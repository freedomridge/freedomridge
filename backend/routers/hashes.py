"""Image hash exact-duplicate detection.

Works with any perceptual hash algorithm. Hashes are stored in the
sample_hashes table with a field_name identifier. The SDK provides
compute_hashes() to populate hashes using standard imagehash algorithms.
"""

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import job_wait
from ..access import require_dataset_access
from ..database import db
from ..diskspace import INGEST_ROW_BYTES, require_free_space
from ..request_limits import guest_heavy
from .auth import require_role

router = APIRouter(
    prefix="/datasets/{id}/hashes",
    tags=["hashes"],
    dependencies=[Depends(require_dataset_access)],
)


class DuplicateRequest(BaseModel):
    field_name: str = ""  # Which hash field to use
    limit: int = Field(50, ge=1)  # a negative LIMIT reached Postgres and 500'd
    offset: int = 0
    filters: Optional[Dict[str, Any]] = None


class SampleDuplicatesRequest(BaseModel):
    sample_id: int
    field_name: str = "_any"  # a specific hash field, "_any" (>=1 field), or "_all" (every field)


class HashUpload(BaseModel):
    sample_id: int
    field_name: str
    hash_hex: str


@router.get("/fields")
async def get_hash_fields(id: int):
    """List available hash fields for this dataset."""
    rows = await db.fetch(
        """
        SELECT DISTINCT h.field_name, COUNT(*) as count
        FROM sample_hashes h
        JOIN samples s ON h.sample_id = s.id
        WHERE s.dataset_id = $1
        GROUP BY h.field_name
        ORDER BY h.field_name
    """,
        id,
    )
    return [{"field_name": r['field_name'], "count": r['count']} for r in rows]


# Upper bound on ONE export page. Bounds the page, not the export: after_id paging walks the
# whole field.
_MAX_EXPORT_PAGE = 50_000


@router.get("/export")
@guest_heavy
async def export_hashes(
    id: int,
    field_name: str,
    after_id: int = 0,
    limit: int = Query(10000, ge=1, le=_MAX_EXPORT_PAGE),
):
    """Bulk-read this dataset's hashes for one field, keyset-paginated by sample_id.

    THE READ SIDE THAT WAS MISSING. `POST /upload` existed but nothing could get hashes back out,
    so `fridge.sync_between_instances` reached for `sample['metadata'][field_name]` — where hashes
    have never lived (they are rows in sample_hashes, and the Sample response model has no hashes
    field). It therefore transferred ZERO hashes on every run while reporting success.

    Keyset (`after_id`), not OFFSET: callers walk millions of rows, and the (sample_id, field_name)
    unique index makes `sample_id > $n ORDER BY sample_id` O(page) at any depth. No COUNT here —
    `GET /hashes/fields` already reports per-field totals, so a paging caller counts once.
    """
    rows = await db.fetch(
        """
        SELECT h.sample_id, h.field_name, h.hash_hex
        FROM sample_hashes h
        JOIN samples s ON s.id = h.sample_id
        WHERE s.dataset_id = $1 AND h.field_name = $2 AND h.sample_id > $3
        ORDER BY h.sample_id
        LIMIT $4
        """,
        id,
        field_name,
        after_id,
        limit,
    )
    return {
        "items": [
            {"sample_id": r['sample_id'], "field_name": r['field_name'], "hash_hex": r['hash_hex']} for r in rows
        ],
    }


# Upper bound on the sample ids named in ONE /by-ids request.
_MAX_HASHES_BY_IDS = 10_000


class HashesByIdsRequest(BaseModel):
    sample_ids: List[int] = Field(..., max_length=_MAX_HASHES_BY_IDS)
    field_names: List[str] = Field(..., min_length=1)


@router.post("/by-ids")
@guest_heavy
async def hashes_by_ids(id: int, request: HashesByIdsRequest):
    """Hashes for specific samples and fields. `GET /export` pages a whole field; this is the lookup
    for copying a subset (`Dataset.add_samples_from`). Scoped to dataset `id`."""
    rows = await db.fetch(
        """
        SELECT h.sample_id, h.field_name, h.hash_hex
        FROM sample_hashes h
        JOIN samples s ON s.id = h.sample_id
        WHERE s.dataset_id = $1 AND h.sample_id = ANY($2) AND h.field_name = ANY($3)
        ORDER BY h.sample_id, h.field_name
        """,
        id,
        request.sample_ids,
        request.field_names,
    )
    return {
        "items": [
            {"sample_id": r['sample_id'], "field_name": r['field_name'], "hash_hex": r['hash_hex']} for r in rows
        ],
    }


