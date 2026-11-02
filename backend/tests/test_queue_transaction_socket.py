"""Short queue calls have a client wall-time budget even if PostgreSQL traffic disappears."""

import asyncio
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable

import asyncpg
import pytest

from backend import job_queue
from backend.tests import test_field_registry_postgres
from backend.tests.tcp_faults import blackhole_pool

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


@pytest.mark.parametrize('operation', ['submit', 'claim', 'cancel'])
def test_silent_queue_connection_is_terminated_before_rollback_can_hang(registry_database, monkeypatch, operation):
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, 'probe')
        original = asyncpg.Connection.execute
        dropped = False
        async with blackhole_pool(database) as (pool, flows):

            async def drop_after_begin(connection, query, *args, **kwargs):
                nonlocal dropped
                result = await original(connection, query, *args, **kwargs)
                port = connection._transport.get_extra_info('sockname')[1]
                if not dropped and query.startswith('SET LOCAL lock_timeout') and port in flows:
                    flows[port].set()
                    dropped = True
                return result

            monkeypatch.setattr(asyncpg.Connection, 'execute', drop_after_begin)
            monkeypatch.setattr(job_queue, 'QUEUE_TRANSACTION_SECONDS', 0.25, raising=False)
            request: Awaitable[Any]
            if operation == 'submit':
                request = job_queue.enqueue_job(pool, 'probe')
            elif operation == 'claim':
                request = job_queue.claim_job(pool, 'review', kinds=['probe'])
            else:
                request = job_queue.cancel_job(pool, job['id'])
            safety = asyncio.get_running_loop().call_later(2, pool.terminate)
            try:
                with pytest.raises(job_queue.QueueBusy, match='wall-time'):
                    await asyncio.wait_for(request, 3)
            finally:
                safety.cancel()
            assert dropped
            assert await pool.fetchval('SELECT 42') == 42
        assert await database.fetchval('SELECT count(*) FROM jobs') == 1
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'queued'

    registry_database(scenario)
