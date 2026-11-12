"""Short queue transactions, durable cancellation and fenced result publication."""

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, TypeAlias
from uuid import UUID, uuid4

import asyncpg

from .job_tasks import run_isolated

Job = dict[str, Any]
DatabaseSource: TypeAlias = asyncpg.Pool | asyncpg.Connection
Publisher = Callable[[asyncpg.Connection], Awaitable[None]]
PUBLISH_TIMEOUT_SECONDS = 5.0
ACQUIRE_TIMEOUT_SECONDS = 1.0
QUEUE_TRANSACTION_SECONDS = 10.0
# Ends a stopped owner's open transaction so its job row lock goes away. It must stay above the publication
# budget: a publisher that takes its full budget, or waits while another slot blocks the shared event loop,
# is not stopped.
IDLE_TRANSACTION_TIMEOUT_MS = int(PUBLISH_TIMEOUT_SECONDS * 2 * 1000)
JOB_COLUMNS = 'jobs.*, controls.cancel_requested'
JOB_TABLE = 'jobs JOIN job_controls controls ON controls.job_id=jobs.id'
logger = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    """The claim expired or another worker owns the job."""


class DedupeConflict(ValueError):
    """A queued dedupe key names a different request."""


class CancelRequested(RuntimeError):
    """A durable cancellation request reached a handler's own final fence (finish_locked)."""


class CancelRefused(RuntimeError):
    """The job is past the point where it can be stopped; the reason says why."""


class QueueBusy(RuntimeError):
    """A bounded queue transaction could not acquire its locks or finish in time."""


class QueueContended(QueueBusy):
    """Database lock contention should defer work without consuming its failure budget."""


# An exclusive resource (resources {'exclusive': <name>}): at most one started, unfinished job holds it, on any
# dataset or none. A job owns it from its claim until it finishes, put back after it started included (queued with
# started_at), the rule a dataset lane follows; one that never started waits, holding nothing, until no job owns it.
# EXCLUSIVE_LOCK (with hashtext(<name>)) orders the claims of one resource; work outside the queue that must not
# overlap the resource's jobs holds it too, then checks exclusive_owned.
EXCLUSIVE_LOCK = 20261065
_EXCLUSIVE_OWNED = """EXISTS(SELECT 1 FROM jobs WHERE resources->>'exclusive'=$1 AND id IS DISTINCT FROM $2
    AND (state='running' OR (state='queued' AND started_at IS NOT NULL)))"""


async def exclusive_owned(connection: asyncpg.Connection, resource: str, job_id: UUID | None = None) -> bool:
    """Whether a job other than `job_id` owns the exclusive `resource` (started, unfinished)."""
    return bool(await connection.fetchval(f'SELECT {_EXCLUSIVE_OWNED}', resource, job_id))


# asyncpg reports a session that is gone with client errors that carry no SQL state.
_SESSION_LOST = ('connection is closed', 'connection was closed', 'released back to the pool')


def _transient(error: BaseException) -> bool:
    if isinstance(error, (QueueBusy, TimeoutError, asyncio.TimeoutError, ConnectionError, asyncpg.InternalClientError)):
        return True
    if isinstance(error, asyncpg.InterfaceError) and any(text in str(error) for text in _SESSION_LOST):
        return True
    code = getattr(error, 'sqlstate', '') or ''
    return code[:2] in ('08', '53') or code in ('57P01', '57P02', '57P03', '40001', '40P01', '55P03', '57014')


