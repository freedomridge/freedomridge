"""An admin's password reset ends the user's API tokens, and an admin can revoke another user's tokens."""

import os
import secrets
from unittest.mock import AsyncMock

import asyncpg
import pytest

from backend.crypto import hash_token
from backend.tests import test_field_registry_postgres, test_public_auth
from backend.tests.test_public_auth import account, login_as

registry_database = test_field_registry_postgres.registry_database
auth_database = test_public_auth.auth_database

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


async def token_for(database, user_id, scope='full'):
    raw = secrets.token_urlsafe(32)
    await database.execute(
        'INSERT INTO api_tokens (user_id, token, name, scope) VALUES ($1, $2, $3, $4)',
        user_id,
        hash_token(raw),
        f'{scope}-token',
        scope,
    )
    return raw


async def bearer_status(client, raw):
    client.cookies.clear()
    response = await client.get('/api/auth/me', headers={'Authorization': f'Bearer {raw}'})
    return response.status_code


async def active_tokens(database, user_id):
    return await database.fetchval('SELECT count(*) FROM api_tokens WHERE user_id = $1 AND is_active', user_id)


def test_an_admin_reset_revokes_the_users_tokens(auth_database):
    # A token copied from the account resumed its access once the user chose a new password.
    async def scenario(database, client):
        admin = await account(database, 'reset-admin', role='admin')
        target = await account(database, 'reset-target', role='power_user')
        other = await account(database, 'reset-bystander', role='power_user')
        stolen = await token_for(database, target)
        kept = await token_for(database, other)
        assert await bearer_status(client, stolen) == 200

        login_as(client, 'reset-admin')
        response = await client.post(f'/api/users/{target}/reset-password', json={'password': 'temporary-credential'})
        assert response.status_code == 200, response.text
        # The user sets their own password (what lifted the must-change gate for the old token).
        await database.execute('UPDATE users SET must_change_password = FALSE WHERE id = $1', target)

        assert await bearer_status(client, stolen) == 401
        assert await active_tokens(database, target) == 0
        assert await database.fetchval('SELECT count(*) FROM sessions WHERE user_id = $1', target) == 0
        assert await bearer_status(client, kept) == 200  # nobody else's tokens
        assert await active_tokens(database, admin) == 0  # the admin had none; nothing else changed

    auth_database(scenario)


def test_reset_admin_revokes_the_admins_tokens(auth_database, monkeypatch):
    import reset_admin

    async def scenario(database, client):
        admin = await database.fetchval("SELECT id FROM users WHERE username = 'admin'")
        other = await account(database, 'reset-cli-bystander', role='power_user')
        stolen = await token_for(database, admin)
        kept = await token_for(database, other)
        namespace = await database.fetchval('SELECT current_schema()')
        connection = await asyncpg.connect(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings={'search_path': f'{namespace},public'},
        )
        monkeypatch.setattr(asyncpg, 'connect', AsyncMock(return_value=connection))
        # A supplied password clears the must-change flag, so the old token worked again at once.
        await reset_admin.reset_admin('reset-cli-credential')

        assert await database.fetchval('SELECT must_change_password FROM users WHERE id = $1', admin) is False
        assert await bearer_status(client, stolen) == 401
        assert await active_tokens(database, admin) == 0
        assert await bearer_status(client, kept) == 200

    auth_database(scenario)


def test_an_admin_revokes_all_of_another_users_tokens(auth_database):
    async def scenario(database, client):
        await account(database, 'revoke-admin', role='admin')
        await account(database, 'revoke-power', role='power_user')
        target = await account(database, 'revoke-target', role='power_user')
        other = await account(database, 'revoke-bystander', role='power_user')
        full = await token_for(database, target)
        await token_for(database, target, scope='worker')
        kept = await token_for(database, other)

        login_as(client, 'revoke-power')
        assert (await client.delete(f'/api/users/{target}/tokens')).status_code == 403
        assert await active_tokens(database, target) == 2

        login_as(client, 'revoke-admin')
        response = await client.delete(f'/api/users/{target}/tokens')
        assert response.status_code == 200, response.text
        assert response.json() == {'revoked': 2}
        assert await active_tokens(database, target) == 0
        assert await bearer_status(client, full) == 401
        assert await bearer_status(client, kept) == 200

        login_as(client, 'revoke-admin')
        assert (await client.delete(f'/api/users/{target}/tokens')).json() == {'revoked': 0}
        assert (await client.delete('/api/users/999999/tokens')).status_code == 404

    auth_database(scenario)
