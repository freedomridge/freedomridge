"""A lasso records the layout id of the map it was drawn on (`layout_id` on an `embedding_polygon` filter). The server
accepts it, keeps it wherever a filter is stored, and never filters or refuses by it: the polygon always selects the
map's current points, and the app compares the id with the map's to warn."""

import os
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request

from backend.models import ViewCreate
from backend.routers import datasets, embeddings, filter_query, views
from backend.tests import test_field_registry_postgres
from backend.tests.test_layout_coordinates import seed, upload

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')
USER = test_field_registry_postgres.USER
# upload() puts sample i at x = i, y = i / 3: this holds samples 1 to 4.
POLYGON = [[0.5, -1], [4.5, -1], [4.5, 3], [0.5, 3]]


def lasso(**recorded):
    return {'field': 'clip', 'operator': 'embedding_polygon', 'value': POLYGON, **recorded}


async def count(db, node):
    args: list = [1]
    sql, _ = datasets.parse_filter_node(node, args, 2, None, None, 1)
    return await db.fetchval(f'SELECT count(*) FROM samples WHERE dataset_id = $1 AND {sql}', *args)


def test_a_recorded_layout_id_changes_nothing_a_lasso_selects_and_a_malformed_one_is_a_400(registry_database):
    async def scenario(db):
        await seed(db)
        await upload(range(1, 9))
        for recorded in ({}, {'layout_id': None}, {'layout_id': 'f81d4fae-7dec-11d0-a765-00a0c91e6bf6'}):
            assert await count(db, lasso(**recorded)) == 4
            assert await count(db, {**lasso(**recorded), 'operator': 'embedding_polygon_exclude'}) == 4
        for malformed in (5, {'id': 'x'}, ['x'], 'x' * 65):
            with pytest.raises(HTTPException) as refused:
                await count(db, lasso(layout_id=malformed))
            assert refused.value.status_code == 400 and 'layout_id' in refused.value.detail

    registry_database(scenario)


def test_a_lasso_drawn_before_the_map_was_laid_out_again_keeps_filtering_on_its_current_points(
    registry_database,
    monkeypatch,
):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db)
        await upload(range(1, 9))
        drawn_on = await db.fetchval("SELECT layout_id::text FROM embedding_layouts WHERE field_name = 'clip'")
        drawn = {'logic': 'AND', 'filters': [lasso(layout_id=drawn_on)]}
        page = await datasets.list_samples_post(1, datasets.SamplesRequest(limit=10, filters=drawn))
        assert sorted(item['id'] for item in page['items']) == [1, 2, 3, 4]
        await upload([1, 2], shift=100)  # a new layout: two of its points leave the polygon
        assert await db.fetchval("SELECT layout_id::text FROM embedding_layouts WHERE field_name = 'clip'") != drawn_on
        page = await datasets.list_samples_post(1, datasets.SamplesRequest(limit=10, filters=drawn))
        assert sorted(item['id'] for item in page['items']) == [3, 4]

    registry_database(scenario)


def test_a_saved_view_keeps_its_lassos_layout_ids(registry_database, monkeypatch):
    async def scenario(db):
        monkeypatch.setattr(views, 'db', db)
        monkeypatch.setattr(views, 'get_optional_user', AsyncMock(return_value=USER))
        monkeypatch.setattr(views, 'check_dataset_access', AsyncMock())
        stages = [
            {
                'type': 'filter',
                'description': 'Embeddings Region',
                'filter': lasso(layout_id='a-layout', model_id=None),
            },
            {'type': 'filter', 'description': 'Embeddings Region', 'filter': lasso()},  # one saved before 2.11
        ]
        created = await views.create_view(1, ViewCreate(name='lassos', content={'stages': stages}), user=USER)
        opened = await views.get_view(created['slug'], Request({'type': 'http', 'headers': []}))
        assert opened['content']['stages'] == stages

    registry_database(scenario)


def test_the_query_language_endpoints_take_a_lasso_with_its_layout_id(registry_database, monkeypatch):
    async def scenario(db):
        monkeypatch.setattr(filter_query, 'db', db)
        recorded = lasso(layout_id='a-layout')
        formatted = await filter_query.format_filter_query(1, filter_query.FilterQueryFormat(filters=[recorded]))
        assert formatted['parts'] == [None]  # the language has no lasso: it stays a chip of its own
        parsed = await filter_query.parse_filter_query(
            1,
            filter_query.FilterQueryParse(query='id > 0', current=[recorded]),
        )
        assert parsed['reuse'] == [None]

    registry_database(scenario)
