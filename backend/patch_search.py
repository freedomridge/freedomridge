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


# A tile field's base field, scale and position, e.g. clip_patch_8x8_r2c3.
PATCH_TILE = re.compile(r'^(.+)_patch_(\d+x\d+)_r(\d+)c(\d+)$')
# A follower that checked in this recently counts as online: its search poll returns every 25 s or less.
ONLINE_SECONDS = 60


async def dataset_patch_scales(conn, dataset_id: int, base_field: str | None = None) -> dict[tuple[str, str], dict]:
    """The patch scales with tile vectors in this dataset, of one base field or all:
    {(base_field, scale): {dims, tiles, indexed_fields}}. `tiles` are the scale's tile fields present here, and
    `indexed_fields` the grid when each tile has its own HNSW index (None: an exact scan)."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT e.field_name
        FROM embeddings e
        JOIN samples s ON e.sample_id = s.id
        WHERE s.dataset_id = $1 AND e.field_name LIKE $2
        """,
        dataset_id,
        f"{base_field}_patch_%" if base_field else '%_patch_%',
    )
    tiles: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        match = PATCH_TILE.match(row['field_name'])
        if match and (base_field is None or match.group(1) == base_field):
            tiles.setdefault((match.group(1), match.group(2)), []).append(row['field_name'])
    scales: dict[tuple[str, str], dict] = {}
    for (base, scale), fields in sorted(tiles.items()):
        dims = await conn.fetchval(
            """SELECT vector_dims(e.vector) FROM embeddings e JOIN samples s ON e.sample_id = s.id
               WHERE s.dataset_id = $1 AND e.field_name = $2 LIMIT 1""",
            dataset_id,
            min(fields),
        )
        indexed = await indexed_patch_fields(conn, dataset_id, base, scale, dims) if dims else None
        scales[(base, scale)] = {'dims': dims, 'tiles': sorted(fields), 'indexed_fields': indexed}
    return scales


async def patch_scales(conn, dataset_id: int, base_field: str) -> dict[str, dict]:
    """The patch scales of a dataset's base field: {scale: {local, dims, indexed_fields, providers}}.

    `local` scales have tile vectors in this dataset, with their size (`dims`) and the tile fields indexed for it
    (None: searched by an exact scan). `providers` are the followers that reported a scale of their own
    (docs/FOLLOWERS.md), each {follower_id, name, dims, indexed, online, last_seen_at}: a scale only they hold
    has `local` false."""
    scales: dict[str, dict] = {
        scale: {'local': True, 'dims': found['dims'], 'indexed_fields': found['indexed_fields'], 'providers': []}
        for (_, scale), found in (await dataset_patch_scales(conn, dataset_id, base_field)).items()
    }
    providers = await conn.fetch(
        f"""SELECT r.scale, r.follower_id, f.name, r.dims, r.indexed, f.last_seen_at,
                  COALESCE(f.last_seen_at > now() - interval '{ONLINE_SECONDS} seconds', FALSE) AS online
           FROM follower_fields r JOIN followers f ON f.id = r.follower_id
           WHERE r.dataset_id = $1 AND r.base_field = $2
           ORDER BY r.scale, r.first_reported_at, f.name""",
        dataset_id,
        base_field,
    )
    for row in providers:
        entry = scales.setdefault(
            row['scale'],
            {'local': False, 'dims': row['dims'], 'indexed_fields': None, 'providers': []},
        )
        entry['providers'].append({key: row[key] for key in row.keys() if key != 'scale'})
    return dict(sorted(scales.items()))


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
            numeric_indexed_fields=await get_promoted_numeric_fields(dataset_id, conn),
            dataset_id=dataset_id,
            array_columns=await get_dataset_array_columns(dataset_id, conn),
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
