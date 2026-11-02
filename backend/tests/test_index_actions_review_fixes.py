"""Index advisor removal and restore regressions, one test each. Each fails
on the code before these fixes or kills a mutation that survived there."""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Dict
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import HTTPException

from backend import diskspace, index_actions, index_advisor, index_jobs, job_queue
from backend.database import init_connection
from backend.job_wait import JobConflict, JobInsufficientStorage
from backend.routers import datasets
from backend.routers import index_advisor as advisor_router
from backend.routers import jobs as jobs_router
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.test_field_registry_postgres import ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL test database',
)
ADMIN: Dict[str, Any] = {'id': 1, 'role': 'admin', 'username': 'admin'}
GIN = 'idx_samples_metadata_gin'


async def _definition(database, name):
    return await database.fetchval(
        'SELECT pg_get_indexdef(c.oid) FROM pg_class c '
        'WHERE c.relname = $1 AND c.relnamespace = current_schema()::regnamespace',
        name,
    )


@asynccontextmanager
async def _holding(database, sql='SELECT count(*) FROM samples'):
    """A long transaction holding locks on samples (a pg_dump-like reader by default), always released:
    a test that fails while it is held must not leave the pool waiting for it at teardown."""
    holder = await database.pool.acquire()
    transaction = holder.transaction()
    await transaction.start()
    try:
        await holder.execute(sql)
        yield holder
    finally:
        await transaction.rollback()
        await database.pool.release(holder)


async def _finish(task, seconds=60):
    """Await a background unit; cancel it if it outlives `seconds` (a test never hangs on it)."""
    try:
        return await asyncio.wait_for(task, seconds)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _claim(database, kind):
    return await job_queue.claim_job(database.pool, 'test', kinds=[kind], lease_seconds=600)


async def _removed(database):
    """Remove the core GIN through the job (both units), as an administrator would."""
    await index_actions.submit_drop(database.pool, GIN, ADMIN, definition=await _definition(database, GIN))
    handler = index_actions.drop_handler(database.pool)
    job = await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), handler)
    assert (await job_helpers.step(database, job, handler))['state'] == 'succeeded'


async def _promote(database, field):
    await datasets.promote_field(1, field, ADMIN, wait=False)
    assert (await job_helpers.finish(database, index_jobs.FIELD_KIND))['state'] == 'succeeded'


# --- the DROP waits for long readers -----------------------------------------------------------


def test_the_drop_waits_out_a_reader_older_than_the_pools_command_timeout(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US'}])
        await index_actions.submit_drop(database.pool, GIN, ADMIN, definition=await _definition(database, GIN))
        schema = await database.fetchval('SELECT current_schema()')
        pool = await asyncpg.create_pool(  # like the runner's pool (command_timeout=300), scaled down
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            min_size=1,
            max_size=6,
            command_timeout=2,
            init=init_connection,
            server_settings={'search_path': f'{schema},public'},
        )
        try:
            handler = index_actions.drop_handler(pool)
            job = await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), handler)
            async with _holding(database):
                dropping = asyncio.create_task(handler(job))
                await asyncio.sleep(4)  # twice the command timeout: the old drop was cancelled here
                cut_off = dropping.done()
            unit = await _finish(dropping, 30)
            assert not cut_off
            assert unit.finished and unit.result['removed'] is True
            assert await _definition(database, GIN) is None
        finally:
            await pool.close()

    registry_database(scenario)


# --- one non-waiting lock per table for every concurrent index build or drop -------------------------


