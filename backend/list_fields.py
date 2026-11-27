"""Making a list field kept in a column of its own a dataset's (POST /datasets/{id}/array-fields, docs/SDK.md "List
fields"), when another dataset made the column first.

Such a column is one column of the whole samples table, shared by every dataset that registers the name. A dataset's
rows written before the column lost its default (0045_list_columns_hold_never_set.sql) hold the old default there, the
empty list, though none of them ever had the field; its rows written since hold no value (NULL). So when a dataset
registers an existing name, its rows holding the empty list become never set first, and only then is the field the
dataset's. No list written after the registration starts is lost: until it ends the field is not the dataset's, and a
write that names it is refused (409) while the job runs (refuse_while_registering). A row holding elements is never
changed.

- At most INLINE_SAMPLES samples: inside the request, in the registration's own transaction (the route).
- A larger dataset: a job of KIND in the dataset's user lane. It walks the dataset's ids in order, STEP_IDS a step, each
  step a transaction of its own that sets that range's empty lists to never set and records how far it is, so a job
  stopped in between is taken up again. Its last step makes the field the dataset's. last_modified_at is not touched:
  the rows say the same as before. A version's restore is refused while it runs (bulk_jobs.require_no_pending_bulk).
- A mirror runs the same walk inside its sync (follower_sync), and registers the field only after it.

Measured on a table of 2.1 million rows of about 950 bytes, the column's GIN index in place: a million rows 215 s,
10.9 GB of WAL written, 1.18 GB of growth of the table and its indexes; a dataset whose rows were all written after the
upgrade, 3.9 s a million rows and nothing changed."""

import time
from typing import Any, Optional

import asyncpg
from fastapi import HTTPException

from . import job_queue
from .job_runner import StepResult
from .job_wait import JobConflict

KIND = 'list_field_registration'
# A step is a range of STEP_IDS ids (one statistics block); one that outruns its budget is taken again at half the
# width, down to STEP_IDS >> MAX_HALVINGS, before the job fails.
MAX_HALVINGS = 6
POLICY = job_queue.JobPolicy(max_unit_seconds=60, max_failures=MAX_HALVINGS + 2, retry_seconds=2, max_retry_seconds=60)
STEP_IDS = 4096
# A dataset of at most this many samples is done inside the request (the bound the bulk edits use).
INLINE_SAMPLES = 1000
# How long, in seconds, until the job that holds a write back is expected to end: the SDK keeps retrying that long.
END_HEADER = 'X-Job-End-Seconds'
RETRY_SECONDS = 5
# Before a job has walked anything, its end is guessed from the dataset's size at this many seconds a row (measured:
# about 0.2 ms a row changed on a busy machine), and at least this much.
GUESS_SECONDS_PER_ROW = 0.001
GUESS_MIN_SECONDS = 30


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def pending(source: Any, dataset_id: int, names=None) -> Optional[asyncpg.Record]:
    """The unfinished registration job on the dataset of one of these names (of any name when None), if any."""
    if names is not None:
        names = sorted({name for name in names if isinstance(name, str)})
        if not names:
            return None
    return await source.fetchrow(
        f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
            WHERE kind = $1 AND dataset_id = $2 AND ($3::text[] IS NULL OR args->>'field_name' = ANY($3::text[]))
              AND state IN ('queued', 'running')
            ORDER BY created_at, jobs.id LIMIT 1""",
        KIND,
        dataset_id,
        names,
    )


def expected_seconds(job: Any) -> int:
    """How long until the job is expected to end: by its pace so far, or by the dataset's size before it has one."""
    done, total = job['progress_done'] or 0, job['progress_total'] or 0
    started = job['started_at']
    if started is not None and done > 0 and total >= done:
        elapsed = max(0.0, time.time() - started.timestamp())
        return max(1, int(elapsed * (total - done) / done) + RETRY_SECONDS)
    return max(GUESS_MIN_SECONDS, int(total * GUESS_SECONDS_PER_ROW) + GUESS_MIN_SECONDS)


def busy(job: Any) -> HTTPException:
    """The 409 a write that names a field gets while the field is being made the dataset's."""
    seconds = expected_seconds(job)
    return HTTPException(
        status_code=409,
        detail=(
            f"'{job['args']['field_name']}' is being added to this dataset (job {job['id']}); retry when it finishes, "
            f'in about {seconds} s'
        ),
        headers={'Retry-After': str(RETRY_SECONDS), END_HEADER: str(seconds)},
    )


async def refuse_while_registering(source: Any, dataset_id: int, names) -> None:
    """Refuse (409, with Retry-After and END_HEADER) a write naming a field whose registration job is unfinished."""
    job = await pending(source, dataset_id, names)
    if job is not None:
        raise busy(job)


async def empty_to_never_set(
    connection: Any,
    dataset_id: int,
    name: str,
    lower: Optional[int] = None,
    upper: Optional[int] = None,
) -> int:
    """Set the dataset's rows holding the empty list in the column `name` (checked by the caller as a column name) to
    never set, those with ids in [lower, upper) when given. Rows holding elements, and rows already never set, are not
    written. Returns how many rows changed."""
    await connection.execute('SELECT require_writable_dataset($1)', dataset_id)
    column = _quoted(name)
    if lower is None:
        status = await connection.execute(
            f"UPDATE samples SET {column} = NULL WHERE dataset_id = $1 AND {column} = '{{}}'::text[]",
            dataset_id,
        )
    else:
        status = await connection.execute(
            f"UPDATE samples SET {column} = NULL WHERE dataset_id = $1 AND id >= $2 AND id < $3 "
            f"AND {column} = '{{}}'::text[]",
            dataset_id,
            lower,
            upper,
        )
    return int(status.split()[-1])


async def registered_here(connection: Any, dataset_id: int, name: str) -> bool:
    """Whether the list field is the dataset's already."""
    origin = await connection.fetchval(
        'SELECT origin FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
        dataset_id,
        name,
    )
    return origin == 'native_array'


async def register(connection: Any, dataset_id: int, name: str) -> None:
    """Make the list field the dataset's: its definition (origin native_array), and its statistics counted again. The
    caller holds the dataset's field-lifecycle gate exclusively and has checked the name is no metadata field.

    created_at says since when the field is a list field of the dataset (a version that did not record the field
    registry reads by it: versions._version_lists), so a row of another kind taken over gets now."""
    await connection.execute(
        """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, is_promoted)
           VALUES ($1, $2, 'list', 'native_array', TRUE)
           ON CONFLICT (dataset_id, field_name) DO UPDATE SET field_type = 'list',
               origin = 'native_array', is_promoted = TRUE,
               created_at = CASE WHEN dataset_fields.origin = 'native_array' THEN dataset_fields.created_at
                                 ELSE CURRENT_TIMESTAMP END""",
        dataset_id,
        name,
    )
    await connection.execute(
        'DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2',
        dataset_id,
        name,
    )


async def submit(pool: Any, dataset_id: int, user_id: Optional[int], name: str) -> Optional[dict]:
    """Join the unfinished registration of this name on the dataset, or queue one. None when the field is the
    dataset's already (a job that ended between the caller's look and this one)."""
    async with job_queue._transaction(pool) as connection:
        await connection.execute('SELECT require_writable_dataset($1)', dataset_id)
        await connection.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
            f'{KIND}:{dataset_id}:{name}',
        )
        existing = await pending(connection, dataset_id, [name])
        if existing is not None:
            return dict(existing)
        # Looked at after the job: a job that ended has made the field the dataset's in its last commit.
        if await registered_here(connection, dataset_id, name):
            return None
        return await job_queue.enqueue_job(
            connection,
            KIND,
            dataset_id=dataset_id,
            args={'field_name': name},
            lane='user',
            created_by=user_id,
        )


