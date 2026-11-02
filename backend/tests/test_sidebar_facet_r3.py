"""Facet requests: database-wide slots shared by every worker,
a stream that always ends with every field answered, NaN/Infinity bounds, a field
error's text in production, unsafe names and a malformed filter.
"""

import asyncio
import json
import logging
import os
import time

import asyncpg
import pytest
from fastapi import HTTPException

from backend.routers import datasets
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, registry_database  # noqa: F401
from backend.tests.test_sidebar_facet_stream import allow_access, lines_of, slow_brightness, stream_post
from backend.tests.test_sidebar_on_open import SELECTIVE, by_name, client, seed

IMAGE = {'logic': 'AND', 'filters': [{'field': 'media_type', 'operator': '!=', 'value': 'object'}]}


async def worker_pool():
    """A second worker's pool on the same database."""
    return await asyncpg.create_pool(os.environ['FRIDGE_TEST_POSTGRES_DSN'], min_size=1, max_size=5)


def test_the_slot_count_is_the_databases_own(registry_database):  # noqa: F811
    async def scenario(database):
        async with database.pool.acquire() as connection:
            configured = int(
                await connection.fetchval("SELECT setting FROM pg_settings WHERE name = 'max_parallel_workers'"),
            )
            processes = int(await connection.fetchval('SHOW max_worker_processes'))
            await connection.execute('SET max_parallel_workers = 1')  # a session's SET does not change it
            # Up to the server's process pool; with parallel query off (0), that pool.
            expected = min(configured, processes) or processes
            assert await datasets._database_slot_count(connection) == max(1, expected)
            await connection.execute('RESET max_parallel_workers')

    registry_database(scenario)


