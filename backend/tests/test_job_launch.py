"""Step 1 runs real maintenance without changing live datasets or services."""

import asyncio
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from backend import job_heartbeat, job_queue, job_runner, job_service, schema_check
from backend.job_runner import StepResult
from backend.tests import test_field_registry_postgres
from backend.tests.test_job_runner import make_worker
from backend.tests.test_launcher_lifecycle import group_members

registry_database = test_field_registry_postgres.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an explicitly supplied throwaway PostgreSQL database',
)
ROOT = Path(__file__).resolve().parents[2]


def test_stray_handler_cancellation_fails_job_not_slot(registry_database):
    async def scenario(database):
        completed = asyncio.Event()

        async def handler(job):
            if job['args'].get('cancel_self'):
                raise asyncio.CancelledError('stray handler cancellation')
            completed.set()
            return StepResult(finished=True)

        first = await job_queue.enqueue_job(database.pool, 'example', args={'cancel_self': True})
        second = await job_queue.enqueue_job(database.pool, 'example')
        worker = await make_worker(database, {'example': handler}, poll_seconds=0.01)
        execution = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(completed.wait(), 3)
        finally:
            worker.stop()
            await asyncio.wait_for(execution, 3)
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', first['id']) == 'failed'
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', second['id']) == 'succeeded'

    registry_database(scenario)


def test_external_slot_cancellation_still_stops_and_closes_lease_session(registry_database):
    async def scenario(database):
        entered = asyncio.Event()

        async def handler(job):
            entered.set()
            await asyncio.Event().wait()

        await job_queue.enqueue_job(database.pool, 'example')
        worker = await make_worker(database, {'example': handler})
        execution = asyncio.create_task(worker.run())
        await asyncio.wait_for(entered.wait(), 2)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution, 2)
        assert worker.lease_session is None
        assert not any(thread.name.startswith('lease:') for thread in threading.enumerate())

    registry_database(scenario)


def test_three_hundred_jobs_reuse_one_lease_session_per_slot(registry_database):
    async def scenario(database):
        for number in range(300):
            await job_queue.enqueue_job(database.pool, 'example')
        namespace = await database.fetchval('SELECT current_schema()')
        connections = []
        handled = []
        completed = asyncio.Event()
        connector = job_heartbeat.heartbeat_connector(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings={'search_path': namespace + ',public'},
        )

        async def connect():
            connection = await connector()
            connections.append(connection)
            return connection

        async def handler(job):
            handled.append(job['id'])
            if len(handled) == 300:
                completed.set()
            return StepResult(finished=True)

        runner = job_runner.JobRunner(database.pool, {'example': handler}, heartbeat_connect=connect, slots=3)
        execution = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(completed.wait(), 20)
        finally:
            runner.stop()
            await asyncio.wait_for(execution, 5)
        assert len(connections) == 3
        assert all(connection.is_closed() for connection in connections)
        assert await database.fetchval("SELECT count(*) FROM jobs WHERE state='succeeded'") == 300
        assert not any(thread.name.startswith('lease:') for thread in threading.enumerate())

    registry_database(scenario)


def test_raw_job_insert_gets_controls_and_can_be_claimed_and_cancelled(registry_database):
    async def scenario(database):
        identifier = uuid4()
        await database.execute('INSERT INTO jobs(id,kind) VALUES ($1,$2)', identifier, 'raw')
        assert await database.fetchval('SELECT cancel_requested FROM job_controls WHERE job_id=$1', identifier) is False
        claim = await job_queue.claim_job(database.pool, 'worker')
        assert claim is not None
        assert claim['id'] == identifier
        cancelled = await job_queue.cancel_job(database.pool, identifier)
        assert cancelled is not None and cancelled['cancel_requested']

    registry_database(scenario)


def test_unsafe_heartbeat_settings_are_rejected(registry_database):
    async def scenario(database):
        async def handler(job):
            return StepResult(finished=True)

        with pytest.raises(ValueError, match='bounded renewal'):
            await make_worker(database, {'example': handler}, lease_seconds=12, heartbeat_seconds=5)

    registry_database(scenario)


def test_slow_telemetry_cannot_delay_lease_renewals(registry_database, monkeypatch):
    async def scenario(database):
        async def telemetry(*args):
            await asyncio.sleep(10)

        monkeypatch.setattr(job_heartbeat, 'worker_heartbeat', telemetry)

        async def handler(job):
            await asyncio.sleep(1.2)
            return StepResult(finished=True, result='alive')

        job = await job_queue.enqueue_job(database.pool, 'example')
        worker = await make_worker(database, {'example': handler}, lease_seconds=0.8, heartbeat_seconds=0.1)
        await asyncio.wait_for(worker.run_once(), 3)
        assert await database.fetchval('SELECT result FROM jobs WHERE id=$1', job['id']) == 'alive'

    registry_database(scenario)


