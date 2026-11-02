"""Regression coverage for incremental duplicates."""

import json
import time
import uuid

import pytest

from backend import duplicate_jobs, job_queue
from backend.tests import test_field_registry_postgres as helpers
from backend.tests.test_concept_jobs import publish
from backend.tests.test_duplicate_jobs import assert_oracle, bootstrap, drain, finish, hidden, sample

registry_database = helpers.registry_database
pytestmark = helpers.pytestmark


def test_dirty_page_repairs_same_id_recreation_without_rewriting_memberships(registry_database):
    async def scenario(database):
        await sample(database, 1)
        await sample(database, 2)
        await bootstrap(database)
        await drain(database)
        before = await database.fetch(
            'SELECT config,sample_id,key,ctid::text FROM duplicate_members ORDER BY 1,2,3',
        )
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute('DELETE FROM samples WHERE id=2')
            await connection.execute(
                "INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES(2,1,'2','image')",
            )
            await connection.execute(
                "INSERT INTO sample_hashes(sample_id,field_name,hash_hex) VALUES(2,'a','x'),(2,'b','y')",
            )
        await drain(database)
        await assert_oracle(database)
        assert (
            await database.fetch(
                'SELECT config,sample_id,key,ctid::text FROM duplicate_members ORDER BY 1,2,3',
            )
            == before
        )

    registry_database(scenario)


@pytest.mark.parametrize('state', ['cancelled', 'failed'])
@pytest.mark.parametrize('table', ['samples CASCADE', 'sample_hashes'])
def test_truncate_discards_interrupted_bootstrap_checkpoint(registry_database, monkeypatch, state, table):
    async def scenario(database):
        monkeypatch.setattr(duplicate_jobs, 'BATCH_SIZE', 2)
        for sid in range(1, 7):
            await sample(database, sid)
        await bootstrap(database, ('a', '_any'))
        await drain(database)
        for config in ('a', '_any'):
            job = await duplicate_jobs.request_refresh(database.pool, 1, config)
            claim = await job_queue.claim_job(database.pool, 'review', kinds=[duplicate_jobs.KIND])
            await publish(database, claim, await duplicate_jobs.handler(database.pool)(claim))
            assert (await database.fetchval('SELECT checkpoint FROM jobs WHERE id=$1', job['id']))['after'] == 2
            if state == 'cancelled':
                await job_queue.cancel_job(database.pool, job['id'])
            else:
                claim = await job_queue.claim_job(database.pool, 'review', kinds=[duplicate_jobs.KIND])
                assert claim is not None
                await job_queue.fail_job(database.pool, claim['id'], claim['lease_token'], {'type': 'ValueError'})
                await database.execute(
                    "UPDATE jobs SET finished_at=now()-interval '10 minutes' WHERE id=$1",
                    job['id'],
                )
        await database.execute('TRUNCATE ' + table)
        for sid in range(2001, 2007):
            await sample(database, sid)
        for _ in range(10):
            jobs = await duplicate_jobs.schedule_duplicates(database.pool)
            if not jobs:
                break
            for job in jobs:
                await finish(database, job)
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 0
        assert await database.fetchval('SELECT count(*) FROM duplicate_members WHERE sample_id<2000') == 0
        await assert_oracle(database, ('a', '_any'))

    registry_database(scenario)


def test_oversized_sample_is_skipped_without_blocking_other_blocks_or_configs(registry_database, caplog):
    async def scenario(database):
        for sid in (1, 2, 3000, 4000):
            await sample(database, sid)
        await bootstrap(database)
        await drain(database)
        await database.execute(
            "UPDATE sample_hashes SET hash_hex=repeat('z',2100) WHERE sample_id=1 AND field_name='a'",
        )
        await database.execute("UPDATE sample_hashes SET hash_hex='unique' WHERE sample_id=3000")
        await drain(database)
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 0
        for config in ('a', '_all', '_any'):
            assert (
                await database.fetchval(
                    'SELECT count(*) FROM duplicate_members WHERE sample_id=1 AND config=$1',
                    config,
                )
                == 0
            )
            assert await hidden(database, config) == {4000}
        await assert_oracle(database, ('b',))
        assert '2000-byte' in caplog.text and 'samples [1]' in caplog.text
        # Full builds also finish, and shortening the key restores normal membership.
        await bootstrap(database)
        await database.execute("UPDATE sample_hashes SET hash_hex='x' WHERE sample_id=1 AND field_name='a'")
        await drain(database)
        await assert_oracle(database)

    registry_database(scenario)