def test_database_slots_are_shared_by_every_worker(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        async def two(connection):
            return 2

        monkeypatch.setattr(datasets, '_database_slot_count', two)
        other = await worker_pool()
        try:
            first, second = datasets._DatabaseSlots(database.pool), datasets._DatabaseSlots(other)
            zero, one = await first.take(), await first.take()
            assert [zero, one, await first.take()] == [0, 1, None]
            assert zero is not None and one is not None
            assert await second.take() is None  # the other worker sees the database's slots taken
            await first.give(zero)
            again = await second.take()
            assert again is not None and again == 0
            await first.give(one)
            await second.give(again)
            assert first.connection is None and second.connection is None  # no slot held: back to the pools
            assert database.pool.get_idle_size() == database.pool.get_size()
        finally:
            await other.close()

    registry_database(scenario)


def test_workers_together_never_run_more_scans_than_the_database_slots(registry_database, monkeypatch):  # noqa: F811
    """regression: 12 slots per worker put 24 scans on the database for two users in two workers."""

    async def scenario(database):
        async def two(connection):
            return 2

        monkeypatch.setattr(datasets, '_database_slot_count', two)
        other = await worker_pool()
        state = {'running': 0, 'peak': 0}
        try:
            workers = [datasets._FacetSlots(4, datasets._DatabaseSlots(pool)) for pool in (database.pool, other)]

            async def field(worker, owner):
                async with worker.slot(owner):
                    state['running'] += 1
                    state['peak'] = max(state['peak'], state['running'])
                    await asyncio.sleep(0.1)
                    state['running'] -= 1

            requests = [asyncio.ensure_future(field(worker, owner)) for worker, owner in [(w, w) for w in workers] * 6]
            await asyncio.wait_for(asyncio.gather(*requests), 30)
            assert state['peak'] == 2  # the database's 2 slots, not 4 per worker
            assert all(worker.busy == 0 and not worker.turns for worker in workers)
        finally:
            await other.close()

    registry_database(scenario)


def test_a_promoted_range_does_not_wait_for_a_slot(registry_database, monkeypatch):  # noqa: F811
    """regression: an index read is not queued behind other fields' scans."""

    async def scenario(database):
        await seed(database, monkeypatch)
        slow_brightness(database, monkeypatch, 1.5)
        busy = asyncio.ensure_future(datasets._get_dataset_field_stats_impl(1, SELECTIVE, fields=['brightness']))
        await asyncio.sleep(0.3)  # brightness holds this worker's only slot (pool of 5)
        started = time.monotonic()
        [score] = await datasets._get_dataset_field_stats_impl(1, SELECTIVE, fields=['score'])
        assert time.monotonic() - started < 0.6
        assert (score['min'], score['max'], score['provenance']) == (18, 20, 'exact')
        await busy

    registry_database(scenario)


def test_a_stream_that_fails_after_it_started_answers_every_field_and_ends(
    registry_database,  # noqa: F811
    monkeypatch,
):
    """regression: the http middleware ended a failed stream cleanly, leaving fields waiting."""

    async def scenario(database):
        await seed(database, monkeypatch)
        allow_access(monkeypatch)
        monkeypatch.setenv('FRIDGE_ENV', 'production')

        async def fails_after_one(id, filters, lightning_mode, fields, *, known_broad=None, emit=None, report=None):
            emit({'name': 'score', 'type': 'number', 'min': 18, 'max': 20, 'provenance': 'exact'})
            await asyncio.sleep(0.05)
            raise RuntimeError('could not open "/var/lib/postgresql/secret": No space left on device')

        monkeypatch.setattr(datasets, '_get_dataset_field_stats_impl', fails_after_one)
        body = {
            'filters': SELECTIVE,
            'lightning_mode': True,
            'fields': ['score', 'brightness', 'color'],
            'stream': True,
        }
        status, _, chunks = await stream_post(body)
        entries = [entry for _, entry in lines_of(chunks)]
        assert status == 200
        assert entries[0]['name'] == 'score' and 'error' not in entries[0]
        failed = {entry['name']: entry for entry in entries[1:-1]}
        assert sorted(failed) == ['brightness', 'color']
        for entry in failed.values():
            assert entry['error'] == 'Could not compute the filtered values' and entry['error_id']
        assert entries[-1] == {'done': True}
        assert 'secret' not in json.dumps(entries)

    registry_database(scenario)


async def seed_nan(database):
    samples = [{'filepath': f'n-{n}.jpg', 'metadata': {'score': n / 2, 'level': n}} for n in range(1, 41)]
    await datasets.add_samples(1, {'samples': samples}, user=USER)
    await run_refresh(database, 1)
    odd = [{'filepath': f'odd-{text}.jpg', 'metadata': {'score': text, 'level': 41}} for text in ('NaN', 'Infinity')]
    await datasets.add_samples(1, {'samples': odd}, user=USER)
    await run_refresh(database, 1)


def test_nan_text_in_a_number_field_never_breaks_the_answer(registry_database, monkeypatch):  # noqa: F811
    """regression: the text 'NaN' in a number field made a non-JSON line and, on main too, a 500
    for a small dataset's default view and lightning-off."""

    async def scenario(database):
        await seed_nan(database)
        for lightning_mode in (True, False):
            fields = by_name(await datasets._get_dataset_field_stats_impl(1, IMAGE, lightning_mode=lightning_mode))
            json.dumps(fields, allow_nan=False)
            assert fields['score']['type'] == 'number'
            assert (fields['score']['min'], fields['score']['max']) == (0.5, 20.0)
        allow_access(monkeypatch)
        stage = {'logic': 'AND', 'filters': [{'field': 'level', 'operator': '>=', 'value': 5}]}
        body = {'filters': stage, 'lightning_mode': True, 'fields': ['score', 'level'], 'stream': True}
        status, _, chunks = await stream_post(body)
        entries = {entry.get('name'): entry for _, entry in lines_of(chunks)}  # every line is JSON
        assert status == 200 and (entries['score']['min'], entries['score']['max']) == (2.5, 20.0)
        assert datasets._finite_bounds({'min': float('nan'), 'max': float('inf')}) == {'min': None, 'max': None}

    registry_database(scenario)


def test_a_malformed_filter_is_a_400_for_a_facet_request(registry_database, monkeypatch):  # noqa: F811
    """regression: the JSON facet request answered 200 with an error on every field."""

    async def scenario(database):
        await seed(database, monkeypatch)
        bogus = {'logic': 'AND', 'filters': [{'field': 'batch', 'operator': 'bogus_op', 'value': 'late'}]}
        for stream in (False, True):
            with pytest.raises(HTTPException) as error:
                await datasets.get_dataset_field_stats_post(
                    1,
                    datasets.FieldStatsRequest(filters=bogus, fields=['score', 'color'], stream=stream),
                    client(),
                )
            assert error.value.status_code == 400

    registry_database(scenario)


def test_a_field_error_keeps_its_text_out_of_production_answers(registry_database, monkeypatch, caplog):  # noqa: F811
    """regression, as the app's unhandled_exception_handler: the text is logged, not answered."""

    async def scenario(database):
        await seed(database, monkeypatch)
        monkeypatch.setenv('FRIDGE_ENV', 'production')

        async def broken(dataset_id, field_name, filters):
            raise RuntimeError('could not open "/var/lib/postgresql/secret": No space left on device')

        monkeypatch.setattr(datasets, 'compute_filtered_minmax', broken)
        with caplog.at_level(logging.ERROR, logger='fridge.errors'):
            [brightness] = await datasets._get_dataset_field_stats_impl(1, SELECTIVE, fields=['brightness'])
        assert brightness['provenance'] == 'cached'
        assert brightness['error'] == 'Could not compute the filtered values'
        assert brightness['error_id'] in caplog.text and 'secret' in caplog.text  # the log keeps the details
        assert 'secret' not in json.dumps(brightness)

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


def test_an_unsafe_name_is_never_pending_and_answers_without_an_error(registry_database, monkeypatch):  # noqa: F811
    """regression: `plant-id` was `pending`, then failed after every filter change, with a Retry."""

    async def scenario(database):
        await seed_unsafe(database)
        monkeypatch.setattr(datasets, '_LIGHTNING_FACET_MAX_ROWS', 5)
        stage = {'logic': 'AND', 'filters': [{'field': 'batch', 'operator': '=', 'value': 'late'}]}
        sidebar = by_name(await datasets._get_dataset_field_stats_impl(1, stage))
        assert (sidebar['plant-id']['provenance'], sidebar['plant-id']['filterable']) == ('cached', False)
        assert sidebar['brightness']['provenance'] == 'pending' and 'filterable' not in sidebar['brightness']
        [entry] = await datasets._get_dataset_field_stats_impl(1, stage, fields=['plant-id'])
        assert (entry['provenance'], entry['filterable'], entry['min'], entry['max']) == ('cached', False, 1, 20)
        assert 'error' not in entry

    registry_database(scenario)
