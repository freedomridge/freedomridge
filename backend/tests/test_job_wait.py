"""Waiting POSTs share job visibility and honor the response deadline even with a busy pool."""

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from backend import access, index_actions, index_jobs, job_queue, job_wait
from backend.routers import auth, datasets, jobs
from backend.routers.auth import get_current_user
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
INTERNAL_ERROR = 'INTERNAL_SENTINEL: connection to internal-db.example lost'
REDACTED = 'The job failed; an administrator can see why'


@pytest.mark.parametrize('requester', ['other', 'submitter', 'admin'])
@pytest.mark.parametrize(
    'error_type,status_code',
    [
        ('RuntimeError', 500),
        ('JobInputError', 400),
        ('JobNotFound', 404),
        ('JobConflict', 409),
        ('JobUnprocessable', 422),
        ('JobInsufficientStorage', 507),
    ],
)
def test_joined_post_uses_same_error_visibility_as_status_and_history(
    registry_database,
    monkeypatch,
    requester,
    error_type,
    status_code,
):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute('UPDATE datasets SET is_global=TRUE WHERE id=1')
        first = await database.fetchval(
            "INSERT INTO users(username,role) VALUES('alice','power_user') RETURNING id",
        )
        second = await database.fetchval(
            "INSERT INTO users(username,role) VALUES('bob','power_user') RETURNING id",
        )
        alice = {'id': first, 'role': 'power_user'}
        user = (
            alice
            if requester == 'submitter'
            else {
                'id': second,
                'role': 'admin' if requester == 'admin' else 'power_user',
            }
        )
        monkeypatch.setattr(auth, 'get_optional_user', AsyncMock(return_value=user))
        await database.execute(
            "INSERT INTO dataset_fields(dataset_id,field_name,field_type) VALUES(1,'score','number')",
        )
        submitted = await datasets.promote_field(1, 'score', alice, wait=False)
        identifier = UUID(json.loads(submitted.body)['job_id'])
        error = {'type': error_type, 'message': INTERNAL_ERROR if error_type == 'RuntimeError' else 'Invalid input'}
        original_submit = index_jobs.submit_field_index

        async def join_then_fail(*args, **kwargs):
            joined = await original_submit(*args, **kwargs)
            assert joined['id'] == identifier
            assert joined['created_by'] == first
            await database.execute(
                "UPDATE jobs SET state='failed',finished_at=clock_timestamp(),error=$2 WHERE id=$1",
                identifier,
                error,
            )
            return joined

        monkeypatch.setattr(index_jobs, 'submit_field_index', join_then_fail)
        application = FastAPI()
        application.include_router(datasets.router, prefix='/api')
        application.include_router(jobs.router, prefix='/api')
        application.dependency_overrides[get_current_user] = lambda: user
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url='http://job-wait.test',
        ) as client:
            response = await client.post('/api/datasets/1/fields/score/promote')
            status = await client.get(f'/api/jobs/{identifier}')
            history = await client.get('/api/datasets/1/jobs')
        expected = REDACTED if requester == 'other' and error_type == 'RuntimeError' else error['message']
        assert response.status_code == status_code
        assert response.json() == {'detail': expected}
        assert status.status_code == history.status_code == 200
        assert status.json()['error']['message'] == expected
        assert history.json()['items'][0]['error']['message'] == expected

    registry_database(scenario)


def test_five_second_answer_is_bounded_when_pool_is_exhausted(registry_database, monkeypatch):
    async def scenario(database):
        user_id = await database.fetchval(
            "INSERT INTO users(username,role) VALUES('alice','power_user') RETURNING id",
        )
        user = {'id': user_id, 'role': 'power_user'}
        await database.execute(
            "INSERT INTO dataset_fields(dataset_id,field_name,field_type) VALUES(1,'score','number')",
        )
        original_submit = index_jobs.submit_field_index
        held = []
        queued = None

        async def submit_then_exhaust(*args, **kwargs):
            nonlocal queued
            queued = await original_submit(*args, **kwargs)
            for _ in range(database.pool.get_max_size()):
                held.append(await database.pool.acquire())
            return queued

        monkeypatch.setattr(index_jobs, 'submit_field_index', submit_then_exhaust)
        started = time.monotonic()
        try:
            try:
                response = await asyncio.wait_for(datasets.promote_field(1, 'score', user), timeout=6)
            except TimeoutError:
                pytest.fail('The five-second response still waited for a pooled connection after six seconds')
            assert time.monotonic() - started < job_wait.ANSWER_SECONDS + 0.75
            assert response.status_code == 202
            assert queued is not None
            assert json.loads(response.body) == {'job_id': str(queued['id']), 'state': 'queued'}
            assert not database.pool._queue._getters
        finally:
            for connection in held:
                await database.pool.release(connection)
        assert database.pool.get_idle_size() == database.pool.get_size()
        assert queued is not None
        durable = await database.fetchrow(
            'SELECT state,cancel_requested FROM jobs JOIN job_controls ON job_id=id WHERE id=$1',
            queued['id'],
        )
        assert dict(durable) == {'state': 'queued', 'cancel_requested': False}

    registry_database(scenario)


