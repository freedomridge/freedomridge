"""Bounded concept upkeep driven by committed changes, not embedding ID watermarks."""

import asyncio
import json
import time
from collections import deque

import asyncpg
import numpy as np
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from . import job_queue
from .job_runner import StepResult
from .job_tasks import run_isolated
from .job_wait import JobConflict, JobInputError

KIND = 'concept_scoring'
BATCH_SIZE = 1024
DELIVERY_BATCH_SIZE = 4096
POLICY = job_queue.JobPolicy(max_unit_seconds=60, max_failures=3, retry_seconds=2, max_retry_seconds=120)


def automatic_upkeep(alias):
    """Automatic upkeep leaves a concept alone while an explicit job owns it (queued or running) and
    while its scores do not match its definition (a stopped explicit job is resumed, not overtaken)."""
    return (
        f"{alias}.scores_ready AND ({alias}.score_job_id IS NULL OR NOT EXISTS(SELECT 1 FROM jobs owner "
        f"WHERE owner.id={alias}.score_job_id AND owner.state IN ('queued','running')))"
    )


async def _read(pool, operation, *, seconds=5):
    started = time.monotonic()
    acquired = False
    try:
        async with pool.acquire(timeout=1) as connection:
            acquired = True

            async def execute():
                async with connection.transaction(isolation='repeatable_read', readonly=True):
                    await connection.execute(
                        f"SET LOCAL statement_timeout='{seconds}s'; "
                        f"SET LOCAL idle_in_transaction_session_timeout='{seconds + 5}s'",
                    )
                    return await operation(connection)

            return await run_isolated(
                execute(),
                name='Concept measurement',
                timeout=max(0, seconds + 1 - (time.monotonic() - started)),
                on_error=lambda: job_queue._terminate_connection(connection),
            )
    except (TimeoutError, asyncio.TimeoutError) as error:
        if not acquired:
            raise job_queue.QueueBusy('Concept measurement is waiting for a database connection') from error
        raise


