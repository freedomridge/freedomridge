"""Dataset schedules: time rules across DST, conditions, the scheduler and the API."""

import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend import dataset_schedules, job_queue
from backend.routers import schedules
from backend.tests import test_field_registry_postgres
from backend.tests.test_map_refit import FIELD, seed

registry_database = test_field_registry_postgres.registry_database
requires_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL test database',
)
CHICAGO = ZoneInfo('America/Chicago')
ADMIN = {'id': 1, 'role': 'admin'}


def local(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CHICAGO)


def rule(frequency='daily', weekday=None, at=time(3, 0), **conditions):
    return {
        'frequency': frequency,
        'weekday': weekday,
        'time_of_day': at,
        'timezone': 'America/Chicago',
        'min_new_samples': conditions.get('min_new_samples', 20000),
        'min_days_between': conditions.get('min_days_between', 7),
    }


@pytest.mark.parametrize(
    'schedule, after, expected',
    [
        (rule(), local(2026, 10, 20, 2, 59), local(2026, 10, 20, 3)),
        (rule(), local(2026, 10, 20, 3), local(2026, 10, 21, 3)),  # strictly after
        (rule('weekly', 0), local(2026, 10, 20, 12), local(2026, 10, 25, 3)),  # Tue -> Sunday
        (rule('weekly', 2), local(2026, 10, 20, 2), local(2026, 10, 20, 3)),  # Tuesday itself
        (rule('weekly', 2), local(2026, 10, 20, 4), local(2026, 10, 27, 3)),
        # 2026-11-01 falls back: 03:00 stays 03:00 local (09:00 UTC, not 08:00).
        (rule(), local(2026, 10, 31, 12), local(2026, 11, 1, 3)),
        (rule(), local(2026, 11, 1, 12), local(2026, 11, 2, 3)),
        # 2027-03-14 springs forward: 02:30 does not exist, so that day it runs at 03:30 CDT.
        (rule(at=time(2, 30)), local(2027, 3, 13, 12), local(2027, 3, 14, 3, 30)),
        (rule(at=time(2, 30)), local(2027, 3, 14, 12), local(2027, 3, 15, 2, 30)),
        # 01:30 happens twice on 2026-11-01: the first one.
        (rule(at=time(1, 30)), local(2026, 10, 31, 12), datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)),
    ],
)
def test_next_slot_is_wall_clock_time_across_daylight_saving(schedule, after, expected):
    found = dataset_schedules.next_slot(schedule, after)
    assert found == expected
    assert found.tzinfo is not None
    assert dataset_schedules.next_slot(schedule, after.astimezone(timezone.utc)) == expected


def test_daily_03_00_is_24_or_25_hours_apart_across_the_fall_back():
    schedule = rule()
    first = dataset_schedules.next_slot(schedule, local(2026, 10, 30, 12))  # Oct 31 03:00 CDT
    second = dataset_schedules.next_slot(schedule, first)
    assert second - first == timedelta(hours=25)
    assert second.astimezone(CHICAGO).hour == 3


@pytest.mark.parametrize(
    'refitted, now, met',
    [
        (None, local(2026, 10, 27, 3), True),  # never re-fitted
        (local(2026, 10, 20, 4, 12), local(2026, 10, 27, 3), True),  # 7 calendar days even if < 7x24h
        (local(2026, 10, 20, 4, 12), local(2026, 10, 26, 3), False),
        # Across the spring-forward (one 23-hour day) a weekly cadence still holds.
        (local(2027, 3, 8, 3), local(2027, 3, 15, 3), True),
        (local(2026, 10, 27, 3, 30), local(2026, 11, 3, 3), True),  # across the fall-back
    ],
)
def test_days_between_runs_count_calendar_days_in_the_schedule_zone(refitted, now, met):
    state = {'refitted_at': refitted, 'points_at_refit': 0} if refitted else None
    ok, reason = dataset_schedules.conditions_met(rule(min_new_samples=None), state, 0, now)
    assert ok is met, reason


@pytest.mark.parametrize(
    'baseline, points, threshold, met',
    [
        (100_000, 119_999, 20000, False),
        (100_000, 120_000, 20000, True),
        (0, 19_999, 20000, False),  # never re-fitted: counted from zero
        (100_000, 90_000, 20000, False),  # deletions never count as new samples
        (100_000, 100_001, None, True),
    ],
)
def test_new_sample_threshold_uses_the_point_counter_since_the_last_refit(baseline, points, threshold, met):
    state = {'refitted_at': local(2026, 10, 1), 'points_at_refit': baseline} if baseline else None
    ok, reason = dataset_schedules.conditions_met(
        rule(min_new_samples=threshold, min_days_between=None),
        state,
        points,
        local(2026, 10, 20),
    )
    assert ok is met, reason
    if not met:
        assert 'new samples since the last re-fit' in reason


