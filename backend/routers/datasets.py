import asyncio
import collections
import datetime
import hashlib
import json
import logging
import math
import random
import re
import secrets
import time as _time
import uuid
from contextlib import asynccontextmanager, suppress
from fractions import Fraction
from functools import partial
from typing import Any, Callable, Deque, Dict, List, Literal, Optional, cast

import anyio
import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

from backend.datetimes import (
    UTC_TEXT_SQL,
    date_sql,
    instant_expr,
    instant_sql,
    instants_from,
    literal_window,
    parse_instant,
    read_once,
    utc_text,
    window_sql,
)
from backend.field_registry import (
    LOCAL_PREFIX,
    REQUEST_FULL_REFRESH,
    cached_distinct_values,
    field_stats,
    invalidate_values,
    lock_field_lifecycle,
    presentation_type,
    prune_fields,
    register_fields,
    validate_metadata,
)
from backend.label_fields import (
    DISCOVERY_TYPES,
    LEGACY_GEOMETRY_FIELDS,
    DatasetArrayColumns,
    append_discovery,
    copy_discovery,
    discover_fields,
    label_context,
    label_path,
)
from backend.local_fields import LOCAL_TAGS, local_field
from backend.sample_ingest import insert_samples

from .. import (
    class_rules,
    dataset_deletion,
    field_usage,
    job_queue,
    job_wait,
    local_writes,
    stale_lassos,
    statistics,
    unlinked_objects,
)
from ..access import check_dataset_access, filter_datasets_for_user, require_dataset_access
from ..database import db
from ..diskspace import INGEST_ROW_BYTES, require_free_space
from ..models import Dataset, Sample
from ..request_limits import guest_heavy
from ..security import private_guest, production
from ..validation import clean_name, clean_tags
from .auth import get_allowed_roles, get_current_user, get_optional_user, require_role
from .embeddings import _validate_field_name as is_embedding_field_name
from .embeddings import validate_embedding_dims
from .views import _decode_content as _decode_view_content

router = APIRouter()

# --- Dynamic array field support ---
# Cache of TEXT[] columns on the samples table. Refreshes every 60s.
_array_columns_cache: set | None = None
_array_columns_cache_time: float = 0
_RESERVED_COLUMNS = frozenset(
    [
        'id',
        'dataset_id',
        'filepath',
        'media_type',
        'tags',
        'metadata',
        'location',
        'heading',
        'created_at',
        'last_modified_at',
        'source_id',
        # A mirror's own fields (0022): never an array field, so the sync never writes them.
        'local_metadata',
        'local_tags',
        # An object's image (0029), derived from metadata.parent_id.
        'parent_id',
    ],
)
_SAFE_COLUMN_RE = re.compile(r'^[a-z][a-z0-9_]{0,62}$')

# Strict allowlist for any field / metadata-key name that gets interpolated into
# a SQL string (JSON keys in `metadata->>'...'`, ORDER BY targets, etc.). asyncpg
# cannot parameterize an identifier or a JSON key, so these names are the one
# token we must format into queries — and therefore the one SQL-injection vector.
# Dots are allowed so dotted pseudo-fields (e.g. `detections.label`) reach their
# dedicated handlers untouched; every breakout character (quote, backslash,
# semicolon, whitespace, parens) is excluded. Every real metadata key in the
# corpus matches `^[A-Za-z_][A-Za-z0-9_]*$`, so this rejects nothing legitimate.
_SAFE_FIELD_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]{0,127}$')


def _is_safe_field_name(name) -> bool:
    """True if `name` is safe to interpolate into SQL as a field / JSON key."""
    return isinstance(name, str) and _SAFE_FIELD_NAME_RE.match(name) is not None


def _queryable_field(name) -> bool:
    """A field name a query can use: a safe metadata name or column, or a mirror's own `@local.` field."""
    try:
        return _is_safe_field_name(name) or local_field(name) is not None
    except ValueError:
        return False


def _field_text_sql(name: str) -> str:
    """A scalar field's value as text: metadata->>'x', or local_metadata->>'x' for a mirror's own field (0022).
    Raises HTTP 400 for a name no query can use."""
    try:
        local = local_field(name)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    return local.text if local is not None else f"metadata->>'{_validate_field_name(name)}'"


def _validate_field_name(name) -> str:
    """Return `name` if safe to interpolate into SQL, else raise HTTP 400.

    Use at every site that formats a user- or DB-derived field name into a query
    string. Parameterize values; validate names.
    """
    if not _is_safe_field_name(name):
        raise HTTPException(status_code=400, detail=f"Invalid field name: {name!r}")
    return name


async def get_array_columns(source=None) -> set:
    """Get the set of TEXT[] array columns on the samples table (cached). `source` is a connection or pool (the
    API's db by default: the job runner, which has no db, passes its own)."""
    global _array_columns_cache, _array_columns_cache_time
    now = _time.time()
    if _array_columns_cache is None or now - _array_columns_cache_time > 60:
        rows = await (source or db).fetch("SELECT column_name FROM sample_array_columns")
        _array_columns_cache = {r['column_name'] for r in rows}
        _array_columns_cache_time = now
    return _array_columns_cache


# Built-in array columns that exist for every dataset. Everything else in
# sample_array_columns is a user-created field that belongs only to the dataset
# that registered it (dataset_fields, field_type='array').
_BUILTIN_ARRAY_COLUMNS = frozenset({'tags'})

# Only legacy geometry names get empty sidebar vocabularies without discovery.
# Classification-list vocabularies follow the discovered shape, including ground_truth/predictions.
_LABEL_ARRAY_FIELDS = LEGACY_GEOMETRY_FIELDS
# Equality/membership operators handled by the JSONB-containment fast path for label
# arrays (`<field>.label` / `segmentation.labels`). Negative ops mean "contains NONE",
# i.e. NOT(containment); anything outside this set (exists/is_null/comparisons) falls
# through to the generic per-element EXISTS handler.
# Include both 'not in' (the space form the frontend emits for facet/multi-select excludes) and
# 'not_in'/'nin' so a label exclusion is recognized however it's spelled.
_LABEL_NEG_OPS = ('!=', '<>', 'not in', 'not_in', 'nin')
_LABEL_EQ_OPS = ('=', '==', 'in') + _LABEL_NEG_OPS
# List-valued label ops (value is a list of labels); scalar ops (=, !=) wrap their single value.
# 'not in' (space) is list-valued like 'in' — omitting it wraps the list and 500s the @> build.
_LABEL_LIST_OPS = ('in', 'not in', 'not_in', 'nin')


async def get_dataset_array_columns(dataset_id: int, source=None) -> DatasetArrayColumns:
    """Array columns that belong to THIS dataset (per-dataset isolation).

    The physical array columns are global (sample_array_columns), but a
    user-created array field only belongs to the dataset that registered it.
    Without this scoping, one dataset's dynamic array fields show up in every
    other dataset's schema / array-field list. Built-ins stay universal.
    """
    cols = await (get_array_columns(source) if source is not None else get_array_columns())
    source = source or db
    result = set(cols & _BUILTIN_ARRAY_COLUMNS)
    if not cols - _BUILTIN_ARRAY_COLUMNS:
        return await label_context(source, dataset_id, result)
    rows = await source.fetch(
        "SELECT field_name FROM dataset_fields WHERE dataset_id = $1 AND origin = 'native_array'",
        dataset_id,
    )
    result.update(r['field_name'] for r in rows if r['field_name'] in cols)
    return await label_context(source, dataset_id, result)


class DatasetCreate(BaseModel):
    name: str
    description: Optional[str] = None
    media_fields: List[str] = ["filepath"]
    storage_config_id: Optional[int] = None
    is_global: bool = False  # private: admins, the owner and granted users


class DatasetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    media_fields: Optional[List[str]] = None
    grid_media_field: Optional[str] = None
    modal_media_field: Optional[str] = None
    segmentation_config: Optional[Dict[str, Any]] = None
    storage_config_id: Optional[int] = None
    is_global: Optional[bool] = None
    default_view_id: Optional[int] = None


# Columns selected when returning a Dataset model. Centralised so list/get/create/update
# all stay in sync with the model definition.
_DATASET_SELECT = (
    "id, name, description, media_fields, grid_media_field, modal_media_field, "
    "segmentation_config, storage_config_id, owner_id, is_global, created_at, deleting, deletion_job_id, "
    "default_view_id, "
    # A mirror of a dataset on another instance (docs/FOLLOWERS.md): read-only here.
    "(SELECT jsonb_build_object('state', m.state, 'primary_url', l.url, 'origin_dataset_id', m.origin_dataset_id, "
    "'tags', m.tags, 'views', m.views, 'concept_views', m.concept_views, "
    "'primary_versions', m.primary_versions) "
    "FROM mirrors m LEFT JOIN primary_links l ON l.id = m.link_id WHERE m.dataset_id = datasets.id) AS mirror"
)


async def _require_storage_config_serves(storage_config_id: int, dataset_id: Optional[int]) -> None:
    """A dataset's own storage config must serve it: a restricted config serves only the datasets it lists."""
    row = await db.fetchrow(
        """
        SELECT name, restricted, EXISTS (
            SELECT 1 FROM storage_config_datasets WHERE storage_config_id = $1 AND dataset_id = $2
        ) AS listed
        FROM storage_configs WHERE id = $1
        """,
        storage_config_id,
        dataset_id,
    )
    if row is None or not row['restricted'] or row['listed']:
        return  # an unknown id is refused by the foreign key
    if dataset_id is None:
        detail = (
            f"Storage config '{row['name']}' only serves the datasets it lists. Create the dataset without it, "
            "add the dataset to the config under Settings → Storage, then assign it."
        )
    else:
        detail = (
            f"Storage config '{row['name']}' only serves the datasets it lists, and this isn't one. "
            "Add the dataset to it under Settings → Storage first."
        )
    raise HTTPException(status_code=400, detail=detail)


# A new dataset's vectors partition waits at most 500 ms for samples, coordinates and pending rows
# (create_embeddings_partition), queued ahead of later writers, so it gets them as the running writes finish.
_PARTITION_RETRY_SECONDS = 10.0


@router.post("/datasets", response_model=Dataset)
async def create_dataset(dataset: DatasetCreate, user: dict = Depends(require_role("admin"))):
    dataset.name = await _clean_dataset_name(dataset.name)
    if dataset.storage_config_id is not None:
        await _require_storage_config_serves(dataset.storage_config_id, None)
    query = f"""
        INSERT INTO datasets (name, description, media_fields, storage_config_id, owner_id, is_global)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_DATASET_SELECT}
    """
    deadline, pause = _time.monotonic() + _PARTITION_RETRY_SECONDS, 0.05
    while True:
        try:
            row = await db.fetchrow(
                query,
                dataset.name,
                dataset.description,
                dataset.media_fields,
                dataset.storage_config_id,
                user['id'],
                dataset.is_global,
            )
            return _parse_dataset_row(row)
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=400, detail="Dataset with this name already exists")
        except asyncpg.ForeignKeyViolationError:
            raise HTTPException(status_code=400, detail="Invalid storage_config_id")
        except asyncpg.LockNotAvailableError:
            if _time.monotonic() + pause > deadline:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "The server is busy writing samples or building an index on them, so the dataset wasn't "
                        "created; try again in a moment"
                    ),
                    headers={'Retry-After': '5'},
                ) from None
            await asyncio.sleep(pause * random.uniform(0.5, 1.5))
            pause = min(pause * 2, 0.5)


async def _clean_dataset_name(name: str, exclude_id: Optional[int] = None) -> str:
    """Trimmed, non-empty and unique ignoring case: "" / "   " were accepted on create and rename,
    and "TEST" sat next to "test". The active-name unique index still guards
    the exact name against a race."""
    name = clean_name(name, "Dataset name")
    taken = await db.fetchval(
        "SELECT 1 FROM datasets WHERE NOT deleting AND lower(name) = lower($1) AND id IS DISTINCT FROM $2",
        name,
        exclude_id,
    )
    if taken:
        raise HTTPException(status_code=400, detail="Dataset with this name already exists")
    return name


def _parse_dataset_row(row) -> dict:
    """Convert dataset row to dict, ensuring segmentation_config is a proper dict."""
    data = dict(row)
    # asyncpg may return JSONB as string if codec isn't set
    if 'segmentation_config' in data and isinstance(data['segmentation_config'], str):
        data['segmentation_config'] = json.loads(data['segmentation_config'])
    return data


@router.get("/datasets", response_model=list[Dataset])
async def list_datasets(request: Request):
    """List datasets visible to the caller.

    - Unauthenticated → none (login required for all access)
    - Admin           → all
    - Other users     → is_global OR owner OR explicit grant in dataset_access
    """
    user = await get_optional_user(request)
    where_sql, where_args = await filter_datasets_for_user(user)
    query = f"SELECT {_DATASET_SELECT} FROM datasets WHERE {where_sql} ORDER BY id"
    rows = await db.fetch(query, *where_args)
    return [_parse_dataset_row(row) for row in rows]


@router.get("/datasets/{id}", response_model=Dataset)
async def get_dataset(id: int, request: Request):
    user = await get_optional_user(request)
    # Raises 404 if missing, 403 if no access. Returns the row but only with
    # the access-check fields, so we still need the full SELECT below.
    await check_dataset_access(id, user)
    row = await db.fetchrow(f"SELECT {_DATASET_SELECT} FROM datasets WHERE id = $1", id)
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return {
        **_parse_dataset_row(row),
        'sidebar_expand_all': await _sidebar_expands_all(id),
        'default_view': await _default_view(row['default_view_id']),
        'map_layouts': await _map_layouts(id),
    }


async def _map_layouts(dataset_id: int) -> Dict[str, str]:
    """Each embeddings map's current layout id (embedding_layouts), by map: a whole-image field, or a patch scale. A
    map not listed has not been laid out again since the upgrade that added layout ids: its layout id is null, and a
    lasso saved without one was drawn on that layout, so it is stale only once the map is listed here with an id."""
    rows = await db.fetch(
        'SELECT field_name, layout_id::text FROM embedding_layouts WHERE dataset_id = $1 AND layout_id IS NOT NULL',
        dataset_id,
    )
    return {row['field_name']: row['layout_id'] for row in rows}


async def _default_view(view_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """The dataset's default view as the app applies it. It is public (see _require_default_view_candidate),
    so everyone who can open the dataset may read it."""
    if view_id is None:
        return None
    # Its owner and whether it is a mirror's copy of the primary's: the app offers "Update this view" to who may. When
    # it was last updated: the app takes its new filters (Clear All goes back to them).
    row = await db.fetchrow(
        """SELECT id, name, slug, content, user_id, updated_at,
                  EXISTS (SELECT 1 FROM mirror_views m WHERE m.view_id = views.id) AS from_primary
           FROM views WHERE id = $1""",
        view_id,
    )
    return None if row is None else {**dict(row), 'content': _decode_view_content(row['content'])}


async def _require_default_view_candidate(connection: Any, dataset_id: int, view_id: int) -> None:
    """A default view must be one of the dataset's saved views, and public: everyone who opens the dataset
    starts on it. FOR SHARE keeps it public until the caller's transaction commits (share_view takes the
    row FOR UPDATE before it checks for a default view)."""
    view = await connection.fetchrow(
        "SELECT name, is_public FROM views WHERE id = $1 AND dataset_id = $2 FOR SHARE",
        view_id,
        dataset_id,
    )
    if view is None:
        if not await connection.fetchval("SELECT 1 FROM datasets WHERE id = $1", dataset_id):
            raise HTTPException(status_code=404, detail="Dataset not found")
        raise HTTPException(status_code=400, detail=f"View {view_id} is not a saved view of this dataset")
    if not view['is_public']:
        raise HTTPException(
            status_code=400,
            detail=f"Share the view '{view['name']}' with everyone first: everyone who opens the dataset "
            "starts on its default view.",
        )


async def _sidebar_expands_all(id: int) -> bool:
    """Whether the filter sidebar opens with every field expanded and computed: the dataset's rows
    number at most _LIGHTNING_FACET_MAX_ROWS, the size a filter is
    cheap to recompute every field for. A larger dataset opens its fields collapsed. Every row counts,
    objects too, though the sidebar shows images: each facet scan reads them all (the image-grain
    predicate filters rows, it bounds no index range), so a small image count alone is not enough.
    The count stops one row past the line, so a large dataset costs no more than a small one."""
    rows = await db.fetchval(
        "SELECT count(*) FROM (SELECT 1 FROM samples WHERE dataset_id = $1 LIMIT $2) s",
        id,
        _LIGHTNING_FACET_MAX_ROWS + 1,
    )
    return rows <= _LIGHTNING_FACET_MAX_ROWS


@router.patch("/datasets/{id}", response_model=Dataset)
async def update_dataset(id: int, dataset: DatasetUpdate, user: dict = Depends(require_role("admin"))):
    # A dataset needs a media field, and the grid/modal must show one of them: `[]` and a grid
    # field outside the list were both stored, leaving every tile blank.
    if dataset.media_fields is not None and not dataset.media_fields:
        raise HTTPException(status_code=400, detail="media_fields must list at least one field")
    if dataset.grid_media_field is not None or dataset.modal_media_field is not None:
        media = dataset.media_fields
        if media is None:
            media = await db.fetchval("SELECT media_fields FROM datasets WHERE id = $1", id) or []
        for which, chosen in (("grid", dataset.grid_media_field), ("modal", dataset.modal_media_field)):
            if chosen is not None and chosen not in media:
                raise HTTPException(status_code=400, detail=f"{which}_media_field must be one of media_fields {media}")

    # Build query dynamically based on set fields
    fields = []
    values: List[Any] = []
    idx = 1

    if dataset.name is not None:
        fields.append(f"name = ${idx}")
        values.append(await _clean_dataset_name(dataset.name, exclude_id=id))
        idx += 1

    if dataset.description is not None:
        fields.append(f"description = ${idx}")
        values.append(dataset.description)
        idx += 1

    if dataset.media_fields is not None:
        fields.append(f"media_fields = ${idx}")
        values.append(dataset.media_fields)
        idx += 1

    if dataset.grid_media_field is not None:
        fields.append(f"grid_media_field = ${idx}")
        values.append(dataset.grid_media_field)
        idx += 1

    if dataset.modal_media_field is not None:
        fields.append(f"modal_media_field = ${idx}")
        values.append(dataset.modal_media_field)
        idx += 1

    if dataset.segmentation_config is not None:
        try:
            config = class_rules.validate_config(dataset.segmentation_config)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        fields.append(f"segmentation_config = ${idx}")
        # Bind the dict directly — the asyncpg jsonb codec serializes it once.
        # (json.dumps here double-encoded it into a jsonb *string*, corrupting it.)
        values.append(config)
        idx += 1

    # storage_config_id is nullable (NULL means "use the default / env-var
    # config"), so we need to distinguish "omitted from request" from "set to
    # null" via model_fields_set. For is_global — a plain boolean — `None`
    # just means "not provided", so the simpler check suffices.
    if "storage_config_id" in dataset.model_fields_set:
        if dataset.storage_config_id is None:
            fields.append("storage_config_id = NULL")
        else:
            await _require_storage_config_serves(dataset.storage_config_id, id)
            fields.append(f"storage_config_id = ${idx}")
            values.append(dataset.storage_config_id)
            idx += 1

    if dataset.is_global is not None:
        fields.append(f"is_global = ${idx}")
        values.append(dataset.is_global)
        idx += 1

    # Nullable like storage_config_id: null removes the default view.
    if "default_view_id" in dataset.model_fields_set:
        if dataset.default_view_id is None:
            fields.append("default_view_id = NULL")
        else:
            fields.append(f"default_view_id = ${idx}")
            values.append(dataset.default_view_id)
            idx += 1

    if not fields:
        # No-op patch. PATCH already required admin, so a straight fetch is
        # fine — no additional access check needed.
        row = await db.fetchrow(f"SELECT {_DATASET_SELECT} FROM datasets WHERE id = $1", id)
        if not row:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return _parse_dataset_row(row)

    values.append(id)
    query = f"""
        UPDATE datasets
        SET {', '.join(fields)}
        WHERE id = ${idx}
        RETURNING {_DATASET_SELECT}
    """

    try:
        async with db.pool.acquire() as connection, connection.transaction():
            if dataset.default_view_id is not None:
                await _require_default_view_candidate(connection, id, dataset.default_view_id)
            row = await connection.fetchrow(query, *values)
        if not row:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return _parse_dataset_row(row)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=400, detail="Dataset with this name already exists")
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(status_code=400, detail="Invalid storage_config_id")


class ClassRuleUpdate(BaseModel):
    # The rule (class_rules.py); null removes the field's default, which is then argmax.
    rule: Optional[Dict[str, Any]] = None


@router.put("/datasets/{id}/class-rules/{field_name}", dependencies=[Depends(require_dataset_access)])
async def set_class_rule(
    id: int,
    field_name: str,
    update: ClassRuleUpdate = Body(...),
    user: dict = Depends(require_role("power_user")),
):
    """A class-probability field's default rule, which the app opens the field with and the grid draws. Anyone who
    may edit the dataset may set it; it changes only how the field is shown."""
    if not 0 < len(field_name) <= 255:
        raise HTTPException(status_code=400, detail='A field name is 1 to 255 characters')
    rule = None
    if update.rule is not None:
        try:
            rule = class_rules.validate_rule(update.rule)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
    async with db.pool.acquire() as connection, connection.transaction():
        # NO KEY UPDATE serializes with PATCH /datasets/{id} and the rename/clone carry, as they do.
        config = await connection.fetchval(
            'SELECT segmentation_config FROM datasets WHERE id = $1 FOR NO KEY UPDATE',
            id,
        )
        if isinstance(config, str):
            config = json.loads(config)
        config = dict(config or {})
        rules = dict(config.get('class_rules') or {})
        if rule is None:
            rules.pop(field_name, None)
        else:
            rules[field_name] = rule
        config['class_rules'] = rules
        await connection.execute('UPDATE datasets SET segmentation_config = $2 WHERE id = $1', id, config)
    return {'field_name': field_name, 'rule': rule}


@router.delete("/datasets/{id}", status_code=202)
async def delete_dataset(id: int, user: dict = Depends(require_role("admin"))):
    """Seal the dataset and return its durable deletion job."""
    if await db.fetchval("SELECT 1 FROM mirrors WHERE dataset_id = $1 AND state <> 'stopped'", id):
        raise HTTPException(
            status_code=409,
            detail='This dataset mirrors one on another Freedom Ridge instance. Stop sharing it with this follower '
            'there (Settings → Followers on the primary), then delete the copy here.',
        )
    return await dataset_deletion.submit(db.pool, id, user['id'])


@router.post("/datasets/{id}/deletion/resume", status_code=202)
async def resume_dataset_deletion(id: int, user: dict = Depends(require_role("admin"))):
    """Continue a failed or cancelled deletion with its existing checkpoint."""
    return await dataset_deletion.submit(db.pool, id, user['id'], resume=True)


# --- Per-dataset access control endpoints ---
#
# Only meaningful for non-global datasets. For global datasets, every user
# already has access regardless of grants — the dataset_access table is
# consulted only when datasets.is_global = FALSE.


class GrantAccessRequest(BaseModel):
    user_id: int


@router.get("/datasets/{id}/access")
async def list_dataset_access(id: int, user: dict = Depends(require_role("admin"))):
    """List users who have been granted explicit access to this dataset."""
    # Verify dataset exists; admins skip the access check intentionally — they
    # need to inspect access on every dataset.
    ds = await db.fetchrow("SELECT id, is_global FROM datasets WHERE id = $1", id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    rows = await db.fetch(
        """
        SELECT da.user_id, u.username, u.email, u.role,
               da.granted_by_user_id, gb.username AS granted_by_username,
               da.created_at
        FROM dataset_access da
        JOIN users u ON da.user_id = u.id
        LEFT JOIN users gb ON da.granted_by_user_id = gb.id
        WHERE da.dataset_id = $1
        ORDER BY u.username
        """,
        id,
    )
    return {"is_global": ds['is_global'], "grants": [dict(r) for r in rows]}


@router.post("/datasets/{id}/access", status_code=201)
async def grant_dataset_access(
    id: int,
    body: GrantAccessRequest,
    user: dict = Depends(require_role("admin")),
):
    """Grant `body.user_id` access to dataset `id`. Idempotent."""
    ds = await db.fetchval("SELECT id FROM datasets WHERE id = $1", id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")
    target = await db.fetchval("SELECT id FROM users WHERE id = $1", body.user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    await db.execute(
        """
        INSERT INTO dataset_access (dataset_id, user_id, granted_by_user_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (dataset_id, user_id) DO NOTHING
        """,
        id,
        body.user_id,
        user['id'],
    )
    return {"dataset_id": id, "user_id": body.user_id}


@router.delete("/datasets/{id}/access/{user_id}", status_code=204)
async def revoke_dataset_access(
    id: int,
    user_id: int,
    user: dict = Depends(require_role("admin")),
):
    """Revoke explicit access. No-op if the grant didn't exist."""
    await db.execute(
        "DELETE FROM dataset_access WHERE dataset_id = $1 AND user_id = $2",
        id,
        user_id,
    )


from fastapi import APIRouter, Depends  # noqa: E402

# ...


@router.get("/datasets/{id}/field-stats", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_dataset_field_stats(id: int, filters: Optional[str] = None, lightning_mode: bool = True):
    """
    Get dataset field statistics from cached dataset_field_stats table.
    For PROMOTED fields: computes filtered min/max based on current filters (fast with index).
    lightning_mode=True (default): Use cached values for performance.
    lightning_mode=False: Compute filtered values dynamically.
    """

    # Check dataset exists
    dataset_check = await db.fetchrow("SELECT id FROM datasets WHERE id = $1", id)
    if not dataset_check:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Parse filters if provided
    filter_obj = json.loads(filters) if filters else None
    return await _get_dataset_field_stats_impl(id, filter_obj, lightning_mode)


class FieldStatsRequest(BaseModel):
    filters: Optional[Dict[str, Any]] = None
    lightning_mode: bool = True
    # Facet request for the sidebar sections the user opened: only these fields are returned, with
    # their filtered facets computed under the usual lightning gates and per-field budget.
    fields: Optional[List[str]] = None
    # A facet request: answer one JSON line per field as it completes (NDJSON), not one list.
    stream: bool = False
    # A facet request: the breadth the lightning sidebar request decided for the same filter (its
    # X-Filter-Breadth header), so this request runs no second selectivity probe. It is a hint the
    # client already controls (lightning off computes every field eagerly), not a permission.
    breadth: Optional[Literal['broad', 'selective']] = None


@router.post("/datasets/{id}/field-stats", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_dataset_field_stats_post(id: int, request: FieldStatsRequest, http_request: Request):
    """
    POST version of get_dataset_field_stats to handle large filter payloads.
    """
    known_broad = None if request.breadth is None else request.breadth == 'broad'
    if request.fields is not None:
        # A missing dataset is a 404 and a malformed filter a 400 for the whole facet request, as on
        # main, not an error on each field; a stream must know before it starts.
        await _check_facet_request(id, request.filters)
        if request.stream:
            return await _stream_field_facets(id, request.filters, request.lightning_mode, request.fields, known_broad)
    report: Dict[str, Any] = {}
    result = await _cancel_on_disconnect(
        http_request,
        _get_dataset_field_stats_impl(
            id,
            request.filters,
            request.lightning_mode,
            request.fields,
            known_broad=known_broad,
            report=report,
        ),
    )
    if 'breadth' not in report or isinstance(result, Response):
        return result
    # The lightning sidebar request's breadth decision, for the same filter's facet request (above).
    breadth = {True: 'broad', False: 'selective', None: 'undecided'}[report['breadth']]
    return JSONResponse(jsonable_encoder(result), headers={'X-Filter-Breadth': breadth})


async def _check_facet_request(id: int, filter_obj: Optional[Dict[str, Any]]) -> None:
    if not await db.fetchval("SELECT 1 FROM datasets WHERE id = $1", id):
        raise HTTPException(status_code=404, detail="Dataset not found")
    if filter_obj and has_active_filters(filter_obj):
        parse_filter_node(
            filter_obj,
            [id],
            2,
            None,
            numeric_indexed_fields=await get_promoted_numeric_fields(id),
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )
        # Before a stream starts, so the request can still answer 409 (as the impl checks for itself).
        await require_ready_concepts(filter_obj)


async def _stream_field_facets(
    id: int,
    filter_obj: Optional[Dict[str, Any]],
    lightning_mode: bool,
    fields: List[str],
    known_broad: Optional[bool],
) -> StreamingResponse:
    """A facet request answered field by field: one JSON line per field as soon as it is final, so a
    fast field (a promoted range) never waits for a slow one. A field that fails answers its own line
    with an `error`. A complete answer ends with the line `{"done": true}`; if the request fails after
    the stream started, every field not yet answered gets an error line first, so no
    client waits for a line that will never come. When the client goes, the work is cancelled.
    """
    lines: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()

    async def produce() -> None:
        try:
            await _get_dataset_field_stats_impl(
                id,
                filter_obj,
                lightning_mode,
                fields,
                known_broad=known_broad,
                emit=lines.put_nowait,
            )
        finally:
            lines.put_nowait(None)

    def line(entry: Dict[str, Any]) -> str:
        return json.dumps(jsonable_encoder(_finite_bounds(entry)), allow_nan=False) + '\n'

    async def body():
        work = asyncio.ensure_future(produce())
        answered: set = set()
        try:
            try:
                while (entry := await lines.get()) is not None:
                    yield line(entry)
                    answered.add(entry.get('name'))  # only once written: a line that fails gets an error line
                await work
            except asyncpg.ObjectNotInPrerequisiteStateError as refused:
                # Refused after the 200 (a rescore started, the map was re-fitted): each field not yet
                # answered carries the 409 the client would have had, so it retries or drops the lasso.
                refusal = {'status': 409, **_refusal(refused)}
                for name in dict.fromkeys(fields):
                    if name not in answered:
                        yield line({'name': name, 'refusal': refusal})
            except Exception as error:  # noqa: BLE001 - the whole request failed after the 200
                failure = _facet_failure(error, f"facet stream of dataset {id}")
                for name in dict.fromkeys(fields):
                    if name not in answered:
                        yield line({'name': name, 'provenance': 'cached', **failure})
            yield line({'done': True})
        finally:
            # A client that left cancels the stream inside Starlette's cancel scope, which would cancel
            # this wait too: shield it, so the queries are cancelled and their connections returned
            # before the response ends.
            work.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(work, return_exceptions=True)

    # no-transform / no buffering: a compressing or buffering proxy would hold the lines back.
    headers = {'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'}
    return StreamingResponse(body(), media_type='application/x-ndjson', headers=headers)


async def _cancel_on_disconnect(request: Request, coro) -> Any:
    """Await `coro`, cancelling it once the client has gone: a superseded filter or a closed field
    otherwise keeps its queries running to the end of their budgets. The cancellation
    is awaited, so the queries are cancelled and their connections returned before this returns.

    It waits on `receive()` for the disconnect. `Request.is_disconnected()` cannot see one behind
    the app's `@app.middleware("http")`: that middleware's receive never reads in its instant poll.
    """

    async def disconnected() -> None:
        while (await request.receive())['type'] != 'http.disconnect':
            pass

    task = asyncio.ensure_future(coro)
    listener = asyncio.ensure_future(disconnected())
    try:
        done, _ = await asyncio.wait({task, listener}, return_when=asyncio.FIRST_COMPLETED)
        if task in done or listener.exception() is not None:
            return await task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return Response(status_code=499)  # client closed the request; nobody reads this
    finally:
        listener.cancel()
        task.cancel()  # no-op once done; stops the work if this handler itself is cancelled
        await asyncio.gather(listener, task, return_exceptions=True)


# Best-effort budget for a single filtered field-stats sub-query when
# lightning_mode is on. The filter sidebar is interactive — under that
# budget, falling back to cached unfiltered values is a better answer
# than holding pool connections open for minutes per UI interaction. The
# global asyncpg/proxy timeouts (10 min) remain in place for intentional
# long-running jobs.
_LIGHTNING_FIELD_STATS_BUDGET_S = 5.0

# A lightning-mode sidebar request computes no filtered facets:
# it serves every field from the cache at once and labels each with its provenance. A section the
# user opens asks for its own facets with `fields` (the on-open request, which keeps the per-field
# budget above). The sidebar request's only optional work, the selectivity probe that tells pending
# from cached, runs last within this budget. It bounds that work, not the response: the dataset
# lookup and the cached reads (including promoted fields' complete value lists) still wait for a
# connection like any request. Lightning-off, and the default view, stay eager with no such budget.
_LIGHTNING_SIDEBAR_BUDGET_S = 0.9

# `provenance` of a field's facet (values or min/max) in a field-stats response:
#   exact          computed for the active filter;
#   cached         dataset-wide values: no filter is active, the lightning gate does not recompute
#                  this field for the filter, or the filtered query failed or found no values;
#   pending        dataset-wide values; the filtered facet is computed when the field is opened;
#   out_of_budget  dataset-wide values; the filtered query ran out of its time budget.


# Advisory-lock first key of the database-wide facet slots (registry in schema.sql), second key 0..K-1.
_FACET_SLOT_LOCK = 20261021
# Second keys of the locks a worker holds (shared) while its requests' fields wait for a slot, one per
# waiting request: -1, -2, .... Every worker counts the others' in pg_locks, and while
# any wait, a worker keeps and takes slots only up to its share of the K slots, K x its waiting requests
# / every worker's waiting requests: the slots are shared per request, whichever worker serves it.
# Every waiting worker holds -1, so one lock tells whether anybody else waits at all.
_FACET_SLOT_WANTED = -1

# True when no other session holds a "wanted" lock: the exclusive lock conflicts with every other
# session's shared one but never with this session's own; released at once. O(1), unlike pg_locks.
_FACET_NOBODY_ELSE_WANTS = (
    'SELECT CASE WHEN pg_try_advisory_lock($1, $2) THEN pg_advisory_unlock($1, $2) ELSE false END'
)
# How many requests of other workers wait for a slot: the "wanted" locks (negative second keys, shared;
# a probe's passing exclusive lock is not one) that other sessions hold.
_FACET_OTHERS_WAITING = (
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND classid = $1::int::oid AND objsubid = 2 AND objid::int < 0 AND mode = 'ShareLock' AND granted "
    "AND pid <> pg_backend_pid()"
)

# How long a worker waits (on average) before asking the database for a slot again, when every slot
# was taken or it has just given one back to a waiting worker: a hundredth of a field's budget, so
# waiting for a slot adds little to a field, and workers asking in turn get the slots in turn.
_FACET_SLOT_RETRY_S = _LIGHTNING_FIELD_STATS_BUDGET_S / 100

# A free slot beyond this worker's share that no worker below its share has claimed for this long (a
# waiting worker asks at least every 2 x _FACET_SLOT_RETRY_S) is waited for by nobody who can take it:
# a worker that says it waits is stalled (its event loop blocked). Then free slots are taken anyway, so
# that a stalled worker's share does not sit idle.
_FACET_SLOT_UNCLAIMED_S = 4 * _FACET_SLOT_RETRY_S


async def _database_slot_count(connection) -> int:
    """How many facet scans the database runs at once: its own `max_parallel_workers`, the number of
    processes it is configured to run queries on in parallel (the server value, which every worker
    reads alike; a session's SET does not change it), and like the server, no more than its
    `max_worker_processes`, the pool of background processes that parallel workers are drawn from. At
    least one.

    0 turns parallel query off; it does not make the server any smaller. Then K is
    `max_worker_processes`, sized to the same CPUs."""
    rows = await connection.fetch(
        "SELECT name, reset_val FROM pg_settings WHERE name IN ('max_parallel_workers', 'max_worker_processes')",
    )
    settings = {row['name']: int(row['reset_val'] or 0) for row in rows}
    workers, processes = settings.get('max_parallel_workers', 0), settings.get('max_worker_processes', 0)
    return max(1, min(workers, processes) if workers else processes)


class _SlotGrant(int):
    """A slot key as take() granted it, with the serial number of that grant. The same key number
    granted again (after it was given back, or won again on a new connection after the old one was
    lost) is another grant, so a field that still has the old one neither hands the number on nor gives
    it back: it is the new holder's slot."""

    serial: int


class _DatabaseSlots:
    """The facet slots of the whole database, shared by every worker process: one
    session advisory lock (_FACET_SLOT_LOCK, k) per slot, k < _database_slot_count. A worker holds its
    slots on one connection of its pool, taken while it holds any, so a worker that dies frees them
    with its connection. On the same connection it says how many of its requests wait for a slot (want)."""

    def __init__(self, pool):
        self.pool = pool
        self.connection: Any = None
        self.held: Dict[int, int] = {}  # key -> the serial of its grant
        self.grants = 0  # serials of the grants so far
        self.wanting: set = set()  # second keys of the shared "wanted" locks held: one per waiting request
        self.size: Optional[int] = None
        self.lock = asyncio.Lock()
        self.unclaimed_since: Optional[float] = None  # since when free slots beyond the share were found

    async def take(self, requests: int = 1) -> Optional[_SlotGrant]:
        """A free slot of the database for this worker's `requests` waiting requests, or None when every one
        is taken or this worker already holds its share while other workers' fields wait.
        A worker holding none is always within its share, so every waiting worker gets a slot in turn,
        however many requests wait. Slots beyond the share left unclaimed for _FACET_SLOT_UNCLAIMED_S are
        taken anyway."""
        async with self.lock:
            beyond = False
            try:
                if self.connection is None:
                    self.connection = await self.pool.acquire()
                if self.size is None:
                    self.size = await _database_slot_count(self.connection)
                # The CASE keeps a slot this worker already holds from being taken twice (advisory locks
                # nest per session); LIMIT 1 stops at the first one won.
                key = await self.connection.fetchval(
                    "SELECT k FROM generate_series(0, $2::int - 1) k "
                    "WHERE CASE WHEN k = ANY($3::int[]) THEN false ELSE pg_try_advisory_lock($1, k) END LIMIT 1",
                    _FACET_SLOT_LOCK,
                    self.size,
                    sorted(self.held),
                )
                if key is not None and self.held:
                    # Only a free slot costs the share check: most asks find none while others wait.
                    # Beyond the share, it is free again at once, for a worker below its share.
                    share = await self._share(requests)
                    beyond = share is not None and len(self.held) >= share
                    if beyond:
                        now = _time.monotonic()
                        self.unclaimed_since = self.unclaimed_since or now
                        if now - self.unclaimed_since < _FACET_SLOT_UNCLAIMED_S:
                            await self.connection.execute('SELECT pg_advisory_unlock($1, $2)', _FACET_SLOT_LOCK, key)
                            key = None
            except Exception:  # noqa: BLE001 - a lost connection frees its slots; try again later
                logging.getLogger("fridge.errors").warning("facet slot connection lost", exc_info=True)
                await self._drop()
                return None
            if not beyond:
                self.unclaimed_since = None  # no free slot, or one within the share
            if key is None:
                await self._put_back_if_idle()
                return None
            self.grants += 1
            self.held[key] = self.grants
            grant = _SlotGrant(key)
            grant.serial = self.grants
            return grant

    def _holds(self, key: int) -> bool:
        """Whether this worker still holds `key` for that grant. Only a grant as take() returned it can be
        held: a bare number could stand for any grant of it, an old one included."""
        return self.connection is not None and isinstance(key, _SlotGrant) and self.held.get(key) == key.serial

    async def keep(self, key: int, requests: int = 1) -> bool:
        """Keeps `key` for the next field of this worker's `requests` waiting requests
        when it still holds it and no more than its share, instead of giving the slot back and asking for
        one again after every field. Otherwise False, and the caller no longer holds `key` and must not
        give it back (its number may be another field's by then): a slot beyond the share is given
        back here, under the same lock, so that fields ending together each count the slots the others
        gave back; and a slot lost with the connection is already free."""
        async with self.lock:
            if not self._holds(key):
                return False
            try:
                share = await self._share(requests)
                if share is None or len(self.held) <= share:
                    return True
                await self.connection.execute('SELECT pg_advisory_unlock($1, $2)', _FACET_SLOT_LOCK, int(key))
            except Exception:  # noqa: BLE001 - a lost connection frees its slots; give this one up
                logging.getLogger("fridge.errors").warning("facet slot connection lost", exc_info=True)
                await self._drop()
                return False
            del self.held[key]
            await self._put_back_if_idle()
            return False

    async def _share(self, requests: int) -> Optional[Fraction]:
        """This worker's share of the K slots while other workers' fields wait: K x its waiting requests /
        the waiting requests of every worker, its own included. Its own are the count its
        caller passes, not its "wanted" locks: at its capacity a worker holds none, so that the others do
        not give it slots it cannot use, but its share still counts every request it serves. The
        share does not depend on the slots the others hold at the moment, which lag behind: a worker takes
        a slot only when it next asks. None while no other worker's fields wait, which an O(1) lock probe
        tells without reading pg_locks: then every slot may be this worker's. Called holding
        self.lock."""
        if await self.connection.fetchval(_FACET_NOBODY_ELSE_WANTS, _FACET_SLOT_LOCK, _FACET_SLOT_WANTED):
            return None
        others = await self.connection.fetchval(_FACET_OTHERS_WAITING, _FACET_SLOT_LOCK)
        mine = max(1, requests)
        return Fraction(cast('int', self.size) * mine, mine + others)

    async def want(self, requests: int) -> None:
        """Says how many of this worker's requests have fields waiting for a slot (0: none): one shared
        advisory lock per request, so any number of workers can say it at once, the others can count
        them, and a worker that dies stops saying it."""
        keys = {_FACET_SLOT_WANTED - i for i in range(requests)}
        async with self.lock:
            if keys == self.wanting or (not keys and self.connection is None):
                return
            try:
                if self.connection is None:
                    self.connection = await self.pool.acquire()
                taken = set()
                if keys - self.wanting:
                    # A key another worker's probe holds at this instant is not taken: asked again next time.
                    taken = set(
                        await self.connection.fetchval(
                            "SELECT coalesce(array_agg(k), '{}') FROM unnest($2::int[]) k "
                            "WHERE pg_try_advisory_lock_shared($1, k)",
                            _FACET_SLOT_LOCK,
                            sorted(keys - self.wanting),
                        ),
                    )
                if self.wanting - keys:
                    await self.connection.execute(
                        'SELECT pg_advisory_unlock_shared($1, k) FROM unnest($2::int[]) k',
                        _FACET_SLOT_LOCK,
                        sorted(self.wanting - keys),
                    )
            except Exception:  # noqa: BLE001 - closing the connection frees every lock it held
                logging.getLogger("fridge.errors").warning("facet slot connection lost", exc_info=True)
                await self._drop()
                return
            self.wanting = (self.wanting & keys) | taken
            await self._put_back_if_idle()

    async def give(self, key: int) -> None:
        async with self.lock:
            if not self._holds(key):
                return  # given back already, or lost with its connection: the number may be another's now
            try:
                await self.connection.execute('SELECT pg_advisory_unlock($1, $2)', _FACET_SLOT_LOCK, int(key))
            except Exception:  # noqa: BLE001 - closing the connection frees every slot it held
                logging.getLogger("fridge.errors").warning("facet slot connection lost", exc_info=True)
                await self._drop()
                return
            del self.held[key]
            await self._put_back_if_idle()

    async def _put_back_if_idle(self) -> None:
        if self.held or self.wanting:
            return
        connection, self.connection = self.connection, None
        if connection is not None:
            await self.pool.release(connection)

    async def _drop(self) -> None:
        connection, self.connection = self.connection, None
        self.held.clear()  # the lost connection's grants: the numbers may be won again, as new grants
        self.wanting = set()
        if connection is not None:
            connection.terminate()
            await self.pool.release(connection)


class _FacetSlots:
    """A worker's turn-taking for facet requests' field scans. Waiting fields
    are queued per request and the requests take turns, so one user's 300-field batch cannot queue
    another user's 3 fields behind it. A field runs once it has one of the database's slots
    (_DatabaseSlots), and a worker runs at most a quarter of its pool at once, so the rest of the pool
    stays free for the grid and counts. A field's budget starts when it has its slot.

    A field that ends hands its slot straight to the next waiting field of this worker, unless this
    worker holds more than its share while other workers' fields wait: then it
    gives the slot back to the database, so every waiting request gets about the same share of the
    slots, whichever worker it is in."""

    def __init__(self, capacity: int, database: Any):
        self.capacity = capacity
        self.database = database
        self.busy = 0
        self.waiting: Dict[object, Deque[asyncio.Future]] = {}  # request -> its waiting fields, in order
        self.turns: Deque[object] = collections.deque()  # requests with waiting fields; next first
        self.wake = asyncio.Event()
        self.dispatcher: Optional[asyncio.Future] = None
        self.gave_back = False

    @asynccontextmanager
    async def slot(self, owner: object):
        key = await self._acquire(owner)
        try:
            yield
        finally:
            await self._release(key)

    async def _acquire(self, owner: object) -> int:
        waiter = asyncio.get_running_loop().create_future()
        queue = self.waiting.setdefault(owner, collections.deque())
        if not queue:
            self.turns.append(owner)
        queue.append(waiter)
        self._dispatch()
        try:
            return await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                asyncio.ensure_future(self._release(waiter.result()))  # granted as it was cancelled
            else:
                self._forget(owner, waiter)
            raise

    def _forget(self, owner: object, waiter: asyncio.Future) -> None:
        queue = self.waiting.get(owner)
        if queue is None:
            return
        if waiter in queue:
            queue.remove(waiter)
        if not queue:
            del self.waiting[owner]
            if owner in self.turns:
                self.turns.remove(owner)

    def _next_waiter(self) -> Optional[asyncio.Future]:
        while self.turns:
            owner = self.turns.popleft()
            queue = self.waiting[owner]
            waiter = queue.popleft()
            if queue:
                self.turns.append(owner)  # this request waits for its next turn behind the others
            else:
                del self.waiting[owner]
            if not waiter.done():
                return waiter
        return None

    def _dispatch(self) -> None:
        self.wake.set()
        if self.dispatcher is None or self.dispatcher.done():
            self.dispatcher = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        """Takes the database's slots for this worker's waiting fields, one request's turn at a time."""
        while True:
            if not self.turns:
                if not self.database.wanting:
                    return  # nothing awaited since the check: a new field starts a new run
                await self.database.want(0)
                continue
            self.wake.clear()
            if self.busy >= self.capacity:
                await self.database.want(0)  # this worker could not run another field now
                await self.wake.wait()
                continue
            if self.gave_back:
                # Just gave a slot back to a waiting worker: let it have the slot first.
                self.gave_back = False
                await asyncio.sleep(random.uniform(0, 2 * _FACET_SLOT_RETRY_S))
                continue
            key = await self.database.take(len(self.turns))
            if key is None:
                # Every slot is taken, or this worker holds its share: say how many of its requests wait,
                # so that workers holding more than their share give slots back after a field, and ask
                # again a little later (or when this worker's own fields are all served).
                await self.database.want(len(self.turns))
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), random.uniform(0, 2 * _FACET_SLOT_RETRY_S))
                continue
            waiter = self._next_waiter()
            if waiter is None:
                await self.database.give(key)
                continue
            self.busy += 1
            waiter.set_result(key)

    async def _release(self, key: int) -> None:
        # Shielded: a field cancelled as it ends still hands its slot on or gives it back.
        await asyncio.shield(self._hand_on(key))

    async def _hand_on(self, key: int) -> None:
        held = True  # whether this field still holds `key`: keep() may give it back itself
        if self.turns:
            held = await self.database.keep(key, len(self.turns))
            if held:
                waiter = self._next_waiter()  # the next request's turn, as _run would choose
                if waiter is not None:
                    waiter.set_result(key)
                    if not self.turns:
                        self.wake.set()  # _run stops asking for slots now
                    return
        self.busy -= 1
        self.gave_back = bool(self.turns)
        try:
            if held:  # never after keep() gave it back: _run may have won the same number since
                await self.database.give(key)
        finally:
            self._dispatch()


_facet_slots_state: Optional[tuple[asyncio.AbstractEventLoop, _FacetSlots]] = None


def _facet_slots() -> _FacetSlots:
    """This worker's facet turn-taking: at most a quarter of its connection pool at once, within the
    database's slots, which every worker shares."""
    global _facet_slots_state
    loop = asyncio.get_running_loop()
    if _facet_slots_state is None or _facet_slots_state[0] is not loop:
        _facet_slots_state = (loop, _FacetSlots(max(1, db.pool.get_max_size() // 4), _DatabaseSlots(db.pool)))
    return _facet_slots_state[1]


def _facet_failure(error: BaseException, what: str) -> Dict[str, str]:
    """A failure answered on a field (or on a stream's remaining fields) instead of the request.

    An HTTPException keeps its detail, which is meant for clients. Anything else is logged with its
    traceback under an error_id and, in production, answered without its text, as the app's
    unhandled_exception_handler does.
    """
    if isinstance(error, HTTPException):
        return {'error': str(error.detail)}
    error_id = uuid.uuid4().hex[:12]
    logging.getLogger("fridge.errors").error("%s failed, error %s", what, error_id, exc_info=error)
    message = "Could not compute the filtered values" if production() else f"{type(error).__name__}: {error}"
    return {'error': message, 'error_id': error_id}


def _refusal(error: asyncpg.ObjectNotInPrerequisiteStateError) -> Dict[str, Any]:
    """The 409 body of a refusal (SQLSTATE 55000: a concept being rescored, a dataset being deleted), as the app's
    handler answers it and as a facet stream answers it for the fields it had not answered yet."""
    return {'detail': str(error).splitlines()[0]}


def _finite_bounds(entry: Dict[str, Any]) -> Dict[str, Any]:
    """A min/max that is NaN or ±Infinity (JSON has neither) becomes null."""
    for key in ('min', 'max'):
        value = entry.get(key)
        if isinstance(value, float) and not math.isfinite(value):
            entry[key] = None
    return entry


async def _facet_query(coro, budget_s: Optional[float]) -> tuple[str, Any]:
    """Run one filtered facet query and return (provenance, result).

    Like `_safe_filter_query`, but tells a timeout ('out_of_budget') from a Postgres error
    ('cached'); the caller keeps the cached values for both. A successful empty result is 'exact'.
    """
    try:
        return 'exact', await (coro if budget_s is None else asyncio.wait_for(coro, timeout=budget_s))
    except asyncio.TimeoutError:
        return 'out_of_budget', None
    except asyncpg.ObjectNotInPrerequisiteStateError:
        raise  # a refusal (a concept being rescored): the request's 409
    except asyncpg.PostgresError:
        return 'cached', None


async def _safe_filter_query(coro, *, budget_s: Optional[float] = None):
    """Run a filter query, returning None when the caller should fall back
    to cached values. A successful empty result is `[]`, not None — the
    distinction matters for the fallback decision.

    `budget_s` (optional): cap individual query time; on timeout return
    None so the caller falls back. Used by field-stats in lightning_mode
    to keep filter-sidebar latency bounded under broad filters. When
    None, no local cap applies (asyncpg's command_timeout is the ceiling).

    Postgres-side errors return None. Programming errors propagate so
    they aren't silently swallowed."""
    import asyncpg

    try:
        if budget_s is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=budget_s)
    except asyncpg.ObjectNotInPrerequisiteStateError:
        raise  # a refusal (concept being rescored), not a slow query to fall back from
    except (asyncpg.PostgresError, asyncio.TimeoutError):
        return None


# Above this many matching rows, per-field faceted recompute (one filtered scan
# PER promoted field, fanned out across ~all sidebar fields) is too slow to run
# interactively, so lightning_mode skips it and falls back to cached unfiltered
# values. A broad filter barely changes the available values anyway — facets
# only meaningfully narrow when the filter is SELECTIVE, which is also the cheap
# case (an indexed equality like source_id resolves to a handful of rows). This
# keeps a broad date range from triggering a separate whole-set DISTINCT/MIN-MAX scan
# for every field — see _filtered_set_is_broad.
_LIGHTNING_FACET_MAX_ROWS = 100_000

# The selectivity probe is itself a query, so cap it — it must never become the
# new freeze. The LIMIT makes it early-exit (~100ms even on million-row
# matches); the budget guards against a loaded DB.
_LIGHTNING_PROBE_BUDGET_S = 2.0

# Per-keystroke autocomplete cap. On an unindexed field the value GROUP BY is a full
# seq scan; above this budget we serve the cached preview instead of hanging the pool.
_AUTOCOMPLETE_BUDGET_S = 2.0


async def _filtered_set_is_broad(
    dataset_id: int,
    filter_obj: Dict[str, Any],
    threshold: int,
    budget_s: Optional[float] = None,
) -> Optional[bool]:
    """Return True if the filtered row set has more than `threshold` rows.

    Uses an early-exit count (LIMIT threshold+1) so the probe costs roughly
    O(threshold) index entries no matter how broad the filter is — ~100ms even
    when millions match — instead of counting the entire set. On timeout or
    error we conservatively return True: better to show cached unfiltered values
    than to risk the multi-second per-field fan-out the probe exists to prevent.

    A `budget_s` shorter than the probe's own (what is left of the sidebar request's shared
    budget) only cuts the probe off: that timeout returns None, undecided.
    """
    f_args: List[Any] = [dataset_id]
    f_sql, _ = parse_filter_node(
        filter_obj,
        f_args,
        2,
        None,
        numeric_indexed_fields=await get_promoted_numeric_fields(dataset_id),
        dataset_id=dataset_id,
        array_columns=await get_dataset_array_columns(dataset_id),
    )
    if not f_sql or f_sql == "TRUE":
        return False
    state, n = await _facet_query(
        db.fetchval(
            f"SELECT count(*) FROM ("
            f"  SELECT 1 FROM samples WHERE dataset_id = $1 AND {f_sql} "
            f"  LIMIT {int(threshold) + 1}"
            f") t",
            *f_args,
        ),
        _LIGHTNING_PROBE_BUDGET_S if budget_s is None else min(budget_s, _LIGHTNING_PROBE_BUDGET_S),
    )
    if state == 'out_of_budget' and budget_s is not None and budget_s < _LIGHTNING_PROBE_BUDGET_S:
        return None
    if n is None:
        return True
    return n > threshold


def _grain_predicate_only(node) -> bool:
    """True if an active filter constrains only media_type: the grain predicate the UI wraps every
    view in (withGrain), with no view stage. That is the default view, not a filter the user chose."""
    if not isinstance(node, dict):
        return False  # malformed: let the normal filter parser answer 400, as main does
    if 'logic' in node or 'filters' in node:
        return all(_grain_predicate_only(child) for child in node.get('filters', []) if has_active_filters(child))
    return node.get('field') == 'media_type'


def has_active_filters(node):
    """True if a filter node actually contains constraints (vs. an empty logic wrapper).

    Module-level so it is in scope for every handler (get_dataset_schema and the
    field-stats impl both call it). Pure/recursive — no closure dependencies.
    """
    if not node:
        return False
    # Mirror parse_filter_node: a node with `filters` but no `logic` is an
    # implicit-AND wrapper, so an empty `{'filters': []}` is NOT an active filter.
    if 'logic' in node or 'filters' in node:
        return any(has_active_filters(f) for f in node.get('filters', []))
    return True  # Leaf node is a filter


# A promoted (indexed) field's value list MUST be the COMPLETE distinct set — the whole
# point of indexing a field is that enumerating its values is cheap, so the sidebar/schema
# should show every value, not a stale sample. Non-promoted fields keep the bounded cache
# (dataset_field_stats.unique_values, built from only the 100k most-recent rows — which is
# why older values could disappear from a country facet).
# Above this many distinct values a field is not a useful facet; we stop and let the UI
# live-search the rest via the autocomplete endpoint.
_ENUM_VALUE_CAP = 500


async def _promoted_distinct_values(
    dataset_id: int,
    field_name: str,
    is_numeric: bool,
) -> Optional[tuple[List[str], bool]]:
    """Complete distinct values for a PROMOTED field via a bounded index skip-scan.

    Uses a recursive loose-index-scan over the per-dataset partial btree
    (idx_samples_meta_N_field): one index descent per distinct value, self-limited to
    _ENUM_VALUE_CAP+1. So it costs a few ms for a real facet (a country facet: 10 values,
    ~4ms) and bails cheaply on a high-cardinality field (source_id: millions of distinct values →
    stops at 501 in ~14ms) instead of materializing the whole column.

    Returns (values, truncated); truncated=True means there are more than
    _ENUM_VALUE_CAP distinct values. Returns None on timeout/DB error (5s budget) so
    the caller falls back to the cached list — e.g. a field flagged promoted whose
    index is actually missing, where the skip-scan degrades to a seq scan.

    dataset_id is inlined as a validated int literal so the planner matches the partial
    index's `WHERE dataset_id = N` predicate (a bound parameter forces a generic plan
    that ignores the partial index). field_name is validated here too.
    """
    _validate_field_name(field_name)
    expr = f"(metadata->>'{field_name}')::float" if is_numeric else f"metadata->>'{field_name}'"
    ds = int(dataset_id)
    # The inner subquery orders by the EXPRESSION, not the CTE alias `v` — `v` there
    # would resolve to the outer constant t.v, ordering by a constant and defeating
    # both the index and the distinct-stepping (returns arbitrary rows, slowly).
    sql = f"""
        WITH RECURSIVE t AS (
            (SELECT {expr} AS v FROM samples
             WHERE dataset_id = {ds} AND {expr} IS NOT NULL
             ORDER BY {expr} LIMIT 1)
            UNION ALL
            SELECT (SELECT {expr} FROM samples
                    WHERE dataset_id = {ds} AND {expr} > t.v
                    ORDER BY {expr} LIMIT 1)
            FROM t WHERE t.v IS NOT NULL
        )
        SELECT v FROM t WHERE v IS NOT NULL LIMIT {_ENUM_VALUE_CAP + 1}
    """
    rows = await _safe_filter_query(db.fetch(sql), budget_s=_LIGHTNING_FIELD_STATS_BUDGET_S)
    if rows is None:
        return None
    truncated = len(rows) > _ENUM_VALUE_CAP
    vals = rows[:_ENUM_VALUE_CAP]
    if is_numeric:
        # Guard int(): a promoted numeric field can hold a non-finite value (metadata text
        # 'NaN'/'Infinity' that ::float accepts), and int(nan)/int(inf) raise — this runs
        # OUTSIDE _safe_filter_query, so an unguarded raise 500s the whole field-stats
        # response. Non-finite values render as their str() ('nan'/'inf') instead.
        return [str(int(v)) if math.isfinite(v) and v == int(v) else str(v) for v in (r['v'] for r in vals)], truncated
    return [r['v'] for r in vals], truncated


def _field_stats_filter_decision(filter_active: bool, filtered_broad: bool, is_promoted: bool) -> tuple[bool, bool]:
    """Per-field field-stats gate: returns (recompute filtered MIN/MAX, recompute filtered DISTINCT).

    - MIN/MAX is recomputed when the filtered set is small/selective (`filter_active and not broad` —
      cheap to scan for ANY field) OR the field is indexed (an index probe is cheap even on a broad
      set). This is what makes the simple-filter datetime/number slider reflect the active global
      filter instead of the dataset-wide range.
    - DISTINCT values are recomputed ONLY on a small set — the broad-set DISTINCT fan-out (one scan
      per field across the sidebar) is the ~12s freeze the lightning cap exists to prevent.
    - `filtered_broad` already folds in lightning mode (it is False in non-lightning mode), so
      everything recomputes when the user opts out of lightning.
    """
    recompute = filter_active and not filtered_broad
    return (recompute or (filter_active and is_promoted), recompute)


async def _get_dataset_field_stats_impl(
    id: int,
    filter_obj: Optional[Dict[str, Any]],
    lightning_mode: bool = True,
    fields: Optional[List[str]] = None,
    *,
    known_broad: Optional[bool] = None,
    emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    report: Optional[Dict[str, Any]] = None,
):
    """Sidebar field schema with per-field facet provenance (see _LIGHTNING_SIDEBAR_BUDGET_S).

    `fields` makes this the facet request for opened sections: only those fields are returned
    and only their filtered facets are computed. Without it a lightning-mode request computes
    no filtered facets, and lightning-off computes every eligible one, as before.

    A facet request computes each field on its own, in the worker's facet slots: one field's failure
    is that field's answer (an `error`), and `emit` receives each field's entry as soon as it is final
    (the rest at the end). Its selectivity probe runs once, when a field first needs it (a promoted
    MIN/MAX never does), and not at all when `known_broad` brings the sidebar request's decision.
    `report` receives the lightning sidebar request's decision as `breadth` (None: undecided).
    """
    loop = asyncio.get_running_loop()
    wanted = None if fields is None else set(fields)
    filter_active = bool(filter_obj and has_active_filters(filter_obj))
    # The default view (the grain predicate alone) keeps computing its eligible facets in the sidebar
    # request: on a dataset with objects they are the image grain's, not the dataset's.
    default_view = filter_active and _grain_predicate_only(cast('Dict[str, Any]', filter_obj))
    computing = wanted is not None or not lightning_mode or default_view
    deadline = None if computing else loop.time() + _LIGHTNING_SIDEBAR_BUDGET_S

    def budget(cap: float) -> float:
        """A query's own budget, cut to what is left of the sidebar request's shared budget."""
        return cap if deadline is None else max(0.0, min(cap, deadline - loop.time()))

    def is_wanted(name: Optional[str]) -> bool:
        return wanted is None or name in wanted

    # Check dataset exists
    dataset_check = await db.fetchrow("SELECT id, deleting FROM datasets WHERE id = $1", id)
    if not dataset_check:
        raise HTTPException(status_code=404, detail="Dataset not found")
    # Filtered sidebar values fall back to cached ones when a sub-query fails; a concept whose
    # scores are being rebuilt must refuse (409) instead of quietly showing unfiltered values.
    await require_ready_concepts(filter_obj)

    # Best-effort timeout per filter sub-query in lightning_mode; off in
    # non-lightning mode where the user has explicitly opted into waiting
    # for accurate filtered values.
    filter_budget_s = _LIGHTNING_FIELD_STATS_BUDGET_S if lightning_mode else None

    stats = await field_stats(db, id)

    # Selectivity gate (lightning_mode only). Decide ONCE whether the active filter is selective
    # enough that a per-field faceted recompute (one filtered scan PER field, fanned out across the
    # whole sidebar) will be fast. A BROAD set (>100k rows) can make each scan expensive, so we
    # only scan it for INDEXED fields' MIN/MAX (an index probe, cheap regardless of breadth) — see
    # should_filter_minmax below. A SELECTIVE set (or non-lightning mode) is cheap to scan for ANY
    # field. See _filtered_set_is_broad.
    # None = undecided. A lightning sidebar request probes last, only to label fields pending or
    # cached; the shared budget can cut it off undecided.
    filtered_broad: Optional[bool] = None if filter_active and lightning_mode and not computing else False
    facet_request = wanted is not None
    probe: Optional[asyncio.Future] = None
    if filter_active and lightning_mode and computing:
        if known_broad is not None:
            filtered_broad = known_broad
        elif facet_request:
            filtered_broad = None  # decided by `breadth()` when a field first needs it
        else:  # the default view
            filtered_broad = await _filtered_set_is_broad(
                id,
                cast('Dict[str, Any]', filter_obj),
                _LIGHTNING_FACET_MAX_ROWS,
            )

    async def breadth() -> bool:
        """A facet request's breadth: one probe, shared by its fields, started by the first that asks."""
        nonlocal probe
        if filtered_broad is not None:
            return filtered_broad
        if probe is None:
            probe = asyncio.ensure_future(
                _filtered_set_is_broad(id, cast('Dict[str, Any]', filter_obj), _LIGHTNING_FACET_MAX_ROWS),
            )
        return bool(await asyncio.shield(probe))

    # A facet request's per-field work: each job fills its entry and finishes it, in a slot.
    facet_jobs: List[tuple[Dict[str, Any], Callable[[], Any]]] = []
    owner = object()
    emitted: set = set()

    def finish(entry: Dict[str, Any]) -> None:
        """A facet request's field is final: a field the gate keeps dataset-wide is `cached`."""
        entry.setdefault('provenance', 'cached')
        if emit is not None and entry['name'] not in emitted:
            emitted.add(entry['name'])
            emit(entry)

    async def run_facet_job(entry: Dict[str, Any], job) -> None:
        """One field of a facet request; its failure is its own answer, not the request's."""
        try:
            await job()
        except (asyncio.CancelledError, asyncpg.ObjectNotInPrerequisiteStateError):
            raise  # a refusal holds for every field of the request: a 409, or refusal lines in a stream
        except Exception as error:  # noqa: BLE001 - reported on the field, not the request
            entry['provenance'] = 'cached'
            entry.update(_facet_failure(error, f"facet field {entry['name']!r} of dataset {id}"))
        finish(entry)

    registered_names = {stat['field_name'] for stat in stats if stat.get('origin') != 'system'}
    filter_numeric_fields = {
        stat['field_name'] for stat in stats if stat['is_promoted'] and stat['field_type'] == 'number'
    }
    value_completeness = {}
    # Filter-sensitive entries and the gate that decides their facet: (entry, is_promoted, 0 for
    # MIN/MAX or 1 for DISTINCT). Labelled at the end unless a computed result already labelled them.
    facet_entries: List[tuple[Dict[str, Any], bool, int]] = []

    # created_at bounds drive the Simple Filters datetime slider (ingestion time). Indexed on
    # (dataset_id, created_at) so MIN/MAX is an index probe, not a multi-million-row seq scan on this
    # hot endpoint. These GLOBAL bounds are the baseline; when the active filter is selective enough
    # they're overridden with FILTERED bounds below (so the slider reflects the global filter).
    created_at_field = {"name": "created_at", "type": "datetime"}
    ca_bounds = await db.fetchrow(
        "SELECT MIN(created_at) AS mn, MAX(created_at) AS mx FROM samples WHERE dataset_id = $1",
        id,
    )
    if ca_bounds and ca_bounds['mn'] is not None:
        created_at_field['min'] = ca_bounds['mn'].isoformat()
        created_at_field['max'] = ca_bounds['mx'].isoformat()

    # Always include built-in fields with their enum values
    schema: List[Dict[str, Any]] = [
        {"name": "id", "type": "number"},
        {"name": "filepath", "type": "filepath"},
        created_at_field,
        await _last_modified_field(id, lightning_mode),
    ]

    # Helper to find stat in cached list
    def get_cached_stat(name):
        for s in stats:
            if s['field_name'] == name:
                return s
        return None

    async def get_or_cache_annotation_labels(field_name: str, json_field: str) -> List[str]:
        if json_field not in registered_names or not _is_safe_field_name(json_field):
            return []
        cached = get_cached_stat(field_name)
        if cached is None:
            cached = await db.fetchrow(
                'SELECT unique_values, values_complete FROM dataset_field_stats WHERE dataset_id=$1 AND field_name=$2',
                id,
                field_name,
            )
        if cached and (cached['unique_values'] or cached.get('values_complete')):
            value_completeness[field_name] = cached.get('values_complete', False)
            return cached['unique_values'] or []
        try:
            labels, complete = await cached_distinct_values(
                db.pool,
                id,
                field_name,
                f"""
                SELECT val FROM (
                    SELECT DISTINCT jsonb_array_elements(metadata->'{json_field}')->>'label' AS val
                    FROM samples WHERE dataset_id = $1 AND metadata ? '{json_field}'
                      AND jsonb_typeof(metadata->'{json_field}') = 'array'
                ) labels WHERE val IS NOT NULL AND val <> '' ORDER BY val LIMIT 201
                """,
                [id],
            )
            value_completeness[field_name] = complete
            return labels
        except Exception as error:
            print(f"[field-stats] Failed to fetch {field_name} for dataset {id}: {error}")
            return []

    async def get_or_cache_segmentation_labels(dataset_id: int, get_cached_stat_fn) -> List[str]:
        if 'segmentation' not in registered_names:
            return []
        cached = get_cached_stat_fn('segmentation.labels')
        if cached and (cached['unique_values'] or cached.get('values_complete')):
            value_completeness['segmentation.labels'] = cached.get('values_complete', False)
            return cached['unique_values'] or []
        try:
            labels, complete = await cached_distinct_values(
                db.pool,
                dataset_id,
                'segmentation.labels',
                """
                SELECT DISTINCT jsonb_array_elements_text(metadata->'segmentation'->'labels') AS val
                FROM samples WHERE dataset_id = $1 AND metadata ? 'segmentation'
                  AND metadata->'segmentation' ? 'labels'
                  AND jsonb_typeof(metadata->'segmentation'->'labels') = 'array'
                ORDER BY val LIMIT 201
                """,
                [dataset_id],
            )
            value_completeness['segmentation.labels'] = complete
            return labels
        except Exception as error:
            print(f"[field-stats] Failed to fetch segmentation.labels for dataset {dataset_id}: {error}")
            return []

    async def compute_filtered_annotation_labels(json_field: str) -> List[str]:
        """
        Compute unique annotation labels (detections/polygons) with filter constraints applied.
        Uses the current filter_obj to limit results to matching samples only.
        """
        args = [id]
        arg_idx = 2
        # Only called when should_filter_annotations is truthy, i.e. filter_obj is set.
        filter_sql, _ = parse_filter_node(
            cast('Dict[str, Any]', filter_obj),
            args,
            arg_idx,
            None,
            numeric_indexed_fields=filter_numeric_fields,
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )

        try:
            rows = await db.fetch(
                f"""
                SELECT label FROM (
                    SELECT DISTINCT jsonb_array_elements(metadata->'{json_field}')->>'label' AS label
                    FROM samples
                    WHERE dataset_id = $1
                      AND metadata ? '{json_field}'
                      AND jsonb_typeof(metadata->'{json_field}') = 'array'
                      AND {filter_sql}
                ) AS labels
                WHERE label IS NOT NULL AND label <> ''
                ORDER BY label
                LIMIT 201
            """,
                *args,
            )
            return [r['label'] for r in rows if r['label']]
        except asyncpg.ObjectNotInPrerequisiteStateError:
            raise  # a refusal (concept being rescored) is a 409, not an empty label list
        except Exception as e:
            print(f"[field-stats] Error computing filtered {json_field} labels: {e}")
            return []

    async def get_or_cache_discovery(cache: str) -> List[str]:
        return await discover_fields(db.pool, id, cache, get_cached_stat(cache))

    async def compute_filtered_segmentation_labels() -> List[str]:
        """
        Compute unique segmentation labels with filter constraints applied.
        Segmentation labels are stored as a simple string array in metadata->segmentation->labels.
        """
        args = [id]
        arg_idx = 2
        # Only called when should_filter_annotations is truthy, i.e. filter_obj is set.
        filter_sql, _ = parse_filter_node(
            cast('Dict[str, Any]', filter_obj),
            args,
            arg_idx,
            None,
            numeric_indexed_fields=filter_numeric_fields,
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )

        try:
            rows = await db.fetch(
                f"""
                SELECT DISTINCT jsonb_array_elements_text(metadata->'segmentation'->'labels') as label
                FROM samples
                WHERE dataset_id = $1
                AND metadata ? 'segmentation'
                AND metadata->'segmentation' ? 'labels'
                AND jsonb_typeof(metadata->'segmentation'->'labels') = 'array'
                AND {filter_sql}
            """,
                *args,
            )
            return [r['label'] for r in rows if r['label']]
        except asyncpg.ObjectNotInPrerequisiteStateError:
            raise  # a refusal (concept being rescored) is a 409, not an empty label list
        except Exception as e:
            print(f"[field-stats] Error computing filtered segmentation labels: {e}")
            return []

    # 1. Media Type
    media_type_stat = get_cached_stat('media_type')
    if media_type_stat and media_type_stat['values_complete'] and media_type_stat['unique_values'] is not None:
        media_values, media_complete = media_type_stat['unique_values'], True
    else:
        media_values, media_complete = await cached_distinct_values(
            db.pool,
            id,
            'media_type',
            """SELECT DISTINCT media_type AS val FROM samples
               WHERE dataset_id = $1 AND media_type IS NOT NULL AND media_type <> '' ORDER BY val""",
            [id],
            cap=None,
        )
    # media_type values are never filtered: always the dataset's.
    schema.append(
        {
            "name": "media_type",
            "type": "enum",
            "values": media_values,
            "values_complete": media_complete,
            "provenance": "cached",
        },
    )

    # The filtered set is small enough (or the user opted out of lightning) to recompute ANY field.
    # Only read when computing, where the probe has already answered.
    recompute_filtered = filter_active and not filtered_broad

    # Override the created_at slider bounds with the FILTERED range when the set is small enough, so
    # the slider reflects the active global filter (fall back to the global bounds on timeout/empty).
    async def filter_created_at() -> None:
        ca_args: List[Any] = [id]
        ca_sql, _ = parse_filter_node(
            cast('Dict[str, Any]', filter_obj),
            ca_args,
            2,
            None,
            numeric_indexed_fields=filter_numeric_fields,
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )
        ca_where = "dataset_id = $1" + (f" AND {ca_sql}" if ca_sql and ca_sql != "TRUE" else "")
        ca_state, ca_filtered = await _facet_query(
            db.fetchrow(f"SELECT MIN(created_at) AS mn, MAX(created_at) AS mx FROM samples WHERE {ca_where}", *ca_args),
            filter_budget_s,
        )
        if ca_filtered and ca_filtered['mn'] is not None:
            created_at_field['min'] = ca_filtered['mn'].isoformat()
            created_at_field['max'] = ca_filtered['mx'].isoformat()
        # A filtered set without rows keeps the dataset's bounds, so it is not an exact answer.
        created_at_field['provenance'] = 'cached' if ca_state == 'exact' and not ca_filtered['mn'] else ca_state

    if facet_request:
        if is_wanted('created_at'):

            async def created_at_job() -> None:
                if filter_obj and filter_active and not await breadth():
                    async with _facet_slots().slot(owner):
                        await filter_created_at()

            facet_jobs.append((created_at_field, created_at_job))
    elif computing and recompute_filtered and filter_obj and is_wanted('created_at'):
        await filter_created_at()
    facet_entries.append((created_at_field, False, 1))

    # 2. Array fields (tags, inferred_tags, etc.) — all handled generically.
    # Scope to THIS dataset so another dataset's dynamic array fields don't leak
    # into this dataset's schema.
    array_cols = await get_dataset_array_columns(id)

    async def fill_array(col_name: str, array_entry: Dict[str, Any], recompute: bool) -> None:
        cached = get_cached_stat(col_name)
        values = (
            cached['unique_values']
            if cached and (cached.get('unique_values') or cached.get('values_complete'))
            else None
        )
        complete = bool(cached and cached.get('values_complete'))
        array_state = None

        # Filter array field values by active filters. On error fall back
        # to whatever cached `values` we already have. Skipped for broad
        # filters (recompute_filtered) so this loop doesn't fan out a scan of
        # the whole filtered set per array field.
        if recompute:
            f_args = [id]
            f_idx = 2
            # recompute_filtered implies filter_obj is set.
            f_sql, _ = parse_filter_node(
                cast('Dict[str, Any]', filter_obj),
                f_args,
                f_idx,
                None,
                numeric_indexed_fields=filter_numeric_fields,
                dataset_id=id,
                array_columns=await get_dataset_array_columns(id),
            )
            array_state, filtered = await _facet_query(
                db.fetch(
                    f"""
                WITH filtered AS (
                    SELECT {col_name} FROM samples
                    WHERE dataset_id = $1 AND {col_name} IS NOT NULL
                    AND array_length({col_name}, 1) > 0 AND {f_sql}
                )
                SELECT DISTINCT unnest({col_name}) as val FROM filtered ORDER BY val LIMIT 201
            """,
                    *f_args,
                ),
                filter_budget_s,
            )
            if filtered is not None:
                values = [r['val'] for r in filtered[:200] if r['val']]
                complete = len(filtered) <= 200
            # else: filter raised; keep cached `values`

        if values is None:
            values, complete = await cached_distinct_values(
                db.pool,
                id,
                col_name,
                f"""
                SELECT DISTINCT unnest({col_name}) AS val FROM samples
                WHERE dataset_id = $1 AND {col_name} IS NOT NULL AND array_length({col_name}, 1) > 0
                ORDER BY val LIMIT 201
                """,
                [id],
            )

        is_promoted = await db.fetchval(
            "SELECT is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
            id,
            col_name,
        )
        array_entry.update(values=values or [], is_promoted=is_promoted or False, values_complete=complete)
        if array_state is not None:
            array_entry['provenance'] = array_state

    for col_name in sorted(array_cols):
        if not is_wanted(col_name):
            continue
        array_entry: Dict[str, Any] = {"name": col_name, "type": "enum"}
        if facet_request:

            async def array_job(col_name: str = col_name, array_entry: Dict[str, Any] = array_entry) -> None:
                recompute = filter_active and not await breadth()
                async with _facet_slots().slot(owner):
                    await fill_array(col_name, array_entry, recompute)
                array_entry['sortable'] = False

            facet_jobs.append((array_entry, array_job))
        else:
            await fill_array(col_name, array_entry, computing and recompute_filtered)
        facet_entries.append((array_entry, False, 1))
        schema.append(array_entry)

    # Only the sidebar may discover. Complete shape discovery before resolving .label paths.
    discovered = await asyncio.gather(*[get_or_cache_discovery(cache) for cache in DISCOVERY_TYPES])
    by_type = dict(zip(DISCOVERY_TYPES.values(), discovered))
    detection_fields = by_type['detections']
    polyline_fields = by_type['polygons']
    cuboid_fields = by_type['cuboids']
    keypoint_fields = by_type['keypoints']
    heatmap_fields = by_type['heatmap']
    probability_fields = by_type['class_probabilities']
    mask_overlay_fields = by_type['segmentation']
    classification_fields = [name for name in by_type['classification'] if name not in cuboid_fields]
    classification_lists = {
        stat['field_name']
        for stat in stats
        if stat['field_name'] in classification_fields and stat['field_type'] == 'list'
    }
    _laf = sorted(
        name
        for name in set(_LABEL_ARRAY_FIELDS)
        | set(detection_fields)
        | set(polyline_fields)
        | set(keypoint_fields)
        | classification_lists
        if f'{name}.label' not in registered_names
    )
    cached_labels = await asyncio.gather(
        *[get_or_cache_annotation_labels(f'{f}.label', f) for f in _laf],
        get_or_cache_segmentation_labels(id, get_cached_stat),
    )
    array_labels = dict(zip(_laf, cached_labels[:-1]))
    segmentation_labels = cached_labels[-1]
    registered_names = {stat['field_name'] for stat in stats if stat.get('origin') != 'system'}
    heatmap_fields = [name for name in heatmap_fields if name in registered_names]
    probability_fields = [name for name in probability_fields if name in registered_names]
    mask_overlay_fields = [name for name in mask_overlay_fields if name in registered_names]
    cuboid_fields = [name for name in cuboid_fields if name in registered_names]
    keypoint_fields = [name for name in keypoint_fields if name in registered_names]
    detection_fields = [name for name in detection_fields if name in registered_names]
    polyline_fields = [name for name in polyline_fields if name in registered_names]
    classification_fields = [name for name in classification_fields if name in registered_names]
    array_labels = {name: labels if name in registered_names else [] for name, labels in array_labels.items()}
    if 'segmentation' not in registered_names:
        segmentation_labels = []
    has_segmentation_labels = bool(segmentation_labels)

    # If NOT in lightning mode with filters, compute filtered labels in parallel
    should_filter_annotations = not lightning_mode and filter_obj and has_active_filters(filter_obj)
    filtered_labels = set()  # label fields recomputed for the filter (never in lightning mode)
    if should_filter_annotations:

        async def noop_list():
            return []

        present = [f for f in _laf if array_labels[f] and is_wanted(f'{f}.label')]
        filter_segmentation = has_segmentation_labels and is_wanted('segmentation.labels')
        filtered_results = await asyncio.gather(
            *[compute_filtered_annotation_labels(f) for f in present],
            compute_filtered_segmentation_labels() if filter_segmentation else noop_list(),
            return_exceptions=True,
        )
        for filtered_result in filtered_results:
            if isinstance(filtered_result, asyncpg.ObjectNotInPrerequisiteStateError):
                raise filtered_result  # not unfiltered labels next to a grid that refuses the filter
        # Apply filtered results if valid (empty list is a valid result!)
        for i, f in enumerate(present):
            filtered_result = filtered_results[i]
            if isinstance(filtered_result, list):
                array_labels[f] = filtered_result[:200]
                value_completeness[f'{f}.label'] = len(filtered_result) <= 200
                filtered_labels.add(f'{f}.label')
        seg_result = filtered_results[len(present)]
        if filter_segmentation and isinstance(seg_result, list):
            segmentation_labels = seg_result[:200]
            value_completeness['segmentation.labels'] = len(seg_result) <= 200
            filtered_labels.add('segmentation.labels')

    # Add to schema - show field if it EXISTS in dataset, even if current filter returns empty
    for f in _laf:
        if array_labels[f]:
            schema.append(
                {
                    "name": f"{f}.label",
                    "type": "enum",
                    "declared_type": "string",
                    "origin": "system",
                    "values": array_labels[f],
                    "values_complete": value_completeness.get(f'{f}.label', False),
                    "provenance": "exact" if f'{f}.label' in filtered_labels else "cached",
                },
            )

    if has_segmentation_labels:
        schema.append(
            {
                "name": "segmentation.labels",
                "type": "enum",
                "declared_type": "string",
                "origin": "system",
                "values": segmentation_labels,
                "values_complete": value_completeness.get('segmentation.labels', False),
                "provenance": "exact" if 'segmentation.labels' in filtered_labels else "cached",
            },
        )

    # Heatmap overlay fields — each named metadata key whose value is
    # {"type": "heatmap", ...}. Emitted as type="heatmap" so the sidebar
    # can render toggle rows without trying to build a filter UI.
    # Per-field overlay config (UI-set colormap/range/opacity), surfaced with the field so
    # the renderers (modal canvas + grid composite URL) can apply it.
    _cfg_rows = await db.fetch(
        "SELECT field_name, overlay_config FROM dataset_fields WHERE dataset_id = $1 AND overlay_config IS NOT NULL",
        id,
    )
    _overlay_cfgs = {
        r['field_name']: (
            json.loads(r['overlay_config']) if isinstance(r['overlay_config'], str) else r['overlay_config']
        )
        for r in _cfg_rows
    }
    for heatmap_field in heatmap_fields:
        schema.append(
            {
                "name": heatmap_field,
                "type": "heatmap",
                "config": _overlay_cfgs.get(heatmap_field),
            },
        )

    # Class-probability overlays ({"type": "class_probabilities", "probPath": ..., "classes": N}): drawn by the app
    # with a rule, the field's default in segmentation_config.class_rules (class_rules.py).
    for probability_field in probability_fields:
        schema.append(
            {
                "name": probability_field,
                "type": "class_probabilities",
                "config": _overlay_cfgs.get(probability_field),
            },
        )

    # Mask-overlay fields — named metadata keys whose value is {"maskPath": ...}
    # (e.g. semantic_mask RGB masks). Emitted as type="segmentation" so the sidebar
    # Overlays section renders a toggle row. Rendering is client-side in
    # MaskRenderer, which keys off the per-sample {maskPath} value, so no filter
    # UI is built here.
    for mask_field in mask_overlay_fields:
        schema.append(
            {
                "name": mask_field,
                "type": "segmentation",
                "config": _overlay_cfgs.get(mask_field),
            },
        )

    # Classification overlay fields — named metadata keys holding {"label": ...}
    # objects (single Classification or a Classifications list). Emitted as
    # type="classification" so the sidebar Overlays section renders a toggle row;
    # rendering is client-side (ClassificationRenderer badges). A PROMOTED field
    # always wins: it stays a normal filterable field (emitted by the stats loop
    # below) rather than being swallowed into the overlay list — classification
    # discovery is a structural heuristic, so promotion is the user's explicit
    # signal that they want to filter on the field.
    promoted_field_names = {s['field_name'] for s in stats if s['is_promoted']}
    for cls_field in classification_fields:
        if cls_field in promoted_field_names:
            continue
        schema.append(
            {
                "name": cls_field,
                "type": "classification",
            },
        )

    # Get dataset media fields to skip them from filters
    dataset_row = await db.fetchrow("SELECT media_fields FROM datasets WHERE id = $1", id)
    dataset_media_fields = (
        set(dataset_row['media_fields']) if dataset_row and dataset_row['media_fields'] else {'filepath'}
    )
    # Fields whose index is being rebuilt in the background (index_jobs.queue_rebuilds): not indexed until it is.
    from ..index_jobs import rebuild_waiting, rebuilding_fields

    rebuilding = set(await rebuilding_fields(db, id))
    waiting = await rebuild_waiting(db, id, rebuilding) if rebuilding else {}

    # Prepare lists for parallel processing
    number_fields_to_filter: List[tuple[str, Any]] = []
    enum_fields_to_filter: List[tuple[str, Any]] = []
    datetime_fields_to_filter: List[tuple[str, Any]] = []
    field_schemas: Dict[str, Dict[str, Any]] = {}  # field_name -> field_schema

    heatmap_field_set = set(heatmap_fields)
    mask_overlay_field_set = set(mask_overlay_fields)
    keypoint_field_set = set(keypoint_fields)
    classification_field_set = set(classification_fields)

    # Dataset total (global; indexed count) → per-field null_count, so the sidebar only
    # offers a "(no value)" filter for fields that actually have missing samples.
    total_count = await db.fetchval("SELECT count(*) FROM samples WHERE dataset_id = $1", id) or 0

    async def fill_cached(field_schema: Dict[str, Any], stat, kind: str) -> None:
        """A field's facet the gate does not recompute: the dataset's values."""
        if kind == 'number':
            field_schema['min'] = stat['min_value']
            field_schema['max'] = stat['max_value']
        elif kind == 'datetime':
            if stat['unique_values'] and len(stat['unique_values']) >= 2:
                field_schema['min'] = stat['unique_values'][0]
                field_schema['max'] = stat['unique_values'][1]
        elif stat['is_promoted'] and _is_safe_field_name(stat['field_name']):
            # Indexed field → serve the COMPLETE distinct set from the index, not the
            # stale bounded cache. Falls back to the cache only if the skip-scan errors.
            _is_num = stat['field_type'] in ('number', 'float', 'int', 'integer')
            promoted_values = await _promoted_distinct_values(id, stat['field_name'], _is_num)
            if promoted_values is not None:
                field_schema['values'], _truncated = promoted_values
                field_schema['values_complete'] = not _truncated
                if _truncated:
                    field_schema['values_truncated'] = True
            elif stat['unique_values']:
                field_schema['values'] = stat['unique_values']
        elif stat['unique_values']:
            field_schema['values'] = stat['unique_values']
        elif stat['display_type'] == 'enum' and _is_safe_field_name(stat['field_name']):
            # Number field overridden to enum but unique_values not cached — compute inline
            vals = await db.fetch(
                f"""
                SELECT DISTINCT metadata->>'{stat['field_name']}' as val
                FROM samples WHERE dataset_id = $1 AND jsonb_typeof(metadata->'{stat['field_name']}') = 'number'
                ORDER BY val LIMIT 101
            """,
                id,
            )
            field_schema['values'] = [r['val'] for r in vals[:100]]
            field_schema['values_complete'] = len(vals) <= 100

    async def filter_number_field(field_name, stat):
        # Fields are only queued for filtering when recompute_filtered is
        # True, which implies filter_obj is set.
        with field_usage.measure(id, {'facet': [field_name]}):
            state, result = await _facet_query(
                compute_filtered_minmax(id, field_name, cast('Dict[str, Any]', filter_obj)),
                filter_budget_s,
            )
        return (field_name, 'number', state, result, stat)

    async def filter_enum_field(field_name, stat):
        with field_usage.measure(id, {'facet': [field_name]}):
            state, result = await _facet_query(
                compute_filtered_values(id, field_name, cast('Dict[str, Any]', filter_obj)),
                filter_budget_s,
            )
        return (field_name, 'enum', state, result, stat)

    async def filter_datetime_field(field_name, stat):
        if not _queryable_field(field_name):
            return (field_name, 'datetime', 'cached', None, stat)
        # The earliest and latest instants (an indexed field's index answers), as the statistics write them.
        # Without an index, each row's text is read once (instants_from), and MIN and MAX skip rows without an instant.
        promoted, text = bool(stat.get('is_promoted')), _field_text_sql(field_name)
        instant = instant_expr(text, True) if promoted else '_instant'
        args = [id]
        arg_idx = 2
        conditions = ["dataset_id = $1", f"{instant} IS NOT NULL"] if promoted else ["dataset_id = $1"]
        if filter_obj:
            sql, arg_idx = parse_filter_node(
                filter_obj,
                args,
                arg_idx,
                numeric_indexed_fields=filter_numeric_fields,
                dataset_id=id,
                array_columns=await get_dataset_array_columns(id),
            )
            if sql and sql != "TRUE":
                conditions.append(sql)
        where = " AND ".join(conditions)
        source = f"samples WHERE {where}" if promoted else instants_from(text, f"samples WHERE {where}")
        earliest, latest = UTC_TEXT_SQL.format(f'MIN({instant})'), UTC_TEXT_SQL.format(f'MAX({instant})')
        with field_usage.measure(id, {'facet': [field_name]}):
            state, row = await _facet_query(
                db.fetchrow(
                    f"""
                SELECT {earliest} as min_val, {latest} as max_val
                FROM {source}
            """,
                    *args,
                ),
                filter_budget_s,
            )
        if row and row['min_val']:
            return (field_name, 'datetime', state, {'min': row['min_val'], 'max': row['max_val']}, stat)
        return (field_name, 'datetime', state, None, stat)

    def apply_result(field_name, field_type, state, result, stat) -> None:
        """A filtered query's answer on its field. Only an answer that replaced the cached values is
        exact: a filtered set without values keeps them, as do a timeout and an error."""
        if field_name not in field_schemas:
            return
        answered = isinstance(result, list) if field_type == 'enum' else bool(result)
        field_schemas[field_name]['provenance'] = 'cached' if state == 'exact' and not answered else state
        if field_type == 'number':
            if result:
                field_schemas[field_name]['min'] = result['min']
                field_schemas[field_name]['max'] = result['max']
            else:
                field_schemas[field_name]['min'] = stat['min_value']
                field_schemas[field_name]['max'] = stat['max_value']
        elif field_type == 'enum':
            if isinstance(result, list):
                field_schemas[field_name]['values'] = result[:100]
                field_schemas[field_name]['values_complete'] = len(result) <= 100
            elif stat['unique_values']:
                # Filtered query failed/timed out — fall back to cached
                # unfiltered values so the sidebar still shows options.
                field_schemas[field_name]['values'] = stat['unique_values']
        elif field_type == 'datetime':
            if result:
                field_schemas[field_name]['min'] = result['min']
                field_schemas[field_name]['max'] = result['max']
            elif stat['unique_values'] and len(stat['unique_values']) >= 2:
                field_schemas[field_name]['min'] = stat['unique_values'][0]
                field_schemas[field_name]['max'] = stat['unique_values'][1]
            else:
                field_schemas[field_name]['values'] = stat['unique_values']

    async def stat_facet_job(stat, field_schema: Dict[str, Any], kind: str) -> None:
        """A facet request's metadata field. A promoted MIN/MAX is recomputed whatever the breadth, so
        it never waits for the probe; the rest decide with the request's one probe."""
        name = stat['field_name']
        if not _queryable_field(name):
            # A name no query can use: its dataset values, for good (labelled `filterable: false`).
            await fill_cached(field_schema, stat, kind)
            return
        promoted = bool(stat['is_promoted'])
        promoted_range = promoted and kind != 'enum'
        broad = True if promoted_range else await breadth()
        should_filter_minmax, should_filter_distinct = _field_stats_filter_decision(filter_active, broad, promoted)
        query = (
            filter_enum_field
            if kind == 'enum' and should_filter_distinct
            else filter_number_field
            if kind == 'number' and should_filter_minmax
            else filter_datetime_field
            if kind == 'datetime' and should_filter_minmax
            else None
        )
        if query is None:
            await fill_cached(field_schema, stat, kind)  # no scan: no slot
            return
        try:
            if promoted_range:
                # An index read, bounded by its budget: it never waits behind other fields' scans.
                apply_result(*await query(name, stat))
            else:
                async with _facet_slots().slot(owner):
                    apply_result(*await query(name, stat))
        except Exception:
            await fill_cached(field_schema, stat, kind)  # its dataset values stand beside the error
            raise

    for stat in stats:
        if not is_wanted(stat['field_name']):
            continue
        # Skip fields already handled explicitly or registered as media fields
        if stat['field_name'] in ({'media_type', 'tags', 'segmentation.labels'} | {f'{f}.label' for f in _laf}):
            continue
        if stat['field_name'] in (
            '__heatmap_fields__',
            '__probability_fields__',
            '__mask_overlay_fields__',
            '__cuboid_fields__',
            '__keypoint_fields__',
            '__detection_fields__',
            '__polyline_fields__',
            '__classification_fields__',
        ):
            continue  # Internal cache rows; never surfaced to the frontend
        if stat['field_name'] in heatmap_field_set or stat['field_name'] in probability_fields:
            continue  # Already emitted above as an overlay; would otherwise appear as "string"
        if stat['field_name'] in mask_overlay_field_set:
            continue  # Already emitted above as a segmentation overlay; would otherwise appear as "dict"
        if stat['field_name'] in classification_field_set and not stat['is_promoted']:
            continue  # Emitted above as a classification overlay (promoted → kept as a normal field)
        if stat['field_name'] in dataset_media_fields:
            continue

        vector_type = (
            'cuboids'
            if stat['field_name'] in cuboid_fields
            else 'detections'
            if stat['field_name'] in detection_fields
            else 'keypoints'
            if stat['field_name'] in keypoint_field_set
            else 'polygons'
            if stat['field_name'] in polyline_fields
            else None
        )
        field_type = vector_type or presentation_type(stat)
        display_type = stat['display_type']
        original_type = stat['field_type']

        field_schema = {
            "name": stat['field_name'],
            "type": field_type,
            "original_type": original_type,
            "sample_count": stat["sample_count"],
            "is_promoted": stat['is_promoted'] or False,
            "display_type": display_type,
            "declared_type": original_type,
            "origin": stat.get("origin", "metadata"),
            "owner": stat.get("owner", "self"),
            "stats_pending": stat.get("stats_pending", False),
            "values_complete": stat.get("values_complete", False),
        }

        if not _queryable_field(stat['field_name']):
            field_schema['filterable'] = False  # no query can name it: never pending, never asked for
        # parent_id names each object's image: in the schema (the stage editor, the query editor, the SDK), not in the
        # sidebar, so nothing is computed for it there; a facet request that names it still gets it.
        hidden = stat['field_name'] in SIDEBAR_HIDDEN_FIELDS and not facet_request
        if hidden:
            field_schema['sidebar'] = False
        if stat['field_name'] in rebuilding:
            field_schema['index_rebuilding'] = True
            if stat['field_name'] in waiting:  # not started yet: say what for
                field_schema['index_rebuild_waiting'] = waiting[stat['field_name']]

        # null_count: samples with no value for this field — drives the "(no value)"
        # filter option, shown only for PARTIAL fields (some present, some missing).
        _sc = stat['sample_count']
        if _sc is not None and total_count and 0 < _sc < total_count:
            field_schema['null_count'] = total_count - _sc

        promoted = bool(stat['is_promoted'])
        kind = (
            'enum'
            if field_type == 'enum'
            else 'number'
            if field_type == 'number' and stat['min_value'] is not None
            else 'datetime'
            if field_type == 'datetime' or (not vector_type and stat['field_type'] == 'datetime')
            else None
        )
        if kind in ('enum', 'datetime'):
            field_schema['type'] = kind
        if kind is not None and hidden:
            await fill_cached(field_schema, stat, kind)
            field_schema['provenance'] = 'cached'
        elif kind is not None:
            facet_entries.append((field_schema, promoted, 1 if kind == 'enum' else 0))
        if kind is not None and facet_request:
            facet_jobs.append((field_schema, partial(stat_facet_job, stat, field_schema, kind)))
        elif kind is not None and not hidden:
            # See _field_stats_filter_decision: MIN/MAX filters on a small set (any field) or an indexed
            # field (even broad); DISTINCT only on a small set (the broad fan-out is the freeze the cap
            # prevents). A lightning sidebar request computes neither: its fields are labelled below.
            should_filter_minmax, should_filter_distinct = (
                _field_stats_filter_decision(filter_active, bool(filtered_broad), promoted)
                if computing
                else (False, False)
            )
            should_filter = should_filter_distinct if kind == 'enum' else should_filter_minmax
            if should_filter and not _queryable_field(stat['field_name']):
                # A name the queries cannot use keeps its cached values; it never fails the request
                # (on a small dataset the whole sidebar answered 400).
                should_filter = False
                field_schema['provenance'] = 'cached'
            if should_filter:
                queue = {'enum': enum_fields_to_filter, 'number': number_fields_to_filter}.get(
                    kind,
                    datetime_fields_to_filter,
                )
                queue.append((stat['field_name'], stat))
            else:
                await fill_cached(field_schema, stat, kind)

        field_schemas[stat['field_name']] = field_schema

    # Run filter queries in parallel. Each task uses _facet_query
    # so a SQL exception yields None instead of failing the whole gather;
    # the apply step below falls back to cached unfiltered values.
    if number_fields_to_filter or enum_fields_to_filter or datetime_fields_to_filter:

        async def guarded(field_name, field_type, stat, query):
            """One field's query: an unexpected failure keeps that field's cached values."""
            try:
                return await query
            except (asyncio.CancelledError, asyncpg.ObjectNotInPrerequisiteStateError):
                raise  # a refusal (concept being rescored, stale lasso, deleting dataset) is the request's 409
            except Exception as error:  # noqa: BLE001 - one field must not fail the sidebar
                logging.getLogger("fridge.errors").error(
                    "field %r of dataset %s kept its cached values",
                    field_name,
                    id,
                    exc_info=error,
                )
                return (field_name, field_type, 'cached', None, stat)

        tasks = (
            [guarded(fn, 'number', s, filter_number_field(fn, s)) for fn, s in number_fields_to_filter]
            + [guarded(fn, 'enum', s, filter_enum_field(fn, s)) for fn, s in enum_fields_to_filter]
            + [guarded(fn, 'datetime', s, filter_datetime_field(fn, s)) for fn, s in datetime_fields_to_filter]
        )
        for result in await asyncio.gather(*tasks):
            apply_result(*result)

    # A facet request: every field on its own, in the worker's facet slots (see _FacetSlots).
    if facet_jobs:
        running = [asyncio.ensure_future(run_facet_job(entry, job)) for entry, job in facet_jobs]
        try:
            await asyncio.gather(*running)
        finally:
            # A refusal (or the request's cancellation) stops the other fields too.
            for task in [*running, *([probe] if probe is not None else [])]:
                task.cancel()
            await asyncio.gather(*running, *([probe] if probe is not None else []), return_exceptions=True)

    # Add all field schemas to the result
    for field_name in sorted(field_schemas.keys()):
        schema.append(field_schemas[field_name])

    # Lightning sidebar request: the probe only tells pending from cached, so it runs last, in what is
    # left of the shared budget, never ahead of the promoted value lists above.
    if filter_active and lightning_mode and not computing and budget(_LIGHTNING_PROBE_BUDGET_S) > 0:
        filtered_broad = await _filtered_set_is_broad(
            id,
            cast('Dict[str, Any]', filter_obj),
            _LIGHTNING_FACET_MAX_ROWS,
            budget_s=budget(_LIGHTNING_PROBE_BUDGET_S),
        )
    if report is not None and filter_active and lightning_mode and not computing:
        report['breadth'] = filtered_broad

    # Fields nothing was computed for: their facet is the dataset's cached one, which the field's
    # section fills in for the filter when opened, if the gate allows it (or the probe is undecided).
    for entry, is_promoted, gate in facet_entries:
        decisions = {
            _field_stats_filter_decision(filter_active, broad, is_promoted)[gate]
            for broad in ((True, False) if filtered_broad is None else (filtered_broad,))
        }
        pending = filter_active and decisions != {False} and entry.get('filterable', True)
        entry.setdefault('provenance', 'pending' if pending else 'cached')

    # Deduplicate — keep first occurrence of each field name
    seen = set()
    deduped = []
    array_cols = await get_dataset_array_columns(id)
    for field in schema:
        name = field.get('name')
        if name not in seen and is_wanted(name):
            seen.add(name)
            if name in array_cols:
                # A native array column (tags, inferred_tags) can be served as an enum here, but the
                # grid cannot order by it: the Sort dropdown offered it and silently kept the default
                # order.
                field['sortable'] = False
            _finite_bounds(field)
            deduped.append(field)
            if emit is not None and name not in emitted:
                emitted.add(name)
                emit(field)
    return deduped


async def compute_filtered_minmax(
    dataset_id: int,
    field_name: str,
    filters: Dict[str, Any],
) -> Optional[Dict[str, float]]:
    """
    Compute min/max for a specific field given filter constraints.
    Uses the B-Tree index on the promoted field for performance.
    """
    text = _field_text_sql(field_name)  # metadata->>'x', or a mirror's own local_metadata->>'x'
    # Build filter SQL
    args = [dataset_id]
    arg_idx = 2
    filter_sql, arg_idx = parse_filter_node(
        filters,
        args,
        arg_idx,
        None,
        numeric_indexed_fields=await get_promoted_numeric_fields(dataset_id),
        dataset_id=dataset_id,
        array_columns=await get_dataset_array_columns(dataset_id),
    )

    # Query for min/max with filters
    # The index on (metadata->>'field_name')::float makes this fast
    query = f"""
        SELECT
            MIN(({text})::float) as min_val,
            MAX(({text})::float) as max_val
        FROM samples
        WHERE dataset_id = $1 AND {filter_sql}
          AND {text} IS NOT NULL
    """
    result = await db.fetchrow(query, *args)
    bounds = (result['min_val'], result['max_val']) if result and result['min_val'] is not None else ()
    if not all(map(math.isfinite, bounds)):
        # The text 'NaN', 'Infinity' or '-Infinity' that a number field can hold (Postgres casts it, and
        # sorts NaN above Infinity): JSON has no such numbers, so the bounds are the finite values'.
        # Only then: the guards read the field from every row twice more.
        query += f"""
          AND ({text})::float > '-Infinity'::float
          AND ({text})::float < 'Infinity'::float
        """
        result = await db.fetchrow(query, *args)

    if result and result['min_val'] is not None:
        return {'min': result['min_val'], 'max': result['max_val']}
    return None


async def compute_filtered_values(dataset_id: int, field_name: str, filters: Dict[str, Any]) -> List[str]:
    """
    Compute unique values for a specific field given filter constraints.
    Uses the B-Tree index on the promoted field for performance.
    """
    text = _field_text_sql(field_name)  # metadata->>'x', or a mirror's own local_metadata->>'x'
    # Build filter SQL
    args = [dataset_id]
    arg_idx = 2
    filter_sql, arg_idx = parse_filter_node(
        filters,
        args,
        arg_idx,
        None,
        numeric_indexed_fields=await get_promoted_numeric_fields(dataset_id),
        dataset_id=dataset_id,
        array_columns=await get_dataset_array_columns(dataset_id),
    )

    # Query for distinct values with filters
    # The index on metadata->>'field_name' makes this fast
    query = f"""
        SELECT DISTINCT {text} as value
        FROM samples
        WHERE dataset_id = $1 AND {filter_sql}
          AND {text} IS NOT NULL
        ORDER BY value LIMIT 101
    """

    rows = await db.fetch(query, *args)
    return [r['value'] for r in rows]


@router.post("/datasets/refresh-stats")
async def refresh_stale_stats(user: dict = Depends(require_role("admin")), wait: bool = True):
    """Schedule independent refreshes; one dataset's failure does not stop the others."""
    jobs = await statistics.schedule_statistics(db.pool, created_by=user['id'])
    if not wait:
        return JSONResponse(status_code=202, content={"jobs": [str(job['id']) for job in jobs]})
    results = []
    for job in jobs:
        try:
            result = await statistics.wait_for_refresh(db.pool, job['id'])
            results.extend(result['datasets'])
        except (TimeoutError, RuntimeError) as error:
            results.append({"dataset_id": job['dataset_id'], "fields_updated": 0, "error": str(error)})
    return {"refreshed_count": sum(not result['error'] for result in results), "datasets": results}


@router.post("/datasets/{id}/refresh-stats", dependencies=[Depends(require_dataset_access)])
async def refresh_dataset_stats(
    id: int,
    user: dict = Depends(require_role("power_user")),
    wait: bool = True,
    force: bool = False,
    timeout: float = 1800,
):
    """Refresh a finite committed-work barrier; later ingest does not extend the wait."""
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 1800:
        raise HTTPException(status_code=422, detail='timeout must be positive and at most 1800 seconds')
    try:
        job = await statistics.request_refresh(db.pool, id, created_by=user['id'], force=force)
    except LookupError as error:
        raise HTTPException(status_code=404, detail='Dataset not found') from error
    except job_queue.QueueBusy as error:
        raise HTTPException(
            status_code=409,
            detail='Statistics submission is busy; retry',
            headers={'Retry-After': '1'},
        ) from error
    handle = {'job_id': str(job['id']), 'state': job['state']}
    if not wait:
        return JSONResponse(status_code=202, content=handle)
    try:
        return await statistics.wait_for_refresh(db.pool, job['id'], timeout)
    except TimeoutError as error:
        return JSONResponse(
            status_code=202,
            content={**handle, 'requested_dataset_refreshed': False, 'skipped_reason': str(error)},
        )
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


class CreateArrayFieldRequest(BaseModel):
    field_name: str


_INDEX_BUILD_RUNNING = 'An index build is running; try again when it finishes (Background jobs shows it)'


async def add_array_column(pool, dataset_id: int, name: str) -> None:
    """The physical TEXT[] column and GIN index of an array field, shared by every dataset (a follower's sync adds
    the primary's this way, from the job runner's pool). ``name`` must already be checked against _SAFE_COLUMN_RE
    and _RESERVED_COLUMNS."""
    from ..index_jobs import INDEX_BUILD

    async with pool.acquire() as conn, _field_index_operation(conn, dataset_id, index_ddl=True):
        # Bounded: ACCESS EXCLUSIVE queued behind a long reader would stall every samples read and
        # write queued behind it (ingest included) until the request timed out. Busy: 409, retry.
        # _RESERVED_COLUMNS is a list someone has to keep; the catalog knows every column samples really has. Over a
        # column of another type, ADD COLUMN IF NOT EXISTS does nothing and the name would be registered as an array.
        taken = await conn.fetchval(
            """SELECT format_type(atttypid, atttypmod) FROM pg_attribute
               WHERE attrelid = 'samples'::regclass AND attname = $1 AND attnum > 0 AND NOT attisdropped""",
            name,
        )
        if taken is not None and taken != 'text[]':
            raise HTTPException(status_code=400, detail=f"'{name}' is a reserved column name")
        # Not during an index build (index_jobs.INDEX_BUILD): a GIN build waits for the snapshot of a concurrent
        # build of an expression or partial index (a field's, a vector index), so this request would wait out an
        # hours-long vector build. The queue's own claim lock for that resource, held until the GIN index is built,
        # keeps a build from starting meanwhile (one queued waits for this, unstarted). Refused: 409 with Retry-After,
        # which the SDK retries for a while, as for a busy table.
        # A session lock on a pooled connection: released in the `finally` below, and, should this request die
        # before that runs, by the pool's reset when the connection goes back (asyncpg's default reset runs
        # pg_advisory_unlock_all(); see database.Database.connect). Without either, no index build would ever be
        # claimed again.
        claims = (job_queue.EXCLUSIVE_LOCK, INDEX_BUILD)
        if not await conn.fetchval('SELECT pg_try_advisory_lock($1, hashtext($2))', *claims):
            raise HTTPException(status_code=409, detail=_INDEX_BUILD_RUNNING, headers={'Retry-After': '30'})
        try:
            if await job_queue.exclusive_owned(conn, INDEX_BUILD):
                raise HTTPException(status_code=409, detail=_INDEX_BUILD_RUNNING, headers={'Retry-After': '30'})
            try:
                async with conn.transaction():
                    await conn.execute("SET LOCAL lock_timeout = '2s'")
                    await conn.execute(f"ALTER TABLE samples ADD COLUMN IF NOT EXISTS {name} TEXT[] DEFAULT '{{}}'")
            except asyncpg.LockNotAvailableError:
                raise HTTPException(
                    status_code=409,
                    detail='The samples table is busy (a long read or an index build); retry shortly',
                    headers={'Retry-After': '5'},
                ) from None
            await conn.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_samples_{name}_gin ON samples USING gin ({name})",
            )
        finally:
            await conn.execute('SELECT pg_advisory_unlock($1, hashtext($2))', *claims)
        await conn.execute(
            "INSERT INTO sample_array_columns (column_name) VALUES ($1) ON CONFLICT DO NOTHING",
            name,
        )
    # Invalidate cache (sample_array_columns changed)
    global _array_columns_cache
    _array_columns_cache = None


@router.post("/datasets/{id}/array-fields", dependencies=[Depends(require_dataset_access)])
async def create_array_field(
    id: int,
    request: CreateArrayFieldRequest,
    user: dict = Depends(require_role("power_user")),
):
    """Create a new tag-like array field on the samples table.

    This adds a real TEXT[] column with a GIN index — same performance
    characteristics as the built-in tags field. PostgreSQL 11+ makes
    ADD COLUMN with a default instant (no table rewrite).
    """
    name = request.field_name.lower().strip()

    if not _SAFE_COLUMN_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Field name must be lowercase alphanumeric with underscores, starting with a letter (max 63 chars)",
        )

    if name in _RESERVED_COLUMNS:
        raise HTTPException(status_code=400, detail=f"'{name}' is a reserved column name")

    origin = await db.fetchval(
        "SELECT origin FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
        id,
        name,
    )
    if origin == 'metadata':
        raise HTTPException(status_code=409, detail=f"'{name}' is already a metadata field")
    existing = await get_array_columns()
    # The physical TEXT[] column is GLOBAL (shared across datasets) and survives
    # dataset deletion, but membership is PER-DATASET (dataset_fields). So a column
    # can already exist globally while this dataset hasn't registered it yet.
    already_exists = name in existing

    # The cap only applies when actually adding a NEW global column; adopting an
    # existing one for this dataset doesn't grow the column count.
    if not already_exists and len(existing) >= 20:
        raise HTTPException(status_code=400, detail="Maximum of 20 array fields reached")

    # Create the physical column + index only when it doesn't exist yet.
    if not already_exists:
        assert db.pool is not None  # connected at startup (lifespan)
        await add_array_column(db.pool, id, name)

    # Register the field for THIS dataset regardless of whether the global column
    # already existed — otherwise a second dataset adopting an existing array column
    # would never show it in its own list_array_fields / schema.
    origin_query = 'SELECT origin FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2'
    async with db.pool.acquire() as conn:
        # Already registered here (a sync calls this on every run): the shared gate is enough.
        async with conn.transaction():
            await lock_field_lifecycle(conn, id, wait=False)
            registered = await conn.fetchval(origin_query, id, name) == 'native_array'
        if registered:
            return {'message': f"Array field '{name}' registered for this dataset", 'created': not already_exists}
        # Registering needs the exclusive gate, taken in a new transaction: two callers that each kept
        # the shared one while trying for exclusive blocked each other until both gave up.
        async with conn.transaction():
            await lock_field_lifecycle(conn, id, exclusive=True, wait=False)
            origin = await conn.fetchval(origin_query, id, name)
            if origin == 'native_array':  # another caller registered it meanwhile
                return {'message': f"Array field '{name}' registered for this dataset", 'created': not already_exists}
            if origin == 'metadata':
                raise HTTPException(status_code=409, detail=f"'{name}' is already a metadata field")
            await conn.execute(
                """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, is_promoted)
                   VALUES ($1, $2, 'list', 'native_array', TRUE)
                   ON CONFLICT (dataset_id, field_name) DO UPDATE SET field_type = 'list',
                       origin = 'native_array', is_promoted = TRUE""",
                id,
                name,
            )
            await conn.execute('DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2', id, name)

    if already_exists:
        return {"message": f"Array field '{name}' registered for this dataset", "created": False}
    return {"message": f"Array field '{name}' created", "created": True}


@router.get("/datasets/{id}/array-fields", dependencies=[Depends(require_dataset_access)])
async def list_array_fields(id: int):
    """List all array fields available for this dataset."""
    columns = await get_dataset_array_columns(id)
    return [{"name": col} for col in sorted(columns)]


# Postgres silently truncates identifiers at NAMEDATALEN-1 = 63 chars. A field name can be
# up to 128 chars (_SAFE_FIELD_NAME_RE), so idx_samples_meta_{id}_{field} can overflow — and
# a truncated name desyncs promote/verify/demote (they reconstruct the untruncated name, which
# no longer matches the actual index), or collides two long field names onto one index. This
# returns a name guaranteed <= 63 chars; short names keep the historical form verbatim so every
# existing index still matches with no migration.
_MAX_IDENTIFIER_LEN = 63


def _promoted_index_name(dataset_id: int, field_name: str) -> str:
    name = f"idx_samples_meta_{dataset_id}_{field_name}"
    if len(name) <= _MAX_IDENTIFIER_LEN:
        return name
    # Overflow → hash the (dataset, field) for a short, collision-free, deterministic name.
    # Deterministic so promote and demote derive the SAME name for the same field.
    digest = hashlib.md5(f"{dataset_id}:{field_name}".encode(), usedforsecurity=False).hexdigest()[:16]
    return f"idx_samples_meta_{dataset_id}_h{digest}"


@router.post("/datasets/{id}/fields/{field_name}/promote", dependencies=[Depends(require_dataset_access)])
async def promote_field(
    id: int,
    field_name: str,
    user: dict = Depends(require_role("power_user")),
    wait: bool = True,
):
    """
    Promote a field to be indexed for faster filtering.
    Creates a B-Tree index for the field in a runner job (backend.index_jobs) and answers within
    five seconds: the final message, or 202 {job_id, state} while the build continues.
    Requires power_user or admin role.
    """
    started = _time.monotonic()
    # Check dataset exists
    dataset = await db.fetchrow("SELECT id FROM datasets WHERE id = $1", id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Check if already promoted
    existing = await db.fetchrow(
        "SELECT id, is_promoted, field_type FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
        id,
        field_name,
    )

    if existing and existing['is_promoted']:
        return {"message": f"Field {field_name} is already promoted"}

    if not existing:
        raise HTTPException(status_code=404, detail=f"Field '{field_name}' has no definition in this dataset")
    field_type = existing['field_type']

    # Sanitize field name for SQL (basic check)
    if not field_name.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid field name")

    # Create index. Name is length-bounded so it can't be silently truncated (which would
    # desync the verify/drop lookups below); demote_field derives the same name.
    index_name = _promoted_index_name(id, field_name)

    # The index expression for the field type (field_name already validated): a number as a float, a datetime on
    # its instant, a string as its text. The background rebuilds use the same one.
    from ..index_jobs import field_index_expression, submit_field_index

    index_expr = field_index_expression(field_type, field_name)
    if index_expr is None:
        raise HTTPException(status_code=400, detail=f"Cannot promote field type: {field_type}")

    # The job builds CONCURRENTLY on its own connection under the index-operation guard (20261017),
    # removes invalid leftovers, and marks the field promoted only if its definition is unchanged.
    try:
        job = await submit_field_index(
            db.pool,
            id,
            user['id'],
            {
                'field_name': field_name,
                'index_name': index_name,
                'expression': index_expr,
                'definition_id': existing['id'],
                'field_type': field_type,
            },
        )
    except job_queue.QueueBusy as error:
        raise HTTPException(status_code=409, detail='Index submission is busy; retry') from error
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)


@router.post("/datasets/{id}/fields/{field_name}/demote", dependencies=[Depends(require_dataset_access)])
async def demote_field(
    id: int,
    field_name: str,
    user: dict = Depends(require_role("power_user")),
    wait: bool = True,
):
    """Remove index from a promoted field.

    An `index_drop` job (backend/index_actions.py) marks the field not indexed, then drops the index
    with DROP INDEX CONCURRENTLY outside any backup, so reads and ingest keep flowing; a plain DROP INDEX
    queued an ACCESS EXCLUSIVE lock on samples behind any long reader (a pg_dump) and stalled every
    read and write behind it. Answers within five seconds: the final message, or 202 {job_id, state}.
    """
    started = _time.monotonic()
    dataset = await db.fetchrow("SELECT id FROM datasets WHERE id = $1", id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    if not field_name.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid field name")

    # Same derivation as promote_field so we drop the index that was actually created.
    index_name = _promoted_index_name(id, field_name)
    from ..index_actions import submit_drop

    try:
        job = await submit_drop(db.pool, index_name, user, record=False, field_name=field_name, dataset_id=id)
    except job_queue.QueueBusy as error:
        raise HTTPException(status_code=409, detail='Index submission is busy; retry') from error
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)


class SetFieldTypeRequest(BaseModel):
    field_type: Literal['unknown', 'boolean', 'number', 'string', 'datetime', 'list', 'object']


@router.patch('/datasets/{id}/fields/{field_name}/type', dependencies=[Depends(require_dataset_access)])
async def set_field_type(
    id: int,
    field_name: str,
    request: SetFieldTypeRequest,
    user: dict = Depends(require_role('power_user')),
):
    """Explicitly change a metadata declaration without rewriting stored values or indexes."""
    _validate_field_name(field_name)
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await lock_field_lifecycle(conn, id, exclusive=True)
            current = await conn.fetchrow(
                'SELECT field_type, origin, is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
                id,
                field_name,
            )
            if current is None:
                raise HTTPException(status_code=404, detail='Field not found')
            if current['origin'] != 'metadata':
                raise HTTPException(status_code=400, detail='Only metadata field types can be changed')
            if current['field_type'] == request.field_type:
                return {'status': 'ok', 'field_name': field_name, 'field_type': request.field_type}
            if current['is_promoted']:
                raise HTTPException(status_code=409, detail='Remove the field index before changing its type')
            await conn.execute(
                'UPDATE dataset_fields SET field_type = $3, display_type = NULL, overlay_config = NULL '
                'WHERE dataset_id = $1 AND field_name = $2',
                id,
                field_name,
                request.field_type,
            )
            await conn.execute(
                'DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2',
                id,
                field_name,
            )
            await conn.execute(
                REQUEST_FULL_REFRESH,
                id,
            )
    return {'status': 'ok', 'field_name': field_name, 'field_type': request.field_type}


class SetDisplayTypeRequest(BaseModel):
    display_type: Optional[str] = None  # 'enum', 'number', or None to reset


@router.post("/datasets/{id}/fields/{field_name}/display-type", dependencies=[Depends(require_dataset_access)])
async def set_field_display_type(
    id: int,
    field_name: str,
    request: SetDisplayTypeRequest,
    user: dict = Depends(require_role("power_user")),
):
    """Override how a field is displayed in filters (e.g., show a number field as a dropdown)."""
    if not field_name.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid field name")
    if request.display_type not in (None, 'enum', 'number'):  # any string was stored
        raise HTTPException(status_code=400, detail="display_type must be 'enum', 'number' or null")

    assert db.pool is not None
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await lock_field_lifecycle(conn, id)
            stat = await conn.fetchrow(
                "SELECT field_type FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
                id,
                field_name,
            )
            if not stat:
                raise HTTPException(status_code=404, detail=f"Field '{field_name}' has no definition in this dataset")
            await conn.execute(
                "UPDATE dataset_fields SET display_type = $3 WHERE dataset_id = $1 AND field_name = $2",
                id,
                field_name,
                request.display_type,
            )
    unique_values = None
    if request.display_type == 'enum' and stat['field_type'] == 'number':
        rows = await db.fetch(
            f"""
            SELECT DISTINCT metadata->>'{field_name}' AS val
            FROM samples WHERE dataset_id = $1 AND jsonb_typeof(metadata->'{field_name}') = 'number'
            ORDER BY val LIMIT 101
            """,
            id,
        )
        unique_values = [row['val'] for row in rows[:100]]

    return {"message": f"Display type for {field_name} set to {request.display_type}", "unique_values": unique_values}


class AutocompleteRequest(BaseModel):
    q: str = ""
    limit: int = 100
    filters: Optional[Dict[str, Any]] = None


@router.post("/datasets/{id}/fields/{field_name}/autocomplete", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def autocomplete_field_post(id: int, field_name: str, request: AutocompleteRequest):
    """Search for matching values in a metadata field with counts.

    Accepts filters so results are scoped to the current view.
    Returns ``{"values": [...], "exhaustive": bool}``; ``exhaustive`` is False when
    the bounded scan timed out and ``values`` is only the cached preview.
    """
    with field_usage.measure(id, {'facet': [field_name]}):
        return await _autocomplete_impl(id, field_name, request.q, request.limit, request.filters)


@router.get("/datasets/{id}/fields/{field_name}/autocomplete", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def autocomplete_field(id: int, field_name: str, q: str = "", limit: int = 100, filters: Optional[str] = None):
    filter_obj = json.loads(filters) if filters else None
    with field_usage.measure(id, {'facet': [field_name]}):
        return await _autocomplete_impl(id, field_name, q, limit, filter_obj)


async def _autocomplete_impl(id: int, field_name: str, q: str, limit: int, filter_obj=None):
    try:
        local = local_field(field_name)  # a mirror's own field: local_metadata, or the local_tags array
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    if local is None and not (
        _is_safe_field_name(field_name) or field_name.replace('_', '').replace('-', '').isalnum()
    ):
        raise HTTPException(status_code=400, detail="Invalid field name")

    # Build base WHERE clause
    conditions = ["dataset_id = $1"]
    args: list = [id]
    arg_idx = 2

    # Apply global filters (excluding the field being searched)
    if filter_obj:
        try:
            # Remove filter for the field being autocompleted to avoid circular filtering
            cleaned = _remove_field_from_filter(filter_obj, field_name)
            if cleaned:
                sql, arg_idx = parse_filter_node(
                    cleaned,
                    args,
                    arg_idx,
                    numeric_indexed_fields=await get_promoted_numeric_fields(id),
                    dataset_id=id,
                    array_columns=await get_dataset_array_columns(id),
                )
                if sql and sql != "TRUE":
                    conditions.append(sql)
        except Exception as e:
            print(f"Autocomplete filter error: {e}")

    where = " AND ".join(conditions)

    # Per-field-type value source so EVERY field's full value space is searchable,
    # not just scalar metadata — array columns (tags/inferred_tags) and the
    # annotation-label pseudo-fields were previously unreachable beyond the cached
    # top-200 preview. Search reaching all = the cache is just a
    # preview, never a cap.
    array_cols = await get_dataset_array_columns(id)
    scalar_meta = False
    path = None if local is not None else label_path(field_name, array_cols)
    scalar_text = local.text if local is not None else f"metadata->>'{field_name}'"
    if path and path[1] == 'label':
        arr = _validate_field_name(path[0])
        val_source = (
            f"SELECT jsonb_array_elements(metadata->'{arr}')->>'label' AS val "
            f"FROM samples WHERE {where} AND jsonb_typeof(metadata->'{arr}') = 'array'"
        )
    elif field_name == 'segmentation.labels':
        val_source = (
            f"SELECT jsonb_array_elements_text(metadata->'segmentation'->'labels') AS val "
            f"FROM samples WHERE {where} AND jsonb_typeof(metadata->'segmentation'->'labels') = 'array'"
        )
    elif local is not None and local.array:
        val_source = f"SELECT unnest({local.column}) AS val FROM samples WHERE {where}"
    elif local is None and field_name in array_cols:
        val_source = f"SELECT unnest({field_name}) AS val FROM samples WHERE {where}"
    else:
        scalar_meta = True
        val_source = f"SELECT {scalar_text} AS val FROM samples WHERE {where} AND {scalar_text} IS NOT NULL"

    # Indexed exact-match probe, promoted scalar fields only. A pasted full identifier on a
    # high-cardinality field (every value unique) is never in the cached preview, and the
    # GROUP BY below cannot use the field's btree under a non-C collation, so the
    # budget trips and the typeahead used to answer "no match" for a value that EXISTS,
    # while Enter (an indexed `=` filter) found it instantly. `=` IS indexed: probe it first.
    exact_hit = None
    if q and scalar_meta:
        # A datetime field's index is on its instant, not its text (backend/datetimes.py): no probe to ride.
        promoted = await db.fetchval(
            "SELECT is_promoted AND field_type <> 'datetime' FROM dataset_fields "
            "WHERE dataset_id = $1 AND field_name = $2",
            id,
            field_name,
        )
        if promoted:
            cnt = await _safe_filter_query(
                db.fetchval(
                    f"SELECT COUNT(*) FROM samples WHERE {where} AND {scalar_text} = ${arg_idx}",
                    *args,
                    q,
                ),
                budget_s=_AUTOCOMPLETE_BUDGET_S,
            )
            if cnt:
                exact_hit = {"value": q, "count": cnt}

    val_conds = ["val IS NOT NULL"]
    if q:
        # Match ANYWHERE in the value, like the picker's cached-list filter: this matched only the
        # start, so "Forest" found nothing on `…/Forest/Forest_1011.tif` once the cached list ran
        # out. No index serves either form (see the probe above), and
        # benchmarks on large and small datasets showed similar costs (±2 %). Typed % and _ are
        # literal.
        args.append('%' + _escape_like(q) + '%')
        val_conds.append(f"val ILIKE ${arg_idx} ESCAPE '\\'")
        arg_idx += 1
    args.append(limit)
    # Bound the per-keystroke scan: on an unindexed field over millions of rows this
    # GROUP BY is a full seq scan, and rapid typing stacks them on the pool. Cap it and
    # fall back to the cached value preview so a keystroke returns instantly instead of
    # freezing the sidebar. (Promoted/indexed fields resolve well within the budget.)
    rows = await _safe_filter_query(
        db.fetch(
            f"SELECT val, COUNT(*) AS cnt FROM ({val_source}) sub "
            f"WHERE {' AND '.join(val_conds)} GROUP BY val ORDER BY cnt DESC LIMIT ${arg_idx}",
            *args,
        ),
        budget_s=_AUTOCOMPLETE_BUDGET_S,
    )
    if rows is None:
        cached = await db.fetchval(
            "SELECT unique_values FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2",
            id,
            field_name,
        )
        preview = cached or []
        if q:
            ql = q.lower()
            preview = [v for v in preview if v and ql in v.lower()]
        # count omitted (None): this is the cached preview, not a true value-space count.
        values = [{"value": v, "count": None} for v in preview[:limit]]
        return _autocomplete_result(values, exact_hit, exhaustive=False)
    values = [{"value": r['val'], "count": r['cnt']} for r in rows if r['val']]
    return _autocomplete_result(values, exact_hit, exhaustive=True)


def _autocomplete_result(values: list, exact_hit: Optional[dict], *, exhaustive: bool) -> dict:
    """``exhaustive=False`` means the bounded scan hit its budget and ``values`` is only the
    cached preview, so an empty list is NOT "no such value" and the UI must say so. A verified
    exact hit always leads the list (once)."""
    if exact_hit and all(v["value"] != exact_hit["value"] for v in values):
        values.insert(0, exact_hit)
    return {"values": values, "exhaustive": exhaustive}


def _remove_field_from_filter(filter_obj: dict, field_name: str) -> Optional[dict]:
    """Remove a specific field from a filter tree so autocomplete isn't self-filtering."""
    if not filter_obj:
        return None
    if 'field' in filter_obj:
        if filter_obj['field'] == field_name:
            return None
        return filter_obj
    if 'logic' in filter_obj and 'filters' in filter_obj:
        cleaned = [_remove_field_from_filter(f, field_name) for f in filter_obj['filters']]
        cleaned = [f for f in cleaned if f is not None]
        if not cleaned:
            return None
        return {**filter_obj, 'filters': cleaned}
    return filter_obj


@router.get("/datasets/{id}/schema", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_dataset_schema(id: int, filters: Optional[str] = None, lightning_mode: bool = True):
    """
    Get schema with stats.
    Optimization: Use cached stats for global min/max.
    Only re-calculate if absolutely necessary (which is rarely for schema).
    """
    schema = []

    args = [id]

    filter_obj = None
    if filters:
        try:
            filter_obj = json.loads(filters)
        except (json.JSONDecodeError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid filter JSON: {e}")

    # 1. media_type (enum)
    if filter_obj and has_active_filters(filter_obj):
        f_args = [id]
        f_idx = 2
        f_sql, _ = parse_filter_node(
            filter_obj,
            f_args,
            f_idx,
            None,
            numeric_indexed_fields=await get_promoted_numeric_fields(id),
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )
        media_types = await db.fetch(
            f"SELECT DISTINCT media_type FROM samples WHERE dataset_id = $1 AND {f_sql}",
            *f_args,
        )
    else:
        media_types = await db.fetch("SELECT DISTINCT media_type FROM samples WHERE dataset_id = $1", *args)

    schema.append({"name": "media_type", "type": "enum", "values": [r['media_type'] for r in media_types]})

    # 2. Tags
    if filter_obj and has_active_filters(filter_obj):
        # Tags are an array, so unnest them under the same filter SQL compute_filtered_values builds.
        f_args = [id]
        f_idx = 2
        f_sql, _ = parse_filter_node(
            filter_obj,
            f_args,
            f_idx,
            None,
            numeric_indexed_fields=await get_promoted_numeric_fields(id),
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )

        tags_rows = await db.fetch(
            f"""
            SELECT tag FROM (
                SELECT DISTINCT unnest(tags) as tag FROM samples
                WHERE dataset_id = $1 AND {f_sql}
            ) sub
            ORDER BY LOWER(tag)
         """,
            *f_args,
        )
    else:
        tags_rows = await db.fetch(
            """
            SELECT tag FROM (
                SELECT DISTINCT unnest(tags) as tag FROM samples
                WHERE dataset_id = $1
            ) sub
            ORDER BY LOWER(tag)
        """,
            *args,
        )

    schema.append({"name": "tags", "type": "array", "values": [r['tag'] for r in tags_rows]})

    # Detection, polygon and segmentation fields keep their cached values: filtering them per request
    # would be slow.

    # 3. created_at: the dataset-wide range unless a filter is active
    if filter_obj and has_active_filters(filter_obj):
        f_args = [id]
        f_idx = 2
        f_sql, _ = parse_filter_node(
            filter_obj,
            f_args,
            f_idx,
            None,
            numeric_indexed_fields=await get_promoted_numeric_fields(id),
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )
        created_at_stats = await db.fetchrow(
            f"""
            SELECT min(created_at) as min_val, max(created_at) as max_val
            FROM samples WHERE dataset_id = $1 AND {f_sql}
        """,
            *f_args,
        )
    else:
        created_at_stats = await db.fetchrow(
            """
            SELECT min(created_at) as min_val, max(created_at) as max_val
            FROM samples WHERE dataset_id = $1
        """,
            *args,
        )

    if created_at_stats and created_at_stats['min_val']:
        schema.append(
            {
                "name": "created_at",
                "type": "datetime",
                "min": created_at_stats['min_val'].isoformat(),
                "max": created_at_stats['max_val'].isoformat(),
            },
        )
    schema.append(await _last_modified_field(id, lightning_mode))

    # 4. id (number) - keep global
    id_stats = await db.fetchrow(
        "SELECT min(id) as min_val, max(id) as max_val FROM samples WHERE dataset_id = $1",
        *args,
    )
    if id_stats and id_stats['min_val'] is not None:
        schema.append({"name": "id", "type": "number", "min": id_stats['min_val'], "max": id_stats['max_val']})

    # 5. filepath + additional media fields
    schema.append({"name": "filepath", "type": "filepath"})

    # Get dataset's media_fields so we can tag them as "filepath" type in the schema
    dataset_row = await db.fetchrow("SELECT media_fields FROM datasets WHERE id = $1", id)
    dataset_media_fields = (
        set(dataset_row['media_fields']) if dataset_row and dataset_row['media_fields'] else {'filepath'}
    )

    filters_active = filter_obj and has_active_filters(filter_obj)

    # --- Metadata Fields ---
    cached_stats = await field_stats(db, id)
    vector_types = {}
    for cache in ('__polyline_fields__', '__keypoint_fields__', '__detection_fields__', '__cuboid_fields__'):
        for stat in cached_stats:
            if stat['field_name'] == cache:
                vector_types.update({name: DISCOVERY_TYPES[cache] for name in stat['unique_values'] or []})

    # Track which field names are already in schema to avoid duplicates
    schema_names = {f['name'] for f in schema}

    for stat in cached_stats:
        if stat['field_name'].startswith('__'):
            continue
        if stat['field_name'] in schema_names:
            continue  # Already added (e.g., by array columns loop or media_type)
        is_media_field = stat['field_name'] in dataset_media_fields

        field_info = {
            "name": stat['field_name'],
            "type": 'filepath' if is_media_field else vector_types.get(stat['field_name'], presentation_type(stat)),
            "is_promoted": stat['is_promoted'] or False,
            "declared_type": stat['field_type'],
            "origin": stat.get('origin', 'metadata'),
            "owner": stat.get('owner', 'self'),
            "display_type": stat.get('display_type'),
            "stats_pending": stat.get('stats_pending', False),
            "values_complete": stat.get('values_complete', False),
        }

        # Media fields get filepath type — skip number/enum enrichment
        if is_media_field:
            schema.append(field_info)
            continue

        if field_info['type'] == 'number':
            field_info['min'] = stat['min_value']
            field_info['max'] = stat['max_value']

            if filters_active and (stat['is_promoted'] or not lightning_mode):
                try:
                    # filters_active implies filter_obj is set.
                    filtered_bounds = await compute_filtered_minmax(
                        id,
                        stat['field_name'],
                        cast('Dict[str, Any]', filter_obj),
                    )
                    if filtered_bounds:
                        field_info['min'] = filtered_bounds['min']
                        field_info['max'] = filtered_bounds['max']
                except Exception as e:
                    print(f"Error computing filtered bounds for {stat['field_name']}: {e}")
                    pass

        elif field_info['type'] == 'array':
            field_info['values'] = stat['unique_values'] or []

        elif field_info['type'] == 'enum':
            field_info['values'] = stat['unique_values']
            if stat['field_type'] == 'number' and not stat['unique_values']:
                _validate_field_name(stat['field_name'])
                rows = await db.fetch(
                    f"SELECT DISTINCT metadata->>'{stat['field_name']}' AS val FROM samples "
                    f"WHERE dataset_id = $1 AND jsonb_typeof(metadata->'{stat['field_name']}') = 'number' "
                    'ORDER BY val LIMIT 101',
                    id,
                )
                field_info['values'] = [row['val'] for row in rows[:100]]
                field_info['values_complete'] = len(rows) <= 100

            if filters_active and (stat['is_promoted'] or not lightning_mode):
                try:
                    # filters_active implies filter_obj is set.
                    filtered_values = await compute_filtered_values(
                        id,
                        stat['field_name'],
                        cast('Dict[str, Any]', filter_obj),
                    )
                    field_info['values'] = filtered_values[:100]
                    field_info['values_complete'] = len(filtered_values) <= 100
                except Exception as e:
                    print(f"Error computing filtered values for {stat['field_name']}: {e}")
                    pass
            elif stat['is_promoted']:
                # Unfiltered indexed field → complete distinct set from the index, not the
                # stale bounded cache (same fix as the field-stats sidebar path).
                _is_num = stat['field_type'] in ('number', 'float', 'int', 'integer')
                promoted = await _promoted_distinct_values(id, stat['field_name'], _is_num)
                if promoted is not None:
                    field_info['values'], _truncated = promoted
                    field_info['values_complete'] = not _truncated
                    if _truncated:
                        field_info['values_truncated'] = True

        elif field_info['type'] == 'datetime' and stat['unique_values']:
            field_info['min'] = stat['unique_values'][0]
            field_info['max'] = stat['unique_values'][-1]

        schema.append(field_info)

    # Deduplicate — keep first occurrence of each field name
    seen = set()
    deduped = []
    for field in schema:
        name = field.get('name')
        if name not in seen:
            seen.add(name)
            deduped.append(field)
    return deduped


# A patch field's tile ('clip_patch_4x4_r0c1'), folded into the patch field ('clip_patch_4x4') by /coverage, as the
# missing filter's SQL pattern folds it: ASCII digits only, matched to the end (fullmatch, so no trailing newline).
_PATCH_TILE = re.compile(r'(.*_patch_[0-9]+x[0-9]+)_r[0-9]+c[0-9]+', re.DOTALL)


async def _embedding_coverage(dataset_id: int) -> list[tuple[str, int]]:
    """How many samples carry each embedding field, by name, a patch field's tiles folded into one entry (one row
    per patch field instead of 64 noisy per-tile rows). The dataset's partition is counted once per stored name,
    with nothing computed per row: the unique key (dataset_id, sample_id, field_name) makes that one row per sample,
    and the (dataset_id, sample_id) key to samples means every row is a sample's. Only a patch field counts distinct
    samples, over its own tiles' names (and a vector stored under the patch field's own name, as the fold counted it).
    The counts are separate statements, so under live ingest a patch count can be a moment newer than the rest: fine
    for a coverage signal. Folding every one of a million rows' names with a regex, joined to samples, took 4.9 s on
    1 million vectors (production, 2026-11-12); this takes about 0.6 s."""
    rows = await db.fetch(
        'SELECT field_name, count(*) AS present FROM embeddings WHERE dataset_id = $1 GROUP BY field_name',
        dataset_id,
    )
    present: dict[str, int] = {}
    tiles: dict[str, list[str]] = {}
    for row in rows:
        tile = _PATCH_TILE.fullmatch(row['field_name'])
        if tile:
            tiles.setdefault(tile.group(1), []).append(row['field_name'])
        else:
            present[row['field_name']] = row['present']
    for patch, names in tiles.items():
        # A sample counts once however many of its tiles are stored.
        if patch in present:  # a vector stored under the patch field's own name too: folded with its tiles
            del present[patch]
            names = [*names, patch]
        present[patch] = await db.fetchval(
            'SELECT count(DISTINCT sample_id) FROM embeddings WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
            dataset_id,
            names,
        )
    return sorted(present.items())


@router.get("/datasets/{id}/coverage", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def dataset_coverage(id: int):
    """Derived-artifact coverage: how many samples have each embedding field, each hash
    field, and a location, vs the dataset total. Powers the "N of M embedded/hashed/
    located" signal and tells an uploader what still needs filling. Occasional/dashboard
    call (counts scan the artifact tables) — not a hot path.
    """
    # Each count is a full scan of its artifact table on large datasets; run them
    # concurrently so wall-clock ≈ the slowest, not the sum. Still an occasional/
    # dashboard call (seconds on multi-million-row datasets) — not for hot polling.
    counts, emb, hsh, loc_present = await asyncio.gather(
        # Object rows counted in the same scan: the map's empty state quotes the image grain the
        # header shows (not "0 of 12,000 samples" next to "2,000 images").
        db.fetchrow(
            "SELECT count(*) AS total, count(*) FILTER (WHERE media_type = 'object') AS objects "
            "FROM samples WHERE dataset_id = $1",
            id,
        ),
        _embedding_coverage(id),
        db.fetch(
            """
            SELECT h.field_name, count(*) AS present
            FROM sample_hashes h JOIN samples s ON h.sample_id = s.id
            WHERE s.dataset_id = $1
            GROUP BY h.field_name ORDER BY h.field_name
        """,
            id,
        ),
        db.fetchval("SELECT count(*) FROM samples WHERE dataset_id = $1 AND location IS NOT NULL", id),
    )
    total = counts['total'] or 0
    objects = counts['objects'] or 0
    loc_present = loc_present or 0

    def _entry(name, present):
        return {"field": name, "present": present, "missing": max(0, total - present)}

    return {
        "total_samples": total,
        "total_objects": objects,
        "embeddings": [_entry(name, present) for name, present in emb],
        "hashes": [_entry(r['field_name'], r['present']) for r in hsh],
        "location": {"present": loc_present, "missing": max(0, total - loc_present)},
    }


_MISSING_TABLES = {"embedding": "embeddings", "hash": "sample_hashes"}
_DERIVED_FIELD_TABLES = {"embeddings": "embeddings", "hashes": "sample_hashes"}


def _derived_field(field: str) -> Optional[tuple[str, str]]:
    """(table, name) for a filter on a derived field — `embeddings.<f>` / `hashes.<f>`, or
    ('location', '') — else None (an ordinary metadata / native field)."""
    if field == 'location':
        return 'location', ''
    prefix, _, name = field.partition('.')
    table = _DERIVED_FIELD_TABLES.get(prefix)
    return (table, name) if table and name else None


@router.get("/datasets/{id}/missing", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def dataset_missing(
    id: int,
    kind: str,
    field: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
    after_id: Optional[int] = None,
):
    """Samples that still LACK a derived artifact, for resumable backfill.

    kind='embedding'|'hash' (needs ?field=) or 'location'. Returns
    {items:[{id, filepath}], total}. Backfill pattern: page, fill each batch (which
    drops those rows from the result), repeat until empty.

    ``after_id`` is an id cursor: only samples with ``id > after_id`` are listed, in id order, so a
    walk that passes the last id it saw advances past samples it could not fill (an unreadable image
    stays missing and would otherwise be returned again forever). ``total`` always counts every
    missing sample, whatever the cursor.
    """
    limit = max(1, min(limit, 10000))
    offset = max(0, offset)

    if kind == "location":
        missing_sql = "s.dataset_id = $1 AND s.location IS NULL"
        args: list[Any] = [id]
    elif kind in _MISSING_TABLES:
        if not field:
            raise HTTPException(status_code=422, detail=f"kind='{kind}' requires a ?field= parameter")
        table = _MISSING_TABLES[kind]  # whitelisted table name (NOT user input)
        missing_sql = (
            f"s.dataset_id = $1 AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.sample_id = s.id AND t.field_name = $2)"
        )
        args = [id, field]
    else:
        raise HTTPException(status_code=422, detail="kind must be 'embedding', 'hash', or 'location'")

    page_sql = missing_sql
    page_args = list(args)
    if after_id is not None:
        page_args.append(after_id)
        page_sql += f" AND s.id > ${len(page_args)}"
    rows = await db.fetch(
        f"SELECT s.id, s.filepath FROM samples s WHERE {page_sql} "
        f"ORDER BY s.id LIMIT ${len(page_args) + 1} OFFSET ${len(page_args) + 2}",
        *page_args,
        limit,
        offset,
    )
    total = await db.fetchval(f"SELECT count(*) FROM samples s WHERE {missing_sql}", *args) or 0

    return {"items": [{"id": r['id'], "filepath": r['filepath']} for r in rows], "total": total}


class SampleListResponse(BaseModel):
    items: List[Sample]
    total: int


def _get_array_columns_sync() -> set:
    """Get cached array columns synchronously. Falls back to {'tags'} if cache not populated."""
    return _array_columns_cache or {'tags'}


def _element_predicate_sql(key: str, op: str, val, args: List[Any], arg_idx: int) -> tuple[str, int]:
    """Build a single predicate on a JSONB array element's key (``elem->>'key'``).

    Used for per-object filtering of label arrays (detections/polygons), e.g.
    `confidence > 0.9` becomes `(elem->>'confidence')::double precision > $N`.
    """
    _validate_field_name(key)
    base = f"elem->>'{key}'"
    op = (op or '=').lower()
    is_num = isinstance(val, (int, float)) and not isinstance(val, bool)

    if op in ('exists', 'is_not_null'):
        return f"{base} IS NOT NULL", arg_idx
    if op in ('is_null', 'not_exists'):
        return f"{base} IS NULL", arg_idx
    if op == 'in':
        values = val if isinstance(val, list) else [val]
        if not values:
            return "FALSE", arg_idx
        args.append([str(v) for v in values])
        sql = f"{base} = ANY(${arg_idx}::text[])"
        return sql, arg_idx + 1
    if op in ('not_in', 'nin'):
        values = val if isinstance(val, list) else [val]
        if not values:
            return "TRUE", arg_idx
        args.append([str(v) for v in values])
        # NULL-aware: an element whose key is absent is "not in" the set.
        sql = f"({base} IS NULL OR NOT ({base} = ANY(${arg_idx}::text[])))"
        return sql, arg_idx + 1
    if op == 'contains':
        args.append(f"%{_escape_like(val)}%")
        sql = f"{base} ILIKE ${arg_idx} ESCAPE '\\'"
        return sql, arg_idx + 1
    # Guarded numeric cast: non-number element values become NULL (non-matching)
    # instead of raising and 500-ing the whole query.
    num = f"CASE WHEN jsonb_typeof(elem->'{key}') = 'number' THEN (elem->>'{key}')::double precision END"
    if op in ('>', '>=', '<', '<='):
        # Bind a real number: a numeric string ("0.9" from a text input) would 500 as
        # "must be real number, not str"; a non-numeric one is a bad request, not a 500.
        if isinstance(val, str):
            try:
                val = float(val)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"{op!r} on {key!r} needs a numeric value, got {val!r}")
        args.append(val)
        sql = f"{num} {op} ${arg_idx}::double precision"
        return sql, arg_idx + 1
    # equality / inequality (numeric or string). An unrecognized operator must not
    # silently match all rows — surface it as a 400 like parse_filter_node does.
    if op not in ('=', '==', '!=', '<>'):
        raise HTTPException(status_code=400, detail=f"Unknown filter operator for label element: {op!r}")
    sql_op = '<>' if op in ('!=', '<>') else '='
    if is_num:
        args.append(val)
        sql = f"{num} {sql_op} ${arg_idx}::double precision"
    else:
        args.append(str(val))
        sql = f"{base} {sql_op} ${arg_idx}"
    return sql, arg_idx + 1


def _parse_element_filter(node, args: List[Any], arg_idx: int) -> tuple[str, int]:
    """Recursively parse a filter node evaluated against a JSONB array element ``elem``."""
    if not isinstance(node, dict):
        return "TRUE", arg_idx
    if 'logic' in node:
        logic = node['logic'].upper()
        if logic not in ('AND', 'OR', 'NOT'):
            raise HTTPException(status_code=400, detail=f"Invalid logic operator: {node['logic']!r}")
        subs = node.get('filters', [])
        if not subs:
            return "TRUE", arg_idx
        parts = []
        for f in subs:
            sql, arg_idx = _parse_element_filter(f, args, arg_idx)
            parts.append(sql)
        if logic == 'NOT':
            return f"NOT ({' AND '.join(parts)})", arg_idx
        return f"({(' ' + logic + ' ').join(parts)})", arg_idx
    if 'field' in node:
        return _element_predicate_sql(node['field'], node.get('operator', '='), node.get('value'), args, arg_idx)
    return "TRUE", arg_idx


def _text_value(val) -> str:
    """Normalize a Python scalar to the text a JSONB `->>'field'` yields.

    Critically, JSON booleans render as lowercase 'true'/'false' — not Python's
    'True'/'False' — so a boolean equality filter actually matches.
    """
    if isinstance(val, bool):
        return 'true' if val else 'false'
    return str(val)


def _numeric_metadata_sql(field: str, source: str = 'metadata') -> str:
    """SQL for a metadata field's numeric value, NULL for non-numeric/missing rows.

    Guards the ::float cast so a text/mixed field doesn't 500 the whole query on a
    non-numeric row. Accepts both JSON numbers and numeric strings — some
    fields store numbers as strings (e.g. "0", "103000002"). `field` must already
    be validated. Used for UNpromoted fields; promoted numeric fields use the bare
    cast so the query matches their float index (see parse_filter_node). `source` is the JSON column: a mirror's own
    fields are in local_metadata.
    """
    return (
        f"CASE WHEN jsonb_typeof({source}->'{field}') = 'number' "
        f"THEN ({source}->>'{field}')::float "
        f"WHEN jsonb_typeof({source}->'{field}') = 'string' "
        f"AND {source}->>'{field}' ~ '^-?[0-9]+(\\.[0-9]+)?([eE][-+]?[0-9]+)?$' "
        f"THEN ({source}->>'{field}')::float END"
    )


def _equality_number(val, numeric_field: bool) -> Optional[float]:
    """The number an equality op (=, !=, in, not in) compares with, or None to compare as text.

    A JSON number from the client always compares numerically: JSONB renders a stored 1.0 as
    the text '1.0', which never equals '1', so `aspect_ratio = 1` matched nothing on a field
    whose every value is 1.0. A numeric STRING compares numerically
    only on a promoted numeric field, whose values are all numbers; on any other field a string
    stays an exact text match ('007' must not match 7).
    """
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        num = float(val)
    elif numeric_field and isinstance(val, str):
        try:
            num = float(val)
        except ValueError:
            return None
    else:
        return None
    return num if math.isfinite(num) else None


# A date or datetime written to day, minute or second precision, with no fraction and no zone.
_ISO_PARTIAL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:([T ])(\d{2}:\d{2})(:\d{2})?)?$")


def _iso_unit_window(val) -> Optional[tuple[str, str]]:
    """[lo, hi) covering the whole day / minute / second a partial ISO value names, both in the
    value's own format: '2024-01-02' -> ('2024-01-02', '2024-01-03'), '2024-01-02T23:59' ->
    ('2024-01-02T23:59', '2024-01-03T00:00'). None for anything else (full precision, a zone,
    not a date), which keeps its exact comparison.

    The datetime picker emits minute precision while stored values carry seconds, so `=` never
    matched, and ISO text compares '2024-01-02T00:00:00' > '2024-01-02', so `<= date`
    dropped the whole boundary day. Treating the value as its unit fixes both and stays a
    text range — ISO strings in one format sort chronologically, which is why the range ops
    compare text (it rides the promoted field's btree index).
    """
    if not isinstance(val, str):
        return None
    m = _ISO_PARTIAL_RE.match(val)
    if not m:
        return None
    date_part, sep, hhmm, ss = m.groups()
    try:
        if sep is None:
            day = datetime.date.fromisoformat(date_part)
            return val, (day + datetime.timedelta(days=1)).isoformat()
        start = datetime.datetime.fromisoformat(f"{date_part}T{hhmm}{ss or ''}")
    except ValueError:  # e.g. 2024-02-30 — not a date after all; compare it as given
        return None
    if ss is None:
        return val, (start + datetime.timedelta(minutes=1)).strftime(f"%Y-%m-%d{sep}%H:%M")
    return val, (start + datetime.timedelta(seconds=1)).strftime(f"%Y-%m-%d{sep}%H:%M:%S")


def _datetime_window(value) -> Optional[tuple[datetime.datetime, datetime.datetime]]:
    """A filter value's [start, end) on a datetime field: the UTC day, minute or second it was written to, or the one
    microsecond it names (backend/datetimes.py literal_window). None for a value that isn't a datetime."""
    window = literal_window(value)
    if window is None:
        return None
    start, end = window
    return start, end or start + datetime.timedelta(microseconds=1)


def _datetime_leaf_sql(field, col, op, val, args, arg_idx, indexed) -> Optional[tuple[str, int]]:
    """A comparison on a field declared datetime, by instant: `=` is the window the value was written to (a whole
    UTC day for a date), `!=` its complement, `in` any of them, `not in` none, and a range starts or ends where the
    value's unit does. Every predicate is TRUE or FALSE, never NULL, so a complement includes the samples without a
    datetime, as `!=` always has. A promoted field is compared through its index. Returns None for comparisons the
    field makes as any other does: equality with a value that isn't a datetime compares text (a stray "unknown"
    can still be found), with a JSON number, numbers."""

    next_idx = arg_idx

    def param(value):
        nonlocal next_idx
        args.append(value)
        next_idx += 1
        return f"${next_idx - 1}"

    def bound(instant):
        return f"{param(instant)}::timestamptz", instant.date()

    if op in ('>', '>=', '<', '<='):
        if val is None or (isinstance(val, str) and val.strip() == ''):
            return "TRUE", arg_idx  # an unfinished filter (a picker with no value yet) filters nothing
        window = _datetime_window(val)
        if window is None:
            raise HTTPException(
                status_code=400,
                detail=f"{field} is a datetime field: compare it with a date and time, such as 2026-01-31T12:00:00Z",
            )
        start, end = window
        lower, upper = {'>=': (start, None), '>': (end, None), '<': (None, start), '<=': (None, end)}[op]
        sql = window_sql(
            col,
            indexed,
            lower=bound(lower) if lower is not None else None,
            upper=bound(upper) if upper is not None else None,
        )
        return sql, next_idx
    if op in ('=', '!='):
        window = _datetime_window(val)
        if window is None:
            return None
        sql = window_sql(col, indexed, lower=bound(window[0]), upper=bound(window[1]))
        return (sql if op == '=' else f"(NOT {sql})"), next_idx
    if op in ('in', 'not in'):
        values = val if isinstance(val, list) else [val]
        windows = [window for window in map(_datetime_window, values) if window is not None]
        if not windows:
            return None
        clauses = [window_sql(col, indexed, lower=bound(start), upper=bound(end)) for start, end in windows]
        others = [_text_value(value) for value in values if _datetime_window(value) is None]
        if others:
            clauses.append(f"COALESCE({col} = ANY({param(others)}::text[]), FALSE)")
        sql = f"({' OR '.join(clauses)})"
        return (sql if op == 'in' else f"(NOT {sql})"), next_idx
    return None


def _native_timestamp_sql(field, col, op, val, args, arg_idx) -> Optional[tuple[str, int]]:
    """A comparison on created_at or last_modified_at (timestamptz columns) by the rule every datetime follows
    (backend/datetimes.py literal_window): a value means the UTC day, minute or second it was written to, whatever its
    offset, and one written with a fraction of a second that exact microsecond. `=` is that window, `!=` its
    complement, `in` any of them, `not in` none, and a range starts or ends where the unit does; a sample without the
    timestamp is in every complement. Plain comparisons of the column, so (dataset_id, created_at)'s index serves
    them. None for the other operators (presence)."""
    values = val if isinstance(val, list) and op in ('in', 'not in') else [val]
    if op not in ('=', '!=', 'in', 'not in', '>', '>=', '<', '<=') or any(value is None for value in values):
        return None  # presence, or `= null`, compile as before
    windows = []
    for value in values:
        window = _datetime_window(value)
        if window is None:
            raise HTTPException(status_code=400, detail=f"Invalid {field} value: {value!r}")
        windows.append(window)
    next_idx = arg_idx

    def param(instant):
        nonlocal next_idx
        args.append(instant)
        next_idx += 1
        return f"${next_idx - 1}"

    if op in ('>', '>=', '<', '<='):
        start, end = windows[0]
        sign, instant = {'>=': ('>=', start), '>': ('>=', end), '<': ('<', start), '<=': ('<', end)}[op]
        return f"{col} {sign} {param(instant)}", next_idx
    clauses = [f"({col} >= {param(start)} AND {col} < {param(end)})" for start, end in windows]
    within = clauses[0] if len(clauses) == 1 else f"({' OR '.join(clauses) or 'FALSE'})"  # FALSE: `in` []
    return (within if op in ('=', 'in') else f"({col} IS NULL OR NOT {within})"), next_idx


def _merge_datetime_ranges(filters, args, arg_idx, field_mapping, array_columns) -> tuple[list, list, int]:
    """Two or more range comparisons on one datetime field, ANDed (a slider's two handles, a histogram bar, `a >= x
    AND a < y`), become one window, so the instant is computed once per row and only for rows near either bound.
    Returns the filters left to compile, the merged windows' SQL and the next parameter index."""
    datetimes = getattr(array_columns, 'datetime_fields', ())
    ranges: dict[str, list] = {}
    for node in filters:
        if not isinstance(node, dict):
            continue  # the compiler refuses it (400)
        field, op = node.get('field'), str(node.get('operator', '=')).lower()
        window = _datetime_window(node.get('value'))
        if (
            isinstance(field, str)
            and field in datetimes
            and op in ('>', '>=', '<', '<=')
            and not (field_mapping and field in field_mapping)
            and window is not None
        ):
            ranges.setdefault(field, []).append((node, op, window))
    merged = {field: nodes for field, nodes in ranges.items() if len(nodes) > 1}
    if not merged:
        return filters, [], arg_idx
    sqls = []
    for field, nodes in merged.items():
        lower = upper = None
        for _node, op, (start, end) in nodes:
            low, high = {'>=': (start, None), '>': (end, None), '<': (None, start), '<=': (None, end)}[op]
            lower = low if lower is None or (low is not None and low > lower) else lower
            upper = high if upper is None or (high is not None and high < upper) else upper
        if lower is not None and upper is not None and lower >= upper:
            sqls.append("FALSE")
            continue
        bounds: list[Optional[tuple[str, datetime.date]]] = []
        for instant in (lower, upper):
            if instant is None:
                bounds.append(None)
                continue
            args.append(instant)
            bounds.append((f"${arg_idx}::timestamptz", instant.date()))
            arg_idx += 1
        indexed = field in getattr(array_columns, 'promoted_datetime', ())
        sqls.append(window_sql(_field_text_sql(field), indexed, lower=bounds[0], upper=bounds[1]))
    used = {id(node) for nodes in merged.values() for node, _op, _window in nodes}
    return [node for node in filters if id(node) not in used], sqls, arg_idx


def _utc_datetime(text: str) -> datetime.datetime:
    """Parse an ISO datetime for a created_at (timestamptz) comparison; zone-less means UTC.

    asyncpg binds a NAIVE datetime to timestamptz as the backend HOST's local time, so a
    zone-less created_at value meant one zone on one host and UTC on another — and the histogram's
    bin labels (now UTC) would have filtered a window hours off.
    """
    dt = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.timezone.utc)


async def _instant_histogram(field, data_cte, args, minmax, total_rows, bin_count, degenerate) -> dict:
    """A date and time field's histogram, on whole microseconds (`val` in `data_cte`: microseconds since 1970).

    Each bar covers whole microseconds, and its labels are its first and last one, zone-less UTC text with six
    fraction digits: the filter a bar stages (`>= min_label AND <= max_label`, frontend/lib/filter-utils.ts) then
    matches exactly that bar's samples. Float edges drifted a microsecond either way of the bar they labelled."""
    lowest, highest = int(minmax['min']), int(minmax['max'])
    total = int(minmax['total'] or 0)
    wanted = 1 if degenerate else (bin_count or max(8, min(30, int(math.sqrt(total)) or 8)))
    width = -(-(highest - lowest + 1) // wanted)  # whole microseconds, at least one
    bin_count = -(-(highest - lowest + 1) // width)  # no bar past the latest value
    rows = await db.fetch(
        f"{data_cte} SELECT (val - {lowest}) / {width} + 1 AS bucket, COUNT(*) AS count FROM data "
        "WHERE val IS NOT NULL GROUP BY 1 ORDER BY 1",
        *args,
    )
    counts = {row['bucket']: row['count'] for row in rows}
    buckets = []
    for key in range(1, bin_count + 1):
        first = lowest + (key - 1) * width
        last = min(first + width, highest + 1) - 1
        min_label, max_label = _utc_micros_label(first), _utc_micros_label(last)
        buckets.append(
            {
                "key": key,
                "count": counts.get(key, 0),
                "label": f"{min_label} - {max_label}",
                "range": {"min": first / 1e6, "max": last / 1e6, "min_label": min_label, "max_label": max_label},
            },
        )
    missing = max(total_rows - total, 0)
    return {"field": field, "total": total_rows, "missing": missing, "buckets": buckets, "bins": bin_count}


def _utc_micros_label(micros: int) -> str:
    """Microseconds since 1970 as zone-less UTC text with six fraction digits, which filters read as that exact UTC
    microsecond (backend/datetimes.py; created_at as _utc_datetime does)."""
    instant = datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=micros)
    return f'{instant.year:04d}-{instant:%m-%dT%H:%M:%S}.{instant.microsecond:06d}'


def _escape_like(val) -> str:
    """`val` as a literal ILIKE substring: `%`, `_` and `\\` are pattern characters, so "contains
    %" used to match every row. Pair with `ESCAPE '\\'`."""
    return str(val).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _numeric_sort_order_by(field: str, promoted: bool, direction: str, source: str = 'metadata') -> str:
    """ORDER BY clause for sorting the grid by a numeric metadata field: samples without a value last, in both
    directions.

    A promoted numeric field uses the bare `(metadata->>'f')::float` cast (matching the promote_field index; safe
    because promotion only builds the index when every value is numeric). Its pages ride the index all the same:
    _sorted_page_ids reads the samples with a value in index order, then those without (one ORDER BY can't: the
    btree holds missing values last ascending, so first descending). Unpromoted fields keep the guarded CASE
    (cast-safe on mixed data) and pay the sort: there's no index to ride. `field` must already be validated.
    """
    if promoted:
        return f"ORDER BY ({source}->>'{field}')::float {direction} NULLS LAST"
    return f"ORDER BY {_numeric_metadata_sql(field, source)} {direction} NULLS LAST"


_NATIVE_SORT_COLUMNS = ('id', 'created_at', 'last_modified_at', 'filepath')
# The samples' own timestamps (timestamptz columns, not metadata keys): filters compare them as instants.
_NATIVE_TIMESTAMPS = ('created_at', 'last_modified_at')


async def _last_modified_field(dataset_id: int, lightning_mode: bool) -> Dict[str, Any]:
    """The samples' last_modified_at, a datetime filters compare like created_at. No index covers it (one would make
    every edit's UPDATE non-HOT), so its bounds are a scan of the dataset's rows: read only outside lightning mode, a
    dataset small enough to compute every field. Without them the sidebar shows no slider; the filter card, the query
    language and the histogram still filter by it. Its bounds are the dataset's, whatever the filter (`cached`)."""
    field: Dict[str, Any] = {"name": "last_modified_at", "type": "datetime", "provenance": "cached"}
    if not lightning_mode:
        bounds = await db.fetchrow(
            "SELECT MIN(last_modified_at) AS mn, MAX(last_modified_at) AS mx FROM samples WHERE dataset_id = $1",
            dataset_id,
        )
        if bounds and bounds['mn'] is not None:
            field['min'], field['max'] = bounds['mn'].isoformat(), bounds['mx'].isoformat()
    return field


def _grid_order_by(sort_by: str, direction: str, field_type_row) -> str:
    """The ORDER BY for a sorted grid / SDK page — always a TOTAL order.

    OFFSET paging over `ORDER BY <field>` is only stable when the order is total: among
    tied rows Postgres promises nothing, and a bounded sort returns ties in a different
    order for every LIMIT/OFFSET it runs, so page boundaries drift — the same sample
    lands on two pages while another is never returned, with `total` still correct.
    Ties are the normal case (every categorical field; `created_at` is one NOW() per
    ingest batch). Every clause built here therefore ends in `, id` (the pkey), except the bare
    `ORDER BY id` itself. Promoted numeric fields still ride their expression index for
    the leading key (incremental sort handles the id tiebreaker within a tie group).

    `sort_by` must already be validated; `field_type_row` is the dataset_fields row
    (field_type, is_promoted) or None, and is ignored for native columns.
    """
    if sort_by in _NATIVE_SORT_COLUMNS:
        if sort_by == 'id':
            return f"ORDER BY id {direction}"
        return f"ORDER BY {sort_by} {direction}, id"
    if not field_type_row:
        return "ORDER BY id"  # not a known field: fall back to the default order
    field_type = field_type_row['field_type']
    local = local_field(sort_by)  # a mirror's own field: its key in local_metadata
    source, key = ('local_metadata', local.name) if local is not None else ('metadata', sort_by)
    if local is not None and local.array:
        return "ORDER BY id"  # `@local.tags` is an array, as unsortable as tags
    if field_type in ('number', 'float', 'int', 'integer'):
        # Cast to float for numeric sorting; promoted fields ride their index.
        return _numeric_sort_order_by(key, bool(field_type_row['is_promoted']), direction, source) + ", id"
    if field_type == 'datetime':
        # By instant (backend/datetimes.py); samples without a datetime last. Pages: _sorted_page_ids.
        instant = instant_expr(f"{source}->>'{key}'", bool(field_type_row['is_promoted']))
        return f"ORDER BY {instant} {direction} NULLS LAST, id"
    if field_type in ('string', 'enum'):
        return f"ORDER BY {source}->>'{key}' {direction} NULLS LAST, id"
    return "ORDER BY id"  # unsupported type (array, object, ...): fall back to the default order


async def _sorted_page_ids(sort_by, direction, field_type_row, where_clause, args, limit, offset):
    """The ids of one page of a sort that one ORDER BY can't answer fast, in order; None for every other sort
    (_grid_order_by's ORDER BY answers those). Samples without a value come last in both directions, as
    _grid_order_by orders them:

    - an indexed (promoted) datetime or number field: the samples with a value in index order, then the rest by
      id (_two_part_page_ids);
    - a datetime field without an index: two cheap passes over the written dates (_datetime_page_ids).

    `where_clause` (" WHERE ...") uses `args` as $1..$len(args). OFFSET paging keeps its meaning."""
    if sort_by in _NATIVE_SORT_COLUMNS or not field_type_row:
        return None
    local = local_field(sort_by)
    if local is not None and local.array:
        return None
    text = _field_text_sql(sort_by)
    promoted = bool(field_type_row['is_promoted'])
    if field_type_row['field_type'] == 'datetime':
        if not promoted:
            return await _datetime_page_ids(text, direction, where_clause, args, limit, offset)
        key = instant_expr(text, True)
    elif field_type_row['field_type'] in ('number', 'float', 'int', 'integer') and promoted:
        key = f"({text})::float"
    else:
        return None
    return await _two_part_page_ids(key, direction, where_clause, args, limit, offset)


async def _two_part_page_ids(key, direction, where_clause, args, limit, offset) -> list:
    """A page sorted by an indexed `key`, samples without a value last, still an index scan with no sort of
    the dataset: the samples with a value in the index's own order (its missing values are last ascending and
    first descending, and there are none here), then, once those run out, the samples without one by id."""
    n = len(args)
    nulls = 'NULLS LAST' if direction == 'ASC' else 'NULLS FIRST'
    rows = await db.fetch(
        f"SELECT id FROM samples {where_clause} AND {key} IS NOT NULL "
        f"ORDER BY {key} {direction} {nulls}, id LIMIT ${n + 1} OFFSET ${n + 2}",
        *args,
        limit,
        offset,
    )
    ids = [row['id'] for row in rows]
    if len(ids) == limit:
        return ids
    skip = 0  # the page holds the last samples with a value: those without one start right after them
    if not ids:  # the page is past them all: how many there are decides where it starts
        with_value = await db.fetchval(f"SELECT count(*) FROM samples {where_clause} AND {key} IS NOT NULL", *args)
        skip = max(offset - with_value, 0)
    rows = await db.fetch(
        f"SELECT id FROM samples {where_clause} AND {key} IS NULL ORDER BY id LIMIT ${n + 1} OFFSET ${n + 2}",
        *args,
        limit - len(ids),
        skip,
    )
    return ids + [row['id'] for row in rows]


async def _datetime_page_ids(text, direction, where_clause, args, limit, offset) -> list:
    """A page sorted by a datetime field without an index, samples without a datetime last, without computing
    every sample's instant. A value written on date W names an instant within 15:59 of that day, so:

    1. find the date D of the page's last sample by the written date alone (newest first: the offset+limit-th
       latest date among values that start like a date);
    2. compute instants only for samples written within three days of D on the near side, and order those.

    When at least offset+limit of them are datetimes written within a day of D or beyond, every sample further
    away is later in the order than all of those (newest first: it names an instant before D - 3 + 15:59, they
    after D - 1 - 15:59), so the page is exact. Otherwise (values that look like dates but aren't, or fewer
    datetimes than the page needs) it falls back to the full sort.

    Each pass reads a sample's text once (instants_from, read_once)."""
    n = len(args)
    rows_sql = f"samples {where_clause}"
    newest = direction == 'DESC'
    cutoff = await db.fetchval(
        f"SELECT _date FROM (SELECT {date_sql(text)} AS _date FROM {rows_sql} OFFSET 0) AS _dates "
        f"WHERE _date BETWEEN '0001-01-01' AND '9999-12-31' ORDER BY 1 {direction} LIMIT 1 OFFSET ${n + 1}",
        *args,
        offset + limit - 1,
    )
    try:
        day = datetime.date.fromisoformat(cutoff) if cutoff else None
        step = datetime.timedelta(days=-1 if newest else 1)
        far, near = (day + 3 * step, day + step) if day else (None, None)
    except (ValueError, OverflowError):
        far = near = None
    if far is not None:
        side = '>=' if newest else '<='
        # The written date picks the candidates before the fence: a sample further away is read once, as before.
        candidates = instants_from(text, f"{rows_sql} AND {date_sql(text)} {side} '{far}'", ('id',))
        rows = await db.fetch(
            f"SELECT id, count(*) FILTER (WHERE near) OVER () AS near_count FROM ("
            f"SELECT id, _instant AS instant, {date_sql('_text')} {side} '{near}' AS near FROM {candidates}"
            f") candidates WHERE instant IS NOT NULL "
            f"ORDER BY instant {direction}, id LIMIT ${n + 1} OFFSET ${n + 2}",
            *args,
            limit,
            offset,
        )
        if rows and rows[0]['near_count'] >= offset + limit:
            return [row['id'] for row in rows]
    rows = await db.fetch(
        f"SELECT id FROM samples {read_once(text)} {where_clause} ORDER BY {instant_sql('_text')} {direction} "
        f"NULLS LAST, id LIMIT ${n + 1} OFFSET ${n + 2}",
        *args,
        limit,
        offset,
    )
    return [row['id'] for row in rows]


async def get_promoted_numeric_fields(dataset_id: int, source=None) -> set:
    """Promoted metadata fields that have a numeric float index for this dataset.

    Range filters on these can use the bare `(metadata->>'f')::float` cast — it
    matches the promote_field index (so the planner can avoid a sequential scan)
    and is cast-safe because the index only builds when every value is numeric.
    """
    rows = await (source or db).fetch(
        "SELECT field_name FROM dataset_fields "
        "WHERE dataset_id = $1 AND is_promoted = TRUE "
        "AND field_type IN ('number', 'float', 'int', 'integer')",
        dataset_id,
    )
    return {r['field_name'] for r in rows}


# Ops that match by their OWN machinery (embeddings, region tokens, parent links, geo) or that
# legitimately want missing-key rows (is_null / the "(no value)" filter) — a leaf using one
# never vacuously leaks the whole grain, so the cross-grain presence guard must NOT suppress it.
_CROSSGRAIN_SKIP_OPS = {
    'embedding_polygon',
    'embedding_polygon_exclude',
    'in_selection',
    'concept_select',
    'concept_exclude',
    'geo_polygon',
    'geo_polygon_exclude',
    'geo_near',
    'collapse_duplicates',
    'cg_self',
    'parent_matches',
    'has_matching_object',
    unlinked_objects.OPERATOR,
    'is_null',
    'exists',
    'not_exists',
}
# Native columns that exist on every row (no JSONB-presence check needed).
_CROSSGRAIN_NATIVE_FIELDS = {'id', 'filepath', 'created_at', 'last_modified_at', 'media_type'}
# All negation operators, however spelled: the frontend emits 'not in' (with a space) for facet /
# multi-select excludes; the dotted-label/array path also accepts 'not_in'/'nin'.
_STRICT_NEG_OPS = {'!=', '<>', 'not in', 'not_in', 'nin'}


def _top_field(field: str) -> str:
    """The field a filter leaf's presence depends on: a dotted metadata path's top key, or a whole `@local.` name."""
    return field if field.startswith(LOCAL_PREFIX) else field.split('.', 1)[0]


def _collect_top_meta_fields(node: Dict[str, Any], array_columns: Optional[set] = None) -> set:
    """Distinct top-level METADATA field names `node` references (excluding native columns and
    virtual ops). Used to attach a field-presence check to a NOT group."""
    fields: set = set()
    arrays = array_columns if array_columns is not None else _get_array_columns_sync()

    def walk(n: Any) -> None:
        if not isinstance(n, dict):
            return
        if 'filters' in n or 'logic' in n:
            for c in n.get('filters', []):
                walk(c)
            return
        if str(n.get('operator', '=')).lower() in _CROSSGRAIN_SKIP_OPS:
            return
        f = n.get('field')
        if isinstance(f, str):
            top = _top_field(f)
            if (
                top not in _CROSSGRAIN_NATIVE_FIELDS
                and top not in arrays
                and top != LOCAL_TAGS
                and _queryable_field(top)
            ):
                fields.add(top)

    walk(node)
    return fields


def _strictify_negations(node: Any, partner: bool = False, array_columns: Optional[set] = None) -> Any:
    """Transform `node` for use inside the cross-grain link so a NULL-tolerant negation can't
    vacuously match a row that lacks the field. PER-LEAF (correct even inside an OR — unlike a
    single whole-filter guard, which a present-key positive in a sibling arm defeats): each
    metadata negation (!=, not in, not_in, ...) is ANDed with an existence check on ITS OWN
    field. NATIVE-column exclusions (id/filepath/... — always present, so an exists check can't
    help) are DROPPED in the partner subquery (`partner=True`): a same-grain row-identity
    exclusion has no meaning for the cross-grain partner, which would otherwise trivially satisfy
    it and leak the whole grain (e.g. the duplicates "collapse" id-not-in). In the self branch
    (`partner=False`) native exclusions are kept (they apply to the same grain). Positive filters,
    the lasso, selections, and is_null pass through unchanged.

    Returns a filter dict, or a sentinel: True (reduced to no constraint) / False (no match) /
    None (drop this leaf — a partner-mode native exclusion). Callers handle the sentinels."""
    if not isinstance(node, dict):
        return node
    arrays = array_columns if array_columns is not None else _get_array_columns_sync()
    if 'filters' in node or 'logic' in node:
        logic = str(node.get('logic', 'AND')).upper()
        kids = [_strictify_negations(c, partner, array_columns) for c in node.get('filters', [])]
        kids = [k for k in kids if k is not None]
        if logic == 'NOT':
            # A NOT group (e.g. the sidebar's exclude-contains / exclude-range) is NULL-tolerant
            # too; if it concerns exactly one metadata field, require that field present.
            real = [k for k in kids if not isinstance(k, bool)]
            if not real:
                return None
            rebuilt = {'logic': 'NOT', 'filters': real}
            fields = _collect_top_meta_fields(node, array_columns)
            if len(fields) == 1:
                only = next(iter(fields))
                return {'logic': 'AND', 'filters': [rebuilt, {'field': only, 'operator': 'exists'}]}
            return rebuilt
        if logic == 'OR':
            if any(k is True for k in kids):
                return True
            kept = [k for k in kids if k is not False]
            return {'logic': 'OR', 'filters': kept} if kept else False
        # AND (and an implicit-AND node carrying `filters` but no explicit logic)
        if any(k is False for k in kids):
            return False
        kept = [k for k in kids if k is not True]
        return {'logic': 'AND', 'filters': kept} if kept else True
    op = str(node.get('operator', '=')).lower()
    f = node.get('field')
    if op in _STRICT_NEG_OPS and isinstance(f, str):
        top = _top_field(f)
        if top in _CROSSGRAIN_NATIVE_FIELDS or top in arrays or top == LOCAL_TAGS:
            return None if partner else node
        if _queryable_field(top):
            return {'logic': 'AND', 'filters': [node, {'field': top, 'operator': 'exists'}]}
    # Absence ops (is_null / not_exists) are NULL-tolerant the same way: rendered against the
    # cross-grain PARTNER — the other grain, which lacks the field — they are vacuously TRUE and
    # leak the whole partner grain (an image-grid "(no value)" filter would match every image that
    # merely HAS a child object). Drop them from the partner subquery (None → the partner arm
    # contributes no match), leaving the self branch (the row's own grain) intact. Native columns
    # exist on both grains, so keep those. (The exists-guard rewrite used for !=/not in would be
    # contradictory for an absence op, hence a drop.) NOTE: this fixes the partner leak; the
    # symmetric self case — an image-only field's absence on the OBJECT grid still matching every
    # object — is a known limitation that needs per-grain field-presence to resolve.
    if partner and op in ('is_null', 'not_exists') and isinstance(f, str):
        top = _top_field(f)
        if top not in _CROSSGRAIN_NATIVE_FIELDS:
            return None
    # concept_exclude is a NULL-tolerant negation too (NOT EXISTS a passing score row): on a row
    # whose grain doesn't OWN this concept's embedding field there is no score row at all, so the
    # exclude vacuously matches and leaks the whole grain across the cross-grain link. Require the
    # concept PRESENT (a score row exists for this row) in BOTH branches: the off-grain row — be it
    # the SELF row (opposite-grain concept on this grid) or the PARTNER row (same-grain concept, so
    # the partner is off-grain) — then can't vacuously satisfy the NOT EXISTS. When the row's grain
    # DOES own the concept the presence guard is simply TRUE, so the exclude is unchanged.
    if op == 'concept_exclude':
        cval = node.get('value')
        cfg = cval if isinstance(cval, dict) else {}
        cid = cfg.get('concept_id')
        if cid is not None:
            return {
                'logic': 'AND',
                'filters': [node, {'field': '_cpresent', 'operator': 'concept_present', 'value': {'concept_id': cid}}],
            }
    return node


def _filter_concept_ids(node: Any) -> set:
    """Concept ids a filter tree names, including cross-grain sub-filters, for the sidebar's up-front
    check. A compiled filter is guarded by the ids its compiler collects, not by this walk."""
    found: set = set()
    if not isinstance(node, dict):  # a value list (`in` ids, a polygon) never holds a filter node
        return found
    value = node.get('value')
    operator = str(node.get('operator', '')).lower()  # the compiler lower-cases operators too
    if operator in ('concept_select', 'concept_exclude', 'concept_present') and isinstance(value, dict):
        with suppress(KeyError, TypeError, ValueError):  # no id is a no-op; a malformed one the compiler's 400
            found.add(int(value['concept_id']))
    for child in [*(node.get('filters') or []), value]:
        found |= _filter_concept_ids(child)
    return found


async def require_ready_concepts(filter_obj: Any) -> None:
    """409 (SQLSTATE 55000) for a concept whose definition changed and whose scoring job has not
    finished; the same guard every concept filter query runs once."""
    concept_ids = sorted(_filter_concept_ids(filter_obj))
    if concept_ids:
        await db.fetchval('SELECT require_concept_scores($1::int[])', concept_ids)


# The `field_mapping` every caller that queries `FROM samples s JOIN ...` must pass.
#
# parse_filter_node emits native columns BARE (`id`, `created_at`, ...) unless field_mapping
# qualifies them. Both tables joined against samples today — `embeddings` and `sample_hashes` —
# have their own `id`, and `embeddings` also has `created_at`, so an unqualified one is
# "column reference is ambiguous": a 500, not a wrong answer.
#
# This used to be hand-copied at each call site, and the `created_at` half was only ever added to
# half of them — so the Sparse Sample panel 500'd on the single most common filter on a streamed
# dataset while the identical filter worked in search and embeddings. Import this instead of
# writing the dict inline. EXTEND IT if a new query joins a table that shares another native
# samples column (`media_type`, `filepath`, `tags`, ...); nothing else joins one today.
# Fields the sidebar doesn't list (field-stats marks them `sidebar: false` and computes nothing for them there).
SIDEBAR_HIDDEN_FIELDS = frozenset({'parent_id'})

SAMPLES_JOIN_FIELD_MAPPING: Dict[str, str] = {
    'id': 's.id',
    'created_at': 's.created_at',
}


def _realiased(field_mapping: Optional[Dict[str, str]], alias: str) -> Dict[str, str]:
    """`field_mapping` for a subquery over samples aliased `alias`: every mapped column on the inner alias. A
    cross-grain subquery that kept the outer `s.created_at` compared the outer row's date on every partner."""
    return {key: f"{alias}.{column.rsplit('.', 1)[-1]}" for key, column in (field_mapping or {}).items()}


def _id_list_bound(node: Any) -> Optional[list]:
    """The ids of an `id in [...]` that every row matching `node` must have (a leaf of it, or of ANDs around it, as a
    selection or a search's results), or None when no such list bounds it."""
    if not isinstance(node, dict):
        return None
    if 'logic' in node or 'filters' in node:
        if str(node.get('logic', 'AND')).upper() != 'AND':
            return None
        return next((ids for ids in map(_id_list_bound, node.get('filters') or []) if ids is not None), None)
    values = node.get('value')
    if node.get('field') == 'id' and str(node.get('operator', '=')).lower() == 'in' and isinstance(values, list):
        return values if all(type(value) is int for value in values) else None
    return None


def parse_filter_node(
    node: Dict[str, Any],
    args: List[Any],
    arg_idx: int,
    field_mapping: Optional[Dict[str, str]] = None,
    numeric_indexed_fields: Optional[set] = None,
    dataset_id: Optional[int] = None,
    array_columns: Optional[set] = None,
) -> tuple[str, int]:
    """
    Compiles a whole filter tree for a statement's WHERE and returns (sql_snippet, next_arg_idx).

    `numeric_indexed_fields`: promoted numeric fields (from
    get_promoted_numeric_fields) whose range comparisons should use the bare
    index-matching ::float cast instead of the slower guarded CASE.

    A filter that reads concept scores starts with ONE guard over all its concepts:
    a concept whose scoring job has not finished refuses the statement (409) in its own snapshot.
    ANDed into the WHERE it is a One-Time Filter on the unchanged parallel plan; inside each concept
    EXISTS it can become a per-row join filter and change the query plan.
    The compiler collects the ids where it compiles each score read, so no read goes unguarded.
    """
    concept_ids: set = set()
    sql, arg_idx = _compile_filter_node(
        node,
        args,
        arg_idx,
        field_mapping,
        numeric_indexed_fields,
        dataset_id,
        array_columns=array_columns,
        concepts=concept_ids,
    )
    if not concept_ids:
        return sql, arg_idx
    args.append(sorted(concept_ids))
    return f"((SELECT require_concept_scores(${arg_idx}::int[])) AND ({sql}))", arg_idx + 1


def _compile_filter_node(
    node: Dict[str, Any],
    args: List[Any],
    arg_idx: int,
    field_mapping: Optional[Dict[str, str]] = None,
    numeric_indexed_fields: Optional[set] = None,
    dataset_id: Optional[int] = None,
    array_columns: Optional[set] = None,
    *,
    concepts: set,
) -> tuple[str, int]:
    """Recursively compiles a filter node. Every concept id whose scores it reads goes into `concepts`,
    for the statement's one guard (parse_filter_node)."""
    # Logic Node (AND/OR/NOT). A node with `filters` but no explicit `logic` is
    # treated as an implicit AND rather than silently dropped.
    if 'logic' in node or 'filters' in node:
        logic = node.get('logic', 'AND').upper()
        if logic not in ('AND', 'OR', 'NOT'):
            raise HTTPException(status_code=400, detail=f"Invalid logic operator: {node['logic']!r}")
        sub_filters = node.get('filters', [])

        if logic == 'NOT':
            if not sub_filters:
                return "TRUE", arg_idx

            child_sqls = []
            for f in sub_filters:
                sql, arg_idx = _compile_filter_node(
                    f,
                    args,
                    arg_idx,
                    field_mapping,
                    numeric_indexed_fields,
                    dataset_id,
                    array_columns=array_columns,
                    concepts=concepts,
                )
                child_sqls.append(sql)

            combined = " AND ".join(child_sqls)
            # COALESCE→FALSE so a child that is NULL for missing-key rows still
            # falls into the NOT complement, consistent with != / not in.
            return f"NOT (COALESCE({combined}, FALSE))", arg_idx

        else:  # AND / OR
            if not sub_filters:
                return "TRUE", arg_idx

            child_sqls = []
            if logic == 'AND':
                sub_filters, child_sqls, arg_idx = _merge_datetime_ranges(
                    sub_filters,
                    args,
                    arg_idx,
                    field_mapping,
                    array_columns,
                )
            for f in sub_filters:
                sql, arg_idx = _compile_filter_node(
                    f,
                    args,
                    arg_idx,
                    field_mapping,
                    numeric_indexed_fields,
                    dataset_id,
                    array_columns=array_columns,
                    concepts=concepts,
                )
                child_sqls.append(sql)

            combined = f" {logic} ".join(child_sqls)
            return f"({combined})", arg_idx

    # Leaf Node (Field Condition)
    elif 'field' in node:
        field = node['field']
        op = node.get('operator', '=').lower()
        val = node.get('value')

        # "Collapse duplicates" toggle: hide non-representative perceptual-hash dupes.
        # `value` is the hash_config (a hash field_name, or '_all').
        # Dispatched before field validation since `field` is just a sentinel here.
        if op == 'collapse_duplicates':
            if not val:
                return "TRUE", arg_idx
            # Reference the OUTER sample id the way this caller does (bare `id` for the
            # unaliased samples query, `s.id` for the embeddings/search joins) — the
            # secondary table has no `id` column so the correlation is unambiguous.
            id_col = (field_mapping or {}).get('id', 'id')
            args.append(val)
            return (
                f"NOT EXISTS (SELECT 1 FROM sample_dup_secondary _dup "
                f"WHERE _dup.sample_id = {id_col} AND _dup.hash_config = ${arg_idx})",
                arg_idx + 1,
            )

        # Objects with no image, of some kinds (unlinked_objects.py): the finder's filter, and the bulk delete's.
        # `field` is a sentinel. The row's dataset is a constant, as the cross-grain operators below take it.
        if op == unlinked_objects.OPERATOR:
            ds = dataset_id if dataset_id is not None else (args[0] if args and isinstance(args[0], int) else None)
            id_col = (field_mapping or {}).get('id', 'samples.id')
            table = id_col.rsplit('.', 1)[0] if '.' in id_col else 'samples'
            try:
                return unlinked_objects.compile_leaf(
                    val,
                    table,
                    str(int(ds)) if ds is not None else f'{table}.dataset_id',
                    arg_idx,
                    args,
                )
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from None

        # Embedding "concept" select/exclude: keep / hide samples whose
        # materialized concept score passes a threshold. `value` = {concept_id, op, threshold};
        # scores live in the narrow embedding_concept_scores table. Like collapse_duplicates,
        # dispatched before field validation (`field` is a sentinel) and id-safe via
        # field_mapping for the joined (embeddings/search) callers. Auto-applies to new data:
        # the maintenance job scores newly-streamed samples, so a saved exclude catches them.
        if op in ('concept_select', 'concept_exclude'):
            cfg = val if isinstance(val, dict) else {}
            cid = cfg.get('concept_id')
            if cid is None:
                return "TRUE", arg_idx
            cmp = str(cfg.get('op', '>'))
            if cmp not in ('>', '>=', '<', '<='):
                raise HTTPException(status_code=400, detail=f"Invalid concept threshold op: {cmp!r}")
            id_col = (field_mapping or {}).get('id', 'id')
            args.append(int(cid))
            args.append(float(cfg.get('threshold', 0.5)))
            concepts.add(int(cid))
            # No guard in here: parse_filter_node puts one for every concept in front of the whole filter.
            inner = (
                f"SELECT 1 FROM embedding_concept_scores _cs "
                f"WHERE _cs.sample_id = {id_col} AND _cs.concept_id = ${arg_idx} "
                f"AND _cs.score {cmp} ${arg_idx + 1}"
            )
            exists = f"EXISTS ({inner})" if op == 'concept_select' else f"NOT EXISTS ({inner})"
            return exists, arg_idx + 2

        # Presence-only concept guard (internal; emitted by _strictify_negations for the cross-grain
        # SELF branch). Does THIS row have ANY score for the concept? Pairs with concept_exclude so a
        # row whose grain doesn't own the concept's embedding field (and thus has no score rows)
        # can't vacuously satisfy the NOT EXISTS and leak the whole grain.
        if op == 'concept_present':
            cfg = val if isinstance(val, dict) else {}
            cid = cfg.get('concept_id')
            if cid is None:
                return "TRUE", arg_idx
            id_col = (field_mapping or {}).get('id', 'id')
            args.append(int(cid))
            concepts.add(int(cid))
            return (
                f"EXISTS (SELECT 1 FROM embedding_concept_scores _cs "
                f"WHERE _cs.sample_id = {id_col} AND _cs.concept_id = ${arg_idx})"
            ), arg_idx + 1

        # Cross-grain link operators (objects are media_type='object' rows whose
        # samples.parent_id is their source image, in the SAME dataset). withGrain
        # wraps a panel's base filter so the two grains stay coupled: an OBJECT grid shows the
        # objects of whatever images match, and an IMAGE grid shows the parent images of
        # whatever objects match — for ANY filter/selection, not just the embeddings lasso.
        # `field` is a sentinel; `value` is a sub-filter node (the panel's grain-less base),
        # re-rendered against the related rows (id remapped to the sub-alias so embedding/
        # concept/id leaves correlate to it; bare metadata/native columns bind to it naturally).
        # Dispatched before field validation, like concept_select.
        #
        # The sub-query is scoped to this dataset as a CONSTANT (an int we control), so a hashed
        # partner set is built from this dataset's rows only, not the whole (multi-million-row)
        # table. The dataset id comes from the explicit param OR, for callers that don't thread
        # it (field-stats, map, selection_token), args[0] — every dataset-scoped query seeds args
        # with [dataset_id], so args[0] is reliably it. Without either, an image's objects are
        # found by its own dataset_id, still one probe of (dataset_id, parent_id).
        # cg_self: the cross-grain link's SAME-grain branch — render the sub-filter (with each
        # negation strictified PER-LEAF so an exclusion on a metadata field this grain LACKS can't
        # vacuously match every row, the whole-grain leak) against the current row. Positive
        # filters, the lasso, selections, and is_null pass through _strictify_negations unchanged.
        if op == 'cg_self':
            if not isinstance(val, dict):
                return "TRUE", arg_idx
            strict = _strictify_negations(val, partner=False, array_columns=array_columns)
            if strict is False:
                return "FALSE", arg_idx
            if not isinstance(strict, dict):  # True / None -> no constraint on the same-grain row
                return "TRUE", arg_idx
            sub_sql, arg_idx = _compile_filter_node(
                strict,
                args,
                arg_idx,
                field_mapping,
                numeric_indexed_fields,
                dataset_id,
                array_columns=array_columns,
                concepts=concepts,
            )
            return f"({sub_sql})", arg_idx

        if op in ('parent_matches', 'has_matching_object'):
            if not isinstance(val, dict):
                return "TRUE", arg_idx
            # Strictify for the PARTNER: drops same-grain-only (native id/filepath) exclusions that
            # have no cross-grain meaning. If nothing partner-meaningful remains, no partner matches.
            strict = _strictify_negations(val, partner=True, array_columns=array_columns)
            if not isinstance(strict, dict):
                return "FALSE", arg_idx
            ds = dataset_id if dataset_id is not None else (args[0] if args and isinstance(args[0], int) else None)
            id_col = (field_mapping or {}).get('id', 'samples.id')
            outer_tbl = id_col.rsplit('.', 1)[0] if '.' in id_col else 'samples'
            # The link is samples.parent_id (0029): an object's image, NULL unless its metadata.parent_id names a
            # non-object of its dataset. Each operator is one EXISTS keyed on it, which the planner runs either
            # as a hashed set built once (when it expects few partners) or as one index probe per row:
            # (dataset_id, parent_id) from an image, the (dataset_id, id) key from an object. Neither re-scans
            # the table per row, as `IN (subquery)` / EXISTS on the JSON expression they read until 0029 did
            # (30-55 s), nor searches a whole array per row, as `= ANY(ARRAY(subquery))` did for every row a
            # count visits. Every unlinked row is FALSE, where the JSON read gave FALSE or NULL: the same rows,
            # since a NOT here coalesces its child to FALSE.
            if op == 'has_matching_object':
                # IMAGE row matches if its id is the parent of an object satisfying the sub-filter.
                sub_map = {**_realiased(field_mapping, 'o'), 'id': 'o.id'}
                sub_sql, arg_idx = _compile_filter_node(
                    strict,
                    args,
                    arg_idx,
                    sub_map,
                    numeric_indexed_fields,
                    ds,
                    array_columns=array_columns,
                    concepts=concepts,
                )
                scope = int(ds) if ds is not None else f"{outer_tbl}.dataset_id"
                return (
                    f"EXISTS (SELECT 1 FROM samples o WHERE o.dataset_id = {scope} AND o.parent_id = {id_col} "
                    f"AND o.media_type = 'object' AND ({sub_sql}))"
                ), arg_idx
            # parent_matches: OBJECT row matches if its parent image satisfies the sub-filter.
            ds_scope = f"s2.dataset_id = {int(ds)} AND " if ds is not None else ""
            sub_map = {**_realiased(field_mapping, 's2'), 'id': 's2.id'}
            sub_sql, arg_idx = _compile_filter_node(
                strict,
                args,
                arg_idx,
                sub_map,
                numeric_indexed_fields,
                ds,
                array_columns=array_columns,
                concepts=concepts,
            )
            link = (
                f"EXISTS (SELECT 1 FROM samples s2 WHERE {ds_scope}s2.id = {outer_tbl}.parent_id "
                f"AND s2.media_type != 'object' AND ({sub_sql}))"
            )
            # A sub-filter within an id list (a selection, a search's results) matches only images of that list, so
            # the object's image must be one of those ids. Said again on the row, `parent_id = ANY(ids)` lets the
            # planner find those objects by idx_samples_dataset_parent, and the grid's `id in ids OR ...` by two
            # index scans. EXISTS alone is a condition on every object of the dataset, which the planner costs as
            # one probe per object (cost 1.2M on a 37,000-object copy): past jit_inline_above_cost, so each count and
            # page was JIT-compiled, 0.2 to 0.3 s for a 15 ms scan (measured). The rows are the same and never
            # NULL: an unlinked object's NULL = ANY(...) is ANDed with its FALSE EXISTS. The list is a constant, so
            # a plan that still reads every object looks it up in a hash table. has_matching_object can't say its
            # images without a sub-query, and an array from one is searched element by element on every row.
            ids = _id_list_bound(strict)
            if ids is None:
                return link, arg_idx
            args.append(ids)
            return f"({outer_tbl}.parent_id = ANY(${arg_idx}) AND {link})", arg_idx + 1

        # `field` is the only user-controlled token interpolated into SQL below
        # (as a column name or JSON key); every value is parameterized. Validate
        # it up front so no branch can build SQL from an injectable name.
        # A mirror's own field (`@local.<name>`, 0022) is stored in local_metadata, or local_tags for
        # `@local.tags`, never in metadata; local_field checks the name after the prefix.
        try:
            local = local_field(field)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        if local is None:
            _validate_field_name(field)
        # The JSON column and key a metadata-like field is read from.
        json_source, json_key = ('local_metadata', local.name) if local is not None else ('metadata', field)

        # Derived fields: an embedding or hash (rows in their own tables, not metadata keys) or the
        # location, named `embeddings.<field>` / `hashes.<field>` / `location`. They answer presence
        # only (exists / not_exists), with the predicate /coverage and /missing count, so the grid's
        # coverage warning and its filter agree.
        # (`location` also takes the geo operators, handled further down.)
        derived = _derived_field(field)
        presence = op in ('exists', 'not_exists', 'is_not_null', 'is_null')
        if derived is not None and (presence or field != 'location'):
            if not presence:
                raise HTTPException(status_code=400, detail=f"'{field}' supports only exists / not_exists")
            present = op in ('exists', 'is_not_null')
            table, name = derived
            if table == 'location':
                return ("location IS NOT NULL" if present else "location IS NULL"), arg_idx
            # Qualified, never bare `id`: embeddings and sample_hashes have their own `id`, so a
            # bare one binds to the subquery's row and the anti-join matches every sample.
            id_col = (field_mapping or {}).get('id', 'samples.id')
            args.append(name)
            # A patch field ('clip_patch_4x4') is stored as one row per tile ('..._r0c0'), and
            # /coverage reports the tiles folded into that virtual name: fold them the same way.
            match = f"_d.field_name = ${arg_idx}"
            if table == 'embeddings' and re.search(r'_patch_\d+x\d+$', name):
                match = (
                    "_d.field_name ~ '_patch_[0-9]+x[0-9]+_r[0-9]+c[0-9]+$' "
                    f"AND regexp_replace(_d.field_name, '_r[0-9]+c[0-9]+$', '') = ${arg_idx}"
                )
            # A sample's vectors are in its dataset's partition (0021): naming the dataset keeps the lookup there.
            scope = ''
            if table == 'embeddings':
                scope = f"_d.dataset_id = {id_col.rsplit('.', 1)[0] + '.' if '.' in id_col else ''}dataset_id AND "
            lookup = f"EXISTS (SELECT 1 FROM {table} _d WHERE {scope}_d.sample_id = {id_col} AND {match})"
            return (lookup if present else f"NOT {lookup}"), arg_idx + 1

        # Per-object match: `field` is an array-of-objects label field and `value`
        # is a (possibly compound) filter node evaluated against each element.
        # Matches the sample if ANY element satisfies it — lets you combine
        # per-object conditions, e.g. label='car' AND confidence>0.9.
        if op in ('match_elements', 'elem_match'):
            inner, arg_idx = _parse_element_filter(val, args, arg_idx)
            # Guard the FROM: jsonb_array_elements() errors on non-array values
            # (JSON null/scalar), which would 500 the whole query for one bad row.
            return (
                f"EXISTS (SELECT 1 FROM jsonb_array_elements("
                f"CASE WHEN jsonb_typeof({json_source}->'{json_key}') = 'array' "
                f"THEN {json_source}->'{json_key}' ELSE '[]'::jsonb END) AS elem WHERE {inner})",
                arg_idx,
            )

        if local is not None:
            col = local.text
        # Handle mapped fields first
        elif field_mapping and field in field_mapping:
            col = field_mapping[field]
        # Handle special fields
        elif field == 'media_type':
            col = "media_type"
        elif field in (array_columns if array_columns is not None else _get_array_columns_sync()):
            col = field  # Native TEXT[] column
        elif field == 'filepath':
            col = "filepath"
        elif field == 'id':
            col = "id"
        elif field in _NATIVE_TIMESTAMPS:
            col = field
        elif (
            label_path(field, array_columns)
            and field.endswith('.label')
            and op in _LABEL_EQ_OPS
            and all(isinstance(item, str) for item in (val if isinstance(val, list) else [val]))
        ):
            # JSONB containment avoids expanding each object: metadata->'detections' @> [{"label": V}].
            # =/in -> contains ANY of the values; !=/not_in -> contains NONE of them
            # (NULL-aware via coalesce, so a sample with no detections joins the negative
            # set). Other operators (exists/is_null/comparison) are NOT matched here and
            # fall through to the generic dotted handler below.
            parent = field.rsplit('.', 1)[0]
            negate = op in _LABEL_NEG_OPS
            values = val if op in _LABEL_LIST_OPS else [val]
            if not isinstance(values, list):
                values = [values]
            if not values:
                return ("TRUE" if negate else "FALSE"), arg_idx
            or_clauses = []
            for v in values:
                args.append(v)
                or_clauses.append(
                    f"coalesce(metadata->'{parent}','[]'::jsonb) @> jsonb_build_array(jsonb_build_object('label', ${arg_idx}::text))",  # noqa: E501
                )
                arg_idx += 1
            inner = f"({' OR '.join(or_clauses)})"
            return (f"(NOT {inner})" if negate else inner), arg_idx

        elif field == 'segmentation.labels':
            # metadata->'segmentation'->'labels' is a string array ["a","b"]; `?` tests
            # array membership. =/in -> contains ANY; !=/not_in -> contains NONE (NULL-aware).
            negate = op in _LABEL_NEG_OPS
            values = val if op in _LABEL_LIST_OPS else [val]
            if not isinstance(values, list):
                values = [values]
            if not values:
                return ("TRUE" if negate else "FALSE"), arg_idx
            or_clauses = []
            for v in values:
                args.append(v)
                or_clauses.append(f"coalesce(metadata->'segmentation'->'labels','[]'::jsonb) ? ${arg_idx}::text")
                arg_idx += 1
            inner = f"({' OR '.join(or_clauses)})"
            return (f"(NOT {inner})" if negate else inner), arg_idx

        elif label_path(field, array_columns):
            # Dotted per-object convenience, e.g. "detections.confidence" > 0.9 or
            # "ground_truth.label" = "car": EXISTS over the array with a predicate on the
            # element key. Negative ops (!=/not_in) mean the sample contains NO matching
            # element -> NOT EXISTS(positive predicate), so a sample without the field is
            # included (NULL-aware), matching the @> handlers above.
            arr, sub = field.rsplit('.', 1)
            negate = op in _LABEL_NEG_OPS
            # Every spelling in _LABEL_NEG_OPS must map to its positive form. 'not in' (with the
            # space) is the one the FRONTEND actually emits for facet / multi-select excludes
            # (filter-utils.ts) and for histogram double-click-to-exclude; omitting it meant it
            # reached _element_predicate_sql unmapped and raised HTTP 400 on ground_truth.label /
            # predictions.label while detections.label (its own @> branch above) worked fine.
            pos_op = {'!=': '=', '<>': '=', 'not in': 'in', 'not_in': 'in', 'nin': 'in'}.get(op, op)
            inner, arg_idx = _element_predicate_sql(sub, pos_op, val, args, arg_idx)
            exists = (
                f"EXISTS (SELECT 1 FROM jsonb_array_elements("
                f"CASE WHEN jsonb_typeof(metadata->'{arr}') = 'array' "
                f"THEN metadata->'{arr}' ELSE '[]'::jsonb END) AS elem WHERE {inner})"
            )
            return (f"(NOT {exists})" if negate else exists), arg_idx

        else:
            # Metadata field
            col = f"metadata->>'{field}'"

        # Generate SQL based on operator
        array_cols = array_columns if array_columns is not None else _get_array_columns_sync()
        # A native array, and its column: `@local.tags` is local_tags.
        is_array = local.array if local is not None else field in array_cols
        arr = local.column if local is not None else field
        # A metadata field's numeric value, for the number comparisons below: a promoted numeric
        # field's bare ::float cast matches its index; any other field guards the cast.
        promoted_numeric = bool(numeric_indexed_fields and field in numeric_indexed_fields)
        numeric_col = f"({col})::float" if promoted_numeric else _numeric_metadata_sql(json_key, json_source)
        # Whether `=`/`in` take a numeric string as a number (_equality_number): on a promoted numeric field. The
        # dataset's array context knows those fields too, so a caller that passes no numeric_indexed_fields (hash
        # duplicates, sampling, bulk jobs, mirror tag forwarding, version queries) matches what the grid matches;
        # the argument only picks the index-matching cast.
        numeric_strings = promoted_numeric or field in getattr(array_cols, 'promoted_numeric', ())

        # A field declared datetime compares instants, through its index when it is promoted.
        if not is_array and field not in ('id', *_NATIVE_TIMESTAMPS, 'media_type', 'filepath'):
            if field in getattr(array_cols, 'datetime_fields', ()):
                indexed = field in getattr(array_cols, 'promoted_datetime', ())
                compiled = _datetime_leaf_sql(field, col, op, val, args, arg_idx, indexed)
                if compiled is not None:
                    return compiled
        # The samples' own timestamps, by the same rule.
        if not is_array and field in _NATIVE_TIMESTAMPS:
            compiled = _native_timestamp_sql(field, col, op, val, args, arg_idx)
            if compiled is not None:
                return compiled

        # A date / datetime given to day, minute or second precision means that whole unit
        #: `=` is the unit, `!=` its complement, `<=` ends where the next unit
        # starts and `>` starts there. (`>=` and `<` are already right at the unit's start.)
        if op in ('=', '!=', '<=', '>') and not is_array and field not in ('id', 'media_type', 'filepath'):
            window = _iso_unit_window(val)
            if window is not None:
                lo, hi = window
                bounds: list[Any] = [lo, hi]
                if field in _NATIVE_TIMESTAMPS:
                    bounds = [_utc_datetime(b) for b in bounds]
                if op == '<=':
                    args.append(bounds[1])
                    return f"{col} < ${arg_idx}", arg_idx + 1
                if op == '>':
                    args.append(bounds[1])
                    return f"{col} >= ${arg_idx}", arg_idx + 1
                args.extend(bounds)
                in_unit = f"({col} >= ${arg_idx} AND {col} < ${arg_idx + 1})"
                return (in_unit if op == '=' else f"({col} IS NULL OR NOT {in_unit})"), arg_idx + 2

        # created_at is timestamptz; coerce string values to datetime so asyncpg binds
        # them as timestamps (a raw string param errors: `timestamp = text`). Applies to
        # =, !=, range, in, not in below — all of which then bind the value natively.
        if field in _NATIVE_TIMESTAMPS and val is not None:

            def _to_ts(v):
                if isinstance(v, str):
                    try:
                        return _utc_datetime(v)
                    except ValueError:
                        # A malformed date bound as text errors at the DB (`timestamp =
                        # text`) → opaque 500. Reject it up front as a clean 400.
                        raise HTTPException(status_code=400, detail=f"Invalid {field} value: {v!r}")
                return v

            val = [_to_ts(v) for v in val] if isinstance(val, list) else _to_ts(val)

        if op == '=':
            if is_array:
                args.append(val)
                return f"${arg_idx} = ANY({arr})", arg_idx + 1
            elif field in ['media_type', 'filepath', 'id', *_NATIVE_TIMESTAMPS]:
                # id (int) and created_at (datetime, coerced above) bind natively; only
                # the text columns are stringified. Use the RESOLVED col (e.g. s.id) — a bare
                # `id`/`created_at` is ambiguous in the embeddings/search JOIN (both tables have
                # them) and 500s; every sibling op already uses {col}, only `=` regressed.
                args.append(val if field in ['id', *_NATIVE_TIMESTAMPS] else str(val))
                return f"{col} = ${arg_idx}", arg_idx + 1
            else:
                num = _equality_number(val, numeric_strings)
                if num is not None:
                    # A number compares as a number; a promoted field rides its float index.
                    args.append(num)
                    return f"{numeric_col} = ${arg_idx}", arg_idx + 1
                # Otherwise text extraction (->>), type-agnostic for strings: matches both
                # {"year": 2024} and {"year": "2024"} when val="2024".
                # _text_value renders booleans as 'true'/'false'.
                args.append(_text_value(val))
                return f"{col} = ${arg_idx}", arg_idx + 1

        elif op == '!=':
            if is_array:
                args.append(val)
                # Include rows with no/empty array value in the complement.
                return f"(NOT (${arg_idx} = ANY({arr})) OR {arr} IS NULL)", arg_idx + 1
            elif field in ['id', *_NATIVE_TIMESTAMPS]:
                # Native typed columns — bind as-is (int / coerced datetime). _text_value
                # would stringify and break `integer/timestamp IS DISTINCT FROM text`.
                args.append(val)
                return f"{col} IS DISTINCT FROM ${arg_idx}", arg_idx + 1
            elif field in ['media_type', 'filepath']:
                args.append(str(val))
                return f"{col} IS DISTINCT FROM ${arg_idx}", arg_idx + 1
            # IS DISTINCT FROM so rows missing the field (NULL) are part of the
            # complement; _text_value handles booleans.
            num = _equality_number(val, numeric_strings)
            if num is not None:  # numeric, as for `=`; non-numeric rows are NULL here -> included
                args.append(num)
                return f"{numeric_col} IS DISTINCT FROM ${arg_idx}", arg_idx + 1
            args.append(_text_value(val))
            return f"{col} IS DISTINCT FROM ${arg_idx}", arg_idx + 1

        elif op in ('>', '>=', '<', '<='):
            # An incomplete rule sends '' / None — e.g. a datetime-local picker with a date
            # but no time yet, or a numeric field left blank. Comparing against it is
            # meaningless and, for a non-datetime field, would bind '' to a ::float cast
            # (asyncpg DataError: "must be real number, not str"). Treat it as a no-op so an
            # unfinished filter never 500s the whole query.
            if val is None or (isinstance(val, str) and val.strip() == ''):
                return "TRUE", arg_idx
            if field not in ['id', *_NATIVE_TIMESTAMPS, 'media_type', 'filepath'] and not is_array:
                # Use the NUMERIC (::float) path only when the value is actually a number.
                # Anything else — an ISO date ('2025-01-01') or datetime ('2025-01-01T00:00'),
                # or arbitrary text — compares as TEXT: correct for datetime (ISO-8601 sorts
                # chronologically, whether date-only or full) and index-friendly (the promoted
                # metadata->>'field' btree; a ::timestamp cast can't use it and seq-scans
                # millions of rows). Critically, a non-numeric value is NEVER bound to a
                # ::float parameter — which is what 500'd ("must be real number, not str") on a
                # date value that lacked a 'T'. (date-only '<='/'<' are slightly loose at the
                # day boundary — a known limitation, best fixed by normalizing at ingest.)
                is_numeric = isinstance(val, (int, float)) and not isinstance(val, bool)
                if isinstance(val, str):
                    try:
                        float(val)
                        is_numeric = True
                    except ValueError:
                        is_numeric = False
                if is_numeric:
                    if numeric_indexed_fields and field in numeric_indexed_fields:
                        # Promoted numeric field: a float index exists and (since it built)
                        # every value is castable, so the bare cast matches the index — avoids
                        # a full sequential scan.
                        col = f"({col})::float"
                    else:
                        # Unpromoted/mixed field: guard the cast so a non-numeric row doesn't
                        # 500 the whole query.
                        col = _numeric_metadata_sql(json_key, json_source)
                    if isinstance(val, str):
                        # float() above only CHOSE the numeric path; bind the number itself.
                        # A str bound to a ::float parameter is the asyncpg DataError ("must
                        # be real number, not str") that 500'd the Add-Filter editor on any
                        # string-typed numeric field (the same
                        # class of bug as for date values).
                        val = float(val)

            args.append(val)
            return f"{col} {op} ${arg_idx}", arg_idx + 1

        elif op == 'contains':
            if is_array:
                args.append(val)
                return f"${arg_idx} = ANY({arr})", arg_idx + 1
            else:
                args.append(f"%{_escape_like(val)}%")
                return f"{col} ILIKE ${arg_idx} ESCAPE '\\'", arg_idx + 1

        elif op == 'in':
            if not isinstance(val, list):
                val = [val]

            if is_array:
                args.append(val)
                return f"{arr} && ${arg_idx}::text[]", arg_idx + 1
            elif field in ['media_type', 'filepath', 'id', *_NATIVE_TIMESTAMPS]:
                args.append(val)
                return f"{col} = ANY(${arg_idx})", arg_idx + 1
            else:
                if not val:
                    return "FALSE", arg_idx
                nums = [_equality_number(v, numeric_strings) for v in val]
                if all(n is not None for n in nums):  # numeric, as for `=`
                    args.append(nums)
                    return f"{numeric_col} = ANY(${arg_idx}::float8[])", arg_idx + 1
                # Use text extraction for type-agnostic matching (_text_value
                # normalizes booleans).
                str_vals = [_text_value(v) for v in val]
                args.append(str_vals)
                return f"{col} = ANY(${arg_idx}::text[])", arg_idx + 1

        elif op == 'not in':
            if not isinstance(val, list):
                val = [val]
            if is_array:
                args.append(val)
                # Include rows with no/empty array value in the complement.
                return f"(NOT ({arr} && ${arg_idx}::text[]) OR {arr} IS NULL)", arg_idx + 1
            elif field in ['media_type', 'filepath', 'id', *_NATIVE_TIMESTAMPS]:
                # Native columns — compare WITHOUT the ::text[] cast, mirroring `in`
                # above. id is integer / created_at is timestamp; a text[] cast here
                # produced `integer = text` and broke every `id not in [...]` (e.g.
                # excluding a selection). NULL-aware so missing rows join the complement.
                args.append(val)
                return f"({col} IS NULL OR NOT ({col} = ANY(${arg_idx})))", arg_idx + 1
            # NULL-aware + text-typed to mirror `in`: rows missing the field are
            # part of the complement; _text_value handles booleans.
            nums = [_equality_number(v, numeric_strings) for v in val]
            if val and all(n is not None for n in nums):  # numeric, as for `in`
                args.append(nums)
                return f"({numeric_col} IS NULL OR NOT ({numeric_col} = ANY(${arg_idx}::float8[])))", arg_idx + 1
            str_vals = [_text_value(v) for v in val]
            args.append(str_vals)
            return f"({col} IS NULL OR NOT ({col} = ANY(${arg_idx}::text[])))", arg_idx + 1

        elif op == 'is_null':
            if is_array:
                # Empty tags are stored as '{}', not NULL: `tags IS NULL` matched nothing, so the
                # "(no value)" filter on an array field was always empty. Same as not_exists.
                return f"array_length({arr}, 1) IS NULL", arg_idx
            # `col` resolves to the native column (id/created_at/filepath/media_type),
            # an array column, or metadata->>'field' — checking the right thing instead
            # of always probing metadata (which made id/created_at is_null match every row).
            return f"{col} IS NULL", arg_idx

        elif op == 'is_not_null':
            if is_array:
                return f"array_length({arr}, 1) IS NOT NULL", arg_idx
            return f"{col} IS NOT NULL", arg_idx

        elif op == 'exists':
            if is_array:
                # Native TEXT[] column (tags, ...): "exists" = has >= 1 element. A row with
                # tags=[] has the column but no tags; NULL/empty -> not exists. (metadata ? field
                # was wrong here — tags isn't a metadata key, so `exists` matched zero rows.)
                return f"({arr} IS NOT NULL AND array_length({arr}, 1) > 0)", arg_idx
            native = ('media_type', 'filepath', 'id', *_NATIVE_TIMESTAMPS)
            if (field_mapping and field in field_mapping) or field in native:
                # Native scalar column: existence = the column is set.
                return f"{col} IS NOT NULL", arg_idx
            # Metadata field: JSONB key-existence — matches when `field` is a top-level key
            # regardless of value type. Unlike `is_not_null` (via metadata->>'field', which
            # coerces to text and returns NULL for dict/array values), this detects complex
            # values like heatmap overlays `{"type": "heatmap", ...}`.
            args.append(json_key)
            return f"({json_source} ? ${arg_idx})", arg_idx + 1

        elif op == 'not_exists':
            if is_array:
                # Inverse of exists: no elements (NULL column or empty array).
                return f"array_length({arr}, 1) IS NULL", arg_idx
            native = ('media_type', 'filepath', 'id', *_NATIVE_TIMESTAMPS)
            if (field_mapping and field in field_mapping) or field in native:
                return f"{col} IS NULL", arg_idx
            args.append(json_key)
            return f"NOT ({json_source} ? ${arg_idx})", arg_idx + 1

        elif op in ('geo_polygon', 'geo_polygon_exclude'):
            # val is list of [lon, lat] points
            if not val or len(val) < 3:
                return "TRUE", arg_idx

            coords = ", ".join([f"{p[0]} {p[1]}" for p in val])
            if val[0] != val[-1]:
                coords += f", {val[0][0]} {val[0][1]}"

            wkt = f"POLYGON(({coords}))"
            args.append(wkt)
            # Include path uses ST_Within (spatial index). Exclude removes what is inside and keeps everything
            # else, samples with no location included (NOT ST_Within alone is NULL for them, which drops them).
            if op == 'geo_polygon_exclude':
                return (
                    f"(location IS NULL OR NOT ST_Within(location, ST_GeomFromText(${arg_idx}, 4326)))",
                    arg_idx + 1,
                )
            return f"ST_Within(location, ST_GeomFromText(${arg_idx}, 4326))", arg_idx + 1

        elif op == 'geo_near':
            # val = {"point": [lon, lat], "max_distance": meters}. Distance is on
            # the geography type so max_distance is in metres; uses the spatial index.
            point = val.get('point') if isinstance(val, dict) else None
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                return "TRUE", arg_idx
            # point is non-None only when val was a dict above.
            val = cast('Dict[str, Any]', val)
            # Coerce up front so a string coord/distance is a clean 400, not a 500.
            try:
                lon, lat = float(point[0]), float(point[1])
                raw_max_dist = val.get('max_distance')
                max_dist = float(raw_max_dist) if raw_max_dist is not None else 1_000_000.0
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="geo_near point/max_distance must be numbers")
            args.append(lon)
            args.append(lat)
            pt = f"ST_SetSRID(ST_MakePoint(${arg_idx}, ${arg_idx + 1}), 4326)::geography"
            arg_idx += 2
            args.append(max_dist)
            sql = f"(location IS NOT NULL AND ST_DWithin(location::geography, {pt}, ${arg_idx}))"
            return sql, arg_idx + 1

        elif op == 'in_selection':
            # value = a token returned by /embeddings/select-region, whose matching
            # sample_ids were materialized ONCE into region_selections. Scope via an
            # indexed EXISTS instead of re-scanning a polygon per page or shipping the
            # id set to the client. Virtual operator — `field` is ignored.
            if not isinstance(val, str) or not val:
                return "FALSE", arg_idx
            outer_id = field_mapping['id'] if (field_mapping and 'id' in field_mapping) else 'samples.id'
            args.append(val)
            return (
                f"EXISTS (SELECT 1 FROM region_selections rs WHERE rs.token = ${arg_idx} AND rs.sample_id = {outer_id} "
                "AND rs.created_at > statement_timestamp() - interval '2 hours')"
            ), arg_idx + 1

        elif op in ('embedding_polygon', 'embedding_polygon_exclude'):
            # value = polygon [[x, y], ...] in the field's reduced 2D coords. Matches
            # samples that have an embedding point for `field` inside the polygon —
            # applied SERVER-SIDE so an embeddings-lasso selection is never
            # materialized or capped into an id list (mirrors /select-region's
            # bbox-pre-filter + point-in-polygon test). `field` was validated above.
            if not isinstance(val, list) or len(val) < 3:
                return ("TRUE" if op == 'embedding_polygon_exclude' else "FALSE"), arg_idx
            try:
                pts = [(float(p[0]), float(p[1])) for p in val]
            except (TypeError, ValueError, IndexError):
                raise HTTPException(status_code=400, detail="embedding_polygon coords must be [x, y] numbers")
            if pts[0] != pts[-1]:
                pts.append(pts[0])  # close the ring
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            # bbox + WKT are built from float-validated coords → safe to inline.
            wkt = "POLYGON((" + ", ".join(f"{x} {y}" for x, y in pts) + "))"
            outer_id = field_mapping['id'] if (field_mapping and 'id' in field_mapping) else 'samples.id'
            # Patch-scale fields (e.g. clip_patch_4x4) store rows as field_patch_NxN_rRcC,
            # so match by prefix; whole-image fields match exactly.
            # Alias `ep` (not `e`) to avoid colliding with the embeddings endpoints'
            # own `e` alias when this filter is applied to their queries too.
            # The layout the lasso was drawn on (`layout_id`, from the scatter; `model_id` before 2.11) is the app's
            # to compare with the map's current one, to warn when the map was laid out again since: it never
            # changes what the polygon selects, which is always the map's current points.
            layout = node.get('layout_id')
            if layout is not None and (not isinstance(layout, str) or len(layout) > 64):
                raise HTTPException(status_code=400, detail="embedding_polygon layout_id must be a string or null")
            if re.search(r'_patch_\d+x\d+$', field):
                args.append(field + '_%')
                field_match = f"ep.field_name LIKE ${arg_idx}"
            else:
                args.append(field)
                field_match = f"ep.field_name = ${arg_idx}"
            arg_idx += 1
            bbox = (
                f"AND ep.x BETWEEN {min(xs)} AND {max(xs)} AND ep.y BETWEEN {min(ys)} AND {max(ys)} "  # bbox pre-filter (index)  # noqa: E501
                f"AND ST_Contains(ST_GeomFromText('{wkt}', 0), ST_SetSRID(ST_MakePoint(ep.x, ep.y), 0))"
            )
            # Match samples whose embedding point for `field` is inside the polygon. This
            # operator matches only rows that CARRY the field (objects for clip_obj, images
            # for clip_img/patch); cross-grain linking (object<->image) is applied uniformly
            # one level up by withGrain's parent_matches/has_matching_object wrappers, so it
            # is NOT duplicated here.
            inside = (
                f"EXISTS (SELECT 1 FROM embedding_coordinates ep WHERE ep.sample_id = {outer_id} "
                f"AND {field_match} AND ep.x IS NOT NULL {bbox})"
            )
            if op == 'embedding_polygon_exclude':
                # Exclude removes what is inside the polygon and keeps everything else, samples with no point on
                # this map included (as geo_polygon_exclude keeps samples with no location). The inside test keeps
                # its bbox index.
                return f"(NOT {inside})", arg_idx
            return inside, arg_idx

        else:
            # A field leaf with an unrecognized operator must not silently match
            # all rows — surface it as a 400.
            raise HTTPException(status_code=400, detail=f"Unknown filter operator: {op!r}")

    # Neither a logic/filters node nor a field leaf — malformed.
    raise HTTPException(status_code=400, detail="Invalid filter node: expected 'logic'/'filters' or 'field'")


def build_sample_query_parts(
    dataset_id: int,
    filters: Optional[str] = None,
    array_columns: Optional[set] = None,
    numeric_indexed_fields: Optional[set] = None,
) -> tuple[str, list[Any], int]:
    """
    Builds the WHERE clause and arguments for sample queries.
    Returns (where_clause, args, next_arg_idx)
    """
    conditions = ["dataset_id = $1"]
    args: list[Any] = [dataset_id]
    arg_idx = 2

    # Dynamic filters
    if filters:
        try:
            filter_data = json.loads(filters)

            # A filter tree only: the flat {field: value} form was removed in 2.0. An empty object is no filter.
            if filter_data != {}:
                tree = isinstance(filter_data, dict) and (
                    'logic' in filter_data
                    or 'filters' in filter_data
                    or ('field' in filter_data and 'operator' in filter_data)
                )
                if not tree:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            'filters must be a filter tree: {"field", "operator", "value"} or {"logic", "filters"}. '
                            'The flat {field: value} form was removed in 2.0'
                        ),
                    )
                sql, arg_idx = parse_filter_node(
                    filter_data,
                    args,
                    arg_idx,
                    None,
                    numeric_indexed_fields,
                    dataset_id,
                    array_columns=array_columns,
                )
                conditions.append(sql)
        except HTTPException:
            # A genuine filter validation error (unknown operator, malformed node) MUST surface as
            # a 400 — never fall through to a filter-stripped WHERE that silently returns the whole
            # over-broad dataset as if the filter had applied (wrong data as success).
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid filter: {e}")

    where_clause = " WHERE " + " AND ".join(conditions)
    return where_clause, args, arg_idx


class MapPointsRequest(BaseModel):
    filters: Optional[Dict[str, Any]] = None
    zoom: Optional[float] = None  # the map's zoom: server-side clusters below _MAP_POINT_ZOOM, points from it on
    bbox: Optional[List[float]] = None  # [west, south, east, north] viewport, longitudes unwrapped or not
    points: bool = False  # force ALL points (manual de-cluster)


@router.post("/datasets/{id}/map_points", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_dataset_map_points_post(id: int, request: MapPointsRequest):
    """The map's points and cluster bubbles for a zoom and viewport (docs/MAP_REQUIREMENTS.md), or with
    `points` every individual point up to the cap. One of `zoom` and `points` is required: an answer with every
    located sample at once has no bound."""
    if request.zoom is None and not request.points:
        raise HTTPException(status_code=422, detail='map_points needs the map\'s zoom, or "points": true')
    return await _get_dataset_map_points_impl(id, request.filters, request.zoom, request.bbox, request.points)


class MapExtentRequest(BaseModel):
    filters: Optional[Dict[str, Any]] = None


@router.post("/datasets/{id}/map_extent", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_dataset_map_extent(id: int, request: MapExtentRequest):
    """True bounding box (min/max lon/lat) of located samples for the current filters.
    Drives 'Zoom to Data' so it fits the ACTUAL data extent — coarse-cluster centroids
    collapse to a point for small/polygon selections and make fitBounds over-zoom.
    Fast: min/max over the stored lon/lat columns (covering index)."""
    conditions = ["dataset_id = $1", "location IS NOT NULL"]
    args: list = [id]
    if request.filters:
        nif = await get_promoted_numeric_fields(id)
        sql, _ = parse_filter_node(
            request.filters,
            args,
            2,
            None,
            nif,
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )
        conditions.append(sql)
    where = " AND ".join(conditions)
    row = await db.fetchrow(
        f"SELECT min(lon) AS w, max(lon) AS e, min(lat) AS s, max(lat) AS n, count(*) AS c FROM samples WHERE {where}",
        *args,
    )
    if not row or row["c"] == 0 or row["w"] is None:
        return {"count": 0}
    return {"w": row["w"], "s": row["s"], "e": row["e"], "n": row["n"], "count": row["c"]}


class SelectionTokenRequest(BaseModel):
    filters: Optional[Dict[str, Any]] = None


@router.post("/datasets/{id}/selection_token", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def create_selection_token(
    id: int,
    request: SelectionTokenRequest,
    user: dict = Depends(require_role("power_user")),
):
    """Materialize the sample ids matching `filters` into a region_selections token so a
    large 'select all' / filtered selection can be APPLIED and operated on via the indexed
    in_selection filter WITHOUT a giant id array crossing the wire or sitting in the
    browser. Returns {token, count}. Mirrors the embeddings select-region / version-diff
    token pattern; tokens TTL-prune after 2h."""
    conditions = ["dataset_id = $1"]
    args: list = [id]
    if request.filters:
        nif = await get_promoted_numeric_fields(id)
        sql, _ = parse_filter_node(
            request.filters,
            args,
            2,
            None,
            nif,
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )
        conditions.append(sql)
    where = " AND ".join(conditions)
    token = secrets.token_hex(16)  # hex literal -> injection-safe to inline
    # Materialize atomically; expiry belongs to the bounded, pin-aware cleanup job.
    assert db.pool is not None  # connected at startup (lifespan)
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.execute(
                f"INSERT INTO region_selections (token, sample_id) SELECT '{token}', id FROM samples WHERE {where}",
                *args,
            )
    count = int(status.split()[-1]) if status else 0
    return {"token": token, "count": count}


class OverlayConfigRequest(BaseModel):
    colormap: Optional[str] = None
    range: Optional[List[float]] = None  # [min, max]
    opacity: Optional[float] = None  # 0..1


@router.put("/datasets/{id}/fields/{field}/overlay_config", dependencies=[Depends(require_dataset_access)])
async def set_overlay_config(
    id: int,
    field: str,
    request: OverlayConfigRequest,
    user: dict = Depends(require_role("power_user")),
):
    """Save per-field overlay display config (heatmap colormap / range / opacity), applied
    to BOTH the modal canvas renderer and the grid composite. An empty body clears the
    config (rendering falls back to the heatmap's own metadata defaults)."""
    cfg: dict = {}
    if request.colormap:
        cfg["colormap"] = request.colormap
    if request.range and len(request.range) == 2:
        cfg["range"] = [float(request.range[0]), float(request.range[1])]
    if request.opacity is not None:
        cfg["opacity"] = max(0.0, min(1.0, float(request.opacity)))
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await lock_field_lifecycle(conn, id)
            status = await conn.execute(
                "UPDATE dataset_fields SET overlay_config = $3::jsonb WHERE dataset_id = $1 AND field_name = $2",
                id,
                field,
                cfg or None,
            )
            if status == 'UPDATE 0':
                raise HTTPException(status_code=404, detail='Field not found')
    return {"ok": True, "config": cfg or None}


_MAP_CLUSTER_MIN_POINTS = 100  # a grid cell with fewer than this renders as individual points, not a cluster (tunable)
_MAP_POINT_ZOOM = 12  # at >= this zoom ALL individual points show automatically (old clusterMaxZoom) — REQUIRED, see docs/MAP_REQUIREMENTS.md  # noqa: E501


def _cluster_cell_deg(zoom: float) -> float:
    """Grid cell size (degrees) for server-side map clustering at a given zoom —
    ~screen-appropriate spacing; smaller cell as you zoom in."""
    z = max(0.0, min(22.0, float(zoom)))
    return max(0.00001, 90.0 / (2**z))


def _map_bubble_radius_px(count: int) -> int:
    """A cluster bubble's radius on screen. The map panel draws the radius each bubble carries."""
    for floor, radius in ((100_000, 84), (10_000, 70), (1_000, 60)):
        if count >= floor:
            return radius
    return 52


def _map_lon_window(west: float, east: float) -> tuple[float, float]:
    """A viewport's longitudes as one window: west in [-180, 180), east above it, under 360 degrees on. mapbox-gl
    reports them unwrapped: a view across the antimeridian is 170 to 190, the same view on a repeated world copy
    530 to 550, and a view wider than the world spans 360 degrees or more (the whole world, -180 to 180). An east
    below the west, as in an RFC 7946 bbox, crosses the antimeridian too. An east past 180 means the window
    crosses it: it holds lon >= west and lon <= east - 360."""
    if east < west:
        east += 360.0
    if east - west >= 360.0:
        return -180.0, 180.0
    shift = math.floor((west + 180.0) / 360.0) * 360.0
    return west - shift, east - shift


def _map_screen_px(lon: float, lat: float, zoom: float) -> tuple[float, float]:
    """Where mapbox-gl draws (lon, lat) at `zoom`, in pixels of its Web Mercator world (512 px tiles)."""
    world = 512.0 * 2.0 ** max(0.0, min(22.0, float(zoom)))
    lat = max(-85.051129, min(85.051129, lat))
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return (lon + 180.0) / 360.0 * world, (1.0 - y / math.pi) / 2.0 * world


async def _get_dataset_map_points_impl(
    id: int,
    filter_obj: Optional[Dict[str, Any]],
    zoom: Optional[float] = None,
    bbox: Optional[List[float]] = None,
    points: bool = False,
):
    # Check dataset exists
    dataset_check = await db.fetchrow("SELECT id FROM datasets WHERE id = $1", id)
    if not dataset_check:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Base conditions WITHOUT bbox: dataset + location + filters. bbox is added per
    # query — the points-vs-clusters DECISION uses the VISIBLE viewport count, while
    # the points DATA is fetched for a slightly PADDED bbox (buffer for panning).
    conditions = ["dataset_id = $1", "location IS NOT NULL"]
    base_args: list = [id]
    if filter_obj:
        nif = await get_promoted_numeric_fields(id)
        sql, _ = parse_filter_node(
            filter_obj,
            base_args,
            2,
            None,
            nif,
            dataset_id=id,
            array_columns=await get_dataset_array_columns(id),
        )
        conditions.append(sql)
    base_where = " AND ".join(conditions)
    nb = len(base_args)  # next free $N param index is nb+1

    vis = None
    if bbox and len(bbox) == 4:
        try:
            vis = tuple(float(v) for v in bbox)  # (w, s, e, n)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="bbox must be [west, south, east, north] numbers")
        if not all(math.isfinite(v) for v in vis):
            raise HTTPException(status_code=400, detail="bbox must be [west, south, east, north] numbers")
        west, east = _map_lon_window(vis[0], vis[2])
        vis = (west, vis[1], east, vis[3])

    def _bbox_sql(i):  # bbox via the stored lon/lat columns (covering index); params w,s,e,n
        # Exact double-precision and faster than `location && ST_MakeEnvelope`, whose
        # float4 bbox over-includes a few points just outside the viewport. One way to
        # do it everywhere (matches _hybrid_items / map_extent). See docs/MAP_REQUIREMENTS.md.
        return f"lon >= ${i} AND lon <= ${i + 2} AND lat >= ${i + 1} AND lat <= ${i + 3}"

    pt_select = "SELECT id, lon, lat, heading, media_type FROM"

    # Per-query source (`FROM {source}`: base + optional bbox window) + args, fresh each call. A window across
    # the antimeridian reads both sides of it and puts the samples east of it 360 degrees further east, so its
    # answer is one stretch of map: the cell on the antimeridian groups the samples on both sides, and a
    # bubble's position and extent lie in the window. mapbox-gl draws every item on every world copy.
    def _wa(padded=False):
        where, a = base_where, list(base_args)
        if not vis:
            return f"samples WHERE {where}", a
        w, s, e, n = vis
        if padded:
            px, py = (e - w) * 0.25, (n - s) * 0.25
            w, e = _map_lon_window(w - px, e + px)
            s, n = max(-85.0, s - py), min(85.0, n + py)
        if e <= 180.0:
            return f"samples WHERE {where} AND {_bbox_sql(nb + 1)}", a + [w, s, e, n]
        # Across the antimeridian: each side is a range scan of the covering index, the east side's longitudes 360
        # degrees on. One `lon >= w OR lon <= e - 360` is not a range: it read every index entry of the dataset, or
        # went to the heap (#11).
        columns = "lat, heading, media_type"
        return (
            f"(SELECT id, lon, {columns} FROM samples WHERE {where} AND {_bbox_sql(nb + 1)} UNION ALL "
            f"SELECT id, lon + 360, {columns} FROM samples WHERE {where} AND {_bbox_sql(nb + 5)}) AS across",
            a + [w, s, 180.0, n, -180.0, s, e - 360.0, n],
        )

    POINT_CAP = 65000

    async def _hybrid_items(source, a, cell, zoom):
        """The ONE per-cell rule shared by every clustered view: a grid cell with
        >= _MAP_CLUSTER_MIN_POINTS samples becomes a single cluster bubble; a sparser
        cell yields its INDIVIDUAL points. Returns (items, loose_point_count). Because
        the floor is applied everywhere, a cluster bubble ALWAYS represents
        >= _MAP_CLUSTER_MIN_POINTS samples — no stray "1"/"12" bubbles.

        A bubble also takes in the loose points it is drawn over. It sits at its points'
        average position and is drawn wider than half a cell (52-84 px; a cell is 128 px),
        so it covered points of neighbouring sparse cells, which looked as if they belonged
        to it. A loose point within a bubble's radius on screen at this zoom counts in the
        nearest such bubble instead of being drawn. The radius reaches the 8 neighbouring
        cells at most, so only points of a sparse cell next to a dense one are measured.

        Each bubble also carries the extent (w, s, e, n) of every sample it counts, the ones it
        took in included, so a click can zoom to fit them and break the bubble apart."""
        a = list(a) + [cell, _MAP_CLUSTER_MIN_POINTS]
        cell_p, minp_p = len(a) - 1, len(a)
        # Group on the stored lon/lat columns with pure float arithmetic:
        # round(coord/cell) is the same grid node ST_SnapToGrid snaps to, so output matches
        # the old geometry form. This lets the whole-world view run as an INDEX-ONLY scan
        # over the covering index (idx_samples_lonlat_cover) instead of reading the whole
        # heap + per-row PostGIS. One GROUP BY (no window): dense cells (cc>=minp) emit one
        # bubble; sparse cells (cc<minp) carry their points in arrays we unnest into point
        # rows. lon/lat are trigger-maintained from location — never stale. See MAP_REQUIREMENTS.md.
        # Planned for every run's own cell size and bbox (#11): from a prepared statement's 6th
        # run on a connection PostgreSQL may switch to a generic plan, which for this query was
        # about 9 times slower (14 s instead of 1.5 s on 3.75M samples at zoom 4.5).
        async with db.pool.acquire() as connection, connection.transaction():
            await connection.execute("SET LOCAL plan_cache_mode = force_custom_plan")
            rows = await connection.fetch(
                f"""
                WITH g AS (
                    SELECT round(lon / ${cell_p}::float8) AS gx, round(lat / ${cell_p}::float8) AS gy,
                           count(*) AS cc, AVG(lon) AS lon, AVG(lat) AS lat,
                           MIN(lon) AS w, MIN(lat) AS s, MAX(lon) AS e, MAX(lat) AS n,
                           array_agg(id) AS ids, array_agg(lon) AS xs, array_agg(lat) AS ys,
                           array_agg(heading::double precision) AS hs
                    FROM {source}
                    GROUP BY round(lon / ${cell_p}::float8), round(lat / ${cell_p}::float8)
                )
                SELECT gx, gy, NULL::int AS id, lon, lat, cc AS count, NULL::double precision AS heading,
                       w, s, e, n
                FROM g WHERE cc >= ${minp_p}
                UNION ALL
                SELECT gx, gy, u.id, u.x AS lon, u.y AS lat, 1 AS count, u.h AS heading,
                       NULL, NULL, NULL, NULL
                FROM g, unnest(g.ids, g.xs, g.ys, g.hs) AS u(id, x, y, h)
                WHERE g.cc < ${minp_p}
            """,
                *a,
            )
        bubbles = {
            (r['gx'], r['gy']): {
                "lon": r['lon'],
                "lat": r['lat'],
                "count": r['count'],
                "radius": _map_bubble_radius_px(r['count']),
                "w": r['w'],
                "s": r['s'],
                "e": r['e'],
                "n": r['n'],
            }
            for r in rows
            if r['id'] is None
        }
        centres = {cell_key: _map_screen_px(b['lon'], b['lat'], zoom) for cell_key, b in bubbles.items()}
        steps = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
        reachable = {(gx + dx, gy + dy) for gx, gy in bubbles for dx, dy in steps}
        points = []
        for r in rows:
            if r['id'] is None:
                continue
            if (r['gx'], r['gy']) in reachable:
                x, y = _map_screen_px(r['lon'], r['lat'], zoom)
                nearest, nearest_d2 = None, 0.0
                for dx, dy in steps:
                    cell_key = (r['gx'] + dx, r['gy'] + dy)
                    if cell_key not in bubbles:
                        continue
                    cx, cy = centres[cell_key]
                    d2 = (x - cx) ** 2 + (y - cy) ** 2
                    if d2 <= bubbles[cell_key]['radius'] ** 2 and (nearest is None or d2 < nearest_d2):
                        nearest, nearest_d2 = cell_key, d2
                if nearest is not None:
                    bubble = bubbles[nearest]
                    bubble['count'] += 1
                    bubble['w'], bubble['e'] = min(bubble['w'], r['lon']), max(bubble['e'], r['lon'])
                    bubble['s'], bubble['n'] = min(bubble['s'], r['lat']), max(bubble['n'], r['lat'])
                    continue
            points.append({"id": r['id'], "lon": r['lon'], "lat": r['lat'], "heading": r['heading']})
        return [*bubbles.values(), *points], len(points)

    # Individual points show when the user de-clusters manually (points=True) OR
    # AUTOMATICALLY once zoomed past _MAP_POINT_ZOOM — the old clusterMaxZoom.
    # This auto-points-at-zoom behavior is a hard REQUIREMENT (docs/MAP_REQUIREMENTS.md
    # #1) and is enforced here, server-side, so it can't be lost in a frontend refactor.
    # Padded for panning. If the viewport holds more points than we can ship, do NOT
    # truncate — fall through to the clustered hybrid (every point still represented as
    # a >=100 bubble) and flag capped so the UI can prompt "zoom in".
    auto_points = zoom is not None and zoom >= _MAP_POINT_ZOOM
    if points or auto_points:
        source, a = _wa(padded=True)
        a.append(POINT_CAP + 1)
        rows = await db.fetch(f"{pt_select} {source} LIMIT ${len(a)}", *a)
        if len(rows) <= POINT_CAP:
            return {"mode": "points", "items": [dict(r) for r in rows], "capped": False}
        # else: too many to show individually -> fall through to the hybrid (capped)

    # Clustered view (default below POINT_ZOOM, and the over-cap fallback above) uses
    # the single per-cell rule — sparse cells show points at any zoom. If there are too
    # many loose points to ship, COARSEN the grid (same rule; bigger cells absorb more
    # points into bubbles) until under the cap, NEVER by dropping the floor — so tiny
    # "1"/"12" bubbles can't appear in any path.
    if zoom is not None:
        source, base = _wa()
        cell = _cluster_cell_deg(zoom)
        for _ in range(6):
            items, loose = await _hybrid_items(source, base, cell, zoom)
            if loose <= POINT_CAP:
                break
            cell *= 2.0
        return {"mode": "hybrid", "items": items, "capped": points or auto_points}

    # Neither a zoom nor points: the endpoint refuses this (no unbounded answer). A points request with no zoom
    # that is over the cap has no zoom to cluster at either.
    raise HTTPException(status_code=422, detail='Too many points to return at once: zoom in, or send the zoom')


class SamplesRequest(BaseModel):
    limit: int = Field(100, ge=0)  # 0 = count only
    offset: int = Field(0, ge=0)  # a negative OFFSET reached Postgres and 500'd
    after_id: Optional[int] = None  # Keyset pagination: fetch rows with id > after_id (ignores offset)
    filters: Optional[Dict[str, Any]] = None
    sort_by: Optional[str] = None
    sort_order: Optional[str] = "asc"
    order_by_ids: Optional[List[int]] = None  # rank order (similarity/patch search): ORDER BY position in this list
    # Cross-grain rank: when True, order_by_ids holds IMAGE ids and OBJECT rows are ranked by their
    # parent image's position in that list (an image/patch search ranks images; objects inherit it).
    order_by_parent: bool = False
    fields: Optional[str] = None  # Comma-separated list of fields to return
    slim: bool = False  # True = only return requested fields, skip Pydantic validation
    skip_count: bool = False  # True = skip COUNT(*) query for faster pagination


class LookupRequest(BaseModel):
    field: str  # metadata field name to match on
    values: List[Any]  # values to look up


@router.post("/datasets/{id}/samples/lookup", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def lookup_samples(id: int, request: LookupRequest):
    """Fast bulk lookup: given a metadata field and values, return matching sample IDs.

    Much faster than /samples/query for bulk ID resolution because it skips
    counting, pagination, and full sample serialization.
    """
    if not request.field.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid field name")

    if request.field in ('id',):
        rows = await db.fetch(
            "SELECT id FROM samples WHERE dataset_id = $1 AND id = ANY($2)",
            id,
            request.values,
        )
        return {r['id']: r['id'] for r in rows}

    if request.field in ('filepath',):
        rows = await db.fetch(
            "SELECT id, filepath FROM samples WHERE dataset_id = $1 AND filepath = ANY($2)",
            id,
            request.values,
        )
        return {r['filepath']: r['id'] for r in rows}

    # Metadata field — one batched unnest join instead of an N+1 prepared-statement loop (which
    # was up to len(values) serialized round-trips on a single pooled connection, and a seq scan
    # PER value for an unpromoted field like source_id). Same contract: one id per matched value
    # (MIN for determinism), values with no match are absent. Uses the per-field expression index
    # when the field is promoted; a single scan otherwise (vs one per value).
    values = [str(v) for v in request.values]
    rows = await db.fetch(
        f"""
        SELECT v.val AS val, MIN(s.id) AS id
        FROM unnest($2::text[]) AS v(val)
        JOIN samples s ON s.dataset_id = $1 AND s.metadata->>'{request.field}' = v.val
        GROUP BY v.val
        """,
        id,
        values,
    )
    return {r['val']: r['id'] for r in rows}


def _grid_usage(filters: Any, sort_by: Optional[str]) -> Dict[str, List[str]]:
    """What a grid request uses, for the index advisor: never raises (field_usage.filter_fields)."""
    sort = [sort_by] if isinstance(sort_by, str) and sort_by and '.' not in sort_by else []
    return {'filter': field_usage.filter_fields(filters), 'sort': sort}


def _sample_dataset_condition(dataset_id: int) -> str:
    # Keep the bind for callers' parameter numbering, but expose the dataset to generic
    # plans too: promoted expression indexes have a literal WHERE dataset_id = N predicate.
    return f"dataset_id = $1 AND dataset_id = {int(dataset_id)}"


def _filter_first_id_order(has_filter: bool, total: int, dataset_rows: Optional[int]) -> bool:
    """Flip the default `ORDER BY id` to `ORDER BY id + 0` (identical ordering, but not
    pkey-navigable) when an active filter matches a minority of the dataset.

    For `ORDER BY id LIMIT n` the planner walks the pkey, evaluating the filter row by
    row until n matches; when the matches sit late in id order (e.g. a recent datetime
    range on a streamed dataset) that can scan many non-matching rows. Resolving the
    filter through its own index first and top-N sorting avoids that walk. For broad
    filters (>= half the dataset) matches are dense in id order and the pkey walk wins,
    so those keep the plain sort.
    """
    return has_filter and total > 0 and dataset_rows is not None and total * 2 < dataset_rows


def _plan_indexes_samples(plan: Dict[str, Any]) -> bool:
    """The skip-count estimate must have an indexed path to the matching samples."""
    if plan.get('Relation Name') == 'samples' and plan['Node Type'] in (
        'Index Scan',
        'Index Only Scan',
        'Bitmap Heap Scan',
    ):
        condition = plan.get('Index Cond', plan.get('Recheck Cond', ''))
        # Restricting the dataset alone says nothing about how its filter is resolved.
        return bool(condition) and not re.fullmatch(r'\(*dataset_id = (?:\d+|\$\d+)\)*', condition)
    return any(_plan_indexes_samples(child) for child in plan.get('Plans', []))


@router.post("/datasets/{id}/samples/query", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def list_samples_post(id: int, request: SamplesRequest):
    """
    POST version of list_samples to handle large filter payloads. When the filters hold a lasso drawn on an earlier
    layout of its map, the answer names those maps (`stale_lassos`): it may select different samples now. The SDK
    warns; the app's chip already does.
    """
    # Index advisor telemetry (backend/field_usage.py): an in-memory count, never a failure or a wait.
    with field_usage.measure(id, _grid_usage(request.filters, request.sort_by)):
        answer = await _list_samples_post(id, request)
    stale = await stale_lassos.stale_maps(db, id, request.filters)
    if stale and isinstance(answer, dict):
        answer['stale_lassos'] = stale
    return answer


def _slim_metadata_select(keys_param: int, source: str = 'metadata') -> str:
    """Select clause for the slim path's requested metadata keys, bound as one text[] parameter. `source` is the
    JSON column, and the name it comes back under: a mirror's own fields are in local_metadata."""
    return (
        f"COALESCE((SELECT jsonb_object_agg(key, {source}->key) FROM unnest(${keys_param}::text[]) AS key "
        f"WHERE {source} ? key), '{{}}'::jsonb) AS {source}"
    )


async def _list_samples_post(id: int, request: SamplesRequest):
    # Check dataset exists
    dataset = await db.fetchrow("SELECT id FROM datasets WHERE id = $1", id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Build query parts
    conditions = ["dataset_id = $1"]
    args: list[Any] = [id]
    arg_idx = 2

    if request.filters:
        nif = await get_promoted_numeric_fields(id)
        sql, arg_idx = parse_filter_node(
            request.filters,
            args,
            arg_idx,
            None,
            nif,
            id,
            array_columns=await get_dataset_array_columns(id),
        )
        conditions.append(sql)

    where_clause = " WHERE " + " AND ".join(conditions)

    # Build ORDER BY clause
    order_by = "ORDER BY id"  # Default sort
    direction, field_type_row = 'ASC', None
    if request.sort_by:
        # Sanitize field name (a mirror's own `@local.` field is checked by local_field)
        if not (_queryable_field(request.sort_by) or request.sort_by.replace("_", "").replace("-", "").isalnum()):
            raise HTTPException(status_code=400, detail=f"Invalid sort field name: {request.sort_by!r}")

        # Determine sort direction
        direction = "DESC" if request.sort_order and request.sort_order.lower() == "desc" else "ASC"

        # Metadata fields need their dataset_fields type for the cast; native columns don't.
        field_type_row = None
        if request.sort_by not in _NATIVE_SORT_COLUMNS:
            field_type_row = await db.fetchrow(
                "SELECT field_type, is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
                id,
                request.sort_by,
            )
        # One builder for every sort path so the order is TOTAL (`, id` tiebreaker) —
        # OFFSET paging over ties duplicated/skipped rows when each branch built its own.
        order_by = _grid_order_by(request.sort_by, direction, field_type_row)

    # Keyset pagination: id > after_id (uses primary key index, instant at any position).
    # Skipped when ranking by an id list (rank order is incompatible with id keyset).
    if request.after_id is not None and not request.order_by_ids:
        conditions.append(f"id > ${arg_idx}")
        args.append(request.after_id)
        arg_idx += 1
        where_clause = " WHERE " + " AND ".join(conditions)
        order_by = "ORDER BY id"  # keyset requires consistent ordering

    # Expose partial indexes for counts and filter-first pages. Keep the original
    # predicate for ordered pages: a generic filter-index plan can sort a broad set
    # after several deep-offset requests, instead of walking the ID index.
    indexed_where_clause = " WHERE " + " AND ".join([_sample_dataset_condition(id), *conditions[1:]])

    # Count query (skip when SDK says so)
    total = -1
    if not request.skip_count:
        count_query = f"SELECT count(*) FROM samples {indexed_where_clause}"
        # asyncpg's fetchval uses a one-row portal, which suppresses parallel
        # execution. This aggregate returns one row even with an unlimited portal.
        total = (await db.fetch(count_query, *args))[0][0]

    # See _filter_first_id_order: don't let the planner walk the pkey through a
    # selective filter's non-matches. sample_count from the ingest-maintained stats
    # cache approximates dataset size (MAX = a fully-populated field's row count).
    if (
        request.filters
        and has_active_filters(request.filters)
        and not request.sort_by
        and request.after_id is None
        and not request.order_by_ids
        and request.limit > 0
    ):
        ds_rows = await db.fetchval(
            "SELECT MAX(sample_count) FROM dataset_field_stats WHERE dataset_id = $1",
            id,
        )
        matching_rows = total
        if total == -1 and ds_rows and ds_rows > 100_000:
            # Planning only: never count the whole filtered set just to choose a
            # first-page strategy. Small datasets don't need the extra probes.
            # EXPLAIN uses the current parameter values.
            raw_plan = await db.fetchval(f"EXPLAIN (FORMAT JSON) SELECT id FROM samples {indexed_where_clause}", *args)
            plan = json.loads(raw_plan)[0]['Plan']
            # An unindexed predicate's default low estimate is not evidence for
            # sorting a full dataset scan (the color-filter first page is already fast).
            if _plan_indexes_samples(plan):
                matching_rows = plan['Plan Rows']
        if _filter_first_id_order(True, matching_rows, ds_rows):
            # Keep the ID walk only when this bounded prefix can fill the requested
            # page. A handful of early matches does not establish density further on.
            page_rows = request.offset + request.limit
            early_page = await _safe_filter_query(
                db.fetchval(
                    f"SELECT count(*) >= ${arg_idx} FROM (SELECT 1 FROM (SELECT * FROM samples "
                    f"WHERE {_sample_dataset_condition(id)} ORDER BY id LIMIT 4096) samples "
                    f"{where_clause} LIMIT ${arg_idx}) matches",
                    *args,
                    page_rows,
                ),
                budget_s=_LIGHTNING_PROBE_BUDGET_S,
            )
            if not early_page:
                order_by = "ORDER BY id + 0"
                where_clause = indexed_where_clause

    # A sort one ORDER BY can't answer fast (_sorted_page_ids) picks its page's ids first; the page's rows then
    # come back in that order. After the count, which doesn't take the ids.
    if request.sort_by and request.after_id is None and not request.order_by_ids:
        page_ids = await _sorted_page_ids(
            request.sort_by,
            direction,
            field_type_row,
            where_clause,
            args,
            request.limit,
            request.offset,
        )
        if page_ids is not None:
            args.append(page_ids)
            where_clause += f" AND id = ANY(${arg_idx}::int[])"
            order_by = f"ORDER BY array_position(${arg_idx}::int[], id)"
            arg_idx += 1
            request = request.model_copy(update={'offset': 0})

    # Ranked ordering (similarity / patch search): order rows by their position in the
    # provided id list. Appended AFTER the count query so the count arg list excludes it.
    if request.order_by_ids:
        args.append([int(x) for x in request.order_by_ids])
        if request.order_by_parent:
            # Object rows ranked by their parent image's position in the (image) rank list, so an
            # image/patch search orders the cross-grain object grid too. Objects of the same image
            # share a rank (tie) -> a secondary `id` makes the order TOTAL so offset paging can't
            # duplicate/skip a tied row. An object with no image (parent_id NULL, 0029) sorts last.
            order_by = f"ORDER BY array_position(${arg_idx}::bigint[], parent_id::bigint), id"
        else:
            order_by = f"ORDER BY array_position(${arg_idx}::bigint[], id)"
        arg_idx += 1

    # Parse requested fields
    selected_fields = None
    if request.fields:
        selected_fields = [f.strip() for f in request.fields.split(',')]

    # SLIM PATH: SDK bulk operations — only fetch requested columns
    if request.slim and selected_fields:
        select_parts = ['id']
        metadata_subfields = []
        local_subfields: list = []
        array_cols = await get_dataset_array_columns(id)

        for field in selected_fields:
            if field == 'id':
                pass  # already included
            elif field in ('filepath', 'media_type', 'tags', 'created_at', 'last_modified_at') or field in array_cols:
                select_parts.append(field)
            elif field == 'metadata':
                select_parts.append('metadata')
            elif field.startswith('metadata.'):
                subfield = field[len('metadata.') :]
                if subfield in array_cols:
                    # Native array column requested with metadata. prefix — select directly
                    select_parts.append(subfield)
                elif subfield:
                    metadata_subfields.append(subfield)
            elif field.startswith(LOCAL_PREFIX):
                # A mirror's own field: `@local.tags` is the local_tags column, any other a key of local_metadata.
                try:
                    local = local_field(field)
                except ValueError as error:
                    raise HTTPException(status_code=400, detail=str(error)) from None
                if local is not None and local.array:
                    select_parts.append('local_tags')
                elif local is not None:
                    local_subfields.append(local.name)

        # Build minimal metadata extraction. The keys are one bound text[] parameter, so any key
        # works (including ones with ':' like 'percent_CROP:CORN', which used to be silently
        # dropped), and `metadata->key` keeps each value's JSON type where `->>` turned numbers,
        # booleans and lists into strings. Keys a sample doesn't have are left out, as in a full row.
        select_args: list = []
        if metadata_subfields and 'metadata' not in select_parts:
            select_parts.append(_slim_metadata_select(arg_idx))
            select_args = [metadata_subfields]
        if local_subfields:
            select_parts.append(_slim_metadata_select(arg_idx + len(select_args), 'local_metadata'))
            select_args.append(sorted(set(local_subfields)))
        limit_idx = arg_idx + len(select_args)

        select_clause = ', '.join(select_parts)
        if request.after_id is not None:
            query = f"SELECT {select_clause} FROM samples {where_clause} {order_by} LIMIT ${limit_idx}"
            query_params = args + select_args + [request.limit]
        else:
            query = (
                f"SELECT {select_clause} FROM samples {where_clause} {order_by} "
                f"LIMIT ${limit_idx} OFFSET ${limit_idx + 1}"
            )
            query_params = args + select_args + [request.limit, request.offset]
        try:
            rows = await db.fetch(query, *query_params)
        except asyncpg.ObjectNotInPrerequisiteStateError:
            raise  # 409 (app handler): a deleting dataset, a concept being rescored, or a lasso on a re-fitted map
        except Exception as e:
            # Log the SQL/error server-side; never return them to the client
            # (schema/traceback disclosure, and it amplifies injection probing).
            print(f"[slim query error] {e}\nSQL: {query}")
            raise HTTPException(status_code=500, detail="Internal query error")
        items = [dict(row) for row in rows]

        # Parse metadata if it's jsonb (from jsonb_build_object or full metadata)
        for item in items:
            for column in ('metadata', 'local_metadata'):
                if isinstance(item.get(column), str):
                    item[column] = json.loads(item[column])

        return {"items": items, "total": total}

    # NORMAL PATH: UI — full sample data including dynamic array fields
    extra_array_cols = sorted(await get_dataset_array_columns(id) - {'tags'})
    extra_array_select = (', ' + ', '.join(extra_array_cols)) if extra_array_cols else ''
    # A mirror's rows also carry its own fields (`@local.`); any other dataset's rows are as they were.
    if await db.fetchval('SELECT 1 FROM mirrors WHERE dataset_id = $1', id):
        extra_array_select += ', local_metadata, local_tags'
    select_clause = f"""id, filepath, media_type, tags, metadata,
           ST_X(location::geometry) as lon, ST_Y(location::geometry) as lat, heading,
           created_at, last_modified_at{extra_array_select}"""

    if request.after_id is not None:
        query = f"SELECT {select_clause} FROM samples {where_clause} {order_by} LIMIT ${arg_idx}"
        query_args = args + [request.limit]
    else:
        query = f"SELECT {select_clause} FROM samples {where_clause} {order_by} LIMIT ${arg_idx} OFFSET ${arg_idx + 1}"
        query_args = args + [request.limit, request.offset]

    if order_by == "ORDER BY id + 0":
        # Sort only IDs; fetch wide metadata after the page is known.
        query = (
            f"WITH page AS MATERIALIZED (SELECT id FROM samples {where_clause} "
            f"{order_by} LIMIT ${arg_idx} OFFSET ${arg_idx + 1}) "
            f"SELECT {select_clause} FROM samples JOIN page USING (id) ORDER BY id"
        )

    try:
        rows = await db.fetch(query, *query_args)
    except asyncpg.ObjectNotInPrerequisiteStateError:
        raise  # 409 (app handler): a deleting dataset, a concept being rescored, or a lasso on a re-fitted map
    except Exception:
        import traceback

        tb = traceback.format_exc()
        print(tb)
        raise HTTPException(status_code=500, detail="Internal query error")

    results = []

    for row in rows:
        data = dict(row)

        # Handle metadata if present
        if 'metadata' in data and data['metadata']:
            data['metadata'] = json.loads(data['metadata']) if isinstance(data['metadata'], str) else data['metadata']
        if isinstance(data.get('local_metadata'), str):
            data['local_metadata'] = json.loads(data['local_metadata'])

        # Construct location object from lat/lon/heading if present
        if 'lon' in data and 'lat' in data:
            if data['lon'] is not None and data['lat'] is not None:
                data['location'] = {"type": "Point", "coordinates": [data['lon'], data['lat']]}
                if 'heading' in data and data['heading'] is not None:
                    data['location']['heading'] = data['heading']
            else:
                data['location'] = None

            # Remove raw lat/lon/heading from top level
            del data['lon']
            del data['lat']
            if 'heading' in data:
                del data['heading']

        # filepath stays as-is (s3://... or local absolute path). The frontend
        # uses /api/samples/{id}/media for image rendering and shows filepath
        # verbatim in the metadata panel.

        results.append(data)

    return {"items": results, "total": total}


@router.get("/datasets/{id}/samples", response_model=SampleListResponse, dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def list_samples(
    id: int,
    request: Request,
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
    filters: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = Query(None, pattern="^(asc|desc)$"),
    fields: Optional[str] = None,  # Comma-separated list of fields to return
):
    removed = [name for name in _REMOVED_SAMPLE_FILTERS if name in request.query_params]
    if removed:
        # Refused, not ignored: an unknown query parameter would otherwise answer with the unfiltered dataset.
        field, operator = _REMOVED_SAMPLE_FILTERS[removed[0]]
        value: Any = request.query_params[removed[0]]
        if field == 'brightness':  # the old parameter was a number, so the filter compares a number
            try:
                value = float(value)
            except ValueError:
                pass  # the hint shows what was sent; the 422 stands either way
        raise HTTPException(
            status_code=422,
            detail=(
                f"{', '.join(removed)}: removed in 2.0. Send filters as a filter tree, for example "
                f'filters={{"field": "{field}", "operator": "{operator}", "value": {json.dumps(value)}}}'
            ),
        )
    with field_usage.measure(id, _grid_usage(filters, sort_by)):
        return await _list_samples(id, limit, offset, filters, sort_by, sort_order, fields)


# Query parameters that filtered fixed metadata keys before 2.0, and the filter that replaces each.
_REMOVED_SAMPLE_FILTERS = {
    'file_date': ('file_date', '='),
    'date_min': ('file_date', '>='),
    'date_max': ('file_date', '<='),
    'brightness_min': ('brightness', '>='),
    'brightness_max': ('brightness', '<='),
}


async def _list_samples(
    id: int,
    limit: int,
    offset: int,
    filters: Optional[str],
    sort_by: Optional[str],
    sort_order: Optional[str],
    fields: Optional[str],
):
    where_clause, args, arg_idx = build_sample_query_parts(
        id,
        filters,
        array_columns=await get_dataset_array_columns(id),
        numeric_indexed_fields=await get_promoted_numeric_fields(id) if filters else None,
    )

    # Build ORDER BY clause
    order_by = "ORDER BY id"  # Default sort
    direction, field_type_row = 'ASC', None
    if sort_by:
        print(f"DEBUG: sort_by={sort_by}, sort_order={sort_order}")
        # Sanitize field name (a mirror's own `@local.` field is checked by local_field)
        if not (_queryable_field(sort_by) or sort_by.replace("_", "").replace("-", "").isalnum()):
            raise HTTPException(status_code=400, detail=f"Invalid sort field name: {sort_by!r}")

        # Determine sort direction
        direction = "DESC" if sort_order and sort_order.lower() == "desc" else "ASC"

        # Metadata fields need their dataset_fields type for the cast; native columns don't.
        field_type_row = None
        if sort_by not in _NATIVE_SORT_COLUMNS:
            field_type_row = await db.fetchrow(
                "SELECT field_type, is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
                id,
                sort_by,
            )
            print(f"DEBUG: field_type_row for {sort_by}: {field_type_row}")
        # One builder for every sort path so the order is TOTAL (`, id` tiebreaker) —
        # OFFSET paging over ties duplicated/skipped rows when each branch built its own.
        order_by = _grid_order_by(sort_by, direction, field_type_row)

    print(f"DEBUG: Final order_by clause: {order_by}")

    # Count query
    count_query = f"SELECT count(*) FROM samples {where_clause}"
    total = await db.fetchval(count_query, *args)

    # A sort one ORDER BY can't answer fast picks its page's ids first (_sorted_page_ids).
    if sort_by:
        page_ids = await _sorted_page_ids(sort_by, direction, field_type_row, where_clause, args, limit, offset)
        if page_ids is not None:
            args.append(page_ids)
            where_clause += f" AND id = ANY(${arg_idx}::int[])"
            order_by = f"ORDER BY array_position(${arg_idx}::int[], id)"
            arg_idx += 1
            offset = 0

    # Parse requested fields if provided
    selected_fields = None
    if fields:
        selected_fields = [f.strip() for f in fields.split(',')]

    # Build SELECT clause based on requested fields
    if selected_fields:
        # Map field names to SQL columns
        select_parts = []
        need_location = False
        need_heading = False

        for field in selected_fields:
            if field == 'id':
                select_parts.append('id')
            elif field == 'filepath':
                select_parts.append('filepath')
            elif field == 'media_type':
                select_parts.append('media_type')
            elif field == 'tags':
                select_parts.append('tags')
            elif field == 'created_at':
                select_parts.append('created_at')
            elif field == 'last_modified_at':
                select_parts.append('last_modified_at')
            elif field == 'location':
                need_location = True
            elif field == 'heading':
                need_heading = True
            elif field.startswith('metadata'):
                # Handle metadata fields - we'll return full metadata for simplicity
                if 'metadata' not in select_parts:
                    select_parts.append('metadata')

        # Always include required fields for Sample model validation
        for required in ['id', 'filepath', 'media_type', 'tags', 'metadata']:
            if required not in select_parts:
                select_parts.append(required)

        # Add location components if needed
        if need_location or need_heading:
            select_parts.append('ST_X(location::geometry) as lon')
            select_parts.append('ST_Y(location::geometry) as lat')
            if need_heading:
                select_parts.append('heading')

        select_clause = ', '.join(select_parts)
    else:
        # Default: return all fields
        # Include dynamic array columns
        _get_arr = await get_dataset_array_columns(id) - {'tags'}
        _extra_arr = (', ' + ', '.join(sorted(_get_arr))) if _get_arr else ''
        select_clause = f"""id, filepath, media_type, tags, metadata,
               ST_X(location::geometry) as lon, ST_Y(location::geometry) as lat, heading,
               created_at, last_modified_at{_extra_arr}"""

    # Data query with field selection
    query = f"""
        SELECT {select_clause}
        FROM samples
        {where_clause}
        {order_by} LIMIT ${arg_idx} OFFSET ${arg_idx + 1}
    """

    query_args = args + [limit, offset]

    try:
        rows = await db.fetch(query, *query_args)
    except asyncpg.ObjectNotInPrerequisiteStateError:
        raise  # 409 (app handler): a deleting dataset, a concept being rescored, or a lasso on a re-fitted map
    except Exception:
        import traceback

        tb = traceback.format_exc()
        print(tb)
        raise HTTPException(status_code=500, detail="Internal query error")

    results = []

    for row in rows:
        data = dict(row)

        # Handle metadata if present - should already be a dict from JSONB codec
        # (No longer need json.loads workaround since root cause of double-serialization is fixed)

        # Construct location object from lat/lon/heading if present
        if 'lon' in data and 'lat' in data:
            if data['lon'] is not None and data['lat'] is not None:
                data['location'] = {"type": "Point", "coordinates": [data['lon'], data['lat']]}
                if 'heading' in data and data['heading'] is not None:
                    data['location']['heading'] = data['heading']
            else:
                data['location'] = None

            # Remove raw lat/lon/heading from top level
            del data['lon']
            del data['lat']
            if 'heading' in data:
                del data['heading']

        # filepath stays raw — see /api/samples/{id}/media for image fetching.

        results.append(data)

    return {"items": results, "total": total}


@router.get("/datasets/{id}/histogram", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_histogram(
    id: int,
    field: str,
    bins: Optional[int] = Query(None, ge=1, le=200),
    filters: Optional[str] = None,
    top: int = Query(50, ge=0, le=10000),
    as_number: bool = False,
):
    """
    Return exact histogram buckets for a field. If `bins` is provided, numeric
    fields are bucketed; otherwise values are grouped categorically.

    A metadata field declared as text (registry type `string`) is grouped categorically by
    default, even when its values look numeric, without the numeric-detection scan. An explicit
    `bins`, or `as_number=true`, asks for the numeric interpretation instead: the detection and
    bins this endpoint used before that default (a field whose present values are mostly not
    numbers still falls back to categories).
    """
    with field_usage.measure(id, {'histogram': [field]}):
        return await _histogram(id, field, bins, filters, top, as_number)


class HistogramRequest(BaseModel):
    """The histogram query parameters, with the filter as a JSON object instead of a URL parameter."""

    field: str
    bins: Optional[int] = Field(None, ge=1, le=200)
    filters: Optional[Dict[str, Any]] = None
    top: int = Field(50, ge=0, le=10000)
    as_number: bool = False

    def filters_json(self) -> Optional[str]:
        return json.dumps(self.filters) if self.filters else None


@router.post("/datasets/{id}/histogram", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_histogram_post(id: int, request: HistogramRequest):
    """POST version of get_histogram, for a filter too large for a URL: a selection of a few thousand sample
    ids already passes the server's 64 KB request-line limit (HTTP 400)."""
    with field_usage.measure(id, {'histogram': [request.field]}):
        return await _histogram(
            id,
            request.field,
            request.bins,
            request.filters_json(),
            request.top,
            request.as_number,
        )


async def _histogram(
    id: int,
    field: str,
    bins: Optional[int],
    filters: Optional[str],
    top: int,
    as_number: bool,
    sample_ids: Optional[List[int]] = None,
):
    """The histogram of the filtered set, or only of the rows in `sample_ids` (the preview)."""
    # `field` is interpolated into the value expression below; reject unsafe names. A mirror's own field
    # (`@local.<name>`) is read from local_metadata (or the local_tags array), never metadata.
    try:
        local = local_field(field)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    if local is None:
        _validate_field_name(field)
    json_source, json_key = ('local_metadata', local.name) if local is not None else ('metadata', field)
    meta_text, meta_json = f"{json_source}->>'{json_key}'", f"{json_source}->'{json_key}'"

    # Detect datetime metadata fields the way the rest of the app does — from the
    # field_type the stats refresh assigns in dataset_field_stats — so `datetime` /
    # `local_datetime` (which don't match the created_at/_at/_date name patterns below)
    # get a time-binned histogram instead of a per-timestamp categorical grouping
    # (thousands of near-unique buckets, which reads as "broken").
    stats_type = await db.fetchval(
        "SELECT field_type FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
        id,
        field,
    )
    field_is_datetime = stats_type == 'datetime'
    numbers_only = stats_type == 'number'
    # Declared text is categorical unless the caller asks for numbers.
    declared_text = stats_type == 'string' and not as_number and not bins
    if declared_text and field not in _NATIVE_TIMESTAMPS and (field.endswith('_at') or field.endswith('_date')):
        # A time-named field typed text (e.g. its first write mixed ISO times with '') keeps main's
        # time bins when its values are mostly ISO dates; non-date text stays categorical. A bounded
        # sample through the metadata index decides, not a scan of the field.
        iso, present = await db.fetchrow(
            "SELECT count(*) FILTER (WHERE v ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'), count(*) FROM ("
            f"  SELECT {json_source}->>$2 AS v FROM samples WHERE dataset_id = $1 AND {json_source} ? $2"
            f"  AND {json_source}->>$2 <> '' LIMIT 1000) sampled",
            id,
            json_key,
        )
        if present and iso * 2 >= present:
            declared_text = False

    where_clause, args, arg_idx = build_sample_query_parts(
        id,
        filters=filters,
        array_columns=await get_dataset_array_columns(id),
        numeric_indexed_fields=await get_promoted_numeric_fields(id) if filters else None,
    )
    # Expose the dataset literal to generic plans using promoted partial indexes.
    where_clause += f" AND {_sample_dataset_condition(id)}"
    if sample_ids is not None:
        where_clause += f" AND id = ANY(${arg_idx}::int[])"
        args.append(sample_ids)
    bin_count = bins

    # Disable binning for obvious non-numeric fields
    if bin_count and field in ['media_type', 'tags', 'filepath']:
        bin_count = None

    # Determine the base value expression
    if field == 'media_type':
        value_expr = "media_type"
        field_kind = "categorical"
    elif local is not None and local.array:
        value_expr = f"unnest({local.column})"
        field_kind = "categorical"
    elif local is None and field in await get_dataset_array_columns(id):
        value_expr = f"unnest({field})"
        field_kind = "categorical"
    elif field == 'filepath':
        value_expr = "filepath"
        field_kind = "categorical"
    elif field == 'id':
        value_expr = "id"
        field_kind = "numeric"
    elif declared_text and field not in _NATIVE_TIMESTAMPS:
        value_expr = meta_text
        field_kind = "categorical"
    elif field in _NATIVE_TIMESTAMPS or field.endswith('_at') or field.endswith('_date') or field_is_datetime:
        value_expr = field if field in _NATIVE_TIMESTAMPS else meta_text
        field_kind = "timestamp"
    else:
        # Default to metadata field (string)
        value_expr = meta_text
        field_kind = "numeric_or_categorical"

    # Timestamp-aware binning: convert to epoch seconds so width_bucket works
    is_timestamp = field_kind == "timestamp"

    # Numeric histogram with bins (auto-bins if requested or if field is numeric/timestamp)
    if bin_count or field_kind in ("numeric", "timestamp", "numeric_or_categorical"):
        # Extract a metadata value (one de-TOAST of `metadata`) once per row in each pass. A
        # function scan keeps the planner from copying the extraction into every reference below
        # (as an inlined expression would) while staying parallel safe (unlike an OFFSET 0
        # subquery). `_value` and `_micros` cannot collide with a samples column. Both passes stream, so no
        # extracted value is spooled to memory or temp files.
        source = f"samples {where_clause}"
        text_expr, json_expr = value_expr, meta_json
        if value_expr == meta_text:
            extract, text_expr = value_expr, "_value"
            if numbers_only and field_kind == "numeric_or_categorical":
                # Keep the JSON value for the number-type guard.
                extract, text_expr, json_expr = json_expr, "(_value #>> '{}')", "_value"
            lateral = f"CROSS JOIN LATERAL unnest(ARRAY[{extract}]) AS _extracted(_value)"
            if is_timestamp:
                # A datetime's instant, by the rule in backend/datetimes.py: text that isn't a datetime counts as
                # missing, and no offset is UTC whatever the session's time zone. It costs a parse, and the passes
                # below read it two or three times, so it is computed once per row the same way.
                micros = f"(EXTRACT(EPOCH FROM {instant_sql(text_expr)}) * 1000000)::int8"
                lateral += f" CROSS JOIN LATERAL unnest(ARRAY[{micros}]) AS _instant(_micros)"
            source = f"samples {lateral} {where_clause}"
        # Build a safe numeric expression; any non-numeric values become NULL
        if is_timestamp:
            # Whole microseconds since 1970, so a bar's edges are exact instants; `infinity` counts as missing.
            column = f"CASE WHEN isfinite({field}) THEN (EXTRACT(EPOCH FROM {field}) * 1000000)::int8 END"
            numeric_expr = column if field in _NATIVE_TIMESTAMPS else "_micros"
        elif field_kind == "numeric":
            numeric_expr = "id::float" if field == 'id' else f"({value_expr})::float"
        elif numbers_only:
            numeric_expr = f"CASE WHEN jsonb_typeof({json_expr}) = 'number' THEN ({text_expr})::float ELSE NULL END"
        else:
            numeric_expr = f"""
                CASE
                    WHEN ({text_expr}) ~ '^[-+]?\\d+(\\.\\d+)?([eE][+-]?\\d+)?$'
                         AND pg_input_is_valid(({text_expr}), 'double precision')
                        THEN NULLIF(({text_expr}), '')::float
                    ELSE NULL
                END
            """

        # `raw` (non-NULL when the field is present) only where it decides anything.
        check_mostly_numbers = field_kind == "numeric_or_categorical" and not numbers_only
        raw_col = f", ({text_expr}) AS raw" if check_mostly_numbers else ", NULL AS raw"
        data_cte = f"WITH data AS (SELECT {numeric_expr} AS val{raw_col} FROM {source})"
        # Regression: one scan for total + bounds (was two separate full scans), with
        # JIT disabled — the per-row regex CASE that guards non-numeric values makes
        # JIT compilation adds overhead before the result can be returned.
        assert db.pool is not None  # connected at startup (lifespan)
        async with db.pool.acquire() as _conn:
            async with _conn.transaction():
                await _conn.execute("SET LOCAL jit = off")
                # fetch, not fetchrow: a one-row portal disables parallel execution of this scan.
                _stats = (
                    await _conn.fetch(
                        f"{data_cte} SELECT COUNT(*) AS total_rows, MIN(val) AS min, "
                        f"MAX(val) AS max, COUNT(val) AS total, COUNT(raw) AS present FROM data",
                        *args,
                    )
                )[0]
        total_rows = _stats['total_rows'] or 0
        minmax = _stats  # Record exposes 'min', 'max', 'total'

        # If nothing numeric, fall back to categorical. A single distinct value falls back too —
        # except for id/timestamp fields, which have no categorical path: they get one bucket
        # below (they used to return no buckets with `missing = total`, which the panel showed
        # as "No data for this field" though every sample had the value).
        degenerate = bool(minmax) and minmax['min'] is not None and minmax['min'] == minmax['max']
        # A metadata field is binned numerically only when most of its present values ARE numbers:
        # hex ids like "0347677e05719710" pass the number check now and then, and binned those
        # into a meaningless numeric range — they get the categorical histogram. A numeric
        # field with a stray "N/A" stays numeric.
        mostly_text = check_mostly_numbers and (minmax['total'] or 0) * 2 < (minmax['present'] or 0)
        if (
            not minmax
            or mostly_text
            or minmax['min'] is None
            or minmax['max'] is None
            or (degenerate and field_kind not in ("numeric", "timestamp"))
            or (minmax['total'] or 0) == 0
        ):
            # If this was forced numeric (id/timestamp), return empty; otherwise try categorical
            if field_kind in ("numeric", "timestamp"):
                return {"field": field, "total": total_rows, "missing": total_rows, "buckets": []}
            # Categorical fallback handled later
        elif is_timestamp:
            return await _instant_histogram(field, data_cte, args, minmax, total_rows, bin_count, degenerate)
        else:
            min_val = float(minmax['min'])
            max_val = float(minmax['max'])
            total = int(minmax['total'] or 0)
            missing = max(total_rows - total, 0)

            # width_bucket excludes the upper bound; nudge max slightly so max values are included
            span = max_val - min_val
            epsilon = max(abs(max_val), abs(min_val), 1.0) * 1e-12 if span == 0 else span * 1e-9
            adjusted_max = max_val + epsilon

            # Choose bin count if not provided: round up sqrt(n) capped between 8 and 30 to avoid overdraw
            bin_count = 1 if degenerate else (bin_count or max(8, min(30, int(math.sqrt(total)) or 8)))

            # Inline min/max literals to avoid indeterminate parameter typing from Postgres
            bucket_query = f"""
                {data_cte}
                SELECT bucket, COUNT(*) as count
                FROM (
                    SELECT width_bucket(
                        val::double precision, {min_val}::double precision,
                        {adjusted_max}::double precision, {bin_count}
                    ) AS bucket
                    FROM data
                    WHERE val IS NOT NULL
                ) sub
                WHERE bucket BETWEEN 1 AND {bin_count}
                GROUP BY bucket
                ORDER BY bucket
            """
            rows = await db.fetch(bucket_query, *args)

            # Fill all bins (include zero-count buckets)
            count_map = {row['bucket']: row['count'] for row in rows}

            # Compute bucket edges for labels
            edges = [min_val + (adjusted_max - min_val) * i / bin_count for i in range(bin_count + 1)]
            buckets = []
            for i in range(1, bin_count + 1):
                start = edges[i - 1]
                end = edges[i]
                count = count_map.get(i, 0)
                is_last = i == bin_count
                # Use the true max for the upper label to avoid showing the epsilon bump
                if is_last:
                    end = max_val
                buckets.append(
                    {
                        "key": i,
                        "count": count,
                        "label": f"{start:.2f} - {end:.2f}",
                        "range": {"min": start, "max": end},
                    },
                )

            return {"field": field, "total": total_rows, "missing": missing, "buckets": buckets, "bins": bin_count}

    # Categorical histogram (default or fallback). ONE scan: GROUP BY value once (materialized),
    # then derive total / missing / distinct / top-N from that grouped result — the numeric path
    # was already collapsed to a single pass; this does the same for categorical (was
    # four separate scans of the filtered set). The cap is NOT silent: distinct_total + other_count
    # let the UI show "N of M", and top=0 returns ALL categories (view-everything).
    # Ties are ordered by value, so `top` cuts the same categories every time. fetchrow keeps this
    # GROUP BY serial: parallel workers would each build their own hash table of the values.
    source_query = f"SELECT {value_expr} AS value FROM samples {where_clause}"
    limit_clause = "" if top <= 0 else f"LIMIT {top}"
    row = await db.fetchrow(
        f"""
        WITH grouped AS MATERIALIZED (
            SELECT value, COUNT(*) AS cnt FROM ({source_query}) sub GROUP BY value
        )
        SELECT
            (SELECT COALESCE(SUM(cnt), 0) FROM grouped) AS total_rows,
            (SELECT COALESCE(SUM(cnt) FILTER (WHERE value IS NULL), 0) FROM grouped) AS missing_count,
            (SELECT COUNT(*) FILTER (WHERE value IS NOT NULL) FROM grouped) AS distinct_total,
            (SELECT jsonb_agg(jsonb_build_object('value', value, 'cnt', cnt) ORDER BY cnt DESC, value)
             FROM (SELECT value, cnt FROM grouped WHERE value IS NOT NULL ORDER BY cnt DESC, value {limit_clause}) t
            ) AS buckets
        """,
        *args,
    )
    total_rows = int(row['total_rows'] or 0)
    missing_count = int(row['missing_count'] or 0)
    distinct_total = int(row['distinct_total'] or 0)
    raw_buckets = row['buckets']
    if isinstance(raw_buckets, str):
        import json as _json

        raw_buckets = _json.loads(raw_buckets)
    buckets = []
    shown = 0
    for b in raw_buckets or []:
        buckets.append(
            {
                "key": b['value'],
                "count": b['cnt'],
                "label": str(b['value']) if b['value'] is not None else "null",
            },
        )
        shown += b['cnt']

    # Samples in categories beyond the returned top-N — surfaced, never silently dropped.
    other_count = max(0, (total_rows - missing_count) - shown)
    return {
        "field": field,
        "total": total_rows,
        "missing": missing_count,
        "buckets": buckets,
        "distinct_total": distinct_total,
        "other_count": other_count,
    }


# Approximate histogram preview. The panel shows this labelled preview while the exact
# whole-dataset request runs, then replaces it. A sample can miss rare categories.
# "Broad" = the filtered set is estimated above _LIGHTNING_FACET_MAX_ROWS, the sidebar's threshold
# for a set too large to recompute interactively. Selective sets get no preview: their exact
# histogram is the fast case, and a sample of them would be too small anyway.
_HISTOGRAM_PREVIEW_DRAWS = 20_000
# Fewer matching rows among the draws (a filter matching under ~5 % of the dataset's id range) is
# too little evidence even for a rough shape; the panel waits for the exact result instead.
_HISTOGRAM_PREVIEW_MIN_MATCHES = 1_000
# The preview's work is bounded by the draws, not the dataset. This guards a filter that is slow
# per row (or a loaded database); after it the panel simply waits for the exact result.
_HISTOGRAM_PREVIEW_BUDGET_S = 2.0


@router.get("/datasets/{id}/histogram/preview", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_histogram_preview(
    id: int,
    field: str,
    bins: Optional[int] = Query(None, ge=1, le=200),
    filters: Optional[str] = None,
    top: int = Query(50, ge=0, le=10000),
    as_number: bool = False,
):
    """APPROXIMATE histogram of a uniform random sample of a broad filtered set.

    Same parameters and computation as /histogram, restricted to the sampled rows that match the
    filter. Counts are sample counts (never scaled up), bin edges come from the sample, and
    categories missing from the sample may exist. `approximate` and `preview` say so. Buckets are
    returned only when `preview.status` is `ok`; otherwise (`not_broad`, `insufficient_sample`,
    `timeout`, `failed`) the caller waits for /histogram, which is always the exact answer.
    """
    return await _histogram_preview(id, field, bins, filters, top, as_number)


@router.post("/datasets/{id}/histogram/preview", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_histogram_preview_post(id: int, request: HistogramRequest):
    """POST version of get_histogram_preview, for a filter too large for a URL (see get_histogram_post)."""
    return await _histogram_preview(
        id,
        request.field,
        request.bins,
        request.filters_json(),
        request.top,
        request.as_number,
    )


async def _histogram_preview(
    id: int,
    field: str,
    bins: Optional[int],
    filters: Optional[str],
    top: int,
    as_number: bool,
) -> Dict[str, Any]:
    if not _queryable_field(field):  # a safe name, or a mirror's own `@local.` field (_histogram reads either)
        _validate_field_name(field)

    def unavailable(status: str, **detail: Any) -> Dict[str, Any]:
        return {"field": field, "approximate": True, "preview": {"status": status, **detail}}

    extent = await db.fetchrow("SELECT min(id) AS lo, max(id) AS hi FROM samples WHERE dataset_id = $1", id)
    lo, hi = extent['lo'], extent['hi']
    if lo is None or hi - lo + 1 <= _LIGHTNING_FACET_MAX_ROWS:
        return unavailable("not_broad")
    # Distinct ids drawn uniformly from the dataset's id range. Gaps and other datasets' rows are
    # rejected, so the rows found are a uniform sample of the dataset. The seed depends only on the
    # extent: a refresh shows the same preview, and new rows draw a new sample.
    span = hi - lo + 1
    draws = random.Random(f"histogram-preview:{id}:{lo}:{hi}").sample(
        range(lo, hi + 1),
        min(_HISTOGRAM_PREVIEW_DRAWS, span),
    )
    where_clause, args, arg_idx = build_sample_query_parts(
        id,
        filters=filters,
        array_columns=await get_dataset_array_columns(id),
        numeric_indexed_fields=await get_promoted_numeric_fields(id) if filters else None,
    )
    # Look the drawn rows up by primary key first, then filter them. The OFFSET 0 fence keeps the
    # planner from scanning a broad filter index and testing every matching row against the draws. Aliased
    # `samples`, so the filter's column and `samples.id` references bind to the drawn rows.
    drawn = f"(SELECT * FROM samples WHERE {_sample_dataset_condition(id)} AND id = ANY(${arg_idx}::int[]) OFFSET 0)"
    args.append(draws)

    async def measure() -> Dict[str, Any]:
        matched = [row['id'] for row in await db.fetch(f"SELECT id FROM {drawn} AS samples {where_clause}", *args)]
        detail = {
            "method": "uniform_id_sample",
            "id_range": [lo, hi],
            "draws": len(draws),
            "matched": len(matched),
            "estimated_matching_rows": len(matched) * span // len(draws),
        }
        if detail["estimated_matching_rows"] <= _LIGHTNING_FACET_MAX_ROWS:
            return unavailable("not_broad", **detail)
        if len(matched) < _HISTOGRAM_PREVIEW_MIN_MATCHES:
            return unavailable("insufficient_sample", **detail)
        # The matched rows already passed the filter, so the histogram only restricts to them.
        result = await _histogram(id, field, bins, None, top, as_number, sample_ids=matched)
        return {**result, "approximate": True, "preview": {"status": "ok", **detail}}

    try:
        return await asyncio.wait_for(measure(), timeout=_HISTOGRAM_PREVIEW_BUDGET_S)
    except asyncio.TimeoutError:
        return unavailable("timeout")
    except asyncpg.PostgresError:
        # The exact request reports the error; a preview only ever adds to it.
        return unavailable("failed")


@router.get("/datasets/{id}/stats", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_sample_stats(id: int, sample_ids: Optional[str] = None):
    """
    Get aggregate statistics for a subset of samples.

    Args:
        id: Dataset ID
        sample_ids: Comma-separated list of sample IDs (optional, defaults to all samples)

    Returns:
        Dictionary with min/max for numeric fields, unique values for categorical
    """
    # Parse sample IDs if provided
    if sample_ids:
        try:
            ids_list = [int(sid.strip()) for sid in sample_ids.split(',')]
            id_condition = "AND id = ANY($2)"
            args = [id, ids_list]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid sample_ids format")
    else:
        id_condition = ""
        args = [id]

    field_rows = await db.fetch(
        "SELECT field_name, field_type FROM dataset_fields WHERE dataset_id = $1 AND origin = 'metadata'",
        id,
    )
    field_param = f"${len(args) + 1}"
    stats = {}
    for definition in field_rows:
        field, field_type = definition['field_name'], definition['field_type']
        if field_type == 'number':
            row = await db.fetchrow(
                f"""SELECT MIN((metadata->>{field_param})::float) AS min_val,
                           MAX((metadata->>{field_param})::float) AS max_val
                    FROM samples WHERE dataset_id = $1 {id_condition}
                    AND jsonb_typeof(metadata->{field_param}) = 'number'""",
                *args,
                field,
            )
            stats[field] = {"type": field_type, "min": row['min_val'], "max": row['max_val']}
        elif field_type in ('string', 'datetime', 'list'):
            value = f"metadata->>{field_param}"
            condition = f"metadata ? {field_param}"
            if field_type == 'list':
                value = f"jsonb_array_elements_text(metadata->{field_param})"
                condition = f"jsonb_typeof(metadata->{field_param}) = 'array'"
            if field_type == 'datetime':
                # The earliest and latest instants, written as the statistics write them; each row's text read once.
                rows_sql = f"samples WHERE dataset_id = $1 {id_condition} AND {condition}"
                row = await db.fetchrow(
                    f"SELECT {UTC_TEXT_SQL.format('MIN(_instant)')} AS min_val, "
                    f"{UTC_TEXT_SQL.format('MAX(_instant)')} AS max_val FROM {instants_from(value, rows_sql)}",
                    *args,
                    field,
                )
                stats[field] = {"type": field_type, "min": row['min_val'], "max": row['max_val']}
            else:
                rows = await db.fetch(
                    f"SELECT DISTINCT {value} AS value FROM samples "
                    f"WHERE dataset_id = $1 {id_condition} AND {condition} ORDER BY value LIMIT 101",
                    *args,
                    field,
                )
                stats[field] = {
                    "type": field_type,
                    "values": [row['value'] for row in rows[:100]],
                    "values_complete": len(rows) <= 100,
                }
    return stats


def _point_location(loc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A sample location as a GeoJSON Point — two finite numbers on the globe — or None.

    Malformed coordinates reached the INSERT and 500'd (`[1, 2, 3]` → ValueError, `["1", "2"]` →
    DataError); `[999, 999]` was stored and pushed the dataset's map extent off the globe, so "Zoom
    to Data" could not work; a location without `coordinates` was silently dropped on ingest and
    became (0, 0) on PATCH.
    """
    if loc is None:
        return None
    coords = loc.get("coordinates") if isinstance(loc, dict) else None
    if (
        not isinstance(coords, (list, tuple))
        or len(coords) != 2
        or not all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) for c in coords)
    ):
        raise ValueError('location must be {"type": "Point", "coordinates": [lon, lat]} with two finite numbers')
    lon, lat = float(coords[0]), float(coords[1])
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(f"location [{lon}, {lat}] is off the globe: lon must be in [-180, 180], lat in [-90, 90]")
    return {"type": "Point", "coordinates": [lon, lat]}


def _finite_vector(name: str, vector: List[float]) -> List[float]:
    if not vector:
        raise ValueError(f"embedding '{name}' must not be empty")
    # NaN / inf would reach pgvector and 500 ("NaN not allowed in vector").
    if not all(math.isfinite(x) for x in vector):
        raise ValueError(f"embedding '{name}' contains NaN or infinity")
    return vector


# Metadata keys that name a sample COLUMN: stored in metadata they were registered as fields and
# merged into the column's stats (`id: -1`, `dataset_id: 7`, `created_at: 1999-…` became sidebar
# fields beside the real ones). The Sample's own attributes carry these.
# (`heading`, `tags`, `media_type` are NOT refused: importers may write them into metadata.)
_RESERVED_METADATA_KEYS = frozenset({'id', 'dataset_id', 'filepath', 'created_at', 'last_modified_at'})


class SampleCreate(BaseModel):
    filepath: str
    media_type: Optional[str] = None
    # Honoured on insert (now() when absent; zone-less = UTC). It was silently dropped, so
    # re-ingesting or syncing between instances reset every sample's created_at to the sync time,
    # although the SDK's Sample.to_dict() sends it. An update keeps the original.
    created_at: Optional[datetime.datetime] = None
    tags: List[str] = []

    @field_validator('location')
    @classmethod
    def _location(cls, loc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return _point_location(loc)

    @field_validator('created_at')
    @classmethod
    def _created_at(cls, v: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
        return v if v is None or v.tzinfo is not None else v.replace(tzinfo=datetime.timezone.utc)

    @field_validator('metadata')
    @classmethod
    def _metadata(cls, md: Dict[str, Any]) -> Dict[str, Any]:
        reserved = sorted(_RESERVED_METADATA_KEYS & md.keys())
        if reserved:
            raise ValueError(f"metadata key(s) {reserved} name sample columns — set them on the sample itself")
        return validate_metadata(md)

    @field_validator('embedding')
    @classmethod
    def _embedding(cls, v: Any) -> None:
        if v is not None:
            raise ValueError("'embedding' was removed in 2.0: send 'embeddings': {field name: vector}")
        return None

    @field_validator('embeddings')
    @classmethod
    def _embeddings(cls, v: Optional[Dict[str, List[float]]]) -> Optional[Dict[str, List[float]]]:
        return None if v is None else {k: _finite_vector(k, vec) for k, vec in v.items()}

    @field_validator('tags')
    @classmethod
    def _tags(cls, tags: List[str]) -> List[str]:
        # Stored verbatim before: ["a", "a", "", "  ", "b"] became {a,a,"","  ",b} and the blank
        # tags showed up in the sidebar.
        return clean_tags(tags)

    metadata: Dict[str, Any] = {}
    location: Optional[Dict[str, Any]] = None
    heading: Optional[float] = None
    # Removed in 2.0: a single vector was stored as 'clip' whatever model made it. Refused with the fix, so an
    # old client's vectors aren't silently dropped.
    embedding: Any = None
    embeddings: Optional[Dict[str, List[float]]] = None  # Named embeddings: {"clip": [...], "dinov2": [...]}
    hashes: Optional[Dict[str, str]] = None  # Named hashes (hex): {"phash": "ff00...", "dhash": "0a1b..."}
    array_fields: Optional[Dict[str, List[str]]] = None  # Dynamic array fields


_HASH_HEX_RE = re.compile(r'^[0-9a-fA-F]+$')


def _validate_hash_hex(field_name: str, hash_hex: str) -> None:
    """Inline-ingest hashes must be a non-empty hex string (the bit cast in
    hash_upsert_sql errors on anything else). Raise a clear 422 instead of an opaque
    DB error that would abort the whole ingest batch."""
    if not isinstance(hash_hex, str) or not _HASH_HEX_RE.match(hash_hex):
        raise HTTPException(status_code=422, detail=f"hashes['{field_name}'] must be a non-empty hex string")


def _merge_batch_field_stats(stats: Dict[str, Any], current: Optional[Any]) -> tuple:
    """Merge measurements using the registry's declared type, never inferring a new type.

    Datetimes retain two bounds; scalar previews are capped at 100 values.
    Completeness is tracked separately, so a later small batch cannot turn an
    incomplete historical preview into a complete enumeration.
    """
    field_type = current['field_type'] if current else stats['type']

    # Calculate new min/max
    new_min = stats['min']
    new_max = stats['max']
    if current and current['min_value'] is not None and new_min is not None:
        new_min = min(current['min_value'], new_min)
    elif current and current['min_value'] is not None:
        new_min = current['min_value']

    if current and current['max_value'] is not None and new_max is not None:
        new_max = max(current['max_value'], new_max)
    elif current and current['max_value'] is not None:
        new_max = current['max_value']

    new_uniques: Optional[list]
    if field_type == 'datetime':
        # The earliest and latest instants (backend/datetimes.py), written as the statistics write them: text order
        # isn't time order once values carry offsets. A value that isn't a datetime has no instant and no say.
        prev_bounds = (current['unique_values'] if current else None) or ()
        instants = sorted(filter(None, map(parse_instant, [*prev_bounds, *stats['unique_values']])))
        new_uniques = [utc_text(instants[0]), utc_text(instants[-1])] if instants else None
    else:
        # Calculate new unique values (limit to 100 to prevent bloat)
        uniques_list = list(stats['unique_values'])
        if current and current['unique_values']:
            # Combine and deduplicate
            combined = set(current['unique_values'])
            combined.update(stats['unique_values'])
            uniques_list = list(combined)

        # Truncate if too large (keep it usable for filters)
        new_uniques = uniques_list
        if len(uniques_list) > 100:
            new_uniques = None  # Stop tracking uniques if too many
        if (
            field_type == 'string'
            and current
            and current['unique_values'] is None
            and current.get('values_complete') is False
        ):
            new_uniques = None

    return field_type, new_min, new_max, new_uniques


@router.post("/datasets/{id}/samples", dependencies=[Depends(require_dataset_access)])
async def add_samples(id: int, payload: Any = Body(...), user: dict = Depends(require_role("power_user"))):
    # A samples wrapper always means add; retain query compatibility for other payloads.
    if (
        isinstance(payload, dict)
        and 'samples' not in payload
        and any(k in payload for k in ("limit", "offset", "filters", "sort_by", "sort_order", "fields"))
    ):
        try:
            req = SamplesRequest(**payload)
        except ValidationError as e:
            # include_context=False: a validator's ValueError in `ctx` is not JSON-serializable — it turned
            # the 422 into a 500 "Object of type ValueError is not JSON serializable".
            raise HTTPException(
                status_code=422,
                detail=e.errors(include_url=False, include_context=False, include_input=False),
            )
        return await list_samples_post(id, req)

    # Extract on_duplicate from wrapper dict if present
    on_duplicate = 'error'  # 'error' | 'skip' | 'update'
    replace_tags = False  # update mode: an existing sample's tags become the ones sent, instead of both
    raw_samples = payload
    if isinstance(payload, dict):
        on_duplicate = payload.get('on_duplicate', 'error')
        replace_tags = payload.get('replace_tags', False)
        raw_samples = payload.get('samples', payload)

    if on_duplicate not in ('error', 'skip', 'update'):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid on_duplicate: {on_duplicate}. Must be 'error', 'skip', or 'update'.",
        )
    if not isinstance(replace_tags, bool):
        raise HTTPException(status_code=400, detail=f"Invalid replace_tags: {replace_tags!r}. Must be true or false.")
    if replace_tags and on_duplicate != 'update':
        raise HTTPException(
            status_code=400,
            detail="replace_tags works only with on_duplicate 'update': other modes never change an existing sample.",
        )

    adapter: TypeAdapter[List[SampleCreate]] = TypeAdapter(List[SampleCreate])
    try:
        samples = adapter.validate_python(raw_samples)
    except ValidationError as e:
        raise HTTPException(
            status_code=422,
            detail=e.errors(include_url=False, include_context=False, include_input=False),
        )

    valid_array_cols = await get_dataset_array_columns(id) - {'tags'}
    requested_array_fields = {name for sample in samples for name in (sample.array_fields or {})}
    if requested_array_fields - valid_array_cols:
        raise HTTPException(
            status_code=422,
            detail=f'Unregistered array fields: {sorted(requested_array_fields - valid_array_cols)}',
        )
    batch_array_fields = sorted(
        {name for sample in samples for name in (sample.array_fields or {}) if name in valid_array_cols},
    )
    for sample in samples:
        for name, value in (sample.hashes or {}).items():
            _validate_hash_hex(name, value)

    # Validate embedding dimensions BEFORE writing: a wrong-dim vector silently poisons the
    # field (search/concepts/quality cast to vector(D)). Collect field -> dims across the batch
    # and check once per field; a mismatch 422s the whole request rather than corrupting data.
    _emb_field_dims: Dict[str, set] = {}
    for s in samples:
        for _fn, _vec in (s.embeddings or {}).items():
            try:
                _emb_field_dims.setdefault(_fn, set()).add(len(_vec))
            except TypeError:
                pass  # non-sized vector — let the insert surface any real error
    await validate_embedding_dims(id, _emb_field_dims)

    sample_ids: List[int] = []
    skipped = 0
    existing_ids: Dict[str, int] = {}  # filepath -> id for duplicates found in 'error' mode
    assert db.pool is not None  # connected at startup (lifespan)
    async with db.pool.acquire() as conn:
        # A full disk mid-ingest PANICs Postgres and can leave it down: refuse
        # the batch with a 507 while there is still room. Rows = each sample + its embeddings + hashes.
        rows = sum(1 + len(s.embeddings or {}) + len(s.hashes or {}) for s in samples)
        await require_free_space(conn, rows * INGEST_ROW_BYTES, f"adding {len(samples)} samples")
        async with conn.transaction():
            await lock_field_lifecycle(conn, id, exclusive=on_duplicate == 'update', wait=on_duplicate != 'update')
            sample_ids, existing_ids, skipped, written_metadata, declared, inserted_fields = await insert_samples(
                conn,
                id,
                samples,
                on_duplicate,
                batch_array_fields,
                replace_tags,
            )
            measured = await conn.fetchval('SELECT initialized FROM dataset_stats_state WHERE dataset_id = $1', id)

            # Track field types and stats in dataset_fields and dataset_field_stats tables
            if written_metadata:
                # 1. Analyze the batch in memory
                batch_stats: Dict[str, Dict[str, Any]] = {}  # field_name -> {type, min, max, unique_values, count}

                for metadata in written_metadata:
                    for key, val in metadata.items():
                        if key not in declared:
                            continue
                        if key not in batch_stats:
                            batch_stats[key] = {
                                'type': declared[key],
                                'min': None,
                                'max': None,
                                'unique_values': set(),
                                'count': 0,
                                'compatible': True,
                            }

                        stats = batch_stats[key]
                        # An explicit null is "no value" (the full refresh never counts it either);
                        # counting it hid those samples from the sidebar's "(no value)".
                        if val is not None:
                            stats['count'] += 1

                        # Track stats
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            if stats['min'] is None or val < stats['min']:
                                stats['min'] = val
                            if stats['max'] is None or val > stats['max']:
                                stats['max'] = val
                            if stats['type'] == 'number':
                                stats['unique_values'].add(json.dumps(val))
                        elif isinstance(val, str) and stats['type'] != 'number':
                            stats['unique_values'].add(val)
                        if val is not None and stats['type'] == 'string' and not isinstance(val, str):
                            stats['compatible'] = False

                # 2. Fetch existing stats to compare.
                # `conn`, NOT `db` — we are inside `async with db.pool.acquire() as conn` and an open
                # transaction, so `db.fetch` would take a SECOND connection from the same pool while
                # holding the first. asyncpg's Pool.acquire() defaults to timeout=None and waits on
                # the queue FOREVER, so once enough concurrent ingests reach this line the worker's
                # pool deadlocks and never recovers. Reusing the held connection also reads the
                # transaction's own snapshot, which is what the comparison below wants.
                # Guarded by backend/tests/test_pool_usage.py.
                existing_stats = await conn.fetch(
                    """
                    SELECT f.field_name, f.field_type, s.min_value, s.max_value, s.unique_values,
                           s.values_complete
                    FROM dataset_fields f LEFT JOIN dataset_field_stats s USING (dataset_id, field_name)
                    WHERE f.dataset_id = $1 AND f.field_name = ANY($2)
                """,
                    id,
                    list(batch_stats.keys()),
                )

                existing_map = {r['field_name']: r for r in existing_stats}

                # 3. Update DB
                for key, stats in sorted(batch_stats.items()):
                    current = existing_map.get(key)
                    upsert_type, new_min, new_max, new_uniques = _merge_batch_field_stats(stats, current)

                    # Perform Upsert
                    await conn.execute(
                        """
                        INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, sample_count,
                                                         min_value, max_value, unique_values, values_complete)
                        VALUES ($1, $2, $3::varchar, $4, $5, $6,
                                CASE WHEN $3::varchar = 'number' AND $7::text[] IS NOT NULL THEN
                                    ARRAY(SELECT DISTINCT value::jsonb #>> '{}'
                                          FROM unnest($7::text[]) value ORDER BY 1)
                                    ELSE $7::text[] END, $8)
                        ON CONFLICT (dataset_id, field_name)
                        DO UPDATE SET
                            sample_count = dataset_field_stats.sample_count + $4,
                            min_value = $5,
                            max_value = $6,
                            unique_values = EXCLUDED.unique_values,
                            values_complete = $8,
                            field_type = $3
                    """,
                        id,
                        key,
                        upsert_type,
                        stats['count'] if not measured else 0,
                        new_min,
                        new_max,
                        new_uniques,
                        bool(
                            (key in inserted_fields or (current and current['values_complete']))
                            and stats['type'] in ('number', 'string')
                            and stats['compatible']
                            and new_uniques is not None,
                        ),
                    )

    if on_duplicate == 'error' and existing_ids:
        return JSONResponse(
            status_code=409,
            content={
                "message": f"{len(existing_ids)} duplicate filepath(s)",
                "ids": sample_ids,
                "existing": existing_ids,
            },
        )

    # In update mode, `ids` stays positional; the message counts distinct samples —
    # the same filepath twice in one on_duplicate='update' batch said "Added 2".
    result: Dict[str, Any] = {"message": f"Added {len(set(sample_ids))} samples", "ids": sample_ids}
    if skipped > 0:
        result["skipped"] = skipped
    if replace_tags:
        result["replace_tags"] = True  # the SDK checks it: a server before replace_tags ignores it and merges
    return result


@router.delete("/datasets/{id}/samples/{sample_id}", dependencies=[Depends(require_dataset_access)])
async def delete_sample(id: int, sample_id: int, user: dict = Depends(require_role("admin"))):
    """
    Delete a single sample
    """
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await lock_field_lifecycle(conn, id, exclusive=True, wait=False)
            # Deleting an image deletes its objects (0030). They go first, here, so they are counted and their fields
            # reach the caches; the key would delete them with the image anyway.
            objects = await conn.fetch(
                'DELETE FROM samples WHERE dataset_id = $2 AND parent_id = $1 RETURNING metadata',
                sample_id,
                id,
            )
            removed = await conn.fetchrow(
                'DELETE FROM samples WHERE id = $1 AND dataset_id = $2 RETURNING metadata',
                sample_id,
                id,
            )
            if removed is None:
                raise HTTPException(status_code=404, detail="Sample not found")
            keys = {key for row in [removed, *objects] for key in row['metadata'] or {}}
            await invalidate_values(conn, id, keys | {'tags'})
            await prune_fields(conn, id, keys)
    if not objects:
        return {"message": "Sample deleted", "objects_deleted": 0}
    return {
        "message": f"Sample and its {len(objects)} object{'s' if len(objects) != 1 else ''} deleted",
        "objects_deleted": len(objects),
    }


class SampleUpdate(BaseModel):
    """Model for updating a sample."""

    @field_validator('metadata')
    @classmethod
    def _metadata(cls, metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        # `@local.` keys are a mirror's own fields: update_sample routes them (local_writes).
        return validate_metadata(metadata, allow_local=True) if metadata is not None else None

    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    remove_metadata_keys: Optional[List[str]] = None
    location: Optional[Dict[str, Any]] = None
    heading: Optional[float] = None
    array_fields: Optional[Dict[str, List[str]]] = None

    @field_validator('location')
    @classmethod
    def _location(cls, loc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return _point_location(loc)


@router.patch("/datasets/{id}/samples/{sample_id}", dependencies=[Depends(require_dataset_access)])
async def update_sample(
    id: int,
    sample_id: int,
    request: SampleUpdate,
    user: dict = Depends(require_role("power_user")),
):
    """
    Update a single sample's tags, metadata, and/or location.

    - tags: If provided, replaces all existing tags
    - metadata: If provided, merges with existing metadata (new fields added, existing updated)
    - remove_metadata_keys: If provided, these top-level keys are removed from metadata.
      Applied after the merge, so ``metadata`` + ``remove_metadata_keys`` together let
      callers authoritatively set a subset of keys (add/update some, remove others).
    - location: If provided, replaces the location
    - heading: If provided, replaces the heading

    Only admin and power_user roles can update samples.

    On a mirror, `@local.<name>` keys in metadata / remove_metadata_keys, and `@local.tags` in array_fields, write
    this server's own fields (docs/FOLLOWERS.md): a request with them changes nothing of the primary's.
    """
    local_set, local_remove, primary_set, primary_remove = local_writes.split_metadata(
        request.metadata,
        request.remove_metadata_keys,
    )
    local_tags = (request.array_fields or {}).get(LOCAL_TAGS)
    if local_set or local_remove or local_tags is not None:
        primary_edits = (
            primary_set,
            primary_remove,
            set(request.array_fields or {}) - {LOCAL_TAGS},
            request.tags is not None,
            request.location is not None,
            request.heading is not None,
        )
        if any(primary_edits):
            raise HTTPException(status_code=409, detail=local_writes.MIXED)
        return await _update_local_fields(id, sample_id, local_set, local_remove, local_tags, user)
    valid_cols = await get_dataset_array_columns(id) - {'tags'} if request.array_fields else set()
    if request.array_fields and set(request.array_fields) - valid_cols:
        raise HTTPException(
            status_code=422,
            detail=f'Unregistered array fields: {sorted(set(request.array_fields) - valid_cols)}',
        )
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await lock_field_lifecycle(
                conn,
                id,
                exclusive=bool(request.remove_metadata_keys),
                wait=not request.remove_metadata_keys,
            )
            # Check sample exists and belongs to dataset. NO KEY: the edit never writes id or dataset_id, so an object
            # written meanwhile can still check its image (the parent key's FOR KEY SHARE); edits still serialize.
            existing = await conn.fetchrow(
                "SELECT id, tags, metadata FROM samples WHERE id = $1 AND dataset_id = $2 FOR NO KEY UPDATE",
                sample_id,
                id,
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Sample not found")

            # Build update query dynamically
            updates = []
            args: List[Any] = []
            arg_idx = 1

            if request.tags is not None:
                updates.append(f"tags = ${arg_idx}")
                args.append(request.tags)
                arg_idx += 1

            # Metadata merge + key removal share the same UPDATE slot since they both
            # rewrite the whole JSONB blob. Read-modify-write: fold in new keys, drop
            # any in remove_metadata_keys, then write the result.
            if request.metadata is not None or request.remove_metadata_keys:
                merged_metadata = {**(existing['metadata'] or {}), **(request.metadata or {})}
                for key in request.remove_metadata_keys or []:
                    merged_metadata.pop(key, None)
                updates.append(f"metadata = ${arg_idx}")
                args.append(merged_metadata)
                arg_idx += 1

            if request.location is not None:
                # Convert GeoJSON to PostGIS
                coords = request.location.get("coordinates", [0, 0])
                lon, lat = coords[0], coords[1]
                updates.append(f"location = ST_SetSRID(ST_MakePoint(${arg_idx}, ${arg_idx + 1}), 4326)")
                args.extend([lon, lat])
                arg_idx += 2

            if request.heading is not None:
                updates.append(f"heading = ${arg_idx}")
                args.append(request.heading)
                arg_idx += 1

            if request.array_fields:
                for col, vals in request.array_fields.items():
                    if col in valid_cols:
                        updates.append(f"{col} = ${arg_idx}")
                        args.append(vals)
                        arg_idx += 1

            if not updates:
                return {"message": "No updates provided", "sample_id": sample_id}

            # Always update last_modified_at
            updates.append("last_modified_at = NOW()")

            # Execute update
            query = f"""
                UPDATE samples
                SET {', '.join(updates)}
                WHERE id = ${arg_idx} AND dataset_id = ${arg_idx + 1}
            """
            args.extend([sample_id, id])

            await conn.execute(query, *args)
            if request.metadata:
                await register_fields(
                    conn,
                    id,
                    [
                        {
                            key: value
                            for key, value in request.metadata.items()
                            if key not in (request.remove_metadata_keys or [])
                        },
                    ],
                )
            if request.metadata is not None or request.remove_metadata_keys:
                await append_discovery(conn, id, [merged_metadata])
            await invalidate_values(
                conn,
                id,
                set(request.metadata or {})
                | set(request.remove_metadata_keys or [])
                | set(request.array_fields or {})
                | ({'tags'} if request.tags is not None else set()),
            )
            if request.tags is not None:
                await conn.execute(
                    "DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = 'tags'",
                    id,
                )
            if request.remove_metadata_keys:
                await prune_fields(conn, id, request.remove_metadata_keys)

    return {"message": "Sample updated", "sample_id": sample_id}


async def _update_local_fields(
    id: int,
    sample_id: int,
    values: Dict[str, Any],
    remove: List[str],
    tags: Optional[List[str]],
    user: dict,
) -> Dict[str, Any]:
    """update_sample on a mirror's own fields: merge `values`, drop `remove`, replace `@local.tags` when given."""
    tags = clean_tags([clean_name(tag, "Tag") for tag in tags]) if tags is not None else None
    async with db.pool.acquire() as conn, conn.transaction():
        await local_writes.begin(conn, id, exclusive=bool(remove))
        found = await conn.fetchval(
            'SELECT 1 FROM samples WHERE id = $1 AND dataset_id = $2 FOR NO KEY UPDATE',
            sample_id,
            id,
        )
        if not found:
            raise HTTPException(status_code=404, detail="Sample not found")
        await conn.execute(
            """UPDATE samples SET local_metadata = (local_metadata || $3::jsonb) - $4::text[],
                                  local_tags = COALESCE($5::text[], local_tags)
               WHERE id = $1 AND dataset_id = $2""",
            sample_id,
            id,
            values,
            remove,
            tags,
        )
        written = {name: value for name, value in values.items() if name not in remove}
        await local_writes.register(conn, id, [written])
        if tags is not None:
            await local_writes.register_tags(conn, id, tags, user['id'])
        await local_writes.finish(conn, id, [*written, *(['tags'] if tags is not None else [])], remove)
        await local_writes.prune(conn, id, remove)
    return {"message": "Sample updated", "sample_id": sample_id}


class BulkTargetRequest(BaseModel):
    sample_ids: List[int] = []
    selection_token: Optional[str] = None
    excluded_ids: List[int] = []
    filters: Optional[Dict[str, Any]] = None
    # Force a job even for a small selection (SDK wait=False).
    background: bool = False


async def _bulk_response(id, user, operation, payload):
    from .. import bulk_jobs

    mirror = await db.fetchrow('SELECT vector_fields FROM mirrors WHERE dataset_id = $1', id)
    # A mirror's own vector fields (patch vectors imported here, say) are this instance's to delete; the database
    # refuses the primary's anyway. Everything else of a mirror is the primary's.
    own_vectors = operation == 'delete_embeddings' and mirror is not None
    if own_vectors and payload['field_name'] in mirror['vector_fields']:
        raise HTTPException(
            status_code=409,
            detail=f"{payload['field_name']} of this mirror comes from its primary: delete it there",
        )
    # This server's own fields (`@local.`): the job writes only them (bulk_jobs._mutate_local).
    own_fields = operation in ('set', 'tag') and payload.get('local')
    if own_fields:
        await local_writes.require_mirror(db, id)
    elif mirror is not None and not own_vectors:
        raise HTTPException(
            status_code=409,
            detail='This dataset is a read-only mirror of a dataset on another Freedom Ridge instance: change it there',
        )

    result = await bulk_jobs.submit(db.pool, id, user['id'], operation, payload)
    return JSONResponse(status_code=202, content=result)


async def _large_bulk_target(id, sample_ids=None, limit=1000):
    # Bound the admission probe even on a multi-million sample dataset. A literal LIMIT keeps
    # the bound visible to cached generic plans.
    if sample_ids is None:
        return (
            await db.fetchval(
                f'SELECT count(*) FROM (SELECT id FROM samples WHERE dataset_id=$1 LIMIT {limit + 1:d}) t',
                id,
            )
            > limit
        )
    if len(sample_ids) <= limit:
        return False
    return (
        await db.fetchval(
            'SELECT count(*) FROM (SELECT id FROM samples WHERE dataset_id=$1 AND id=ANY($2::int[]) '
            f'LIMIT {limit + 1:d}) t',
            id,
            sample_ids,
        )
        > limit
    )


# The SDK's per-sample setters send batches of this size; main applied each in one statement.
PER_SAMPLE_SYNC_LIMIT = 5000


@router.post('/datasets/{id}/samples/bulk-delete', dependencies=[Depends(require_dataset_access)])
async def bulk_delete_samples(id: int, request: BulkTargetRequest, user: dict = Depends(require_role('admin'))):
    if not request.sample_ids and not request.selection_token and request.filters is None:
        raise HTTPException(400, 'Provide sample_ids, selection_token, or filters ({} for all samples)')
    return await _bulk_response(id, user, 'delete', request.model_dump())


class UnlinkedObjectsDelete(BaseModel):
    # Only these kinds (unlinked_objects.KINDS); by default the objects whose image is gone.
    kinds: List[str] = list(unlinked_objects.DEFAULT_DELETE)


async def _link_unlinked_objects(id: int) -> int:
    """The finder's first step: link the objects whose image is there but not linked yet, so they're never counted or
    deleted as having none. Not on a mirror (its sync keeps it) or a dataset being deleted."""
    async with db.pool.acquire() as conn, conn.transaction():
        await lock_field_lifecycle(conn, id)
        return await unlinked_objects.link(conn, id)


async def _mirror_or_deleting(id: int) -> tuple[bool, bool]:
    row = await db.fetchrow(
        'SELECT EXISTS (SELECT 1 FROM mirrors WHERE dataset_id = $1) AS mirror, deleting FROM datasets WHERE id = $1',
        id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail='Dataset not found')
    return row['mirror'], row['deleting']


@router.post('/datasets/{id}/objects/unlinked', dependencies=[Depends(require_dataset_access)])
async def find_unlinked_objects(id: int, user: dict = Depends(get_current_user)):
    """The object samples of this dataset with no image, by kind (unlinked_objects.py), with the filter that shows
    them. For a user who may edit the dataset, the objects whose image is there but not linked yet are linked first
    (`linked`); anyone else sees them as `linkable`. Cheap: idx_samples_unlinked_objects holds only those objects."""
    mirror, deleting = await _mirror_or_deleting(id)
    linked = 0
    if not mirror and not deleting and user['role'] in get_allowed_roles('power_user'):
        linked = await _link_unlinked_objects(id)
    found = await unlinked_objects.counts(db, id)
    return {**found, 'linked': linked, 'mirror': mirror, 'filter': unlinked_objects.filter_leaf()}


@router.post('/datasets/{id}/objects/unlinked/delete', dependencies=[Depends(require_dataset_access)])
async def delete_unlinked_objects(
    id: int,
    request: UnlinkedObjectsDelete,
    user: dict = Depends(require_role('admin')),
):
    """Delete the object samples with no image of the kinds named (by default only those whose image is gone), as a
    bulk delete of their filter: resumable units, logged for followers and statistics like any delete, each unit
    deleting only what still matches. Never automatic. The objects are linked first, so one whose image is there is
    never deleted. Refused on a mirror: its objects change only through its sync."""
    if not request.kinds:
        raise HTTPException(status_code=400, detail=f'Name the kinds to delete: {", ".join(unlinked_objects.KINDS)}')
    try:
        kinds = unlinked_objects.kinds_of(request.kinds)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    mirror, deleting = await _mirror_or_deleting(id)
    if mirror:
        raise HTTPException(
            status_code=409,
            detail='This dataset is a read-only mirror of a dataset on another Freedom Ridge instance: its objects '
            'change only through its sync, so delete them on the primary',
        )
    if deleting:
        raise HTTPException(status_code=409, detail='This dataset is being deleted')
    linked = await _link_unlinked_objects(id)
    found = await unlinked_objects.counts(db, id)
    expected = sum(found['kinds'][kind] for kind in kinds)
    answer = {'kinds': kinds, 'linked': linked, 'expected': expected}
    if not expected:
        return {**answer, 'deleted_count': 0, 'objects_deleted': 0}
    from .. import bulk_jobs

    target = BulkTargetRequest(filters={'logic': 'AND', 'filters': [unlinked_objects.filter_leaf(kinds)]})
    result = await bulk_jobs.submit(db.pool, id, user['id'], 'delete', target.model_dump())
    return JSONResponse(status_code=202, content={**result, **answer})


@router.post('/datasets/{id}/embeddings/{field_name}/delete', dependencies=[Depends(require_dataset_access)])
async def bulk_delete_embeddings(
    id: int,
    field_name: str,
    request: BulkTargetRequest,
    user: dict = Depends(require_role('admin')),
):
    """Delete one embedding field's vectors for a captured target, as a resumable bulk job.

    The target is the one bulk sample deletion takes: ``sample_ids``, a ``selection_token`` or ``filters``
    (``{}`` for every sample of the dataset). Samples and their metadata are untouched. Re-uploading vectors
    of the field's dimension afterwards is accepted and they are queued for map projection.

    Nothing else changes, also when every vector of the field is deleted: the dataset's map state and map point
    counts for the field stay. If the field is refilled by a different model under the same name, re-fit the
    map or use a new field name. The field-global model record and ANN index stay too; see docs/SDK.md.
    """
    if not is_embedding_field_name(field_name):
        raise HTTPException(status_code=400, detail=f"Invalid embedding field name: {field_name!r}")
    if not request.sample_ids and not request.selection_token and request.filters is None:
        raise HTTPException(400, 'Provide sample_ids, selection_token, or filters ({} for all samples)')
    return await _bulk_response(id, user, 'delete_embeddings', {**request.model_dump(), 'field_name': field_name})


class BulkTagRequest(BulkTargetRequest):
    sample_ids: List[int] = []
    # Tag by a selection token (from /selection_token) instead of an id list, so a huge
    # Select-All never ships millions of ids. excluded_ids are samples to skip within the
    # token (the "select all EXCEPT these" case) — a small list.
    selection_token: Optional[str] = None
    excluded_ids: List[int] = []
    add_tags: List[str] = []
    remove_tags: List[str] = []
    field_name: str = "tags"  # Which array field to operate on


def _bulk_target_predicate(request: BulkTagRequest, dataset_id: int, *, start_index: int = 2) -> tuple[str, list]:
    """WHERE predicate (by default reserving $1 for the tag value) selecting rows to tag, plus its
    trailing args. Token mode resolves the set server-side via region_selections (dataset-
    scoped at token creation; the dataset_id guard is defense-in-depth); id-list mode uses
    ANY($sample_ids)."""
    if request.selection_token:
        return (
            f"id IN (SELECT sample_id FROM region_selections WHERE token = ${start_index} "
            "AND created_at > statement_timestamp() - interval '2 hours') "
            f"AND dataset_id = ${start_index + 1} AND NOT (id = ANY(${start_index + 2}))",
            [request.selection_token, dataset_id, request.excluded_ids],
        )
    return (f"id = ANY(${start_index}) AND dataset_id = ${start_index + 1}", [request.sample_ids, dataset_id])


async def _require_live_selection(token: Optional[str], source=None) -> None:
    """Reject expired selections even while bounded cleanup leaves some of their rows behind."""
    if source is None:
        source = db
    if token and not await source.fetchval(
        "SELECT EXISTS (SELECT 1 FROM region_selections WHERE token = $1 "
        "AND created_at > statement_timestamp() - interval '2 hours')",
        token,
    ):
        raise HTTPException(
            status_code=410,
            detail="This selection has expired (selections last 2 hours). Select the samples again.",
        )


@router.post("/datasets/{id}/samples/bulk-tag", dependencies=[Depends(require_dataset_access)])
async def bulk_tag_samples(id: int, request: BulkTagRequest, user: dict = Depends(require_role("power_user"))):
    """Bulk add/remove values from any array field (tags, inferred_tags, etc.)."""
    return await tag_samples(id, request, user)


async def tag_samples(id: int, request: BulkTagRequest, user: dict, made_by: str | None = None):
    """bulk_tag_samples, and a follower's tag edit (routers/followers.py), which says who made it there: `made_by` is
    recorded with a tag this edit adds to the registry (display only). It isn't a request field, so only a follower's
    edit sets it."""
    if not request.sample_ids and not request.selection_token and request.filters is None:
        raise HTTPException(status_code=400, detail="Provide sample_ids, selection_token or filters")
    await _require_live_selection(request.selection_token)
    # A tag to ADD is trimmed and must be a name ("" and "   " were stored). Tags to REMOVE
    # stay as given, so a tag stored verbatim before this check can still be cleaned up.
    request.add_tags = clean_tags([clean_name(t, "Tag") for t in request.add_tags])

    col = request.field_name
    if col == LOCAL_TAGS:  # a mirror's own tags: never sent to the primary
        return await _tag_local(id, request, user)
    array_cols = await get_dataset_array_columns(id)
    if col not in array_cols:
        raise HTTPException(status_code=400, detail=f"'{col}' is not a registered array field")

    overlap = sorted(set(request.add_tags) & set(request.remove_tags))
    if overlap:
        raise HTTPException(status_code=400, detail=f"Tag(s) {overlap} are both added and removed")
    # A repeated id said "Some samples not found" (the count of distinct rows fell short).
    request.sample_ids = list(dict.fromkeys(request.sample_ids))

    if not request.selection_token and request.filters is None:
        count = await db.fetchval(
            "SELECT COUNT(*) FROM samples WHERE id = ANY($1) AND dataset_id = $2",
            request.sample_ids,
            id,
        )
        if count != len(request.sample_ids):
            raise HTTPException(status_code=400, detail=f"Some samples not found in dataset {id}")

    # A mirror's tags are its primary's: sent there when its tags option is both ways, refused otherwise. Its other
    # array fields are the primary's alone: the tags option covers tags only.
    if await db.fetchval('SELECT 1 FROM mirrors WHERE dataset_id = $1', id):
        from .. import mirror_writes

        if request.field_name != 'tags':
            raise HTTPException(
                status_code=409,
                detail=f"{request.field_name} of a mirror is its primary's: only tags go back to the primary",
            )

        return await mirror_writes.forward_tags(db, id, request, user)

    if (
        request.background
        or (
            request.selection_token
            and await db.fetchval(
                "SELECT count(*) FROM (SELECT sample_id FROM region_selections WHERE token=$1 LIMIT 1001) t",
                request.selection_token,
            )
            > 1000
        )
        or request.filters is not None
        or await _large_bulk_target(id, request.sample_ids)
    ):
        payload = {**request.model_dump(), **({'made_by': made_by} if made_by else {})}
        return await _bulk_response(id, user, 'tag', payload)

    where_sql, target_args = _bulk_target_predicate(request, id, start_index=1)
    add_arg = f'${len(target_args) + 1}::text[]'
    remove_arg = f'${len(target_args) + 2}::text[]'
    user_arg = f'${len(target_args) + 3}'
    selection_live = (
        "EXISTS (SELECT 1 FROM region_selections WHERE token = $1 "
        "AND created_at > statement_timestamp() - interval '2 hours')"
        if request.selection_token
        else 'TRUE'
    )
    updated = 0
    assert db.pool is not None  # connected at startup (lifespan)
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await lock_field_lifecycle(conn, id)
            if request.add_tags or request.remove_tags:
                result = await conn.fetchrow(
                    f"""WITH targets AS MATERIALIZED (
                        SELECT id FROM samples WHERE {where_sql} ORDER BY id FOR NO KEY UPDATE
                    ), changed AS (
                        UPDATE samples
                        SET {col} = array(SELECT DISTINCT unnest(samples.{col} || {add_arg})
                                          EXCEPT SELECT unnest({remove_arg})),
                            last_modified_at = NOW(), modified_by_user_id = {user_arg}
                        FROM targets WHERE samples.id = targets.id AND (
                            (cardinality({add_arg}) > 0 AND NOT (COALESCE(samples.{col}, '{{}}') @> {add_arg}))
                            OR (cardinality({remove_arg}) > 0 AND samples.{col} && {remove_arg})
                        )
                        RETURNING samples.id
                    )
                    SELECT (SELECT count(*) FROM changed) AS updated_count,
                           {selection_live} AS selection_live""",
                    *target_args,
                    request.add_tags,
                    request.remove_tags,
                    user["id"],
                )
                if not result['selection_live']:
                    raise HTTPException(
                        status_code=410,
                        detail="This selection has expired (selections last 2 hours). Select the samples again.",
                    )
                updated = result['updated_count']
            if request.add_tags:
                # Tag registry: first sight = created (by whom); every later
                # apply = last used. created_at is left alone for a tag the registry already knew.
                await conn.execute(
                    """
                    INSERT INTO dataset_tags
                        (dataset_id, field_name, tag, created_at, created_by_user_id, created_by_name, last_used_at)
                    SELECT $1, $2, unnest($3::text[]), NOW(), $4, $5, NOW()
                    ON CONFLICT (dataset_id, field_name, tag) DO UPDATE SET last_used_at = EXCLUDED.last_used_at
                    """,
                    id,
                    col,
                    request.add_tags,
                    user["id"],
                    made_by,
                )

            if request.add_tags:
                await conn.execute(
                    """
                    UPDATE dataset_field_stats
                    SET unique_values = ARRAY(SELECT DISTINCT unnest(unique_values || $2::text[]) ORDER BY 1 LIMIT 200),
                        values_complete = values_complete AND
                            (SELECT count(*) FROM (SELECT DISTINCT unnest(unique_values || $2::text[])
                             LIMIT 201) distinct_values) <= 200
                    WHERE dataset_id = $1 AND field_name = $3
                """,
                    id,
                    request.add_tags,
                    col,
                )

            if request.remove_tags:
                await conn.execute("DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2", id, col)

    return {"updated_count": updated, "message": f"Updated {col} for {updated} samples"}


def _metadata_with_local(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """`@local.` keys are a mirror's own fields: bulk_set_metadata routes them (local_writes)."""
    return validate_metadata(metadata, allow_local=True)


async def _tag_local(id: int, request: BulkTagRequest, user: dict) -> Any:
    """tag_samples on `@local.tags`, a mirror's own tags: written here only, whatever its tags option says."""
    await local_writes.require_mirror(db, id)
    overlap = sorted(set(request.add_tags) & set(request.remove_tags))
    if overlap:
        raise HTTPException(status_code=400, detail=f"Tag(s) {overlap} are both added and removed")
    request.sample_ids = list(dict.fromkeys(request.sample_ids))
    if not request.selection_token and request.filters is None:
        count = await db.fetchval(
            "SELECT COUNT(*) FROM samples WHERE id = ANY($1) AND dataset_id = $2",
            request.sample_ids,
            id,
        )
        if count != len(request.sample_ids):
            raise HTTPException(status_code=400, detail=f"Some samples not found in dataset {id}")
    if (
        request.background
        or request.selection_token
        or request.filters is not None
        or (await _large_bulk_target(id, request.sample_ids))
    ):
        return await _bulk_response(id, user, 'tag', {**request.model_dump(), 'local': True})
    async with db.pool.acquire() as conn, conn.transaction():
        await local_writes.begin(conn, id)
        status = await conn.execute(
            """UPDATE samples SET local_tags = ARRAY(SELECT DISTINCT unnest(COALESCE(local_tags, '{}') || $3::text[])
                                              EXCEPT SELECT unnest($4::text[]))
               WHERE dataset_id = $1 AND id = ANY($2::int[])
                 AND (NOT COALESCE(local_tags, '{}') @> $3::text[] OR local_tags && $4::text[])""",
            id,
            request.sample_ids,
            request.add_tags,
            request.remove_tags,
        )
        updated = _parse_update_status(status, 0)
        await local_writes.register_tags(conn, id, request.add_tags, user['id'])
        await local_writes.finish(conn, id, ['tags'])
    return {"updated_count": updated, "message": f"Updated {LOCAL_TAGS} for {updated} samples"}


class BulkSetUpdate(BaseModel):
    _metadata = field_validator('metadata')(_metadata_with_local)

    sample_id: int
    metadata: Dict[str, Any] = {}


class BulkSetRequest(BulkTargetRequest):
    _metadata = field_validator('metadata')(_metadata_with_local)

    sample_ids: List[int] = []
    metadata: Dict[str, Any] = {}
    remove_metadata_keys: List[str] = []
    # Per-sample mode: each sample is merged with its own patch (one statement).
    # Mutually exclusive with the same-value (sample_ids/metadata) mode above.
    updates: Optional[List[BulkSetUpdate]] = None


def _parse_update_status(status, fallback: int) -> int:
    """asyncpg returns a command tag like 'UPDATE 42'."""
    try:
        return int(status.split()[-1])
    except (AttributeError, ValueError, IndexError):
        return fallback


@router.post("/datasets/{id}/samples/bulk-set", dependencies=[Depends(require_dataset_access)])
async def bulk_set_metadata(id: int, request: BulkSetRequest, user: dict = Depends(require_role("power_user"))):
    """Bulk-merge metadata onto many samples in a single statement.

    Two modes after ordered row locking, touching only this dataset's rows:
      - same value: shallow-merge `metadata` into every `sample_ids`, then drop
        `remove_metadata_keys` (mirrors the single-sample PATCH).
      - per sample: `updates=[{sample_id, metadata}, ...]` merges each sample
        with its own patch (used for per-sample scores like uniqueness).

    On a mirror, `@local.<name>` keys write this server's own fields (docs/FOLLOWERS.md), never mixed with the
    primary's in one request.
    """
    local = await _bulk_set_local(id, request, user)
    if local is not None:
        return local
    patches = {update.sample_id: update.metadata for update in request.updates} if request.updates is not None else None
    sample_ids = list(patches) if patches is not None else request.sample_ids
    if sample_ids:
        # Refuse the complete explicit request before allocating job targets; queued
        # writes also recheck capacity per chunk when the runner executes them.
        async with db.pool.acquire() as conn:
            await require_free_space(
                conn,
                len(sample_ids) * INGEST_ROW_BYTES,
                f"setting metadata on {len(sample_ids)} samples",
            )
    if (
        request.background
        or request.selection_token
        or request.filters is not None
        or await _large_bulk_target(id, sample_ids, PER_SAMPLE_SYNC_LIMIT if patches is not None else 1000)
    ):
        return await _bulk_response(id, user, 'set', {**request.model_dump(), 'per_sample': patches is not None})
    if not sample_ids:
        return {"updated_count": 0, "message": "No samples provided"}
    remove_keys = [] if patches is not None else request.remove_metadata_keys
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await lock_field_lifecycle(conn, id, exclusive=bool(remove_keys), wait=not remove_keys)
            # NO KEY, as update_sample: only metadata is written.
            await conn.fetchval(
                'SELECT count(*) FROM (SELECT id FROM samples WHERE dataset_id = $1 '
                'AND id = ANY($2::int[]) ORDER BY id FOR NO KEY UPDATE) locked_samples',
                id,
                sample_ids,
            )
            if patches is None:
                status = await conn.execute(
                    """UPDATE samples
                       SET metadata = (COALESCE(metadata, '{}'::jsonb) || $1::jsonb) - $2::text[]
                       WHERE dataset_id = $3 AND id = ANY($4::int[])""",
                    request.metadata,
                    remove_keys,
                    id,
                    sample_ids,
                )
                updated = _parse_update_status(status, 0)
                written_metadata = [{key: value for key, value in request.metadata.items() if key not in remove_keys}]
            else:
                written = await conn.fetch(
                    """UPDATE samples AS samples
                       SET metadata = COALESCE(samples.metadata, '{}'::jsonb) || payload.patch::jsonb
                       FROM unnest($1::int[], $2::text[]) AS payload(id, patch)
                       WHERE samples.id = payload.id AND samples.dataset_id = $3
                       RETURNING samples.id""",
                    sample_ids,
                    [json.dumps(patch) for patch in patches.values()],
                    id,
                )
                updated = len(written)
                written_metadata = [patches[row['id']] for row in written]
            if updated:
                await register_fields(conn, id, written_metadata)
                await append_discovery(conn, id, written_metadata)
                await invalidate_values(
                    conn,
                    id,
                    set(remove_keys) | {key for patch in written_metadata for key in patch},
                )
                await prune_fields(conn, id, remove_keys)
    return {"updated_count": updated, "message": f"Updated metadata for {updated} samples"}


async def _bulk_set_local(id: int, request: BulkSetRequest, user: dict) -> Any:
    """bulk_set_metadata on a mirror's own fields, or None when the request names none of them."""
    values: Dict[str, Any] = {}
    remove: List[str] = []
    patches: Optional[Dict[int, Dict[str, Any]]] = None
    if request.updates is not None:
        parts = {update.sample_id: local_writes.split_metadata(update.metadata, None) for update in request.updates}
        patches = {sample_id: part[0] for sample_id, part in parts.items()}
        mixed = any(part[2] for part in parts.values())
        named = any(part[0] for part in parts.values())
    else:
        values, remove, primary_set, primary_remove = local_writes.split_metadata(
            request.metadata,
            request.remove_metadata_keys,
        )
        mixed = bool(primary_set or primary_remove)
        named = bool(values or remove)
    if not named:
        return None
    if mixed:
        raise HTTPException(status_code=409, detail=local_writes.MIXED)
    await local_writes.require_mirror(db, id)
    sample_ids = list(patches) if patches is not None else request.sample_ids
    if sample_ids:
        async with db.pool.acquire() as conn:
            await require_free_space(
                conn,
                len(sample_ids) * INGEST_ROW_BYTES,
                f"setting fields on {len(sample_ids)} samples",
            )
    if (
        request.background
        or request.selection_token
        or request.filters is not None
        or await _large_bulk_target(id, sample_ids, PER_SAMPLE_SYNC_LIMIT if patches is not None else 1000)
    ):
        payload = {
            **request.model_dump(),
            'metadata': values,
            'remove_metadata_keys': remove,
            'updates': None if patches is None else [{'sample_id': k, 'metadata': v} for k, v in patches.items()],
            'per_sample': patches is not None,
            'local': True,
        }
        return await _bulk_response(id, user, 'set', payload)
    async with db.pool.acquire() as conn, conn.transaction():
        await local_writes.begin(conn, id, exclusive=bool(remove))
        await conn.fetchval(
            'SELECT count(*) FROM (SELECT id FROM samples WHERE dataset_id = $1 '
            'AND id = ANY($2::int[]) ORDER BY id FOR NO KEY UPDATE) locked_samples',
            id,
            sample_ids,
        )
        if patches is None:
            status = await conn.execute(
                """UPDATE samples SET local_metadata = (local_metadata || $1::jsonb) - $2::text[]
                   WHERE dataset_id = $3 AND id = ANY($4::int[])""",
                values,
                remove,
                id,
                sample_ids,
            )
            updated = _parse_update_status(status, 0)
            written = [{name: value for name, value in values.items() if name not in remove}]
        else:
            rows = await conn.fetch(
                """UPDATE samples AS samples SET local_metadata = samples.local_metadata || payload.patch::jsonb
                   FROM unnest($1::int[], $2::text[]) AS payload(id, patch)
                   WHERE samples.id = payload.id AND samples.dataset_id = $3
                   RETURNING samples.id""",
                sample_ids,
                [json.dumps(patch) for patch in patches.values()],
                id,
            )
            updated = len(rows)
            written = [patches[row['id']] for row in rows]
        if updated:
            await local_writes.register(conn, id, written)
        await local_writes.finish(conn, id, {name for patch in written for name in patch}, remove)
        await local_writes.prune(conn, id, remove)
    return {"updated_count": updated, "message": f"Updated this server's own fields for {updated} samples"}


class FieldRenameRequest(BaseModel):
    new_name: str
    background: bool = False
    # Replace a key that already holds values. Without it, renaming or cloning onto such a key is
    # a 409: jsonb `||` used to overwrite it on every sample with no warning
    # (renaming `label2` onto `dt` silently replaced 30 datetimes with label strings).
    overwrite: bool = False


_DISCOVERY_CACHES = list(DISCOVERY_TYPES)


async def _invalidate_field_derivations(connection, dataset_id: int, field_names):
    """Drop only derived vocabularies as metadata keys change; keep discovery warm."""
    names = set(field_names)
    context = await label_context(connection, dataset_id)
    derived = {
        f'{name}.label' for name in names if name in context.label_fields and f'{name}.label' not in context.registered
    }
    if 'segmentation' in names:
        derived.add('segmentation.labels')
    await connection.execute(
        'DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
        dataset_id,
        sorted(derived),
    )


async def _carry_segmentation_entries(
    connection,
    dataset_id: int,
    field: str,
    new_name: Optional[str],
    *,
    keep_source: bool,
) -> None:
    """Give new_name the field's class names, colours and default class rule: its segmentation_config entries
    (mask_targets, color_scheme.fields and class_rules, nested fields included), which are keyed by field name.
    new_name None only drops them; keep_source leaves the field's own entries in place."""
    # NO KEY UPDATE serializes with PATCH /datasets/{id} but not with writers' KEY SHARE dataset lock.
    config = await connection.fetchval(
        'SELECT segmentation_config FROM datasets WHERE id = $1 FOR NO KEY UPDATE',
        dataset_id,
    )
    if isinstance(config, str):
        config = json.loads(config)
    changed = False
    for section in (
        (config or {}).get('mask_targets'),
        ((config or {}).get('color_scheme') or {}).get('fields'),
        (config or {}).get('class_rules'),
    ):
        if not isinstance(section, dict):
            continue
        keys = [key for key in section if key == field or key.startswith(f'{field}.')]
        if not keys:
            continue
        carried = {} if new_name is None else {new_name + key[len(field) :]: section[key] for key in keys}
        if not keep_source:
            for key in keys:
                del section[key]
        section.update(carried)
        changed = True
    if changed:
        await connection.execute('UPDATE datasets SET segmentation_config = $2 WHERE id = $1', dataset_id, config)


def _refuse_local_field_operation(*names: str) -> None:
    """Rename, clone and clear work on metadata fields; a mirror's own (`@local.`) fields aren't taken yet."""
    if any(name.startswith(LOCAL_PREFIX) for name in names):
        raise HTTPException(
            status_code=400,
            detail="Rename, clone and clear don't take this server's own (@local.) fields yet: "
            'set and remove them with bulk-set',
        )


async def _check_field_target(
    dataset_id: int,
    field_name: str,
    new_name: str,
    overwrite: bool,
    connection=None,
) -> bool:
    """Validate a rename/clone target; True when it already holds values (and overwrite allowed)."""
    _refuse_local_field_operation(field_name, new_name)
    _validate_field_name(field_name)
    _validate_field_name(new_name)
    if new_name == field_name:
        raise HTTPException(status_code=400, detail="new_name is the field's current name")
    connection = connection or db
    source = await connection.fetchrow(
        'SELECT origin FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
        dataset_id,
        field_name,
    )
    if source is None:
        raise HTTPException(status_code=404, detail=f"Field '{field_name}' is not registered")
    if source['origin'] != 'metadata':
        raise HTTPException(status_code=400, detail='Rename and clone require a metadata field')
    target_origin = await connection.fetchval(
        'SELECT origin FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
        dataset_id,
        new_name,
    )
    if target_origin is not None and target_origin != 'metadata':
        raise HTTPException(status_code=409, detail='Cannot replace a native or system field with metadata')
    taken = await connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM samples WHERE dataset_id = $1 AND metadata ? $2)",
        dataset_id,
        new_name,
    )
    if taken and not overwrite:
        raise HTTPException(
            status_code=409,
            detail=f"Field '{new_name}' already has values in this dataset; pass overwrite=true to replace them",
        )
    if taken and await connection.fetchval(
        "SELECT is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
        dataset_id,
        new_name,
    ):
        # Its index casts the OLD values' type (e.g. ::float); writing the new values through it
        # can fail half-way. Demoting first makes the overwrite a plain data change.
        raise HTTPException(status_code=409, detail=f"Field '{new_name}' is promoted (indexed); demote it first")
    return bool(taken)


@asynccontextmanager
async def _field_index_operation(connection, dataset_id: int, *, index_ddl: bool = False):
    """Reject competing index operations without holding a waiting PostgreSQL snapshot. `index_ddl`: the
    operation will CREATE or DROP an index CONCURRENTLY on samples, so it also takes the table's index
    lock (index_jobs.TABLE_LOCK), never waiting for it: a second concurrent index DDL on samples would
    wait for the table lock holding a snapshot and kill the first one's last phase as a deadlock."""
    from ..index_jobs import TABLE_LOCK, try_lock, unlock

    if not await connection.fetchval('SELECT pg_try_advisory_lock(20261017, $1)', dataset_id):
        raise HTTPException(status_code=409, detail='Another index operation is running for this dataset; retry')
    table_held = False
    try:
        if index_ddl:
            if not await try_lock(connection, TABLE_LOCK, 'samples'):
                raise HTTPException(
                    status_code=409,
                    detail='An index is being built or removed on samples; retry when it finishes',
                    headers={'Retry-After': '30'},
                )
            table_held = True
        await connection.fetchval('SELECT require_writable_dataset($1)', dataset_id)
        yield
    finally:
        if table_held:
            await unlock(connection, TABLE_LOCK, 'samples')
        await connection.execute('SELECT pg_advisory_unlock(20261017, $1)', dataset_id)


async def _is_promoted(dataset_id: int, field_name: str) -> bool:
    return bool(
        await db.fetchval(
            'SELECT is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
            dataset_id,
            field_name,
        ),
    )


async def _drop_promoted_index(connection, dataset_id: int, field_name: str) -> None:
    """DROP INDEX CONCURRENTLY (outside any transaction, so reads and writes keep flowing)."""
    await connection.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{_promoted_index_name(dataset_id, field_name)}"')


async def _refuse_parent_field_change(id: int, *names: str) -> None:
    """parent_id names each object sample's image (0029). On a dataset with object samples, renaming or clearing the
    field, or copying another over it, would unlink every object at once, so it is refused. Writing one sample's
    parent_id (PATCH, a bulk set) re-points that object and stays allowed."""
    if 'parent_id' in names and await db.fetchval(
        "SELECT EXISTS (SELECT 1 FROM samples WHERE dataset_id = $1 AND media_type = 'object')",
        id,
    ):
        raise HTTPException(
            status_code=409,
            detail="parent_id names each object sample's image, and this dataset has object samples, so the field "
            "can't be renamed, cleared or overwritten. Re-point an object by writing its own parent_id",
        )


@router.post("/datasets/{id}/fields/{field_name}/rename", dependencies=[Depends(require_dataset_access)])
async def rename_field(
    id: int,
    field_name: str,
    request: FieldRenameRequest,
    user: dict = Depends(require_role("power_user")),
):
    """Rename a metadata key across every sample (keys bound as params; SQLi-safe).

    The field's registration — type, display type, promotion — and its cached stats follow the
    rename (they stayed on the old name, so the schema showed
    neither name until a stats refresh, the old name lingered as an empty export column, and the
    renamed field came back unpromoted with a re-inferred type).
    """
    # Reject invalid requests before choosing sync/job execution; revalidate under
    # the definition guard below (or at job submission) to cover concurrent writes.
    await _refuse_parent_field_change(id, field_name, request.new_name)
    await _check_field_target(id, field_name, request.new_name, request.overwrite)
    if request.background or await _large_bulk_target(id):
        return await _bulk_response(id, user, 'rename', {**request.model_dump(), 'field_name': field_name})
    assert db.pool is not None
    promoted = await _is_promoted(id, field_name)
    from .. import index_jobs

    rebuilding = await index_jobs.is_rebuilding(db, id, field_name)  # its old index is still there (0024)
    async with db.pool.acquire() as conn, _field_index_operation(conn, id, index_ddl=promoted or rebuilding):
        async with conn.transaction():
            await lock_field_lifecycle(conn, id, exclusive=True)
            target_had_values = await _check_field_target(id, field_name, request.new_name, request.overwrite, conn)
            was_promoted = bool(
                await conn.fetchval(
                    'SELECT is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
                    id,
                    field_name,
                ),
            )
            if was_promoted and not promoted:
                raise HTTPException(status_code=409, detail='The field was just indexed; retry')
            status = await conn.execute(
                """
                UPDATE samples
                SET metadata = (metadata - $2) || jsonb_build_object($3::text, metadata->$2)
                WHERE dataset_id = $1 AND metadata ? $2
                """,
                id,
                field_name,
                request.new_name,
            )
            await conn.execute(
                "DELETE FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
                id,
                request.new_name,
            )
            await conn.execute(
                "UPDATE dataset_fields SET field_name = $3, is_promoted = FALSE "
                "WHERE dataset_id = $1 AND field_name = $2",
                id,
                field_name,
                request.new_name,
            )
            await conn.execute(
                "DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2",
                id,
                request.new_name,
            )
            if target_had_values:
                # Samples without the old key kept their own values for the new one: recompute.
                await conn.execute(
                    "DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2",
                    id,
                    field_name,
                )
            else:
                # Same values under a new key: the stats are exact as they stand.
                await conn.execute(
                    "UPDATE dataset_field_stats SET field_name = $3 WHERE dataset_id = $1 AND field_name = $2",
                    id,
                    field_name,
                    request.new_name,
                )
            await copy_discovery(conn, id, field_name, request.new_name)
            await _carry_segmentation_entries(conn, id, field_name, request.new_name, keep_source=False)
            await _invalidate_field_derivations(conn, id, [field_name, request.new_name])
            # Being rebuilt: the new name is rebuilt instead, and the old name's index goes below.
            moved = rebuilding and await index_jobs.move_field_rebuild(conn, id, field_name, request.new_name)
            await conn.execute(
                REQUEST_FULL_REFRESH,
                id,
            )
        if was_promoted or moved:
            await _drop_promoted_index(conn, id, field_name)
    if was_promoted:
        await promote_field(id, request.new_name, user)
    return {"updated_count": _parse_update_status(status, 0), "promoted": was_promoted}


@router.post("/datasets/{id}/fields/{field_name}/clone", dependencies=[Depends(require_dataset_access)])
async def clone_field(
    id: int,
    field_name: str,
    request: FieldRenameRequest,
    user: dict = Depends(require_role("power_user")),
):
    """Copy a metadata key's value into a new key on every sample.

    The copy is registered with the source's type and display type (not promoted — indexing is a
    choice per field) and, when the new key was empty, starts with the source's exact stats.
    """
    await _refuse_parent_field_change(id, request.new_name)
    await _check_field_target(id, field_name, request.new_name, request.overwrite)
    if request.background or await _large_bulk_target(id):
        return await _bulk_response(id, user, 'clone', {**request.model_dump(), 'field_name': field_name})
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await lock_field_lifecycle(conn, id, exclusive=True, wait=False)
            target_had_values = await _check_field_target(id, field_name, request.new_name, request.overwrite, conn)
            status = await conn.execute(
                """
                UPDATE samples
                SET metadata = metadata || jsonb_build_object($3::text, metadata->$2)
                WHERE dataset_id = $1 AND metadata ? $2
                """,
                id,
                field_name,
                request.new_name,
            )
            await conn.execute(
                "DELETE FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
                id,
                request.new_name,
            )
            await conn.execute(
                """
                INSERT INTO dataset_fields
                    (dataset_id, field_name, field_type, display_type, is_promoted, origin, overlay_config)
                SELECT dataset_id, $3, field_type, display_type, FALSE, origin, overlay_config FROM dataset_fields
                WHERE dataset_id = $1 AND field_name = $2
                """,
                id,
                field_name,
                request.new_name,
            )
            await conn.execute(
                "DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2",
                id,
                request.new_name,
            )
            if not target_had_values:
                await conn.execute(
                    """
                    INSERT INTO dataset_field_stats
                        (dataset_id, field_name, field_type, sample_count,
                         min_value, max_value, unique_values, values_complete)
                    SELECT dataset_id, $3, field_type, sample_count,
                           min_value, max_value, unique_values, values_complete
                    FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2
                    """,
                    id,
                    field_name,
                    request.new_name,
                )
            await copy_discovery(conn, id, field_name, request.new_name)
            await _carry_segmentation_entries(conn, id, field_name, request.new_name, keep_source=True)
            await _invalidate_field_derivations(conn, id, [request.new_name])
            await conn.execute(
                REQUEST_FULL_REFRESH,
                id,
            )
    return {"updated_count": _parse_update_status(status, 0)}


@router.post("/datasets/{id}/fields/{field_name}/clear", dependencies=[Depends(require_dataset_access)])
async def clear_field(
    id: int,
    field_name: str,
    user: dict = Depends(require_role("power_user")),
    background: bool = False,
):
    """Remove a metadata key from every sample in the dataset — and its registration, cached stats
    and promoted index, which used to linger as an empty schema/export column."""
    await _refuse_parent_field_change(id, field_name)
    if background or await _large_bulk_target(id):
        return await _bulk_response(id, user, 'clear', {'field_name': field_name})
    _refuse_local_field_operation(field_name)
    _validate_field_name(field_name)
    promoted = await _is_promoted(id, field_name)
    from .. import index_jobs

    rebuilding = await index_jobs.is_rebuilding(db, id, field_name)  # its old index is still there (0024)
    async with db.pool.acquire() as conn, _field_index_operation(conn, id, index_ddl=promoted or rebuilding):
        async with conn.transaction():
            await lock_field_lifecycle(conn, id, exclusive=True)
            registration = await conn.fetchrow(
                'SELECT origin, is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
                id,
                field_name,
            )
            if registration is not None and registration['is_promoted'] and not promoted:
                raise HTTPException(status_code=409, detail='The field was just indexed; retry')
            # Clearing only removes metadata keys; for a native column it would just delete the registration,
            # hiding the column's data and making later array writes fail until the field is created again.
            if registration is not None and registration['origin'] != 'metadata':
                raise HTTPException(status_code=400, detail='Clear requires a metadata field')
            was_promoted = bool(registration and registration['is_promoted'])
            status = await conn.execute(
                "UPDATE samples SET metadata = metadata - $2 WHERE dataset_id = $1 AND metadata ? $2",
                id,
                field_name,
            )
            await conn.execute("DELETE FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2", id, field_name)
            await conn.execute(
                "DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2",
                id,
                field_name,
            )
            await _invalidate_field_derivations(conn, id, [field_name])
            moved = rebuilding and await index_jobs.move_field_rebuild(conn, id, field_name, None)
        if was_promoted or moved:
            await _drop_promoted_index(conn, id, field_name)
    return {"updated_count": _parse_update_status(status, 0)}


class AggregationSpec(BaseModel):
    field: str
    op: str  # count|sum|mean|std|min|max|bounds|quantiles|count_values|distinct|values
    quantiles: Optional[List[float]] = None  # for op="quantiles" (default [.25,.5,.75])
    limit: Optional[int] = None  # cap for count_values/distinct/values; None = uncapped


class AggregateRequest(BaseModel):
    aggregations: List[AggregationSpec]
    filters: Optional[Dict[str, Any]] = None


def _agg_value_expr(field: str, array_columns: Optional[set] = None) -> str:
    """SQL expression yielding a categorical value (for count_values/distinct/values)."""
    if field == 'media_type':
        return "media_type"
    if field in (array_columns if array_columns is not None else _get_array_columns_sync()):
        return f"unnest({field})"
    if field == 'filepath':
        return "filepath"
    if field == 'id':
        return "id::text"
    # Dotted annotation-label fields (detections.label, polygons.label): unnest the
    # JSONB array so count_values/distinct count PER OBJECT — consistent with how
    # parse_filter_node filters these fields. A set-returning expr is valid here because
    # the aggregate endpoint wraps it as `SELECT <expr> AS v FROM samples`.
    if label_path(field, array_columns):
        arr, sub = field.rsplit('.', 1)
        _validate_field_name(arr)
        _validate_field_name(sub)
        return (
            f"jsonb_array_elements(CASE WHEN jsonb_typeof(metadata->'{arr}') = 'array' "
            f"THEN metadata->'{arr}' ELSE '[]'::jsonb END)->>'{sub}'"
        )
    if field == 'segmentation.labels':
        return (
            "jsonb_array_elements_text(CASE WHEN "
            "jsonb_typeof(metadata->'segmentation'->'labels') = 'array' "
            "THEN metadata->'segmentation'->'labels' ELSE '[]'::jsonb END)"
        )
    _validate_field_name(field)
    return f"metadata->>'{field}'"


def _agg_numeric_expr(field: str) -> str:
    """SQL expression yielding a float for numeric aggregations.

    Non-numeric / unparseable metadata values become NULL (skipped by the
    aggregates), so a single bad value can't 500 the whole query — same
    regex guard get_histogram uses.
    """
    if field == 'id':
        return "id::double precision"
    _validate_field_name(field)
    col = f"metadata->>'{field}'"
    return f"CASE WHEN ({col}) ~ '^[-+]?\\d+(\\.\\d+)?([eE][+-]?\\d+)?$' THEN ({col})::double precision ELSE NULL END"


@router.post("/datasets/{id}/aggregate", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def aggregate(id: int, request: AggregateRequest):
    """Run one or more aggregations over the (optionally filtered) dataset in one request.

    Each entry is {field, op, ...}. Numeric ops: count, sum, mean, std, min, max,
    bounds, quantiles. Categorical/any ops: count_values, distinct, values
    (exact and uncapped unless `limit` is given). Returns {"results": [...]}, one
    per aggregation, in order.
    """
    filters_json = json.dumps(request.filters) if request.filters else None
    where_clause, base_args, _ = build_sample_query_parts(
        id,
        filters=filters_json,
        array_columns=await get_dataset_array_columns(id),
        numeric_indexed_fields=await get_promoted_numeric_fields(id) if filters_json else None,
    )
    return await run_aggregations(id, request.aggregations, where_clause, base_args)


async def run_aggregations(
    id: int,
    aggregations: List[AggregationSpec],
    where_clause: str,
    base_args: list,
    with_clause: str = "",
    array_columns: Optional[set] = None,
) -> dict:
    """Run aggregations over `samples` (or, given `with_clause`, a CTE named `samples`).

    `with_clause` is a leading ``WITH samples AS (...)`` that stands in for the table, which is how
    a version's stored rows are aggregated with the live dataset's SQL. `array_columns` is the field
    context for those rows (a version's own fields); by default the dataset's current one.
    """
    results = []
    for spec in aggregations:
        op, field = spec.op, spec.field

        if op == "count":
            n = await db.fetchval(f"{with_clause}SELECT count(*) FROM samples {where_clause}", *base_args)
            results.append({"op": op, "field": field, "value": int(n or 0)})

        elif op in ("sum", "mean", "std", "min", "max", "bounds"):
            # The numeric expr already NULLs non-numeric values, so no null guard
            # is needed (aggregates skip NULL).
            expr = _agg_numeric_expr(field)
            if op == "bounds":
                r = await db.fetchrow(
                    f"{with_clause}SELECT min({expr}) AS mn, max({expr}) AS mx FROM samples {where_clause}",
                    *base_args,
                )
                lo = float(r['mn']) if r and r['mn'] is not None else None
                hi = float(r['mx']) if r and r['mx'] is not None else None
                results.append({"op": op, "field": field, "value": [lo, hi]})
            else:
                fn = {"sum": "sum", "mean": "avg", "std": "stddev_samp", "min": "min", "max": "max"}[op]
                v = await db.fetchval(
                    f"{with_clause}SELECT {fn}({expr}) FROM samples {where_clause}",
                    *base_args,
                )
                results.append({"op": op, "field": field, "value": float(v) if v is not None else None})

        elif op == "quantiles":
            qs = spec.quantiles or [0.25, 0.5, 0.75]
            expr = _agg_numeric_expr(field)
            args = list(base_args) + [qs]
            r = await db.fetchval(
                f"{with_clause}SELECT percentile_cont(${len(args)}::double precision[]) "
                f"WITHIN GROUP (ORDER BY {expr}) FROM samples {where_clause}",
                *args,
            )
            results.append({"op": op, "field": field, "quantiles": qs, "value": [float(x) for x in r] if r else []})

        elif op in ("count_values", "distinct", "values"):
            columns = array_columns if array_columns is not None else await get_dataset_array_columns(id)
            value_expr = _agg_value_expr(field, array_columns=columns)
            # A datetime field's values in instant order (backend/datetimes.py), those without one last, then by text.
            datetime_field = field in getattr(columns, 'datetime_fields', ())

            def order(column):
                return f"{instant_sql(column)} NULLS LAST, {column}" if datetime_field else column

            # A datetime's text read once a sample: values sort on each one's instant, which would name it a dozen
            # times (read_once); distinct reads it for its key and its filter (fenced: a join there costs more).
            values = (
                f"v FROM samples {read_once(value_expr, 'v')}" if datetime_field else f"{value_expr} AS v FROM samples"
            )
            fence = " OFFSET 0" if datetime_field else ""

            args = list(base_args)
            limit_sql = ""
            if spec.limit is not None:
                args.append(spec.limit)
                limit_sql = f" LIMIT ${len(args)}"
            if op == "count_values":
                rows = await db.fetch(
                    f"{with_clause}SELECT v AS value, count(*) AS count FROM "
                    f"(SELECT {value_expr} AS v FROM samples {where_clause}) sub "
                    f"WHERE v IS NOT NULL GROUP BY v ORDER BY count DESC{limit_sql}",
                    *args,
                )
                results.append(
                    {"op": op, "field": field, "value": [{"value": r['value'], "count": r['count']} for r in rows]},
                )
            elif op == "distinct":
                rows = await db.fetch(
                    f"{with_clause}SELECT value FROM (SELECT DISTINCT v AS value FROM "
                    f"(SELECT {value_expr} AS v FROM samples {where_clause}{fence}) sub "
                    f"WHERE v IS NOT NULL) found ORDER BY {order('value')}{limit_sql}",
                    *args,
                )
                results.append({"op": op, "field": field, "value": [r['value'] for r in rows]})
            else:  # values — every value, not deduped (unlike distinct)
                rows = await db.fetch(
                    f"{with_clause}SELECT v AS value FROM "
                    f"(SELECT {values} {where_clause}) sub "
                    f"WHERE v IS NOT NULL ORDER BY {order('v')}{limit_sql}",
                    *args,
                )
                results.append({"op": op, "field": field, "value": [r['value'] for r in rows]})

        else:
            raise HTTPException(status_code=400, detail=f"Unknown aggregation op: {op!r}")

    return {"results": results}


class TagCountsRequest(BaseModel):
    sample_ids: List[int] = []
    # Same token/exclusions option as bulk-tag, so the modal can show counts for a huge
    # Select-All without shipping the ids.
    selection_token: Optional[str] = None
    excluded_ids: List[int] = []
    field_name: str = "tags"


# POST (not GET): selections can be tens of thousands of ids — a query string 414s
# past ~1k samples, the same reason bulk-tag takes a body.
@router.post("/datasets/{id}/samples/tag-counts", dependencies=[Depends(require_dataset_access)])
@guest_heavy
async def get_tag_counts(id: int, request: TagCountsRequest, user: dict = Depends(get_current_user)):
    """Get value counts for any array field on selected samples."""
    col = request.field_name

    if not request.sample_ids and not request.selection_token:
        return {"all_tags": [], "tag_counts": {}, "total_samples": 0}
    await _require_live_selection(request.selection_token)

    if col == LOCAL_TAGS:
        column = 'local_tags'  # a mirror's own tags (docs/FOLLOWERS.md)
    else:
        array_cols = await get_dataset_array_columns(id)
        if col not in array_cols:
            raise HTTPException(status_code=400, detail=f"'{col}' is not a registered array field")
        column = col

    # Selection predicate over `s`: token mode resolves the set via region_selections
    # (minus any exceptions); id-list mode uses ANY($sample_ids). Same $-numbering either way.
    if request.selection_token:
        sel_where = (
            "s.id IN (SELECT sample_id FROM region_selections WHERE token = $1) "
            "AND s.dataset_id = $2 AND NOT (s.id = ANY($3))"
        )
        sel_args: list = [request.selection_token, id, request.excluded_ids]
    else:
        sel_where = "s.id = ANY($1) AND s.dataset_id = $2"
        sel_args = [request.sample_ids, id]

    async def measure(source):
        all_vals = await source.fetch(
            f"""SELECT DISTINCT unnest({column}) as tag FROM samples
                WHERE dataset_id = $1 AND {column} IS NOT NULL AND array_length({column}, 1) > 0
                ORDER BY tag""",
            id,
        )
        count_rows = await source.fetch(
            f"""SELECT t.tag AS tag, count(DISTINCT s.id) AS cnt
                FROM samples s, unnest(s.{column}) AS t(tag)
                WHERE {sel_where}
                GROUP BY t.tag""",
            *sel_args,
        )
        sel_counts = {row['tag']: row['cnt'] for row in count_rows}
        tag_counts = {row['tag']: sel_counts.get(row['tag'], 0) for row in all_vals}
        total_samples = (
            await source.fetchval(f"SELECT count(*) FROM samples s WHERE {sel_where}", *sel_args)
            if request.selection_token
            else len(request.sample_ids)
        )
        return all_vals, tag_counts, total_samples

    if request.selection_token:
        assert db.pool is not None
        async with db.pool.acquire() as connection:
            async with connection.transaction(isolation='repeatable_read', readonly=True):
                await _require_live_selection(request.selection_token, connection)
                all_vals, tag_counts, total_samples = await measure(connection)
    else:
        all_vals, tag_counts, total_samples = await measure(db)

    tag_info = await _tag_registry_info(id, col, [r['tag'] for r in all_vals], user)
    return {
        "all_tags": [r['tag'] for r in all_vals],
        "tag_counts": tag_counts,
        "total_samples": total_samples,
        "tag_info": tag_info,
    }


async def _tag_registry_info(dataset_id: int, col: str, tags: list, user: dict) -> dict:
    """``{tag: {created_at, created_by, last_used_at}}`` from the tag registry.

    Tags in use that the registry has never seen (ingested with tags, SDK field writes, data that
    predates the registry) are registered on sight with unknown dates, so every listed tag has a
    row and the UI can say "created: unknown" rather than nothing. Bookkeeping never breaks the
    listing: a registry failure logs and returns what it can.
    """
    if not tags:
        return {}
    try:
        await db.execute(
            "INSERT INTO dataset_tags (dataset_id, field_name, tag) SELECT $1, $2, unnest($3::text[]) "
            "ON CONFLICT (dataset_id, field_name, tag) DO NOTHING",
            dataset_id,
            col,
            tags,
        )
        rows = await db.fetch(
            # A tag a follower sent up shows who made it there and on which follower: the follower's name from the
            # account that made it, never from the name it sent (docs/FOLLOWERS.md).
            """SELECT t.tag, t.created_at, t.last_used_at,
                      CASE WHEN f.name IS NOT NULL AND t.created_by_name IS NOT NULL
                           THEN t.created_by_name || ' on ' || f.name ELSE u.username END AS created_by
               FROM dataset_tags t
               LEFT JOIN users u ON u.id = t.created_by_user_id
               LEFT JOIN followers f ON f.user_id = t.created_by_user_id
               WHERE t.dataset_id = $1 AND t.field_name = $2""",
            dataset_id,
            col,
        )
    except Exception as e:
        print(f"Tag registry lookup failed for dataset {dataset_id}/{col}: {e}")
        return {}
    return {
        r['tag']: {
            "created_at": r['created_at'].isoformat() if r['created_at'] else None,
            "created_by": None if private_guest(user) else r['created_by'],
            "last_used_at": r['last_used_at'].isoformat() if r['last_used_at'] else None,
        }
        for r in rows
    }
