"""One rule for a missing value, everywhere (contract g7, one rule everywhere), tested where a user or a script meets
it: the HTTP API signed in with real API tokens (the region tests' world: a database of its own per test), the SDK
driven as a script drives it (its requests served by the app in-process) and the assistant's tools. Written from the
contract, not from the code.

The rule: a row with no value for a field never matches a comparison of that field, and never matches the NOT of one;
only `is null` and `not exists` find it. A row has a value when the value is there and isn't a JSON null. Tags (and
`@local.tags`), the lists every sample has, have one when they hold at least one tag, and so has a list field kept
in a column of its own, which is read as tags are (rule 1.5). A list in the metadata (such as `detections`) has one
when it is there, empty or not: an empty list says the list was looked at and holds nothing (Words, changed
2026-11-26, the owner's decision and his last word on tags). A list of labels (`detections.label`) has one when the
list is there and is empty or has a label; a list whose elements have no label at all has none (rule 1.4). A
concept's score is a row's value for the concept, its location its value for a place, its point on an embeddings map
its value for a lasso drawn on that map.

The fixture (section 7.1). PLAIN is a dataset without objects of ten rows, `r0` to `r9`, each with a `plot` and a
`rank`, and for each field a value, another value, a JSON null and no key, spread so that no two fields have their gaps
on the same rows: a text field `colour`, a number field `n`, a list of labels `detections`, tags (four rows have
none), the concept `scene`'s score (four rows unscored), a location (three rows have none; four inside the square
XA-NM, which the region tests' boundary file loads as [0, 5] x [0, 5]) and a point on the `clip` map (four rows have
none); for rule 1.7, a number field `size` whose one row holds the text `many`, and a datetime field `seen_at` holding
the text `unknown` and the number 12345 on two rows. OBJECTS (section 7.2) is a dataset with objects for the places and
the tags through the page's wrapping.

Every expected set on PLAIN is computed by `Reading`, a plain function over the planted rows written from the
contract's words; `test_the_reference_reads_the_rules_words_as_the_hand_does` checks it against sets written out by
hand first, so a wrong expectation shows up as the test's own mistake. `Reading(today=True)` is what main answers (a
negation the complement of its show), for the pins of section 7.4.

Where the contract leaves a point open, these tests read it this way, and stay away from what it leaves open:
- A place test, `geo_polygon`, `geo_region` or `geo_near`, compares the row's location: a NOT around one needs a
  location (rule 1.6). A lasso compares the row's point on its map: a NOT around one is the excluded lasso, and
  through the wrapping it is read as a place is (rule 1.6, made exact 2026-11-25). On a dataset with objects the
  images have points on the `clip` map and the objects on `clip_obj`, so each lasso is met through the partner on one
  grid; a lasso is never asked beside another test in one card (a lasso of the other kind read with the same object
  is not in this piece).
- `contains` on tags is a membership test, as `in` with one name.
- A value of another kind than the one compared (rule 1.7): the text `many` in the number field `size`, and the text
  `unknown` and the number 12345 in the datetime field `seen_at`. It is a value: `!=` shows the row, as main does,
  and a range never matches it (so a NOT around a range shows it).
- "Every reader" (2.1) is samples/query and its count, an export's pages, aggregate, the histogram, field-stats and a
  field's autocomplete (the sidebar), map_points and map_extent (the map), the embeddings map's points, a selection
  token and in_selection (Select All), /api/search, a version's samples/query and aggregate, a saved view read back and
  opened by its stage's filter, the SDK (a script's condition, and load_view), the assistant (make_view and
  get_samples, open_saved_view, concept_preview's counts against the views its keep and remove make) and a bulk tag
  (its count and the rows it tagged), each where main reads the filter at all. Follower search is not asked: it relays
  a patch search to a mirror, which reads the filter with the same compiler, and needs a second instance.
- The old meaning of an excluded place written out (5.3, 7.4) is pinned as a filter node: main's text refuses
  `location is null` on a dataset with no field named location. The text now takes `location is null`, `is not
  null`, `exists` and `not exists`, read as their filter nodes are read (rule 1.6, made exact 2026-11-25): a test of
  its own, beside a pin of the dataset that has a metadata field named `location`, where main's text already takes
  them.
- A list of labels (rule 1.6, made exact 2026-11-25, and changed by the next contract, g9 rule 1.2: `is null` means
  no value): `detections.label is null` shows the rows with no list (no key, a JSON null, a value that is no list) and
  the rows whose list has elements none of which has a label, not a row whose list is empty; `is not null` shows the
  rows whose list is empty or has a label. A dataset of its own, LABELLED, holds each case. `exists` and `not exists`
  of the list of labels' path show the same rows as `is not null` and `is null` (g9 rule 1.7, made exact
  2026-11-26); of the field, `detections exists` asks for the key, as today. `detections is null`, asked of the field,
  does not show an empty list, as today. The empty list's own test, `is empty`, is
  backend/tests/test_is_empty_spec.py's.
- The empty list in a field (7.7, added 2026-11-26): LABELLED and the file of cases hold no key, a JSON null, `[]`, a
  list with the value, a list without it and a list whose elements have no label. A list field kept in a column of
  its own (rule 1.5's last paragraph, the owner's `inferred_tags`) is made as the app makes one, through the API, on
  a dataset of its own, ARRAY_FIELD, and read as tags are: a sample added without it and one given `[]` have no
  value, nor has a column written NULL.
- Each test that holds new behaviour asks something main answers first, as a control, so a failure on main is the
  rule's. The pins (sections 7.4 and 7.6, rule 2.3) pass on main and after.
"""

import asyncio
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fridge import Condition, Dataset, F, geo_within, match_elements

from backend.routers import datasets, embeddings
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_assistant_core_spec import open_door
from backend.tests.test_assistant_views_spec import result, view_ids
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_objects_filter_reading_spec import child, image, page_filter
from backend.tests.test_objects_filter_reading_spec import plant as plant_objects
from backend.tests.test_protected_metadata_keys_spec import sdk
from backend.tests.test_regions_spec import ADMIN_USER, Regions, loaded, nodes, post, selected, text_of

database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(
    not (os.getenv('FRIDGE_TEST_POSTGRES_DSN') and os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')),
    reason='Requires disposable PostgreSQL',
)

PLAIN, OBJECTS, LABELLED, LOCATION_FIELD, MATCHED, NOT_IN_ELEMENTS = 70, 71, 72, 73, 74, 75
ARRAY_FIELD = 79
SQUARE = [[0, 0], [5, 0], [5, 5], [0, 5]]  # XA-NM, the region tests' square; no planted location is on its border
REGIONS = {'XA-NM': SQUARE}
NEAR = {'point': [2.5, 2.5], 'max_distance': 300_000}  # metres
LASSO = [[0, 0], [5, 0], [5, 5], [0, 5]]  # on the `clip` map; no planted point is on its border
CUT = 0.5
BOX = [0.1, 0.1, 0.2, 0.2]


def boxes(*labels: str) -> list[dict[str, Any]]:
    """A list of labels as a detection field stores it."""
    return [{'label': label, 'bounding_box': BOX} for label in labels]


# --- The planted rows ---


@dataclass(frozen=True)
class Row:
    values: dict[str, Any]  # metadata as stored: None is a JSON null, a key left out is no key
    tags: tuple[str, ...] = ()
    score: Optional[float] = None  # the concept `scene`
    location: Optional[tuple[float, float]] = None
    point: Optional[tuple[float, float]] = None  # on the `clip` embeddings map


def row(number: int, values: dict[str, Any], **more: Any) -> Row:
    return Row({'plot': f'p{number}', 'rank': number, **values}, **more)


ROWS: dict[str, Row] = {
    'r0': row(
        0,
        {'colour': 'red', 'n': 1, 'size': 10, 'detections': boxes('car'), 'seen_at': '2026-01-01T08:00:00Z'},
        tags=('rejected',),
        score=0.75,
        location=(2.5, 2.5),
        point=(1.0, 1.0),
    ),
    'r1': row(
        1,
        {'colour': 'blue', 'n': 2, 'size': 20, 'detections': boxes('truck'), 'seen_at': '2026-01-02T08:00:00Z'},
        tags=('seen',),
        score=0.25,
        location=(32.0, 2.5),
        point=(8.0, 8.0),
    ),
    'r2': row(
        2,
        {'colour': None, 'n': None, 'size': None, 'detections': None, 'seen_at': None},
        location=(1.0, 1.0),
    ),
    'r3': row(3, {}),
    'r4': row(
        4,
        {'colour': 'red', 'n': 2, 'size': 20, 'detections': boxes('car', 'truck'), 'seen_at': 'unknown'},
        tags=('seen', 'rejected'),
        score=0.5,
        location=(7.5, 2.5),
        point=(2.0, 3.0),
    ),
    'r5': row(
        5,
        {'colour': 'blue', 'detections': [], 'seen_at': '2026-01-01T20:00:00Z'},
        tags=('seen',),
        score=0.875,
        point=(9.0, 1.0),
    ),
    'r6': row(6, {'n': 1, 'detections': boxes('truck')}, score=0.125, location=(3.0, 4.0)),
    'r7': row(
        7,
        {'colour': None, 'n': 4, 'size': 'many', 'seen_at': '2026-01-02T08:00:00Z'},
        tags=('other',),
        location=(100.0, 50.0),
        point=(3.0, 3.0),
    ),
    'r8': row(8, {'colour': 'green', 'n': None, 'detections': boxes('car'), 'seen_at': None}, score=0.375),
    'r9': row(
        9,
        {'colour': 'red', 'n': 3, 'size': 30, 'detections': None, 'seen_at': 12345},
        tags=('seen',),
        location=(4.0, 1.5),
        point=(7.0, 7.0),
    ),
}
EVERY = set(ROWS)
LOCATED = {name for name, item in ROWS.items() if item.location is not None}
ON_THE_MAP = {name for name, item in ROWS.items() if item.point is not None}


@dataclass
class Planted:
    dataset_id: int
    ids: dict[str, int]
    concept: int

    def names(self, ids: Any) -> set[str]:
        by_id = {sample_id: name for name, sample_id in self.ids.items()}
        return {by_id[sample_id] for sample_id in ids}

    def of(self, names: set[str]) -> set[int]:
        return {self.ids[name] for name in names}


async def plant(r: Regions, dataset_id: int = PLAIN) -> Planted:
    """ROWS as a dataset without objects, with a vector each, their points on the `clip` map, the concept `scene` and
    the region set loaded."""
    await r.dataset(dataset_id)
    samples = []
    for name, item in ROWS.items():
        body: dict[str, Any] = {'filepath': f'{name}.jpg', 'metadata': dict(item.values), 'tags': list(item.tags)}
        if item.location is not None:
            body['location'] = {'type': 'Point', 'coordinates': list(item.location)}
        samples.append(body)
    await datasets.add_samples(dataset_id, {'samples': samples}, user=ADMIN_USER)
    stored = await r.database.fetch(
        'SELECT id, filepath, metadata, tags, lon, lat FROM samples WHERE dataset_id = $1',
        dataset_id,
    )
    ids = {item['filepath'][: -len('.jpg')]: item['id'] for item in stored}
    assert set(ids) == EVERY, sorted(ids)
    for item in stored:
        name = item['filepath'][: -len('.jpg')]
        metadata = item['metadata'] if isinstance(item['metadata'], dict) else json.loads(item['metadata'])
        planted = ROWS[name]
        for key, value in planted.values.items():  # a JSON null is kept as one, an empty list as one
            assert key in metadata and metadata[key] == value, (name, key, metadata)
        assert set(metadata) == set(planted.values), (name, metadata)
        assert tuple(item['tags'] or ()) == planted.tags, (name, item['tags'])
        assert (None if item['lon'] is None else (item['lon'], item['lat'])) == planted.location, name
    vectors = [
        {'sample_id': ids[name], 'vector': [1.0, number / 100, 0.0, 0.0], 'field_name': 'clip'}
        for number, name in enumerate(ROWS)
    ]
    assert (await post(r, f'/api/datasets/{dataset_id}/embeddings', vectors)).is_success
    r.monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    points = [
        {'sample_id': ids[name], 'x': item.point[0], 'y': item.point[1]}
        for name, item in ROWS.items()
        if item.point is not None
    ]
    response = await r.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/embeddings/coordinates',
        params={'field_name': 'clip'},
        json=points,
    )
    assert response.status_code == 200, response.text
    on_map = await r.database.fetch(
        "SELECT sample_id FROM embedding_coordinates WHERE field_name = 'clip' AND sample_id = ANY($1::int[])",
        list(ids.values()),
    )
    assert {item['sample_id'] for item in on_map} == {ids[name] for name in ON_THE_MAP}
    concept = await r.database.fetchval(
        'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
        "VALUES ($1, 'scene', 'clip', 'seed', '[1,0,0,0]', $2) RETURNING id",
        dataset_id,
        CUT,
    )
    for name, item in ROWS.items():
        if item.score is not None:
            await r.database.execute(
                'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, $3)',
                concept,
                ids[name],
                item.score,
            )
    typed = await r.call(
        ADMIN,
        'PATCH',
        f'/api/datasets/{dataset_id}/fields/seen_at/type',
        json={'field_type': 'datetime'},
    )
    assert typed.status_code == 200, typed.text
    await loaded(r)
    return Planted(dataset_id, ids, concept)


