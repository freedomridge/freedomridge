"""Deletion fences and bounded fan-out across concepts, duplicates, coordinates and deletion."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import numpy as np
import pytest

from backend import dataset_deletion, duplicate_jobs, job_queue
from backend.coordinates import SAVE_PROJECTED_COORDINATES
from backend.routers import embeddings
from backend.tests import test_field_registry_postgres
from backend.tests.test_dataset_deletion import claim, finish, seed, step
from backend.tests.test_field_registry_postgres import USER

registry_database = test_field_registry_postgres.registry_database

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL')
DB = Path(dataset_deletion.__file__).parent / 'db'
NEW_TABLES = (
    'embedding_coordinates',
    'embedding_projection_pending',
    'embedding_viz_rank',
    'embedding_viz_meta',
    'duplicate_configs',
    'duplicate_members',
    'duplicate_pending',
)


async def seed_stack(db):
    await seed(db)
    await db.execute(
        """INSERT INTO embedding_coordinates SELECT sample_id,field_name,1,2 FROM embeddings WHERE field_name='patch_1';
           SELECT recompute_viz_rank(1,'patch_1');
           INSERT INTO duplicate_configs(dataset_id,config) VALUES(1,'_any'),(1,'hash');
           INSERT INTO duplicate_members SELECT 1,c.config,s.id,'key-'||n
             FROM duplicate_configs c CROSS JOIN samples s CROSS JOIN generate_series(1,25) n
             WHERE s.dataset_id=1;
           INSERT INTO duplicate_pending(dataset_id,block_id) VALUES(1,-1) ON CONFLICT DO NOTHING""",
    )


def test_new_children_are_drained_in_measured_units(registry_database, monkeypatch):
    monkeypatch.setattr(dataset_deletion, 'BATCH_SIZE', 7)
    monkeypatch.setattr(dataset_deletion, 'BYTE_BUDGET', 400)

    async def scenario(db):
        await seed_stack(db)
        tables = NEW_TABLES + (
            'samples',
            'embeddings',
            'sample_hashes',
            'embedding_concepts',
            'embedding_concept_scores',
            'concept_score_pending',
            'concept_score_count_deltas',
            'concept_embedding_pending',
            'dataset_versions',
            'version_snapshots',
        )

        async def count():
            return sum([await db.fetchval(f'SELECT count(*) FROM {table}') for table in tables])

        await dataset_deletion.submit(db.pool, 1, None)
        current = await claim(db)
        before = await count()
        while current['state'] != 'succeeded':
            current = await step(db, current)
            after = await count()
            # Count physical child removals as well as the publisher's target rows.
            assert before - after <= 7
            assert current['checkpoint'].get('last_unit_bytes', 0) <= 400
            before = after
        for table in NEW_TABLES:
            where = '' if table == 'embedding_coordinates' else ' WHERE dataset_id=1'
            assert await db.fetchval(f'SELECT count(*) FROM {table}{where}') == 0
        assert await db.fetchval('SELECT count(*) FROM samples WHERE dataset_id=2') == 1

    registry_database(scenario)


@pytest.mark.parametrize('table', ['embedding_viz_rank', 'embedding_coordinates', 'sample_hashes', 'samples'])
def test_deletion_triggers_do_no_disposable_work(registry_database, table):
    async def scenario(db):
        await seed_stack(db)
        async with db.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('fridge.deletion_dataset','1',TRUE)")
            # Keep the dataset active here so missing suppression cannot hide behind a write-guard error.
            await conn.execute('DELETE FROM duplicate_pending; DELETE FROM embedding_projection_pending')
            meta = await conn.fetch('SELECT * FROM embedding_viz_meta')
            await conn.execute(
                f'DELETE FROM {table} WHERE sample_id=1' if table != 'samples' else 'DELETE FROM samples WHERE id=1',
            )
            assert await conn.fetchval('SELECT count(*) FROM duplicate_pending') == 0
            assert await conn.fetchval('SELECT count(*) FROM embedding_projection_pending') == 0
            assert await conn.fetch('SELECT * FROM embedding_viz_meta') == meta

    registry_database(scenario)


@pytest.mark.parametrize(
    'table',
    ['duplicate_configs', 'duplicate_members', 'duplicate_pending', 'embedding_projection_pending'],
)
def test_new_tables_reject_writes_after_seal(registry_database, table):
    async def scenario(db):
        await seed_stack(db)
        await dataset_deletion.submit(db.pool, 1, None)
        column = 'config' if table in ('duplicate_configs', 'duplicate_members') else 'dataset_id'
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being deleted'):
            await db.execute(f'UPDATE {table} SET {column}={column}')

    registry_database(scenario)


@pytest.mark.parametrize('writer', ['upload', 'placement', 'reduce', 'rank'])
def test_coordinate_publication_rejects_seal(registry_database, monkeypatch, writer):
    from backend.tests.test_layout_coordinates import seed as seed_coordinates

    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed_coordinates(db)
        # No meta or ranks: only the coordinate guard can reject initial publication.
        await dataset_deletion.submit(db.pool, 1, None)
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being deleted'):
            if writer == 'upload':
                await embeddings.upload_coordinates(
                    1,
                    'clip',
                    [embeddings.CoordinateUpdate(sample_id=1, x=1, y=2)],
                    USER,
                )
            elif writer == 'placement':  # the statement map placement writes with (backend/map_placement.py)
                await db.execute(SAVE_PROJECTED_COORDINATES, [1], [1.0], [2.0], 1, 'clip', [None])
            elif writer == 'reduce':
                monkeypatch.setitem(
                    sys.modules,
                    'umap',
                    SimpleNamespace(
                        UMAP=lambda **kwargs: SimpleNamespace(
                            fit_transform=lambda vectors: np.zeros((len(vectors), 2)),
                        ),
                    ),
                )
                await embeddings.reduce_embeddings(1, embeddings.ReduceRequest(field_name='clip'), USER)
            else:
                await db.execute("SELECT recompute_viz_rank(1,'clip',FALSE)")
        assert await db.fetchval('SELECT count(*) FROM embedding_coordinates') == 0

    registry_database(scenario)


@pytest.mark.parametrize('configs', [False, True])
def test_duplicate_scheduler_skips_sealed_cleanup_and_admission(registry_database, configs):
    async def scenario(db):
        await seed_stack(db)
        if not configs:
            await db.execute('DELETE FROM duplicate_configs')
        await dataset_deletion.submit(db.pool, 1, None)
        before = await db.fetch('SELECT * FROM duplicate_pending WHERE dataset_id=1 ORDER BY 1,2,3')
        assert await duplicate_jobs.schedule_duplicates(db.pool) == []
        assert await db.fetch('SELECT * FROM duplicate_pending WHERE dataset_id=1 ORDER BY 1,2,3') == before
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being deleted'):
            await duplicate_jobs.request_refresh(db.pool, 1, 'hash')

    registry_database(scenario)


def test_running_duplicate_owner_is_cancelled_at_deletion_admission(registry_database):
    async def scenario(db):
        await seed_stack(db)
        await duplicate_jobs.request_refresh(db.pool, 1, 'hash')
        current = await job_queue.claim_job(db.pool, 'duplicate-test', kinds=[duplicate_jobs.KIND])
        assert current is not None
        result = await duplicate_jobs.handler(db.pool)(current)
        await dataset_deletion.submit(db.pool, 1, None)
        current = await job_queue.publish_step(db.pool, current['id'], current['lease_token'], publisher=result.publish)
        assert current['state'] == 'cancelled'
        await finish(db)

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['vector', 'coordinate', 'truncate'])
def test_deletion_context_skips_projection_and_rank_queue_work(registry_database, operation):
    async def scenario(db):
        await seed_stack(db)
        async with db.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('fridge.deletion_dataset','1',TRUE)")
            await conn.execute('DELETE FROM duplicate_pending; DELETE FROM embedding_projection_pending')
            meta = await conn.fetch('SELECT * FROM embedding_viz_meta')
            if operation == 'vector':
                await conn.execute("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=1")
            elif operation == 'coordinate':
                await conn.execute('UPDATE embedding_coordinates SET x=42 WHERE sample_id=1')
            else:
                await conn.execute('TRUNCATE sample_hashes')
            assert await conn.fetchval('SELECT count(*) FROM duplicate_pending') == 0
            assert await conn.fetchval('SELECT count(*) FROM embedding_projection_pending') == 0
            assert await conn.fetch('SELECT * FROM embedding_viz_meta') == meta

    registry_database(scenario)


async def stack_ids(db):
    """Sample IDs of datasets 1 and 2 plus dataset 1's concept and version, after seed_stack."""
    first = await db.fetchval('SELECT min(id) FROM samples WHERE dataset_id=1')
    second = await db.fetchval('SELECT min(id) FROM samples WHERE dataset_id=2')
    concept = await db.fetchval('SELECT id FROM embedding_concepts WHERE dataset_id=1')
    version = await db.fetchval('SELECT id FROM dataset_versions WHERE dataset_id=1')
    return {'first': first, 'second': second, 'concept': concept, 'version': version}


