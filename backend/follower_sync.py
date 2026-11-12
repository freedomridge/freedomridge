"""A follower's sync (docs/FOLLOWERS.md): copy each dataset the primary assigns this instance, then apply the
primary's change feed to the copy and acknowledge it, so the primary can prune its log.

One job per primary link, queued every FOLLOWER_SYNC_SECONDS when none is waiting or running. Each step is one
bounded unit (the config, a few blocks, or part of a feed page) committed together with the mirror's progress, so a
restart resumes where it stopped. A mirror is written only here, in transactions that set fridge.mirror_sync to its
dataset id (migration 0009); everything the primary didn't send stays the follower's own.
"""

import base64
import copy
import io
import logging
import re
import struct
from urllib.parse import urlparse

import asyncpg
import httpx
import numpy as np
from fastapi import HTTPException

from . import job_queue, patch_search
from .crypto import decrypt_secret
from .diskspace import INGEST_ROW_BYTES, require_free_space
from .field_registry import LOCAL_PREFIX, lock_field_lifecycle
from .job_runner import StepResult
from .routers import datasets as datasets_router
from .routers import followers
from .sample_ids import ranges_on_ids, sample_ids_in, translate_ids

logger = logging.getLogger(__name__)

KIND = 'follower_sync'
# A step can re-rank a whole map, as the primary's own re-rank does (up to 1800 s there).
POLICY = job_queue.JobPolicy(max_unit_seconds=1800, max_failures=5, retry_seconds=30, max_retry_seconds=900)
RANK_SECONDS = 1800
BLOCK = followers.BLOCK
COPY_BLOCKS = 2  # blocks per step: up to 8192 samples and their vectors
RESTORE_ROWS = 2000  # rows per step when a held copy is resumed into after a primary restore
HELD_REASON = 'The primary was restored into a new database: choose on the Primaries page whether this copy resumes'
OLDER_PRIMARY_REASON = (
    "The restored primary runs a release that can't compare copies: a new copy was made, and this one is kept"
)
REQUEST_SECONDS = 120
RETURN_SECONDS = 10  # a sync's own /follower/return holds the link row meanwhile (return_if_linked)
LOCK_SECONDS = 5
RETRY_SECONDS = 60  # a failed mirror's wait before its next try, doubling after each failure in a row
RETRY_MAX_SECONDS = 3600
REPORT_SECONDS = 600  # how often a mirror reports its own patch scales to a primary that takes them
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
PATCH_TILE = re.compile(r'(_patch_\d+x\d+)_r\d+c\d+$')  # a patch tile's field name, and its scale's


class PrimaryUnavailable(job_queue.QueueBusy):
    """The primary couldn't be reached or failed: the step is retried with backoff."""


class PrimaryRefused(Exception):
    def __init__(self, status: int, detail: str, code: str | None = None):
        super().__init__(f'The primary answered {status}: {detail}')
        self.status, self.detail, self.code = status, detail, code


# The primary's refusals that are final: the follower was removed or its token deleted. Its mirrors stop. Any other
# refusal (a proxy's 403, an inactive account for a moment, an older primary without codes) is retried.
FINAL_REFUSALS = ('follower_removed', 'follower_token_invalid')


def connect(link, instance: str | None = None) -> httpx.AsyncClient:
    """A client for the primary's API, saying which instance this is (the primary binds a follower's token to the
    first that uses it); tests bind one to the app instead."""
    headers = {'Authorization': f"Bearer {decrypt_secret(link['token_encrypted'])}"}
    if instance:
        headers['X-Fridge-Instance'] = instance
    return httpx.AsyncClient(base_url=link['url'].rstrip('/'), headers=headers, timeout=REQUEST_SECONDS)


async def installation_id(source) -> str:
    """This installation's id, which a primary binds this instance's follower token to."""
    return str(await source.fetchval('SELECT id FROM installation'))


def _detail(response) -> str:
    try:
        detail = response.json().get('detail')
    except ValueError:
        detail = None
    return detail if isinstance(detail, str) else response.text[:300]


def _code(response) -> str | None:
    try:
        code = response.json().get('code')
    except (ValueError, AttributeError):
        return None
    return code if isinstance(code, str) else None


async def call(client, method: str, path: str, *, params=None, body=None):
    try:
        response = await client.request(method, path, params=params, json=body)
    except httpx.HTTPError as error:
        raise PrimaryUnavailable(f'The primary could not be reached: {error}') from error
    if response.status_code >= 500 or response.status_code == 429:
        raise PrimaryUnavailable(f'The primary answered {response.status_code}: {_detail(response)}')
    if response.status_code >= 400:
        raise PrimaryRefused(response.status_code, _detail(response), _code(response))
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


def uses_local_fields(node) -> bool:
    """A filter on a mirror's own fields (`@local.<name>`), which only this instance has."""
    if isinstance(node, dict):
        field = node.get('field')
        own = isinstance(field, str) and field.startswith(LOCAL_PREFIX)
        return own or any(uses_local_fields(value) for value in node.values())
    if isinstance(node, list):
        return any(uses_local_fields(value) for value in node)
    return False


def instance_bound(node) -> bool:
    """A view that stays on the instance that made it: it uses a concept (each instance has its own), a range on
    sample ids (each numbers its samples in its own order), or a mirror's own `@local.` field."""
    return uses_concept(node) or ranges_on_ids(node) or uses_local_fields(node)


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


async def fetch_vector_pairs(client, origin: int, pairs: list) -> list:
    """The vectors of these (sample, field) pairs, asked for a request's worth of fields at a time: one sample can
    change in more fields than a request takes (4x4 and 8x8 patch tiles are 80)."""
    fields = sorted({field for _, field in pairs})
    found: list = []
    for start in range(0, len(fields), followers.VECTOR_FIELDS):
        asked = set(fields[start : start + followers.VECTOR_FIELDS])
        sample_ids = sorted({sample_id for sample_id, field in pairs if field in asked})
        found += await fetch_vectors(client, origin, sample_ids, sorted(asked))
    return found


async def ensure_array_columns(pool, dataset_id: int, names: list) -> None:
    """The primary's array fields need their physical column here before samples carrying them are written."""
    if not names:
        return
    existing = {row['column_name'] for row in await pool.fetch('SELECT column_name FROM sample_array_columns')}
    for name in names:
        if name in existing:
            continue
        if not datasets_router._SAFE_COLUMN_RE.match(name):
            raise ValueError(f'The primary sent an array field this instance cannot hold: {name!r}')
        # A name the samples table here has for a column of its own (parent_id since 2.11.0, say).
        taken = ValueError(
            f'The primary sent an array field this instance cannot hold: {name!r} is already a column of the samples '
            f'here. Rename the field {name!r} on the primary (copy it to a field of another name there and delete it).',
        )
        if name in datasets_router._RESERVED_COLUMNS:
            raise taken
        try:
            await datasets_router.add_array_column(pool, dataset_id, name)
        except HTTPException as error:
            if error.status_code == 409:
                raise job_queue.QueueContended(error.detail) from None
            if error.status_code == 400:
                raise taken from None
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


