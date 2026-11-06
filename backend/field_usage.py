"""Field usage telemetry for the index advisor.

How often each dataset field is filtered, sorted, faceted or histogrammed, and how long those requests
took. pg_stat_statements is not loaded (adding it needs a database restart), so the API records this
itself:

- Recording is an in-memory counter update in the API worker: no I/O, no await, no database or
  advisory lock. It never raises into, and never waits for anything in, the request it measures.
- Only requests that completed are recorded (a rejected or cancelled one says nothing about speed),
  and only field names promote could index. A request's time is shared equally among the fields it
  uses in one role, so the waiting of a request filtering by three fields is counted once, not three
  times, and a fast field used beside a slow one does not take on the slow one's whole time.
- Each worker adds its counts to `field_usage_daily` once a minute in one statement. The statement keeps
  only fields the dataset defines (a forged or removed name is dropped) and skips deleting datasets.
  A worker that dies loses at most one minute of telemetry.
- Rows older than the review window are pruned once a day, so the table holds at most
  (fields used) x 4 usages x (window + 1) days per dataset.
"""

import asyncio
import contextlib
import json
import logging
import math
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

USAGES = ('filter', 'sort', 'facet', 'histogram')
BUCKETS = 16
# One review period (index_advisor.WINDOW): the advisor reads this many days, including today.
WINDOW_DAYS = 7
FLUSH_SECONDS = float(os.getenv('FRIDGE_USAGE_FLUSH_SECONDS', '60'))
# Comparisons a field's B-tree index serves; `contains`, `exists`, regexes and negations do not count.
INDEXABLE_OPERATORS = frozenset({'=', '==', 'eq', 'in', '<', '<=', '>', '>=', 'lt', 'lte', 'gt', 'gte'})

_Key = Tuple[int, str, str, date]
_lock = threading.Lock()
_pending: Dict[_Key, List[Any]] = {}
_last_prune: Optional[date] = None


def _bucket(milliseconds: float) -> int:
    """k = 0 under 1 ms, else [2^(k-1), 2^k) ms; the last bucket is 2^(BUCKETS-2) ms and more."""
    if not milliseconds >= 1:  # also NaN
        return 0
    return min(BUCKETS - 1, int(math.log2(milliseconds)) + 1)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def indexable_name(name: Any) -> bool:
    """The field names promote accepts (routers/datasets.promote_field); nothing else is recorded."""
    return isinstance(name, str) and bool(name) and name.replace('_', '').replace('-', '').isalnum()


def record(dataset_id: int, usage: str, fields: Iterable[str], seconds: float) -> None:
    """Count one request of `usage` on each field, each with an equal share of its time (no field at all:
    the dataset's '' baseline). Names promote could not index are skipped. Never raises."""
    try:
        if usage not in USAGES:
            return
        given = list(fields)
        names = sorted({name for name in given if indexable_name(name)})
        if given and not names:
            return  # only names nothing could index (a forged or broken one)
        names = names or ['']
        milliseconds = max(0.0, float(seconds) * 1000.0) / len(names)
        bucket = _bucket(milliseconds)
        day = _today()
        with _lock:
            for name in names:
                key = (int(dataset_id), usage, name, day)
                entry = _pending.get(key)
                if entry is None:
                    entry = _pending[key] = [0, 0.0, [0] * BUCKETS]
                entry[0] += 1
                entry[1] += milliseconds
                entry[2][bucket] += 1
    except Exception:  # telemetry must never fail the request it measures
        logger.debug('Field usage telemetry could not record a request', exc_info=True)


