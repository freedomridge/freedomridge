"""Field registry regressions under concurrent writes, reverts and refreshes.

Statistics publish under concurrent writes, interactive writes wait briefly instead of failing,
a revert keeps value lists as previews, overlay discovery survives unrelated writes, and the
refresh endpoint says why it did not publish.
"""

import asyncio
import os
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.field_registry import cached_distinct_values, lock_field_lifecycle
from backend.routers import datasets, versions
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import refresh_during, run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest
from backend.tests.test_registry_rereview_refresh import settle

registry_database = test_field_registry_postgres.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)

BEFORE_MEASUREMENT = 'before_measurement'
AFTER_MEASUREMENT = 'after_measurement'


async def refresh_while(database, during, marker=AFTER_MEASUREMENT):
    return await refresh_during(database, during, phase=marker)


async def stat(database, name):
    return await database.fetchrow(
        'SELECT * FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = $1',
        name,
    )


async def schema_field(name):
    return next(field for field in await datasets.get_dataset_schema(1) if field['name'] == name)


def test_refresh_publishes_and_merges_an_ingest_made_during_measurement(registry_database):
    # Any write during measurement used to discard the whole refresh, so with a sync running the
    # statistics of a big dataset never updated.
    async def scenario(database):
        await ingest([{'num': 1, 'color': 'a'}, {'num': 2, 'color': 'b'}, {'num': 3, 'color': 'a'}])
        await run_refresh(database)

        refreshed = await refresh_while(database, lambda: ingest([{'num': 10, 'color': 'c'}], prefix='during'))

        assert [(row['dataset_id'], row['error']) for row in refreshed] == [(1, None)]
        num = await stat(database, 'num')
        assert (num['min_value'], num['max_value']) == (1, 10)  # the measurement widened by the ingest's merge
        color = await stat(database, 'color')
        assert sorted(color['unique_values']) == ['a', 'b', 'c'] and color['values_complete']
        # The new sample is past the published watermark: still stale, and counted exactly once next run.
        assert await database.fetchval('SELECT stats_need_refresh FROM datasets WHERE id = 1')
        await run_refresh(database)
        assert (await stat(database, 'num'))['sample_count'] == 4
        assert (await stat(database, 'color'))['sample_count'] == 4
        assert not await database.fetchval('SELECT stats_need_refresh FROM datasets WHERE id = 1')

    registry_database(scenario)


def test_refresh_never_certifies_or_resurrects_what_a_writer_changed_meanwhile(registry_database):
    async def scenario(database):
        ids = (await ingest([{'color': 'a', 'gone': 1, 'keep': 1}, {'color': 'b', 'gone': 2, 'keep': 2}]))['ids']
        await run_refresh(database)

        async def edits():
            await datasets.update_sample(1, ids[0], datasets.SampleUpdate(metadata={'color': 'new'}), USER)
            await datasets.clear_field(1, 'gone', USER)

        refreshed = await refresh_while(database, edits)

        assert [(row['dataset_id'], row['error']) for row in refreshed] == [(1, None)]
        assert await stat(database, 'gone') is None  # cleared meanwhile: not brought back
        assert not (await stat(database, 'color'))['values_complete']  # edited meanwhile: not certified
        assert (await stat(database, 'keep'))['max_value'] == 2
        assert await database.fetchval('SELECT stats_need_refresh FROM datasets WHERE id = 1')

    registry_database(scenario)


def test_interactive_exclusive_write_waits_for_an_ingest_in_flight(registry_database):
    # It used to be refused at once with a 409 whenever any write was in flight (most of the
    # time while a sync runs). It polls, so it never queues ahead of later writers either.
    async def scenario(database):
        ids = (await ingest([{'a': 1}, {'a': 2}]))['ids']
        pending = []
        try:
            async with database.pool.acquire() as writer:
                async with writer.transaction():
                    await writer.execute('SELECT pg_advisory_xact_lock_shared(20261016, 1)')  # an ingest batch
                    delete = asyncio.create_task(datasets.delete_sample(1, ids[0], USER))
                    pending.append(delete)
                    await asyncio.sleep(0.5)
                    assert not delete.done()  # still trying, not refused
            await asyncio.wait_for(delete, 5)
        finally:
            await settle(pending)
        assert await database.fetchval('SELECT count(*) FROM samples WHERE id = $1', ids[0]) == 0

    registry_database(scenario)


