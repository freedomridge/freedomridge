"""Scheduled backups on the job runner: when one is due, "Back up now", and the job that runs
scripts/backup_local.py (Settings → System status → Backups)."""

import asyncio
import json
import os
import signal
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from backend import backups, job_queue, job_service, schema_check, statistics, system_status
from backend.job_runner import StepResult
from backend.main import app
from backend.routers import backup_settings
from backend.routers.auth import get_current_user
from backend.tests import test_backup_local, test_field_registry_postgres, test_system_status
from backend.tests.test_job_runner import make_worker

registry_database = test_field_registry_postgres.registry_database
scratch_databases = test_backup_local.scratch_databases
requires_container = test_backup_local.requires_container
requires_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
CHICAGO = ZoneInfo('America/Chicago')
ADMIN_ID = 1
ADMIN = {'id': ADMIN_ID, 'username': 'admin', 'role': 'admin'}


def local(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CHICAGO)


@pytest.mark.parametrize(
    'now, interval, start, slot',
    [
        (local(2026, 10, 20, 1, 59), 24, 2, local(2026, 10, 19, 2)),
        (local(2026, 10, 20, 2, 0), 24, 2, local(2026, 10, 20, 2)),
        (local(2026, 10, 20, 13, 30), 6, 2, local(2026, 10, 20, 8)),
        (local(2026, 10, 20, 10), 48, 2, local(2026, 10, 20, 2)),  # 292 days after the anchor: even
        (local(2026, 10, 21, 10), 48, 2, local(2026, 10, 20, 2)),
        (local(2026, 10, 25, 1), 168, 2, local(2026, 10, 22, 2)),
        # Across the 2026-11-01 fall-back a daily 02:00 stays at 02:00 on the wall clock.
        (local(2026, 11, 2, 3), 24, 2, local(2026, 11, 2, 2)),
    ],
)
def test_slots_fall_on_the_start_hour_every_interval_in_wall_clock_time(now, interval, start, slot):
    found = backups.last_slot(now, interval, start, CHICAGO)
    assert found == slot
    assert backups.last_slot(now.astimezone(timezone.utc), interval, start, CHICAGO) == slot


def test_the_next_run_is_the_first_slot_after_the_timing_changed():
    schedule = {'interval_hours': 24, 'start_hour': 2, 'keep': 7, 'timing_changed_at': local(2026, 10, 20, 3)}
    assert backups.next_run(schedule, local(2026, 10, 20, 3, 5), CHICAGO) == local(2026, 10, 21, 2)
    schedule['timing_changed_at'] = local(2026, 10, 19, 23)
    assert backups.next_run(schedule, local(2026, 10, 20, 1), CHICAGO) == local(2026, 10, 20, 2)
    assert backups.next_run({**schedule, 'interval_hours': None}, local(2026, 10, 20, 1), CHICAGO) is None


@pytest.fixture
def server_in_chicago(monkeypatch):
    """The production path: no tz argument, the process's local time zone."""
    monkeypatch.setenv('TZ', 'America/Chicago')
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize(
    'asked, expected',
    [
        (local(2026, 10, 31, 12), local(2026, 11, 1, 2)),  # the day before falling back
        (local(2027, 3, 13, 12), local(2027, 3, 14, 3)),  # 02:00 doesn't exist on 2027-03-14
        (local(2027, 3, 14, 12), local(2027, 3, 15, 2)),  # the day after springing forward
    ],
)
def test_the_card_promises_the_slot_the_scheduler_will_use_across_clock_changes(server_in_chicago, asked, expected):
    schedule = {'interval_hours': 24, 'start_hour': 2, 'keep': 7, 'timing_changed_at': asked - timedelta(days=9)}
    promised = backups.next_run(schedule, asked)
    assert promised == expected
    assert backups.last_slot(promised, 24, 2) == promised  # the scheduler queues it then
    assert backups.last_slot(promised - timedelta(minutes=1), 24, 2) < asked


