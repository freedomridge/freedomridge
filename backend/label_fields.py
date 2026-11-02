"""Overlay shape discovery and dataset-scoped label paths.

Writes only append to existing discovery caches. Statistics owns removals; a
missing cache is discovered only by the sidebar, never by filter compilation or writes.
"""

import asyncpg

LEGACY_GEOMETRY_FIELDS = frozenset({'detections', 'polygons'})
LEGACY_LABEL_FIELDS = LEGACY_GEOMETRY_FIELDS | {'ground_truth', 'predictions'}
VECTOR_CACHES = {
    '__detection_fields__': 'detections',
    '__polyline_fields__': 'polygons',
    '__keypoint_fields__': 'keypoints',
}
DISCOVERY_TYPES = {
    **VECTOR_CACHES,
    '__cuboid_fields__': 'cuboids',
    '__heatmap_fields__': 'heatmap',
    '__mask_overlay_fields__': 'segmentation',
    '__classification_fields__': 'classification',
}
LABEL_CACHES = (*VECTOR_CACHES, '__classification_fields__')
# No other sidebar scan has a server-side limit. Match statistics measurements' 10 s budget.
DISCOVERY_TIMEOUT_MS = 10_000
# Applied to an object, never a string element (JSONB's ? also matches strings).
BBOX_SQL = "COALESCE(jsonb_typeof(value->0->'bbox') IN ('object','array'), FALSE)"
VECTOR_SQL = {
    'detections': f"value->0->>'_type' IS DISTINCT FROM 'cuboid' AND {BBOX_SQL}",
    'keypoints': f"NOT {BBOX_SQL} AND value->0->>'_type' = 'keypoints'",
    'polygons': f"NOT {BBOX_SQL} AND value->0->>'_type' IS DISTINCT FROM 'keypoints' "
    "AND value->0->>'_type' IS DISTINCT FROM 'cuboid' "
    "AND (jsonb_typeof(value->0->'points')='array' OR jsonb_typeof(value->0->'paths')='array')",
}


def overlay_type(value):
    """Match frontend detectLabelType, including its precedence."""
    if isinstance(value, dict):
        if value.get('type') == 'heatmap':
            return 'heatmap'
        if value.get('_type') == 'cuboid':
            return None
        if 'maskPath' in value:
            return 'segmentation'
        return 'classification' if 'label' in value else None
    if not isinstance(value, list) or not value or not isinstance(value[0], dict):
        return None
    first = value[0]
    if first.get('_type') == 'cuboid':
        return 'cuboids'
    if isinstance(first.get('bbox'), (dict, list)):
        return 'detections'
    if first.get('_type') == 'keypoints':
        return 'keypoints'
    if isinstance(first.get('points'), list) or isinstance(first.get('paths'), list):
        return 'polygons'
    return 'classification' if 'label' in first else None


async def append_discovery(connection, dataset_id, metadata):
    members: dict[str, set[str]] = {}
    by_type = {kind: cache for cache, kind in DISCOVERY_TYPES.items()}
    for row in metadata:
        for name, value in row.items():
            kind = overlay_type(value)
            if kind is None or (kind == 'segmentation' and name == 'segmentation'):
                continue
            if kind == 'classification' and name in LEGACY_GEOMETRY_FIELDS:
                continue
            members.setdefault(by_type[kind], set()).add(name)
    # Deterministic lock order; skip unchanged rows, including their xmin token.
    for cache, names in sorted(members.items()):
        await connection.execute(
            """UPDATE dataset_field_stats SET unique_values = ARRAY(
                   SELECT DISTINCT unnest(unique_values || $3::text[]) ORDER BY 1)
               WHERE dataset_id=$1 AND field_name=$2 AND unique_values IS NOT NULL
                 AND NOT unique_values @> $3::text[]""",
            dataset_id,
            cache,
            sorted(names),
        )


async def copy_discovery(connection, dataset_id, source, target):
    """A rename/clone has the source's known shapes without scanning samples."""
    await connection.fetch(
        'SELECT field_name FROM dataset_field_stats WHERE dataset_id=$1 AND field_name=ANY($4::text[]) '
        'AND $2=ANY(unique_values) AND NOT $3=ANY(unique_values) ORDER BY field_name FOR UPDATE',
        dataset_id,
        source,
        target,
        sorted(DISCOVERY_TYPES),
    )
    await connection.execute(
        """UPDATE dataset_field_stats SET unique_values = ARRAY(
               SELECT DISTINCT unnest(unique_values || ARRAY[$3::text]) ORDER BY 1)
           WHERE dataset_id=$1 AND field_name=ANY($4::text[])
             AND $2=ANY(unique_values) AND NOT $3=ANY(unique_values)""",
        dataset_id,
        source,
        target,
        sorted(DISCOVERY_TYPES),
    )


