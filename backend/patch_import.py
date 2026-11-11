"""Import patch vectors computed elsewhere, such as by ``fridge.compute_image_patch_vectors`` in a batch job next to
the images, from Parquet files under an S3 prefix.

A prefix holds:

- ``info.json``, written once, describing the vectors: ``scale`` (``"4x4"``), ``shape`` (``[16, 512]``: tiles,
  then values) and ``dtype`` (``"float32"``) are required; anything else (``model``, ``revision``,
  ``crop_version``, ``sdk_version``) is kept in the job's result.
- Parquet files (``*.parquet``, one per image or many images each) with the columns ``filepath``, the
  sample's full ``s3://bucket/key``, ``vectors``, a ``fixed_size_list<float32, tiles × values>`` in tile order
  r0c0, r0c1, ..., and ``scale``.

The job lists the files after its checkpoint in key order, one batch per step, downloads them, matches
each row's filepath to an image sample of the dataset, and writes its tiles to the fields
``<field>_patch_<scale>_r{row}c{col}`` with a binary COPY and one upsert. The writes commit with the
checkpoint, so a resumed job repeats nothing. Rows whose filepath matches no image sample, and files that
can't be read, are counted (with a few examples) rather than failing the import.

It runs in the maintenance lane: an import of millions of images takes hours, and the user lane (bulk
edits) stays free. The S3 credentials come from the storage configuration that serves the bucket to this
dataset (``storage.route_s3_bucket``), which the job runner reads itself.
"""

import asyncio
import io
import json
import re
import struct
from concurrent.futures import ThreadPoolExecutor

import asyncpg
import numpy as np
from fastapi import HTTPException

from . import index_jobs, job_queue, storage
from .diskspace import INGEST_ROW_BYTES, require_free_space
from .field_registry import lock_field_lifecycle
from .job_runner import StepResult
from .job_wait import JobInputError

KIND = 'patch_vector_import'
POLICY = job_queue.JobPolicy(max_unit_seconds=300, max_failures=5, retry_seconds=10, max_retry_seconds=600)
FILES_PER_STEP = 500  # S3 returns at most 1000 keys a page
DOWNLOAD_THREADS = 32
EXAMPLES = 5
LOCK_SECONDS = 5

_SCALE_RE = re.compile(r'^(\d+)x(\d+)$')

_UPSERT = """
    INSERT INTO embeddings (dataset_id, sample_id, vector, field_name)
    SELECT $4, sample_id, vector, $1 || '_patch_' || $2 || '_r' || (position / $3) || 'c' || (position % $3)
    FROM patch_import_stage
    ON CONFLICT (dataset_id, sample_id, field_name)
    DO UPDATE SET vector = EXCLUDED.vector
    WHERE embeddings.vector IS DISTINCT FROM EXCLUDED.vector
"""


def parse_prefix(prefix: str) -> tuple:
    """``s3://bucket/some/path/`` → ``('bucket', 'some/path/')``; the path always ends with '/'."""
    match = re.match(r'^s3://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])/(.*)$', prefix or '')
    if not match:
        raise HTTPException(status_code=400, detail=f'Give the prefix as s3://bucket/path/, not {prefix!r}')
    bucket, path = match.groups()
    return bucket, path if not path or path.endswith('/') else path + '/'


def patch_fields(field_name: str, scale: str) -> list:
    """The stored fields of a scale's tiles, in tile order: clip_patch_4x4_r0c0, clip_patch_4x4_r0c1, ..."""
    rows, cols = (int(part) for part in _SCALE_RE.match(scale).groups())  # type: ignore[union-attr]
    return [f'{field_name}_patch_{scale}_r{row}c{col}' for row in range(rows) for col in range(cols)]


async def s3_client(source, dataset_id: int, bucket: str):
    """The S3 client of the storage configuration that serves ``bucket`` to this dataset; 403 if none does."""
    route = await storage.route_s3_bucket(dataset_id, bucket, source)
    backend = await storage.backend_for_route(route, source) if route is not None else None
    if backend is None:
        raise HTTPException(
            status_code=403,
            detail=f'No storage configuration serves s3://{bucket} to this dataset (Settings → Storage Configuration)',
        )
    return backend.s3_client


