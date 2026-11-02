"""The indicator offers cancel only to people the cancel endpoint accepts; history names who started each job."""

import json
import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend import access, job_queue
from backend.main import app
from backend.routers import jobs
from backend.tests import test_field_registry_postgres, test_jobs_api

api = test_jobs_api.api
registry_database = test_field_registry_postgres.registry_database
JOB_ID = test_jobs_api.JOB_ID
CASES = [
    ('admin', 1, 10, True),
    ('admin', 1, None, True),
    ('power_user', 10, 10, True),
    ('power_user', 11, 10, False),
    ('power_user', 10, None, False),
    ('guest', 10, 10, False),
    ('guest', 12, 10, False),
]


@pytest.mark.parametrize('role,identifier,creator,allowed', CASES)
def test_indicator_cancel_offer_matches_what_the_cancel_endpoint_accepts(api, role, identifier, creator, allowed):
    api.identity['user'] = {'id': identifier, 'role': role}
    row = test_jobs_api.job_row(state='running', created_by=creator, started_by='Owner')
    api.database.fetch.return_value = [row]
    api.database.fetchrow.return_value = row
    offered = api.client.get('/api/datasets/1/jobs/active').json()['running'][0]['can_cancel']
    response = api.client.post(f'/api/jobs/{JOB_ID}/cancel')
    assert offered is allowed
    assert response.status_code == (200 if allowed else 403)


def test_history_names_who_started_each_job(api):
    api.database.fetch.return_value = [test_jobs_api.job_row(state='succeeded', started_by='Alex')]
    item = api.client.get('/api/datasets/1/jobs').json()['items'][0]
    assert item['started_by'] == 'Alex'


def test_cancel_response_names_the_submitter_like_every_other_read(api):
    api.identity['user'] = {'id': 1, 'role': 'admin'}
    api.database.fetchrow.return_value = test_jobs_api.job_row(started_by='Alex')
    api.cancellation.return_value = {
        key: value for key, value in test_jobs_api.job_row(state='cancelled').items() if key != 'started_by'
    }
    response = api.client.post(f'/api/jobs/{JOB_ID}/cancel')
    assert response.status_code == 200
    assert response.json()['started_by'] == 'Alex'


def test_global_job_list_requires_authentication():
    assert TestClient(app).get('/api/admin/jobs').status_code == 401


@pytest.mark.parametrize('role,expected', [('guest', 403), ('power_user', 403), ('admin', 200)])
def test_global_job_list_is_admin_only(api, role, expected):
    api.identity['user'] = {'id': 10, 'role': role}
    assert api.client.get('/api/admin/jobs').status_code == expected
    if expected != 200:
        api.database.fetch.assert_not_awaited()


@pytest.mark.parametrize('limit', [0, 101])
def test_global_job_list_page_is_bounded(api, limit):
    api.identity['user'] = {'id': 1, 'role': 'admin'}
    assert api.client.get('/api/admin/jobs', params={'limit': limit}).status_code == 422
    api.database.fetch.assert_not_awaited()


def test_global_job_list_rejects_a_malformed_cursor(api):
    api.identity['user'] = {'id': 1, 'role': 'admin'}
    assert api.client.get('/api/admin/jobs', params={'cursor': 'not-a-cursor'}).status_code == 400
    api.database.fetch.assert_not_awaited()


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires isolated PostgreSQL')
def test_real_history_and_global_list_name_submitters_and_page_separately(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        admin = {'id': 1, 'role': 'admin'}
        await database.execute(
            "INSERT INTO users(id,username,role) VALUES (1,'root-admin','admin'),"
            "(10,'Alex','power_user'),(11,'Leaver','power_user') ON CONFLICT (id) DO NOTHING",
        )
        mine = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1, created_by=10)
        orphaned = await job_queue.enqueue_job(database.pool, 'bulk_tag', dataset_id=1, created_by=11)
        scheduled = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1, lane='maintenance')
        await database.execute('DELETE FROM users WHERE id=11')
        global_ids = []
        for number in range(5):
            job = await job_queue.enqueue_job(database.pool, f'cleanup-{number}', lane='maintenance')
            global_ids.append(job['id'])
        await database.execute("UPDATE jobs SET created_at='2026-10-17T00:00:00Z'")

        history = await jobs.list_dataset_jobs(1, limit=50, cursor=None, user=admin)
        names = {item.id: item.started_by for item in history.items}
        assert names == {mine['id']: 'Alex', orphaned['id']: 'Deleted user', scheduled['id']: 'System'}
        assert (await jobs.get_job(mine['id'], admin)).started_by == 'Alex'

        expected = [
            row['id']
            for row in await database.fetch(
                'SELECT id FROM jobs WHERE dataset_id IS NULL ORDER BY created_at DESC,id DESC',
            )
        ]
        assert sorted(expected) == sorted(global_ids)
        seen: list = []
        cursor = None
        while True:
            page = await jobs.list_global_jobs(limit=2, cursor=cursor, user=admin)
            assert all(item.dataset_id is None and item.started_by == 'System' for item in page.items)
            seen.extend(item.id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == expected

    registry_database(scenario)


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires isolated PostgreSQL')
def test_real_global_job_list_reads_its_own_index_instead_of_sorting_history(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        await database.execute("""INSERT INTO jobs(id,kind,state,created_at)
            SELECT gen_random_uuid(),'selection_cleanup','succeeded',now()-g*interval '5 minutes'
            FROM generate_series(1,50000) g""")
        await database.execute("""INSERT INTO jobs(id,kind,dataset_id,state)
            SELECT gen_random_uuid(),'old_work',1,'succeeded' FROM generate_series(1,50000)""")
        await database.execute('INSERT INTO job_controls(job_id) SELECT id FROM jobs ON CONFLICT DO NOTHING')
        await database.execute('ANALYZE jobs; ANALYZE job_controls')
        query = AsyncMock(wraps=database.fetch)
        monkeypatch.setattr(database, 'fetch', query)
        page = await jobs.list_global_jobs(limit=25, cursor=None, user={'id': 1, 'role': 'admin'})
        assert len(page.items) == 25 and page.next_cursor
        statement, *parameters = query.call_args.args
        plan = json.dumps(await database.fetchval('EXPLAIN (FORMAT JSON) ' + statement, *parameters))
        assert 'idx_jobs_global_created' in plan
        assert '"Sort"' not in plan

    registry_database(scenario)
