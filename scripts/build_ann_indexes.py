"""Build per-field ANN (HNSW) indexes on the embeddings table.

`embeddings.vector` is a VARIABLE-dimension column (e.g. CLIP-512 and DINOv3-1024
coexist), so a single pgvector index is impossible. This builds ONE partial,
expression HNSW index per (field_name, dimension):

    CREATE INDEX CONCURRENTLY hnsw_emb_<field>
      ON embeddings USING hnsw ((vector::vector(<dim>)) vector_cosine_ops)
      WHERE field_name = '<field>';

which matches the cast ORDER BY expression in backend/routers/search.py, turning
the full seq-scan exact KNN into an index scan.

OPERATOR-RUN: the build is CONCURRENTLY (no write lock) but is slow and
memory-heavy on large tables (millions of vectors) — run it deliberately, off
peak. Re-running is safe (IF NOT EXISTS). Tune maintenance_work_mem first.

    DATABASE_URL=postgres://... python3 scripts/build_ann_indexes.py
    # or limit to one field:
    DATABASE_URL=... python3 scripts/build_ann_indexes.py --field embeddings_clip_vit_base32_torch
"""

import argparse
import asyncio
import os
import re
import time

import asyncpg

_SAFE = re.compile(r'^[a-zA-Z0-9_]+$')


async def main():
    parser = argparse.ArgumentParser(description="Build per-field HNSW indexes on embeddings.")
    parser.add_argument("--field", help="Only build the index for this field_name")
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge"),
    )
    parser.add_argument(
        "--work-mem",
        default="512MB",
        help="maintenance_work_mem for the build session (default 512MB; see the shm note below)",
    )
    args = parser.parse_args()

    conn = await asyncpg.connect(args.database_url)
    try:
        q = (
            "SELECT field_name, vector_dims(vector) AS dim, count(*) AS n "
            "FROM embeddings WHERE vector IS NOT NULL "
            + ("AND field_name = $1 " if args.field else "")
            + "GROUP BY 1, 2 ORDER BY 1"
        )
        rows = await conn.fetch(q, *([args.field] if args.field else []))
        if not rows:
            print("No embeddings found — nothing to index.")
            return

        # One field should have exactly one dimension; warn and skip if mixed.
        by_field = {}
        for r in rows:
            by_field.setdefault(r['field_name'], []).append((r['dim'], r['n']))

        for field, dims in by_field.items():
            if not _SAFE.match(field or ""):
                print(f"SKIP {field!r}: unsafe field name")
                continue
            if len(dims) > 1:
                print(f"SKIP {field!r}: mixed dimensions {dims} — clean up before indexing")
                continue
            dim, n = dims[0]
            # ≤63 chars without silent truncation collisions (two long fields sharing a
            # prefix used to truncate to the SAME name; the second was never built).
            idx = f"hnsw_emb_{field}"
            if len(idx) > 63:
                import hashlib

                idx = idx[:54] + "_" + hashlib.sha1(field.encode()).hexdigest()[:8]
            # A previously-failed CONCURRENTLY build leaves an INVALID index under this
            # name; IF NOT EXISTS would "succeed" without fixing it. Drop the carcass.
            invalid = await conn.fetchval(
                "SELECT NOT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = $1",
                idx,
            )
            if invalid:
                print(f"  dropping INVALID leftover {idx} from a failed build…")
                await conn.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{idx}"')
            print(f"Building {idx} on {field} (dim={dim}, {n:,} vectors)…", flush=True)
            t = time.time()
            # CONCURRENTLY → must run outside a transaction (asyncpg autocommits
            # single statements). Serial + bounded memory: a PARALLEL build allocates a
            # /dev/shm segment ~maintenance_work_mem in size, which ENOSPC-crashes when
            # the server tunes it near the container's shm limit. Tune --work-mem up
            # only if your /dev/shm has headroom.
            await conn.execute(f"SET maintenance_work_mem = '{args.work_mem}'")
            await conn.execute("SET max_parallel_maintenance_workers = 0")
            await conn.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{idx}" '
                f'ON embeddings USING hnsw ((vector::vector({dim})) vector_cosine_ops) '
                f"WHERE field_name = '{field}'",
            )
            print(f"  done in {time.time() - t:.0f}s")
        print("✅ ANN index build complete.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
