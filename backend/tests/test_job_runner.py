"""Server worker recovery, fairness, cancellation and publication fencing."""

import asyncio
import os
import sys

import asyncpg
import pytest

from backend import job_queue
from backend.job_heartbeat import heartbeat_connector
from backend.job_runner import ServerWorker, StepResult
from backend.routers import datasets
from backend.tests import test_field_registry_postgres

registry_database = test_field_registry_postgres.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def make_worker(database, handlers, **options):
    namespace = await database.fetchval('SELECT current_schema()')
    return ServerWorker(
        database.pool,
        handlers,
        heartbeat_connect=heartbeat_connector(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings={'search_path': namespace + ',public'},
        ),
        **options,
    )


async def job_state(database, job):
    return dict(await database.fetchrow('SELECT * FROM jobs WHERE id = $1', job['id']))


@pytest.mark.parametrize(
    'options',
    [
        {'lease_seconds': 0},
        {'poll_seconds': float('nan')},
        {'retry_seconds': float('inf')},
        {'step_budget_seconds': -1},
        {'lease_seconds': 1, 'heartbeat_seconds': 0.5},
        {'lease_seconds': 600, 'heartbeat_seconds': 30},
    ],
)
def test_worker_rejects_unbounded_or_unhealthy_timing_configuration(registry_database, options):
    async def scenario(database):
        async def handler(job):
            return StepResult(finished=True)

        with pytest.raises(ValueError):
            await make_worker(database, {'example': handler}, **options)

    registry_database(scenario)


def test_worker_renews_lease_during_long_unit_without_holding_job_lock(registry_database):
    async def scenario(database):
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(job):
            started.set()
            await release.wait()
            return StepResult(finished=True, result={'answer': 42})

        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(database, {'example': handler}, lease_seconds=0.8, heartbeat_seconds=0.1)
        running = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(started.wait(), 3)
            first_lease = (await job_state(database, job))['lease_expires_at']
            # Free between the lease thread's brief renewals: never held across the unit.
            for attempt in range(50):
                try:
                    async with database.pool.acquire() as connection, connection.transaction():
                        await connection.fetchrow('SELECT id FROM jobs WHERE id = $1 FOR UPDATE NOWAIT', job['id'])
                    break
                except asyncpg.LockNotAvailableError:
                    assert attempt < 49, 'The worker held the job row lock throughout its unit'
                    await asyncio.sleep(0.01)
            await asyncio.sleep(1)
            latest = await job_state(database, job)
            assert latest['lease_expires_at'] > first_lease
            assert latest['state'] == 'running'
            heartbeat = await database.fetchrow('SELECT * FROM workers WHERE id = $1', worker.worker_id)
            assert heartbeat['current_job_id'] == job['id']
            assert heartbeat['capabilities']['kinds'] == ['example']
        finally:
            release.set()
            await asyncio.wait_for(running, 3)
        assert (await job_state(database, job))['result'] == {'answer': 42}

    registry_database(scenario)


def test_heartbeat_contention_with_own_publication_does_not_abandon_valid_lease(registry_database):
    async def scenario(database):
        async def handler(job):
            unit = job['checkpoint'].get('unit', 0) + 1

            async def publish(connection):
                if unit == 1:
                    await connection.execute('SELECT pg_sleep(0.18)')

            return StepResult(checkpoint={'unit': unit}, done=unit, total=2, finished=unit == 2, publish=publish)

        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(database, {'example': handler}, lease_seconds=0.8, heartbeat_seconds=0.03)
        await worker.run_once()
        final = await job_state(database, job)
        assert final['state'] == 'succeeded'
        assert final['checkpoint'] == {'unit': 2}

    registry_database(scenario)


def test_worker_telemetry_contention_does_not_stop_lease_renewal(registry_database):
    async def scenario(database):
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(job):
            started.set()
            await release.wait()
            return StepResult(finished=True)

        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(database, {'example': handler}, lease_seconds=0.8, heartbeat_seconds=0.1)
        running = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(started.wait(), 3)
            first_lease = (await job_state(database, job))['lease_expires_at']
            async with database.pool.acquire() as connection, connection.transaction():
                await connection.fetchrow('SELECT id FROM workers WHERE id = $1 FOR UPDATE', worker.worker_id)
                await asyncio.sleep(1)
                assert (await job_state(database, job))['lease_expires_at'] > first_lease
        finally:
            release.set()
            await asyncio.wait_for(running, 3)
        assert (await job_state(database, job))['state'] == 'succeeded'

    registry_database(scenario)


