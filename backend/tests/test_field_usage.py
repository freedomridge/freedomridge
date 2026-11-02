"""Index advisor telemetry (backend/field_usage.py): what the API records per dataset field,
and that recording never fails, slows or waits in the request it measures."""

import asyncio
import os
import time
from datetime import timedelta

import pytest

from backend import field_usage
from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_field_registry_postgres import ingest

registry_database = test_field_registry_postgres.registry_database
requires_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL test database',
)


@pytest.fixture(autouse=True)
def empty_pending():
    field_usage.take_pending()
    yield
    field_usage.take_pending()


@pytest.mark.parametrize(
    'milliseconds, bucket',
    [(0, 0), (0.5, 0), (1, 1), (1.9, 1), (2, 2), (3.99, 2), (1000, 10), (16383, 14), (16384, 15), (10**9, 15)],
)
def test_latency_buckets_are_log2_milliseconds(milliseconds, bucket):
    assert field_usage._bucket(milliseconds) == bucket


def test_filter_fields_count_only_comparisons_a_field_index_serves():
    tree = {
        'logic': 'and',
        'filters': [
            {'field': 'country', 'operator': '=', 'value': 'US'},
            {'field': 'score', 'operator': '>=', 'value': 3},
            {'logic': 'or', 'filters': [{'field': 'site', 'operator': 'in', 'value': ['a']}]},
            {'field': 'note', 'operator': 'contains', 'value': 'x'},
            {'field': 'flag', 'operator': 'exists'},
            {'field': 'other', 'operator': '!=', 'value': 1},
            {'field': 'detections.label', 'operator': '=', 'value': 'car'},
        ],
    }
    assert field_usage.filter_fields(tree) == ['country', 'score', 'site']
    assert field_usage.filter_fields('{"field": "country", "operator": "<", "value": 1}') == ['country']
    assert field_usage.filter_fields({'country': 'US', 'score': {'min': 1}}) == ['country', 'score']  # legacy flat form
    assert field_usage.filter_fields(None) == []
    assert field_usage.filter_fields('not json') == []
    assert field_usage.filter_fields(['garbage']) == []


def test_recording_aggregates_in_memory():
    field_usage.record(1, 'filter', ['country', 'country', 'score'], 0.006)  # 3 ms each: shared by 2 fields
    field_usage.record(1, 'filter', ['country'], 0.5)
    field_usage.record(1, 'sort', [], 0.0001)
    field_usage.record(1, 'nonsense', ['x'], 1)
    batch = field_usage.take_pending()
    today = field_usage._today()
    country = batch[(1, 'filter', 'country', today)]
    assert country[0] == 2 and country[1] == pytest.approx(503)
    assert country[2][2] == 1 and country[2][9] == 1  # 3 ms and 500 ms
    assert batch[(1, 'filter', 'score', today)][0] == 1
    assert batch[(1, 'sort', '', today)][0] == 1  # no field: the dataset's baseline
    assert not any(key[1] == 'nonsense' for key in batch)