async def request_scoring(pool, concept_id):
    async with job_queue._transaction(pool) as connection:
        await connection.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
            f'concept-scoring:{concept_id}',
        )
        target = await connection.fetchrow(
            f'''SELECT dataset_id,embedding_field FROM embedding_concepts concept WHERE id=$1 AND auto_rescore
               AND {automatic_upkeep('concept')}
               AND EXISTS(SELECT 1 FROM datasets WHERE id=dataset_id AND NOT deleting)''',
            concept_id,
        )
        if target is None:
            return None
        await connection.fetchval(
            'SELECT dispatch_concept_score_work($1,$2,$3)',
            DELIVERY_BATCH_SIZE,
            target['dataset_id'],
            target['embedding_field'],
        )
        existing = await connection.fetchrow(
            f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
                WHERE kind='concept_scoring' AND args->>'concept_id'=$1
                  AND state IN ('queued','running') AND NOT cancel_requested
                ORDER BY created_at,id LIMIT 1""",
            str(concept_id),
        )
        if existing:
            return dict(existing)
        boundary = await connection.fetchrow(
            """SELECT dataset_id,score_revision,NOT score_initialized AS bootstrap,
                      txid_current_snapshot()::text AS snapshot,
                      GREATEST(COALESCE((SELECT max(id) FROM samples WHERE dataset_id=concept.dataset_id),0),
                               COALESCE((SELECT max(sample_id) FROM embedding_concept_scores
                                         WHERE concept_id=concept.id),0)) AS upper_id
               FROM embedding_concepts concept WHERE id=$1 AND auto_rescore""",
            concept_id,
        )
        if boundary is None:
            return None
        previous = await connection.fetchrow(
            """SELECT state,barrier,checkpoint FROM jobs WHERE kind='concept_scoring'
               AND args->>'concept_id'=$1 ORDER BY created_at DESC,id DESC LIMIT 1""",
            str(concept_id),
        )
        resume = (
            previous is not None
            and previous['state'] in ('failed', 'cancelled')
            and previous['barrier'].get('score_revision') == boundary['score_revision']
            and previous['barrier'].get('bootstrap') == boundary['bootstrap']
        )
        job = await job_queue.enqueue_job(
            connection,
            KIND,
            dataset_id=boundary['dataset_id'],
            lane='maintenance',
            args={'concept_id': concept_id},
            barrier=previous['barrier'] if resume else dict(boundary),
            dedupe_key=f'concept:{concept_id}',
        )
        if resume:
            await connection.execute('UPDATE jobs SET checkpoint=$2 WHERE id=$1', job['id'], previous['checkpoint'])
            job['checkpoint'] = previous['checkpoint']
        return job


async def schedule_concepts(pool, limit=10):
    await dispatch_changes(pool)
    try:
        async with job_queue._transaction(pool) as connection:
            for table in ('concept_score_pending', 'concept_score_count_deltas'):
                enabled = 'AND auto_rescore' if table == 'concept_score_pending' else ''
                await connection.execute(
                    f"""DELETE FROM {table} WHERE ctid IN (
                        SELECT pending.ctid FROM {table} pending
                        WHERE NOT EXISTS(SELECT 1 FROM embedding_concepts WHERE id=pending.concept_id {enabled})
                          AND NOT EXISTS(SELECT 1 FROM embedding_concepts concept JOIN datasets dataset
                              ON dataset.id=concept.dataset_id
                              WHERE concept.id=pending.concept_id AND dataset.deleting)
                        LIMIT 1000)""",
                )
    except (job_queue.QueueBusy, asyncpg.ObjectNotInPrerequisiteStateError):
        # Delete admission can win after the cleanup statement took its snapshot.
        pass

    async def candidates(connection):
        return await connection.fetch(
            f"""SELECT concept.id FROM embedding_concepts concept
               WHERE auto_rescore AND {automatic_upkeep('concept')}
                 AND EXISTS(SELECT 1 FROM datasets WHERE id=concept.dataset_id AND NOT deleting)
                 AND (NOT score_initialized
                   OR EXISTS(SELECT 1 FROM concept_score_pending WHERE concept_id=concept.id)
                   OR EXISTS(SELECT 1 FROM concept_score_count_deltas WHERE concept_id=concept.id))
                 AND NOT EXISTS(SELECT 1 FROM jobs WHERE kind='concept_scoring'
                     AND args->>'concept_id'=concept.id::text AND state IN ('queued','running'))
                 AND NOT EXISTS(SELECT 1 FROM jobs WHERE kind='concept_scoring'
                     AND args->>'concept_id'=concept.id::text AND state='failed'
                     AND (finished_at>clock_timestamp()-interval '5 minutes'
                          OR (barrier->>'score_revision'=concept.score_revision::text
                              AND error->>'type' IN ('DataError','DataException','ValueError'))))
               ORDER BY concept.updated_at,concept.id LIMIT $1""",
            limit,
        )

    submitted = []
    for row in await _read(pool, candidates):
        try:
            job = await request_scoring(pool, row['id'])
            if job is not None:
                submitted.append(job)
        except (job_queue.QueueBusy, asyncpg.ObjectNotInPrerequisiteStateError, asyncpg.LockNotAvailableError):
            continue
    return submitted


async def dispatch_changes(pool, *, after_dataset=0, max_batches=16):
    started = time.monotonic()

    async def datasets(connection):
        return await connection.fetch(
            """SELECT id FROM datasets WHERE NOT deleting AND EXISTS(
                   SELECT 1 FROM concept_embedding_pending WHERE dataset_id=datasets.id)
               ORDER BY id<=$1,id LIMIT $2""",
            after_dataset,
            max_batches,
        )

    pending = deque(row['id'] for row in await _read(pool, datasets))
    for batch in range(max_batches):
        if not pending:
            break
        after_dataset = pending.popleft()
        try:
            async with job_queue._transaction(pool) as connection:
                delivered = await connection.fetchval(
                    'SELECT dispatch_concept_score_work($1,$2)',
                    DELIVERY_BATCH_SIZE,
                    after_dataset,
                )
            if delivered == DELIVERY_BATCH_SIZE:
                pending.append(after_dataset)
        except (job_queue.QueueBusy, asyncpg.ObjectNotInPrerequisiteStateError, asyncpg.LockNotAvailableError):
            pass
        if time.monotonic() - started >= 0.5:
            break
    return after_dataset


async def _measure(pool, job, size):
    concept_id = job['args']['concept_id']
    barrier = job['barrier']
    after = job['checkpoint'].get('after_id', 0)

    async def measure(connection):
        definition = await connection.fetchrow(
            f"SELECT *,{automatic_upkeep('concept')} AS automatic FROM embedding_concepts concept WHERE id=$1",
            concept_id,
        )
        if definition is not None and definition['dataset_id'] != job['dataset_id']:
            raise ValueError('Concept does not belong to the job dataset')
        if (
            definition is None
            or not definition['auto_rescore']
            or not definition['automatic']
            or definition['score_revision'] != barrier['score_revision']
        ):
            return None
        snapshot = await connection.fetchval('SELECT txid_current_snapshot()::text')
        if job['checkpoint'].get('count_phase'):
            total = await connection.fetchval(
                'SELECT count(*) FROM embedding_concept_scores WHERE concept_id=$1',
                concept_id,
            )
            return {'snapshot': snapshot, 'values': [], 'total': total, 'deltas': False, 'after': after}
        block = None
        if barrier['bootstrap']:
            targets = await connection.fetch(
                """SELECT sample_id FROM (
                     (SELECT id AS sample_id FROM samples WHERE dataset_id=$1 AND id>$2 AND id<=$3
                      ORDER BY id LIMIT $4)
                     UNION
                     (SELECT sample_id FROM embedding_concept_scores
                      WHERE concept_id=$5 AND sample_id>$2 AND sample_id<=$3 ORDER BY sample_id LIMIT $4)
                   ) candidates ORDER BY sample_id LIMIT $4""",
                job['dataset_id'],
                after,
                barrier['upper_id'],
                size,
                concept_id,
            )
        else:
            block = await connection.fetchval(
                """SELECT block_id FROM concept_score_pending
                   WHERE concept_id=$1 AND block_id>=($2::bigint+1)/1024
                     AND txid_visible_in_snapshot(transaction_id,$3::text::txid_snapshot)
                   ORDER BY block_id LIMIT 1""",
                concept_id,
                after,
                barrier['snapshot'],
            )
            targets = (
                []
                if block is None
                else await connection.fetch(
                    """SELECT id AS sample_id FROM samples
                   WHERE dataset_id=$1 AND id>$2 AND id<=$3
                     AND id>=$4::bigint*1024 AND id<($4::bigint+1)*1024
                   ORDER BY id LIMIT $5""",
                    job['dataset_id'],
                    after,
                    barrier['upper_id'],
                    block,
                    size,
                )
            )
        sample_ids = [row['sample_id'] for row in targets]
        values = await _score_values(connection, job['dataset_id'], definition, sample_ids)
        deltas = await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM concept_score_count_deltas WHERE concept_id=$1
               AND txid_visible_in_snapshot(transaction_id,$2::text::txid_snapshot))""",
            concept_id,
            barrier['snapshot'],
        )
        next_after = max(sample_ids) if sample_ids else after
        if block is not None and len(sample_ids) < size:
            next_after = (block + 1) * 1024 - 1
        return {
            'snapshot': snapshot,
            'values': values,
            'total': None,
            'deltas': deltas,
            'after': next_after,
            'block': block,
        }

    return await _read(pool, measure, seconds=45 if job['checkpoint'].get('count_phase') else 5)


async def _score_values(connection, dataset_id, definition, sample_ids, *, previous=False):
    """The shared scorer: one score per target sample; NULL when the sample or its vector is gone.
    `previous` also returns each sample's stored score, to count what a unit changes."""
    existing = (
        (
            ',existing.score AS previous',
            'LEFT JOIN embedding_concept_scores existing ON existing.concept_id=$7 '
            'AND existing.sample_id=target.sample_id',
        )
        if previous
        else ('', '')
    )
    return await connection.fetch(
        f"""SELECT target.sample_id,
                  concept_score_value($3,embedding.vector,$4::text::vector,$5) AS score{existing[0]}
           FROM unnest($1::int[]) target(sample_id)
           LEFT JOIN samples sample ON sample.id=target.sample_id AND sample.dataset_id=$2
           LEFT JOIN embeddings embedding ON embedding.sample_id=sample.id AND embedding.field_name=$6
           {existing[1]}
           ORDER BY target.sample_id""",
        sample_ids,
        dataset_id,
        definition['kind'],
        definition['weight'],
        definition['bias'],
        definition['embedding_field'],
        *([definition['id']] if previous else []),
    )


