"""Dataset versioning — create snapshots, view history, compare diffs."""

import asyncio
import contextlib
import copy
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel

from ..access import require_dataset_access
from ..database import db
from ..diskspace import estimate_snapshot_bytes, require_free_space
from ..field_registry import LOCAL_PREFIX, REQUEST_FULL_REFRESH, _value_names, lock_field_lifecycle
from ..label_fields import DISCOVERY_TYPES, LEGACY_LABEL_FIELDS, DatasetArrayColumns, label_context
from ..request_limits import guest_heavy
from ..validation import clean_name
from .auth import require_role
from .datasets import (
    AggregationSpec,
    _derived_field,
    _validate_field_name,
    build_sample_query_parts,
    get_dataset_array_columns,
    run_aggregations,
)

# Router-level dependency → every endpoint under /datasets/{id}/versions/*
# automatically enforces access control. One line, impossible to forget on
# a new endpoint.
router = APIRouter(
    prefix="/datasets/{id}/versions",
    tags=["versions"],
    dependencies=[Depends(require_dataset_access)],
)


class CreateVersionRequest(BaseModel):
    name: str
    description: Optional[str] = None
    include_metadata: bool = True  # False = hash-only (lightweight), True = full snapshot
    # When the version dates from, for history brought in from another system: it is listed, and counted as
    # the latest or not, by this date. With a UTC offset, and not in the future. Default: now.
    created_at: Optional[AwareDatetime] = None
    # Who made the version, for history brought in from another system: its author there. Default: the user
    # making the request; or, for a version given a created_at, no one, since its maker is then unknown.
    # A version given either is recorded as imported by the user making the request (imported_by).
    created_by: Optional[str] = None


async def get_array_columns():
    return {row['column_name'] for row in await db.fetch('SELECT column_name FROM sample_array_columns')}


async def _snapshot_arrays(conn):
    columns = sorted(
        row['column_name']
        for row in await conn.fetch('SELECT column_name FROM sample_array_columns')
        if row['column_name'] != 'tags'
    )
    fields = ', '.join(f"'{column}', s.{column}" for column in columns)
    return columns, f'jsonb_build_object({fields})' if fields else "'{}'::jsonb"


def _array_field_compare_sql(arr_cols):
    """Build (diff_sql, same_sql) fragments comparing CURRENT array columns to a
    version's ``array_fields_snapshot``.

    Only columns the snapshot actually recorded (key present in
    ``array_fields_snapshot``) are compared. This is the fix for: an array field
    ADDED AFTER a snapshot was taken used to make every row diff as 'modified',
    because the snapshot's object had fewer keys than a freshly-built one. Now a
    column the snapshot never tracked is simply ignored.

    Column names come from the array-columns registry (validated identifiers,
    created via create_array_field's _SAFE_COLUMN_RE), so interpolation here is
    as safe as the surrounding ``s.{col}`` interpolation.

    Returns ("", "") when there are no array columns.
    """
    # NB: the jsonb `?` operator returns NULL (not false) when the left side is
    # SQL NULL, so a wholly-NULL array_fields_snapshot (a snapshot taken when no
    # array columns existed) must be guarded explicitly — otherwise the boolean
    # chain goes NULL and silently drops rows from the unchanged tally.
    diff = ''.join(
        f" OR (v.array_fields_snapshot IS NOT NULL AND v.array_fields_snapshot ? '{c}' "
        f"AND v.array_fields_snapshot->'{c}' IS DISTINCT FROM COALESCE(to_jsonb(s.{c}), 'null'::jsonb))"
        for c in arr_cols
    )
    same = ''.join(
        f" AND (v.array_fields_snapshot IS NULL OR NOT (v.array_fields_snapshot ? '{c}') "
        f"OR v.array_fields_snapshot->'{c}' IS NOT DISTINCT FROM COALESCE(to_jsonb(s.{c}), 'null'::jsonb))"
        for c in arr_cols
    )
    return diff, same


# Versions written by this code record every row's media_type, location and heading (migration
# 046, snapshot_format = 2): there a NULL location/heading means the sample HAD none. Older
# snapshots never recorded geo, so their NULL means "unknown" and is skipped
# (a heading added after a modern snapshot survived the revert and vanished from the diff).
SNAPSHOT_FORMAT = 3
EXACT_GEO_FORMAT = 2


def _dataset_snapshot_sql(dataset_param: str) -> str:
    return f"""
        SELECT jsonb_build_object(
            'field_registry_version', 1,
            'name', d.name, 'description', d.description,
            'media_fields', d.media_fields, 'grid_media_field', d.grid_media_field,
            'modal_media_field', d.modal_media_field, 'segmentation_config', d.segmentation_config,
            'fields', COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'field_name', f.field_name, 'field_type', f.field_type,
                    'display_type', f.display_type, 'overlay_config', f.overlay_config, 'origin', f.origin
                ) ORDER BY f.field_name)
                FROM dataset_fields f WHERE f.dataset_id = d.id AND f.origin <> 'system'
            ), '[]'::jsonb)
        ) FROM datasets d WHERE d.id = {dataset_param}
    """


# A view's content as an object: a legacy row stored the JSON text of one (views._decode_content).
_VIEW_CONTENT_SQL = "CASE WHEN jsonb_typeof(w.content) = 'string' THEN (w.content #>> '{}')::jsonb ELSE w.content END"


def _saved_views_sql(dataset_param: str) -> str:
    """The dataset's public saved views as a version records them, by name: [{name, description, content, slug,
    user_id, is_default}].

    A private view is its owner's, and everyone with the dataset can read a version, so only public views are
    recorded. Their names are unique in a dataset. The name, description and content are the view; the slug (its
    link) and owner are kept so a restore can bring back a view that was deleted as it was. is_default marks the
    view the dataset opens on (datasets.default_view_id): a view made again has a new id, so the default is
    recorded with its view, by name.
    """
    return f"""
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'name', w.name, 'description', w.description, 'content', {_VIEW_CONTENT_SQL},
            'slug', w.slug, 'user_id', w.user_id,
            'is_default', COALESCE(w.id = (SELECT default_view_id FROM datasets WHERE id = {dataset_param}), FALSE)
        ) ORDER BY w.name), '[]'::jsonb)
        FROM views w WHERE w.dataset_id = {dataset_param} AND w.is_public
    """


def _version_views_sql(version_param: str) -> str:
    """The saved views a version recorded; NULL when it recorded none (see _view_changes_sql)."""
    return f"SELECT saved_views FROM dataset_versions WHERE id = {version_param}"


def _view_changes_sql(before: str, after: str) -> str:
    """A query for how the saved views of ``after`` differ from those of ``before`` (two queries for recorded
    views): {added, removed, filters_changed}, each the views' names. A view is the same view while it keeps
    its name, and what can differ is its content, the filters.

    NULL when either side recorded no views (a version made before versions recorded them, or a hash-only one):
    nothing is known about its views, so nothing is reported as changed.
    """
    return f"""
        SELECT CASE WHEN b.views IS NULL OR a.views IS NULL THEN NULL ELSE jsonb_build_object(
            'added', (
                SELECT COALESCE(jsonb_agg(new.value->>'name' ORDER BY new.value->>'name'), '[]'::jsonb)
                FROM jsonb_array_elements(a.views) new
                WHERE NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(b.views) old WHERE old.value->>'name' = new.value->>'name'
                )
            ),
            'removed', (
                SELECT COALESCE(jsonb_agg(old.value->>'name' ORDER BY old.value->>'name'), '[]'::jsonb)
                FROM jsonb_array_elements(b.views) old
                WHERE NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(a.views) new WHERE new.value->>'name' = old.value->>'name'
                )
            ),
            'filters_changed', (
                SELECT COALESCE(jsonb_agg(old.value->>'name' ORDER BY old.value->>'name'), '[]'::jsonb)
                FROM jsonb_array_elements(b.views) old
                JOIN jsonb_array_elements(a.views) new ON new.value->>'name' = old.value->>'name'
                WHERE new.value->'content' IS DISTINCT FROM old.value->'content'
            )
        ) END
        FROM (SELECT ({before}) AS views) b, (SELECT ({after}) AS views) a
    """


def _views_differ(changes: Optional[dict]) -> bool:
    """Whether a _view_changes_sql result names any change. Unknown (None) is not a change."""
    return changes is not None and any(changes.values())


# The dataset's row ($1) without the stats_ columns: those are statistics bookkeeping, written by every refresh
# and full-refresh request.
_SETTINGS_SQL = """
    SELECT jsonb_object_agg(key, value) FROM datasets d, jsonb_each(to_jsonb(d))
    WHERE d.id = $1 AND NOT starts_with(key, 'stats_')
"""


async def _hold_unchanged_settings(conn, dataset_id: int, held: contextlib.AsyncExitStack) -> None:
    """Lock the dataset row until `held` closes, on a second connection, and raise SerializationError
    if the row `conn`'s snapshot read (_SETTINGS_SQL) is no longer the committed one.

    Locking the row in `conn`'s own repeatable-read transaction fails on any update since its snapshot
    began, statistics bookkeeping included, and a refresh finishes every few seconds while data comes in.
    """
    guard = await held.enter_async_context(db.pool.acquire())
    await held.enter_async_context(guard.transaction())
    # SHARE waits out a writer in progress; holding it, no update commits before `conn` does.
    await guard.fetchval('SELECT id FROM datasets WHERE id = $1 FOR SHARE', dataset_id)
    if await guard.fetchval(_SETTINGS_SQL, dataset_id) != await conn.fetchval(_SETTINGS_SQL, dataset_id):
        raise asyncpg.SerializationError('Dataset settings changed during the snapshot')


def _media_compare_sql(snapshot='v', other='s', pair=False):
    guard = f"{snapshot}.metadata IS NOT NULL"
    if pair:
        guard += f" AND {other}.metadata IS NOT NULL"
    type_guard = f"{snapshot}.media_type IS NOT NULL"
    if pair:
        type_guard += f" AND {other}.media_type IS NOT NULL"
    different = (
        f"({guard} AND {snapshot}.filepath IS DISTINCT FROM {other}.filepath)"
        f" OR ({type_guard} AND {snapshot}.media_type IS DISTINCT FROM {other}.media_type)"
    )
    return f" OR ({different})", f" AND NOT ({different})"


