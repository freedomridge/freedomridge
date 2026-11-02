"""Deletion admission, fencing and recovery on an explicitly isolated PostgreSQL database."""

import asyncio
import json
import os
import sys
import time
from unittest.mock import AsyncMock
from uuid import UUID

import asyncpg
import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.routing import APIRoute

from backend import access, dataset_deletion, job_queue
from backend.routers import datasets, jobs
from backend.tests import test_field_registry_postgres
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


async def claim(database):
    from backend import dataset_deletion

    return await job_queue.claim_job(
        database.pool,
        'deletion-test',
        kinds=['dataset_deletion'],
        policies={'dataset_deletion': dataset_deletion.POLICY},
    )


async def step(database, current):
    from backend import dataset_deletion

    result = await dataset_deletion.handler(database.pool)(current)
    return await job_queue.publish_step(
        database.pool,
        current['id'],
        current['lease_token'],
        checkpoint=result.checkpoint,
        done=result.done,
        total=result.total,
        message=result.message,
        result=result.result,
        finished=result.finished,
        publisher=result.publish,
    )


async def finish(database, current=None):
    current = current or await claim(database)
    for _ in range(10000):
        current = await step(database, current)
        if current['state'] == 'succeeded':
            return current
    pytest.fail('Deletion did not finish')


async def seed(database):
    ids = (await ingest([{'score': 1}, {'score': 2}, {'score': 3}]))['ids']
    await ingest([{'kept': True}], dataset_id=2)
    await database.execute(
        """INSERT INTO embeddings(sample_id,field_name,vector)
           SELECT id,'patch_'||n,'[1,0]'::vector FROM samples CROSS JOIN generate_series(1,30) n WHERE dataset_id=1""",
    )
    await database.execute(
        "INSERT INTO sample_hashes(sample_id,field_name,hash_hex) SELECT id,'hash','abcd' FROM samples",
    )
    concept_id = await database.fetchval(
        """INSERT INTO embedding_concepts(dataset_id,name,embedding_field,kind,weight)
           VALUES(1,'test','patch_1','seed','[1,0]') RETURNING id""",
    )
    await database.execute('SELECT score_concept($1)', concept_id)
    await database.execute(
        """INSERT INTO concept_score_pending(concept_id,block_id) VALUES($1,0);
        """,
        concept_id,
    )
    await database.execute(
        "INSERT INTO concept_score_count_deltas(concept_id,delta) VALUES($1,3) ON CONFLICT DO NOTHING",
        concept_id,
    )
    version_id = await database.fetchval(
        "INSERT INTO dataset_versions(dataset_id,name,sample_count) VALUES(1,'saved',3) RETURNING id",
    )
    await database.execute(
        """INSERT INTO version_snapshots(version_id,sample_id,metadata_hash)
           SELECT $1,id,'hash' FROM samples WHERE dataset_id=1""",
        version_id,
    )
    return ids, concept_id, version_id


def test_delete_returns_202_one_job_and_keeps_dataset(registry_database):
    async def scenario(database):
        from backend.routers.auth import require_role

        await ingest([{}])
        app = FastAPI()
        app.include_router(datasets.router, prefix='/api')
        app.dependency_overrides[require_role('admin')] = lambda: USER
        # require_role returns a closure per call; use the dependency declared on the route.
        route = next(
            route
            for route in datasets.router.routes
            if isinstance(route, APIRoute)
            and route.path == '/datasets/{id}'
            and route.methods
            and 'DELETE' in route.methods
        )
        dependency = route.dependant.dependencies[0].call
        assert dependency is not None
        app.dependency_overrides[dependency] = lambda: USER
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.delete('/api/datasets/1')
            assert response.status_code == 202
            submitted = response.json()
            again = await client.delete('/api/datasets/1')
            assert again.json()['job_id'] == submitted['job_id']
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id=1') == 1
        row = await database.fetchrow('SELECT deleting,deletion_job_id FROM datasets WHERE id=1')
        assert row['deleting'] and str(row['deletion_job_id']) == submitted['job_id']
        current = await claim(database)
        assert current['kind'] == 'dataset_deletion' and current['lane'] == 'user'
        assert (await datasets.delete_dataset(1, USER))['job_id'] == submitted['job_id']

    registry_database(scenario)


