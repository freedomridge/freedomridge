"""Named regions: countries, states or provinces, and named groups of states, loaded from public boundary data
(docs/REGIONS.md). The `geo_region` filter keeps the samples inside one, `GET /api/regions` finds them by name.

Boundaries come with the install: a set's pinned file sits in the boundaries directory (boundaries_dir: the Docker
image carries it, a source checkout runs `python -m backend.regions fetch`), and when the job runner starts it
queues the load of each set this database has never loaded (queue_bundled_loads). An administrator's load
(POST /api/admin/regions/load, a job) reads the same file, or downloads it when it isn't there, and a server with
no internet can load a file from anywhere on disk:

    python -m backend.regions load ne_10m_admin_1_states_provinces.geojson --set natural-earth-admin1

Every way the file is checked against the set's pinned SHA-256 and size limit, and loading a set again replaces
that set's regions and nothing else. PostGIS does the geometry: each region is kept whole, for its outline and
bounding box, and cut into small pieces (region_parts) that the filter tests a sample's location against.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import asyncpg
from fastapi import HTTPException

from . import diskspace, job_queue
from .filter_query import RegionMatch, canonical_region_key
from .job_queue import JobPolicy
from .job_runner import StepResult
from .job_wait import JobInputError, JobInsufficientStorage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BoundarySet:
    name: str
    title: str
    version: str
    url: str
    sha256: str
    max_bytes: int
    licence: str


SETS: Dict[str, BoundarySet] = {
    'natural-earth-admin1': BoundarySet(
        name='natural-earth-admin1',
        title='Natural Earth 1:10m admin 1 states and provinces',
        version='5.1.1',
        # The tagged release in Natural Earth's vector repository: a tag never moves, so the checksum holds.
        url=(
            'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.1/geojson/'
            'ne_10m_admin_1_states_provinces.geojson'
        ),
        sha256='22d0e3ad85eb3e27f17cabf8ba2d50e554fbc27a87796ff891d958185da62fb5',
        max_bytes=64 * 1024 * 1024,  # the file is 40.7 MB
        licence='Public domain (Natural Earth, naturalearthdata.com)',
    ),
}
KINDS = ('country', 'state', 'group')
KIND = 'region_load'
# Taken by each load of the set $1, in its transaction: one at a time.
SET_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('regions:' || $1, 0))"
# One step reads (or downloads) a set's file and loads it; the unions of the large countries take most of it.
POLICY = JobPolicy(max_unit_seconds=1800, max_failures=2, retry_seconds=30, max_retry_seconds=300)
DOWNLOAD_TIMEOUT_SECONDS = 300
# What a load asks the free-space gate for, besides the gate's own headroom: 5.1.1's region tables come to 151 MB, and
# the load's WAL and temporary table about as much again while it runs.
LOAD_FREE_BYTES = 500 * 1024**2
# How long queue_bundled_loads waits, in all, while another runner holds the queue's lock.
CONTENDED_SECONDS = 2.0
# The pieces the filter tests a location against: at most this many vertices each (ST_Subdivide's default).
PIECE_VERTICES = 256
# The cells the per-row test looks a location up in: a quarter of a degree, a power of two so a cell's edges and
# floor(longitude * 4) agree exactly. All of 5.1.1's regions make 936,220 cells (40 MB).
CELLS_PER_DEGREE = 4
# An outline for a map: simplified until it has at most this many vertices, coordinates to 5 decimals (about 1 m),
# so the answer stays under about 120 KB whatever the region.
OUTLINE_VERTICES = 5000

_ISO_3166_2 = re.compile(r'[A-Z]{2}-[A-Z0-9]{1,3}\Z')
_ISO_A2 = re.compile(r'[A-Z]{2}\Z')
# Letters NFKD leaves whole, folded by hand.
_FOLD = str.maketrans(
    {
        'Đ': 'D',
        'đ': 'd',
        'Ð': 'D',
        'ð': 'd',
        'Ø': 'O',
        'ø': 'o',
        'Ł': 'L',
        'ł': 'l',
        'ı': 'i',
        'ß': 'ss',
        'Æ': 'AE',
        'æ': 'ae',
        'Œ': 'OE',
        'œ': 'oe',
        'Þ': 'Th',
        'þ': 'th',
    },
)


class RegionLoadError(ValueError):
    """A file that can't be loaded as this set: the message says why."""


def slug(text: str) -> str:
    """`text` with its letters folded to ASCII (accents stripped), lower-cased, and each run of anything else but a-z
    and 0-9, ASCII or not (dashes of any kind, spaces, punctuation), one hyphen: 'Midwest' -> 'midwest', 'Grand Est'
    -> 'grand-est', 'Đông Bắc' -> 'dong-bac', 'Îles—du  Sud!' -> 'iles-du-sud'."""
    decomposed = unicodedata.normalize('NFKD', text.translate(_FOLD))
    folded = ''.join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r'[^a-z0-9]+', '-', folded.lower()).strip('-')


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass
class Plan:
    """What a boundary file makes: each state (by its feature's index), each country and each group, with the
    decisions the file forced (`report`)."""

    states: List[Dict[str, Any]] = field(default_factory=list)  # index, key, name, country_key, group_key
    countries: Dict[str, str] = field(default_factory=dict)  # key -> name
    groups: Dict[str, Tuple[str, str]] = field(default_factory=dict)  # key -> (name, country key)
    report: Dict[str, Any] = field(default_factory=dict)


