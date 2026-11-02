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
