"""Field registry completeness regression contracts."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request

from backend.field_registry import invalidate_values, merge_annotation_values
from backend.models import ObjectToPatchSearchRequest, PatchSearchRequest, SearchRequest
from backend.routers import datasets, embeddings, search, versions
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = registry_helpers.registry_database

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def refresh(database):
    results = await run_refresh(database)
    assert all(result['error'] is None for result in results), results


async def schema_field(name):
    return next(field for field in await datasets.get_dataset_schema(1) if field['name'] == name)


def test_incremental_refresh_extends_complete_values_without_hiding_enums(registry_database):
    async def scenario(database):
        await ingest([{'color': color, 'untouched': 'known'} for color in ['red', 'green', 'blue'] * 10])
        await refresh(database)
        await database.execute(
            """INSERT INTO samples (dataset_id, filepath, media_type, metadata)
               VALUES (1, 'incremental.jpg', 'image', '{"color":"yellow"}')""",
        )
        await refresh(database)
        color = await schema_field('color')
        assert color['type'] == 'enum'
        assert color['values_complete'] is True
        assert set(color['values']) == {'red', 'green', 'blue', 'yellow'}
        assert (await schema_field('untouched'))['values_complete'] is True

    registry_database(scenario)


@pytest.mark.parametrize('distinct_count', [100, 101])
def test_incremental_preview_cap_preserves_proof_until_real_overflow(registry_database, distinct_count):
    async def scenario(database):
        await ingest([{'color': f'color-{index:03}', 'untouched': 'known'} for index in range(99)])
        await refresh(database)
        await database.execute(
            """INSERT INTO samples (dataset_id, filepath, media_type, metadata)
               SELECT 1, 'incremental-' || value || '.jpg', 'image',
                      jsonb_build_object('color', 'color-' || lpad(value::text, 3, '0'))
               FROM generate_series(99, $1::integer) value""",
            distinct_count - 1,
        )
        await refresh(database)
        color = await schema_field('color')
        assert color['values_complete'] is (distinct_count == 100)
        if color['values_complete']:
            assert color['type'] == 'enum'
            assert len(color['values']) == distinct_count
        assert (await schema_field('untouched'))['values_complete'] is True

    registry_database(scenario)


@pytest.mark.parametrize('writer', ['patch', 'bulk'])
def test_edits_cannot_leave_a_falsely_complete_preview(registry_database, writer):
    async def scenario(database):
        sample_id = (await ingest([{'split': 'train'}] * 30))['ids'][0]
        await refresh(database)
        if writer == 'patch':
            await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'split': 'test'}), USER)
        else:
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(sample_ids=[sample_id], metadata={'split': 'test'}),
                USER,
            )
        split = await schema_field('split')
        assert not split['values_complete'] or 'test' in (split.get('values') or [])
        await refresh(database)
        split = await schema_field('split')
        assert not split['values_complete'] or 'test' in (split.get('values') or [])

    registry_database(scenario)


def test_ingest_does_not_treat_a_missing_cache_as_an_empty_field(registry_database):
    async def scenario(database):
        sample_ids = (await ingest([{}] * 5))['ids']
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=sample_ids, metadata={'split': 'A'}),
            USER,
        )
        await ingest([{'split': 'B'}], prefix='later')
        split = await schema_field('split')
        assert not split['values_complete'] or {'A', 'B'} <= set(split.get('values') or [])

    registry_database(scenario)


def test_mixed_scalar_preview_cannot_certify_omitted_numeric_values(registry_database):
    async def scenario(database):
        await ingest([{'mixed': 'word'}, {'mixed': 3}, {'mixed': True}])
        mixed = await schema_field('mixed')
        assert mixed['declared_type'] == 'string'
        assert not mixed['values_complete'] or {'word', '3', 'true'} <= set(mixed.get('values') or [])

    registry_database(scenario)


def test_small_numeric_enum_keeps_complete_exactly_filterable_values(registry_database):
    async def scenario(database):
        await ingest([{'level': level} for level in [1, 2, 3] * 20])
        await datasets.set_field_display_type(1, 'level', datasets.SetDisplayTypeRequest(display_type='enum'), USER)
        before_refresh = await schema_field('level')
        assert set(before_refresh['values']) == {'1', '2', '3'}
        await database.execute('UPDATE datasets SET stats_need_refresh = TRUE WHERE id = 1')
        await refresh(database)
        level = await schema_field('level')
        assert level['values_complete'] is True
        assert set(level['values']) == {'1', '2', '3'}
        matches = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters={'field': 'level', 'operator': 'in', 'value': ['1']}),
        )
        assert matches['total'] == 20

    registry_database(scenario)


def test_numeric_enum_full_refresh_keeps_value_preview_bounded(registry_database):
    async def scenario(database):
        await ingest([{'level': level} for level in range(201)])
        await datasets.set_field_display_type(1, 'level', datasets.SetDisplayTypeRequest(display_type='enum'), USER)
        await database.execute('UPDATE datasets SET stats_need_refresh = TRUE, stats_full_refreshed_at = NULL')
        await refresh(database)
        cached = await database.fetchrow(
            "SELECT unique_values, values_complete FROM dataset_field_stats WHERE field_name = 'level'",
        )
        assert len(cached['unique_values'] or []) <= 100
        assert cached['values_complete'] is False

    registry_database(scenario)


@pytest.mark.parametrize('full_refresh', [False, True])
def test_datetime_bounds_never_claim_to_be_a_complete_enum(registry_database, full_refresh):
    async def scenario(database):
        dates = {'2026-01-01', '2026-01-02', '2026-01-03'}
        sample_ids = (await ingest([{'captured': value} for value in sorted(dates)]))['ids']
        await datasets.set_field_display_type(1, 'captured', datasets.SetDisplayTypeRequest(display_type='enum'), USER)
        if full_refresh:
            await refresh(database)
        cached = await database.fetchrow(
            "SELECT unique_values, values_complete FROM dataset_field_stats WHERE field_name = 'captured'",
        )
        assert cached['values_complete'] is False
        assert len(cached['unique_values']) == 2
        for fields in (
            await datasets.get_dataset_schema(1),
            await datasets._get_dataset_field_stats_impl(1, None),
        ):
            captured = next(field for field in fields if field['name'] == 'captured')
            assert not captured['values_complete'] or dates <= set(captured['values'])
        filters = {'field': 'id', 'operator': 'in', 'value': sample_ids}
        for fields in (
            await datasets.get_dataset_schema(1, filters=json.dumps(filters), lightning_mode=False),
            await datasets._get_dataset_field_stats_impl(1, filters, fields=['captured']),
        ):
            captured = next(field for field in fields if field['name'] == 'captured')
            assert captured['values_complete'] is True
            assert set(captured['values']) == dates

    registry_database(scenario)


def test_small_detection_label_list_is_provably_complete(registry_database):
    async def scenario(database):
        await ingest([{'detections': [{'label': label}]} for label in ['cat', 'dog', 'bird'] * 20])
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        labels = next(field for field in fields if field['name'] == 'detections.label')
        assert set(labels['values']) == {'cat', 'dog', 'bird'}
        assert labels['values_complete'] is True

    registry_database(scenario)


@pytest.mark.parametrize('initial_count, added_count', [(0, 1), (1, 1), (199, 1), (199, 2)])
def test_segmentation_ingest_extends_warmed_complete_cache(registry_database, initial_count, added_count):
    async def scenario(database):
        initial = [f'label-{index:03}' for index in range(initial_count)]
        added = [f'label-{index:03}' for index in range(initial_count, initial_count + added_count)]
        await ingest([{'segmentation': {'labels': initial}}])
        await datasets._get_dataset_field_stats_impl(1, None)
        assert await database.fetchval(
            "SELECT values_complete FROM dataset_field_stats WHERE field_name = 'segmentation.labels'",
        )
        await ingest([{'segmentation': {'labels': added}}], prefix='later')
        cached = await database.fetchrow(
            "SELECT unique_values, values_complete FROM dataset_field_stats WHERE field_name = 'segmentation.labels'",
        )
        expected = sorted(set(initial + added))
        assert cached['unique_values'] == expected[:200]
        assert cached['values_complete'] is (len(expected) <= 200)
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        labels = next(field for field in fields if field['name'] == 'segmentation.labels')
        assert labels['values'] == expected[:200]
        assert labels['values_complete'] is (len(expected) <= 200)

    registry_database(scenario)


@pytest.mark.parametrize('initial', [[], ['old']])
@pytest.mark.parametrize('added', [[7], ['new', 7]])
def test_segmentation_ingest_cannot_certify_omitted_nonstring_labels(registry_database, initial, added):
    async def scenario(database):
        await ingest([{'segmentation': {'labels': initial}}])
        await datasets._get_dataset_field_stats_impl(1, None)
        await ingest([{'segmentation': {'labels': added}}], prefix='later')
        cached = await database.fetchrow(
            "SELECT unique_values, values_complete FROM dataset_field_stats WHERE field_name = 'segmentation.labels'",
        )
        assert cached['values_complete'] is False
        assert set(cached['unique_values'] or []) == set(initial) | {value for value in added if isinstance(value, str)}
        assert (
            await database.fetchval(
                "SELECT metadata->'segmentation'->'labels' FROM samples WHERE filepath = 'later-0.jpg'",
            )
            == added
        )

    registry_database(scenario)


def test_new_label_cache_does_not_omit_labels_written_before_ingest(registry_database):
    async def scenario(database):
        sample_ids = (await ingest([{}] * 5))['ids']
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=sample_ids, metadata={'detections': [{'label': 'old-label'}]}),
            USER,
        )
        await ingest([{'detections': [{'label': 'new-label'}]}], prefix='later')
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        labels = next(field for field in fields if field['name'] == 'detections.label')
        assert not labels['values_complete'] or {'old-label', 'new-label'} <= set(labels['values'])

    registry_database(scenario)


def test_ingest_and_refresh_have_one_count_owner_across_forced_full_refresh(registry_database):
    async def scenario(database):
        await ingest([{'score': index} if index < 45 else {} for index in range(50)])
        await refresh(database)
        await ingest([{'score': index} for index in range(10)], prefix='second')
        await refresh(database)
        count = await database.fetchval("SELECT sample_count FROM dataset_field_stats WHERE field_name = 'score'")
        assert count == 55
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        assert next(field for field in fields if field['name'] == 'score')['null_count'] == 5
        await database.execute('UPDATE datasets SET stats_full_refreshed_at = NULL, stats_need_refresh = TRUE')
        await ingest([{'score': index} for index in range(5)], prefix='third')
        await refresh(database)
        assert (
            await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE field_name = 'score'",
            )
            == 60
        )
        await database.execute('UPDATE datasets SET stats_need_refresh = TRUE')
        await refresh(database)
        assert (
            await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE field_name = 'score'",
            )
            == 60
        )

    registry_database(scenario)


def test_new_field_after_watermark_is_counted_once(registry_database):
    async def scenario(database):
        await ingest([{'existing': 'known'}] * 10)
        await refresh(database)
        await ingest([{'new_number': 1, 'new_string': 'value'}] * 5, prefix='later')
        await refresh(database)
        rows = await database.fetch(
            """SELECT field_name, sample_count FROM dataset_field_stats
               WHERE field_name IN ('new_number', 'new_string')""",
        )
        assert {row['field_name']: row['sample_count'] for row in rows} == {'new_number': 5, 'new_string': 5}

    registry_database(scenario)


def test_native_array_ingest_invalidates_a_previously_complete_cache(registry_database):
    async def scenario(database):
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
        await datasets.add_samples(
            1,
            [{'filepath': 'before.jpg', 'array_fields': {'reviewed': ['old']}}],
            USER,
        )
        await datasets._get_dataset_field_stats_impl(1, None)
        await datasets.add_samples(
            1,
            [{'filepath': 'after.jpg', 'array_fields': {'reviewed': ['new']}}],
            USER,
        )
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        reviewed = next(field for field in fields if field['name'] == 'reviewed')
        assert not reviewed['values_complete'] or {'old', 'new'} <= set(reviewed['values'])

    registry_database(scenario)


@pytest.mark.parametrize('surface', ['sidebar', 'schema'])
def test_selective_recompute_reports_its_own_completeness(registry_database, surface):
    async def scenario(database):
        sample_ids = (await ingest([{'color': f'color-{index}'} for index in range(150)]))['ids']
        await database.execute(
            """UPDATE dataset_field_stats SET unique_values = ARRAY['color-0'], values_complete = FALSE
               WHERE field_name = 'color'""",
        )
        filters = {'field': 'id', 'operator': 'in', 'value': sample_ids[:3]}
        fields = (
            await datasets._get_dataset_field_stats_impl(1, filters, fields=['color'])
            if surface == 'sidebar'
            else await datasets.get_dataset_schema(1, filters=json.dumps(filters), lightning_mode=False)
        )
        color = next(field for field in fields if field['name'] == 'color')
        assert set(color['values']) == {'color-0', 'color-1', 'color-2'}
        assert color['values_complete'] is True

    registry_database(scenario)


def test_cold_label_cache_cannot_overwrite_a_newer_write_as_complete(registry_database):
    async def scenario(database):
        from backend.field_registry import cached_distinct_values

        sample_id = (await ingest([{'detections': [{'label': 'old'}]}]))['ids'][0]
        await database.execute("DELETE FROM dataset_field_stats WHERE field_name = 'detections.label'")
        scanned = asyncio.Event()
        publish = asyncio.Event()

        @asynccontextmanager
        async def paused_acquire():
            async with database.pool.acquire() as connection:

                async def pause_after_label_scan(query, *args):
                    result = await connection.fetch(query, *args)
                    scanned.set()
                    await asyncio.wait_for(publish.wait(), 3)
                    return result

                yield SimpleNamespace(
                    execute=connection.execute,
                    fetchrow=connection.fetchrow,
                    fetchval=connection.fetchval,
                    fetch=pause_after_label_scan,
                    transaction=connection.transaction,
                )

        reader = asyncio.create_task(
            cached_distinct_values(
                SimpleNamespace(acquire=paused_acquire),
                1,
                'detections.label',
                """SELECT DISTINCT jsonb_array_elements(metadata->'detections')->>'label' AS val
                   FROM samples WHERE dataset_id = $1 LIMIT 201""",
                [1],
            ),
        )
        await asyncio.wait_for(scanned.wait(), 3)
        writer = asyncio.create_task(
            datasets.update_sample(
                1,
                sample_id,
                datasets.SampleUpdate(metadata={'detections': [{'label': 'new'}]}),
                USER,
            ),
        )
        await asyncio.wait_for(writer, 1)
        publish.set()
        values, complete = await asyncio.wait_for(reader, 3)
        assert values == ['old'] and complete is False
        labels = await database.fetchrow(
            "SELECT unique_values, values_complete FROM dataset_field_stats WHERE field_name = 'detections.label'",
        )
        assert labels is None or not labels['values_complete'] or 'new' in labels['unique_values']

    registry_database(scenario)


def test_orphaned_derived_labels_are_not_schema_definitions(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, unique_values, values_complete)
               VALUES (1, 'detections.label', 'enum', ARRAY['orphan'], TRUE)""",
        )
        names = {field['name'] for field in await datasets.get_dataset_schema(1)}
        assert 'detections.label' not in names

    registry_database(scenario)


