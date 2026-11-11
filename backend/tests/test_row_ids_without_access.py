"""A signed-in user who can't open a dataset could tell which sample, view and job ids exist in it: a missing id
answered 404 and one in that dataset 403 (check_dataset_access). Both answer the same 404 now, and a user who can open
the dataset gets what they got before."""

import asyncio
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import access, job_queue, main
from backend.routers import jobs, views
from backend.routers.auth import get_current_user
from backend.tests import test_field_registry_postgres
from backend.tests.test_field_registry_postgres import ingest
from backend.tests.test_jobs_api import JOB_ID, job_row

registry_database = test_field_registry_postgres.registry_database
OWNER = 10
PRIVATE = {'id': 2, 'name': 'private', 'storage_config_id': None, 'owner_id': OWNER, 'is_global': False}
CALLER = {'id': 20, 'role': 'power_user'}

# (method, path, module whose db finds the row, the row) for every endpoint that finds a row by its own id.
ENDPOINTS = {
    'sample media': ('GET', '/api/samples/7/media', main, {'dataset_id': 2, 'path': None}),
    'view deletion': ('DELETE', '/api/views/7', views, {'user_id': OWNER, 'dataset_id': 2}),
    'job': ('GET', f'/api/jobs/{JOB_ID}', jobs, job_row(dataset_id=2, created_by=OWNER)),
    'job cancel': ('POST', f'/api/jobs/{JOB_ID}/cancel', jobs, job_row(dataset_id=2, created_by=OWNER)),
    'job resume': ('POST', f'/api/jobs/{JOB_ID}/resume', jobs, job_row(dataset_id=2, created_by=OWNER)),
    'job discard': ('POST', f'/api/jobs/{JOB_ID}/discard', jobs, job_row(dataset_id=2, created_by=OWNER)),
}
# What the caller gets once they may open the dataset: as before this change.
WITH_ACCESS = {
    'sample media': (404, "Sample 7 has no value for field 'filepath'"),
    'view deletion': (403, 'Not authorized to delete this view'),
    'job': (200, None),
    'job cancel': (
        403,
        'Only an administrator or the submitting power user can cancel a job '
        '(power users can also cancel map re-fits in their datasets)',
    ),
    'job resume': (403, 'Only an administrator or the submitting power user can resume a job'),
    'job discard': (403, 'Only an administrator or the submitting power user can discard a job'),
}


class Rows:
    def __init__(self, row, granted=None):
        self.row, self.granted = row, granted

    async def fetchrow(self, query, *args):
        return self.row

    async def fetchval(self, query, *args):
        return self.granted


@pytest.fixture
def ask(monkeypatch):
    """ask(endpoint, row, granted) → (status, body) of the endpoint for CALLER, the row found by its id (None: no
    such id) in a private dataset CALLER is granted (or not), with the real check_dataset_access."""
    monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value=CALLER))
    main.app.dependency_overrides[get_current_user] = lambda: CALLER

    def run(endpoint, row, granted):
        method, path, module, _ = ENDPOINTS[endpoint]
        monkeypatch.setattr(module, 'db', Rows(row))
        monkeypatch.setattr(access, 'db', Rows(PRIVATE, granted))
        response = TestClient(main.app).request(method, path)
        return response.status_code, response.json()

    try:
        yield run
    finally:
        main.app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_an_id_in_a_dataset_you_cannot_open_answers_as_a_missing_one(ask, endpoint):
    existing = ask(endpoint, ENDPOINTS[endpoint][3], granted=None)
    missing = ask(endpoint, None, granted=None)
    assert existing == missing
    assert existing[0] == 404


@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_a_user_who_can_open_the_dataset_is_unaffected(ask, endpoint):
    status, body = ask(endpoint, ENDPOINTS[endpoint][3], granted=1)
    status_wanted, detail_wanted = WITH_ACCESS[endpoint]
    assert status == status_wanted
    if detail_wanted is None:
        assert body['id'] == str(JOB_ID)
    else:
        assert body == {'detail': detail_wanted}
    assert ask(endpoint, None, granted=1)[0] == 404  # a missing id still is


def test_a_follower_deleting_a_view_it_did_not_make_still_hears_why(monkeypatch):
    """The followers router authorizes a follower's request by the dataset it follows, not its account's access."""
    monkeypatch.setattr(views, 'db', Rows({'user_id': OWNER, 'dataset_id': 2}))
    checked = AsyncMock(side_effect=AssertionError('a follower is not asked for dataset access'))
    monkeypatch.setattr(views, 'check_dataset_access', checked)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(views.remove_view(7, CALLER, check_access=False))
    assert (refused.value.status_code, refused.value.detail) == (403, 'Not authorized to delete this view')


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
def test_on_a_database_rows_of_a_dataset_you_cannot_open_answer_as_missing_ones(registry_database, monkeypatch):
    async def scenario(database):
        for module in (access, main, views, jobs):
            monkeypatch.setattr(module, 'db', database)
        monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value=CALLER))
        await database.execute(
            "INSERT INTO users (id, username, role) VALUES ($1, 'owner', 'power_user'), ($2, 'caller', 'power_user')",
            OWNER,
            CALLER['id'],
        )
        await database.execute('UPDATE datasets SET is_global = FALSE, owner_id = $1 WHERE id = 2', OWNER)
        sample = (await ingest([{}], dataset_id=2))['ids'][0]
        view = await database.fetchval(
            "INSERT INTO views (dataset_id, user_id, name, slug, content) VALUES (2, $1, 'theirs', 'theirs', '{}') "
            'RETURNING id',
            OWNER,
        )
        job = (await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=2, created_by=OWNER))['id']
        paths = [
            ('GET', '/api/samples/{}/media', sample, sample + 1000),
            ('DELETE', '/api/views/{}', view, view + 1000),
            ('GET', '/api/jobs/{}', job, uuid4()),
            ('POST', '/api/jobs/{}/cancel', job, uuid4()),
            ('POST', '/api/jobs/{}/resume', job, uuid4()),
            ('POST', '/api/jobs/{}/discard', job, uuid4()),
        ]
        main.app.dependency_overrides[get_current_user] = lambda: CALLER
        try:
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                for method, path, existing, missing in paths:
                    there = await client.request(method, path.format(existing))
                    absent = await client.request(method, path.format(missing))
                    assert (there.status_code, there.json()) == (absent.status_code, absent.json()), path
                    assert there.status_code == 404, path

                await database.execute('INSERT INTO dataset_access (dataset_id, user_id) VALUES (2, $1)', CALLER['id'])
                media = await client.get(f'/api/samples/{sample}/media', params={'field': 'nothing'})
                assert media.json() == {'detail': f"Sample {sample} has no value for field 'nothing'"}
                assert (await client.delete(f'/api/views/{view}')).status_code == 403
                assert (await client.get(f'/api/jobs/{job}')).json()['id'] == str(job)
                assert (await client.post(f'/api/jobs/{job}/cancel')).status_code == 403
        finally:
            main.app.dependency_overrides.pop(get_current_user, None)
        assert await database.fetchval('SELECT count(*) FROM views WHERE id = $1', view) == 1

    registry_database(scenario)
