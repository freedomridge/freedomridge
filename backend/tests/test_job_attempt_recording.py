"""An attempt's end (deferral, failure, cancellation) is recorded even when a short holder has the job row."""

import asyncio
import logging
import os
import threading
import time
from uuid import uuid4

import pytest

from backend import job_queue
from backend.job_runner import StepResult
from backend.tests import test_field_registry_postgres
from backend.tests.test_job_runner import job_state, make_worker

registry_database = test_field_registry_postgres.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)

# Renewals every second, a one-second renewal budget and an eight-second lease (the production shape, scaled down).
LEASE = {'lease_seconds': 8, 'heartbeat_seconds': 1, 'retry_seconds': 0.01}


async def hold_job_row(database, job_id, locked, seconds, *, then=None):
    """Another session holds the job row, as a renewal whose commit waits on a slow disk does."""
    async with database.pool.acquire() as connection, connection.transaction():
        await connection.execute('SELECT id FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        locked.set()
        await asyncio.sleep(seconds)
        if then is not None:
            await then(connection)


def stall_next_renewal_commit(monkeypatch, seconds=0.5):
    """Once armed, the next lease renewal writes its row and then holds it, as a commit waiting on a slow WAL
    flush does. Returns (armed, holding), both threading events: the renewal runs on the lease thread."""
    renew = job_queue.heartbeat_job
    armed, holding = threading.Event(), threading.Event()

    async def slow_commit_renewal(connection, job_id, lease_token, **options):
        if not armed.is_set() or holding.is_set():
            return await renew(connection, job_id, lease_token, **options)
        async with connection.transaction():
            renewed = await renew(connection, job_id, lease_token, **options)
            holding.set()
            await asyncio.sleep(seconds)
        return renewed

    monkeypatch.setattr(job_queue, 'heartbeat_job', slow_commit_renewal)
    return armed, holding


def test_publication_and_deferral_wait_out_the_jobs_own_slow_lease_renewal(registry_database, monkeypatch):
    """Under load, the job's own renewal held its row past the queue's 100 ms lock timeout, so the
    unit's publication was contended, and its deferral, attempted at once, met the same renewal and gave up.
    Whatever the publication meets, the attempt must end: published, or deferred without a failure."""

    async def scenario(database):
        armed, holding = stall_next_renewal_commit(monkeypatch)
        attempts = []

        async def handler(job):
            attempts.append(job['attempt'])
            if len(attempts) == 1:
                armed.set()
                assert await asyncio.to_thread(holding.wait, 5)
            return StepResult(finished=True, result={'attempt': job['attempt']})

        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(database, {'example': handler}, **LEASE)
        assert await asyncio.wait_for(worker.run_once(), 5)
        ended = await job_state(database, submitted)
        assert ended['state'] in ('succeeded', 'queued'), 'The attempt was stranded until its lease expired'
        assert ended['lease_token'] is None and ended['lease_expires_at'] is None
        assert ended['failures'] == 0
        if ended['state'] == 'queued':
            await asyncio.sleep(LEASE['retry_seconds'] * 10)  # past the contention's retry delay
            assert await asyncio.wait_for(worker.run_once(), 5)
        assert (await job_state(database, submitted))['state'] == 'succeeded'

    registry_database(scenario)


@pytest.mark.parametrize('ending', ['deferred', 'failed', 'cancelled'])
def test_attempt_end_is_recorded_once_a_transient_holder_releases_the_job_row(registry_database, ending):
    """A holder that outlasts the stopped heartbeat (a unit or renewal session the server is still finishing) is
    retried at the heartbeat's retry gap, within the lease."""

    async def scenario(database):
        locked = asyncio.Event()
        holders = []

        async def handler(job):
            holders.append(asyncio.create_task(hold_job_row(database, job['id'], locked, 1.5)))
            await locked.wait()
            if ending == 'deferred':
                raise ConnectionError('database connection interrupted')
            if ending == 'failed':
                raise ValueError('unsupported test input')
            await job_queue.cancel_job(database.pool, job['id'])
            raise job_queue.CancelRequested('Cancelled before the result was published')

        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(database, {'example': handler}, **LEASE)
        try:
            assert await asyncio.wait_for(worker.run_once(), 6)
        finally:
            await asyncio.gather(*holders)
        row = await job_state(database, submitted)
        assert row['state'] == {'deferred': 'queued', 'failed': 'failed', 'cancelled': 'cancelled'}[ending]
        assert row['lease_token'] is None and row['lease_expires_at'] is None
        if ending == 'deferred':
            assert row['error'] == {'type': 'ConnectionError', 'message': 'database connection interrupted'}
            assert row['failures'] == 1
        elif ending == 'failed':
            assert row['error'] == {'type': 'ValueError', 'message': 'unsupported test input'}

    registry_database(scenario)


def test_recording_gives_up_with_the_lease_and_the_expiry_path_takes_over(registry_database, caplog):
    """Retries never outlive the lease: the slot moves on, writes nothing, and the job is claimable at expiry."""

    async def scenario(database):
        caplog.set_level(logging.WARNING, logger='backend.job_runner')
        locked = asyncio.Event()
        holders = []

        async def handler(job):
            holders.append(asyncio.create_task(hold_job_row(database, job['id'], locked, 3)))
            await locked.wait()
            raise ConnectionError('database connection interrupted')

        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(
            database,
            {'example': handler},
            lease_seconds=0.8,
            heartbeat_seconds=0.1,
            retry_seconds=0.01,
        )
        try:
            started = time.monotonic()
            assert await asyncio.wait_for(worker.run_once(), 3)
            assert time.monotonic() - started < 2, 'Recording retried past the lease'
            assert not holders[0].done(), 'The slot waited for the holder instead of its lease'
        finally:
            await asyncio.gather(*holders)
        assert 'could not record failure' in caplog.text
        stranded = await job_state(database, submitted)
        assert stranded['state'] == 'running' and stranded['error'] is None
        assert await database.fetchval(
            'SELECT lease_expires_at <= clock_timestamp() FROM jobs WHERE id=$1',
            submitted['id'],
        )
        successor = await job_queue.claim_job(database.pool, 'successor', kinds=['example'])
        assert successor is not None and successor['id'] == submitted['id']
        assert successor['attempt'] == 2 and successor['failures'] == 1

    registry_database(scenario)


def test_recording_stops_when_the_lease_moves_to_another_worker(registry_database):
    """Fencing: once another worker owns the lease, the old slot's retries stop and it never writes the row."""

    async def scenario(database):
        locked = asyncio.Event()
        holders = []
        successor = uuid4()

        async def hand_over(connection):
            await connection.execute(
                """UPDATE jobs SET worker_id='successor',lease_token=$2,
                   lease_expires_at=clock_timestamp()+interval '60 seconds' WHERE id=$1""",
                submitted['id'],
                successor,
            )

        async def handler(job):
            holders.append(asyncio.create_task(hold_job_row(database, job['id'], locked, 0.5, then=hand_over)))
            await locked.wait()
            raise ConnectionError('database connection interrupted')

        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(database, {'example': handler}, **LEASE)
        try:
            started = time.monotonic()
            assert await asyncio.wait_for(worker.run_once(), 6)
            assert time.monotonic() - started < LEASE['lease_seconds'] / 2
        finally:
            await asyncio.gather(*holders)
        row = await job_state(database, submitted)
        assert (row['state'], row['worker_id'], row['lease_token']) == ('running', 'successor', successor)
        assert row['error'] is None and row['failures'] == 0

    registry_database(scenario)


def test_cancellation_ack_that_outlives_the_lease_leaves_the_job_to_the_expiry_sweep(registry_database, caplog):
    """A transient error that outlasts the lease ends the attempt quietly, as for a deferral: the cancellation is
    durable, so the expiry sweep cancels the job; the slot neither raises nor backs off."""

    async def scenario(database):
        caplog.set_level(logging.WARNING, logger='backend.job_runner')
        locked = asyncio.Event()
        holders = []

        async def handler(job):
            holders.append(asyncio.create_task(hold_job_row(database, job['id'], locked, 3)))
            await locked.wait()
            await job_queue.cancel_job(database.pool, job['id'])
            raise job_queue.CancelRequested('Cancelled before the result was published')

        submitted = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        worker = await make_worker(
            database,
            {'example': handler},
            lease_seconds=0.8,
            heartbeat_seconds=0.1,
            retry_seconds=0.01,
        )
        try:
            assert await asyncio.wait_for(worker.run_once(), 3)
            assert not holders[0].done(), 'The slot waited for the holder instead of its lease'
        finally:
            await asyncio.gather(*holders)
        assert 'could not acknowledge its cancellation; its lease will expire' in caplog.text
        assert await job_queue.claim_job(database.pool, 'successor', kinds=['example']) is None
        assert (await job_state(database, submitted))['state'] == 'cancelled'

    registry_database(scenario)
