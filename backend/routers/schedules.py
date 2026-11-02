"""Dataset Jobs panel → Schedules: recurring jobs for one dataset (backend/dataset_schedules.py).

Anyone with access to the dataset can read its schedules; power users and admins create, change,
delete and Run now map re-fits. The read-only index review (backend/index_advisor.py) is an
administrator's tool: only admins create, change, delete and run its schedule. Scheduled and Run-now
jobs show in Running and History like any other job.
"""

from datetime import datetime, time
from typing import Any, Dict, Literal, Optional, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictInt, model_validator

from .. import dataset_schedules, index_advisor, map_refit
from ..access import require_dataset_access
from ..api_errors import CodedHTTPException
from ..database import db
from ..job_queue import QueueBusy
from .auth import require_role

router = APIRouter(
    prefix='/datasets/{id}/schedules',
    tags=['schedules'],
    dependencies=[Depends(require_dataset_access)],
)
_RETURNING = dataset_schedules.COLUMNS.replace('s.', '')
DEFAULTS_WEEKDAY: int = 0  # Sunday: dataset_schedules.DEFAULTS['weekday'] (a test keeps them equal)


class ScheduleRule(BaseModel):
    # Defaults (dataset_schedules.DEFAULTS): weekly on Sunday at 03:00, only when >= 20,000 new samples.
    frequency: Literal['daily', 'weekly'] = 'weekly'
    weekday: Optional[StrictInt] = Field(None, ge=0, le=6)  # weekly only; 0 = Sunday (the default)
    time: str = Field('03:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    timezone: str = 'UTC'
    min_new_samples: Optional[StrictInt] = Field(20000, ge=1)  # None: no sample condition
    # None (the default): no minimum gap. With one, the next check goes straight to the first time the gap
    # since the last re-fit allows.
    min_days_between: Optional[StrictInt] = Field(None, ge=1)
    # A due run starts only this long after its time; later (runner down, waiting behind another
    # re-fit or a backup) it is skipped and recorded.
    window_minutes: StrictInt = Field(180, ge=1, le=7 * 24 * 60)
    enabled: StrictBool = False

    @model_validator(mode='after')
    def _consistent(self):
        if self.frequency == 'weekly' and self.weekday is None:
            self.weekday = DEFAULTS_WEEKDAY
        if (self.frequency == 'weekly') != (self.weekday is not None):
            raise ValueError('A weekly schedule needs a weekday (0 = Sunday); a daily one has none')
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f'Unknown time zone {self.timezone!r}')
        return self


class ScheduleCreate(ScheduleRule):
    job_type: Literal['map_refit', 'index_review'] = 'map_refit'
    # map_refit: the embedding field (required); index_review: always 'indexes'.
    target: Optional[str] = Field(None, min_length=1, max_length=200)


def _require_admin_for(job_type: str, user: dict) -> None:
    if job_type == index_advisor.KIND and user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='Only administrators can schedule index reviews')


def _review_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    """An index review has no conditions and no start window."""
    return {**rule, 'min_new_samples': None, 'min_days_between': None}


def _time(value: str) -> time:
    hour, minute = value.split(':')
    return time(int(hour), int(minute))


async def _serialize(connection, row) -> Dict[str, Any]:
    schedule = dict(row)
    review = schedule['job_type'] == index_advisor.KIND
    state = None if review else await map_refit.read_state(connection, schedule['dataset_id'], schedule['target'])
    points = 0 if review else await map_refit.current_points(connection, schedule['dataset_id'], schedule['target'])
    job = None
    if schedule['last_job_id'] is not None:
        job = await connection.fetchrow(
            """SELECT id, state, message, created_at, started_at, finished_at FROM jobs WHERE id=$1""",
            schedule['last_job_id'],
        )
    return {
        'id': schedule['id'],
        'dataset_id': schedule['dataset_id'],
        'job_type': schedule['job_type'],
        'target': schedule['target'],
        'enabled': schedule['enabled'],
        'frequency': schedule['frequency'],
        'weekday': schedule['weekday'],
        'time': schedule['time_of_day'].strftime('%H:%M'),
        'timezone': schedule['timezone'],
        'min_new_samples': schedule['min_new_samples'],
        'min_days_between': schedule['min_days_between'],
        'window_minutes': schedule['window_minutes'],
        'next_run_at': schedule['next_run_at'],
        'last_run_at': schedule['last_run_at'],
        'last_result': schedule['last_result'],
        'last_check': schedule['last_check'],
        'last_job': dict(job) if job else None,
        'map': None
        if review
        else {
            'model_id': str(state['model_id']) if state and state['model_id'] else None,
            'refitted_at': state['refitted_at'] if state else None,
            'points_at_refit': state['points_at_refit'] if state else 0,
            'current_points': points,
            'new_samples': max(0, points - (state['points_at_refit'] if state else 0)),
            'refit_running': bool(state and state['active']),
        },
    }


