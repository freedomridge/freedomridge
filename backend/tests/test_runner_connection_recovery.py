"""A failed lease session must not poison future jobs or end a healthy slot."""

import asyncio
import os
import threading
import time

import asyncpg
import pytest

from backend import job_heartbeat, job_queue, job_runner
from backend.job_runner import StepResult
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_job_launch import wait_until
from backend.tests.test_job_runner import make_worker

registry_database = registry_helpers.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an explicitly supplied disposable PostgreSQL database',
)


def test_session_shutdown_is_bounded_when_its_thread_is_stalled(monkeypatch):
    async def scenario():
        async def connect():
            raise AssertionError('No connection is needed for this shutdown test')

        monkeypatch.setattr(job_heartbeat, 'LEASE_SHUTDOWN_SECONDS', 0.1, raising=False)
        session = job_heartbeat.LeaseSession(connect, 'shutdown-review')
        entered = threading.Event()
        release = threading.Event()

        def stall():
            entered.set()
            release.wait(1.5)

        await session.start()
        assert session.loop is not None
        session.loop.call_soon_threadsafe(stall)
        await asyncio.to_thread(entered.wait, 1)
        try:
            started = time.monotonic()
            await session.close()
            assert time.monotonic() - started < 0.7
        finally:
            release.set()
            await asyncio.to_thread(session.thread.join, 3)
        assert not session.thread.is_alive()

    asyncio.run(scenario())


@pytest.mark.parametrize('lease,heartbeat', [(60, 10), (0.8, 0.1), (6, 1)])
def test_budgets_include_connect_query_and_retry_delay(lease, heartbeat):
    renewal, telemetry = job_heartbeat.heartbeat_budgets(lease, heartbeat)
    assert heartbeat + 4 * renewal + telemetry + min(heartbeat, 1) < lease


def test_budget_rejects_settings_without_room_for_two_complete_attempts():
    with pytest.raises(ValueError, match='two bounded renewal attempts'):
        job_heartbeat.heartbeat_budgets(12, 4)


def test_lease_connection_has_idle_transaction_limit_before_begin(registry_database):
    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        settings = {'search_path': namespace + ',public'}
        connection = await job_heartbeat.heartbeat_connector(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings=settings,
        )()
        try:
            assert await connection.fetchval(
                "SELECT current_setting('idle_in_transaction_session_timeout')::interval = "
                "$1::float8 * interval '1 millisecond'",
                job_queue.IDLE_TRANSACTION_TIMEOUT_MS,
            )
            assert await connection.fetchval('SELECT current_schema()') == namespace
            assert settings == {'search_path': namespace + ',public'}
        finally:
            await connection.close()

    registry_database(scenario)


@pytest.mark.parametrize('phase,failure', [('renewal', 'timeout'), ('telemetry', 'timeout'), ('telemetry', 'cancel')])
def test_cancel_during_begin_discards_session_and_later_jobs_commit(registry_database, monkeypatch, phase, failure):
    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        connector = job_heartbeat.heartbeat_connector(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings={'search_path': namespace + ',public'},
        )
        connections: list[asyncpg.Connection] = []
        injected = threading.Event()
        operation = threading.local()
        original_execute = asyncpg.Connection.execute
        original_renewal = job_queue.heartbeat_job
        original_telemetry = job_heartbeat.worker_heartbeat

        async def connect():
            connection = await connector()
            connections.append(connection)
            return connection

        async def renewal(*args, **kwargs):
            operation.phase = 'renewal'
            try:
                return await original_renewal(*args, **kwargs)
            finally:
                operation.phase = None

        async def telemetry(*args, **kwargs):
            operation.phase = 'telemetry'
            try:
                return await original_telemetry(*args, **kwargs)
            finally:
                operation.phase = None

        async def execute(connection, query, *args, **kwargs):
            response = await original_execute(connection, query, *args, **kwargs)
            if (
                query.startswith('BEGIN')
                and getattr(operation, 'phase', None) == phase
                and threading.current_thread().name.startswith('lease:')
                and not injected.is_set()
            ):
                injected.set()
                if failure == 'cancel':
                    raise asyncio.CancelledError('unexpected telemetry cancellation')
                await asyncio.Event().wait()
            return response

        monkeypatch.setattr(asyncpg.Connection, 'execute', execute)
        monkeypatch.setattr(job_queue, 'heartbeat_job', renewal)
        monkeypatch.setattr(job_heartbeat, 'worker_heartbeat', telemetry)

        async def handler(job):
            await asyncio.sleep(0.3)
            return StepResult(finished=True, result=job['args']['value'])

        jobs = [await job_queue.enqueue_job(database.pool, 'example', args={'value': value}) for value in (1, 2)]
        # Each lease step (connect, renewal, telemetry) gets lease_seconds / 8, capped at 1 s for telemetry.
        # At 0.8 s that was 100 ms, which a busy machine's reconnect or commit can outlast: the session was
        # then discarded a second time, opening a third connection. 8 s gives every step 1 s.
        consumer = job_runner.ServerWorker(
            database.pool,
            {'example': handler},
            heartbeat_connect=connect,
            lease_seconds=8,
            heartbeat_seconds=0.05,
            poll_seconds=0.01,
        )
        running = asyncio.create_task(consumer.run())
        try:

            async def completed():
                return await database.fetchval("SELECT count(*) FROM jobs WHERE state='succeeded'") == 2

            await wait_until(completed, timeout=15)
            assert injected.is_set()
            assert len(connections) == 2
            assert connections[0].is_closed()
            for job in jobs:
                saved = await database.fetchrow('SELECT state,result,attempt,failures FROM jobs WHERE id=$1', job['id'])
                assert saved['result'] == job['args']['value']
                assert saved['attempt'] == 1 and saved['failures'] == 0
        finally:
            consumer.stop()
            await asyncio.wait_for(running, 3)
        assert all(connection.is_closed() for connection in connections)

    registry_database(scenario)


