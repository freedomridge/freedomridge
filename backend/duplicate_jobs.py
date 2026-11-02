"""Incremental exact-duplicate collapse, with retained keys and indexed minimum probes."""

import asyncio
import logging
import time

import asyncpg

from . import job_queue
from .concept_jobs import _read
from .job_runner import StepResult

KIND = 'duplicate_maintenance'
BLOCK_SIZE = 1024
BATCH_SIZE = 256
POLICY = job_queue.JobPolicy(max_unit_seconds=60, max_failures=3, retry_seconds=2, max_retry_seconds=120)
logger = logging.getLogger(__name__)


async def request_refresh(pool, dataset_id, config=None, *, created_by=None):
    """Explicit config requests bootstrap; the scheduler drains a finite committed barrier."""
    async with job_queue._transaction(pool) as connection:
        await connection.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
            f'duplicates:{dataset_id}',
        )
        # A deleting dataset raises the lifecycle error (HTTP 409); the scheduler skips it.
        await connection.execute('SELECT require_writable_dataset($1)', dataset_id)
        args = {} if config is None else {'config': config}
        existing = await connection.fetchrow(
            f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
                WHERE kind=$1 AND dataset_id=$2 AND args=$3
                  AND state IN ('queued','running') AND NOT cancel_requested
                ORDER BY created_at,id LIMIT 1""",
            KIND,
            dataset_id,
            args,
        )
        if existing:
            return dict(existing)
        if config is not None:
            await connection.execute(
                """INSERT INTO duplicate_configs(dataset_id,config) VALUES($1,$2)
                   ON CONFLICT(dataset_id,config) DO UPDATE SET initialized=FALSE
                   WHERE duplicate_configs.initialized""",
                dataset_id,
                config,
            )
        configs = await connection.fetch(
            'SELECT config FROM duplicate_configs WHERE dataset_id=$1 ORDER BY config',
            dataset_id,
        )
        barrier = {
            'full': config is not None,
            'snapshot': await connection.fetchval('SELECT txid_current_snapshot()::text'),
            'configs': [row['config'] for row in configs],
        }
        checkpoint = {}
        if config is not None:
            barrier['upper_id'] = await connection.fetchval(
                """SELECT GREATEST(
                    COALESCE((SELECT max(id) FROM samples WHERE dataset_id=$1),0),
                    COALESCE((SELECT max(sample_id) FROM duplicate_members WHERE dataset_id=$1 AND config=$2),0),
                    COALESCE((SELECT max(sample_id) FROM sample_dup_secondary
                              WHERE dataset_id=$1 AND hash_config=$2),0))""",
                dataset_id,
                config,
            )
            previous = await connection.fetchrow(
                f"""SELECT state,barrier,checkpoint,controls.cancel_requested FROM {job_queue.JOB_TABLE}
                    WHERE kind=$1 AND dataset_id=$2 AND args=$3
                    ORDER BY created_at DESC,jobs.id DESC LIMIT 1""",
                KIND,
                dataset_id,
                args,
            )
            restart = await connection.fetchval(
                'SELECT restart_required FROM duplicate_configs WHERE dataset_id=$1 AND config=$2',
                dataset_id,
                config,
            )
            if (
                not restart
                and previous
                and previous['barrier']
                and (
                    previous['state'] in ('failed', 'cancelled')
                    or (previous['state'] in ('queued', 'running') and previous['cancel_requested'])
                )
            ):
                barrier = previous['barrier']
                checkpoint = previous['checkpoint']
        job = await job_queue.enqueue_job(
            connection,
            KIND,
            dataset_id=dataset_id,
            lane='maintenance',
            args=args,
            barrier=barrier,
            created_by=created_by,
            dedupe_key=f'duplicates:{dataset_id}:{config}',
        )
        if checkpoint:
            await connection.execute('UPDATE jobs SET checkpoint=$2 WHERE id=$1', job['id'], checkpoint)
            job['checkpoint'] = checkpoint
        if config is not None:
            await connection.execute(
                'UPDATE duplicate_configs SET restart_required=FALSE WHERE dataset_id=$1 AND config=$2 '
                'AND restart_required',
                dataset_id,
                config,
            )
        return job


async def schedule_duplicates(pool, limit=10):
    async with job_queue._transaction(pool) as connection:
        await connection.execute(
            """DELETE FROM duplicate_pending WHERE ctid IN (
                 SELECT p.ctid FROM duplicate_pending p
                 WHERE EXISTS(SELECT 1 FROM datasets d WHERE d.id=p.dataset_id AND NOT d.deleting)
                   AND NOT EXISTS(SELECT 1 FROM duplicate_configs c WHERE c.dataset_id=p.dataset_id)
                 LIMIT 1024)""",
        )
        # A truncate requests a rebuild after any current lane owner has finished.
        await connection.execute(
            """WITH removed AS (
                 DELETE FROM duplicate_pending WHERE ctid IN (
                   SELECT p.ctid FROM duplicate_pending p WHERE block_id=-1
                     AND EXISTS(SELECT 1 FROM datasets d WHERE d.id=p.dataset_id AND NOT d.deleting)
                     AND NOT EXISTS(SELECT 1 FROM jobs WHERE dataset_id=p.dataset_id AND kind=$1
                                    AND state IN ('queued','running'))
                   LIMIT 1024) RETURNING dataset_id)
               UPDATE duplicate_configs SET initialized=FALSE,restart_required=TRUE
               WHERE dataset_id IN (SELECT dataset_id FROM removed)""",
            KIND,
        )
        candidates = await connection.fetch(
            """SELECT d.id,(SELECT config FROM duplicate_configs c
                            WHERE c.dataset_id=d.id AND (NOT initialized OR restart_required)
                            ORDER BY config LIMIT 1) AS bootstrap
               FROM datasets d WHERE NOT d.deleting AND EXISTS(SELECT 1 FROM duplicate_configs WHERE dataset_id=d.id)
                 AND (EXISTS(SELECT 1 FROM duplicate_configs WHERE dataset_id=d.id
                             AND (NOT initialized OR restart_required))
                      OR EXISTS(SELECT 1 FROM duplicate_pending WHERE dataset_id=d.id))
                 AND NOT EXISTS(SELECT 1 FROM jobs WHERE dataset_id=d.id AND kind=$1
                                AND state IN ('queued','running'))
                 AND NOT EXISTS(SELECT 1 FROM jobs WHERE dataset_id=d.id AND kind=$1 AND state='failed'
                                AND finished_at>clock_timestamp()-interval '5 minutes')
               ORDER BY d.id LIMIT $2""",
            KIND,
            limit,
        )
    submitted = []
    for row in candidates:
        try:
            submitted.append(await request_refresh(pool, row['id'], row['bootstrap']))
        except (job_queue.QueueBusy, asyncpg.ObjectNotInPrerequisiteStateError):
            continue
    return submitted


async def _keys(connection, dataset_id, config, ids):
    if config == '_all':
        expression = "string_agg(h.field_name||':'||h.hash_hex,',' ORDER BY h.field_name)"
        suffix = 'GROUP BY h.sample_id'
    elif config == '_any':
        expression = 'jsonb_build_array(h.field_name,h.hash_hex)::text'
        suffix = ''
    else:
        expression = 'h.hash_hex'
        suffix = 'AND h.field_name=$3'
    rows = await connection.fetch(
        f"""SELECT h.sample_id,{expression} AS key FROM unnest($2::int[]) target(id)
            JOIN samples s ON s.id=target.id AND s.dataset_id=$1
            JOIN sample_hashes h ON h.sample_id=s.id {suffix}""",
        *([dataset_id, ids, config] if config not in ('_all', '_any') else [dataset_id, ids]),
    )
    values = {(row['sample_id'], row['key']) for row in rows}
    oversized = {sid for sid, key in values if len(config.encode()) + len(key.encode()) > 2000}
    if oversized:
        logger.warning(
            'Skipping duplicate config %s for dataset %s samples %s: key exceeds the 2000-byte btree budget',
            config,
            dataset_id,
            sorted(oversized),
        )
    return {(sid, key) for sid, key in values if sid not in oversized}


async def _members(connection, dataset_id, config, ids):
    rows = await connection.fetch(
        """SELECT sample_id,key FROM duplicate_members
           WHERE dataset_id=$1 AND config=$2 AND sample_id=ANY($3::int[])""",
        dataset_id,
        config,
        ids,
    )
    return {(row['sample_id'], row['key']) for row in rows}


async def _minima(connection, dataset_id, config, keys, excluded=()):
    rows = await connection.fetch(
        """SELECT target.key,m.sample_id FROM unnest($3::text[]) target(key)
           LEFT JOIN LATERAL (
             SELECT sample_id FROM duplicate_members m
             WHERE m.dataset_id=$1 AND m.config=$2 AND m.key=target.key
               AND NOT sample_id=ANY($4::int[])
             ORDER BY sample_id LIMIT 1) m ON TRUE""",
        dataset_id,
        config,
        sorted(keys),
        list(excluded),
    )
    return {row['key']: row['sample_id'] for row in rows}


async def _hidden_diff(connection, dataset_id, config, candidates, memberships, minima):
    desired = {sid for sid, key in memberships if minima[key] is not None and sid > minima[key]}
    existing = {
        row['sample_id']
        for row in await connection.fetch(
            """SELECT sample_id FROM sample_dup_secondary
               WHERE dataset_id=$1 AND hash_config=$2 AND sample_id=ANY($3::int[])""",
            dataset_id,
            config,
            sorted(candidates),
        )
    }
    return sorted(existing - desired), sorted(desired - existing)


async def _delta(connection, dataset_id, config, ids, *, bootstrap=False, reconcile=False):
    old = await _members(connection, dataset_id, config, ids)
    new = old if reconcile else await _keys(connection, dataset_id, config, ids)
    removed, added = old - new, new - old
    hidden_removed, hidden_added = [], []
    if reconcile:
        minima = await _minima(connection, dataset_id, config, {key for _, key in old})
        hidden_removed, hidden_added = await _hidden_diff(connection, dataset_id, config, ids, old, minima)
    elif not bootstrap:
        affected = {key for _, key in removed | added}
        before = await _minima(connection, dataset_id, config, affected)
        minima = await _minima(connection, dataset_id, config, affected, ids)
        for sid, key in new:
            if key in affected and (minima[key] is None or sid < minima[key]):
                minima[key] = sid
        # A sample delete/recreate can cascade its hidden row without changing its cached key.
        candidates = set(ids)
        candidates.update(sid for sid in [*before.values(), *minima.values()] if sid is not None)
        memberships = await _members(connection, dataset_id, config, sorted(candidates - set(ids)))
        memberships |= {(sid, key) for sid, key in new if sid in candidates}
        other_keys = {key for _, key in memberships} - affected
        minima.update(await _minima(connection, dataset_id, config, other_keys))
        hidden_removed, hidden_added = await _hidden_diff(
            connection,
            dataset_id,
            config,
            candidates,
            memberships,
            minima,
        )
    return {
        'removed': sorted(removed),
        'added': sorted(added),
        'hidden_removed': hidden_removed,
        'hidden_added': hidden_added,
    }


async def _page(connection, dataset_id, config, after, upper, size, *, reconcile=False):
    source = (
        'SELECT sample_id FROM sample_dup_secondary WHERE dataset_id=$1 AND hash_config=$2'
        if reconcile
        else 'SELECT id AS sample_id FROM samples WHERE dataset_id=$1'
    )
    rows = await connection.fetch(
        f"""SELECT sample_id FROM (
              ({source} AND {'sample_id' if reconcile else 'id'}>$3
                 AND {'sample_id' if reconcile else 'id'}<=$4
                 ORDER BY {'sample_id' if reconcile else 'id'} LIMIT $5)
              UNION
              (SELECT DISTINCT sample_id FROM duplicate_members WHERE dataset_id=$1 AND config=$2
                 AND sample_id>$3 AND sample_id<=$4 ORDER BY sample_id LIMIT $5)
            ) targets ORDER BY sample_id LIMIT $5""",
        dataset_id,
        config,
        after,
        upper,
        size,
    )
    return [row['sample_id'] for row in rows]


async def _measure(connection, job, size):
    dataset_id = job['dataset_id']
    if not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM datasets WHERE id=$1)', dataset_id):
        return {}, None, {}, True, {'dataset_deleted': True}
    checkpoint = dict(job['checkpoint'])
    full = job['barrier']['full']
    if full:
        config = job['args']['config']
        phase = checkpoint.get('phase', 'members')
        if phase == 'count':
            hidden = await connection.fetchval(
                'SELECT count(*) FROM sample_dup_secondary WHERE dataset_id=$1 AND hash_config=$2',
                dataset_id,
                config,
            )
            return checkpoint, config, {}, True, {'config': config, 'hidden': hidden}
        ids = await _page(
            connection,
            dataset_id,
            config,
            checkpoint.get('after', 0),
            job['barrier']['upper_id'],
            size,
            reconcile=phase == 'reconcile',
        )
        delta = await _delta(
            connection,
            dataset_id,
            config,
            ids,
            bootstrap=True,
            reconcile=phase == 'reconcile',
        )
        if ids:
            checkpoint['after'] = ids[-1]
        else:
            checkpoint = {'phase': 'reconcile' if phase == 'members' else 'count', 'after': 0}
        return checkpoint, config, delta, False, None
    if 'block' not in checkpoint:
        if await connection.fetchval(
            'SELECT EXISTS(SELECT 1 FROM duplicate_configs WHERE dataset_id=$1 '
            'AND (NOT initialized OR restart_required))',
            dataset_id,
        ):
            # A cancelled bootstrap may have left only part of the cache repaired.
            # Release the lane without acknowledging work so its successor can resume.
            return {}, None, {}, True, {'bootstrap_pending': True}
        rows = await connection.fetch(
            """SELECT block_id,transaction_id FROM duplicate_pending WHERE dataset_id=$1
                 AND txid_visible_in_snapshot(transaction_id,$2::text::txid_snapshot)
               ORDER BY block_id,transaction_id LIMIT 1024""",
            dataset_id,
            job['barrier']['snapshot'],
        )
        if not rows or rows[0]['block_id'] == -1:
            return checkpoint, None, {}, True, {'updated': True}
        block = rows[0]['block_id']
        checkpoint = {
            'block': block,
            'transactions': [r['transaction_id'] for r in rows if r['block_id'] == block],
            'config_index': 0,
            'after': block * BLOCK_SIZE - 1,
        }
    index = checkpoint['config_index']
    if index == len(job['barrier']['configs']):
        return {}, None, {'ack': checkpoint}, False, None
    config = job['barrier']['configs'][index]
    ids = await _page(
        connection,
        dataset_id,
        config,
        checkpoint['after'],
        (checkpoint['block'] + 1) * BLOCK_SIZE - 1,
        size,
    )
    delta = await _delta(connection, dataset_id, config, ids)
    if ids:
        checkpoint['after'] = ids[-1]
    else:
        checkpoint.update(config_index=index + 1, after=checkpoint['block'] * BLOCK_SIZE - 1)
    return checkpoint, config, delta, False, None


async def _publish_delta(connection, dataset_id, config, delta):
    for operation in ('removed', 'added'):
        values = delta.get(operation, [])
        if not values:
            continue
        ids, keys = map(list, zip(*values))
        if operation == 'removed':
            await connection.execute(
                """DELETE FROM duplicate_members m USING unnest($3::int[],$4::text[]) d(sample_id,key)
                   WHERE m.dataset_id=$1 AND m.config=$2 AND m.sample_id=d.sample_id AND m.key=d.key""",
                dataset_id,
                config,
                ids,
                keys,
            )
        else:
            await connection.execute(
                """INSERT INTO duplicate_members(dataset_id,config,sample_id,key)
                   SELECT $1,$2,sample_id,key FROM unnest($3::int[],$4::text[]) d(sample_id,key)
                   ON CONFLICT DO NOTHING""",
                dataset_id,
                config,
                ids,
                keys,
            )
    if delta.get('hidden_removed'):
        await connection.execute(
            """DELETE FROM sample_dup_secondary
               WHERE dataset_id=$1 AND hash_config=$2 AND sample_id=ANY($3::int[])""",
            dataset_id,
            config,
            delta['hidden_removed'],
        )
    if delta.get('hidden_added'):
        await connection.fetch(
            'SELECT id FROM samples WHERE id=ANY($1::int[]) ORDER BY id FOR KEY SHARE NOWAIT',
            delta['hidden_added'],
        )
        await connection.execute(
            """INSERT INTO sample_dup_secondary(dataset_id,hash_config,sample_id)
               SELECT $1,$2,id FROM samples WHERE dataset_id=$1 AND id=ANY($3::int[])
               ON CONFLICT DO NOTHING""",
            dataset_id,
            config,
            delta['hidden_added'],
        )


def handler(pool):
    async def measure(job):
        if job['lane'] != 'maintenance':
            raise ValueError('Duplicate upkeep belongs to the maintenance lane')
        size = job['checkpoint'].get('batch_size', BATCH_SIZE)
        counting = job['checkpoint'].get('phase') == 'count'
        started = time.monotonic()
        while True:
            try:
                checkpoint, config, delta, finished, result = await _read(
                    pool,
                    lambda connection: _measure(connection, job, size),
                    seconds=45 if counting else 5,
                )
                break
            except (asyncpg.QueryCanceledError, TimeoutError, asyncio.TimeoutError):
                if counting or size == 1 or time.monotonic() - started >= 20:
                    raise
                size = max(1, size // 2)
        if checkpoint:
            checkpoint['batch_size'] = size

        async def publish(connection):
            if not await connection.fetchval('SELECT pg_try_advisory_xact_lock_shared(20261016,$1)', job['dataset_id']):
                raise job_queue.QueueContended('Duplicate publication is waiting for a dataset operation')
            if not await connection.fetchval(
                'SELECT id FROM datasets WHERE id=$1 AND NOT deleting FOR KEY SHARE NOWAIT',
                job['dataset_id'],
            ):
                # A deleted dataset cannot be claimed again. Retire this job in the fenced commit.
                await connection.execute(
                    'UPDATE job_controls SET cancel_requested=TRUE WHERE job_id=$1',
                    job['id'],
                )
                return
            if config is not None:
                await _publish_delta(connection, job['dataset_id'], config, delta)
            if 'ack' in delta:
                ack = delta['ack']
                await connection.execute(
                    """DELETE FROM duplicate_pending WHERE dataset_id=$1 AND block_id=$2
                       AND transaction_id=ANY($3::bigint[])""",
                    job['dataset_id'],
                    ack['block'],
                    ack['transactions'],
                )
            if job['barrier']['full']:
                await connection.execute(
                    'UPDATE duplicate_configs SET initialized=$3 WHERE dataset_id=$1 AND config=$2 AND initialized<>$3',
                    job['dataset_id'],
                    config,
                    finished,
                )

        return StepResult(
            checkpoint=checkpoint,
            finished=finished,
            result=result,
            publish=publish,
            message='Updating duplicate groups' if not finished else 'Duplicate groups updated',
        )

    return measure
