"""Compute workers claim jobs over the API: a lease per job, renewed by the job heartbeat, ended by complete,
fail or release; only kinds that run remotely, and only on datasets the worker's owner can access."""

import asyncio
import os
import secrets
import uuid

import httpx
import pytest
from fastapi import FastAPI

from backend import job_queue
from backend.crypto import hash_token
from backend.job_queue import JobPolicy
from backend.routers import auth, workers
from backend.tests import test_field_registry_postgres

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL database',
)
KIND = 'test_compute'


async def setup(database, monkeypatch):
    """Users 10 (power user), 12 (another power user) and 13 (admin), each with a worker token; dataset 2 is
    private to user 12; KIND runs remotely."""
    for module in (auth, workers):
        monkeypatch.setattr(module, 'db', database)
    monkeypatch.setattr(workers, 'COMPUTE_KINDS', (*workers.COMPUTE_KINDS, KIND))
    remote = workers.RemoteKind(policy=JobPolicy(max_failures=2, retry_seconds=1))
    monkeypatch.setattr(workers, 'REMOTE_KINDS', {KIND: remote})
    await database.execute(
        "INSERT INTO users(id, username, role) VALUES (10, 'owner', 'power_user'), (12, 'other', 'power_user'), "
        "(13, 'boss', 'admin')",
    )
    await database.execute('UPDATE datasets SET is_global = FALSE, owner_id = 12 WHERE id = 2')
    headers = {}
    for user_id in (10, 12, 13):
        raw = secrets.token_urlsafe(32)
        await database.execute(
            "INSERT INTO api_tokens (user_id, token, name, scope) VALUES ($1, $2, 'worker', 'worker')",
            user_id,
            hash_token(raw),
        )
        headers[user_id] = {'Authorization': f'Bearer {raw}'}
    return headers


def client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(workers.router, prefix='/api')
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')


