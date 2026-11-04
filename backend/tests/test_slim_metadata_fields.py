"""Slim sample queries return the requested metadata keys with their JSON types, whatever the key."""

import os

import pytest

from backend.routers import datasets
from backend.tests.test_field_registry_postgres import registry_database  # noqa: F401

needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')

FIELDS = 'id,metadata.percent_CROP:CORN,metadata.score,metadata.flag,metadata.crops,metadata.absent'
FIRST_ROW = {'percent_CROP:CORN': 1.5, 'score': 1, 'flag': False, 'crops': ['CROP:CORN']}


async def _seed(database):
    await database.execute("""
        INSERT INTO samples (dataset_id, filepath, media_type, metadata)
        SELECT 1, 'sample-' || n, 'image',
               jsonb_build_object('percent_CROP:CORN', n * 1.5, 'score', n, 'flag', n % 2 = 0,
                                  'crops', jsonb_build_array('CROP:CORN'), 'other', 'not requested')
        FROM generate_series(1, 5) n;
    """)


def test_slim_select_binds_keys_and_keeps_json_types():
    clause = datasets._slim_metadata_select(4)

    assert 'unnest($4::text[])' in clause
    assert 'metadata->key' in clause
    assert '->>' not in clause


@needs_postgres
def test_slim_metadata_keeps_types_and_keys_with_colons(registry_database):  # noqa: F811
    async def scenario(database):
        await _seed(database)
        result = await datasets.list_samples_post(1, datasets.SamplesRequest(slim=True, fields=FIELDS, limit=2))
        assert result['total'] == 5
        assert [item['metadata'] for item in result['items']] == [
            FIRST_ROW,
            {'percent_CROP:CORN': 3.0, 'score': 2, 'flag': True, 'crops': ['CROP:CORN']},
        ]

    registry_database(scenario)


@needs_postgres
def test_slim_metadata_with_keyset_paging(registry_database):  # noqa: F811
    async def scenario(database):
        await _seed(database)
        first_id = await database.fetchval("SELECT min(id) FROM samples WHERE dataset_id = 1")
        result = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(slim=True, fields=FIELDS, limit=1, after_id=first_id - 1, skip_count=True),
        )
        assert [item['metadata'] for item in result['items']] == [FIRST_ROW]

    registry_database(scenario)
