"""Queue concurrency contracts, exclusively on an explicit disposable database."""

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from backend import job_queue
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def expire(database, claim):
    await database.execute(
        "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '60 seconds' WHERE id=$1",
        claim['id'],
    )


async def claimed(pool, worker_id, **options) -> job_queue.Job:
    result = await job_queue.claim_job(pool, worker_id, **options)
    assert result is not None
    return result


def test_enqueue_deduplicates_concurrent_identical_requests(registry_database):
    async def scenario(database):
        first, second = await asyncio.gather(
            job_queue.enqueue_job(database.pool, 'stats', dataset_id=1, args={'force': True}, dedupe_key='stats:1'),
            job_queue.enqueue_job(database.pool, 'stats', dataset_id=1, args={'force': True}, dedupe_key='stats:1'),
        )
        assert first['id'] == second['id']
        assert await database.fetchval('SELECT count(*) FROM jobs') == 1
        assert first['args'] == {'force': True} and first['checkpoint'] == {}

    registry_database(scenario)


@pytest.mark.parametrize(
    'changed',
    [{'args': {'force': False}}, {'dataset_id': 2}, {'placement': 'compute'}, {'resources': {'gpu': True}}],
)
def test_active_dedupe_rejects_different_request(registry_database, changed):
    async def scenario(database):
        request: dict[str, Any] = {'dataset_id': 1, 'args': {'force': True}, 'dedupe_key': 'stats:1'}
        await job_queue.enqueue_job(database.pool, 'stats', **request)
        with pytest.raises(job_queue.DedupeConflict):
            await job_queue.enqueue_job(database.pool, 'stats', **(request | changed))

    registry_database(scenario)


def test_terminal_job_releases_dedupe_key(registry_database):
    async def scenario(database):
        original = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1, dedupe_key='stats:1')
        claim = await claimed(database.pool, 'worker')
        await job_queue.complete_job(database.pool, claim['id'], claim['lease_token'], {'fields': 7})
        next_job = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1, dedupe_key='stats:1')
        assert next_job['id'] != original['id']
        assert (await database.fetchrow('SELECT * FROM jobs WHERE id=$1', original['id']))['result'] == {'fields': 7}

    registry_database(scenario)


def test_simultaneous_claims_are_distinct_and_exclude_same_dataset(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        await job_queue.enqueue_job(database.pool, 'export', dataset_id=1)
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=2)
        results = await asyncio.gather(
            job_queue.claim_job(database.pool, 'first'),
            job_queue.claim_job(database.pool, 'second'),
        )
        claims = [claim for claim in results if claim is not None]
        while len(claims) < 2:
            claim = await job_queue.claim_job(database.pool, 'retry')
            assert claim is not None
            claims.append(claim)
        assert len({claim['id'] for claim in claims}) == 2
        assert {claim['dataset_id'] for claim in claims} == {1, 2}
        assert await job_queue.claim_job(database.pool, 'third') is None

    registry_database(scenario)


def test_locked_jobs_and_lanes_do_not_block_claims_or_dataset_writes(registry_database):
    async def scenario(database):
        first = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        second = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=2)
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('SELECT id FROM jobs WHERE id=$1 FOR UPDATE', first['id'])
            await holder.execute('SELECT dataset_id FROM dataset_job_lanes WHERE dataset_id=1 FOR UPDATE')
            await holder.execute('SELECT id FROM datasets WHERE id=2 FOR UPDATE')
            claim = await asyncio.wait_for(claimed(database.pool, 'worker'), timeout=1)
            assert claim['id'] == second['id']
        await asyncio.wait_for(database.execute("UPDATE datasets SET description='editable' WHERE id=2"), timeout=1)

    registry_database(scenario)


def test_running_dataset_backlog_does_not_hide_other_ready_dataset(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        await job_queue.claim_job(database.pool, 'busy')
        for _ in range(40):
            await job_queue.enqueue_job(database.pool, 'export', dataset_id=1)
        ready = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=2)
        assert (await claimed(database.pool, 'free'))['id'] == ready['id']

    registry_database(scenario)


