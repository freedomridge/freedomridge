"""The follower change log (migration 0007): what changes in a followed dataset is logged with the writing
transaction, whatever wrote it. Samples are logged by block (id / 4096) from the samples statistics trigger,
vectors and hashes per sample and field, and dataset state per kind. An unfollowed dataset,
an unchanged row and a dataset being deleted log nothing, and a deleted dataset's log rows go with it."""

import os

import pytest

from backend.routers import datasets
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
ingest, USER = registry_helpers.ingest, registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
COLUMNS = {
    'sample_block_changes': 'dataset_id, block_id',
    'vector_changes': 'dataset_id, sample_id, kind, field_name',
    'dataset_changes': 'dataset_id, kind',
}


async def taken(database, table):
    """The table's rows since the last call, oldest first; the table is emptied."""
    order = 'transaction_id, block_id' if table == 'sample_block_changes' else 'id'
    rows = await database.fetch(f'SELECT {COLUMNS[table]} FROM {table} ORDER BY {order}')
    await database.execute(f'DELETE FROM {table}')
    return [tuple(row) for row in rows]


def test_sample_writes_log_their_block_for_a_followed_dataset_only(registry_database):
    async def scenario(database):
        await database.execute('UPDATE datasets SET followed = (id = 1)')
        # The real ingest path, into the followed dataset and the other one.
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/a')
        await ingest([{'crop': 'corn'}], dataset_id=2, prefix='s3://images/b')
        first = await database.fetchval("SELECT id FROM samples WHERE filepath = 's3://images/a-0.jpg'")
        block = first // 4096
        assert await taken(database, 'sample_block_changes') == [(1, block)]

        # A sample edit through the API logs the block again (a new transaction); an unchanged write logs nothing.
        await datasets.update_sample(1, first, datasets.SampleUpdate(tags=['reviewed']), user=USER)
        assert await taken(database, 'sample_block_changes') == [(1, block)]
        await database.execute('UPDATE samples SET tags = tags WHERE id = $1', first)
        assert await taken(database, 'sample_block_changes') == []
        # A statement over both datasets logs only the followed one's block.
        await database.execute("UPDATE samples SET metadata = metadata || '{\"split\": \"train\"}'")
        assert await taken(database, 'sample_block_changes') == [(1, block)]
        # A deleted sample logs its block: the follower finds it missing there.
        await database.execute('DELETE FROM samples WHERE id = $1', first)
        assert await taken(database, 'sample_block_changes') == [(1, block)]
        # Statistics bookkeeping still runs as before, and on its own (a statistics job queueing a block) changes
        # no sample, so it logs nothing.
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id = 2') > 0
        await database.execute('SELECT record_stats_work(1, $1, NULL)', block)
        assert await taken(database, 'sample_block_changes') == []

    registry_database(scenario)


def test_vectors_and_hashes_are_logged_per_sample_and_field(registry_database):
    async def scenario(database):
        await database.execute('UPDATE datasets SET followed = (id = 1)')
        await ingest([{}], dataset_id=1, prefix='s3://images/a')
        await ingest([{}], dataset_id=2, prefix='s3://images/b')
        followed = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 1')
        other = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 2')
        for sample in (followed, other):
            await database.execute(
                "INSERT INTO embeddings (sample_id, field_name, vector) "
                "VALUES ($1, 'clip', '[1,0]'), ($1, 'dinov3', '[0,1]')",
                sample,
            )
        await database.execute("UPDATE embeddings SET vector = '[0.5,0.5]' WHERE field_name = 'clip'")
        await database.execute("DELETE FROM embeddings WHERE field_name = 'dinov3'")
        await database.execute(
            "INSERT INTO sample_hashes (sample_id, field_name, hash_hex, hash_bits, prefix) "
            "VALUES ($1, 'phash', 'ffff', B'1111111111111111', B'1111111111111111')",
            followed,
        )
        assert await taken(database, 'vector_changes') == [
            (1, followed, 'vector', 'clip'),
            (1, followed, 'vector', 'dinov3'),
            (1, followed, 'vector', 'clip'),
            (1, followed, 'vector', 'dinov3'),
            (1, followed, 'hash', 'phash'),
        ]
        # Vectors and hashes deleted with their sample are covered by its block, not logged one by one.
        await database.execute('DELETE FROM samples WHERE id = $1', followed)
        assert await taken(database, 'vector_changes') == []

    registry_database(scenario)