async def _restore_field_registry(conn, dataset_id: int, version):
    """Restore declared field semantics, retaining definitions needed by non-reverted samples.

    Index placement is current operational state, never restored from a snapshot.
    Restore does not build or drop physical indexes.
    """
    snapshot = version['dataset_snapshot']
    if snapshot is not None:
        normalized = await conn.fetchval('SELECT normalize_dataset_snapshot($1::jsonb)', snapshot)
        fields = normalized['fields']
        await conn.execute(
            """INSERT INTO dataset_fields
                   (dataset_id, field_name, field_type, display_type, overlay_config, origin)
               SELECT $1, field_name, field_type, display_type, overlay_config, origin
               FROM jsonb_to_recordset($2::jsonb) AS definitions(
                   field_name text, field_type text, display_type text, overlay_config jsonb, origin text)
               ORDER BY field_name
               ON CONFLICT (dataset_id, field_name) DO UPDATE SET
                   field_type = EXCLUDED.field_type, display_type = EXCLUDED.display_type,
                   overlay_config = EXCLUDED.overlay_config, origin = EXCLUDED.origin""",
            dataset_id,
            fields,
        )
        await conn.execute(
            """DELETE FROM dataset_fields definition WHERE dataset_id = $1 AND origin = 'metadata'
               AND NOT (field_name = ANY($2::text[]))
               AND NOT EXISTS (SELECT 1 FROM samples WHERE samples.dataset_id = $1
                               AND samples.metadata ? definition.field_name)""",
            dataset_id,
            [field['field_name'] for field in fields],
        )
    if snapshot is None or snapshot.get('field_registry_version') != 1:
        await conn.execute(
            """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin)
               SELECT $1, key,
                   CASE WHEN count(DISTINCT jsonb_typeof(value)) FILTER (WHERE value <> 'null'::jsonb) > 1
                            THEN 'string'
                        WHEN bool_and(jsonb_typeof(value) = 'string'
                            AND value #>> '{}' ~ '^\\d{4}-\\d{2}-\\d{2}([T ]|$)'
                            AND pg_input_is_valid(value #>> '{}', 'timestamp with time zone'))
                            FILTER (WHERE value <> 'null'::jsonb) THEN 'datetime'
                        ELSE field_type_canonical(min(jsonb_typeof(value)) FILTER (WHERE value <> 'null'::jsonb)) END,
                   'metadata'
               FROM version_snapshots snapshot CROSS JOIN LATERAL jsonb_each(snapshot.metadata)
               WHERE snapshot.version_id = $2
                 AND key NOT IN ('tags', 'media_type', 'detections.label', 'ground_truth.label',
                     'predictions.label', 'polygons.label', 'segmentation.labels', '__heatmap_fields__',
                     '__probability_fields__', '__mask_overlay_fields__', '__keypoint_fields__', '__cuboid_fields__',
                     '__classification_fields__',
                     '__detection_fields__', '__polyline_fields__')
               GROUP BY key ORDER BY key ON CONFLICT (dataset_id, field_name) DO NOTHING""",
            dataset_id,
            version['id'],
        )


# The dataset columns a revert can restore from a version's dataset_snapshot, and the cast each one's value takes.
_RESTORED_SETTINGS = {
    'description': '',
    'media_fields': '::text[]',
    'grid_media_field': '',
    'modal_media_field': '',
    'segmentation_config': '',
}


async def _restore_dataset_settings(conn, dataset_id: int, snapshot: dict) -> None:
    """Put the dataset's own settings back as the version recorded them: its description, its media fields and
    which one the grid and a sample's view show, and the segmentation classes and colours.

    Not its name: another dataset may have it now, and links and scripts use the name it has. The fields' types
    and display settings aren't these: every revert restores them (_restore_field_registry). A setting the
    snapshot never recorded stays as it is.
    """
    recorded = [setting for setting in _RESTORED_SETTINGS if setting in snapshot]
    if recorded:
        assignments = ', '.join(
            f'{setting} = ${position}{_RESTORED_SETTINGS[setting]}' for position, setting in enumerate(recorded, 2)
        )
        await conn.execute(
            f'UPDATE datasets SET {assignments} WHERE id = $1',
            dataset_id,
            *(snapshot[setting] for setting in recorded),
        )


async def _restore_default_view(conn, dataset_id: int, version_id: int) -> None:
    """Make the dataset open on the view it opened on when the version was made, or on none if it had none then.
    By name, after _restore_saved_views has put the recorded views back: a view made again has a new id.

    A version that recorded no views says nothing about the default, so it stays as it is.
    """
    recorded = await conn.fetchval(_version_views_sql('$1'), version_id)
    if recorded is None:
        return
    default_name = next((view['name'] for view in recorded if view.get('is_default')), None)
    await conn.execute(
        """UPDATE datasets SET default_view_id = (
               SELECT id FROM views WHERE dataset_id = $1 AND is_public AND name = $2
           ) WHERE id = $1""",
        dataset_id,
        default_name,
    )


async def _restore_saved_views(conn, dataset_id: int, version_id: int, user: dict, exact: bool) -> tuple[int, int]:
    """Put the dataset's public saved views back as the version recorded them; (views restored, views removed).

    A recorded view comes back under its name with its filters and description: a public view of that name is
    given them, and a missing one is made again, with the link and owner it had when the link is still free and
    the owner still a user. ``exact`` also removes the public views the version doesn't have. A version that
    recorded no views leaves the views as they are.

    Private views are never touched. One that holds a recorded view's name therefore stops the restore (409),
    before anything is changed: names are unique in a dataset, and it isn't this restore's to rename or replace.
    """
    recorded = await conn.fetchval(_version_views_sql('$1'), version_id)
    if recorded is None:
        return 0, 0
    rows = await conn.fetch(
        f"""SELECT w.id, w.name, w.description, w.is_public, {_VIEW_CONTENT_SQL} AS content
            FROM views w WHERE w.dataset_id = $1 AND (w.is_public OR w.name_scope = 0)""",
        dataset_id,
    )
    current = {row['name']: row for row in rows}
    private = sorted(
        view['name'] for view in recorded if view['name'] in current and not current[view['name']]['is_public']
    )
    if private:
        raise HTTPException(
            status_code=409,
            detail=f"The version has saved views named {private}, and private views now have those names. "
            "Rename or delete the private ones, then restore again; nothing was changed.",
        )
    owners = {view['user_id'] for view in recorded} | {user['id']}
    known_users = {
        row['id'] for row in await conn.fetch('SELECT id FROM users WHERE id = ANY($1::int[])', list(owners - {None}))
    }
    restored = 0
    for view in recorded:
        existing = current.get(view['name'])
        if existing is None:
            slug = view.get('slug')
            if slug is None or await conn.fetchval('SELECT 1 FROM views WHERE slug = $1', slug):
                slug = secrets.token_urlsafe(8)  # as views.generate_slug
            owner = next(
                (candidate for candidate in (view.get('user_id'), user['id']) if candidate in known_users),
                None,
            )
            await conn.execute(
                """INSERT INTO views (dataset_id, user_id, name, description, slug, content, is_public, name_scope)
                   VALUES ($1, $2, $3, $4, $5, $6, TRUE, 0)""",
                dataset_id,
                owner,
                view['name'],
                view['description'],
                slug,
                view['content'],
            )
            restored += 1
        elif existing['content'] != view['content'] or existing['description'] != view['description']:
            await conn.execute(
                'UPDATE views SET content = $2, description = $3 WHERE id = $1',
                existing['id'],
                view['content'],
                view['description'],
            )
            restored += 1
    removed = 0
    if exact:
        names = {view['name'] for view in recorded}
        added_since = [row['id'] for row in rows if row['is_public'] and row['name'] not in names]
        if added_since:
            await conn.execute('DELETE FROM views WHERE id = ANY($1::int[])', added_since)
        removed = len(added_since)
    return restored, removed


async def _changed_restore_fields(conn, version_id: int, dataset_id: int, arr_cols, changed_sql: str) -> set:
    """Names whose values the restore's update will change: metadata keys, tags, media_type and
    native array columns that differ between a live sample and its row in the version."""
    array_names = ''.join(
        f" UNION ALL SELECT '{col}' WHERE v.array_fields_snapshot ? '{col}' "
        f"AND COALESCE(to_jsonb(s.{col}), 'null'::jsonb) IS DISTINCT FROM v.array_fields_snapshot->'{col}'"
        for col in arr_cols
    )
    rows = await conn.fetch(
        f"""SELECT DISTINCT changed.name FROM samples s
            JOIN version_snapshots v ON v.version_id = $1 AND v.sample_id = s.id
            CROSS JOIN LATERAL (
                SELECT keys.key FROM (
                    SELECT jsonb_object_keys(COALESCE(s.metadata, '{{}}'::jsonb))
                    UNION SELECT jsonb_object_keys(COALESCE(v.metadata, '{{}}'::jsonb))
                ) keys(key)
                WHERE s.metadata->keys.key IS DISTINCT FROM v.metadata->keys.key
                UNION ALL SELECT 'tags' WHERE s.tags IS DISTINCT FROM v.tags
                UNION ALL SELECT 'media_type'
                    WHERE v.media_type IS NOT NULL AND v.media_type IS DISTINCT FROM s.media_type
                {array_names}
            ) changed(name)
            WHERE s.dataset_id = $2 AND {changed_sql}""",
        version_id,
        dataset_id,
        timeout=600,
    )
    return {row['name'] for row in rows}