def test_revert_invalidates_cached_values_from_the_replaced_state(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'split': 'train'}]))['ids'][0]
        version = await versions.create_version(1, versions.CreateVersionRequest(name='training'))
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'split': 'test'}), USER)
        await refresh(database)
        assert (await schema_field('split'))['values_complete'] is True
        await versions._restore_version(1, version['id'], restore_deleted=False, user=USER, exact_membership=False)
        split = await schema_field('split')
        assert not split['values_complete'] or 'train' in (split.get('values') or [])

    registry_database(scenario)


@pytest.mark.parametrize('later_batch', [False, True])
def test_nonstring_labels_cannot_make_an_incomplete_preview_complete(registry_database, later_batch):
    async def scenario(database):
        if later_batch:
            await ingest([{'detections': [{'label': 'cat'}]}])
            await ingest([{'detections': [{'label': 3}]}], prefix='later')
        else:
            await ingest([{'detections': [{'label': 'cat'}, {'label': 3}]}])
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        labels = next(field for field in fields if field['name'] == 'detections.label')
        assert not labels['values_complete'] or {'cat', '3'} <= set(labels['values'])

    registry_database(scenario)


def test_patch_and_ingest_lock_all_cache_rows_in_the_same_order(registry_database, monkeypatch):
    async def scenario(database):
        from backend import sample_ingest

        sample_id = (await ingest([{'color': 'old', 'detections': [{'label': 'old'}]}]))['ids'][0]
        ingest_ready = asyncio.Event()
        release_ingest = asyncio.Event()
        patch_ready = asyncio.Event()
        original_merge = merge_annotation_values
        original_invalidate = invalidate_values

        async def pause_ingest_after_labels(connection, dataset_id, metadata, inserted_fields):
            await original_merge(connection, dataset_id, metadata, inserted_fields)
            ingest_ready.set()
            await asyncio.wait_for(release_ingest.wait(), 3)

        async def lock_color_before_invalidating(connection, dataset_id, names):
            patch_ready.set()
            await connection.fetchrow(
                "SELECT field_name FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = 'color' FOR UPDATE",
                dataset_id,
            )
            await original_invalidate(connection, dataset_id, names)

        monkeypatch.setattr(sample_ingest, 'merge_annotation_values', pause_ingest_after_labels)
        monkeypatch.setattr(datasets, 'invalidate_values', lock_color_before_invalidating)
        inserting = asyncio.create_task(
            ingest([{'color': 'insert', 'detections': [{'label': 'insert'}]}], prefix='later'),
        )
        await asyncio.wait_for(ingest_ready.wait(), 3)
        patching = asyncio.create_task(
            datasets.update_sample(
                1,
                sample_id,
                datasets.SampleUpdate(metadata={'color': 'patch', 'detections': [{'label': 'patch'}]}),
                USER,
            ),
        )
        await asyncio.wait_for(patch_ready.wait(), 3)
        await asyncio.wait({patching}, timeout=0.1)
        release_ingest.set()
        await asyncio.wait_for(asyncio.gather(inserting, patching), 3)
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 1') == 2

    registry_database(scenario)


