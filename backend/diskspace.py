"""Free-space gate for the few multi-GB single-transaction writes (full version snapshot, the
auto-snapshot inside a revert, a full viz-rank rebuild) and for every ingest request (samples,
embeddings, patch embeddings, coordinates, hashes, an in-process UMAP reduce, bulk metadata sets,
and the UMAP projection worker's coordinate writes).

Postgres has no SQL function for free disk space, and the backend may not share a disk with the
database (locally the DB volume is on root while the caches are elsewhere), so the number comes
from the database server itself: a superuser-only `COPY ... FROM PROGRAM 'df'` of its own data
directory. Where that is unavailable (managed Postgres / non-superuser) the gate is skipped — it
degrades to today's behaviour, never to a false refusal.

Why: a write that fills the disk mid-transaction makes Postgres PANIC (WAL write failure), and its
crash recovery can then fail for want of space too, leaving the database down until someone frees
space and starts it by hand. A viz-rank rebuild can do this in one transaction; ingest does the same
thing a batch at a time. A 507 before the write is the cheap way to avoid it.
"""

import logging
from typing import Optional, Tuple

from fastapi import HTTPException

logger = logging.getLogger("fridge.disk")

# Fixed headroom on top of the estimated write: WAL burst + index growth + anything else landing
# on the volume while the write runs.
HEADROOM_BYTES = 1 * 1024**3

# Ingest is many small transactions, so its estimate is per row written: an 8 KB HNSW page per
# indexed vector (measured) plus the row and its WAL. A normal batch needs a few MB; the
# term only matters for one huge request.
INGEST_ROW_BYTES = 16 * 1024
# Narrow coordinates and their rank-cache/index writes; no vector/HNSW copy.
COORDINATE_ROW_BYTES = 1024


async def db_volume_bytes(conn) -> Optional[Tuple[int, int]]:
    """(total, free) bytes of the database's data volume, or None when it cannot be determined."""
    try:
        data_dir = await conn.fetchval("SELECT current_setting('data_directory')")
        if not data_dir or "'" in data_dir:
            return None
        await conn.execute("CREATE TEMP TABLE IF NOT EXISTS _fridge_df (line TEXT)")
        await conn.execute("DELETE FROM _fridge_df")
        await conn.execute(f"COPY _fridge_df FROM PROGRAM 'df -B1 --output=size,avail {data_dir} | tail -n 1'")
        raw = await conn.fetchval("SELECT trim(line) FROM _fridge_df LIMIT 1")
        parts = raw.split() if raw else []
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        return int(parts[0]), int(parts[1])
    except Exception as e:  # not superuser, no df in the image, managed Postgres, ...
        logger.info("free-space check unavailable (%s); skipping the gate", e)
        return None


async def db_free_bytes(conn) -> Optional[int]:
    """Free bytes on the database's data volume, or None when it cannot be determined."""
    volume = await db_volume_bytes(conn)
    return volume[1] if volume else None


def _gb(n: int) -> str:
    # Two decimals: at the ingest line "needs 1.0 GB, 1.0 GB free" read as a contradiction.
    return f"{n / 1024**3:.2f} GB"


async def require_free_space(conn, needed_bytes: int, what: str) -> None:
    """Raise 507 when the database volume has less than needed_bytes + HEADROOM free.
    No-op when free space cannot be determined."""
    free = await db_free_bytes(conn)
    if free is None:
        return
    needed = int(needed_bytes) + HEADROOM_BYTES
    if free < needed:
        logger.error("refusing %s: needs ~%s on the database volume, %s free", what, _gb(needed), _gb(free))
        raise HTTPException(
            status_code=507,
            detail=(
                f"Not enough free disk on the database volume for {what}: needs about {_gb(needed)} "
                f"(including {_gb(HEADROOM_BYTES)} headroom), {_gb(free)} free. Free space or grow the "
                "volume, then retry. Refusing now avoids the write failing part-way and crashing the database. "
                "An admin can check Settings → System status."
            ),
        )
    logger.info("%s: ~%s needed, %s free on the database volume", what, _gb(needed), _gb(free))


async def estimate_snapshot_bytes(conn, dataset_id: int) -> int:
    """A full version snapshot copies every sample's metadata/tags/filepath/location into
    version_snapshots (+ its indexes): ~2x the dataset's live row bytes."""
    row_bytes = await conn.fetchval(
        "SELECT coalesce(sum(pg_column_size(metadata) + pg_column_size(tags) "
        "+ pg_column_size(filepath) + 128), 0)::bigint FROM samples WHERE dataset_id = $1",
        dataset_id,
    )
    return 2 * int(row_bytes or 0)


async def estimate_viz_rank_bytes(conn, dataset_id: int, field_name: str) -> int:
    """A full viz-rank rebuild deletes and re-inserts the field's rank rows (+3 indexes); the
    old rows stay as dead tuples until vacuum, so budget 2x the field's share of the table."""
    total = await conn.fetchval("SELECT pg_total_relation_size('embedding_viz_rank')")
    field_rows = await conn.fetchval(
        "SELECT count(*) FROM embedding_coordinates e JOIN samples s ON s.id = e.sample_id "
        "WHERE s.dataset_id = $1 AND e.field_name = $2 AND e.x IS NOT NULL",
        dataset_id,
        field_name,
    )
    all_rows = await conn.fetchval("SELECT coalesce(sum(total_points), 0)::bigint FROM embedding_viz_meta")
    share = (int(field_rows or 0) / int(all_rows)) if all_rows else 1.0
    return int(2 * int(total or 0) * min(1.0, max(share, 0.0)))
