"""The map's clustered view: a cluster bubble takes in the loose points it is drawn over
(docs/MAP_REQUIREMENTS.md #2), and the query is planned for every run (#11)."""

import os

import asyncpg
import pytest

from backend.database import Database, init_connection
from backend.routers import datasets
from backend.tests.test_field_registry_postgres import registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

# At zoom 6 a cell is 1.40625 degrees and a degree of longitude is 91 px. The 150 samples at
# DENSE fill one cell near its north-east corner, so its 52 px bubble reaches into the cells
# east and north of it.
ZOOM = 6
DENSE = (-92.2, 42.8)
GROUPS = {
    'east, 18 px away': ((-92.0, 42.8), 5, True),
    'east, 91 px away': ((-91.2, 42.8), 5, False),
    # North, the Mercator distance counts: 0.35 degrees is 44 px at this latitude, 0.45 is 56 px.
    'north, 44 px away': ((-92.2, 43.15), 3, True),
    'north, 56 px away': ((-92.2, 43.25), 3, False),
    'not a neighbouring cell': ((-80.0, 30.0), 5, False),
}


async def _add(database_, name, location, count):
    await database_.execute(
        "INSERT INTO samples (dataset_id, filepath, media_type, location) "
        "SELECT 1, $1 || '/' || i, 'image', ST_SetSRID(ST_MakePoint($2, $3), 4326) FROM generate_series(1, $4) i",
        name,
        *location,
        count,
    )


def test_a_bubble_takes_in_the_loose_points_it_is_drawn_over(registry_database):
    async def scenario(database_):
        await _add(database_, 'dense', DENSE, 150)
        for name, (location, count, _) in GROUPS.items():
            await _add(database_, name, location, count)

        result = await datasets._get_dataset_map_points_impl(1, None, zoom=ZOOM)
        assert result['mode'] == 'hybrid'
        bubbles = [item for item in result['items'] if item.get('id') is None]
        points = [item for item in result['items'] if item.get('id') is not None]
        absorbed = sum(count for _, count, inside in GROUPS.values() if inside)
        # The bubble stays where its own samples are, and is drawn at the radius it was measured with.
        # It also carries the extent of every sample it counts, the ones it took in included.
        assert bubbles == [
            {
                'lon': pytest.approx(DENSE[0]),
                'lat': pytest.approx(DENSE[1]),
                'count': 150 + absorbed,
                'radius': 52,
                'w': DENSE[0],
                's': DENSE[1],
                'e': -92.0,
                'n': 43.15,
            },
        ]
        drawn = sorted({(point['lon'], point['lat']) for point in points})
        assert drawn == sorted(location for location, _, inside in GROUPS.values() if not inside)
        # Every sample is still on the map, in the bubble or as a point.
        assert bubbles[0]['count'] + len(points) == 150 + sum(count for _, count, _ in GROUPS.values())

    registry_database(scenario)


def test_a_loose_point_under_two_bubbles_counts_in_the_nearer_one(registry_database):
    async def scenario(database_):
        # Bubbles in two cells side by side, and points in the cell north of them: 25 px from
        # the east bubble, 28 px from the west one.
        west, east = (-92.2, 42.8), (-92.0, 42.8)
        await _add(database_, 'west', west, 150)
        await _add(database_, 'east', east, 150)
        await _add(database_, 'north of both', (-92.05, 43.0), 4)

        result = await datasets._get_dataset_map_points_impl(1, None, zoom=ZOOM)
        counts = {
            (round(item['lon'], 6), round(item['lat'], 6)): item['count']
            for item in result['items']
            if item.get('id') is None
        }
        assert counts == {west: 150, east: 154}

    registry_database(scenario)


def test_the_clustered_view_never_runs_on_a_generic_plan(registry_database, monkeypatch):
    """From a prepared statement's 6th run on a connection PostgreSQL may switch to a generic plan,
    which for this query was about 9 times slower. pg_prepared_statements counts the plans used."""

    async def scenario(database_):
        await _add(database_, 'dense', DENSE, 150)
        # One connection, as a busy backend reuses one, so all eight runs share its prepared statement.
        single = Database()
        single.pool = await asyncpg.create_pool(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            min_size=1,
            max_size=1,
            init=init_connection,
            server_settings={'search_path': await database_.fetchval('SHOW search_path')},
        )
        monkeypatch.setattr(datasets, 'db', single)
        try:
            for _ in range(8):
                await datasets._get_dataset_map_points_impl(1, None, zoom=ZOOM)
            # This lookup is a prepared statement too, and its own text contains the pattern.
            plans = await single.fetch(
                "SELECT generic_plans, custom_plans FROM pg_prepared_statements "
                "WHERE statement LIKE '%array_agg(id)%' AND statement NOT LIKE '%pg_prepared_statements%'",
            )
        finally:
            await single.pool.close()
        assert [(row['generic_plans'], row['custom_plans']) for row in plans] == [(0, 8)]

    registry_database(scenario)


@pytest.mark.parametrize(
    ('count', 'radius'),
    [(100, 52), (999, 52), (1_000, 60), (10_000, 70), (99_999, 70), (100_000, 84)],
)
def test_bubble_radius_grows_with_its_count(count, radius):
    assert datasets._map_bubble_radius_px(count) == radius
