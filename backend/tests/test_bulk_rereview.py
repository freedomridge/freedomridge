"""Bulk job regressions: view scope, discarding stopped jobs, staged targets and unit costs."""

import os

import asyncpg
import pytest
from fastapi import HTTPException

from backend import access, bulk_jobs, job_queue, job_service
from backend.routers import datasets, versions
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.test_bulk_jobs import claim, finish, step, submitted
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

CAR = {'field': 'label', 'operator': '=', 'value': 'car'}
CROSS_GRAIN = {
    'logic': 'or',
    'filters': [
        {'field': '_cgself', 'operator': 'cg_self', 'value': CAR},
        {'field': '_cg', 'operator': 'has_matching_object', 'value': CAR},
    ],
}
NOT_ID = {'field': 'id', 'operator': '!=', 'value': 999999}


async def add(dataset_id, rows, media_type='image'):
    samples = [{'filepath': path, 'metadata': metadata, 'media_type': media_type} for path, metadata in rows]
    return (await datasets.add_samples(dataset_id, {'samples': samples}, user=USER))['ids']


async def run_cleanup(database):
    """Run the maintenance cleanup to completion, returning how many target-release units it took."""
    job = {'lane': 'maintenance', 'created_at': await database.fetchval('SELECT NOW()'), 'checkpoint': {}}
    units = 0
    while True:
        unit = await job_service.selection_cleanup(database.pool)(job)
        if unit.finished:
            return units
        units += unit.message == 'Releasing targets of discarded bulk jobs'
        if unit.publish is not None:
            async with database.pool.acquire() as connection, connection.transaction():
                await unit.publish(connection)
        job['checkpoint'] = unit.checkpoint


@pytest.mark.parametrize(
    'node',
    [
        CROSS_GRAIN,
        {'logic': 'and', 'filters': [NOT_ID, CROSS_GRAIN]},
        {
            'logic': 'and',
            'filters': [
                NOT_ID,
                {'field': 'media_type', 'operator': '=', 'value': 'image'},
                {'logic': 'not', 'filters': [{'field': '_cg', 'operator': 'has_matching_object', 'value': CAR}]},
            ],
        },
    ],
    ids=['with-grain', 'id-exclusion-first', 'negated-after-id-exclusion'],
)
def test_cross_grain_filter_targets_exactly_the_grid_view(registry_database, node):
    async def scenario(database):
        images = await add(1, [(f'img{i}.jpg', {}) for i in range(6)])
        await add(
            1,
            [(f'obj{i}.jpg', {'parent_id': images[i], 'label': 'car' if i < 2 else 'tree'}) for i in range(6)],
            'object',
        )
        # Another dataset's object names image 3 as its parent; only an unscoped subquery sees it.
        await add(2, [('foreign.jpg', {'parent_id': images[2], 'label': 'car'})], 'object')
        args: list = [1]
        sql, _ = datasets.parse_filter_node(node, args, 2, None, None, 1, array_columns={'tags'})
        grid = {
            row['id'] for row in await database.fetch(f'SELECT id FROM samples WHERE dataset_id=$1 AND {sql}', *args)
        }
        submitted(await datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters=node, add_tags=['bulk']), USER))
        job = await finish(database)
        assert 'o.dataset_id = 1 ' in job['args']['predicate']
        assert {row['id'] for row in await database.fetch("SELECT id FROM samples WHERE 'bulk'=ANY(tags)")} == grid

    registry_database(scenario)


