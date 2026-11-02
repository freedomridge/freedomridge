"""Continuous-ingest isolation and opt-in realistic statistics/write-cost diagnostics."""

import asyncio
import json
import os
import time
from typing import Any

import pytest

from backend import statistics, statistics_measure
from backend.job_heartbeat import heartbeat_connector
from backend.job_runner import JobRunner
from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest
from backend.tests.test_job_runner import make_worker

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


def test_small_dataset_and_interactive_use_continue_during_a_heavy_refresh(registry_database, monkeypatch):
    async def scenario(database):
        sample_id = (await ingest([{'score': 1, 'detections': [{'label': 'car'}] * 300}]))['ids'][0]
        await ingest([{'score': 10}], dataset_id=2)
        heavy = await statistics.request_refresh(database.pool, 1)
        measured, release = asyncio.Event(), asyncio.Event()
        original = statistics_measure.measure_part
        paused = False

        async def hold_heavy(pool, dataset_id, *args):
            nonlocal paused
            result = await original(pool, dataset_id, *args)
            if dataset_id == 1 and not paused:
                paused = True
                measured.set()
                await release.wait()
            return result

        monkeypatch.setattr(statistics_measure, 'measure_part', hold_heavy)
        namespace = await database.fetchval('SELECT current_schema()')
        runner = JobRunner(
            database.pool,
            {statistics.KIND: statistics.handler(database.pool)},
            slots=3,
            heartbeat_connect=heartbeat_connector(
                os.environ['FRIDGE_TEST_POSTGRES_DSN'],
                server_settings={'search_path': namespace + ',public'},
            ),
            policies={statistics.KIND: statistics.POLICY},
            poll_seconds=0.01,
        )
        execution = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(measured.wait(), 5)
            small = await statistics.request_refresh(database.pool, 2)
            started = time.monotonic()
            for batch in range(12):
                await asyncio.wait_for(ingest([{'score': batch + 2}] * 20, prefix=f'stream-{batch}'), 2)
                await asyncio.wait_for(
                    datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'score': batch + 30}), USER),
                    2,
                )
                result = await asyncio.wait_for(
                    datasets.list_samples_post(
                        1,
                        datasets.SamplesRequest(filters={'field': 'score', 'operator': '>', 'value': 0}),
                    ),
                    2,
                )
                assert result['total'] == 1 + (batch + 1) * 20
            await statistics.wait_for_refresh(database.pool, small['id'], timeout=5)
            assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', heavy['id']) == 'running'
            print(
                'Concurrent heavy refresh: 240 arrivals, 12 edits, 12 filtered reads '
                f'in {time.monotonic() - started:.3f}s',
            )
            release.set()
            await statistics.wait_for_refresh(database.pool, heavy['id'], timeout=10)
        finally:
            release.set()
            runner.stop()
            await asyncio.wait_for(execution, 5)
        await run_refresh(database, 1)
        assert (
            await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE dataset_id=1 AND field_name='score'",
            )
            == 241
        )
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1') == 0

    registry_database(scenario)


async def timed_refresh(database):
    job = await statistics.request_refresh(database.pool, 1)
    worker = await make_worker(
        database,
        {statistics.KIND: statistics.handler(database.pool)},
        policies={statistics.KIND: statistics.POLICY},
        poll_seconds=0.01,
    )
    started = time.monotonic()
    execution = asyncio.create_task(worker.run())
    try:
        await statistics.wait_for_refresh(database.pool, job['id'], timeout=900)
    finally:
        worker.stop()
        await asyncio.wait_for(execution, 5)
    return time.monotonic() - started


