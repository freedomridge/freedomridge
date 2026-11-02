import asyncio
import json
import logging
import math
import re
import struct
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Set

import asyncpg
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, FiniteFloat, model_validator

from ..access import require_dataset_access
from ..api_errors import CodedHTTPException
from ..coordinates import (
    SAVE_COORDINATES,
    SAVE_PROJECTED_COORDINATES,
    MapManaged,
    MapModelChanged,
    MapRefitActive,
    check_map_fence,
    check_map_identity,
)
from ..database import db
from ..diskspace import COORDINATE_ROW_BYTES, INGEST_ROW_BYTES, estimate_viz_rank_bytes, require_free_space
from ..field_registry import lock_field_lifecycle, map_swap_holder, map_swap_refusal
from ..map_artifacts import LocalArtifactStore
from ..request_limits import guest_heavy
from .auth import require_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/datasets/{id}/embeddings",
    tags=["embeddings"],
    dependencies=[Depends(require_dataset_access)],
)

# Safe field name pattern — alphanumeric and underscores only
_SAFE_FIELD_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
_DIRECT_FIELDS = frozenset(['filepath', 'tags', 'id', 'dataset_id', 'created_at'])


def _validate_field_name(fn: str) -> bool:
    return bool(_SAFE_FIELD_RE.match(fn))


async def _valid_sample_ids(dataset_id: int, sample_ids: list) -> set:
    """Return the subset of `sample_ids` that actually belong to `dataset_id`.

    Used by the embedding-write endpoints to reject any forged sample_id that
    points at another dataset (cross-dataset write protection). Legitimate
    callers only ever pass sample_ids from the dataset they're ingesting, so
    this is a no-op for them.
    """
    if not sample_ids:
        return set()
    rows = await db.fetch(
        "SELECT id FROM samples WHERE dataset_id = $1 AND id = ANY($2)",
        dataset_id,
        list({int(s) for s in sample_ids}),
    )
    return {r['id'] for r in rows}


_PATCH_SCALE_RE = re.compile(r'_patch_\d+x\d+$')
_PATCH_RC_RE = re.compile(r'_r(\d+)c(\d+)$')


def _is_patch_scale_field(field: str) -> bool:
    """Check if field is a patch scale virtual field like 'clip_patch_4x4' (not 'clip_patch_4x4_r0c0')."""
    return bool(_PATCH_SCALE_RE.search(field))


def _build_include_fields(
    include_fields: Optional[List[str]],
    table_prefix: str = "s",
    array_fields: Optional[Set[str]] = None,
):
    """Build qualified (s.col) and unqualified (col) select field lists from include_fields.

    array_fields: registered array-field COLUMN names for the dataset (field_type='array',
    e.g. inferred_tags). They're real samples columns, not metadata keys, so they must be
    selected as s.<col> — otherwise include/color-by returns NULL because
    metadata->>'<col>' misses the column entirely (the 2026-06-20 color-by bug)."""
    qualified = []
    unqualified = []
    if include_fields:
        for fn in include_fields:
            if '.' in fn:
                # Dotted path like "detections.label"
                # Handles both nested objects (metadata->'detections'->>'label')
                # and arrays of objects (metadata->'detections'->0->>'label')
                parts = fn.split('.', 1)
                if not all(_validate_field_name(p) for p in parts):
                    continue
                parent, child = parts
                alias = fn.replace('.', '_')
                qualified.append(
                    f"COALESCE("
                    f"{table_prefix}.metadata->'{parent}'->0->>'{child}', "
                    f"{table_prefix}.metadata->'{parent}'->>'{child}'"
                    f") as {alias}",
                )
                unqualified.append(alias)
            elif not _validate_field_name(fn):
                continue
            elif fn in _DIRECT_FIELDS or (array_fields is not None and fn in array_fields):
                qualified.append(f"{table_prefix}.{fn}")
                unqualified.append(fn)
            else:
                qualified.append(f"{table_prefix}.metadata->>'{fn}' as {fn}")
                unqualified.append(fn)
    return qualified, unqualified


async def _array_field_names(dataset_id: int) -> Set[str]:
    """Registered array-field column names for a dataset (field_type='array', e.g.
    inferred_tags). They're real samples columns, so include/color-by must read them
    as s.<col>, not metadata->>'<col>'."""
    from .datasets import get_dataset_array_columns

    return await get_dataset_array_columns(dataset_id)


class EmbeddingUpload(BaseModel):
    sample_id: int
    vector: List[float]
    field_name: str = "clip"


class EmbeddingsRequest(BaseModel):
    field: str = "clip"
    filters: Optional[Dict[str, Any]] = None
    include_fields: Optional[List[str]] = None
    sample_pct: int = 30  # 0-100, 0 or 100 = all


class ReduceRequest(BaseModel):
    field_name: str = "clip"
    n_neighbors: int = 15
    min_dist: float = 0.1
    # -1 = no cap (reduce ALL points). The SDK Dataset.compute_visualization
    # (float32 streaming + RAM-scaled fit) is the preferred path for very large
    # datasets; this server-side reduce fits on all selected rows in RAM.
    max_embeddings: int = -1


class RegionSelectRequest(BaseModel):
    field: str = "clip"
    filters: Optional[Dict[str, Any]] = None
    polygon: List[List[float]]  # [[x,y], [x,y], ...] in UMAP coordinates


class CoordinateUpdate(BaseModel):
    sample_id: int
    x: FiniteFloat
    y: FiniteFloat
    vector_version: Optional[str] = None


# --- Helper ---


def _build_where(id, field, filter_obj=None, array_columns=None, numeric_indexed_fields=None):
    """Build WHERE clause for embedding queries."""
    # Patch scale virtual fields (e.g. "clip_patch_4x4") match via LIKE
    if _is_patch_scale_field(field):
        conditions = ["s.dataset_id = $1", "e.field_name LIKE $2", "e.x IS NOT NULL"]
        args = [id, f"{field}_%"]
    else:
        conditions = ["s.dataset_id = $1", "e.field_name = $2", "e.x IS NOT NULL"]
        args = [id, field]
    arg_idx = 3

    if filter_obj:
        from ..routers.datasets import SAMPLES_JOIN_FIELD_MAPPING, parse_filter_node

        filter_sql, arg_idx = parse_filter_node(
            filter_obj,
            args,
            arg_idx,
            field_mapping=SAMPLES_JOIN_FIELD_MAPPING,
            numeric_indexed_fields=numeric_indexed_fields,
            dataset_id=id,
            array_columns=array_columns,
        )
        if filter_sql and filter_sql != "TRUE":
            conditions.append(filter_sql)

    return " AND ".join(conditions), args, arg_idx


def _parse_vector(vector_str) -> List[float]:
    """Parse a vector string like '[1.0,2.0,3.0]' into a list of floats."""
    if not vector_str:
        raise ValueError("Empty vector")
    return [float(x) for x in vector_str.strip('[]').split(',')]


def _patch_field_name(field_name: str, patch_scale: str, patch_row: int, patch_col: int) -> str:
    """Build the internal field_name for a patch embedding."""
    return f"{field_name}_patch_{patch_scale}_r{patch_row}c{patch_col}"


# --- Upload ---


class PatchEmbeddingUpload(BaseModel):
    sample_id: int
    vector: List[float]
    field_name: str = "clip"
    patch_scale: str  # e.g. "2x2", "4x4"
    patch_row: int = 0
    patch_col: int = 0

    @model_validator(mode='after')
    def validate_patch(self):
        if not _SAFE_FIELD_RE.match(self.field_name):
            raise ValueError(f"Invalid field_name: {self.field_name}")
        if not re.match(r'^\d+x\d+$', self.patch_scale):
            raise ValueError(f"Invalid patch_scale: {self.patch_scale}")
        rows, cols = (int(x) for x in self.patch_scale.split('x'))
        if self.patch_row < 0 or self.patch_row >= rows:
            raise ValueError(f"patch_row must be 0..{rows - 1}")
        if self.patch_col < 0 or self.patch_col >= cols:
            raise ValueError(f"patch_col must be 0..{cols - 1}")
        return self


async def validate_embedding_dims(field_dims: Dict[str, Set[int]]) -> None:
    """Reject an embedding write whose vector length disagrees with the field's established
    dimension, or is internally inconsistent within the request.

    Embedding dimension is a property of the field_name (the model that produced it) — search,
    concepts and quality all cast to `vector(D)` probed from an existing row, so a wrong-dim
    vector silently POISONS the field (query-time cast errors / garbage results) with no signal
    at ingest. A brand-new field is accepted and establishes the dimension. Raises 422 on a
    mismatch. field_dims maps field_name -> the set of vector lengths in this request.
    """
    for field_name, dims in field_dims.items():
        dims = {d for d in dims if d and d > 0}
        if not dims:
            continue
        if len(dims) > 1:
            raise HTTPException(
                status_code=422,
                detail=f"Inconsistent embedding dimensions for field '{field_name}' in one request: {sorted(dims)}",
            )
        incoming = next(iter(dims))
        established = await db.fetchval(
            "SELECT vector_dims(vector) FROM embeddings WHERE field_name = $1 AND vector IS NOT NULL LIMIT 1",
            field_name,
        )
        if established is not None and established != incoming:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Embedding dimension mismatch for field '{field_name}': got {incoming}, "
                    f"field is {established}-d. The dimension is fixed per field name across every "
                    f"dataset on this server, not per dataset — use a distinct field_name for a "
                    f"different model/dimension."
                ),
            )


