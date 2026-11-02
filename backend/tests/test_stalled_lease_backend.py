"""Reproduce a frozen lease socket using only a positively identified disposable PostgreSQL session."""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest

from backend import job_heartbeat, job_queue, job_runner
from backend.job_runner import StepResult
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_job_launch import wait_until

registry_database = registry_helpers.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires an explicit disposable PostgreSQL DSN and its Docker container name',
)


@pytest.mark.parametrize('lease,heartbeat,unit_seconds', [(6, 1, 8), (60, 10, 22)])
def test_stopped_lease_backend_is_replaced_and_later_jobs_finish(registry_database, lease, heartbeat, unit_seconds):
    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        database_name = await database.fetchval('SELECT current_database()')
        username = await database.fetchval('SELECT current_user')
        application = 'fridge-lease-probe-' + uuid4().hex
        connector = job_heartbeat.heartbeat_connector(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings={'search_path': namespace + ',public', 'application_name': application},
        )
        sessions: list[int] = []
        stopped: list[int] = []

        async def connect():
            connection = await connector()
            sessions.append(connection.get_server_pid())
            return connection

        def signal_owned_backend(signum, identifier):
            container = os.environ['FRIDGE_TEST_POSTGRES_CONTAINER']
            verified = subprocess.run(
                [
                    'docker',
                    'exec',
                    container,
                    'psql',
                    '-X',
                    '-U',
                    username,
                    '-d',
                    database_name,
                    '-Atc',
                    f'SELECT application_name FROM pg_stat_activity WHERE pid={int(identifier)}',
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            if not verified and signum == 'CONT':
                return
            assert verified == application, 'Refusing to signal a database session not owned by this test'
            subprocess.run(
                ['docker', 'exec', container, 'sh', '-c', 'kill "-$1" "$2"', 'lease-signal', signum, str(identifier)],
                check=True,
                timeout=5,
            )

        async def handler(job):
            if job['args']['number'] == 1 and not stopped:
                await asyncio.sleep(0.3)
                stopped.append(sessions[-1])
                await asyncio.to_thread(signal_owned_backend, 'STOP', stopped[-1])
                await asyncio.sleep(unit_seconds)
            return StepResult(finished=True, result=job['args']['number'])

        for number in (1, 2, 3):
            await job_queue.enqueue_job(database.pool, 'lease-probe', args={'number': number})
        worker = job_runner.ServerWorker(
            database.pool,
            {'lease-probe': handler},
            heartbeat_connect=connect,
            lease_seconds=lease,
            heartbeat_seconds=heartbeat,
            poll_seconds=0.05,
        )
        execution = asyncio.create_task(worker.run())
        try:

            async def completed():
                assert not execution.done(), 'A frozen database connection ended the worker slot'
                return await database.fetchval("SELECT count(*) FROM jobs WHERE state='succeeded'") == 3

            await wait_until(completed, timeout=unit_seconds + 8)
            assert len(sessions) >= 2
            assert stopped == [sessions[0]]
            rows = await database.fetch('SELECT result,attempt,failures FROM jobs ORDER BY result')
            assert [(row['result'], row['attempt'], row['failures']) for row in rows] == [
                (1, 1, 0),
                (2, 1, 0),
                (3, 1, 0),
            ]
            worker.stop()
            await asyncio.wait_for(execution, 3)
            assert worker.lease_session is None
        finally:
            try:
                for identifier in stopped:
                    await asyncio.to_thread(signal_owned_backend, 'CONT', identifier)
            finally:
                worker.stop()
                if not execution.done():
                    execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)

    registry_database(scenario)
