"""A follower's sync (docs/FOLLOWERS.md): copy each dataset the primary assigns this instance, then apply the
primary's change feed to the copy and acknowledge it, so the primary can prune its log.

One job per primary link, queued every FOLLOWER_SYNC_SECONDS when none is waiting or running. Each step is one
bounded unit (the config, a few blocks, or part of a feed page) committed together with the mirror's progress, so a
restart resumes where it stopped. A mirror is written only here, in transactions that set fridge.mirror_sync to its
dataset id (migration 0009); everything the primary didn't send stays the follower's own.
"""

import base64
import io
import logging
import struct
from urllib.parse import urlparse

import asyncpg
import httpx
import numpy as np
from fastapi import HTTPException

from . import job_queue
from .crypto import decrypt_secret
from .diskspace import INGEST_ROW_BYTES, require_free_space
from .field_registry import lock_field_lifecycle
from .job_runner import StepResult
from .routers import datasets as datasets_router
from .routers import followers

logger = logging.getLogger(__name__)

KIND = 'follower_sync'
# A step can re-rank a whole map, as the primary's own re-rank does (up to 1800 s there).
POLICY = job_queue.JobPolicy(max_unit_seconds=1800, max_failures=5, retry_seconds=30, max_retry_seconds=900)
RANK_SECONDS = 1800
BLOCK = followers.BLOCK
COPY_BLOCKS = 2  # blocks per step: up to 8192 samples and their vectors
REQUEST_SECONDS = 120
LOCK_SECONDS = 5
SAMPLE_COLUMNS = (
    ('id', 'integer'),
    ('filepath', 'text'),
    ('media_type', 'text'),
    ('tags', 'text[]'),
    ('metadata', 'jsonb'),
    ('lon', 'float8'),
    ('lat', 'float8'),
    ('heading', 'float8'),
    ('created_at', 'timestamptz'),
    ('last_modified_at', 'timestamptz'),
)
COPIED = ('filepath', 'media_type', 'tags', 'metadata', 'heading', 'created_at', 'last_modified_at')
CONCEPT_OPERATORS = ('concept_select', 'concept_exclude')


class PrimaryUnavailable(job_queue.QueueBusy):
    """The primary couldn't be reached or failed: the step is retried with backoff."""


class PrimaryRefused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f'The primary answered {status}: {detail}')
        self.status, self.detail = status, detail


def connect(link) -> httpx.AsyncClient:
    """A client for the primary's API; tests bind one to the app instead."""
    return httpx.AsyncClient(
        base_url=link['url'].rstrip('/'),
        headers={'Authorization': f"Bearer {decrypt_secret(link['token_encrypted'])}"},
        timeout=REQUEST_SECONDS,
    )


def _detail(response) -> str:
    try:
        detail = response.json().get('detail')
    except ValueError:
        detail = None
    return detail if isinstance(detail, str) else response.text[:300]


async def call(client, method: str, path: str, *, params=None, body=None):
    try:
        response = await client.request(method, path, params=params, json=body)
    except httpx.HTTPError as error:
        raise PrimaryUnavailable(f'The primary could not be reached: {error}') from error
    if response.status_code >= 500 or response.status_code == 429:
        raise PrimaryUnavailable(f'The primary answered {response.status_code}: {_detail(response)}')
    if response.status_code >= 400:
        raise PrimaryRefused(response.status_code, _detail(response))
    return response.json()


async def queue_sync(pool, link_id: int) -> None:
    """Queue a sync of this link unless one is waiting or running: one job at a time writes a link's mirrors, and a
    running one that has caught up picks later changes up on its next run."""
    busy = await pool.fetchval(
        """SELECT EXISTS (SELECT 1 FROM jobs WHERE kind = $1 AND state IN ('queued', 'running')
                          AND args->>'link_id' = $2)""",
        KIND,
        str(link_id),
    )
    if not busy:
        await job_queue.enqueue_job(
            pool,
            KIND,
            lane='maintenance',
            args={'link_id': link_id},
            dedupe_key=f'link:{link_id}',
        )


async def schedule_sync(pool) -> None:
    """Queue a sync for each primary link."""
    for link in await pool.fetch('SELECT id FROM primary_links ORDER BY id'):
        await queue_sync(pool, link['id'])


# --- What a mirror holds -------------------------------------------------------------------------------------------


def uses_concept(node) -> bool:
    if isinstance(node, dict):
        return node.get('operator') in CONCEPT_OPERATORS or any(uses_concept(value) for value in node.values())
    if isinstance(node, list):
        return any(uses_concept(value) for value in node)
    return False


def sample_ids_in(node, found: set) -> set:
    """The sample ids a view's filters name (`id in [...]` from a brushed selection, `id = n`)."""
    if isinstance(node, dict):
        if node.get('field') == 'id' and node.get('operator') in ('in', 'not in', '=', '!='):
            value = node.get('value')
            found.update(v for v in (value if isinstance(value, list) else [value]) if isinstance(v, int))
        for value in node.values():
            sample_ids_in(value, found)
    elif isinstance(node, list):
        for value in node:
            sample_ids_in(value, found)
    return found


def translate_ids(node, mapping: dict):
    """A view's filters with the primary's sample ids replaced by this instance's. A sample that isn't here matches
    nothing either way: it leaves a list, and a single id becomes 0, which no sample has."""
    if isinstance(node, dict):
        node = {key: translate_ids(value, mapping) for key, value in node.items()}
        if node.get('field') == 'id' and node.get('operator') in ('in', 'not in', '=', '!='):
            value = node.get('value')
            if isinstance(value, list):
                node['value'] = [mapping[v] for v in value if v in mapping]
            elif isinstance(value, int):
                node['value'] = mapping.get(value, 0)
        return node
    if isinstance(node, list):
        return [translate_ids(value, mapping) for value in node]
    return node


