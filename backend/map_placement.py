"""Placing new samples on re-fitted maps (kind ``map_place``), from their nearest already-mapped neighbours.

A re-fitted map keeps no model. Each sample waiting in the projection queue of a re-fitted map goes where the
images most like it already are: the vector index finds the ``NEIGHBOURS`` mapped samples of the dataset with
the closest vectors (cosine). On the map they usually sit together, but can be split between groups, so the
sample goes to the densest group: the neighbour whose similarity-weighted map distances to the others are
smallest, then the similarity-weighted mean of the neighbours within twice their median distance from it
(``place``). Measured against where UMAP put the same images, on Open Images (42k) and a 3.7M-image dataset,
9 in 10 land within 1.0-1.3% of the map's width of UMAP's spot; a plain mean of the neighbours is two to four
times worse there (docs/architecture/JOBS.md).

A scheduler (``schedule_placements``) queues one job per map with samples waiting, up to the newest one
waiting then (its barrier), so a job ends even while ingest continues and the dataset's other maintenance
work gets its turn. Each step places one batch: the lookups run in parallel, one batch of samples per
connection, and the coordinates are written under the fence every map writer takes (``check_map_fence``).
Nothing is placed while the map is being re-fitted, and a batch looked up on a map that was replaced
meanwhile is dropped: its samples stay queued, for the new map.
"""

import asyncio
import logging

import asyncpg
import numpy as np

from . import job_queue
from .coordinates import SAVE_PROJECTED_COORDINATES, MapModelChanged, MapRefitActive, check_map_fence
from .diskspace import COORDINATE_ROW_BYTES, require_free_space
from .job_runner import StepResult

KIND = 'map_place'
POLICY = job_queue.JobPolicy(max_unit_seconds=300, max_failures=3, retry_seconds=5, max_retry_seconds=300)
# As UMAP's n_neighbors, which the placement was measured with.
NEIGHBOURS = 15
# Samples per lookup statement: each takes one index search, about 5 ms for 512-d vectors and 55 ms for
# 1024-d ones on 3.7M (measured 2026-11-06), so a batch stays within seconds.
LOOKUP_BATCH = 64
# The vector index's search, as similarity search sets it: candidates kept per search, and how far an
# iterative scan may walk the graph to find enough of this dataset's mapped samples.
EF_SEARCH = 100
MAX_SCAN_TUPLES = 200_000
logger = logging.getLogger(__name__)

_LOOKUP = """
SELECT q.sample_id, n.distance, n.x, n.y
FROM embeddings q
CROSS JOIN LATERAL (
    SELECT e.vector::vector({dimension}) <=> q.vector::vector({dimension}) AS distance, c.x, c.y
    FROM embeddings e
    JOIN embedding_coordinates c ON c.sample_id = e.sample_id AND c.field_name = e.field_name
    JOIN samples s ON s.id = e.sample_id
    WHERE e.field_name = $2 AND s.dataset_id = $3 AND e.sample_id <> q.sample_id
      AND c.x IS NOT NULL AND c.y IS NOT NULL
    ORDER BY {order}
    LIMIT {neighbours}
) n
WHERE q.field_name = $2 AND q.sample_id = ANY($1::int[])
"""
# The index serves an ORDER BY on the distance alone, one search per sample (LATERAL). The exact form orders
# by an expression the index can't serve: the dataset's own mapped samples, scanned and sorted.
INDEXED = 'e.vector::vector({dimension}) <=> q.vector::vector({dimension})'
EXACT = '(e.vector::vector({dimension}) <=> q.vector::vector({dimension})) + 0'


def lookup_sql(dimension: int, *, exact: bool = False) -> str:
    order = (EXACT if exact else INDEXED).format(dimension=int(dimension))
    return _LOOKUP.format(dimension=int(dimension), order=order, neighbours=NEIGHBOURS)


def place(positions: np.ndarray, distances: np.ndarray) -> np.ndarray:
    """Where a sample goes, from its neighbours' map positions (n, 2) and cosine distances (n,)."""
    weights = 1.0 / np.maximum(distances, 1e-6)
    gaps = np.linalg.norm(positions[:, None, :] - positions[None, :, :], axis=-1)
    centre = positions[np.argmin((gaps * weights[None, :]).sum(axis=1))]
    away = np.linalg.norm(positions - centre, axis=1)
    close = away <= 2 * np.median(away)  # always holds for the centre itself
    return (positions[close] * weights[close, None]).sum(axis=0) / weights[close].sum()


