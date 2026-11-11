"""The map across the antimeridian (docs/MAP_REQUIREMENTS.md #0). mapbox-gl reports a view's longitudes
unwrapped: a view across the antimeridian is 170 to 190, and the same view on a repeated world copy 530 to 550.
Such a view used to be clamped to 180, which left out every sample east of the antimeridian, or emptied the map."""

import asyncio
import json
import os

import asyncpg
import pytest
from fastapi import HTTPException

from backend.routers import datasets
from backend.tests.test_field_registry_postgres import bulk_setup, registry_database
from scripts.backfill_sample_lonlat import INDEX_SQL

__all__ = ['registry_database']
postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

# At zoom 6 a cell is 1.40625 degrees, and the cell on the antimeridian holds 179.3 to 180.7 in a window across
# it. 60 samples on each side of the antimeridian make one 120-sample bubble there, 60 alone stay points.
ZOOM = 6
WEST_OF_IT, EAST_OF_IT = (179.5, 0.0), (-179.5, 0.0)
ACROSS = [175.0, -5.0, 185.0, 5.0]


async def _add(database_, name, location, count):
    await database_.execute(
        "INSERT INTO samples (dataset_id, filepath, media_type, location) "
        "SELECT 1, $1 || '/' || i, 'image', ST_SetSRID(ST_MakePoint($2, $3), 4326) FROM generate_series(1, $4) i",
        name,
        *location,
        count,
    )


def _split(result):
    bubbles = [item for item in result['items'] if item.get('id') is None]
    points = sorted((round(item['lon'], 6), round(item['lat'], 6)) for item in result['items'] if item.get('id'))
    return bubbles, points


async def _both_sides(database_):
    await _add(database_, 'west', WEST_OF_IT, 60)
    await _add(database_, 'east', EAST_OF_IT, 60)
    await _add(database_, 'dense', (10.0, 10.0), 150)
    await _add(database_, 'sparse', (20.0, 10.0), 5)


@postgres
def test_views_that_do_not_cross_the_antimeridian_answer_as_before(registry_database):
    async def scenario(database_):
        await _both_sides(database_)
        dense = {'lon': 10.0, 'lat': 10.0, 'count': 150, 'radius': 52, 'w': 10.0, 's': 10.0, 'e': 10.0, 'n': 10.0}

        bubbles, points = _split(await datasets._get_dataset_map_points_impl(1, None, zoom=ZOOM, bbox=[0, 0, 30, 20]))
        assert (bubbles, points) == ([dense], [(20.0, 10.0)] * 5)
        # Up to the antimeridian: the samples west of it only.
        result = await datasets._get_dataset_map_points_impl(1, None, zoom=ZOOM, bbox=[170, -5, 180, 5])
        assert _split(result) == ([], [WEST_OF_IT] * 60)
        result = await datasets._get_dataset_map_points_impl(1, None, zoom=13, bbox=[9.99, 9.99, 10.01, 10.01])
        assert (result['mode'], len(result['items'])) == ('points', 150)
        # The whole world keeps its one frame, -180 to 180: the antimeridian is its edge, so each side of it is a
        # cell of its own there, as it was.
        for bbox in ([-180.0, -85.0, 180.0, 85.0], None):
            bubbles, points = _split(await datasets._get_dataset_map_points_impl(1, None, zoom=ZOOM, bbox=bbox))
            assert (bubbles, points) == ([dense], sorted([EAST_OF_IT] * 60 + [(20.0, 10.0)] * 5 + [WEST_OF_IT] * 60))

    registry_database(scenario)


