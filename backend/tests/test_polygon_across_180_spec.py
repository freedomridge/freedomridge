"""A polygon filter drawn across longitude 180 finds the samples on both sides (contract g3, polygon across 180).

Written from the contract, not from the code, and tested where a user meets it: the HTTP API signed in with real API
tokens (the access-rule tests' World, through the region tests' regions_world: a database of its own per test), the
samples added through POST /api/datasets/{id}/samples, the SDK driven as a script drives it (its requests served by the
app in-process), and the assistant's tools through assistant.call_tool. The browser's side is
frontend/e2e/polygon-across-180.spec.ts.

The rule (contract section 1): a polygon is a shape as it was drawn, its points in one frame whose longitudes may run
past 180 or below -180; a sample is inside when its location, moved east or west by whole turns of 360 degrees, lies
inside the shape. A polygon whose longitudes all lie from -180 to 180 means what it means today. The exclusion removes
exactly what the polygon holds and keeps every other sample, those with no location included. A polygon 360 degrees
wide or more is no error, up to 60 turns of the globe (21,600 degrees); one wider is refused with a 400 (amended
2026-11-24). What each other value answers (rule 1.6, amended 2026-11-24): null and a list of fewer than three entries
keep every sample, and so does a ring with fewer than three points besides its closing point; an open ring is closed
and a point's entries after its latitude are ignored; a coordinate written as text is read as that number; any other
value is refused with a 400 naming `geo_polygon` wherever the filter is read at once.

The samples (section 5.1): two each side of the antimeridian near Fiji (177.4 and 179.2; -179.5 and -178.2, which are
180.5 and 181.8 in a frame running past 180), three far away (longitude 0, 100 and -100) and one with no location;
half of them in group `a`, for the polygon ANDed with another filter. Each polygon is asked as `geo_polygon` or
`geo_polygon_exclude`, alone and ANDed with `grp = "a"`, through samples/query and its count, map_extent, and map_points
(every point, and the map's view across the antimeridian). Every expected answer is a set of planted samples the test
names; a reference point-in-polygon (moving each sample by whole turns) checks the test's own sets first, so a wrong set
is the test's mistake, not the server's.

Where the contract leaves something open, these tests read it this way:
- "Runs the same SQL (the same statement and plan)" (1.3) is not asserted: these tests assert what a user or a script
  sees. A polygon within -180 to 180 is pinned by the samples, counts and map answers it gives today.
- map_extent's box is the one docs/MAP_REQUIREMENTS.md #7 gives for the samples the filter keeps (a box across the
  antimeridian when they fit in half the globe that way); every case is more than a degree from that rule's limits.
- map_points' "points and bubbles" (2.1): bubbles are asked of a dataset of their own, 60 samples each side of the
  antimeridian and 5 far away; what the map shows is counted as each bubble's count plus each point.
- On a dataset with objects (2.1, 5.2) the grids' filters are the app's (FilterContext.withGrain, as
  test_regions_spec.grain and .excluded write it): the row or its partner inside, and for an exclusion NOT (the row or
  its partner is inside).
- Rule 1.6 is asked through samples/query (its count agreeing), map_extent and map_points, for geo_polygon and
  geo_polygon_exclude, alone and ANDed with another filter, through a client that returns the server's error as its
  status instead of raising it, so main's unhandled errors show as 500s. "Every sample" is every sample for both
  operators (1.6.2's "no polygon either" read as 1.6.1's). 1.6.5's 400 "names `geo_polygon` and says what it takes"
  is read as an answer whose text holds `geo_polygon`, `longitude` and `latitude`; "a coordinate that is not a finite
  number" includes text that reads as no finite number (`"NaN"`) and null. The value read as 1.6.1's "a list of fewer
  than three entries" holds whatever its entries are (`[[1]]`, `[1, 2]`).
- Rule 1.5's limit is a test of its own: a polygon whose longitudes span exactly 21,600 degrees is answered, one
  spanning more (21,601 degrees, and points at 176 and 1e15) is refused with a 400 whose text names the limit (60
  turns, or 21,600 degrees). A narrow polygon lying far out (all its longitudes near 1e15) is not asked: the limit is
  written for a polygon's width.
- "Every reader" (2.1) is samples/query and its count, an export's pages (samples/query with after_id, slim and
  skip_count, as the export dialog pages), aggregate, the histogram, field-stats and a field's autocomplete (the
  sidebar), map_points, map_extent, a selection token and in_selection (Select All), /api/search, a bulk tag (its count
  and the rows it tagged), a version's samples/query and aggregate, the SDK (geo_within, and a Condition of
  geo_polygon_exclude) and the assistant (open_saved_view's count and get_samples' samples). The same readers are
  pinned for a polygon within -180 to 180.
- Saved views and links (2.4): a view saved with the polygon as the app saves a drawn one (a `map_region` stage), read
  back by its slug and in the dataset's list with its points as sent, and opened again by its stage's filter, the SDK
  (load_view) and the assistant (open_saved_view). A polygon has no text form today (filter_query/format answers null
  for one); that a polygon across the antimeridian answers the same is pinned. The address bar is the browser test's.
- Each test that holds new behaviour asks a polygon within -180 to 180 first, as a control that passes on main, so a
  failure on main is the polygon across the antimeridian's.
"""

import asyncio
import json
import os
from typing import Any, Optional
from uuid import uuid4

import httpx
import pytest
from fridge import Condition, Dataset, geo_within

from backend import main
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_assistant_core_spec import open_door
from backend.tests.test_assistant_views_spec import result, view_ids
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_protected_metadata_keys_spec import sdk
from backend.tests.test_regions_spec import Regions, excluded, grain, post, selected

