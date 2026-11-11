"""Writes of a mirror's own fields, `@local.<name>` (docs/FOLLOWERS.md). Only a mirror that still follows has them,
and a write of them changes nothing else: it runs under fridge.mirror_local, which samples_mirror_local_guard holds to
local_metadata and local_tags, and never touches last_modified_at or modified_by_user_id, which are the primary's."""

from typing import Any, Iterable

from fastapi import HTTPException

from .field_registry import LOCAL_PREFIX, invalidate_values, lock_field_lifecycle, value_type
from .local_fields import LOCAL_TAGS, local_field

NOT_A_MIRROR = "Only a mirror has fields of its own (@local.): on this dataset, add the field itself"
STOPPED = "This mirror no longer follows its primary: it's read-only, its own (@local.) fields included"
MIXED = "A write of this server's own (@local.) fields changes nothing else: send the primary's fields separately"
TAGS_HERE = f"{LOCAL_TAGS} is a list of tags: add or remove them with bulk-tag (field_name {LOCAL_TAGS!r})"


def split_metadata(metadata: dict | None, remove: Iterable[str] | None) -> tuple[dict, list, dict, list]:
    """(local values by name, local names to remove, other metadata, other keys to remove), names without the
    prefix. A bad name after the prefix is a 400; `@local.tags` is the local_tags list, written with bulk-tag."""
    local_set: dict[str, Any] = {}
    local_remove: list[str] = []
    other_set: dict[str, Any] = {}
    other_remove: list[str] = []
    for key, value in (metadata or {}).items():
        name = _local_name(key)
        if name is None:
            other_set[key] = value
        else:
            local_set[name] = value
    for key in remove or []:
        name = _local_name(key)
        if name is None:
            other_remove.append(key)
        else:
            local_remove.append(name)
    return local_set, local_remove, other_set, other_remove


