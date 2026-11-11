"""
Per-dataset access control.

Centralises the rule for "can this user see / use this dataset" so the same
logic powers list_datasets filtering, get_dataset 403s, and the sample-media
proxy. Lives at the package root (not under routers/) so any module can import
it without dragging in HTTP handlers.

Access rules (applied in order):
  1. user is None (unauthenticated) → 401. Login is required for ALL access.
  2. dataset.is_global = TRUE → any logged-in user ("public to this instance").
  3. user.role == 'admin' → always allowed.
  4. dataset.owner_id == user.id → allowed.
  5. (dataset_id, user_id) row exists in dataset_access → allowed.
  6. Otherwise → 403.

Endpoints should use the `require_dataset_access` FastAPI dependency — one line
declaration, hard to forget, impossible to skip by accident.
"""

from contextlib import contextmanager
from typing import Iterator, Optional

from fastapi import HTTPException, Request

from .database import db

# These endpoints use POST for large query bodies, not dataset mutations. Keep the
# list explicit: new write endpoints must default to refusing a sealed dataset.
_READ_ONLY_DATASET_POSTS = frozenset(
    {
        'get_dataset_field_stats_post',
        'get_histogram_post',
        'get_histogram_preview_post',
        'autocomplete_field_post',
        'get_dataset_map_points_post',
        'get_dataset_map_extent',
        'create_selection_token',
        'lookup_samples',
        'list_samples_post',
        'aggregate',
        'get_tag_counts',
        'get_embeddings_post',
        'select_region',
        'lookup_coordinates',
        'lookup_patch_coordinates',
        'representativeness_centroid',
        'duplicates_groups',
        'diff_pair_select',
        'diff_current_select',
    },
)


async def check_dataset_access(dataset_id: int, user: Optional[dict]) -> dict:
    """
    Return the dataset row if `user` may access dataset `dataset_id`.

    Raises 404 if the dataset doesn't exist, 403 if the user lacks access.
    Returns the row so callers don't have to re-query for fields like
    storage_config_id.
    """
    # Login is required for ALL access — no anonymous reads, even of "public"
    # (is_global) datasets. 401 (not 403) so the frontend redirects to login. Checked BEFORE the
    # lookup: answering 404 first told an anonymous caller which dataset ids exist.
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    dataset = await db.fetchrow(
        """
        SELECT id, name, storage_config_id, owner_id, is_global, deleting
        FROM datasets WHERE id = $1
        """,
        dataset_id,
    )
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Public datasets: visible to any logged-in user. Fast-path so we skip the
    # dataset_access query entirely on the hot path.
    if dataset['is_global']:
        return dict(dataset)

    if user.get('role') == 'admin':
        return dict(dataset)

    if dataset['owner_id'] is not None and dataset['owner_id'] == user['id']:
        return dict(dataset)

    granted = await db.fetchval(
        """
        SELECT 1 FROM dataset_access
        WHERE dataset_id = $1 AND user_id = $2
        """,
        dataset_id,
        user['id'],
    )
    if granted:
        return dict(dataset)

    raise HTTPException(status_code=403, detail="Access denied for this dataset")


@contextmanager
def missing_unless_allowed(detail: str) -> Iterator[None]:
    """Around check_dataset_access for a row looked up by its own id (a sample, a view, a job): when the caller may
    not open the row's dataset (403), or it is gone (404), answer 404 `detail`, as for an id that doesn't exist.
    A 403 told a signed-in user which ids exist in datasets they can't open. A dataset's own id keeps its 403."""
    try:
        yield
    except HTTPException as error:
        if error.status_code not in (403, 404):
            raise
        raise HTTPException(status_code=404, detail=detail) from None


async def require_dataset_access(request: Request) -> dict:
    """
    FastAPI dependency: verify the caller may access the dataset identified by
    the route's path parameters. Returns the dataset row.

    Works with both naming conventions in this codebase — `{id}` and
    `{dataset_id}` — so existing routers don't need to be renamed. Body-param
    endpoints (e.g. /search with dataset_id in the request body) should call
    `check_dataset_access` inline instead.

    Usage:
        @router.get(
            "/datasets/{id}/samples",
            dependencies=[Depends(require_dataset_access)],
        )
        async def list_samples(id: int, ...): ...
    """
    # Lazy import to avoid a circular (auth → db pool → access).
    from .routers.auth import get_optional_user

    raw = request.path_params.get("id") or request.path_params.get("dataset_id")
    if raw is None:
        raise HTTPException(
            status_code=500,
            detail="require_dataset_access: no 'id' or 'dataset_id' path parameter",
        )
    try:
        dataset_id = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid dataset id")
    user = await get_optional_user(request)
    dataset = await check_dataset_access(dataset_id, user)
    endpoint = request.scope.get('endpoint')
    reading = request.method in ('GET', 'HEAD', 'OPTIONS') or (
        request.method == 'POST' and getattr(endpoint, '__name__', '') in _READ_ONLY_DATASET_POSTS
    )
    if dataset.get('deleting') and not reading:
        raise HTTPException(409, 'Dataset is being deleted; writes are disabled. Resume deletion to finish.')
    return dataset


async def filter_datasets_for_user(user: Optional[dict]) -> tuple[str, list]:
    """
    Build a SQL WHERE-clause fragment (without the leading WHERE) that restricts
    `datasets` rows to those the user may see. Returns (sql, args) — caller
    splices into its own query.

    Example:
        where_sql, where_args = await filter_datasets_for_user(user)
        rows = await db.fetch(f"SELECT ... FROM datasets WHERE {where_sql}", *where_args)
    """
    if user is None:
        # Login required for all access — anonymous callers see no datasets.
        return ("FALSE", [])
    if user.get('role') == 'admin':
        return ("TRUE", [])
    return (
        "(is_global = TRUE OR owner_id = $1 OR id IN (SELECT dataset_id FROM dataset_access WHERE user_id = $1))",
        [user['id']],
    )
