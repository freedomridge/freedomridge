"""Finite view mutations. A cancelled job retains its committed prefix and frozen target."""

import re
from contextlib import suppress
from datetime import datetime

from fastapi import HTTPException

from . import job_queue, local_writes
from .diskspace import INGEST_ROW_BYTES, require_free_space
from .field_registry import (
    LOCAL_PREFIX,
    invalidate_values,
    lock_field_lifecycle,
    prune_fields,
    register_fields,
)
from .job_runner import StepResult
from .label_fields import append_discovery, copy_discovery

KIND = 'bulk_mutation'
BATCH_SIZE = 1000
# Explicit targets are inserted after the lifecycle gate, this many per transaction.
STAGE_BATCH_SIZE = 50000
POLICY = job_queue.JobPolicy(max_unit_seconds=15, max_failures=5, retry_seconds=2)
_SAMPLE_IDS = range(-(2**31), 2**31)


def _count_key(operation):
    return {'delete': 'deleted_count', 'delete_embeddings': 'deleted'}.get(operation, 'updated_count')


def _matches_every_sample(predicate):
    """True when a compiled filter cannot exclude a sample. parse_filter_node compiles a condition it can't
    apply (an empty group, a comparison without a value) to TRUE, so a filter of only such conditions, or an
    OR with one of them, targets the whole dataset. Compiled filters contain no other TRUE."""
    previous = None
    while predicate != previous:
        previous = predicate
        predicate = re.sub(r'\(\s*TRUE\s*\)', 'TRUE', predicate)
        predicate = re.sub(r'\bTRUE\s+AND\s+TRUE\b', 'TRUE', predicate)
    return predicate.strip() == 'TRUE' or re.search(r'\bTRUE\s+OR\b|\bOR\s+TRUE\b', predicate) is not None


def _require_sample_ids(values):
    # Checked before anything is queued, so staging cannot fail on input.
    if not all(type(value) is int and value in _SAMPLE_IDS for value in values):
        raise HTTPException(400, 'Sample IDs must be 32-bit integers')


def _freeze_value(value):
    """Tag every container so user JSON cannot collide with the datetime encoding."""
    if isinstance(value, datetime):
        return ['datetime', value.isoformat()]
    if isinstance(value, list):
        return ['list', [_freeze_value(item) for item in value]]
    if isinstance(value, dict):
        return ['dict', {key: _freeze_value(item) for key, item in value.items()}]
    return ['value', value]


def _restore_value(encoded):
    kind, value = encoded
    if kind == 'datetime':
        return datetime.fromisoformat(value)
    if kind == 'list':
        return [_restore_value(item) for item in value]
    if kind == 'dict':
        return {key: _restore_value(item) for key, item in value.items()}
    return value


async def require_no_pending_bulk(connection, dataset_id, *, fields_only=False):
    # Stopped jobs retain their target for resume until they are resumed or discarded.
    blocking = await connection.fetch(
        """SELECT id,state,args->>'operation' AS operation,
                  (SELECT username FROM users WHERE users.id=jobs.created_by) AS username
           FROM jobs WHERE dataset_id=$1 AND kind='bulk_mutation' AND state<>'succeeded'
             AND NOT COALESCE(result,'{}'::jsonb) ? 'discarded'
             AND (NOT $2::boolean OR args->>'operation' IN ('rename','clone','clear'))
           ORDER BY created_at,id LIMIT 3""",
        dataset_id,
        fields_only,
    )
    if blocking:
        names = '; '.join(
            f"{row['operation']} job {row['id']} ({row['state']}, by {row['username'] or 'a deleted user'})"
            for row in blocking
        )
        raise HTTPException(
            409,
            f'Unfinished bulk jobs block this operation: {names}. Let queued or running jobs finish, or cancel '
            'them; then resume or discard stopped jobs in Job history (an administrator or the submitter can).',
        )