def test_telemetry_never_fails_the_request_it_measures(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('telemetry is broken')

    monkeypatch.setattr(field_usage, '_bucket', broken)
    field_usage.record(1, 'filter', ['country'], 0.1)  # swallowed
    field_usage.record('not a dataset', 'filter', ['country'], 'not a number')  # type: ignore[arg-type]
    with field_usage.measure(1, {'filter': ['country']}):
        pass
    monkeypatch.setattr(field_usage, 'record', broken)
    with field_usage.measure(1, {'filter': ['country']}):
        result = 'the request ran'
    assert result == 'the request ran'
    with pytest.raises(ValueError, match='the request failed'):  # the request's own error is untouched
        with field_usage.measure(1, {'filter': ['country']}):
            raise ValueError('the request failed')


def test_a_failed_flush_drops_its_rows_and_raises_nothing():
    class Broken:
        def acquire(self, timeout=None):
            raise OSError('database unavailable')

    field_usage.record(1, 'filter', ['country'], 0.1)
    assert asyncio.run(field_usage.flush(Broken())) == 0
    assert field_usage.take_pending() == {}


async def _fields(database, dataset_id=1):
    rows = await database.fetch(
        'SELECT usage, field_name, uses, total_ms, latency_log2 FROM field_usage_daily WHERE dataset_id = $1 '
        'ORDER BY usage, field_name',
        dataset_id,
    )
    return {(row['usage'], row['field_name']): row for row in rows}


@requires_database
def test_grid_histogram_autocomplete_and_facets_record_their_fields(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US', 'score': index, 'note': 'n'} for index in range(6)])
        await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(
                filters={
                    'logic': 'and',
                    'filters': [
                        {'field': 'country', 'operator': '=', 'value': 'US'},
                        {'field': 'note', 'operator': 'contains', 'value': 'n'},
                        {'field': 'forged', 'operator': '=', 'value': 'x'},
                    ],
                },
                sort_by='score',
                limit=2,
            ),
        )
        await datasets.list_samples_post(1, datasets.SamplesRequest(limit=2))
        await datasets.list_samples(
            1,
            limit=2,
            offset=0,
            filters='{"field": "score", "operator": ">", "value": 2}',
            sort_order='asc',
        )
        await datasets.get_histogram(1, 'score', bins=4, filters=None, top=50, as_number=False)
        await datasets.autocomplete_field(1, 'country', q='U', limit=10, filters=None)
        await datasets._get_dataset_field_stats_impl(
            1,
            {'field': 'score', 'operator': '>', 'value': 4},
            lightning_mode=False,
        )
        await field_usage.flush(database.pool)
        rows = await _fields(database)
        assert rows[('filter', 'country')]['uses'] == 1
        assert rows[('filter', 'score')]['uses'] == 1  # the GET grid
        assert rows[('filter', '')]['uses'] == 1  # the unfiltered page: the baseline
        assert rows[('sort', 'score')]['uses'] == 1
        assert rows[('sort', '')]['uses'] == 2
        assert rows[('histogram', 'score')]['uses'] == 1
        assert rows[('facet', 'country')]['uses'] >= 1  # autocomplete, and the filtered sidebar facet
        assert ('filter', 'note') not in rows  # `contains` is not served by a field index
        assert ('filter', 'forged') not in rows  # not a field of the dataset
        assert all(sum(row['latency_log2']) == row['uses'] for row in rows.values())

    registry_database(scenario)


@requires_database
def test_flushes_add_up_and_old_days_are_pruned(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US'}])
        today = field_usage._today()
        await database.execute(
            """INSERT INTO field_usage_daily VALUES
               (1, 'filter', 'country', $1, 5, 50, array_fill(0::bigint, ARRAY[16])),
               (1, 'filter', 'country', $2, 7, 70, array_fill(0::bigint, ARRAY[16]))""",
            today - timedelta(days=field_usage.WINDOW_DAYS),  # outside the window
            today - timedelta(days=field_usage.WINDOW_DAYS - 1),  # the window's first day
        )
        field_usage._last_prune = None
        field_usage.record(1, 'filter', ['country'], 0.004)
        await field_usage.flush(database.pool)
        field_usage.record(1, 'filter', ['country'], 0.004)
        field_usage.record(1, 'filter', ['country'], 2.0)
        await field_usage.flush(database.pool)
        rows = await database.fetch(
            "SELECT day, uses, total_ms, latency_log2 FROM field_usage_daily WHERE field_name = 'country' ORDER BY day",
        )
        assert [row['day'] for row in rows] == [today - timedelta(days=field_usage.WINDOW_DAYS - 1), today]
        current = rows[-1]
        assert current['uses'] == 3 and current['total_ms'] == pytest.approx(2008)
        assert current['latency_log2'][3] == 2 and current['latency_log2'][11] == 1

    registry_database(scenario)


@requires_database
def test_deleting_datasets_record_nothing(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US'}])
        await database.execute('UPDATE datasets SET deleting = TRUE WHERE id = 1')
        field_usage.record(1, 'filter', ['country'], 0.01)
        field_usage.record(2, 'filter', [], 0.01)
        await field_usage.flush(database.pool)
        assert await _fields(database) == {}
        assert ('filter', '') in await _fields(database, 2)

    registry_database(scenario)


@requires_database
def test_a_stuck_flush_never_slows_the_requests_it_measures(registry_database):
    """The flush runs outside any request: a flush blocked on its rows (here another session holds them)
    must not make a grid request wait, and requests keep recording meanwhile."""

    async def scenario(database):
        await ingest([{'country': 'US', 'score': 1}])
        field_usage.record(1, 'filter', ['country'], 0.01)
        await field_usage.flush(database.pool)
        blocker = await database.pool.acquire()
        transaction = blocker.transaction()
        await transaction.start()
        await blocker.execute('SELECT * FROM field_usage_daily FOR UPDATE')
        try:
            field_usage.record(1, 'filter', ['country'], 0.01)
            stuck = asyncio.create_task(field_usage.flush(database.pool))
            await asyncio.sleep(0.3)
            assert not stuck.done()  # waiting for the row lock
            started = time.monotonic()
            request = datasets.SamplesRequest(
                filters={'field': 'country', 'operator': '=', 'value': 'US'},
                limit=1,
            )
            await asyncio.wait_for(datasets.list_samples_post(1, request), 5)
            assert time.monotonic() - started < 2
            assert field_usage.take_pending()  # recorded while the flush was stuck
        finally:
            await transaction.rollback()
            await database.pool.release(blocker)
        assert await asyncio.wait_for(stuck, 10) == 1

    registry_database(scenario)