def test_overdue_follows_the_schedule_and_never_without_one():
    assert backups.overdue_after({'interval_hours': 24}) == timedelta(hours=36)
    assert backups.overdue_after({'interval_hours': 6}) == timedelta(hours=9)
    assert backups.overdue_after({'interval_hours': None}) is None
    assert backups.overdue_after(None) is None


async def _set_schedule(database, interval_hours, changed_hours_ago, keep=7):
    await database.execute(
        "UPDATE backup_schedule SET interval_hours=$1, start_hour=0, keep=$2, "
        "timing_changed_at=clock_timestamp() - $3::float8 * interval '1 hour'",
        interval_hours,
        keep,
        changed_hours_ago,
    )


async def _backup_jobs(database):
    return await database.fetch("SELECT * FROM jobs WHERE kind='backup' ORDER BY created_at")


@requires_database
def test_one_backup_is_queued_per_slot_and_only_when_scheduled(registry_database, tmp_path):
    async def scenario(database):
        folder = str(tmp_path)
        assert await backups.schedule_backups(database.pool, folder) is None  # off by default
        await _set_schedule(database, 1, 3, keep=4)
        assert await backups.schedule_backups(database.pool, '') is None  # no folder: nothing to back up into
        job = await backups.schedule_backups(database.pool, folder)
        assert (job['kind'], job['lane'], job['args'], job['created_by']) == (
            'backup',
            'maintenance',
            {'keep': 4},
            None,
        )
        assert await backups.schedule_backups(database.pool, folder) is None  # one is already queued
        await database.execute("UPDATE jobs SET state='failed', finished_at=clock_timestamp()")
        assert await backups.schedule_backups(database.pool, folder) is None  # a failed slot is not retried
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        assert (await backups.schedule_backups(database.pool, folder, later))['id'] != job['id']  # next slot
        assert len(await _backup_jobs(database)) == 2

    registry_database(scenario)


@requires_database
def test_a_schedule_saved_after_the_last_slot_waits_for_the_next(registry_database, tmp_path):
    async def scenario(database):
        await _set_schedule(database, 24, 0)  # daily at midnight, saved just now
        assert await backups.schedule_backups(database.pool, str(tmp_path)) is None
        tomorrow = datetime.now(timezone.utc) + timedelta(hours=24)
        assert await backups.schedule_backups(database.pool, str(tmp_path), tomorrow) is not None

    registry_database(scenario)


@requires_database
def test_a_backup_still_running_at_a_slot_covers_it(registry_database, tmp_path):
    async def scenario(database):
        folder = str(tmp_path)
        await _set_schedule(database, 24, 48)  # daily at midnight, set two days ago
        slot = backups.last_slot(datetime.now(timezone.utc), 24, 0)
        job, _ = await backups.run_now(database.pool, ADMIN_ID)  # "Back up now" an hour before the slot
        await database.execute("UPDATE jobs SET created_at=$1::timestamptz - interval '1 hour'", slot)
        assert await backups.schedule_backups(database.pool, folder) is None  # still queued
        await database.execute(
            "UPDATE jobs SET state='succeeded', finished_at=$1::timestamptz + interval '10 minutes'",
            slot,
        )
        assert await backups.schedule_backups(database.pool, folder) is None  # it finished after the slot
        await database.execute("UPDATE jobs SET state='failed'")
        assert (await backups.schedule_backups(database.pool, folder))['id'] != job['id']  # a failure doesn't

    registry_database(scenario)


@requires_database
def test_the_card_shows_the_schedule_and_the_backup_in_progress(registry_database, tmp_path):
    async def scenario(database):
        await _set_schedule(database, 24, 48, keep=3)
        job, _ = await backups.run_now(database.pool, ADMIN_ID)
        status = await system_status.backups(database, str(tmp_path))
        assert status['level'] == 'none' and status['overdue_after_hours'] == 36
        assert status['schedule']['keep'] == 3 and status['schedule']['next_run_at'] is not None
        assert status['active_job']['id'] == job['id'] and status['active_job']['state'] == 'queued'
        await database.execute("UPDATE backup_schedule SET interval_hours=NULL")
        await database.execute("UPDATE jobs SET state='succeeded', finished_at=clock_timestamp()")
        status = await system_status.backups(database, str(tmp_path))
        assert status['schedule']['next_run_at'] is None and status['active_job'] is None
        assert status['overdue_after_hours'] is None

    registry_database(scenario)