def plan_regions(features: Sequence[Optional[Dict[str, Any]]]) -> Plan:
    """The regions Natural Earth admin-1 features make, from their properties (`features[i]` is feature i's, None
    for a feature with no geometry). Keys, names and countries follow docs/REGIONS.md."""
    plan = Plan()
    report: Dict[str, Any] = {'features': len(features)}
    iso_counts = Counter(_text(p.get('iso_3166_2')) for p in features if p is not None)
    country_names: Dict[str, Counter] = defaultdict(Counter)
    geonunits: Dict[str, Counter] = defaultdict(Counter)
    feature_names: Dict[str, List[str]] = defaultdict(list)
    group_texts: Dict[str, Counter] = defaultdict(Counter)
    no_geometry, no_country, unnamed, no_key = [], [], [], []
    merged_keys: Dict[str, List[str]] = {}
    keyed: Dict[str, Dict[str, Any]] = {}
    damaged_groups: Dict[str, str] = {}
    duplicated_iso: Dict[str, int] = {}
    malformed_iso: List[str] = []
    from_adm0: set = set()
    for index, properties in enumerate(features):
        if properties is None:
            no_geometry.append(index)
            continue
        iso_a2, adm0_a3 = _text(properties.get('iso_a2')), _text(properties.get('adm0_a3'))
        if iso_a2 and _ISO_A2.match(iso_a2):
            country_key = iso_a2
        elif adm0_a3:
            country_key = adm0_a3.upper()
            from_adm0.add(country_key)
        else:
            no_country.append(index)
            continue
        iso, adm1 = _text(properties.get('iso_3166_2')), _text(properties.get('adm1_code'))
        key: Optional[str]
        if iso and _ISO_3166_2.match(iso) and iso_counts[iso] == 1:
            key = iso
        else:
            if iso and _ISO_3166_2.match(iso):
                duplicated_iso[iso] = iso_counts[iso]
            elif iso:
                malformed_iso.append(iso)
            key = adm1.upper() if adm1 else None
        if key is None:
            no_key.append(index)
            continue
        name = _text(properties.get('name_en')) or _text(properties.get('name'))
        if key in keyed:
            # A second feature with the key after the fallback: its land joins the first's, never dropped.
            keyed[key]['merged'].append(index)
            merged_keys.setdefault(key, [keyed[key]['name']]).append(name or f'feature {index}')
            continue
        if name is None:
            unnamed.append(key)
            name = key
        country_names[country_key][_text(properties.get('admin')) or country_key] += 1
        geonunits[country_key][_text(properties.get('geonunit'))] += 1
        feature_names[country_key].append(name)
        group_text = _text(properties.get('region'))
        group_key = None
        if group_text and ('?' in group_text or '\ufffd' in group_text):
            # Damaged in the file (Vietnam's 'Ðông B?c' beside 'Đông Bắc'): no group for this feature.
            damaged_groups[key] = group_text
        elif group_text and slug(group_text):
            group_key = f'{country_key}:{slug(group_text)}'
            group_texts[group_key][group_text] += 1
        keyed[key] = {
            'index': index,
            'key': key,
            'name': name,
            'country_key': country_key,
            'group_key': group_key,
            'merged': [],
        }
        plan.states.append(keyed[key])
    several_names = {}
    for country_key, names in sorted(country_names.items()):
        # The name most of its features give; names tied for most are joined ('Gaza Strip and West Bank').
        most = max(names.values())
        plan.countries[country_key] = ' and '.join(sorted(name for name, count in names.items() if count == most))
        if len(names) > 1:
            several_names[country_key] = sorted(names)
    renamed = _unique_country_names(plan.countries, country_names, geonunits, feature_names)
    merged = {}
    for group_key, texts in sorted(group_texts.items()):
        plan.groups[group_key] = (texts.most_common(1)[0][0], group_key.split(':', 1)[0])
        if len(texts) > 1:
            merged[group_key] = sorted(texts)
    report.update(
        counts={
            'country': len(plan.countries),
            'state': len(plan.states),
            'group': len(plan.groups),
        },
        state_keys={
            'iso_3166_2': sum(1 for state in plan.states if _ISO_3166_2.match(state['key'])),
            'adm1_code': sum(1 for state in plan.states if not _ISO_3166_2.match(state['key'])),
        },
        iso_3166_2_duplicated=dict(sorted(duplicated_iso.items())),
        iso_3166_2_malformed=len(malformed_iso),
        iso_3166_2_malformed_examples=sorted(malformed_iso)[:10],
        country_keys_from_adm0_a3=sorted(from_adm0),
        countries_with_several_names=several_names,
        countries_renamed=renamed,
        unnamed_states=unnamed,
        no_geometry=no_geometry,
        no_country=no_country,
        no_key=no_key,
        keys_merged=merged_keys,
        groups_by_country=dict(sorted(Counter(country for _, country in plan.groups.values()).items())),
        group_names_merged=merged,
        group_names_damaged=damaged_groups,
    )
    plan.report = report
    return plan


def _unique_country_names(
    countries: Dict[str, str],
    admins: Dict[str, Counter],
    geonunits: Dict[str, Counter],
    feature_names: Dict[str, List[str]],
) -> Dict[str, Dict[str, Any]]:
    """Rename, in place, each country whose name another country key has too (Tokelau's feature says New Zealand):
    the one with the most features keeps it, the others take their features' geonunit when it differs, else their
    single feature's name, else their key. Returns what was renamed and what the file offered."""
    holders: Dict[str, List[str]] = defaultdict(list)
    for key, name in countries.items():
        holders[name].append(key)
    renamed: Dict[str, Dict[str, Any]] = {}
    for name, keys in sorted(holders.items()):
        for key in sorted(keys, key=lambda key: (-len(feature_names[key]), key))[1:]:
            taken = set(countries.values())
            units = [unit for unit, _ in geonunits[key].most_common() if unit and unit != name]
            offered = units[:1] + (feature_names[key] if len(feature_names[key]) == 1 else [])
            countries[key] = next((choice for choice in offered if choice not in taken), key)
            renamed[key] = {
                'name': countries[key],
                'instead_of': name,
                'admin': sorted(admins[key]),
                'geonunit': sorted(unit for unit in geonunits[key] if unit),
                'feature_names': sorted(feature_names[key])[:5],
            }
    return renamed