@router.post("/patches")
async def upload_patch_embeddings(
    id: int,
    embeddings: List[PatchEmbeddingUpload],
    user: dict = Depends(require_role("power_user")),
):
    """Upload patch embeddings. The field_name encoding is handled internally —
    callers just provide sample_id, vector, scale, row, col."""
    submitted = len(embeddings)
    if not embeddings:
        return {"message": "No embeddings provided", "written": 0, "submitted": 0, "skipped": 0}

    # Reject the entire request if any sample is absent or belongs to another dataset.
    valid = await _valid_sample_ids(id, [e.sample_id for e in embeddings])
    rejected = sorted({e.sample_id for e in embeddings} - valid)
    if rejected:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Embedding sample IDs are absent or do not belong to this dataset",
                "unknown_sample_ids": rejected,
            },
        )

    values = []
    field_dims: Dict[str, Set[int]] = {}
    for e in embeddings:
        fn = _patch_field_name(e.field_name, e.patch_scale, e.patch_row, e.patch_col)
        field_dims.setdefault(fn, set()).add(len(e.vector))
        values.append((e.sample_id, str(e.vector), fn))
    await validate_embedding_dims(field_dims)
    async with db.pool.acquire() as conn:  # a full disk mid-ingest PANICs Postgres
        await require_free_space(conn, len(values) * INGEST_ROW_BYTES, f"adding {len(values)} patch embeddings")

    query = """
        INSERT INTO embeddings (sample_id, vector, field_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (sample_id, field_name)
        DO UPDATE SET vector = EXCLUDED.vector
    """

    try:
        await db.pool.executemany(query, values)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    return {
        "message": f"Uploaded {len(values)} patch embeddings",
        "written": len(values),
        "submitted": submitted,
        "skipped": submitted - len(values),
    }


@router.post("")
async def upload_embeddings(
    id: int,
    embeddings: List[EmbeddingUpload],
    user: dict = Depends(require_role("power_user")),
):
    submitted = len(embeddings)
    if not embeddings:
        return {"message": "No embeddings provided", "written": 0, "submitted": 0, "skipped": 0}

    # Only accept embeddings for samples that belong to THIS dataset.
    valid = await _valid_sample_ids(id, [e.sample_id for e in embeddings])
    rejected = sorted({e.sample_id for e in embeddings} - valid)
    if rejected:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Embedding sample IDs are absent or do not belong to this dataset",
                "unknown_sample_ids": rejected,
            },
        )

    values = []
    field_dims: Dict[str, Set[int]] = {}
    for e in embeddings:
        field_dims.setdefault(e.field_name, set()).add(len(e.vector))
        vector_str = str(e.vector)
        values.append((e.sample_id, vector_str, e.field_name))
    await validate_embedding_dims(field_dims)
    async with db.pool.acquire() as conn:  # a full disk mid-ingest PANICs Postgres
        await require_free_space(conn, len(values) * INGEST_ROW_BYTES, f"adding {len(values)} embeddings")

    # An identical re-send keeps its row version: no new tuple or HNSW entry, and a map re-fit that
    # exported this vector still places it.
    query = """
        INSERT INTO embeddings (sample_id, vector, field_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (sample_id, field_name)
        DO UPDATE SET vector = EXCLUDED.vector
        WHERE embeddings.vector IS DISTINCT FROM EXCLUDED.vector
    """

    try:
        async with db.pool.acquire() as conn, conn.transaction():
            # Shared, like sample ingest: a map re-fit swaps its coordinates in under the exclusive
            # gate, so no vector can change between its check and its commit.
            await lock_field_lifecycle(conn, id)
            # A changed vector removes its point's coordinate, which waits for the dataset's rank fence:
            # held by a map re-fit's re-rank for minutes. Refuse after a few seconds (409, retried by the
            # SDK until the re-fit's end) rather than hold the request and a pooled connection meanwhile.
            await conn.execute(f"SET LOCAL lock_timeout = '{WRITE_LOCK_SECONDS}s'")
            await conn.executemany(query, values)
    except HTTPException:
        raise
    except asyncpg.LockNotAvailableError:
        raise await lock_refusal(id)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    return {
        "message": f"Uploaded {len(values)} embeddings",
        "written": len(values),
        "submitted": submitted,
        "skipped": submitted - len(values),
    }


class FieldModelRegistration(BaseModel):
    field_name: str
    model: str  # HF checkpoint id or sentence-transformers name
    force: bool = False  # required to OVERWRITE a different existing registration


@router.post("/field-model")
async def register_field_model(
    id: int,
    body: FieldModelRegistration,
    user: dict = Depends(require_role("power_user")),
):
    """Record which checkpoint produced an embedding field.

    Field-GLOBAL by field_name (like dim validation and HNSW indexes) — the dataset in
    the path is only the access-check scope. Text search / text concepts consult this
    to pick the matching CLIP text tower and to refuse text-less models (DINO etc.).
    The SDK's compute_embeddings registers automatically; ingest scripts using their
    own models should call this once per field.
    """
    # The registry is field-GLOBAL, so a silent overwrite would repoint text search for
    # every dataset sharing the field (last-writer-wins). Refuse a conflicting change
    # unless the caller says force=true.
    existing = await db.fetchval("SELECT model FROM embedding_field_models WHERE field_name = $1", body.field_name)
    if existing is not None and existing != body.model and not body.force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Field '{body.field_name}' is already registered as {existing!r} (registry is "
                f"field-global across datasets). Pass force=true to overwrite, or use a distinct "
                f"field name for a different model."
            ),
        )
    await db.execute(
        "INSERT INTO embedding_field_models (field_name, model) VALUES ($1, $2) "
        "ON CONFLICT (field_name) DO UPDATE SET model = EXCLUDED.model, updated_at = CURRENT_TIMESTAMP",
        body.field_name,
        body.model,
    )
    return {"field_name": body.field_name, "model": body.model}


# --- Reduce (server-side UMAP — for SMALL fields only) ---

# Above this many vectors the in-process fit (held in the uvicorn worker) OOMs / drops the
# connection — large and patch-scale fields must use the SDK's client-side, RAM-scaled
# Dataset.compute_visualization instead. 4x4 patch grids (~100k tiles) stay under this; 8x8 (~400k+)
# correctly route to the SDK.
_MAX_IN_PROCESS_REDUCE = 200_000


@asynccontextmanager
async def coordinate_write(id, fields, model_id=None, *, projected=False, wait_for_rank=False):
    """Check permanent identity refusals before waiting, then recheck under publication's fences."""
    fields = sorted(set(fields))
    async with db.pool.acquire() as conn:
        try:
            async with conn.transaction():
                for field in fields:
                    state = await conn.fetchrow(
                        'SELECT model_id::text AS model_id FROM embedding_map_state '
                        'WHERE dataset_id=$1 AND field_name=$2',
                        id,
                        field,
                    )
                    check_map_identity(state, field, model_id, projected=projected)
                await conn.execute("SET LOCAL lock_timeout = '0'" if wait_for_rank else "SET LOCAL lock_timeout = '5s'")
                if wait_for_rank:
                    await conn.execute("SET LOCAL statement_timeout = '1800s'")
                await lock_field_lifecycle(conn, id, wait=wait_for_rank)
                for field in fields:
                    await check_map_fence(conn, id, field, model_id, projected=projected)
                yield conn
        except (MapRefitActive, MapModelChanged, MapManaged) as error:
            raise await map_write_refusal(id, conn, error, fields)
        except asyncpg.LockNotAvailableError:
            raise await lock_refusal(id, conn)
        except HTTPException as error:
            if error.status_code == 409:
                raise await lock_refusal(id, conn)
            raise


