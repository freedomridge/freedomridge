"""Validate the disposable database before collection or any test fixture can write."""

import asyncio
import os
import subprocess
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest

from scripts.db_test_safety import UnsafeTestDatabase, endpoint, require_container, require_test_url

IDENTITY_SQL = 'SELECT system_identifier FROM pg_control_system()'
_patch = pytest.MonkeyPatch()


async def dsn_identity(dsn):
    connection = await asyncpg.connect(
        dsn,
        timeout=5,
        server_settings={
            'default_transaction_read_only': 'on',
            'statement_timeout': '5000',
            'application_name': 'fridge-test-identity',
        },
    )
    try:
        return str(await connection.fetchval(IDENTITY_SQL))
    finally:
        await connection.close()


def pytest_sessionstart(session):
    _patch.setenv('FRIDGE_TEST_MODE', '1')
    dsn = os.getenv('FRIDGE_TEST_POSTGRES_DSN')
    container = os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')
    if not dsn and not container:
        return  # Unit-only runs need no PostgreSQL server.
    try:
        if not dsn or not container:
            raise UnsafeTestDatabase('supply both FRIDGE_TEST_POSTGRES_DSN and FRIDGE_TEST_POSTGRES_CONTAINER')
        endpoint(dsn)
        if 'DATABASE_URL' in os.environ:
            require_test_url(os.environ['DATABASE_URL'])
        identity = require_container(container)
        if os.getenv('FRIDGE_DB_CONTAINER'):
            require_container(os.environ['FRIDGE_DB_CONTAINER'], identity)
        user = unquote(urlsplit(dsn).username or 'freedomridge')
        result = subprocess.run(
            [
                'docker',
                'exec',
                '-e',
                'PGOPTIONS=-c default_transaction_read_only=on',
                '-e',
                'PGCONNECT_TIMEOUT=5',
                identity,
                'psql',
                '-X',
                '-h',
                '/var/run/postgresql',
                '-U',
                user,
                '-d',
                'postgres',
                '-Atq',
                '-v',
                'ON_ERROR_STOP=1',
                '-c',
                IDENTITY_SQL,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        system_identifier = result.stdout.strip()
        if not system_identifier.isdigit() or system_identifier != asyncio.run(dsn_identity(dsn)):
            raise UnsafeTestDatabase('DSN and container resolve to different PostgreSQL servers')
        # Backend modules capture this at import time, so pin it before collection.
        _patch.setenv('DATABASE_URL', dsn)
    except UnsafeTestDatabase as error:
        pytest.exit(str(error), returncode=2)
    except Exception:
        # Connection errors can contain connection strings; do not expose credentials.
        pytest.exit('Unsafe test database target: could not verify PostgreSQL server identity', returncode=2)


def pytest_unconfigure(config):
    _patch.undo()
