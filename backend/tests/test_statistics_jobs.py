"""Committed-work statistics under edits, late commits and resumable worker execution."""

import asyncio
import os
from unittest.mock import patch

import pytest

from backend import job_queue, statistics, statistics_measure
from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest
from backend.tests.test_job_runner import make_worker

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


@pytest.mark.parametrize(
    'value,normalized',
    [
        ('2026-01-01', '2026-01-01T00:00:00.000000Z'),
        ('2026-01-01T00:30:00Z', '2026-01-01T00:30:00.000000Z'),
        ('2026-01-01T01:00:00+0200', '2025-12-31T23:00:00.000000Z'),
        ('2026-01-01T01:00:00+02:00', '2025-12-31T23:00:00.000000Z'),
        ('2026-01-01 12:30:00.123456', '2026-01-01T12:30:00.123456Z'),
        ('2026-01-01T00:00:00-05:30', '2026-01-01T05:30:00.000000Z'),
    ],
)
def test_datetime_measurement_ignores_invalid_values_and_orders_real_instants(registry_database, value, normalized):
    async def scenario(database):
        await ingest([{'captured': value}])
        await datasets.set_field_type(1, 'captured', datasets.SetFieldTypeRequest(field_type='datetime'), USER)
        await ingest([{'captured': 'not-a-date'}, {'captured': '2026-02-30'}, {'captured': None}], prefix='invalid')
        await run_refresh(database, 1)
        field = await database.fetchrow("SELECT * FROM dataset_field_stats WHERE field_name='captured'")
        assert field['field_type'] == 'datetime' and field['values_complete'] is False
        assert field['unique_values'] == [normalized, normalized]
        assert field['sample_count'] == 3
        assert field['min_value'] is None and field['max_value'] is None

    registry_database(scenario)


async def finish_job(database, job):
    worker = await make_worker(
        database,
        {statistics.KIND: statistics.handler(database.pool)},
        policies={statistics.KIND: statistics.POLICY},
    )
    for attempt in range(40):
        await asyncio.wait_for(worker.run_once(), 20)
        current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id'])
        if current['state'] in ('succeeded', 'failed', 'cancelled'):
            assert current['state'] == 'succeeded', dict(current)
            return dict(current)
        await asyncio.sleep(0.01)
    pytest.fail('Finite statistics job did not finish')


async def refresh(database, dataset_id=1, *, force=False):
    return await finish_job(database, await statistics.request_refresh(database.pool, dataset_id, force=force))


def test_statistics_cover_values_counts_edits_and_deletion_without_mutating_definitions(registry_database):
    async def scenario(database):
        sample_ids = (
            await ingest(
                [
                    {'color': 'red', 'score': 2, 'captured': '2026-01-01', 'detections': [{'label': 'car'}]},
                    {'color': 'blue', 'score': 5, 'captured': '2026-02-01', 'detections': [{'label': 'person'}]},
                ],
            )
        )['ids']
        registry = await database.fetch('SELECT * FROM dataset_fields ORDER BY field_name')
        result = await refresh(database)
        assert result['result']['requested_dataset_refreshed'] is True
        fields = {row['field_name']: row for row in await database.fetch('SELECT * FROM dataset_field_stats')}
        assert fields['score']['sample_count'] == 2
        assert (fields['score']['min_value'], fields['score']['max_value']) == (2, 5)
        assert fields['color']['values_complete'] is True
        assert fields['color']['unique_values'] == ['blue', 'red']
        assert fields['captured']['unique_values'] == ['2026-01-01T00:00:00.000000Z', '2026-02-01T00:00:00.000000Z']
        assert fields['detections.label']['unique_values'] == ['car', 'person']
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') == 0
        assert await database.fetch('SELECT * FROM dataset_fields ORDER BY field_name') == registry
        await datasets.update_sample(
            1,
            sample_ids[0],
            datasets.SampleUpdate(metadata={'score': 9, 'color': 'green'}),
            USER,
        )
        await database.execute('DELETE FROM samples WHERE id=$1', sample_ids[1])
        await refresh(database)
        fields = {row['field_name']: row for row in await database.fetch('SELECT * FROM dataset_field_stats')}
        assert fields['score']['sample_count'] == 1
        assert (fields['score']['min_value'], fields['score']['max_value']) == (9, 9)
        assert fields['color']['values_complete'] is True
        assert fields['color']['unique_values'] == ['green']
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') == 0

    registry_database(scenario)


