"""Deterministic regressions for the field lifecycle."""

import asyncio
import os

import asyncpg
import pytest
from fastapi import HTTPException

from backend import sample_ingest
from backend.field_registry import register_fields
from backend.routers import datasets, versions
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, definitions, ingest

registry_database = test_field_registry_postgres.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def wait_for_blocked_session(database, blocker_pid, finished_task=None):
    async def poll():
        while True:
            if finished_task is not None and finished_task.done():
                await finished_task
                return None
            waiting_pid = await database.fetchval(
                """SELECT pid FROM pg_stat_activity
                   WHERE datname = current_database() AND state = 'active'
                     AND $1::integer = ANY(pg_blocking_pids(pid))
                   ORDER BY pid LIMIT 1""",
                blocker_pid,
            )
            if waiting_pid is not None:
                return waiting_pid
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), 5)


@pytest.mark.parametrize('operation', ['clear', 'rename'])
@pytest.mark.parametrize('writer', ['ingest', 'patch', 'bulk'])
def test_field_removal_serializes_with_concurrent_writers(registry_database, operation, writer):
    async def scenario(database):
        first_id, second_id = (await ingest([{'source': 1}, {}]))['ids']
        pending = []
        try:
            async with database.pool.acquire() as blocker:
                async with blocker.transaction():
                    blocker_pid = await blocker.fetchval('SELECT pg_backend_pid()')
                    await blocker.fetchrow('SELECT id FROM samples WHERE id = $1 FOR UPDATE', first_id)
                    removal = asyncio.create_task(
                        datasets.clear_field(1, 'source', USER)
                        if operation == 'clear'
                        else datasets.rename_field(
                            1,
                            'source',
                            datasets.FieldRenameRequest(new_name='renamed'),
                            USER,
                        ),
                    )
                    pending.append(removal)
                    removal_pid = await wait_for_blocked_session(database, blocker_pid)
                    if writer == 'ingest':
                        concurrent_write = ingest([{'source': 2}], prefix='concurrent')
                    elif writer == 'patch':
                        concurrent_write = datasets.update_sample(
                            1,
                            second_id,
                            datasets.SampleUpdate(metadata={'source': 2}),
                            USER,
                        )
                    else:
                        concurrent_write = datasets.bulk_set_metadata(
                            1,
                            datasets.BulkSetRequest(sample_ids=[second_id], metadata={'source': 2}),
                            USER,
                        )
                    write_task = asyncio.create_task(concurrent_write)
                    pending.append(write_task)
                    await wait_for_blocked_session(database, removal_pid, write_task)
                await asyncio.wait_for(asyncio.gather(*pending), 5)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        assert await database.fetchval("SELECT count(*) FROM samples WHERE metadata ? 'source'") == 1
        fields = await definitions(database)
        assert fields['source']['field_type'] == 'number'
        if operation == 'rename':
            assert fields['renamed']['field_type'] == 'number'
            assert await database.fetchval("SELECT metadata->'renamed' FROM samples WHERE id = $1", first_id) == 1
        await run_refresh(database)
        assert 'source' in {field['name'] for field in await datasets.get_dataset_schema(1)}

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['patch', 'bulk', 'replace', 'delete'])
def test_last_value_removal_prunes_implicit_definitions(registry_database, operation):
    async def scenario(database):
        await ingest([{'retained': 1}], prefix='retained')
        version = await versions.create_version(1, versions.CreateVersionRequest(name='before-temporary'))
        sample_id = (await ingest([{'temporary': 1, 'temporary_object': {'nested': True}}]))['ids'][0]
        if operation == 'patch':
            await datasets.update_sample(
                1,
                sample_id,
                datasets.SampleUpdate(remove_metadata_keys=['temporary', 'temporary_object']),
                USER,
            )
        elif operation == 'bulk':
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(
                    sample_ids=[sample_id],
                    metadata={},
                    remove_metadata_keys=['temporary', 'temporary_object'],
                ),
                USER,
            )
        elif operation == 'replace':
            await ingest([{}], on_duplicate='update')
        else:
            await datasets.delete_sample(1, sample_id, USER)
        assert set(await definitions(database)) == {'retained'}
        if operation != 'delete':
            await datasets.delete_sample(1, sample_id, USER)
        async with database.pool.acquire() as connection:
            status = await versions._version_status(connection, 1)
        assert status['current_version']['id'] == version['id']
        assert not status['has_changes']

    registry_database(scenario)


