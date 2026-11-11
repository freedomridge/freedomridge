"""Run with FRIDGE_TEST_POSTGRES_DSN and FRIDGE_TEST_POSTGRES_CONTAINER (a disposable PostGIS server)."""

import asyncio
import os
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import HTTPException

from backend.database import Database, init_connection
from backend.routers import datasets, versions
from backend.schema_check import function_statements

# Only the DSN that conftest verified against the disposable container: no other way to name a server.
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


@pytest.fixture
def snapshot_database(monkeypatch):
    def run(scenario):
        async def execute():
            connection_args = {'dsn': os.environ['FRIDGE_TEST_POSTGRES_DSN']}
            admin = await asyncpg.connect(**connection_args)
            namespace = 'snapshot_test_' + uuid.uuid4().hex
            database = Database()
            try:
                await admin.execute('CREATE EXTENSION IF NOT EXISTS postgis')
                await admin.execute(f'CREATE SCHEMA {namespace}')
                database.pool = await asyncpg.create_pool(
                    **connection_args,
                    min_size=1,
                    max_size=4,
                    init=init_connection,
                    server_settings={'search_path': f'{namespace},public'},
                )
                schema = (Path(__file__).parents[1] / 'db/schema.sql').read_text()
                for table in (
                    'users',
                    'datasets',
                    'views',
                    'samples',
                    'dataset_fields',
                    'dataset_field_stats',
                    'sample_hashes',
                    'dataset_versions',
                    'version_snapshots',
                    'sample_array_columns',
                    'region_selections',
                ):
                    definition = re.search(rf'CREATE TABLE IF NOT EXISTS {table} \([\s\S]*?\n\);', schema)
                    assert definition is not None
                    await database.execute(definition.group())
                for function_definition in function_statements():
                    if any(
                        f'FUNCTION {name}(' in function_definition
                        for name in (
                            'field_type_canonical',
                            'normalize_dataset_snapshot',
                            'dataset_snapshots_equal',
                        )
                    ):
                        await database.execute(function_definition)
                await database.execute('ALTER TABLE samples ADD COLUMN inferred_tags TEXT[]')
                await database.execute("INSERT INTO sample_array_columns VALUES ('inferred_tags')")
                await database.execute("INSERT INTO users (id, username) VALUES (1, 'tester')")
                await database.execute("INSERT INTO datasets (id, name) VALUES (1, 'training'), (2, 'other')")
                monkeypatch.setattr(versions, 'db', database)
                monkeypatch.setattr(datasets, 'db', database)
                monkeypatch.setattr(datasets, '_array_columns_cache', None)
                monkeypatch.setattr(versions, 'require_free_space', AsyncMock())
                monkeypatch.setattr(versions, 'estimate_snapshot_bytes', AsyncMock(return_value=0))
                await scenario(database)
            finally:
                if database.pool is not None:
                    await database.pool.close()
                await admin.execute(f'DROP SCHEMA IF EXISTS {namespace} CASCADE')
                await admin.close()

        asyncio.run(execute())

    return run


async def add_sample(database, path='image.jpg'):
    return await database.fetchval(
        """INSERT INTO samples (dataset_id, filepath, media_type, metadata, tags, inferred_tags)
           VALUES (1, $1, 'image', '{"label":"original", "mask":{"maskPath":"old-mask.png"}}',
                   ARRAY['train'], ARRAY['accepted']) RETURNING id""",
        path,
    )


async def snapshot(name):
    return (await versions.create_version(1, versions.CreateVersionRequest(name=name)))['id']