async def map_phase_holder(conn, id, fields):
    """Only this map's running publication/rank can give its fence a retry deadline."""
    own_phase = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM embedding_map_state m JOIN jobs j ON j.id=m.active_job_id
           WHERE m.dataset_id=$1 AND m.field_name=ANY($2::text[]) AND j.state='running'
             AND j.checkpoint->>'phase' IN ('publish','rank'))""",
        id,
        fields,
    )
    return await map_swap_holder(conn, id) if own_phase else None


async def map_write_refusal(id, conn, error, fields):
    if isinstance(error, MapManaged):
        code = 'map_managed'
    elif isinstance(error, MapModelChanged):
        code = 'map_model_changed'
    else:
        code = 'map_refit_active'
        holder = await map_phase_holder(conn, id, fields)
        if holder is not None:
            refusal = map_swap_refusal(*holder)
            return CodedHTTPException(
                status_code=409,
                detail=refusal.detail,
                code=f'map_{holder[1]}_busy',
                headers=refusal.headers,
            )
    return CodedHTTPException(status_code=409, detail=str(error), code=code)


@router.post("/reduce")
async def reduce_embeddings(id: int, req: ReduceRequest, user: dict = Depends(require_role("power_user"))):
    """Server-side UMAP reduction for SMALL fields. Fitting hundreds of thousands of vectors in the
    API worker OOMs/drops the connection, so this refuses oversized fits with a 413 pointing to the
    SDK's Dataset.compute_visualization (client-side, RAM-scaled) — the scalable path."""
    # Patch scale virtual fields use LIKE matching
    is_patch_scale = _is_patch_scale_field(req.field_name)
    if is_patch_scale:
        field_condition = "e.field_name LIKE $1"
        field_arg = f"{req.field_name}_%"
    else:
        field_condition = "e.field_name = $1"
        field_arg = req.field_name

    fields = [req.field_name]
    if is_patch_scale:
        # Virtual patch names write each matched tile's real field, so fence each tile.
        fields += [
            row['field_name']
            for row in await db.fetch(
                f"SELECT DISTINCT e.field_name FROM embeddings e JOIN samples s ON s.id=e.sample_id "
                f"WHERE {field_condition} AND s.dataset_id=$2",
                field_arg,
                id,
            )
        ]
    async with coordinate_write(id, fields):
        pass  # Refuse before loading vectors or fitting; check again when publishing below.

    # Count first and REFUSE oversized reductions rather than load every vector into the worker and
    # fit UMAP in-process. Explicit 413 (not a silent cap): the full reduction stays reachable via
    # the SDK. A caller may still opt into a bounded subset via max_embeddings.
    matched = await db.fetchval(
        f"SELECT COUNT(*) FROM embeddings e JOIN samples s ON e.sample_id = s.id "
        f"WHERE {field_condition} AND s.dataset_id = $2",
        field_arg,
        id,
    )
    fit_n = matched if req.max_embeddings <= 0 else min(matched, req.max_embeddings)
    if fit_n > _MAX_IN_PROCESS_REDUCE:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{fit_n} vectors exceeds the in-process reduce limit ({_MAX_IN_PROCESS_REDUCE}); "
                f"this would OOM the worker. Use the SDK "
                f"Dataset(name).compute_visualization(field_name='{req.field_name}') "
                f"(client-side, RAM-scaled), or pass a smaller max_embeddings."
            ),
        )

    limit_clause = "" if req.max_embeddings <= 0 else f"LIMIT {req.max_embeddings}"
    rows = await db.fetch(
        f"""
        SELECT e.id, e.vector, e.field_name
        FROM embeddings e
        JOIN samples s ON e.sample_id = s.id
        WHERE {field_condition} AND s.dataset_id = $2
        {limit_clause}
    """,
        field_arg,
        id,
    )

    if len(rows) < 5:
        raise HTTPException(status_code=400, detail="Not enough embeddings (need at least 5)")

    emb_ids = [r['id'] for r in rows]
    vectors = [_parse_vector(r['vector']) for r in rows]

    import asyncio

    import umap

    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=min(req.n_neighbors, len(vectors) - 1),
        min_dist=req.min_dist,
        metric='cosine',
        verbose=True,
    )
    # Run in thread pool so it doesn't block the event loop (30+ min on large datasets)
    coords = await asyncio.to_thread(reducer.fit_transform, np.array(vectors))

    # Write reduced coordinates separately from the vectors.
    update_values = [(float(x), float(y), emb_id) for emb_id, (x, y) in zip(emb_ids, coords)]
    write_fields = sorted(set(fields).union(r['field_name'] for r in rows))
    async with coordinate_write(id, write_fields, wait_for_rank=True) as conn:
        # The same x/y update /coordinates gates, then a full viz-rank rebuild (gated on its own
        # endpoint): a full disk mid-write PANICs Postgres.
        needed = len(update_values) * COORDINATE_ROW_BYTES
        if not is_patch_scale:
            needed += await estimate_viz_rank_bytes(conn, id, req.field_name)
        await require_free_space(conn, needed, f"saving {len(update_values)} reduced coordinates")
        await conn.execute(
            "INSERT INTO embedding_coordinates AS c (sample_id, field_name, x, y) "
            "SELECT e.sample_id, e.field_name, v.x, v.y FROM "
            "unnest($1::int[], $2::float8[], $3::float8[]) AS v(id, x, y) "
            "JOIN embeddings e ON e.id = v.id "
            "JOIN samples s ON s.id=e.sample_id WHERE s.dataset_id=$4 AND e.field_name=ANY($5::text[]) "
            "ON CONFLICT (sample_id, field_name) DO UPDATE SET x = EXCLUDED.x, y = EXCLUDED.y "
            "WHERE (c.x, c.y) IS DISTINCT FROM (EXCLUDED.x, EXCLUDED.y)",
            emb_ids,
            [v[0] for v in update_values],
            [v[1] for v in update_values],
            id,
            write_fields,
        )
        if not is_patch_scale:
            await conn.execute(
                'UPDATE embedding_viz_meta SET rank_stale=TRUE, occupied_cells=0 WHERE dataset_id=$1 AND field_name=$2',
                id,
                req.field_name,
            )

    # The coordinate triggers hold the rank fence shared. Commit before taking it exclusive,
    # otherwise concurrent reductions can deadlock while both upgrade that fence. A failed rank
    # leaves the committed layout explicitly stale. The automatic heal only covers managed maps,
    # so tell this caller to retry ranking without fitting and replacing the saved coordinates again.
    if not is_patch_scale:
        try:
            async with db.pool.acquire() as conn, conn.transaction():
                await conn.execute("SET LOCAL lock_timeout='0'; SET LOCAL statement_timeout='1800s'")
                await conn.fetchval('SELECT recompute_viz_rank($1, $2, TRUE)', id, req.field_name, timeout=1800)
        except Exception:
            logger.exception('Coordinates saved but ranking failed for dataset %s, field %s', id, req.field_name)
            raise CodedHTTPException(
                status_code=503,
                code='map_rank_failed',
                detail=(
                    'Coordinates saved; ranking failed. The map remains usable with slower queries. '
                    f'Retry ranking with POST /api/datasets/{id}/embeddings/recompute-viz-rank'
                    f'?field_name={req.field_name}&full=true; there is no need to repeat the reduction.'
                ),
            )

    # total_embeddings counts every vector of the field, so a max_embeddings cap that left some
    # without coordinates shows as reduced < total_embeddings instead of looking complete.
    return {"total_embeddings": matched, "reduced": len(emb_ids)}


# --- Query (visualization) ---


