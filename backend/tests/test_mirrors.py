"""A follower's mirrors (migration 0009): what came from the primary changes only through the sync job, which sets
fridge.mirror_sync to the mirror's dataset id; what belongs to this instance stays writable."""

import os

import asyncpg
import pytest

from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
ingest = registry_helpers.ingest
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
REFUSED = 'read-only mirror'


async def as_sync(database, dataset_id, statement, *args):
    """Run a statement as the sync job does, in a transaction that names the mirror."""
    async with database.pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT set_config('fridge.mirror_sync', $1, TRUE)", str(dataset_id))
        return await connection.fetchval(statement, *args)


async def refused(database, statement, *args, message=REFUSED):
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError) as error:
        await database.execute(statement, *args)
    assert message in str(error.value)


def test_a_mirror_changes_only_through_its_sync(registry_database):
    async def scenario(database):
        # Dataset 2 is a local dataset with a sample; dataset 1 becomes a mirror once it holds the primary's copy.
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        await ingest([{'crop': 'soy'}], dataset_id=2, prefix='s3://images/b')
        await database.execute(
            "INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor, vector_fields, hash_fields) "
            "VALUES (1, 7, 0, '{clip}', '{dhash_8}')",
        )
        mirrored = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 1')
        local = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 2')

        # Samples: no insert, edit or delete except through the sync.
        await refused(database, "INSERT INTO samples (dataset_id, filepath, media_type) VALUES (1, 's3://x', 'image')")
        await refused(database, "UPDATE samples SET tags = '{reviewed}' WHERE id = $1", mirrored)
        await refused(database, 'DELETE FROM samples WHERE id = $1', mirrored)
        await as_sync(database, 1, "UPDATE samples SET tags = '{reviewed}' WHERE id = $1", mirrored)
        assert await database.fetchval('SELECT tags FROM samples WHERE id = $1', mirrored) == ['reviewed']
        # The sync of one mirror can't write another dataset, and other datasets are untouched by all this.
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await as_sync(database, 2, "UPDATE samples SET tags = '{x}' WHERE id = $1", mirrored)
        await database.execute("UPDATE samples SET tags = '{local}' WHERE id = $1", local)
        # Statistics are this instance's own: a statistics job queueing a mirror's block changes no sample.
        await database.execute('SELECT record_stats_work(1, $1, NULL)', mirrored // 4096)

        # The field registry is the primary's; is_promoted (an index here) stays local.
        await refused(
            database,
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type) VALUES (1, 'height', 'number')",
        )
        await refused(database, "UPDATE dataset_fields SET display_type = 'enum' WHERE dataset_id = 1")
        await database.execute("UPDATE dataset_fields SET is_promoted = TRUE WHERE dataset_id = 1")

        # Settings are the primary's; storage and access stay local.
        await refused(database, "UPDATE datasets SET name = 'renamed' WHERE id = 1")
        await database.execute('UPDATE datasets SET is_global = FALSE WHERE id = 1')
        await as_sync(database, 1, "UPDATE datasets SET name = 'renamed' WHERE id = 1")

        # Vector and hash fields from the primary are read-only; fields only this instance has are not.
        await refused(
            database,
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[1,0]')",
            mirrored,
            message='came from another Freedom Ridge instance',
        )
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'patches', '[1,0]')",
            mirrored,
        )
        await as_sync(
            database,
            1,
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[1,0]')",
            mirrored,
        )
        await refused(
            database,
            "INSERT INTO sample_hashes (sample_id, field_name, hash_hex) VALUES ($1, 'dhash_8', 'ff00ff00ff00ff00')",
            mirrored,
            message='came from another Freedom Ridge instance',
        )

        # A view copied from the primary is read-only; a view made here on the mirror is the user's own.
        copied = await as_sync(
            database,
            1,
            "INSERT INTO views (dataset_id, name, slug, is_public, name_scope) "
            "VALUES (1, 'Corn', 'mirror-1-5', TRUE, -1) RETURNING id",
        )
        await database.execute(
            'INSERT INTO mirror_views (dataset_id, origin_view_id, view_id) VALUES (1, 5, $1)',
            copied,
        )
        await refused(database, "UPDATE views SET name = 'Mine' WHERE id = $1", copied)
        await refused(database, 'DELETE FROM views WHERE id = $1', copied)
        own = await database.fetchval(
            "INSERT INTO views (dataset_id, name, slug) VALUES (1, 'Corn', 'own-view') RETURNING id",
        )
        await database.execute("UPDATE views SET name = 'Corn close-ups' WHERE id = $1", own)
        await as_sync(database, 1, 'DELETE FROM views WHERE id = $1', copied)
        assert await database.fetchval('SELECT count(*) FROM mirror_views') == 0

    registry_database(scenario)
