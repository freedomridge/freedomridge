"""An admin's password reset changes the password: the user signs in with the new one, which they must then change,
and the old one is refused."""

import os

import bcrypt
import pytest

from backend.tests import test_field_registry_postgres, test_public_auth
from backend.tests.test_public_auth import account, login_as

registry_database = test_field_registry_postgres.registry_database
auth_database = test_public_auth.auth_database

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


def test_after_an_admin_reset_the_new_password_signs_in_and_the_old_one_does_not(auth_database):
    async def scenario(database, client):
        await account(database, 'reset-admin', role='admin')
        old_hash = bcrypt.hashpw(b'old-credential-1', bcrypt.gensalt(rounds=4)).decode()
        target = await account(database, 'reset-target', role='power_user', password_hash=old_hash)

        login_as(client, 'reset-admin')
        response = await client.post(f'/api/users/{target}/reset-password', json={'password': 'new-credential-2'})
        assert response.status_code == 200, response.text

        client.cookies.clear()

        async def sign_in(password):
            return await client.post('/api/auth/login', json={'username': 'reset-target', 'password': password})

        refused = await sign_in('old-credential-1')
        assert refused.status_code == 401, refused.text
        signed_in = await sign_in('new-credential-2')
        assert signed_in.status_code == 200, signed_in.text
        me = await client.get('/api/auth/me')
        assert me.status_code == 200, me.text
        assert me.json()['username'] == 'reset-target'
        assert me.json()['must_change_password'] is True

    auth_database(scenario)