def test_discarded_delete_unblocks_revert_and_releases_targets(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(bulk_jobs, 'BATCH_SIZE', 2)
        monkeypatch.setattr(job_service, 'PRUNE_BATCH_SIZE', 2)
        ids = (await ingest([{'k': n} for n in range(6)]))['ids']
        await versions.create_version(1, versions.CreateVersionRequest(name='before'))
        version_id = await database.fetchval("SELECT id FROM dataset_versions WHERE name='before'")
        job_id = submitted(
            await datasets.bulk_delete_samples(1, datasets.BulkTargetRequest(sample_ids=ids, background=True), USER),
        )
        job = await step(database, await claim(database))
        await job_queue.cancel_job(database.pool, job_id)
        assert (await step(database, job))['state'] == 'cancelled'
        assert await database.fetchval('SELECT count(*) FROM samples') == 4
        with pytest.raises(HTTPException) as error:
            await versions.revert_to_version(1, version_id, restore_deleted=True, user=USER)
        assert error.value.status_code == 409
        assert f'delete job {job_id} (cancelled, by admin)' in error.value.detail
        assert 'discard' in error.value.detail

        assert (await bulk_jobs.discard(database.pool, job_id, {**USER, 'username': 'admin'}))['discarded']
        row = await database.fetchrow('SELECT state,result,message FROM jobs WHERE id=$1', job_id)
        assert row['state'] == 'cancelled' and row['result']['discarded']['by'] == 'admin'
        assert row['result']['deleted_count'] == 2 and row['message'] == 'Discarded by admin'
        with pytest.raises(HTTPException) as error:
            await bulk_jobs.resume(database.pool, job_id)
        assert error.value.status_code == 409 and 'discarded' in error.value.detail

        await versions.revert_to_version(1, version_id, restore_deleted=True, user=USER)
        assert await database.fetchval('SELECT count(*) FROM samples') == 6
        assert await database.fetchval('SELECT count(*) FROM bulk_job_targets WHERE job_id=$1', job_id) == 6
        assert await run_cleanup(database) == 3
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM bulk_job_targets)')
        assert await database.fetchval('SELECT count(*) FROM samples') == 6

    registry_database(scenario)


def test_discarded_failed_field_job_no_longer_blocks_promotion(registry_database):
    async def scenario(database):
        await ingest([{'source': 1}])
        job_id = submitted(
            await datasets.rename_field(
                1,
                'source',
                datasets.FieldRenameRequest(new_name='target', background=True),
                USER,
            ),
        )
        await database.execute("UPDATE jobs SET state='failed',finished_at=now() WHERE id=$1", job_id)
        with pytest.raises(HTTPException) as error:
            await datasets.promote_field(1, 'source', USER)
        assert error.value.status_code == 409 and f'rename job {job_id} (failed' in error.value.detail
        await bulk_jobs.discard(database.pool, job_id, USER)
        await datasets.promote_field(1, 'source', USER)
        assert await database.fetchval(
            "SELECT is_promoted FROM dataset_fields WHERE dataset_id=1 AND field_name='source'",
        )

    registry_database(job_helpers.with_runner(scenario))


def test_discard_http_policy(registry_database, monkeypatch):
    import httpx
    from fastapi import FastAPI

    from backend.routers import jobs
    from backend.routers.auth import get_current_user

    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute(
            "INSERT INTO users(id,username,password_hash,role) VALUES (2,'alice','x','power_user'),"
            "(3,'bob','x','power_user')",
        )
        await database.execute('UPDATE datasets SET is_global=TRUE WHERE id=1')
        await ingest([{}])
        alice, bob = {'id': 2, 'role': 'power_user', 'username': 'alice'}, {'id': 3, 'role': 'power_user'}
        job_id = submitted(
            await datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters={}, add_tags=['x']), alice),
        )
        app = FastAPI()
        app.include_router(jobs.router)
        current = {'user': alice}
        app.dependency_overrides[get_current_user] = lambda: current['user']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/jobs/{job_id}/discard')
            assert response.status_code == 409 and 'Cancel' in response.json()['detail']
            await job_queue.cancel_job(database.pool, job_id)
            current['user'] = bob
            assert (await client.post(f'/jobs/{job_id}/discard')).status_code == 403
            current['user'] = alice
            for _ in range(2):  # Idempotent.
                response = await client.post(f'/jobs/{job_id}/discard')
                assert response.status_code == 200 and response.json()['discarded'] is True
        history = await database.fetchval('SELECT result FROM jobs WHERE id=$1', job_id)
        assert history['discarded']['by'] == 'alice' and history['discarded']['user_id'] == 2
        await bulk_jobs.require_no_pending_bulk(database, 1)

    registry_database(scenario)


