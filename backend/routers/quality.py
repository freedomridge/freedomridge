"""Server-side embedding quality scoring (bounded ANN).

Replaces the SDK's brute-force O(n^2) cosine NearestNeighbors (which loaded every
vector into the client and OOM'd / ran for hours on millions of samples) with
keyset-chunked queries that use the per-field pgvector HNSW index via a LATERAL
top-k probe. Memory stays flat; the SDK drives the chunks with a progress bar and
the work is resumable (keyset `after_id`).

Each compute pass writes raw scores to samples.metadata; min-max normalization (to
match the old [0,1] output) is a separate chunked pass (/range + /normalize).

Requires the HNSW index from scripts/build_ann_indexes.py — without it a per-sample
LATERAL would seq-scan the whole field, so the ANN endpoints refuse (409) unless
allow_seq_scan=true.
"""

import re
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import job_queue, job_wait
from ..access import require_dataset_access
from ..database import db
from ..field_registry import (
    REQUEST_FULL_REFRESH,
    invalidate_values,
    lock_field_lifecycle,
    prune_fields,
    register_fields,
)
from ..index_jobs import submit_ann_index, valid_hnsw_index
from .auth import require_role

router = APIRouter(
    prefix="/datasets/{id}/quality",
    tags=["quality"],
    dependencies=[Depends(require_dataset_access)],
)

_MAX_PAIRS = 5_000_000  # finalize() refuses above this — lower the threshold instead


def _clamp_ef(ef: int) -> int:
    return max(10, min(1000, int(ef)))


async def _probe_dim(dataset_id: int, field: str) -> Optional[int]:
    row = await db.fetchrow(
        "SELECT vector_dims(vector) AS d FROM embeddings WHERE dataset_id = $1 AND field_name = $2 "
        "AND vector IS NOT NULL LIMIT 1",
        dataset_id,
        field,
    )
    return int(row['d']) if row and row['d'] else None


async def _require_ann(dataset_id: int, field: str, allow_seq_scan: bool) -> int:
    """Probe the field's dimension in the dataset and ensure its eligible HNSW index exists. Only a valid one counts:
    an INVALID leftover would send the ANN endpoints down a per-sample seq scan while claiming to be indexed (the
    exact cliff the 409 prevents)."""
    dim = await _probe_dim(dataset_id, field)
    if dim is None:
        raise HTTPException(status_code=404, detail=f"No embeddings for field '{field}'")
    async with db.pool.acquire() as connection:
        indexed = await valid_hnsw_index(connection, dataset_id, field, dim)
    if not indexed and not allow_seq_scan:
        raise HTTPException(
            status_code=409,
            detail=(
                f"No HNSW index for field '{field}' (dim {dim}). Build it via "
                f"POST .../quality/ann-index (SDK: dataset.build_ann_index('{field}')) or "
                f"`python3 scripts/build_ann_indexes.py --field {field}`, or pass "
                f"allow_seq_scan=true to force a (slow) sequential scan."
            ),
        )
    return dim


# Matches scripts/build_ann_indexes.py — the field name becomes part of the CREATE
# INDEX DDL below (identifier + literal), so it must be strictly alphanumeric.
_SAFE_FIELD = re.compile(r"^[a-zA-Z0-9_]+$")


class AnnIndexRequest(BaseModel):
    field: str


@router.post("/ann-index")
async def build_ann_index(
    id: int,
    request: AnnIndexRequest,
    user: dict = Depends(require_role("power_user")),
    wait: bool = True,
):
    """Build the per-field HNSW index server-side, as a runner job (backend.index_jobs).

    The ANN endpoints' 409 used to point ONLY at scripts/build_ann_indexes.py — an
    operator-run script needing repo + DATABASE_URL access an SDK/API user doesn't
    have, making uniqueness/duplicates a dead end for them. Same index as the
    script: partial expression index per (field_name, dim), built CONCURRENTLY (no
    write lock), IF NOT EXISTS (idempotent). Slow + memory-heavy on millions of
    vectors, so the request answers within five seconds: the final result, or
    202 {job_id, state} while the build continues (progress/cancel in Jobs).
    """
    started = time.monotonic()
    field = request.field
    if not _SAFE_FIELD.match(field or ""):
        raise HTTPException(status_code=422, detail=f"Unsafe field name {field!r}")
    try:
        job = await submit_ann_index(db.pool, id, user['id'], field)
    except job_queue.QueueBusy as error:
        raise HTTPException(status_code=409, detail='Index submission is busy; retry') from error
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)