def filter_fields(filters: Any) -> List[str]:
    """Top-level metadata fields that a filter tree compares in a way a field index serves."""
    found: set = set()
    try:
        if isinstance(filters, str):
            filters = json.loads(filters) if filters else None

        def walk(node: Any, depth: int) -> None:
            if not isinstance(node, dict) or depth > 64:
                return
            if 'filters' in node or 'logic' in node:
                for child in node.get('filters') or []:
                    walk(child, depth + 1)
                return
            field = node.get('field')
            operator = str(node.get('operator', '=')).lower()
            if isinstance(field, str) and field and '.' not in field and operator in INDEXABLE_OPERATORS:
                found.add(field)

        if isinstance(filters, dict) and ('logic' in filters or 'field' in filters or 'filters' in filters):
            walk(filters, 0)
    except Exception:
        logger.debug('Field usage telemetry could not read a filter', exc_info=True)
    return sorted(found)


@contextlib.contextmanager
def measure(dataset_id: int, usages: Mapping[str, Iterable[str]]) -> Iterator[None]:
    """Time the enclosed request and record it under each usage if it completed (an exception, a 4xx
    refusal included, or a cancellation records nothing and propagates untouched)."""
    started = time.monotonic()
    yield
    try:
        elapsed = time.monotonic() - started
        for usage, fields in usages.items():
            record(dataset_id, usage, fields, elapsed)
    except Exception:
        logger.debug('Field usage telemetry could not record a request', exc_info=True)


def take_pending() -> Dict[_Key, List[Any]]:
    global _pending
    with _lock:
        batch, _pending = _pending, {}
    return batch


_FLUSH = """
INSERT INTO field_usage_daily AS current (dataset_id, usage, field_name, day, uses, total_ms, latency_log2)
SELECT r.dataset_id, r.usage, r.field_name, r.day, r.uses, r.total_ms, r.latency::bigint[]
FROM unnest($1::int[], $2::text[], $3::text[], $4::date[], $5::bigint[], $6::float8[], $7::text[])
    AS r(dataset_id, usage, field_name, day, uses, total_ms, latency)
JOIN datasets d ON d.id = r.dataset_id AND NOT d.deleting
WHERE r.field_name = '' OR EXISTS (
    SELECT 1 FROM dataset_fields f WHERE f.dataset_id = r.dataset_id AND f.field_name = r.field_name)
ORDER BY r.dataset_id, r.usage, r.field_name, r.day
ON CONFLICT (dataset_id, usage, field_name, day) DO UPDATE SET
    uses = current.uses + EXCLUDED.uses,
    total_ms = current.total_ms + EXCLUDED.total_ms,
    latency_log2 = ARRAY(SELECT a + b FROM unnest(current.latency_log2, EXCLUDED.latency_log2)
                         WITH ORDINALITY AS t(a, b, n) ORDER BY n)
"""


async def flush(pool) -> int:
    """Add this worker's counts to the table; returns the rows sent. On failure the counts are dropped
    (and logged): retrying them would let a database outage grow this worker's memory."""
    global _last_prune
    batch = take_pending()
    try:
        async with pool.acquire(timeout=5) as connection:
            if batch:
                keys = sorted(batch)
                await connection.execute(
                    _FLUSH,
                    [key[0] for key in keys],
                    [key[1] for key in keys],
                    [key[2] for key in keys],
                    [key[3] for key in keys],
                    [batch[key][0] for key in keys],
                    [batch[key][1] for key in keys],
                    ['{' + ','.join(str(count) for count in batch[key][2]) + '}' for key in keys],
                    timeout=30,
                )
            today = _today()
            if _last_prune != today:
                await connection.execute(
                    'DELETE FROM field_usage_daily WHERE day <= $1',
                    today - timedelta(days=WINDOW_DAYS),
                    timeout=30,
                )
                _last_prune = today
    except Exception as error:
        logger.warning('Field usage telemetry: dropped %d rows that could not be saved: %s', len(batch), error)
        return 0
    return len(batch)


async def run(pool, stopping: asyncio.Event) -> None:
    """The worker's flush loop; one last flush when the API shuts down."""
    while not stopping.is_set():
        try:
            await asyncio.wait_for(stopping.wait(), FLUSH_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        await flush(pool)