def test_a_field_build_during_a_restore_defers_instead_of_deadlocking_it(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US', 'score': index} for index in range(50)])
        await _removed(database)
        await index_actions.submit_restore(database.pool, GIN, ADMIN)
        restore = index_actions.restore_handler(database.pool)
        claimed = await _claim(database, index_actions.RESTORE_KIND)
        async with _holding(database, 'LOCK TABLE samples IN ROW EXCLUSIVE MODE'):  # ingest: holds the CIC
            restoring = asyncio.create_task(restore(claimed))
            await asyncio.sleep(1)
            await datasets.promote_field(1, 'score', ADMIN, wait=False)  # "Index this field" during the restore
            building = asyncio.create_task(
                index_jobs.field_handler(database.pool)(await _claim(database, index_jobs.FIELD_KIND)),
            )
            await asyncio.sleep(1)
        outcomes = await asyncio.gather(_finish(restoring), _finish(building), return_exceptions=True)
        unit = outcomes[0]
        assert not isinstance(unit, BaseException), unit  # was DeadlockDetectedError after the full build
        assert unit.finished and unit.result['restored'] is True
        assert isinstance(outcomes[1], job_queue.QueueContended)  # deferred, never waited holding a snapshot

    registry_database(scenario)


def test_a_demote_during_a_restore_defers_instead_of_deadlocking_it(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US', 'score': index} for index in range(50)])
        await _promote(database, 'score')
        await _removed(database)
        await index_actions.submit_restore(database.pool, GIN, ADMIN)
        restore = index_actions.restore_handler(database.pool)
        claimed = await _claim(database, index_actions.RESTORE_KIND)
        name = datasets._promoted_index_name(1, 'score')
        drop = index_actions.drop_handler(database.pool)
        async with _holding(database, 'LOCK TABLE samples IN ROW EXCLUSIVE MODE'):
            restoring = asyncio.create_task(restore(claimed))
            await asyncio.sleep(1)
            await index_actions.submit_drop(database.pool, name, ADMIN, record=False, field_name='score', dataset_id=1)
            demote = await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), drop)
            demoting = asyncio.create_task(drop(demote))
            await asyncio.sleep(1)
        outcomes = await asyncio.gather(_finish(restoring), _finish(demoting), return_exceptions=True)
        unit = outcomes[0]
        assert not isinstance(unit, BaseException), unit  # was DeadlockDetectedError
        assert unit.finished and unit.result['restored'] is True
        assert isinstance(outcomes[1], job_queue.QueueContended)

    registry_database(scenario)


def test_synchronous_index_ddl_answers_409_while_the_table_is_busy(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}])
        await _promote(database, 'score')
        holder = await database.pool.acquire()  # stands in for a restore holding samples' index lock
        try:
            assert await holder.fetchval("SELECT pg_try_advisory_lock(20261067, hashtext('samples'))")
            with pytest.raises(HTTPException) as cleared:
                await datasets.clear_field(1, 'score', ADMIN)
            assert cleared.value.status_code == 409 and 'samples' in cleared.value.detail
            with pytest.raises(HTTPException) as created:
                await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='labels9'), ADMIN)
            assert created.value.status_code == 409
        finally:
            await holder.execute('SELECT pg_advisory_unlock_all()')
            await database.pool.release(holder)
        assert await database.fetchval("SELECT metadata->>'score' FROM samples") == '1'  # nothing was cleared

    registry_database(scenario)


# --- vector and array-field indexes are not removed by the advisor ---------------------------------


def test_vector_and_array_field_indexes_are_refused(registry_database):
    async def scenario(database):
        await database.execute(
            """CREATE INDEX hnsw_emb_clip ON embeddings USING hnsw ((vector::vector(3)) vector_cosine_ops)
                   WHERE field_name = 'clip';
               CREATE INDEX my_ivf ON embeddings USING ivfflat ((vector::vector(3)) vector_l2_ops);
               CREATE INDEX idx_samples_labels_gin ON samples USING gin (tags);""",
        )
        for name, words in (
            ('hnsw_emb_clip', 'embedding index controls'),
            ('my_ivf', 'embedding index controls'),
            ('idx_samples_labels_gin', 'array field'),
        ):
            with pytest.raises(HTTPException, match=words) as refused:
                await index_actions.submit_drop(
                    database.pool,
                    name,
                    ADMIN,
                    definition=await _definition(database, name),
                )
            assert refused.value.status_code == 409
        assert not await database.fetchval('SELECT count(*) FROM jobs')

    registry_database(scenario)


# --- a restore needs room for the build's temp files and WAL ----------------------------------------


