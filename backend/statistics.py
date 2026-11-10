"""Finite, resumable statistics jobs with disposable committed-work tracking."""

import asyncio
import time
from typing import Any

import asyncpg

from . import job_queue, statistics_measure
from .field_registry import REQUEST_FULL_REFRESH
from .job_runner import StepResult

KIND = 'statistics'
POLICY = job_queue.JobPolicy(max_unit_seconds=30, max_failures=3, retry_seconds=2, max_retry_seconds=120)
# A full refresh queued or running, whoever asked: it re-measures every block and publishes every cache.
FULL_REFRESH_ACTIVE = f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
    WHERE kind='{KIND}' AND dataset_id=$1 AND state IN ('queued','running')
      AND (barrier->>'full')::boolean AND NOT cancel_requested
    ORDER BY created_at,id LIMIT 1"""


async def request_refresh(pool, dataset_id, *, created_by=None, force=False, unless_full_active=False):
    """unless_full_active: a full refresh already queued or running is returned instead, with no new request,
    checked under the enqueue's lock so callers arriving together get one job."""
    if force and not unless_full_active:
        async with job_queue._transaction(pool) as connection:
            await connection.execute(REQUEST_FULL_REFRESH, dataset_id)
    owner = 'system' if created_by is None else f'user:{created_by}'
    async with job_queue._transaction(pool) as connection:
        await connection.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
            f'statistics:{dataset_id}:{owner}',
        )
        if unless_full_active:
            active = await connection.fetchrow(FULL_REFRESH_ACTIVE + ' FOR UPDATE OF jobs', dataset_id)
            if active is not None:
                return dict(active)
            if force:
                await connection.execute(REQUEST_FULL_REFRESH, dataset_id)
        boundary = await connection.fetchrow(
            """SELECT txid_current_snapshot()::text AS snapshot, stats_full_requests,
                      GREATEST(COALESCE((SELECT max(id) FROM samples WHERE dataset_id=$1),0),
                        COALESCE((SELECT max(block_id)*4096+4095 FROM dataset_stats_blocks WHERE dataset_id=$1),0))
                        AS upper_id,
                      NOT COALESCE((SELECT initialized FROM dataset_stats_state WHERE dataset_id=$1),FALSE)
                        OR EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=$1 AND block_id=-1) AS full
               FROM datasets WHERE id=$1 AND NOT deleting""",
            dataset_id,
        )
        if boundary is None:
            raise LookupError('Dataset not found')
        barrier = {**dict(boundary), 'full': boundary['full'] or force}
        existing = await connection.fetchrow(
            f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
                WHERE kind=$1 AND dataset_id=$2 AND dedupe_owner=$3
                  AND state='queued' AND started_at IS NULL AND NOT cancel_requested
                ORDER BY created_at,id LIMIT 1 FOR UPDATE OF jobs""",
            KIND,
            dataset_id,
            owner,
        )
        if existing:
            barrier['full'] |= existing['barrier']['full']
            barrier['upper_id'] = max(barrier['upper_id'], existing['barrier']['upper_id'])
            row = await connection.fetchrow(
                'UPDATE jobs SET barrier=$2,updated_at=clock_timestamp() WHERE id=$1 RETURNING *',
                existing['id'],
                barrier,
            )
            return {**dict(row), 'cancel_requested': False}
        return await job_queue.enqueue_job(
            connection,
            KIND,
            dataset_id=dataset_id,
            lane='maintenance',
            created_by=created_by,
            barrier=barrier,
            dedupe_key=f'statistics:{dataset_id}',
        )


async def schedule_statistics(pool, *, limit=10, created_by=None):
    rows = await statistics_measure.read_rows(
        pool,
        """SELECT dataset.id FROM datasets dataset LEFT JOIN dataset_stats_state state ON state.dataset_id=dataset.id
           WHERE NOT dataset.deleting AND (NOT COALESCE(state.initialized,FALSE) OR state.publish_pending OR EXISTS(
               SELECT 1 FROM dataset_stats_pending pending WHERE pending.dataset_id=dataset.id))
             AND NOT EXISTS(SELECT 1 FROM jobs WHERE dataset_id=dataset.id AND kind=$1
                 AND state IN ('queued','running'))
             AND NOT EXISTS(SELECT 1 FROM jobs WHERE dataset_id=dataset.id AND kind='statistics'
                 AND state='failed' AND finished_at>clock_timestamp()-interval '5 minutes')
           ORDER BY dataset.stats_last_updated NULLS FIRST,dataset.id LIMIT $2""",
        KIND,
        limit,
    )
    jobs = []
    for row in rows:
        try:
            jobs.append(await request_refresh(pool, row['id'], created_by=created_by))
        except (
            LookupError,
            job_queue.QueueBusy,
            asyncpg.ObjectNotInPrerequisiteStateError,
            asyncpg.LockNotAvailableError,
        ):
            continue
    return jobs


async def wait_for_refresh(pool, job_id, timeout=1800):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Statistics are still queued or running; the job has not been cancelled')
        job = await statistics_measure._read_snapshot(
            pool,
            lambda connection: connection.fetchrow('SELECT state,result,error FROM jobs WHERE id=$1', job_id),
            timeout=min(5, remaining),
        )
        if job is None:
            raise LookupError('Statistics job no longer exists')
        if job['state'] == 'succeeded':
            return job['result']
        if job['state'] in ('failed', 'cancelled'):
            raise RuntimeError(f"Statistics job {job['state']}: {job['error'] or 'no result published'}")
        await asyncio.sleep(min(0.25, remaining))


async def _lifecycle(connection, dataset_id):
    if not await connection.fetchval('SELECT pg_try_advisory_xact_lock_shared(20261016,$1)', dataset_id):
        raise job_queue.QueueContended('Statistics publication is waiting for a field operation')


async def _next_block(pool, dataset_id, barrier, after):
    if barrier['full']:
        return await statistics_measure.read_value(
            pool,
            """SELECT min(block_id) FROM (
                 (SELECT id::bigint/4096 AS block_id FROM samples
                  WHERE dataset_id=$1 AND id >= $2 AND id <= $3 ORDER BY id LIMIT 1)
                 UNION ALL
                 (SELECT block_id FROM dataset_stats_blocks WHERE dataset_id=$1 AND block_id>$4
                  AND block_id <= $3::bigint/4096 ORDER BY block_id LIMIT 1)
                 UNION ALL
                 (SELECT block_id FROM dataset_stats_pending WHERE dataset_id=$1 AND block_id>$4
                  AND txid_visible_in_snapshot(transaction_id,$5::text::txid_snapshot)
                  ORDER BY block_id LIMIT 1)) candidates""",
            dataset_id,
            (after + 1) * statistics_measure.BLOCK_SIZE,
            barrier['upper_id'],
            after,
            barrier['snapshot'],
        )
    return await statistics_measure.read_value(
        pool,
        """SELECT block_id FROM dataset_stats_pending WHERE dataset_id=$1 AND block_id>$2
           AND txid_visible_in_snapshot(transaction_id,$3::text::txid_snapshot) ORDER BY block_id LIMIT 1""",
        dataset_id,
        after,
        barrier['snapshot'],
    )


async def _publish_part(connection, job, block_id, part_id, split_level, measured, first_snapshot):
    dataset_id = job['dataset_id']
    await _lifecycle(connection, dataset_id)
    current = {
        field['field_name']: field['token'] for field in await statistics_measure.definitions(connection, dataset_id)
    }
    await connection.execute(
        'DELETE FROM dataset_stats_parts WHERE dataset_id=$1 AND block_id=$2 AND part_id=$3',
        dataset_id,
        block_id,
        part_id,
    )
    await connection.execute(
        'INSERT INTO dataset_stats_parts VALUES ($1,$2,$3,$4,$5)',
        dataset_id,
        block_id,
        part_id,
        measured['snapshot'],
        measured['count'],
    )
    rows = [
        field
        for field in measured['fields']
        if field['definition_token'] == current.get(statistics_measure.parent_field(field['field_name'], current))
    ]
    if rows:
        await connection.executemany(
            """INSERT INTO dataset_block_field_stats
               (dataset_id,block_id,part_id,field_name,field_type,definition_token,sample_count,
                min_value,max_value,text_min,text_max,unique_values,values_complete)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
            [
                (
                    dataset_id,
                    block_id,
                    part_id,
                    field['field_name'],
                    field['field_type'],
                    field['definition_token'],
                    field['sample_count'],
                    field['min_value'],
                    field['max_value'],
                    field['text_min'],
                    field['text_max'],
                    field['unique_values'],
                    field['values_complete'],
                )
                for field in rows
            ],
        )
    if part_id + 1 == 1 << split_level:
        await connection.execute(
            """DELETE FROM dataset_stats_pending WHERE dataset_id=$1 AND block_id=$2
               AND txid_visible_in_snapshot(transaction_id,$3::text::txid_snapshot)""",
            dataset_id,
            block_id,
            first_snapshot,
        )
    await connection.execute(
        """INSERT INTO dataset_stats_state(dataset_id,publish_pending) VALUES ($1,TRUE)
           ON CONFLICT (dataset_id) DO UPDATE SET publish_pending=TRUE""",
        dataset_id,
    )


