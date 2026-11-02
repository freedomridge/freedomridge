"""Exact collapse invariants, committed-block delivery and fenced bootstrap recovery."""

import json
import random
from datetime import timedelta

import asyncpg
import pytest

from backend import access, duplicate_jobs, job_queue, job_service
from backend.routers import hashes
from backend.routers import jobs as jobs_router
from backend.tests import test_field_registry_postgres as helpers
from backend.tests.test_concept_jobs import publish

registry_database = helpers.registry_database
pytestmark = helpers.pytestmark
CONFIGS = ('a', 'b', '_all', '_any')


async def sample(database, sid, a='x', b='y', dataset_id=1):
    await database.execute(
        "INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES($1,$2,$3,'image')",
        sid,
        dataset_id,
        str(sid),
    )
    for field, value in (('a', a), ('b', b)):
        if value is not None:
            await database.execute(
                'INSERT INTO sample_hashes(sample_id,field_name,hash_hex) VALUES($1,$2,$3)',
                sid,
                field,
                value,
            )


async def hidden(database, config, dataset_id=1):
    return {
        r['sample_id']
        for r in await database.fetch(
            'SELECT sample_id FROM sample_dup_secondary WHERE dataset_id=$1 AND hash_config=$2',
            dataset_id,
            config,
        )
    }


async def oracle(database, config, dataset_id=1):
    async with database.pool.acquire() as connection:
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.fetchval('SELECT refresh_dup_secondary($1,$2)', dataset_id, config)
            return {
                r['sample_id']
                for r in await connection.fetch(
                    'SELECT sample_id FROM sample_dup_secondary WHERE dataset_id=$1 AND hash_config=$2',
                    dataset_id,
                    config,
                )
            }
        finally:
            await transaction.rollback()


async def finish(database, job):
    for _ in range(500):
        current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id'])
        if current['state'] == 'succeeded':
            return dict(current)
        assert current['state'] not in ('failed', 'cancelled'), dict(current)
        claim = await job_queue.claim_job(database.pool, 'duplicates-test', kinds=[duplicate_jobs.KIND])
        assert claim is not None
        step = await duplicate_jobs.handler(database.pool)(claim)
        await publish(database, claim, step)
    pytest.fail('Duplicate job did not finish its finite boundary')


async def bootstrap(database, configs=CONFIGS, dataset_id=1):
    for config in configs:
        await finish(database, await duplicate_jobs.request_refresh(database.pool, dataset_id, config))


async def drain(database, dataset_id=1):
    return await finish(database, await duplicate_jobs.request_refresh(database.pool, dataset_id))


async def assert_oracle(database, configs=CONFIGS, dataset_id=1):
    for config in configs:
        assert await hidden(database, config, dataset_id) == await oracle(database, config, dataset_id), config


def test_randomized_incremental_matches_unchanged_rebuild(registry_database):
    async def scenario(database):
        rng = random.Random(57054)
        await bootstrap(database)
        live = set()
        for _ in range(60):
            async with database.pool.acquire() as connection, connection.transaction():
                for _ in range(6):
                    sid = rng.randrange(1, 80)
                    if sid not in live:
                        await connection.execute(
                            "INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES($1,1,$2,'image')",
                            sid,
                            str(sid),
                        )
                        live.add(sid)
                    operation = rng.randrange(4)
                    if operation == 0:
                        await connection.execute('DELETE FROM samples WHERE id=$1', sid)
                        live.remove(sid)
                    elif operation == 1:
                        await connection.execute(
                            'DELETE FROM sample_hashes WHERE sample_id=$1 AND field_name=$2',
                            sid,
                            rng.choice(['a', 'b']),
                        )
                    else:
                        await connection.execute(
                            """INSERT INTO sample_hashes(sample_id,field_name,hash_hex) VALUES($1,$2,$3)
                               ON CONFLICT(sample_id,field_name) DO UPDATE SET hash_hex=EXCLUDED.hash_hex""",
                            sid,
                            rng.choice(['a', 'b']),
                            str(rng.randrange(8)),
                        )
            await drain(database)
            await assert_oracle(database)
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 0

    registry_database(scenario)