class HashStatsRequest(BaseModel):
    field_name: Optional[str] = None
    filters: Optional[Dict[str, Any]] = None


@router.get("/stats")
@guest_heavy
async def hash_stats_get(id: int, field_name: Optional[str] = None):
    """Get hash stats (unfiltered)."""
    return await _hash_stats_impl(id, field_name, None)


@router.post("/stats")
@guest_heavy
async def hash_stats_post(id: int, request: HashStatsRequest):
    """Get hash stats with optional filters."""
    return await _hash_stats_impl(id, request.field_name, request.filters)


async def _hash_stats_impl(id: int, field_name: Optional[str], filter_obj: Optional[Dict[str, Any]]):
    """Get hash coverage and duplicate statistics, optionally filtered."""
    filter_clause = ""
    args: list = [id]
    arg_idx = 2
    if filter_obj:
        from .datasets import SAMPLES_JOIN_FIELD_MAPPING, get_dataset_array_columns, parse_filter_node

        filter_sql, arg_idx = parse_filter_node(
            filter_obj,
            args,
            arg_idx,
            field_mapping=SAMPLES_JOIN_FIELD_MAPPING,
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )
        if filter_sql and filter_sql != "TRUE":
            filter_clause = f"AND {filter_sql}"

    total = await db.fetchval(f"SELECT count(*) FROM samples s WHERE s.dataset_id = $1 {filter_clause}", *args)

    # Single query for all fields — one GROUP BY scan instead of N
    rows = await db.fetch(
        f"""
        SELECT
            field_name,
            COALESCE(sum(cnt), 0) as with_hashes,
            count(*) FILTER (WHERE cnt > 1) as dup_groups,
            COALESCE(sum(cnt) FILTER (WHERE cnt > 1), 0) as dup_samples
        FROM (
            SELECT h.field_name, h.hash_hex, count(*) as cnt
            FROM sample_hashes h
            JOIN samples s ON h.sample_id = s.id
            WHERE s.dataset_id = $1 {filter_clause}
            GROUP BY h.field_name, h.hash_hex
        ) sub
        GROUP BY field_name
    """,
        *args,
    )

    stats_by_field = {}
    for row in rows:
        if field_name and row['field_name'] != field_name:
            continue
        stats_by_field[row['field_name']] = {
            "samples_with_hashes": row['with_hashes'],
            "exact_duplicate_groups": row['dup_groups'],
            "exact_duplicate_samples": row['dup_samples'],
        }

    return {"total_samples": total, "fields": stats_by_field}


# One aggregation per page: the grouped set is computed ONCE and the page plus both totals are read
# off it. Each page used to run the same full GROUP BY three times (page, total groups, total
# samples); sharing the materialized aggregation avoids those repeated scans.
_DUP_PAGE_SQL = """
    WITH g AS MATERIALIZED ({grouped}),
         t AS (SELECT count(*) AS total_groups, COALESCE(sum(cnt), 0) AS total_samples FROM g),
         p AS (SELECT sample_ids, cnt FROM g ORDER BY cnt DESC LIMIT ${limit_idx} OFFSET ${offset_idx})
    SELECT t.total_groups, t.total_samples, p.sample_ids, p.cnt FROM t LEFT JOIN p ON TRUE
"""