def test_a_restore_checks_room_for_the_index_its_temp_sort_and_its_wal(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'country': 'US'}])
        await _removed(database)
        size = await database.fetchval('SELECT size_bytes FROM removed_indexes')
        need = index_actions.restore_bytes(size, await database.fetchval('SELECT definition FROM removed_indexes'))
        assert need == int(size * 1.89)
        # 1.5x the index plus the headroom: the old check accepted it (index + headroom only).
        monkeypatch.setattr(
            diskspace,
            'db_free_bytes',
            AsyncMock(return_value=int(size * 1.5) + diskspace.HEADROOM_BYTES),
        )
        with pytest.raises(HTTPException) as refused:
            await index_actions.submit_restore(database.pool, GIN, ADMIN)
        assert refused.value.status_code == 507
        monkeypatch.setattr(diskspace, 'db_free_bytes', AsyncMock(return_value=need + diskspace.HEADROOM_BYTES))
        job = await index_actions.submit_restore(database.pool, GIN, ADMIN)
        # Short of room by the time the job runs: the job answers 507 too.
        monkeypatch.setattr(diskspace, 'db_free_bytes', AsyncMock(return_value=0))
        with pytest.raises(JobInsufficientStorage):
            await index_actions.restore_handler(database.pool)(await _claim(database, index_actions.RESTORE_KIND))
        assert job is not None

    registry_database(scenario)


# --- a field restore shows as restoring ------------------------------------------------------------


def test_a_field_index_restore_shows_restoring_until_its_build_ends(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(advisor_router, 'db', database)
        await ingest([{'score': index} for index in range(3)])
        await _promote(database, 'score')
        name = datasets._promoted_index_name(1, 'score')
        await index_actions.submit_drop(database.pool, name, ADMIN, definition=await _definition(database, name))
        handler = index_actions.drop_handler(database.pool)
        job = await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), handler)
        assert (await job_helpers.step(database, job, handler))['state'] == 'succeeded'
        response = await advisor_router.restore_index(name, wait=False, user=ADMIN)
        assert response.status_code == 202
        async with database.pool.acquire() as connection:
            assert [item['state'] for item in await index_actions.removals(connection, 1)] == ['restoring']

    registry_database(scenario)


# --- tests for the surviving mutations -------------------------------------------------------------


def test_removals_on_one_table_are_serialised(registry_database):
    async def scenario(database):
        await database.execute(
            """CREATE TABLE advisor_child (id serial PRIMARY KEY, dataset_id int REFERENCES datasets(id), x int);
               CREATE INDEX advisor_child_a ON advisor_child (dataset_id);
               CREATE INDEX advisor_child_b ON advisor_child (dataset_id, x);""",
        )
        handler = index_actions.drop_handler(database.pool)
        jobs = []
        for name in ('advisor_child_a', 'advisor_child_b'):
            await index_actions.submit_drop(database.pool, name, ADMIN, definition=await _definition(database, name))
            jobs.append(await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), handler))
        async with _holding(database, 'SELECT count(*) FROM advisor_child'):
            first = asyncio.create_task(handler(jobs[0]))  # waits for the reader, holding the table's lock
            await asyncio.sleep(1)
            second = asyncio.create_task(handler(jobs[1]))  # concurrently: deferred, never both waiting
            await asyncio.sleep(1)
        outcomes = await asyncio.gather(_finish(first, 30), _finish(second, 30), return_exceptions=True)
        assert not isinstance(outcomes[0], BaseException) and outcomes[0].finished, outcomes[0]
        assert isinstance(outcomes[1], job_queue.QueueContended) and 'advisor_child' in str(outcomes[1])
        assert await _definition(database, 'advisor_child_b') is not None

    registry_database(scenario)


def test_a_field_indexed_again_between_the_units_keeps_its_index(registry_database):
    async def scenario(database):
        await ingest([{'score': index} for index in range(3)])
        await _promote(database, 'score')
        name = datasets._promoted_index_name(1, 'score')
        await index_actions.submit_drop(database.pool, name, ADMIN, definition=await _definition(database, name))
        handler = index_actions.drop_handler(database.pool)
        job = await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), handler)
        assert await database.fetchval("SELECT is_promoted FROM dataset_fields WHERE field_name = 'score'") is False
        await database.execute("UPDATE dataset_fields SET is_promoted = TRUE WHERE field_name = 'score'")  # promote
        finished = await job_helpers.step(database, job, handler)
        assert finished['state'] == 'succeeded' and finished['result']['removed'] is False
        assert await _definition(database, name) is not None
        assert await database.fetchval('SELECT closed_note FROM removed_indexes') == 'indexed again'

    registry_database(scenario)