def test_cleanup_keeps_configuration_native_fields_and_remaining_nulls(registry_database):
    async def scenario(database):
        first_id, second_id = (
            await ingest(
                [
                    {'implicit': 1, 'displayed': 1, 'overlay': {'maskPath': 'mask.png'}, 'indexed': 1, 'nullable': 1},
                    {'nullable': None},
                ],
            )
        )['ids']
        await database.execute("UPDATE dataset_fields SET display_type = 'enum' WHERE field_name = 'displayed'")
        await database.execute(
            "UPDATE dataset_fields SET overlay_config = '{\"opacity\":0.5}'::jsonb WHERE field_name = 'overlay'",
        )
        await database.execute("UPDATE dataset_fields SET is_promoted = TRUE WHERE field_name = 'indexed'")
        await database.execute(
            """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin)
               VALUES (1, 'native', 'list', 'native_array'), (1, 'system', 'string', 'system')""",
        )
        await datasets.delete_sample(1, first_id, USER)
        assert set(await definitions(database)) == {'displayed', 'overlay', 'indexed', 'nullable', 'native', 'system'}
        await datasets.delete_sample(1, second_id, USER)
        assert set(await definitions(database)) == {'displayed', 'overlay', 'indexed', 'native', 'system'}

    registry_database(scenario)


@pytest.mark.parametrize('second_operation', ['patch', 'clear'])
def test_patch_lifecycle_lock_does_not_serialize_ordinary_edits(registry_database, monkeypatch, second_operation):
    async def scenario(database):
        first_id, second_id = (await ingest([{'first': 0}, {'second': 0}]))['ids']
        registered = asyncio.Event()
        release = asyncio.Event()
        original_register = register_fields
        first_pid = None

        async def pause_first_registration(connection, dataset_id, metadata, *args, **kwargs):
            nonlocal first_pid
            values = list(metadata)
            result = await original_register(connection, dataset_id, values, *args, **kwargs)
            if any('first' in item for item in values):
                first_pid = connection.get_server_pid()
                registered.set()
                await release.wait()
            return result

        monkeypatch.setattr(datasets, 'register_fields', pause_first_registration)
        first_task = asyncio.create_task(
            datasets.update_sample(1, first_id, datasets.SampleUpdate(metadata={'first': 1}), USER),
        )
        pending = [first_task]
        try:
            await asyncio.wait_for(registered.wait(), 5)
            if second_operation == 'patch':
                await asyncio.wait_for(
                    datasets.update_sample(1, second_id, datasets.SampleUpdate(metadata={'second': 1}), USER),
                    5,
                )
            else:
                second_task = asyncio.create_task(datasets.clear_field(1, 'first', USER))
                pending.append(second_task)
                waiting_pid = await wait_for_blocked_session(database, first_pid)
                assert await database.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = $1 AND locktype = 'advisory' AND NOT granted)",
                    waiting_pid,
                )
            release.set()
            await asyncio.wait_for(asyncio.gather(*pending), 5)
        finally:
            release.set()
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    registry_database(scenario)


def test_blocked_field_clear_does_not_block_other_datasets(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'source': 1}]))['ids'][0]
        removal = None
        try:
            async with database.pool.acquire() as blocker:
                async with blocker.transaction():
                    blocker_pid = await blocker.fetchval('SELECT pg_backend_pid()')
                    await blocker.fetchrow('SELECT id FROM samples WHERE id = $1 FOR UPDATE', sample_id)
                    removal = asyncio.create_task(datasets.clear_field(1, 'source', USER))
                    await wait_for_blocked_session(database, blocker_pid)
                    await asyncio.wait_for(ingest([{'source': 2}], dataset_id=2), 5)
                await asyncio.wait_for(removal, 5)
        finally:
            if removal is not None:
                if not removal.done():
                    removal.cancel()
                await asyncio.gather(removal, return_exceptions=True)

    registry_database(scenario)