def test_bounded_fanout_delete_keeps_other_dataset_and_retained_job(registry_database, monkeypatch):
    from backend import dataset_deletion

    async def scenario(database):
        monkeypatch.setattr(dataset_deletion, 'BATCH_SIZE', 7)
        monkeypatch.setattr(dataset_deletion, 'BYTE_BUDGET', 400)
        _, concept_id, _ = await seed(database)
        await database.execute(
            "CREATE INDEX idx_samples_meta_1_test ON samples ((metadata->>'score')) WHERE dataset_id=1",
        )
        await database.execute(
            "CREATE INDEX idx_samples_meta_2_test ON samples ((metadata->>'kept')) WHERE dataset_id=2",
        )
        submitted = await datasets.delete_dataset(1, USER)
        current = await claim(database)
        units = 0
        while current['state'] != 'succeeded':
            current = await step(database, current)
            assert current['checkpoint'].get('last_unit_rows', 0) <= 7
            assert current['checkpoint'].get('last_unit_bytes', 0) <= 400
            units += 1
        assert units > 20
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM datasets WHERE id=1)')
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id=2') == 1
        assert not await database.fetchval(
            'SELECT EXISTS(SELECT 1 FROM concept_score_pending WHERE concept_id=$1)',
            concept_id,
        )
        assert not await database.fetchval(
            'SELECT EXISTS(SELECT 1 FROM concept_score_count_deltas WHERE concept_id=$1)',
            concept_id,
        )
        assert await database.fetchval("SELECT to_regclass('idx_samples_meta_1_test')") is None
        assert await database.fetchval("SELECT to_regclass('idx_samples_meta_2_test')") is not None
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        status = await jobs.get_job(UUID(submitted['job_id']), USER)
        assert status.state == 'succeeded'
        assert status.result == {'message': 'Dataset 1 deleted successfully'}
        assert status.dataset_id == 1

    registry_database(scenario)


def test_cancel_resume_retains_checkpoint_and_same_job(registry_database, monkeypatch):
    from backend import dataset_deletion

    async def scenario(database):
        monkeypatch.setattr(dataset_deletion, 'BATCH_SIZE', 2)
        await seed(database)
        submitted = await datasets.delete_dataset(1, USER)
        current = await claim(database)
        current = await step(database, current)
        checkpoint = current['checkpoint']
        assert checkpoint['deleted_rows'] == 2
        await job_queue.cancel_job(database.pool, current['id'])
        current = await step(database, current)
        assert current['state'] == 'cancelled'
        assert await database.fetchval('SELECT deleting FROM datasets WHERE id=1')
        assert (await datasets.delete_dataset(1, USER))['state'] == 'cancelled'
        resumed = await datasets.resume_dataset_deletion(1, USER)
        assert resumed['job_id'] == submitted['job_id']
        current = await claim(database)
        assert current['checkpoint'] == checkpoint
        await finish(database, current)

    registry_database(scenario)


