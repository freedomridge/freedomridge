"""Named regions (a loaded boundary set's countries, states and groups) and a filter for the samples inside one.

Written from the contract, not from the code, and tested where a user meets it: the loader's command line
(backend.regions.main), the HTTP API signed in with real API tokens, and the filter in every reader of filters. The
rule, in short:

- Loading a Natural Earth "admin 1" file makes a `state` per feature (key: a well-formed iso_3166_2 no other feature
  has, else adm1_code; name: name_en, else name, else the key), a `country` per country (key: a two-letter iso_a2,
  else adm0_a3; name: the admin text most of its features give, distinct from every other country's) and a `group`
  per country and non-empty, undamaged `region` text (key `<country key>:<slug>`). Keys are canonical (upper case, a
  group's slug lower case) and are accepted in any case. Each region has a bounding box [west, south, east, north],
  west > east across the antimeridian, and an exact geometry. Without --no-verify a file that isn't the pinned one is
  refused; loading a set again replaces it and reports the keys it no longer has.
- POST /api/admin/regions/load and GET /api/admin/regions are for admins; GET /api/regions (search, in the contract's
  order) and GET /api/regions/{key} for any signed-in user.
- {"field": "location", "operator": "geo_region" | "geo_region_exclude", "value": key} keeps, or removes, the samples
  inside the region, exactly (islands, holes, borders included), wherever a filter is read; an unknown key, a bad
  value or another field is a 400.
- `location within "KEY"` / `location not within "KEY"` in the filter query language, a region's name accepted in
  place of its key; `within` and `location` stay usable as ordinary names.
- Migration 0034 adds empty region tables and changes nothing else; a relayed follower search refuses a region filter.

The boundary file is this module's own synthetic geometry (FEATURES, written to a temporary file per test): four
countries, thirteen states, four groups. Its sample points and what each is inside (POINTS) are checked against
PostGIS directly in test_the_fixture_is_valid_geojson_and_postgis_agrees_with_the_expected_points, which needs no
region feature. Each test runs on a database of its own, made from this checkout's schema.sql, so the loader can be
pointed at it by its URL (backend.database.DATABASE_URL, the DATABASE_URL environment variable, and the loader
module's own DATABASE_URL if it keeps one).

Where the contract leaves something open, these tests read it this way: the loaded sha256 is the loaded file's; a
region read without ?outline=true has no outline; a lookup by key needs a signed-in user like the search; with no set
loaded a region filter's 400 says no boundary set is loaded (its prefix isn't asserted); NOT around a region keeps the
samples it doesn't, those with no location included (the language's NOT is a complement), the same as the exclude;
an ambiguous or unknown name's 400 points at the name. The POST that loads a set is checked by its answer and the
queued job only: no job runner runs in these tests, so nothing is downloaded.
"""

import asyncio
import hashlib
import importlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock
from urllib.parse import quote, urlparse

import asyncpg
import httpx
import pytest

from backend import database as database_module
from backend import main
from backend.database import PLANNING, Database, init_connection
from backend.routers import datasets, versions
from backend.tests import test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN, GUEST, POWER, ROLES, World
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_upgrade_paths import ROOT

database = test_upgrade_paths.database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires disposable PostgreSQL',
)

SET = 'natural-earth-admin1'
ADMIN_USER = {'id': ADMIN, 'role': 'admin', 'username': f'user-{ADMIN}'}
D, OBJ, LOC, EMPTY = 10, 11, 12, 13  # the farm, a dataset with objects, one with a `location` field, an empty one
NOT_LOADED = ('boundary set', 'loaded')

# --- The boundary file ---

PROPERTIES = ('iso_3166_2', 'adm1_code', 'iso_a2', 'adm0_a3', 'admin', 'name_en', 'name', 'region')


def ring(west: float, south: float, east: float, north: float, clockwise: bool = False) -> list:
    points = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return points[::-1] if clockwise else points


def circle(lon: float, lat: float, radius: float, vertices: int) -> list:
    points = [
        [lon + radius * math.cos(2 * math.pi * i / vertices), lat + radius * math.sin(2 * math.pi * i / vertices)]
        for i in range(vertices)
    ]
    return [*points, points[0]]


def polygon(*rings: list) -> dict:
    return {'type': 'Polygon', 'coordinates': list(rings)}


def multipolygon(*polygons: dict) -> dict:
    return {'type': 'MultiPolygon', 'coordinates': [item['coordinates'] for item in polygons]}


def coast(lon: float) -> float:
    """The southern land's coastline: a latitude near -70 that wanders with the longitude."""
    return -70 + 2 * math.sin(math.radians(2 * lon))


# A ring round the pole: along the coast at every half degree (-179.75 .. 179.75, never longitude 0) from -180 to
# 180, down to the pole and back, counterclockwise.
COAST = [-180.0, *[-179.75 + 0.5 * i for i in range(720)], 180.0]
POLAR = polygon([[-180, coast(-180)], [-180, -90], [180, -90], *[[lon, coast(lon)] for lon in reversed(COAST)]])
# A band from -170 to 170, wider than half the globe without crossing the antimeridian, a vertex every half degree.
BAND_LONS = [-170 + 0.5 * i for i in range(681)]
BAND = polygon([*[[lon, 40] for lon in BAND_LONS], *[[lon, 45] for lon in reversed(BAND_LONS)], [-170, 40]])

