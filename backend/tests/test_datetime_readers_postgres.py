"""Every reader of a datetime field compares instants (backend/datetimes.py), with and without an index on it, and
agrees with the others: filters, the query language, sorts and their pages, the histogram and the filter its bars
stage, bounds, /stats, /aggregate, autocomplete and a version. Only an explicitly supplied disposable database."""

import json
import os

import pytest
from fastapi import HTTPException

from backend.filter_query import DATETIME, parse_query
from backend.routers import datasets, versions
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_histogram_postgres import MISSING, seed

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)

# a-d are one instant (08:00 UTC); text order and instant order differ throughout.
VALUES = {
    'a': '2026-01-01T10:00:00+02:00',
    'b': '2026-01-01T08:00:00Z',
    'c': '2026-01-01 08:00:00',
    'd': '2026-01-01T03:00:00-0500',
    'e': '2026-01-01T01:00:00+02:00',  # 23:00 UTC on 31 December
    'f': '2025-12-31T23:30:00Z',
    'g': '2026-01-01',
    'h': '2026-01-01T23:30:00-05:00',  # 04:30 UTC on 2 January
    'i': '2026-01-01T08:00:00.000001Z',
    'unknown': 'unknown',
    'empty': '',
    'impossible': '2026-02-30',
    'number': 12345,
    'null': None,
    'missing': MISSING,
}
NOT_DATETIMES = ['unknown', 'empty', 'impossible', 'number', 'null', 'missing']
OLDEST_FIRST = ['e', 'f', 'g', 'a', 'b', 'c', 'd', 'i', 'h', *NOT_DATETIMES]
NEWEST_FIRST = ['h', 'i', 'a', 'b', 'c', 'd', 'g', 'f', 'e', *NOT_DATETIMES]
INDEX = 'idx_samples_meta_1_observed_at'


def leaf(operator, value, field='observed_at'):
    return {'field': field, 'operator': operator, 'value': value}


async def names(filters=None, sort_order=None, limit=100, offset=0, field='observed_at'):
    request = datasets.SamplesRequest(
        filters=filters,
        sort_by=field if sort_order else None,
        sort_order=sort_order or 'asc',
        limit=limit,
        offset=offset,
    )
    return [item['filepath'] for item in (await datasets.list_samples_post(1, request))['items']]


async def setup(database, indexed):
    await seed(database, list(VALUES.values()), 'datetime', 'observed_at')
    await database.execute("UPDATE samples SET filepath = $1 WHERE filepath = $2", 'x', 'x')  # keeps the ids
    for position, name in enumerate(VALUES):
        await database.execute('UPDATE samples SET filepath = $1 WHERE filepath = $2', name, str(position))
    if indexed:  # as promote builds it: the index is on the instant
        await database.execute(
            f"CREATE INDEX {INDEX} ON samples ((metadata_instant_v1(metadata->>'observed_at'))) WHERE dataset_id = 1",
        )
        await database.execute("UPDATE dataset_fields SET is_promoted = TRUE WHERE field_name = 'observed_at'")
    await database.execute('ANALYZE samples')


