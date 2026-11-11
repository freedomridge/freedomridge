"""The patch-vector import (backend/patch_import.py): Parquet files under an S3 prefix become a dataset's patch
fields, matched by filepath, one checkpointed batch of files per step."""

import io
import json
import os
from unittest.mock import AsyncMock

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import HTTPException

from backend import index_jobs, patch_import
from backend.routers import embeddings
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_job_runner import make_worker

registry_database = registry_helpers.registry_database
ingest, USER = registry_helpers.ingest, registry_helpers.USER
needs_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
VALUES = 4  # vector length, small for the test; the real ones have 512
BUCKET = 'patch-bucket'
PATH = 'patch-vectors/openai-clip-vit-base-patch32@rev/4x4/v1/'
PREFIX = f's3://{BUCKET}/{PATH}'
INFO = {'scale': '4x4', 'shape': [16, VALUES], 'dtype': 'float32', 'model': 'openai/clip-vit-base-patch32'}


class FakeS3:
    """list_objects_v2 and get_object over a dict of keys, answered as boto3 does."""

    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self, objects):
        self.objects = dict(objects)
        self.gets = []

    def list_objects_v2(self, Bucket, Prefix, StartAfter, MaxKeys):
        keys = sorted(key for key in self.objects if key.startswith(Prefix) and key > StartAfter)
        return {'Contents': [{'Key': key} for key in keys[:MaxKeys]], 'IsTruncated': len(keys) > MaxKeys}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        self.gets.append(Key)
        return {'Body': io.BytesIO(self.objects[Key])}


def tiles(seed):
    return np.random.default_rng(seed).standard_normal((16, VALUES)).astype(np.float32)


def parquet(rows, width=16 * VALUES, scale='4x4'):
    flat = np.concatenate([vectors.reshape(-1) for _, vectors in rows]) if rows else np.zeros(0, np.float32)
    table = pa.table(
        {
            'filepath': [filepath for filepath, _ in rows],
            'vectors': pa.FixedSizeListArray.from_arrays(pa.array(flat, type=pa.float32()), width),
            'scale': [scale] * len(rows),
        },
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression='none')
    return buffer.getvalue()


async def run_import(database, job_id, max_units=20):
    worker = await make_worker(
        database,
        {patch_import.KIND: patch_import.handler(database.pool)},
        policies={patch_import.KIND: patch_import.POLICY},
        placements=('server',),
        step_budget_seconds=0.001,
    )
    for _ in range(max_units):
        if await database.fetchval('SELECT state FROM jobs WHERE id=$1', job_id) in (
            'succeeded',
            'failed',
            'cancelled',
        ):
            break
        await database.execute('UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1', job_id)
        await worker.run_once()
    return dict(await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job_id))


@needs_database
def test_import_writes_every_tile_and_counts_what_it_cannot(monkeypatch, registry_database):
    async def scenario(database):
        await ingest([{}, {}, {}], prefix='s3://images/img')  # s3://images/img-0.jpg ... img-2.jpg
        ids = {row['filepath']: row['id'] for row in await database.fetch('SELECT id, filepath FROM samples')}
        fake = FakeS3(
            {
                PATH + 'info.json': json.dumps(INFO).encode(),
                PATH + 'a.parquet': parquet(
                    [
                        ('s3://images/img-0.jpg', tiles(0)),
                        ('s3://images/img-1.jpg', tiles(1)),
                        ('s3://images/gone.jpg', tiles(9)),
                    ],
                ),
                PATH + 'b.parquet': parquet([('s3://images/img-2.jpg', tiles(2))]),
                PATH + 'c-broken.parquet': b'not a parquet file',
                PATH + 'notes.txt': b'not an import file',
            },
        )
        monkeypatch.setattr(patch_import, 's3_client', AsyncMock(return_value=fake))
        monkeypatch.setattr(patch_import, 'require_free_space', AsyncMock())
        monkeypatch.setattr(patch_import, 'FILES_PER_STEP', 2)  # several steps, each resuming after the last

        answer = await embeddings.import_patch_vectors(1, embeddings.PatchVectorImport(prefix=PREFIX), user=USER)
        assert answer.status_code == 202
        job = await run_import(database, json.loads(answer.body)['job_id'])

        assert job['state'] == 'succeeded' and job['lane'] == 'maintenance'
        result = job['result']
        assert {key: result[key] for key in ('files', 'images', 'unmatched', 'bad_files')} == {
            'files': 3,
            'images': 3,
            'unmatched': 1,
            'bad_files': 1,
        }
        assert result['unmatched_examples'] == ['s3://images/gone.jpg']
        assert result['bad_examples'][0].startswith(f'{PREFIX}c-broken.parquet: not a readable Parquet file')
        assert result['fields'] == [f'clip_patch_4x4_r{row}c{col}' for row in range(4) for col in range(4)]
        assert result['info'] == INFO
        assert sorted(fake.gets) == sorted(set(fake.gets))  # each file read once across the steps

        for index in range(3):
            rows = await database.fetch(
                'SELECT field_name, vector::real[] AS vector FROM embeddings WHERE sample_id=$1 ORDER BY field_name',
                ids[f's3://images/img-{index}.jpg'],
            )
            stored = {row['field_name']: row['vector'] for row in rows}
            expected = tiles(index)
            assert len(stored) == 16
            for position, field in enumerate(result['fields']):
                np.testing.assert_array_equal(np.array(stored[field], dtype=np.float32), expected[position])

        # Each tile position's search index, queued as build_ann_index would.
        queued = await database.fetch("SELECT id, args->>'field' AS field FROM jobs WHERE kind=$1", index_jobs.ANN_KIND)
        assert sorted(row['field'] for row in queued) == sorted(result['fields'])
        assert sorted(result['index_jobs']) == sorted(str(row['id']) for row in queued)

    registry_database(scenario)


