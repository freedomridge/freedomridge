"""Index builds as user-lane jobs: one CREATE INDEX CONCURRENTLY per unit, on its own connection.

The build holds a session advisory lock for its whole duration (field indexes: the dataset's index
operation guard 20261017; ANN indexes: 20261066 per embedding field), so a retry never drops or
rebuilds an index a live build owns. A build that dies leaves an INVALID index; the next attempt,
holding the same lock, drops that carcass concurrently and builds again. The build connection asks
PostgreSQL to notice a dead client (client_connection_check_interval), so a killed runner's build
stops instead of holding the lock until it finishes.

Every CREATE or DROP INDEX CONCURRENTLY the application runs on a table also holds that table's lock
(TABLE_LOCK, 20261067 by hashtext(table)), taken with a try-lock and never waited for: a second one
would wait for the table's SHARE UPDATE EXCLUSIVE lock while holding a snapshot, and the first one's
last phase, which waits for older snapshots, would then be killed as a deadlock. A busy table defers
the job (no failure counted) or answers 409.
"""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from typing import Optional

import asyncpg
from fastapi import HTTPException

from . import job_queue
from .job_runner import StepResult
from .job_wait import JobConflict, JobInputError, JobNotFound, JobUnprocessable

FIELD_KIND = 'field_index_build'
ANN_KIND = 'ann_index_build'
INDEX_LOCK = 20261017
ANN_LOCK = 20261066
TABLE_LOCK = 20261067
# The API's former caps: promote ran under the 600 s command timeout, the ANN build had 3600 s.
FIELD_BUILD_SECONDS = 3600
ANN_BUILD_SECONDS = 3600
# Above the build budget, so PostgreSQL's statement timeout ends a slow build before the runner does.
FIELD_POLICY = job_queue.JobPolicy(max_unit_seconds=3900, max_failures=3, retry_seconds=5, max_retry_seconds=300)
ANN_POLICY = job_queue.JobPolicy(max_unit_seconds=4500, max_failures=3, retry_seconds=5, max_retry_seconds=300)
_SAFE_FIELD = re.compile(r'^[a-zA-Z0-9_]+$')
logger = logging.getLogger(__name__)
_VALIDITY = (
    'SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid '
    'WHERE c.relname = $1 AND c.relnamespace = current_schema()::regnamespace'
)


