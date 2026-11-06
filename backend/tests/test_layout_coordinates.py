"""Coordinate isolation, migration fidelity and bounded projection work on PostgreSQL."""

import json
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import numpy as np
import pytest
from fridge.core import client as sdk_client
from fridge.core import dataset as sdk_dataset
from pydantic import ValidationError

from backend.routers import embeddings, sampling
from backend.tests import test_field_registry_postgres

registry_database = test_field_registry_postgres.registry_database

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')
ROOT = Path(__file__).resolve().parents[2]
USER = {'id': 1, 'role': 'admin'}


async def seed(db, n=8, field='clip'):
    await db.execute(
        "INSERT INTO samples(id,dataset_id,filepath,media_type) "
        "SELECT i,1,'image-' || i,'image' FROM generate_series(1,$1::int) i ON CONFLICT DO NOTHING",
        n,
    )
    await db.execute(
        "INSERT INTO embeddings(sample_id,field_name,vector) "
        "SELECT i,$2,'[1,2,3]'::vector FROM generate_series(1,$1::int) i",
        n,
        field,
    )


async def upload(ids, shift=0, field='clip'):
    return await embeddings.upload_coordinates(
        1,
        field,
        [embeddings.CoordinateUpdate(sample_id=i, x=i + shift, y=i / 3 + shift) for i in ids],
        USER,
    )


async def points(db):
    return [tuple(r) for r in await db.fetch('SELECT sample_id,field_name,x,y FROM embedding_coordinates ORDER BY 1,2')]


def test_api_coordinate_writes_do_not_update_embedding_tuples(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db)
        before = await db.fetch('SELECT id,ctid::text,xmin::text FROM embeddings ORDER BY id')
        await upload(range(1, 9))
        await upload(range(1, 9), shift=10)
        assert await db.fetch('SELECT id,ctid::text,xmin::text FROM embeddings ORDER BY id') == before
        assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending') == 0
        assert await db.fetchval('SELECT count(*) FROM embeddings WHERE x IS NOT NULL OR y IS NOT NULL') == 0

    registry_database(scenario)


def test_visualization_example_uses_supported_sdk_arguments(monkeypatch):
    calls = []

    def compute(*, field_name, fit_sample_size):
        calls.append((field_name, fit_sample_size))
        return {'reduced': 5}

    urls = []

    def client(url=None, **_kwargs):
        urls.append(url)
        return SimpleNamespace(base_url=url, get_dataset=lambda name: {'id': 7} if name == 'dataset' else None)

    monkeypatch.setattr(sdk_client, 'Client', client)
    # The example looks the dataset up without creating it, then loads it by id.
    monkeypatch.setattr(
        sdk_dataset.Dataset,
        'from_id',
        staticmethod(lambda dataset_id: SimpleNamespace(compute_visualization=compute)),
    )
    monkeypatch.setattr(sys, 'argv', ['compute_visualization.py', 'dataset', 'clip', '--all'])
    monkeypatch.setenv('FRIDGE_API_TOKEN', 'fixture-token')  # the example authenticates; a token skips the password
    monkeypatch.setenv('FRIDGE_API_URL', 'http://127.0.0.1:65530/api')
    runpy.run_path(str(ROOT / 'examples/sdk/compute_visualization.py'), run_name='__main__')
    assert calls == [('clip', -1)]
    assert urls == ['http://127.0.0.1:65530/api']  # honours FRIDGE_API_URL, never a hard-coded localhost:8000


