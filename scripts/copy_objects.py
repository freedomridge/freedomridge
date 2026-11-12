#!/usr/bin/env python3
"""Re-home object crops from a sibling `*-objects` dataset INTO the image dataset
as `media_type='object'` rows — the in-dataset object/patch view.

Objects become real `samples` rows in the SAME dataset as their parent image
(see docs/OBJECT_PATCH_VIEW.md), so the existing sample-keyed grid, scatter,
concepts, faceting, map, and lasso serve them unchanged. This is a NON-destructive
key-remap of already-materialized data — it does NOT recompute embeddings and it
leaves the source dataset untouched (delete it separately once you're happy).

Mapping (source crop row -> new object row, same dataset as the parent image):
  - filepath   : kept as-is (the crop file; already unique, already servable)
  - media_type : 'object'
  - metadata   : kept as-is (already has crop_bbox, parent_id, label, ...)
  - tags       : kept as-is ([label])
  - the per-object vector is copied under the SAME field_name (default clip_obj),
    matched by filepath (unique), to the new object row.

Idempotent: re-running inserts nothing new (ON CONFLICT on (dataset_id, filepath)
and (sample_id, field_name)).

Usage:
    python scripts/copy_objects.py --src-dataset open-images-v7-objects \
        --dest-dataset open-images-v7 --field clip_obj
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_samples_dataset_media_type ON samples (dataset_id, media_type)",
    # As schema.sql makes it (0029): the parent_id triggers find waiting objects by it.
    "CREATE INDEX IF NOT EXISTS idx_samples_parent_id ON samples ((ltrim(metadata ->> 'parent_id', '0'))) "
    "WHERE media_type = 'object'",
]


async def _dataset_id(conn: asyncpg.Connection, name: str) -> int:
    row = await conn.fetchrow("SELECT id FROM datasets WHERE name = $1 AND NOT deleting", name)
    if not row:
        sys.exit(f"dataset not found: {name}")
    return row["id"]


async def copy_samples(conn: asyncpg.Connection, src: int, dest: int, batch_size: int = 5000) -> int:
    """Copy bounded batches, registering only committed destination metadata in the same transaction."""
    from backend.field_registry import REQUEST_FULL_REFRESH, invalidate_values, lock_field_lifecycle, register_fields

    last_id = 0
    written = 0
    source_limit = await conn.fetchval('SELECT COALESCE(max(id), 0) FROM samples WHERE dataset_id = $1', src)
    while True:
        source_ids = await conn.fetch(
            'SELECT id FROM samples WHERE dataset_id = $1 AND id > $2 AND id <= $3 ORDER BY id LIMIT $4',
            src,
            last_id,
            source_limit,
            batch_size,
        )
        if not source_ids:
            return written
        async with conn.transaction():
            for dataset_id in sorted({src, dest}):
                await lock_field_lifecycle(conn, dataset_id)
            inserted = await conn.fetch(
                """INSERT INTO samples (dataset_id, filepath, media_type, tags, metadata, location)
                   SELECT $2, crop.filepath, 'object', crop.tags, crop.metadata, parent.location
                   FROM samples crop JOIN samples parent ON parent.id = (crop.metadata ->> 'parent_id')::int
                   WHERE crop.dataset_id = $1 AND crop.id = ANY($3::int[]) AND parent.dataset_id = $2
                   ORDER BY crop.id
                   ON CONFLICT ON CONSTRAINT samples_dataset_filepath_unique DO NOTHING
                   RETURNING metadata""",
                src,
                dest,
                [row['id'] for row in source_ids],
            )
            metadata = [row['metadata'] or {} for row in inserted]
            declared = await register_fields(conn, dest, metadata)
            if inserted:
                await invalidate_values(conn, dest, set(declared) | {'tags', 'media_type'})
        if inserted:
            await conn.execute(
                REQUEST_FULL_REFRESH,
                dest,
            )
        written += len(inserted)
        last_id = source_ids[-1]['id']


async def run(args) -> None:
    from backend.database import PLANNING, init_connection

    conn = await asyncpg.connect(DATABASE_URL, server_settings=PLANNING)
    try:
        await init_connection(conn)
        src = await _dataset_id(conn, args.src_dataset)
        dest = await _dataset_id(conn, args.dest_dataset)
        print(f"src dataset {args.src_dataset} (id {src}) -> dest {args.dest_dataset} (id {dest})")

        for ddl in INDEXES:
            await conn.execute(ddl)
        print("indexes ensured")

        # Sanity: every source crop's parent_id must resolve to a dest-dataset image.
        bad = await conn.fetchval(
            """
            SELECT count(*) FROM samples crop
            WHERE crop.dataset_id = $1
              AND NOT EXISTS (
                SELECT 1 FROM samples p
                WHERE p.id = (crop.metadata ->> 'parent_id')::int AND p.dataset_id = $2
              )
            """,
            src,
            dest,
        )
        if bad:
            sys.exit(f"{bad} source crops have a parent_id that is not a {args.dest_dataset} image — aborting")

        # 1. Object rows into the dest (image) dataset. filepath/metadata/tags kept;
        #    media_type flipped to 'object'; location inherited from the parent image.
        inserted = await copy_samples(conn, src, dest)
        print(f"object rows inserted into dataset {dest}: {inserted}")

        # 2. Re-home the per-object vectors (matched by the unique filepath). No recompute.
        emb = await conn.fetchval(
            """
            WITH ins AS (
                INSERT INTO embeddings (dataset_id, sample_id, field_name, vector)
                SELECT $2, newobj.id, $3, e.vector
                FROM samples newobj
                JOIN samples crop ON crop.dataset_id = $1 AND crop.filepath = newobj.filepath
                JOIN embeddings e ON e.dataset_id = $1 AND e.sample_id = crop.id AND e.field_name = $3
                WHERE newobj.dataset_id = $2 AND newobj.media_type = 'object'
                ON CONFLICT (dataset_id, sample_id, field_name) DO NOTHING
                RETURNING 1
            )
            SELECT count(*) FROM ins
            """,
            src,
            dest,
            args.field,
        )
        print(f"object embeddings ('{args.field}') re-homed: {emb}")

        total_obj = await conn.fetchval(
            "SELECT count(*) FROM samples WHERE dataset_id = $1 AND media_type = 'object'",
            dest,
        )
        print(f"total object rows now in dataset {dest}: {total_obj}")
    finally:
        await conn.close()

    if not args.no_viz:
        print(f"\nComputing the object scatter (coordinates + viz_rank) for field '{args.field}'...")
        sys.path.insert(0, str(Path(__file__).parent.parent / "python"))
        from fridge import Dataset, connect, get_client

        connect(args.api_url)
        get_client().login(args.username, args.password)
        Dataset(args.dest_dataset).compute_visualization(field_name=args.field)
        print("object scatter ready.")


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src-dataset", required=True, help="The sibling *-objects dataset to re-home from")
    ap.add_argument("--dest-dataset", required=True, help="The image dataset to add object rows to")
    ap.add_argument("--field", default="clip_obj", help="Per-object embedding field name (default: clip_obj)")
    ap.add_argument("--api-url", default=os.getenv("FRIDGE_API_URL", "http://localhost:8000/api"))
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--no-viz", action="store_true", help="Skip compute_visualization")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
