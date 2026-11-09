"""Authoritative field definitions; statistics are optional, derived measurements."""

import asyncio
import datetime
import math
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

import asyncpg
from fastapi import HTTPException

from .label_fields import LABEL_CACHES, LEGACY_GEOMETRY_FIELDS, LEGACY_LABEL_FIELDS, label_context, overlay_type

FIELD_TYPES = frozenset({'unknown', 'boolean', 'number', 'string', 'datetime', 'list', 'object'})
SYSTEM_FIELDS = frozenset(
    {
        'tags',
        'media_type',
        'detections.label',
        'ground_truth.label',
        'predictions.label',
        'polygons.label',
        'segmentation.labels',
        '__heatmap_fields__',
        '__probability_fields__',
        '__mask_overlay_fields__',
        '__cuboid_fields__',
        '__keypoint_fields__',
        '__detection_fields__',
        '__polyline_fields__',
        '__classification_fields__',
    },
)
_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}(?:$|[T ])')


def canonical_type(field_type: str) -> str:
    return {
        'bool': 'boolean',
        'int': 'number',
        'integer': 'number',
        'float': 'number',
        'date': 'datetime',
        'array': 'list',
        'dict': 'object',
        'enum': 'string',
    }.get(field_type, field_type if field_type in FIELD_TYPES else 'unknown')


def validate_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Reject non-finite/out-of-float8-range numbers before JSON can silently turn them into null."""

    def visit(value: Any, path: str) -> None:
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"metadata {path}: numbers must be finite and fit in a 64-bit float")
        elif isinstance(value, dict):
            for key, child in value.items():
                visit(child, f'{path}.{key}' if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f'{path}[{index}]')

    visit(metadata, '')
    return metadata


def value_type(value: Any) -> str:
    if value is None:
        return 'unknown'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, list):
        return 'list'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, str) and _ISO_DATE.match(value):
        try:
            datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
            return 'datetime'
        except ValueError:
            pass
    return 'string'


def infer_fields(metadata: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Infer across the entire first write, ignoring nulls; heterogeneous values are strings."""
    fields: dict[str, str] = {}
    for item in metadata:
        for name, value in item.items():
            if name in SYSTEM_FIELDS:
                continue
            inferred = value_type(value)
            previous = fields.get(name, 'unknown')
            if previous == 'unknown':
                fields[name] = inferred
            elif inferred not in ('unknown', previous):
                fields[name] = 'string'
    return fields


async def register_fields(
    connection: Any,
    dataset_id: int,
    metadata: Iterable[Mapping[str, Any]],
    inserted_fields: set[str] | None = None,
) -> dict[str, str]:
    """One set-based registration per write transaction. Only unknown may become concrete.

    Existing concrete definitions are read, not locked or rewritten. Missing/unknown
    definitions are visited in name order, including concurrent first writers.
    Call once after the sample writes, using only successfully written metadata.
    """
    fields = infer_fields(metadata)
    if not fields:
        return {}
    existing = await connection.fetch(
        'SELECT field_name, field_type FROM dataset_fields WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
        dataset_id,
        list(fields),
    )
    declared = {row['field_name']: row['field_type'] for row in existing}
    candidates = sorted(name for name in fields if declared.get(name, 'unknown') == 'unknown')
    if candidates:
        registered = await connection.fetch(
            """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin)
               SELECT $1, name, kind, 'metadata'
               FROM unnest($2::text[], $3::text[]) AS definitions(name, kind) ORDER BY name
               ON CONFLICT (dataset_id, field_name) DO UPDATE SET field_type = EXCLUDED.field_type
               WHERE dataset_fields.field_type = 'unknown' AND EXCLUDED.field_type <> 'unknown'
                 AND dataset_fields.origin = 'metadata'
               RETURNING field_name, xmax = 0 AS inserted""",
            dataset_id,
            candidates,
            [fields[name] for name in candidates],
        )
        if inserted_fields is not None:
            inserted_fields.update(row['field_name'] for row in registered if row['inserted'])
        existing = await connection.fetch(
            'SELECT field_name, field_type FROM dataset_fields WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
            dataset_id,
            list(fields),
        )
        declared = {row['field_name']: row['field_type'] for row in existing}
    return declared


