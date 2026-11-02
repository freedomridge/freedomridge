"""Regression contracts for incremental concept upkeep."""

import asyncio
import json
import time
from datetime import timedelta

import asyncpg
import pytest

from backend import access, concept_jobs, job_queue, job_service
from backend.routers import jobs as jobs_router
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_concept_jobs import assert_count, begin, finish, publish
from backend.tests.test_concept_maintenance import concept, sample, score, scores
from backend.tests.test_job_runner import make_worker

registry_database = registry_helpers.registry_database
pytestmark = registry_helpers.pytestmark


async def bulk_samples(database, count):
    await database.execute(
        """INSERT INTO samples(dataset_id,filepath,media_type)
           SELECT 1,number::text,'image' FROM generate_series(1,$1::int) number""",
        count,
    )
    await database.execute(
        "INSERT INTO embeddings(sample_id,field_name,vector) SELECT id,'clip','[1,0]' FROM samples",
    )


async def write_layout(database, sample_id=None):
    """Write map coordinates where this schema keeps them: embeddings.x/y, or embedding_coordinates."""
    if await database.fetchval("SELECT to_regclass('embedding_coordinates') IS NOT NULL"):
        await database.execute(
            'INSERT INTO embedding_coordinates(sample_id,field_name,x,y) '
            'SELECT sample_id,field_name,1,2 FROM embeddings WHERE $1::int IS NULL OR sample_id=$1 '
            'ON CONFLICT (sample_id,field_name) DO UPDATE SET x=EXCLUDED.x,y=EXCLUDED.y',
            sample_id,
        )
    else:
        await database.execute('UPDATE embeddings SET x=1,y=2 WHERE $1::int IS NULL OR sample_id=$1', sample_id)


def test_statement_recording_coalesces_blocks_and_never_touches_layout(registry_database):
    async def scenario(database):
        await bulk_samples(database, 2500)
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 3
        await database.execute('DELETE FROM concept_embedding_pending')
        await write_layout(database)
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 0
        async with database.pool.acquire() as writer, writer.transaction():
            await writer.execute("UPDATE embeddings SET vector='[0,1]'")
            await writer.execute("UPDATE embeddings SET vector='[-1,0]'")
            assert await writer.fetchval('SELECT count(*) FROM concept_embedding_pending') == 3
        await database.execute('DELETE FROM concept_embedding_pending')
        await database.execute('DELETE FROM embeddings')
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 3

    registry_database(scenario)


def test_manual_concept_has_no_backlog_and_reenabling_rebuilds(registry_database):
    async def scenario(database):
        cid = await concept(database)
        sid = await sample(database, 'first')
        await finish(database, cid)
        await database.execute('UPDATE embedding_concepts SET auto_rescore=FALSE WHERE id=$1', cid)
        for value in ('[0,1]', '[-1,0]'):
            await database.execute('UPDATE embeddings SET vector=$1::text::vector WHERE sample_id=$2', value, sid)
            await concept_jobs.dispatch_changes(database.pool)
            assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 0
        await database.execute('UPDATE embedding_concepts SET auto_rescore=TRUE WHERE id=$1', cid)
        job = await concept_jobs.request_scoring(database.pool, cid)
        assert job['barrier']['bootstrap']
        await finish(database, cid, job)
        assert (await scores(database, cid))[sid] == 0
        assert await concept_jobs.schedule_concepts(database.pool) == []

    registry_database(scenario)


def test_score_new_manual_concept_only_writes_and_reads_new_inputs(registry_database):
    async def scenario(database):
        cid = await concept(database)
        await database.execute('UPDATE embedding_concepts SET auto_rescore=FALSE WHERE id=$1', cid)
        await bulk_samples(database, 10000)
        await database.execute("ANALYZE samples; ANALYZE embeddings")
        await score(database, cid, full=True)
        await score(database, cid)
        await database.execute('UPDATE embedding_concepts SET score_initialized=FALSE WHERE id=$1', cid)
        before = await database.fetchval('SELECT ctid::text FROM embedding_concept_scores WHERE sample_id=1')
        await sample(database, 'new')
        plan = await database.fetchval(
            'EXPLAIN(ANALYZE,BUFFERS,FORMAT JSON) SELECT score_concept($1,FALSE)',
            cid,
        )
        assert json.loads(plan)[0]['Plan']['Shared Hit Blocks'] < 500, plan
        assert await database.fetchval('SELECT ctid::text FROM embedding_concept_scores WHERE sample_id=1') == before
        await assert_count(database, cid, 10001)
        assert await score(database, cid) == 0

    registry_database(scenario)


