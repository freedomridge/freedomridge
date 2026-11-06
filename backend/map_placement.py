"""Placing new samples on maps (kind ``map_place``), from their nearest already-mapped neighbours.

No map keeps a model. Each sample waiting in the projection queue of a field with a map (an
``embedding_viz_meta`` row with points: every whole-image map; patch maps are not placed) goes where the
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
Nothing is placed while the map is being re-fitted, or while a full layout of it is being written
(``placement_held_until``: the SDK's ``compute_visualization`` holds placement from before it fetches the vectors
until it has ranked the new layout, renewing the hold as it goes). A batch looked up on a map that was replaced
or held meanwhile is dropped (its samples stay queued, for the new map), and a sample another writer gave a
place meanwhile keeps it.
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
# How long a hold on a map's placement lasts unless its writer renews it (the SDK renews every
# HOLD_RENEW_SECONDS): a script that dies holds placement up for at most this long.
PLACEMENT_HOLD_SECONDS = 600
HOLD_RENEW_SECONDS = 120
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


# Hold placement on a map for PLACEMENT_HOLD_SECONDS from now (taking or renewing the hold), and release it. A
# field without a map (no embedding_viz_meta row) has nothing placed on it, so nothing to hold.
HOLD_SQL = """UPDATE embedding_viz_meta SET placement_held_until = clock_timestamp() + $3 * interval '1 second'
              WHERE dataset_id = $1 AND field_name = $2 RETURNING placement_held_until"""
RELEASE_SQL = 'UPDATE embedding_viz_meta SET placement_held_until = NULL WHERE dataset_id = $1 AND field_name = $2'


async def hold(connection, dataset_id, field):
    """Hold the map's placement (or renew the hold); its end, or None for a field without a map."""
    return await connection.fetchval(HOLD_SQL, dataset_id, field, PLACEMENT_HOLD_SECONDS)


async def release(connection, dataset_id, field) -> None:
    await connection.execute(RELEASE_SQL, dataset_id, field)


async def _held(connection, dataset_id, field, *, lock=False) -> bool:
    """Whether a full layout of the map holds its placement now. With `lock`, the row stays share-locked until
    the commit, so a hold taken meanwhile waits for this transaction's write (whose samples its layout then
    covers) or is seen."""
    return bool(
        await connection.fetchval(
            f"""SELECT placement_held_until > clock_timestamp() FROM embedding_viz_meta
                WHERE dataset_id = $1 AND field_name = $2{' FOR SHARE' if lock else ''}""",
            dataset_id,
            field,
        ),
    )


async def _measure(pool, job, lookups):
    """The next batch of the job's samples and where each goes, or None when the map can't take any now."""
    dataset_id, field = job['dataset_id'], job['args']['field']
    checkpoint = job['checkpoint'] or {}
    after, upper = checkpoint.get('after', 0), job['barrier']['upper']
    async with pool.acquire(timeout=30) as connection:
        state = await connection.fetchrow(
            """SELECT m.model_id::text AS model_id, (j.state IN ('queued','running')) IS TRUE AS active
               FROM embedding_map_state m LEFT JOIN jobs j ON j.id = m.active_job_id
               WHERE m.dataset_id = $1 AND m.field_name = $2""",
            dataset_id,
            field,
        )
        if (state is not None and state['active']) or await _held(connection, dataset_id, field):
            return None
        # The map's dimension, from one of its points, once per job: a field's vectors share it (ingest refuses
        # another), and one that doesn't (older data) is left unplaced.
        dimension = checkpoint.get('dimension') or await connection.fetchval(
            """SELECT vector_dims(e.vector) FROM samples s
               JOIN embedding_coordinates c ON c.sample_id = s.id AND c.field_name = $2
               JOIN embeddings e ON e.sample_id = s.id AND e.field_name = $2
               WHERE s.dataset_id = $1 LIMIT 1""",
            dataset_id,
            field,
        )
        waiting = await connection.fetch(
            """SELECT p.sample_id, e.xmin::text AS vector_version, vector_dims(e.vector) AS dimension
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
    matching = [row['sample_id'] for row in waiting if row['dimension'] == dimension]
    found = await _neighbours(pool, matching, field, dataset_id, dimension, lookups) if matching else {}
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
        'model_id': state['model_id'] if state else None,  # None: a map never re-fitted
        'dimension': dimension,
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
            'dimension': batch['dimension'],
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
            if await _held(connection, dataset_id, field, lock=True):
                logger.info(
                    'Map placement on dataset %s, %s: a full layout holds it; samples stay queued',
                    dataset_id,
                    field,
                )
                return
            # Only samples still waiting, held until the commit: one another writer placed meanwhile keeps its
            # place, and one it is placing now (its queue row locked) is left to it.
            waiting = {
                row['sample_id']
                for row in await connection.fetch(
                    """SELECT sample_id FROM embedding_projection_pending
                       WHERE sample_id = ANY($1::int[]) AND field_name = $2 FOR SHARE SKIP LOCKED""",
                    [sample_id for sample_id, _, _ in batch['placed']],
                    field,
                )
            }
            kept = [(spot, version) for spot, version in zip(batch['placed'], batch['versions']) if spot[0] in waiting]
            if not kept:
                return
            ids, xs, ys = zip(*(spot for spot, _ in kept))
            status = await connection.execute(
                SAVE_PROJECTED_COORDINATES,
                list(ids),
                list(xs),
                list(ys),
                dataset_id,
                field,
                [version for _, version in kept],
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
    """Queue a placement for each map with samples waiting, up to the newest one waiting now. Skipped: a map
    being re-fitted (its samples wait for the new map), one whose placement is queued or running, and, for 5
    minutes, one whose last placement failed or found no neighbours for anything."""
    async with job_queue._transaction(pool) as connection:
        maps = await connection.fetch(
            """SELECT v.dataset_id, v.field_name,
                      (SELECT max(p.sample_id) FROM embedding_projection_pending p
                       WHERE p.dataset_id = v.dataset_id AND p.field_name = v.field_name) AS upper
               FROM embedding_viz_meta v JOIN datasets d ON d.id = v.dataset_id AND NOT d.deleting
               LEFT JOIN embedding_map_state m ON m.dataset_id = v.dataset_id AND m.field_name = v.field_name
               WHERE v.total_points > 0
                 AND (v.placement_held_until IS NULL OR v.placement_held_until <= clock_timestamp())
                 AND NOT EXISTS(SELECT 1 FROM jobs a WHERE a.id = m.active_job_id AND a.state IN ('queued','running'))
                 AND EXISTS(SELECT 1 FROM embedding_projection_pending p
                            WHERE p.dataset_id = v.dataset_id AND p.field_name = v.field_name)
                 AND NOT EXISTS(
                     SELECT 1 FROM jobs j WHERE j.kind = $1 AND j.dataset_id = v.dataset_id
                       AND j.args->>'field' = v.field_name
                       AND (j.state IN ('queued','running')
                            OR (j.finished_at > clock_timestamp() - interval '5 minutes'
                                AND (j.state = 'failed'
                                     OR (j.state = 'succeeded' AND (j.result->>'placed')::int = 0
                                         AND (j.result->>'without_neighbours')::int > 0)))))
               ORDER BY v.dataset_id, v.field_name LIMIT $2""",
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