REQUEST_FULL_REFRESH = (
    'UPDATE datasets SET stats_need_refresh = TRUE, stats_full_refreshed_at = NULL, '
    'stats_full_requests = stats_full_requests + 1 WHERE id = $1'
)


# How long an interactive write keeps trying the lifecycle gate before a retryable 409. It polls
# instead of queueing: a queued exclusive request would also stall every writer that arrives after
# it. A sync leaves gaps between its ingest batches (each holds the shared gate only for its own
# transaction), and polling gets in at one of them.
BOUNDED_LIFECYCLE_WAIT_SECONDS = 2.0
_LIFECYCLE_POLL_SECONDS = 0.05


async def lock_field_lifecycle(
    connection: Any,
    dataset_id: int,
    *,
    exclusive: bool = False,
    wait: bool = True,
) -> None:
    """Coordinate definition removal before taking any sample, registry, or dataset row locks.

    ``wait=False`` tries for up to BOUNDED_LIFECYCLE_WAIT_SECONDS without queueing, then raises a
    409 with ``Retry-After``. It is the first statement of its transaction, so nothing was written
    and a retry is safe (the SDK retries these).

    ``wait=True`` (ingest, embedding uploads, tags, clear/rename/revert/set-type) waits as long as the
    holder needs, keeping its place in the queue, except behind a map re-fit (map_refit), which holds
    the gate for minutes: then it answers like ``wait=False``, a fast 409 with the re-fit's end
    (``X-Map-Swap-Seconds``), so an upload never sits on the gate holding a request and a pooled
    connection for the whole swap. A re-fit cannot overtake a writer already queued: it takes the gate
    only with try-locks, which fail against a queued conflicting request, and if a writer queued in the
    instant after it got the gate it lets the writer go first (map_refit._take_gate). In case the writer
    was held up between its look and its queueing (a busy event loop) and a re-fit got the gate
    meanwhile, the first wait is bounded (QUEUE_LOOK_SECONDS) and followed by a second look.
    """
    if wait:
        await _wait_unless_swapping(connection, dataset_id, exclusive)
        return
    await _bounded(connection, dataset_id, exclusive)


# How long a writer queues before it looks for a map re-fit a second time.
QUEUE_LOOK_SECONDS = 1


async def _wait_unless_swapping(connection: Any, dataset_id: int, exclusive: bool) -> None:
    take = 'pg_advisory_xact_lock' if exclusive else 'pg_advisory_xact_lock_shared'
    try_take = 'pg_try_advisory_xact_lock' if exclusive else 'pg_try_advisory_xact_lock_shared'
    if await connection.fetchval(f'SELECT {try_take}(20261016, $1)', dataset_id):
        return
    if await map_swap_holder(connection, dataset_id) is not None:
        await _bounded(connection, dataset_id, exclusive)
        return
    if connection.is_in_transaction():
        try:
            async with connection.transaction():  # a savepoint: a lock timeout leaves the transaction usable
                previous = await connection.fetchval("SELECT current_setting('lock_timeout')")
                await connection.execute(f"SELECT set_config('lock_timeout', '{QUEUE_LOOK_SECONDS}s', true)")
                await connection.execute(f'SELECT {take}(20261016, $1)', dataset_id)
                await connection.execute("SELECT set_config('lock_timeout', $1, true)", previous)
            return
        except asyncpg.LockNotAvailableError:
            pass  # the savepoint's rollback restored lock_timeout
        if await map_swap_holder(connection, dataset_id) is not None:
            await _bounded(connection, dataset_id, exclusive)
            return
    await connection.execute(f'SELECT {take}(20261016, $1)', dataset_id)