@pytest.mark.parametrize(
    ('west', 'east', 'window'),
    [
        (-10.0, 10.0, (-10.0, 10.0)),
        (170.0, 190.0, (170.0, 190.0)),  # across the antimeridian
        (530.0, 550.0, (170.0, 190.0)),  # the same view on a repeated world copy
        (-190.0, -170.0, (170.0, 190.0)),
        (170.0, -170.0, (170.0, 190.0)),  # RFC 7946: an east below the west crosses
        (200.0, 220.0, (-160.0, -140.0)),  # all east of it, on the next world copy
        (180.0, 200.0, (-180.0, -160.0)),
        (-180.0, 180.0, (-180.0, 180.0)),
        (-400.0, 400.0, (-180.0, 180.0)),  # wider than the world
        (10.0, 370.0, (-180.0, 180.0)),
    ],
)
def test_a_viewport_is_one_window_of_longitudes(west, east, window):
    assert datasets._map_lon_window(west, east) == window


@postgres
def test_a_view_across_the_antimeridian_holds_both_sides(registry_database):
    async def scenario(database_):
        await _both_sides(database_)
        # One bubble on the antimeridian, for the 60 samples on each side of it (60 alone are points): its position
        # and extent are in the view's window, the samples east of the antimeridian 360 degrees on.
        across = {'lon': 180.0, 'lat': 0.0, 'count': 120, 'radius': 52, 'w': 179.5, 's': 0.0, 'e': 180.5, 'n': 0.0}
        result = await datasets._get_dataset_map_points_impl(1, None, zoom=ZOOM, bbox=ACROSS)
        assert result['mode'] == 'hybrid'
        bubbles, points = _split(result)
        assert (bubbles, points) == ([pytest.approx(across)], [])
        # The same view on a repeated world copy, as mapbox-gl reports it after a pan across the Pacific, and in
        # RFC 7946's form.
        for bbox in ([535.0, -5.0, 545.0, 5.0], [-185.0, -5.0, -175.0, 5.0], [175.0, -5.0, -175.0, 5.0]):
            assert await datasets._get_dataset_map_points_impl(1, None, zoom=ZOOM, bbox=bbox) == result

    registry_database(scenario)


@postgres
def test_a_bubble_across_the_antimeridian_takes_in_the_points_under_it(registry_database):
    async def scenario(database_):
        # 100 samples just east of the antimeridian and 50 just west make a bubble at 180.38 in the window. Three
        # samples at -179.2 (180.8 in the window, the next cell) are 38 px from it at zoom 6, under its 52 px.
        await _add(database_, 'east', (-179.4, 0.0), 100)
        await _add(database_, 'west', (179.95, 0.0), 50)
        await _add(database_, 'under it', (-179.2, 0.0), 3)
        bubbles, points = _split(await datasets._get_dataset_map_points_impl(1, None, zoom=ZOOM, bbox=ACROSS))
        assert points == []
        assert [(bubble['count'], bubble['w'], bubble['e']) for bubble in bubbles] == [
            (153, pytest.approx(179.95), pytest.approx(180.8)),
        ]
        assert bubbles[0]['lon'] == pytest.approx((100 * 180.6 + 50 * 179.95) / 150)

    registry_database(scenario)


@postgres
def test_every_point_across_the_antimeridian_at_a_points_zoom(registry_database):
    async def scenario(database_):
        await _add(database_, 'west', (179.9999, 0.0), 1)
        await _add(database_, 'east', (-179.9999, 0.0), 1)
        await _add(database_, 'elsewhere', (10.0, 0.0), 1)
        # Zoom 18 on the west sample, as selecting it in the grid flies the map there (MAP_REQUIREMENTS #12), and
        # the same on the next world copy.
        for bbox in ([179.9992, -0.0004, 180.0006, 0.0004], [539.9992, -0.0004, 540.0006, 0.0004]):
            result = await datasets._get_dataset_map_points_impl(1, None, zoom=18, bbox=bbox)
            assert result['mode'] == 'points'
            assert sorted(round(item['lon'], 6) for item in result['items']) == [179.9999, 180.0001]
        # Points forced at a clustered zoom, and the padding around a view that ends at the antimeridian.
        for zoom, bbox, points in ((ZOOM, ACROSS, True), (18, [179.9993, -0.0004, 179.99995, 0.0004], False)):
            result = await datasets._get_dataset_map_points_impl(1, None, zoom=zoom, bbox=bbox, points=points)
            assert sorted(round(item['lon'], 6) for item in result['items']) == [179.9999, 180.0001]

    registry_database(scenario)