def test_reusable_session_rejects_an_open_transaction_before_next_job(registry_database):
    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        session = job_heartbeat.LeaseSession(
            job_heartbeat.heartbeat_connector(
                os.environ['FRIDGE_TEST_POSTGRES_DSN'],
                server_settings={'search_path': namespace + ',public'},
            ),
            'recovery-test',
        )
        await session.start()

        async def poison_and_reconnect():
            original = await session.connect()
            await original.execute('BEGIN')
            assert original.is_in_transaction()
            replacement = await session.connect()
            assert replacement is not original
            assert original.is_closed()
            assert not replacement.is_in_transaction()

        try:
            await asyncio.wait_for(asyncio.wrap_future(session.submit(poison_and_reconnect())), 2)
        finally:
            await session.close()

    registry_database(scenario)


def test_stray_publication_cancellation_fails_job_without_ending_slot(registry_database):
    async def scenario(database):
        failed = await job_queue.enqueue_job(database.pool, 'example', args={'cancel_publish': True})
        following = await job_queue.enqueue_job(database.pool, 'example')

        async def handler(job):
            async def publish(connection):
                if job['args'].get('cancel_publish'):
                    await connection.execute("UPDATE datasets SET description='uncommitted' WHERE id=1")
                    raise asyncio.CancelledError('unexpected publication cancellation')

            return StepResult(finished=True, publish=publish)

        consumer = await make_worker(database, {'example': handler}, poll_seconds=0.01)
        execution = asyncio.create_task(consumer.run())
        try:

            async def completed():
                if execution.done():
                    await execution
                return await database.fetchval('SELECT state FROM jobs WHERE id=$1', following['id']) == 'succeeded'

            await wait_until(completed, timeout=2)
            row = await database.fetchrow('SELECT state,error FROM jobs WHERE id=$1', failed['id'])
            assert row['state'] == 'failed'
            assert row['error']['type'] == 'RuntimeError'
            assert 'publication' in row['error']['message'].lower()
            assert await database.fetchval('SELECT description FROM datasets WHERE id=1') != 'uncommitted'
        finally:
            consumer.stop()
            await asyncio.wait_for(execution, 3)

    registry_database(scenario)


@pytest.mark.parametrize('phase', ['claim', 'telemetry'])
def test_stray_housekeeping_cancellation_does_not_end_slot(registry_database, monkeypatch, phase):
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, 'example')

        async def handler(job):
            return StepResult(finished=True)

        consumer = await make_worker(database, {'example': handler}, poll_seconds=0.01)
        source, name = (job_queue, 'claim_job') if phase == 'claim' else (consumer, 'heartbeat')
        original = getattr(source, name)
        calls = []

        async def interrupted(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise asyncio.CancelledError('unexpected housekeeping cancellation')
            return await original(*args, **kwargs)

        monkeypatch.setattr(source, name, interrupted)
        execution = asyncio.create_task(consumer.run())
        try:

            async def completed():
                if execution.done():
                    await execution
                return (
                    len(calls) >= 2
                    and await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'succeeded'
                )

            await wait_until(completed, timeout=2)
            assert not execution.done()
        finally:
            consumer.stop()
            await asyncio.wait_for(execution, 3)

    registry_database(scenario)


@pytest.mark.parametrize('close_connection', [False, True])
def test_external_publication_cancellation_still_rolls_back_and_stops_slot(registry_database, close_connection):
    async def scenario(database):
        entered = asyncio.Event()
        job = await job_queue.enqueue_job(database.pool, 'example')

        async def handler(job):
            async def publish(connection):
                await connection.execute("UPDATE datasets SET description='uncommitted' WHERE id=1")
                if close_connection:
                    connection.terminate()
                entered.set()
                await asyncio.Event().wait()

            return StepResult(finished=True, publish=publish)

        consumer = await make_worker(database, {'example': handler})
        execution = asyncio.create_task(consumer.run())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(execution, 3)
            assert await database.fetchval('SELECT description FROM datasets WHERE id=1') != 'uncommitted'
            assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'running'
            assert consumer.lease_session is None
        finally:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)

    registry_database(scenario)


def test_deadline_closes_socket_before_waiting_for_transaction_cancellation(registry_database, monkeypatch):
    async def scenario(database):
        original = job_queue.heartbeat_job
        connections: list[asyncpg.Connection] = []

        async def stalled(connection, *args, **kwargs):
            if not connections:
                connections.append(connection)
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    while not connection.is_closed():
                        await asyncio.sleep(0.01)
                    raise
            return await original(connection, *args, **kwargs)

        monkeypatch.setattr(job_queue, 'heartbeat_job', stalled)

        async def handler(job):
            return StepResult(finished=True)

        job = await job_queue.enqueue_job(database.pool, 'example')
        consumer = await make_worker(database, {'example': handler}, lease_seconds=0.8, heartbeat_seconds=0.05)
        execution = asyncio.create_task(consumer.run_once())
        try:
            completed, _ = await asyncio.wait({execution}, timeout=2)
            assert completed, 'Renewal timeout must not wait indefinitely for transaction cleanup'
            await execution
            assert connections[0].is_closed()
            assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'succeeded'
        finally:
            if consumer.lease_session is not None:
                for connection in connections:
                    consumer.lease_session.loop.call_soon_threadsafe(connection.terminate)
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)

    registry_database(scenario)
