"""Queue-barrier and bounded-pruning regressions on the field registry write path."""

import asyncio
import os

import asyncpg
import pytest
from fastapi import HTTPException

from backend import field_registry, sample_ingest
from backend.routers import datasets, quality
from backend.tests import test_field_registry_postgres
from backend.tests.test_field_registry_postgres import USER, definitions, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def settled_or_waiting(database, task, blocker_pid):
    async def poll():
        while True:
            if task.done():
                try:
                    return None, task.result()
                except HTTPException as error:
                    return None, error
            waiting_pid = await database.fetchval(
                """SELECT pid FROM pg_stat_activity
                   WHERE datname = current_database() AND state = 'active'
                     AND $1::integer = ANY(pg_blocking_pids(pid))
                   ORDER BY pid LIMIT 1""",
                blocker_pid,
            )
            if waiting_pid is not None:
                return waiting_pid, None
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), 5)


async def cancel_pending(task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    'operation',
    [
        'delete',
        'patch_remove',
        'bulk_remove',
        'create_array_existing',
        'create_array_adopt',
        'ingest_update',
        'clone',
        'delete_dataset',
        'duplicates_finalize',
    ],
)
def test_interactive_operations_never_queue_exclusive_lifecycle_gate(registry_database, operation):
    async def scenario(database):
        target_id, unrelated_id = (await ingest([{'removed': 1, 'retained': 1}, {'untouched': 1}]))['ids']
        if operation.startswith('create_array'):
            await datasets.create_array_field(
                1 if operation == 'create_array_existing' else 2,
                datasets.CreateArrayFieldRequest(field_name='reviewed'),
                USER,
            )

        async def perform():
            if operation == 'delete':
                return await datasets.delete_sample(1, target_id, USER)
            if operation == 'delete_dataset':
                return await datasets.delete_dataset(1, USER)
            if operation == 'duplicates_finalize':
                return await quality.duplicates_finalize(
                    1,
                    quality.DuplicatesFinalizeRequest(token='rereview-lifecycle', group_field='retained'),
                    USER,
                )
            if operation == 'patch_remove':
                return await datasets.update_sample(
                    1,
                    target_id,
                    datasets.SampleUpdate(remove_metadata_keys=['removed']),
                    USER,
                )
            if operation == 'bulk_remove':
                return await datasets.bulk_set_metadata(
                    1,
                    datasets.BulkSetRequest(sample_ids=[target_id], remove_metadata_keys=['removed']),
                    USER,
                )
            if operation.startswith('create_array'):
                return await datasets.create_array_field(
                    1,
                    datasets.CreateArrayFieldRequest(field_name='reviewed'),
                    USER,
                )
            if operation == 'clone':
                return await datasets.clone_field(
                    1,
                    'retained',
                    datasets.FieldRenameRequest(new_name='cloned'),
                    USER,
                )
            return await ingest([{'retained': 2}], on_duplicate='update')

        async with database.pool.acquire() as long_writer:
            async with long_writer.transaction():
                await long_writer.execute('SELECT pg_advisory_xact_lock_shared(20261016, 1)')
                operation_task = asyncio.create_task(perform())
                try:
                    waiting_pid, result = await settled_or_waiting(
                        database,
                        operation_task,
                        long_writer.get_server_pid(),
                    )
                    async with database.pool.acquire() as later_writer:
                        async with later_writer.transaction():
                            granted = await later_writer.fetchval(
                                'SELECT pg_try_advisory_xact_lock_shared(20261016, 1)',
                            )
                    assert granted, f'{operation} queued an exclusive request and blocked a later ordinary writer'
                    assert waiting_pid is None, f'{operation} waited for the long writer instead of returning promptly'
                    if isinstance(result, HTTPException):
                        assert result.status_code == 409
                    await asyncio.wait_for(
                        datasets.update_sample(
                            1,
                            unrelated_id,
                            datasets.SampleUpdate(metadata={'ordinary': 2}),
                            USER,
                        ),
                        5,
                    )
                finally:
                    await cancel_pending(operation_task)
        assert await database.fetchval("SELECT metadata->'ordinary' FROM samples WHERE id = $1", unrelated_id) == 2

    registry_database(scenario)


