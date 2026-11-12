"""Real PostgreSQL contract tests. Only an explicitly supplied disposable DSN is used."""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import HTTPException

from backend.database import PLANNING, Database, init_connection
from backend.routers import datasets, embeddings, quality, versions
from backend.tests.statistics_helpers import run_refresh

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
USER = {'id': 1, 'role': 'admin'}


@asynccontextmanager
async def bulk_setup(database):
    """A connection for a test's bulk setup, planned as before database.PLANNING. Re-planning the row triggers and
    foreign-key checks for every row doubles a 150,000-row insert, past the fixture's statement timeout when the
    machine is loaded. What a test checks still runs with the app's planning."""
    async with database.pool.acquire() as connection, connection.transaction():
        await connection.execute('SET LOCAL plan_cache_mode = auto')
        yield connection


@pytest.fixture
def registry_database(monkeypatch):
    def run(scenario, pool_size=5):
        async def execute():
            dsn = os.environ['FRIDGE_TEST_POSTGRES_DSN']
            admin = await asyncpg.connect(dsn)
            namespace = 'registry_test_' + uuid.uuid4().hex
            database = Database()
            try:
                await admin.execute('CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS vector')
                await admin.execute(f'CREATE SCHEMA {namespace}')
                database.pool = await asyncpg.create_pool(
                    dsn,
                    min_size=1,
                    max_size=pool_size,
                    init=init_connection,
                    server_settings={'search_path': f'{namespace},public', 'statement_timeout': '10000', **PLANNING},
                )
                await database.execute((Path(datasets.__file__).parents[1] / 'db/schema.sql').read_text())
                # Open to every user; tests needing a private dataset set FALSE.
                await database.execute(
                    "INSERT INTO datasets (id, name, is_global) VALUES (1, 'training', TRUE), (2, 'other', TRUE)",
                )
                for module in (datasets, embeddings, quality, versions):
                    monkeypatch.setattr(module, 'db', database)
                monkeypatch.setattr(datasets, '_array_columns_cache', None)
                monkeypatch.setattr(datasets, 'require_free_space', AsyncMock())
                monkeypatch.setattr(datasets, 'validate_embedding_dims', AsyncMock())
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


async def ingest(metadata, dataset_id=1, prefix='sample', on_duplicate='error'):
    return await datasets.add_samples(
        dataset_id,
        {
            'samples': [{'filepath': f'{prefix}-{index}.jpg', 'metadata': item} for index, item in enumerate(metadata)],
            'on_duplicate': on_duplicate,
        },
        user=USER,
    )


async def definitions(database, dataset_id=1):
    rows = await database.fetch('SELECT * FROM dataset_fields WHERE dataset_id = $1 ORDER BY field_name', dataset_id)
    return {row['field_name']: dict(row) for row in rows}


def test_new_fields_visible_without_statistics(registry_database):
    async def scenario(database):
        await ingest([{'captured': '2026-01-01', 'score': 3, 'mixed': 'x'}, {'mixed': 5}])
        await database.execute('DELETE FROM dataset_field_stats')
        for response in (
            await datasets.get_dataset_schema(1),
            await datasets._get_dataset_field_stats_impl(1, None),
        ):
            fields = {field['name']: field for field in response}
            assert fields['captured']['type'] == 'datetime'
            assert fields['score']['type'] == 'number'
            assert fields['mixed']['type'] == 'string'
            assert fields['captured']['stats_pending'] is True

    registry_database(scenario)


def test_cache_cannot_define_types_or_resurrect_fields(registry_database):
    async def scenario(database):
        await ingest([{'captured': '2026-01-01', 'mixed': 'word', 'cleared': 1, 'renamed': 1}])
        await ingest([{'captured': 'invalid later value', 'mixed': '2026-01-01'}], prefix='later')
        await datasets.clear_field(1, 'cleared', user=USER)
        await datasets.rename_field(1, 'renamed', datasets.FieldRenameRequest(new_name='new_name'), user=USER)
        await database.execute("""INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, unique_values)
            VALUES (1, 'cleared', 'number', NULL), (1, 'renamed', 'number', NULL),
                   (1, 'captured', 'string', ARRAY['2026-01-01']), (1, 'mixed', 'datetime', ARRAY['2026-01-01'])
            ON CONFLICT (dataset_id, field_name) DO UPDATE SET field_type = EXCLUDED.field_type""")
        fields = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert fields['captured']['type'] == 'datetime'
        assert fields['mixed']['declared_type'] == 'string'
        assert 'cleared' not in fields and 'renamed' not in fields
        assert fields['new_name']['type'] == 'number'

    registry_database(scenario)