def test_server_reduce_writes_separate_coordinates(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    monkeypatch.setitem(
        sys.modules,
        'umap',
        SimpleNamespace(
            UMAP=lambda **kwargs: SimpleNamespace(fit_transform=lambda vectors: np.zeros((len(vectors), 2))),
        ),
    )

    async def scenario(db):
        await seed(db)
        before = await db.fetch('SELECT id,ctid::text,xmin::text FROM embeddings ORDER BY id')
        result = await embeddings.reduce_embeddings(1, embeddings.ReduceRequest(), USER)
        assert result['reduced'] == 8
        assert await db.fetch('SELECT id,ctid::text,xmin::text FROM embeddings ORDER BY id') == before
        assert await db.fetchval('SELECT count(*) FROM embedding_coordinates') == 8

    registry_database(scenario)


def test_unranked_patch_filtered_scatter_and_sampling(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        monkeypatch.setattr(sampling, 'db', db)
        await seed(db)
        await seed(db, field='clip_patch_2x2_r0c0')
        await upload(range(1, 9))
        await upload(range(1, 9), field='clip_patch_2x2_r0c0')
        # Unranked fields use the coordinate-based fallback.
        for field in ['clip', 'clip_patch_2x2']:
            result = await embeddings.get_embeddings_post(
                1,
                embeddings.EmbeddingsRequest(
                    field=field,
                    sample_pct=100,
                    filters={'field': 'id', 'operator': '>', 'value': 4},
                    include_fields=['filepath'],
                ),
            )
            assert {p['sample_id'] for p in result['points']} == {5, 6, 7, 8}
            assert all(p['filepath'].startswith('image-') for p in result['points'])
        result = await embeddings.lookup_patch_coordinates(
            1,
            embeddings.PatchLookupRequest(patches=[{'sample_id': 3, 'field_name': 'clip_patch_2x2_r0c0'}]),
        )
        assert result['points'][0]['x'] == 3
        result = await sampling.sparse_sample(1, sampling.SparseSampleRequest(count=3, fields=['clip']))
        assert result['candidate_count'] == 8 and result['count'] == 3
        fields = await sampling.get_available_fields(1)
        assert any(f['name'] == 'clip' for f in fields)
        await db.execute("SELECT recompute_viz_rank(1,'clip',TRUE)")
        binary = await embeddings.get_embeddings_post(1, embeddings.EmbeddingsRequest(sample_pct=100), format='bin')
        body = bytes(binary.body)
        header_size = int.from_bytes(body[:4], 'little')
        header = json.loads(body[4 : 4 + header_size])
        assert header['count'] == header['total'] == 8

    registry_database(scenario)


def test_rank_move_clear_new_and_retry(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db)
        await upload([1, 2, 3])
        assert await db.fetchval("SELECT recompute_viz_rank(1,'clip',TRUE)") == 3
        ranks = await db.fetch('SELECT sample_id,rank FROM embedding_viz_rank ORDER BY sample_id')
        await upload([1, 2, 3], shift=10)
        assert await db.fetch('SELECT sample_id,rank FROM embedding_viz_rank ORDER BY sample_id') == ranks
        assert await db.fetchval('SELECT rank_stale FROM embedding_viz_meta')
        assert (
            await db.fetchval(
                'SELECT count(*) FROM embedding_viz_rank r JOIN embedding_coordinates c USING(sample_id,field_name) '
                'WHERE (r.x,r.y) IS DISTINCT FROM (c.x::real,c.y::real)',
            )
            == 0
        )
        await db.execute("SELECT recompute_viz_rank(1,'clip',TRUE)")
        before = await db.fetch('SELECT sample_id,ctid::text,xmin::text FROM embedding_coordinates ORDER BY 1')
        meta = await db.fetchrow('SELECT * FROM embedding_viz_meta')
        await upload([1, 2, 3], shift=10)
        await db.execute('UPDATE embedding_coordinates SET x=x,y=y')
        assert await db.fetch('SELECT sample_id,ctid::text,xmin::text FROM embedding_coordinates ORDER BY 1') == before
        assert await db.fetchrow('SELECT * FROM embedding_viz_meta') == meta
        await db.execute('DELETE FROM embedding_coordinates WHERE sample_id=2')
        assert await db.fetchval('SELECT total_points FROM embedding_viz_meta') == 2
        assert await db.fetchval('SELECT rank_stale FROM embedding_viz_meta')
        assert await db.fetchval('SELECT count(*) FROM embedding_viz_rank WHERE sample_id=2') == 0
        assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending WHERE sample_id=2') == 1
        await upload([2, 4], shift=10)
        assert await db.fetchval('SELECT total_points FROM embedding_viz_meta') == 4
        assert await db.fetchval("SELECT recompute_viz_rank(1,'clip',FALSE)") == 0
        assert await db.fetchval('SELECT total_points FROM embedding_viz_meta') == 4

    registry_database(scenario)


@pytest.mark.parametrize('target', ['embeddings', 'samples', 'datasets'])
def test_cascades_and_rank_counts(registry_database, monkeypatch, target):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db)
        await upload([1, 2])
        await db.execute("SELECT recompute_viz_rank(1,'clip',TRUE)")
        key = 'sample_id' if target == 'embeddings' else 'id'
        await db.execute(f'DELETE FROM {target} WHERE {key}=1')
        assert await db.fetchval('SELECT count(*) FROM embedding_coordinates WHERE sample_id=1') == 0
        assert await db.fetchval('SELECT count(*) FROM embedding_viz_rank WHERE sample_id=1') == 0
        assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending WHERE sample_id=1') == 0
        if target != 'datasets':
            assert await db.fetchval('SELECT total_points FROM embedding_viz_meta') == 1
        else:
            assert await db.fetchval('SELECT count(*) FROM embedding_coordinates') == 0
            assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending') == 0

    registry_database(scenario)


def test_vector_change_queues_only_changed_points(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db)
        await upload(range(1, 9))
        await db.execute('UPDATE embeddings SET vector=vector')
        assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending') == 0
        await db.execute("UPDATE embeddings SET vector='[2,3,4]' WHERE sample_id IN (2,4)")
        assert await db.fetchval('SELECT count(*) FROM embedding_coordinates') == 6
        response = await embeddings.get_vectors(
            1,
            'clip',
            limit=10,
            offset=0,
            after_id=None,
            pending_only=True,
            response_format='json',
        )
        payload = json.loads(bytes(response.body))
        assert payload['total'] == 2
        assert [p['sample_id'] for p in payload['items']] == [2, 4]
        await upload([2])
        response = await embeddings.get_vectors(
            1,
            'clip',
            limit=10,
            offset=0,
            after_id=2,
            pending_only=True,
            response_format='json',
        )
        assert [p['sample_id'] for p in json.loads(bytes(response.body))['items']] == [4]
        await db.execute('UPDATE embeddings SET vector=NULL WHERE sample_id=4')
        assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending') == 0
        await db.execute("UPDATE embeddings SET vector='[1,2,3]' WHERE sample_id=4")
        assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending') == 1

    registry_database(scenario)


@pytest.mark.parametrize('x,y', [(float('nan'), 0), (float('inf'), 0), (0, float('-inf')), (None, 0), (0, None)])
def test_rejects_incomplete_nonfinite_coordinates(registry_database, x, y):
    with pytest.raises(ValidationError):
        embeddings.CoordinateUpdate(sample_id=1, x=x, y=y)

    async def scenario(db):
        await seed(db, 1)
        with pytest.raises((asyncpg.CheckViolationError, asyncpg.NotNullViolationError)):
            await db.execute(
                "INSERT INTO embedding_coordinates VALUES(1,'clip',$1,$2)",
                x,
                y,
            )
        assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending') == 1

    registry_database(scenario)


def test_coordinate_scope_and_duplicate_input(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db, 2)
        await db.execute("INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES(3,2,'foreign','image')")
        await db.execute("INSERT INTO embeddings(sample_id,vector) VALUES(3,'[1,2,3]')")
        await embeddings.upload_coordinates(
            1,
            'clip',
            [embeddings.CoordinateUpdate(sample_id=i, x=x, y=0) for i, x in [(1, 1), (1, 2), (3, 3), (99, 99)]],
            USER,
        )
        assert await points(db) == [(1, 'clip', 2.0, 0.0)]

    registry_database(scenario)


@pytest.mark.parametrize('dimensions', [512, 1024])
@pytest.mark.parametrize('projected', [False, True])
def test_repeated_coordinate_updates_leave_hnsw_size_unchanged(
    registry_database,
    monkeypatch,
    dimensions,
    projected,
):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await db.execute(
            "INSERT INTO samples(id,dataset_id,filepath,media_type) "
            "SELECT i,1,'image-' || i,'image' FROM generate_series(1,200) i",
        )
        # Insert at full size: rewriting seed()'s 3-d vectors leaves dead rows that VACUUM keeps while any
        # other transaction is open, and CREATE INDEX still casts them ("expected 1024 dimensions, not 3").
        await db.execute(
            "INSERT INTO embeddings(sample_id,field_name,vector) SELECT i,'clip',"
            f"(SELECT array_agg(sin(i*d)::real) FROM generate_series(1,{dimensions}) d)::vector "
            "FROM generate_series(1,200) i",
        )
        await db.execute(
            'ALTER TABLE embeddings SET (autovacuum_enabled=false);'
            'CREATE INDEX layout_test_hnsw ON embeddings USING hnsw '
            f'((vector::vector({dimensions})) vector_cosine_ops)',
        )
        before = await db.fetchval("SELECT pg_relation_size('layout_test_hnsw')")
        tuples = await db.fetch('SELECT id,ctid::text,xmin::text FROM embeddings ORDER BY id')
        versions = dict(await db.fetch('SELECT sample_id,xmin::text FROM embeddings'))
        # pgvector can reuse slots for identical vectors: a one-round size check misses the bug.
        for shift in range(12):
            if projected:
                await embeddings.upload_coordinates(
                    1,
                    'clip',
                    [
                        embeddings.CoordinateUpdate(
                            sample_id=i,
                            x=i + shift,
                            y=i / 3 + shift,
                            vector_version=versions[i],
                        )
                        for i in range(1, 201)
                    ],
                    USER,
                )
            else:
                await upload(range(1, 201), shift=shift)
        assert await db.fetchval("SELECT pg_relation_size('layout_test_hnsw')") == before
        assert await db.fetch('SELECT id,ctid::text,xmin::text FROM embeddings ORDER BY id') == tuples

    registry_database(scenario)


def test_clear_decrements_rank_total_on_both_storage_versions(registry_database, monkeypatch):
    """Clearing a point's coordinates removes its rank row and decrements the map's point count."""
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db, 3)
        await upload([1, 2, 3])
        await db.execute("SELECT recompute_viz_rank(1,'clip',TRUE)")
        if await db.fetchval("SELECT to_regclass(current_schema() || '.embedding_coordinates')"):
            await db.execute('DELETE FROM embedding_coordinates WHERE sample_id=2')
        else:
            await db.execute('UPDATE embeddings SET x=NULL,y=NULL WHERE sample_id=2')
        assert await db.fetchval('SELECT count(*) FROM embedding_viz_rank') == 2
        assert await db.fetchval('SELECT total_points FROM embedding_viz_meta') == 2

    registry_database(scenario)