def test_schedule_rules_are_validated():
    assert schedules.ScheduleRule(frequency='weekly').weekday == 0  # weekly defaults to Sunday
    with pytest.raises(ValidationError):
        schedules.ScheduleRule(frequency='daily', weekday=3)
    with pytest.raises(ValidationError):
        schedules.ScheduleRule(time='3:00')
    with pytest.raises(ValidationError):
        schedules.ScheduleRule(timezone='Mars/Olympus')
    # The default rule (default policy): weekly on Sunday at 03:00, only when >= 20,000 new samples,
    # no minimum gap; the API's defaults (which the Add form starts from) are the same.
    assert schedules.ScheduleRule().model_dump() == {
        'frequency': 'weekly',
        'weekday': 0,
        'time': '03:00',
        'timezone': 'UTC',
        'min_new_samples': 20000,
        'min_days_between': None,
        'window_minutes': 180,
        'enabled': False,
    }
    assert {key: value for key, value in dataset_schedules.DEFAULTS.items() if key != 'time'} == {
        key: value for key, value in schedules.ScheduleRule().model_dump().items() if key != 'time'
    }


# --- Scheduler (PostgreSQL) -------------------------------------------------------------------------


async def add_schedule(database, dataset_id=1, field=FIELD, **overrides):
    values = {
        'enabled': True,
        'frequency': 'daily',
        'weekday': None,
        'time_of_day': time(3, 0),
        'timezone': 'America/Chicago',  # a zone with daylight-saving changes, as local() uses
        'min_new_samples': 20000,
        'min_days_between': 7,
        'next_run_at': local(2026, 10, 20, 3),
        **overrides,
    }
    return await database.fetchrow(
        """INSERT INTO dataset_job_schedules (dataset_id, job_type, target, enabled, frequency, weekday,
               time_of_day, timezone, min_new_samples, min_days_between, next_run_at)
           VALUES ($1,'map_refit',$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *""",
        dataset_id,
        field,
        values['enabled'],
        values['frequency'],
        values['weekday'],
        values['time_of_day'],
        values['timezone'],
        values['min_new_samples'],
        values['min_days_between'],
        values['next_run_at'],
    )


async def refits(database):
    return await database.fetch("SELECT * FROM jobs WHERE kind='map_refit' ORDER BY created_at")


@requires_database
def test_due_schedule_queues_a_compute_job_in_the_maintenance_lane_and_moves_to_the_next_time(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        schedule = await add_schedule(database, min_new_samples=20)
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 20, 2, 59)) == []
        [job] = await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 20, 3, 0))
        assert (job['kind'], job['placement'], job['lane'], job['dataset_id']) == (
            'map_refit',
            'compute',
            'maintenance',
            1,
        )
        assert job['args']['field'] == FIELD and job['args']['schedule_id'] == schedule['id']
        assert datetime.fromisoformat(job['args']['window_end']) == local(2026, 10, 20, 6)  # 03:00 + 180 min
        assert job['resources'] == {'exclusive': 'map_fit'}
        assert job['dedupe_owner'] == 'system'
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['next_run_at'] == local(2026, 10, 21, 3)
        assert row['last_job_id'] == job['id'] and row['last_run_at'] == local(2026, 10, 20, 3)
        assert row['last_check']['ran'] is True
        # The same slot never queues twice.
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 20, 3, 1)) == []
        assert len(await refits(database)) == 1

    registry_database(scenario)


@requires_database
def test_conditions_not_met_record_why_and_wait_for_the_next_time(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        schedule = await add_schedule(database)  # needs 20,000 new samples
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 20, 3)) == []
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['next_run_at'] == local(2026, 10, 21, 3)
        assert row['last_check']['ran'] is False
        assert row['last_check']['reason'] == '30 new samples since the last re-fit; needs 20,000'
        assert await refits(database) == []
        # A recent re-fit holds it back too, whatever the sample count: the check goes straight to the
        # first night the 7-day gap allows (Oct 18 + 7), without recording a skip.
        await database.execute(
            """INSERT INTO embedding_map_state (dataset_id, field_name, refitted_at, points_at_refit)
               VALUES (1, $1, $2, 0)""",
            FIELD,
            local(2026, 10, 18, 4),
        )
        await database.execute('UPDATE dataset_job_schedules SET min_new_samples=NULL WHERE id=$1', schedule['id'])
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 21, 3)) == []
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['next_run_at'] == local(2026, 10, 25, 3)
        assert row['last_check']['reason'] == '30 new samples since the last re-fit; needs 20,000'  # unchanged
        # The runner was down from Oct 25 to Oct 26 10:00: the missed Oct 25 slot is not caught up then.
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 26, 10)) == []
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['last_check']['reason'].startswith('Missed Oct 25 03:00')
        assert row['next_run_at'] == local(2026, 10, 27, 3)
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 27, 3)) != []

    registry_database(scenario)


