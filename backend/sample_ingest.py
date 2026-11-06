"""Set-based sample ingestion with the existing duplicate and inline-artifact contract."""

import os
from collections import defaultdict
from typing import Any

from .field_registry import invalidate_values, lock_value_rows, merge_annotation_values, prune_fields, register_fields
from .label_fields import append_discovery
from .routers.hashes import hash_upsert_sql


def media_type(filepath):
    extension = os.path.splitext(filepath)[1].lower()
    for kind, extensions in (
        ('image', {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff'}),
        ('video', {'.mp4', '.avi', '.mov', '.mkv', '.webm'}),
        ('audio', {'.mp3', '.wav', '.ogg', '.flac'}),
    ):
        if extension in extensions:
            return kind
    return 'unknown'


async def insert_samples(connection, dataset_id, samples, on_duplicate, array_fields):
    previous_keys = set()
    if on_duplicate == 'update':
        previous_keys = set(
            await connection.fetchval(
                """SELECT array_agg(DISTINCT key) FROM samples CROSS JOIN LATERAL jsonb_object_keys(metadata) key
               WHERE dataset_id = $1 AND filepath = ANY($2::text[])""",
                dataset_id,
                [sample.filepath for sample in samples],
            )
            or [],
        )
    grouped: dict[str, dict[str, Any]] = {}
    for sample in samples:
        row = sample.model_dump(mode='json')
        row['media_type'] = sample.media_type or media_type(sample.filepath)
        row['embeddings'] = sample.embeddings or {}
        previous = grouped.get(sample.filepath)
        if previous is None:
            grouped[sample.filepath] = row
        elif on_duplicate == 'update':
            row['created_at'] = previous['created_at']
            row['tags'] = sorted(set(previous['tags'] or []) | set(row['tags'] or []))
            row['embeddings'] = {**(previous['embeddings'] or {}), **(row['embeddings'] or {})}
            row['hashes'] = {**(previous['hashes'] or {}), **(row['hashes'] or {})}
            grouped[sample.filepath] = row

    columns = ', '.join(f'"{name}"' for name in array_fields)
    extra_columns = ', ' + columns if columns else ''
    extra_values = ''.join(
        f", ARRAY(SELECT jsonb_array_elements_text(COALESCE(payload.array_fields->'{name}', '[]'::jsonb)))"
        for name in array_fields
    )
    conflict = 'DO NOTHING'
    if on_duplicate == 'update':
        assignments = [
            'media_type = EXCLUDED.media_type',
            "tags = ARRAY(SELECT DISTINCT tag FROM unnest(COALESCE(samples.tags, '{}') || EXCLUDED.tags) tag "
            "ORDER BY tag)",
            'metadata = EXCLUDED.metadata',
            'location = EXCLUDED.location',
            'heading = EXCLUDED.heading',
            'last_modified_at = now()',
            *(f'"{name}" = EXCLUDED."{name}"' for name in array_fields),
        ]
        conflict = 'DO UPDATE SET ' + ', '.join(assignments)
    query = f"""
        INSERT INTO samples
            (dataset_id, filepath, media_type, tags, metadata, location, heading, created_at{extra_columns})
        SELECT $1, payload.filepath, payload.media_type, payload.tags, payload.metadata,
            CASE WHEN payload.location->'coordinates' IS NOT NULL
                 THEN ST_SetSRID(ST_MakePoint((payload.location->'coordinates'->>0)::float8,
                                             (payload.location->'coordinates'->>1)::float8), 4326) END,
            payload.heading, COALESCE(payload.created_at, now()){extra_values}
        FROM jsonb_to_recordset($2::jsonb) AS payload(
            filepath text, media_type text, tags text[], metadata jsonb, location jsonb,
            heading float8, created_at timestamptz, array_fields jsonb, position integer)
        ORDER BY payload.position
        ON CONFLICT (dataset_id, filepath) {conflict}
        RETURNING id, filepath
    """
    written = {}
    rows = list(grouped.values())
    for start in range(0, len(rows), 500):
        payloads = [
            {
                **{key: value for key, value in row.items() if key not in ('embedding', 'embeddings', 'hashes')},
                'position': position,
            }
            for position, row in enumerate(rows[start : start + 500])
        ]
        inserted = await connection.fetch(query, dataset_id, payloads)
        written.update({row['filepath']: row['id'] for row in inserted})

    existing = {}
    if on_duplicate == 'error':
        found = await connection.fetch(
            'SELECT filepath, id FROM samples WHERE dataset_id = $1 AND filepath = ANY($2::text[])',
            dataset_id,
            list(grouped),
        )
        found_ids = {row['filepath']: row['id'] for row in found}
        seen = set()
        for sample in samples:
            if sample.filepath in seen or sample.filepath not in written:
                existing[sample.filepath] = found_ids[sample.filepath]
            seen.add(sample.filepath)

    written_metadata = [grouped[filepath]['metadata'] for filepath in written]
    inserted_fields: set[str] = set()
    declared = await register_fields(connection, dataset_id, written_metadata, inserted_fields)
    await append_discovery(connection, dataset_id, written_metadata)
    await lock_value_rows(
        connection,
        dataset_id,
        previous_keys | set(declared) | set(array_fields) | {'tags', 'media_type'},
    )
    if on_duplicate == 'update':
        await invalidate_values(connection, dataset_id, previous_keys | set(declared))
        retained_keys = {name for metadata in written_metadata for name in metadata}
        await prune_fields(connection, dataset_id, previous_keys - retained_keys)
    await merge_annotation_values(connection, dataset_id, written_metadata, inserted_fields)

    for name in sorted({'tags', 'media_type', *array_fields}):
        values = sorted(
            {
                value
                for filepath in written
                for value in (
                    [grouped[filepath]['media_type']]
                    if name == 'media_type'
                    else (grouped[filepath]['tags'] or [])
                    if name == 'tags'
                    else (grouped[filepath].get('array_fields') or {}).get(name, [])
                )
            },
        )
        if on_duplicate == 'update' and name != 'tags':
            await connection.execute(
                'DELETE FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = $2',
                dataset_id,
                name,
            )
        elif values:
            await connection.execute(
                """UPDATE dataset_field_stats SET
                       unique_values = ARRAY(SELECT DISTINCT unnest(
                           COALESCE(unique_values, '{}') || $3::text[]) ORDER BY 1 LIMIT 200),
                       values_complete = values_complete AND $4 AND
                           (SELECT count(*) FROM (SELECT DISTINCT unnest(
                               COALESCE(unique_values, '{}') || $3::text[]) LIMIT 201) distinct_values) <= 200
                   WHERE dataset_id = $1 AND field_name = $2""",
                dataset_id,
                name,
                values[:200],
                len(values) <= 200,
            )

    vectors = []
    hashes = defaultdict(list)
    for filepath, sample_id in written.items():
        payload = grouped[filepath]
        for field, vector in (payload['embeddings'] or {}).items():
            vectors.append({'sample_id': sample_id, 'field_name': field, 'vector': str(vector)})
        for field, value in (payload['hashes'] or {}).items():
            hashes[len(value)].append((sample_id, field, value))
    for start in range(0, len(vectors), 500):
        await connection.execute(
            """INSERT INTO embeddings (sample_id, field_name, vector)
               SELECT sample_id, field_name, vector::vector FROM jsonb_to_recordset($1::jsonb)
                   AS payload(sample_id integer, field_name text, vector text)
               ON CONFLICT (sample_id, field_name) DO UPDATE SET vector = EXCLUDED.vector
               WHERE embeddings.vector IS DISTINCT FROM EXCLUDED.vector""",
            vectors[start : start + 500],
        )
    for length, values in hashes.items():
        await connection.executemany(hash_upsert_sql(length), values)
    sample_ids = (
        [written[sample.filepath] for sample in samples]
        if on_duplicate == 'update'
        else [written[filepath] for filepath in grouped if filepath in written]
    )
    return sample_ids, existing, len(samples) - len(sample_ids), written_metadata, declared, inserted_fields