@pytest.mark.parametrize('exclusive_writer', [False, True])
@pytest.mark.parametrize('surface', ['helper', 'sidebar'])
def test_cold_value_reads_remain_nonblocking_and_incomplete(registry_database, exclusive_writer, surface):
    async def scenario(database):
        sample_id = (await ingest([{}]))['ids'][0]
        await database.execute("UPDATE samples SET tags = ARRAY['kept'] WHERE id = $1", sample_id)
        await database.execute('DELETE FROM dataset_field_stats WHERE dataset_id = 1')

        async def read_values():
            if surface == 'helper':
                return await field_registry.cached_distinct_values(
                    database.pool,
                    1,
                    'tags',
                    'SELECT DISTINCT unnest(tags) AS val FROM samples WHERE dataset_id = $1 LIMIT 201',
                    [1],
                )
            fields = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
            return fields['tags']['values'], fields['tags']['values_complete']

        async with database.pool.acquire() as long_writer:
            async with long_writer.transaction():
                function = 'pg_advisory_xact_lock' if exclusive_writer else 'pg_advisory_xact_lock_shared'
                await long_writer.execute(f'SELECT {function}(20261016, 1)')
                reader = asyncio.create_task(read_values())
                try:
                    waiting_pid, result = await settled_or_waiting(database, reader, long_writer.get_server_pid())
                    assert waiting_pid is None, 'A cold value-list read waited behind the lifecycle writer'
                    assert not isinstance(result, HTTPException)
                    values, complete = result
                    assert values == ['kept']
                    assert complete is False
                    assert not await database.fetchval(
                        "SELECT COALESCE(bool_or(values_complete), FALSE) FROM dataset_field_stats "
                        "WHERE dataset_id = 1 AND field_name = 'tags'",
                    )
                finally:
                    await cancel_pending(reader)

    registry_database(scenario)


@pytest.mark.parametrize('replacement', ['same_keys', 'removed_key', 'duplicate_winner'])
def test_update_ingest_prunes_only_keys_missing_from_effective_payload(registry_database, monkeypatch, replacement):
    async def scenario(database):
        original = {f'retained_{index:02d}': index for index in range(38)} | {'removed': 1}
        await ingest([original])
        offered: set[str] = set()
        original_prune = field_registry.prune_fields

        async def record_pruning(connection, dataset_id, names):
            candidates = set(names)
            offered.update(candidates)
            return await original_prune(connection, dataset_id, candidates)

        monkeypatch.setattr(sample_ingest, 'prune_fields', record_pruning)
        retained = {name: value for name, value in original.items() if name != 'removed'}
        payloads = [original] if replacement == 'same_keys' else [retained]
        if replacement == 'duplicate_winner':
            payloads = [original, retained]
        await datasets.add_samples(
            1,
            {
                'on_duplicate': 'update',
                'samples': [{'filepath': 'sample-0.jpg', 'metadata': metadata} for metadata in payloads],
            },
            USER,
        )
        expected = set() if replacement == 'same_keys' else {'removed'}
        assert offered == expected, 'Unchanged metadata keys must not issue dataset-wide existence probes'
        fields = await definitions(database)
        assert ('removed' in fields) is (replacement == 'same_keys')
        assert all(name in fields for name in retained)

    registry_database(scenario)