def test_key_cursors_advance_and_reset_for_each_parent(registry_database, monkeypatch):
    from backend import dataset_deletion

    async def scenario(database):
        monkeypatch.setattr(dataset_deletion, 'BATCH_SIZE', 2)
        ids, concept_id, version_id = await seed(database)
        # Names, not IDs, determine parent order. A higher ID must not skip the earlier parent's children.
        second = await database.fetchval(
            """INSERT INTO embedding_concepts(dataset_id,name,embedding_field,kind,weight)
               VALUES(1,'aaa','patch_1','seed','[1,0]') RETURNING id""",
        )
        await database.execute('SELECT score_concept($1)', second)
        second_version = await database.fetchval(
            "INSERT INTO dataset_versions(dataset_id,name,sample_count) VALUES(1,'aaa',3) RETURNING id",
        )
        await database.execute(
            """INSERT INTO version_snapshots(version_id,sample_id,metadata_hash)
               SELECT $1,id,'hash' FROM samples WHERE dataset_id=1""",
            second_version,
        )
        await datasets.delete_dataset(1, USER)
        result = await finish(database)
        cursors = result['checkpoint']['cursors']
        assert cursors['samples']['key']['id'] == max(ids)
        assert cursors['embeddings']['key']['sample_id'] == max(ids)
        assert cursors['embedding_concept_scores'] == {
            'scope': concept_id,
            'key': {'concept_id': concept_id, 'sample_id': max(ids)},
        }
        assert cursors['version_snapshots'] == {
            'scope': version_id,
            'key': {'version_id': version_id, 'sample_id': max(ids)},
        }

    registry_database(scenario)


def test_oversize_row_fails_safely_and_failed_deletion_can_resume(registry_database, monkeypatch):
    from backend import dataset_deletion

    async def scenario(database):
        await ingest([{'wide': ''.join(str(number) for number in range(1000))}])
        await datasets.delete_dataset(1, USER)
        current = await claim(database)
        while current['checkpoint'].get('phase') != 'samples':
            current = await step(database, current)
        monkeypatch.setattr(dataset_deletion, 'BYTE_BUDGET', 256)
        with pytest.raises(ValueError, match='single row exceeds'):
            await step(database, current)
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id=1') == 1
        saved = current['checkpoint']
        await job_queue.fail_job(database.pool, current['id'], current['lease_token'], {'type': 'ValueError'})
        resumed = await datasets.resume_dataset_deletion(1, USER)
        assert resumed['job_id'] == str(current['id'])
        current = await claim(database)
        assert current['checkpoint'] == saved
        monkeypatch.setattr(dataset_deletion, 'BYTE_BUDGET', 8 * 1024 * 1024)
        await finish(database, current)

    registry_database(scenario)


def test_lost_lease_cannot_delete_or_advance(registry_database):
    from backend import dataset_deletion

    async def scenario(database):
        await seed(database)
        await datasets.delete_dataset(1, USER)
        current = await claim(database)
        result = await dataset_deletion.handler(database.pool)(current)
        await database.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            current['id'],
        )
        before = await database.fetchval('SELECT count(*) FROM embedding_concept_scores')
        with pytest.raises(job_queue.LeaseLost):
            await job_queue.publish_step(
                database.pool,
                current['id'],
                current['lease_token'],
                publisher=result.publish,
                checkpoint=result.checkpoint,
            )
        assert await database.fetchval('SELECT count(*) FROM embedding_concept_scores') == before
        assert await database.fetchval('SELECT checkpoint FROM jobs WHERE id=$1', current['id']) == {}

    registry_database(scenario)


def test_index_drop_is_owned_fenced_and_repeatable_after_crash(registry_database):
    from backend import dataset_deletion

    async def scenario(database):
        await database.execute('CREATE INDEX idx_samples_meta_1_retry ON samples(id) WHERE dataset_id=1')
        await datasets.delete_dataset(1, USER)
        current = await claim(database)
        while current['checkpoint'].get('phase') != 'indexes':
            current = await step(database, current)
        await database.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '10 minutes' WHERE id=$1",
            current['id'],
        )
        with pytest.raises(job_queue.LeaseLost):
            await dataset_deletion.handler(database.pool)(current)
        assert await database.fetchval("SELECT to_regclass('idx_samples_meta_1_retry')") is not None
        current = await claim(database)
        await dataset_deletion.handler(database.pool)(current)  # Drop succeeds; its checkpoint is deliberately lost.
        assert await database.fetchval("SELECT to_regclass('idx_samples_meta_1_retry')") is None
        await database.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '10 minutes' WHERE id=$1",
            current['id'],
        )
        await finish(database)

    registry_database(scenario)