async def _publish_field(connection, dataset_id, field):
    await _lifecycle(connection, dataset_id)
    token = await connection.fetchval(
        f'SELECT {statistics_measure.DEFINITION_TOKEN} FROM dataset_fields '
        "WHERE dataset_id=$1 AND field_name=ANY($2::text[]) AND origin<>'system' "
        'ORDER BY (field_name=$3) DESC LIMIT 1',
        dataset_id,
        [field['field_name'], field.get('parent_field', statistics_measure.parent_field(field['field_name']))],
        field['field_name'],
    )
    if token != field['definition_token']:
        return
    cached = await connection.fetchrow(
        'SELECT *,xmin::text AS token FROM dataset_field_stats WHERE dataset_id=$1 AND field_name=$2 FOR UPDATE NOWAIT',
        dataset_id,
        field['field_name'],
    )
    changed = (cached['token'] if cached else None) != field['cache_token']
    if changed and cached is None:
        return
    if not field['defined']:
        if cached is not None and not changed:
            await connection.execute('DELETE FROM dataset_field_stats WHERE id=$1', cached['id'])
        return
    pending = await connection.fetchval(
        """SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=$1
           AND (field_names IS NULL OR field_names && $2::text[] OR $3))""",
        dataset_id,
        [field['field_name'], field.get('parent_field', statistics_measure.parent_field(field['field_name']))],
        field['field_name'] in statistics_measure.INDEX_TYPES,
    )
    values = field['unique_values']
    minimum, maximum = field['min_value'], field['max_value']
    if cached is not None and pending:
        values = sorted(set(values or []) | set(cached['unique_values'] or []))
        minimum = (
            min(value for value in (minimum, cached['min_value']) if value is not None)
            if minimum is not None or cached['min_value'] is not None
            else None
        )
        maximum = (
            max(value for value in (maximum, cached['max_value']) if value is not None)
            if maximum is not None or cached['max_value'] is not None
            else None
        )
    if field['field_name'] in statistics_measure.INDEX_TYPES:
        registered = {
            definition['field_name'] for definition in await statistics_measure.definitions(connection, dataset_id)
        }
        values = [value for value in values if value in registered]
    cap = statistics_measure.value_limit(field['field_type'])
    complete = (
        field['field_type'] != 'datetime'
        and field['values_complete']
        and (not pending or (cached is not None and cached['values_complete'] is True))
        and (cap is None or len(values) <= cap)
    )
    if field['field_type'] == 'datetime' and values:
        values = [min(values), max(values)]
    published_values = values[:cap]
    if field['field_type'] == 'string' and (
        not field['values_complete']
        or len(values) > cap
        or (pending and cached is not None and cached['unique_values'] is None)
    ):
        published_values = None
        complete = False
    await connection.execute(
        """INSERT INTO dataset_field_stats(dataset_id,field_name,field_type,sample_count,min_value,max_value,
                  unique_values,values_complete,last_updated)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,clock_timestamp())
           ON CONFLICT (dataset_id,field_name) DO UPDATE SET field_type=EXCLUDED.field_type,
               sample_count=EXCLUDED.sample_count,min_value=EXCLUDED.min_value,max_value=EXCLUDED.max_value,
               unique_values=EXCLUDED.unique_values,values_complete=EXCLUDED.values_complete,
               last_updated=EXCLUDED.last_updated""",
        dataset_id,
        field['field_name'],
        field['field_type'],
        field['sample_count'],
        minimum,
        maximum,
        published_values,
        complete,
    )


