"""Part A regression checks: liveness as well as fenced effects."""

import asyncio
import inspect
import os
import signal
import sys
import threading
import time
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest

from backend import job_queue, job_runner
from backend.database import init_connection
from backend.job_runner import StepResult
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database


def present(value):
    """The row a call must return (claims, cancels): fail clearly instead of indexing None."""
    assert value is not None
    return value


pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def worker(database, handler, **options):
    if 'heartbeat_connect' in inspect.signature(job_runner.ServerWorker).parameters:
        from backend.job_heartbeat import heartbeat_connector

        namespace = await database.fetchval('SELECT current_schema()')
        options['heartbeat_connect'] = heartbeat_connector(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings={'search_path': namespace + ',public'},
        )
    return job_runner.ServerWorker(
        database.pool,
        {'example': handler},
        lease_seconds=0.8,
        heartbeat_seconds=0.05,
        **options,
    )


async def state(database, job):
    return dict(await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id']))


def test_cancel_interrupts_hung_measurement_without_publication(registry_database):
    async def scenario(database):
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def handler(job):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            raise AssertionError('Unreachable')

        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        consumer = await worker(database, handler)
        execution = asyncio.create_task(consumer.run_once())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await job_queue.cancel_job(database.pool, job['id'])
            await asyncio.wait_for(stopped.wait(), 1)
            await asyncio.wait_for(execution, 1)
            assert (await state(database, job))['state'] == 'cancelled'
            assert (await state(database, job))['checkpoint'] == {}
        finally:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)

    registry_database(scenario)


def test_unit_has_a_per_kind_wall_time_limit(registry_database):
    async def scenario(database):
        async def handler(job):
            await asyncio.Event().wait()

        policy = job_queue.JobPolicy(max_unit_seconds=0.12, max_failures=1)
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        consumer = await worker(database, handler, policies={'example': policy})
        await asyncio.wait_for(consumer.run_once(), 2)
        final = await state(database, job)
        assert final['state'] == 'failed'
        assert final['error']['type'] == 'TimeoutError'
        assert 'wall-time' in final['error']['message']

    registry_database(scenario)