async def _run_grid_sampled_query(
    runner,
    from_clause: str,
    where_clause: str,
    args: List[Any],
    next_arg_idx: int,
    base_fields_q: List[str],
    extra_qualified: List[str],
    outer_clause: str,
    sample_pct: int,
):
    """Shared sampling pipeline used by both filtered and unfiltered paths.

    `runner` is anything with `.fetch/.fetchval/.fetchrow` (an asyncpg connection
    or the `db` singleton). `from_clause` and `where_clause` define the row set;
    everything else mirrors the original inline logic.

    This is the FALLBACK grid window-sort, used only when the field has no
    precomputed ranks (see _fast_rank_rows) — e.g. patch scatter, or before backfill.
    """
    total = await runner.fetchval(f"SELECT COUNT(*) FROM {from_clause} WHERE {where_clause}", *args)
    # "All" (pct>=100) returns every point — NO artificial cap (view-everything). The
    # sample-% selector (default 30%, options down to 5%) is the lever for weaker machines;
    # "All" is an explicit, reachable opt-in for capable ones.
    pct = max(0, min(100, sample_pct))
    max_points = total if pct == 0 or pct >= 100 or total <= 100000 else int(total * pct / 100)

    inner_select = ", ".join(base_fields_q + extra_qualified)

    if max_points >= total:
        rows = await runner.fetch(f"SELECT {inner_select} FROM {from_clause} WHERE {where_clause}", *args)
        return rows, total

    extent = await runner.fetchrow(
        f"SELECT MIN(e.x) AS xn, MAX(e.x) AS xx, MIN(e.y) AS yn, MAX(e.y) AS yx "
        f"FROM {from_clause} WHERE {where_clause}",
        *args,
    )
    if not extent or extent['xn'] is None:
        return None, 0

    xn, xx = float(extent['xn']), float(extent['xx'])
    yn, yx = float(extent['yn']), float(extent['yx'])
    xr = xx - xn or 1.0
    yr = yx - yn or 1.0
    grid = 200

    p_xn, p_xr, p_grid, p_grid_max = (
        f"${next_arg_idx}",
        f"${next_arg_idx + 1}",
        f"${next_arg_idx + 2}",
        f"${next_arg_idx + 3}",
    )
    p_yn, p_yr = f"${next_arg_idx + 4}", f"${next_arg_idx + 5}"
    grid_args = [xn, xr, grid, grid - 1, yn, yr]
    all_args = list(args) + grid_args

    grid_expr_x = f"LEAST(FLOOR((e.x - {p_xn}) / {p_xr} * {p_grid})::int, {p_grid_max})"
    grid_expr_y = f"LEAST(FLOOR((e.y - {p_yn}) / {p_yr} * {p_grid})::int, {p_grid_max})"

    occupied = await runner.fetchval(
        f"SELECT COUNT(*) FROM ("
        f"  SELECT DISTINCT {grid_expr_x}, {grid_expr_y} FROM {from_clause} WHERE {where_clause}"
        f") cells",
        *all_args,
    )
    per_cell = max(1, max_points // max(1, occupied))

    p_per_cell = f"${next_arg_idx + 6}"
    p_limit = f"${next_arg_idx + 7}"
    sample_args = all_args + [per_cell, max_points]

    rows = await runner.fetch(
        f"SELECT {outer_clause} FROM ("
        f"  SELECT {inner_select},"
        f"    ROW_NUMBER() OVER ("
        f"      PARTITION BY {grid_expr_x}, {grid_expr_y}"
        f"      ORDER BY random()"
        f"    ) AS rn"
        f"  FROM {from_clause} WHERE {where_clause}"
        f") sub WHERE sub.rn <= {p_per_cell} LIMIT {p_limit}",
        *sample_args,
    )
    return rows, total


async def _fast_rank_rows(
    runner,
    dataset_id: int,
    field: str,
    extra_qualified: List[str],
    sample_pct: int,
    meta,
    fids_join: bool,
):
    """Fast scatter sampler: read precomputed even-coverage ranks from
    embedding_viz_rank (WHERE dataset_id=? AND field_name=? AND rank < per_cell) — an
    index range scan, no window-sort. `fids_join` adds the per-request _fids temp table
    (filtered path, which already encodes the user filter); include_fields add a
    `samples s` join for the extra metadata. Returns (rows, total) with the same column
    shape (sample_id, x, y [, extras]) as the grid path.
    """
    select = ", ".join(["r.sample_id", "r.x", "r.y"] + extra_qualified)
    from_sql = "embedding_viz_rank r"
    if fids_join:
        from_sql += " JOIN _fids f ON r.sample_id = f.id"
    if extra_qualified:
        from_sql += " JOIN samples s ON r.sample_id = s.id"
    where = "r.dataset_id = $1 AND r.field_name = $2"
    args: List[Any] = [dataset_id, field]

    total = await runner.fetchval(f"SELECT COUNT(*) FROM {from_sql} WHERE {where}", *args)
    pct = max(0, min(100, sample_pct))
    max_points = total if pct == 0 or pct >= 100 or total <= 100000 else int(total * pct / 100)

    if max_points >= total:
        rows = await runner.fetch(f"SELECT {select} FROM {from_sql} WHERE {where}", *args)
        return rows, total

    if not fids_join:
        # The max_points smallest ranks across all cells: every cell gives its first k points
        # before any gives its (k+1)-th — the even coverage the per-cell quota below aims for, but
        # it fills the budget. `rank < ceil(budget / occupied)` under-filled wherever cells hold
        # fewer points than the quota: "5%" of main showed 3.2%. An
        # index-only scan in rank order (idx_embedding_viz_rank_scan): 182,761 rows in ~40 ms.
        rows = await runner.fetch(
            f"SELECT {select} FROM {from_sql} WHERE {where} ORDER BY r.rank LIMIT $3",
            *args,
            max_points,
        )
        return rows, total

    occupied = meta['occupied_cells']
    global_total = meta['total_points'] or total
    # per_cell = budget / occupied, scaled UP when a filter narrows the set (rank is
    # global) so the budget still fills; LIMIT caps the top. Ratio is 1.0 unfiltered.
    per_cell = max(1, math.ceil(max_points / occupied * max(1.0, global_total / max(1, total))))
    rows = await runner.fetch(
        f"SELECT {select} FROM {from_sql} WHERE {where} AND r.rank < $3 LIMIT $4",
        *args,
        per_cell,
        max_points,
    )
    return rows, total


async def _viz_meta(runner, dataset_id: int, field: str):
    """The embedding_viz_meta row for (dataset, field), or None if never ranked."""
    return await runner.fetchrow(
        "SELECT occupied_cells, total_points, rank_stale FROM embedding_viz_meta "
        "WHERE dataset_id = $1 AND field_name = $2",
        dataset_id,
        field,
    )


async def _query_embeddings_no_filter(
    id,
    request,
    base_fields_q,
    extra_qualified,
    outer_clause,
):
    """No filter / trivial filter. Uses precomputed ranks when available, else the
    grid window-sort fallback (patch fields, or before backfill)."""
    field = request.field
    if not _is_patch_scale_field(field):
        meta = await _viz_meta(db, id, field)
        if meta and meta['occupied_cells']:
            return await _fast_rank_rows(db, id, field, extra_qualified, request.sample_pct, meta, fids_join=False)
    where_clause, args, arg_idx = _build_where(id, field, None)
    from_clause = "embedding_coordinates e JOIN samples s ON e.sample_id = s.id"
    return await _run_grid_sampled_query(
        db,
        from_clause,
        where_clause,
        args,
        arg_idx,
        base_fields_q,
        extra_qualified,
        outer_clause,
        request.sample_pct,
    )


def _scatter_rejections(node, args):
    """Compile only conjunctions of known exclusions; everything else keeps _fids.

    Sample predicates share the normal compiler, including its NULL semantics.
    Concept/duplicate leaves read their narrow membership tables directly.
    """
    from .datasets import parse_filter_node

    sources = []
    probed = []  # index-only shapes of the PK-probed sources, for the admission bound
    sample_predicates = []
    concept_ids = set()
    has_tags = False

    def visit(part):
        nonlocal has_tags
        if 'logic' in part or 'filters' in part:
            logic = part.get('logic', 'AND').upper()
            children = part.get('filters', [])
            if logic == 'AND':
                return all(visit(child) for child in children)
            if logic == 'NOT' and len(children) == 1:
                child = children[0]
                if child.get('field') == 'tags' and child.get('operator', '=').lower() in ('in', 'contains'):
                    sql, _ = parse_filter_node(child, args, len(args) + 1)
                    sample_predicates.append(sql)
                    has_tags = True
                    return True
            return False
        op = part.get('operator', '=').lower()
        value = part.get('value')
        if 'field' not in part:
            return False
        if op == 'concept_exclude':
            cfg = value if isinstance(value, dict) else {}
            if cfg.get('concept_id') is None:
                return True
            # The normal filter is compiled first and validates the comparison.
            cmp = str(cfg.get('op', '>'))
            if cmp not in ('>', '>=', '<', '<='):
                return False
            idx = len(args) + 1
            args.extend([int(cfg['concept_id']), float(cfg.get('threshold', 0.5))])
            concept_ids.add(int(cfg['concept_id']))
            sources.append(
                f"SELECT sample_id AS id FROM embedding_concept_scores "
                f"WHERE concept_id = ${idx} AND score {cmp} ${idx + 1}",
            )
            probed.append(
                f"SELECT 1 FROM embedding_concept_scores WHERE concept_id = ${idx} AND score {cmp} ${idx + 1}",
            )
            return True
        if op == 'collapse_duplicates':
            if value:
                args.append(value)
                sources.append(
                    f"SELECT DISTINCT sample_id AS id FROM sample_dup_secondary "
                    f"WHERE dataset_id = $1 AND hash_config = ${len(args)}",
                )
                probed.append(
                    f"SELECT 1 FROM sample_dup_secondary WHERE dataset_id = $1 AND hash_config = ${len(args)}",
                )
            return True
        positive = None
        if part['field'] == 'media_type' and op == '!=' and value == 'object':
            positive = {**part, 'operator': '='}
        elif part['field'] == 'tags' and op == 'not in':
            has_tags = True
            positive = {**part, 'operator': 'in'}
        if positive is None:
            return False
        sql, _ = parse_filter_node(positive, args, len(args) + 1)
        sample_predicates.append(sql)
        return True

    if not visit(node):
        return None
    if sample_predicates:
        sources.append('SELECT id FROM samples WHERE dataset_id = $1 AND (' + ' OR '.join(sample_predicates) + ')')
    if not sources:
        return None
    # UNION makes overlapping rejections unique. The final rank membership lookup
    # scopes every source to the requested dataset/field and excludes missing coords.
    rejected = ' UNION '.join(sources)
    if concept_ids:
        # One guard for every concept, outside the sources (as parse_filter_node places it).
        args.append(sorted(concept_ids))
        rejected = f"SELECT id FROM ({rejected}) _rejected WHERE (SELECT require_concept_scores(${len(args)}::int[]))"
    return rejected, has_tags, probed


# Probing the rank primary key once per concept/duplicate rejection only beats the general
# path while those rejections are a modest share of the field. The admission threshold
# limits random index probes; larger sources go straight to the general path.
PROBED_REJECTION_SHARE = 0.30


async def _query_broad_exclusions(dataset_id, request):
    if _is_patch_scale_field(request.field) or request.include_fields:
        return None
    args: List[Any] = [dataset_id, request.field]
    compiled = _scatter_rejections(request.filters, args)
    if compiled is None:
        return None
    rejected, has_tags, probed = compiled
    pct = max(0, min(100, request.sample_pct))
    # Admission: a bounded index-only count of each probed source, before any probe runs. The
    # sum bounds the probes (overlaps count twice, which only makes the bound stricter). Over
    # the cap, the meta row is filtered out, nothing below runs, and the caller falls back.
    admission = ''
    admitted = ''
    if probed:
        bound = ' + '.join(
            f'(SELECT count(*) FROM ({source} LIMIT (SELECT n + 1 FROM cap)) c{i})' for i, source in enumerate(probed)
        )
        admission = f"""cap AS MATERIALIZED (
            SELECT floor(total_points * {PROBED_REJECTION_SHARE})::bigint AS n
            FROM embedding_viz_meta WHERE dataset_id = $1 AND field_name = $2
        ), admitted AS MATERIALIZED (SELECT {bound} <= n AS ok FROM cap),
        """
        admitted = ' AND (SELECT ok FROM admitted)'
    # Narrow membership sources benefit from PK probes; tag rejection can be much
    # larger, so leave its join strategy to the planner (as in the measured queries).
    membership_offset = '' if has_tags else ' OFFSET 0'
    # Stop small-source PK probes once a majority is rejected. A LIMIT biases
    # tag membership toward nested loops, so let its larger join complete before
    # assessing selectivity. Only the broad branch consumes the total below.
    membership_limit = (
        ''
        if has_tags
        else ('LIMIT (SELECT total_points / 2 + 1 FROM embedding_viz_meta WHERE dataset_id = $1 AND field_name = $2)')
    )
    rows = await db.fetch(
        f"""WITH {admission}rejected AS MATERIALIZED ({rejected}),
        excluded AS MATERIALIZED (
            SELECT j.id FROM rejected j CROSS JOIN LATERAL (
                SELECT 1 FROM embedding_viz_rank r
                WHERE r.dataset_id = $1 AND r.field_name = $2 AND r.sample_id = j.id{membership_offset}
            ) matched
            {membership_limit}
        ), meta AS MATERIALIZED (
            SELECT total_points, occupied_cells, total_points - (SELECT count(*) FROM excluded) AS total
            FROM embedding_viz_meta WHERE dataset_id = $1 AND field_name = $2 AND occupied_cells > 0{admitted}
        ), budget AS MATERIALIZED (
            SELECT *, CASE WHEN {pct} IN (0, 100) OR total <= 100000 THEN total
                           ELSE floor(total::double precision * {pct} / 100)::bigint END AS max_points
            FROM meta
        ), points AS (
            -- OFFSET 0 keeps the covering rank-range scan; the anti-join (not NOT IN, which
            -- degrades to a per-row rescan when the ids exceed work_mem) is applied on top.
            SELECT r.sample_id, r.x, r.y FROM (
                SELECT r.sample_id, r.x, r.y FROM embedding_viz_rank r
                WHERE r.dataset_id = $1 AND r.field_name = $2
                  AND (SELECT total * 2 >= total_points FROM budget)
                  AND r.rank <= (
                      SELECT (CASE WHEN max_points >= total THEN 2147483647 ELSE
                          greatest(1, ceil(max_points::double precision / occupied_cells *
                              greatest(1.0, total_points::double precision / greatest(1, total)))) - 1 END)::integer
                      FROM budget
                  )
                OFFSET 0
            ) r
            WHERE NOT EXISTS (SELECT 1 FROM excluded x WHERE x.id = r.sample_id)
            LIMIT (SELECT max_points + 1 FROM budget)
        )
        SELECT NULL::integer AS sample_id, NULL::real AS x, NULL::real AS y,
               total, max_points, total * 2 >= total_points AS broad FROM budget
        UNION ALL SELECT sample_id, x, y, NULL::bigint, NULL::bigint, NULL::boolean FROM points""",
        *args,
    )
    if not rows or rows[0]['sample_id'] is not None or not rows[0]['broad']:
        return None
    # The old filtered LIMIT has no ORDER BY. If the quota overfills, a different
    # plan could pick a different subset; retain that path for saturated quotas.
    if len(rows) - 1 > rows[0]['max_points']:
        return None
    return rows[1:], rows[0]['total']


async def _query_embeddings_with_filter(
    id,
    request,
    base_fields_q,
    extra_qualified,
    outer_clause,
    needs_samples_join,
    filter_sql,
    filter_args,
):
    """Filtered path: materialize matching sample IDs into a temp table once,
    then run all sub-queries against it. Avoids re-scanning the heap per
    sub-query (4× speedup on JSONB-key filters)."""
    broad = await _query_broad_exclusions(id, request)
    if broad is not None:
        return broad

    # Embedding-side conditions only — sample filter lives in the temp table
    if _is_patch_scale_field(request.field):
        e_cond = "e.field_name LIKE $1 AND e.x IS NOT NULL"
        e_args: List[Any] = [f"{request.field}_%"]
    else:
        e_cond = "e.field_name = $1 AND e.x IS NOT NULL"
        e_args = [request.field]

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            # Materialize filtered sample IDs. $1 is dataset_id, filter_sql
            # references $2..$N (we constructed it with arg_idx=2 above).
            await conn.execute(
                f"CREATE TEMP TABLE _fids ON COMMIT DROP AS "
                f"SELECT id FROM samples WHERE dataset_id = $1 AND {filter_sql}",
                id,
                *filter_args,
            )
            await conn.execute("CREATE INDEX ON _fids(id)")
            await conn.execute("ANALYZE _fids")

            # Fast path: precomputed ranks joined to the filtered _fids set.
            if not _is_patch_scale_field(request.field):
                meta = await _viz_meta(conn, id, request.field)
                if meta and meta['occupied_cells']:
                    return await _fast_rank_rows(
                        conn,
                        id,
                        request.field,
                        extra_qualified,
                        request.sample_pct,
                        meta,
                        fids_join=True,
                    )

            from_clause = "embedding_coordinates e JOIN _fids f ON e.sample_id = f.id"
            if needs_samples_join:
                from_clause += " JOIN samples s ON e.sample_id = s.id"

            return await _run_grid_sampled_query(
                conn,
                from_clause,
                e_cond,
                e_args,
                len(e_args) + 1,
                base_fields_q,
                extra_qualified,
                outer_clause,
                request.sample_pct,
            )


def _embeddings_binary_response(rows, total: int, rank_stale: bool = False, map_state=None) -> Response:
    """Pack base scatter coords as a columnar binary payload (Part 2): a 4-byte LE
    header length, a JSON header (space-padded to a 4-byte boundary), then
    int32[count] sample_id, float32[count] x, float32[count] y. ~½ the bytes of JSON
    and no big-array JSON.parse on the client — the typed arrays drop straight into
    the scatter's Float32Array storage. Base coords only (no include_fields / patches).
    """
    n = len(rows)
    ids = np.fromiter((r['sample_id'] for r in rows), dtype='<i4', count=n)
    xs = np.fromiter((r['x'] for r in rows), dtype='<f4', count=n)
    ys = np.fromiter((r['y'] for r in rows), dtype='<f4', count=n)
    header = json.dumps(
        {
            "count": n,
            "total": total,
            "sampled": n < total,
            "rank_stale": rank_stale,
            "map_state": map_state,
            "fields": ["sample_id", "x", "y"],
        },
    ).encode("utf-8")
    header += b" " * ((-len(header)) % 4)  # pad so the columns start 4-byte aligned
    body = struct.pack("<I", len(header)) + header + ids.tobytes() + xs.tobytes() + ys.tobytes()
    return Response(content=body, media_type="application/octet-stream")


async def _map_status(dataset_id: int, field: str):
    return await db.fetchrow(
        """SELECT (SELECT rank_stale FROM embedding_viz_meta WHERE dataset_id = $1 AND field_name = $2) AS rank_stale,
                  m.model_id, m.refitted_at
           FROM (SELECT 1) one LEFT JOIN embedding_map_state m ON m.dataset_id = $1 AND m.field_name = $2""",
        dataset_id,
        field,
    )


@router.post("/query")
@guest_heavy
async def get_embeddings_post(id: int, request: EmbeddingsRequest, format: str = "json"):
    """Get embeddings for scatter plot visualization.

    For large datasets, samples points (precomputed even-coverage ranks from
    embedding_viz_rank when available, else grid-stratified) to bound the client cost
    while keeping spatial coverage of the UMAP space, including sparse outlier regions.
    Use /select-region for lasso selection which queries ALL points.

    `format=bin` returns a columnar binary payload (base coords only) — see
    _embeddings_binary_response. Falls back to JSON for patch/include_fields requests.
    """
    # Build SELECT — qualified for direct JOIN, unqualified for subquery wrapper
    array_fields = await _array_field_names(id) if request.include_fields else None
    extra_qualified, extra_unqualified = _build_include_fields(request.include_fields, array_fields=array_fields)
    is_patch = _is_patch_scale_field(request.field)
    base_fields_q = ["e.sample_id", "e.x", "e.y"] + (["e.field_name"] if is_patch else [])
    base_fields_u = ["sample_id", "x", "y"] + (["field_name"] if is_patch else [])
    outer_clause = ", ".join(base_fields_u + extra_unqualified)
    needs_samples_join = bool(extra_qualified)

    # When explicit filters are present, the sample filter is run 4× per
    # request (count, extent, occupied cells, sample). On large filtered
    # row sets the planner picks a nested-loop join with a wildly low row
    # estimate — performance collapses. We materialize matching sample IDs
    # into a per-request temp table once so all sub-queries hash-join
    # against a small set with accurate cardinality.
    use_temp = request.filters is not None

    if use_temp:
        # Build sample-side filter SQL (without dataset_id; we add it explicitly)
        from ..routers.datasets import get_promoted_numeric_fields, parse_filter_node

        filter_args: List[Any] = []
        assert request.filters is not None  # use_temp ⇒ filters present (set just above)
        # Pass dataset_id so cross-grain ops (cg_self/parent_matches/has_matching_object, sent by
        # the grain-wrapped scatter filter) scope their subquery to this dataset. The temp-table
        # query is `FROM samples` (no alias), so the ops' default `samples.*` references resolve.
        filter_sql, _ = parse_filter_node(
            request.filters,
            filter_args,
            2,
            None,
            await get_promoted_numeric_fields(id),
            id,
            array_columns=await _array_field_names(id),
        )
        # parse_filter_node may return TRUE for empty/no-op filters; treat as no filter
        if not filter_sql or filter_sql == "TRUE":
            use_temp = False

    # A map re-fit publishes all of a map's points in one transaction, and each query below reads one
    # snapshot. Reading the map's identity before and after makes the points and the map_state they are
    # reported with agree: if a re-fit was published in between, read the points again.
    before = await _map_status(id, request.field)
    for _ in range(2):
        if not use_temp:
            # Fast path: no explicit filters. Filter on dataset_id directly via
            # the embeddings → samples join.
            rows, total = await _query_embeddings_no_filter(
                id,
                request,
                base_fields_q,
                extra_qualified,
                outer_clause,
            )
        else:
            rows, total = await _query_embeddings_with_filter(
                id,
                request,
                base_fields_q,
                extra_qualified,
                outer_clause,
                needs_samples_join,
                filter_sql,
                filter_args,
            )
        status = await _map_status(id, request.field)
        if status['model_id'] == before['model_id']:
            break
        before = status

    if rows is None:
        rows, total = [], 0

    # Coverage-rank staleness: x/y are always current (the embeddings
    # coordinate-table trigger keeps the rank table in sync), but the even-coverage ORDER may
    # predate a re-projection until the next full recompute_viz_rank.
    rank_stale = bool(status['rank_stale'])
    map_state = map_state_summary(status)

    # Binary fast path: base coords only (no include_fields, no patch metadata).
    if format == "bin" and not request.include_fields and not is_patch:
        return _embeddings_binary_response(rows, total, rank_stale, map_state)

    results = []
    for row in rows:
        r = {"sample_id": row['sample_id'], "x": row['x'], "y": row['y']}
        if is_patch and 'field_name' in row:
            r['field_name'] = row['field_name']
            # Extract position label for color-by (e.g. "r2c3" from "clip_patch_4x4_r2c3")
            m = _PATCH_RC_RE.search(row['field_name'])
            if m:
                r['patch_position'] = f"r{m.group(1)}c{m.group(2)}"
        if request.include_fields:
            for fn in request.include_fields:
                # Dotted fields are aliased: "detections.label" → "detections_label"
                alias = fn.replace('.', '_') if '.' in fn else fn
                if alias in row:
                    r[alias] = row[alias]
                elif fn in row:
                    r[fn] = row[fn]
        results.append(r)

    return {
        "points": results,
        "total": total,
        "sampled": len(results) < total,
        "rank_stale": rank_stale,
        "map_state": map_state,
    }


# --- Region selection (full dataset, not sampled) ---


@router.post("/select-region")
@guest_heavy
async def select_region(id: int, request: RegionSelectRequest):
    """Return ALL sample IDs within a UMAP polygon.

    Uses bounding box as a fast pre-filter, then exact point-in-polygon
    test on the results. Queries the full dataset, not just the sampled
    visualization points.
    """
    poly = request.polygon
    if len(poly) < 3:
        return {"token": None, "total": 0}

    # Bounding box pre-filter
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    bbox_xmin, bbox_xmax = min(xs), max(xs)
    bbox_ymin, bbox_ymax = min(ys), max(ys)

    from .datasets import get_promoted_numeric_fields

    where_clause, args, arg_idx = _build_where(
        id,
        request.field,
        request.filters,
        await _array_field_names(id),
        await get_promoted_numeric_fields(id) if request.filters else None,
    )
    args.extend([bbox_xmin, bbox_xmax, bbox_ymin, bbox_ymax])
    bbox = f"e.x >= ${arg_idx} AND e.x <= ${arg_idx + 1} AND e.y >= ${arg_idx + 2} AND e.y <= ${arg_idx + 3}"

    is_patch = _is_patch_scale_field(request.field)
    select_fields = "e.sample_id, e.x, e.y" + (", e.field_name" if is_patch else "")

    rows = await db.fetch(
        f"""
        SELECT {select_fields} FROM embedding_coordinates e
        JOIN samples s ON e.sample_id = s.id
        WHERE {where_clause} AND {bbox}
    """,
        *args,
    )

    # Point-in-polygon test (ray casting)
    def point_in_polygon(px: float, py: float) -> bool:
        n = len(poly)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    selected = [r for r in rows if point_in_polygon(r['x'], r['y'])]
    sample_ids = list(set(r['sample_id'] for r in selected))

    # The embeddings lasso now filters via the live `embedding_polygon` operator
    # (re-evaluated per query, ANDed with the other filters), so we no longer
    # materialize the selection into region_selections — that token was unused and a
    # big lasso wrote millions of throwaway rows. Return the total (+ patches for
    # patch fields) only; `token` stays for response-shape compatibility.
    result: Dict[str, Any] = {"token": None, "total": len(sample_ids)}
    if is_patch:
        result["patches"] = [{"sample_id": r['sample_id'], "field_name": r['field_name']} for r in selected]
    return result


# --- Lookup coordinates for specific sample IDs ---


class CoordinateLookupRequest(BaseModel):
    field: str = "clip"
    sample_ids: List[int]
    include_fields: Optional[List[str]] = None


@router.post("/lookup")
@guest_heavy
async def lookup_coordinates(id: int, request: CoordinateLookupRequest):
    """Get UMAP coordinates for specific sample IDs.

    Used to highlight grid-selected samples on the embeddings panel even
    when they aren't in the sampled visualization points.
    """
    if not request.sample_ids:
        return {"points": []}

    array_fields = await _array_field_names(id) if request.include_fields else None
    extra_qualified, extra_unqualified = _build_include_fields(request.include_fields, array_fields=array_fields)
    select_clause = ", ".join(["e.sample_id", "e.x", "e.y"] + extra_qualified)

    # Scope to THIS dataset so a foreign sample_id can't leak coordinates/metadata
    # from a dataset the caller has no access to (the path dataset access was
    # already verified by require_dataset_access).
    if extra_qualified:
        rows = await db.fetch(
            f"""
            SELECT {select_clause}
            FROM embedding_coordinates e
            JOIN samples s ON e.sample_id = s.id
            WHERE e.field_name = $1 AND e.sample_id = ANY($2) AND e.x IS NOT NULL
              AND s.dataset_id = $3
        """,
            request.field,
            request.sample_ids,
            id,
        )
    else:
        rows = await db.fetch(
            """
            SELECT e.sample_id, e.x, e.y
            FROM embedding_coordinates e
            JOIN samples s ON e.sample_id = s.id
            WHERE e.field_name = $1 AND e.sample_id = ANY($2) AND e.x IS NOT NULL
              AND s.dataset_id = $3
        """,
            request.field,
            request.sample_ids,
            id,
        )

    results = []
    for r in rows:
        point = {"sample_id": r['sample_id'], "x": r['x'], "y": r['y']}
        for fn in extra_unqualified:
            if fn in r:
                point[fn] = r[fn]
        results.append(point)

    return {"points": results}


class PatchLookupRequest(BaseModel):
    """Lookup coordinates for specific (sample_id, field_name) pairs."""

    patches: List[Dict[str, Any]]  # [{sample_id: int, field_name: str}, ...]


@router.post("/lookup-patches")
@guest_heavy
async def lookup_patch_coordinates(id: int, request: PatchLookupRequest):
    """Get UMAP coordinates for specific patch embeddings by field_name."""
    if not request.patches:
        return {"points": []}

    # Extract unique field_names and sample_ids
    sample_ids = list(set(p['sample_id'] for p in request.patches if 'sample_id' in p))
    field_names = list(set(p['field_name'] for p in request.patches if 'field_name' in p))

    if not sample_ids or not field_names:
        return {"points": []}

    rows = await db.fetch(
        """
        SELECT e.sample_id, e.x, e.y, e.field_name
        FROM embedding_coordinates e
        JOIN samples s ON e.sample_id = s.id
        WHERE e.sample_id = ANY($1) AND e.field_name = ANY($2) AND e.x IS NOT NULL
          AND s.dataset_id = $3
    """,
        sample_ids,
        field_names,
        id,
    )

    # Filter to only the requested pairs
    requested = set(
        (p['sample_id'], p['field_name']) for p in request.patches if 'sample_id' in p and 'field_name' in p
    )
    points = []
    for row in rows:
        if (row['sample_id'], row['field_name']) not in requested:
            continue
        r = {"sample_id": row['sample_id'], "x": row['x'], "y": row['y'], "field_name": row['field_name']}
        m = _PATCH_RC_RE.search(row['field_name'])
        if m:
            r['patch_position'] = f"r{m.group(1)}c{m.group(2)}"
        points.append(r)

    return {"points": points}


# --- GET endpoint (legacy) ---


@router.get("")
@guest_heavy
async def get_embeddings(
    id: int,
    field: str = "clip",
    filters: Optional[str] = None,
    include_fields: Optional[str] = None,
):
    filter_obj = json.loads(filters) if filters else None
    include_fields_list = [f.strip() for f in include_fields.split(",") if f.strip()] if include_fields else None
    req = EmbeddingsRequest(field=field, filters=filter_obj, include_fields=include_fields_list)
    return await get_embeddings_post(id, req)


def _split_embedding_fields(names) -> list:
    """Split raw embedding field_names into [whole-image fields] + [patch SCALE
    aggregates] (e.g. 'clip_patch_4x4_r0c0' → 'clip_patch_4x4'), both sorted."""
    whole, scales = [], set()
    for fn in names:
        if '_patch_' in fn:
            m = _PATCH_RC_RE.search(fn)
            if m:
                scales.add(fn[: m.start()])
        else:
            whole.append(fn)
    return sorted(whole) + sorted(scales)


@router.get("/field-inventory")
async def get_embedding_field_inventory(id: int):
    """EVERY embedding field name stored for this dataset — the raw truth, unaggregated.

    Distinct from `/fields`, which answers a different question: "what can the scatter show?"
    That one reads embedding_viz_meta (only fields with computed COORDS) and collapses patch
    fields into pseudo-scale names like `clip_patch_4x4` — names no row actually has.

    Anything doing data movement or auditing needs this instead. `sync_between_instances` used
    `/fields` and therefore silently skipped whole embedded-but-never-projected fields (a
    dinov3 field with millions of vectors would just not be copied, under a "✅ Sync complete"), and
    asked for the pseudo-names, which match no row and returned 0 vectors.

    Returns real `field_name` values with per-field counts, so a caller can verify completeness.
    """
    rows = await db.fetch(
        """
        SELECT e.field_name, count(*) AS count
        FROM embeddings e
        JOIN samples s ON e.sample_id = s.id
        WHERE s.dataset_id = $1
        GROUP BY e.field_name
        ORDER BY e.field_name
        """,
        id,
    )
    return [{"field_name": r['field_name'], "count": r['count']} for r in rows]


@router.get("/fields")
async def get_embedding_fields(id: int):
    """Embedding fields available to visualize (those with computed coords).

    Sourced from embedding_viz_meta — a tiny per-(dataset, field) registry maintained by
    recompute_viz_rank on every projection — so this is an instant PK lookup instead of a
    DISTINCT scan over millions of embedding rows (which was ~2.4s PER query and gated the
    whole scatter on cold load). Falls back to the scan only for a dataset that has never
    been ranked (e.g. coords uploaded without a recompute), which then self-heals on the
    next projection.
    """
    meta_rows = await db.fetch(
        "SELECT field_name FROM embedding_viz_meta WHERE dataset_id = $1",
        id,
    )
    if meta_rows:
        names = [r['field_name'] for r in meta_rows]
        # Patch fields store their 2D coords in embedding_coordinates.x/y and intentionally skip
        # viz_meta (a patch SCALE has no single field to rank — see reduce_embeddings), so
        # they're absent above. Add the reduced patch fields so their scales are selectable
        # in the scatter dropdown — same LIKE scan /patch-scales uses for the grid dropdown.
        patch_rows = await db.fetch(
            """
            SELECT DISTINCT e.field_name FROM embedding_coordinates e
            JOIN samples s ON e.sample_id = s.id
            WHERE s.dataset_id = $1 AND e.field_name LIKE '%patch%' AND e.x IS NOT NULL
            """,
            id,
        )
        names.extend(r['field_name'] for r in patch_rows)
        return _split_embedding_fields(names)

    # Fallback: never-ranked dataset — DISTINCT scan of its embedding field names (fields with
    # vectors, projected or not: the SDK's get_embedding_fields relies on this).
    rows = await db.fetch(
        """
        SELECT DISTINCT field_name FROM embeddings
        JOIN samples s ON embeddings.sample_id = s.id
        WHERE s.dataset_id = $1
    """,
        id,
    )
    return _split_embedding_fields(r['field_name'] for r in rows)


@router.get("/patch-scales")
async def get_patch_scales(id: int, field_name: str = "clip"):
    """Return available patch scales for a given embedding field.

    Patch embeddings use field_name convention: "{field_name}_patch_{scale}_r{row}c{col}"
    This endpoint extracts distinct scales from the field_name pattern.
    """
    pattern = f"{field_name}_patch_%"
    rows = await db.fetch(
        """
        SELECT DISTINCT e.field_name
        FROM embeddings e
        JOIN samples s ON e.sample_id = s.id
        WHERE s.dataset_id = $1 AND e.field_name LIKE $2
    """,
        id,
        pattern,
    )

    # Parse scales from field names like "clip_patch_4x4_r0c0"
    scales = set()
    prefix = f"{field_name}_patch_"
    for r in rows:
        fn = r['field_name']
        rest = fn[len(prefix) :]  # "4x4_r0c0"
        scale = rest.split('_')[0]  # "4x4"
        scales.add(scale)

    return [{"scale": s} for s in sorted(scales)]


# --- Raw vectors (for SDK UMAP computation) ---


# Upper bound on ONE page of /vectors. This bounds the page, not what is reachable: keyset
# paging (after_id) still walks every vector in the field. Before it existed, `limit` was
# completely unvalidated, so any caller with dataset access could ask for an arbitrarily large
# page and scale the per-request CPU and memory cost with it.
_MAX_VECTOR_PAGE = 50_000


def _vectors_to_matrix(rows) -> tuple[np.ndarray, np.ndarray]:
    """Parse a /vectors page into (int32 ids, float32 [n, dim] matrix).

    pgvector comes back from asyncpg as TEXT, so this is the unavoidable parse. `np.fromstring`
    with a separator is the text-mode form (only the binary mode was deprecated) and measured
    ~35% faster than the float() list comprehension it replaces. Runs in a worker thread — see
    the callers.
    """
    n = len(rows)
    ids = np.fromiter((r['sample_id'] for r in rows), dtype='<i4', count=n)
    if n == 0:
        return ids, np.empty((0, 0), dtype='<f4')

    first = np.fromstring(rows[0]['vector'].strip('[]'), dtype='<f4', sep=',')
    dim = int(first.shape[0])
    mat = np.empty((n, dim), dtype='<f4')
    mat[0] = first
    for i in range(1, n):
        v = np.fromstring(rows[i]['vector'].strip('[]'), dtype='<f4', sep=',')
        if v.shape[0] != dim:
            # A rectangular payload can't represent a ragged field. Fail loudly naming the row
            # rather than silently truncating/padding — a mixed-dimension field is itself a bug
            # (wrong-dim upload), and the JSON format can still read it.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Field has mixed vector dimensions: sample {rows[i]['sample_id']} is "
                    f"{v.shape[0]}-d but the page starts {dim}-d. Re-request with format=json, "
                    f"and fix the field — something uploaded the wrong dimension."
                ),
            )
        mat[i] = v
    return ids, mat


