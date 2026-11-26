"""Zoom to Data for samples on both sides of longitude 180: POST /api/datasets/{id}/map_extent answers a box across
the antimeridian when the samples fit in half the globe that way (docs/MAP_REQUIREMENTS.md #7, beside its antimeridian
rules in #13). Its answer was the smallest and largest longitude: for Fiji's samples at 179 and -179 a box from -179 to
179, nearly the whole world, and "Zoom to Data" zoomed out to the world for data a few degrees wide.

Written from the contract, not from the code, and tested where a user meets it: the HTTP API signed in with a real API
token, on a database schema of its own per test, the samples added through POST /api/datasets/{id}/samples. The rule:

- When the widest stretch of longitude holding no located sample of the filter does not include longitude 180 and is
  at least 180 degrees wide, the answer is the box leaving it out, at most 180 degrees wide, across the antimeridian:
  w (the sample east of the stretch) greater than e (the sample west of it); Fiji: w 177.4, e -178.2. The latitudes
  are the smallest and largest, as before.
- In every other case the answer is exactly today's, the smallest and largest longitude, w <= e: when the widest
  stretch includes 180, and when it doesn't but is narrower than 180 degrees (the United States with China; the
  United States, Europe and Australia; samples round the globe). A plain box no wider than 180 degrees costs what it
  costs today: the smallest-and-largest read is the first and the only one; a second read happens only when the
  plain box is wider than 180 degrees.
- The widest stretch is found to within one degree; w and e are always the longitudes of real samples. Of two
  stretches equally wide within that degree either may be chosen, the same one every time for the same data.
- count, {"count": 0} for no located sample, w = e for a single sample, and the filters the route takes are unchanged.

Where the contract leaves something open, these tests read it this way:
- "Exactly today's" is the whole answer, {w, s, e, n, count}, and a box across the antimeridian has the same five
  keys. Every answer is asked for twice and must be the same both times.
- Each case asserted to one box is more than a degree from a tie and from the half-globe limit (its crossing box more
  than a degree narrower or wider than 180 degrees), so the one-degree leeway can't change it. Within a degree of
  either, both boxes the leeway allows are accepted, the same every time: today's, or the crossing box of a stretch
  that may be found at least 180 degrees wide.
- The cost is held as the statements the request sends that read the samples table: SQL text with `FROM samples` or
  `JOIN samples`, recorded on every asyncpg connection for the length of the request. For a plain box no wider than
  180 degrees that is one statement, taking min() and max(), as main sends. When the plain box is wider, "a second
  read" is read as at most two, the first still taking min() and max(). How long a statement takes isn't asserted.
"""

import os
import re
from typing import Any, Optional

import asyncpg
import httpx
import pytest

from backend import database as database_module
from backend import main
from backend.tests.test_access_rule_spec import ADMIN, World
from backend.tests.test_field_registry_postgres import registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

D, EMPTY = 1, 2  # registry_database's datasets: D holds each test's samples, EMPTY none