@pytest.mark.parametrize('origin', ['metadata', 'native_array'])
def test_inferred_tags_configuration_and_values_restore(registry_database, origin):
    async def scenario(database):
        if origin == 'native_array':
            await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='inferred_tags'), USER)
            sample_id = (await ingest([{}]))['ids'][0]
            await datasets.update_sample(
                1,
                sample_id,
                datasets.SampleUpdate(array_fields={'inferred_tags': ['original']}),
                USER,
            )
        else:
            sample_id = (await ingest([{'inferred_tags': ['original']}]))['ids'][0]
        assert (await definitions(database))['inferred_tags']['origin'] == origin
        version = await versions.create_version(1, versions.CreateVersionRequest(name='inferred-tags-original'))
        await datasets.set_field_display_type(
            1,
            'inferred_tags',
            datasets.SetDisplayTypeRequest(display_type='enum'),
            USER,
        )
        async with database.pool.acquire() as connection:
            assert (await versions._version_status(connection, 1))['has_changes']
        update = (
            datasets.SampleUpdate(array_fields={'inferred_tags': ['changed']})
            if origin == 'native_array'
            else datasets.SampleUpdate(metadata={'inferred_tags': ['changed']})
        )
        await datasets.update_sample(1, sample_id, update, USER)
        await versions._restore_version(1, version['id'], restore_deleted=False, user=USER, exact_membership=False)
        field = (await definitions(database))['inferred_tags']
        assert field['origin'] == origin and field['display_type'] is None
        expression = 'inferred_tags' if origin == 'native_array' else "metadata->'inferred_tags'"
        assert await database.fetchval(f'SELECT {expression} FROM samples WHERE id = $1', sample_id) == ['original']
        async with database.pool.acquire() as connection:
            assert (await versions._version_status(connection, 1))['current_version']['id'] == version['id']

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['rename', 'clone'])
def test_missing_source_cannot_remove_overwrite_target(registry_database, operation):
    async def scenario(database):
        sample_id = (await ingest([{'target': 'retained'}]))['ids'][0]
        original_fields = await definitions(database)
        with pytest.raises(HTTPException) as error:
            handler = datasets.rename_field if operation == 'rename' else datasets.clone_field
            await handler(1, 'missing', datasets.FieldRenameRequest(new_name='target', overwrite=True), USER)
        assert error.value.status_code == 404
        assert await definitions(database) == original_fields
        assert await database.fetchval("SELECT metadata->>'target' FROM samples WHERE id = $1", sample_id) == 'retained'
        field = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'target')
        assert field['declared_type'] == 'string'

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['rename', 'clone'])
def test_metadata_field_cannot_replace_native_target(registry_database, operation):
    async def scenario(database):
        sample_id = (await ingest([{'source': 'retained'}]))['ids'][0]
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='native_target'), USER)
        await datasets.update_sample(
            1,
            sample_id,
            datasets.SampleUpdate(array_fields={'native_target': ['native-value']}),
            USER,
        )
        original_fields = await definitions(database)
        with pytest.raises(HTTPException) as error:
            handler = datasets.rename_field if operation == 'rename' else datasets.clone_field
            await handler(1, 'source', datasets.FieldRenameRequest(new_name='native_target', overwrite=True), USER)
        assert error.value.status_code == 409
        assert await definitions(database) == original_fields
        assert await database.fetchval('SELECT native_target FROM samples WHERE id = $1', sample_id) == ['native-value']
        assert await database.fetchval("SELECT metadata->>'source' FROM samples WHERE id = $1", sample_id) == 'retained'

    registry_database(scenario)


@pytest.mark.parametrize('origin', ['native_array', 'system'])
def test_clear_rejects_fields_that_are_not_metadata(registry_database, origin):
    async def scenario(database):
        sample_id = (await ingest([{'kept': 1}]))['ids'][0]
        if origin == 'native_array':
            await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='native'), USER)
            await datasets.update_sample(1, sample_id, datasets.SampleUpdate(array_fields={'native': ['before']}), USER)
            field_name = 'native'
        else:
            await database.execute(
                "INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin) "
                "VALUES (1, 'builtin', 'string', 'system')",
            )
            field_name = 'builtin'
        original_fields = await definitions(database)
        with pytest.raises(HTTPException) as error:
            await datasets.clear_field(1, field_name, USER)
        assert error.value.status_code == 400
        assert await definitions(database) == original_fields
        if origin == 'native_array':
            assert await database.fetchval('SELECT native FROM samples WHERE id = $1', sample_id) == ['before']
            # The registration survived, so the sync's next array write still lands.
            await datasets.update_sample(1, sample_id, datasets.SampleUpdate(array_fields={'native': ['after']}), USER)
            assert await database.fetchval('SELECT native FROM samples WHERE id = $1', sample_id) == ['after']

    registry_database(scenario)


