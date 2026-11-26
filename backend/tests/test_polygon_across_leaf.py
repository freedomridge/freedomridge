"""_polygon_across_leaf, the filter compiler's geo_polygon across the antimeridian (docs/MAP_REQUIREMENTS.md #8). The
map's draw tool sends a shape drawn across the antimeridian with longitudes past 180; a sample is inside when its
location, moved by whole turns of 360 degrees, lies inside the shape. A polygon within -180 to 180 compiles as it
always did. And _polygon_points, which reads the value: no polygon keeps every sample, a malformed value is a 400."""

import json
import os

import pytest
from fastapi import HTTPException

from backend.routers import datasets
from backend.tests.test_field_registry_postgres import registry_database

__all__ = ['registry_database']
postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

ACROSS = [[176, -20], [183, -20], [183, -15], [176, -15]]
COPIES = [
    'POLYGON((176.0 -20.0, 183.0 -20.0, 183.0 -15.0, 176.0 -15.0, 176.0 -20.0))',
    'POLYGON((-184.0 -20.0, -177.0 -20.0, -177.0 -15.0, -184.0 -15.0, -184.0 -20.0))',
]


def _where(op, value):
    tree = {'field': 'location', 'operator': op, 'value': value}
    where, args, _ = datasets.build_sample_query_parts(1, json.dumps(tree), array_columns=set())
    return where, args[1:]


def test_a_polygon_within_180_compiles_as_it_always_did():
    within = ' WHERE dataset_id = $1 AND ST_Within(location, ST_GeomFromText($2, 4326))'
    without = ' WHERE dataset_id = $1 AND (location IS NULL OR NOT ST_Within(location, ST_GeomFromText($2, 4326)))'
    # Points at 170 and -170 are the shape through longitude 0, as before; an open ring is closed with its first point.
    through_zero = [[170, -20], [-170, -20], [-170, -15], [170, -15]]
    assert _where('geo_polygon', through_zero) == (within, ['POLYGON((170 -20, -170 -20, -170 -15, 170 -15, 170 -20))'])
    edges = [[-180, -20], [180, -20], [180, -15], [-180, -15], [-180, -20]]
    assert _where('geo_polygon_exclude', edges) == (
        without,
        ['POLYGON((-180 -20, 180 -20, 180 -15, -180 -15, -180 -20))'],
    )
    # A point's further entries are ignored, and a ring closed with its first point but for a further entry is closed
    # again, as before.
    assert _where('geo_polygon', [[1, 2, 5], [3, 2], [3, 4], [1, 2, 6]]) == (
        within,
        ['POLYGON((1 2, 3 2, 3 4, 1 2, 1 2))'],
    )


@pytest.mark.parametrize('turns', [0, 1, -1, 3])
def test_a_polygon_across_180_is_tested_on_its_two_copies_on_any_copy_of_the_world(turns):
    shape = [[lon + 360 * turns, lat] for lon, lat in ACROSS]
    either = '(ST_Within(location, ST_GeomFromText($2, 4326)) OR ST_Within(location, ST_GeomFromText($3, 4326)))'
    assert _where('geo_polygon', shape) == (f' WHERE dataset_id = $1 AND {either}', COPIES)
    # The exclude tests the two copies as one MULTIPOLYGON, one test a sample, behind their boxes.
    where, args = _where('geo_polygon_exclude', shape)
    assert where == (
        ' WHERE dataset_id = $1 AND (location IS NULL OR NOT (location && ST_GeomFromText($2, 4326) '
        'OR location && ST_GeomFromText($3, 4326)) OR NOT ST_Within(location, ST_GeomFromText($4, 4326)))'
    )
    assert args == [*COPIES, f"MULTIPOLYGON({', '.join(copy.removeprefix('POLYGON') for copy in COPIES)})"]


def test_a_polygon_on_one_other_copy_of_the_world_is_that_polygon_on_the_first():
    within = ' WHERE dataset_id = $1 AND ST_Within(location, ST_GeomFromText($2, 4326))'
    without = ' WHERE dataset_id = $1 AND (location IS NULL OR NOT ST_Within(location, ST_GeomFromText($2, 4326)))'
    copy = ['POLYGON((10.0 -20.0, 20.0 -20.0, 20.0 -15.0, 10.0 -20.0))']
    assert _where('geo_polygon', [[370, -20], [380, -20], [380, -15]]) == (within, copy)
    assert _where('geo_polygon_exclude', [[-350, -20], [-340, -20], [-340, -15]]) == (without, copy)


def test_a_polygon_360_degrees_wide_or_more_is_tested_on_each_copy_it_needs():
    where, args = _where('geo_polygon_exclude', [[176, -30], [536, -30], [536, 50], [176, 50]])
    assert where == (
        ' WHERE dataset_id = $1 AND (location IS NULL OR NOT (ST_Within(location, ST_GeomFromText($2, 4326)) '
        'OR ST_Within(location, ST_GeomFromText($3, 4326))))'
    )
    assert args == [
        'POLYGON((176.0 -30.0, 536.0 -30.0, 536.0 50.0, 176.0 50.0, 176.0 -30.0))',
        'POLYGON((-184.0 -30.0, 176.0 -30.0, 176.0 50.0, -184.0 50.0, -184.0 -30.0))',
    ]
    assert len(_where('geo_polygon', [[0, -1], [21600, -1], [21600, 1], [0, 1]])[1]) == 61
    with pytest.raises(HTTPException) as refused:
        _where('geo_polygon', [[0, -1], [21601, -1], [21601, 1], [0, 1]])
    assert refused.value.status_code == 400


