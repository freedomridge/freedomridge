"""Bulk mutations use finite fenced units and retain a resumable prefix."""

import asyncio
import json
import os
import sys
from uuid import UUID

import pytest
from fastapi import HTTPException

from backend import job_queue, job_service
from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


def submitted(response):
    assert response.status_code == 202
    return UUID(json.loads(response.body)['job_id'])


async def claim(database):
    from backend import bulk_jobs

    return await job_queue.claim_job(database.pool, 'bulk-test', kinds=[bulk_jobs.KIND], lease_seconds=600)


async def step(database, job, *, release=False):
    from backend import bulk_jobs

    result = await bulk_jobs.handler(database.pool)(job)
    return await job_queue.publish_step(
        database.pool,
        job['id'],
        job['lease_token'],
        checkpoint=result.checkpoint,
        done=result.done,
        result=result.result,
        finished=result.finished,
        publisher=result.publish,
        release=release and not result.finished,
    )


async def finish(database, job=None):
    job = job or await claim(database)
    for _ in range(10000):
        job = await step(database, job)
        if job['state'] == 'succeeded':
            return job
    pytest.fail('Bulk operation did not finish')


def test_submit_freezes_filter_and_rechecks_locked_rows(registry_database, monkeypatch):
    from backend import bulk_jobs

    async def scenario(database):
        monkeypatch.setattr(bulk_jobs, 'BATCH_SIZE', 2)
        ids = (await ingest([{'label': 'yes'}] * 4))['ids']
        request = datasets.BulkTagRequest(
            filters={'field': 'label', 'operator': '=', 'value': 'yes'},
            add_tags=['done'],
        )
        submitted(await datasets.bulk_tag_samples(1, request, USER))
        assert request.filters is not None
        request.filters['value'] = 'no'
        job = await claim(database)
        unit = await bulk_jobs.handler(database.pool)(job)
        await database.execute("UPDATE samples SET metadata='{}' WHERE id=$1", ids[0])
        job = await job_queue.publish_step(
            database.pool,
            job['id'],
            job['lease_token'],
            checkpoint=unit.checkpoint,
            publisher=unit.publish,
        )
        assert job['checkpoint']['affected'] == 1
        later = (await ingest([{'label': 'yes'}], prefix='later'))['ids'][0]
        result = await finish(database, job)
        assert result['result']['updated_count'] == 3
        assert result['checkpoint']['examined'] == 4
        assert await database.fetchval('SELECT tags FROM samples WHERE id=$1', later) == []
        assert await database.fetchval('SELECT tags FROM samples WHERE id=$1', ids[0]) == []

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['tag', 'delete'])
def test_500k_bounded_units_with_concurrent_ingest(registry_database, operation):
    async def scenario(database):
        # Seed in bounded statements with all normal indexes and triggers enabled.
        for start in range(0, 500000, 5000):
            await database.execute(
                "INSERT INTO samples(dataset_id,filepath,media_type) "
                "SELECT 1,'scale-'||n,'image' FROM generate_series($1::int,$2::int) n",
                start + 1,
                start + 5000,
            )
        response = await (
            datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters={}, add_tags=['done']), USER)
            if operation == 'tag'
            else datasets.bulk_delete_samples(1, datasets.BulkTargetRequest(filters={}), USER)
        )
        submitted(response)
        job = await claim(database)
        units = 0
        later = []
        while job['state'] != 'succeeded':
            if units % 10 == 0:
                job, arrival = await asyncio.gather(
                    step(database, job),
                    ingest([{}], prefix=f'arrival-{units}'),
                )
                later.extend(arrival['ids'])
            else:
                job = await step(database, job)
            assert job['checkpoint'].get('last_unit_rows', 0) <= 1000
            units += 1
        assert units >= 501
        assert job['checkpoint']['examined'] == 500000
        assert job['checkpoint']['affected'] == 500000
        assert job['result']['updated_count' if operation == 'tag' else 'deleted_count'] == 500000
        assert await database.fetchval(
            'SELECT count(*) FROM samples WHERE id=ANY($1::int[]) AND tags=\'{}\'',
            later,
        ) == len(later)
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE block_id=-1)')

    registry_database(scenario)