@requires_database
def test_due_runs_of_several_datasets_queue_together_and_one_fit_runs_at_a_time(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        await seed(database, n=30, dataset_id=2, start=1001)
        await add_schedule(database, min_new_samples=None, next_run_at=local(2026, 10, 20, 3))
        await add_schedule(database, dataset_id=2, min_new_samples=None, next_run_at=local(2026, 10, 20, 3))
        jobs = await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 20, 3, 1))
        assert sorted(job['dataset_id'] for job in jobs) == [1, 2]
        claimed = await job_queue.claim_job(database.pool, 'w1', placements=['compute'], kinds=['map_refit'])
        assert claimed is not None
        assert await job_queue.claim_job(database.pool, 'w2', placements=['compute'], kinds=['map_refit']) is None

    registry_database(scenario)


@requires_database
def test_disabled_schedules_and_deleting_datasets_never_run(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        await seed(database, n=30, dataset_id=2, start=1001)
        await add_schedule(database, enabled=False, min_new_samples=None)
        await add_schedule(database, dataset_id=2, min_new_samples=None)
        await database.execute('UPDATE datasets SET deleting=TRUE WHERE id=2')
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 22, 3)) == []
        assert await refits(database) == []

    registry_database(scenario)


@requires_database
def test_run_now_ignores_conditions_and_joins_an_unfinished_refit(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        schedule = dict(await add_schedule(database, enabled=False))  # conditions would not hold
        job, created = await dataset_schedules.run_now(database.pool, schedule, 1)
        assert created and job['created_by'] == 1 and job['placement'] == 'compute'
        assert job['args']['window_end'] is None  # Run now has no start window
        again, created_again = await dataset_schedules.run_now(database.pool, schedule, 1)
        assert again['id'] == job['id'] and not created_again
        # The scheduler does not queue a second fit of the same map either.
        await database.execute(
            'UPDATE dataset_job_schedules SET enabled=TRUE, min_new_samples=NULL WHERE id=$1',
            schedule['id'],
        )
        [joined] = await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 20, 3))
        assert joined['id'] == job['id']
        assert len(await refits(database)) == 1

    registry_database(scenario)


# --- API ---------------------------------------------------------------------------------------------


@requires_database
def test_schedule_api_create_edit_toggle_run_and_delete(registry_database, monkeypatch):
    monkeypatch.setattr(schedules, 'db', None)

    async def scenario(database):
        monkeypatch.setattr(schedules, 'db', database)
        await seed(database, n=30)
        listed = await schedules.list_schedules(1)
        assert listed['items'] == [] and listed['defaults']['min_new_samples'] == 20000
        created = await schedules.create_schedule(1, schedules.ScheduleCreate(target=FIELD), user=ADMIN)
        assert created['enabled'] is False and created['next_run_at'] is None
        assert (
            created['frequency'],
            created['weekday'],
            created['time'],
            created['min_new_samples'],
            created['min_days_between'],
        ) == ('weekly', 0, '03:00', 20000, None)
        assert created['map'] == {
            'model_id': None,
            'refitted_at': None,
            'points_at_refit': 0,
            'current_points': 30,
            'new_samples': 30,
            'refit_running': False,
        }
        with pytest.raises(HTTPException) as duplicate:
            await schedules.create_schedule(1, schedules.ScheduleCreate(target=FIELD), user=ADMIN)
        assert duplicate.value.status_code == 409
        for bad in ('missing_field', 'clip_patch_4x4'):
            with pytest.raises(HTTPException) as refused:
                await schedules.create_schedule(1, schedules.ScheduleCreate(target=bad), user=ADMIN)
            assert refused.value.status_code == 422
        enabled = await schedules.update_schedule(
            1,
            created['id'],
            schedules.ScheduleRule(enabled=True, frequency='weekly', weekday=0, time='04:15', min_new_samples=None),
            user=ADMIN,
        )
        assert enabled['enabled'] and enabled['min_new_samples'] is None
        next_run = enabled['next_run_at'].astimezone(ZoneInfo(enabled['timezone']))
        assert (next_run.weekday(), next_run.hour, next_run.minute) == (6, 4, 15)  # a Sunday, 04:15 in its zone
        # Changing only a condition keeps the next run.
        same = await schedules.update_schedule(
            1,
            created['id'],
            schedules.ScheduleRule(enabled=True, frequency='weekly', weekday=0, time='04:15', min_new_samples=5),
            user=ADMIN,
        )
        assert same['next_run_at'] == enabled['next_run_at']
        ran = await schedules.run_schedule(1, created['id'], user=ADMIN)
        assert ran['created'] and ran['state'] == 'queued'
        listed = await schedules.list_schedules(1)
        assert listed['items'][0]['last_job']['id'] == uuid_of(ran['job_id'])
        with pytest.raises(HTTPException) as other_dataset:
            await schedules.run_schedule(2, created['id'], user=ADMIN)
        assert other_dataset.value.status_code == 404
        await schedules.delete_schedule(1, created['id'], user=ADMIN)
        assert (await schedules.list_schedules(1))['items'] == []

    registry_database(scenario)


def uuid_of(text):
    import uuid

    return uuid.UUID(text)
