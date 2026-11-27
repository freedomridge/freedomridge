"""Which kind of sample (image: any sample that isn't an object; object) has been seen with which field, for speed only
(docs/FILTER_QUERY.md, "What the server skips").

The record is kept by the database (schema.sql, 0044): triggers mark a kind in the transaction of every write that puts
a field on a sample of that kind, and it never says a kind lacks a field while a committed sample of that kind has it.
This module reads it for a filter (the reader in routers/datasets.py skips a kind's rows for a field the kind has never
been seen with), and runs the job that examines a dataset from before 0044 and clears a mark once no sample of the kind
has the field.

A filter statement compiled with skips starts with require_field_kinds, which refuses it (SQLSTATE 40001) when, in the
statement's own snapshot, a field skipped for a kind has been seen on it since the record was read: `reading` then
reads the record again, and the third time reads the filter without it.
"""

import asyncio
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator, Mapping, Optional, Sequence, TypeVar

import asyncpg

from . import job_queue
from .field_registry import LOCAL_PREFIX
from .job_runner import StepResult

KIND = 'field_kinds'
POLICY = job_queue.JobPolicy(max_unit_seconds=60, max_failures=3, retry_seconds=5, max_retry_seconds=300)
# The advisory lock's first key (schema.sql's registry): a marking write holds it shared until it ends; the clearing
# takes it exclusive, trying only, for one-row statements on the record (a check's token, the clear).
LOCK = 20261125
# Samples an examination reads in one unit (about a second on a million images with four million objects, measured).
EXAMINE_ROWS = 100_000
# How often the job's schedule looks for datasets to examine, and how old a note of a write that may have taken a kind's
# last sample with a field is when the schedule queues a check itself: the statistics count right after such a write
# queues it first (backend/statistics.py).
SCHEDULE_SECONDS = 10
LEFTOVER_SECONDS = 60
# A pass a write held off (it couldn't take the lock for a mark) waits before the next one: 20 s after the first held
# pass, doubling with each held pass in a row, at most 5 minutes. Notes are hints and a mark left set costs speed only,
# so under writes that never stop a dataset gets at most one pass every 5 minutes, and one soon after a short write.
HELD_BACKOFF_SECONDS = 2 * SCHEDULE_SECONDS
HELD_BACKOFF_MAX_SECONDS = 300
# A unit's share of time for checking marks. A check that reads samples holds no lock anyone waits on: first one
# statement, up to CHECK_TIMEOUT_SECONDS (a present field answers in milliseconds, an absent one reads the kind's rows,
# about 0.65 s for four million objects); past that, a walk of the dataset's rows in id order, WALK_ROWS a statement as
# the examination reads them (about a second each), halving the rows of a statement that runs past
# WALK_TIMEOUT_SECONDS. So no limit stops a mark from being cleared on a dataset of any size: the walk takes time in
# proportion to its rows, in units of the job. The statements under the exclusive lock change one row of the record
# and have CLEAR_TIMEOUT_SECONDS; one that doesn't get the lock (or its time) leaves the mark for a later pass.
CHECK_SECONDS = 8
CHECK_TIMEOUT_SECONDS = 8
WALK_ROWS = 100_000
WALK_TIMEOUT_SECONDS = 20
CLEAR_TIMEOUT_SECONDS = 1
_KIND_SQL = {'image': "media_type <> 'object'", 'object': "media_type = 'object'"}

T = TypeVar('T')
A = TypeVar('A')
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldKinds:
    """An examined dataset's record: each field it names with whether images and objects have been seen with it. A
    field it doesn't name has been seen on neither kind."""

    dataset_id: int
    seen_on: Mapping[str, tuple[bool, bool]]

    def seen(self, name: str, kind: str) -> bool:
        images, objects = self.seen_on.get(name, (False, False))
        return images if kind == 'image' else objects


