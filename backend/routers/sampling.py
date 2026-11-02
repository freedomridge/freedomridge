"""Sparse sampling endpoint — select maximally diverse subsets from a dataset view."""

import asyncio
import math
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from ..access import require_dataset_access
from ..database import db
from ..request_limits import guest_heavy
from .auth import get_optional_user
from .datasets import (
    SAMPLES_JOIN_FIELD_MAPPING,
    _cancel_on_disconnect,
    _validate_field_name,
    get_dataset_array_columns,
    parse_filter_node,
)

router = APIRouter()

_SAMPLING_SLOTS = asyncio.Semaphore(max(1, int(os.getenv("FRIDGE_SAMPLING_CONCURRENCY", "1"))))
_QUEUE_SECONDS = max(0.01, float(os.getenv("FRIDGE_SAMPLING_QUEUE_SECONDS", "5")))


def _sampling_budget(user: dict | None) -> float | None:
    """Local sampling can take minutes; only demo guests get a default timeout."""
    demo_guest = os.getenv('DEMO_MODE', 'false').lower() == 'true' and user and user.get('role') == 'guest'
    production = os.getenv('FRIDGE_ENV', 'development').strip().lower() in ('production', 'prod')
    configured = os.getenv('FRIDGE_SAMPLING_BUDGET_SECONDS')
    if demo_guest or (production and configured is not None):
        return max(0.01, float(configured if configured is not None else '60'))
    return None


def _check_cancelled(stop: threading.Event | None) -> None:
    if stop is not None and stop.is_set():
        raise asyncio.CancelledError()


# Individual patch tiles (e.g. clip_patch_8x8_r0c0 … _r7c7) aren't a meaningful single diversify axis
# and would flood the picker (80 entries at 4x4+8x8) — offer the whole-image/object embedding instead.
_PATCH_TILE_RE = re.compile(r"_patch_\d+x\d+_r\d+c\d+$")
# Numeric metadata that is an id / foreign key, not a measured quantity — diversifying on it (spreading
# picks across opaque ids) is meaningless, so don't offer it.
_RESERVED_NUMERIC_FIELDS = {"parent_id", "id"}


class SparseSampleRequest(BaseModel):
    # 0 / negative values each silently returned one sample.
    count: Optional[int] = Field(None, ge=1)  # Exact number of samples to select
    percentage: Optional[float] = Field(None, gt=0, le=100)  # Or percentage (0-100] of candidates
    # Bound join fan-out and feature width independently of candidate/result counts.
    fields: List[str] = Field(min_length=1, max_length=32)
    weights: Optional[Dict[str, float]] = None  # Optional per-field weights (default 1.0)
    filters: Optional[Dict[str, Any]] = None  # Current view filters

    @model_validator(mode="after")
    def validate_fields(self):
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("fields must be unique")
        for field in self.fields:
            _validate_field_name(field)
        for field, weight in (self.weights or {}).items():
            if field not in self.fields or not math.isfinite(weight) or weight < 0:
                raise ValueError("weights must be finite, nonnegative and refer to requested fields")
        return self


def farthest_point_sampling(features: np.ndarray, k: int, stop: threading.Event | None = None) -> np.ndarray:
    """Greedy farthest-point sampling: iteratively pick the point most distant from selected set."""
    n = len(features)
    if k >= n:
        return np.arange(n)

    # Start from the point closest to the centroid (stable, not random)
    centroid = features.mean(axis=0)
    dists_to_centroid = np.sum((features - centroid) ** 2, axis=1)
    start = int(np.argmin(dists_to_centroid))

    selected = [start]
    min_dists = np.full(n, np.inf)
    # A chosen point is never the farthest again. Without this, degenerate features (every
    # candidate at one position) left min_dists all 0 and argmax returned index 0 every time —
    # "Selected 5 samples" was 5 copies of one id.
    min_dists[start] = -1.0

    for _ in range(k - 1):
        _check_cancelled(stop)
        # Squared distance from last selected point to all points
        dists = np.sum((features - features[selected[-1]]) ** 2, axis=1)
        np.minimum(min_dists, dists, out=min_dists)
        # Pick farthest
        nxt = int(np.argmax(min_dists))
        selected.append(nxt)
        min_dists[nxt] = -1.0

    return np.array(selected)