def test_cancel_resume_expired_selection_pinned_and_exclusions(registry_database, monkeypatch):
    from backend import bulk_jobs

    async def scenario(database):
        monkeypatch.setattr(bulk_jobs, 'BATCH_SIZE', 2)
        ids = (await ingest([{}] * 5))['ids']
        await database.execute("INSERT INTO region_selections(token,sample_id) SELECT 'pinned',id FROM samples")
        identifier = submitted(
            await datasets.bulk_delete_samples(
                1,
                datasets.BulkTargetRequest(selection_token='pinned', excluded_ids=[ids[1]]),
                USER,
            ),
        )
        await database.execute("UPDATE region_selections SET created_at=NOW()-interval '3 hours'")
        cleanup_job = {'lane': 'maintenance', 'created_at': await database.fetchval('SELECT NOW()'), 'checkpoint': {}}
        cleanup = await job_service.selection_cleanup(database.pool)(cleanup_job)
        assert cleanup.checkpoint and 'after_selection' in cleanup.checkpoint
        cleanup_job['checkpoint'] = cleanup.checkpoint
        cleanup = await job_service.selection_cleanup(database.pool)(cleanup_job)
        assert cleanup.finished
        assert await database.fetchval("SELECT count(*) FROM region_selections WHERE token='pinned'") == 5
        job = await step(database, await claim(database))
        assert job['checkpoint']['affected'] == 1
        await job_queue.cancel_job(database.pool, identifier)
        job = await step(database, job)
        assert job['state'] == 'cancelled'
        await bulk_jobs.resume(database.pool, identifier)
        with pytest.raises(HTTPException):
            await bulk_jobs.resume(database.pool, identifier)
        result = await finish(database)
        assert result['result']['deleted_count'] == 4
        assert await database.fetchval('SELECT count(*) FROM samples') == 1
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM bulk_selection_pins)')

    registry_database(scenario)


def test_per_sample_patches_counts_and_empty_background(registry_database, monkeypatch):
    from backend import bulk_jobs

    async def scenario(database):
        monkeypatch.setattr(bulk_jobs, 'BATCH_SIZE', 2)
        ids = (await ingest([{'keep': True}] * 4))['ids']
        other = (await ingest([{}], dataset_id=2))['ids'][0]
        submitted(
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(
                    updates=[
                        datasets.BulkSetUpdate(sample_id=sid, metadata={'score': n})
                        for n, sid in enumerate([*ids, ids[0], other, 999])
                    ],
                    background=True,
                ),
                USER,
            ),
        )
        job = await finish(database)
        assert job['result']['updated_count'] == 4
        assert await database.fetchval('SELECT metadata FROM samples WHERE id=$1', ids[0]) == {'keep': True, 'score': 4}
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM bulk_job_targets)')
        submitted(
            await datasets.bulk_set_metadata(1, datasets.BulkSetRequest(background=True, metadata={'wrong': 1}), USER),
        )
        assert (await finish(database))['result']['updated_count'] == 0

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['rename', 'clone', 'clear'])
def test_field_chunks_keep_late_values_and_definitions(registry_database, monkeypatch, operation):
    from backend import bulk_jobs

    async def scenario(database):
        monkeypatch.setattr(bulk_jobs, 'BATCH_SIZE', 2)
        await ingest([{'source': 1}] * 4)
        if operation == 'clear':
            response = await datasets.clear_field(1, 'source', USER, background=True)
        else:
            response = await getattr(datasets, f'{operation}_field')(
                1,
                'source',
                datasets.FieldRenameRequest(new_name='destination', background=True),
                USER,
            )
        submitted(response)
        late = (await ingest([{'source': 9}], prefix='late'))['ids'][0]
        result = await finish(database)
        assert result['result']['updated_count'] == 4
        assert await database.fetchval('SELECT metadata FROM samples WHERE id=$1', late) == {'source': 9}
        assert await database.fetchval("SELECT 1 FROM dataset_fields WHERE dataset_id=1 AND field_name='source'")
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE block_id=-1)')

    registry_database(scenario)


def test_kill_restart_between_units(registry_database, tmp_path):
    async def scenario(database):
        await ingest([{}] * 5)
        identifier = submitted(
            await datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters={}, add_tags=['done']), USER),
        )
        namespace = await database.fetchval('SELECT current_schema()')
        ready = tmp_path / 'ready'
        code = """
import asyncio, os, sys
from pathlib import Path
import asyncpg
from backend import bulk_jobs, job_queue
from backend.database import init_connection
async def run():
    pool = await asyncpg.create_pool(os.environ['FRIDGE_TEST_POSTGRES_DSN'], min_size=1, max_size=2,
        init=init_connection, server_settings={'search_path': sys.argv[1]+',public'})
    job = await job_queue.claim_job(pool,'crash-test',kinds=['bulk_mutation'])
    bulk_jobs.BATCH_SIZE=2
    unit = await bulk_jobs.handler(pool)(job)
    await job_queue.publish_step(pool,job['id'],job['lease_token'],publisher=unit.publish,checkpoint=unit.checkpoint)
    Path(sys.argv[2]).write_text(str(job['id']))
    await asyncio.Event().wait()
asyncio.run(run())
"""
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', code, namespace, str(ready))
        try:

            async def wait_ready():
                while not ready.exists():
                    assert process.returncode is None
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_ready(), 10)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        saved = await database.fetchval('SELECT checkpoint FROM jobs WHERE id=$1', identifier)
        assert saved['affected'] == 2
        await database.execute("UPDATE jobs SET lease_expires_at=NOW()-interval '10 minutes' WHERE id=$1", identifier)
        job = await claim(database)
        assert job['attempt'] == 2 and job['checkpoint'] == saved
        assert (await finish(database, job))['result']['updated_count'] == 5

    registry_database(scenario)