async def load(source: Any, dataset_id: int) -> Optional[FieldKinds]:
    """The dataset's record, or None while it is not examined (every field then counts as seen on both kinds). None
    too on a database 0044 hasn't run on yet: the server started before scripts/apply_migrations.sh, the state
    /api/health reports as degraded (pending migrations). No dataset is examined there, nothing is skipped, and every
    answer is as without the record; nothing then compiles a guard (require_field_kinds), which needs a record. Only
    the missing table is taken for that: any other error is raised. Catching it is safe only because `source` is a
    pool or `db`, each statement on its own (reading's callers, field-stats, the bulk submit): inside a caller's
    transaction the error would leave that transaction aborted, so never pass a connection that is in one."""
    try:
        rows = await source.fetch(
            """SELECT k.field_name, k.on_images, k.on_objects FROM dataset_field_kinds_state s
               LEFT JOIN dataset_field_kinds k ON k.dataset_id = s.dataset_id WHERE s.dataset_id = $1""",
            dataset_id,
        )
    except asyncpg.UndefinedTableError:
        return None
    if not rows:
        return None
    return FieldKinds(
        dataset_id,
        {row['field_name']: (row['on_images'], row['on_objects']) for row in rows if row['field_name'] is not None},
    )


def members(record: Optional[FieldKinds], name: Optional[str]) -> dict[str, bool]:
    """The API's `on_images` and `on_objects` for a field: false only when the dataset is examined and no sample of
    that kind has been seen with it. `name` is the record's name for the field, None for one it doesn't cover (a list,
    a mirror's own field, a built-in), which is true on both."""
    if record is None or name is None:
        return {'on_images': True, 'on_objects': True}
    images, objects = record.seen_on.get(name, (False, False))
    return {'on_images': images, 'on_objects': objects}


_READING: ContextVar[Optional[FieldKinds]] = ContextVar('field_kinds_reading', default=None)
_COMPILING: ContextVar[Optional[tuple[FieldKinds, set]]] = ContextVar('field_kinds_compiling', default=None)


def current(dataset_id: Optional[int]) -> Optional[FieldKinds]:
    """The record `reading` loaded for this dataset, if any."""
    record = _READING.get()
    return record if record is not None and dataset_id is not None and record.dataset_id == dataset_id else None


@contextmanager
def compiling(record: Optional[FieldKinds], skipped: set) -> Iterator[None]:
    """One filter's compile: the reader skips by `record` and adds to `skipped` each (name, kind) it skipped."""
    token = _COMPILING.set((record, skipped) if record is not None else None)
    try:
        yield
    finally:
        _COMPILING.reset(token)


def compile_state() -> tuple[Optional[FieldKinds], Optional[set]]:
    state = _COMPILING.get()
    return state if state is not None else (None, None)


def guarded(sql: str, args: list, arg_idx: int, dataset_id: int, skipped: set) -> tuple[str, int]:
    """A compiled filter with require_field_kinds in front: a One-Time Filter, so the plan below is unchanged."""
    args.append(sorted(name for name, kind in skipped if kind == 'image'))
    args.append(sorted(name for name, kind in skipped if kind == 'object'))
    guard = f"(SELECT require_field_kinds({int(dataset_id)}, ${arg_idx}::text[], ${arg_idx + 1}::text[]))"
    return f"({guard} AND ({sql}))", arg_idx + 2


def _wrapped(node: Any) -> bool:
    """The filter (a filter tree, or its JSON text) holds the page's wrapping (cg_self), the only form the record is
    read for."""
    if isinstance(node, str):
        try:
            node = json.loads(node)
        except ValueError:
            return False
    if isinstance(node, list):
        return any(map(_wrapped, node))
    if not isinstance(node, dict):
        return False
    if str(node.get('operator', '')).lower() == 'cg_self':
        return True
    return _wrapped(node.get('filters')) or isinstance(node.get('value'), dict) and _wrapped(node['value'])


