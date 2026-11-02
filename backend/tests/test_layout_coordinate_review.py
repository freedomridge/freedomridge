"""Regressions for deploying 058 and for concurrent projection."""

import asyncio
import importlib.util
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import numpy as np
import pytest
from fridge.core.dataset import Dataset

from backend.routers import embeddings
from backend.tests.test_layout_coordinates import ROOT, registry_database, seed, upload

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


@pytest.mark.parametrize(
    'statement',
    [
        'UPDATE embeddings SET x=1,y=2',
        'UPDATE embeddings SET x=NULL',
        "INSERT INTO embeddings(sample_id,field_name,x,y) VALUES(1,'legacy',1,2)",
        "INSERT INTO embeddings(sample_id,field_name,x) VALUES(1,'legacy',1)",
    ],
)
def test_legacy_coordinate_writes_fail_loudly(registry_database, statement):
    async def scenario(db):
        await seed(db, 1)
        with pytest.raises(asyncpg.RaiseError, match='coordinates live in embedding_coordinates'):
            await db.execute(statement)

    registry_database(scenario)


def test_sdk_project_pending_passes_vector_version():
    written = []
    dataset = object.__new__(Dataset)
    dataset._id = 1
    pages = iter([{'items': [{'sample_id': 1, 'vector': [1, 2, 3], 'vector_version': '123'}]}, {'items': []}])
    dataset.client = SimpleNamespace(
        get_map_state=lambda dataset_id, field: {'managed': False, 'active': False, 'model_id': None},
        get_vectors=lambda *args, **kwargs: next(pages),
        upload_coordinates=lambda ds, field, payload: written.extend(payload),
        recompute_viz_rank=lambda *args, **kwargs: None,
    )
    model = SimpleNamespace(transform=lambda vectors: np.zeros((len(vectors), 2)))
    dataset.project_pending('clip', model)
    assert written == [{'sample_id': 1, 'x': 0.0, 'y': 0.0, 'vector_version': '123'}]


def test_direct_projector_rejects_reembedding_during_transform(registry_database, monkeypatch):
    spec = importlib.util.spec_from_file_location('review_projection', ROOT / 'scripts/umap_project_job.py')
    assert spec and spec.loader
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    async def scenario(db):
        await seed(db, 1)

        async def reembed(*args):
            await db.execute("UPDATE embeddings SET vector='[7,8,9]' WHERE sample_id=1")

        monkeypatch.setattr(worker, 'require_free_space', reembed)
        model = SimpleNamespace(transform=lambda vectors: np.zeros((len(vectors), 2)))
        async with db.pool.acquire() as conn:
            await worker._project(conn, 1, 'clip', model, 10)
        assert await db.fetchval('SELECT count(*) FROM embedding_coordinates') == 0
        assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending') == 1

    registry_database(scenario)


def test_incremental_rank_heals_missing_metadata_once(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db, 3)
        await upload([1, 2, 3])
        assert await db.fetchval("SELECT recompute_viz_rank(1,'clip',FALSE)") == 3
        assert await db.fetchval('SELECT total_points FROM embedding_viz_meta') == 3
        async with db.pool.acquire() as blocker, blocker.transaction():
            await blocker.execute('LOCK TABLE embedding_coordinates,embedding_viz_rank IN ACCESS EXCLUSIVE MODE')
            async with db.pool.acquire() as conn:
                await conn.execute("SET lock_timeout='100ms'")
                assert await conn.fetchval("SELECT recompute_viz_rank(1,'clip',FALSE)") == 0

    registry_database(scenario)


def test_empty_embedding_statements_do_not_touch_projection_tables(registry_database):
    async def scenario(db):
        async with db.pool.acquire() as blocker, blocker.transaction():
            await blocker.execute(
                'LOCK TABLE embedding_coordinates,embedding_projection_pending IN ACCESS EXCLUSIVE MODE',
            )
            async with db.pool.acquire() as conn:
                await conn.execute("SET lock_timeout='100ms'")
                await conn.execute('UPDATE embeddings SET vector=vector WHERE FALSE')
                await conn.execute('INSERT INTO embeddings(sample_id,field_name) SELECT 1,\'clip\' WHERE FALSE')

    registry_database(scenario)


@pytest.mark.parametrize(
    'statement',
    [
        'DELETE FROM embedding_coordinates WHERE FALSE',
        'UPDATE embedding_coordinates SET x=x WHERE FALSE',
        "INSERT INTO embedding_coordinates SELECT 1,'clip',1,2 WHERE FALSE",
        'DELETE FROM embedding_viz_rank WHERE FALSE',
    ],
)
def test_empty_coordinate_statements_do_not_touch_metadata_or_queue(registry_database, statement):
    async def scenario(db):
        async with db.pool.acquire() as blocker, blocker.transaction():
            await blocker.execute(
                'LOCK TABLE embedding_viz_meta,embedding_projection_pending IN ACCESS EXCLUSIVE MODE',
            )
            async with db.pool.acquire() as conn:
                await conn.execute("SET lock_timeout='100ms'")
                await conn.execute(statement)

    registry_database(scenario)


@pytest.mark.parametrize('response_format', ['json', 'binary'])
@pytest.mark.parametrize('during_write', [False, True])
def test_projection_rejects_changed_vector_and_keeps_work(
    registry_database,
    monkeypatch,
    response_format,
    during_write,
):
    from fridge.core.client import _decode_vectors_binary

    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db, 1)
        response = await embeddings.get_vectors(1, 'clip', 10, 0, None, True, response_format)
        page = (
            _decode_vectors_binary(bytes(response.body))
            if response_format == 'binary'
            else json.loads(bytes(response.body))
        )
        item = page['items'][0]
        assert 'vector_version' in item
        coordinate = embeddings.CoordinateUpdate(sample_id=1, x=5, y=6, vector_version=item['vector_version'])
        async with db.pool.acquire() as updater:
            async with updater.transaction():
                await updater.execute("UPDATE embeddings SET vector='[4,5,6]' WHERE sample_id=1")
                if during_write:
                    task = asyncio.create_task(embeddings.upload_coordinates(1, 'clip', [coordinate], {}))
                    # Wait until the coordinate writer is blocked on the vector row lock.
                    for _ in range(100):
                        await updater.execute('SELECT pg_stat_clear_snapshot()')
                        if await updater.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                            "WHERE wait_event_type='Lock' AND query LIKE '%expected_version%')",
                        ):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        pytest.fail('projection writer did not wait for the vector transaction')
            if during_write:
                await task
            else:
                await embeddings.upload_coordinates(1, 'clip', [coordinate], {})
        assert await db.fetchval('SELECT count(*) FROM embedding_coordinates') == 0
        assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending') == 1
        # The next pass succeeds with the new version, without rewriting the vector.
        version = await db.fetchval('SELECT xmin::text FROM embeddings')
        coordinate.vector_version = version
        await embeddings.upload_coordinates(1, 'clip', [coordinate], {})
        assert await db.fetchval('SELECT count(*) FROM embedding_coordinates') == 1
        assert await db.fetchval('SELECT count(*) FROM embedding_projection_pending') == 0
        assert await db.fetchval('SELECT xmin::text FROM embeddings') == version

    registry_database(scenario)