# (lon, lat) of located samples
FIJI = [(177.4, -17.6), (179.2, -16.8), (-179.5, -18.1), (-178.2, -16.2)]
# Attu, Anadyr, St. Paul, Nome, Anchorage: the widest empty stretch is from -149.9 east to 172.5.
BERING = [(172.5, 52.9), (177.6, 64.7), (-170.3, 57.1), (-165.4, 64.5), (-149.9, 61.2)]
EUROPE = [(10.0, 45.0), (12.0, 46.0)]
# Two groups either side of longitude 180 and one near 0: the widest empty stretch, 135 degrees from 110 to -115,
# includes 180 (the next is 110 degrees).
PACIFIC_RIM = [(100.0, 10.0), (110.0, 20.0), (-115.0, 30.0), (-105.0, 40.0), (5.0, 50.0)]
# The widest empty stretch, 225 degrees from -90 east to 135, includes longitude 0, not 180: the crossing box, from 135
# east to -90, is 135 degrees wide.
US_AUSTRALIA = [(-95.0, 40.0), (-90.0, 35.0), (135.0, -25.0), (140.0, -30.0)]
# Oregon, Boston; Kashgar, Shanghai, Harbin. The widest empty stretch, 147 degrees from -71 east to 76, avoids 180 but
# is narrower than 180 (the crossing box would be 213 degrees wide; across 180 the stretch is 109.4): today's box.
US_CHINA = [(-124.0, 43.0), (-71.0, 42.4), (76.0, 39.5), (121.5, 31.2), (126.6, 45.8)]
# The United States, Europe and Australia. The widest empty stretch, 103 degrees over Asia from 13 east to 116, avoids
# 180 but is narrower than 180 (the crossing box would be 257 wide; over the Pacific the stretch is 87): today's box.
THREE_CONTINENTS = [(-122.0, 38.0), (-74.0, 41.0), (2.0, 49.0), (13.0, 52.0), (116.0, -32.0), (151.0, -34.0)]
# Either side of 180 and of 0. Under the limit: the widest empty stretch, 183 degrees from -179 east to 4, avoids 180,
# so the crossing box, from 4 east to -179, is 177 degrees wide (the next stretch is 174).
UNDER_HALF_THE_GLOBE = [(179.0, 1.0), (-179.0, 2.0), (4.0, 3.0), (5.0, 4.0)]
# Over it: two stretches of 176 degrees (-179 east to -3, and 3 east to 179) avoid 180, both narrower than 180 by
# more than a degree (crossing boxes 184 wide): today's box, -179 to 179.
OVER_HALF_THE_GLOBE = [(179.0, 1.0), (-179.0, 2.0), (-3.0, 3.0), (3.0, 4.0)]
WEST_ONLY = [(170.0, -15.0), (175.5, -16.0), (179.9, -17.0)]
EAST_ONLY = [(-179.9, -15.0), (-175.0, -16.0), (-170.5, -17.0)]
IOWA = [(-93.62, 41.98), (-93.6, 42.03), (-93.55, 42.01), (-93.65, 41.99)]
# Every 20 degrees from -160 to 160: across 180 the stretch is 40 degrees, every other 20.
EVERY_20 = [(float(lon), lon / 4) for lon in range(-160, 161, 20)]
# Every 20 degrees from -170 to 170: all 18 stretches 20 degrees wide, none near 180 wide.
ROUND_THE_GLOBE = [(float(lon), 0.0) for lon in range(-170, 171, 20)]


def box(w: float, s: float, e: float, n: float, count: int) -> dict[str, Any]:
    return {'w': w, 's': s, 'e': e, 'n': n, 'count': count}


def todays(points: list) -> dict[str, Any]:
    """Today's answer: the smallest and largest longitude and latitude, and the number of samples."""
    lons, lats = [point[0] for point in points], [point[1] for point in points]
    return box(min(lons), min(lats), max(lons), max(lats), len(points))


@pytest.fixture
def extent_world(registry_database, monkeypatch):
    """extent_world(scenario) runs `scenario(World)`: the app on the test's schema, an admin signed in with an API
    token."""

    def run(scenario):
        async def with_app(database):
            # The modules registry_database doesn't point at the test's schema (auth, access) use the app's shared
            # Database: it gets the test's pool.
            monkeypatch.setattr(database_module.db, 'pool', database.pool)
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                w = World(database, client)
                await w.user(ADMIN, 'admin')
                await scenario(w)

        registry_database(with_app)

    return run


