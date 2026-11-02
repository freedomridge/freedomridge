"""Histogram semantics and extraction work against explicitly isolated PostgreSQL."""

import json
import math
import os

import asyncpg
import pytest

from backend.routers import datasets
from backend.tests import test_field_registry_postgres

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)

MISSING = object()


async def seed(database, values, kind='number', field='value'):
    if kind is not None:
        await database.execute(
            'INSERT INTO dataset_fields (dataset_id,field_name,field_type) VALUES (1,$1,$2)',
            field,
            kind,
        )
    async with database.pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO samples (dataset_id,filepath,media_type,metadata) VALUES (1,$1,'image',$2)",
            [(str(i), {} if v is MISSING else {field: v}) for i, v in enumerate(values)],
        )


async def histogram(field='value', bins=None, filters=None, top=50, as_number=False):
    return await datasets.get_histogram(1, field, bins, json.dumps(filters) if filters else None, top, as_number)


def categories(result):
    return {bucket['key']: bucket['count'] for bucket in result['buckets']}


def record_reads(monkeypatch):
    calls = []
    for method in ('fetch', 'fetchrow'):
        original = getattr(asyncpg.Connection, method)

        async def record(conn, query, *args, original=original, method=method, **kwargs):
            if 'FROM samples' in query:
                calls.append((method, query, args))
            return await original(conn, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, method, record)
    return calls


@pytest.mark.parametrize(
    'kind,values',
    [
        ('number', [0, 5, 10, '7', 'N/A', None, MISSING, True, {'x': 1}, []]),
        ('string', [0, '5', '10', 'N/A', None, MISSING]),
        (None, [0, '5', '10', 'N/A', None, MISSING]),
        ('string', [0, '5', '10', 'a', 'b', 'c', None]),  # Exactly half present values are numeric.
    ],
)
def test_numeric_detection_and_missing(registry_database, kind, values):
    async def scenario(database):
        await seed(database, values, kind)
        result = await histogram(bins=2)
        assert result == {
            'field': 'value',
            'total': len(values),
            'missing': len(values) - 3,
            'bins': 2,
            'buckets': [
                {'key': 1, 'count': 2, 'label': '0.00 - 5.00', 'range': {'min': 0.0, 'max': 5.000000005}},
                {'key': 2, 'count': 1, 'label': '5.00 - 10.00', 'range': {'min': 5.000000005, 'max': 10.0}},
            ],
        }

    registry_database(scenario)


@pytest.mark.parametrize(
    'kind,values,expected,missing',
    [
        (
            'string',
            ['1e2', '3e2', 'cat', 'dog', 'elephant', None, MISSING],
            {'1e2': 1, '3e2': 1, 'cat': 1, 'dog': 1, 'elephant': 1},
            2,
        ),
        ('number', [5, 5, 'N/A', None], {'5': 2, 'N/A': 1}, 1),
        ('number', ['2', '3', None], {'2': 1, '3': 1}, 1),
        ('number', [None, MISSING], {}, 2),
        ('string', [True, False, {'a': 1}, [], '', None], {'true': 1, 'false': 1, '{"a": 1}': 1, '[]': 1, '': 1}, 1),
        (
            'string',
            ['1e9999', '-1e9999', '1e-9999', '.5', '1.', 'NaN', 'Infinity', ''],
            {'1e9999': 1, '-1e9999': 1, '1e-9999': 1, '.5': 1, '1.': 1, 'NaN': 1, 'Infinity': 1, '': 1},
            0,
        ),
    ],
)
def test_categorical_fallback_keeps_all_raw_values(
    registry_database,
    kind,
    values,
    expected,
    missing,
):
    async def scenario(database):
        await seed(database, values, kind)
        result = await histogram(top=0)
        assert categories(result) == expected
        assert result['total'] == len(values)
        assert result['missing'] == missing
        assert result['distinct_total'] == len(expected)
        assert result['other_count'] == 0
        assert 'bins' not in result
        assert all(b['label'] == b['key'] for b in result['buckets'])

    registry_database(scenario)


