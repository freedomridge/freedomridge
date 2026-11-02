"""Large, low-cardinality source datasets must retain exact categorical selection."""

import os
from unittest.mock import AsyncMock

import pytest

from backend.field_registry import cached_distinct_values
from backend.routers import datasets
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


def test_large_source_dataset_keeps_exact_categorical_selection(registry_database):
    async def scenario(database):
        await database.execute("""INSERT INTO dataset_fields(dataset_id, field_name, field_type)
            VALUES (1, 'color', 'string')""")
        await database.execute('ALTER TABLE samples DISABLE TRIGGER USER')
        await database.execute("""INSERT INTO samples (dataset_id, filepath, media_type, metadata)
            SELECT 1, 'scale-' || position || '.jpg', 'image',
                   jsonb_build_object('color', (ARRAY['red', 'reddish', 'infrared', 'blue', 'green'])[position % 5 + 1])
            FROM generate_series(1, 111000) position""")
        await database.execute('ALTER TABLE samples ENABLE TRIGGER USER')
        await database.execute('UPDATE datasets SET stats_need_refresh = TRUE WHERE id = 1')
        refreshed = await run_refresh(database)
        assert all(row['error'] is None for row in refreshed)
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert schema['color']['type'] == 'enum'
        assert schema['color']['values_complete'] is True
        assert set(schema['color']['values']) == {'red', 'reddish', 'infrared', 'blue', 'green'}
        result = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters={'field': 'color', 'operator': 'in', 'value': ['red']}),
        )
        assert result['total'] == 22200

    registry_database(scenario)


def test_complete_empty_lists_do_not_rescan_until_a_writer_changes_them(registry_database, monkeypatch):
    async def scenario(database):
        sample_id = (await ingest([{'detections': [], 'segmentation': {'labels': []}}]))['ids'][0]
        await datasets._get_dataset_field_stats_impl(1, None)
        measure = AsyncMock(wraps=cached_distinct_values)
        monkeypatch.setattr(datasets, 'cached_distinct_values', measure)
        await datasets._get_dataset_field_stats_impl(1, None)
        assert measure.await_count == 0
        await datasets.update_sample(
            1,
            sample_id,
            datasets.SampleUpdate(tags=['new'], metadata={'detections': [{'label': 'car'}]}),
            USER,
        )
        fields = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
        assert fields['tags']['values'] == ['new']
        assert fields['detections.label']['values'] == ['car']

    registry_database(scenario)