async def _bounded(connection: Any, dataset_id: int, exclusive: bool) -> None:
    function = 'pg_try_advisory_xact_lock' if exclusive else 'pg_try_advisory_xact_lock_shared'
    deadline = time.monotonic() + BOUNDED_LIFECYCLE_WAIT_SECONDS
    while not await connection.fetchval(f'SELECT {function}(20261016, $1)', dataset_id):
        if time.monotonic() >= deadline:
            holder = await map_swap_holder(connection, dataset_id)
            if holder is not None:
                raise map_swap_refusal(*holder)
            raise HTTPException(
                status_code=409,
                detail='Dataset has an active field operation or write; retry',
                headers={'Retry-After': '1'},
            )
        await asyncio.sleep(_LIFECYCLE_POLL_SECONDS)


# A map re-fit (backend/map_refit.py) names its session `fridge map <phase> until <deadline>` while it
# holds the dataset's lifecycle gate: "swap", exclusively, while it swaps the coordinates in (every
# write waits); "rank", shared, while it re-ranks the new map (only exclusive writers wait: deletes,
# update-mode ingest, removed keys, field operations). It never holds the gate past the deadline (Unix
# seconds, on the database clock). Writers that cannot get the gate tell their callers how long to retry.
MAP_SWAP_APPLICATION = 'fridge map swap until'
MAP_RANK_APPLICATION = 'fridge map rank until'
MAP_SWAP_HEADER = 'X-Map-Swap-Seconds'


async def map_swap_holder(connection: Any, dataset_id: int) -> tuple[int, str] | None:
    """(seconds until it must end, 'swap' or 'rank') for a map re-fit holding this dataset's gate."""
    # pg_stat_activity is a snapshot taken at its first read in a transaction: clear it, so a writer
    # sees a re-fit that named itself since.
    await connection.execute('SELECT pg_stat_clear_snapshot()')
    holder = await connection.fetchrow(
        """SELECT a.application_name, extract(epoch FROM clock_timestamp())::float8 AS now
           FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
           WHERE l.locktype = 'advisory' AND l.classid = 20261016 AND l.objid = $1 AND l.objsubid = 2
             AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
             AND l.mode IN ('ExclusiveLock', 'ShareLock') AND l.granted
             AND (a.application_name LIKE $2 OR a.application_name LIKE $3)
           ORDER BY l.mode = 'ExclusiveLock' DESC LIMIT 1""",
        dataset_id,
        MAP_SWAP_APPLICATION + ' %',
        MAP_RANK_APPLICATION + ' %',
    )
    if holder is None:
        return None
    try:
        until = float(holder['application_name'].rsplit(' ', 1)[1])
    except ValueError:
        return None
    phase = 'rank' if holder['application_name'].startswith(MAP_RANK_APPLICATION) else 'swap'
    return max(1, math.ceil(until - holder['now'])), phase  # both on the database's clock


async def map_swap_seconds(connection: Any, dataset_id: int) -> int | None:
    """Seconds until the map re-fit holding this dataset's gate must end, or None if none holds it."""
    holder = await map_swap_holder(connection, dataset_id)
    return None if holder is None else holder[0]


def map_swap_refusal(seconds: int, phase: str) -> HTTPException:
    """The 409 for a write that a map re-fit holds back; the SDK retries it until the re-fit's end."""
    if phase == 'rank':
        detail = (
            'The dataset map is being re-ranked after a re-fit; ingest goes on, and deletes, update-mode '
            f'writes and field operations resume within at most {seconds} s. Retry shortly'
        )
    else:
        detail = (
            'The dataset map is being swapped in after a re-fit (usually under a minute per million points); '
            f'writes that change samples resume within at most {seconds} s. Retry shortly'
        )
    return HTTPException(status_code=409, detail=detail, headers={'Retry-After': '5', MAP_SWAP_HEADER: str(seconds)})


def _value_names(names: Iterable[str], label_fields=LEGACY_LABEL_FIELDS, registered=()) -> set[str]:
    affected = set(names)
    affected.update(
        f'{name}.label' for name in list(affected) if name in label_fields and f'{name}.label' not in registered
    )
    if 'segmentation' in affected:
        affected.add('segmentation.labels')
    return affected


