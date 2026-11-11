"""Index advisor actions: remove an index, and restore one the advisor removed, as jobs.

Removal is `DROP INDEX CONCURRENTLY` on the job's own connection: it holds SHARE UPDATE EXCLUSIVE on the
table, which conflicts with neither reads nor writes, so ingest keeps going while it waits for older
transactions (a running pg_dump included). A plain `DROP INDEX` would queue an ACCESS EXCLUSIVE request
behind such a reader, and every later read and write of the table would queue behind it.

- `index_drop`, two units. 1: re-check the index is removable and unchanged since the administrator saw
  it, then, in one fenced publication, record what is removed (`removed_indexes`, unless it is a plain
  field demote) and, for a field index, mark the field not indexed (a promoted field without its index
  would turn the sidebar's index descent into one scan per value). 2: wait out a database backup (no
  failure counted), then drop concurrently under the index's advisory lock and publish `dropped_at`.
  An interrupted drop can leave the index INVALID; the retry, or Remove again, drops it (IF EXISTS).
- `index_restore`: `CREATE INDEX CONCURRENTLY IF NOT EXISTS` from the recorded `pg_get_indexdef`, through
  index_jobs._build (an INVALID leftover is dropped first; a failed or cancelled build removes its own),
  verified valid and identical, after a free-space check and outside a backup. A field index is restored
  by the field index build (promote), which re-derives the index from the field's current definition.

Locks: a field index uses its dataset's index-operation guard (20261017, like builds and demote); every
removal and restore also holds its table's index lock (index_jobs.TABLE_LOCK, 20261067 by table), which
every concurrent index build or drop on that table takes with a try-lock and never waits for: a busy
table defers the job (no failure counted), so no CREATE or DROP INDEX CONCURRENTLY ever waits for the
table lock while holding a snapshot (which deadlocks the other one's last phase). Removals on one table
are therefore serialised, and each re-checks under the lock, right before dropping, that the index is not
needed now: two removals can never together leave a foreign key without an index. The DROP waits as
long as older transactions need (its own 12 h budget, not the pool's command timeout).

Not offered: vector (ANN) indexes, whose rebuild can take hours (managed by the embedding index
controls), and array-field GIN indexes (managed with their array field, which stays marked indexed).
Every kind is in the user lane; a field index removal is a dataset job (it changes the field) and, like
any job of that lane, holds the dataset's user lane while it waits out a backup; the others belong to no
dataset. A field index whose dataset no longer exists (an orphan) is removed like any other index.
"""

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from . import diskspace, index_advisor, index_jobs, job_queue
from .job_queue import JobPolicy
from .job_runner import StepResult
from .job_wait import JobConflict, JobInsufficientStorage

logger = logging.getLogger(__name__)

DROP_KIND = 'index_drop'
RESTORE_KIND = 'index_restore'
CORE_LOCK = index_jobs.TABLE_LOCK
VECTOR_METHODS = ('hnsw', 'ivfflat')
# What a CREATE INDEX CONCURRENTLY of an index of size S writes on the database volume at its peak, by
# access method: the index (S), its build's temp sort and its full-page images in WAL, as (temp, WAL)
# multiples of S measured on PostgreSQL 16.15:
# - gin: a jsonb_path_ops build (rviab_gin_footprint.sh: 139 MB index, 80 MB temp, 45 MB WAL), 1.89 S;
# - btree: a two-key build on 600k rows with maintenance_work_mem 16 MB (rviab_btree_footprint.sh: 46 MB
#   index, largest temp file 41 MB, 32 MB WAL), 2.57 S: the build sort spills, unlike GIN's.
# Other methods were not measured and take the largest measured footprint. diskspace adds its headroom.
RESTORE_FOOTPRINT = {'gin': (0.57, 0.32), 'btree': (0.88, 0.69)}
_METHOD = re.compile(r' USING (\w+) ')
# A liveness bound, as for backups: waiting for a long reader and rebuilding a large GIN index both take
# as long as they take. Cancelling the job stops either at once.
SESSION_SECONDS = 12 * 3600
POLICY = JobPolicy(max_unit_seconds=SESSION_SECONDS + 600, max_failures=3, retry_seconds=5, max_retry_seconds=300)
_DEFINITION = re.compile(r'^CREATE (UNIQUE )?INDEX (\S+) ON ')
_LOOKUP = """
    SELECT pg_get_indexdef(c.oid) AS definition, i.indisvalid AS valid, pg_relation_size(c.oid) AS size_bytes
    FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
    WHERE c.relname = $1 AND c.relnamespace = current_schema()::regnamespace"""


