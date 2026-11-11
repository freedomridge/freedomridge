"""Declared-text categorical default and the approximate broad-histogram preview."""

import asyncio
import json
import os

import asyncpg
import pytest

from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_histogram_postgres import MISSING, categories, histogram, record_reads, seed

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)

NUMERIC_LOOKING = ['1', '2', '10', '10', None, MISSING]


async def preview(field='value', bins=None, filters=None, top=50, as_number=False):
    filters_json = json.dumps(filters) if filters else None
    return await datasets.get_histogram_preview(1, field, bins, filters_json, top, as_number)


async def seed_broad(database):
    """150,000 dense rows: country USA 105,000 / Canada 44,996 / Mexico 4; speed 0-24.75; code '0'-'6'."""
    async with test_field_registry_postgres.bulk_setup(database) as connection:
        await connection.execute("""
        INSERT INTO dataset_fields (dataset_id,field_name,field_type)
            VALUES (1,'country','string'),(1,'speed','number'),(1,'code','string');
        INSERT INTO samples (dataset_id,filepath,media_type,metadata)
        SELECT 1, n::text, 'image', jsonb_build_object(
            'country', CASE WHEN n % 37500 = 0 THEN 'Mexico' WHEN n % 10 < 3 THEN 'Canada' ELSE 'USA' END,
            'speed', (n % 100) / 4.0,
            'code', (n % 7)::text)
        FROM generate_series(1, 150000) n;
    """)


# ---- declared text is categorical unless numbers are asked for ----


def test_declared_text_is_categorical_without_the_detection_pass(registry_database, monkeypatch):
    async def scenario(database):
        await seed(database, NUMERIC_LOOKING, 'string')
        calls = record_reads(monkeypatch)
        result = await histogram()
        assert result == {
            'field': 'value',
            'total': 6,
            'missing': 2,
            'distinct_total': 3,
            'other_count': 0,
            'buckets': [
                {'key': '10', 'count': 2, 'label': '10'},
                {'key': '1', 'count': 1, 'label': '1'},
                {'key': '2', 'count': 1, 'label': '2'},
            ],
        }
        # One grouped read of the samples: no numeric-detection scan first.
        assert [method for method, _, _ in calls] == ['fetchrow']

    registry_database(scenario)


@pytest.mark.parametrize('field', ['file_date', 'reviewed_at'])
def test_declared_text_with_a_time_like_name_is_categorical(registry_database, field):
    async def scenario(database):
        # Text the registry did not recognise as a datetime used to be binned as a time and,
        # with no ISO dates in it, came back as "no data".
        await seed(database, ['20240601', '20240601', 'unknown'], 'string', field)
        assert categories(await histogram(field)) == {'20240601': 2, 'unknown': 1}

    registry_database(scenario)


@pytest.mark.parametrize('field', ['file_date', 'reviewed_at'])
def test_declared_text_with_a_time_like_name_and_iso_values_keeps_time_bins(registry_database, field):
    # A timestamp field typed text (its first write mixed ISO times with '') kept main's
    # time bins; per-value categories would turn every timestamp into its own bar.
    async def scenario(database):
        values = [f'2024-01-{d:02d}T07:00:00' for d in range(1, 21)] + ['', '', MISSING]
        await seed(database, values, 'string', field)
        assert await histogram(field) == await histogram(field, as_number=True)
        assert 'distinct_total' not in await histogram(field)

    registry_database(scenario)


def test_as_number_restores_the_numeric_histogram(registry_database):
    async def scenario(database):
        await seed(database, NUMERIC_LOOKING, 'string')
        as_number = await histogram(as_number=True)
        assert as_number['bins'] == 8 and as_number['total'] == 6 and as_number['missing'] == 2
        assert sum(b['count'] for b in as_number['buckets']) == 4
        assert as_number['buckets'][0]['range']['min'] == 1 and as_number['buckets'][-1]['range']['max'] == 10
        # Exactly the detection-based histogram an undeclared field still gets.
        await database.execute("DELETE FROM dataset_fields WHERE field_name = 'value'")
        assert await histogram() == as_number
        # An explicit bin count (the SDK's histogram_values) is a numeric request too.
        await database.execute(
            "INSERT INTO dataset_fields (dataset_id,field_name,field_type) VALUES (1,'value','string')",
        )
        assert await histogram(bins=8) == as_number

    registry_database(scenario)


def test_as_number_keeps_the_mostly_text_fallback(registry_database):
    async def scenario(database):
        await seed(database, ['1', 'a', 'b', 'c'], 'string')
        result = await histogram(as_number=True, top=0)
        assert 'bins' not in result
        assert categories(result) == {'1': 1, 'a': 1, 'b': 1, 'c': 1}

    registry_database(scenario)


@pytest.mark.parametrize(
    'kind,values',
    [
        ('number', [0, 5, 10]),
        ('datetime', ['2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z']),
        (None, ['0', '5', '10']),
        ('boolean', [True, False, True]),
    ],
)
def test_other_declarations_ignore_as_number(registry_database, kind, values):
    async def scenario(database):
        await seed(database, values, kind)
        result = await histogram()
        assert ('bins' in result) == (kind != 'boolean')
        assert await histogram(as_number=True) == result

    registry_database(scenario)


# ---- a labelled approximate preview for broad histograms ----