# ---------------------------------------------------------------------------
# Uniqueness — mean cosine distance to the k nearest neighbours (chunked)
# ---------------------------------------------------------------------------


class UniquenessRequest(BaseModel):
    field: str
    k: int = 3
    ef_search: int = 64
    after_id: int = 0
    chunk: int = 5000
    metadata_field: str = "uniqueness"
    allow_seq_scan: bool = False


@router.post("/uniqueness/compute")
async def uniqueness_compute(id: int, request: UniquenessRequest, user: dict = Depends(require_role("power_user"))):
    """Write the RAW mean-k-NN cosine distance for one keyset chunk of samples.

    Loop with after_id = last_id until processed < chunk, then /range + /normalize.
    """
    dim = await _require_ann(id, request.field, request.allow_seq_scan)
    k = max(1, int(request.k))
    chunk = max(1, min(50000, int(request.chunk)))
    sql = f"""
        WITH seeds AS (
            SELECT e1.sample_id AS sid, e1.vector AS vec
            FROM embeddings e1
            WHERE e1.dataset_id = $2 AND e1.field_name = $1 AND e1.sample_id > $3
            ORDER BY e1.sample_id LIMIT $4
        ),
        scored AS (
            SELECT sd.sid, AVG(nn.dist) AS raw
            FROM seeds sd CROSS JOIN LATERAL (
                SELECT (e2.vector::vector({dim}) <=> sd.vec::vector({dim})) AS dist
                FROM embeddings e2
                WHERE e2.dataset_id = $2 AND e2.field_name = $1 AND e2.sample_id <> sd.sid
                ORDER BY e2.vector::vector({dim}) <=> sd.vec::vector({dim})
                LIMIT $5
            ) nn
            GROUP BY sd.sid
        ),
        upd AS (
            UPDATE samples s SET metadata = s.metadata || jsonb_build_object($6::text, sc.raw)
            FROM scored sc WHERE s.id = sc.sid RETURNING s.id
        )
        SELECT (SELECT count(*) FROM seeds) AS processed,
               (SELECT COALESCE(max(sid), $3) FROM seeds) AS last_id,
               (SELECT count(*) FROM upd) AS written
    """
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL hnsw.ef_search = {_clamp_ef(request.ef_search)}")
            await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
            await lock_field_lifecycle(conn, id)
            row = await conn.fetchrow(sql, request.field, id, request.after_id, chunk, k, request.metadata_field)
            if row['written']:
                await register_fields(conn, id, [{request.metadata_field: 0.0}])
                await invalidate_values(conn, id, [request.metadata_field])
    if row.get('written', row['processed']):
        await db.execute(
            REQUEST_FULL_REFRESH,
            id,
        )
    processed = row['processed']
    return {"processed": processed, "last_id": row['last_id'], "done": processed < chunk}


# ---------------------------------------------------------------------------
# Representativeness — cosine similarity to the dataset centroid (chunked)
# ---------------------------------------------------------------------------


class CentroidRequest(BaseModel):
    field: str


@router.post("/representativeness/centroid")
async def representativeness_centroid(id: int, request: CentroidRequest):
    """Compute the dataset centroid (mean of unit vectors, re-normalized). Returns
    it as a pgvector literal the caller passes back to /representativeness/compute.
    O(n) single scan; no index needed."""
    dim = await _probe_dim(id, request.field)
    if dim is None:
        raise HTTPException(status_code=404, detail=f"No embeddings for field '{request.field}'")
    row = await db.fetchrow(
        f"""
        SELECT l2_normalize(avg(l2_normalize(e.vector::vector({dim}))))::text AS centroid
        FROM embeddings e JOIN samples s ON s.dataset_id = e.dataset_id AND s.id = e.sample_id
        WHERE e.field_name = $1 AND s.dataset_id = $2
    """,
        request.field,
        id,
    )
    if not row or not row['centroid']:
        raise HTTPException(status_code=404, detail="No embeddings to average")
    return {"centroid": row['centroid'], "dim": dim}