def test_sigstop_publication_loses_database_lock_and_rolls_back(registry_database):
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        namespace = await database.fetchval('SELECT current_schema()')
        program = """
import asyncio, sys
import asyncpg
from backend import job_queue
from backend.database import init_connection
job_queue.IDLE_TRANSACTION_TIMEOUT_MS = 1000  # the mechanism, without waiting out the production value

async def main():
    pool = await asyncpg.create_pool(sys.argv[1], min_size=1, max_size=2,
        init=init_connection, server_settings={'search_path':sys.argv[2]+',public'})
    claimed = await job_queue.claim_job(pool, 'stopped', lease_seconds=0.5)
    async def publish(connection):
        await connection.execute("UPDATE datasets SET description='uncommitted' WHERE id=1")
        print('locked', flush=True)
        await asyncio.sleep(60)
    await job_queue.publish_step(pool, claimed['id'], claimed['lease_token'], publisher=publish)
asyncio.run(main())
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            '-c',
            program,
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            namespace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        try:
            assert await asyncio.wait_for(process.stdout.readline(), 5) == b'locked\n'
            os.kill(process.pid, signal.SIGSTOP)
            await asyncio.sleep(3.3)
            async with database.pool.acquire() as connection, connection.transaction():
                await connection.execute("SET LOCAL lock_timeout='300ms'")
                await connection.fetchrow('SELECT id FROM jobs WHERE id=$1 FOR UPDATE', job['id'])
            assert await database.fetchval('SELECT description FROM datasets WHERE id=1') != 'uncommitted'
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    registry_database(scenario)


def test_slots_keep_user_work_moving_during_maintenance(registry_database):
    async def scenario(database):
        started = asyncio.Event()
        completed = asyncio.Event()
        release = asyncio.Event()

        async def handler(job):
            if job['lane'] == 'maintenance':
                started.set()
                await release.wait()
            else:
                completed.set()
            return StepResult(finished=True)

        first = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1, lane='maintenance')
        await job_queue.enqueue_job(database.pool, 'example', dataset_id=2, lane='maintenance')
        template = await worker(database, handler)
        runner = job_runner.JobRunner(
            database.pool,
            {'example': handler},
            heartbeat_connect=template.heartbeat_connect,
            slots=2,
            poll_seconds=0.01,
        )
        execution = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(started.wait(), 2)
            await job_queue.enqueue_job(database.pool, 'example', dataset_id=1, lane='user')
            await asyncio.wait_for(completed.wait(), 2)
            assert (await state(database, first))['state'] == 'running'
            identifiers = await database.fetch("SELECT DISTINCT id FROM workers WHERE current_job_id IS NOT NULL")
            assert identifiers
        finally:
            runner.stop()
            release.set()
            await asyncio.wait_for(execution, 3)

    registry_database(scenario)


def test_retries_back_off_and_reach_failure_limit(registry_database):
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        policy = job_queue.JobPolicy(retry_seconds=2, max_retry_seconds=3, max_failures=3)
        delays = []
        for failure in range(1, 4):
            claim = present(await job_queue.claim_job(database.pool, 'worker', policies={'example': policy}))
            result = await job_queue.defer_job(
                database.pool,
                claim['id'],
                claim['lease_token'],
                {'message': 'temporary'},
                delay_seconds=2,
            )
            assert result['failures'] == failure
            delays.append((result['available_at'] - result['updated_at']).total_seconds())
            await database.execute("UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1", job['id'])
        assert delays == pytest.approx([2, 3, 3], abs=0.01)
        assert (await state(database, job))['state'] == 'failed'
        assert await job_queue.claim_job(database.pool, 'worker') is None

    registry_database(scenario)


def test_crash_failures_are_bounded_but_fair_yields_are_not_failures(registry_database):
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        policy = job_queue.JobPolicy(max_failures=2)
        for unit in range(12):
            claim = present(await job_queue.claim_job(database.pool, 'worker', policies={'example': policy}))
            await job_queue.publish_step(
                database.pool,
                job['id'],
                claim['lease_token'],
                checkpoint={'unit': unit},
                release=True,
            )
        assert (await state(database, job))['failures'] == 0
        for crash in range(2):
            claim = present(await job_queue.claim_job(database.pool, 'worker', policies={'example': policy}))
            assert claim is not None
            await database.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
                job['id'],
            )
            assert await job_queue.claim_job(database.pool, 'reaper', kinds=['different']) is None
            current = await state(database, job)
            assert current['failures'] == crash + 1
            if crash == 0:
                assert current['available_at'] > current['updated_at']
                await database.execute("UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1", job['id'])
        assert (await state(database, job))['state'] == 'failed'
        assert (await state(database, job))['error']['type'] == 'LeaseExpired'

    registry_database(scenario)


@pytest.mark.parametrize(
    'error_type',
    [asyncpg.ConnectionDoesNotExistError, asyncpg.TooManyConnectionsError, asyncpg.AdminShutdownError],
)
def test_connection_and_resource_errors_defer_instead_of_permanent_failure(registry_database, error_type):
    async def scenario(database):
        async def handler(job):
            raise error_type('temporary outage')

        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        await (await worker(database, handler)).run_once()
        final = await state(database, job)
        assert final['state'] == 'queued'
        assert final['error']['type'] == error_type.__name__
        assert final['failures'] == 1

    registry_database(scenario)


@pytest.mark.parametrize('publication_delay', [0, 0.35])
def test_heartbeat_retries_resource_failure_without_abandoning_claim(
    registry_database,
    monkeypatch,
    publication_delay,
):
    async def scenario(database):
        original = job_queue.heartbeat_job
        calls = []
        recovered = threading.Event()
        holders: list[asyncio.Task] = []

        async def flaky(*args, **kwargs):
            calls.append(args[2])
            if len(calls) == 2:
                raise asyncpg.TooManyConnectionsError('temporary capacity')
            renewed = await original(*args, **kwargs)
            if len(calls) > 2 and renewed is not None and args[2] == calls[0]:
                recovered.set()
            return renewed

        monkeypatch.setattr(job_queue, 'heartbeat_job', flaky)

        async def handler(job):
            await asyncio.sleep(1.1)
            assert recovered.is_set(), 'The original lease was not renewed after the resource error'
            if publication_delay and not holders:
                locked = asyncio.Event()

                async def hold_row():
                    async with database.pool.acquire() as connection, connection.transaction():
                        await connection.fetchrow('SELECT id FROM jobs WHERE id=$1 FOR UPDATE', job['id'])
                        locked.set()
                        await asyncio.sleep(publication_delay)

                holders.append(asyncio.create_task(hold_row()))
                await locked.wait()
            return StepResult(finished=True, result=17)

        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        consumer = await worker(database, handler, poll_seconds=0.01, retry_seconds=0.02)
        execution = asyncio.create_task(consumer.run())
        try:

            async def completed():
                while True:
                    final = await state(database, job)
                    assert final['state'] not in ('failed', 'cancelled'), final
                    if final['state'] == 'succeeded':
                        return final
                    assert not execution.done(), 'The worker stopped before finishing the job'
                    await asyncio.sleep(0.02)

            final = await asyncio.wait_for(completed(), 8)
            assert len(calls) > 3 and recovered.is_set()
            assert final['result'] == 17
            if publication_delay:
                assert final['attempt'] > 1
        finally:
            consumer.stop()
            try:
                await asyncio.wait_for(execution, 3)
            finally:
                await asyncio.gather(*holders)

    registry_database(scenario)


def test_cancel_during_publication_is_durable_and_stops_at_next_boundary(registry_database):
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        claim = present(await job_queue.claim_job(database.pool, 'worker'))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def publish(connection):
            entered.set()
            await release.wait()
            await connection.execute("UPDATE datasets SET description='unit one' WHERE id=1")

        execution = asyncio.create_task(
            job_queue.publish_step(
                database.pool,
                job['id'],
                claim['lease_token'],
                publisher=publish,
                checkpoint={'unit': 1},
            ),
        )
        try:
            await entered.wait()
            cancelled = present(await asyncio.wait_for(job_queue.cancel_job(database.pool, job['id']), 0.5))
            assert cancelled['cancel_requested'] and cancelled['state'] == 'running'
            release.set()
            result = await execution
            assert result['state'] == 'cancelled'
            assert result['checkpoint'] == {'unit': 1}
            assert await database.fetchval('SELECT description FROM datasets WHERE id=1') == 'unit one'
        finally:
            release.set()
            await asyncio.gather(execution, return_exceptions=True)

    registry_database(scenario)


def test_running_resumed_cancelled_and_distinct_barriers_do_not_join(registry_database):
    async def scenario(database):
        arguments: dict[str, Any] = {'dataset_id': 1, 'dedupe_key': 'refresh:1'}
        first = await job_queue.enqueue_job(database.pool, 'example', **arguments)
        claim = present(await job_queue.claim_job(database.pool, 'worker'))
        following = await job_queue.enqueue_job(database.pool, 'example', **arguments)
        assert first['id'] != following['id']
        await job_queue.publish_step(database.pool, first['id'], claim['lease_token'], release=True)
        assert (await job_queue.enqueue_job(database.pool, 'example', **arguments))['id'] == following['id']
        await job_queue.cancel_job(database.pool, following['id'])
        newest = await job_queue.enqueue_job(database.pool, 'example', **arguments, barrier={'visible_work': ['first']})
        another = await job_queue.enqueue_job(
            database.pool,
            'example',
            **arguments,
            barrier={'visible_work': ['later']},
        )
        assert len({first['id'], following['id'], newest['id'], another['id']}) == 4
        assert newest['args'] == another['args'] == {}

    registry_database(scenario)


def test_dedupe_ownership_survives_submitter_deletion(registry_database):
    async def scenario(database):
        await database.execute(
            "INSERT INTO users(id,username,role) VALUES (10,'one','power_user'),(11,'two','power_user')",
        )
        arguments: dict[str, Any] = {'dataset_id': 1, 'dedupe_key': 'shared'}
        first = await job_queue.enqueue_job(database.pool, 'example', **arguments, created_by=10)
        second = await job_queue.enqueue_job(database.pool, 'example', **arguments, created_by=11)
        await database.execute('DELETE FROM users WHERE id IN (10,11)')
        system = await job_queue.enqueue_job(database.pool, 'example', **arguments)
        assert len({first['id'], second['id'], system['id']}) == 3

    registry_database(scenario)


@pytest.mark.parametrize('stress', ['blocking_cpu', 'full_pool'])
def test_lease_survives_blocked_handler_loop_and_exhausted_pool(registry_database, stress):
    async def scenario(database):
        async def handler(job):
            if stress == 'blocking_cpu':
                time.sleep(1.1)
            else:
                held = [await database.pool.acquire() for connection_number in range(database.pool.get_max_size())]
                try:
                    await asyncio.sleep(1.1)
                finally:
                    for connection in held:
                        await database.pool.release(connection)
            return StepResult(finished=True, result='survived')

        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        await (await worker(database, handler)).run_once()
        assert (await state(database, job))['result'] == 'survived'

    registry_database(scenario)


def test_local_pool_budget_does_not_inherit_api_fifty_connections(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setenv('DB_POOL_MAX_SIZE', '50')
        monkeypatch.delenv('JOB_POOL_MAX_SIZE', raising=False)
        monkeypatch.delenv('JOB_WORKER_SLOTS', raising=False)
        namespace = await database.fetchval('SELECT current_schema()')
        async with job_runner.local_runner(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            {'example': AsyncMock(return_value=StepResult(finished=True))},
            server_settings={'search_path': namespace + ',public'},
        ) as runner:
            assert len(runner.workers) == 3
            # Six for the three slots' work, plus one that restore recovery can hold for a whole rewrite.
            assert runner.workers[0].pool.get_max_size() == 7
            assert len({consumer.worker_id for consumer in runner.workers}) == 3

    registry_database(scenario)


def test_compute_and_server_share_lane_but_maintenance_does_not_block_user(registry_database):
    async def scenario(database):
        maintenance = await job_queue.enqueue_job(
            database.pool,
            'example',
            dataset_id=1,
            lane='maintenance',
            placement='compute',
        )
        await job_queue.enqueue_job(database.pool, 'example', dataset_id=1, lane='maintenance')
        user = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1, lane='user')
        assert (
            present(await job_queue.claim_job(database.pool, 'remote', placements=['compute']))['id']
            == maintenance['id']
        )
        assert present(await job_queue.claim_job(database.pool, 'local'))['id'] == user['id']
        assert await job_queue.claim_job(database.pool, 'local') is None
        await database.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            maintenance['id'],
        )
        assert await job_queue.claim_job(database.pool, 'local') is None
        assert (await state(database, maintenance))['failures'] == 1
        await job_queue.cancel_job(database.pool, maintenance['id'])
        following = present(await job_queue.claim_job(database.pool, 'local'))
        assert following['lane'] == 'maintenance'

    registry_database(scenario)


def test_telemetry_failure_after_claim_and_omitted_checkpoint_are_harmless(registry_database, monkeypatch):
    async def scenario(database):
        async def handler(job):
            if job['checkpoint']:
                return StepResult(finished=True, result='done')
            return StepResult(checkpoint={'unit': 1})

        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        consumer = await worker(database, handler)
        monkeypatch.setattr(consumer, 'heartbeat', AsyncMock(side_effect=RuntimeError('telemetry unavailable')))
        await consumer.run_once()
        final = await state(database, job)
        assert final['state'] == 'succeeded'
        assert final['checkpoint'] == {'unit': 1}

    registry_database(scenario)


def test_workers_not_heard_from_for_a_day_are_pruned(registry_database):
    # Every runner start registers new worker ids; their rows used to accumulate forever.
    async def scenario(database):
        from backend.job_heartbeat import worker_heartbeat

        await database.execute(
            """INSERT INTO workers (id, placement, heartbeat_at) VALUES
               ('gone', 'server', clock_timestamp() - interval '2 days'),
               ('recent', 'server', clock_timestamp() - interval '1 hour')""",
        )
        await worker_heartbeat(database.pool, 'alive', {'example'})
        assert sorted(await database.fetchval('SELECT array_agg(id) FROM workers')) == ['alive', 'recent']

    registry_database(scenario)


def test_publication_may_use_its_whole_budget(registry_database):
    # The idle-transaction timeout (3 s) used to be shorter than the 5 s publication budget.
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        claim = present(await job_queue.claim_job(database.pool, 'worker', lease_seconds=30))

        async def publish(connection):
            await connection.execute("UPDATE datasets SET description='published' WHERE id=1")
            await asyncio.sleep(job_queue.PUBLISH_TIMEOUT_SECONDS - 1)

        done = await job_queue.publish_step(
            database.pool,
            job['id'],
            claim['lease_token'],
            finished=True,
            publisher=publish,
        )
        assert done['state'] == 'succeeded'
        assert await database.fetchval('SELECT description FROM datasets WHERE id=1') == 'published'

    registry_database(scenario)


def test_slot_blocking_the_loop_neither_fails_nor_leaks_another_slots_publication(registry_database, monkeypatch):
    # Slots share one event loop. A blocking unit in one slot left another slot's open publication idle until the
    # server ended its session: that job failed permanently and its pool connection was never returned.
    monkeypatch.setattr(job_queue, 'IDLE_TRANSACTION_TIMEOUT_MS', 300)

    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        pool = await asyncpg.create_pool(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            min_size=1,
            max_size=4,
            init=init_connection,
            server_settings={'search_path': namespace + ',public'},
        )
        blocking = asyncio.Event()
        publishing = asyncio.Event()

        async def handler(job):
            if job['lane'] == 'maintenance':
                blocking.set()
                await publishing.wait()
                time.sleep(1)  # CPU work done on the loop, against the handler rules
                return StepResult(finished=True)

            async def publish(connection):
                await connection.execute('SELECT 1')
                publishing.set()
                await asyncio.sleep(0.05)

            return StepResult(finished=True, publish=publish)

        await job_queue.enqueue_job(pool, 'example', dataset_id=1, lane='maintenance')
        template = await worker(database, handler)
        runner = job_runner.JobRunner(
            pool,
            {'example': handler},
            heartbeat_connect=template.heartbeat_connect,
            slots=2,
            lease_seconds=5,
            heartbeat_seconds=0.5,
            poll_seconds=0.01,
        )
        execution = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(blocking.wait(), 3)
            user = await job_queue.enqueue_job(pool, 'example', dataset_id=2, lane='user')
            for _ in range(100):
                current = await state(database, user)
                if current['state'] != 'running' and current['attempt'] > 0:
                    break
                await asyncio.sleep(0.05)
            assert current['state'] == 'queued', current['error']
            assert current['failures'] == 1
        finally:
            runner.stop()
            await asyncio.wait_for(execution, 5)
        await asyncio.wait_for(pool.close(), 5)

    registry_database(scenario)


def test_session_lost_inside_a_queue_transaction_is_transient(registry_database):
    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        pool = await asyncpg.create_pool(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            min_size=1,
            max_size=2,
            init=init_connection,
            server_settings={'search_path': namespace + ',public'},
        )
        try:
            with pytest.raises(Exception) as caught:
                async with job_queue._transaction(pool) as connection:
                    pid = await connection.fetchval('SELECT pg_backend_pid()')
                    await database.execute('SELECT pg_terminate_backend($1)', pid)
                    await connection.fetchval('SELECT 1')
            assert job_queue.is_transient(caught.value), repr(caught.value)
            assert not job_queue.is_transient(asyncpg.InterfaceError('the server expects 2 arguments, 1 was passed'))
            # The lost connection went back to the pool: it can still close.
            await asyncio.wait_for(pool.close(), 5)
        finally:
            pool.terminate()

    registry_database(scenario)


def test_failure_limit_counts_failures_without_progress(registry_database):
    # A job that keeps advancing between crashes must not die after max_failures crashes in total.
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        policy = job_queue.JobPolicy(max_failures=2)
        for unit in range(4):
            claim = present(await job_queue.claim_job(database.pool, 'worker', policies={'example': policy}))
            await job_queue.publish_step(database.pool, job['id'], claim['lease_token'], checkpoint={'unit': unit})
            await database.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
                job['id'],
            )
            assert await job_queue.claim_job(database.pool, 'reaper', kinds=['different']) is None
            current = await state(database, job)
            assert (current['state'], current['failures']) == ('queued', 1)
            await database.execute("UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1", job['id'])

    registry_database(scenario)


def test_republishing_the_same_checkpoint_is_not_progress(registry_database):
    # A handler that re-publishes its stored checkpoint and then always fails must still reach the limit.
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        policy = job_queue.JobPolicy(max_failures=2, retry_seconds=0.01, max_retry_seconds=0.01)
        for _ in range(2):
            claim = present(await job_queue.claim_job(database.pool, 'worker', policies={'example': policy}))
            await job_queue.publish_step(database.pool, job['id'], claim['lease_token'], checkpoint={'unit': 0})
            await job_queue.defer_job(
                database.pool,
                job['id'],
                claim['lease_token'],
                {'message': 'poison'},
                delay_seconds=0.01,
            )
            await database.execute("UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1", job['id'])
        current = await state(database, job)
        assert (current['state'], current['failures']) == ('failed', 2)

    registry_database(scenario)


def test_a_bug_raised_while_handling_a_timeout_is_not_transient():
    try:
        try:
            raise TimeoutError('slow')
        except TimeoutError:
            raise KeyError('missing')  # noqa: B904 - the implicit context is the point
    except KeyError as error:
        assert not job_queue.is_transient(error)
    try:
        raise job_queue.QueueBusy('busy') from asyncpg.LockNotAvailableError('locked')
    except job_queue.QueueBusy as error:
        assert job_queue.is_transient(error)
