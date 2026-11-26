"""On a dataset with objects, a filter is read on an image together with its objects (contract g5, objects filter
reading), tested where a user or a script meets it: the HTTP API signed in with real API tokens (the region tests'
world: a database of its own per test), with the filter wrapped as the dataset page wraps it, and the assistant's
tools. Written from the contract, not from the code.

The rule, in short: a row with no value for a field never matches a comparison of that field, nor the NOT of one;
only `is null` finds it (and `not exists`, by the key). On the images' grid an image has an object's field when one
of its objects has a value for it, and any object counts. A filter without NOT is met by the row's own values
together with its partner's (the image's on the objects' grid, ONE object's on the images' grid); a NOT, `is null`
and `not exists` are read for the row as a whole.

The fixtures (section 6.1). TABLE is exactly the owner's five images: 1 red with a car and a truck, 2 red with a truck,
3 blue with a car, 4 with no `colour` and a car, 5 red with no objects; each line of the owner's table is asked of it
on both grids, its expected rows copied from the table. FULL is the same five with more fields beside them, and adds an
image whose only object has no `label` (img-6) and an object with no image (`loose`); `source` is a field both kinds
have (img-6 holds it as a JSON null); `score` and `occluded` are a second and third object field; images inside and
outside a square place, with objects that have a location and objects that don't; a concept scored on the images
(`scene`) and one scored on the objects (`thing`). PLAIN is FULL's images without objects. ABSENT is a few images
with cars, for an absence test beside another test: a car with no `score` beside a scored truck, a car alone with no
`score`, an image that holds a `score` itself, and a car whose `score` is a JSON null.

Every expected set on FULL is computed by `Reading`, a plain function over the planted rows written from the
contract's words alone; `test_the_reference_reads_the_owners_table_as_the_owner_did` checks it against the owner's
table first, so a wrong expectation shows up as the test's own mistake. Many expectations are also written out by
hand in comments where a test leans on them.

The wrapping. The images' and the objects' grids send what main's FilterContext.withGrain makes
(frontend/contexts/FilterContext.tsx), written again here as `page_filter` (its excluded-place forms and the lasso
coupling included): the kind's predicate, then `cg_self` and `has_matching_object` (images) or `parent_matches`
(objects) around the stages. `test_the_page_filter_here_is_the_pages_withgrain` checks it against every case of
frontend/tests/fixtures/image-grain-cases.json, which the frontend's own test checks against the real withGrain. A
filter typed in one filter card is one stage (its one part, or the AND of its parts, as
frontend/components/filter-stage-editor.tsx saves it); the parse route turns the text into its parts.

Where the contract leaves a point open, these tests read it this way, and stay away from what it leaves open:
- "X shows the row" in rule 2.3 is X read by rules 2.1 and 2.2 (made exact): on the images' grid, X met by the image
  with ONE of its objects; "has a value" is asked field by field, of any object.
- `is null` and `not exists` are read for the row as a whole, alone or beside other tests (rules 2.5 and 2.6, made
  exact); `is not null` and `exists` are showing tests (rule 2.1). `exists` and `not exists` ask for the key, as today
  (a JSON null under the key counts as there); `is null` and `is not null` for the value.
- A place test (`geo_polygon`, `geo_region`) is not a comparison (section "Words" lists none): a NOT around one, and
  an exclusion, keep the rows with no location, as 2.8 says.
- Rule 2.11 ("nothing is stored") is tested by what it promises: a field that only images had is read on an object
  from the moment an object holds it, before and after the field statistics are counted.
- Rule 2.9: the page's form (page_filter) and the assistant's own (backend/assistant/grain.py, through make_view and
  get_samples) are both read by these rules.
- The built-in fields and lists (2.8) are asked in the forms the contract names (`id not in [...]`, `id != x`,
  `filepath != x`, `tags not in (...)`): alone and beside an image field as pins, and beside a test the partner meets
  as the new behaviour (made exact: the row the hiding test names is never shown). A NOT around one of them is left
  out (the contract's "to say in the report"), and so is an id of the other kind's row. A list field kept in a column
  of its own and `@local.tags` are not planted here.
- The SDK's side of 2.8 is asked as the SDK sends a filter: the same filter, not wrapped, through samples/query.
"""

import json
import os
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

import pytest

from backend.routers import datasets
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_assistant_conversation_spec import count_statistics
from backend.tests.test_assistant_core_spec import open_door
from backend.tests.test_assistant_views_spec import image_grain, result, view_ids
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_regions_spec import ADMIN_USER, Regions, nodes, post, selected
from backend.tests.test_upgrade_paths import ROOT