async def plant(w: World, points: list, prefix: str = 'sample', unlocated: int = 0, dataset_id: int = D) -> None:
    """The samples, added through the API: one at each (lon, lat[, metadata]) of `points`, and `unlocated` more with
    no location."""
    samples: list[dict[str, Any]] = []
    for index, (lon, lat, *metadata) in enumerate(points):
        sample: dict[str, Any] = {
            'filepath': f'{prefix}-{index}.jpg',
            'location': {'type': 'Point', 'coordinates': [lon, lat]},
        }
        if metadata:
            sample['metadata'] = metadata[0]
        samples.append(sample)
    samples.extend({'filepath': f'{prefix}-nowhere-{index}.jpg'} for index in range(unlocated))
    added = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/samples', json={'samples': samples})
    assert added.is_success, added.text
    rows = await w.database.fetch(
        "SELECT lon, lat FROM samples WHERE dataset_id = $1 AND filepath LIKE $2 || '-%'",
        dataset_id,
        prefix,
    )
    stored = sorted((row['lon'], row['lat']) for row in rows if row['lon'] is not None)
    assert stored == sorted((point[0], point[1]) for point in points)
    assert len(rows) == len(points) + unlocated


async def extent(w: World, filters: Optional[dict[str, Any]] = None, dataset_id: int = D) -> dict[str, Any]:
    """map_extent's answer for the filter, asked twice: the same both times."""
    answers = []
    for _ in range(2):
        body = {} if filters is None else {'filters': filters}
        response = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/map_extent', json=body)
        assert response.status_code == 200, response.text
        answers.append(response.json())
    assert answers[0] == answers[1], answers
    return answers[0]


def assert_real_edges(answer: dict[str, Any], points: list) -> None:
    """w and e are the longitudes of real samples."""
    lons = {point[0] for point in points}
    assert answer['w'] in lons and answer['e'] in lons, (answer, sorted(lons))


def side(name: str) -> dict[str, Any]:
    return {'field': 'side', 'operator': '=', 'value': name}


def either(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'OR', 'filters': list(nodes)}


def near(lon: float, lat: float, metres: float) -> dict[str, Any]:
    return {'field': 'location', 'operator': 'geo_near', 'value': {'point': [lon, lat], 'max_distance': metres}}


# --- 1.1: samples that fit in half the globe across 180 get a box across it ---


@pytest.mark.parametrize(
    ('points', 'expected'),
    [
        (FIJI, box(177.4, -18.1, -178.2, -16.2, 4)),
        (BERING, box(172.5, 52.9, -149.9, 64.7, 5)),
    ],
    ids=['fiji', 'chukotka-and-alaska'],
)
def test_samples_on_both_sides_of_180_only_get_a_box_across_it(extent_world, points, expected):
    async def scenario(w):
        await plant(w, points, unlocated=2)  # the two with no location aren't counted
        answer = await extent(w)
        assert answer == expected, f'today: {todays(points)}'
        assert answer['w'] > answer['e']
        assert_real_edges(answer, points)

    extent_world(scenario)


@pytest.mark.parametrize(
    ('points', 'expected'),
    [
        # The widest empty stretch, 188.2 degrees from -178.2 east to 10, avoids 180 (the next is 165.4): the crossing
        # box, from 10 east to -178.2, is 171.8 degrees wide.
        (FIJI + EUROPE, box(10.0, -18.1, -178.2, 46.0, 6)),
        (US_AUSTRALIA, box(135.0, -30.0, -90.0, 40.0, 4)),
        (UNDER_HALF_THE_GLOBE, box(4.0, 1.0, -179.0, 4.0, 4)),
    ],
    ids=['fiji-and-europe', 'united-states-and-australia', 'just-under-half-the-globe'],
)
def test_with_a_group_elsewhere_a_box_at_most_half_the_globe_crosses_180(extent_world, points, expected):
    async def scenario(w):
        await plant(w, points)
        answer = await extent(w)
        assert answer == expected, f'today: {todays(points)}'
        assert answer['w'] > answer['e']
        assert_real_edges(answer, points)

    extent_world(scenario)


# --- 1.3 and 1.5: in every other case, today's answer exactly ---


@pytest.mark.parametrize(
    'points',
    [WEST_ONLY, EAST_ONLY, IOWA, EVERY_20],
    ids=['west-of-180-only', 'east-of-180-only', 'one-cluster', 'every-20-degrees'],
)
def test_samples_on_one_side_of_180_get_todays_box(extent_world, points):
    async def scenario(w):
        await plant(w, points)
        answer = await extent(w)
        assert answer == todays(points)
        assert answer['w'] <= answer['e']

    extent_world(scenario)


