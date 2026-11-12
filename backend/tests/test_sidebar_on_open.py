"""Lightning sidebar: cached facets at once, filtered facets when a field is opened.

The sidebar request serves every field from the cache within one shared budget and labels each
facet's provenance; the facet request for an opened field computes it under the unchanged gates
and per-field budget. Lightning-off stays eager.
"""

import asyncio
import json
import time
from typing import cast

import pytest
from fastapi import Request

from backend.routers import datasets
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, registry_database  # noqa: F401
from backend.tests.test_filtered_query_plans import record_queries

IMAGE = {'field': 'media_type', 'operator': '!=', 'value': 'object'}


def stage(batch):
    """The sidebar's filter as the UI sends it: withGrain(stages, 'image') on a dataset without objects."""
    return {'logic': 'AND', 'filters': [IMAGE, {'field': 'batch', 'operator': '=', 'value': batch}]}


SELECTIVE = stage('late')  # 3 rows, under the (patched) 5-row breadth threshold
BROAD = stage('early')  # 17 rows
BARE = stage('bare')  # 2 rows without brightness or color
FACETS = ('created_at', 'tags', 'batch', 'color', 'score', 'brightness', 'captured')
# The probe is the only filtered query a lightning sidebar request may run.
PROBE = 'LIMIT 6'


async def seed(database, monkeypatch):
    monkeypatch.setattr(datasets, '_LIGHTNING_FACET_MAX_ROWS', 5)
    samples = []
    for n in range(1, 23):
        batch = 'bare' if n > 20 else 'late' if n > 17 else 'early'
        metadata = {'batch': batch, 'score': n, 'captured': f'2026-01-{n:02d}T00:00:00'}
        if batch != 'bare':
            metadata.update(brightness=n * 10, color=f'c{n % 4}')
        samples.append({'filepath': f'sample-{n}.jpg', 'tags': [batch], 'metadata': metadata})
    await datasets.add_samples(1, {'samples': samples}, user=USER)
    await database.execute(
        "UPDATE dataset_fields SET is_promoted = true WHERE dataset_id = 1 AND field_name = ANY($1::text[])",
        ['batch', 'score', 'captured'],
    )
    await run_refresh(database, 1)


def by_name(fields):
    return {field['name']: field for field in fields}


class Client:
    """The endpoint's view of an HTTP client that stays connected (its body was already read)."""

    async def receive(self):
        await asyncio.Event().wait()


def client() -> Request:
    return cast('Request', Client())


async def opened(filters, name, **kwargs):
    fields = await datasets._get_dataset_field_stats_impl(1, filters, fields=[name], **kwargs)
    assert [field['name'] for field in fields] == [name]
    return fields[0]


def test_lightning_sidebar_serves_cached_facets_with_provenance(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, monkeypatch)
        queries = record_queries(database, monkeypatch)
        unfiltered = by_name(await datasets._get_dataset_field_stats_impl(1, None))
        selective = by_name(await datasets._get_dataset_field_stats_impl(1, SELECTIVE))
        broad = by_name(await datasets._get_dataset_field_stats_impl(1, BROAD))

        assert {name: unfiltered[name]['provenance'] for name in (*FACETS, 'media_type')} == dict.fromkeys(
            (*FACETS, 'media_type'),
            'cached',
        )
        # A selective set allows every field's facet: each fills in when its section is opened.
        assert {name: selective[name]['provenance'] for name in FACETS} == dict.fromkeys(FACETS, 'pending')
        # A broad set allows only promoted MIN/MAX; the rest stay the dataset's values.
        assert {name: broad[name]['provenance'] for name in FACETS} == {
            'created_at': 'cached',
            'tags': 'cached',
            'batch': 'cached',
            'color': 'cached',
            'score': 'pending',
            'brightness': 'cached',
            'captured': 'pending',
        }
        assert selective['media_type']['provenance'] == 'cached'
        # Every field, value list and range is served at once from the cache, uncapped.
        assert set(selective) == set(unfiltered)
        assert (selective['score']['min'], selective['score']['max']) == (1, 22)
        assert (selective['brightness']['min'], selective['brightness']['max']) == (10, 200)
        assert (selective['captured']['min'], selective['captured']['max']) == (
            unfiltered['captured']['min'],
            unfiltered['captured']['max'],
        )
        assert set(selective['color']['values']) == {'c0', 'c1', 'c2', 'c3'}
        assert selective['batch']['values'] == ['bare', 'early', 'late']
        assert set(selective['tags']['values']) == {'bare', 'early', 'late'}
        # Nothing but the selectivity probe ran with the filter's values.
        filtered = [sql for _, sql, args in queries if {'late', 'early'} & set(map(str, args))]
        assert filtered and all(PROBE in sql for sql in filtered), filtered

    registry_database(scenario)


