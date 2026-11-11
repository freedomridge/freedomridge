"""Each dataset's vectors are their own embeddings partition (0021): made with the dataset, keyed by it, dropped by
the sweeper once the dataset is gone, with its vector indexes rebuilt per dataset after the upgrade."""

import asyncio
import os

import asyncpg
import pytest
from fastapi import HTTPException

from backend import dataset_deletion, index_jobs, sample_ingest
from backend.routers import datasets
from backend.routers.datasets import DatasetCreate
from backend.tests import test_field_registry_postgres
from backend.tests.test_dataset_deletion import finish
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


async def create(database, name):
    # The fixture's datasets have explicit ids, so the sequence starts past them first.
    await database.execute("SELECT setval(pg_get_serial_sequence('datasets', 'id'), 10)")
    return await datasets.create_dataset(DatasetCreate(name=name), user=USER)


async def partitions(database):
    return [
        row['relname']
        for row in await database.fetch(
            """SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
               WHERE i.inhparent = to_regclass('embeddings') ORDER BY 1""",
        )
    ]


def test_a_dataset_has_its_own_partition_and_a_vector_must_name_its_samples_dataset(registry_database):
    async def scenario(database):
        assert await partitions(database) == ['embeddings_d1', 'embeddings_d2']
        sample_id = (await ingest([{}]))['ids'][0]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES (1, $1, 'clip', '[1,0]')",
            sample_id,
        )
        assert await database.fetchval('SELECT count(*) FROM embeddings_d1') == 1
        # Another dataset's partition: the FK names the sample's dataset, so the vector can't hide there.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await database.execute(
                "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES (2, $1, 'dino', '[1,0]')",
                sample_id,
            )
        # No partition at all.
        with pytest.raises(asyncpg.CheckViolationError):
            await database.execute(
                "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES (99, $1, 'x', '[1,0]')",
                sample_id,
            )
        # Coordinates name their dataset from the sample.
        await database.execute(
            "INSERT INTO embedding_coordinates (sample_id, field_name, x, y) VALUES ($1, 'clip', 1, 2)",
            sample_id,
        )
        assert await database.fetchval('SELECT dataset_id FROM embedding_coordinates') == 1

    registry_database(scenario)


def test_the_sweeper_drops_a_deleted_datasets_emptied_partition_and_nothing_else(registry_database, monkeypatch):
    async def scenario(database):
        dataset = await create(database, 'gone')
        sample_id = (await ingest([{}], dataset_id=dataset['id']))['ids'][0]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ($1, $2, 'clip', '[1,0]')",
            dataset['id'],
            sample_id,
        )
        assert f'embeddings_d{dataset['id']}' in await partitions(database)
        assert await dataset_deletion.sweep_partitions(database.pool) == []  # its dataset is still there
        await dataset_deletion.submit(database.pool, dataset['id'], USER['id'])
        await finish(database)
        assert await database.fetchval('SELECT count(*) FROM datasets WHERE id = $1', dataset['id']) == 0
        assert await dataset_deletion.sweep_partitions(database.pool) == [f'embeddings_d{dataset['id']}']
        assert await partitions(database) == ['embeddings_d1', 'embeddings_d2']
        assert await database.fetchval("SELECT to_regclass($1)", f'embeddings_d{dataset['id']}') is None
        assert await dataset_deletion.sweep_partitions(database.pool) == []

    registry_database(scenario)


def test_the_sweeper_leaves_a_partition_it_cant_lock_for_the_next_sweep(registry_database):
    async def scenario(database):
        dataset = await create(database, 'gone')
        await dataset_deletion.submit(database.pool, dataset['id'], USER['id'])
        await finish(database)
        reader = await database.pool.acquire()
        try:
            # A long search holds the parent: the sweep gives up after 100 ms instead of queueing every search.
            async with reader.transaction():
                await reader.fetchval('SELECT count(*) FROM embeddings WHERE dataset_id = 1')
                assert await dataset_deletion.sweep_partitions(database.pool) == []
        finally:
            await database.pool.release(reader)
        assert await dataset_deletion.sweep_partitions(database.pool) == [f'embeddings_d{dataset['id']}']

    registry_database(scenario)