def _local_name(key: str) -> str | None:
    try:
        local = local_field(key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    if local is not None and local.array:
        raise HTTPException(status_code=400, detail=TAGS_HERE)
    return None if local is None else local.name


async def require_mirror(connection, dataset_id: int) -> None:
    """A mirror that still follows its primary, or an HTTP error that says why not."""
    state = await connection.fetchval('SELECT state FROM mirrors WHERE dataset_id = $1', dataset_id)
    if state is None:
        raise HTTPException(status_code=400, detail=NOT_A_MIRROR)
    if state == 'stopped':
        raise HTTPException(status_code=409, detail=STOPPED)


async def begin(connection, dataset_id: int, *, exclusive: bool = False) -> None:
    """Open this transaction to a local write of the mirror: the setting lasts only to its end."""
    await require_mirror(connection, dataset_id)
    await connection.execute("SELECT set_config('fridge.mirror_local', $1, TRUE)", str(dataset_id))
    await lock_field_lifecycle(connection, dataset_id, exclusive=exclusive, wait=not exclusive)


async def register(connection, dataset_id: int, values: list) -> None:
    """Register the local fields these written values name (dicts by name, without the prefix), as metadata fields
    are registered: a new field with the type its values have, and only an unknown type made concrete. (As
    field_registry.infer_fields, without its skip of system names: `@local.media_type` is a field of its own.)
    Every write touches the field's row, so a bulk job's walk that started before it sees a new xmin and leaves the
    field registered (bulk_jobs, unregister). Keep the update unconditional: it costs one dead tuple per write and
    makes two writers of one field wait on its row for a unit, both small on this table, and making it conditional
    reopens that race."""
    kinds: dict[str, str] = {}
    for item in values:
        for name, value in item.items():
            inferred, previous = value_type(value), kinds.get(name, 'unknown')
            if previous == 'unknown':
                kinds[name] = inferred
            elif inferred not in ('unknown', previous):
                kinds[name] = 'string'
    if not kinds:
        return
    names = sorted(kinds)
    await connection.execute(
        """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin)
           SELECT $1, $2 || name, kind, 'local' FROM unnest($3::text[], $4::text[]) AS definitions(name, kind)
           ORDER BY name
           ON CONFLICT (dataset_id, field_name) DO UPDATE SET field_type = CASE
               WHEN dataset_fields.field_type = 'unknown' THEN EXCLUDED.field_type
               ELSE dataset_fields.field_type END""",
        dataset_id,
        LOCAL_PREFIX,
        names,
        [kinds[name] for name in names],
    )


async def register_tags(connection, dataset_id: int, tags: list, user_id: int | None) -> None:
    """`@local.tags` as a field, and the tags added to it in the tag registry, as bulk-tag records any array field's."""
    await connection.execute(
        """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin) VALUES ($1, $2, 'list', 'local')
           ON CONFLICT (dataset_id, field_name) DO NOTHING""",
        dataset_id,
        LOCAL_TAGS,
    )
    if tags:
        await connection.execute(
            """INSERT INTO dataset_tags (dataset_id, field_name, tag, created_by_user_id, last_used_at)
               SELECT $1, $2, unnest($3::text[]), $4, NOW()
               ON CONFLICT (dataset_id, field_name, tag) DO UPDATE SET last_used_at = EXCLUDED.last_used_at""",
            dataset_id,
            LOCAL_TAGS,
            tags,
            user_id,
        )


async def finish(connection, dataset_id: int, written: Iterable[str], removed: Iterable[str] = ()) -> None:
    """Invalidate the fields written or removed (names without the prefix)."""
    names = {LOCAL_PREFIX + name for name in [*written, *removed]}
    if names:
        await invalidate_values(connection, dataset_id, names)


# local_metadata has no index (a local write stays eligible for a HOT update), so whether any sample still holds a
# removed field is read in windows of sample ids, never in one scan.
PRUNE_WINDOW = 20_000
# A request's inline check reads at most this many samples; past that, the field stays registered until a bulk job
# removing it walks the rest in bounded steps (bulk_jobs).
INLINE_PRUNE_ROWS = 100_000


async def holds(connection, dataset_id: int, name: str, after: int) -> tuple[bool, int | None]:
    """Whether a sample in the next window after id `after` holds the field, and that window's last id (None past
    the dataset's last sample)."""
    row = await connection.fetchrow(
        """SELECT COALESCE(bool_or(local_metadata ? $3), FALSE) AS found, max(id) AS last
           FROM (SELECT id, local_metadata FROM samples WHERE dataset_id = $1 AND id > $2 ORDER BY id LIMIT $4) w""",
        dataset_id,
        after,
        name,
        PRUNE_WINDOW,
    )
    return row['found'], row['last']


async def prune(connection, dataset_id: int, removed: Iterable[str]) -> None:
    """Unregister removed fields (names without the prefix) that no sample holds, reading at most INLINE_PRUNE_ROWS
    samples per field; a field not settled within them stays registered. The caller holds the field lifecycle gate
    exclusively, so no write adds one meanwhile."""
    for name in sorted(set(removed)):
        after = 0
        for _ in range(max(1, INLINE_PRUNE_ROWS // PRUNE_WINDOW)):
            found, last = await holds(connection, dataset_id, name, after)
            if found:
                break  # still held: it stays
            if last is None:
                await unregister(connection, dataset_id, name)  # read to the end: no sample holds it
                break
            after = last


async def unregister(connection, dataset_id: int, name: str, mark: str | None = None) -> None:
    """A removed field's definition and statistics. With `mark` (its row's xmin when a walk began), only if no write
    has touched the row since: a sample given the field meanwhile keeps it registered."""
    deleted = await connection.fetchval(
        "DELETE FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2 AND origin = 'local' AND NOT is_promoted "
        'AND ($3::text IS NULL OR xmin::text = $3) RETURNING 1',
        dataset_id,
        LOCAL_PREFIX + name,
        mark,
    )
    if not deleted:
        return
    await connection.execute(
        'DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2',
        dataset_id,
        LOCAL_PREFIX + name,
    )