def test_opened_field_fills_in_under_the_unchanged_gates(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, monkeypatch)
        # Selective set: an opened field's facet is recomputed whatever its kind.
        score = await opened(SELECTIVE, 'score')
        assert (score['min'], score['max'], score['provenance']) == (18, 20, 'exact')
        brightness = await opened(SELECTIVE, 'brightness')
        assert (brightness['min'], brightness['max'], brightness['provenance']) == (180, 200, 'exact')
        captured = await opened(SELECTIVE, 'captured')
        assert (captured['min'], captured['max'], captured['provenance']) == (  # the earliest and latest instants
            '2026-01-18T00:00:00.000000Z',
            '2026-01-20T00:00:00.000000Z',
            'exact',
        )
        color = await opened(SELECTIVE, 'color')
        assert (sorted(color['values']), color['values_complete'], color['provenance']) == (
            ['c0', 'c2', 'c3'],
            True,
            'exact',
        )
        assert ((await opened(SELECTIVE, 'batch'))['values'], (await opened(SELECTIVE, 'tags'))['values']) == (
            ['late'],
            ['late'],
        )
        assert (await opened(SELECTIVE, 'created_at'))['provenance'] == 'exact'

        # Broad set: opening is not a way around the gate. Promoted MIN/MAX is recomputed; the
        # rest keep the dataset's values (a promoted enum its complete indexed list).
        score = await opened(BROAD, 'score')
        assert (score['min'], score['max'], score['provenance']) == (1, 17, 'exact')
        captured = await opened(BROAD, 'captured')
        assert (captured['max'], captured['provenance']) == ('2026-01-17T00:00:00.000000Z', 'exact')
        brightness = await opened(BROAD, 'brightness')
        assert (brightness['min'], brightness['max'], brightness['provenance']) == (10, 200, 'cached')
        batch = await opened(BROAD, 'batch')
        assert (batch['values'], batch['provenance']) == (['bare', 'early', 'late'], 'cached')
        assert (await opened(BROAD, 'color'))['provenance'] == 'cached'
        assert (await opened(BROAD, 'tags'))['provenance'] == 'cached'
        assert (await opened(BROAD, 'created_at'))['provenance'] == 'cached'

        # The endpoint takes the opened fields and returns only them.
        response = await datasets.get_dataset_field_stats_post(
            1,
            datasets.FieldStatsRequest(filters=SELECTIVE, fields=['score', 'color']),
            client(),
        )
        assert {field['name']: field['provenance'] for field in response} == {'score': 'exact', 'color': 'exact'}

        # Lightning-off stays eager: one request computes every facet, even on a broad set.
        eager = by_name(await datasets._get_dataset_field_stats_impl(1, BROAD, lightning_mode=False))
        assert {name: eager[name]['provenance'] for name in FACETS} == dict.fromkeys(FACETS, 'exact')
        assert (eager['brightness']['min'], eager['brightness']['max']) == (10, 170)
        assert (eager['batch']['values'], eager['tags']['values']) == (['early'], ['early'])

    registry_database(scenario)


