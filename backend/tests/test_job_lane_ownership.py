"""Logical operation ordering across worker yields, retries and lease recovery."""

import asyncio
import os
import time

import asyncpg
import pytest

from backend import job_queue
from backend.job_heartbeat import heartbeat_connector
from backend.job_runner import JobRunner, StepResult
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_job_queue_postgres import claimed

registry_database = registry_helpers.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


@pytest.mark.parametrize('lane', ['user', 'maintenance'])
def test_yield_preserves_operation_order_without_holding_the_worker_slot(registry_database, lane):
    async def scenario(database):
        sample_ids = (await registry_helpers.ingest([{}, {}]))['ids']
        first = await job_queue.enqueue_job(database.pool, 'chunked_tag', dataset_id=1, lane=lane)
        second = await job_queue.enqueue_job(database.pool, 'reset_tags', dataset_id=1, lane=lane)
        first_claim = await claimed(database.pool, 'first-slot')
        assert first_claim['id'] == first['id']
        independent = await job_queue.enqueue_job(database.pool, 'other_dataset', dataset_id=2, lane=lane)

        async def first_chunk(connection):
            await connection.execute("UPDATE samples SET tags=ARRAY['tagged'] WHERE id=$1", sample_ids[0])

        await job_queue.publish_step(
            database.pool,
            first['id'],
            first_claim['lease_token'],
            checkpoint={'after_id': sample_ids[0]},
            done=1,
            total=2,
            release=True,
            publisher=first_chunk,
        )
        other_claim = await claimed(database.pool, 'second-slot')
        assert other_claim['id'] == independent['id']
        resumed = await claimed(database.pool, 'third-slot')
        assert resumed['id'] == first['id']

        async def second_chunk(connection):
            await connection.execute("UPDATE samples SET tags=ARRAY['tagged'] WHERE id=$1", sample_ids[1])

        await job_queue.publish_step(
            database.pool,
            first['id'],
            resumed['lease_token'],
            done=2,
            finished=True,
            publisher=second_chunk,
        )
        following = await claimed(database.pool, 'fourth-slot')
        assert following['id'] == second['id']

        async def reset(connection):
            await connection.execute("UPDATE samples SET tags=ARRAY[]::text[] WHERE dataset_id=1")

        await job_queue.publish_step(
            database.pool,
            second['id'],
            following['lease_token'],
            finished=True,
            publisher=reset,
        )
        assert [row['tags'] for row in await database.fetch('SELECT tags FROM samples ORDER BY id')] == [[], []]

    registry_database(scenario)


@pytest.mark.parametrize('transition', ['retry', 'expired'])
def test_backoff_keeps_lane_but_not_edits_or_other_datasets_waiting(registry_database, transition):
    async def scenario(database):
        first = await job_queue.enqueue_job(database.pool, 'first', dataset_id=1)
        second = await job_queue.enqueue_job(database.pool, 'second', dataset_id=1)
        original = await claimed(database.pool, 'original')
        if transition == 'retry':
            await job_queue.defer_job(
                database.pool,
                original['id'],
                original['lease_token'],
                {'message': 'temporary interruption'},
                delay_seconds=30,
            )
        else:
            await database.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 millisecond' WHERE id=$1",
                original['id'],
            )
        independent = await job_queue.enqueue_job(database.pool, 'other_dataset', dataset_id=2)
        maintenance = await job_queue.enqueue_job(database.pool, 'upkeep', dataset_id=1, lane='maintenance')
        assert (await claimed(database.pool, 'free-slot'))['id'] == independent['id']
        assert (await claimed(database.pool, 'another-slot'))['id'] == maintenance['id']
        assert await job_queue.claim_job(database.pool, 'waiting') is None
        await asyncio.wait_for(registry_helpers.ingest([{'source': transition}]), 1)
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id=1') == 1
        await database.execute("UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1", first['id'])
        resumed = await claimed(database.pool, 'resumed')
        assert resumed['id'] == first['id']
        with pytest.raises(job_queue.LeaseLost):
            await job_queue.complete_job(database.pool, original['id'], original['lease_token'])
        await job_queue.complete_job(database.pool, resumed['id'], resumed['lease_token'])
        assert (await claimed(database.pool, 'next'))['id'] == second['id']

    registry_database(scenario)