async def _write_scores(connection, concept_id, dataset_id, values):
    """Upsert changed scores (unchanged ones keep their tuples) and remove scores for missing vectors."""
    sample_ids = [row['sample_id'] for row in values]
    if not sample_ids:
        return
    await connection.execute(
        """INSERT INTO embedding_concept_scores(concept_id,sample_id,score)
           SELECT $1,measured.sample_id,measured.score
           FROM unnest($2::int[],$3::real[]) measured(sample_id,score)
           JOIN samples sample ON sample.id=measured.sample_id AND sample.dataset_id=$4
           WHERE measured.score IS NOT NULL
           ON CONFLICT(concept_id,sample_id) DO UPDATE SET score=EXCLUDED.score
           WHERE embedding_concept_scores.score IS DISTINCT FROM EXCLUDED.score""",
        concept_id,
        sample_ids,
        [row['score'] for row in values],
        dataset_id,
    )
    await connection.execute(
        'DELETE FROM embedding_concept_scores WHERE concept_id=$1 AND sample_id=ANY($2::int[])',
        concept_id,
        [row['sample_id'] for row in values if row['score'] is None],
    )


async def _lock_definition(connection, job):
    if not await connection.fetchval('SELECT pg_try_advisory_xact_lock_shared(20261016,$1)', job['dataset_id']):
        raise job_queue.QueueContended('Concept publication is waiting for a dataset operation')
    definition = await connection.fetchrow(
        f"""SELECT score_revision,auto_rescore,{automatic_upkeep('concept')} AS automatic
            FROM embedding_concepts concept WHERE id=$1 FOR NO KEY UPDATE OF concept NOWAIT""",
        job['args']['concept_id'],
    )
    if (
        definition is None
        or not definition['auto_rescore']
        or not definition['automatic']
        or definition['score_revision'] != job['barrier']['score_revision']
    ):
        raise job_queue.QueueContended('Concept definition changed during measurement')


