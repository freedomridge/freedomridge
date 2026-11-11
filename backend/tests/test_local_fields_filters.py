"""A mirror's own fields in queries (docs/FOLLOWERS.md): `@local.<name>` filters read local_metadata and
`@local.tags` the local_tags array, beside the primary's same-named fields; the query language writes them without
backticks; versions refuse them; statistics, histograms, autocomplete and facets count them."""

import os

import pytest
from fastapi import HTTPException

from backend.filter_query import NUMBER, format_filter, parse_query
from backend.routers import datasets, versions
from backend.routers import filter_query as filter_query_router
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.statistics_helpers import run_refresh

registry_database = registry_helpers.registry_database
ingest = registry_helpers.ingest
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def mirror_with_local_fields(database):
    """Dataset 2 as a mirror of four samples, with fields of its own written as a local write does."""
    await ingest(
        [{'weeds': 100, 'local.x': 7}, {'weeds': 200}, {'weeds': 300}, {'weeds': 400}],
        dataset_id=2,
        prefix='s3://images/m',
    )
    await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')
    await database.execute(
        """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin)
           VALUES (2, '@local.weeds', 'number', 'local'), (2, '@local.kind', 'string', 'local'),
                  (2, '@local.tags', 'list', 'local')""",
    )
    ids = [row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = 2 ORDER BY id')]
    values = [({'weeds': 3, 'kind': 'thistle'}, ['checked']), ({'weeds': 5, 'kind': 'dock'}, ['checked', 'redo'])]
    async with database.pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT set_config('fridge.mirror_local', '2', TRUE)")
        for sample_id, (local, tags) in zip(ids, values):
            await connection.execute(
                'UPDATE samples SET local_metadata = $2, local_tags = $3 WHERE id = $1',
                sample_id,
                local,
                tags,
            )
    return ids


async def matching(database, node):
    args: list = []
    sql, _ = datasets.parse_filter_node(node, args, 1, dataset_id=2, array_columns={'tags'})
    rows = await database.fetch(f'SELECT id FROM samples WHERE dataset_id = 2 AND {sql} ORDER BY id', *args)
    return [row['id'] for row in rows]


def test_local_fields_filter_beside_the_primarys(registry_database):
    async def scenario(database):
        first, second, third, fourth = await mirror_with_local_fields(database)

        def leaf(field, operator, value=None):
            return {'field': field, 'operator': operator, 'value': value}

        cases = [
            (leaf('@local.weeds', '>', 3), [second]),
            (leaf('@local.weeds', '=', 3), [first]),
            (leaf('@local.weeds', 'in', [3, 5]), [first, second]),
            (leaf('@local.weeds', 'not in', [3]), [second, third, fourth]),
            (leaf('@local.weeds', '!=', 3), [second, third, fourth]),
            (leaf('@local.weeds', 'exists'), [first, second]),
            (leaf('@local.weeds', 'not_exists'), [third, fourth]),
            (leaf('@local.weeds', 'is_null'), [third, fourth]),
            (leaf('@local.kind', 'contains', 'this'), [first]),
            (leaf('@local.tags', '=', 'redo'), [second]),
            (leaf('@local.tags', 'in', ['checked']), [first, second]),
            (leaf('@local.tags', 'not in', ['redo']), [first, third, fourth]),
            (leaf('@local.tags', 'exists'), [first, second]),
            (leaf('@local.tags', 'not_exists'), [third, fourth]),
            # The primary's fields of the same names are untouched by them.
            (leaf('weeds', '>', 250), [third, fourth]),
            (leaf('local.x', '=', 7), [first]),
            ({'logic': 'AND', 'filters': [leaf('@local.weeds', '<', 4), leaf('weeds', '=', 100)]}, [first]),
        ]
        for node, expected in cases:
            assert await matching(database, node) == expected, node

        # A bad name after the prefix is refused, as any unsafe name is.
        for name in ('@local.a.b', "@local.x'y", '@local.'):
            with pytest.raises(HTTPException) as refused:
                await matching(database, leaf(name, '=', 1))
            assert refused.value.status_code == 400

        # Inside the cross-grain link, a negation on one needs that field present, like a metadata field.
        guarded = datasets._strictify_negations(leaf('@local.weeds', '!=', 3), partner=True, array_columns={'tags'})
        presence = {'field': '@local.weeds', 'operator': 'exists'}
        assert guarded == {'logic': 'AND', 'filters': [leaf('@local.weeds', '!=', 3), presence]}
        assert (
            datasets._strictify_negations(leaf('@local.tags', '!=', 'x'), partner=True, array_columns={'tags'}) is None
        )

    registry_database(scenario)


def test_a_version_keeps_no_history_of_local_fields():
    with pytest.raises(HTTPException) as refused:
        versions._reject_live_only_filters(
            {'logic': 'AND', 'filters': [{'field': '@local.weeds', 'operator': '>', 'value': 1}]},
        )
    assert refused.value.status_code == 400 and 'no history' in refused.value.detail


def test_the_query_language_writes_local_fields_as_they_are():
    [node] = parse_query('@local.weeds > 3', {'@local.weeds': NUMBER})
    assert node == {'field': '@local.weeds', 'operator': '>', 'value': 3}
    assert format_filter(node) == '@local.weeds > 3'


def test_local_fields_are_counted_and_searched(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(filter_query_router, 'db', database)
        first, second, _, _ = await mirror_with_local_fields(database)
        # The local write recorded its fields for statistics.
        pending = await database.fetchval(
            'SELECT array_agg(DISTINCT name) FROM dataset_stats_pending, unnest(field_names) name WHERE dataset_id = 2',
        )
        assert {'@local.weeds', '@local.kind', '@local.tags'} <= set(pending)
        await run_refresh(database, 2)
        stats = {
            row['field_name']: row
            for row in await database.fetch(
                "SELECT * FROM dataset_field_stats WHERE dataset_id = 2 AND field_name LIKE '@local.%'",
            )
        }
        assert (stats['@local.weeds']['min_value'], stats['@local.weeds']['max_value']) == (3, 5)
        assert stats['@local.weeds']['sample_count'] == 2
        assert sorted(stats['@local.kind']['unique_values']) == ['dock', 'thistle']
        assert sorted(stats['@local.tags']['unique_values']) == ['checked', 'redo']

        # The query language knows them, and the sidebar can filter on them.
        kinds = await filter_query_router.field_kinds(2)
        assert (kinds['@local.weeds'], kinds['@local.tags']) == ('number', 'list')
        schema = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(2, None)}
        assert schema['@local.weeds'].get('filterable', True) and schema['@local.kind'].get('filterable', True)

        histogram = await datasets._histogram(2, '@local.weeds', 2, None, 50, False)
        assert sum(bucket['count'] for bucket in histogram['buckets']) == 2
        tags = await datasets._histogram(2, '@local.tags', None, None, 50, False)
        assert {bucket['key']: bucket['count'] for bucket in tags['buckets']} == {'checked': 2, 'redo': 1}

        values = await datasets._autocomplete_impl(2, '@local.kind', 'do', 100, None)
        assert [entry['value'] for entry in values['values']] == ['dock']
        values = await datasets._autocomplete_impl(2, '@local.tags', 're', 100, None)
        assert [entry['value'] for entry in values['values']] == ['redo']

        only_first = {'field': 'id', 'operator': 'in', 'value': [first]}
        assert await datasets.compute_filtered_values(2, '@local.kind', only_first) == ['thistle']
        both = {'field': 'id', 'operator': 'in', 'value': [first, second]}
        assert await datasets.compute_filtered_minmax(2, '@local.weeds', both) == {'min': 3, 'max': 5}

    registry_database(scenario)


def test_local_fields_in_joins_cross_grain_filters_sorts_and_views(registry_database):
    """The places a local filter meets other SQL: the vector-search join (samples aliased `s`), a cross-grain
    subquery (aliased `o`), a sort, and a view that must stay on the mirror."""

    async def scenario(database):
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=2, prefix='s3://images/m')
        image, other = [row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = 2')]
        # A third object, under the first image, has no local value: a negation must not match it.
        await ingest(
            [{'parent_id': image}, {'parent_id': other}, {'parent_id': image}],
            dataset_id=2,
            prefix='s3://images/o',
        )
        await database.execute("UPDATE samples SET media_type = 'object' WHERE filepath LIKE 's3://images/o%'")
        first_object, second_object, bare_object = [
            row['id'] for row in await database.fetch("SELECT id FROM samples WHERE media_type = 'object' ORDER BY id")
        ]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
            "SELECT dataset_id, id, 'clip', '[1,0]' FROM samples",
        )
        await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('fridge.mirror_local', '2', TRUE)")
            await connection.execute(
                """UPDATE samples SET local_metadata = CASE id WHEN $1::int THEN '{"weeds": 3, "label": "dock"}'::jsonb
                                                              WHEN $2::int THEN '{"weeds": 9}'::jsonb
                                                              WHEN $3::int THEN '{"weeds": 5}'::jsonb END
                   WHERE id = ANY(ARRAY[$1::int, $2::int, $3::int])""",
                image,
                first_object,
                second_object,
            )

        # The vector search's join: samples is `s`, and the local column resolves to it.
        args: list = []
        node = {'field': '@local.weeds', 'operator': '>', 'value': 4}
        sql, _ = datasets.parse_filter_node(node, args, 1, datasets.SAMPLES_JOIN_FIELD_MAPPING, dataset_id=2)
        joined = await database.fetch(
            f"""SELECT s.id FROM embeddings e JOIN samples s ON e.sample_id = s.id
                WHERE s.dataset_id = 2 AND e.field_name = 'clip' AND {sql} ORDER BY s.id""",
            *args,
        )
        assert [row['id'] for row in joined] == [first_object, second_object]

        # Cross-grain: an image whose object has a local value, and a negation that needs the field present.
        def images_with(leaf):
            return {'field': '_cg', 'operator': 'has_matching_object', 'value': leaf}

        assert await matching(database, images_with({'field': '@local.weeds', 'operator': '>', 'value': 6})) == [image]
        either = images_with({'field': '@local.weeds', 'operator': '!=', 'value': 9})
        assert await matching(database, either) == [other]

        # `@local.label` is a local field like any other, never a label path.
        dock = {'field': '@local.label', 'operator': '=', 'value': 'dock'}
        assert await matching(database, dock) == [image]

        # Sorted by a local field: numbers as numbers, missing values last, ties by id.
        number = {'field_type': 'number', 'is_promoted': False}
        for direction, expected in (
            ('ASC', [image, second_object, first_object]),
            ('DESC', [first_object, second_object, image]),
        ):
            order = datasets._grid_order_by('@local.weeds', direction, number)
            rows = await database.fetch(f'SELECT id FROM samples WHERE dataset_id = 2 {order}')
            assert [row['id'] for row in rows] == expected + [other, bare_object]

    registry_database(scenario)


def test_a_view_on_a_local_field_stays_on_the_mirror():
    from backend import follower_sync

    local = {'filters': {'logic': 'AND', 'filters': [{'field': '@local.weeds', 'operator': '>', 'value': 3}]}}
    assert follower_sync.instance_bound(local)
    assert not follower_sync.instance_bound({'filters': {'field': 'weeds', 'operator': '>', 'value': 3}})
    # The query language writes a plain local name as it is, and backticks one the compiler would refuse.
    assert format_filter({'field': '@local.a.b', 'operator': '=', 'value': 1}) == '`@local.a.b` = 1'