@requires_database
def test_back_up_now_starts_one_backup_and_returns_it_while_it_runs(registry_database, monkeypatch):
    async def scenario(database):
        await _set_schedule(database, None, 0, keep=3)
        first, started = await backups.run_now(database.pool, ADMIN_ID)
        assert started and first['created_by'] == ADMIN_ID and first['args'] == {'keep': 3}
        again, started = await backups.run_now(database.pool, ADMIN_ID)
        assert not started and again['id'] == first['id']
        await database.execute("UPDATE jobs SET state='succeeded', finished_at=clock_timestamp()")
        assert (await backups.run_now(database.pool, ADMIN_ID))[1] is True

        monkeypatch.setattr(backup_settings, 'db', database)
        monkeypatch.setattr(backups, 'BACKUP_DIR', None)
        with pytest.raises(Exception) as refused:
            await backup_settings.run_backup(ADMIN)
        assert getattr(refused.value, 'status_code', None) == 409

    registry_database(scenario)


@requires_database
def test_the_schedule_endpoint_saves_who_changed_it_and_when_the_timing_changed(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(backup_settings, 'db', database)

        async def save(interval_hours, start_hour, keep):
            body = backup_settings.BackupSchedule(interval_hours=interval_hours, start_hour=start_hour, keep=keep)
            return await backup_settings.put_schedule(body, ADMIN)

        await _set_schedule(database, None, 5)
        saved = await save(12, 3, 5)
        assert (saved['interval_hours'], saved['start_hour'], saved['keep']) == (12, 3, 5)
        assert await database.fetchval('SELECT updated_by FROM backup_schedule') == 1
        changed = saved['timing_changed_at']
        assert changed > datetime.now(timezone.utc) - timedelta(minutes=1)  # turning it on moves it
        assert (await save(12, 3, 6))['timing_changed_at'] == changed  # keep alone doesn't
        assert (await save(12, 4, 6))['timing_changed_at'] > changed
        off = await save(None, 4, 6)
        assert off['interval_hours'] is None and dict(await backup_settings.get_schedule(ADMIN)) == dict(off)

    registry_database(scenario)


DAILY = {'interval_hours': 24, 'start_hour': 2, 'keep': 7}


@pytest.mark.parametrize(
    'role, body, code',
    [
        ('power_user', DAILY, 403),
        ('admin', {**DAILY, 'interval_hours': 0}, 422),
        ('admin', {**DAILY, 'interval_hours': True}, 422),
        ('admin', {**DAILY, 'start_hour': 24}, 422),
        ('admin', {**DAILY, 'keep': 0}, 422),
        ('admin', {'keep': 5}, 422),  # a partial body would turn backups off
        ('admin', {}, 422),
    ],
)
def test_only_admins_can_change_the_schedule_and_values_are_checked(role, body, code):
    app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'username': 'u', 'role': role}
    try:
        client = TestClient(app)
        assert client.put('/api/admin/backups/schedule', json=body).status_code == code
        if role != 'admin':
            assert client.post('/api/admin/backups/run').status_code == 403
            assert client.get('/api/admin/backups/schedule').status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _job(keep=7, attempt=1, started_at=None):
    return {'id': 'job', 'args': {'keep': keep}, 'attempt': attempt, 'started_at': started_at}


def test_a_missing_folder_fails_the_job_with_a_reason(tmp_path):
    with pytest.raises(RuntimeError, match='is the backup drive mounted'):
        asyncio.run(backups.handler(str(tmp_path / 'unmounted'))(_job()))
    with pytest.raises(RuntimeError, match='FRIDGE_BACKUP_DIR is not set'):
        asyncio.run(backups.handler('')(_job()))