def test_the_field_is_unpromoted_before_its_index_is_dropped(registry_database):
    async def scenario(database):
        await ingest([{'score': index} for index in range(3)])
        await _promote(database, 'score')
        name = datasets._promoted_index_name(1, 'score')
        await index_actions.submit_drop(database.pool, name, ADMIN, record=False, field_name='score', dataset_id=1)
        handler = index_actions.drop_handler(database.pool)
        job = await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), handler)
        async with _holding(database):
            dropping = asyncio.create_task(handler(job))
            await asyncio.sleep(1)  # the DROP is waiting for the reader
            promoted = await database.fetchval("SELECT is_promoted FROM dataset_fields WHERE field_name = 'score'")
            present = await _definition(database, name) is not None
        assert (await _finish(dropping, 30)).finished
        assert promoted is False and present

    registry_database(scenario)


def test_a_restore_verifies_the_rebuilt_definition(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'country': 'US'}])
        await _removed(database)
        await index_actions.submit_restore(database.pool, GIN, ADMIN)

        async def someone_elses_index(connection, name, statement, seconds):
            await connection.execute(f'CREATE INDEX {name} ON samples (id)')  # the same name, raced in

        monkeypatch.setattr(index_jobs, '_build', someone_elses_index)
        with pytest.raises(JobConflict, match='rebuilt with another definition'):
            await index_actions.restore_handler(database.pool)(await _claim(database, index_actions.RESTORE_KIND))

    registry_database(scenario)


# --- the array field's ALTER never queues behind a long reader ---------------------------------------


def test_creating_an_array_column_behind_a_long_reader_answers_409(registry_database):
    async def scenario(database):
        async with _holding(database):
            with pytest.raises(HTTPException) as busy:
                await asyncio.wait_for(
                    datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='labels8'), ADMIN),
                    8,  # was: ACCESS EXCLUSIVE queued until the reader ended (ingest queued behind it)
                )
            assert busy.value.status_code == 409

    registry_database(scenario)


# --- a removal whose field is gone is closed ---------------------------------------------------------


def test_a_removal_whose_field_was_cleared_is_closed_by_the_review(registry_database):
    async def scenario(database):
        await ingest([{'score': index} for index in range(3)])
        await _promote(database, 'score')
        name = datasets._promoted_index_name(1, 'score')
        await index_actions.submit_drop(database.pool, name, ADMIN, definition=await _definition(database, name))
        handler = index_actions.drop_handler(database.pool)
        job = await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), handler)
        assert (await job_helpers.step(database, job, handler))['state'] == 'succeeded'
        await datasets.clear_field(1, 'score', ADMIN)
        await index_advisor.submit(database.pool, 1, ADMIN['id'])
        review = await job_helpers.step(
            database,
            await _claim(database, index_advisor.KIND),
            index_advisor.handler(database.pool),
        )
        assert review['result']['recreated'][0]['note'].startswith('its field was renamed, cleared or removed')
        assert await database.fetchval('SELECT closed_at IS NOT NULL FROM removed_indexes')

    registry_database(scenario)


# --- edges ----------------------------------------------------------------------------------------


def test_two_concurrent_clicks_queue_one_removal(registry_database):
    async def scenario(database):
        definition = await _definition(database, GIN)
        for _ in range(5):
            outcomes = await asyncio.gather(
                *(index_actions.submit_drop(database.pool, GIN, ADMIN, definition=definition) for _ in range(4)),
                return_exceptions=True,
            )
            jobs = {outcome['id'] for outcome in outcomes if isinstance(outcome, dict)}
            # Every click joins the one removal, or is told to retry (the queue's 100 ms lock wait).
            assert len(jobs) == 1
            assert all(isinstance(outcome, (dict, job_queue.QueueBusy)) for outcome in outcomes)
        assert await database.fetchval("SELECT count(*) FROM jobs WHERE kind = 'index_drop'") == 1

    registry_database(scenario)


def test_a_demote_never_joins_an_advisor_removal(registry_database):
    async def scenario(database):
        await ingest([{'score': index} for index in range(3)])
        await _promote(database, 'score')
        name = datasets._promoted_index_name(1, 'score')
        await index_actions.submit_drop(database.pool, name, ADMIN, definition=await _definition(database, name))
        with pytest.raises(HTTPException, match='already being removed'):
            await index_actions.submit_drop(database.pool, name, ADMIN, record=False, field_name='score', dataset_id=1)

    registry_database(scenario)