def test_unsupported_owner_stays_exclusive_until_cancelled(registry_database):
    async def scenario(database):
        original = await job_queue.enqueue_job(database.pool, 'remote_fit', dataset_id=1, placement='compute')
        owner = await claimed(database.pool, 'remote', placements=['compute'])
        second = await job_queue.enqueue_job(database.pool, 'local_mutation', dataset_id=1)
        await job_queue.publish_step(database.pool, owner['id'], owner['lease_token'], release=True)
        assert await job_queue.claim_job(database.pool, 'local', kinds=['local_mutation']) is None
        await job_queue.cancel_job(database.pool, original['id'])
        assert (await claimed(database.pool, 'local', kinds=['local_mutation']))['id'] == second['id']

    registry_database(scenario)


@pytest.mark.parametrize('started', [False, True])
def test_cancelled_queued_job_is_reaped_without_its_kind_or_lane_being_claimable(registry_database, started):
    async def scenario(database):
        original = await job_queue.enqueue_job(database.pool, 'remote_fit', dataset_id=1, placement='compute')
        if started:
            owner = await claimed(database.pool, 'remote', placements=['compute'])
            await job_queue.publish_step(database.pool, owner['id'], owner['lease_token'], release=True)
        else:
            await job_queue.enqueue_job(database.pool, 'blocker', dataset_id=1)
            await claimed(database.pool, 'blocker', kinds=['blocker'])
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('SELECT id FROM jobs WHERE id=$1 FOR UPDATE', original['id'])
            cancellation = await job_queue.cancel_job(database.pool, original['id'])
            assert cancellation is not None
            assert cancellation['state'] == 'queued' and cancellation['cancel_requested']
            await job_queue.claim_job(database.pool, 'reaper', kinds=['unrelated'])
            assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', original['id']) == 'queued'
        await job_queue.claim_job(database.pool, 'reaper', kinds=['unrelated'])
        result = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', original['id'])
        assert result['state'] == 'cancelled' and result['finished_at'] is not None

    registry_database(scenario)


def test_waiting_backlog_does_not_hide_another_dataset(registry_database):
    async def scenario(database):
        owner = await job_queue.enqueue_job(database.pool, 'owner', dataset_id=1)
        active = await claimed(database.pool, 'owner')
        await job_queue.defer_job(database.pool, owner['id'], active['lease_token'], {}, delay_seconds=30)
        for number in range(40):
            await job_queue.enqueue_job(database.pool, f'waiting-{number}', dataset_id=1)
        independent = await job_queue.enqueue_job(database.pool, 'independent', dataset_id=2)
        assert (await claimed(database.pool, 'independent'))['id'] == independent['id']

    registry_database(scenario)


@pytest.mark.parametrize('terminal', ['succeeded', 'cancelled', 'failed'])
def test_terminal_transition_releases_logical_lane(registry_database, terminal):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'owner', dataset_id=1)
        active = await claimed(database.pool, 'owner')
        following = await job_queue.enqueue_job(database.pool, 'following', dataset_id=1)
        if terminal == 'failed':
            result = await job_queue.fail_job(database.pool, active['id'], active['lease_token'], {'reason': 'test'})
        elif terminal == 'cancelled':
            await job_queue.cancel_job(database.pool, active['id'])
            result = await job_queue.publish_step(database.pool, active['id'], active['lease_token'], release=True)
        else:
            result = await job_queue.complete_job(database.pool, active['id'], active['lease_token'])
        assert result['state'] == terminal
        assert (await claimed(database.pool, 'next'))['id'] == following['id']

    registry_database(scenario)