def test_index_placement_is_not_snapshotted_or_versioned(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}])
        version = await versions.create_version(1, versions.CreateVersionRequest(name='without-index'))
        snapshot = await database.fetchval('SELECT dataset_snapshot FROM dataset_versions WHERE id = $1', version['id'])
        assert all('is_promoted' not in field for field in snapshot['fields'])
        await datasets.promote_field(1, 'score', USER)
        async with database.pool.acquire() as connection:
            assert (await versions._version_status(connection, 1))['current_version']['id'] == version['id']
        field = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'score')
        assert field['is_promoted'] is True
        await datasets.demote_field(1, 'score', USER)
        async with database.pool.acquire() as connection:
            assert (await versions._version_status(connection, 1))['current_version']['id'] == version['id']
        field = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'score')
        assert field['is_promoted'] is False

    registry_database(job_helpers.with_runner(scenario))


@pytest.mark.parametrize('current_indexed', [False, True])
def test_restore_keeps_current_index_not_historical_flag(registry_database, current_indexed):
    async def scenario(database):
        sample_id = (await ingest([{'score': 1}]))['ids'][0]
        version = await versions.create_version(1, versions.CreateVersionRequest(name='historical-index'))
        if current_indexed:
            await datasets.promote_field(1, 'score', USER)
        await database.execute(
            "UPDATE dataset_versions SET dataset_snapshot = jsonb_set(dataset_snapshot, '{fields,0,is_promoted}', $2) "
            'WHERE id = $1',
            version['id'],
            not current_indexed,
        )
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'score': 2}), USER)
        await versions._restore_version(1, version['id'], restore_deleted=False, user=USER, exact_membership=False)
        assert (await definitions(database))['score']['is_promoted'] is current_indexed
        field = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'score')
        assert field['is_promoted'] is current_indexed
        async with database.pool.acquire() as connection:
            assert (await versions._version_status(connection, 1))['current_version']['id'] == version['id']

    registry_database(job_helpers.with_runner(scenario))


def test_unknown_widening_uses_same_registry_then_statistics_lock_order(registry_database, monkeypatch):
    async def scenario(database):
        sample_id = (await ingest([{'widened': None}]))['ids'][0]
        registered = asyncio.Event()
        release = asyncio.Event()
        original_register = register_fields
        ingest_pid = None

        async def pause_after_widening(connection, *args, **kwargs):
            nonlocal ingest_pid
            result = await original_register(connection, *args, **kwargs)
            ingest_pid = connection.get_server_pid()
            registered.set()
            await release.wait()
            return result

        monkeypatch.setattr(sample_ingest, 'register_fields', pause_after_widening)
        insert_task = asyncio.create_task(ingest([{'widened': 1}], prefix='concurrent'))
        pending = [insert_task]
        try:
            await asyncio.wait_for(registered.wait(), 5)
            edit_task = asyncio.create_task(
                datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'widened': 2}), USER),
            )
            pending.append(edit_task)
            await wait_for_blocked_session(database, ingest_pid)
            release.set()
            await asyncio.wait_for(asyncio.gather(*pending), 5)
        finally:
            release.set()
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        assert (await definitions(database))['widened']['field_type'] == 'number'
        assert await database.fetchval("SELECT metadata->'widened' FROM samples WHERE id = $1", sample_id) == 2

    registry_database(scenario)


def test_promotion_does_not_index_changed_definition(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'score': 1}])
        building = asyncio.Event()
        release = asyncio.Event()
        original_execute = asyncpg.Connection.execute

        async def pause_index_build(connection, query, *args, **kwargs):
            if query.startswith('CREATE INDEX CONCURRENTLY'):
                building.set()
                await release.wait()
            return await original_execute(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'execute', pause_index_build)
        promotion = asyncio.create_task(datasets.promote_field(1, 'score', USER))
        try:
            await asyncio.wait_for(building.wait(), 5)
            await datasets.set_field_type(1, 'score', datasets.SetFieldTypeRequest(field_type='string'), USER)
            release.set()
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(promotion, 5)
            assert error.value.status_code == 409
        finally:
            release.set()
            if not promotion.done():
                promotion.cancel()
            await asyncio.gather(promotion, return_exceptions=True)
        fields = await definitions(database)
        assert fields['score']['is_promoted'] is False
        assert fields['score']['field_type'] == 'string'
        assert not await database.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = $1)",
            datasets._promoted_index_name(1, 'score'),
        )
        async with database.pool.acquire() as connection:
            assert await connection.fetchval('SELECT pg_try_advisory_lock(20261017, 1)')
            await connection.execute('SELECT pg_advisory_unlock(20261017, 1)')

    registry_database(job_helpers.with_runner(scenario))


