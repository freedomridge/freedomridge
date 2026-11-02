"""Claims remain responsive with dataset-scoped history and a blocked backlog."""

import os
import time

import pytest

from backend import job_queue
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_job_queue_postgres import claimed

registry_database = registry_helpers.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


@pytest.mark.parametrize('history_count,backlog_count,budget', [(10000, 1000, 0.2), (20000, 5000, 1.0)])
@pytest.mark.parametrize('owner_state', ['running', 'yielded'])
def test_dataset_history_and_blocked_backlog_keep_claims_responsive(
    registry_database,
    history_count,
    backlog_count,
    budget,
    owner_state,
):
    async def scenario(database):
        await database.execute(
            """INSERT INTO jobs(id,kind,state,started_at,finished_at,dataset_id)
               SELECT md5('history'||number)::uuid,'history','succeeded',clock_timestamp(),clock_timestamp(),
                      1+(number%2)
               FROM generate_series(1,$1::integer) number""",
            history_count,
        )
        await job_queue.enqueue_job(database.pool, 'owner', dataset_id=1)
        owner = await claimed(database.pool, 'owner', kinds=['owner'])
        if owner_state == 'yielded':
            await job_queue.publish_step(database.pool, owner['id'], owner['lease_token'], release=True)
        await database.execute(
            """INSERT INTO jobs(id,kind,dataset_id)
               SELECT md5('waiting'||number)::uuid,'waiting',1 FROM generate_series(1,$1::integer) number""",
            backlog_count,
        )
        await database.execute('ANALYZE jobs; ANALYZE job_controls')
        ready = await job_queue.enqueue_job(database.pool, 'waiting', dataset_id=2)
        started = time.monotonic()
        assert (await claimed(database.pool, 'free', kinds=['waiting']))['id'] == ready['id']
        assert await job_queue.claim_job(database.pool, 'idle', kinds=['waiting']) is None
        elapsed = time.monotonic() - started
        assert elapsed < budget, f'two claims took {elapsed:.3f} s with {history_count} past / {backlog_count} waiting'
        if owner_state == 'yielded':
            assert (await claimed(database.pool, 'resume', kinds=['owner']))['id'] == owner['id']

    registry_database(scenario)
