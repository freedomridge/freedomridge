"""Uniqueness and near-duplicate scores look for a sample's neighbours in its own dataset (0021). Before, a field's
vectors and its HNSW index held every dataset that had the field, and the neighbour search had no dataset condition:
a sample whose twin was in another dataset scored as a duplicate of it, and was paired with it."""

import os

import pytest

from backend.routers import quality
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = registry_helpers.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def seed(database):
    """Dataset 1: three vectors far from each other. Dataset 2: a twin of each of them."""
    own = (await ingest([{}, {}, {}]))['ids']
    other = (await ingest([{}, {}, {}], dataset_id=2, prefix='other'))['ids']
    vectors = ['[1,0,0]', '[0,1,0]', '[0,0,1]']
    for ids in (own, other):
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
            "SELECT s.dataset_id, s.id, 'clip', t.vector::vector FROM unnest($1::integer[], $2::text[]) "
            "AS t(sample_id, vector) JOIN samples s ON s.id = t.sample_id",
            ids,
            vectors,
        )
    return own, other


def test_a_twin_in_another_dataset_is_not_a_neighbour(registry_database):
    async def scenario(database):
        own, other = await seed(database)

        await quality.uniqueness_compute(
            1,
            quality.UniquenessRequest(field='clip', k=1, allow_seq_scan=True),
            user=USER,
        )
        scores = await database.fetch(
            "SELECT (metadata->>'uniqueness')::float AS raw FROM samples WHERE dataset_id = 1",
        )
        # Orthogonal vectors are at cosine distance 1; the twin in dataset 2 is at 0.
        assert [round(row['raw'], 6) for row in scores] == [1.0, 1.0, 1.0]
        assert (
            await database.fetchval("SELECT count(*) FROM samples WHERE dataset_id = 2 AND metadata ? 'uniqueness'")
            == 0
        )

        answer = await quality.duplicates_compute(
            1,
            quality.DuplicatesComputeRequest(field='clip', thresh=0.04, k=5, token='t', allow_seq_scan=True),
            user=USER,
        )
        assert answer['pairs_written'] == 0
        assert await database.fetchval("SELECT count(*) FROM quality_dup_pairs WHERE b = ANY($1::int[])", other) == 0

        # A twin inside the dataset is still found.
        twin = (await ingest([{}], prefix='twin'))['ids'][0]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES (1, $1, 'clip', '[1,0,0]')",
            twin,
        )
        answer = await quality.duplicates_compute(
            1,
            quality.DuplicatesComputeRequest(field='clip', thresh=0.04, k=5, token='u', allow_seq_scan=True),
            user=USER,
        )
        pairs = await database.fetch("SELECT a, b FROM quality_dup_pairs WHERE token = 'u' ORDER BY a")
        assert sorted((row['a'], row['b']) for row in pairs) == sorted([(own[0], twin), (twin, own[0])])

    registry_database(scenario)