NO_POLYGON = {
    'null': None,
    'an empty list': [],
    'two points': [[176, -20], [183, -20]],
    'two entries that are no points': [5, 'x'],
    'three equal points': [[1, 2], [1, 2], [1, 2]],
    'two points and the first again': [[176, -20], [183, -20], [176, -20]],
    'two points and the first again, a further entry apart': [[1, 2, 5], [3, 4], [1, 2, 6]],
}


@pytest.mark.parametrize('value', NO_POLYGON.values(), ids=NO_POLYGON.keys())
@pytest.mark.parametrize('op', ['geo_polygon', 'geo_polygon_exclude'])
def test_no_polygon_keeps_every_sample(op, value):
    assert _where(op, value) == (' WHERE dataset_id = $1 AND TRUE', [])


REFUSED = {
    'text': 'abc',
    'empty text': '',
    'an empty object': {},
    'a GeoJSON polygon': {'type': 'Polygon', 'coordinates': [ACROSS]},
    'a number': 5,
    'true': True,
    'an entry that is a number': [5, [1, 2], [3, 4]],
    'a point of one entry': [[1], [2, 3], [4, 5]],
    'a null entry': [None, [1, 2], [3, 4]],
    'a point as an object': [{'lon': 1, 'lat': 2}, [2, 3], [4, 5]],
    'text that is no number': [['x', 1], [2, 3], [4, 5]],
    'empty text as a coordinate': [['', 1], [2, 3], [4, 5]],
    'true as a coordinate': [[True, 1], [2, 3], [4, 5]],
    'null as a coordinate': [[1, None], [2, 3], [4, 5]],
    'a list as a coordinate': [[[1], 1], [2, 3], [4, 5]],
    'NaN': [[float('nan'), 1], [2, 3], [4, 5]],
    'infinity': [[1, float('inf')], [2, 3], [4, 5]],
    'text infinity': [['inf', 1], [2, 3], [4, 5]],
    'an integer too large for a double': [[10**400, 1], [2, 3], [4, 5]],
}


@pytest.mark.parametrize('value', REFUSED.values(), ids=REFUSED.keys())
@pytest.mark.parametrize('op', ['geo_polygon', 'geo_polygon_exclude'])
def test_a_malformed_value_is_refused_with_what_the_operator_takes(op, value):
    with pytest.raises(HTTPException) as refused:
        _where(op, value)
    assert refused.value.status_code == 400
    assert refused.value.detail.startswith(f'{op} takes a list of [longitude, latitude] points'), refused.value.detail


def test_a_coordinate_may_be_text_that_reads_as_a_number():
    either = '(ST_Within(location, ST_GeomFromText($2, 4326)) OR ST_Within(location, ST_GeomFromText($3, 4326)))'
    text = [[str(lon), f' {lat} '] for lon, lat in ACROSS]
    assert _where('geo_polygon', text) == (f' WHERE dataset_id = $1 AND {either}', COPIES)
    within = ' WHERE dataset_id = $1 AND ST_Within(location, ST_GeomFromText($2, 4326))'
    assert _where('geo_polygon', [['-10', -5], [10, '-5'], ['1e1', '5']]) == (
        within,
        ['POLYGON((-10.0 -5, 10 -5.0, 10.0 5.0, -10.0 -5))'],
    )


@postgres
def test_the_samples_on_both_sides_are_inside_and_the_exclude_keeps_the_rest(registry_database):
    places = {
        'west': (177.4, -17.6),
        'on 180': (180.0, -17.0),
        'on -180': (-180.0, -17.5),
        'east': (-178.2, -16.2),
        'far': (100.0, 10.0),
        'zero': (0.0, 0.0),
    }

    async def scenario(database_):
        for name, (lon, lat) in places.items():
            await database_.execute(
                "INSERT INTO samples (dataset_id, filepath, media_type, location) "
                "VALUES (1, $1, 'image', ST_SetSRID(ST_MakePoint($2, $3), 4326))",
                name,
                lon,
                lat,
            )
        await database_.execute("INSERT INTO samples (dataset_id, filepath, media_type) VALUES (1, 'nowhere', 'image')")

        async def found(op, value):
            where, args = _where(op, value)
            return sorted(
                r['filepath'] for r in await database_.fetch(f'SELECT filepath FROM samples {where}', 1, *args)
            )

        fiji = sorted(['west', 'on 180', 'on -180', 'east'])
        for turns in (0, 1, -1):
            shape = [[lon + 360 * turns, lat] for lon, lat in ACROSS]
            assert await found('geo_polygon', shape) == fiji
            assert await found('geo_polygon_exclude', shape) == ['far', 'nowhere', 'zero']
        # The halves as ordinary polygons hold one side each, as before.
        assert await found('geo_polygon', [[176, -20], [180, -20], [180, -15], [176, -15]]) == ['west']
        wide = [[176, -30], [536, -30], [536, 50], [176, 50]]
        assert await found('geo_polygon', wide) == sorted([*fiji, 'far', 'zero'])
        assert await found('geo_polygon_exclude', wide) == ['nowhere']
        # Text that reads as a number is that number: "183" is past 180.
        assert await found('geo_polygon', [[str(lon), str(lat)] for lon, lat in ACROSS]) == fiji

    registry_database(scenario)
