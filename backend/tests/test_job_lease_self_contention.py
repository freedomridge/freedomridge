"""A slot's publication never meets its own lease thread: renewals pause around it, and telemetry naming the job
does not lock the row against the fence."""

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager

import pytest

from backend import job_heartbeat, job_queue, map_refit
from backend.job_runner import StepResult
from backend.map_artifacts import LocalArtifactStore
from backend.tests import test_field_registry_postgres, test_map_refit
from backend.tests.test_job_attempt_recording import LEASE, stall_next_renewal_commit
from backend.tests.test_job_launch import wait_until
from backend.tests.test_job_runner import job_state, make_worker
from backend.tests.test_map_refit import run_job, seed, submit
from backend.tests.test_map_refit_review import assert_grid_ranks

registry_database = test_field_registry_postgres.registry_database
fake_fit = test_map_refit.fake_fit

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


def assert_published_first_time(row):
    assert row['state'] == 'succeeded', f"The publication met its own lease thread: {row['error']}"
    assert (row['attempt'], row['failures'], row['contentions'], row['error']) == (1, 0, 0, None)


def test_publication_waits_for_its_own_in_flight_renewal(registry_database, monkeypatch):
    async def scenario(database):
        armed, holding = stall_next_renewal_commit(monkeypatch)

        async def handler(job):
            armed.set()
            assert await asyncio.to_thread(holding.wait, 5)
            return StepResult(finished=True)

        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(database, {'example': handler}, **LEASE)
        assert await asyncio.wait_for(worker.run_once(), 5)
        assert_published_first_time(await job_state(database, submitted))

    registry_database(scenario)


def test_first_telemetry_of_an_attempt_does_not_contend_with_a_short_first_unit(registry_database, monkeypatch):
    """The first lease telemetry of every attempt names the job in workers.current_job_id (a foreign key). A short
    unit that publishes while that commit is slow must still publish, on its first attempt."""

    async def scenario(database):
        report = job_heartbeat.worker_heartbeat
        holding = threading.Event()

        async def slow_commit_telemetry(source, worker_id, kinds, current_job_id=None):
            if current_job_id is None or holding.is_set() or not hasattr(source, 'transaction'):
                return await report(source, worker_id, kinds, current_job_id)
            async with source.transaction():
                await report(source, worker_id, kinds, current_job_id)
                holding.set()
                await asyncio.sleep(0.5)  # the commit waits on a slow WAL flush

        monkeypatch.setattr(job_heartbeat, 'worker_heartbeat', slow_commit_telemetry)

        async def handler(job):
            assert await asyncio.to_thread(holding.wait, 5)
            return StepResult(finished=True)

        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(database, {'example': handler}, **LEASE)
        assert await asyncio.wait_for(worker.run_once(), 5)
        assert_published_first_time(await job_state(database, submitted))

    registry_database(scenario)


