"""Field promotion builds its index in a user-lane job: five-second answer, crash recovery, guards."""

import asyncio
import json
import os
import sys
import time
from uuid import UUID

import pytest
from fastapi import HTTPException

from backend import access, index_jobs, job_queue, job_wait
from backend.routers import datasets
from backend.routers import jobs as jobs_router
from backend.tests import job_helpers
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = registry_helpers.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
KIND = index_jobs.FIELD_KIND
INDEX = 'idx_samples_meta_1_score'


async def index_state(database, name=INDEX):
    return await database.fetchval(
        'SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid '
        'WHERE c.relname=$1 AND c.relnamespace=current_schema()::regnamespace',
        name,
    )


async def promoted(database):
    return await database.fetchval(
        "SELECT is_promoted FROM dataset_fields WHERE dataset_id=1 AND field_name='score'",
    )


async def lock_free(database):
    async with database.pool.acquire() as connection:
        if await connection.fetchval('SELECT pg_try_advisory_lock(20261017, 1)'):
            await connection.execute('SELECT pg_advisory_unlock(20261017, 1)')
            return True
    return False


async def open_snapshot(database):
    """CREATE INDEX CONCURRENTLY waits for this snapshot after cataloguing its (invalid) index."""
    connection = await database.pool.acquire()
    transaction = connection.transaction(isolation='repeatable_read')
    await transaction.start()
    await connection.fetchval('SELECT count(*) FROM datasets')
    return connection, transaction


def test_fast_promotion_answers_final_message_and_appears_in_history(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs_router, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await ingest([{'score': value} for value in range(20)])
        async with job_helpers.runner(database, kinds=[KIND]):
            started = time.monotonic()
            result = await datasets.promote_field(1, 'score', USER)
            assert time.monotonic() - started <= job_wait.ANSWER_SECONDS + 1
        assert result == {'message': 'Field score promoted successfully'}
        assert await promoted(database) and await index_state(database) is True
        assert await datasets.promote_field(1, 'score', USER) == {'message': 'Field score is already promoted'}
        page = await jobs_router.list_dataset_jobs(1, limit=10, cursor=None, user=USER)
        assert [(job.kind, job.state, job.result) for job in page.items] == [(KIND, 'succeeded', result)]
        assert await lock_free(database)

    registry_database(scenario)


def test_slow_promotion_answers_202_and_its_job_finishes(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(job_wait, 'ANSWER_SECONDS', 0.2)
        await ingest([{'score': 1}, {'score': 2}])
        response = await datasets.promote_field(1, 'score', USER)
        assert response.status_code == 202
        handle = json.loads(response.body)
        assert set(handle) == {'job_id', 'state'}
        # The same field joins the unfinished build; another index operation is refused, as before.
        again = await datasets.promote_field(1, 'score', USER)
        assert json.loads(again.body)['job_id'] == handle['job_id']
        job = await job_helpers.finish(database, KIND)
        assert str(job['id']) == handle['job_id']
        assert job['result'] == {'message': 'Field score promoted successfully'}
        assert await promoted(database) and await index_state(database) is True

    registry_database(scenario)


def test_bad_values_fail_with_409_and_leave_no_invalid_index(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}, {'score': 2}])
        await database.execute(
            """UPDATE samples SET metadata=jsonb_set(metadata,'{score}','"abc"')
               WHERE id=(SELECT min(id) FROM samples)""",
        )
        async with job_helpers.runner(database, kinds=[KIND]):
            with pytest.raises(HTTPException) as failed:
                await datasets.promote_field(1, 'score', USER)
        assert failed.value.status_code == 409 and 'check values' in failed.value.detail
        assert await index_state(database) is None and not await promoted(database)
        assert await lock_free(database)

    registry_database(scenario)


def test_busy_index_lock_defers_without_spending_failures(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}])
        submitted = await index_jobs.submit_field_index(
            database.pool,
            1,
            1,
            {
                'field_name': 'score',
                'index_name': INDEX,
                'expression': "(( (metadata->>'score')::float ))",
                'definition_id': await database.fetchval("SELECT id FROM dataset_fields WHERE field_name='score'"),
                'field_type': 'number',
            },
        )
        holder = await database.pool.acquire()
        try:
            assert await holder.fetchval('SELECT pg_try_advisory_lock(20261017, 1)')
            job = await job_helpers.claim(database, KIND)
            assert job['id'] == submitted['id']
            with pytest.raises(job_queue.QueueContended):
                await index_jobs.field_handler(database.pool)(job)
            deferred = await job_queue.defer_job(
                database.pool,
                job['id'],
                job['lease_token'],
                {},
                delay_seconds=0,
                count_failure=False,
            )
            assert deferred['state'] == 'queued' and deferred['failures'] == 0
        finally:
            await holder.execute('SELECT pg_advisory_unlock(20261017, 1)')
            await database.pool.release(holder)
        assert (await job_helpers.finish(database, KIND))['state'] == 'succeeded'
        assert await promoted(database)

    registry_database(scenario)