class IndexRefused(HTTPException):
    """The index cannot be removed or restored as asked (a 409 for the API, a failed job for the runner)."""

    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(status_code=status_code, detail=detail)


async def describe(connection, name: str) -> Optional[Dict[str, Any]]:
    """The index named `name` in this schema, with its kind and why it must stay (if it must)."""
    query = index_advisor._index_query(await _has_last_scan(connection), table_of=True)
    indexes = [dict(row) for row in await connection.fetch(query, name)]
    found = next((index for index in indexes if index['name'] == name), None)
    if found is None:
        return None
    keys = [dict(row) for row in await connection.fetch(index_advisor._FOREIGN_KEYS)]
    table = [index for index in indexes if index['table_name'] == found['table_name']]
    needed = index_advisor.needed_reasons(table, [key for key in keys if key['table_name'] == found['table_name']])
    kind, dataset_id, field = index_advisor.classify(found)
    return {**found, 'kind': kind, 'dataset_id': dataset_id, 'field': field, 'needed': needed.get(found['oid'], [])}


async def _has_last_scan(connection) -> bool:
    return await connection.fetchval("SELECT current_setting('server_version_num')::int >= 160000")


def restore_factor(definition: Optional[str]) -> float:
    """Peak space of rebuilding an index, as a multiple of its size, from its recorded definition's method."""
    method = _METHOD.search(definition or '')
    temp, wal = RESTORE_FOOTPRINT.get(
        method.group(1) if method else '',
        max(RESTORE_FOOTPRINT.values(), key=sum),
    )
    return 1 + temp + wal


def restore_bytes(size_bytes: Optional[int], definition: Optional[str]) -> int:
    """Free space a restore needs on the database volume (before diskspace's headroom)."""
    return int((size_bytes or 0) * restore_factor(definition))


def managed_elsewhere(index: Dict[str, Any]) -> Optional[str]:
    """Why the advisor does not remove this index itself, or None."""
    if index['kind'] == 'ann' or index['method'] in VECTOR_METHODS:
        return (
            f"{index['name']} is a vector (ANN) index: it is managed by the embedding index controls, because "
            'rebuilding a large one can take hours'
        )
    if index['kind'] == 'array':
        return f"{index['name']} belongs to an array field: remove it with the field, which stays marked indexed"
    return None


def _refusal(index: Dict[str, Any], definition: Optional[str]) -> Optional[str]:
    managed = managed_elsewhere(index)
    if managed:
        return managed
    if index['needed']:
        return f"{index['name']} must stay: {'; '.join(index['needed'])}"
    if index['building']:
        return f"{index['name']} is being built right now"
    if definition is not None and index['definition'] != definition:
        return f"{index['name']} changed since it was reviewed; review again"
    return None


def lock_key(kind: str, table: str, dataset_id: Optional[int], field: Optional[str]) -> Tuple[int, Any]:
    """(advisory first key, second key: an int, or a str keyed by hashtext): see the module docstring. The
    table's TABLE_LOCK is added by index_jobs._build_session."""
    if kind == 'field' and dataset_id is not None:
        return index_jobs.INDEX_LOCK, int(dataset_id)
    if kind == 'ann' and field and dataset_id is not None:
        return index_jobs.ANN_LOCK, index_jobs.ann_lock_value(dataset_id, field)
    return CORE_LOCK, table


