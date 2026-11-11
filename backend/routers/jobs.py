"""Access-controlled job status and cancellation; job submission belongs to operation endpoints."""

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..access import check_dataset_access, missing_unless_allowed
from ..database import db
from ..index_actions import public_message
from ..job_queue import JOB_TABLE, CancelRefused, QueueBusy, cancel_job
from ..job_wait import job_outcome
from ..security import private_guest
from .auth import get_current_user

router = APIRouter(tags=['jobs'])
_PUBLIC_COLUMNS = (
    'id, kind, dataset_id, placement, lane, resources, state, progress_done, progress_total, message, '
    'result, error, created_by, created_at, updated_at, started_at, finished_at, '
    'heartbeat_at, attempt, failures, max_failures, available_at, cancel_requested, '
    # An index build's field, so the jobs list can name it and build it again; no other job's arguments.
    "CASE WHEN kind IN ('ann_index_build', 'field_index_build') "
    "THEN COALESCE(args->>'field', args->>'field_name') END AS field, "
    "COALESCE((SELECT username FROM users WHERE users.id = jobs.created_by), "
    "CASE WHEN jobs.dedupe_owner = 'system' THEN 'System' ELSE 'Deleted user' END) AS started_by"
)
_ACTIVE_COLUMNS = (
    'id, kind, placement, lane, state, progress_done, progress_total, message, available_at, created_at, '
    'created_by, started_at'
)


class JobProgress(BaseModel):
    done: int
    total: int | None
    message: str | None


class JobStatus(BaseModel):
    id: UUID
    kind: str
    dataset_id: int | None
    placement: Literal['server', 'compute']
    lane: Literal['maintenance', 'user']
    resources: dict[str, Any]
    field: str | None = None
    state: Literal['queued', 'running', 'succeeded', 'failed', 'cancelled']
    progress: JobProgress
    result: Any = None
    error: Any = None
    created_by: int | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    attempt: int
    failures: int
    max_failures: int
    available_at: datetime
    cancel_requested: bool
    started_by: str


class JobPage(BaseModel):
    items: list[JobStatus]
    next_cursor: str | None


class ActiveJobStatus(BaseModel):
    id: UUID
    kind: str
    placement: Literal['server', 'compute']
    lane: Literal['maintenance', 'user']
    state: Literal['queued', 'running']
    started_by: str
    progress: JobProgress
    cancel_requested: bool
    can_cancel: bool
    stalled: bool
    retry_delayed: bool
    # False for a job about this dataset that owns no dataset lane (an index review): it neither waits
    # behind the lane's owner nor holds up the lane's other jobs.
    dataset_lane: bool = True


# Read-only jobs about one dataset that belong to no dataset lane, so they never wait behind a long
# dataset job (backend/index_advisor.py). They name the dataset in args and are listed with its jobs.
_DATASET_REFERENCE = (
    "jobs.dataset_id IS NULL AND jobs.kind = 'index_review' "
    "AND jobs.args @> jsonb_build_object('scope', 'dataset', 'dataset_id', $1::int)"
)


class ActiveJobPage(BaseModel):
    running: list[ActiveJobStatus]
    resuming: list[ActiveJobStatus]
    waiting: list[ActiveJobStatus]
    next_cursor: str | None


def _can_cancel(job, user: dict) -> bool:
    if user['role'] == 'admin':
        return True
    # Map re-fits are system jobs from a dataset's schedules, which its power users manage (the caller
    # has already checked dataset access); they can stop tonight's fit like they can Run now.
    return user['role'] == 'power_user' and (job['created_by'] == user['id'] or job['kind'] == 'map_refit')


def _message(job, user: dict) -> str | None:
    """The progress message; an index removal's or restore's names the sessions it waits for, which only
    administrators see."""
    return job['message'] if user['role'] == 'admin' else public_message(job['kind'], job['message'])