# Row-level UPDATE guards: dataset-keyed rows and sample/concept/version-keyed rows, in place and moves.
UPDATE_WRITES = {
    'samples in place': ('UPDATE samples SET metadata=metadata WHERE dataset_id=1', ()),
    'samples move out': ("UPDATE samples SET dataset_id=2,filepath='moved' WHERE id=$1", ('first',)),
    'samples move in': ("UPDATE samples SET dataset_id=1,filepath='moved' WHERE id=$1", ('second',)),
    'viz rank in place': ('UPDATE embedding_viz_rank SET x=x+1', ()),
    'embeddings in place': ("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=$1", ('first',)),
    'embeddings move in': ("UPDATE embeddings SET sample_id=$1 WHERE field_name='moved'", ('first',)),
    'embeddings move out': (
        "UPDATE embeddings SET sample_id=$1 WHERE sample_id=$2 AND field_name='patch_2'",
        ('second', 'first'),
    ),
    'coordinates in place': ('UPDATE embedding_coordinates SET x=x+1 WHERE sample_id=$1', ('first',)),
    'hashes in place': ("UPDATE sample_hashes SET hash_hex='late' WHERE sample_id=$1", ('first',)),
    'scores in place': ('UPDATE embedding_concept_scores SET score=0.5 WHERE concept_id=$1', ('concept',)),
    'score queue in place': ('UPDATE concept_score_pending SET block_id=block_id+1 WHERE concept_id=$1', ('concept',)),
    'snapshots in place': ("UPDATE version_snapshots SET metadata_hash='late' WHERE version_id=$1", ('version',)),
}


