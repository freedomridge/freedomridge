"""Concept recording and scoring regressions."""

import asyncio

import asyncpg
import pytest

from backend import concept_jobs, job_queue
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_concept_jobs import assert_count, begin, finish, publish
from backend.tests.test_concept_maintenance import concept, sample, scores
from backend.tests.test_concept_review import bulk_samples
from backend.tests.test_job_runner import make_worker

registry_database = registry_helpers.registry_database
pytestmark = registry_helpers.pytestmark


def test_incremental_unit_keeps_a_lower_block_delivered_after_measurement(registry_database, monkeypatch):
    async def scenario(database):
        cid = await concept(database)
        await database.execute(
            """INSERT INTO samples(id,dataset_id,filepath,media_type)
               VALUES(2100,1,'low','image'),(102500,1,'high','image');
               INSERT INTO embeddings(dataset_id,sample_id,field_name,vector)
               SELECT dataset_id,id,'clip','[1,0]' FROM samples""",
        )
        await finish(database, cid)
        await database.execute("UPDATE embeddings SET vector='[-1,0]' WHERE sample_id=102500")
        async with database.pool.acquire() as writer:
            transaction = writer.transaction()
            await transaction.start()
            committed = False
            original = asyncpg.Connection.fetchval

            async def commit_after_delivery(connection, query, *args, **kwargs):
                nonlocal committed
                result = await original(connection, query, *args, **kwargs)
                if not committed and query.startswith('SELECT dispatch_concept_score_work($1,$2,$3)'):
                    await transaction.commit()
                    committed = True
                return result

            try:
                await writer.execute("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=2100")
                writer_txid = await writer.fetchval('SELECT txid_current()')
                with monkeypatch.context() as patch:
                    patch.setattr(asyncpg.Connection, 'fetchval', commit_after_delivery)
                    job = await concept_jobs.request_scoring(database.pool, cid)
            finally:
                if not committed:
                    await transaction.rollback()
        assert await database.fetchval(
            'SELECT txid_visible_in_snapshot($1::bigint,$2::text::txid_snapshot)',
            writer_txid,
            job['barrier']['snapshot'],
        )
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 1
        claim = await job_queue.claim_job(database.pool, 'review', kinds=[concept_jobs.KIND])
        step = await concept_jobs.handler(database.pool)(claim)
        assert step.checkpoint['after_id'] == 103423
        await concept_jobs.dispatch_changes(database.pool)
        await publish(database, claim, step)
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending WHERE block_id=2') == 1
        await finish(database, cid, job)
        later = await concept_jobs.schedule_concepts(database.pool)
        assert len(later) == 1
        await finish(database, cid, later[0])
        assert await scores(database, cid) == {2100: 0.5, 102500: 0.0}

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['upsert', 'delete'])
def test_empty_embedding_transition_never_locks_the_pending_table(registry_database, operation):
    async def scenario(database):
        sid = await sample(database, 'existing')
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('LOCK TABLE concept_embedding_pending IN ACCESS EXCLUSIVE MODE')
            if operation == 'upsert':
                query = """INSERT INTO embeddings(dataset_id,sample_id,field_name,vector)
                           VALUES((SELECT dataset_id FROM samples WHERE id = $1),$1,'clip','[1,0]')
                           ON CONFLICT(dataset_id, sample_id,field_name) DO UPDATE SET vector=EXCLUDED.vector"""
            else:
                query = 'DELETE FROM embeddings WHERE sample_id=$1 AND FALSE'
            await asyncio.wait_for(database.execute(query, sid), 1)

    registry_database(scenario)


def test_batch_recovery_requires_successes_before_retrying_a_larger_size(registry_database, monkeypatch):
    async def scenario(database):
        cid = await concept(database)
        await bulk_samples(database, 8000)
        await concept_jobs.request_scoring(database.pool, cid)
        original = concept_jobs._measure
        units: list[list[int]] = []
        timed_out = []
        attempts = []

        async def initially_cold(pool, job, size):
            attempts.append(size)
            if len(units) < 5 and size > 256 and not job['checkpoint'].get('count_phase'):
                timed_out.append(size)
                raise asyncpg.QueryCanceledError('Read exceeded the database budget')
            return await original(pool, job, size)

        monkeypatch.setattr(concept_jobs, '_measure', initially_cold)
        for attempt in range(100):
            claim = await job_queue.claim_job(database.pool, 'review', kinds=[concept_jobs.KIND])
            attempts.clear()
            step = await concept_jobs.handler(database.pool)(claim)
            units.append(list(attempts))
            await publish(database, claim, step)
            if step.finished:
                break
        else:
            pytest.fail('Initial build did not finish')
        assert len(timed_out) <= 3, units
        assert [1024] in units[5:], units
        await assert_count(database, cid, 8000)

    registry_database(scenario)