def selection_tokens(node):
    if not isinstance(node, dict):
        return set()
    tokens = {node['value']} if node.get('operator') == 'in_selection' and isinstance(node.get('value'), str) else set()
    for child in node.get('filters', []):
        tokens.update(selection_tokens(child))
    return tokens


async def submit(pool, dataset_id, user_id, operation, payload):
    from .routers import datasets

    payload = dict(payload)
    ids = payload.pop('sample_ids', [])
    updates = payload.pop('updates', None)
    patches = {item['sample_id']: item['metadata'] for item in updates} if updates is not None else None
    if patches is not None:
        ids = list(patches)
    node = payload.get('filters') or {}
    id_filter = node.get('field') == 'id' and node.get('operator') == 'in' and not {'filters', 'logic'} & node.keys()
    if id_filter:
        values = node.get('value', [])
        values = values if isinstance(values, list) else [values]
        _require_sample_ids(values)
        if ids or patches is not None:
            members = set(values)
            ids = [sid for sid in ids if sid in members]
        else:
            ids = values
        # The targets are the exact membership. Re-sending the list as each unit's filter
        # costs a linear scan per row once PostgreSQL switches to a generic plan.
        payload['filters'] = None
    ids = list(dict.fromkeys(ids))
    _require_sample_ids(ids)
    tokens = selection_tokens(payload.get('filters'))
    if payload.get('selection_token'):
        tokens.add(payload['selection_token'])
    # Freeze the validated expression and catalog interpretation, not a mutable saved view ID.
    # The dataset ID scopes cross-grain subqueries exactly as the grid's own query does.
    args: list = []
    predicate, _ = (
        datasets.parse_filter_node(
            payload.get('filters') or {},
            args,
            1,
            None,
            None,
            dataset_id,
            array_columns=await datasets.get_dataset_array_columns(dataset_id),
        )
        if payload.get('filters')
        else ('TRUE', 1)
    )
    predicate = predicate.replace("AND rs.created_at > statement_timestamp() - interval '2 hours'", '')
    if operation == 'delete_embeddings' and payload.get('filters') and _matches_every_sample(predicate):
        # Only filters == {} deletes a whole field (delete_embedding_field); a filter is never a silent everything.
        raise HTTPException(
            400,
            'The filter has a condition without a value, so it would select every sample. Complete the filter, '
            'or delete the whole field with filters {} (delete_embedding_field)',
        )
    try:
        async with job_queue._transaction(pool) as connection:
            # Serialize with index builds and reverts before taking the lifecycle gate.
            if not await connection.fetchval('SELECT pg_try_advisory_xact_lock(20261017,$1)', dataset_id):
                raise job_queue.QueueContended('Dataset has an active index operation or revert; retry')
            # Poll briefly for in-flight ingest without queueing behind it.
            await lock_field_lifecycle(connection, dataset_id, exclusive=True, wait=False)
            await connection.execute('SELECT require_writable_dataset($1)', dataset_id)
            # Cleanup and pin admission serialize; no unbounded token copy is needed.
            if tokens:
                await connection.execute('LOCK TABLE bulk_selection_pins IN SHARE ROW EXCLUSIVE MODE')
                for token in sorted(tokens):
                    await datasets._require_live_selection(token, connection)
            if operation in ('rename', 'clone', 'clear'):
                await connection.execute("SELECT set_config('fridge.bulk_dataset',$1,TRUE)", str(dataset_id))
                await connection.execute('SET LOCAL enable_seqscan=off')
                field = payload['field_name']
                datasets._validate_field_name(field)
                source = await connection.fetchrow(
                    'SELECT * FROM dataset_fields WHERE dataset_id=$1 AND field_name=$2',
                    dataset_id,
                    field,
                )
                if source is not None and source['origin'] != 'metadata':
                    raise HTTPException(400, 'Field operations require a metadata field')
                if source is not None and source['is_promoted']:
                    raise HTTPException(409, 'Demote the field before a bulk field operation')
                if operation != 'clear':
                    if source is None:
                        raise HTTPException(404, f"Field '{field}' is not registered")
                    await datasets._check_field_target(
                        dataset_id,
                        field,
                        payload['new_name'],
                        payload['overwrite'],
                        connection,
                    )
                    await connection.execute(
                        """INSERT INTO dataset_fields
                           (dataset_id,field_name,field_type,display_type,origin,overlay_config)
                           SELECT dataset_id,$3,field_type,display_type,origin,overlay_config FROM dataset_fields
                           WHERE dataset_id=$1 AND field_name=$2 ON CONFLICT(dataset_id,field_name) DO UPDATE
                           SET field_type=EXCLUDED.field_type,display_type=EXCLUDED.display_type,
                               overlay_config=EXCLUDED.overlay_config""",
                        dataset_id,
                        field,
                        payload['new_name'],
                    )
                    # Copied now like the definition; a rename drops the old name's entries when it is gone.
                    await datasets._carry_segmentation_entries(
                        connection,
                        dataset_id,
                        field,
                        payload['new_name'],
                        keep_source=True,
                    )
            cutoff = (
                await connection.fetchval(
                    'SELECT id FROM samples WHERE dataset_id=$1 ORDER BY id DESC LIMIT 1',
                    dataset_id,
                )
                or 0
            )
            job = await job_queue.enqueue_job(
                connection,
                KIND,
                dataset_id=dataset_id,
                created_by=user_id,
                args={
                    **payload,
                    'operation': operation,
                    'explicit': bool(ids)
                    or id_filter
                    or updates is not None
                    or (
                        operation in ('set', 'tag', 'delete', 'delete_embeddings')
                        and payload.get('filters') is None
                        and not payload.get('selection_token')
                    ),
                    'predicate': predicate,
                    'typed_predicate_args': _freeze_value(args),
                },
                barrier={'max_id': cutoff},
            )
            if ids:
                # Unclaimable until its targets are staged after the gate is released.
                await connection.execute(
                    """UPDATE jobs SET available_at='infinity',checkpoint='{"staging":true}' WHERE id=$1""",
                    job['id'],
                )
            for token in tokens:
                await connection.execute('INSERT INTO bulk_selection_pins VALUES($1,$2)', job['id'], token)
    except job_queue.QueueContended as error:
        raise HTTPException(409, f'{error}', headers={'Retry-After': '1'}) from error
    except job_queue.QueueBusy as error:
        raise HTTPException(409, 'Dataset has an active operation; retry', headers={'Retry-After': '1'}) from error
    if ids:
        await _stage_targets(pool, dataset_id, job['id'], ids, patches)
    return {'job_id': str(job['id']), 'state': job['state']}