def test_numeric_strings_ignore_overflow_without_changing_detection(registry_database):
    async def scenario(database):
        await seed(database, ['-1e2', '+1e2', '1e9999', '1e-9999', None], 'string')
        result = await histogram(bins=1)
        assert result['missing'] == 3
        assert result['buckets'] == [
            {'key': 1, 'count': 2, 'label': '-100.00 - 100.00', 'range': {'min': -100.0, 'max': 100.0}},
        ]

    registry_database(scenario)


def test_categorical_detection_does_not_evaluate_unused_numeric_range(registry_database):
    async def scenario(database):
        await seed(database, ['-1e308', '1e308', 'a', 'b', 'c'], 'string')
        result = await histogram()
        assert categories(result) == {'-1e308': 1, '1e308': 1, 'a': 1, 'b': 1, 'c': 1}
        assert 'bins' not in result

    registry_database(scenario)


@pytest.mark.parametrize('maximum', [1e-320, math.nextafter(2.47032823e-315, 0), 2.47032823e-315, 1e-310])
def test_subnormal_bin_padding_matches_python(registry_database, maximum):
    async def scenario(database):
        await seed(database, [0, maximum])
        result = await histogram(bins=2)
        midpoint = (maximum + maximum * 1e-9) / 2
        assert result == {
            'field': 'value',
            'total': 2,
            'missing': 0,
            'bins': 2,
            'buckets': [
                {'key': 1, 'count': 1, 'label': '0.00 - 0.00', 'range': {'min': 0.0, 'max': midpoint}},
                {
                    'key': 2,
                    'count': int(maximum * 1e-9 > 0),
                    'label': '0.00 - 0.00',
                    'range': {'min': midpoint, 'max': maximum},
                },
            ],
        }

    registry_database(scenario)


@pytest.mark.parametrize(
    'kind,values',
    [
        ('number', [-1e308, 1e308]),
        ('number', [0, 1.7976931348623157e308]),
        # The span overflows although the minimum is small: still main's error, not a 400.
        ('number', [-5e306, 1.79e308]),
        ('number', [-1e306, 1.7976931348623157e308]),
        ('string', ['-5e306', '1.79e308']),
    ],
)
def test_nonfinite_bin_bound_preserves_error(registry_database, kind, values):
    async def scenario(database):
        await seed(database, values, kind)
        with pytest.raises(asyncpg.UndefinedColumnError, match='column "inf" does not exist'):
            await histogram(as_number=kind == 'string')

    registry_database(scenario)


@pytest.mark.parametrize('values', [[-1e-310, 1e-310], [-5e-324, 1.79e308]])
def test_opposite_sign_extremes_do_not_underflow(registry_database, values):
    async def scenario(database):
        await seed(database, values)
        result = await histogram(bins=2)
        assert sum(b['count'] for b in result['buckets']) == 2
        assert [result['buckets'][0]['range']['min'], result['buckets'][-1]['range']['max']] == values

    registry_database(scenario)


def test_created_at_all_infinity_preserves_error(registry_database):
    async def scenario(database):
        await seed(database, [1, 2])
        await database.execute("UPDATE samples SET created_at = 'infinity'")
        with pytest.raises(asyncpg.UndefinedColumnError, match='column "inf" does not exist'):
            await histogram('created_at')

    registry_database(scenario)


@pytest.mark.parametrize('bins,count,expected_bins', [(None, 2, 8), (None, 1000, 30), (1, 2, 1), (200, 2, 200)])
def test_bin_count_and_zero_buckets(registry_database, bins, count, expected_bins):
    async def scenario(database):
        await seed(database, [0] * (count - 1) + [10])
        result = await histogram(bins=bins)
        assert result['bins'] == expected_bins
        assert len(result['buckets']) == expected_bins
        assert sum(b['count'] for b in result['buckets']) == count
        if expected_bins > 1:
            assert [b['count'] for b in result['buckets']] == [count - 1] + [0] * (expected_bins - 2) + [1]
        assert result['buckets'][0]['range']['min'] == 0
        assert result['buckets'][-1]['range']['max'] == 10

    registry_database(scenario)


