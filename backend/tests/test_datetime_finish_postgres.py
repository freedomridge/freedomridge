"""The datetime work's last part: last_modified_at filters like created_at; renaming or clearing a field whose index is
being rebuilt removes its old index; an indexed datetime field's range and bounds read its index. Only an explicitly
supplied disposable database."""

import json
import os

import pytest
from fastapi import HTTPException

from backend import index_jobs
from backend.datetimes import instant_expr
from backend.filter_query import parse_query
from backend.routers import datasets, versions
from backend.routers import filter_query as filter_query_router
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.test_field_index_rebuilds import INSTANT, index, promoted, queue_field, rows, text_index
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

# When each sample last changed (the samples column, not metadata).
MODIFIED = {
    'a': '2026-01-01T08:00:00Z',
    'b': '2026-01-01T23:30:00Z',
    'c': '2026-01-02T00:00:00.000001Z',
    'd': '2025-12-31T23:59:59Z',
}


def leaf(operator, value=None, field='last_modified_at'):
    return {'field': field, 'operator': operator, 'value': value}


async def names(filters=None):
    request = datasets.SamplesRequest(filters=filters, limit=100)
    return {item['filepath'] for item in (await datasets.list_samples_post(1, request))['items']}


async def modified(database):
    ids = (await ingest([{'note': name} for name in MODIFIED]))['ids']
    for (name, moment), sample_id in zip(MODIFIED.items(), ids):
        await database.execute(
            'UPDATE samples SET filepath = $1, last_modified_at = $2::text::timestamptz WHERE id = $3',
            name,
            moment,
            sample_id,
        )


