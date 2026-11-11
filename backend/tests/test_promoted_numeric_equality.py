"""`score = "1"` on a promoted numeric field matches the same rows in the grid and in every other filtered request.

The grid passes the promoted fields to the filter compiler, which then compares a numeric string as a number and
matches a stored 1.0. Hash duplicates, sparse sampling, bulk jobs by filter, mirror tag forwarding and version
queries didn't pass them, so they compared the text '1' and missed it.
"""

import os

import pytest

from backend.routers import datasets, hashes, versions
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


async def grid_ids(node):
    rows = await datasets.list_samples_post(1, datasets.SamplesRequest(filters=node, limit=100))
    return sorted(row['id'] for row in rows['items'])


async def compiled_ids(database, node):
    """The rows the filter selects as compiled by the callers that pass no promoted fields: bulk jobs, mirror tag
    forwarding and sampling (bulk_jobs.py, mirror_writes.py, sampling.py) compile it with only the dataset context."""
    args: list = [1]
    sql, _ = datasets.parse_filter_node(
        node,
        args,
        2,
        field_mapping=datasets.SAMPLES_JOIN_FIELD_MAPPING,
        dataset_id=1,
        array_columns=await datasets.get_dataset_array_columns(1),
    )
    rows = await database.fetch(f'SELECT s.id FROM samples s WHERE s.dataset_id = $1 AND {sql} ORDER BY s.id', *args)
    return [row['id'] for row in rows]


def test_a_numeric_string_matches_alike_everywhere_on_a_promoted_field(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(hashes, 'db', database)
        ids = (await ingest([{'score': 1.0}, {'score': 2}, {'score': 1}, {}]))['ids']
        one, two, also_one, missing = ids
        await datasets.promote_field(1, 'score', USER)
        assert await datasets.get_promoted_numeric_fields(1) == {'score'}
        await database.execute(
            "INSERT INTO sample_hashes (sample_id, field_name, hash_hex) SELECT unnest($1::int[]), 'phash', 'ab'",
            ids,
        )
        version = (await versions.create_version(1, versions.CreateVersionRequest(name='v1')))['id']

        for node, expected in [
            ({'field': 'score', 'operator': '=', 'value': '1'}, [one, also_one]),
            ({'field': 'score', 'operator': 'in', 'value': ['1', '2']}, [one, two, also_one]),
            ({'field': 'score', 'operator': '!=', 'value': '1'}, [two, missing]),
            ({'field': 'score', 'operator': 'not in', 'value': ['2']}, [one, also_one, missing]),
        ]:
            assert await grid_ids(node) == expected, node
            assert await compiled_ids(database, node) == expected, node
            request = versions.VersionSamplesQueryRequest(filters=node)
            assert [row['id'] for row in (await versions.query_version_samples(1, version, request))['items']] == (
                expected
            ), node
            groups = await hashes.find_duplicates(1, hashes.DuplicateRequest(field_name='phash', filters=node))
            found = sorted(sample for group in groups['groups'] for sample in group['sample_ids'])
            assert found == (expected if len(expected) > 1 else []), node

    registry_database(job_helpers.with_runner(scenario))  # the promotion builds its index in a runner job


def test_an_unpromoted_field_still_matches_a_numeric_string_as_text(registry_database):
    # Unchanged: without the index's guarantee that every value is a number, '007' must not match 7.
    async def scenario(database):
        seven, text = (await ingest([{'code': 7}, {'code': '007'}]))['ids']
        node = {'field': 'code', 'operator': '=', 'value': '007'}
        assert await grid_ids(node) == [text]
        assert await compiled_ids(database, node) == [text]
        assert seven not in await compiled_ids(database, {'field': 'code', 'operator': 'in', 'value': ['007']})

    registry_database(scenario)