@pytest.mark.skipif(not os.getenv('FRIDGE_STATISTICS_SCALE'), reason='Opt-in concurrent statistics workload')
def test_streaming_ingest_patch_and_search_during_real_statistics(registry_database):
    async def scenario(database):
        metadata = {f'metric_{index:02}': index for index in range(58)}
        sample_id = (await ingest([metadata]))['ids'][0]
        for first in range(1, 50000, 5000):
            await database.execute(
                "INSERT INTO samples(dataset_id,filepath,media_type,metadata) "
                "SELECT 1,'stream-seed-'||position,'image',$1 FROM generate_series($2::int,$3::int) position",
                metadata,
                first,
                min(first + 4999, 49999),
            )
        job = await statistics.request_refresh(database.pool, 1)
        namespace = await database.fetchval('SELECT current_schema()')
        runner = JobRunner(
            database.pool,
            {statistics.KIND: statistics.handler(database.pool)},
            slots=3,
            heartbeat_connect=heartbeat_connector(
                os.environ['FRIDGE_TEST_POSTGRES_DSN'],
                server_settings={'search_path': namespace + ',public'},
            ),
            policies={statistics.KIND: statistics.POLICY},
            poll_seconds=0.01,
        )
        stopping = asyncio.Event()
        latencies: dict[str, list[float]] = {'ingest': [], 'patch': [], 'search': []}
        execution = asyncio.create_task(runner.run())

        async def requests(name):
            while not stopping.is_set():
                sequence = len(latencies[name])
                if name == 'ingest':
                    operation = ingest([{'metric_00': sequence}] * 20, prefix=f'arrival-{sequence}')
                elif name == 'patch':
                    operation = datasets.update_sample(
                        1,
                        sample_id,
                        datasets.SampleUpdate(metadata={'metric_00': sequence}),
                        USER,
                    )
                else:
                    operation = datasets.list_samples_post(
                        1,
                        datasets.SamplesRequest(filters={'field': 'metric_00', 'operator': '>=', 'value': 0}),
                    )
                started = time.monotonic()
                await asyncio.wait_for(operation, 3)
                latencies[name].append(time.monotonic() - started)
                await asyncio.sleep(0.01)

        traffic = [asyncio.create_task(requests(name)) for name in latencies]
        started = time.monotonic()
        try:
            await statistics.wait_for_refresh(database.pool, job['id'], timeout=90)
        finally:
            stopping.set()
            await asyncio.gather(*traffic, return_exceptions=True)
            runner.stop()
            await asyncio.wait_for(execution, 5)
        for task in traffic:
            task.result()
        assert all(len(samples) >= 10 for samples in latencies.values())
        print(
            {
                'seconds': time.monotonic() - started,
                'arrivals': 20 * len(latencies['ingest']),
                'latencies': {
                    name: {
                        'calls': len(samples),
                        'p95_ms': 1000 * sorted(samples)[int(len(samples) * 0.95)],
                        'max_ms': 1000 * max(samples),
                    }
                    for name, samples in latencies.items()
                },
            },
        )
        await timed_refresh(database)
        actual = await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id=1')
        assert actual == 50000 + 20 * len(latencies['ingest'])
        assert (
            await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE field_name='metric_00'",
            )
            == actual
        )
        assert (
            await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE field_name='metric_57'",
            )
            == 50000
        )
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending)')

    registry_database(scenario)


async def timed_write(database, *, tracking, value):
    before_temp = await database.fetchval('SELECT temp_bytes FROM pg_stat_database WHERE datname=current_database()')
    started = time.monotonic()
    async with database.pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("SET LOCAL statement_timeout='600s'")
            if not tracking:
                await connection.execute('ALTER TABLE samples DISABLE TRIGGER trigger_stats_updated')
            report = await connection.fetchval(
                """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) UPDATE samples
                   SET metadata=jsonb_set(metadata,'{c00}',to_jsonb($1::text)) WHERE dataset_id=1""",
                value,
            )
            if not tracking:
                await connection.execute('ALTER TABLE samples ENABLE TRIGGER trigger_stats_updated')
        await connection.execute('SELECT pg_stat_force_next_flush()')
    elapsed = time.monotonic() - started
    await asyncio.sleep(0.1)
    after_temp = await database.fetchval('SELECT temp_bytes FROM pg_stat_database WHERE datname=current_database()')
    return {'seconds': elapsed, 'temporary_bytes': after_temp - before_temp, 'explain': json.loads(report)[0]}