def test_all_write_families_reject_deleting_and_old_snapshot(registry_database):
    async def scenario(database):
        ids, concept_id, version_id = await seed(database)
        await database.execute("INSERT INTO region_selections(token,sample_id) VALUES('expiring',$1)", ids[0])
        await database.execute("INSERT INTO quality_dup_pairs(token,a,b) VALUES('expiring',$1,$2)", ids[0], ids[1])
        async with database.pool.acquire() as old:
            async with old.transaction(isolation='repeatable_read'):
                await old.fetchval('SELECT count(*) FROM datasets')
                await datasets.delete_dataset(1, USER)
                with pytest.raises(asyncpg.SerializationError):
                    await old.execute("UPDATE samples SET tags=ARRAY['old'] WHERE id=$1", ids[0])
        statements = [
            ("INSERT INTO samples(dataset_id,filepath,media_type) VALUES(1,'late','image')", []),
            ("UPDATE samples SET tags=ARRAY['late'] WHERE id=$1", [ids[0]]),
            ("INSERT INTO embeddings(sample_id,field_name,vector) VALUES($1,'late','[1,0]')", [ids[0]]),
            ("UPDATE sample_hashes SET hash_hex='bad' WHERE sample_id=$1", [ids[0]]),
            ("UPDATE dataset_fields SET display_type='enum' WHERE dataset_id=1", []),
            ("UPDATE embedding_concepts SET threshold=0.2 WHERE id=$1", [concept_id]),
            ("UPDATE version_snapshots SET metadata_hash='late' WHERE version_id=$1", [version_id]),
            ("INSERT INTO dataset_versions(dataset_id,name,sample_count) VALUES(1,'late',0)", []),
            ("UPDATE datasets SET description='late' WHERE id=1", []),
            ("DELETE FROM samples WHERE id=$1", [ids[0]]),
        ]
        for sql, args in statements:
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being deleted'):
                await database.execute(sql, *args)
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being deleted'):
            await job_queue.enqueue_job(database.pool, 'other', dataset_id=1)
        # A cancelled deletion must not prevent global garbage collection of temporary query results.
        await database.execute("DELETE FROM region_selections WHERE token='expiring'")
        await database.execute("DELETE FROM quality_dup_pairs WHERE token='expiring'")
        await ingest([{'ok': 1}], dataset_id=2, prefix='later')

    registry_database(scenario)


async def wait_for_row_lock(database, task, pid):
    async with asyncio.timeout(3):
        while not await database.fetchval('SELECT cardinality(pg_blocking_pids($1))>0', pid):
            if task.done():
                await task
                pytest.fail('Writer did not wait for the dataset row')
            await asyncio.sleep(0.01)


def test_normal_writer_waits_for_row_lock_and_keeps_database_timeout(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}])
        async with database.pool.acquire() as holder, database.pool.acquire() as writer:
            pending = None
            try:
                async with holder.transaction():
                    await holder.execute('SELECT id FROM datasets WHERE id=1 FOR UPDATE')
                    pid = await writer.fetchval('SELECT pg_backend_pid()')
                    pending = asyncio.create_task(
                        writer.execute("UPDATE dataset_fields SET display_type='enum' WHERE dataset_id=1"),
                    )
                    await wait_for_row_lock(database, pending, pid)
            finally:
                if pending is not None:
                    await asyncio.wait_for(pending, 3)
            async with holder.transaction(), writer.transaction():
                await holder.execute('SELECT id FROM datasets WHERE id=1 FOR UPDATE')
                await writer.execute("SET LOCAL lock_timeout='50ms'")
                with pytest.raises(asyncpg.LockNotAvailableError, match='lock timeout'):
                    await writer.execute("UPDATE dataset_fields SET display_type='string' WHERE dataset_id=1")

    registry_database(scenario)