# --- The reference: the contract's reading on one row, from its words ---

COMPARISONS = {'=', '!=', 'in', 'not in', '<', '<=', '>', '>=', 'between', 'contains'}
PLACE_TESTS = {'geo_polygon', 'geo_region', 'geo_near'}
LASSOS = {'embedding_polygon'}
CONCEPTS = {'concept_select'}
MATCHES = {'match_elements', 'elem_match'}  # an element match compares the field's list (Words, added 2026-11-25)
# Each hiding test and the show it is the NOT of (rules 1.1, 1.3, 1.6; and `is not empty`, contract g9 rule 1.4).
SHOW_OF = {
    '!=': '=',
    'not in': 'in',
    'concept_exclude': 'concept_select',
    'geo_polygon_exclude': 'geo_polygon',
    'geo_region_exclude': 'geo_region',
    'embedding_polygon_exclude': 'embedding_polygon',
    'is_not_empty': 'is_empty',
}
EMPTINESS = {'is_empty'}  # a list that holds nothing (contract g9, rule 1.4): it needs a value, as a comparison does
ABSENCE = {'is_null', 'is_not_null', 'exists', 'not_exists'}


def labels(stored: Any) -> Optional[list[str]]:
    """A detection field's list of labels: the empty list for an empty list (looked at, nothing found), None when the
    field holds no list or a list whose elements have no label at all (something found, what it is not known)."""
    if not isinstance(stored, list):
        return None
    found = [item['label'] for item in stored if isinstance(item, dict) and item.get('label') is not None]
    return found if found or not stored else None


def inside(point: tuple[float, float], polygon: Sequence[Sequence[float]]) -> bool:
    """Inside an axis-aligned rectangle given as a polygon (no planted point lies on a border)."""
    lons, lats = [corner[0] for corner in polygon], [corner[1] for corner in polygon]
    return min(lons) < point[0] < max(lons) and min(lats) < point[1] < max(lats)


def metres(a: tuple[float, float], b: list[float]) -> float:
    """The great-circle distance, on a sphere of the earth's mean radius."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(h))


def number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare(value: Any, operator: str, wanted: Any) -> bool:
    """A comparison of a value that is there. A value of another kind than the one compared never meets it; text is
    compared with text (a range by its order), and `contains` reads a number written as text, as main does."""
    if operator == 'contains':
        written = value if isinstance(value, str) else json.dumps(value) if number(value) else None
        return written is not None and str(wanted).lower() in written.lower()
    if operator in ('<', '<=', '>', '>=') and isinstance(value, str) and isinstance(wanted, str):
        return bool({'<': value < wanted, '<=': value <= wanted, '>': value > wanted, '>=': value >= wanted}[operator])
    if operator == 'in':
        return any(compare(value, '=', each) for each in wanted)
    if operator == '=':
        if number(value) and number(wanted):
            return float(value) == float(wanted)
        if number(value) and isinstance(wanted, str):  # text is compared with the value written as text, as today
            return json.dumps(value) == wanted
        return type(value) is type(wanted) and value == wanted
    if not (number(value) and number(wanted if operator != 'between' else wanted[0])):
        return False
    if operator == 'between':
        return bool(wanted[0] <= value <= wanted[1])
    return bool({'<': value < wanted, '<=': value <= wanted, '>': value > wanted, '>=': value >= wanted}[operator])


def element_meets(element: Any, node: dict[str, Any]) -> bool:
    """An element match's conditions on one element (a detection), each on the element's own value for its key."""
    if 'filters' in node or 'logic' in node:
        found = [element_meets(element, part) for part in node.get('filters') or []]
        return all(found) if str(node.get('logic', 'AND')).upper() == 'AND' else any(found)
    held = element.get(node['field']) if isinstance(element, dict) else None
    if held is None:
        return False
    if node['operator'] in ('not in', 'not_in', 'nin'):
        return not any(compare(held, '=', each) for each in node['value'])
    return compare(held, node['operator'], node['value'])


class Reading:
    """What a filter shows on PLAIN, read on each row with its own values (contract section 1).

    Rule 1.1: `f != v` and `f not in (...)` show a row that has a value for f which isn't v. Rule 1.2: `NOT (X)` shows
    a row that has a value for every field compared inside X (an absence test adds none) and that X doesn't show.
    Rules 1.3, 1.4, 1.5 and 1.6: a concept's exclude, a list's negation and an excluded place or lasso are the NOT of
    their show, with the score, the list, the location or the point as the value. With `today`, main's reading: a
    negation is the complement of its show."""

    def __init__(self, today: bool = False):
        self.today = today

    @staticmethod
    def value(item: Row, leaf: dict[str, Any]) -> Any:
        operator, name = leaf['operator'], leaf.get('field')
        if operator in CONCEPTS:
            return item.score
        if operator in PLACE_TESTS or name == 'location':
            return item.location
        if operator in LASSOS:
            return item.point
        if operator in MATCHES:
            held = item.values.get(str(name))
            return held if isinstance(held, list) else None  # a value that is no list is no value for it
        if name == 'tags':
            return list(item.tags)
        if isinstance(name, str) and name.endswith('.label'):
            return labels(item.values.get(name[: -len('.label')]))
        return item.values.get(str(name))

    def has(self, item: Row, leaf: dict[str, Any]) -> bool:
        """A value for the test. Tags have one when they hold a tag, as before (rule 1.5); a list in the metadata when
        it is there, empty or not (Words, changed 2026-11-26)."""
        found = self.value(item, leaf)
        if leaf.get('field') == 'tags':
            return bool(found)
        return found is not None

    def filled(self, item: Row, leaf: dict[str, Any]) -> bool:
        """What `exists` asks of tags and of a location, as today: whether the list holds anything, the place is
        there."""
        found = self.value(item, leaf)
        return found is not None and found != []

    def passes(self, item: Row, leaf: dict[str, Any]) -> bool:
        """A showing test, or an absence test, on the row's own value. `is null` and `is not null` ask for a value
        (contract g9, rule 1.1): a list of labels has one when it is empty or holds a label (g9 1.2), tags when they
        hold a tag (g9 1.6). `is empty` asks for a list that holds nothing; of tags, which every sample holds, it asks
        what `is null` asks (g9 1.6)."""
        operator = leaf['operator']
        name = leaf.get('field')
        if operator in ('exists', 'not_exists'):
            # Of a path through a list of labels, a value (g9 rule 1.7, made exact 2026-11-26); of a field, its key.
            if isinstance(name, str) and name.endswith('.label'):
                there = self.has(item, leaf)
            else:
                there = self.filled(item, leaf) if name in ('tags', 'location') else name in item.values
            return there if operator == 'exists' else not there
        if operator in ('is_null', 'is_not_null'):
            there = self.has(item, leaf)
            return there if operator == 'is_not_null' else not there
        if operator in EMPTINESS:
            found = self.value(item, leaf)
            return not found if name == 'tags' else found == []
        if not self.has(item, leaf):
            return False
        found = self.value(item, leaf)
        if operator == 'geo_polygon' or operator in LASSOS:
            return inside(found, leaf['value'])
        if operator == 'geo_region':
            return inside(found, REGIONS[leaf['value']])
        if operator == 'geo_near':
            return metres(found, leaf['value']['point']) <= leaf['value']['max_distance']
        if operator in CONCEPTS:
            return compare(found, leaf['value']['op'], float(leaf['value']['threshold']))
        if operator in MATCHES:
            return any(element_meets(element, leaf['value']) for element in found)
        assert operator in COMPARISONS - set(SHOW_OF), leaf
        if isinstance(found, list):  # tags, a list of labels: any element
            wanted = leaf['value'] if operator == 'in' else [leaf['value']]
            return any(each in found for each in wanted)
        return compare(found, operator, leaf['value'])

    def compared(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        """The tests inside a node that need a value: comparisons, concepts, places, lassos and `is empty` (never an
        absence)."""
        if 'filters' in node or 'logic' in node:
            return [leaf for part in node.get('filters') or [] for leaf in self.compared(part)]
        operator = node['operator']
        if operator in SHOW_OF:
            return [{**node, 'operator': SHOW_OF[operator]}]
        return [node] if operator in COMPARISONS | PLACE_TESTS | LASSOS | CONCEPTS | MATCHES | EMPTINESS else []

    def negation(self, item: Row, inside_not: dict[str, Any]) -> bool:
        if self.today:
            return not self.shows(item, inside_not)
        return all(self.has(item, leaf) for leaf in self.compared(inside_not)) and not self.shows(item, inside_not)

    def shows(self, item: Row, node: dict[str, Any]) -> bool:
        if 'filters' in node or 'logic' in node:
            logic = str(node.get('logic', 'AND')).upper()
            parts = node.get('filters') or []
            if logic == 'NOT':
                return self.negation(item, {'logic': 'AND', 'filters': parts})
            found = [self.shows(item, part) for part in parts]
            return all(found) if logic == 'AND' else any(found)
        operator = node['operator']
        if operator in SHOW_OF:
            return self.negation(item, {**node, 'operator': SHOW_OF[operator]})
        return self.passes(item, node)

    def rows(self, node: dict[str, Any], rows: Optional[dict[str, Row]] = None) -> set[str]:
        return {name for name, item in (ROWS if rows is None else rows).items() if self.shows(item, node)}


RULE, TODAY = Reading(), Reading(today=True)


# --- Filters, as JSON and as text ---


def leaf(field: str, operator: str, value: Any = None) -> dict[str, Any]:
    return {'field': field, 'operator': operator, 'value': value}


def negated(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'NOT', 'filters': list(parts)}


def both(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'AND', 'filters': list(parts)}


def either(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'OR', 'filters': list(parts)}


def concept(p: Planted, operator: str, op: str = '>', threshold: float = CUT) -> dict[str, Any]:
    return {
        'field': '_concept',
        'operator': operator,
        'value': {'concept_id': p.concept, 'op': op, 'threshold': threshold},
    }


async def parsed(r: Regions, text: str, dataset_id: int = PLAIN) -> dict[str, Any]:
    """The filter the parse route makes of the text: the AND of its parts."""
    return both(*await nodes(r, text, dataset_id))


async def shown(r: Regions, p: Planted, form: Any) -> set[str]:
    """The rows samples/query lists for a filter (text, read by the parse route, or a node), its count agreeing."""
    node = await parsed(r, form, p.dataset_id) if isinstance(form, str) else form
    return p.names(await selected(r, node, p.dataset_id))


async def assert_shows(r: Regions, p: Planted, cases: list[tuple[Any, set[str]]]) -> None:
    """Each form shows the rows the contract's reading shows, which the test also writes out by hand."""
    for form, by_hand in cases:
        node = await parsed(r, form, p.dataset_id) if isinstance(form, str) else form
        assert RULE.rows(node) == by_hand, ("the test's own expectation", form, sorted(RULE.rows(node)))
        found = await shown(r, p, node)
        assert found == by_hand, (form, 'shown', sorted(found), 'expected', sorted(by_hand))