def test_consumer_recovers_after_idle_poll_and_iteration_failure(registry_database, monkeypatch):
    async def scenario(database):
        calls = []

        async def handler(job):
            return StepResult(finished=True)

        worker = await make_worker(database, {'example': handler}, poll_seconds=0.001)

        async def iterate():
            calls.append(True)
            if len(calls) == 2:
                raise ConnectionError('temporary database outage')
            if len(calls) == 3:
                worker.stop()
            return False

        monkeypatch.setattr(worker, 'run_once', iterate)
        await asyncio.wait_for(worker.run(), 1)
        assert len(calls) == 3

    registry_database(scenario)


def test_time_slice_finishes_unit_then_yields_to_another_dataset(registry_database):
    async def scenario(database):
        visited = []

        async def handler(job):
            visited.append(job['dataset_id'])
            unit = job['checkpoint'].get('unit', 0) + 1
            await asyncio.sleep(0.01)
            return StepResult(checkpoint={'unit': unit}, done=unit, total=2, finished=unit == 2)

        first = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        second = await job_queue.enqueue_job(database.pool, 'example', dataset_id=2)
        worker = await make_worker(database, {'example': handler}, step_budget_seconds=0.001)
        assert await worker.run_once()
        assert (await job_state(database, first))['checkpoint'] == {'unit': 1}
        assert (await job_state(database, first))['state'] == 'queued'
        assert await worker.run_once()
        assert (await job_state(database, second))['checkpoint'] == {'unit': 1}
        assert visited == [1, 2]
        assert await worker.run_once()
        assert (await job_state(database, first))['state'] == 'succeeded'
        assert await worker.run_once()
        assert (await job_state(database, second))['state'] == 'succeeded'

    registry_database(scenario)


def test_timed_out_unit_keeps_checkpoint_and_does_not_starve_other_work(registry_database):
    async def scenario(database):
        async def handler(job):
            if job['dataset_id'] == 2:
                return StepResult(finished=True, result='small dataset done')
            if job['checkpoint'].get('unit') == 1:
                raise TimeoutError('one expensive unit exceeded its statement budget')
            return StepResult(checkpoint={'unit': 1}, done=1, total=2)

        slow = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        small = await job_queue.enqueue_job(database.pool, 'example', dataset_id=2)
        worker = await make_worker(database, {'example': handler})
        await worker.run_once()
        deferred = await job_state(database, slow)
        assert deferred['state'] == 'queued'
        assert deferred['checkpoint'] == {'unit': 1}
        assert 'expensive unit' in deferred['error']['message']
        await worker.run_once()
        assert (await job_state(database, small))['state'] == 'succeeded'

    registry_database(scenario)


def test_failed_handler_exposes_error_and_next_job_still_runs(registry_database):
    async def scenario(database):
        async def handler(job):
            if job['dataset_id'] == 1:
                raise ValueError('unsupported test input')
            return StepResult(finished=True, result={'success': True})

        failing = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        following = await job_queue.enqueue_job(database.pool, 'example', dataset_id=2)
        worker = await make_worker(database, {'example': handler})
        await worker.run_once()
        failed = await job_state(database, failing)
        assert failed['state'] == 'failed'
        assert failed['error'] == {'type': 'ValueError', 'message': 'unsupported test input'}
        await worker.run_once()
        assert (await job_state(database, following))['state'] == 'succeeded'

    registry_database(scenario)


@pytest.mark.parametrize('action', ['cancel', 'stop', 'expire'])
def test_inflight_unit_obeys_cancellation_shutdown_and_lease_loss(registry_database, action):
    async def scenario(database):
        started = asyncio.Event()
        release = asyncio.Event()
        published = []

        async def publish(connection):
            published.append('published')

        async def handler(job):
            started.set()
            await release.wait()
            return StepResult(checkpoint={'unit': 1}, done=1, total=2, publish=publish)

        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(database, {'example': handler})
        running = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(started.wait(), 3)
            if action == 'cancel':
                await job_queue.cancel_job(database.pool, job['id'])
            elif action == 'stop':
                worker.stop()
            else:
                await database.execute(
                    "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = $1",
                    job['id'],
                )
            release.set()
            await asyncio.wait_for(running, 3)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        state = await job_state(database, job)
        if action == 'stop':
            assert published == ['published']
            assert state['state'] == 'queued' and state['checkpoint'] == {'unit': 1}
        else:
            assert published == []
            assert state['checkpoint'] == {}
            assert state['state'] == ('cancelled' if action == 'cancel' else 'running')

    registry_database(scenario)