def longitude_span(extents: Iterable[Tuple[float, float]]) -> Tuple[float, float]:
    """[west, east] of polygons with these longitude extents (Natural Earth's never cross ±180): the way round the
    globe that leaves out its widest stretch with no polygon, west > east when that way crosses the antimeridian (as
    in GeoJSON), and [-180, 180] when no stretch is left (Antarctica). Russia: [19.6, -169]. A region's own extreme
    longitudes would mistake a band from -170 to 170 for one across the antimeridian, or Antarctica for one with a
    sliver missing at 0."""
    merged: List[List[float]] = []
    for start, end in sorted(extents):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    wrap = merged[0][0] + 360 - merged[-1][1]  # from the last polygon east round to the first
    gap, after = max(((merged[i + 1][0] - merged[i][1], i) for i in range(len(merged) - 1)), default=(0.0, -1))
    if wrap <= 0 and gap <= 0:
        return -180.0, 180.0
    if wrap >= gap:
        return merged[0][0], merged[-1][1]
    return merged[after + 1][0], merged[after][1]


def check_file(data: bytes, boundary_set: BoundarySet, *, verify: bool = True) -> str:
    """The file's SHA-256, after checking its size and, with `verify`, that it is the set's pinned file."""
    if len(data) > boundary_set.max_bytes:
        raise RegionLoadError(f'The file is {len(data):,} bytes, over the limit of {boundary_set.max_bytes:,}')
    digest = hashlib.sha256(data).hexdigest()
    if verify and digest != boundary_set.sha256:
        raise RegionLoadError(
            f'The file\'s SHA-256 is {digest}, not {boundary_set.sha256} ({boundary_set.title} '
            f'{boundary_set.version}). Load the pinned file, or pass --no-verify for one you vouch for',
        )
    return digest


# A feature's properties the plan reads, from the file the load holds in the database.
_PROPERTIES = ('iso_3166_2', 'adm1_code', 'iso_a2', 'adm0_a3', 'admin', 'geonunit', 'name_en', 'name', 'region')


