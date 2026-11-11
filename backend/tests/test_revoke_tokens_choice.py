"""Revoking the API tokens is a choice when a password changes: an admin's reset revokes them unless told not to, and a
user's own change revokes them only when asked, follower and worker tokens included either way."""

import os
from unittest.mock import AsyncMock

import asyncpg
import pytest

from backend import passwords
from backend.tests import test_field_registry_postgres, test_public_auth
from backend.tests.test_public_auth import account, login_as
from backend.tests.test_token_revocation import active_tokens, bearer_status, token_for

registry_database = test_field_registry_postgres.registry_database
auth_database = test_public_auth.auth_database

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')

NONE = {'full': 0, 'follower': 0, 'worker': 0}


@pytest.mark.parametrize('body', [{'revoke_tokens': True}, {}])
def test_an_admin_reset_revokes_the_tokens_when_asked_or_not_told(auth_database, body):
    # Absent, as from a client before the choice, the reset revokes them as 2.9.0 did.
    async def scenario(database, client):
        await account(database, 'choice-reset-admin', role='admin')
        target = await account(database, 'choice-reset-target', role='power_user')
        raw = await token_for(database, target)
        await token_for(database, target, 'worker')

        login_as(client, 'choice-reset-admin')
        response = await client.post(
            f'/api/users/{target}/reset-password',
            json={'password': 'temporary-credential', **body},
        )
        assert response.status_code == 200, response.text
        assert response.json()['revoked_tokens'] == {'full': 1, 'follower': 0, 'worker': 1}
        await database.execute('UPDATE users SET must_change_password = FALSE WHERE id = $1', target)
        assert await bearer_status(client, raw) == 401

    auth_database(scenario)


def test_an_admin_reset_that_keeps_the_tokens_still_signs_the_user_out(auth_database):
    async def scenario(database, client):
        await account(database, 'choice-keep-admin', role='admin')
        target = await account(database, 'choice-keep-target', role='power_user')
        raw = await token_for(database, target)
        await token_for(database, target, 'follower')

        login_as(client, 'choice-keep-admin')
        response = await client.post(
            f'/api/users/{target}/reset-password',
            json={'password': 'temporary-credential', 'revoke_tokens': False},
        )
        assert response.status_code == 200, response.text
        assert response.json()['revoked_tokens'] == NONE
        assert await database.fetchval('SELECT count(*) FROM sessions WHERE user_id = $1', target) == 0
        assert await database.fetchval('SELECT must_change_password FROM users WHERE id = $1', target) is True
        assert await active_tokens(database, target) == 2
        await database.execute('UPDATE users SET must_change_password = FALSE WHERE id = $1', target)
        assert await bearer_status(client, raw) == 200

    auth_database(scenario)


@pytest.mark.parametrize('body', [{'revoke_tokens': False}, {}])
def test_changing_your_own_password_keeps_your_tokens_unless_asked(auth_database, body):
    async def scenario(database, client):
        secret = await passwords.hash_password('old-credential')
        me = await account(database, 'choice-own-keep', 'power_user', password_hash=secret)
        raw = await token_for(database, me)

        login_as(client, 'choice-own-keep')
        response = await client.post(
            '/api/auth/change-password',
            json={'current_password': 'old-credential', 'new_password': 'new-credential', **body},
        )
        assert response.status_code == 200, response.text
        assert response.json()['revoked_tokens'] == NONE
        assert await active_tokens(database, me) == 1
        assert await bearer_status(client, raw) == 200

    auth_database(scenario)


@pytest.mark.parametrize('forced', [False, True])
def test_changing_your_own_password_can_revoke_every_one_of_your_tokens(auth_database, forced):
    # The forced change at first login offers the same choice.
    async def scenario(database, client):
        secret = await passwords.hash_password('old-credential')
        me = await account(database, 'choice-own-revoke', 'power_user', password_hash=secret)
        other = await account(database, 'choice-own-bystander', role='power_user')
        await database.execute('UPDATE users SET must_change_password = $1 WHERE id = $2', forced, me)
        raw = await token_for(database, me)
        for scope in ('follower', 'worker', 'worker'):
            await token_for(database, me, scope)
        kept = await token_for(database, other)

        login_as(client, 'choice-own-revoke')
        response = await client.post(
            '/api/auth/change-password',
            json={'current_password': 'old-credential', 'new_password': 'new-credential', 'revoke_tokens': True},
        )
        assert response.status_code == 200, response.text
        assert response.json()['revoked_tokens'] == {'full': 1, 'follower': 1, 'worker': 2}
        assert await active_tokens(database, me) == 0
        assert await bearer_status(client, raw) == 401
        assert await bearer_status(client, kept) == 200
        # The session that changed the password is kept, as without revoking.
        login_as(client, 'choice-own-revoke')
        assert (await client.get('/api/auth/me')).status_code == 200

    auth_database(scenario)


def test_changing_your_own_password_with_a_wrong_current_password_revokes_nothing(auth_database):
    async def scenario(database, client):
        secret = await passwords.hash_password('old-credential')
        me = await account(database, 'choice-own-wrong', 'power_user', password_hash=secret)
        await token_for(database, me)
        login_as(client, 'choice-own-wrong')
        response = await client.post(
            '/api/auth/change-password',
            json={'current_password': 'not-it', 'new_password': 'new-credential', 'revoke_tokens': True},
        )
        assert response.status_code == 400
        assert await active_tokens(database, me) == 1

    auth_database(scenario)


def test_reset_admin_keep_tokens_keeps_the_admins_tokens(auth_database, monkeypatch, capsys):
    import reset_admin

    async def scenario(database, client):
        admin = await database.fetchval("SELECT id FROM users WHERE username = 'admin'")
        raw = await token_for(database, admin)
        namespace = await database.fetchval('SELECT current_schema()')
        connection = await asyncpg.connect(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings={'search_path': f'{namespace},public'},
        )
        monkeypatch.setattr(asyncpg, 'connect', AsyncMock(return_value=connection))
        await reset_admin.reset_admin('reset-cli-credential', keep_tokens=True)

        assert await active_tokens(database, admin) == 1
        assert await bearer_status(client, raw) == 200
        assert 'were kept' in capsys.readouterr().out

    auth_database(scenario)