ALDORIA = {'iso_a2': 'XA', 'adm0_a3': 'XAL', 'admin': 'Aldoria'}
BRENMARK = {'iso_a2': 'XB', 'adm0_a3': 'XBR', 'admin': 'Brenmark'}
CORALLINE = {'iso_a2': 'XC', 'adm0_a3': 'XCO', 'admin': 'Coralline'}
DUNMORE = {'iso_a2': '-1', 'adm0_a3': 'XDD', 'admin': 'Coralline'}  # no two-letter code; Coralline's admin text
# (the key the contract gives the feature, its properties, its geometry)
FEATURES: list[tuple[str, dict, dict]] = [
    (
        'XA-NM',
        {**ALDORIA, 'iso_3166_2': 'XA-NM', 'adm1_code': 'XAL-01', 'name_en': 'Northmarch', 'name': 'Nordmark'}
        | {'region': 'Midlands'},
        polygon(ring(0, 0, 5, 5)),
    ),
    (  # no name_en: the name; shares the meridian 5 with Northmarch
        'XA-LF',
        {**ALDORIA, 'iso_3166_2': 'XA-LF', 'adm1_code': 'XAL-02', 'name': 'Ashmere', 'region': 'Midlands'},
        polygon(ring(5, 0, 10, 5)),
    ),
    (  # a hole, which Corehold fills
        'XA-RG',
        {**ALDORIA, 'iso_3166_2': 'XA-RG', 'adm1_code': 'XAL-03', 'name_en': 'Ringwold', 'region': 'Côte Élevée'},
        polygon(ring(10, 0, 20, 10), ring(13, 3, 17, 7, clockwise=True)),
    ),
    (
        'XA-CO',
        {**ALDORIA, 'iso_3166_2': 'XA-CO', 'adm1_code': 'XAL-04', 'name_en': 'Corehold', 'region': 'Côte Élevée'},
        polygon(ring(13, 3, 17, 7)),
    ),
    (  # a mainland and an island; a damaged group name
        'XA-IS',
        {**ALDORIA, 'iso_3166_2': 'XA-IS', 'adm1_code': 'XAL-05', 'name_en': 'Isla', 'region': 'Nord?'},
        multipolygon(polygon(ring(0, 5, 10, 10)), polygon(ring(2, 12, 4, 14))),
    ),
    (  # a state named as a country is; an empty group name
        'XA-BK',
        {**ALDORIA, 'iso_3166_2': 'XA-BK', 'adm1_code': 'XAL-06', 'name_en': 'Brenmark', 'region': ''},
        polygon(ring(20, 0, 25, 5)),
    ),
    (  # a group name with a replacement character
        'XB-NO',
        {**BRENMARK, 'iso_3166_2': 'XB-NO', 'adm1_code': 'XBR-100', 'name_en': 'Mere', 'region': 'S�d'},
        polygon(ring(30, 0, 35, 5)),
    ),
    (  # a malformed iso_3166_2: keyed by adm1_code
        'MERE',
        {**BRENMARK, 'iso_3166_2': 'XB-ABCD', 'adm1_code': 'MERE', 'name_en': 'Fenwick', 'region': 'Fen and Mere'},
        polygon(ring(35, 0, 40, 5)),
    ),
    (  # two features with one iso_3166_2: each keyed by adm1_code
        'XBR-102',
        {**BRENMARK, 'iso_3166_2': 'XB-DU', 'adm1_code': 'XBR-102', 'name_en': 'Lakemere', 'region': 'Fen and Mere'},
        polygon(ring(30, 5, 35, 10)),
    ),
    (  # the minority's admin text; a key holding + and ?
        'XBR+00?',
        {**BRENMARK, 'admin': 'Brenmarke', 'iso_3166_2': 'XB-DU', 'adm1_code': 'XBR+00?', 'name_en': 'Dunwater'},
        polygon(ring(35, 5, 40, 10)),
    ),
    (  # across the antimeridian, as two pieces
        'XC-AM',
        {**CORALLINE, 'iso_3166_2': 'XC-AM', 'adm1_code': 'XCO-01', 'name_en': 'Merewater', 'region': 'Îles—du  Sud!'},
        multipolygon(polygon(ring(170, -10, 180, 0)), polygon(ring(-180, -10, -170, 0))),
    ),
    (
        'XC-NO',
        {**CORALLINE, 'iso_3166_2': 'XC-NO', 'adm1_code': 'XCO-02', 'name_en': 'Northreach', 'region': 'Îles—du  Sud!'},
        polygon(ring(170, 0, 180, 10)),
    ),
    (  # no name at all; a 6,000-vertex outline
        'XD-NN',
        {**DUNMORE, 'iso_3166_2': 'XD-NN', 'adm1_code': 'XDD-01'},
        polygon(circle(52.5, 2.5, 2, 6000)),
    ),
    (  # every longitude, from the coast down to the pole
        'XE-PL',
        {'iso_a2': 'XE', 'adm0_a3': 'XEF', 'admin': 'Frostmark'}
        | {'iso_3166_2': 'XE-PL', 'adm1_code': 'XEF-01', 'name_en': 'Polaris'},
        POLAR,
    ),
    (  # 340 degrees of longitude, not across the antimeridian
        'XF-BD',
        {'iso_a2': 'XF', 'adm0_a3': 'XFB', 'admin': 'Beltland'}
        | {'iso_3166_2': 'XF-BD', 'adm1_code': 'XFB-01', 'name_en': 'Longbelt'},
        BAND,
    ),
]
STATE_KEYS = [key for key, _, _ in FEATURES]
COUNTRIES = {
    'XA': [key for key in STATE_KEYS if key.startswith('XA-')],
    'XB': ['XB-NO', 'MERE', 'XBR-102', 'XBR+00?'],
    'XC': ['XC-AM', 'XC-NO'],
    'XDD': ['XD-NN'],
    'XE': ['XE-PL'],
    'XF': ['XF-BD'],
}
GROUPS = {
    'XA:midlands': ('Midlands', ['XA-NM', 'XA-LF']),
    'XA:cote-elevee': ('Côte Élevée', ['XA-RG', 'XA-CO']),
    'XB:fen-and-mere': ('Fen and Mere', ['MERE', 'XBR-102']),
    'XC:iles-du-sud': ('Îles—du  Sud!', ['XC-AM', 'XC-NO']),
}
# (key, kind, name (None: not asserted, a country renamed apart from another), country key)
REGIONS: list[tuple[str, str, Optional[str], str]] = [
    ('XA', 'country', 'Aldoria', 'XA'),
    ('XB', 'country', 'Brenmark', 'XB'),  # three of its four features say Brenmark
    ('XC', 'country', None, 'XC'),
    ('XDD', 'country', None, 'XDD'),
    ('XE', 'country', 'Frostmark', 'XE'),
    ('XF', 'country', 'Beltland', 'XF'),
    ('XA-NM', 'state', 'Northmarch', 'XA'),
    ('XA-LF', 'state', 'Ashmere', 'XA'),
    ('XA-RG', 'state', 'Ringwold', 'XA'),
    ('XA-CO', 'state', 'Corehold', 'XA'),
    ('XA-IS', 'state', 'Isla', 'XA'),
    ('XA-BK', 'state', 'Brenmark', 'XA'),
    ('XB-NO', 'state', 'Mere', 'XB'),
    ('MERE', 'state', 'Fenwick', 'XB'),
    ('XBR-102', 'state', 'Lakemere', 'XB'),
    ('XBR+00?', 'state', 'Dunwater', 'XB'),
    ('XC-AM', 'state', 'Merewater', 'XC'),
    ('XC-NO', 'state', 'Northreach', 'XC'),
    ('XD-NN', 'state', 'XD-NN', 'XDD'),
    ('XE-PL', 'state', 'Polaris', 'XE'),
    ('XF-BD', 'state', 'Longbelt', 'XF'),
    *[(key, 'group', name, key.split(':')[0]) for key, (name, _) in GROUPS.items()],
]
KEYS = [key for key, _, _, _ in REGIONS]
COUNTS = {'country': 6, 'state': 15, 'group': 4}
# [west, south, east, north]; west > east across the antimeridian
BBOXES: dict[str, list[float]] = {
    'XA': [0, 0, 25, 14],
    'XA-IS': [0, 5, 10, 14],
    'XA-RG': [10, 0, 20, 10],
    'XA:midlands': [0, 0, 10, 5],
    'XB': [30, 0, 40, 10],
    'XC': [170, -10, -170, 10],
    'XC-AM': [170, -10, -170, 0],
    'XC-NO': [170, 0, 180, 10],
    'XC:iles-du-sud': [170, -10, -170, 10],
    'XF-BD': [-170, 40, 170, 45],
    'XF': [-170, 40, 170, 45],
}
# The farm's samples: (name, lon, lat (None: no location), the states it is inside)
POINTS: list[tuple[str, Optional[float], Optional[float], set[str]]] = [
    ('p01', 2.5, 2.5, {'XA-NM'}),
    ('p02', 7.5, 2.5, {'XA-LF'}),
    ('p03', 5, 2.5, {'XA-NM', 'XA-LF'}),  # on the border they share
    ('p04', 0, 2.5, {'XA-NM'}),  # on its outer border
    ('p05', 11, 1, {'XA-RG'}),
    ('p06', 15, 5, {'XA-CO'}),  # in the hole: inside Corehold, not Ringwold
    ('p07', 13, 5, {'XA-RG', 'XA-CO'}),  # on the hole's edge
    ('p08', 3, 13, {'XA-IS'}),  # on the island
    ('p09', 3, 11, set()),  # in the water between the island and the mainland
    ('p10', 5, 7.5, {'XA-IS'}),
    ('p11', 22, 2.5, {'XA-BK'}),
    ('p12', 32, 2.5, {'XB-NO'}),
    ('p13', 37, 2.5, {'MERE'}),
    ('p14', 32, 7.5, {'XBR-102'}),
    ('p15', 37, 7.5, {'XBR+00?'}),
    ('p16', 175, -5, {'XC-AM'}),
    ('p17', -175, -5, {'XC-AM'}),
    ('p18', 175, 5, {'XC-NO'}),
    ('p19', 52, 2, {'XD-NN'}),
    ('p20', 100, 50, set()),
    ('p21', None, None, set()),
    ('p22', None, None, set()),
    ('p23', -0.1, -75, {'XE-PL'}),  # the land round the pole, either side of longitude 0
    ('p24', 0.1, -75, {'XE-PL'}),
    ('p25', 179.9, -75, {'XE-PL'}),  # and either side of 180
    ('p26', -179.9, -75, {'XE-PL'}),
    ('p27', 0, -90, {'XE-PL'}),  # the pole, on its edge
    ('p28', 45, -89.5, {'XE-PL'}),
    ('p29', 10, -68, set()),  # just north of the coast
    ('p35', -0.1, -65, set()),  # north of the coast by longitude 0
    ('p30', 0, 42.5, {'XF-BD'}),  # the band's middle
    ('p31', 169, 42.5, {'XF-BD'}),  # near its ends
    ('p32', -169, 42.5, {'XF-BD'}),
    ('p33', 175, 42.5, set()),  # past its ends, where a box the short way round would reach
    ('p34', -175, 42.5, set()),
]
NAMES = [name for name, _, _, _ in POINTS]
LOCATED = {name for name, lon, _, _ in POINTS if lon is not None}


def members(key: str) -> list[str]:
    """The states a region is made of."""
    if key in COUNTRIES:
        return COUNTRIES[key]
    if key in GROUPS:
        return GROUPS[key][1]
    return [key]


def inside(key: str) -> set[str]:
    """The names of the farm's samples inside the region."""
    states = set(members(key))
    return {name for name, _, _, within in POINTS if within & states}


def boundary_file(path: Path, leave_out: tuple[str, ...] = ()) -> Path:
    collection = {
        'type': 'FeatureCollection',
        'features': [
            {
                'type': 'Feature',
                'properties': {name: properties.get(name) for name in PROPERTIES},
                'geometry': geometry,
            }
            for key, properties, geometry in FEATURES
            if key not in leave_out
        ],
    }
    path.write_text(json.dumps(collection, ensure_ascii=False))
    return path