def vector_payload(rows: list) -> bytes:
    """PostgreSQL binary COPY rows (sample_id integer, vector) for mirror_vector_stage, all of one dimension."""
    dimension = len(rows[0][1])
    row = np.dtype(
        [
            ('fields', '>i2'),
            ('id_length', '>i4'),
            ('id', '>i4'),
            ('vector_length', '>i4'),
            ('dimension', '>i2'),
            ('unused', '>i2'),
            ('vector', '>f4', (dimension,)),
        ],
    )
    data = np.empty(len(rows), dtype=row)
    data['fields'] = 2
    data['id_length'] = 4
    data['id'] = [sample_id for sample_id, _ in rows]
    data['vector_length'] = 4 + 4 * dimension
    data['dimension'] = dimension
    data['unused'] = 0
    data['vector'] = np.stack([vector for _, vector in rows])
    return b'PGCOPY\n\xff\r\n\x00' + struct.pack('>ii', 0, 0) + data.tobytes() + struct.pack('>h', -1)


def decode_vectors(entries: list) -> dict:
    """{field: [(origin sample id, float32 vector)]} from the primary's base64 little-endian rows."""
    fields: dict = {}
    for entry in entries:
        vector = np.frombuffer(base64.b64decode(entry['vector']), dtype='<f4')
        fields.setdefault(entry['field_name'], []).append((entry['sample_id'], vector))
    return fields


async def local_name(source, name: str, primary_host: str, origin_id: int, dataset_id=None) -> str:
    """The primary's name, or with "(from host)" when a dataset here already has it."""
    for candidate in (name, f'{name} (from {primary_host})', f'{name} (from {primary_host}, {origin_id})'):
        taken = await source.fetchval(
            'SELECT 1 FROM datasets WHERE NOT deleting AND lower(name) = lower($1) AND id IS DISTINCT FROM $2',
            candidate,
            dataset_id,
        )
        if not taken:
            return candidate
    raise ValueError(f'No free name for the mirror of {name!r}')


async def fetch_blocks(client, origin: int, blocks: list) -> dict:
    merged: dict = {'samples': [], 'hashes': [], 'array_fields': []}
    for start in range(0, len(blocks), followers.SAMPLE_BLOCKS):
        part = await call(
            client,
            'POST',
            f'/follower/datasets/{origin}/blocks/samples',
            body={'blocks': blocks[start : start + followers.SAMPLE_BLOCKS]},
        )
        merged['samples'] += part['samples']
        merged['hashes'] += part['hashes']
        merged['array_fields'] = sorted(set(merged['array_fields']) | set(part['array_fields']))
    return merged


async def fetch_coordinates(client, origin: int, blocks: list) -> list:
    found: list = []
    for start in range(0, len(blocks), followers.SAMPLE_BLOCKS):
        part = await call(
            client,
            'POST',
            f'/follower/datasets/{origin}/coordinates',
            body={'blocks': blocks[start : start + followers.SAMPLE_BLOCKS]},
        )
        found += part['coordinates']
    return found


async def fetch_vectors(client, origin: int, sample_ids: list, fields=None) -> list:
    found: list = []
    for start in range(0, len(sample_ids), followers.VECTOR_SAMPLES):
        part = await call(
            client,
            'POST',
            f'/follower/datasets/{origin}/vectors',
            body={'sample_ids': sample_ids[start : start + followers.VECTOR_SAMPLES], 'fields': fields or []},
        )
        found += part['vectors']
    return found


async def ensure_array_columns(pool, dataset_id: int, names: list) -> None:
    """The primary's array fields need their physical column here before samples carrying them are written."""
    if not names:
        return
    existing = {row['column_name'] for row in await pool.fetch('SELECT column_name FROM sample_array_columns')}
    for name in names:
        if name in existing:
            continue
        if not datasets_router._SAFE_COLUMN_RE.match(name) or name in datasets_router._RESERVED_COLUMNS:
            raise ValueError(f'The primary sent an array field this instance cannot hold: {name!r}')
        try:
            await datasets_router.add_array_column(pool, dataset_id, name)
        except HTTPException as error:
            if error.status_code == 409:
                raise job_queue.QueueContended(error.detail) from None
            raise


async def begin_mirror_write(connection, dataset_id: int) -> None:
    """Open this transaction to writing the mirror, and take the shared field-lifecycle lock every embedding writer
    takes (a map swap holds it exclusively, briefly)."""
    await connection.execute("SELECT set_config('fridge.mirror_sync', $1, TRUE)", str(dataset_id))
    try:
        await connection.execute(f"SET LOCAL lock_timeout = '{LOCK_SECONDS}s'")
        await lock_field_lifecycle(connection, dataset_id)
    except asyncpg.LockNotAvailableError:
        raise job_queue.QueueContended('A map is being swapped in on this mirror; syncing after it') from None


def _record_type(arrays: list) -> str:
    columns = [f'{name} {kind}' for name, kind in SAMPLE_COLUMNS] + [f'"{name}" text[]' for name in arrays]
    return ', '.join(columns)


