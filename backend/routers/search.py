import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..access import check_dataset_access
from ..database import db
from ..encoders import encode_image, encode_text, has_image_tower, has_text_tower
from ..models import ImageSearchRequest, ObjectToPatchSearchRequest, PatchSearchRequest, Sample, SearchRequest
from ..request_limits import guest_heavy
from .auth import get_optional_user

router = APIRouter()

# Budget for an iterative HNSW scan (index tuples visited before giving up on filling
# LIMIT). pgvector's default is 20,000, which a 1% filter exhausts at ~200 survivors;
# A 200,000-tuple budget gives selective filters more headroom to fill LIMIT.
# Past it the search returns fewer rows than LIMIT — the same partial
# answer as before the iterative scan, just 2000x further out.
_HNSW_MAX_SCAN_TUPLES = 200_000

# Reverse-image-search URL fetch caps (SSRF-guarded — see _fetch_image_from_url).
log = logging.getLogger("fridge.search")

_IMAGE_URL_TIMEOUT = 10.0
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20MB — a query image, not a dataset upload
_DATA_URL_RE = re.compile(r"^data:image/[\w.+-]+;base64,", re.IGNORECASE)

_PATCH_RC_RE = re.compile(r'_r(\d+)c(\d+)$')
# Full patch field_name -> grid scale + position, e.g. clip_patch_8x8_r2c3.
_PATCH_SCALE_RC_RE = re.compile(r'_patch_(\d+)x(\d+)_r(\d+)c(\d+)$')


def _tile_rect(field_name: str):
    """Patch field_name -> normalized [x, y, w, h] of that tile, or None.

    Scale is written rows x cols (matching PatchSearchRequest), field as _r{row}c{col}.
    """
    m = _PATCH_SCALE_RC_RE.search(field_name)
    if not m:
        return None
    rows, cols, row, col = (int(g) for g in m.groups())
    if rows <= 0 or cols <= 0:
        return None
    return [col / cols, row / rows, 1.0 / cols, 1.0 / rows]