async def lock_value_rows(connection: Any, dataset_id: int, names: Iterable[str]) -> None:
    """Lock every cache row a writer will touch in one shared, deterministic order."""
    context = await label_context(connection, dataset_id)
    affected = _value_names(names, context.label_fields, context.registered)
    if affected:
        await connection.fetch(
            'SELECT field_name FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = ANY($2::text[]) '
            'ORDER BY field_name FOR UPDATE',
            dataset_id,
            sorted(affected),
        )


async def invalidate_values(connection: Any, dataset_id: int, names: Iterable[str]) -> None:
    """Invalidate changed fields and derived label lists in the data-write transaction."""
    original_names = set(names)
    context = await label_context(connection, dataset_id)
    affected = _value_names(original_names, context.label_fields, context.registered)
    if affected:
        await lock_value_rows(connection, dataset_id, affected)
        await connection.execute(
            """INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, last_updated)
               SELECT $1, candidate.name, COALESCE(definition.field_type, 'enum'), NULL
               FROM unnest($2::text[]) candidate(name)
               LEFT JOIN dataset_fields definition
                 ON definition.dataset_id = $1 AND definition.field_name = candidate.name
               WHERE definition.field_name IS NOT NULL OR candidate.name IN ('tags', 'media_type')
               ORDER BY candidate.name
               ON CONFLICT (dataset_id, field_name) DO NOTHING""",
            dataset_id,
            sorted(original_names),
        )
        await connection.execute(
            'UPDATE dataset_field_stats SET values_complete = FALSE '
            'WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
            dataset_id,
            sorted(affected),
        )


async def prune_fields(connection: Any, dataset_id: int, names: Iterable[str]) -> None:
    """Remove unused implicit declarations under the exclusive gate with first-match existence probes."""
    candidates = sorted(set(names) - SYSTEM_FIELDS)
    if candidates:
        eligible = await connection.fetch(
            """SELECT field_name FROM dataset_fields WHERE dataset_id = $1
               AND field_name = ANY($2::text[]) AND origin = 'metadata'
               AND NOT is_promoted AND display_type IS NULL AND overlay_config IS NULL
               ORDER BY field_name""",
            dataset_id,
            candidates,
        )
        unused = []
        for field in eligible:
            if (
                await connection.fetchval(
                    'SELECT 1 FROM samples WHERE dataset_id = $1 AND metadata ? $2 LIMIT 1',
                    dataset_id,
                    field['field_name'],
                )
                is None
            ):
                unused.append(field['field_name'])
        if unused:
            await connection.execute(
                'DELETE FROM dataset_fields WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
                dataset_id,
                unused,
            )
            await connection.execute(
                'DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
                dataset_id,
                unused,
            )


async def merge_annotation_values(connection: Any, dataset_id: int, metadata, inserted_fields: set[str]) -> None:
    """Merge effective written labels atomically, preserving rather than inventing completeness."""
    context = await label_context(connection, dataset_id)
    known = context.label_fields | {
        name
        for row in metadata
        for name, value in row.items()
        if isinstance(value, list) and overlay_type(value) in ('detections', 'polygons', 'keypoints', 'classification')
    }
    for parent in sorted({name for row in metadata for name in row}):
        if parent != 'segmentation' and (parent not in known or f'{parent}.label' in context.registered):
            continue
        if parent == 'segmentation':
            raw_labels = [
                value
                for row in metadata
                if isinstance(row.get(parent), dict) and isinstance(row[parent].get('labels'), list)
                for value in row[parent]['labels']
            ]
            field_name = parent + '.labels'
        else:
            raw_labels = [
                item.get('label')
                for row in metadata
                if isinstance(row.get(parent), list)
                for item in row[parent]
                if isinstance(item, dict)
            ]
            field_name = parent + '.label'
        compatible = all(value in (None, '') or isinstance(value, str) for value in raw_labels)
        labels = sorted({value for value in raw_labels if isinstance(value, str) and value})
        if not labels:
            if not compatible:
                await invalidate_values(connection, dataset_id, [parent])
            continue
        await connection.execute(
            """INSERT INTO dataset_field_stats
                   (dataset_id, field_name, field_type, sample_count, unique_values, values_complete)
               VALUES ($1, $2, 'enum', 0, $3, $4)
               ON CONFLICT (dataset_id, field_name) DO UPDATE SET
                   unique_values = ARRAY(SELECT DISTINCT unnest(
                       COALESCE(dataset_field_stats.unique_values, '{}') || $3::text[]) ORDER BY 1 LIMIT 200),
                   values_complete = dataset_field_stats.values_complete AND $4
                       AND (SELECT count(*) FROM (SELECT DISTINCT unnest(
                           COALESCE(dataset_field_stats.unique_values, '{}') || $3::text[]) LIMIT 201) values) <= 200
               """,
            dataset_id,
            field_name,
            labels[:200],
            compatible
            and len(labels) <= 200
            and (
                parent in inserted_fields
                or await connection.fetchval(
                    'SELECT values_complete FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2',
                    dataset_id,
                    field_name,
                )
                is True
            ),
        )