def test_checkout_survives_changes_and_deletion(snapshot_database):
    async def scenario(database):
        sample_id = await add_sample(database)
        await database.execute(
            "UPDATE datasets SET segmentation_config = '{\"default_mask_targets\":{\"1\":\"car\"}}'",
        )
        await database.execute(
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type) VALUES (1, 'mask', 'object')",
        )
        version_id = await snapshot('original')
        original = (await versions.get_version_samples(1, version_id, full=True))['items'][0]
        await database.execute(
            """UPDATE samples SET filepath = 'changed.jpg', media_type = 'video',
               location = ST_SetSRID(ST_MakePoint(10, 20), 4326), heading = 120,
               metadata = '{"label":"new"}', tags = '{}', inferred_tags = '{}', last_modified_at = NOW()""",
        )
        await database.execute("UPDATE datasets SET segmentation_config = '{}'::jsonb")
        current = (await versions.get_version_samples(1, version_id, full=True))['items'][0]
        assert current == original
        assert current['location'] is None and current['heading'] is None
        assert current['media_type'] == 'image' and current['inferred_tags'] == ['accepted']
        assert (await versions.diff_current(1, version_id))['dataset_changed'] is True
        assert (await versions.version_status(1))['has_changes'] is True
        await database.execute('DELETE FROM samples')
        assert (await versions.get_version_samples(1, version_id, full=True))['items'] == [original]
        info = await versions.get_version(1, version_id)
        assert info['dataset_snapshot']['segmentation_config']['default_mask_targets'] == {'1': 'car'}
        assert info['dataset_snapshot']['fields'][0]['field_name'] == 'mask'
        from backend.main import _build_media_path_sql

        media = await database.fetchrow(_build_media_path_sql('mask.maskPath', versioned=True), sample_id, version_id)
        assert media['path'] == 'old-mask.png' and media['dataset_id'] == 1
        with pytest.raises(HTTPException) as error:
            await versions.get_version_samples(2, version_id, full=True)
        assert error.value.status_code == 404

    snapshot_database(scenario)


@pytest.mark.parametrize(
    'assignment',
    [
        'heading = 90',
        'location = ST_SetSRID(ST_MakePoint(10,20),4326)',
        "filepath = 'renamed.jpg'",
        "media_type = 'object'",
        "tags = ARRAY['reviewed']",
        "metadata = '{\"label\":\"changed\"}'",
        "inferred_tags = ARRAY['different']",
        'inferred_tags = NULL',
    ],
)
def test_diffs_and_changed_sample_lists_agree(snapshot_database, assignment):
    async def scenario(database):
        sample_id = await add_sample(database)
        before = await snapshot('before')
        await database.execute(f'UPDATE samples SET {assignment}')
        after = await snapshot('after')
        for diff in (await versions.diff_versions(1, before, after), await versions.diff_current(1, before)):
            assert (diff['added'], diff['removed'], diff['modified'], diff['unchanged']) == (0, 0, 1, 0)
        assert (await versions.diff_samples(1, before, after, 'modified'))['sample_ids'] == [sample_id]
        assert (await versions.diff_current_samples(1, before, 'modified'))['sample_ids'] == [sample_id]
        assert (await versions.version_status(1))['current_version']['id'] == after
        await database.execute('UPDATE samples SET inferred_tags = NULL')
        nullable = await snapshot('nullable')
        assert (await versions.diff_current(1, nullable))['modified'] == 0
        await versions.revert_to_version(1, before, user={'id': 1})
        assert (await versions.diff_current(1, before))['modified'] == 0
        await versions.revert_to_version(1, nullable, user={'id': 1})
        assert await database.fetchval('SELECT inferred_tags FROM samples WHERE id = $1', sample_id) is None
        assert (await versions.diff_current(1, nullable))['modified'] == 0

    snapshot_database(scenario)


def test_empty_version_and_exact_membership_restore(snapshot_database):
    async def scenario(database):
        empty = await snapshot('empty')
        first_id = await add_sample(database, 'first.jpg')
        original = await snapshot('original')
        await database.execute('DELETE FROM samples')
        second_id = await add_sample(database, 'second.jpg')
        await versions.revert_to_version(1, original, user={'id': 1})
        assert [row['id'] for row in await database.fetch('SELECT id FROM samples')] == [second_id]
        restored = await versions.revert_to_version(1, original, exact_membership=True, user={'id': 1})
        assert restored['samples_removed'] == 1 and restored['samples_restored'] == 1
        assert [row['id'] for row in await database.fetch('SELECT id FROM samples')] == [first_id]
        undo = restored['auto_snapshot_id']
        await versions.revert_to_version(1, undo, exact_membership=True, user={'id': 1})
        assert [row['id'] for row in await database.fetch('SELECT id FROM samples')] == [second_id]
        await versions.revert_to_version(1, empty, exact_membership=True, user={'id': 1})
        assert await database.fetchval('SELECT count(*) FROM samples') == 0
        assert (await versions.get_version_samples(1, empty, full=True))['items'] == []

    snapshot_database(scenario)