def test_interactive_exclusive_write_gives_up_with_a_retryable_409(registry_database):
    async def scenario(database):
        ids = (await ingest([{'a': 1}, {'a': 2}]))['ids']
        async with database.pool.acquire() as operation:
            async with operation.transaction():
                await operation.execute('SELECT pg_advisory_xact_lock_shared(20261016, 1)')  # a long write
                started = time.monotonic()
                with pytest.raises(HTTPException) as error:
                    await asyncio.wait_for(datasets.delete_sample(1, ids[0], USER), 10)
                waited = time.monotonic() - started
        assert error.value.status_code == 409 and error.value.headers == {'Retry-After': '1'}
        assert 1.5 < waited < 5
        assert await database.fetchval('SELECT count(*) FROM samples WHERE id = $1', ids[0]) == 1

    registry_database(scenario)


def test_revert_keeps_the_value_lists_of_fields_it_did_not_change(registry_database):
    # Every categorical field lost its list after any revert, not just the ones it changed.
    async def scenario(database):
        ids = (await ingest([{'cat': 'x', 'note': 'a'}, {'cat': 'y', 'note': 'b'}, {'cat': 'z', 'note': 'c'}]))['ids']
        await run_refresh(database)
        version = await versions.create_version(1, versions.CreateVersionRequest(name='before-edit'))
        await datasets.update_sample(1, ids[0], datasets.SampleUpdate(metadata={'note': 'edited'}), USER)
        await run_refresh(database)
        await versions._restore_version(1, version['id'], False, USER, False)

        cat = await schema_field('cat')  # untouched by the revert: still a complete list
        assert cat['type'] == 'enum' and cat['values'] == ['x', 'y', 'z'] and cat['values_complete'] is True
        note = await schema_field('note')  # changed by it: no stale 'edited', not certified
        assert 'edited' not in (note.get('values') or []) and not note.get('values_complete')

    registry_database(scenario)


def test_overlay_discovery_appends_new_fields_and_keeps_removals_until_statistics(registry_database):
    # Rediscovery scans every sample; it ran after every bulk edit, clear, rename and clone.
    async def scenario(database):
        ids = (await ingest([{'hm': {'type': 'heatmap', 'path': 'a.png'}, 'score': 1}, {'score': 2}]))['ids']

        async def discovered():
            await datasets.get_dataset_field_stats(1)
            rows = await database.fetch(
                'SELECT field_name, unique_values FROM dataset_field_stats WHERE dataset_id = 1 '
                "AND field_name LIKE '\\_\\_%'",
            )
            return {row['field_name']: row['unique_values'] for row in rows}

        assert (await discovered())['__heatmap_fields__'] == ['hm']
        await datasets.bulk_set_metadata(1, datasets.BulkSetRequest(sample_ids=ids, metadata={'score': 5}), USER)
        await datasets.clear_field(1, 'score', USER)
        assert '__heatmap_fields__' in await database.fetchval(
            'SELECT array_agg(field_name) FROM dataset_field_stats WHERE dataset_id = 1 '
            "AND field_name LIKE '\\_\\_%'",
        )

        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(updates=[{'sample_id': ids[1], 'metadata': {'hm2': {'type': 'heatmap'}}}]),
            USER,
        )
        assert (await stat(database, '__heatmap_fields__'))['unique_values'] == ['hm', 'hm2']
        assert sorted((await discovered())['__heatmap_fields__']) == ['hm', 'hm2']

        await datasets.clear_field(1, 'hm2', USER)
        assert (await stat(database, '__heatmap_fields__'))['unique_values'] == ['hm', 'hm2']
        fields = await datasets.get_dataset_field_stats(1)
        assert 'hm2' not in {field['name'] for field in fields}
        assert await stat(database, '__keypoint_fields__') is not None  # it did not

    registry_database(scenario)


def test_refresh_endpoint_says_a_publication_was_deferred(registry_database):
    import json

    from backend.statistics import KIND, POLICY, handler
    from backend.tests.test_job_runner import make_worker

    async def scenario(database):
        await ingest([{'a': 1}])
        worker = await make_worker(database, {KIND: handler(database.pool)}, policies={KIND: POLICY})
        async with database.pool.acquire() as operation:
            async with operation.transaction():
                await operation.execute('SELECT pg_advisory_xact_lock(20261016, 1)')
                response = await datasets.refresh_dataset_stats(1, USER, wait=False)
                job_id = json.loads(response.body)['job_id']
                await worker.run_once()
                status = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job_id)
                assert status['state'] == 'queued' and status['failures'] == 0
                assert status['contentions'] == 1
        await database.execute('UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1', job_id)
        await run_refresh(database)
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job_id) == 'succeeded'

    registry_database(scenario)