def _iou(a: list[float], b: list[float]) -> float:
    """IoU of two normalized [x, y, w, h] boxes."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


@router.post("/search", response_model=list[Sample])
@guest_heavy
async def vector_search(req: SearchRequest, request: Request):
    # Enforce per-dataset access: dataset_id lives in the body, so we can't
    # use the path-param dependency. Inline check is equivalent.
    user = await get_optional_user(request)
    await check_dataset_access(req.dataset_id, user)

    # Get the vector: from single sample, multiple samples (averaged), or raw vector
    if req.sample_ids and len(req.sample_ids) > 0:
        # Multiple samples: fetch all vectors and average them. Scope the seed lookup to the
        # authorized dataset (join samples) so a foreign sample_id can't be used as a seed or
        # probed for embedding presence across datasets.
        rows = await db.fetch(
            """
            SELECT e.vector FROM embeddings e JOIN samples s ON e.sample_id = s.id
            WHERE e.sample_id = ANY($1) AND e.field_name = $2 AND s.dataset_id = $3
        """,
            req.sample_ids,
            req.field_name,
            req.dataset_id,
        )
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"No {req.field_name} embeddings found for the provided samples",
            )
        import numpy as np

        vectors = [np.array([float(x) for x in r['vector'].strip('[]').split(',')]) for r in rows]
        centroid = np.mean(vectors, axis=0)
        vector_str = '[' + ','.join(str(x) for x in centroid) + ']'
    elif req.sample_id:
        vector_query = """
            SELECT e.vector FROM embeddings e JOIN samples s ON e.sample_id = s.id
            WHERE e.sample_id = $1 AND e.field_name = $2 AND s.dataset_id = $3
        """
        vector_row = await db.fetchrow(vector_query, req.sample_id, req.field_name, req.dataset_id)
        if not vector_row:
            raise HTTPException(
                status_code=404,
                detail=f"No {req.field_name} embedding found for sample {req.sample_id}",
            )
        vector_str = vector_row['vector']
    elif req.vector:
        vector_str = str(req.vector)
    elif req.text:
        # Natural-language search: CLIP-text-encode the prompt server-side. Only valid
        # against a field embedded by the SAME CLIP checkpoint — consult the registry
        # and refuse known text-less models instead of returning garbage.
        registered = await db.fetchval(
            "SELECT model FROM embedding_field_models WHERE field_name = $1",
            req.field_name,
        )
        if not has_text_tower(registered):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Field '{req.field_name}' was embedded with {registered!r}, which has no text "
                    f"tower — text search needs a CLIP-family embedding field."
                ),
            )
        vec = await encode_text(req.text, registered)
        field_dim = await db.fetchval(
            "SELECT vector_dims(vector) FROM embeddings WHERE field_name = $1 AND vector IS NOT NULL LIMIT 1",
            req.field_name,
        )
        if field_dim is not None and field_dim != len(vec):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Text encoder produced {len(vec)} dims but field '{req.field_name}' stores "
                    f"{field_dim} — register the field's checkpoint (embedding_field_models) so the "
                    f"matching text tower is used."
                ),
            )
        vector_str = '[' + ','.join(str(x) for x in vec) + ']'
    else:
        raise HTTPException(status_code=400, detail="Either text, vector, sample_id, or sample_ids must be provided")

    # Build query with parameters in order
    # Start with base parameters that will always be used
    args: list[Any] = []
    arg_idx = 1

    # Add vector parameter ($1)
    args.append(vector_str)
    vector_param = f"${arg_idx}"
    arg_idx += 1

    # Add limit parameter ($2)
    args.append(req.limit)
    limit_param = f"${arg_idx}"
    arg_idx += 1

    # Build WHERE conditions
    conditions = []

    # Add dataset_id condition
    args.append(req.dataset_id)
    conditions.append(f"s.dataset_id = ${arg_idx}")
    arg_idx += 1

    # Add field_name condition
    args.append(req.field_name)
    conditions.append(f"e.field_name = ${arg_idx}")
    arg_idx += 1

    # Add optional filters
    if req.filters:
        from .datasets import (
            SAMPLES_JOIN_FIELD_MAPPING,
            get_dataset_array_columns,
            get_promoted_numeric_fields,
            parse_filter_node,
        )

        # samples is aliased `s` here (joined to embeddings e), so map `id` → s.id:
        # bare `id` would be ambiguous (embeddings.id vs samples.id), and the
        # collapse_duplicates operator needs the correct outer sample id.
        filter_sql, arg_idx = parse_filter_node(
            req.filters,
            args,
            arg_idx,
            field_mapping=SAMPLES_JOIN_FIELD_MAPPING,
            numeric_indexed_fields=await get_promoted_numeric_fields(req.dataset_id),
            dataset_id=req.dataset_id,
            array_columns=await get_dataset_array_columns(req.dataset_id),
        )
        if filter_sql and filter_sql != "TRUE":
            conditions.append(filter_sql)

    where_clause = " AND ".join(conditions)

    # Cast to the field's fixed dimension so the ORDER BY matches a per-field HNSW
    # index (USING hnsw ((vector::vector(D)) vector_cosine_ops) WHERE field_name=...),
    # turning the full seq-scan exact KNN over millions of vectors into an index
    # scan. embeddings.vector is variable-dimension, but all rows for one field_name
    # share a dimension, so the cast does not change results — it only makes the
    # query index-eligible once the index exists (scripts/build_ann_indexes.py).
    # Scope the dimension probe to THIS dataset: embeddings.vector is variable-dimension
    # and two datasets can share a field_name at different dims (e.g. 512-d CLIP vs 1024-d
    # DINOv3), so an unscoped LIMIT 1 could pick the wrong dimension and mis-cast the KNN.
    dim_row = await db.fetchrow(
        "SELECT vector_dims(e.vector) AS d FROM embeddings e JOIN samples s ON e.sample_id = s.id "
        "WHERE e.field_name = $1 AND s.dataset_id = $2 AND e.vector IS NOT NULL LIMIT 1",
        req.field_name,
        req.dataset_id,
    )
    if dim_row and dim_row['d']:
        d = int(dim_row['d'])
        if req.vector and len(req.vector) != d:
            # The text path already checked this; a raw vector reached the ::vector(d) cast and
            # 500'd with "expected 512 dimensions, not 3".
            raise HTTPException(
                status_code=400,
                detail=f"vector has {len(req.vector)} dimensions; '{req.field_name}' is {d}-d in this dataset",
            )
        dist_expr = f"(e.vector::vector({d}) <=> {vector_param}::vector({d}))"
    else:
        dist_expr = f"(e.vector <=> {vector_param})"

    query = f"""
        SELECT s.id, s.filepath, s.metadata, s.media_type, s.tags,
               ST_AsGeoJSON(s.location)::json as location,
               s.created_at, s.last_modified_at,
               {dist_expr} as distance
        FROM embeddings e
        JOIN samples s ON e.sample_id = s.id
        WHERE {where_clause}
        ORDER BY {dist_expr} ASC
        LIMIT {limit_param}
    """

    # HNSW returns at most hnsw.ef_search candidates, and pgvector caps ef_search at 1000.
    # So there's no single fast path for an arbitrary top-K:
    #  - limit <= 1000: HNSW with ef_search >= limit (indexed, ms). Touch the vector type
    #    first so pgvector's GUC is registered even on a cold pooled connection.
    #  - limit  > 1000: EXACT scan (disable the index) — sorts the whole field by distance
    #    and returns the true top-K for ANY K (slower; full scan, ~seconds).
    # No artificial cap: a LIMIT beyond the available count simply returns everything.
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            if req.limit <= 1000:
                await conn.execute("SELECT '[0]'::vector(1)")
                await conn.execute(f"SET LOCAL hnsw.ef_search = {min(1000, max(req.limit * 2, 100))}")
                # pgvector post-filters the HNSW candidates: a plain scan yields at most
                # ef_search rows and the WHERE (dataset, user filters) is applied AFTER, so a
                # 1% filter turned LIMIT 50 into ~1 row with no signal. An iterative scan keeps
                # walking the graph until LIMIT rows survive the filter (or the tuple budget
                # is hit); relaxed_order is fine because the outer ORDER BY re-sorts the
                # survivors. Requires pgvector >= 0.8.
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                await conn.execute(f"SET LOCAL hnsw.max_scan_tuples = {_HNSW_MAX_SCAN_TUPLES}")
            else:
                await conn.execute("SET LOCAL enable_indexscan = off")
                await conn.execute("SET LOCAL enable_indexonlyscan = off")
            rows = await conn.fetch(query, *args)

    results = []
    for row in rows:
        data = dict(row)
        if data.get('metadata'):
            data['metadata'] = json.loads(data['metadata']) if isinstance(data['metadata'], str) else data['metadata']
        else:
            data['metadata'] = {}

        # Parse location from GeoJSON string to dict
        if data.get('location') and isinstance(data['location'], str):
            data['location'] = json.loads(data['location'])

        # filepath stays raw — see /api/samples/{id}/media for image fetching.

        results.append(data)

    return results


def _address_is_public(address: str) -> bool:
    """True only for a globally routable address. `is_global` is the ONE predicate that
    covers every non-routable range at once — private (10/8, 172.16/12, 192.168/16),
    loopback, link-local (incl. the 169.254.169.254 metadata service), multicast,
    reserved, 0.0.0.0/8, benchmarking (198.18/15), IPv6 ULA / link-local, and the
    carrier-grade NAT range 100.64.0.0/10 that Tailscale uses — which the previous
    flag list (is_private / is_loopback / ...) let through."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve_public_addresses(host: str, port: int) -> list[str]:
    """Resolve `host` (off the event loop) and refuse it if ANY address is non-public."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Could not resolve image_url host.")
    addresses = [str(info[4][0]) for info in infos]
    if not addresses or not all(_address_is_public(a) for a in addresses):
        raise HTTPException(status_code=400, detail="image_url resolves to a disallowed (internal) address.")
    return addresses


async def _fetch_image_from_url(url: str) -> bytes:
    """Fetch an image from a user-supplied URL, SSRF-guarded.

    A server may sit on a private network with internal services, so an unrestricted
    server-side fetch would be an SSRF hole. Guards: http(s) only; resolve the host and
    refuse any private/loopback/link-local/reserved/multicast address; no redirects (a
    redirect could hop to an internal target); a hard timeout; a streamed size cap; and
    a content-type check; and the connection is pinned to the validated address so a
    DNS-rebinding record cannot swap in an internal target between the check and the fetch.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="image_url must be an http(s) URL.")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="image_url has no host.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await _resolve_public_addresses(host, port)

    # Pin the connection to the address we just validated: the URL's host becomes that
    # IP, the original name travels in the Host header (and as the TLS SNI / certificate
    # name for https). Without this, httpx resolved the name a SECOND time and a
    # rebinding DNS record (public A record, then 127.0.0.1 / 169.254.169.254 on the
    # next lookup) walked straight past the check above.
    ip = addresses[0]
    netloc = f"[{ip}]" if ":" in ip else ip
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    pinned_url = parsed._replace(netloc=netloc).geturl()
    host_header = f"{host}:{parsed.port}" if parsed.port else host
    extensions = {"sni_hostname": host} if parsed.scheme == "https" else {}

    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=_IMAGE_URL_TIMEOUT) as client:
            async with client.stream(
                "GET",
                pinned_url,
                headers={"User-Agent": "FreedomRidge-image-search", "Host": host_header},
                extensions=extensions,
            ) as resp:
                # Generic errors on purpose: the upstream status / content-type were an
                # oracle for probing whatever the server can reach. Details go to the log.
                if resp.status_code != 200:
                    log.info("image_url fetch: %s returned HTTP %s", host, resp.status_code)
                    raise HTTPException(status_code=400, detail="Could not fetch image_url (not a reachable image).")
                ctype = resp.headers.get("content-type", "")
                if not ctype.startswith("image/"):
                    log.info("image_url fetch: %s returned content-type %r", host, ctype)
                    raise HTTPException(status_code=400, detail="Could not fetch image_url (not a reachable image).")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_IMAGE_BYTES:
                        raise HTTPException(status_code=400, detail="image_url image exceeds 20MB.")
                    chunks.append(chunk)
    except httpx.HTTPError:
        raise HTTPException(status_code=400, detail="Could not fetch image_url.")
    return b"".join(chunks)


