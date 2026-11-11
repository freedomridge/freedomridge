"""Coverage presence filters compose with dataset-owned native and JSON list fields."""

import os

import pytest

from backend.routers import datasets, embeddings
from backend.tests.test_field_registry_postgres import USER, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


@pytest.mark.parametrize('derived_field', ['embeddings.clip', 'hashes.phash', 'location'])
@pytest.mark.parametrize('operator', ['exists', 'not_exists'])
def test_coverage_presence_composes_with_scoped_array_filters(registry_database, derived_field, operator):
    async def scenario(database):
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='native_labels'), USER)
        await datasets.create_array_field(2, datasets.CreateArrayFieldRequest(field_name='inferred_tags'), USER)
        result = await datasets.add_samples(
            1,
            {
                'samples': [
                    {
                        'filepath': f'coverage-{position}.jpg',
                        'metadata': {'inferred_tags': ['json-label']},
                        'array_fields': {'native_labels': ['keep' if position < 2 else 'exclude']},
                    }
                    for position in range(3)
                ],
            },
            USER,
        )
        first, second, _ = result['ids']
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[1,0]')",
            first,
        )
        await database.execute("INSERT INTO embedding_coordinates VALUES ($1, 'clip', 0, 0)", first)
        await database.execute(
            """INSERT INTO embeddings (dataset_id, sample_id, field_name, vector)
               SELECT (SELECT dataset_id FROM samples WHERE id = sample_id), sample_id, 'view', '[1,0]'::vector
               FROM unnest($1::integer[]) sample_id""",
            result['ids'],
        )
        await database.execute(
            "INSERT INTO embedding_coordinates SELECT sample_id, 'view', 0, 0 FROM unnest($1::integer[]) sample_id",
            result['ids'],
        )
        await database.execute(
            "INSERT INTO sample_hashes (sample_id, field_name, hash_hex) VALUES ($1, 'phash', 'aa')",
            first,
        )
        await database.execute(
            'UPDATE samples SET location = ST_SetSRID(ST_MakePoint(1, 1), 4326) WHERE id = $1',
            first,
        )
        filters = {
            'logic': 'AND',
            'filters': [
                {'field': derived_field, 'operator': operator},
                {'field': 'native_labels', 'operator': 'in', 'value': ['keep']},
                {'field': 'inferred_tags', 'operator': 'exists'},
            ],
        }
        expected = first if operator == 'exists' else second
        samples = await datasets.list_samples_post(1, datasets.SamplesRequest(filters=filters))
        assert [sample['id'] for sample in samples['items']] == [expected]
        scatter = await embeddings.get_embeddings_post(
            1,
            embeddings.EmbeddingsRequest(field='view', filters=filters, sample_pct=100),
        )
        assert scatter['total'] == 1
        assert [point['sample_id'] for point in scatter['points']] == [expected]

    registry_database(scenario)