async def _stage_targets(pool, dataset_id, job_id, ids, patches):
    """Bounded inserts outside the lifecycle gate: units ignore targets above the captured cutoff."""
    try:
        for start in range(0, len(ids), STAGE_BATCH_SIZE):
            batch = ids[start : start + STAGE_BATCH_SIZE]
            async with job_queue._transaction(pool) as connection:
                # Deletion drains job targets once sealed; stop adding any after that.
                await connection.execute('SELECT require_writable_dataset($1)', dataset_id)
                await connection.execute(
                    'INSERT INTO bulk_job_targets(job_id,sample_id,patch) '
                    'SELECT $1,id,patch FROM unnest($2::int[],$3::jsonb[]) t(id,patch)',
                    job_id,
                    batch,
                    [patches[sid] for sid in batch] if patches is not None else [None] * len(batch),
                )
        async with job_queue._transaction(pool) as connection:
            await connection.execute(
                """UPDATE jobs SET available_at=clock_timestamp(),updated_at=clock_timestamp(),
                   checkpoint=checkpoint-'staging' WHERE id=$1 AND state='queued'""",
                job_id,
            )
    except Exception as error:
        # A partial target must never run: discard it so cleanup releases what was staged.
        with suppress(Exception):
            async with job_queue._transaction(pool) as connection:
                await connection.execute(
                    """UPDATE jobs SET state='cancelled',finished_at=clock_timestamp(),updated_at=clock_timestamp(),
                       result=COALESCE(result,'{}'::jsonb)||$2::jsonb WHERE id=$1 AND state='queued'""",
                    job_id,
                    {'discarded': {'reason': 'Submission did not finish staging its targets'}},
                )
        if isinstance(error, job_queue.QueueBusy):
            raise HTTPException(409, 'Dataset has an active operation; retry', headers={'Retry-After': '1'}) from error
        raise