async def _unfinished(connection, name: str) -> Optional[Dict[str, Any]]:
    row = await connection.fetchrow(
        f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
            WHERE kind = ANY($1::text[]) AND args->>'index_name' = $2 AND state IN ('queued', 'running')
              AND NOT cancel_requested ORDER BY created_at LIMIT 1""",
        [DROP_KIND, RESTORE_KIND],
        name,
    )
    return dict(row) if row is not None else None


async def submit_drop(
    pool,
    name: str,
    user: Dict[str, Any],
    *,
    definition: Optional[str] = None,
    record: bool = True,
    reason: Optional[str] = None,
    field_name: Optional[str] = None,
    dataset_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Queue the removal of an index (or join the one already queued). `record=False` is the Indexes tab's
    demote: no removal record, and a field whose index is already gone is still marked not indexed."""
    async with job_queue._transaction(pool) as connection:
        # One submission per index at a time: two clicks never queue two removals.
        await connection.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
            json.dumps(['index-action', name]),
        )
        existing = await _unfinished(connection, name)
        if existing is not None:
            if existing['kind'] == DROP_KIND and bool(existing['args'].get('record')) == record:
                return existing
            if existing['kind'] == DROP_KIND:
                raise IndexRefused(
                    f'{name} is already being removed ({"by the advisor" if record is False else "demoted"})',
                )
            raise IndexRefused(f'{name} is being restored; wait for that to finish')
        index = await describe(connection, name)
        if index is None and field_name is None:
            raise IndexRefused(f'There is no index named {name}', status_code=404)
        table = index['table_name'] if index is not None else 'samples'
        if index is not None:
            refused = _refusal(index, definition)
            if refused:
                raise IndexRefused(refused)
            kind, dataset_id, field_name = index['kind'], index['dataset_id'], index['field'] or field_name
        else:
            kind = 'field'
        if kind == 'field' and not await connection.fetchval(
            'SELECT EXISTS(SELECT 1 FROM datasets WHERE id=$1)',
            dataset_id,
        ):
            if not record:
                raise IndexRefused('Dataset not found', status_code=404)
            kind, dataset_id = 'other', None  # an orphan: its dataset was deleted
        if kind == 'field':
            from .bulk_jobs import require_no_pending_bulk

            await connection.execute('SELECT require_writable_dataset($1)', dataset_id)
            # As before: a competing index operation (a build, demote, rename or clear) is refused, not queued.
            if not await connection.fetchval(
                'SELECT pg_try_advisory_xact_lock($1,$2)',
                index_jobs.INDEX_LOCK,
                dataset_id,
            ):
                raise IndexRefused('Another index operation is running for this dataset; retry')
            await require_no_pending_bulk(connection, dataset_id, fields_only=True)
        return await job_queue.enqueue_job(
            connection,
            DROP_KIND,
            dataset_id=dataset_id if kind == 'field' else None,
            lane='user',
            created_by=user.get('id'),
            args={
                'index_name': name,
                'table': table,
                'definition': index['definition'] if index is not None else None,
                'kind': kind,
                'dataset_id': dataset_id,
                'field': field_name,
                'record': record,
                'reason': reason,
                'user_name': user.get('username'),
            },
        )


async def submit_restore(pool, name: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """Queue the restore of an index the advisor removed (field indexes are restored by promote)."""
    async with job_queue._transaction(pool) as connection:
        await connection.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
            json.dumps(['index-action', name]),
        )
        removal = await connection.fetchrow(
            """SELECT * FROM removed_indexes WHERE index_name = $1 AND restored_at IS NULL AND closed_at IS NULL""",
            name,
        )
        if removal is None:
            raise IndexRefused(f'{name} was not removed by the index advisor', status_code=404)
        existing = await _unfinished(connection, name)
        if existing is not None:
            if existing['kind'] == RESTORE_KIND:
                return existing
            raise IndexRefused(f'{name} is still being removed; wait for that to finish')
        if removal['kind'] == 'ann' or removal['kind'] == 'array':
            raise IndexRefused(f'{name} is managed outside the index advisor; rebuild it there')
        await diskspace.require_free_space(
            connection,
            restore_bytes(removal['size_bytes'], removal['definition']),
            f'restoring index {name}',
        )
        job = await job_queue.enqueue_job(
            connection,
            RESTORE_KIND,
            lane='user',
            created_by=user.get('id'),
            args={
                'index_name': name,
                'table': removal['table_name'],
                'removal_id': removal['id'],
                'definition': removal['definition'],
                'kind': removal['kind'],
                'field': removal['field_name'],
                'size_bytes': removal['size_bytes'],
                'user_name': user.get('username'),
            },
        )
        await connection.execute(
            'UPDATE removed_indexes SET restore_job_id = $2 WHERE id = $1',
            removal['id'],
            job['id'],
        )
        return job