async def load(
    connection: asyncpg.Connection,
    boundary_set: BoundarySet,
    data: bytes,
    *,
    version: str,
    sha256: str,
    verified: bool,
    loaded_from: str,
    user_id: Optional[int],
) -> Dict[str, Any]:
    """Replace `boundary_set`'s regions with the ones the GeoJSON `data` makes, in one transaction, and return what
    the load did: the counts of each kind, the decisions the file forced, and the keys the set no longer has (saved
    views naming one are refused until they change)."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as error:
        raise RegionLoadError(f'The file is not UTF-8 text: {error}') from None
    async with connection.transaction():
        # One load of a set at a time; readers keep the old regions until this commits.
        await connection.execute(SET_LOCK_SQL, boundary_set.name)
        await connection.execute(
            'CREATE TEMP TABLE region_load_features (index INTEGER PRIMARY KEY, properties JSONB, geometry JSONB) '
            'ON COMMIT DROP',
        )
        try:
            await connection.execute(
                """INSERT INTO region_load_features
                   SELECT ordinality - 1, feature->'properties', feature->'geometry'
                   FROM jsonb_array_elements($1::text::jsonb->'features') WITH ORDINALITY AS f(feature, ordinality)""",
                text,
            )
        except (asyncpg.DataError, asyncpg.InvalidTextRepresentationError) as error:
            raise RegionLoadError(f'The file is not a GeoJSON FeatureCollection: {error}') from None
        rows = await connection.fetch(
            f"""SELECT index, jsonb_typeof(geometry) = 'object' AS located,
                       {', '.join(f"properties->>'{name}' AS {name}" for name in _PROPERTIES)}
                FROM region_load_features ORDER BY index""",
        )
        if not rows:
            raise RegionLoadError('The file has no features')
        plan = plan_regions([dict(row) if row['located'] else None for row in rows])
        states = plan.states
        before = {
            row['key']
            for row in await connection.fetch('SELECT key FROM regions WHERE set_name = $1', boundary_set.name)
        }
        await connection.execute(
            """INSERT INTO region_sets (name, version, sha256, verified, loaded_from, loaded_by, loaded_at)
               VALUES ($1, $2, $3, $4, $5, $6, now())
               ON CONFLICT (name) DO UPDATE SET version = $2, sha256 = $3, verified = $4, loaded_from = $5,
                   loaded_by = $6, loaded_at = now()""",
            boundary_set.name,
            version,
            sha256,
            verified,
            loaded_from,
            user_id,
        )
        await connection.execute('DELETE FROM regions WHERE set_name = $1', boundary_set.name)
        # Each state's geometry made valid and kept to its polygons. A feature whose polygons vanish is left out.
        repaired = await connection.fetchrow(
            """WITH raw AS (
                   SELECT p.key, p.name, p.country_key, p.group_key,
                          ST_SetSRID(ST_GeomFromGeoJSON(f.geometry), 4326) AS geom
                   FROM unnest($2::int[], $3::text[], $4::text[], $5::text[], $6::text[])
                        AS p(index, key, name, country_key, group_key)
                   JOIN region_load_features f USING (index)
               ), made AS (
                   SELECT *, ST_IsValid(geom) AS valid,
                          ST_Multi(ST_CollectionExtract(ST_MakeValid(geom), 3)) AS polygons
                   FROM raw
               ), stored AS (
                   INSERT INTO regions (set_name, kind, key, name, country_key, country_name, west, south, east,
                                        north, geom)
                   SELECT $1, 'state', key, name, country_key, '', 0, 0, 0, 0, polygons
                   FROM made WHERE NOT ST_IsEmpty(polygons)
                   RETURNING key
               )
               SELECT count(*) FILTER (WHERE NOT valid) AS repaired,
                      array_agg(key ORDER BY key) FILTER (WHERE key NOT IN (SELECT key FROM stored)) AS empty
               FROM made""",
            boundary_set.name,
            [state['index'] for state in states],
            [state['key'] for state in states],
            [state['name'] for state in states],
            [state['country_key'] for state in states],
            [state['group_key'] for state in states],
        )
        # A feature whose key another one had (plan_regions) adds its land to that state.
        merged = [(state['key'], index) for state in states for index in state['merged']]
        await connection.execute(
            """UPDATE regions r SET geom = ST_Multi(ST_CollectionExtract(ST_Union(r.geom, extra.geom), 3))
               FROM (SELECT m.key, ST_Union(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(f.geometry), 4326))) AS geom
                     FROM unnest($2::text[], $3::int[]) AS m(key, index) JOIN region_load_features f USING (index)
                     GROUP BY m.key) extra
               WHERE r.set_name = $1 AND r.key = extra.key""",
            boundary_set.name,
            [key for key, _ in merged],
            [index for _, index in merged],
        )
        group_of = {state['key']: state['group_key'] for state in states}
        # Countries and groups: the union of their states, made here once.
        await connection.execute(
            """INSERT INTO regions (set_name, kind, key, name, country_key, country_name, west, south, east, north,
                                   geom)
               SELECT $1, 'country', c.key, c.name, c.key, c.name, 0, 0, 0, 0,
                      ST_Multi(ST_CollectionExtract(ST_Union(s.geom), 3))
               FROM unnest($2::text[], $3::text[]) AS c(key, name)
               JOIN regions s ON s.set_name = $1 AND s.kind = 'state' AND s.country_key = c.key
               GROUP BY c.key, c.name""",
            boundary_set.name,
            list(plan.countries),
            list(plan.countries.values()),
        )
        await connection.execute(
            """INSERT INTO regions (set_name, kind, key, name, country_key, country_name, west, south, east, north,
                                   geom)
               SELECT $1, 'group', g.key, g.name, g.country_key, '', 0, 0, 0, 0,
                      ST_Multi(ST_CollectionExtract(ST_Union(s.geom), 3))
               FROM unnest($2::text[], $3::text[], $4::text[]) AS g(key, name, country_key)
               JOIN unnest($5::text[], $6::text[]) AS m(state_key, group_key) ON m.group_key = g.key
               JOIN regions s ON s.set_name = $1 AND s.kind = 'state' AND s.key = m.state_key
               GROUP BY g.key, g.name, g.country_key""",
            boundary_set.name,
            list(plan.groups),
            [name for name, _ in plan.groups.values()],
            [country for _, country in plan.groups.values()],
            [key for key, group in group_of.items() if group],
            [group for group in group_of.values() if group],
        )
        # Each region's bounding box [west, south, east, north], its longitudes from its polygons' own (longitude_span),
        # and its country's name.
        extents = await connection.fetch(
            """SELECT r.id, array_agg(ST_XMin(d.geom)) AS starts, array_agg(ST_XMax(d.geom)) AS ends,
                      min(ST_YMin(d.geom)) AS south, max(ST_YMax(d.geom)) AS north
               FROM regions r, ST_Dump(r.geom) d WHERE r.set_name = $1 GROUP BY r.id""",
            boundary_set.name,
        )
        spans = [longitude_span(zip(row['starts'], row['ends'])) for row in extents]
        await connection.execute(
            """UPDATE regions r SET
                   country_name = (SELECT c.name FROM regions c WHERE c.set_name = $1 AND c.kind = 'country'
                                   AND c.key = r.country_key),
                   west = box.west, south = box.south, east = box.east, north = box.north
               FROM unnest($2::int[], $3::float8[], $4::float8[], $5::float8[], $6::float8[])
                    AS box(id, west, south, east, north)
               WHERE r.id = box.id""",
            boundary_set.name,
            [row['id'] for row in extents],
            [west for west, _ in spans],
            [row['south'] for row in extents],
            [east for _, east in spans],
            [row['north'] for row in extents],
        )
        await connection.execute(
            """INSERT INTO region_parts (region_id, geom)
               SELECT r.id, (ST_Dump(piece)).geom FROM regions r, ST_Subdivide(r.geom, $2) AS piece
               WHERE r.set_name = $1""",
            boundary_set.name,
            PIECE_VERTICES,
        )
        await connection.execute('ANALYZE region_parts')
        # A cell is inside when one piece covers it whole; one that only touches the region is on its border. By
        # the set's region ids: joined to regions instead, the planner rescanned every piece's cells per region. The
        # inner query (OFFSET 0) tests each piece and cell, so the grouping sorts four small columns, not geometries.
        await connection.execute(
            """INSERT INTO region_cells (region_id, i, j, inside)
               SELECT region_id, i, j, bool_or(covers) FROM (
                   SELECT p.region_id, cell.i, cell.j, ST_Covers(p.geom, cell.geom) AS covers
                   FROM region_parts p, ST_SquareGrid(1.0 / $2, p.geom) AS cell
                   WHERE p.region_id = ANY($1::int[]) AND ST_Intersects(p.geom, cell.geom)
                   OFFSET 0
               ) touching
               GROUP BY region_id, i, j""",
            [
                row['id']
                for row in await connection.fetch('SELECT id FROM regions WHERE set_name = $1', boundary_set.name)
            ],
            CELLS_PER_DEGREE,
        )
        counts = {
            row['kind']: row['count']
            for row in await connection.fetch(
                'SELECT kind, count(*) FROM regions WHERE set_name = $1 GROUP BY kind',
                boundary_set.name,
            )
        }
        after = {
            row['key']
            for row in await connection.fetch('SELECT key FROM regions WHERE set_name = $1', boundary_set.name)
        }
        report = {
            **plan.report,
            'counts': {kind: counts.get(kind, 0) for kind in KINDS},
            'geometries_repaired': repaired['repaired'],
            'empty_geometries': repaired['empty'] or [],
            'pieces': await connection.fetchval(
                'SELECT count(*) FROM region_parts p JOIN regions r ON r.id = p.region_id WHERE r.set_name = $1',
                boundary_set.name,
            ),
            'cells': await connection.fetchval(
                'SELECT count(*) FROM region_cells c JOIN regions r ON r.id = c.region_id WHERE r.set_name = $1',
                boundary_set.name,
            ),
            'version': version,
            'removed_keys': sorted(before - after),
            'added_keys': len(after - before),
        }
        await connection.execute(
            'UPDATE region_sets SET report = $2::text::jsonb WHERE name = $1',
            boundary_set.name,
            json.dumps(report),
        )
        await connection.execute('ANALYZE regions')
        await connection.execute('ANALYZE region_cells')
    return report


def row_inside_sql(key: str) -> str:
    """SQL true for a row whose own `location` is inside the region whose key is the text parameter `key` (`$3`), or
    on its border, for any rows (a version's included) and anywhere in a filter (under OR or NOT too): the bounding box
    (both halves across the antimeridian, which the location index can serve), then the location's cell, from the
    row's `lon` and `lat` (samples keeps them with `location`; ST_X and ST_Y read the point again, 1 us each). A cell
    inside the region answers at once, a cell on its border tests the location against the whole region (which
    PostGIS caches for the statement), and no cell means outside. The cells are two hashed sets made once per
    statement, sized by the region, so a sample costs a hash lookup: tested against the whole region instead, a
    location inside Canada's box took 5 us (412 polygons) and a count of Canada under OR on 2M samples 6.7 s."""
    region = f'(SELECT region_id({key}))'
    cell = f'(floor(lon * {CELLS_PER_DEGREE})::int, floor(lat * {CELLS_PER_DEGREE})::int)'
    cells = f'(SELECT i, j FROM region_cells WHERE region_id = {region} AND '
    return (
        # The second half of the box only across the antimeridian: FALSE, not NULL, otherwise, and NULL as the index
        # key, so its index scan reads nothing.
        f'((location && (SELECT ST_MakeEnvelope(west, south, CASE WHEN west > east THEN 180 ELSE east END, north, '
        f'4326) FROM regions WHERE id = {region}) OR ((SELECT west > east FROM regions WHERE id = {region}) '
        f'AND location && (SELECT ST_MakeEnvelope(-180, south, east, north, 4326) FROM regions '
        f'WHERE id = {region} AND west > east))) '
        f'AND ({cell} IN {cells}inside) OR ({cell} IN {cells}NOT inside) '
        f'AND ST_Intersects(location, (SELECT geom FROM regions WHERE id = {region})))))'
    )


def joined_inside_sql(key: str, id_column: str, dataset_id: Optional[int]) -> str:
    """The same test as a join from the region's pieces to the samples table: each piece finds the samples in its
    box through the location index, so the work follows the region's samples, not the dataset's size or the length
    of the region's coast. The filter compiler uses it, under NOT, for an exclude ANDed at the top of a statement on the
    samples table, where PostgreSQL runs it once as an anti-join; nested under OR or NOT it would run once per row,
    and for an include the per-row test is as fast or faster (2M samples, the United States: 0.85 s against 2.0 s,
    and 1.6 s against 5.1 s where the region holds most of the dataset). OFFSET 0 keeps
    the samples inside planned on their own: folded into the statement, a grid page (ORDER BY id LIMIT 50) of an
    exclude walked the dataset in id order testing every sample against the pieces (7.6 s on 2M samples)."""
    scope = f' AND _rs.dataset_id = {int(dataset_id)}' if dataset_id is not None else ''
    return (
        f'EXISTS (SELECT 1 FROM (SELECT _rs.id FROM region_parts _rp JOIN samples _rs ON ST_Intersects(_rp.geom, '
        f'_rs.location) WHERE _rp.region_id = (SELECT region_id({key})){scope} OFFSET 0) _inside '
        f'WHERE _inside.id = {id_column})'
    )


# --- Lookups -------------------------------------------------------------------------------------------------------

_COLUMNS = 'key, kind, name, country_key, country_name'


async def search(source, text: str, kind: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Regions matching `text`, case-insensitively: a key equal to it first, then names equal to it, names starting
    with it, names containing it; ties by kind (country, state, group), then name."""
    rows = await source.fetch(
        f"""SELECT {_COLUMNS} FROM (
                SELECT *, CASE WHEN lower(key) = lower($1) THEN 0
                               WHEN lower(name) = lower($1) THEN 1
                               WHEN starts_with(lower(name), lower($1)) THEN 2
                               WHEN strpos(lower(name), lower($1)) > 0 THEN 3 END AS rank
                FROM regions WHERE $2::text IS NULL OR kind = $2
            ) matches
            WHERE rank IS NOT NULL
            ORDER BY rank, array_position(ARRAY['country', 'state', 'group'], kind), name, key
            LIMIT $3""",
        text.strip(),
        kind,
        limit,
    )
    return [dict(row) for row in rows]


async def unknown(source, key: str) -> str:
    """What a request naming a region this server doesn't have is told, with close matches."""
    return await source.fetchval('SELECT unknown_region_message($1)', key)


async def region(source, key: str, *, with_outline: bool) -> Optional[Dict[str, Any]]:
    """The region with this key (in any case), its set and bounding box, and with `with_outline` a GeoJSON outline
    of at most OUTLINE_VERTICES vertices. None for a key no loaded set has."""
    row = await source.fetchrow(
        f"""SELECT r.id, {', '.join('r.' + column for column in _COLUMNS.split(', '))}, r.set_name, s.version,
                   r.west, r.south, r.east, r.north,
                   ST_NPoints(r.geom) AS points,
                   greatest(CASE WHEN r.west > r.east THEN r.east + 360 ELSE r.east END - r.west, r.north - r.south)
                       AS extent
            FROM regions r JOIN region_sets s ON s.name = r.set_name WHERE r.key = $1""",
        canonical_region_key(key),
    )
    if row is None:
        return None
    found = {column: row[column] for column in _COLUMNS.split(', ')}
    found.update(
        set=row['set_name'],
        version=row['version'],
        bbox=[round(row[side], 6) for side in ('west', 'south', 'east', 'north')],
    )
    if with_outline:
        found['outline'] = await _outline(source, row['id'], row['points'], row['extent'])
    return found


async def _outline(source, region_id: int, points: int, extent: float) -> Dict[str, Any]:
    """The region simplified until it has at most OUTLINE_VERTICES vertices: islands smaller than the tolerance go
    (the largest polygon always stays), and each ring loses the detail the tolerance allows."""
    tolerance = 0.0 if points <= OUTLINE_VERTICES else extent / 2000
    while True:
        row = await source.fetchrow(
            """WITH parts AS (
                   SELECT d.geom, ST_Area(d.geom) AS area, row_number() OVER (ORDER BY ST_Area(d.geom) DESC) AS rank
                   FROM regions r, ST_Dump(r.geom) d WHERE r.id = $1
               ), kept AS (
                   SELECT ST_SimplifyPreserveTopology(geom, $2) AS geom FROM parts
                   WHERE rank = 1 OR area >= 4 * $2 * $2
               )
               SELECT ST_NPoints(outline) AS points, ST_AsGeoJSON(outline, 5) AS geojson
               FROM (SELECT ST_Multi(ST_Collect(geom)) AS outline FROM kept) simplified""",
            region_id,
            tolerance,
        )
        if row['points'] <= OUTLINE_VERTICES or tolerance > extent:
            return json.loads(row['geojson'])
        tolerance *= 1.5


async def match_names(source, texts: Sequence[str]) -> Dict[str, RegionMatch]:
    """What each text written after `location within` names (filter_query.region_names): the region with that key
    in any case, else the regions with that name in any case, else nothing, with close keys."""
    if not texts:
        return {}
    rows = await source.fetch(
        """SELECT t.text, r.key, r.kind, r.country_name, r.key = t.canonical AS by_key
           FROM unnest($1::text[], $2::text[]) AS t(text, canonical)
           JOIN regions r ON r.key = t.canonical OR lower(r.name) = lower(t.text)
           ORDER BY t.text, array_position(ARRAY['country', 'state', 'group'], r.kind), r.name, r.key""",
        list(texts),
        [canonical_region_key(text) for text in texts],
    )
    found: Dict[str, RegionMatch] = {}
    for text in texts:
        named = [row for row in rows if row['text'] == text]
        by_key = [row['key'] for row in named if row['by_key']]
        if by_key or len(named) == 1:
            found[text] = RegionMatch(key=(by_key or [named[0]['key']])[0])
        elif named:
            found[text] = RegionMatch(candidates=[(row['key'], row['kind'], row['country_name']) for row in named])
        else:
            found[text] = RegionMatch(
                suggestions=await source.fetchval('SELECT region_suggestions($1)', text),
                loaded=await source.fetchval('SELECT EXISTS (SELECT 1 FROM regions)'),
            )
    return found


async def sets(source) -> List[Dict[str, Any]]:
    """The boundary sets this server can load and, for each loaded one, what was loaded, when and by whom; for each,
    the region load queued or running for it (`load_job`), else null."""
    jobs = {
        row['set']: {'job_id': str(row['id']), 'state': row['state']}
        for row in await source.fetch(
            "SELECT id, state, args->>'set' AS set FROM jobs WHERE kind = $1 AND state IN ('queued', 'running')",
            KIND,
        )
    }
    loaded = {
        row['name']: row
        for row in await source.fetch(
            """SELECT s.name, s.version, s.sha256, s.verified, s.loaded_from, s.loaded_at, s.loaded_by,
                      u.username AS loaded_by_name, s.report->'removed_keys' AS removed_keys,
                      (SELECT jsonb_object_agg(kind, n) FROM (SELECT kind, count(*) AS n FROM regions r
                                                              WHERE r.set_name = s.name GROUP BY kind) k) AS counts
               FROM region_sets s LEFT JOIN users u ON u.id = s.loaded_by""",
        )
    }
    answer = []
    for name in sorted(set(SETS) | set(loaded)):
        known, row = SETS.get(name), loaded.get(name)
        entry: Dict[str, Any] = {'name': name}
        if known is not None:
            entry.update(
                title=known.title,
                version=known.version,
                url=known.url,
                sha256=known.sha256,
                licence=known.licence,
            )
        entry['loaded'] = (
            None
            if row is None
            else {
                'version': row['version'],
                'sha256': row['sha256'],
                'verified': row['verified'],
                'loaded_from': row['loaded_from'],
                'loaded_at': row['loaded_at'],
                'loaded_by': None
                if row['loaded_by'] is None
                else {'id': row['loaded_by'], 'username': row['loaded_by_name']},
                'counts': {kind: _json(row['counts']).get(kind, 0) for kind in KINDS},
                'removed_keys': _json(row['removed_keys']) or [],
            }
        )
        entry['load_job'] = jobs.get(name)
        answer.append(entry)
    return answer


def _json(value: Any) -> Any:
    """A JSONB value from a connection with or without the app's JSON codec."""
    return json.loads(value) if isinstance(value, str) else (value or {})


# --- The file that comes with the install --------------------------------------------------------------------------


def boundaries_dir() -> Path:
    """The one directory a server looks in for boundary sets' files: FRIDGE_REGIONS_DIR when it is set, else
    backend/boundaries/ beside this code (which git ignores). Read at each call."""
    return Path(os.environ.get('FRIDGE_REGIONS_DIR') or Path(__file__).resolve().parent / 'boundaries')


def boundary_file(boundary_set: BoundarySet) -> Path:
    """Where the set's file is looked for: in boundaries_dir(), named as in the set's URL."""
    return boundaries_dir() / boundary_set.url.rsplit('/', 1)[-1]


def _not_pinned(data: bytes, boundary_set: BoundarySet) -> Optional[str]:
    """Why `data` isn't the set's pinned file, or None when it is."""
    if len(data) > boundary_set.max_bytes:
        return f'it is over the limit of {boundary_set.max_bytes:,} bytes'
    digest = hashlib.sha256(data).hexdigest()
    if digest != boundary_set.sha256:
        return f'its SHA-256 is {digest}, not {boundary_set.sha256}'
    return None


def _read_boundary_file(boundary_set: BoundarySet) -> Tuple[Optional[bytes], Optional[str]]:
    """(the set's pinned file from boundaries_dir(), None) when it is there; (None, why) for a file there that is
    anything else; (None, None) when there is none."""
    try:
        with open(boundary_file(boundary_set), 'rb') as file:
            data = file.read(boundary_set.max_bytes + 1)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, f'it could not be read ({error.strerror or error})'
    why = _not_pinned(data, boundary_set)
    return (None, why) if why else (data, None)


# The files the log has named as not the pinned one, each with why: each is named once.
_reported: Set[Tuple[str, str]] = set()


def pinned_file(boundary_set: BoundarySet) -> Optional[bytes]:
    """The set's pinned file from boundaries_dir(), or None when it isn't there. A file there that is anything else
    counts as none, and the log says so once, naming it: nothing unverified is loaded from this directory."""
    data, why = _read_boundary_file(boundary_set)
    if why is not None:
        path = str(boundary_file(boundary_set))
        if (path, why) not in _reported:
            _reported.add((path, why))
            logger.warning(
                '%s is not the pinned file of %s %s: %s. It is ignored, as if there were no file '
                '(python -m backend.regions fetch replaces it)',
                path,
                boundary_set.title,
                boundary_set.version,
                why,
            )
    return data


def _write_file(path: Path, data: bytes) -> None:
    """`data` at `path`, whole or not at all: written beside it, then renamed over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.part')
    try:
        with os.fdopen(handle, 'wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o644)  # readable by a job runner running as another user
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


# --- Loading as a job ----------------------------------------------------------------------------------------------


async def _queue_load(
    pool,
    set_name: str,
    user_id: Optional[int],
    *,
    automatic: bool,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """(the region load queued for the set, True), or (the region load already queued or running, False): one at a
    time, for the button and the job runner alike. `automatic`: the job runner's own, which reads the file that came
    with the install and never downloads, and is queued only for a set this database has never loaded ((None, False)
    otherwise)."""
    async with job_queue._transaction(pool) as connection:
        await connection.execute(f"SELECT pg_advisory_xact_lock(hashtextextended('schedule:{KIND}', 0))")
        active = await connection.fetchrow(
            f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
                WHERE kind = $1 AND state IN ('queued', 'running') ORDER BY created_at LIMIT 1""",
            KIND,
        )
        if active is not None:
            return dict(active), False
        if automatic and await connection.fetchval(
            'SELECT EXISTS (SELECT 1 FROM region_sets WHERE name = $1)',
            set_name,
        ):
            return None, False
        job = await job_queue.enqueue_job(
            connection,
            KIND,
            lane='maintenance',
            args={'set': set_name, 'automatic': True} if automatic else {'set': set_name},
            created_by=user_id,
        )
        return job, True


async def start_load(pool, set_name: str, user_id: Optional[int]) -> Tuple[Dict[str, Any], bool]:
    """Queue the load of a set for an administrator (from the file that came with the install when it is there,
    else downloaded), or return the region load already queued or running: one at a time."""
    job, started = await _queue_load(pool, set_name, user_id, automatic=False)
    assert job is not None  # only an automatic load is ever not queued
    return job, started


async def queue_bundled_loads(pool) -> List[Dict[str, Any]]:
    """When the job runner starts: queue the region load of each set whose pinned file is in boundaries_dir() and
    that this database has never loaded (no region_sets row), unless a region load is queued or running. The load
    is the ordinary job; it reads that file, never downloads, and records loaded_from 'bundled' and no user. A set
    loaded once, from whatever, is never loaded again by itself. Returns the jobs it queued; safe to call again, and
    from two runners at once."""
    queued = []
    for boundary_set in list(SETS.values()):
        if not boundary_file(boundary_set).exists():
            continue  # nothing to decide, and no database to ask
        if await pool.fetchval('SELECT EXISTS (SELECT 1 FROM region_sets WHERE name = $1)', boundary_set.name):
            continue
        if await asyncio.to_thread(pinned_file, boundary_set) is None:
            continue
        loop = asyncio.get_running_loop()
        give_up = loop.time() + CONTENDED_SECONDS
        while True:
            try:
                job, started = await _queue_load(pool, boundary_set.name, None, automatic=True)
                break
            except job_queue.QueueContended:
                # Another runner holds the lock for a moment. Each try waits up to the queue's lock timeout
                # (100 ms) on its own; past the limit the error goes up, and the job runner tries again later.
                if loop.time() + 0.2 > give_up:
                    raise
                await asyncio.sleep(0.1)
        if started and job is not None:
            logger.info(
                'Queued the load of %s %s from %s (job %s)',
                boundary_set.title,
                boundary_set.version,
                boundary_file(boundary_set),
                job['id'],
            )
            queued.append(job)
    return queued


async def download(boundary_set: BoundarySet) -> bytes:
    """The set's file from its pinned URL, refused once it passes the size limit. The only place this module reaches
    the network: for an administrator's load and for `fetch`, never for a load the server starts by itself."""
    import httpx

    chunks, size = [], 0
    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
            async with client.stream('GET', boundary_set.url) as response:
                if response.status_code != 200:
                    raise RegionLoadError(f'{boundary_set.url} answered {response.status_code}')
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > boundary_set.max_bytes:
                        raise RegionLoadError(
                            f'{boundary_set.url} is over the limit of {boundary_set.max_bytes:,} bytes',
                        )
                    chunks.append(chunk)
    except httpx.HTTPError as error:
        raise RegionLoadError(
            f'Could not download {boundary_set.url}: {error or type(error).__name__}. On a server with no internet, '
            f'load the file with python -m backend.regions load <file> --set {boundary_set.name}',
        ) from None
    return b''.join(chunks)


def _counted(count: int, one: str, many: str) -> str:
    return f'1 {one}' if count == 1 else f'{count:,} {many}'


def summary(report: Dict[str, Any]) -> str:
    counts, removed = report['counts'], len(report['removed_keys'])
    gone = f"; {_counted(removed, 'key', 'keys')} of the earlier load {'is' if removed == 1 else 'are'} gone"
    return (
        f"{_counted(counts['country'], 'country', 'countries')}, "
        f"{_counted(counts['state'], 'state or province', 'states or provinces')} and "
        f"{_counted(counts['group'], 'group', 'groups')} loaded ({report['version']})" + (gone if removed else '')
    )


def handler(pool):
    async def measure(job):
        args = job['args'] or {}
        name = args.get('set')
        boundary_set = SETS.get(name) if isinstance(name, str) else None
        if boundary_set is None:
            raise JobInputError(f'Unknown boundary set {name!r}')
        automatic = args.get('automatic') is True
        # Free space first, as for every large write: refused, nothing is read, downloaded or written, and the job
        # fails with the gate's own words.
        async with pool.acquire() as connection:
            try:
                await diskspace.require_free_space(connection, LOAD_FREE_BYTES, 'loading region boundaries')
            except HTTPException as error:
                raise JobInsufficientStorage(str(error.detail)) from None
        data = await asyncio.to_thread(pinned_file, boundary_set)
        if data is not None:
            loaded_from, digest = 'bundled', boundary_set.sha256
        elif automatic:
            # The file was there when the job runner queued this; a load it starts by itself never downloads.
            raise RegionLoadError(
                f'{boundary_file(boundary_set)} is gone, or is no longer the pinned file of {boundary_set.title} '
                f'{boundary_set.version}, and a load the server starts by itself never downloads. Load the set from '
                f'Settings → System status → Region boundaries',
            )
        else:
            data = await download(boundary_set)
            loaded_from, digest = 'download', check_file(data, boundary_set)
        try:
            async with pool.acquire() as connection, connection.transaction():
                if automatic:
                    # Loaded meanwhile (from the command line): a loaded set is never loaded again by itself.
                    await connection.execute(SET_LOCK_SQL, boundary_set.name)
                    if await connection.fetchval(
                        'SELECT EXISTS (SELECT 1 FROM region_sets WHERE name = $1)',
                        boundary_set.name,
                    ):
                        return StepResult(
                            finished=True,
                            result={'already_loaded': True, 'set': boundary_set.name},
                            message=f'{boundary_set.title} was loaded meanwhile; it is left as it is',
                        )
                report = await load(
                    connection,
                    boundary_set,
                    data,
                    version=boundary_set.version,
                    sha256=digest,
                    verified=True,
                    loaded_from=loaded_from,
                    user_id=job['created_by'],
                )
        except asyncpg.PostgresError as error:
            if job_queue.is_transient(error):
                raise  # retried
            raise RegionLoadError(f'The database refused {boundary_set.name}: {error}') from None
        return StepResult(finished=True, result=report, message=summary(report))

    return measure


# --- From a file, on a server with no internet ---------------------------------------------------------------------


async def _load_file(path: str, boundary_set: BoundarySet, verify: bool, version: Optional[str]) -> Dict[str, Any]:
    from .database import DATABASE_URL, PLANNING

    with open(path, 'rb') as file:
        data = file.read(boundary_set.max_bytes + 1)
    digest = check_file(data, boundary_set, verify=verify)
    pinned = digest == boundary_set.sha256
    if not pinned and not version:
        raise RegionLoadError(
            f'This file is not {boundary_set.title} {boundary_set.version} (SHA-256 {digest}): say which version it '
            f'is with --set-version',
        )
    connection = await asyncpg.connect(DATABASE_URL, server_settings=dict(PLANNING))
    try:
        return await load(
            connection,
            boundary_set,
            data,
            version=boundary_set.version if pinned else str(version),
            sha256=digest,
            verified=pinned,
            loaded_from='file',
            user_id=None,
        )
    finally:
        await connection.close()


# --- Fetching the file, in a source checkout's install steps -------------------------------------------------------


async def _fetch(boundary_sets: Sequence[BoundarySet]) -> int:
    """`fetch`: each set's pinned file into boundaries_dir(), for the job runner to load. No database."""
    code = 0
    for boundary_set in boundary_sets:
        path = boundary_file(boundary_set)
        data, why = await asyncio.to_thread(_read_boundary_file, boundary_set)
        if data is not None:
            print(f'{boundary_set.name} {boundary_set.version}: {path} is already the pinned file; nothing downloaded')
            continue
        if why is not None:
            print(f'{boundary_set.name}: {path} is not the pinned file ({why}); downloading the pinned one')
        try:
            data = await download(boundary_set)
            why = _not_pinned(data, boundary_set)
            if why is not None:
                raise RegionLoadError(
                    f'{boundary_set.url} gave a file that is not the pinned one of {boundary_set.title} '
                    f'{boundary_set.version}: {why}',
                )
            # Renamed over the old file only once verified: until then the directory is as it was.
            await asyncio.to_thread(_write_file, path, data)
        except (OSError, RegionLoadError) as error:
            print(f'error: {error}', file=sys.stderr)
            print(
                'The region boundaries can be loaded later, from Settings → System status → Region boundaries.',
                file=sys.stderr,
            )
            code = 1
            continue
        print(f'{boundary_set.name} {boundary_set.version}: downloaded to {path} (SHA-256 checked)')
    return code


# --- The command line ----------------------------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m backend.regions',
        description=(
            "Fetch boundary sets' pinned files for the job runner to load, or load a boundary set from a file "
            '(docs/REGIONS.md).'
        ),
    )
    commands = parser.add_subparsers(dest='command', required=True)
    fetch_command = commands.add_parser(
        'fetch',
        help=f"download each boundary set's pinned file into {boundaries_dir()}, for the job runner to load",
    )
    fetch_command.add_argument('--set', choices=sorted(SETS), help='fetch only this set')
    load_command = commands.add_parser('load', help='load a GeoJSON file as a boundary set, replacing its regions')
    load_command.add_argument('file', help="the set's GeoJSON file, as its source publishes it")
    load_command.add_argument('--set', required=True, choices=sorted(SETS), help='the boundary set it is')
    load_command.add_argument(
        '--no-verify',
        action='store_true',
        help="load it even when it is not the set's pinned file (its SHA-256 differs): a file you vouch for",
    )
    load_command.add_argument(
        '--set-version',
        help='with --no-verify, the version of a file that is not the pinned one',
    )
    args = parser.parse_args(argv)
    if args.command == 'fetch':
        return asyncio.run(_fetch([SETS[args.set]] if args.set else list(SETS.values())))
    boundary_set = SETS[args.set]
    try:
        report = asyncio.run(_load_file(args.file, boundary_set, not args.no_verify, args.set_version))
    except (OSError, RegionLoadError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    except asyncpg.PostgresError as error:  # a geometry PostGIS can't read, in a file loaded with --no-verify
        print(f'error: the database refused {args.file}: {error}', file=sys.stderr)
        return 1
    print(summary(report))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