def test_pruning_uses_standalone_first_match_probes(registry_database, monkeypatch):
    async def scenario(database):
        sample_id = (await ingest([{'common': 1, 'orphan': 1}]))['ids'][0]
        await database.execute("UPDATE samples SET metadata = metadata - 'orphan' WHERE id = $1", sample_id)
        sample_queries: list[str] = []
        original_fetch = asyncpg.Connection.fetch
        original_fetchval = asyncpg.Connection.fetchval
        original_fetchrow = asyncpg.Connection.fetchrow

        def record_query(query):
            normalized = ' '.join(query.lower().split())
            if 'from samples' in normalized and 'metadata ?' in normalized:
                sample_queries.append(normalized)

        async def record_fetch(connection, query, *args, **kwargs):
            record_query(query)
            return await original_fetch(connection, query, *args, **kwargs)

        async def record_fetchval(connection, query, *args, **kwargs):
            record_query(query)
            return await original_fetchval(connection, query, *args, **kwargs)

        async def record_fetchrow(connection, query, *args, **kwargs):
            record_query(query)
            return await original_fetchrow(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetch', record_fetch)
        monkeypatch.setattr(asyncpg.Connection, 'fetchval', record_fetchval)
        monkeypatch.setattr(asyncpg.Connection, 'fetchrow', record_fetchrow)
        async with database.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute('SELECT pg_advisory_xact_lock(20261016, 1)')
                await field_registry.prune_fields(connection, 1, {'common', 'orphan'})
        fields = await definitions(database)
        assert 'common' in fields and 'orphan' not in fields
        assert sample_queries
        assert all(query.startswith('select 1 from samples ') and 'limit 1' in query for query in sample_queries), (
            'Correlated DELETE/NOT EXISTS probes can materialize a whole-database GIN posting list for each key'
        )

    registry_database(scenario)


def test_existing_native_array_registration_preserves_cache_identity(registry_database):
    async def scenario(database):
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
        await database.execute(
            """INSERT INTO dataset_field_stats
                   (dataset_id, field_name, field_type, unique_values, values_complete)
               VALUES (1, 'reviewed', 'enum', ARRAY['accepted'], TRUE)""",
        )
        before = await database.fetchrow(
            "SELECT xmin::text AS token, unique_values FROM dataset_field_stats "
            "WHERE dataset_id = 1 AND field_name = 'reviewed'",
        )
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
        after = await database.fetchrow(
            "SELECT xmin::text AS token, unique_values FROM dataset_field_stats "
            "WHERE dataset_id = 1 AND field_name = 'reviewed'",
        )
        assert after == before, 'Re-registering a native field must not discard a valid value cache'

    registry_database(scenario)


def test_numeric_histogram_uses_json_type_without_per_row_regex(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'score': 1}, {'score': 2}])
        await ingest([{'score': '0347677e05719710'}, {'score': '3'}, {'score': None}], prefix='later')
        queries: list[str] = []
        original_fetchrow = asyncpg.Connection.fetchrow
        original_fetch = asyncpg.Connection.fetch

        async def record_fetchrow(connection, query, *args, **kwargs):
            if 'WITH data AS' in query:
                queries.append(query)
            return await original_fetchrow(connection, query, *args, **kwargs)

        async def record_fetch(connection, query, *args, **kwargs):
            if 'WITH data AS' in query:
                queries.append(query)
            return await original_fetch(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetchrow', record_fetchrow)
        monkeypatch.setattr(asyncpg.Connection, 'fetch', record_fetch)
        histogram = await datasets.get_histogram(1, 'score', bins=2, filters=None, top=50)
        assert histogram['total'] == 5 and histogram['missing'] == 3
        assert sum(bucket['count'] for bucket in histogram['buckets']) == 2
        assert len(queries) == 2
        assert all("jsonb_typeof(_value) = 'number'" in query for query in queries)
        assert all(' ~ ' not in query and 'pg_input_is_valid' not in query for query in queries)

    registry_database(scenario)


def test_cached_value_publication_never_waits_for_exclusive_lifecycle(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{}]))['ids'][0]
        await database.execute("UPDATE samples SET tags = ARRAY['kept'] WHERE id = $1", sample_id)
        await database.execute(
            """INSERT INTO dataset_field_stats
                   (dataset_id, field_name, field_type, unique_values, values_complete)
               VALUES (1, 'tags', 'enum', ARRAY['old'], FALSE)
               ON CONFLICT (dataset_id, field_name) DO UPDATE
               SET unique_values = ARRAY['old'], values_complete = FALSE""",
        )
        async with database.pool.acquire() as publisher:
            async with publisher.transaction():
                await publisher.execute('SELECT pg_advisory_xact_lock(20261016, 1)')
                reader = asyncio.create_task(
                    field_registry.cached_distinct_values(
                        database.pool,
                        1,
                        'tags',
                        'SELECT DISTINCT unnest(tags) AS val FROM samples WHERE dataset_id = $1 LIMIT 201',
                        [1],
                    ),
                )
                try:
                    waiting_pid, result = await settled_or_waiting(database, reader, publisher.get_server_pid())
                    assert waiting_pid is None
                    assert result == (['kept'], False)
                    assert await database.fetchval(
                        "SELECT unique_values FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'tags'",
                    ) == ['old']
                finally:
                    await cancel_pending(reader)

    registry_database(scenario)


