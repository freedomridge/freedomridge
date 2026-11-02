"""Index advisor views: Dataset Settings → Indexes and Settings → System status (administrators only).

Reads return the newest review of the scope, the review queued or running now and the scope's weekly
schedule. "Review now" queues a read-only `index_review` job (backend/index_advisor.py), or joins the
one already queued or running, and follows the five-second rule. A dataset's weekly review is a dataset
schedule (Jobs → Schedules, job type index_review); the system-wide one is `system_job_schedules`.
Remove and Restore run as jobs (backend/index_actions.py); the reads list the scope's open removals.
"""

import time
from datetime import time as clock_time
from typing import Any, Dict, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictInt, model_validator

from .. import dataset_schedules, index_actions, index_advisor, job_wait
from ..access import check_dataset_access
from ..database import db
from ..job_queue import QueueBusy
from .auth import require_role

router = APIRouter(tags=['index-advisor'])


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _rule(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        'enabled': row['enabled'],
        'frequency': row['frequency'],
        'weekday': row['weekday'],
        'time': row['time_of_day'].strftime('%H:%M'),
        'timezone': row['timezone'],
        'next_run_at': _iso(row['next_run_at']),
        'last_run_at': _iso(row['last_run_at']),
    }


async def _submit(dataset_id: Optional[int], user: dict, wait: bool):
    started = time.monotonic()
    try:
        job, _ = await index_advisor.submit(db.pool, dataset_id, user['id'])
    except QueueBusy as error:
        raise HTTPException(
            status_code=409,
            detail='The job queue is busy; retry',
            headers={'Retry-After': '1'},
        ) from error
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)


@router.get('/datasets/{id}/index-advisor')
async def dataset_index_advisor(id: int, user: dict = Depends(require_role('admin'))):
    dataset = await check_dataset_access(id, user)
    async with db.pool.acquire() as connection:
        state = await index_advisor.latest(connection, id)
        schedule = await connection.fetchrow(
            f"""SELECT {dataset_schedules.COLUMNS} FROM dataset_job_schedules s
                WHERE s.dataset_id=$1 AND s.job_type=$2""",
            id,
            index_advisor.KIND,
        )
        removed = await index_actions.removals(connection, id)
        pending = await index_actions.pending(connection, id)
    rule = _rule(schedule)
    if rule is not None:
        rule['id'] = schedule['id']
    return {
        **state,
        'dataset': {'id': id, 'name': dataset['name']},
        'schedule': rule,
        'removed': removed,
        'pending_actions': pending,
        'window_days': index_advisor.WINDOW.days,
    }


@router.post('/datasets/{id}/index-advisor/review')
async def review_dataset_indexes(id: int, wait: bool = True, user: dict = Depends(require_role('admin'))):
    await check_dataset_access(id, user)
    return await _submit(id, user, wait)


@router.get('/admin/index-advisor')
async def system_index_advisor(user: dict = Depends(require_role('admin'))):
    async with db.pool.acquire() as connection:
        state = await index_advisor.latest(connection, None)
        schedule = await dataset_schedules.read_system_schedule(connection)
        removed = await index_actions.removals(connection, None)
        pending = await index_actions.pending(connection, None)
    return {
        **state,
        'schedule': _rule(schedule),
        'removed': removed,
        'pending_actions': pending,
        'window_days': index_advisor.WINDOW.days,
    }


@router.post('/admin/index-advisor/review')
async def review_system_indexes(wait: bool = True, user: dict = Depends(require_role('admin'))):
    return await _submit(None, user, wait)


class SystemReviewSchedule(BaseModel):
    enabled: StrictBool
    frequency: Literal['daily', 'weekly'] = 'weekly'
    weekday: Optional[StrictInt] = Field(0, ge=0, le=6)  # weekly only; 0 = Sunday
    time: str = Field('03:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    timezone: str = 'UTC'

    @model_validator(mode='after')
    def _consistent(self):
        if self.frequency == 'daily':
            self.weekday = None
        elif self.weekday is None:
            raise ValueError('A weekly schedule needs a weekday (0 = Sunday)')
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f'Unknown time zone {self.timezone!r}')
        return self


