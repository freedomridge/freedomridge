"""Scheduled whole-database backups on the job runner (Settings → System status → Backups).

A backup job runs scripts/backup_local.py, the script cron uses, into FRIDGE_BACKUP_DIR: it dumps the
database, reads the dump back, keeps it only if every table's row count matches, and writes a manifest
(naming the job) or a failure record beside it. This module decides when a backup is due, runs the
script as a job, and stops it cleanly when the job is cancelled or the runner shuts down.
"""

import asyncio
import os
import re
import signal
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import job_queue
from .job_queue import JobPolicy
from .job_runner import StepResult

KIND = 'backup'
# The folder backup_local.py writes to. Read only; never created, so an unmounted drive fails the job.
BACKUP_DIR = os.getenv('FRIDGE_BACKUP_DIR') or None
# A dump of a large database takes tens of minutes. A failure (a full disk, an unmounted drive)
# shows on the card and waits for the next slot or "Back up now". The second attempt is only for a backup
# a restart interrupted (and, as a side effect, a 12-hour timeout or a lost lease).
POLICY = JobPolicy(max_unit_seconds=12 * 3600, max_failures=2)
SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'backup_local.py'
# backup_local.py handles SIGTERM itself: it ends its pg_dump in the container, deletes the partial dump
# and writes a failure record, in a few seconds. This fits inside the runner's 20 s shutdown grace.
STOP_GRACE_SECONDS = 8
# What backup_local.py records when a signal stopped it. The job's own stop never reads the record, so
# this one came from outside: a backend restart signals the runner's whole process group.
_STOPPED_BY_RESTART = 'BackupError: stopped by SIGTERM'
# backup_local.py's log() starts each line with "2026-10-20 02:00:01 +0000 ".
_LOG_STAMP = re.compile(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4} ')
_SCHEDULE_LOCK = "SELECT pg_try_advisory_xact_lock(hashtextextended('schedule:backup',0))"
# Before this anchor day, slots don't matter; after it, they fall on start_hour every interval_hours.
_ANCHOR_DAY = datetime(2026, 1, 1)


def last_slot(
    now: datetime,
    interval_hours: int,
    start_hour: int,
    tz: Optional[tzinfo] = None,
    *,
    later: int = 0,
) -> datetime:
    """The latest scheduled time at or before `now`, or the `later`th slot after it. Counted in wall-clock
    time (server local time unless `tz` is given), so a daily 02:00 backup stays at 02:00 across
    daylight-saving changes."""
    wall = now.astimezone(tz).replace(tzinfo=None)
    anchor = _ANCHOR_DAY.replace(hour=start_hour)
    step = timedelta(hours=interval_hours)
    slot = anchor + ((wall - anchor) // step + later) * step
    return slot.replace(tzinfo=tz) if tz is not None else slot.astimezone()


def next_run(schedule: Dict[str, Any], now: datetime, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """When the scheduler next starts a backup, or None when backups aren't scheduled."""
    if schedule.get('interval_hours') is None:
        return None
    since = max(now, schedule['timing_changed_at'])
    return last_slot(since, schedule['interval_hours'], schedule['start_hour'], tz, later=1)


def overdue_after(schedule: Optional[Dict[str, Any]]) -> Optional[timedelta]:
    """How old the newest verified backup may get before the card says overdue: 1.5 intervals. Never,
    when backups aren't scheduled (a one-off backup of a dataset that doesn't change stays fine)."""
    if not schedule or schedule.get('interval_hours') is None:
        return None
    return timedelta(hours=1.5 * schedule['interval_hours'])


async def read_schedule(connection) -> Optional[Dict[str, Any]]:
    row = await connection.fetchrow('SELECT interval_hours, start_hour, keep, timing_changed_at FROM backup_schedule')
    return dict(row) if row else None


async def active_backup(connection) -> Optional[Dict[str, Any]]:
    row = await connection.fetchrow(
        f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
            WHERE kind=$1 AND state IN ('queued','running') ORDER BY created_at LIMIT 1""",
        KIND,
    )
    return dict(row) if row else None


async def schedule_backups(pool, folder: Optional[str] = None, now: Optional[datetime] = None):
    """Queue one backup per scheduled slot. A schedule whose timing changed after a slot starts at the next
    one, and a runner that was down at a slot catches up once when it comes back."""
    folder = folder if folder is not None else BACKUP_DIR
    if not folder:
        return None
    async with job_queue._transaction(pool) as connection:
        if not await connection.fetchval(_SCHEDULE_LOCK):
            return None
        schedule = await read_schedule(connection)
        if schedule is None or schedule['interval_hours'] is None:
            return None
        # The database's clock, which also stamps the jobs' created_at below.
        now = now or await connection.fetchval('SELECT clock_timestamp()')
        slot = last_slot(now, schedule['interval_hours'], schedule['start_hour'])
        if slot <= schedule['timing_changed_at']:
            return None
        # A map re-fit swapping its new map in and re-ranking it writes millions of rows for a few
        # minutes: start the backup after it (the re-fit's own steps wait for a backup already running).
        if await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM jobs WHERE kind='map_refit' AND state='running'
                             AND checkpoint->>'phase' IN ('publish','rank'))""",
        ):
            return None
        latest = await connection.fetchrow(
            'SELECT state, created_at, finished_at FROM jobs WHERE kind=$1 ORDER BY created_at DESC LIMIT 1',
            KIND,
        )
        if latest is not None and (
            latest['state'] in ('queued', 'running')
            or latest['created_at'] >= slot
            # A backup that was still running at the slot (a "Back up now" just before it) covers it.
            or (latest['state'] == 'succeeded' and latest['finished_at'] >= slot)
        ):
            return None
        return await job_queue.enqueue_job(
            connection,
            KIND,
            lane='maintenance',
            args={'keep': schedule['keep']},
            dedupe_key='scheduled:backup',
        )


async def run_now(pool, user_id: Optional[int]) -> Tuple[Dict[str, Any], bool]:
    """Start a backup for an admin, or return the one already queued or running (only one runs at a
    time: backup_local.py's lock would fail a second)."""
    async with job_queue._transaction(pool) as connection:
        await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('schedule:backup',0))")
        active = await active_backup(connection)
        if active is not None:
            return active, False
        schedule = await read_schedule(connection)
        job = await job_queue.enqueue_job(
            connection,
            KIND,
            lane='maintenance',
            args={'keep': schedule['keep'] if schedule else 7},
            created_by=user_id,
        )
        return job, True


def _last_line(path: Path, offset: int) -> Optional[str]:
    """The last line written to `path` after `offset`, without its timestamp."""
    try:
        with open(path, 'rb') as log:
            log.seek(offset)
            lines = log.read().decode(errors='replace').strip().splitlines()
    except OSError:
        return None
    return _LOG_STAMP.sub('', lines[-1]) if lines else None


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), STOP_GRACE_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):
            process.kill()
            await process.wait()