@needs_database
def test_refusals_come_before_anything_is_queued(monkeypatch, registry_database):
    async def scenario(database):
        async def refused(request, objects=None):
            if objects is not None:
                monkeypatch.setattr(patch_import, 's3_client', AsyncMock(return_value=FakeS3(objects)))
            with pytest.raises(HTTPException) as error:
                await embeddings.import_patch_vectors(1, request, user=USER)
            return error.value.status_code, str(error.value.detail)

        # No storage configuration serves the bucket to this dataset (the real lookup).
        status, detail = await refused(embeddings.PatchVectorImport(prefix=PREFIX))
        assert status == 403 and f's3://{BUCKET}' in detail
        assert (await refused(embeddings.PatchVectorImport(prefix='/local/path')))[0] == 400
        assert 'info.json is missing' in (await refused(embeddings.PatchVectorImport(prefix=PREFIX), {}))[1]
        for bad, reason in (
            ({'shape': [4, VALUES]}, 'shape'),
            ({'dtype': 'float16'}, 'dtype'),
            ({'scale': 'four'}, 'scale'),
        ):
            objects = {PATH + 'info.json': json.dumps({**INFO, **bad}).encode()}
            status, detail = await refused(embeddings.PatchVectorImport(prefix=PREFIX), objects)
            assert status == 400 and reason in detail
        # A tile field already holding vectors of another length.
        await ingest([{}], prefix='s3://images/old')
        sample = await database.fetchval('SELECT id FROM samples LIMIT 1')
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip_patch_4x4_r0c0', '[1,0,0]')",
            sample,
        )
        status, _ = await refused(
            embeddings.PatchVectorImport(prefix=PREFIX),
            {PATH + 'info.json': json.dumps(INFO).encode()},
        )
        assert status == 422
        assert await database.fetchval('SELECT count(*) FROM jobs WHERE kind=$1', patch_import.KIND) == 0

    registry_database(scenario)


@pytest.mark.parametrize(
    ('data', 'message'),
    [
        (parquet([('s3://images/a.jpg', tiles(0)[:8])], width=8 * VALUES), 'fixed_size_list<float32, 64>'),
        (parquet([('s3://images/a.jpg', tiles(0))], scale='2x2'), 'scale must be 4x4'),
        (parquet([('s3://images/a.jpg', np.full((16, VALUES), np.nan, np.float32))]), 'NaN'),
    ],
)
def test_a_file_that_does_not_match_its_prefix_is_refused(data, message):
    with pytest.raises(ValueError, match=message):
        patch_import.read_rows(data, '4x4', 16 * VALUES)


def test_rows_read_back_in_tile_order():
    filepaths, vectors = patch_import.read_rows(parquet([('s3://images/a.jpg', tiles(3))]), '4x4', 16 * VALUES)
    assert filepaths == ['s3://images/a.jpg']
    np.testing.assert_array_equal(vectors.reshape(16, VALUES), tiles(3))