def test_one_transaction_logs_one_transaction_id(registry_database):
    async def scenario(database):
        await database.execute('UPDATE datasets SET followed = TRUE WHERE id = 1')
        async with database.pool.acquire() as connection, connection.transaction():
            current = await connection.fetchval('SELECT txid_current()')
            sample = await connection.fetchval(
                "INSERT INTO samples (dataset_id, filepath, media_type) "
                "VALUES (1, 's3://i/1.jpg', 'image') RETURNING id",
            )
            await connection.execute("UPDATE samples SET tags = '{x}' WHERE id = $1", sample)
            await connection.execute(
                "INSERT INTO embeddings (sample_id, field_name, vector) VALUES ($1, 'clip', '[1]')",
                sample,
            )
        assert await database.fetchval('SELECT array_agg(DISTINCT transaction_id) FROM sample_block_changes') == [
            current,
        ]
        assert await database.fetchval('SELECT array_agg(DISTINCT transaction_id) FROM vector_changes') == [current]
        assert await database.fetchval('SELECT count(*) FROM sample_block_changes') == 1  # one row per block

    registry_database(scenario)


def test_dataset_state_and_deletion_are_logged(registry_database):
    async def scenario(database):
        await database.execute('UPDATE datasets SET followed = (id = 1)')
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        for table in COLUMNS:
            await taken(database, table)

        await database.execute("UPDATE datasets SET description = 'field images' WHERE id = 1")
        await database.execute("UPDATE datasets SET description = 'other' WHERE id = 2")
        await database.execute('UPDATE datasets SET stats_full_requests = stats_full_requests + 1 WHERE id = 1')
        await database.execute(
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin) "
            "VALUES (1, 'score', 'number', 'metadata')",
        )
        await database.execute(
            "INSERT INTO views (dataset_id, user_id, name, slug, content) VALUES (1, 1, 'corn', 'corn-view', '{}')",
        )
        assert await taken(database, 'dataset_changes') == [(1, 'settings'), (1, 'fields'), (1, 'views')]

        # A dataset deletion's per-row work (fridge.deletion_dataset) logs nothing, and removing the dataset deletes
        # its log rows: its subscriptions go with it, so nobody could read or acknowledge them.
        await database.execute("UPDATE datasets SET description = 'unread' WHERE id = 1")
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SET LOCAL fridge.deletion_dataset = '1'")
            await connection.execute('DELETE FROM views WHERE dataset_id = 1')
            await connection.execute('DELETE FROM dataset_fields WHERE dataset_id = 1')
            await connection.execute('DELETE FROM samples WHERE dataset_id = 1')
            await connection.execute('UPDATE datasets SET default_view_id = NULL WHERE id = 1')
            await connection.execute('DELETE FROM datasets WHERE id = 1')
        assert await taken(database, 'sample_block_changes') == []
        assert await taken(database, 'vector_changes') == []
        assert await taken(database, 'dataset_changes') == []

    registry_database(scenario)


def test_truncate_is_refused_while_anything_is_followed_or_mirrored(registry_database):
    async def scenario(database):
        import asyncpg

        # Every table whose changes are logged for followers, or guarded on a mirror, refuses TRUNCATE too.
        logged = {
            row['relname']
            for row in await database.fetch(
                """SELECT DISTINCT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
                   WHERE NOT t.tgisinternal AND (t.tgname LIKE '%\\_log\\_%' OR t.tgname LIKE '%mirror\\_guard')""",
            )
        } | {'samples', 'datasets'}  # samples log from their statistics trigger
        guarded = {
            row['relname']
            for row in await database.fetch(
                """SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
                   WHERE t.tgname = 'follower_truncate_guard'""",
            )
        }
        assert logged <= guarded, f'no TRUNCATE guard on {sorted(logged - guarded)}'

        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        # Nothing followed and no mirror: TRUNCATE works as before.
        await database.execute('TRUNCATE sample_hashes')
        await database.execute('UPDATE datasets SET followed = TRUE WHERE id = 1')
        for table in ('sample_hashes', 'embedding_coordinates', 'embeddings CASCADE', 'samples CASCADE'):
            with pytest.raises(asyncpg.PostgresError, match='followed or is a mirror'):
                await database.execute(f'TRUNCATE {table}')
        assert await database.fetchval('SELECT count(*) FROM samples') == 1
        # A mirror alone refuses it too.
        await database.execute('UPDATE datasets SET followed = FALSE')
        await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')
        with pytest.raises(asyncpg.PostgresError, match='followed or is a mirror'):
            await database.execute('TRUNCATE embeddings CASCADE')

    registry_database(scenario)
