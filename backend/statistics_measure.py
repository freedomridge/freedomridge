"""Read-only statistics measurements; each unit owns one coherent database snapshot."""

import asyncio
import time
from pathlib import Path

import asyncpg

from .job_queue import _terminate_connection
from .job_tasks import run_isolated
from .label_fields import LABEL_CACHES, LEGACY_GEOMETRY_FIELDS, label_context

BLOCK_SIZE = 4096
VALUE_LIMIT = 100
MEASURE_SECONDS = 10.0
INDEX_TYPES = {
    '__heatmap_fields__': 'heatmap_index',
    '__probability_fields__': 'probability_index',
    '__mask_overlay_fields__': 'mask_overlay_index',
    '__cuboid_fields__': 'cuboid_index',
    '__keypoint_fields__': 'keypoint_index',
    '__detection_fields__': 'detection_index',
    '__polyline_fields__': 'polyline_index',
    '__classification_fields__': 'classification_index',
}
DEFINITION_TOKEN = "id::text || ':' || field_type || ':' || origin"
MEASURE_SQL = (Path(__file__).parent / 'db/statistics_measure.sql').read_text()


class MeasurementTooLarge(TimeoutError):
    """A bounded measurement needs smaller work, not another identical attempt."""


def value_limit(field_type):
    if field_type.endswith('_index'):
        return None
    return 200 if field_type == 'enum' else VALUE_LIMIT


async def _read_snapshot(pool, read, *, timeout=None):
    budget = MEASURE_SECONDS + 1 if timeout is None else timeout
    started = time.monotonic()
    async with pool.acquire(timeout=min(1, budget)) as connection:

        async def operation():
            async with connection.transaction(isolation='repeatable_read', readonly=True):
                await connection.execute(
                    f"SET LOCAL statement_timeout='{max(1, int(min(MEASURE_SECONDS, budget) * 1000))}ms'; "
                    "SET LOCAL idle_in_transaction_session_timeout='15s'",
                )
                return await read(connection)

        try:
            return await run_isolated(
                operation(),
                name='Statistics measurement',
                timeout=max(0, budget - (time.monotonic() - started)),
                on_error=lambda: _terminate_connection(connection),
            )
        except (TimeoutError, asyncio.TimeoutError, asyncpg.QueryCanceledError) as error:
            raise MeasurementTooLarge('Statistics measurement exceeded its bounded budget') from error


async def read_value(pool, query, *args):
    return await _read_snapshot(pool, lambda connection: connection.fetchval(query, *args))


async def read_rows(pool, query, *args):
    return await _read_snapshot(pool, lambda connection: connection.fetch(query, *args))


def parent_field(name, registered=()):
    if name in registered:
        return name
    if name.endswith('.label'):
        return name.removesuffix('.label')
    return 'segmentation' if name == 'segmentation.labels' else name


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


async def definitions(connection, dataset_id):
    return [
        dict(row)
        for row in await connection.fetch(
            f'SELECT field_name, field_type, origin, {DEFINITION_TOKEN} AS token FROM dataset_fields '
            "WHERE dataset_id=$1 AND origin<>'system' ORDER BY field_name",
            dataset_id,
        )
    ]


async def measure_part(pool, dataset_id, block_id, part_id, split_level):
    width = BLOCK_SIZE >> split_level
    lower = block_id * BLOCK_SIZE + part_id * width

    async def read(connection):
        snapshot = await connection.fetchval('SELECT txid_current_snapshot()::text')
        fields = await definitions(connection, dataset_id)
        context = await label_context(connection, dataset_id)
        for field in fields:
            field['label_list'] = field['field_name'] in context.label_fields
        native_names = [field['field_name'] for field in fields if field['origin'] == 'native_array']
        native = "'{}'::jsonb"
        if native_names:
            columns = ', '.join(quote_identifier(name) for name in native_names)
            native = f'(SELECT to_jsonb(native_fields) FROM (SELECT {columns}) native_fields)'
        rows = await connection.fetch(
            MEASURE_SQL.replace('__NATIVE__', native),
            dataset_id,
            lower,
            lower + width,
            fields,
            timeout=MEASURE_SECONDS,
        )
        count = await connection.fetchval(
            'SELECT count(*) FROM samples WHERE dataset_id=$1 AND id >= $2 AND id < $3',
            dataset_id,
            lower,
            lower + width,
        )
        return {'snapshot': snapshot, 'count': count, 'fields': [dict(row) for row in rows]}

    return await _read_snapshot(pool, read)


