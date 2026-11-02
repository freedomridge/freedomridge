"""A real silently dropped TCP flow must not trap publication rollback or slot shutdown."""

import asyncio
import os
from urllib.parse import urlsplit

import asyncpg
import pytest

from backend import job_queue
from backend.database import init_connection
from backend.job_heartbeat import heartbeat_connector
from backend.job_runner import ServerWorker, StepResult
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_job_launch import wait_until

registry_database = registry_helpers.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an explicitly supplied disposable PostgreSQL database',
)


def test_publication_timeout_discards_a_blackholed_connection_and_keeps_slot_alive(registry_database, monkeypatch):
    async def scenario(database):
        direct = os.environ['FRIDGE_TEST_POSTGRES_DSN']
        address = urlsplit(direct)
        namespace = await database.fetchval('SELECT current_schema()')
        settings = {'search_path': namespace + ',public'}
        flows = {}
        connections = set()
        await database.execute('CREATE TABLE publication_effects (job_id UUID PRIMARY KEY)')

        async def forward(reader, writer, dropped):
            while chunk := await reader.read(65536):
                if not dropped.is_set():
                    writer.write(chunk)
                    await writer.drain()

        async def accept(reader, writer):
            connection_task = asyncio.current_task()
            assert connection_task is not None
            connections.add(connection_task)
            upstream = None
            pumps = []
            try:
                upstream_reader, upstream = await asyncio.open_connection(address.hostname, address.port or 5432)
                port = writer.get_extra_info('peername')[1]
                dropped = asyncio.Event()
                flows[port] = dropped
                pumps = [
                    asyncio.create_task(forward(reader, upstream, dropped)),
                    asyncio.create_task(forward(upstream_reader, writer, dropped)),
                ]
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pump in pumps:
                    pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
                writer.close()
                if upstream is not None:
                    upstream.close()
                    await upstream.wait_closed()
                await writer.wait_closed()
                connections.discard(connection_task)

        proxy = await asyncio.start_server(accept, '127.0.0.1', 0)
        pool = await asyncpg.create_pool(
            direct,
            host='127.0.0.1',
            port=proxy.sockets[0].getsockname()[1],
            ssl=False,
            min_size=1,
            max_size=2,
            init=init_connection,
            server_settings=settings,
        )
        monkeypatch.setattr(job_queue, 'PUBLISH_TIMEOUT_SECONDS', 0.5)
        dropped_ports: list[int] = []

        async def handler(job):
            async def publish(connection):
                await connection.execute('INSERT INTO publication_effects VALUES ($1)', job['id'])
                if not dropped_ports:
                    port = connection._transport.get_extra_info('sockname')[1]
                    flows[port].set()
                    dropped_ports.append(port)
                await connection.execute('SELECT 1')

            return StepResult(finished=True, publish=publish)

        first = await job_queue.enqueue_job(database.pool, 'probe')
        second = await job_queue.enqueue_job(database.pool, 'probe')
        worker = ServerWorker(
            pool,
            {'probe': handler},
            heartbeat_connect=heartbeat_connector(direct, server_settings=settings),
            lease_seconds=2,
            heartbeat_seconds=0.1,
            poll_seconds=0.01,
            retry_seconds=0.01,
        )
        execution = asyncio.create_task(worker.run())
        try:

            async def completed():
                assert not execution.done(), 'The worker slot stopped after losing its publication connection'
                return await database.fetchval("SELECT count(*) FROM jobs WHERE state='succeeded'") == 2

            await wait_until(completed, timeout=4)
            assert len(dropped_ports) == 1
            assert await database.fetchval('SELECT count(*) FROM publication_effects') == 2
            assert await database.fetchval('SELECT attempt FROM jobs WHERE id=$1', first['id']) >= 2
            assert {row['job_id'] for row in await database.fetch('SELECT job_id FROM publication_effects')} == {
                first['id'],
                second['id'],
            }
            worker.stop()
            await asyncio.wait_for(execution, 2)
            assert worker.lease_session is None
        finally:
            worker.stop()
            if not execution.done():
                execution.cancel()
                pool.terminate()
            proxy.close()
            for connection_task in list(connections):
                connection_task.cancel()
            await asyncio.gather(*list(connections), return_exceptions=True)
            await proxy.wait_closed()
            await asyncio.wait_for(asyncio.gather(execution, return_exceptions=True), 3)
            await pool.close()

    registry_database(scenario)