def test_the_fixture_is_valid_geojson_and_postgis_agrees_with_the_expected_points(database, tmp_path):
    """No region feature needed: the file is GeoJSON PostGIS reads as valid shapes, and each region's union covers
    exactly the samples POINTS says, borders included (the bounding boxes too, where they don't cross 180)."""
    make = database[0]
    name = make('CREATE EXTENSION IF NOT EXISTS postgis;')
    collection = json.loads(boundary_file(tmp_path / 'boundaries.geojson').read_text())
    assert collection['type'] == 'FeatureCollection' and len(collection['features']) == len(FEATURES)
    geometry_of = {key: json.dumps(item['geometry']) for (key, _, _), item in zip(FEATURES, collection['features'])}

    async def check() -> None:
        url = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()
        connection = await asyncpg.connect(url)
        try:
            for key, text in geometry_of.items():
                valid = await connection.fetchval('SELECT ST_IsValid(ST_GeomFromGeoJSON($1))', text)
                assert valid, key
            union = 'SELECT ST_Union(ST_SetSRID(ST_GeomFromGeoJSON(g), 4326)) FROM unnest($1::text[]) g'
            for key in KEYS:
                shapes = [geometry_of[state] for state in members(key)]
                covered = await connection.fetch(
                    f'WITH region AS ({union}) '
                    'SELECT p.name FROM region, unnest($2::text[], $3::float8[], $4::float8[]) p(name, lon, lat) '
                    'WHERE ST_Covers((SELECT * FROM region), ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326))',
                    shapes,
                    [point for point, lon, _, _ in POINTS if lon is not None],
                    [lon for _, lon, _, _ in POINTS if lon is not None],
                    [lat for _, lon, lat, _ in POINTS if lon is not None],
                )
                assert {row['name'] for row in covered} == inside(key), key
                if key in BBOXES and BBOXES[key][0] < BBOXES[key][2]:
                    box = await connection.fetchrow(
                        f'SELECT ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g) FROM ({union}) region(g)',
                        shapes,
                    )
                    assert list(box) == pytest.approx(BBOXES[key]), key
        finally:
            await connection.close()

    asyncio.run(check())


# --- The test world: a database of its own, the app on it, and the loader pointed at it ---


class Regions(World):
    def __init__(self, database_: Database, client: httpx.AsyncClient, url: str, folder: Path, monkeypatch: Any):
        super().__init__(database_, client)
        self.url, self.folder, self.monkeypatch = url, folder, monkeypatch
        self.ids: dict[str, int] = {}  # the farm's samples by name

    def of(self, names: set[str]) -> set[int]:
        return {self.ids[name] for name in names}

    @property
    def everything(self) -> set[int]:
        return set(self.ids.values())


@pytest.fixture
def regions_world(database, monkeypatch, tmp_path):
    make = database[0]

    def run(scenario):
        name = make((ROOT / 'backend/db/schema.sql').read_text())
        url = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()
        monkeypatch.setattr(database_module, 'DATABASE_URL', url)
        monkeypatch.setenv('DATABASE_URL', url)
        monkeypatch.setattr(datasets, '_array_columns_cache', None)
        monkeypatch.setattr(datasets, 'require_free_space', AsyncMock())
        monkeypatch.setattr(datasets, 'validate_embedding_dims', AsyncMock())
        monkeypatch.setattr(versions, 'require_free_space', AsyncMock())
        monkeypatch.setattr(versions, 'estimate_snapshot_bytes', AsyncMock(return_value=0))

        async def body() -> None:
            pool = await asyncpg.create_pool(
                url,
                min_size=1,
                max_size=8,
                init=init_connection,
                server_settings={'statement_timeout': '30000', **PLANNING},
            )
            monkeypatch.setattr(database_module.db, 'pool', pool)
            database_ = Database()
            database_.pool = pool
            try:
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                    r = Regions(database_, client, url, tmp_path, monkeypatch)
                    for user_id, role in ROLES.items():
                        await r.user(user_id, role)
                    await scenario(r)
            finally:
                await pool.close()

        asyncio.run(body())

    return run


async def load(r: Regions, version: str = 'v1', leave_out: tuple[str, ...] = (), verify: bool = False) -> int:
    """The loader's command line on a boundary file of FEATURES (less `leave_out`), in a thread of its own."""
    path = boundary_file(r.folder / f'boundaries-{version}.geojson', leave_out)
    module = importlib.import_module('backend.regions')
    if hasattr(module, 'DATABASE_URL'):
        r.monkeypatch.setattr(module, 'DATABASE_URL', r.url)
    argv = ['load', str(path), '--set', SET, *([] if verify else ['--no-verify']), '--set-version', version]
    return await asyncio.to_thread(module.main, argv)


async def loaded(r: Regions) -> None:
    assert await load(r) == 0


async def farm(r: Regions) -> None:
    """The farm (private; the admins have it): a sample at each point of POINTS, two with no location."""
    await r.dataset(D)
    samples = []
    for rank, (name, lon, lat, _) in enumerate(POINTS, start=1):
        sample: dict[str, Any] = {
            'filepath': f'farm-{name}.jpg',
            'metadata': {'plot': name, 'rank': rank, 'grp': 'a' if rank % 2 else 'b'},
        }
        if lon is not None:
            sample['location'] = {'type': 'Point', 'coordinates': [lon, lat]}
        samples.append(sample)
    samples[0]['metadata']['within'] = 'within'  # a field named within, and that value
    samples[1]['metadata']['within'] = 'outside'
    samples[2]['metadata']['note'] = 'within'
    await datasets.add_samples(D, {'samples': samples}, user=ADMIN_USER)
    rows = await r.database.fetch(
        "SELECT id, metadata->>'plot' AS plot, lon, lat FROM samples WHERE dataset_id = $1",
        D,
    )
    r.ids = {row['plot']: row['id'] for row in rows}
    stored = {row['plot']: (row['lon'], row['lat']) for row in rows}
    assert stored == {name: (lon, lat) for name, lon, lat, _ in POINTS}


def keep(key: Any) -> dict[str, Any]:
    return {'field': 'location', 'operator': 'geo_region', 'value': key}


def drop(key: Any) -> dict[str, Any]:
    return {'field': 'location', 'operator': 'geo_region_exclude', 'value': key}


async def post(r: Regions, path: str, body: Any, user: Optional[int] = ADMIN) -> httpx.Response:
    return await r.call(user, 'POST', path, json=body)


async def selected(r: Regions, filter_node: Any, dataset_id: int = D) -> set[int]:
    """The samples the filter selects, as samples/query lists them, and its count agreeing."""
    response = await post(r, f'/api/datasets/{dataset_id}/samples/query', {'limit': 1000, 'filters': filter_node})
    assert response.status_code == 200, (filter_node, response.text)
    body = response.json()
    ids = {item['id'] for item in body['items']}
    assert body['total'] == len(ids) == len(body['items']), filter_node
    return ids


def refusal(response: httpx.Response) -> str:
    assert response.status_code == 400, response.text
    detail = response.json()['detail']
    return detail if isinstance(detail, str) else json.dumps(detail)


async def parse(r: Regions, query: str, dataset_id: int = D) -> httpx.Response:
    return await post(r, f'/api/datasets/{dataset_id}/filter_query/parse', {'query': query, 'current': []})


async def nodes(r: Regions, query: str, dataset_id: int = D) -> list[dict[str, Any]]:
    response = await parse(r, query, dataset_id)
    assert response.status_code == 200, (query, response.text)
    body = response.json()
    assert body['filter'] == {'logic': 'AND', 'filters': body['filters']}
    return body['filters']


async def text_of(r: Regions, node: dict[str, Any], dataset_id: int = D) -> Optional[str]:
    response = await post(r, f'/api/datasets/{dataset_id}/filter_query/format', {'filters': [node]})
    assert response.status_code == 200, response.text
    return response.json()['parts'][0]


def mistake(response: httpx.Response, line: int) -> dict[str, Any]:
    """The one error of a refused query, shaped as the language's other mistakes."""
    assert response.status_code == 400, response.text
    detail = response.json()['detail']
    [error] = detail['errors']
    assert set(error) == {'message', 'line', 'column', 'suggestions'}, error
    assert error['line'] == line, error
    assert detail['message'] == f"line {error['line']}, column {error['column']}: {error['message']}"
    return error


async def region(r: Regions, key: str, user: Optional[int] = ADMIN, **params: Any) -> httpx.Response:
    return await r.call(user, 'GET', '/api/regions/' + quote(key, safe=''), params=params)


