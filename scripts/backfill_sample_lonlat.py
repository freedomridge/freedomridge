#!/usr/bin/env python
"""Online backfill for samples.lon/lat + the covering index.

Does the heavy parts that can't live in schema.sql's single transaction, without pausing
ingestion:

  1. Backfill lon/lat for any rows that lack them, in small committed batches (rows written
     through the trigger already have them). Idempotent — only touches rows where lon IS NULL.
  2. CREATE INDEX CONCURRENTLY the covering index (cannot run inside a transaction).
  3. ANALYZE so the planner has stats on the new columns.

Usage:
    DATABASE_URL=postgres://freedomridge:password@localhost:5432/freedomridge \
        python scripts/backfill_sample_lonlat.py
Re-runnable: skips already-filled rows and uses IF NOT EXISTS for the index.
"""

import asyncio
import os
import time

import asyncpg

DSN = os.environ.get("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")
BATCH = 50_000
INDEX_SQL = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_samples_lonlat_cover "
    "ON samples (dataset_id, lon, lat) INCLUDE (id, heading) "
    "WHERE location IS NOT NULL"
)


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        todo = await conn.fetchval("SELECT count(*) FROM samples WHERE location IS NOT NULL AND lon IS NULL")
        print(f"rows needing backfill: {todo:,}")
        done = 0
        t0 = time.perf_counter()
        # Walk by id range; each UPDATE is its own (autocommit) statement so locks/WAL
        # stay small and ingestion is never blocked. Re-derives the id window each loop
        # so it naturally terminates when no NULL-lon rows remain.
        while True:
            lo = await conn.fetchval("SELECT min(id) FROM samples WHERE location IS NOT NULL AND lon IS NULL")
            if lo is None:
                break
            hi = lo + BATCH
            n = await conn.execute(
                "UPDATE samples SET lon = ST_X(location::geometry), "
                "lat = ST_Y(location::geometry) "
                "WHERE location IS NOT NULL AND lon IS NULL AND id >= $1 AND id < $2",
                lo,
                hi,
            )
            done += int(n.split()[-1])
            print(
                f"  backfilled {done:,}/{todo:,} (id < {hi:,})  "
                f"{done / max(1e-9, time.perf_counter() - t0):,.0f} rows/s",
            )
        print(f"backfill done in {time.perf_counter() - t0:.0f}s")

        left = await conn.fetchval("SELECT count(*) FROM samples WHERE location IS NOT NULL AND lon IS NULL")
        assert left == 0, f"still {left} rows with NULL lon — aborting before index"

        print("creating covering index (CONCURRENTLY)...")
        t1 = time.perf_counter()
        await conn.execute(INDEX_SQL)  # autocommit — not in a txn
        print(f"index ready in {time.perf_counter() - t1:.0f}s")

        # Reclaim the dead tuples this backfill created AND set the visibility map, so the
        # map's index-only scan skips the heap IMMEDIATELY (otherwise it does heap fetches
        # and stays slow until the next autovacuum). Non-parallel to avoid a >1GB dynamic
        # shared-memory alloc on small /dev/shm (e.g. a 1GB Docker shm).
        print("VACUUM (ANALYZE) samples...")
        t2 = time.perf_counter()
        await conn.execute("SET max_parallel_maintenance_workers = 0")
        await conn.execute("VACUUM (ANALYZE) samples")
        print(f"vacuum done in {time.perf_counter() - t2:.0f}s")
        print("done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
