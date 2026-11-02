"""The Indexes tab's remove-index toggle (demote) drops the field index online.

Before 066 it ran a plain DROP INDEX: its ACCESS EXCLUSIVE request on samples queued behind any long
reader (a pg_dump holds ACCESS SHARE for the whole dump), and every later read and write of samples,
ingest included, queued behind that request for the rest of the backup. Now an index_drop job marks the
field not indexed and runs DROP INDEX CONCURRENTLY, which conflicts with neither reads nor writes.
"""

import asyncio
import os

import pytest

from backend.routers import datasets
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.test_field_registry_postgres import ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL test database',
)
USER = {'id': 1, 'role': 'admin', 'username': 'admin'}


def test_demote_never_blocks_ingest_behind_a_long_reader(registry_database):
    async def scenario(database):
        await ingest([{'score': index} for index in range(3)])
        await datasets.promote_field(1, 'score', USER)
        index = datasets._promoted_index_name(1, 'score')

        async def index_present():
            return await database.fetchval(
                'SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = $1)',
                index,
            )

        assert await index_present()
        holder = await database.pool.acquire()
        transaction = holder.transaction()
        await transaction.start()
        await holder.fetchval('SELECT count(*) FROM samples')  # ACCESS SHARE for the whole transaction
        try:
            demotion = asyncio.create_task(datasets.demote_field(1, 'score', USER))
            await asyncio.sleep(1.5)  # the removal is waiting for the reader now
            await asyncio.wait_for(ingest([{'score': 9}], prefix='during'), 5)  # must not queue behind it
        finally:
            await transaction.rollback()
            await database.pool.release(holder)
        await asyncio.wait_for(demotion, 30)

        async def removed():
            while await index_present():
                await asyncio.sleep(0.05)

        await asyncio.wait_for(removed(), 30)
        assert await database.fetchval("SELECT is_promoted FROM dataset_fields WHERE field_name = 'score'") is False

    registry_database(job_helpers.with_runner(scenario))
