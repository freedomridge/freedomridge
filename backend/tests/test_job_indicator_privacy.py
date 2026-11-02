"""Public progress never contains private diagnostics; job readers are not necessarily its submitter."""

import os
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from backend import access, job_queue
from backend.routers import jobs
from backend.routers.auth import get_current_user
from backend.tests import test_field_registry_postgres, test_jobs_api

api = test_jobs_api.api
registry_database = test_field_registry_postgres.registry_database
PRIVATE_ERROR = {'type': 'OperationalError', 'message': 'private-db.internal: password failed for svc_fridge'}
PRIVATE_RESULT = {'artifact': 's3://private-training-bucket/owner-only/model.bin'}
PUBLIC_FIELDS = {
    'id',
    'kind',
    'placement',
    'lane',
    'state',
    'started_by',
    'progress',
    'cancel_requested',
    'can_cancel',
    'stalled',
    'retry_delayed',
    'dataset_lane',
}


@pytest.mark.parametrize('role,identifier', [('admin', 1), ('power_user', 10), ('power_user', 11), ('guest', 12)])
def test_indicator_has_its_own_narrow_response_for_every_viewer(api, role, identifier):
    api.identity['user'] = {'id': identifier, 'role': role}
    api.database.fetch.return_value = [
        test_jobs_api.job_row(
            state='running',
            started_by='Owner',
            error=PRIVATE_ERROR,
            result=PRIVATE_RESULT,
            stalled=True,
            retry_delayed=False,
        ),
    ]
    response = api.client.get('/api/datasets/1/jobs/active')
    assert response.status_code == 200
    body = response.json()['running'][0]
    assert body.keys() == PUBLIC_FIELDS
    assert body['stalled'] is True
    assert body['retry_delayed'] is False
    assert 'private-' not in response.text


@pytest.mark.parametrize('endpoint', ['detail', 'history'])
@pytest.mark.parametrize(
    'role,identifier,creator,can_read',
    [
        ('admin', 1, 10, True),
        ('power_user', 10, 10, True),
        ('guest', 10, 10, True),
        ('power_user', 11, 10, False),
        ('guest', 12, 10, False),
        ('guest', 12, None, False),
    ],
)
def test_error_and_result_require_admin_or_actual_submitter(api, endpoint, role, identifier, creator, can_read):
    api.identity['user'] = {'id': identifier, 'role': role}
    row = test_jobs_api.job_row(created_by=creator, state='failed', error=PRIVATE_ERROR, result=PRIVATE_RESULT)
    api.database.fetchrow.return_value = row
    api.database.fetch.return_value = [row]
    response = api.client.get(f'/api/jobs/{row["id"]}' if endpoint == 'detail' else '/api/datasets/1/jobs')
    assert response.status_code == 200
    body = response.json() if endpoint == 'detail' else response.json()['items'][0]
    if can_read:
        assert body['error'] == PRIVATE_ERROR
        assert body['result'] == PRIVATE_RESULT
    else:
        assert 'error' not in body and 'result' not in body
        assert 'private-' not in response.text
    assert body['state'] == 'failed'
    assert body['progress'] == {'done': 2, 'total': 10, 'message': 'Measuring fields'}


@pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an explicitly isolated PostgreSQL database',
)
def test_stalled_and_retry_state_use_database_time_without_mutating_queue(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        owner = {'id': 10, 'role': 'power_user'}
        await database.execute("INSERT INTO users(id,username,role) VALUES (10,'Owner','power_user')")
        running = await job_queue.enqueue_job(database.pool, 'umap_fit', dataset_id=1, created_by=10)
        claimed = await job_queue.claim_job(database.pool, 'stalled-worker', kinds=['umap_fit'])
        assert claimed is not None
        retry = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1, lane='maintenance')
        retry_claim = await job_queue.claim_job(database.pool, 'retry-worker', kinds=['statistics'])
        assert retry_claim is not None
        await job_queue.defer_job(
            database.pool,
            retry['id'],
            retry_claim['lease_token'],
            PRIVATE_ERROR,
            delay_seconds=60,
        )
        await database.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            running['id'],
        )
        before = await database.fetch('SELECT * FROM jobs ORDER BY id')
        page = await jobs.list_active_dataset_jobs(1, limit=25, cursor=None, user=owner)
        assert page.running[0].stalled is True
        assert page.running[0].retry_delayed is False
        assert page.resuming[0].retry_delayed is True
        assert page.resuming[0].stalled is False
        assert page.running[0].model_dump().keys() == PUBLIC_FIELDS
        assert page.resuming[0].model_dump().keys() == PUBLIC_FIELDS
        assert await database.fetch('SELECT * FROM jobs ORDER BY id') == before
        await database.execute(
            "UPDATE jobs SET available_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            retry['id'],
        )
        page = await jobs.list_active_dataset_jobs(1, limit=25, cursor=None, user=owner)
        assert page.resuming[0].retry_delayed is False
        assert {item.id for item in page.running + page.resuming} == {running['id'], retry['id']}
        assert all(isinstance(item.id, UUID) for item in page.running + page.resuming)

    registry_database(scenario)


@pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an explicitly isolated PostgreSQL database',
)
def test_private_diagnostics_follow_submitter_not_dataset_sharing(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute("""INSERT INTO users(id,username,role) VALUES
            (10,'Owner','power_user'), (20,'Shared','guest'), (21,'Unshared','guest')""")
        await database.execute('UPDATE datasets SET is_global=false,owner_id=10 WHERE id=1')
        await database.execute('INSERT INTO dataset_access(dataset_id,user_id) VALUES (1,20)')
        submitted = await job_queue.enqueue_job(database.pool, 'umap_fit', dataset_id=1, created_by=10)
        await database.execute(
            'UPDATE jobs SET error=$2,result=$3 WHERE id=$1',
            submitted['id'],
            PRIVATE_ERROR,
            PRIVATE_RESULT,
        )
        application = FastAPI()
        application.include_router(jobs.router, prefix='/api')
        identity = {'id': 20, 'role': 'guest'}
        application.dependency_overrides[get_current_user] = lambda: identity
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://test') as client:
            for role, identifier, allowed in [('guest', 20, False), ('power_user', 10, True), ('admin', 1, True)]:
                identity.update(id=identifier, role=role)
                progress = await client.get('/api/datasets/1/jobs/active')
                assert progress.status_code == 200
                assert progress.json()['waiting'][0].keys() == PUBLIC_FIELDS
                assert 'private-' not in progress.text
                detail = await client.get(f'/api/jobs/{submitted["id"]}')
                history = await client.get('/api/datasets/1/jobs')
                assert detail.status_code == history.status_code == 200
                for body in (detail.json(), history.json()['items'][0]):
                    assert ('error' in body) == allowed
                    assert ('result' in body) == allowed
                    if allowed:
                        assert body['error'] == PRIVATE_ERROR
                        assert body['result'] == PRIVATE_RESULT
            identity.update(id=20, role='guest')
            await database.execute('DELETE FROM dataset_access WHERE dataset_id=1 AND user_id=20')
            for endpoint in ('/api/datasets/1/jobs/active', '/api/datasets/1/jobs', f'/api/jobs/{submitted["id"]}'):
                assert (await client.get(endpoint)).status_code == 403
            await database.execute('UPDATE datasets SET is_global=true WHERE id=1')
            identity.update(id=21)
            detail = await client.get(f'/api/jobs/{submitted["id"]}')
            assert detail.status_code == 200
            assert 'error' not in detail.json() and 'result' not in detail.json()

    registry_database(scenario)