def test_placement_and_handler_filter_precede_claim_limit(registry_database):
    async def scenario(database):
        for _ in range(40):
            await job_queue.enqueue_job(database.pool, 'umap', dataset_id=1, placement='compute', lane='maintenance')
        await job_queue.enqueue_job(database.pool, 'unsupported')
        target = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        assert (await claimed(database.pool, 'server', kinds=['stats']))['id'] == target['id']
        remote = await claimed(database.pool, 'remote', placements=['compute'], kinds=['umap'])
        assert remote['placement'] == 'compute'

    registry_database(scenario)


def test_expired_lease_resumes_checkpoint_and_fences_all_stale_writes(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        old = await claimed(database.pool, 'old')
        await job_queue.checkpoint_job(database.pool, old['id'], old['lease_token'], checkpoint={'block': 4}, done=4)
        await expire(database, old)
        new = await claimed(database.pool, 'new')
        assert new['id'] == old['id'] and new['attempt'] == 2
        assert new['checkpoint'] == {'block': 4} and new['progress_done'] == 4
        assert new['lease_token'] != old['lease_token']
        assert await job_queue.heartbeat_job(database.pool, old['id'], old['lease_token']) is None
        invoked = []

        async def publisher(connection):
            invoked.append(True)
            await connection.execute("UPDATE datasets SET description='stale' WHERE id=1")

        operations = (
            job_queue.publish_step(database.pool, old['id'], old['lease_token'], publisher=publisher),
            job_queue.complete_job(database.pool, old['id'], old['lease_token']),
            job_queue.fail_job(database.pool, old['id'], old['lease_token'], {'message': 'stale'}),
            job_queue.defer_job(database.pool, old['id'], old['lease_token'], {'message': 'stale'}),
        )
        for operation in operations:
            with pytest.raises(job_queue.LeaseLost):
                await operation
        assert not invoked
        await job_queue.complete_job(database.pool, new['id'], new['lease_token'])

    registry_database(scenario)


def test_expired_owner_cannot_renew_or_publish_before_reclaim(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats')
        claim = await claimed(database.pool, 'worker')
        await expire(database, claim)
        assert await job_queue.heartbeat_job(database.pool, claim['id'], claim['lease_token']) is None
        with pytest.raises(job_queue.LeaseLost):
            await job_queue.publish_step(database.pool, claim['id'], claim['lease_token'], checkpoint={'bad': True})

    registry_database(scenario)


def test_heartbeat_waiting_across_expiry_cannot_resurrect_lease(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats')
        claim = await claimed(database.pool, 'worker', lease_seconds=0.3)
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('SELECT id FROM jobs WHERE id=$1 FOR UPDATE', claim['id'])
            await asyncio.sleep(0.25)
            heartbeat = asyncio.create_task(job_queue.heartbeat_job(database.pool, claim['id'], claim['lease_token']))
            await asyncio.sleep(0.075)
        assert await heartbeat is None
        current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', claim['id'])
        assert current['lease_expires_at'] == claim['lease_expires_at']

    registry_database(scenario)


def test_cancel_expired_running_job_finishes_without_worker(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats')
        claim = await claimed(database.pool, 'dead')
        await expire(database, claim)
        cancelled = await job_queue.cancel_job(database.pool, claim['id'])
        assert cancelled is not None
        assert cancelled['state'] == 'cancelled' and cancelled['lease_token'] is None
        assert cancelled['finished_at'] is not None
        with pytest.raises(job_queue.LeaseLost):
            await job_queue.complete_job(database.pool, claim['id'], claim['lease_token'])

    registry_database(scenario)


def test_publisher_failure_rolls_back_effects_and_checkpoint(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        claim = await claimed(database.pool, 'worker')

        async def publisher(connection):
            await connection.execute("UPDATE datasets SET description='must rollback' WHERE id=1")
            raise RuntimeError('publication failed')

        with pytest.raises(RuntimeError, match='publication failed'):
            await job_queue.publish_step(
                database.pool,
                claim['id'],
                claim['lease_token'],
                checkpoint={'block': 1},
                done=1,
                publisher=publisher,
            )
        current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', claim['id'])
        assert current['checkpoint'] == {} and current['progress_done'] == 0
        assert await database.fetchval('SELECT description FROM datasets WHERE id=1') != 'must rollback'

    registry_database(scenario)


def test_successful_unit_commits_effects_with_checkpoint_and_yields_fairly(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        claim = await claimed(database.pool, 'worker')
        second = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=2)

        async def publisher(connection):
            await connection.execute("UPDATE datasets SET description='block saved' WHERE id=1")

        released = await job_queue.publish_step(
            database.pool,
            claim['id'],
            claim['lease_token'],
            checkpoint={'block': 1},
            done=1,
            total=9,
            message='one block',
            publisher=publisher,
            release=True,
        )
        assert released['state'] == 'queued' and released['lease_token'] is None
        assert released['checkpoint'] == {'block': 1} and released['progress_total'] == 9
        assert await database.fetchval('SELECT description FROM datasets WHERE id=1') == 'block saved'
        assert (await claimed(database.pool, 'worker'))['id'] == second['id']

    registry_database(scenario)


def test_retry_preserves_checkpoint_and_defers_only_failed_job(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        claim = await claimed(database.pool, 'worker')
        await job_queue.checkpoint_job(
            database.pool,
            claim['id'],
            claim['lease_token'],
            checkpoint={'block': 7},
            done=7,
        )
        deferred = await job_queue.defer_job(
            database.pool,
            claim['id'],
            claim['lease_token'],
            {'message': 'timeout'},
            delay_seconds=30,
        )
        assert deferred['checkpoint'] == {'block': 7} and deferred['progress_done'] == 7
        assert deferred['error'] == {'message': 'timeout'} and deferred['state'] == 'queued'
        next_job = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=2)
        assert (await claimed(database.pool, 'worker'))['id'] == next_job['id']
        assert await job_queue.claim_job(database.pool, 'worker') is None

    registry_database(scenario)


@pytest.mark.parametrize('started', [False, True])
def test_cancel_wins_before_publication_and_is_idempotent(registry_database, started):
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        claim = await claimed(database.pool, 'worker') if started else None
        cancelled = await job_queue.cancel_job(database.pool, job['id'])
        assert cancelled is not None
        assert cancelled['cancel_requested'] is True
        if started:
            assert claim is not None
            assert cancelled['state'] == 'running'

            async def publisher(connection):
                pytest.fail('Cancelled work must not publish')

            cancelled = await job_queue.publish_step(
                database.pool,
                claim['id'],
                claim['lease_token'],
                publisher=publisher,
                finished=True,
            )
        assert cancelled['state'] == 'cancelled'
        assert (await job_queue.cancel_job(database.pool, job['id'])) == cancelled
        assert await job_queue.cancel_job(database.pool, uuid4()) is None

    registry_database(scenario)


def test_completion_wins_when_cancel_arrives_during_atomic_publication(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        claim = await claimed(database.pool, 'worker')
        publishing, release = asyncio.Event(), asyncio.Event()

        async def publisher(connection):
            publishing.set()
            await release.wait()
            await connection.execute("UPDATE datasets SET description='completed' WHERE id=1")

        completion = asyncio.create_task(
            job_queue.publish_step(
                database.pool,
                claim['id'],
                claim['lease_token'],
                publisher=publisher,
                finished=True,
            ),
        )
        await publishing.wait()
        cancellation = asyncio.create_task(job_queue.cancel_job(database.pool, claim['id']))
        cancelled = await asyncio.wait_for(cancellation, 1)
        assert cancelled is not None
        assert cancelled['state'] == 'running' and cancelled['cancel_requested']
        release.set()
        completed, cancelled = await asyncio.gather(completion, cancellation)
        assert cancelled is not None
        assert completed['state'] == 'succeeded'
        assert completed['cancel_requested'] is True

    registry_database(scenario)


def test_crashed_cancelled_owner_is_reaped_and_releases_dataset_lane(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        claim = await claimed(database.pool, 'dead')
        await job_queue.cancel_job(database.pool, claim['id'])
        await expire(database, claim)
        next_job = await job_queue.enqueue_job(database.pool, 'export', dataset_id=1)
        next_claim = await claimed(database.pool, 'replacement')
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', claim['id']) == 'cancelled'
        assert next_claim['id'] == next_job['id']

    registry_database(scenario)


def test_expired_unsupported_owner_requires_resumption_or_explicit_cancellation(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'old_handler', dataset_id=1)
        expired = await claimed(database.pool, 'old-worker', kinds=['old_handler'])
        await expire(database, expired)
        ready = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        assert await job_queue.claim_job(database.pool, 'new-worker', kinds=['stats']) is None
        old = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', expired['id'])
        assert old['state'] == 'queued' and old['lease_token'] is None
        await job_queue.cancel_job(database.pool, expired['id'])
        assert (await claimed(database.pool, 'new-worker', kinds=['stats']))['id'] == ready['id']

    registry_database(scenario)


def test_job_history_survives_dataset_deletion_and_enqueue_checks_existence(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'delete', dataset_id=1)
        claim = await claimed(database.pool, 'worker')

        async def publisher(connection):
            await connection.execute('DELETE FROM datasets WHERE id=1')

        completed = await job_queue.publish_step(
            database.pool,
            claim['id'],
            claim['lease_token'],
            publisher=publisher,
            result={'deleted': True},
            finished=True,
        )
        assert completed['dataset_id'] == 1 and completed['state'] == 'succeeded'
        assert await database.fetchval('SELECT count(*) FROM dataset_job_lanes WHERE dataset_id=1') == 0
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await job_queue.enqueue_job(database.pool, 'umap', dataset_id=1, placement='compute')

    registry_database(scenario)


def test_deleted_dataset_backlog_cannot_starve_valid_jobs(registry_database):
    async def scenario(database):
        for _ in range(40):
            await job_queue.enqueue_job(database.pool, 'unsupported', dataset_id=1)
        await database.execute('DELETE FROM datasets WHERE id=1')
        ready = await job_queue.enqueue_job(database.pool, 'stats', dataset_id=2)
        assert (await claimed(database.pool, 'worker', kinds=['stats']))['id'] == ready['id']
        assert await database.fetchval("SELECT count(*) FROM jobs WHERE dataset_id=1 AND state='cancelled'") == 32
        await job_queue.claim_job(database.pool, 'worker', kinds=['stats'])
        assert await database.fetchval("SELECT count(*) FROM jobs WHERE dataset_id=1 AND state='cancelled'") == 40
        assert await database.fetchval("SELECT bool_and(error->>'code'='dataset_deleted') FROM jobs WHERE dataset_id=1")

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['heartbeat', 'publish', 'fail', 'defer'])
def test_state_transitions_wait_only_briefly_for_job_row(registry_database, operation):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        claim = await claimed(database.pool, 'worker')
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('SELECT id FROM jobs WHERE id=$1 FOR UPDATE', claim['id'])
            if operation == 'cancel':
                action = job_queue.cancel_job(database.pool, claim['id'])
            elif operation == 'heartbeat':
                action = job_queue.heartbeat_job(database.pool, claim['id'], claim['lease_token'])
            elif operation == 'publish':
                action = job_queue.publish_step(database.pool, claim['id'], claim['lease_token'], done=1)
            elif operation == 'fail':
                action = job_queue.fail_job(database.pool, claim['id'], claim['lease_token'], {'message': 'fail'})
            else:
                action = job_queue.defer_job(database.pool, claim['id'], claim['lease_token'], {'message': 'retry'})
            with pytest.raises(job_queue.QueueBusy):
                await asyncio.wait_for(action, timeout=1)
        current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', claim['id'])
        assert current['state'] == 'running'
        assert not await database.fetchval('SELECT cancel_requested FROM job_controls WHERE job_id=$1', claim['id'])
        assert current['progress_done'] == 0

    registry_database(scenario)


def test_blocked_effect_write_does_not_hold_job_indefinitely(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        claim = await claimed(database.pool, 'worker')

        async def publisher(connection):
            await connection.execute("UPDATE datasets SET description='blocked' WHERE id=1")

        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('SELECT id FROM datasets WHERE id=1 FOR UPDATE')
            with pytest.raises(job_queue.QueueBusy):
                await asyncio.wait_for(
                    job_queue.publish_step(
                        database.pool,
                        claim['id'],
                        claim['lease_token'],
                        publisher=publisher,
                        done=1,
                    ),
                    timeout=1,
                )
            cancelled = await asyncio.wait_for(job_queue.cancel_job(database.pool, claim['id']), timeout=1)
            assert cancelled is not None
            assert cancelled['cancel_requested'] is True
        assert await database.fetchval('SELECT progress_done FROM jobs WHERE id=$1', claim['id']) == 0

    registry_database(scenario)


def test_lease_expiry_during_publisher_rolls_back_effect_and_checkpoint(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        claim = await claimed(database.pool, 'worker', lease_seconds=0.1)

        async def publisher(connection):
            await connection.execute("UPDATE datasets SET description='expired' WHERE id=1")
            await connection.execute('SELECT pg_sleep(0.2)')

        with pytest.raises(job_queue.LeaseLost):
            await job_queue.publish_step(
                database.pool,
                claim['id'],
                claim['lease_token'],
                publisher=publisher,
                checkpoint={'bad': True},
                done=1,
                finished=True,
            )
        current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', claim['id'])
        assert current['checkpoint'] == {} and current['progress_done'] == 0 and current['state'] == 'running'
        assert await database.fetchval('SELECT description FROM datasets WHERE id=1') != 'expired'

    registry_database(scenario)


def test_callback_wall_time_budget_rolls_back_prior_sql_effect(registry_database, monkeypatch):
    monkeypatch.setattr(job_queue, 'PUBLISH_TIMEOUT_SECONDS', 0.05)

    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats', dataset_id=1)
        claim = await claimed(database.pool, 'worker')

        async def publisher(connection):
            await connection.execute("UPDATE datasets SET description='must rollback' WHERE id=1")
            await asyncio.sleep(10)

        with pytest.raises(job_queue.QueueBusy):
            await job_queue.publish_step(
                database.pool,
                claim['id'],
                claim['lease_token'],
                publisher=publisher,
                done=1,
                finished=True,
            )
        assert await database.fetchval('SELECT description FROM datasets WHERE id=1') != 'must rollback'
        current = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', claim['id'])
        assert current['state'] == 'running' and current['progress_done'] == 0

    registry_database(scenario)


def test_exhausted_connection_pool_returns_bounded_retryable_error(registry_database, monkeypatch):
    monkeypatch.setattr(job_queue, 'ACQUIRE_TIMEOUT_SECONDS', 0.05)

    async def scenario(database):
        holders = [await database.pool.acquire() for _ in range(database.pool.get_max_size())]
        try:
            with pytest.raises(job_queue.QueueBusy):
                await asyncio.wait_for(job_queue.enqueue_job(database.pool, 'stats'), timeout=1)
        finally:
            await asyncio.gather(*(database.pool.release(holder) for holder in holders))
        assert await database.fetchval('SELECT count(*) FROM jobs') == 0

    registry_database(scenario)


def test_final_null_result_replaces_partial_result(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'stats')
        claim = await claimed(database.pool, 'worker')
        partial = await job_queue.publish_step(
            database.pool,
            claim['id'],
            claim['lease_token'],
            result={'partial': True},
        )
        assert partial['result'] == {'partial': True}
        finished = await job_queue.complete_job(database.pool, claim['id'], claim['lease_token'])
        assert finished['state'] == 'succeeded' and finished['result'] is None

    registry_database(scenario)


@pytest.mark.parametrize('duration', [0, -1, float('nan'), float('inf')])
def test_invalid_lease_duration_rejected_before_database_access(duration):
    async def scenario():
        with pytest.raises(ValueError, match='finite'):
            await job_queue.claim_job(AsyncMock(), 'worker', lease_seconds=duration)

    asyncio.run(scenario())