def _vectors_binary_response(rows, total: Optional[int]) -> Response:
    """Columnar binary page: 4-byte LE header length, JSON header, int32[n] ids, float32[n*dim].

    Same envelope as _embeddings_binary_response. This exists because the JSON form of a 10k x
    1024-d page is a ~208MB string that costs seconds to encode; here the bytes are already the
    client's numpy buffer.
    """
    ids, mat = _vectors_to_matrix(rows)
    header = json.dumps(
        {
            "count": int(ids.shape[0]),
            "total": total,
            "dim": int(mat.shape[1]),
            "fields": ["sample_id", "vector"],
            **(
                {"vector_versions": [r['vector_version'] for r in rows]} if rows and 'vector_version' in rows[0] else {}
            ),
        },
    ).encode("utf-8")
    header += b" " * ((-len(header)) % 4)  # pad so the columns start 4-byte aligned
    body = struct.pack("<I", len(header)) + header + ids.tobytes() + mat.tobytes()
    return Response(content=body, media_type="application/octet-stream")


def _vectors_json_response(rows, total: Optional[int]) -> Response:
    """The legacy {"items": [...], "total": n} shape, pre-rendered.

    Returning a Response rather than a dict skips FastAPI's jsonable_encoder pass over every
    vector (the route has no response_model, so it would otherwise fall through to it).

    Rendering in a thread reduces work on the event loop, but json.dumps still holds
    the GIL. The binary format avoids that serialization cost; this path is retained
    for older clients. Compact separators preserve JSONResponse's payload size.
    """
    items = [{"sample_id": r['sample_id'], "vector": _parse_vector(r['vector'])} for r in rows]
    for item, row in zip(items, rows):
        if 'vector_version' in row:
            item['vector_version'] = row['vector_version']
    return Response(
        content=json.dumps({"items": items, "total": total}, separators=(",", ":")),
        media_type="application/json",
    )


