"""The sidebar lists a field's values whichever kind of row has the field, and says so when it can't (contract g12),
tested at the HTTP API with real tokens (the region tests' world: a database of its own per test), the view wrapped
as the page wraps it (test_objects_filter_reading_spec.page_filter). Written from the contract, not from the code.

Readers (7.1): the suggestions (POST .../fields/{f}/autocomplete), the opened field (POST .../field-stats with `fields`
and `stream`, as getFieldFacet sends it), lightning off (POST .../field-stats, lightning_mode false), the sidebar's own
request on the default view (lightning on, no stage: it computes there; with a stage it answers the kept lists by
design, 7.9 and 14), and each list's range (min/max). The expected values come from `read`, rule 1.3 read over the
planted rows with the objects spec's Reading: the grid's own rows the view shows, and the partners whose pair meets the
view (an object with no image is no image's partner, 7.3); with no kind, each row on its own values (1.6). The own
rule is kept by every reader, on every dataset, with a kind or without (13, 14). The reference is checked against
values written by hand and against the property form of rule 1.3 (a value is read iff adding `f = v` shows a row),
and against the server's own grids (samples/query) in a test of its own.

Where the contract leaves a point open, these tests read it this way:
- A range where the field has no value for the view (14, 16): `min` and `max` null, and not labelled as a
  dataset-wide answer (`cached`, `pending`, `out_of_budget`), on every dataset and for every request, with a kind or
  without. The sidebar's own request computes only on the default view, where every number field here has a value,
  so the empty range is asked of the opened field and of lightning off.
- Rule 1.6 is pinned with exactly its four moves left out (15, 16): on a dataset without objects, the picker of a
  field with an applied rule offers the values the view has (asserted here); a pending selection narrows the picker
  (the page's, tested in frontend/tests/sidebar-view-spec.test.cjs and the browser); the own rule is kept in a request
  with no kind that carries a filter (asserted here, on datasets with and without objects); a range the view has no
  value for is no range (asserted here, on datasets with and without objects).
- The uncounted reading (7.7) is asked with the counted limit at zero and its row stop patched small (7.11, 14's
  names). "Rows" are the rows that hold the typed term (it takes the first values that hold it and stops at the row
  stop): a term few rows hold reaches the end and is complete. Which values it keeps when it is not complete is not
  defined (14): at most the limit, in the order of the value in "C", each one showing a row.
- The kept list, the last resort (both limits at zero), is asked only for its form: no counts, not complete (14).
"""

import json
import os
from collections import Counter
from typing import Any, Optional

import pytest

from backend.routers import datasets
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_assistant_conversation_spec import count_statistics
from backend.tests.test_assistant_cost_spec import index_field
from backend.tests.test_objects_filter_reading_spec import Reading, Row, child, image, page_filter, plant
from backend.tests.test_regions_spec import Regions, post, selected