def test_build_killed_mid_way_leaves_an_invalid_index_that_the_retry_recovers(registry_database, tmp_path):
    async def scenario(database):
        await ingest([{'score': value} for value in range(50)])
        submitted = json.loads(
            (await datasets.promote_field(1, 'score', USER, wait=False)).body,
        )
        namespace = await database.fetchval('SELECT current_schema()')
        code = """
import asyncio, os, sys
import asyncpg
from backend import index_jobs, job_queue
from backend.database import init_connection
async def run():
    pool = await asyncpg.create_pool(os.environ['FRIDGE_TEST_POSTGRES_DSN'], min_size=1, max_size=3,
        init=init_connection, server_settings={'search_path': sys.argv[1]+',public'})
    job = await job_queue.claim_job(pool, 'crash-test', kinds=['field_index_build'], lease_seconds=600)
    await index_jobs.field_handler(pool)(job)
asyncio.run(run())
"""
        blocker, transaction = await open_snapshot(database)
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', code, namespace)
        try:

            async def building():
                while await index_state(database) is not False:
                    assert process.returncode is None
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(building(), 20)
            process.kill()
            await process.wait()

            async def released():
                # The dead runner's backend notices its lost client (client_connection_check_interval)
                # and aborts the build mid-way, releasing the build lock and leaving the carcass.
                while not await lock_free(database):
                    await asyncio.sleep(0.2)

            await asyncio.wait_for(released(), 30)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await transaction.rollback()
            await database.pool.release(blocker)
        assert await index_state(database) is False and not await promoted(database)
        await database.execute(
            "UPDATE jobs SET lease_expires_at=NOW()-interval '10 minutes' WHERE id=$1::uuid",
            submitted['job_id'],
        )
        job = await job_helpers.claim(database, KIND)
        assert str(job['id']) == submitted['job_id'] and job['attempt'] == 2
        assert (await job_helpers.finish(database, KIND, job))['state'] == 'succeeded'
        assert await index_state(database) is True and await promoted(database)

    registry_database(scenario)


async def indisready(database, name=INDEX):
    return await database.fetchval(
        'SELECT i.indisready FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid '
        'WHERE c.relname=$1 AND c.relnamespace=current_schema()::regnamespace',
        name,
    )


async def samples_reader(database):
    """An old snapshot that has also locked `samples` (a pg_dump, a version capture, an export)."""
    connection = await database.pool.acquire()
    transaction = connection.transaction(isolation='repeatable_read')
    await transaction.start()
    await connection.fetchval('SELECT count(*) FROM samples')
    return connection, transaction


def test_budget_expiry_fails_the_build_once_and_the_sweep_removes_its_carcass(registry_database, monkeypatch):
    """The budget was asyncpg's client timeout, a transient error retried three times."""

    async def scenario(database):
        monkeypatch.setattr(index_jobs, 'FIELD_BUILD_SECONDS', 1)
        await ingest([{'score': value} for value in range(50)])
        await datasets.promote_field(1, 'score', USER, wait=False)
        reader, transaction = await samples_reader(database)
        try:
            job = await job_helpers.claim(database, KIND)
            started = time.monotonic()
            with pytest.raises(job_wait.JobConflict, match='budget') as raised:
                await index_jobs.field_handler(database.pool)(job)
            assert time.monotonic() - started < 30
            assert not job_queue.is_transient(raised.value)
            failed = await job_queue.fail_job(database.pool, job['id'], job['lease_token'], {'type': 'JobConflict'})
            assert failed['state'] == 'failed'
        finally:
            await transaction.rollback()
            await database.pool.release(reader)
        assert await lock_free(database)
        if await index_state(database) is not None:
            assert await index_jobs.sweep_invalid_indexes(database.pool) == [INDEX]
        assert await index_state(database) is None and not await promoted(database)

    registry_database(scenario)