async def retrying(attempts: Sequence[A], run: Callable[[A], Awaitable[T]], *, savepoint: Any = None) -> T:
    """The one place a statement refused by require_field_kinds (SQLSTATE 40001: a field it skipped for a kind has been
    seen on that kind since it was compiled) runs again: run(attempt) for each attempt in turn, the next only after a
    refusal, and the last one's refusal is raised. With `savepoint`, a connection in a transaction, each attempt runs in
    a savepoint of its own: a refused attempt, a write included, leaves nothing behind for the next."""
    for index, attempt in enumerate(attempts):
        try:
            if savepoint is None:
                return await run(attempt)
            async with savepoint.transaction():
                return await run(attempt)
        except asyncpg.SerializationError:
            if index == len(attempts) - 1:
                raise
    raise ValueError('retrying needs an attempt')


# reading's `record` when the caller hasn't read the record itself.
UNREAD: Any = object()


async def reading(
    source: Any,
    dataset_id: int,
    filters: Any,
    run: Callable[[], Awaitable[T]],
    *,
    savepoint: Any = None,
    record: Any = UNREAD,
    always: bool = False,
) -> T:
    """Runs `run` (it may run again: a read, or a write in `savepoint`) with the dataset's record for every filter it
    compiles. Refused (retrying), it runs again with the record read again, and the third time without it, which
    nothing refuses. Every reader of the page's filter goes through here, right around its own compile and statements,
    so a refusal is retried before any fallback of the reader's (cached values, an empty list) can see it. Inside a
    reading of the same dataset it runs `run` as it is: that reading retries. `record`: the dataset's record the caller
    already read (the sidebar's request, for on_images and on_objects), for the first attempt; its guard checks it like
    any other, and the second attempt reads it again. `always`: with the record whatever the filter's form, for a reader
    that reads the other kind's rows under a filter with nothing wrapped (a field's values on a dataset with objects,
    routers/datasets.py value_view)."""
    if not (always or _wrapped(filters)) or current(dataset_id) is not None:
        return await run()

    async def attempt(which: str) -> T:
        given = record if which == 'given' else await load(source, dataset_id) if which == 'load' else None
        token = _READING.set(given)
        try:
            return await run()
        finally:
            _READING.reset(token)

    return await retrying(('load' if record is UNREAD else 'given', 'load', 'none'), attempt, savepoint=savepoint)


def guarded_sql(sql: str) -> bool:
    """A compiled filter that skips a kind somewhere, and so starts with require_field_kinds (guarded)."""
    return '(SELECT require_field_kinds(' in sql


# --- Clearing marks no sample of their kind needs: by the job below, which the statistics count queues right after it
# (backend/statistics.py) and the schedule queues for notes left over ---
#
# For each mark (a field on a kind) a pass does:
#   A. A check without any lock: a sample of the kind has the field: keep.
#   B. A short transaction under the exclusive lock (tried, not waited for: a write holds it, the pass is held): write a
#      fresh token of this check on the mark (checking_images / checking_objects), only where the mark is on.
#   C. The check again without any lock, every statement of it begun after B committed: present, take the token off
#      (if it is still this check's) and keep.
#   D. A short transaction under the exclusive lock (tried): clear the mark if this check's token is still on it.
#      Otherwise, still under the lock: another check's token on a mark that is on: held (this pass decided nothing
#      about the mark, so it forgets no note; the other check may yet be held off); no token on a mark that is on:
#      kept, decided (a writer, or a check that found the field, took it off); the mark off or the row gone: decided
#      (another check cleared it).
# A writer (field_kinds_mark) takes the lock shared before it reads the mark and holds it to its end; it treats a mark
# with a token as not set and writes it (mark on, token NULL); under REPEATABLE READ its upsert refuses a mark row the
# job changed after its snapshot. Say D clears a kind's mark while a committed sample s of that kind has the field, and
# W is the transaction that last put the field on s. W held the lock at neither B nor D (the tries would have failed).
# If W ended before B, s has had the field since before C began, and C sees it. If W took the lock after B committed, it
# met the token and removed it, and committed before D, which then doesn't find its token. If W took it after D, it
# reads the mark cleared and sets it. The update trigger skips the mark only for a row that already had the field, or a
# field the transaction already marked (holding the lock since). So D never clears a mark a committed sample needs. The
# token, not a flag, makes this hold whatever other check of the same mark runs beside (a job whose lease expired while
# its worker still runs, resumed by another): D finding its own token means nothing touched the mark since its B. C
# need not be one snapshot: a writer that adds the field after B removes the token. Its walk runs to the dataset's last
# row (until a statement comes back empty), so a row committed before B with a higher id than any seen before is read.


