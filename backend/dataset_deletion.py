"""Delete a sealed dataset in fenced, bounded units, retaining its job and resume cursor."""

import asyncio
import time

from fastapi import HTTPException

from . import job_queue
from .job_runner import StepResult
from .job_tasks import run_isolated

KIND = 'dataset_deletion'
BATCH_SIZE = 1000
BYTE_BUDGET = 8 * 1024 * 1024
POLICY = job_queue.JobPolicy(max_unit_seconds=30, max_failures=5, retry_seconds=2, max_retry_seconds=120)
BUSY = 'Dataset has an active operation; retry'
# Ingest, an index build or a background unit can hold the dataset for a moment. Admission waits
# that long, as main's synchronous delete did, before answering a retryable 409.
ADMISSION_WAIT_SECONDS = 5.0

# Remove fan-out before its parent. Every scope uses an existing leading-key index.
DATASET_TABLES = (
    ('embedding_viz_rank', ('dataset_id', 'field_name', 'sample_id')),
    ('duplicate_members', ('dataset_id', 'config', 'sample_id', 'key')),
    ('duplicate_pending', ('dataset_id', 'block_id', 'transaction_id')),
    ('duplicate_configs', ('dataset_id', 'config')),
    ('sample_dup_secondary', ('dataset_id', 'hash_config', 'sample_id')),
    ('concept_embedding_pending', ('dataset_id', 'field_name', 'block_id', 'transaction_id')),
    ('dataset_block_field_stats', ('dataset_id', 'block_id', 'part_id', 'field_name')),
    ('dataset_stats_parts', ('dataset_id', 'block_id', 'part_id')),
    ('dataset_stats_blocks', ('dataset_id', 'block_id')),
    ('dataset_stats_pending', ('dataset_id', 'block_id', 'transaction_id')),
    ('dataset_field_stats', ('dataset_id', 'field_name')),
    ('dataset_fields', ('dataset_id', 'field_name')),
    ('dataset_tags', ('dataset_id', 'field_name', 'tag')),
    ('embedding_viz_meta', ('dataset_id', 'field_name')),
    ('views', ('id',)),
    ('dataset_access', ('dataset_id', 'user_id')),
    ('dataset_stats_state', ('dataset_id',)),
)
CONCEPT_TABLES = (
    ('embedding_concept_scores', ('concept_id', 'sample_id')),
    ('concept_score_pending', ('concept_id', 'block_id', 'transaction_id')),
    ('concept_score_count_deltas', ('concept_id', 'transaction_id')),
)
SAMPLE_TABLES = (
    ('embedding_coordinates', ('sample_id', 'field_name')),
    ('embedding_projection_pending', ('sample_id', 'field_name')),
    ('embeddings', ('sample_id', 'field_name')),
    ('sample_hashes', ('sample_id', 'field_name')),
)


async def submit(pool, dataset_id, user_id, *, resume=False):
    deadline = time.monotonic() + ADMISSION_WAIT_SECONDS
    delay = 0.05
    while True:
        try:
            return await _submit(pool, dataset_id, user_id, resume=resume)
        except job_queue.QueueBusy:
            pass
        except HTTPException as error:
            if error.status_code != 409 or error.detail != BUSY:
                raise
        if time.monotonic() + delay > deadline:
            raise HTTPException(409, BUSY, headers={'Retry-After': '1'})
        await asyncio.sleep(delay)
        delay = min(delay * 2, 0.5)


