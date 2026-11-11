"""A mirror's own fields (migration 0022, docs/FOLLOWERS.md): `@local.` fields are stored in samples.local_metadata
and local_tags, which a write under fridge.mirror_local may change and nothing else; dataset_fields says who owns
each field, and the sync keeps this instance's own."""

import os
from pathlib import Path

import asyncpg
import pytest
from fastapi import HTTPException

from backend import field_registry, follower_sync
from backend.routers import datasets
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
ingest, USER = registry_helpers.ingest, registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
LOCAL_ONLY = 'changes only those fields'
OWN_FIELD = """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin)
               VALUES (2, '@local.weeds', 'number', 'local')"""
READ_ONLY = 'read-only mirror'


async def as_local(database, dataset_id, statement, *args):
    """One statement under fridge.mirror_local, as a local write runs."""
    async with database.pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT set_config('fridge.mirror_local', $1, TRUE)", str(dataset_id))
        return await connection.execute(statement, *args)


async def as_sync(database, dataset_id, call):
    async with database.pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT set_config('fridge.mirror_sync', $1, TRUE)", str(dataset_id))
        return await call(connection)


def test_a_mirror_writes_its_own_fields_and_nothing_else(registry_database):
    async def scenario(database):
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=2, prefix='s3://images/m')
        # An array column added at runtime, as create_array_field does: the check covers columns it doesn't name.
        await database.execute('ALTER TABLE samples ADD COLUMN boxes TEXT[]')
        await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')
        before = await database.fetch('SELECT * FROM samples WHERE dataset_id = 2 ORDER BY id')
        first = before[0]['id']

        # Without the setting, the mirror refuses even its local columns.
        with pytest.raises(asyncpg.PostgresError, match=READ_ONLY):
            await database.execute(
                """UPDATE samples SET local_metadata = '{"weeds": 3}' WHERE id = $1""",
                first,
            )

        # Under it, the two local columns change, and nothing else does (last_modified_at is the primary's).
        await as_local(
            database,
            2,
            """UPDATE samples SET local_metadata = local_metadata || '{"weeds": 3}', local_tags = '{checked}'
               WHERE id = $1""",
            first,
        )
        after = await database.fetchrow('SELECT * FROM samples WHERE id = $1', first)
        assert (after['local_metadata'], after['local_tags']) == ({'weeds': 3}, ['checked'])
        unchanged = {key for key in before[0].keys() if key not in ('local_metadata', 'local_tags')}
        assert {key: after[key] for key in unchanged} == {key: before[0][key] for key in unchanged}

        # Any other column, in the same statement or alone, is refused, and so are inserts, deletes and other datasets.
        for change in (
            """metadata = metadata || '{"crop": "rye"}'""",
            "local_metadata = '{}', tags = '{new}'",
            "location = ST_SetSRID(ST_MakePoint(1, 2), 4326)",
            "boxes = '{car}'",
            'last_modified_at = now()',
        ):
            with pytest.raises(asyncpg.PostgresError, match=LOCAL_ONLY):
                await as_local(database, 2, f'UPDATE samples SET {change} WHERE id = $1', first)
        with pytest.raises(asyncpg.PostgresError, match=LOCAL_ONLY):
            await as_local(database, 2, 'DELETE FROM samples WHERE id = $1', first)
        with pytest.raises(asyncpg.PostgresError, match=LOCAL_ONLY):
            await as_local(
                database,
                2,
                "INSERT INTO samples (dataset_id, filepath, media_type) VALUES (2, 's3://images/new.jpg', 'image')",
            )
        # The setting names dataset 1 and the row is 2's: either guard may say so first.
        with pytest.raises(asyncpg.PostgresError, match=f'{LOCAL_ONLY}|{READ_ONLY}'):
            await as_local(database, 1, """UPDATE samples SET local_metadata = '{"x": 1}' WHERE id = $1""", first)
        assert dict(await database.fetchrow('SELECT * FROM samples WHERE id = $1', first)) == dict(after)

        # The sync keeps writing the primary's columns, and leaves the local ones alone.
        await as_sync(
            database,
            2,
            lambda connection: connection.execute(
                """UPDATE samples SET metadata = '{"crop": "rye"}' WHERE id = $1""",
                first,
            ),
        )
        synced = await database.fetchrow('SELECT metadata, local_metadata FROM samples WHERE id = $1', first)
        assert (synced['metadata'], synced['local_metadata']) == ({'crop': 'rye'}, {'weeds': 3})

    registry_database(scenario)