# --- The reference, checked first ---

RED, BLUE = {'r0', 'r4', 'r9'}, {'r1', 'r5'}
INSIDE = {'r0', 'r2', 'r6', 'r9'}  # located inside XA-NM
ON_LASSO = {'r0', 'r4', 'r7'}  # a point inside LASSO
UNTAGGED = {'r2', 'r3', 'r6', 'r8'}
UNSCORED = {'r2', 'r3', 'r7', 'r9'}

# (the filter, what the rule shows, what main shows), written out by hand from the planted rows.
BY_HAND: list[tuple[dict[str, Any], set[str], set[str]]] = [
    (leaf('colour', '!=', 'red'), {'r1', 'r5', 'r8'}, EVERY - RED),
    (leaf('colour', 'not in', ['red', 'green']), BLUE, EVERY - RED - {'r8'}),
    (leaf('n', '!=', 2), {'r0', 'r6', 'r7', 'r9'}, EVERY - {'r1', 'r4'}),
    (leaf('n', 'not in', [1, 2]), {'r7', 'r9'}, EVERY - {'r0', 'r1', 'r4', 'r6'}),
    (negated(both(leaf('colour', '=', 'red'), leaf('n', '=', 1))), {'r1', 'r4', 'r9'}, EVERY - {'r0'}),
    (negated(either(leaf('colour', '=', 'red'), leaf('n', '=', 1))), {'r1'}, EVERY - RED - {'r6'}),
    (negated(leaf('n', '>', 1)), {'r0', 'r6'}, EVERY - {'r1', 'r4', 'r7', 'r9'}),
    (negated(both(leaf('colour', '=', 'blue'), leaf('n', 'is_null'))), {'r0', 'r1', 'r4', 'r8', 'r9'}, EVERY - {'r5'}),
    (leaf('detections.label', '!=', 'car'), {'r1', 'r5', 'r6'}, EVERY - {'r0', 'r4', 'r8'}),
    (leaf('detections', 'is_null'), {'r2', 'r3', 'r7', 'r9'}, {'r2', 'r3', 'r7', 'r9'}),
    (
        negated(leaf('detections', 'match_elements', leaf('label', '=', 'car'))),
        {'r1', 'r5', 'r6'},
        EVERY - {'r0', 'r4', 'r8'},
    ),
    (leaf('tags', 'not in', ['rejected']), {'r1', 'r5', 'r7', 'r9'}, EVERY - {'r0', 'r4'}),
    (leaf('tags', 'is_null'), UNTAGGED, UNTAGGED),
    (leaf('location', 'geo_polygon_exclude', SQUARE), {'r1', 'r4', 'r7'}, EVERY - INSIDE),
    (negated(leaf('location', 'geo_region', 'XA-NM')), {'r1', 'r4', 'r7'}, EVERY - INSIDE),
    (leaf('clip', 'embedding_polygon_exclude', LASSO), {'r1', 'r5', 'r9'}, EVERY - ON_LASSO),
    (leaf('size', '!=', 20), {'r0', 'r7', 'r9'}, EVERY - {'r1', 'r4'}),
    (either(leaf('colour', '!=', 'red'), leaf('colour', 'is_null')), EVERY - RED, EVERY - RED),
]


def test_the_reference_reads_the_rules_words_as_the_hand_does():
    for node, rule, today in BY_HAND:
        assert RULE.rows(node) == rule, (node, sorted(RULE.rows(node)))
        assert TODAY.rows(node) == today, (node, sorted(TODAY.rows(node)))
    # A concept's exclude: scored, and not above the cut.
    exclude = {
        'field': '_concept',
        'operator': 'concept_exclude',
        'value': {'concept_id': 1, 'op': '>', 'threshold': CUT},
    }
    assert RULE.rows(exclude) == {'r1', 'r4', 'r6', 'r8'} and TODAY.rows(exclude) == {'r1', 'r4', 'r6', 'r8'} | UNSCORED


# --- Section 1, rule by rule, on a dataset without objects (7.1) ---


def test_rule_1_1_not_equal_and_not_in_show_only_the_rows_with_another_value(regions_world):
    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers them: the shows.
        await assert_shows(r, p, [('colour = "red"', RED), ('n in (1, 2)', {'r0', 'r1', 'r4', 'r6'})])
        await assert_shows(
            r,
            p,
            [
                ('colour != "red"', {'r1', 'r5', 'r8'}),
                ('colour not in ("red", "green")', BLUE),
                ('n != 2', {'r0', 'r6', 'r7', 'r9'}),
                ('n not in (1, 2)', {'r7', 'r9'}),
                ('colour != "red" OR n != 2', {'r0', 'r1', 'r5', 'r6', 'r7', 'r8', 'r9'}),
                (leaf('colour', '!=', 'red'), {'r1', 'r5', 'r8'}),
                (leaf('colour', 'not in', ['red']), {'r1', 'r5', 'r8'}),
                (leaf('n', '!=', 2), {'r0', 'r6', 'r7', 'r9'}),
                (leaf('n', 'not in', [2, 3]), {'r0', 'r6', 'r7'}),
            ],
        )

    regions_world(scenario)


def test_rule_1_2_a_not_shows_a_row_that_has_every_compared_field_and_that_the_inside_does_not_show(regions_world):
    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers them: the insides alone, and a NOT of a NOT.
        await assert_shows(
            r,
            p,
            [
                ('colour = "red" AND n = 1', {'r0'}),
                ('colour = "red" OR n = 1', RED | {'r6'}),
                ('NOT (NOT (colour = "red"))', RED),
            ],
        )
        await assert_shows(
            r,
            p,
            [
                ('NOT (colour = "red")', {'r1', 'r5', 'r8'}),
                # Two fields, both needed: r5 has no n, r8 a null one, r6 no colour, r7 a null one.
                ('NOT (colour = "red" AND n = 1)', {'r1', 'r4', 'r9'}),
                ('NOT (colour = "red" OR n = 1)', {'r1'}),
                ('NOT (n > 1)', {'r0', 'r6'}),
                ('NOT (n between 1 and 2)', {'r7', 'r9'}),
                ('NOT (colour contains "bl")', {'r0', 'r4', 'r8', 'r9'}),
                ('NOT (colour in ("red", "blue"))', {'r8'}),
                ('colour = "blue" OR NOT (n = 2)', {'r0', 'r1', 'r5', 'r6', 'r7', 'r9'}),
                # An absence test inside adds no field (rule 1.7): n is not needed, colour is.
                ('NOT (colour = "blue" AND n is null)', {'r0', 'r1', 'r4', 'r8', 'r9'}),
                (negated(leaf('colour', '=', 'red')), {'r1', 'r5', 'r8'}),
            ],
        )

    regions_world(scenario)


def test_rule_1_3_a_concepts_exclude_shows_only_the_rows_that_have_a_score(regions_world):
    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers it: the show.
        await assert_shows(
            r,
            p,
            [(f'concept "scene" > {CUT}', {'r0', 'r5'}), (concept(p, 'concept_select'), {'r0', 'r5'})],
        )
        await assert_shows(
            r,
            p,
            [
                (f'not concept "scene" > {CUT}', {'r1', 'r4', 'r6', 'r8'}),
                (f'NOT (concept "scene" > {CUT})', {'r1', 'r4', 'r6', 'r8'}),
                (concept(p, 'concept_exclude'), {'r1', 'r4', 'r6', 'r8'}),  # the Concepts panel's exclude
                ('not concept "scene" <= 0.25', {'r0', 'r4', 'r5', 'r8'}),
                (f'NOT (concept "scene" > {CUT} AND colour = "red")', {'r1', 'r4', 'r5', 'r8'}),
                (f'colour = "red" AND not concept "scene" > {CUT}', {'r4'}),
            ],
        )

    regions_world(scenario)


def test_rule_1_4_a_negation_of_a_list_of_labels_shows_the_rows_whose_list_is_there_and_holds_none_of_them(
    regions_world,
):
    """Changed 2026-11-26: the empty list (r5's) is shown, nothing was found, so no car. A JSON null (r2, r9) and no
    list (r3, r7) are not."""

    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers it: the show.
        await assert_shows(r, p, [('detections.label = "car"', {'r0', 'r4', 'r8'})])
        await assert_shows(
            r,
            p,
            [
                ('detections.label != "car"', {'r1', 'r5', 'r6'}),
                ('detections.label not in ("car", "bus")', {'r1', 'r5', 'r6'}),
                ('detections.label not in ("truck")', {'r0', 'r5', 'r8'}),
                ('NOT (detections.label = "car")', {'r1', 'r5', 'r6'}),
                (leaf('detections.label', '!=', 'car'), {'r1', 'r5', 'r6'}),
                (leaf('detections.label', 'not in', ['car']), {'r1', 'r5', 'r6'}),
            ],
        )

    regions_world(scenario)


def test_rule_1_5_a_negation_of_tags_shows_only_rows_with_a_tag_none_of_them_named(regions_world):
    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers it: the show.
        await assert_shows(r, p, [('tags in ("rejected")', {'r0', 'r4'})])
        kept = {'r1', 'r5', 'r7', 'r9'}
        await assert_shows(
            r,
            p,
            [
                ('tags not in ("rejected")', kept),
                ('tags != "rejected"', kept),
                ('NOT (tags in ("rejected"))', kept),
                ('NOT (tags contains "rejected")', kept),
                ('tags not in ("rejected", "seen")', {'r7'}),
                (leaf('tags', '!=', 'rejected'), kept),
                (leaf('tags', 'not in', ['rejected']), kept),
            ],
        )

    regions_world(scenario)


def test_rule_1_5_pin_tags_is_null_shows_the_rows_with_no_tags_and_is_not_null_the_rows_with_one(regions_world):
    """What main answers, and the rule keeps (1.5, 1.7)."""

    async def scenario(r):
        p = await plant(r)
        await assert_shows(
            r,
            p,
            [
                ('tags is null', UNTAGGED),
                ('tags is not null', EVERY - UNTAGGED),
                ('tags not exists', UNTAGGED),
                ('tags exists', EVERY - UNTAGGED),
            ],
        )

    regions_world(scenario)


def test_rule_1_6_an_excluded_place_shows_only_the_rows_that_have_a_location(regions_world):
    async def scenario(r):
        p = await plant(r)
        place = leaf('location', 'geo_polygon', SQUARE)
        near = leaf('location', 'geo_near', NEAR)
        # Asked first, as main answers them: the places.
        await assert_shows(r, p, [('location within "XA-NM"', INSIDE), (place, INSIDE), (near, INSIDE)])
        outside = {'r1', 'r4', 'r7'}  # r3, r5 and r8 have no location
        await assert_shows(
            r,
            p,
            [
                ('location not within "XA-NM"', outside),
                (leaf('location', 'geo_region_exclude', 'XA-NM'), outside),
                (leaf('location', 'geo_polygon_exclude', SQUARE), outside),
                ('NOT (location within "XA-NM")', outside),
                (negated(place), outside),
                (negated(near), outside),
                ('location not within "XA-NM" AND tags in ("seen")', {'r1', 'r4'}),
                (either(leaf('location', 'geo_polygon_exclude', SQUARE), leaf('plot', '=', 'nowhere')), outside),
            ],
        )

    regions_world(scenario)