def test_an_edit_after_a_forced_full_measurement_keeps_the_next_refresh_full(registry_database):
    # The refresh used to certify itself anyway, so the next pass was incremental and a value the
    # edit moved out of range stayed out of the slider's range (and its filter) for up to 24 h.
    async def scenario(database):
        ids = (await ingest([{'num': number} for number in range(1, 6)]))['ids']
        await run_refresh(database)

        refreshed = await refresh_while(
            database,
            lambda: datasets.update_sample(1, ids[0], datasets.SampleUpdate(metadata={'num': 99999}), USER),
            AFTER_MEASUREMENT,
        )

        assert [(row['dataset_id'], row['error']) for row in refreshed] == [(1, None)]
        state = await database.fetchrow('SELECT stats_need_refresh, stats_full_refreshed_at FROM datasets WHERE id = 1')
        assert state['stats_need_refresh'] and state['stats_full_refreshed_at'] is None
        await run_refresh(database)
        assert (await stat(database, 'num'))['max_value'] == 99999

    registry_database(scenario)


def test_two_callers_adopting_the_same_array_field_do_not_block_each_other(registry_database, monkeypatch):
    # Each used to keep the shared gate while trying for the exclusive one: both gave up with 409s.
    async def scenario(database):
        await ingest([{'a': 1}])
        await datasets.create_array_field(2, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
        original = lock_field_lifecycle
        holding, both_holding = [], asyncio.Event()

        async def together(connection, dataset_id, *args, exclusive=False, **kwargs):
            await original(connection, dataset_id, *args, exclusive=exclusive, **kwargs)
            if not exclusive:  # both callers hold the shared gate at the same time
                holding.append(connection)
                if len(holding) == 2:
                    both_holding.set()
                await asyncio.wait_for(both_holding.wait(), 5)

        monkeypatch.setattr(datasets, 'lock_field_lifecycle', together)
        started = time.monotonic()
        await asyncio.gather(
            *(
                datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
                for _ in range(2)
            ),
        )
        assert time.monotonic() - started < 1.5
        assert (
            await database.fetchval(
                "SELECT origin FROM dataset_fields WHERE dataset_id = 1 AND field_name = 'reviewed'",
            )
            == 'native_array'
        )

    registry_database(scenario)


def test_an_ingest_in_progress_does_not_defer_a_refresh(registry_database):
    # Every inserted row used to rewrite the datasets row, so an open ingest held it and the refresh's
    # publication gave up.
    async def scenario(database):
        await ingest([{'a': 1}])
        await database.execute('UPDATE datasets SET stats_need_refresh = TRUE WHERE id = 1')
        async with database.pool.acquire() as writer:
            async with writer.transaction():
                await writer.execute(
                    "INSERT INTO samples (dataset_id, filepath, media_type) VALUES (1, 'in-progress.jpg', 'image')",
                )
                refreshed = await asyncio.wait_for(run_refresh(database), 10)
        assert [(row['dataset_id'], row['error']) for row in refreshed if row['dataset_id'] == 1] == [(1, None)]

    registry_database(scenario)


def test_a_momentarily_busy_gate_does_not_stop_the_sidebar_caching_its_lists(registry_database, monkeypatch):
    # The sidebar measures several value lists in parallel, and each stored its first cache row with
    # a single try of the gate another one held for a moment: a list went uncached and was measured
    # again on every read (and test_complete_empty_lists_do_not_rescan... failed intermittently).
    async def scenario(database):
        await ingest([{'detections': [{'label': 'car'}]}])
        async with database.pool.acquire() as other:
            transaction = other.transaction()
            await transaction.start()
            await other.execute('SELECT pg_advisory_xact_lock(20261016, 1)')  # another list's placeholder insert

            async def release():
                await asyncio.sleep(0.1)
                await transaction.commit()

            releasing = asyncio.create_task(release())
            await datasets._get_dataset_field_stats_impl(1, None)
            await releasing
        measure = AsyncMock(wraps=cached_distinct_values)
        monkeypatch.setattr(datasets, 'cached_distinct_values', measure)
        await datasets._get_dataset_field_stats_impl(1, None)
        assert measure.await_count == 0

    registry_database(scenario)
