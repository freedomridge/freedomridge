"""Patch search's core: which patch scales a dataset has, which tile fields have their own HNSW index, and the
nearest patches to a vector. The search router uses it, and so will a follower answering a primary's patch searches
(docs/FOLLOWERS.md), so the two always search the same way.

Patch embeddings use the field name convention "{base}_patch_{scale}_r{row}c{col}", one field per tile position.
"""

import re
from typing import Any

from . import index_jobs

# Budget for an iterative HNSW scan (index tuples visited before giving up on filling
# LIMIT). pgvector's default is 20,000, which a 1% filter exhausts at ~200 survivors;
# A 200,000-tuple budget gives selective filters more headroom to fill LIMIT.
# Past it the search returns fewer rows than LIMIT — the same partial
# answer as before the iterative scan, just 2000x further out.
HNSW_MAX_SCAN_TUPLES = 200_000

# A field name that is safe to write into SQL as a literal (index builds refuse any other name).
SAFE_FIELD_NAME = re.compile(r'^[a-zA-Z0-9_]+$')


def indexed_grid(base_field: str, scale: str, indexed: set[str]) -> list[str] | None:
    """Every position field of the scale's grid ({base}_patch_{scale}_r{row}c{col}), or None
    unless each one has its own HNSW index. A grid with more positions than there are indexed
    fields can't be fully indexed, so it is never spelled out."""
    rows, cols = (int(x) for x in scale.split('x'))
    if rows * cols > len(indexed):
        return None
    fields = [f"{base_field}_patch_{scale}_r{row}c{col}" for row in range(rows) for col in range(cols)]
    return fields if all(field in indexed for field in fields) else None


async def indexed_patch_fields(conn, dataset_id: int, base_field: str, scale: str, dims: int) -> list[str] | None:
    """The scale's tile fields when each has its own HNSW index at `dims` in this dataset, else None (an exact scan).
    Indexes are per field name over every dataset for now, so `dataset_id` doesn't narrow it yet."""
    return indexed_grid(base_field, scale, await index_jobs.cosine_hnsw_fields(conn, dims) if dims else set())


async def patch_scales(conn, dataset_id: int, base_field: str) -> dict[str, dict]:
    """The patch scales of a dataset's base field: {scale: {local, dims, indexed_fields, providers}}.

    `local` scales have tile vectors in this dataset, with their size (`dims`) and the tile fields indexed for it
    (None: searched by an exact scan). `providers` are the followers that hold a scale this dataset doesn't, empty
    until they report their own fields."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT e.field_name
        FROM embeddings e
        JOIN samples s ON e.sample_id = s.id
        WHERE s.dataset_id = $1 AND e.field_name LIKE $2
        """,
        dataset_id,
        f"{base_field}_patch_%",
    )
    # Scales from field names like "clip_patch_4x4_r0c0".
    prefix = f"{base_field}_patch_"
    tiles: dict[str, str] = {}
    for row in rows:
        scale = row['field_name'][len(prefix) :].split('_')[0]
        tiles.setdefault(scale, row['field_name'])
    scales: dict[str, dict] = {}
    for scale, tile in sorted(tiles.items()):
        dims = await conn.fetchval(
            """SELECT vector_dims(e.vector) FROM embeddings e JOIN samples s ON e.sample_id = s.id
               WHERE s.dataset_id = $1 AND e.field_name = $2 LIMIT 1""",
            dataset_id,
            tile,
        )
        indexed = await indexed_patch_fields(conn, dataset_id, base_field, scale, dims) if dims else None
        scales[scale] = {'local': True, 'dims': dims, 'indexed_fields': indexed, 'providers': []}
    return scales


