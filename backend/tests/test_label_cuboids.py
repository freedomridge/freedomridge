"""Cuboids use overlay discovery and scalar object rows, never classification facets."""

import os

import pytest

from backend.routers import datasets, versions
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable Postgres')
CUBOID = {
    '_type': 'cuboid',
    'id': 'box',
    'label': 'Car',
    'center': [0, 0, 10],
    'size': [4, 2, 2],
    'yaw': 0,
    'frame': 'camera',
}


async def cache(database, name):
    return await database.fetchval(
        'SELECT unique_values FROM dataset_field_stats WHERE dataset_id=1 AND field_name=$1',
        name,
    )


async def assert_overlay():
    assert [f['type'] for f in await datasets.get_dataset_schema(1) if f['name'] == 'boxes'] == ['cuboids']
    fields = await datasets.get_dataset_field_stats(1)
    assert [f['type'] for f in fields if f['name'] == 'boxes'] == ['cuboids']
    assert not any(f['name'] == 'boxes.label' for f in fields)


def test_discovery_ingest_patch_and_classification_conflict(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'boxes': [{'label': 'Car'}]}]))['ids'][0]
        await datasets.get_dataset_field_stats(1)
        assert await cache(database, '__classification_fields__') == ['boxes']
        assert await cache(database, '__cuboid_fields__') == []

        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'boxes': [CUBOID]}), USER)
        assert await cache(database, '__cuboid_fields__') == ['boxes']
        # Discovery is append-only; statistics owns removals.
        assert await cache(database, '__classification_fields__') == ['boxes']
        await run_refresh(database)
        await assert_overlay()
        assert await cache(database, '__classification_fields__') == []
        assert await cache(database, '__cuboid_fields__') == ['boxes']

        await ingest([{'other_boxes': [CUBOID]}], prefix='new')
        assert await cache(database, '__cuboid_fields__') == ['boxes', 'other_boxes']
        await datasets.get_dataset_field_stats(1)
        assert await cache(database, '__cuboid_fields__') == ['boxes', 'other_boxes']

        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'boxes': [{'label': 'Car'}]}), USER)
        assert await cache(database, '__classification_fields__') == ['boxes']
        assert await cache(database, '__cuboid_fields__') == ['boxes', 'other_boxes']
        await datasets.get_dataset_field_stats(1)
        assert await cache(database, '__classification_fields__') == ['boxes']
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'boxes': [CUBOID]}), USER)
        await assert_overlay()
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(remove_metadata_keys=['boxes']), USER)
        assert await cache(database, '__cuboid_fields__') == ['boxes', 'other_boxes']
        assert not any(f['name'] == 'boxes' for f in await datasets.get_dataset_field_stats(1))

    registry_database(scenario)


def test_statistics_publish_and_restore_preserves_discovery(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'boxes': [CUBOID], 'category': {'label': 'scene'}, 'single_box': CUBOID}]))['ids'][
            0
        ]
        await run_refresh(database)
        assert await cache(database, '__cuboid_fields__') == ['boxes']
        assert await cache(database, '__classification_fields__') == ['category']
        await assert_overlay()
        version = await versions.create_version(1, versions.CreateVersionRequest(name='cuboids'))
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'boxes': []}), USER)
        await run_refresh(database)
        assert not await cache(database, '__cuboid_fields__')
        await datasets.get_dataset_field_stats(1)
        assert await cache(database, '__cuboid_fields__') == []
        await versions._restore_version(1, version['id'], False, USER, False)
        assert await cache(database, '__cuboid_fields__') == []
        await run_refresh(database)
        await assert_overlay()

    registry_database(scenario)


def test_cuboid_objects_are_filterable_rows_in_the_parent_dataset(registry_database, tmp_path):
    from fridge import Calibration, Cuboid, objects_from_cuboids
    from PIL import Image

    async def scenario(database):
        calib = Calibration([100, 0, 50, 0, 0, 100, 40, 0, 0, 0, 1, 0], [100, 80])
        boxes = [
            Cuboid('Car', [0, 0, 8], [4, 2, 2], 0, occluded=1, difficulty='moderate'),
            Cuboid('Person', [1, 0, 20], [1, 1, 2], 0, occluded=2, difficulty='hard'),
        ]
        parent_id = (await ingest([{'boxes': boxes, 'calibration': calib}]))['ids'][0]
        objects = objects_from_cuboids(
            parent_id,
            'sample-0.jpg',
            Image.new('RGB', (100, 80)),
            boxes,
            calib,
            tmp_path,
            location=(-122.42, 37.77),
            field='boxes',
        )
        result = await datasets.add_samples(1, {'samples': [row.to_dict() for row in objects]}, user=USER)
        assert len(result['ids']) == 2
        await run_refresh(database)
        fields = {f['name']: f for f in await datasets.get_dataset_field_stats(1)}
        assert fields['label']['type'] == fields['difficulty']['type'] == 'enum'
        assert fields['distance_m']['type'] == fields['length_m']['type'] == 'number'
        assert fields['calibration']['declared_type'] == 'object'
        await assert_overlay()
        for label, expected in [('Car', [parent_id]), ('Person', [])]:
            response = await datasets.list_samples_post(
                1,
                datasets.SamplesRequest(
                    filters={
                        'field': '_cg',
                        'operator': 'has_matching_object',
                        'value': {
                            'logic': 'AND',
                            'filters': [
                                {'field': 'label', 'operator': '=', 'value': label},
                                {'field': 'distance_m', 'operator': '<=', 'value': 10},
                            ],
                        },
                    },
                ),
            )
            assert [row['id'] for row in response['items']] == expected
        response = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(
                filters={
                    'field': 'media_type',
                    'operator': '=',
                    'value': 'object',
                },
            ),
        )
        assert response['total'] == 2
        assert {row['metadata']['cuboid_id'] for row in response['items']} == {box['id'] for box in boxes}

    registry_database(scenario)
