"""Bulk metadata writes keep uniform payloads constant-sized and preserve atomic registry semantics."""

import os

import asyncpg
import pytest

from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_field_registry_postgres import USER, definitions, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


def test_uniform_bulk_binds_one_patch_and_returns_only_count(registry_database, monkeypatch):
    async def scenario(database):
        sample_ids = (await ingest([{'old': 1, 'label': 'before'}, {'old': 2}]))['ids']
        other_id = (await ingest([{'old': 'unchanged'}], 2))['ids'][0]
        calls = []

        def record_updates(method):
            original = getattr(asyncpg.Connection, method)

            async def execute(connection, query, *args, **kwargs):
                if query.lstrip().startswith('UPDATE samples'):
                    calls.append((method, query, args))
                return await original(connection, query, *args, **kwargs)

            monkeypatch.setattr(asyncpg.Connection, method, execute)

        record_updates('execute')
        record_updates('fetch')
        metadata = {'label': 'after', 'score': 3, 'removed': 'ignored', 'payload': 'x' * 512}
        result = await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(
                sample_ids=[*sample_ids, sample_ids[0], other_id, *range(-10000, 0)],
                metadata=metadata,
                remove_metadata_keys=['old', 'removed'],
            ),
            USER,
        )
        assert len(calls) == 1
        method, query, arguments = calls[0]
        assert method == 'execute'
        assert 'RETURNING' not in query.upper()
        assert sum(argument == metadata for argument in arguments) == 1
        assert not any(
            isinstance(argument, list) and argument and isinstance(argument[0], str) and len(argument) > 2
            for argument in arguments
        )
        assert result['updated_count'] == 2
        fields = await definitions(database)
        assert fields['score']['field_type'] == 'number'
        assert fields['label']['field_type'] == 'string'
        assert 'old' not in fields and 'removed' not in fields
        assert await database.fetchval('SELECT metadata FROM samples WHERE id = $1', other_id) == {'old': 'unchanged'}
        assert not await database.fetchval(
            "SELECT values_complete FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'label'",
        )
        assert await database.fetchval('SELECT metadata FROM samples WHERE id = $1', sample_ids[0]) == {
            key: value for key, value in metadata.items() if key != 'removed'
        }

    registry_database(scenario)


def test_uniform_bulk_registration_failure_rolls_back_samples(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'retained': 1}]))['ids'][0]
        await database.execute(
            """CREATE FUNCTION reject_bulk_field() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN RAISE EXCEPTION 'registration rejected'; END $$;
               CREATE TRIGGER reject_bulk_field BEFORE INSERT ON dataset_fields
               FOR EACH ROW WHEN (NEW.field_name = 'new_field') EXECUTE FUNCTION reject_bulk_field()""",
        )
        with pytest.raises(asyncpg.RaiseError, match='registration rejected'):
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(sample_ids=[sample_id], metadata={'new_field': 2}),
                USER,
            )
        assert await database.fetchval('SELECT metadata FROM samples WHERE id = $1', sample_id) == {'retained': 1}
        assert set(await definitions(database)) == {'retained'}

    registry_database(scenario)


def test_bulk_modes_only_register_written_rows_and_last_patch_wins(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{}]))['ids'][0]
        other_id = (await ingest([{}], 2))['ids'][0]
        result = await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=[other_id, -1], metadata={'unwritten': 1}),
            USER,
        )
        assert result['updated_count'] == 0 and not await definitions(database)
        result = await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(
                updates=[
                    datasets.BulkSetUpdate(sample_id=sample_id, metadata={'discarded': 1}),
                    datasets.BulkSetUpdate(sample_id=sample_id, metadata={'retained': 'last'}),
                    datasets.BulkSetUpdate(sample_id=other_id, metadata={'foreign': 1}),
                    datasets.BulkSetUpdate(sample_id=-1, metadata={'missing': 1}),
                ],
            ),
            USER,
        )
        assert result['updated_count'] == 1
        assert set(await definitions(database)) == {'retained'}
        assert await database.fetchval('SELECT metadata FROM samples WHERE id = $1', sample_id) == {'retained': 'last'}

    registry_database(scenario)