def handler(folder: Optional[str] = None):
    folder = folder if folder is not None else BACKUP_DIR

    async def measure(job):
        if not folder:
            raise RuntimeError('FRIDGE_BACKUP_DIR is not set for the job runner')
        if not Path(folder).is_dir():
            raise RuntimeError(f'{folder} does not exist (is the backup drive mounted?)')
        from .system_status import job_dump, read_backup_folder  # system_status imports this module

        if job['attempt'] > 1:
            # An earlier attempt may have made its dump before it was cut off. A dump holds its own backup job
            # mid-run, so restoring it (into this cluster too, where restore recovery doesn't see it) resumes
            # that job. Only the dump whose manifest names this job counts, not a newer one of another history.
            made = await asyncio.to_thread(job_dump, folder, str(job['id']))
            if made:
                return StepResult(finished=True, result=made, message='Backup verified; an earlier attempt made it')
        keep = int((job['args'] or {}).get('keep', 7))
        started = datetime.now(timezone.utc).replace(microsecond=0)
        # The same log the cron entry appended to, so "see backup.log" keeps working.
        log_path = Path(folder) / 'backup.log'
        with open(log_path, 'ab') as log:
            offset = log.tell()
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(SCRIPT),
                '--dir',
                folder,
                '--keep',
                str(keep),
                '--job-id',
                str(job['id']),
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                code = await process.wait()
            except BaseException:  # cancelled, timed out, lease lost or runner shutdown
                await asyncio.shield(_stop(process))
                raise
        status = await asyncio.to_thread(read_backup_folder, folder)
        latest = status.get('latest')
        if code == 0 and latest:
            return StepResult(
                finished=True,
                result={'file': latest['file'], 'size_bytes': latest['size_bytes'], 'tables': latest['tables']},
                message='Backup verified',
            )
        failure = status.get('last_failure')
        if failure and datetime.fromisoformat(failure['failed_at']) >= started:
            if failure['error'] == _STOPPED_BY_RESTART:
                raise ConnectionError('Interrupted by a backend restart; it runs again when the runner is back')
            raise RuntimeError(failure['error'])
        # No record: another backup held the lock, or the folder went away. The script's last line says which.
        last_line = _last_line(log_path, offset)
        reason = last_line or f'see backup.log in {folder}'
        raise RuntimeError(f'backup_local.py exited {code}: {reason}')

    return measure
