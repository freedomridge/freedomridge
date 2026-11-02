"""Deterministic field registry refresh and tuple-lock regressions."""

import asyncio
import os

import pytest

from backend import sample_ingest, statistics_measure
from backend.field_registry import invalidate_values, lock_field_lifecycle, merge_annotation_values, register_fields
from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import refresh_during, run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def blocked_session(database, blocker_pid):
    async def poll():
        while True:
            blocked = await database.fetchval(
                """SELECT pid FROM pg_stat_activity
                   WHERE datname = current_database() AND $1::integer = ANY(pg_blocking_pids(pid))
                   ORDER BY pid LIMIT 1""",
                blocker_pid,
            )
            if blocked is not None:
                return blocked
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), 5)


async def settle(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('operation', ['patch', 'bulk'])
@pytest.mark.parametrize('phase', ['before_measurement', 'after_measurement'])
def test_refresh_measurement_never_blocks_edits_or_certifies_obsolete_values(registry_database, operation, phase):
    async def scenario(database):
        sample_id = (await ingest([{'color': 'old'}]))['ids'][0]
        await run_refresh(database)

        async def edit():
            if operation == 'patch':
                mutation = datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'color': 'new'}), USER)
            else:
                mutation = datasets.bulk_set_metadata(
                    1,
                    datasets.BulkSetRequest(sample_ids=[sample_id], metadata={'color': 'new'}),
                    USER,
                )
            await asyncio.wait_for(mutation, 1)
            color = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'color')
            assert not color['values_complete'] or 'new' in color.get('values', [])

        refreshed = await refresh_during(database, edit, phase=phase)
        assert all(row['error'] is None for row in refreshed)
        color = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'color')
        assert not color['values_complete'] or 'new' in color.get('values', [])

    registry_database(scenario)


@pytest.mark.parametrize('mode', ['uniform', 'per_sample'])
def test_bulk_writers_keep_sample_lock_order_after_patch_moves_a_tuple(registry_database, monkeypatch, mode):
    async def scenario(database):
        first_id, second_id = (await ingest([{'color': 'first'}, {'color': 'second'}]))['ids']
        await database.execute(
            """CREATE FUNCTION pause_first_bulk_tuple() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN
                   IF NEW.metadata ? 'bulk_first' AND NOT OLD.metadata ? 'bulk_first' THEN
                       PERFORM pg_advisory_xact_lock(20261017, 102);
                   END IF;
                   RETURN NEW;
               END $$;
               CREATE TRIGGER pause_first_bulk_tuple BEFORE UPDATE ON samples
               FOR EACH ROW EXECUTE FUNCTION pause_first_bulk_tuple()""",
        )
        patch_ready = asyncio.Event()
        release_patch = asyncio.Event()
        patch_pid = None

        async def sequential_scan(connection, dataset_id, *args, **kwargs):
            await connection.execute('SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off')
            return await lock_field_lifecycle(connection, dataset_id, *args, **kwargs)

        async def pause_patch(connection, dataset_id, metadata, *args, **kwargs):
            nonlocal patch_pid
            result = await register_fields(connection, dataset_id, metadata, *args, **kwargs)
            if any('patch_churn' in item for item in metadata):
                patch_pid = connection.get_server_pid()
                patch_ready.set()
                await release_patch.wait()
            return result

        monkeypatch.setattr(datasets, 'lock_field_lifecycle', sequential_scan)
        monkeypatch.setattr(datasets, 'register_fields', pause_patch)

        def bulk_request(name):
            if mode == 'uniform':
                return datasets.BulkSetRequest(sample_ids=[first_id, second_id], metadata={name: True})
            selected = (first_id, second_id) if name == 'bulk_first' else (second_id, first_id)
            return datasets.BulkSetRequest(
                updates=[datasets.BulkSetUpdate(sample_id=sample_id, metadata={name: True}) for sample_id in selected],
            )

        pending = []
        try:
            async with database.pool.acquire() as blocker:
                async with blocker.transaction():
                    await blocker.execute('SELECT pg_advisory_xact_lock(20261017, 102)')
                    patching = asyncio.create_task(
                        datasets.update_sample(
                            1,
                            first_id,
                            datasets.SampleUpdate(metadata={'patch_churn': True}),
                            USER,
                        ),
                    )
                    pending.append(patching)
                    await asyncio.wait_for(patch_ready.wait(), 5)
                    first_bulk = asyncio.create_task(datasets.bulk_set_metadata(1, bulk_request('bulk_first'), USER))
                    pending.append(first_bulk)
                    await blocked_session(database, patch_pid)
                    release_patch.set()
                    await asyncio.wait_for(patching, 5)
                    first_bulk_pid = await blocked_session(database, blocker.get_server_pid())
                    second_bulk = asyncio.create_task(datasets.bulk_set_metadata(1, bulk_request('bulk_second'), USER))
                    pending.append(second_bulk)
                    await blocked_session(database, first_bulk_pid)
                results = await asyncio.wait_for(asyncio.gather(first_bulk, second_bulk, return_exceptions=True), 5)
            assert all(isinstance(result, dict) for result in results), results
            rows = await database.fetch('SELECT id, metadata FROM samples ORDER BY id')
            assert all(row['metadata']['bulk_first'] and row['metadata']['bulk_second'] for row in rows)
            assert rows[0]['metadata']['patch_churn'] is True
        finally:
            release_patch.set()
            await settle(pending)

    registry_database(scenario)


