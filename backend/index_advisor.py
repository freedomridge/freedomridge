"""Index advisor: a read-only review of the database's indexes.

An `index_review` job reads the catalogs, PostgreSQL's index usage counters and the API's field usage
telemetry (backend/field_usage.py) and recommends; it never creates or drops an index. Its result has
three sections: Suggested indexes (per dataset), Possibly unused indexes and Overlapping indexes.
Every change is a separate, explicit administrator action that runs as its own job.

Thresholds are derived, not tuned:
- REVIEW_PERIOD is one review period (the approved weekly cadence): the telemetry window and the
  payback horizon. IDLE_WINDOW is two of them: an index is called unused only after more than a whole
  cycle of the longest regular (weekly) jobs has passed without it, with room for one missed run.
- Statistics start S = the later of pg_stat_database.stats_reset (pg_stat_reset()) and
  pg_stat_bgwriter.stats_reset, which PostgreSQL stamps whenever the cumulative statistics start from
  zero: a new cluster or crash recovery (verified on PostgreSQL 16 and 18).
- An index is observed since GREATEST(S, first seen). First seen is when the advisor first observed the
  index (index_observations, seeded when 066 is applied): never back-dated, so after a deploy or a
  restore "possibly unused" says "not enough history" until the idle window has passed. An index whose
  OID, cluster or database differs from its observation row, or whose counter went backwards, starts
  over. Overlaps are structural and need no history.
- Verdicts are joint: the index an overlap keeps is never itself offered for removal (and a pair is
  offered only when the kept index is used at least as much as the redundant one), and the removals
  offered together never leave a foreign key without an index (the best supporter is kept as needed).
- A field is suggested when its requests are slower than the same dataset's indexed fields (or, when
  none is indexed, its own average) and the extra waiting in one review period is at least the measured
  time an index build takes on this deployment. Until a build has been measured, only a field whose
  requests take, on average, at least twice the reference's average is suggested (measured means, not
  histogram buckets). A request's time is shared equally among the fields it filters by, so a field's
  mean depends on its company: one always filtered alone is compared at its full request time
  (field_usage.record); an accepted heuristic, documented with the rule.

Reviews belong to no dataset: a read-only review never waits behind a dataset's lane owner (a bulk
edit, an hour-long index build). A dataset review names its dataset in `args.dataset_id`; the dataset's
Jobs panel lists it from there. A review writes only its own bookkeeping (index_observations).
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import field_usage, job_queue
from .job_queue import JobPolicy
from .job_runner import StepResult

logger = logging.getLogger(__name__)

KIND = 'index_review'
REVIEW_PERIOD = timedelta(days=field_usage.WINDOW_DAYS)
WINDOW = REVIEW_PERIOD  # the telemetry window shown with a review
IDLE_WINDOW = 2 * REVIEW_PERIOD
POLICY = JobPolicy(max_unit_seconds=300, max_failures=3, retry_seconds=5, max_retry_seconds=60)
DB_DIR = Path(__file__).resolve().parent / 'db'
INDEXABLE_TYPES = ('number', 'string', 'datetime')  # the field types promote can index
# indoption bits (pg_index.h): a backward scan reverses both, so an index read backwards serves the other.
INDOPTION_REVERSED = 0x0001 | 0x0002
_FIELD_INDEX = re.compile(r'^idx_samples_meta_(\d+)_')
_ARRAY_INDEX = re.compile(r'^idx_samples_[A-Za-z0-9_]+_gin$')
_FIELD_EXPRESSION = re.compile(r"metadata ->> '((?:[^']|'')*)'")
_CREATE_INDEX = re.compile(
    r'\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)'
    r'\s+ON\s+(?:ONLY\s+)?(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)'
    r'|\bDROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)',
    re.I,
)


# The field names promote accepts (routers/datasets.promote_field): the advisor suggests no others.
promotable_name = field_usage.indexable_name


# ---------------------------------------------------------------------------------------------------
# Core (schema-defined) indexes


def _index_statements(text: str) -> List[Tuple[str, Optional[str], bool]]:
    """(name, table, created) for each CREATE INDEX / DROP INDEX in order, comments ignored."""
    text = re.sub(r'--[^\n]*', '', text)
    found = []
    for match in _CREATE_INDEX.finditer(text):
        if match.group(1):
            found.append((match.group(1), match.group(2), True))
        else:
            found.append((match.group(3), None, False))
    return found


@lru_cache(maxsize=1)
def core_indexes() -> Dict[str, str]:
    """name -> table for every index schema.sql leaves in a fresh installation."""
    indexes: Dict[str, str] = {}
    for name, table, created in _index_statements((DB_DIR / 'schema.sql').read_text()):
        if created:
            indexes[name] = table or ''
        else:
            indexes.pop(name, None)
    return indexes


# ---------------------------------------------------------------------------------------------------
# Collection: one read-only snapshot of catalogs, statistics, telemetry and build measurements


def _index_query(has_last_scan: bool, *, table_of: bool = False) -> str:
    """Every index of this schema; `table_of`: only those on the table of the index named $1."""
    last_scan = 's.last_idx_scan' if has_last_scan else 'NULL::timestamptz'
    only = (
        """ AND i.indrelid = (SELECT x.indrelid FROM pg_index x JOIN pg_class n ON n.oid = x.indexrelid
                              WHERE n.relname = $1 AND n.relnamespace = current_schema()::regnamespace)"""
        if table_of
        else ''
    )
    return f"""
        SELECT c.oid::bigint AS oid, c.relname AS name, t.relname AS table_name, am.amname AS method,
               i.indisunique AS is_unique, i.indisprimary AS is_primary, i.indisexclusion AS is_exclusion,
               i.indisvalid AS valid, i.indisready AS ready, i.indisreplident AS replica_identity,
               EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = c.oid AND k.contype IN ('p', 'u', 'x'))
                   AS constraint_backed,
               EXISTS (SELECT 1 FROM pg_stat_progress_create_index p WHERE p.index_relid = c.oid) AS building,
               pg_get_indexdef(c.oid) AS definition,
               pg_get_expr(i.indpred, i.indrelid) AS predicate,
               ARRAY(SELECT pg_get_indexdef(c.oid, k, TRUE) FROM generate_series(1, i.indnkeyatts) k) AS keys,
               ARRAY(SELECT pg_get_indexdef(c.oid, k, TRUE) FROM generate_series(i.indnkeyatts + 1, i.indnatts) k)
                   AS include,
               ARRAY(SELECT oc.opcfamily::bigint FROM unnest(i.indclass::oid[]) WITH ORDINALITY AS x(cls, n)
                     JOIN pg_opclass oc ON oc.oid = x.cls WHERE x.n <= i.indnkeyatts ORDER BY x.n) AS families,
               ARRAY(SELECT x.coll::bigint FROM unnest(i.indcollation::oid[]) WITH ORDINALITY AS x(coll, n)
                     WHERE x.n <= i.indnkeyatts ORDER BY x.n) AS collations,
               ARRAY(SELECT x.opt::int FROM unnest(i.indoption::int2[]) WITH ORDINALITY AS x(opt, n)
                     WHERE x.n <= i.indnkeyatts ORDER BY x.n) AS options,
               pg_relation_size(c.oid) AS size_bytes,
               COALESCE(s.idx_scan, 0) AS scans, {last_scan} AS last_scan
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_am am ON am.oid = c.relam
        LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = c.oid
        WHERE c.relnamespace = current_schema()::regnamespace{only}
        ORDER BY c.relname"""


_FOREIGN_KEYS = """
    SELECT k.conname AS name, t.relname AS table_name, p.relname AS parent,
           ARRAY(SELECT quote_ident(a.attname) FROM unnest(k.conkey) WITH ORDINALITY AS x(attnum, n)
                 JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = x.attnum ORDER BY x.n) AS columns
    FROM pg_constraint k JOIN pg_class t ON t.oid = k.conrelid JOIN pg_class p ON p.oid = k.confrelid
    WHERE k.contype = 'f' AND t.relnamespace = current_schema()::regnamespace"""


async def collect(pool, dataset_id: Optional[int]) -> Dict[str, Any]:
    """Everything a review needs, read in one repeatable-read snapshot. Catalog and statistics reads only:
    no table is scanned. A lock wait (an index being dropped) times out instead of hanging the review."""
    async with pool.acquire(timeout=5) as connection:
        async with connection.transaction(isolation='repeatable_read', readonly=True):
            await connection.execute("SET LOCAL lock_timeout = '5s'; SET LOCAL statement_timeout = '60s'")
            head = await connection.fetchrow(
                """SELECT clock_timestamp() AS now,
                          GREATEST((SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()),
                                   (SELECT stats_reset FROM pg_stat_bgwriter)) AS stats_start,
                          (SELECT system_identifier FROM pg_control_system()) AS system_identifier,
                          (SELECT oid::bigint FROM pg_database WHERE datname = current_database()) AS database_oid,
                          current_setting('server_version_num')::int AS server_version""",
            )
            indexes = [dict(row) for row in await connection.fetch(_index_query(head['server_version'] >= 160000))]
            families = sorted({family for index in indexes for family in index['families']})
            operators = {
                row['family']: set(row['operators'])
                for row in await connection.fetch(
                    """SELECT amopfamily::bigint AS family,
                              array_agg(DISTINCT amopopr::text || ':' || amoppurpose::text) AS operators
                       FROM pg_amop WHERE amopfamily = ANY($1::oid[]) GROUP BY amopfamily""",
                    families,
                )
            }
            foreign_keys = [dict(row) for row in await connection.fetch(_FOREIGN_KEYS)]
            observations = [
                dict(row)
                for row in await connection.fetch(
                    'SELECT index_oid::bigint AS oid, system_identifier, database_oid::bigint AS database_oid, '
                    'first_seen_at, last_scans FROM index_observations',
                )
            ]
            snapshot: Dict[str, Any] = {
                'now': head['now'],
                'stats_start': head['stats_start'],
                'system_identifier': head['system_identifier'],
                'database_oid': head['database_oid'],
                'indexes': indexes,
                'operators': operators,
                'foreign_keys': foreign_keys,
                'observations': observations,
                'datasets': {row['id'] for row in await connection.fetch('SELECT id FROM datasets')},
                'dataset': None,
            }
            snapshot['open_removals'] = [
                dict(row)
                for row in await connection.fetch(
                    """SELECT r.id, r.index_name, r.kind, r.dataset_id, r.dropped_at, r.removed_at,
                              j.state AS drop_state,
                              r.kind = 'field' AND NOT EXISTS (
                                  SELECT 1 FROM dataset_fields f
                                  WHERE f.dataset_id = r.dataset_id AND f.field_name = r.field_name) AS field_gone
                       FROM removed_indexes r LEFT JOIN jobs j ON j.id = r.drop_job_id
                       WHERE r.restored_at IS NULL AND r.closed_at IS NULL""",
                )
            ]
            snapshot['removed'] = {row['index_name'] for row in snapshot['open_removals']}
            if dataset_id is None:
                return snapshot
            dataset = await connection.fetchrow('SELECT id, name, deleting FROM datasets WHERE id = $1', dataset_id)
            snapshot['dataset'] = dict(dataset) if dataset is not None else None
            if dataset is None:
                return snapshot
            snapshot['fields'] = [
                dict(row)
                for row in await connection.fetch(
                    """SELECT field_name, field_type, origin, is_promoted FROM dataset_fields
                       WHERE dataset_id = $1 ORDER BY field_name""",
                    dataset_id,
                )
            ]
            # Fields whose index is being rebuilt (index_jobs.queue_rebuilds): getting one, never suggested.
            snapshot['rebuilding'] = {
                row['field_name']
                for row in await connection.fetch(
                    "SELECT field_name FROM ann_index_rebuilds WHERE kind = 'field' AND dataset_id = $1",
                    dataset_id,
                )
            }
            first_day = head['now'].astimezone(timezone.utc).date() - timedelta(days=field_usage.WINDOW_DAYS - 1)
            snapshot['usage'] = [
                dict(row)
                for row in await connection.fetch(
                    """SELECT usage, field_name, day, uses, total_ms, latency_log2 FROM field_usage_daily
                       WHERE dataset_id = $1 AND day >= $2""",
                    dataset_id,
                    first_day,
                )
            ]
            snapshot['build_rates'] = [
                row['rate']
                for row in await connection.fetch(
                    """SELECT (checkpoint->>'build_seconds')::float8
                              / NULLIF((checkpoint->>'table_bytes')::float8, 0) AS rate
                       FROM jobs WHERE kind = 'field_index_build' AND state = 'succeeded'
                         AND checkpoint ? 'build_seconds' AND checkpoint ? 'table_bytes'""",
                )
                if row['rate'] is not None
            ]
            snapshot['samples_bytes'] = await connection.fetchval("SELECT pg_relation_size('samples')")
            return snapshot


# ---------------------------------------------------------------------------------------------------
# Rules (pure functions of a snapshot)


def classify(index: Dict[str, Any]) -> Tuple[str, Optional[int], Optional[str]]:
    """(kind, dataset_id, field) of an index: a dataset's field index, an ANN index, a core index named
    in schema.sql (or backing a constraint), an array-field GIN, or other (hand-made, operator scripts)."""
    match = _FIELD_INDEX.match(index['name'])
    if match and index['table_name'] == 'samples':
        field = _FIELD_EXPRESSION.search(index['definition'] or '')
        return 'field', int(match.group(1)), field.group(1).replace("''", "'") if field else None
    if index['method'] == 'hnsw' or index['name'].startswith('hnsw_emb_'):
        field = re.search(r"field_name = '((?:[^']|'')*)'", index['definition'] or '')
        # Each dataset's vector indexes are on its own embeddings partition (0021).
        partition = re.fullmatch(r'embeddings_d(\d+)', index['table_name'] or '')
        return (
            'ann',
            int(partition.group(1)) if partition else None,
            field.group(1).replace("''", "'") if field else None,
        )
    if index['name'] in core_indexes() or index['constraint_backed']:
        return 'core', None, None
    if index['table_name'] == 'samples' and _ARRAY_INDEX.match(index['name']):
        return 'array', None, None
    return 'other', None, None


def fk_supporters(indexes: List[Dict[str, Any]], key: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The indexes a foreign key's lookups (deleting or updating a parent row) can use: valid, ready, not
    partial, an equality-capable access method, and leading with exactly the key's columns."""
    columns = set(key['columns'])
    return [
        index
        for index in indexes
        if index['table_name'] == key['table_name']
        and index['valid']
        and index['ready']
        and index['predicate'] is None
        and index['method'] in ('btree', 'hash')
        and len(index['keys']) >= len(columns)
        and set(index['keys'][: len(columns)]) == columns
    ]


