"""Contention delays work without consuming the failure budget; timeouts still do."""

import os

import pytest

from backend import job_queue, job_service
from backend.job_runner import StepResult
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_job_runner import make_worker

registry_database = registry_helpers.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an explicitly supplied disposable PostgreSQL database',
)


def test_cleanup_retries_row_contention_without_charging_failures(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO region_selections(token,sample_id,created_at)
               VALUES ('expired',1,clock_timestamp()-interval '3 hours')""",
        )
        job = await job_service.schedule_cleanup(database.pool)
        worker = await make_worker(
            database,
            {job_service.SELECTION_CLEANUP: job_service.selection_cleanup(database.pool)},
            policies={job_service.SELECTION_CLEANUP: job_queue.JobPolicy(max_failures=1, retry_seconds=0.01)},
        )
        async with database.pool.acquire() as blocker, blocker.transaction():
            await blocker.execute("SELECT 1 FROM region_selections WHERE token='expired' FOR UPDATE")
            for attempt in range(6):
                assert await worker.run_once()
                current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id'])
                assert current['state'] == 'queued'
                assert current['failures'] == 0
                assert current['checkpoint'] == {}
                assert 'lock' in current['error']['message'].lower()
                assert await database.fetchval('SELECT count(*) FROM region_selections') == 1
                await database.execute('UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1', job['id'])
        await worker.run_once()
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'succeeded'
        assert await database.fetchval('SELECT count(*) FROM region_selections') == 0

    registry_database(scenario)


def test_statement_timeout_still_consumes_bounded_failure_budget(registry_database):
    async def scenario(database):
        async def handler(job):
            async def publish(connection):
                await connection.execute("SET LOCAL statement_timeout='20ms'")
                await connection.execute("INSERT INTO region_selections(token,sample_id) VALUES ('rolled-back',1)")
                await connection.execute('SELECT pg_sleep(0.05)')

            return StepResult(checkpoint={'unit': 1}, finished=True, publish=publish)

        job = await job_queue.enqueue_job(database.pool, 'timeout')
        worker = await make_worker(
            database,
            {'timeout': handler},
            policies={'timeout': job_queue.JobPolicy(max_failures=2, retry_seconds=0.01)},
        )
        for failures, expected_state in ((1, 'queued'), (2, 'failed')):
            await worker.run_once()
            current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id'])
            assert current['state'] == expected_state
            assert current['failures'] == failures
            assert current['checkpoint'] == {}
            assert await database.fetchval('SELECT count(*) FROM region_selections') == 0
            await database.execute('UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1', job['id'])

    registry_database(scenario)