async def aggregate_field(pool, dataset_id, name, partial=None, limit=128):
    async def read(connection):
        definition = await connection.fetchrow(
            f'SELECT field_name, field_type, origin, {DEFINITION_TOKEN} AS token FROM dataset_fields '
            "WHERE dataset_id=$1 AND field_name=ANY($2::text[]) AND origin<>'system' "
            "AND (field_name=$3 OR (field_name='segmentation' AND $3='segmentation.labels') "
            "OR field_name=ANY($4::text[]) OR EXISTS (SELECT 1 FROM dataset_field_stats discovery "
            "WHERE discovery.dataset_id=$1 AND discovery.field_name=ANY($5::text[]) "
            "AND dataset_fields.field_name=ANY(discovery.unique_values) "
            "AND (discovery.field_name<>'__classification_fields__' OR dataset_fields.field_type='list'))) "
            'ORDER BY (field_name=$3) DESC LIMIT 1',
            dataset_id,
            [name, parent_field(name)],
            name,
            sorted(LEGACY_GEOMETRY_FIELDS),
            list(LABEL_CACHES),
        )
        parent = definition['field_name'] if definition else name
        token = definition['token'] if definition else None
        if partial is None:
            cache_token = await connection.fetchval(
                'SELECT xmin::text FROM dataset_field_stats WHERE dataset_id=$1 AND field_name=$2',
                dataset_id,
                name,
            )
            result = {
                'field_name': name,
                'parent_field': parent,
                'definition_token': token,
                'cache_token': cache_token,
                'defined': definition is not None or name in INDEX_TYPES or name in ('tags', 'media_type'),
                'field_type': (
                    'enum' if name != parent or definition['origin'] == 'native_array' else definition['field_type']
                )
                if definition
                else INDEX_TYPES.get(name),
                'sample_count': 0,
                'min_value': None,
                'max_value': None,
                'text_min': None,
                'text_max': None,
                'unique_values': [],
                'values_complete': True,
                'after_part': [-1, -1],
            }
        else:
            result = dict(partial)
        if token != result['definition_token']:
            result['after_part'] = None
            return result
        rows = await connection.fetch(
            """SELECT block_id,part_id,field_type,sample_count,min_value,max_value,text_min,text_max,
                      unique_values,values_complete
               FROM dataset_block_field_stats WHERE dataset_id=$1 AND field_name=$2
                 AND definition_token IS NOT DISTINCT FROM $3 AND (block_id,part_id)>($4,$5)
               ORDER BY block_id,part_id LIMIT $6""",
            dataset_id,
            name,
            token,
            *result['after_part'],
            limit + 1,
        )
        values = set(result['unique_values'])
        for row in rows[:limit]:
            result['sample_count'] += row['sample_count']
            result['field_type'] = result['field_type'] or row['field_type']
            for key, choose in [('min_value', min), ('max_value', max), ('text_min', min), ('text_max', max)]:
                present = [value for value in (result[key], row[key]) if value is not None]
                result[key] = choose(present) if present else None
            values.update(row['unique_values'] or [])
            cap = value_limit(result['field_type'])
            result['values_complete'] &= row['values_complete'] and (cap is None or len(values) <= cap)
            values = set(sorted(values)[:cap])
        if name in INDEX_TYPES:
            registered = {field['field_name'] for field in await definitions(connection, dataset_id)}
            values.intersection_update(registered)
        result['unique_values'] = sorted(values)
        result['field_type'] = result['field_type'] or 'enum'
        if result['field_type'] == 'datetime':
            result['unique_values'] = [value for value in (result['text_min'], result['text_max']) if value is not None]
        result['after_part'] = [rows[limit - 1]['block_id'], rows[limit - 1]['part_id']] if len(rows) > limit else None
        return result

    return await _read_snapshot(pool, read)
