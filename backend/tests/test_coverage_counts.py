"""/coverage's embedding counts: samples carrying each field, a patch field's tiles folded into one entry."""

import os

import pytest

from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_field_registry_postgres import ingest

registry_database = test_field_registry_postgres.registry_database

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


def test_coverage_counts_samples_per_field_and_folds_patch_tiles(registry_database):
    async def scenario(database):
        whole, partial, other = (await ingest([{}, {}, {}]))['ids']
        stored = [(whole, 'clip'), (partial, 'clip')]
        stored += [(whole, f'clip_patch_2x2_r{r}c{c}') for r in (0, 1) for c in (0, 1)]  # every tile
        stored += [(partial, 'clip_patch_2x2_r1c1')]  # one tile: still a sample carrying the patch field
        stored += [(other, 'odd_patch_2x2_rx')]  # named like a tile but isn't one: a field of its own
        stored += [(other, 'clip_patch_2x2')]  # the patch field's own name: folded with its tiles, as before
        for sample_id, field in stored:
            await database.execute(
                "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES (1, $1, $2, '[1,0]')",
                sample_id,
                field,
            )

        assert await datasets._embedding_coverage(1) == [
            ('clip', 2),
            ('clip_patch_2x2', 3),
            ('odd_patch_2x2_rx', 1),
        ]
        assert await datasets._embedding_coverage(2) == []  # another dataset's partition holds none of them

    registry_database(scenario)
