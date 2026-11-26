"""
Per-dataset access control.

Centralises the rule for "can this user see / use this dataset" so the same
logic powers list_datasets filtering, get_dataset 403s, and the sample-media
proxy. Lives at the package root (not under routers/) so any module can import
it without dragging in HTTP handlers.

A dataset is shared with single users (dataset_access) and with groups of users
(dataset_group_access), each share at a level: view, tag, edit or manage, each
including the ones before it (LEVELS). A global dataset gives every signed-in
user its everyone level (view, tag or edit).

Access rules (applied in order):
  1. user is None (unauthenticated) → 401. Login is required for ALL access.
  2. user.role == 'admin' → manage, in every dataset.
  3. Otherwise the highest of the user's own share, their groups' shares and,
     when dataset.is_global, its everyone level. A guest gets view at most.
  4. None of those → 403; below the level an action needs → 403 naming it.
dataset.owner_id records who created the dataset and gives no access by itself:
the creator is given an edit share when the dataset is created.

Endpoints should use the `require_dataset_access` FastAPI dependency — one line
declaration, hard to forget, impossible to skip by accident — and
`require_level(...)` for a level above view. A level that depends on the request
(its body, or a row it names) is checked in code marked with `checks_level`.
Every write route declares who may call it; tests/test_route_levels.py checks.

The assistant (docs/ASSISTANT.md): a request a tool makes in process carries its user marked as the assistant's
(identity.assistant_door). For such a request, and only for it, a dataset closed to the assistant (assistant_closed)
is refused by check_dataset_access, after the access check, with ASSISTANT_CLOSED, and left out by
filter_datasets_for_user. An administrator's request through a tool is checked the same way: caller_level doesn't
skip the lookup for it. The same user reads the dataset in the app and through the API as before.
"""

from contextlib import contextmanager
from typing import Any, Callable, Iterator, Literal, Optional, TypeVar

from fastapi import Depends, HTTPException, Request

from .assistant.identity import assistant_door
from .database import db

# The levels of a share, lowest first: each includes the ones before it. Stored as their numbers (1 to 4), so SQL
# takes the highest; the API and the SDK speak the names.
LEVELS = ('view', 'tag', 'edit', 'manage')
Level = Literal['view', 'tag', 'edit', 'manage']
EveryoneLevel = Literal['view', 'tag', 'edit']  # what a global dataset can give every signed-in user
MANAGE = 4

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
        'hashes_by_ids',
        'hash_stats_post',
        'find_duplicates',
        'sample_duplicates',
        'get_vectors_by_ids',
        'aggregate_version',
        'query_version_samples',
        'sparse_sample_route',
        'parse_filter_query',
        'format_filter_query',
    },
)
_DELETING = 'Dataset is being deleted; writes are disabled. Resume deletion to finish.'
ASSISTANT_CLOSED = 'The assistant may not read this dataset'


class LevelRefused(HTTPException):
    """403 below the level an action needs. The caller can open the dataset, so missing_unless_allowed lets it
    through: a row they can see isn't answered as missing."""


class AssistantClosed(LevelRefused):
    """403 for an assistant's request in a dataset closed to the assistant. The user can open the dataset, so a row
    of it (a view, a job) reached through a tool is refused with these words, not answered as missing."""

    def __init__(self) -> None:
        super().__init__(status_code=403, detail=ASSISTANT_CLOSED)


def level_refusal(level: str) -> LevelRefused:
    return LevelRefused(status_code=403, detail=f"This needs {level} access to the dataset")


F = TypeVar('F', bound=Callable[..., Any])


def checks_level(levels: str, *, while_deleting: Optional[str] = None) -> Callable[[F], F]:
    """Marks a handler, or a dependency, that checks the caller's level itself (check_dataset_access with a level, or
    caller_level) because the level depends on the request. `levels` says what it needs, for the route guard and its
    table (tests/test_route_levels.py). `while_deleting` says why the route also runs on a dataset being deleted, for
    a route without require_dataset_access or require_level to refuse it (409)."""

    def mark(function: F) -> F:
        setattr(function, '__fridge_level__', levels)
        setattr(function, '__fridge_while_deleting__', while_deleting)
        return function

    return mark