# Core indexes a database function needs, which an idle spell would otherwise call unused: the parent_id trigger
# probes this one only when images are inserted, and without it every image insert would scan the objects (0029);
# the objects with no image are counted and linked by the other, which is nearly empty and read only by the finder.
FUNCTION_INDEXES = {
    ('samples', 'idx_samples_parent_id'): "finds the objects waiting for a new image (samples.parent_id's trigger)",
    ('samples', 'idx_samples_unlinked_objects'): 'counts and links the objects with no image (the orphan finder)',
}


def needed_reasons(indexes: List[Dict[str, Any]], foreign_keys: List[Dict[str, Any]]) -> Dict[int, List[str]]:
    """oid -> why the index must stay. The only index that can serve a foreign key's lookups is needed:
    without it, deleting or updating a parent row scans the child table."""
    reasons: Dict[int, List[str]] = {}
    for index in indexes:
        own = []
        if (index['table_name'], index['name']) in FUNCTION_INDEXES:
            own.append(FUNCTION_INDEXES[(index['table_name'], index['name'])])
        if index['is_primary']:
            own.append('primary key')
        elif index['is_exclusion']:
            own.append('exclusion constraint')
        elif index['is_unique']:
            own.append('enforces uniqueness')
        elif index['constraint_backed']:
            own.append('backs a constraint')
        if index['replica_identity']:
            own.append('replica identity')
        if own:
            reasons[index['oid']] = own
    for key in foreign_keys:
        supporters = fk_supporters(indexes, key)
        if len(supporters) == 1:
            reasons.setdefault(supporters[0]['oid'], []).append(_fk_reason(key, 'only index for'))
    return reasons