@pytest.mark.parametrize('operation', ['clear', 'rename', 'demote'])
def test_destructive_index_operations_reject_active_build(registry_database, monkeypatch, operation):
    async def scenario(database):
        sample_id = (await ingest([{'score': 1}]))['ids'][0]
        building = asyncio.Event()
        release = asyncio.Event()
        original_execute = asyncpg.Connection.execute

        async def pause_index_build(connection, query, *args, **kwargs):
            if query.startswith('CREATE INDEX CONCURRENTLY'):
                building.set()
                await release.wait()
            return await original_execute(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'execute', pause_index_build)
        promotion = asyncio.create_task(datasets.promote_field(1, 'score', USER))
        try:
            await asyncio.wait_for(building.wait(), 5)
            with pytest.raises(HTTPException) as error:
                if operation == 'clear':
                    await asyncio.wait_for(datasets.clear_field(1, 'score', USER), 5)
                elif operation == 'rename':
                    await asyncio.wait_for(
                        datasets.rename_field(1, 'score', datasets.FieldRenameRequest(new_name='renamed'), USER),
                        5,
                    )
                else:
                    await asyncio.wait_for(datasets.demote_field(1, 'score', USER), 5)
            assert error.value.status_code == 409
            await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'ordinary': 2}), USER)
            release.set()
            await asyncio.wait_for(promotion, 5)
        finally:
            release.set()
            if not promotion.done():
                promotion.cancel()
            await asyncio.gather(promotion, return_exceptions=True)
        assert (await definitions(database))['score']['is_promoted'] is True
        assert await database.fetchval("SELECT metadata->'score' FROM samples WHERE id = $1", sample_id) == 1

    registry_database(job_helpers.with_runner(scenario))


@pytest.mark.parametrize('operation', ['clear', 'rename'])
def test_delayed_index_cleanup_cannot_drop_recreated_field_index(registry_database, monkeypatch, operation):
    async def scenario(database):
        sample_id = (await ingest([{'score': 1}]))['ids'][0]
        await datasets.promote_field(1, 'score', USER)
        cleaning = asyncio.Event()
        release = asyncio.Event()
        original_drop = datasets._drop_promoted_index

        async def pause_cleanup(*args, **kwargs):
            cleaning.set()
            await release.wait()
            return await original_drop(*args, **kwargs)

        monkeypatch.setattr(datasets, '_drop_promoted_index', pause_cleanup)
        removal = asyncio.create_task(
            datasets.clear_field(1, 'score', USER)
            if operation == 'clear'
            else datasets.rename_field(1, 'score', datasets.FieldRenameRequest(new_name='renamed'), USER),
        )
        try:
            await asyncio.wait_for(cleaning.wait(), 5)
            await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'score': 2}), USER)
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(datasets.promote_field(1, 'score', USER), 5)
            assert error.value.status_code == 409
            release.set()
            await asyncio.wait_for(removal, 5)
        finally:
            release.set()
            if not removal.done():
                removal.cancel()
            await asyncio.gather(removal, return_exceptions=True)
        await datasets.promote_field(1, 'score', USER)
        assert (await definitions(database))['score']['is_promoted'] is True
        assert await database.fetchval(
            'SELECT index_metadata.indisvalid FROM pg_class index_table '
            'JOIN pg_index index_metadata ON index_metadata.indexrelid = index_table.oid '
            'WHERE index_table.relname = $1 AND index_table.relnamespace = current_schema()::regnamespace',
            datasets._promoted_index_name(1, 'score'),
        )

    registry_database(job_helpers.with_runner(scenario))


def test_restore_during_statistics_measurement_is_safe_and_not_blocked(registry_database):
    from backend.tests.statistics_helpers import refresh_during

    async def scenario(database):
        sample_id = (await ingest([{'score': 1}]))['ids'][0]
        version = await versions.create_version(1, versions.CreateVersionRequest(name='before-edit'))
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'score': 2}), USER)
        await run_refresh(database, 1)

        async def restore():
            await asyncio.wait_for(versions._restore_version(1, version['id'], False, USER, False), 1)

        await refresh_during(database, restore)
        assert await database.fetchval("SELECT metadata->'score' FROM samples WHERE id=$1", sample_id) == 1
        assert await database.fetchval('SELECT count(*) FROM dataset_versions') == 2
        assert await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=1)')
        assert await database.fetchval('SELECT stats_full_refreshed_at FROM datasets WHERE id=1') is None
        await run_refresh(database, 1)
        assert (
            await database.fetchval(
                "SELECT max_value FROM dataset_field_stats WHERE dataset_id=1 AND field_name='score'",
            )
            == 1
        )
        assert not await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=1)')

    registry_database(scenario)