async def _submit(pool, dataset_id, user_id, *, resume):
    async with job_queue._transaction(pool) as connection:
        # Do not queue behind ingest, an index build, or an old publication.
        for namespace in (20261016, 20261017):
            if not await connection.fetchval('SELECT pg_try_advisory_xact_lock($1,$2)', namespace, dataset_id):
                raise HTTPException(409, BUSY, headers={'Retry-After': '1'})
        # FOR UPDATE is part of the seal, not just admission serialization. PostgreSQL
        # marks the following UPDATE as a key update: old repeatable-read writers then
        # fail serialization at the guard's FOR KEY SHARE. A plain SELECT/UPDATE lets
        # those writers read the old deleting=FALSE row and commit through the seal.
        dataset = await connection.fetchrow(
            'SELECT deleting,deletion_job_id FROM datasets WHERE id=$1 FOR UPDATE NOWAIT',
            dataset_id,
        )
        if dataset is None:
            raise HTTPException(404, 'Dataset not found')
        if dataset['deleting']:
            job = await connection.fetchrow(
                f'SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE} WHERE jobs.id=$1 FOR UPDATE OF jobs NOWAIT',
                dataset['deletion_job_id'],
            )
            if job is None:
                raise HTTPException(409, 'Dataset deletion job is missing')
            if resume and job['cancel_requested'] and job['state'] in ('queued', 'running'):
                raise HTTPException(409, 'Wait for cancellation to finish before resuming deletion')
            if resume and job['state'] in ('failed', 'cancelled'):
                await connection.execute(
                    """UPDATE jobs SET state='queued',error=NULL,failures=0,contentions=0,
                       lease_token=NULL,lease_expires_at=NULL,finished_at=NULL,
                       available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=$1""",
                    job['id'],
                )
                await connection.execute('UPDATE job_controls SET cancel_requested=FALSE WHERE job_id=$1', job['id'])
                return {'job_id': str(job['id']), 'state': 'queued'}
            return {'job_id': str(job['id']), 'state': job['state']}
        if resume:
            raise HTTPException(409, 'Dataset deletion has not started')
        job = await job_queue.enqueue_job(connection, KIND, dataset_id=dataset_id, lane='user', created_by=user_id)
        await connection.execute(
            'UPDATE datasets SET deleting=TRUE,deletion_job_id=$2 WHERE id=$1',
            dataset_id,
            job['id'],
        )
        # At most two logical lane owners. Unstarted followers are reaped by claim_job.
        await connection.execute(
            """UPDATE job_controls SET cancel_requested=TRUE WHERE job_id IN (
                   SELECT id FROM jobs WHERE dataset_id=$1 AND id<>$2
                     AND (state='running' OR (state='queued' AND started_at IS NOT NULL)))""",
            dataset_id,
            job['id'],
        )
        return {'job_id': str(job['id']), 'state': job['state']}


async def _rows(pool, query, *args):
    async with job_queue._transaction(pool) as connection:
        return await connection.fetch(query, *args)


def _bounded(rows):
    selected, size = [], 0
    for row in rows:
        if row['bytes'] > BYTE_BUDGET:
            raise ValueError(f"A single row exceeds the dataset deletion byte budget ({BYTE_BUDGET} bytes)")
        if size + row['bytes'] > BYTE_BUDGET:
            break
        selected.append(row['key'])
        size += row['bytes']
    return selected, size


async def _batch(pool, table, keys, where, args, size, checkpoint=None, scope=None):
    cursor = (checkpoint or {}).get('cursors', {}).get(table)
    if cursor and cursor['scope'] == scope:
        start = len(args) + 1
        where += (
            f" AND ({','.join('t.' + name for name in keys)})"
            f" > ({','.join('$' + str(start + index) for index in range(len(keys)))})"
        )
        args = [*args, *(cursor['key'][name] for name in keys)]
    key = ','.join(f"'{name}',t.{name}" for name in keys)
    rows = await _rows(
        pool,
        f"""SELECT jsonb_build_object({key}) AS key,pg_column_size(t)::bigint AS bytes
            FROM {table} t WHERE {where} ORDER BY {','.join('t.' + name for name in keys)} LIMIT ${len(args) + 1}""",
        *args,
        size,
    )
    return _bounded(rows)