@pytest.mark.parametrize('surface', ['samples', 'embeddings', 'region', 'vector', 'patches', 'object_patches'])
def test_promoted_numeric_enum_choices_match_across_query_surfaces(registry_database, monkeypatch, surface):
    async def scenario(database):
        monkeypatch.setattr(search, 'db', database)
        monkeypatch.setattr(search, 'get_optional_user', AsyncMock(return_value=USER))
        monkeypatch.setattr(search, 'check_dataset_access', AsyncMock())
        request = Request({'type': 'http', 'method': 'POST', 'path': '/search', 'headers': []})
        sample_ids = (await ingest([{'level': 1.0}, {'level': 2.0}]))['ids']
        await database.execute(
            """INSERT INTO embeddings (dataset_id, sample_id, field_name, vector)
               SELECT (SELECT dataset_id FROM samples WHERE id = sample_id), sample_id, field_name, '[1,0]'::vector
               FROM unnest($1::integer[]) sample_id
               CROSS JOIN unnest(ARRAY['clip', 'clip_obj', 'clip_patch_2x2_r0c0']) field_name""",
            sample_ids,
        )
        await database.execute(
            """INSERT INTO embedding_coordinates
               SELECT sample_id, field_name, 0, 0 FROM unnest($1::integer[]) sample_id
               CROSS JOIN unnest(ARRAY['clip', 'clip_obj', 'clip_patch_2x2_r0c0']) field_name""",
            sample_ids,
        )
        await datasets.set_field_display_type(1, 'level', datasets.SetDisplayTypeRequest(display_type='enum'), USER)
        await datasets.promote_field(1, 'level', USER)
        choices = (await schema_field('level'))['values']
        assert len(choices) == 2
        for choice in choices:
            filters = {'field': 'level', 'operator': 'in', 'value': [choice]}
            seed = sample_ids[1] if float(choice) == 1.0 else sample_ids[0]
            if surface == 'samples':
                result = await datasets.list_samples_post(1, datasets.SamplesRequest(filters=filters))
            elif surface == 'embeddings':
                result = await embeddings.get_embeddings_post(
                    1,
                    embeddings.EmbeddingsRequest(filters=filters, sample_pct=100),
                )
            elif surface == 'region':
                result = await embeddings.select_region(
                    1,
                    embeddings.RegionSelectRequest(filters=filters, polygon=[[-1, -1], [1, -1], [1, 1], [-1, 1]]),
                )
            elif surface == 'vector':
                matches = await search.vector_search(
                    SearchRequest(dataset_id=1, vector=[1, 0], filters=filters),
                    request,
                )
                result = {'total': len(matches)}
            elif surface == 'patches':
                matches = await search.patch_search(
                    PatchSearchRequest(dataset_id=1, sample_id=seed, patch_scale='2x2', filters=filters),
                    request,
                )
                result = {'total': len(matches)}
            else:
                matches = await search.object_to_patch_search(
                    ObjectToPatchSearchRequest(dataset_id=1, object_sample_id=seed, filters=filters),
                    request,
                )
                result = {'total': len(matches)}
            assert result['total'] == 1

    registry_database(scenario)