def test_metadata_lists_do_not_join_native_catalog(registry_database):
    async def scenario(database):
        await database.execute('ALTER TABLE samples ADD COLUMN reviewed TEXT[]')
        await database.execute("INSERT INTO sample_array_columns VALUES ('reviewed')")
        await database.execute("""INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin)
                                  VALUES (2, 'reviewed', 'list', 'native_array')""")
        sample_id = (await ingest([{'reviewed': ['yes'], 'kw': ['leaf']}]))['ids'][0]
        assert (await definitions(database))['reviewed']['origin'] == 'metadata'
        assert 'reviewed' not in await datasets.get_dataset_array_columns(1)
        assert 'reviewed' not in await embeddings._array_field_names(1)
        assert 'kw' not in await embeddings._array_field_names(1)
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[1,0]')",
            sample_id,
        )
        await database.execute("INSERT INTO embedding_coordinates VALUES ($1, 'clip', 0, 0)", sample_id)
        result = await embeddings.get_embeddings_post(
            1,
            embeddings.EmbeddingsRequest(include_fields=['reviewed', 'kw'], sample_pct=100),
        )
        assert 'yes' in json.dumps(result) and 'leaf' in json.dumps(result)
        samples = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters={'field': 'reviewed', 'operator': 'contains', 'value': 'yes'}),
        )
        assert samples['total'] == 1
        assert samples['items'][0]['metadata']['reviewed'] == ['yes']

    registry_database(scenario)


def test_patch_bulk_and_unknown_widening(registry_database):
    async def scenario(database):
        sample_ids = (await ingest([{'unknown': None}, {}]))['ids']
        await datasets.update_sample(
            1,
            sample_ids[0],
            datasets.SampleUpdate(metadata={'patch_date': '2026-01-02'}),
            USER,
        )
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(
                updates=[
                    datasets.BulkSetUpdate(sample_id=sample_ids[0], metadata={'unknown': 1, 'mixed': 1}),
                    datasets.BulkSetUpdate(sample_id=sample_ids[1], metadata={'mixed': 'word'}),
                    datasets.BulkSetUpdate(sample_id=-1, metadata={'ghost': 1}),
                ],
            ),
            USER,
        )
        fields = await definitions(database)
        assert fields['unknown']['field_type'] == 'number'
        assert fields['patch_date']['field_type'] == 'datetime'
        assert fields['mixed']['field_type'] == 'string'
        assert 'ghost' not in fields

    registry_database(scenario)


def test_skipped_duplicates_never_register_phantom_fields(registry_database):
    async def scenario(database):
        first = await ingest([{'real': 1}])
        await ingest([{'ghost': 1}], on_duplicate='skip')
        assert set(await definitions(database)) == {'real'}
        assert first['ids']

    registry_database(scenario)


def test_large_first_write_order_and_types(registry_database):
    async def scenario(database):
        metadata = [{'mixed': '2026-01-01', 'number': index} for index in range(1200)]
        metadata[-1]['mixed'] = 'not a date'
        result = await ingest(metadata)
        rows = await database.fetch('SELECT id, filepath FROM samples ORDER BY id')
        assert result['ids'] == [row['id'] for row in rows]
        assert [row['filepath'] for row in rows] == [f'sample-{index}.jpg' for index in range(1200)]
        assert (await definitions(database))['mixed']['field_type'] == 'string'

    registry_database(scenario)


def test_duplicate_updates_preserve_first_creation_and_last_metadata(registry_database):
    async def scenario(database):
        result = await datasets.add_samples(
            1,
            {
                'on_duplicate': 'update',
                'samples': [
                    {
                        'filepath': 'duplicate.jpg',
                        'created_at': '2020-01-01T00:00:00Z',
                        'tags': ['first'],
                        'metadata': {'discarded': 1},
                    },
                    {
                        'filepath': 'duplicate.jpg',
                        'created_at': '2026-01-01T00:00:00Z',
                        'tags': ['last'],
                        'metadata': {'retained': 2},
                    },
                ],
            },
            user=USER,
        )
        assert len(result['ids']) == 2 and result['ids'][0] == result['ids'][1]
        sample = await database.fetchrow('SELECT created_at, tags, metadata FROM samples')
        assert sample['created_at'].year == 2020
        assert sample['tags'] == ['first', 'last']
        assert sample['metadata'] == {'retained': 2}
        assert set(await definitions(database)) == {'retained'}

    registry_database(scenario)


