"""Access-controlled job status and cancellation; job submission belongs to operation endpoints."""

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from starlette.datastructures import State

from ..access import (
    MANAGE,
    AssistantClosed,
    Level,
    LevelRefused,
    check_dataset_access,
    checks_level,
    level_number,
    level_refusal,
    missing_unless_allowed,
)
from ..assistant.identity import assistant_door
from ..database import db
from ..index_actions import public_message
from ..job_queue import JOB_TABLE, CancelRefused, QueueBusy, cancel_job
from ..job_wait import job_outcome
from ..security import private_guest
from .auth import get_current_user

router = APIRouter(tags=['jobs'])
# A queued job that hasn't started while another owns its exclusive resource (job_queue): waiting for that one.
_EXCLUSIVE_WAITING = (
    "(jobs.state = 'queued' AND jobs.started_at IS NULL AND jobs.resources ? 'exclusive' AND EXISTS ("
    "SELECT 1 FROM jobs owner WHERE owner.resources->>'exclusive' = jobs.resources->>'exclusive' "
    "AND owner.id <> jobs.id "
    "AND (owner.state = 'running' OR (owner.state = 'queued' AND owner.started_at IS NOT NULL))))"
)
_WAITING_FOR = {  # by the resource's name before any ':' (a follower's sync has one per primary)
    'index_build': 'for another index build to finish (one index build at a time)',
    'map_fit': 'for another map re-fit to finish (one at a time)',
    'follower_sync': "for this primary's running sync to finish (one at a time per primary)",
}
# A bulk edit of tags alone (`tags`, or a mirror's own `@local.tags`), which the tag level may start (and so stop,
# resume or discard: _kind_level).
_TAGS_ONLY = (
    "(jobs.kind = 'bulk_mutation' AND jobs.args->>'operation' = 'tag' "
    "AND jobs.args->>'field_name' IN ('tags', '@local.tags'))"
)
_PUBLIC_COLUMNS = (
    'id, kind, dataset_id, placement, lane, resources, state, progress_done, progress_total, message, '
    'result, error, created_by, created_at, updated_at, started_at, finished_at, '
    'heartbeat_at, attempt, failures, max_failures, available_at, cancel_requested, '
    f'{_TAGS_ONLY} AS tags_only, '
    # An index build's field, so the jobs list can name it and build it again; no other job's arguments.
    "CASE WHEN kind IN ('ann_index_build', 'field_index_build') "
    "THEN COALESCE(args->>'field', args->>'field_name') END AS field, "
    "COALESCE((SELECT username FROM users WHERE users.id = jobs.created_by), "
    "CASE WHEN jobs.dedupe_owner = 'system' THEN 'System' ELSE 'Deleted user' END) AS started_by, "
    f'{_EXCLUSIVE_WAITING} AS exclusive_waiting'
)
_ACTIVE_COLUMNS = (
    'id, kind, placement, lane, state, progress_done, progress_total, message, available_at, created_at, '
    'created_by, started_at'
)
# The level in its dataset a job's kind needs, so its starter may stop, resume or discard it while they still have
# that level; any other kind, edit. None: an administrator's (a patch import reads a bucket with the server's
# credentials; a mirror's sync belongs to its link).
_KIND_LEVELS: dict[str, Optional[str]] = {
    'dataset_deletion': 'manage',
    'patch_vector_import': None,
    'follower_sync': None,
}


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
    # What a queued job that hasn't started waits for, when it isn't its turn yet: "for ..." (waiting_for).
    waiting_for: str | None = None
    # The job lists only: whether the caller may stop, resume or discard it (_can_cancel), as the active list's
    # can_cancel says for a running one.
    can_control: bool | None = None


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
    waiting_for: str | None = None  # as JobStatus.waiting_for


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


def _kind_level(job) -> Optional[str]:
    return 'tag' if job.get('tags_only') else _KIND_LEVELS.get(job['kind'], 'edit')


def _yours(job, user: dict) -> bool:
    # Map re-fits are system jobs from a dataset's schedules, which who has edit there manages: they can stop
    # tonight's fit like they can Run now.
    return job['created_by'] == user['id'] or job['kind'] == 'map_refit'


