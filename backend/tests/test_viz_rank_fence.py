"""A full rank rebuild fences only its own dataset, on disposable PostgreSQL.

recompute_viz_rank(full) takes advisory 20261020 exclusive for its dataset (the coordinate triggers
take it shared) and the field-lifecycle lock 20261016 shared (every sample/dataset delete path takes
it exclusive first). It takes no table locks, so other datasets' writes, maintenance and the
existing lock orders of delete/clone/rename paths are unaffected.

Races pause the rebuild with an advisory lock in a statement trigger on embedding_viz_rank that fires
only in the rebuild's session (custom setting test.pause), run one concurrent writer, release the
rebuild, then compare the rank cache with embedding_coordinates and the counter with membership.
"""

import asyncio
import os
import random

import asyncpg
import pytest
from fastapi import HTTPException

from backend.field_registry import REQUEST_FULL_REFRESH
from backend.routers import datasets
from backend.tests import test_dataset_deletion as deletion_tests
from backend.tests.test_layout_coordinates import registry_database, seed

__all__ = ['registry_database']

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')

PAUSE_KEY = 648064
ADMIN = {'id': 1, 'role': 'admin'}
REBUILD = "SELECT recompute_viz_rank(1,'clip')"


async def fixture(db):
    await seed(db, 3)  # dataset 1: samples 1..3 with 'clip' vectors
    await db.execute("INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES(900,2,'other','image')")
    await db.execute("INSERT INTO embeddings(sample_id,field_name,vector) VALUES(900,'clip','[1,2,3]')")
    await db.execute("INSERT INTO embedding_coordinates VALUES(1,'clip',1,1),(2,'clip',2,2)")
    await db.execute(REBUILD)
    await db.execute(
        f"""CREATE FUNCTION test_pause() RETURNS trigger AS $$
        BEGIN
            IF current_setting('test.pause', true) = TG_WHEN THEN PERFORM pg_advisory_xact_lock({PAUSE_KEY}); END IF;
            RETURN NULL;
        END $$ LANGUAGE plpgsql;
        CREATE TRIGGER test_pause_before BEFORE DELETE ON embedding_viz_rank
            FOR EACH STATEMENT EXECUTE FUNCTION test_pause();
        CREATE TRIGGER test_pause_after AFTER DELETE ON embedding_viz_rank
            FOR EACH STATEMENT EXECUTE FUNCTION test_pause();""",
    )