@router.get("/vectors")
@guest_heavy
async def get_vectors(
    id: int,
    field_name: str = "clip",
    limit: int = Query(10000, ge=1, le=_MAX_VECTOR_PAGE),
    offset: int = 0,
    after_id: Optional[int] = None,
    pending_only: bool = False,
    response_format: str = Query("json", alias="format", pattern="^(json|binary)$"),
):
    # Pending requests start at the small dataset/field queue, never at embeddings.
    if pending_only:
        source = (
            "embedding_projection_pending p JOIN embeddings e "
            "ON e.sample_id = p.sample_id AND e.field_name = p.field_name"
        )
        scope = "p.field_name = $1 AND p.dataset_id = $2"
        cursor = "p.sample_id"
    else:
        source = "embeddings e JOIN samples s ON e.sample_id = s.id"
        scope = "e.field_name = $1 AND s.dataset_id = $2"
        cursor = "e.sample_id"
    columns = "e.sample_id, e.vector" + (", e.xmin::text AS vector_version" if pending_only else "")
    if after_id is not None:
        rows = await db.fetch(
            f"SELECT {columns} FROM {source} WHERE {scope} AND {cursor} > $3 ORDER BY {cursor} LIMIT $4",
            field_name,
            id,
            after_id,
            limit,
        )
    else:
        rows = await db.fetch(
            f"SELECT {columns} FROM {source} WHERE {scope} ORDER BY {cursor} LIMIT $3 OFFSET $4",
            field_name,
            id,
            limit,
            offset,
        )

    total = None
    if offset == 0 and not after_id:
        if pending_only:
            total = await db.fetchval(
                "SELECT COUNT(*) FROM embedding_projection_pending WHERE field_name = $1 AND dataset_id = $2",
                field_name,
                id,
            )
        else:
            total = await db.fetchval(f"SELECT COUNT(*) FROM {source} WHERE {scope}", field_name, id)

    # Serialize in a worker thread. Inline, a 10k x 1024-d page cost 8.23s of CPU — pure-Python
    # float parsing, then jsonable_encoder, then json.dumps of a ~91MB string — and
    # compute_visualization pages the whole field, so ONE UMAP fit froze a uvicorn worker in ~8s
    # blocks about 320 times in a row while thumbnails, grid and map requests queued behind it.
    #
    # Measured worst-case event-loop stall for the same page:
    #   inline (before)      8226 ms
    #   json,   in a thread  2086 ms   <- json.dumps holds the GIL; a thread can't fully help
    #   binary, in a thread    28 ms   <- numpy releases it
    # So binary is the real fix and is what the SDK asks for; json stays for older clients.
    if response_format == "binary":
        return await asyncio.to_thread(_vectors_binary_response, rows, total)
    return await asyncio.to_thread(_vectors_json_response, rows, total)