def _decode_base64_image(image_base64: str) -> bytes:
    """Decode an uploaded image sent as base64 (optionally a `data:image/...;base64,` URL)."""
    payload = _DATA_URL_RE.sub("", image_base64.strip())
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="image_base64 is not valid base64.")
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Image exceeds 20MB.")
    return data


@router.post("/search/by-image", response_model=list[Sample])
@guest_heavy
async def search_by_image(req: ImageSearchRequest, request: Request):
    """Reverse image search: encode an uploaded image (base64) or one fetched from
    image_url with the field's CLIP vision tower, then run the same cosine-KNN as /search.

    Only valid against a CLIP-family field (the server has no image tower for DINO-style
    fields); the query image is embedded into that field's space and compared with the
    stored image vectors. Reuses vector_search wholesale via a synthetic SearchRequest.
    """
    user = await get_optional_user(request)
    await check_dataset_access(req.dataset_id, user)

    if req.image_url and user and user.get("role") == "guest":
        raise HTTPException(403, "Guests cannot search by image URL. Upload an image instead.")

    if req.image_base64:
        image_bytes = _decode_base64_image(req.image_base64)
    elif req.image_url:
        image_bytes = await _fetch_image_from_url(req.image_url)
    else:
        raise HTTPException(status_code=400, detail="Provide image_base64 or image_url.")
    if not image_bytes:
        raise HTTPException(status_code=400, detail="The provided image is empty.")

    # Only CLIP-family fields can be image-encoded server-side; refuse DINO-style fields
    # with a clear message instead of returning garbage (mirrors the text-search guard).
    registered = await db.fetchval(
        "SELECT model FROM embedding_field_models WHERE field_name = $1",
        req.field_name,
    )
    if not has_image_tower(registered):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Field '{req.field_name}' was embedded with {registered!r}, which has no server-side "
                f"image encoder — reverse image search needs a CLIP-family embedding field."
            ),
        )

    vec = await encode_image(image_bytes, registered)

    field_dim = await db.fetchval(
        "SELECT vector_dims(vector) FROM embeddings WHERE field_name = $1 AND vector IS NOT NULL LIMIT 1",
        req.field_name,
    )
    if field_dim is not None and field_dim != len(vec):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Image encoder produced {len(vec)} dims but field '{req.field_name}' stores {field_dim} — "
                f"register the field's checkpoint (embedding_field_models) so the matching CLIP model is used."
            ),
        )

    search_req = SearchRequest(
        dataset_id=req.dataset_id,
        vector=vec,
        field_name=req.field_name,
        limit=req.limit,
        filters=req.filters,
    )
    return await vector_search(search_req, request)