def test_final_count_exhausts_its_retry_budget_and_releases_the_lane(registry_database, monkeypatch):
    async def scenario(database):
        cid = await concept(database)
        await sample(database, 'first')
        claim, measured = await begin(database, cid)
        await publish(database, claim, measured)
        claim = await job_queue.claim_job(database.pool, 'review', kinds=[concept_jobs.KIND])
        assert claim is not None
        phase = await concept_jobs.handler(database.pool)(claim)
        await publish(database, claim, phase)
        assert phase.checkpoint['count_phase']
        worker = await make_worker(
            database,
            {concept_jobs.KIND: concept_jobs.handler(database.pool)},
            policies={concept_jobs.KIND: concept_jobs.POLICY},
        )
        stats = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1, lane='maintenance')
        original = asyncpg.Connection.fetchval
        attempts = []

        async def count_timeout(connection, query, *args, **kwargs):
            if query == 'SELECT count(*) FROM embedding_concept_scores WHERE concept_id=$1':
                attempts.append(await original(connection, 'SHOW statement_timeout'))
                raise asyncpg.QueryCanceledError('Count exceeded the database budget')
            return await original(connection, query, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(asyncpg.Connection, 'fetchval', count_timeout)
            for attempt in range(concept_jobs.POLICY.max_failures):
                await database.execute("UPDATE jobs SET available_at=now() WHERE id=$1", claim['id'])
                await worker.run_once()
        failed = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', claim['id'])
        assert failed['state'] == 'failed', dict(failed)
        assert attempts == ['45s'] * concept_jobs.POLICY.max_failures
        assert failed['checkpoint'] == phase.checkpoint
        assert failed['error']['message'] == 'Concept final count exceeded its database budget'
        stats_claim = await job_queue.claim_job(database.pool, 'review', kinds=['statistics'])
        assert stats_claim is not None and stats_claim['id'] == stats['id']
        await job_queue.complete_job(database.pool, stats_claim['id'], stats_claim['lease_token'])
        await database.execute("UPDATE jobs SET finished_at=now()-interval '6 minutes' WHERE id=$1", claim['id'])
        resumed = await concept_jobs.schedule_concepts(database.pool)
        assert len(resumed) == 1 and resumed[0]['checkpoint'] == phase.checkpoint
        await finish(database, cid, resumed[0])
        await assert_count(database, cid, 1)

    registry_database(scenario)


def test_invalid_concept_receives_no_more_work_until_its_definition_changes(registry_database):
    async def scenario(database):
        invalid = await concept(database, weight='[1,0,0]')
        await sample(database, 'first')
        await concept_jobs.request_scoring(database.pool, invalid)
        worker = await make_worker(database, {concept_jobs.KIND: concept_jobs.handler(database.pool)})
        await worker.run_once()
        assert await database.fetchval("SELECT count(*) FROM jobs WHERE state='failed'") == 1
        pending = await database.fetchval('SELECT count(*) FROM concept_score_pending WHERE concept_id=$1', invalid)
        healthy = await concept(database, name='healthy')
        for index in range(10):
            await sample(database, f'new-{index}')
            await concept_jobs.dispatch_changes(database.pool)
            assert (
                await database.fetchval('SELECT count(*) FROM concept_score_pending WHERE concept_id=$1', invalid)
                == pending
            )
        await finish(database, healthy)
        await assert_count(database, healthy, 11)
        assert await concept_jobs.schedule_concepts(database.pool) == []
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 0
        await database.execute("UPDATE embedding_concepts SET weight='[1,0]' WHERE id=$1", invalid)
        await sample(database, 'after-definition-change')
        await concept_jobs.dispatch_changes(database.pool)
        assert (
            await database.fetchval('SELECT count(*) FROM concept_score_pending WHERE concept_id=$1', invalid) > pending
        )
        await finish(database, invalid)
        await assert_count(database, invalid, 12)

    registry_database(scenario)