def test_refresh_does_not_hold_first_dataset_locks_while_measuring_another(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'color': 'old'}]))['ids'][0]
        await ingest([{'score': 1}], dataset_id=2)
        await run_refresh(database)

        async def edit_first_dataset():
            await asyncio.wait_for(
                datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'color': 'new'}), USER),
                1,
            )

        refreshed = await refresh_during(database, edit_first_dataset, dataset_id=2, all_datasets=True)
        assert all(row['error'] is None for row in refreshed)
        assert {row['dataset_id'] for row in refreshed} == {1, 2}
        assert not await database.fetchval(
            "SELECT values_complete FROM dataset_field_stats WHERE dataset_id=1 AND field_name='color'",
        )
        assert await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=1)')

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['patch', 'bulk'])
def test_missing_cache_invalidation_survives_postcommit_dirty_flag_gap(registry_database, monkeypatch, operation):
    async def scenario(database):
        sample_id = (await ingest([{'color': 'old'}]))['ids'][0]
        await run_refresh(database)
        await database.execute('DELETE FROM dataset_field_stats WHERE dataset_id=1')
        original_execute = database.execute

        async def reject_postcommit_dirty_flag(query, *args, **kwargs):
            assert not query.strip().startswith('UPDATE datasets SET stats_need_refresh = TRUE')
            return await original_execute(query, *args, **kwargs)

        monkeypatch.setattr(database, 'execute', reject_postcommit_dirty_flag)

        async def edit():
            if operation == 'patch':
                await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'color': 'new'}), USER)
            else:
                await datasets.bulk_set_metadata(
                    1,
                    datasets.BulkSetRequest(sample_ids=[sample_id], metadata={'color': 'new'}),
                    USER,
                )
            assert await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=1)')

        refreshed = await refresh_during(database, edit)
        assert [(row['dataset_id'], row['error']) for row in refreshed] == [(1, None)]
        color = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'color')
        assert color['values_complete'] is False
        await run_refresh(database)
        color = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'color')
        assert color['values'] == ['new'] and color['values_complete'] is True and color['stats_pending'] is False

    registry_database(scenario)


def test_missing_parent_cache_creation_cannot_reverse_annotation_ingest_locks(registry_database, monkeypatch):
    async def scenario(database):
        sample_id = (await ingest([{'z': 1, 'a': 2, 'detections': [{'label': 'old'}]}]))['ids'][0]
        await database.execute('DELETE FROM dataset_field_stats WHERE dataset_id = 1')
        inserted_labels = asyncio.Event()
        release_ingest = asyncio.Event()
        invalidated = asyncio.Event()

        async def pause_annotation_merge(connection, dataset_id, metadata, inserted_fields):
            await merge_annotation_values(connection, dataset_id, metadata, inserted_fields)
            inserted_labels.set()
            await release_ingest.wait()

        async def observe_invalidation(connection, dataset_id, names):
            await invalidate_values(connection, dataset_id, names)
            invalidated.set()

        monkeypatch.setattr(sample_ingest, 'merge_annotation_values', pause_annotation_merge)
        monkeypatch.setattr(datasets, 'invalidate_values', observe_invalidation)
        pending = []
        try:
            inserting = asyncio.create_task(
                ingest([{'z': 3, 'detections': [{'label': 'inserted'}], 'a': 4}], prefix='later'),
            )
            pending.append(inserting)
            await asyncio.wait_for(inserted_labels.wait(), 5)
            patching = asyncio.create_task(
                datasets.update_sample(
                    1,
                    sample_id,
                    datasets.SampleUpdate(metadata={'a': 5, 'detections': [{'label': 'patched'}], 'z': 6}),
                    USER,
                ),
            )
            pending.append(patching)
            await asyncio.wait_for(invalidated.wait(), 1)
            release_ingest.set()
            results = await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
            assert all(isinstance(result, dict) for result in results), results
            assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 1') == 2
            labels = await database.fetchrow(
                "SELECT unique_values, values_complete FROM dataset_field_stats WHERE field_name = 'detections.label'",
            )
            assert not labels['values_complete'] or {'old', 'inserted', 'patched'} <= set(labels['unique_values'])
        finally:
            release_ingest.set()
            await settle(pending)

    registry_database(scenario)


