#!/usr/bin/env python3
"""Backfill the hidden-duplicate sets behind the "collapse duplicates" toggle.

For every (dataset, hash field) that has hashes — plus each dataset's '_all' combined
config — submits a bounded duplicate-maintenance job. The co-located runner must be running.
Registered configs then stay current as hashes change.

    DATABASE_URL=postgres://freedomridge:password@localhost:5432/freedomridge python3 scripts/backfill_dup_secondary.py
"""

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")


async def main():
    from backend.database import init_connection
    from backend.duplicate_jobs import request_refresh

    async with asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2, init=init_connection) as pool:
        fields = await pool.fetch(
            "SELECT DISTINCT s.dataset_id, h.field_name "
            "FROM sample_hashes h JOIN samples s ON h.sample_id = s.id "
            "ORDER BY 1, 2",
        )
        datasets = sorted({r["dataset_id"] for r in fields})
        configs = (
            [(r["dataset_id"], r["field_name"]) for r in fields]
            + [(d, "_all") for d in datasets]
            + [(d, "_any") for d in datasets]
        )
        print(f"Scheduling {len(configs)} (dataset, config) duplicate set(s)...")
        for ds, cfg in configs:
            job = await request_refresh(pool, ds, cfg)
            print(f"  dataset {ds} / {cfg}: job {job['id']}")
        print("Done.")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    asyncio.run(main())