def test_rule_1_6_an_excluded_lasso_shows_only_the_rows_that_have_a_point_on_that_map(regions_world):
    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers it: the lasso. r2, r3, r6 and r8 have no point on the map.
        await assert_shows(r, p, [(leaf('clip', 'embedding_polygon', LASSO), ON_LASSO)])
        excluded = leaf('clip', 'embedding_polygon_exclude', LASSO)
        await assert_shows(
            r,
            p,
            [(excluded, {'r1', 'r5', 'r9'}), (both(excluded, leaf('colour', 'exists')), {'r1', 'r5', 'r9'})],
        )

    regions_world(scenario)


def test_rule_1_6_pin_a_polygon_of_fewer_than_three_points_is_still_no_filter(regions_world):
    async def scenario(r):
        p = await plant(r)
        for node in (
            leaf('location', 'geo_polygon_exclude', SQUARE[:2]),
            leaf('location', 'geo_polygon', SQUARE[:2]),
            leaf('location', 'geo_polygon_exclude', []),
            leaf('clip', 'embedding_polygon_exclude', LASSO[:2]),
        ):
            assert await shown(r, p, node) == EVERY, node

    regions_world(scenario)


def test_rule_1_7_pin_absence_tests_and_showing_tests_answer_as_today(regions_world):
    async def scenario(r):
        p = await plant(r)
        cases: list[tuple[Any, set[str]]] = [
            ('colour is null', {'r2', 'r3', 'r6', 'r7'}),
            ('colour is not null', {'r0', 'r1', 'r4', 'r5', 'r8', 'r9'}),
            ('colour exists', EVERY - {'r3', 'r6'}),  # a JSON null under the key counts as there
            ('colour not exists', {'r3', 'r6'}),
            ('n is null', {'r2', 'r3', 'r5', 'r8'}),
            ('colour = "red"', RED),
            ('colour in ("blue", "green")', BLUE | {'r8'}),
            ('colour contains "e"', EVERY - {'r2', 'r3', 'r6', 'r7'}),
            ('n > 1', {'r1', 'r4', 'r7', 'r9'}),
            ('n between 2 and 3', {'r1', 'r4', 'r9'}),
            ('detections.label in ("truck")', {'r1', 'r4', 'r6'}),
            ('tags contains "seen"', {'r1', 'r4', 'r5', 'r9'}),
            (f'concept "scene" <= {CUT}', {'r1', 'r4', 'r6', 'r8'}),
            ('location within "XA-NM"', INSIDE),
            (leaf('location', 'is_null'), EVERY - LOCATED),
            (leaf('location', 'is_not_null'), LOCATED),
            (leaf('clip', 'embedding_polygon', LASSO), ON_LASSO),
            ('colour = "red" OR tags in ("other")', RED | {'r7'}),
        ]
        for form, by_hand in cases:
            node = await parsed(r, form) if isinstance(form, str) else form
            assert RULE.rows(node) == TODAY.rows(node) == by_hand, ("the test's own expectation", form)
        await assert_shows(r, p, cases)

    regions_world(scenario)


def test_rule_1_7_pin_the_built_in_fields_every_row_has_answer_as_today(regions_world):
    async def scenario(r):
        p = await plant(r)
        ids = p.ids
        for node, kept in (
            (leaf('id', '!=', ids['r3']), EVERY - {'r3'}),
            (leaf('id', 'not in', [ids['r2'], ids['r3']]), EVERY - {'r2', 'r3'}),
            (negated(leaf('id', 'in', [ids['r2']])), EVERY - {'r2'}),
            (leaf('filepath', '!=', 'r6.jpg'), EVERY - {'r6'}),
            (negated(leaf('filepath', 'contains', 'r8')), EVERY - {'r8'}),
            (leaf('media_type', '!=', 'object'), EVERY),
            (leaf('media_type', 'not in', ['video']), EVERY),
            (leaf('created_at', '!=', '1999-01-01'), EVERY),
            (leaf('last_modified_at', 'not in', ['1999-01-01']), EVERY),
        ):
            assert await shown(r, p, node) == kept, node

    regions_world(scenario)


def test_rule_1_7_pin_a_value_of_another_kind_is_a_value_and_not_equal_shows_it(regions_world):
    """`size` holds the text `many` on r7, `seen_at` (a datetime field) the text `unknown` on r4 and the number 12345 on
    r9. Each is a value: `!=` and `not in` show its row, a range never matches it, and a NOT around a range shows it, as
    main does. Only the rows that have a value are asked here (the rows with none are rule 1.1's)."""

    async def scenario(r):
        p = await plant(r)
        sized = {name for name, item in ROWS.items() if item.values.get('size') is not None}
        seen = {name for name, item in ROWS.items() if item.values.get('seen_at') is not None}
        for node, having, kept in (
            (leaf('size', '!=', 20), sized, {'r0', 'r7', 'r9'}),
            (leaf('size', 'not in', [10, 20]), sized, {'r7', 'r9'}),
            (leaf('size', '>', 15), sized, {'r1', 'r4', 'r9'}),
            (negated(leaf('size', '>', 15)), sized, {'r0', 'r7'}),
            (leaf('seen_at', '!=', '2026-01-01'), seen, {'r1', 'r4', 'r7', 'r9'}),
            (leaf('seen_at', 'not in', ['2026-01-01']), seen, {'r1', 'r4', 'r7', 'r9'}),
            (leaf('seen_at', '>=', '2026-01-02T00:00:00Z'), seen, {'r1', 'r7'}),
            (negated(leaf('seen_at', '>=', '2026-01-02T00:00:00Z')), seen, {'r0', 'r4', 'r5', 'r9'}),
        ):
            assert await shown(r, p, node) & having == kept, node
        for text, having, kept in (
            ('seen_at != 2026-01-01', seen, {'r1', 'r4', 'r7', 'r9'}),
            ('seen_at not in (2026-01-01)', seen, {'r1', 'r4', 'r7', 'r9'}),
        ):
            assert await shown(r, p, text) & having == kept, text
        # An absence test inside a NOT adds no field: NOT around absence tests alone is their opposite.
        await assert_shows(
            r,
            p,
            [
                ('NOT (colour is null)', {'r0', 'r1', 'r4', 'r5', 'r8', 'r9'}),
                ('NOT (colour is null OR n is null)', {'r0', 'r1', 'r4', 'r9'}),
            ],
        )

    regions_world(scenario)


# --- 2.1: every reader of a filter ---


async def ok(response: Any, where: Any = None) -> Any:
    assert response.status_code == 200, (where, response.status_code, response.text)
    return response.json()


async def version_of(r: Regions, p: Planted) -> int:
    await ok(await post(r, f'/api/datasets/{p.dataset_id}/versions', {'name': 'as planted'}))
    return await r.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', p.dataset_id)


def stage_of(node: dict[str, Any]) -> dict[str, Any]:
    """The stage the app saves for a filter: a map region as a drawn polygon's chip, anything else as a filter card."""
    if node.get('operator') in ('geo_polygon', 'geo_polygon_exclude'):
        excluding = node['operator'] == 'geo_polygon_exclude'
        return {
            'type': 'map_region',
            'description': 'Exclude Map Region' if excluding else 'Map Region',
            'filter': node,
        }
    return {'type': 'filter', 'description': 'a filter', 'filter': node}


async def saved_view(r: Regions, p: Planted, node: dict[str, Any], name: str, public: bool = False) -> dict[str, Any]:
    content = {'viewStages': [stage_of(node)]}
    body = {'name': name, 'content': content, 'is_public': public}
    return await ok(await post(r, f'/api/datasets/{p.dataset_id}/views', body), name)


async def every_reader(
    r: Regions,
    p: Planted,
    form: Any,
    names: set[str],
    monkeypatch: Any,
    *,
    version: Optional[int],
) -> None:
    """Every reader of filters keeps exactly the rows `names` for the filter (a text, or a node with no text form)."""
    text = form if isinstance(form, str) else None
    node = await parsed(r, form) if text else form
    where = text or node
    assert RULE.rows(node) == names, ("the test's own expectation", where, sorted(RULE.rows(node)))
    base = f'/api/datasets/{p.dataset_id}'
    ids = p.of(names)
    # The grid's list and its count.
    assert await selected(r, node, p.dataset_id) == ids, ('samples/query', where)
    # An export's pages, as the export dialog asks them.
    exported: list[int] = []
    after: Optional[int] = None
    while True:
        body: dict[str, Any] = {'limit': 2, 'filters': node, 'fields': 'filepath', 'slim': True, 'skip_count': True}
        if after is not None:
            body['after_id'] = after
        items = (await ok(await post(r, f'{base}/samples/query', body), where))['items']
        exported += [item['id'] for item in items]
        if len(items) < 2:
            break
        after = items[-1]['id']
    assert sorted(exported) == sorted(ids), ('the export', where)
    plots = sorted(ROWS[name].values['plot'] for name in names)
    ranks = [ROWS[name].values['rank'] for name in names]
    aggregations = [{'field': 'plot', 'op': 'count'}, {'field': 'plot', 'op': 'values'}]
    count, values = (await ok(await post(r, f'{base}/aggregate', {'filters': node, 'aggregations': aggregations})))[
        'results'
    ]
    assert (count['value'], sorted(values['value'])) == (len(names), plots), ('aggregate', where)
    histogram = await ok(await post(r, f'{base}/histogram', {'field': 'rank', 'filters': node, 'bins': 10}))
    assert histogram['total'] == len(names), ('histogram', where, histogram)
    [facet] = await ok(await post(r, f'{base}/field-stats', {'filters': node, 'fields': ['rank']}))
    assert (facet['min'], facet['max']) == (min(ranks), max(ranks)), ('field-stats', where, facet)
    listed = await ok(await post(r, f'{base}/fields/plot/autocomplete', {'filters': node, 'limit': 100}))
    assert sorted(item['value'] for item in listed['values']) == plots, ('autocomplete', where)
    # The map, and the embeddings map.
    points = await ok(await post(r, f'{base}/map_points', {'filters': node, 'points': True}))
    assert {item['id'] for item in points['items']} == p.of(names & LOCATED), ('map_points', where)
    box = await ok(await post(r, f'{base}/map_extent', {'filters': node}))
    assert box['count'] == len(names & LOCATED), ('map_extent', where, box)
    scatter = await ok(await post(r, f'{base}/embeddings/query', {'field': 'clip', 'filters': node, 'sample_pct': 100}))
    assert {item['sample_id'] for item in scatter['points']} == p.of(names & ON_THE_MAP), ('embeddings map', where)
    # Select All and its token.
    token = await ok(await post(r, f'{base}/selection_token', {'filters': node}))
    assert token['count'] == len(names), ('selection_token', where)
    in_token = {'field': 'id', 'operator': 'in_selection', 'value': token['token']}
    assert await selected(r, in_token, p.dataset_id) == ids, ('in_selection', where)
    search = {'dataset_id': p.dataset_id, 'vector': [1.0, 0.0, 0.0, 0.0], 'field_name': 'clip', 'limit': 1000}
    found = await ok(await post(r, '/api/search', {**search, 'filters': node}))
    assert {item['id'] for item in found} == ids, ('/api/search', where)
    # A version's query (a filter read on one row).
    if version is not None:
        rows = await ok(await post(r, f'{base}/versions/{version}/samples/query', {'filters': node, 'limit': 1000}))
        assert {item['id'] for item in rows['items']} == ids, ('a version: samples/query', where)
        kept = await ok(
            await post(
                r,
                f'{base}/versions/{version}/aggregate',
                {'filters': node, 'aggregations': [{'field': 'plot', 'op': 'values'}]},
            ),
        )
        assert sorted(kept['results'][0]['value']) == plots, ('a version: aggregate', where)
    # A saved view, read back as saved and opened by its stage's filter.
    name = f'view-{uuid4().hex[:8]}'
    made = await saved_view(r, p, node, name)
    read = await ok(await r.call(ADMIN, 'GET', f"/api/views/{made['slug']}"))
    assert read['content'] == {'viewStages': [stage_of(node)]}, ('the saved view, read back', where)
    assert await selected(r, read['content']['viewStages'][0]['filter'], p.dataset_id) == ids, ('its stage', where)
    # The SDK, as a script writes the filter, and the saved view loaded.
    sdk(r, asyncio.get_running_loop(), monkeypatch)

    def through_the_sdk() -> tuple[set[int], int, set[int]]:
        dataset = Dataset(f'dataset-{p.dataset_id}')
        if text is not None:
            view = dataset.query(text)
        else:
            view = dataset.samples[Condition(node['field'], node['operator'], node['value'])]
        return set(view.ids()), view.count(), set(dataset.load_view(name).ids())

    assert await asyncio.to_thread(through_the_sdk) == (ids, len(names), ids), ('the SDK', where)
    # The assistant: its view of the text, and the saved view opened.
    await open_door(r)
    if text is not None:
        view = await result(ADMIN, 'make_view', {'dataset': p.dataset_id, 'filter': text})
        assert view['count'] == len(names), ('the assistant: make_view', where, view['count'])
        assert await view_ids(ADMIN, view['view']) == ids, ('the assistant: get_samples', where)
    opened = await result(ADMIN, 'open_saved_view', {'slug': made['slug']})
    assert opened['count'] == len(names), ('the assistant: open_saved_view', where, opened['count'])
    assert await view_ids(ADMIN, opened['view']) == ids, ('the assistant: its samples', where)
    # A bulk tag by the filter, run as the job runner would, last (it changes the tags): its count, and its rows.
    tag = f'bulk-{uuid4().hex[:12]}'
    tagged = await post(r, f'{base}/samples/bulk-tag', {'filters': node, 'add_tags': [tag]})
    assert tagged.status_code in (200, 202), tagged.text
    if tagged.status_code == 202:
        updated = (await finish(r.database))['result']['updated_count']
    else:
        updated = tagged.json()['updated_count']
    rows_tagged = await r.database.fetch(
        'SELECT id FROM samples WHERE dataset_id = $1 AND $2 = ANY(tags)',
        p.dataset_id,
        tag,
    )
    assert (updated, {item['id'] for item in rows_tagged}) == (len(names), ids), ('the bulk tag', where)