async def nearest_patches(
    conn,
    *,
    dataset_id: int,
    fields: list[str] | None,
    like_pattern: str,
    seed_vector: Any,
    dims: int,
    limit: int,
    per_image: int,
    filters: dict | None = None,
    exclude: tuple[int, str] | None = None,
):
    """The nearest patches of a dataset to `seed_vector`, at most `per_image` per image, nearest first, `limit` rows,
    with each row's sample. `filters` are the dataset's filters on its samples; `exclude` is a (sample id, field)
    patch left out (the query patch itself).

    `fields` (or None) are the patch position fields to search, each with its own HNSW index.
    Then each position's index finds its `limit` nearest patches and those are merged. Merging
    loses nothing: a patch that makes the result has fewer than `limit` nearer patches at its own
    position, because each of them is on a different image whose best patch ranks ahead of it.
    The answer is as approximate as each index's search, as in vector_search. Without indexes
    (or for limit > 1000, beyond hnsw.ef_search), every patch matching `like_pattern` is ranked
    exactly.
    """
    args: list[Any] = [seed_vector, limit, dataset_id]
    conditions = ['s.dataset_id = $3']
    if exclude is not None:
        args += [exclude[0], exclude[1]]
        conditions.append('NOT (e.sample_id = $4 AND e.field_name = $5)')
    if filters:
        from .routers.datasets import (
            SAMPLES_JOIN_FIELD_MAPPING,
            get_dataset_array_columns,
            get_promoted_numeric_fields,
            parse_filter_node,
        )

        # samples is aliased `s` here (joined to embeddings e), so map `id` → s.id:
        # bare `id` would be ambiguous (embeddings.id vs samples.id), and the
        # collapse_duplicates operator needs the correct outer sample id.
        filter_sql, _ = parse_filter_node(
            filters,
            args,
            len(args) + 1,
            field_mapping=SAMPLES_JOIN_FIELD_MAPPING,
            numeric_indexed_fields=await get_promoted_numeric_fields(dataset_id),
            dataset_id=dataset_id,
            array_columns=await get_dataset_array_columns(dataset_id),
        )
        if filter_sql and filter_sql != 'TRUE':
            conditions.append(filter_sql)
    where_clause = ' AND '.join(conditions)

    indexed = bool(fields) and limit <= 1000 and all(SAFE_FIELD_NAME.match(f) for f in fields or ())
    if indexed:
        dist = f"(e.vector::vector({dims}) <=> $1::vector({dims}))"
        distances = "\nUNION ALL\n".join(
            f"""(SELECT e.sample_id, e.field_name, {dist} AS distance
                 FROM embeddings e JOIN samples s ON e.sample_id = s.id
                 WHERE e.field_name = '{field}' AND {where_clause}
                 ORDER BY {dist} LIMIT $2)"""
            for field in fields or ()
        )
    else:
        args = [*args, like_pattern]
        distances = f"""SELECT e.sample_id, e.field_name, (e.vector <=> $1) AS distance
            FROM embeddings e JOIN samples s ON e.sample_id = s.id
            WHERE {where_clause} AND e.field_name LIKE ${len(args)}"""
    query = f"""
        WITH distances AS ({distances}),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY sample_id ORDER BY distance) as rn
            FROM distances
        )
        SELECT r.sample_id, r.field_name, r.distance,
               s.filepath, s.metadata, s.media_type, s.tags,
               ST_AsGeoJSON(s.location)::json as location
        FROM ranked r
        JOIN samples s ON r.sample_id = s.id
        WHERE r.rn <= {per_image}
        ORDER BY r.distance ASC
        LIMIT $2
    """
    async with conn.transaction():
        if indexed:
            # As in vector_search: ef_search >= limit, and an iterative scan so filters applied
            # after the index (the dataset, the user's filters) still leave `limit` rows.
            await conn.execute("SELECT '[0]'::vector(1)")
            await conn.execute(f"SET LOCAL hnsw.ef_search = {min(1000, max(limit * 2, 100))}")
            await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            await conn.execute(f"SET LOCAL hnsw.max_scan_tuples = {HNSW_MAX_SCAN_TUPLES}")
        return await conn.fetch(query, *args)