async def _briefly_try_lifecycle(connection: Any, dataset_id: int, function: str) -> bool:
    """Try the lifecycle gate a few times over ~0.2 s: a reader's own placeholder insert holds it for
    only a moment, and the sidebar measures several value lists in parallel."""
    for _ in range(10):
        if await connection.fetchval(f'SELECT {function}(20261016, $1)', dataset_id):
            # The seal takes the exclusive gate, so this check also covers a seal
            # between measurement and publication. Reads may return an uncached preview.
            return await connection.fetchval('SELECT NOT deleting FROM datasets WHERE id=$1', dataset_id) is True
        await asyncio.sleep(0.02)
    return False


async def cached_distinct_values(
    pool: Any,
    dataset_id: int,
    field_name: str,
    query: str,
    args: Iterable[Any],
    cap: int | None = 200,
) -> tuple[list[str], bool]:
    """Publish a bounded preview only if no writer changed its cache during measurement.

    The caller's query returns ``val`` and at most ``cap + 1`` rows, or every
    distinct value when ``cap`` is None. A new
    placeholder commits under a try-only exclusive lifecycle gate so writers cannot
    miss an uncommitted row. A busy gate returns an incomplete uncached preview.
    Measurement holds no row or lifecycle locks. Its
    publication compares the row's MVCC identity, never an increasing checkpoint;
    a competing write makes the returned preview explicitly incomplete.
    """
    async with pool.acquire() as connection:
        cached = await connection.fetchrow(
            'SELECT xmin::text AS token FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2',
            dataset_id,
            field_name,
        )
        if cached is None:
            async with connection.transaction():
                can_publish = await _briefly_try_lifecycle(connection, dataset_id, 'pg_try_advisory_xact_lock')
                if can_publish:
                    await connection.execute(
                        """INSERT INTO dataset_field_stats (dataset_id, field_name, field_type)
                           VALUES ($1, $2, 'enum') ON CONFLICT (dataset_id, field_name) DO NOTHING""",
                        dataset_id,
                        field_name,
                    )
            if can_publish:
                cached = await connection.fetchrow(
                    'SELECT xmin::text AS token FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2',
                    dataset_id,
                    field_name,
                )
        rows = await connection.fetch(query, *args)
        values = [row['val'] for row in rows[:cap] if row['val'] is not None and row['val'] != '']
        complete = cap is None or len(rows) <= cap
        if cached is None:
            return values, False
        async with connection.transaction():
            if not await _briefly_try_lifecycle(connection, dataset_id, 'pg_try_advisory_xact_lock_shared'):
                return values, False
            status = await connection.execute(
                """WITH writable AS (
                       SELECT id FROM dataset_field_stats
                       WHERE dataset_id = $1 AND field_name = $2 AND xmin::text = $5
                       FOR UPDATE SKIP LOCKED
                   )
                   UPDATE dataset_field_stats cache SET unique_values = $3, values_complete = $4,
                       field_type = 'enum', last_updated = now()
                   FROM writable WHERE cache.id = writable.id""",
                dataset_id,
                field_name,
                values,
                complete,
                cached['token'],
            )
        return values, complete and status == 'UPDATE 1'