async def write_samples(connection, dataset_id: int, blocks: list, data: dict) -> list:
    """Make the mirror's samples in these blocks the primary's: delete what it no longer has, update what changed,
    insert what's new, and translate object samples' parent ids. Returns the origin ids present."""
    arrays = data['array_fields']
    samples = data['samples']
    present = [sample['id'] for sample in samples]
    await connection.execute(
        f"""DELETE FROM samples s USING mirror_samples m, unnest($2::bigint[]) b
            WHERE m.dataset_id = $1 AND s.id = m.sample_id
              AND m.origin_id >= b * {BLOCK} AND m.origin_id < (b + 1) * {BLOCK}
              AND NOT (m.origin_id = ANY($3::int[]))""",
        dataset_id,
        blocks,
        present,
    )
    if not samples:
        return present
    # A filepath the primary moved here from another sample (a version restore recreating a deleted sample, say):
    # that sample's own block, which changed too, brings it back. Until then it would break UNIQUE (dataset, filepath).
    await connection.execute(
        """DELETE FROM samples s USING mirror_samples m, unnest($2::int[], $3::text[]) AS r(origin_id, filepath)
           WHERE m.dataset_id = $1 AND s.id = m.sample_id AND s.filepath = r.filepath AND m.origin_id <> r.origin_id""",
        dataset_id,
        present,
        [sample['filepath'] for sample in samples],
    )
    source = f"""
        incoming AS (
            SELECT r.*, CASE WHEN r.metadata->>'parent_id' ~ '^-?[0-9]+$'
                             THEN (r.metadata->>'parent_id')::int END AS origin_parent
            FROM jsonb_to_recordset($2::jsonb) AS r({_record_type(arrays)})
        ),
        translated AS (
            SELECT i.*,
                   CASE WHEN i.origin_parent IS NULL THEN COALESCE(i.metadata, '{{}}'::jsonb)
                        ELSE jsonb_set(i.metadata, '{{parent_id}}', COALESCE(to_jsonb(p.sample_id), 'null'::jsonb))
                   END AS local_metadata,
                   CASE WHEN i.lon IS NULL OR i.lat IS NULL THEN NULL
                        ELSE ST_SetSRID(ST_MakePoint(i.lon, i.lat), 4326) END AS local_location
            FROM incoming i LEFT JOIN mirror_samples p ON p.dataset_id = $1 AND p.origin_id = i.origin_parent
        )"""
    array_set = ''.join(f', "{name}" = COALESCE(t."{name}", \'{{}}\')' for name in arrays)
    array_old = ''.join(f', s."{name}"' for name in arrays)
    array_new = ''.join(f', COALESCE(t."{name}", \'{{}}\')' for name in arrays)
    array_fresh = ''.join(f', COALESCE(fresh."{name}", \'{{}}\')' for name in arrays)
    rows = samples  # the connection's jsonb codec encodes it
    await connection.execute(
        f"""WITH {source}
            UPDATE samples s SET filepath = t.filepath, media_type = t.media_type, tags = COALESCE(t.tags, '{{}}'),
                   metadata = t.local_metadata, location = t.local_location, heading = t.heading,
                   created_at = t.created_at, last_modified_at = t.last_modified_at{array_set}
            FROM translated t JOIN mirror_samples m ON m.dataset_id = $1 AND m.origin_id = t.id
            WHERE s.id = m.sample_id
              AND (s.filepath, s.media_type, s.tags, s.metadata, s.lon, s.lat, s.heading, s.created_at,
                   s.last_modified_at{array_old})
                  IS DISTINCT FROM (t.filepath, t.media_type, COALESCE(t.tags, '{{}}'), t.local_metadata, t.lon, t.lat,
                                    t.heading, t.created_at, t.last_modified_at{array_new})""",
        dataset_id,
        rows,
    )
    await connection.execute(
        f"""WITH {source}
            UPDATE mirror_samples m SET origin_parent_id = t.origin_parent
            FROM translated t WHERE m.dataset_id = $1 AND m.origin_id = t.id
              AND m.origin_parent_id IS DISTINCT FROM t.origin_parent""",
        dataset_id,
        rows,
    )
    array_columns = ''.join(f', "{name}"' for name in arrays)
    await connection.execute(
        f"""WITH {source},
            fresh AS (
                SELECT t.* FROM translated t
                WHERE NOT EXISTS (SELECT 1 FROM mirror_samples m WHERE m.dataset_id = $1 AND m.origin_id = t.id)
            ),
            inserted AS (
                INSERT INTO samples (dataset_id, filepath, media_type, tags, metadata, location, heading, created_at,
                                     last_modified_at{array_columns})
                SELECT $1, fresh.filepath, fresh.media_type, COALESCE(fresh.tags, '{{}}'), fresh.local_metadata,
                       fresh.local_location, fresh.heading, fresh.created_at, fresh.last_modified_at{array_fresh}
                FROM fresh ORDER BY fresh.id
                RETURNING id, filepath
            )
            INSERT INTO mirror_samples (dataset_id, origin_id, sample_id, origin_parent_id)
            SELECT $1, fresh.id, inserted.id, fresh.origin_parent FROM inserted JOIN fresh USING (filepath)""",
        dataset_id,
        rows,
    )
    # A parent that arrived in this batch, or a child whose parent is here: point the child at the local parent.
    await connection.execute(
        """UPDATE samples s SET metadata = jsonb_set(s.metadata, '{parent_id}', to_jsonb(p.sample_id))
           FROM mirror_samples c
           JOIN mirror_samples p ON p.dataset_id = c.dataset_id AND p.origin_id = c.origin_parent_id
           WHERE c.dataset_id = $1 AND c.sample_id = s.id
             AND (c.origin_id = ANY($2::int[]) OR c.origin_parent_id = ANY($2::int[]))
             AND s.metadata->'parent_id' IS DISTINCT FROM to_jsonb(p.sample_id)""",
        dataset_id,
        present,
    )
    return present