async def discard(pool, job_id, user):
    """Release a stopped job's remaining target and resumability; committed chunks stay as they are."""
    async with job_queue._transaction(pool) as connection:
        job = await connection.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE NOWAIT', job_id)
        if job['kind'] != KIND:
            raise HTTPException(400, 'This job is not a bulk mutation')
        if job['state'] not in ('failed', 'cancelled'):
            raise HTTPException(409, 'Cancel the bulk job and wait for it to stop before discarding it')
        if 'discarded' not in (job['result'] or {}):
            # Pins are one row per token; selection cleanup releases the targets in bounded units.
            await connection.execute('DELETE FROM bulk_selection_pins WHERE job_id=$1', job_id)
            await connection.execute(
                """UPDATE jobs SET updated_at=clock_timestamp(),message=$3,
                   result=COALESCE(result,'{}'::jsonb)||jsonb_build_object('discarded',
                       jsonb_build_object('by',$2::text,'user_id',$4::int,'at',clock_timestamp()))
                   WHERE id=$1""",
                job_id,
                user.get('username'),
                f"Discarded by {user.get('username') or 'user ' + str(user['id'])}",
                user['id'],
            )
        return {'job_id': str(job_id), 'state': job['state'], 'discarded': True}


async def resume(pool, job_id):
    async with job_queue._transaction(pool) as connection:
        job = await connection.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE NOWAIT', job_id)
        if job['kind'] != KIND:
            raise HTTPException(400, 'This job is not a bulk mutation')
        if not await connection.fetchval('SELECT id FROM datasets WHERE id=$1 FOR KEY SHARE', job['dataset_id']):
            raise HTTPException(404, 'Dataset not found')
        await connection.execute('SELECT require_writable_dataset($1)', job['dataset_id'])
        if job['state'] not in ('failed', 'cancelled'):
            raise HTTPException(409, 'Wait for the bulk job to stop before resuming')
        if 'discarded' in (job['result'] or {}):
            raise HTTPException(409, 'This bulk job was discarded; submit the operation again')
        if job['checkpoint'].get('staging'):
            raise HTTPException(409, 'This bulk job was never fully submitted; discard it and submit it again')
        await connection.execute(
            """UPDATE jobs SET state='queued',error=NULL,failures=0,contentions=0,
               lease_token=NULL,lease_expires_at=NULL,finished_at=NULL,
               available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=$1""",
            job_id,
        )
        await connection.execute('UPDATE job_controls SET cancel_requested=FALSE WHERE job_id=$1', job_id)
        return {'job_id': str(job_id), 'state': 'queued'}