@postgres
def test_a_view_on_another_world_copy_is_not_empty(registry_database):
    async def scenario(database_):
        await _add(database_, 'pacific', (-165.0, 0.0), 3)
        # West of the antimeridian's next copy and wholly east of it: the samples there, at their own longitude.
        for zoom in (5, 13):
            result = await datasets._get_dataset_map_points_impl(1, None, zoom=zoom, bbox=[190.0, -1.0, 200.0, 1.0])
            assert _split(result) == ([], [(-165.0, 0.0)] * 3)
        # A view wider than the world is the whole world.
        result = await datasets._get_dataset_map_points_impl(1, None, zoom=0, bbox=[-400.0, -85.0, 400.0, 85.0])
        assert _split(result) == ([], [(-165.0, 0.0)] * 3)

    registry_database(scenario)


def test_a_bbox_that_is_not_finite_numbers_is_refused(monkeypatch):
    class Database:
        async def fetchrow(self, *_args):
            return {'id': 1}

    monkeypatch.setattr(datasets, 'db', Database())
    for bbox in ([float('nan'), 0.0, 10.0, 10.0], [0.0, 0.0, float('inf'), 10.0]):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(datasets._get_dataset_map_points_impl(1, None, zoom=ZOOM, bbox=bbox))
        assert (refused.value.status_code, refused.value.detail) == (
            400,
            'bbox must be [west, south, east, north] numbers',
        )


def _scans(plan):
    nodes = [plan]
    for node in nodes:
        nodes.extend(node.get('Plans', []))
    return nodes


@postgres
def test_a_view_across_the_antimeridian_reads_two_ranges_of_the_covering_index(registry_database, monkeypatch):
    """#11: the clustered view reads the covering index alone, an index-only scan. Across the antimeridian that is two
    range scans, one for each side: `lon >= w OR lon <= e - 360` is no range, so it read every index entry of the
    dataset, or went to the heap through a bitmap. The points view reads media_type, which the index doesn't hold, so
    its two scans are index scans, as for a view that doesn't cross."""

    async def scenario(database_):
        async with bulk_setup(database_) as connection:
            await connection.execute(
                "INSERT INTO samples (dataset_id, filepath, media_type, location) "
                "SELECT 1, 'world/' || i, 'image', "
                "ST_SetSRID(ST_MakePoint((i * 0.0137) % 360 - 180, (i * 0.0071) % 170 - 85), 4326) "
                "FROM generate_series(1, 200000) i",
            )
        await database_.execute(INDEX_SQL)  # the index scripts/backfill_sample_lonlat.py builds; schema.sql has none
        await database_.execute('VACUUM ANALYZE samples')
        queries = []
        fetch = asyncpg.Connection.fetch

        async def recorded(self, query, *args, **kwargs):
            if 'FROM samples' in query:
                queries.append((query, args))
            return await fetch(self, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetch', recorded)
        for zoom, bbox, scan in ((6, [170.0, -10.0, 190.0, 10.0], 'Index Only Scan'), (13, ACROSS, 'Index Scan')):
            queries.clear()
            await datasets._get_dataset_map_points_impl(1, None, zoom=zoom, bbox=bbox)
            [(query, args)] = queries
            plan = json.loads(await database_.fetchval('EXPLAIN (FORMAT JSON) ' + query, *args))[0]['Plan']
            nodes = _scans(plan)
            assert not {'Seq Scan', 'Bitmap Heap Scan', 'Bitmap Index Scan'} & {node['Node Type'] for node in nodes}
            reads = [node for node in nodes if node.get('Relation Name') == 'samples']
            index = 'idx_samples_lonlat_cover'
            assert [(node['Node Type'], node['Index Name']) for node in reads] == [(scan, index)] * 2
            for node in reads:  # each a range of longitudes, not a filter over the dataset's entries
                assert 'lon >=' in node['Index Cond'] and 'lon <=' in node['Index Cond'] and 'Filter' not in node

    registry_database(scenario)