async def search(r: Regions, text: str, user: Optional[int] = ADMIN, **params: Any) -> httpx.Response:
    return await r.call(user, 'GET', '/api/regions', params={'search': text, 'limit': 100, **params})


async def admin_sets(r: Regions) -> dict[str, Any]:
    response = await r.call(ADMIN, 'GET', '/api/admin/regions')
    assert response.status_code == 200, response.text
    sets = {item['name']: item for item in response.json()['sets']}
    assert SET in sets, sets
    return sets[SET]


def vertices(outline: dict) -> int:
    return sum(len(points) for shape in outline['coordinates'] for points in shape)


def assert_bbox(found: Any, expected: list[float], key: str) -> None:
    assert isinstance(found, list) and len(found) == 4, (key, found)
    assert found == pytest.approx(expected, abs=1e-6), key


# --- Loading ---


def test_loading_makes_the_three_kinds_with_their_keys_names_and_counts(regions_world):
    async def scenario(r):
        await loaded(r)
        state = await admin_sets(r)
        assert isinstance(state['loaded'], dict), state
        info = state['loaded']
        assert (info['version'], info['verified'], info['counts'], info['removed_keys']) == ('v1', False, COUNTS, [])
        digest = hashlib.sha256((r.folder / 'boundaries-v1.geojson').read_bytes()).hexdigest()
        assert info['sha256'] == digest
        for key in ('loaded_from', 'loaded_at', 'loaded_by'):
            assert key in info, info
        country_names: dict[str, str] = {}
        for key, kind, name, country_key in REGIONS:
            response = await region(r, key)
            assert response.status_code == 200, (key, response.text)
            body = response.json()
            assert (body['key'], body['kind'], body['country_key']) == (key, kind, country_key), body
            assert (body['set'], body['version']) == (SET, 'v1'), body
            if name is not None:
                assert body['name'] == name, body
            assert isinstance(body['name'], str) and body['name'], body
            if kind == 'country':
                country_names[key] = body['name']
            if key in BBOXES:
                assert_bbox(body['bbox'], BBOXES[key], key)
            else:
                assert isinstance(body['bbox'], list) and len(body['bbox']) == 4, body
        assert len(set(country_names.values())) == len(country_names), country_names  # no two share a name
        for key, _, _, country_key in REGIONS:
            body = (await region(r, key)).json()
            assert body['country_name'] == country_names[country_key], body
        # The codes a key is not made from.
        for key in ('XB-ABCD', 'XB-DU', '-1', 'XA:nord', 'XB:s-d', 'XA:'):
            assert (await region(r, key)).status_code == 404, key

    regions_world(scenario)


def test_a_file_that_is_not_the_pinned_one_is_refused_without_no_verify(regions_world):
    async def scenario(r):
        assert await load(r, verify=True) == 1
        assert (await search(r, 'Isla')).json() == []
        assert (await admin_sets(r))['loaded'] is None
        assert (await region(r, 'XA-IS')).status_code == 404

    regions_world(scenario)


def test_loading_again_replaces_the_set_and_names_the_keys_it_removed(regions_world, capfd):
    async def scenario(r):
        await loaded(r)
        capfd.readouterr()
        gone = ('XA-BK', 'XC-AM', 'XC-NO')
        removed = {'XA-BK', 'XC-AM', 'XC-NO', 'XC', 'XC:iles-du-sud'}
        assert await load(r, 'v2', leave_out=gone) == 0
        report = ''.join(capfd.readouterr())
        for key in removed:
            assert key in report, (key, report)
        info = (await admin_sets(r))['loaded']
        assert info['version'] == 'v2'
        assert set(info['removed_keys']) == removed
        assert info['counts'] == {'country': 5, 'state': 12, 'group': 3}
        for key in removed:
            assert (await region(r, key)).status_code == 404, key
        assert (await region(r, 'XA-NM')).json()['version'] == 'v2'
        assert [item['key'] for item in (await search(r, 'Merewater')).json()] == []
        await farm(r)
        message = refusal(await post(r, f'/api/datasets/{D}/samples/query', {'filters': keep('XA-BK')}))
        assert message.startswith("Invalid input: Unknown region 'XA-BK'"), message

    regions_world(scenario)


# --- The HTTP routes ---


def test_the_admin_routes_are_for_admins_only(regions_world):
    async def scenario(r):
        state = await admin_sets(r)
        assert state['loaded'] is None
        for key in ('title', 'version', 'url', 'licence'):
            assert isinstance(state[key], str) and state[key], state
        assert re.fullmatch(r'[0-9a-f]{64}', state['sha256']), state
        await r.dataset(D)
        await r.share(D, POWER, 'manage')
        for user, status in ((POWER, 403), (GUEST, 403), (None, 401)):
            assert (await r.call(user, 'GET', '/api/admin/regions')).status_code == status, user
            response = await post(r, '/api/admin/regions/load', {'set': SET}, user=user)
            assert response.status_code == status, (user, response.text)
        unknown = await post(r, '/api/admin/regions/load', {'set': 'world-rivers'})
        assert unknown.status_code == 400 and SET in unknown.text, unknown.text
        started = await post(r, '/api/admin/regions/load', {'set': SET})
        assert started.status_code == 202, started.text
        body = started.json()
        assert {'job_id', 'state', 'started'} <= set(body), body
        job = await r.database.fetchrow('SELECT state FROM jobs WHERE id::text = $1', str(body['job_id']))
        assert job is not None and job['state'] == 'queued', job

    regions_world(scenario)


def test_any_signed_in_user_searches_the_regions_in_the_contract_order(regions_world):
    async def scenario(r):
        assert (await search(r, 'mere')).json() == []  # no set loaded
        await loaded(r)
        assert (await search(r, 'mere', user=None)).status_code == 401
        order = ['MERE', 'XB-NO', 'XC-AM', 'XA-LF', 'XBR-102', 'XB:fen-and-mere']
        for text in ('mere', 'MERE', 'Mere'):
            response = await search(r, text, user=GUEST)
            assert response.status_code == 200, response.text
            assert [item['key'] for item in response.json()] == order, text
        listed = {item['key']: item for item in (await search(r, 'mere')).json()}
        for key, kind, name, country_key in REGIONS:
            if key in listed:
                item = listed[key]
                assert set(item) == {'key', 'kind', 'name', 'country_key', 'country_name'}, item
                assert (item['kind'], item['country_key']) == (kind, country_key), item
                assert name is None or item['name'] == name, item
        assert listed['MERE']['country_name'] == 'Brenmark' and listed['XA-LF']['country_name'] == 'Aldoria'
        # Ties by kind: a country before a state of the same name.
        assert [item['key'] for item in (await search(r, 'brenmark')).json()] == ['XB', 'XA-BK']
        assert [item['key'] for item in (await search(r, 'xa-nm')).json()][0] == 'XA-NM'
        assert [item['key'] for item in (await search(r, 'mere', kind='state')).json()] == order[:5]
        assert [item['key'] for item in (await search(r, 'mere', kind='group')).json()] == order[5:]
        assert (await search(r, 'mere', kind='country')).json() == []
        assert [item['key'] for item in (await search(r, 'mere', limit=2)).json()] == order[:2]
        for params in ({'kind': 'province'}, {'limit': 0}, {'limit': 101}):
            assert (await search(r, 'mere', **params)).status_code == 422, params

    regions_world(scenario)


def test_a_region_is_read_by_its_key_in_any_case_with_an_outline_on_request(regions_world):
    async def scenario(r):
        await loaded(r)
        for asked, key in (
            ('xa-is', 'XA-IS'),
            ('Xa-Is', 'XA-IS'),
            ('xa:MIDLANDS', 'XA:midlands'),
            ('XA:Cote-Elevee', 'XA:cote-elevee'),
            ('xbr+00?', 'XBR+00?'),
            ('XBR+00?', 'XBR+00?'),
            ('mere', 'MERE'),
            ('xdd', 'XDD'),
        ):
            response = await region(r, asked, user=GUEST)
            assert response.status_code == 200, (asked, response.text)
            assert response.json()['key'] == key, asked
            assert response.json().get('outline') is None, asked
        assert (await region(r, 'XA-ZZ')).status_code == 404
        island = (await region(r, 'XA-IS', outline='true')).json()['outline']
        assert island['type'] == 'MultiPolygon' and len(island['coordinates']) == 2, island
        ringed = (await region(r, 'XA-RG', outline='true')).json()['outline']
        assert ringed['type'] == 'MultiPolygon' and [len(shape) for shape in ringed['coordinates']] == [2], ringed
        covers = 'SELECT ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON($1), 4326), ST_SetSRID(ST_MakePoint($2, $3), 4326))'
        assert await r.database.fetchval(covers, json.dumps(ringed), 11.0, 1.0)  # the ring
        assert not await r.database.fetchval(covers, json.dumps(ringed), 15.0, 5.0)  # the hole
        assert not await r.database.fetchval(covers, json.dumps(island), 3.0, 11.0)  # the water
        for key in KEYS:
            outline = (await region(r, key, outline='true')).json()['outline']
            assert outline['type'] == 'MultiPolygon' and 4 <= vertices(outline) <= 5000, key
        assert vertices((await region(r, 'XD-NN', outline='true')).json()['outline']) <= 5000  # from 6,000

    regions_world(scenario)