@pytest.mark.parametrize(
    'points',
    [PACIFIC_RIM, US_CHINA, THREE_CONTINENTS, OVER_HALF_THE_GLOBE, ROUND_THE_GLOBE],
    ids=[
        'stretch-across-180-widest',
        'united-states-and-china',
        'three-continents',
        'just-over-half-the-globe',
        'round-the-globe',
    ],
)
def test_samples_wider_than_half_the_globe_either_way_get_todays_box(extent_world, points):
    async def scenario(w):
        await plant(w, points)
        answer = await extent(w)
        assert answer == todays(points)
        assert answer['w'] <= answer['e']

    extent_world(scenario)


# --- 1.2: within a degree of a tie, or of the half-globe limit ---


@pytest.mark.parametrize(
    ('points', 'valid'),
    [
        # 180 degrees each way: the stretch across 180 and the one across 0 tie, and the crossing box is 180 wide.
        ([(-90.0, 0.0), (90.0, 0.0)], [box(-90.0, 0.0, 90.0, 0.0, 2), box(90.0, 0.0, -90.0, 0.0, 2)]),
        # From -179 east to 0.4 is 179.4 degrees (crossing box 180.6 wide, within a degree of 180), from 0.4 to 179
        # 178.6 (crossing box 181.4, more than a degree over): today's box, or the first crossing box.
        ([(179.0, 0.0), (-179.0, 0.0), (0.4, 0.0)], [box(-179.0, 0.0, 179.0, 0.0, 3), box(0.4, 0.0, -179.0, 0.0, 3)]),
        # From -179 east to 1.6 is 180.6 degrees (crossing box 179.4 wide, within a degree of 180): either.
        ([(179.0, 0.0), (-179.0, 0.0), (1.6, 0.0)], [box(-179.0, 0.0, 179.0, 0.0, 3), box(1.6, 0.0, -179.0, 0.0, 3)]),
    ],
    ids=['opposite-meridians', 'crossing-box-just-over-180-wide', 'crossing-box-just-under-180-wide'],
)
def test_within_a_degree_of_a_tie_or_of_half_the_globe_either_box_and_the_same_every_time(extent_world, points, valid):
    async def scenario(w):
        await plant(w, points)
        answers = [await extent(w) for _ in range(3)]
        assert answers[0] in valid, f'today: {todays(points)}'
        assert answers == [answers[0]] * 3

    extent_world(scenario)


# --- 1.4: count, no located sample, a single sample, filters ---


def test_a_single_sample_and_no_located_sample_answer_as_before(extent_world):
    async def scenario(w):
        await plant(w, [], prefix='unlocated', unlocated=3)
        assert await extent(w) == {'count': 0}
        assert await extent(w, dataset_id=EMPTY) == {'count': 0}
        await plant(w, [(179.9, -17.0)], prefix='one')
        assert await extent(w) == box(179.9, -17.0, 179.9, -17.0, 1)
        await plant(w, [(-179.9, 16.0)], prefix='one', dataset_id=EMPTY)
        assert await extent(w, dataset_id=EMPTY) == box(-179.9, 16.0, -179.9, 16.0, 1)
        await plant(w, [(-179.9, 16.0)] * 2, prefix='same-spot', dataset_id=EMPTY)
        assert await extent(w, dataset_id=EMPTY) == box(-179.9, 16.0, -179.9, 16.0, 3)

    extent_world(scenario)


FIJI_EUROPE_SIDES = [(lon, lat, {'side': 'west' if lon > 0 else 'east'}) for lon, lat in FIJI] + [
    (lon, lat, {'side': 'far'}) for lon, lat in EUROPE
]
WEST_BOX = box(177.4, -17.6, 179.2, -16.8, 2)
FIJI_BOX = box(177.4, -18.1, -178.2, -16.2, 4)