def test_a_dataset_review_leaves_system_records_to_the_system_review(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US'}])
        await _removed(database)
        await database.execute(f'CREATE INDEX {GIN} ON samples USING gin (metadata jsonb_path_ops)')  # by hand
        await index_advisor.submit(database.pool, 1, ADMIN['id'])
        await job_helpers.step(
            database,
            await _claim(database, index_advisor.KIND),
            index_advisor.handler(database.pool),
        )
        assert await database.fetchval('SELECT closed_at FROM removed_indexes') is None
        await index_advisor.submit(database.pool, None, ADMIN['id'])
        review = await job_helpers.step(
            database,
            await _claim(database, index_advisor.KIND),
            index_advisor.handler(database.pool),
        )
        assert [item['index_name'] for item in review['result']['recreated']] == [GIN]
        assert await database.fetchval('SELECT closed_note FROM removed_indexes') == 're-created outside the advisor'

    registry_database(scenario)


def test_observations_of_another_database_do_not_break_a_removal(registry_database):
    async def scenario(database):
        oid = await database.fetchval(f"SELECT '{GIN}'::regclass::oid")
        await database.execute(
            """INSERT INTO index_observations (index_oid, system_identifier, database_oid, index_name, first_seen_at)
               SELECT $1, system_identifier, 1, 'restored elsewhere', now() FROM pg_control_system()
               ON CONFLICT DO NOTHING""",
            oid,
        )
        await _removed(database)  # was: "more than one row returned by a subquery"

    registry_database(scenario)


# --- Found by the e2e: a queued removal is visible before its record exists ------------------------------
# A removal is recorded by its first unit. The page polled only while a review or a recorded removal ran, so
# when its one fetch after the click came before that unit, it never fetched again and kept offering Remove.


def test_a_queued_removal_is_reported_until_its_job_ends(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(advisor_router, 'db', database)
        monkeypatch.setattr(advisor_router, 'check_dataset_access', AsyncMock(return_value={'name': 'one'}))
        await ingest([{'score': index} for index in range(3)])
        await _promote(database, 'score')
        field_index = datasets._promoted_index_name(1, 'score')
        await index_actions.submit_drop(database.pool, GIN, ADMIN, definition=await _definition(database, GIN))
        await index_actions.submit_drop(
            database.pool,
            field_index,
            ADMIN,
            definition=await _definition(database, field_index),
        )
        system = await advisor_router.system_index_advisor(user=ADMIN)
        assert system['removed'] == [] and system['pending_actions'] == [GIN]  # queued, not yet recorded
        dataset = await advisor_router.dataset_index_advisor(1, user=ADMIN)
        assert dataset['pending_actions'] == [field_index]
        handler = index_actions.drop_handler(database.pool)
        job = await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), handler)
        assert job['args']['index_name'] == GIN
        recorded = await advisor_router.system_index_advisor(user=ADMIN)
        assert [item['state'] for item in recorded['removed']] == ['removing'] and recorded['pending_actions'] == [GIN]
        assert (await job_helpers.step(database, job, handler))['state'] == 'succeeded'
        assert (await advisor_router.system_index_advisor(user=ADMIN))['pending_actions'] == []

    registry_database(scenario)


# --- Further fixes ---------------------------------------------------------------------------------------
# a drop waiting for a forgotten transaction says which one, to administrators only.


def test_a_waiting_drop_names_its_blocker_to_administrators_only(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(index_actions, 'BLOCKER_SAMPLE_SECONDS', 0.2)
        await index_actions.submit_drop(database.pool, GIN, ADMIN, definition=await _definition(database, GIN))
        handler = index_actions.drop_handler(database.pool)
        job = await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), handler)

        async def reported():
            while True:
                message = await database.fetchval('SELECT message FROM jobs WHERE id = $1', job['id'])
                if message and 'waiting for' in message:
                    return message
                await asyncio.sleep(0.1)

        dropping = None
        try:
            async with _holding(
                database,
                "SET application_name = 'forgotten-psql'; SELECT count(*) FROM samples",
            ) as holder:
                pid = holder.get_server_pid()
                dropping = asyncio.create_task(handler(job))
                message = await asyncio.wait_for(reported(), 20)
                row = await database.fetchrow(
                    f'SELECT {jobs_router._PUBLIC_COLUMNS} FROM {job_queue.JOB_TABLE} WHERE jobs.id = $1',
                    job['id'],
                )
                shown_to_admin = jobs_router.job_status(row, ADMIN).progress.message
                shown_to_reader = jobs_router.job_status(row, {'id': 2, 'role': 'user'}).progress.message
        finally:
            unit = await _finish(dropping, 30) if dropping is not None else None
        assert message.startswith(
            f'Removing index {GIN} — waiting for pid {pid} (forgotten-psql, idle in transaction, ',
        )
        assert 'transaction open ' in message
        assert shown_to_admin == message
        assert shown_to_reader == f'Removing index {GIN} — waiting for older transactions on the table to finish'
        assert unit is not None and unit.finished and unit.result['removed'] is True

    registry_database(scenario)