def test_normal_writer_does_not_take_an_extra_lifecycle_gate(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}])
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('SELECT pg_advisory_xact_lock(20261016,1)')
            await holder.execute("UPDATE datasets SET description='maintenance' WHERE id=1")
            # Existing writers that need the lifecycle gate take it before their row locks.
            # A raw SQL update and queue submission do not add a second acquisition here.
            await database.execute("UPDATE dataset_fields SET display_type='enum' WHERE dataset_id=1")
            await job_queue.enqueue_job(database.pool, 'normal', dataset_id=1)

    registry_database(scenario)


@pytest.mark.parametrize('commit_seal', [False, True])
def test_writer_waiting_on_seal_observes_commit_or_rollback(registry_database, commit_seal):
    from backend import dataset_deletion

    async def scenario(database):
        await ingest([{'score': 1}])
        async with database.pool.acquire() as seal, database.pool.acquire() as writer:
            transaction = seal.transaction()
            await transaction.start()
            pending = None
            try:
                await dataset_deletion.submit(seal, 1, USER['id'])
                pid = await writer.fetchval('SELECT pg_backend_pid()')
                pending = asyncio.create_task(
                    writer.execute("UPDATE dataset_fields SET display_type='enum' WHERE dataset_id=1"),
                )
                await wait_for_row_lock(database, pending, pid)
            finally:
                if commit_seal:
                    await transaction.commit()
                else:
                    await transaction.rollback()
                if pending is not None:
                    outcome = (await asyncio.gather(pending, return_exceptions=True))[0]
            if commit_seal:
                assert isinstance(outcome, asyncpg.ObjectNotInPrerequisiteStateError)
                assert 'being deleted' in str(outcome)
            else:
                assert outcome == 'UPDATE 1'

    registry_database(scenario)


def test_writer_row_lock_prevents_sealing_until_it_commits(registry_database, monkeypatch):
    monkeypatch.setattr(dataset_deletion, 'ADMISSION_WAIT_SECONDS', 0.3)

    async def scenario(database):
        await ingest([{'score': 1}])
        async with database.pool.acquire() as writer, writer.transaction():
            await writer.execute("UPDATE dataset_fields SET display_type='enum' WHERE dataset_id=1")
            with pytest.raises(HTTPException) as error:
                await datasets.delete_dataset(1, USER)
            assert error.value.status_code == 409
            assert not await writer.fetchval('SELECT deleting FROM datasets WHERE id=1')
        assert (await datasets.delete_dataset(1, USER))['state'] == 'queued'

    registry_database(scenario)


def test_http_writes_to_deleting_dataset_return_409(registry_database, monkeypatch):
    from backend.main import dataset_lifecycle_error
    from backend.routers import auth, concepts, embeddings, hashes, quality, versions

    async def scenario(database):
        await seed(database)
        await datasets.delete_dataset(1, USER)
        monkeypatch.setattr(access, 'db', database)
        monkeypatch.setattr(auth, 'get_optional_user', AsyncMock(return_value=USER))
        app = FastAPI()
        app.add_exception_handler(asyncpg.ObjectNotInPrerequisiteStateError, dataset_lifecycle_error)
        routers = (
            datasets.router,
            concepts.router,
            embeddings.router,
            hashes.router,
            versions.router,
            quality.router,
        )
        for router in routers:
            app.include_router(router, prefix='/api')
        # Exercise every actual route dependency, including writes with empty/no-op payloads.
        guarded, reads = 0, set()
        for route in [route for router in routers for route in router.routes]:
            if not isinstance(route, APIRoute) or not route.methods or not (route.methods - {'GET', 'HEAD', 'OPTIONS'}):
                continue
            if any(dependency.call is access.require_dataset_access for dependency in route.dependant.dependencies):
                request = Request(
                    {
                        'type': 'http',
                        'method': next(iter(route.methods)),
                        'path_params': {'dataset_id': '1'},
                        'endpoint': route.endpoint,
                    },
                )
                if route.endpoint.__name__ in access._READ_ONLY_DATASET_POSTS:
                    assert (await access.require_dataset_access(request))['deleting']
                    reads.add(route.endpoint.__name__)
                    continue
                with pytest.raises(HTTPException) as error:
                    await access.require_dataset_access(request)
                assert error.value.status_code == 409 and 'being deleted' in error.value.detail
                guarded += 1
        assert guarded > 20
        assert reads == access._READ_ONLY_DATASET_POSTS

        @app.patch('/direct-write')
        async def direct_write():
            await database.execute("UPDATE datasets SET description='late' WHERE id=1")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.patch('/direct-write')
            assert response.status_code == 409 and 'being deleted' in response.json()['detail']

    registry_database(scenario)


