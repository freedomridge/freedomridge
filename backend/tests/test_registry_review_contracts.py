"""Adversarial field registry API contracts."""

import json
import os

import pytest
from fastapi import HTTPException

from backend.field_registry import FIELD_TYPES, field_stats
from backend.routers import datasets, embeddings
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest, registry_database

__all__ = ['registry_database']

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


@pytest.mark.parametrize('cached', [False, True])
def test_media_type_always_exposes_a_complete_checkbox_list(registry_database, cached):
    async def scenario(database):
        await datasets.add_samples(
            1,
            {
                'samples': [
                    {'filepath': 'image.jpg', 'media_type': 'image'},
                    {'filepath': 'video.mp4', 'media_type': 'video'},
                ],
            },
            user=USER,
        )
        if cached:
            await datasets._get_dataset_field_stats_impl(1, None)
            assert await database.fetchval(
                "SELECT EXISTS (SELECT 1 FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'media_type')",
            )
        else:
            await database.execute("DELETE FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'media_type'")
        fields = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        assert fields['media_type']['type'] == 'enum'
        assert fields['media_type']['values_complete'] is True
        assert set(fields['media_type']['values']) == {'image', 'video'}

    registry_database(scenario)


@pytest.mark.parametrize('prewarm', [False, True])
def test_media_type_never_claims_a_truncated_ingest_preview_is_complete(registry_database, prewarm):
    async def scenario(database):
        await datasets.add_samples(
            1,
            {'samples': [{'filepath': 'original.jpg', 'media_type': 'image'}]},
            user=USER,
        )
        if prewarm:
            await datasets._get_dataset_field_stats_impl(1, None)
        expected = {'image'} | {f'custom-{index:03d}' for index in range(250)}
        await datasets.add_samples(
            1,
            {'samples': [{'filepath': f'{kind}.data', 'media_type': kind} for kind in sorted(expected - {'image'})]},
            user=USER,
        )
        if prewarm:
            truncated = await database.fetchrow(
                "SELECT * FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'media_type'",
            )
            assert len(truncated['unique_values']) == 200
            assert truncated['values_complete'] is False

        fields = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}

        assert fields['media_type']['type'] == 'enum'
        assert set(fields['media_type']['values']) == expected
        cache = await database.fetchrow(
            "SELECT * FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'media_type'",
        )
        assert cache['values_complete'] is True
        assert set(cache['unique_values']) == expected
        assert fields['media_type']['values_complete'] is True
        again = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        assert again['media_type'] == fields['media_type']

    registry_database(scenario)


def test_system_labels_have_canonical_declared_type_and_exact_values(registry_database):
    async def scenario(database):
        await ingest([{'detections': [{'label': 'car'}, {'label': 'person'}]}])
        await database.execute("""INSERT INTO dataset_field_stats
            (dataset_id, field_name, field_type, unique_values, values_complete)
            VALUES (1, 'detections.label', 'enum', ARRAY['car', 'person'], FALSE)
            ON CONFLICT (dataset_id, field_name) DO UPDATE SET
                field_type = 'enum', unique_values = ARRAY['car', 'person'], values_complete = FALSE""")
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        label = schema['detections.label']
        assert label['declared_type'] in FIELD_TYPES
        assert label['type'] == 'enum'
        assert set(label['values']) == {'car', 'person'}

    registry_database(scenario)


def test_cleared_annotation_parent_cannot_reappear_as_cached_schema_labels(registry_database):
    async def scenario(database):
        await ingest([{'detections': [{'label': 'car'}], 'segmentation': {'labels': ['person']}}])
        await datasets._get_dataset_field_stats_impl(1, None)
        before = {field['name'] for field in await datasets.get_dataset_schema(1)}
        assert {'detections.label', 'segmentation.labels'} <= before
        await datasets.clear_field(1, 'detections', user=USER)
        await datasets.clear_field(1, 'segmentation', user=USER)
        for response in [await datasets.get_dataset_schema(1), await datasets._get_dataset_field_stats_impl(1, None)]:
            names = {field['name'] for field in response}
            assert not {'detections', 'detections.label', 'segmentation', 'segmentation.labels'} & names

    registry_database(scenario)


def test_measured_type_mismatch_is_not_pending_and_cannot_supply_bounds(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}, {'score': 2}])
        await database.execute("""UPDATE dataset_field_stats SET field_type = 'string',
            min_value = 999, max_value = 1000, unique_values = ARRAY['wrong']
            WHERE dataset_id = 1 AND field_name = 'score'""")
        measured = {field['field_name']: field for field in await field_stats(database, 1)}['score']
        assert measured['stats_pending'] is False
        assert measured['min_value'] is None and measured['max_value'] is None
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert schema['score']['declared_type'] == 'number'
        assert schema['score']['stats_pending'] is False

    registry_database(scenario)


def test_native_arrays_without_measurements_are_not_pending(registry_database):
    async def scenario(database):
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='review_labels'), user=USER)
        measured = {field['field_name']: field for field in await field_stats(database, 1)}['review_labels']
        assert measured['stats_pending'] is False
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert schema['review_labels']['stats_pending'] is False

    registry_database(scenario)