def test_rule_2_1_pin_every_reader_shows_the_rows_of_a_showing_filter(regions_world, monkeypatch):
    """The readers asked below, with filters main and the rule read alike: a pin, and the control for the next test."""

    async def scenario(r):
        p = await plant(r)
        version = await version_of(r, p)
        await every_reader(r, p, 'colour = "red"', RED, monkeypatch, version=version)
        await every_reader(r, p, leaf('location', 'geo_polygon', SQUARE), INSIDE, monkeypatch, version=version)

    regions_world(scenario)


# (what is asked, the filter (a text, or a node with no text form), the rows the rule shows, whether a version reads it)
READERS: list[tuple[str, Any, set[str], bool]] = [
    ('not-equal', 'colour != "red"', {'r1', 'r5', 'r8'}, True),
    ('not-over-two-fields', 'NOT (colour = "red" AND n = 1)', {'r1', 'r4', 'r9'}, True),
    ('list-of-labels', 'detections.label not in ("car")', {'r1', 'r5', 'r6'}, True),
    ('tags-not-in', 'tags not in ("rejected")', {'r1', 'r5', 'r7', 'r9'}, True),
    ('excluded-region', 'location not within "XA-NM"', {'r1', 'r4', 'r7'}, True),
    ('excluded-polygon', leaf('location', 'geo_polygon_exclude', SQUARE), {'r1', 'r4', 'r7'}, True),
    ('excluded-lasso', leaf('clip', 'embedding_polygon_exclude', LASSO), {'r1', 'r5', 'r9'}, True),
    # A version keeps no concept scores: main refuses a concept filter there, and so it is not asked of one.
    ('concept-exclude', f'not concept "scene" > {CUT}', {'r1', 'r4', 'r6', 'r8'}, False),
]


@pytest.mark.parametrize(('case', 'form', 'names', 'versioned'), READERS, ids=[case[0] for case in READERS])
def test_rule_2_1_every_reader_follows_the_rule(regions_world, monkeypatch, case, form, names, versioned):
    async def scenario(r):
        p = await plant(r)
        version = await version_of(r, p) if versioned else None
        await every_reader(r, p, form, names, monkeypatch, version=version)

    regions_world(scenario)


def test_rule_2_1_concept_previews_counts_are_the_counts_of_the_views_its_keep_and_remove_make(regions_world):
    async def scenario(r):
        p = await plant(r)
        await open_door(r)
        preview = await result(
            ADMIN,
            'concept_preview',
            {'dataset': p.dataset_id, 'concept': 'scene', 'threshold': CUT},
        )
        above, below = {'r0', 'r5'}, {'r1', 'r4', 'r6', 'r8'}
        counts = (preview['view_count'], preview['above'], preview['below'], preview['unscored'])
        assert counts == (len(EVERY), len(above), len(below), len(UNSCORED)), preview
        assert (preview['keep'], preview['remove']) == (f'concept "scene" > {CUT}', f'not concept "scene" > {CUT}')
        whole = await result(ADMIN, 'make_view', {'dataset': p.dataset_id})
        # Asked first, as main answers it: the keep.
        kept = await result(ADMIN, 'refine_view', {'view': whole['view'], 'add': preview['keep']})
        assert (kept['count'], p.names(await view_ids(ADMIN, kept['view']))) == (preview['above'], above)
        # The remove keeps the images at or below the cut, the count concept_preview gave for them.
        removed = await result(ADMIN, 'refine_view', {'view': whole['view'], 'add': preview['remove']})
        assert (removed['count'], p.names(await view_ids(ADMIN, removed['view']))) == (preview['below'], below)

    regions_world(scenario)


# --- 2.3 and 2.5 ---


def test_rule_2_3_pin_the_text_of_a_filter_does_not_change(regions_world):
    async def scenario(r):
        p = await plant(r)
        for text, node in (
            ('colour != "red"', leaf('colour', '!=', 'red')),
            ('colour not in ("red", "green")', leaf('colour', 'not in', ['red', 'green'])),
            ('n != 2', leaf('n', '!=', 2)),
            ('tags not in ("rejected")', leaf('tags', 'not in', ['rejected'])),
            ('detections.label != "car"', leaf('detections.label', '!=', 'car')),
            ('location not within "XA-NM"', leaf('location', 'geo_region_exclude', 'XA-NM')),
            (f'not concept "scene" > {CUT}', concept(p, 'concept_exclude')),
            ('NOT (colour = "red" AND n = 1)', negated(both(leaf('colour', '=', 'red'), leaf('n', '=', 1)))),
        ):
            assert await nodes(r, text, p.dataset_id) == [node], text
            assert await text_of(r, node, p.dataset_id) == text, text

    regions_world(scenario)


def test_rule_2_5_a_saved_view_and_the_default_view_keep_their_filter_and_are_read_by_the_rule(regions_world):
    async def scenario(r):
        p = await plant(r)
        cases = [
            (leaf('colour', '!=', 'red'), {'r1', 'r5', 'r8'}),
            (leaf('tags', 'not in', ['rejected']), {'r1', 'r5', 'r7', 'r9'}),
            (leaf('location', 'geo_polygon_exclude', SQUARE), {'r1', 'r4', 'r7'}),
        ]
        made = []
        for number, (node, _) in enumerate(cases):
            made.append(await saved_view(r, p, node, f'saved-{number}', public=True))
        default = made[0]
        await ok(await r.call(ADMIN, 'PATCH', f'/api/datasets/{p.dataset_id}', json={'default_view_id': default['id']}))
        # Each read back exactly as it was saved, the default view too: nothing is rewritten.
        listed = {
            view['slug']: view for view in await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{p.dataset_id}/views'))
        }
        for view, (node, _) in zip(made, cases):
            read = await ok(await r.call(ADMIN, 'GET', f"/api/views/{view['slug']}"))
            assert read['content'] == listed[view['slug']]['content'] == {'viewStages': [stage_of(node)]}, node
        dataset = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{p.dataset_id}'))
        assert dataset['default_view']['content'] == {'viewStages': [stage_of(cases[0][0])]}, dataset['default_view']
        # Opened, each shows the rows the rule shows.
        for view, (node, names) in zip(made, cases):
            read = await ok(await r.call(ADMIN, 'GET', f"/api/views/{view['slug']}"))
            stage_filter = read['content']['viewStages'][0]['filter']
            assert await shown(r, p, stage_filter) == names, node

    regions_world(scenario)


# --- 7.2: on a dataset with objects, through the page's wrapping (rules 1.5 and 1.6) ---

# Images and their objects (objects carry a `label`), with locations and tags spread so that the row as a whole and
# the grid's own row tell the readings apart. Around XA-NM: (2.5, 2.5) and (1.5, 1.5) are inside, the others outside.
OBJECT_ROWS = {
    'im-in': image({'plot': 'a'}, location=(2.5, 2.5), tags=('seen',)),
    'ob-in': child('im-in', {'label': 'crate'}, tags=('seen',)),
    'im-out': image({'plot': 'b'}, location=(32.0, 2.5), tags=('rejected',)),
    'ob-out-1': child('im-out', {'label': 'crate'}),
    'ob-out-2': child('im-out', {'label': 'crate'}, location=(33.0, 3.0), tags=('seen',)),
    'im-none': image({'plot': 'c'}),
    'ob-none': child('im-none', {'label': 'crate'}, tags=('rejected',)),
    'im-objout': image({'plot': 'd'}, tags=('seen', 'other')),
    'ob-x': child('im-objout', {'label': 'crate'}, location=(100.0, 50.0), tags=('other',)),
    'im-objin': image({'plot': 'e'}, tags=('other',)),
    'ob-y': child('im-objin', {'label': 'crate'}, location=(1.5, 1.5)),
    'im-alone': image({'plot': 'f'}, tags=('seen',)),
    'im-alone-out': image({'plot': 'g'}, location=(7.5, 2.5)),
    'loose': child(None, {'label': 'crate'}, tags=('seen',)),
    'loose-out': child(None, {'label': 'crate'}, location=(45.0, 7.0)),
}


def wrapped(grain: str, test: str) -> set[str]:
    """What a grid shows through the wrapping, by the contract's words. `place`: an excluded place, shown when the row
    as a whole (an image with its objects, an object with its image) has a location and none of them is inside.
    `tags`: `tags not in ("rejected")`, answered by the grid's own row, which needs a tag and none named rejected."""
    shown_ = set()
    for name, item in OBJECT_ROWS.items():
        if item.is_object != (grain == 'object'):
            continue
        if test == 'tags':
            if item.tags and 'rejected' not in item.tags:
                shown_.add(name)
            continue
        if grain == 'image':
            whole = [item, *(each for each in OBJECT_ROWS.values() if each.image == name)]
        else:
            whole = [item, *([OBJECT_ROWS[item.image]] if item.image else [])]
        located = [each.location for each in whole if each.location is not None]
        if located and not any(inside(point, SQUARE) for point in located):
            shown_.add(name)
    return shown_