def _delete_step(job, checkpoint, table, keys, rows, size, scope=None):
    next_checkpoint = {
        **checkpoint,
        'deleted_rows': checkpoint.get('deleted_rows', 0) + len(rows),
        'last_unit_rows': len(rows),
        'last_unit_bytes': size,
    }
    if rows:
        next_checkpoint['cursors'] = {
            **checkpoint.get('cursors', {}),
            table: {'scope': scope, 'key': rows[-1]},
        }

    async def publish(connection):
        await connection.execute("SELECT set_config('fridge.deletion_dataset',$1,TRUE)", str(job['dataset_id']))
        matches = ' AND '.join(f't.{key}=target.{key}' for key in keys)
        status = await connection.execute(
            f'DELETE FROM {table} t USING jsonb_populate_recordset(NULL::{table},$1::jsonb) target WHERE {matches}',
            rows,
        )
        if int(status.split()[-1]) != len(rows):
            raise job_queue.QueueContended('Deletion targets changed; remeasure this unit')

    return StepResult(
        checkpoint=next_checkpoint,
        done=next_checkpoint['deleted_rows'],
        message=f'Removing {table.replace("_", " ")}',
        publish=publish,
    )


async def _drop_index(pool, job):
    async with pool.acquire(timeout=1) as connection:

        async def drop():
            from .index_jobs import TABLE_LOCK, try_lock, unlock

            if not await connection.fetchval('SELECT pg_try_advisory_lock(20261017,$1)', job['dataset_id']):
                raise job_queue.QueueContended('Waiting for the dataset index operation')
            # Like every concurrent index DDL on samples: never wait behind another one (index_jobs).
            if not await try_lock(connection, TABLE_LOCK, 'samples'):
                await connection.execute('SELECT pg_advisory_unlock(20261017,$1)', job['dataset_id'], timeout=1)
                raise job_queue.QueueContended('Waiting for another index build or removal on samples')
            try:
                valid = await connection.fetchval(
                    """SELECT 1 FROM jobs JOIN job_controls ON job_id=id
                       WHERE id=$1 AND lease_token=$2 AND lease_expires_at>clock_timestamp()
                         AND state='running' AND NOT cancel_requested""",
                    job['id'],
                    job['lease_token'],
                )
                if not valid:
                    raise job_queue.LeaseLost('Deletion lease was lost before index removal')
                index = await connection.fetchval(
                    """SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()
                       AND tablename='samples' AND indexname ~ $1 ORDER BY indexname LIMIT 1""",
                    f'^idx_samples_meta_{job["dataset_id"]}_',
                )
                if index is None:
                    return False
                await connection.execute("SET lock_timeout='100ms'; SET statement_timeout='5s'")
                quoted = index.replace('"', '""')
                await connection.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{quoted}"', timeout=5)
                return True
            finally:
                if not connection.is_closed():
                    await unlock(connection, TABLE_LOCK, 'samples')
                    await connection.execute(
                        'SELECT pg_advisory_unlock(20261017,$1)',
                        job['dataset_id'],
                        timeout=1,
                    )
                    await connection.execute('RESET lock_timeout; RESET statement_timeout', timeout=1)

        return await run_isolated(
            drop(),
            name='Dataset index removal',
            timeout=8,
            on_error=lambda: job_queue._terminate_connection(connection),
        )


