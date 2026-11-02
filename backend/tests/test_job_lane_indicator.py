"""A yielded operation remains visible independently of its followers' waiting cursor."""

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend import access, job_queue
from backend.routers import jobs
from backend.tests import test_field_registry_postgres
from backend.tests.test_job_cancel_history import CASES
from backend.tests.test_job_indicator_privacy import PUBLIC_FIELDS
from backend.tests.test_job_queue_postgres import claimed

if TYPE_CHECKING:
    from uuid import UUID

registry_database = test_field_registry_postgres.registry_database
USER = {'id': 1, 'role': 'admin'}
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an explicitly isolated PostgreSQL database',
)


@pytest.mark.parametrize('role,identifier,creator,allowed', CASES)
def test_resuming_owner_preserves_cancel_permissions_and_privacy(
    registry_database,
    monkeypatch,
    role,
    identifier,
    creator,
    allowed,
):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute('UPDATE datasets SET is_global=TRUE WHERE id=1')
        await database.execute("INSERT INTO users(id,username,role) VALUES (10,'Owner','power_user')")
        user = {'id': identifier, 'role': role}
        owner = await job_queue.enqueue_job(database.pool, 'owner', dataset_id=1, created_by=creator)
        active = await claimed(database.pool, 'owner')
        await job_queue.defer_job(
            database.pool,
            owner['id'],
            active['lease_token'],
            {'message': 'private diagnostics'},
            delay_seconds=60,
        )
        page = await jobs.list_active_dataset_jobs(1, limit=25, cursor=None, user=user)
        assert page.running == [] and page.waiting == []
        assert len(page.resuming) == 1
        assert page.resuming[0].id == owner['id']
        assert page.resuming[0].can_cancel is allowed
        assert page.resuming[0].model_dump().keys() == PUBLIC_FIELDS
        assert 'private diagnostics' not in page.model_dump_json()
        if allowed:
            cancelled = await jobs.request_job_cancellation(owner['id'], user)
            assert cancelled.state == 'cancelled'
        else:
            with pytest.raises(HTTPException) as error:
                await jobs.request_job_cancellation(owner['id'], user)
            assert error.value.status_code == 403
            assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', owner['id']) == 'queued'

    registry_database(scenario)


@pytest.mark.parametrize('transition', ['yield', 'retry', 'expired'])
def test_queued_lane_owner_is_visible_on_every_waiting_page(registry_database, monkeypatch, transition):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        owner = await job_queue.enqueue_job(database.pool, 'umap_fit', dataset_id=1, placement='compute')
        active = await claimed(
            database.pool,
            'compute',
            placements=['compute'],
            policies={'umap_fit': job_queue.JobPolicy(retry_seconds=60)},
        )
        if transition == 'retry':
            await job_queue.defer_job(database.pool, owner['id'], active['lease_token'], {}, delay_seconds=60)
        elif transition == 'expired':
            await database.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
                owner['id'],
            )
            assert await job_queue.claim_job(database.pool, 'reaper', kinds=['unrelated']) is None
        else:
            await job_queue.publish_step(database.pool, owner['id'], active['lease_token'], release=True)
        upkeep = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1, lane='maintenance')
        await claimed(database.pool, 'upkeep', kinds=['statistics'])
        followers = [await job_queue.enqueue_job(database.pool, 'bulk_tag', dataset_id=1) for number in range(19)]
        await job_queue.enqueue_job(database.pool, 'other_dataset', dataset_id=2)
        before = await database.fetch('SELECT * FROM jobs ORDER BY id')
        cursor = None
        seen: list[UUID] = []
        for number in range(3):
            page = await jobs.list_active_dataset_jobs(1, limit=7, cursor=cursor, user=USER)
            assert [item.id for item in page.running] == [upkeep['id']]
            assert [item.id for item in page.resuming] == [owner['id']]
            assert page.resuming[0].state == 'queued'
            assert page.resuming[0].retry_delayed is (transition != 'yield')
            assert page.resuming[0].model_dump().keys() == PUBLIC_FIELDS
            assert owner['id'] not in {item.id for item in page.waiting}
            assert len(page.waiting) <= 7
            seen.extend(item.id for item in page.waiting)
            cursor = page.next_cursor
        assert cursor is None
        assert seen == [follower['id'] for follower in followers]
        assert await database.fetch('SELECT * FROM jobs ORDER BY id') == before
        await job_queue.cancel_job(database.pool, owner['id'])
        page = await jobs.list_active_dataset_jobs(1, limit=7, cursor=None, user=USER)
        assert page.resuming == []
        assert (await claimed(database.pool, 'following'))['id'] == followers[0]['id']

    registry_database(scenario)


def test_owner_lookup_uses_its_partial_index_not_waiting_or_terminal_history(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute("""INSERT INTO jobs(id,kind,dataset_id,state)
            SELECT gen_random_uuid(),'history',1,'succeeded' FROM generate_series(1,100000)""")
        owner = await job_queue.enqueue_job(database.pool, 'owner', dataset_id=1)
        active = await claimed(database.pool, 'owner')
        await job_queue.defer_job(database.pool, owner['id'], active['lease_token'], {}, delay_seconds=300)
        await database.execute("""INSERT INTO jobs(id,kind,dataset_id)
            SELECT gen_random_uuid(),'waiting',1 FROM generate_series(1,2000)""")
        await database.execute('ANALYZE jobs; ANALYZE job_controls')
        query = AsyncMock(wraps=database.fetch)
        monkeypatch.setattr(database, 'fetch', query)
        page = await jobs.list_active_dataset_jobs(1, limit=25, cursor=None, user=USER)
        assert [item.id for item in page.resuming] == [owner['id']]
        assert len(page.waiting) == 25
        statement, *parameters = query.call_args.args
        plan = await database.fetchval('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + statement, *parameters)
        if isinstance(plan, str):
            plan = json.loads(plan)
        serialized = json.dumps(plan)
        assert 'idx_jobs_lane_owner' in serialized
        assert 'idx_jobs_queued_dataset' in serialized
        print('Owner and waiting page with 100k terminal rows: execution_ms=', plan[0]['Execution Time'])

    registry_database(scenario)