def test_publication_preserves_an_overlay_invalidation_waiting_on_the_old_tuple(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'hm': {'type': 'heatmap', 'path': 'old.png'}}])
        await run_refresh(database)
        original = statistics_measure.aggregate_field
        changed = False

        async def invalidate_after_measurement(*args):
            nonlocal changed
            result = await original(*args)
            if result['field_name'] == '__heatmap_fields__' and not changed:
                changed = True
                status = await asyncio.wait_for(
                    database.execute(
                        "DELETE FROM dataset_field_stats WHERE dataset_id=1 AND field_name='__heatmap_fields__'",
                    ),
                    1,
                )
                assert status == 'DELETE 1'
            return result

        monkeypatch.setattr(statistics_measure, 'aggregate_field', invalidate_after_measurement)
        await run_refresh(database)
        assert changed
        assert not await database.fetchval(
            "SELECT EXISTS(SELECT 1 FROM dataset_field_stats WHERE field_name='__heatmap_fields__')",
        )

    registry_database(scenario)


@pytest.mark.parametrize('target_mode', ['ids', 'selection'])
@pytest.mark.parametrize('tag_operation', ['add', 'remove', 'both'])
def test_bulk_tag_and_bulk_set_share_ordered_sample_locks(registry_database, monkeypatch, target_mode, tag_operation):
    async def scenario(database):
        await database.execute(
            "INSERT INTO users (id, username, role) VALUES (1, 'reviewer', 'admin') ON CONFLICT (id) DO NOTHING",
        )
        first_id, second_id, excluded_id = (
            await datasets.add_samples(
                1,
                [
                    {'filepath': f'tag-{index}.jpg', 'tags': ['remove_me'], 'metadata': {'color': str(index)}}
                    for index in range(3)
                ],
                USER,
            )
        )['ids']
        foreign_id = (await ingest([{}], dataset_id=2))['ids'][0]
        await database.execute('UPDATE samples SET tags = ARRAY[\'remove_me\'] WHERE id = $1', foreign_id)
        await datasets.update_sample(1, first_id, datasets.SampleUpdate(metadata={'shifted': True}), USER)
        await database.execute(
            f"""CREATE FUNCTION pause_tag_tuple() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF OLD.id = {second_id} AND NEW.tags IS DISTINCT FROM OLD.tags THEN
                        PERFORM pg_advisory_xact_lock(20261017, 106);
                    END IF;
                    RETURN NEW;
                END $$;
                CREATE TRIGGER pause_tag_tuple BEFORE UPDATE ON samples
                FOR EACH ROW EXECUTE FUNCTION pause_tag_tuple()""",
        )

        async def sequential_scan(connection, dataset_id, *args, **kwargs):
            await connection.execute('SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off')
            return await lock_field_lifecycle(connection, dataset_id, *args, **kwargs)

        monkeypatch.setattr(datasets, 'lock_field_lifecycle', sequential_scan)
        request = datasets.BulkTagRequest(
            sample_ids=[first_id, second_id],
            add_tags=['added'] if tag_operation != 'remove' else [],
            remove_tags=['remove_me'] if tag_operation != 'add' else [],
        )
        if target_mode == 'selection':
            await database.execute(
                "INSERT INTO region_selections (token, sample_id) SELECT 'lock-review', unnest($1::int[])",
                [second_id, first_id, excluded_id, foreign_id],
            )
            request.sample_ids = []
            request.selection_token = 'lock-review'
            request.excluded_ids = [excluded_id]
        pending = []
        try:
            async with database.pool.acquire() as blocker:
                async with blocker.transaction():
                    await blocker.execute('SELECT pg_advisory_xact_lock(20261017, 106)')
                    tagging = asyncio.create_task(datasets.bulk_tag_samples(1, request, USER))
                    pending.append(tagging)
                    tagger_pid = await blocked_session(database, blocker.get_server_pid())
                    setting = asyncio.create_task(
                        datasets.bulk_set_metadata(
                            1,
                            datasets.BulkSetRequest(sample_ids=[first_id, second_id], metadata={'bulk_set': True}),
                            USER,
                        ),
                    )
                    pending.append(setting)
                    await blocked_session(database, tagger_pid)
                results = await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
            assert all(isinstance(result, dict) for result in results), results
            rows = await database.fetch('SELECT id, tags, metadata FROM samples ORDER BY id')
            expected = (
                {'added', 'remove_me'} if tag_operation == 'add' else {'added'} if tag_operation == 'both' else set()
            )
            for row in rows:
                if row['id'] in (first_id, second_id):
                    assert set(row['tags']) == expected
                    assert row['metadata']['bulk_set'] is True
                else:
                    assert row['tags'] == ['remove_me']
                    assert 'bulk_set' not in row['metadata']
        finally:
            await settle(pending)

    registry_database(scenario)
