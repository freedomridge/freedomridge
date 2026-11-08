"""Excluding a map region or an embeddings lasso removes what is inside it and keeps everything else, samples
with no location (or no point on the map) included."""

import os

import pytest

from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_map_refit import FIELD, seed

registry_database = test_field_registry_postgres.registry_database
needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


async def count(database, node):
    args: list = [1]
    sql, _ = datasets.parse_filter_node(node, args, 2, None, None, 1)
    return await database.fetchval(f'SELECT count(*) FROM samples WHERE dataset_id = $1 AND {sql}', *args)


@needs_postgres
def test_excluding_a_lasso_keeps_the_samples_off_the_map(registry_database):
    async def scenario(database):
        await seed(database, n=200)  # map points at x = -100 - (id % 40), y = id % 13
        await database.execute(
            "INSERT INTO samples(id,dataset_id,filepath,media_type) "
            "SELECT i,1,'off-map-' || i,'image' FROM generate_series(1001,1020) i",
        )
        box = [[-120.5, -1], [-99.5, -1], [-99.5, 13], [-120.5, 13]]  # id % 40 in 0..20
        inside = await count(database, {'field': FIELD, 'operator': 'embedding_polygon', 'value': box})
        outside = await count(database, {'field': FIELD, 'operator': 'embedding_polygon_exclude', 'value': box})
        assert inside == sum(1 for i in range(1, 201) if i % 40 <= 20)
        assert outside == 220 - inside  # the other map points, and the 20 samples with no point

    registry_database(scenario)


@needs_postgres
def test_excluding_a_map_region_keeps_the_samples_without_a_location(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        await database.execute(
            'UPDATE samples SET location = ST_SetSRID(ST_MakePoint(10 + (id % 3), 50), 4326) WHERE id <= 20',
        )
        box = [[9.5, 49], [10.5, 49], [10.5, 51], [9.5, 51]]  # holds the located samples at longitude 10
        inside = await count(database, {'field': 'location', 'operator': 'geo_polygon', 'value': box})
        outside = await count(database, {'field': 'location', 'operator': 'geo_polygon_exclude', 'value': box})
        assert inside == sum(1 for i in range(1, 21) if i % 3 == 0)
        assert outside == 30 - inside  # the other located samples, and the 10 with no location

    registry_database(scenario)


@needs_postgres
def test_a_lasso_exclude_with_too_few_points_excludes_nothing(registry_database):
    async def scenario(database):
        await seed(database, n=20)
        line = [[-120, 0], [-100, 0]]
        assert await count(database, {'field': FIELD, 'operator': 'embedding_polygon_exclude', 'value': line}) == 20
        assert await count(database, {'field': FIELD, 'operator': 'embedding_polygon', 'value': line}) == 0

    registry_database(scenario)


def test_cross_grain_subqueries_read_the_partner_row_columns():
    # A joined query (/search, /hashes) maps created_at to the outer `s`; inside the partner subquery it must be
    # the partner's own column, or each partner is compared with the outer row's date.
    since = {'logic': 'AND', 'filters': [{'field': 'created_at', 'operator': '>=', 'value': '2026-01-01T00:00:00'}]}
    for operator, alias in (('has_matching_object', 'o'), ('parent_matches', 's2')):
        args: list = []
        sql, _ = datasets.parse_filter_node(
            {'field': '_crossgrain', 'operator': operator, 'value': since},
            args,
            1,
            field_mapping=datasets.SAMPLES_JOIN_FIELD_MAPPING,
            numeric_indexed_fields=set(),
            dataset_id=1,
            array_columns=set(),
        )
        assert f'{alias}.created_at' in sql, sql
        assert 's.created_at' not in sql, sql