def test_rebuilds_queue_one_build_per_dataset_and_field_once(registry_database):
    async def scenario(database):
        await database.execute(
            "INSERT INTO ann_index_rebuilds (dataset_id, field_name) VALUES (1, 'clip'), (2, 'clip'), (2, 'dino')",
        )
        assert await index_jobs.queue_rebuilds(database.pool) == 3
        jobs = await database.fetch(
            "SELECT dataset_id, args->>'field' AS field FROM jobs WHERE kind = $1 ORDER BY 1, 2",
            index_jobs.ANN_KIND,
        )
        assert [(row['dataset_id'], row['field']) for row in jobs] == [(1, 'clip'), (2, 'clip'), (2, 'dino')]
        assert await database.fetchval('SELECT count(*) FROM ann_index_rebuilds') == 0
        assert await index_jobs.queue_rebuilds(database.pool) == 0

    registry_database(scenario)


def test_creating_a_dataset_while_samples_are_written_answers_503_instead_of_holding_them(
    registry_database,
    monkeypatch,
):
    async def scenario(database):
        monkeypatch.setattr(datasets, '_PARTITION_RETRY_SECONDS', 1.0)
        writer = await database.pool.acquire()
        try:
            # An open sample write: the new partition's ATTACH would wait for it, and every later write behind that.
            async with writer.transaction():
                await writer.execute('LOCK TABLE samples IN ROW EXCLUSIVE MODE')
                with pytest.raises(HTTPException) as refused:
                    await asyncio.wait_for(create(database, 'busy'), 20)
        finally:
            await database.pool.release(writer)
        assert refused.value.status_code == 503
        assert await database.fetchval("SELECT count(*) FROM datasets WHERE name = 'busy'") == 0

    registry_database(scenario)


async def partitions_locked(connection):
    """The embeddings partitions this transaction holds a lock on."""
    return await connection.fetchval(
        """SELECT count(DISTINCT c.oid) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation
           JOIN pg_inherits i ON i.inhrelid = c.oid AND i.inhparent = to_regclass('embeddings')
           WHERE l.pid = pg_backend_pid()""",
    )


def test_the_apps_connections_and_the_database_plan_each_statement_for_its_values(registry_database, monkeypatch):
    from backend import database as database_module

    async def scenario(database):
        settings = {}

        async def create_pool(*args, **kwargs):
            settings.update(kwargs['server_settings'])
            raise RuntimeError('stop here')

        monkeypatch.setattr(asyncpg, 'create_pool', create_pool)
        with pytest.raises(RuntimeError, match='stop here'):
            await database_module.Database().connect()
        assert settings['plan_cache_mode'] == 'force_custom_plan'
        # schema.sql sets the same default on the database, for scripts and other clients.
        fresh = await asyncpg.connect(os.environ['FRIDGE_TEST_POSTGRES_DSN'])
        try:
            assert await fresh.fetchval('SHOW plan_cache_mode') == 'force_custom_plan'
        finally:
            await fresh.close()

    registry_database(scenario)


def test_statements_run_often_and_the_coordinates_trigger_lock_one_datasets_partition(registry_database):
    async def scenario(database):
        # A generic plan, which a prepared statement switches to after five runs, would lock all 22 partitions.
        await database.execute("SELECT setval(pg_get_serial_sequence('datasets', 'id'), 10)")
        await database.execute("INSERT INTO datasets (name) SELECT 'more-' || g FROM generate_series(1, 20) g")
        sample_id = (await ingest([{}]))['ids'][0]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES (1, $1, 'clip', '[1,0]')",
            sample_id,
        )
        async with database.pool.acquire() as connection:
            assert await connection.fetchval('SHOW plan_cache_mode') == 'force_custom_plan'
            async with connection.transaction():
                search = await connection.prepare(
                    """SELECT e.sample_id FROM embeddings e JOIN samples s ON s.dataset_id = e.dataset_id
                       AND s.id = e.sample_id WHERE s.dataset_id = $1 AND e.field_name = $2""",
                )
                for _ in range(8):
                    assert await search.fetch(1, 'clip')
                # The foreign key's check into embeddings is a cached plan of its own.
                for x in range(8):
                    await connection.execute(
                        "INSERT INTO embedding_coordinates (sample_id, field_name, x, y) VALUES ($1, 'clip', $2, 0) "
                        'ON CONFLICT (sample_id, field_name) DO UPDATE SET x = EXCLUDED.x',
                        sample_id,
                        x,
                    )
                assert await partitions_locked(connection) == 1
            # The delete trigger reads embeddings with each dataset's id as a value, whatever the plan cache mode.
            await connection.execute('SET plan_cache_mode = auto')
            async with connection.transaction():
                await connection.execute('DELETE FROM embedding_coordinates WHERE sample_id = $1', sample_id)
                assert await partitions_locked(connection) == 1
            await connection.execute('RESET plan_cache_mode')

    registry_database(scenario)


