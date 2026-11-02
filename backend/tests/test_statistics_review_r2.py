"""Statistics remain incremental and preserve complete lists during continuous ingest."""

import os

import pytest

from backend import statistics, statistics_measure
from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import refresh_during, run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


@pytest.mark.parametrize('initial_tags', [[], ['existing']])
def test_continuous_ingest_preserves_complete_scalar_tag_and_native_lists(registry_database, initial_tags):
    async def scenario(database):
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)

        async def append_sample(number, color, tags):
            await datasets.add_samples(
                1,
                [
                    {
                        'filepath': f'continuous-{number}.jpg',
                        'metadata': {
                            'color': color,
                            'detections': [{'label': color}],
                            'segmentation': {'labels': [color]},
                        },
                        'tags': tags,
                        'array_fields': {'reviewed': tags},
                    },
                ],
                USER,
            )

        await append_sample(0, 'red', initial_tags)
        await run_refresh(database, 1)
        expected_colors = {'red'}
        expected_tags = set(initial_tags)
        for number, color in enumerate(['green', 'blue', 'red'], 1):
            tag = f'tag-{number}'
            await refresh_during(database, lambda: append_sample(number, color, [tag]))
            expected_colors.add(color)
            expected_tags.add(tag)
            assert await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1')
            cached = {
                row['field_name']: row
                for row in await database.fetch('SELECT * FROM dataset_field_stats WHERE dataset_id=1')
            }
            for name, expected in [
                ('color', expected_colors),
                ('detections.label', expected_colors),
                ('segmentation.labels', expected_colors),
                ('tags', expected_tags),
                ('reviewed', expected_tags),
            ]:
                assert set(cached[name]['unique_values']) == expected
                assert cached[name]['values_complete'] is True, name
            schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
            assert schema['color']['values_complete'] is True
            assert set(schema['tags']['values']) == expected_tags
            sidebar = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
            for name in ['color', 'detections.label', 'segmentation.labels', 'tags', 'reviewed']:
                assert sidebar[name]['values_complete'] is True, name
        await run_refresh(database, 1)
        assert (
            await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE dataset_id=1 AND field_name='color'",
            )
            == 4
        )

    registry_database(scenario)


def test_concurrent_ingest_cannot_certify_value_lists_that_exceed_their_cap(registry_database):
    async def scenario(database):
        await datasets.add_samples(
            1,
            [
                {
                    'filepath': f'initial-{number}.jpg',
                    'metadata': {
                        'color': f'color-{number:03}',
                        'detections': [{'label': f'label-{number:03}'}, {'label': f'label-{number + 100:03}'}],
                    },
                    'tags': [f'tag-{number:03}', f'tag-{number + 100:03}'],
                }
                for number in range(100)
            ],
            USER,
        )
        await run_refresh(database, 1)

        async def overflow():
            await datasets.add_samples(
                1,
                [
                    {
                        'filepath': 'overflow.jpg',
                        'metadata': {'color': 'overflow', 'detections': [{'label': 'overflow'}]},
                        'tags': ['overflow'],
                    },
                ],
                USER,
            )

        await refresh_during(database, overflow)
        for name, limit in [('color', 100), ('tags', 200), ('detections.label', 200)]:
            cached = await database.fetchrow(
                'SELECT * FROM dataset_field_stats WHERE dataset_id=1 AND field_name=$1',
                name,
            )
            assert cached['values_complete'] is False
            assert len(cached['unique_values'] or []) <= limit

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['ingest', 'patch'])
@pytest.mark.parametrize(
    'metadata,field_name',
    [({'new_field': 'new'}, 'new_field'), ({'detections': [{'label': 'new'}]}, 'detections.label')],
)
def test_first_seen_field_measures_only_changed_blocks(registry_database, monkeypatch, operation, metadata, field_name):
    async def scenario(database):
        sample_ids = []
        for block in range(4):
            await database.fetchval(
                "SELECT setval(pg_get_serial_sequence('samples','id'),$1,FALSE)",
                block * statistics_measure.BLOCK_SIZE + 1,
            )
            sample_ids.extend((await ingest([{'color': 'red', 'score': block}], prefix=f'block-{block}'))['ids'])
        await ingest([metadata], dataset_id=2, prefix='other-dataset')
        await run_refresh(database)
        previous = {
            row['block_id']: row['measured_snapshot']
            for row in await database.fetch('SELECT * FROM dataset_stats_parts WHERE dataset_id=1')
        }
        measured = []
        original = statistics_measure.measure_part

        async def measure(pool, dataset_id, block_id, part_id, split_level):
            measured.append((dataset_id, block_id))
            return await original(pool, dataset_id, block_id, part_id, split_level)

        monkeypatch.setattr(statistics_measure, 'measure_part', measure)
        if operation == 'ingest':
            await ingest([metadata], prefix='new-field')
            changed_block = 3
        else:
            await datasets.update_sample(1, sample_ids[0], datasets.SampleUpdate(metadata=metadata), USER)
            changed_block = 0
        job = await statistics.request_refresh(database.pool, 1)
        assert job['barrier']['full'] is False
        await run_refresh(database, 1)
        assert measured == [(1, changed_block)]
        for part in await database.fetch('SELECT * FROM dataset_stats_parts WHERE dataset_id=1'):
            if part['block_id'] != changed_block:
                assert part['measured_snapshot'] == previous[part['block_id']]
        field = await database.fetchrow(
            'SELECT * FROM dataset_field_stats WHERE dataset_id=1 AND field_name=$1',
            field_name,
        )
        assert field['sample_count'] == 1 and field['unique_values'] == ['new'] and field['values_complete']
        assert not await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1')

    registry_database(scenario)


def test_adopting_a_native_column_still_measures_its_preexisting_values(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}, {'score': 2}])
        await database.execute(
            "ALTER TABLE samples ADD COLUMN reviewed TEXT[]; "
            "INSERT INTO sample_array_columns VALUES ('reviewed'); "
            "UPDATE samples SET reviewed=ARRAY['legacy'] WHERE dataset_id=1",
        )
        await run_refresh(database, 1)
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
        job = await statistics.request_refresh(database.pool, 1)
        assert job['barrier']['full'] is True
        await run_refresh(database, 1)
        field = await database.fetchrow(
            "SELECT * FROM dataset_field_stats WHERE dataset_id=1 AND field_name='reviewed'",
        )
        assert field['sample_count'] == 2 and field['unique_values'] == ['legacy'] and field['values_complete']

    registry_database(scenario)