def test_owned_inferred_tags_schema_preserves_values_and_promotion(registry_database):
    async def scenario(database):
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='inferred_tags'), user=USER)
        await datasets.add_samples(
            1,
            {'samples': [{'filepath': 'native.jpg', 'array_fields': {'inferred_tags': ['car', 'person']}}]},
            user=USER,
        )
        await database.execute("""INSERT INTO dataset_field_stats
            (dataset_id, field_name, field_type, unique_values, values_complete)
            VALUES (1, 'inferred_tags', 'enum', ARRAY['car', 'person'], TRUE)""")
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        field = schema['inferred_tags']
        assert field['declared_type'] == 'list'
        assert field['origin'] == 'native_array'
        assert field['type'] in {'enum', 'array'}
        assert set(field['values']) == {'car', 'person'}
        assert field['is_promoted'] is True
        assert field['stats_pending'] is False

    registry_database(scenario)


@pytest.mark.parametrize('foreign_column_exists', [False, True])
def test_inferred_tags_scatter_uses_metadata_unless_dataset_owns_column(registry_database, foreign_column_exists):
    async def scenario(database):
        if foreign_column_exists:
            await datasets.create_array_field(
                2,
                datasets.CreateArrayFieldRequest(field_name='inferred_tags'),
                user=USER,
            )
        sample_id = (await ingest([{'inferred_tags': ['metadata-label']}]))['ids'][0]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[1,0]')",
            sample_id,
        )
        await database.execute("INSERT INTO embedding_coordinates VALUES ($1, 'clip', 0, 0)", sample_id)
        response = await embeddings.get_embeddings_post(
            1,
            embeddings.EmbeddingsRequest(include_fields=['inferred_tags'], sample_pct=100),
        )
        assert 'metadata-label' in json.dumps(response)
        assert 'inferred_tags' not in await embeddings._array_field_names(1)
        assert 'inferred_tags' not in await datasets.get_dataset_array_columns(1)
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert schema['inferred_tags']['origin'] == 'metadata'
        assert schema['inferred_tags']['declared_type'] == 'list'

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['ingest', 'patch'])
def test_unowned_array_writes_are_rejected_without_partial_changes(registry_database, operation):
    async def scenario(database):
        await datasets.create_array_field(2, datasets.CreateArrayFieldRequest(field_name='foreign_labels'), user=USER)
        sample_id = (await ingest([{'score': 1}]))['ids'][0]
        with pytest.raises(HTTPException) as error:
            if operation == 'ingest':
                await datasets.add_samples(
                    1,
                    {'samples': [{'filepath': 'rejected.jpg', 'array_fields': {'foreign_labels': ['lost']}}]},
                    user=USER,
                )
            else:
                await datasets.update_sample(
                    1,
                    sample_id,
                    datasets.SampleUpdate(metadata={'score': 2}, array_fields={'foreign_labels': ['lost']}),
                    user=USER,
                )
        assert error.value.status_code == 422
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 1') == 1
        assert await database.fetchval('SELECT metadata FROM samples WHERE id = $1', sample_id) == {'score': 1}

    registry_database(scenario)


def test_declared_number_histogram_rejects_later_overflow_strings_without_error(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}, {'score': 2}])
        await ingest([{'score': '0347677e05719710'}, {'score': 'not-a-number'}], prefix='later')
        histogram = await datasets.get_histogram(1, 'score', bins=5, top=50)
        assert histogram['total'] == 4
        assert sum(bucket['count'] for bucket in histogram['buckets']) == 2
        assert histogram['missing'] == 2

    registry_database(scenario)


@pytest.mark.parametrize('numeric_values', [[1, 2, 3], [1.0, 2.0, 1.5]])
@pytest.mark.parametrize('refresh', [False, True])
def test_numeric_enum_values_round_trip_through_exact_filters(registry_database, numeric_values, refresh):
    async def scenario(database):
        await ingest([{'level': value} for value in numeric_values])
        response = await datasets.set_field_display_type(
            1,
            'level',
            datasets.SetDisplayTypeRequest(display_type='enum'),
            user=USER,
        )
        assert len(response['unique_values']) == len(set(numeric_values))
        if refresh:
            await database.execute('UPDATE datasets SET stats_need_refresh = TRUE, stats_full_refreshed_at = NULL')
            results = await run_refresh(database)
            assert all(result['error'] is None for result in results)
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        sidebar = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        for advertised in [response['unique_values'], schema['level']['values'], sidebar['level']['values']]:
            matched: set[int] = set()
            for value in advertised:
                samples = await datasets.list_samples_post(
                    1,
                    datasets.SamplesRequest(filters={'field': 'level', 'operator': 'in', 'value': [value]}),
                )
                assert samples['total'] > 0, f'Advertised enum value {value!r} matched no samples'
                matched.update(sample['id'] for sample in samples['items'])
            assert len(matched) == len(numeric_values)

    registry_database(scenario)


def test_numeric_enum_later_ingest_uses_jsonb_text_not_python_number_format(registry_database):
    async def scenario(database):
        await ingest([{'level': 1}])
        await datasets.set_field_display_type(
            1,
            'level',
            datasets.SetDisplayTypeRequest(display_type='enum'),
            user=USER,
        )
        await ingest([{'level': 1e-7}, {'level': 1e20}, {'level': -0.0}], prefix='later')
        actual_values = set(
            await database.fetchval(
                "SELECT array_agg(DISTINCT metadata->>'level') FROM samples WHERE dataset_id = 1",
            ),
        )
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        field = schema['level']
        assert set(field['values']) <= actual_values
        if field['values_complete']:
            assert set(field['values']) == actual_values
        for value in field['values']:
            samples = await datasets.list_samples_post(
                1,
                datasets.SamplesRequest(filters={'field': 'level', 'operator': 'in', 'value': [value]}),
            )
            assert samples['total'] > 0

    registry_database(scenario)