def slow_probe(database, monkeypatch):
    original = database.fetchval

    async def fetchval(sql, *args):
        if PROBE in sql:
            return await original('SELECT count(*) FROM (SELECT pg_sleep(5)) slow_probe')
        return await original(sql, *args)

    monkeypatch.setattr(database, 'fetchval', fetchval)
    return original


def test_sidebar_budget_includes_a_slow_probe_and_leaves_nothing_running(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, monkeypatch)
        original = slow_probe(database, monkeypatch)
        started = time.monotonic()
        fields = by_name(await datasets._get_dataset_field_stats_impl(1, SELECTIVE))
        elapsed = time.monotonic() - started
        # The shared budget (0.9 s) includes the probe; before, the request waited out the probe's own 2 s.
        assert elapsed < 1.6, elapsed
        # Breadth undecided: every filter-sensitive field fills in when opened.
        assert {name: fields[name]['provenance'] for name in FACETS} == dict.fromkeys(FACETS, 'pending')
        # The cut-off probe was cancelled and its connection returned: no query still runs and
        # the whole pool is idle, ready for the next request.
        assert database.pool.get_idle_size() == database.pool.get_size()
        running = await original(
            "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND state <> 'idle' "
            "AND query LIKE '%slow_probe%'",
        )
        assert running == 0
        score = await opened(SELECTIVE, 'score')  # promoted MIN/MAX needs no probe
        assert (score['min'], score['max'], score['provenance']) == (18, 20, 'exact')

    registry_database(scenario)


def test_probe_out_of_its_own_budget_still_means_broad(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, monkeypatch)
        slow_probe(database, monkeypatch)
        monkeypatch.setattr(datasets, '_LIGHTNING_PROBE_BUDGET_S', 0.3)
        # The probe's own budget ran out before the shared one: broad, as always.
        fields = by_name(await datasets._get_dataset_field_stats_impl(1, SELECTIVE))
        assert (fields['brightness']['provenance'], fields['score']['provenance']) == ('cached', 'pending')
        # An opened field runs the probe with its own budget: unpromoted MIN/MAX stays cached.
        brightness = await opened(SELECTIVE, 'brightness')
        assert (brightness['min'], brightness['max'], brightness['provenance']) == (10, 200, 'cached')

    registry_database(scenario)


@pytest.mark.parametrize(
    'field,replacement,expected',
    [
        # Out of the per-field budget: the cached range, labelled as such.
        ('brightness', 'SELECT NULL::float AS min_val, NULL::float AS max_val FROM pg_sleep(5)', 'out_of_budget'),
        # A failed query also keeps the cached range, but retrying will not help.
        ('brightness', 'SELECT 1 / 0 AS min_val, 1 AS max_val', 'cached'),
    ],
)
def test_opened_field_tells_timeout_from_failure(
    registry_database,  # noqa: F811
    monkeypatch,
    field,
    replacement,
    expected,
):
    async def scenario(database):
        await seed(database, monkeypatch)
        monkeypatch.setattr(datasets, '_LIGHTNING_FIELD_STATS_BUDGET_S', 0.3)
        original = database.fetchrow

        async def fetchrow(sql, *args):
            if f"MIN((metadata->>'{field}')" in sql:
                return await original(replacement)
            return await original(sql, *args)

        monkeypatch.setattr(database, 'fetchrow', fetchrow)
        entry = await opened(SELECTIVE, field)
        assert (entry['min'], entry['max'], entry['provenance']) == (10, 200, expected)
        assert database.pool.get_idle_size() == database.pool.get_size()

    registry_database(scenario)


def test_opened_field_with_no_filtered_values(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, monkeypatch)
        # No value in the set: a range keeps the dataset's bounds (so it is not exact) ...
        brightness = await opened(BARE, 'brightness')
        assert (brightness['min'], brightness['max'], brightness['provenance']) == (10, 200, 'cached')
        # ... while an empty value list is the exact answer.
        color = await opened(BARE, 'color')
        assert (color['values'], color['provenance']) == ([], 'exact')

    registry_database(scenario)