# the restore's footprint depends on the access method.


def test_a_btree_restore_reserves_the_measured_btree_footprint(registry_database, monkeypatch):
    async def scenario(database):
        await database.execute('CREATE INDEX advisor_btree ON samples (filepath, id)')
        definition = await _definition(database, 'advisor_btree')
        assert index_actions.restore_factor(definition) == pytest.approx(2.57)
        assert index_actions.restore_factor(await _definition(database, GIN)) == pytest.approx(1.89)
        assert index_actions.restore_factor('CREATE INDEX x ON public.samples USING brin (id)') == pytest.approx(2.57)
        await index_actions.submit_drop(database.pool, 'advisor_btree', ADMIN, definition=definition)
        handler = index_actions.drop_handler(database.pool)
        job = await job_helpers.step(database, await _claim(database, index_actions.DROP_KIND), handler)
        assert (await job_helpers.step(database, job, handler))['state'] == 'succeeded'
        size = await database.fetchval('SELECT size_bytes FROM removed_indexes')
        # Room for a GIN-sized rebuild is not room for a btree one (its build sort spills to temp files).
        monkeypatch.setattr(
            diskspace,
            'db_free_bytes',
            AsyncMock(return_value=int(size * 1.89) + diskspace.HEADROOM_BYTES),
        )
        with pytest.raises(HTTPException) as refused:
            await index_actions.submit_restore(database.pool, 'advisor_btree', ADMIN)
        assert refused.value.status_code == 507
        monkeypatch.setattr(
            diskspace,
            'db_free_bytes',
            AsyncMock(return_value=int(size * 2.57) + diskspace.HEADROOM_BYTES),
        )
        assert await index_actions.submit_restore(database.pool, 'advisor_btree', ADMIN) is not None

    registry_database(scenario)


# the ANN build takes its table's lock too (it waits for the table lock without limit otherwise).


def test_an_ann_build_during_another_embeddings_build_defers_instead_of_deadlocking_it(registry_database):
    async def scenario(database):
        for dataset_id, field in ((1, 'clip'), (2, 'dino')):
            for index, value in enumerate(['[1,0]', '[0,1]', '[1,1]']):
                sample_id = await database.fetchval(
                    "INSERT INTO samples (dataset_id, filepath, media_type) VALUES ($1, $2, 'image') RETURNING id",
                    dataset_id,
                    f'{field}-{index}',
                )
                await database.execute(
                    'INSERT INTO embeddings (sample_id, field_name, vector) VALUES ($1, $2, $3::text::vector)',
                    sample_id,
                    field,
                    value,
                )
        build = index_jobs.ann_handler(database.pool)
        await index_jobs.submit_ann_index(database.pool, 1, ADMIN['id'], 'clip')
        first = await _claim(database, index_jobs.ANN_KIND)
        await index_jobs.submit_ann_index(database.pool, 2, ADMIN['id'], 'dino')
        second = await _claim(database, index_jobs.ANN_KIND)
        assert (first['args']['field'], second['args']['field']) == ('clip', 'dino')
        async with _holding(database, 'LOCK TABLE embeddings IN ROW EXCLUSIVE MODE'):  # ingest: holds the first build
            building = asyncio.create_task(build(first))
            await asyncio.sleep(1)
            other = asyncio.create_task(build(second))
            await asyncio.sleep(1)
        outcomes = await asyncio.gather(_finish(building), _finish(other), return_exceptions=True)
        unit = outcomes[0]
        assert not isinstance(unit, BaseException), unit  # without the ANN build's TABLE_LOCK: DeadlockDetectedError
        assert unit.finished and unit.result['built'] is True
        assert isinstance(outcomes[1], job_queue.QueueContended) and 'embeddings' in str(outcomes[1])

    registry_database(scenario)