async def _mutate_local(connection, job, ids):
    """A set or tag job on a mirror's own fields (`@local.`): this unit writes local_metadata or local_tags only. The
    unit holds the field lifecycle gate shared (handler's publish); removed fields are unregistered by the job's last
    steps, which read the dataset in bounded windows and take the gate exclusively only to unregister."""
    request, dataset_id = job['args'], job['dataset_id']
    await local_writes.require_mirror(connection, dataset_id)  # it may have stopped since the job was queued
    await connection.execute("SELECT set_config('fridge.mirror_local', $1, TRUE)", str(dataset_id))
    if request['operation'] == 'tag':
        status = await connection.execute(
            """UPDATE samples SET local_tags = ARRAY(SELECT DISTINCT unnest(COALESCE(local_tags, '{}') || $3::text[])
                                              EXCEPT SELECT unnest($4::text[]))
               WHERE dataset_id = $1 AND id = ANY($2::int[])
                 AND (NOT COALESCE(local_tags, '{}') @> $3::text[] OR local_tags && $4::text[])""",
            dataset_id,
            ids,
            request['add_tags'],
            request['remove_tags'],
        )
        await local_writes.register_tags(connection, dataset_id, request['add_tags'], job['created_by'])
        await local_writes.finish(connection, dataset_id, ['tags'])
        return int(status.split()[-1])
    remove = request['remove_metadata_keys']
    if request.get('per_sample'):
        rows = await connection.fetch(
            """UPDATE samples s SET local_metadata = s.local_metadata || t.patch
               FROM bulk_job_targets t WHERE t.job_id = $1 AND t.sample_id = s.id
               AND s.dataset_id = $2 AND s.id = ANY($3::int[]) RETURNING t.patch""",
            job['id'],
            dataset_id,
            ids,
        )
        written, count = [row['patch'] for row in rows], len(rows)
    else:
        status = await connection.execute(
            """UPDATE samples SET local_metadata = (local_metadata || $3::jsonb) - $4::text[]
               WHERE dataset_id = $1 AND id = ANY($2::int[])""",
            dataset_id,
            ids,
            request['metadata'],
            remove,
        )
        written = [{name: value for name, value in request['metadata'].items() if name not in remove}]
        count = int(status.split()[-1])
    if count:
        await local_writes.register(connection, dataset_id, written)
    await local_writes.finish(connection, dataset_id, {name for patch in written for name in patch}, remove)
    return count