def presentation_type(field: Mapping[str, Any]) -> str:
    """The single mapping from declared type + display override + completeness to UI type."""
    declared = canonical_type(field['field_type'])
    if field.get('origin') == 'native_array':
        return 'array'
    if field.get('display_type'):
        return field['display_type']
    if declared == 'string' and (
        field.get('is_promoted') or field.get('values_complete') or field.get('unique_values')
    ):
        return 'enum'
    return declared


async def field_stats(connection: Any, dataset_id: int) -> list[dict[str, Any]]:
    """Read declared fields even before measurements exist; never discover from cache keys."""
    rows = await connection.fetch(
        """SELECT f.field_name, f.field_type, f.origin, f.is_promoted, f.display_type,
                  s.sample_count,
                  CASE WHEN field_type_canonical(s.field_type) = f.field_type THEN s.min_value END AS min_value,
                  CASE WHEN field_type_canonical(s.field_type) = f.field_type THEN s.max_value END AS max_value,
                  CASE WHEN f.origin = 'native_array' OR field_type_canonical(s.field_type) = f.field_type
                       THEN s.unique_values END AS unique_values,
                  COALESCE(s.values_complete AND (f.origin = 'native_array'
                      OR field_type_canonical(s.field_type) = f.field_type), FALSE)
                      AS values_complete,
                  (s.id IS NULL OR s.last_updated IS NULL) AND f.origin = 'metadata' AS stats_pending
           FROM dataset_fields f LEFT JOIN dataset_field_stats s
             ON s.dataset_id = f.dataset_id AND s.field_name = f.field_name
           WHERE f.dataset_id = $1 AND f.origin <> 'system'
           UNION ALL
           SELECT s.field_name, field_type_canonical(s.field_type), 'system', FALSE,
                  CASE WHEN s.field_type = 'enum' THEN 'enum' END,
                  s.sample_count, s.min_value, s.max_value, s.unique_values, s.values_complete, FALSE
           FROM dataset_field_stats s WHERE s.dataset_id = $1
             AND NOT EXISTS (SELECT 1 FROM dataset_fields own WHERE own.dataset_id=s.dataset_id
                             AND own.field_name=s.field_name AND own.origin<>'system')
             AND ((s.field_name = ANY($2::text[]) AND s.field_name NOT LIKE '%.label') OR (
                 s.field_name LIKE '%.label' AND (left(s.field_name,-6)=ANY($4::text[]) OR EXISTS (
                     SELECT 1 FROM dataset_field_stats discovery WHERE discovery.dataset_id=s.dataset_id
                     AND discovery.field_name=ANY($3::text[])
                     AND left(s.field_name,-6)=ANY(discovery.unique_values)
                     AND (discovery.field_name<>'__classification_fields__' OR EXISTS (
                         SELECT 1 FROM dataset_fields parent WHERE parent.dataset_id=s.dataset_id
                         AND parent.field_name=left(s.field_name,-6) AND parent.field_type='list'))))))
             AND ((s.field_name NOT LIKE '%.label' AND s.field_name <> 'segmentation.labels')
                  OR EXISTS (SELECT 1 FROM dataset_fields parent
                             WHERE parent.dataset_id = s.dataset_id
                               AND parent.field_name = CASE WHEN s.field_name='segmentation.labels' THEN 'segmentation'
                                                       ELSE left(s.field_name,-6) END
                               AND parent.origin = 'metadata'))
           ORDER BY field_name""",
        dataset_id,
        sorted(SYSTEM_FIELDS),
        list(LABEL_CACHES),
        sorted(LEGACY_GEOMETRY_FIELDS),
    )
    return [dict(row) for row in rows]
