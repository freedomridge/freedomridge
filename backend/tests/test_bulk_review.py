"""Adversarial regressions for typed targets, row locks and bulk lifecycle races."""

import asyncio
import json
import os
from datetime import datetime, timezone

import asyncpg
import pytest
from fastapi import HTTPException

from backend import bulk_jobs, job_queue
from backend.routers import datasets, versions
from backend.tests import test_field_registry_postgres
from backend.tests.test_bulk_jobs import claim, finish, step, submitted
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


@pytest.mark.parametrize(
    'field,operator,value',
    [
        ('created_at', '>', '2020-01-01T12:34:56'),
        ('created_at', '>=', '2026-01-02T01:02:03.123456+05:30'),
        ('created_at', 'in', ['2026-01-02T01:02:03.123456+05:30', '2001-01-01']),
        ('date', '=', '2026-01-02'),
        ('text', '=', 'snow ☃'),
        ('integer', '=', 7),
        ('number', '>', 1.25),
        ('boolean', '=', True),
        ('missing', 'is_null', None),
        ('tags', 'in', ['seed', 'other']),
        ('id', 'in', [1, 2]),
        ('detections', 'match_elements', {'field': 'confidence', 'operator': '>', 'value': 0.5}),
    ],
)
def test_frozen_filter_types_match_live_query(registry_database, field, operator, value):
    async def scenario(database):
        ids = (
            await ingest(
                [
                    {
                        'date': '2026-01-02',
                        'text': 'snow ☃',
                        'integer': 7,
                        'number': 2.5,
                        'boolean': True,
                        'detections': [{'label': 'car', 'confidence': 0.9}],
                    },
                    {'date': '2001-01-01', 'text': 'other', 'integer': 2, 'number': 0, 'boolean': False},
                ],
            )
        )['ids']
        await database.execute(
            "UPDATE samples SET created_at='2026-01-01T19:32:03.123456Z',tags='{seed}' WHERE id=$1",
            ids[0],
        )
        await database.execute("UPDATE samples SET created_at='2000-01-01' WHERE id=$1", ids[1])
        node = {'field': field, 'operator': operator, 'value': value}
        args: list = []
        predicate, _ = datasets.parse_filter_node(node, args, 1, array_columns={'tags'})
        expected = {row['id'] for row in await database.fetch(f'SELECT id FROM samples WHERE {predicate}', *args)}
        assert expected
        submitted(await datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters=node, add_tags=['done']), USER))
        job = await finish(database)
        assert job['result']['updated_count'] == len(expected)
        assert {row['id'] for row in await database.fetch("SELECT id FROM samples WHERE 'done'=ANY(tags)")} == expected

    registry_database(scenario)


def test_typed_filter_encoding_cannot_confuse_user_json():
    original = [
        None,
        False,
        7,
        1.25,
        'text',
        ['datetime', 'user value'],
        {'datetime': 'user value', 'nested': [datetime(2026, 1, 2, tzinfo=timezone.utc)]},
    ]
    encoded = json.loads(json.dumps(bulk_jobs._freeze_value(original)))
    assert bulk_jobs._restore_value(encoded) == original


def test_only_matches_are_locked_and_stay_locked_through_mutation(registry_database, monkeypatch):
    original = bulk_jobs._mutate

    async def scenario(database):
        ids = (await ingest([{'pick': True}, {'pick': False}]))['ids']
        submitted(
            await datasets.bulk_tag_samples(
                1,
                datasets.BulkTagRequest(filters={'field': 'pick', 'operator': '=', 'value': True}, add_tags=['done']),
                USER,
            ),
        )

        async def inspect(connection, job, selected, *extra):
            assert selected == ids[:1]
            async with database.pool.acquire() as other:
                async with other.transaction():
                    await other.fetch('SELECT id FROM samples WHERE id=$1 FOR UPDATE NOWAIT', ids[1])
                with pytest.raises(asyncpg.LockNotAvailableError):
                    async with other.transaction():
                        await other.fetch('SELECT id FROM samples WHERE id=$1 FOR UPDATE NOWAIT', ids[0])
            return await original(connection, job, selected, *extra)

        monkeypatch.setattr(bulk_jobs, '_mutate', inspect)
        assert (await finish(database))['result']['updated_count'] == 1

    registry_database(scenario)