database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(
    not (os.getenv('FRIDGE_TEST_POSTGRES_DSN') and os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')),
    reason='Requires disposable PostgreSQL',
)

FULL, TABLE, PLAIN, ABSENT = 90, 91, 92, 93
SQUARE = [[0, 0], [5, 0], [5, 5], [0, 5]]  # the place: no planted point is on its border
CUT = 0.5


# --- The planted rows ---


@dataclass(frozen=True)
class Row:
    values: dict[str, Any]  # metadata as stored; None is a JSON null
    image: Optional[str] = None  # an object's image (None for an image, and for an object with no image)
    is_object: bool = False
    location: Optional[tuple[float, float]] = None
    tags: tuple[str, ...] = ()
    scores: dict[str, float] = field(default_factory=dict)  # concept name: score


def image(values: dict[str, Any], **more: Any) -> Row:
    return Row(values, **more)


def child(of: Optional[str], values: dict[str, Any], **more: Any) -> Row:
    return Row(values, image=of, is_object=True, **more)


# The owner's five images, and their objects, with nothing else.
TABLE_ROWS: dict[str, Row] = {
    'img-1': image({'colour': 'red'}),
    'img-2': image({'colour': 'red'}),
    'img-3': image({'colour': 'blue'}),
    'img-4': image({}),
    'img-5': image({'colour': 'red'}),
    'car-1': child('img-1', {'label': 'car'}),
    'truck-1': child('img-1', {'label': 'truck'}),
    'truck-2': child('img-2', {'label': 'truck'}),
    'car-3': child('img-3', {'label': 'car'}),
    'car-4': child('img-4', {'label': 'car'}),
}

# The same five with more fields, an image whose only object has no label, and an object with no image.
FULL_ROWS: dict[str, Row] = {
    'img-1': image(
        {'colour': 'red', 'source': 'drone', 'plot': 'p1', 'n': 1},
        location=(2.5, 2.5),
        tags=('seen',),
        scores={'scene': 0.75},
    ),
    'img-2': image(
        {'colour': 'red', 'source': 'tractor', 'plot': 'p2', 'n': 2},
        location=(32.0, 2.5),
        tags=('rejected',),
        scores={'scene': 0.25},
    ),
    'img-3': image({'colour': 'blue', 'source': 'tractor', 'plot': 'p3', 'n': 3}, scores={'scene': 0.5}),
    'img-4': image({'plot': 'p4', 'n': 4}, location=(1.0, 1.0)),
    'img-5': image(
        {'colour': 'red', 'source': 'drone', 'plot': 'p5', 'n': 5},
        location=(100.0, 50.0),
        scores={'scene': 0.875},
    ),
    'img-6': image({'colour': 'blue', 'source': None, 'plot': 'p6', 'n': 6}, scores={'scene': 0.125}),
    'car-1': child('img-1', {'label': 'car', 'score': 0.5, 'occluded': False}, scores={'thing': 0.25}),
    'truck-1': child(
        'img-1',
        {'label': 'truck', 'score': 0.95, 'occluded': True},
        location=(2.5, 2.5),
        scores={'thing': 0.75},
    ),
    'truck-2': child(
        'img-2',
        {'label': 'truck', 'score': 0.2, 'occluded': False, 'source': 'drone'},
        location=(32.0, 2.5),
        scores={'thing': 0.375},
    ),
    'car-3': child(
        'img-3',
        {'label': 'car', 'score': 0.95, 'occluded': False},
        location=(1.5, 1.5),
        tags=('rejected',),
        scores={'thing': 0.875},
    ),
    'car-4': child('img-4', {'label': 'car', 'score': 0.3, 'occluded': True, 'source': 'tractor'}),
    'part-6': child('img-6', {'score': 0.99, 'occluded': False}, scores={'thing': 0.125}),
    'loose': child(None, {'label': 'car', 'score': 0.99}, location=(4.0, 4.0), scores={'thing': 0.625}),
}

PLAIN_ROWS = {name: row for name, row in FULL_ROWS.items() if not row.is_object}

# For an absence test beside another test (rules 2.5 and 2.6): cars with and without a `score`.
ABSENT_ROWS: dict[str, Row] = {
    'img-a': image({'colour': 'red'}),  # a car with no score, and a truck that has one
    'img-b': image({'colour': 'red'}),  # a car with no score, alone
    'img-c': image({'colour': 'blue', 'score': 0.4}),  # the image holds a score; its car has none
    'img-d': image({'colour': 'red'}),  # a truck with no score, no car
    'img-e': image({'colour': 'red'}),  # a car with a score
    'img-f': image({'colour': 'red'}),  # a car whose score is a JSON null: the key is there, no value
    'car-a': child('img-a', {'label': 'car'}),
    'truck-a': child('img-a', {'label': 'truck', 'score': 0.95}),
    'car-b': child('img-b', {'label': 'car'}),
    'car-c': child('img-c', {'label': 'car'}),
    'truck-d': child('img-d', {'label': 'truck'}),
    'car-e': child('img-e', {'label': 'car', 'score': 0.5}),
    'car-f': child('img-f', {'label': 'car', 'score': None}),
}
CONCEPTS = {'scene': 'clip', 'thing': 'clip_obj'}  # name: embedding field


@dataclass
class Planted:
    dataset_id: int
    rows: dict[str, Row]
    ids: dict[str, int]
    concepts: dict[int, str]  # concept id: name

    @property
    def objects(self) -> bool:
        return any(row.is_object for row in self.rows.values())

    def names(self, ids: set[int]) -> set[str]:
        by_id = {sample_id: name for name, sample_id in self.ids.items()}
        return {by_id[sample_id] for sample_id in ids}

    def reading(self) -> 'Reading':
        return Reading(self.rows, self.concepts)


def sample(name: str, row: Row, ids: dict[str, int]) -> dict[str, Any]:
    metadata = dict(row.values)
    if row.image is not None:
        metadata['parent_id'] = ids[row.image]
    body: dict[str, Any] = {'filepath': f'{name}.jpg', 'metadata': metadata, 'tags': list(row.tags)}
    if row.is_object:
        body['media_type'] = 'object'
    if row.location is not None:
        body['location'] = {'type': 'Point', 'coordinates': list(row.location)}
    return body


async def plant(r: Regions, dataset_id: int, rows: dict[str, Row]) -> Planted:
    """The rows as a dataset: the images first, then the objects naming them; each concept scoring its rows."""
    await r.dataset(dataset_id)
    ids: dict[str, int] = {}
    for objects in (False, True):
        batch = [sample(name, row, ids) for name, row in rows.items() if row.is_object is objects]
        if batch:
            await datasets.add_samples(dataset_id, {'samples': batch}, user=ADMIN_USER)
        stored = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', dataset_id)
        ids = {item['filepath'][: -len('.jpg')]: item['id'] for item in stored}
    assert set(ids) == set(rows), sorted(ids)
    links = await r.database.fetch(
        "SELECT id, parent_id, media_type, metadata FROM samples WHERE dataset_id = $1",
        dataset_id,
    )
    names = {sample_id: name for name, sample_id in ids.items()}
    for link in links:
        row = rows[names[link['id']]]
        assert (link['media_type'] == 'object') is row.is_object, names[link['id']]
        assert link['parent_id'] == (ids[row.image] if row.image else None), names[link['id']]
        stored_metadata = json.loads(link['metadata']) if isinstance(link['metadata'], str) else link['metadata']
        for key, value in row.values.items():  # a JSON null is kept as one
            assert key in stored_metadata and stored_metadata[key] == value, (names[link['id']], key)
    concepts: dict[int, str] = {}
    for name, embedding_field in CONCEPTS.items():
        scored = {row_name: row.scores[name] for row_name, row in rows.items() if name in row.scores}
        if not scored:
            continue
        concept = await r.database.fetchval(
            'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
            "VALUES ($1, $2, $3, 'seed', '[1,0]', $4) RETURNING id",
            dataset_id,
            name,
            embedding_field,
            CUT,
        )
        concepts[concept] = name
        for row_name, score in scored.items():
            await r.database.execute(
                'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, $3)',
                concept,
                ids[row_name],
                score,
            )
    return Planted(dataset_id, rows, ids, concepts)


# --- The page's wrapping: main's withGrain (frontend/contexts/FilterContext.tsx), written again ---

EXCLUDES = {
    'geo_polygon_exclude': 'geo_polygon',
    'geo_region_exclude': 'geo_region',
    'embedding_polygon_exclude': 'embedding_polygon',
}


def sole_rule(node: dict[str, Any]) -> dict[str, Any]:
    """The node, or the one rule of AND groups of one rule around it (frontend/lib/filter-query.ts soleRule)."""
    while node.get('logic') == 'AND' and len(node.get('filters') or []) == 1:
        node = node['filters'][0]
    return node


def page_filter(stages: list[dict[str, Any]], grain: str, has_objects: bool = True) -> dict[str, Any]:
    """withGrain({logic: AND, filters: stages}, grain, null, has_objects): what the page's grid of that grain sends."""
    kind = {'field': 'media_type', 'operator': '=' if grain == 'object' else '!=', 'value': 'object'}
    if not stages:
        return {'logic': 'AND', 'filters': [kind]}
    if not has_objects:
        return {'logic': 'AND', 'filters': [kind, *stages]}
    cross = 'parent_matches' if grain == 'object' else 'has_matching_object'

    def region_exclude(node: dict[str, Any]) -> bool:
        return sole_rule(node).get('operator') in EXCLUDES

    def off_grain_lasso(node: dict[str, Any]) -> bool:
        lasso_grain = 'object' if node.get('field') == 'clip_obj' else 'image'
        return node.get('operator') == 'embedding_polygon' and lasso_grain != grain

    def coupled(node: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {'field': '_cgself', 'operator': 'cg_self', 'value': node},
            {'field': '_crossgrain', 'operator': cross, 'value': node},
        ]

    excluded = [
        rule
        for rule in (sole_rule(node) for node in stages if region_exclude(node))
        if rule['operator'] == 'geo_region_exclude' or (isinstance(rule.get('value'), list) and len(rule['value']) >= 3)
    ]
    lassos = [node for node in stages if not region_exclude(node) and off_grain_lasso(node)]
    rest = [node for node in stages if not region_exclude(node) and not off_grain_lasso(node)]
    parts: list[dict[str, Any]] = [kind]
    if rest:
        parts.append({'logic': 'OR', 'filters': coupled({'logic': 'AND', 'filters': rest})})
    for lasso in lassos:
        parts.append({'field': '_crossemb', 'operator': cross, 'value': {'logic': 'AND', 'filters': [lasso]}})
    for region in excluded:
        inside = {'logic': 'AND', 'filters': [{**region, 'operator': EXCLUDES[region['operator']]}]}
        parts.append({'logic': 'NOT', 'filters': [{'logic': 'OR', 'filters': coupled(inside)}]})
    return {'logic': 'AND', 'filters': parts}


def card(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """One filter card's stage: its one part, or the AND of its parts (filter-stage-editor.tsx)."""
    return parts[0] if len(parts) == 1 else {'logic': 'AND', 'filters': parts}


# --- The reference: the contract's reading, from its words ---

COMPARISONS = {'=', '!=', 'in', 'not in', '<', '<=', '>', '>=', 'between', 'contains'}
CONCEPT_TESTS = {'concept_select', 'concept_exclude'}
SHOW_OF = {'!=': '=', 'not in': 'in', 'concept_exclude': 'concept_select'}  # rule 2.4: each is the NOT of its show
PLACE_EXCLUDES = {'geo_polygon_exclude': 'geo_polygon'}  # an exclusion: NOT (inside), keeping rows with no location
# Rules 2.5 and 2.6: `is null` (no value) and `not exists` (no key) are read for the row as a whole; `is not null`
# and `exists` are showing tests. A JSON null is no value, but its key is there.
ABSENCE, SHOWING_PRESENCE = {'is_null', 'not_exists'}, {'is_not_null', 'exists'}


def inside(point: tuple[float, float], polygon: list[list[float]]) -> bool:
    """Inside an axis-aligned rectangle given as a polygon (no planted point lies on a border)."""
    lons, lats = [corner[0] for corner in polygon], [corner[1] for corner in polygon]
    return min(lons) < point[0] < max(lons) and min(lats) < point[1] < max(lats)


def compare(value: Any, operator: str, wanted: Any) -> bool:
    if operator == '=':
        return bool(value == wanted)
    if operator == 'in':
        return value in wanted
    if operator == 'contains':
        return str(wanted).lower() in str(value).lower()
    if operator == 'between':
        return bool(wanted[0] <= value <= wanted[1])
    return bool(
        {
            '<': value < wanted,
            '<=': value <= wanted,
            '>': value > wanted,
            '>=': value >= wanted,
        }[operator],
    )


class Reading:
    """What each grid shows for a filter, by the contract's section 2, over the planted rows.

    A row has a value for a field when the value is there and isn't a JSON null; a concept's score is a row's value
    for that concept. Rule 2.1: without NOT, the objects' grid meets each test with the object's or its image's value,
    the images' grid with the image's or ONE object's (or the image's alone). Rule 2.3: `NOT (X)` shows the row when it
    has a value for every field compared inside X (the image with any of its objects; the object with its image) and
    the grid does not show the row for X. Rule 2.4: `!=`, `not in` and a concept's exclude are the NOT of their show.
    Rules 2.5 and 2.6: `is null` and `not exists` are read for the row as a whole, `is not null` and `exists` as
    showing tests; `exists` asks for the key. Rule 2.7: a row with no partner has its own values only.
    An excluded place is NOT (inside), with no field to have (2.8)."""

    def __init__(self, rows: dict[str, Row], concepts: Optional[dict[int, str]] = None):
        self.rows = rows
        self.concepts = concepts or {}

    def value(self, row: Row, leaf: dict[str, Any]) -> Any:
        if leaf['operator'] in CONCEPT_TESTS:
            return row.scores.get(self.concepts[int(leaf['value']['concept_id'])])
        if leaf['operator'] in ('geo_polygon', *PLACE_EXCLUDES):
            return row.location
        return row.values.get(leaf['field'])

    def has(self, row: Row, leaf: dict[str, Any]) -> bool:
        return self.value(row, leaf) is not None

    def present(self, row: Row, leaf: dict[str, Any], operator: str) -> bool:
        """What an absence test asks of a row: a value (`is null`, `is not null`), or the key (`exists`)."""
        return leaf['field'] in row.values if operator in ('exists', 'not_exists') else self.has(row, leaf)

    def passes(self, row: Row, leaf: dict[str, Any]) -> bool:
        """A showing test, on one row's own value."""
        value = self.value(row, leaf)
        if value is None:
            return False
        operator = leaf['operator']
        if operator == 'geo_polygon':
            return inside(value, leaf['value'])
        if operator == 'concept_select':
            return compare(value, leaf['value']['op'], float(leaf['value']['threshold']))
        assert operator in COMPARISONS - set(SHOW_OF), leaf
        return compare(value, operator, leaf['value'])

    def compared(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        """The comparisons inside a node (each standing for its field)."""
        if 'filters' in node or 'logic' in node:
            return [leaf for part in node.get('filters') or [] for leaf in self.compared(part)]
        return [node] if node['operator'] in COMPARISONS | CONCEPT_TESTS else []

    def met(
        self,
        node: dict[str, Any],
        rows: list[Row],
        whole: list[Row],
        negation: Callable[[dict[str, Any]], bool],
    ) -> bool:
        if 'filters' in node or 'logic' in node:
            logic = str(node.get('logic', 'AND')).upper()
            parts = node.get('filters') or []
            if logic == 'NOT':
                return negation({'logic': 'AND', 'filters': parts})
            found = [self.met(part, rows, whole, negation) for part in parts]
            return all(found) if logic == 'AND' else any(found)
        operator = node['operator']
        if operator in SHOW_OF:
            return negation({**node, 'operator': SHOW_OF[operator]})
        if operator in PLACE_EXCLUDES:
            return negation({**node, 'operator': PLACE_EXCLUDES[operator]})
        if operator in ABSENCE:
            return not any(self.present(row, node, operator) for row in whole)
        if operator in SHOWING_PRESENCE:
            return any(self.present(row, node, operator) for row in rows)
        return any(self.passes(row, node) for row in rows)

    def has_every_field(self, whole: list[Row], inside_not: dict[str, Any]) -> bool:
        return all(any(self.has(row, leaf) for row in whole) for leaf in self.compared(inside_not))

    def objects_of(self, name: str) -> list[Row]:
        return [row for row in self.rows.values() if row.image == name]

    def image_shows(self, name: str, node: dict[str, Any]) -> bool:
        own = self.rows[name]
        objects = self.objects_of(name)
        whole = [own, *objects]

        def negation(inside_not: dict[str, Any]) -> bool:
            return self.has_every_field(whole, inside_not) and not self.image_shows(name, inside_not)

        return any(self.met(node, [own, item], whole, negation) for item in objects) or self.met(
            node,
            [own],
            whole,
            negation,
        )

    def object_shows(self, name: str, node: dict[str, Any]) -> bool:
        own = self.rows[name]
        pair = [own, *([self.rows[own.image]] if own.image else [])]

        def negation(inside_not: dict[str, Any]) -> bool:
            return self.has_every_field(pair, inside_not) and not self.object_shows(name, inside_not)

        return self.met(node, pair, pair, negation)

    def images(self, node: dict[str, Any]) -> set[str]:
        return {name for name, row in self.rows.items() if not row.is_object and self.image_shows(name, node)}

    def objects(self, node: dict[str, Any]) -> set[str]:
        return {name for name, row in self.rows.items() if row.is_object and self.object_shows(name, node)}

    def grid(self, grain: str, node: dict[str, Any]) -> set[str]:
        return self.images(node) if grain == 'image' else self.objects(node)

    # Today's readings, for the pins of 2.8.

    def each_row(self, node: dict[str, Any], names: Any) -> set[str]:
        """Each row on its own values, as a dataset without objects and a filter sent without the wrapping are read
        (2.8): a negation is the complement of its show, so it keeps the rows with no value."""

        def keeps(row: Row, part: dict[str, Any]) -> bool:
            if 'filters' in part or 'logic' in part:
                logic = str(part.get('logic', 'AND')).upper()
                found = [keeps(row, item) for item in part.get('filters') or []]
                if logic == 'NOT':
                    return not all(found)
                return all(found) if logic == 'AND' else any(found)
            operator = part['operator']
            if operator in SHOW_OF:
                return not keeps(row, {**part, 'operator': SHOW_OF[operator]})
            if operator == 'is_null':
                return not self.has(row, part)
            if operator == 'is_not_null':
                return self.has(row, part)
            assert operator not in ('exists', 'not_exists'), part  # key presence today: not asked
            return self.passes(row, part)

        return {name for name in names if keeps(self.rows[name], node)}

    def today_showing(self, grain: str, node: dict[str, Any]) -> set[str]:
        """What main shows for a filter of showing tests only: the row meets the whole filter on its own values, or
        its partner does (an image's object, an object's image)."""
        assert not any(leaf['operator'] in SHOW_OF for leaf in self.compared(node)), node
        alone = self.each_row(node, self.rows)
        if grain == 'image':
            return {
                name
                for name, row in self.rows.items()
                if not row.is_object and (name in alone or any(row_.image == name for row_ in self.of(alone)))
            }
        return {
            name
            for name, row in self.rows.items()
            if row.is_object and (name in alone or (row.image is not None and row.image in alone))
        }

    def of(self, names: set[str]) -> list[Row]:
        return [self.rows[name] for name in names]


# --- Asking the grids ---


async def parts_of(r: Regions, planted: Planted, text: str) -> list[dict[str, Any]]:
    return await nodes(r, text, planted.dataset_id)


async def stage_of(r: Regions, planted: Planted, form: Any) -> dict[str, Any]:
    """One filter card: query text read by the parse route into its parts, or a filter node as it is."""
    return card(await parts_of(r, planted, form)) if isinstance(form, str) else form


async def shown(r: Regions, planted: Planted, grain: str, *forms: Any) -> set[str]:
    """What the grid of `grain` shows for these filter cards (one stage each), by its list and its count (which
    `selected` checks agree): the names of its rows."""
    stages = [await stage_of(r, planted, form) for form in forms]
    ids = await selected(r, page_filter(stages, grain, planted.objects), planted.dataset_id)
    return planted.names(ids)


async def expected(r: Regions, planted: Planted, grain: str, form: Any) -> set[str]:
    """What the contract's reading shows for one filter card."""
    stage = await stage_of(r, planted, form)
    return planted.reading().grid(grain, {'logic': 'AND', 'filters': [stage]})


async def assert_reads(r: Regions, planted: Planted, forms: list[Any], grains: tuple[str, ...] = ('image', 'object')):
    """Each form on each grid shows what the contract's reading shows; the forms are asked in order."""
    for form in forms:
        for grain in grains:
            want = await expected(r, planted, grain, form)
            got = await shown(r, planted, grain, form)
            assert got == want, (grain, form, 'shown', sorted(got), 'expected', sorted(want))


def leaf(field_: str, operator: str, value: Any = None) -> dict[str, Any]:
    return {'field': field_, 'operator': operator, 'value': value}


def negated(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'NOT', 'filters': list(parts)}


def both(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'AND', 'filters': list(parts)}


def either(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'OR', 'filters': list(parts)}


# --- The reference and the wrapping, checked first ---

# The owner's table: (the filter as text, as a node, the images' grid, the objects' grid).
OWNERS_TABLE: list[tuple[str, dict[str, Any], set[str], set[str]]] = [
    (
        'colour = "red" AND label = "car"',
        both(leaf('colour', '=', 'red'), leaf('label', '=', 'car')),
        {'img-1'},
        {'car-1'},
    ),
    ('colour != "red"', leaf('colour', '!=', 'red'), {'img-3'}, {'car-3'}),
    ('NOT (colour = "red")', negated(leaf('colour', '=', 'red')), {'img-3'}, {'car-3'}),
    ('label != "car"', leaf('label', '!=', 'car'), {'img-2'}, {'truck-1', 'truck-2'}),
    ('NOT (label = "car")', negated(leaf('label', '=', 'car')), {'img-2'}, {'truck-1', 'truck-2'}),
    (
        'NOT (colour = "red" AND label = "car")',
        negated(both(leaf('colour', '=', 'red'), leaf('label', '=', 'car'))),
        {'img-2', 'img-3'},
        {'truck-1', 'truck-2', 'car-3'},
    ),
    ('colour is null', leaf('colour', 'is_null'), {'img-4'}, {'car-4'}),
    ('label is null', leaf('label', 'is_null'), {'img-5'}, set()),
]
TABLE_IDS = [
    'red-and-car',
    'colour-not-red',
    'not-colour-red',
    'label-not-car',
    'not-label-car',
    'not-red-and-car',
    'colour-is-null',
    'label-is-null',
]


def test_the_reference_reads_the_owners_table_as_the_owner_did():
    reading = Reading(TABLE_ROWS)
    for text, node, images, objects in OWNERS_TABLE:
        assert reading.images(node) == images, text
        assert reading.objects(node) == objects, text


def test_the_page_filter_here_is_the_pages_withgrain():
    """page_filter reproduces every case the frontend's own test checks against the real withGrain (image grain), and
    for a bare filter it is the suite's other copy of it (test_assistant_views_spec.image_grain)."""
    cases = json.loads((ROOT / 'frontend/tests/fixtures/image-grain-cases.json').read_text())
    for case in cases:
        assert page_filter(case['stages'], 'image', case['has_objects']) == case['filter'], case['name']
    for _, node, _, _ in OWNERS_TABLE:
        assert page_filter([node], 'image') == image_grain({'logic': 'AND', 'filters': [node]})
    # The objects' grid: the kind's predicate and the cross operator differ, nothing else.
    stage = leaf('colour', '=', 'red')
    assert page_filter([stage], 'object') == {
        'logic': 'AND',
        'filters': [
            {'field': 'media_type', 'operator': '=', 'value': 'object'},
            {
                'logic': 'OR',
                'filters': [
                    {'field': '_cgself', 'operator': 'cg_self', 'value': both(stage)},
                    {'field': '_crossgrain', 'operator': 'parent_matches', 'value': both(stage)},
                ],
            },
        ],
    }


# --- The owner's table, line by line, on both grids (section 6.2) ---


@pytest.mark.parametrize('line', range(len(OWNERS_TABLE)), ids=TABLE_IDS)
def test_the_owners_table_on_the_images_grid(regions_world, line):
    text, _, images, _ = OWNERS_TABLE[line]

    async def scenario(r):
        planted = await plant(r, TABLE, TABLE_ROWS)
        assert await shown(r, planted, 'image') == {name for name in TABLE_ROWS if name.startswith('img-')}
        assert await shown(r, planted, 'image', text) == images, text

    regions_world(scenario)


@pytest.mark.parametrize('line', range(len(OWNERS_TABLE)), ids=TABLE_IDS)
def test_the_owners_table_on_the_objects_grid(regions_world, line):
    text, _, _, objects = OWNERS_TABLE[line]

    async def scenario(r):
        planted = await plant(r, TABLE, TABLE_ROWS)
        assert await shown(r, planted, 'object') == {name for name, row in TABLE_ROWS.items() if row.is_object}
        assert await shown(r, planted, 'object', text) == objects, text

    regions_world(scenario)


# --- Section 2, rule by rule, on FULL ---


def test_rule_2_1_a_filter_without_negation_is_met_by_the_row_together_with_its_partner(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        reading = planted.reading()
        # Hand-checked: an image field on the objects' grid is read on the object's image, an object field on the
        # images' grid on its objects; together, by the image with one object.
        red_and_car = both(leaf('colour', '=', 'red'), leaf('label', '=', 'car'))
        assert reading.images(red_and_car) == {'img-1'} and reading.objects(red_and_car) == {'car-1'}
        tractor = leaf('source', '=', 'tractor')
        assert reading.images(tractor) == {'img-2', 'img-3', 'img-4'}
        assert reading.objects(tractor) == {'truck-2', 'car-3', 'car-4'}
        await assert_reads(
            r,
            planted,
            [
                # Asked first, as main answers them: each test on one kind's field, and an OR across the kinds.
                'colour = "red"',
                'label = "car"',
                'colour = "blue" OR label = "truck"',
                # Then the pairs: an image field with an object field, and a field both kinds have.
                'colour = "red" AND label = "car"',
                'colour in ("red", "blue") AND label = "car"',
                'colour = "red" AND score > 0.9',
                'source = "tractor"',
                'source = "drone" AND label = "car"',
            ],
        )

    regions_world(scenario)


def test_rule_2_2_tests_joined_by_and_are_met_with_the_same_object(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        # img-1's car scores 0.5 and its truck 0.95: no ONE object of img-1 is a car scoring over 0.9.
        assert await shown(r, planted, 'image', 'label = "car" AND score > 0.9') == {'img-3'}
        assert await shown(r, planted, 'object', 'label = "car" AND score > 0.9') == {'car-3', 'loose'}
        # A red image that has a car, and one with a truck scoring over 0.9: img-2's truck scores 0.2.
        assert await shown(r, planted, 'image', 'colour = "red" AND label = "car"') == {'img-1'}
        assert await shown(r, planted, 'image', 'colour = "red" AND label = "truck" AND score > 0.9') == {'img-1'}
        assert await shown(r, planted, 'object', 'colour = "red" AND label = "truck" AND score > 0.9') == {'truck-1'}
        await assert_reads(
            r,
            planted,
            ['label = "car" AND score > 0.9', 'colour = "red" AND label = "car"', 'label = "car" AND occluded = true'],
        )

    regions_world(scenario)


def test_rule_2_3_a_not_shows_a_row_that_has_every_compared_field_and_that_the_inside_does_not_show(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        # Asked first, as main answers it: a NOT around one field of the images.
        assert await shown(r, planted, 'image', 'NOT (colour = "red")') == {'img-3', 'img-6'}
        assert await shown(r, planted, 'object', 'NOT (colour = "red")') == {'car-3', 'part-6'}
        # The image as a whole: img-1 has a car, so it is out though it has a truck; img-5 and img-6 have no label.
        assert await shown(r, planted, 'image', 'NOT (label = "car")') == {'img-2'}
        # Two fields, both needed: img-4 has no colour, img-5 no label; img-6 has no label (its object has none).
        assert await shown(r, planted, 'image', 'NOT (colour = "red" AND label = "car")') == {'img-2', 'img-3'}
        assert await shown(r, planted, 'object', 'NOT (colour = "red" AND label = "car")') == {
            'truck-1',
            'truck-2',
            'car-3',
        }
        # A field both kinds have: img-2's truck holds `drone`, so img-2 is out; img-6's JSON null is no value.
        assert await shown(r, planted, 'image', 'NOT (source = "drone")') == {'img-3', 'img-4'}
        assert await shown(r, planted, 'object', 'NOT (source = "drone")') == {'car-3', 'car-4'}
        await assert_reads(
            r,
            planted,
            [
                'NOT (colour = "red")',
                'NOT (label = "car")',
                'NOT (colour = "red" AND label = "car")',
                'NOT (colour = "blue" AND source = "tractor")',
                'NOT (source = "drone")',
                'NOT (label = "car" OR colour = "blue")',
            ],
        )

    regions_world(scenario)


def test_rule_2_3_a_not_over_two_object_fields_reads_its_inside_with_one_object(regions_world):
    """Made exact: "X shows the row" is X read with ONE object; "has a value" is asked field by field, of any
    object."""

    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        text = 'NOT (label = "car" AND score > 0.9)'
        # Asked first, as main answers it: a NOT around one field of the images.
        assert await shown(r, planted, 'image', 'NOT (colour = "red")') == {'img-3', 'img-6'}
        # img-3's car scores 0.95: hidden. img-1's car scores 0.5 and its truck 0.95: shown (it has a label and a
        # score, and no one object is a car scoring over 0.9). img-5 has no label; img-6's object has no label.
        assert await shown(r, planted, 'image', text) == {'img-1', 'img-2', 'img-4'}
        # `loose` is a car scoring 0.99; part-6 has no label.
        assert await shown(r, planted, 'object', text) == {'car-1', 'truck-1', 'truck-2', 'car-4'}
        await assert_reads(r, planted, [text, 'NOT (label = "truck" AND occluded = true)'])

    regions_world(scenario)


def test_rule_2_4_not_equal_not_in_and_a_concepts_exclude_are_the_not_of_their_show(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        pairs = [
            ('colour != "red"', 'NOT (colour = "red")'),
            ('label != "car"', 'NOT (label = "car")'),
            ('label not in ("car", "bus")', 'NOT (label in ("car", "bus"))'),
            ('source != "drone"', 'NOT (source = "drone")'),
            (f'not concept "thing" > {CUT}', f'NOT (concept "thing" > {CUT})'),
        ]
        for written, as_not in pairs:
            for grain in ('image', 'object'):
                want = await expected(r, planted, grain, as_not)
                assert await shown(r, planted, grain, as_not) == want, (grain, as_not)
                assert await shown(r, planted, grain, written) == want, (grain, written)
        # The objects' concept, on the images' grid: only images whose objects are scored, none above the cut
        # (img-1's truck scores 0.75, img-3's car 0.875; img-4's car has no score; img-5 has no objects).
        assert await shown(r, planted, 'image', f'not concept "thing" > {CUT}') == {'img-2', 'img-6'}
        assert await shown(r, planted, 'object', f'not concept "thing" > {CUT}') == {'car-1', 'truck-2', 'part-6'}

    regions_world(scenario)


def test_rule_2_5_is_null_shows_the_rows_with_no_value_and_not_exists_those_with_no_key_on_either_side(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        # Asked first, as main answers it: an image field's absence on the images' grid.
        assert await shown(r, planted, 'image', 'colour is null') == {'img-4'}
        # On the objects' grid, neither the object nor its image; `loose` has no image.
        assert await shown(r, planted, 'object', 'colour is null') == {'car-4', 'loose'}
        # On the images' grid, neither the image nor any of its objects.
        assert await shown(r, planted, 'image', 'label is null') == {'img-5', 'img-6'}
        assert await shown(r, planted, 'object', 'label is null') == {'part-6'}
        # A JSON null is no value: img-6 holds `source` as null and its object has none.
        assert await shown(r, planted, 'image', 'source is null') == {'img-6'}
        assert await shown(r, planted, 'object', 'source is null') == {'part-6', 'loose'}
        await assert_reads(
            r,
            planted,
            [
                'colour is null',
                'label is null',
                'source is null',
                'colour is not null',
                'label is not null',
                'source is not null',
                'label exists',
                'label not exists',
                'colour not exists',
                # An absence test inside a NOT adds no field: NOT (colour is null) is `colour is not null`.
                'NOT (colour is null)',
                'NOT (label is null)',
            ],
        )
        for grain in ('image', 'object'):
            assert await shown(r, planted, grain, 'NOT (colour is null)') == await shown(
                r,
                planted,
                grain,
                'colour is not null',
            ), grain
        # `exists` and `not exists` ask for the key, as today: img-6 holds `source` as a JSON null, so it has the key
        # and no value. `not exists` is read for the row as a whole (img-4 lacks the key, its car holds it); `exists`
        # is a showing test.
        images = {name for name, row in FULL_ROWS.items() if not row.is_object}
        objects = {name for name, row in FULL_ROWS.items() if row.is_object}
        assert await shown(r, planted, 'image', 'source not exists') == set()
        assert await shown(r, planted, 'object', 'source not exists') == {'loose'}
        assert await shown(r, planted, 'image', 'source exists') == images
        assert await shown(r, planted, 'object', 'source exists') == objects - {'loose'}
        await assert_reads(r, planted, ['source not exists', 'source exists', 'NOT (source exists)'])

    regions_world(scenario)


def test_rule_2_6_a_not_inside_a_larger_filter_is_read_first_for_the_row_as_a_whole(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        text = 'label = "car" AND NOT (occluded = true)'
        # The images that have a car and no occluded object: img-1's truck is occluded, img-4's car is.
        assert await shown(r, planted, 'image', text) == {'img-3'}
        # "A car that is not occluded" is the objects' grid's question: `loose` has no `occluded`, so no value.
        assert await shown(r, planted, 'object', text) == {'car-1', 'car-3'}
        await assert_reads(
            r,
            planted,
            [
                text,
                'label = "truck" AND NOT (colour = "blue")',
                'colour = "red" AND NOT (label = "car")',
                'source = "tractor" AND NOT (label = "car")',
            ],
        )

    regions_world(scenario)


def test_rule_2_6_is_null_and_not_exists_beside_other_tests_are_read_first_for_the_row_as_a_whole(regions_world):
    """Made exact: `is null` and `not exists` beside other tests are read first and for the row as a whole; `is not
    null` and `exists` are showing tests, met with the same object (rule 2.1)."""

    async def scenario(r):
        planted = await plant(r, ABSENT, ABSENT_ROWS)
        # Asked first, as main answers it: the car alone.
        assert await shown(r, planted, 'image', 'label = "car"') == {'img-a', 'img-b', 'img-c', 'img-e', 'img-f'}
        # The images that have a car and in which no object, nor the image, has a score: img-a's truck has one, img-c
        # holds one itself, img-e's car has one; img-f's car holds a JSON null, no value.
        assert await shown(r, planted, 'image', 'label = "car" AND score is null') == {'img-b', 'img-f'}
        # On the objects' grid, the car with its image: car-c's image holds a score; car-a's truck is not its partner.
        assert await shown(r, planted, 'object', 'label = "car" AND score is null') == {'car-a', 'car-b', 'car-f'}
        # `not exists` asks for the key: img-f's car holds it (as a JSON null).
        assert await shown(r, planted, 'image', 'label = "car" AND score not exists') == {'img-b'}
        assert await shown(r, planted, 'object', 'label = "car" AND score not exists') == {'car-a', 'car-b'}
        # The showing forms, with one object: img-a's car has no score (its truck does); img-c's image holds one.
        assert await shown(r, planted, 'image', 'label = "car" AND score is not null') == {'img-c', 'img-e'}
        assert await shown(r, planted, 'image', 'label = "car" AND score exists') == {'img-c', 'img-e', 'img-f'}
        assert await shown(r, planted, 'object', 'label = "car" AND score is not null') == {'car-c', 'car-e'}
        assert await shown(r, planted, 'object', 'label = "car" AND score exists') == {'car-c', 'car-e', 'car-f'}
        await assert_reads(
            r,
            planted,
            [
                'label = "car" AND score is null',
                'label = "car" AND score not exists',
                'label = "car" AND score is not null',
                'label = "car" AND score exists',
                'colour = "red" AND score is null',
                'label = "truck" AND score is null',
            ],
        )

    regions_world(scenario)


def test_rule_2_7_an_object_with_no_image_and_an_image_with_no_objects_have_only_their_own_values(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        # Asked first, as main answers them: `loose` by its own values; img-5 by its own.
        assert 'loose' in await shown(r, planted, 'object', 'label = "car" AND score > 0.9')
        assert 'img-5' in await shown(r, planted, 'image', 'colour = "red" AND source = "drone"')
        # No image: no colour to meet, no image to bring a value.
        assert 'loose' not in await shown(r, planted, 'object', 'colour = "red" AND label = "car"')
        assert 'loose' in await shown(r, planted, 'object', 'colour is null')
        assert 'loose' not in await shown(r, planted, 'object', 'NOT (colour = "red")')
        # No objects: no label to meet, none to have.
        assert 'img-5' not in await shown(r, planted, 'image', 'colour = "red" AND label = "car"')
        assert 'img-5' in await shown(r, planted, 'image', 'label is null')
        assert 'img-5' not in await shown(r, planted, 'image', 'NOT (colour = "red" AND label = "car")')
        await assert_reads(
            r,
            planted,
            [
                'label = "car" AND score > 0.9',
                'colour = "red" AND source = "drone"',
                'colour = "red" OR label = "car"',
                'colour is null',
                'label is null',
                'NOT (colour = "red" AND label = "car")',
                'label != "truck"',
            ],
        )

    regions_world(scenario)


# --- 2.8: what must not change (each a pin, passing on main), and what changes for places ---


def test_rule_2_8_pin_a_dataset_without_objects_reads_each_row_on_its_own_values_as_today(regions_world):
    async def scenario(r):
        planted = await plant(r, PLAIN, PLAIN_ROWS)
        reading = planted.reading()
        # Hand-checked: a negation keeps the images with no value too, here.
        assert reading.each_row(leaf('colour', '!=', 'red'), PLAIN_ROWS) == {'img-3', 'img-4', 'img-6'}
        for text in (
            'colour != "red"',
            'NOT (colour = "red")',
            'colour not in ("red")',
            'NOT (colour = "red" AND source = "drone")',
            'source != "drone"',
            'colour = "red" AND source = "tractor"',
            'colour is null',
            'source is null',
            f'not concept "scene" > {CUT}',
            f'concept "scene" > {CUT}',
        ):
            stage = card(await parts_of(r, planted, text))
            want = reading.each_row(stage, PLAIN_ROWS)
            assert await shown(r, planted, 'image', text) == want, text
        # The page sends the plain form here: no wrapping operator.
        assert page_filter([leaf('colour', '!=', 'red')], 'image', has_objects=False) == both(
            leaf('media_type', '!=', 'object'),
            leaf('colour', '!=', 'red'),
        )

    regions_world(scenario)


def test_rule_2_8_pin_a_filter_of_showing_tests_keeps_every_row_it_keeps_today(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        reading = planted.reading()
        forms: list[Any] = [
            'colour = "red"',
            'label = "car"',
            'label = "car" AND score > 0.9',
            'colour = "blue" OR label = "truck"',
            'source = "tractor"',
            'colour = "red" AND label = "car"',
            f'concept "thing" > {CUT}',
            f'concept "scene" > {CUT}',
            leaf('location', 'geo_polygon', SQUARE),
        ]
        for form in forms:
            stage = await stage_of(r, planted, form)
            for grain in ('image', 'object'):
                today = reading.today_showing(grain, both(stage))
                got = await shown(r, planted, grain, form)
                assert today <= got, (grain, form, 'lost', sorted(today - got))

    regions_world(scenario)


def test_rule_2_8_pin_a_negation_of_one_image_field_and_an_images_concept_exclude_keep_todays_rows(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        cases: list[tuple[str, set[str], set[str]]] = [
            ('colour != "red"', {'img-3', 'img-6'}, {'car-3', 'part-6'}),
            ('colour not in ("red", "green")', {'img-3', 'img-6'}, {'car-3', 'part-6'}),
            ('NOT (colour = "blue")', {'img-1', 'img-2', 'img-5'}, {'car-1', 'truck-1', 'truck-2'}),
            ('NOT (colour contains "LU")', {'img-1', 'img-2', 'img-5'}, {'car-1', 'truck-1', 'truck-2'}),
            # Scored on the images: img-3 at the cut is not above it; img-4 and `loose` have no score.
            (f'not concept "scene" > {CUT}', {'img-2', 'img-3', 'img-6'}, {'truck-2', 'car-3', 'part-6'}),
        ]
        for text, images, objects in cases:
            assert (await expected(r, planted, 'image', text), await expected(r, planted, 'object', text)) == (
                images,
                objects,
            ), text
            assert await shown(r, planted, 'image', text) == images, text
            assert await shown(r, planted, 'object', text) == objects, text

    regions_world(scenario)


def test_rule_2_8_pin_a_hiding_test_of_a_built_in_field_is_answered_by_the_row_of_the_grid(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        images = {name for name, row in FULL_ROWS.items() if not row.is_object}
        objects = {name for name, row in FULL_ROWS.items() if row.is_object}
        ids = planted.ids
        # img-1 and img-3 stay out, their objects aside; car-1 and truck-2 stay out, their images aside.
        hidden_images = leaf('id', 'not in', [ids['img-1'], ids['img-3']])
        assert await shown(r, planted, 'image', hidden_images) == images - {'img-1', 'img-3'}
        hidden_objects = leaf('id', 'not in', [ids['car-1'], ids['truck-2']])
        assert await shown(r, planted, 'object', hidden_objects) == objects - {'car-1', 'truck-2'}
        assert await shown(r, planted, 'image', leaf('id', '!=', ids['img-2'])) == images - {'img-2'}
        assert await shown(r, planted, 'object', leaf('id', '!=', ids['car-3'])) == objects - {'car-3'}
        assert await shown(r, planted, 'image', leaf('filepath', '!=', 'img-4.jpg')) == images - {'img-4'}
        assert await shown(r, planted, 'object', leaf('filepath', '!=', 'car-4.jpg')) == objects - {'car-4'}
        # Beside a test of the images, on the images' grid: a selection with a row unticked.
        red = both(leaf('colour', '=', 'red'), hidden_images)
        assert await shown(r, planted, 'image', red) == {'img-2', 'img-5'}

    regions_world(scenario)


def test_rule_2_8_the_row_a_hiding_test_of_a_built_in_field_names_is_not_shown_whichever_row_meets_the_rest(
    regions_world,
):
    """Made exact (2.8, the one new case for the built-in fields): the row of the grid that `id not in [...]`, `id !=`
    or `filepath !=` names is not shown, even when the rest of the filter is met through its partner."""

    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        ids = planted.ids
        # Asked first, as main answers them: the hiding test alone.
        assert 'truck-1' not in await shown(r, planted, 'object', leaf('id', 'not in', [ids['truck-1']]))
        assert 'img-3' not in await shown(r, planted, 'image', leaf('id', 'not in', [ids['img-3']]))
        # The objects' grid: truck-1's image is red, and truck-1 is the row unticked.
        red_but_truck_1 = both(leaf('colour', '=', 'red'), leaf('id', 'not in', [ids['truck-1']]))
        assert await shown(r, planted, 'object', red_but_truck_1) == {'car-1', 'truck-2'}
        # The images' grid: img-3 has a car, and img-3 is the row unticked.
        cars_but_img_3 = both(leaf('label', '=', 'car'), leaf('id', 'not in', [ids['img-3']]))
        assert await shown(r, planted, 'image', cars_but_img_3) == {'img-1', 'img-4'}
        # The same with `id !=` and `filepath !=`.
        assert await shown(
            r,
            planted,
            'object',
            both(leaf('colour', '=', 'red'), leaf('id', '!=', ids['truck-1'])),
        ) == {
            'car-1',
            'truck-2',
        }
        assert await shown(
            r,
            planted,
            'image',
            both(leaf('label', '=', 'car'), leaf('filepath', '!=', 'img-3.jpg')),
        ) == {
            'img-1',
            'img-4',
        }

    regions_world(scenario)


def test_rule_2_8_pin_tags_not_in_keeps_the_rows_with_no_tags_and_is_answered_by_the_row_of_the_grid(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        images = {name for name, row in FULL_ROWS.items() if not row.is_object}
        objects = {name for name, row in FULL_ROWS.items() if row.is_object}
        # img-2 is rejected, and car-3: img-3 stays (its car aside), truck-2 stays (its image aside).
        assert await shown(r, planted, 'image', 'tags not in ("rejected")') == images - {'img-2'}
        assert await shown(r, planted, 'object', 'tags not in ("rejected")') == objects - {'car-3'}
        assert await shown(r, planted, 'image', 'tags not in ("rejected", "seen")') == images - {'img-1', 'img-2'}

    regions_world(scenario)


EXCLUDED_SQUARE = leaf('location', 'geo_polygon_exclude', SQUARE)
OUTSIDE_IMAGES = {'img-2', 'img-5', 'img-6'}  # not inside, and no object inside (img-3's car is; img-6 has no point)
OUTSIDE_OBJECTS = {'truck-2', 'part-6'}  # not inside, and its image not inside (`loose` is inside)


def test_rule_2_8_pin_an_excluded_place_keeps_the_rows_with_no_location_on_both_grids(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        reading = planted.reading()
        assert reading.images(both(EXCLUDED_SQUARE)) == OUTSIDE_IMAGES
        assert reading.objects(both(EXCLUDED_SQUARE)) == OUTSIDE_OBJECTS
        for name, stage in (
            ('the exclusion', EXCLUDED_SQUARE),
            ("the exclusion as a filter card's Builder tab saves it", both(EXCLUDED_SQUARE)),
        ):
            assert await shown(r, planted, 'image', stage) == OUTSIDE_IMAGES, name
            assert await shown(r, planted, 'object', stage) == OUTSIDE_OBJECTS, name
        # A polygon of two points is no polygon and excludes nothing.
        everything = {name for name, row in FULL_ROWS.items() if not row.is_object}
        no_polygon = leaf('location', 'geo_polygon_exclude', SQUARE[:2])
        assert await shown(r, planted, 'image', no_polygon) == everything

    regions_world(scenario)


def test_rule_2_8_a_not_around_a_place_an_exclusion_in_an_or_or_in_a_card_shows_what_the_exclusion_shows(regions_world):
    """What changes for places (2.8): each of these shows the rows the exclusion standing alone shows, on both
    grids. The card holds the exclusion beside a test of the objects."""

    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        place = leaf('location', 'geo_polygon', SQUARE)
        vehicle = leaf('label', 'in', ['car', 'truck'])
        never = leaf('plot', '=', 'no-such-plot')
        # Asked first, as main answers it: the exclusion standing alone.
        assert await shown(r, planted, 'image', EXCLUDED_SQUARE) == OUTSIDE_IMAGES
        assert await shown(r, planted, 'object', EXCLUDED_SQUARE) == OUTSIDE_OBJECTS
        for name, stage, images, objects in (
            ('NOT around the place', negated(place), OUTSIDE_IMAGES, OUTSIDE_OBJECTS),
            ('NOT around the place, in a group', negated(both(place)), OUTSIDE_IMAGES, OUTSIDE_OBJECTS),
            ('the exclusion inside an OR', either(EXCLUDED_SQUARE, never), OUTSIDE_IMAGES, OUTSIDE_OBJECTS),
            # Beside a label test in one card: img-1 (inside) is out though its car has no location.
            ('the exclusion sharing a card', both(EXCLUDED_SQUARE, vehicle), {'img-2'}, {'truck-2'}),
        ):
            assert (planted.reading().images(both(stage)), planted.reading().objects(both(stage))) == (
                images,
                objects,
            ), name
            assert await shown(r, planted, 'image', stage) == images, name
            assert await shown(r, planted, 'object', stage) == objects, name

    regions_world(scenario)


def test_rule_2_8_pin_a_filter_sent_without_the_wrapping_reads_each_row_on_its_own_values(regions_world):
    """As the SDK sends a filter: samples/query with the filter itself, images and objects alike, each row on its own
    values (a negation keeps the rows with no value)."""

    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        reading = planted.reading()
        assert reading.each_row(both(leaf('colour', '=', 'red'), leaf('label', '=', 'car')), FULL_ROWS) == set()
        for text in (
            'colour = "red" AND label = "car"',
            'label = "car"',
            'colour != "red"',
            'NOT (colour = "red" AND label = "car")',
            'label is null',
            'colour is null',
        ):
            filters = await parts_of(r, planted, text)
            ids = await selected(r, {'logic': 'AND', 'filters': filters}, FULL)
            assert planted.names(ids) == reading.each_row(both(*filters), FULL_ROWS), text

    regions_world(scenario)


def test_rule_2_8_pin_a_versions_query_refuses_the_wrapping_operators_as_today(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        response = await post(r, f'/api/datasets/{FULL}/versions', {'name': 'before'})
        assert response.status_code == 200, response.text
        version = await r.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', FULL)
        stage = card(await parts_of(r, planted, 'colour = "red" AND label = "car"'))
        refusal = {'detail': "Filter on '_cgself' (cg_self) reads live data that a version does not store"}
        for grain in ('image', 'object'):
            response = await post(
                r,
                f'/api/datasets/{FULL}/versions/{version}/samples/query',
                {'filters': page_filter([stage], grain), 'limit': 100},
            )
            assert (response.status_code, response.json()) == (400, refusal), (grain, response.text)

    regions_world(scenario)


# --- 2.9 to 2.12 ---


def test_rule_2_9_the_page_and_the_assistant_send_todays_form_and_it_is_read_by_these_rules(regions_world):
    """The page's form (page_filter) and the assistant's own (backend/assistant/grain.py, through make_view and
    get_samples) show the owner's table on the images' grid. The lines main already answers come first."""

    async def scenario(r):
        planted = await plant(r, TABLE, TABLE_ROWS)
        await open_door(r)
        order = [1, 2, 6, 0, 3, 4, 5, 7]
        for line in order:
            text, _, images, _ = OWNERS_TABLE[line]
            assert await shown(r, planted, 'image', text) == images, ('the page', text)
            view = await result(ADMIN, 'make_view', {'dataset': TABLE, 'filter': text})
            found = planted.names(await view_ids(ADMIN, view['view']))
            assert (view['count'], found) == (len(images), images), ('the assistant', text, view['count'], found)

    regions_world(scenario)


async def every_reader(r: Regions, planted: Planted, text: str, tag: str) -> None:
    """Every reader of the page's wrapped filter keeps, for the text, the rows the contract's reading shows."""
    stage = card(await parts_of(r, planted, text))
    images = planted.reading().images(both(stage))
    objects = planted.reading().objects(both(stage))
    assert images and objects, text
    by_name = planted.ids
    where = f'/api/datasets/{FULL}'
    for grain, rows in (('image', images), ('object', objects)):
        node = page_filter([stage], grain)
        ids = {by_name[name] for name in rows}
        # The grid's list and its count (the export reads the same query), and the top bar's count.
        assert await selected(r, node, FULL) == ids, (grain, text)
        response = await post(r, f'{where}/samples/query', {'filters': node, 'limit': 1})
        assert response.json()['total'] == len(ids), (grain, text, response.text)
        aggregate = await post(
            r,
            f'{where}/aggregate',
            {'filters': node, 'aggregations': [{'field': 'plot', 'op': 'count'}]},
        )
        assert aggregate.json()['results'][0]['value'] == len(ids), (grain, text, aggregate.text)
        # Select All and its token.
        token = (await post(r, f'{where}/selection_token', {'filters': node})).json()
        assert token['count'] == len(ids), (grain, text, token)
        in_token = {'field': 'id', 'operator': 'in_selection', 'value': token['token']}
        assert await selected(r, in_token, FULL) == ids, (grain, text)
        # A bulk edit by the filter: its count, and the rows it tagged.
        tagged = await post(r, f'{where}/samples/bulk-tag', {'filters': node, 'add_tags': [f'{tag}-{grain}']})
        assert tagged.status_code in (200, 202), tagged.text
        if tagged.status_code == 202:
            count = (await finish(r.database))['result']['updated_count']
        else:
            count = tagged.json()['updated_count']
        rows_tagged = await r.database.fetch(
            'SELECT id FROM samples WHERE dataset_id = $1 AND $2 = ANY(tags)',
            FULL,
            f'{tag}-{grain}',
        )
        assert (count, {row['id'] for row in rows_tagged}) == (len(ids), ids), (grain, text)
    # The panels that read the images: histogram, sidebar facet and values, map, Sparse Sample.
    node = page_filter([stage], 'image')
    ns = sorted(planted.rows[name].values['n'] for name in images)
    plots = sorted(planted.rows[name].values['plot'] for name in images)
    located = {by_name[name] for name in images if planted.rows[name].location is not None}
    histogram = (await post(r, f'{where}/histogram', {'field': 'n', 'filters': node, 'bins': 10})).json()
    assert histogram['total'] == len(images), (text, histogram)
    [facet] = (await post(r, f'{where}/field-stats', {'filters': node, 'fields': ['n']})).json()
    assert (facet['min'], facet['max']) == (ns[0], ns[-1]), (text, facet)
    values = (await post(r, f'{where}/fields/plot/autocomplete', {'filters': node, 'limit': 100})).json()
    assert sorted(item['value'] for item in values['values']) == plots, (text, values)
    points = (await post(r, f'{where}/map_points', {'filters': node, 'points': True})).json()
    assert {item['id'] for item in points['items']} == located, (text, points)
    box = (await post(r, f'{where}/map_extent', {'filters': node})).json()
    assert box['count'] == len(located), (text, box)
    sparse = await post(r, f'{where}/sparse-sample', {'count': 100, 'fields': ['n'], 'filters': node})
    assert set(sparse.json()['sample_ids']) == {by_name[name] for name in images}, (text, sparse.text)
    # The assistant's tools.
    view = await result(ADMIN, 'make_view', {'dataset': FULL, 'filter': text})
    assert (view['count'], planted.names(await view_ids(ADMIN, view['view']))) == (len(images), images), text


def test_rule_2_10_the_reading_holds_in_every_reader_of_the_wrapped_filter(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        await open_door(r)
        # Asked first, as main answers it: an object field alone.
        await every_reader(r, planted, 'label in ("car", "truck")', 'vehicle')
        # Red images with a car or a truck: img-1 and img-2; on the objects' grid car-1, truck-1 and truck-2.
        await every_reader(r, planted, 'colour = "red" AND label in ("car", "truck")', 'red-vehicle')

    regions_world(scenario)


async def edit(r: Regions, planted: Planted, name: str, **body: Any) -> None:
    response = await r.call(
        ADMIN,
        'PATCH',
        f'/api/datasets/{planted.dataset_id}/samples/{planted.ids[name]}',
        json=body,
    )
    assert response.status_code == 200, response.text


def with_values(planted: Planted, name: str, values: dict[str, Any]) -> Planted:
    rows = dict(planted.rows)
    rows[name] = replace(rows[name], values=values)
    return replace(planted, rows=rows)


def test_rule_2_11_nothing_is_stored_about_which_kind_of_row_has_which_field(regions_world):
    """The reading follows the values alone: the same before and after the field statistics are counted, and a
    field only the images held is read on an object from the moment one holds it, and no longer once it doesn't."""

    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        texts = ['colour != "red"', 'colour = "red" AND label = "car"', 'colour is null']
        # Asked first, as main answers it.
        assert await shown(r, planted, 'image', 'colour != "red"') == {'img-3', 'img-6'}
        before = {
            (grain, text): await shown(r, planted, grain, text) for grain in ('image', 'object') for text in texts
        }
        await count_statistics(r, FULL)
        after = {(grain, text): await shown(r, planted, grain, text) for grain in ('image', 'object') for text in texts}
        assert after == before
        # car-3 (img-3 is blue) is given a colour: img-3 now has a red object, so `colour != "red"` hides it.
        await edit(r, planted, 'car-3', metadata={'colour': 'red'})
        changed = with_values(planted, 'car-3', {**FULL_ROWS['car-3'].values, 'colour': 'red'})
        assert await shown(r, changed, 'image', 'colour != "red"') == {'img-6'}
        assert await shown(r, changed, 'object', 'colour != "red"') == {'part-6'}
        await assert_reads(r, changed, texts)
        # And taken away again.
        await edit(r, planted, 'car-3', remove_metadata_keys=['colour'])
        await assert_reads(r, planted, texts)

    regions_world(scenario)


def test_rule_2_12_a_value_written_to_a_row_is_found_by_a_filter_at_once_on_both_grids(regions_world):
    async def scenario(r):
        planted = await plant(r, FULL, FULL_ROWS)
        await edit(r, planted, 'truck-2', metadata={'label': 'car'})
        changed = with_values(planted, 'truck-2', {**FULL_ROWS['truck-2'].values, 'label': 'car'})
        # Asked first, as main answers it: the new value alone.
        assert 'img-2' in await shown(r, changed, 'image', 'label = "car"')
        assert 'truck-2' in await shown(r, changed, 'object', 'label = "car"')
        # With an image field: img-2 is red, and now has a car.
        assert await shown(r, changed, 'image', 'colour = "red" AND label = "car"') == {'img-1', 'img-2'}
        assert await shown(r, changed, 'object', 'colour = "red" AND label = "car"') == {'car-1', 'truck-2'}
        # A value written to an image: img-3 turns red, and has a car.
        await edit(r, planted, 'img-3', metadata={'colour': 'red'})
        changed = with_values(changed, 'img-3', {**FULL_ROWS['img-3'].values, 'colour': 'red'})
        assert await shown(r, changed, 'image', 'colour = "red" AND label = "car"') == {'img-1', 'img-2', 'img-3'}
        assert await shown(r, changed, 'object', 'colour = "red" AND label = "car"') == {'car-1', 'truck-2', 'car-3'}
        await assert_reads(r, changed, ['colour = "red" AND label = "car"', 'label != "car"', 'NOT (colour = "red")'])

    regions_world(scenario)