def test_slow_query_uses_only_remaining_response_budget(registry_database):
    async def scenario(database):
        queued = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        async with database.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute('LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE')
                started = time.monotonic()
                response = await asyncio.wait_for(
                    job_wait.answer(
                        database.pool,
                        queued,
                        started - job_wait.ANSWER_SECONDS + 0.2,
                        user={'id': 1, 'role': 'admin'},
                    ),
                    timeout=1,
                )
                assert time.monotonic() - started < 0.75
                assert response.status_code == 202
                assert json.loads(response.body) == {'job_id': str(queued['id']), 'state': 'queued'}
                assert (
                    await database.fetchval(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND query LIKE 'SELECT id,%jobs%'",
                    )
                    == 0
                )
        assert database.pool.get_idle_size() == database.pool.get_size()
        durable = await database.fetchrow(
            'SELECT state,cancel_requested FROM jobs JOIN job_controls ON job_id=id WHERE id=$1',
            queued['id'],
        )
        assert dict(durable) == {'state': 'queued', 'cancel_requested': False}

    registry_database(scenario)


@pytest.mark.parametrize('wait', [True, False])
def test_expired_budget_or_no_wait_returns_handle_without_reading_pool(registry_database, wait):
    async def scenario(database):
        queued = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        pool = AsyncMock()
        response = await job_wait.answer(
            pool,
            queued,
            time.monotonic() - job_wait.ANSWER_SECONDS - 1 if wait else time.monotonic(),
            user={'id': 1, 'role': 'admin'},
            wait=wait,
        )
        assert response.status_code == 202
        assert json.loads(response.body) == {'job_id': str(queued['id']), 'state': 'queued'}
        pool.fetchrow.assert_not_called()

    registry_database(scenario)


@pytest.mark.parametrize('outcome', ['succeeded', 'JobConflict'])
def test_joined_demote_returns_public_outcome(registry_database, monkeypatch, outcome):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute('UPDATE datasets SET is_global=TRUE WHERE id=1')
        first = await database.fetchval(
            "INSERT INTO users(username,role) VALUES('alice','power_user') RETURNING id",
        )
        second = await database.fetchval(
            "INSERT INTO users(username,role) VALUES('bob','power_user') RETURNING id",
        )
        alice = {'id': first, 'role': 'power_user'}
        bob = {'id': second, 'role': 'power_user'}
        monkeypatch.setattr(auth, 'get_optional_user', AsyncMock(return_value=bob))
        await database.execute(
            "INSERT INTO dataset_fields(dataset_id,field_name,field_type,is_promoted) VALUES(1,'score','number',TRUE)",
        )
        submitted = await datasets.demote_field(1, 'score', alice, wait=False)
        identifier = UUID(json.loads(submitted.body)['job_id'])
        result = {'message': 'Field score demoted successfully'}
        error = {'type': 'JobConflict', 'message': 'idx must stay now: needed; nothing was dropped'}
        original_submit = index_actions.submit_drop

        async def join_then_finish(*args, **kwargs):
            joined = await original_submit(*args, **kwargs)
            assert joined['id'] == identifier
            assert joined['created_by'] == first
            await database.execute(
                'UPDATE jobs SET state=$2,finished_at=clock_timestamp(),result=$3,error=$4 WHERE id=$1',
                identifier,
                'succeeded' if outcome == 'succeeded' else 'failed',
                result if outcome == 'succeeded' else None,
                error if outcome != 'succeeded' else None,
            )
            return joined

        monkeypatch.setattr(index_actions, 'submit_drop', join_then_finish)
        application = FastAPI()
        application.include_router(datasets.router, prefix='/api')
        application.include_router(jobs.router, prefix='/api')
        application.dependency_overrides[get_current_user] = lambda: bob
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url='http://job-wait.test',
        ) as client:
            response = await client.post('/api/datasets/1/fields/score/demote')
            status = await client.get(f'/api/jobs/{identifier}')
            history = await client.get('/api/datasets/1/jobs')
        assert response.status_code == (200 if outcome == 'succeeded' else 409)
        assert response.json() == (result if outcome == 'succeeded' else {'detail': error['message']})
        assert status.status_code == history.status_code == 200
        for entry in (status.json(), history.json()['items'][0]):
            assert entry['result' if outcome == 'succeeded' else 'error'] == (
                result if outcome == 'succeeded' else error
            )

    registry_database(scenario)