def test_last_modified_at_filters_compare_its_moments_like_created_at(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(filter_query_router, 'db', database)
        await modified(database)
        assert await names(leaf('>=', '2026-01-01')) == {'a', 'b', 'c'}
        assert await names(leaf('=', '2026-01-01')) == {'a', 'b'}  # the whole UTC day
        assert await names(leaf('!=', '2026-01-01')) == {'c', 'd'}
        assert await names(leaf('>', '2026-01-01')) == {'c'}
        assert await names(leaf('<=', '2026-01-01T23:30')) == {'a', 'b', 'd'}  # the whole minute
        assert await names(leaf('=', '2026-01-01T10:00+02:00')) == {'a'}  # 08:00 UTC
        assert await names(leaf('in', ['2026-01-02T00:00:00.000001Z', '2025-12-31T23:59:59Z'])) == {'c', 'd'}
        assert await names(leaf('exists')) == set(MODIFIED) and await names(leaf('not_exists')) == set()
        with pytest.raises(HTTPException) as refused:
            await names(leaf('>', 'soon'))
        assert refused.value.status_code == 400 and 'last_modified_at' in refused.value.detail
        # The query language knows it as a datetime, and reads its literals the same way.
        kinds = await filter_query_router.field_kinds(1)
        assert kinds['last_modified_at'] == 'datetime'
        (parsed,) = parse_query('last_modified_at = 2026-01-01', kinds)
        assert await names(parsed) == {'a', 'b'}
        # Its histogram bins the column, and each bar's filter returns exactly that bar.
        histogram = await datasets.get_histogram(1, 'last_modified_at', 4, None, 50, False)
        assert histogram['total'] == 4 and histogram['missing'] == 0
        for bucket in histogram['buckets']:
            low, high = bucket['range']['min_label'], bucket['range']['max_label']
            staged = {'logic': 'AND', 'filters': [leaf('>=', low), leaf('<=', high)]}
            assert len(await names(staged)) == bucket['count'], bucket
        # A version answers like the dataset.
        version = (await versions.create_version(1, versions.CreateVersionRequest(name='v1')))['id']
        request = versions.VersionSamplesQueryRequest(filters=leaf('=', '2026-01-01'), limit=100)
        answer = await versions.query_version_samples(1, version, request)
        assert {item['filepath'] for item in answer['items']} == {'a', 'b'}

    registry_database(scenario)


# Each with a fraction of a second, as stored timestamps have; `none` has no timestamp.
STAMPED = {
    'a': '2026-01-01T08:00:00.123456Z',
    'b': '2026-01-01T23:30:15.5Z',
    'c': '2026-01-02T00:00:00.000001Z',
    'd': '2025-12-31T23:59:59.999999Z',
    'none': None,
}


@pytest.mark.parametrize('field', ['created_at', 'last_modified_at'])
def test_the_samples_timestamps_follow_the_rule_every_datetime_follows(registry_database, monkeypatch, field):
    """A value means the UTC day, minute or second it was written to, whatever its offset, for `=`, `!=`, `in` and
    `not in` as for ranges; one written with a fraction is that exact moment. They matched nothing before (`=` with
    an offset, `in` with any unit), as the stored values carry fractions. A sample without the timestamp is in every
    complement."""

    async def scenario(database):
        monkeypatch.setattr(filter_query_router, 'db', database)
        ids = (await ingest([{'note': name} for name in STAMPED]))['ids']
        for (name, moment), sample_id in zip(STAMPED.items(), ids):
            await database.execute(
                f'UPDATE samples SET filepath = $1, {field} = $2::text::timestamptz WHERE id = $3',
                name,
                moment,
                sample_id,
            )
        everything = set(STAMPED)

        def check(op, value):
            return names(leaf(op, value, field))

        assert await check('=', '2026-01-01') == {'a', 'b'}  # a day
        assert await check('!=', '2026-01-01') == everything - {'a', 'b'}
        assert await check('in', ['2026-01-02', '2025-12-31']) == {'c', 'd'}
        assert await check('not in', ['2026-01-01']) == everything - {'a', 'b'}
        assert await check('=', '2026-01-01T08:00') == {'a'}  # a minute
        assert await check('=', '2026-01-01T10:00+02:00') == {'a'}  # the same minute, written with an offset
        assert await check('in', ['2026-01-01T23:30', '2026-01-02T01:00+01:00']) == {'b', 'c'}
        assert await check('not in', ['2026-01-01T23:30']) == everything - {'b'}
        assert await check('=', '2026-01-01T23:30:15') == {'b'}  # a second
        assert await check('!=', '2026-01-01T23:30:15') == everything - {'b'}
        assert await check('=', '2026-01-01T08:00:00.123456Z') == {'a'}  # a fraction: that exact moment
        assert await check('=', '2026-01-01T08:00:00.123Z') == set()
        assert await check('in', ['2025-12-31T23:59:59.999999Z']) == {'d'}
        assert await check('<=', '2026-01-01T23:30') == {'a', 'b', 'd'}
        assert await check('>', '2026-01-01') == {'c'}
        assert await check('>=', '2026-01-01T08:00:00.123457Z') == {'b', 'c'}
        (parsed,) = parse_query(f'{field} in (2026-01-02, 2025-12-31)', await filter_query_router.field_kinds(1))
        assert await names(parsed) == {'c', 'd'}
        version = (await versions.create_version(1, versions.CreateVersionRequest(name='v1')))['id']
        request = versions.VersionSamplesQueryRequest(filters=leaf('in', ['2026-01-01T08:00'], field), limit=100)
        answer = await versions.query_version_samples(1, version, request)
        assert {item['filepath'] for item in answer['items']} == {'a'}

    registry_database(scenario)


def test_created_at_by_the_day_reads_its_index(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO samples (dataset_id, filepath, media_type, created_at)
               SELECT 1, n::text, 'image', timestamptz '2026-01-01' + n * interval '1 minute' + interval '0.5 second'
               FROM generate_series(1, 20000) n""",
        )
        await database.execute('ANALYZE samples')
        where, args, _ = datasets.build_sample_query_parts(
            1,
            filters=json.dumps(leaf('=', '2026-01-03', 'created_at')),
            array_columns=await datasets.get_dataset_array_columns(1),
        )
        day = await database.fetchval(f'EXPLAIN (FORMAT JSON) SELECT id FROM samples {where}', *args)
        assert '"Index Name": "idx_samples_dataset_created_at"' in plan(day)
        counted = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters=leaf('=', '2026-01-03', 'created_at'), limit=1),
        )
        assert counted['total'] == 24 * 60

    registry_database(scenario)


def test_the_schema_lists_last_modified_at_with_bounds_when_every_field_is_computed(registry_database):
    """No index covers it, so its bounds (a scan of the dataset) are read outside lightning mode only."""

    async def scenario(database):
        await modified(database)
        computed = await datasets._get_dataset_field_stats_impl(1, None, lightning_mode=False)
        (field,) = [field for field in computed if field.get('name') == 'last_modified_at']
        assert field['type'] == 'datetime'
        assert (field['min'], field['max']) == ('2025-12-31T23:59:59+00:00', '2026-01-02T00:00:00.000001+00:00')
        lightning = await datasets._get_dataset_field_stats_impl(1, None, lightning_mode=True)
        (field,) = [field for field in lightning if field.get('name') == 'last_modified_at']
        assert field == {'name': 'last_modified_at', 'type': 'datetime', 'provenance': 'cached'}
        schema = await datasets.get_dataset_schema(1, None, lightning_mode=False)
        assert {'name': 'last_modified_at', 'type': 'datetime'}.items() <= next(
            entry for entry in schema if entry['name'] == 'last_modified_at'
        ).items()

    registry_database(scenario)


async def being_rebuilt(database):
    """A datetime field indexed before 0024 (its index on the text), queued for its rebuild, which is queued too."""
    await ingest([{'captured': '2026-01-01T10:00:00+02:00'}, {'captured': '2026-01-02'}])
    old = await text_index(database, 'captured')
    await queue_field(database, (1, 'captured'))
    assert await index_jobs.queue_rebuilds(database.pool) == 1
    return old


def test_renaming_a_field_being_rebuilt_drops_its_old_index_and_rebuilds_the_new_name(registry_database):
    async def scenario(database):
        old = await being_rebuilt(database)
        await datasets.rename_field(1, 'captured', datasets.FieldRenameRequest(new_name='taken'), USER)
        assert await index(database, old) is None  # it stayed behind, named for a field that no longer exists
        assert await rows(database) == [('field', 1, 'taken')]
        assert await job_helpers.claim(database, index_jobs.FIELD_KIND) is None  # the old name's build: cancelled
        assert await database.fetchval("SELECT state FROM jobs WHERE args->>'field_name' = 'captured'") == 'cancelled'
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert (await job_helpers.finish(database, index_jobs.FIELD_KIND))['state'] == 'succeeded'
        name = datasets._promoted_index_name(1, 'taken')
        assert await index(database, name) == (INSTANT.format('taken'), True)
        assert await promoted(database, 'taken') and await rows(database) == []

    registry_database(scenario)


def test_clearing_a_field_being_rebuilt_drops_its_old_index(registry_database):
    async def scenario(database):
        old = await being_rebuilt(database)
        await datasets.clear_field(1, 'captured', USER)
        assert await index(database, old) is None
        assert await rows(database) == []
        assert await job_helpers.claim(database, index_jobs.FIELD_KIND) is None

    registry_database(scenario)


def test_a_background_rename_or_clear_of_a_field_being_rebuilt_is_refused(registry_database):
    """The bulk job can't drop the old index in its transaction, so it refuses until the rebuild is done."""

    async def scenario(database):
        old = await being_rebuilt(database)
        for attempt in (
            datasets.rename_field(1, 'captured', datasets.FieldRenameRequest(new_name='taken', background=True), USER),
            datasets.clear_field(1, 'captured', USER, background=True),
        ):
            with pytest.raises(HTTPException) as refused:
                await attempt
            assert refused.value.status_code == 409 and 'being rebuilt' in refused.value.detail
        assert await index(database, old) is not None and await rows(database) == [('field', 1, 'captured')]

    registry_database(scenario)


def plan(rows_json):
    return json.dumps(json.loads(rows_json)[0]['Plan'])


def test_an_indexed_datetime_range_and_its_bounds_read_the_index(registry_database):
    async def scenario(database):
        await database.execute(
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type, is_promoted) "
            "VALUES (1, 'observed_at', 'datetime', TRUE)",
        )
        await database.execute(
            """INSERT INTO samples (dataset_id, filepath, media_type, metadata)
               SELECT 1, n::text, 'image', jsonb_build_object('observed_at',
                   to_char(timestamp '2026-01-01' + n * interval '1 minute', 'YYYY-MM-DD"T"HH24:MI:SS') || '+02:00')
               FROM generate_series(1, 20000) n""",
        )
        name = datasets._promoted_index_name(1, 'observed_at')
        await database.execute(
            f"CREATE INDEX {name} ON samples ((metadata_instant_v1(metadata->>'observed_at'))) WHERE dataset_id = 1",
        )
        await database.execute('ANALYZE samples')
        week = {
            'logic': 'AND',
            'filters': [leaf('>=', '2026-01-03', 'observed_at'), leaf('<', '2026-01-04', 'observed_at')],
        }
        where, args, _ = datasets.build_sample_query_parts(
            1,
            filters=json.dumps(week),
            array_columns=await datasets.get_dataset_array_columns(1),
        )
        ranged = await database.fetchval(f'EXPLAIN (FORMAT JSON) SELECT id FROM samples {where}', *args)
        assert f'"Index Name": "{name}"' in plan(ranged)
        counted = await datasets.list_samples_post(1, datasets.SamplesRequest(filters=week, limit=1))
        assert counted['total'] == 24 * 60
        instant = instant_expr(datasets._field_text_sql('observed_at'), True)
        bounds = await database.fetchval(
            f'EXPLAIN (FORMAT JSON) SELECT MIN({instant}), MAX({instant}) FROM samples '
            f'WHERE dataset_id = 1 AND {instant} IS NOT NULL',
        )
        assert f'"Index Name": "{name}"' in plan(bounds) and '"Node Type": "Seq Scan"' not in plan(bounds)

    registry_database(scenario)