async def register(api, headers, kinds=(KIND,)) -> str:
    registration = {'name': 'desk', 'kinds': list(kinds)}
    response = await api.post('/api/workers/register', json=registration, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['worker_id']


async def enqueue(database, dataset_id=1, kind=KIND):
    return await job_queue.enqueue_job(database.pool, kind, dataset_id=dataset_id, placement='compute')


def test_a_worker_claims_renews_reports_and_completes_a_job(registry_database, monkeypatch):
    async def scenario(database):
        headers = await setup(database, monkeypatch)
        async with client() as api:
            worker = await register(api, headers[10])
            await database.execute(
                "UPDATE workers SET heartbeat_at = clock_timestamp() - interval '5 minutes' WHERE id = $1",
                worker,
            )
            assert (await api.post(f'/api/workers/{worker}/claim', json={}, headers=headers[10])).status_code == 204
            # Even a claim that finds nothing keeps the worker online.
            assert await database.fetchval(
                "SELECT heartbeat_at > clock_timestamp() - interval '5 seconds' FROM workers WHERE id = $1",
                worker,
            )
            queued = await enqueue(database)
            claimed = await api.post(f'/api/workers/{worker}/claim', json={}, headers=headers[10])
            assert claimed.status_code == 200
            job = claimed.json()
            assert (job['id'], job['kind'], job['dataset_id']) == (str(queued['id']), KIND, 1)
            assert (job['lease_seconds'], job['heartbeat_seconds']) == (
                workers.LEASE_SECONDS,
                workers.JOB_HEARTBEAT_SECONDS,
            )
            assert await database.fetchval('SELECT current_job_id FROM workers WHERE id = $1', worker) == queued['id']
            lease = job['lease_token']

            before = await database.fetchval('SELECT lease_expires_at FROM jobs WHERE id = $1', queued['id'])
            beat = await api.post(
                f'/api/workers/{worker}/jobs/{job["id"]}/heartbeat',
                json={'lease_token': lease, 'done': 3, 'total': 10, 'message': 'fitting'},
                headers=headers[10],
            )
            assert beat.json() == {'cancel_requested': False, 'lease_seconds': workers.LEASE_SECONDS}
            row = await database.fetchrow('SELECT * FROM jobs WHERE id = $1', queued['id'])
            assert row['lease_expires_at'] > before
            assert (row['progress_done'], row['progress_total'], row['message']) == (3, 10, 'fitting')

            # Only the lease holder reports: a wrong lease, or another worker of the same owner, gets 409.
            wrong = {'lease_token': str(uuid.uuid4())}
            assert (
                await api.post(f'/api/workers/{worker}/jobs/{job["id"]}/heartbeat', json=wrong, headers=headers[10])
            ).status_code == 409
            second = await register(api, headers[10])
            stolen = await api.post(
                f'/api/workers/{second}/jobs/{job["id"]}/complete',
                json={'lease_token': lease},
                headers=headers[10],
            )
            assert stolen.status_code == 409

            done = await api.post(
                f'/api/workers/{worker}/jobs/{job["id"]}/complete',
                json={'lease_token': lease, 'result': {'points': 7}},
                headers=headers[10],
            )
            assert done.json() == {'state': 'succeeded'}
            row = await database.fetchrow('SELECT state, result FROM jobs WHERE id = $1', queued['id'])
            assert (row['state'], row['result']) == ('succeeded', {'points': 7})
            assert await database.fetchval('SELECT current_job_id FROM workers WHERE id = $1', worker) is None
            again = await api.post(
                f'/api/workers/{worker}/jobs/{job["id"]}/complete',
                json={'lease_token': lease},
                headers=headers[10],
            )
            assert again.status_code == 409

    registry_database(scenario)


def test_a_worker_claims_only_remote_kinds_on_datasets_its_owner_can_access(registry_database, monkeypatch):
    async def scenario(database):
        headers = await setup(database, monkeypatch)
        async with client() as api:
            mine = await register(api, headers[10])
            await enqueue(database, kind='map_refit')  # a compute kind this worker didn't register for
            private = await enqueue(database, dataset_id=2)  # dataset 2 is user 12's
            assert (await api.post(f'/api/workers/{mine}/claim', json={}, headers=headers[10])).status_code == 204
            theirs = await register(api, headers[12])
            claimed = await api.post(f'/api/workers/{theirs}/claim', json={}, headers=headers[12])
            assert claimed.json()['id'] == str(private['id'])
            # A job on no dataset: only an admin's worker.
            unscoped = await job_queue.enqueue_job(database.pool, KIND, placement='compute')
            assert (await api.post(f'/api/workers/{mine}/claim', json={}, headers=headers[10])).status_code == 204
            admins = await register(api, headers[13])
            assert (await api.post(f'/api/workers/{admins}/claim', json={}, headers=headers[13])).json()['id'] == str(
                unscoped['id'],
            )
            # And a worker can't claim for another user's worker.
            assert (await api.post(f'/api/workers/{theirs}/claim', json={}, headers=headers[10])).status_code == 404

    registry_database(scenario)


def test_a_claim_waits_for_a_job_that_arrives(registry_database, monkeypatch):
    async def scenario(database):
        headers = await setup(database, monkeypatch)
        async with client() as api:
            worker = await register(api, headers[10])

            async def arrive():
                await asyncio.sleep(0.5)
                return await enqueue(database)

            claimed, queued = await asyncio.gather(
                api.post(f'/api/workers/{worker}/claim', json={'wait_seconds': 5}, headers=headers[10]),
                arrive(),
            )
            assert claimed.status_code == 200 and claimed.json()['id'] == str(queued['id'])
            too_long = await api.post(f'/api/workers/{worker}/claim', json={'wait_seconds': 60}, headers=headers[10])
            assert too_long.status_code == 422

    registry_database(scenario)


def test_cancel_failures_release_and_an_expired_lease(registry_database, monkeypatch):
    async def scenario(database):
        headers = await setup(database, monkeypatch)
        async with client() as api:
            worker = await register(api, headers[10])

            async def claim():
                response = await api.post(f'/api/workers/{worker}/claim', json={}, headers=headers[10])
                assert response.status_code == 200, response.text
                return response.json()

            def path(job, action):
                return f'/api/workers/{worker}/jobs/{job["id"]}/{action}'

            # A cancel reaches the worker through its heartbeat; releasing then ends the job cancelled.
            queued = await enqueue(database)
            job = await claim()
            await job_queue.cancel_job(database.pool, queued['id'])
            beat = await api.post(path(job, 'heartbeat'), json={'lease_token': job['lease_token']}, headers=headers[10])
            assert beat.json()['cancel_requested'] is True
            released = await api.post(
                path(job, 'release'),
                json={'lease_token': job['lease_token']},
                headers=headers[10],
            )
            assert released.json() == {'state': 'cancelled'}

            # A retryable failure goes back to the queue with its error; another kind of failure ends it.
            queued = await enqueue(database)
            job = await claim()
            error = {'type': 'MemoryError', 'message': 'the machine ran short'}
            retry = await api.post(
                path(job, 'fail'),
                json={'lease_token': job['lease_token'], 'error': error, 'retryable': True},
                headers=headers[10],
            )
            assert retry.json() == {'state': 'queued'}
            row = await database.fetchrow('SELECT failures, error FROM jobs WHERE id = $1', queued['id'])
            assert (row['failures'], row['error']) == (1, error)
            await database.execute('UPDATE jobs SET available_at = clock_timestamp() WHERE id = $1', queued['id'])
            job = await claim()
            failed = await api.post(
                path(job, 'fail'),
                json={'lease_token': job['lease_token'], 'error': {'type': 'ValueError', 'message': 'bad input'}},
                headers=headers[10],
            )
            assert failed.json() == {'state': 'failed'}

            # Released without a cancel: queued again for any worker.
            queued = await enqueue(database)
            job = await claim()
            released = await api.post(
                path(job, 'release'),
                json={'lease_token': job['lease_token']},
                headers=headers[10],
            )
            assert released.json() == {'state': 'queued'}

            # A worker that stopped renewing loses the job, which counts a failure: the next claim takes it back
            # with a new lease (here at once: the retry delay already passed), and the old lease is refused.
            stale = await claim()
            await database.execute(
                "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = $1",
                stale['id'],
            )
            retaken = await claim()
            assert retaken['id'] == stale['id'] and retaken['lease_token'] != stale['lease_token']
            assert await database.fetchval('SELECT failures FROM jobs WHERE id = $1', stale['id']) == 1
            late = await api.post(
                path(stale, 'complete'),
                json={'lease_token': stale['lease_token']},
                headers=headers[10],
            )
            assert late.status_code == 409
            done = await api.post(
                path(retaken, 'complete'),
                json={'lease_token': retaken['lease_token']},
                headers=headers[10],
            )
            assert done.json() == {'state': 'succeeded'}

    registry_database(scenario)