def test_promoted_value_lists_stay_complete_when_the_probe_is_slow(registry_database, monkeypatch):  # noqa: F811
    """The probe spending the shared budget must not starve promoted fields' complete lists."""

    async def scenario(database):
        await seed(database, monkeypatch)
        # The bounded recent-rows cache, as on large datasets (8 of 10 countries, values_complete false).
        await database.execute(
            "UPDATE dataset_field_stats SET unique_values = ARRAY['early'], values_complete = false "
            "WHERE dataset_id = 1 AND field_name = 'batch'",
        )
        slow_probe(database, monkeypatch)
        fields = by_name(await datasets._get_dataset_field_stats_impl(1, SELECTIVE))
        batch = fields['batch']
        assert (batch['values'], batch['values_complete'], batch['provenance']) == (
            ['bare', 'early', 'late'],
            True,
            'pending',
        )

    registry_database(scenario)


def test_promoted_enum_stays_complete_when_the_probe_is_slow(registry_database, monkeypatch):  # noqa: F811
    """Reproduce the sidebar expansion regression."""
    countries = [f'country-{i}' for i in range(10)]

    async def scenario(database):
        monkeypatch.setattr(datasets, '_LIGHTNING_FACET_MAX_ROWS', 5)
        samples = [
            {'filepath': f's{n}.jpg', 'metadata': {'country': countries[n % 10], 'batch': 'late' if n > 17 else 'x'}}
            for n in range(1, 31)
        ]
        await datasets.add_samples(1, {'samples': samples}, user=USER)
        await database.execute(
            "UPDATE dataset_fields SET is_promoted = true WHERE dataset_id = 1 AND field_name = 'country'",
        )
        await run_refresh(database, 1)
        await database.execute(
            "UPDATE dataset_field_stats SET unique_values = $1::text[], values_complete = false "
            "WHERE dataset_id = 1 AND field_name = 'country'",
            countries[:8],
        )
        slow_probe(database, monkeypatch)
        fields = by_name(await datasets._get_dataset_field_stats_impl(1, SELECTIVE))
        assert sorted(fields['country']['values']) == countries, fields['country']
        assert fields['country']['values_complete'] is True

    registry_database(scenario)


def test_default_view_of_an_object_dataset_shows_image_grain_values(registry_database, monkeypatch):  # noqa: F811
    """With no view stage the sidebar filters by grain only; it still computes the
    image grain's facets instead of serving the dataset-wide (images + objects) cache."""

    async def scenario(database):
        images = [
            {'filepath': f'image-{n}.jpg', 'tags': ['validation'], 'metadata': {'split': 'val', 'width': 100 + n}}
            for n in range(4)
        ]
        objects = [
            {
                'filepath': f'object-{n}.jpg',
                'media_type': 'object',
                'tags': [f'class-{n}'],
                'metadata': {'split': f'object-{n}', 'width': 1000 + n},
            }
            for n in range(6)
        ]
        await datasets.add_samples(1, {'samples': images + objects}, user=USER)
        await run_refresh(database, 1)
        default_view = {'logic': 'AND', 'filters': [IMAGE]}
        fields = by_name(await datasets._get_dataset_field_stats_impl(1, default_view))
        assert (fields['tags']['values'], fields['tags']['provenance']) == (['validation'], 'exact')
        assert (fields['split']['values'], fields['split']['provenance']) == (['val'], 'exact')
        assert (fields['width']['min'], fields['width']['max'], fields['width']['provenance']) == (100, 103, 'exact')

    registry_database(scenario)