def _fk_reason(key: Dict[str, Any], how: str) -> str:
    return (
        f"{how} foreign key {key['name']} ({', '.join(key['columns'])} → {key['parent']}): "
        f"without it, deleting a {key['parent']} row scans {key['table_name']}"
    )


def _same_order(a: List[int], b: List[int]) -> bool:
    """Equal sort options, or all reversed (one index read backwards gives the other's order)."""
    return a == b or (len(a) == len(b) and all((x ^ y) == INDOPTION_REVERSED for x, y in zip(a, b)))


def redundancy(a: Dict[str, Any], b: Dict[str, Any], operators: Dict[int, Set[str]]) -> Optional[Tuple[str, str]]:
    """(rule, reason) when index `a` is redundant to index `b` (same table, access method and predicate):
    a duplicate (the same keys, operator families, collations and sort options, and every column `a`
    carries in INCLUDE is also in `b`); a B-tree whose keys lead `b`'s; or the same keys with an operator
    class whose operators are a subset of `b`'s (jsonb_path_ops within jsonb_ops). A unique index is never
    redundant to a non-unique one. None otherwise."""
    if a['is_unique'] and not b['is_unique']:
        return None
    if not set(a['include']) <= set(b['keys']) | set(b['include']):
        return None  # `a` covers columns `b` does not: its index-only scans would be lost

    def ops(family: int) -> Set[str]:
        return operators.get(family, set())

    count = len(a['keys'])
    same_keys = (
        a['keys'] == b['keys'] and a['collations'] == b['collations'] and _same_order(a['options'], b['options'])
    )
    if same_keys and a['families'] == b['families']:
        if set(a['include']) == set(b['include']):
            return 'duplicate', f"Same columns, operators, order and predicate as {b['name']}"
        return 'duplicate', f"Same key as {b['name']}, which also carries every column this one includes"
    if same_keys and all(ops(x) <= ops(y) for x, y in zip(a['families'], b['families'])):
        return 'operators', (
            f"Same columns as {b['name']}, whose {b['method']} operator class supports every operator this one "
            'does: queries it serves can use the other (possibly a little slower)'
        )
    if (
        a['method'] == 'btree'
        and count < len(b['keys'])
        and b['keys'][:count] == a['keys']
        and b['families'][:count] == a['families']
        and b['collations'][:count] == a['collations']
        and _same_order(a['options'], b['options'][:count])
    ):
        return 'prefix', (
            f"Its columns ({', '.join(a['keys'])}) lead {b['name']} ({', '.join(b['keys'])}), which serves the same "
            'searches (reading a larger index)'
        )
    return None