class RepresentativenessComputeRequest(BaseModel):
    field: str
    centroid: str  # pgvector literal from /representativeness/centroid
    after_id: int = 0
    chunk: int = 20000
    metadata_field: str = "representativeness"


@router.post("/representativeness/compute")
async def representativeness_compute(
    id: int,
    request: RepresentativenessComputeRequest,
    user: dict = Depends(require_role("power_user")),
):
    """Write RAW cosine similarity to the centroid for one keyset chunk. No ANN
    index needed (plain scan), so no guard. Loop, then /range + /normalize."""
    dim = await _probe_dim(id, request.field)
    if dim is None:
        raise HTTPException(status_code=404, detail=f"No embeddings for field '{request.field}'")
    chunk = max(1, min(100000, int(request.chunk)))
    sql = f"""
        WITH seeds AS (
            SELECT e.sample_id AS sid,
                   (1 - (e.vector::vector({dim}) <=> $5::vector({dim}))) AS sim
            FROM embeddings e JOIN samples s ON s.dataset_id = e.dataset_id AND s.id = e.sample_id
            WHERE e.field_name = $1 AND s.dataset_id = $2 AND e.sample_id > $3
            ORDER BY e.sample_id LIMIT $4
        ),
        upd AS (
            UPDATE samples s SET metadata = s.metadata || jsonb_build_object($6::text, sd.sim)
            FROM seeds sd WHERE s.id = sd.sid RETURNING s.id
        )
        SELECT (SELECT count(*) FROM seeds) AS processed,
               (SELECT COALESCE(max(sid), $3) FROM seeds) AS last_id,
               (SELECT count(*) FROM upd) AS written
    """
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await lock_field_lifecycle(conn, id)
            row = await conn.fetchrow(
                sql,
                request.field,
                id,
                request.after_id,
                chunk,
                request.centroid,
                request.metadata_field,
            )
            if row['written']:
                await register_fields(conn, id, [{request.metadata_field: 0.0}])
                await invalidate_values(conn, id, [request.metadata_field])
    if row.get('written', row['processed']):
        await db.execute(
            REQUEST_FULL_REFRESH,
            id,
        )
    processed = row['processed']
    return {"processed": processed, "last_id": row['last_id'], "done": processed < chunk}


# ---------------------------------------------------------------------------
# Min-max normalize a numeric metadata field, in place, chunked
# ---------------------------------------------------------------------------


@router.get("/range")
async def metadata_range(id: int, field: str):
    """min/max of a numeric metadata field over the dataset (for normalization)."""
    row = await db.fetchrow(
        """
        SELECT min((metadata->>$2)::float) AS lo, max((metadata->>$2)::float) AS hi
        FROM samples WHERE dataset_id = $1 AND metadata ? $2
    """,
        id,
        field,
    )
    return {"min": row['lo'], "max": row['hi']}


class NormalizeRequest(BaseModel):
    metadata_field: str
    lo: float
    span: float  # max - min (0 => everything maps to 0)
    after_id: int = 0
    chunk: int = 50000