# --- The filter ---


def test_inside_and_outside_select_exactly_the_samples_of_each_region(regions_world):
    async def scenario(r):
        await farm(r)
        await loaded(r)
        no_location = r.of({'p21', 'p22'})
        for key in KEYS:
            expected = r.of(inside(key))
            assert await selected(r, keep(key)) == expected, key
            assert await selected(r, drop(key)) == r.everything - expected, key
            assert no_location <= await selected(r, drop(key)), key
        for asked, key in (
            ('xa-nm', 'XA-NM'),
            ('xa:MIDLANDS', 'XA:midlands'),
            ('xbr+00?', 'XBR+00?'),
            ('mere', 'MERE'),
        ):
            assert await selected(r, keep(asked)) == r.of(inside(key)), asked
        # The exact shapes: the island, the hole, the borders.
        assert r.ids['p08'] in await selected(r, keep('XA-IS'))
        assert r.ids['p09'] not in await selected(r, keep('XA-IS'))
        assert r.ids['p06'] not in await selected(r, keep('XA-RG'))
        assert r.ids['p06'] in await selected(r, keep('XA-CO'))
        for key in ('XA-NM', 'XA-LF'):
            assert r.ids['p03'] in await selected(r, keep(key)), key
        assert r.ids['p04'] in await selected(r, keep('XA-NM'))
        assert {r.ids['p16'], r.ids['p17']} <= await selected(r, keep('XC-AM'))

    regions_world(scenario)


def test_an_unknown_key_a_bad_value_or_another_field_is_a_400(regions_world):
    async def scenario(r):
        await farm(r)
        await r.dataset(EMPTY)
        message = refusal(await post(r, f'/api/datasets/{D}/samples/query', {'filters': keep('XA-NM')}))
        assert all(word in message.lower() for word in NOT_LOADED), message  # no set loaded
        await loaded(r)
        for asked, shown in (('xa-zz', 'XA-ZZ'), ('xa:Nowhere', 'XA:nowhere'), ('XB-DU', 'XB-DU')):
            for dataset_id in (D, EMPTY):
                for node in (keep(asked), drop(asked)):
                    query = {'filters': node}
                    message = refusal(await post(r, f'/api/datasets/{dataset_id}/samples/query', query))
                    assert message.startswith(f"Invalid input: Unknown region '{shown}'"), message
        for path, body in (
            (f'/api/datasets/{D}/aggregate', {'aggregations': [{'field': 'plot', 'op': 'count'}]}),
            (f'/api/datasets/{D}/map_points', {'points': True}),
            (f'/api/datasets/{D}/histogram', {'field': 'rank'}),
        ):
            message = refusal(await post(r, path, {**body, 'filters': keep('xa-zz')}))
            assert message.startswith("Invalid input: Unknown region 'XA-ZZ'"), (path, message)
        for value in (5, ['XA-NM'], None, {'key': 'XA-NM'}, True):
            for node in (keep(value), drop(value)):
                response = await post(r, f'/api/datasets/{D}/samples/query', {'filters': node})
                assert response.status_code == 400, (value, response.text)
        for field in ('plot', 'grp', 'locations'):
            node = {'field': field, 'operator': 'geo_region', 'value': 'XA-NM'}
            response = await post(r, f'/api/datasets/{D}/samples/query', {'filters': node})
            assert response.status_code == 400, (field, response.text)

    regions_world(scenario)


def test_the_filter_holds_in_every_reader_of_filters(regions_world):
    async def scenario(r):
        await farm(r)
        vectors = [{'sample_id': r.ids[name], 'vector': [1.0, i / 100, 0.0, 0.0]} for i, name in enumerate(NAMES)]
        added = await post(r, f'/api/datasets/{D}/embeddings', [{**item, 'field_name': 'clip'} for item in vectors])
        assert added.is_success, added.text
        await loaded(r)
        rank = {name: index for index, name in enumerate(NAMES, start=1)}
        for node, names in (
            (keep('XA'), inside('XA')),
            (keep('XA-IS'), inside('XA-IS')),
            (drop('XA'), set(NAMES) - inside('XA')),
            (keep('XC:iles-du-sud'), inside('XC:iles-du-sud')),
        ):
            ids = r.of(names)
            where = (node['operator'], node['value'])
            aggregate = await post(
                r,
                f'/api/datasets/{D}/aggregate',
                {
                    'filters': node,
                    'aggregations': [{'field': 'plot', 'op': 'count'}, {'field': 'plot', 'op': 'values'}],
                },
            )
            assert aggregate.status_code == 200, aggregate.text
            count, values = aggregate.json()['results']
            assert (count['value'], sorted(values['value'])) == (len(names), sorted(names)), where
            histogram = await post(r, f'/api/datasets/{D}/histogram', {'field': 'rank', 'filters': node, 'bins': 20})
            assert histogram.status_code == 200, histogram.text
            assert histogram.json()['total'] == len(names), where
            facet = await post(r, f'/api/datasets/{D}/field-stats', {'filters': node, 'fields': ['rank']})
            assert facet.status_code == 200, facet.text
            [entry] = facet.json()
            assert (entry['min'], entry['max']) == (min(rank[n] for n in names), max(rank[n] for n in names)), where
            points = await post(r, f'/api/datasets/{D}/map_points', {'filters': node, 'points': True})
            assert points.status_code == 200, points.text
            assert {item['id'] for item in points.json()['items']} == r.of(names & LOCATED), where
            token = await post(r, f'/api/datasets/{D}/selection_token', {'filters': node})
            assert token.status_code == 200, token.text
            assert token.json()['count'] == len(names), where
            in_token = {'field': 'id', 'operator': 'in_selection', 'value': token.json()['token']}
            assert await selected(r, in_token) == ids, where
            found = await post(
                r,
                '/api/search',
                {'dataset_id': D, 'vector': [1.0, 0.0, 0.0, 0.0], 'field_name': 'clip', 'limit': 100, 'filters': node},
            )
            assert found.status_code == 200, found.text
            assert {item['id'] for item in found.json()} == ids, where
        extent = await post(r, f'/api/datasets/{D}/map_extent', {'filters': keep('XA-IS')})
        assert extent.status_code == 200, extent.text
        assert extent.json() == {'w': 3, 's': 7.5, 'e': 5, 'n': 13, 'count': 2}
        for tag, node in (('midlands', keep('XA:midlands')), ('not-aldoria', drop('XA'))):
            tagged = await post(r, f'/api/datasets/{D}/samples/bulk-tag', {'filters': node, 'add_tags': [tag]})
            assert tagged.status_code in (200, 202), tagged.text
            if tagged.status_code == 202:  # a job: run it here, as the runner would
                await finish(r.database)
            rows = await r.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 AND $2 = ANY(tags)', D, tag)
            expected = inside('XA:midlands') if tag == 'midlands' else set(NAMES) - inside('XA')
            assert {row['id'] for row in rows} == r.of(expected), tag

    regions_world(scenario)


def test_a_version_answers_by_the_locations_it_saved(regions_world):
    async def scenario(r):
        await farm(r)
        made = await post(r, f'/api/datasets/{D}/versions', {'name': 'before the move'})
        assert made.is_success, made.text
        version = await r.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', D)
        moved = await r.call(
            ADMIN,
            'PATCH',
            f"/api/datasets/{D}/samples/{r.ids['p01']}",
            json={'location': {'type': 'Point', 'coordinates': [100.0, 50.0]}},
        )
        assert moved.status_code == 200, moved.text
        assert await r.database.fetchval('SELECT lon FROM samples WHERE id = $1', r.ids['p01']) == 100
        await loaded(r)
        assert await selected(r, keep('XA-NM')) == r.of(inside('XA-NM') - {'p01'})
        base = f'/api/datasets/{D}/versions/{version}'
        for node, names in ((keep('XA-NM'), inside('XA-NM')), (drop('XA-NM'), set(NAMES) - inside('XA-NM'))):
            rows = await post(r, f'{base}/samples/query', {'filters': node, 'limit': 1000})
            assert rows.status_code == 200, rows.text
            assert {item['id'] for item in rows.json()['items']} == r.of(names), node
            aggregate = await post(
                r,
                f'{base}/aggregate',
                {'filters': node, 'aggregations': [{'field': 'plot', 'op': 'values'}]},
            )
            assert aggregate.status_code == 200, aggregate.text
            assert sorted(aggregate.json()['results'][0]['value']) == sorted(names), node

    regions_world(scenario)