def test_unstarted_refreshes_coalesce_a_later_finite_barrier_without_args_changes(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}])
        first = await statistics.request_refresh(database.pool, 1)
        await ingest([{'score': 2}], prefix='later')
        later = await statistics.request_refresh(database.pool, 1, force=True)
        assert later['id'] == first['id']
        assert later['args'] == first['args'] == {}
        assert later['barrier']['snapshot'] != first['barrier']['snapshot']
        assert later['barrier']['full'] is True
        assert await database.fetchval('SELECT count(*) FROM jobs') == 1
        await finish_job(database, later)
        assert await database.fetchval("SELECT sample_count FROM dataset_field_stats WHERE field_name='score'") == 2

    registry_database(scenario)


def test_late_committing_lower_sample_id_remains_pending_after_a_completed_barrier(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}])
        async with database.pool.acquire() as writer:
            async with writer.transaction():
                lower = await writer.fetchval(
                    "INSERT INTO samples(dataset_id,filepath,media_type,metadata) "
                    "VALUES (1,'uncommitted','image', '{\"score\":3}') RETURNING id",
                )
                higher = (await ingest([{'score': 5}], prefix='committed'))['ids'][0]
                assert lower < higher
                await refresh(database)
                assert (
                    await database.fetchval("SELECT sample_count FROM dataset_field_stats WHERE field_name='score'")
                    == 2
                )
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1') > 0
        jobs = await statistics.schedule_statistics(database.pool)
        mine = next(job for job in jobs if job['dataset_id'] == 1)
        await finish_job(database, mine)
        assert await database.fetchval("SELECT sample_count FROM dataset_field_stats WHERE field_name='score'") == 3
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1') == 0

    registry_database(scenario)


def test_dirty_tracking_never_locks_a_shared_marker_between_writers(registry_database):
    async def scenario(database):
        await database.execute(
            "INSERT INTO samples(id,dataset_id,filepath,media_type) "
            "VALUES (1,1,'one','image'),(2,1,'two','image'),(4097,1,'three','image'),(4098,1,'four','image')",
        )
        async with database.pool.acquire() as first, database.pool.acquire() as second:
            async with first.transaction(), second.transaction():
                await first.execute("UPDATE samples SET tags=ARRAY['first'] WHERE id=1")
                await second.execute("UPDATE samples SET tags=ARRAY['second'] WHERE id=4098")
                await asyncio.wait_for(
                    asyncio.gather(
                        first.execute("UPDATE samples SET tags=ARRAY['first'] WHERE id=4097"),
                        second.execute("UPDATE samples SET tags=ARRAY['second'] WHERE id=2"),
                    ),
                    1,
                )
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') == 6

    registry_database(scenario)


def test_transaction_coalescing_never_skips_a_new_key_column_or_dataset_move(registry_database):
    async def scenario(database):
        sample_ids = (await ingest([{'score': 1}, {'score': 2}]))['ids']
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
        await refresh(database)
        async with database.pool.acquire() as writer, writer.transaction():
            await writer.execute("UPDATE samples SET metadata=jsonb_build_object('score',3) WHERE dataset_id=1")
            await writer.execute("UPDATE samples SET metadata=metadata || '{\"score\":4}' WHERE dataset_id=1")
            await writer.execute("UPDATE samples SET metadata=metadata || '{\"later\":true}' WHERE dataset_id=1")
            await writer.execute(
                "UPDATE samples SET tags=ARRAY['tag'],media_type='video',reviewed=ARRAY['done'] WHERE dataset_id=1",
            )
            fields = await writer.fetchval('SELECT field_names FROM dataset_stats_pending WHERE block_id=0')
            assert {'score', 'later', 'tags', 'media_type', 'reviewed'} <= set(fields)
            await writer.execute('UPDATE samples SET dataset_id=2 WHERE id=$1', sample_ids[1])
            assert await writer.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE block_id=0') == 2
            await writer.execute('UPDATE samples SET id=9000 WHERE id=$1', sample_ids[0])
            assert await writer.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE block_id=2') == 1
        await run_refresh(database)
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') == 0
        assert (
            await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE dataset_id=1 AND field_name='score'",
            )
            == 1
        )

    registry_database(scenario)