def test_7_2_on_a_dataset_with_objects_an_excluded_place_needs_a_location_on_the_row_as_a_whole(regions_world):
    """Rule 1.6 through the wrapping, on both grids: the excluded place (a drawn region as the page lifts it out, and
    the region form), a NOT around the place, and the place excluded inside a card beside another test."""

    async def scenario(r):
        planted = await plant_objects(r, OBJECTS, OBJECT_ROWS)
        await loaded(r)
        images = {'im-out', 'im-objout', 'im-alone-out'}  # im-none and im-alone have no location, nor their objects
        objects = {'ob-out-1', 'ob-out-2', 'ob-x', 'loose-out'}  # ob-none and loose have none, nor their image
        assert (wrapped('image', 'place'), wrapped('object', 'place')) == (images, objects)

        async def grid(grain: str, stage: dict[str, Any]) -> set[str]:
            return planted.names(await selected(r, page_filter([stage], grain), OBJECTS))

        # Asked first, as main answers it: the place.
        assert await grid('image', leaf('location', 'geo_polygon', SQUARE)) == {'im-in', 'im-objin'}
        assert await grid('object', leaf('location', 'geo_polygon', SQUARE)) == {'ob-in', 'ob-y'}
        crate = leaf('label', '=', 'crate')
        for name, stage, (on_images, on_objects) in (
            ('a drawn region excluded', leaf('location', 'geo_polygon_exclude', SQUARE), (images, objects)),
            ('the region form excluded', leaf('location', 'geo_region_exclude', 'XA-NM'), (images, objects)),
            ('a NOT around the region', negated(leaf('location', 'geo_region', 'XA-NM')), (images, objects)),
            ('a NOT around the polygon', negated(leaf('location', 'geo_polygon', SQUARE)), (images, objects)),
            # One card: the exclusion beside a test of the objects (an image with no object has no label).
            (
                'the exclusion sharing a card',
                both(leaf('location', 'geo_polygon_exclude', SQUARE), crate),
                (images - {'im-alone-out'}, objects),
            ),
        ):
            assert await grid('image', stage) == on_images, (name, 'images')
            assert await grid('object', stage) == on_objects, (name, 'objects')

    regions_world(scenario)


def test_7_2_on_a_dataset_with_objects_a_negation_of_tags_needs_a_tag_on_the_grids_own_row(regions_world):
    """Rule 1.5 through the wrapping, on both grids: still answered by the grid's own row (#521), now with the guard."""

    async def scenario(r):
        planted = await plant_objects(r, OBJECTS, OBJECT_ROWS)
        images = {'im-in', 'im-objout', 'im-objin', 'im-alone'}  # im-none and im-alone-out have no tags
        objects = {'ob-in', 'ob-out-2', 'ob-x', 'loose'}  # ob-out-1, ob-y and loose-out have no tags
        assert (wrapped('image', 'tags'), wrapped('object', 'tags')) == (images, objects)

        async def grid(grain: str, stage: dict[str, Any]) -> set[str]:
            return planted.names(await selected(r, page_filter([stage], grain), OBJECTS))

        # Asked first, as main answers it: the show, met by the row or its partner (im-none's object is rejected).
        assert await grid('image', leaf('tags', 'in', ['rejected'])) == {'im-out', 'im-none'}
        assert await grid('object', leaf('tags', 'in', ['rejected'])) == {'ob-none', 'ob-out-1', 'ob-out-2'}
        for stage in (
            leaf('tags', 'not in', ['rejected']),
            leaf('tags', '!=', 'rejected'),
            negated(leaf('tags', 'in', ['rejected'])),
            negated(leaf('tags', 'contains', 'rejected')),
        ):
            assert await grid('image', stage) == images, (stage, 'images')
            assert await grid('object', stage) == objects, (stage, 'objects')

    regions_world(scenario)


# --- 7.3: the SDK as a script uses it ---


def test_7_3_the_sdk_as_a_script_reads_not_equal_an_excluded_place_and_a_tags_exclusion_by_the_rule(
    regions_world,
    monkeypatch,
):
    async def scenario(r):
        p = await plant(r)
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def script() -> dict[str, tuple[set[int], int]]:
            dataset = Dataset(f'dataset-{p.dataset_id}')
            s = dataset.samples
            views = {
                'colour == red': s[s.colour == 'red'],  # asked first, as main answers it
                'geo_within': s[geo_within(SQUARE)],
                'colour != red': s[s.colour != 'red'],
                'n != 2': s[s.n != 2],
                '~geo_within': s[~geo_within(SQUARE)],
                'exclude(geo_within)': dataset.samples.all().exclude(geo_within(SQUARE)),
                'tags not_in': s[s.tags.not_in(['rejected'])],
                'exclude(tags is_in)': dataset.samples.all().exclude(s.tags.is_in(['rejected'])),
            }
            return {name: (set(view.ids()), view.count()) for name, view in views.items()}

        found = await asyncio.to_thread(script)
        for name, names in (
            ('colour == red', RED),
            ('geo_within', INSIDE),
            ('colour != red', {'r1', 'r5', 'r8'}),
            ('n != 2', {'r0', 'r6', 'r7', 'r9'}),
            ('~geo_within', {'r1', 'r4', 'r7'}),
            ('exclude(geo_within)', {'r1', 'r4', 'r7'}),
            ('tags not_in', {'r1', 'r5', 'r7', 'r9'}),
            ('exclude(tags is_in)', {'r1', 'r5', 'r7', 'r9'}),
        ):
            assert found[name] == (p.of(names), len(names)), (name, sorted(p.names(found[name][0])))

    regions_world(scenario)


# --- 7.4: the old meaning written out (pins) ---


def test_7_4_pin_the_old_meaning_written_out_gives_the_rows_main_gives_for_the_bare_form(regions_world):
    """`f != v OR f is null`, `tags not in (...) OR tags is null` and `location not within R OR location is null` (the
    last as a filter node: main's text refuses `location is null`, see the next test) give the rows main gives for the
    bare form, before and after."""

    async def scenario(r):
        p = await plant(r)
        region_out = leaf('location', 'geo_region_exclude', 'XA-NM')
        polygon_out = leaf('location', 'geo_polygon_exclude', SQUARE)
        no_location = leaf('location', 'is_null')
        for bare, written in (
            ('colour != "red"', 'colour != "red" OR colour is null'),
            ('colour not in ("red", "green")', 'colour not in ("red", "green") OR colour is null'),
            ('n != 2', 'n != 2 OR n is null'),
            ('tags not in ("rejected")', 'tags not in ("rejected") OR tags is null'),
            (region_out, either(region_out, no_location)),
            (polygon_out, either(polygon_out, no_location)),
        ):
            bare_node = await parsed(r, bare) if isinstance(bare, str) else bare
            written_node = await parsed(r, written) if isinstance(written, str) else written
            old = TODAY.rows(bare_node)
            assert RULE.rows(written_node) == TODAY.rows(written_node) == old, ("the test's own expectation", written)
            assert await shown(r, p, written_node) == old, written

    regions_world(scenario)


# --- Rule 1.6, made exact 2026-11-25: lassos through the wrapping, `location is null` in text, a list of labels ---

# A dataset with objects whose images have points on the `clip` map and whose objects have points on `clip_obj`.
LASSO_OBJECT_ROWS = {
    'im-a': image({'plot': 'a'}),
    'ob-a': child('im-a', {'label': 'crate'}),
    'im-b': image({'plot': 'b'}),
    'ob-b1': child('im-b', {'label': 'crate'}),
    'ob-b2': child('im-b', {'label': 'crate'}),
    'im-c': image({'plot': 'c'}),
    'ob-c': child('im-c', {'label': 'crate'}),
    'im-d': image({'plot': 'd'}),
    'ob-d': child('im-d', {'label': 'crate'}),
    'im-e': image({'plot': 'e'}),
    'im-f': image({'plot': 'f'}),
    'loose-in': child(None, {'label': 'crate'}),
    'loose-out': child(None, {'label': 'crate'}),
    'loose-none': child(None, {'label': 'crate'}),
}
# Each map's points (a row left out has none on that map); LASSO holds (1, 1), (2, 2) and (3, 3).
LASSO_POINTS: dict[str, dict[str, tuple[float, float]]] = {
    'clip': {'im-a': (1.0, 1.0), 'im-b': (8.0, 8.0), 'im-e': (7.0, 7.0)},
    'clip_obj': {
        'ob-a': (8.0, 8.0),
        'ob-b2': (2.0, 2.0),
        'ob-c': (9.0, 9.0),
        'loose-in': (3.0, 3.0),
        'loose-out': (9.0, 1.0),
    },
}


async def plant_lasso_objects(r: Regions) -> Any:
    """LASSO_OBJECT_ROWS as a dataset with objects, with a vector and a point on each map for the rows that have one."""
    planted = await plant_objects(r, OBJECTS, LASSO_OBJECT_ROWS)
    r.monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    for field_name, points in LASSO_POINTS.items():
        vectors = [
            {'sample_id': planted.ids[name], 'vector': [1.0, 0.0, 0.0, 0.0], 'field_name': field_name}
            for name in points
        ]
        assert (await post(r, f'/api/datasets/{OBJECTS}/embeddings', vectors)).is_success
        uploaded = await r.call(
            ADMIN,
            'POST',
            f'/api/datasets/{OBJECTS}/embeddings/coordinates',
            params={'field_name': field_name},
            json=[{'sample_id': planted.ids[name], 'x': x, 'y': y} for name, (x, y) in points.items()],
        )
        assert uploaded.status_code == 200, uploaded.text
    return planted


def lasso_wrapped(grain: str, field_name: str) -> set[str]:
    """An excluded lasso through the wrapping, by the contract's words: shown when the row as a whole (an image with
    its objects, an object with its image) has a point on that map and none of them is inside."""
    points = LASSO_POINTS[field_name]
    shown_ = set()
    for name, item in LASSO_OBJECT_ROWS.items():
        if item.is_object != (grain == 'object'):
            continue
        if grain == 'image':
            whole = [name, *(other for other, each in LASSO_OBJECT_ROWS.items() if each.image == name)]
        else:
            whole = [name, *([item.image] if item.image else [])]
        on_map = [points[each] for each in whole if each in points]
        if on_map and not any(inside(point, LASSO) for point in on_map):
            shown_.add(name)
    return shown_


# (the map, what the lasso shows on the images' and the objects' grid, what its exclusion shows on each)
LASSO_GRIDS: list[tuple[str, tuple[set[str], set[str]], tuple[set[str], set[str]]]] = [
    ('clip', ({'im-a'}, {'ob-a'}), ({'im-b', 'im-e'}, {'ob-b1', 'ob-b2'})),
    ('clip_obj', ({'im-b'}, {'ob-b2', 'loose-in'}), ({'im-a', 'im-c'}, {'ob-a', 'ob-c', 'loose-out'})),
]


def test_rule_1_6_an_excluded_lasso_through_the_wrapping_needs_a_point_on_the_row_as_a_whole(regions_world):
    """Made exact 2026-11-25: through the wrapping a lasso is read as a place is. The row as a whole has a point on the
    lasso's map (it or a partner) and none of them is inside. On the images' grid im-c has a point only through its
    object (on `clip_obj`); on the objects' grid ob-b1 has one only through its image (on `clip`)."""
    for field_name, _, excluded in LASSO_GRIDS:
        assert (lasso_wrapped('image', field_name), lasso_wrapped('object', field_name)) == excluded, field_name

    async def scenario(r):
        planted = await plant_lasso_objects(r)

        async def grid(grain: str, stage: dict[str, Any]) -> set[str]:
            return planted.names(await selected(r, page_filter([stage], grain), OBJECTS))

        # Asked first, as main answers them: the lassos.
        for field_name, (images, objects), _ in LASSO_GRIDS:
            lasso = leaf(field_name, 'embedding_polygon', LASSO)
            assert (await grid('image', lasso), await grid('object', lasso)) == (images, objects), field_name
        for field_name, _, (images, objects) in LASSO_GRIDS:
            excluded = leaf(field_name, 'embedding_polygon_exclude', LASSO)
            assert await grid('image', excluded) == images, (field_name, 'images')
            assert await grid('object', excluded) == objects, (field_name, 'objects')

    regions_world(scenario)