@router.post("/normalize")
async def normalize(id: int, request: NormalizeRequest, user: dict = Depends(require_role("power_user"))):
    """Rewrite metadata_field in place to (raw - lo)/span for one keyset chunk."""
    chunk = max(1, min(200000, int(request.chunk)))
    span = request.span if request.span and request.span > 0 else None
    expr = "(((s.metadata->>$2)::float - $3) / $4)" if span else "0"
    params = [id, request.metadata_field]
    if span:
        params += [request.lo, request.span]
    sql = f"""
        WITH seeds AS (
            SELECT id AS sid FROM samples
            WHERE dataset_id = $1 AND metadata ? $2 AND id > ${'5' if span else '3'}
            ORDER BY id LIMIT ${'6' if span else '4'}
        ),
        upd AS (
            UPDATE samples s SET metadata = s.metadata || jsonb_build_object($2::text, {expr})
            FROM seeds sd WHERE s.id = sd.sid RETURNING s.id
        )
        SELECT (SELECT count(*) FROM seeds) AS processed,
               (SELECT COALESCE(max(sid), ${'5' if span else '3'}) FROM seeds) AS last_id,
               (SELECT count(*) FROM upd) AS written
    """
    params += [request.after_id, chunk]
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await lock_field_lifecycle(conn, id)
            row = await conn.fetchrow(sql, *params)
            if row['processed']:
                await register_fields(conn, id, [{request.metadata_field: 0.0}])
                await invalidate_values(conn, id, [request.metadata_field])
    if row.get('written', row['processed']):
        await db.execute(
            REQUEST_FULL_REFRESH,
            id,
        )
    processed = row['processed']
    return {"processed": processed, "last_id": row['last_id'], "done": processed < chunk}


# ---------------------------------------------------------------------------
# Near-duplicate grouping — HNSW radius pairs -> staging -> union-find (chunked)
# ---------------------------------------------------------------------------


class DuplicatesComputeRequest(BaseModel):
    field: str
    thresh: float = 0.04
    k: int = 20
    ef_search: int = 64
    after_id: int = 0
    chunk: int = 5000
    token: str
    allow_seq_scan: bool = False


@router.post("/duplicates/compute")
async def duplicates_compute(
    id: int,
    request: DuplicatesComputeRequest,
    user: dict = Depends(require_role("power_user")),
):
    """Stream candidate near-duplicate pairs (HNSW top-k within `thresh`) for one
    keyset chunk into quality_dup_pairs[token]. Loop, then /duplicates/finalize."""
    dim = await _require_ann(id, request.field, request.allow_seq_scan)
    if not (0 < request.thresh <= 0.5):
        raise HTTPException(status_code=400, detail="thresh must be in (0, 0.5] for near-duplicates")
    k = max(1, min(100, int(request.k)))
    chunk = max(1, min(50000, int(request.chunk)))
    if request.after_id == 0:
        await db.execute("DELETE FROM quality_dup_pairs WHERE created_at < now() - interval '6 hours'")
    sql = f"""
        WITH seeds AS (
            SELECT e1.sample_id AS sid, e1.vector AS vec
            FROM embeddings e1
            WHERE e1.dataset_id = $2 AND e1.field_name = $1 AND e1.sample_id > $3
            ORDER BY e1.sample_id LIMIT $4
        ),
        pairs AS (
            SELECT sd.sid AS a, nn.b AS b
            FROM seeds sd CROSS JOIN LATERAL (
                SELECT e2.sample_id AS b, (e2.vector::vector({dim}) <=> sd.vec::vector({dim})) AS dist
                FROM embeddings e2
                WHERE e2.dataset_id = $2 AND e2.field_name = $1 AND e2.sample_id <> sd.sid
                ORDER BY e2.vector::vector({dim}) <=> sd.vec::vector({dim})
                LIMIT $5
            ) nn
            WHERE nn.dist <= $6
        ),
        ins AS (
            INSERT INTO quality_dup_pairs (token, a, b) SELECT $7, a, b FROM pairs RETURNING 1
        )
        SELECT (SELECT count(*) FROM seeds) AS processed,
               (SELECT COALESCE(max(sid), $3) FROM seeds) AS last_id,
               (SELECT count(*) FROM ins) AS pairs_written
    """
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL hnsw.ef_search = {_clamp_ef(request.ef_search)}")
            await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
            row = await conn.fetchrow(sql, request.field, id, request.after_id, chunk, k, request.thresh, request.token)
    processed = row['processed']
    return {
        "processed": processed,
        "last_id": row['last_id'],
        "pairs_written": row['pairs_written'],
        "done": processed < chunk,
    }


class DuplicatesFinalizeRequest(BaseModel):
    token: str
    group_field: str = "emb_dup_group"