def kmeans_diverse_sample(
    features: np.ndarray,
    k: int,
    max_iter: int = 30,
    stop: threading.Event | None = None,
) -> np.ndarray:
    """K-means clustering then pick nearest sample to each centroid. For large k."""
    n = len(features)
    if k >= n:
        return np.arange(n)

    # K-means++ init (sample proportional to distance)
    centroids = np.empty((k, features.shape[1]), dtype=np.float64)
    centroids[0] = features[np.random.randint(n)]
    nearest = np.full(n, np.inf)

    for i in range(1, min(k, 50)):
        _check_cancelled(stop)
        # Full k-means++ for first 50 centroids
        np.minimum(nearest, np.sum((features - centroids[i - 1]) ** 2, axis=1), out=nearest)
        total = nearest.sum()
        centroids[i] = features[np.random.choice(n, p=nearest / total)] if total > 0 else features[np.random.randint(n)]

    if k > 50:
        # Random init for the rest (k-means++ is O(n*k) which is slow for large k)
        remaining_idx = np.random.choice(n, size=k - 50, replace=False)
        centroids[50:] = features[remaining_idx]

    # Run k-means iterations in mini-batches to avoid N×K memory
    batch_size = min(n, 512)
    for _ in range(max_iter):
        # Assign in batches
        labels = np.empty(n, dtype=np.int32)
        for start in range(0, n, batch_size):
            _check_cancelled(stop)
            end = min(start + batch_size, n)
            chunk = features[start:end]
            # Compute distances to centroids in batches of centroids too
            best_dist = np.full(end - start, np.inf)
            best_label = np.zeros(end - start, dtype=np.int32)
            # Bound the temporary N×K×dimensions tensor, not the candidate/result set.
            centroid_batch = min(500, max(1, 1_000_000 // max(1, len(chunk) * features.shape[1])))
            for c_start in range(0, k, centroid_batch):
                _check_cancelled(stop)
                c_end = min(c_start + centroid_batch, k)
                d = np.sum((chunk[:, None] - centroids[c_start:c_end]) ** 2, axis=2)
                local_best = np.argmin(d, axis=1)
                local_dist = d[np.arange(len(d)), local_best]
                improved = local_dist < best_dist
                best_dist[improved] = local_dist[improved]
                best_label[improved] = local_best[improved] + c_start
            labels[start:end] = best_label

        # Update centroids
        new_centroids = np.empty_like(centroids)
        for j in range(k):
            _check_cancelled(stop)
            mask = labels == j
            if mask.any():
                new_centroids[j] = features[mask].mean(axis=0)
            else:
                new_centroids[j] = centroids[j]

        if np.allclose(centroids, new_centroids, atol=1e-6):
            break
        centroids = new_centroids

    # Pick nearest sample to each centroid (avoid duplicates)
    selected = []
    used = set()
    for j in range(k):
        _check_cancelled(stop)
        cluster_mask = labels == j
        cluster_indices = np.where(cluster_mask)[0]
        if len(cluster_indices) == 0:
            continue
        cluster_dists = np.sum((features[cluster_indices] - centroids[j]) ** 2, axis=1)
        order = np.argsort(cluster_dists)
        for idx in order:
            real_idx = int(cluster_indices[idx])
            if real_idx not in used:
                selected.append(real_idx)
                used.add(real_idx)
                break

    return np.array(selected)


def _as_float(v: Any) -> Optional[float]:
    try:
        value = float(v)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _build_diversity_matrix(rows, field_dimensions, weights, stop: threading.Event | None = None):
    """Build the normalized, weighted feature matrix for diversity sampling.

    A sample MISSING a requested field is IMPUTED on that field's axes (the per-dim mean) rather
    than dropped — so a field only some samples have still works (the sample is diversified on the
    fields it DOES have and stays neutral on the rest). A row is dropped only if it has NONE of the
    requested fields. Weights are applied AFTER per-dim [0,1] normalization, because applying them
    before (as the old code did) is cancelled out by the normalization — weights had no effect.

    Returns (sample_ids, features) with features shaped (n, total_dims); ([], None) if nothing usable.
    """
    # 1. Decide each field's kind + width once, consistently across all rows.
    specs = []  # (field, kind, width)
    for field in field_dimensions:
        _check_cancelled(stop)
        if field == "location":
            # Gate location like the other kinds: include it only if some candidate row actually has
            # a location. Otherwise both location columns would be all-NaN, nanmin/nanmax/nanmean
            # return NaN, the impute can't fill them, and the NaN matrix collapses FPS to one point.
            if any(r.get("lon") is not None and r.get("lat") is not None for r in rows):
                specs.append((field, "location", 2))
            continue
        x_key, y_key = f"{field}_x", f"{field}_y"
        if any(r.get(x_key) is not None and r.get(y_key) is not None for r in rows):
            specs.append((field, "embedding", 2))
        elif any(_as_float(r.get(f"meta_{field}")) is not None for r in rows):
            specs.append((field, "numeric", 1))
        # else: the field has no usable values on any candidate row — omit it entirely.
    if not specs:
        return [], None

    # 2. Raw rows with NaN for a missing field's dims; keep a row only if it has >= 1 field present.
    sample_ids, raw = [], []
    for r in rows:
        _check_cancelled(stop)
        vec: list = []
        present = False
        for field, kind, _width in specs:
            if kind == "location":
                lon, lat = r.get("lon"), r.get("lat")
                if lon is not None and lat is not None:
                    vec += [float(lon), float(lat)]
                    present = True
                else:
                    vec += [np.nan, np.nan]
            elif kind == "embedding":
                x, y = r.get(f"{field}_x"), r.get(f"{field}_y")
                if x is not None and y is not None:
                    vec += [float(x), float(y)]
                    present = True
                else:
                    vec += [np.nan, np.nan]
            else:  # numeric metadata
                v = _as_float(r.get(f"meta_{field}"))
                if v is not None:
                    vec.append(v)
                    present = True
                else:
                    vec.append(np.nan)
        if present:
            sample_ids.append(r["id"])
            raw.append(vec)
    if not raw:
        return [], None

    features = np.array(raw, dtype=np.float64)

    # 3. Normalize each dim to [0,1] over PRESENT values, impute missing with the per-dim mean
    #    (least-biasing: a missing field neither spreads nor clusters the sample), then weight.
    col_min = np.nanmin(features, axis=0)
    col_max = np.nanmax(features, axis=0)
    col_range = col_max - col_min
    col_range[col_range == 0] = 1.0
    features = (features - col_min) / col_range
    col_mean = np.nanmean(features, axis=0)
    nan_idx = np.where(np.isnan(features))
    features[nan_idx] = np.take(col_mean, nan_idx[1])

    wvec = []
    for field, _kind, width in specs:
        wvec += [float(weights.get(field, 1.0))] * width
    features = features * np.array(wvec, dtype=np.float64)

    return sample_ids, features


@router.post("/datasets/{dataset_id}/sparse-sample", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def sparse_sample_route(
    dataset_id: int,
    request: SparseSampleRequest,
    http_request: Request,
    user: dict | None = Depends(get_optional_user),
):
    return await sparse_sample(dataset_id, request, http_request, user)


async def sparse_sample(
    dataset_id: int,
    request: SparseSampleRequest,
    http_request: Request | None = None,
    user: dict | None = None,
):
    """Bound admission before fetching candidates; keep the slot until the worker exits."""
    stop = threading.Event()

    async def run():
        try:
            await asyncio.wait_for(_SAMPLING_SLOTS.acquire(), timeout=_QUEUE_SECONDS)
        except asyncio.TimeoutError:
            raise HTTPException(503, "Sparse sampling is busy, retry shortly", headers={"Retry-After": "5"})
        try:
            return await _sparse_sample(dataset_id, request, stop)
        finally:
            _SAMPLING_SLOTS.release()

    async def bounded():
        try:
            # Includes queueing and SQL, and waits for cooperative thread shutdown
            # before returning a timeout or releasing the sampling slot.
            return await asyncio.wait_for(run(), timeout=_sampling_budget(user))
        except asyncio.TimeoutError:
            raise HTTPException(
                503,
                "Sparse sample took too long; try a smaller count or percentage",
                headers={"Retry-After": "5"},
            )
        finally:
            stop.set()

    if http_request is not None:
        return await _cancel_on_disconnect(http_request, bounded())
    return await bounded()


async def _sparse_sample(dataset_id: int, request: SparseSampleRequest, stop: threading.Event):
    if request.count is None and request.percentage is None:
        raise HTTPException(400, "Must provide either 'count' or 'percentage'")
    if not request.fields:
        raise HTTPException(400, "Must provide at least one field to diversify on")

    t0 = time.time()

    # --- 1. Build candidate query with filters ---
    conditions = ["s.dataset_id = $1"]
    args: list = [dataset_id]
    arg_idx = 2

    if request.filters:
        sql, arg_idx = parse_filter_node(
            request.filters,
            args,
            arg_idx,
            field_mapping=SAMPLES_JOIN_FIELD_MAPPING,
            dataset_id=dataset_id,
            array_columns=await get_dataset_array_columns(dataset_id),
        )
        if sql and sql != "TRUE":
            conditions.append(sql)

    # --- 2. Determine which columns/joins we need ---
    select_parts = ["s.id"]
    joins = []
    field_dimensions: Dict[str, List[str]] = {}  # field -> list of column aliases

    array_cols = await get_dataset_array_columns(dataset_id)

    for i, field in enumerate(request.fields):
        # Field names are interpolated into the SELECT list below (e.g.
        # `(s.metadata->>'{field}')`), and asyncpg cannot parameterize an
        # identifier / JSON key — so every field MUST pass the same strict
        # allowlist used across the rest of the codebase before it reaches SQL.
        # Without this, request.fields is a raw SQL-injection vector (and this
        # endpoint is reachable unauthenticated on global datasets).
        _validate_field_name(field)
        if field == "location":
            select_parts.append("ST_X(s.location::geometry) AS lon")
            select_parts.append("ST_Y(s.location::geometry) AS lat")
            # No NOT NULL pre-filter — location-missing rows are imputed in the matrix (so location
            # can be combined with a field some non-located rows have), not pre-dropped here.
            field_dimensions["location"] = ["lon", "lat"]

        elif field in array_cols:
            # Skip array fields — can't diversify on categorical
            continue

        else:
            # Check if it's an embedding field name (has rows in embedding_coordinates)
            # or a numeric metadata field
            alias = f"e{i}"
            # Try as embedding field first — we'll validate after query
            joins.append(
                f"LEFT JOIN embedding_coordinates {alias} ON {alias}.sample_id = s.id "
                f"AND {alias}.field_name = ${arg_idx}",
            )
            args.append(field)
            arg_idx += 1

            select_parts.append(f'{alias}.x AS "{field}_x"')
            select_parts.append(f'{alias}.y AS "{field}_y"')
            # Also grab metadata value as fallback for numeric fields
            select_parts.append(f"(s.metadata->>'{field}') AS \"meta_{field}\"")
            field_dimensions[field] = [f"{field}_x", f"{field}_y", f"meta_{field}"]

    if not field_dimensions:
        raise HTTPException(400, "None of the requested fields can be used for diversity sampling")

    # Location-ONLY diversify can prune at the DB via the partial location index (fast). With other
    # fields, non-located rows must survive (location is imputed in the matrix), so don't pre-filter.
    if list(field_dimensions) == ["location"]:
        conditions.append("s.location IS NOT NULL")

    where_clause = " AND ".join(conditions)
    join_clause = "\n".join(joins)

    query = f"""
        SELECT {', '.join(select_parts)}
        FROM samples s
        {join_clause}
        WHERE {where_clause}
    """

    rows = await db.fetch(query, *args)

    if not rows:
        return {"sample_ids": [], "count": 0, "candidate_count": 0}

    worker = asyncio.create_task(asyncio.to_thread(_calculate_sample, rows, field_dimensions, request, stop, t0))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        stop.set()
        # Cancelling to_thread doesn't stop its thread. Wait for the cooperative checkpoints
        # before returning the semaphore slot, so disconnected clients cannot bypass it.
        await asyncio.gather(worker, return_exceptions=True)
        raise


def _calculate_sample(rows, field_dimensions, request, stop, t0):
    _check_cancelled(stop)

    # --- 3. Build feature matrix (impute missing fields; drop a row only if it has none) ---
    sample_ids, features = _build_diversity_matrix(
        [dict(r) for r in rows],
        field_dimensions,
        request.weights or {},
        stop,
    )
    if not sample_ids or features is None:
        return {"sample_ids": [], "count": 0, "candidate_count": 0}
    candidate_count = len(sample_ids)

    # --- 4. Determine target count ---
    if request.count is not None:
        k = min(request.count, candidate_count)
    else:
        # Validated at the top of the endpoint: count or percentage must be set.
        assert request.percentage is not None
        k = min(int(candidate_count * request.percentage / 100.0), candidate_count)
        k = max(k, 1)

    # --- 5. Run diversity sampling ---
    # FPS for small-medium k, k-means for large k
    if k <= 5000:
        indices = farthest_point_sampling(features, k, stop=stop)
    else:
        indices = kmeans_diverse_sample(features, k, stop=stop)

    selected_ids = [sample_ids[i] for i in indices]

    elapsed = time.time() - t0
    print(f"Sparse sample: {k} from {candidate_count} candidates, {len(request.fields)} fields, {elapsed:.2f}s")

    return {
        "sample_ids": selected_ids,
        "count": len(selected_ids),
        "candidate_count": candidate_count,
        "elapsed_seconds": round(elapsed, 2),
    }


@router.get("/datasets/{dataset_id}/sparse-sample/available-fields", dependencies=[Depends(require_dataset_access)])
async def get_available_fields(dataset_id: int):
    """Return fields available for diversity sampling: embedding fields, location, numeric metadata."""
    available = []

    # Check if dataset has location data
    has_location = await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM samples WHERE dataset_id = $1 AND location IS NOT NULL LIMIT 1)",
        dataset_id,
    )
    if has_location:
        available.append({"name": "location", "type": "location", "description": "GPS coordinates"})

    # Embedding fields with UMAP coordinates. Scoped to image rows: sparse-sample draws from the
    # image grain, so an object-grain field (clip_obj, on media_type='object' rows) would offer a
    # space the sampled rows lack -> every candidate skipped -> 0 selected with no hint why.
    emb_rows = await db.fetch(
        """
        SELECT DISTINCT e.field_name, COUNT(*) as count
        FROM embedding_coordinates e
        JOIN samples s ON e.sample_id = s.id
        WHERE s.dataset_id = $1 AND e.x IS NOT NULL AND s.media_type <> 'object'
        GROUP BY e.field_name
    """,
        dataset_id,
    )
    for r in emb_rows:
        if _PATCH_TILE_RE.search(r["field_name"]):
            continue  # skip per-tile patch fields — not a useful single diversify axis (would flood the picker)
        available.append(
            {"name": r["field_name"], "type": "embedding", "description": f"Embedding UMAP ({r['count']} samples)"},
        )

    # Numeric metadata fields from stats cache
    num_rows = await db.fetch(
        """
        SELECT field_name, sample_count
        FROM dataset_field_stats
        WHERE dataset_id = $1 AND field_type = 'number'
        ORDER BY field_name
    """,
        dataset_id,
    )
    for r in num_rows:
        if r["field_name"] in _RESERVED_NUMERIC_FIELDS:
            continue  # id / foreign-key fields are numeric but meaningless to diversify on
        available.append(
            {
                "name": r["field_name"],
                "type": "numeric",
                "description": f"Numeric metadata ({r['sample_count']} samples)",
            },
        )

    return available