def _can_cancel(job, user: dict, level: Optional[int]) -> bool:
    """Whether `user`, whose level in the job's dataset is `level`, may stop, resume or discard the job: an
    administrator; for a kind that isn't an administrator's alone, a manager of its dataset, or its starter while
    they have the level its kind needs."""
    if user['role'] == 'admin':
        return True
    needed = _kind_level(job)
    if needed is None or level is None:
        return False
    return level >= MANAGE or (level >= level_number(needed) and _yours(job, user))


async def _level_in(dataset_id: int, user: dict, state: Any) -> Optional[int]:
    """The caller's level in the dataset: the highest check_dataset_access grants them, from the one lookup it keeps
    on `state`. None: the dataset is gone (a deleted dataset's jobs stay readable to who started them)."""
    if user['role'] == 'admin' and not assistant_door(user):
        return MANAGE
    levels: tuple[Level, ...] = ('manage', 'edit', 'tag')
    for level in levels:
        try:
            await check_dataset_access(dataset_id, user, level, state)
            return level_number(level)
        except AssistantClosed:
            raise
        except LevelRefused:
            continue
        except HTTPException as error:
            if error.status_code != 404:
                raise
            return None
    return level_number('view')


async def _controlled_job(job_id: UUID, user: dict):
    """The job, if `user` may stop, resume or discard it (_can_cancel); 403 naming the level they lack."""
    job = await _authorized_job(job_id, user)
    level = None if job['dataset_id'] is None else await _level_in(job['dataset_id'], user, State())
    if not _can_cancel(job, user, level):
        needed = _kind_level(job)
        if needed is None:
            raise HTTPException(status_code=403, detail='Only an administrator can stop, resume or discard this job')
        raise level_refusal(needed if _yours(job, user) else 'manage')
    return job


def waiting_for(job, user: dict) -> str | None:
    """What a queued job that hasn't started waits for: another job owning its exclusive resource (one index build,
    one map re-fit at a time), or what put it aside (a field rebuild parked while a bulk job holds its dataset; the
    job it names is left out for a private guest, who sees no job's error)."""
    if job['state'] != 'queued' or job['started_at'] is not None:
        return None
    if job.get('exclusive_waiting'):
        resource = job['resources'].get('exclusive')
        return _WAITING_FOR.get(resource.split(':')[0], f'for another job using {resource} to finish')
    error = job.get('error')
    if isinstance(error, dict) and error.get('code') == 'fields_held':
        if private_guest(user):
            return 'for an unfinished bulk job on this dataset'
        return re.sub(r'^waiting\s+', '', str(error.get('message') or ''), flags=re.IGNORECASE) or None
    return None


def _message(job, user: dict) -> str | None:
    """The progress message; an index removal's or restore's names the sessions it waits for, which only
    administrators see."""
    return job['message'] if user['role'] == 'admin' else public_message(job['kind'], job['message'])


def job_status(job, user: dict, can_control: Optional[bool] = None) -> JobStatus:
    """An explicit public allowlist keeps internal arguments, checkpoints and lease credentials private. Only the job
    lists give `can_control`; without it the field stays unset, so the answer leaves it out."""
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
        waiting_for=waiting_for(job, user),
        **outcome,
        **({} if can_control is None else {'can_control': can_control}),
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
        if assistant_door(user):  # it may name datasets closed to the assistant: as a job that doesn't exist
            raise HTTPException(status_code=404, detail='Job not found')
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
    state = State()  # one dataset lookup for both
    await check_dataset_access(dataset_id, user, state=state)
    level = await _level_in(dataset_id, user, state)  # who may stop, resume or discard which job (_can_cancel)
    scope = 'dataset_id = $1'
    if not include_system_statistics:
        scope += (
            " AND NOT (kind IN ('statistics','concept_scoring','duplicate_maintenance','map_place')"
            " AND dedupe_owner='system' AND state='succeeded'"
            " AND NOT barrier @> '{\"full\":true}')"
        )
    return await _job_page(
        scope,
        [dataset_id],
        limit=limit,
        cursor=cursor,
        user=user,
        also=_DATASET_REFERENCE,
        level=level,
    )


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