async def write_hashes(connection, dataset_id: int, present: list, hashes: list) -> None:
    """The primary's hashes of these samples; hash fields only this instance has are left alone."""
    fields = sorted({row['field_name'] for row in hashes})
    await connection.execute(
        """UPDATE mirrors SET hash_fields = ARRAY(SELECT DISTINCT unnest(hash_fields || $2::text[]) ORDER BY 1)
           WHERE dataset_id = $1 AND NOT ($2::text[] <@ hash_fields)""",
        dataset_id,
        fields,
    )
    await connection.execute(
        """DELETE FROM sample_hashes h USING mirror_samples m, mirrors r
           WHERE m.dataset_id = $1 AND r.dataset_id = $1 AND h.sample_id = m.sample_id
             AND m.origin_id = ANY($2::int[]) AND h.field_name = ANY(r.hash_fields)
             AND NOT EXISTS (SELECT 1 FROM unnest($3::int[], $4::text[]) AS kept(origin_id, field_name)
                             WHERE kept.origin_id = m.origin_id AND kept.field_name = h.field_name)""",
        dataset_id,
        present,
        [row['sample_id'] for row in hashes],
        [row['field_name'] for row in hashes],
    )
    if hashes:
        await connection.execute(
            """INSERT INTO sample_hashes (sample_id, field_name, hash_hex, hash_bits, prefix)
               SELECT m.sample_id, h.field_name, h.hash_hex, ('x' || h.hash_hex)::bit varying,
                      substring(('x' || h.hash_hex)::bit varying from 1 for 16)::bit(16)
               FROM unnest($2::int[], $3::text[], $4::text[]) AS h(origin_id, field_name, hash_hex)
               JOIN mirror_samples m ON m.dataset_id = $1 AND m.origin_id = h.origin_id
               ON CONFLICT (sample_id, field_name) DO UPDATE
               SET hash_hex = EXCLUDED.hash_hex, hash_bits = EXCLUDED.hash_bits, prefix = EXCLUDED.prefix
               WHERE sample_hashes.hash_hex IS DISTINCT FROM EXCLUDED.hash_hex""",
            dataset_id,
            [row['sample_id'] for row in hashes],
            [row['field_name'] for row in hashes],
            [row['hash_hex'] for row in hashes],
        )


async def write_vectors(connection, dataset_id: int, named: list, vectors: dict) -> None:
    """The primary's vectors of the (origin sample, field) pairs named: upsert what came, delete what didn't. A field
    this instance had on its own becomes the primary's once the primary sends one of that name."""
    await connection.execute(
        """UPDATE mirrors SET vector_fields = ARRAY(SELECT DISTINCT unnest(vector_fields || $2::text[]) ORDER BY 1)
           WHERE dataset_id = $1 AND NOT ($2::text[] <@ vector_fields)""",
        dataset_id,
        sorted(vectors),
    )
    kept = [(sample_id, field) for field, rows in vectors.items() for sample_id, _ in rows]
    await connection.execute(
        """DELETE FROM embeddings e USING mirror_samples m, unnest($2::int[], $3::text[]) AS n(origin_id, field_name)
           WHERE m.dataset_id = $1 AND m.origin_id = n.origin_id AND e.sample_id = m.sample_id
             AND (e.field_name = n.field_name OR n.field_name IS NULL)
             AND e.field_name = ANY((SELECT vector_fields FROM mirrors WHERE dataset_id = $1)::text[])
             AND NOT EXISTS (SELECT 1 FROM unnest($4::int[], $5::text[]) AS k(origin_id, field_name)
                             WHERE k.origin_id = m.origin_id AND k.field_name = e.field_name)""",
        dataset_id,
        [sample_id for sample_id, _ in named],
        [field for _, field in named],
        [sample_id for sample_id, _ in kept],
        [field for _, field in kept],
    )
    if not vectors:
        return
    await require_free_space(connection, len(kept) * INGEST_ROW_BYTES, 'syncing a mirror')
    await connection.execute(
        'CREATE TEMP TABLE IF NOT EXISTS mirror_vector_stage (sample_id integer, vector vector) ON COMMIT DROP',
    )
    for field, rows in sorted(vectors.items()):
        by_dimension: dict = {}
        for sample_id, vector in rows:
            by_dimension.setdefault(len(vector), []).append((sample_id, vector))
        for group in by_dimension.values():
            await connection.execute('TRUNCATE mirror_vector_stage')
            payload = io.BytesIO(vector_payload(group))
            await connection.copy_to_table('mirror_vector_stage', source=payload, format='binary')
            await connection.execute(
                """INSERT INTO embeddings (sample_id, field_name, vector)
                   SELECT m.sample_id, $2, st.vector FROM mirror_vector_stage st
                   JOIN mirror_samples m ON m.dataset_id = $1 AND m.origin_id = st.sample_id
                   ON CONFLICT (sample_id, field_name) DO UPDATE SET vector = EXCLUDED.vector
                   WHERE embeddings.vector IS DISTINCT FROM EXCLUDED.vector""",
                dataset_id,
                field,
            )
    await clear_placement(connection, dataset_id, sorted({sample_id for sample_id, _ in named}))


async def clear_placement(connection, dataset_id: int, origin_ids: list) -> None:
    """A new or changed vector queues its sample for map placement. On a mirror, the primary places the samples of
    its fields and the coordinates come with the feed, so the queue rows go."""
    await connection.execute(
        """DELETE FROM embedding_projection_pending p USING mirror_samples m, mirrors r
           WHERE m.dataset_id = $1 AND r.dataset_id = $1 AND p.sample_id = m.sample_id
             AND m.origin_id = ANY($2::int[]) AND p.field_name = ANY(r.vector_fields)""",
        dataset_id,
        origin_ids,
    )


