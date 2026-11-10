"""Patch searches use one HNSW index per tile position only when every position has one.

python/fridge/test_objects.py checks the answers against a brute-force ranking with no, some and
all positions indexed. This pins which positions count as indexed, and that each position's query
can be served by that position's index (a small fixture never needs it, so the plan is forced).
"""

import asyncio
import os
import random
from typing import Any

import pytest

from backend import index_jobs, patch_search
from backend.patch_search import indexed_grid as _indexed_grid
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
needs_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)

GRID = [f'clip_patch_2x2_r{r}c{c}' for r in range(2) for c in range(2)]


def test_a_complete_grid_is_spelled_out_in_row_major_order():
    assert _indexed_grid('clip', '2x2', set(GRID) | {'clip'}) == GRID


def test_a_missing_position_sends_the_search_to_the_exact_scan():
    assert _indexed_grid('clip', '2x2', set(GRID[:3])) is None


def test_a_grid_larger_than_the_indexed_set_is_never_enumerated():
    # 10^10 positions would never finish; more positions than indexed fields can't all be indexed.
    assert _indexed_grid('clip', '100000x100000', set(GRID)) is None


class _Conn:
    def __init__(self, indexdefs):
        self.indexdefs = indexdefs

    async def fetch(self, query, *args):
        return [{'indexdef': d} for d in self.indexdefs]


def _index(field, dim, ops='vector_cosine_ops'):
    return (
        f'CREATE INDEX hnsw_emb_{field} ON public.embeddings USING hnsw '
        f"(((vector)::vector({dim})) {ops}) WHERE (field_name = '{field}'::text)"
    )


def test_only_cosine_indexes_at_the_searched_dimension_count():
    conn = _Conn(
        [
            _index('clip_patch_2x2_r0c0', 512),
            _index('clip_patch_2x2_r0c1', 768),  # another dimension
            _index('clip_patch_2x2_r1c0', 512, ops='vector_l2_ops'),  # can't serve <=>
            _index('clip_patch_2x2_r1c1', 5120),  # vector(5120) must not pass for 512
        ],
    )
    assert asyncio.run(index_jobs.cosine_hnsw_fields(conn, 512)) == {'clip_patch_2x2_r0c0'}


class _Recording:
    """A connection that records the queries it runs."""

    def __init__(self, connection):
        self.connection = connection
        self.queries: list = []

    def transaction(self):
        return self.connection.transaction()

    async def execute(self, query, *args):
        return await self.connection.execute(query, *args)

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return await self.connection.fetch(query, *args)


@needs_database
def test_each_position_query_can_use_its_index_and_matches_the_exact_scan(registry_database):
    async def scenario(database):
        rng = random.Random(5)
        for i in range(30):
            sample_id = await database.fetchval(
                "INSERT INTO samples(dataset_id,filepath,media_type) VALUES(1,$1,'image') RETURNING id",
                f'img{i}',
            )
            for field in GRID:
                vector = '[' + ','.join(str(round(rng.uniform(-1, 1), 3)) for _ in range(3)) + ']'
                await database.execute(
                    'INSERT INTO embeddings(sample_id,field_name,vector) VALUES($1,$2,$3::text::vector)',
                    sample_id,
                    field,
                    vector,
                )
        for field in GRID:  # the definition ann_index_build uses
            await database.execute(
                f'CREATE INDEX "{index_jobs.ann_index_name(field)}" ON embeddings '
                f"USING hnsw ((vector::vector(3)) vector_cosine_ops) WHERE field_name = '{field}'",
            )
        seed_id, seed = await database.fetchrow(
            "SELECT sample_id, vector FROM embeddings WHERE field_name = $1 ORDER BY sample_id LIMIT 1",
            'clip_patch_2x2_r0c0',
        )
        common: dict[str, Any] = dict(
            dataset_id=1,
            like_pattern='clip_patch_2x2_%',
            seed_vector=seed,
            dims=3,
            limit=10,
            per_image=1,
            exclude=(seed_id, 'clip_patch_2x2_r0c0'),
        )
        async with database.pool.acquire() as connection:
            fields = _indexed_grid('clip', '2x2', await index_jobs.cosine_hnsw_fields(connection, 3))
            assert fields == GRID
            recording = _Recording(connection)
            indexed = await patch_search.nearest_patches(recording, fields=fields, **common)
            exact = await patch_search.nearest_patches(connection, fields=None, **common)
            assert [(r['sample_id'], r['field_name']) for r in indexed] == [
                (r['sample_id'], r['field_name']) for r in exact
            ]
            # With sorting priced out, only an index can order a position's rows by distance.
            query, query_args = recording.queries[0]
            async with connection.transaction():
                await connection.execute('SET LOCAL enable_sort = off')
                await connection.execute('SET LOCAL enable_seqscan = off')
                plan = '\n'.join(r[0] for r in await connection.fetch('EXPLAIN ' + query, *query_args))
        for field in GRID:
            assert f'Index Scan using {index_jobs.ann_index_name(field)} on embeddings' in plan, plan

    registry_database(scenario)