async def _backup_running(pool) -> bool:
    async with pool.acquire(timeout=1) as connection:
        return bool(
            await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM jobs WHERE kind = 'backup' AND state IN ('queued', 'running'))",
                timeout=5,
            ),
        )


async def _wait_for_backup(pool) -> None:
    """Never during a database backup: its long transaction would hold the DDL's waits (and SHARE UPDATE
    EXCLUSIVE on the table) for the whole dump. Deferred without spending the failure budget."""
    if await _backup_running(pool):
        raise job_queue.QueueContended('Waiting for the database backup to finish')


# While a drop or restore waits for older transactions, its job message names them:
# a transaction left open on the table holds every index change on it for up to SESSION_SECONDS. Sampled
# at the advisor page's polling interval (it shows nothing sooner), written only when the text changes.
BLOCKER_SAMPLE_SECONDS = 2.0
WAITING = ' — waiting for '
_SHOWN_BLOCKERS = 3
_BLOCKERS = """
    SELECT a.pid, NULLIF(a.application_name, '') AS application_name, a.state,
           floor(EXTRACT(epoch FROM clock_timestamp() - a.xact_start))::bigint AS transaction_seconds
    FROM unnest(pg_blocking_pids($1)) AS b(pid) JOIN pg_stat_activity a ON a.pid = b.pid
    ORDER BY a.xact_start NULLS LAST, a.pid"""


def _age(seconds: Optional[int]) -> str:
    seconds = int(seconds or 0)
    hours, minutes = divmod(seconds // 60, 60)
    if hours:
        return f'{hours} h {minutes} min'
    return f'{minutes} min' if minutes else f'{seconds} s'


def blocked_message(base: str, blockers) -> str:
    """`base`, then the sessions the DDL waits for (pid, application, state, transaction age)."""
    if not blockers:
        return base
    shown = '; '.join(
        f"pid {row['pid']} ({row['application_name'] or 'no application name'}, {row['state'] or 'unknown state'}, "
        f"transaction open {_age(row['transaction_seconds'])})"
        for row in blockers[:_SHOWN_BLOCKERS]
    )
    more = f'; and {len(blockers) - _SHOWN_BLOCKERS} more' if len(blockers) > _SHOWN_BLOCKERS else ''
    return f'{base}{WAITING}{shown}{more}'


def public_message(kind: str, message: Optional[str]) -> Optional[str]:
    """A removal's or restore's message for someone who is not an administrator: that it waits, not for
    which sessions (a field index removal is a dataset job, followed by every reader of the dataset)."""
    if kind not in (DROP_KIND, RESTORE_KIND) or not message or WAITING not in message:
        return message
    return message.split(WAITING, 1)[0] + WAITING + 'older transactions on the table to finish'


@asynccontextmanager
async def _report_blockers(pool, job, pid: int, base: str):
    """While the block runs, keep the running job's message naming what the backend `pid` waits for. Each
    sample borrows a pool connection; a failed sample is skipped, so it never fails or slows the DDL."""
    stop = asyncio.Event()

    async def sample():
        shown = None
        while not stop.is_set():
            try:
                async with pool.acquire(timeout=1) as watcher:
                    message = blocked_message(base, await watcher.fetch(_BLOCKERS, pid, timeout=5))
                    if message != shown:
                        await watcher.execute(
                            "UPDATE jobs SET message = $3 WHERE id = $1 AND lease_token = $2 AND state = 'running'",
                            job['id'],
                            job['lease_token'],
                            message,
                            timeout=5,
                        )
                        shown = message
            except Exception as error:
                logger.debug('Could not report what %s waits for: %s', base, error)
            with suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(stop.wait(), BLOCKER_SAMPLE_SECONDS)

    task = asyncio.create_task(sample())
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)