def test_settings_changes_and_legacy_snapshots(snapshot_database):
    async def scenario(database):
        await add_sample(database)
        before = await snapshot('before')
        await database.execute("UPDATE datasets SET modal_media_field = 'thumbnail' WHERE id = 1")
        after = await snapshot('after')
        diff = await versions.diff_versions(1, before, after)
        assert diff['modified'] == 0 and diff['dataset_changed'] is True
        await database.execute(
            'UPDATE dataset_versions SET snapshot_format = 1, dataset_snapshot = NULL WHERE id = $1',
            before,
        )
        await database.execute(
            'UPDATE version_snapshots SET media_type = NULL, created_at = NULL, last_modified_at = NULL '
            'WHERE version_id = $1',
            before,
        )
        await database.execute('UPDATE samples SET heading = 120')
        legacy = (await versions.get_version_samples(1, before, full=True))['items'][0]
        assert legacy['heading'] is None and legacy['media_type'] == 'unknown' and legacy['created_at'] is None
        modern = await snapshot('modern')
        assert (await versions.diff_versions(1, before, modern))['modified'] == 0
        await database.execute('UPDATE version_snapshots SET heading = 90 WHERE version_id = $1', before)
        await database.execute('UPDATE samples SET heading = NULL')
        cleared = await snapshot('cleared')
        assert (await versions.diff_versions(1, before, cleared))['modified'] == 1

    snapshot_database(scenario)


def test_search_and_diff_browse_use_saved_rows(snapshot_database):
    async def scenario(database):
        first_id = await add_sample(database, 'percent%_first.jpg')
        await add_sample(database, 'second.jpg')
        before = await snapshot('before')
        await database.execute('DELETE FROM samples WHERE id = $1', first_id)
        result = await versions.get_version_samples(1, before, full=True, change_type='removed')
        assert result['total'] == 1 and result['items'][0]['id'] == first_id
        result = await versions.get_version_samples(1, before, full=True, q='%_')
        assert result['total'] == 1 and result['items'][0]['id'] == first_id
        result = await versions.get_version_samples(1, before, full=True, after_id=first_id)
        assert result['total'] == 2 and len(result['items']) == 1
        after = await snapshot('after')
        result = await versions.get_version_samples(
            1,
            before,
            full=True,
            change_type='removed',
            diff_base=before,
            diff_target=after,
        )
        assert result['total'] == 1 and result['items'][0]['id'] == first_id

    snapshot_database(scenario)


def test_hash_only_is_never_reported_as_exact_state(snapshot_database):
    async def scenario(database):
        await add_sample(database)
        record = await versions.create_version(1, versions.CreateVersionRequest(name='hashes', include_metadata=False))
        await database.execute("UPDATE samples SET tags = ARRAY['edited']")
        assert (await versions.version_status(1))['current_version'] is None
        with pytest.raises(HTTPException) as error:
            await versions.get_version_samples(1, record['id'], full=True)
        assert error.value.status_code == 400

    snapshot_database(scenario)


def test_conflicting_restore_rolls_back_everything(snapshot_database):
    async def scenario(database):
        first_id = await add_sample(database)
        before = await snapshot('before')
        await database.execute("UPDATE samples SET filepath = 'renamed.jpg' WHERE id = $1", first_id)
        await add_sample(database)
        with pytest.raises(HTTPException) as error:
            await versions.revert_to_version(1, before, user={'id': 1})
        assert error.value.status_code == 409
        assert await database.fetchval('SELECT count(*) FROM dataset_versions') == 1
        assert await database.fetchval('SELECT count(*) FROM samples') == 2
        assert await database.fetchval('SELECT filepath FROM samples WHERE id = $1', first_id) == 'renamed.jpg'
        await versions.revert_to_version(1, before, exact_membership=True, user={'id': 1})
        assert await database.fetchval('SELECT count(*) FROM samples') == 1
        assert await database.fetchval('SELECT filepath FROM samples WHERE id = $1', first_id) == 'image.jpg'

    snapshot_database(scenario)


def test_snapshot_has_one_consistent_state_during_ingest(snapshot_database, monkeypatch):
    async def scenario(database):
        await add_sample(database)
        await database.execute(
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type, display_type) "
            "VALUES (1, 'mask', 'object', 'segmentation')",
        )
        read_arrays = versions._snapshot_arrays

        async def concurrent_ingest(conn):
            await database.execute("UPDATE samples SET metadata = '{\"label\":\"new\"}'")
            await database.execute("UPDATE dataset_fields SET display_type = 'text'")
            await add_sample(database, 'later.jpg')
            return await read_arrays(conn)

        monkeypatch.setattr(versions, '_snapshot_arrays', concurrent_ingest)
        version_id = await snapshot('during-ingest')
        result = await versions.get_version_samples(1, version_id, full=True)
        assert result['total'] == 1 and len(result['items']) == 1
        assert result['items'][0]['metadata']['label'] == 'original'
        settings = (await versions.get_version(1, version_id))['dataset_snapshot']
        assert settings['name'] == 'training'
        assert settings['fields'][0]['display_type'] == 'segmentation'

    snapshot_database(scenario)