def level_number(name: str) -> int:
    """A level's stored number: view 1, tag 2, edit 3, manage 4."""
    return LEVELS.index(name) + 1


def level_name(number: int) -> str:
    """A stored level's name."""
    return LEVELS[number - 1]


def _capped(level: int, user: dict) -> int:
    # Guests are view-only everywhere, whatever a share or the everyone level gives.
    return min(level, 1) if user.get('role') == 'guest' else level


async def effective_level(dataset: Any, user: dict) -> Optional[int]:
    """`user`'s level in `dataset` (a row with id, is_global and everyone_level), as a number; None: no access."""
    if user.get('role') == 'admin':
        return MANAGE
    shared = await db.fetchval(
        """
        SELECT max(level) FROM (
            SELECT level FROM dataset_access WHERE dataset_id = $1 AND user_id = $2
            UNION ALL
            SELECT g.level FROM dataset_group_access g JOIN group_members m ON m.group_id = g.group_id
            WHERE g.dataset_id = $1 AND m.user_id = $2
        ) shares
        """,
        dataset['id'],
        user['id'],
    )
    everyone = dataset['everyone_level'] if dataset['is_global'] else None
    levels = [level for level in (shared, everyone) if level is not None]
    return _capped(max(levels), user) if levels else None


async def check_dataset_access(
    dataset_id: int,
    user: Optional[dict],
    level: Level = 'view',
    state: Any = None,
) -> dict:
    """
    Return the dataset row if `user` has at least `level` in dataset `dataset_id`.

    Raises 404 if the dataset doesn't exist, 403 if the user lacks access or has
    a lower level. Returns the row so callers don't have to re-query for fields
    like storage_config_id.

    With a request's `state`, the row is kept on it (state.dataset), and the
    user's level too once it was needed (state.dataset_level), so the next check
    in the same request looks neither up again.
    """
    # Login is required for ALL access — no anonymous reads, even of "public"
    # (is_global) datasets. 401 (not 403) so the frontend redirects to login. Checked BEFORE the
    # lookup: answering 404 first told an anonymous caller which dataset ids exist.
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    needed = level_number(level)

    dataset = getattr(state, 'dataset', None)
    if dataset is None or dataset['id'] != dataset_id:
        row = await db.fetchrow(
            """
            SELECT id, name, storage_config_id, owner_id, is_global, everyone_level, deleting, assistant_closed
            FROM datasets WHERE id = $1
            """,
            dataset_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Dataset not found")
        dataset = dict(row)
        if state is not None:
            state.dataset, state.dataset_level = dataset, None  # None: not looked up yet

    # A global dataset gives every signed-in user view at least, and its everyone level: when that covers the
    # request, no shares query, the hot path for reads.
    if not (dataset['is_global'] and (needed == 1 or _capped(dataset['everyone_level'], user) >= needed)):
        if await _level(dataset, user, state) < needed:
            raise level_refusal(level)
    # After the access check: a user who can't open the dataset learns nothing about it from a tool.
    if dataset.get('assistant_closed') and assistant_door(user):
        raise AssistantClosed()
    return dataset


async def _level(dataset: dict, user: dict, state: Any) -> int:
    have = getattr(state, 'dataset_level', None)
    if have is None:
        have = await effective_level(dataset, user)
        if have is None:
            raise HTTPException(status_code=403, detail="Access denied for this dataset")
        if state is not None:
            state.dataset_level = have
    return have


async def caller_level(dataset_id: int, user: dict, state: Any = None) -> int:
    """The caller's level in a dataset, as a number, raising as check_dataset_access does when they can't open it.
    An administrator's is manage without a lookup, so a caller that must 404 a missing dataset checks that first;
    except through the assistant, which looks the dataset up for the closed-dataset rule."""
    if user.get('role') == 'admin' and not assistant_door(user):
        return MANAGE
    return await _level(await check_dataset_access(dataset_id, user, state=state), user, state)


@contextmanager
def missing_unless_allowed(detail: str) -> Iterator[None]:
    """Around check_dataset_access for a row looked up by its own id (a sample, a view, a job): when the caller may
    not open the row's dataset (403), or it is gone (404), answer 404 `detail`, as for an id that doesn't exist.
    A 403 told a signed-in user which ids exist in datasets they can't open. A dataset's own id keeps its 403, and
    so does a level too low (LevelRefused): that caller can open the dataset and see the row."""
    try:
        yield
    except HTTPException as error:
        if error.status_code not in (403, 404) or isinstance(error, LevelRefused):
            raise
        raise HTTPException(status_code=404, detail=detail) from None


def _path_dataset_id(request: Request, dependency: str) -> int:
    raw = request.path_params.get("id") or request.path_params.get("dataset_id")
    if raw is None:
        raise HTTPException(
            status_code=500,
            detail=f"{dependency}: no 'id' or 'dataset_id' path parameter",
        )
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid dataset id")


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

    dataset_id = _path_dataset_id(request, 'require_dataset_access')
    user = await get_optional_user(request)
    dataset = await check_dataset_access(dataset_id, user, state=request.state)
    _refuse_writes_while_deleting(request, dataset)
    return dataset


def _refuse_writes_while_deleting(request: Request, dataset: dict) -> None:
    endpoint = request.scope.get('endpoint')
    reading = request.method in ('GET', 'HEAD', 'OPTIONS') or (
        request.method == 'POST' and getattr(endpoint, '__name__', '') in _READ_ONLY_DATASET_POSTS
    )
    if dataset.get('deleting') and not reading:
        raise HTTPException(409, _DELETING)


def require_level(level: Level):
    """
    FastAPI dependency factory: the caller, refused (403) below `level` in the
    dataset the route's path names (`{id}` or `{dataset_id}`, as for
    require_dataset_access). It shares the dataset row and the caller's level
    with require_dataset_access on request.state: one lookup per request. A
    write is refused (409) on a dataset being deleted, as there.

    Usage:
        @router.post("/datasets/{id}/samples", dependencies=[Depends(require_dataset_access)])
        async def add_samples(id: int, user: dict = Depends(require_level('edit'))): ...
    """
    # Lazy import to avoid a circular (auth → db pool → access).
    from .routers.auth import get_current_user

    level_number(level)  # an unknown level fails where the route is declared

    @checks_level(level)
    async def check_level(request: Request, user: dict = Depends(get_current_user)) -> dict:
        dataset = await check_dataset_access(_path_dataset_id(request, 'require_level'), user, level, request.state)
        _refuse_writes_while_deleting(request, dataset)
        return user

    return check_level


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
    # Through the assistant, a dataset closed to it is left out.
    closed = " AND NOT assistant_closed" if assistant_door(user) else ""
    if user.get('role') == 'admin':
        return ("NOT assistant_closed" if closed else "TRUE", [])
    return (
        "((is_global = TRUE OR id IN (SELECT dataset_id FROM dataset_access WHERE user_id = $1)"
        " OR id IN (SELECT g.dataset_id FROM dataset_group_access g JOIN group_members m ON m.group_id = g.group_id"
        f" WHERE m.user_id = $1)){closed})",
        [user['id']],
    )


def level_select(user: Optional[dict], param: int) -> tuple[str, list]:
    """
    The user's level in each `datasets` row as SQL (a number; NULL: no access),
    effective_level for a whole list in its one query. Returns (sql, args): the
    sql names the user's id as ${param}, and args holds it when it does. The
    query's outer table must be `datasets`, unaliased: the sql names its columns.

    Example:
        where_sql, where_args = await filter_datasets_for_user(user)
        level_sql, level_args = level_select(user, len(where_args) + 1)
        rows = await db.fetch(f"SELECT ..., {level_sql} FROM datasets WHERE {where_sql}", *where_args, *level_args)
    """
    if user is None:
        return ("NULL", [])
    if user.get('role') == 'admin':
        return (str(MANAGE), [])
    level = (
        "GREATEST(CASE WHEN is_global THEN everyone_level END,"
        f" (SELECT level FROM dataset_access WHERE dataset_id = datasets.id AND user_id = ${param}),"
        " (SELECT max(g.level) FROM dataset_group_access g JOIN group_members m ON m.group_id = g.group_id"
        f" WHERE g.dataset_id = datasets.id AND m.user_id = ${param}))"
    )
    # A guest: view wherever they have any level (LEAST would make a NULL 1).
    if user.get('role') == 'guest':
        level = f"CASE WHEN {level} IS NOT NULL THEN 1 END"
    return (level, [user['id']])