def test_database_rejects_a_second_lane_owner_even_without_a_live_lease(registry_database):
    async def scenario(database):
        await job_queue.enqueue_job(database.pool, 'owner', dataset_id=1)
        active = await claimed(database.pool, 'owner')
        await job_queue.publish_step(database.pool, active['id'], active['lease_token'], release=True)
        following = await job_queue.enqueue_job(database.pool, 'following', dataset_id=1)
        with pytest.raises(asyncpg.UniqueViolationError):
            await database.execute('UPDATE jobs SET started_at=clock_timestamp() WHERE id=$1', following['id'])
        assert await database.fetchval('SELECT started_at FROM jobs WHERE id=$1', following['id']) is None

    registry_database(scenario)


def test_large_history_and_blocked_backlog_do_not_hide_ready_work(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO jobs(id,kind,state,finished_at)
               SELECT md5('history-'||number::text)::uuid,'history','cancelled',clock_timestamp()
               FROM generate_series(1,100000) AS number;
               UPDATE job_controls SET cancel_requested=TRUE""",
        )
        owner = await job_queue.enqueue_job(database.pool, 'owner', dataset_id=1)
        active = await claimed(database.pool, 'owner')
        await job_queue.defer_job(database.pool, owner['id'], active['lease_token'], {}, delay_seconds=300)
        await database.execute(
            """INSERT INTO jobs(id,kind,dataset_id)
               SELECT md5('waiting-'||number::text)::uuid,'waiting',1
               FROM generate_series(1,2000) AS number;
               ANALYZE jobs; ANALYZE job_controls""",
        )
        independent = await job_queue.enqueue_job(database.pool, 'independent', dataset_id=2)
        started = time.monotonic()
        assert (await claimed(database.pool, 'independent'))['id'] == independent['id']
        print(f'Claim past 100k terminal jobs and 2k blocked jobs: {1000 * (time.monotonic() - started):.2f} ms')
        assert await job_queue.claim_job(database.pool, 'nothing-else') is None

    registry_database(scenario)


def test_three_real_slots_never_interleave_same_lane_operations(registry_database):
    async def scenario(database):
        await database.execute(
            """CREATE TABLE operation_trace (
                   position BIGSERIAL PRIMARY KEY, dataset_id INTEGER, lane TEXT, job_id UUID, unit INTEGER,
                   UNIQUE(job_id,unit))""",
        )
        groups = {}
        for dataset_id in (1, 2):
            for lane in ('user', 'maintenance'):
                groups[dataset_id, lane] = [
                    await job_queue.enqueue_job(database.pool, 'chunked', dataset_id=dataset_id, lane=lane)
                    for _ in range(2)
                ]

        async def handler(job):
            unit = job['checkpoint'].get('unit', 0) + 1
            await asyncio.sleep(0.002)

            async def publish(connection):
                await connection.execute(
                    'INSERT INTO operation_trace(dataset_id,lane,job_id,unit) VALUES ($1,$2,$3,$4)',
                    job['dataset_id'],
                    job['lane'],
                    job['id'],
                    unit,
                )

            return StepResult(checkpoint={'unit': unit}, finished=unit == 6, publish=publish)

        namespace = await database.fetchval('SELECT current_schema()')
        runner = JobRunner(
            database.pool,
            {'chunked': handler},
            heartbeat_connect=heartbeat_connector(
                os.environ['FRIDGE_TEST_POSTGRES_DSN'],
                server_settings={'search_path': namespace + ',public'},
            ),
            slots=3,
            step_budget_seconds=0.001,
            poll_seconds=0.01,
        )
        execution = asyncio.create_task(runner.run())

        async def all_completed():
            while await database.fetchval("SELECT count(*) FROM jobs WHERE state='succeeded'") < 8:
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(all_completed(), 10)
        finally:
            runner.stop()
            await asyncio.wait_for(execution, 5)
        for (dataset_id, lane), operations in groups.items():
            rows = await database.fetch(
                'SELECT job_id,unit FROM operation_trace WHERE dataset_id=$1 AND lane=$2 ORDER BY position',
                dataset_id,
                lane,
            )
            assert [(row['job_id'], row['unit']) for row in rows] == [
                (operation['id'], unit) for operation in operations for unit in range(1, 7)
            ]
        assert await database.fetchval('SELECT min(attempt) FROM jobs') == 6

    registry_database(scenario)