@pytest.mark.parametrize('mutation', ['delete', 'change', 'remove', 'late', 'chain', 'all'])
def test_representative_promotions_and_any_union(registry_database, mutation):
    async def scenario(database):
        await sample(database, 1, None if mutation == 'late' else 'x', None if mutation == 'late' else 'u')
        await sample(database, 2, 'x', 'v' if mutation == 'chain' else 'u')
        await sample(database, 3, 'y' if mutation == 'chain' else 'x', 'v' if mutation == 'chain' else 'u')
        await bootstrap(database)
        if mutation in ('delete', 'chain'):
            await database.execute('DELETE FROM samples WHERE id=1')
        elif mutation == 'change':
            await database.execute("UPDATE sample_hashes SET hash_hex='z' WHERE sample_id=1")
        elif mutation == 'remove':
            await database.execute('DELETE FROM sample_hashes WHERE sample_id=1')
        elif mutation == 'late':
            await database.execute(
                "INSERT INTO sample_hashes(sample_id,field_name,hash_hex) VALUES(1,'a','x'),(1,'b','u')",
            )
        else:
            await database.execute('DELETE FROM samples')
        await drain(database)
        await assert_oracle(database)
        if mutation == 'chain':
            assert await hidden(database, '_any') == {3}
        if mutation == 'late':
            assert await hidden(database, '_all') == {2, 3}

    registry_database(scenario)


