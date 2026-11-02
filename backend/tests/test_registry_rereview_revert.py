"""Real PostgreSQL regressions for stale, usable measurements after snapshot restore."""

import os

import pytest

from backend.routers import datasets, versions
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, definitions, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def cached_statistics(database):
    rows = await database.fetch('SELECT * FROM dataset_field_stats WHERE dataset_id = 1')
    return {row['field_name']: dict(row) for row in rows}


def test_revert_preserves_measured_ranges_without_claiming_categorical_completeness(registry_database):
    async def scenario(database):
        sample_ids = (
            await ingest(
                [
                    {'score': 1, 'captured': '2026-01-01T12:00:00Z', 'color': 'old-red'},
                    {'score': 3, 'captured': '2026-01-03T12:00:00Z', 'color': 'old-blue'},
                ],
            )
        )['ids']
        version = await versions.create_version(1, versions.CreateVersionRequest(name='saved-measurements'))
        for sample_id, score, captured in zip(
            sample_ids,
            [2, 4],
            ['2026-01-02T12:00:00Z', '2026-01-04T12:00:00Z'],
            strict=True,
        ):
            await datasets.update_sample(
                1,
                sample_id,
                datasets.SampleUpdate(metadata={'score': score, 'captured': captured, 'color': 'new', 'obsolete': 9}),
                USER,
            )
        await run_refresh(database, 1, force=True)
        before = await cached_statistics(database)
        assert before['score']['min_value'] == 2
        assert before['score']['max_value'] == 4
        assert len(before['captured']['unique_values']) == 2
        assert before['color']['values_complete'] is True

        await versions.revert_to_version(1, version['id'], user=USER)

        after = await cached_statistics(database)
        assert {'score', 'captured', 'color'} <= after.keys()
        assert 'obsolete' not in after
        assert 'obsolete' not in await definitions(database)
        for name in ['score', 'captured', 'color']:
            assert after[name]['values_complete'] is False
            assert after[name]['sample_count'] == before[name]['sample_count']
        assert after['score']['min_value'] == before['score']['min_value']
        assert after['score']['max_value'] == before['score']['max_value']
        assert after['captured']['unique_values'] == before['captured']['unique_values']
        assert after['color']['unique_values'] is None
        assert after['score']['unique_values'] is None
        state = await database.fetchrow('SELECT stats_need_refresh, stats_full_refreshed_at FROM datasets WHERE id = 1')
        assert state['stats_need_refresh'] is True
        assert state['stats_full_refreshed_at'] is None
        for response in [await datasets.get_dataset_schema(1), await datasets._get_dataset_field_stats_impl(1, None)]:
            fields = {field['name']: field for field in response}
            assert fields['score']['stats_pending'] is False
            assert fields['score']['min'] == 2
            assert fields['score']['max'] == 4
            assert fields['captured']['stats_pending'] is False
            assert fields['captured']['min'] == before['captured']['unique_values'][0]
            assert fields['captured']['max'] == before['captured']['unique_values'][-1]
            assert not fields['color'].get('values_complete', False)
            assert 'new' not in fields['color'].get('values', [])

    registry_database(scenario)


@pytest.mark.parametrize('exact_membership', [False, True])
def test_revert_invalidates_system_previews_and_keeps_only_owned_measurements(registry_database, exact_membership):
    async def scenario(database):
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
        await datasets.add_samples(
            1,
            {
                'samples': [
                    {
                        'filepath': 'sample.jpg',
                        'media_type': 'image',
                        'tags': ['old-tag'],
                        'array_fields': {'reviewed': ['old-review']},
                        'metadata': {'score': 1, 'detections': [{'label': 'old-label'}]},
                    },
                ],
            },
            user=USER,
        )
        version = await versions.create_version(1, versions.CreateVersionRequest(name='saved-system-values'))
        await datasets.add_samples(
            1,
            {
                'on_duplicate': 'update',
                'samples': [
                    {
                        'filepath': 'sample.jpg',
                        'media_type': 'video',
                        'tags': ['new-tag'],
                        'array_fields': {'reviewed': ['new-review']},
                        'metadata': {'score': 9, 'detections': [{'label': 'new-label'}]},
                    },
                ],
            },
            user=USER,
        )
        await ingest([{'later_only': 5}], prefix='later')
        await run_refresh(database, 1, force=True)
        await datasets._get_dataset_field_stats_impl(1, None)
        discovery_fields = [
            '__heatmap_fields__',
            '__mask_overlay_fields__',
            '__keypoint_fields__',
            '__classification_fields__',
            '__detection_fields__',
            '__polyline_fields__',
        ]
        for field_name in [*discovery_fields, 'unowned_metadata', 'polygons.label']:
            await database.execute(
                """INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, unique_values, values_complete)
                   VALUES (1, $1, 'string', ARRAY['obsolete'], TRUE)
                   ON CONFLICT (dataset_id, field_name) DO UPDATE SET unique_values = EXCLUDED.unique_values,
                       values_complete = TRUE""",
                field_name,
            )
        assert {'tags', 'reviewed', 'detections.label', 'media_type'} <= (await cached_statistics(database)).keys()

        await versions.revert_to_version(1, version['id'], user=USER, exact_membership=exact_membership)

        after = await cached_statistics(database)
        assert {'score', 'tags', 'reviewed', 'detections.label'} <= after.keys()
        for name in ['tags', 'reviewed', 'detections.label']:
            assert after[name]['unique_values'] is None
            assert after[name]['values_complete'] is False
        assert set(discovery_fields) <= after.keys()
        assert all(after[name]['unique_values'] == ['obsolete'] for name in discovery_fields)
        assert all(after[name]['values_complete'] for name in discovery_fields)
        assert 'media_type' not in after
        assert 'unowned_metadata' not in after
        assert 'polygons.label' not in after
        assert ('later_only' in after) is not exact_membership
        assert ('later_only' in await definitions(database)) is not exact_membership
        fields = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        assert 'obsolete' not in fields  # Registry ownership hides stale discovery members.
        assert fields['media_type']['values'] == ['image']
        assert fields['media_type']['values_complete'] is True
        assert fields['tags']['values'] == ['old-tag']
        assert fields['reviewed']['values'] == ['old-review']
        assert fields['detections.label']['values'] == ['old-label']

    registry_database(scenario)


@pytest.mark.parametrize('changed_value,changed_type', [(42, 'number'), ('2026-01-01T12:00:00Z', 'datetime')])
def test_revert_never_reinterprets_incompatible_measurements_as_complete_string_values(
    registry_database,
    changed_value,
    changed_type,
):
    async def scenario(database):
        sample_id = (await ingest([{'value': 'original-text'}]))['ids'][0]
        version = await versions.create_version(1, versions.CreateVersionRequest(name='saved-string-type'))
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'value': changed_value}), USER)
        await datasets.set_field_type(1, 'value', datasets.SetFieldTypeRequest(field_type=changed_type), USER)
        await run_refresh(database, 1, force=True)

        await versions.revert_to_version(1, version['id'], user=USER)

        after = await cached_statistics(database)
        assert 'value' in after
        assert after['value']['unique_values'] is None
        assert after['value']['values_complete'] is False
        fields = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        assert fields['value']['declared_type'] == 'string'
        assert fields['value']['stats_pending'] is False
        assert fields['value'].get('min') is None
        assert fields['value'].get('max') is None
        assert not fields['value'].get('values')

    registry_database(scenario)