@router.post("/duplicates/finalize")
async def duplicates_finalize(
    id: int,
    request: DuplicatesFinalizeRequest,
    user: dict = Depends(require_role("power_user")),
):
    """Union-find the staged pairs into groups and write the group id back to
    samples.metadata[group_field]. Returns {total_groups, total_duplicate_samples}."""
    n_pairs = await db.fetchval("SELECT count(*) FROM quality_dup_pairs WHERE token = $1", request.token)
    if n_pairs > _MAX_PAIRS:
        await db.execute("DELETE FROM quality_dup_pairs WHERE token = $1", request.token)
        raise HTTPException(status_code=413, detail=f"Too many candidate pairs ({n_pairs}); lower the threshold.")

    parent: Dict[int, int] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    rows = await db.fetch("SELECT a, b FROM quality_dup_pairs WHERE token = $1", request.token)
    for r in rows:
        a, b = r['a'], r['b']
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    comp: Dict[int, List[int]] = {}
    for node in list(parent.keys()):
        comp.setdefault(find(node), []).append(node)
    groups = [m for m in comp.values() if len(m) > 1]

    # Deterministic group ids (sorted by smallest member); write back batched.
    groups.sort(key=lambda m: min(m))
    updates = []  # (sample_id, group_id)
    for gid, members in enumerate(groups):
        for sid in members:
            updates.append((sid, gid))

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            # Clear any prior grouping for this field, then write the new ids.
            await lock_field_lifecycle(conn, id, exclusive=True, wait=False)
            await conn.execute(
                "UPDATE samples SET metadata = metadata - $2::text WHERE dataset_id = $1 AND metadata ? $2",
                id,
                request.group_field,
            )
            for i in range(0, len(updates), 5000):
                batch = updates[i : i + 5000]
                await conn.execute(
                    """UPDATE samples s SET metadata = s.metadata || jsonb_build_object($2::text, v.gid)
                        FROM (SELECT * FROM unnest($3::int[], $4::int[]) AS t(sid, gid)) v
                        WHERE s.id = v.sid AND s.dataset_id = $1""",
                    id,
                    request.group_field,
                    [u[0] for u in batch],
                    [u[1] for u in batch],
                )

            await conn.execute("DELETE FROM quality_dup_pairs WHERE token = $1", request.token)
            if updates:
                await register_fields(conn, id, [{request.group_field: 0}])
            await invalidate_values(conn, id, [request.group_field])
            await prune_fields(conn, id, [request.group_field])
    await db.execute(
        REQUEST_FULL_REFRESH,
        id,
    )
    return {"total_groups": len(groups), "total_duplicate_samples": len(updates), "group_field": request.group_field}


class DuplicatesGroupsRequest(BaseModel):
    group_field: str = "emb_dup_group"
    limit: int = 50
    offset: int = 0


@router.post("/duplicates/groups")
async def duplicates_groups(id: int, request: DuplicatesGroupsRequest):
    """Paged read of the near-duplicate groups written by finalize (group_field)."""
    rows = await db.fetch(
        """
        SELECT (metadata->>$2) AS gid, array_agg(id ORDER BY id) AS sample_ids, count(*) AS cnt
        FROM samples WHERE dataset_id = $1 AND metadata ? $2
        GROUP BY gid HAVING count(*) > 1
        ORDER BY cnt DESC, gid LIMIT $3 OFFSET $4
    """,
        id,
        request.group_field,
        request.limit,
        request.offset,
    )
    total = await db.fetchval(
        """
        SELECT count(*) FROM (
            SELECT 1 FROM samples WHERE dataset_id = $1 AND metadata ? $2
            GROUP BY (metadata->>$2) HAVING count(*) > 1
        ) g
    """,
        id,
        request.group_field,
    )
    total_samples = await db.fetchval(
        """
        SELECT COALESCE(count(*), 0) FROM samples WHERE dataset_id = $1 AND metadata ? $2
    """,
        id,
        request.group_field,
    )
    return {
        "groups": [{"sample_ids": r['sample_ids'], "size": r['cnt']} for r in rows],
        "total_groups": total,
        "total_duplicate_samples": total_samples,
        "group_field": request.group_field,
    }