def test_replace_tags_makes_an_existing_samples_tags_the_ones_sent(registry_database):
    async def scenario(database):
        await datasets.add_samples(1, {'samples': [{'filepath': 'a.jpg', 'tags': ['old', 'kept']}]}, user=USER)
        await run_refresh(database)
        result = await datasets.add_samples(
            1,
            {
                'on_duplicate': 'update',
                'replace_tags': True,
                'samples': [{'filepath': 'a.jpg', 'tags': ['new', 'kept']}, {'filepath': 'b.jpg', 'tags': ['b']}],
            },
            user=USER,
        )
        assert result['replace_tags'] is True
        assert dict(await database.fetch('SELECT filepath, tags FROM samples')) == {
            'a.jpg': ['kept', 'new'],
            'b.jpg': ['b'],
        }
        stored = await database.fetchval("SELECT unique_values FROM dataset_field_stats WHERE field_name = 'tags'")
        assert 'old' not in (stored or []), f'the tag statistics still list a tag no sample has: {stored}'
        for mode in ('skip', 'error'):
            with pytest.raises(HTTPException) as refused:
                await datasets.add_samples(
                    1,
                    {'on_duplicate': mode, 'replace_tags': True, 'samples': [{'filepath': 'a.jpg'}]},
                    user=USER,
                )
            assert refused.value.status_code == 400

    registry_database(scenario)


def test_incremental_refresh_merges_values_before_certifying_preview(registry_database):
    async def scenario(database):
        await ingest([{'label': 'old'}])
        await run_refresh(database)
        assert await database.fetchval("SELECT values_complete FROM dataset_field_stats WHERE field_name = 'label'")
        await database.execute("""INSERT INTO samples (dataset_id, filepath, media_type, metadata)
                                  VALUES (1, 'later.jpg', 'image', '{"label":"new"}')""")
        await run_refresh(database)
        field = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'label')
        assert field['values_complete'] is True
        assert field['type'] == 'enum'
        assert set(field['values']) == {'old', 'new'}

    registry_database(scenario)


def test_registration_rolls_back_with_samples(registry_database):
    async def scenario(database):
        await database.execute("""CREATE FUNCTION refuse_registry() RETURNS trigger LANGUAGE plpgsql AS $$
                                  BEGIN RAISE EXCEPTION 'test failure'; END $$;
                                  CREATE TRIGGER refuse_registry BEFORE INSERT ON dataset_fields
                                  FOR EACH STATEMENT EXECUTE FUNCTION refuse_registry()""")
        with pytest.raises(asyncpg.RaiseError, match='test failure'):
            await ingest([{'new_field': 1}])
        assert await database.fetchval('SELECT count(*) FROM samples') == 0

    registry_database(scenario)


def test_statistics_does_not_change_current_version(registry_database):
    async def scenario(database):
        await ingest([{'day': '2026-01-01', 'label': 'car'}])
        version = await versions.create_version(1, versions.CreateVersionRequest(name='saved'))
        before = await definitions(database)
        await run_refresh(database)
        assert await definitions(database) == before
        async with database.pool.acquire() as conn:
            status = await versions._version_status(conn, 1)
        assert status['current_version']['id'] == version['id']
        assert not status['has_changes']

    registry_database(scenario)


def test_legacy_snapshot_normalization_and_strict_new_snapshots(registry_database):
    async def scenario(database):
        old: dict[str, Any] = {
            'name': 'training',
            'fields': [{'field_name': 'score', 'field_type': 'float'}, {'field_name': 'tags', 'field_type': 'string'}],
        }
        current: dict[str, Any] = {
            'name': 'training',
            'field_registry_version': 1,
            'fields': [
                {'field_name': 'score', 'field_type': 'number', 'origin': 'metadata'},
                {'field_name': 'backfilled', 'field_type': 'list', 'origin': 'metadata'},
            ],
        }
        assert await database.fetchval('SELECT dataset_snapshots_equal($1, $2)', old, current)
        old['field_registry_version'] = 1
        assert not await database.fetchval('SELECT dataset_snapshots_equal($1, $2)', old, current)
        old.pop('field_registry_version')
        removed = {**current, 'fields': []}
        assert not await database.fetchval('SELECT dataset_snapshots_equal($1, $2)', old, removed)
        current['fields'][0]['display_type'] = 'enum'
        assert not await database.fetchval('SELECT dataset_snapshots_equal($1, $2)', old, current)

    registry_database(scenario)


def test_concurrent_patch_preserves_both_changes(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{}]))['ids'][0]
        await asyncio.gather(
            *(
                datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={f'key_{index}': index}), USER)
                for index in range(4)
            ),
        )
        assert await database.fetchval('SELECT metadata FROM samples WHERE id = $1', sample_id) == {
            f'key_{index}': index for index in range(4)
        }
        assert len(await definitions(database)) == 4

    registry_database(scenario)


