"""The assistant's call log (assistant_calls, 0037): one row per tool call made through a door, read by administrators
(GET /api/admin/assistant/calls, routers/assistant_calls.py) and kept 30 days.

A row says who, which door, which tool, which dataset when the arguments named one, when it started, how long it took,
and whether it succeeded (the error code when it didn't). Never the arguments, never the result: filter text can hold
serial numbers, and none of it is stored.
"""

from datetime import datetime
from typing import Any

KEEP_DAYS = 30
_INT4_MAX = 2**31 - 1


def named_dataset(arguments: Any) -> int | None:
    """The dataset a call's arguments name (the tools' `dataset` argument), when it is one an id can be."""
    dataset = arguments.get('dataset') if isinstance(arguments, dict) else None
    if isinstance(dataset, int) and not isinstance(dataset, bool) and 0 < dataset <= _INT4_MAX:
        return dataset
    return None


async def record(
    pool,
    *,
    user_id: int,
    door: str,
    tool: str,
    arguments: Any,
    started_at: datetime,
    duration_ms: int,
    error_code: str | None,
) -> None:
    """Log one call. Called after the call returned, so it never holds a connection while a tool runs."""
    async with pool.acquire() as connection:
        await connection.execute(
            'INSERT INTO assistant_calls (user_id, door, tool, dataset_id, started_at, duration_ms, ok, error_code) '
            'VALUES ($1, $2, $3, $4, $5, $6, $7, $8)',
            user_id,
            door,
            tool,
            named_dataset(arguments),
            started_at,
            duration_ms,
            error_code is None,
            error_code,
        )


async def prune(pool) -> int:
    """Remove the rows older than KEEP_DAYS: the job service runs this hourly (job_service.py)."""
    async with pool.acquire(timeout=5) as connection:
        status = await connection.execute(
            "DELETE FROM assistant_calls WHERE started_at < NOW() - $1::int * INTERVAL '1 day'",
            KEEP_DAYS,
        )
    return int(status.split()[-1])


async def page(pool, *, limit: int, offset: int) -> dict[str, Any]:
    """The rows newest first, a page at a time, with the total and where the next page starts (None on the last)."""
    async with pool.acquire() as connection, connection.transaction(isolation='repeatable_read', readonly=True):
        total = await connection.fetchval('SELECT count(*) FROM assistant_calls')
        rows = await connection.fetch(
            'SELECT c.id, c.user_id, u.username, c.door, c.tool, c.dataset_id, c.started_at, c.duration_ms, c.ok, '
            'c.error_code FROM assistant_calls c LEFT JOIN users u ON u.id = c.user_id '
            'ORDER BY c.started_at DESC, c.id DESC LIMIT $1 OFFSET $2',
            limit,
            offset,
        )
    following = offset + len(rows)
    return {
        'calls': [dict(row) for row in rows],
        'total': total,
        'offset': offset,
        'next_offset': following if following < total else None,
    }