STAGING_LOG = """
CREATE TABLE staging_log(n SERIAL, rows INTEGER, unclaimable BOOLEAN, gate_held BOOLEAN);
CREATE TABLE staging_fail(seal BOOLEAN);
CREATE FUNCTION log_staging() RETURNS trigger AS $$
BEGIN
    INSERT INTO staging_log(rows,unclaimable,gate_held)
    SELECT count(*),
           bool_and(j.available_at='infinity' AND j.checkpoint ? 'staging'),
           EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()
                  AND classid=20261016 AND objid=1 AND objsubid=2 AND mode='ExclusiveLock')
      FROM new_targets t JOIN jobs j ON j.id=t.job_id;
    IF EXISTS (SELECT 1 FROM staging_fail WHERE seal) THEN
        UPDATE datasets SET deleting = TRUE WHERE id = 1;
    ELSIF EXISTS (SELECT 1 FROM staging_fail) AND (SELECT count(*) FROM staging_log) = 2 THEN
        RAISE EXCEPTION 'busy' USING ERRCODE = 'lock_not_available';
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER log_staging AFTER INSERT ON bulk_job_targets REFERENCING NEW TABLE AS new_targets
    FOR EACH STATEMENT EXECUTE FUNCTION log_staging();
"""


def test_explicit_targets_are_staged_in_bounded_batches_after_the_gate(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(bulk_jobs, 'STAGE_BATCH_SIZE', 2, raising=False)
        await database.execute(STAGING_LOG)
        ids = (await ingest([{}] * 6))['ids']
        target = [*ids[:5], ids[0]]
        job_id = submitted(
            await datasets.bulk_tag_samples(
                1,
                datasets.BulkTagRequest(sample_ids=target, add_tags=['x'], background=True),
                USER,
            ),
        )
        log = [
            tuple(row) for row in await database.fetch('SELECT rows,unclaimable,gate_held FROM staging_log ORDER BY n')
        ]
        assert log == [(2, True, False), (2, True, False), (1, True, False)]
        row = await database.fetchrow('SELECT available_at<=now() AS ready,checkpoint FROM jobs WHERE id=$1', job_id)
        assert row['ready'] and row['checkpoint'] == {}
        assert (await finish(database))['result']['updated_count'] == 5

    registry_database(scenario)


@pytest.mark.parametrize('cause', ['contention', 'dataset-sealed'])
def test_interrupted_staging_is_discarded_never_run_and_released(registry_database, monkeypatch, cause):
    async def scenario(database):
        monkeypatch.setattr(bulk_jobs, 'STAGE_BATCH_SIZE', 2, raising=False)
        await database.execute(STAGING_LOG)
        await database.execute('INSERT INTO staging_fail VALUES($1)', cause == 'dataset-sealed')
        ids = (await ingest([{}] * 5))['ids']
        expected = HTTPException if cause == 'contention' else asyncpg.ObjectNotInPrerequisiteStateError
        with pytest.raises(expected) as error:
            await datasets.bulk_tag_samples(
                1,
                datasets.BulkTagRequest(sample_ids=ids, add_tags=['x'], background=True),
                USER,
            )
        if cause == 'contention':
            assert error.value.status_code == 409
        job = await database.fetchrow('SELECT * FROM jobs')
        assert job['state'] == 'cancelled' and job['result']['discarded']
        assert await database.fetchval('SELECT count(*) FROM bulk_job_targets') == 2
        await bulk_jobs.require_no_pending_bulk(database, 1)
        with pytest.raises((HTTPException, asyncpg.ObjectNotInPrerequisiteStateError)):
            await bulk_jobs.resume(database.pool, job['id'])
        assert await claim(database) is None
        assert await run_cleanup(database) == 1
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM bulk_job_targets)')
        assert await database.fetchval("SELECT count(*) FROM samples WHERE 'x'=ANY(tags)") == 0

    registry_database(scenario)


def test_id_view_uses_targets_not_a_retained_id_list(registry_database, monkeypatch):
    original = bulk_jobs._mutate
    plans = []

    async def mutate(connection, *rest):
        plans.append(await connection.fetchval("SELECT current_setting('plan_cache_mode')"))
        return await original(connection, *rest)

    async def scenario(database):
        monkeypatch.setattr(bulk_jobs, '_mutate', mutate)
        ids = (await ingest([{}] * 4))['ids']
        # The SDK's id-view shape: explicit IDs plus the view's own `id in` filter.
        node = {'field': 'id', 'operator': 'in', 'value': ids[1:]}
        job_id = submitted(
            await datasets.bulk_tag_samples(
                1,
                datasets.BulkTagRequest(sample_ids=ids[:3], filters=node, add_tags=['x']),
                USER,
            ),
        )
        args = await database.fetchval('SELECT args FROM jobs WHERE id=$1', job_id)
        assert args['predicate'] == 'TRUE' and args['typed_predicate_args'] == bulk_jobs._freeze_value([])
        assert args['filters'] is None
        assert (
            await database.fetchval('SELECT array_agg(sample_id ORDER BY sample_id) FROM bulk_job_targets') == ids[1:3]
        )
        assert (await finish(database))['result']['updated_count'] == 2
        assert plans and set(plans) == {'force_custom_plan'}

    registry_database(scenario)