JobState = Literal['queued', 'running', 'succeeded', 'failed', 'cancelled']
_STATES: tuple[JobState, ...] = ('queued', 'running', 'succeeded', 'failed', 'cancelled')
_ACTIVE = ('queued', 'running')  # not ended: listed and counted whatever the window
# The dataset a job is about: the one it belongs to, or the one its arguments name (a dataset's index review, a vector
# index's removal belong to none and name theirs).
_ABOUT = (
    "COALESCE(jobs.dataset_id, CASE WHEN jobs.args->>'dataset_id' ~ '^[0-9]{1,9}$' "
    "THEN (jobs.args->>'dataset_id')::int END)"
)


def _all_job(row) -> dict[str, Any]:
    return {
        'id': str(row['id']),
        'kind': row['kind'],
        'state': row['state'],
        'dataset_id': row['about'],
        'dataset_name': row['dataset_name'],
        'field': row['field'],
        'started_by': row['started_by'],
        'created_at': row['created_at'].isoformat(),
        'started_at': row['started_at'].isoformat() if row['started_at'] else None,
        'finished_at': row['finished_at'].isoformat() if row['finished_at'] else None,
        'progress': {'done': row['progress_done'], 'total': row['progress_total'], 'message': row['message']},
        'failures': row['failures'],
        'max_failures': row['max_failures'],
        'cancel_requested': row['cancel_requested'],
        'error': row['error'],
        # A backup's own result, all it holds (null until it has succeeded); no other kind's result is given.
        **({'backup': row['backup']} if row['kind'] == 'backup' else {}),
    }


class _Args:
    """The parameters of one statement by name, each placeholder made when the text first uses it."""

    def __init__(self, values: dict[str, Any]):
        self.values = values
        self.order: list[str] = []

    def __getitem__(self, name: str) -> str:
        if name not in self.order:
            self.order.append(name)
        return f'${self.order.index(name) + 1}'

    def used(self) -> list[Any]:
        return [self.values[name] for name in self.order]


def _parts(args: _Args, columns: str, states: Any, *, kind: bool = False, tail: str = '') -> str:
    """The jobs of `states` in the window as a UNION ALL of fixed predicates, one per state and per way a job is about
    the dataset, so each is a range of one index (idx_jobs_state_created): a queued or running job whatever its age,
    one that has ended when it was created at or after `since`; the jobs a dataset holds, and those of no dataset that
    name it (idx_jobs_named_dataset)."""
    branches = ['']
    if args.values['dataset'] is not None:
        named = args['dataset']
        branches = [
            f'jobs.dataset_id = {named}',
            f"jobs.dataset_id IS NULL AND jobs.args ? 'dataset_id' AND jobs.args->>'dataset_id' = {named}::text",
        ]
    found = []
    for state in states:
        for branch in branches:
            where = [f"jobs.state = '{state}'", branch]
            if args.values['since'] is not None and state not in _ACTIVE:
                where.append(f"jobs.created_at >= {args['since']}")
            if kind:
                where.append(f"jobs.kind = {args['kind']}")
            found.append(f"(SELECT {columns} FROM jobs WHERE {' AND '.join(part for part in where if part)}{tail})")
    return ' UNION ALL '.join(found)


