"""Recurring jobs per dataset (Jobs panel → Schedules); the first job type is the map re-fit.

A schedule has a time rule (daily, or weekly on one day, at a wall-clock time in its time zone),
conditions, and a start window. `schedule_dataset_jobs` runs in the job runner every minute: for each
enabled schedule whose `next_run_at` has passed it checks the conditions on small per-map rows (no
embedding scans), queues the job when they hold, records why when they don't, and moves `next_run_at`
to the next time. A minimum gap between re-fits (`min_days_between`) is not checked night by night:
`next_run_at` goes straight to the first time the gap allows (after each re-fit, and whenever a check
finds the gap not yet over), so "Next check" shows that date and no skip is recorded for the nights in
between. The sample condition is checked at each time. Queued runs wait in line at claim time (one fit
at a time: the exclusive job resource) and their first step waits out a backup; a run that cannot start
inside its window (by `window_minutes` after its time) is skipped and recorded, so a re-fit never starts
in working hours because the runner was down or it waited behind another fit. "Run now" queues the job
without conditions or window.

The second job type is the read-only **index review** (backend/index_advisor.py), target 'indexes':
no conditions and no start window (a review the runner missed simply runs at the next pass). The
system-wide review has the same rule in its one `system_job_schedules` row, handled by the same pass.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from . import backups, index_advisor, job_queue, map_refit

logger = logging.getLogger(__name__)

JOB_TYPES = (map_refit.KIND, index_advisor.KIND)
REVIEW_TARGET = 'indexes'
# A new re-fit schedule (default policy): weekly on Sunday at 03:00, only when there are at least
# 20,000 new samples; no minimum gap between re-fits (available in the form).
DEFAULTS = {
    'enabled': False,
    'frequency': 'weekly',
    'weekday': 0,
    'time': '03:00',
    'timezone': 'UTC',
    'min_new_samples': 20000,
    'min_days_between': None,
    'window_minutes': 180,
}


def review_default_slot(refit: Dict[str, Any]) -> Tuple[int, str]:
    """(weekday, 'HH:MM') of a new index review schedule: when the default re-fit's start window closes
    (its time + window_minutes, carried into the next day past midnight). The window bounds when a re-fit
    may *start*, not how long it runs, so a re-fit can still be running then; the review is read-only and
    its 14-day idle window makes the exact slot irrelevant to its verdicts. This only keeps the two
    defaults apart."""
    start = datetime.combine(date(2026, 1, 4), time.fromisoformat(str(refit['time'])))  # 2026-01-04 is a Sunday
    slot = start + timedelta(minutes=int(str(refit['window_minutes'])))
    return (int(str(refit['weekday'])) + (slot.date() - start.date()).days) % 7, slot.strftime('%H:%M')


_REVIEW_WEEKDAY, _REVIEW_TIME = review_default_slot(DEFAULTS)
# A new index review schedule: no conditions; switched on by an administrator.
REVIEW_DEFAULTS = {**DEFAULTS, 'min_new_samples': None, 'weekday': _REVIEW_WEEKDAY, 'time': _REVIEW_TIME}
SCHEDULER_LOCK = 'SELECT pg_try_advisory_xact_lock(20261064, 0)'
SYSTEM_COLUMNS = """job_type, enabled, frequency, weekday, time_of_day, timezone, next_run_at, last_run_at, last_job_id,
    last_check, updated_at"""
COLUMNS = """s.id, s.dataset_id, s.job_type, s.target, s.enabled, s.frequency, s.weekday, s.time_of_day, s.timezone,
    s.min_new_samples, s.min_days_between, s.window_minutes, s.next_run_at, s.last_run_at, s.last_job_id, s.last_result,
    s.last_check, s.created_at, s.updated_at"""


def _weekday(day: date) -> int:
    """0 = Sunday, as stored."""
    return (day.weekday() + 1) % 7


def next_slot(rule: Dict[str, Any], after: datetime) -> datetime:
    """The first time strictly after `after` that the rule names, in UTC. Wall-clock times: a daily
    03:00 stays at 03:00 local across daylight-saving changes. A time the spring-forward skips runs an
    hour later that day; a time the fall-back repeats runs at its first occurrence."""
    zone = ZoneInfo(rule['timezone'])
    day = after.astimezone(zone).date()
    for offset in range(8):
        candidate_day = day + timedelta(days=offset)
        if rule['frequency'] == 'weekly' and _weekday(candidate_day) != rule['weekday']:
            continue
        candidate = datetime.combine(candidate_day, rule['time_of_day'], tzinfo=zone).astimezone(timezone.utc)
        if candidate > after:
            return candidate
    raise ValueError(f'No time matches the rule {rule!r}')


def gap_ends(rule: Dict[str, Any], refitted_at: Optional[datetime]) -> Optional[date]:
    """The first calendar day (in the schedule's time zone) a re-fit may run again, or None."""
    if not rule.get('min_days_between') or refitted_at is None:
        return None
    zone = ZoneInfo(rule['timezone'])
    return refitted_at.astimezone(zone).date() + timedelta(days=rule['min_days_between'])


def first_allowed_slot(rule: Dict[str, Any], refitted_at: Optional[datetime], after: datetime) -> datetime:
    """The first time after `after` that the rule names, on or after the day the minimum gap since the
    last re-fit ends (days counted in the schedule's time zone, as `conditions_met` counts them)."""
    first_day = gap_ends(rule, refitted_at)
    if first_day is not None:
        midnight = datetime.combine(first_day, time(0), tzinfo=ZoneInfo(rule['timezone'])).astimezone(timezone.utc)
        after = max(after, midnight - timedelta(microseconds=1))
    return next_slot(rule, after)


def conditions_met(
    schedule: Dict[str, Any],
    state: Optional[Dict[str, Any]],
    current_points: int,
    now: datetime,
) -> Tuple[bool, str]:
    """Whether a due map re-fit should run: at least `min_days_between` calendar days (in the schedule's
    time zone) since the last re-fit, and at least `min_new_samples` more points on the map since then.
    The point count is the exact per-map counter (embedding_viz_meta.total_points); a field never
    re-fitted counts from zero."""
    refitted_at = state['refitted_at'] if state else None
    baseline = state['points_at_refit'] if state else 0
    new_samples = max(0, current_points - baseline)
    if schedule['min_days_between'] and refitted_at is not None:
        zone = ZoneInfo(schedule['timezone'])
        days = (now.astimezone(zone).date() - refitted_at.astimezone(zone).date()).days
        if days < schedule['min_days_between']:
            return False, (
                f'Re-fitted {days} day{"s" if days != 1 else ""} ago; '
                f'runs at most every {schedule["min_days_between"]} days'
            )
    if schedule['min_new_samples'] and new_samples < schedule['min_new_samples']:
        return False, f'{new_samples:,} new samples since the last re-fit; needs {schedule["min_new_samples"]:,}'
    return True, f'{new_samples:,} new samples since the last re-fit'


def _local(moment: datetime, zone: str) -> str:
    return moment.astimezone(ZoneInfo(zone)).strftime('%b %-d %H:%M')


async def waiting_for(connection, dataset_id: int) -> Optional[str]:
    """What a newly queued re-fit of this dataset will wait for, if anything (shown on the schedule)."""
    row = await connection.fetchrow(
        """SELECT kind, dataset_id, args->>'field' AS field FROM jobs
           WHERE (kind = $1 AND state IN ('queued','running')) OR (kind = $2 AND state = 'running'
                  AND dataset_id <> $3 AND resources ? 'exclusive')
           ORDER BY kind = $1 DESC LIMIT 1""",
        backups.KIND,
        map_refit.KIND,
        dataset_id,
    )
    if row is None:
        return None
    if row['kind'] == backups.KIND:
        return 'it starts after the database backup'
    return f"one map re-fit runs at a time: it starts after dataset {row['dataset_id']}'s ({row['field']})"


async def schedule_dataset_jobs(pool, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Queue every due run whose conditions hold; returns the queued (or joined) jobs."""
    async with job_queue._transaction(pool) as connection:
        if not await connection.fetchval(SCHEDULER_LOCK):
            return []
        now = now or await connection.fetchval('SELECT clock_timestamp()')
        due = await connection.fetch(
            f"""SELECT {COLUMNS} FROM dataset_job_schedules s JOIN datasets d ON d.id = s.dataset_id
                WHERE s.enabled AND s.next_run_at <= $1 AND NOT d.deleting
                ORDER BY s.next_run_at, s.id LIMIT 32 FOR UPDATE OF s SKIP LOCKED""",
            now,
        )
        queued = []
        for schedule in map(dict, due):
            if schedule['job_type'] == index_advisor.KIND:
                job = await _queue_review(connection, 'dataset_job_schedules', schedule, now)
                if job is not None:
                    queued.append(job)
                continue
            try:
                async with connection.transaction():
                    job = await _queue_refit(connection, schedule, now)
                if job is not None:
                    queued.append(job)
            except Exception as error:
                await _record_schedule_failure(connection, 'dataset_job_schedules', schedule, now, error)
        system = await connection.fetch(
            f"""SELECT {SYSTEM_COLUMNS} FROM system_job_schedules WHERE enabled AND next_run_at <= $1
                ORDER BY job_type FOR UPDATE SKIP LOCKED""",
            now,
        )
        for schedule in map(dict, system):
            job = await _queue_review(connection, 'system_job_schedules', schedule, now)
            if job is not None:
                queued.append(job)
        return queued


async def _queue_refit(connection, schedule, now):
    """One schedule's savepoint covers admission and its bookkeeping; contention leaves it due."""
    state = await map_refit.read_state(connection, schedule['dataset_id'], schedule['target'])
    refitted_at = state['refitted_at'] if state else None
    following = first_allowed_slot(schedule, refitted_at, now)
    first_day = gap_ends(schedule, refitted_at)
    if first_day is not None and now.astimezone(ZoneInfo(schedule['timezone'])).date() < first_day:
        # The minimum gap is not over: go straight to the first time it allows (nothing to record).
        await connection.execute(
            'UPDATE dataset_job_schedules SET next_run_at=$2 WHERE id=$1',
            schedule['id'],
            following,
        )
        return None
    window_end = schedule['next_run_at'] + timedelta(minutes=schedule['window_minutes'])
    if now >= window_end:
        run, reason = (
            False,
            (
                f"Missed {_local(schedule['next_run_at'], schedule['timezone'])}: the job could not be queued "
                f"before {_local(window_end, schedule['timezone'])}, the end of its start window"
            ),
        )
        previous = schedule['last_check'] or {}
        if previous.get('retrying_for') == schedule['next_run_at'].isoformat():
            reason += f". Last attempt: {previous['reason']}"
    else:
        points = await map_refit.current_points(connection, schedule['dataset_id'], schedule['target'])
        run, reason = conditions_met(schedule, state, points, now)
    if not run:
        await connection.execute(
            'UPDATE dataset_job_schedules SET next_run_at=$2, last_check=$3 WHERE id=$1',
            schedule['id'],
            following,
            {'at': now.isoformat(), 'ran': False, 'reason': reason},
        )
        return None
    wait = await waiting_for(connection, schedule['dataset_id'])
    if wait:
        end = _local(window_end, schedule['timezone'])
        reason = f'{reason}; queued, {wait} (it is skipped if that is after {end})'
    job, _ = await map_refit.submit(
        connection,
        schedule['dataset_id'],
        schedule['target'],
        schedule=schedule,
        window_end=window_end,
        wait=False,
    )
    await connection.execute(
        """UPDATE dataset_job_schedules SET next_run_at=$2, last_check=$3, last_run_at=$4,
           last_job_id=$5, last_result=NULL WHERE id=$1""",
        schedule['id'],
        following,
        {'at': now.isoformat(), 'ran': True, 'reason': reason},
        now,
        job['id'],
    )
    return job


async def _queue_review(connection, table: str, schedule: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """Queue (or join) the scheduled index review and move the schedule to its next time. In its own
    savepoint: a review that cannot be queued now (a busy lock, a dataset sealed for deletion meanwhile)
    stays due for the next pass and never aborts the rest of this one (map re-fits included)."""
    dataset_id = schedule.get('dataset_id')
    key, value = ('id', schedule['id']) if table == 'dataset_job_schedules' else ('job_type', schedule['job_type'])
    try:
        async with connection.transaction():
            job, created = await index_advisor.submit(connection, dataset_id, None)
            reason = 'Index review queued' if created else 'An index review was already queued or running; joined it'
            await connection.execute(
                f"""UPDATE {table} SET next_run_at=$2, last_check=$3, last_run_at=$4, last_job_id=$5 WHERE {key}=$1""",
                value,
                next_slot(schedule, now),
                {'at': now.isoformat(), 'ran': True, 'reason': reason},
                now,
                job['id'],
            )
            return job
    except Exception as error:  # one review must not stop the pass; it is retried next minute
        await _record_schedule_failure(connection, table, schedule, now, error)
        return None


async def _record_schedule_failure(connection, table, schedule, now, error):
    """Persist the reason outside the failed savepoint; log once until this due run is resolved.

    Using last_check survives runner restarts and resets naturally when a run is queued or skipped.
    The due time also distinguishes a new streak after editing or postponing a schedule.
    """
    key, value = ('id', schedule['id']) if table == 'dataset_job_schedules' else ('job_type', schedule['job_type'])
    due = schedule['next_run_at'].isoformat()
    if (schedule['last_check'] or {}).get('retrying_for') != due:
        logger.warning('Scheduled job (%s %s) stays due: %s', table, value, error, exc_info=True)
    await connection.execute(
        f'UPDATE {table} SET last_check=$2 WHERE {key}=$1',
        value,
        {'at': now.isoformat(), 'ran': False, 'retrying_for': due, 'reason': f'Could not queue the job: {error}'},
    )


async def read_system_schedule(connection, job_type: str = index_advisor.KIND) -> Optional[Dict[str, Any]]:
    row = await connection.fetchrow(f'SELECT {SYSTEM_COLUMNS} FROM system_job_schedules WHERE job_type=$1', job_type)
    return dict(row) if row is not None else None


async def after_refit(connection, dataset_id: int, field: str, refitted_at: datetime) -> None:
    """A re-fit of this map was published: an enabled schedule with a minimum gap next checks on the
    first time the gap allows (in the publishing transaction)."""
    rows = await connection.fetch(
        """SELECT id, frequency, weekday, time_of_day, timezone, min_days_between, next_run_at
           FROM dataset_job_schedules WHERE dataset_id=$1 AND job_type=$2 AND target=$3 AND enabled
           AND min_days_between IS NOT NULL FOR UPDATE""",
        dataset_id,
        map_refit.KIND,
        field,
    )
    for row in map(dict, rows):
        first = first_allowed_slot(row, refitted_at, refitted_at)
        if row['next_run_at'] is None or row['next_run_at'] < first:
            await connection.execute('UPDATE dataset_job_schedules SET next_run_at=$2 WHERE id=$1', row['id'], first)


async def run_now(pool, schedule: Dict[str, Any], user_id: Optional[int]) -> Tuple[Dict[str, Any], bool]:
    """Queue the schedule's job now, ignoring its conditions, or return the one already queued/running."""
    async with job_queue._transaction(pool) as connection:
        if schedule['job_type'] == index_advisor.KIND:
            job, created = await index_advisor.submit(connection, schedule['dataset_id'], user_id)
        else:
            job, created = await map_refit.submit(
                connection,
                schedule['dataset_id'],
                schedule['target'],
                schedule=schedule,
                created_by=user_id,
                wait=False,
            )
        if created:
            await connection.execute(
                """UPDATE dataset_job_schedules SET last_run_at=clock_timestamp(), last_job_id=$2, last_result=NULL
                   WHERE id=$1""",
                schedule['id'],
                job['id'],
            )
        return job, created