async def _invalidate_restored_statistics(conn, dataset_id: int, affected=None):
    """Keep stale measurements usable without presenting old categories as complete.

    ``affected`` names the fields whose values or definitions the restore changed (None: all of
    them, as when samples were removed or re-created). Only those lose their value preview and
    completeness; a field the restore did not touch keeps its list. Datetime bounds live in
    unique_values rather than the numeric range columns. Discovery caches stay available until
    statistics republishes them; measurements without a surviving owner are removed.
    """
    await conn.execute(
        """DELETE FROM dataset_field_stats statistics WHERE dataset_id = $1
           AND field_name <> ALL($2::text[])
           AND (field_name = 'media_type'
                OR (field_name <> 'tags'
                    AND NOT EXISTS (
                        SELECT 1 FROM dataset_fields definition
                        WHERE definition.dataset_id = statistics.dataset_id
                          AND definition.field_name = statistics.field_name
                          AND definition.origin <> 'system')
                    AND NOT (
                        (field_name LIKE '%.label' OR field_name = 'segmentation.labels')
                        AND EXISTS (
                            SELECT 1 FROM dataset_fields parent
                            WHERE parent.dataset_id = statistics.dataset_id
                              AND parent.field_name = CASE WHEN statistics.field_name='segmentation.labels'
                                                       THEN 'segmentation' ELSE left(statistics.field_name,-6) END
                              AND parent.origin = 'metadata'))))""",
        dataset_id,
        list(DISCOVERY_TYPES),
    )
    context = await label_context(conn, dataset_id)
    await conn.execute(
        """UPDATE dataset_field_stats statistics SET values_complete = FALSE,
               unique_values = CASE
                   WHEN field_type_canonical(statistics.field_type) = 'datetime'
                        AND EXISTS (
                            SELECT 1 FROM dataset_fields definition
                            WHERE definition.dataset_id = statistics.dataset_id
                              AND definition.field_name = statistics.field_name
                              AND definition.field_type = 'datetime')
                   THEN statistics.unique_values END
           WHERE dataset_id = $1 AND field_name <> ALL($3::text[])
             AND ($2::text[] IS NULL OR field_name = ANY($2::text[]))""",
        dataset_id,
        None if affected is None else sorted(_value_names(affected, context.label_fields, context.registered)),
        list(DISCOVERY_TYPES),
    )


def _pair_geo_compare_sql():
    exact_a = "(SELECT snapshot_format >= 2 FROM dataset_versions WHERE id = $1)"
    exact_b = "(SELECT snapshot_format >= 2 FROM dataset_versions WHERE id = $2)"
    different = (
        f"(({exact_a} OR a.location IS NOT NULL) AND ({exact_b} OR b.location IS NOT NULL)"
        " AND a.location IS DISTINCT FROM b.location)"
        f" OR (({exact_a} OR a.heading IS NOT NULL) AND ({exact_b} OR b.heading IS NOT NULL)"
        " AND a.heading IS DISTINCT FROM b.heading)"
    )
    return f" OR ({different})", f" AND NOT ({different})"


def _geo_compare_sql(exact: bool) -> tuple[str, str]:
    """(differs, same) SQL fragments comparing snapshot `v` with live `s` on location/heading."""
    if exact:
        return (
            " OR v.location IS DISTINCT FROM s.location OR v.heading IS DISTINCT FROM s.heading",
            " AND v.location IS NOT DISTINCT FROM s.location AND v.heading IS NOT DISTINCT FROM s.heading",
        )
    return (
        " OR (v.location IS NOT NULL AND v.location IS DISTINCT FROM s.location)"
        " OR (v.heading IS NOT NULL AND v.heading IS DISTINCT FROM s.heading)",
        " AND (v.location IS NULL OR v.location IS NOT DISTINCT FROM s.location)"
        " AND (v.heading IS NULL OR v.heading IS NOT DISTINCT FROM s.heading)",
    )


async def _exact_geo(version_id: int) -> bool:
    fmt = await db.fetchval("SELECT snapshot_format FROM dataset_versions WHERE id = $1", version_id)
    return (fmt or 1) >= EXACT_GEO_FORMAT


def _tags_compare_sql(snap='v', live='s'):
    """Build (diff_sql, same_sql) fragments comparing a snapshot's ``tags`` column
    to the live ``tags`` column, as an ORDER-INSENSITIVE SET.

    ``tags`` lives in its own snapshot column (NOT array_fields_snapshot) and used
    to be omitted from diff/status entirely — so a tag-only edit was invisible to
    "has unsaved changes" and made revert a silent no-op (it early-returns "already
    at" when status reports no change). Two subtleties handled here:
      * SET comparison (sort both sides): bulk_tag rewrites the column via
        ``array(SELECT DISTINCT unnest(...))`` with no ORDER BY, so the stored
        order is nondeterministic — an ordered compare would phantom-diff.
      * Only compare when the snapshot actually recorded tags, i.e. it's a
        full-metadata snapshot (``{snap}.metadata IS NOT NULL``). Hash-only
        snapshots never stored tags and must not false-positive — same guard
        philosophy as _array_field_compare_sql.
    """
    snap_norm = f"(SELECT array_agg(t ORDER BY t) FROM unnest({snap}.tags) t)"
    live_norm = f"(SELECT array_agg(t ORDER BY t) FROM unnest({live}.tags) t)"
    diff = f" OR ({snap}.metadata IS NOT NULL AND {snap_norm} IS DISTINCT FROM {live_norm})"
    same = f" AND ({snap}.metadata IS NULL OR {snap_norm} IS NOT DISTINCT FROM {live_norm})"
    return diff, same


def _tags_compare_two_snapshots_sql(a='a', b='b'):
    """(diff, same) ``tags`` fragments for SNAPSHOT-vs-SNAPSHOT diffs (both sides are
    version_snapshots rows). Set comparison, applied only when BOTH snapshots
    recorded tags (full-metadata); hash-only snapshots never stored tags.

    The diff/same pair are exact boolean complements over the joined rows, so the
    modified/unchanged counts still partition the "in both" set.
    """
    an = f"(SELECT array_agg(t ORDER BY t) FROM unnest({a}.tags) t)"
    bn = f"(SELECT array_agg(t ORDER BY t) FROM unnest({b}.tags) t)"
    guard = f"{a}.metadata IS NOT NULL AND {b}.metadata IS NOT NULL"
    diff = f" OR ({guard} AND {an} IS DISTINCT FROM {bn})"
    same = f" AND (NOT ({guard}) OR {an} IS NOT DISTINCT FROM {bn})"
    return diff, same


def _array_fields_compare_two_snapshots_sql(a='a', b='b'):
    """(diff, same) array-field fragments for SNAPSHOT-vs-SNAPSHOT diffs.

    Pair diffs used to skip array fields entirely, so an array-only edit between
    two versions counted as unchanged. A column is compared only when BOTH
    snapshots recorded it (key present) — same untracked-facet guard as
    _array_field_compare_sql — and the pair are exact boolean complements, so
    modified/unchanged still partition the "in both" set.
    """
    different = (
        f"EXISTS (SELECT 1 FROM jsonb_each({a}.array_fields_snapshot) AS saved_field "
        f"WHERE {b}.array_fields_snapshot ? saved_field.key "
        f"AND saved_field.value IS DISTINCT FROM {b}.array_fields_snapshot->saved_field.key)"
    )
    return f" OR {different}", f" AND NOT {different}"


@router.get("/status")
async def version_status(id: int):
    async with db.pool.acquire() as conn:
        async with conn.transaction(isolation='repeatable_read'):
            return await _version_status(conn, id)


async def _version_status(conn, id: int):
    """Check which version the dataset currently matches, if any.

    Compares the current live metadata hashes against all version snapshots.
    Returns the matching version (if any) and whether there are unsaved changes.

    Optimized to:
    - Skip versions whose sample count doesn't match (cheap int compare)
    - Use EXISTS instead of COUNT for the diff query (short-circuits on first mismatch)
    - Check both directions (live→snap and snap→live) to catch additions and removals
    """
    versions = await conn.fetch(
        """
        SELECT id, name, sample_count, created_at, include_metadata, snapshot_format, dataset_snapshot
        FROM dataset_versions
        WHERE dataset_id = $1 ORDER BY created_at DESC
    """,
        id,
    )

    if not versions:
        return {"current_version": None, "has_changes": False}

    current_count = await conn.fetchval("SELECT count(*) FROM samples WHERE dataset_id = $1", id)
    current_settings = await conn.fetchval(_dataset_snapshot_sql('$1'), id)
    current_views = _saved_views_sql('$2')
    arr_cols, _ = await _snapshot_arrays(conn)
    for v in versions:
        if v['sample_count'] != current_count:
            continue
        if v['include_metadata'] is False:
            continue
        if v['dataset_snapshot'] is not None and not await conn.fetchval(
            'SELECT dataset_snapshots_equal($1::jsonb, $2::jsonb)',
            v['dataset_snapshot'],
            current_settings,
        ):
            continue
        # A public view added, removed or given other filters since the version is a change too.
        if _views_differ(await conn.fetchval(_view_changes_sql(_version_views_sql('$1'), current_views), v['id'], id)):
            continue
        geo_diff, _ = _geo_compare_sql((v['snapshot_format'] or 1) >= EXACT_GEO_FORMAT)
        media_diff, _ = _media_compare_sql()

        # Build array field comparison

        af_check, _ = _array_field_compare_sql(arr_cols)
        tags_check, _ = _tags_compare_sql('v', 's')

        # EXISTS short-circuits on first mismatch
        has_diff = await conn.fetchval(
            f"""
            SELECT EXISTS (
                SELECT 1 FROM samples s
                LEFT JOIN version_snapshots v ON v.version_id = $2 AND v.sample_id = s.id
                WHERE s.dataset_id = $1
                  AND (v.sample_id IS NULL
                       OR v.metadata_hash != md5(s.metadata::text)
                       OR v.hashes_hash IS DISTINCT FROM (
                           SELECT md5(string_agg(h.field_name || ':' || h.hash_hex, ',' ORDER BY h.field_name))
                           FROM sample_hashes h WHERE h.sample_id = s.id
                       )
                       {geo_diff}{af_check}{tags_check}{media_diff})
            ) OR EXISTS (
                SELECT 1 FROM version_snapshots v
                LEFT JOIN samples s ON s.id = v.sample_id AND s.dataset_id = $1
                WHERE v.version_id = $2 AND s.id IS NULL
            )
        """,
            id,
            v['id'],
        )

        if not has_diff:
            return {
                "current_version": {"id": v['id'], "name": v['name'], "created_at": v['created_at']},
                "has_changes": False,
            }

    latest = versions[0] if versions else None
    return {
        "current_version": None,
        "latest_version": {"id": latest['id'], "name": latest['name']} if latest else None,
        "has_changes": True,
        # What changed about the saved views since the latest version; null when it recorded none.
        "view_changes": await conn.fetchval(
            _view_changes_sql(_version_views_sql('$1'), current_views),
            versions[0]['id'],
            id,
        ),
    }


