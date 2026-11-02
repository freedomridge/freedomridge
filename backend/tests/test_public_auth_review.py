"""Public-mode authentication regression scenarios, using disposable PostgreSQL."""

import asyncio
import ipaddress
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import bcrypt
import httpx
import pytest
from fastapi import Depends

from backend import label_fields, passwords, public_settings
from backend.database import Database
from backend.request_limits import _guest_slots, guest_heavy
from backend.routers import auth, datasets
from backend.tests.test_public_auth import account, login_as
from backend.tests.test_public_auth import auth_database as _auth_database
from backend.tests.test_public_auth import registry_database as _registry_database

registry_database = _registry_database
auth_database = _auth_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


def test_real_job_service_cleans_without_mode_variables(auth_database, tmp_path):
    async def scenario(database, client):
        await account(database, 'runner-old-demo', demo=True, age=2)
        permanent = await account(database, 'runner-permanent', role='admin')
        await database.execute(
            "INSERT INTO sessions VALUES ('expired-runner',$1,NOW()-INTERVAL '1 hour',NOW())",
            permanent,
        )
        namespace = await database.fetchval('SELECT current_schema()')
        env = {
            'PATH': os.environ['PATH'],
            'PYTHONPATH': 'python:.',
            'DATABASE_URL': os.environ['FRIDGE_TEST_POSTGRES_DSN'] + '?search_path=' + namespace + ',public',
            'JOB_WORKER_SLOTS': '1',
            'JOB_POOL_MAX_SIZE': '4',
        }
        with (tmp_path / 'runner.log').open('w+') as output:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                '-m',
                'backend.job_service',
                env=env,
                cwd=Path(__file__).resolve().parents[2],
                stdout=output,
                stderr=output,
            )
            try:
                async with asyncio.timeout(20):
                    while await database.fetchval("SELECT count(*) FROM users WHERE username='runner-old-demo'"):
                        assert process.returncode is None
                        await asyncio.sleep(0.1)
                assert not await database.fetchval("SELECT 1 FROM sessions WHERE id='expired-runner'")
                assert await database.fetchval('SELECT 1 FROM users WHERE id=$1', permanent)
            finally:
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                output.seek(0)
                assert process.returncode == 0, output.read()

    auth_database(scenario)


@pytest.mark.parametrize('limit', ['hourly', 'live'])
def test_ip_burst_cannot_take_global_slots(auth_database, monkeypatch, limit):
    monkeypatch.setenv('FRIDGE_DEMO_IP_ALLOWLIST', '203.0.113.0/24')
    monkeypatch.setenv('FRIDGE_DEMO_ACCOUNT_CAP', '12')
    monkeypatch.setenv('FRIDGE_DEMO_IP_HOURLY_LIMIT', '3' if limit == 'hourly' else '100')
    monkeypatch.setenv('FRIDGE_DEMO_IP_LIVE_CAP', '3' if limit == 'live' else '100')
    monkeypatch.setattr(auth, '_TRUSTED_PROXIES', [ipaddress.ip_network('127.0.0.1/32')])

    async def scenario(database, client):
        responses = await asyncio.gather(
            *(client.post('/api/auth/demo-login', headers={'X-Real-IP': '192.0.2.1'}) for _ in range(80)),
        )
        assert sorted(r.status_code for r in responses) == [200] * 3 + [429] * 77
        assert all(r.headers.get('Retry-After') for r in responses if r.status_code == 429)
        assert (await client.post('/api/auth/demo-login', headers={'X-Real-IP': '192.0.2.2'})).status_code == 200
        assert await database.fetchval('SELECT count(*) FROM users WHERE is_demo_account') == 4
        assert await database.fetchval('SELECT count(*) FROM sessions') == 4
        # A deletion cannot reset the hourly counter; old events still count for an hour.
        if limit == 'hourly':
            await database.execute('DELETE FROM users WHERE is_demo_account')
            assert (await client.post('/api/auth/demo-login', headers={'X-Real-IP': '192.0.2.1'})).status_code == 429
            await database.execute("UPDATE demo_admissions SET created_at=NOW()-INTERVAL '61 minutes'")
            assert (await client.post('/api/auth/demo-login', headers={'X-Real-IP': '192.0.2.1'})).status_code == 200
        else:
            await database.execute("UPDATE demo_admissions SET created_at=NOW()-INTERVAL '61 minutes'")
            assert (await client.post('/api/auth/demo-login', headers={'X-Real-IP': '192.0.2.1'})).status_code == 429
            await database.execute('DELETE FROM users WHERE is_demo_account')
            assert (await client.post('/api/auth/demo-login', headers={'X-Real-IP': '192.0.2.1'})).status_code == 200

    auth_database(scenario)