async def _mutate(connection, job, ids, removed_fields):
    from .routers import datasets

    request = job['args']
    operation = request['operation']
    dataset_id = job['dataset_id']
    if request.get('local'):
        return await _mutate_local(connection, job, ids)
    if operation == 'delete':
        removed = await connection.fetch(
            'DELETE FROM samples WHERE dataset_id=$1 AND id=ANY($2::int[]) RETURNING metadata',
            dataset_id,
            ids,
        )
        names = {'tags'} | {key for row in removed for key in row['metadata']}
        removed_fields.update(key for row in removed for key in row['metadata'])
        await invalidate_values(connection, dataset_id, names)
        return len(removed)
    if operation == 'delete_embeddings':
        # Cascades remove the vectors' coordinates and queued projections; triggers keep map rank rows and counts.
        status = await connection.execute(
            'DELETE FROM embeddings WHERE dataset_id=$3 AND field_name=$1 AND sample_id=ANY($2::int[])',
            request['field_name'],
            ids,
            dataset_id,
        )
        return int(status.split()[-1])
    if operation == 'tag':
        col = request['field_name'].replace('"', '""')
        status = await connection.execute(
            f'''UPDATE samples SET "{col}"=ARRAY(SELECT DISTINCT unnest(COALESCE("{col}",'{{}}') || $3::text[])
                    EXCEPT SELECT unnest($4::text[])),last_modified_at=NOW(),modified_by_user_id=$5
                WHERE dataset_id=$1 AND id=ANY($2::int[]) AND (
                    NOT COALESCE("{col}",'{{}}') @> $3::text[] OR "{col}" && $4::text[])''',
            dataset_id,
            ids,
            request['add_tags'],
            request['remove_tags'],
            job['created_by'],
        )
        if request['add_tags']:
            await connection.execute(
                """INSERT INTO dataset_tags(dataset_id,field_name,tag,created_by_user_id,created_by_name,last_used_at)
                   SELECT $1,$2,unnest($3::text[]),$4,$5,NOW()
                   ON CONFLICT(dataset_id,field_name,tag) DO UPDATE SET last_used_at=EXCLUDED.last_used_at""",
                dataset_id,
                request['field_name'],
                request['add_tags'],
                job['created_by'],
                request.get('made_by'),  # a follower's edit: who made it there (routers/datasets.py tag_samples)
            )
        await invalidate_values(connection, dataset_id, [request['field_name']])
        return int(status.split()[-1])
    names = set(request.get('metadata', {})) | set(request.get('remove_metadata_keys', []))
    if operation == 'set':
        if request.get('per_sample'):
            rows = await connection.fetch(
                """UPDATE samples s SET metadata=COALESCE(s.metadata,'{}') || t.patch
                   FROM bulk_job_targets t WHERE t.job_id=$1 AND t.sample_id=s.id
                   AND s.dataset_id=$2 AND s.id=ANY($3::int[]) RETURNING t.patch""",
                job['id'],
                dataset_id,
                ids,
            )
            patches = [row['patch'] for row in rows]
            count = len(rows)
            names.update(key for patch in patches for key in patch)
        else:
            patch = {k: v for k, v in request['metadata'].items() if k not in request['remove_metadata_keys']}
            status = await connection.execute(
                """UPDATE samples SET metadata=(COALESCE(metadata,'{}') || $3::jsonb)-$4::text[]
                   WHERE dataset_id=$1 AND id=ANY($2::int[])""",
                dataset_id,
                ids,
                request['metadata'],
                request['remove_metadata_keys'],
            )
            patches, count = [patch], int(status.split()[-1])
        if count:
            await register_fields(connection, dataset_id, patches)
    else:
        field = request['field_name']
        names.add(field)
        expression = (
            'metadata-$3'
            if operation == 'clear'
            else (
                "(metadata-$3) || jsonb_build_object($4::text,metadata->$3)"
                if operation == 'rename'
                else "metadata || jsonb_build_object($4::text,metadata->$3)"
            )
        )
        values = [dataset_id, ids, field]
        if operation != 'clear':
            values.append(request['new_name'])
            names.add(request['new_name'])
        status = await connection.execute(
            f'UPDATE samples SET metadata={expression} WHERE dataset_id=$1 AND id=ANY($2::int[]) AND metadata ? $3',
            *values,
        )
        count = int(status.split()[-1])
    if operation in ('rename', 'clone', 'clear'):
        await connection.execute(
            'SELECT record_stats_work($1,block,$3::text[]) FROM '
            '(SELECT DISTINCT id::bigint/4096 AS block FROM unnest($2::int[]) t(id)) b',
            dataset_id,
            ids,
            sorted(names),
        )
    # All writers acquire discovery rows before vocabulary rows (as ingest does).
    if operation == 'set':
        await append_discovery(connection, dataset_id, patches)
    elif operation in ('rename', 'clone'):
        await copy_discovery(connection, dataset_id, request['field_name'], request['new_name'])
    await invalidate_values(connection, dataset_id, names)
    if operation in ('rename', 'clone', 'clear'):
        await datasets._invalidate_field_derivations(connection, dataset_id, names)
    return count