@router.get("")
async def list_versions(id: int):
    """List all versions for a dataset, newest first."""
    rows = await db.fetch(
        """
        SELECT id, name, description, sample_count, include_metadata, created_at, snapshot_format, created_by,
               imported_by
        FROM dataset_versions
        WHERE dataset_id = $1
        ORDER BY created_at DESC
    """,
        id,
    )
    return [dict(r) for r in rows]


# A full snapshot rolls back if the dataset's row (its settings) changed while it copied, statistics
# bookkeeping aside (see _hold_unchanged_settings). It is taken again after each of these pauses (seconds),
# as a restore is, and reports 409 only when the last attempt fails.
_CREATE_RETRY_DELAYS = (0.1, 0.2)


@router.post("")
async def post_version(id: int, request: CreateVersionRequest, user: dict = Depends(require_role("power_user"))):
    """Create a version, made by the requesting user unless the request says who made it."""
    return await create_version(id, request, user.get('username'))


async def create_version(id: int, request: CreateVersionRequest, made_by: Optional[str] = None):
    """Capture a consistent snapshot, or report a retryable concurrent-write conflict. ``made_by`` is recorded
    as who made it, unless the request says who did."""
    for delay in (*_CREATE_RETRY_DELAYS, None):
        try:
            return await _create_version(id, request, made_by)
        except asyncpg.SerializationError:
            if delay is None:
                # Rolled back, nothing kept: a retryable refusal, marked with Retry-After like the others.
                raise HTTPException(
                    status_code=409,
                    detail="Dataset changed during snapshot creation; retry after the concurrent writer finishes",
                    headers={'Retry-After': '1'},
                )
            await asyncio.sleep(delay)