async def waiting(db, pid, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not await db.fetchval('SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND NOT granted)', pid):
            await asyncio.sleep(0.01)


async def waits(db, pid, timeout=1.0):
    try:
        await waiting(db, pid, timeout)
    except TimeoutError:
        return False
    return True


async def assert_cache_and_counter_exact(db):
    ranks = await db.fetch('SELECT sample_id,x,y FROM embedding_viz_rank WHERE dataset_id=1 ORDER BY 1')
    coords = await db.fetch(
        'SELECT c.sample_id,c.x::real AS x,c.y::real AS y FROM embedding_coordinates c '
        "JOIN samples s ON s.id=c.sample_id WHERE s.dataset_id=1 AND c.field_name='clip' ORDER BY 1",
    )
    assert [tuple(r) for r in ranks] == [tuple(r) for r in coords]
    total = await db.fetchval("SELECT total_points FROM embedding_viz_meta WHERE dataset_id=1 AND field_name='clip'")
    assert total == len(ranks)


RACES = {
    # Same dataset: the writer waits for the rebuild and the cache stays exact.
    'insert': ('BEFORE', "INSERT INTO embedding_coordinates VALUES(3,'clip',3,3)", True),
    'move': ('AFTER', 'UPDATE embedding_coordinates SET x=x+100 WHERE sample_id=1', True),
    'clear': ('AFTER', 'DELETE FROM embedding_coordinates WHERE sample_id=1', True),
    # Delete paths take the field-lifecycle lock exclusive before deleting.
    'sample_delete': ('AFTER', 'SELECT pg_advisory_xact_lock(20261016, 1); DELETE FROM samples WHERE id=1', True),
    # Another dataset: nothing waits.
    'other_sample_insert': (
        'AFTER',
        "INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES(901,2,'other-2','image')",
        False,
    ),
    'other_tag': ('AFTER', "UPDATE samples SET tags=ARRAY['x'] WHERE id=900", False),
    'other_dataset_row': ('AFTER', "UPDATE datasets SET description='d' WHERE id=2", False),
    'other_embedding': ('AFTER', 'UPDATE embeddings SET field_name=field_name WHERE sample_id=900', False),
    'other_coordinate': ('AFTER', "INSERT INTO embedding_coordinates VALUES(900,'clip',9,9)", False),
}


@pytest.mark.parametrize('case', list(RACES))
def test_rebuild_fences_its_own_dataset_only(registry_database, case):
    when, sql, same_dataset = RACES[case]

    async def scenario(db):
        await fixture(db)
        async with db.pool.acquire() as blocker, db.pool.acquire() as rebuild, db.pool.acquire() as writer:
            await blocker.execute(f'SELECT pg_advisory_lock({PAUSE_KEY})')
            await rebuild.execute(f"SET test.pause = '{when}'")
            pid = await rebuild.fetchval('SELECT pg_backend_pid()')
            rebuilt = asyncio.create_task(rebuild.execute(REBUILD))
            await waiting(db, pid)
            written = asyncio.create_task(writer.execute(sql))
            done, _ = await asyncio.wait({written}, timeout=0.5)
            await blocker.execute(f'SELECT pg_advisory_unlock({PAUSE_KEY})')
            await asyncio.gather(rebuilt, written)
            await rebuild.execute('RESET test.pause')
        assert (not done) == same_dataset
        await assert_cache_and_counter_exact(db)

    registry_database(scenario)


@pytest.mark.parametrize(
    'first, then, args',
    [
        # clone_field / rename_field: UPDATE samples, then REQUEST_FULL_REFRESH (UPDATE datasets).
        ("UPDATE samples SET metadata = metadata || '{\"copy\": 1}' WHERE dataset_id = 2", REQUEST_FULL_REFRESH, (2,)),
        # Embedding write, then a sample write (an e2e deadlock against table-locking rebuilds).
        (
            'UPDATE embeddings SET field_name = field_name WHERE sample_id = 900',
            "UPDATE samples SET tags = ARRAY['t'] WHERE id = 900",
            (),
        ),
    ],
)
def test_rebuild_never_waits_on_another_datasets_transaction(registry_database, first, then, args):
    async def scenario(db):
        await fixture(db)
        async with db.pool.acquire() as other, db.pool.acquire() as rebuild:
            transaction = other.transaction()
            await transaction.start()
            await other.execute(first)
            pid = await rebuild.fetchval('SELECT pg_backend_pid()')
            rebuilt = asyncio.create_task(rebuild.execute(REBUILD))
            rebuild_waited = await waits(db, pid)
            # A long first statement (minutes on large datasets) lets the rebuild's own deadlock check pass,
            # so a lock-order inversion would abort this transaction's next statement.
            await asyncio.sleep(1.5)
            await other.execute(then, *args)
            await transaction.commit()
            await rebuilt
        assert not rebuild_waited
        await assert_cache_and_counter_exact(db)

    registry_database(scenario)


@pytest.mark.parametrize('holder', ['share_update_exclusive', 'create_index_concurrently'])
def test_rebuild_does_not_queue_behind_maintenance(registry_database, holder):
    """CREATE INDEX CONCURRENTLY (HNSW builds, field promotion), VACUUM and ANALYZE hold SHARE UPDATE
    EXCLUSIVE for a long time. A rebuild must not queue behind them holding locks other writers need."""

    async def scenario(db):
        await fixture(db)
        async with (
            db.pool.acquire() as maintenance,
            db.pool.acquire() as build,
            db.pool.acquire() as rebuild,
            db.pool.acquire() as writer,
        ):
            transaction = maintenance.transaction()
            await transaction.start()
            built = None
            if holder == 'share_update_exclusive':
                await maintenance.execute('LOCK TABLE embeddings IN SHARE UPDATE EXCLUSIVE MODE')
            else:
                # An open write keeps the concurrent build in its wait phase, holding SHARE UPDATE
                # EXCLUSIVE on embeddings (a real HNSW build holds it for ~30 min on large datasets).
                await maintenance.execute("INSERT INTO embeddings(sample_id,field_name) VALUES(900,'other')")
                build_pid = await build.fetchval('SELECT pg_backend_pid()')
                built = asyncio.create_task(
                    build.execute('CREATE INDEX CONCURRENTLY test_cic ON embeddings (field_name)'),
                )
                await waiting(db, build_pid)
            pid = await rebuild.fetchval('SELECT pg_backend_pid()')
            rebuilt = asyncio.create_task(rebuild.execute(REBUILD))
            rebuild_queued = await waits(db, pid)
            await writer.execute("SET lock_timeout = '2s'")
            for sql in (
                "INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES(902,2,'w','image')",
                "UPDATE samples SET tags=ARRAY['t'] WHERE id=900",
                "UPDATE datasets SET description='d' WHERE id=2",
            ):
                async with writer.transaction():
                    await writer.execute(sql)
            await transaction.rollback()
            await rebuilt
            if built is not None:
                await built
                assert await db.fetchval("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('test_cic')")
        assert not rebuild_queued
        await assert_cache_and_counter_exact(db)

    registry_database(scenario)


def test_delete_dataset_endpoint_during_another_datasets_rebuild(registry_database):
    """Dataset 1's full rebuild holds its rank fence until commit. Deleting dataset 2 (a runner job since
    060: seal, then embeddings -> samples in units) must run to the end meanwhile: no waits, no deadlock."""

    async def scenario(db):
        await fixture(db)
        async with db.pool.acquire() as rebuild:
            async with rebuild.transaction():
                await rebuild.execute(REBUILD)
                assert (await datasets.delete_dataset(2, ADMIN))['state'] == 'queued'
                async with asyncio.timeout(30):
                    await deletion_tests.finish(db)
                assert await db.fetchval('SELECT count(*) FROM datasets WHERE id=2') == 0
        await assert_cache_and_counter_exact(db)

    registry_database(scenario)


def test_delete_sample_endpoint_waits_for_its_datasets_rebuild(registry_database):
    """Without the shared lifecycle lock in the rebuild, the rank FK cascade and the rebuild's FK check
    on the deleted sample deadlock. With it, delete_sample (lifecycle exclusive) waits its turn."""

    async def scenario(db):
        await fixture(db)
        async with db.pool.acquire() as blocker, db.pool.acquire() as rebuild:
            await blocker.execute(f'SELECT pg_advisory_lock({PAUSE_KEY})')
            await rebuild.execute("SET test.pause = 'AFTER'")
            pid = await rebuild.fetchval('SELECT pg_backend_pid()')
            rebuilt = asyncio.create_task(rebuild.execute(REBUILD))
            await waiting(db, pid)
            deleted = asyncio.create_task(datasets.delete_sample(1, 1, ADMIN))
            await asyncio.sleep(1.0)  # inside delete_sample's bounded lifecycle wait (2 s, then 409)
            assert not deleted.done()
            await blocker.execute(f'SELECT pg_advisory_unlock({PAUSE_KEY})')
            outcomes = await asyncio.gather(deleted, rebuilt, return_exceptions=True)
            await rebuild.execute('RESET test.pause')
        assert not [o for o in outcomes if isinstance(o, (asyncpg.PostgresError, HTTPException))]
        assert await db.fetchval('SELECT count(*) FROM samples WHERE id=1') == 0
        await assert_cache_and_counter_exact(db)

    registry_database(scenario)


N = 40  # samples per dataset in the random-concurrency test


def random_statement(rng):
    ds = rng.choice((1, 2))
    base = 0 if ds == 1 else 1000
    sid = base + rng.randint(1, N)
    kind = rng.choice(['insert', 'upsert', 'move', 'clear', 'embedding', 'sample_delete', 'full', 'incremental'])
    if kind == 'insert':
        return (
            f"INSERT INTO embedding_coordinates SELECT sample_id,'clip',{rng.random()},{rng.random()} "
            f"FROM embeddings WHERE sample_id={sid} AND field_name='clip' ON CONFLICT DO NOTHING"
        )
    if kind == 'upsert':  # a retried SAVE_COORDINATES-shaped batch
        ids = ','.join(str(base + rng.randint(1, N)) for _ in range(rng.randint(1, 6)))
        return (
            "INSERT INTO embedding_coordinates AS c (sample_id, field_name, x, y) "
            'SELECT e.sample_id, e.field_name, e.sample_id*0.5, 1 FROM embeddings e '
            f"WHERE e.sample_id = ANY(ARRAY[{ids}]) AND e.field_name='clip' "
            'ON CONFLICT (sample_id, field_name) DO UPDATE SET x = EXCLUDED.x, y = EXCLUDED.y '
            'WHERE (c.x, c.y) IS DISTINCT FROM (EXCLUDED.x, EXCLUDED.y)'
        )
    if kind == 'move':
        return f'UPDATE embedding_coordinates SET x = x + 1 WHERE sample_id = {sid}'
    if kind == 'clear':
        return f'DELETE FROM embedding_coordinates WHERE sample_id = {sid}'
    if kind == 'embedding':
        return (
            f"DELETE FROM embeddings WHERE sample_id={sid} AND field_name='clip'; "
            f"INSERT INTO embeddings(sample_id,field_name,vector) SELECT {sid},'clip','[1,2,3]' "
            f'WHERE EXISTS (SELECT 1 FROM samples WHERE id={sid})'
        )
    if kind == 'sample_delete':  # as the delete endpoints: lifecycle lock exclusive first
        return (
            f'SELECT pg_advisory_xact_lock(20261016, {ds}); DELETE FROM samples WHERE id={sid}; '
            f"INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES({sid},{ds},'re{sid}','image'); "
            f"INSERT INTO embeddings(sample_id,field_name,vector) VALUES({sid},'clip','[1,2,3]')"
        )
    return f"SELECT recompute_viz_rank({ds},'clip',{kind == 'full'})"


@pytest.mark.parametrize('seed_value', [1, 2, 3])
def test_counter_and_cache_exact_under_random_concurrency(registry_database, seed_value):
    async def scenario(db):
        for ds, base in ((1, 0), (2, 1000)):
            await db.execute(
                "INSERT INTO samples(id,dataset_id,filepath,media_type) "
                "SELECT $2+i,$1,'f'||($2+i),'image' FROM generate_series(1,$3::int) i",
                ds,
                base,
                N,
            )
            await db.execute(
                "INSERT INTO embeddings(sample_id,field_name,vector) SELECT $1+i,'clip','[1,2,3]' "
                'FROM generate_series(1,$2::int) i',
                base,
                N,
            )
            await db.execute(
                "INSERT INTO embedding_coordinates SELECT $1+i,'clip',i,i FROM generate_series(1,$2::int/2) i",
                base,
                N,
            )
            await db.execute("SELECT recompute_viz_rank($1,'clip')", ds)

        async def worker(rng):
            for _ in range(60):
                async with db.pool.acquire() as conn:
                    transaction = conn.transaction()
                    await transaction.start()
                    try:
                        await conn.execute(random_statement(rng))
                    except (
                        asyncpg.DeadlockDetectedError,
                        asyncpg.UniqueViolationError,
                        asyncpg.ForeignKeyViolationError,
                    ):
                        # Raw statements on the same rows can race (re-inserted ids, a sample delete
                        # against an embedding re-add); a rolled-back one must not corrupt anything.
                        await transaction.rollback()
                        continue
                    if rng.random() < 0.1:
                        await transaction.rollback()
                    else:
                        await transaction.commit()

        await asyncio.gather(*(worker(random.Random(seed_value * 100 + w)) for w in range(4)))
        drift = await db.fetch(
            'SELECT m.dataset_id, m.total_points, (SELECT count(*) FROM embedding_viz_rank r '
            'WHERE r.dataset_id=m.dataset_id AND r.field_name=m.field_name) AS n FROM embedding_viz_meta m',
        )
        assert all(r['total_points'] == r['n'] for r in drift), drift
        mismatch = await db.fetch(
            """WITH c AS (SELECT s.dataset_id, e.field_name, e.sample_id, e.x::real x, e.y::real y
                          FROM embedding_coordinates e JOIN samples s ON s.id = e.sample_id
                          JOIN embedding_viz_meta m ON m.dataset_id = s.dataset_id AND m.field_name = e.field_name),
                    r AS (SELECT dataset_id, field_name, sample_id, x, y FROM embedding_viz_rank)
               (SELECT * FROM r EXCEPT SELECT * FROM c) UNION ALL (SELECT * FROM c EXCEPT SELECT * FROM r)""",
        )
        assert mismatch == []

    registry_database(scenario)