# Stands in for scripts/backup_local.py: records its arguments, then does what the test asks.
FAKE_SCRIPT = '''
import json, sys
from datetime import datetime, timezone
from pathlib import Path
args = sys.argv[1:]
folder = Path(args[args.index('--dir') + 1])
(folder / 'arguments.json').write_text(json.dumps(args))
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
'''


def _fake_backup(tmp_path, monkeypatch, body):
    script = tmp_path / 'fake_backup_local.py'
    script.write_text(FAKE_SCRIPT + body)
    monkeypatch.setattr(backups, 'SCRIPT', script)
    folder = tmp_path / 'backups'
    folder.mkdir()
    return folder


def _fails_with(error):
    return f'''
record = {{'verified': False, 'failed_at': stamp, 'error': {error!r}}}
(folder / f'freedomridge_{{stamp}}.failed.json').write_text(json.dumps(record))
sys.exit(1)
'''


def test_the_job_passes_keep_and_the_folder_to_the_script(tmp_path, monkeypatch):
    folder = _fake_backup(tmp_path, monkeypatch, _fails_with('BackupError: disk full'))
    with pytest.raises(RuntimeError, match='disk full') as failed:
        asyncio.run(backups.handler(str(folder))(_job(keep=3)))
    assert not job_queue.is_transient(failed.value)  # a real failure isn't retried
    arguments = ['--dir', str(folder), '--keep', '3', '--job-id', 'job']
    assert json.loads((folder / 'arguments.json').read_text()) == arguments


def test_a_backup_a_restart_interrupted_runs_again(tmp_path, monkeypatch):
    # A restart signals the runner's whole process group: the script stops itself, not the job.
    folder = _fake_backup(tmp_path, monkeypatch, _fails_with('BackupError: stopped by SIGTERM'))
    with pytest.raises(ConnectionError) as interrupted:
        asyncio.run(backups.handler(str(folder))(_job()))
    assert job_queue.is_transient(interrupted.value)


def test_a_resumed_backup_finishes_with_the_dump_an_earlier_attempt_made(tmp_path, monkeypatch):
    # A restored dump holds the job that made it, mid-run; in the same cluster the runner resumes that job.
    # Undoing damage restores an older dump in place: the folder also holds newer dumps of the damaged history.
    folder = _fake_backup(tmp_path, monkeypatch, _fails_with('BackupError: disk full'))
    started = datetime(2026, 10, 20, 7, 0, 4, 300000, tzinfo=timezone.utc)
    dump = test_system_status._dump(folder, started + timedelta(seconds=1), job_id='job')
    test_system_status._dump(folder, started + timedelta(days=1), job_id='next-night')
    outcome = asyncio.run(backups.handler(str(folder))(_job(attempt=2, started_at=started)))
    assert (outcome.finished, outcome.message) == (True, 'Backup verified; an earlier attempt made it')
    assert outcome.result == {'file': dump.name, 'size_bytes': 1000, 'tables': 2}
    assert not (folder / 'arguments.json').exists()  # no second pg_dump


def test_an_interrupted_backup_without_its_own_verified_dump_runs_again(tmp_path, monkeypatch):
    folder = _fake_backup(tmp_path, monkeypatch, _fails_with('BackupError: stopped by SIGTERM'))
    started = datetime(2026, 10, 20, 7, 0, 4, tzinfo=timezone.utc)
    test_system_status._dump(folder, started + timedelta(minutes=1), job_id='job', verified=False)
    test_system_status._dump(folder, started + timedelta(minutes=5), manifest=False)  # cut off mid-dump
    test_system_status._dump(folder, started + timedelta(hours=2))  # cron, or made before manifests named jobs
    test_system_status._dump(folder, started + timedelta(days=1), job_id='next-night')  # another history's
    with pytest.raises(ConnectionError):
        asyncio.run(backups.handler(str(folder))(_job(attempt=2, started_at=started)))
    assert (folder / 'arguments.json').exists()