def test_historical_arrays_survive_registry_removal(snapshot_database):
    async def scenario(database):
        sample_id = await add_sample(database)
        before = await snapshot('before')
        await database.execute("UPDATE samples SET inferred_tags = ARRAY['changed']")
        after = await snapshot('after')
        await database.execute("DELETE FROM sample_array_columns WHERE column_name = 'inferred_tags'")
        await database.execute('ALTER TABLE samples DROP COLUMN inferred_tags')
        assert (await versions.diff_versions(1, before, after))['modified'] == 1
        assert (await versions.diff_samples(1, before, after, 'modified'))['sample_ids'] == [sample_id]
        saved = (await versions.get_version_samples(1, before, full=True))['items'][0]
        assert saved['array_fields'] == {'inferred_tags': ['accepted']}

    snapshot_database(scenario)


def test_snapshot_settings_conflict_is_retryable(snapshot_database, monkeypatch):
    async def scenario(database):
        await add_sample(database)
        read_arrays = versions._snapshot_arrays
        renames = 0

        async def concurrent_settings(conn):
            # A new name each attempt: an update that leaves the settings as the snapshot read them isn't a conflict.
            nonlocal renames
            renames += 1
            await database.execute('UPDATE datasets SET name = $1 WHERE id = 1', f'renamed {renames}')
            return await read_arrays(conn)

        monkeypatch.setattr(versions, '_snapshot_arrays', concurrent_settings)
        with pytest.raises(HTTPException) as error:
            await snapshot('during-settings-change')
        assert error.value.status_code == 409
        assert 'retry' in error.value.detail
        assert await database.fetchval('SELECT count(*) FROM dataset_versions') == 0
        assert await database.fetchval('SELECT count(*) FROM version_snapshots') == 0
        monkeypatch.setattr(versions, '_snapshot_arrays', read_arrays)
        version_id = await snapshot('during-settings-change')
        assert (await versions.get_version(1, version_id))['dataset_snapshot']['name'] == 'renamed 3'

    snapshot_database(scenario)


async def add_training_samples(database):
    await database.execute(
        "INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin) "
        "VALUES (1, 'inferred_tags', 'list', 'native_array')",
    )
    for path, split, crop, accepted in (
        ('a.jpg', 'train', 'corn', 'yes'),
        ('b.jpg', 'train', 'soy', 'no'),
        ('c.jpg', 'val', 'corn', 'yes'),
        ('d.jpg', 'test', 'soy', 'yes'),
    ):
        await database.execute(
            """INSERT INTO samples (dataset_id, filepath, media_type, metadata, tags, inferred_tags, location)
               VALUES (1, $1, 'image', jsonb_build_object('split', $2::text, 'category', $3::text,
                                                          'source_id', 'm-' || $2::text, 'big', 'x'),
                       ARRAY['t'], ARRAY[$4::text], ST_SetSRID(ST_MakePoint(1, 2), 4326))""",
            path,
            split,
            crop,
            accepted,
        )


def aggregate_request(*specs, filters=None):
    return versions.VersionAggregateRequest(
        aggregations=[datasets.AggregationSpec(field=field, op=op) for field, op in specs],
        filters=filters,
    )