def read_info(client, bucket: str, path: str) -> dict:
    """The prefix's info.json, checked: a scale, a [tiles, values] shape that fits it, and float32."""
    try:
        info = json.loads(client.get_object(Bucket=bucket, Key=path + 'info.json')['Body'].read())
    except client.exceptions.NoSuchKey:
        raise HTTPException(status_code=400, detail=f's3://{bucket}/{path}info.json is missing')
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f's3://{bucket}/{path}info.json is not JSON: {error}')
    scale, shape = info.get('scale'), info.get('shape')
    match = _SCALE_RE.match(scale) if isinstance(scale, str) else None
    if match is None:
        raise HTTPException(status_code=400, detail=f'info.json: scale must be like "4x4", not {scale!r}')
    tiles = int(match.group(1)) * int(match.group(2))
    if not (
        isinstance(shape, list) and len(shape) == 2 and shape[0] == tiles and isinstance(shape[1], int) and shape[1] > 0
    ):
        raise HTTPException(
            status_code=400,
            detail=f'info.json: shape must be [{tiles}, values] for {scale}, not {shape!r}',
        )
    if info.get('dtype') != 'float32':
        raise HTTPException(status_code=400, detail=f'info.json: dtype must be "float32", not {info.get("dtype")!r}')
    return info


async def check_prefix(pool, dataset_id: int, prefix: str) -> tuple:
    """A prefix this dataset may read, with a valid info.json: ``(normalised prefix, info)``. 400/403 otherwise."""
    bucket, path = parse_prefix(prefix)
    client = await s3_client(pool, dataset_id, bucket)
    return f's3://{bucket}/{path}', await asyncio.to_thread(read_info, client, bucket, path)


async def enqueue(pool, dataset_id: int, user_id: int, prefix: str, info: dict, field_name: str, build_indexes: bool):
    """Queue the import of a checked prefix, or join the same prefix and field's import waiting to start."""
    args = {
        'prefix': prefix,
        'field_name': field_name,
        'scale': info['scale'],
        'values': info['shape'][1],
        'build_indexes': bool(build_indexes),
        'info': info,
    }
    return await job_queue.enqueue_job(
        pool,
        KIND,
        dataset_id=dataset_id,
        created_by=user_id,
        lane='maintenance',
        args=args,
        dedupe_key=f'{prefix}|{field_name}',
    )


def _list_page(client, bucket: str, path: str, after: str) -> tuple:
    """The next keys after ``after`` (at most FILES_PER_STEP), and whether more follow."""
    page = client.list_objects_v2(Bucket=bucket, Prefix=path, StartAfter=after or path, MaxKeys=FILES_PER_STEP)
    return [item['Key'] for item in page.get('Contents', [])], bool(page.get('IsTruncated'))


def _download(client, bucket: str, keys: list) -> list:
    with ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS) as threads:
        return list(threads.map(lambda key: client.get_object(Bucket=bucket, Key=key)['Body'].read(), keys))


