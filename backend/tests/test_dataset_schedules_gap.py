"""Schedules with a minimum gap between re-fits go straight to the first night the gap allows (no nightly
skips), and the default rule is weekly on Sunday at 03:00 with >= 20,000 new samples (2026-10-21)."""

import asyncio
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fridge import map_fit
from fridge.core import dataset as sdk_dataset

from backend import dataset_schedules, map_refit
from backend.map_artifacts import LocalArtifactStore
from backend.routers import schedules
from backend.tests import test_field_registry_postgres
from backend.tests.test_dataset_schedules import ADMIN, CHICAGO, add_schedule, local, refits, rule
from backend.tests.test_map_refit import FIELD, fake_reducer, run_job, seed

registry_database = test_field_registry_postgres.registry_database
requires_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Disposable PostgreSQL required',
)


@pytest.fixture
def fake_fit(monkeypatch):
    """Only the child process is replaced: the real export is read and real outputs are written."""

    async def run_fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
        monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
        await asyncio.to_thread(map_fit.main, ['--input', str(source), '--output', str(output)])

    monkeypatch.setattr(map_refit, 'run_fit', run_fit)


def test_the_default_rule_is_weekly_on_sunday_with_no_gap():
    assert dataset_schedules.DEFAULTS == {
        'enabled': False,
        'frequency': 'weekly',
        'weekday': 0,
        'time': '03:00',
        'timezone': 'UTC',
        'min_new_samples': 20000,
        'min_days_between': None,
        'window_minutes': 180,
    }


@pytest.mark.parametrize(
    'zone, refitted, at, expected',
    [
        # Re-fitted Tue Oct 20 04:12; 7 days -> Tue Oct 27; the daily 03:00 that day.
        ('America/Chicago', local(2026, 10, 20, 4, 12), time(3, 0), local(2026, 10, 27, 3)),
        # Across the fall-back (Nov 1): 03:00 CST on Nov 6, i.e. 09:00 UTC, not 08:00.
        ('America/Chicago', local(2026, 10, 30, 3, 30), time(3, 0), datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)),
        # Across the spring-forward (Mar 14 2027): 02:30 does not exist on Mar 14, but the gap ends Mar 15.
        ('America/Chicago', local(2027, 3, 8, 3), time(2, 30), datetime(2027, 3, 15, 2, 30, tzinfo=CHICAGO)),
        # Days count in the schedule's zone: Oct 21 23:30 UTC is Oct 22 in Tokyo, so the gap ends Oct 29 there.
        (
            'Asia/Tokyo',
            datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc),
            time(3, 0),
            datetime(2026, 10, 29, 3, 0, tzinfo=ZoneInfo('Asia/Tokyo')),
        ),
    ],
)
def test_the_first_allowed_night_counts_calendar_days_in_the_schedule_zone(zone, refitted, at, expected):
    schedule = {**rule(at=at, min_days_between=7), 'timezone': zone}
    assert dataset_schedules.first_allowed_slot(schedule, refitted, refitted) == expected
    # It agrees with the condition the scheduler checks: allowed that night, not the night before.
    state = {'refitted_at': refitted, 'points_at_refit': 0}
    assert dataset_schedules.conditions_met({**schedule, 'min_new_samples': None}, state, 0, expected)[0]
    before = dataset_schedules.next_slot(schedule, expected - timedelta(hours=30))
    assert before < expected
    assert not dataset_schedules.conditions_met({**schedule, 'min_new_samples': None}, state, 0, before)[0]


@requires_database
def test_a_daily_schedule_with_a_gap_jumps_to_the_first_allowed_night_after_a_refit(
    registry_database,
    tmp_path,
    fake_fit,
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        # The re-fit runs on the real clock (the job checks its start window against it), so the
        # schedule is due now; the nights after it are simulated.
        await seed(database, n=40)
        now = datetime.now(timezone.utc)
        schedule = await add_schedule(database, min_new_samples=None, next_run_at=now)
        [job] = await dataset_schedules.schedule_dataset_jobs(database.pool, now + timedelta(minutes=1))
        assert (await run_job(database, job, store))['state'] == 'succeeded'
        refitted = await database.fetchval('SELECT refitted_at FROM embedding_map_state WHERE field_name=$1', FIELD)
        assert refitted is not None
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        first = dataset_schedules.first_allowed_slot(dict(row), refitted, refitted)
        assert row['next_run_at'] == first
        assert (first.astimezone(CHICAGO).date() - refitted.astimezone(CHICAGO).date()).days == 7
        assert first.astimezone(CHICAGO).time() == time(3, 0)
        check = row['last_check']
        # Every six hours until then: nothing due, nothing recorded, no job.
        moment = refitted + timedelta(minutes=1)
        while moment < first:
            assert await dataset_schedules.schedule_dataset_jobs(database.pool, moment) == []
            moment += timedelta(hours=6)
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['last_check'] == check and row['next_run_at'] == first
        assert len(await refits(database)) == 1
        # Then it runs.
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, first + timedelta(seconds=30)) != []

    registry_database(scenario)