def test_disabling_during_delivery_does_not_leave_a_permanent_backlog(registry_database):
    async def scenario(database):
        cid = await concept(database)
        await sample(database, 'pending')
        async with database.pool.acquire() as dispatcher, dispatcher.transaction():
            await dispatcher.execute('SELECT dispatch_concept_score_work(4096,1)')
            await asyncio.wait_for(
                database.execute('UPDATE embedding_concepts SET auto_rescore=FALSE WHERE id=$1', cid),
                1,
            )
        assert await concept_jobs.schedule_concepts(database.pool) == []
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 0

    registry_database(scenario)


@pytest.mark.parametrize('full', [False, True])
def test_manual_watermark_never_skips_an_arrival_during_scoring(registry_database, full):
    async def scenario(database):
        cid = await concept(database)
        await sample(database, 'existing')
        await score(database, cid, full=True)
        await database.execute('UPDATE embedding_concepts SET auto_rescore=FALSE WHERE id=$1', cid)
        await sample(database, 'new')
        await database.execute(
            """CREATE FUNCTION pause_score() RETURNS trigger AS $$
               BEGIN PERFORM pg_advisory_xact_lock(120056); RETURN NEW; END;
               $$ LANGUAGE plpgsql;
               CREATE TRIGGER pause_score BEFORE INSERT ON embedding_concept_scores
               FOR EACH ROW EXECUTE FUNCTION pause_score()""",
        )

        async def wait_for_score():
            while not await database.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND objid=120056 AND NOT granted)",
            ):
                await asyncio.sleep(0.01)

        task = None
        try:
            async with database.pool.acquire() as holder, holder.transaction():
                await holder.execute('SELECT pg_advisory_xact_lock(120056)')
                task = asyncio.create_task(score(database, cid, full=full))
                await asyncio.wait_for(wait_for_score(), 2)
                late = await sample(database, 'arrived-during-score')
        finally:
            if task is not None:
                await asyncio.wait_for(task, 2)
        assert late not in await scores(database, cid)
        assert await score(database, cid) == 1
        assert late in await scores(database, cid)
        await assert_count(database, cid, 3)

    registry_database(scenario)


def test_manual_auto_rescore_sees_repeated_edits_in_its_own_transaction(registry_database):
    async def scenario(database):
        cid = await concept(database)
        sid = await sample(database, 'existing')
        await score(database, cid, full=True)
        async with database.pool.acquire() as writer, writer.transaction():
            await writer.execute("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=$1", sid)
            await score(writer, cid)
            await writer.execute("UPDATE embeddings SET vector='[-1,0]' WHERE sample_id=$1", sid)
            await score(writer, cid)
            assert (await scores(writer, cid))[sid] == 0

    registry_database(scenario)


@pytest.mark.parametrize('full', [False, True])
def test_manual_rescores_do_not_deliver_or_lock_other_concepts(registry_database, full):
    async def scenario(database):
        first = await concept(database)
        second = await concept(database, name='second')
        await score(database, first, full=True)
        await score(database, second, full=True)
        await sample(database, 'pending')
        pending = await database.fetchval('SELECT count(*) FROM concept_embedding_pending')
        async with database.pool.acquire() as writer, writer.transaction():
            await score(writer, first, full=full)
            assert await writer.fetchval('SELECT count(*) FROM concept_embedding_pending') == pending
            assert await writer.fetchval('SELECT count(*) FROM concept_score_pending') == 0
            await asyncio.wait_for(score(database, second, full=full), 1)
        assert len(await scores(database, second)) == 1

    registry_database(scenario)


def test_sample_delete_never_takes_a_concept_fk_lock(registry_database):
    async def scenario(database):
        cid = await concept(database)
        sid = await sample(database, 'delete')
        await finish(database, cid)
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('SELECT id FROM embedding_concepts WHERE id=$1 FOR UPDATE', cid)
            await asyncio.wait_for(database.execute('DELETE FROM samples WHERE id=$1', sid), 1)
            await holder.execute('DELETE FROM embedding_concepts WHERE id=$1', cid)
        await concept_jobs.schedule_concepts(database.pool)
        assert await database.fetchval('SELECT count(*) FROM concept_score_count_deltas') == 0

    registry_database(scenario)


def test_delivery_and_other_rescore_survive_concurrent_concept_delete(registry_database):
    async def scenario(database):
        removed = await concept(database)
        kept = await concept(database, name='kept')
        await score(database, kept, full=True)
        await sample(database, 'pending')
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('DELETE FROM embedding_concepts WHERE id=$1', removed)
            await asyncio.wait_for(score(database, kept), 1)
            await asyncio.wait_for(concept_jobs.dispatch_changes(database.pool), 1)
        await concept_jobs.schedule_concepts(database.pool)
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending WHERE concept_id=$1', removed) == 0
        assert len(await scores(database, kept)) == 1

    registry_database(scenario)


