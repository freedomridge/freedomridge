"""Job visibility, cancellation authorization, and bounded history pagination."""

import base64
import json
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import access, job_queue
from backend.main import app
from backend.routers import jobs
from backend.routers.auth import get_current_user
from backend.tests import test_field_registry_postgres

registry_database = test_field_registry_postgres.registry_database
JOB_ID = UUID('813b5d30-e738-4d1d-9ace-0acf1e6466fd')
USER = {'id': 10, 'role': 'power_user'}


def job_row(**changes):
    timestamp = datetime(2026, 10, 17, tzinfo=timezone.utc)
    return {
        'id': JOB_ID,
        'kind': 'statistics',
        'dataset_id': 1,
        'placement': 'server',
        'resources': {'memory_bytes': 1024},
        'state': 'queued',
        'progress_done': 2,
        'progress_total': 10,
        'message': 'Measuring fields',
        'result': None,
        'error': None,
        'created_by': USER['id'],
        'created_at': timestamp,
        'updated_at': timestamp,
        'started_at': None,
        'finished_at': None,
        'heartbeat_at': None,
        'attempt': 0,
        'failures': 0,
        'max_failures': 5,
        'lane': 'user',
        'available_at': timestamp,
        'cancel_requested': False,
        'started_by': 'jobs-owner',
        'stalled': False,
        'retry_delayed': False,
        'dataset_lane': True,  # the active query's flag (index reviews belong to no dataset lane)
        'args': {'private_path': '/private'},
        'checkpoint': {'internal': True},
        'lease_token': uuid4(),
        'worker_id': 'private-hostname',
        **changes,
    }


