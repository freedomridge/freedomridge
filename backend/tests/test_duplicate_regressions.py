"""Duplicate-maintenance contracts, each written to catch a regression."""

from unittest.mock import AsyncMock

from backend import access, duplicate_jobs, job_queue
from backend.routers import hashes
from backend.routers import jobs as jobs_router
from backend.tests import job_helpers
from backend.tests import test_field_registry_postgres as helpers

registry_database = helpers.registry_database
pytestmark = helpers.pytestmark


def test_hash_writes_leave_committed_block_work(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO samples(id,dataset_id,filepath,media_type)
               SELECT n,1,n::text,'image' FROM generate_series(1,2500) n;
               INSERT INTO sample_hashes(sample_id,field_name,hash_hex) SELECT id,'a','x' FROM samples""",
        )
        exists = await database.fetchval("SELECT to_regclass(current_schema()||'.duplicate_pending') IS NOT NULL")
        assert exists, 'Hash writes must leave durable duplicate maintenance work'
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 3

    registry_database(scenario)


def test_routine_duplicate_jobs_are_hidden_from_default_history(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs_router, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        job = await job_queue.enqueue_job(
            database.pool,
            'duplicate_maintenance',
            dataset_id=1,
            lane='maintenance',
            barrier={'full': False},
        )
        claim = await job_queue.claim_job(database.pool, 'history-test', kinds=['duplicate_maintenance'])
        assert claim is not None
        await job_queue.publish_step(database.pool, job['id'], claim['lease_token'], finished=True)
        page = await jobs_router.list_dataset_jobs(1, limit=50, cursor=None, user={'id': 1, 'role': 'admin'})
        assert page.items == [], 'Routine duplicate maintenance must share the routine-history filter'

    registry_database(scenario)


def test_refresh_endpoint_delegates_to_runner_not_full_rebuild(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(hashes, 'db', database)
        await database.execute(
            """INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES(1,1,'one','image');
               INSERT INTO sample_hashes(sample_id,field_name,hash_hex) VALUES(1,'a','x')""",
        )
        oracle = AsyncMock()
        monkeypatch.setattr(hashes, 'refresh_dup_secondary', oracle, raising=False)
        async with job_helpers.runner(database, kinds=[duplicate_jobs.KIND]):
            result = await hashes.refresh_duplicates(1, 'a', user={'id': 1, 'role': 'admin'})
        assert result == {'config': 'a', 'hidden': 0}
        oracle.assert_not_called()
        job = await database.fetchrow("SELECT * FROM jobs WHERE kind='duplicate_maintenance'")
        assert job['created_by'] == 1 and job['args'] == {'config': 'a'} and job['state'] == 'succeeded'

    registry_database(scenario)