async def detach_children(connection, dataset_id: int, doomed: list) -> None:
    """Before the mirror deletes these samples: their objects that it keeps lose their image, so the key (0030) never
    deletes a mirror's object with its image. A primary on 2.10.x or older keeps an image's objects when it deletes the
    image, and a 2.11 one deletes them through their own blocks, or re-points them in the same batch. Their parent_id
    becomes JSON null, what a copy writes for a parent that isn't here, so a re-read writes the same. They are selected
    by the column, which is what the key deletes by; mirror_samples keeps their primary's parent, for a re-point if it
    returns. Run in the transaction that then deletes."""
    await connection.execute(
        """UPDATE samples SET metadata = jsonb_set(metadata, '{parent_id}', 'null')
           WHERE dataset_id = $1 AND parent_id = ANY($2::int[]) AND NOT (id = ANY($2::int[]))""",
        dataset_id,
        doomed,
    )


async def write_samples(connection, dataset_id: int, blocks: list, data: dict) -> list:
    """Make the mirror's samples in these blocks the primary's: delete what it no longer has, update what changed,
    insert what's new, and translate object samples' parent ids. Returns the origin ids present."""
    arrays = data['array_fields']
    samples = data['samples']
    present = [sample['id'] for sample in samples]
    doomed = [
        row['sample_id']
        for row in await connection.fetch(
            f"""SELECT m.sample_id FROM mirror_samples m, unnest($2::bigint[]) b
                WHERE m.dataset_id = $1 AND m.origin_id >= b * {BLOCK} AND m.origin_id < (b + 1) * {BLOCK}
                  AND NOT (m.origin_id = ANY($3::int[]))""",
            dataset_id,
            blocks,
            present,
        )
    ]
    await detach_children(connection, dataset_id, doomed)
    await connection.execute('DELETE FROM samples WHERE dataset_id = $1 AND id = ANY($2::int[])', dataset_id, doomed)
    if not samples:
        return present
    # A filepath the primary moved here from another sample (two samples swapping paths, or a version restore
    # recreating a deleted one): the sample holding it here steps aside under a name of its own, so it keeps its
    # vectors and what this instance added to it. Its own row in this batch, or its own block, which changed too,
    # then gives it its path or removes it. Until then it would break UNIQUE (dataset, filepath).
    await connection.execute(
        """UPDATE samples s SET filepath = 'mirror-moved://' || m.dataset_id || '/' || m.origin_id
           FROM mirror_samples m, unnest($2::int[], $3::text[]) AS r(origin_id, filepath)
           WHERE m.dataset_id = $1 AND s.id = m.sample_id AND s.filepath = r.filepath AND m.origin_id <> r.origin_id""",
        dataset_id,
        present,
        [sample['filepath'] for sample in samples],
    )
    source = f"""
        incoming AS (
            -- A numeric parent_id is the primary's sample id: translated, or null when no sample here has it. Only
            -- one an id can be (1 to 2^31 - 1) is cast: a longer number, which ingest accepts, would fail the cast.
            SELECT r.*, COALESCE(r.metadata->>'parent_id' ~ '^-?[0-9]+$', FALSE) AS numeric_parent,
                   CASE WHEN r.metadata->>'parent_id' ~ '^[0-9]{{1,10}}$'
                        THEN CASE WHEN (r.metadata->>'parent_id')::bigint BETWEEN 1 AND 2147483647
                                  THEN (r.metadata->>'parent_id')::int END
                   END AS origin_parent
            FROM jsonb_to_recordset($2::jsonb) AS r({_record_type(arrays)})
        ),
        translated AS (
            SELECT i.*,
                   CASE WHEN NOT i.numeric_parent THEN COALESCE(i.metadata, '{{}}'::jsonb)
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
    await supersede(connection, dataset_id, 'hash_fields', fields, ('sample_hashes',))
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
    this instance had on its own becomes the primary's once the primary sends one of that name: all of it, at once."""
    await supersede(
        connection,
        dataset_id,
        'vector_fields',
        sorted(vectors),
        ('embeddings', 'map_state_rows'),
    )
    await connection.execute(
        """UPDATE mirrors SET vector_fields = ARRAY(SELECT DISTINCT unnest(vector_fields || $2::text[]) ORDER BY 1)
           WHERE dataset_id = $1 AND NOT ($2::text[] <@ vector_fields)""",
        dataset_id,
        sorted(vectors),
    )
    kept = [(sample_id, field) for field, rows in vectors.items() for sample_id, _ in rows]
    await connection.execute(
        """DELETE FROM embeddings e USING mirror_samples m, unnest($2::int[], $3::text[]) AS n(origin_id, field_name)
           WHERE m.dataset_id = $1 AND m.origin_id = n.origin_id AND e.dataset_id = $1 AND e.sample_id = m.sample_id
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
            try:
                # Changed rows updated, missing ones inserted: an upsert would take an embeddings.id sequence value
                # for every row, those that conflict included.
                await connection.execute(
                    """UPDATE embeddings e SET vector = st.vector FROM mirror_vector_stage st
                       JOIN mirror_samples m ON m.dataset_id = $1 AND m.origin_id = st.sample_id
                       WHERE e.dataset_id = $1 AND e.sample_id = m.sample_id AND e.field_name = $2
                         AND e.vector IS DISTINCT FROM st.vector""",
                    dataset_id,
                    field,
                )
                await connection.execute(
                    """INSERT INTO embeddings (dataset_id, sample_id, field_name, vector)
                       SELECT $1, m.sample_id, $2, st.vector FROM mirror_vector_stage st
                       JOIN mirror_samples m ON m.dataset_id = $1 AND m.origin_id = st.sample_id
                       WHERE NOT EXISTS (SELECT 1 FROM embeddings e
                                         WHERE e.dataset_id = $1 AND e.sample_id = m.sample_id AND e.field_name = $2)
                       ON CONFLICT (dataset_id, sample_id, field_name) DO NOTHING""",
                    dataset_id,
                    field,
                )
            except asyncpg.PostgresError as error:
                if 'dimensions' not in str(error):
                    raise
                # A vector index is per dataset and field (index_jobs.py): this copy's own index of the field.
                raise MirrorFieldClash(
                    f"The primary's vector field {field!r} has {len(group[0][1])} dimensions, but this copy's index "
                    f'of that field was built at another size ({error}). Drop that index here, and the mirror '
                    'catches up.',
                ) from error
    await clear_placement(connection, dataset_id, sorted({sample_id for sample_id, _ in named}))


async def supersede(connection, dataset_id: int, column: str, fields: list, what: tuple) -> None:
    """Fields arriving from the primary that this instance had on its own, under the same name: the primary's
    replace them (docs/FOLLOWERS.md). Every row of this instance's goes now, not only those of the samples this page
    names, or the field would hold two sources, the leftovers read-only. The primary's own rows come with the copy
    and the feed. The mirror's map of such a vector field goes too: it was of the other vectors. Its coordinates and
    pending placements go with the vectors."""
    arriving = await connection.fetchval(
        f'SELECT ARRAY(SELECT unnest($2::text[]) EXCEPT SELECT unnest({column})) FROM mirrors WHERE dataset_id = $1',
        dataset_id,
        fields,
    )
    if not arriving:
        return
    replaced: set = set()  # the fields and maps this instance had of its own
    if 'embeddings' in what:
        gone = await connection.fetch(
            """WITH gone AS (
                   DELETE FROM embeddings e USING mirror_samples m
                   WHERE m.dataset_id = $1 AND e.dataset_id = $1 AND e.sample_id = m.sample_id
                     AND e.field_name = ANY($2::text[])
                   RETURNING e.field_name)
               SELECT DISTINCT field_name FROM gone""",
            dataset_id,
            arriving,
        )
        replaced.update(row['field_name'] for row in gone)
    if 'map_state_rows' in what:
        # A patch map is kept under its scale's name (clip_patch_4x4), not its tiles' (clip_patch_4x4_r0c0).
        maps = sorted({*arriving, *(PATCH_TILE.sub(r'\1', field) for field in arriving)})
        for table in ('embedding_viz_rank', 'embedding_viz_meta', 'embedding_map_state', 'embedding_layouts'):
            verb = 'SELECT field_name FROM' if table == 'embedding_layouts' else 'DELETE FROM'
            returning = '' if table == 'embedding_layouts' else 'RETURNING field_name'
            gone = await connection.fetch(
                f"""WITH gone AS ({verb} {table} WHERE dataset_id = $1 AND field_name = ANY($2::text[]) {returning})
                    SELECT DISTINCT field_name FROM gone""",
                dataset_id,
                maps,
            )
            replaced.update(row['field_name'] for row in gone)
        # A map of this instance's own is another one now: a new layout id here, so a lasso drawn on it (with its id,
        # or with none) warns. The primary's id replaces it once the primary has one (write_layouts).
        for name in sorted({PATCH_TILE.sub(r'\1', field) for field in replaced}):
            await connection.execute('SELECT bump_map_layout($1, $2)', dataset_id, name)
    if 'sample_hashes' in what:
        await connection.execute(
            """DELETE FROM sample_hashes h USING mirror_samples m
               WHERE m.dataset_id = $1 AND h.sample_id = m.sample_id AND h.field_name = ANY($2::text[])""",
            dataset_id,
            arriving,
        )


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
            """INSERT INTO embedding_coordinates AS c (sample_id, field_name, x, y, dataset_id)
               SELECT m.sample_id, n.field_name, n.x, n.y, $1
               FROM unnest($2::int[], $3::text[], $4::float8[], $5::float8[]) AS n(origin_id, field_name, x, y)
               JOIN mirror_samples m ON m.dataset_id = $1 AND m.origin_id = n.origin_id
               JOIN embeddings e ON e.dataset_id = $1 AND e.sample_id = m.sample_id AND e.field_name = n.field_name
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


async def write_layouts(connection, dataset_id: int, layouts: list) -> None:
    """The layout ids of the primary's maps (embedding_layouts), each with the last transaction the primary's read of
    it could see. The mirror takes one once its feed has passed that transaction (take_layouts), so its id never names
    a layout whose coordinates haven't all arrived: a lasso drawn here meanwhile records the old id, and warns later,
    which is true. A map the primary has no id for keeps the one it has here: none, as the primary's (a copy starts
    with none, forget_primary_layouts), or the one a superseded map of this instance's got. The maps of this
    instance's own vector fields keep their own ids."""
    sent = [layout for layout in layouts if layout['field_name'] in await primary_maps(connection, dataset_id)]
    await connection.execute(
        """INSERT INTO embedding_layouts AS l (dataset_id, field_name, pending_layout_id, pending_txid)
           SELECT $1, s.field_name, s.layout_id, s.txid
           FROM jsonb_to_recordset($2::jsonb) AS s(field_name text, layout_id uuid, txid bigint)
           ON CONFLICT (dataset_id, field_name) DO UPDATE
           SET pending_layout_id = EXCLUDED.pending_layout_id, pending_txid = EXCLUDED.pending_txid
           WHERE l.layout_id IS DISTINCT FROM EXCLUDED.pending_layout_id OR l.pending_layout_id IS NOT NULL""",
        dataset_id,
        sent,
    )
    await take_layouts(connection, dataset_id)


async def primary_maps(connection, dataset_id: int) -> set:
    """The maps of the primary's vector fields here: a whole-image field, or a patch scale (its tiles' fields)."""
    fields = await connection.fetchval('SELECT vector_fields FROM mirrors WHERE dataset_id = $1', dataset_id)
    return {*fields, *(PATCH_TILE.sub(r'\1', field) for field in fields)}


async def forget_primary_layouts(connection, dataset_id: int) -> None:
    """A copy that starts again (after a restore of the primary, or into a copy stopped before) takes the primary's
    layout ids afresh, at its end: the ones held, and any waiting, are another database's."""
    await connection.execute(
        'DELETE FROM embedding_layouts WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
        dataset_id,
        sorted(await primary_maps(connection, dataset_id)),
    )


async def take_layouts(connection, dataset_id: int) -> None:
    """The primary's layout ids whose transaction the mirror's feed has passed (write_layouts)."""
    await connection.execute(
        """UPDATE embedding_layouts l SET layout_id = l.pending_layout_id, pending_layout_id = NULL, pending_txid = NULL
           FROM mirrors m WHERE m.dataset_id = $1 AND l.dataset_id = $1 AND l.pending_txid <= m.feed_cursor""",
        dataset_id,
    )


async def write_fields(connection, dataset_id: int, fields: list) -> None:
    """The primary's field registry. is_promoted stays this instance's: it stands for an index here, and so do this
    instance's own `@local.` fields. An older primary's metadata key by such a name, unreachable there, is skipped."""
    fields = [field for field in fields if not field['field_name'].startswith(LOCAL_PREFIX)]
    await connection.execute(
        """DELETE FROM dataset_fields
           WHERE dataset_id = $1 AND owner = 'primary' AND NOT (field_name = ANY($2::text[]))""",
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
    """The primary's shared views, with their sample ids translated. A view bound to the primary (a concept, or a
    range on sample ids: instance_bound) stays there: its name is returned."""
    concept = sorted(view['name'] for view in views if instance_bound(view['content']))
    copied = [view for view in views if not instance_bound(view['content'])]
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
        f"""UPDATE mirrors SET state = 'stopped', last_error = ${len(args) + 1}, stopped_at = now()
            WHERE state <> 'stopped' AND {where}""",
        *args,
        reason,
    )


# The stopped mirrors a new copy can resume into, latest first: not being deleted or kept as a separate copy, and of a
# known primary database. A mirror stopped before mirrors recorded it has none, and is never resumed.
RESUMABLE = """SELECT m.* FROM mirrors m JOIN datasets d ON d.id = m.dataset_id
               WHERE m.state = 'stopped' AND NOT m.keep_separate AND NOT d.deleting
                 AND m.primary_identity IS NOT NULL"""
RESUMABLE_ORDER = 'ORDER BY m.stopped_at DESC NULLS LAST, m.dataset_id DESC'


def resumed_from(stopped: list, identity: str):
    """Of one dataset's RESUMABLE mirrors, in RESUMABLE_ORDER: the one a new copy from the primary database
    `identity` resumes into, the latest of that database. None: the copy makes a new dataset."""
    return next((row for row in stopped if followers.same_identity(row['primary_identity'], identity)), None)


async def resumable_mirror(connection, origin: int, identity: str):
    """The stopped mirror a new copy of the primary's dataset `origin` resumes into, locked until the copy is set up,
    so a "Keep as a separate copy" can't land in between."""
    rows = await connection.fetch(
        f'{RESUMABLE} AND m.origin_dataset_id = $1 {RESUMABLE_ORDER} FOR UPDATE OF m',
        origin,
    )
    row = resumed_from(rows, identity)
    return None if row is None else dict(row)


def primary_identity(config: dict) -> str:
    """The primary database's identity from its config. A primary before 2.6.1 sends its cluster's alone."""
    return config.get('database_identity') or config['identity']


async def return_if_linked(pool, link, client) -> bool:
    """The primary has this instance as having stopped following it, but the link is still here: a Stop following
    from a link removed since reached the primary after this one was added (its return found nothing to restore).
    Return again (POST /follower/return). The link stays locked meanwhile: a Stop following now deletes it before
    telling the primary, so it can't come between, and a sync still running after one never returns."""
    async with pool.acquire() as connection, connection.transaction():
        if await connection.fetchval('SELECT 1 FROM primary_links WHERE id = $1 FOR SHARE', link['id']) is None:
            return False
        # Briefly: a Stop following in the UI waits on this row. A timeout is PrimaryUnavailable, and the next sync
        # tries again.
        previous, client.timeout = client.timeout, httpx.Timeout(RETURN_SECONDS)
        try:
            await call(client, 'POST', '/follower/return')
        except PrimaryRefused as error:
            if error.status == 404:  # a primary before 2.7.0 says nothing of leaving
                return False
            raise
        finally:
            client.timeout = previous
    return True


# --- A copy held after a primary restore (docs/FOLLOWERS.md, "When the primary was restored") ----------------------


# A held copy compared afresh: the primary was restored again before it was resumed.
COMPARE_AGAIN = """UPDATE mirror_restores SET phase = 'compare', decision = NULL, decided_by = NULL, decided_at = NULL,
                          compare_after = 0, total = 0, matched = 0, matched_hash = 0, only_here = NULL,
                          only_here_local = NULL, computed_at = NULL, hash_field = NULL,
                          primary_identity = COALESCE($2, primary_identity)
                   WHERE dataset_id = $1"""


async def restore_step(pool, link, client, checkpoint: dict) -> StepResult | None:
    """One step for a copy held after its primary was restored: comparing it with the primary, or, once its admin has
    chosen, resuming into it or letting it go, in steps of RESTORE_ROWS rows. The held copies take turns, and one
    whose step fails is isolated as a mirror is: the error on its row, tried again later, the others carry on.
    None: nothing to do now."""
    rows = await pool.fetch(
        """SELECT r.*, m.hash_fields FROM mirror_restores r JOIN mirrors m ON m.dataset_id = r.dataset_id
           WHERE r.link_id = $1 AND r.phase <> 'ask' AND (r.retry_after IS NULL OR r.retry_after <= now())
           ORDER BY (r.dataset_id <= $2), r.dataset_id""",
        link['id'],
        checkpoint.get('held_turn', 0),
    )
    for row in rows:
        before = copy.deepcopy(checkpoint)
        try:
            result = await held_step(pool, link, client, dict(row), checkpoint)
        except PrimaryUnavailable:
            raise  # every mirror of this primary waits for it
        except Exception as error:
            if job_queue.is_transient(error):
                raise
            reason = describe(error)

            async def note(connection, dataset_id=row['dataset_id'], reason=reason):
                await note_held_failure(connection, dataset_id, reason)

            logger.warning('Held copy %s failed this run: %s', row['dataset_id'], reason)
            return StepResult(
                checkpoint={**before, 'held_turn': row['dataset_id']},
                message=f'A held copy failed: {reason}',
                publish=note,
            )
        if result is None:
            continue
        checkpoint['held_turn'] = row['dataset_id']
        if row['failures'] or row['last_error']:  # it works again: the error goes, as a mirror's does
            inner = result.publish

            async def cleared(connection, inner=inner, dataset_id=row['dataset_id']):
                if inner is not None:
                    await inner(connection)
                await connection.execute(
                    """UPDATE mirror_restores SET last_error = NULL, failures = 0, retry_after = NULL
                       WHERE dataset_id = $1""",
                    dataset_id,
                )

            result.publish = cleared
        return held_isolated(row['dataset_id'], before, result)
    return None


async def note_held_failure(connection, dataset_id: int, reason: str) -> None:
    """Put the error on the held copy's row and wait before trying it again, as note_failure does for a mirror."""
    await connection.execute(
        """UPDATE mirror_restores SET last_error = $2, failures = failures + 1,
                  retry_after = now() + make_interval(secs => least($3 * power(2, failures), $4))
           WHERE dataset_id = $1""",
        dataset_id,
        reason,
        RETRY_SECONDS,
        RETRY_MAX_SECONDS,
    )


def held_isolated(dataset_id: int, before: dict, result: StepResult) -> StepResult:
    """A held copy's writes run in a savepoint, as a mirror's do (isolated): if they fail, they're undone, the error
    goes on its row, and the job carries on from the checkpoint before the step. A transient error still fails the
    job, which then runs again."""
    publish = result.publish
    progress = result.checkpoint if result.checkpoint is not None else dict(before)

    async def guarded(connection):
        try:
            async with connection.transaction():
                if publish is not None:
                    await publish(connection)
        except Exception as error:
            if job_queue.is_transient(error) or isinstance(error, asyncpg.InterfaceError):
                raise
            await note_held_failure(connection, dataset_id, describe(error))
            progress.clear()
            progress.update({**before, 'held_turn': dataset_id})

    return StepResult(
        checkpoint=progress,
        done=result.done,
        total=result.total,
        message=result.message,
        result=result.result,
        finished=result.finished,
        publish=guarded,
    )


async def held_step(pool, link, client, row: dict, checkpoint: dict) -> StepResult | None:
    """The next step of one held copy (restore_step)."""
    dataset_id, phase = row['dataset_id'], row['phase']
    if phase == 'compare':
        return await compare_step(pool, client, dict(row), checkpoint)
    if phase == 'resume':
        return await resume_held(pool, link, client, dict(row), checkpoint)
    queries = {
        # The copy's samples the primary no longer has, matched to none of its: they go, with what was added here.
        'unmatched': """SELECT s.id FROM samples s WHERE s.dataset_id = $1
                          AND NOT EXISTS (SELECT 1 FROM mirror_restore_map p
                                          WHERE p.dataset_id = $1 AND p.sample_id = s.id)
                        ORDER BY s.id LIMIT $2""",
        # The matched ones take the primary's new ids in two passes, through negative ids, so the unique
        # (dataset_id, origin_id) never clashes.
        'negate': """SELECT m.sample_id AS id FROM mirror_samples m
                       JOIN mirror_restore_map p ON p.dataset_id = m.dataset_id AND p.sample_id = m.sample_id
                     WHERE m.dataset_id = $1 AND m.origin_id <> -p.new_origin_id ORDER BY m.sample_id LIMIT $2""",
        'move': """SELECT sample_id AS id FROM mirror_samples WHERE dataset_id = $1 AND origin_id < 0
                   ORDER BY sample_id LIMIT $2""",
        'map': 'SELECT sample_id AS id FROM mirror_restore_map WHERE dataset_id = $1 ORDER BY sample_id LIMIT $2',
    }
    ids = [] if phase == 'views' else [r['id'] for r in await pool.fetch(queries[phase], dataset_id, RESTORE_ROWS)]
    following = {'unmatched': 'negate', 'negate': 'move', 'move': 'views', 'views': 'map'}
    checkpoint['mirror_turn'] = True
    if ids or phase == 'views':

        async def publish(connection):
            await begin_mirror_write(connection, dataset_id)
            if phase == 'unmatched':
                await detach_children(connection, dataset_id, ids)  # so the count asked about stays exact
                await connection.execute(
                    'DELETE FROM samples WHERE dataset_id = $1 AND id = ANY($2::int[])',
                    dataset_id,
                    ids,
                )
            elif phase == 'negate':
                await connection.execute(
                    """UPDATE mirror_samples m SET origin_id = -p.new_origin_id FROM mirror_restore_map p
                       WHERE m.dataset_id = $1 AND p.dataset_id = $1 AND p.sample_id = m.sample_id
                         AND m.sample_id = ANY($2::int[])""",
                    dataset_id,
                    ids,
                )
            elif phase == 'move':
                await connection.execute(
                    """UPDATE mirror_samples SET origin_id = -origin_id
                       WHERE dataset_id = $1 AND sample_id = ANY($2::int[])""",
                    dataset_id,
                    ids,
                )
            elif phase == 'views':
                # The copies of the primary's shared views: its view ids can name other views now. The next sync copies
                # its shared views again; views made here stay.
                await connection.execute(
                    'DELETE FROM views WHERE id IN (SELECT view_id FROM mirror_views WHERE dataset_id = $1)',
                    dataset_id,
                )
                await connection.execute("UPDATE mirror_restores SET phase = 'map' WHERE dataset_id = $1", dataset_id)
            else:
                await connection.execute(
                    'DELETE FROM mirror_restore_map WHERE dataset_id = $1 AND sample_id = ANY($2::int[])',
                    dataset_id,
                    ids,
                )

        return StepResult(
            checkpoint=checkpoint,
            message=f'Resuming a copy held after a primary restore ({phase})',
            publish=publish,
        )
    if phase != 'map':
        nxt = following[phase]

        async def advance(connection):
            await connection.execute('UPDATE mirror_restores SET phase = $2 WHERE dataset_id = $1', dataset_id, nxt)

        return StepResult(checkpoint=checkpoint, publish=advance)
    # The matches are cleared: what comes next is the admin's choice, or why the question went.
    decision = row['decision']

    async def settle(connection):
        if decision == 'resume':
            await connection.execute("UPDATE mirror_restores SET phase = 'resume' WHERE dataset_id = $1", dataset_id)
        elif decision == 'redo':
            await connection.execute(COMPARE_AGAIN, dataset_id, None)
        else:
            if decision == 'new':  # kept as it is: the new copy is made by the next configure
                await connection.execute('UPDATE mirrors SET keep_separate = TRUE WHERE dataset_id = $1', dataset_id)
            await connection.execute('DELETE FROM mirror_restores WHERE dataset_id = $1', dataset_id)

    if decision == 'new':
        checkpoint['configured'] = False
    return StepResult(checkpoint=checkpoint, publish=settle)


async def compare_step(pool, client, row: dict, checkpoint: dict) -> StepResult:
    """A page of the restored primary's samples compared with the held copy's, by file path, and by image hash where
    both have one (a matched path with another hash doesn't match). Changes nothing on the copy."""
    dataset_id = row['dataset_id']
    first = row['total'] == 0 and row['compare_after'] == 0
    # The first page asks for the copy's hash fields (as many as a page takes, lowest names first), to see which
    # the primary has too.
    asked = (
        sorted(row['hash_fields'])[: followers.MATCH_HASH_FIELDS]
        if first
        else [row['hash_field']]
        if row['hash_field']
        else []
    )
    try:
        page = await call(
            client,
            'GET',
            f"/follower/datasets/{row['origin_dataset_id']}/match-keys",
            params={'after': row['compare_after'], 'hash_fields': asked},
        )
    except PrimaryRefused as error:
        # Only a primary without the route answers FastAPI's own 404; one for a dataset unshared meanwhile ("does not
        # follow") is an ordinary failure, and the next configure lets the question go.
        if error.status != 404 or error.detail != 'Not Found':
            raise
        # The restored primary runs a release that can't compare copies: as before, a new copy, this one kept.

        async def older(connection):
            await connection.execute(
                "UPDATE mirror_restores SET decision = 'new', phase = 'map' WHERE dataset_id = $1",
                dataset_id,
            )
            await connection.execute(
                'UPDATE mirrors SET last_error = $2 WHERE dataset_id = $1',
                dataset_id,
                OLDER_PRIMARY_REASON,
            )

        checkpoint['mirror_turn'] = True
        return StepResult(checkpoint=checkpoint, message=OLDER_PRIMARY_REASON, publish=older)
    keys = page['keys']
    hash_field = row['hash_field']
    if first:  # the lowest field name both sides have (the primary says which it has), kept for the whole comparison
        present = set(page.get('hash_fields') or [])
        hash_field = min(present) if present else None
    here = {
        r['filepath']: r
        for r in await pool.fetch(
            """SELECT s.id, s.filepath, h.hash_hex FROM samples s
               LEFT JOIN sample_hashes h ON h.sample_id = s.id AND h.field_name = $3
               WHERE s.dataset_id = $1 AND s.filepath = ANY($2::text[])""",
            dataset_id,
            [key['filepath'] for key in keys],
            hash_field,
        )
    }
    pairs, by_hash = [], 0
    for key in keys:
        mine = here.get(key['filepath'])
        if mine is None:
            continue
        theirs = key['hashes'].get(hash_field) if hash_field else None
        if theirs is not None and mine['hash_hex'] is not None:
            if theirs != mine['hash_hex']:
                continue
            by_hash += 1
        pairs.append((mine['id'], key['id']))
    done = page['next'] is None
    after = page['next'] if page['next'] is not None else (keys[-1]['id'] if keys else row['compare_after'])

    async def publish(connection):
        await connection.execute(
            """INSERT INTO mirror_restore_map (dataset_id, sample_id, new_origin_id)
               SELECT $1, p.sample_id, p.origin_id FROM unnest($2::int[], $3::int[]) AS p(sample_id, origin_id)
               ON CONFLICT DO NOTHING""",
            dataset_id,
            [pair[0] for pair in pairs],
            [pair[1] for pair in pairs],
        )
        await connection.execute(
            """UPDATE mirror_restores SET hash_field = $2, compare_after = $3, total = total + $4,
                      matched = matched + $5, matched_hash = matched_hash + $6
               WHERE dataset_id = $1""",
            dataset_id,
            hash_field,
            after,
            len(keys),
            len(pairs),
            by_hash,
        )
        if done:
            await connection.execute(
                """UPDATE mirror_restores SET phase = 'ask', computed_at = now(),
                          only_here = (SELECT count(*) FROM samples WHERE dataset_id = $1) - matched,
                          -- what this instance added there that a resume deletes with them: read only when it has
                          -- a field of its own, so a copy without one isn't scanned
                          only_here_local = CASE
                              WHEN NOT EXISTS (SELECT 1 FROM dataset_fields
                                               WHERE dataset_id = $1 AND origin = 'local') THEN 0
                              ELSE (SELECT count(*) FROM samples s
                                    WHERE s.dataset_id = $1
                                      AND (s.local_metadata <> '{}'::jsonb OR cardinality(s.local_tags) > 0)
                                      AND NOT EXISTS (SELECT 1 FROM mirror_restore_map m
                                                      WHERE m.dataset_id = $1 AND m.sample_id = s.id))
                          END
                   WHERE dataset_id = $1""",
                dataset_id,
            )

    checkpoint['mirror_turn'] = True
    return StepResult(
        checkpoint=checkpoint,
        message=f'Comparing a held copy with the restored primary: {row["total"] + len(keys):,} samples',
        publish=publish,
    )


async def resume_held(pool, link, client, row: dict, checkpoint: dict) -> StepResult | None:
    """The last step of resuming into a held copy: its samples carry the restored primary's ids now, so it copies
    again as a resumed copy does (case A): every block read again, the feed from the subscription's start."""
    dataset_id, origin = row['dataset_id'], row['origin_dataset_id']
    config = await call(client, 'GET', '/follower/config')
    entry = next((item for item in config['datasets'] if item['dataset_id'] == origin), None)
    if entry is not None and not entry['ready']:
        return None  # waiting for writes in flight on the primary, as a new copy does
    current = primary_identity(config)
    plan = await call(client, 'GET', f'/follower/datasets/{origin}/blocks') if entry is not None else {'blocks': []}

    async def publish(connection):
        if entry is None:  # no longer shared: the copy stays stopped
            await connection.execute('DELETE FROM mirror_restores WHERE dataset_id = $1', dataset_id)
            return
        if not followers.same_identity(row['primary_identity'], current):
            await connection.execute(COMPARE_AGAIN, dataset_id, current)
            return
        held = [
            r['block']
            for r in await connection.fetch(
                f'SELECT DISTINCT origin_id / {BLOCK} AS block FROM mirror_samples WHERE dataset_id = $1',
                dataset_id,
            )
        ]
        blocks = sorted({*(item['block'] for item in plan['blocks']), *held})
        await connection.execute(
            """UPDATE mirrors SET link_id = $2, state = 'copying', tags = $3, views = $4, images = $5,
                      copy_blocks = $6, copy_total = $7, feed_cursor = $8, primary_identity = $9,
                      stopped_at = NULL, last_error = NULL, failures = 0, retry_after = NULL,
                      fields_reported_at = NULL, keep_separate = FALSE
               WHERE dataset_id = $1""",
            dataset_id,
            link['id'],
            entry['tags'],
            entry['views'],
            entry.get('images', False),
            blocks,
            len(blocks),
            entry['start_txid'] - 1,
            current,
        )
        await forget_primary_layouts(connection, dataset_id)
        await connection.execute('DELETE FROM mirror_restores WHERE dataset_id = $1', dataset_id)

    checkpoint['mirror_turn'] = True
    return StepResult(
        checkpoint=checkpoint,
        message='Resumed into a copy held after a primary restore',
        publish=publish,
    )


async def configure(pool, link, client, checkpoint: dict) -> StepResult:
    """Read what the primary assigns this follower: start a copy of each newly ready dataset, update the options,
    and stop the mirrors of datasets it no longer shares."""
    try:
        config = await call(client, 'GET', '/follower/config')
    except PrimaryRefused as error:
        if error.status not in (401, 403) or error.code not in FINAL_REFUSALS:
            raise
        reason = f'No longer following: {error.detail}'

        async def removed(connection):
            await stop_mirrors(connection, 'link_id = $1', (link['id'],), reason)
            await connection.execute('UPDATE primary_links SET last_error = $2 WHERE id = $1', link['id'], reason)

        return StepResult(finished=True, result={'stopped': reason}, message=reason, publish=removed)

    if config.get('left') and await return_if_linked(pool, link, client):
        config = await call(client, 'GET', '/follower/config')
    current = primary_identity(config)
    restored = link['identity'] is not None and not followers.same_identity(link['identity'], current)
    live = {
        row['origin_dataset_id']: dict(row)
        for row in await pool.fetch("SELECT * FROM mirrors WHERE link_id = $1 AND state <> 'stopped'", link['id'])
    }
    assigned = {entry['dataset_id']: entry for entry in config['datasets']}
    restores = {
        row['origin_dataset_id']: dict(row)
        for row in await pool.fetch('SELECT * FROM mirror_restores WHERE link_id = $1', link['id'])
    }
    # After a restore, each copy of a dataset the primary still shares is held, and its admin chooses whether it
    # resumes (docs/FOLLOWERS.md): its sample ids can't be trusted. One held already waits too, unless they chose
    # a new copy.
    holding = {origin for origin in live if origin in assigned} if restored else set()
    held = holding | {origin for origin, row in restores.items() if row['decision'] != 'new'}
    plans = {}
    for origin, entry in assigned.items():
        if entry['ready'] and origin not in held and (restored or origin not in live):
            plans[origin] = await call(client, 'GET', f'/follower/datasets/{origin}/blocks')

    resumed: list[int] = []

    async def publish(connection):
        resumed.clear()
        if restored:
            await stop_mirrors(
                connection,
                'link_id = $1 AND origin_dataset_id = ANY($2::int[])',
                (link['id'], sorted(holding)),
                HELD_REASON,
            )
            await stop_mirrors(
                connection,
                'link_id = $1',
                (link['id'],),
                'The primary was restored into a new database: this copy stopped, and a new one started',
            )
            for origin in sorted(holding):
                await connection.execute(
                    """INSERT INTO mirror_restores
                           (dataset_id, link_id, origin_dataset_id, origin_name, primary_identity)
                       VALUES ($1, $2, $3, $4, $5) ON CONFLICT (dataset_id) DO NOTHING""",
                    live[origin]['dataset_id'],
                    link['id'],
                    origin,
                    assigned[origin]['name'],
                    current,
                )
        for origin, row in restores.items():
            # Only a question not yet acted on: a resume half applied finishes first (resume_held then finds the
            # dataset unshared, or the primary restored again), so the copy's sample ids are never left half moved.
            if row['phase'] not in ('compare', 'ask'):
                continue
            if origin not in assigned and row['decision'] != 'drop':
                # No longer shared: the question goes (its matches cleared in steps), and the copy stays stopped.
                await connection.execute(
                    """UPDATE mirror_restores SET decision = 'drop', phase = 'map'
                       WHERE dataset_id = $1 AND phase IN ('compare', 'ask')""",
                    row['dataset_id'],
                )
            elif row['decision'] != 'drop' and not followers.same_identity(row['primary_identity'], current):
                # Restored again before it was resumed: compare again, with the primary as it is now.
                await connection.execute(
                    """UPDATE mirror_restores SET decision = 'redo', phase = 'map', primary_identity = $2,
                              origin_name = $3
                       WHERE dataset_id = $1 AND phase IN ('compare', 'ask')""",
                    row['dataset_id'],
                    current,
                    assigned[origin]['name'],
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
            blocks = [row['block'] for row in plan['blocks']]
            # The same primary database copied this dataset here before: resume into that copy, keeping what this
            # instance added (its own fields, indexes, concepts, grants, views). Never after a restore: its sample
            # ids may have been handed out again.
            previous = None if restored else await resumable_mirror(connection, origin, current)
            if previous is not None:
                # Every block it holds is read again with the primary's: a block the primary no longer has comes
                # back empty, and its samples go here, as samples gone from a re-read block always do.
                held = [
                    row['block']
                    for row in await connection.fetch(
                        f'SELECT DISTINCT origin_id / {BLOCK} AS block FROM mirror_samples WHERE dataset_id = $1',
                        previous['dataset_id'],
                    )
                ]
                blocks = sorted({*blocks, *held})
                await connection.execute(
                    """UPDATE mirrors SET link_id = $2, state = 'copying', tags = $3, views = $4, images = $5,
                              copy_blocks = $6, copy_total = $7, feed_cursor = $8, primary_identity = $9,
                              stopped_at = NULL, last_error = NULL, failures = 0, retry_after = NULL,
                              fields_reported_at = NULL
                       WHERE dataset_id = $1""",
                    previous['dataset_id'],
                    link['id'],
                    entry['tags'],
                    entry['views'],
                    entry.get('images', False),
                    blocks,
                    len(blocks),
                    entry['start_txid'] - 1,
                    current,
                )
                await forget_primary_layouts(connection, previous['dataset_id'])
                resumed.append(previous['dataset_id'])
                continue
            name = await local_name(connection, entry['name'], host_of(link), origin)
            dataset_id = await connection.fetchval(
                'INSERT INTO datasets (name, owner_id, is_global) VALUES ($1, $2, FALSE) RETURNING id',
                name,
                link['created_by'],
            )
            await connection.execute(
                """INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, tags, views, images, copy_blocks,
                                        copy_total, feed_cursor, primary_identity)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                dataset_id,
                link['id'],
                origin,
                entry['tags'],
                entry['views'],
                entry.get('images', False),
                blocks,
                len(blocks),
                entry['start_txid'] - 1,
                current,
            )
        # How many versions the primary keeps of each dataset. They stay there (docs/FOLLOWERS.md), and the mirror's
        # Versions tab says so. A primary from before it counted them doesn't say: unknown.
        for origin, entry in assigned.items():
            await connection.execute(
                """UPDATE mirrors SET primary_versions = $3
                   WHERE link_id = $1 AND origin_dataset_id = $2 AND state <> 'stopped'
                     AND primary_versions IS DISTINCT FROM $3""",
                link['id'],
                origin,
                entry.get('versions'),
            )
        await connection.execute(
            'UPDATE primary_links SET identity = $2, follower_name = $3, last_error = NULL WHERE id = $1',
            link['id'],
            current,
            config['name'],
        )

    checkpoint.update(
        configured=True,
        identity=current,
        protocol=config.get('protocol', 1),
        caught_up=[],
        failed=[],
        acked={},
        reported=[],
    )
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
        if 'layouts' in state:  # a primary before 2.11 sends none: the mirror's ids stay as they are
            await write_layouts(connection, dataset_id, state['layouts'])
        if after_write is not None:
            await after_write(connection)

    return StepResult(
        checkpoint=checkpoint,
        message=f"Applied the primary's settings to {state['dataset']['name']}",
        publish=publish,
    )


async def apply_blocks(pool, client, mirror, blocks: list, after_write=None, *, new_only: bool = False) -> tuple:
    """Fetch these blocks' samples, hashes and vectors from the primary, and a publish that writes them. From the
    feed (new_only), only the vectors of samples new here: the feed's vector log carries every change to the others,
    and re-reading a block whose samples were tagged mustn't download all their vectors again."""
    dataset_id, origin = mirror['dataset_id'], mirror['origin_dataset_id']
    data = await fetch_blocks(client, origin, blocks)
    await ensure_array_columns(pool, dataset_id, data['array_fields'])
    origin_ids = [sample['id'] for sample in data['samples']]
    wanted = origin_ids
    if new_only and origin_ids:
        known = {
            row['origin_id']
            for row in await pool.fetch(
                'SELECT origin_id FROM mirror_samples WHERE dataset_id = $1 AND origin_id = ANY($2::int[])',
                dataset_id,
                origin_ids,
            )
        }
        wanted = [origin_id for origin_id in origin_ids if origin_id not in known]
    vectors = decode_vectors(await fetch_vectors(client, origin, wanted)) if wanted else {}
    coordinates = await fetch_coordinates(client, origin, blocks) if origin_ids else []

    async def publish(connection):
        await begin_mirror_write(connection, dataset_id)
        present = await write_samples(connection, dataset_id, blocks, data)
        await write_hashes(connection, dataset_id, present, data['hashes'])
        # Every primary field of every sample asked for: what didn't come was deleted there.
        wanted_ids = set(wanted)
        asked = present if not new_only else [sample_id for sample_id in present if sample_id in wanted_ids]
        await write_vectors(connection, dataset_id, [(sample_id, None) for sample_id in asked], vectors)
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
            # Not a mirror stopped meanwhile (its primary removed here while this step ran): it stays stopped.
            await connection.execute(
                "UPDATE mirrors SET state = 'following', last_synced_at = now(), last_error = NULL, failures = 0, "
                "retry_after = NULL WHERE dataset_id = $1 AND state <> 'stopped'",
                dataset_id,
            )

        result = await apply_state(pool, link, client, mirror, checkpoint, views=True, after_write=following)
        result.message = 'Copy complete; following the feed'
        return result
    blocks, rest = remaining[:COPY_BLOCKS], remaining[COPY_BLOCKS:]

    async def progress(connection):
        await connection.execute(
            'UPDATE mirrors SET copy_blocks = $2, last_synced_at = now(), last_error = NULL, failures = 0, '
            "retry_after = NULL WHERE dataset_id = $1 AND state <> 'stopped'",
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
    # A mirror stopped while this step ran keeps its reason (a resume recomputes its copy anyway).
    await connection.execute(
        'UPDATE mirrors SET feed_cursor = $2, last_synced_at = now(), last_error = NULL, failures = 0, '
        "retry_after = NULL WHERE dataset_id = $1 AND state <> 'stopped'",
        dataset_id,
        cursor,
    )
    await take_layouts(connection, dataset_id)


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
                params={
                    'after': mirror['feed_cursor'],
                    'identity': checkpoint['identity'],
                    # Bounded pages: the page is kept in the job's checkpoint. An older primary ignores it.
                    'vector_limit': followers.FEED_VECTOR_ROWS,
                },
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
        # Every vector change is fetched on its own: a re-read block brings only the vectors of samples new here.
        loose = [
            (vector['sample_id'], vector['field_name']) for vector in changes['vectors'] if vector['kind'] == 'vector'
        ]
        page = {
            'dataset_id': dataset_id,
            'cursor': changes['cursor'],
            'more': changes['more'],
            'blocks': blocks,
            'vectors': loose,
            # Coordinates are applied last, re-read blocks included: a block's step writes its coordinates before the
            # page's vectors are here, and a coordinate of a vector that isn't here yet can't be written.
            'maps': sorted({entry['block'] for entry in changes['maps']}),
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
        count, publish = await apply_blocks(pool, client, mirror, blocks, new_only=True)
        return StepResult(checkpoint=checkpoint, message=f'Applied {count:,} changed samples', publish=publish)
    if page['vectors']:
        part, page['vectors'] = page['vectors'][: followers.VECTOR_SAMPLES], page['vectors'][followers.VECTOR_SAMPLES :]
        vectors = decode_vectors(await fetch_vector_pairs(client, origin, part))

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


class MirrorFieldClash(Exception):
    """The primary's vectors don't fit a same-named field this instance indexes at another size."""


def isolated(mirror, before: dict, result: StepResult) -> StepResult:
    """One mirror's failure mustn't hold up the others of its primary. Its writes run in a savepoint: if they fail,
    they're undone, the error goes on the mirror, and the job's checkpoint (written after this, from the same dict)
    becomes the one from before the step with the mirror passed over in this run. A transient error (a lost
    connection, a serialization failure) still fails the job, which then runs again."""
    publish = result.publish
    progress = result.checkpoint if result.checkpoint is not None else dict(before)

    async def guarded(connection):
        try:
            async with connection.transaction():
                if publish is not None:
                    await publish(connection)
        except Exception as error:
            if job_queue.is_transient(error) or isinstance(error, asyncpg.InterfaceError):
                raise
            await note_failure(connection, mirror['dataset_id'], describe(error))
            replacement = passed_over(before, mirror)
            progress.clear()
            progress.update(replacement)

    return StepResult(
        checkpoint=progress,
        done=result.done,
        total=result.total,
        message=result.message,
        result=result.result,
        finished=result.finished,
        publish=guarded,
    )


async def note_failure(connection, dataset_id: int, reason: str) -> None:
    """Put the error on the mirror, and wait before trying it again: a mirror that fails until someone fixes it
    shouldn't download its next page again every minute. A stopped mirror keeps the reason it stopped."""
    await connection.execute(
        """UPDATE mirrors SET last_error = $2, failures = failures + 1,
                  retry_after = now() + make_interval(secs => least($3 * power(2, failures), $4))
           WHERE dataset_id = $1 AND state <> 'stopped'""",
        dataset_id,
        reason,
        RETRY_SECONDS,
        RETRY_MAX_SECONDS,
    )


def describe(error: Exception) -> str:
    return f'{type(error).__name__}: {error}'[:1000]


def passed_over(checkpoint: dict, mirror) -> dict:
    """The checkpoint with this mirror skipped for the rest of the run, and counted as failed."""
    return {
        **checkpoint,
        'caught_up': [*checkpoint.get('caught_up', []), mirror['dataset_id']],
        'failed': [*checkpoint.get('failed', []), mirror['dataset_id']],
    }


async def own_patch_scales(pool, mirror) -> list[dict]:
    """The patch scales this instance computed for a mirror itself: those whose tiles aren't the primary's."""
    async with pool.acquire() as connection:
        found = await patch_search.dataset_patch_scales(connection, mirror['dataset_id'])
    theirs = set(mirror['vector_fields'])
    return [
        {'base_field': base, 'scale': scale, 'dims': entry['dims'], 'indexed': entry['indexed_fields'] is not None}
        for (base, scale), entry in found.items()
        if entry['dims'] and not theirs.intersection(entry['tiles'])
    ]


async def report_step(pool, client, mirror, checkpoint: dict) -> StepResult:
    """Tell the primary the patch scales this instance holds of one mirror (docs/FOLLOWERS.md), so a patch search on
    one of them can come here. Each mirror reports every REPORT_SECONDS."""
    dataset_id = mirror['dataset_id']
    scales = await own_patch_scales(pool, mirror)
    try:
        await call(
            client,
            'POST',
            f"/follower/datasets/{mirror['origin_dataset_id']}/fields",
            body={'scales': scales},
        )
        message = f'Reported {len(scales)} patch scale(s) of mirror {dataset_id}'
    except PrimaryRefused as error:
        # Not followed there any more, say: the next config read stops the mirror. Try again next time.
        message = f'The primary refused the patch scales of mirror {dataset_id}: {error.detail}'
        logger.warning(message)
    checkpoint.setdefault('reported', []).append(dataset_id)

    async def reported(connection):
        await connection.execute('UPDATE mirrors SET fields_reported_at = now() WHERE dataset_id = $1', dataset_id)

    return StepResult(checkpoint=checkpoint, message=message, publish=reported)


def handler(pool):
    async def step(job):
        link = await pool.fetchrow('SELECT * FROM primary_links WHERE id = $1', job['args']['link_id'])
        if link is None:
            return StepResult(finished=True, message='This primary was removed here')
        checkpoint = dict(job['checkpoint'])
        async with connect(link, await installation_id(pool)) as client:
            try:
                if not checkpoint.get('configured'):
                    return await configure(pool, link, client, checkpoint)
                # A copy held after a primary restore takes a step turn about with the mirrors, so neither holds up the
                # other (its steps set mirror_turn).
                if not checkpoint.pop('mirror_turn', False):
                    held = await restore_step(pool, link, client, checkpoint)
                    if held is not None:
                        return held
                # The mirrors take turns, so a long first copy or a busy feed doesn't hold up the others of this
                # primary: the mirror whose page of the feed is half applied carries on with it (the page is in the
                # checkpoint), and otherwise the next one after the mirror served last.
                page = checkpoint.get('page')
                mirror = await pool.fetchrow(
                    """SELECT m.* FROM mirrors m JOIN datasets d ON d.id = m.dataset_id
                       WHERE m.link_id = $1 AND m.state <> 'stopped' AND NOT d.deleting
                         AND NOT (m.dataset_id = ANY($2::int[])) AND (m.retry_after IS NULL OR m.retry_after <= now())
                       ORDER BY (m.dataset_id = $3) DESC, (m.dataset_id <= $4), m.dataset_id LIMIT 1""",
                    link['id'],
                    checkpoint.get('caught_up', []),
                    page['dataset_id'] if page else 0,
                    checkpoint.get('turn', 0),
                )
                if mirror is not None:
                    checkpoint['turn'] = mirror['dataset_id']
                if mirror is None and checkpoint.get('protocol', 1) >= 2:
                    due = await pool.fetchrow(
                        f"""SELECT m.* FROM mirrors m JOIN datasets d ON d.id = m.dataset_id
                           WHERE m.link_id = $1 AND m.state = 'following' AND NOT d.deleting
                             AND NOT (m.dataset_id = ANY($2::int[]))
                             AND (m.fields_reported_at IS NULL
                                  OR m.fields_reported_at < now() - interval '{REPORT_SECONDS} seconds')
                           ORDER BY m.dataset_id LIMIT 1""",
                        link['id'],
                        checkpoint.get('reported', []),
                    )
                    if due is not None:
                        return await report_step(pool, client, due, checkpoint)
                if mirror is None:
                    held = await restore_step(pool, link, client, checkpoint)
                    if held is not None:
                        return held

                    async def synced(connection):
                        await connection.execute(
                            'UPDATE primary_links SET last_synced_at = now(), last_error = NULL WHERE id = $1',
                            link['id'],
                        )

                    failed = checkpoint.get('failed', [])
                    waiting = await pool.fetchval(
                        """SELECT count(*) FROM mirrors WHERE link_id = $1 AND state <> 'stopped'
                             AND retry_after > now() AND NOT (dataset_id = ANY($2::int[]))""",
                        link['id'],
                        failed,
                    )
                    problems = [
                        *([f'{len(failed)} mirror(s) failed'] if failed else []),
                        *([f'{waiting} wait(s) to try again after an error'] if waiting else []),
                    ]
                    message = (
                        f"{'; '.join(problems)}; the others are up to date: see Settings → Primaries"
                        if problems
                        else 'Every mirror is up to date'
                    )
                    return StepResult(
                        finished=True,
                        message=message,
                        result={'failed': failed, 'waiting': waiting},
                        publish=synced,
                    )
                # The steps change the checkpoint in place: this is the one to go back to if the mirror fails.
                before = copy.deepcopy(checkpoint)
                try:
                    if mirror['state'] == 'copying':
                        result = await copy_step(pool, link, client, mirror, checkpoint)
                    else:
                        result = await feed_step(pool, link, client, mirror, checkpoint)
                except PrimaryUnavailable:
                    raise  # every mirror of this primary waits for it
                except Exception as error:
                    if job_queue.is_transient(error):
                        raise
                    reason = describe(error)

                    async def note(connection):
                        await note_failure(connection, mirror['dataset_id'], reason)

                    logger.warning('Mirror %s failed this run: %s', mirror['dataset_id'], reason)
                    return StepResult(
                        checkpoint=passed_over(before, mirror),
                        message=f'A mirror failed: {reason}',
                        publish=note,
                    )
                if result.committed:
                    return result
                return isolated(mirror, before, result)
            except (PrimaryUnavailable, PrimaryRefused) as error:
                # Shown on Settings → Primary as well as on the job.
                await pool.execute('UPDATE primary_links SET last_error = $2 WHERE id = $1', link['id'], str(error))
                raise

    return step