async def _target_exists(connection, dataset_id: int, field: str) -> bool:
    """A map field: one with map ranks or vectors waiting for projection (both small indexed probes)."""
    return bool(
        await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM embedding_viz_meta WHERE dataset_id=$1 AND field_name=$2)
                   OR EXISTS(SELECT 1 FROM embedding_projection_pending WHERE dataset_id=$1 AND field_name=$2)""",
            dataset_id,
            field,
        ),
    )


async def _schedule(connection, dataset_id: int, schedule_id: int, *, lock: bool = False):
    row = await connection.fetchrow(
        f"SELECT {dataset_schedules.COLUMNS} FROM dataset_job_schedules s WHERE s.id=$1 AND s.dataset_id=$2"
        + (' FOR UPDATE' if lock else ''),
        schedule_id,
        dataset_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail='Schedule not found')
    return dict(row)


async def _next_run(connection, dataset_id: int, rule: Dict[str, Any], now: datetime) -> Optional[datetime]:
    """The schedule's next check: its next time, or the first time the minimum gap since the map's last
    re-fit allows; none while it is off."""
    if not rule['enabled']:
        return None
    if rule.get('job_type') == index_advisor.KIND:
        return dataset_schedules.next_slot(rule, now)
    state = await map_refit.read_state(connection, dataset_id, rule['target'])
    return dataset_schedules.first_allowed_slot(rule, state['refitted_at'] if state else None, now)


@router.get('')
async def list_schedules(id: int):
    async with db.pool.acquire() as connection:
        rows = await connection.fetch(
            f'SELECT {dataset_schedules.COLUMNS} FROM dataset_job_schedules s WHERE s.dataset_id=$1 ORDER BY s.id',
            id,
        )
        return {
            'items': [await _serialize(connection, row) for row in rows],
            'defaults': dataset_schedules.DEFAULTS,
            'review_defaults': dataset_schedules.REVIEW_DEFAULTS,
        }


@router.post('', status_code=201)
async def create_schedule(id: int, body: ScheduleCreate, user: dict = Depends(require_role('power_user'))):
    _require_admin_for(body.job_type, user)
    review = body.job_type == index_advisor.KIND
    if review:
        body = body.model_copy(update=_review_rule({'target': dataset_schedules.REVIEW_TARGET}))
    elif not body.target:
        raise HTTPException(status_code=422, detail='A map re-fit schedule needs the embedding field (target)')
    target = cast('str', body.target)
    if not review and map_refit.is_patch_field(target):
        raise HTTPException(status_code=422, detail='Patch-scale fields are not re-fitted on a schedule')
    async with db.pool.acquire() as connection, connection.transaction():
        await connection.execute('SELECT require_writable_dataset($1)', id)
        if not review and not await _target_exists(connection, id, target):
            raise HTTPException(status_code=422, detail=f'No map or embeddings for field {target!r}')
        now = await connection.fetchval('SELECT clock_timestamp()')
        rule = {**body.model_dump(), 'time_of_day': _time(body.time)}
        row = await connection.fetchrow(
            f"""INSERT INTO dataset_job_schedules (dataset_id, job_type, target, enabled, frequency, weekday,
                    time_of_day, timezone, min_new_samples, min_days_between, next_run_at, created_by, window_minutes)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (dataset_id, job_type, target) DO NOTHING RETURNING {_RETURNING}""",
            id,
            body.job_type,
            body.target,
            body.enabled,
            body.frequency,
            body.weekday,
            rule['time_of_day'],
            body.timezone,
            body.min_new_samples,
            body.min_days_between,
            await _next_run(connection, id, {**rule, 'target': target}, now),
            user['id'],
            body.window_minutes,
        )
        if row is None:
            if review:
                raise HTTPException(
                    status_code=409,
                    detail='This dataset already has an index review schedule; edit that one',
                )
            raise HTTPException(status_code=409, detail=f'{target!r} already has a re-fit schedule; edit that one')
        return await _serialize(connection, row)


@router.put('/{schedule_id}')
async def update_schedule(
    id: int,
    schedule_id: int,
    body: ScheduleRule,
    user: dict = Depends(require_role('power_user')),
):
    async with db.pool.acquire() as connection, connection.transaction():
        await connection.execute('SELECT require_writable_dataset($1)', id)
        current = await _schedule(connection, id, schedule_id, lock=True)
        _require_admin_for(current['job_type'], user)
        if current['job_type'] == index_advisor.KIND:
            body = body.model_copy(update=_review_rule({}))
        now = await connection.fetchval('SELECT clock_timestamp()')
        rule = {**body.model_dump(), 'time_of_day': _time(body.time), 'job_type': current['job_type']}
        timing = ('enabled', 'frequency', 'weekday', 'time_of_day', 'timezone', 'min_days_between')
        # A due run that waits behind a fit or backup keeps its place unless the timing changed.
        changed = any(rule[name] != current[name] for name in timing)
        row = await connection.fetchrow(
            f"""UPDATE dataset_job_schedules SET enabled=$3, frequency=$4, weekday=$5, time_of_day=$6,
                    timezone=$7, min_new_samples=$8, min_days_between=$9, next_run_at=$10,
                    window_minutes=$11, updated_at=clock_timestamp()
                WHERE id=$1 AND dataset_id=$2 RETURNING {_RETURNING}""",
            schedule_id,
            id,
            body.enabled,
            body.frequency,
            body.weekday,
            rule['time_of_day'],
            body.timezone,
            body.min_new_samples,
            body.min_days_between,
            await _next_run(connection, id, {**rule, 'target': current['target']}, now)
            if changed
            else current['next_run_at'],
            body.window_minutes,
        )
        return await _serialize(connection, row)


@router.delete('/{schedule_id}', status_code=204)
async def delete_schedule(id: int, schedule_id: int, user: dict = Depends(require_role('power_user'))):
    async with db.pool.acquire() as connection, connection.transaction():
        await connection.execute('SELECT require_writable_dataset($1)', id)
        current = await _schedule(connection, id, schedule_id, lock=True)
        _require_admin_for(current['job_type'], user)
        await connection.execute('DELETE FROM dataset_job_schedules WHERE id=$1', schedule_id)


@router.post('/{schedule_id}/run', status_code=202)
async def run_schedule(id: int, schedule_id: int, user: dict = Depends(require_role('power_user'))):
    """Queue the job now, ignoring the sample and frequency conditions (or return the one already
    queued or running for this map)."""
    async with db.pool.acquire() as connection:
        schedule = await _schedule(connection, id, schedule_id)
    _require_admin_for(schedule['job_type'], user)
    try:
        job, created = await dataset_schedules.run_now(db.pool, schedule, user['id'])
    except map_refit.MapAdmissionBusy as error:
        raise CodedHTTPException(
            status_code=409,
            detail=str(error),
            code='map_admission_busy',
            headers={'Retry-After': '1'},
        )
    except QueueBusy:
        raise CodedHTTPException(
            status_code=409,
            detail='The job queue is busy; retry',
            code='job_queue_busy',
            headers={'Retry-After': '1'},
        )
    return {'job_id': str(job['id']), 'state': job['state'], 'created': created}