def test_broad_preview_is_labelled_sampled_and_leaves_exact_unchanged(registry_database):
    async def scenario(database):
        await seed_broad(database)
        exact = await histogram('country', top=0)
        assert categories(exact) == {'USA': 105_000, 'Canada': 44_996, 'Mexico': 4}
        assert 'approximate' not in exact and 'preview' not in exact

        result = await preview('country', top=0)
        assert result['approximate'] is True
        assert result['preview'] == {
            'status': 'ok',
            'method': 'uniform_id_sample',
            'id_range': [1, 150_000],
            'draws': 20_000,
            'matched': 20_000,
            'estimated_matching_rows': 150_000,
        }
        # Sample counts, never scaled to the population; a category can be missing from them.
        assert result['total'] == 20_000 and result['missing'] == 0
        assert sum(b['count'] for b in result['buckets']) == 20_000
        sampled = categories(result)
        assert set(sampled) <= set(categories(exact)) and {'USA', 'Canada'} <= set(sampled)
        assert all(count <= categories(exact)[key] for key, count in sampled.items())
        assert result['distinct_total'] == len(sampled)
        # Stable for the same extent, so a refresh does not flicker; exact is unchanged by it.
        assert await preview('country', top=0) == result
        assert await histogram('country', top=0) == exact

    registry_database(scenario)


def test_numeric_preview_bins_the_sampled_values(registry_database):
    async def scenario(database):
        await seed_broad(database)
        result = await preview('speed')
        assert result['approximate'] is True and result['preview']['status'] == 'ok'
        assert result['bins'] == 30 and result['total'] == 20_000
        assert sum(b['count'] for b in result['buckets']) == 20_000 - result['missing']
        assert result['buckets'][0]['range']['min'] >= 0 and result['buckets'][-1]['range']['max'] <= 24.75

    registry_database(scenario)


def test_preview_applies_the_filter_to_the_sampled_rows(registry_database):
    async def scenario(database):
        await seed_broad(database)
        filters = {
            'logic': 'AND',
            'filters': [
                {'field': 'speed', 'operator': '>=', 'value': 5},
                {'field': 'media_type', 'operator': '!=', 'value': 'object'},
            ],
        }
        result = await preview('speed', filters=filters)
        detail = result['preview']
        assert detail['status'] == 'ok'
        # 120,000 rows match (80 %); every sampled row in the histogram passed the filter.
        assert 15_000 < detail['matched'] < 17_000 and result['total'] == detail['matched']
        assert detail['estimated_matching_rows'] == detail['matched'] * 150_000 // 20_000
        assert result['buckets'][0]['range']['min'] >= 5

    registry_database(scenario)


def test_preview_follows_declared_text_and_as_number(registry_database):
    async def scenario(database):
        await seed_broad(database)
        assert set(categories(await preview('code'))) == {str(i) for i in range(7)}
        numeric = await preview('code', as_number=True)
        assert numeric['bins'] == 30 and numeric['preview']['status'] == 'ok'

    registry_database(scenario)


def test_selective_filter_gets_no_preview(registry_database):
    async def scenario(database):
        await seed_broad(database)
        result = await preview('country', filters={'field': 'country', 'operator': '=', 'value': 'Canada'})
        assert result['preview']['status'] == 'not_broad' and 'buckets' not in result
        assert result['preview']['estimated_matching_rows'] <= 100_000

    registry_database(scenario)


def test_small_dataset_gets_no_preview_or_sample(registry_database, monkeypatch):
    async def scenario(database):
        await seed(database, list(range(12)))
        calls = record_reads(monkeypatch)
        assert await preview() == {'field': 'value', 'approximate': True, 'preview': {'status': 'not_broad'}}
        assert len(calls) == 1  # only the dataset's id extent

    registry_database(scenario)


def test_sparse_sample_is_insufficient_not_a_guess(registry_database):
    async def scenario(database):
        # 125,000 rows spread over 3 M ids: ~830 of 20,000 draws land on a row. Set up like seed_broad: planned per
        # row, the insert took 7-8 s of the fixture's 10 s statement timeout on an idle machine.
        async with test_field_registry_postgres.bulk_setup(database) as connection:
            await connection.execute("""
                INSERT INTO samples (id,dataset_id,filepath,media_type,metadata)
                SELECT n, 1, n::text, 'image', '{"value": 1}' FROM generate_series(1, 3000000, 24) n;
            """)
        result = await preview()
        detail = result['preview']
        assert detail['status'] == 'insufficient_sample' and 'buckets' not in result
        assert detail['matched'] < 1_000 < 100_000 < detail['estimated_matching_rows']

    registry_database(scenario)


def test_preview_budget_cancels_its_query_and_failures_are_not_errors(registry_database, monkeypatch):
    async def scenario(database):
        await seed_broad(database)

        async def slow(*args, **kwargs):
            await database.fetch('SELECT pg_sleep(30)')

        monkeypatch.setattr(datasets, '_histogram', slow)
        monkeypatch.setattr(datasets, '_HISTOGRAM_PREVIEW_BUDGET_S', 0.5)
        assert await preview('country') == {'field': 'country', 'approximate': True, 'preview': {'status': 'timeout'}}
        for _ in range(50):
            running = await database.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND query = 'SELECT pg_sleep(30)'",
            )
            if not running:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail('the cancelled preview query is still running')

        async def broken(*args, **kwargs):
            raise asyncpg.DatetimeFieldOverflowError('date/time field value out of range')

        monkeypatch.setattr(datasets, '_histogram', broken)
        assert (await preview('country'))['preview'] == {'status': 'failed'}

    registry_database(scenario)