def job_status(job, user: dict) -> JobStatus:
    """An explicit public allowlist keeps internal arguments, checkpoints and lease credentials private."""
    outcome = job_outcome(job, user)
    if private_guest(user):
        outcome['error'] = None
    return JobStatus(
        id=job['id'],
        kind=job['kind'],
        dataset_id=job['dataset_id'],
        placement=job['placement'],
        lane=job['lane'],
        resources=job['resources'],
        field=job.get('field'),  # job rows read elsewhere (job_queue) don't carry it
        state=job['state'],
        progress=JobProgress(done=job['progress_done'], total=job['progress_total'], message=_message(job, user)),
        created_by=None if private_guest(user) else job['created_by'],
        created_at=job['created_at'],
        updated_at=job['updated_at'],
        started_at=job['started_at'],
        finished_at=job['finished_at'],
        heartbeat_at=job['heartbeat_at'],
        attempt=job['attempt'],
        failures=job['failures'],
        max_failures=job['max_failures'],
        available_at=job['available_at'],
        cancel_requested=job['cancel_requested'],
        started_by='' if private_guest(user) else job['started_by'],
        **outcome,
    )


def _cursor(job) -> str:
    data = json.dumps([job['created_at'].isoformat(), str(job['id'])], separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        decoded = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        parts = json.loads(decoded)
        if not isinstance(parts, list) or len(parts) != 2 or not all(isinstance(part, str) for part in parts):
            raise ValueError('Cursor must contain a timestamp and job ID')
        timestamp, job_id = parts
        created_at = datetime.fromisoformat(timestamp)
        if created_at.tzinfo is None:
            raise ValueError('Timestamp must include a timezone')
        return created_at.astimezone(timezone.utc), UUID(job_id)
    except (ValueError, TypeError, UnicodeError, binascii.Error, OverflowError) as error:
        raise HTTPException(status_code=400, detail='Invalid jobs cursor') from error


async def _authorized_job(job_id: UUID, user: dict):
    job = await db.fetchrow(f'SELECT {_PUBLIC_COLUMNS} FROM {JOB_TABLE} WHERE id = $1', job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Job not found')
    if job['dataset_id'] is None:
        if user['role'] != 'admin':
            raise HTTPException(status_code=403, detail='Only administrators can access global jobs')
    else:
        # A job of a dataset the caller can't open answers as a missing one (a 403 said it exists). A deleted
        # dataset's jobs stay readable to whoever started them, and to administrators.
        with missing_unless_allowed('Job not found'):
            try:
                await check_dataset_access(job['dataset_id'], user)
            except HTTPException as error:
                if error.status_code != 404 or (user['role'] != 'admin' and job['created_by'] != user['id']):
                    raise
    return job


@router.get('/jobs/{job_id}', response_model=JobStatus, response_model_exclude_unset=True)
async def get_job(job_id: UUID, user: dict = Depends(get_current_user)):
    return job_status(await _authorized_job(job_id, user), user)


@router.get('/datasets/{dataset_id}/jobs', response_model=JobPage, response_model_exclude_unset=True)
async def list_dataset_jobs(
    dataset_id: int,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    user: dict = Depends(get_current_user),
    include_system_statistics: bool = False,
):
    await check_dataset_access(dataset_id, user)
    scope = 'dataset_id = $1'
    if not include_system_statistics:
        scope += (
            " AND NOT (kind IN ('statistics','concept_scoring','duplicate_maintenance','map_place')"
            " AND dedupe_owner='system' AND state='succeeded'"
            " AND NOT barrier @> '{\"full\":true}')"
        )
    return await _job_page(scope, [dataset_id], limit=limit, cursor=cursor, user=user, also=_DATASET_REFERENCE)


@router.get('/admin/jobs', response_model=JobPage, response_model_exclude_unset=True)
async def list_global_jobs(
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    user: dict = Depends(get_current_user),
):
    """Jobs that belong to no dataset, such as scheduled maintenance."""
    if user['role'] != 'admin':
        raise HTTPException(status_code=403, detail='Only administrators can access global jobs')
    return await _job_page('dataset_id IS NULL', [], limit=limit, cursor=cursor, user=user)


async def _job_page(
    scope: str,
    scope_args: list,
    *,
    limit: int,
    cursor: str | None,
    user: dict,
    also: str | None = None,
) -> JobPage:
    """Newest first. `scope` is a fixed predicate whose parameters come first; `also` adds the jobs of a
    second predicate (same parameters), each part read through its own index, merged by the same keyset."""
    created_at, job_id = _decode_cursor(cursor) if cursor is not None else (None, None)
    after = len(scope_args)
    keyset = (
        f'(${after + 1}::timestamptz IS NULL OR (created_at, id) < (${after + 1}, ${after + 2}::uuid)) '
        f'ORDER BY created_at DESC, id DESC LIMIT ${after + 3}'
    )
    query = f'SELECT {_PUBLIC_COLUMNS} FROM {JOB_TABLE} WHERE {scope} AND {keyset}'
    if also is not None:
        query = (
            f'({query}) UNION ALL (SELECT {_PUBLIC_COLUMNS} FROM {JOB_TABLE} WHERE {also} AND {keyset}) '
            f'ORDER BY created_at DESC, id DESC LIMIT ${after + 3}'
        )
    rows = await db.fetch(query, *scope_args, created_at, job_id, limit + 1)
    return JobPage(
        items=[job_status(row, user) for row in rows[:limit]],
        next_cursor=_cursor(rows[limit - 1]) if len(rows) > limit else None,
    )


def _active_cursor(job) -> str:
    values = [job['available_at'].isoformat(), job['created_at'].isoformat(), str(job['id'])]
    return base64.urlsafe_b64encode(json.dumps(values, separators=(',', ':')).encode()).decode().rstrip('=')


def _decode_active_cursor(cursor):
    if cursor is None:
        return None, None, None
    try:
        values = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        if not isinstance(values, list) or len(values) != 3 or not all(isinstance(value, str) for value in values):
            raise ValueError('Invalid cursor')
        timestamps = [datetime.fromisoformat(value) for value in values[:2]]
        if any(timestamp.tzinfo is None for timestamp in timestamps):
            raise ValueError('Cursor requires timezone')
        available_at, created_at = (timestamp.astimezone(timezone.utc) for timestamp in timestamps)
        return available_at, created_at, UUID(values[2])
    except (ValueError, TypeError, UnicodeError, binascii.Error, OverflowError) as error:
        raise HTTPException(status_code=400, detail='Invalid active jobs cursor') from error


@router.get('/datasets/{dataset_id}/jobs/active', response_model=ActiveJobPage)
async def list_active_dataset_jobs(
    dataset_id: int,
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    user: dict = Depends(get_current_user),
):
    await check_dataset_access(dataset_id, user)
    available_at, created_at, identifier = _decode_active_cursor(cursor)
    columns = ', '.join(f'jobs.{column}' for column in _ACTIVE_COLUMNS.split(', '))
    rows = await db.fetch(
        f"""WITH active AS (
                SELECT * FROM jobs WHERE dataset_id=$1
                  AND (state='running' OR (state='queued' AND started_at IS NOT NULL))
                UNION ALL
                (SELECT * FROM jobs WHERE dataset_id=$1 AND state='queued' AND started_at IS NULL
                   AND ($2::timestamptz IS NULL OR (available_at,created_at,id)>($2,$3,$4::uuid))
                 ORDER BY available_at,created_at,id LIMIT $5)
                UNION ALL
                SELECT * FROM jobs WHERE {_DATASET_REFERENCE}
                  AND (state='running' OR (state='queued' AND (started_at IS NOT NULL
                       OR $2::timestamptz IS NULL OR (available_at,created_at,id)>($2,$3,$4::uuid))))
            )
            SELECT {columns}, controls.cancel_requested, jobs.dataset_id IS NOT NULL AS dataset_lane,
                (jobs.state = 'running' AND jobs.checkpoint ? 'uncancellable') AS uncancellable,
                (jobs.state='running' AND jobs.lease_expires_at<=clock_timestamp()) AS stalled,
                (jobs.state='queued' AND jobs.available_at>clock_timestamp()) AS retry_delayed,
                COALESCE(submitter.username,
                    CASE WHEN jobs.dedupe_owner='system' THEN 'System' ELSE 'Deleted user' END) AS started_by
            FROM active jobs JOIN job_controls controls ON controls.job_id=jobs.id
            LEFT JOIN users submitter ON submitter.id=jobs.created_by
            ORDER BY (jobs.state='running') DESC,jobs.available_at,jobs.created_at,jobs.id""",
        dataset_id,
        available_at,
        created_at,
        identifier,
        limit + 1,
    )
    running = [row for row in rows if row['state'] == 'running']
    resuming = [row for row in rows if row['state'] == 'queued' and row['started_at'] is not None]
    waiting = [row for row in rows if row['state'] == 'queued' and row['started_at'] is None]

    def public(row):
        return ActiveJobStatus(
            id=row['id'],
            kind=row['kind'],
            placement=row['placement'],
            lane=row['lane'],
            state=row['state'],
            started_by='' if private_guest(user) else row['started_by'],
            progress=JobProgress(done=row['progress_done'], total=row['progress_total'], message=_message(row, user)),
            cancel_requested=row['cancel_requested'],
            can_cancel=_can_cancel(row, user) and not row.get('uncancellable'),
            stalled=row['stalled'],
            retry_delayed=row['retry_delayed'],
            dataset_lane=row['dataset_lane'],
        )

    return ActiveJobPage(
        running=[public(row) for row in running],
        resuming=[public(row) for row in resuming],
        waiting=[public(row) for row in waiting[:limit]],
        next_cursor=_active_cursor(waiting[limit - 1]) if len(waiting) > limit else None,
    )


@router.post('/jobs/{job_id}/cancel', response_model=JobStatus, response_model_exclude_unset=True)
async def request_job_cancellation(job_id: UUID, user: dict = Depends(get_current_user)):
    job = await _authorized_job(job_id, user)
    if not _can_cancel(job, user):
        raise HTTPException(
            status_code=403,
            detail='Only an administrator or the submitting power user can cancel a job '
            '(power users can also cancel map re-fits in their datasets)',
        )
    try:
        cancelled = await cancel_job(db.pool, job_id)
    except CancelRefused as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except QueueBusy as error:
        raise HTTPException(status_code=409, detail='Job state is busy; retry cancellation') from error
    if cancelled is None:
        raise HTTPException(status_code=404, detail='Job not found')
    # The queue returns the bare job row; the submitter's name comes from the authorized read.
    return job_status({**cancelled, 'started_by': job['started_by']}, user)


@router.post('/jobs/{job_id}/resume', status_code=202)
async def resume_bulk_job(job_id: UUID, user: dict = Depends(get_current_user)):
    from .. import bulk_jobs, concept_jobs

    job = await _authorized_job(job_id, user)
    if not _can_cancel(job, user):
        raise HTTPException(403, 'Only an administrator or the submitting power user can resume a job')
    if job['kind'] == concept_jobs.EXPLICIT_KIND:
        # Continues the same checkpoint while the job still owns its concept.
        try:
            return await concept_jobs.resume_explicit(db.pool, job_id)
        except QueueBusy as error:
            raise HTTPException(409, 'Job state is busy; retry resume') from error
    if job['kind'] != bulk_jobs.KIND:
        raise HTTPException(400, 'This job does not support bulk resume')
    operation = await db.fetchval("SELECT args->>'operation' FROM jobs WHERE id=$1", job_id)
    if operation in ('delete', 'delete_embeddings') and user['role'] != 'admin':
        raise HTTPException(403, 'Only an administrator can resume a deletion')
    try:
        return await bulk_jobs.resume(db.pool, job_id)
    except QueueBusy as error:
        raise HTTPException(409, 'Job state is busy; retry resume') from error
    except asyncpg.UniqueViolationError as error:
        # The stopped job keeps owning its operation; the lane admits one started owner at a time.
        raise HTTPException(
            409,
            'Another job of this dataset is running; resume this one when it finishes',
            headers={'Retry-After': '5'},
        ) from error


@router.post('/jobs/{job_id}/discard')
async def discard_bulk_job(job_id: UUID, user: dict = Depends(get_current_user)):
    """Give up a stopped bulk job: completed chunks stay, the rest never runs and no longer blocks revert."""
    from .. import bulk_jobs

    job = await _authorized_job(job_id, user)
    if not _can_cancel(job, user):
        raise HTTPException(403, 'Only an administrator or the submitting power user can discard a job')
    if job['kind'] != bulk_jobs.KIND:
        raise HTTPException(400, 'Only bulk jobs can be discarded')
    try:
        return await bulk_jobs.discard(db.pool, job_id, user)
    except QueueBusy as error:
        raise HTTPException(409, 'Job state is busy; retry discard') from error