@pytest.mark.parametrize('write', sorted(UPDATE_WRITES))
def test_row_update_guard_refuses_in_place_updates_and_moves(registry_database, write):
    async def scenario(db):
        await seed_stack(db)
        ids = await stack_ids(db)
        await db.execute(
            "INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) VALUES("
            "(SELECT dataset_id FROM samples WHERE id = $1),$1,'moved','[1,0]')",
            ids['second'],
        )
        # Moves must meet only the guard, not unique or projection-queue foreign keys.
        await db.execute('DELETE FROM embedding_projection_pending')
        await dataset_deletion.submit(db.pool, 1, None)
        sql, args = UPDATE_WRITES[write]
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being deleted'):
            await db.execute(sql, *(ids[name] for name in args))
        # The other dataset's rows stay writable while dataset 1 is deleting.
        await db.execute("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=$1", ids['second'])
        await db.execute("UPDATE samples SET tags=ARRAY['ok'] WHERE id=$1", ids['second'])

    registry_database(scenario)


@pytest.mark.parametrize(
    'write',
    ['coordinates in place', 'hashes in place', 'scores in place', 'snapshots in place', 'viz rank in place'],
)
def test_row_update_guard_refuses_old_repeatable_read_snapshot(registry_database, write):
    async def scenario(db):
        await seed_stack(db)
        ids = await stack_ids(db)
        sql, args = UPDATE_WRITES[write]
        async with db.pool.acquire() as writer, writer.transaction(isolation='repeatable_read'):
            await writer.fetchval('SELECT count(*) FROM samples')
            await db.execute("UPDATE datasets SET description='new metadata' WHERE id=1")
            await dataset_deletion.submit(db.pool, 1, None)
            with pytest.raises(asyncpg.SerializationError):
                await writer.execute(sql, *(ids[name] for name in args))

    registry_database(scenario)


