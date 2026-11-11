"""Shared bulk coordinate write for the API and local projection worker."""

import json

# Last occurrence wins within a retried batch; PostgreSQL upsert cannot touch
# the same key twice. The joins preserve the existing cross-dataset/missing-key guard.
SAVE_COORDINATES = """
INSERT INTO embedding_coordinates AS c (sample_id, field_name, x, y, dataset_id)
SELECT e.sample_id, e.field_name, v.x, v.y, $4
FROM (
    SELECT DISTINCT ON (sample_id) sample_id, x, y
    FROM unnest($1::int[], $2::float8[], $3::float8[]) WITH ORDINALITY AS u(sample_id, x, y, ord)
    ORDER BY sample_id, ord DESC
) v
JOIN samples s ON s.id = v.sample_id AND s.dataset_id = $4
JOIN embeddings e ON e.dataset_id = $4 AND e.sample_id = v.sample_id AND e.field_name = $5
ON CONFLICT (sample_id, field_name) DO UPDATE SET x = EXCLUDED.x, y = EXCLUDED.y
WHERE (c.x, c.y) IS DISTINCT FROM (EXCLUDED.x, EXCLUDED.y)
"""

# Projection reads happen before model.transform. Hold a SHARE row lock while
# checking xmin and publishing, so a concurrent vector UPDATE either precedes
# this check or subsequently clears these coordinates and requeues the point.
# This lock creates no new embedding tuple or HNSW entry.
SAVE_PROJECTED_COORDINATES = """
WITH candidates AS MATERIALIZED (
    SELECT e.sample_id, e.field_name, e.xmin::text AS vector_version, v.x, v.y, v.expected_version
    FROM (
        SELECT DISTINCT ON (sample_id) sample_id, x, y, expected_version
        FROM unnest($1::int[], $2::float8[], $3::float8[], $6::text[])
            WITH ORDINALITY AS u(sample_id, x, y, expected_version, ord)
        ORDER BY sample_id, ord DESC
    ) v
    JOIN samples s ON s.id = v.sample_id AND s.dataset_id = $4
    JOIN embeddings e ON e.dataset_id = $4 AND e.sample_id = v.sample_id AND e.field_name = $5
    ORDER BY e.sample_id
    FOR SHARE OF e
)
INSERT INTO embedding_coordinates AS c (sample_id, field_name, x, y, dataset_id)
SELECT sample_id, field_name, x, y, $4 FROM candidates
WHERE expected_version IS NULL OR vector_version = expected_version
ON CONFLICT (sample_id, field_name) DO UPDATE SET x = EXCLUDED.x, y = EXCLUDED.y
WHERE (c.x, c.y) IS DISTINCT FROM (EXCLUDED.x, EXCLUDED.y)
"""

# A field with scheduled re-fits has one current model. Projection writes hold this
# row lock while checking it, so a re-fit's activation or publication orders against them: during a
# re-fit nothing is placed, and afterwards only the current model's results are accepted.
MAP_FENCE = """
SELECT m.model_id::text AS model_id, (j.state IN ('queued','running')) IS TRUE AS active
FROM embedding_map_state m LEFT JOIN jobs j ON j.id = m.active_job_id
WHERE m.dataset_id = $1 AND m.field_name = $2
FOR SHARE OF m
"""


class MapRefitActive(Exception):
    """A re-fit is active, or a full replacement is fenced by a queued re-fit."""


class MapModelChanged(Exception):
    """The coordinates were computed with a model that is no longer the field's current one."""


class MapManaged(Exception):
    """A manual replacement would invalidate the managed model."""


def check_map_identity(state, field_name, model_id=None, *, projected=True):
    """Permanent refusals take precedence over temporary activity or lock contention."""
    if state is None or state['model_id'] is None:
        return
    if not projected:
        raise MapManaged('This map is managed by scheduled re-fits; run a re-fit to change its layout.')
    if state['model_id'] != (str(model_id) if model_id else None):
        raise MapModelChanged(
            f"The map of '{field_name}' was re-fitted; project with its current model {state['model_id']}",
        )


async def check_map_fence(connection, dataset_id, field_name, model_id=None, *, projected=True):
    """Fence full replacements from admission; fence incremental writes only from activation.

    Incremental writes before activation are safe: the subsequent export and publication replace
    their coordinates. A managed map's state-row lock orders them against activation/publication.
    """
    # Publication takes the lifecycle gate (20261016) before the map row. Coordinate triggers
    # instead take the separate rank fence (20261020); take the lifecycle gate here first.
    await connection.execute('SELECT pg_advisory_xact_lock_shared(20261016, $1)', dataset_id)
    if not projected:
        await connection.execute(
            'SELECT pg_advisory_xact_lock_shared(hashtextextended($1, 0))',
            json.dumps(['map_refit', dataset_id, field_name]),
        )
    state = await connection.fetchrow(MAP_FENCE, dataset_id, field_name)
    check_map_identity(state, field_name, model_id, projected=projected)
    active = state is not None and state['active']
    if not projected:
        active = active or await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM jobs WHERE kind='map_refit' AND dataset_id=$1 "
            "AND args->>'field'=$2 AND state IN ('queued','running'))",
            dataset_id,
            field_name,
        )
    if active:
        raise MapRefitActive(f"A map re-fit of '{field_name}' is in progress; new points are placed when it finishes")
