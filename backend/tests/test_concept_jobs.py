"""Fenced maintenance, finite streaming boundaries and failure recovery for concepts."""

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import asyncpg
import pytest

from backend import concept_jobs, job_queue
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_concept_maintenance import concept, sample, score, scores
from backend.tests.test_job_runner import make_worker

registry_database = registry_helpers.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def finish(database, concept_id, job=None):
    job = job or await concept_jobs.request_scoring(database.pool, concept_id)
    assert job is not None
    worker = await make_worker(
        database,
        {concept_jobs.KIND: concept_jobs.handler(database.pool)},
        policies={concept_jobs.KIND: concept_jobs.POLICY},
    )
    for attempt in range(100):
        await asyncio.wait_for(worker.run_once(), 20)
        current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id'])
        if current['state'] in ('succeeded', 'failed', 'cancelled'):
            assert current['state'] == 'succeeded', dict(current)
            return dict(current)
        await asyncio.sleep(0.01)
    pytest.fail('Concept job did not finish its finite boundary')


async def begin(database, concept_id):
    job = await concept_jobs.request_scoring(database.pool, concept_id)
    assert job is not None
    claim = await job_queue.claim_job(database.pool, 'concept-test', kinds=[concept_jobs.KIND])
    assert claim is not None and claim['id'] == job['id']
    return claim, await concept_jobs.handler(database.pool)(claim)


async def publish(database, claim, step):
    return await job_queue.publish_step(
        database.pool,
        claim['id'],
        claim['lease_token'],
        checkpoint=step.checkpoint,
        done=step.done,
        total=step.total,
        message=step.message,
        result=step.result,
        finished=step.finished,
        release=not step.finished,
        publisher=step.publish,
    )


async def assert_count(database, concept_id, expected):
    assert (
        await database.fetchval('SELECT count(*) FROM embedding_concept_scores WHERE concept_id=$1', concept_id)
        == expected
    )
    assert await database.fetchval('SELECT scored_count FROM embedding_concepts WHERE id=$1', concept_id) == expected