@pytest.mark.skipif(os.getenv('FRIDGE_STATISTICS_SCALE') != '1', reason='Opt-in diagnostic: 58 fields and large writes')
def test_realistic_statistics_and_whole_dataset_write_cost(registry_database, monkeypatch):
    async def scenario(database):
        count = int(os.getenv('FRIDGE_STATISTICS_ROWS', '200000'))
        assert 1000 <= count <= 1000000
        metadata: dict[str, Any] = {f'n{index:02}': index for index in range(30)}
        metadata.update({f'c{index:02}': 'v0' for index in range(24)})
        metadata.update({f'd{index:02}': '2026-01-01' for index in range(4)})
        await ingest([metadata])
        numeric = ','.join(f"'n{index:02}',position%1000+{index}" for index in range(30))
        category = ','.join(f"'c{index:02}','v'||(position%7)::text" for index in range(24))
        dates = ','.join(f"'d{index:02}',(date '2026-01-01'+(position%365)::int)::text" for index in range(4))
        insert = (
            "INSERT INTO samples(dataset_id,filepath,media_type,metadata) SELECT 1,'scale-'||position,'image',"
            f"jsonb_build_object({numeric})||jsonb_build_object({category})||jsonb_build_object({dates}) "
            'FROM generate_series($1::int,$2::int) position'
        )
        started = time.monotonic()
        for first in range(1, count, 10000):
            await database.execute(insert, first, min(first + 9999, count - 1))
        load_seconds = time.monotonic() - started
        unit_seconds = []
        original = statistics_measure.measure_part

        async def timed_part(*args):
            started = time.monotonic()
            result = await original(*args)
            unit_seconds.append(time.monotonic() - started)
            return result

        monkeypatch.setattr(statistics_measure, 'measure_part', timed_part)
        initial_seconds = await timed_refresh(database)
        rows = await database.fetch('SELECT * FROM dataset_field_stats WHERE dataset_id=1')
        measured = {row['field_name']: row for row in rows}
        for index in range(30):
            field = measured[f'n{index:02}']
            assert field['sample_count'] == count and (field['min_value'], field['max_value']) == (index, index + 999)
            assert field['values_complete'] is False
        for index in range(24):
            field = measured[f'c{index:02}']
            assert field['sample_count'] == count and field['values_complete'] is True
            assert field['unique_values'] == [f'v{number}' for number in range(7)]
        for index in range(4):
            field = measured[f'd{index:02}']
            assert field['sample_count'] == count and field['field_type'] == 'datetime'
            assert field['unique_values'] == ['2026-01-01T00:00:00.000000Z', '2026-12-31T00:00:00.000000Z']
            assert field['values_complete'] is False
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') == 0
        baseline = await timed_write(database, tracking=False, value='baseline')
        tracked = await timed_write(database, tracking=True, value='changed')
        pending = await database.fetchval('SELECT count(*) FROM dataset_stats_pending')
        assert pending <= count // 4096 + 2
        assert tracked['temporary_bytes'] < count * 1024
        incremental_seconds = await timed_refresh(database)
        assert await database.fetchval(
            "SELECT unique_values FROM dataset_field_stats WHERE dataset_id=1 AND field_name='c00'",
        ) == ['changed']
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') == 0
        print(
            json.dumps(
                {
                    'rows': count,
                    'fields': 58,
                    'load_seconds': load_seconds,
                    'initial_refresh_seconds': initial_seconds,
                    'changed_blocks_refresh_seconds': incremental_seconds,
                    'max_block_seconds': max(unit_seconds),
                    'pending_rows_for_whole_dataset_edit': pending,
                    'baseline_write': baseline,
                    'tracked_write': tracked,
                },
                sort_keys=True,
            ),
        )

    registry_database(scenario)