def handler(pool: Any):
    async def step(job: dict) -> StepResult:
        dataset_id, name = job['dataset_id'], job['args']['field_name']
        checkpoint = dict(job['checkpoint'])
        if 'end' not in checkpoint:
            # The rows to walk: the dataset's ids up to the last one there now. A row added later holds no value for
            # the field already (the column has no default).
            row = await pool.fetchrow(
                'SELECT max(id) AS last, count(*) AS total FROM samples WHERE dataset_id = $1',
                dataset_id,
                timeout=POLICY.max_unit_seconds,
            )
            return StepResult(
                checkpoint={'after': 0, 'end': row['last'] or 0, 'walked': 0, 'changed': 0},
                done=0,
                total=row['total'],
                message=f'Making {name} a field of this dataset: its empty lists from before become never set',
            )
        after, end = checkpoint['after'], checkpoint['end']
        start = None
        if after <= end:
            start = await pool.fetchval(
                'SELECT min(id) FROM samples WHERE dataset_id = $1 AND id >= $2 AND id <= $3',
                dataset_id,
                after,
                end,
                timeout=POLICY.max_unit_seconds,
            )
        if start is None:

            async def registered(connection: asyncpg.Connection) -> None:
                if not await connection.fetchval('SELECT pg_try_advisory_xact_lock(20261016, $1)', dataset_id):
                    raise job_queue.QueueContended('Waiting for the field definitions')
                origin = await connection.fetchval(
                    'SELECT origin FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
                    dataset_id,
                    name,
                )
                if origin == 'metadata':
                    raise JobConflict(f"'{name}' is already a metadata field")
                await register(connection, dataset_id, name)

            return StepResult(
                checkpoint=checkpoint,
                finished=True,
                done=checkpoint['walked'],
                result={'message': f"Array field '{name}' registered for this dataset", 'created': False},
                message=f'{name} is a field of this dataset; {checkpoint["changed"]:,} empty lists became never set',
                publish=registered,
            )
        # A step that outran its budget (a slow disk, a busy table) is taken again at half the width.
        width = max(1, STEP_IDS >> min(job['failures'] or 0, MAX_HALVINGS))
        stop = min(start + width, end + 1)
        rows = await pool.fetchval(
            'SELECT count(*) FROM samples WHERE dataset_id = $1 AND id >= $2 AND id < $3',
            dataset_id,
            start,
            stop,
            timeout=POLICY.max_unit_seconds,
        )
        checkpoint.update(after=stop, walked=checkpoint['walked'] + rows)

        async def walked(connection: asyncpg.Connection) -> None:
            if not await connection.fetchval('SELECT pg_try_advisory_xact_lock_shared(20261016, $1)', dataset_id):
                raise job_queue.QueueContended('Waiting for the field definitions')
            # Made the dataset's meanwhile some other way (a request that came in before this job was there to be
            # joined): an empty list in its rows now may have been written since, so none is changed.
            if await registered_here(connection, dataset_id, name):
                return
            checkpoint['changed'] += await empty_to_never_set(connection, dataset_id, name, start, stop)

        return StepResult(
            checkpoint=checkpoint,
            done=checkpoint['walked'],
            message=f'Making {name} a field of this dataset: its empty lists from before become never set',
            publish=walked,
        )

    return step
