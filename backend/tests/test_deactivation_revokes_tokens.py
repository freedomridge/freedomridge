"""Deactivating an account revokes its API tokens for good: reactivating it doesn't bring them back."""

import os

import pytest

from backend.tests import test_field_registry_postgres, test_public_auth
from backend.tests.test_public_auth import account, login_as
from backend.tests.test_token_revocation import active_tokens, bearer_status, token_for

registry_database = test_field_registry_postgres.registry_database
auth_database = test_public_auth.auth_database

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


def test_deactivating_revokes_every_token_and_reactivating_brings_none_back(auth_database):
    # A deactivated account's tokens only paused (403) and worked again once it was reactivated.
    async def scenario(database, client):
        admin = await account(database, 'deactivate-admin', role='admin')
        target = await account(database, 'deactivate-target', role='power_user')
        other = await account(database, 'deactivate-bystander', role='power_user')
        full = await token_for(database, target)
        follower = await token_for(database, target, 'follower')
        for scope in ('worker', 'worker'):
            await token_for(database, target, scope)
        kept = await token_for(database, other)
        assert await bearer_status(client, full) == 200

        login_as(client, 'deactivate-admin')
        response = await client.patch(f'/api/users/{target}', json={'is_active': False})
        assert response.status_code == 200, response.text
        # The page says what stopped working from these counts, as after a password reset.
        assert response.json()['revoked_tokens'] == {'full': 1, 'follower': 1, 'worker': 2}
        assert await active_tokens(database, target) == 0
        assert await bearer_status(client, full) in (401, 403)

        login_as(client, 'deactivate-admin')
        response = await client.patch(f'/api/users/{target}', json={'is_active': True})
        assert response.status_code == 200, response.text
        assert 'revoked_tokens' not in response.json()
        assert await database.fetchval('SELECT is_active FROM users WHERE id = $1', target) is True
        assert await active_tokens(database, target) == 0
        assert await bearer_status(client, full) == 401
        assert await bearer_status(client, follower) == 401
        assert await bearer_status(client, kept) == 200  # nobody else's tokens
        assert await active_tokens(database, admin) == 0

    auth_database(scenario)


def test_only_deactivating_revokes(auth_database):
    async def scenario(database, client):
        admin = await account(database, 'deactivate-only-admin', role='admin')
        target = await account(database, 'deactivate-only-target', role='power_user')
        raw = await token_for(database, target)

        login_as(client, 'deactivate-only-admin')
        for body in ({'role': 'guest'}, {'is_active': True}, {'email': 'someone@example.com'}):
            response = await client.patch(f'/api/users/{target}', json=body)
            assert response.status_code == 200, response.text
            assert 'revoked_tokens' not in response.json()
        assert await active_tokens(database, target) == 1
        assert await bearer_status(client, raw) == 200

        # An admin can't deactivate themselves, and nothing is revoked when the change is refused.
        own = await token_for(database, admin)
        login_as(client, 'deactivate-only-admin')
        assert (await client.patch(f'/api/users/{admin}', json={'is_active': False})).status_code == 400
        assert await bearer_status(client, own) == 200
        login_as(client, 'deactivate-only-admin')
        assert (await client.patch('/api/users/999999', json={'is_active': False})).status_code == 404

    auth_database(scenario)


def test_saving_an_inactive_account_inactive_revokes_tokens_it_still_has(auth_database):
    # An account deactivated before deactivating revoked tokens kept them; saving it inactive again revokes them.
    async def scenario(database, client):
        await account(database, 'deactivate-again-admin', role='admin')
        target = await account(database, 'deactivate-again-target', role='power_user')
        await token_for(database, target, 'follower')
        await database.execute('UPDATE users SET is_active = FALSE WHERE id = $1', target)

        login_as(client, 'deactivate-again-admin')
        response = await client.patch(f'/api/users/{target}', json={'role': 'power_user', 'is_active': False})
        assert response.status_code == 200, response.text
        assert response.json()['revoked_tokens'] == {'full': 0, 'follower': 1, 'worker': 0}
        response = await client.patch(f'/api/users/{target}', json={'is_active': False})
        assert response.json()['revoked_tokens'] == {'full': 0, 'follower': 0, 'worker': 0}
        assert await active_tokens(database, target) == 0

    auth_database(scenario)
