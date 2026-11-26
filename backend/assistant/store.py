"""The assistant's own rows: working views (`wv_...`) and value lists (`vl_...`), each for the user who made it, kept
for 30 days after its last use (migration 0038). Not a dataset's content: nothing here is read by anyone else.

Each function takes one short query (or one transaction) and releases its connection before it returns: no
connection is held while a tool's request to the API runs.
"""

import json
import secrets
from typing import Any, Iterable, Optional

from .. import database

KEPT_DAYS = 30
# The most working views and value lists one user keeps: making one more removes their least recently used, so a
# model in a loop can't fill the server between cleanups. A list a kept view names is never removed this way.
MAX_VIEWS = 2000
MAX_LISTS = 500
# Values a list keeps for filtering; a longer list keeps the filter it was counted over, for its download.
LIST_LIMIT = 10_000
VIEW_GONE = (
    'No working view {handle}: a view is kept for 30 days after its last use, for the person who made it. '
    'Make it again with make_view.'
)
LIST_GONE = (
    'No value list {handle}: a list is kept for 30 days after its last use, for the person who made it. '
    'Ask field_values again.'
)
_FRESH = f"used_at > now() - interval '{KEPT_DAYS} days'"


def new_handle(prefix: str) -> str:
    """A handle nobody can guess: the prefix and 144 random bits."""
    return prefix + secrets.token_urlsafe(18)


def _db(source: Any = None) -> Any:
    return source if source is not None else database.db


async def load_view(user_id: int, handle: str) -> Optional[dict[str, Any]]:
    """The view `handle` of `user_id`, kept and not expired, marked used with the lists its stages name; None
    otherwise (another user's view reads as one that never existed)."""
    if not isinstance(handle, str) or not handle.startswith('wv_'):
        return None
    row = await _db().fetchrow(
        f"""WITH used AS (
                UPDATE assistant_views SET used_at = now()
                WHERE handle = $1 AND user_id = $2 AND {_FRESH}
                RETURNING handle, user_id, dataset_id, stages, lists, from_handle, created_at, used_at
            ), lists AS (
                UPDATE assistant_lists SET used_at = now() WHERE handle IN (SELECT unnest(lists) FROM used) RETURNING 1
            )
            SELECT * FROM used""",
        handle,
        user_id,
    )
    return None if row is None else {**dict(row), 'stages': _json(row['stages'])}


async def view_for_link(handle: str) -> Optional[dict[str, Any]]:
    """Any user's kept view by its handle, for GET /api/assistant/views/{handle} (which says whose it is)."""
    if not isinstance(handle, str) or not handle.startswith('wv_'):
        return None
    row = await _db().fetchrow(
        f"""SELECT handle, user_id, dataset_id, stages, lists, created_at, used_at FROM assistant_views
            WHERE handle = $1 AND {_FRESH}""",
        handle,
    )
    return None if row is None else {**dict(row), 'stages': _json(row['stages'])}


async def touch_view(handle: str) -> None:
    await _db().execute(
        """WITH used AS (UPDATE assistant_views SET used_at = now() WHERE handle = $1 RETURNING lists)
           UPDATE assistant_lists SET used_at = now() WHERE handle IN (SELECT unnest(lists) FROM used)""",
        handle,
    )


async def save_view(
    user_id: int,
    dataset_id: int,
    stages: list[dict[str, Any]],
    from_handle: Optional[str] = None,
) -> str:
    handle = new_handle('wv_')
    lists = sorted({stage['list'] for stage in stages if 'list' in stage})
    await _db().execute(
        """INSERT INTO assistant_views (handle, user_id, dataset_id, stages, lists, from_handle)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6)""",
        handle,
        user_id,
        dataset_id,
        json.dumps(stages),
        lists,
        from_handle,
    )
    # Past the ceiling, the user's least recently used views go (their handles then answer as expired ones).
    await _db().execute(
        """DELETE FROM assistant_views WHERE user_id = $1 AND handle IN (
               SELECT handle FROM assistant_views WHERE user_id = $1
               ORDER BY used_at DESC, created_at DESC, handle OFFSET $2)""",
        user_id,
        MAX_VIEWS,
    )
    return handle


async def load_list(user_id: int, handle: str, touch: bool = True) -> Optional[dict[str, Any]]:
    """The value list `handle` of `user_id`, kept and not expired (marked used); None otherwise."""
    if not isinstance(handle, str) or not handle.startswith('vl_'):
        return None
    row = await _db().fetchrow(
        f"""UPDATE assistant_lists SET used_at = CASE WHEN $3 THEN now() ELSE used_at END
            WHERE handle = $1 AND user_id = $2 AND {_FRESH}
            RETURNING handle, user_id, dataset_id, field_name, value_kind, distinct_count, entries, source, search,
                      created_at, used_at""",
        handle,
        user_id,
        touch,
    )
    if row is None:
        return None
    return {**dict(row), 'entries': _json(row['entries']), 'source': _json(row['source'])}