@requires_database
def test_a_dumped_backup_restored_into_the_same_cluster_is_not_dumped_again(registry_database, tmp_path, monkeypatch):
    # Restore recovery can't see a restore into the same cluster, so the runner resumes the dumped job.
    folder = _fake_backup(tmp_path, monkeypatch, _fails_with('BackupError: disk full'))

    async def scenario(database):
        policies = {backups.KIND: backups.POLICY}
        job, _ = await backups.run_now(database.pool, None)
        claim = await job_queue.claim_job(database.pool, 'source', kinds=[backups.KIND], policies=policies)
        assert claim is not None and claim['id'] == job['id']
        dump = test_system_status._dump(folder, claim['started_at'] + timedelta(seconds=1), job_id=str(job['id']))
        await database.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 hour' WHERE id=$1",
            job['id'],
        )
        assert not await database.fetchval('SELECT restored_work_pending()')
        worker = await make_worker(database, {backups.KIND: backups.handler(str(folder))}, policies=policies)
        assert await worker.run_once()
        row = await database.fetchrow("SELECT state, attempt, result->>'file' AS file FROM jobs WHERE id=$1", job['id'])
        assert tuple(row) == ('succeeded', 2, dump.name)
        assert not (folder / 'arguments.json').exists()

    registry_database(scenario)


def test_a_run_without_a_failure_record_reports_the_scripts_last_line(tmp_path, monkeypatch):
    lock_held = '''
print('2026-10-20 02:00:01 -0500 another backup is still running; skipping this one')
sys.exit(1)
'''
    folder = _fake_backup(tmp_path, monkeypatch, lock_held)
    (folder / 'backup.log').write_text('2026-10-19 02:00:01 -0500 an earlier run\n')
    with pytest.raises(RuntimeError) as failed:
        asyncio.run(backups.handler(str(folder))(_job()))
    assert str(failed.value) == 'backup_local.py exited 1: another backup is still running; skipping this one'
    (folder / 'arguments.json').unlink()
    backups.SCRIPT.write_text(FAKE_SCRIPT + 'sys.exit(1)\n')  # says nothing: the earlier run's line isn't this one's
    with pytest.raises(RuntimeError, match=f'exited 1: see backup.log in {folder}$'):
        asyncio.run(backups.handler(str(folder))(_job()))


def test_the_runner_service_runs_and_schedules_backups(monkeypatch, tmp_path):
    seen: dict = {}
    (tmp_path / '0001_shipped.sql').write_text('SELECT 1;\n')
    monkeypatch.setattr(schema_check, 'MIGRATIONS_DIR', tmp_path)

    async def scenario():
        loop = asyncio.get_running_loop()
        callbacks: dict = {}
        stopped = asyncio.Event()

        async def fetchval(sql, migrations, **kwargs):
            seen['migrations'] = migrations
            return True

        @asynccontextmanager
        async def local_runner(dsn, handlers, policies, placements):
            seen.update(handlers=handlers, policies=policies, placements=placements)
            yield SimpleNamespace(
                workers=[SimpleNamespace(pool=SimpleNamespace(fetchval=fetchval))],
                run=stopped.wait,
                stop=stopped.set,
            )

        async def nothing_due(*args, **kwargs):
            return None

        async def schedule_backups(pool):
            seen['scheduled'] = True
            callbacks[signal.SIGTERM]()

        monkeypatch.setattr(loop, 'add_signal_handler', lambda signum, callback: callbacks.update({signum: callback}))
        monkeypatch.setattr(loop, 'remove_signal_handler', lambda signum: None)
        monkeypatch.setattr(job_service, 'local_runner', local_runner)
        monkeypatch.setattr(job_service, 'schedule_cleanup', nothing_due)
        monkeypatch.setattr(statistics, 'schedule_statistics', nothing_due)
        monkeypatch.setattr(backups, 'schedule_backups', schedule_backups)
        await asyncio.wait_for(job_service.run_service('unused'), 10)

    asyncio.run(scenario())
    assert backups.KIND in seen['handlers'] and seen['policies'][backups.KIND] is backups.POLICY
    assert seen['placements'] == ('server', 'compute')  # the default: one machine takes every step
    assert seen['migrations'] == [schema_check.BASELINE, '0001_shipped.sql'] and seen['scheduled']