def handler(pool):
    async def measure(job):
        if job['lane'] != 'maintenance':
            raise ValueError('Concept upkeep belongs to the maintenance lane')
        size = job['checkpoint'].get('batch_size', BATCH_SIZE)
        successes = job['checkpoint'].get('batch_successes', 0)
        started = time.monotonic()
        while True:
            try:
                measured = await _measure(pool, job, size)
                break
            except (asyncpg.QueryCanceledError, TimeoutError, asyncio.TimeoutError):
                if job['checkpoint'].get('count_phase'):
                    raise job_queue.QueueBusy('Concept final count exceeded its database budget') from None
                if size == 1 or time.monotonic() - started >= 20:
                    raise job_queue.QueueContended('Concept measurement exceeded its database budget') from None
                size = max(1, size // 2)
                successes = 0
        if measured is None:
            return StepResult(finished=True, message='Concept changed or automatic scoring was disabled')
        sample_ids = [row['sample_id'] for row in measured['values']]
        concept_id = job['args']['concept_id']
        successes += 1
        checkpoint = {
            **job['checkpoint'],
            'batch_size': min(BATCH_SIZE, size * 2) if successes >= 4 else size,
            'batch_successes': 0 if successes >= 4 else successes,
            'after_id': measured['after'],
            'examined': job['checkpoint'].get('examined', 0) + len(sample_ids),
            'units': job['checkpoint'].get('units', 0) + 1,
        }
        counting = job['checkpoint'].get('count_phase', False)
        if job['barrier']['bootstrap'] and not sample_ids:
            checkpoint['count_phase'] = True
        finished = counting or (
            not job['barrier']['bootstrap'] and measured.get('block') is None and not measured['deltas']
        )

        async def publish(connection):
            await connection.fetch(
                'SELECT id FROM samples WHERE id=ANY($1::int[]) ORDER BY id FOR KEY SHARE NOWAIT',
                sample_ids,
            )
            await _lock_definition(connection, job)
            await _write_scores(connection, concept_id, job['dataset_id'], measured['values'])
            await connection.execute(
                """DELETE FROM concept_score_pending WHERE concept_id=$1 AND block_id<($2::bigint+1)/1024
                   AND ($5 OR block_id=$6)
                   AND txid_visible_in_snapshot(transaction_id,$3::text::txid_snapshot)
                   AND txid_visible_in_snapshot(transaction_id,$4::text::txid_snapshot)""",
                concept_id,
                checkpoint['after_id'],
                measured['snapshot'],
                job['barrier']['snapshot'],
                job['barrier']['bootstrap'],
                measured.get('block'),
            )
            if counting:
                await connection.execute(
                    """DELETE FROM concept_score_pending WHERE concept_id=$1 AND block_id<=$2::bigint/1024
                       AND txid_visible_in_snapshot(transaction_id,$3::text::txid_snapshot)""",
                    concept_id,
                    job['barrier']['upper_id'],
                    job['barrier']['snapshot'],
                )
                await connection.execute(
                    'UPDATE embedding_concepts SET scored_count=$2,score_initialized=TRUE,updated_at=now() WHERE id=$1',
                    concept_id,
                    measured['total'],
                )
                await connection.execute(
                    """DELETE FROM concept_score_count_deltas WHERE concept_id=$1
                       AND txid_visible_in_snapshot(transaction_id,$2::text::txid_snapshot)""",
                    concept_id,
                    measured['snapshot'],
                )
            await connection.execute('SELECT settle_concept_score_count($1)', concept_id)

        return StepResult(
            checkpoint=checkpoint,
            done=checkpoint['examined'],
            message='Updating concept scores' if sample_ids else 'Concept scores updated',
            finished=finished,
            result={'concept_id': concept_id, 'examined': checkpoint['examined']} if finished else None,
            publish=publish,
        )

    return measure


# --- Explicit operations: create, rebuild, refine and rescore as user-lane jobs -------------------

EXPLICIT_KIND = 'concept_rescore'
EXPLICIT_BATCH_SIZE = 4096
# Manual "score new" walks embedding IDs; one unit examines at most this many IDs.
APPEND_SPAN = 65536
# Training reads (a random background, the current cut) scan the field once; bounded by one unit.
TRAIN_SECONDS = 150
EXPLICIT_POLICY = job_queue.JobPolicy(max_unit_seconds=180, max_failures=3, retry_seconds=2, max_retry_seconds=120)
MAX_CONCEPTS_PER_DATASET = 50
# How many samples to weakly-label from each side of the current cut when self-training.
SELF_TRAIN_SAMPLE = 300
# User-correction weight: a mark pulls the boundary much harder than a random background point.
REFINE_MARK_WEIGHT = 10.0
ACTIVE_OWNER = (
    "concept.score_job_id IS NOT NULL AND EXISTS(SELECT 1 FROM jobs owner "
    "WHERE owner.id=concept.score_job_id AND owner.state IN ('queued','running'))"
)


def vector_literal(values) -> str:
    """Format a float sequence as a pgvector literal."""
    return '[' + ','.join(repr(float(component)) for component in values) + ']'


def spec_dict(raw) -> dict:
    """Tolerate a legacy double-encoded (string) spec."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def refine_marks(spec: dict, keep: list, exclude: list):
    """Accumulate boundary marks: "exclude" adds positives, "keep" adds negatives; the newest mark wins
    and a kept sample also leaves the base positives."""
    added = list(dict.fromkeys(list(spec.get('pos_added') or []) + exclude))
    negatives = list(dict.fromkeys(list(spec.get('neg_ids') or []) + keep))
    added = [sample for sample in added if sample not in keep]
    negatives = [sample for sample in negatives if sample not in exclude]
    base = [sample for sample in (spec.get('seed_ids') or spec.get('pos_ids') or []) if sample not in negatives]
    return base, added, negatives


def fit_probe(pos, neg, pos_w=None, neg_w=None):
    """L2-regularized logistic regression (numpy GD) on positive vs negative embeddings ->
    (weight literal, bias). Per-sample weights let user corrections outweigh the background."""
    x = np.vstack([pos, neg])
    y = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))])
    sw = np.concatenate(
        [
            np.ones(len(pos)) if pos_w is None else np.asarray(pos_w, dtype=np.float64),
            np.ones(len(neg)) if neg_w is None else np.asarray(neg_w, dtype=np.float64),
        ],
    )
    sw_sum = float(sw.sum()) or 1.0
    w: np.ndarray = np.zeros(x.shape[1])
    b = 0.0
    for _ in range(800):
        z = np.clip(x @ w + b, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        err = (p - y) * sw
        w -= 0.5 * (x.T @ err / sw_sum + 1e-3 * w)
        b -= 0.5 * float(err.sum() / sw_sum)
    # Guard against a divergent fit producing non-finite weights (pgvector rejects NaN/Inf).
    w = np.nan_to_num(w, nan=0.0, posinf=0.0, neginf=0.0)
    return vector_literal(w.tolist()), float(np.nan_to_num(b, nan=0.0, posinf=0.0, neginf=0.0))


def _matrix(texts):
    if not texts:
        return np.empty((0, 0))
    return np.array([[float(x) for x in text.strip('[]').split(',')] for text in texts], dtype=np.float64)


async def _vector_texts(connection, dataset_id, field, ids):
    rows = await connection.fetch(
        """SELECT e.vector::text AS v FROM embeddings e JOIN samples s ON e.sample_id = s.id
           WHERE s.dataset_id = $1 AND e.field_name = $2 AND e.sample_id = ANY($3::int[])
             AND e.vector IS NOT NULL""",
        dataset_id,
        field,
        ids,
    )
    return [row['v'] for row in rows]


# Training samples cast to text only after the random LIMIT: casting below the sort converts every
# candidate vector of the field (measured 18.8 s vs 0.72 s for 200 of 1.2M 512-d vectors).
BACKGROUND_SQL = """SELECT sampled.v::text AS v FROM (
        SELECT e.vector AS v FROM embeddings e JOIN samples s ON e.sample_id = s.id
        WHERE s.dataset_id = $1 AND e.field_name = $2 AND e.vector IS NOT NULL
          AND NOT (e.sample_id = ANY($3::int[])) ORDER BY random() LIMIT $4) sampled"""
SCORED_SQL = """SELECT sampled.v::text AS v FROM (
        SELECT e.vector AS v FROM embedding_concept_scores cs
        JOIN samples s ON s.id = cs.sample_id
        JOIN embeddings e ON e.sample_id = cs.sample_id AND e.field_name = $2
        WHERE cs.concept_id = $5 AND s.dataset_id = $1 AND cs.score {op} $3
          AND NOT (cs.sample_id = ANY($6::int[])) AND e.vector IS NOT NULL
        ORDER BY random() LIMIT $4) sampled"""


async def _background_texts(connection, dataset_id, field, exclude_ids, limit):
    rows = await connection.fetch(BACKGROUND_SQL, dataset_id, field, exclude_ids, limit)
    return [row['v'] for row in rows]


async def _scored_texts(connection, dataset_id, field, concept_id, op, threshold, exclude_ids, limit):
    """A random sample of vectors whose current score satisfies `score <op> threshold` (op is internal)."""
    rows = await connection.fetch(
        SCORED_SQL.format(op=op),
        dataset_id,
        field,
        threshold,
        limit,
        concept_id,
        exclude_ids,
    )
    return [row['v'] for row in rows]


async def submit_explicit(pool, dataset_id, user_id, args):
    """Queue one explicit operation. Contention on the concept row retries briefly, then answers 409."""
    deadline = time.monotonic() + 2
    while True:
        try:
            return await _submit_explicit(pool, dataset_id, user_id, args)
        except job_queue.QueueBusy:
            if time.monotonic() >= deadline:
                raise HTTPException(409, 'Concept is busy; retry', headers={'Retry-After': '1'}) from None
            await asyncio.sleep(0.1)


async def _bounds(connection, dataset_id, concept_id):
    """The finite scan boundary: samples and scores present now. Later arrivals are pending
    automatic work or, for manual concepts, newer than the embedding watermark."""
    return dict(
        await connection.fetchrow(
            """SELECT GREATEST(
                   COALESCE((SELECT id FROM samples WHERE dataset_id=$1 ORDER BY id DESC LIMIT 1),0),
                   COALESCE((SELECT sample_id FROM embedding_concept_scores WHERE concept_id=$2
                             ORDER BY sample_id DESC LIMIT 1),0)) AS upper_id,
                   COALESCE((SELECT max(id) FROM embeddings),0) AS watermark""",
            dataset_id,
            concept_id,
        ),
    )


async def _submit_explicit(pool, dataset_id, user_id, args):
    async with job_queue._transaction(pool) as connection:
        concept_id = args.get('concept_id')
        if concept_id is None:
            job = await job_queue.enqueue_job(
                connection,
                EXPLICIT_KIND,
                dataset_id=dataset_id,
                created_by=user_id,
                args=args,
            )
            checkpoint = {'phase': 'train'}
            await connection.execute('UPDATE jobs SET checkpoint=$2 WHERE id=$1', job['id'], checkpoint)
            return {**job, 'checkpoint': checkpoint}
        # Serializes with automatic submission, which then sees the new owner.
        await connection.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
            f'concept-scoring:{concept_id}',
        )
        concept = await connection.fetchrow(
            f"""SELECT score_job_id,auto_rescore,last_scored_embedding_id,scores_ready,{ACTIVE_OWNER} AS owned
                FROM embedding_concepts concept WHERE id=$1 AND dataset_id=$2 FOR NO KEY UPDATE OF concept""",
            concept_id,
            dataset_id,
        )
        if concept is None:
            raise HTTPException(404, 'Concept not found')
        if concept['owned']:
            raise HTTPException(
                409,
                f"Concept is already being rescored (job {concept['score_job_id']}); "
                'wait for it to finish or cancel it in Jobs',
            )
        if args['operation'] == 'rescore' and not args.get('full', True) and not concept['scores_ready']:
            # Scoring new or pending work would leave most scores from the previous definition.
            raise HTTPException(
                409,
                "The concept's scores do not match its definition yet; resume its scoring job "
                '(Resume on the concept, or POST /api/jobs/{id}/resume) or run a full rescore',
            )
        defining = args['operation'] != 'rescore'
        barrier = {} if defining else await _bounds(connection, dataset_id, concept_id)
        if not defining:
            barrier['since'] = concept['last_scored_embedding_id']
        job = await job_queue.enqueue_job(
            connection,
            EXPLICIT_KIND,
            dataset_id=dataset_id,
            created_by=user_id,
            args=args,
            barrier=barrier,
        )
        # The revision bump fences every older publication, automatic or explicit.
        revision = await connection.fetchval(
            'UPDATE embedding_concepts SET score_job_id=$2,score_revision=score_revision+1 WHERE id=$1 '
            'RETURNING score_revision',
            concept_id,
            job['id'],
        )
        checkpoint = {'phase': 'train' if defining else 'score', 'concept_id': concept_id, 'revision': revision}
        await connection.execute('UPDATE jobs SET checkpoint=$2 WHERE id=$1', job['id'], checkpoint)
        return {**job, 'checkpoint': checkpoint}


async def resume_explicit(pool, job_id):
    """Requeue the same stopped job and checkpoint, if it still owns its concept."""
    async with job_queue._transaction(pool) as connection:
        job = await connection.fetchrow(
            f'SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE} WHERE jobs.id=$1 FOR UPDATE OF jobs NOWAIT',
            job_id,
        )
        if job is None or job['kind'] != EXPLICIT_KIND:
            raise HTTPException(404, 'Job not found')
        if job['state'] in ('queued', 'running'):
            if job['cancel_requested']:
                raise HTTPException(409, 'Wait for cancellation to finish before resuming')
            return {'job_id': str(job['id']), 'state': job['state']}
        if job['state'] != 'failed' and job['state'] != 'cancelled':
            raise HTTPException(409, 'Only a stopped concept job can be resumed')
        checkpoint = dict(job['checkpoint'] or {})
        concept_id = checkpoint.get('concept_id')
        if concept_id is not None:
            await connection.execute(
                'SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
                f'concept-scoring:{concept_id}',
            )
            owner = await connection.fetchval(
                'SELECT score_job_id FROM embedding_concepts WHERE id=$1 FOR NO KEY UPDATE',
                concept_id,
            )
            if owner != job['id']:
                raise HTTPException(409, 'The concept was deleted or a newer operation owns it; nothing to resume')
            checkpoint['revision'] = await connection.fetchval(
                'UPDATE embedding_concepts SET score_revision=score_revision+1 WHERE id=$1 RETURNING score_revision',
                concept_id,
            )
        # As an unstarted job it queues behind any other started job of the dataset's user lane
        # (one owner per lane) instead of colliding with it; its concept stays fenced meanwhile.
        await connection.execute(
            """UPDATE jobs SET state='queued',error=NULL,failures=0,contentions=0,checkpoint=$2,
               lease_token=NULL,lease_expires_at=NULL,finished_at=NULL,started_at=NULL,
               available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=$1""",
            job['id'],
            checkpoint,
        )
        await connection.execute('UPDATE job_controls SET cancel_requested=FALSE WHERE job_id=$1', job['id'])
        return {'job_id': str(job['id']), 'state': 'queued'}


async def _owned(connection, job):
    """Measurement-side ownership check: a deleted or superseded concept ends the job visibly."""
    concept_id = job['checkpoint']['concept_id']
    definition = await connection.fetchrow('SELECT * FROM embedding_concepts WHERE id=$1', concept_id)
    if definition is None or definition['dataset_id'] != job['dataset_id']:
        raise JobConflict('The concept was deleted while it was being scored')
    if definition['score_job_id'] != job['id'] or definition['score_revision'] != job['checkpoint']['revision']:
        raise JobConflict('The concept was changed by another operation; its newer job owns it')
    return definition


async def _lock_owned(connection, job, revision):
    """Publication fence: this job still owns the concept at the revision it measured."""
    if not await connection.fetchval('SELECT pg_try_advisory_xact_lock_shared(20261016,$1)', job['dataset_id']):
        raise job_queue.QueueContended('Concept publication is waiting for a dataset operation')
    current = await connection.fetchrow(
        'SELECT score_revision,score_job_id FROM embedding_concepts WHERE id=$1 FOR NO KEY UPDATE NOWAIT',
        job['checkpoint']['concept_id'],
    )
    if current is None or current['score_job_id'] != job['id'] or current['score_revision'] != revision:
        raise job_queue.QueueContended('Concept ownership changed during measurement')


async def _train(pool, job):
    args = job['args']
    operation = args['operation']

    new_id = None
    if operation == 'create':
        # Allocated before the read-only measurement; a retried unit simply leaves a gap.
        async with pool.acquire(timeout=1) as connection:
            new_id = await connection.fetchval(
                "SELECT nextval(pg_get_serial_sequence('embedding_concepts','id'))",
                timeout=5,
            )

    async def read(connection):
        if operation == 'create':
            definition = None
            concept_id = new_id
            field = args['embedding_field']
        else:
            definition = await _owned(connection, job)
            concept_id = definition['id']
            field = definition['embedding_field']
        spec: dict = spec_dict(definition['spec']) if definition is not None else {}
        inputs: dict = {'concept_id': concept_id, 'spec': spec}
        training = args.get('train') or {}
        if training.get('probe'):
            positives = training['probe']['pos_ids']
            inputs['pos'] = await _vector_texts(connection, job['dataset_id'], field, positives)
            negatives = training['probe'].get('neg_ids')
            inputs['neg'] = (
                await _vector_texts(connection, job['dataset_id'], field, negatives)
                if negatives
                else await _background_texts(
                    connection,
                    job['dataset_id'],
                    field,
                    positives,
                    max(len(positives) * 5, 200),
                )
            )
        elif training.get('refine'):
            refine = training['refine']
            base, added, negatives = refine_marks(spec, refine['keep_ids'], refine['exclude_ids'])
            inputs.update(base=base, added=added, negatives=negatives)
            inputs['base_v'] = await _vector_texts(connection, job['dataset_id'], field, base) if base else []
            inputs['added_v'] = await _vector_texts(connection, job['dataset_id'], field, added) if added else []
            inputs['neg_v'] = await _vector_texts(connection, job['dataset_id'], field, negatives) if negatives else []
            threshold = refine.get('threshold')
            # The cut level to preserve, measured on the current scores before the refit changes them.
            if threshold is not None and definition is not None and definition['scores_ready']:
                inputs['old_hidden'] = await connection.fetchval(
                    'SELECT count(*) FROM embedding_concept_scores WHERE concept_id = $1 AND score > $2',
                    concept_id,
                    threshold,
                )
            exclude = list(dict.fromkeys(base + added)) + negatives
            if refine.get('self_train') and threshold is not None:
                for name, op in (('hidden', '>'), ('kept', '<=')):
                    inputs[name] = await _scored_texts(
                        connection,
                        job['dataset_id'],
                        field,
                        concept_id,
                        op,
                        threshold,
                        exclude,
                        SELF_TRAIN_SAMPLE,
                    )
            else:
                inputs['background'] = await _background_texts(
                    connection,
                    job['dataset_id'],
                    field,
                    exclude,
                    max(len(base) + len(added), 1) * 5 + 200,
                )
        return inputs

    inputs = await _read(pool, read, seconds=TRAIN_SECONDS)
    concept_id = inputs['concept_id']
    spec = dict(args.get('spec') or {})
    kind, weight, bias = args.get('kind'), args.get('weight'), args.get('bias')
    training = args.get('train') or {}
    if training.get('probe'):
        pos, neg = await asyncio.to_thread(lambda: (_matrix(inputs['pos']), _matrix(inputs['neg'])))
        if pos.size == 0:
            raise JobInputError(f"No '{args['embedding_field']}' embeddings found for the positive samples")
        if neg.size == 0:
            raise JobInputError('No negative examples available to train the probe')
        weight, bias = await asyncio.to_thread(fit_probe, pos, neg)
        spec.setdefault('pos_ids', training['probe']['pos_ids'])
        spec.setdefault('negatives', training['probe'].get('neg_ids') or 'random_background')
    elif training.get('refine'):

        def refit():
            positives = [(_matrix(inputs['base_v']), 1.0), (_matrix(inputs['added_v']), REFINE_MARK_WEIGHT)]
            negatives = [(_matrix(inputs['neg_v']), REFINE_MARK_WEIGHT)]
            if 'hidden' in inputs:
                positives.append((_matrix(inputs['hidden']), 1.0))
                negatives.append((_matrix(inputs['kept']), 1.0))
            else:
                negatives.append((_matrix(inputs['background']), 1.0))
            positives = [(vectors, w) for vectors, w in positives if vectors.size]
            negatives = [(vectors, w) for vectors, w in negatives if vectors.size]
            if not positives:
                raise JobInputError('No positive examples with embeddings to refit')
            if not negatives:
                raise JobInputError('No negatives available to refit the probe')
            return fit_probe(
                np.vstack([vectors for vectors, _ in positives]),
                np.vstack([vectors for vectors, _ in negatives]),
                np.concatenate([np.full(len(vectors), w) for vectors, w in positives]),
                np.concatenate([np.full(len(vectors), w) for vectors, w in negatives]),
            )

        weight, bias = await asyncio.to_thread(refit)
        kind = 'probe'
        spec = {**inputs['spec'], 'pos_added': inputs['added'], 'neg_ids': inputs['negatives']}
    create = args['operation'] == 'create'
    revision = 1 if create else job['checkpoint']['revision'] + 1
    checkpoint = {
        'phase': 'score',
        'concept_id': concept_id,
        'revision': revision,
        'old_hidden': inputs.get('old_hidden'),
        'after_id': 0,
        'examined': 0,
        'changed': 0,
    }

    async def publish(connection):
        if not await connection.fetchval('SELECT pg_try_advisory_xact_lock_shared(20261016,$1)', job['dataset_id']):
            raise job_queue.QueueContended('Concept publication is waiting for a dataset operation')
        if create:
            count = await connection.fetchval(
                'SELECT count(*) FROM embedding_concepts WHERE dataset_id=$1',
                job['dataset_id'],
            )
            if count >= MAX_CONCEPTS_PER_DATASET:
                raise JobInputError(
                    f'Concept limit reached ({MAX_CONCEPTS_PER_DATASET}); delete unused concepts first',
                )
            try:
                await connection.execute(
                    """INSERT INTO embedding_concepts
                       (id,dataset_id,name,embedding_field,kind,spec,weight,bias,threshold,created_by_user_id,
                        score_job_id,scores_ready,score_revision)
                       VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11,FALSE,$12)""",
                    concept_id,
                    job['dataset_id'],
                    args['name'],
                    args['embedding_field'],
                    kind,
                    spec,
                    weight,
                    bias or 0.0,
                    args['threshold'] if args.get('threshold') is not None else 0.5,
                    job['created_by'],
                    job['id'],
                    revision,
                )
            except asyncpg.UniqueViolationError:
                raise JobConflict(f"A concept named {args['name']!r} already exists") from None
        else:
            await _lock_owned(connection, job, job['checkpoint']['revision'])
            # Stale scores of the old definition stay until their bounded units replace them;
            # scores_ready=FALSE keeps filters from mixing the two meanings meanwhile.
            await connection.execute(
                """UPDATE embedding_concepts SET kind=COALESCE($2,kind),weight=$3,bias=COALESCE($4,bias),
                   spec=CASE WHEN $5 THEN $6::jsonb ELSE spec || $6::jsonb END,
                   threshold=COALESCE($7,threshold),scores_ready=FALSE,score_revision=$8,updated_at=now()
                   WHERE id=$1""",
                concept_id,
                kind,
                weight,
                bias,
                bool(training.get('refine')),
                spec,
                args.get('threshold') if not training.get('refine') else None,
                revision,
            )
        # The boundary is read in the transaction that publishes the definition: anything committed
        # before it is scanned, anything after reaches this concept as pending automatic work.
        bounds = await _bounds(connection, job['dataset_id'], concept_id)
        bounds['since'] = 0
        await connection.execute('UPDATE jobs SET barrier=$2 WHERE id=$1', job['id'], bounds)

    return StepResult(checkpoint=checkpoint, done=0, message='Concept definition saved; scoring', publish=publish)


async def _measure_explicit(pool, job, size):
    args, checkpoint, barrier = job['args'], job['checkpoint'], job['barrier']
    after = checkpoint.get('after_id', 0)
    mode = args.get('mode', 'full')

    async def measure(connection):
        definition = await _owned(connection, job)
        snapshot = await connection.fetchval('SELECT txid_current_snapshot()::text')
        block = None
        next_after = after
        if mode == 'full':
            targets = await connection.fetch(
                """SELECT sample_id FROM (
                     (SELECT id AS sample_id FROM samples WHERE dataset_id=$1 AND id>$2 AND id<=$3
                      ORDER BY id LIMIT $4)
                     UNION
                     (SELECT sample_id FROM embedding_concept_scores
                      WHERE concept_id=$5 AND sample_id>$2 AND sample_id<=$3 ORDER BY sample_id LIMIT $4)
                   ) candidates ORDER BY sample_id LIMIT $4""",
                job['dataset_id'],
                after,
                barrier['upper_id'],
                size,
                definition['id'],
            )
            sample_ids = [row['sample_id'] for row in targets]
            done = not sample_ids
            next_after = max(sample_ids) if sample_ids else after
        elif mode == 'pending':
            first, last = (after + 1) // 1024, barrier['upper_id'] // 1024
            block = await connection.fetchval(
                """SELECT min(block_id) FROM (
                     (SELECT block_id FROM concept_score_pending WHERE concept_id=$1
                      AND block_id>=$2 AND block_id<=$3 ORDER BY block_id LIMIT 1)
                     UNION ALL
                     (SELECT block_id FROM concept_embedding_pending WHERE dataset_id=$4 AND field_name=$5
                      AND block_id>=$2 AND block_id<=$3 ORDER BY block_id LIMIT 1)) blocks""",
                definition['id'],
                first,
                last,
                job['dataset_id'],
                definition['embedding_field'],
            )
            targets = (
                []
                if block is None
                else await connection.fetch(
                    """SELECT id AS sample_id FROM samples WHERE dataset_id=$1 AND id>$2 AND id<=$3
                       AND id>=$4::bigint*1024 AND id<($4::bigint+1)*1024 ORDER BY id LIMIT $5""",
                    job['dataset_id'],
                    after,
                    barrier['upper_id'],
                    block,
                    size,
                )
            )
            sample_ids = [row['sample_id'] for row in targets]
            done = block is None
            if block is not None:
                next_after = max(sample_ids) if len(sample_ids) == size else (block + 1) * 1024 - 1
        else:
            # Manual "score new": embeddings appended after the last explicit scoring.
            start = max(after, barrier['since'])
            stop = min(barrier['watermark'], start + APPEND_SPAN)
            rows = await connection.fetch(
                """SELECT embedding.id,embedding.sample_id FROM embeddings embedding
                   JOIN samples sample ON sample.id=embedding.sample_id
                   WHERE embedding.id>$1 AND embedding.id<=$2 AND embedding.field_name=$3 AND sample.dataset_id=$4
                   ORDER BY embedding.id LIMIT $5""",
                start,
                stop,
                definition['embedding_field'],
                job['dataset_id'],
                size,
            )
            sample_ids = sorted({row['sample_id'] for row in rows})
            next_after = rows[-1]['id'] if len(rows) == size else stop
            done = start >= barrier['watermark']
        values = await _score_values(connection, job['dataset_id'], definition, sample_ids, previous=True)
        return {'snapshot': snapshot, 'values': values, 'after': next_after, 'block': block, 'done': done}

    return await _read(pool, measure)


async def _score(pool, job):
    checkpoint = dict(job['checkpoint'])
    size = checkpoint.get('batch_size', EXPLICIT_BATCH_SIZE)
    successes = checkpoint.get('batch_successes', 0)
    started = time.monotonic()
    while True:
        try:
            measured = await _measure_explicit(pool, job, size)
            break
        except (asyncpg.QueryCanceledError, TimeoutError, asyncio.TimeoutError):
            if size == 1 or time.monotonic() - started >= 20:
                raise job_queue.QueueContended('Concept measurement exceeded its database budget') from None
            size = max(1, size // 2)
            successes = 0
    successes += 1
    values = measured['values']
    # Only this job writes the concept's scores while it owns it (sample deletes only remove rows),
    # so an unchanged score needs no write, no lock and no WAL.
    writes = [
        row
        for row in values
        if (row['score'] is None) != (row['previous'] is None)
        or (row['score'] is not None and row['score'] != row['previous'])
    ]
    mode = job['args'].get('mode', 'full')
    checkpoint.update(
        batch_size=min(EXPLICIT_BATCH_SIZE, size * 2) if successes >= 4 else size,
        batch_successes=0 if successes >= 4 else successes,
        after_id=measured['after'],
        examined=checkpoint.get('examined', 0) + len(values),
        changed=checkpoint.get('changed', 0) + len(writes),
    )
    if measured['done']:
        checkpoint['phase'] = 'count' if mode == 'full' else 'finish'
    concept_id, revision = checkpoint['concept_id'], checkpoint['revision']
    sample_ids = [row['sample_id'] for row in writes]

    async def publish(connection):
        await connection.fetch(
            'SELECT id FROM samples WHERE id=ANY($1::int[]) ORDER BY id FOR KEY SHARE NOWAIT',
            sample_ids,
        )
        await _lock_owned(connection, job, revision)
        await _write_scores(connection, concept_id, job['dataset_id'], writes)
        if measured['block'] is not None and (measured['after'] + 1) // 1024 > measured['block']:
            # A finished block only; source rows still reach the field's other concepts.
            await connection.execute(
                """DELETE FROM concept_score_pending WHERE concept_id=$1 AND block_id=$2
                   AND txid_visible_in_snapshot(transaction_id,$3::text::txid_snapshot)""",
                concept_id,
                measured['block'],
                measured['snapshot'],
            )
        await connection.execute('SELECT settle_concept_score_count($1)', concept_id)

    return StepResult(
        checkpoint=checkpoint,
        done=checkpoint['examined'],
        message='Scoring concept' if not measured['done'] else 'Concept scored',
        publish=publish,
    )


async def _count(pool, job):
    checkpoint = dict(job['checkpoint'])
    concept_id, revision = checkpoint['concept_id'], checkpoint['revision']

    async def measure(connection):
        await _owned(connection, job)
        return (
            await connection.fetchval('SELECT txid_current_snapshot()::text'),
            await connection.fetchval('SELECT count(*) FROM embedding_concept_scores WHERE concept_id=$1', concept_id),
        )

    try:
        snapshot, total = await _read(pool, measure, seconds=45)
    except (asyncpg.QueryCanceledError, TimeoutError, asyncio.TimeoutError):
        raise job_queue.QueueBusy('Concept count exceeded its database budget') from None
    checkpoint.update(phase='finish', total=total)

    async def publish(connection):
        await _lock_owned(connection, job, revision)
        await connection.execute('UPDATE embedding_concepts SET scored_count=$2 WHERE id=$1', concept_id, total)
        await connection.execute(
            """DELETE FROM concept_score_count_deltas WHERE concept_id=$1
               AND txid_visible_in_snapshot(transaction_id,$2::text::txid_snapshot)""",
            concept_id,
            snapshot,
        )

    return StepResult(checkpoint=checkpoint, message='Counting concept scores', publish=publish)


async def _finish(pool, job):
    args, checkpoint, barrier = job['args'], dict(job['checkpoint']), job['barrier']
    concept_id, revision = checkpoint['concept_id'], checkpoint['revision']
    operation = args['operation']

    async def measure(connection):
        definition = await _owned(connection, job)
        band = await connection.fetchrow(
            """SELECT min(score) AS lo, max(score) AS hi FROM embedding_concept_scores
               WHERE concept_id = $1 AND score <> 'NaN'::real""",
            concept_id,
        )
        threshold = None
        calibrate = (operation == 'create' and args.get('threshold') is None) or operation == 'refine'
        if operation == 'refine' and checkpoint.get('old_hidden') is not None:
            # Preserve the cut level: the threshold hiding about as many samples as before the refit.
            kept = await connection.fetchval(
                """SELECT score FROM embedding_concept_scores WHERE concept_id = $1
                   ORDER BY score DESC OFFSET $2 LIMIT 1""",
                concept_id,
                int(checkpoint['old_hidden']),
            )
            if kept is not None:
                threshold, calibrate = float(kept), False
        if calibrate and band['lo'] is not None and band['hi'] is not None and band['hi'] > band['lo']:
            # Default into the actual score band (exclude a small top slice), not 0.5.
            threshold = round(band['lo'] + 0.85 * (band['hi'] - band['lo']), 4)
        final = definition['threshold'] if threshold is None else threshold
        matched = await connection.fetchval(
            'SELECT count(*) FROM embedding_concept_scores WHERE concept_id = $1 AND score > $2',
            concept_id,
            final,
        )
        return definition, threshold, final, matched or 0, band, await connection.fetchval('SELECT clock_timestamp()')

    definition, threshold, final, matched, band, finished_at = await _read(pool, measure, seconds=45)
    full = args.get('mode', 'full') == 'full'
    if operation == 'rescore':
        scored = checkpoint.get('total', definition['scored_count']) if full else checkpoint.get('changed', 0)
        result = {'concept_id': concept_id, 'scored': scored}
    else:
        result = jsonable_encoder(
            {
                'id': definition['id'],
                'dataset_id': definition['dataset_id'],
                'name': definition['name'],
                'embedding_field': definition['embedding_field'],
                'kind': definition['kind'],
                'spec': spec_dict(definition['spec']),
                'bias': definition['bias'],
                'threshold': final,
                'scored_count': definition['scored_count'],
                'created_at': definition['created_at'],
                'updated_at': finished_at,
                'matched': matched,
                'score_min': band['lo'],
                'score_max': band['hi'],
            },
        )

    async def publish(connection):
        await _lock_owned(connection, job, revision)
        # Only a full pass (every definition change is one) makes the scores match the definition;
        # a pending/appended pass leaves a not-ready concept not ready.
        await connection.execute(
            """UPDATE embedding_concepts SET threshold=COALESCE($2,threshold),scores_ready=scores_ready OR $3,
               score_job_id=NULL,score_initialized=score_initialized OR $3,
               last_scored_embedding_id=GREATEST(last_scored_embedding_id,$4),updated_at=$5 WHERE id=$1""",
            concept_id,
            threshold,
            full,
            barrier['watermark'],
            finished_at,
        )

    return StepResult(
        checkpoint={**checkpoint, 'phase': 'done'},
        done=checkpoint.get('examined', 0),
        message='Concept scored',
        result=result,
        finished=True,
        publish=publish,
    )


def explicit_handler(pool):
    async def measure(job):
        if job['lane'] != 'user':
            raise ValueError('Explicit concept operations belong to the user lane')
        phase = job['checkpoint'].get('phase', 'train')
        if phase == 'train':
            return await _train(pool, job)
        if phase == 'score':
            return await _score(pool, job)
        if phase == 'count':
            return await _count(pool, job)
        return await _finish(pool, job)

    return measure