def test_initial_build_preserves_unchanged_tuple_versions(registry_database):
    async def scenario(database):
        cid = await concept(database)
        await bulk_samples(database, 1200)
        await score(database, cid, full=True)
        before = await database.fetch(
            'SELECT sample_id,ctid::text,xmin::text FROM embedding_concept_scores ORDER BY sample_id',
        )
        await database.execute('UPDATE embedding_concepts SET score_initialized=FALSE WHERE id=$1', cid)
        await finish(database, cid)
        assert (
            await database.fetch(
                'SELECT sample_id,ctid::text,xmin::text FROM embedding_concept_scores ORDER BY sample_id',
            )
            == before
        )

    registry_database(scenario)


@pytest.mark.parametrize('state', ['failed', 'cancelled'])
def test_initial_build_resumes_published_cursor_across_jobs(registry_database, monkeypatch, state):
    async def scenario(database):
        monkeypatch.setattr(concept_jobs, 'BATCH_SIZE', 1)
        cid = await concept(database)
        await bulk_samples(database, 3)
        claim, measured = await begin(database, cid)
        await publish(database, claim, measured)
        await database.execute("UPDATE jobs SET state=$2,finished_at=now() WHERE id=$1", claim['id'], state)
        resumed = await concept_jobs.request_scoring(database.pool, cid)
        assert resumed['checkpoint']['after_id'] == 1
        assert resumed['barrier'] == claim['barrier']
        result = await finish(database, cid, resumed)
        assert result['checkpoint']['examined'] == 3
        await assert_count(database, cid, 3)

    registry_database(scenario)