def overlaps(
    indexes: List[Dict[str, Any]],
    operators: Dict[int, Set[str]],
    needed: Dict[int, List[str]],
) -> List[Dict[str, Any]]:
    """Structural pairs (redundant, kept): see `redundancy`. Identical indexes keep the needed, unique or
    more-used one. The kept index is always one that is redundant to nothing (so removing every
    redundant index of a group leaves its keeper), the most used of those."""
    usable = [index for index in indexes if index['valid'] and index['ready'] and not index['building']]
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for index in usable:
        groups.setdefault((index['table_name'], index['method'], index['predicate'] or ''), []).append(index)
    rank = {'duplicate': 0, 'operators': 1, 'prefix': 2}

    def worth(index):  # which of two identical indexes to keep
        return (index['oid'] in needed, index['is_unique'], index['scans'], -index['size_bytes'], index['name'])

    candidates: Dict[int, List[Tuple[Dict[str, Any], str, str]]] = {}
    for group in groups.values():
        for a in group:
            for b in group:
                if a['oid'] == b['oid']:
                    continue
                found = redundancy(a, b, operators)
                if found is None:
                    continue
                mutual = redundancy(b, a, operators) is not None
                if mutual and worth(a) > worth(b):
                    continue  # equivalent: only the less worth keeping one is redundant
                candidates.setdefault(a['oid'], []).append((b, *found))
    pairs = []
    by_oid = {index['oid']: index for index in usable}
    for oid, options in candidates.items():
        if oid in needed:
            continue
        keepers = [option for option in options if option[0]['oid'] not in candidates]
        if not keepers:
            continue
        kept, rule, reason = min(
            keepers,
            key=lambda option: (rank[option[1]], -option[0]['scans'], option[0]['size_bytes'], option[0]['name']),
        )
        pairs.append({'redundant': by_oid[oid], 'kept': kept, 'rule': rule, 'reason': reason})
    return sorted(pairs, key=lambda pair: -pair['redundant']['size_bytes'])