@pytest.mark.parametrize('count', [150, 201])
def test_tags_annotations_and_native_arrays_keep_their_200_value_preview_contract(registry_database, count):
    async def scenario(database):
        values = [f'value-{number:03}' for number in range(count)]
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
        await datasets.add_samples(
            1,
            [
                {
                    'filepath': 'labels.jpg',
                    'tags': values,
                    'array_fields': {'reviewed': values},
                    'metadata': {'detections': [{'label': value} for value in values], '__custom_field': 'kept'},
                },
            ],
            USER,
        )
        await refresh(database)
        fields = {row['field_name']: row for row in await database.fetch('SELECT * FROM dataset_field_stats')}
        for name in ['tags', 'reviewed', 'detections.label']:
            assert fields[name]['unique_values'] == values[:200]
            assert fields[name]['values_complete'] is (count <= 200)
        assert fields['__custom_field']['unique_values'] == ['kept']
        assert fields['__custom_field']['values_complete'] is True

    registry_database(scenario)


def test_overlay_field_indexes_are_not_truncated_or_resurrected(registry_database, monkeypatch):
    async def scenario(database):
        metadata = {f'heatmap_{number:03}': {'type': 'heatmap', 'path': 'map.png'} for number in range(205)}
        await ingest([metadata])
        await refresh(database)
        assert set(
            await database.fetchval(
                "SELECT unique_values FROM dataset_field_stats WHERE field_name='__heatmap_fields__'",
            ),
        ) == set(metadata)
        await database.execute("DELETE FROM dataset_field_stats WHERE field_name='__heatmap_fields__'")
        original = statistics_measure.aggregate_field
        removed = False

        async def clear_after_read(*args):
            nonlocal removed
            measured = await original(*args)
            if measured['field_name'] == '__heatmap_fields__' and not removed:
                removed = True
                await datasets.clear_field(1, 'heatmap_000', USER)
            return measured

        monkeypatch.setattr(statistics_measure, 'aggregate_field', clear_after_read)
        await refresh(database)
        assert removed
        assert 'heatmap_000' not in await database.fetchval(
            "SELECT unique_values FROM dataset_field_stats WHERE field_name='__heatmap_fields__'",
        )
        assert not await database.fetchval("SELECT EXISTS(SELECT 1 FROM dataset_fields WHERE field_name='heatmap_000')")

    registry_database(scenario)