def test_deletion_cancels_lane_owner_and_stops_other_publication(registry_database):
    async def scenario(database):
        owner = await job_queue.enqueue_job(database.pool, 'other', dataset_id=1)
        running = await job_queue.claim_job(database.pool, 'other-worker', kinds=['other'])
        assert running is not None
        queued = await job_queue.enqueue_job(database.pool, 'unsupported', dataset_id=1)
        await datasets.delete_dataset(1, USER)
        assert await database.fetchval('SELECT cancel_requested FROM job_controls WHERE job_id=$1', owner['id'])
        # Even a caller that clears its cancel request cannot publish into the sealed dataset.
        await database.execute('UPDATE job_controls SET cancel_requested=FALSE WHERE job_id=$1', owner['id'])
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await job_queue.publish_step(database.pool, running['id'], running['lease_token'], finished=True)
        await database.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            owner['id'],
        )
        assert (await claim(database))['kind'] == 'dataset_deletion'
        for job in (owner, queued):
            assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'cancelled'

    registry_database(scenario)


def test_inflight_ingest_defers_admission_without_blocking_other_ingest(registry_database, monkeypatch):
    monkeypatch.setattr(dataset_deletion, 'ADMISSION_WAIT_SECONDS', 0.3)

    async def scenario(database):
        async with database.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute('SELECT pg_advisory_xact_lock_shared(20261016,1)')
                with pytest.raises(HTTPException) as error:
                    await datasets.delete_dataset(1, USER)
                assert error.value.status_code == 409
                await ingest([{}], dataset_id=2)
            async with connection.transaction():
                await connection.execute('SELECT id FROM datasets WHERE id=1 FOR UPDATE')
                with pytest.raises(HTTPException) as error:
                    await datasets.delete_dataset(1, USER)
                assert error.value.status_code == 409
        await datasets.delete_dataset(1, USER)
        current = await claim(database)
        while current['state'] != 'succeeded':
            current, _ = await asyncio.gather(
                step(database, current),
                ingest([{}], dataset_id=2, prefix=f'unit-{current["checkpoint"]}'),
            )

    registry_database(scenario)


def test_kill_restart_resumes_committed_unit(registry_database, tmp_path):
    async def scenario(database):
        await seed(database)
        submitted = await datasets.delete_dataset(1, USER)
        namespace = await database.fetchval('SELECT current_schema()')
        ready = tmp_path / 'ready'
        code = """
import asyncio, os, sys
from pathlib import Path
import asyncpg
from backend import dataset_deletion, job_queue
from backend.database import init_connection
async def run():
    pool = await asyncpg.create_pool(os.environ['FRIDGE_TEST_POSTGRES_DSN'], min_size=1, max_size=2,
        init=init_connection, server_settings={'search_path': sys.argv[1]+',public'})
    job = await job_queue.claim_job(pool,'crash-test',kinds=['dataset_deletion'])
    dataset_deletion.BATCH_SIZE=2
    result = await dataset_deletion.handler(pool)(job)
    await job_queue.publish_step(pool,job['id'],job['lease_token'],publisher=result.publish,
        checkpoint=result.checkpoint,done=result.done)
    Path(sys.argv[2]).write_text(str(job['id']))
    await asyncio.Event().wait()
asyncio.run(run())
"""
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', code, namespace, str(ready))
        try:

            async def wait_ready():
                while not ready.exists():
                    assert process.returncode is None
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_ready(), 10)
            process.kill()
            await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        job_id = UUID(submitted['job_id'])
        saved = await database.fetchval('SELECT checkpoint FROM jobs WHERE id=$1', job_id)
        assert saved['deleted_rows'] == 2
        await database.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '10 minutes' WHERE id=$1",
            job_id,
        )
        current = await claim(database)
        assert current['checkpoint'] == saved
        assert current['attempt'] == 2
        await finish(database, current)

    registry_database(scenario)