def test_incremental_rank_refresh_does_not_access_embedding_heap(registry_database):
    async def scenario(db):
        await seed(db, 1)
        await db.execute("INSERT INTO embedding_coordinates VALUES(1,'clip',1,2)")
        await db.execute("SELECT recompute_viz_rank(1,'clip',TRUE)")
        async with db.pool.acquire() as blocker, blocker.transaction():
            await blocker.execute('LOCK TABLE embeddings IN ACCESS EXCLUSIVE MODE')
            async with db.pool.acquire() as conn:
                await conn.execute("SET lock_timeout='100ms'")
                assert await conn.fetchval("SELECT recompute_viz_rank(1,'clip',FALSE)") == 0

    registry_database(scenario)


def test_pending_queries_and_incremental_rank_do_not_scan_the_dataset(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db, 20000)
        await upload(range(1, 20001))
        await db.execute("SELECT recompute_viz_rank(1,'clip',TRUE)")
        await db.execute('ANALYZE embeddings; ANALYZE samples')
        await db.execute('VACUUM ANALYZE embedding_projection_pending')
        await db.execute("UPDATE embeddings SET vector='[2,3,4]' WHERE sample_id=20000")
        queries = []
        fetch, fetchval = db.fetch, db.fetchval

        async def record_fetch(sql, *args):
            queries.append((sql, args))
            return await fetch(sql, *args)

        async def record_fetchval(sql, *args):
            queries.append((sql, args))
            return await fetchval(sql, *args)

        monkeypatch.setattr(db, 'fetch', record_fetch)
        monkeypatch.setattr(db, 'fetchval', record_fetchval)
        response = await embeddings.get_vectors(
            1,
            'clip',
            limit=10,
            offset=0,
            after_id=None,
            pending_only=True,
            response_format='json',
        )
        assert json.loads(bytes(response.body))['total'] == 1
        for sql, args in queries:
            raw = await fetchval('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql, *args)
            plan = json.loads(raw)[0]['Plan']
            assert plan['Shared Hit Blocks'] + plan['Shared Read Blocks'] < 100, plan
        async with db.pool.acquire() as blocker, blocker.transaction():
            await blocker.execute(
                'LOCK TABLE embeddings,embedding_coordinates,embedding_viz_rank IN ACCESS EXCLUSIVE MODE',
            )
            # A compatibility refresh must not even read the large tables.
            async with db.pool.acquire() as conn:
                await conn.execute("SET lock_timeout='100ms'")
                assert await conn.fetchval("SELECT recompute_viz_rank(1,'clip',FALSE)") == 0

    registry_database(scenario)


def test_fields_lists_embedded_but_unprojected_fields(registry_database):
    async def scenario(db):
        await seed(db, n=3, field='unprojected')
        assert 'unprojected' in await embeddings.get_embedding_fields(1)

    registry_database(scenario)