def test_the_filter_combines_under_and_or_and_not(regions_world):
    async def scenario(r):
        await farm(r)
        await loaded(r)
        grp_a = r.of({name for rank, name in enumerate(NAMES, start=1) if rank % 2})
        a = {'field': 'grp', 'operator': '=', 'value': 'a'}
        b = {'field': 'grp', 'operator': '=', 'value': 'b'}
        cases = [
            ({'logic': 'AND', 'filters': [keep('XA'), a]}, r.of(inside('XA')) & grp_a),
            ({'logic': 'OR', 'filters': [keep('XA-IS'), b]}, r.of(inside('XA-IS')) | (r.everything - grp_a)),
            ({'logic': 'OR', 'filters': [keep('XA-IS'), keep('XB')]}, r.of(inside('XA-IS') | inside('XB'))),
            ({'logic': 'NOT', 'filters': [keep('XA')]}, r.everything - r.of(inside('XA'))),
            ({'logic': 'NOT', 'filters': [drop('XA')]}, r.of(inside('XA'))),
            ({'logic': 'AND', 'filters': [drop('XA'), a]}, (r.everything - r.of(inside('XA'))) & grp_a),
            (
                {'logic': 'OR', 'filters': [{'logic': 'AND', 'filters': [keep('XA:midlands'), b]}, keep('XDD')]},
                (r.of(inside('XA:midlands')) - grp_a) | r.of(inside('XDD')),
            ),
        ]
        for node, expected in cases:
            assert await selected(r, node) == expected, node
        assert await selected(r, {'logic': 'NOT', 'filters': [keep('XA')]}) == await selected(r, drop('XA'))

    regions_world(scenario)


ROUND_KEYS = ('XE-PL', 'XE', 'XF-BD', 'XF')  # round the pole, and wider than half the globe


async def assert_every_reader(r: Regions, node: dict[str, Any], names: set[str]) -> None:
    """Each reader of filters gives exactly the samples `names` for the filter."""
    ids, where = r.of(names), (node['operator'], node['value'])
    assert await selected(r, node) == ids, where
    rank = {name: index for index, name in enumerate(NAMES, start=1)}
    aggregations = [{'field': 'plot', 'op': 'count'}, {'field': 'plot', 'op': 'values'}]
    aggregate = await post(r, f'/api/datasets/{D}/aggregate', {'filters': node, 'aggregations': aggregations})
    assert aggregate.status_code == 200, aggregate.text
    count, values = aggregate.json()['results']
    assert (count['value'], sorted(values['value'])) == (len(names), sorted(names)), where
    histogram = await post(r, f'/api/datasets/{D}/histogram', {'field': 'rank', 'filters': node, 'bins': 20})
    assert histogram.status_code == 200 and histogram.json()['total'] == len(names), (where, histogram.text)
    facet = await post(r, f'/api/datasets/{D}/field-stats', {'filters': node, 'fields': ['rank']})
    assert facet.status_code == 200, facet.text
    if names:
        [entry] = facet.json()
        assert (entry['min'], entry['max']) == (min(rank[n] for n in names), max(rank[n] for n in names)), where
    points = await post(r, f'/api/datasets/{D}/map_points', {'filters': node, 'points': True})
    assert points.status_code == 200, points.text
    assert {item['id'] for item in points.json()['items']} == r.of(names & LOCATED), where
    token = await post(r, f'/api/datasets/{D}/selection_token', {'filters': node})
    assert token.status_code == 200 and token.json()['count'] == len(names), (where, token.text)
    assert await selected(r, {'field': 'id', 'operator': 'in_selection', 'value': token.json()['token']}) == ids
    found = await post(
        r,
        '/api/search',
        {'dataset_id': D, 'vector': [1.0, 0.0, 0.0, 0.0], 'field_name': 'clip', 'limit': 100, 'filters': node},
    )
    assert found.status_code == 200, found.text
    assert {item['id'] for item in found.json()} == ids, where


def test_a_region_round_the_pole_and_one_wider_than_half_the_globe_hold_in_every_position(regions_world):
    """A sample is inside exactly when its point lies in the shape, wherever the filter sits: near longitude 0 and
    180, at the pole, past the ends of a 340-degree band."""

    async def scenario(r):
        await farm(r)
        vectors = [{'sample_id': r.ids[name], 'vector': [1.0, i / 100, 0.0, 0.0]} for i, name in enumerate(NAMES)]
        added = await post(r, f'/api/datasets/{D}/embeddings', [{**item, 'field_name': 'clip'} for item in vectors])
        assert added.is_success, added.text
        made = await post(r, f'/api/datasets/{D}/versions', {'name': 'before the move'})
        assert made.is_success, made.text
        version = await r.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', D)
        await loaded(r)
        located = r.of(LOCATED)
        a = {'field': 'grp', 'operator': '=', 'value': 'a'}
        grp_a = r.of({name for rank, name in enumerate(NAMES, start=1) if rank % 2})
        one = {'field': 'plot', 'operator': '=', 'value': 'p01'}
        for key in ROUND_KEYS:
            names = inside(key)
            ids, outside = r.of(names), r.everything - r.of(names)
            assert names and names <= LOCATED, key
            # The two forms: complements over the samples with a location; the exclude keeps those without one.
            kept, dropped = await selected(r, keep(key)), await selected(r, drop(key))
            assert (kept, dropped) == (ids, outside), key
            assert kept | (dropped & located) == located and not kept & dropped, key
            # Under AND, OR and NOT, and as text.
            for node, expected in (
                ({'logic': 'AND', 'filters': [keep(key), a]}, ids & grp_a),
                ({'logic': 'OR', 'filters': [keep(key), one]}, ids | {r.ids['p01']}),
                ({'logic': 'OR', 'filters': [drop(key), one]}, outside | {r.ids['p01']}),
                ({'logic': 'NOT', 'filters': [keep(key)]}, outside),
                ({'logic': 'NOT', 'filters': [drop(key)]}, ids),
                ({'logic': 'NOT', 'filters': [{'logic': 'OR', 'filters': [keep(key), one]}]}, outside - {r.ids['p01']}),
            ):
                assert await selected(r, node) == expected, (key, node)
            for query, expected in (
                (f'location within "{key}"', ids),
                (f'location not within "{key}"', outside),
                (f'location within "{key}" OR plot = "p01"', ids | {r.ids['p01']}),
                (f'NOT (location within "{key}")', outside),
            ):
                assert await selected(r, {'logic': 'AND', 'filters': await nodes(r, query)}) == expected, query
            # Every reader of filters.
            await assert_every_reader(r, keep(key), names)
            await assert_every_reader(r, drop(key), set(NAMES) - names)
            # Its bounding box holds each sample inside it.
            box = (await region(r, key)).json()['bbox']
            west, south, east, north = box
            for name, lon, lat, _ in POINTS:
                if name in names:
                    assert lon is not None and lat is not None
                    across = lon >= west or lon <= east
                    assert (west <= lon <= east if west <= east else across) and south <= lat <= north, (key, name, box)
        # A bulk tag by the filter.
        tagged = await post(r, f'/api/datasets/{D}/samples/bulk-tag', {'filters': keep('XE-PL'), 'add_tags': ['polar']})
        assert tagged.status_code in (200, 202), tagged.text
        if tagged.status_code == 202:
            await finish(r.database)
        rows = await r.database.fetch("SELECT id FROM samples WHERE dataset_id = $1 AND 'polar' = ANY(tags)", D)
        assert {row['id'] for row in rows} == r.of(inside('XE-PL'))
        # A version answers by where its samples were: one moved off the land near longitude 0 afterwards.
        moved = await r.call(
            ADMIN,
            'PATCH',
            f"/api/datasets/{D}/samples/{r.ids['p23']}",
            json={'location': {'type': 'Point', 'coordinates': [-0.1, -60.0]}},
        )
        assert moved.status_code == 200, moved.text
        assert await selected(r, keep('XE-PL')) == r.of(inside('XE-PL') - {'p23'})
        for key in ROUND_KEYS:
            for node, names in ((keep(key), inside(key)), (drop(key), set(NAMES) - inside(key))):
                rows = await post(
                    r,
                    f'/api/datasets/{D}/versions/{version}/samples/query',
                    {'filters': node, 'limit': 1000},
                )
                assert rows.status_code == 200, rows.text
                assert {item['id'] for item in rows.json()['items']} == r.of(names), (key, node['operator'])

    regions_world(scenario)