async def write_coordinates(connection, dataset_id: int, blocks: list, coordinates: list) -> None:
    """The primary's map coordinates of these blocks' samples; maps of fields only this instance has stay its own."""
    await connection.execute(
        f"""DELETE FROM embedding_coordinates c USING mirror_samples m, mirrors r, unnest($2::bigint[]) b
            WHERE m.dataset_id = $1 AND r.dataset_id = $1 AND c.sample_id = m.sample_id
              AND m.origin_id >= b * {BLOCK} AND m.origin_id < (b + 1) * {BLOCK}
              AND c.field_name = ANY(r.vector_fields)
              AND NOT EXISTS (SELECT 1 FROM unnest($3::int[], $4::text[]) AS k(origin_id, field_name)
                              WHERE k.origin_id = m.origin_id AND k.field_name = c.field_name)""",
        dataset_id,
        blocks,
        [row['sample_id'] for row in coordinates],
        [row['field_name'] for row in coordinates],
    )
    if coordinates:
        await connection.execute(
            """INSERT INTO embedding_coordinates AS c (sample_id, field_name, x, y)
               SELECT m.sample_id, n.field_name, n.x, n.y
               FROM unnest($2::int[], $3::text[], $4::float8[], $5::float8[]) AS n(origin_id, field_name, x, y)
               JOIN mirror_samples m ON m.dataset_id = $1 AND m.origin_id = n.origin_id
               JOIN embeddings e ON e.sample_id = m.sample_id AND e.field_name = n.field_name
               ON CONFLICT (sample_id, field_name) DO UPDATE SET x = EXCLUDED.x, y = EXCLUDED.y
               WHERE (c.x, c.y) IS DISTINCT FROM (EXCLUDED.x, EXCLUDED.y)""",
            dataset_id,
            [row['sample_id'] for row in coordinates],
            [row['field_name'] for row in coordinates],
            [row['x'] for row in coordinates],
            [row['y'] for row in coordinates],
        )
    # Clearing a coordinate queues its sample for placement again.
    origin_ids = await connection.fetch(
        f"""SELECT m.origin_id FROM mirror_samples m, unnest($2::bigint[]) b
            WHERE m.dataset_id = $1 AND m.origin_id >= b * {BLOCK} AND m.origin_id < (b + 1) * {BLOCK}""",
        dataset_id,
        blocks,
    )
    await clear_placement(connection, dataset_id, [row['origin_id'] for row in origin_ids])


async def write_fields(connection, dataset_id: int, fields: list) -> None:
    """The primary's field registry. is_promoted stays this instance's: it stands for an index here."""
    await connection.execute(
        'DELETE FROM dataset_fields WHERE dataset_id = $1 AND NOT (field_name = ANY($2::text[]))',
        dataset_id,
        [field['field_name'] for field in fields],
    )
    await connection.execute(
        """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, display_type, overlay_config)
           SELECT $1, f.field_name, f.field_type, f.origin, f.display_type, f.overlay_config
           FROM jsonb_to_recordset($2::jsonb)
                AS f(field_name text, field_type text, origin text, display_type text, overlay_config jsonb)
           ON CONFLICT (dataset_id, field_name) DO UPDATE
           SET field_type = EXCLUDED.field_type, origin = EXCLUDED.origin, display_type = EXCLUDED.display_type,
               overlay_config = EXCLUDED.overlay_config
           WHERE (dataset_fields.field_type, dataset_fields.origin, dataset_fields.display_type,
                  dataset_fields.overlay_config)
                 IS DISTINCT FROM (EXCLUDED.field_type, EXCLUDED.origin, EXCLUDED.display_type,
                                   EXCLUDED.overlay_config)""",
        dataset_id,
        fields,
    )


async def write_views(connection, dataset_id: int, views: list) -> list:
    """The primary's shared views, with their sample ids translated. A view that uses a concept stays on the primary
    (concepts are per instance): its name is returned."""
    concept = sorted(view['name'] for view in views if uses_concept(view['content']))
    copied = [view for view in views if not uses_concept(view['content'])]
    named = sorted(set().union(*(sample_ids_in(view['content'], set()) for view in copied)))
    mapping = {
        row['origin_id']: row['sample_id']
        for row in await connection.fetch(
            'SELECT origin_id, sample_id FROM mirror_samples WHERE dataset_id = $1 AND origin_id = ANY($2::int[])',
            dataset_id,
            named,
        )
    }
    await connection.execute(
        """DELETE FROM views WHERE id IN (
               SELECT view_id FROM mirror_views WHERE dataset_id = $1 AND NOT (origin_view_id = ANY($2::int[])))""",
        dataset_id,
        [view['id'] for view in copied],
    )
    copies = {
        row['origin_view_id']: row['view_id']
        for row in await connection.fetch(
            'SELECT origin_view_id, view_id FROM mirror_views WHERE dataset_id = $1',
            dataset_id,
        )
    }
    # Renames first through names of their own, so views that swapped names there don't collide here.
    renamed = [copies[view['id']] for view in copied if view['id'] in copies]
    await connection.execute(
        """UPDATE views v SET name = 'renaming ' || v.id
           FROM unnest($1::int[], $2::text[]) AS n(id, name) WHERE v.id = n.id AND v.name <> n.name""",
        renamed,
        [view['name'] for view in copied if view['id'] in copies],
    )
    for view in copied:
        content = translate_ids(view['content'], mapping)
        local = copies.get(view['id'])
        if local is not None:
            await connection.execute(
                """UPDATE views SET name = $2, content = $3::jsonb
                   WHERE id = $1 AND (name, content) IS DISTINCT FROM ($2, $3::jsonb)""",
                local,
                view['name'],
                content,
            )
            continue
        local = await connection.fetchval(
            """INSERT INTO views (dataset_id, user_id, name, slug, content, is_public, name_scope)
               VALUES ($1, NULL, $2, $3, $4::jsonb, TRUE, -1) RETURNING id""",
            dataset_id,
            view['name'],
            f"mirror-{dataset_id}-{view['id']}",
            content,
        )
        await connection.execute(
            'INSERT INTO mirror_views (dataset_id, origin_view_id, view_id) VALUES ($1, $2, $3)',
            dataset_id,
            view['id'],
            local,
        )
    await connection.execute('UPDATE mirrors SET concept_views = $2 WHERE dataset_id = $1', dataset_id, concept)
    return concept


