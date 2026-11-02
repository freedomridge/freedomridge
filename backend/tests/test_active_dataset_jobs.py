"""The dataset indicator is a bounded, access-controlled read, not a new job gate."""

import asyncio
import base64
import json
import os
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import access, job_queue
from backend.main import app
from backend.routers import datasets, jobs
from backend.tests import test_field_registry_postgres, test_jobs_api

registry_database = test_field_registry_postgres.registry_database
api = test_jobs_api.api
USER = {'id': 10, 'role': 'power_user'}


def test_active_jobs_route_exists_and_requires_authentication():
    assert TestClient(app).get('/api/datasets/1/jobs/active').status_code == 401


def test_active_jobs_does_not_bypass_dataset_access(api):
    api.allowed.side_effect = HTTPException(status_code=403, detail='Dataset access denied')
    response = api.client.get('/api/datasets/1/jobs/active')
    assert response.status_code == 403
    api.database.fetch.assert_not_awaited()


def test_active_jobs_http_response_includes_progress_without_private_credentials(api):
    api.database.fetch.return_value = [test_jobs_api.job_row(state='running', started_by='Alex')]
    response = api.client.get('/api/datasets/1/jobs/active')
    assert response.status_code == 200
    body = response.json()
    assert body['waiting'] == [] and body['next_cursor'] is None
    assert body['running'][0]['started_by'] == 'Alex'
    assert body['running'][0]['progress'] == {'done': 2, 'total': 10, 'message': 'Measuring fields'}
    assert not {'args', 'checkpoint', 'barrier', 'lease_token', 'worker_id'} & body['running'][0].keys()


@pytest.mark.parametrize('limit', [0, 101, -1])
def test_active_jobs_page_is_bounded(api, limit):
    assert api.client.get('/api/datasets/1/jobs/active', params={'limit': limit}).status_code == 422
    api.database.fetch.assert_not_awaited()


@pytest.mark.parametrize(
    'values',
    [
        None,
        {},
        ['bad', 'date', 'uuid'],
        ['9999-12-31T23:59:59-14:00', '2026-01-01T00:00:00Z', str(uuid4())],
        ['2026-01-01T00:00:00', '2026-01-01T00:00:00Z', str(uuid4())],
    ],
)
def test_bad_active_cursor_is_safe_client_error(api, values):
    cursor = base64.urlsafe_b64encode(json.dumps(values).encode()).decode()
    response = api.client.get('/api/datasets/1/jobs/active', params={'cursor': cursor})
    assert response.status_code == 400
    assert response.json()['detail'] == 'Invalid active jobs cursor'
    api.database.fetch.assert_not_awaited()