@pytest.mark.parametrize('field', ['id', 'created_at', 'value'])
def test_no_matches(registry_database, field):
    async def scenario(database):
        await seed(database, [0, 10])
        result = await histogram(field, filters={'field': 'id', 'operator': '=', 'value': -1})
        assert result['total'] == result['missing'] == 0
        assert result['buckets'] == []
        assert 'bins' not in result

    registry_database(scenario)


@pytest.mark.parametrize('field', ['id', 'created_at', 'capture_time', 'taken_at'])
def test_single_native_or_timestamp_value(registry_database, field):
    async def scenario(database):
        await seed(database, ['2024-01-01T00:00:00-06:00'], 'datetime', field)
        await database.execute("UPDATE samples SET created_at='2024-01-01T06:00:00Z'")
        result = await histogram(field)
        assert result['bins'] == 1
        assert result['total'] == 1 and result['missing'] == 0
        bucket = result['buckets'][0]
        assert bucket['count'] == 1
        assert bucket['range']['min'] == bucket['range']['max']
        if field != 'id':
            assert bucket['range']['min_label'] == bucket['range']['max_label'] == '2024-01-01T06:00:00'

    registry_database(scenario)


def test_timestamp_offsets_nulls_invalid_text_and_labels(registry_database):
    async def scenario(database):
        await seed(
            database,
            [
                '2024-01-01T00:00:00-06:00',
                '2024-01-01T06:00:00Z',
                '2024-01-01T08:00:00+00:00',
                'not a date',
                None,
                MISSING,
            ],
            'datetime',
        )
        result = await histogram(bins=2)
        assert result == {
            'field': 'value',
            'total': 6,
            'missing': 3,
            'bins': 2,
            'buckets': [
                {
                    'key': 1,
                    'count': 2,
                    'label': '2024-01-01T06:00:00 - 2024-01-01T07:00:00.000004',
                    'range': {
                        'min': 1704088800.0,
                        'max': 1704092400.0000036,
                        'min_label': '2024-01-01T06:00:00',
                        'max_label': '2024-01-01T07:00:00.000004',
                    },
                },
                {
                    'key': 2,
                    'count': 1,
                    'label': '2024-01-01T07:00:00.000004 - 2024-01-01T08:00:00',
                    'range': {
                        'min': 1704092400.0000036,
                        'max': 1704096000.0,
                        'min_label': '2024-01-01T07:00:00.000004',
                        'max_label': '2024-01-01T08:00:00',
                    },
                },
            ],
        }

    registry_database(scenario)


def test_timestamp_degeneracy_is_checked_before_float_rounding(registry_database):
    async def scenario(database):
        await seed(database, ['9000-01-01T00:00:00.000001Z', '9000-01-01T00:00:00.000002Z'], 'datetime')
        result = await histogram()
        assert result['bins'] == 8  # Distinct exact epochs can round to the same float8.
        assert [b['count'] for b in result['buckets']] == [2] + [0] * 7

    registry_database(scenario)


@pytest.mark.parametrize(
    'kind,value,error',
    [
        ('datetime', '2024-99-01', asyncpg.DatetimeFieldOverflowError),
        ('number', 10**400, asyncpg.NumericValueOutOfRangeError),
    ],
)
def test_invalid_typed_values_preserve_errors(registry_database, kind, value, error):
    async def scenario(database):
        await seed(database, [value], kind)
        with pytest.raises(error):
            await histogram()

    registry_database(scenario)


