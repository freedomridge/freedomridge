"""A stored metadata key named like a sample's own field (`id`, `dataset_id`, `filepath`, `created_at`,
`last_modified_at`; `tags` and `media_type` too) is no field: no path that finds fields in metadata registers it (an
edit of another key, a bulk set, a revert to a version saved while it was registered, an older version's inference,
a mirror's sync from a primary that still lists it, overlay discovery), and a filter or a value search by its name
answers as on a dataset where no sample has it. The stored values stay in the samples."""

import os

import pytest
from fastapi import HTTPException

from backend import follower_sync, label_fields
from backend.field_registry import RESERVED_METADATA_KEYS, field_stats, infer_fields, without_reserved_fields
from backend.routers import datasets, versions
from backend.tests import test_field_registry_postgres
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
FIVE = ('id', 'dataset_id', 'filepath', 'created_at', 'last_modified_at')
STORED = {'created_at': '1999-03-04T05:06:07Z', 'dataset_id': 'patched', 'id': 99, 'filepath': 'old.jpg'}
REGISTERED = "SELECT field_name || '/' || origin FROM dataset_fields WHERE dataset_id = $1 AND field_name = ANY($2)"


def test_inference_never_registers_a_name_of_a_samples_own_field():
    assert infer_fields(
        [{**STORED, 'last_modified_at': 'x', 'tags': 'a', 'media_type': 'b', 'created_at_source': 1}],
    ) == {
        'created_at_source': 'number',  # a look-alike is an ordinary key
    }


def test_definitions_named_as_a_samples_own_field_go_back_to_its_row_or_go():
    written = without_reserved_fields(
        [
            {
                'field_name': 'created_at',
                'field_type': 'string',
                'origin': 'metadata',
                'display_type': 'enum',
                'overlay_config': {'a': 1},
                'description': 'When',
                'unit': None,
            },
            {
                'field_name': 'dataset_id',
                'field_type': 'string',
                'origin': 'metadata',
                'description': None,
                'unit': None,
            },
            {'field_name': 'filepath', 'field_type': 'unknown', 'origin': 'system', 'description': 'Path', 'unit': 'p'},
            {'field_name': 'n', 'field_type': 'number', 'origin': 'metadata', 'description': None, 'unit': None},
        ],
    )
    assert written == [
        {
            'field_name': 'created_at',
            'field_type': 'unknown',
            'origin': 'system',
            'display_type': None,
            'overlay_config': None,
            'description': 'When',
            'unit': None,
        },
        {'field_name': 'filepath', 'field_type': 'unknown', 'origin': 'system', 'description': 'Path', 'unit': 'p'},
        {'field_name': 'n', 'field_type': 'number', 'origin': 'metadata', 'description': None, 'unit': None},
    ]


async def stored_sample(database):
    ids = (await ingest([{'n': 0}, {'n': 1}]))['ids']
    await database.execute('UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1', ids[0], STORED)
    return ids


@needs_postgres
def test_edits_and_bulk_sets_around_a_stored_key_register_none(registry_database):
    async def scenario(database):
        ids = await stored_sample(database)
        await datasets.update_sample(1, ids[0], datasets.SampleUpdate(metadata={**STORED, 'n': 5}), USER)
        await datasets.bulk_set_metadata(1, datasets.BulkSetRequest(sample_ids=ids, metadata={'checked': True}), USER)
        assert await database.fetch(REGISTERED, 1, list(RESERVED_METADATA_KEYS)) == []
        assert {**STORED, 'n': 5, 'checked': True} == await database.fetchval(
            'SELECT metadata FROM samples WHERE id = $1',
            ids[0],
        )

    registry_database(scenario)