database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(
    not (os.getenv('FRIDGE_TEST_POSTGRES_DSN') and os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')),
    reason='Requires disposable PostgreSQL',
)

# --- The route's limits (14), set in the tests, never met by making data large ---
COUNTED_LIMIT = '_AUTOCOMPLETE_BUDGET_S'  # the counted reading's time limit (2 s)
UNCOUNTED_LIMIT = '_AUTOCOMPLETE_UNCOUNTED_BUDGET_S'  # the uncounted reading's own time limit (1 s)
UNCOUNTED_ROWS = '_AUTOCOMPLETE_UNCOUNTED_ROWS'  # the rows the uncounted reading reads before it stops (1,000)
LIGHTNING_LIMITS = ('_LIGHTNING_FIELD_STATS_BUDGET_S', '_LIGHTNING_PROBE_BUDGET_S', '_LIGHTNING_SIDEBAR_BUDGET_S')
PLENTY = 600.0
ROW_STOP = 20  # the uncounted reading's row stop in these tests

OBJECTS, PROBE, PLAIN, VARIED = 120, 121, 122, 123  # the datasets

# Images and objects each have fields the other lacks (colour, alt / label, make, weight, occluded), both have
# source and level, and two objects have no image (van-0, car-0).
ROWS: dict[str, Row] = {
    'img-1': image({'colour': 'red', 'alt': 10, 'source': 'drone', 'level': 1}),
    'img-2': image({'colour': 'red', 'alt': 20, 'source': 'tractor'}),
    'img-3': image({'colour': 'blue', 'alt': 30, 'source': 'tractor', 'level': 3}),
    'img-4': image({'alt': 40, 'source': 'drone'}),
    'img-5': image({'colour': 'green', 'alt': 50}),
    'img-6': image({'colour': 'blue', 'source': 'drone'}),
    'car-1': child(
        'img-1',
        {'label': 'car', 'make': 'ford', 'weight': 100, 'occluded': False, 'source': 'cam-a', 'level': 7},
    ),
    'truck-1': child('img-1', {'label': 'truck', 'make': 'scania', 'weight': 300, 'occluded': True}),
    'truck-2': child(
        'img-2',
        {'label': 'truck', 'make': 'ford', 'weight': 250, 'occluded': False, 'source': 'drone', 'level': 9},
    ),
    'car-3': child('img-3', {'label': 'car', 'make': 'audi', 'weight': 120, 'occluded': False}),
    'person-3': child('img-3', {'label': 'person', 'weight': 70, 'occluded': True, 'source': 'cam-a'}),
    'car-4': child('img-4', {'label': 'car', 'make': 'volvo', 'weight': 140, 'occluded': True}),
    'bike-6': child('img-6', {'label': 'bike', 'make': 'ford'}),
    'van-0': child(
        None,
        {'label': 'van', 'make': 'kia', 'weight': 999, 'occluded': True, 'source': 'cam-z', 'level': 99},
    ),
    'car-0': child(None, {'label': 'car', 'make': 'tesla', 'weight': 5, 'occluded': False}),
}
TEXT_FIELDS = ('colour', 'label', 'make', 'source')
NUMBER_FIELDS = ('alt', 'weight', 'level')
LISTED = (*TEXT_FIELDS, *NUMBER_FIELDS)  # the fields a list or a range is asked of (a boolean lists no values)
FIELDS = (*LISTED, 'occluded')
KINDS = ('image', 'object', 'none')
KEPT = ('cached', 'pending', 'out_of_budget')  # the provenances of a dataset-wide answer


def leaf(field: str, operator: str, value: Any = None) -> dict[str, Any]:
    return {'field': field, 'operator': operator, 'value': value}


def negated(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'NOT', 'filters': list(parts)}


def both(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'AND', 'filters': list(parts)}


VIEWS: dict[str, list[dict[str, Any]]] = {
    'nothing': [],
    'colour = red': [leaf('colour', '=', 'red')],
    'colour = green': [leaf('colour', '=', 'green')],  # img-5, which has no objects: empty ranges
    'label = car': [leaf('label', '=', 'car')],
    'NOT (label = car)': [negated(leaf('label', '=', 'car'))],
    'make != ford': [leaf('make', '!=', 'ford')],
    'colour = red, label = car': [leaf('colour', '=', 'red'), leaf('label', '=', 'car')],
    'label in (car, truck)': [leaf('label', 'in', ['car', 'truck'])],
    'source = cam-a': [leaf('source', '=', 'cam-a')],
    'alt > 25': [leaf('alt', '>', 25)],
    'weight >= 120': [leaf('weight', '>=', 120)],
}


def fields_tested_by(stages: list[dict[str, Any]]) -> set[str]:
    """The fields a view's stages test (a field asked of such a view has its own rule in it)."""
    found: set[str] = set()
    for node in stages:
        if 'filters' in node:
            found |= fields_tested_by(node['filters'])
        else:
            found.add(node['field'])
    return found


# --- The reference: rule 1.3 (with 7.3 and 13) over the planted rows ---


def text(value: Any) -> str:
    """A value as the route answers it (metadata->>'f')."""
    return value if isinstance(value, str) else json.dumps(value)


def pair_meets(reading: Reading, rows: dict[str, Row], image_name: str, object_name: str, node: dict) -> bool:
    """The image and this one object meet the view's tests, a NOT read for the image as a whole (rule 1.3)."""
    own = rows[image_name]
    whole = [own, *reading.objects_of(image_name)]

    def negation(inside: dict[str, Any]) -> bool:
        return reading.has_every_field(whole, inside) and not reading.image_shows(image_name, inside)

    return reading.met(node, [own, rows[object_name]], whole, negation)


def read(rows: dict[str, Row], kind: str, stages: list[dict[str, Any]], field: str) -> list[tuple[str, Any]]:
    """(row, value) for every row the reading reads a field's values from: the grid's own rows the view shows and the
    partners whose pair meets it (7.2); an object with no image is no image's partner (7.3); with no kind, each row on
    its own values (1.6)."""
    reading = Reading(rows)
    node = both(*stages)
    if kind == 'none':
        names = set(reading.each_row(node, rows))
    else:
        shown = reading.grid(kind, node)
        if kind == 'image':
            partners = {
                name
                for name, row in rows.items()
                if row.is_object and row.image is not None and pair_meets(reading, rows, row.image, name, node)
            }
        else:
            partners = {image_name for name in shown if (image_name := rows[name].image) is not None}
        names = shown | partners
    return [(name, rows[name].values[field]) for name in sorted(names) if rows[name].values.get(field) is not None]


def counts(rows: dict[str, Row], kind: str, stages: list[dict[str, Any]], field: str) -> dict[str, int]:
    """Rule 7.5: the rows that have the value among the rows the reading reads."""
    return dict(Counter(text(value) for _, value in read(rows, kind, stages, field)))


def span(rows: dict[str, Row], kind: str, stages: list[dict[str, Any]], field: str) -> Optional[tuple[float, float]]:
    numbers = [float(value) for _, value in read(rows, kind, stages, field)]
    return (min(numbers), max(numbers)) if numbers else None


def shows(rows: dict[str, Row], kind: str, stages: list[dict[str, Any]]) -> bool:
    reading = Reading(rows)
    node = both(*stages)
    return bool(reading.each_row(node, rows) if kind == 'none' else reading.grid(kind, node))


def candidates(rows: dict[str, Row], field: str) -> list[Any]:
    seen: dict[str, Any] = {}
    for row in rows.values():
        if row.values.get(field) is not None:
            seen.setdefault(text(row.values[field]), row.values[field])
    return list(seen.values())


# Written out by hand from ROWS: (kind, view, field) -> the suggestions' values and numbers.
BY_HAND: dict[tuple[str, str, str], dict[str, int]] = {
    ('image', 'nothing', 'label'): {'car': 3, 'truck': 2, 'person': 1, 'bike': 1},
    ('image', 'nothing', 'source'): {'drone': 4, 'tractor': 2, 'cam-a': 2},
    ('image', 'label = car', 'make'): {'ford': 1, 'audi': 1, 'volvo': 1},
    ('image', 'label = car', 'occluded'): {'false': 2, 'true': 1},
    ('image', 'label = car', 'label'): {'car': 3},
    ('image', 'label = car', 'source'): {'drone': 2, 'tractor': 1, 'cam-a': 1},
    ('image', 'NOT (label = car)', 'label'): {'truck': 1, 'bike': 1},
    ('image', 'NOT (label = car)', 'make'): {'ford': 2},
    ('image', 'make != ford', 'label'): {'car': 2, 'person': 1},
    ('image', 'colour = red', 'make'): {'ford': 2, 'scania': 1},
    ('image', 'colour = green', 'label'): {},
    ('object', 'nothing', 'label'): {'car': 4, 'truck': 2, 'person': 1, 'bike': 1, 'van': 1},
    ('object', 'nothing', 'colour'): {'red': 2, 'blue': 2},
    ('object', 'nothing', 'source'): {'cam-a': 2, 'drone': 4, 'cam-z': 1, 'tractor': 2},
    ('none', 'label = car', 'label'): {'car': 4},
    ('none', 'colour = red', 'label'): {},
}


def test_the_reference_reads_rule_1_3_as_the_hand_does():
    """The reference against sets written by hand, and against the property form of rule 1.3: a value is read iff
    adding `f = v` to the view shows a row."""
    for (kind, view, field), by_hand in BY_HAND.items():
        assert counts(ROWS, kind, VIEWS[view], field) == by_hand, (kind, view, field)
    assert span(ROWS, 'image', [], 'weight') == (70, 300) and span(ROWS, 'object', [], 'weight') == (5, 999)
    assert span(ROWS, 'image', [], 'level') == (1, 9) and span(ROWS, 'object', [], 'level') == (1, 99)
    assert span(ROWS, 'image', VIEWS['colour = green'], 'weight') is None
    for kind in KINDS:
        for view, stages in VIEWS.items():
            for field in FIELDS:
                showing = {
                    text(value)
                    for value in candidates(ROWS, field)
                    if shows(ROWS, kind, [*stages, leaf(field, '=', value)])
                }
                assert set(counts(ROWS, kind, stages, field)) == showing, (kind, view, field)


# --- Asking the server ---


def unhurried(monkeypatch: Any) -> None:
    """Every time limit far away, so no answer depends on the machine's load (7.11)."""
    for name in (*LIGHTNING_LIMITS, COUNTED_LIMIT):
        monkeypatch.setattr(datasets, name, PLENTY)
    monkeypatch.setattr(datasets, UNCOUNTED_LIMIT, PLENTY, raising=False)


def view_of(kind: str, stages: list[dict[str, Any]], has_objects: bool = True) -> Optional[dict[str, Any]]:
    """The view a request of that kind sends: the page's wrapping, or (no kind) the stages alone, as a script would."""
    if kind == 'none':
        return both(*stages) if stages else None
    return page_filter(stages, kind, has_objects)


async def ok(response: Any, where: Any) -> Any:
    assert response.status_code == 200, (where, response.status_code, response.text)
    return response.json() if 'ndjson' not in response.headers.get('content-type', '') else response.text


async def suggest(r: Regions, dataset_id: int, field: str, view: Optional[dict], q: str = '', limit: int = 100) -> dict:
    """The suggestions' answer, its shape checked: {"values": [{"value", "count"}], "exhaustive"} (14)."""
    body: dict[str, Any] = {'q': q, 'limit': limit}
    if view is not None:
        body['filters'] = view
    answer = await ok(await post(r, f'/api/datasets/{dataset_id}/fields/{field}/autocomplete', body), (field, view, q))
    assert set(answer) == {'values', 'exhaustive'} and isinstance(answer['exhaustive'], bool), answer
    assert all(set(item) == {'value', 'count'} for item in answer['values']), answer
    return answer


def offered(answer: dict) -> dict[str, Optional[int]]:
    return {item['value']: item['count'] for item in answer['values']}


def by_count(answer: dict) -> bool:
    """The values come with their numbers in an order of no increasing number."""
    numbers = [item['count'] for item in answer['values']]
    return numbers == sorted(numbers, reverse=True)


async def opened(r: Regions, dataset_id: int, field: str, view: Optional[dict]) -> dict:
    """The opened field's request, as getFieldFacet sends it."""
    body = {'filters': view, 'lightning_mode': True, 'fields': [field], 'stream': True}
    answer = await ok(await post(r, f'/api/datasets/{dataset_id}/field-stats', body), (field, view))
    lines = [json.loads(line) for line in answer.splitlines() if line.strip()]
    assert lines[-1] == {'done': True}, lines
    [entry] = [line for line in lines if line.get('name') == field]
    return entry


async def every_field(r: Regions, dataset_id: int, view: Optional[dict], lightning: bool) -> dict[str, dict]:
    """Lightning off (False), or the sidebar's own request (True)."""
    body = {'filters': view, 'lightning_mode': lightning}
    answer = await ok(await post(r, f'/api/datasets/{dataset_id}/field-stats', body), view)
    return {entry['name']: entry for entry in answer}


async def planted(r: Regions) -> None:
    await plant(r, OBJECTS, ROWS)
    await count_statistics(r, OBJECTS)


def list_mismatch(
    entry: dict,
    rows: dict[str, Row],
    kind: str,
    stages: list,
    field: str,
    *,
    computed: bool,
    empty_range: bool,
) -> Optional[str]:
    """What a list reader got wrong for a field against the reference, or None. `computed`: the answer must be the
    view's own (provenance exact). `empty_range`: a range where the field has no value for the view must be no range
    (14); False leaves such a range unasserted (a 1.6 pin)."""
    if field in TEXT_FIELDS:
        want = set(counts(rows, kind, stages, field))
        if entry.get('type') != 'enum' or set(entry.get('values') or []) != want:
            return f"{field}: {entry.get('type')} {sorted(entry.get('values') or [])} wanted {sorted(want)}"
    else:
        want_span = span(rows, kind, stages, field)
        got = (entry.get('min'), entry.get('max'))
        if want_span is None:
            if empty_range and (got != (None, None) or entry.get('provenance') in KEPT):
                return f"{field}: {got[0]}..{got[1]} ({entry.get('provenance')}) wanted no range"
            return None
        if got != want_span:
            return f"{field}: {got[0]}..{got[1]} wanted {want_span}"
    if computed and entry.get('provenance') != 'exact':
        return f"{field}: provenance {entry.get('provenance')}"
    return None


# --- Rule 1: a field's values, whichever kind has it, through every reader (7.1) ---


@pytest.mark.parametrize('kind', ['image', 'object'])
def test_rule_1_1_and_7_5_the_suggestions_offer_the_values_and_numbers_rule_1_3_reads(regions_world, kind):
    """Rules 1.1, 1.3, 7.3, 7.5 and 13: every view and every field, under each grid's kind, the own rule kept."""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted(r)
        wrong = []
        for name, stages in VIEWS.items():
            for field in FIELDS:
                answer = await suggest(r, OBJECTS, field, view_of(kind, stages))
                got, want = offered(answer), counts(ROWS, kind, stages, field)
                if got != want or answer['exhaustive'] is not True:
                    wrong.append(f'{name} / {field}: {got} {answer["exhaustive"]} wanted {want}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


@pytest.mark.parametrize('kind', ['image', 'object'])
def test_rule_7_1_the_opened_field_lists_the_values_and_range_rule_1_3_reads(regions_world, kind):
    """Rule 7.1 (and 14's empty range): the opened field's values and range, under each grid's kind."""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted(r)
        wrong = []
        for name, stages in VIEWS.items():
            for field in LISTED:
                entry = await opened(r, OBJECTS, field, view_of(kind, stages))
                problem = list_mismatch(entry, ROWS, kind, stages, field, computed=True, empty_range=True)
                if problem:
                    wrong.append(f'{name} / {problem}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


@pytest.mark.parametrize('kind', ['image', 'object'])
def test_rule_7_1_lightning_off_lists_the_values_and_range_rule_1_3_reads(regions_world, kind):
    """Rule 7.1 (and 14's empty range): lightning mode off, every field computed for the view."""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted(r)
        wrong = []
        for name, stages in VIEWS.items():
            entries = await every_field(r, OBJECTS, view_of(kind, stages), lightning=False)
            for field in LISTED:
                problem = list_mismatch(entries[field], ROWS, kind, stages, field, computed=True, empty_range=True)
                if problem:
                    wrong.append(f'{name} / {problem}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


@pytest.mark.parametrize('kind', ['image', 'object'])
def test_rule_7_1_the_sidebars_own_request_on_the_default_view_lists_the_values_and_range_rule_1_3_reads(
    regions_world,
    kind,
):
    """Rule 7.1 and 14: the sidebar's own request in lightning mode, on the default view (the kind alone), where it
    computes. (Today the weight range falls back to the dataset's, objects with no image included.)"""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted(r)
        entries = await every_field(r, OBJECTS, view_of(kind, []), lightning=True)
        wrong = [
            problem
            for field in LISTED
            if (problem := list_mismatch(entries[field], ROWS, kind, [], field, computed=True, empty_range=True))
        ]
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


def test_rule_1_6_pin_a_request_with_no_kind_lists_and_suggests_each_rows_own_values_as_today(regions_world):
    """Rule 1.6 (pin), its moves left out: with no kind, every reader reads each row on its own values: the
    suggestions for a field the view does not test (in an order of no increasing number), the opened field and
    lightning off for every view (the lists keep the own rule today). An empty range is point 16's, asked below."""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted(r)
        wrong = []
        for name, stages in VIEWS.items():
            view = view_of('none', stages)
            entries = await every_field(r, OBJECTS, view, lightning=False)
            for field in FIELDS:
                if field not in fields_tested_by(stages):
                    answer = await suggest(r, OBJECTS, field, view)
                    got, want = offered(answer), counts(ROWS, 'none', stages, field)
                    if got != want or answer['exhaustive'] is not True or not by_count(answer):
                        wrong.append(f'{name} / {field}: suggests {answer} wanted {want}')
                if field == 'occluded':
                    continue
                for reader, entry in (('opened', await opened(r, OBJECTS, field, view)), ('off', entries[field])):
                    problem = list_mismatch(entry, ROWS, 'none', stages, field, computed=False, empty_range=False)
                    if problem:
                        wrong.append(f'{name} / {reader} / {problem}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


def test_rule_14_a_request_with_no_kind_keeps_the_fields_own_rule_in_its_suggestions(regions_world):
    """Rules 13 and 14: a request with no kind (a script) keeps the asked field's own rule, as every reader does, so
    `label = car` suggests only car for label, and `make != ford` no ford. (Today the rule is taken out.)"""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted(r)
        wrong = []
        for name, stages in VIEWS.items():
            for field in sorted(fields_tested_by(stages)):
                answer = await suggest(r, OBJECTS, field, view_of('none', stages))
                got, want = offered(answer), counts(ROWS, 'none', stages, field)
                if got != want or answer['exhaustive'] is not True:
                    wrong.append(f'{name} / {field}: {got} {answer["exhaustive"]} wanted {want}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


async def no_range_where_the_view_has_no_value(
    r: Regions,
    dataset_id: int,
    rows: dict[str, Row],
    kind: str,
    views: dict[str, list[dict[str, Any]]],
    fields: tuple[str, ...],
) -> list[str]:
    """Rule 16: for every view and number field the reference reads no value for, what the opened field and lightning
    off answer other than no range (null `min` and `max`, not a dataset-wide provenance). At least one such pair is
    asked, so the check is never empty."""
    has_objects = any(row.is_object for row in rows.values())
    wrong, asked = [], 0
    for name, stages in views.items():
        empty = [field for field in fields if span(rows, kind, stages, field) is None]
        if not empty:
            continue
        view = view_of(kind, stages, has_objects)
        entries = await every_field(r, dataset_id, view, lightning=False)
        for field in empty:
            for reader, entry in (('opened', await opened(r, dataset_id, field, view)), ('off', entries[field])):
                asked += 1
                problem = list_mismatch(entry, rows, kind, stages, field, computed=False, empty_range=True)
                if problem:
                    wrong.append(f'{kind} / {name} / {reader} / {problem}')
    assert asked, (kind, 'no view without a value for a number field')
    return wrong


def test_rule_16_a_request_with_no_kind_answers_no_range_where_the_view_has_no_value(regions_world):
    """Rule 16: with no kind, on a dataset with objects, a number field the view has no value for (`colour = red`
    asked for weight, `label = car` asked for alt, ...) answers no range in the opened field and with lightning off:
    `min` and `max` null, not the dataset's kept range. (Today the dataset's range, called cached.)"""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted(r)
        wrong = await no_range_where_the_view_has_no_value(r, OBJECTS, ROWS, 'none', VIEWS, NUMBER_FIELDS)
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


# --- Rule 1.3's property, asked of the server and free of how it is built (6.1) ---


async def showing(
    r: Regions,
    dataset_id: int,
    rows: dict[str, Row],
    kind: str,
    stages: list,
    field: str,
    has_objects: bool = True,
) -> dict[str, Any]:
    """{text: value} of the field's values whose addition to the view as `f = v` shows a row (samples/query)."""
    found: dict[str, Any] = {}
    for value in candidates(rows, field):
        if await selected(r, view_of(kind, [*stages, leaf(field, '=', value)], has_objects), dataset_id):
            found[text(value)] = value
    return found


def property_mismatch(entry: dict, truth: dict[str, Any], field: str, empty_range: bool) -> Optional[str]:
    """A list or range against rule 1.3's property: every value it offers shows a row, a list that says it is complete
    misses none that would, and a range is the least and the greatest of the values that would."""
    if field in TEXT_FIELDS:
        got = set(entry.get('values') or [])
        if got - set(truth):
            return f'{field}: offers {sorted(got - set(truth))}, which show no row'
        if entry.get('values_complete') is True and set(truth) - got:
            return f'{field}: says it is complete and misses {sorted(set(truth) - got)}'
        return None
    numbers = [float(value) for value in truth.values()]
    want_range = (min(numbers), max(numbers)) if numbers else None
    got_range = (entry.get('min'), entry.get('max'))
    if want_range is None:
        return f'{field}: {got_range} wanted no range' if empty_range and got_range != (None, None) else None
    return None if got_range == want_range else f'{field}: {got_range} wanted {want_range}'


@pytest.mark.parametrize('kind', KINDS)
def test_rule_1_3_property_every_reader_offers_only_values_that_show_rows_and_a_complete_list_misses_none(
    regions_world,
    kind,
):
    """Rule 1.3's property (6.1, 7.4 as changed by 13): for every view, field and reader, each value offered, added to
    the view as sent as `f = v`, shows a row through samples/query; an answer that says its list is complete
    (`exhaustive`, `values_complete`) holds every value that would; a range spans exactly those values."""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted(r)
        wrong = []
        for name, stages in VIEWS.items():
            view = view_of(kind, stages)
            entries = await every_field(r, OBJECTS, view, lightning=False)
            for field in FIELDS:
                truth = await showing(r, OBJECTS, ROWS, kind, stages, field)
                answer = await suggest(r, OBJECTS, field, view)
                got = set(offered(answer))
                if got - set(truth) or (answer['exhaustive'] and set(truth) - got):
                    wrong.append(
                        f'{name} / suggestions / {field}: {sorted(got)} {answer["exhaustive"]} vs {sorted(truth)}',
                    )
                if field == 'occluded':
                    continue
                for reader, entry in (('opened', await opened(r, OBJECTS, field, view)), ('off', entries[field])):
                    problem = property_mismatch(entry, truth, field, empty_range=True)
                    if problem:
                        wrong.append(f'{name} / {reader} / {problem}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


@pytest.mark.parametrize('kind', KINDS)
def test_the_reference_agrees_with_the_grids_a_value_is_read_iff_adding_it_shows_a_row(regions_world, kind):
    """The reference (rule 1.3 read over the planted rows) against the server's own grids: for every view and field,
    the values it reads are exactly those whose addition as `f = v` shows a row through samples/query."""

    async def scenario(r):
        await planted(r)
        wrong = []
        for name, stages in VIEWS.items():
            for field in FIELDS:
                truth = set(await showing(r, OBJECTS, ROWS, kind, stages, field))
                reference = counts(ROWS, kind, stages, field)
                if truth != set(reference):
                    wrong.append(f'{name} / {field}: the grids {sorted(truth)}, the reference {reference}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


def test_rules_7_3_and_1_3_by_hand_objects_with_no_image_a_field_both_kinds_have_and_one_object_for_the_pair(
    regions_world,
):
    """Written out by hand: under the images' kind an object with no image is no one's partner (van, kia, tesla,
    cam-z, 999 and 5 are not listed) and under the objects' kind it is (7.3); a field both kinds have lists both
    (7.1); `label = car` asked for make and occluded offers the cars' values, not the truck's beside car-1 (1.3);
    `NOT (label = car)` binds no object (1.3); with no kind the own rule is kept (14)."""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted(r)
        wrong = []
        for (kind, name, field), want in BY_HAND.items():
            answer = await suggest(r, OBJECTS, field, view_of(kind, VIEWS[name]))
            if offered(answer) != want or answer['exhaustive'] is not True:
                wrong.append(f'{kind} / {name} / {field}: {answer} wanted {want}')
        for kind, field, want_range in (
            ('image', 'weight', (70.0, 300.0)),
            ('object', 'weight', (5.0, 999.0)),
            ('image', 'level', (1.0, 9.0)),
            ('object', 'level', (1.0, 99.0)),
        ):
            entry = await opened(r, OBJECTS, field, view_of(kind, []))
            if (entry.get('min'), entry.get('max')) != want_range:
                wrong.append(f'{kind} / range of {field}: {entry.get("min")}..{entry.get("max")} wanted {want_range}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


# --- The exact-value probe (2.4, 7.1) ---

PROBE_ROWS: dict[str, Row] = {
    **{f'p-img-{n}': image({'colour': 'red'}) for n in 'abcdef'},
    'p-car-a': child('p-img-a', {'label': 'car'}),
    'p-car-b': child('p-img-b', {'label': 'car'}),
    'p-car-loose': child(None, {'label': 'car'}),
    **{f'p-carton-{n}': child(f'p-img-{n}', {'label': 'carton'}) for n in 'cdef'},
}


@pytest.mark.parametrize(('kind', 'cars'), [('image', 2), ('object', 3)])
def test_rule_2_4_an_exact_value_typed_in_full_is_found_first_on_an_indexed_field_under_each_kind(
    regions_world,
    kind,
    cars,
):
    """Rules 2.4 and 7.1: on an indexed `label`, `car` typed with a limit of one is found first by the probe, with the
    number of cars the reading reads (two with an image under the images' kind, the loose one too under the objects'),
    then the counted reading's top value, carton (4). (Today, under the images' kind, nothing.)"""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await plant(r, PROBE, PROBE_ROWS)
        await index_field(r, PROBE, 'label')
        answer = await suggest(r, PROBE, 'label', view_of(kind, []), q='car', limit=1)
        assert answer == {
            'values': [{'value': 'car', 'count': cars}, {'value': 'carton', 'count': 4}],
            'exhaustive': True,
        }

    regions_world(scenario)


# --- A dataset without objects (1.6, with 13's and 14's moves) ---

PLAIN_ROWS = {name: row for name, row in ROWS.items() if not row.is_object}
PLAIN_TEXT, PLAIN_NUMBERS = ('colour', 'source'), ('alt', 'level')
PLAIN_VIEWS: dict[str, list[dict[str, Any]]] = {
    'nothing': [],
    'colour = red': [leaf('colour', '=', 'red')],
    'colour = green': [leaf('colour', '=', 'green')],  # img-5, which has no level: an empty range
    'NOT (colour = red)': [negated(leaf('colour', '=', 'red'))],
    'colour != red': [leaf('colour', '!=', 'red')],
    'colour in (red, blue)': [leaf('colour', 'in', ['red', 'blue'])],
    'source = drone': [leaf('source', '=', 'drone')],
    'alt > 25': [leaf('alt', '>', 25)],
    'colour = red, source = drone': [leaf('colour', '=', 'red'), leaf('source', '=', 'drone')],
}


async def planted_plain(r: Regions) -> None:
    await plant(r, PLAIN, PLAIN_ROWS)
    await count_statistics(r, PLAIN)


def test_rule_1_6_pin_a_dataset_without_objects_lists_and_suggests_as_today(regions_world):
    """Rule 1.6 (pin), its four moves left out (15, 16): on a dataset without objects, under the images' kind (the
    page's plain form) and with no kind, the suggestions for a field the view does not test (values, numbers, an order
    of no increasing number), the opened field and lightning off for every view, and the sidebar's own request on the
    default view, each row read on its own values. An empty range is point 16's, asked below."""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted_plain(r)
        wrong = []
        for kind in ('image', 'none'):
            for name, stages in PLAIN_VIEWS.items():
                view = view_of(kind, stages, has_objects=False)
                entries = await every_field(r, PLAIN, view, lightning=False)
                for field in (*PLAIN_TEXT, *PLAIN_NUMBERS):
                    if field not in fields_tested_by(stages):
                        answer = await suggest(r, PLAIN, field, view)
                        want = counts(PLAIN_ROWS, kind, stages, field)
                        if offered(answer) != want or answer['exhaustive'] is not True or not by_count(answer):
                            wrong.append(f'{kind} / {name} / {field}: suggests {answer} wanted {want}')
                    for reader, entry in (('opened', await opened(r, PLAIN, field, view)), ('off', entries[field])):
                        problem = list_mismatch(
                            entry,
                            PLAIN_ROWS,
                            kind,
                            stages,
                            field,
                            computed=False,
                            empty_range=False,
                        )
                        if problem:
                            wrong.append(f'{kind} / {name} / {reader} / {problem}')
        entries = await every_field(r, PLAIN, view_of('image', [], has_objects=False), lightning=True)
        for field in (*PLAIN_TEXT, *PLAIN_NUMBERS):
            problem = list_mismatch(entries[field], PLAIN_ROWS, 'image', [], field, computed=False, empty_range=False)
            if problem:
                wrong.append(f'default view / sidebar / {problem}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


def test_rules_13_and_15_without_objects_the_picker_of_a_field_with_an_applied_rule_offers_the_views_values(
    regions_world,
):
    """Rules 13, 14 and 15 (two of 1.6's four moves): on a dataset without objects the suggestions for a field the view
    already tests offer the values the view has, under the images' kind (`colour in (red)` applied offers red, not
    blue) and in a request with no kind that carries a filter. (Today every value of the field.)"""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted_plain(r)
        wrong = []
        for kind in ('image', 'none'):
            for name, stages in PLAIN_VIEWS.items():
                for field in sorted(fields_tested_by(stages)):
                    answer = await suggest(r, PLAIN, field, view_of(kind, stages, has_objects=False))
                    want = counts(PLAIN_ROWS, kind, stages, field)
                    if offered(answer) != want or answer['exhaustive'] is not True:
                        wrong.append(f'{kind} / {name} / {field}: {offered(answer)} wanted {want}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


def test_rule_16_on_a_dataset_without_objects_a_range_the_view_has_no_value_for_is_no_range(regions_world):
    """Rule 16 (1.6's fourth move): on a dataset without objects, under the images' kind and with no kind, a number
    field the view has no value for (`colour = green` asked for level) answers no range in the opened field and with
    lightning off: `min` and `max` null, not the dataset's kept range. (Today the dataset's range, called cached.)"""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted_plain(r)
        wrong: list[str] = []
        for kind in ('image', 'none'):
            wrong += await no_range_where_the_view_has_no_value(r, PLAIN, PLAIN_ROWS, kind, PLAIN_VIEWS, PLAIN_NUMBERS)
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


# --- Rule 2: a field too varied to count in time (7.7, 7.8, 7.11, 13, 14) ---

NEEDLES = {(0, 3), (2, 7), (3, 11)}  # the urls a rare term is in (one on a red image, two on blue ones)


def url_of(image_number: int, row: int) -> str:
    word = 'needle' if (image_number, row) in NEEDLES else 'photo'
    return f'https://farm{image_number}.staticflickr.com/{row:02d}/{word}_{image_number}{row:02d}.jpg'


# Equal numbers in an order that differs between "C" (upper case, then `_`, then lower case) and a language's.
GRADES = {'Omega': 3, 'Beta': 2, 'Charlie': 2, 'Zeta': 2, '_mid': 2, 'alpha': 2, 'delta': 2, 'zz': 1}
GRADES_COUNTED = [{'value': value, 'count': n} for value, n in sorted(GRADES.items(), key=lambda kv: (-kv[1], kv[0]))]
LOOSE_URL = 'https://loose.staticflickr.com/00/needle_loose.jpg'


def varied_rows() -> dict[str, Row]:
    """Four images (two red, two blue), fifteen objects on each with a url of its own (every one holds `flickr`, three
    hold `needle`), a loose object with a url, and sixteen objects with a `grade` (GRADES)."""
    rows: dict[str, Row] = {f'v-img-{i}': image({'colour': 'red' if i < 2 else 'blue'}) for i in range(4)}
    for i in range(4):
        for row in range(15):
            rows[f'v-url-{i}-{row:02d}'] = child(f'v-img-{i}', {'url': url_of(i, row)})
    rows['v-loose'] = child(None, {'url': LOOSE_URL})
    number = 0
    for value, n in GRADES.items():
        for _ in range(n):
            rows[f'v-grade-{number:02d}'] = child(f'v-img-{number % 4}', {'grade': value})
            number += 1
    return rows


VARIED_ROWS = varied_rows()


async def planted_varied(r: Regions) -> None:
    await plant(r, VARIED, VARIED_ROWS)
    await count_statistics(r, VARIED)


def uncounted(monkeypatch: Any) -> None:
    """The counted reading out of time at once (its limit at zero), the uncounted one with time to spare and its row
    stop small (7.11)."""
    unhurried(monkeypatch)
    monkeypatch.setattr(datasets, COUNTED_LIMIT, 0.0)
    monkeypatch.setattr(datasets, UNCOUNTED_ROWS, ROW_STOP, raising=False)


async def every_value_shows_a_row(r: Regions, kind: str, stages: list, field: str, answer: dict) -> list[str]:
    """The values offered whose addition as `f = v` shows no row (rule 1.7's twin)."""
    return [
        item['value']
        for item in answer['values']
        if not await selected(r, view_of(kind, [*stages, leaf(field, '=', item['value'])]), VARIED)
    ]


def some_not_all(answer: dict, limit: int = 100) -> Optional[str]:
    """The form of an answer that could not list every value (2.2, 7.7): not blank, not complete, no numbers, at most
    the limit, in the order of the value in "C" (code points; the values are ASCII)."""
    values = [item['value'] for item in answer['values']]
    if not values or answer['exhaustive'] is not False or any(item['count'] is not None for item in answer['values']):
        return f'not some of the values without numbers: {answer}'
    if len(values) > limit or values != sorted(values):
        return f'more than {limit} values, or not in the order of the value in C: {values}'
    return None


@pytest.mark.parametrize('kind', ['image', 'object'])
def test_rules_2_2_and_7_7_a_field_too_varied_to_count_in_time_answers_some_of_its_values_without_numbers(
    regions_world,
    kind,
):
    """Rules 2.1, 2.2, 7.7 and 1.7's twin: with the counted limit at zero and the row stop at twenty (sixty urls under
    the images' kind, sixty-one under the objects'), nothing typed and a term every value holds both answer some of
    the values: not blank, not complete, no numbers, at most the limit, in C order, every one showing a row (under a
    filter on an image field too); a typed term narrows them. (Today the cached list, nearly always empty.)"""

    async def scenario(r):
        uncounted(r.monkeypatch)
        await planted_varied(r)
        wrong = []
        for name, stages, q, limit in (
            ('nothing', [], '', 100),
            ('nothing', [], 'flickr', 100),
            ('nothing', [], 'FLICKR', 100),
            ('nothing', [], '', 5),
            ('colour = red', [leaf('colour', '=', 'red')], '', 100),
        ):
            answer = await suggest(r, VARIED, 'url', view_of(kind, stages), q=q, limit=limit)
            problem = some_not_all(answer, limit)
            if problem:
                wrong.append(f'{name} / {q!r} / {limit}: {problem}')
            if any(q.lower() not in item['value'].lower() for item in answer['values']):
                wrong.append(f'{name} / {q!r}: a value without the term: {answer}')
            hidden = await every_value_shows_a_row(r, kind, stages, 'url', answer)
            if hidden:
                wrong.append(f'{name} / {q!r}: offered, and shows no row: {hidden}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


def test_rules_7_7_and_13_an_uncounted_reading_that_reached_the_end_answers_what_the_counted_one_would(regions_world):
    """Rules 7.7 and 13 (point 7's first bullet): an uncounted reading that read every row before its stop is complete
    and answers exactly the counted answer, numbers and order (the top hundred by number, equal numbers in C order):
    a term three urls hold (the loose one not, under the images' kind), the sixteen grades, and a term no value holds
    (nothing, complete). (Today the cached list, not complete.)"""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted_varied(r)
        view = view_of('image', [])
        needles = sorted(url_of(i, row) for i, row in NEEDLES)
        cases = [
            ('url', 'needle', {'values': [{'value': value, 'count': 1} for value in needles], 'exhaustive': True}),
            ('grade', '', {'values': GRADES_COUNTED, 'exhaustive': True}),
            ('url', 'zzz', {'values': [], 'exhaustive': True}),
        ]
        counted = [await suggest(r, VARIED, field, view, q=q) for field, q, _ in cases]
        uncounted(r.monkeypatch)
        wrong = []
        for (field, q, want), in_time in zip(cases, counted):
            answer = await suggest(r, VARIED, field, view, q=q)
            if answer != want or in_time != want:
                wrong.append(f'{field} / {q!r}: uncounted {answer}, counted {in_time}, wanted {want}')
        assert not wrong, '\n'.join(wrong)

    regions_world(scenario)


def test_rule_2_2_pin_with_both_limits_at_zero_the_kept_list_answers_without_numbers_and_not_complete(regions_world):
    """Rules 2.2 and 14 (pin): when the uncounted reading runs out of time too, the kept list is the last resort, as
    today: asked only for its form, no numbers and not complete, a typed term in every value."""

    async def scenario(r):
        uncounted(r.monkeypatch)
        r.monkeypatch.setattr(datasets, UNCOUNTED_LIMIT, 0.0, raising=False)
        await planted_varied(r)
        for kind in ('image', 'object'):
            for q in ('', 'a'):
                answer = await suggest(r, VARIED, 'grade', view_of(kind, []), q=q)
                assert answer['exhaustive'] is False, (kind, q, answer)
                assert all(item['count'] is None for item in answer['values']), (kind, q, answer)
                assert all(q.lower() in item['value'].lower() for item in answer['values']), (kind, q, answer)

    regions_world(scenario)


@pytest.mark.parametrize('kind', KINDS)
def test_rule_7_8_equal_numbers_come_in_the_order_of_the_value_in_c(regions_world, kind):
    """Rule 7.8: the counted reading orders equal numbers by the value in "C" (Beta, Charlie, Zeta, _mid, alpha,
    delta), under every kind. (Today the order among equal numbers is not defined.)"""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted_varied(r)
        answer = await suggest(r, VARIED, 'grade', view_of(kind, []))
        assert answer == {'values': GRADES_COUNTED, 'exhaustive': True}

    regions_world(scenario)


@pytest.mark.parametrize('kind', ['object', 'none'])
def test_rule_2_3_pin_a_field_counted_in_time_answers_the_same_values_and_numbers_as_today(regions_world, kind):
    """Rule 2.3 (pin): a field whose values are counted in time answers as today: every url (the loose one too, under
    the objects' kind and with no kind) once, and the grades with their numbers, in an order of no increasing number
    (the order among equal numbers is 7.8's)."""

    async def scenario(r):
        unhurried(r.monkeypatch)
        await planted_varied(r)
        urls = await suggest(r, VARIED, 'url', view_of(kind, []))
        assert offered(urls) == {**{url_of(i, row): 1 for i in range(4) for row in range(15)}, LOOSE_URL: 1}
        assert urls['exhaustive'] is True
        grades = await suggest(r, VARIED, 'grade', view_of(kind, []))
        assert offered(grades) == GRADES and grades['exhaustive'] is True and by_count(grades), grades

    regions_world(scenario)


def test_the_limits_these_tests_set_are_the_routes_own():
    """Rule 14: the names and values of the limits these tests set (a limit set under a name the route does not read
    would leave the tests above asking nothing)."""
    assert getattr(datasets, COUNTED_LIMIT, None) == 2.0
    assert getattr(datasets, UNCOUNTED_LIMIT, None) == 1.0
    assert getattr(datasets, UNCOUNTED_ROWS, None) == 1000