def test_server_worker_never_claims_compute_or_unknown_kinds(registry_database):
    async def scenario(database):
        async def handler(job):
            raise AssertionError('No compatible server job exists')

        compute = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1, placement='compute')
        unsupported = await job_queue.enqueue_job(database.pool, 'not_registered', dataset_id=2)
        worker = await make_worker(database, {'example': handler})
        assert not await worker.run_once()
        assert (await job_state(database, compute))['state'] == 'queued'
        assert (await job_state(database, unsupported))['state'] == 'queued'

    registry_database(scenario)


@pytest.mark.parametrize('has_follower', [False, True])
def test_killed_worker_resumes_committed_checkpoint_without_duplicate_effects(registry_database, has_follower):
    async def scenario(database):
        await database.execute('CREATE TABLE job_effects (unit INTEGER PRIMARY KEY)')
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        follower = await job_queue.enqueue_job(database.pool, 'following', dataset_id=1) if has_follower else None
        namespace = await database.fetchval('SELECT current_schema()')
        program = '''
import asyncio
import sys
import asyncpg
from backend.database import init_connection
from backend.job_heartbeat import heartbeat_connector
from backend.job_runner import ServerWorker, StepResult

async def main():
    pool = await asyncpg.create_pool(sys.argv[1], min_size=1, max_size=3,
        init=init_connection, server_settings={'search_path': sys.argv[2] + ',public'})
    async def handler(job):
        if job['checkpoint'].get('unit') == 1:
            await asyncio.sleep(60)
        async def publish(connection):
            await connection.execute('INSERT INTO job_effects VALUES (1) ON CONFLICT DO NOTHING')
        return StepResult(checkpoint={'unit': 1}, done=1, total=2, publish=publish)
    worker = ServerWorker(pool, {'example': handler}, lease_seconds=0.8, heartbeat_seconds=0.1,
        retry_seconds=0.01, heartbeat_connect=heartbeat_connector(sys.argv[1],
        server_settings={'search_path': sys.argv[2] + ',public'}))
    await worker.run_once()
    await pool.close()

asyncio.run(main())
'''
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            '-c',
            program,
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            namespace,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        async def checkpoint_committed():
            while True:
                if (await job_state(database, job))['checkpoint'] == {'unit': 1}:
                    return
                if process.returncode is not None:
                    assert process.stderr is not None
                    raise AssertionError((await process.stderr.read()).decode())
                await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(checkpoint_committed(), 5)
            inserted = await asyncio.wait_for(
                test_field_registry_postgres.ingest([{'arrival': number} for number in range(128)]),
                2,
            )
            await asyncio.wait_for(
                datasets.update_sample(
                    1,
                    inserted['ids'][0],
                    datasets.SampleUpdate(metadata={'reviewed': True}),
                    {'id': 1, 'role': 'admin'},
                ),
                2,
            )
            assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id=1') == 128
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        await asyncio.sleep(1)

        async def resumed(job):
            assert job['checkpoint'] == {'unit': 1}

            async def publish(connection):
                await connection.execute('INSERT INTO job_effects VALUES (2) ON CONFLICT DO NOTHING')

            return StepResult(checkpoint={'unit': 2}, done=2, total=2, finished=True, publish=publish)

        async def following(job):
            assert await database.fetchval('SELECT count(*) FROM job_effects') == 2

            async def publish(connection):
                await connection.execute('INSERT INTO job_effects VALUES (3)')

            return StepResult(finished=True, publish=publish)

        worker = await make_worker(database, {'example': resumed, 'following': following})
        assert await worker.run_once()
        assert (await job_state(database, job))['state'] == 'succeeded'
        assert [row['unit'] for row in await database.fetch('SELECT unit FROM job_effects ORDER BY unit')] == [1, 2]
        if follower is not None:
            assert (await job_state(database, follower))['state'] == 'queued'
            assert await worker.run_once()
            assert (await job_state(database, follower))['state'] == 'succeeded'
            effects = await database.fetch('SELECT unit FROM job_effects ORDER BY unit')
            assert [row['unit'] for row in effects] == [1, 2, 3]

    registry_database(scenario)
