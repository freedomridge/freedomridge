"""Compute workers: a worker token works on the worker endpoints and nowhere else; workers register, heartbeat
and are listed to their owner (and to admins); python -m fridge.worker keeps going through server trouble."""

import os
import secrets

import httpx
import pytest
import requests
from fastapi import Depends, FastAPI
from fridge import worker as sdk_worker

from backend import system_status
from backend.crypto import hash_token
from backend.routers import auth, tokens, workers
from backend.routers.auth import get_current_user
from backend.tests import test_field_registry_postgres

registry_database = test_field_registry_postgres.registry_database
needs_postgres = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL database',
)
CAPABILITIES = {'memory_bytes': 64 * 1024**3, 'cpus': 16, 'gpu': True}
REGISTRATION = {
    'name': 'desktop',
    'hostname': 'desk-1',
    'sdk_version': '1.3.1',
    'kinds': ['map_refit'],
    'capabilities': CAPABILITIES,
}


async def setup(database, monkeypatch):
    for module in (auth, tokens, workers):
        monkeypatch.setattr(module, 'db', database)
    await database.execute(
        """INSERT INTO users(id, username, role) VALUES
           (10, 'owner', 'power_user'), (11, 'visitor', 'guest'), (12, 'other', 'power_user'), (13, 'boss', 'admin')""",
    )


async def token(database, user_id, scope='full', active=True) -> dict:
    raw = secrets.token_urlsafe(32)
    await database.execute(
        'INSERT INTO api_tokens (user_id, token, name, scope, is_active) VALUES ($1, $2, $3, $4, $5)',
        user_id,
        hash_token(raw),
        f'{scope} token',
        scope,
        active,
    )
    return {'Authorization': f'Bearer {raw}'}


def application() -> FastAPI:
    app = FastAPI()
    app.include_router(tokens.router, prefix='/api')
    app.include_router(workers.router, prefix='/api')

    @app.get('/api/probe')
    async def probe(user: dict = Depends(get_current_user)):  # stands for every route behind get_current_user
        return {'id': user['id']}

    return app


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url='http://test')


@needs_postgres
def test_a_worker_token_works_on_the_worker_endpoints_and_nowhere_else(registry_database, monkeypatch):
    async def scenario(database):
        await setup(database, monkeypatch)
        worker, full = await token(database, 10, 'worker'), await token(database, 10)
        async with client() as api:
            refused = await api.get('/api/probe', headers=worker)
            assert refused.status_code == 403 and 'compute worker token' in refused.json()['detail']
            assert (await api.get('/api/tokens', headers=worker)).status_code == 403
            assert (await api.get('/api/probe', headers=full)).status_code == 200
            assert (await api.post('/api/workers/register', json=REGISTRATION, headers=full)).status_code == 403
            assert (await api.post('/api/workers/register', json=REGISTRATION)).status_code == 401
            revoked = await token(database, 10, 'worker', active=False)
            assert (await api.post('/api/workers/register', json=REGISTRATION, headers=revoked)).status_code == 401
            guest = await token(database, 11, 'worker')  # a guest can't run a worker, as it can't make tokens
            assert (await api.post('/api/workers/register', json=REGISTRATION, headers=guest)).status_code == 403
            assert (await api.post('/api/workers/register', json=REGISTRATION, headers=worker)).status_code == 200

    registry_database(scenario)


@needs_postgres
def test_a_power_user_makes_a_worker_token_and_sees_its_scope(registry_database, monkeypatch):
    async def scenario(database):
        await setup(database, monkeypatch)
        full = await token(database, 10)
        async with client() as api:
            made = await api.post('/api/tokens', json={'name': 'desktop worker', 'scope': 'worker'}, headers=full)
            assert made.status_code == 200 and made.json()['scope'] == 'worker'
            listed = {row['name']: row['scope'] for row in (await api.get('/api/tokens', headers=full)).json()}
            assert listed == {'desktop worker': 'worker', 'full token': 'full'}
            new = {'Authorization': f"Bearer {made.json()['token']}"}
            assert (await api.post('/api/workers/register', json=REGISTRATION, headers=new)).status_code == 200
            invalid = await api.post('/api/tokens', json={'name': 'x', 'scope': 'admin'}, headers=full)
            assert invalid.status_code == 422

    registry_database(scenario)