async def write_settings(connection, dataset_id: int, dataset: dict, primary_host: str, origin: int) -> None:
    """The primary's settings. The default view is its local copy, or none when that view isn't copied."""
    default_view = None
    if dataset.get('default_view_id') is not None:
        default_view = await connection.fetchval(
            'SELECT view_id FROM mirror_views WHERE dataset_id = $1 AND origin_view_id = $2',
            dataset_id,
            dataset['default_view_id'],
        )
    name = await local_name(connection, dataset['name'], primary_host, origin, dataset_id)
    await connection.execute(
        """UPDATE datasets SET name = $2, description = $3, media_fields = $4, grid_media_field = $5,
                  modal_media_field = $6, segmentation_config = $7::jsonb, default_view_id = $8
           WHERE id = $1 AND (name, description, media_fields, grid_media_field, modal_media_field,
                              segmentation_config, default_view_id)
                 IS DISTINCT FROM ($2, $3, $4, $5, $6, $7::jsonb, $8)""",
        dataset_id,
        name,
        dataset.get('description'),
        dataset.get('media_fields'),
        dataset.get('grid_media_field'),
        dataset.get('modal_media_field'),
        dataset.get('segmentation_config') or {},
        default_view,
    )


# --- The job -------------------------------------------------------------------------------------------------------


def host_of(link) -> str:
    return urlparse(link['url']).hostname or link['url']


async def stop_mirrors(connection, where: str, args: tuple, reason: str) -> None:
    await connection.execute(
        f"UPDATE mirrors SET state = 'stopped', last_error = ${len(args) + 1} WHERE state <> 'stopped' AND {where}",
        *args,
        reason,
    )


