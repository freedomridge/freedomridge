"""The former SQL shell regressions, now exercising real fenced statistics jobs."""

import asyncio
import os

import pytest

from backend import statistics
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import ingest
from backend.tests.test_job_launch import wait_until
from backend.tests.test_job_runner import make_worker

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


def test_mixed_json_shapes_produce_one_cache_row_without_retyping_the_registry(registry_database):
    async def scenario(database):
        await ingest([{'score': number, 'split': 'train'} for number in range(50)])
        await ingest(
            [
                {'x': 1, 'b': True, 'n': None, 's': 'a'},
                {'x': None, 'b': None, 'n': None, 's': 2},
                {'x': 'two', 'b': False, 'n': None},
                {'x': 3, 'b': True, 'n': None, 's': 'c'},
            ]
            * 10,
            dataset_id=2,
        )
        registry = await database.fetch('SELECT * FROM dataset_fields ORDER BY dataset_id,field_name')
        await run_refresh(database)
        fields = {
            row['field_name']: row
            for row in await database.fetch(
                'SELECT * FROM dataset_field_stats WHERE dataset_id=2',
            )
        }
        assert [(fields[name]['field_type'], fields[name]['sample_count']) for name in ['x', 'b', 'n', 's']] == [
            ('string', 30),
            ('boolean', 30),
            ('unknown', 0),
            ('string', 30),
        ]
        assert fields['x']['unique_values'] == ['1', '3', 'two']
        assert fields['s']['unique_values'] == ['2', 'a', 'c']
        assert fields['x']['values_complete'] is True
        assert await database.fetch('SELECT * FROM dataset_fields ORDER BY dataset_id,field_name') == registry
        assert (
            await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE dataset_id=1 AND field_name='score'",
            )
            == 50
        )

    registry_database(scenario)


def test_failed_dataset_keeps_its_cache_and_does_not_stop_other_datasets(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}], dataset_id=1)
        await ingest([{'score': 2}], dataset_id=2)
        await run_refresh(database)
        before = await database.fetch('SELECT * FROM dataset_field_stats WHERE dataset_id=2 ORDER BY id')
        parts = await database.fetch('SELECT * FROM dataset_block_field_stats WHERE dataset_id=2 ORDER BY field_name')
        await database.execute(
            """CREATE FUNCTION reject_statistics_part() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN RAISE EXCEPTION 'injected statistics publication failure'; END $$;
               CREATE TRIGGER reject_statistics_part BEFORE INSERT ON dataset_block_field_stats
               FOR EACH ROW WHEN (NEW.dataset_id=2) EXECUTE FUNCTION reject_statistics_part()""",
        )
        jobs = [await statistics.request_refresh(database.pool, identifier, force=True) for identifier in (1, 2)]
        worker = await make_worker(
            database,
            {statistics.KIND: statistics.handler(database.pool)},
            policies={statistics.KIND: statistics.POLICY},
            poll_seconds=0.01,
        )
        execution = asyncio.create_task(worker.run())
        try:

            async def complete():
                return (
                    await database.fetchval(
                        "SELECT count(*) FROM jobs WHERE id=ANY($1::uuid[]) AND state IN ('succeeded','failed')",
                        [job['id'] for job in jobs],
                    )
                    == 2
                )

            await wait_until(complete, timeout=10)
        finally:
            worker.stop()
            await asyncio.wait_for(execution, 3)
        states = await database.fetch(
            'SELECT dataset_id,state FROM jobs WHERE id=ANY($1::uuid[]) ORDER BY dataset_id',
            [job['id'] for job in jobs],
        )
        assert [(row['dataset_id'], row['state']) for row in states] == [(1, 'succeeded'), (2, 'failed')]
        assert await database.fetch('SELECT * FROM dataset_field_stats WHERE dataset_id=2 ORDER BY id') == before
        assert (
            await database.fetch('SELECT * FROM dataset_block_field_stats WHERE dataset_id=2 ORDER BY field_name')
            == parts
        )
        assert await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=2)')
        assert await statistics.schedule_statistics(database.pool) == []
        await database.execute('DROP TRIGGER reject_statistics_part ON dataset_block_field_stats')
        await run_refresh(database, 2)
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=2)')

    registry_database(scenario)