@needs_postgres
@pytest.mark.parametrize('old_format', [False, True])
def test_a_revert_to_a_version_saved_while_they_were_registered_brings_no_field_back(registry_database, old_format):
    async def scenario(database):
        ids = await stored_sample(database)
        # The registry an older release left: metadata fields of the names, one described.
        await database.execute(
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, description) VALUES "
            "(1, 'created_at', 'string', 'metadata', 'When it was taken'), "
            "(1, 'dataset_id', 'string', 'metadata', NULL)",
        )
        version = await versions.create_version(1, versions.CreateVersionRequest(name='registered'))
        if old_format:  # a version from before the registry was saved: its fields are inferred from its samples
            await database.execute(
                "UPDATE dataset_versions SET dataset_snapshot = dataset_snapshot - 'field_registry_version' - 'fields' "
                'WHERE id = $1',
                version['id'],
            )
        # What migration 0043 leaves: the described one is the built-in's row, the other is gone.
        await database.execute(
            "UPDATE dataset_fields SET field_type = 'unknown', origin = 'system' "
            "WHERE dataset_id = 1 AND field_name = 'created_at'",
        )
        await database.execute("DELETE FROM dataset_fields WHERE dataset_id = 1 AND field_name = 'dataset_id'")
        await database.execute('DELETE FROM samples WHERE id = $1', ids[1])

        await versions.revert_to_version(1, version['id'], restore_deleted=True, user=USER)

        assert sorted(row[0] for row in await database.fetch(REGISTERED, 1, list(FIVE))) == ['created_at/system']
        described = "SELECT description FROM dataset_fields WHERE dataset_id = 1 AND field_name = 'created_at'"
        assert await database.fetchval(described) == 'When it was taken'
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 1') == 2  # the revert ran

    registry_database(scenario)


@needs_postgres
def test_a_mirror_takes_a_primarys_such_field_as_no_field(registry_database):
    async def scenario(database):
        await database.execute(
            'INSERT INTO primary_links (url, token_encrypted, identity) '
            "VALUES ('https://primary.example/api', 'x', 'c')",
        )
        await database.execute(
            "INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, state, feed_cursor) "
            "SELECT 2, id, 7, 'following', 1 FROM primary_links",
        )
        listed = [
            {
                'field_name': 'created_at',
                'field_type': 'string',
                'origin': 'metadata',
                'display_type': None,
                'overlay_config': None,
                'description': 'When',
                'unit': None,
            },
            {
                'field_name': 'dataset_id',
                'field_type': 'string',
                'origin': 'metadata',
                'display_type': None,
                'overlay_config': None,
                'description': None,
                'unit': None,
            },
            {
                'field_name': 'n',
                'field_type': 'number',
                'origin': 'metadata',
                'display_type': None,
                'overlay_config': None,
                'description': None,
                'unit': None,
            },
        ]
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('fridge.mirror_sync', '2', TRUE)")
            await follower_sync.write_fields(connection, 2, listed)
        rows = await database.fetch(
            "SELECT field_name, origin, description FROM dataset_fields WHERE dataset_id = 2 ORDER BY field_name",
        )
        assert [tuple(row) for row in rows] == [('created_at', 'system', 'When'), ('n', 'metadata', None)]

    registry_database(scenario)


@needs_postgres
def test_overlay_discovery_lists_no_such_key(registry_database):
    async def scenario(database):
        ids = (await ingest([{'n': 0}]))['ids']
        heatmap = {'type': 'heatmap', 'map_path': 'm.png', 'range': [0, 1]}
        await database.execute(
            'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
            ids[0],
            {'dataset_id': heatmap, 'heat': heatmap},
        )
        await database.execute("DELETE FROM dataset_field_stats WHERE field_name = '__heatmap_fields__'")
        assert await label_fields.discover_fields(database.pool, 1, '__heatmap_fields__') == ['heat']

    registry_database(scenario)


