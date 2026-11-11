"""A version's rows are read with the fields the version stored, so its answers don't change with the live dataset."""

import os

import pytest

from backend.routers import datasets, versions
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
BOX = [{'label': 'car', 'bbox': [0.1, 0.1, 0.2, 0.2], 'confidence': 0.9}]


def test_label_paths_on_a_version_survive_live_changes(registry_database):
    # animals.label on a version matched nothing once animals was cleared in the live dataset, and
    # vehicles.label once the next statistics refresh dropped vehicles from the discovered box fields.
    async def scenario(database):
        await ingest([{'vehicles': BOX, 'animals': [{'label': 'cat'}]}, {'vehicles': [], 'animals': []}])
        await datasets._get_dataset_field_stats_impl(1, None)  # discovery, as the sidebar does
        version = (await versions.create_version(1, versions.CreateVersionRequest(name='v1')))['id']

        async def answers():
            matched = []
            for field, value in (('vehicles.label', 'car'), ('animals.label', 'cat')):
                request = versions.VersionSamplesQueryRequest(filters={'field': field, 'operator': '=', 'value': value})
                matched.append(len((await versions.query_version_samples(1, version, request))['items']))
            aggregate = await versions.aggregate_version(
                1,
                version,
                versions.VersionAggregateRequest(
                    aggregations=[datasets.AggregationSpec(field='animals.label', op='count_values')],
                ),
            )
            return matched, aggregate['results'][0]['value']

        expected = ([1, 1], [{'value': 'cat', 'count': 1}])
        assert await answers() == expected
        await datasets.clear_field(1, 'vehicles', USER)
        await datasets.clear_field(1, 'animals', USER)
        assert await answers() == expected
        await database.execute('UPDATE datasets SET stats_need_refresh = TRUE, stats_full_refreshed_at = NULL')
        await run_refresh(database, 1, force=True)
        assert await answers() == expected

    registry_database(scenario)


def test_a_field_the_version_did_not_have_as_a_list_is_not_read_as_one(registry_database):
    # The other way round: a field that is a list now but was text in the version reads as the version's text.
    async def scenario(database):
        await ingest([{'notes.label': 'kept', 'notes': 'plain'}])
        version = (await versions.create_version(1, versions.CreateVersionRequest(name='v1')))['id']
        await database.execute('DELETE FROM samples WHERE dataset_id = 1')
        await database.execute("DELETE FROM dataset_fields WHERE dataset_id = 1 AND field_name = 'notes.label'")
        await database.execute(
            "UPDATE dataset_fields SET field_type = 'list' WHERE dataset_id = 1 AND field_name = 'notes'",
        )
        node = {'field': 'notes.label', 'operator': '=', 'value': 'kept'}
        request = versions.VersionSamplesQueryRequest(filters=node)
        assert len((await versions.query_version_samples(1, version, request))['items']) == 1

    registry_database(scenario)
