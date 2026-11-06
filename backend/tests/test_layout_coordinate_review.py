"""Regressions for deploying 058 and for concurrent projection."""

import asyncio
import json
import os
from unittest.mock import AsyncMock

import asyncpg
import pytest

from backend.routers import embeddings
from backend.tests.test_layout_coordinates import registry_database, seed, upload

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