@pytest.mark.parametrize('value', [[None], [2**40], ['1'], [True], [1.5]])
def test_invalid_id_filter_values_are_rejected_before_queueing(registry_database, value):
    async def scenario(database):
        await ingest([{}])
        with pytest.raises(HTTPException) as error:
            await datasets.bulk_tag_samples(
                1,
                datasets.BulkTagRequest(filters={'field': 'id', 'operator': 'in', 'value': value}, add_tags=['x']),
                USER,
            )
        assert error.value.status_code == 400
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM jobs)')

    registry_database(scenario)


def test_sdk_sized_per_sample_batches_stay_synchronous(registry_database, monkeypatch):
    async def scenario(database):
        await database.execute(
            "INSERT INTO samples(dataset_id,filepath,media_type) "
            "SELECT 1,'s'||n,'image' FROM generate_series(1,5001) n",
        )
        ids = [row['id'] for row in await database.fetch('SELECT id FROM samples ORDER BY id')]
        updates = [datasets.BulkSetUpdate(sample_id=sid, metadata={'score': n}) for n, sid in enumerate(ids)]
        result = await datasets.bulk_set_metadata(1, datasets.BulkSetRequest(updates=updates[:5000]), USER)
        assert result['updated_count'] == 5000
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM jobs)')
        submitted(await datasets.bulk_set_metadata(1, datasets.BulkSetRequest(updates=updates), USER))

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['tag', 'delete'])
def test_only_deletes_block_fk_writers_on_matching_rows(registry_database, monkeypatch, operation):
    original = bulk_jobs._mutate
    outcome: list[str] = []

    async def scenario(database):
        async def mutate(connection, *rest):
            async with database.pool.acquire() as writer:
                try:
                    async with writer.transaction():
                        await writer.execute("SET LOCAL lock_timeout='200ms'")
                        await writer.execute(
                            "INSERT INTO embeddings(sample_id,field_name,vector) VALUES($1,'clip','[1,0,0]')",
                            ids[0],
                        )
                    outcome.append('inserted')
                except asyncpg.LockNotAvailableError:
                    outcome.append('blocked')
            return await original(connection, *rest)

        monkeypatch.setattr(bulk_jobs, '_mutate', mutate)
        ids = (await ingest([{'pick': True}, {'pick': False}]))['ids']
        node = {'field': 'pick', 'operator': '=', 'value': True}
        submitted(
            await (
                datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters=node, add_tags=['x']), USER)
                if operation == 'tag'
                else datasets.bulk_delete_samples(1, datasets.BulkTargetRequest(filters=node), USER)
            ),
        )
        await finish(database)
        assert outcome == (['inserted'] if operation == 'tag' else ['blocked'])

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['tag', 'delete'])
def test_delete_units_take_the_rank_fence_before_cascading(registry_database, operation):
    async def scenario(database):
        await ingest([{}])
        submitted(
            await (
                datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters={}, add_tags=['x']), USER)
                if operation == 'tag'
                else datasets.bulk_delete_samples(1, datasets.BulkTargetRequest(filters={}), USER)
            ),
        )
        job = await claim(database)
        async with database.pool.acquire() as rebuild, rebuild.transaction():
            # A full rank rebuild: lifecycle shared, then the dataset's rank fence exclusive.
            await rebuild.execute('SELECT pg_advisory_xact_lock_shared(20261016,1), pg_advisory_xact_lock(20261020,1)')
            if operation == 'delete':
                with pytest.raises(job_queue.QueueContended, match='map rank'):
                    await step(database, job)
            else:
                job = await step(database, job)
        assert (await finish(database, job))['state'] == 'succeeded'
        assert await database.fetchval('SELECT count(*) FROM samples') == (1 if operation == 'tag' else 0)

    registry_database(scenario)