def first_seen(snapshot: Dict[str, Any]) -> Dict[int, datetime]:
    """oid -> when the advisor first observed the index in this database of this cluster. Never back-dated:
    an index without an observation row (new, or the database was restored: other OIDs, another cluster
    or database identity) is first seen now, and so is one whose usage counter went backwards (a
    per-table statistics reset)."""
    now = snapshot['now']
    mine = {
        row['oid']: row
        for row in snapshot['observations']
        if row['system_identifier'] == snapshot['system_identifier'] and row['database_oid'] == snapshot['database_oid']
    }
    seen: Dict[int, datetime] = {}
    for index in snapshot['indexes']:
        row = mine.get(index['oid'])
        if row is None or (row['last_scans'] is not None and index['scans'] < row['last_scans']):
            seen[index['oid']] = now
        else:
            seen[index['oid']] = row['first_seen_at']
    return seen


def percentile(histogram: List[int], fraction: float) -> Optional[float]:
    """The upper edge (ms) of the log2 bucket holding that fraction of requests."""
    total = sum(histogram)
    if total <= 0:
        return None
    running = 0
    for bucket, count in enumerate(histogram):
        running += count
        if running >= fraction * total:
            return float(2**bucket)
    return float(2 ** (len(histogram) - 1))


def _usage_totals(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    totals: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        entry = totals.setdefault(
            (row['usage'], row['field_name']),
            {'uses': 0, 'total_ms': 0.0, 'histogram': [0] * field_usage.BUCKETS},
        )
        entry['uses'] += row['uses']
        entry['total_ms'] += row['total_ms']
        for bucket, count in enumerate(row['latency_log2'][: field_usage.BUCKETS]):
            entry['histogram'][bucket] += count
    return totals


def build_estimate(snapshot: Dict[str, Any]) -> Optional[float]:
    """Seconds a field index build takes here now: the median measured seconds per byte of the samples
    heap (CREATE INDEX CONCURRENTLY scans the whole heap twice, whatever the dataset's share; waiting for
    other transactions is not counted) times the heap's current size. None until a build is measured."""
    rates = snapshot.get('build_rates') or []
    if not rates or not snapshot.get('samples_bytes'):
        return None
    ordered = sorted(rates)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return median * snapshot['samples_bytes']


def suggestions(snapshot: Dict[str, Any], field_indexes: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fields worth indexing in this dataset (see the module docstring for the rule). A request's time is
    shared among the fields it filters by (field_usage.record), so its waiting is counted once."""
    totals = _usage_totals(snapshot.get('usage') or [])
    fields = {row['field_name']: row for row in snapshot.get('fields') or []}
    indexed = {name for name, index in field_indexes.items() if index['valid'] and index['ready']}
    reference: Dict[str, Optional[float]] = {}
    for usage in field_usage.USAGES:
        rows = [entry for (kind, name), entry in totals.items() if kind == usage and name in indexed]
        if not any(entry['uses'] for entry in rows):
            rows = [entry for (kind, _), entry in totals.items() if kind == usage]
        uses = sum(entry['uses'] for entry in rows)
        reference[usage] = sum(entry['total_ms'] for entry in rows) / uses if uses else None
    build_seconds = build_estimate(snapshot)
    rebuilding = snapshot.get('rebuilding') or set()
    found = []
    for name, field in fields.items():
        if field['origin'] != 'metadata' or field['field_type'] not in INDEXABLE_TYPES or name in indexed:
            continue
        if name in rebuilding:
            continue  # its index is being rebuilt in the background
        if not promotable_name(name):
            continue  # promote refuses it: nothing to offer
        missing = bool(field['is_promoted'])
        usages, excess_ms, clearly_slower = {}, 0.0, False
        for usage in field_usage.USAGES:
            entry = totals.get((usage, name))
            if not entry or not entry['uses']:
                continue
            mean = entry['total_ms'] / entry['uses']
            ref = reference[usage]
            extra = max(0.0, (mean - ref) * entry['uses']) if ref is not None else 0.0
            excess_ms += extra
            # At least twice as slow as the reference, on the measured means.
            clearly_slower = clearly_slower or (ref is not None and mean >= 2 * ref)
            usages[usage] = {
                'uses': entry['uses'],
                'mean_ms': round(mean, 1),
                'p50_ms': percentile(entry['histogram'], 0.5),
                'p95_ms': percentile(entry['histogram'], 0.95),
                'reference_ms': None if ref is None else round(ref, 1),
                'slower': ref is not None and mean > ref,
            }
        slow = any(item['slower'] for item in usages.values())
        excess = excess_ms / 1000.0
        pays_back = None if build_seconds is None else excess >= build_seconds
        # Unmeasured build time: no "often enough" test is possible, so ask for a clear difference.
        worth = pays_back is True or (pays_back is None and clearly_slower and excess > 0)
        if missing or (slow and worth):
            found.append(
                {
                    'field': name,
                    'type': field['field_type'],
                    'reason': 'index_missing' if missing else 'slow',
                    'usages': usages,
                    'excess_seconds': round(excess, 1),
                    'build_seconds': None if build_seconds is None else round(build_seconds, 1),
                    'pays_back': pays_back,
                },
            )
    return sorted(found, key=lambda item: (item['reason'] != 'index_missing', -item['excess_seconds'], item['field']))


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def build_review(snapshot: Dict[str, Any], dataset_id: Optional[int]) -> Tuple[Dict[str, Any], Dict[int, datetime]]:
    """The review result for one scope, and the first-seen times to record. Verdicts are computed for
    every index together (an overlap's keeper and a foreign key's last index are never offered), then
    filtered to the scope: a dataset's field indexes, or every index no live dataset owns."""
    now, start = snapshot['now'], snapshot['stats_start'] or snapshot['now']
    indexes = snapshot['indexes']
    by_oid = {index['oid']: index for index in indexes}
    seen = first_seen(snapshot)
    needed = needed_reasons(indexes, snapshot['foreign_keys'])
    kinds = {index['oid']: classify(index) for index in indexes}
    live = snapshot.get('datasets')
    observed = {oid: max(start, seen[oid]) for oid in by_oid}

    status: Dict[int, str] = {}
    idle_days: Dict[int, Optional[float]] = {}
    for index in indexes:
        oid = index['oid']
        if index['building']:
            status[oid] = 'building'
        elif oid in needed:
            status[oid] = 'needed'
        elif not (index['valid'] and index['ready']):
            status[oid] = 'invalid'
        elif index['scans'] and index['last_scan'] is None:
            status[oid] = 'used'  # PostgreSQL before 16 does not say when: never called idle
        else:
            used_since = index['scans'] and index['last_scan'] is not None and index['last_scan'] > observed[oid]
            idle_from = index['last_scan'] if used_since else observed[oid]
            if now - observed[oid] < IDLE_WINDOW:
                status[oid] = 'used' if used_since else 'too_new'
            elif now - idle_from >= IDLE_WINDOW:
                status[oid] = 'idle'
                idle_days[oid] = round((now - idle_from).total_seconds() / 86400, 1)
            else:
                status[oid] = 'used'

    pairs = [
        pair
        for pair in overlaps(indexes, snapshot['operators'], needed)
        # Never "remove both": the kept index must not be offered itself, and must be used at least as much.
        if status[pair['kept']['oid']] in ('needed', 'used', 'too_new')
        and (pair['kept']['oid'] in needed or pair['kept']['scans'] >= pair['redundant']['scans'])
    ]
    removable = {oid for oid, verdict in status.items() if verdict in ('idle', 'invalid')}
    removable |= {pair['redundant']['oid'] for pair in pairs}
    for key in snapshot['foreign_keys']:
        supporters = fk_supporters(indexes, key)
        if supporters and all(index['oid'] in removable for index in supporters):
            best = max(supporters, key=lambda index: (index['scans'], -index['size_bytes'], index['name']))
            needed.setdefault(best['oid'], []).append(_fk_reason(key, 'last index left for'))
            status[best['oid']] = 'needed'
            removable.discard(best['oid'])
            pairs = [pair for pair in pairs if pair['redundant']['oid'] != best['oid']]

    def in_scope(index) -> bool:
        kind, owner, _ = kinds[index['oid']]
        if dataset_id is not None:
            return kind == 'field' and owner == dataset_id
        return kind != 'field' or (live is not None and owner not in live)

    def entry(index) -> Dict[str, Any]:
        kind, owner, field = kinds[index['oid']]
        return {
            'name': index['name'],
            'table': index['table_name'],
            'kind': kind,
            'method': index['method'],
            'dataset_id': owner,
            'field': field,
            'orphan': kind == 'field' and live is not None and owner not in live,
            'definition': index['definition'],
            'size_bytes': index['size_bytes'],
            'scans': index['scans'],
            'last_scan': _iso(index['last_scan']),
            'observed_since': _iso(observed[index['oid']]),
            'valid': index['valid'],
            'needed': needed.get(index['oid'], []),
        }

    unused, too_new, building, kept = [], [], [], []
    for index in indexes:
        if not in_scope(index):
            continue
        verdict = status[index['oid']]
        if verdict == 'building':
            building.append(entry(index))
        elif verdict == 'needed':
            kept.append(entry(index))
        elif verdict == 'invalid':
            unused.append({**entry(index), 'invalid': True, 'idle_days': None})
        elif verdict == 'idle':
            unused.append({**entry(index), 'invalid': False, 'idle_days': idle_days[index['oid']]})
        elif verdict == 'too_new':
            too_new.append(entry(index))
    unused.sort(key=lambda item: -item['size_bytes'])
    history_since = max(start, min(seen.values())) if seen else start
    review: Dict[str, Any] = {
        'scope': 'dataset' if dataset_id is not None else 'system',
        'dataset_id': dataset_id,
        'reviewed_at': _iso(now),
        'window_days': REVIEW_PERIOD.days,
        'idle_days_required': IDLE_WINDOW.days,
        'stats_since': _iso(start),
        'history_since': _iso(history_since),
        'enough_history': now - history_since >= IDLE_WINDOW,
        'possibly_unused': unused,
        'too_new': too_new,
        'overlapping': [
            {
                'redundant': entry(pair['redundant']),
                'kept': entry(pair['kept']),
                'rule': pair['rule'],
                'reason': pair['reason'],
            }
            for pair in pairs
            if in_scope(pair['redundant']) or in_scope(pair['kept'])
        ],
        'building': building,
    }
    if dataset_id is None:
        present = {index['name'] for index in indexes}
        review['needed'] = kept
        review['missing_core'] = [
            {'name': name, 'table': table}
            for name, table in sorted(core_indexes().items())
            if name not in present and name not in snapshot.get('removed', set())
        ]
    else:
        field_indexes = {}
        for index in indexes:
            kind, owner, field = kinds[index['oid']]
            if kind == 'field' and owner == dataset_id and field:
                field_indexes[field] = index
        days = sorted({row['day'] for row in snapshot.get('usage') or []})
        review['telemetry_since'] = days[0].isoformat() if days else None
        review['suggested'] = suggestions(snapshot, field_indexes)
        review['build_seconds'] = build_estimate(snapshot)
    return review, seen


def stale_removals(snapshot: Dict[str, Any]) -> List[Tuple[Dict[str, Any], str]]:
    """Open removal records whose index is back: re-created another way (an operator script, a restore
    into a database initialised from schema.sql, CREATE INDEX by hand), or a removal stopped before its
    drop. The review closes them, so nothing silently brings a removed index back unnoticed."""
    present = {index['name'] for index in snapshot['indexes'] if index['valid'] and index['ready']}
    stale = []
    for removal in snapshot.get('open_removals') or []:
        if removal['drop_state'] in ('queued', 'running'):
            continue
        if removal['index_name'] not in present:
            if removal.get('field_gone'):
                # Renamed, cleared or removed since: there is no field to index again.
                stale.append((removal, 'its field was renamed, cleared or removed; nothing to restore'))
            continue
        if removal['dropped_at'] is not None:
            stale.append((removal, 're-created outside the advisor'))
        else:
            stale.append((removal, 'removal stopped before the drop; the index is still there'))
    return stale


def summary(review: Dict[str, Any]) -> str:
    """A progress message safe for every viewer of the dataset."""
    parts = []
    if 'suggested' in review:
        parts.append(f"{len(review['suggested'])} suggested")
    parts.append(f"{len(review['possibly_unused'])} possibly unused")
    parts.append(f"{len(review['overlapping'])} overlapping")
    return 'Index review: ' + ', '.join(parts)


async def save_observations(connection, snapshot: Dict[str, Any], seen: Dict[int, datetime]) -> None:
    """Record first-seen times for this database's indexes; forget rows of other clusters or databases
    (a restored dump's) and of dropped indexes."""
    cluster, database = snapshot['system_identifier'], snapshot['database_oid']
    names = {index['oid']: index['name'] for index in snapshot['indexes']}
    scans = {index['oid']: index['scans'] for index in snapshot['indexes']}
    oids = sorted(seen)
    await connection.execute(
        'DELETE FROM index_observations WHERE system_identifier <> $1 OR database_oid <> $2',
        cluster,
        database,
    )
    await connection.execute(
        'DELETE FROM index_observations WHERE NOT (index_oid = ANY($1::oid[]))',
        oids,
    )
    await connection.execute(
        """INSERT INTO index_observations (index_oid, system_identifier, database_oid, index_name, first_seen_at,
               last_scans)
           SELECT o, $1, $2, n, f, s
           FROM unnest($3::oid[], $4::text[], $5::timestamptz[], $6::bigint[]) AS x(o, n, f, s)
           ORDER BY o
           ON CONFLICT (index_oid, system_identifier, database_oid) DO UPDATE SET index_name = EXCLUDED.index_name,
               first_seen_at = EXCLUDED.first_seen_at, last_scans = EXCLUDED.last_scans,
               updated_at = clock_timestamp()""",
        cluster,
        database,
        oids,
        [names[oid] for oid in oids],
        [seen[oid] for oid in oids],
        [scans[oid] for oid in oids],
    )


# ---------------------------------------------------------------------------------------------------
# The job


def scope_args(dataset_id: Optional[int]) -> Dict[str, Any]:
    return {'scope': 'dataset', 'dataset_id': dataset_id} if dataset_id is not None else {'scope': 'system'}


async def submit(pool, dataset_id: Optional[int], user_id: Optional[int]) -> Tuple[Dict[str, Any], bool]:
    """Queue a review of a dataset (or of the system), or return the one already queued or running for
    that scope, whoever asked. (job, created)."""
    args = scope_args(dataset_id)
    async with job_queue._transaction(pool) as connection:
        if dataset_id is not None:
            await connection.execute('SELECT require_writable_dataset($1)', dataset_id)
        await connection.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
            json.dumps(['index-review', args], sort_keys=True),
        )
        existing = await connection.fetchrow(
            f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
                WHERE kind = $1 AND args = $2::jsonb AND state IN ('queued', 'running') AND NOT cancel_requested
                ORDER BY created_at LIMIT 1""",
            KIND,
            args,
        )
        if existing is not None:
            return dict(existing), False
        job = await job_queue.enqueue_job(connection, KIND, args=args, lane='user', created_by=user_id)
        return job, True


def handler(pool):
    async def measure(job):
        args = job['args'] or {}
        dataset_id = args.get('dataset_id') if args.get('scope') == 'dataset' else None
        snapshot = await collect(pool, dataset_id)
        if dataset_id is not None and (snapshot['dataset'] is None or snapshot['dataset']['deleting']):
            return StepResult(
                finished=True,
                result={**scope_args(dataset_id), 'skipped': 'The dataset was deleted'},
                message='Index review skipped: the dataset was deleted',
            )
        review, seen = build_review(snapshot, dataset_id)
        # Only this scope's records: a dataset review never closes a system record it does not report.
        stale = [
            (removal, note)
            for removal, note in stale_removals(snapshot)
            if (dataset_id is not None and removal['kind'] == 'field' and removal['dataset_id'] == dataset_id)
            or (dataset_id is None and removal['kind'] != 'field')
        ]
        review['recreated'] = [
            {'index_name': removal['index_name'], 'removed_at': _iso(removal['removed_at']), 'note': note}
            for removal, note in stale
        ]

        async def publish(connection):
            await save_observations(connection, snapshot, seen)
            for removal, note in stale:
                await connection.execute(
                    """UPDATE removed_indexes SET closed_at = clock_timestamp(), closed_note = $2
                       WHERE id = $1 AND restored_at IS NULL AND closed_at IS NULL""",
                    removal['id'],
                    note,
                )

        return StepResult(finished=True, result=review, message=summary(review), publish=publish)

    return measure


async def latest(connection, dataset_id: Optional[int]) -> Dict[str, Any]:
    """The newest review of a scope, the one queued or running now, and the newest that ended without a
    result (failed or cancelled), for the advisor's views. Read in one snapshot: a review that finished
    between the queries would show as neither running nor done, and the views stop polling."""
    args = scope_args(dataset_id)
    rows = {}
    async with connection.transaction(isolation='repeatable_read', readonly=True):
        for name, condition in (
            ('review', "state = 'succeeded'"),
            ('active', "state IN ('queued', 'running')"),
            ('last', "state IN ('succeeded', 'failed', 'cancelled')"),
        ):
            rows[name] = await connection.fetchrow(
                f"""SELECT id, state, result, error, message, created_at, started_at, finished_at FROM jobs
                    WHERE kind = $1 AND args = $2::jsonb AND {condition} ORDER BY created_at DESC LIMIT 1""",
                KIND,
                args,
            )
    review = rows['review']
    active = rows['active']
    last = rows['last']
    return {
        'review': review['result'] if review is not None else None,
        'review_job': None if review is None else {'id': str(review['id']), 'finished_at': _iso(review['finished_at'])},
        'active_job': None
        if active is None
        else {'id': str(active['id']), 'state': active['state'], 'created_at': _iso(active['created_at'])},
        'last_failure': None
        if last is None or last['state'] == 'succeeded'
        else {
            'id': str(last['id']),
            'state': last['state'],
            'finished_at': _iso(last['finished_at']),
            'message': (last['error'] or {}).get('message') if isinstance(last['error'], dict) else None,
        },
    }