def test_rule_1_6_a_not_around_a_lasso_is_the_excluded_lasso(regions_world):
    """Made exact 2026-11-25: a NOT around a lasso shows what the excluded lasso shows, on a dataset without objects
    (the rows with a point on the map, none inside) and through the wrapping on a dataset with objects."""

    async def scenario(r):
        p = await plant(r)
        planted = await plant_lasso_objects(r)
        lasso = leaf('clip', 'embedding_polygon', LASSO)
        # Asked first, as main answers it: the lasso on the dataset without objects.
        assert await shown(r, p, lasso) == ON_LASSO
        for form in (negated(lasso), negated(both(lasso))):
            assert RULE.rows(form) == {'r1', 'r5', 'r9'}, ("the test's own expectation", form)
            assert await shown(r, p, form) == {'r1', 'r5', 'r9'}, form

        async def grid(grain: str, stage: dict[str, Any]) -> set[str]:
            return planted.names(await selected(r, page_filter([stage], grain), OBJECTS))

        for field_name, _, (images, objects) in LASSO_GRIDS:
            around = negated(leaf(field_name, 'embedding_polygon', LASSO))
            assert await grid('image', around) == images, (field_name, 'images')
            assert await grid('object', around) == objects, (field_name, 'objects')

    regions_world(scenario)


LOCATION_ABSENCE = [
    ('is_null', 'location is null'),
    ('is_not_null', 'location is not null'),
    ('exists', 'location exists'),
    ('not_exists', 'location not exists'),
]


def test_rule_1_6_location_absence_tests_in_text_read_as_their_filter_nodes_and_the_old_meaning_can_be_typed(
    regions_world,
):
    """Made exact 2026-11-25 (and 5.3, 7.4): on a dataset with no field named `location` the text takes `location is
    null`, `is not null`, `exists` and `not exists`, each the filter node of that operator on `location`, read as main
    reads the node (the sample's own location); so `location not within R OR location is null` can be typed. Main
    refuses the text with a 400 ("'location' takes within or not within and a region"): this fails on main for that
    reason. The nodes, and the text format writes for them, are asked first, as main answers them."""

    async def scenario(r):
        p = await plant(r)
        unlocated = EVERY - LOCATED
        by_hand = {'is_null': unlocated, 'is_not_null': LOCATED, 'exists': LOCATED, 'not_exists': unlocated}
        for operator, text in LOCATION_ABSENCE:
            node = leaf('location', operator)
            assert await shown(r, p, node) == by_hand[operator], operator
            assert await text_of(r, node, p.dataset_id) == text, operator
        for operator, text in LOCATION_ABSENCE:
            assert await nodes(r, text, p.dataset_id) == [leaf('location', operator)], text
            assert await shown(r, p, text) == by_hand[operator], text
        # The old meaning of an excluded place, typed: what main shows for the exclusion.
        assert await shown(r, p, 'location not within "XA-NM" OR location is null') == EVERY - INSIDE

    regions_world(scenario)


# A dataset with a metadata field named `location` (text), beside the samples' own locations: (field, location).
LOCATION_FIELD_ROWS: dict[str, tuple[dict[str, Any], Optional[tuple[float, float]]]] = {
    'f1': ({'location': 'barn'}, (1.0, 1.0)),
    'f2': ({'location': 'field'}, None),
    'f3': ({}, (2.0, 2.0)),
    'f4': ({}, None),
    'f5': ({'location': None}, (3.0, 3.0)),  # the field holds a JSON null; the sample has a location
}


def test_rule_1_6_pin_with_a_field_named_location_its_absence_tests_in_text_read_as_their_filter_nodes(regions_world):
    """A pin (rule 1.7, and the 2026-11-25 addition to 1.6): on a dataset with a metadata field named `location`, main's
    text already takes the four absence tests, as the filter node of each, and main reads the node by the sample's own
    location, never the field (f2 holds the field but has no location; f5's field is a JSON null and it has one). A
    comparison on `location` reads the field, as before."""

    async def scenario(r):
        await r.dataset(LOCATION_FIELD)
        samples = []
        for name, (metadata, point) in LOCATION_FIELD_ROWS.items():
            body: dict[str, Any] = {'filepath': f'{name}.jpg', 'metadata': metadata}
            if point is not None:
                body['location'] = {'type': 'Point', 'coordinates': list(point)}
            samples.append(body)
        await datasets.add_samples(LOCATION_FIELD, {'samples': samples}, user=ADMIN_USER)
        stored = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', LOCATION_FIELD)
        p = Planted(LOCATION_FIELD, {row['filepath'][: -len('.jpg')]: row['id'] for row in stored}, 0)
        located = {name for name, (_, point) in LOCATION_FIELD_ROWS.items() if point is not None}
        unlocated = set(LOCATION_FIELD_ROWS) - located
        by_hand = {'is_null': unlocated, 'is_not_null': located, 'exists': located, 'not_exists': unlocated}
        for operator, text in LOCATION_ABSENCE:
            node = leaf('location', operator)
            assert await shown(r, p, node) == by_hand[operator], operator
            assert await nodes(r, text, LOCATION_FIELD) == [node], text
            assert await shown(r, p, text) == by_hand[operator], text
            assert await text_of(r, node, LOCATION_FIELD) == text, operator
        assert await shown(r, p, 'location = "barn"') == {'f1'}

    regions_world(scenario)


# A list of labels in each state: (the stored detections; a row left out of LABEL_HOLDS has no key).
UNLABELLED = {'bounding_box': BOX}  # a detection with no label
LABEL_HOLDS: dict[str, Any] = {
    'l-car': boxes('car'),
    'l-truck': boxes('truck'),
    'l-both': boxes('car', 'truck'),
    'l-car-unlabelled': [*boxes('car'), UNLABELLED],
    'l-truck-unlabelled': [*boxes('truck'), UNLABELLED],
    'l-unlabelled': [UNLABELLED, UNLABELLED],
    'l-empty': [],
    'l-null': None,
}
LABEL_ROWS: dict[str, Row] = {
    **{name: Row({'plot': name, 'detections': held}) for name, held in LABEL_HOLDS.items()},
    'l-none': Row({'plot': 'l-none'}),
}
# No value for the list of labels (contract g9, rule 1.2): no list, or elements none of which has a label. Not the empty
# list: it is a value (looked at, nothing found).
NO_LABEL = {'l-unlabelled', 'l-null', 'l-none'}
WITH_A_CAR = {'l-car', 'l-both', 'l-car-unlabelled'}
# The list is there and holds no car, and is empty or has a label (rule 1.4, changed 2026-11-26).
NOT_A_CAR = {'l-truck', 'l-truck-unlabelled', 'l-empty'}


async def plant_labelled(r: Regions) -> Planted:
    await r.dataset(LABELLED)
    samples = [{'filepath': f'{name}.jpg', 'metadata': dict(item.values)} for name, item in LABEL_ROWS.items()]
    await datasets.add_samples(LABELLED, {'samples': samples}, user=ADMIN_USER)
    stored = await r.database.fetch('SELECT id, filepath, metadata FROM samples WHERE dataset_id = $1', LABELLED)
    for row_ in stored:
        metadata = row_['metadata'] if isinstance(row_['metadata'], dict) else json.loads(row_['metadata'])
        assert metadata == LABEL_ROWS[row_['filepath'][: -len('.jpg')]].values, row_['filepath']
    return Planted(LABELLED, {row_['filepath'][: -len('.jpg')]: row_['id'] for row_ in stored}, 0)


def test_rule_1_6_a_list_of_labels_is_null_shows_the_rows_with_no_list_or_no_label_not_an_empty_list(regions_world):
    """Rewritten for contract g9 (rule 1.2, `is null` means no value): `detections.label is null` shows the rows with no
    list (no key, a JSON null) and the rows whose detections have no label at all, and not a row whose list is empty:
    an empty list is a value (looked at, nothing found). `is not null` shows the rows whose list is empty or has a
    label. So every row is shown by exactly one of the two, and with rule 1.4 the rows `!=` doesn't show are exactly
    the rows `is null` shows and the rows that have a car (g9 1.3). Main's `is null` (made exact 2026-11-25) also
    showed the empty list, and its `is not null` didn't: this fails on main for that row."""
    is_null, is_not_null = leaf('detections.label', 'is_null'), leaf('detections.label', 'is_not_null')
    valued = set(LABEL_ROWS) - NO_LABEL
    assert 'l-empty' in valued and 'l-empty' in NOT_A_CAR
    assert RULE.rows(is_null, LABEL_ROWS) == NO_LABEL
    assert RULE.rows(is_not_null, LABEL_ROWS) == valued
    assert RULE.rows(leaf('detections.label', '!=', 'car'), LABEL_ROWS) == NOT_A_CAR

    async def scenario(r):
        p = await plant_labelled(r)
        # Asked first, as main answers them: the show, and `is null` asked of the field.
        for text, by_hand in (
            ('detections.label = "car"', WITH_A_CAR),
            ('detections is null', {'l-null', 'l-none'}),
        ):
            assert await shown(r, p, text) == by_hand, text
        for form, by_hand in (
            ('detections.label is null', NO_LABEL),
            (is_null, NO_LABEL),
            ('detections.label is not null', valued),
            (is_not_null, valued),
            ('NOT (detections.label is null)', valued),
            ('detections.label != "car"', NOT_A_CAR),
            ('detections.label not in ("car")', NOT_A_CAR),
        ):
            assert await shown(r, p, form) == by_hand, form
        not_car = await shown(r, p, 'detections.label != "car"')
        no_label, car = await shown(r, p, 'detections.label is null'), await shown(r, p, 'detections.label = "car"')
        assert not not_car & no_label and not no_label & car
        assert set(LABEL_ROWS) - not_car == no_label | car

    regions_world(scenario)


def test_rule_1_7_a_list_of_labels_exists_shows_the_rows_is_not_null_shows_and_not_exists_those_is_null_shows(
    regions_world,
):
    """Rewritten for contract g9 (rule 1.7, made exact 2026-11-26): of the path through a list of labels, `exists`
    shows the rows that have a value for it (an empty list, or a list with a label: the rows of `is not null`) and
    `not exists` the rows that have none (no list, or elements none of which has a label: the rows of `is null`).
    Main's `exists` asked for a detection with a label (not the empty list) and its `not exists` for a detection without
    one (the mixed lists, not the rows with no list): this fails on main for those rows. Of the field itself,
    `detections exists` asks whether the key is there, as main does, asked first."""
    valued = set(LABEL_ROWS) - NO_LABEL
    for operator, names in (('exists', valued), ('not_exists', NO_LABEL)):
        assert RULE.rows(leaf('detections.label', operator), LABEL_ROWS) == names, operator

    async def scenario(r):
        p = await plant_labelled(r)
        assert await shown(r, p, 'detections exists') == set(LABEL_ROWS) - {'l-none'}
        assert await shown(r, p, 'detections not exists') == {'l-none'}
        for form, names in (
            (leaf('detections.label', 'exists'), valued),
            (leaf('detections.label', 'not_exists'), NO_LABEL),
            ('detections.label exists', valued),
            ('detections.label not exists', NO_LABEL),
        ):
            assert await shown(r, p, form) == names, form
        assert await shown(r, p, 'detections.label exists') == await shown(r, p, 'detections.label is not null')
        assert await shown(r, p, 'detections.label not exists') == await shown(r, p, 'detections.label is null')

    regions_world(scenario)


