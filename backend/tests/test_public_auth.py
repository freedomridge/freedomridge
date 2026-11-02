"""Public auth contracts against an explicitly supplied, disposable PostgreSQL server."""

import asyncio
import json
import os
import threading
from unittest.mock import AsyncMock

import asyncpg
import bcrypt
import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException

from backend import access, auth_cleanup, job_queue, passwords, public_settings
from backend import database as database_module
from backend.admin_bootstrap import bootstrap_admin
from backend.database import init_connection
from backend.request_limits import RequestConnection, RequestLimitsMiddleware, RequestPool
from backend.routers import auth, datasets, jobs, tokens, users, views
from backend.tests.test_field_registry_postgres import registry_database as _registry_database

registry_database = _registry_database

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


@pytest.fixture
def auth_database(registry_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_ENV', 'development')
    monkeypatch.setenv('DEMO_MODE', 'true')
    monkeypatch.delenv('FRIDGE_ADMIN_PASSWORD', raising=False)
    monkeypatch.setattr(auth, 'SESSION_COOKIE_SECURE', False)
    auth._LOGIN_FAILS.clear()

    def run(scenario):
        async def upgraded(database):
            monkeypatch.setattr(public_settings, 'settings', public_settings.settings)
            public_settings.load_settings()
            namespace = await database.fetchval('SELECT current_schema()')
            await database.pool.close()
            database.pool = RequestPool(
                await asyncpg.create_pool(
                    os.environ['FRIDGE_TEST_POSTGRES_DSN'],
                    min_size=1,
                    max_size=5,
                    init=init_connection,
                    connection_class=RequestConnection,
                    server_settings={'search_path': f'{namespace},public'},
                ),
            )
            for module in (access, auth, views, users, jobs, tokens, database_module):
                monkeypatch.setattr(module, 'db', database)
            app = FastAPI()
            app.add_middleware(RequestLimitsMiddleware)
            for router in (auth.router, views.router, users.router, jobs.router, tokens.router, datasets.router):
                app.include_router(router, prefix='/api')

            @app.get('/slow')
            async def slow(direct: bool = False, swallowed: bool = False, user=Depends(auth.get_current_user)):
                try:
                    if direct:
                        async with database.pool.acquire() as connection, connection.transaction():
                            await connection.execute('SELECT pg_sleep(0.08)')
                    else:
                        await database.fetchval('SELECT pg_sleep(0.08)')
                except (asyncpg.QueryCanceledError, asyncio.TimeoutError):
                    if swallowed:
                        raise HTTPException(500, 'An old route swallowed the timeout') from None
                    raise
                return {'ok': True}

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://auth-test') as client:
                await scenario(database, client)

        registry_database(upgraded)

    return run


async def account(database, name, role='guest', demo=False, age=0, password_hash=passwords.DEMO_PASSWORD):
    identifier = await database.fetchval(
        "INSERT INTO users (username,role,is_demo_account,password_hash,created_at) "
        "VALUES ($1,$2,$3,$4,NOW()-$5::int*INTERVAL '1 day') RETURNING id",
        name,
        role,
        demo,
        password_hash,
        age,
    )
    await database.execute(
        "INSERT INTO sessions(id,user_id,expires_at) VALUES ($1,$2,NOW()+INTERVAL '1 day')",
        name,
        identifier,
    )
    return identifier


def login_as(client, name):
    client.cookies.clear()
    client.cookies.set('session_id', name)


@pytest.mark.parametrize('password', [None, '', 'short', 'password', 'admin123', 'Admin123', 'ADMIN123'])
def test_production_refuses_seeded_admin(auth_database, monkeypatch, password):
    monkeypatch.setenv('FRIDGE_ENV', 'production')
    if password is not None:
        monkeypatch.setenv('FRIDGE_ADMIN_PASSWORD', password)

    async def scenario(database, client):
        with pytest.raises(RuntimeError, match='FRIDGE_ADMIN_PASSWORD'):
            await bootstrap_admin(database)
        assert await passwords.verify_password(
            'admin123',
            await database.fetchval("SELECT password_hash FROM users WHERE username='admin'"),
        )

    auth_database(scenario)


def test_bootstrap_rotates_once_and_invalidates_default_sessions(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_ENV', 'production')
    monkeypatch.setenv('FRIDGE_ADMIN_PASSWORD', 'new-admin-password')

    async def scenario(database, client):
        await database.execute("INSERT INTO sessions VALUES ('old-admin',1,NOW()+INTERVAL '1 day',NOW())")
        await bootstrap_admin(database)
        row = await database.fetchrow("SELECT * FROM users WHERE username='admin'")
        assert await passwords.verify_password('new-admin-password', row['password_hash'])
        assert not row['must_change_password']
        assert not await database.fetchval("SELECT 1 FROM sessions WHERE id='old-admin'")
        monkeypatch.setenv('FRIDGE_ADMIN_PASSWORD', 'another-password')
        await bootstrap_admin(database)
        assert row['password_hash'] == await database.fetchval('SELECT password_hash FROM users WHERE id=1')

    auth_database(scenario)


def test_development_bootstrap_and_guest_sharing_unchanged(auth_database, monkeypatch):
    monkeypatch.setenv('DEMO_MODE', 'false')
    monkeypatch.setenv('FRIDGE_ADMIN_PASSWORD', 'ignored-password')

    async def scenario(database, client):
        await bootstrap_admin(database)
        row = await database.fetchrow('SELECT * FROM users WHERE id=1')
        assert row['must_change_password']
        assert await passwords.verify_password('admin123', row['password_hash'])
        await account(database, 'local')
        login_as(client, 'local')
        response = await client.post(
            '/api/datasets/1/views',
            json={
                'name': 'local view',
                'content': {'large': 'x' * 270000},
                'is_public': True,
            },
        )
        assert response.status_code == 200 and response.json()['is_public']
        assert (await client.get('/api/users/names')).status_code == 200

    auth_database(scenario)


def test_startup_refusal_closes_pool(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_ENV', 'production')

    async def scenario(database, client):
        monkeypatch.setattr(database, 'connect', AsyncMock())
        monkeypatch.setattr(database, 'disconnect', AsyncMock())
        with pytest.raises(RuntimeError, match='FRIDGE_ADMIN_PASSWORD'):
            async with database_module.lifespan(FastAPI()):
                pytest.fail('Production startup admitted the seeded password')
        database.disconnect.assert_awaited_once()

    auth_database(scenario)


def test_guest_views_stay_private_and_owned(auth_database):
    async def scenario(database, client):
        await account(database, 'guest-one', demo=True)
        await account(database, 'guest-two', demo=True)
        login_as(client, 'guest-one')
        payload = {
            'name': 'my view',
            'content': {'filter': 'original'},
            'is_public': True,
            'shared_with_user_ids': [99999],
        }
        response = await client.post('/api/datasets/1/views', json=payload)
        assert response.status_code == 200, response.text
        view = response.json()
        assert not view['is_public'] and view['shared_with_user_ids'] == []
        path = f"/api/datasets/1/views/{view['id']}"
        assert (await client.get('/api/datasets/1/views')).json()[0]['id'] == view['id']
        assert (await client.get(f"/api/views/{view['slug']}")).status_code == 200
        assert (await client.patch(path, json={'content': {'filter': 'edited'}})).status_code == 404
        assert (await client.patch(path, json={'is_public': True})).status_code == 404
        assert (await client.patch(path, json={'shared_with_user_ids': [1]})).status_code == 404
        assert (
            await client.patch(path + '/share', json={'is_public': True, 'shared_with_user_ids': []})
        ).status_code == 403
        assert (await client.get('/api/users/names')).status_code == 403
        assert not (await client.get('/api/auth/me')).json()['can_share_views']
        login_as(client, 'guest-two')
        assert (await client.get('/api/datasets/1/views')).json() == []
        assert (await client.get(f"/api/views/{view['slug']}")).status_code == 404
        assert (await client.patch(path, json={'name': 'stolen'})).status_code == 404
        assert (await client.delete(f"/api/views/{view['id']}")).status_code == 403
        login_as(client, 'guest-one')
        assert (await client.get(f"/api/views/{view['slug']}")).json()['content'] == {'filter': 'original'}
        assert (await client.delete(f"/api/views/{view['id']}")).status_code == 200

    auth_database(scenario)


@pytest.mark.parametrize('chunked', [False, True])
def test_view_body_limit_before_json_decode(auth_database, chunked):
    async def scenario(database, client):
        await account(database, 'bounded')
        login_as(client, 'bounded')
        body = json.dumps({'name': 'bounded view', 'content': {}}).encode()
        body += b' ' * (views.MAX_VIEW_BODY_BYTES - len(body))
        assert (
            await client.post('/api/datasets/1/views', content=body, headers={'Content-Type': 'application/json'})
        ).status_code == 200

        async def chunks():
            yield body
            yield b'!'

        response = await client.post('/api/datasets/1/views', content=chunks() if chunked else body + b'!')
        assert response.status_code == 413
        view_id = await database.fetchval("SELECT id FROM views WHERE name='bounded view'")
        for path in (f'/api/datasets/1/views/{view_id}/share',):
            assert (await client.patch(path, content=body + b'!')).status_code == 413

    auth_database(scenario)


def test_demo_login_never_uses_bcrypt_and_password_change_forbidden(auth_database, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('Demo authentication must never call bcrypt')

    monkeypatch.setattr(bcrypt, 'hashpw', unexpected)
    monkeypatch.setattr(bcrypt, 'checkpw', unexpected)

    async def scenario(database, client):
        response = await client.post('/api/auth/demo-login')
        assert response.status_code == 200
        name = response.json()['username']
        assert (
            await database.fetchval('SELECT password_hash FROM users WHERE username=$1', name)
            == passwords.DEMO_PASSWORD
        )
        assert (
            await client.post(
                '/api/auth/change-password',
                json={
                    'current_password': '',
                    'new_password': 'permanent-password',
                },
            )
        ).status_code == 403
        assert await database.fetchval('SELECT is_demo_account FROM users WHERE username=$1', name)

    auth_database(scenario)


@pytest.mark.parametrize('forced', [False, True])
def test_password_change_requires_current_password_and_revokes_other_sessions(auth_database, forced):
    async def scenario(database, client):
        identifier = await account(database, 'permanent', password_hash=await passwords.hash_password('old-password'))
        await database.execute('UPDATE users SET must_change_password=$1 WHERE id=$2', forced, identifier)
        await database.execute(
            "INSERT INTO sessions VALUES ('other-session',$1,NOW()+INTERVAL '1 day',NOW())",
            identifier,
        )
        login_as(client, 'permanent')
        for current in ('', 'incorrect'):
            assert (
                await client.post(
                    '/api/auth/change-password',
                    json={
                        'current_password': current,
                        'new_password': 'new-password',
                    },
                )
            ).status_code == 400
        assert (
            await client.post(
                '/api/auth/change-password',
                json={
                    'current_password': 'old-password',
                    'new_password': 'new-password',
                },
            )
        ).status_code == 200
        assert not await database.fetchval('SELECT must_change_password FROM users WHERE id=$1', identifier)
        assert await database.fetchval('SELECT count(*) FROM sessions WHERE user_id=$1', identifier) == 1

    auth_database(scenario)


def test_demo_cap_is_atomic_across_concurrent_logins(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_DEMO_ACCOUNT_CAP', '2')

    async def scenario(database, client):
        results = await asyncio.gather(*(client.post('/api/auth/demo-login') for _ in range(12)))
        assert sorted(response.status_code for response in results) == [200] * 2 + [503] * 10
        assert 'limit' in next(response for response in results if response.status_code == 503).json()['detail']
        assert await database.fetchval('SELECT count(*) FROM users WHERE is_demo_account') == 2
        assert await database.fetchval('SELECT count(*) FROM sessions') == 2

    auth_database(scenario)


def test_scheduled_cleanup_deletes_owned_rows_and_expired_sessions(auth_database, monkeypatch):
    async def scenario(database, client):
        old = await account(database, 'old-demo', demo=True, age=2)
        young = await account(database, 'young-demo', demo=True)
        permanent = await account(database, 'old-permanent', age=2)
        await database.execute("INSERT INTO sessions VALUES ('expired',$1,NOW()-INTERVAL '1 minute',NOW())", permanent)
        await database.execute("INSERT INTO api_tokens(user_id,token,name) VALUES ($1,'old-token','legacy')", old)
        await database.execute('INSERT INTO dataset_access(dataset_id,user_id) VALUES (1,$1)', old)
        await database.execute(
            "INSERT INTO views(dataset_id,user_id,name,slug,is_public,shared_with_user_ids) VALUES "
            "(1,$1,'private','private',false,'{}'), (1,$1,'public','public',true,'{}'), "
            "(1,$2,'kept','kept',false,ARRAY[$1])",
            old,
            young,
        )
        monkeypatch.setenv('DEMO_MODE', 'false')
        assert await auth_cleanup.scheduled_cleanup(database.pool) == 1
        assert not await database.fetchval('SELECT 1 FROM users WHERE id=$1', old)
        for table in ('sessions', 'views', 'api_tokens', 'dataset_access'):
            assert await database.fetchval(f'SELECT count(*) FROM {table} WHERE user_id=$1', old) == 0
        assert await database.fetchval('SELECT count(*) FROM users WHERE id=ANY($1::int[])', [young, permanent]) == 2
        assert not await database.fetchval("SELECT 1 FROM sessions WHERE id='expired'")
        assert await database.fetchval("SELECT shared_with_user_ids FROM views WHERE slug='kept'") == []
        assert await auth_cleanup.scheduled_cleanup(database.pool) == 0

    auth_database(scenario)


@pytest.mark.parametrize('direct,swallowed', [(False, False), (True, False), (False, True)])
def test_guest_timeout_and_pool_reuse_leave_privileged_queries_unchanged(auth_database, monkeypatch, direct, swallowed):
    monkeypatch.setenv('FRIDGE_GUEST_STATEMENT_TIMEOUT_SECONDS', '0.02')

    async def scenario(database, client):
        for role in ('guest', 'admin', 'power_user'):
            await account(database, 'timeout-' + role, role=role)
            login_as(client, 'timeout-' + role)
            response = await client.get(
                '/slow',
                params={'direct': str(direct).lower(), 'swallowed': str(swallowed).lower()},
            )
            assert response.status_code == (504 if role == 'guest' else 200), response.text
            if role == 'guest':
                assert 'took too long' in response.json()['detail']
        assert await database.fetchval('SHOW statement_timeout') == '0'
        monkeypatch.setenv('DEMO_MODE', 'false')
        login_as(client, 'timeout-guest')
        assert (await client.get('/slow')).status_code == 200

    auth_database(scenario)


def test_exhausted_pool_returns_503(auth_database, monkeypatch):
    async def scenario(database, client):
        await account(database, 'waiting')
        login_as(client, 'waiting')
        holders = []
        try:
            for _ in range(5):
                holders.append(await database.pool.acquire())
            monkeypatch.setenv('FRIDGE_DB_ACQUIRE_TIMEOUT_SECONDS', '0.02')
            public_settings.load_settings()
            response = await asyncio.wait_for(client.get('/api/auth/me'), timeout=1)
            assert response.status_code == 503 and 'busy' in response.json()['detail']
        finally:
            await asyncio.gather(*(database.pool.release(holder) for holder in holders))
        assert (await client.get('/api/auth/me')).status_code == 200

    auth_database(scenario)


def test_job_identity_and_errors_hidden_only_for_public_guests(auth_database):
    async def scenario(database, client):
        submitter = await account(database, 'real-admin', role='admin')
        await account(database, 'job-guest')
        job = await job_queue.enqueue_job(database.pool, 'field_index_build', dataset_id=1, created_by=submitter)
        await database.execute(
            "UPDATE jobs SET error=$1,message='Preparing index' WHERE id=$2",
            {'message': 'private'},
            job['id'],
        )
        login_as(client, 'job-guest')
        for path in (f"/api/jobs/{job['id']}", '/api/datasets/1/jobs'):
            response = (await client.get(path)).json()
            result = response['items'][0] if 'items' in response else response
            assert result['started_by'] == '' and result['error'] is None and result['created_by'] is None
        active = (await client.get('/api/datasets/1/jobs/active')).json()
        assert active['waiting'][0]['started_by'] == ''
        login_as(client, 'real-admin')
        result = (await client.get(f"/api/jobs/{job['id']}")).json()
        assert result['started_by'] == 'real-admin' and result['error'] == {'message': 'private'}

    auth_database(scenario)


def test_bcrypt_runs_in_threads(monkeypatch):
    main_thread = threading.get_ident()
    calls = []

    def hashpw(*args):
        calls.append(threading.get_ident())
        return b'hashed'

    def checkpw(*args):
        calls.append(threading.get_ident())
        return True

    monkeypatch.setattr(bcrypt, 'hashpw', hashpw)
    monkeypatch.setattr(bcrypt, 'checkpw', checkpw)

    async def scenario():
        assert await passwords.hash_password('test-password') == 'hashed'
        assert await passwords.verify_password('test-password', 'hashed')

    asyncio.run(scenario())
    assert len(calls) == 2 and all(thread != main_thread for thread in calls)


@pytest.mark.parametrize(
    'environment,demo',
    [('production', 'false'), ('prod', 'false'), (' Production ', 'false'), ('development', 'true')],
)
def test_privacy_gate_accepts_either_public_mode(auth_database, monkeypatch, environment, demo):
    monkeypatch.setenv('FRIDGE_ENV', environment)
    monkeypatch.setenv('DEMO_MODE', demo)

    async def scenario(database, client):
        await account(database, 'guest-gate')
        login_as(client, 'guest-gate')
        response = await client.post('/api/datasets/1/views', json={'name': 'gated', 'content': {}, 'is_public': True})
        assert response.status_code == 200 and response.json()['is_public'] is False
        assert (await client.get('/api/users/names')).status_code == 403

    auth_database(scenario)


@pytest.mark.parametrize('source', ['environment', 'argument', 'development'])
def test_noninteractive_admin_reset(auth_database, monkeypatch, source, capsys):
    import reset_admin

    async def scenario(database, client):
        namespace = await database.fetchval('SELECT current_schema()')
        connection = await asyncpg.connect(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings={'search_path': f'{namespace},public'},
        )
        monkeypatch.setattr(asyncpg, 'connect', AsyncMock(return_value=connection))
        password = 'reset-credential' if source != 'development' else 'admin123'
        if source == 'environment':
            monkeypatch.setenv('FRIDGE_ADMIN_PASSWORD', password)
        await reset_admin.reset_admin(password if source == 'argument' else None)
        row = await database.fetchrow('SELECT * FROM users WHERE id=1')
        assert await passwords.verify_password(password, row['password_hash'])
        assert row['must_change_password'] is (source == 'development')
        assert password not in capsys.readouterr().out

    auth_database(scenario)


def test_demo_admission_lock_contention_returns_503(auth_database, monkeypatch):
    async def scenario(database, client):
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute('SELECT pg_advisory_xact_lock($1)', auth_cleanup.DEMO_ACCOUNT_LOCK)
            # Open another connection before shortening the acquisition budget.
            await database.fetchval('SELECT 1')
            monkeypatch.setenv('FRIDGE_DB_ACQUIRE_TIMEOUT_SECONDS', '0.05')
            public_settings.load_settings()
            response = await client.post('/api/auth/demo-login')
            assert response.status_code == 503 and 'busy' in response.json()['detail']
        assert await database.fetchval('SELECT count(*) FROM users WHERE is_demo_account') == 0

    auth_database(scenario)


def test_cleanup_releases_demo_cap_slots(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_DEMO_ACCOUNT_CAP', '1')

    async def scenario(database, client):
        await account(database, 'old-cap-holder', demo=True, age=2)
        assert (await client.post('/api/auth/demo-login')).status_code == 503
        await auth_cleanup.scheduled_cleanup(database.pool)
        assert (await client.post('/api/auth/demo-login')).status_code == 200

    auth_database(scenario)