def test_arrays_native_columns_and_top_zero(registry_database):
    async def scenario(database):
        await seed(database, [1] * 5)
        await database.execute("""
            UPDATE samples SET tags = CASE id WHEN 1 THEN ARRAY['a','a'] WHEN 2 THEN ARRAY['b']
                WHEN 3 THEN ARRAY[]::text[] WHEN 4 THEN NULL ELSE ARRAY['a',NULL] END;
            ALTER TABLE samples ADD COLUMN reviewed text[];
            INSERT INTO sample_array_columns VALUES ('reviewed');
            INSERT INTO dataset_fields (dataset_id,field_name,field_type,origin)
                VALUES (1,'reviewed','list','native_array');
            UPDATE samples SET reviewed=ARRAY['0','10','N/A'];
        """)
        result = await histogram('tags', bins=2, top=1)
        assert result == {
            'field': 'tags',
            'total': 5,
            'missing': 1,
            'distinct_total': 2,
            'other_count': 1,
            'buckets': [{'key': 'a', 'count': 3, 'label': 'a'}],
        }
        assert categories(await histogram('tags', top=0)) == {'a': 3, 'b': 1}
        result = await histogram('reviewed')
        assert result['total'] == 15 and result['missing'] == 0
        assert categories(result) == {'0': 5, '10': 5, 'N/A': 5}
        # Explicit numeric bins on a non-builtin array fail in the original SQL too.
        with pytest.raises(asyncpg.DatatypeMismatchError, match='must not return a set'):
            await histogram('reviewed', bins=2)
        result = await histogram('filepath', bins=2, top=0)
        assert result['total'] == 5 and result['distinct_total'] == 5
        assert categories(result) == {str(i): 1 for i in range(5)}
        result = await histogram('media_type', bins=2)
        assert categories(result) == {'image': 5} and 'bins' not in result

    registry_database(scenario)


def test_top_limit_reports_other_and_all_is_complete(registry_database):
    async def scenario(database):
        await seed(database, ['a'] * 3 + ['b'] * 2 + ['c', None, MISSING], 'string')
        result = await histogram(top=2)
        assert result == {
            'field': 'value',
            'total': 8,
            'missing': 2,
            'distinct_total': 3,
            'other_count': 1,
            'buckets': [{'key': 'a', 'count': 3, 'label': 'a'}, {'key': 'b', 'count': 2, 'label': 'b'}],
        }
        result = await histogram(top=0)
        assert categories(result) == {'a': 3, 'b': 2, 'c': 1}
        assert result['other_count'] == 0

    registry_database(scenario)


def test_tied_categories_are_ordered_by_value(registry_database):
    async def scenario(database):
        singles = list('qwertyuiopasdfghjklz')
        await seed(database, ['m', 'm', 'b', 'b', *singles], 'string')
        await database.execute("UPDATE samples SET tags = ARRAY[metadata->>'value']")
        expected = ['b', 'm', *sorted(singles)]
        for field in ('value', 'tags'):  # metadata fallback and native categorical
            result = await histogram(field, top=0)
            assert [b['key'] for b in result['buckets']] == expected
            result = await histogram(field, top=5)
            assert [b['key'] for b in result['buckets']] == expected[:5]
            assert result['other_count'] == len(singles) - 3

    registry_database(scenario)


def test_cross_grain_concept_and_duplicate_filters(registry_database):
    async def scenario(database):
        await seed(database, [0, 10, 20, 30])
        await database.execute("""
            UPDATE samples SET metadata=metadata || '{"split":"train"}' WHERE id=1;
            UPDATE samples SET media_type='object', metadata=metadata || '{"parent_id":1,"label":"person"}' WHERE id=3;
            UPDATE samples SET media_type='object', metadata=metadata || '{"parent_id":2,"label":"car"}' WHERE id=4;
            INSERT INTO samples (dataset_id,filepath,media_type,metadata) VALUES (2,'foreign','image','{"value":999}');
            INSERT INTO embedding_concepts (id,dataset_id,name,embedding_field,kind,weight)
                VALUES (1,1,'concept','clip','seed','[1,0]');
            INSERT INTO embedding_concept_scores (concept_id,sample_id,score) VALUES (1,1,0.9),(1,2,0.1);
            INSERT INTO sample_dup_secondary VALUES (1,'_any',2);
        """)
        cases = [
            (
                {
                    'field': '_cg',
                    'operator': 'parent_matches',
                    'value': {'field': 'split', 'operator': '=', 'value': 'train'},
                },
                1,
                [20, 20],
            ),
            (
                {
                    'field': '_cg',
                    'operator': 'has_matching_object',
                    'value': {'field': 'label', 'operator': '=', 'value': 'person'},
                },
                1,
                [0, 0],
            ),
            (
                {'field': '_concept', 'operator': 'concept_select', 'value': {'concept_id': 1, 'threshold': 0.5}},
                1,
                [0, 0],
            ),
            (
                {'field': '_concept', 'operator': 'concept_exclude', 'value': {'concept_id': 1, 'threshold': 0.5}},
                3,
                [10, 30],
            ),
            ({'field': '_collapse_duplicates', 'operator': 'collapse_duplicates', 'value': '_any'}, 3, [0, 30]),
        ]
        for filters, count, bounds in cases:
            result = await histogram(bins=2, filters=filters)
            assert result['total'] == count
            assert sum(b['count'] for b in result['buckets']) == count
            if bounds[0] == bounds[1]:
                assert categories(result) == {str(bounds[0]): 1}
            else:
                assert [result['buckets'][0]['range']['min'], result['buckets'][-1]['range']['max']] == bounds

    registry_database(scenario)