def test_bootstrap_then_upkeep_only_measure_changed_samples(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        first = await sample(database, 'first')
        await sample(database, 'second', '[0,1]')
        initial = await finish(database, concept_id)
        assert initial['result']['examined'] == 2
        await assert_count(database, concept_id, 2)
        assert await concept_jobs.schedule_concepts(database.pool) == []
        original = await database.fetchval(
            'SELECT ctid::text FROM embedding_concept_scores WHERE concept_id=$1 AND sample_id=$2',
            concept_id,
            first,
        )
        third = await sample(database, 'third', '[-1,0]')
        jobs = await concept_jobs.schedule_concepts(database.pool)
        assert len(jobs) == 1
        updated = await finish(database, concept_id, jobs[0])
        assert updated['result']['examined'] == 3
        assert (await scores(database, concept_id))[third] == 0
        await assert_count(database, concept_id, 3)
        assert (
            await database.fetchval(
                'SELECT ctid::text FROM embedding_concept_scores WHERE concept_id=$1 AND sample_id=$2',
                concept_id,
                first,
            )
            == original
        )
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 0
        assert await database.fetchval('SELECT count(*) FROM concept_score_count_deltas') == 0

    registry_database(scenario)


def test_job_barrier_finishes_during_ingest_and_catches_late_lower_ids_next_time(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await finish(database, concept_id)
        async with database.pool.acquire() as writer, writer.transaction():
            lower = await sample(writer, 'late')
            higher = await sample(database, 'early')
            job = await concept_jobs.request_scoring(database.pool, concept_id)
            later = await sample(database, 'after-boundary')
            await concept_jobs.dispatch_changes(database.pool)
            await finish(database, concept_id, job)
            assert set(await scores(database, concept_id)) == {higher}
        await finish(database, concept_id)
        assert set(await scores(database, concept_id)) == {lower, higher, later}
        await assert_count(database, concept_id, 3)

    registry_database(scenario)


def test_vector_changed_after_measurement_remains_pending(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await finish(database, concept_id)
        sample_id = await sample(database, 'first')
        claim, measured = await begin(database, concept_id)
        await database.execute("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=$1", sample_id)
        await concept_jobs.dispatch_changes(database.pool)
        await publish(database, claim, measured)
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 1
        await finish(database, concept_id, claim)
        await finish(database, concept_id)
        assert (await scores(database, concept_id))[sample_id] == pytest.approx(0.5)
        await assert_count(database, concept_id, 1)

    registry_database(scenario)


@pytest.mark.parametrize('action', ['disable', 'change', 'delete'])
def test_definition_changes_cannot_publish_stale_results(registry_database, action):
    async def scenario(database):
        concept_id = await concept(database)
        await sample(database, 'first')
        claim, measured = await begin(database, concept_id)
        if action == 'disable':
            await database.execute('UPDATE embedding_concepts SET auto_rescore=FALSE WHERE id=$1', concept_id)
        elif action == 'change':
            await database.execute("UPDATE embedding_concepts SET weight='[0,1]' WHERE id=$1", concept_id)
        else:
            await database.execute('DELETE FROM embedding_concepts WHERE id=$1', concept_id)
        with pytest.raises(job_queue.QueueContended):
            await publish(database, claim, measured)
        assert await scores(database, concept_id) == {}
        terminal = await concept_jobs.handler(database.pool)(claim)
        assert terminal.finished
        await publish(database, claim, terminal)
        if action == 'change':
            await finish(database, concept_id)
            assert list((await scores(database, concept_id)).values()) == [0.5]
        else:
            assert await concept_jobs.schedule_concepts(database.pool) == []

    registry_database(scenario)


def test_sample_delete_and_vector_delete_are_incremental(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        removed = await sample(database, 'removed')
        vector_removed = await sample(database, 'vector-removed')
        kept = await sample(database, 'kept')
        await finish(database, concept_id)
        await database.execute('DELETE FROM samples WHERE id=$1', removed)
        await database.execute('DELETE FROM embeddings WHERE sample_id=$1', vector_removed)
        result = await finish(database, concept_id)
        assert result['result']['examined'] == 2
        assert set(await scores(database, concept_id)) == {kept}
        await assert_count(database, concept_id, 1)

    registry_database(scenario)


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


def test_layout_updates_and_unrelated_fields_do_not_queue_concept_work(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        sample_id = await sample(database, 'first')
        await finish(database, concept_id)
        await write_layout(database, sample_id)
        await database.execute('UPDATE embeddings SET vector=vector WHERE sample_id=$1', sample_id)
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 0
        await database.execute(
            "INSERT INTO embeddings(sample_id,field_name,vector) VALUES($1,'different','[1,0]')",
            sample_id,
        )
        await sample(database, 'other-dataset', dataset_id=2)
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 0
        assert await concept_jobs.schedule_concepts(database.pool) == []

    registry_database(scenario)


def test_duplicate_scheduler_submissions_join_started_concept_upkeep(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await sample(database, 'first')
        requests = await asyncio.gather(
            *(concept_jobs.request_scoring(database.pool, concept_id) for attempt in range(5)),
        )
        assert len({job['id'] for job in requests}) == 1
        claim = await job_queue.claim_job(database.pool, 'test', kinds=[concept_jobs.KIND])
        assert claim is not None
        assert (await concept_jobs.request_scoring(database.pool, concept_id))['id'] == claim['id']
        assert await concept_jobs.schedule_concepts(database.pool) == []

    registry_database(scenario)


@pytest.mark.parametrize('stop', ['cancel', 'lease'])
def test_cancel_and_lease_loss_fence_scores_and_keep_work(registry_database, stop):
    async def scenario(database):
        concept_id = await concept(database)
        sample_id = await sample(database, 'first')
        claim, measured = await begin(database, concept_id)
        if stop == 'cancel':
            await job_queue.cancel_job(database.pool, claim['id'])
            result = await publish(database, claim, measured)
            assert result['state'] == 'cancelled'
        else:
            await database.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '60 seconds' WHERE id=$1",
                claim['id'],
            )
            with pytest.raises(job_queue.LeaseLost):
                await publish(database, claim, measured)
        assert await scores(database, concept_id) == {}
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 1
        await finish(database, concept_id)
        assert set(await scores(database, concept_id)) == {sample_id}
        await assert_count(database, concept_id, 1)

    registry_database(scenario)


def test_publication_never_waits_for_interactive_row_or_lifecycle_locks(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        sample_id = await sample(database, 'first')
        claim, measured = await begin(database, concept_id)
        for lock in (
            'SELECT id FROM samples WHERE id=$1 FOR UPDATE',
            'SELECT id FROM embedding_concepts WHERE id=$1 FOR NO KEY UPDATE',
            'SELECT pg_advisory_xact_lock(20261016,$1)',
        ):
            async with database.pool.acquire() as holder, holder.transaction():
                await holder.execute(lock, sample_id if 'FROM samples' in lock else concept_id)
                with pytest.raises((asyncpg.LockNotAvailableError, job_queue.QueueContended)):
                    await asyncio.wait_for(publish(database, claim, measured), 1)
        assert await scores(database, concept_id) == {}
        await publish(database, claim, measured)
        await finish(database, concept_id, claim)
        await assert_count(database, concept_id, 1)

    registry_database(scenario)


def test_a_manual_full_rescore_fences_an_older_measured_job(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        sample_id = await sample(database, 'first')
        claim, measured = await begin(database, concept_id)
        await database.execute("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=$1", sample_id)
        await score(database, concept_id, full=True)
        with pytest.raises(job_queue.QueueContended):
            await publish(database, claim, measured)
        terminal = await publish(database, claim, await concept_jobs.handler(database.pool)(claim))
        assert terminal['state'] == 'succeeded'
        assert (await scores(database, concept_id))[sample_id] == pytest.approx(0.5)
        await assert_count(database, concept_id, 1)

    registry_database(scenario)


def test_deleted_sample_cannot_be_resurrected_by_a_measured_batch(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        sample_id = await sample(database, 'first')
        claim, measured = await begin(database, concept_id)
        await database.execute('DELETE FROM samples WHERE id=$1', sample_id)
        await publish(database, claim, measured)
        await finish(database, concept_id, claim)
        await assert_count(database, concept_id, 0)
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 0

    registry_database(scenario)


@pytest.mark.parametrize('kind', ['seed', 'probe', 'text'])
def test_bounded_jobs_match_sql_scores_and_repeated_scoring_does_not_inflate_counts(registry_database, kind):
    async def scenario(database):
        concept_id = await concept(database, kind=kind, weight='[0.8,-0.6]', bias=0.25)
        for index, vector in enumerate(('[1,0]', '[0,1]', '[-1,0]', '[0,-1]')):
            await sample(database, str(index), vector)
        await score(database, concept_id, full=True)
        expected = await scores(database, concept_id)
        await database.execute('UPDATE embedding_concepts SET score_initialized=FALSE WHERE id=$1', concept_id)
        await finish(database, concept_id)
        assert await scores(database, concept_id) == expected
        await assert_count(database, concept_id, 4)
        await score(database, concept_id, full=True)
        await assert_count(database, concept_id, 4)

    registry_database(scenario)


def test_count_delta_backlogs_are_consumed_in_bounded_publications(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await finish(database, concept_id)
        await database.execute('UPDATE embedding_concepts SET scored_count=2500 WHERE id=$1', concept_id)
        await database.execute(
            """INSERT INTO concept_score_count_deltas(concept_id,transaction_id,delta)
               SELECT $1,number,-1 FROM generate_series(1,2500) number""",
            concept_id,
        )
        claim, measured = await begin(database, concept_id)
        assert not measured.finished
        await publish(database, claim, measured)
        assert await database.fetchval('SELECT count(*) FROM concept_score_count_deltas') == 1500
        await finish(database, concept_id, claim)
        await assert_count(database, concept_id, 0)
        assert await database.fetchval('SELECT count(*) FROM concept_score_count_deltas') == 0

    registry_database(scenario)


def test_cancelled_partial_bootstrap_resumes_with_exact_counts(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(concept_jobs, 'BATCH_SIZE', 1)
        concept_id = await concept(database)
        for index in range(3):
            await sample(database, str(index))
        claim, measured = await begin(database, concept_id)
        await publish(database, claim, measured)
        assert len(await scores(database, concept_id)) == 1
        await job_queue.cancel_job(database.pool, claim['id'])
        await finish(database, concept_id)
        await assert_count(database, concept_id, 3)

    registry_database(scenario)


def test_timed_out_measurement_shrinks_its_next_unit_and_does_not_retry_unchanged(registry_database, monkeypatch):
    async def scenario(database):
        concept_id = await concept(database)
        await sample(database, 'first')
        original = concept_jobs._measure
        sizes = []

        async def oversized(pool, job, size):
            sizes.append(size)
            if size > 64 and not job['checkpoint'].get('count_phase'):
                raise asyncpg.QueryCanceledError('Simulated oversized read')
            return await original(pool, job, size)

        monkeypatch.setattr(concept_jobs, '_measure', oversized)
        await finish(database, concept_id)
        assert sizes == [1024, 512, 256, 128, 64, 64, 64]
        await assert_count(database, concept_id, 1)

    registry_database(scenario)


def test_pool_exhaustion_is_transient_not_an_oversized_sample_failure(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await sample(database, 'first')
        job = await concept_jobs.request_scoring(database.pool, concept_id)
        async with AsyncExitStack() as holders:
            for slot in range(database.pool.get_max_size()):
                await holders.enter_async_context(database.pool.acquire())
            with pytest.raises(job_queue.QueueBusy, match='database connection'):
                await concept_jobs.handler(database.pool)(job)
        await finish(database, concept_id, job)
        await assert_count(database, concept_id, 1)

    registry_database(scenario)


@pytest.mark.parametrize('existing_concept', [False, True])
def test_vector_transaction_started_before_registration_is_not_lost(registry_database, existing_concept):
    async def scenario(database):
        if existing_concept:
            await concept(database, name='existing')
        async with database.pool.acquire() as writer, writer.transaction():
            late = await sample(writer, 'late-registration')
            concept_id = await concept(database)
            await sample(database, 'visible-before-bootstrap')
            await finish(database, concept_id)
            assert late not in await scores(database, concept_id)
        await finish(database, concept_id)
        assert late in await scores(database, concept_id)
        await assert_count(database, concept_id, 2)

    registry_database(scenario)


@pytest.mark.parametrize('definition_change', [False, True])
def test_uncommitted_definition_and_rescore_require_a_post_commit_bootstrap(registry_database, definition_change):
    async def scenario(database):
        if definition_change:
            concept_id = await concept(database)
            await finish(database, concept_id)
        async with database.pool.acquire() as writer, writer.transaction():
            if definition_change:
                await writer.execute("UPDATE embedding_concepts SET weight='[0,1]' WHERE id=$1", concept_id)
            else:
                concept_id = await concept(writer)
            await score(writer, concept_id, full=True)
            late = await sample(database, 'after-in-transaction-rescore')
            await concept_jobs.dispatch_changes(database.pool)
        assert not await database.fetchval('SELECT score_initialized FROM embedding_concepts WHERE id=$1', concept_id)
        jobs = await concept_jobs.schedule_concepts(database.pool)
        assert len(jobs) == 1
        await finish(database, concept_id, jobs[0])
        assert (await scores(database, concept_id))[late] == pytest.approx(0.5 if definition_change else 1.0)
        await assert_count(database, concept_id, 1)

    registry_database(scenario)


def test_manual_rescore_reads_inflight_delivery_without_waiting_or_missing_it(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await score(database, concept_id, full=True)
        sample_id = await sample(database, 'delivery')
        async with database.pool.acquire() as dispatcher:
            transaction = dispatcher.transaction()
            await transaction.start()
            task = None
            try:
                await dispatcher.fetchval('SELECT dispatch_concept_score_work(256,1)')
                task = asyncio.create_task(score(database, concept_id))
                await asyncio.sleep(0.1)
                assert await asyncio.wait_for(asyncio.shield(task), 1) == 1
            finally:
                await transaction.commit()
                if task is not None:
                    await asyncio.wait_for(task, 5)
        assert set(await scores(database, concept_id)) == {sample_id}
        await assert_count(database, concept_id, 1)

    registry_database(scenario)


def test_source_delivery_is_bounded_fair_and_transactional(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(concept_jobs, 'DELIVERY_BATCH_SIZE', 1)
        first_concept = await concept(database)
        second_concept = await concept(database, dataset_id=2)
        for index in range(3):
            await sample(database, f'large-{index}')
        other = await sample(database, 'small', dataset_id=2)
        async with database.pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            await connection.fetchval('SELECT dispatch_concept_score_work(1,1)')
            assert await connection.fetchval('SELECT count(*) FROM concept_score_pending') == 1
            await transaction.rollback()
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 0
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 4
        cursor = await concept_jobs.dispatch_changes(database.pool, max_batches=1)
        assert cursor == 1
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 3
        cursor = await concept_jobs.dispatch_changes(database.pool, after_dataset=cursor, max_batches=1)
        assert cursor == 2
        assert (
            await database.fetchval(
                'SELECT block_id FROM concept_score_pending WHERE concept_id=$1',
                second_concept,
            )
            == other // 1024
        )
        assert (
            await database.fetchval(
                'SELECT count(*) FROM concept_score_pending WHERE concept_id=$1',
                first_concept,
            )
            == 1
        )

    registry_database(scenario)


def test_locked_concept_does_not_block_other_datasets_source_delivery(registry_database):
    async def scenario(database):
        locked = await concept(database)
        available = await concept(database, dataset_id=2)
        await sample(database, 'blocked')
        other = await sample(database, 'available', dataset_id=2)
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('SELECT id FROM embedding_concepts WHERE id=$1 FOR UPDATE', locked)
            cursor = await asyncio.wait_for(concept_jobs.dispatch_changes(database.pool), 2)
            assert cursor == 2
            assert (
                await database.fetchval(
                    'SELECT block_id FROM concept_score_pending WHERE concept_id=$1',
                    available,
                )
                == other // 1024
            )
            assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending WHERE dataset_id=1') == 0
        await concept_jobs.dispatch_changes(database.pool, after_dataset=cursor)
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 0

    registry_database(scenario)


def test_delivery_uses_its_batch_budget_and_coalesces_a_writers_changes(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(concept_jobs, 'DELIVERY_BATCH_SIZE', 2)
        concept_id = await concept(database)
        for index in range(10):
            await sample(database, str(index))
        cursor = await concept_jobs.dispatch_changes(database.pool, max_batches=3)
        assert cursor == 1
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 4
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 6
        async with database.pool.acquire() as writer, writer.transaction():
            sample_id = await sample(writer, 'coalesced')
            await writer.execute("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=$1", sample_id)
            await writer.execute("UPDATE embeddings SET vector='[-1,0]' WHERE sample_id=$1", sample_id)
            assert (
                await writer.fetchval(
                    """SELECT count(*) FROM concept_embedding_pending WHERE block_id=$1::bigint/1024
                       AND transaction_id=txid_current()""",
                    sample_id,
                )
                == 1
            )
        await concept_jobs.dispatch_changes(database.pool)
        await finish(database, concept_id)
        assert (await scores(database, concept_id))[sample_id] == 0
        await assert_count(database, concept_id, 11)

    registry_database(scenario)


def test_work_delivery_and_count_acknowledgement_do_not_scan_a_million_row_backlog(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SET LOCAL statement_timeout='60s'")
            await connection.execute(
                """INSERT INTO concept_embedding_pending(dataset_id,field_name,block_id)
                   SELECT 1,'clip',number FROM generate_series(1,1000000) number""",
            )
            await connection.execute(
                """INSERT INTO concept_score_count_deltas(concept_id,transaction_id,delta)
                   SELECT $1,number,1 FROM generate_series(1,1000000) number""",
                concept_id,
            )
        await database.execute('ANALYZE concept_embedding_pending; ANALYZE concept_score_count_deltas')
        for statement, limit in (
            ('SELECT dispatch_concept_score_work(256,1)', 5000),
            # The deletion write guard captures the 1,000 old rows in a transition table.
            # Allow that bounded heap work, while still rejecting a backlog-sized scan.
            (f'SELECT settle_concept_score_count({concept_id})', 6000),
        ):
            touched = []
            for repeat in range(3):
                plan = await database.fetchval(f'EXPLAIN(ANALYZE,BUFFERS,FORMAT JSON) {statement}')
                if isinstance(plan, str):
                    plan = json.loads(plan)
                root = plan[0]['Plan']
                touched.append(root.get('Shared Hit Blocks', 0) + root.get('Shared Read Blocks', 0))
            assert max(touched[1:]) < limit, (statement, touched)
        assert await database.fetchval('SELECT count(*) FROM concept_embedding_pending') == 1000000 - 3 * 256
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 3 * 256
        assert await database.fetchval('SELECT count(*) FROM concept_score_count_deltas') == 997000
        assert await database.fetchval('SELECT scored_count FROM embedding_concepts WHERE id=$1', concept_id) == 3000

    registry_database(scenario)


def test_concurrent_writers_do_not_share_bookkeeping_locks(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await finish(database, concept_id)
        async with database.pool.acquire() as writer, writer.transaction():
            await sample(writer, 'held')
            await asyncio.wait_for(sample(database, 'independent'), 1)
            await finish(database, concept_id)
        await finish(database, concept_id)
        await assert_count(database, concept_id, 2)

    registry_database(scenario)


def test_deleted_sample_during_bootstrap_count_is_not_double_counted(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        sample_id = await sample(database, 'first')
        claim, measured = await begin(database, concept_id)
        await publish(database, claim, measured)
        claim = await job_queue.claim_job(database.pool, 'test', kinds=[concept_jobs.KIND])
        measured = await concept_jobs.handler(database.pool)(claim)
        assert measured.checkpoint['count_phase']
        await publish(database, claim, measured)
        claim = await job_queue.claim_job(database.pool, 'test', kinds=[concept_jobs.KIND])
        measured = await concept_jobs.handler(database.pool)(claim)
        assert measured.finished
        await database.execute('DELETE FROM samples WHERE id=$1', sample_id)
        await publish(database, claim, measured)
        await assert_count(database, concept_id, 0)

    registry_database(scenario)


def test_model_revision_rebuild_cleans_scores_after_embedding_field_change(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await sample(database, 'first')
        await finish(database, concept_id)
        await database.execute("UPDATE embedding_concepts SET embedding_field='missing' WHERE id=$1", concept_id)
        await finish(database, concept_id)
        await assert_count(database, concept_id, 0)

    registry_database(scenario)


def test_bad_concept_does_not_stop_other_datasets(registry_database):
    async def scenario(database):
        invalid = await concept(database, weight='[1,0,0]')
        valid = await concept(database, dataset_id=2)
        await sample(database, 'first')
        await sample(database, 'other', dataset_id=2)
        await concept_jobs.request_scoring(database.pool, invalid)
        good = await concept_jobs.request_scoring(database.pool, valid)
        await finish(database, valid, good)
        assert await database.fetchval("SELECT state FROM jobs WHERE args->>'concept_id'=$1", str(invalid)) == 'failed'
        await assert_count(database, valid, 1)
        assert await concept_jobs.schedule_concepts(database.pool) == []

    registry_database(scenario)


def test_vector_trigger_restores_with_pg_dumps_empty_search_path(registry_database):
    async def scenario(database):
        sample_id = await sample(database, 'restore')
        await database.execute('DELETE FROM concept_embedding_pending')
        namespace = await database.fetchval('SELECT current_schema()')
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SET LOCAL search_path=''")
            definition = await connection.fetchval(
                """SELECT pg_catalog.pg_get_triggerdef(oid) FROM pg_catalog.pg_trigger
                   WHERE tgrelid=$1::pg_catalog.regclass AND tgname='concept_embeddings_updated'""",
                f'{namespace}.embeddings',
            )
            await connection.execute(f'DROP TRIGGER concept_embeddings_updated ON {namespace}.embeddings')
            await connection.execute(definition)
        await database.execute("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=$1", sample_id)
        assert await database.fetchval('SELECT block_id FROM concept_embedding_pending') == sample_id // 1024

    registry_database(scenario)


def test_local_service_automatically_scores_ingest_and_recovers_queued_changes_after_restart(
    registry_database,
    tmp_path,
):
    async def scenario(database):
        concept_id = await concept(database)
        first = await sample(database, 'first')
        namespace = await database.fetchval('SELECT current_schema()')
        environment = {
            **os.environ,
            'DATABASE_URL': os.environ['FRIDGE_TEST_POSTGRES_DSN'] + '?search_path=' + namespace + ',public',
            'JOB_CONCEPT_SECONDS': '0.1',
            'JOB_STATISTICS_SECONDS': '0.1',
            'JOB_WORKER_SLOTS': '2',
            'JOB_POOL_MAX_SIZE': '3',
            'JOB_SHUTDOWN_SECONDS': '2',
        }
        for iteration in range(2):
            if iteration:
                await sample(database, 'while-stopped')
                await database.execute("UPDATE embeddings SET vector='[0,1]' WHERE sample_id=$1", first)
            log_path = tmp_path / f'concept-service-{iteration}.log'
            with log_path.open('wb') as log:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    '-m',
                    'backend.job_service',
                    cwd=Path(__file__).resolve().parents[2],
                    env=environment,
                    stdout=log,
                    stderr=log,
                )
                try:
                    async with asyncio.timeout(20):
                        while True:
                            assert process.returncode is None, log_path.read_text()
                            row = await database.fetchrow(
                                'SELECT score_initialized,scored_count FROM embedding_concepts WHERE id=$1',
                                concept_id,
                            )
                            pending = await database.fetchval(
                                """SELECT (SELECT count(*) FROM concept_embedding_pending)
                                     +(SELECT count(*) FROM concept_score_pending)""",
                            )
                            completed = await database.fetchval(
                                "SELECT count(*) FROM jobs WHERE kind='concept_scoring' AND state='succeeded'",
                            )
                            if (
                                row['score_initialized']
                                and row['scored_count'] == iteration + 1
                                and not pending
                                and completed >= iteration + 1
                            ):
                                break
                            await asyncio.sleep(0.05)
                    assert (await scores(database, concept_id))[first] == pytest.approx(0.5 if iteration else 1.0)
                finally:
                    if process.returncode is None:
                        process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 10)
                        except TimeoutError:
                            process.kill()
                            await process.wait()
                assert process.returncode == 0, log_path.read_text()

    registry_database(scenario)
