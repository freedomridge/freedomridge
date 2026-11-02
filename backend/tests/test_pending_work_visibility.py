"""Restored writer IDs and live transactions share the same bounded queue readers."""

from pathlib import Path

import pytest

from backend import concept_jobs, job_queue, statistics
from backend.tests import test_field_registry_postgres as helpers
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_concept_jobs import assert_count
from backend.tests.test_concept_jobs import finish as finish_concept
from backend.tests.test_concept_maintenance import concept, sample, score, scores
from backend.tests.test_duplicate_jobs import bootstrap, drain, hidden
from backend.tests.test_duplicate_jobs import sample as duplicate_sample

registry_database = helpers.registry_database
pytestmark = helpers.pytestmark
DB = Path(statistics.__file__).parent / 'db'
QUEUES = (
    'dataset_stats_pending',
    'concept_embedding_pending',
    'concept_score_pending',
    'concept_score_count_deltas',
    'duplicate_pending',
)


async def simulate_restore(database):
    await database.execute('UPDATE transaction_high_water SET transaction_id=txid_current()+100000')
    assert await job_queue.check_transaction_high_water(database.pool)


async def prepare(database, queue):
    if queue == 'dataset_stats_pending':
        await run_refresh(database, 1)
        await database.execute("INSERT INTO samples(dataset_id,filepath,media_type) VALUES(1,'new','image')")
        return None
    if queue == 'duplicate_pending':
        await bootstrap(database, ('a',))
        async with database.pool.acquire() as connection, connection.transaction():
            await duplicate_sample(connection, 1)
            await duplicate_sample(connection, 2)
        return None
    concept_id = await concept(database)
    await finish_concept(database, concept_id)
    async with database.pool.acquire() as connection, connection.transaction():
        sample_id = await sample(connection, 'new')
        if queue == 'concept_score_count_deltas':
            await connection.execute(
                'INSERT INTO embedding_concept_scores(concept_id,sample_id,score) VALUES($1,$2,1)',
                concept_id,
                sample_id,
            )
            await connection.execute('DELETE FROM concept_embedding_pending')
            # Bootstrap recount must acknowledge restored deltas included in the
            # measured total, or settling them afterwards double-counts the scores.
            await connection.execute(
                'UPDATE embedding_concepts SET score_initialized=FALSE WHERE id=$1',
                concept_id,
            )
    if queue == 'concept_score_pending':
        await concept_jobs.dispatch_changes(database.pool)
    return concept_id


async def consume(database, queue, concept_id):
    if queue == 'dataset_stats_pending':
        await run_refresh(database, 1)
        assert await database.fetchval('SELECT sample_count FROM dataset_stats_parts') == 1
    elif queue == 'duplicate_pending':
        await drain(database)
        assert await hidden(database, 'a') == {2}
    else:
        # Exercise the snapshot-filtered SQL dispatcher as well as the background reader.
        snapshot = await database.fetchval('SELECT txid_current_snapshot()::text')
        await database.fetchval('SELECT dispatch_concept_score_work(256,1,NULL,$1)', snapshot)
        job = None
        if queue == 'concept_score_count_deltas':
            job = await concept_jobs.request_scoring(database.pool, concept_id)
            # Resume at the recount checkpoint with a delta committed before the dump.
            await database.execute(
                """UPDATE jobs SET checkpoint='{"count_phase":true}' WHERE id=$1""",
                job['id'],
            )
        await finish_concept(database, concept_id, job)
        assert len(await scores(database, concept_id)) == 1
        await assert_count(database, concept_id, 1)
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 0
        assert await database.fetchval('SELECT count(*) FROM concept_score_count_deltas') == 0
    assert await database.fetchval(f'SELECT count(*) FROM {queue}') == 0


