"""Changes to existing behavior for the scheduled map re-fit: the local runner takes
compute work, embedding uploads join the lifecycle gate, a handler can finish a job in its own fenced
transaction, and the migration rolls back cleanly."""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from backend import job_queue, job_runner, job_service
from backend.job_runner import StepResult
from backend.routers import embeddings
from backend.tests import test_field_registry_postgres
from backend.tests.test_job_runner import make_worker

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')
USER = {'id': 1, 'role': 'admin'}


def test_local_runner_claims_compute_jobs_and_a_plain_server_worker_still_does_not(registry_database):
    async def scenario(database):
        ran = []

        async def handler(job):
            ran.append(job['placement'])
            return StepResult(finished=True)

        compute = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1, placement='compute')
        server_only = await make_worker(database, {'example': handler})
        assert not await server_only.run_once()
        local = await make_worker(database, {'example': handler}, placements=('server', 'compute'))
        assert await local.run_once()
        assert ran == ['compute']
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', compute['id']) == 'succeeded'

    registry_database(scenario)


def test_the_co_located_runner_takes_compute_work_and_registers_map_refits(registry_database, monkeypatch):
    captured: dict = {}

    class Stop(Exception):
        pass

    class FakeRunner:
        def __init__(self, dsn, handlers, **options):
            captured.update(options, handlers=handlers)

        async def __aenter__(self):
            raise Stop()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(job_service, 'local_runner', FakeRunner)
    with pytest.raises(Stop):
        asyncio.run(job_service.run_service('postgres://unused'))
    assert 'map_refit' in captured['handlers'] and 'map_refit' in captured['policies']
    monkeypatch.undo()

    async def scenario(database):
        ran = []

        async def handler(job):
            ran.append(job['placement'])
            return StepResult(finished=True)

        namespace = await database.fetchval('SELECT current_schema()')
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1, placement='compute')
        async with job_runner.local_runner(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            {'example': handler},
            slots=1,
            server_settings={'search_path': namespace + ',public'},
        ) as runner:
            assert await runner.workers[0].run_once()
        assert ran == ['compute']
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'succeeded'

    registry_database(scenario)


def test_embedding_uploads_wait_for_an_exclusive_lifecycle_gate(registry_database, monkeypatch):
    """A map re-fit swaps coordinates in under the exclusive gate: a vector upload must not commit
    between its row-version check and its commit."""
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    monkeypatch.setattr(embeddings, 'validate_embedding_dims', AsyncMock())

    async def scenario(database):
        monkeypatch.setattr(embeddings, 'db', database)
        await database.execute("INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES (1,1,'a.jpg','image')")
        upload = [embeddings.EmbeddingUpload(sample_id=1, field_name='clip', vector=[1.0, 2.0, 3.0])]
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('SELECT pg_advisory_xact_lock(20261016, 1)')
            pending = asyncio.create_task(embeddings.upload_embeddings(1, upload, USER))
            await asyncio.sleep(1.0)
            assert not pending.done()
            assert await database.fetchval('SELECT count(*) FROM embeddings') == 0
        assert (await asyncio.wait_for(pending, 10))['written'] == 1
        assert await database.fetchval('SELECT count(*) FROM embeddings') == 1

    registry_database(scenario)


def test_finish_locked_fences_on_lease_and_cancellation(registry_database):
    async def scenario(database):
        await database.execute('CREATE TABLE effects (value INTEGER)')

        async def handler(job):
            async with database.pool.acquire() as connection, connection.transaction():
                await connection.execute('INSERT INTO effects VALUES (1)')
                await job_queue.finish_locked(
                    connection,
                    job['id'],
                    job['lease_token'],
                    result={'ok': True},
                    message='done',
                )
            return StepResult(committed=True)

        worker = await make_worker(database, {'example': handler})
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        await worker.run_once()
        row = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id'])
        assert (row['state'], row['result'], row['message'], row['lease_token']) == (
            'succeeded',
            {'ok': True},
            'done',
            None,
        )
        assert await database.fetchval('SELECT count(*) FROM effects') == 1

        # A cancel recorded before the fence wins: the effects roll back and the job is cancelled.
        async def cancelled_handler(job):
            await database.execute('UPDATE job_controls SET cancel_requested=TRUE WHERE job_id=$1', job['id'])
            return await handler(job)

        worker = await make_worker(database, {'example': cancelled_handler})
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=2)
        await worker.run_once()
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'cancelled'
        assert await database.fetchval('SELECT count(*) FROM effects') == 1

        # An owner whose lease expired cannot publish.
        claimed = await job_queue.claim_job(database.pool, 'late', kinds=['example'])
        assert claimed is None
        job = await job_queue.enqueue_job(database.pool, 'example', dataset_id=1)
        claimed = await job_queue.claim_job(database.pool, 'late', kinds=['example'])
        assert claimed is not None
        await database.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            job['id'],
        )
        async with database.pool.acquire() as connection:
            with pytest.raises(job_queue.LeaseLost):
                async with connection.transaction():
                    await job_queue.finish_locked(connection, claimed['id'], claimed['lease_token'])

    registry_database(scenario)