async def configure(pool, link, client, checkpoint: dict) -> StepResult:
    """Read what the primary assigns this follower: start a copy of each newly ready dataset, update the options,
    and stop the mirrors of datasets it no longer shares."""
    try:
        config = await call(client, 'GET', '/follower/config')
    except PrimaryRefused as error:
        if error.status not in (401, 403):
            raise
        reason = f'No longer following: {error.detail}'

        async def removed(connection):
            await stop_mirrors(connection, 'link_id = $1', (link['id'],), reason)
            await connection.execute('UPDATE primary_links SET last_error = $2 WHERE id = $1', link['id'], reason)

        return StepResult(finished=True, result={'stopped': reason}, message=reason, publish=removed)

    restored = link['identity'] is not None and link['identity'] != config['identity']
    live = {
        row['origin_dataset_id']: dict(row)
        for row in await pool.fetch("SELECT * FROM mirrors WHERE link_id = $1 AND state <> 'stopped'", link['id'])
    }
    assigned = {entry['dataset_id']: entry for entry in config['datasets']}
    plans = {}
    for origin, entry in assigned.items():
        if entry['ready'] and (restored or origin not in live):
            plans[origin] = await call(client, 'GET', f'/follower/datasets/{origin}/blocks')

    async def publish(connection):
        if restored:
            await stop_mirrors(
                connection,
                'link_id = $1',
                (link['id'],),
                'The primary was restored into a new database: this copy stopped, and a new one started',
            )
        else:
            gone = [origin for origin in live if origin not in assigned]
            await stop_mirrors(
                connection,
                'link_id = $1 AND origin_dataset_id = ANY($2::int[])',
                (link['id'], gone),
                'No longer following: the primary stopped sharing this dataset with this follower',
            )
            for origin, mirror in live.items():
                entry = assigned.get(origin)
                # A primary before 2.6.0 has no images option: off.
                options = (entry['tags'], entry['views'], entry.get('images', False)) if entry else None
                if options and options != (mirror['tags'], mirror['views'], mirror['images']):
                    await connection.execute(
                        'UPDATE mirrors SET tags = $2, views = $3, images = $4 WHERE dataset_id = $1',
                        mirror['dataset_id'],
                        *options,
                    )
        for origin, plan in plans.items():
            entry = assigned[origin]
            name = await local_name(connection, entry['name'], host_of(link), origin)
            dataset_id = await connection.fetchval(
                'INSERT INTO datasets (name, owner_id, is_global) VALUES ($1, $2, FALSE) RETURNING id',
                name,
                link['created_by'],
            )
            blocks = [row['block'] for row in plan['blocks']]
            await connection.execute(
                """INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, tags, views, images, copy_blocks,
                                        copy_total, feed_cursor)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                dataset_id,
                link['id'],
                origin,
                entry['tags'],
                entry['views'],
                entry.get('images', False),
                blocks,
                len(blocks),
                entry['start_txid'] - 1,
            )
        await connection.execute(
            'UPDATE primary_links SET identity = $2, follower_name = $3, last_error = NULL WHERE id = $1',
            link['id'],
            config['identity'],
            config['name'],
        )

    checkpoint.update(configured=True, identity=config['identity'], caught_up=[], acked={})
    waiting = sorted(entry['name'] for entry in config['datasets'] if not entry['ready'])
    message = f"{len(assigned)} datasets shared, {len(plans)} new copies"
    if waiting:
        message += f"; waiting for writes in flight on the primary before copying {', '.join(waiting)}"
    return StepResult(checkpoint=checkpoint, message=message, publish=publish)


async def apply_state(pool, link, client, mirror, checkpoint: dict, *, views: bool, after_write=None) -> StepResult:
    """Fields and settings (and the shared views once the samples are all here) from the primary's state."""
    dataset_id, origin = mirror['dataset_id'], mirror['origin_dataset_id']
    state = await call(client, 'GET', f'/follower/datasets/{origin}/state')
    await ensure_array_columns(
        pool,
        dataset_id,
        [field['field_name'] for field in state['fields'] if field['origin'] == 'native_array'],
    )

    if views:
        # Once the samples are all here, and with each later state: the maps to re-rank where the primary did.
        checkpoint.setdefault('ranks', {})[str(dataset_id)] = state['maps']

    async def publish(connection):
        await begin_mirror_write(connection, dataset_id)
        await write_fields(connection, dataset_id, state['fields'])
        if views:
            await write_views(connection, dataset_id, state['views'])
        await write_settings(connection, dataset_id, state['dataset'], host_of(link), origin)
        if after_write is not None:
            await after_write(connection)

    return StepResult(
        checkpoint=checkpoint,
        message=f"Applied the primary's settings to {state['dataset']['name']}",
        publish=publish,
    )


async def apply_blocks(pool, client, mirror, blocks: list, after_write=None) -> tuple:
    """Fetch these blocks' samples, hashes and vectors from the primary, and a publish that writes them."""
    dataset_id, origin = mirror['dataset_id'], mirror['origin_dataset_id']
    data = await fetch_blocks(client, origin, blocks)
    await ensure_array_columns(pool, dataset_id, data['array_fields'])
    origin_ids = [sample['id'] for sample in data['samples']]
    vectors = decode_vectors(await fetch_vectors(client, origin, origin_ids)) if origin_ids else {}
    coordinates = await fetch_coordinates(client, origin, blocks) if origin_ids else []

    async def publish(connection):
        await begin_mirror_write(connection, dataset_id)
        present = await write_samples(connection, dataset_id, blocks, data)
        await write_hashes(connection, dataset_id, present, data['hashes'])
        # Every primary field of every sample present: what didn't come was deleted there.
        await write_vectors(connection, dataset_id, [(sample_id, None) for sample_id in present], vectors)
        await write_coordinates(connection, dataset_id, blocks, coordinates)
        if after_write is not None:
            await after_write(connection)

    return len(origin_ids), publish


async def copy_step(pool, link, client, mirror, checkpoint: dict) -> StepResult:
    dataset_id = mirror['dataset_id']
    started = checkpoint.setdefault('started', [])
    if dataset_id not in started:
        started.append(dataset_id)
        return await apply_state(pool, link, client, mirror, checkpoint, views=False)
    remaining = list(mirror['copy_blocks'])
    if not remaining:
        # Every block is here: copy the views, whose sample ids can now be translated, and follow the feed.
        async def following(connection):
            await connection.execute(
                "UPDATE mirrors SET state = 'following', last_synced_at = now(), last_error = NULL "
                'WHERE dataset_id = $1',
                dataset_id,
            )

        result = await apply_state(pool, link, client, mirror, checkpoint, views=True, after_write=following)
        result.message = 'Copy complete; following the feed'
        return result
    blocks, rest = remaining[:COPY_BLOCKS], remaining[COPY_BLOCKS:]

    async def progress(connection):
        await connection.execute(
            'UPDATE mirrors SET copy_blocks = $2, last_synced_at = now() WHERE dataset_id = $1',
            dataset_id,
            rest,
        )

    count, publish = await apply_blocks(pool, client, mirror, blocks, progress)
    done = mirror['copy_total'] - len(rest)
    return StepResult(
        checkpoint=checkpoint,
        done=done,
        total=mirror['copy_total'],
        message=f'Copying: {done:,} of {mirror["copy_total"]:,} blocks ({count:,} samples in this step)',
        publish=publish,
    )


async def rank_step(pool, mirror, checkpoint: dict):
    """Rank the mirror's copy of a primary map that has no ranking here yet, or that the primary has re-ranked since
    its coordinates moved (its rank_stale is clear and ours isn't). The rest of the time the coordinate triggers
    keep the ranking as the primary's own placements keep its: exact positions, approximate order."""
    dataset_id = mirror['dataset_id']
    pending = checkpoint.get('ranks', {}).get(str(dataset_id)) or []
    while pending:
        entry = pending.pop(0)
        field = entry['field_name']
        local = await pool.fetchrow(
            'SELECT rank_stale FROM embedding_viz_meta WHERE dataset_id = $1 AND field_name = $2',
            dataset_id,
            field,
        )
        if local is not None and not (local['rank_stale'] and not entry['rank_stale']):
            continue
        placed = await pool.fetchval(
            """SELECT EXISTS (SELECT 1 FROM embedding_coordinates c JOIN samples s ON s.id = c.sample_id
                              WHERE s.dataset_id = $1 AND c.field_name = $2)""",
            dataset_id,
            field,
        )
        if not placed:
            continue

        async def publish(connection, field=field):
            await connection.execute(f"SET LOCAL lock_timeout = '0'; SET LOCAL statement_timeout = '{RANK_SECONDS}s'")
            await connection.fetchval(
                'SELECT recompute_viz_rank($1, $2, TRUE)',
                dataset_id,
                field,
                timeout=RANK_SECONDS,
            )

        return StepResult(checkpoint=checkpoint, message=f'Ranked the map of {field}', publish=publish)
    return None


async def advance_cursor(connection, dataset_id: int, cursor: int) -> None:
    await connection.execute(
        'UPDATE mirrors SET feed_cursor = $2, last_synced_at = now(), last_error = NULL WHERE dataset_id = $1',
        dataset_id,
        cursor,
    )


async def feed_step(pool, link, client, mirror, checkpoint: dict) -> StepResult:
    dataset_id, origin = mirror['dataset_id'], mirror['origin_dataset_id']
    page = checkpoint.get('page')
    if page is None or page['dataset_id'] != dataset_id:
        ranked = await rank_step(pool, mirror, checkpoint)
        if ranked is not None:
            return ranked
        acked = checkpoint.setdefault('acked', {})
        if acked.get(str(dataset_id)) != mirror['feed_cursor']:
            await call(client, 'POST', f'/follower/datasets/{origin}/ack', body={'cursor': mirror['feed_cursor']})
            acked[str(dataset_id)] = mirror['feed_cursor']
        try:
            changes = await call(
                client,
                'GET',
                f'/follower/datasets/{origin}/changes',
                params={'after': mirror['feed_cursor'], 'identity': checkpoint['identity']},
            )
        except PrimaryRefused as error:
            if error.status not in (404, 409, 410):
                raise
            # 410 restored, 409 a cursor from before this copy, 404 no longer shared: this copy can't continue.
            # The next config read starts a new copy if the primary still shares the dataset.
            reason = f'This copy stopped: {error.detail}'

            async def stopped(connection):
                await stop_mirrors(connection, 'dataset_id = $1', (dataset_id,), reason)

            checkpoint['configured'] = False
            return StepResult(checkpoint=checkpoint, message=reason, publish=stopped)
        hashed = {vector['sample_id'] // BLOCK for vector in changes['vectors'] if vector['kind'] == 'hash'}
        reread = set(changes['blocks']) | hashed
        blocks = sorted(reread)
        # A vector change in a block being re-read comes with the block; the rest are fetched on their own.
        loose = [
            (vector['sample_id'], vector['field_name'])
            for vector in changes['vectors']
            if vector['kind'] == 'vector' and vector['sample_id'] // BLOCK not in reread
        ]
        page = {
            'dataset_id': dataset_id,
            'cursor': changes['cursor'],
            'more': changes['more'],
            'blocks': blocks,
            'vectors': loose,
            # Coordinates alone (a placement or a re-fit there): no samples or vectors to re-read.
            'maps': sorted({entry['block'] for entry in changes['maps']} - reread),
            'state': bool(set(changes['dataset']) & {'fields', 'settings', 'views', 'maps'}),
        }
        if not (blocks or loose or page['maps'] or page['state']):
            checkpoint['page'] = None
            if not changes['more']:
                checkpoint.setdefault('caught_up', []).append(dataset_id)

            async def advance(connection):
                await advance_cursor(connection, dataset_id, changes['cursor'])

            return StepResult(checkpoint=checkpoint, message='Up to date with the primary', publish=advance)
        checkpoint['page'] = page
        return StepResult(
            checkpoint=checkpoint,
            message=f"{len(blocks):,} changed blocks and {len(loose):,} changed vectors to apply",
        )

    if page['blocks']:
        blocks, page['blocks'] = page['blocks'][:COPY_BLOCKS], page['blocks'][COPY_BLOCKS:]
        count, publish = await apply_blocks(pool, client, mirror, blocks)
        return StepResult(checkpoint=checkpoint, message=f'Applied {count:,} changed samples', publish=publish)
    if page['vectors']:
        part, page['vectors'] = page['vectors'][: followers.VECTOR_SAMPLES], page['vectors'][followers.VECTOR_SAMPLES :]
        sample_ids = sorted({sample_id for sample_id, _ in part})
        fields = sorted({field for _, field in part})
        vectors = decode_vectors(await fetch_vectors(client, origin, sample_ids, fields))

        async def publish(connection):
            await begin_mirror_write(connection, dataset_id)
            await write_vectors(connection, dataset_id, [tuple(pair) for pair in part], vectors)

        return StepResult(checkpoint=checkpoint, message=f'Applied {len(part):,} changed vectors', publish=publish)
    if page['maps']:
        blocks, page['maps'] = page['maps'][:COPY_BLOCKS], page['maps'][COPY_BLOCKS:]
        coordinates = await fetch_coordinates(client, origin, blocks)

        async def publish(connection):
            await begin_mirror_write(connection, dataset_id)
            await write_coordinates(connection, dataset_id, blocks, coordinates)

        return StepResult(
            checkpoint=checkpoint,
            message=f'Applied {len(coordinates):,} changed map coordinates',
            publish=publish,
        )
    if page['state']:
        page['state'] = False
        return await apply_state(pool, link, client, mirror, checkpoint, views=True)

    # The whole page is applied: move the cursor past it. The next step acknowledges it to the primary.
    checkpoint['page'] = None
    cursor = page['cursor']

    async def finish_page(connection):
        await advance_cursor(connection, dataset_id, cursor)

    return StepResult(checkpoint=checkpoint, message='Applied a page of the feed', publish=finish_page)


def handler(pool):
    async def step(job):
        link = await pool.fetchrow('SELECT * FROM primary_links WHERE id = $1', job['args']['link_id'])
        if link is None:
            return StepResult(finished=True, message='This primary was removed here')
        checkpoint = dict(job['checkpoint'])
        async with connect(link) as client:
            try:
                if not checkpoint.get('configured'):
                    return await configure(pool, link, client, checkpoint)
                mirror = await pool.fetchrow(
                    """SELECT m.* FROM mirrors m JOIN datasets d ON d.id = m.dataset_id
                       WHERE m.link_id = $1 AND m.state <> 'stopped' AND NOT d.deleting
                         AND NOT (m.dataset_id = ANY($2::int[]))
                       ORDER BY m.dataset_id LIMIT 1""",
                    link['id'],
                    checkpoint.get('caught_up', []),
                )
                if mirror is None:

                    async def synced(connection):
                        await connection.execute(
                            'UPDATE primary_links SET last_synced_at = now(), last_error = NULL WHERE id = $1',
                            link['id'],
                        )

                    return StepResult(finished=True, message='Every mirror is up to date', publish=synced)
                if mirror['state'] == 'copying':
                    return await copy_step(pool, link, client, mirror, checkpoint)
                return await feed_step(pool, link, client, mirror, checkpoint)
            except (PrimaryUnavailable, PrimaryRefused) as error:
                # Shown on Settings → Primary as well as on the job.
                await pool.execute('UPDATE primary_links SET last_error = $2 WHERE id = $1', link['id'], str(error))
                raise

    return step