@pytest.fixture
def api(monkeypatch):
    identity = {'user': dict(USER)}

    async def current_user():
        if identity['user'] is None:
            raise HTTPException(status_code=401, detail='Authentication required')
        return identity['user']

    database = SimpleNamespace(
        pool=object(),
        fetchrow=AsyncMock(return_value=job_row()),
        fetch=AsyncMock(return_value=[]),
    )
    allowed = AsyncMock(return_value={'id': 1})
    cancellation = AsyncMock(return_value=job_row(cancel_requested=True, state='cancelled'))
    monkeypatch.setattr(jobs, 'db', database)
    monkeypatch.setattr(jobs, 'check_dataset_access', allowed)
    monkeypatch.setattr(jobs, 'cancel_job', cancellation)
    app.dependency_overrides[get_current_user] = current_user
    try:
        yield SimpleNamespace(
            client=TestClient(app),
            identity=identity,
            database=database,
            allowed=allowed,
            cancellation=cancellation,
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_job_status_preserves_progress_and_real_error_but_never_internal_credentials(api):
    error = {'code': 'invalid_model', 'message': 'Model dimension 512 does not match 768'}
    api.database.fetchrow.return_value = job_row(state='failed', error=error)
    response = api.client.get(f'/api/jobs/{JOB_ID}')
    assert response.status_code == 200
    body = response.json()
    assert body['error'] == error
    assert body['progress'] == {'done': 2, 'total': 10, 'message': 'Measuring fields'}
    assert body['resources'] == {'memory_bytes': 1024}
    assert body['placement'] == 'server'
    assert not {'args', 'checkpoint', 'lease_token', 'worker_id'} & body.keys()
    api.allowed.assert_awaited_once_with(1, USER)
    assert '*' not in api.database.fetchrow.call_args.args[0]


@pytest.mark.parametrize('path', [f'/api/jobs/{JOB_ID}', '/api/datasets/1/jobs', f'/api/jobs/{JOB_ID}/cancel'])
def test_jobs_require_authentication_before_lookup(api, path):
    api.identity['user'] = None
    response = api.client.post(path) if path.endswith('/cancel') else api.client.get(path)
    assert response.status_code == 401
    api.database.fetchrow.assert_not_awaited()
    api.database.fetch.assert_not_awaited()
    api.cancellation.assert_not_awaited()


@pytest.mark.parametrize('action', ['get', 'list', 'list_all', 'cancel'])
def test_existing_jobs_always_recheck_current_dataset_access(api, action):
    api.allowed.side_effect = HTTPException(status_code=403, detail='Access revoked')
    if action == 'cancel':
        response = api.client.post(f'/api/jobs/{JOB_ID}/cancel')
    else:
        response = api.client.get(
            '/api/datasets/1/jobs?include_system_statistics=true'
            if action == 'list_all'
            else '/api/datasets/1/jobs'
            if action == 'list'
            else f'/api/jobs/{JOB_ID}',
        )
    assert response.status_code == 403
    assert response.json()['detail'] == 'Access revoked'
    api.cancellation.assert_not_awaited()


@pytest.mark.parametrize('role,expected', [('guest', 403), ('power_user', 403), ('admin', 200)])
def test_global_jobs_are_admin_only(api, role, expected):
    api.identity['user']['role'] = role
    api.database.fetchrow.return_value = job_row(dataset_id=None)
    assert api.client.get(f'/api/jobs/{JOB_ID}').status_code == expected
    api.allowed.assert_not_awaited()


@pytest.mark.parametrize(
    'role,creator,expected',
    [('guest', 10, 403), ('power_user', 11, 403), ('power_user', 10, 200), ('admin', 11, 200)],
)
def test_cancellation_requires_admin_or_submitting_power_user(api, role, creator, expected):
    api.identity['user']['role'] = role
    api.database.fetchrow.return_value = job_row(created_by=creator)
    response = api.client.post(f'/api/jobs/{JOB_ID}/cancel')
    assert response.status_code == expected
    if expected == 200:
        api.cancellation.assert_awaited_once_with(api.database.pool, JOB_ID)
    else:
        api.cancellation.assert_not_awaited()


def test_running_cancel_response_does_not_pretend_the_worker_has_stopped(api):
    api.cancellation.return_value = job_row(state='running', cancel_requested=True)
    response = api.client.post(f'/api/jobs/{JOB_ID}/cancel')
    assert response.status_code == 200
    assert response.json()['state'] == 'running'
    assert response.json()['cancel_requested'] is True


def test_busy_cancellation_returns_actionable_retry_instead_of_500(api):
    api.cancellation.side_effect = job_queue.QueueBusy('Job state is busy')
    response = api.client.post(f'/api/jobs/{JOB_ID}/cancel')
    assert response.status_code == 409
    assert response.json()['detail'] == 'Job state is busy; retry cancellation'


@pytest.mark.parametrize('role,creator,expected', [('guest', 10, 200), ('power_user', 11, 404), ('admin', 11, 200)])
def test_deleted_dataset_history_is_visible_only_to_creator_or_admin(api, role, creator, expected):
    api.identity['user']['role'] = role
    api.database.fetchrow.return_value = job_row(created_by=creator)
    api.allowed.side_effect = HTTPException(status_code=404, detail='Dataset not found')
    assert api.client.get(f'/api/jobs/{JOB_ID}').status_code == expected


def test_deleted_dataset_cancellation_still_requires_a_power_user(api):
    api.identity['user']['role'] = 'guest'
    api.allowed.side_effect = HTTPException(status_code=404, detail='Dataset not found')
    assert api.client.post(f'/api/jobs/{JOB_ID}/cancel').status_code == 403
    api.cancellation.assert_not_awaited()


@pytest.mark.parametrize('method,suffix', [('get', ''), ('post', '/cancel')])
def test_missing_job_is_404(api, method, suffix):
    api.database.fetchrow.return_value = None
    assert getattr(api.client, method)(f'/api/jobs/{JOB_ID}{suffix}').status_code == 404


@pytest.mark.parametrize('limit', [0, 101, -1])
def test_job_history_cannot_request_an_unbounded_page(api, limit):
    assert api.client.get('/api/datasets/1/jobs', params={'limit': limit}).status_code == 422
    api.database.fetch.assert_not_awaited()


@pytest.mark.parametrize(
    'cursor',
    [
        'not-a-cursor',
        base64.urlsafe_b64encode(b'["2026-10-17T00:00:00+00:00",123]').decode(),
        base64.urlsafe_b64encode(json.dumps(['2026-10-17T00:00:00', str(JOB_ID)]).encode()).decode(),
        base64.urlsafe_b64encode(b'{}').decode(),
    ],
)
def test_malformed_history_cursor_is_a_client_error_not_a_500(api, cursor):
    response = api.client.get('/api/datasets/1/jobs', params={'cursor': cursor})
    assert response.status_code == 400
    api.database.fetch.assert_not_awaited()


def test_history_cursor_keeps_timestamp_and_uuid_tiebreaker(api):
    first, second = job_row(), job_row(id=uuid4())
    api.database.fetch.return_value = [first, second]
    response = api.client.get('/api/datasets/1/jobs', params={'limit': 1})
    assert response.status_code == 200
    page = response.json()
    assert [item['id'] for item in page['items']] == [str(first['id'])]
    assert page['next_cursor']
    api.database.fetch.return_value = [second]
    following = api.client.get('/api/datasets/1/jobs', params={'limit': 1, 'cursor': page['next_cursor']})
    assert following.status_code == 200
    assert following.json()['next_cursor'] is None
    assert api.database.fetch.call_args.args[1:] == (1, first['created_at'], first['id'], 2)


def test_arbitrary_job_submission_is_not_an_http_endpoint(api):
    assert api.client.post('/api/jobs', json={'kind': 'execute_sql'}).status_code == 404


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires isolated PostgreSQL')
def test_real_job_history_pagination_and_cancellation_preserve_queue_state(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute("INSERT INTO users(id,username,role) VALUES (10,'jobs-owner','power_user')")
        await database.execute('UPDATE datasets SET is_global=FALSE,owner_id=10 WHERE id=1')
        for index in range(5):
            await job_queue.enqueue_job(database.pool, f'test-{index}', dataset_id=1, created_by=10)
        await database.execute("UPDATE jobs SET created_at='2026-10-17T00:00:00Z'")
        expected = [row['id'] for row in await database.fetch('SELECT id FROM jobs ORDER BY created_at DESC,id DESC')]
        seen: list[UUID] = []
        cursor = None
        while True:
            page = await jobs.list_dataset_jobs(1, limit=2, cursor=cursor, user=USER)
            seen.extend(item.id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == expected
        cancelled = await jobs.request_job_cancellation(expected[0], USER)
        again = await jobs.request_job_cancellation(expected[0], USER)
        assert cancelled.state == again.state == 'cancelled'
        assert cancelled.finished_at == again.finished_at
        assert cancelled.cancel_requested is True
        claimed = await job_queue.claim_job(database.pool, 'api-test-worker')
        assert claimed is not None
        requested = await jobs.request_job_cancellation(claimed['id'], USER)
        assert requested.state == 'running' and requested.cancel_requested
        acknowledged = await job_queue.complete_job(database.pool, claimed['id'], claimed['lease_token'])
        assert acknowledged['state'] == 'cancelled'
        assert (await jobs.get_job(claimed['id'], USER)).state == 'cancelled'
        await database.execute('UPDATE datasets SET owner_id=NULL WHERE id=1')
        with pytest.raises(HTTPException) as revoked:
            await jobs.get_job(expected[0], USER)
        assert revoked.value.status_code == 403
        deletion = await job_queue.enqueue_job(database.pool, 'delete_dataset', dataset_id=1, created_by=10)
        deleting = await job_queue.claim_job(database.pool, 'api-test-worker', kinds=['delete_dataset'])
        assert deleting is not None

        async def delete_target(connection):
            await connection.execute('DELETE FROM datasets WHERE id=1')

        await job_queue.publish_step(
            database.pool,
            deletion['id'],
            deleting['lease_token'],
            publisher=delete_target,
            finished=True,
            result={'message': 'Dataset deleted'},
        )
        retained = await jobs.get_job(deletion['id'], USER)
        assert retained.state == 'succeeded'
        assert retained.dataset_id == 1
        assert retained.result == {'message': 'Dataset deleted'}
        with pytest.raises(HTTPException) as unrelated:
            await jobs.get_job(deletion['id'], {'id': 11, 'role': 'power_user'})
        assert unrelated.value.status_code == 404
        with pytest.raises(HTTPException) as missing_dataset:
            await jobs.list_dataset_jobs(1, limit=2, cursor=None, user=USER)
        assert missing_dataset.value.status_code == 404

    registry_database(scenario)


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires isolated PostgreSQL')
def test_real_cancellation_does_not_wait_behind_a_locked_job(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute("INSERT INTO users(id,username,role) VALUES (10,'jobs-owner','power_user')")
        job = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1, created_by=10)
        async with database.pool.acquire() as blocker, blocker.transaction():
            await blocker.execute('SELECT id FROM jobs WHERE id=$1 FOR UPDATE', job['id'])
            started = time.monotonic()
            accepted = await jobs.request_job_cancellation(job['id'], USER)
            assert time.monotonic() - started < 1
            assert accepted.cancel_requested
            assert (await jobs.get_job(job['id'], USER)).cancel_requested
            assert (await jobs.list_dataset_jobs(1, limit=10, cursor=None, user=USER)).items[0].cancel_requested
        assert (await jobs.request_job_cancellation(job['id'], USER)).state == 'cancelled'

    registry_database(scenario)