def test_small_request_can_explicitly_submit_a_job(registry_database):
    async def scenario(database):
        ids = (await ingest([{}]))['ids']
        response = await datasets.bulk_tag_samples(
            1,
            datasets.BulkTagRequest(sample_ids=ids, add_tags=['done'], background=True),
            USER,
        )
        submitted(response)
        assert await database.fetchval('SELECT tags FROM samples WHERE id=$1', ids[0]) == []
        assert (await finish(database))['result']['updated_count'] == 1

    registry_database(scenario)


def test_lost_lease_cannot_publish(registry_database):
    from backend import bulk_jobs

    async def scenario(database):
        await ingest([{}])
        submitted(await datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters={}, add_tags=['done']), USER))
        job = await claim(database)
        unit = await bulk_jobs.handler(database.pool)(job)
        await database.execute("UPDATE jobs SET lease_expires_at=NOW()-interval '10 minutes' WHERE id=$1", job['id'])
        with pytest.raises(job_queue.LeaseLost):
            await job_queue.publish_step(
                database.pool,
                job['id'],
                job['lease_token'],
                checkpoint=unit.checkpoint,
                publisher=unit.publish,
            )
        assert await database.fetchval('SELECT tags FROM samples LIMIT 1') == []
        assert (await finish(database))['result']['updated_count'] == 1

    registry_database(scenario)


def test_bulk_delete_http_admin_policy(registry_database):
    import httpx
    from fastapi import FastAPI

    from backend.access import require_dataset_access
    from backend.routers.auth import get_current_user

    async def scenario(database):
        await ingest([{}])
        app = FastAPI()
        app.include_router(datasets.router)
        app.dependency_overrides[require_dataset_access] = lambda: None
        app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'role': 'power_user'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/datasets/1/samples/bulk-delete', json={'filters': {}})
            assert response.status_code == 403
            app.dependency_overrides[get_current_user] = lambda: USER
            response = await client.post('/datasets/1/samples/bulk-delete', json={'filters': {}})
            assert response.status_code == 202
        assert await database.fetchval('SELECT count(*) FROM samples') == 1

    registry_database(scenario)


def test_admission_drains_low_id_writers_and_prunes_empty_field(registry_database):
    async def scenario(database):
        await ingest([{'source': 1}])
        async with database.pool.acquire() as writer, writer.transaction():
            await writer.execute('SELECT pg_advisory_xact_lock_shared(20261016,1)')
            await writer.execute(
                "INSERT INTO samples(dataset_id,filepath,media_type,metadata) "
                "VALUES(1,'inflight','image','{\"source\":2}')",
            )
            with pytest.raises(HTTPException) as error:
                await datasets.clear_field(1, 'source', USER, background=True)
            assert error.value.status_code == 409
        submitted(await datasets.clear_field(1, 'source', USER, background=True))
        assert (await finish(database))['result']['updated_count'] == 2
        assert not await database.fetchval("SELECT 1 FROM dataset_fields WHERE dataset_id=1 AND field_name='source'")
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE block_id=-1)')

    registry_database(scenario)


def test_dataset_deletion_releases_bulk_targets_and_pins(registry_database):
    from backend.tests.test_dataset_deletion import finish as finish_deletion

    async def scenario(database):
        ids = (await ingest([{}] * 3))['ids']
        await database.execute("INSERT INTO region_selections(token,sample_id) SELECT 'pinned',id FROM samples")
        submitted(
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(
                    sample_ids=ids,
                    background=True,
                    metadata={'label': 'yes'},
                ),
                USER,
            ),
        )
        submitted(await datasets.bulk_delete_samples(1, datasets.BulkTargetRequest(selection_token='pinned'), USER))
        await datasets.delete_dataset(1, USER)
        await finish_deletion(database)
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM bulk_job_targets)')
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM bulk_selection_pins)')

    registry_database(scenario)


def test_nested_selection_filter_is_pinned_and_rechecked(registry_database):
    async def scenario(database):
        ids = (await ingest([{'label': 'yes'}] * 3))['ids']
        await database.execute("INSERT INTO region_selections(token,sample_id) SELECT 'nested',id FROM samples")
        filters = {
            'logic': 'AND',
            'filters': [
                {'field': 'id', 'operator': 'in_selection', 'value': 'nested'},
                {'field': 'label', 'operator': '=', 'value': 'yes'},
            ],
        }
        submitted(
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(filters=filters, metadata={'reviewed': True}),
                USER,
            ),
        )
        await database.execute("UPDATE region_selections SET created_at=NOW()-interval '3 hours'")
        await database.execute("UPDATE samples SET metadata='{}' WHERE id=$1", ids[0])
        assert (await finish(database))['result']['updated_count'] == 2
        assert await database.fetchval('SELECT metadata FROM samples WHERE id=$1', ids[0]) == {}

    registry_database(scenario)