@pytest.mark.parametrize(
    ('filters', 'expected'),
    [
        (side('west'), WEST_BOX),
        (side('east'), box(-179.5, -18.1, -178.2, -16.2, 2)),
        (
            {'field': 'location', 'operator': 'geo_polygon', 'value': [[176, -20], [180, -20], [180, -15], [176, -15]]},
            WEST_BOX,
        ),
        # One side and Europe: the widest empty stretch, 190.8 degrees from 179.2 east to 10, includes 180.
        (either(side('west'), side('far')), box(10.0, -17.6, 179.2, 46.0, 4)),
        (near(11.0, 45.5, 500_000), box(10.0, 45.0, 12.0, 46.0, 2)),
        (side('nowhere'), {'count': 0}),
        # Both sides of 180, not Europe: across the antimeridian.
        (either(side('west'), side('east')), FIJI_BOX),
        (near(180.0, -17.0, 500_000), FIJI_BOX),
    ],
    ids=[
        'one-side',
        'other-side',
        'polygon-one-side',
        'one-side-and-europe',
        'near-europe',
        'none',
        'both-sides',
        'near-180',
    ],
)
def test_a_filter_keeping_one_side_answers_todays_box_and_one_keeping_both_sides_crosses(
    extent_world,
    filters,
    expected,
):
    async def scenario(w):
        await plant(w, FIJI_EUROPE_SIDES, unlocated=1)
        answer = await extent(w, filters)
        assert answer == expected
        if answer['count']:
            assert_real_edges(answer, FIJI_EUROPE_SIDES)

    extent_world(scenario)


# --- 1.3: the cost ---


SAMPLES_READ = re.compile(r'\b(?:FROM|JOIN)\s+samples\b', re.IGNORECASE)


async def samples_reads(w: World, monkeypatch: Any, filters: Optional[dict[str, Any]] = None) -> list[str]:
    """The statements one map_extent request sends that read the samples table, in order."""
    statements: list[str] = []
    with monkeypatch.context() as patch:
        for name in ('fetch', 'fetchrow', 'fetchval', 'execute', 'executemany', 'prepare', 'cursor'):
            original = getattr(asyncpg.Connection, name)

            def recorded(self, query, *args, _original=original, **kwargs):
                statements.append(query)
                return _original(self, query, *args, **kwargs)

            patch.setattr(asyncpg.Connection, name, recorded)
        body = {} if filters is None else {'filters': filters}
        response = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/map_extent', json=body)
    assert response.status_code == 200, response.text
    return [statement for statement in statements if SAMPLES_READ.search(statement)]


def takes_min_and_max(statement: str) -> bool:
    found = [re.search(rf'\b{name}\s*\(', statement, re.IGNORECASE) for name in ('min', 'max')]
    return all(found)


@pytest.mark.parametrize(
    ('points', 'filters'),
    [(WEST_ONLY, None), (IOWA, None), (FIJI_EUROPE_SIDES, side('west'))],
    ids=['west-of-180-only', 'one-cluster', 'filtered-to-one-side'],
)
def test_a_plain_box_no_wider_than_180_degrees_reads_the_samples_once_as_before(
    extent_world,
    monkeypatch,
    points,
    filters,
):
    async def scenario(w):
        await plant(w, points)
        reads = await samples_reads(w, monkeypatch, filters)
        assert len(reads) == 1, reads  # main sends one: the min() and max() read
        assert takes_min_and_max(reads[0]), reads

    extent_world(scenario)


@pytest.mark.parametrize('points', [FIJI, PACIFIC_RIM], ids=['fiji', 'stretch-across-180-widest'])
def test_a_plain_box_wider_than_180_degrees_reads_the_samples_at_most_twice(extent_world, monkeypatch, points):
    async def scenario(w):
        await plant(w, points)
        reads = await samples_reads(w, monkeypatch)
        assert 1 <= len(reads) <= 2, reads
        assert takes_min_and_max(reads[0]), reads

    extent_world(scenario)