def test_cancelled_build_carcass_is_swept_once_readers_finish(registry_database):
    """A cancelled build's cleanup gave up after 5 s and nothing ever retried it."""

    async def scenario(database):
        await ingest([{'score': value} for value in range(50)])
        reader, transaction = await samples_reader(database)
        try:
            async with job_helpers.runner(database, kinds=[KIND]):
                handle = json.loads((await datasets.promote_field(1, 'score', USER, wait=False)).body)

                async def catalogued():
                    while await index_state(database) is not False:
                        await asyncio.sleep(0.05)

                await asyncio.wait_for(catalogued(), 20)
                await job_queue.cancel_job(database.pool, handle['job_id'])

                async def stopped():
                    while await database.fetchval('SELECT state FROM jobs WHERE id=$1::uuid', handle['job_id']) != (
                        'cancelled'
                    ):
                        await asyncio.sleep(0.1)

                await asyncio.wait_for(stopped(), 60)
            # The reader still holds the table: the cleanup could not drop the maintained carcass.
            assert await index_state(database) is False and await indisready(database)
            assert await index_jobs.sweep_invalid_indexes(database.pool) == []
        finally:
            await transaction.rollback()
            await database.pool.release(reader)
        assert await index_jobs.sweep_invalid_indexes(database.pool) == [INDEX]
        assert await index_state(database) is None and not await promoted(database)

    registry_database(scenario)


def test_sweep_leaves_owned_and_in_progress_builds_alone(registry_database):
    async def scenario(database):
        await ingest([{'score': value} for value in range(20)])
        reader, transaction = await samples_reader(database)
        builder = await database.pool.acquire()
        builder_pid = await builder.fetchval('SELECT pg_backend_pid()')
        build = asyncio.create_task(
            builder.execute(f'CREATE INDEX CONCURRENTLY "{INDEX}" ON samples (((metadata->>\'score\')::float))'),
        )
        try:

            async def catalogued():
                while await index_state(database) is not False:
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(catalogued(), 20)
            # Another session's CREATE INDEX is still running: an invalid index, but not a carcass.
            assert await index_jobs.sweep_invalid_indexes(database.pool) == []
            assert await index_state(database) is False
            await database.execute('SELECT pg_cancel_backend($1)', builder_pid)
            await asyncio.gather(build, return_exceptions=True)
        finally:
            await transaction.rollback()
            await database.pool.release(reader)
            await database.pool.release(builder)
        holder = await database.pool.acquire()
        try:
            assert await holder.fetchval('SELECT pg_try_advisory_lock(20261017, 1)')
            assert await index_jobs.sweep_invalid_indexes(database.pool) == []  # an index operation owns it
            await holder.execute('SELECT pg_advisory_unlock(20261017, 1)')
        finally:
            await database.pool.release(holder)
        assert await index_jobs.sweep_invalid_indexes(database.pool) == [INDEX]

    registry_database(scenario)


def test_a_user_joining_another_users_build_reads_its_outcome(registry_database, monkeypatch):
    """The joiner followed a job whose result it could not read (SDK returned None)."""

    async def scenario(database):
        monkeypatch.setattr(jobs_router, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        monkeypatch.setattr(job_wait, 'ANSWER_SECONDS', 0.2)
        await database.execute('UPDATE datasets SET is_global=TRUE WHERE id=1')
        first = await database.fetchval("INSERT INTO users(username,role) VALUES('pa','power_user') RETURNING id")
        second = await database.fetchval("INSERT INTO users(username,role) VALUES('pb','power_user') RETURNING id")
        alice, bob = {'id': first, 'role': 'power_user'}, {'id': second, 'role': 'power_user'}
        await ingest([{'score': value} for value in range(10)])
        handle = json.loads((await datasets.promote_field(1, 'score', alice)).body)
        assert json.loads((await datasets.promote_field(1, 'score', bob)).body)['job_id'] == handle['job_id']
        await job_helpers.finish(database, KIND)
        status = await jobs_router.get_job(UUID(handle['job_id']), bob)
        assert status.result == {'message': 'Field score promoted successfully'}
        # Failures of joinable jobs show the requester-facing refusal, never raw internals.
        await database.execute(
            """UPDATE jobs SET state='failed',result=NULL,
               error='{"type":"RuntimeError","message":"connection to 10.0.0.5 lost"}' WHERE id=$1::uuid""",
            handle['job_id'],
        )
        hidden = await jobs_router.get_job(UUID(handle['job_id']), bob)
        assert hidden.error == {'type': 'JobFailed', 'message': 'The job failed; an administrator can see why'}
        assert (await jobs_router.get_job(UUID(handle['job_id']), alice)).error['type'] == 'RuntimeError'

    registry_database(scenario)
