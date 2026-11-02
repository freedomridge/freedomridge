"""Filtered-query strategies against disposable PostgreSQL, including generic plans."""

import pytest

from backend.routers import datasets
from backend.tests.test_field_registry_postgres import registry_database  # noqa: F401

FILTER = {'field': 'batch', 'operator': '=', 'value': 'late'}


async def seed(database, count=100_010):
    await database.execute(f"""
        INSERT INTO samples (dataset_id, filepath, media_type, metadata)
        SELECT 1, 'sample-' || n, 'image',
               jsonb_build_object('batch', CASE WHEN n > {count - 10} THEN 'late' ELSE 'early' END,
                                  'score', n)
        FROM generate_series(1, {count}) n;
        INSERT INTO dataset_fields (dataset_id, field_name, field_type, is_promoted)
        VALUES (1, 'batch', 'string', true), (1, 'score', 'number', true);
        INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, sample_count, min_value, max_value)
        VALUES (1, 'score', 'number', {count}, 1, {count});
        CREATE INDEX test_batch ON samples ((metadata->>'batch')) WHERE dataset_id = 1;
        CREATE INDEX test_score ON samples (((metadata->>'score')::float)) WHERE dataset_id = 1;
        ANALYZE samples;
    """)


def record_queries(database, monkeypatch):
    queries = []
    for method in ('fetch', 'fetchrow', 'fetchval'):
        original = getattr(database, method)

        async def record(sql, *args, original=original, method=method):
            queries.append((method, sql, args))
            return await original(sql, *args)

        monkeypatch.setattr(database, method, record)
    return queries


@pytest.mark.parametrize('slim', [False, True])
def test_skip_count_selective_grid_filters_before_sorting(registry_database, monkeypatch, slim):  # noqa: F811
    async def scenario(database):
        await seed(database)
        queries = record_queries(database, monkeypatch)
        result = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters=FILTER, skip_count=True, limit=3, offset=2, slim=slim, fields='id'),
        )
        assert result['total'] == -1
        assert [r['id'] for r in result['items']] == [100003, 100004, 100005]
        page = next(sql for method, sql, _ in queries if method == 'fetch' and 'ORDER BY' in sql)
        assert 'ORDER BY id + 0' in page
        assert any('LIMIT 4096' in sql for _, sql, _ in queries)
        if not slim:
            assert 'MATERIALIZED (SELECT id FROM samples' in page
            assert 'JOIN page USING (id) ORDER BY id' in page
        assert not any('SELECT count(*) FROM samples' in sql for _, sql, _ in queries)

    registry_database(scenario)


def test_selective_grid_keeps_early_matches_on_id_walk(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database)
        await database.execute("""
            UPDATE samples SET metadata = metadata || '{"batch":"head"}' WHERE id <= 10;
            ANALYZE samples;
        """)
        queries = record_queries(database, monkeypatch)
        result = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters={**FILTER, 'value': 'head'}, skip_count=True, limit=3),
        )
        assert [r['id'] for r in result['items']] == [1, 2, 3]
        assert not any('id + 0' in sql for _, sql, _ in queries)

    registry_database(scenario)


def test_unindexed_estimate_does_not_force_a_full_scan_sort(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database)
        await database.execute('DROP INDEX test_batch')
        queries = record_queries(database, monkeypatch)
        result = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters=FILTER, skip_count=True, limit=3),
        )
        assert [r['id'] for r in result['items']] == [100001, 100002, 100003]
        assert not any('id + 0' in sql for _, sql, _ in queries)

    registry_database(scenario)


def test_small_grid_skips_planning_probes(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, count=1000)
        queries = record_queries(database, monkeypatch)
        result = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters=FILTER, skip_count=True, limit=3),
        )
        assert [r['id'] for r in result['items']] == [991, 992, 993]
        assert not any('EXPLAIN' in sql or 'LIMIT 4096' in sql for _, sql, _ in queries)

    registry_database(scenario)


@pytest.mark.parametrize(
    'options,expected',
    [
        ({'after_id': 100005}, [100006, 100007, 100008]),
        ({'sort_by': 'id', 'sort_order': 'desc'}, [100010, 100009, 100008]),
        ({'order_by_ids': [100008, 100001, 100007]}, [100008, 100001, 100007]),
        ({'sort_by': 'score', 'sort_order': 'desc'}, [100010, 100009, 100008]),
    ],
)
def test_grid_preserves_keyset_and_explicit_sorts(registry_database, monkeypatch, options, expected):  # noqa: F811
    async def scenario(database):
        await seed(database)
        queries = record_queries(database, monkeypatch)
        result = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters=FILTER, skip_count=True, limit=3, **options),
        )
        assert [r['id'] for r in result['items']] == expected
        assert not any('EXPLAIN' in sql or 'id + 0' in sql for _, sql, _ in queries)

    registry_database(scenario)


def test_count_uses_unlimited_portal(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database)
        queries = record_queries(database, monkeypatch)
        result = await datasets.list_samples_post(1, datasets.SamplesRequest(filters=FILTER, limit=0))
        assert result['total'] == 10
        assert [method for method, sql, _ in queries if 'count(*)' in sql.lower()] == ['fetch']

    registry_database(scenario)


def test_promoted_filter_index_is_available_to_generic_plan(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database)
        queries = record_queries(database, monkeypatch)
        await datasets.list_samples_post(1, datasets.SamplesRequest(filters=FILTER, limit=0))
        _, sql, args = next(q for q in queries if 'count(*)' in q[1].lower())
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL plan_cache_mode = force_generic_plan")
                await conn.execute("SET LOCAL enable_seqscan = off")
                # SQL PREPARE/EXPLAIN EXECUTE makes this explicitly a generic cached plan.
                await conn.execute('PREPARE filtered_count AS ' + sql)
                try:
                    plan = await conn.fetchval("EXPLAIN (FORMAT JSON) EXECUTE filtered_count(1, 'late')")
                    assert 'test_batch' in plan
                    assert (await conn.fetch(sql, *args))[0][0] == 10
                finally:
                    await conn.execute('DEALLOCATE filtered_count')

    registry_database(scenario)