def _point_at(monkeypatch, container, user, database):
    monkeypatch.setenv('FRIDGE_DB_CONTAINER', container)
    monkeypatch.setenv('FRIDGE_DB_USER', user)
    monkeypatch.setenv('FRIDGE_DB_NAME', database)


@requires_container
def test_the_job_keeps_a_verified_backup_and_reports_it(tmp_path, scratch_databases, monkeypatch):
    container, user, psql, create = scratch_databases
    source = create()
    psql(source, 'CREATE TABLE items AS SELECT g AS id FROM generate_series(1, 500) g;')
    _point_at(monkeypatch, container, user, source)
    outcome = asyncio.run(backups.handler(str(tmp_path))(_job(keep=2)))
    assert isinstance(outcome, StepResult) and outcome.finished
    dump = tmp_path / outcome.result['file']
    assert dump.exists() and outcome.result['size_bytes'] == dump.stat().st_size and outcome.result['tables'] == 1
    manifest = json.loads((tmp_path / f'{dump.name}.manifest.json').read_text())
    assert (manifest['verified'], manifest['job_id']) == (True, 'job')
    assert 'verified' in (tmp_path / 'backup.log').read_text()


@requires_container
def test_a_failed_backup_fails_the_job_with_the_scripts_reason(tmp_path, scratch_databases, monkeypatch):
    container, user, _, create = scratch_databases
    _point_at(monkeypatch, container, user, f'{create()}_missing')
    with pytest.raises(RuntimeError) as failed:
        asyncio.run(backups.handler(str(tmp_path))(_job()))
    record = json.loads(next(tmp_path.glob('*.failed.json')).read_text())
    assert str(failed.value) == record['error']


@requires_container
def test_cancelling_the_job_stops_the_dump_and_keeps_nothing(tmp_path, scratch_databases, monkeypatch):
    container, user, psql, create = scratch_databases
    source = create()
    psql(source, 'CREATE TABLE big AS SELECT g AS id, md5(g::text) AS h FROM generate_series(1, 6000000) g;')
    _point_at(monkeypatch, container, user, source)
    sessions = (
        "SELECT count(*) FROM pg_stat_activity "
        f"WHERE datname = '{source}' AND application_name LIKE 'fridge-backup-%' AND query LIKE 'COPY%';"
    )

    async def cancel_once_dumping():
        task = asyncio.create_task(backups.handler(str(tmp_path))(_job()))
        deadline = time.monotonic() + 60
        while (await asyncio.to_thread(psql, 'postgres', sessions)).strip() == '0':
            assert not task.done() and time.monotonic() < deadline, 'the dump never started'
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_once_dumping())
    assert 'stopped by SIGTERM' in json.loads(next(tmp_path.glob('*.failed.json')).read_text())['error']
    assert not list(tmp_path.glob('*.partial')) and not list(tmp_path.glob('*.sql.gz'))
    deadline = time.monotonic() + 1.5
    while psql('postgres', sessions).strip() != '0':
        assert time.monotonic() < deadline, 'pg_dump kept running in the container'
        time.sleep(0.1)


@requires_container
@requires_database
def test_the_runner_runs_a_queued_backup_to_success(registry_database, tmp_path, scratch_databases, monkeypatch):
    container, user, psql, create = scratch_databases
    source = create()
    psql(source, 'CREATE TABLE items AS SELECT g AS id FROM generate_series(1, 100) g;')
    _point_at(monkeypatch, container, user, source)

    async def scenario(database):
        job, _ = await backups.run_now(database.pool, None)
        worker = await make_worker(
            database,
            {backups.KIND: backups.handler(str(tmp_path))},
            policies={backups.KIND: backups.POLICY},
        )
        await worker.run_once()
        row = await database.fetchrow('SELECT state, result, message FROM jobs WHERE id=$1', job['id'])
        assert (row['state'], row['message'], row['result']['tables']) == ('succeeded', 'Backup verified', 1)

    registry_database(scenario)