def test_restart_request_survives_concurrent_bootstrap_completion(registry_database):
    async def scenario(database):
        await sample(database, 1)
        job = await duplicate_jobs.request_refresh(database.pool, 1, 'a')
        # A truncate scheduler can mark the config after a concurrent request submitted its job.
        await database.execute('UPDATE duplicate_configs SET restart_required=TRUE')
        await finish(database, job)
        jobs = await duplicate_jobs.schedule_duplicates(database.pool)
        assert len(jobs) == 1 and jobs[0]['barrier']['full']
        assert jobs[0]['checkpoint'] == {}
        assert not await database.fetchval('SELECT restart_required FROM duplicate_configs')
        await finish(database, jobs[0])
        await drain(database)
        await assert_oracle(database, ('a',))

    registry_database(scenario)


def test_dataset_deleted_between_measurement_and_publication_finishes_quietly(registry_database):
    async def scenario(database):
        await sample(database, 1)
        await sample(database, 2, dataset_id=2)
        await bootstrap(database, ('a',), dataset_id=2)
        job = await duplicate_jobs.request_refresh(database.pool, 1, 'a')
        claim = await job_queue.claim_job(database.pool, 'review', kinds=[duplicate_jobs.KIND])
        step = await duplicate_jobs.handler(database.pool)(claim)
        await database.execute('DELETE FROM datasets WHERE id=1')
        result = await publish(database, claim, step)
        assert result['state'] == 'cancelled'
        assert result['error'] is None
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'cancelled'
        assert await database.fetchval('SELECT count(*) FROM duplicate_members WHERE dataset_id=1') == 0
        await assert_oracle(database, ('a',), dataset_id=2)

    registry_database(scenario)


def test_refresh_answers_within_five_seconds_and_keeps_the_job(registry_database, monkeypatch):
    """Formerly a 540 s wait below the proxies' 600 s; now the five-second rule, then 202."""

    async def scenario(database):
        from backend import job_wait
        from backend.routers import hashes

        monkeypatch.setattr(hashes, 'db', database)
        await sample(database, 1)
        assert job_wait.ANSWER_SECONDS <= 5
        monkeypatch.setattr(job_wait, 'ANSWER_SECONDS', 0.3)
        started = time.monotonic()
        response = await hashes.refresh_duplicates(1, 'a', user={'id': 1, 'role': 'admin'})
        assert time.monotonic() - started < 3
        assert response.status_code == 202
        handle = json.loads(response.body)
        job = await finish(database, {'id': uuid.UUID(handle['job_id'])})
        assert job['result'] == {'config': 'a', 'hidden': 0}

    registry_database(scenario)


def test_metadata_updates_do_not_materialize_sample_transition_tables(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO samples(id,dataset_id,filepath,media_type,metadata)
               SELECT n,1,n::text,'image',jsonb_build_object('blob',repeat(md5(n::text),50))
               FROM generate_series(1,1000) n""",
        )
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SET LOCAL work_mem='64kB'")
            plan = await connection.fetchval(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) UPDATE samples SET tags=tags",
            )
        assert json.loads(plan)[0]['Plan']['Temp Written Blocks'] == 0
        # Column-limited row recording still handles genuine moves and ignores no-op assignments.
        await database.execute('UPDATE samples SET dataset_id=dataset_id')
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 0
        await database.execute('UPDATE samples SET dataset_id=2 WHERE id=1')
        assert {
            tuple(row)
            for row in await database.fetch(
                'SELECT dataset_id,block_id FROM duplicate_pending',
            )
        } == {(1, 0), (2, 0)}

    registry_database(scenario)


def test_hash_recorder_skips_empty_statements_and_caches_singletons(registry_database):
    async def scenario(database):
        # The recorder policy must keep static singleton plans alongside bulk EXECUTE.
        body = await database.fetchval(
            "SELECT prosrc FROM pg_proc WHERE oid='record_duplicate_hash_work()'::regprocedure",
        )
        assert 'LIMIT 2' in body
        assert 'changed_count = 0' in body and 'changed_count = 1' in body
        assert body.count('EXECUTE $q$') == 3
        for sid in (1, 2):
            await sample(database, sid)
        await database.execute('DELETE FROM duplicate_pending')
        for _ in range(8):
            await database.execute(
                """INSERT INTO sample_hashes(sample_id,field_name,hash_hex) VALUES(1,'a','x')
                   ON CONFLICT(sample_id,field_name) DO UPDATE SET hash_hex=EXCLUDED.hash_hex""",
            )
        await database.execute("UPDATE sample_hashes SET hash_hex='unused' WHERE FALSE")
        await database.execute('DELETE FROM sample_hashes WHERE FALSE')
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 0
        await database.execute("UPDATE sample_hashes SET hash_hex='changed' WHERE sample_id=1 AND field_name='a'")
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 1

    registry_database(scenario)
