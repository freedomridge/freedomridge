"""Filtered-query regression cases, on disposable PostgreSQL."""

import asyncio

import pytest

from backend.routers import datasets
from backend.tests.test_field_registry_postgres import bulk_setup, registry_database  # noqa: F401
from backend.tests.test_filtered_query_plans import FILTER, record_queries, seed


@pytest.mark.parametrize('skip_count,offset', [(False, 0), (True, 0), (True, 200)])
def test_few_early_matches_do_not_veto_filter_first(
    registry_database,  # noqa: F811
    monkeypatch,
    skip_count,
    offset,
):
    async def scenario(database):
        await seed(database, count=150_000)
        # June OR WHEAT_EARLY4: most matches are late, with a few early tagged rows. Set up like the seed: the update
        # planned per row took 8.5 s of the fixture's 10 s statement timeout on an idle machine.
        async with bulk_setup(database) as connection:
            await connection.execute("""
                UPDATE samples SET metadata = metadata || jsonb_build_object(
                    'datetime', CASE WHEN id > 145000 THEN '2026-06-15' ELSE '2025-01-01' END),
                    tags = CASE WHEN id <= 8 THEN ARRAY['WHEAT_EARLY4'] ELSE ARRAY[]::text[] END;
                CREATE INDEX test_datetime ON samples ((metadata->>'datetime')) WHERE dataset_id = 1;
                ANALYZE samples;
            """)
        filters = {
            'logic': 'OR',
            'filters': [
                {
                    'logic': 'AND',
                    'filters': [
                        {'field': 'datetime', 'operator': '>=', 'value': '2026-06-01'},
                        {'field': 'datetime', 'operator': '<', 'value': '2026-07-01'},
                    ],
                },
                {'field': 'tags', 'operator': 'in', 'value': ['WHEAT_EARLY4']},
            ],
        }
        queries = record_queries(database, monkeypatch)
        result = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters=filters, limit=200, offset=offset, skip_count=skip_count),
        )
        assert result['total'] == (-1 if skip_count else 5008)
        expected = (list(range(1, 9)) + list(range(145001, 150001)))[offset : offset + 200]
        assert [r['id'] for r in result['items']] == expected
        pages = [sql for method, sql, _ in queries if method == 'fetch' and 'ORDER BY' in sql]
        assert any('id + 0' in sql for sql in pages), pages

    registry_database(scenario)


def test_lightning_off_adds_no_selectivity_probe(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database)
        queries = record_queries(database, monkeypatch)
        await datasets._get_dataset_field_stats_impl(1, FILTER, lightning_mode=False)
        assert not any('LIMIT 100001' in sql for _, sql, _ in queries)

    registry_database(scenario)


@pytest.mark.parametrize('lightning', [True, False])
def test_sidebar_pool_wait_keeps_original_extrema_query(registry_database, monkeypatch, lightning):  # noqa: F811
    async def scenario(database):
        await seed(database)
        original = database.fetchrow
        extrema = []
        released = asyncio.Event()
        release_task = None

        async def release(connections):
            try:
                await asyncio.sleep(0.1)  # Longer than the removed 50 ms probe budget.
            finally:
                for conn in connections:
                    await database.pool.release(conn)
                released.set()

        async def fetchrow(sql, *args):
            nonlocal release_task
            if "MIN((metadata->>'score')" in sql:
                extrema.append(sql)
                if len(extrema) == 1:
                    connections = [await database.pool.acquire() for _ in range(database.pool.get_max_size())]
                    release_task = asyncio.create_task(release(connections))
            return await original(sql, *args)

        monkeypatch.setattr(database, 'fetchrow', fetchrow)
        try:
            # Lightning mode computes the facet when the field's section is opened.
            fields = await datasets._get_dataset_field_stats_impl(
                1,
                FILTER,
                lightning_mode=lightning,
                fields=['score'] if lightning else None,
            )
        finally:
            if release_task is not None:
                await release_task
        assert released.is_set()
        score = next(f for f in fields if f['name'] == 'score')
        assert (score['min'], score['max']) == (100001, 100010)
        assert len(extrema) == 1
        assert '::float + 0' not in extrema[0]
        assert 'dataset_id = $1 AND dataset_id' not in extrema[0]

    registry_database(scenario)


@pytest.mark.parametrize('bitmap', [False, True])
def test_dataset_only_index_is_not_a_filter_index(registry_database, monkeypatch, bitmap):  # noqa: F811
    async def scenario(database):
        await seed(database, count=150_000)
        await database.execute('DROP INDEX test_batch')
        # Force the index path that a smaller secondary dataset naturally uses.
        # Every acquisition executes these settings on its actual connection.
        original = database.fetchval
        plans = []

        async def fetchval(sql, *args):
            if sql.startswith('EXPLAIN'):
                async with database.pool.acquire() as conn, conn.transaction():
                    await conn.execute('SET LOCAL enable_seqscan = off')
                    await conn.execute('SET LOCAL enable_indexscan = ' + ('off' if bitmap else 'on'))
                    await conn.execute('SET LOCAL enable_bitmapscan = ' + ('on' if bitmap else 'off'))
                    plan = await conn.fetchval(sql, *args)
                    plans.append(plan)
                    return plan
            return await original(sql, *args)

        monkeypatch.setattr(database, 'fetchval', fetchval)
        queries = record_queries(database, monkeypatch)
        result = await datasets.list_samples_post(1, datasets.SamplesRequest(filters=FILTER, skip_count=True, limit=3))
        assert [r['id'] for r in result['items']] == [149991, 149992, 149993]
        assert plans and 'Index' in plans[0]
        assert not any('id + 0' in sql for _, sql, _ in queries)

    registry_database(scenario)


@pytest.mark.parametrize('slim', [False, True])
def test_broad_page_keeps_custom_plan_after_deep_pages(registry_database, monkeypatch, slim):  # noqa: F811
    async def scenario(database):
        await seed(database, count=150_000)
        async with bulk_setup(database) as connection:  # as above
            await connection.execute("""
                UPDATE samples SET metadata = metadata || '{"datetime":"2026-06-15"}';
                CREATE INDEX test_datetime ON samples ((metadata->>'datetime')) WHERE dataset_id = 1;
                ANALYZE samples;
            """)
        queries = record_queries(database, monkeypatch)
        filters = {
            'logic': 'AND',
            'filters': [
                {'field': 'datetime', 'operator': '>=', 'value': '2020-01-01'},
                {'field': 'datetime', 'operator': '<', 'value': '2027-01-01'},
            ],
        }
        result = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters=filters, skip_count=True, limit=200, slim=slim, fields='id'),
        )
        _, sql, args = next(q for q in queries if q[0] == 'fetch' and 'ORDER BY' in q[1])
        async with database.pool.acquire() as conn:
            # The app's connections plan every statement anyway (database.PLANNING); this checks the query on its own.
            await conn.execute('SET plan_cache_mode = auto')
            stmt = await conn.prepare(sql)
            for _ in range(5):
                await stmt.fetch(*args[:-1], 50000)
            rows = await stmt.fetch(*args)
            assert len(rows) == 200
            assert [r['id'] for r in rows] == [r['id'] for r in result['items']]
            counters = await conn.fetchrow(
                'SELECT generic_plans, custom_plans FROM pg_prepared_statements WHERE name = $1',
                stmt.get_name(),
            )
            assert counters['generic_plans'] == 0, dict(counters)
            await conn.execute('RESET plan_cache_mode')

    registry_database(scenario)