def test_schedulers_skip_deleting_dataset(registry_database):
    from backend import concept_jobs, statistics

    async def scenario(database):
        await seed(database)
        await database.execute('UPDATE embedding_concepts SET auto_rescore=FALSE WHERE dataset_id=1')
        await datasets.delete_dataset(1, USER)
        assert await concept_jobs.schedule_concepts(database.pool) == []
        scheduled = await statistics.schedule_statistics(database.pool)
        assert scheduled and all(job['dataset_id'] == 2 for job in scheduled)
        await finish(database)
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM concept_embedding_pending WHERE dataset_id=1)')
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=1)')

    registry_database(scenario)


@pytest.mark.skipif(os.getenv('FRIDGE_TEST_DELETION_SCALE') != '1', reason='Opt-in 500k, two-vector-field diagnostic')
def test_delete_500k_two_vector_fields(registry_database):
    """Use an empty 8 GiB disposable database; old deletion exceeds its temporary space."""

    async def scenario(database):
        async with database.pool.acquire() as connection:
            await connection.execute("SET statement_timeout='120s'")
            # Keep dead index entries so the checkpoint seeks, not vacuum timing, bound later batches.
            quiet = (
                'samples',
                'embeddings',
                'embedding_concept_scores',
                'embedding_coordinates',
                'embedding_projection_pending',
                'embedding_viz_rank',
                'duplicate_members',
                'sample_dup_secondary',
            )
            for table in quiet:
                await connection.execute(f'ALTER TABLE {table} SET (autovacuum_enabled=false)')
            await connection.execute(
                """INSERT INTO samples(dataset_id,filepath,media_type,metadata)
                   SELECT 1,'scale/'||i,'image',jsonb_build_object('score',i%1000)
                   FROM generate_series(1,500000) i""",
            )
            for field, dimensions in (('clip', 512), ('dino', 1024)):
                vector = '[' + ','.join(str(((i * 7907) % 65537) / 65537) for i in range(dimensions)) + ']'
                for lower in range(0, 500000, 50000):
                    await connection.execute(
                        """INSERT INTO embeddings(sample_id,field_name,vector)
                           SELECT id,$1,$2::vector FROM samples WHERE dataset_id=1 AND id>$3 AND id<=$3+50000""",
                        field,
                        vector,
                        lower,
                    )
                concept = await connection.fetchval(
                    """INSERT INTO embedding_concepts(dataset_id,name,embedding_field,kind,weight,auto_rescore)
                       VALUES(1,$1,$1,'seed',$2,FALSE) RETURNING id""",
                    field,
                    vector,
                )
                for lower in range(0, 500000, 50000):
                    await connection.execute(
                        """INSERT INTO embedding_concept_scores(concept_id,sample_id,score)
                           SELECT $1,id,0.5 FROM samples WHERE dataset_id=1 AND id>$2 AND id<=$2+50000""",
                        concept,
                        lower,
                    )
            await connection.execute(
                """INSERT INTO embedding_coordinates
                   SELECT sample_id,field_name,sample_id%1000,sample_id/1000 FROM embeddings
                   WHERE field_name='clip' OR sample_id%2=0;
                   SELECT recompute_viz_rank(1,'clip'); SELECT recompute_viz_rank(1,'dino');
                   INSERT INTO sample_hashes(sample_id,field_name,hash_hex)
                   SELECT id,'hash',(id%100)::text FROM samples WHERE dataset_id=1;
                   INSERT INTO duplicate_configs(dataset_id,config,initialized)
                   VALUES(1,'hash',TRUE),(1,'_any',TRUE);
                   INSERT INTO duplicate_members
                   SELECT 1,c.config,s.id,(s.id%100)::text FROM samples s CROSS JOIN duplicate_configs c
                   WHERE s.dataset_id=1;
                   INSERT INTO sample_dup_secondary(dataset_id,hash_config,sample_id)
                   SELECT 1,c.config,s.id FROM samples s CROSS JOIN duplicate_configs c
                   WHERE s.dataset_id=1 AND s.id>100""",
            )
            # Collect the statistics autovacuum would have; without them the planner can sort a
            # whole table for one cursor unit (1.5 s on 1M duplicate members).
            await connection.execute(f'ANALYZE {",".join(quiet)}')
            await connection.execute('RESET statement_timeout')
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id=1') == 500000
        assert await database.fetchval('SELECT count(*) FROM embeddings') == 1000000
        print(
            json.dumps({'seed_bytes': await database.fetchval('SELECT pg_database_size(current_database())')}),
            flush=True,
        )
        started = time.monotonic()
        submitted = await datasets.delete_dataset(1, USER)
        assert 'job_id' in submitted
        current = await claim(database)
        maximum, units, ingests = 0.0, 0, []
        slowest_message = None
        while current['state'] != 'succeeded':
            if units % 100 == 0:
                await job_queue.heartbeat_job(database.pool, current['id'], current['lease_token'])
                before = time.monotonic()
                await ingest([{}], dataset_id=2, prefix=f'concurrent-{units}')
                ingests.append(time.monotonic() - before)
            before = time.monotonic()
            current = await step(database, current)
            elapsed = time.monotonic() - before
            if elapsed > maximum:
                maximum = elapsed
                slowest_message = current['message']
            assert current['checkpoint'].get('last_unit_rows', 0) <= 1000
            assert current['checkpoint'].get('last_unit_bytes', 0) <= 8 * 1024 * 1024
            units += 1
        print(
            json.dumps(
                {
                    'delete_seconds': time.monotonic() - started,
                    'units': units,
                    'max_unit_seconds': maximum,
                    'slowest_unit': slowest_message,
                    'other_ingests': len(ingests),
                    'max_ingest_seconds': max(ingests),
                },
            ),
            flush=True,
        )
        for table in (
            'embedding_coordinates',
            'embedding_projection_pending',
            'embedding_viz_rank',
            'embedding_viz_meta',
            'duplicate_configs',
            'duplicate_members',
            'duplicate_pending',
            'sample_dup_secondary',
        ):
            assert await database.fetchval(f'SELECT count(*) FROM {table}') == 0
        assert maximum < 30
        assert max(ingests) < 5
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id=2') == len(ingests)
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM datasets WHERE id=1)')
        retained = await database.fetchrow('SELECT state,result FROM jobs WHERE id=$1', current['id'])
        assert retained['state'] == 'succeeded'
        assert retained['result'] == {'message': 'Dataset 1 deleted successfully'}

    registry_database(scenario)


def test_admission_waits_for_a_brief_holder_instead_of_refusing(registry_database):
    # A background unit or an ingest batch holds the dataset for a moment; main's synchronous
    # delete waited for it, so a click on Delete must not bounce with a 409.
    async def scenario(database):
        async with database.pool.acquire() as holder:
            transaction = holder.transaction()
            await transaction.start()
            await holder.execute('SELECT pg_advisory_xact_lock_shared(20261016,1)')
            release = asyncio.get_running_loop().call_later(0.5, lambda: asyncio.ensure_future(transaction.rollback()))
            try:
                assert (await datasets.delete_dataset(1, USER))['state'] == 'queued'
            finally:
                release.cancel()
        assert await database.fetchval('SELECT deleting FROM datasets WHERE id=1')

    registry_database(scenario)