async def _create_version(id: int, request: CreateVersionRequest, made_by: Optional[str] = None):
    """Create a new version snapshot of the current dataset state.

    Full snapshots capture sample data and dataset display settings atomically.
    Hash-only records support limited comparisons, not historical reads.
    """
    # "" and "   " used to be accepted.
    request.name = clean_name(request.name, "Version name")
    if request.created_at is not None and request.created_at > datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="created_at is in the future; a version dates from now or before")
    # A date or an author that the request states is a claim; who made the request is recorded beside it.
    imported_by = made_by if request.created_at is not None or request.created_by is not None else None
    if request.created_by is not None:
        made_by = clean_name(request.created_by, "created_by")
    elif request.created_at is not None:
        # History from another system that names no author: who made it is unknown, not whoever brings it in.
        made_by = None

    # Check dataset exists
    ds = await db.fetchrow("SELECT id FROM datasets WHERE id = $1", id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Count, version row, and snapshot rows run in ONE repeatable-read transaction
    # so they see the same MVCC snapshot. Otherwise concurrent ingestion between
    # the COUNT and the INSERT...SELECT would record a sample_count that disagrees
    # with the number of snapshot rows actually captured.
    async with db.pool.acquire() as conn, contextlib.AsyncExitStack() as held:
        if request.include_metadata:
            # A full snapshot is a multi-GB single transaction on a big dataset: refuse (507)
            # rather than let it fill the volume mid-write (Postgres would PANIC).
            await require_free_space(conn, await estimate_snapshot_bytes(conn, id), f"a full snapshot of dataset {id}")
        async with conn.transaction(isolation='repeatable_read'):
            count = await conn.fetchval("SELECT count(*) FROM samples WHERE dataset_id = $1", id)
            _, af_expr = await _snapshot_arrays(conn)
            # Version names are unique per dataset (dataset_versions_dataset_id_name_key). A
            # duplicate is the caller's mistake, so it's a 409 — not an unhandled 500. Raising
            # here exits the transaction with an exception, so it rolls back.
            try:
                version = await conn.fetchrow(
                    f"""
                    INSERT INTO dataset_versions (dataset_id, name, description, sample_count, include_metadata,
                                                  snapshot_format, dataset_snapshot, saved_views, created_at,
                                                  created_by, imported_by)
                    VALUES ($1, $2, $3, $4, $5, CASE WHEN $5 THEN $6::smallint ELSE 1::smallint END,
                            CASE WHEN $5 THEN ({_dataset_snapshot_sql('$1')}) ELSE NULL END,
                            CASE WHEN $5 THEN ({_saved_views_sql('$1')}) ELSE NULL END,
                            COALESCE($7::timestamptz, CURRENT_TIMESTAMP), $8, $9)
                    RETURNING id, name, description, sample_count, include_metadata, created_at, created_by,
                              imported_by, jsonb_array_length(saved_views) AS saved_view_count
                """,
                    id,
                    request.name,
                    request.description,
                    count,
                    request.include_metadata,
                    SNAPSHOT_FORMAT,
                    request.created_at,
                    made_by,
                    imported_by,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    status_code=409,
                    detail=f"A version named {request.name!r} already exists for this dataset",
                )
            version_id = version['id']

            if request.include_metadata:
                await conn.execute(
                    f"""
                    INSERT INTO version_snapshots (version_id, sample_id, metadata_hash, hashes_hash, metadata, tags, filepath, location, heading, array_fields_snapshot, media_type, created_at, last_modified_at)
                    SELECT $1, s.id, md5(s.metadata::text),
                        (SELECT md5(string_agg(h.field_name || ':' || h.hash_hex, ',' ORDER BY h.field_name))
                         FROM sample_hashes h WHERE h.sample_id = s.id),
                        s.metadata, s.tags, s.filepath, s.location, s.heading,
                        {af_expr}, s.media_type, s.created_at, s.last_modified_at
                    FROM samples s WHERE s.dataset_id = $2
                """,  # noqa: E501
                    version_id,
                    id,
                    timeout=600,
                )
            else:
                await conn.execute(
                    f"""
                    INSERT INTO version_snapshots (version_id, sample_id, metadata_hash, hashes_hash, array_fields_snapshot)
                    SELECT $1, s.id, md5(s.metadata::text),
                        (SELECT md5(string_agg(h.field_name || ':' || h.hash_hex, ',' ORDER BY h.field_name))
                         FROM sample_hashes h WHERE h.sample_id = s.id),
                        {af_expr}
                    FROM samples s WHERE s.dataset_id = $2
                """,  # noqa: E501
                    version_id,
                    id,
                    timeout=600,
                )

            if request.include_metadata:
                # Check settings again just before commit (the FK's KEY SHARE lock doesn't detect renames
                # with the partial active-name unique index), without locking them during the large copy.
                # The lock is held until `held` closes, after this transaction commits.
                await _hold_unchanged_settings(conn, id, held)

    result = dict(version)
    result['include_metadata'] = request.include_metadata
    return result


@router.get("/{version_id}")
async def get_version(id: int, version_id: int):
    """Get details of a specific version."""
    row = await db.fetchrow(
        """
        SELECT id, name, description, sample_count, include_metadata, created_at, snapshot_format, dataset_snapshot,
               created_by, imported_by
        FROM dataset_versions
        WHERE id = $1 AND dataset_id = $2
    """,
        version_id,
        id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")
    result = dict(row)
    if result.get('dataset_snapshot'):
        snapshot = dict(result['dataset_snapshot'])
        snapshot['fields'] = [
            {key: value for key, value in field.items() if key != 'is_promoted'} for field in snapshot.get('fields', [])
        ]
        result['dataset_snapshot'] = snapshot
    return result


@router.get("/{version_id}/views")
async def get_version_views(id: int, version_id: int):
    """The saved views a version recorded: the name, description and content of each of the dataset's public
    views when it was made. A view's filters run on the version through the version's sample queries.

    ``views`` is null for a version that recorded none: one made before versions recorded views, or a hash-only
    one. That is not the same as a version of a dataset that had no public views, which has an empty list.
    """
    row = await db.fetchrow(
        "SELECT saved_views FROM dataset_versions WHERE id = $1 AND dataset_id = $2",
        version_id,
        id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")
    return {"views": row['saved_views']}


async def _recorded_view_filter(id: int, version_id: int, name: str) -> Optional[dict]:
    """The filter of a saved view the version recorded, as the app applies a view: the AND of its stages' filters,
    or the compiled filters of a view saved before views had stages. None for a view of every sample.

    404 when the version recorded no view of that name; 400 for a view that holds no usable filter.
    """
    recorded = (await get_version_views(id, version_id))['views'] or []
    content = next((view['content'] for view in recorded if view['name'] == name), None)
    if content is None:
        raise HTTPException(status_code=404, detail=f"This version recorded no saved view named {name!r}")
    stages = content.get('viewStages') if isinstance(content, dict) else None
    if isinstance(stages, list):
        filters = [stage.get('filter') if isinstance(stage, dict) else None for stage in stages]
        if all(isinstance(stage_filter, dict) and stage_filter for stage_filter in filters):
            return {'logic': 'AND', 'filters': filters} if filters else None
    elif isinstance(content, dict) and isinstance(content.get('filters'), dict):
        return content['filters'] or None
    raise HTTPException(
        status_code=400,
        detail=f"Saved view {name!r} has a stage without a filter, so it can't be shown on a version",
    )


def _attach_location(data: dict) -> None:
    """Replace a row's lon/lat columns with the GeoJSON-style ``location`` the samples endpoints return."""
    if data.get('lon') is not None and data.get('lat') is not None:
        data['location'] = {"type": "Point", "coordinates": [data['lon'], data['lat']]}
    else:
        data['location'] = None
    data.pop('lon', None)
    data.pop('lat', None)
    if data.get('heading') is not None and data.get('location'):
        data['location']['heading'] = data['heading']


def _full_snapshot_item(row, version_id: int) -> dict:
    """One stored version row in the samples-endpoint shape, with the version's metadata, tags and arrays."""
    data = dict(row)
    if data['metadata'] is None or data['filepath'] is None:
        raise HTTPException(status_code=400, detail="This older version did not capture historical sample data")
    data['version_id'] = version_id
    data['media_type'] = data.get('media_type') or 'unknown'
    data.update({name: value for name, value in (data.get('array_fields') or {}).items() if name not in data})
    _attach_location(data)
    # filepath stays raw — see /api/samples/{id}/media for image fetching.
    return data


@router.get("/{version_id}/samples")
async def get_version_samples(
    id: int,
    version_id: int,
    limit: int = 100,
    offset: int = 0,
    full: bool = False,
    q: str = '',
    after_id: Optional[int] = None,
    change_type: Optional[str] = None,
    diff_base: Optional[int] = None,
    diff_target: Optional[int] = None,
    view: Optional[str] = None,
):
    """Get samples from this version.

    full=False: just sample IDs (fast)
    full=True: returns samples in the same format as the main samples
    endpoint, but with snapshotted metadata/tags from version time.
    Filepaths are resolved through the storage backend.
    view: only the samples of this saved view, one the version recorded (GET .../views), as it held them then.
    """
    # IDOR guard: the version must belong to THIS dataset. Every sibling endpoint
    # verifies this; without it a caller with access to any (e.g. global) dataset
    # could read another dataset's snapshotted metadata/tags/filepaths by guessing
    # a version_id.
    version = await get_version(id, version_id)
    if not 1 <= limit <= 10000 or offset < 0 or (after_id is not None and after_id < 0):
        raise HTTPException(status_code=400, detail="Invalid snapshot pagination")
    if len(q) > 1000:
        raise HTTPException(status_code=400, detail="Search must be at most 1000 characters")
    if full and version['include_metadata'] is False:
        raise HTTPException(status_code=400, detail="Hash-only versions have no historical sample data")
    args: list[Any] = [version_id]
    conditions = ["v.version_id = $1"]
    if change_type is not None:
        base = diff_base if diff_base is not None else version_id
        await _verify_versions(id, base)
        if diff_target is not None:
            await _verify_versions(id, diff_target)
            selection = await _pair_diff_select_sql(change_type)
            args.extend([base, diff_target])
        else:
            selection = await _current_diff_select_sql(change_type, await _exact_geo(base))
            args.extend([base, id])
        selection = selection.replace('$2', '$3').replace('$1', '$2')
        conditions.append(f"v.sample_id IN ({selection})")
    if view is not None:
        view_filter = await _recorded_view_filter(id, version_id, view)
        if view_filter is not None:
            # The view's own query on the version (its CTE and filter), with its parameters after this one's.
            with_clause, view_where, view_args, _, _ = await _version_query(id, version_id, view_filter)
            shift = len(args)
            view_where = re.sub(r'\$(\d+)', lambda match: f'${int(match.group(1)) + shift}', view_where)
            conditions.append(f"v.sample_id IN ({with_clause}SELECT id FROM samples {view_where})")
            args.extend(view_args)
    if q:
        args.append('%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        conditions.append(
            f"(v.filepath ILIKE ${len(args)} OR v.metadata::text ILIKE ${len(args)}"
            f" OR array_to_string(v.tags, ' ') ILIKE ${len(args)})",
        )
    where = ' AND '.join(conditions)
    total = version['sample_count']
    if q or change_type is not None or view is not None:
        total = await db.fetchval(f"SELECT count(*) FROM version_snapshots v WHERE {where}", *args)
    if after_id is not None:
        args.append(after_id)
        where += f" AND v.sample_id > ${len(args)}"
    args.extend([limit, offset])
    paging = f"ORDER BY v.sample_id LIMIT ${len(args) - 1} OFFSET ${len(args)}"
    if full:
        rows = await db.fetch(
            f"""
            SELECT v.sample_id as id, v.filepath, v.metadata, v.tags,
                   v.media_type, v.array_fields_snapshot as array_fields,
                   ST_X(v.location::geometry) as lon,
                   ST_Y(v.location::geometry) as lat,
                   v.heading, v.created_at, v.last_modified_at
            FROM version_snapshots v
            WHERE {where} {paging}
        """,
            *args,
        )

        items = [_full_snapshot_item(row, version_id) for row in rows]

        return {"items": items, "total": total, "snapshot_format": version['snapshot_format']}
    else:
        rows = await db.fetch(
            f"""
            SELECT v.sample_id FROM version_snapshots v
            WHERE {where} {paging}
        """,
            *args,
        )
        return {"sample_ids": [r['sample_id'] for r in rows], "total": total}


# Samples-endpoint operators that read live tables (duplicates, concept scores, parent/object links) a
# version does not snapshot. Refused rather than answered from today's data.
_LIVE_ONLY_FILTER_OPS = frozenset(
    {
        'collapse_duplicates',
        'concept_select',
        'concept_exclude',
        'concept_present',
        'cg_self',
        'parent_matches',
        'has_matching_object',
    },
)
_VERSION_COLUMN_FIELDS = ('filepath', 'media_type', 'tags', 'created_at', 'last_modified_at')


def _reject_live_only_filters(node: Any) -> None:
    if not isinstance(node, dict):
        return
    for child in node.get('filters') or []:
        _reject_live_only_filters(child)
    if 'field' not in node:
        return
    operator = str(node.get('operator', '=')).lower()
    field = node['field']
    if isinstance(field, str) and field.startswith(LOCAL_PREFIX):
        # A version keeps no history of a mirror's own fields, by decision (docs/FOLLOWERS.md).
        raise HTTPException(
            status_code=400,
            detail=f"Filter on {field!r}: a version keeps no history of this server's own (@local.) fields",
        )
    derived = isinstance(field, str) and field != 'location' and _derived_field(field) is not None
    if operator in _LIVE_ONLY_FILTER_OPS or derived:
        raise HTTPException(
            status_code=400,
            detail=f"Filter on {field!r} ({operator}) reads live data that a version does not store",
        )


async def _queryable_version(id: int, version_id: int) -> dict:
    """The version; 404 unless it belongs to this dataset, 400 if it kept no sample data (hash-only)."""
    version = await get_version(id, version_id)
    if version['include_metadata'] is False:
        raise HTTPException(status_code=400, detail="Hash-only versions have no historical sample data")
    return version


async def _version_columns(id: int, version: dict) -> DatasetArrayColumns:
    """The field context a version's rows are read with: the fields the version stored, not today's.

    Which dotted paths are list elements (``animals.label``) depends on the declared fields. Taken
    from the live registry, a version's answer changed when the live dataset did: clearing a list
    field, or the next statistics refresh after its last value went, made the same query on an
    unchanged version match nothing. Only the declarations are the version's: the physical array
    columns (the snapshot's stored arrays are rebuilt into them) and anything else the context holds
    about the dataset as it is now stay the live context's. A snapshot from before the field registry
    (no ``field_registry_version``) kept no reliable field list and reads with the live one, as before.
    """
    live = await get_dataset_array_columns(id)
    snapshot = version.get('dataset_snapshot') or {}
    if snapshot.get('field_registry_version') != 1:
        return live
    fields = snapshot.get('fields') or []
    columns = copy.copy(live)
    columns.label_fields = set(LEGACY_LABEL_FIELDS)
    columns.registered = {field['field_name'] for field in fields}
    columns.list_fields = {field['field_name'] for field in fields if field.get('field_type') == 'list'}
    return columns


async def _version_query(
    id: int,
    version_id: int,
    filters: Optional[dict],
) -> tuple[str, str, list, list, DatasetArrayColumns]:
    """(WITH clause, WHERE clause, args, stored array columns, field context) to query a version's rows like
    the live table.

    The WITH clause names a CTE ``samples`` holding the version's rows under the live column names, so
    the live filter compiler and aggregations run on it unchanged: ``id`` is the sample id,
    ``dataset_id`` the dataset, and each registered array field is rebuilt from the snapshot's
    ``array_fields_snapshot`` (NULL where the version never recorded it). The CTE is referenced once per
    statement, so PostgreSQL inlines it and only computes the columns a query touches. Label paths are
    read with the version's own fields (_version_columns).
    """
    version = await _queryable_version(id, version_id)
    if filters:
        _reject_live_only_filters(filters)
    array_columns = await _version_columns(id, version)
    stored = [c for c in sorted(array_columns & await get_array_columns()) if c != 'tags']
    rebuilt = ''.join(
        f", CASE WHEN jsonb_typeof(v.array_fields_snapshot->'{c}') = 'array' "
        f"THEN ARRAY(SELECT jsonb_array_elements_text(v.array_fields_snapshot->'{c}')) END AS {c}"
        for c in stored
    )
    with_clause = (
        f"WITH samples AS (SELECT v.sample_id AS id, {int(id)} AS dataset_id, v.filepath, v.metadata, v.tags, "
        f"v.media_type, v.location, v.heading, v.created_at, v.last_modified_at, v.array_fields_snapshot{rebuilt} "
        f"FROM version_snapshots v WHERE v.version_id = {int(version_id)}) "
    )
    where, args, _ = build_sample_query_parts(
        id,
        filters=json.dumps(filters) if filters else None,
        array_columns=array_columns,
    )
    return with_clause, where, args, stored, array_columns


class VersionAggregateRequest(BaseModel):
    aggregations: list[AggregationSpec]
    filters: Optional[dict[str, Any]] = None


@router.post("/{version_id}/aggregate")
@guest_heavy
async def aggregate_version(id: int, version_id: int, request: VersionAggregateRequest):
    """Aggregate a version's stored samples, optionally filtered; same specs and results as
    ``POST /datasets/{id}/aggregate``, computed in the database against the version."""
    with_clause, where, args, _, columns = await _version_query(id, version_id, request.filters)
    return await run_aggregations(id, request.aggregations, where, args, with_clause, array_columns=columns)


class VersionSamplesQueryRequest(BaseModel):
    filters: Optional[dict[str, Any]] = None
    fields: Optional[list[str]] = None  # None = whole rows; else only these (id is always returned)
    limit: int = 1000
    after_id: Optional[int] = None  # keyset cursor: rows with id > after_id, ordered by id


@router.post("/{version_id}/samples/query")
@guest_heavy
async def query_version_samples(id: int, version_id: int, request: VersionSamplesQueryRequest):
    """A page of a version's samples matching ``filters``, as they were at the version, ordered by id.

    ``fields`` limits the columns read: native columns, registered array fields, or metadata keys
    (a key absent from a sample's stored metadata is absent from its ``metadata``).
    """
    if not 1 <= request.limit <= 10000 or (request.after_id is not None and request.after_id < 0):
        raise HTTPException(status_code=400, detail="Invalid snapshot pagination")
    with_clause, where, args, stored, _ = await _version_query(id, version_id, request.filters)
    if request.after_id is not None:
        args.append(request.after_id)
        where += f" AND id > ${len(args)}"
    args.append(request.limit)
    paging = f"ORDER BY id LIMIT ${len(args)}"

    if request.fields is None:
        columns = (
            "id, filepath, metadata, tags, media_type, array_fields_snapshot AS array_fields, "
            "ST_X(location::geometry) AS lon, ST_Y(location::geometry) AS lat, heading, created_at, last_modified_at"
        )
    else:
        select = ['id', '(metadata IS NULL OR filepath IS NULL) AS incomplete']
        metadata_keys = []
        for field in dict.fromkeys(request.fields):
            if field == 'location':
                select += ['ST_X(location::geometry) AS lon', 'ST_Y(location::geometry) AS lat', 'heading']
            elif field == 'heading':
                select.append('heading')
            elif field == 'metadata':
                select.append('metadata')
            elif field in _VERSION_COLUMN_FIELDS or field in stored:
                select.append(field)
            elif field != 'id':
                metadata_keys.append(_validate_field_name(field.split('.')[0]))
        if metadata_keys:
            args.append(list(dict.fromkeys(metadata_keys)))
            select.append(
                "(SELECT COALESCE(jsonb_object_agg(e.key, e.value), '{}'::jsonb) FROM jsonb_each(metadata) e "
                f"WHERE e.key = ANY(${len(args)}::text[])) AS metadata",
            )
        columns = ', '.join(dict.fromkeys(select))

    rows = await db.fetch(f"{with_clause}SELECT {columns} FROM samples {where} {paging}", *args)

    if request.fields is None:
        return {"items": [_full_snapshot_item(row, version_id) for row in rows]}
    items = []
    for row in rows:
        data = dict(row)
        if data.pop('incomplete'):
            raise HTTPException(status_code=400, detail="This older version did not capture historical sample data")
        data['version_id'] = version_id
        if 'media_type' in data:
            data['media_type'] = data['media_type'] or 'unknown'
        if 'lon' in data:
            _attach_location(data)
        items.append(data)
    return {"items": items}


# An admin: a deleted version can't be brought back, and it is the record of what the dataset was.
@router.delete("/{version_id}", dependencies=[Depends(require_role("admin"))])
async def delete_version(id: int, version_id: int):
    """Delete a version and its snapshot data."""
    row = await db.fetchrow(
        "DELETE FROM dataset_versions WHERE id = $1 AND dataset_id = $2 RETURNING id",
        version_id,
        id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")
    return {"message": "Version deleted"}


@router.get("/diff/{version_a}/{version_b}")
async def diff_versions(id: int, version_a: int, version_b: int):
    async with db.pool.acquire() as conn:
        async with conn.transaction(isolation='repeatable_read'):
            return await _diff_versions(conn, id, version_a, version_b)


async def _diff_versions(conn, id: int, version_a: int, version_b: int):
    """Compare two versions and return the diff.

    Returns:
    - added: samples in version_b but not in version_a
    - removed: samples in version_a but not in version_b
    - modified: samples in both but with different metadata
    - unchanged: samples in both with same metadata
    """
    # Verify both versions belong to this dataset
    for vid in [version_a, version_b]:
        exists = await conn.fetchval("SELECT 1 FROM dataset_versions WHERE id = $1 AND dataset_id = $2", vid, id)
        if not exists:
            raise HTTPException(status_code=404, detail=f"Version {vid} not found")

    tags_ab_diff, tags_ab_same = _tags_compare_two_snapshots_sql('a', 'b')
    af_ab_diff, af_ab_same = _array_fields_compare_two_snapshots_sql('a', 'b')
    geo_diff, geo_same = _pair_geo_compare_sql()
    media_diff, media_same = _media_compare_sql('a', 'b', pair=True)

    # Count added (in B but not A)
    added = await conn.fetchval(
        """
        SELECT count(*) FROM version_snapshots b
        WHERE b.version_id = $2
        AND NOT EXISTS (SELECT 1 FROM version_snapshots a WHERE a.version_id = $1 AND a.sample_id = b.sample_id)
    """,
        version_a,
        version_b,
    )

    # Count removed (in A but not B)
    removed = await conn.fetchval(
        """
        SELECT count(*) FROM version_snapshots a
        WHERE a.version_id = $1
        AND NOT EXISTS (SELECT 1 FROM version_snapshots b WHERE b.version_id = $2 AND b.sample_id = a.sample_id)
    """,
        version_a,
        version_b,
    )

    # Count modified (in both, different metadata, hashes, or tags)
    modified = await conn.fetchval(
        f"""
        SELECT count(*) FROM version_snapshots a
        JOIN version_snapshots b ON a.sample_id = b.sample_id
        WHERE a.version_id = $1 AND b.version_id = $2
        AND (a.metadata_hash != b.metadata_hash OR a.hashes_hash IS DISTINCT FROM b.hashes_hash
             {geo_diff}{tags_ab_diff}{af_ab_diff}{media_diff})
    """,  # noqa: E501
        version_a,
        version_b,
    )

    # Count unchanged (in both, same metadata, hashes, tags, geo). Geo is guarded so a
    # hash-only snapshot (location/heading NULL) doesn't count as changed vs a full one —
    # exact complement of the modified predicate above.
    unchanged = await conn.fetchval(
        f"""
        SELECT count(*) FROM version_snapshots a
        JOIN version_snapshots b ON a.sample_id = b.sample_id
        WHERE a.version_id = $1 AND b.version_id = $2
        AND a.metadata_hash = b.metadata_hash
        AND a.hashes_hash IS NOT DISTINCT FROM b.hashes_hash
        {geo_same}{tags_ab_same}{af_ab_same}{media_same}
    """,  # noqa: E501
        version_a,
        version_b,
    )

    dataset_changed = await conn.fetchval(
        """SELECT NOT dataset_snapshots_equal(a.dataset_snapshot, b.dataset_snapshot)
           FROM dataset_versions a, dataset_versions b
           WHERE a.id = $1 AND b.id = $2
             AND a.dataset_snapshot IS NOT NULL AND b.dataset_snapshot IS NOT NULL""",
        version_a,
        version_b,
    )
    view_changes = await conn.fetchval(
        _view_changes_sql(_version_views_sql('$1'), _version_views_sql('$2')),
        version_a,
        version_b,
    )
    return {
        "version_a": version_a,
        "version_b": version_b,
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged": unchanged,
        "dataset_changed": dataset_changed,
        "view_changes": view_changes,
    }


async def _pair_diff_select_sql(change_type: str) -> str:
    """Inner SELECT returning `sample_id` for a version-pair diff.
    Params: $1=version_a, $2=version_b. No ORDER/LIMIT (caller appends)."""
    if change_type == "added":
        return (
            "SELECT b.sample_id FROM version_snapshots b WHERE b.version_id = $2 "
            "AND NOT EXISTS (SELECT 1 FROM version_snapshots a WHERE a.version_id = $1 AND a.sample_id = b.sample_id)"
        )
    if change_type == "removed":
        return (
            "SELECT a.sample_id FROM version_snapshots a WHERE a.version_id = $1 "
            "AND NOT EXISTS (SELECT 1 FROM version_snapshots b WHERE b.version_id = $2 AND b.sample_id = a.sample_id)"
        )
    if change_type == "modified":
        tags_ab_diff, _ = _tags_compare_two_snapshots_sql('a', 'b')
        af_ab_diff, _ = _array_fields_compare_two_snapshots_sql('a', 'b')
        geo_diff, _ = _pair_geo_compare_sql()
        media_diff, _ = _media_compare_sql('a', 'b', pair=True)
        return (
            f"SELECT a.sample_id FROM version_snapshots a "
            f"JOIN version_snapshots b ON a.sample_id = b.sample_id "
            f"WHERE a.version_id = $1 AND b.version_id = $2 "
            f"AND (a.metadata_hash != b.metadata_hash OR a.hashes_hash IS DISTINCT FROM b.hashes_hash "
            f"{geo_diff}{tags_ab_diff}{af_ab_diff}{media_diff})"
        )
    raise HTTPException(status_code=400, detail="change_type must be 'added', 'removed', or 'modified'")


async def _verify_versions(id: int, *version_ids: int):
    for vid in version_ids:
        exists = await db.fetchval("SELECT 1 FROM dataset_versions WHERE id = $1 AND dataset_id = $2", vid, id)
        if not exists:
            raise HTTPException(status_code=404, detail=f"Version {vid} not found")


async def _materialize_selection(inner_sql: str, *params) -> dict:
    """Run `inner_sql` (returning sample_id) INTO a region_selections token so the
    grid can scope via the in_selection filter — no id list crosses the wire.
    `token` is a 16-byte hex literal (injection-safe) so it can be inlined."""
    token = secrets.token_hex(16)
    status = await db.execute(
        f"INSERT INTO region_selections (token, sample_id) SELECT '{token}', sample_id FROM ({inner_sql}) q",
        *params,
    )
    total = int(status.split()[-1]) if status else 0
    return {"token": token, "total": total}


@router.get("/diff/{version_a}/{version_b}/samples")
async def diff_samples(
    id: int,
    version_a: int,
    version_b: int,
    change_type: str = "added",
    limit: int = 50,
    offset: int = 0,
):
    """Get the actual sample IDs for a specific change type in a diff (paged).

    change_type: 'added', 'removed', 'modified'
    """
    await _verify_versions(id, version_a, version_b)
    sel = await _pair_diff_select_sql(change_type)
    rows = await db.fetch(
        f"{sel} ORDER BY sample_id LIMIT $3 OFFSET $4",
        version_a,
        version_b,
        min(limit, 10000),
        offset,
    )
    return {"sample_ids": [r['sample_id'] for r in rows]}


@router.post("/diff/{version_a}/{version_b}/select")
async def diff_pair_select(id: int, version_a: int, version_b: int, change_type: str = "added"):
    """Materialize ALL changed sample_ids for a version-pair diff into a
    region_selections token (no id list to the client). Returns {token, total}."""
    await _verify_versions(id, version_a, version_b)
    sel = await _pair_diff_select_sql(change_type)
    return await _materialize_selection(sel, version_a, version_b)


@router.get("/current-diff/{version_id}")
async def diff_current(id: int, version_id: int):
    async with db.pool.acquire() as conn:
        async with conn.transaction(isolation='repeatable_read'):
            return await _diff_current(conn, id, version_id)


async def _diff_current(conn, id: int, version_id: int):
    """Compare a version against the current live dataset state.

    Same as diff but version_b is the live dataset (not a snapshot).
    """
    exists = await conn.fetchval(
        "SELECT snapshot_format FROM dataset_versions WHERE id = $1 AND dataset_id = $2",
        version_id,
        id,
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Version not found")

    # Added: in current dataset but not in version
    added = await conn.fetchval(
        """
        SELECT count(*) FROM samples s
        WHERE s.dataset_id = $2
        AND NOT EXISTS (SELECT 1 FROM version_snapshots v WHERE v.version_id = $1 AND v.sample_id = s.id)
    """,
        version_id,
        id,
    )

    # Removed: in version but not in current dataset
    removed = await conn.fetchval(
        """
        SELECT count(*) FROM version_snapshots v
        WHERE v.version_id = $1
        AND NOT EXISTS (SELECT 1 FROM samples s WHERE s.dataset_id = $2 AND s.id = v.sample_id)
    """,
        version_id,
        id,
    )

    # Build array field + tags comparisons for diff
    arr_cols, _ = await _snapshot_arrays(conn)
    af_diff, af_same = _array_field_compare_sql(arr_cols)
    tags_diff, tags_same = _tags_compare_sql('v', 's')
    geo_diff, geo_same = _geo_compare_sql(exists >= EXACT_GEO_FORMAT)
    media_diff, media_same = _media_compare_sql()

    # Modified: in both but metadata, hashes, array fields, or tags changed
    modified = await conn.fetchval(
        f"""
        SELECT count(*) FROM version_snapshots v
        JOIN samples s ON v.sample_id = s.id
        WHERE v.version_id = $1 AND s.dataset_id = $2
        AND (v.metadata_hash != md5(s.metadata::text)
             OR v.hashes_hash IS DISTINCT FROM (
                 SELECT md5(string_agg(h.field_name || ':' || h.hash_hex, ',' ORDER BY h.field_name))
                 FROM sample_hashes h WHERE h.sample_id = s.id
             )
             {geo_diff}{af_diff}{tags_diff}{media_diff})
    """,
        version_id,
        id,
    )

    # Unchanged
    unchanged = await conn.fetchval(
        f"""
        SELECT count(*) FROM version_snapshots v
        JOIN samples s ON v.sample_id = s.id
        WHERE v.version_id = $1 AND s.dataset_id = $2
        AND v.metadata_hash = md5(s.metadata::text)
        AND v.hashes_hash IS NOT DISTINCT FROM (
            SELECT md5(string_agg(h.field_name || ':' || h.hash_hex, ',' ORDER BY h.field_name))
            FROM sample_hashes h WHERE h.sample_id = s.id
        )
        {geo_same}{af_same}{tags_same}{media_same}
    """,
        version_id,
        id,
    )

    # Current total
    current_count = await conn.fetchval("SELECT count(*) FROM samples WHERE dataset_id = $1", id)
    dataset_changed = await conn.fetchval(
        f"""SELECT NOT dataset_snapshots_equal(dataset_snapshot, ({_dataset_snapshot_sql('$2')}))
            FROM dataset_versions WHERE id = $1 AND dataset_snapshot IS NOT NULL""",
        version_id,
        id,
    )
    view_changes = await conn.fetchval(
        _view_changes_sql(_version_views_sql('$1'), _saved_views_sql('$2')),
        version_id,
        id,
    )

    return {
        "version_id": version_id,
        "vs": "current",
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged": unchanged,
        "current_sample_count": current_count,
        "dataset_changed": dataset_changed,
        "view_changes": view_changes,
    }


# The restore is one SERIALIZABLE transaction, so a writer that commits during it (an edit, an
# ingest, the statistics refresh the previous restore queued) rolls the whole restore back. It is
# run again after each of these pauses (seconds), and reports 409 only when the last attempt fails.
_RESTORE_RETRY_DELAYS = (0.1, 0.2)


@router.post("/{version_id}/revert")
async def revert_to_version(
    id: int,
    version_id: int,
    restore_deleted: bool = False,
    # An admin: a revert can do what only an admin may do one at a time. It deletes the samples added since
    # (exact_membership), rewrites or removes public views whoever owns them, and can change the dataset's
    # settings (restore_settings).
    user: dict = Depends(require_role("admin")),
    exact_membership: bool = False,
    restore_settings: bool = False,
):
    for delay in (*_RESTORE_RETRY_DELAYS, None):
        try:
            return await _restore_version(id, version_id, restore_deleted, user, exact_membership, restore_settings)
        except asyncpg.SerializationError:
            if delay is None:
                raise HTTPException(status_code=409, detail="Dataset changed during restore; retry the operation")
            await asyncio.sleep(delay)
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail="A sample filepath conflicts with the snapshot; restore was rolled back",
            )


async def _restore_version(
    id: int,
    version_id: int,
    restore_deleted: bool,
    user: dict,
    exact_membership: bool,
    restore_settings: bool = False,
):
    """Revert the dataset to a specific version's state.

    Automatically creates a snapshot of the current state before reverting
    so you can undo. Restores metadata, tags for all samples in the version.
    Samples added after the version are kept unless exact_membership=True.

    restore_settings=True also puts the dataset's own settings back as the version
    recorded them (_restore_dataset_settings) and the view it opens on (_restore_default_view);
    otherwise they stay as they are.

    restore_deleted=True additionally re-creates samples that were deleted
    since the version, from their snapshot (original id, filepath, metadata,
    tags, location, heading, array fields). Embeddings/hashes were destroyed
    with the sample and are NOT restored — they're uploader-owned derived
    data; restored samples show up in /datasets/{id}/missing for backfill.
    """
    # Verify version exists and has full metadata
    version = await db.fetchrow(
        """
        SELECT id, name, include_metadata, sample_count, snapshot_format, dataset_snapshot FROM dataset_versions
        WHERE id = $1 AND dataset_id = $2
    """,
        version_id,
        id,
    )
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    if version['include_metadata'] is False:
        raise HTTPException(status_code=400, detail="Cannot revert to a hash-only version (no metadata stored)")
    if restore_settings and version['dataset_snapshot'] is None:
        raise HTTPException(
            status_code=400,
            detail="This version did not record the dataset's settings, so they can't be restored. "
            "Revert without restoring settings.",
        )

    # Verify the snapshot actually has metadata (older versions may not)
    has_metadata = await db.fetchval(
        "SELECT COUNT(*) FROM version_snapshots "
        "WHERE version_id = $1 AND metadata IS NOT NULL AND filepath IS NOT NULL",
        version_id,
    )
    if has_metadata != version['sample_count']:
        raise HTTPException(
            status_code=400,
            detail="This version's snapshot does not contain metadata (created before full snapshots were enabled). Cannot revert.",  # noqa: E501
        )

    auto_snapshot_id = None
    auto_snapshot_name = None
    async with db.pool.acquire() as conn:
        # The auto-snapshot inside the revert is a full snapshot: same free-space gate.
        await require_free_space(
            conn,
            await estimate_snapshot_bytes(conn, id),
            f"the auto-snapshot before reverting dataset {id}",
        )

    # Auto-snapshot + restore run in ONE transaction: a crash/timeout mid-revert
    # must leave either both committed or neither, never an orphaned auto-snapshot
    # next to a half-reverted dataset.
    from ..bulk_jobs import require_no_pending_bulk
    from .datasets import _field_index_operation

    # Acquire before the SERIALIZABLE snapshot so a concurrent submission is visible.
    async with db.pool.acquire() as conn, _field_index_operation(conn, id):
        async with conn.transaction(isolation='serializable'):
            await require_no_pending_bulk(conn, id)
            await lock_field_lifecycle(conn, id, exclusive=True)
            locked_version = await conn.fetchval(
                "SELECT id FROM dataset_versions WHERE id = $1 AND dataset_id = $2 FOR SHARE",
                version_id,
                id,
            )
            if locked_version is None:
                raise HTTPException(status_code=404, detail="Version not found")
            from datetime import datetime

            current_count = await conn.fetchval("SELECT count(*) FROM samples WHERE dataset_id = $1", id)
            # Include seconds + microseconds so two reverts within the same
            # minute don't collide on the (dataset_id, name) unique constraint
            # and 500 mid-revert.
            timestamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            auto_snapshot_name = f"auto-{timestamp}"
            auto_version = await conn.fetchrow(
                f"""
                INSERT INTO dataset_versions (dataset_id, name, description, sample_count, include_metadata,
                                              snapshot_format, dataset_snapshot, saved_views, created_by)
                VALUES ($1, $2, $3, $4, TRUE, $5, ({_dataset_snapshot_sql('$1')}), ({_saved_views_sql('$1')}), $6)
                RETURNING id
            """,
                id,
                auto_snapshot_name,
                f"Auto-snapshot before reverting to {version['name']}",
                current_count,
                SNAPSHOT_FORMAT,
                user.get('username'),
            )
            auto_snapshot_id = auto_version['id']
            # The undo point has recorded the views as they are; now put the version's back. First, since a
            # private view in the way stops the restore: before the samples are copied and rewritten.
            views_restored, views_removed = await _restore_saved_views(conn, id, version_id, user, exact_membership)

            arr_cols, auto_af_expr = await _snapshot_arrays(conn)

            # Capture hashes_hash too (mirrors create_version). Omitting it
            # made this undo-point falsely report every hashed sample as
            # 'modified' in status/diff.
            await conn.execute(
                f"""
                INSERT INTO version_snapshots (version_id, sample_id, metadata_hash, hashes_hash, metadata, tags, filepath, location, heading, array_fields_snapshot, media_type, created_at, last_modified_at)
                SELECT $1, s.id, md5(s.metadata::text),
                    (SELECT md5(string_agg(h.field_name || ':' || h.hash_hex, ',' ORDER BY h.field_name))
                     FROM sample_hashes h WHERE h.sample_id = s.id),
                    s.metadata, s.tags, s.filepath, s.location, s.heading,
                    {auto_af_expr}, s.media_type, s.created_at, s.last_modified_at
                FROM samples s WHERE s.dataset_id = $2
            """,  # noqa: E501
                auto_snapshot_id,
                id,
                timeout=600,
            )

            removed = 0
            if exact_membership:
                delete_status = await conn.execute(
                    """DELETE FROM samples s WHERE s.dataset_id = $2
                       AND NOT EXISTS (SELECT 1 FROM version_snapshots v
                                       WHERE v.version_id = $1 AND v.sample_id = s.id)""",
                    version_id,
                    id,
                )
                removed = int(delete_status.split()[-1])

            # Revert metadata, tags, and array fields. Array columns the snapshot
            # never recorded (added after it was taken) are left as-is — never
            # wiped — consistent with the diff now ignoring them.
            af_set_clauses = ""
            if arr_cols:
                af_set_clauses = ",\n                " + ",\n                ".join(
                    f"{col} = CASE WHEN v.array_fields_snapshot ? '{col}' "
                    f"THEN CASE WHEN jsonb_typeof(v.array_fields_snapshot->'{col}') = 'array' "
                    f"THEN ARRAY(SELECT jsonb_array_elements_text(v.array_fields_snapshot->'{col}')) ELSE NULL END "
                    f"ELSE s.{col} END"
                    for col in arr_cols
                )

            # A format-2 snapshot recorded every row's geo, so its NULL is restored as NULL; an
            # older one never recorded geo and keeps the live value.
            exact_geo = (version['snapshot_format'] or 1) >= EXACT_GEO_FORMAT
            if exact_geo:
                geo_set = "location = v.location, heading = v.heading"
            else:
                geo_set = "location = COALESCE(v.location, s.location), heading = COALESCE(v.heading, s.heading)"
            geo_diff, _ = _geo_compare_sql(exact_geo)
            media_diff, _ = _media_compare_sql()
            af_diff = "".join(
                f" OR (v.array_fields_snapshot ? '{col}' AND COALESCE(to_jsonb(s.{col}), 'null'::jsonb) "
                f"IS DISTINCT FROM v.array_fields_snapshot->'{col}')"
                for col in arr_cols
            )
            # Only a row the revert actually changes is stamped modified — and by whom. It stamped
            # every snapshotted row NOW() with no author, so "recently modified" and the modified-by
            # filter meant nothing after a revert. Every row is still written, so
            # samples_reverted keeps counting the version's rows that still exist.
            changed = (
                f"(s.metadata IS DISTINCT FROM v.metadata OR s.tags IS DISTINCT FROM v.tags"
                f"{geo_diff}{af_diff}{media_diff})"
            )
            # Which fields this restore changes, before it changes them (all of them when samples are
            # also removed or re-created).
            affected = (
                None
                if exact_membership or restore_deleted
                else await _changed_restore_fields(conn, version_id, id, arr_cols, changed)
            )
            update_status = await conn.execute(
                f"""
                UPDATE samples s
                SET metadata = v.metadata,
                    tags = v.tags,
                    filepath = v.filepath,
                    media_type = COALESCE(v.media_type, s.media_type),
                    {geo_set},
                    last_modified_at = CASE WHEN {changed} THEN NOW() ELSE s.last_modified_at END,
                    modified_by_user_id = CASE WHEN {changed} THEN $3 ELSE s.modified_by_user_id END{af_set_clauses}
                FROM version_snapshots v
                WHERE v.version_id = $1 AND v.sample_id = s.id AND s.dataset_id = $2
            """,
                version_id,
                id,
                user['id'],
                timeout=600,
            )

            restored = 0
            if restore_deleted or exact_membership:
                # Re-create samples deleted since the version, from their snapshot,
                # with their ORIGINAL id — sequences never reissue ids, so this is
                # safe and reconnects the sample to its rows in every other
                # version's snapshots. media_type is the snapshot's (it was once
                # guessed from the extension, so object rows and extension-less images came
                # back 'unknown'); only older snapshots still guess. If another sample
                # reoccupied the filepath since, that row is skipped (the
                # filepath "exists again"); the count reflects actual inserts.
                af_insert_cols = ''.join(f", {col}" for col in arr_cols)
                af_insert_vals = ''.join(
                    f", CASE WHEN jsonb_typeof(v.array_fields_snapshot->'{col}') = 'array' "
                    f"THEN ARRAY(SELECT jsonb_array_elements_text(v.array_fields_snapshot->'{col}')) "
                    f"ELSE NULL END"
                    for col in arr_cols
                )
                conflict = (
                    '' if exact_membership else 'ON CONFLICT ON CONSTRAINT samples_dataset_filepath_unique DO NOTHING'
                )
                insert_status = await conn.execute(
                    f"""
                    INSERT INTO samples (id, dataset_id, filepath, media_type, tags, metadata, location, heading,
                                         created_at, last_modified_at, modified_by_user_id{af_insert_cols})
                    SELECT v.sample_id, $2, v.filepath,
                        COALESCE(v.media_type, CASE
                            WHEN lower(v.filepath) ~ '\\.(jpg|jpeg|png|gif|bmp|webp|tiff)$' THEN 'image'
                            WHEN lower(v.filepath) ~ '\\.(mp4|avi|mov|mkv|webm)$' THEN 'video'
                            WHEN lower(v.filepath) ~ '\\.(mp3|wav|ogg|flac)$' THEN 'audio'
                            ELSE 'unknown'
                        END),
                        COALESCE(v.tags, '{{}}'), COALESCE(v.metadata, '{{}}'::jsonb),
                        v.location, v.heading, COALESCE(v.created_at, NOW()), NOW(), $3{af_insert_vals}
                    FROM version_snapshots v
                    WHERE v.version_id = $1
                      AND NOT EXISTS (SELECT 1 FROM samples s WHERE s.id = v.sample_id)
                    {conflict}
                """,  # noqa: E501
                    version_id,
                    id,
                    user['id'],
                    timeout=600,
                )
                restored = int(insert_status.split()[-1]) if insert_status else 0
            definitions_query = (
                'SELECT field_name, field_type, display_type, origin FROM dataset_fields WHERE dataset_id = $1'
            )
            definitions_before = {tuple(row) for row in await conn.fetch(definitions_query, id)}
            await _restore_field_registry(conn, id, version)
            if restore_settings:
                await _restore_dataset_settings(conn, id, version['dataset_snapshot'])
                await _restore_default_view(conn, id, version_id)
            definitions_after = {tuple(row) for row in await conn.fetch(definitions_query, id)}
            if affected is not None:
                affected |= {row[0] for row in definitions_before ^ definitions_after}
            await conn.execute(
                REQUEST_FULL_REFRESH,
                id,
            )
            await _invalidate_restored_statistics(conn, id, affected)

    return {
        "message": f"Reverted to {version['name']}",
        "auto_snapshot_name": auto_snapshot_name,
        "auto_snapshot_id": auto_snapshot_id,
        # Actual rows updated — fewer than the version's sample_count when
        # snapshotted samples have since been deleted (see restore_deleted).
        "samples_reverted": int(update_status.split()[-1]) if update_status else 0,
        "samples_restored": restored,
        "samples_removed": removed,
        # Public saved views given back their recorded filters or made again, and (exact_membership) those
        # removed because the version doesn't have them. Both 0 for a version that recorded no views.
        "views_restored": views_restored,
        "views_removed": views_removed,
        "settings_restored": restore_settings,
    }


async def _current_diff_select_sql(change_type: str, exact_geo: bool) -> str:
    """Inner SELECT returning `sample_id` for a vs-current diff.
    Params: $1=version_id, $2=dataset_id. No ORDER/LIMIT (caller appends)."""
    if change_type == "added":
        return (
            "SELECT s.id AS sample_id FROM samples s WHERE s.dataset_id = $2 "
            "AND NOT EXISTS (SELECT 1 FROM version_snapshots v WHERE v.version_id = $1 AND v.sample_id = s.id)"
        )
    if change_type == "removed":
        return (
            "SELECT v.sample_id FROM version_snapshots v WHERE v.version_id = $1 "
            "AND NOT EXISTS (SELECT 1 FROM samples s WHERE s.dataset_id = $2 AND s.id = v.sample_id)"
        )
    if change_type == "modified":
        # SAME comparison (incl. location/heading) as diff_current's count so the
        # listing can't disagree with the badge — omitting geo here made a
        # location-only or heading-only change count as modified but never list.
        arr_cols = sorted(await get_array_columns() - {'tags'})
        af_diff, _ = _array_field_compare_sql(arr_cols)
        tags_diff, _ = _tags_compare_sql('v', 's')
        geo_diff, _ = _geo_compare_sql(exact_geo)
        media_diff, _ = _media_compare_sql()
        return (
            f"SELECT v.sample_id FROM version_snapshots v JOIN samples s ON v.sample_id = s.id "
            f"WHERE v.version_id = $1 AND s.dataset_id = $2 "
            f"AND (v.metadata_hash != md5(s.metadata::text) "
            f"OR v.hashes_hash IS DISTINCT FROM ("
            f"SELECT md5(string_agg(h.field_name || ':' || h.hash_hex, ',' ORDER BY h.field_name)) "
            f"FROM sample_hashes h WHERE h.sample_id = s.id) "
            f"{geo_diff}{af_diff}{tags_diff}{media_diff})"
        )
    raise HTTPException(status_code=400, detail="change_type must be 'added', 'removed', or 'modified'")


@router.get("/current-diff/{version_id}/samples")
async def diff_current_samples(
    id: int,
    version_id: int,
    change_type: str = "added",
    limit: int = 10000,
    offset: int = 0,
):
    """Get sample IDs for a specific change type vs current state (paged)."""
    await _verify_versions(id, version_id)
    sel = await _current_diff_select_sql(change_type, await _exact_geo(version_id))
    rows = await db.fetch(f"{sel} ORDER BY sample_id LIMIT $3 OFFSET $4", version_id, id, min(limit, 10000), offset)
    return {"sample_ids": [r['sample_id'] for r in rows]}


@router.post("/current-diff/{version_id}/select")
async def diff_current_select(id: int, version_id: int, change_type: str = "added"):
    """Materialize ALL changed sample_ids (vs current) into a region_selections
    token (no id list to the client). Returns {token, total}."""
    await _verify_versions(id, version_id)
    sel = await _current_diff_select_sql(change_type, await _exact_geo(version_id))
    return await _materialize_selection(sel, version_id, id)
