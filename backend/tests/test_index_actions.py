"""Index advisor actions (backend/index_actions.py): remove and restore indexes as jobs, online, and
the representation of an intentionally removed core index."""

import asyncio
import logging
import os
from typing import Any, Dict

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import database as database_module
from backend import index_actions, index_advisor, job_queue, schema_check
from backend.job_wait import JobConflict
from backend.routers import datasets
from backend.routers import index_advisor as advisor_router
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.test_field_registry_postgres import ingest

registry_database = test_field_registry_postgres.registry_database
requires_database = pytest.mark.skipif(
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


async def _until(database, job_id, timeout=60):
    async def ended():
        while True:
            state = await database.fetchval('SELECT state FROM jobs WHERE id = $1', job_id)
            if state not in ('queued', 'running'):
                return state
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(ended(), timeout)


async def _removals(database, dataset_id=None):
    async with database.pool.acquire() as connection:
        return await index_actions.removals(connection, dataset_id)


async def _review(database, dataset_id=None):
    snapshot = await index_advisor.collect(database.pool, dataset_id)
    review, _ = index_advisor.build_review(snapshot, dataset_id)
    return review


@requires_database
def test_a_core_index_is_removed_online_recorded_and_restored(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(advisor_router, 'db', database)
        await ingest([{'country': 'US'}])
        definition = await _definition(database, GIN)
        job = await index_actions.submit_drop(database.pool, GIN, ADMIN, definition=definition, reason='overlap')
        assert job['dataset_id'] is None and job['lane'] == 'user' and job['kind'] == 'index_drop'
        assert await _until(database, job['id']) == 'succeeded'
        assert await _definition(database, GIN) is None
        record = await database.fetchrow('SELECT * FROM removed_indexes WHERE index_name = $1', GIN)
        assert record['definition'] == definition and record['kind'] == 'core' and record['reason'] == 'overlap'
        assert record['removed_by'] == ADMIN['id'] and record['removed_by_name'] == 'admin'
        assert record['dropped_at'] is not None and record['size_bytes'] > 0 and record['observed_since'] is not None
        assert [(item['index_name'], item['state']) for item in await _removals(database)] == [(GIN, 'removed')]
        # The state follows the catalog: a drop whose last publication was lost still reads as removed.
        await database.execute('UPDATE removed_indexes SET dropped_at = NULL')
        assert [item['state'] for item in await _removals(database)] == ['removed']
        # Nothing reports the intentional removal as damage: health checks never looked at indexes, and
        # the review's missing-core-index check honours the record.
        assert await schema_check.pending_migrations(database) == []
        assert await schema_check.stale_functions(database) == []
        review = await _review(database)
        assert review['missing_core'] == []
        assert GIN not in {pair['redundant']['name'] for pair in review['overlapping']}
        # One click restores it, online, exactly as it was.
        restored = await advisor_router.restore_index(GIN, wait=False, user=ADMIN)
        assert restored.status_code == 202
        restore_job = await database.fetchval("SELECT id FROM jobs WHERE kind = 'index_restore'")
        assert await _until(database, restore_job) == 'succeeded'
        assert await _definition(database, GIN) == definition
        assert await database.fetchval('SELECT restored_at IS NOT NULL FROM removed_indexes WHERE index_name = $1', GIN)
        assert await _removals(database) == []

    registry_database(job_helpers.with_runner(scenario))


@requires_database
def test_needed_changed_and_unknown_indexes_are_refused(registry_database):
    async def scenario(database):
        with pytest.raises(HTTPException) as needed:
            await index_actions.submit_drop(
                database.pool,
                'samples_pkey',
                ADMIN,
                definition=await _definition(database, 'samples_pkey'),
            )
        assert needed.value.status_code == 409 and 'primary key' in needed.value.detail
        with pytest.raises(HTTPException) as changed:
            await index_actions.submit_drop(database.pool, GIN, ADMIN, definition='CREATE INDEX something else')
        assert changed.value.status_code == 409 and 'changed since it was reviewed' in changed.value.detail
        with pytest.raises(HTTPException) as missing:
            await index_actions.submit_drop(database.pool, 'no_such_index', ADMIN, definition='x')
        assert missing.value.status_code == 404
        with pytest.raises(HTTPException) as never_removed:
            await index_actions.submit_restore(database.pool, GIN, ADMIN)
        assert never_removed.value.status_code == 404
        assert not await database.fetchval('SELECT count(*) FROM jobs')

    registry_database(scenario)


@requires_database
def test_a_drop_waits_out_a_backup_without_spending_failures(registry_database):
    async def scenario(database):
        definition = await _definition(database, GIN)
        await index_actions.submit_drop(database.pool, GIN, ADMIN, definition=definition)
        await job_queue.enqueue_job(database.pool, 'backup', lane='maintenance')
        handler = index_actions.drop_handler(database.pool)
        job = await job_queue.claim_job(database.pool, 'test', kinds=[index_actions.DROP_KIND])
        job = await job_helpers.step(database, job, handler)  # records the removal
        assert job['checkpoint']['phase'] == 'drop'
        with pytest.raises(job_queue.QueueContended, match='backup'):
            await handler(job)
        assert await _definition(database, GIN) == definition
        await database.execute("UPDATE jobs SET state = 'succeeded', finished_at = now() WHERE kind = 'backup'")
        job = await job_helpers.step(database, job, handler)
        assert job['state'] == 'succeeded' and job['failures'] == 0
        assert await _definition(database, GIN) is None

    registry_database(scenario)


@requires_database
def test_a_stopped_removal_or_a_recreated_index_is_closed_by_the_next_review(registry_database):
    async def scenario(database):
        definition = await _definition(database, GIN)
        await index_actions.submit_drop(database.pool, GIN, ADMIN, definition=definition)
        handler = index_actions.drop_handler(database.pool)
        job = await job_queue.claim_job(database.pool, 'test', kinds=[index_actions.DROP_KIND])
        job = await job_helpers.step(database, job, handler)  # recorded, then cancelled before the drop
        await job_queue.cancel_job(database.pool, job['id'])
        # The slot acknowledges the cancellation at its next boundary, as the runner does.
        stopped = await job_queue.publish_step(database.pool, job['id'], job['lease_token'], release=True)
        assert stopped['state'] == 'cancelled'
        assert [item['state'] for item in await _removals(database)] == ['stopped']
        review_job, _ = await index_advisor.submit(database.pool, None, ADMIN['id'])
        claimed = await job_queue.claim_job(database.pool, 'test', kinds=[index_advisor.KIND])
        finished = await job_helpers.step(database, claimed, index_advisor.handler(database.pool))
        assert finished['result']['recreated'][0]['note'].startswith('removal stopped')
        assert await _removals(database) == []
        # A completed removal whose index is re-created by hand (an old migration, an operator script).
        job = await index_actions.submit_drop(database.pool, GIN, ADMIN, definition=definition)
        claimed = await job_queue.claim_job(database.pool, 'test', kinds=[index_actions.DROP_KIND])
        claimed = await job_helpers.step(database, claimed, handler)
        assert (await job_helpers.step(database, claimed, handler))['state'] == 'succeeded'
        await database.execute(f'CREATE INDEX {GIN} ON samples USING gin (metadata jsonb_path_ops)')
        await index_advisor.submit(database.pool, None, ADMIN['id'])
        claimed = await job_queue.claim_job(database.pool, 'test', kinds=[index_advisor.KIND])
        finished = await job_helpers.step(database, claimed, index_advisor.handler(database.pool))
        assert finished['result']['recreated'] == [
            {
                'index_name': GIN,
                'removed_at': finished['result']['recreated'][0]['removed_at'],
                'note': 're-created outside the advisor',
            },
        ]
        assert await database.fetchval('SELECT closed_note FROM removed_indexes ORDER BY id DESC LIMIT 1') == (
            're-created outside the advisor'
        )

    registry_database(scenario)


@requires_database
def test_an_interrupted_drop_that_left_an_invalid_index_is_finished_by_remove_again(registry_database):
    async def scenario(database):
        definition = await _definition(database, GIN)
        await database.execute(f"UPDATE pg_index SET indisvalid = FALSE WHERE indexrelid = '{GIN}'::regclass")
        review = await _review(database)
        invalid = next(item for item in review['possibly_unused'] if item['name'] == GIN)
        assert invalid['invalid'] is True
        job = await index_actions.submit_drop(database.pool, GIN, ADMIN, definition=definition, reason='invalid')
        handler = index_actions.drop_handler(database.pool)
        claimed = await job_queue.claim_job(database.pool, 'test', kinds=[index_actions.DROP_KIND])
        claimed = await job_helpers.step(database, claimed, handler)
        assert (await job_helpers.step(database, claimed, handler))['state'] == 'succeeded'
        assert await _definition(database, GIN) is None and job is not None

    registry_database(scenario)


@requires_database
def test_a_field_index_removal_unpromotes_first_and_restores_through_the_field_build(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(advisor_router, 'db', database)
        await ingest([{'score': index} for index in range(3)])
        await datasets.promote_field(1, 'score', ADMIN)
        name = datasets._promoted_index_name(1, 'score')
        definition = await _definition(database, name)
        response = await advisor_router.remove_index(
            name,
            advisor_router.RemoveIndex(definition=definition, reason='unused'),
            wait=True,
            user=ADMIN,
        )
        assert response == {'index': name, 'removed': True, 'seconds': response['seconds']}
        assert await database.fetchval("SELECT is_promoted FROM dataset_fields WHERE field_name = 'score'") is False
        removals = await _removals(database, 1)
        assert [(item['index_name'], item['kind'], item['field'], item['state']) for item in removals] == [
            (name, 'field', 'score', 'removed'),
        ]
        assert await _removals(database) == []  # a dataset's field index is not a system removal
        restored = await advisor_router.restore_index(name, wait=True, user=ADMIN)
        assert restored == {'message': 'Field score promoted successfully'}
        assert await database.fetchval("SELECT is_promoted FROM dataset_fields WHERE field_name = 'score'") is True
        assert await _removals(database, 1) == []

    registry_database(job_helpers.with_runner(scenario))


async def _hold_access_share(database):
    """A long reader of samples, as pg_dump is: ACCESS SHARE held for the whole transaction."""
    holder = await database.pool.acquire()
    transaction = holder.transaction()
    await transaction.start()
    await holder.fetchval('SELECT count(*) FROM samples')
    return holder, transaction


@requires_database
def test_the_core_index_drop_never_blocks_ingest_behind_a_long_reader(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US'}])
        holder, transaction = await _hold_access_share(database)
        try:
            job = await index_actions.submit_drop(
                database.pool,
                GIN,
                ADMIN,
                definition=await _definition(database, GIN),
            )
            await asyncio.sleep(1.5)  # the drop is waiting for the reader now
            await asyncio.wait_for(ingest([{'country': 'CA'}], prefix='during'), 5)
            assert await database.fetchval('SELECT state FROM jobs WHERE id = $1', job['id']) in ('queued', 'running')
        finally:
            await transaction.rollback()
            await database.pool.release(holder)
        assert await _until(database, job['id']) == 'succeeded'
        assert await _definition(database, GIN) is None

    registry_database(job_helpers.with_runner(scenario))


@requires_database
def test_adding_a_field_index_never_blocks_ingest_behind_a_long_reader(registry_database):
    """The add-index path (promote) already builds CONCURRENTLY: a guard, not a change."""

    async def scenario(database):
        await ingest([{'score': 1}])
        holder, transaction = await _hold_access_share(database)
        try:
            promotion = asyncio.create_task(datasets.promote_field(1, 'score', ADMIN))
            await asyncio.sleep(1.5)  # the build is waiting for the reader now
            await asyncio.wait_for(ingest([{'score': 2}], prefix='during'), 5)
            assert not promotion.done() or getattr(await promotion, 'status_code', None) == 202
        finally:
            await transaction.rollback()
            await database.pool.release(holder)
        await asyncio.wait_for(promotion, 30)
        job = await database.fetchval("SELECT id FROM jobs WHERE kind = 'field_index_build'")
        assert await _until(database, job) == 'succeeded'

    registry_database(job_helpers.with_runner(scenario))


def test_the_startup_ann_warning_skips_an_index_removed_on_purpose(monkeypatch, caplog):
    class FakeDB:
        def __init__(self, removed):
            self.removed = removed

        async def fetch(self, query, *args):
            if 'FROM removed_indexes' in query:
                return [{'field_name': name} for name in self.removed]
            if 'pg_get_indexdef' in query:
                return []
            return [{'field_name': 'clip'}]

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(database_module, 'db', FakeDB(['clip']))
    asyncio.run(database_module._warn_missing_ann_indexes())
    assert "field 'clip'" not in caplog.text
    monkeypatch.setattr(database_module, 'db', FakeDB([]))
    asyncio.run(database_module._warn_missing_ann_indexes())
    assert "field 'clip'" in caplog.text


@pytest.mark.parametrize('role', ['power_user', 'viewer'])
def test_only_administrators_can_remove_or_restore(role):
    from backend.main import app
    from backend.routers.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {'id': 5, 'username': 'u', 'role': role}
    try:
        client = TestClient(app)
        assert client.post(f'/api/admin/indexes/{GIN}/remove', json={'definition': 'x'}).status_code == 403
        assert client.post(f'/api/admin/indexes/{GIN}/restore').status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@requires_database
def test_two_removals_can_never_leave_a_foreign_key_without_an_index(registry_database):
    """Each removal re-checks, under its table's lock and right before dropping, that the index is not
    needed now; removals on one table are serialised, so the second sees the first."""

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
            claimed = await job_queue.claim_job(database.pool, 'test', kinds=[index_actions.DROP_KIND])
            jobs.append(await job_helpers.step(database, claimed, handler))  # both removals recorded
        assert (await job_helpers.step(database, jobs[0], handler))['state'] == 'succeeded'
        with pytest.raises(JobConflict, match="must stay now"):
            await handler(jobs[1])
        assert await _definition(database, 'advisor_child_b') is not None
        assert await _definition(database, 'advisor_child_a') is None

    registry_database(scenario)


@requires_database
def test_an_orphan_field_index_is_removed_like_any_other_index(registry_database):
    async def scenario(database):
        name = 'idx_samples_meta_99_gone'
        await database.execute(f"CREATE INDEX {name} ON samples ((metadata->>'gone')) WHERE dataset_id = 99")
        job = await index_actions.submit_drop(database.pool, name, ADMIN, definition=await _definition(database, name))
        assert job['dataset_id'] is None and job['args']['kind'] == 'other'
        handler = index_actions.drop_handler(database.pool)
        claimed = await job_queue.claim_job(database.pool, 'test', kinds=[index_actions.DROP_KIND])
        claimed = await job_helpers.step(database, claimed, handler)
        assert (await job_helpers.step(database, claimed, handler))['state'] == 'succeeded'
        assert await _definition(database, name) is None
        assert [(item['index_name'], item['state']) for item in await _removals(database)] == [(name, 'removed')]

    registry_database(scenario)