@needs_postgres
def test_a_filter_or_a_value_search_by_such_a_name_answers_as_without_the_key(registry_database):
    async def scenario(database):
        await stored_sample(database)
        for node, matching in (
            ({'field': 'dataset_id', 'operator': 'exists'}, 0),
            ({'field': 'dataset_id', 'operator': '=', 'value': 'patched'}, 0),
            ({'field': 'dataset_id', 'operator': 'not_exists'}, 2),
            ({'field': 'filepath', 'operator': '=', 'value': 'old.jpg'}, 0),  # the column, as before
        ):
            args = [1]
            sql, _ = datasets.parse_filter_node(node, args, 2, dataset_id=1, array_columns=set())
            assert await database.fetchval(f'SELECT count(*) FROM samples WHERE dataset_id = $1 AND {sql}', *args) == (
                matching
            ), node
        for name in FIVE:
            answer = await datasets._autocomplete_impl(1, name, '', 100)
            assert answer['values'] == [], (name, answer)

    registry_database(scenario)


async def answers(dataset_id, name):
    """What describe, promote, clone, rename and clear answer for `name`, in that order: 200 or the refusal's status."""
    calls = (
        lambda: datasets.set_field_description(
            dataset_id,
            name,
            datasets.FieldDescriptionRequest(description='x'),
            USER,
        ),
        lambda: datasets.promote_field(dataset_id, name, USER),
        lambda: datasets.clone_field(dataset_id, name, datasets.FieldRenameRequest(new_name=f'copy_{name}'), USER),
        lambda: datasets.rename_field(dataset_id, name, datasets.FieldRenameRequest(new_name=f'old_{name}'), USER),
        lambda: datasets.clear_field(dataset_id, name, USER),
    )
    statuses = []
    for call in calls:
        try:
            await call()
            statuses.append(200)
        except HTTPException as error:
            statuses.append(error.status_code)
    return statuses


@needs_postgres
def test_a_registration_left_by_an_older_release_is_no_field_before_the_migration_too(registry_database):
    async def scenario(database):
        await stored_sample(database)
        await ingest([{'n': 0}], dataset_id=2, prefix='clean')
        # The rows an older release's edits registered, as they stand until migration 0043 runs (or on a mirror).
        await database.execute(
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin) "
            "SELECT 1, name, 'string', 'metadata' FROM unnest($1::text[]) name",
            list(FIVE),
        )
        async with database.pool.acquire() as connection:
            listed = {row['field_name'] for row in await field_stats(connection, 1)}
        assert listed.isdisjoint(FIVE)  # dataset_id isn't listed; the built-ins are listed as themselves
        # The field routes by name answer as on a dataset without stored keys (main's answers there).
        for name, expected in (('created_at', [200, 400, 400, 400, 400]), ('dataset_id', [404, 404, 404, 404, 200])):
            assert await answers(2, name) == expected, name
            assert await answers(1, name) == expected, name
        # A value list of such a name reads no stored key: count_values and the number aggregations.
        for name in ('dataset_id', 'created_at', 'last_modified_at'):
            assert datasets._agg_value_expr(name, set()) == f"'{{}}'::jsonb->>'{name}'"
        assert datasets._number_source('dataset_id', set()).sql == "'{}'::jsonb->>'dataset_id'"
        assert datasets._agg_value_expr('filepath', set()) == 'filepath'  # a column, as before

    registry_database(scenario)


ENTRY_KEYS = (
    'type',
    'values',
    'is_promoted',
    'display_type',
    'overlay_config',
    'origin',
    'sortable',
    'description',
    'unit',
    'values_private',
)


def entries(listing, name):
    return [{key: entry.get(key) for key in ENTRY_KEYS} for entry in listing if entry.get('name') == name]