async def list_for_link(handle: str) -> Optional[dict[str, Any]]:
    """Any user's kept list by its handle, for its download (which checks whose it is, then marks it used)."""
    if not isinstance(handle, str) or not handle.startswith('vl_'):
        return None
    row = await _db().fetchrow(
        f"""SELECT handle, user_id, dataset_id, field_name, value_kind, distinct_count, entries, source
            FROM assistant_lists WHERE handle = $1 AND {_FRESH}""",
        handle,
    )
    return None if row is None else {**dict(row), 'entries': _json(row['entries']), 'source': _json(row['source'])}


async def touch_list(handle: str) -> None:
    await _db().execute('UPDATE assistant_lists SET used_at = now() WHERE handle = $1', handle)


async def list_values(handles: Iterable[str]) -> dict[str, dict[str, Any]]:
    """The kept lists a view's stages name, by handle (their entries and kind), for expanding the stages."""
    rows = await _db().fetch(
        'SELECT handle, field_name, value_kind, distinct_count, entries FROM assistant_lists WHERE handle = ANY($1)',
        sorted(set(handles)),
    )
    return {row['handle']: {**dict(row), 'entries': _json(row['entries'])} for row in rows}


async def save_list(
    user_id: int,
    dataset_id: int,
    field_name: str,
    value_kind: str,
    distinct_count: int,
    entries: Optional[list[list[Any]]],
    source: Optional[dict[str, Any]],
    search: Optional[str],
) -> str:
    """A value list's handle: the same list this user already keeps for the same question (field, search, and the
    same values, or the same filter counted over), marked used, so every page of one question names one list; else a
    new one."""
    entries_json = None if entries is None else json.dumps(entries)
    source_json = None if source is None else json.dumps(source)
    kept = await _db().fetchval(
        f"""UPDATE assistant_lists SET used_at = now() WHERE handle = (
                SELECT handle FROM assistant_lists
                WHERE user_id = $1 AND dataset_id = $2 AND field_name = $3 AND search IS NOT DISTINCT FROM $4
                  AND distinct_count = $5 AND entries IS NOT DISTINCT FROM $6::jsonb
                  AND source IS NOT DISTINCT FROM $7::jsonb AND {_FRESH}
                ORDER BY created_at DESC LIMIT 1)
            RETURNING handle""",
        user_id,
        dataset_id,
        field_name,
        search,
        distinct_count,
        entries_json,
        source_json,
    )
    if kept is not None:
        return str(kept)
    handle = new_handle('vl_')
    await _db().execute(
        """INSERT INTO assistant_lists (handle, user_id, dataset_id, field_name, value_kind, distinct_count, entries,
                                         source, search)
           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9)""",
        handle,
        user_id,
        dataset_id,
        field_name,
        value_kind,
        distinct_count,
        entries_json,
        source_json,
        search,
    )
    # Past the ceiling, the user's least recently used lists go, except the one just made and those a kept view
    # names, which live as long as the view: those count toward the ceiling, and the others make room for them.
    await _db().execute(
        """WITH named AS (SELECT DISTINCT unnest(lists) AS handle FROM assistant_views WHERE user_id = $1),
                kept AS (
                    SELECT count(*) AS n FROM assistant_lists
                    WHERE user_id = $1 AND handle IN (SELECT handle FROM named))
           DELETE FROM assistant_lists WHERE user_id = $1 AND handle IN (
               SELECT handle FROM assistant_lists
               WHERE user_id = $1 AND handle <> $3 AND handle NOT IN (SELECT handle FROM named)
               ORDER BY used_at DESC, created_at DESC, handle
               OFFSET GREATEST(0, $2 - 1 - (SELECT n FROM kept)))""",
        user_id,
        MAX_LISTS,
        handle,
    )
    return handle


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


# A cleanup pass removes at most this many rows of each table, so it never holds a long transaction.
_PRUNE_BATCH = 1000


async def prune(pool: Any) -> int:
    """The job service's housekeeping: remove views unused for 30 days, then lists unused for 30 days that no kept
    view names, a batch per statement until none is left. Answers how many rows went."""
    statements = (
        f"""DELETE FROM assistant_views WHERE handle IN (
            SELECT handle FROM assistant_views WHERE NOT ({_FRESH}) LIMIT {_PRUNE_BATCH})""",
        f"""DELETE FROM assistant_lists WHERE handle IN (
            SELECT handle FROM assistant_lists l WHERE l.used_at <= now() - interval '{KEPT_DAYS} days'
              AND NOT EXISTS (SELECT 1 FROM assistant_views v WHERE v.lists @> ARRAY[l.handle])
            LIMIT {_PRUNE_BATCH})""",
    )
    removed = 0
    async with pool.acquire() as connection:
        for statement in statements:
            while True:
                gone = int((await connection.execute(statement)).split()[-1])
                removed += gone
                if gone < _PRUNE_BATCH:
                    break
    return removed