@router.post("/search/patches")
@guest_heavy
async def patch_search(req: PatchSearchRequest, request: Request):
    """Find images with patches similar to a selected patch.

    Patch embeddings use field_name convention: "{base}_patch_{scale}_r{row}c{col}".
    Searches all patches at the same scale and returns the best match per image.
    """
    # Per-dataset access enforcement — same reasoning as vector_search above.
    user = await get_optional_user(request)
    await check_dataset_access(req.dataset_id, user)

    # Build the query patch's field_name
    query_field = f"{req.field_name}_patch_{req.patch_scale}_r{req.patch_row}c{req.patch_col}"

    # Scope the seed lookup to the authorized dataset (join samples) so a foreign
    # sample_id can't be used as a seed or probed for embedding presence — the same
    # treatment as the vector_search seed lookups above.
    vector_row = await db.fetchrow(
        """
        SELECT e.vector FROM embeddings e JOIN samples s ON e.sample_id = s.id
        WHERE e.sample_id = $1 AND e.field_name = $2 AND s.dataset_id = $3
    """,
        req.sample_id,
        query_field,
        req.dataset_id,
    )
    if not vector_row:
        raise HTTPException(
            status_code=404,
            detail=f"No patch embedding found: {query_field} for sample {req.sample_id}",
        )
    vector_str = vector_row['vector']

    # Match all patches at the same scale: field_name LIKE 'clip_patch_4x4_%'
    scale_pattern = f"{req.field_name}_patch_{req.patch_scale}_%"

    # Exclude only the exact query patch (same sample + same position), not the whole sample.
    # Other patches on the same image are valid results.
    args = [vector_str, req.limit, req.dataset_id, scale_pattern, req.sample_id, query_field]
    conditions = [
        "s.dataset_id = $3",
        "e.field_name LIKE $4",
        "NOT (e.sample_id = $5 AND e.field_name = $6)",
    ]
    arg_idx = 7

    if req.filters:
        from .datasets import (
            SAMPLES_JOIN_FIELD_MAPPING,
            get_dataset_array_columns,
            get_promoted_numeric_fields,
            parse_filter_node,
        )

        # samples is aliased `s` here (joined to embeddings e), so map `id` → s.id:
        # bare `id` would be ambiguous (embeddings.id vs samples.id), and the
        # collapse_duplicates operator needs the correct outer sample id.
        filter_sql, arg_idx = parse_filter_node(
            req.filters,
            args,
            arg_idx,
            field_mapping=SAMPLES_JOIN_FIELD_MAPPING,
            numeric_indexed_fields=await get_promoted_numeric_fields(req.dataset_id),
            dataset_id=req.dataset_id,
            array_columns=await get_dataset_array_columns(req.dataset_id),
        )
        if filter_sql and filter_sql != "TRUE":
            conditions.append(filter_sql)

    where_clause = " AND ".join(conditions)

    # Find best matching patch per sample (compute distance once, rank, join)
    rows = await db.fetch(
        f"""
        WITH distances AS (
            SELECT e.sample_id, e.field_name,
                   (e.vector <=> $1) as distance
            FROM embeddings e
            JOIN samples s ON e.sample_id = s.id
            WHERE {where_clause}
        ),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY sample_id ORDER BY distance) as rn
            FROM distances
        )
        SELECT r.sample_id, r.field_name, r.distance,
               s.filepath, s.metadata, s.media_type, s.tags,
               ST_AsGeoJSON(s.location)::json as location
        FROM ranked r
        JOIN samples s ON r.sample_id = s.id
        WHERE r.rn = 1
        ORDER BY r.distance ASC
        LIMIT $2
    """,
        *args,
    )

    # Parse row/col from field_name like "clip_patch_4x4_r2c3"
    results = []
    for row in rows:
        data = dict(row)
        if data.get('metadata'):
            data['metadata'] = json.loads(data['metadata']) if isinstance(data['metadata'], str) else data['metadata']
        else:
            data['metadata'] = {}
        if data.get('location') and isinstance(data['location'], str):
            data['location'] = json.loads(data['location'])

        # filepath stays raw — see /api/samples/{id}/media for image fetching.

        # Extract patch position from field_name
        m = _PATCH_RC_RE.search(data.pop('field_name', ''))
        data['patch_row'] = int(m.group(1)) if m else 0
        data['patch_col'] = int(m.group(2)) if m else 0
        results.append(data)

    return results