def read_rows(data: bytes, scale: str, width: int) -> tuple:
    """A file's (filepaths, vectors) with vectors as an (n, width) float32 array; ValueError if it isn't valid."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(io.BytesIO(data), columns=['filepath', 'vectors', 'scale'])
    except (pa.ArrowException, OSError) as error:
        raise ValueError(f'not a readable Parquet file with filepath, vectors and scale: {error}')
    vector_type = table.schema.field('vectors').type
    if not (
        pa.types.is_fixed_size_list(vector_type)
        and vector_type.list_size == width
        and vector_type.value_type == pa.float32()
    ):
        raise ValueError(f'vectors must be fixed_size_list<float32, {width}>, not {vector_type}')
    if table.column('scale').null_count or set(table.column('scale').to_pylist()) - {scale}:
        raise ValueError(f'every row\'s scale must be {scale}')
    vectors = table.column('vectors').combine_chunks()
    if table.column('filepath').null_count or vectors.null_count:
        raise ValueError('a row has no filepath or no vectors')
    values = vectors.flatten().to_numpy(zero_copy_only=False).reshape(len(table), width)
    if not np.isfinite(values).all():
        raise ValueError('vectors hold NaN or infinite values')
    return table.column('filepath').to_pylist(), values


def copy_payload(sample_ids: np.ndarray, tiles: np.ndarray, values: int) -> bytes:
    """PostgreSQL binary COPY rows (sample_id integer, position smallint, vector) for patch_import_stage:
    one row per tile, ``tiles`` shaped (images, tiles, values). pgvector's binary form is the dimension,
    an unused int16 and the float4s."""
    images, count = tiles.shape[:2]
    row = np.dtype(
        [
            ('fields', '>i2'),
            ('id_length', '>i4'),
            ('id', '>i4'),
            ('position_length', '>i4'),
            ('position', '>i2'),
            ('vector_length', '>i4'),
            ('dimension', '>i2'),
            ('unused', '>i2'),
            ('vector', '>f4', (values,)),
        ],
    )
    rows = np.empty(images * count, dtype=row)
    rows['fields'] = 3
    rows['id_length'] = 4
    rows['id'] = np.repeat(sample_ids, count)
    rows['position_length'] = 2
    rows['position'] = np.tile(np.arange(count), images)
    rows['vector_length'] = 4 + 4 * values
    rows['dimension'] = values
    rows['unused'] = 0
    rows['vector'] = tiles.reshape(images * count, values)
    return b'PGCOPY\n\xff\r\n\x00' + struct.pack('>ii', 0, 0) + rows.tobytes() + struct.pack('>h', -1)


def _note(checkpoint: dict, count_key: str, examples_key: str, count: int, examples: list) -> None:
    checkpoint[count_key] = checkpoint.get(count_key, 0) + count
    kept = checkpoint.setdefault(examples_key, [])
    kept.extend(examples[: max(0, EXAMPLES - len(kept))])


def handler(pool):
    async def measure(job):
        if job['kind'] != KIND or job['lane'] != 'maintenance':
            raise ValueError('Patch-vector imports run in the maintenance lane')
        args, checkpoint, dataset_id = job['args'], dict(job['checkpoint']), job['dataset_id']
        bucket, path = parse_prefix(args['prefix'])
        scale, values, field_name = args['scale'], args['values'], args['field_name']
        rows, cols = (int(part) for part in _SCALE_RE.match(scale).groups())  # type: ignore[union-attr]
        try:
            client = await s3_client(pool, dataset_id, bucket)
        except HTTPException as error:
            raise JobInputError(error.detail)
        keys, more = await asyncio.to_thread(_list_page, client, bucket, path, checkpoint.get('after', ''))
        files = [key for key in keys if key.endswith('.parquet')]
        found: dict = {}
        bad: list = []
        for key, data in zip(files, await asyncio.to_thread(_download, client, bucket, files)):
            try:
                filepaths, vectors = read_rows(data, scale, rows * cols * values)
            except ValueError as error:
                bad.append(f's3://{bucket}/{key}: {error}')
                continue
            found.update(zip(filepaths, vectors))
        matched = await pool.fetch(
            "SELECT id, filepath FROM samples WHERE dataset_id = $1 AND filepath = ANY($2::text[]) "
            "AND media_type IS DISTINCT FROM 'object' ORDER BY id",
            dataset_id,
            list(found),
        )
        unmatched = sorted(set(found) - {row['filepath'] for row in matched})
        sample_ids = np.array([row['id'] for row in matched], dtype=np.int64)
        tiles = (
            np.stack([found[row['filepath']] for row in matched]).reshape(len(matched), rows * cols, values)
            if matched
            else None
        )

        if keys:
            checkpoint['after'] = keys[-1]
        checkpoint['files'] = checkpoint.get('files', 0) + len(files)
        checkpoint['images'] = checkpoint.get('images', 0) + len(matched)
        _note(checkpoint, 'unmatched', 'unmatched_examples', len(unmatched), unmatched)
        _note(checkpoint, 'bad_files', 'bad_examples', len(bad), bad)
        finished = not more
        fields = patch_fields(field_name, scale)
        result = None
        if finished:
            result = {key: checkpoint.get(key, 0) for key in ('files', 'images', 'unmatched', 'bad_files')}
            result.update(
                unmatched_examples=checkpoint.get('unmatched_examples', []),
                bad_examples=checkpoint.get('bad_examples', []),
                fields=fields,
                info=args['info'],
            )

        async def publish(connection):
            if tiles is not None:
                await require_free_space(
                    connection,
                    tiles.shape[0] * tiles.shape[1] * INGEST_ROW_BYTES,
                    'importing patch vectors',
                )
                try:
                    # Shared, like any embedding write: a map re-fit's swap holds it exclusively, briefly.
                    await connection.execute(f"SET LOCAL lock_timeout = '{LOCK_SECONDS}s'")
                    await lock_field_lifecycle(connection, dataset_id)
                except asyncpg.LockNotAvailableError:
                    raise job_queue.QueueContended('A map is being swapped in on this dataset; importing after it')
                await connection.execute(
                    'CREATE TEMP TABLE patch_import_stage (sample_id integer, position smallint, vector vector) '
                    'ON COMMIT DROP',
                )
                payload = copy_payload(sample_ids, tiles, values)
                await connection.copy_to_table('patch_import_stage', source=io.BytesIO(payload), format='binary')
                await connection.execute(_UPSERT, field_name, scale, cols, dataset_id)
            if result is not None and args['build_indexes']:
                # The search index of each tile position, as build_ann_index would queue them.
                queued = []
                for field in fields:
                    existing = await index_jobs._unfinished(connection, index_jobs.ANN_KIND, dataset_id, 'field', field)
                    if existing is None:
                        existing = await job_queue.enqueue_job(
                            connection,
                            index_jobs.ANN_KIND,
                            dataset_id=dataset_id,
                            created_by=job['created_by'],
                            args={'field': field},
                        )
                    queued.append(str(existing['id']))
                result['index_jobs'] = queued

        return StepResult(
            checkpoint=checkpoint,
            done=checkpoint['files'],
            message=f"{checkpoint['files']:,} files read, {checkpoint['images']:,} images imported",
            finished=finished,
            result=result,
            publish=publish,
        )

    return measure