@router.get('/admin/jobs/all')
async def list_all_jobs(
    state: Optional[list[JobState]] = Query(None),
    kind: Optional[str] = Query(None, min_length=1, max_length=200),
    dataset_id: Optional[int] = Query(None, ge=1, le=2**31 - 1),
    since: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """Jobs across the whole server, newest first, for an administrator: those in `state` (repeatable; every state
    without it), of one `kind`, about one dataset (`dataset_id`: the jobs it holds, and those of no dataset that name
    it, such as its index reviews), in the window: every queued or running job however old, and the jobs that have
    ended created at or after `since`. `total` counts what matches; `counts` is by state for the same kind, dataset and
    window, whatever `state` asks; `kinds` are the kinds of the jobs in the window, whatever the other filters. A job's
    result, and what it was asked beyond the dataset it is about, are never given, but for a backup's own `backup`:
    `{file, size_bytes, tables}` once it has succeeded."""
    if user['role'] != 'admin':
        raise HTTPException(status_code=403, detail='Only administrators can access global jobs')
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    wanted = list(dict.fromkeys(state)) if state else list(_STATES)
    values = {'dataset': dataset_id, 'since': since, 'kind': kind, 'limit': limit, 'offset': offset}
    async with db.pool.acquire() as connection, connection.transaction(isolation='repeatable_read', readonly=True):
        if dataset_id is not None and not await connection.fetchval(
            'SELECT EXISTS (SELECT 1 FROM datasets WHERE id = $1)',
            dataset_id,
        ):
            raise HTTPException(status_code=404, detail='Dataset not found')
        # The window's jobs of every state by kind: the counts, and the kinds a model can ask for (of every dataset).
        args = _Args(values)
        tally = await connection.fetch(
            f"""SELECT kind, state, count(*) AS jobs FROM ({_parts(args, 'jobs.kind, jobs.state', _STATES)}) found
                GROUP BY kind, state""",
            *args.used(),
        )
        kinds = {row['kind'] for row in tally}
        if dataset_id is not None:
            args = _Args({**values, 'dataset': None})
            found = _parts(args, 'jobs.kind', _STATES)
            kinds = {
                row['kind']
                for row in await connection.fetch(f'SELECT DISTINCT kind FROM ({found}) found', *args.used())
            }
        counts = {name: 0 for name in _STATES}
        for row in tally:
            if kind is None or row['kind'] == kind:
                counts[row['state']] += row['jobs']
        total = sum(counts[name] for name in wanted)
        rows = []
        if offset < total:
            args = _Args({**values, 'enough': offset + limit})
            # Each part is a range of idx_jobs_state_created (or of a dataset's indexes), read newest first and no
            # further than the page needs.
            found = _parts(
                args,
                'jobs.id, jobs.created_at',
                wanted,
                kind=kind is not None,
                tail=f' ORDER BY jobs.created_at DESC, jobs.id DESC LIMIT {args["enough"]}',
            )
            rows = await connection.fetch(
                f"""WITH page AS (
                        SELECT id, created_at FROM ({found}) found
                        ORDER BY created_at DESC, id DESC LIMIT {args['limit']} OFFSET {args['offset']}
                    )
                    SELECT jobs.id, jobs.kind, jobs.state, {_ABOUT} AS about, d.name AS dataset_name,
                           CASE WHEN jobs.kind IN ('ann_index_build', 'field_index_build')
                                THEN COALESCE(jobs.args->>'field', jobs.args->>'field_name') END AS field,
                           COALESCE(u.username, CASE WHEN jobs.dedupe_owner = 'system' THEN 'System'
                                                     ELSE 'Deleted user' END) AS started_by,
                           jobs.created_at, jobs.started_at, jobs.finished_at, jobs.progress_done,
                           jobs.progress_total, jobs.message, jobs.failures, jobs.max_failures,
                           COALESCE(controls.cancel_requested, FALSE) AS cancel_requested, jobs.error,
                           CASE WHEN jobs.kind = 'backup' AND jobs.state = 'succeeded'
                                     AND jsonb_typeof(jobs.result) = 'object'
                                THEN jsonb_build_object('file', jobs.result->'file', 'size_bytes',
                                                        jobs.result->'size_bytes', 'tables', jobs.result->'tables')
                           END AS backup
                    FROM page JOIN jobs ON jobs.id = page.id
                    -- Every job gets its row (the jobs_initialize_controls trigger); a LEFT JOIN all the same, so a job
                    -- missing one is never dropped from a page its total counts.
                    LEFT JOIN job_controls controls ON controls.job_id = jobs.id
                    LEFT JOIN users u ON u.id = jobs.created_by
                    LEFT JOIN datasets d ON d.id = {_ABOUT}
                    ORDER BY page.created_at DESC, page.id DESC""",
                *args.used(),
            )
    following = offset + len(rows)
    return {
        'jobs': [_all_job(row) for row in rows],
        'total': total,
        'counts': counts,
        'kinds': sorted(kinds),
        'offset': offset,
        'next_offset': following if rows and following < total else None,
    }


async def _job_page(
    scope: str,
    scope_args: list,
    *,
    limit: int,
    cursor: str | None,
    user: dict,
    also: str | None = None,
    level: Optional[int] = None,
) -> JobPage:
    """Newest first. `scope` is a fixed predicate whose parameters come first; `also` adds the jobs of a
    second predicate (same parameters), each part read through its own index, merged by the same keyset.

    `level` is the caller's in the one dataset whose jobs are listed, for each job's can_control: a list holds one
    dataset's jobs and jobs of no dataset (an index review naming it; every job of the global list), which the
    level doesn't reach (_controlled_job). A list spanning datasets would need one level per dataset."""
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
        items=[
            job_status(row, user, can_control=_can_cancel(row, user, None if row['dataset_id'] is None else level))
            for row in rows[:limit]
        ],
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
    state = State()  # one dataset lookup for both
    await check_dataset_access(dataset_id, user, state=state)
    level = await _level_in(dataset_id, user, state)  # who may stop which job (_can_cancel)
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
                {_TAGS_ONLY} AS tags_only,
                (jobs.state = 'running' AND jobs.checkpoint ? 'uncancellable') AS uncancellable,
                (jobs.state='running' AND jobs.lease_expires_at<=clock_timestamp()) AS stalled,
                (jobs.state='queued' AND jobs.available_at>clock_timestamp()) AS retry_delayed,
                jobs.resources, jobs.error, {_EXCLUSIVE_WAITING} AS exclusive_waiting,
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
            can_cancel=_can_cancel(row, user, level) and not row.get('uncancellable'),
            stalled=row['stalled'],
            retry_delayed=row['retry_delayed'],
            dataset_lane=row['dataset_lane'],
            waiting_for=waiting_for(row, user),
        )

    return ActiveJobPage(
        running=[public(row) for row in running],
        resuming=[public(row) for row in resuming],
        waiting=[public(row) for row in waiting[:limit]],
        next_cursor=_active_cursor(waiting[limit - 1]) if len(waiting) > limit else None,
    )