def test_a_row_referencing_the_job_does_not_block_its_fence_but_writers_still_exclude(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        job = await job_queue.claim_job(database.pool, 'slot', kinds=['example'])
        assert job is not None
        async with database.pool.acquire() as telemetry, telemetry.transaction():
            # Uncommitted: its foreign-key check holds FOR KEY SHARE on the job row.
            await job_heartbeat.worker_heartbeat(telemetry, 'slot', ['example'], job['id'])
            published = await job_queue.publish_step(database.pool, job['id'], job['lease_token'], message='unit')
            assert published['message'] == 'unit'
        async with database.pool.acquire() as publication, publication.transaction():
            await job_queue._locked_job(publication, job['id'], job['lease_token'])
            with pytest.raises(job_queue.QueueContended):
                await job_queue.heartbeat_job(database.pool, job['id'], job['lease_token'])

    registry_database(scenario)


class Renewals:
    """Every renewal the lease thread starts, as (paused, remaining): whether the test held a pause then, and
    how much of the lease was left. A pause lets through only a renewal within the monitor's renewal reserve
    of expiry, so the tests assert that order of events instead of wall-clock windows."""

    def __init__(self, monkeypatch):
        self.monitor: job_heartbeat.LeaseMonitor | None = None
        self.paused = False
        self.started: list[tuple[bool, float]] = []
        renew = job_queue.heartbeat_job

        async def recorded(connection, job_id, lease_token, **options):
            if self.monitor is not None:
                self.started.append((self.paused, self.monitor.renew_by - time.monotonic()))
            return await renew(connection, job_id, lease_token, **options)

        monkeypatch.setattr(job_queue, 'heartbeat_job', recorded)

    def during_pause(self):
        return [remaining for paused, remaining in self.started if paused]

    def assert_pause_held_all_but_reserve_renewals(self):
        assert self.monitor is not None
        reserve = self.monitor.renewal_reserve
        early = [remaining for remaining in self.during_pause() if not 0 < remaining <= reserve]
        assert not early, f'Renewed during a pause with {early} s of lease left (reserve {reserve} s)'

    def count(self, *, paused):
        return sum(1 for state, _ in self.started if state == paused)

    async def wait_for(self, count, *, paused):
        """Poll for `count` renewals started with (or without) the pause, allowing a few leases."""
        monitor = self.monitor
        assert monitor is not None

        async def started():
            assert not monitor.lost.is_set(), 'The lease was lost while waiting for renewals'
            return self.count(paused=paused) >= count

        await wait_until(started, timeout=3 * monitor.lease_seconds)

    async def wait_for_normal_cadence(self):
        """Poll for an unpaused renewal started with more than the reserve left: renewals are back at the
        heartbeat rate, not only the reserve's (which a resume() that did nothing would still produce)."""
        monitor = self.monitor
        assert monitor is not None
        before = len(self.started)

        async def back():
            assert not monitor.lost.is_set(), 'The lease was lost while waiting for renewals'
            return any(not paused and left > monitor.renewal_reserve for paused, left in self.started[before:])

        await wait_until(back, timeout=3 * monitor.lease_seconds)

    @asynccontextmanager
    async def pause(self, monitor):
        async with monitor.paused():
            self.paused = True
            try:
                yield
            finally:
                self.paused = False


async def monitored(database, renewals, lease_seconds=8, heartbeat_seconds=1):
    """An 8 s lease renewed every second: 1 s renewal budget, 6 s renewal reserve (4*1 + 1 + 1), so a renewal
    due 1 s after the last one is held by a pause until 2 s, with seconds of slack for a loaded host."""
    namespace = await database.fetchval('SELECT current_schema()')
    connect = job_heartbeat.heartbeat_connector(
        os.environ['FRIDGE_TEST_POSTGRES_DSN'],
        server_settings={'search_path': namespace + ',public'},
    )
    await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
    job = await job_queue.claim_job(database.pool, 'slot', kinds=['example'], lease_seconds=lease_seconds)
    assert job is not None
    session = job_heartbeat.LeaseSession(connect, 'slot')
    await session.start()
    monitor = job_heartbeat.LeaseMonitor(session, job, 'slot', ['example'], lease_seconds, heartbeat_seconds)
    renewals.monitor = monitor
    await monitor.start()
    return job, session, monitor


def test_paused_renewals_wait_until_resumed(registry_database, monkeypatch):
    async def scenario(database):
        renewals = Renewals(monkeypatch)
        job, session, monitor = await monitored(database, renewals)
        try:
            async with renewals.pause(monitor):
                # The renewal due a heartbeat after the last one waits; the first to start is the reserve's.
                await renewals.wait_for(1, paused=True)
            renewals.assert_pause_held_all_but_reserve_renewals()
            await renewals.wait_for_normal_cadence()
            assert not monitor.lost.is_set()
        finally:
            await monitor.close()
            await session.close()

    registry_database(scenario)


def test_an_inner_pause_ending_does_not_end_the_outer_pause(registry_database, monkeypatch):
    """A pause inside a pause (a publisher callback around a handler-style fence, say): renewals resume only
    when the outer pause ends."""

    async def scenario(database):
        renewals = Renewals(monkeypatch)
        job, session, monitor = await monitored(database, renewals)
        try:
            async with renewals.pause(monitor):
                async with monitor.paused():
                    pass
                await renewals.wait_for(1, paused=True)
            renewals.assert_pause_held_all_but_reserve_renewals()
            await renewals.wait_for_normal_cadence()
            assert not monitor.lost.is_set()
        finally:
            await monitor.close()
            await session.close()

    registry_database(scenario)


def test_a_long_pause_still_renews_the_lease_before_it_could_expire(registry_database, monkeypatch):
    async def scenario(database):
        renewals = Renewals(monkeypatch)
        job, session, monitor = await monitored(database, renewals)
        try:
            async with renewals.pause(monitor):
                paused_at = time.monotonic()
                # Longer than the lease itself, and through more than one reserve renewal.
                await renewals.wait_for(2, paused=True)
                await asyncio.sleep(max(0.0, monitor.lease_seconds - (time.monotonic() - paused_at)))
                assert not monitor.lost.is_set()
                row = await job_state(database, job)
                assert await database.fetchval('SELECT $1 > clock_timestamp()', row['lease_expires_at'])
            renewals.assert_pause_held_all_but_reserve_renewals()
        finally:
            await monitor.close()
            await session.close()

    registry_database(scenario)


@pytest.mark.parametrize('phase', ['publish', 'rank'])
def test_a_handler_fence_waits_for_the_jobs_own_slow_renewal(registry_database, tmp_path, monkeypatch, fake_fit, phase):
    """map_refit's swap (checkpoint_locked) and re-rank (finish_locked) fence the job row inside their own long
    transactions, with a 2 s lock timeout. The job's own renewal is mid-commit on that row for 2.5 s when the
    fence is reached: the swap or re-rank still commits on its first try."""
    store = LocalArtifactStore(tmp_path)
    armed, holding = stall_next_renewal_commit(monkeypatch, seconds=2.5)
    record = map_refit._record_schedule_result
    fence_name = 'checkpoint_locked' if phase == 'publish' else 'finish_locked'
    fence = getattr(job_queue, fence_name)
    outcomes: list[str] = []

    async def record_while_the_renewal_commits(connection, job, result):
        await record(connection, job, result)  # the last statement before the fence
        if job['checkpoint'].get('phase') == phase and not armed.is_set():
            armed.set()
            assert await asyncio.to_thread(holding.wait, 5)

    async def counted_fence(connection, job_id, lease_token, **options):
        try:
            row = await fence(connection, job_id, lease_token, **options)
        except BaseException as error:
            outcomes.append(type(error).__name__)
            raise
        outcomes.append('fenced')
        return row

    monkeypatch.setattr(map_refit, '_record_schedule_result', record_while_the_renewal_commits)
    monkeypatch.setattr(job_queue, fence_name, counted_fence)

    async def scenario(database):
        await seed(database, n=200, dim=4)
        # A 3 s renewal budget (lease / 8), so the stalled commit is not cut short by the lease thread.
        finished = await run_job(database, await submit(database), store, lease_seconds=24, heartbeat_seconds=1)
        assert outcomes == ['fenced'], outcomes
        assert finished['state'] == 'succeeded'
        await assert_grid_ranks(database, 200)

    registry_database(scenario)


def test_renewals_paused_outside_a_slot_does_nothing():
    async def scenario():
        assert job_heartbeat.current_lease.get() is None
        async with job_heartbeat.renewals_paused():
            pass

    asyncio.run(scenario())