def test_type_correction_is_scheduled_automatically_and_indexes_do_not_invalidate_measurements(
    registry_database,
    monkeypatch,
):
    async def scenario(database):
        sample_id = (await ingest([{'quality': 'pending'}]))['ids'][0]
        await refresh(database)
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'quality': 42}), USER)
        await datasets.set_field_type(1, 'quality', datasets.SetFieldTypeRequest(field_type='number'), USER)
        submitted = await statistics.schedule_statistics(database.pool)
        await finish_job(database, next(job for job in submitted if job['dataset_id'] == 1))
        field = await database.fetchrow("SELECT * FROM dataset_field_stats WHERE field_name='quality'")
        assert field['field_type'] == 'number'
        assert (field['min_value'], field['max_value']) == (42, 42)
        assert field['last_updated'] is not None
        await database.execute("UPDATE dataset_fields SET is_promoted=TRUE WHERE field_name='quality'")
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') == 0

        async def unexpected_measurement(*args):
            pytest.fail('Changing an index must not invalidate measured blocks')

        monkeypatch.setattr(statistics_measure, 'measure_part', unexpected_measurement)
        await refresh(database)
        assert await database.fetchval("SELECT sample_count FROM dataset_field_stats WHERE field_name='quality'") == 1
        assert await database.fetchval("SELECT min_value FROM dataset_field_stats WHERE field_name='quality'") == 42

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['edit', 'clear', 'rename', 'type'])
def test_writes_between_measurement_and_publication_are_not_certified_or_resurrected(
    registry_database,
    monkeypatch,
    operation,
):
    async def scenario(database):
        sample_id = (await ingest([{'color': 'red', 'untouched': 'stable'}]))['ids'][0]
        await refresh(database)
        original = statistics_measure.measure_part
        mutated = False

        async def change_after_measurement(*args):
            nonlocal mutated
            result = await original(*args)
            if not mutated:
                mutated = True
                if operation == 'edit':
                    await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'color': 'blue'}), USER)
                elif operation == 'clear':
                    await datasets.clear_field(1, 'color', user=USER)
                elif operation == 'rename':
                    await datasets.rename_field(1, 'color', datasets.FieldRenameRequest(new_name='shade'), USER)
                else:
                    await datasets.set_field_type(1, 'color', datasets.SetFieldTypeRequest(field_type='number'), USER)
            return result

        monkeypatch.setattr(statistics_measure, 'measure_part', change_after_measurement)
        await refresh(database, force=True)
        cached = await database.fetchrow("SELECT * FROM dataset_field_stats WHERE field_name='color'")
        if operation == 'edit':
            assert cached is not None and cached['values_complete'] is False
            assert (
                await database.fetchval(
                    "SELECT values_complete FROM dataset_field_stats WHERE field_name='untouched'",
                )
                is True
            )
        elif operation in ('clear', 'rename'):
            assert cached is None
            assert not await database.fetchval("SELECT EXISTS(SELECT 1 FROM dataset_fields WHERE field_name='color')")
        else:
            assert cached is None or cached['field_type'] == 'number'
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1') > 0
        last_full = await database.fetchval('SELECT stats_full_refreshed_at FROM datasets WHERE id=1')
        assert last_full is None
        monkeypatch.setattr(statistics_measure, 'measure_part', original)
        await refresh(database)
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1') == 0
        if operation == 'edit':
            assert await database.fetchval(
                "SELECT unique_values FROM dataset_field_stats WHERE field_name='color'",
            ) == ['blue']

    registry_database(scenario)


def test_cache_refill_is_not_mistaken_for_an_unmeasured_dataset_write(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'color': 'red'}, {'color': 'blue'}])
        await refresh(database)
        original = statistics_measure.aggregate_field

        async def refill_after_measurement(*args):
            field = await original(*args)
            if field['field_name'] == 'color':
                await database.execute(
                    "UPDATE dataset_field_stats SET last_updated=clock_timestamp() WHERE field_name='color'",
                )
            return field

        monkeypatch.setattr(statistics_measure, 'aggregate_field', refill_after_measurement)
        await refresh(database)
        assert await database.fetchval("SELECT values_complete FROM dataset_field_stats WHERE field_name='color'")
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending)')

    registry_database(scenario)


def test_subdivision_preserves_late_work_until_every_leaf_has_observed_it(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'score': 1}])
        await database.execute(
            "INSERT INTO samples(id,dataset_id,filepath,media_type,metadata) "
            "VALUES (3000,1,'upper','image','{\"score\":2}')",
        )
        original = statistics_measure.measure_part
        calls = []
        inserted = False

        async def heavy_part(pool, dataset_id, block_id, part_id, split_level):
            nonlocal inserted
            calls.append((block_id, part_id, split_level))
            if split_level == 0:
                raise statistics_measure.MeasurementTooLarge('Artificially heavy block')
            result = await original(pool, dataset_id, block_id, part_id, split_level)
            if part_id == 0 and not inserted:
                inserted = True
                await database.execute(
                    "INSERT INTO samples(id,dataset_id,filepath,media_type,metadata) "
                    "VALUES (5,1,'late-lower','image','{\"score\":3}')",
                )
            return result

        monkeypatch.setattr(statistics_measure, 'measure_part', heavy_part)
        await refresh(database)
        assert calls == [(0, 0, 0), (0, 0, 1), (0, 1, 1)]
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_parts WHERE dataset_id=1') == 2
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1') > 0
        assert await database.fetchval("SELECT sample_count FROM dataset_field_stats WHERE field_name='score'") == 2
        await refresh(database)
        assert calls[-2:] == [(0, 0, 1), (0, 1, 1)]
        assert await database.fetchval("SELECT sample_count FROM dataset_field_stats WHERE field_name='score'") == 3
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1') == 0

    registry_database(scenario)