def test_filter_rechecked_after_waiting_for_concurrent_edit(registry_database):
    async def scenario(database):
        sid = (await ingest([{'pick': True}]))['ids'][0]
        submitted(
            await datasets.bulk_tag_samples(
                1,
                datasets.BulkTagRequest(filters={'field': 'pick', 'operator': '=', 'value': True}, add_tags=['done']),
                USER,
            ),
        )
        job = await claim(database)
        unit = await bulk_jobs.handler(database.pool)(job)
        async with database.pool.acquire() as writer, database.pool.acquire() as publisher:
            transaction = writer.transaction()
            await transaction.start()
            task = None
            try:
                await writer.execute('UPDATE samples SET metadata=\'{"pick":false}\' WHERE id=$1', sid)
                async with publisher.transaction():
                    pid = publisher.get_server_pid()
                    task = asyncio.create_task(unit.publish(publisher))

                    async def wait_for_lock():
                        while not await database.fetchval(
                            "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1",
                            pid,
                        ):
                            if task.done():
                                await task
                                pytest.fail('Publication did not wait for the matching row')
                            await asyncio.sleep(0.01)

                    await asyncio.wait_for(wait_for_lock(), 2)
                    await transaction.commit()
                    transaction = None
                    await task
            finally:
                if transaction is not None:
                    await transaction.rollback()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert unit.checkpoint['affected'] == 0
        assert await database.fetchval('SELECT tags FROM samples WHERE id=$1', sid) == []

    registry_database(scenario)


def test_submission_waits_briefly_for_ingest_and_captures_its_ids(registry_database):
    async def scenario(database):
        async with database.pool.acquire() as writer:
            transaction = writer.transaction()
            await transaction.start()
            await writer.execute('SELECT pg_advisory_xact_lock_shared(20261016,1)')
            await writer.execute("INSERT INTO samples(dataset_id,filepath,media_type) VALUES(1,'inflight','image')")
            task = asyncio.create_task(
                datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters={}, add_tags=['x']), USER),
            )
            try:
                await asyncio.sleep(0.1)
                assert not task.done()
            finally:
                await transaction.commit()
            submitted(await task)
        assert (await finish(database))['result']['updated_count'] == 1

    registry_database(scenario)


def test_bulk_delete_prunes_only_unused_implicit_fields(registry_database):
    async def scenario(database):
        ids = (await ingest([{'gone': 1, 'retained': 2, 'declared': 3}, {'retained': 4}]))['ids']
        await database.execute("UPDATE dataset_fields SET display_type='number' WHERE field_name='declared'")
        submitted(
            await datasets.bulk_delete_samples(
                1,
                datasets.BulkTargetRequest(sample_ids=ids[:1], background=True),
                USER,
            ),
        )
        job = await claim(database)
        job = await step(database, job)
        # A new arrival between delete and pruning keeps the definition.
        await ingest([{'retained': 5}], prefix='late')
        assert (await finish(database, job))['result']['deleted_count'] == 1
        fields = {row['field_name'] for row in await database.fetch('SELECT field_name FROM dataset_fields')}
        assert 'gone' not in fields
        assert {'retained', 'declared'} <= fields
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE block_id=-1)')

    registry_database(scenario)


