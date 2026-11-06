"""POST /histogram and /histogram/preview: the GET answers, with the filter in the body.

A saved selection of a few thousand sample ids makes a filter too large for a URL (the server answers 400
past its 64 KB request line), so the app and the SDK send histogram requests as POST. The SDK suite checks
that end to end through a running server; these check the two routes answer exactly as GET does.
"""

import os

import pytest

from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_histogram_postgres import histogram, seed
from backend.tests.test_histogram_text_and_preview import preview, seed_broad

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


def test_post_histogram_answers_as_get_for_a_filter_too_large_for_a_url(registry_database):
    async def scenario(database):
        await seed(database, [1, 2, 3, 4, 10])
        real = [row['id'] for row in await database.fetch('SELECT id FROM samples ORDER BY id LIMIT 3')]
        # 20,000 ids, three of them samples: about 200 KB of JSON.
        selection = {'field': 'id', 'operator': 'in', 'value': real + list(range(10_000_000, 10_019_997))}
        big = {'logic': 'AND', 'filters': [selection]}
        for bins, as_number in ((None, False), (2, False), (None, True)):
            request = datasets.HistogramRequest(field='value', bins=bins, filters=big, as_number=as_number)
            expected = await histogram('value', bins, big, as_number=as_number)
            assert await datasets.get_histogram_post(1, request) == expected
        filtered = await datasets.get_histogram_post(1, datasets.HistogramRequest(field='value', filters=big))
        assert filtered['total'] == 3
        unfiltered = datasets.HistogramRequest(field='value', bins=2)
        assert await datasets.get_histogram_post(1, unfiltered) == await histogram('value', 2)

    registry_database(scenario)


def test_post_preview_answers_as_get(registry_database):
    async def scenario(database):
        await seed_broad(database)
        filters = {'logic': 'AND', 'filters': [{'field': 'speed', 'operator': '>=', 'value': 5}]}
        request = datasets.HistogramRequest(field='speed', filters=filters)
        result = await datasets.get_histogram_preview_post(1, request)
        assert result['preview']['status'] == 'ok'
        assert result == await preview('speed', filters=filters)

    registry_database(scenario)