@router.post("/search/object-to-patches")
@guest_heavy
async def object_to_patch_search(req: ObjectToPatchSearchRequest, request: Request):
    """From a labeled object crop, find similar PATCH tiles that are NOT already covered by a
    labeled box on their image — i.e. surface unlabeled look-alikes ("find missing things").

    Object crops (clip_obj) and patch tiles ({base}_patch_NxN_*) are the same CLIP space, so
    the object's vector probes the tiles directly. We take the best few tiles per parent
    image, then keep the best tile whose region does NOT overlap (IoU > threshold) an existing
    object box on that same parent — the uncovered, unlabeled candidate. Exact scan over the
    patch space (the LIKE-all-positions pattern can't use a per-field HNSW), like /search/patches.
    """
    user = await get_optional_user(request)
    await check_dataset_access(req.dataset_id, user)

    seed = await db.fetchrow(
        """
        SELECT e.vector FROM embeddings e JOIN samples s ON e.sample_id = s.id
        WHERE e.sample_id = $1 AND e.field_name = 'clip_obj' AND s.dataset_id = $2
        """,
        req.object_sample_id,
        req.dataset_id,
    )
    if not seed:
        raise HTTPException(
            status_code=404,
            detail=f"No clip_obj embedding for object {req.object_sample_id}",
        )
    vector_str = seed['vector']

    # Best few tiles per parent image. By default across all computed scales
    # ('{base}_patch_%'); if patch_scale is given, restrict to that one ('{base}_patch_NxN_%').
    if req.patch_scale:
        if not re.match(r'^\d+x\d+$', req.patch_scale):
            raise HTTPException(status_code=400, detail=f"patch_scale must be NxN, got: {req.patch_scale}")
        patch_pattern = f"{req.base_field}_patch_{req.patch_scale}_%"
    else:
        patch_pattern = f"{req.base_field}_patch_%"
    args: list[Any] = [vector_str, req.dataset_id, patch_pattern]
    conditions = ["s.dataset_id = $2", "e.field_name LIKE $3"]
    arg_idx = 4
    if req.filters:
        from .datasets import (
            SAMPLES_JOIN_FIELD_MAPPING,
            get_dataset_array_columns,
            get_promoted_numeric_fields,
            parse_filter_node,
        )

        filter_sql, arg_idx = parse_filter_node(
            req.filters,
            args,
            arg_idx,
            field_mapping=SAMPLES_JOIN_FIELD_MAPPING,
            numeric_indexed_fields=await get_promoted_numeric_fields(req.dataset_id),
            dataset_id=req.dataset_id,
            array_columns=await get_dataset_array_columns(req.dataset_id),
        )
        if filter_sql and filter_sql != "TRUE":
            conditions.append(filter_sql)
    where_clause = " AND ".join(conditions)
    args.append(max(req.limit * 4, 50))
    limit_param = f"${arg_idx}"

    rows = await db.fetch(
        f"""
        WITH distances AS (
            SELECT e.sample_id, e.field_name, (e.vector <=> $1) as distance
            FROM embeddings e
            JOIN samples s ON e.sample_id = s.id
            WHERE {where_clause}
        ),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY sample_id ORDER BY distance) as rn
            FROM distances
        )
        SELECT r.sample_id, r.field_name, r.distance,
               s.filepath, s.metadata, s.media_type, s.tags,
               ST_AsGeoJSON(s.location)::json as location
        FROM ranked r
        JOIN samples s ON r.sample_id = s.id
        WHERE r.rn <= 3
        ORDER BY r.distance ASC
        LIMIT {limit_param}
        """,
        *args,
    )

    # Existing labeled boxes per candidate parent image, to exclude already-covered tiles.
    parent_ids = list({r['sample_id'] for r in rows})
    covered: dict[int, list[list[float]]] = {}
    if parent_ids:
        # Match on the TEXT expression so idx_samples_parent_id (a text-expression index) serves
        # the lookup — a `(metadata->>'parent_id')::int = ANY(int[])` predicate cannot use it and
        # would full-scan every object row (~16M at Open Images scale) for a ~240-id candidate set.
        box_rows = await db.fetch(
            """
            SELECT (metadata ->> 'parent_id')::int AS pid, metadata -> 'crop_bbox' AS bbox
            FROM samples
            WHERE dataset_id = $1 AND media_type = 'object'
              AND metadata ->> 'parent_id' = ANY($2)
            """,
            req.dataset_id,
            [str(p) for p in parent_ids],
        )
        for br in box_rows:
            bbox = br['bbox']
            if isinstance(bbox, str):
                bbox = json.loads(bbox)
            # Guard the float() cast: a malformed crop_bbox (non-numeric / null element) must not
            # 500 the whole find-unlabeled request — just skip that box.
            if bbox and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
                covered.setdefault(br['pid'], []).append([float(v) for v in bbox])

    # Keep the best UNCOVERED tile per parent image (rows are already distance-sorted).
    results = []
    seen_parents: set[int] = set()
    for row in rows:
        pid = row['sample_id']
        if pid in seen_parents:
            continue
        rect = _tile_rect(row['field_name'])
        if rect is None:
            continue
        if any(_iou(rect, box) > req.iou_threshold for box in covered.get(pid, [])):
            continue  # tile overlaps an existing label -> already covered, skip
        seen_parents.add(pid)

        data = dict(row)
        data['metadata'] = (
            json.loads(data['metadata']) if isinstance(data.get('metadata'), str) else (data.get('metadata') or {})
        )
        if data.get('location') and isinstance(data['location'], str):
            data['location'] = json.loads(data['location'])
        m = _PATCH_SCALE_RC_RE.search(data.pop('field_name', ''))
        data['patch_scale'] = f"{m.group(1)}x{m.group(2)}" if m else None
        data['patch_row'] = int(m.group(3)) if m else 0
        data['patch_col'] = int(m.group(4)) if m else 0
        results.append(data)
        if len(results) >= req.limit:
            break

    return results