async def _unfinished(connection, kind, dataset_id, key, value):
    return await connection.fetchrow(
        f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
            WHERE kind=$1 AND dataset_id=$2 AND args->>'{key}'=$3
              AND state IN ('queued','running') AND NOT cancel_requested
            ORDER BY created_at,jobs.id LIMIT 1""",
        kind,
        dataset_id,
        value,
    )


async def submit_field_index(pool, dataset_id, user_id, args):
    """Join an unfinished build of the same field, or queue one."""
    from .bulk_jobs import require_no_pending_bulk

    async with job_queue._transaction(pool) as connection:
        await connection.execute('SELECT require_writable_dataset($1)', dataset_id)
        existing = await _unfinished(connection, FIELD_KIND, dataset_id, 'field_name', args['field_name'])
        if existing is not None:
            return dict(existing)
        # As before: a competing index operation (a build, demote, clear or rename) is refused, not queued.
        if not await connection.fetchval('SELECT pg_try_advisory_xact_lock($1,$2)', INDEX_LOCK, dataset_id):
            raise HTTPException(status_code=409, detail='Another index operation is running for this dataset; retry')
        await require_no_pending_bulk(connection, dataset_id, fields_only=True)
        return await job_queue.enqueue_job(connection, FIELD_KIND, dataset_id=dataset_id, created_by=user_id, args=args)


async def submit_ann_index(pool, dataset_id, user_id, field):
    async with job_queue._transaction(pool) as connection:
        existing = await _unfinished(connection, ANN_KIND, dataset_id, 'field', field)
        if existing is not None:
            return dict(existing)
        return await job_queue.enqueue_job(
            connection,
            ANN_KIND,
            dataset_id=dataset_id,
            created_by=user_id,
            args={'field': field},
        )


async def try_lock(connection, key: int, value) -> bool:
    """A session advisory try-lock; a str value is keyed by hashtext(value) (a table, a field)."""
    if isinstance(value, str):
        return bool(await connection.fetchval('SELECT pg_try_advisory_lock($1, hashtext($2))', key, value, timeout=5))
    return bool(await connection.fetchval('SELECT pg_try_advisory_lock($1,$2)', key, value, timeout=5))


async def unlock(connection, key: int, value) -> None:
    if isinstance(value, str):
        await connection.fetchval('SELECT pg_advisory_unlock($1, hashtext($2))', key, value, timeout=5)
    else:
        await connection.fetchval('SELECT pg_advisory_unlock($1,$2)', key, value, timeout=5)


def session_locks(lock_key: int, lock_value, table: Optional[str]) -> list:
    """The operation's own lock, then its table's (TABLE_LOCK), once."""
    locks = [(lock_key, lock_value)]
    if table is not None and (TABLE_LOCK, table) not in locks:
        locks.append((TABLE_LOCK, table))
    return locks


async def take_locks(connection, locks: list) -> None:
    """Try every lock in order; on the first that is busy, release the ones taken and defer."""
    taken: list = []
    for key, value in locks:
        if not await try_lock(connection, key, value):
            for held in reversed(taken):
                await unlock(connection, *held)
            if key == TABLE_LOCK:
                raise job_queue.QueueContended(f'Another index is being built or removed on {value}; retrying later')
            raise job_queue.QueueContended('Another index operation holds this index; retrying later')
        taken.append((key, value))


@asynccontextmanager
async def _build_session(pool, lock_key, lock_value, seconds, table: Optional[str] = None):
    """A pool connection owned by one build, holding a session advisory lock (and, given `table`, that
    table's TABLE_LOCK). A refusal the job reports (JobInputError) releases the locks cleanly; any other
    failure or cancellation terminates the session, so the locks, the session settings and an aborted
    command never return to the pool. `seconds` is a server-side statement budget: PostgreSQL ends an
    overlong build itself, and the job fails visibly instead of retrying a build that cannot finish
    (asyncpg's client timeout is only a fallback a minute later)."""
    locks = session_locks(lock_key, lock_value, table)
    connection = await pool.acquire(timeout=1)
    held = False
    try:
        await take_locks(connection, locks)
        held = True
        # A killed runner's backend notices its dead client and stops the build, releasing the lock.
        await connection.execute("SET client_connection_check_interval = '5s'", timeout=5)
        await connection.execute(f"SET statement_timeout = '{int(seconds)}s'", timeout=5)
        try:
            yield connection
        except JobInputError:
            await _end_build_session(connection, locks)
            held = False
            raise
        await _end_build_session(connection, locks)
        held = False
    finally:
        if held:
            with suppress(asyncpg.InterfaceError):
                connection.terminate()
        await pool.release(connection)


async def _end_build_session(connection, locks):
    await connection.execute(
        'RESET client_connection_check_interval; RESET maintenance_work_mem; RESET max_parallel_maintenance_workers; '
        'RESET statement_timeout',
        timeout=5,
    )
    for held in reversed(locks):
        await unlock(connection, *held)


async def _drop_carcass(connection, name, *, valid_too=False, lock_timeout='5s'):
    """Drop an INVALID leftover (or any index, `valid_too`) concurrently. Waiting for old readers is
    bounded: a busy table leaves the carcass for the next attempt or for sweep_invalid_indexes."""
    valid = await connection.fetchval(_VALIDITY, name, timeout=5)
    if valid is None or (valid and not valid_too):
        return
    await connection.execute(f"SET lock_timeout = '{lock_timeout}'", timeout=5)
    try:
        await connection.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"', timeout=60)
    finally:
        await connection.execute('RESET lock_timeout', timeout=5)


async def _build(connection, name, statement, seconds):
    """CREATE INDEX CONCURRENTLY IF NOT EXISTS, verified valid. Cancellation or failure removes the
    carcass while this session still holds the build lock."""
    await _drop_carcass(connection, name)
    try:
        await connection.execute(statement, timeout=seconds + 60)
    except BaseException as error:
        # Cancelling the task cancels the server command first; the session stays usable for cleanup.
        # A cleanup that cannot get its locks now is finished by sweep_invalid_indexes later.
        with suppress(Exception):
            await _drop_carcass(connection, name)
        # `from None`: a transient cause (statement timeout) would make the runner rebuild it again.
        if isinstance(error, (asyncpg.QueryCanceledError, asyncio.TimeoutError, TimeoutError)):
            raise JobConflict(f'Index build exceeded its {seconds} s budget; nothing was kept') from None
        if isinstance(error, (asyncpg.DataError, asyncpg.IntegrityConstraintViolationError)):
            raise JobConflict('Index build failed or data changed; check values and retry') from None
        raise
    if not await connection.fetchval(_VALIDITY, name, timeout=5):
        await _drop_carcass(connection, name)
        raise JobConflict('Index build did not complete (indisvalid = false); retry')


class _Waits:
    seconds: Optional[float] = 0.0


# How often a build's lock waits are sampled: each wait is measured to within about this much, so a
# build's own time below it cannot be told from zero (measured_build_seconds floors it there).
WAIT_SAMPLE_SECONDS = 0.25


def measured_build_seconds(elapsed: float, waited: float) -> float:
    """A build's own time: its wall time minus its lock waits, never below the sampling resolution (a
    tiny or mostly-waiting build must not read as 0 s, which would make every index look free)."""
    return max(WAIT_SAMPLE_SECONDS, elapsed - waited)


@asynccontextmanager
async def _lock_waits(pool, pid: int):
    """Measure how long the backend `pid` spends waiting on locks while the block runs (CREATE INDEX
    CONCURRENTLY waits on the table lock and on older transactions' virtual xids), by sampling
    pg_stat_activity. Each sample borrows a pool connection for one query and returns it, so a long build
    does not hold a second runner connection. `seconds` is None when the waits could not be watched: a
    sample that cannot get a connection or finds no row for `pid` (its waits would be counted as building)."""
    waits = _Waits()
    stop = asyncio.Event()

    async def sample():
        last = time.monotonic()
        try:
            while not stop.is_set():
                async with pool.acquire(timeout=1) as watcher:
                    waiting = await watcher.fetchval(
                        "SELECT wait_event_type IS NOT DISTINCT FROM 'Lock' FROM pg_stat_activity WHERE pid = $1",
                        pid,
                        timeout=5,
                    )
                if waiting is None:
                    waits.seconds = None  # the backend is not visible: its waits cannot be told apart
                    return
                now = time.monotonic()
                if waiting and waits.seconds is not None:
                    waits.seconds += now - last
                last = now
                with suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(stop.wait(), WAIT_SAMPLE_SECONDS)
        except Exception:
            waits.seconds = None

    task = asyncio.create_task(sample())
    try:
        yield waits
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)


async def _definition(connection, dataset_id, field_name):
    return await connection.fetchrow(
        'SELECT id, field_type, is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
        dataset_id,
        field_name,
        timeout=5,
    )


def field_handler(pool):
    async def measure(job):
        if job['lane'] != 'user':
            raise ValueError('Index builds belong to the user lane')
        from .bulk_jobs import require_no_pending_bulk

        args, dataset_id = job['args'], job['dataset_id']
        name, field_name = args['index_name'], args['field_name']
        if not _SAFE_FIELD.match(field_name.replace('-', '_')):
            raise JobUnprocessable('Invalid field name')
        statement = (
            f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{name}" '
            f'ON samples {args["expression"]} WHERE dataset_id = {int(dataset_id)}'
        )

        def unchanged(current):
            return (
                current is not None
                and current['id'] == args['definition_id']
                and current['field_type'] == args['field_type']
            )

        async with _build_session(pool, INDEX_LOCK, dataset_id, FIELD_BUILD_SECONDS, table='samples') as connection:
            await connection.fetchval('SELECT require_writable_dataset($1)', dataset_id, timeout=5)
            try:
                await require_no_pending_bulk(connection, dataset_id, fields_only=True)
            except HTTPException as error:
                raise JobConflict(error.detail) from None
            current = await _definition(connection, dataset_id, field_name)
            if unchanged(current) and current['is_promoted']:
                return StepResult(
                    finished=True,
                    result={'message': f'Field {field_name} is already promoted'},
                    message='Field index already present',
                )
            measured = None
            if unchanged(current):
                built_before = await connection.fetchval(_VALIDITY, name, timeout=5)
                if not built_before:
                    await _drop_carcass(connection, name)  # an INVALID leftover: not part of the measured build
                started = time.monotonic()
                async with _lock_waits(pool, connection.get_server_pid()) as waits:
                    await _build(connection, name, statement, FIELD_BUILD_SECONDS)
                if not built_before and waits.seconds is not None:
                    # The index advisor's build-time estimate: seconds per byte of the heap this build scanned,
                    # without the time it waited for other transactions (a pg_dump's snapshot, another build's
                    # table lock). A retry that found the index already built measures nothing, and so does a
                    # build whose waits could not be watched.
                    elapsed = time.monotonic() - started
                    measured = {
                        'build_seconds': round(measured_build_seconds(elapsed, waits.seconds), 3),
                        'waited_seconds': round(waits.seconds, 3),
                        'table_bytes': await connection.fetchval("SELECT pg_relation_size('samples')", timeout=5),
                    }
                current = await _definition(connection, dataset_id, field_name)
            if not unchanged(current):
                # Never leave an index under a changed definition; a promoted field keeps its index.
                if current is None or not current['is_promoted']:
                    await _drop_carcass(connection, name, valid_too=True)
                raise JobConflict('Field definition changed during index build; retry')

        async def publish(connection):
            # A changed definition defers this unit; its retry above drops the index and fails visibly.
            if not await connection.fetchval('SELECT pg_try_advisory_xact_lock_shared(20261016,$1)', dataset_id):
                raise job_queue.QueueContended('Index publication is waiting for a field operation')
            current = await _definition(connection, dataset_id, field_name)
            if not unchanged(current) or not await connection.fetchval(_VALIDITY, name):
                raise job_queue.QueueContended('Field or index changed after the build')
            await connection.execute('UPDATE dataset_fields SET is_promoted = TRUE WHERE id = $1', current['id'])
            # An index advisor removal of this index is restored by the build.
            await connection.execute(
                """UPDATE removed_indexes SET restored_at = clock_timestamp(), restored_by = $2,
                       restore_job_id = COALESCE(restore_job_id, $3),
                       restored_by_name = (SELECT username FROM users WHERE id = $2)
                   WHERE index_name = $1 AND restored_at IS NULL AND closed_at IS NULL""",
                name,
                job['created_by'],
                job['id'],
            )

        return StepResult(
            finished=True,
            checkpoint=measured,
            result={'message': f'Field {field_name} promoted successfully'},
            message='Field index built',
            publish=publish,
        )

    return measure


def ann_index_name(field: str) -> str:
    """Deterministic ≤63-char index name. Plain truncation collided: two fields sharing
    their first ~54 chars truncated to the SAME name, so the second build's IF NOT EXISTS
    "succeeded" without ever creating an index. Over-long names get a hash suffix."""
    name = f"hnsw_emb_{field}"
    if len(name) <= 63:
        return name
    import hashlib

    return name[:54] + "_" + hashlib.sha1(field.encode()).hexdigest()[:8]


# indisvalid matters: a failed CREATE INDEX CONCURRENTLY leaves an INVALID index that pg_indexes
# still lists.
_VALID_HNSW_INDEXDEFS = (
    "SELECT ix.indexdef FROM pg_indexes ix "
    "JOIN pg_class c ON c.relname = ix.indexname AND c.relnamespace = ix.schemaname::regnamespace "
    "JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE ix.tablename = 'embeddings' AND ix.indexdef LIKE '%hnsw%' AND i.indisvalid"
)
# The field of a per-field index, as PostgreSQL prints its predicate: WHERE (field_name = 'clip'::text)
_INDEXED_FIELD = re.compile(r"WHERE \(field_name = '([a-zA-Z0-9_]+)'::text\)$")


async def valid_hnsw_index(connection, field: str, dim: int) -> bool:
    rows = await connection.fetch(_VALID_HNSW_INDEXDEFS)
    need_dim, need_field = f"vector({dim})", f"field_name = '{field}'"
    return any(need_dim in row['indexdef'] and need_field in row['indexdef'] for row in rows)


async def cosine_hnsw_fields(connection, dim: int) -> set[str]:
    """Fields with a valid per-field cosine HNSW index at dimension `dim`: the ones an
    `ORDER BY vector::vector(dim) <=> ...` query on `field_name = '<field>'` can use."""
    rows = await connection.fetch(_VALID_HNSW_INDEXDEFS)
    fields = set()
    for row in rows:
        indexdef = row['indexdef']
        match = _INDEXED_FIELD.search(indexdef)
        if match and f"::vector({dim})" in indexdef and 'vector_cosine_ops' in indexdef:
            fields.add(match.group(1))
    return fields


def ann_handler(pool):
    async def measure(job):
        if job['lane'] != 'user':
            raise ValueError('Index builds belong to the user lane')
        field = job['args']['field']
        if not _SAFE_FIELD.match(field or ''):
            raise JobUnprocessable(f'Unsafe field name {field!r}')
        started = time.monotonic()
        async with pool.acquire(timeout=1) as connection:
            lock_value = await connection.fetchval('SELECT hashtext($1)', field, timeout=5)
        async with _build_session(pool, ANN_LOCK, lock_value, ANN_BUILD_SECONDS, table='embeddings') as connection:
            # One scan of the field's vectors: part of the build, not of the request.
            try:
                dims = await connection.fetch(
                    'SELECT DISTINCT vector_dims(vector) AS d FROM embeddings WHERE field_name = $1 '
                    'AND vector IS NOT NULL',
                    field,
                    timeout=ANN_BUILD_SECONDS + 60,
                )
            except (asyncpg.QueryCanceledError, asyncio.TimeoutError, TimeoutError):
                raise JobConflict(f"Reading field '{field}' exceeded the {ANN_BUILD_SECONDS} s budget") from None
            if not dims:
                raise JobNotFound(f"No embeddings for field '{field}'")
            if len(dims) > 1:
                mixed = sorted(row['d'] for row in dims)
                raise JobUnprocessable(f"Field '{field}' has mixed dimensions {mixed} — clean up before indexing")
            dim = int(dims[0]['d'])
            if await valid_hnsw_index(connection, field, dim):
                result = {'field': field, 'dim': dim, 'built': False, 'detail': 'index already exists'}
            else:
                name = ann_index_name(field)
                # Bound the build's memory to fit any deployment: the server may tune
                # maintenance_work_mem near (or above) the container's /dev/shm, and a PARALLEL
                # index build allocates a shared-memory segment of about that size — which
                # ENOSPC-crashes in a default Docker shm. Serial + bounded always completes.
                await connection.execute("SET maintenance_work_mem = '256MB'", timeout=5)
                await connection.execute('SET max_parallel_maintenance_workers = 0', timeout=5)
                await _build(
                    connection,
                    name,
                    f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{name}" '
                    f"ON embeddings USING hnsw ((vector::vector({dim})) vector_cosine_ops) "
                    f"WHERE field_name = '{field}'",
                    ANN_BUILD_SECONDS,
                )
                result = {
                    'field': field,
                    'dim': dim,
                    'index': name,
                    'built': True,
                    'seconds': round(time.monotonic() - started, 1),
                }
        return StepResult(finished=True, result=result, message='ANN index ready')

    return measure


# Invalid indexes this module builds: cancelled, timed-out or killed builds whose cleanup could not
# get its locks (a pg_dump or another long reader holds the table). They are still maintained on
# every write (for HNSW, every insert into the field), so the runner sweeps them up.
_CARCASSES = r"""
    SELECT c.relname AS name, t.relname AS table_name, pg_get_expr(i.indpred, i.indrelid) AS predicate
    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid
    WHERE NOT i.indisvalid AND c.relnamespace = current_schema()::regnamespace
      AND NOT EXISTS (SELECT 1 FROM pg_stat_progress_create_index p WHERE p.index_relid = i.indexrelid)
      AND ((t.relname = 'samples' AND c.relname LIKE 'idx\_samples\_meta\_%')
        OR (t.relname = 'embeddings' AND c.relname LIKE 'hnsw\_emb\_%'))
    ORDER BY c.relname LIMIT $1"""


async def sweep_invalid_indexes(pool, limit=2):
    """Drop leftover INVALID build indexes that no build owns. Each drop holds the same advisory lock
    as its build, waits at most 0.5 s for old readers, and is retried by the next sweep. Returns the
    names dropped."""
    async with pool.acquire(timeout=1) as connection:
        candidates = await connection.fetch(_CARCASSES, limit, timeout=5)
    dropped = []
    for candidate in candidates:
        if candidate['table_name'] == 'samples':
            owner = re.match(r'^idx_samples_meta_(\d+)_', candidate['name'])
        else:
            owner = re.search(r"field_name = '([^']*)'", candidate['predicate'] or '')
        if owner is None:
            continue
        async with pool.acquire(timeout=1) as connection:
            if candidate['table_name'] == 'samples':
                key = (INDEX_LOCK, int(owner.group(1)))
            else:
                key = (ANN_LOCK, await connection.fetchval('SELECT hashtext($1)', owner.group(1), timeout=5))
            locks = session_locks(key[0], key[1], candidate['table_name'])
            try:
                await take_locks(connection, locks)
            except job_queue.QueueContended:
                continue  # a build or index operation owns it, or its table, now
            try:
                # The sweep runs in the job service's scheduling loop and retries every minute, so it
                # waits only briefly for old readers instead of the builds' 5 s.
                await _drop_carcass(connection, candidate['name'], lock_timeout='500ms')
                dropped.append(candidate['name'])
            except (asyncpg.PostgresError, asyncio.TimeoutError, TimeoutError) as error:
                logger.info('Invalid index %s is still busy; the next sweep retries: %s', candidate['name'], error)
            finally:
                for held in reversed(locks):
                    await unlock(connection, *held)
    return dropped