def test_creating_a_dataset_beside_a_writer_waits_for_it_and_never_deadlocks(registry_database):
    """The partition's ATTACH locked samples, coordinates and pending rows one at a time: holding samples while it
    waited for coordinates, it deadlocked with a writer that held coordinates and then wrote samples. Now it takes
    them all without waiting, holds nothing while the writer works, and is created once the writer commits."""

    async def scenario(database):
        sample_id = (await ingest([{}]))['ids'][0]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES (1, $1, 'clip', '[1,0]')",
            sample_id,
        )
        writer = await database.pool.acquire()
        try:
            async with writer.transaction():
                await writer.execute(
                    "INSERT INTO embedding_coordinates (sample_id, field_name, x, y) VALUES ($1, 'clip', 1, 2)",
                    sample_id,
                )
                creating = asyncio.create_task(create(database, 'beside'))
                await asyncio.sleep(1.5)  # past deadlock_timeout
                assert not creating.done()
                await writer.execute("UPDATE samples SET tags = '{reviewed}' WHERE id = $1", sample_id)
            created = await asyncio.wait_for(creating, 15)
        finally:
            await database.pool.release(writer)
        assert f'embeddings_d{created["id"]}' in await partitions(database)
        assert await database.fetchval('SELECT tags FROM samples WHERE id = $1', sample_id) == ['reviewed']

    registry_database(scenario)


def test_creating_a_dataset_while_writers_keep_overlapping_gets_its_turn(registry_database, monkeypatch):
    """Writers whose samples transactions overlap leave no moment with samples free, so a create that only tried
    when it was free never got it. Queued, it is next once the running writes finish, ahead of the writers' next."""

    async def scenario(database):
        monkeypatch.setattr(datasets, '_PARTITION_RETRY_SECONDS', 3.0)
        first, second = (await ingest([{}, {}]))['ids']
        stop = asyncio.Event()

        async def writer(sample_id, offset):
            await asyncio.sleep(offset)
            async with database.pool.acquire() as connection:
                while not stop.is_set():
                    async with connection.transaction():
                        await connection.execute("UPDATE samples SET tags = '{busy}' WHERE id = $1", sample_id)
                        await asyncio.sleep(0.1)

        writers = [asyncio.create_task(writer(first, 0)), asyncio.create_task(writer(second, 0.05))]
        try:
            await asyncio.sleep(0.3)
            created = await asyncio.wait_for(create(database, 'between writes'), 10)
        finally:
            stop.set()
            await asyncio.gather(*writers)
        assert f'embeddings_d{created["id"]}' in await partitions(database)

    registry_database(scenario)


def test_ingest_plans_its_samples_generically_and_locks_only_its_datasets_partition(registry_database, monkeypatch):
    """Ingest's samples insert runs with generic plans (its row triggers never read embeddings); its vectors and their
    pending rows, whose foreign key checks embeddings, plan per statement and lock its dataset's partition only."""

    async def scenario(database):
        await database.execute("SELECT setval(pg_get_serial_sequence('datasets', 'id'), 10)")
        await database.execute("INSERT INTO datasets (name) SELECT 'more-' || g FROM generate_series(1, 20) g")
        seen = []
        original = sample_ingest.insert_samples

        async def counted(connection, *args, **kwargs):
            answer = await original(connection, *args, **kwargs)
            seen.append((await partitions_locked(connection), await connection.fetchval('SHOW plan_cache_mode')))
            return answer

        monkeypatch.setattr(datasets, 'insert_samples', counted)
        for batch in range(3):  # each statement past five runs on the pool's connection
            await datasets.add_samples(
                1,
                {
                    'samples': [
                        {'filepath': f'batch{batch}-{index}.jpg', 'embeddings': {'clip': [1, index]}}
                        for index in range(8)
                    ],
                },
                user=USER,
            )
        assert seen == [(1, 'force_custom_plan')] * 3
        assert await database.fetchval('SELECT count(*) FROM embeddings_d1') == 24

    registry_database(scenario)
