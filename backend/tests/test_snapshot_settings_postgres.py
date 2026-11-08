"""Snapshot settings must not silently lose a concurrent dataset update."""

import os

import asyncpg
import pytest
from fastapi import HTTPException

from backend.routers import versions
from backend.tests.test_field_registry_postgres import ingest, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


@pytest.mark.parametrize('change_after_copy', [False, True])
def test_snapshot_settings_match_committed_dataset(registry_database, monkeypatch, change_after_copy):
    async def scenario(database):
        await ingest([{'label': 'original'}])

        async def change_settings():
            await database.execute(
                """UPDATE datasets SET name = 'renamed', description = 'concurrent description',
                   grid_media_field = 'alternate' WHERE id = 1""",
            )

        read_arrays = versions._snapshot_arrays
        execute = asyncpg.Connection.execute

        async def concurrent_settings(conn):
            await change_settings()
            return await read_arrays(conn)

        async def copy_then_change(conn, query, *args, **kwargs):
            result = await execute(conn, query, *args, **kwargs)
            if query.lstrip().startswith('INSERT INTO version_snapshots'):
                await change_settings()
            return result

        with monkeypatch.context() as patch:
            if change_after_copy:
                patch.setattr(asyncpg.Connection, 'execute', copy_then_change)
            else:
                patch.setattr(versions, '_snapshot_arrays', concurrent_settings)
            try:
                record = await versions.create_version(1, versions.CreateVersionRequest(name='concurrent'))
            except HTTPException as error:
                assert error.status_code == 409 and 'retry' in error.detail
                assert await database.fetchval('SELECT count(*) FROM dataset_versions') == 0
                assert await database.fetchval('SELECT count(*) FROM version_snapshots') == 0
                record = None

        if record is None:
            record = await versions.create_version(1, versions.CreateVersionRequest(name='concurrent'))
        saved = (await versions.get_version(1, record['id']))['dataset_snapshot']
        committed = dict(
            await database.fetchrow(
                """SELECT name, description, media_fields, grid_media_field, modal_media_field,
                   segmentation_config FROM datasets WHERE id = 1""",
            ),
        )
        assert committed['name'] == 'renamed'
        assert {key: saved[key] for key in committed} == committed
        count = await database.fetchval('SELECT count(*) FROM version_snapshots WHERE version_id = $1', record['id'])
        assert count == 1

    registry_database(scenario)


def test_a_settings_update_waits_until_the_snapshot_commits(registry_database, monkeypatch):
    """Once the settings are checked, an update must not commit before the snapshot does."""

    async def scenario(database):
        await ingest([{'label': 'original'}])
        hold = versions._hold_unchanged_settings
        blocked = []

        async def hold_then_rename(conn, dataset_id, held):
            await hold(conn, dataset_id, held)
            async with database.pool.acquire() as other:
                await other.execute("SET lock_timeout = '100ms'")
                with pytest.raises(asyncpg.LockNotAvailableError):
                    await other.execute("UPDATE datasets SET name = 'late' WHERE id = 1")
                await other.execute('RESET lock_timeout')
            blocked.append(dataset_id)

        monkeypatch.setattr(versions, '_hold_unchanged_settings', hold_then_rename)
        record = await versions.create_version(1, versions.CreateVersionRequest(name='held'))
        assert blocked == [1]
        saved = (await versions.get_version(1, record['id']))['dataset_snapshot']
        assert saved['name'] == await database.fetchval('SELECT name FROM datasets WHERE id = 1') != 'late'

    registry_database(scenario)