def test_a_refresh_finishes_its_captured_work_while_every_unit_adds_new_samples(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'score': 1}])
        original = statistics.handler(database.pool)
        arrivals = 0

        async def streaming(job):
            nonlocal arrivals
            result = await original(job)
            arrivals += 1
            await database.execute(
                "INSERT INTO samples(id,dataset_id,filepath,media_type,metadata) "
                "VALUES ($1,1,$2,'image','{\"score\":2}')",
                (arrivals + 1) * statistics_measure.BLOCK_SIZE,
                f'streaming-{arrivals}',
            )
            return result

        submitted = await statistics.request_refresh(database.pool, 1)
        worker = await make_worker(
            database,
            {statistics.KIND: streaming},
            policies={statistics.KIND: statistics.POLICY},
        )
        await asyncio.wait_for(worker.run_once(), 5)
        state = await database.fetchrow('SELECT state,result FROM jobs WHERE id=$1', submitted['id'])
        assert state['state'] == 'succeeded', dict(state)
        assert 4 <= arrivals < 20
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id=1') == arrivals + 1
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1') >= arrivals
        assert state['result']['requested_dataset_refreshed'] is True

    registry_database(scenario)


def test_failed_aggregate_keeps_committed_blocks_instead_of_rebuilding(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'score': 1}])
        submitted = await statistics.request_refresh(database.pool, 1)
        original = statistics_measure.aggregate_field

        async def unavailable(*args):
            raise ConnectionResetError('Temporary failure after block checkpoints')

        monkeypatch.setattr(statistics_measure, 'aggregate_field', unavailable)
        worker = await make_worker(database, {statistics.KIND: statistics.handler(database.pool)})
        await asyncio.wait_for(worker.run_once(), 5)
        row = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', submitted['id'])
        assert row['state'] == 'queued' and row['checkpoint']['phase'] == 'fields'
        before = await database.fetch('SELECT * FROM dataset_block_field_stats ORDER BY field_name')
        assert before
        await database.execute('UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1', submitted['id'])
        monkeypatch.setattr(statistics_measure, 'aggregate_field', original)
        await finish_job(database, submitted)
        assert await database.fetch('SELECT * FROM dataset_block_field_stats ORDER BY field_name') == before

    registry_database(scenario)


def test_repeated_lock_contention_backs_off_without_spending_the_failure_budget(registry_database):
    async def scenario(database):
        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        delays = []
        for attempt in range(4):
            claimed = await job_queue.claim_job(database.pool, 'review', kinds=['example'])
            assert claimed is not None
            deferred = await job_queue.defer_job(
                database.pool,
                submitted['id'],
                claimed['lease_token'],
                {'type': 'QueueContended'},
                delay_seconds=2,
                count_failure=False,
            )
            assert deferred['failures'] == 0
            delays.append(round((deferred['available_at'] - deferred['updated_at']).total_seconds()))
            await database.execute('UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1', submitted['id'])
        assert delays == [2, 4, 8, 16]
        claimed = await job_queue.claim_job(database.pool, 'review', kinds=['example'])
        assert claimed is not None
        progress = await job_queue.publish_step(
            database.pool,
            submitted['id'],
            claimed['lease_token'],
            checkpoint={'done': 1},
            release=True,
        )
        assert progress['contentions'] == 0

    registry_database(scenario)