def test_allowlisted_admissions_still_take_global_slots(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_DEMO_ACCOUNT_CAP', '4')
    monkeypatch.setenv('FRIDGE_DEMO_IP_HOURLY_LIMIT', '1')
    monkeypatch.setenv('FRIDGE_DEMO_IP_LIVE_CAP', '1')
    monkeypatch.setenv('FRIDGE_DEMO_IP_ALLOWLIST', '203.0.113.0/24')
    monkeypatch.setattr(auth, '_TRUSTED_PROXIES', [ipaddress.ip_network('127.0.0.1/32')])

    async def scenario(database, client):
        responses = await asyncio.gather(
            *(client.post('/api/auth/demo-login', headers={'X-Real-IP': '203.0.113.9'}) for _ in range(8)),
        )
        assert sorted(response.status_code for response in responses) == [200] * 4 + [503] * 4
        assert await database.fetchval('SELECT count(*) FROM users WHERE is_demo_account') == 4
        assert await database.fetchval('SELECT count(*) FROM sessions') == 4
        assert await database.fetchval("SELECT count(*) FROM demo_admissions WHERE client_ip='203.0.113.9'") == 4

    auth_database(scenario)


def test_untrusted_forwarding_headers_cannot_evade_admission(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_DEMO_IP_HOURLY_LIMIT', '2')
    monkeypatch.setattr(auth, '_TRUSTED_PROXIES', [])

    async def scenario(database, client):
        responses = [await client.post('/api/auth/demo-login', headers={'X-Real-IP': f'192.0.2.{i}'}) for i in range(5)]
        assert [r.status_code for r in responses] == [200, 200, 429, 429, 429]
        assert await database.fetchval('SELECT count(DISTINCT client_ip) FROM demo_admissions') == 1

    auth_database(scenario)


@pytest.mark.parametrize('quota', ['bytes', 'count'])
def test_guest_save_quota_is_atomic_and_owner_wide(auth_database, monkeypatch, quota):
    monkeypatch.setenv('FRIDGE_GUEST_VIEW_TOTAL_BYTES', '1000' if quota == 'bytes' else '2097152')
    monkeypatch.setenv('FRIDGE_GUEST_VIEW_COUNT', '3' if quota == 'count' else '200')
    monkeypatch.setenv('FRIDGE_GUEST_CONCURRENCY', '100')

    async def scenario(database, client):
        owner = await account(database, 'quota-owner', demo=True)
        login_as(client, 'quota-owner')
        results = await asyncio.gather(
            *(
                client.post(f'/api/datasets/{1 + i % 2}/views', json={'name': f'v{i}', 'content': {'blob': 'x' * 280}})
                for i in range(30)
            ),
        )
        assert sum(r.status_code == 200 for r in results) == 3
        assert all(r.status_code in (200, 413 if quota == 'bytes' else 429) for r in results)
        rejected = next(r for r in results if r.status_code != 200)
        assert 'quota' in rejected.json()['detail']
        saved = await database.fetchval('SELECT count(*) FROM views WHERE user_id=$1', owner)
        assert saved == 3
        view = next(r.json() for r in results if r.status_code == 200)
        assert (await client.get(f"/api/views/{view['slug']}")).status_code == 200
        assert (await client.delete(f"/api/views/{view['id']}")).status_code == 200
        assert (
            await client.post('/api/datasets/1/views', json={'name': 'replacement', 'content': {}})
        ).status_code == 200
        await account(database, 'quota-other', demo=True)
        login_as(client, 'quota-other')
        assert (await client.post('/api/datasets/1/views', json={'name': 'mine', 'content': {}})).status_code == 200

    auth_database(scenario)


@pytest.mark.parametrize('role', ['admin', 'power_user'])
@pytest.mark.parametrize('chunked', [False, True])
def test_privileged_saves_are_not_guest_bounded(auth_database, monkeypatch, role, chunked):
    import json

    monkeypatch.setenv('FRIDGE_GUEST_VIEW_TOTAL_BYTES', '5')
    monkeypatch.setenv('FRIDGE_GUEST_VIEW_COUNT', '1')

    async def scenario(database, client):
        await account(database, 'unbounded', role=role)
        login_as(client, 'unbounded')
        for i in range(2):
            body = json.dumps({'name': f'large-{i}', 'content': {'blob': 'x' * 300000}}).encode()

            async def chunks():
                yield body[:100000]
                yield body[100000:]

            response = await client.post(
                '/api/datasets/1/views',
                content=chunks() if chunked else body,
                headers={'Content-Type': 'application/json'},
            )
            assert response.status_code == 200, response.text

    auth_database(scenario)


def test_private_names_cannot_be_probed_or_reserved(auth_database):
    async def scenario(database, client):
        for name, role in [('view-admin', 'admin'), ('view-guest', 'guest'), ('view-other', 'guest')]:
            await account(database, name, role=role)
        for name in ('view-admin', 'view-guest', 'view-other'):
            login_as(client, name)
            assert (
                await client.post('/api/datasets/1/views', json={'name': 'Admin Secret Plan', 'content': {}})
            ).status_code == 200
            assert (
                await client.post('/api/datasets/1/views', json={'name': 'ADMIN SECRET PLAN', 'content': {}})
            ).status_code == 400
        login_as(client, 'view-guest')
        assert (await client.post('/api/datasets/1/views', json={'name': 'Reserved', 'content': {}})).status_code == 200
        login_as(client, 'view-admin')
        assert (await client.post('/api/datasets/1/views', json={'name': 'reserved', 'content': {}})).status_code == 200

    auth_database(scenario)


def test_tag_registry_identity_redaction(auth_database):
    async def scenario(database, client):
        owner = await account(database, 'tag-admin', role='admin')
        await account(database, 'tag-guest', demo=True)
        sample = await database.fetchval(
            "INSERT INTO samples(dataset_id,filepath,media_type,tags) "
            "VALUES (1,'tag.jpg','image',ARRAY['reviewed']) RETURNING id",
        )
        await database.execute(
            "INSERT INTO dataset_tags(dataset_id,field_name,tag,created_by_user_id) VALUES (1,'tags','reviewed',$1)",
            owner,
        )
        for name, expected in [('tag-guest', None), ('tag-admin', 'tag-admin')]:
            login_as(client, name)
            response = await client.post('/api/datasets/1/samples/tag-counts', json={'sample_ids': [sample]})
            assert response.status_code == 200, response.text
            assert response.json()['tag_info']['reviewed']['created_by'] == expected

    auth_database(scenario)


def test_tighter_route_timeout_and_sidebar_fallbacks(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_GUEST_STATEMENT_TIMEOUT_SECONDS', '0.2')
    monkeypatch.setattr(label_fields, 'DISCOVERY_TIMEOUT_MS', 20)

    async def scenario(database, client):
        app = client._transport.app

        @app.get('/fallback')
        async def fallback(user=Depends(auth.get_current_user)):
            async with database.pool.acquire() as connection, connection.transaction():
                await connection.execute("SET LOCAL statement_timeout='20ms'")
                assert await connection.fetchval('SHOW statement_timeout') == '20ms'
                # Run the sidebar helpers themselves with a real PostgreSQL timeout.
                result = await datasets._safe_filter_query(connection.fetchval('SELECT pg_sleep(1)'))
                assert result is None
            source, result = await datasets._facet_query(database.fetchval('SELECT pg_sleep(1)'), None)
            assert source in ('cached', 'out_of_budget') and result is None
            fields = await label_fields.discover_fields(database.pool, 1, '__heatmap_fields__')
            return {'cached': ['all', 'values'], 'discovered': fields}

        await account(database, 'fallback-guest')
        login_as(client, 'fallback-guest')
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                'SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
                'overlay-discovery:1:__heatmap_fields__',
            )
            started = time.monotonic()
            response = await client.get('/fallback')
            assert response.status_code == 200, response.text
            assert response.json() == {'cached': ['all', 'values'], 'discovered': []}
            assert time.monotonic() - started < 0.8

    auth_database(scenario)


def test_route_cannot_raise_guest_timeout_ceiling(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_GUEST_STATEMENT_TIMEOUT_SECONDS', '0.05')

    async def scenario(database, client):
        app = client._transport.app

        @app.get('/raised-budget')
        async def raised_budget(user=Depends(auth.get_current_user)):
            async with database.pool.acquire() as connection, connection.transaction():
                await connection.execute("SET LOCAL statement_timeout='10s'")
                await connection.fetchval('SELECT pg_sleep(1)')

        await account(database, 'ceiling-guest')
        login_as(client, 'ceiling-guest')
        started = time.monotonic()
        assert (await client.get('/raised-budget')).status_code == 504
        assert time.monotonic() - started < 0.5

    auth_database(scenario)


def test_concurrency_limit_releases_permits_on_completion(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_GUEST_CONCURRENCY', '1')
    monkeypatch.setenv('FRIDGE_GUEST_QUEUE_WAIT_SECONDS', '0.1')

    async def scenario(database, client):
        await account(database, 'concurrent-guest')
        await account(database, 'concurrent-admin', role='admin')
        await account(database, 'other-guest')
        entered, finish = asyncio.Event(), asyncio.Event()
        app = client._transport.app

        @app.post('/api/search')
        @guest_heavy
        async def hold(user=Depends(auth.get_current_user)):
            if user['username'] == 'concurrent-guest':
                entered.set()
                await finish.wait()
            return {'ok': True}

        login_as(client, 'concurrent-guest')
        first = asyncio.create_task(client.post('/api/search'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            started = time.monotonic()
            response = await client.post('/api/search')
            assert response.status_code == 503 and response.headers['Retry-After']
            assert time.monotonic() - started >= 0.1
            assert (await client.get('/api/auth/me')).status_code == 200
            assert (
                await client.post('/api/datasets/1/views', json={'name': 'While busy', 'content': {}})
            ).status_code == 200
            async with httpx.AsyncClient(transport=client._transport, base_url='http://auth-test') as other:
                for name in ('concurrent-admin', 'other-guest'):
                    login_as(other, name)
                    assert (await other.post('/api/search')).status_code == 200
        finally:
            finish.set()
            await first
        assert (await client.post('/api/search')).status_code == 200
        assert not _guest_slots

    auth_database(scenario)


def test_disconnect_cancels_postgres_query_and_releases_guest_permit(auth_database):
    async def scenario(database, client):
        await account(database, 'disconnect-guest')
        app = client._transport.app
        entered = asyncio.Event()

        @app.post('/api/search/patches')
        @guest_heavy
        async def disconnect_query(user=Depends(auth.get_current_user)):
            entered.set()
            await database.fetchval('SELECT pg_sleep(10) /* guest_disconnect_probe */')
            return {'ok': True}

        messages: asyncio.Queue = asyncio.Queue()
        await messages.put({'type': 'http.request', 'body': b'', 'more_body': False})
        scope = {
            'type': 'http',
            'asgi': {'version': '3.0'},
            'http_version': '1.1',
            'method': 'POST',
            'scheme': 'http',
            'path': '/api/search/patches',
            'query_string': b'',
            'headers': [(b'cookie', b'session_id=disconnect-guest')],
            'client': ('127.0.0.1', 12345),
            'server': ('auth-test', 80),
        }
        task = asyncio.create_task(app(scope, messages.get, AsyncMock()))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert _guest_slots
            async with asyncio.timeout(2):
                while not await database.fetchval(
                    "SELECT 1 FROM pg_stat_activity WHERE query LIKE 'SELECT pg_sleep(10)%' AND state='active'",
                ):
                    await asyncio.sleep(0.01)
            await messages.put({'type': 'http.disconnect'})
            await asyncio.wait_for(task, 1)
            assert not _guest_slots
            async with asyncio.timeout(2):
                while await database.fetchval(
                    "SELECT 1 FROM pg_stat_activity WHERE query LIKE 'SELECT pg_sleep(10)%' AND state='active'",
                ):
                    await asyncio.sleep(0.01)
            login_as(client, 'disconnect-guest')
            assert (await client.get('/api/auth/me')).status_code == 200
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    auth_database(scenario)


@pytest.mark.parametrize(
    'name',
    [
        'FRIDGE_GUEST_STATEMENT_TIMEOUT_SECONDS',
        'FRIDGE_DB_ACQUIRE_TIMEOUT_SECONDS',
        'FRIDGE_DEMO_ACCOUNT_CAP',
        'FRIDGE_DEMO_IP_HOURLY_LIMIT',
        'FRIDGE_DEMO_IP_LIVE_CAP',
        'FRIDGE_GUEST_VIEW_TOTAL_BYTES',
        'FRIDGE_GUEST_VIEW_COUNT',
        'FRIDGE_GUEST_CONCURRENCY',
        'FRIDGE_GUEST_QUEUE_WAIT_SECONDS',
    ],
)
@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', '30s', 'abc'])
def test_bad_settings_fail_before_opening_database(monkeypatch, name, value):
    monkeypatch.setattr(public_settings, 'settings', public_settings.settings)
    monkeypatch.setenv(name, value)
    pool = AsyncMock()
    monkeypatch.setattr(asyncpg, 'create_pool', pool)
    with pytest.raises(ValueError, match=name):
        asyncio.run(Database().connect())
    pool.assert_not_awaited()


def test_demo_and_unknown_login_pay_same_dummy_hash(auth_database, monkeypatch):
    calls = []

    def checkpw(password, hashed):
        calls.append(hashed)
        return False

    monkeypatch.setattr(bcrypt, 'checkpw', checkpw)

    async def scenario(database, client):
        await account(database, 'dummy-guest', demo=True)
        for username in ('dummy-guest', 'unknown-user'):
            assert (
                await client.post('/api/auth/login', json={'username': username, 'password': 'incorrect'})
            ).status_code == 401
        assert calls == [passwords.DUMMY_HASH.encode()] * 2

    auth_database(scenario)


def test_guest_queries_do_not_add_per_statement_round_trips(auth_database, monkeypatch):
    from backend.request_limits import RequestContext, request_context

    async def scenario(database, client):
        context = RequestContext(user={'id': 1, 'role': 'guest'})
        token = request_context.set(context)
        statements = []
        try:
            async with database.pool.acquire() as connection:
                with connection.query_logger(lambda record: statements.append(record.query)):
                    assert await connection.fetchval('SELECT 1') == 1
                    assert await connection.fetchval('SELECT 2') == 2
                    await asyncio.sleep(0)
            assert statements == ['SELECT 1', 'SELECT 2']
        finally:
            request_context.reset(token)

    auth_database(scenario)


@pytest.mark.parametrize('value', ['Admin123', 'ADMIN123'])
def test_production_reset_refuses_default_variants(monkeypatch, value):
    from reset_admin import reset_admin

    monkeypatch.setenv('FRIDGE_ENV', ' prod ')
    connection = AsyncMock()
    monkeypatch.setattr(asyncpg, 'connect', connection)
    with pytest.raises(RuntimeError, match='default'):
        asyncio.run(reset_admin(value))
    connection.assert_not_awaited()


def test_tokens_keep_privileged_access_and_guest_isolation(auth_database):
    async def scenario(database, client):
        await account(database, 'token-owner', role='power_user')
        await account(database, 'token-guest', demo=True)
        login_as(client, 'token-owner')
        response = await client.post('/api/tokens', json={'name': 'integration-token'})
        assert response.status_code == 200, response.text
        token = response.json()
        assert (await client.get('/api/tokens')).json()[0]['token'] is None
        login_as(client, 'token-guest')
        assert (await client.post('/api/tokens', json={'name': 'forbidden'})).status_code == 403
        assert (await client.get('/api/tokens')).json() == []
        assert (await client.delete(f"/api/tokens/{token['id']}")).status_code == 403
        client.cookies.clear()
        client.headers['Authorization'] = 'Bearer ' + token['token']
        assert (await client.get('/api/auth/me')).json()['role'] == 'power_user'
        assert (await client.get('/api/datasets/1/views')).status_code == 200
        assert (await client.delete(f"/api/tokens/{token['id']}")).status_code == 200
        assert (await client.get('/api/auth/me')).status_code == 401

    auth_database(scenario)