@needs_postgres
def test_a_promoted_leftover_tags_or_media_type_lends_the_samples_own_entry_nothing(registry_database):
    async def scenario(database):
        for dataset_id in (1, 2):
            await ingest([{'n': 0}, {'n': 1}], dataset_id=dataset_id, prefix=f'd{dataset_id}')
        await database.execute("UPDATE samples SET tags = '{own}'")
        # Dataset 1 as before migration 0042: the keys stored, and promoted metadata fields of the names with their
        # indexes, one with display settings.
        await database.execute(
            "UPDATE samples SET metadata = metadata || jsonb_build_object('tags', 'L' || id, 'media_type', 'k') "
            'WHERE dataset_id = 1',
        )
        await database.execute(
            'INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, is_promoted, display_type, '
            "overlay_config) VALUES (1, 'tags', 'string', 'metadata', TRUE, 'enum', '{\"colormap\": \"viridis\"}'), "
            "(1, 'media_type', 'string', 'metadata', TRUE, NULL, NULL)",
        )
        await database.execute(
            "CREATE INDEX idx_samples_meta_1_tags ON samples ((metadata->>'tags')) WHERE dataset_id = 1",
        )
        stats = {dataset_id: await datasets.get_dataset_field_stats(dataset_id) for dataset_id in (1, 2)}
        schema = {dataset_id: await datasets.get_dataset_schema(dataset_id) for dataset_id in (1, 2)}
        for name in ('tags', 'media_type'):
            assert len(entries(stats[1], name)) == 1, (name, stats[1])
            assert entries(stats[1], name) == entries(stats[2], name), name
            assert entries(schema[1], name) == entries(schema[2], name), name
        assert entries(stats[1], 'tags')[0]['is_promoted'] is False
        assert entries(stats[1], 'tags')[0]['values'] == ['own']

    registry_database(scenario)


@needs_postgres
def test_a_lookup_by_such_a_name_answers_as_without_the_key(registry_database):
    async def scenario(database):
        ids = await stored_sample(database)
        await ingest([{'n': 0}], dataset_id=2, prefix='clean')
        for name, value in (*STORED.items(), ('tags', 'x'), ('media_type', 'image'), ('last_modified_at', 'x')):
            if name in ('id', 'filepath'):
                continue  # the sample's own field, as before
            for dataset_id in (1, 2):
                answer = await datasets.lookup_samples(dataset_id, datasets.LookupRequest(field=name, values=[value]))
                assert answer == {}, (name, dataset_id, answer)
        found = await datasets.lookup_samples(1, datasets.LookupRequest(field='filepath', values=['sample-0.jpg']))
        assert found == {'sample-0.jpg': ids[0]}  # the column, not the stored `filepath` key

    registry_database(scenario)


def test_the_scatter_and_the_sort_read_no_stored_key_of_such_a_name():
    from backend.routers import embeddings

    qualified, _ = embeddings._build_include_fields(sorted(RESERVED_METADATA_KEYS), 's')
    assert not [part for part in qualified if 'metadata' in part], qualified  # never the stored key
    # What the scatter gives on a dataset where no sample has the key: a column where it read one, else null.
    assert qualified == [
        's.created_at',
        's.dataset_id',
        's.filepath',
        's.id',
        'NULL::text as last_modified_at',
        'NULL::text as media_type',
        's.tags',
    ]
    for name in ('dataset_id', 'media_type', 'tags'):
        order = datasets._grid_order_by(name, 'ASC', {'field_type': 'string', 'is_promoted': False})
        assert 'metadata' not in order and "'{}'::jsonb" in order, order  # as on a dataset without the key


@needs_postgres
def test_diversity_sampling_reads_and_offers_no_stored_key_of_such_a_name(registry_database, monkeypatch):
    from backend.routers import sampling

    async def scenario(database):
        monkeypatch.setattr(sampling, 'db', database)
        for dataset_id in (1, 2):
            await ingest([{'n': k} for k in range(4)], dataset_id=dataset_id, prefix=f'd{dataset_id}')
        await database.execute(
            "UPDATE samples SET metadata = metadata || jsonb_build_object('created_at', id) WHERE dataset_id = 1",
        )
        await database.execute(
            "INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, sample_count) "
            "VALUES (1, 'created_at', 'number', 4)",
        )
        answers: list[tuple[object, object]] = []
        for dataset_id in (1, 2):
            try:
                result = await sampling.sparse_sample(
                    dataset_id,
                    sampling.SparseSampleRequest(fields=['created_at'], count=2),
                )
                answers.append(('ok', len(result.get('sample_ids', []))))
            except HTTPException as error:
                answers.append((error.status_code, error.detail))
        assert answers[0] == answers[1], answers  # the stored numbers are not a dimension
        offered = [field['name'] for field in await sampling.get_available_fields(1)]
        assert 'created_at' not in offered

    registry_database(scenario)