def handler(pool):
    async def measure(job):
        if job['lane'] != 'maintenance' or job['dataset_id'] is None:
            raise ValueError('Statistics require a dataset and the maintenance lane')
        dataset_id, barrier = job['dataset_id'], job['barrier']
        checkpoint = dict(job['checkpoint'])
        phase = checkpoint.get('phase', 'blocks')
        if phase == 'blocks':
            block_id = checkpoint.get('block')
            if block_id is None:
                block_id = await _next_block(pool, dataset_id, barrier, checkpoint.get('after_block', -1))
                if block_id is None:
                    names = await statistics_measure.read_rows(
                        pool,
                        """SELECT field_name FROM (
                             SELECT field_name FROM dataset_block_field_stats WHERE dataset_id=$1
                             UNION SELECT field_name FROM dataset_field_stats WHERE dataset_id=$1
                             UNION SELECT field_name FROM dataset_fields WHERE dataset_id=$1 AND origin<>'system'
                             UNION SELECT 'tags' UNION SELECT 'media_type'
                             UNION SELECT unnest($2::text[])) names
                           ORDER BY (field_name=ANY($2::text[])) DESC, field_name COLLATE "C"
                        """,
                        dataset_id,
                        list(statistics_measure.INDEX_TYPES),
                    )

                    async def finish_blocks(connection):
                        await _lifecycle(connection, dataset_id)
                        await connection.execute(
                            """DELETE FROM dataset_stats_pending WHERE dataset_id=$1 AND block_id=-1
                               AND txid_visible_in_snapshot(transaction_id,$2::text::txid_snapshot)""",
                            dataset_id,
                            barrier['snapshot'],
                        )
                        await connection.execute(
                            """INSERT INTO dataset_stats_state(dataset_id,initialized,publish_pending)
                               VALUES ($1,TRUE,TRUE) ON CONFLICT (dataset_id) DO UPDATE SET
                               initialized=dataset_stats_state.initialized OR $2,publish_pending=TRUE""",
                            dataset_id,
                            barrier['full'],
                        )

                    return StepResult(
                        checkpoint={
                            'phase': 'fields',
                            'fields': [row['field_name'] for row in names],
                            'field_index': 0,
                        },
                        publish=finish_blocks,
                    )

                async def prepare(connection):
                    await connection.execute(
                        'INSERT INTO dataset_stats_blocks(dataset_id,block_id) VALUES ($1,$2) ON CONFLICT DO NOTHING',
                        dataset_id,
                        block_id,
                    )
                    await connection.execute('SELECT record_stats_work($1,$2,NULL)', dataset_id, block_id)

                return StepResult(checkpoint={**checkpoint, 'block': block_id, 'part': 0}, publish=prepare)
            split_level = await statistics_measure.read_value(
                pool,
                'SELECT split_level FROM dataset_stats_blocks WHERE dataset_id=$1 AND block_id=$2',
                dataset_id,
                block_id,
            )
            part_id = checkpoint.get('part', 0)
            try:
                measured = await statistics_measure.measure_part(pool, dataset_id, block_id, part_id, split_level)
            except statistics_measure.MeasurementTooLarge:
                if split_level >= 12:
                    raise ValueError(f'Statistics cannot measure one sample in block {block_id} within its budget')

                async def subdivide(connection):
                    await connection.execute(
                        'UPDATE dataset_stats_blocks SET split_level=split_level+1 WHERE dataset_id=$1 AND block_id=$2',
                        dataset_id,
                        block_id,
                    )
                    await connection.execute(
                        'DELETE FROM dataset_stats_parts WHERE dataset_id=$1 AND block_id=$2',
                        dataset_id,
                        block_id,
                    )

                checkpoint.pop('first_snapshot', None)
                return StepResult(
                    checkpoint={**checkpoint, 'part': 0, 'split_level': split_level + 1},
                    message='Subdividing a large statistics block',
                    publish=subdivide,
                )
            first_snapshot = checkpoint.get('first_snapshot', measured['snapshot'])

            async def publish(connection):
                await _publish_part(connection, job, block_id, part_id, split_level, measured, first_snapshot)

            if part_id + 1 < 1 << split_level:
                checkpoint.update(part=part_id + 1, first_snapshot=first_snapshot)
            else:
                checkpoint = {'phase': 'blocks', 'after_block': block_id}
            return StepResult(
                checkpoint=checkpoint,
                done=job['progress_done'] + 1,
                message='Measuring statistics blocks',
                publish=publish,
            )
        index = checkpoint['field_index']
        name = checkpoint['fields'][index] if index < len(checkpoint['fields']) else None
        if name is not None:
            page_size = checkpoint.get('aggregate_page_size', 128)
            try:
                field = await statistics_measure.aggregate_field(
                    pool,
                    dataset_id,
                    name,
                    checkpoint.get('aggregate'),
                    page_size,
                )
            except statistics_measure.MeasurementTooLarge:
                if page_size <= 1:
                    raise ValueError(f'Statistics cannot aggregate one cached part for {name!r} within its budget')
                return StepResult(
                    checkpoint={**checkpoint, 'aggregate_page_size': max(1, page_size // 2)},
                    message='Reducing statistics aggregation unit size',
                )
            if field['after_part'] is not None:
                return StepResult(
                    checkpoint={**checkpoint, 'aggregate': field},
                    message='Combining statistics blocks',
                )

            async def publish_field(connection):
                await _publish_field(connection, dataset_id, field)

            checkpoint.pop('aggregate', None)
            checkpoint.pop('aggregate_page_size', None)
            return StepResult(
                checkpoint={**checkpoint, 'field_index': index + 1},
                message='Publishing filter statistics',
                publish=publish_field,
            )

        async def finish(connection):
            await _lifecycle(connection, dataset_id)
            await connection.fetchrow('SELECT id FROM datasets WHERE id=$1 FOR NO KEY UPDATE NOWAIT', dataset_id)
            await connection.execute(
                """UPDATE datasets SET stats_last_updated=clock_timestamp(),
                   stats_need_refresh=EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=$1),
                   stats_full_refreshed_at=CASE WHEN $2 AND stats_full_requests=$3
                       AND NOT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=$1)
                       THEN clock_timestamp() ELSE stats_full_refreshed_at END WHERE id=$1""",
                dataset_id,
                barrier['full'],
                barrier['stats_full_requests'],
            )
            await connection.execute(
                'UPDATE dataset_stats_state SET publish_pending=FALSE WHERE dataset_id=$1',
                dataset_id,
            )

        count = await statistics_measure.read_value(
            pool,
            'SELECT count(*) FROM dataset_field_stats WHERE dataset_id=$1',
            dataset_id,
        )
        result: dict[str, Any] = {
            'requested_dataset_refreshed': True,
            'refreshed_count': 1,
            'datasets': [{'dataset_id': dataset_id, 'fields_updated': count, 'error': None}],
        }
        return StepResult(finished=True, result=result, message='Statistics updated', publish=finish)

    return measure