def test_final_count_uses_its_own_budget_and_timeout_never_shrinks_batches(registry_database, monkeypatch):
    async def scenario(database):
        cid = await concept(database)
        await sample(database, 'first')
        claim, measured = await begin(database, cid)
        await publish(database, claim, measured)
        claim = await job_queue.claim_job(database.pool, 'test', kinds=[concept_jobs.KIND])
        step = await concept_jobs.handler(database.pool)(claim)
        assert not step.finished and step.checkpoint['count_phase']
        await publish(database, claim, step)
        claim = await job_queue.claim_job(database.pool, 'test', kinds=[concept_jobs.KIND])
        original = asyncpg.Connection.fetchval
        calls = []

        async def timed_count(connection, query, *args, **kwargs):
            if query == 'SELECT count(*) FROM embedding_concept_scores WHERE concept_id=$1':
                calls.append(await original(connection, 'SHOW statement_timeout'))
                raise asyncpg.QueryCanceledError('Database stall')
            return await original(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetchval', timed_count)
        with pytest.raises(job_queue.QueueBusy):
            await concept_jobs.handler(database.pool)(claim)
        assert calls == ['45s']

        async def slow_count(connection, query, *args, **kwargs):
            if query == 'SELECT count(*) FROM embedding_concept_scores WHERE concept_id=$1':
                await connection.execute('SELECT pg_sleep(6)')
            return await original(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetchval', slow_count)
        await publish(database, claim, await concept_jobs.handler(database.pool)(claim))
        monkeypatch.setattr(asyncpg.Connection, 'fetchval', original)
        await assert_count(database, cid, 1)

    registry_database(scenario)


def test_batch_stall_is_transient_and_success_restores_batch_size(registry_database, monkeypatch):
    async def scenario(database):
        cid = await concept(database)
        await sample(database, 'first')
        job = await concept_jobs.request_scoring(database.pool, cid)
        original = concept_jobs._measure
        sizes = []

        async def stall(pool, job, size):
            sizes.append(size)
            raise asyncpg.QueryCanceledError('Database stall')

        monkeypatch.setattr(concept_jobs, '_measure', stall)
        with pytest.raises(job_queue.QueueBusy):
            await concept_jobs.handler(database.pool)(job)
        assert sizes[-1] == 1
        monkeypatch.setattr(concept_jobs, '_measure', original)
        job['checkpoint']['batch_size'] = 16
        step = await concept_jobs.handler(database.pool)(job)
        assert step.checkpoint['batch_size'] == 16
        job['checkpoint']['batch_successes'] = 3
        step = await concept_jobs.handler(database.pool)(job)
        assert step.checkpoint['batch_size'] == 32

    registry_database(scenario)


def test_repeated_slow_reads_defer_before_the_unit_deadline(registry_database, monkeypatch):
    async def scenario(database):
        cid = await concept(database)
        job = await concept_jobs.request_scoring(database.pool, cid)
        started = time.monotonic()
        elapsed = 0
        sizes = []

        async def stall(pool, job, size):
            nonlocal elapsed
            sizes.append(size)
            elapsed += 6
            raise asyncpg.QueryCanceledError('Database stall')

        with monkeypatch.context() as patch:
            patch.setattr(concept_jobs, '_measure', stall)
            patch.setattr(time, 'monotonic', lambda: started + elapsed)
            with pytest.raises(job_queue.QueueBusy):
                await concept_jobs.handler(database.pool)(job)
        assert elapsed < concept_jobs.POLICY.max_unit_seconds
        assert sizes[-1] > 1

    registry_database(scenario)


def test_database_stalls_do_not_exhaust_the_jobs_failure_budget(registry_database, monkeypatch):
    async def scenario(database):
        cid = await concept(database)
        await sample(database, 'first')
        job = await concept_jobs.request_scoring(database.pool, cid)
        worker = await make_worker(
            database,
            {concept_jobs.KIND: concept_jobs.handler(database.pool)},
            policies={concept_jobs.KIND: concept_jobs.POLICY},
        )

        async def stall(pool, job, size):
            raise asyncpg.QueryCanceledError('Database stall')

        with monkeypatch.context() as patch:
            patch.setattr(concept_jobs, '_measure', stall)
            for attempt in range(concept_jobs.POLICY.max_failures + 1):
                await database.execute("UPDATE jobs SET available_at=now() WHERE id=$1", job['id'])
                await worker.run_once()
                row = await database.fetchrow('SELECT state,failures FROM jobs WHERE id=$1', job['id'])
                assert (row['state'], row['failures']) == ('queued', 0)
        await database.execute("UPDATE jobs SET available_at=now() WHERE id=$1", job['id'])
        await finish(database, cid, job)
        await assert_count(database, cid, 1)

    registry_database(scenario)


def test_upkeep_ignores_late_work_inside_the_existing_sample_extent(registry_database):
    async def scenario(database):
        cid = await concept(database)
        await bulk_samples(database, 2050)
        await finish(database, cid)
        await database.execute("UPDATE embeddings SET vector='[-1,0]' WHERE sample_id=1")
        job = await concept_jobs.request_scoring(database.pool, cid)
        await database.execute("UPDATE embeddings SET vector='[-1,0]' WHERE sample_id=2048")
        await concept_jobs.dispatch_changes(database.pool)
        await finish(database, cid, job)
        assert (await scores(database, cid))[1] == 0
        assert (await scores(database, cid))[2048] == 1
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending WHERE concept_id=$1', cid) == 1
        await finish(database, cid)
        assert (await scores(database, cid))[2048] == 0

    registry_database(scenario)


def test_invalid_definition_stays_failed_until_changed(registry_database):
    async def scenario(database):
        cid = await concept(database, weight='[1,0,0]')
        await sample(database, 'first')
        job = await concept_jobs.request_scoring(database.pool, cid)
        worker = await make_worker(database, {concept_jobs.KIND: concept_jobs.handler(database.pool)})
        await worker.run_once()
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'failed'
        await database.execute("UPDATE jobs SET finished_at=now()-interval '1 day'")
        assert await concept_jobs.schedule_concepts(database.pool) == []
        await database.execute("UPDATE embedding_concepts SET weight='[1,0]' WHERE id=$1", cid)
        jobs = await concept_jobs.schedule_concepts(database.pool)
        assert len(jobs) == 1
        await finish(database, cid, jobs[0])

    registry_database(scenario)


def test_delivery_prefers_older_transactions_over_lower_blocks(registry_database):
    async def scenario(database):
        await concept(database)
        await database.execute(
            "INSERT INTO concept_embedding_pending(dataset_id,field_name,block_id) VALUES(1,'clip',100)",
        )
        await database.execute(
            "INSERT INTO concept_embedding_pending(dataset_id,field_name,block_id) VALUES(1,'clip',1)",
        )
        await database.execute('SELECT dispatch_concept_score_work(1,1)')
        assert await database.fetchval('SELECT block_id FROM concept_score_pending') == 100

    registry_database(scenario)


def test_concept_history_shares_statistics_filter_and_retention(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs_router, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        cid = await concept(database)
        await sample(database, 'first')
        await finish(database, cid)
        user_job = await job_queue.enqueue_job(database.pool, 'user_export', dataset_id=1, created_by=1)
        user = {'id': 1, 'role': 'admin'}
        page = await jobs_router.list_dataset_jobs(1, limit=50, cursor=None, user=user)
        assert [item.id for item in page.items] == [user_job['id']]
        page = await jobs_router.list_dataset_jobs(1, limit=50, cursor=None, user=user, include_system_statistics=True)
        assert len(page.items) == 2
        cleanup = {'created_at': await database.fetchval('SELECT now()') + timedelta(days=2), 'checkpoint': {}}
        step = await job_service._cleanup_statistics_history(database.pool, cleanup)
        assert step.publish is not None
        async with database.pool.acquire() as connection, connection.transaction():
            await step.publish(connection)
        assert await database.fetchval("SELECT count(*) FROM jobs WHERE kind='concept_scoring'") == 0
        assert await database.fetchval('SELECT count(*) FROM jobs') == 1

    registry_database(scenario)
