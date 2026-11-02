"""Facet requests: the worker's shared facet slots taken in turn,
one streamed line per field, one field's failure as that field's answer,
and the sidebar request's breadth sparing the facet request a second probe.
"""

import asyncio
import json
import time
from typing import Any, Dict

import pytest

from backend.routers import datasets
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, registry_database  # noqa: F401
from backend.tests.test_sidebar_facet_requests import FIELDS
from backend.tests.test_sidebar_facet_requests import SELECTIVE as SELECTIVE_LATE
from backend.tests.test_sidebar_facet_requests import seed as seed_numbers
from backend.tests.test_sidebar_on_open import PROBE, SELECTIVE, by_name, client, seed


async def stream_post(body, gone_after=None):
    """One raw ASGI request through the real app and its middleware; returns the status, the headers
    and each body chunk with the time it arrived. The client disconnects `gone_after` seconds in."""
    from backend.main import app

    messages = [{'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}]
    sent = []
    started = time.monotonic()

    async def receive():
        if messages:
            return messages.pop(0)
        if gone_after is None:
            await asyncio.Event().wait()
        await asyncio.sleep(gone_after)
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append((time.monotonic() - started, message))

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
    start = next(m for _, m in sent if m['type'] == 'http.response.start')
    headers = {k.decode(): v.decode() for k, v in start['headers']}
    chunks = [(t, m.get('body', b'')) for t, m in sent if m['type'] == 'http.response.body' and m.get('body')]
    return start['status'], headers, chunks


def lines_of(chunks):
    """Each streamed line as (arrival time, entry)."""
    out = []
    for at, chunk in chunks:
        for line in chunk.decode().splitlines():
            if line.strip():
                out.append((at, json.loads(line)))
    return out


def slow_brightness(database, monkeypatch, seconds):
    original = database.fetchrow

    async def fetchrow(sql, *args):
        if "MIN((metadata->>'brightness')" in sql:
            await original(f'SELECT pg_sleep({seconds}) AS slow_brightness')
        return await original(sql, *args)

    monkeypatch.setattr(database, 'fetchrow', fetchrow)


def count_probes(database, monkeypatch, seconds=0.0):
    """Count the selectivity probes; each takes `seconds` longer than it would."""
    probes = []
    original = database.fetchval

    async def fetchval(sql, *args):
        if PROBE in sql:
            probes.append(sql)
            if seconds:
                await original(f'SELECT pg_sleep({seconds})')
        return await original(sql, *args)

    monkeypatch.setattr(database, 'fetchval', fetchval)
    return probes


def allow_access(monkeypatch):
    from backend.access import require_dataset_access
    from backend.main import app

    monkeypatch.setitem(app.dependency_overrides, require_dataset_access, lambda: None)


class FreeDatabase:
    """A database with a slot free whenever one is asked for (the turn-taking alone is tested)."""

    wanting = False

    async def take(self, requests=1):
        return 0

    async def give(self, key):
        pass

    async def keep(self, key, requests=1):
        return True

    async def want(self, wanting):
        pass


def test_facet_slots_are_taken_in_turn_and_a_cancelled_field_gives_its_place_up():
    async def scenario():
        slots = datasets._FacetSlots(1, FreeDatabase())
        order = []

        async def field(owner, name, hold=0.01):
            async with slots.slot(owner):
                order.append(name)
                await asyncio.sleep(hold)

        first = asyncio.ensure_future(field('A', 'A1', 0.05))
        await asyncio.sleep(0.01)
        queued = [
            asyncio.ensure_future(field(owner, name))
            for owner, name in [('A', 'A2'), ('A', 'A3'), ('A', 'A4'), ('B', 'B1'), ('B', 'B2')]
        ]
        dropped = asyncio.ensure_future(field('C', 'C1'))
        await asyncio.sleep(0.01)
        dropped.cancel()
        await asyncio.gather(first, *queued, dropped, return_exceptions=True)
        # B's fields do not wait behind all of A's: the requests take turns.
        assert order == ['A1', 'A2', 'B1', 'A3', 'B2', 'A4']
        assert slots.busy == 0 and not slots.waiting and not slots.turns

    asyncio.run(scenario())


def test_facet_requests_share_the_worker_slots(registry_database, monkeypatch):  # noqa: F811
    """regression: one limit per worker, not per request; a small request is not stuck behind a
    big one."""

    async def scenario(database):
        await seed_numbers(database, monkeypatch)
        state: Dict[str, Any] = {'running': 0, 'peak': 0, 'done': {}}

        async def minmax(dataset_id, field_name, filters):
            state['running'] += 1
            state['peak'] = max(state['peak'], state['running'])
            try:
                await database.fetchval('SELECT pg_sleep(0.15)')
            finally:
                state['running'] -= 1
            return {'min': 1, 'max': 2}

        monkeypatch.setattr(datasets, 'compute_filtered_minmax', minmax)
        capacity = max(1, database.pool.get_max_size() // 4)

        async def request(label, names):
            await datasets._get_dataset_field_stats_impl(1, SELECTIVE_LATE, fields=names)
            state['done'][label] = time.monotonic()

        started = time.monotonic()
        big = asyncio.ensure_future(request('big', FIELDS))  # 12 fields
        await asyncio.sleep(0.05)
        small = asyncio.ensure_future(request('small', ['n00']))
        await asyncio.gather(big, small)
        assert state['peak'] == capacity  # both requests together never exceed the worker's slots
        # The small request takes its turn after one of the big one's fields, not after all eleven.
        assert state['done']['small'] - started < 4 * 0.15 + 0.1 < state['done']['big'] - started, state['done']

    registry_database(scenario)


def test_a_streamed_facet_request_answers_each_field_when_it_is_ready(registry_database, monkeypatch):  # noqa: F811
    """regression: a promoted range does not wait for the probe or a slow field."""

    async def scenario(database):
        await seed(database, monkeypatch)
        allow_access(monkeypatch)
        probes = count_probes(database, monkeypatch, seconds=0.6)
        slow_brightness(database, monkeypatch, 0.6)
        body = {
            'filters': SELECTIVE,
            'lightning_mode': True,
            'fields': ['score', 'brightness', 'color'],
            'stream': True,
        }
        status, headers, chunks = await stream_post(body)
        assert status == 200 and headers['content-type'].startswith('application/x-ndjson')
        assert 'no-transform' in headers['cache-control']
        lines = lines_of(chunks)
        assert lines[-1][1] == {'done': True}  # a complete answer says so
        arrived = {entry['name']: at for at, entry in lines[:-1]}
        entries = {entry['name']: entry for _, entry in lines[:-1]}
        assert sorted(arrived) == ['brightness', 'color', 'score']
        assert (entries['score']['min'], entries['score']['max'], entries['score']['provenance']) == (18, 20, 'exact')
        assert (entries['brightness']['min'], entries['brightness']['max'], entries['brightness']['provenance']) == (
            180,
            200,
            'exact',
        )
        assert entries['color']['provenance'] == 'exact'
        # The promoted range needs no probe: it is first, long before the probe (0.6 s) is done.
        assert arrived['score'] < 0.4 < min(arrived['color'], arrived['brightness']), arrived
        assert len(probes) == 1

    registry_database(scenario)


def test_leaving_a_facet_stream_stops_its_queries(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, monkeypatch)
        allow_access(monkeypatch)
        slow_brightness(database, monkeypatch, 5)
        body = {'filters': SELECTIVE, 'lightning_mode': True, 'fields': ['score', 'brightness'], 'stream': True}
        started = time.monotonic()
        status, headers, chunks = await stream_post(body, gone_after=0.4)
        assert status == 200 and headers['content-type'].startswith('application/x-ndjson')
        assert [entry.get('name') for _, entry in lines_of(chunks)] == ['score']  # no done line: it was left
        assert time.monotonic() - started < 1.5  # not the 5 s query
        assert database.pool.get_idle_size() == database.pool.get_size()
        running = await database.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND state <> 'idle' "
            "AND query LIKE '%slow_brightness%'",
        )
        assert running == 0

    registry_database(scenario)


async def seed_unsafe(database):
    samples = [
        {
            'filepath': f'u-{n}.jpg',
            'metadata': {'batch': 'late' if n > 17 else 'early', 'brightness': n * 10, 'plant-id': n},
        }
        for n in range(1, 21)
    ]
    await datasets.add_samples(1, {'samples': samples}, user=USER)
    await run_refresh(database, 1)


def test_one_field_that_cannot_be_computed_fails_alone(registry_database, monkeypatch):  # noqa: F811
    """regression: a field that fails answers its own error; the rest of the batch loads. (An
    unsafe name such as `plant-id` keeps its dataset values without an error after validation.)"""

    async def scenario(database):
        await seed_unsafe(database)
        monkeypatch.setattr(datasets, '_LIGHTNING_FACET_MAX_ROWS', 5)
        original = datasets.compute_filtered_minmax

        async def brightness_fails(dataset_id, field_name, filters):
            if field_name == 'brightness':
                raise RuntimeError('brightness is broken')
            return await original(dataset_id, field_name, filters)

        monkeypatch.setattr(datasets, 'compute_filtered_minmax', brightness_fails)
        stage = {'logic': 'AND', 'filters': [{'field': 'batch', 'operator': '=', 'value': 'late'}]}
        fields = by_name(
            await datasets._get_dataset_field_stats_impl(1, stage, fields=['brightness', 'batch', 'plant-id']),
        )
        assert (fields['brightness']['min'], fields['brightness']['max'], fields['brightness']['provenance']) == (
            10,
            200,
            'cached',
        )
        assert 'brightness is broken' in fields['brightness']['error']
        assert (fields['batch']['values'], fields['batch']['provenance']) == (['late'], 'exact')
        assert (fields['plant-id']['provenance'], fields['plant-id']['filterable']) == ('cached', False)
        assert 'error' not in fields['plant-id']
        assert (fields['plant-id']['min'], fields['plant-id']['max']) == (1, 20)

    registry_database(scenario)


@pytest.mark.parametrize('lightning_mode', [True, False])
def test_an_unsafe_field_name_never_empties_a_small_sidebar(
    registry_database,  # noqa: F811
    monkeypatch,
    lightning_mode,
):
    """regression (pre-existing): the default view and lightning-off compute every field of a small
    dataset; `plant-id` keeps its cached values instead of failing the request."""

    async def scenario(database):
        await seed_unsafe(database)
        image = {'logic': 'AND', 'filters': [{'field': 'media_type', 'operator': '!=', 'value': 'object'}]}
        fields = by_name(await datasets._get_dataset_field_stats_impl(1, image, lightning_mode=lightning_mode))
        assert (fields['plant-id']['min'], fields['plant-id']['max'], fields['plant-id']['provenance']) == (
            1,
            20,
            'cached',
        )
        assert fields['brightness']['provenance'] == 'exact'

    registry_database(scenario)


def test_the_sidebar_breadth_spares_the_facet_request_its_probe(registry_database, monkeypatch):  # noqa: F811
    """regression: one filter change, one probe."""

    async def scenario(database):
        await seed(database, monkeypatch)
        probes = count_probes(database, monkeypatch)
        sidebar = await datasets.get_dataset_field_stats_post(
            1,
            datasets.FieldStatsRequest(filters=SELECTIVE),
            client(),
        )
        assert sidebar.headers['X-Filter-Breadth'] == 'selective'
        assert by_name(json.loads(sidebar.body))['brightness']['provenance'] == 'pending'
        assert len(probes) == 1
        facets = await datasets.get_dataset_field_stats_post(
            1,
            datasets.FieldStatsRequest(filters=SELECTIVE, fields=['brightness', 'color'], breadth='selective'),
            client(),
        )
        assert {field['name']: field['provenance'] for field in facets} == {'brightness': 'exact', 'color': 'exact'}
        assert len(probes) == 1
        # Without the sidebar's answer the facet request decides for itself, once for all its fields.
        await datasets.get_dataset_field_stats_post(
            1,
            datasets.FieldStatsRequest(filters=SELECTIVE, fields=['brightness', 'color']),
            client(),
        )
        assert len(probes) == 2

    registry_database(scenario)