@needs_postgres
def test_statistics_publish_the_samples_own_tags_while_a_leftover_row_stands(registry_database):
    from backend.tests.test_statistics_jobs import refresh

    async def scenario(database):
        await ingest([{'n': 0}, {'n': 1}])
        await database.execute("UPDATE samples SET tags = '{own}', metadata = metadata || '{\"tags\": \"L\"}'")
        await database.execute(
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, is_promoted) "
            "VALUES (1, 'tags', 'string', 'metadata', TRUE), (1, 'created_at', 'string', 'metadata', FALSE)",
        )
        await refresh(database, force=True)
        rows = {
            row['field_name']: row
            for row in await database.fetch(
                "SELECT field_name, unique_values FROM dataset_field_stats WHERE field_name IN ('tags', 'created_at')",
            )
        }
        assert rows['tags']['unique_values'] == ['own']  # the sample's own, published
        assert 'created_at' not in rows  # the leftover field isn't measured

    registry_database(scenario)


@pytest.mark.parametrize('name', sorted(RESERVED_METADATA_KEYS))
def test_a_heatmap_overlay_by_such_a_name_is_none_as_without_the_key(name):
    import asyncio

    from backend import main

    with pytest.raises(HTTPException) as refused:
        asyncio.run(main._serve_composite(1, 1, 'base.jpg', name, None, 0.5))
    assert refused.value.status_code == 404  # what a sample without the key answers, before any read of it
    assert refused.value.detail == f'Sample 1 has no heatmap in metadata.{name}'


@needs_postgres
def test_the_media_route_reads_no_stored_key_of_such_a_name(registry_database):
    from backend import main

    async def scenario(database):
        ids = await stored_sample(database)
        stored = {name: f'stored/{name}.jpg' for name in RESERVED_METADATA_KEYS} | {
            'thumb': 't.jpg',
            'heat': {'map_path': 'm.png'},
        }
        await database.execute('UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1', ids[0], stored)
        for field in (*sorted(RESERVED_METADATA_KEYS - {'filepath'}), 'created_at.map_path', 'filepath.x'):
            row = await database.fetchrow(main._build_media_path_sql(field), ids[0])
            assert row['path'] is None, field  # no media, as for a sample without the key (the route's 404)
            assert row['dataset_id'] == 1, field  # the sample is still found and its access checked first
        own = await database.fetchrow(main._build_media_path_sql('filepath'), ids[0])
        assert own['path'] == 'sample-0.jpg'  # the sample's own path, as before
        for field, path in (('thumb', 't.jpg'), ('heat.map_path', 'm.png')):  # ordinary media fields, as before
            assert (await database.fetchrow(main._build_media_path_sql(field), ids[0]))['path'] == path

    registry_database(scenario)


def test_the_scatter_reads_nothing_into_a_stored_key_of_such_a_name():
    from backend.routers import embeddings

    qualified, _ = embeddings._build_include_fields(
        ['created_at.x', 'dataset_id.label', 'tags.label', 'media_type.a', 'detections.label'],
        's',
    )
    assert qualified[:4] == [
        'NULL::text as created_at_x',
        'NULL::text as dataset_id_label',
        'NULL::text as tags_label',
        'NULL::text as media_type_a',
    ]  # null, as the plain names give
    assert "s.metadata->'detections'" in qualified[4]  # an ordinary parent, as before
