"""Exact rejected-set scatter and rank membership accounting on disposable PostgreSQL."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.routers import embeddings
from backend.tests.test_layout_coordinates import registry_database, seed

__all__ = ['registry_database']

CONCEPT = {'field': '_concept', 'operator': 'concept_exclude', 'value': {'concept_id': 1, 'threshold': 0.5}}
DUPS = {'field': '_duplicates', 'operator': 'collapse_duplicates', 'value': '_any'}
TAG = {'field': 'tags', 'operator': 'not in', 'value': ['bad']}
GRAIN = {'field': 'media_type', 'operator': '!=', 'value': 'object'}
BIG = {'field': '_concept', 'operator': 'concept_exclude', 'value': {'concept_id': 3, 'threshold': 0.5}}


def conjunction(*nodes):
    return {'logic': 'AND', 'filters': list(nodes)}


async def populate(db, n=20):
    await seed(db, n)
    await db.execute(
        "INSERT INTO embedding_coordinates SELECT i,'clip',i,i/3.0 FROM generate_series(1,$1::int) i",
        n - 1,
    )
    await db.execute("SELECT recompute_viz_rank(1,'clip')")
    await db.execute("UPDATE samples SET tags=ARRAY['bad'] WHERE id IN (2,3)")
    await db.execute('ALTER TABLE samples ALTER COLUMN media_type DROP NOT NULL')
    await db.execute('UPDATE samples SET tags=NULL,media_type=NULL WHERE id=4')
    await db.execute("UPDATE samples SET media_type='object' WHERE id=5")
    await db.execute("INSERT INTO samples(id,dataset_id,filepath) VALUES(200001,2,'other')")
    await db.execute(
        "INSERT INTO embeddings(dataset_id,sample_id,field_name) VALUES("
        "(SELECT dataset_id FROM samples WHERE id = 200001),200001,'clip')",
    )
    await db.execute("INSERT INTO embedding_coordinates VALUES(200001,'clip',4,5)")
    await db.execute("SELECT recompute_viz_rank(2,'clip')")
    await db.execute(
        "INSERT INTO embedding_concepts(id,dataset_id,name,embedding_field,kind,weight) "
        "VALUES(1,1,'sky','clip','text','[1,2,3]'),(2,2,'unrelated','clip','text','[1,2,3]')",
    )
    await db.execute(
        'INSERT INTO embedding_concept_scores VALUES(1,1,0.9),(1,2,0.8),(1,3,0.1),(1,$1,0.9),(1,200001,0.9),(2,6,0.9)',
        n,
    )
    await db.execute(
        "INSERT INTO sample_dup_secondary VALUES(1,'_any',2),(1,'_any',7),(1,'named',8),(2,'_any',200001)",
    )


async def exact_count(db):
    assert (
        await db.fetchval(
            'SELECT count(*) FROM embedding_viz_meta m FULL JOIN '
            '(SELECT dataset_id,field_name,count(*) n FROM embedding_viz_rank GROUP BY 1,2) r '
            'USING(dataset_id,field_name) WHERE coalesce(m.total_points,0) != coalesce(r.n,0)',
        )
        == 0
    )


def test_rank_insert_boundary_counts_conflicts_and_first_population(registry_database):
    async def scenario(db):
        await seed(db, 8)
        await db.execute(
            "INSERT INTO embedding_viz_rank SELECT 1,'clip',i,0,i,i FROM generate_series(1,8) i",
        )
        await exact_count(db)
        await db.execute('INSERT INTO embedding_viz_rank SELECT * FROM embedding_viz_rank ON CONFLICT DO NOTHING')
        await exact_count(db)
        await db.execute("DELETE FROM embedding_viz_rank WHERE sample_id IN (1,2)")
        await exact_count(db)
        await db.execute('DELETE FROM samples WHERE id=3')
        await exact_count(db)
        await db.execute('DELETE FROM datasets WHERE id=1')
        await exact_count(db)

    registry_database(scenario)


def test_rank_coordinate_lifecycle_and_rebuild(registry_database):
    async def scenario(db):
        await populate(db)
        await exact_count(db)
        for statement in [
            "UPDATE embedding_coordinates SET x=x+10 WHERE sample_id=1",
            "DELETE FROM embedding_coordinates WHERE sample_id=1",
            "INSERT INTO embedding_coordinates VALUES(1,'clip',1,2)",
            "SELECT recompute_viz_rank(1,'clip',FALSE)",
            "SELECT recompute_viz_rank(1,'clip',TRUE)",
            "DELETE FROM embeddings WHERE sample_id=2",
            "DELETE FROM samples WHERE id=3",
            "DELETE FROM embedding_coordinates WHERE field_name='clip'",
            "SELECT recompute_viz_rank(1,'clip',TRUE)",
        ]:
            await db.execute(statement)
            await exact_count(db)

    registry_database(scenario)


def test_full_rebuild_resets_a_drifted_counter(registry_database):
    async def scenario(db):
        await populate(db)
        # Drift from any future path that bypasses the rank triggers.
        await db.execute('UPDATE embedding_viz_meta SET total_points = total_points + 7 WHERE dataset_id = 1')
        assert await db.fetchval("SELECT recompute_viz_rank(1,'clip',TRUE)") == 19
        await exact_count(db)

    registry_database(scenario)


def test_rank_concurrent_transactions_and_rollback(registry_database):
    async def scenario(db):
        await seed(db, 20)
        await db.execute("INSERT INTO embedding_coordinates VALUES(1,'clip',1,1)")
        await db.execute("SELECT recompute_viz_rank(1,'clip')")

        async def append(i):
            async with db.pool.acquire() as conn, conn.transaction():
                await conn.execute("INSERT INTO embedding_coordinates VALUES($1::int,'clip',$1::int,$1::int)", i)

        await asyncio.gather(*(append(i) for i in range(2, 12)))
        await exact_count(db)
        await asyncio.gather(db.execute("SELECT recompute_viz_rank(1,'clip')"), append(12), append(13))
        await exact_count(db)
        assert await db.fetchval('SELECT count(*) FROM embedding_viz_rank') == 13
        async with db.pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()
            await conn.execute("INSERT INTO embedding_coordinates VALUES(14,'clip',14,14)")
            await transaction.rollback()
        await exact_count(db)

    registry_database(scenario)


@pytest.mark.parametrize(
    'filters',
    [
        CONCEPT,
        DUPS,
        {**DUPS, 'value': 'named'},
        TAG,
        {'logic': 'NOT', 'filters': [{'field': 'tags', 'operator': 'in', 'value': ['bad']}]},
        {'logic': 'NOT', 'filters': [{'field': 'tags', 'operator': 'contains', 'value': 'bad'}]},
        conjunction(GRAIN, CONCEPT, DUPS, TAG),
        conjunction(GRAIN, {**CONCEPT, 'value': {'concept_id': 2, 'op': '<=', 'threshold': 1}}),
    ],
)
@pytest.mark.parametrize('pct', [0, 30, 100])
def test_broad_scatter_matches_all_ids_coordinates_and_headers(registry_database, monkeypatch, filters, pct):
    async def scenario(db):
        await populate(db, 40)  # 39 points: every probed source stays under the admission share
        await db.execute('UPDATE embedding_coordinates SET x=x+0.25 WHERE sample_id=1')
        request = embeddings.EmbeddingsRequest(filters=filters, sample_pct=pct)
        original = embeddings._query_broad_exclusions
        calls = []

        async def optimized(*args):
            result = await original(*args)
            calls.append(result is not None)
            return result

        monkeypatch.setattr(embeddings, '_query_broad_exclusions', optimized)
        actual = await embeddings.get_embeddings_post(1, request)
        assert calls == [True], 'Supported broad filters must avoid materializing accepted IDs'
        monkeypatch.setattr(embeddings, '_query_broad_exclusions', AsyncMock(return_value=None))
        expected = await embeddings.get_embeddings_post(1, request)
        assert {tuple(sorted(p.items())) for p in actual.pop('points')} == {
            tuple(sorted(p.items())) for p in expected.pop('points')
        }
        assert actual == expected
        assert actual['rank_stale']

    registry_database(scenario)


@pytest.mark.parametrize(
    'filters, scores, fast',
    [
        # 40 ranked points: at a 30% share the probed-rejection cap is 12.
        (conjunction(GRAIN, BIG), 12, True),
        (conjunction(GRAIN, BIG), 13, False),
        # Sources add up (the two '_any' duplicates in dataset 1 count too).
        (conjunction(GRAIN, BIG, DUPS), 10, True),
        (conjunction(GRAIN, BIG, DUPS), 11, False),
    ],
)
def test_large_probed_rejections_go_to_the_general_path(registry_database, monkeypatch, filters, scores, fast):
    """Each concept/duplicate rejection costs a rank-PK probe, so past a share of the field the
    general path is faster. Admission counts the sources first and probes nothing when over."""

    async def scenario(db):
        await populate(db, 41)
        await db.execute(
            "INSERT INTO embedding_concepts(id,dataset_id,name,embedding_field,kind,weight) "
            "VALUES(3,1,'big','clip','text','[1,2,3]')",
        )
        await db.execute(
            'INSERT INTO embedding_concept_scores SELECT 3,i,0.9 FROM generate_series(1,$1::int) i',
            scores,
        )
        # populate's '_any' duplicate in dataset 2 must not count against dataset 1.
        assert await db.fetchval("SELECT count(*) FROM sample_dup_secondary WHERE dataset_id=2 AND hash_config='_any'")
        request = embeddings.EmbeddingsRequest(filters=filters, sample_pct=30)
        assert (await embeddings._query_broad_exclusions(1, request) is not None) == fast
        actual = await embeddings.get_embeddings_post(1, request)
        monkeypatch.setattr(embeddings, '_query_broad_exclusions', AsyncMock(return_value=None))
        expected = await embeddings.get_embeddings_post(1, request)
        assert sorted(actual.pop('points'), key=lambda p: p['sample_id']) == sorted(
            expected.pop('points'),
            key=lambda p: p['sample_id'],
        )
        assert actual == expected

    registry_database(scenario)


def test_exclusion_uses_an_anti_join_when_ids_exceed_work_mem(registry_database, monkeypatch):
    """NOT IN over the excluded ids degrades to a rescan per rank row when its hash table would
    not fit in work_mem (>30 s on large datasets at 4 MB). The anti-join spills to disk instead."""

    class Capture:
        async def fetch(self, sql, *args):
            self.sql, self.args = sql, args
            return []

    async def scenario(db):
        await populate(db, 20000)
        await db.execute("UPDATE samples SET tags=ARRAY['bad'] WHERE id % 4 = 0")
        await db.execute('ANALYZE')
        request = embeddings.EmbeddingsRequest(filters=conjunction(GRAIN, TAG), sample_pct=30)
        capture = Capture()
        monkeypatch.setattr(embeddings, 'db', capture)
        await embeddings._query_broad_exclusions(1, request)
        monkeypatch.setattr(embeddings, 'db', db)
        async with db.pool.acquire() as conn:
            plan = '\n'.join(r[0] for r in await conn.fetch('EXPLAIN ' + capture.sql, *capture.args))
            expected = await conn.fetch(capture.sql, *capture.args)
            async with conn.transaction():
                await conn.execute("SET LOCAL work_mem = '64kB'")
                small = '\n'.join(r[0] for r in await conn.fetch('EXPLAIN ' + capture.sql, *capture.args))
                actual = await conn.fetch(capture.sql, *capture.args)
        for text in (plan, small):
            assert 'Anti Join' in text and 'SubPlan' not in text, text
        assert sorted(map(repr, actual)) == sorted(map(repr, expected))
        assert len(expected) > 10000

    registry_database(scenario)


@pytest.mark.parametrize(
    'case',
    ['selective', 'zero', 'include', 'missing_rank', 'patch', 'or', 'metadata', 'crossgrain', 'polygon'],
)
def test_scatter_fallback_equivalence(registry_database, monkeypatch, case):
    async def scenario(db):
        await populate(db)
        request = embeddings.EmbeddingsRequest(filters=TAG)
        if case in ('selective', 'zero'):
            await db.execute("UPDATE samples SET tags=ARRAY['bad'] WHERE id > $1", 0 if case == 'zero' else 2)
        elif case == 'include':
            request.include_fields = ['tags', 'filepath']
        elif case == 'missing_rank':
            await db.execute('DELETE FROM embedding_viz_rank; DELETE FROM embedding_viz_meta')
        elif case == 'patch':
            await db.execute("DELETE FROM embeddings WHERE sample_id=20")
            await db.execute(
                "INSERT INTO embeddings(dataset_id,sample_id,field_name) VALUES("
                "(SELECT dataset_id FROM samples WHERE id = 20),20,'clip_patch_2x2_r0c0')",
            )
            await db.execute("INSERT INTO embedding_coordinates VALUES(20,'clip_patch_2x2_r0c0',1,2)")
            request.field = 'clip_patch_2x2'
        elif case == 'or':
            request.filters = {'logic': 'OR', 'filters': [CONCEPT, TAG]}
        elif case == 'metadata':
            request.filters = conjunction(TAG, {'field': 'unknown', 'operator': 'is_null'})
        elif case == 'polygon':
            request.filters = {
                'field': 'clip',
                'operator': 'embedding_polygon',
                'value': [[0, 0], [10, 0], [10, 10], [0, 10]],
            }
        elif case == 'crossgrain':
            request.filters = {'field': '_cg', 'operator': 'cg_self', 'value': TAG}
        assert await embeddings._query_broad_exclusions(1, request) is None
        actual = await embeddings.get_embeddings_post(1, request)
        monkeypatch.setattr(embeddings, '_query_broad_exclusions', AsyncMock(return_value=None))
        expected = await embeddings.get_embeddings_post(1, request)
        assert sorted(actual.pop('points'), key=lambda p: p['sample_id']) == sorted(
            expected.pop('points'),
            key=lambda p: p['sample_id'],
        )
        assert actual == expected

    registry_database(scenario)


@pytest.mark.parametrize('pct', [0, 30, 100])
def test_broad_sampled_population_preserves_rank_quota_and_binary(registry_database, monkeypatch, pct):
    async def scenario(db):
        await populate(db, 100010)
        # Stable ranks, with deliberately uneven cell populations: the existing
        # filtered sampler underfills its budget, which this optimization preserves.
        await db.execute('UPDATE embedding_viz_rank SET rank=sample_id%1000 WHERE dataset_id=1')
        await db.execute('UPDATE embedding_viz_meta SET occupied_cells=200 WHERE dataset_id=1')
        request = embeddings.EmbeddingsRequest(filters=conjunction(GRAIN, CONCEPT, DUPS, TAG), sample_pct=pct)
        original = embeddings._query_broad_exclusions
        actual = await embeddings.get_embeddings_post(1, request, format='bin')
        monkeypatch.setattr(embeddings, '_query_broad_exclusions', AsyncMock(return_value=None))
        expected = await embeddings.get_embeddings_post(1, request, format='bin')
        from backend.tests.test_embeddings_binary import _parse

        a, ai, ax, ay = _parse(bytes(actual.body))
        b, bi, bx, by = _parse(bytes(expected.body))
        assert sorted(zip(ai, ax, ay)) == sorted(zip(bi, bx, by))
        assert a == b
        if pct == 30:
            await db.execute('UPDATE embedding_viz_meta SET occupied_cells=1 WHERE dataset_id=1')
            assert await original(1, request) is None  # Saturated quota retains the old unordered LIMIT.

    registry_database(scenario)


def test_broad_scatter_authorization_precedes_queries(registry_database, monkeypatch):
    import httpx
    from fastapi import FastAPI

    from backend import access
    from backend.routers import auth

    async def scenario(db):
        monkeypatch.setattr(access, 'db', db)
        await db.execute('UPDATE datasets SET is_global=FALSE WHERE id=1')
        app = FastAPI()
        app.include_router(embeddings.router)
        query = AsyncMock()
        monkeypatch.setattr(embeddings, '_query_broad_exclusions', query)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for user, status in [(None, 401), ({'id': 9, 'role': 'viewer'}, 403)]:
                monkeypatch.setattr(auth, 'get_optional_user', AsyncMock(return_value=user))
                response = await client.post('/datasets/1/embeddings/query', json={'filters': TAG})
                assert response.status_code == status
        query.assert_not_awaited()

    registry_database(scenario)


def test_empty_rank_population_and_null_tag_elements(registry_database, monkeypatch):
    async def scenario(db):
        await populate(db)
        await db.execute('UPDATE samples SET tags=ARRAY[NULL]::text[] WHERE id=4')
        request = embeddings.EmbeddingsRequest(filters=TAG)
        actual = await embeddings.get_embeddings_post(1, request)
        original = embeddings._query_broad_exclusions
        monkeypatch.setattr(embeddings, '_query_broad_exclusions', AsyncMock(return_value=None))
        expected = await embeddings.get_embeddings_post(1, request)
        assert sorted(actual['points'], key=lambda p: p['sample_id']) == sorted(
            expected['points'],
            key=lambda p: p['sample_id'],
        )
        monkeypatch.setattr(embeddings, '_query_broad_exclusions', original)
        await db.execute('DELETE FROM embedding_coordinates WHERE sample_id < 200001')
        rows, total = await original(1, request)
        assert rows == [] and total == 0
        await exact_count(db)

    registry_database(scenario)


def test_full_rebuild_fences_a_concurrent_coordinate_insert(registry_database):
    async def scenario(db):
        await seed(db, 2)
        await db.execute("INSERT INTO embedding_coordinates VALUES(1,'clip',1,1)")
        await db.execute("SELECT recompute_viz_rank(1,'clip')")
        await db.execute(
            """CREATE FUNCTION pause_rank_delete() RETURNS trigger AS $$
            BEGIN PERFORM pg_advisory_xact_lock(648063); RETURN NULL; END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER pause_rank_delete BEFORE DELETE ON embedding_viz_rank
            FOR EACH STATEMENT EXECUTE FUNCTION pause_rank_delete()""",
        )
        async with db.pool.acquire() as blocker, db.pool.acquire() as rebuild:
            await blocker.execute('SELECT pg_advisory_lock(648063)')
            pid = await rebuild.fetchval('SELECT pg_backend_pid()')
            task = asyncio.create_task(rebuild.execute("SELECT recompute_viz_rank(1,'clip')"))
            try:
                async with asyncio.timeout(5):
                    while not await db.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND locktype='advisory' AND NOT granted)",
                        pid,
                    ):
                        await asyncio.sleep(0.01)
                writer = asyncio.create_task(db.execute("INSERT INTO embedding_coordinates VALUES(2,'clip',2,2)"))
                # On 058 the insert commits between the extent count and rank rebuild;
                # on 063 its trigger waits for the rebuild's per-dataset fence.
                await asyncio.wait({writer}, timeout=0.1)
            finally:
                await blocker.execute('SELECT pg_advisory_unlock(648063)')
            await asyncio.gather(task, writer)
        await exact_count(db)
        assert await db.fetchval('SELECT total_points FROM embedding_viz_meta') == 2

    registry_database(scenario)


def test_broad_scatter_snapshot_during_ingest_and_cancellation(registry_database):
    async def scenario(db):
        await populate(db)
        await seed(db, 20, field='other')
        request = embeddings.EmbeddingsRequest(filters=TAG, sample_pct=100)

        async def mutate():
            for _ in range(5):
                await db.execute("INSERT INTO embedding_coordinates VALUES(20,'clip',20,20)")
                await db.execute("DELETE FROM embedding_coordinates WHERE sample_id=20 AND field_name='clip'")

        async def read():
            for _ in range(10):
                rows, total = await embeddings._query_broad_exclusions(1, request)
                assert len(rows) == total
                assert len({row['sample_id'] for row in rows}) == total

        await asyncio.gather(mutate(), read())
        await exact_count(db)
        async with db.pool.acquire() as blocker, blocker.transaction():
            await blocker.execute('LOCK TABLE embedding_viz_rank IN ACCESS EXCLUSIVE MODE')
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(embeddings._query_broad_exclusions(1, request), timeout=0.1)
        # Cancellation must release its connection and drain server-side work.
        async with asyncio.timeout(5):
            rows, total = await embeddings._query_broad_exclusions(1, request)
        assert len(rows) == total
        assert (
            await db.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                "AND pid<>pg_backend_pid() AND state='active' AND query LIKE 'WITH rejected AS MATERIALIZED%'",
            )
            == 0
        )

    registry_database(scenario)


def test_duplicate_rejections_are_scoped_to_the_dataset():
    # Config names like _any are shared across datasets; probing another dataset's secondaries made
    # the fast path 4.6x slower than the admission count (which is scoped) allowed for.
    args = [7, 'clip']
    rejected, _, probed = embeddings._scatter_rejections({'logic': 'AND', 'filters': [DUPS]}, args)
    assert 'dataset_id = $1' in rejected
    assert all('dataset_id = $1' in shape for shape in probed)