def test_history_cursor_overflow_is_safe_client_error(api):
    cursor = base64.urlsafe_b64encode(json.dumps(['9999-12-31T23:59:59-14:00', str(uuid4())]).encode()).decode()
    response = api.client.get('/api/datasets/1/jobs', params={'cursor': cursor})
    assert response.status_code == 400
    assert response.json()['detail'] == 'Invalid jobs cursor'
    api.database.fetch.assert_not_awaited()


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires isolated PostgreSQL')
def test_real_active_jobs_include_old_running_work_all_waiting_pages_and_submitters(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute("INSERT INTO users(id,username,role) VALUES (10,'Alex','power_user')")
        await database.execute('UPDATE datasets SET owner_id=10,is_global=FALSE WHERE id=1')
        running = await job_queue.enqueue_job(
            database.pool,
            'umap_fit',
            dataset_id=1,
            placement='compute',
            created_by=10,
        )
        claim = await job_queue.claim_job(database.pool, 'remote', placements=['compute'])
        assert claim is not None
        await job_queue.publish_step(database.pool, running['id'], claim['lease_token'], done=20, total=100)
        for number in range(120):
            terminal = await job_queue.enqueue_job(database.pool, 'completed', dataset_id=1)
            await database.execute("UPDATE jobs SET state='succeeded' WHERE id=$1", terminal['id'])
        for number in range(61):
            await job_queue.enqueue_job(database.pool, 'bulk_tag', dataset_id=1, created_by=10)
        await job_queue.enqueue_job(database.pool, 'other_dataset', dataset_id=2)
        await database.execute(
            """UPDATE jobs SET available_at='2026-10-17T00:00:00Z',created_at='2026-10-17T00:00:00Z'
               WHERE state='queued'""",
        )
        expected = [
            row['id']
            for row in await database.fetch(
                "SELECT id FROM jobs WHERE dataset_id=1 AND state='queued' ORDER BY available_at,created_at,id",
            )
        ]
        cursor = None
        seen: list[UUID] = []
        while True:
            page = await jobs.list_active_dataset_jobs(1, limit=25, cursor=cursor, user=USER)
            assert len(page.running) == 1 and page.running[0].id == running['id']
            assert page.running[0].started_by == 'Alex'
            assert page.running[0].progress.done == 20
            assert page.running[0].progress.total == 100
            for item in page.running + page.waiting:
                assert (
                    not {'args', 'barrier', 'checkpoint', 'lease_token', 'worker_id', 'dedupe_owner'}
                    & item.model_dump().keys()
                )
            seen.extend(item.id for item in page.waiting)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == expected
        sample_id = (await test_field_registry_postgres.ingest([{'reviewed': False}]))['ids'][0]
        await asyncio.wait_for(
            datasets.update_sample(
                1,
                sample_id,
                datasets.SampleUpdate(metadata={'reviewed': True}),
                USER,
            ),
            2,
        )
        assert await database.fetchval("SELECT metadata->'reviewed' FROM samples WHERE id=$1", sample_id) is True
        assert await job_queue.claim_job(database.pool, 'local') is not None
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', running['id']) == 'running'
        await database.execute("DELETE FROM jobs WHERE dataset_id=2")
        assert await job_queue.claim_job(database.pool, 'local') is None
        await job_queue.complete_job(database.pool, running['id'], claim['lease_token'])
        assert (await jobs.list_active_dataset_jobs(1, limit=25, cursor=None, user=USER)).running == []
        assert await job_queue.claim_job(database.pool, 'local') is not None

    registry_database(scenario)


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires isolated PostgreSQL')
def test_active_read_is_nonblocking_and_does_not_reveal_another_dataset(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute(
            "INSERT INTO users(id,username,role) VALUES (10,'Alex','power_user'),(11,'other','power_user')",
        )
        await database.execute('UPDATE datasets SET owner_id=11,is_global=FALSE WHERE id=2')
        job = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1, lane='maintenance')
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.fetchrow('SELECT id FROM jobs WHERE id=$1 FOR UPDATE', job['id'])
            result = await asyncio.wait_for(jobs.list_active_dataset_jobs(1, limit=25, cursor=None, user=USER), 1)
            assert result.waiting[0].started_by == 'System'
        with pytest.raises(HTTPException) as denied:
            await jobs.list_active_dataset_jobs(2, limit=25, cursor=None, user=USER)
        assert denied.value.status_code == 403
        personal = await job_queue.enqueue_job(database.pool, 'bulk_tag', dataset_id=1, created_by=10)
        await database.execute('DELETE FROM users WHERE id=10')
        admin = {'id': 1, 'role': 'admin'}
        page = await jobs.list_active_dataset_jobs(1, limit=25, cursor=None, user=admin)
        assert next(item for item in page.waiting if item.id == personal['id']).started_by == 'Deleted user'

    registry_database(scenario)


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires isolated PostgreSQL')
def test_active_read_uses_dataset_indexes_not_terminal_history(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute("""INSERT INTO jobs(id,kind,dataset_id,state)
            SELECT gen_random_uuid(),'old_work',1,'succeeded' FROM generate_series(1,100000)""")
        await database.execute("""INSERT INTO jobs(id,kind,dataset_id)
            SELECT gen_random_uuid(),'other_dataset',2 FROM generate_series(1,2000)""")
        await database.execute('INSERT INTO job_controls(job_id) SELECT id FROM jobs ON CONFLICT DO NOTHING')
        await job_queue.enqueue_job(database.pool, 'umap_fit', dataset_id=1, placement='compute')
        await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1, lane='maintenance')
        assert await job_queue.claim_job(database.pool, 'remote', placements=['compute']) is not None
        assert await job_queue.claim_job(database.pool, 'maintenance', lanes=['maintenance']) is not None
        for number in range(30):
            await job_queue.enqueue_job(database.pool, 'bulk_tag', dataset_id=1)
        await database.execute('ANALYZE jobs; ANALYZE job_controls')
        query = AsyncMock(wraps=database.fetch)
        monkeypatch.setattr(database, 'fetch', query)
        page = await jobs.list_active_dataset_jobs(1, limit=25, cursor=None, user={'id': 1, 'role': 'admin'})
        assert len(page.running) == 2 and len(page.waiting) == 25 and page.next_cursor
        statement, *parameters = query.call_args.args
        plan = await database.fetchval('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + statement, *parameters)
        if isinstance(plan, str):
            plan = json.loads(plan)
        serialized = json.dumps(plan)
        assert 'idx_jobs_queued_dataset' in serialized
        assert 'idx_jobs_lane_owner' in serialized
        print('Active jobs with 100k terminal rows: execution_ms=', plan[0]['Execution Time'])

    registry_database(scenario)