def test_statistics_refresh_skips_active_restore(registry_database, monkeypatch):
    from backend import statistics
    from backend.tests.test_job_runner import make_worker

    async def scenario(database):
        sample_id = (await ingest([{'score': 1}]))['ids'][0]
        version = await versions.create_version(1, versions.CreateVersionRequest(name='before-edit'))
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'score': 2}), USER)
        restoring, release = asyncio.Event(), asyncio.Event()
        original = versions._snapshot_arrays

        async def pause_restore(*args, **kwargs):
            result = await original(*args, **kwargs)
            restoring.set()
            await release.wait()
            return result

        monkeypatch.setattr(versions, '_snapshot_arrays', pause_restore)
        restore = asyncio.create_task(versions._restore_version(1, version['id'], False, USER, False))
        try:
            await asyncio.wait_for(restoring.wait(), 5)
            job = await statistics.request_refresh(database.pool, 1)
            worker = await make_worker(
                database,
                {statistics.KIND: statistics.handler(database.pool)},
                policies={statistics.KIND: statistics.POLICY},
            )
            await asyncio.wait_for(worker.run_once(), 5)
            state = await database.fetchrow('SELECT state,failures,contentions FROM jobs WHERE id=$1', job['id'])
            assert state['state'] == 'queued' and state['failures'] == 0 and state['contentions'] == 1
            release.set()
            await asyncio.wait_for(restore, 5)
        finally:
            release.set()
            if not restore.done():
                restore.cancel()
            await asyncio.gather(restore, return_exceptions=True)
        assert await database.fetchval("SELECT metadata->'score' FROM samples WHERE id=$1", sample_id) == 1
        await database.execute('UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1', job['id'])
        refreshed = await run_refresh(database)
        assert any(row['dataset_id'] == 1 and row['error'] is None for row in refreshed)
        assert (
            await database.fetchval(
                "SELECT max_value FROM dataset_field_stats WHERE dataset_id=1 AND field_name='score'",
            )
            == 1
        )

    registry_database(scenario)


async def revert_with_conflicts(database, monkeypatch, conflicts):
    """Edit a sample, then revert it while the first `conflicts` restore attempts each have a write
    committed after the attempt's SERIALIZABLE snapshot, to the row the attempt then updates.
    Returns the sample id, the restore attempts made and the revert's HTTPException (or None)."""
    sample_id = (await ingest([{'score': 1}]))['ids'][0]
    version = await versions.create_version(1, versions.CreateVersionRequest(name='before-edit'))
    await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'score': 2}), USER)
    original = versions._snapshot_arrays
    attempts = 0

    async def write_during_restore(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= conflicts:
            await database.execute("UPDATE samples SET tags = ARRAY['concurrent'] WHERE id=$1", sample_id)
        return await original(*args, **kwargs)

    monkeypatch.setattr(versions, '_snapshot_arrays', write_during_restore)
    try:
        await asyncio.wait_for(versions.revert_to_version(1, version['id'], user=USER), 10)
    except HTTPException as error:
        return sample_id, attempts, error
    return sample_id, attempts, None


def test_restore_retries_after_a_concurrent_write(registry_database, monkeypatch):
    async def scenario(database):
        sample_id, attempts, error = await revert_with_conflicts(database, monkeypatch, conflicts=1)
        assert error is None and attempts == 2
        sample = await database.fetchrow("SELECT metadata->'score' AS score, tags FROM samples WHERE id=$1", sample_id)
        assert sample['score'] == 1 and sample['tags'] == []
        # The failed attempt's auto-snapshot rolled back with it: one undo point, not two.
        assert await database.fetchval("SELECT count(*) FROM dataset_versions WHERE name LIKE 'auto-%'") == 1

    registry_database(scenario)


def test_restore_reports_409_when_every_attempt_conflicts(registry_database, monkeypatch):
    async def scenario(database):
        sample_id, attempts, error = await revert_with_conflicts(database, monkeypatch, conflicts=3)
        assert error is not None and error.status_code == 409 and attempts == 3
        sample = await database.fetchrow("SELECT metadata->'score' AS score, tags FROM samples WHERE id=$1", sample_id)
        assert sample['score'] == 2 and sample['tags'] == ['concurrent']
        assert await database.fetchval("SELECT count(*) FROM dataset_versions WHERE name LIKE 'auto-%'") == 0

    registry_database(scenario)