@pytest.mark.parametrize('field_name', ['tags', 'detections.label'])
def test_cached_value_publication_skips_a_shared_writers_locked_cache(registry_database, field_name):
    async def scenario(database):
        sample_id = (await ingest([{'detections': [{'label': 'kept'}]}]))['ids'][0]
        await database.execute("UPDATE samples SET tags = ARRAY['kept'] WHERE id = $1", sample_id)
        await database.execute(
            """INSERT INTO dataset_field_stats
                   (dataset_id, field_name, field_type, unique_values, values_complete)
               VALUES (1, $1, 'enum', ARRAY['old'], FALSE)
               ON CONFLICT (dataset_id, field_name) DO UPDATE
               SET unique_values = ARRAY['old'], values_complete = FALSE""",
            field_name,
        )
        query = (
            'SELECT DISTINCT unnest(tags) AS val FROM samples WHERE dataset_id = $1 LIMIT 201'
            if field_name == 'tags'
            else "SELECT DISTINCT jsonb_array_elements(metadata->'detections')->>'label' AS val "
            'FROM samples WHERE dataset_id = $1 LIMIT 201'
        )
        async with database.pool.acquire() as writer:
            async with writer.transaction():
                await writer.execute('SELECT pg_advisory_xact_lock_shared(20261016, 1)')
                await writer.fetchrow(
                    'SELECT id FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = $1 FOR UPDATE',
                    field_name,
                )
                reader = asyncio.create_task(
                    field_registry.cached_distinct_values(database.pool, 1, field_name, query, [1]),
                )
                try:
                    waiting_pid, result = await settled_or_waiting(database, reader, writer.get_server_pid())
                    assert waiting_pid is None, 'A value-list read waited for an ordinary writer cache-row lock'
                    assert result == (['kept'], False)
                    assert await database.fetchval(
                        'SELECT unique_values FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = $1',
                        field_name,
                    ) == ['old']
                finally:
                    await cancel_pending(reader)

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['clear', 'rename', 'clone', 'rename_source'])
@pytest.mark.parametrize('parent', ['detections', 'ground_truth', 'predictions', 'polygons', 'segmentation'])
def test_field_lifecycle_invalidates_derived_annotation_caches(registry_database, operation, parent):
    async def scenario(database):
        def annotation(label):
            return {'labels': [label]} if parent == 'segmentation' else [{'label': label, 'bbox': [0, 0, 1, 1]}]

        await ingest([{parent: annotation('old'), 'source_labels': annotation('new'), 'unrelated_score': 17}])
        derived = parent + ('.labels' if parent == 'segmentation' else '.label')
        before = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        assert before[derived]['values'] == ['old']
        assert before[derived]['values_complete'] is True
        unrelated_token = await database.fetchval(
            "SELECT xmin::text FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'unrelated_score'",
        )

        if operation == 'clear':
            await datasets.clear_field(1, parent, USER)
        elif operation == 'rename_source':
            await datasets.rename_field(1, parent, datasets.FieldRenameRequest(new_name='archived_labels'), USER)
        else:
            endpoint = datasets.clone_field if operation == 'clone' else datasets.rename_field
            await endpoint(1, 'source_labels', datasets.FieldRenameRequest(new_name=parent, overwrite=True), USER)

        assert not await database.fetchval(
            'SELECT EXISTS (SELECT 1 FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = $1)',
            derived,
        ), 'A renamed, replaced, or removed annotation parent retained its obsolete child label cache'
        assert (
            await database.fetchval(
                "SELECT xmin::text FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'unrelated_score'",
            )
            == unrelated_token
        )
        if operation in ('clear', 'rename_source'):
            await ingest([{parent: annotation('new')}], prefix='reintroduced')
        after = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        assert after[derived]['values'] == ['new']
        assert after[derived]['values_complete'] is True

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['clear', 'rename', 'clone'])
@pytest.mark.parametrize(
    ('overlay', 'presentation'),
    [
        ({'type': 'heatmap', 'map_path': 'source.npy', 'range': [0, 1]}, 'heatmap'),
        ({'maskPath': 'mask.png'}, 'segmentation'),
        ([{'_type': 'keypoints', 'points': [[0.5, 0.5]]}], 'keypoints'),
        ({'label': 'classified'}, 'classification'),
    ],
)
def test_field_lifecycle_preserves_overlay_discovery(registry_database, operation, overlay, presentation):
    async def scenario(database):
        await ingest([{'source_overlay': overlay}])
        before = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        assert before['source_overlay']['type'] == presentation
        if operation == 'clear':
            await datasets.clear_field(1, 'source_overlay', USER)
        else:
            endpoint = datasets.clone_field if operation == 'clone' else datasets.rename_field
            await endpoint(1, 'source_overlay', datasets.FieldRenameRequest(new_name='target_overlay'), USER)
        # Writes retain discovery; the registry hides removals until statistics republishes it.
        assert await database.fetchval(
            """SELECT EXISTS (SELECT 1 FROM dataset_field_stats WHERE dataset_id = 1
               AND field_name = ANY($1::text[]) AND unique_values && ARRAY['source_overlay'])""",
            ['__heatmap_fields__', '__mask_overlay_fields__', '__keypoint_fields__', '__classification_fields__'],
        )
        after = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        if operation == 'clear':
            assert 'source_overlay' not in after
        else:
            assert after['target_overlay']['type'] == presentation
            if operation == 'clone':
                assert after['source_overlay']['type'] == presentation
            else:
                assert 'source_overlay' not in after

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['type', 'display'])
def test_field_presentation_changes_preserve_unchanged_derived_caches(registry_database, operation):
    async def scenario(database):
        await ingest([{'detections': [{'label': 'kept'}], 'overlay': {'type': 'heatmap', 'map_path': 'source.npy'}}])
        await datasets._get_dataset_field_stats_impl(1, None)
        names = [
            'detections.label',
            '__heatmap_fields__',
            '__mask_overlay_fields__',
            '__keypoint_fields__',
            '__classification_fields__',
        ]
        before = await database.fetch(
            'SELECT field_name, unique_values, values_complete, xmin::text FROM dataset_field_stats '
            'WHERE dataset_id = 1 AND field_name = ANY($1::text[]) ORDER BY field_name',
            names,
        )
        if operation == 'type':
            await datasets.set_field_type(1, 'detections', datasets.SetFieldTypeRequest(field_type='string'), USER)
        else:
            await datasets.set_field_display_type(
                1,
                'detections',
                datasets.SetDisplayTypeRequest(display_type='enum'),
                USER,
            )
        after = await database.fetch(
            'SELECT field_name, unique_values, values_complete, xmin::text FROM dataset_field_stats '
            'WHERE dataset_id = 1 AND field_name = ANY($1::text[]) ORDER BY field_name',
            names,
        )
        assert after == before

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['rename', 'clone'])
@pytest.mark.parametrize('parent', ['detections', 'ground_truth', 'predictions', 'polygons', 'segmentation'])
def test_field_lifecycle_replaces_complete_empty_annotation_cache(registry_database, operation, parent):
    async def scenario(database):
        empty = {'labels': []} if parent == 'segmentation' else []
        new_value = {'labels': ['new']} if parent == 'segmentation' else [{'label': 'new', 'bbox': [0, 0, 1, 1]}]
        await ingest([{parent: empty, 'source_labels': new_value}])
        derived = parent + ('.labels' if parent == 'segmentation' else '.label')
        await datasets._get_dataset_field_stats_impl(1, None)
        before = await database.fetchrow(
            'SELECT unique_values, values_complete FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = $1',
            derived,
        )
        if parent in ('ground_truth', 'predictions'):
            assert before is None  # Empty ordinary lists have no discovered label type yet.
        else:
            assert before['unique_values'] == [] and before['values_complete'] is True
        endpoint = datasets.clone_field if operation == 'clone' else datasets.rename_field
        await endpoint(1, 'source_labels', datasets.FieldRenameRequest(new_name=parent, overwrite=True), USER)
        after = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        assert after[derived]['values'] == ['new']
        assert after[derived]['values_complete'] is True

    registry_database(scenario)