def test_aggregation_is_paged_and_retries_do_not_double_count_parts(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'score': 1, 'color': 'red'}])
        await database.execute(
            """INSERT INTO samples(id,dataset_id,filepath,media_type,metadata)
               SELECT 4096*number,1,'part-' || number,'image',jsonb_build_object('score',number,'color','blue')
               FROM generate_series(1,9) number""",
        )
        original = statistics_measure.aggregate_field
        visits = []

        async def paged(pool, dataset_id, name, partial=None, limit=128):
            result = await original(pool, dataset_id, name, partial, 2)
            visits.append((name, result['after_part']))
            return result

        monkeypatch.setattr(statistics_measure, 'aggregate_field', paged)
        await run_refresh(database, 1)
        score = await database.fetchrow("SELECT * FROM dataset_field_stats WHERE field_name='score'")
        assert score['sample_count'] == 10 and (score['min_value'], score['max_value']) == (1, 9)
        assert sum(name == 'score' and cursor is not None for name, cursor in visits) == 4
        assert await database.fetchval(
            "SELECT unique_values FROM dataset_field_stats WHERE field_name='color'",
        ) == ['blue', 'red']

    registry_database(scenario)


def test_cancelled_partial_force_refresh_keeps_unfinished_work_durable(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'score': 1}, {'score': 2}])
        await refresh(database)
        await database.execute('UPDATE dataset_stats_blocks SET split_level=1')
        submitted = await statistics.request_refresh(database.pool, 1, force=True)
        worker = await make_worker(
            database,
            {statistics.KIND: statistics.handler(database.pool)},
            policies={statistics.KIND: statistics.POLICY},
            step_budget_seconds=0.001,
        )
        for attempt in range(20):
            await worker.run_once()
            current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', submitted['id'])
            if current['checkpoint'].get('part') == 1:
                break
        assert current['checkpoint']['part'] == 1
        await job_queue.cancel_job(database.pool, submitted['id'])
        await worker.run_once()
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', submitted['id']) == 'cancelled'
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') > 0
        await run_refresh(database, 1)
        assert await database.fetchval("SELECT sample_count FROM dataset_field_stats WHERE field_name='score'") == 2
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') == 0

    registry_database(scenario)


def test_arriving_field_definitions_do_not_extend_an_active_refresh_forever(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'score': 1}])
        original = statistics_measure.aggregate_field
        measured = 0

        async def new_field(*args):
            nonlocal measured
            measured += 1
            result = await original(*args)
            await ingest([{f'new_{measured:03}': measured}], prefix=f'added-{measured}')
            return result

        monkeypatch.setattr(statistics_measure, 'aggregate_field', new_field)
        await refresh(database)
        # The finite list also publishes all six discovery caches, including empty ones.
        assert measured == 3 + len(statistics_measure.INDEX_TYPES)
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') > 0

    registry_database(scenario)


@pytest.mark.parametrize('stage', ['publish_step', 'defer_job', 'fail_job'])
def test_queue_call_self_cancellation_does_not_stop_the_slot(registry_database, stage):
    from backend.job_runner import StepResult
    from backend.tests.test_job_launch import wait_until

    async def scenario(database):
        original = getattr(job_queue, stage)
        cancelled = False

        async def once(*args, **kwargs):
            nonlocal cancelled
            if not cancelled:
                cancelled = True
                raise asyncio.CancelledError
            return await original(*args, **kwargs)

        async def measure(job):
            if job['args'].get('fail') and stage != 'publish_step':
                if stage == 'defer_job':
                    raise job_queue.QueueContended('try again')
                raise ValueError('bad input')
            return StepResult(finished=True)

        await job_queue.enqueue_job(database.pool, 'probe', args={'fail': True})
        second = await job_queue.enqueue_job(database.pool, 'probe')
        worker = await make_worker(database, {'probe': measure}, poll_seconds=0.01)
        with patch.object(job_queue, stage, once):
            execution = asyncio.create_task(worker.run())
            try:

                async def completed():
                    assert not execution.done()
                    return await database.fetchval('SELECT state FROM jobs WHERE id=$1', second['id']) == 'succeeded'

                await wait_until(completed, timeout=5)
                assert cancelled
            finally:
                worker.stop()
                await asyncio.wait_for(execution, 3)

    registry_database(scenario)