@router.put('/admin/index-advisor/schedule')
async def save_system_review_schedule(body: SystemReviewSchedule, user: dict = Depends(require_role('admin'))):
    hour, minute = body.time.split(':')
    rule = {
        'frequency': body.frequency,
        'weekday': body.weekday,
        'time_of_day': clock_time(int(hour), int(minute)),
        'timezone': body.timezone,
    }
    async with db.pool.acquire() as connection, connection.transaction():
        now = await connection.fetchval('SELECT clock_timestamp()')
        row = await connection.fetchrow(
            f"""INSERT INTO system_job_schedules (job_type, enabled, frequency, weekday, time_of_day, timezone,
                    next_run_at, updated_by, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,clock_timestamp())
                ON CONFLICT (job_type) DO UPDATE SET enabled=EXCLUDED.enabled, frequency=EXCLUDED.frequency,
                    weekday=EXCLUDED.weekday, time_of_day=EXCLUDED.time_of_day, timezone=EXCLUDED.timezone,
                    next_run_at=EXCLUDED.next_run_at, updated_by=EXCLUDED.updated_by, updated_at=clock_timestamp()
                RETURNING {dataset_schedules.SYSTEM_COLUMNS}""",
            index_advisor.KIND,
            body.enabled,
            body.frequency,
            body.weekday,
            rule['time_of_day'],
            body.timezone,
            dataset_schedules.next_slot(rule, now) if body.enabled else None,
            user['id'],
        )
    return _rule(row)


class RemoveIndex(BaseModel):
    # The definition the administrator saw: an index changed or re-created since is not removed.
    definition: str = Field(..., min_length=1)
    reason: Optional[Literal['unused', 'overlap', 'invalid']] = None


@router.post('/admin/indexes/{name}/remove')
async def remove_index(name: str, body: RemoveIndex, wait: bool = True, user: dict = Depends(require_role('admin'))):
    """Queue DROP INDEX CONCURRENTLY of a removable index (a field index marks its field not indexed
    first), recording what it takes to restore it; answers like every job-backed endpoint."""
    started = time.monotonic()
    try:
        job = await index_actions.submit_drop(db.pool, name, user, definition=body.definition, reason=body.reason)
    except QueueBusy as error:
        raise HTTPException(
            status_code=409,
            detail='The job queue is busy; retry',
            headers={'Retry-After': '1'},
        ) from error
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)


@router.post('/admin/indexes/{name}/restore')
async def restore_index(name: str, wait: bool = True, user: dict = Depends(require_role('admin'))):
    """Re-create an index the advisor removed: CREATE INDEX CONCURRENTLY from its recorded definition,
    or, for a field index, the field index build (promote), which follows the field's current type."""
    started = time.monotonic()
    async with db.pool.acquire() as connection:
        removal = await connection.fetchrow(
            """SELECT kind, dataset_id, field_name FROM removed_indexes
               WHERE index_name = $1 AND restored_at IS NULL AND closed_at IS NULL""",
            name,
        )
    if removal is not None and removal['kind'] == 'field':
        from .datasets import promote_field

        if removal['dataset_id'] is None or not removal['field_name']:
            raise HTTPException(status_code=409, detail='This field index no longer has a field to index')
        answer = await promote_field(removal['dataset_id'], removal['field_name'], user=user, wait=wait)
        # Show the record as restoring while the field index build runs (it closes the record when done).
        async with db.pool.acquire() as connection:
            await connection.execute(
                """UPDATE removed_indexes r SET restore_job_id = j.id
                   FROM (SELECT id FROM jobs WHERE kind = 'field_index_build' AND dataset_id = $2
                           AND args->>'field_name' = $3 ORDER BY created_at DESC LIMIT 1) j
                   WHERE r.index_name = $1 AND r.restored_at IS NULL AND r.closed_at IS NULL""",
                name,
                removal['dataset_id'],
                removal['field_name'],
            )
        return answer
    try:
        job = await index_actions.submit_restore(db.pool, name, user)
    except QueueBusy as error:
        raise HTTPException(
            status_code=409,
            detail='The job queue is busy; retry',
            headers={'Retry-After': '1'},
        ) from error
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)