@pytest.mark.parametrize('indexed', [False, True], ids=['unindexed', 'indexed'])
def test_filters_compare_instants(registry_database, indexed):
    async def scenario(database):
        await setup(database, indexed)
        moment = {'a', 'b', 'c', 'd'}
        assert set(await names(leaf('=', '2026-01-01T08:00:00.000000Z'))) == moment
        assert set(await names(leaf('=', '2026-01-01T08:00:00Z'))) == moment | {'i'}  # a whole second
        assert set(await names(leaf('=', '2026-01-01T10:00+02:00'))) == moment | {'i'}  # a whole minute, offset
        day = moment | {'g', 'i'}
        assert set(await names(leaf('=', '2026-01-01'))) == day  # the whole UTC day
        assert set(await names(leaf('!=', '2026-01-01'))) == set(VALUES) - day  # those without a datetime too
        assert set(await names(leaf('>=', '2026-01-01T00:00:00Z'))) == day | {'h'}
        assert set(await names(leaf('>', '2026-01-01'))) == {'h'}  # after the whole day
        assert set(await names(leaf('<=', '2026-01-01T08:00'))) == moment | {'e', 'f', 'g', 'i'}  # the whole minute
        assert set(await names(leaf('<', '2026-01-01T08:00'))) == {'e', 'f', 'g'}
        both = {'logic': 'AND', 'filters': [leaf('>=', '2025-12-31T23:15:00Z'), leaf('<', '2026-01-01T08:00Z')]}
        assert set(await names(both)) == {'f', 'g'}
        empty = {'logic': 'AND', 'filters': [leaf('>', '2026-01-02'), leaf('<', '2026-01-01')]}
        assert await names(empty) == []
        assert set(await names(leaf('in', ['2026-01-01', 'unknown']))) == day | {'unknown'}
        assert set(await names(leaf('not in', ['2026-01-01', 'unknown']))) == set(VALUES) - day - {'unknown'}
        assert set(await names({'logic': 'NOT', 'filters': [leaf('>=', '2026-01-01')]})) == {'e', 'f', *NOT_DATETIMES}
        # A value that isn't a datetime: equality compares the text, a range is refused, an empty one ignored.
        assert await names(leaf('=', 'unknown')) == ['unknown']
        assert set(await names(leaf('=', 12345))) == {'number'}
        with pytest.raises(HTTPException) as refused:
            await names(leaf('>', 'soon'))
        assert refused.value.status_code == 400 and 'is a datetime field' in refused.value.detail
        assert len(await names(leaf('>=', ''))) == len(VALUES)
        # A spelling outside the grammar that filters accepted before is still read, exactly.
        assert set(await names(leaf('>=', '2026-01-01T08'))) == moment | {'h', 'i'}
        # The query language reads its literals the same way.
        for query in ('observed_at = 2026-01-01', 'observed_at >= 2026-01-01T00:00:00Z AND observed_at < 2026-01-02'):
            parts = parse_query(query, {'observed_at': DATETIME})
            filters = parts[0] if len(parts) == 1 else {'logic': 'AND', 'filters': parts}
            assert set(await names(filters)) == day

    registry_database(scenario)


@pytest.mark.parametrize('indexed', [False, True], ids=['unindexed', 'indexed'])
def test_sorts_put_samples_without_a_datetime_last_in_both_directions(registry_database, indexed):
    async def scenario(database):
        await setup(database, indexed)
        assert await names(sort_order='asc') == OLDEST_FIRST
        assert await names(sort_order='desc') == NEWEST_FIRST
        for order, expected in (('asc', OLDEST_FIRST), ('desc', NEWEST_FIRST)):
            for size in (1, 4, 7):  # pages that cross from the datetimes into the rest
                pages = [await names(sort_order=order, limit=size, offset=start) for start in range(0, 16, size)]
                assert [name for page in pages for name in page] == expected, (order, size)
        filtered = await names(leaf('!=', '2025-12-31T23:30:00Z'), sort_order='desc', limit=3, offset=7)
        assert filtered == ['e', 'unknown', 'empty']

    registry_database(scenario)


@pytest.mark.parametrize('indexed', [False, True], ids=['unindexed', 'indexed'])
def test_histogram_bounds_stats_aggregate_and_autocomplete_agree(registry_database, indexed):
    async def scenario(database):
        await setup(database, indexed)
        histogram = await datasets.get_histogram(1, 'observed_at', 4, None, 50, False)
        assert histogram['total'] == len(VALUES) and histogram['missing'] == len(NOT_DATETIMES)
        assert sum(bucket['count'] for bucket in histogram['buckets']) == 9
        for bucket in histogram['buckets']:  # the filter a bar stages returns exactly that bar
            low, high = bucket['range']['min_label'], bucket['range']['max_label']
            staged = {'logic': 'AND', 'filters': [leaf('>=', low), leaf('<=', high)]}
            assert len(await names(staged)) == bucket['count'], bucket
        await run_refresh(database, 1, force=True)  # the statistics' bounds (stats_datetime)
        for filters, bounds in (
            (None, ('2025-12-31T23:00:00.000000Z', '2026-01-02T04:30:00.000000Z')),
            (leaf('<', '2026-01-02'), ('2025-12-31T23:00:00.000000Z', '2026-01-01T08:00:00.000001Z')),
        ):
            fields = await datasets._get_dataset_field_stats_impl(1, filters, lightning_mode=False)
            (field,) = [field for field in fields if field.get('name') == 'observed_at']
            assert (field['min'], field['max']) == bounds, filters
        ids = await database.fetch("SELECT id FROM samples WHERE filepath = ANY($1::text[])", ['b', 'e', 'unknown'])
        stats = await datasets.get_sample_stats(1, ','.join(str(row['id']) for row in ids))
        assert (stats['observed_at']['min'], stats['observed_at']['max']) == (
            '2025-12-31T23:00:00.000000Z',
            '2026-01-01T08:00:00.000000Z',
        )
        aggregate = await datasets.run_aggregations(
            1,
            [datasets.AggregationSpec(field='observed_at', op='values')],
            'WHERE dataset_id = $1',
            [1],
        )
        # Instant order, equal instants by their text, then the values that aren't datetimes by text.
        in_order = [VALUES[name] for name in ('e', 'f', 'g', 'c', 'd', 'b', 'a', 'i', 'h')]
        assert aggregate['results'][0]['value'] == [*in_order, '', '12345', '2026-02-30', 'unknown']
        found = await datasets._autocomplete_impl(1, 'observed_at', '2026-01-01T08', 10, None)
        assert {value['value'] for value in found['values']} == {VALUES['b'], VALUES['i']}

    registry_database(scenario)