@pytest.mark.parametrize('queue', QUEUES)
@pytest.mark.parametrize('epoch', [0, 1])
def test_restored_future_work_is_processed(registry_database, queue, epoch):
    async def scenario(database):
        concept_id = await prepare(database, queue)
        future = await database.fetchval('SELECT txid_current()+100000+$1::bigint*4294967296', epoch)
        assert await database.fetchval(f'SELECT count(*) FROM {queue}') > 0
        await database.execute(f'UPDATE {queue} SET transaction_id=$1', future)
        await database.execute('UPDATE transaction_high_water SET transaction_id=$1', future)
        assert await job_queue.check_transaction_high_water(database.pool)
        await consume(database, queue, concept_id)

    registry_database(scenario)


@pytest.mark.parametrize('queue', ['concept_embedding_pending', 'concept_score_pending'])
@pytest.mark.parametrize('full', [False, True])
def test_synchronous_scoring_consumes_restored_work(registry_database, queue, full):
    async def scenario(database):
        concept_id = await prepare(database, queue)
        await database.execute(f'UPDATE {queue} SET transaction_id=txid_current()+100000')
        await simulate_restore(database)
        await score(database, concept_id, full=full)
        await assert_count(database, concept_id, 1)
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') == 0

    registry_database(scenario)


def test_restored_source_work_is_newer_than_a_previous_manual_rescore(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await score(database, concept_id, full=True)
        await sample(database, 'after-manual-rescore')
        await database.execute('UPDATE concept_embedding_pending SET transaction_id=txid_current()+100000')
        await simulate_restore(database)
        await score(database, concept_id)
        await assert_count(database, concept_id, 1)

    registry_database(scenario)


def test_full_statistics_refresh_acknowledges_restored_full_request(registry_database):
    async def scenario(database):
        await prepare(database, 'dataset_stats_pending')
        await database.execute(
            'INSERT INTO dataset_stats_pending(dataset_id,block_id,transaction_id) VALUES(1,-1,txid_current()+100000)',
        )
        await simulate_restore(database)
        await run_refresh(database, 1)
        assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending') == 0

    registry_database(scenario)


@pytest.mark.parametrize('queue', QUEUES)
def test_uncommitted_and_post_boundary_work_is_not_acknowledged(registry_database, queue):
    async def scenario(database):
        concept_id = await prepare(database, queue)
        await database.execute(f'DELETE FROM {queue}')
        snapshot = await database.fetchval('SELECT txid_current_snapshot()::text')
        async with database.pool.acquire() as writer, writer.transaction():
            xid = await writer.fetchval('SELECT txid_current()')
            if queue == 'concept_embedding_pending':
                await writer.execute(
                    "INSERT INTO concept_embedding_pending(dataset_id,field_name,block_id) VALUES(1,'clip',0)",
                )
            elif queue == 'concept_score_count_deltas':
                await writer.execute(
                    'INSERT INTO concept_score_count_deltas(concept_id,delta) VALUES($1,1)',
                    concept_id,
                )
            elif queue == 'concept_score_pending':
                await writer.execute(
                    'INSERT INTO concept_score_pending(concept_id,block_id) VALUES($1,0)',
                    concept_id,
                )
            else:
                await writer.execute(f'INSERT INTO {queue}(dataset_id,block_id) VALUES(1,0)')
            assert await database.fetchval('SELECT txid_snapshot_xmax(txid_current_snapshot())') <= xid
            query = f"""DELETE FROM {queue} WHERE
                        txid_visible_in_snapshot(transaction_id,$1::text::txid_snapshot)"""
            # Even when xid >= current xmax, MVCC hides the active writer's row.
            assert await database.execute(query, snapshot) == 'DELETE 0'
        # The row now exists, but is newer than the saved measurement boundary.
        assert await database.fetchval(f'SELECT transaction_id FROM {queue}') == xid
        assert await database.execute(query, snapshot) == 'DELETE 0'
        latest = await database.fetchval('SELECT txid_current_snapshot()::text')
        assert await database.execute(query, latest) == 'DELETE 1'

    registry_database(scenario)
