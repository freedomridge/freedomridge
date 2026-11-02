"""Direct-SQL maintenance tools obey the same field registry write contract as the API."""

import argparse
import os

import asyncpg
import pytest

from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import definitions, ingest
from scripts import copy_objects

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def copy_from_other_dataset(database, monkeypatch):
    namespace = await database.fetchval('SELECT current_schema()')
    original_connect = asyncpg.connect

    async def isolated_connection(*args, **kwargs):
        return await original_connect(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            server_settings={'search_path': f'{namespace},public'},
        )

    with monkeypatch.context() as context:
        context.setattr(asyncpg, 'connect', isolated_connection)
        await copy_objects.run(
            argparse.Namespace(src_dataset='other', dest_dataset='training', field='clip_obj', no_viz=True),
        )


def test_copy_registers_actual_rows_and_invalidates_old_preview(registry_database, monkeypatch):
    async def scenario(database):
        parent_id = (await ingest([{'category': 'parent'}]))['ids'][0]
        await ingest([{'retained': True}], prefix='duplicate')
        await ingest([{'parent_id': parent_id, 'category': 'object', 'object_label': 'car'}], 2, prefix='object')
        await ingest([{'parent_id': parent_id, 'skipped_field': 'unwritten'}], 2, prefix='duplicate')
        await database.execute("UPDATE samples SET tags = ARRAY['car-tag'] WHERE dataset_id = 2")
        await run_refresh(database)
        await database.execute(
            """INSERT INTO dataset_field_stats
                   (dataset_id, field_name, field_type, unique_values, values_complete)
               VALUES (1, 'tags', 'list', ARRAY[]::text[], TRUE),
                      (1, 'media_type', 'string', ARRAY['image'], TRUE)
               ON CONFLICT (dataset_id,field_name) DO UPDATE SET
                   unique_values=EXCLUDED.unique_values,values_complete=EXCLUDED.values_complete""",
        )
        await copy_from_other_dataset(database, monkeypatch)
        fields = await definitions(database)
        assert fields['object_label']['field_type'] == 'string'
        assert fields['parent_id']['field_type'] == 'number'
        assert 'skipped_field' not in fields
        assert not await database.fetchval(
            "SELECT values_complete FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'category'",
        )
        assert not await database.fetchval(
            "SELECT bool_or(values_complete) FROM dataset_field_stats "
            "WHERE dataset_id = 1 AND field_name IN ('tags', 'media_type')",
        )
        assert await database.fetchval('SELECT stats_full_refreshed_at FROM datasets WHERE id = 1') is None
        assert (
            await database.fetchval("SELECT count(*) FROM samples WHERE dataset_id = 1 AND media_type = 'object'") == 1
        )
        await run_refresh(database)
        assert (
            await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'object_label'",
            )
            == 1
        )
        await copy_from_other_dataset(database, monkeypatch)
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 1') == 3
        assert not await database.fetchval('SELECT stats_need_refresh FROM datasets WHERE id = 1')
        assert await database.fetchval('SELECT stats_full_refreshed_at FROM datasets WHERE id = 1') is not None

    registry_database(scenario)


def test_copy_registry_failure_rolls_back_batch(registry_database, monkeypatch):
    async def scenario(database):
        parent_id = (await ingest([{}]))['ids'][0]
        await ingest([{'parent_id': parent_id, 'copied_key': 'car'}], 2, prefix='object')
        await database.execute(
            """CREATE FUNCTION reject_copied_field() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN RAISE EXCEPTION 'registration rejected'; END $$;
               CREATE TRIGGER reject_copied_field BEFORE INSERT ON dataset_fields
               FOR EACH ROW WHEN (NEW.dataset_id = 1 AND NEW.field_name = 'copied_key')
               EXECUTE FUNCTION reject_copied_field()""",
        )
        with pytest.raises(asyncpg.RaiseError, match='registration rejected'):
            await copy_from_other_dataset(database, monkeypatch)
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 1') == 1
        assert not await definitions(database)

    registry_database(scenario)


def test_operator_name_lookup_uses_active_replacement(registry_database):
    from backend.routers import datasets
    from backend.tests.test_field_registry_postgres import USER

    async def scenario(database):
        await database.execute("SELECT setval('datasets_id_seq',2)")
        await datasets.delete_dataset(1, USER)
        replacement = await datasets.create_dataset(datasets.DatasetCreate(name='training'), USER)
        async with database.pool.acquire() as connection:
            assert await copy_objects._dataset_id(connection, 'training') == replacement['id']

    registry_database(scenario)