# --- Coordinate upload (for SDK UMAP results) ---


@router.post("/coordinates")
async def upload_coordinates(
    id: int,
    field_name: str,
    coordinates: List[CoordinateUpdate],
    user: dict = Depends(require_role("power_user")),
    model_id: Optional[str] = None,
):
    """Store 2D layout coordinates. Managed maps require their current `model_id` for incremental
    writes and refuse manual replacements. Incremental writes pause only once a re-fit is active."""
    if not coordinates:
        return {"message": "No coordinates provided"}

    # One set-based upsert, scoped to this dataset and existing embeddings.
    sample_ids = [c.sample_id for c in coordinates]
    xs = [c.x for c in coordinates]
    ys = [c.y for c in coordinates]
    versions = [c.vector_version for c in coordinates]
    async with coordinate_write(id, [field_name], model_id, projected=any(versions) or model_id is not None) as conn:
        await require_free_space(
            conn,
            len(coordinates) * COORDINATE_ROW_BYTES,
            f"updating {len(coordinates)} coordinates",
        )
        await conn.execute(
            SAVE_PROJECTED_COORDINATES if any(versions) else SAVE_COORDINATES,
            sample_ids,
            xs,
            ys,
            id,
            field_name,
            *([versions] if any(versions) else []),
            timeout=120.0,
        )

    return {"message": f"Updated {len(coordinates)} coordinates"}