def test_scheduler_concurrency_cadence_and_bounded_cleanup_preserve_recent_selections(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO region_selections(token,sample_id,created_at)
               SELECT 'expired',number,clock_timestamp()-interval '3 hours' FROM generate_series(1,2500) number;
               INSERT INTO region_selections(token,sample_id) VALUES ('recent',1)""",
        )
        await asyncio.gather(*(job_service.schedule_cleanup(database.pool) for attempt in range(8)))
        assert await database.fetchval('SELECT count(*) FROM jobs') == 1
        claim = await job_queue.claim_job(database.pool, 'manual')
        assert claim is not None
        handler = job_service.selection_cleanup(database.pool)
        unit = await handler(claim)
        current = await job_queue.publish_step(
            database.pool,
            claim['id'],
            claim['lease_token'],
            checkpoint=unit.checkpoint,
            publisher=unit.publish,
            release=True,
        )
        assert await database.fetchval("SELECT count(*) FROM region_selections WHERE token='expired'") == 1500
        assert current['checkpoint'] == {'batches': 1}
        consumer = await make_worker(database, {job_service.SELECTION_CLEANUP: handler})
        await consumer.run_once()
        assert await database.fetchval("SELECT count(*) FROM region_selections WHERE token='expired'") == 0
        assert await database.fetchval("SELECT count(*) FROM region_selections WHERE token='recent'") == 1
        assert await job_service.schedule_cleanup(database.pool) is None
        await database.execute("UPDATE jobs SET created_at=clock_timestamp()-interval '10 minutes'")
        assert await job_service.schedule_cleanup(database.pool) is not None

    registry_database(scenario)


async def wait_until(check, timeout=15):
    async def poll():
        while not await check():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout)


@pytest.mark.parametrize(
    'signals',
    [(signal.SIGHUP, signal.SIGHUP), (signal.SIGTERM, signal.SIGTERM), (signal.SIGHUP, signal.SIGINT)],
)
def test_repeated_shutdown_signals_preserve_the_inflight_unit(registry_database, tmp_path, signals):
    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        job = await job_queue.enqueue_job(database.pool, 'shutdown')
        ready = tmp_path / 'measuring'
        program = """
import asyncio, os, signal, sys
from pathlib import Path
from backend import job_queue
from backend.job_runner import StepResult, local_runner

async def main():
    async def handler(job):
        Path(sys.argv[1]).write_text('measuring')
        await asyncio.sleep(0.7)
        async def publish(connection):
            await connection.execute("UPDATE datasets SET description='shutdown-committed' WHERE id=1")
        return StepResult(checkpoint={'unit':1}, finished=True, publish=publish)
    async with local_runner(os.environ['DATABASE_URL'], {'shutdown':handler}, slots=1) as runner:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, runner.stop)
        await runner.run()