def test_quality_output_is_registered_without_refresh(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{}]))['ids'][0]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'test', '[1,0]')",
            sample_id,
        )
        await quality.representativeness_compute(
            1,
            quality.RepresentativenessComputeRequest(field='test', centroid='[1,0]'),
            USER,
        )
        fields = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert fields['representativeness']['type'] == 'number'

    registry_database(scenario)


def test_native_creation_cannot_replace_metadata(registry_database):
    async def scenario(database):
        await ingest([{'reviewed': ['yes']}])
        with pytest.raises(HTTPException) as error:
            await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
        assert error.value.status_code == 409
        assert (await definitions(database))['reviewed']['origin'] == 'metadata'

    registry_database(scenario)


def test_restore_restores_declared_fields_after_clear_and_rename(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'day': '2026-01-01', 'label': 'car'}]))['ids'][0]
        version = await versions.create_version(1, versions.CreateVersionRequest(name='before'))
        await datasets.clear_field(1, 'day', USER)
        await datasets.rename_field(1, 'label', datasets.FieldRenameRequest(new_name='renamed'), USER)
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'temporary': 1}), USER)
        await versions._restore_version(1, version['id'], restore_deleted=False, user=USER, exact_membership=False)
        fields = await definitions(database)
        assert set(fields) == {'day', 'label'}
        assert fields['day']['field_type'] == 'datetime'
        async with database.pool.acquire() as conn:
            assert (await versions._version_status(conn, 1))['current_version']['id'] == version['id']

    registry_database(scenario)


@pytest.mark.parametrize('has_dataset_snapshot', [False, True])
def test_legacy_restore_discovers_unsnapshotted_definitions(registry_database, has_dataset_snapshot):
    async def scenario(database):
        await ingest([{'day': '2026-01-01', 'labels': ['car']}])
        version = await versions.create_version(1, versions.CreateVersionRequest(name='legacy'))
        await database.execute(
            """UPDATE dataset_versions SET dataset_snapshot = CASE WHEN $2 THEN
                   (dataset_snapshot - 'field_registry_version') || '{"fields":[]}'::jsonb ELSE NULL END
               WHERE id = $1""",
            version['id'],
            has_dataset_snapshot,
        )
        await datasets.clear_field(1, 'day', USER)
        await datasets.clear_field(1, 'labels', USER)
        await versions._restore_version(1, version['id'], restore_deleted=False, user=USER, exact_membership=False)
        fields = await definitions(database)
        assert fields['day']['field_type'] == 'datetime'
        assert fields['labels']['field_type'] == 'list'
        assert fields['labels']['origin'] == 'metadata'

    registry_database(scenario)


def test_patch_registry_lock_does_not_deadlock_ingest(registry_database, monkeypatch):
    from backend.field_registry import register_fields

    async def scenario(database):
        sample_id = (await ingest([{}]))['ids'][0]
        registered = asyncio.Event()
        release = asyncio.Event()
        original = register_fields

        async def pause_after_registration(*args):
            result = await original(*args)
            registered.set()
            await release.wait()
            return result

        monkeypatch.setattr(datasets, 'register_fields', pause_after_registration)
        edit_task = asyncio.create_task(
            datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'shared_new_field': 1}), USER),
        )
        await asyncio.wait_for(registered.wait(), 2)
        ingest_task = asyncio.create_task(ingest([{'shared_new_field': 2}], prefix='concurrent'))
        await asyncio.sleep(0.1)
        release.set()
        await asyncio.wait_for(asyncio.gather(edit_task, ingest_task), 2)
        assert await database.fetchval('SELECT count(*) FROM samples') == 2

    registry_database(scenario)


def test_subset_statistics_use_declared_types(registry_database):
    async def scenario(database):
        sample_ids = (
            await ingest(
                [
                    {'score': 1, 'kind': 'a', 'labels': ['car'], 'day': '2026-01-01'},
                    {'score': 3, 'kind': 'b', 'labels': ['person'], 'day': '2026-01-02'},
                ],
            )
        )['ids']
        stats = await datasets.get_sample_stats(1, str(sample_ids[0]))
        assert stats['score'] == {'type': 'number', 'min': 1.0, 'max': 1.0}
        assert stats['labels']['type'] == 'list' and stats['labels']['values'] == ['car']
        assert stats['kind']['type'] == 'string' and stats['kind']['values'] == ['a']
        # The earliest instant, written as the statistics write datetime bounds.
        assert stats['day']['type'] == 'datetime' and stats['day']['min'] == '2026-01-01T00:00:00.000000Z'

    registry_database(scenario)