def grain(base: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """A grid's filter as the web app wraps it on a dataset with objects (FilterContext.withGrain): the row matches,
    or its partner in the other grain does."""
    node = {'logic': 'AND', 'filters': base}
    cross = 'parent_matches' if kind == 'object' else 'has_matching_object'
    return {
        'logic': 'AND',
        'filters': [
            {'field': 'media_type', 'operator': '=' if kind == 'object' else '!=', 'value': 'object'},
            {
                'logic': 'OR',
                'filters': [
                    {'field': '_cgself', 'operator': 'cg_self', 'value': node},
                    {'field': '_crossgrain', 'operator': cross, 'value': node},
                ],
            },
        ],
    }


def excluded(base: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """An excluded region as the app couples it: NOT (the row or its partner is inside)."""
    node = {'logic': 'AND', 'filters': base}
    cross = 'parent_matches' if kind == 'object' else 'has_matching_object'
    return {
        'logic': 'AND',
        'filters': [
            {'field': 'media_type', 'operator': '=' if kind == 'object' else '!=', 'value': 'object'},
            {
                'logic': 'NOT',
                'filters': [
                    {
                        'logic': 'OR',
                        'filters': [
                            {'field': '_cgself', 'operator': 'cg_self', 'value': node},
                            {'field': '_crossgrain', 'operator': cross, 'value': node},
                        ],
                    },
                ],
            },
        ],
    }


def test_on_a_dataset_with_objects_the_region_couples_images_and_their_objects(regions_world):
    async def scenario(r):
        await r.dataset(OBJ)
        point = {'type': 'Point', 'coordinates': [2.5, 2.5]}  # in XA-NM
        far = {'type': 'Point', 'coordinates': [32, 2.5]}  # in XB-NO
        images = [{'filepath': 'img-1.jpg', 'location': point}, {'filepath': 'img-2.jpg', 'location': far}]
        images.append({'filepath': 'img-3.jpg'})
        await datasets.add_samples(OBJ, {'samples': images}, user=ADMIN_USER)
        rows = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', OBJ)
        image = {row['filepath']: row['id'] for row in rows}
        objects = [
            {'filepath': 'obj-1.jpg', 'media_type': 'object', 'metadata': {'parent_id': image['img-1.jpg']}},
            {'filepath': 'obj-2.jpg', 'media_type': 'object', 'metadata': {'parent_id': image['img-2.jpg']}},
            {
                'filepath': 'obj-3.jpg',
                'media_type': 'object',
                'metadata': {'parent_id': image['img-3.jpg']},
                'location': point,
            },
        ]
        await datasets.add_samples(OBJ, {'samples': objects}, user=ADMIN_USER)
        rows = await r.database.fetch('SELECT id, filepath, parent_id FROM samples WHERE dataset_id = $1', OBJ)
        ids = {row['filepath'].split('.')[0]: row['id'] for row in rows}
        assert {row['filepath']: row['parent_id'] for row in rows}['obj-3.jpg'] == image['img-3.jpg']
        cases = [
            ('image', grain, {'img-1', 'img-3'}),  # inside, or holding an object inside
            ('object', grain, {'obj-1', 'obj-3'}),  # inside, or on an image inside
            ('image', excluded, {'img-2'}),
            ('object', excluded, {'obj-2'}),
        ]
        # A map region drawn around the state's inside selects these too (no sample here is on its border).
        drawn = {'field': 'location', 'operator': 'geo_polygon', 'value': [[0, 0], [5, 0], [5, 5], [0, 5]]}
        for kind, wrap, names in cases:
            assert await selected(r, wrap([drawn], kind), OBJ) == {ids[name] for name in names}, (kind, names)
        await loaded(r)
        for kind, wrap, names in cases:
            assert await selected(r, wrap([keep('XA-NM')], kind), OBJ) == {ids[name] for name in names}, (kind, names)

    regions_world(scenario)


# --- The filter as text ---


def test_location_within_parses_formats_and_selects_as_the_filter_does(regions_world):
    async def scenario(r):
        await farm(r)
        await loaded(r)
        for query, node in (
            ('location within "XA-NM"', keep('XA-NM')),
            ('location within XA-NM', keep('XA-NM')),
            ('location not within "XA-NM"', drop('XA-NM')),
            ('location WITHIN "xa-nm"', keep('XA-NM')),  # the keyword and the key in any case
            ('location Within Xa-Nm', keep('XA-NM')),
            ('location NOT WITHIN "xa-nm"', drop('XA-NM')),
            ('location within "xa:MIDLANDS"', keep('XA:midlands')),
            ('location not within "xbr+00?"', drop('XBR+00?')),
            ('location within "XC:iles-du-sud"', keep('XC:iles-du-sud')),
        ):
            assert await nodes(r, query) == [node], query
        # A field name is case sensitive, location too.
        error = mistake(await parse(r, 'LOCATION within "XA-NM"'), line=1)
        assert "unknown field 'LOCATION'" in error['message'], error
        for key in KEYS:
            for node, written in ((keep(key), f'location within "{key}"'), (drop(key), f'location not within "{key}"')):
                assert await text_of(r, node) == written, key
                assert await nodes(r, written) == [node], written
        # The text selects what the filter selects, alone and combined.
        a = {'field': 'grp', 'operator': '=', 'value': 'a'}
        b = {'field': 'grp', 'operator': '=', 'value': 'b'}
        for query, node in (
            ('location within Isla', keep('XA-IS')),
            ('location not within "XA"', drop('XA')),
            ('location within "XA" AND grp = "a"', {'logic': 'AND', 'filters': [keep('XA'), a]}),
            (
                'location not within "XA:cote-elevee" OR grp = "b"',
                {'logic': 'OR', 'filters': [drop('XA:cote-elevee'), b]},
            ),
            (
                'NOT (location within "XA-NM" OR location within XB)',
                {'logic': 'NOT', 'filters': [{'logic': 'OR', 'filters': [keep('XA-NM'), keep('XB')]}]},
            ),
        ):
            filters = await nodes(r, query)
            assert await selected(r, {'logic': 'AND', 'filters': filters}) == await selected(r, node), query

    regions_world(scenario)


def test_a_region_name_is_accepted_in_place_of_its_key(regions_world):
    async def scenario(r):
        await farm(r)
        await loaded(r)
        for query, node in (
            ('location within Isla', keep('XA-IS')),
            ('location within ISLA', keep('XA-IS')),
            ('location not within isla', drop('XA-IS')),
            ('location within Northmarch', keep('XA-NM')),  # its name_en
            ('location within "Fen and Mere"', keep('XB:fen-and-mere')),
            ('location within "Côte Élevée"', keep('XA:cote-elevee')),
            ('location within Lakemere', keep('XBR-102')),
            ('location within mere', keep('MERE')),  # a key match wins over the state named Mere
            ('location within "XD-NN"', keep('XD-NN')),  # a key that is also its name
        ):
            assert await nodes(r, query) == [node], query
        # Two regions named Brenmark: the country and a state.
        error = mistake(await parse(r, 'location within Brenmark'), line=1)
        assert error['column'] == 17, error
        assert set(error['suggestions']) == {'XB', 'XA-BK'}, error
        for word in ('XB', 'XA-BK', 'country', 'state'):
            assert word in error['message'], (word, error)
        error = mistake(await parse(r, 'grp = "a"\nAND location within "Brenmark"'), line=2)
        assert error['column'] == 21 and set(error['suggestions']) == {'XB', 'XA-BK'}, error
        # No region of that key or name.
        error = mistake(await parse(r, 'location within "XA-NX"'), line=1)
        assert error['column'] == 17, error
        assert 'XA-NM' in error['suggestions'] and set(error['suggestions']) <= set(KEYS), error
        for query in ('location within Isl', 'location within "Islas"', 'location within Atlantis'):
            error = mistake(await parse(r, query), line=1)
            assert set(error['suggestions']) <= set(KEYS), error

    regions_world(scenario)


def test_within_and_location_are_names_too(regions_world):
    """`within` names a field or a value; `location` a dataset's own field, exactly as before beside region tests."""

    async def scenario(r):
        await farm(r)
        # The dataset with a field named location (a text), beside the samples' own locations.
        await r.dataset(LOC)
        located = {'type': 'Point', 'coordinates': [2.5, 2.5]}  # in XA-NM
        samples: list[dict[str, Any]] = [
            {'filepath': 'l1.jpg', 'metadata': {'location': 'barn'}, 'location': located},
            {
                'filepath': 'l2.jpg',
                'metadata': {'location': 'field'},
                'location': {'type': 'Point', 'coordinates': [32, 2.5]},
            },
            {'filepath': 'l3.jpg', 'metadata': {'location': 'barnyard'}},
            {'filepath': 'l4.jpg', 'metadata': {}, 'location': located},
            {'filepath': 'l5.jpg', 'metadata': {}},
        ]
        await datasets.add_samples(LOC, {'samples': samples}, user=ADMIN_USER)
        rows = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', LOC)
        ids = {row['filepath'].split('.')[0]: row['id'] for row in rows}
        # As before: a comparison reads the field; `exists` asks whether the sample has a location.
        for query, node, written, names in (
            ('location = "barn"', {'operator': '=', 'value': 'barn'}, 'location = "barn"', {'l1'}),
            (
                'location in ("barn", "field")',
                {'operator': 'in', 'value': ['barn', 'field']},
                'location in ("barn", "field")',
                {'l1', 'l2'},
            ),
            ('location exists', {'operator': 'exists', 'value': None}, 'location exists', {'l1', 'l2', 'l4'}),
            (
                'location contains "barn"',
                {'operator': 'contains', 'value': 'barn'},
                'location contains "barn"',
                {'l1', 'l3'},
            ),
        ):
            field_node = {'field': 'location', **node}
            assert await nodes(r, query, LOC) == [field_node], query
            assert await text_of(r, field_node, LOC) == written, query
            assert await selected(r, field_node, LOC) == {ids[name] for name in names}, query
        # The farm: a field and a value named within.
        within = {'field': 'within', 'operator': '=', 'value': 'within'}
        assert await nodes(r, 'within = within') == [within]
        assert await nodes(r, 'note = within') == [{'field': 'note', 'operator': '=', 'value': 'within'}]
        assert await text_of(r, within) == 'within = "within"'
        assert await selected(r, within) == r.of({'p01'})

        await loaded(r)
        for query, names in (
            ('location contains "barn" AND location within "XA-NM"', {'l1'}),
            ('location exists AND location not within "XA-NM"', {'l2'}),
            ('location = "field" OR location within XA-NM', {'l1', 'l2', 'l4'}),
        ):
            filters = await nodes(r, query, LOC)
            assert await selected(r, {'logic': 'AND', 'filters': filters}, LOC) == {ids[name] for name in names}, query
        filters = await nodes(r, 'within = within AND location within "XA-NM"')
        assert filters == [within, keep('XA-NM')]
        assert await selected(r, {'logic': 'AND', 'filters': filters}) == r.of({'p01'})
        # Without a field named location, a comparison on it says to use within.
        error = mistake(await parse(r, 'location = "XA-NM"'), line=1)
        assert 'within' in error['message'], error
        # within after any other field.
        for query in ('plot within "XA-NM"', 'grp within Isla', 'within within "XA-NM"'):
            mistake(await parse(r, query), line=1)

    regions_world(scenario)


# --- Followers ---


def test_a_relayed_follower_search_refuses_a_region_filter():
    from backend import follower_search

    for node in (
        keep('XA-NM'),
        drop('XA-NM'),
        {'logic': 'AND', 'filters': [{'field': 'grp', 'operator': '=', 'value': 'a'}, keep('XA')]},
        {'logic': 'NOT', 'filters': [{'logic': 'OR', 'filters': [drop('XB')]}]},
    ):
        with pytest.raises(follower_search.RelayRefused):
            follower_search.check_filters(node)


# --- The upgrade ---

RELEASE = 'v2.13.1'  # the last release before 0034
UPGRADE_SEED = """
INSERT INTO datasets (id, name, description) VALUES (1, 'farm', 'fields and plots'), (2, 'empty', NULL);
SELECT setval(pg_get_serial_sequence('datasets', 'id'), 2);
INSERT INTO samples (dataset_id, filepath, media_type, metadata, tags, location) VALUES
    (1, 's3://upgrade/a.jpg', 'image', '{"plot": "a", "score": 0.5}', '{seen}',
     ST_SetSRID(ST_MakePoint(2.5, 2.5), 4326)),
    (1, 's3://upgrade/b.jpg', 'image', '{"plot": "b", "location": "barn"}', '{}',
     ST_SetSRID(ST_MakePoint(-175, -5), 4326)),
    (1, 's3://upgrade/c.jpg', 'image', '{"plot": "c"}', '{}', NULL);
INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, is_promoted, display_type) VALUES
    (1, 'plot', 'string', 'metadata', FALSE, NULL),
    (1, 'score', 'number', 'metadata', TRUE, 'enum'),
    (1, 'location', 'string', 'metadata', FALSE, NULL);
"""
TABLES = "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public'"
COLUMNS = (
    "SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = '{table}'"
)


MIGRATIONS = ROOT / 'backend/db/migrations'


def apply_migration(psql: Any, name: str, migration: Path) -> None:
    """One migration file as scripts/apply_migrations.sh applies it: in one transaction with its ledger row."""
    ledger = f"INSERT INTO schema_migrations (filename) VALUES ('{migration.name}');"
    psql(name, f'BEGIN;\n{migration.read_text()}\n{ledger}\nCOMMIT;\n')


def apply_before(psql: Any, name: str, number: str) -> None:
    """The migrations numbered below `number` the database doesn't record, in the runner's (byte) order."""
    recorded = set(psql(name, 'SELECT filename FROM schema_migrations').splitlines())
    for migration in sorted(MIGRATIONS.glob('*.sql'), key=lambda found: found.name.encode()):
        if migration.name < number and migration.name not in recorded:
            apply_migration(psql, name, migration)


def tables_as_they_are(psql: Any, name: str, tables: set[str]) -> dict[str, tuple[str, str]]:
    """Each table's columns, and its rows read on those columns (the migrations' ledger aside)."""
    found = {}
    for table in sorted(tables - {'schema_migrations'}):
        listed = psql(name, COLUMNS.format(table=table))
        rows = psql(
            name,
            f"SELECT md5(coalesce(string_agg(r::text, E'\\n' ORDER BY r::text), '')) "
            f'FROM (SELECT {listed} FROM {table}) r',
        )
        found[table] = (listed, rows)
    return found


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'), reason='Requires disposable PostgreSQL')
def test_0034_adds_empty_region_tables_and_changes_nothing_else(database, monkeypatch):
    make, psql, _, migrate = database
    old_schema = subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{RELEASE}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = make(old_schema + UPGRADE_SEED)
    seeded = psql(name, "SELECT string_agg(r::text, E'\\n' ORDER BY r::text) FROM (SELECT * FROM samples) r")
    assert seeded.count('\n') == 2 and 'barn' in seeded
    # 0034 on its own, after whatever comes before it: what it adds, and that it changes nothing that was there.
    apply_before(psql, name, '0034')
    tables_before = set(psql(name, TABLES).split(','))
    before = tables_as_they_are(psql, name, tables_before)
    (own,) = MIGRATIONS.glob('0034_*.sql')
    apply_migration(psql, name, own)
    assert tables_as_they_are(psql, name, tables_before) == before  # every table's columns and rows, as they were
    added = set(psql(name, TABLES).split(',')) - tables_before
    assert any('region' in table for table in added), added
    for table in sorted(added):
        assert psql(name, f'SELECT count(*) FROM {table}') == '0', table
    # The rest of the way, as an upgrade runs: the app then reads the upgraded database.
    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'

    async def after() -> None:
        url = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()
        pool = await asyncpg.create_pool(url, min_size=1, max_size=4, init=init_connection)
        monkeypatch.setattr(database_module.db, 'pool', pool)
        monkeypatch.setattr(datasets, '_array_columns_cache', None)
        try:
            upgraded = Database()
            upgraded.pool = pool
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                w = World(upgraded, client)
                await w.user(ADMIN, 'admin')
                sets = (await w.call(ADMIN, 'GET', '/api/admin/regions')).json()['sets']
                assert [item['loaded'] for item in sets if item['name'] == SET] == [None], sets
                assert (await w.call(ADMIN, 'GET', '/api/regions', params={'search': 'XA'})).json() == []
                assert (await w.call(ADMIN, 'GET', '/api/regions/XA-NM')).status_code == 404
                for node in (keep('XA-NM'), drop('XA-NM')):
                    response = await w.call(ADMIN, 'POST', '/api/datasets/1/samples/query', json={'filters': node})
                    message = refusal(response)
                    assert all(word in message.lower() for word in NOT_LOADED), message
                parsed = await w.call(
                    ADMIN,
                    'POST',
                    '/api/datasets/1/filter_query/parse',
                    json={'query': 'location within "XA-NM"', 'current': []},
                )
                assert parsed.status_code == 400, parsed.text
                # The field named location reads as it did.
                plain = await w.call(
                    ADMIN,
                    'POST',
                    '/api/datasets/1/samples/query',
                    json={'filters': {'field': 'location', 'operator': '=', 'value': 'barn'}},
                )
                assert plain.status_code == 200 and plain.json()['total'] == 1, plain.text
        finally:
            await pool.close()

    asyncio.run(after())
