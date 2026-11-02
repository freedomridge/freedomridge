#!/usr/bin/env python3
"""One-time backfill of the even-coverage scatter sample-priority.

For every (dataset, field) that has projected embeddings (rows with x IS NOT NULL),
runs recompute_viz_rank(..., full=TRUE) to populate embedding_viz_rank and the
embedding_viz_meta row. After this, the embeddings scatter query uses the fast index
range scan (WHERE viz_cell_rank < per_cell) instead of the per-request grid window-sort.

Idempotent — safe to re-run (a full re-rank each time).

    DATABASE_URL=postgres://freedomridge:password@localhost:5432/freedomridge python3 scripts/backfill_viz_rank.py
"""

import asyncio
import os

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        pairs = await conn.fetch(
            """SELECT DISTINCT s.dataset_id, e.field_name
                 FROM embedding_coordinates e JOIN samples s ON e.sample_id = s.id
                WHERE e.x IS NOT NULL
                ORDER BY 1, 2""",
        )
        print(f"Backfilling viz_cell_rank for {len(pairs)} (dataset, field) pair(s)...")
        for r in pairs:
            ranked = await conn.fetchval(
                "SELECT recompute_viz_rank($1, $2, TRUE)",
                r["dataset_id"],
                r["field_name"],
                timeout=3600,
            )
            print(f"  dataset {r['dataset_id']} / {r['field_name']}: ranked {ranked:,}")
        print("Done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
