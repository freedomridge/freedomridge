"""Explicit declaration changes preserve data and respect current index placement."""

import os

import asyncpg
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.routers import datasets, versions
from backend.tests import job_helpers
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, definitions, ingest, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


def test_explicit_type_correction_changes_semantics_not_samples(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'score': 'placeholder'}]))['ids'][0]
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'score': 3}), USER)
        assert (await definitions(database))['score']['field_type'] == 'string'
        version = await versions.create_version(1, versions.CreateVersionRequest(name='before-correction'))
        before = await database.fetchval('SELECT metadata FROM samples WHERE id = $1', sample_id)
        await database.execute('UPDATE datasets SET stats_full_refreshed_at = now() WHERE id = 1')
        result = await datasets.set_field_type(1, 'score', datasets.SetFieldTypeRequest(field_type='number'), USER)
        assert result == {'status': 'ok', 'field_name': 'score', 'field_type': 'number'}
        assert await database.fetchval('SELECT metadata FROM samples WHERE id = $1', sample_id) == before
        assert (await definitions(database))['score']['field_type'] == 'number'
        assert await database.fetchval('SELECT stats_full_refreshed_at FROM datasets WHERE id = 1') is None
        assert not await database.fetchval(
            "SELECT EXISTS(SELECT 1 FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'score')",
        )
        assert (await versions.version_status(1))['has_changes']
        await run_refresh(database)
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert schema['score']['type'] == 'number'
        assert schema['score']['min'] == schema['score']['max'] == 3
        await versions.revert_to_version(1, version['id'], user=USER)
        assert (await definitions(database))['score']['field_type'] == 'string'

    registry_database(scenario)


@pytest.mark.parametrize('field_type', ['float', 'enum', 'array', 'bogus'])
def test_type_change_rejects_noncanonical_vocabulary(field_type):
    with pytest.raises(ValidationError):
        datasets.SetFieldTypeRequest(field_type=field_type)


def test_type_change_protects_native_system_missing_and_indexed_fields(registry_database):
    async def scenario(database):
        await ingest([{'score': 3}])
        await database.execute("""INSERT INTO dataset_fields
            (dataset_id, field_name, field_type, origin) VALUES
            (1, 'native', 'list', 'native_array'), (1, 'builtin', 'string', 'system')""")
        for name, expected in [('native', 400), ('builtin', 400), ('absent', 404)]:
            with pytest.raises(HTTPException) as error:
                await datasets.set_field_type(1, name, datasets.SetFieldTypeRequest(field_type='string'), USER)
            assert error.value.status_code == expected
        await datasets.promote_field(1, 'score', USER)
        with pytest.raises(HTTPException) as error:
            await datasets.set_field_type(1, 'score', datasets.SetFieldTypeRequest(field_type='string'), USER)
        assert error.value.status_code == 409
        assert (await definitions(database))['score']['is_promoted'] is True
        assert (await definitions(database))['score']['field_type'] == 'number'
        await datasets.set_field_type(1, 'score', datasets.SetFieldTypeRequest(field_type='number'), USER)

    registry_database(job_helpers.with_runner(scenario))


def test_patch_added_field_requests_measurement_even_below_the_ingest_watermark(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'existing': 1}]))['ids'][0]
        await run_refresh(database)
        assert await database.fetchval('SELECT initialized FROM dataset_stats_state WHERE dataset_id = 1')
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'added_by_patch': 42}), USER)
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert schema['added_by_patch']['stats_pending'] is True
        assert await database.fetchval('SELECT EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id = 1)')
        await run_refresh(database)
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert schema['added_by_patch']['stats_pending'] is False
        assert schema['added_by_patch']['min'] == schema['added_by_patch']['max'] == 42

    registry_database(scenario)


def test_patch_field_remains_visible_if_runner_finishes_before_page_load(registry_database):
    async def scenario(database):
        result = await datasets.add_samples(
            1,
            {
                'samples': [
                    {
                        'filepath': f'registry/{index}.jpg',
                        'tags': [f'shared-prefix-{index:03}'],
                        'metadata': {'serial': f'serial-{index}'},
                    }
                    for index in range(250)
                ],
            },
            user=USER,
        )
        await datasets.update_sample(
            1,
            result['ids'][0],
            datasets.SampleUpdate(metadata={'new_measurement': 123}),
            USER,
        )
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert schema['new_measurement']['stats_pending'] is True
        # The page loads /field-stats independently of the preceding /schema request.
        for measured in (False, True):
            if measured:
                await run_refresh(database)
            fields = {field['name']: field for field in await datasets._get_dataset_field_stats_impl(1, None)}
            field = fields['new_measurement']
            assert field['type'] == field['declared_type'] == 'number'
            assert field['stats_pending'] is not measured
            if measured:
                assert field['min'] == field['max'] == 123
                assert len(fields['tags']['values']) <= 200
                assert fields['tags']['values_complete'] is False
            matches = await datasets._autocomplete_impl(1, 'tags', 'shared-prefix-24', 100)
            assert {'value': 'shared-prefix-249', 'count': 1} in matches['values']

    registry_database(scenario)


def test_type_correction_rolls_back_if_refresh_scheduling_fails(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'score': 'placeholder'}])
        before = await definitions(database)
        execute = asyncpg.Connection.execute

        async def fail_refresh(connection, query, *args, **kwargs):
            if query.startswith('UPDATE datasets SET stats_need_refresh = TRUE, stats_full_refreshed_at = NULL'):
                raise RuntimeError('injected scheduling failure')
            return await execute(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'execute', fail_refresh)
        with pytest.raises(RuntimeError, match='injected scheduling failure'):
            await datasets.set_field_type(1, 'score', datasets.SetFieldTypeRequest(field_type='number'), USER)
        assert await definitions(database) == before
        assert await database.fetchval(
            "SELECT unique_values FROM dataset_field_stats WHERE dataset_id = 1 AND field_name = 'score'",
        ) == ['placeholder']

    registry_database(scenario)