@pytest.mark.parametrize('parent', ['detections', 'segmentation'])
def test_warm_annotation_ingest_does_not_count_the_whole_dataset(registry_database, parent):
    async def scenario(database):
        previous = {'labels': ['old']} if parent == 'segmentation' else [{'label': 'old'}]
        incoming = {'labels': ['new']} if parent == 'segmentation' else [{'label': 'new'}]
        await ingest([{parent: previous}] * 30)
        await datasets._get_dataset_field_stats_impl(1, None)
        async with database.pool.acquire() as connection:
            captured = []

            async def guarded_execute(query, *args):
                captured.append(query)
                assert 'from samples' not in query.lower()
                return await connection.execute(query, *args)

            async def guarded_fetchval(query, *args):
                captured.append(query)
                assert 'from samples' not in query.lower()
                return await connection.fetchval(query, *args)

            async def guarded_fetch(query, *args):
                captured.append(query)
                assert 'from samples' not in query.lower()
                return await connection.fetch(query, *args)

            async with connection.transaction():
                await merge_annotation_values(
                    SimpleNamespace(execute=guarded_execute, fetchval=guarded_fetchval, fetch=guarded_fetch),
                    1,
                    [{parent: incoming}],
                    set(),
                )
        assert captured

    registry_database(scenario)


@pytest.mark.parametrize('surface', ['values', 'autocomplete'])
def test_promoted_numeric_choices_scope_other_field_values(registry_database, surface):
    async def scenario(database):
        await ingest([{'level': 1.0, 'color': 'red'}, {'level': 2.0, 'color': 'blue'}])
        await datasets.set_field_display_type(1, 'level', datasets.SetDisplayTypeRequest(display_type='enum'), USER)
        await datasets.promote_field(1, 'level', USER)
        choice = next(value for value in (await schema_field('level'))['values'] if float(value) == 1.0)
        filters = {'field': 'level', 'operator': 'in', 'value': [choice]}
        if surface == 'values':
            values = await datasets.compute_filtered_values(1, 'color', filters)
        else:
            response = await datasets.autocomplete_field_post(1, 'color', datasets.AutocompleteRequest(filters=filters))
            values = [entry['value'] for entry in response['values']]
        assert values == ['red']

    registry_database(scenario)