@pytest.mark.parametrize(
    'kind,values',
    [
        ('number', [0, 10, None]),
        ('string', ['x', 'x', 'y']),
        ('string', ['1', '2', None]),
        ('datetime', ['2024-01-01']),
    ],
)
def test_each_pass_extracts_each_sample_once(registry_database, monkeypatch, kind, values):
    async def scenario(database):
        await seed(database, values, kind)
        columns = await database.fetch("""SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='samples' ORDER BY ordinal_position""")
        projection = ','.join(
            'hist_touch(metadata) AS metadata' if r['column_name'] == 'metadata' else '"' + r['column_name'] + '"'
            for r in columns
        )
        # STABLE, so the view is flattened and every reference to `metadata` calls hist_touch.
        await database.execute(f"""
            CREATE SEQUENCE histogram_reads;
            CREATE FUNCTION hist_touch(value jsonb) RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
                BEGIN PERFORM nextval('histogram_reads'); RETURN value; END $$;
            ALTER TABLE samples RENAME TO histogram_samples;
            CREATE VIEW samples AS SELECT {projection} FROM histogram_samples;
        """)
        calls = record_reads(monkeypatch)
        result = await histogram(bins=2)
        assert result['total'] == len(values)
        reads = await database.fetchval("SELECT last_value FROM histogram_reads")
        assert reads == len(values) * len(calls)

    registry_database(scenario)


@pytest.mark.parametrize(
    'field,kind,values,portals',
    [
        ('value', 'number', [0, 10], ['fetch', 'fetch']),
        ('created_at', 'number', [0, 10], ['fetch', 'fetch']),
        ('value', None, ['x', 'y'], ['fetch', 'fetchrow']),
        # Declared text skips the numeric-detection pass.
        ('value', 'string', ['x', 'y'], ['fetchrow']),
        ('media_type', 'string', ['x', 'y'], ['fetchrow']),
        ('tags', 'string', ['x', 'y'], ['fetchrow']),
    ],
)
def test_scans_can_run_in_parallel_but_category_grouping_stays_serial(
    registry_database,
    monkeypatch,
    field,
    kind,
    values,
    portals,
):
    async def scenario(database):
        await seed(database, values, kind)
        calls = record_reads(monkeypatch)
        await histogram(field)
        # A one-row (fetchrow) portal runs serially: parallel workers would each hash the values.
        assert [method for method, _, _ in calls] == portals

    registry_database(scenario)


@pytest.mark.parametrize('field,kind', [('id', None), ('created_at', None), ('value', 'number'), ('value', 'string')])
def test_numeric_passes_run_in_parallel_workers(registry_database, monkeypatch, field, kind):
    async def scenario(database):
        await seed(database, [str(i) if kind == 'string' else i for i in range(2000)], kind)
        calls = record_reads(monkeypatch)
        result = await histogram(field, as_number=kind == 'string')
        assert sum(b['count'] for b in result['buckets']) == 2000
        assert len(calls) == 2
        await database.execute('ANALYZE samples')
        async with database.pool.acquire() as conn:
            # Make a parallel scan of this small table cheap, to see whether each pass can use one.
            await conn.execute("""SET parallel_setup_cost = 0; SET parallel_tuple_cost = 0;
                SET min_parallel_table_scan_size = 0; SET max_parallel_workers_per_gather = 2;
                SET enable_indexscan = off; SET enable_bitmapscan = off""")
            plans = []
            for _, sql, args in calls:
                plan = await conn.fetchval(f'EXPLAIN (FORMAT JSON) {sql}', *args)
                plans.append(plan if isinstance(plan, str) else json.dumps(plan))
        # Stats are aggregated in the workers; the bucket pass is gathered from them too.
        assert '"Partial Mode": "Partial"' in plans[0]
        assert all('"Node Type": "Gather' in plan for plan in plans)

    registry_database(scenario)