DISCOVERY_SQL = {
    cache: "jsonb_typeof(value)='array' AND jsonb_typeof(value->0)='object' AND " + VECTOR_SQL[kind]
    for cache, kind in VECTOR_CACHES.items()
}
DISCOVERY_SQL.update(
    {
        '__cuboid_fields__': "jsonb_typeof(value)='array' AND jsonb_typeof(value->0)='object' "
        "AND value->0->>'_type'='cuboid'",
        '__heatmap_fields__': "jsonb_typeof(value)='object' AND value->>'type'='heatmap'",
        '__mask_overlay_fields__': "jsonb_typeof(value)='object' AND value ? 'maskPath' AND key<>'segmentation' "
        "AND value->>'type' IS DISTINCT FROM 'heatmap'",
        '__classification_fields__': """key NOT IN ('detections','polygons') AND (
        (jsonb_typeof(value)='object' AND value ? 'label'
         AND value->>'_type' IS DISTINCT FROM 'cuboid' AND NOT value ? 'maskPath'
         AND value->>'type' IS DISTINCT FROM 'heatmap') OR
        (jsonb_typeof(value)='array' AND jsonb_typeof(value->0)='object' AND value->0 ? 'label'
         AND NOT COALESCE(jsonb_typeof(value->0->'bbox') IN ('object','array'), FALSE)
         AND jsonb_typeof(value->0->'points') IS DISTINCT FROM 'array'
         AND jsonb_typeof(value->0->'paths') IS DISTINCT FROM 'array'
         AND value->0->>'_type' IS DISTINCT FROM 'cuboid'
         AND value->0->>'_type' IS DISTINCT FROM 'keypoints'))""",
    },
)


async def discover_fields(pool, dataset_id, cache, cached=None):
    """Sidebar-only, cross-worker single flight per dataset/cache; empty lists are hits.

    The transaction-scoped advisory lock covers the second cache check, scan and
    publication on one connection. Cancellation/errors release it and SET LOCAL.
    Writers do not take this lock: append-only publication preserves their entries.
    """
    if cached is not None and cached.get('unique_values') is not None:
        return cached['unique_values']
    try:
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(f"SET LOCAL statement_timeout='{DISCOVERY_TIMEOUT_MS}ms'")
            await connection.execute(
                'SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
                f'overlay-discovery:{dataset_id}:{cache}',
            )
            fresh = await connection.fetchrow(
                'SELECT unique_values FROM dataset_field_stats WHERE dataset_id=$1 AND field_name=$2',
                dataset_id,
                cache,
            )
            if fresh is not None and fresh['unique_values'] is not None:
                return fresh['unique_values']
            rows = await connection.fetch(
                "SELECT DISTINCT key AS field FROM samples s, jsonb_each(s.metadata) AS je(key,value) "
                f"WHERE s.dataset_id=$1 AND ({DISCOVERY_SQL[cache]}) ORDER BY key",
                dataset_id,
            )
            fields = [row['field'] for row in rows]
            # A sealing dataset remains readable. Roll back just publication if sealing races us.
            try:
                async with connection.transaction():
                    await connection.execute(
                        """INSERT INTO dataset_field_stats (dataset_id,field_name,field_type,unique_values)
                           SELECT $1,$2,$3,$4::text[] WHERE EXISTS (
                               SELECT 1 FROM datasets WHERE id=$1 AND NOT deleting)
                           ON CONFLICT (dataset_id,field_name) DO UPDATE SET
                           unique_values=ARRAY(SELECT DISTINCT unnest(
                               EXCLUDED.unique_values || COALESCE(dataset_field_stats.unique_values,'{}')) ORDER BY 1),
                           field_type=EXCLUDED.field_type""",
                        dataset_id,
                        cache,
                        cache.removeprefix('__').removesuffix('_fields__') + '_index',
                        fields,
                    )
            except asyncpg.ObjectNotInPrerequisiteStateError:
                pass
            return fields
    except Exception as error:
        print(f'[field-stats] Failed to discover {cache} for dataset {dataset_id}: {error}')
        return []


class DatasetArrayColumns(set):
    """Native array names plus the schema context needed by synchronous SQL builders."""

    def __init__(self, columns=(), *, label_fields=LEGACY_LABEL_FIELDS, registered=(), list_fields=()):
        super().__init__(columns)
        self.label_fields = set(label_fields)
        self.registered = set(registered)
        # Registered list fields: read paths treat them as element paths even before discovery
        # has cached them (a new dataset, a deploy, a restore). Writes and statistics use
        # label_fields only, so plain lists still get no `.label` vocabulary.
        self.list_fields = set(list_fields)


def label_path(field, columns=None):
    """A registered dotted key always wins over a derived element path."""
    if field in getattr(columns, 'registered', ()) or '.' not in field:
        return None
    parent, sub = field.rsplit('.', 1)
    labels = getattr(columns, 'label_fields', LEGACY_LABEL_FIELDS)
    known = parent in labels or parent in getattr(columns, 'list_fields', ())
    return (parent, sub) if known else None


async def label_context(connection, dataset_id, columns=()):
    """Filter/write context is cache-only; discovery belongs exclusively to field-stats."""
    registered = await connection.fetch(
        "SELECT field_name, field_type FROM dataset_fields WHERE dataset_id=$1 AND origin<>'system'",
        dataset_id,
    )
    lists = {row['field_name'] for row in registered if row['field_type'] == 'list'}
    rows = await connection.fetch(
        'SELECT field_name, unique_values FROM dataset_field_stats WHERE dataset_id=$1 AND field_name=ANY($2::text[])',
        dataset_id,
        list(LABEL_CACHES),
    )
    labels = set(LEGACY_LABEL_FIELDS)
    for row in rows:
        members = set(row['unique_values'] or [])
        if row['field_name'] == '__classification_fields__':
            members &= lists  # single classification objects are overlays, not label lists
        labels.update(members)
    return DatasetArrayColumns(
        columns,
        label_fields=labels,
        registered=[row['field_name'] for row in registered],
        list_fields=lists,
    )
