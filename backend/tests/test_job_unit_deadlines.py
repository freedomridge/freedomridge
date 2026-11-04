"""Measurement deadlines cannot outlive their unit or silently stop healthy lease renewal."""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend import job_heartbeat, job_queue, job_runner
from backend.job_runner import StepResult
from backend.tests import test_field_registry_postgres
from backend.tests.test_job_runner import make_worker

registry_database = test_field_registry_postgres.registry_database
postgres = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an explicitly isolated PostgreSQL database',
)


@postgres
@pytest.mark.parametrize('delay', ['notification_cleanup', 'loop_dispatch'])
def test_completed_units_keep_their_results_and_lease_during_dispatch_delays(registry_database, monkeypatch, delay):
    # Each delay outlasts the unit deadline, and the three together outlast the lease, so the job only succeeds
    # if a late deadline spares completed units and renewals continue. Budgets of 1 s and more, not 0.15 s: a
    # healthy unit has to tolerate a main-thread pause (a GC pass late in a full suite) after it starts.
    unit_seconds, delay_seconds, lease_seconds = 1, 2, 4

    async def scenario(database):
        class SlowCancellation(asyncio.Event):
            async def wait(self):
                try:
                    return await super().wait()
                except asyncio.CancelledError:
                    await asyncio.sleep(delay_seconds)
                    raise

        class DelayedMonitor(job_heartbeat.LeaseMonitor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if delay == 'notification_cleanup':
                    self.lost = SlowCancellation()

        monkeypatch.setattr(job_runner, 'LeaseMonitor', DelayedMonitor)
        await database.execute('CREATE TABLE deadline_effects(unit INTEGER PRIMARY KEY)')

        async def handler(job):
            unit = job['checkpoint'].get('unit', 0) + 1
            if delay == 'loop_dispatch':
                asyncio.get_running_loop().call_soon(time.sleep, delay_seconds)

            async def publish(connection):
                await connection.execute('INSERT INTO deadline_effects VALUES ($1)', unit)

            return StepResult(checkpoint={'unit': unit}, done=unit, total=3, finished=unit == 3, publish=publish)

        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(
            database,
            {'example': handler},
            lease_seconds=lease_seconds,
            heartbeat_seconds=0.05,
            policies={'example': job_queue.JobPolicy(max_unit_seconds=unit_seconds)},
        )
        assert await asyncio.wait_for(worker.run_once(), 30)
        row = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', submitted['id'])
        assert row['state'] == 'succeeded', dict(row)
        assert row['checkpoint'] == {'unit': 3}
        assert row['attempt'] == 1 and row['failures'] == 0
        assert row['error'] is None
        assert [row['unit'] for row in await database.fetch('SELECT unit FROM deadline_effects ORDER BY unit')] == [
            1,
            2,
            3,
        ]
        assert worker.lease_session is None

    registry_database(scenario)


def test_a_late_timeout_notification_cannot_expire_the_next_unit():
    async def scenario():
        timestamp = datetime.now(timezone.utc)
        job = {'id': uuid4(), 'heartbeat_at': timestamp, 'lease_expires_at': timestamp + timedelta(seconds=60)}
        monitor = job_heartbeat.LeaseMonitor(None, job, 'test', ['example'], 60, 10)

        def begin():
            if hasattr(monitor, 'begin_unit'):
                return monitor.begin_unit(1)
            setattr(monitor, 'unit_deadline', time.monotonic() + 1)
            return SimpleNamespace(timed_out=getattr(monitor, 'timed_out'))

        first = begin()
        monitor.signal(first.timed_out)
        if hasattr(monitor, 'finish_unit'):
            monitor.finish_unit(first)
        else:
            setattr(monitor, 'unit_deadline', float('inf'))
        second = begin()
        await asyncio.sleep(0)
        assert first.timed_out.is_set()
        assert not second.timed_out.is_set()
        if hasattr(monitor, 'finish_unit'):
            monitor.finish_unit(second)

    asyncio.run(scenario())


def test_deadline_watchdog_tracks_only_the_active_unit_and_notifies_once(monkeypatch):
    async def scenario():
        timestamp = datetime.now(timezone.utc)
        job = {'id': uuid4(), 'heartbeat_at': timestamp, 'lease_expires_at': timestamp + timedelta(seconds=60)}
        monitor = job_heartbeat.LeaseMonitor(None, job, 'test', ['example'], 60, 10)
        notifications: list[asyncio.Event] = []
        monkeypatch.setattr(monitor, 'signal', notifications.append)
        first = monitor.begin_unit(1)
        assert not monitor._unit_overdue(first.deadline - 0.01)
        with pytest.raises(RuntimeError, match='already active'):
            monitor.begin_unit(1)
        assert monitor._unit_overdue(first.deadline)
        assert monitor._unit_overdue(first.deadline + 1)
        assert notifications == [first.timed_out]
        monitor.finish_unit(first)
        completed_at = first.completed_at
        assert not monitor._unit_overdue(first.deadline + 2)
        second = monitor.begin_unit(1)
        monitor.finish_unit(first)
        assert first.completed_at == completed_at
        assert monitor._unit_overdue(second.deadline)
        assert notifications == [first.timed_out, second.timed_out]
        monitor.finish_unit(second)

    asyncio.run(scenario())


@postgres
@pytest.mark.parametrize('blocks_loop', [False, True])
def test_late_measurements_never_publish_even_if_the_handler_returns(registry_database, blocks_loop):
    async def scenario(database):
        published = []

        async def handler(job):
            if blocks_loop:
                time.sleep(0.3)
            else:
                await asyncio.sleep(0.3)

            async def publish(connection):
                published.append(True)

            return StepResult(finished=True, publish=publish)

        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(
            database,
            {'example': handler},
            lease_seconds=0.8,
            heartbeat_seconds=0.05,
            policies={'example': job_queue.JobPolicy(max_unit_seconds=0.12, max_failures=1)},
        )
        assert await asyncio.wait_for(worker.run_once(), 3)
        row = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', submitted['id'])
        assert row['state'] == 'failed', dict(row)
        assert row['error']['type'] == 'TimeoutError'
        assert row['checkpoint'] == {}
        assert published == []
        assert worker.lease_session is None

    registry_database(scenario)


@postgres
def test_an_overdue_noncooperative_unit_cannot_keep_renewing_its_lease(registry_database):
    async def scenario(database):
        cancelling, release = asyncio.Event(), asyncio.Event()
        published = []

        async def handler(job):
            try:
                await asyncio.Event().wait()
            finally:
                cancelling.set()
                await release.wait()

            async def publish(connection):
                published.append(True)

            return StepResult(finished=True, publish=publish)

        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(
            database,
            {'example': handler},
            lease_seconds=0.8,
            heartbeat_seconds=0.05,
            policies={'example': job_queue.JobPolicy(max_unit_seconds=0.12)},
        )
        execution = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(cancelling.wait(), 2)
            await asyncio.sleep(1)
            assert await database.fetchval(
                'SELECT lease_expires_at < clock_timestamp() FROM jobs WHERE id=$1',
                submitted['id'],
            )
            assert not execution.done()
        finally:
            release.set()
            await asyncio.wait_for(execution, 3)
        assert published == []
        assert worker.lease_session is None

    registry_database(scenario)