_CONTROL = (
    "manage in its dataset; its starter: the level its kind needs; a map re-fit: edit; "
    "a patch import or a mirror's sync: an administrator"
)
_CONTROL_WHILE_DELETING = "its dataset comes from the job; the queue runs none of a deleting dataset's jobs"


@router.post('/jobs/{job_id}/cancel', response_model=JobStatus, response_model_exclude_unset=True)
@checks_level(_CONTROL, while_deleting=_CONTROL_WHILE_DELETING)
async def request_job_cancellation(job_id: UUID, user: dict = Depends(get_current_user)):
    job = await _controlled_job(job_id, user)
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
@checks_level(_CONTROL, while_deleting=_CONTROL_WHILE_DELETING)
async def resume_bulk_job(job_id: UUID, user: dict = Depends(get_current_user)):
    from .. import bulk_jobs, concept_jobs

    job = await _controlled_job(job_id, user)
    if job['kind'] == concept_jobs.EXPLICIT_KIND:
        # Continues the same checkpoint while the job still owns its concept.
        try:
            return await concept_jobs.resume_explicit(db.pool, job_id)
        except QueueBusy as error:
            raise HTTPException(409, 'Job state is busy; retry resume') from error
    if job['kind'] != bulk_jobs.KIND:
        raise HTTPException(400, 'This job does not support bulk resume')
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
@checks_level(_CONTROL, while_deleting=_CONTROL_WHILE_DELETING)
async def discard_bulk_job(job_id: UUID, user: dict = Depends(get_current_user)):
    """Give up a stopped bulk job: completed chunks stay, the rest never runs and no longer blocks revert."""
    from .. import bulk_jobs

    job = await _controlled_job(job_id, user)
    if job['kind'] != bulk_jobs.KIND:
        raise HTTPException(400, 'Only bulk jobs can be discarded')
    try:
        return await bulk_jobs.discard(db.pool, job_id, user)
    except QueueBusy as error:
        raise HTTPException(409, 'Job state is busy; retry discard') from error