def test_7_7_a_list_of_labels_in_each_state_shows_the_rows_rules_1_4_and_1_6_give(regions_world):
    """Section 7.7 (added 2026-11-26), on LABELLED, through the samples query and its count: no key (l-none), a JSON
    null (l-null), `[]` (l-empty), a list with the value (l-car), a list without it (l-truck) and a list whose elements
    have no label (l-unlabelled), with the mixed lists beside them. The empty list is a value: a negation shows it, and
    (rewritten for contract g9, rule 1.2) `detections.label is null` doesn't; nor does `detections is null`, asked of
    the field."""
    match_a_car = leaf('detections', 'match_elements', leaf('label', '=', 'car'))
    by_hand: list[tuple[Any, set[str]]] = [
        # Asked first, as main answers them: the match, and `is null` asked of the field.
        (match_a_car, WITH_A_CAR),
        ('detections is null', {'l-null', 'l-none'}),
        (leaf('detections', 'is_null'), {'l-null', 'l-none'}),
        # A list, empty or not, none of whose elements is a car.
        (negated(match_a_car), {'l-truck', 'l-truck-unlabelled', 'l-unlabelled', 'l-empty'}),
        ('detections.label != "car"', NOT_A_CAR),
        ('detections.label not in ("car", "bus")', NOT_A_CAR),
        (leaf('detections.label', '!=', 'car'), NOT_A_CAR),
        (leaf('detections.label', 'not in', ['car']), NOT_A_CAR),
        ('detections.label is null', NO_LABEL),
    ]

    async def scenario(r):
        p = await plant_labelled(r)
        for form, names in by_hand:
            node = await parsed(r, form, LABELLED) if isinstance(form, str) else form
            assert RULE.rows(node, LABEL_ROWS) == names, ("the test's own expectation", form)
            assert await shown(r, p, node) == names, form

    regions_world(scenario)


# A list field kept in a column of its own, made as the app makes it (the owner's `inferred_tags`): (the list sent for
# the sample, or None to add it without the field). a-null's column is then written NULL straight to the database.
PESTS: dict[str, Optional[list[str]]] = {
    'a-unset': None,
    'a-empty': [],
    'a-aphid': ['aphid'],
    'a-mite': ['mite'],
    'a-null': ['mite'],
}
NO_PEST = {'a-unset', 'a-empty', 'a-null'}  # no element: no value, as tags (rule 1.5's last paragraph)


def test_7_7_a_list_field_in_a_column_of_its_own_is_read_as_tags_are_an_empty_list_is_no_value(regions_world):
    """Rule 1.5's last paragraph (changed 2026-11-26, twice) on ARRAY_FIELD: the field made as the app makes it (POST
    array-fields, a `TEXT[] DEFAULT '{}'` column, as `dataset.create_array_field` does), the samples added through the
    API with their lists. The server cannot tell a sample added without the field from one given `[]`, so the column
    is read as tags are: neither has a value, nor has a column written NULL. A negation shows only the row whose list
    holds other values; `is null` and `is not null` answer as main does (the rows with no element; the rows with at
    least one). The shows and the absence tests are asked first."""

    async def scenario(r):
        await r.dataset(ARRAY_FIELD)
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{ARRAY_FIELD}/array-fields', json={'field_name': 'pests'})
        assert made.is_success, made.text
        samples = [
            {
                'filepath': f'{name}.jpg',
                'metadata': {'plot': name},
                **({} if held is None else {'array_fields': {'pests': held}}),
            }
            for name, held in PESTS.items()
        ]
        added = await post(r, f'/api/datasets/{ARRAY_FIELD}/samples', {'samples': samples})
        assert added.is_success, added.text
        stored = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', ARRAY_FIELD)
        p = Planted(ARRAY_FIELD, {row_['filepath'][: -len('.jpg')]: row_['id'] for row_ in stored}, 0)
        await r.database.execute('UPDATE samples SET pests = NULL WHERE id = $1', p.ids['a-null'])
        assert await r.database.fetchval('SELECT pests FROM samples WHERE id = $1', p.ids['a-null']) is None
        for node, names in (
            (leaf('pests', 'in', ['aphid']), {'a-aphid'}),
            (leaf('pests', 'contains', 'mite'), {'a-mite'}),
            (leaf('pests', 'is_null'), NO_PEST),
            (leaf('pests', 'is_not_null'), {'a-aphid', 'a-mite'}),
        ):
            assert await shown(r, p, node) == names, node
        for node in (
            leaf('pests', 'not in', ['aphid']),
            leaf('pests', '!=', 'aphid'),
            negated(leaf('pests', 'in', ['aphid'])),
            negated(leaf('pests', 'contains', 'aphid')),
        ):
            assert await shown(r, p, node) == {'a-mite'}, node

    regions_world(scenario)


# An element match (`match_elements`) on each kind of `detections`, for the contract's words on it (added 2026-11-25).
ELEMENT_HOLDS: dict[str, Any] = {
    'm-hit': [{'label': 'car', 'confidence': 0.95}],
    'm-miss': [{'label': 'car', 'confidence': 0.5}, {'label': 'truck', 'confidence': 0.95}],
    'm-unlabelled': [{'confidence': 0.99}],
    'm-empty': [],
    'm-null': None,
    'm-text': 'not a list',
}
ELEMENT_ROWS = [*ELEMENT_HOLDS, 'm-none']  # m-none has no `detections`
CONFIDENT_CAR = {
    'logic': 'AND',
    'filters': [leaf('label', '=', 'car'), leaf('confidence', '>', 0.9)],
}


def meets(element: Any) -> bool:
    return isinstance(element, dict) and element.get('label') == 'car' and element.get('confidence', 0) > 0.9


def test_rule_1_4_a_not_around_an_element_match_needs_a_list_empty_or_not_none_of_whose_elements_meets_it(
    regions_world,
):
    """Contract g7, rule 1.4 (changed 2026-11-26): an element match compares the field's list, which has a value when
    it is there, empty or not. So `NOT (detections match_elements {...})` shows only the rows whose `detections` is a
    list, the empty one included, none of whose elements meets the conditions; no `detections`, a JSON null or a value
    that is no list is not shown. On a dataset without objects; the match itself, which main answers alike, is asked
    first."""
    listed = {name for name, held in ELEMENT_HOLDS.items() if isinstance(held, list)}
    matched = {name for name in listed if any(meets(element) for element in ELEMENT_HOLDS[name])}
    assert (matched, listed - matched) == ({'m-hit'}, {'m-miss', 'm-unlabelled', 'm-empty'})

    async def scenario(r):
        await r.dataset(MATCHED)
        samples = [
            {
                'filepath': f'{name}.jpg',
                'metadata': {'plot': name, **({} if name == 'm-none' else {'detections': held})},
            }
            for name, held in [*ELEMENT_HOLDS.items(), ('m-none', None)]
        ]
        await datasets.add_samples(MATCHED, {'samples': samples}, user=ADMIN_USER)
        stored = await r.database.fetch('SELECT id, filepath, metadata FROM samples WHERE dataset_id = $1', MATCHED)
        for row_ in stored:
            metadata = row_['metadata'] if isinstance(row_['metadata'], dict) else json.loads(row_['metadata'])
            name = row_['filepath'][: -len('.jpg')]
            assert ('detections' in metadata) is (name != 'm-none'), name
            assert metadata.get('detections') == ELEMENT_HOLDS.get(name), name
        p = Planted(MATCHED, {row_['filepath'][: -len('.jpg')]: row_['id'] for row_ in stored}, 0)
        for operator in ('match_elements', 'elem_match'):
            match = leaf('detections', operator, CONFIDENT_CAR)
            assert await shown(r, p, match) == matched, operator
            assert await shown(r, p, negated(match)) == listed - matched, operator

    regions_world(scenario)


# Inside an element match, `not in` in every spelling (contract g7, 2.2, made exact 2026-11-25).
NOT_IN_HOLDS: dict[str, list[dict[str, Any]]] = {
    'e-car': boxes('car'),
    'e-truck': boxes('truck'),
    'e-both': boxes('car', 'truck'),
    'e-bus': boxes('bus'),
}


def test_rule_2_2_inside_an_element_match_not_in_is_taken_in_every_spelling(regions_world, monkeypatch):
    """`not in`, `not_in` and `nin` inside `match_elements` are one test: a detection whose label isn't car. Main
    refuses `not in` with a 400, and the SDK's own `F("label").not_in([...])` sends it; the script's call is asked
    through the SDK, against the server. Every detection here has a label. The match on `=`, and the two spellings
    main takes, are asked first."""

    async def scenario(r):
        await r.dataset(NOT_IN_ELEMENTS)
        samples = [{'filepath': f'{name}.jpg', 'metadata': {'detections': held}} for name, held in NOT_IN_HOLDS.items()]
        await datasets.add_samples(NOT_IN_ELEMENTS, {'samples': samples}, user=ADMIN_USER)
        stored = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', NOT_IN_ELEMENTS)
        p = Planted(NOT_IN_ELEMENTS, {row_['filepath'][: -len('.jpg')]: row_['id'] for row_ in stored}, 0)
        car = leaf('detections', 'match_elements', leaf('label', '=', 'car'))
        assert await shown(r, p, car) == {'e-car', 'e-both'}
        other = {'e-truck', 'e-both', 'e-bus'}  # a detection whose label isn't car
        for spelling in ('not_in', 'nin', 'not in'):  # the two main takes, then the one it refuses
            node = leaf('detections', 'match_elements', leaf('label', spelling, ['car']))
            response = await post(r, f'/api/datasets/{NOT_IN_ELEMENTS}/samples/query', {'limit': 100, 'filters': node})
            assert response.status_code == 200, (spelling, response.status_code, response.text)
            assert await shown(r, p, node) == other, spelling
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def script() -> tuple[set[int], int]:
            view = Dataset(f'dataset-{NOT_IN_ELEMENTS}').samples[
                match_elements('detections', F('label').not_in(['car']))
            ]
            return set(view.ids()), view.count()

        assert await asyncio.to_thread(script) == (p.of(other), len(other))

    regions_world(scenario)


# --- 5.3: the pages say what the NOT of an element match and `!=` on a list of labels do (2026-11-26) ---

PAGES = ['docs/FILTER_QUERY.md', 'docs/REGIONS.md', 'docs/MAP_REQUIREMENTS.md', 'docs/SDK.md', 'docs/ASSISTANT.md']
NEEDS_ONE = re.compile(r'\bat least one (element|label|detection)s?\b|\bnon-?empty list\b', re.I)
HIDING = re.compile(r'\b(negation|negated|not|NOT|exclu\w*|hides?)\b|!=')


def test_rule_5_3_no_page_says_an_element_matchs_not_or_a_label_lists_negation_needs_at_least_one_element():
    """Contract 5.3 (2026-11-26): every page of 5.1 and 5.2 that said the NOT of an element match, or `!=` on a list
    of labels, needs at least one element is brought to the empty list being a value. What the pages say of tags
    stays, and is not asked here; a sentence about `is null`, `is not null`, `exists` or `not exists` is no such
    sentence."""
    root = Path(__file__).resolve().parents[2]
    saying = []
    for page in PAGES:
        text = ' '.join((root / page).read_text().split())
        for found in re.split(r'(?<=[.;:])\s+', text):
            if re.search(r'is (not )?null|\bexists\b|not_exists|is_null|is_not_null|\btags?\b', found, re.I):
                continue
            if NEEDS_ONE.search(found) and HIDING.search(found):
                saying.append((page, found))
    assert not saying, saying