@needs_postgres
def test_workers_register_heartbeat_and_are_listed_to_their_owner(registry_database, monkeypatch):
    async def scenario(database):
        await setup(database, monkeypatch)
        worker, other_worker = await token(database, 10, 'worker'), await token(database, 12, 'worker')
        await database.execute("INSERT INTO workers (id, placement, name) VALUES ('runner-1', 'server', 'runner-1')")
        async with client() as api:
            other_kind = {**REGISTRATION, 'kinds': ['umap_fit']}
            unknown = await api.post('/api/workers/register', json=other_kind, headers=worker)
            assert unknown.status_code == 400 and 'map_refit' in unknown.json()['detail']
            registered = (await api.post('/api/workers/register', json=REGISTRATION, headers=worker)).json()
            worker_id = registered['worker_id']
            assert registered['heartbeat_seconds'] == workers.HEARTBEAT_SECONDS
            row = await database.fetchrow('SELECT * FROM workers WHERE id = $1', worker_id)
            assert (row['placement'], row['user_id'], row['name'], row['hostname'], row['sdk_version']) == (
                'compute',
                10,
                'desktop',
                'desk-1',
                '1.3.1',
            )
            assert row['capabilities'] == {**CAPABILITIES, 'kinds': ['map_refit']}

            await database.execute(
                "UPDATE workers SET heartbeat_at = clock_timestamp() - interval '5 minutes' WHERE id = $1",
                worker_id,
            )
            assert (await system_status.worker_health(database))['compute']['online'] == 0
            assert (await api.post(f'/api/workers/{worker_id}/heartbeat', headers=worker)).status_code == 200
            assert (await system_status.worker_health(database))['compute']['online'] == 1
            # Another user's worker token can't keep this worker alive; a dropped worker is asked to register again.
            assert (await api.post(f'/api/workers/{worker_id}/heartbeat', headers=other_worker)).status_code == 404
            assert (await api.post('/api/workers/runner-1/heartbeat', headers=worker)).status_code == 404

            def ids(response):
                assert response.status_code == 200
                return {item['id'] for item in response.json()}

            assert ids(await api.get('/api/workers', headers=await token(database, 13))) == {worker_id, 'runner-1'}
            assert ids(await api.get('/api/workers', headers=await token(database, 10))) == {worker_id}
            assert ids(await api.get('/api/workers', headers=await token(database, 12))) == set()
            assert (await api.get('/api/workers', headers=await token(database, 11))).status_code == 403
            listed = (await api.get('/api/workers', headers=await token(database, 10))).json()[0]
            assert listed['owner'] == 'owner' and listed['online'] is True

    registry_database(scenario)


class FakeServer:
    """The client calls python -m fridge.worker makes, answering each with the next planned outcome: None (ok),
    an HTTP status, 'down' or 'stalled'."""

    base_url = 'http://server/api'

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def answer(self, call):
        self.calls.append(call)
        outcome = self.outcomes.pop(0)
        if outcome == 'down':
            raise requests.ConnectionError('connection refused')
        if outcome == 'stalled':
            raise requests.ReadTimeout('read timed out')  # not a ConnectionError
        if isinstance(outcome, int):
            response = requests.Response()
            response.status_code = outcome
            raise requests.HTTPError(f'{outcome} from the server', response=response)

    def register_worker(self, name, kinds, capabilities, hostname, sdk_version):
        self.answer('register')
        return {'worker_id': f'compute-{len(self.calls)}', 'heartbeat_seconds': 20}

    def claim_worker_job(self, worker_id, wait_seconds):
        self.answer(f'claim {worker_id}')  # no job came within the wait


def test_the_worker_retries_registers_again_when_dropped_and_stops_on_a_refused_token():
    server = FakeServer(['down', None, None, 503, 'stalled', 429, 404, None, None, 401])
    waits: list[float] = []
    with pytest.raises(requests.HTTPError, match='401'):
        sdk_worker.run(server, 'desk', ['map_refit'], sleep=waits.append)
    assert server.calls == [
        'register',  # the server is down: try again
        'register',
        'claim compute-2',  # each claim waits on the server and keeps the worker online
        'claim compute-2',  # 503 while the server restarts: keep the registration
        'claim compute-2',  # no answer in time
        'claim compute-2',  # 429, busy
        'claim compute-2',  # 404: the server dropped it
        'register',
        'claim compute-8',
        'claim compute-8',  # 401: the token was revoked
    ]
    # A wait only after a failure: a claim that found no job already waited on the server.
    assert waits == [sdk_worker.RETRY_SECONDS] * 5


def test_the_worker_exits_when_idle_counting_from_its_last_job():
    now = [0.0]
    server = FakeServer([None, None, 'down', None, None])
    worked = []

    def claim(worker_id, wait_seconds):
        server.answer(f'claim {worker_id}')
        now[0] += wait_seconds  # each claim waits on the server
        return {'id': 7, 'lease_token': 't'} if len(server.calls) == 2 else None

    def work(client, worker_id, job):
        now[0] += 500  # the job takes a while: not idle
        worked.append(job['id'])
        return True

    setattr(server, 'claim_worker_job', claim)  # this test's claims, which advance the clock
    sdk_worker.run(
        server,
        'desk',
        ['map_refit'],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        work=work,
        idle_seconds=60,
        clock=lambda: now[0],
    )
    # A job at the first claim; then idle from its end: the server down (20 s), then two claims (25 s each): 70 s.
    assert worked == [7]
    assert server.calls == ['register', 'claim compute-1', 'claim compute-1', 'claim compute-1', 'claim compute-1']
    assert now[0] - 525 >= 60


def test_the_worker_stops_on_a_mistake_only_a_person_can_fix():
    server = FakeServer([400])  # e.g. a --kinds the server doesn't have
    with pytest.raises(requests.HTTPError, match='400'):
        sdk_worker.run(server, 'desk', ['map_refitt'], sleep=lambda seconds: None)
    assert server.calls == ['register']