# How long an upload waits for a lock (the rank fence, a row) before a retryable 409.
WRITE_LOCK_SECONDS = 2


async def lock_refusal(dataset_id: int, connection=None) -> HTTPException:
    """Dataset-wide contention keeps any swap/rank deadline, even for a sibling map's writer.
    Looks on ``connection`` (outside any transaction) when given, else on a pooled one."""
    if connection is not None:
        holder = await map_swap_holder(connection, dataset_id)
    else:
        async with db.pool.acquire(timeout=5) as pooled:
            holder = await map_swap_holder(pooled, dataset_id)
    if holder is not None:
        refusal = map_swap_refusal(*holder)
        return CodedHTTPException(
            status_code=409,
            detail=refusal.detail,
            code=f'map_{holder[1]}_busy',
            headers=refusal.headers,
        )
    return CodedHTTPException(
        status_code=409,
        code='dataset_busy',
        detail='The dataset is busy (a re-rank or another write holds what this write needs); retry shortly',
        headers={'Retry-After': '5'},
    )


def map_state_summary(row) -> Optional[Dict[str, Any]]:
    """What a scatter response says about the map: the current model and when it was re-fitted."""
    if row is None or row['model_id'] is None:
        return None
    return {"model_id": str(row['model_id']), "refitted_at": row['refitted_at'].isoformat()}


@router.get("/map-state")
async def get_map_state(id: int, field_name: str, user: dict = Depends(require_role("power_user"))):
    """The field's map: its current model (from the last scheduled re-fit or Run now, or none while it
    still uses a legacy saved model), when it was re-fitted, and whether a re-fit is in progress. For
    projectors (power users): it names the model file on the server. Viewers get `map_state` with the
    scatter."""
    row = await db.fetchrow(
        """SELECT m.*, (j.state IN ('queued','running')) IS TRUE AS active,
                  (SELECT total_points FROM embedding_viz_meta v
                   WHERE v.dataset_id = m.dataset_id AND v.field_name = m.field_name) AS current_points
           FROM embedding_map_state m LEFT JOIN jobs j ON j.id = m.active_job_id
           WHERE m.dataset_id = $1 AND m.field_name = $2""",
        id,
        field_name,
    )
    if row is None:
        return {"field_name": field_name, "managed": False, "active": False, "model_id": None, "refitted_at": None}
    location = row['artifact_location']
    return {
        "field_name": field_name,
        "managed": row['model_id'] is not None,
        "active": row['active'],
        "active_job_id": str(row['active_job_id']) if row['active'] else None,
        "model_id": str(row['model_id']) if row['model_id'] else None,
        "refitted_at": row['refitted_at'].isoformat() if row['refitted_at'] else None,
        "artifact_location": location,
        # Where this server keeps it; a process on the same machine can load it from there.
        "artifact_path": str(LocalArtifactStore().path(location)) if location else None,
        "artifact_checksum": row['artifact_checksum'],
        "artifact_bytes": row['artifact_bytes'],
        "dimension": row['dimension'],
        "fit_size": row['fit_size'],
        "points_at_refit": row['points_at_refit'],
        "current_points": row['current_points'] or 0,
    }


@router.post("/recompute-viz-rank")
async def recompute_viz_rank(
    id: int,
    field_name: str,
    full: bool = True,
    user: dict = Depends(require_role("power_user")),
):
    """Recompute the even-coverage scatter sample-priority (viz_cell_rank +
    embedding_viz_meta) for a field. The SDK calls this after a UMAP reduce
    (full=True → re-rank all points). full=False is a compatibility no-op:
    coordinate triggers already maintain ranks in ranked fields.

    Delegates to the recompute_viz_rank() SQL function (the same one the direct-DB
    umap_project_job daemon calls), so the logic lives in exactly one place.
    """
    async with db.pool.acquire() as conn:
        if full:
            # A full rebuild deletes and re-inserts every rank row of the field.
            # Refuse (507) rather than fill the volume mid-write.
            await require_free_space(
                conn,
                await estimate_viz_rank_bytes(conn, id, field_name),
                f"a full viz-rank rebuild of {field_name} on dataset {id}",
            )
        ranked = await conn.fetchval(
            "SELECT recompute_viz_rank($1, $2, $3)",
            id,
            field_name,
            full,
            timeout=1800,
        )
    return {"ranked": ranked or 0, "field_name": field_name, "full": full}