def test_a_disconnected_client_cancels_the_facet_queries(registry_database, monkeypatch):  # noqa: F811
    """A superseded or closed opened-field request stops querying once its client is gone,
    through the real app and its HTTP middleware (which hides disconnects from is_disconnected())."""
    from backend.access import require_dataset_access
    from backend.main import app

    async def scenario(database):
        await seed(database, monkeypatch)
        monkeypatch.setitem(app.dependency_overrides, require_dataset_access, lambda: None)
        original = database.fetchrow

        async def fetchrow(sql, *args):
            if "MIN((metadata->>'brightness')" in sql:
                return await original('SELECT NULL::float AS min_val, NULL::float AS max_val FROM pg_sleep(5) gone')
            return await original(sql, *args)

        monkeypatch.setattr(database, 'fetchrow', fetchrow)

        async def post(body, gone_after):
            """One raw ASGI request whose client disconnects `gone_after` seconds after sending it."""
            messages = [{'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}]
            sent = []

            async def receive():
                if messages:
                    return messages.pop(0)
                if gone_after is None:
                    await asyncio.Event().wait()
                await asyncio.sleep(gone_after)
                return {'type': 'http.disconnect'}

            async def send(message):
                sent.append(message)

            path = '/api/datasets/1/field-stats'
            scope = {
                'type': 'http',
                'asgi': {'version': '3.0'},
                'http_version': '1.1',
                'method': 'POST',
                'scheme': 'http',
                'path': path,
                'raw_path': path.encode(),
                'query_string': b'',
                'root_path': '',
                'headers': [(b'content-type', b'application/json'), (b'host', b'test')],
                'client': ('127.0.0.1', 1),
                'server': ('test', 80),
            }
            await app(scope, receive, send)
            status = next(m['status'] for m in sent if m['type'] == 'http.response.start')
            payload = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
            return status, payload

        body = {'filters': SELECTIVE, 'lightning_mode': True, 'fields': ['brightness']}
        started = time.monotonic()
        status, _ = await post(body, 0.3)
        elapsed = time.monotonic() - started
        assert status == 499
        assert elapsed < 1.5, elapsed  # not the 5 s per-field budget
        # The cancellation was awaited: nothing still runs and every connection is back.
        assert database.pool.get_idle_size() == database.pool.get_size()
        running = await database.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND state <> 'idle' "
            "AND query LIKE '%pg_sleep(5) gone%'",
        )
        assert running == 0
        # A client that stays gets its answer.
        monkeypatch.setattr(database, 'fetchrow', original)
        status, payload = await post(body, None)
        [entry] = json.loads(payload)
        assert (status, entry['min'], entry['max'], entry['provenance']) == (200, 180, 200, 'exact')

    registry_database(scenario)


def test_probe_budget_times_the_count_only(registry_database, monkeypatch):  # noqa: F811
    """As before, the probe's own budget times its count query, not its helpers."""

    async def scenario(database):
        await seed(database, monkeypatch)
        monkeypatch.setattr(datasets, '_LIGHTNING_PROBE_BUDGET_S', 0.5)
        original = datasets.get_promoted_numeric_fields

        async def slow_helper(dataset_id):
            await asyncio.sleep(0.6)
            return await original(dataset_id)

        monkeypatch.setattr(datasets, 'get_promoted_numeric_fields', slow_helper)
        brightness = await opened(SELECTIVE, 'brightness')
        assert (brightness['min'], brightness['max'], brightness['provenance']) == (180, 200, 'exact')

    registry_database(scenario)


@pytest.mark.parametrize('lightning_mode', [True, False])
def test_malformed_filter_is_a_400_as_on_main(registry_database, lightning_mode):  # noqa: F811
    # The grain check raised AttributeError on a non-object filter, a 500 even with
    # lightning off; main's filter parser answers 400.
    from fastapi import HTTPException

    async def scenario(database):
        malformed = {'logic': 'AND', 'filters': ['x']}
        assert datasets._grain_predicate_only(malformed) is False
        with pytest.raises(HTTPException) as error:
            await datasets._get_dataset_field_stats_impl(1, malformed, lightning_mode=lightning_mode)
        assert error.value.status_code == 400

    registry_database(scenario)