@pytest.mark.parametrize('state', ['queued', 'running', 'cancelled', 'failed'])
def test_revert_refuses_resumable_bulk_targets(registry_database, state):
    async def scenario(database):
        ids = (await ingest([{'k': 1}] * 3))['ids']
        await versions.create_version(1, versions.CreateVersionRequest(name='before'))
        version_id = await database.fetchval("SELECT id FROM dataset_versions WHERE name='before'")
        await database.execute('DELETE FROM samples WHERE id=$1', ids[-1])
        job_id = submitted(
            await datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters={}, add_tags=['x']), USER),
        )
        if state == 'running':
            await claim(database)
        else:
            await database.execute('UPDATE jobs SET state=$2 WHERE id=$1', job_id, state)
        with pytest.raises(HTTPException, match='bulk') as error:
            await versions.revert_to_version(1, version_id, restore_deleted=True, user=USER)
        assert error.value.status_code == 409
        assert not await database.fetchval('SELECT 1 FROM samples WHERE id=$1', ids[-1])
        assert await database.fetchval('SELECT count(*) FROM dataset_versions') == 1
        if state == 'queued':
            await finish(database)
            await versions.revert_to_version(1, version_id, restore_deleted=True, user=USER)
            assert await database.fetchval('SELECT tags FROM samples WHERE id=$1', ids[-1]) == []

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['rename', 'clear'])
@pytest.mark.parametrize('action', ['promote_field', 'demote_field'])
def test_index_operations_refuse_unfinished_field_jobs(registry_database, operation, action):
    async def scenario(database):
        await ingest([{'source': 1}])
        if operation == 'rename':
            submitted(
                await datasets.rename_field(
                    1,
                    'source',
                    datasets.FieldRenameRequest(new_name='target', background=True),
                    USER,
                ),
            )
        else:
            submitted(await datasets.clear_field(1, 'source', USER, background=True))
        with pytest.raises(HTTPException, match='bulk') as error:
            await getattr(datasets, action)(1, 'source', USER)
        assert error.value.status_code == 409
        await finish(database)
        assert not await database.fetchval(
            "SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() AND indexname='idx_samples_meta_1_source'",
        )

    registry_database(scenario)


def test_resume_missing_dataset_refuses_before_requeue(registry_database):
    from backend.tests.test_dataset_deletion import finish as finish_deletion

    async def scenario(database):
        await ingest([{}])
        job_id = submitted(
            await datasets.bulk_tag_samples(1, datasets.BulkTagRequest(filters={}, add_tags=['x']), USER),
        )
        await job_queue.cancel_job(database.pool, job_id)
        await datasets.delete_dataset(1, USER)
        await finish_deletion(database)
        with pytest.raises(HTTPException) as error:
            await bulk_jobs.resume(database.pool, job_id)
        assert error.value.status_code == 404
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job_id) == 'cancelled'

    registry_database(scenario)


@pytest.mark.parametrize('values', [[], [1, 3]])
def test_id_filter_uses_finite_explicit_targets(registry_database, values):
    async def scenario(database):
        await ingest([{}] * 10)
        submitted(
            await datasets.bulk_tag_samples(
                1,
                datasets.BulkTagRequest(filters={'field': 'id', 'operator': 'in', 'value': values}, add_tags=['x']),
                USER,
            ),
        )
        job = await finish(database)
        assert job['checkpoint'].get('examined', 0) == len(values)
        assert job['result']['updated_count'] == len(values)

    registry_database(scenario)


def test_submission_serializes_with_index_build_and_revert(registry_database):
    async def scenario(database):
        await ingest([{'source': 1}])
        async with database.pool.acquire() as other, datasets._field_index_operation(other, 1):
            with pytest.raises(HTTPException) as error:
                await datasets.rename_field(
                    1,
                    'source',
                    datasets.FieldRenameRequest(new_name='target', background=True),
                    USER,
                )
            assert error.value.status_code == 409
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM jobs)')

    registry_database(scenario)


def test_legacy_json_only_filter_parameters_remain_resumable(registry_database):
    async def scenario(database):
        await ingest([{'score': 3}, {'score': 0}])
        job_id = submitted(
            await datasets.bulk_tag_samples(
                1,
                datasets.BulkTagRequest(filters={'field': 'score', 'operator': '>', 'value': 1}, add_tags=['x']),
                USER,
            ),
        )
        await database.execute(
            "UPDATE jobs SET args=(args-'typed_predicate_args') || '{\"predicate_args\":[1]}'::jsonb WHERE id=$1",
            job_id,
        )
        assert (await finish(database))['result']['updated_count'] == 1

    registry_database(scenario)