def limit_temp_files(monkeypatch, temp_file_limit):
    """Run each sample read with 64 kB work_mem and the temp_file_limit chosen for its SQL."""
    for method in ('fetch', 'fetchrow'):
        original = getattr(asyncpg.Connection, method)

        async def limited(conn, query, *args, original=original, **kwargs):
            if 'FROM samples' in query:
                await conn.execute(f"SET work_mem = '64kB'; SET temp_file_limit = '{temp_file_limit(query)}'")
            return await original(conn, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, method, limited)


@pytest.mark.parametrize(
    'field,kind',
    [('id', None), ('created_at', None), ('value', 'number'), ('value', 'string'), ('value', 'datetime')],
)
def test_detection_and_bounds_pass_writes_no_temp_files(registry_database, monkeypatch, field, kind):
    async def scenario(database):
        values = [str(i) if kind == 'string' else i for i in range(5000)]
        if kind == 'datetime':
            values = [f'2024-01-01T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z' for i in range(5000)]
        await seed(database, values, kind)
        # Extracted values are never spooled; the bucket pass is the original grouped query.
        limit_temp_files(monkeypatch, lambda query: 0 if 'MIN(val)' in query else -1)
        result = await histogram(field, as_number=kind == 'string')
        assert result['total'] == 5000 and sum(b['count'] for b in result['buckets']) == 5000

    registry_database(scenario)


def test_text_detection_writes_no_temp_files_before_category_grouping(registry_database, monkeypatch):
    async def scenario(database):
        await seed(database, [f'{i:05d}-' + 'x' * 500 for i in range(2000)], 'string')
        # Only the categorical GROUP BY may spill; extracted values are never spooled.
        limit_temp_files(monkeypatch, lambda query: -1 if 'GROUP BY value' in query else 0)
        result = await histogram(top=3)
        assert result['distinct_total'] == 2000 and result['other_count'] == 1997
        assert [b['key'] for b in result['buckets']] == [f'{i:05d}-' + 'x' * 500 for i in range(3)]

    registry_database(scenario)


@pytest.mark.parametrize('mode', ['force_custom_plan', 'force_generic_plan'])
def test_histogram_promoted_filter_uses_partial_index(registry_database, monkeypatch, mode):
    async def scenario(database):
        await database.execute("""
            INSERT INTO samples (dataset_id,filepath,media_type,metadata)
            SELECT 1,n::text,'image',jsonb_build_object('batch',CASE WHEN n>1990 THEN 'late' ELSE 'early' END,
                'value',n) FROM generate_series(1,2000) n;
            INSERT INTO dataset_fields (dataset_id,field_name,field_type,is_promoted)
                VALUES (1,'batch','string',true),(1,'value','number',false);
            CREATE INDEX test_hist_batch ON samples ((metadata->>'batch')) WHERE dataset_id=1;
            ANALYZE samples;
        """)
        calls = record_reads(monkeypatch)
        result = await histogram(filters={'field': 'batch', 'operator': '=', 'value': 'late'})
        assert result['total'] == 10
        _, sql, args = calls[0]
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL plan_cache_mode={mode}')
                await conn.execute('SET LOCAL enable_seqscan=off')
                await conn.execute('PREPARE histogram_plan AS ' + sql)
                try:
                    params = "1, 'late'" + (', NULL' if len(args) == 3 else '')
                    plan = await conn.fetchval(f'EXPLAIN (FORMAT JSON) EXECUTE histogram_plan({params})')
                    assert 'test_hist_batch' in plan
                    assert (await conn.fetch(sql, *args))[0]['total_rows'] == 10
                finally:
                    await conn.execute('DEALLOCATE histogram_plan')

    registry_database(scenario)