database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(
    not (os.getenv('FRIDGE_TEST_POSTGRES_DSN') and os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')),
    reason='Requires disposable PostgreSQL',
)

Point = tuple[float, float]
Polygon = list[list[float]]

PLAIN, DENSE, OBJECTS = 41, 42, 43

# The samples of the plain dataset: name -> location (None: no location).
PLACES: dict[str, Optional[Point]] = {
    'w1': (177.4, -17.6),  # west of the antimeridian
    'w2': (179.2, -16.8),
    'e1': (-179.5, -18.1),  # east of it: 180.5 in a frame running past 180
    'e2': (-178.2, -16.2),  # 181.8
    'zero': (0.0, -17.0),
    'hundred': (100.0, 20.0),
    'minus': (-100.0, 30.0),
    'none': None,
}
EVERY = set(PLACES)
LOCATED = {name for name, point in PLACES.items() if point is not None}
FIJI = {'w1', 'w2', 'e1', 'e2'}
WEST, EAST = {'w1', 'w2'}, {'e1', 'e2'}
GROUP_A = {'w1', 'e1', 'hundred', 'none'}  # one each side of the antimeridian, one far, one nowhere
GRP_A = {'field': 'grp', 'operator': '=', 'value': 'a'}
# The map's view across the antimeridian over Fiji, as mapbox-gl reports it (docs/MAP_REQUIREMENTS.md #13).
VIEW_ACROSS = [170.0, -25.0, 190.0, -10.0]


def rect(west: float, south: float, east: float, north: float) -> Polygon:
    """An open ring, as the map's draw tool and the SDK send one: the first point is not repeated."""
    return [[west, south], [east, south], [east, north], [west, north]]


def turned(polygon: Polygon, turns: int) -> Polygon:
    """The same shape, `turns` whole turns east (west when negative)."""
    return [[lon + 360 * turns, lat] for lon, lat in polygon]


def closed(polygon: Polygon) -> Polygon:
    return [*polygon, polygon[0]]


def place(operator: str, polygon: Any) -> dict[str, Any]:
    return {'field': 'location', 'operator': operator, 'value': polygon}


def both(node: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'AND', 'filters': [node, other]}


# Across the antimeridian: some longitude past 180 (the draw tool's frame over Fiji).
ACROSS = rect(176, -20, 183, -14)
TRIANGLE_ACROSS: Polygon = [[175, -20], [185, -20], [180, -12]]
NARROW_ACROSS = rect(179, -20, 181, -14)  # 179.2 and -179.5 (180.5); not 177.4, nor -178.2 (181.8)
# The two halves of ACROSS written as ordinary polygons, within -180 to 180.
WEST_HALF = rect(176, -20, 180, -14)
EAST_HALF = rect(-180, -20, -177, -14)

# (what the polygon is, its points, the samples inside it)
CROSSING: list[tuple[str, Polygon, set[str]]] = [
    ('176 to 183', ACROSS, FIJI),
    ('176 to 183, its ring closed', closed(ACROSS), FIJI),
    ('a triangle from 175 to 185', TRIANGLE_ACROSS, FIJI),
    ('179 to 181', NARROW_ACROSS, {'w2', 'e1'}),
]
OTHER_COPIES: list[tuple[str, Polygon, set[str]]] = [
    ('176 to 183, one turn east: 536 to 543', turned(ACROSS, 1), FIJI),
    ('176 to 183, one turn west: -184 to -177', turned(ACROSS, -1), FIJI),
    ('176 to 183, two turns east', turned(ACROSS, 2), FIJI),
    ('179 to 181, one turn west', turned(NARROW_ACROSS, -1), {'w2', 'e1'}),
    ('the east half one turn east: 180 to 183', turned(EAST_HALF, 1), EAST),
    ('the west half one turn west: -184 to -180', turned(WEST_HALF, -1), WEST),
    ('the west half one turn east: 536 to 540', turned(WEST_HALF, 1), WEST),
]
WIDE: list[tuple[str, Polygon, set[str]]] = [
    ('360 degrees wide, -5 to 355', rect(-5, -25, 355, 25), FIJI | {'zero', 'hundred'}),
    ('400 degrees wide, -200 to 200', rect(-200, -25, 200, 25), FIJI | {'zero', 'hundred'}),
    ('720 degrees wide, -365 to 355', rect(-365, -25, 355, 25), FIJI | {'zero', 'hundred'}),
    # Wider than the world at its base, narrow at its top: 100 at latitude 20 is outside it on every copy.
    ('a triangle 420 degrees wide at its base', [[-170, -30], [250, -30], [30, 40]], FIJI | {'zero'}),
]
# Within -180 to 180: what they hold today.
ORDINARY: list[tuple[str, Polygon, set[str]]] = [
    ('the west half, 176 to 180', WEST_HALF, WEST),
    ('the east half, -180 to -177', EAST_HALF, EAST),
    ('round longitude 0', rect(-10, -25, 10, -10), {'zero'}),
    ('round longitude 100', rect(90, 10, 110, 30), {'hundred'}),
    # Points at 170 and -170: the shape through longitude 0, as today.
    ('170 and -170: through longitude 0', [[170, -20], [-170, -20], [-170, -14], [170, -14]], {'zero'}),
    ('-180 to 180', rect(-180, -25, 180, 25), FIJI | {'zero', 'hundred'}),
]


# --- The test's own expectations, checked against a reference point-in-polygon ---


def _inside(polygon: Polygon, lon: float, lat: float) -> bool:
    """Ray casting; no planted sample is within 0.1 degree of an edge of a polygon here."""
    found = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            found = not found
    return found


def by_the_rule(polygon: Polygon, places: dict[str, Optional[Point]]) -> set[str]:
    """The samples inside the shape when each is moved by whole turns (contract 1.1)."""
    return {
        name
        for name, point in places.items()
        if point is not None and any(_inside(polygon, point[0] + 360 * k, point[1]) for k in range(-3, 4))
    }


def check_expectations(cases: list[tuple[str, Polygon, set[str]]]) -> None:
    for label, polygon, names in cases:
        assert by_the_rule(polygon, PLACES) == names, f"the test's own expectation for {label}"


def box_of(names: set[str]) -> dict[str, Any]:
    """map_extent's answer for these samples (docs/MAP_REQUIREMENTS.md #7): the smallest and largest longitude and
    latitude, or the box across the antimeridian when the widest stretch with no sample avoids 180 and is at least 180
    degrees wide."""
    points = [point for name, point in PLACES.items() if name in names and point is not None]
    if not points:
        return {'count': 0}
    lons = sorted({lon for lon, _ in points})
    lats = [lat for _, lat in points]
    answer = {'w': lons[0], 's': min(lats), 'e': lons[-1], 'n': max(lats), 'count': len(points)}
    stretches = sorted(((east - west, east, west) for west, east in zip(lons, lons[1:])), reverse=True)
    if stretches:
        width, east_of_it, west_of_it = stretches[0]
        assert not 179 <= width <= 181, f"the test's case is within a degree of #7's limit: {sorted(names)}"
        if width >= 180:
            answer.update(w=east_of_it, e=west_of_it)
    return answer


# --- The datasets ---


async def ok(response: httpx.Response, where: Any = None) -> Any:
    assert response.status_code == 200, (where, response.status_code, response.text)
    return response.json()


class Planted:
    """A dataset's samples by name."""

    def __init__(self, dataset_id: int, ids: dict[str, int]):
        self.dataset_id, self.ids = dataset_id, ids
        self.by_id = {sample_id: name for name, sample_id in ids.items()}

    def of(self, names: set[str]) -> set[int]:
        return {self.ids[name] for name in names}

    def names(self, ids: Any) -> set[str]:
        return {self.by_id[sample_id] for sample_id in ids}


async def add(r: Regions, dataset_id: int, samples: list[dict[str, Any]]) -> None:
    await ok(await post(r, f'/api/datasets/{dataset_id}/samples', {'samples': samples}), dataset_id)


async def stored_ids(r: Regions, dataset_id: int) -> dict[str, int]:
    rows = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', dataset_id)
    return {row['filepath'][: -len('.jpg')]: row['id'] for row in rows}


async def plant(r: Regions) -> Planted:
    """The plain dataset: a sample at each of PLACES, with its name, a rank and its group; a vector for search."""
    await r.dataset(PLAIN)
    samples = []
    for rank, (name, point) in enumerate(PLACES.items(), start=1):
        sample: dict[str, Any] = {
            'filepath': f'{name}.jpg',
            'metadata': {'plot': name, 'rank': rank, 'grp': 'a' if name in GROUP_A else 'b'},
        }
        if point is not None:
            sample['location'] = {'type': 'Point', 'coordinates': list(point)}
        samples.append(sample)
    await add(r, PLAIN, samples)
    rows = await r.database.fetch('SELECT filepath, lon, lat FROM samples WHERE dataset_id = $1', PLAIN)
    stored = {row['filepath'][: -len('.jpg')]: None if row['lon'] is None else (row['lon'], row['lat']) for row in rows}
    assert stored == PLACES
    planted = Planted(PLAIN, await stored_ids(r, PLAIN))
    vectors = [
        {'sample_id': planted.ids[name], 'vector': [1.0, rank / 100, 0.0, 0.0], 'field_name': 'clip'}
        for rank, name in enumerate(PLACES, start=1)
    ]
    assert (await post(r, f'/api/datasets/{PLAIN}/embeddings', vectors)).is_success
    return planted


# --- What samples/query, map_extent and map_points answer ---


async def on_the_map(
    r: Regions,
    p: Planted,
    node: Any,
    zoom: Optional[int] = None,
    bbox: Optional[list[float]] = None,
) -> set[str]:
    """The samples map_points answers as points, every one asked for (`points`)."""
    body: dict[str, Any] = {'filters': node, 'points': True}
    if zoom is not None:
        body.update(zoom=zoom, bbox=bbox)
    answer = await ok(await post(r, f'/api/datasets/{p.dataset_id}/map_points', body), (node, zoom, bbox))
    ids = [item['id'] for item in answer['items'] if item.get('id') is not None]
    assert len(ids) == len(answer['items']) == len(set(ids)), answer
    return p.names(ids)


async def extent(r: Regions, p: Planted, node: Any) -> dict[str, Any]:
    return await ok(await post(r, f'/api/datasets/{p.dataset_id}/map_extent', {'filters': node}), node)


async def assert_routes(r: Regions, p: Planted, node: Any, names: set[str], where: Any) -> None:
    """samples/query (its count agreeing), map_extent and map_points keep exactly the samples `names`."""
    assert p.names(await selected(r, node, p.dataset_id)) == names, ('samples/query', where)
    assert await extent(r, p, node) == pytest.approx(box_of(names)), ('map_extent', where)
    assert await on_the_map(r, p, node) == names & LOCATED, ('map_points, every point', where)
    across = await on_the_map(r, p, node, zoom=6, bbox=VIEW_ACROSS)
    assert across == names & FIJI, ('map_points, the view across the antimeridian', where)


async def assert_alone_and_anded(r: Regions, p: Planted, node: dict[str, Any], names: set[str], where: Any) -> None:
    await assert_routes(r, p, node, names, (where, 'alone'))
    await assert_routes(r, p, both(node, GRP_A), names & GROUP_A, (where, 'AND grp = "a"'))


# --- 1.1 ---


def test_a_polygon_drawn_across_180_holds_the_samples_on_both_sides(regions_world):
    """Contract 1.1 (and 1.2's first sentence): a polygon from 176 to 183 holds 177.4 and -178.2 (181.8 in its frame),
    through samples/query and its count, map_extent and map_points, alone and ANDed with another filter. Main holds
    only the samples west of the antimeridian."""
    check_expectations(CROSSING + ORDINARY[:2])

    async def scenario(r):
        p = await plant(r)
        for label, polygon, names in ORDINARY[:2]:  # the control: the halves as ordinary polygons
            await assert_alone_and_anded(r, p, place('geo_polygon', polygon), names, label)
        for label, polygon, names in CROSSING:
            await assert_alone_and_anded(r, p, place('geo_polygon', polygon), names, label)

    regions_world(scenario)


# --- 1.2 ---


def test_the_same_shape_on_another_copy_of_the_world_holds_the_same_samples(regions_world):
    """Contract 1.2: a polygon lying wholly on another copy of the world (536 to 543, -184 to -177, two turns east)
    holds the same samples as the same shape moved onto the first copy, which are the samples its two halves hold
    written as ordinary polygons. Main holds none on the copy east, and only the east side on the copy west."""
    check_expectations(OTHER_COPIES)

    async def scenario(r):
        p = await plant(r)
        halves = p.names(await selected(r, place('geo_polygon', WEST_HALF), PLAIN)) | p.names(
            await selected(r, place('geo_polygon', EAST_HALF), PLAIN),
        )
        assert halves == FIJI  # the control: what the two halves hold today
        for label, polygon, names in OTHER_COPIES:
            await assert_alone_and_anded(r, p, place('geo_polygon', polygon), names, label)
        for turns in (1, -1, 2):
            moved = await selected(r, place('geo_polygon', turned(ACROSS, turns)), PLAIN)
            assert p.names(moved) == p.names(await selected(r, place('geo_polygon', ACROSS), PLAIN)) == halves, turns

    regions_world(scenario)


# --- 1.3 and 5.4: pins ---


# The dataset with objects: its images, and one object under each (its name, its own location).
IMAGES: dict[str, Optional[Point]] = {
    'iw': (177.4, -17.6),
    'ie': (-178.2, -16.2),
    'ifar': (100.0, 20.0),
    'inone': None,
}
OBJECT_OF: dict[str, tuple[str, Optional[Point]]] = {
    'iw': ('ow', None),
    'ie': ('oe', None),
    'ifar': ('ofar', None),
    'inone': ('ox', (-179.5, -18.1)),  # an object east of the antimeridian under an image with no location
}
ROWS: dict[str, Optional[Point]] = {**IMAGES, **{name: point for name, point in OBJECT_OF.values()}}
PARENT = {name: image for image, (name, _) in OBJECT_OF.items()}


async def plant_objects(r: Regions) -> Planted:
    await r.dataset(OBJECTS)
    images: list[dict[str, Any]] = []
    for name, point in IMAGES.items():
        image: dict[str, Any] = {'filepath': f'{name}.jpg', 'metadata': {'plot': name}}
        if point is not None:
            image['location'] = {'type': 'Point', 'coordinates': list(point)}
        images.append(image)
    await add(r, OBJECTS, images)
    image_ids = await stored_ids(r, OBJECTS)
    objects: list[dict[str, Any]] = []
    for image_name, (name, point) in OBJECT_OF.items():
        child: dict[str, Any] = {
            'filepath': f'{name}.jpg',
            'media_type': 'object',
            'metadata': {'parent_id': image_ids[image_name], 'label': 'crate'},
        }
        if point is not None:
            child['location'] = {'type': 'Point', 'coordinates': list(point)}
        objects.append(child)
    await add(r, OBJECTS, objects)
    planted = Planted(OBJECTS, await stored_ids(r, OBJECTS))
    rows = await r.database.fetch('SELECT id, parent_id FROM samples WHERE dataset_id = $1', OBJECTS)
    parents = {planted.by_id[row['id']]: row['parent_id'] for row in rows if row['parent_id'] is not None}
    assert parents == {name: planted.ids[image] for name, image in PARENT.items()}
    return planted


def grids(inside: set[str]) -> dict[str, set[str]]:
    """What each grid keeps for a polygon holding the rows `inside`: an image inside or holding an object inside, an
    object inside or on an image inside; and for its exclusion every other row of the grid."""
    images = {image for image in IMAGES if image in inside or OBJECT_OF[image][0] in inside}
    objects = {name for name in PARENT if name in inside or PARENT[name] in inside}
    return {
        'image': images,
        'object': objects,
        'image, excluded': set(IMAGES) - images,
        'object, excluded': set(PARENT) - objects,
    }


async def assert_grids(r: Regions, p: Planted, polygon: Polygon, inside: set[str], where: Any) -> None:
    assert by_the_rule(polygon, ROWS) == inside, f"the test's own expectation for {where}"
    expected = grids(inside)
    node = place('geo_polygon', polygon)
    for kind in ('image', 'object'):
        kept = p.names(await selected(r, grain([node], kind), OBJECTS))
        assert kept == expected[kind], (where, f'the {kind} grid')
        dropped = p.names(await selected(r, excluded([node], kind), OBJECTS))
        assert dropped == expected[f'{kind}, excluded'], (where, f'the {kind} grid, the polygon excluded')


async def assert_every_reader(
    r: Regions,
    p: Planted,
    node: dict[str, Any],
    names: set[str],
    version: int,
    monkeypatch: Any,
) -> None:
    """Every reader of filters keeps exactly the samples `names` for the filter (2.1)."""
    where = json.dumps(node)
    ids = p.of(names)
    assert await selected(r, node, PLAIN) == ids, ('samples/query', where)
    # An export's pages, as the export dialog asks them.
    exported: list[int] = []
    after: Optional[int] = None
    while True:
        body: dict[str, Any] = {'limit': 2, 'filters': node, 'fields': 'filepath', 'slim': True, 'skip_count': True}
        if after is not None:
            body['after_id'] = after
        items = (await ok(await post(r, f'/api/datasets/{PLAIN}/samples/query', body), where))['items']
        exported += [item['id'] for item in items]
        if len(items) < 2:
            break
        after = items[-1]['id']
    assert sorted(exported) == sorted(ids), ('the export', where)
    plots, ranks = sorted(names), [list(PLACES).index(name) + 1 for name in names]
    aggregations = [{'field': 'plot', 'op': 'count'}, {'field': 'plot', 'op': 'values'}]
    body = await ok(await post(r, f'/api/datasets/{PLAIN}/aggregate', {'filters': node, 'aggregations': aggregations}))
    count, values = body['results']
    assert (count['value'], sorted(values['value'])) == (len(names), plots), ('aggregate', where)
    body = await ok(await post(r, f'/api/datasets/{PLAIN}/histogram', {'field': 'rank', 'filters': node, 'bins': 20}))
    assert body['total'] == len(names), ('histogram', where)
    [facet] = await ok(await post(r, f'/api/datasets/{PLAIN}/field-stats', {'filters': node, 'fields': ['rank']}))
    assert (facet['min'], facet['max']) == (min(ranks), max(ranks)), ('field-stats', where, facet)
    listed = await ok(
        await post(r, f'/api/datasets/{PLAIN}/fields/plot/autocomplete', {'filters': node, 'limit': 1000}),
    )
    assert sorted(item['value'] for item in listed['values']) == plots, ('autocomplete', where)
    assert await on_the_map(r, p, node) == names & LOCATED, ('map_points', where)
    assert await extent(r, p, node) == pytest.approx(box_of(names)), ('map_extent', where)
    token = await ok(await post(r, f'/api/datasets/{PLAIN}/selection_token', {'filters': node}))
    assert token['count'] == len(names), ('selection_token', where)
    in_token = {'field': 'id', 'operator': 'in_selection', 'value': token['token']}
    assert await selected(r, in_token, PLAIN) == ids, ('in_selection', where)
    search = {'dataset_id': PLAIN, 'vector': [1.0, 0.0, 0.0, 0.0], 'field_name': 'clip', 'limit': 1000}
    found = await ok(await post(r, '/api/search', {**search, 'filters': node}))
    assert {item['id'] for item in found} == ids, ('/api/search', where)
    base = f'/api/datasets/{PLAIN}/versions/{version}'
    rows = await ok(await post(r, f'{base}/samples/query', {'filters': node, 'limit': 1000}))
    assert {item['id'] for item in rows['items']} == ids, ('a version: samples/query', where)
    saved = await ok(
        await post(r, f'{base}/aggregate', {'filters': node, 'aggregations': [{'field': 'plot', 'op': 'values'}]}),
    )
    assert sorted(saved['results'][0]['value']) == plots, ('a version: aggregate', where)
    # A bulk tag by the filter, run as the job runner would: its count, and the rows it tagged.
    tag = f'bulk-{uuid4().hex[:12]}'
    tagged = await post(r, f'/api/datasets/{PLAIN}/samples/bulk-tag', {'filters': node, 'add_tags': [tag]})
    assert tagged.status_code in (200, 202), tagged.text
    if tagged.status_code == 202:
        assert (await finish(r.database))['result']['updated_count'] == len(names), ('bulk tag', where)
    else:
        assert tagged.json()['updated_count'] == len(names), ('bulk tag', where)
    rows = await r.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 AND $2 = ANY(tags)', PLAIN, tag)
    assert {row['id'] for row in rows} == ids, ('bulk tag: the rows tagged', where)
    # The SDK, as a script writes the filter.
    sdk(r, asyncio.get_running_loop(), monkeypatch)
    polygon, operator = node['value'], node['operator']
    condition = geo_within(polygon) if operator == 'geo_polygon' else Condition('location', operator, polygon)

    def through_the_sdk() -> tuple[set[int], int]:
        view = Dataset(f'dataset-{PLAIN}').samples[condition]
        return set(view.ids()), view.count()

    assert await asyncio.to_thread(through_the_sdk) == (ids, len(names)), ('the SDK', where)
    # The assistant: a saved view holding the filter, opened by open_saved_view, its samples by get_samples.
    slug = await saved_view(r, node, f'assistant {tag}')
    await open_door(r)
    opened = await result(ADMIN, 'open_saved_view', {'slug': slug})
    assert opened['count'] == len(names), ('the assistant: open_saved_view', where, opened)
    assert await view_ids(ADMIN, opened['view']) == ids, ('the assistant: get_samples', where)


def stage_of(node: dict[str, Any]) -> dict[str, Any]:
    """The stage the app saves for a drawn polygon (frontend/lib/filter-utils.ts getMapStage, and the chip's toggle)."""
    excluding = node['operator'] == 'geo_polygon_exclude'
    return {'type': 'map_region', 'description': 'Exclude Map Region' if excluding else 'Map Region', 'filter': node}


async def saved_view(r: Regions, node: dict[str, Any], name: str) -> str:
    made = await ok(
        await post(r, f'/api/datasets/{PLAIN}/views', {'name': name, 'content': {'viewStages': [stage_of(node)]}}),
    )
    return made['slug']


async def version_of(r: Regions) -> int:
    await ok(await post(r, f'/api/datasets/{PLAIN}/versions', {'name': 'as planted'}))
    return await r.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', PLAIN)


def test_a_polygon_within_minus_180_to_180_holds_what_it_holds_today(regions_world, monkeypatch):
    """Contract 1.3 and 5.4, a pin that passes on main: a polygon whose longitudes all lie from -180 to 180 holds what
    it holds today, through every route: samples/query and its count, map_extent and map_points, alone and ANDed with
    another filter, as geo_polygon and geo_polygon_exclude; the image and the object grid of a dataset with objects; and
    every reader of filters. A polygon with points at 170 and -170 stays the shape through longitude 0."""
    check_expectations(ORDINARY)

    async def scenario(r):
        p = await plant(r)
        for label, polygon, names in ORDINARY:
            await assert_alone_and_anded(r, p, place('geo_polygon', polygon), names, label)
            await assert_alone_and_anded(r, p, place('geo_polygon_exclude', polygon), EVERY - names, (label, 'out'))
        o = await plant_objects(r)
        await assert_grids(r, o, WEST_HALF, {'iw'}, 'the west half')
        await assert_grids(r, o, EAST_HALF, {'ie', 'ox'}, 'the east half')
        version = await version_of(r)
        await assert_every_reader(r, p, place('geo_polygon', WEST_HALF), WEST, version, monkeypatch)
        await assert_every_reader(r, p, place('geo_polygon_exclude', WEST_HALF), EVERY - WEST, version, monkeypatch)

    regions_world(scenario)


# --- 1.4 ---


def test_the_exclusion_keeps_exactly_the_samples_the_polygon_does_not_hold(regions_world):
    """Contract 1.4: geo_polygon_exclude of a polygon across the antimeridian (on any copy of the world, and 360 degrees
    wide or more) removes exactly the samples geo_polygon of the same points holds, and keeps every other sample, the
    one with no location included; alone and ANDed with another filter, through every route. Main keeps the samples
    east of the antimeridian a polygon across it holds."""
    cases = CROSSING + OTHER_COPIES + WIDE
    check_expectations(cases + ORDINARY[:1])

    async def scenario(r):
        p = await plant(r)
        control = ORDINARY[0]  # the west half, an ordinary polygon
        await assert_alone_and_anded(r, p, place('geo_polygon_exclude', control[1]), EVERY - control[2], control[0])
        for label, polygon, names in cases:
            await assert_alone_and_anded(r, p, place('geo_polygon_exclude', polygon), EVERY - names, label)
            kept = await selected(r, place('geo_polygon', polygon), PLAIN)
            dropped = await selected(r, place('geo_polygon_exclude', polygon), PLAIN)
            assert not kept & dropped and kept | dropped == p.of(EVERY), label

    regions_world(scenario)


# --- 1.5 ---


def test_a_polygon_360_degrees_wide_or_more_holds_every_sample_inside_its_shape(regions_world):
    """Contract 1.5: a polygon 360 degrees wide or more is not an error; it holds every sample that lies inside the
    shape under rule 1 (a rectangle 360, 400 and 720 degrees wide holds every sample within its latitudes; a triangle
    wider than the world at its base holds only the samples inside it). Main holds those whose own longitude is
    inside."""
    check_expectations(WIDE)

    async def scenario(r):
        p = await plant(r)
        for label, polygon, names in WIDE:
            await assert_alone_and_anded(r, p, place('geo_polygon', polygon), names, label)

    regions_world(scenario)


# --- 1.5's limit, and 1.6 ---


async def answered(r: Regions, path: str, body: dict[str, Any]) -> httpx.Response:
    """The server's answer as a client sees it, an unhandled error as its 500 (not raised in the test)."""
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        return await client.post(path, json=body, headers={'Authorization': f'Bearer {r.tokens[ADMIN]}'})


async def at_once(r: Regions, p: Planted, node: dict[str, Any]) -> dict[str, tuple[int, Any]]:
    """What samples/query, map_extent and map_points answer for the filter: each one's status, and with a 200 the
    samples it keeps by name (with samples/query's count), the box, the points; otherwise its text."""
    base = f'/api/datasets/{p.dataset_id}'
    listed = await answered(r, f'{base}/samples/query', {'limit': 1000, 'filters': node})
    box = await answered(r, f'{base}/map_extent', {'filters': node})
    points = await answered(r, f'{base}/map_points', {'filters': node, 'points': True})
    found: dict[str, tuple[int, Any]] = {}
    for route, response in (('samples/query', listed), ('map_extent', box), ('map_points', points)):
        if response.status_code != 200:
            found[route] = (response.status_code, response.text)
        elif route == 'samples/query':
            body = response.json()
            found[route] = (200, (p.names(item['id'] for item in body['items']), body['total']))
        elif route == 'map_extent':
            found[route] = (200, response.json())
        else:
            found[route] = (200, p.names(item['id'] for item in response.json()['items']))
    return found


def answering(names: set[str]) -> dict[str, tuple[int, Any]]:
    """at_once's answer when the filter keeps exactly the samples `names`."""
    return {
        'samples/query': (200, (names, len(names))),
        'map_extent': (200, pytest.approx(box_of(names))),
        'map_points': (200, names & LOCATED),
    }


async def assert_keeps_at_once(r: Regions, p: Planted, value: Any, kept: set[str], kept_out: set[str]) -> None:
    """geo_polygon of the value keeps `kept` and geo_polygon_exclude `kept_out`, alone and ANDed with another filter."""
    for operator, names in (('geo_polygon', kept), ('geo_polygon_exclude', kept_out)):
        node = place(operator, value)
        for form, asked, expected in (
            ('alone', node, names),
            ('AND grp = "a"', both(node, GRP_A), names & GROUP_A),
        ):
            found = await at_once(r, p, asked)
            assert found == answering(expected), (operator, form, f'expected {sorted(expected)}, answered {found}')


def refusal(found: dict[str, tuple[int, Any]], words: tuple[str, ...]) -> list[str]:
    """What is wrong with the answers where each should be a 400 whose text holds every one of `words`."""
    wrong = []
    for route, (status, text) in found.items():
        if status != 400 or not all(word in str(text).lower() for word in words):
            wrong.append(f'{route}: {status} {str(text)[:200]}')
    return wrong


async def assert_refused(r: Regions, p: Planted, value: Any, words: tuple[str, ...]) -> None:
    for operator in ('geo_polygon', 'geo_polygon_exclude'):
        node = place(operator, value)
        for form, asked in (('alone', node), ('AND grp = "a"', both(node, GRP_A))):
            wrong = refusal(await at_once(r, p, asked), words)
            assert not wrong, (operator, form, wrong)


SAYS_WHAT_IT_TAKES = ('geo_polygon', 'longitude', 'latitude')
NAMES_THE_LIMIT = ('60', '21600', '21,600')  # 60 turns, or 21,600 degrees

# 1.6.1: no polygon, as today.
NO_POLYGON: list[tuple[str, Any]] = [
    ('null', None),
    ('an empty list', []),
    ('one short point', [[1]]),
    ('two numbers', [1, 2]),
    ('two points', [[176, -20], [180, -14]]),
    ('two points across 180', [[176, -20], [183, -14]]),
]


@pytest.mark.parametrize(('label', 'value'), NO_POLYGON, ids=[case[0] for case in NO_POLYGON])
def test_null_and_a_list_of_fewer_than_three_entries_keep_every_sample(regions_world, label, value):
    """Contract 1.6.1, a pin that passes on main: null and a list of fewer than three entries are no polygon; every
    sample is kept, by the exclusion too, through samples/query, map_extent and map_points."""

    async def scenario(r):
        p = await plant(r)
        await assert_keeps_at_once(r, p, value, EVERY, EVERY)

    regions_world(scenario)


# 1.6.2: a closed ring of fewer than three points. Main: an unhandled error.
SHORT_RINGS: list[tuple[str, Any]] = [
    ('three equal points', [[0, 0], [0, 0], [0, 0]]),
    ('two points and the first again', [[176, -20], [180, -14], [176, -20]]),
    ('two points across 180 and the first again', [[176, -20], [183, -14], [176, -20]]),
]


@pytest.mark.parametrize(('label', 'value'), SHORT_RINGS, ids=[case[0] for case in SHORT_RINGS])
def test_a_ring_with_fewer_than_three_points_besides_its_closing_point_keeps_every_sample(regions_world, label, value):
    """Contract 1.6.2: a ring that has fewer than three points once its closing point (a last point equal to the
    first) is set aside is no polygon either: every sample, for geo_polygon and geo_polygon_exclude. Main ends each
    in an unhandled error."""

    async def scenario(r):
        p = await plant(r)
        await assert_keeps_at_once(r, p, value, EVERY, EVERY)

    regions_world(scenario)


# 1.6.3: as today. (what is asked, the polygon, what geo_polygon keeps)
CLOSED_AND_LONG_POINTS: list[tuple[str, Optional[Polygon], set[str]]] = [
    ('an open ring answers as the same ring closed', None, set()),
    ('points with a third entry', [[176, -20, 0], [180, -20, 5], [180, -14, 9], [176, -14, 1]], WEST),
    (
        'points with a third and a fourth entry',
        [[176, -20, 0, 1], [180, -20, 5, 1], [180, -14, 9, 1], [176, -14, 1, 1]],
        WEST,
    ),
    ('points of three numbers on a line', [[1, 2, 3], [4, 5, 6], [7, 8, 9]], set()),
]


@pytest.mark.parametrize(
    ('label', 'value', 'kept'),
    CLOSED_AND_LONG_POINTS,
    ids=[case[0] for case in CLOSED_AND_LONG_POINTS],
)
def test_an_open_ring_is_closed_and_the_entries_after_a_points_latitude_are_ignored(regions_world, label, value, kept):
    """Contract 1.6.3, a pin that passes on main: an open ring is closed with its first point (it answers what the
    same ring closed answers, within -180 to 180 and across 180); a point's first two entries are its longitude and
    latitude and further entries are ignored (points of three numbers on a line are a polygon with no inside)."""

    async def scenario(r):
        p = await plant(r)
        if value is not None:
            await assert_keeps_at_once(r, p, value, kept, EVERY - kept)
            return
        for polygon in (WEST_HALF, ACROSS, turned(ACROSS, -1)):
            for operator in ('geo_polygon', 'geo_polygon_exclude'):
                open_ring = await at_once(r, p, place(operator, polygon))
                assert open_ring == await at_once(r, p, place(operator, closed(polygon))), (polygon, operator)
                assert all(status == 200 for status, _ in open_ring.values()), (polygon, operator, open_ring)

    regions_world(scenario)


def in_text(polygon: Polygon) -> list[list[str]]:
    return [[str(lon), str(lat)] for lon, lat in polygon]


# 1.6.4: (what is asked, the value, what geo_polygon keeps)
TEXT_COORDINATES: list[tuple[str, Any, set[str]]] = [
    # Within -180 to 180: as the numbers answer today (a pin that passes on main).
    ('text within -180 to 180', in_text(WEST_HALF), WEST),
    (
        'decimal text within -180 to 180',
        [['176.0', '-20.5'], ['180', '-20.5'], ['180.0', '-14'], ['176', '-14.0']],
        WEST,
    ),
    # Past 180: across the antimeridian (main reads them as a polygon that does not cross).
    ('text past 180', in_text(ACROSS), FIJI),
    ('a longitude "183" among numbers', [[176, -20], ['183', -20], [183, -14], [176, -14]], FIJI),
    ('text one turn west', in_text(turned(ACROSS, -1)), FIJI),
]


@pytest.mark.parametrize(('label', 'value', 'kept'), TEXT_COORDINATES, ids=[case[0] for case in TEXT_COORDINATES])
def test_a_coordinate_written_as_text_is_read_as_that_number(regions_world, label, value, kept):
    """Contract 1.6.4: a coordinate is a number, or text that reads as one, and text is read as that number, so a
    longitude "183" is past 180 and makes the polygon one across the antimeridian. Text within -180 to 180 answers as
    the numbers do today (those cases pass on main); main reads text past 180 as a polygon that does not cross."""
    assert by_the_rule([[float(lon), float(lat)] for lon, lat in value], PLACES) == kept, "the test's own expectation"

    async def scenario(r):
        p = await plant(r)
        await assert_keeps_at_once(r, p, value, kept, EVERY - kept)

    regions_world(scenario)


# 1.6.5: refused. Main: an unhandled error, or every sample for an object.
REFUSED: list[tuple[str, Any]] = [
    ('a text', 'abc'),
    ('a number', 5),
    ('true', True),
    ('an empty object', {}),
    ('an object', {'a': 1}),
    (
        'a GeoJSON polygon',
        {'type': 'Polygon', 'coordinates': [[[176, -20], [183, -20], [183, -14], [176, -14], [176, -20]]]},
    ),
    ('numbers, not points', [1, 2, 3]),
    ('a point with one entry', [[176, -20], [183], [183, -14]]),
    ('a number among the points', [[176, -20], 5, [183, -14]]),
    ('null among the points', [[176, -20], None, [183, -14]]),
    ('text coordinates', [['a', 'b'], ['c', 'd'], ['e', 'f']]),
    ('a text coordinate', [[1, 2], [3, 'x'], [5, 6]]),
    ('a text coordinate, across 180', [[176, -20], [183, 'x'], [183, -14]]),
    ('true as a coordinate', [[176, -20], [True, -20], [183, -14]]),
    ('null as a coordinate', [[176, -20], [None, -20], [183, -14]]),
    ('"NaN" as a coordinate', [[176, -20], ['NaN', -20], [183, -14]]),
]


@pytest.mark.parametrize(('label', 'value'), REFUSED, ids=[case[0] for case in REFUSED])
def test_any_other_value_is_refused_with_a_400_naming_geo_polygon(regions_world, label, value):
    """Contract 1.6.5 and 1.6.6: any other value (one that is not a list, a list of three or more entries one of which
    is not a point, a coordinate that is not a finite number) is refused with a 400 whose message names geo_polygon and
    says what it takes, a list of [longitude, latitude] points; by samples/query, map_extent and map_points, for
    geo_polygon and geo_polygon_exclude, alone and ANDed with another filter. Main ends these in an unhandled error,
    except an object, which keeps every sample."""

    async def scenario(r):
        p = await plant(r)
        await assert_refused(r, p, value, SAYS_WHAT_IT_TAKES)

    regions_world(scenario)


def test_a_polygon_wider_than_60_turns_of_the_globe_is_refused_with_a_400(regions_world):
    """Contract 1.5's limit (amended 2026-11-24, waiting for the owner's word, so a test of its own): a polygon exactly
    60 turns wide (21,600 degrees) is answered, holding every sample within its latitudes; one wider (21,601 degrees,
    and one with a longitude of 1e15) is refused with a 400 that says so, by samples/query, map_extent and map_points.
    Main answers the wider ones too."""
    sixty_turns = rect(-10800, -25, 10800, 25)
    check_expectations([('60 turns wide', sixty_turns, FIJI | {'zero', 'hundred'})])

    async def scenario(r):
        p = await plant(r)
        await assert_keeps_at_once(r, p, sixty_turns, FIJI | {'zero', 'hundred'}, {'minus', 'none'})
        for label, polygon in (
            ('21,601 degrees wide', rect(-10800, -25, 10801, 25)),
            ('a longitude of 1e15', rect(176, -20, 1e15, -14)),
        ):
            for operator in ('geo_polygon', 'geo_polygon_exclude'):
                found = await at_once(r, p, place(operator, polygon))
                wrong = [
                    f'{route}: {status} {str(text)[:200]}'
                    for route, (status, text) in found.items()
                    if status != 400 or not any(limit in str(text) for limit in NAMES_THE_LIMIT)
                ]
                assert not wrong, (label, operator, wrong)

    regions_world(scenario)


# --- 2.1 ---


async def represented(r: Regions, node: Any, zoom: int, bbox: Optional[list[float]]) -> tuple[int, int]:
    """How many samples the map shows in a view (each bubble's count plus each point), and how many bubbles."""
    body = {'filters': node, 'zoom': zoom, 'bbox': bbox}
    answer = await ok(await post(r, f'/api/datasets/{DENSE}/map_points', body), (node, zoom, bbox))
    bubbles = [item for item in answer['items'] if item.get('id') is None]
    points = [item for item in answer['items'] if item.get('id') is not None]
    return sum(bubble['count'] for bubble in bubbles) + len(points), len(bubbles)


def test_every_reader_of_filters_finds_the_samples_on_both_sides(regions_world, monkeypatch):
    """Contract 2.1: the rule holds wherever a filter is read: the grid and its count, an export, aggregate, the
    histogram, the sidebar's field-stats and values, map_points (points and bubbles), map_extent, Select All's token,
    /api/search, a bulk tag, a version, the SDK and the assistant's tools; for geo_polygon and geo_polygon_exclude.
    Main misses the samples east of the antimeridian in each."""
    check_expectations(CROSSING[:1])

    async def scenario(r):
        p = await plant(r)
        version = await version_of(r)
        for node, names in (
            (place('geo_polygon', ACROSS), FIJI),
            (place('geo_polygon_exclude', ACROSS), EVERY - FIJI),
            (place('geo_polygon', turned(ACROSS, -1)), FIJI),
        ):
            await assert_every_reader(r, p, node, names, version, monkeypatch)
        # Bubbles: 60 samples each side of the antimeridian make one bubble on it in a view across it at zoom 6
        # (docs/MAP_REQUIREMENTS.md #13); 5 far away.
        await r.dataset(DENSE)
        dense = [((179.5, -17.0), 'west', 60), ((-179.5, -17.0), 'east', 60), ((10.0, 10.0), 'far', 5)]
        await add(
            r,
            DENSE,
            [
                {'filepath': f'{name}-{k}.jpg', 'location': {'type': 'Point', 'coordinates': list(point)}}
                for point, name, count in dense
                for k in range(count)
            ],
        )
        across = (6, [175.0, -25.0, 185.0, -10.0])  # the view across the antimeridian at zoom 6
        world = (2, None)
        assert await represented(r, None, *across) == (120, 1)  # the control: no filter, one bubble on the antimeridian
        inside, outside = place('geo_polygon', ACROSS), place('geo_polygon_exclude', ACROSS)
        assert await represented(r, inside, *across) == (120, 1), 'the polygon, the view across'
        assert (await represented(r, inside, *world))[0] == 120, 'the polygon, the whole world'
        assert await represented(r, outside, *across) == (0, 0), 'the exclusion, the view across'
        assert (await represented(r, outside, *world))[0] == 5, 'the exclusion, the whole world'
        box = await ok(await post(r, f'/api/datasets/{DENSE}/map_extent', {'filters': place('geo_polygon', ACROSS)}))
        assert box == pytest.approx({'w': 179.5, 's': -17.0, 'e': -179.5, 'n': -17.0, 'count': 120})

    regions_world(scenario)


def test_on_a_dataset_with_objects_the_polygon_across_180_holds_through_the_images_and_their_objects(regions_world):
    """Contract 2.1's last sentence and 5.2: on a dataset with objects the rule holds through the link between images
    and objects, in the image grid and the object grid, for the polygon and its exclusion: an image east of the
    antimeridian, and an image whose object is, are inside a polygon across it, and so are their objects."""

    async def scenario(r):
        o = await plant_objects(r)
        await assert_grids(r, o, WEST_HALF, {'iw'}, 'the control: the west half')
        await assert_grids(r, o, ACROSS, {'iw', 'ie', 'ox'}, '176 to 183')
        await assert_grids(r, o, turned(ACROSS, 1), {'iw', 'ie', 'ox'}, '176 to 183, one turn east')
        await assert_grids(r, o, NARROW_ACROSS, {'ox'}, '179 to 181')

    regions_world(scenario)


# --- 2.4 ---


def test_a_polygon_across_180_saved_in_a_view_finds_the_same_samples_when_opened_again(regions_world, monkeypatch):
    """Contract 2.4: a polygon across the antimeridian saved in a view keeps its longitudes on the way (read back by its
    slug and in the dataset's list exactly as saved), and finds the same samples when it is opened again: by its stage's
    filter, by the SDK's load_view and by the assistant's open_saved_view. Its text form is today's for a polygon (none:
    the address carries it encoded)."""
    check_expectations(CROSSING[:1] + OTHER_COPIES[:2] + ORDINARY[:1])

    async def scenario(r):
        p = await plant(r)
        await open_door(r)
        sdk(r, asyncio.get_running_loop(), monkeypatch)
        cases = [
            ('the control: the west half', place('geo_polygon', WEST_HALF), WEST),
            ('176 to 183', place('geo_polygon', ACROSS), FIJI),
            ('176 to 183, excluded', place('geo_polygon_exclude', ACROSS), EVERY - FIJI),
            ('-184 to -177', place('geo_polygon', turned(ACROSS, -1)), FIJI),
            ('536 to 543, excluded', place('geo_polygon_exclude', turned(ACROSS, 1)), EVERY - FIJI),
        ]
        formatted = await ok(await post(r, f'/api/datasets/{PLAIN}/filter_query/format', {'filters': [cases[0][1]]}))
        for name, node, names in cases:
            slug = await saved_view(r, node, name)
            read = await ok(await r.call(ADMIN, 'GET', f'/api/views/{slug}'))
            assert read['content'] == {'viewStages': [stage_of(node)]}, name
            views = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{PLAIN}/views'))
            [listed] = [view for view in views if view['slug'] == slug]
            assert listed['content'] == read['content'], name
            stage_filter = read['content']['viewStages'][0]['filter']
            assert p.names(await selected(r, stage_filter, PLAIN)) == names, (name, 'its stage')

            def loaded(view_name: str = name) -> set[int]:
                return set(Dataset(f'dataset-{PLAIN}').load_view(view_name).ids())

            assert p.names(await asyncio.to_thread(loaded)) == names, (name, "the SDK's load_view")
            opened = await result(ADMIN, 'open_saved_view', {'slug': slug})
            assert opened['count'] == len(names), (name, "the assistant's open_saved_view", opened)
            assert p.names(await view_ids(ADMIN, opened['view'])) == names, (name, "the assistant's get_samples")
            again = await ok(await post(r, f'/api/datasets/{PLAIN}/filter_query/format', {'filters': [node]}))
            assert again == formatted, (name, 'its text form')

    regions_world(scenario)