def is_transient(error: BaseException) -> bool:
    """Retry infrastructure interruptions, not invalid input or programming errors. Explicit causes count
    (`raise ... from`); an unrelated error raised while handling one (its __context__) does not."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if _transient(current):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


def _seconds(value: float, *, positive: bool = True) -> float:
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(
            'Duration must be finite and positive' if positive else 'Duration must be finite and nonnegative',
        )
    return value


@dataclass(frozen=True)
class JobPolicy:
    max_unit_seconds: float = 300
    max_failures: int = 5
    retry_seconds: float = 5
    max_retry_seconds: float = 300

    def __post_init__(self):
        for duration in (self.max_unit_seconds, self.retry_seconds, self.max_retry_seconds):
            _seconds(duration)
        if not isinstance(self.max_failures, int) or isinstance(self.max_failures, bool) or self.max_failures < 1:
            raise ValueError('max_failures must be a positive integer')
        if self.max_retry_seconds < self.retry_seconds:
            raise ValueError('max_retry_seconds must be at least retry_seconds')


@asynccontextmanager
async def _connection(source: DatabaseSource):
    if isinstance(source, asyncpg.Connection):
        yield source
    else:
        async with source.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS) as connection:
            yield connection


@asynccontextmanager
async def _transaction(pool: DatabaseSource):
    try:
        async with _connection(pool) as connection:
            operation_error = None
            expired = False

            def expire():
                nonlocal expired
                expired = True
                _terminate_connection(connection)

            deadline = asyncio.get_running_loop().call_later(QUEUE_TRANSACTION_SECONDS, expire)
            try:
                async with connection.transaction():
                    await connection.execute(
                        "SET LOCAL lock_timeout='100ms'; SET LOCAL statement_timeout='5s'; "
                        f"SET LOCAL idle_in_transaction_session_timeout='{IDLE_TRANSACTION_TIMEOUT_MS}ms'",
                    )
                    try:
                        yield connection
                    except BaseException as error:
                        operation_error = error
                        raise
            except BaseException as error:
                if isinstance(operation_error, asyncio.CancelledError):
                    raise operation_error
                if expired and not isinstance(error, asyncio.CancelledError):
                    raise QueueBusy('Queue transaction exceeded its wall-time budget') from error
                if isinstance(error, (asyncpg.InterfaceError, asyncpg.InternalClientError)):
                    _terminate_connection(connection)
                    if operation_error is not None:
                        raise operation_error
                raise
            finally:
                deadline.cancel()
    except asyncpg.LockNotAvailableError as error:
        raise QueueContended('Operation is waiting for a database lock; retry this operation') from error
    except (asyncpg.QueryCanceledError, asyncio.TimeoutError, TimeoutError) as error:
        raise QueueBusy('Operation exceeded its database time budget; retry this operation') from error


def _terminate_connection(connection):
    with suppress(asyncpg.InterfaceError):
        connection.terminate()


async def check_transaction_high_water(pool: asyncpg.Pool, *, refresh: bool = False) -> bool:
    """Recover independently of queue budgets; ordinary claims only read the marker."""
    async with pool.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS) as connection:
        pending = await connection.fetchval('SELECT restored_work_pending()', timeout=5)
        if not pending and not refresh:
            return False
        if pending:
            missing = not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM transaction_high_water)', timeout=5)
            logger.warning(
                'Missing transaction high-water mark; recovering pending work before re-establishing detection'
                if missing
                else 'Database restore detected; recovering pending work; unrelated jobs remain available',
            )
        async with connection.transaction():
            # A busy dump can contain millions of rows. Wait for ingest to finish,
            # without imposing a queue statement deadline on the atomic rewrite.
            await connection.execute("SET LOCAL lock_timeout='30s'; SET LOCAL statement_timeout='1h'")
            restored = await connection.fetchval('SELECT normalize_restored_work()', timeout=3605)
            if restored:
                # Every dump the runner makes holds its own backup job, mid-run; re-running it would start a
                # long pg_dump straight after recovery. A backup with a live lease was started here meanwhile.
                await connection.execute(
                    """UPDATE jobs SET state='cancelled',error=jsonb_build_object('code','restored_backup','message',
                         'This backup was unfinished when database restore recovery ran; it was not resumed'),
                         lease_token=NULL,lease_expires_at=NULL,updated_at=clock_timestamp(),finished_at=clock_timestamp()
                       WHERE kind='backup' AND started_at IS NOT NULL
                         AND (state='queued' OR (state='running' AND lease_expires_at<=clock_timestamp()))""",
                )
    if restored:
        logger.warning(
            'Database restore recovery completed; normalized pending work and restarted unfinished upkeep jobs',
        )
    return restored


async def enqueue_job(
    pool: DatabaseSource,
    kind: str,
    *,
    dataset_id: int | None = None,
    args: dict | None = None,
    barrier: dict | None = None,
    placement: str = 'server',
    lane: str = 'user',
    resources: dict | None = None,
    created_by: int | None = None,
    dedupe_key: str | None = None,
) -> Job:
    """Join an identical unstarted request, or queue a separate finite barrier."""
    if not kind or placement not in ('server', 'compute') or lane not in ('maintenance', 'user'):
        raise ValueError('A kind, valid placement and valid lane are required')
    arguments, requirements, boundary = args or {}, resources or {}, barrier or {}
    owner = 'system' if created_by is None else f'user:{created_by}'
    async with _transaction(pool) as connection:
        if dataset_id is not None:
            if kind != 'dataset_deletion':
                await connection.execute('SELECT require_writable_dataset($1)', dataset_id)
            await connection.execute(
                'INSERT INTO dataset_job_lanes(dataset_id,lane) VALUES ($1,$2) ON CONFLICT DO NOTHING',
                dataset_id,
                lane,
            )
        if dedupe_key is not None:
            await connection.execute(
                'SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
                json.dumps(['job-dedupe', kind, dedupe_key, owner]),
            )
            existing = await connection.fetch(
                f"""SELECT {JOB_COLUMNS} FROM {JOB_TABLE}
                    WHERE kind=$1 AND dedupe_key=$2 AND dedupe_owner=$3
                      AND state='queued' AND started_at IS NULL AND NOT cancel_requested
                    ORDER BY created_at,id FOR UPDATE OF jobs""",
                kind,
                dedupe_key,
                owner,
            )
            for row in existing:
                if (
                    row['dataset_id'] != dataset_id
                    or row['args'] != arguments
                    or row['placement'] != placement
                    or row['resources'] != requirements
                    or row['lane'] != lane
                ):
                    raise DedupeConflict(f'Queued {kind!r} job has different arguments for dedupe key {dedupe_key!r}')
                if row['barrier'] == boundary:
                    return dict(row)
        row = await connection.fetchrow(
            """INSERT INTO jobs
               (id,kind,dataset_id,args,barrier,placement,lane,resources,created_by,dedupe_key,dedupe_owner)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""",
            uuid4(),
            kind,
            dataset_id,
            arguments,
            boundary,
            placement,
            lane,
            requirements,
            created_by,
            dedupe_key,
            owner,
        )
        await connection.execute('INSERT INTO job_controls(job_id) VALUES ($1) ON CONFLICT DO NOTHING', row['id'])
        return {**dict(row), 'cancel_requested': False}


async def claim_job(
    pool: asyncpg.Pool,
    worker_id: str,
    *,
    placements: Sequence[str] = ('server',),
    kinds: Sequence[str] | None = None,
    lanes: Sequence[str] | None = None,
    policies: Mapping[str, JobPolicy] | None = None,
    lease_seconds: float = 60,
    dataset_ids: Sequence[int] | None = None,
    unscoped: bool = True,
) -> Job | None:
    """Lease a slot without overtaking an unfinished operation in its dataset lane. `dataset_ids` limits it to
    jobs on those datasets (a remote worker's: what its owner can access), and then a job on no dataset only
    with `unscoped`."""
    _seconds(lease_seconds)
    if not worker_id:
        raise ValueError('worker_id is required')
    async with _transaction(pool) as connection:
        recovering = await connection.fetchval('SELECT restored_work_pending()')
        await connection.execute(
            """WITH cancelled AS (
                   SELECT jobs.id FROM jobs
                   JOIN job_controls controls ON controls.job_id=jobs.id
                   WHERE state='queued' AND controls.cancel_requested
                   ORDER BY available_at,jobs.id LIMIT 32 FOR UPDATE OF jobs SKIP LOCKED)
               UPDATE jobs SET state='cancelled',lease_token=NULL,lease_expires_at=NULL,
                 updated_at=clock_timestamp(),finished_at=clock_timestamp()
               FROM cancelled WHERE jobs.id=cancelled.id""",
        )
        await connection.execute(
            """WITH orphaned AS (
                   SELECT id FROM jobs WHERE dataset_id IS NOT NULL
                     AND (state='queued' OR (state='running' AND lease_expires_at<=clock_timestamp()))
                     AND NOT EXISTS(SELECT 1 FROM datasets WHERE datasets.id=jobs.dataset_id
                         AND (NOT deleting OR jobs.kind='dataset_deletion'))
                   ORDER BY available_at,id LIMIT 32 FOR UPDATE SKIP LOCKED)
               UPDATE jobs SET state='cancelled',
                 error='{"code":"dataset_deleted","message":"The target dataset was deleted"}'::jsonb,
                 lease_token=NULL,lease_expires_at=NULL,updated_at=clock_timestamp(),finished_at=clock_timestamp()
               FROM orphaned WHERE jobs.id=orphaned.id""",
        )
        await connection.execute(
            """WITH expired AS (
                   SELECT jobs.id,controls.cancel_requested FROM jobs
                   JOIN job_controls controls ON controls.job_id=jobs.id
                   WHERE state='running' AND lease_expires_at<=clock_timestamp()
                   ORDER BY lease_expires_at,jobs.id LIMIT 32 FOR UPDATE OF jobs SKIP LOCKED)
               UPDATE jobs SET
                 state=CASE WHEN expired.cancel_requested THEN 'cancelled'
                            WHEN failures+1>=max_failures THEN 'failed' ELSE 'queued' END,
                 failures=failures+CASE WHEN expired.cancel_requested THEN 0 ELSE 1 END,
                 error=CASE WHEN expired.cancel_requested THEN error ELSE
                   '{"type":"LeaseExpired","message":"Worker lease expired before a safe boundary"}'::jsonb END,
                 available_at=lease_expires_at
                   +LEAST(max_retry_seconds,retry_seconds*power(2::float8,LEAST(failures,30)))*interval '1 second',
                 lease_token=NULL,lease_expires_at=NULL,updated_at=clock_timestamp(),
                 finished_at=CASE WHEN expired.cancel_requested OR failures+1>=max_failures
                                 THEN clock_timestamp() ELSE NULL END
               FROM expired WHERE jobs.id=expired.id""",
        )
        candidates = await connection.fetch(
            f"""WITH owners AS MATERIALIZED (
                    SELECT dataset_id,lane,resources->>'exclusive' AS exclusive FROM jobs WHERE dataset_id IS NOT NULL
                      AND (state='running' OR (state='queued' AND started_at IS NOT NULL))
                    -- A job of no dataset owns no lane, only its exclusive resource (an index restore's).
                    UNION ALL
                    SELECT NULL,NULL,resources->>'exclusive' FROM jobs
                    WHERE dataset_id IS NULL AND resources ? 'exclusive'
                      AND (state='running' OR (state='queued' AND started_at IS NOT NULL)))
                SELECT {JOB_COLUMNS} FROM {JOB_TABLE}
                WHERE placement=ANY($1::text[]) AND ($2::text[] IS NULL OR kind=ANY($2::text[]))
                  AND ($3::text[] IS NULL OR lane=ANY($3::text[]))
                  AND (NOT $4 OR (kind NOT IN ('statistics','concept_scoring','duplicate_maintenance','concept_rescore')
                      -- A backup that already started may be the one a restored dump holds; recovery cancels it.
                      AND NOT (kind='backup' AND jobs.started_at IS NOT NULL)))
                  AND state='queued' AND available_at<=clock_timestamp()
                  AND ($5::int[] IS NULL OR jobs.dataset_id=ANY($5::int[]) OR (jobs.dataset_id IS NULL AND $6))
                  AND (dataset_id IS NULL OR EXISTS(SELECT 1 FROM datasets WHERE datasets.id=jobs.dataset_id
                      AND (NOT deleting OR jobs.kind='dataset_deletion')))
                  AND (dataset_id IS NULL OR jobs.started_at IS NOT NULL OR NOT EXISTS (
                      SELECT 1 FROM owners WHERE owners.dataset_id=jobs.dataset_id AND owners.lane=jobs.lane))
                  AND (jobs.started_at IS NOT NULL OR NOT jobs.resources ? 'exclusive' OR NOT EXISTS (
                      SELECT 1 FROM owners WHERE owners.exclusive=jobs.resources->>'exclusive'))
                ORDER BY available_at,created_at,id LIMIT 32 FOR UPDATE OF jobs SKIP LOCKED""",
            list(placements),
            None if kinds is None else list(kinds),
            None if lanes is None else list(lanes),
            recovering,
            None if dataset_ids is None else list(dataset_ids),
            unscoped,
        )
        for candidate in candidates:
            if candidate['cancel_requested']:
                await _cancel_locked(connection, candidate['id'])
                continue
            if candidate['dataset_id'] is not None:
                lane_row = await connection.fetchval(
                    'SELECT dataset_id FROM dataset_job_lanes WHERE dataset_id=$1 AND lane=$2 FOR UPDATE SKIP LOCKED',
                    candidate['dataset_id'],
                    candidate['lane'],
                )
                if lane_row is None:
                    exists = await connection.fetchval(
                        'SELECT 1 FROM dataset_job_lanes WHERE dataset_id=$1 AND lane=$2',
                        candidate['dataset_id'],
                        candidate['lane'],
                    )
                    if exists is None:
                        await connection.execute(
                            """UPDATE jobs SET state='failed',finished_at=clock_timestamp(),
                               error='{"type":"MissingLane","message":"Dataset job lane is missing"}'::jsonb
                               WHERE id=$1""",
                            candidate['id'],
                        )
                    continue
                if await connection.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM jobs WHERE dataset_id=$1 AND lane=$2 AND id<>$3
                       AND (state='running' OR (state='queued' AND started_at IS NOT NULL)))""",
                    candidate['dataset_id'],
                    candidate['lane'],
                    candidate['id'],
                ):
                    continue
            exclusive = candidate['resources'].get('exclusive')
            if exclusive is not None and candidate['started_at'] is None:
                # At most one started, unfinished job per exclusive resource (one map fit, one index build at a
                # time). The lock orders competing claims; the recheck sees any claim that committed.
                if not await connection.fetchval(
                    'SELECT pg_try_advisory_xact_lock($1, hashtext($2))',
                    EXCLUSIVE_LOCK,
                    exclusive,
                ) or await exclusive_owned(connection, exclusive, candidate['id']):
                    continue
            policy = (policies or {}).get(candidate['kind'], JobPolicy())
            row = await connection.fetchrow(
                """UPDATE jobs SET state='running',worker_id=$2,lease_token=$3,
                   lease_expires_at=clock_timestamp()+$4::float8*interval '1 second',
                   heartbeat_at=clock_timestamp(),updated_at=clock_timestamp(),attempt=attempt+1,
                   max_failures=$5,retry_seconds=$6,max_retry_seconds=$7,
                   started_at=COALESCE(started_at,clock_timestamp()) WHERE id=$1 RETURNING *""",
                candidate['id'],
                worker_id,
                uuid4(),
                lease_seconds,
                policy.max_failures,
                policy.retry_seconds,
                policy.max_retry_seconds,
            )
            return {**dict(row), 'cancel_requested': False}
    return None


async def heartbeat_job(
    pool: DatabaseSource,
    job_id: UUID,
    lease_token: UUID,
    *,
    lease_seconds: float = 60,
) -> Job | None:
    """Renew only a valid claim, returning independent durable cancellation signals."""
    _seconds(lease_seconds)
    async with _transaction(pool) as connection:
        try:
            current = await _locked_job(connection, job_id, lease_token)
        except LeaseLost:
            return None
        row = await connection.fetchrow(
            """UPDATE jobs SET heartbeat_at=clock_timestamp(),updated_at=clock_timestamp(),
               lease_expires_at=clock_timestamp()+$3::float8*interval '1 second'
               WHERE id=$1 AND lease_token=$2 AND state='running' AND lease_expires_at>clock_timestamp() RETURNING *""",
            job_id,
            lease_token,
            lease_seconds,
        )
        return {**dict(row), 'cancel_requested': current['cancel_requested']} if row is not None else None


async def _locked_job(connection: asyncpg.Connection, job_id: UUID, lease_token: UUID) -> Job:
    # FOR NO KEY UPDATE is the lock of the UPDATE that follows: it excludes every other writer of the row
    # (renewals, publications, claims, cancellation) but not a row that only references the job, whose
    # foreign-key check needs the key alone (a slot's telemetry naming its job in workers.current_job_id).
    row = await connection.fetchrow(
        f'SELECT {JOB_COLUMNS} FROM {JOB_TABLE} WHERE jobs.id=$1 FOR NO KEY UPDATE OF jobs',
        job_id,
    )
    current_time = await connection.fetchval('SELECT clock_timestamp()')
    if (
        row is None
        or row['state'] != 'running'
        or row['lease_token'] != lease_token
        or row['lease_expires_at'] <= current_time
    ):
        raise LeaseLost(f'Job {job_id} no longer has this active lease')
    return dict(row)


async def _fenced_job(connection: asyncpg.Connection, job_id: UUID, lease_token: UUID) -> Job:
    """The job row locked inside a handler's own long transaction (finish_locked, checkpoint_locked), with
    its cancellation re-read under a share lock held to the commit: a cancel recorded first wins, and one
    requested meanwhile waits for the commit (and then sees an uncancellable checkpoint). A lock timeout
    here (the job's own lease renewal mid-commit on a slow disk, say) is contention: the caller rolls back
    and the step is retried without spending its failure budget."""
    try:
        current = await _locked_job(connection, job_id, lease_token)
        current['cancel_requested'] = await connection.fetchval(
            'SELECT cancel_requested FROM job_controls WHERE job_id=$1 FOR SHARE',
            job_id,
        )
    except asyncpg.LockNotAvailableError as error:
        raise QueueContended(f'Job {job_id} was busy at its fence (a lease renewal); the step is retried') from error
    return current


async def _cancel_locked(connection: asyncpg.Connection, job_id: UUID) -> Job:
    row = await connection.fetchrow(
        """UPDATE jobs SET state='cancelled',finished_at=clock_timestamp(),updated_at=clock_timestamp(),
           lease_token=NULL,lease_expires_at=NULL WHERE id=$1 RETURNING *""",
        job_id,
    )
    return {**dict(row), 'cancel_requested': True}


async def publish_step(
    pool: asyncpg.Pool,
    job_id: UUID,
    lease_token: UUID,
    *,
    checkpoint: dict | None = None,
    done: int | None = None,
    total: int | None = None,
    message: str | None = None,
    result: Any = None,
    finished: bool = False,
    release: bool = False,
    publisher: Publisher | None = None,
    placement: str | None = None,
) -> Job:
    """Commit effects with their checkpoint; a late cancel stops after this unit. `placement` hands the job's
    next step to the server's runners or to compute workers (a map re-fit's fit runs on compute)."""
    if finished and release:
        raise ValueError('A finished job cannot be requeued')
    if placement not in (None, 'server', 'compute'):
        raise ValueError("placement is 'server' or 'compute'")
    if (done is not None and done < 0) or (total is not None and total < 0):
        raise ValueError('Progress must be nonnegative')
    async with _transaction(pool) as connection:
        current = await _locked_job(connection, job_id, lease_token)
        if current['cancel_requested']:
            return await _cancel_locked(connection, job_id)
        if current['dataset_id'] is not None and current['kind'] != 'dataset_deletion':
            await connection.execute('SELECT require_writable_dataset($1)', current['dataset_id'])
        if publisher is not None:
            await run_isolated(
                publisher(connection),
                name='Publication callback',
                timeout=PUBLISH_TIMEOUT_SECONDS,
                on_error=lambda: _terminate_connection(connection),
            )
        cancelled = await connection.fetchval('SELECT cancel_requested FROM job_controls WHERE job_id=$1', job_id)
        next_state = 'succeeded' if finished else 'cancelled' if cancelled else 'queued' if release else 'running'
        row = await connection.fetchrow(
            """UPDATE jobs SET checkpoint=COALESCE($2,checkpoint),progress_done=COALESCE($3,progress_done),
               progress_total=COALESCE($4,progress_total),message=COALESCE($5,message),
               result=CASE WHEN $7='succeeded' THEN $6 ELSE COALESCE($6,result) END,
               state=$7,updated_at=clock_timestamp(),error=NULL,
               failures=CASE WHEN $2::jsonb IS NOT NULL AND $2::jsonb IS DISTINCT FROM checkpoint
                             THEN 0 ELSE failures END,
               contentions=CASE WHEN $2::jsonb IS NOT NULL AND $2::jsonb IS DISTINCT FROM checkpoint
                                THEN 0 ELSE contentions END,
               available_at=CASE WHEN $7='queued' THEN clock_timestamp() ELSE available_at END,
               finished_at=CASE WHEN $7 IN ('succeeded','cancelled') THEN clock_timestamp() ELSE finished_at END,
               lease_token=CASE WHEN $7='running' THEN lease_token ELSE NULL END,
               lease_expires_at=CASE WHEN $7='running' THEN lease_expires_at ELSE NULL END,
               placement=COALESCE($8,placement)
               WHERE id=$1 AND lease_expires_at>clock_timestamp() RETURNING *""",
            job_id,
            checkpoint,
            done,
            total,
            message,
            result,
            next_state,
            placement,
        )
        if row is None:
            raise LeaseLost(f'Job {job_id} lease expired during publication')
        return {**dict(row), 'cancel_requested': cancelled}


async def finish_locked(
    connection: asyncpg.Connection,
    job_id: UUID,
    lease_token: UUID,
    *,
    result: Any = None,
    message: str | None = None,
    done: int | None = None,
    total: int | None = None,
) -> Job:
    """Record success inside the caller's own long mutation transaction (JOBS.md, long atomic
    operations): the latest job row is locked only now, the lease and cancellation are validated, and
    the caller commits right after. Cancellation recorded before this fence wins (CancelRequested);
    a lost lease raises LeaseLost. Either way the caller rolls back all of its effects."""
    current = await _fenced_job(connection, job_id, lease_token)
    if current['cancel_requested']:
        raise CancelRequested(f'Job {job_id} was cancelled before its result was published')
    row = await connection.fetchrow(
        """UPDATE jobs SET state='succeeded',result=$3,message=COALESCE($4,message),
           progress_done=COALESCE($5,progress_done),progress_total=COALESCE($6,progress_total),error=NULL,
           updated_at=clock_timestamp(),finished_at=clock_timestamp(),lease_token=NULL,lease_expires_at=NULL
           WHERE id=$1 AND lease_token=$2 AND lease_expires_at>clock_timestamp() RETURNING *""",
        job_id,
        lease_token,
        result,
        message,
        done,
        total,
    )
    if row is None:
        raise LeaseLost(f'Job {job_id} lease expired before its result was published')
    return {**dict(row), 'cancel_requested': False}


async def checkpoint_locked(
    connection: asyncpg.Connection,
    job_id: UUID,
    lease_token: UUID,
    *,
    checkpoint: dict,
    message: str | None = None,
) -> Job:
    """Advance the checkpoint inside the caller's own long mutation transaction, like finish_locked but
    keeping the job running under its lease: the caller's effects and the job's next step commit
    together, so a runner that stops after the commit resumes at that step. Cancellation recorded
    before this fence wins (CancelRequested); a lost lease raises LeaseLost."""
    current = await _fenced_job(connection, job_id, lease_token)
    if current['cancel_requested']:
        raise CancelRequested(f'Job {job_id} was cancelled before this step was published')
    row = await connection.fetchrow(
        """UPDATE jobs SET checkpoint=$3,message=COALESCE($4,message),error=NULL,failures=0,contentions=0,
           updated_at=clock_timestamp()
           WHERE id=$1 AND lease_token=$2 AND lease_expires_at>clock_timestamp() RETURNING *""",
        job_id,
        lease_token,
        checkpoint,
        message,
    )
    if row is None:
        raise LeaseLost(f'Job {job_id} lease expired before this step was published')
    return {**dict(row), 'cancel_requested': False}


async def checkpoint_job(pool: asyncpg.Pool, job_id: UUID, lease_token: UUID, **progress: Any) -> Job:
    return await publish_step(pool, job_id, lease_token, **progress)


async def complete_job(pool: asyncpg.Pool, job_id: UUID, lease_token: UUID, result: Any = None) -> Job:
    return await publish_step(pool, job_id, lease_token, result=result, finished=True)


async def _end_attempt(
    pool: asyncpg.Pool,
    job_id: UUID,
    lease_token: UUID,
    error: dict,
    delay_seconds: float | None,
    *,
    count_failure: bool = True,
) -> Job:
    async with _transaction(pool) as connection:
        current = await _locked_job(connection, job_id, lease_token)
        if current['cancel_requested']:
            return await _cancel_locked(connection, job_id)
        failures = current['failures'] + int(count_failure)
        contentions = current['contentions'] if count_failure else min(current['contentions'] + 1, 30)
        terminal = delay_seconds is None or failures >= current['max_failures']
        exponent = current['failures'] if count_failure else current['contentions']
        delay = min(current['max_retry_seconds'], (delay_seconds or 0) * 2 ** min(exponent, 30))
        row = await connection.fetchrow(
            """UPDATE jobs SET state=$2,error=$3,lease_token=NULL,lease_expires_at=NULL,failures=$5,contentions=$6,
               updated_at=clock_timestamp(),available_at=clock_timestamp()+$4::float8*interval '1 second',
               finished_at=CASE WHEN $2='failed' THEN clock_timestamp() ELSE NULL END WHERE id=$1 RETURNING *""",
            job_id,
            'failed' if terminal else 'queued',
            error,
            delay,
            failures,
            contentions,
        )
        return {**dict(row), 'cancel_requested': False}


async def fail_job(pool: asyncpg.Pool, job_id: UUID, lease_token: UUID, error: dict) -> Job:
    return await _end_attempt(pool, job_id, lease_token, error, None)


async def defer_job(
    pool: asyncpg.Pool,
    job_id: UUID,
    lease_token: UUID,
    error: dict,
    *,
    delay_seconds: float = 5,
    count_failure: bool = True,
) -> Job:
    _seconds(delay_seconds, positive=False)
    return await _end_attempt(pool, job_id, lease_token, error, delay_seconds, count_failure=count_failure)


async def cancel_job(pool: asyncpg.Pool, job_id: UUID) -> Job | None:
    """Durably request cancellation without waiting for an in-flight publication. A job whose checkpoint
    names a reason under ``uncancellable`` (a step past the point of no return) is refused while it runs
    (CancelRefused); queued between attempts (its row not being claimed), it is cancelled at once.
    The job's control row is locked first, so a fenced step that is committing that checkpoint
    (checkpoint_locked holds the row shared) is waited for, briefly, and then seen."""
    async with _transaction(pool) as connection:
        await connection.execute('SELECT 1 FROM job_controls WHERE job_id=$1 FOR NO KEY UPDATE', job_id)
        current = await connection.fetchrow(f'SELECT {JOB_COLUMNS} FROM {JOB_TABLE} WHERE jobs.id=$1', job_id)
        if current is None or current['state'] not in ('queued', 'running'):
            return dict(current) if current is not None else None
        reason = (current['checkpoint'] or {}).get('uncancellable')
        if reason:
            idle = await connection.fetchrow('SELECT state FROM jobs WHERE id=$1 FOR UPDATE SKIP LOCKED', job_id)
            if idle is None or idle['state'] != 'queued':
                raise CancelRefused(reason)
            await connection.execute('UPDATE job_controls SET cancel_requested=TRUE WHERE job_id=$1', job_id)
            return await _cancel_locked(connection, job_id)
        await connection.execute('UPDATE job_controls SET cancel_requested=TRUE WHERE job_id=$1', job_id)
        locked = await connection.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE SKIP LOCKED', job_id)
        if locked is not None:
            now = await connection.fetchval('SELECT clock_timestamp()')
            if locked['state'] == 'queued' or (locked['state'] == 'running' and locked['lease_expires_at'] <= now):
                return await _cancel_locked(connection, job_id)
            current = locked
        return {**dict(current), 'cancel_requested': True}