@requires_database
def test_a_gap_not_over_at_a_due_time_moves_on_without_recording_a_skip(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        await database.execute(
            """INSERT INTO embedding_map_state (dataset_id, field_name, refitted_at, points_at_refit)
               VALUES (1, $1, $2, 0)""",
            FIELD,
            local(2026, 10, 18, 4),
        )
        schedule = await add_schedule(database, min_new_samples=None, next_run_at=local(2026, 10, 19, 3))
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 19, 3)) == []
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['next_run_at'] == local(2026, 10, 25, 3) and row['last_check'] is None

    registry_database(scenario)


@requires_database
def test_gap_over_but_too_few_new_samples_records_the_skip_and_checks_the_next_slot(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        await database.execute(
            """INSERT INTO embedding_map_state (dataset_id, field_name, refitted_at, points_at_refit)
               VALUES (1, $1, $2, 0)""",
            FIELD,
            local(2026, 10, 18, 4),
        )
        schedule = await add_schedule(database, next_run_at=local(2026, 10, 25, 3))  # needs 20,000; has 30
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 25, 3)) == []
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['last_check']['ran'] is False
        assert row['last_check']['reason'] == '30 new samples since the last re-fit; needs 20,000'
        assert row['next_run_at'] == local(2026, 10, 26, 3)  # the next slot, re-checked then

    registry_database(scenario)


@requires_database
def test_weekly_without_a_gap_runs_every_sunday_with_enough_new_samples(registry_database, tmp_path, fake_fit):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=40)
        schedule = await add_schedule(
            database,
            frequency='weekly',
            weekday=0,
            min_new_samples=10,
            min_days_between=None,
            next_run_at=local(2030, 10, 27, 3),
        )
        sundays = []
        start = 10_000
        for week in range(3):
            sunday = local(2030, 10, 27, 3) + timedelta(days=7 * week)  # Nov 3 2030 falls back
            jobs = await dataset_schedules.schedule_dataset_jobs(database.pool, sunday + timedelta(seconds=30))
            assert len(jobs) == 1, sunday
            assert (await run_job(database, jobs[0], store))['state'] == 'succeeded'
            sundays.append(sunday)
            row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
            assert row['next_run_at'] == sunday + timedelta(days=7)  # 03:00 local across the fall-back
            assert row['next_run_at'].astimezone(CHICAGO).time() == time(3, 0)
            await seed(database, n=10, start=start)  # 10 new samples for next Sunday
            start += 100
        assert len(await refits(database)) == 3

    registry_database(scenario)


@requires_database
def test_the_api_shows_the_first_allowed_night_as_the_next_check(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(schedules, 'db', database)
        await seed(database, n=30)
        refitted = datetime.now(timezone.utc) - timedelta(days=2)
        await database.execute(
            """INSERT INTO embedding_map_state (dataset_id, field_name, refitted_at, points_at_refit)
               VALUES (1, $1, $2, 0)""",
            FIELD,
            refitted,
        )
        created = await schedules.create_schedule(
            1,
            schedules.ScheduleCreate(target=FIELD, enabled=True, frequency='daily', min_days_between=7),
            user=ADMIN,
        )
        zone = ZoneInfo(created['timezone'])
        next_check = created['next_run_at'].astimezone(zone)
        assert (next_check.date() - refitted.astimezone(zone).date()).days == 7
        # Switching the gap off moves the next check to the next 03:00.
        rule_off = schedules.ScheduleRule(enabled=True, frequency='daily', min_days_between=None)
        updated = await schedules.update_schedule(1, created['id'], rule_off, user=ADMIN)
        assert updated['next_run_at'] - datetime.now(timezone.utc) <= timedelta(days=1, hours=1)

    registry_database(scenario)


@requires_database
def test_run_now_ignores_the_gap_and_its_refit_moves_the_next_check(registry_database, tmp_path, fake_fit):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=40)
        await database.execute(
            """INSERT INTO embedding_map_state (dataset_id, field_name, refitted_at, points_at_refit)
               VALUES (1, $1, now() - interval '1 day', 40)""",
            FIELD,
        )
        schedule = dict(await add_schedule(database, min_new_samples=None, next_run_at=local(2030, 1, 1, 3)))
        job, created = await dataset_schedules.run_now(database.pool, schedule, 1)
        assert created  # a re-fit yesterday and a 7-day gap do not hold back Run now
        assert (await run_job(database, job, store))['state'] == 'succeeded'
        refitted = await database.fetchval('SELECT refitted_at FROM embedding_map_state WHERE field_name=$1', FIELD)
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['next_run_at'] == local(2030, 1, 1, 3)  # already later than the gap: kept
        await database.execute(
            'UPDATE dataset_job_schedules SET next_run_at=$2 WHERE id=$1',
            schedule['id'],
            local(2020, 1, 1, 3),
        )
        again, _ = await dataset_schedules.run_now(database.pool, schedule, 1)
        assert (await run_job(database, again, store))['state'] == 'succeeded'
        refitted = await database.fetchval('SELECT refitted_at FROM embedding_map_state WHERE field_name=$1', FIELD)
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['next_run_at'] == dataset_schedules.first_allowed_slot(dict(row), refitted, refitted)

    registry_database(scenario)