async def _lookup(pool, sample_ids, field, dataset_id, dimension, *, exact=False):
    """{sample_id: [(distance, x, y), ...]} for the samples, nearest first, from one connection."""
    async with pool.acquire(timeout=30) as connection, connection.transaction(readonly=True):
        await connection.execute("SELECT '[0]'::vector(1)")  # registers pgvector's settings on a new session
        await connection.execute(
            f'SET LOCAL hnsw.ef_search = {EF_SEARCH}; SET LOCAL hnsw.iterative_scan = relaxed_order; '
            f'SET LOCAL hnsw.max_scan_tuples = {MAX_SCAN_TUPLES}; '
            # The field's index is partial (WHERE field_name = ...): only a plan made for the actual field name
            # can use it, never a cached generic one.
            'SET LOCAL plan_cache_mode = force_custom_plan',
        )
        rows = await connection.fetch(
            lookup_sql(dimension, exact=exact),
            list(sample_ids),
            field,
            dataset_id,
            timeout=POLICY.max_unit_seconds,
        )
    found: dict = {}
    for row in rows:
        found.setdefault(row['sample_id'], []).append((row['distance'], row['x'], row['y']))
    return found


async def _neighbours(pool, sample_ids, field, dataset_id, dimension, lookups):
    """Each sample's neighbours, looked up in parallel batches; a sample the index came up short for (a
    dataset that is a small part of a shared field's index) is looked up exactly."""
    batches = [sample_ids[start : start + LOOKUP_BATCH] for start in range(0, len(sample_ids), LOOKUP_BATCH)]
    found: dict = {}
    for start in range(0, len(batches), lookups):
        parts = await asyncio.gather(
            *(_lookup(pool, batch, field, dataset_id, dimension) for batch in batches[start : start + lookups]),
        )
        for part in parts:
            found.update(part)
    short = [sample_id for sample_id in sample_ids if len(found.get(sample_id, ())) < NEIGHBOURS]
    if short:
        found.update(await _lookup(pool, short, field, dataset_id, dimension, exact=True))
    return found


async def _measure(pool, job, lookups):
    """The next batch of the job's samples and where each goes, or None when the map can't take any now."""
    dataset_id, field = job['dataset_id'], job['args']['field']
    checkpoint = job['checkpoint'] or {}
    after, upper = checkpoint.get('after', 0), job['barrier']['upper']
    async with pool.acquire(timeout=30) as connection:
        state = await connection.fetchrow(
            """SELECT m.model_id::text AS model_id, m.dimension,
                      (j.state IN ('queued','running')) IS TRUE AS active
               FROM embedding_map_state m LEFT JOIN jobs j ON j.id = m.active_job_id
               WHERE m.dataset_id = $1 AND m.field_name = $2""",
            dataset_id,
            field,
        )
        if state is None or state['model_id'] is None or state['active']:
            return None
        waiting = await connection.fetch(
            """SELECT p.sample_id, e.xmin::text AS vector_version
               FROM embedding_projection_pending p
               JOIN embeddings e ON e.sample_id = p.sample_id AND e.field_name = p.field_name
               WHERE p.dataset_id = $1 AND p.field_name = $2 AND p.sample_id > $3 AND p.sample_id <= $4
               ORDER BY p.sample_id LIMIT $5""",
            dataset_id,
            field,
            after,
            upper,
            LOOKUP_BATCH * lookups,
        )
        total = checkpoint.get('total')
        if total is None:
            total = await connection.fetchval(
                """SELECT count(*) FROM embedding_projection_pending
                   WHERE dataset_id = $1 AND field_name = $2 AND sample_id > $3 AND sample_id <= $4""",
                dataset_id,
                field,
                after,
                upper,
            )
        if waiting:
            await require_free_space(connection, len(waiting) * COORDINATE_ROW_BYTES, f'placing {len(waiting)} points')
    sample_ids = [row['sample_id'] for row in waiting]
    found = await _neighbours(pool, sample_ids, field, dataset_id, state['dimension'], lookups) if waiting else {}
    placed, versions, unplaced = [], [], 0
    for row in waiting:
        neighbours = np.array(found.get(row['sample_id'], ()), dtype=np.float64).reshape(-1, 3)
        neighbours = neighbours[np.isfinite(neighbours).all(axis=1)]  # a zero vector has no cosine distance
        if not len(neighbours):
            unplaced += 1  # stays queued, for a later job (or a re-fit) to place
            continue
        x, y = place(neighbours[:, 1:], neighbours[:, 0])
        placed.append((row['sample_id'], float(x), float(y)))
        versions.append(row['vector_version'])
    return {
        'model_id': state['model_id'],
        'last': sample_ids[-1] if sample_ids else None,
        'total': total,
        'placed': placed,
        'versions': versions,
        'unplaced': unplaced,
    }