def _has(name: str, at: int = 2) -> tuple[str, list]:
    """SQL true for a sample with the field, and its arguments from $<at> on (the dataset is $1): the key (a JSON null
    under it counts), a mirror's own field's key in local_metadata, for `location` the location or the key."""
    if name == 'location':
        return "(location IS NOT NULL OR metadata ? 'location')", []
    if name.startswith(LOCAL_PREFIX):
        return f'local_metadata ? ${at}', [name[len(LOCAL_PREFIX) :]]
    return f'metadata ? ${at}', [name]


def _present_sql(name: str, kind: str) -> tuple[str, list]:
    has, args = _has(name)
    return f"SELECT EXISTS (SELECT 1 FROM samples WHERE dataset_id = $1 AND {_KIND_SQL[kind]} AND {has})", args


async def _present(pool: asyncpg.Pool, dataset_id: int, name: str, kind: str) -> Optional[bool]:
    """A or C as one statement, without any lock: None when it runs out of time (the walk answers then)."""
    sql, args = _present_sql(name, kind)
    try:
        return bool(await pool.fetchval(sql, dataset_id, *args, timeout=CHECK_TIMEOUT_SECONDS))
    except (asyncpg.QueryCanceledError, asyncio.TimeoutError):
        return None


async def _walk(pool: asyncpg.Pool, dataset_id: int, name: str, kind: str, walk: dict) -> tuple[Optional[bool], dict]:
    """One statement of C's walk: the next rows of the dataset in id order after walk['after'], walk['rows'] of them.
    True when one of the kind has the field, False when the walk reached the end without one, None to go on (with the
    walk's new place; a statement that runs out of time is tried again with half the rows)."""
    has, args = _has(name)
    at = 2 + len(args)
    sql = (
        f'WITH chunk AS MATERIALIZED (SELECT id, ({_KIND_SQL[kind]} AND {has}) AS hit FROM samples '
        f'WHERE dataset_id = $1 AND id > ${at} ORDER BY id LIMIT ${at + 1}) '
        'SELECT max(id) AS last, COALESCE(bool_or(hit), FALSE) AS found FROM chunk'
    )
    try:
        row = await pool.fetchrow(sql, dataset_id, *args, walk['after'], walk['rows'], timeout=WALK_TIMEOUT_SECONDS)
    except (asyncpg.QueryCanceledError, asyncio.TimeoutError):
        return None, {**walk, 'rows': max(1, walk['rows'] // 2)}
    if row['found']:
        return True, walk
    if row['last'] is None:
        return False, walk
    return None, {**walk, 'after': row['last']}


async def _locked(
    pool: asyncpg.Pool,
    dataset_id: int,
    work: Callable[[asyncpg.Connection], Awaitable[T]],
) -> tuple[bool, Optional[T]]:
    """B or D: a short READ COMMITTED transaction holding the exclusive lock, tried (a write in flight makes it (False,
    None)), for one-row statements on the record, each in a snapshot taken after the lock: (True, what `work` gave)."""
    try:
        connection = await pool.acquire(timeout=5)
    except asyncio.TimeoutError:
        return False, None
    try:
        async with connection.transaction(isolation='read_committed'):
            await connection.execute(
                f"SET LOCAL statement_timeout = '{CLEAR_TIMEOUT_SECONDS}s'; "
                f"SET LOCAL lock_timeout = '{CLEAR_TIMEOUT_SECONDS}s'",
            )
            if not await connection.fetchval('SELECT pg_try_advisory_xact_lock(20261125, $1)', dataset_id):
                return False, None
            return True, await work(connection)
    except (asyncpg.QueryCanceledError, asyncpg.LockNotAvailableError):
        return False, None
    finally:
        await pool.release(connection)


def _columns(kind: str) -> tuple[str, str]:
    return ('on_images', 'checking_images') if kind == 'image' else ('on_objects', 'checking_objects')


async def _flag(pool: asyncpg.Pool, dataset_id: int, name: str, kind: str) -> Optional[str]:
    """B: this check's token, written on the mark; '' when the mark isn't on (nothing to clear); None when held."""
    on, checking = _columns(kind)

    async def work(connection: asyncpg.Connection) -> str:
        token = await connection.fetchval(
            f'UPDATE dataset_field_kinds SET {checking} = gen_random_uuid() '
            f'WHERE dataset_id = $1 AND field_name = $2 AND {on} RETURNING {checking}::text',
            dataset_id,
            name,
        )
        return token or ''

    got, token = await _locked(pool, dataset_id, work)
    return token if got else None


async def _unflag(pool: asyncpg.Pool, dataset_id: int, name: str, kind: str, token: str) -> None:
    """C found the field: this check's token goes, without the lock (a token left only makes the next writer of the
    field write the mark again)."""
    _, checking = _columns(kind)
    try:
        async with pool.acquire(timeout=5) as connection, connection.transaction():
            await connection.execute(
                f"SET LOCAL statement_timeout = '{CLEAR_TIMEOUT_SECONDS}s'; "
                f"SET LOCAL lock_timeout = '{CLEAR_TIMEOUT_SECONDS}s'",
            )
            await connection.execute(
                f'UPDATE dataset_field_kinds SET {checking} = NULL '
                f'WHERE dataset_id = $1 AND field_name = $2 AND {checking} = $3::uuid',
                dataset_id,
                name,
                token,
            )
    except (asyncpg.QueryCanceledError, asyncpg.LockNotAvailableError, asyncio.TimeoutError):
        return


async def _clear_flagged(pool: asyncpg.Pool, dataset_id: int, name: str, kind: str, token: str) -> Optional[bool]:
    """D: True when the mark was cleared (this check's token still on it). Otherwise, under the lock: another check's
    token on a mark that is on: None, held; no token on a mark that is on: False, kept, decided; the mark off or the
    row gone: False, decided. None too when the lock isn't got. A row with neither kind marked goes."""
    on, checking = _columns(kind)

    async def work(connection: asyncpg.Connection) -> Optional[bool]:
        status = await connection.execute(
            f'UPDATE dataset_field_kinds SET {on} = FALSE, {checking} = NULL '
            f'WHERE dataset_id = $1 AND field_name = $2 AND {on} AND {checking} = $3::uuid',
            dataset_id,
            name,
            token,
        )
        if status != 'UPDATE 1':
            other = await connection.fetchval(
                f'SELECT {on} AND {checking} IS NOT NULL FROM dataset_field_kinds '
                'WHERE dataset_id = $1 AND field_name = $2',
                dataset_id,
                name,
            )
            return None if other else False
        await connection.execute(
            'DELETE FROM dataset_field_kinds WHERE dataset_id = $1 AND field_name = $2 '
            'AND NOT on_images AND NOT on_objects',
            dataset_id,
            name,
        )
        return True

    got, cleared = await _locked(pool, dataset_id, work)
    return cleared if got else None


async def check_start(pool: asyncpg.Pool, dataset_id: int) -> Optional[dict]:
    """A clearing pass over every mark of an examined dataset, when a write that may have taken a kind's last sample
    with a field was noted. None when there is nothing to check, or a held pass is backing off. The pass keeps the
    snapshot taken here (before its marks are read and before any check begins): at its end it forgets just the notes
    whose transaction had committed by then (finish_check)."""
    state = await pool.fetchrow(
        """SELECT pg_current_snapshot()::text AS snapshot, s.retry_after > clock_timestamp() AS waiting,
                  EXISTS (SELECT 1 FROM dataset_field_kinds_pending p WHERE p.dataset_id = s.dataset_id) AS noted
           FROM dataset_field_kinds_state s WHERE s.dataset_id = $1""",
        dataset_id,
        timeout=5,
    )
    if state is None or state['waiting'] or not state['noted']:
        return None
    rows = await pool.fetch(
        'SELECT field_name, on_images, on_objects FROM dataset_field_kinds WHERE dataset_id = $1 ORDER BY field_name',
        dataset_id,
        timeout=5,
    )
    marks = [
        [row['field_name'], kind]
        for row in rows
        for kind, marked in (('image', row['on_images']), ('object', row['on_objects']))
        if marked
    ]
    return {'snapshot': state['snapshot'], 'marks': marks, 'next': 0, 'walk': None, 'cleared': [], 'held': False}


def checked_all(state: dict) -> bool:
    return state['next'] >= len(state['marks'])


async def check_some(pool: asyncpg.Pool, dataset_id: int, state: dict) -> dict:
    """Takes the pass a step or more further (at least one), for about CHECK_SECONDS; returns it as it then stands. A
    mark whose C is walking keeps its place and its check's token in state['walk'] between units (B committed before
    the walk began)."""
    marks, index, walk = state['marks'], state['next'], state.get('walk')
    cleared, held = list(state['cleared']), bool(state['held'])
    loop = asyncio.get_running_loop()
    started = loop.time()
    stepped = False
    while index < len(marks) and not (stepped and loop.time() - started >= CHECK_SECONDS):
        stepped = True
        name, kind = marks[index]
        if walk is None:
            present = await _present(pool, dataset_id, name, kind)  # A
            if present:
                index += 1
                continue
            token = await _flag(pool, dataset_id, name, kind)  # B
            if not token:
                held = held or token is None
                index += 1
                continue
            # C: as one statement, unless A already ran out of time; else the walk.
            present = await _present(pool, dataset_id, name, kind) if present is False else None
            if present is None:
                walk = {'after': 0, 'rows': WALK_ROWS, 'token': token}
                continue
        else:
            token = walk['token']
            present, walk = await _walk(pool, dataset_id, name, kind, walk)
            if present is None:
                continue
        walk = None
        if present:
            await _unflag(pool, dataset_id, name, kind, token)
        else:
            outcome = await _clear_flagged(pool, dataset_id, name, kind, token)  # D
            if outcome is None:
                held = True
            elif outcome:
                cleared.append([name, kind])
        index += 1
    return {**state, 'next': index, 'walk': walk, 'cleared': cleared, 'held': held}


async def finish_check(connection: asyncpg.Connection, dataset_id: int, state: dict) -> None:
    """The end of a pass: the notes whose transaction had committed when the pass began go (its checks all began after
    those writes), unless a mark was left undecided (a write held the lock); then the next pass waits
    (HELD_BACKOFF_SECONDS, doubling with each held pass in a row). A note committed while the pass ran stays for the
    next one."""
    if state['held']:
        await connection.execute(
            """UPDATE dataset_field_kinds_state SET held_passes = held_passes + 1,
                   retry_after = clock_timestamp() + make_interval(secs => least($3, $2 * 2 ^ least(held_passes, 10)))
               WHERE dataset_id = $1""",
            dataset_id,
            float(HELD_BACKOFF_SECONDS),
            float(HELD_BACKOFF_MAX_SECONDS),
        )
        return
    await connection.execute(
        'DELETE FROM dataset_field_kinds_pending '
        'WHERE dataset_id = $1 AND pg_visible_in_snapshot(xact, $2::text::pg_snapshot)',
        dataset_id,
        state['snapshot'],
    )
    await connection.execute(
        'UPDATE dataset_field_kinds_state SET held_passes = 0, retry_after = NULL '
        'WHERE dataset_id = $1 AND (held_passes > 0 OR retry_after IS NOT NULL)',
        dataset_id,
    )


async def queue_check(pool: asyncpg.Pool, dataset_id: int) -> None:
    """Queues a clearing pass for an examined dataset with notes, unless a held pass is backing off: what the
    statistics count calls right after it. A pass checks every mark. Never raises: the count must not fail over the
    record, which is speed only (a pass not queued here is queued by the schedule once its notes are old); what it
    caught is logged."""
    try:
        wanted = await pool.fetchval(
            """SELECT (s.retry_after IS NULL OR s.retry_after <= clock_timestamp())
                      AND EXISTS (SELECT 1 FROM dataset_field_kinds_pending p WHERE p.dataset_id = s.dataset_id)
               FROM dataset_field_kinds_state s WHERE s.dataset_id = $1""",
            dataset_id,
            timeout=5,
        )
        if wanted:
            await job_queue.enqueue_job(
                pool,
                KIND,
                dataset_id=dataset_id,
                lane='maintenance',
                dedupe_key=f'{KIND}:{dataset_id}',
            )
    except Exception:
        logger.exception('Could not queue the field kinds check of dataset %s after its statistics', dataset_id)


# --- The job: examine a dataset from before 0044, and check notes a count of the statistics left ---


async def schedule(pool: asyncpg.Pool, *, limit: int = 10) -> list:
    """Queues the job for datasets not yet examined, and for notes older than LEFTOVER_SECONDS that no statistics job
    (queued or running) is about to queue, unless a held pass is backing off."""
    rows = await pool.fetch(
        """SELECT d.id FROM datasets d
           WHERE NOT d.deleting
             AND (NOT EXISTS (SELECT 1 FROM dataset_field_kinds_state s WHERE s.dataset_id = d.id)
                  OR (EXISTS (SELECT 1 FROM dataset_field_kinds_pending p WHERE p.dataset_id = d.id
                              AND p.noted_at < clock_timestamp() - make_interval(secs => $3))
                      AND NOT EXISTS (SELECT 1 FROM dataset_field_kinds_state s WHERE s.dataset_id = d.id
                                      AND s.retry_after > clock_timestamp())
                      AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.dataset_id = d.id AND j.kind = 'statistics'
                                      AND j.state IN ('queued', 'running'))))
             AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.dataset_id = d.id AND j.kind = $1
                             AND (j.state IN ('queued', 'running')
                                  OR (j.state = 'failed' AND j.finished_at > clock_timestamp() - interval '5 minutes')))
           ORDER BY d.id LIMIT $2""",
        KIND,
        limit,
        float(LEFTOVER_SECONDS),
        timeout=5,
    )
    jobs = []
    for row in rows:
        try:
            jobs.append(
                await job_queue.enqueue_job(
                    pool,
                    KIND,
                    dataset_id=row['id'],
                    lane='maintenance',
                    dedupe_key=f"{KIND}:{row['id']}",
                ),
            )
        except (job_queue.QueueBusy, asyncpg.ObjectNotInPrerequisiteStateError, asyncpg.LockNotAvailableError):
            continue
    return jobs


_EXAMINE_SQL = """
WITH chunk AS MATERIALIZED (
    SELECT id, media_type = 'object' AS is_object, location IS NOT NULL AS located,
           CASE WHEN jsonb_typeof(metadata) = 'object' THEN metadata ELSE '{}'::jsonb END AS metadata,
           CASE WHEN jsonb_typeof(local_metadata) = 'object' THEN local_metadata ELSE '{}'::jsonb END AS local_metadata
    FROM samples WHERE dataset_id = $1 AND id > $2 ORDER BY id LIMIT $3
), seen AS (
    SELECT c.is_object, k.key FROM chunk c CROSS JOIN LATERAL jsonb_object_keys(c.metadata) AS k(key)
    UNION
    SELECT c.is_object, '@local.' || k.key FROM chunk c CROSS JOIN LATERAL jsonb_object_keys(c.local_metadata) AS k(key)
    UNION
    SELECT is_object, 'location' FROM chunk WHERE located
)
SELECT (SELECT max(id) FROM chunk) AS last, (SELECT count(*) FROM chunk) AS rows,
       ARRAY(SELECT key FROM seen WHERE NOT is_object ORDER BY key) AS images,
       ARRAY(SELECT key FROM seen WHERE is_object ORDER BY key) AS objects
"""


def handler(pool: asyncpg.Pool) -> Callable[[dict], Awaitable[StepResult]]:
    async def measure(job: dict) -> StepResult:
        if job['lane'] != 'maintenance' or job['dataset_id'] is None:
            raise ValueError('The field kinds job needs a dataset and the maintenance lane')
        dataset_id = job['dataset_id']
        checkpoint = dict(job['checkpoint'])
        phase = checkpoint.get('phase', 'start')
        if phase == 'start':
            state = await check_start(pool, dataset_id)
            if state is None and not await pool.fetchval(
                'SELECT EXISTS (SELECT 1 FROM dataset_field_kinds_state WHERE dataset_id = $1)',
                dataset_id,
                timeout=5,
            ):
                return StepResult(
                    checkpoint={'phase': 'examine', 'after': 0},
                    message='Noting which kinds of sample have each field',
                )
            if state is None:
                return StepResult(finished=True, result={'cleared': [], 'held': False}, message='Nothing to check')
            return StepResult(
                checkpoint={'phase': 'check', 'state': state},
                total=len(state['marks']),
                message='Checking which kinds of sample still have each field',
            )
        if phase == 'examine':
            found = await pool.fetchrow(
                _EXAMINE_SQL,
                dataset_id,
                checkpoint['after'],
                EXAMINE_ROWS,
                timeout=POLICY.max_unit_seconds,
            )
            if found['last'] is None:

                async def examined(connection: asyncpg.Connection) -> None:
                    # With a note, so that a pass then checks every mark: one a write without the triggers left (it
                    # deleted this row, schema.sql) has a note waiting like any other.
                    await connection.execute(
                        'INSERT INTO dataset_field_kinds_state (dataset_id) VALUES ($1) ON CONFLICT DO NOTHING',
                        dataset_id,
                    )
                    await connection.execute(
                        'INSERT INTO dataset_field_kinds_pending (dataset_id) VALUES ($1)',
                        dataset_id,
                    )

                return StepResult(
                    finished=True,
                    result={'examined': True, 'samples': job['progress_done']},
                    message='Noted which kinds of sample have each field',
                    publish=examined,
                )

            async def noted(connection: asyncpg.Connection) -> None:
                await connection.execute(
                    'SELECT field_kinds_mark($1, $2::text[], $3::text[])',
                    dataset_id,
                    found['images'],
                    found['objects'],
                )

            return StepResult(
                checkpoint={'phase': 'examine', 'after': found['last']},
                done=job['progress_done'] + found['rows'],
                message='Noting which kinds of sample have each field',
                publish=noted,
            )
        state = await check_some(pool, dataset_id, checkpoint['state'])
        if not checked_all(state):
            return StepResult(
                checkpoint={'phase': 'check', 'state': state},
                done=state['next'],
                message='Checking which kinds of sample still have each field',
            )

        async def checked(connection: asyncpg.Connection) -> None:
            await finish_check(connection, dataset_id, state)

        return StepResult(
            finished=True,
            done=state['next'],
            result={'cleared': state['cleared'], 'held': state['held']},
            message='Checked which kinds of sample still have each field',
            publish=checked,
        )

    return measure