asyncio.run(main())
"""
        launcher = (
            'import signal; signal.signal(signal.SIGHUP,signal.SIG_DFL); '
            'from backend.launch import main; raise SystemExit(main())'
        )
        with (tmp_path / 'double-signal.log').open('wb') as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                '-c',
                launcher,
                '--',
                sys.executable,
                '-c',
                program,
                str(ready),
                cwd=ROOT,
                env={
                    **os.environ,
                    'DATABASE_URL': os.environ['FRIDGE_TEST_POSTGRES_DSN'] + '?search_path=' + namespace + ',public',
                    'FRIDGE_RUNNER_ENABLED': 'false',
                    'FRIDGE_SHUTDOWN_SECONDS': '3',
                },
                stdout=log,
                stderr=log,
            )
            try:

                async def started():
                    assert process.returncode is None, (tmp_path / 'double-signal.log').read_text()
                    return ready.exists()

                await wait_until(started)
                process.send_signal(signals[0])
                await asyncio.sleep(0.15)
                process.send_signal(signals[1])
                await asyncio.wait_for(process.wait(), 5)
                assert process.returncode == 0, (tmp_path / 'double-signal.log').read_text()
                saved = await database.fetchrow('SELECT state,checkpoint,failures FROM jobs WHERE id=$1', job['id'])
                assert saved['state'] == 'succeeded' and saved['checkpoint'] == {'unit': 1}
                assert saved['failures'] == 0
                assert await database.fetchval('SELECT description FROM datasets WHERE id=1') == 'shutdown-committed'
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    registry_database(scenario)


def test_service_reconnects_lease_connection_after_backend_termination(registry_database):
    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        connections = []
        first = asyncio.Event()
        resumed = asyncio.Event()
        connector = job_heartbeat.heartbeat_connector(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings={'search_path': namespace + ',public'},
        )

        async def connect():
            connection = await connector()
            connections.append(connection.get_server_pid())
            return connection

        async def handler(job):
            first.set()
            await resumed.wait()
            await asyncio.sleep(0.4)
            return StepResult(finished=True)

        worker = job_runner.ServerWorker(
            database.pool,
            {'example': handler},
            heartbeat_connect=connect,
            lease_seconds=1,
            heartbeat_seconds=0.1,
        )
        await job_queue.enqueue_job(database.pool, 'example')
        execution = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(first.wait(), 2)
            await database.fetchval('SELECT pg_terminate_backend($1)', connections[0])
            resumed.set()
            await asyncio.wait_for(execution, 3)
            assert len(connections) == 2
            assert await database.fetchval("SELECT state FROM jobs") == 'succeeded'
        finally:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)

    registry_database(scenario)


@pytest.mark.parametrize('exit_cause', ['signal', 'hangup', 'api_exit', 'runner_exit', 'launcher_exit'])
def test_supervisor_starts_one_runner_and_stops_both_process_groups(registry_database, tmp_path, exit_cause):
    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        dsn = os.environ['FRIDGE_TEST_POSTGRES_DSN'] + '?search_path=' + namespace + ',public'
        environment = {
            **os.environ,
            'DATABASE_URL': dsn,
            'JOB_SELECTION_CLEANUP_SECONDS': '1',
            'JOB_WORKER_SLOTS': '2',
            'JOB_POOL_MAX_SIZE': '3',
            'FRIDGE_SHUTDOWN_SECONDS': '5',
            'JOB_SHUTDOWN_SECONDS': '2',
            'FRIDGE_RUNNER_ENABLED': 'true',
        }
        api_pids = tmp_path / 'api-pids'
        api_program = (
            'import os,subprocess,sys,time; '
            'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(90)"]); '
            f'open({str(api_pids)!r},"w").write(str(os.getpid())+" "+str(child.pid)); time.sleep(90)'
        )
        log_path = tmp_path / 'supervisor.log'
        owned_groups: list[int] = []
        with log_path.open('wb') as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                '-m',
                'backend.launch',
                '--',
                sys.executable,
                '-c',
                api_program,
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:

                async def online():
                    return api_pids.exists() and await database.fetchval('SELECT count(*) FROM workers') == 2

                await wait_until(online)
                direct_children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
                assert len(direct_children) == 2
                owned_groups.extend(int(child) for child in direct_children)
                api_pid, grandchild_pid = map(int, api_pids.read_text().split())
                runner_pid = next(int(child) for child in direct_children if int(child) != api_pid)
                if exit_cause == 'signal':
                    process.terminate()
                elif exit_cause == 'hangup':
                    process.send_signal(signal.SIGHUP)
                elif exit_cause == 'launcher_exit':
                    process.kill()
                else:
                    os.kill(api_pid if exit_cause == 'api_exit' else runner_pid, signal.SIGKILL)
                if exit_cause == 'runner_exit':

                    async def restarted():
                        assert process.returncode is None, 'Runner failure must not stop the API supervisor'
                        current = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
                        owned_groups.extend(int(child) for child in current if int(child) not in owned_groups)
                        return (
                            str(api_pid) in current
                            and len(current) == 2
                            and str(runner_pid) not in current
                            and await database.fetchval('SELECT count(*) FROM workers') == 4
                        )

                    await wait_until(restarted)
                    assert api_pids.read_text() == f'{api_pid} {grandchild_pid}'
                    process.terminate()
                await asyncio.wait_for(process.wait(), 10)

                async def all_stopped():
                    for identifier in (*owned_groups, grandchild_pid):
                        try:
                            state = Path(f'/proc/{identifier}/stat').read_text().split()[2]
                        except (FileNotFoundError, ProcessLookupError):
                            continue  # reaped between the check and the read
                        if state != 'Z':
                            return False
                    return True

                await wait_until(all_stopped)
                assert (
                    process.returncode != 0 if exit_cause in ('api_exit', 'launcher_exit') else process.returncode == 0
                )
            finally:
                if process.returncode is None:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 10)
                for group_id in owned_groups:
                    try:
                        os.killpg(group_id, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                assert 'Traceback' not in log_path.read_text(), log_path.read_text()

    registry_database(scenario)


def test_missing_runner_migration_keeps_api_up_and_recovers_without_restart(registry_database, tmp_path):
    migration = schema_check.BASELINE

    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        await database.execute('DELETE FROM schema_migrations WHERE filename=$1', migration)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        environment = {
            **os.environ,
            'DATABASE_URL': os.environ['FRIDGE_TEST_POSTGRES_DSN'] + '?search_path=' + namespace + ',public',
            'FRIDGE_RUNNER_ENABLED': 'true',
            'JOB_WORKER_SLOTS': '2',
            'JOB_POOL_MAX_SIZE': '3',
            'DB_POOL_MIN_SIZE': '1',
            'DB_POOL_MAX_SIZE': '3',
            'FRIDGE_SHUTDOWN_SECONDS': '5',
            'JOB_SHUTDOWN_SECONDS': '2',
        }
        log_path = tmp_path / 'pending-migration.log'
        with log_path.open('wb') as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                '-m',
                'backend.launch',
                '--',
                sys.executable,
                '-m',
                'uvicorn',
                'backend.main:app',
                '--host',
                '127.0.0.1',
                '--port',
                str(port),
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=log,
            )

            async def api_responds():
                assert process.returncode is None, log_path.read_text()
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.1', port)
                    try:
                        writer.write(b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                        await writer.drain()
                        return b'200 OK' in await asyncio.wait_for(reader.read(), 2)
                    finally:
                        writer.close()
                        await writer.wait_closed()
                except OSError:
                    return False

            try:
                await wait_until(api_responds)
                await asyncio.sleep(1.5)
                assert await api_responds()
                children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
                api_pid = next(child for child in children if b'uvicorn' in Path(f'/proc/{child}/cmdline').read_bytes())
                runner_pid = next(
                    child for child in children if b'backend.job_service' in Path(f'/proc/{child}/cmdline').read_bytes()
                )
                assert await database.fetchval('SELECT count(*) FROM workers') == 0
                await database.execute(
                    'INSERT INTO schema_migrations(filename) VALUES ($1) ON CONFLICT DO NOTHING',
                    migration,
                )

                async def worker_recovers():
                    assert await api_responds()
                    return await database.fetchval('SELECT count(*) FROM workers') == 2

                await wait_until(worker_recovers)
                children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
                assert api_pid in children and runner_pid in children
            finally:
                if process.returncode is None:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 10)

    registry_database(scenario)


@pytest.mark.parametrize('stop_cause', ['signal', 'launcher_exit'])
def test_local_shell_launcher_runs_two_api_workers_but_one_runner(registry_database, tmp_path, stop_cause):
    async def scenario(database):
        namespace = await database.fetchval('SELECT current_schema()')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        environment = {
            **os.environ,
            'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
            'ROOT_DIR': str(tmp_path),
            'PYTHONPATH': os.pathsep.join((str(ROOT), str(ROOT / 'python'))),
            'DATABASE_URL': os.environ['FRIDGE_TEST_POSTGRES_DSN'] + '?search_path=' + namespace + ',public',
            'FRIDGE_BACKEND_HOST': '127.0.0.1',
            'FRIDGE_BACKEND_PORT': str(port),
            'FRIDGE_WORKERS': '2',
            'FRIDGE_RUNNER_ENABLED': 'true',
            'JOB_WORKER_SLOTS': '2',
            'JOB_POOL_MAX_SIZE': '3',
            'DB_POOL_MIN_SIZE': '1',
            'DB_POOL_MAX_SIZE': '3',
            'FRIDGE_LOG_FILE': '',
            'FRIDGE_SHUTDOWN_SECONDS': '10',
            'JOB_SHUTDOWN_SECONDS': '3',
        }
        with (tmp_path / 'local-launch.log').open('wb') as log:
            process = await asyncio.create_subprocess_exec(
                'bash',
                'scripts/run_backend.sh',
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=log,
            )
            groups = []
            try:

                async def ready():
                    try:
                        reader, writer = await asyncio.open_connection('127.0.0.1', port)
                        writer.write(b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                        await writer.drain()
                        response = await reader.read()
                        writer.close()
                        await writer.wait_closed()
                        return b'200 OK' in response and await database.fetchval('SELECT count(*) FROM workers') == 2
                    except OSError:
                        return False

                await wait_until(ready, timeout=30)
                children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
                assert len(children) == 2
                groups = [int(child) for child in children]
                if stop_cause == 'launcher_exit':
                    process.kill()
                else:
                    process.terminate()
                await asyncio.wait_for(process.wait(), 15)

                async def all_stopped():
                    return not any(group_members(group) for group in groups)

                await wait_until(all_stopped, timeout=3)
                with pytest.raises(OSError):
                    await asyncio.open_connection('127.0.0.1', port)
            finally:
                if process.returncode is None:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 15)
                for group in groups:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        assert (process.returncode == 0) == (stop_cause == 'signal'), (tmp_path / 'local-launch.log').read_text()

    registry_database(scenario)