def handler(pool, lookups: int = 1):
    """`lookups`: how many lookup statements run at once (connections this job may hold)."""

    async def run(job):
        dataset_id, field = job['dataset_id'], job['args']['field']
        checkpoint = dict(job['checkpoint'] or {})
        done = {'placed': checkpoint.get('placed', 0), 'without_neighbours': checkpoint.get('without_neighbours', 0)}
        batch = await _measure(pool, job, max(1, lookups))
        if batch is None or batch['last'] is None:
            # Done, or the map is being re-fitted (its samples wait for the new map) or is no longer re-fitted.
            return StepResult(finished=True, result=done, message=f'Placed {done["placed"]} new points')
        following = {
            **checkpoint,
            'after': batch['last'],
            'total': batch['total'],
            'without_neighbours': done['without_neighbours'] + batch['unplaced'],
            'placed': done['placed'],
        }

        async def publish(connection):
            if not batch['placed']:
                return
            try:
                await check_map_fence(connection, dataset_id, field, batch['model_id'])
            except (MapRefitActive, MapModelChanged) as error:
                # Looked up on a map that is being re-fitted or was replaced: these samples stay queued.
                logger.info('Map placement on dataset %s, %s: %s', dataset_id, field, error)
                return
            ids, xs, ys = zip(*batch['placed'])
            status = await connection.execute(
                SAVE_PROJECTED_COORDINATES,
                list(ids),
                list(xs),
                list(ys),
                dataset_id,
                field,
                batch['versions'],
            )
            # Runs before the checkpoint is saved, in its transaction: count only what was written (a sample
            # whose vector changed since the lookup is skipped, and queued again).
            following['placed'] += int(status.split()[-1])

        return StepResult(
            checkpoint=following,
            done=checkpoint.get('placed', 0) + len(batch['placed']),
            total=batch['total'],
            message=f'Placing new points on the map of {field}',
            publish=publish,
        )

    return run


async def schedule_placements(pool, limit: int = 10):
    """Queue a placement for each re-fitted map with samples waiting, up to the newest one waiting now. Skipped:
    a map being re-fitted (its samples wait for the new map), one whose placement is queued or running, and,
    for 5 minutes, one whose last placement failed or found no neighbours for anything (an empty map)."""
    async with job_queue._transaction(pool) as connection:
        maps = await connection.fetch(
            """SELECT m.dataset_id, m.field_name,
                      (SELECT max(p.sample_id) FROM embedding_projection_pending p
                       WHERE p.dataset_id = m.dataset_id AND p.field_name = m.field_name) AS upper
               FROM embedding_map_state m JOIN datasets d ON d.id = m.dataset_id AND NOT d.deleting
               WHERE m.model_id IS NOT NULL
                 AND NOT EXISTS(SELECT 1 FROM jobs a WHERE a.id = m.active_job_id AND a.state IN ('queued','running'))
                 AND EXISTS(SELECT 1 FROM embedding_projection_pending p
                            WHERE p.dataset_id = m.dataset_id AND p.field_name = m.field_name)
                 AND NOT EXISTS(
                     SELECT 1 FROM jobs j WHERE j.kind = $1 AND j.dataset_id = m.dataset_id
                       AND j.args->>'field' = m.field_name
                       AND (j.state IN ('queued','running')
                            OR (j.finished_at > clock_timestamp() - interval '5 minutes'
                                AND (j.state = 'failed'
                                     OR (j.state = 'succeeded' AND (j.result->>'placed')::int = 0
                                         AND (j.result->>'without_neighbours')::int > 0)))))
               ORDER BY m.dataset_id, m.field_name LIMIT $2""",
            KIND,
            limit,
        )
    queued = []
    for row in maps:
        try:
            queued.append(
                await job_queue.enqueue_job(
                    pool,
                    KIND,
                    dataset_id=row['dataset_id'],
                    args={'field': row['field_name']},
                    barrier={'upper': row['upper']},
                    lane='maintenance',
                    dedupe_key=f"map-place:{row['dataset_id']}:{row['field_name']}",
                ),
            )
        except (job_queue.QueueBusy, asyncpg.ObjectNotInPrerequisiteStateError):
            continue  # busy, or the dataset is being deleted: the next run tries again
    return queued