@router.post("/duplicates")
@guest_heavy
async def find_duplicates(id: int, request: DuplicateRequest):
    """Find groups of exact-duplicate images (identical perceptual hash).

    field_name='_all': match on ALL hash fields combined.
    """
    if not request.field_name:
        return {"groups": [], "total_groups": 0, "field_name": ""}
    if request.field_name == '_any':
        # A collapse config (hidden if a duplicate under ANY hash), not a grouping: there is no
        # single key to group by. It used to be read as a field name and answer "0 groups".
        raise HTTPException(
            status_code=400,
            detail="'_any' is a collapse setting, not a browsable grouping — browse a hash field or '_all'",
        )

    from .datasets import SAMPLES_JOIN_FIELD_MAPPING, get_dataset_array_columns, parse_filter_node

    if request.field_name == '_all':
        # All fields: group by the concatenated hash across all fields per sample.
        args: List[Any] = [id]
        arg_idx = 2
        filter_clause = ""
        if request.filters:
            fs, arg_idx = parse_filter_node(
                request.filters,
                args,
                arg_idx,
                field_mapping=SAMPLES_JOIN_FIELD_MAPPING,
                dataset_id=id,
                array_columns=await get_dataset_array_columns(id),
            )
            if fs and fs != "TRUE":
                filter_clause = f"AND {fs}"
        grouped = f"""
            SELECT combined_hash, array_agg(sample_id) AS sample_ids, count(*) AS cnt FROM (
                SELECT h.sample_id,
                       string_agg(h.field_name || ':' || h.hash_hex, ',' ORDER BY h.field_name) AS combined_hash
                FROM sample_hashes h
                JOIN samples s ON h.sample_id = s.id
                WHERE s.dataset_id = $1 {filter_clause}
                GROUP BY h.sample_id
            ) sub
            GROUP BY combined_hash
            HAVING count(*) > 1
        """
    else:
        args = [id, request.field_name]
        arg_idx = 3
        filter_clause = ""
        if request.filters:
            fs, arg_idx = parse_filter_node(
                request.filters,
                args,
                arg_idx,
                field_mapping=SAMPLES_JOIN_FIELD_MAPPING,
                dataset_id=id,
                array_columns=await get_dataset_array_columns(id),
            )
            if fs and fs != "TRUE":
                filter_clause = f"AND {fs}"
        grouped = f"""
            SELECT h.hash_hex, array_agg(h.sample_id) AS sample_ids, count(*) AS cnt
            FROM sample_hashes h
            JOIN samples s ON h.sample_id = s.id
            WHERE s.dataset_id = $1 AND h.field_name = $2 {filter_clause}
            GROUP BY h.hash_hex
            HAVING count(*) > 1
        """

    rows = await db.fetch(
        _DUP_PAGE_SQL.format(grouped=grouped, limit_idx=arg_idx, offset_idx=arg_idx + 1),
        *args,
        request.limit,
        request.offset,
    )
    return {
        "groups": [{"sample_ids": r['sample_ids'], "size": r['cnt']} for r in rows if r['sample_ids'] is not None],
        "total_groups": rows[0]['total_groups'] if rows else 0,
        "total_duplicate_samples": rows[0]['total_samples'] if rows else 0,
        "field_name": request.field_name,
    }


@router.post("/sample-duplicates")
async def sample_duplicates(id: int, request: SampleDuplicatesRequest):
    """Exact duplicates of a single sample: other samples sharing an identical
    hash with it (and the sample itself).

    field_name: a specific hash field, "_any" (match on at least one of the
    sample's hash fields), or "_all" (match on every hash field it has).
    """
    seed = request.sample_id
    if request.field_name == "_all":
        rows = await db.fetch(
            """
            WITH seed AS (
                SELECT h.field_name, h.hash_hex
                FROM sample_hashes h
                JOIN samples s ON h.sample_id = s.id
                WHERE h.sample_id = $1 AND s.dataset_id = $2
            )
            SELECT h.sample_id
            FROM sample_hashes h
            JOIN samples s ON h.sample_id = s.id
            JOIN seed ON seed.field_name = h.field_name AND seed.hash_hex = h.hash_hex
            WHERE s.dataset_id = $2
            GROUP BY h.sample_id
            HAVING count(*) = (SELECT count(*) FROM seed)
        """,
            seed,
            id,
        )
    elif request.field_name in ("", "_any"):
        rows = await db.fetch(
            """
            WITH seed AS (
                SELECT h.field_name, h.hash_hex
                FROM sample_hashes h
                JOIN samples s ON h.sample_id = s.id
                WHERE h.sample_id = $1 AND s.dataset_id = $2
            )
            SELECT DISTINCT h.sample_id
            FROM sample_hashes h
            JOIN samples s ON h.sample_id = s.id
            JOIN seed ON seed.field_name = h.field_name AND seed.hash_hex = h.hash_hex
            WHERE s.dataset_id = $2
        """,
            seed,
            id,
        )
    else:
        rows = await db.fetch(
            """
            WITH seed AS (
                SELECT h.hash_hex
                FROM sample_hashes h
                JOIN samples s ON h.sample_id = s.id
                WHERE h.sample_id = $1 AND s.dataset_id = $2 AND h.field_name = $3
            )
            SELECT DISTINCT h.sample_id
            FROM sample_hashes h
            JOIN samples s ON h.sample_id = s.id
            JOIN seed ON seed.hash_hex = h.hash_hex
            WHERE s.dataset_id = $2 AND h.field_name = $3
        """,
            seed,
            id,
            request.field_name,
        )

    ids = [r['sample_id'] for r in rows]
    if not ids:
        ids = [seed]
    return {"sample_ids": ids, "field_name": request.field_name}