def test_a_field_is_the_primarys_on_a_mirror_unless_it_is_this_instances_own(registry_database):
    async def scenario(database):
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        await ingest([{'crop': 'corn'}], dataset_id=2, prefix='s3://images/m')
        owners = 'SELECT field_name, owner FROM dataset_fields WHERE dataset_id = $1 ORDER BY field_name'
        assert dict(await database.fetch(owners, 2)) == {'crop': 'self'}

        # A dataset that becomes a mirror: the migration's statement marks its fields, past the mirror guard.
        await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')
        await database.execute(
            "UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id IN (SELECT dataset_id FROM mirrors)",
        )
        assert dict(await database.fetch(owners, 2)) == {'crop': 'primary'}

        # This instance's own field registers on the mirror without the sync, and is its own. Owner can't be set.
        await database.execute(OWN_FIELD)
        await database.execute("UPDATE dataset_fields SET owner = 'self' WHERE dataset_id = 2 AND field_name = 'crop'")
        await database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE field_name = '@local.weeds'")
        assert dict(await database.fetch(owners, 2)) == {'@local.weeds': 'self', 'crop': 'primary'}
        assert dict(await database.fetch(owners, 1)) == {'crop': 'self'}
        # The primary's fields still change only through the sync; this instance's own go when it says.
        with pytest.raises(asyncpg.PostgresError, match=READ_ONLY):
            await database.execute(
                "INSERT INTO dataset_fields (dataset_id, field_name, field_type) VALUES (2, 'score', 'number')",
            )
        await database.execute("DELETE FROM dataset_fields WHERE field_name = '@local.weeds'")

        # `@local.` names are origin 'local', and only they are.
        for name, origin in (('@local.x', 'metadata'), ('x', 'local')):
            with pytest.raises(asyncpg.CheckViolationError, match='dataset_fields_local_check'):
                await database.execute(
                    """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin)
                       VALUES (1, $1, 'string', $2)""",
                    name,
                    origin,
                )

    registry_database(scenario)


def test_the_sync_keeps_this_instances_own_fields(registry_database):
    async def scenario(database):
        await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')
        await database.execute(OWN_FIELD)
        primary = [
            {'field_name': 'crop', 'field_type': 'string', 'origin': 'metadata'},
            # An older primary's metadata key by that name, unreachable there: never registered here.
            {'field_name': '@local.odd', 'field_type': 'string', 'origin': 'metadata'},
        ]
        await as_sync(database, 2, lambda connection: follower_sync.write_fields(connection, 2, primary))
        owners = 'SELECT field_name, owner FROM dataset_fields WHERE dataset_id = 2 ORDER BY field_name'
        assert dict(await database.fetch(owners)) == {'@local.weeds': 'self', 'crop': 'primary'}
        # The primary drops its field: it goes here, and this instance's own stays.
        await as_sync(database, 2, lambda connection: follower_sync.write_fields(connection, 2, []))
        assert dict(await database.fetch(owners)) == {'@local.weeds': 'self'}

    registry_database(scenario)


def test_the_local_columns_are_never_an_array_field(registry_database):
    """An array field by either name would make the column a field the sync writes from the primary."""

    async def scenario(database):
        await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')
        for name in ('local_tags', 'local_metadata'):
            # A primary's array field by that name: the sync refuses it, as any name it can't hold.
            with pytest.raises(ValueError, match='cannot hold'):
                await follower_sync.ensure_array_columns(database, 2, [name])
            with pytest.raises(HTTPException) as refused:
                await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name=name), user=USER)
            assert refused.value.status_code == 400 and 'reserved' in refused.value.detail
        assert await database.fetchval("SELECT count(*) FROM sample_array_columns WHERE column_name LIKE 'local%'") == 0

    registry_database(scenario)


def test_the_migration_names_what_is_in_its_way(registry_database):
    """Its first statement refuses a database that already has either column; this schema has both."""
    sql = (Path(datasets.__file__).parents[1] / 'db/migrations/0022_local_fields.sql').read_text()
    check = sql[sql.index('DO $$') : sql.index('$$;') + 3]

    async def scenario(database):
        with pytest.raises(asyncpg.RaiseError, match='samples already has a column named local_'):
            await database.execute(check)

    registry_database(scenario)


def test_metadata_never_holds_a_local_name():
    with pytest.raises(ValueError, match="'@local.weeds'"):
        field_registry.validate_metadata({'crop': 'corn', '@local.weeds': 3})
    # Only a top-level key names a field; a nested one is data, and other `@` keys are ordinary.
    assert field_registry.validate_metadata({'box': {'@local.x': 1}, '@timestamp': 'now'})
    assert field_registry.infer_fields([{'crop': 'corn', '@local.weeds': 3}]) == {'crop': 'string'}