def test_cached_dataset_check_follows_its_lock(registry_database):
    async def scenario(db):
        await seed_stack(db)
        async with db.pool.acquire() as writer:
            async with writer.transaction():
                savepoint = writer.transaction()
                await savepoint.start()
                await writer.execute('UPDATE samples SET tags=tags WHERE dataset_id=1')
                assert await writer.fetchval("SELECT current_setting('fridge.writable_dataset')") == '1'
                await savepoint.rollback()
                # Rolling back the subtransaction released the key-share lock and the cached check together.
                assert not await writer.fetchval("SELECT current_setting('fridge.writable_dataset')")
                await dataset_deletion.submit(db.pool, 1, None)
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being deleted'):
                    await writer.execute('UPDATE samples SET tags=tags WHERE dataset_id=1')
            # A sealing transaction cannot reuse a check it made before the seal.
            async with writer.transaction():
                await writer.execute('UPDATE samples SET tags=tags WHERE dataset_id=2')
                await writer.execute('SELECT id FROM datasets WHERE id=2 FOR UPDATE')
                await writer.execute('UPDATE datasets SET deleting=TRUE WHERE id=2')
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being deleted'):
                    await writer.execute('UPDATE samples SET tags=tags WHERE dataset_id=2')

    registry_database(scenario)


def test_update_guards_have_no_transition_tables():
    lines = [line for line in (DB / 'schema.sql').read_text().splitlines() if 'TRIGGER deletion_guard_update' in line]
    assert len(lines) == 3
    assert all('REFERENCING' not in line and 'BEFORE UPDATE' in line for line in lines)


@pytest.mark.parametrize(
    ('sql', 'transition_free'),
    [
        ("""UPDATE samples SET metadata=metadata || '{"reviewed":true}' WHERE dataset_id=1""", True),
        ('UPDATE embedding_concept_scores SET score=score/2', True),
        ("UPDATE version_snapshots SET metadata_hash='rehashed'", True),
        # The coordinate tables' own projection/rank statement triggers read transitions on these two tables.
        ("UPDATE embeddings SET vector='[0,1]'", False),
        ('UPDATE embedding_coordinates SET x=x+1', False),
    ],
)
def test_bulk_updates_run_no_per_row_guard_work(registry_database, sql, transition_free):
    async def scenario(db):
        await seed_stack(db)
        await db.execute(
            """INSERT INTO samples(dataset_id,filepath,media_type,metadata)
               SELECT 1,'bulk-'||n,'image',jsonb_build_object('blob',repeat(md5(n::text),50))
               FROM generate_series(1,1000) n;
               INSERT INTO embeddings(dataset_id,sample_id,field_name,vector)
               SELECT dataset_id,id,'patch_1','[1,0]' FROM samples WHERE filepath LIKE 'bulk-%';
               INSERT INTO embedding_coordinates SELECT id,'patch_1',1,2 FROM samples WHERE filepath LIKE 'bulk-%';
               INSERT INTO embedding_concept_scores SELECT concept.id,sample.id,0.5
               FROM embedding_concepts concept CROSS JOIN samples sample WHERE sample.filepath LIKE 'bulk-%';
               INSERT INTO version_snapshots(version_id,sample_id,metadata_hash)
               SELECT version.id,sample.id,'hash' FROM dataset_versions version CROSS JOIN samples sample
               WHERE sample.filepath LIKE 'bulk-%'""",
        )

        async def explain():
            async with db.pool.acquire() as connection:
                transaction = connection.transaction()
                await transaction.start()
                try:
                    await connection.execute("SET LOCAL work_mem='64kB'")
                    plan = json.loads(await connection.fetchval(f'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) {sql}'))[0]
                finally:
                    await transaction.rollback()
            calls = {trigger['Trigger Name']: trigger['Calls'] for trigger in plan.get('Triggers', [])}
            return plan['Plan']['Temp Written Blocks'], calls.get('deletion_guard_update', 0)

        written, calls = await explain()
        assert written == 0 or not transition_free
        # One locking check for dataset 1; parent-keyed rows need none while no deletion is visible.
        assert calls == (1 if sql.startswith('UPDATE samples') else 0)
        await dataset_deletion.submit(db.pool, 2, None)
        written, calls = await explain()
        assert written == 0 or not transition_free
        if sql.startswith('UPDATE samples'):
            assert calls == 1
        else:
            # While any dataset is deleting, parent-keyed rows are checked one by one.
            assert calls > 1000

    registry_database(scenario)