async def _drop_statement(connection, name: str) -> str:
    """DROP INDEX CONCURRENTLY of this schema's index, schema-qualified: a retry after the drop must never
    reach a same-named index further along the search path."""
    return await connection.fetchval(
        "SELECT format('DROP INDEX CONCURRENTLY IF EXISTS %I.%I', current_schema(), $1::text)",
        name,
        timeout=5,
    )


async def _field_state(connection, dataset_id: int, field: str) -> Optional[Dict[str, Any]]:
    row = await connection.fetchrow(
        'SELECT id, is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
        dataset_id,
        field,
    )
    return dict(row) if row is not None else None


def drop_handler(pool):
    async def measure(job):
        checkpoint = job['checkpoint'] or {}
        if job['lane'] != 'user':
            raise ValueError('Index removals belong to the user lane')
        if checkpoint.get('phase') != 'drop':
            return await _record(job)
        return await _drop(job)

    async def _record(job):
        args = job['args']
        name, kind, dataset_id, field = args['index_name'], args['kind'], args['dataset_id'], args['field']
        async with pool.acquire(timeout=1) as connection:
            index = await describe(connection, name)
            observed = None
            if index is not None:
                refused = _refusal(index, args['definition'])
                if refused:
                    raise JobConflict(refused)
                # The window its usage was counted over: as the review computes it.
                observed = await connection.fetchval(
                    """SELECT GREATEST((SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()),
                                       (SELECT stats_reset FROM pg_stat_bgwriter),
                                       (SELECT first_seen_at FROM index_observations o WHERE o.index_oid = $1
                                          AND o.system_identifier =
                                              (SELECT system_identifier FROM pg_control_system())
                                          AND o.database_oid =
                                              (SELECT oid FROM pg_database WHERE datname = current_database())))""",
                    index['oid'],
                )
            elif not (kind == 'field' and args['record'] is False):
                return StepResult(finished=True, result={'index': name, 'removed': False, 'detail': 'Already gone'})

        async def publish(connection):
            if kind == 'field':
                # Not indexed before the index goes: a promoted field without its index is far slower.
                if not await connection.fetchval('SELECT pg_try_advisory_xact_lock_shared(20261016,$1)', dataset_id):
                    raise job_queue.QueueContended('Index removal is waiting for a field operation')
                await connection.execute(
                    'UPDATE dataset_fields SET is_promoted = FALSE WHERE dataset_id = $1 AND field_name = $2',
                    dataset_id,
                    field,
                )
            if args['record'] and index is not None:
                # A removal still open for this name (stopped before its drop, or its index re-created since)
                # is replaced by this one.
                await connection.execute(
                    """UPDATE removed_indexes SET closed_at = clock_timestamp(),
                           closed_note = 'superseded by a later removal'
                       WHERE index_name = $1 AND restored_at IS NULL AND closed_at IS NULL""",
                    name,
                )
                await connection.execute(
                    """INSERT INTO removed_indexes (index_name, table_name, definition, kind, dataset_id, field_name,
                           size_bytes, scans, last_scan, observed_since, reason, removed_by, removed_by_name,
                           drop_job_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
                    name,
                    index['table_name'],
                    index['definition'],
                    kind,
                    dataset_id if kind == 'field' else None,
                    field,
                    index['size_bytes'],
                    index['scans'],
                    index['last_scan'],
                    observed,
                    args.get('reason'),
                    job['created_by'],
                    args.get('user_name'),
                    job['id'],
                )

        return StepResult(
            checkpoint={'phase': 'drop', 'size_bytes': index['size_bytes'] if index else 0},
            message=f'Removing index {name}',
            publish=publish,
        )

    async def _drop(job):
        args = job['args']
        name, kind, dataset_id, field = args['index_name'], args['kind'], args['dataset_id'], args['field']
        await _wait_for_backup(pool)
        table = args.get('table') or 'samples'
        key, value = lock_key(kind, table, dataset_id, field)
        started = time.monotonic()
        async with index_jobs._build_session(pool, key, value, SESSION_SECONDS, table=table) as connection:
            if kind == 'field':
                state = await _field_state(connection, dataset_id, field) if field else None
                if state is not None and state['is_promoted']:
                    # Indexed again (promote) since this removal was recorded: leave the index alone.
                    async def reopened(connection):
                        await connection.execute(
                            """UPDATE removed_indexes SET closed_at = clock_timestamp(), closed_note = 'indexed again'
                               WHERE drop_job_id = $1 AND closed_at IS NULL""",
                            job['id'],
                        )

                    return StepResult(
                        finished=True,
                        result={'index': name, 'removed': False, 'detail': 'The field was indexed again'},
                        message='Not removed: the field was indexed again',
                        publish=reopened,
                    )
            current = await connection.fetchrow(_LOOKUP, name, timeout=5)
            if current is not None and args['definition'] is not None and current['definition'] != args['definition']:
                raise JobConflict(f'{name} changed after its removal was recorded; nothing was dropped')
            if current is not None:
                # Under the table's lock: another removal on this table may have made this index needed
                # (the last one left for a foreign key) since the administrator asked.
                now = await describe(connection, name)
                if now is not None and now['needed']:
                    raise JobConflict(f"{name} must stay now: {'; '.join(now['needed'])}; nothing was dropped")
            # As long as older transactions need (a long read, a pg_dump outside the jobs table): its own budget,
            # never the pool's command timeout, which would cut it off and leave an INVALID index behind.
            statement = await _drop_statement(connection, name)
            async with _report_blockers(pool, job, connection.get_server_pid(), f'Removing index {name}'):
                await connection.execute(statement, timeout=SESSION_SECONDS + 60)
            if await connection.fetchrow(_LOOKUP, name, timeout=5) is not None:
                raise JobConflict(f'{name} is still there after DROP INDEX CONCURRENTLY; retry')

        async def dropped(connection):
            if args['record']:
                await connection.execute(
                    'UPDATE removed_indexes SET dropped_at = clock_timestamp() WHERE drop_job_id = $1',
                    job['id'],
                )

        result: Dict[str, Any] = {'index': name, 'removed': True, 'seconds': round(time.monotonic() - started, 1)}
        if not args['record'] and kind == 'field':
            result = {'message': f'Field {field} demoted successfully'}
        return StepResult(finished=True, result=result, message=f'Removed index {name}', publish=dropped)

    return measure


def restore_statement(definition: str) -> str:
    """pg_get_indexdef's `CREATE [UNIQUE] INDEX name ON ...` as CREATE INDEX CONCURRENTLY IF NOT EXISTS."""
    match = _DEFINITION.match(definition)
    if match is None:
        raise JobConflict('The recorded definition is not a CREATE INDEX statement')
    return _DEFINITION.sub(
        lambda m: f'CREATE {m.group(1) or ""}INDEX CONCURRENTLY IF NOT EXISTS {m.group(2)} ON ',
        definition,
        1,
    )


def restore_handler(pool):
    async def measure(job):
        args = job['args']
        name, kind, field, definition = args['index_name'], args['kind'], args.get('field'), args['definition']
        if job['lane'] != 'user':
            raise ValueError('Index restores belong to the user lane')
        statement = restore_statement(definition)
        if kind in ('ann', 'array'):
            raise JobConflict(f'{name} is managed outside the index advisor; rebuild it there')
        async with pool.acquire(timeout=1) as connection:
            try:
                await diskspace.require_free_space(
                    connection,
                    restore_bytes(args.get('size_bytes'), definition),
                    f'restoring index {name}',
                )
            except HTTPException as error:
                raise JobInsufficientStorage(str(error.detail)) from None
        await _wait_for_backup(pool)
        table = args.get('table') or 'samples'
        key, value = lock_key(kind, table, None, field)
        started = time.monotonic()
        async with index_jobs._build_session(pool, key, value, SESSION_SECONDS, table=table) as connection:
            current = await connection.fetchrow(_LOOKUP, name, timeout=5)
            if current is not None and current['valid'] and current['definition'] != definition:
                raise JobConflict(f'Another index named {name} exists; nothing was restored')
            async with _report_blockers(pool, job, connection.get_server_pid(), f'Restoring index {name}'):
                await index_jobs._build(connection, name, statement, SESSION_SECONDS)
            rebuilt = await connection.fetchrow(_LOOKUP, name, timeout=5)
            if rebuilt is None or rebuilt['definition'] != definition:
                raise JobConflict(f'{name} was rebuilt with another definition; check it')

        async def restored(connection):
            await connection.execute(
                """UPDATE removed_indexes SET restored_at = clock_timestamp(), restored_by = $2, restored_by_name = $3,
                       restore_job_id = $4
                   WHERE id = $1 AND restored_at IS NULL""",
                args['removal_id'],
                job['created_by'],
                args.get('user_name'),
                job['id'],
            )

        return StepResult(
            finished=True,
            result={'index': name, 'restored': True, 'seconds': round(time.monotonic() - started, 1)},
            message=f'Restored index {name}',
            publish=restored,
        )

    return measure


async def pending(connection, dataset_id: Optional[int]) -> List[str]:
    """Names of the scope's queued or running removals and restores. A removal is recorded by its first unit,
    so until then only its job shows it: the page keeps polling while any is pending."""
    scope = (
        "jobs.args->>'kind' = 'field' AND jobs.dataset_id = $2"
        if dataset_id is not None
        else ("jobs.args->>'kind' <> 'field' AND $2::int IS NULL")
    )
    rows = await connection.fetch(
        f"""SELECT DISTINCT jobs.args->>'index_name' AS name FROM jobs
            WHERE jobs.kind = ANY($1::text[]) AND jobs.state IN ('queued', 'running') AND {scope}
            ORDER BY 1""",
        [DROP_KIND, RESTORE_KIND],
        dataset_id,
    )
    return [row['name'] for row in rows]


async def removals(connection, dataset_id: Optional[int]) -> List[Dict[str, Any]]:
    """Open removals of a scope (a dataset's field indexes, or every other index), with their state, read
    from the jobs and the catalog: removing, restoring, removed (gone: Restore), stopped (the removal
    ended with the index still there: Remove again), or back (re-created another way; the next review
    closes the record)."""
    scope = 'r.kind = $1 AND r.dataset_id = $2' if dataset_id is not None else "r.kind <> $1 AND $2::int IS NULL"
    rows = await connection.fetch(
        f"""SELECT r.*, drop_job.state AS drop_state, restore_job.state AS restore_state,
                   restore_job.error->>'message' AS restore_error,
                   to_regclass(format('%I.%I', current_schema(), r.index_name)) IS NOT NULL AS present
            FROM removed_indexes r
            LEFT JOIN jobs drop_job ON drop_job.id = r.drop_job_id
            LEFT JOIN jobs restore_job ON restore_job.id = r.restore_job_id
            WHERE r.restored_at IS NULL AND r.closed_at IS NULL AND {scope}
            ORDER BY r.removed_at DESC""",
        'field',
        dataset_id,
    )
    found = []
    for row in rows:
        if row['restore_state'] in ('queued', 'running'):
            state = 'restoring'
        elif row['drop_state'] in ('queued', 'running'):
            state = 'removing'
        elif not row['present']:
            state = 'removed'
        else:
            state = 'stopped' if row['dropped_at'] is None else 'back'
        found.append(
            {
                'id': row['id'],
                'index_name': row['index_name'],
                'table': row['table_name'],
                'kind': row['kind'],
                'field': row['field_name'],
                'definition': row['definition'],
                'size_bytes': row['size_bytes'],
                'scans': row['scans'],
                'last_scan': index_advisor._iso(row['last_scan']),
                'observed_since': index_advisor._iso(row['observed_since']),
                'reason': row['reason'],
                'removed_by': row['removed_by_name'],
                'removed_at': index_advisor._iso(row['removed_at']),
                'dropped_at': index_advisor._iso(row['dropped_at']),
                'state': state,
                'restore_failed': row['restore_error'] if row['restore_state'] in ('failed', 'cancelled') else None,
            },
        )
    return found