def test_a_version_answers_like_the_dataset(registry_database):
    async def scenario(database):
        await setup(database, False)
        await datasets._get_dataset_field_stats_impl(1, None)
        version = (await versions.create_version(1, versions.CreateVersionRequest(name='v1')))['id']
        for filters in (leaf('=', '2026-01-01'), leaf('>=', '2026-01-01T00:00:00Z'), leaf('!=', '2026-01-01')):
            request = versions.VersionSamplesQueryRequest(filters=filters, limit=100)
            answer = await versions.query_version_samples(1, version, request)
            assert {item['filepath'] for item in answer['items']} == set(await names(filters)), filters

    registry_database(scenario)


def plan(rows):
    return json.dumps(json.loads(rows)[0]['Plan'])


@pytest.mark.parametrize('field,kind', [('observed_at', 'datetime'), ('speed', 'number')])
def test_an_indexed_sort_reads_the_index_and_still_puts_missing_values_last(registry_database, field, kind):
    """Newest first on an indexed field: the samples with a value straight from the index (no sort of the dataset),
    then those without; paging crosses from one part to the other. Numbers too (they put missing values first)."""

    async def scenario(database):
        key = f"metadata_instant_v1(metadata->>'{field}')" if kind == 'datetime' else f"((metadata->>'{field}')::float)"
        value = (
            "to_char(timestamp '2026-01-01' + n * interval '1 minute', 'YYYY-MM-DD\"T\"HH24:MI:SS') || '+02:00'"
            if kind == 'datetime'
            else 'n * 1.5'
        )
        await database.execute(
            'INSERT INTO dataset_fields (dataset_id, field_name, field_type, is_promoted) VALUES (1, $1, $2, TRUE)',
            field,
            kind,
        )
        await database.execute(
            f"""INSERT INTO samples (dataset_id, filepath, media_type, metadata)
                SELECT 1, n::text, 'image',
                       CASE WHEN n % 10 = 0 THEN '{{}}'::jsonb ELSE jsonb_build_object('{field}', {value}) END
                FROM generate_series(1, 20000) n""",
        )
        await database.execute(f'CREATE INDEX idx_samples_meta_1_{field} ON samples (({key})) WHERE dataset_id = 1')
        await database.execute('ANALYZE samples')
        newest = await names(sort_order='desc', limit=3, field=field)
        assert newest == ['19999', '19998', '19997']
        crossing = await names(sort_order='desc', limit=4, offset=17998, field=field)
        assert crossing == ['2', '1', '10', '20']  # the last two with a value, then the first without, by id
        assert await names(sort_order='asc', limit=2, offset=19999, field=field) == ['20000']
        first_part = await database.fetchval(
            f"EXPLAIN (FORMAT JSON) SELECT id FROM samples WHERE dataset_id = 1 AND {key} IS NOT NULL "
            f"ORDER BY {key} DESC NULLS FIRST, id LIMIT 50",
        )
        assert f'"Index Name": "idx_samples_meta_1_{field}"' in plan(first_part)
        assert '"Scan Direction": "Backward"' in plan(first_part)
        assert '"Sort Method"' not in plan(first_part) and '"Node Type": "Sort"' not in plan(first_part)

    registry_database(scenario)