def handler(pool):
    async def measure(job):
        keys: tuple[str, ...]
        if job['kind'] != KIND or job['lane'] != 'user' or job['dataset_id'] is None:
            raise ValueError('Dataset deletion requires the user lane and a dataset')
        checkpoint = dict(job['checkpoint'])
        phase = checkpoint.get('phase', 'concepts')
        batch_size = max(1, checkpoint.get('batch_size', BATCH_SIZE) // (2 ** min(job['failures'], 10)))
        checkpoint['batch_size'] = batch_size
        dataset_id = job['dataset_id']
        target = await _rows(
            pool,
            'SELECT id FROM datasets WHERE id=$1 AND deleting AND deletion_job_id=$2',
            dataset_id,
            job['id'],
        )
        if not target:
            raise ValueError('This job does not own the dataset deletion')

        if phase in ('concepts', 'versions'):
            parent = 'embedding_concepts' if phase == 'concepts' else 'dataset_versions'
            parents = await _rows(
                pool,
                f'SELECT id FROM {parent} WHERE dataset_id=$1 ORDER BY name LIMIT 1',
                dataset_id,
            )
            if not parents:
                return StepResult(checkpoint={**checkpoint, 'phase': 'versions' if phase == 'concepts' else 'tables'})
            parent_id = parents[0]['id']
            children = CONCEPT_TABLES if phase == 'concepts' else (('version_snapshots', ('version_id', 'sample_id')),)
            for table, keys in children:
                field = 'concept_id' if phase == 'concepts' else 'version_id'
                rows, size = await _batch(
                    pool,
                    table,
                    keys,
                    f't.{field}=$1',
                    [parent_id],
                    batch_size,
                    checkpoint,
                    parent_id,
                )
                if rows:
                    return _delete_step(job, checkpoint, table, keys, rows, size, parent_id)
            rows, size = await _batch(pool, parent, ('id',), 't.id=$1', [parent_id], 1)
            return _delete_step(job, checkpoint, parent, ('id',), rows, size)

        if phase == 'tables':
            for table, keys in (
                ('bulk_job_targets', ('job_id', 'sample_id')),
                ('bulk_selection_pins', ('job_id', 'token')),
            ):
                rows, size = await _batch(
                    pool,
                    table,
                    keys,
                    't.job_id IN (SELECT id FROM jobs WHERE dataset_id=$1)',
                    [dataset_id],
                    batch_size,
                    checkpoint,
                )
                if rows:
                    return _delete_step(job, checkpoint, table, keys, rows, size)
            index = checkpoint.get('table', 0)
            if index == len(DATASET_TABLES):
                return StepResult(checkpoint={**checkpoint, 'phase': 'samples'})
            table, keys = DATASET_TABLES[index]
            rows, size = await _batch(pool, table, keys, 't.dataset_id=$1', [dataset_id], batch_size, checkpoint)
            if rows:
                return _delete_step(job, checkpoint, table, keys, rows, size)
            return StepResult(checkpoint={**checkpoint, 'table': index + 1})

        if phase == 'samples':
            sample_ids = checkpoint.get('sample_ids')
            if not sample_ids:
                rows, _ = await _batch(
                    pool,
                    'samples',
                    ('id',),
                    't.dataset_id=$1',
                    [dataset_id],
                    batch_size,
                    checkpoint,
                )
                if not rows:
                    return StepResult(checkpoint={**checkpoint, 'phase': 'indexes'})
                sample_ids = [row['id'] for row in rows]
                checkpoint['sample_ids'] = sample_ids
            for table, keys in SAMPLE_TABLES:
                rows, size = await _batch(
                    pool,
                    table,
                    keys,
                    't.sample_id=ANY($1::int[])',
                    [sample_ids],
                    batch_size,
                    checkpoint,
                )
                if rows:
                    return _delete_step(job, checkpoint, table, keys, rows, size)
            rows, size = await _batch(
                pool,
                'samples',
                ('id',),
                't.id=ANY($1::int[])',
                [sample_ids],
                batch_size,
                checkpoint,
            )
            # A retry can shrink the sample batch; retain its remaining IDs until all are removed.
            if rows:
                return _delete_step(job, checkpoint, 'samples', ('id',), rows, size)
            checkpoint.pop('sample_ids')
            return StepResult(checkpoint=checkpoint)

        if phase == 'indexes':
            if await _drop_index(pool, job):
                return StepResult(
                    checkpoint={**checkpoint, 'indexes': checkpoint.get('indexes', 0) + 1},
                    message='Removing dataset indexes',
                )

            async def finish(connection):
                await connection.execute("SELECT set_config('fridge.deletion_dataset',$1,TRUE)", str(dataset_id))
                await connection.execute(
                    'DELETE FROM datasets WHERE id=$1 AND deleting AND deletion_job_id=$2',
                    dataset_id,
                    job['id'],
                )

            return StepResult(
                checkpoint={**checkpoint, 'phase': 'finished'},
                finished=True,
                result={'message': f'Dataset {dataset_id} deleted successfully'},
                message='Dataset deleted',
                publish=finish,
            )
        raise ValueError(f'Unknown dataset deletion phase: {phase}')

    return measure