def test_version_aggregates_are_computed_from_the_snapshot(snapshot_database):
    async def scenario(database):
        await add_training_samples(database)
        version_id = await snapshot('v1')
        await database.execute("UPDATE samples SET metadata = metadata || '{\"split\": \"gone\"}'")
        await database.execute("DELETE FROM samples WHERE filepath = 'a.jpg'")
        await database.execute(
            "INSERT INTO samples (dataset_id, filepath, media_type, metadata) VALUES (1, 'new.jpg', 'image', '{}')",
        )

        specs = (('id', 'count'), ('split', 'count_values'), ('source_id', 'distinct'))
        counts, split_counts, source_ids = (await versions.aggregate_version(1, version_id, aggregate_request(*specs)))[
            'results'
        ]
        assert counts['value'] == 4
        assert {row['value']: row['count'] for row in split_counts['value']} == {'train': 2, 'val': 1, 'test': 1}
        assert source_ids['value'] == ['m-test', 'm-train', 'm-val']

        in_train_or_val = {'field': 'split', 'operator': 'in', 'value': ['train', 'val']}
        filtered = await versions.aggregate_version(
            1,
            version_id,
            aggregate_request(('id', 'count'), ('category', 'count_values'), filters=in_train_or_val),
        )
        assert filtered['results'][0]['value'] == 3
        assert {row['value']: row['count'] for row in filtered['results'][1]['value']} == {'corn': 2, 'soy': 1}

        # Array fields are rebuilt from the snapshot, and native columns filter too.
        accepted = await versions.aggregate_version(
            1,
            version_id,
            aggregate_request(('id', 'count'), filters={'field': 'inferred_tags', 'operator': 'in', 'value': ['yes']}),
        )
        assert accepted['results'][0]['value'] == 3
        located = await versions.aggregate_version(
            1,
            version_id,
            aggregate_request(('id', 'count'), filters={'field': 'location', 'operator': 'exists'}),
        )
        assert located['results'][0]['value'] == 4

    snapshot_database(scenario)


def test_version_sample_query_filters_projects_and_pages(snapshot_database):
    async def scenario(database):
        await add_training_samples(database)
        version_id = await snapshot('v1')
        await database.execute("UPDATE samples SET metadata = metadata || '{\"split\": \"gone\"}'")
        in_train_or_val = {'field': 'split', 'operator': 'in', 'value': ['train', 'val']}

        def query(**kwargs):
            return versions.query_version_samples(1, version_id, versions.VersionSamplesQueryRequest(**kwargs))

        everything = (await query(filters=in_train_or_val))['items']
        assert [item['filepath'] for item in everything] == ['a.jpg', 'b.jpg', 'c.jpg']
        assert everything[0]['metadata']['split'] == 'train' and everything[0]['version_id'] == version_id
        assert everything[0]['location']['coordinates'] == [1.0, 2.0]

        slim = (await query(filters=in_train_or_val, fields=['split', 'inferred_tags', 'filepath']))['items']
        assert slim[1] == {
            'id': everything[1]['id'],
            'version_id': version_id,
            'filepath': 'b.jpg',
            'inferred_tags': ['no'],
            'metadata': {'split': 'train'},
        }
        assert (await query(fields=['nonexistent']))['items'][0]['metadata'] == {}

        first = (await query(filters=in_train_or_val, limit=2))['items']
        rest = (await query(filters=in_train_or_val, limit=2, after_id=first[-1]['id']))['items']
        assert [item['filepath'] for item in first + rest] == ['a.jpg', 'b.jpg', 'c.jpg']

    snapshot_database(scenario)


def test_version_queries_refuse_what_a_version_cannot_answer(snapshot_database):
    async def scenario(database):
        await add_training_samples(database)
        version_id = await snapshot('v1')
        hash_only = (await versions.create_version(1, versions.CreateVersionRequest(name='h', include_metadata=False)))[
            'id'
        ]
        count = aggregate_request(('id', 'count'))

        with pytest.raises(HTTPException) as error:
            await versions.aggregate_version(2, version_id, count)  # another dataset's version
        assert error.value.status_code == 404
        with pytest.raises(HTTPException) as error:
            await versions.aggregate_version(1, hash_only, count)
        assert error.value.status_code == 400
        for node in (
            {'field': 'id', 'operator': 'collapse_duplicates', 'value': {}},
            {'field': 'embeddings.clip', 'operator': 'exists'},
            {'logic': 'AND', 'filters': [{'field': 'hashes.phash', 'operator': 'not_exists'}]},
        ):
            with pytest.raises(HTTPException) as error:
                await versions.aggregate_version(1, version_id, aggregate_request(('id', 'count'), filters=node))
            assert error.value.status_code == 400 and 'live data' in error.value.detail
        with pytest.raises(HTTPException) as error:
            await versions.query_version_samples(
                1,
                version_id,
                versions.VersionSamplesQueryRequest(fields=["x'; DROP TABLE samples; --"]),
            )
        assert error.value.status_code == 400
        with pytest.raises(HTTPException) as error:
            await versions.query_version_samples(1, version_id, versions.VersionSamplesQueryRequest(limit=0))
        assert error.value.status_code == 400
        assert (await versions.aggregate_version(1, version_id, count))['results'][0]['value'] == 4

    snapshot_database(scenario)