def handler(pool):
    async def measure(job):
        if job['kind'] != KIND or job['lane'] != 'user':
            raise ValueError('Bulk mutations require the user lane')
        request = job['args']
        checkpoint = dict(job['checkpoint'])
        cursor = checkpoint.get('cursor', 0)
        size = max(1, BATCH_SIZE // (2 ** min(job['failures'], 10)))
        async with job_queue._transaction(pool) as connection:
            if request['explicit']:
                rows = await connection.fetch(
                    'SELECT sample_id AS id FROM bulk_job_targets WHERE job_id=$1 AND sample_id>$2 '
                    'AND sample_id<=$3 ORDER BY sample_id LIMIT $4',
                    job['id'],
                    cursor,
                    job['barrier']['max_id'],
                    size,
                )
            else:
                rows = await connection.fetch(
                    'SELECT id FROM samples WHERE dataset_id=$1 AND id>$2 AND id<=$3 ORDER BY id LIMIT $4',
                    job['dataset_id'],
                    cursor,
                    job['barrier']['max_id'],
                    size,
                )
        if not rows:
            names = list(
                dict.fromkeys([*request.get('remove_metadata_keys', []), *checkpoint.get('removed_fields', [])]),
            )
            if request['operation'] in ('rename', 'clear'):
                names.append(request['field_name'])
            index = checkpoint.get('pruned', 0)
            if index < len(names) and request.get('local'):
                # A mirror's own field (names without the prefix): one window of the walk per step, read outside the
                # gate (local_metadata has no index); only the unregister takes it. The field's row xmin when the
                # walk began is kept: a write of the field since touches the row, and the unregister then keeps it.
                after, mark = checkpoint.get('prune_after', 0), checkpoint.get('prune_mark')
                async with job_queue._transaction(pool) as connection:
                    if after == 0:
                        mark = await connection.fetchval(
                            'SELECT xmin::text FROM dataset_fields WHERE dataset_id=$1 AND field_name=$2',
                            job['dataset_id'],
                            LOCAL_PREFIX + names[index],
                        )
                    found, last = await local_writes.holds(connection, job['dataset_id'], names[index], after)
                done_name = {**checkpoint, 'pruned': index + 1, 'prune_after': 0, 'prune_mark': None}
                if found or mark is None:  # still held, or not registered at all
                    return StepResult(checkpoint=done_name)
                if last is not None:
                    return StepResult(checkpoint={**checkpoint, 'prune_after': last, 'prune_mark': mark})

                async def unregister_local(connection):
                    if not await connection.fetchval(
                        'SELECT pg_try_advisory_xact_lock(20261016,$1)',
                        job['dataset_id'],
                    ):
                        raise job_queue.QueueContended('Waiting to prune the field definition')
                    await local_writes.unregister(connection, job['dataset_id'], names[index], mark)

                return StepResult(checkpoint=done_name, publish=unregister_local)
            if index < len(names):

                async def prune(connection):
                    if not await connection.fetchval(
                        'SELECT pg_try_advisory_xact_lock(20261016,$1)',
                        job['dataset_id'],
                    ):
                        raise job_queue.QueueContended('Waiting to prune the field definition')
                    # The metadata GIN index answers key existence, including later arrivals.
                    # Do not let an empty-key probe become a whole-dataset sequential scan.
                    await connection.execute("SELECT set_config('fridge.bulk_dataset',$1,TRUE)", str(job['dataset_id']))
                    await connection.execute('SET LOCAL enable_seqscan=off')
                    if request['operation'] in ('rename', 'clear'):
                        present = await connection.fetchval(
                            'SELECT 1 FROM samples WHERE dataset_id=$1 AND metadata ? $2 LIMIT 1',
                            job['dataset_id'],
                            names[index],
                        )
                        if not present:
                            await connection.execute(
                                'DELETE FROM dataset_fields WHERE dataset_id=$1 AND field_name=$2',
                                job['dataset_id'],
                                names[index],
                            )
                            await connection.execute(
                                'DELETE FROM dataset_field_stats WHERE dataset_id=$1 AND field_name=$2',
                                job['dataset_id'],
                                names[index],
                            )
                            if request['operation'] == 'rename' and names[index] == request['field_name']:
                                from .routers import datasets

                                await datasets._carry_segmentation_entries(
                                    connection,
                                    job['dataset_id'],
                                    names[index],
                                    None,
                                    keep_source=False,
                                )
                    else:
                        await prune_fields(connection, job['dataset_id'], [names[index]])

                return StepResult(checkpoint={**checkpoint, 'pruned': index + 1}, publish=prune)
            # Targets are job-owned and can be large: dispose in bounded units before success.
            async with job_queue._transaction(pool) as connection:
                targets = await connection.fetch(
                    'SELECT sample_id FROM bulk_job_targets WHERE job_id=$1 ORDER BY sample_id LIMIT $2',
                    job['id'],
                    size,
                )

            async def cleanup(connection):
                if targets:
                    await connection.execute(
                        'DELETE FROM bulk_job_targets WHERE job_id=$1 AND sample_id=ANY($2::int[])',
                        job['id'],
                        [r['sample_id'] for r in targets],
                    )
                else:
                    await connection.execute('DELETE FROM bulk_selection_pins WHERE job_id=$1', job['id'])

            count = checkpoint.get('affected', 0)
            operation = request['operation']
            result = {_count_key(operation): count}
            if operation in ('set', 'tag'):
                result['message'] = f"Updated {request.get('field_name', 'metadata')} for {count} samples"
            if operation == 'rename':
                result['promoted'] = False
            return StepResult(
                checkpoint=checkpoint,
                finished=not targets,
                result=result,
                message='Bulk operation complete' if not targets else 'Cleaning up targets',
                publish=cleanup,
            )
        ids = [row['id'] for row in rows]
        if request['operation'] == 'set':
            async with pool.acquire(timeout=1) as connection:
                await require_free_space(
                    connection,
                    len(ids) * INGEST_ROW_BYTES,
                    f'setting metadata on {len(ids)} samples',
                )
        checkpoint.update(cursor=ids[-1], examined=checkpoint.get('examined', 0) + len(ids), last_unit_rows=len(ids))

        progress = {_count_key(request['operation']): checkpoint.get('affected', 0)}

        async def publish(connection):
            if not await connection.fetchval('SELECT pg_try_advisory_xact_lock_shared(20261016,$1)', job['dataset_id']):
                raise job_queue.QueueContended('Waiting for field definitions')
            # Deleted samples and vectors cascade to map coordinates, whose triggers take the per-dataset rank
            # fence (20261020) shared. Take it first, as they would: a full rank rebuild holds
            # 20261016 shared and then 20261020 exclusive, so waiting inside the cascade can deadlock.
            if request['operation'] in ('delete', 'delete_embeddings') and not await connection.fetchval(
                'SELECT pg_try_advisory_xact_lock_shared(20261020,$1)',
                job['dataset_id'],
            ):
                raise job_queue.QueueContended('Waiting for a map rank rebuild')
            # Long ID lists (exclusions, `in` filters) bind as parameters. A generic plan scans
            # them linearly per row; a custom plan hashes them.
            await connection.execute('SET LOCAL plan_cache_mode = force_custom_plan')
            # Older jobs used JSON-only parameters; preserve their resumability.
            args = (
                _restore_value(request['typed_predicate_args'])
                if 'typed_predicate_args' in request
                else list(request['predicate_args'])
            )
            where = request['predicate']
            args.extend([job['dataset_id'], ids, request.get('excluded_ids', [])])
            n = len(args)
            where += f' AND dataset_id=${n - 2} AND id=ANY(${n - 1}::int[]) AND NOT(id=ANY(${n}::int[]))'
            if request.get('selection_token'):
                args.append(request['selection_token'])
                where += f' AND id IN (SELECT sample_id FROM region_selections WHERE token=${len(args)})'
            # PostgreSQL rechecks row predicates after waiting for a concurrent updater.
            # Nonmatches never get row locks (and never dirty their heap pages). Only delete changes
            # a key: edits leave FK writers (embeddings, hashes, scores) for matching rows unblocked.
            lock = 'FOR UPDATE' if request['operation'] == 'delete' else 'FOR NO KEY UPDATE'  # vectors keep sample keys
            selected = await connection.fetch(f'SELECT id FROM samples WHERE {where} ORDER BY id {lock}', *args)
            # Re-evaluate subqueries in a fresh snapshot too, retaining only locked rows.
            args[n - 2] = [row['id'] for row in selected]
            selected = await connection.fetch(f'SELECT id FROM samples WHERE {where}', *args) if selected else []
            removed_fields = set(checkpoint.get('removed_fields', []))
            affected = (
                await _mutate(connection, job, [row['id'] for row in selected], removed_fields) if selected else 0
            )
            if removed_fields:
                checkpoint['removed_fields'] = sorted(removed_fields)
            checkpoint['affected'] = checkpoint.get('affected', 0) + affected
            progress[_count_key(request['operation'])] = checkpoint['affected']

        return StepResult(
            checkpoint=checkpoint,
            done=checkpoint['examined'],
            result=progress,
            message=f"{request['operation'].capitalize()}: examining captured samples",
            publish=publish,
        )

    return measure