def test_statement_triggers_coalesce_updates_and_cascade_deletes(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO samples(id,dataset_id,filepath,media_type)
               SELECT n,1,n::text,'image' FROM generate_series(1,2500) n""",
        )
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO sample_hashes(sample_id,field_name,hash_hex) SELECT id,'a','x' FROM samples",
            )
            await connection.execute("UPDATE sample_hashes SET hash_hex='y'")
            assert await connection.fetchval('SELECT count(*) FROM duplicate_pending') == 3
        await database.execute('DELETE FROM duplicate_pending')
        await database.execute('UPDATE sample_hashes SET hash_hex=hash_hex')
        await database.execute("UPDATE samples SET metadata='{\"test\":1}'")
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 0
        await database.execute('DELETE FROM samples')
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 3
        assert await duplicate_jobs.schedule_duplicates(database.pool) == []
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 0

    registry_database(scenario)


def test_late_and_out_of_order_commits_in_one_block(registry_database):
    async def scenario(database):
        await sample(database, 1)
        await sample(database, 2)
        await sample(database, 3)
        await bootstrap(database)
        await drain(database)
        async with database.pool.acquire() as slow, database.pool.acquire() as fast:
            tx = slow.transaction()
            await tx.start()
            try:
                await slow.execute("UPDATE sample_hashes SET hash_hex='late' WHERE sample_id=1")
                old_xid = await slow.fetchval('SELECT txid_current()')
                async with fast.transaction():
                    await fast.execute("UPDATE sample_hashes SET hash_hex='early' WHERE sample_id=2")
                    new_xid = await fast.fetchval('SELECT txid_current()')
                assert old_xid < new_xid
                job = await duplicate_jobs.request_refresh(database.pool, 1)
                await tx.commit()
            except BaseException:
                if slow.is_in_transaction():
                    await tx.rollback()
                raise
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 2
        await finish(database, job)
        assert await database.fetchval('SELECT transaction_id FROM duplicate_pending') == old_xid
        await drain(database)
        await assert_oracle(database)
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 0

    registry_database(scenario)


def test_late_lower_id_insert_is_not_skipped(registry_database):
    async def scenario(database):
        await sample(database, 20)
        await bootstrap(database)
        await drain(database)
        async with database.pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            await connection.execute(
                "INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES(1,1,'late','image')",
            )
            await connection.execute("INSERT INTO sample_hashes(sample_id,field_name,hash_hex) VALUES(1,'a','x')")
            job = await duplicate_jobs.request_refresh(database.pool, 1)
            await transaction.commit()
        await finish(database, job)
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 1
        await drain(database)
        assert await hidden(database, 'a') == {20}
        await assert_oracle(database)

    registry_database(scenario)


def test_writer_snapshot_before_config_registration_is_still_recorded(registry_database):
    async def scenario(database):
        await sample(database, 20)
        async with database.pool.acquire() as writer, writer.transaction(isolation='repeatable_read'):
            await writer.fetchval('SELECT count(*) FROM duplicate_configs')
            await bootstrap(database, ('a',))
            await writer.execute(
                "INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES(1,1,'late-registration','image')",
            )
            await writer.execute("INSERT INTO sample_hashes(sample_id,field_name,hash_hex) VALUES(1,'a','x')")
        await drain(database)
        await assert_oracle(database, ('a',))
        assert await hidden(database, 'a') == {20}

    registry_database(scenario)


def test_measurement_timeout_subdivides_the_unit(registry_database, monkeypatch):
    import asyncpg

    async def scenario(database):
        await sample(database, 1)
        original = duplicate_jobs._measure
        sizes = []

        async def limited(connection, job, size):
            sizes.append(size)
            if size > 2:
                raise asyncpg.QueryCanceledError('Oversized unit')
            return await original(connection, job, size)

        monkeypatch.setattr(duplicate_jobs, '_measure', limited)
        await bootstrap(database, ('a',))
        assert sizes[0] == duplicate_jobs.BATCH_SIZE
        assert sizes[-1] == 2
        assert await database.fetchval('SELECT initialized FROM duplicate_configs')

    registry_database(scenario)


def test_refresh_answering_202_keeps_the_job_and_exposes_its_id(registry_database, monkeypatch):
    from backend import job_wait

    async def scenario(database):
        await sample(database, 1)
        monkeypatch.setattr(hashes, 'db', database)
        monkeypatch.setattr(job_wait, 'ANSWER_SECONDS', 0.2)
        response = await hashes.refresh_duplicates(1, 'a', user={'id': 1})
        job = await database.fetchrow('SELECT id,state FROM jobs')
        assert response.status_code == 202
        assert json.loads(response.body) == {'job_id': str(job['id']), 'state': 'queued'}
        assert job['state'] == 'queued'

    registry_database(scenario)


def test_commit_after_measurement_survives_acknowledgement(registry_database):
    async def scenario(database):
        await sample(database, 1)
        await sample(database, 2)
        await bootstrap(database, ('a',))
        await drain(database)
        await database.execute("UPDATE sample_hashes SET hash_hex='changed' WHERE sample_id=1 AND field_name='a'")
        job = await duplicate_jobs.request_refresh(database.pool, 1)
        claim = await job_queue.claim_job(database.pool, 'measured', kinds=[duplicate_jobs.KIND])
        step = await duplicate_jobs.handler(database.pool)(claim)
        await database.execute("UPDATE sample_hashes SET hash_hex='x' WHERE sample_id=1 AND field_name='a'")
        await publish(database, claim, step)
        await finish(database, job)
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 1
        assert await hidden(database, 'a') != await oracle(database, 'a')
        await drain(database)
        await assert_oracle(database, ('a',))

    registry_database(scenario)


def test_noop_replay_writes_no_memberships_or_hidden_rows(registry_database):
    async def scenario(database):
        await sample(database, 1)
        await sample(database, 2)
        await bootstrap(database)
        await drain(database)
        before = await database.fetch(
            'SELECT config,sample_id,key,ctid::text,xmin::text FROM duplicate_members ORDER BY 1,2,3',
        )
        hidden_before = await database.fetch(
            'SELECT hash_config,sample_id,ctid::text,xmin::text FROM sample_dup_secondary ORDER BY 1,2',
        )
        await database.execute('INSERT INTO duplicate_pending(dataset_id,block_id) VALUES(1,0)')
        await drain(database)
        assert (
            await database.fetch(
                'SELECT config,sample_id,key,ctid::text,xmin::text FROM duplicate_members ORDER BY 1,2,3',
            )
            == before
        )
        assert (
            await database.fetch(
                'SELECT hash_config,sample_id,ctid::text,xmin::text FROM sample_dup_secondary ORDER BY 1,2',
            )
            == hidden_before
        )

    registry_database(scenario)


def test_any_reconciliation_pages_distinct_samples_and_writes_only_differences(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(duplicate_jobs, 'BATCH_SIZE', 2)
        for sid in range(1, 7):
            await sample(database, sid)
        await database.execute(
            "INSERT INTO sample_dup_secondary VALUES(1,'_any',1),(1,'_any',3),(1,'_any',5)",
        )
        before = await database.fetch(
            "SELECT sample_id,ctid::text FROM sample_dup_secondary WHERE sample_id IN (3,5) ORDER BY sample_id",
        )
        await bootstrap(database, ('_any',))
        await assert_oracle(database, ('_any',))
        assert (
            await database.fetch(
                "SELECT sample_id,ctid::text FROM sample_dup_secondary WHERE sample_id IN (3,5) ORDER BY sample_id",
            )
            == before
        )

    registry_database(scenario)


def test_any_deleted_representative_cannot_fall_between_page_sources(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(duplicate_jobs, 'BATCH_SIZE', 2)
        await sample(database, 1, 'unique', 'unique')
        for sid in (2, 3, 4):
            await sample(database, sid)
        await bootstrap(database, ('_any',))
        await database.execute('DELETE FROM samples WHERE id=2')
        await drain(database)
        await assert_oracle(database, ('_any',))
        assert await hidden(database, '_any') == {4}
        assert await database.fetchval('SELECT count(*) FROM duplicate_members WHERE sample_id=2') == 0

    registry_database(scenario)


@pytest.mark.parametrize('interruption', ['cancel', 'cancel_pending', 'lease'])
def test_bootstrap_resume_preserves_hidden_rows_until_reconciliation(registry_database, monkeypatch, interruption):
    async def scenario(database):
        monkeypatch.setattr(duplicate_jobs, 'BATCH_SIZE', 2)
        for sid in range(1, 7):
            await sample(database, sid)
        await database.execute("SELECT refresh_dup_secondary(1,'a')")
        before = await database.fetch(
            "SELECT sample_id,ctid::text FROM sample_dup_secondary WHERE hash_config='a' ORDER BY sample_id",
        )
        job = await duplicate_jobs.request_refresh(database.pool, 1, 'a')
        claim = await job_queue.claim_job(database.pool, 'interrupted', kinds=[duplicate_jobs.KIND])
        await publish(database, claim, await duplicate_jobs.handler(database.pool)(claim))
        claim = await job_queue.claim_job(database.pool, 'interrupted', kinds=[duplicate_jobs.KIND])
        step = await duplicate_jobs.handler(database.pool)(claim)
        if interruption in ('cancel', 'cancel_pending'):
            await job_queue.cancel_job(database.pool, job['id'])
            if interruption == 'cancel_pending':
                resumed = await duplicate_jobs.request_refresh(database.pool, 1, 'a')
            cancelled = await publish(database, claim, step)
            assert cancelled['state'] == 'cancelled'
            if interruption == 'cancel':
                resumed = await duplicate_jobs.request_refresh(database.pool, 1, 'a')
            assert resumed['checkpoint']['after'] == 2
        else:
            await database.execute("UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE id=$1", job['id'])
            with pytest.raises(job_queue.LeaseLost):
                await publish(database, claim, step)
            await job_queue.claim_job(database.pool, 'lease-reaper', kinds=['unused'])
            await database.execute("UPDATE jobs SET available_at=now() WHERE id=$1", job['id'])
            resumed = job
        assert (
            await database.fetch(
                "SELECT sample_id,ctid::text FROM sample_dup_secondary WHERE hash_config='a' ORDER BY sample_id",
            )
            == before
        )
        await finish(database, resumed)
        assert (
            await database.fetch(
                "SELECT sample_id,ctid::text FROM sample_dup_secondary WHERE hash_config='a' ORDER BY sample_id",
            )
            == before
        )
        await assert_oracle(database, ('a',))

    registry_database(scenario)


def test_sample_move_repairs_both_datasets(registry_database):
    async def scenario(database):
        await sample(database, 1)
        await sample(database, 2)
        await sample(database, 3, dataset_id=2)
        await bootstrap(database)
        await bootstrap(database, dataset_id=2)
        await database.execute('UPDATE samples SET dataset_id=2 WHERE id=1')
        await drain(database)
        await drain(database, 2)
        await assert_oracle(database)
        await assert_oracle(database, dataset_id=2)

    registry_database(scenario)


def test_queued_incremental_job_yields_to_cancelled_bootstrap_recovery(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(duplicate_jobs, 'BATCH_SIZE', 2)
        for sid in range(1, 7):
            await sample(database, sid)
        await bootstrap(database, ('a',))
        await database.execute("UPDATE sample_hashes SET hash_hex='changed' WHERE sample_id=1")
        rebuilding = await duplicate_jobs.request_refresh(database.pool, 1, 'a')
        claim = await job_queue.claim_job(database.pool, 'rebuild', kinds=[duplicate_jobs.KIND])
        await publish(database, claim, await duplicate_jobs.handler(database.pool)(claim))
        routine = await duplicate_jobs.request_refresh(database.pool, 1)
        await job_queue.cancel_job(database.pool, rebuilding['id'])
        await job_queue.claim_job(database.pool, 'cancel-finalizer', kinds=['unused'])
        result = await finish(database, routine)
        assert result['result']['bootstrap_pending']
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') > 0
        jobs = await duplicate_jobs.schedule_duplicates(database.pool)
        assert len(jobs) == 1
        assert jobs[0]['checkpoint']['after'] == 2
        await finish(database, jobs[0])
        await drain(database)
        await assert_oracle(database, ('a',))

    registry_database(scenario)


def test_truncate_requests_bootstrap_without_changing_collapse_table(registry_database):
    async def scenario(database):
        await sample(database, 1)
        await sample(database, 2)
        await bootstrap(database, ('a',))
        await database.execute('TRUNCATE sample_hashes')
        assert await hidden(database, 'a') == {2}
        for _ in range(5):
            jobs = await duplicate_jobs.schedule_duplicates(database.pool)
            if not jobs:
                break
            for job in jobs:
                await finish(database, job)
        assert await hidden(database, 'a') == set()
        assert await database.fetchval('SELECT count(*) FROM duplicate_members') == 0
        assert await database.fetchval('SELECT count(*) FROM duplicate_pending') == 0

    registry_database(scenario)


def test_history_filter_and_retention_share_existing_switch(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs_router, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await sample(database, 1)
        await bootstrap(database, ('a',))
        routine = await drain(database)
        user = {'id': 1, 'role': 'admin'}
        page = await jobs_router.list_dataset_jobs(1, limit=50, cursor=None, user=user)
        assert len(page.items) == 1
        assert page.items[0].id != routine['id']
        page = await jobs_router.list_dataset_jobs(1, limit=50, cursor=None, user=user, include_system_statistics=True)
        assert len(page.items) == 2
        cleanup = {'created_at': await database.fetchval('SELECT now()') + timedelta(days=2), 'checkpoint': {}}
        step = await job_service._cleanup_statistics_history(database.pool, cleanup)
        async with database.pool.acquire() as connection, connection.transaction():
            await step.publish(connection)
        assert await database.fetchval('SELECT count(*) FROM jobs') == 1
        assert await database.fetchval("SELECT barrier->>'full' FROM jobs") == 'true'

    registry_database(scenario)


def test_endpoint_registers_runner_request_and_preserves_response(registry_database, monkeypatch):
    from backend.tests import job_helpers

    async def scenario(database):
        await sample(database, 1)
        monkeypatch.setattr(hashes, 'db', database)
        async with job_helpers.runner(database, kinds=[duplicate_jobs.KIND]):
            result = await hashes.refresh_duplicates(1, 'a', user={'id': 1, 'role': 'admin'})
        assert result == {'config': 'a', 'hidden': 0}
        assert await database.fetchval('SELECT config FROM duplicate_configs') == 'a'
        assert await database.fetchval('SELECT created_by FROM jobs') == 1
        assert await database.fetchval("SELECT barrier->>'full' FROM jobs") == 'true'
        assert await database.fetchval('SELECT state FROM jobs') == 'succeeded'

    registry_database(scenario)


def test_backfill_cli_submits_full_jobs_without_building_sets(registry_database, monkeypatch):
    from contextlib import asynccontextmanager

    from scripts import backfill_dup_secondary

    async def scenario(database):
        await sample(database, 1)
        await sample(database, 2)

        @asynccontextmanager
        async def pool(*args, **kwargs):
            yield database.pool

        monkeypatch.setattr(asyncpg, 'create_pool', pool)
        await backfill_dup_secondary.main()
        assert await database.fetchval('SELECT count(*) FROM duplicate_configs') == 4
        assert await database.fetchval("SELECT count(*) FROM jobs WHERE barrier @> '{\"full\":true}'") == 4
        assert await database.fetchval('SELECT count(*) FROM duplicate_members') == 0
        assert await database.fetchval('SELECT count(*) FROM sample_dup_secondary') == 0

    registry_database(scenario)


def test_oversized_all_key_skips_only_that_sample(registry_database):
    async def scenario(database):
        await sample(database, 1, 'a' * 1000, 'b' * 1000)
        await sample(database, 2)
        await sample(database, 3)
        await bootstrap(database, ('_all',))
        assert await database.fetchval('SELECT count(*) FROM duplicate_members WHERE sample_id=1') == 0
        assert await hidden(database, '_all') == {3}

    registry_database(scenario)