def hash_upsert_sql(hex_len: int) -> str:
    """SQL to upsert one sample_hashes row from a hex string.

    bit_len = hex_len*4 — a derived int, safe to interpolate; the hash_hex binds as
    $3 ($1=sample_id, $2=field_name). hash_bits/prefix are derived in-SQL so the bit
    representation is identical everywhere. Shared by the /upload endpoint AND the
    inline-ingest path (datasets.add_samples) so the two can't drift.
    """
    bit_len = hex_len * 4
    return f"""
        INSERT INTO sample_hashes (sample_id, field_name, hash_hex, hash_bits, prefix)
        VALUES ($1, $2, $3,
            ('x' || $3)::bit({bit_len})::bit varying,
            substring(('x' || $3)::bit({bit_len}) from 1 for 16))
        ON CONFLICT (sample_id, field_name)
        DO UPDATE SET hash_hex = $3,
            hash_bits = ('x' || $3)::bit({bit_len})::bit varying,
            prefix = substring(('x' || $3)::bit({bit_len}) from 1 for 16)
    """


@router.post("/upload")
async def upload_hashes(id: int, hashes: List[HashUpload], user: dict = Depends(require_role("power_user"))):
    """Upload pre-computed hashes for samples (power_user+, scoped to this dataset)."""
    if not hashes:
        return {"message": "No hashes provided", "written": 0, "skipped": 0}

    # Drop any sample_id that doesn't belong to THIS dataset — a forged sample_id
    # must not write hash rows into another dataset's samples (cross-dataset write).
    requested_ids = list({h.sample_id for h in hashes})
    valid_rows = await db.fetch(
        "SELECT id FROM samples WHERE dataset_id = $1 AND id = ANY($2)",
        id,
        requested_ids,
    )
    valid = {r['id'] for r in valid_rows}

    # Use individual inserts since bit construction needs SQL expressions
    written = 0
    async with db.pool.acquire() as conn:
        # A full disk mid-ingest PANICs Postgres: refuse with a 507 while there is room.
        await require_free_space(conn, len(hashes) * INGEST_ROW_BYTES, f"adding {len(hashes)} hashes")
        for h in hashes:
            if h.sample_id not in valid:
                continue
            await conn.execute(hash_upsert_sql(len(h.hash_hex)), h.sample_id, h.field_name, h.hash_hex)
            written += 1

    return {"message": f"Uploaded {written} hashes", "written": written, "skipped": len(hashes) - written}


@router.post("/refresh-duplicates")
async def refresh_duplicates(
    id: int,
    config: str,
    user: dict = Depends(require_role("power_user")),
    wait: bool = True,
):
    """(Re)build the hidden-duplicate set powering the "collapse duplicates" toggle, for one
    hash_config (a hash field_name, or '_all'). Returns how many samples would be hidden.

    Submits a durable, bounded runner job and waits at most five seconds for {config, hidden};
    after that it answers 202 {job_id, state} and the job continues. A wait never cancels it.
    """
    started = time.monotonic()
    # '_all' and '_any' are configs, not fields; the collapse menu offers both, but '_any' was
    # refused here as "No hash field '_any'", so that set could never be rebuilt.
    if config not in ('_all', '_any'):
        exists = await db.fetchval(
            "SELECT 1 FROM sample_hashes h JOIN samples s ON h.sample_id = s.id "
            "WHERE s.dataset_id = $1 AND h.field_name = $2 LIMIT 1",
            id,
            config,
        )
        if not exists:
            raise HTTPException(status_code=400, detail=f"No hash field '{config}' for this dataset")
    from ..duplicate_jobs import request_refresh
    from ..job_queue import QueueBusy

    try:
        job = await request_refresh(db.pool, id, config, created_by=user['id'])
    except QueueBusy as error:
        raise HTTPException(status_code=409, detail='Duplicate submission is busy; retry') from error
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)


@router.get("/duplicate-count")
async def duplicate_count(id: int, config: str):
    """How many samples the collapse-duplicates toggle currently hides for this config
    (0 if never built). Read-only — the job runner maintains registered configs."""
    n = await db.fetchval(
        "SELECT count(*) FROM sample_dup_secondary WHERE dataset_id = $1 AND hash_config = $2",
        id,
        config,
    )
    return {"config": config, "hidden": n or 0}
