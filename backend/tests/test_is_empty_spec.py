"""`is null` means no value, and an empty list has a test of its own (contract g9), tested where a user or a script
meets it: the HTTP API signed in with real API tokens (the region tests' world: a database of its own per test), the
page's wrapping on a dataset with objects, the SDK driven as a script drives it and the assistant's tools; the page
itself in frontend/e2e/is-empty.spec.ts. Written from the contract, not from the code.

The rule (section 1). A row has a value for a field when the value is there and isn't a JSON null, and an empty list
written in a field is a value: the list was looked at and holds nothing. `f is null` shows the rows with no value for
`f`, `f is not null` the rows with one, for every field. A list of labels (`detections.label`) has a value when its
list is empty or one of its elements has a label, so `detections.label is null` shows the rows with no list and the
rows whose elements have no label at all, never an empty list. `f is empty` shows the rows whose `f` is a list that
holds nothing, and `f is not empty` is `NOT (f is empty)`, read as every NOT of a comparison: the rows that have a
value for `f` which isn't an empty list. The lists every sample holds (`tags` and `@local.tags`) don't change: an empty
one is no value, `is null` and `is not null` answer as main does, `is empty` answers as `is null` and `is not empty` as
`is not null` (rule 1.6). A list field kept in a column of its own was among them until contract g10 let it hold
"never set": its tests here are rewritten for g10, which reads it as a list in a field is.

The fixtures (section 7.1). EMPTY is a dataset without objects of eight rows, `e0` to `e7`, each with a `plot` and a
`rank`. It holds a plain list `inferred_tags` (the owner's example of a list someone added) and a list of labels
`detections`, each in every state (no key, a JSON null, `[]`, a list with elements, and for `detections` a list whose
elements have no label, alone and beside a labelled one), their states on different rows; a text field `colour` (a
value, a JSON null, no key); tags (four rows untagged); `note`, text on most rows and an empty list on one (a key not
known as a list); a text field named `empty`; a location (three rows have none), a point on the `clip` map (three rows
have none) and the concept `scene` (two rows unscored). LOCAL is a mirror whose samples hold `@local.tags` written
through the API: a tag, two, written and then emptied, and never written. COLUMN has a list field kept in a column of
its own, made as the app makes it (the one-rule spec's PESTS). OBJECTS (section 7.4) is a dataset with objects with a
list on the images (`inferred_tags`, and a list of labels `detections`) and a list on the objects (`marks`).

Every expected set on EMPTY is computed by the one-rule spec's reference reading (test_one_rule_everywhere_spec.RULE,
which reads `is null`, `is empty` and `is not empty` by this contract's words) and checked against sets written out by
hand first (`test_the_reference_reads_the_contracts_words_as_the_hand_does`). OBJECTS has a reference of its own,
`wrapped`, checked the same way.

Where the contract leaves a point open, these tests read it this way, and stay away from what it leaves open:
- `is empty` needs a value, as a comparison does (rule 1.4: its NOT is read as every NOT of a comparison): a NOT
  around it and a comparison of another field needs both fields. Through the page's wrapping (rule 1.8) it is met as a
  comparison of the same field is met today (by the row or its partner: on the images' grid by the image's own list
  or by one object's), `is not empty` and a NOT around `is empty` are read for the row as a whole as every NOT is, and
  `is null` for the row as a whole, as today.
- A value that is no list (text where a list is expected) is a value, so `is not empty` shows it
  (backend/tests/one_rule_filter_cases.json holds one).
- `exists` and `not exists` of a plain list and of the list field itself (`detections exists`) ask whether the key is
  there, as main does (rule 1.7, made exact 2026-11-26: an empty list exists; a JSON null's key is there too). Of a
  path through a list (`detections.label`), `exists` shows the rows that have a value for it, the rows of `is not
  null`, and `not exists` the rows of `is null`, on one row and through the page's wrapping (there `exists` is a
  showing test and `not exists` is read for the row as a whole, as `is not null` and `is null` are).
- The refusals of rule 1.5 are asked of the built-in fields (`id`, `filepath`, `media_type`, `created_at`,
  `last_modified_at`), `location`, a concept and the path through a list of labels, in the text and (but the concept)
  as a filter; a metadata field declared a date is not asked. Their message says what `is empty` can be asked of with
  the word "list", and for the path it names the list itself, `detections is empty` (or `is not empty`).
- The JSON form of each new test is `{"field": f, "operator": "is_empty" | "is_not_empty", "value": null}`, as
  `is_null`'s is. The SDK's conditions are taken to be named in the style of the ones it has: `F(f).is_empty()` and
  `F(f).is_not_empty()` beside `F(f).exists()`, and `f__is_empty=True/False` and `f__is_not_empty=True/False` beside
  `f__is_null=True/False` (False the other test). Keywords aren't case sensitive, `IS NOT EMPTY` among them.
- What the assistant (and the MCP server, which gives the same text as its instructions) is told is the text a model
  is given before its tools, with every tool's description and argument words.
- "Every reader" (2.2, the list of g7 2.1) is asked as test_one_rule_everywhere_spec asks it: samples/query and its
  count, an export's pages, aggregate, the histogram, field-stats and autocomplete, map_points and map_extent, the
  embeddings map, a selection token and in_selection, /api/search, a version's samples/query and aggregate, a saved
  view read back and opened, the SDK (the text, the filter as a Condition, and load_view), the assistant (make_view,
  get_samples and open_saved_view) and a bulk tag.
- Each test of what changes asks something main answers first, as a control, so a failure on main is the rule's. The
  pins (named so) pass on main and after.
"""

import asyncio
import json
import re
from typing import Any, Optional
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fridge import Condition, Dataset, F

from backend import assistant
from backend.routers import datasets, embeddings
from backend.tests import test_one_rule_everywhere_spec as one_rule
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_assistant_core_spec import open_door
from backend.tests.test_assistant_views_spec import result, view_ids
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_objects_filter_reading_spec import child, image, page_filter
from backend.tests.test_objects_filter_reading_spec import plant as plant_objects
from backend.tests.test_objects_filter_reading_spec import shown as grid_shown
from backend.tests.test_one_rule_everywhere_spec import (
    PESTS,
    RULE,
    UNLABELLED,
    Planted,
    Row,
    both,
    boxes,
    either,
    leaf,
    negated,
    ok,
    parsed,
    saved_view,
    shown,
    stage_of,
    version_of,
)
from backend.tests.test_protected_metadata_keys_spec import sdk
from backend.tests.test_regions_spec import ADMIN_USER, Regions, nodes, parse, post, refusal, selected, text_of

database = one_rule.database
regions_world = one_rule.regions_world
pytestmark = one_rule.pytestmark

EMPTY, LOCAL, COLUMN, OBJECTS = 80, 81, 82, 83
CUT = 0.5
QUERY = [1.0, 0.0, 0.0, 0.0]


# --- The planted rows ---


def row(number: int, values: dict[str, Any], **more: Any) -> Row:
    return Row({'plot': f'p{number}', 'rank': number, **values}, **more)


ROWS: dict[str, Row] = {
    'e0': row(
        0,
        {'inferred_tags': ['weed'], 'detections': boxes('car'), 'colour': 'red', 'note': 'a', 'empty': 'x'},
        tags=('seen',),
        score=0.75,
        location=(1.0, 1.0),
        point=(1.0, 1.0),
    ),
    'e1': row(
        1,
        {'inferred_tags': [], 'detections': [], 'colour': 'blue', 'note': 'b', 'empty': 'y'},
        score=0.25,
        location=(2.0, 2.0),
        point=(2.0, 2.0),
    ),
    'e2': row(
        2,
        {'inferred_tags': None, 'detections': [UNLABELLED], 'colour': None, 'note': []},
        tags=('seen',),
        score=0.625,
        point=(3.0, 3.0),
    ),
    'e3': row(3, {}, score=0.125, location=(3.0, 3.0)),
    'e4': row(
        4,
        {'inferred_tags': ['rock', 'weed'], 'detections': None, 'colour': 'red', 'note': 'c', 'empty': None},
        tags=('done',),
        score=0.875,
        location=(4.0, 4.0),
        point=(4.0, 4.0),
    ),
    'e5': row(5, {'inferred_tags': [], 'detections': [*boxes('truck'), UNLABELLED], 'colour': 'green'}, score=0.375),
    'e6': row(6, {'detections': [], 'colour': 'blue', 'note': None}, tags=('seen', 'done'), location=(6.0, 6.0)),
    'e7': row(7, {'inferred_tags': None, 'detections': boxes('car', 'truck')}, point=(7.0, 7.0)),
}
EVERY = set(ROWS)
LOCATED = {name for name, item in ROWS.items() if item.location is not None}
ON_THE_MAP = {name for name, item in ROWS.items() if item.point is not None}

# What each test shows on EMPTY, written out by hand from the rows above.
INFERRED = {
    'is_null': {'e2', 'e3', 'e6', 'e7'},  # a JSON null, no key
    'is_not_null': {'e0', 'e1', 'e4', 'e5'},  # the list is there, empty or not
    'is_empty': {'e1', 'e5'},
    'is_not_empty': {'e0', 'e4'},
    'exists': {'e0', 'e1', 'e2', 'e4', 'e5', 'e7'},  # the key is there
    'not_exists': {'e3', 'e6'},
}
DETECTIONS = {
    'is_null': {'e3', 'e4'},
    'is_not_null': {'e0', 'e1', 'e2', 'e5', 'e6', 'e7'},
    'is_empty': {'e1', 'e6'},
    'is_not_empty': {'e0', 'e2', 'e5', 'e7'},  # e2's detections have no label: a list with elements, not empty
    'exists': EVERY - {'e3'},
    'not_exists': {'e3'},
}
# The list of labels: no value is no list (e3, e4) or elements none of which has a label (e2); e1's and e6's empty
# lists are values. Main showed them for `is null` and not for `is not null`.
LABEL = {'is_null': {'e2', 'e3', 'e4'}, 'is_not_null': {'e0', 'e1', 'e5', 'e6', 'e7'}}
# Of the path, `exists` and `not exists` ask for a value too (rule 1.7, made exact 2026-11-26). Main's `exists` showed
# the rows with a labelled detection (not e1's nor e6's empty list), its `not exists` those with an unlabelled one (e2,
# e5), not the rows with no list.
LABEL['exists'], LABEL['not_exists'] = LABEL['is_not_null'], LABEL['is_null']
COLOUR = {'is_null': {'e2', 'e3', 'e7'}, 'is_not_null': {'e0', 'e1', 'e4', 'e5', 'e6'}, 'is_empty': set()}
COLOUR['is_not_empty'] = COLOUR['is_not_null']
NOTE = {'is_null': {'e3', 'e5', 'e6', 'e7'}, 'is_empty': {'e2'}, 'is_not_empty': {'e0', 'e1', 'e4'}}
UNTAGGED = {'e1', 'e3', 'e5', 'e7'}
# NOT (detections is empty OR colour = "red"): a list, not empty, and a colour, not red.
NOT_EMPTY_NOR_RED = {'e5'}

LISTS = {'inferred_tags': INFERRED, 'detections': DETECTIONS}


async def plant(r: Regions) -> Planted:
    """ROWS as a dataset without objects, with a vector each, their points on the `clip` map and the concept `scene`;
    the sidebar read once, as the page reads it."""
    await r.dataset(EMPTY)
    samples = []
    for name, item in ROWS.items():
        body: dict[str, Any] = {'filepath': f'{name}.jpg', 'metadata': dict(item.values), 'tags': list(item.tags)}
        if item.location is not None:
            body['location'] = {'type': 'Point', 'coordinates': list(item.location)}
        samples.append(body)
    await datasets.add_samples(EMPTY, {'samples': samples}, user=ADMIN_USER)
    stored = await r.database.fetch('SELECT id, filepath, metadata, tags FROM samples WHERE dataset_id = $1', EMPTY)
    ids = {item['filepath'][: -len('.jpg')]: item['id'] for item in stored}
    assert set(ids) == EVERY, sorted(ids)
    for item in stored:  # a JSON null kept as one, an empty list as one, a key left out left out
        name = item['filepath'][: -len('.jpg')]
        metadata = item['metadata'] if isinstance(item['metadata'], dict) else json.loads(item['metadata'])
        assert metadata == ROWS[name].values, (name, metadata)
        assert tuple(item['tags'] or ()) == ROWS[name].tags, (name, item['tags'])
    vectors = [
        {'sample_id': ids[name], 'vector': [1.0, number / 100, 0.0, 0.0], 'field_name': 'clip'}
        for number, name in enumerate(ROWS)
    ]
    assert (await post(r, f'/api/datasets/{EMPTY}/embeddings', vectors)).is_success
    r.monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    points = [
        {'sample_id': ids[name], 'x': item.point[0], 'y': item.point[1]}
        for name, item in ROWS.items()
        if item.point is not None
    ]
    response = await r.call(
        ADMIN,
        'POST',
        f'/api/datasets/{EMPTY}/embeddings/coordinates',
        params={'field_name': 'clip'},
        json=points,
    )
    assert response.status_code == 200, response.text
    concept = await r.database.fetchval(
        'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
        "VALUES ($1, 'scene', 'clip', 'seed', '[1,0,0,0]', $2) RETURNING id",
        EMPTY,
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
    sidebar = await r.call(ADMIN, 'GET', f'/api/datasets/{EMPTY}/field-stats')
    assert sidebar.status_code == 200, sidebar.text
    return Planted(EMPTY, ids, concept)


async def assert_rows(r: Regions, p: Planted, cases: list[tuple[Any, set[str]]]) -> None:
    """Each form (text, read by the parse route, or a filter node) shows the rows the contract's reading shows, which
    the test also writes out by hand, through samples/query and its count."""
    for form, by_hand in cases:
        node = await parsed(r, form, p.dataset_id) if isinstance(form, str) else form
        assert RULE.rows(node, ROWS) == by_hand, ("the test's own expectation", form, sorted(RULE.rows(node, ROWS)))
        found = await shown(r, p, node)
        assert found == by_hand, (form, 'shown', sorted(found), 'expected', sorted(by_hand))


# --- The reference, checked first ---


def test_the_reference_reads_the_contracts_words_as_the_hand_does():
    for field, table in (*LISTS.items(), ('detections.label', LABEL), ('colour', COLOUR), ('note', NOTE)):
        for operator, names in table.items():
            assert RULE.rows(leaf(field, operator), ROWS) == names, (field, operator)
        if 'is_empty' in table:
            assert RULE.rows(negated(leaf(field, 'is_empty')), ROWS) == table['is_not_empty'], field
    assert RULE.rows(leaf('tags', 'is_null'), ROWS) == RULE.rows(leaf('tags', 'is_empty'), ROWS) == UNTAGGED
    assert RULE.rows(leaf('tags', 'is_not_null'), ROWS) == RULE.rows(leaf('tags', 'is_not_empty'), ROWS)
    assert RULE.rows(leaf('tags', 'is_not_empty'), ROWS) == EVERY - UNTAGGED
    around = negated(either(leaf('detections', 'is_empty'), leaf('colour', '=', 'red')))
    assert RULE.rows(around, ROWS) == NOT_EMPTY_NOR_RED


# --- Section 1, rule by rule, on a dataset without objects (7.1) ---


def test_rule_1_1_pin_is_null_of_a_list_in_a_field_shows_no_list_and_is_not_null_an_empty_list_too(regions_world):
    """A pin: `is null` asked of a list in a field (a plain list, and the list of labels asked as the field) shows the
    rows with no list, a JSON null or no key, and `is not null` the rows whose list is there, empty or not. Main
    answers so already."""

    async def scenario(r):
        p = await plant(r)
        cases: list[tuple[Any, set[str]]] = []
        for field, table in LISTS.items():
            for operator, words in (('is_null', 'is null'), ('is_not_null', 'is not null')):
                cases += [(f'{field} {words}', table[operator]), (leaf(field, operator), table[operator])]
        await assert_rows(r, p, cases)

    regions_world(scenario)


def test_rule_1_2_a_list_of_labels_is_null_shows_no_list_or_no_label_and_is_not_null_an_empty_list_too(regions_world):
    """`detections.label is null` shows the rows with no list (e3 no key, e4 a JSON null) and the row whose detections
    have no label (e2); not e1's nor e6's empty list, which `is not null` shows, with the rows that have a label (e5's
    beside an unlabelled one). Main showed the empty lists for `is null` and not for `is not null`."""

    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers them: the show, and `is null` asked of the field.
        await assert_rows(r, p, [('detections.label = "car"', {'e0', 'e7'}), ('detections is null', {'e3', 'e4'})])
        await assert_rows(
            r,
            p,
            [
                ('detections.label is null', LABEL['is_null']),
                ('detections.label is not null', LABEL['is_not_null']),
                (leaf('detections.label', 'is_null'), LABEL['is_null']),
                (leaf('detections.label', 'is_not_null'), LABEL['is_not_null']),
                ('NOT (detections.label is null)', LABEL['is_not_null']),
                ('detections.label is null OR detections.label = "car"', LABEL['is_null'] | {'e0', 'e7'}),
            ],
        )

    regions_world(scenario)


def test_rule_1_4_is_empty_shows_the_rows_whose_list_holds_nothing(regions_world):
    """`f is empty`, in the text and as its filter, on the plain list and on the list of labels asked as the field:
    the rows whose list is `[]`; not a JSON null, no key, nor a list whose elements have no label."""

    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers it: the rows whose list is there.
        await assert_rows(r, p, [('inferred_tags is not null', INFERRED['is_not_null'])])
        cases: list[tuple[Any, set[str]]] = []
        for field, table in LISTS.items():
            cases += [(f'{field} is empty', table['is_empty']), (leaf(field, 'is_empty'), table['is_empty'])]
        cases += [
            ('inferred_tags IS EMPTY', INFERRED['is_empty']),
            ('inferred_tags is empty AND detections is empty', {'e1'}),
            ('inferred_tags is empty OR detections is empty', {'e1', 'e5', 'e6'}),
            ('inferred_tags is empty AND colour = "green"', {'e5'}),
        ]
        await assert_rows(r, p, cases)

    regions_world(scenario)


def test_rule_1_4_is_not_empty_is_the_not_of_is_empty_and_needs_a_value(regions_world):
    """`f is not empty` is `NOT (f is empty)`, read as every NOT of a comparison: the rows that have a value for `f`
    which isn't an empty list. Neither shows a row with no value. A NOT around `is empty` and a comparison of another
    field needs both fields (e2 has detections but a null colour, e4 a colour but null detections)."""

    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers them: the rows whose list is there, and a NOT of a comparison beside it.
        await assert_rows(
            r,
            p,
            [('detections is not null', DETECTIONS['is_not_null']), ('NOT (colour = "red")', {'e1', 'e5', 'e6'})],
        )
        cases: list[tuple[Any, set[str]]] = []
        for field, table in LISTS.items():
            wanted = table['is_not_empty']
            cases += [
                (f'{field} is not empty', wanted),
                (leaf(field, 'is_not_empty'), wanted),
                (f'NOT ({field} is empty)', wanted),
                (negated(leaf(field, 'is_empty')), wanted),
            ]
        cases += [
            ('inferred_tags IS NOT EMPTY', INFERRED['is_not_empty']),
            ('NOT (detections is empty OR colour = "red")', NOT_EMPTY_NOR_RED),
            (negated(either(leaf('detections', 'is_empty'), leaf('colour', '=', 'red'))), NOT_EMPTY_NOR_RED),
            ('detections is not empty AND inferred_tags is null', {'e2', 'e7'}),
        ]
        await assert_rows(r, p, cases)

    regions_world(scenario)


def test_rule_1_5_is_empty_reads_a_key_not_known_as_a_list_row_by_row(regions_world):
    """A key of the metadata that is not known as a list is not refused: `is empty` shows the rows where it is a list
    with no element (none of `colour`, a text field; e2's `note`, text on the other rows) and `is not empty` the rows
    that have a value that isn't an empty list."""

    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers them.
        await assert_rows(r, p, [('colour is not null', COLOUR['is_not_null']), ('note = "a"', {'e0'})])
        await assert_rows(
            r,
            p,
            [
                ('colour is empty', set()),
                ('colour is not empty', COLOUR['is_not_empty']),
                ('note is empty', NOTE['is_empty']),
                ('note is not empty', NOTE['is_not_empty']),
                (leaf('note', 'is_empty'), NOTE['is_empty']),
                (leaf('colour', 'is_not_empty'), COLOUR['is_not_empty']),
            ],
        )

    regions_world(scenario)


def says_what_is_empty_takes(message: str, path: Optional[str] = None) -> None:
    """A refusal of rule 1.5 says what `is empty` can be asked of: a list; for a path through one, the list itself."""
    assert 'list' in message.lower(), message
    if path is not None:
        assert re.search(rf'\b{re.escape(path)} is (not )?empty\b', message), message


# The text of each refusal of rule 1.5, and the list it names (for a path through one).
REFUSED_TEXT: list[tuple[str, Optional[str]]] = [
    ('id is empty', None),
    ('filepath is empty', None),
    ('media_type is not empty', None),
    ('created_at is empty', None),
    ('last_modified_at is not empty', None),
    ('location is empty', None),
    ('concept "scene" is empty', None),
    ('detections.label is empty', 'detections'),
    ('detections.label is not empty', 'detections'),
]
REFUSED_FILTERS: list[tuple[dict[str, Any], Optional[str]]] = [
    (leaf('id', 'is_empty'), None),
    (leaf('filepath', 'is_not_empty'), None),
    (leaf('media_type', 'is_empty'), None),
    (leaf('created_at', 'is_empty'), None),
    (leaf('last_modified_at', 'is_not_empty'), None),
    (leaf('location', 'is_empty'), None),
    (leaf('detections.label', 'is_empty'), 'detections'),
    (leaf('detections.label', 'is_not_empty'), 'detections'),
]


def test_rule_1_5_is_empty_of_a_field_that_holds_no_list_or_of_a_path_through_a_list_is_refused(regions_world):
    """Asked of a field that can hold no list, or of a path through a list (`detections.label`, which the dataset knows
    as a list of labels), `is empty` and `is not empty` are refused with a 400 that says what they can be asked of:
    the text by the parse route (a mistake with its line and column), the filter by the samples query and by a
    version's. Main takes `is null` of the same fields, asked first; it refuses the text with words about `null`."""

    async def scenario(r):
        p = await plant(r)
        for text in ('id is null', 'detections.label is null', 'location is null'):
            assert (await parse(r, text, EMPTY)).status_code == 200, text
        for text, path in REFUSED_TEXT:
            response = await parse(r, text, EMPTY)
            assert response.status_code == 400, (text, response.status_code, response.text)
            detail = response.json()['detail']
            [error] = detail['errors']
            says_what_is_empty_takes(error['message'], path)
        version = await version_of(r, p)
        for node, path in REFUSED_FILTERS:
            for where in (
                f'/api/datasets/{EMPTY}/samples/query',
                f'/api/datasets/{EMPTY}/versions/{version}/samples/query',
            ):
                response = await post(r, where, {'limit': 10, 'filters': node})
                assert response.status_code == 400, (where, node, response.status_code, response.text)
                says_what_is_empty_takes(refusal(response), path)

    regions_world(scenario)


def test_rule_1_6_tags_is_empty_answers_as_tags_is_null_and_is_not_empty_as_is_not_null(regions_world):
    """For tags, which every sample holds, the empty list is both empty and no value: `tags is empty` shows the samples
    with no tags, `tags is not empty` those with at least one, and the NOT of `tags is empty` too."""

    async def scenario(r):
        p = await plant(r)
        # Asked first, as main answers it.
        await assert_rows(r, p, [('tags is null', UNTAGGED)])
        await assert_rows(
            r,
            p,
            [
                ('tags is empty', UNTAGGED),
                (leaf('tags', 'is_empty'), UNTAGGED),
                ('tags is not empty', EVERY - UNTAGGED),
                (leaf('tags', 'is_not_empty'), EVERY - UNTAGGED),
                ('NOT (tags is empty)', EVERY - UNTAGGED),
                ('tags is empty AND inferred_tags is empty', {'e1', 'e5'}),
            ],
        )

    regions_world(scenario)


def test_rule_1_6_pin_tags_is_null_and_is_not_null_answer_as_main_does(regions_world):
    async def scenario(r):
        p = await plant(r)
        await assert_rows(
            r,
            p,
            [
                ('tags is null', UNTAGGED),
                ('tags is not null', EVERY - UNTAGGED),
                (leaf('tags', 'is_null'), UNTAGGED),
                (leaf('tags', 'is_not_null'), EVERY - UNTAGGED),
            ],
        )

    regions_world(scenario)


# A mirror's own tags, written through the API: (the lists written one after the other; none: never written).
LOCAL_WRITES: dict[str, list[list[str]]] = {
    'm-checked': [['checked']],
    'm-two': [['checked', 'redo']],
    'm-emptied': [['redo'], []],
    'm-never': [],
}
LOCAL_TAGGED = {'m-checked', 'm-two'}


async def plant_local(r: Regions) -> Planted:
    """LOCAL: samples added, the dataset made a mirror (as test_protected_metadata_keys_spec makes one), and each
    sample's `@local.tags` written as LOCAL_WRITES says."""
    await r.dataset(LOCAL)
    samples = [{'filepath': f'{name}.jpg', 'metadata': {'plot': name}} for name in LOCAL_WRITES]
    await datasets.add_samples(LOCAL, {'samples': samples}, user=ADMIN_USER)
    stored = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', LOCAL)
    p = Planted(LOCAL, {item['filepath'][: -len('.jpg')]: item['id'] for item in stored}, 0)
    await r.database.execute(
        'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES ($1, 7, 0)',
        LOCAL,
    )
    await r.database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = $1", LOCAL)
    for name, writes in LOCAL_WRITES.items():
        for written in writes:
            response = await r.call(
                ADMIN,
                'PATCH',
                f'/api/datasets/{LOCAL}/samples/{p.ids[name]}',
                json={'array_fields': {'@local.tags': written}},
            )
            assert response.status_code == 200, response.text
    stored = await r.database.fetch('SELECT id, local_tags FROM samples WHERE dataset_id = $1', LOCAL)
    held = {item['id']: list(item['local_tags'] or []) for item in stored}
    assert {name: held[p.ids[name]] for name in LOCAL_WRITES} == {
        name: writes[-1] if writes else [] for name, writes in LOCAL_WRITES.items()
    }, held
    return p


def test_rule_1_6_pin_local_tags_is_null_and_is_not_null_answer_as_main_does(regions_world):
    async def scenario(r):
        p = await plant_local(r)
        assert await shown(r, p, leaf('@local.tags', 'is_null')) == set(LOCAL_WRITES) - LOCAL_TAGGED
        assert await shown(r, p, leaf('@local.tags', 'is_not_null')) == LOCAL_TAGGED

    regions_world(scenario)


def test_rule_1_6_local_tags_is_empty_answers_as_is_null_and_is_not_empty_as_is_not_null(regions_world):
    """A mirror's own tags are read as tags are: written and emptied, or never written, a sample has none."""

    async def scenario(r):
        p = await plant_local(r)
        # Asked first, as main answers it.
        assert await shown(r, p, leaf('@local.tags', 'in', ['checked'])) == LOCAL_TAGGED
        untagged = set(LOCAL_WRITES) - LOCAL_TAGGED
        for node, names in (
            (leaf('@local.tags', 'is_empty'), untagged),
            (leaf('@local.tags', 'is_not_empty'), LOCAL_TAGGED),
            (negated(leaf('@local.tags', 'is_empty')), LOCAL_TAGGED),
        ):
            assert await shown(r, p, node) == names, node

    regions_world(scenario)


NEVER_SET = one_rule.NEVER_SET  # added without the field, and a column written NULL (contract g10)
PESTERED = {'a-aphid', 'a-mite'}  # a list with an element; a-empty holds `[]`, a value


async def plant_column(r: Regions) -> Planted:
    """COLUMN: the list field `pests` made as the app makes it (POST array-fields, a column of its own), the samples
    added through the API with their lists (PESTS), and a-null's column then written NULL straight to the database."""
    await r.dataset(COLUMN)
    made = await r.call(ADMIN, 'POST', f'/api/datasets/{COLUMN}/array-fields', json={'field_name': 'pests'})
    assert made.is_success, made.text
    samples = [
        {
            'filepath': f'{name}.jpg',
            'metadata': {'plot': name},
            **({} if held is None else {'array_fields': {'pests': held}}),
        }
        for name, held in PESTS.items()
    ]
    added = await post(r, f'/api/datasets/{COLUMN}/samples', {'samples': samples})
    assert added.is_success, added.text
    stored = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', COLUMN)
    p = Planted(COLUMN, {item['filepath'][: -len('.jpg')]: item['id'] for item in stored}, 0)
    await r.database.execute('UPDATE samples SET pests = NULL WHERE id = $1', p.ids['a-null'])
    return p


def test_rule_1_1_a_list_field_in_a_column_of_its_own_is_null_shows_the_rows_never_set_is_not_null_an_empty_list_too(
    regions_world,
):
    """Rewritten for contract g10 (a list column can hold "never set", rule 3.1): `is null` shows the samples never set
    (a-unset added without the field, a-null's column written NULL) and `is not null` the samples with a list, the
    empty list a-empty among them. Main read the column as tags are, a-empty with no value. A show is asked first."""

    async def scenario(r):
        p = await plant_column(r)
        assert await shown(r, p, leaf('pests', 'in', ['aphid', 'mite'])) == PESTERED
        for form, names in (
            (leaf('pests', 'is_null'), NEVER_SET),
            (leaf('pests', 'is_not_null'), PESTERED | {'a-empty'}),
            ('pests is null', NEVER_SET),
            ('pests is not null', PESTERED | {'a-empty'}),
        ):
            assert await shown(r, p, form) == names, form

    regions_world(scenario)


def test_rule_1_4_a_list_field_in_a_column_of_its_own_is_empty_shows_the_empty_list_and_is_not_empty_an_element(
    regions_world,
):
    """Rewritten for contract g10 (rule 3.1): `is empty` of a list column shows the sample whose list is `[]`, not the
    samples never set; `is not empty` and the NOT of `is empty` the samples with an element. Main answered `is empty`
    as `is null`. A show is asked first."""

    async def scenario(r):
        p = await plant_column(r)
        # Asked first, as main answers it.
        assert await shown(r, p, leaf('pests', 'in', ['aphid', 'mite'])) == PESTERED
        for form, names in (
            (leaf('pests', 'is_empty'), {'a-empty'}),
            (leaf('pests', 'is_not_empty'), PESTERED),
            ('pests is empty', {'a-empty'}),
            ('pests is not empty', PESTERED),
            ('NOT (pests is empty)', PESTERED),
        ):
            assert await shown(r, p, form) == names, form

    regions_world(scenario)


def test_rule_1_7_pin_exists_and_not_exists_of_a_list_in_a_field_ask_for_the_key_an_empty_list_exists(regions_world):
    """A pin: an empty list in a field exists (rule 1.7), as main answers already; so does a JSON null's key."""

    async def scenario(r):
        p = await plant(r)
        cases: list[tuple[Any, set[str]]] = []
        for field, table in LISTS.items():
            for operator, words in (('exists', 'exists'), ('not_exists', 'not exists')):
                cases += [(f'{field} {words}', table[operator]), (leaf(field, operator), table[operator])]
        await assert_rows(r, p, cases)

    regions_world(scenario)


def test_rule_1_7_a_path_through_a_list_exists_shows_the_rows_of_is_not_null_and_not_exists_those_of_is_null(
    regions_world,
):
    """Rule 1.7, made exact 2026-11-26: `detections.label exists` shows the rows that have a value for the path (e1's
    and e6's empty lists, and the lists with a label, e5's beside an unlabelled one), and `not exists` the rows that
    have none (e3 no key, e4 a JSON null, e2's detections without a label). `detections exists`, of the field, asks
    for the key, as main does, asked first."""

    async def scenario(r):
        p = await plant(r)
        await assert_rows(
            r,
            p,
            [('detections exists', DETECTIONS['exists']), ('detections not exists', DETECTIONS['not_exists'])],
        )
        await assert_rows(
            r,
            p,
            [
                ('detections.label exists', LABEL['exists']),
                ('detections.label not exists', LABEL['not_exists']),
                (leaf('detections.label', 'exists'), LABEL['exists']),
                (leaf('detections.label', 'not_exists'), LABEL['not_exists']),
                ('NOT (detections.label not exists)', LABEL['exists']),
            ],
        )

    regions_world(scenario)


# --- 7.2: the two properties, and the embedding map ---

# Comparisons, and NOTs of comparisons, of each field (filter nodes, so no text form is needed).
COMPARED: dict[str, list[dict[str, Any]]] = {
    'inferred_tags': [
        leaf('inferred_tags', '=', 'weed'),
        leaf('inferred_tags', '!=', 'weed'),
        leaf('inferred_tags', 'in', ['weed']),
        leaf('inferred_tags', 'not in', ['weed']),
        leaf('inferred_tags', 'contains', 'weed'),
        negated(leaf('inferred_tags', '=', 'weed')),
        negated(leaf('inferred_tags', 'contains', 'weed')),
    ],
    'detections': [
        leaf('detections', 'match_elements', leaf('label', '=', 'car')),
        negated(leaf('detections', 'match_elements', leaf('label', '=', 'car'))),
    ],
    'detections.label': [
        leaf('detections.label', '=', 'car'),
        leaf('detections.label', '!=', 'car'),
        leaf('detections.label', 'in', ['car']),
        leaf('detections.label', 'not in', ['car']),
        negated(leaf('detections.label', '=', 'car')),
    ],
    'colour': [
        leaf('colour', '=', 'red'),
        leaf('colour', '!=', 'red'),
        leaf('colour', 'in', ['red', 'blue']),
        leaf('colour', 'not in', ['red']),
        leaf('colour', 'contains', 'e'),
        leaf('colour', '>', 'b'),
        negated(leaf('colour', '=', 'red')),
    ],
    'tags': [
        leaf('tags', 'in', ['seen']),
        leaf('tags', 'not in', ['seen']),
        leaf('tags', '!=', 'seen'),
        leaf('tags', 'contains', 'seen'),
        negated(leaf('tags', 'in', ['seen'])),
    ],
}


async def assert_null_splits_the_rows(
    r: Regions,
    p: Planted,
    field: str,
    compared: list[dict[str, Any]],
    every: set[str],
) -> None:
    """Rule 1.3: each row is in exactly one of `f is null` and `f is not null`, and no comparison nor NOT of one shows
    a row that `f is null` shows."""
    null, valued = await shown(r, p, leaf(field, 'is_null')), await shown(r, p, leaf(field, 'is_not_null'))
    assert not null & valued and null | valued == every, (field, sorted(null), sorted(valued))
    assert null and valued, (field, 'the property tests nothing here')
    for node in compared:
        found = await shown(r, p, node)
        assert not found & null, (field, node, 'shows rows with no value', sorted(found & null))


def test_rule_1_3_every_row_is_in_exactly_one_of_is_null_and_is_not_null_and_no_comparison_shows_one_is_null_shows(
    regions_world,
):
    """Over EMPTY's lists, its list of labels, a scalar field and tags, LOCAL's `@local.tags` and COLUMN's list field.
    Main fails it on the list of labels: its `is null` showed the empty lists, which `!=` shows."""

    async def scenario(r):
        p = await plant(r)
        for field, compared in COMPARED.items():
            await assert_null_splits_the_rows(r, p, field, compared, EVERY)
        local = await plant_local(r)
        local_compared = [
            leaf('@local.tags', 'in', ['checked']),
            leaf('@local.tags', 'not in', ['redo']),
            leaf('@local.tags', 'contains', 'redo'),
            negated(leaf('@local.tags', 'in', ['redo'])),
        ]
        await assert_null_splits_the_rows(r, local, '@local.tags', local_compared, set(LOCAL_WRITES))
        column = await plant_column(r)
        column_compared = [
            leaf('pests', 'in', ['aphid']),
            leaf('pests', 'not in', ['aphid']),
            leaf('pests', '!=', 'aphid'),
            leaf('pests', 'contains', 'mite'),
            negated(leaf('pests', 'in', ['aphid'])),
        ]
        await assert_null_splits_the_rows(r, column, 'pests', column_compared, set(PESTS))

    regions_world(scenario)


def test_rule_1_3_each_row_of_a_list_in_a_field_is_in_exactly_one_of_is_null_is_empty_and_is_not_empty(regions_world):
    async def scenario(r):
        p = await plant(r)
        for field in LISTS:
            null, empty, filled = [
                await shown(r, p, leaf(field, operator)) for operator in ('is_null', 'is_empty', 'is_not_empty')
            ]
            assert null and empty and filled, (field, 'the property tests nothing here')
            assert not null & empty and not null & filled and not empty & filled, (field, null, empty, filled)
            assert null | empty | filled == EVERY, field

    regions_world(scenario)


# (the filter text, whether its rows are asked against the rule too). The forms of main's operators are a pin. Each
# shows a row with a point on the map.
SCATTER: list[tuple[str, bool]] = [
    ('detections.label is null', False),
    ('tags not in ("done") AND detections.label is not null', False),
    (f'not concept "scene" > {CUT} AND inferred_tags is not null', True),
    ('inferred_tags is empty', True),
    ('tags is empty', True),
    ('tags not in ("done") AND detections is not empty', True),
    (f'not concept "scene" > {CUT} AND inferred_tags is empty', True),
    ('NOT (tags in ("done")) AND inferred_tags is not empty', True),
]


@pytest.mark.parametrize(('text', 'by_rule'), SCATTER, ids=[text for text, _ in SCATTER])
def test_7_2_the_embedding_map_shows_the_points_of_the_rows_the_samples_query_shows(regions_world, text, by_rule):
    """Section 7.2: on the embedding map, a filter with one of the new tests, alone or beside an exclusion its fast
    path knows (a concept's exclude, a tags exclusion), gives the points of the rows the samples query gives. The
    forms of main's operators pass on main."""

    async def scenario(r):
        p = await plant(r)
        node = await parsed(r, text, EMPTY)
        rows = await selected(r, node, EMPTY)
        if by_rule:
            assert p.names(rows) == RULE.rows(node, ROWS), (text, sorted(p.names(rows)))
        assert rows & p.of(ON_THE_MAP), (text, 'the map tests nothing here')
        scatter = await ok(
            await post(
                r,
                f'/api/datasets/{EMPTY}/embeddings/query',
                {'field': 'clip', 'filters': node, 'sample_pct': 100},
            ),
            text,
        )
        found = {item['sample_id'] for item in scatter['points']}
        assert found == rows & p.of(ON_THE_MAP), (text, sorted(p.names(found)), sorted(p.names(rows)))

    regions_world(scenario)


# --- 2.2: every reader of a filter takes `is empty` ---


async def every_reader(r: Regions, p: Planted, text: str, names: set[str], monkeypatch: Any, version: int) -> None:
    """Every reader of filters keeps exactly the rows `names` for the filter text (the list of g7 2.1)."""
    node = await parsed(r, text, EMPTY)
    [rule] = node['filters']
    assert RULE.rows(node, ROWS) == names, ("the test's own expectation", text, sorted(RULE.rows(node, ROWS)))
    assert names and names != EVERY, ('the reader tests nothing here', text)
    base = f'/api/datasets/{EMPTY}'
    ids = p.of(names)
    # The grid's list and its count.
    assert await selected(r, node, EMPTY) == ids, ('samples/query', text)
    # An export's pages, as the export dialog asks them.
    exported: list[int] = []
    after: Optional[int] = None
    while True:
        body: dict[str, Any] = {'limit': 2, 'filters': node, 'fields': 'filepath', 'slim': True, 'skip_count': True}
        if after is not None:
            body['after_id'] = after
        items = (await ok(await post(r, f'{base}/samples/query', body), text))['items']
        exported += [item['id'] for item in items]
        if len(items) < 2:
            break
        after = items[-1]['id']
    assert sorted(exported) == sorted(ids), ('the export', text)
    plots = sorted(ROWS[name].values['plot'] for name in names)
    ranks = [ROWS[name].values['rank'] for name in names]
    aggregations = [{'field': 'plot', 'op': 'count'}, {'field': 'plot', 'op': 'values'}]
    count, values = (await ok(await post(r, f'{base}/aggregate', {'filters': node, 'aggregations': aggregations})))[
        'results'
    ]
    assert (count['value'], sorted(values['value'])) == (len(names), plots), ('aggregate', text)
    histogram = await ok(await post(r, f'{base}/histogram', {'field': 'rank', 'filters': node, 'bins': 10}))
    assert histogram['total'] == len(names), ('histogram', text, histogram)
    [facet] = await ok(await post(r, f'{base}/field-stats', {'filters': node, 'fields': ['rank']}))
    assert (facet['min'], facet['max']) == (min(ranks), max(ranks)), ('field-stats', text, facet)
    listed = await ok(await post(r, f'{base}/fields/plot/autocomplete', {'filters': node, 'limit': 100}))
    assert sorted(item['value'] for item in listed['values']) == plots, ('autocomplete', text)
    # The map, and the embeddings map.
    points = await ok(await post(r, f'{base}/map_points', {'filters': node, 'points': True}))
    assert {item['id'] for item in points['items']} == p.of(names & LOCATED), ('map_points', text)
    box = await ok(await post(r, f'{base}/map_extent', {'filters': node}))
    assert box['count'] == len(names & LOCATED), ('map_extent', text, box)
    scatter = await ok(await post(r, f'{base}/embeddings/query', {'field': 'clip', 'filters': node, 'sample_pct': 100}))
    assert {item['sample_id'] for item in scatter['points']} == p.of(names & ON_THE_MAP), ('embeddings map', text)
    # Select All and its token.
    token = await ok(await post(r, f'{base}/selection_token', {'filters': node}))
    assert token['count'] == len(names), ('selection_token', text)
    in_token = {'field': 'id', 'operator': 'in_selection', 'value': token['token']}
    assert await selected(r, in_token, EMPTY) == ids, ('in_selection', text)
    search = {'dataset_id': EMPTY, 'vector': QUERY, 'field_name': 'clip', 'limit': 1000}
    found = await ok(await post(r, '/api/search', {**search, 'filters': node}))
    assert {item['id'] for item in found} == ids, ('/api/search', text)
    # A version's query (a filter read on one row).
    rows = await ok(await post(r, f'{base}/versions/{version}/samples/query', {'filters': node, 'limit': 1000}))
    assert {item['id'] for item in rows['items']} == ids, ('a version: samples/query', text)
    kept = await ok(
        await post(
            r,
            f'{base}/versions/{version}/aggregate',
            {'filters': node, 'aggregations': [{'field': 'plot', 'op': 'values'}]},
        ),
    )
    assert sorted(kept['results'][0]['value']) == plots, ('a version: aggregate', text)
    # A saved view, read back as saved and opened by its stage's filter.
    name = f'view-{uuid4().hex[:8]}'
    made = await saved_view(r, p, rule, name)
    read = await ok(await r.call(ADMIN, 'GET', f"/api/views/{made['slug']}"))
    assert read['content'] == {'viewStages': [stage_of(rule)]}, ('the saved view, read back', text)
    assert await selected(r, read['content']['viewStages'][0]['filter'], EMPTY) == ids, ('its stage', text)
    # The SDK: the text, the filter as a script writes it, and the saved view loaded.
    sdk(r, asyncio.get_running_loop(), monkeypatch)

    def through_the_sdk() -> tuple[set[int], int, set[int], set[int]]:
        dataset = Dataset(f'dataset-{EMPTY}')
        view = dataset.query(text)
        written = dataset.samples[Condition(rule['field'], rule['operator'], rule['value'])]
        return set(view.ids()), view.count(), set(written.ids()), set(dataset.load_view(name).ids())

    assert await asyncio.to_thread(through_the_sdk) == (ids, len(names), ids, ids), ('the SDK', text)
    # The assistant: its view of the text, and the saved view opened.
    await open_door(r)
    view = await result(ADMIN, 'make_view', {'dataset': EMPTY, 'filter': text})
    assert view['count'] == len(names), ('the assistant: make_view', text, view['count'])
    assert await view_ids(ADMIN, view['view']) == ids, ('the assistant: get_samples', text)
    opened = await result(ADMIN, 'open_saved_view', {'slug': made['slug']})
    assert opened['count'] == len(names), ('the assistant: open_saved_view', text, opened['count'])
    assert await view_ids(ADMIN, opened['view']) == ids, ('the assistant: its samples', text)
    # A bulk tag by the filter, run as the job runner would, last (it changes the tags): its count, and its rows.
    tag = f'bulk-{uuid4().hex[:12]}'
    tagged = await post(r, f'{base}/samples/bulk-tag', {'filters': node, 'add_tags': [tag]})
    assert tagged.status_code in (200, 202), tagged.text
    if tagged.status_code == 202:
        updated = (await finish(r.database))['result']['updated_count']
    else:
        updated = tagged.json()['updated_count']
    rows_tagged = await r.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 AND $2 = ANY(tags)', EMPTY, tag)
    assert (updated, {item['id'] for item in rows_tagged}) == (len(names), ids), ('the bulk tag', text)


READERS: list[tuple[str, set[str]]] = [
    ('inferred_tags is empty', INFERRED['is_empty']),
    ('detections is empty', DETECTIONS['is_empty']),
    ('inferred_tags is not empty', INFERRED['is_not_empty']),
]


def test_2_2_pin_every_reader_reads_is_null_of_a_list_in_a_field(regions_world, monkeypatch):
    """The readers below, with a filter main and the rule read alike: a pin, and the control for the next test."""

    async def scenario(r):
        p = await plant(r)
        version = await version_of(r, p)
        await every_reader(r, p, 'inferred_tags is null', INFERRED['is_null'], monkeypatch, version)

    regions_world(scenario)


@pytest.mark.parametrize(('text', 'names'), READERS, ids=[text for text, _ in READERS])
def test_2_2_every_reader_reads_is_empty_and_is_not_empty(regions_world, monkeypatch, text, names):
    async def scenario(r):
        p = await plant(r)
        version = await version_of(r, p)
        await every_reader(r, p, text, names, monkeypatch, version)

    regions_world(scenario)


# --- 7.3: the text ---

# Each text, the filters the parse route makes of it (one per top-level AND part), and each part written back.
WRITTEN: list[tuple[str, list[dict[str, Any]], list[str]]] = [
    ('inferred_tags is empty', [leaf('inferred_tags', 'is_empty')], ['inferred_tags is empty']),
    ('inferred_tags is not empty', [leaf('inferred_tags', 'is_not_empty')], ['inferred_tags is not empty']),
    ('detections is empty', [leaf('detections', 'is_empty')], ['detections is empty']),
    ('inferred_tags IS NOT EMPTY', [leaf('inferred_tags', 'is_not_empty')], ['inferred_tags is not empty']),
    ('tags is empty', [leaf('tags', 'is_empty')], ['tags is empty']),
    ('tags is not empty', [leaf('tags', 'is_not_empty')], ['tags is not empty']),
    ('NOT (inferred_tags is empty)', [negated(leaf('inferred_tags', 'is_empty'))], ['NOT (inferred_tags is empty)']),
    (
        'inferred_tags is empty AND colour = "red"',
        [leaf('inferred_tags', 'is_empty'), leaf('colour', '=', 'red')],
        ['inferred_tags is empty', 'colour = "red"'],
    ),
    (
        '(inferred_tags is empty OR detections is not empty)',
        [either(leaf('inferred_tags', 'is_empty'), leaf('detections', 'is_not_empty'))],
        ['(inferred_tags is empty OR detections is not empty)'],
    ),
    (
        'NOT (inferred_tags is empty OR detections is not empty)',
        [negated(either(leaf('inferred_tags', 'is_empty'), leaf('detections', 'is_not_empty')))],
        ['NOT (inferred_tags is empty OR detections is not empty)'],
    ),
    (
        'NOT (detections is empty AND colour = "blue")',
        [negated(both(leaf('detections', 'is_empty'), leaf('colour', '=', 'blue')))],
        ['NOT (detections is empty AND colour = "blue")'],
    ),
    ('empty is empty', [leaf('empty', 'is_empty')], ['empty is empty']),
    ('empty is not empty', [leaf('empty', 'is_not_empty')], ['empty is not empty']),
]


def test_2_1_each_new_test_parses_to_its_json_form_and_is_written_back_the_same_alone_and_nested(regions_world):
    """Rule 2.1 (section 7.3): `is empty` and `is not empty` in the text, alone and inside AND, OR and NOT, parse to
    their filters and are written back the same, by the parse route's parts and by the format route. The JSON form of
    each is named as its neighbours are. `is null` is asked first, as main answers it."""

    async def scenario(r):
        await plant(r)
        assert await nodes(r, 'inferred_tags is null', EMPTY) == [leaf('inferred_tags', 'is_null')]
        assert await text_of(r, leaf('inferred_tags', 'is_null'), EMPTY) == 'inferred_tags is null'
        for text, filters, parts in WRITTEN:
            response = await parse(r, text, EMPTY)
            assert response.status_code == 200, (text, response.text)
            body = response.json()
            assert (body['filters'], body['parts']) == (filters, parts), (text, body)
            for node, part in zip(filters, parts):
                assert await text_of(r, node, EMPTY) == part, (text, node)
                assert await nodes(r, part, EMPTY) == [node], part

    regions_world(scenario)


def test_2_1_pin_a_field_named_empty_is_read_as_today(regions_world):
    """A field named `empty` (text; e4 holds a JSON null) is read as main reads it, and whatever the format route
    writes for it reads back to the same filter."""

    async def scenario(r):
        p = await plant(r)
        for text, node, names in (
            ('empty = "x"', leaf('empty', '=', 'x'), {'e0'}),
            ('empty in ("x", "y")', leaf('empty', 'in', ['x', 'y']), {'e0', 'e1'}),
            ('empty is null', leaf('empty', 'is_null'), EVERY - {'e0', 'e1'}),
            ('empty exists', leaf('empty', 'exists'), {'e0', 'e1', 'e4'}),
            ('NOT (empty = "x")', negated(leaf('empty', '=', 'x')), {'e1'}),
        ):
            assert await nodes(r, text, EMPTY) == [node], text
            written = await text_of(r, node, EMPTY)
            assert written is not None and await nodes(r, written, EMPTY) == [node], (text, written)
            assert await shown(r, p, text) == names, text

    regions_world(scenario)


def test_2_1_the_messages_that_list_the_operators_name_is_empty_and_is_not_empty(regions_world):
    """The parse route's mistakes that list the operators: an operator a list field doesn't take, a word that is no
    operator, and a word after `is` that isn't null. Each is a 400 at main too, asked first by its line and column."""

    async def scenario(r):
        await plant(r)
        for text, what in (
            ('inferred_tags > 1', 'the operators of a list field'),
            ('inferred_tags maybe', 'the operators'),
            ('inferred_tags is maybe', 'what may follow is'),
        ):
            response = await parse(r, text, EMPTY)
            assert response.status_code == 400, (text, response.text)
            [error] = response.json()['detail']['errors']
            assert error['line'] == 1, error
            message = error['message']
            assert 'not empty' in message and re.search(r'(?<!not )\bempty\b', message), (what, message)

    regions_world(scenario)


# --- 7.4: through the page's wrapping on a dataset with objects ---

OBJECT_ROWS = {
    'im-a': image({'plot': 'a', 'inferred_tags': ['weed'], 'detections': boxes('car')}),
    'ob-a1': child('im-a', {'label': 'crate', 'marks': []}),
    'ob-a2': child('im-a', {'label': 'crate', 'marks': ['scratch']}),
    'im-b': image({'plot': 'b', 'inferred_tags': [], 'detections': []}),
    'ob-b1': child('im-b', {'label': 'crate', 'marks': []}),
    'im-c': image({'plot': 'c', 'inferred_tags': None, 'detections': [UNLABELLED]}),
    'ob-c1': child('im-c', {'label': 'crate', 'marks': None}),
    'ob-c2': child('im-c', {'label': 'crate'}),
    'im-d': image({'plot': 'd'}),
    'ob-d1': child('im-d', {'label': 'crate', 'marks': ['dent']}),
    'im-e': image({'plot': 'e', 'inferred_tags': [], 'detections': None}),  # no objects
    'im-f': image({'plot': 'f', 'detections': boxes('car')}),  # no objects
    'lo-1': child(None, {'label': 'crate', 'marks': []}),
    'lo-2': child(None, {'label': 'crate'}),
}


def held(item: Any, field: str) -> tuple[bool, bool]:
    """(whether the row has a value for the field, whether its list is empty)."""
    if field.endswith('.label'):
        return one_rule.labels(item.values.get(field[: -len('.label')])) is not None, False
    value = item.values.get(field)
    return value is not None, value == []


def wrapped(grain: str, field: str, operator: str) -> set[str]:
    """What a grid shows through the wrapping, by the contract's words (rule 1.8). The rows asked are the image with its
    objects on the images' grid, the object with its image on the objects' grid. `is empty` is met as a comparison
    is: by one of those rows. `is not null` likewise. `is not empty` is the NOT of `is empty`: one of the rows has a
    value and `is empty` isn't met. `is null`: none of them has a value. Of a path through a list (the only field they
    are asked of here), `exists` is `is not null` and `not exists` is `is null` (rule 1.7, made exact 2026-11-26)."""
    found = set()
    for name, item in OBJECT_ROWS.items():
        if item.is_object != (grain == 'object'):
            continue
        if grain == 'image':
            whole = [item, *(each for each in OBJECT_ROWS.values() if each.image == name)]
        else:
            whole = [item, *([OBJECT_ROWS[item.image]] if item.image else [])]
        valued = any(held(each, field)[0] for each in whole)
        empty = any(held(each, field)[1] for each in whole)
        answers = {
            'is_null': not valued,
            'is_not_null': valued,
            'is_empty': empty,
            'is_not_empty': valued and not empty,
        }
        answers.update({'exists': valued, 'not_exists': not valued})
        if answers[operator]:
            found.add(name)
    return found


# (the grid, the field): what each test shows there, written out by hand.
WRAPPED: dict[tuple[str, str], dict[str, set[str]]] = {
    ('image', 'inferred_tags'): {
        'is_null': {'im-c', 'im-d', 'im-f'},
        'is_not_null': {'im-a', 'im-b', 'im-e'},
        'is_empty': {'im-b', 'im-e'},
        'is_not_empty': {'im-a'},
    },
    ('object', 'inferred_tags'): {
        'is_null': {'ob-c1', 'ob-c2', 'ob-d1', 'lo-1', 'lo-2'},
        'is_not_null': {'ob-a1', 'ob-a2', 'ob-b1'},
        'is_empty': {'ob-b1'},
        'is_not_empty': {'ob-a1', 'ob-a2'},
    },
    ('image', 'marks'): {
        'is_null': {'im-c', 'im-e', 'im-f'},  # an image with no objects has no value for an object's field
        'is_not_null': {'im-a', 'im-b', 'im-d'},
        'is_empty': {'im-a', 'im-b'},  # one object's list is empty
        'is_not_empty': {'im-d'},  # im-a's other object has marks, but one of its objects' lists is empty
    },
    ('object', 'marks'): {
        'is_null': {'ob-c1', 'ob-c2', 'lo-2'},
        'is_not_null': {'ob-a1', 'ob-a2', 'ob-b1', 'ob-d1', 'lo-1'},
        'is_empty': {'ob-a1', 'ob-b1', 'lo-1'},
        'is_not_empty': {'ob-a2', 'ob-d1'},
    },
    ('image', 'detections'): {
        'is_null': {'im-d', 'im-e'},
        'is_not_null': {'im-a', 'im-b', 'im-c', 'im-f'},
        'is_empty': {'im-b'},
        'is_not_empty': {'im-a', 'im-c', 'im-f'},
    },
    ('object', 'detections'): {
        'is_null': {'ob-d1', 'lo-1', 'lo-2'},
        'is_not_null': {'ob-a1', 'ob-a2', 'ob-b1', 'ob-c1', 'ob-c2'},
        'is_empty': {'ob-b1'},
        'is_not_empty': {'ob-a1', 'ob-a2', 'ob-c1', 'ob-c2'},
    },
    # The list of labels: im-b's empty list is a value; im-c's detections have no label.
    ('image', 'detections.label'): {
        'is_null': {'im-c', 'im-d', 'im-e'},
        'is_not_null': {'im-a', 'im-b', 'im-f'},
        'not_exists': {'im-c', 'im-d', 'im-e'},
        'exists': {'im-a', 'im-b', 'im-f'},
    },
    ('object', 'detections.label'): {
        'is_null': {'ob-c1', 'ob-c2', 'ob-d1', 'lo-1', 'lo-2'},
        'is_not_null': {'ob-a1', 'ob-a2', 'ob-b1'},
        'not_exists': {'ob-c1', 'ob-c2', 'ob-d1', 'lo-1', 'lo-2'},
        'exists': {'ob-a1', 'ob-a2', 'ob-b1'},
    },
}


def test_7_4_through_the_wrapping_is_empty_is_met_as_a_comparison_and_is_null_by_the_row_as_a_whole(regions_world):
    """Rule 1.8 on both grids, for a list of the images (`inferred_tags`, and the list of labels `detections`) and one
    of the objects (`marks`): `is empty` and the changed absence tests (`is null`, `is not null`, `exists` and `not
    exists` of the path `detections.label`) ask the rows #521 asks of the same field. `is null` and `is not null` of
    the plain lists are asked first, as main answers them."""
    for (grain, field), table in WRAPPED.items():
        for operator, names in table.items():
            assert wrapped(grain, field, operator) == names, ("the test's own expectation", grain, field, operator)

    async def scenario(r):
        planted = await plant_objects(r, OBJECTS, OBJECT_ROWS)
        sidebar = await r.call(ADMIN, 'GET', f'/api/datasets/{OBJECTS}/field-stats')
        assert sidebar.status_code == 200, sidebar.text

        async def grid(grain: str, stage: dict[str, Any]) -> set[str]:
            return planted.names(await selected(r, page_filter([stage], grain), OBJECTS))

        for (grain, field), table in WRAPPED.items():
            if field != 'detections.label':
                for operator in ('is_null', 'is_not_null'):
                    assert await grid(grain, leaf(field, operator)) == table[operator], (grain, field, operator)
        for (grain, field), table in WRAPPED.items():
            for operator, names in table.items():
                assert await grid(grain, leaf(field, operator)) == names, (grain, field, operator)
            if 'is_empty' in table:
                around = negated(leaf(field, 'is_empty'))
                assert await grid(grain, around) == table['is_not_empty'], (grain, field, 'NOT is empty')
        # Typed in a filter card, as the page sends it.
        for text, field, operator in (
            ('marks is empty', 'marks', 'is_empty'),
            ('NOT (marks is empty)', 'marks', 'is_not_empty'),
            ('inferred_tags is not empty', 'inferred_tags', 'is_not_empty'),
            ('detections.label is null', 'detections.label', 'is_null'),
            ('detections.label not exists', 'detections.label', 'not_exists'),
        ):
            for grain in ('image', 'object'):
                assert await grid_shown(r, planted, grain, text) == WRAPPED[(grain, field)][operator], (text, grain)

    regions_world(scenario)


# --- 7.5: the SDK as a script uses it ---


def test_7_5_the_sdk_as_a_script_has_a_condition_for_each_new_test(regions_world, monkeypatch):
    """The SDK's conditions for `is empty` and `is not empty`, in the style of the ones it has (`F(f).exists()`,
    `f__is_null=True`), send the two filters and show their rows; so do the text and a NOT of the condition.
    `inferred_tags__is_null=False` is asked first, as main answers it."""

    async def scenario(r):
        p = await plant(r)
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def control() -> tuple[set[int], int]:
            view = Dataset(f'dataset-{EMPTY}').filter(inferred_tags__is_null=False)
            return set(view.ids()), view.count()

        assert await asyncio.to_thread(control) == (p.of(INFERRED['is_not_null']), len(INFERRED['is_not_null']))

        def script() -> tuple[dict[str, Any], dict[str, tuple[set[int], int]]]:
            dataset = Dataset(f'dataset-{EMPTY}')
            s = dataset.samples
            wire = {
                'is_empty': F('inferred_tags').is_empty().to_dict(),
                'is_not_empty': F('inferred_tags').is_not_empty().to_dict(),
            }
            views = {
                's.inferred_tags.is_empty()': s[s.inferred_tags.is_empty()],
                's.inferred_tags.is_not_empty()': s[s.inferred_tags.is_not_empty()],
                '~s.inferred_tags.is_empty()': s[~s.inferred_tags.is_empty()],
                'exclude(is_empty())': s.all().exclude(s.inferred_tags.is_empty()),
                "F('detections').is_empty()": s[F('detections').is_empty()],
                "F('detections').is_not_empty()": s[F('detections').is_not_empty()],
                's.tags.is_empty()': s[s.tags.is_empty()],
                'inferred_tags__is_empty=True': dataset.filter(inferred_tags__is_empty=True),
                'inferred_tags__is_empty=False': dataset.filter(inferred_tags__is_empty=False),
                'inferred_tags__is_not_empty=True': dataset.filter(inferred_tags__is_not_empty=True),
                'inferred_tags__is_not_empty=False': dataset.filter(inferred_tags__is_not_empty=False),
                "query('detections is empty')": dataset.query('detections is empty'),
            }
            return wire, {name: (set(view.ids()), view.count()) for name, view in views.items()}

        wire, found = await asyncio.to_thread(script)
        assert wire == {
            'is_empty': leaf('inferred_tags', 'is_empty'),
            'is_not_empty': leaf('inferred_tags', 'is_not_empty'),
        }
        for name, names in (
            ('s.inferred_tags.is_empty()', INFERRED['is_empty']),
            ('s.inferred_tags.is_not_empty()', INFERRED['is_not_empty']),
            ('~s.inferred_tags.is_empty()', INFERRED['is_not_empty']),
            ('exclude(is_empty())', INFERRED['is_not_empty']),
            ("F('detections').is_empty()", DETECTIONS['is_empty']),
            ("F('detections').is_not_empty()", DETECTIONS['is_not_empty']),
            ('s.tags.is_empty()', UNTAGGED),
            ('inferred_tags__is_empty=True', INFERRED['is_empty']),
            ('inferred_tags__is_empty=False', INFERRED['is_not_empty']),
            ('inferred_tags__is_not_empty=True', INFERRED['is_not_empty']),
            ('inferred_tags__is_not_empty=False', INFERRED['is_empty']),
            ("query('detections is empty')", DETECTIONS['is_empty']),
        ):
            assert found[name] == (p.of(names), len(names)), (name, sorted(p.names(found[name][0])))

    regions_world(scenario)


NEAREST = 5  # the similarity search keeps e0 to e4, nearest first


def test_7_5_a_filter_after_a_similarity_search_with_is_empty_keeps_the_results_whose_list_is_empty(
    regions_world,
    monkeypatch,
):
    """`sort_by_similarity(...).filter(f is empty)` keeps the results whose list is empty, in their order with their
    distances (e1 among the nearest five; e5's empty list is no result), as the condition and as the filter node; `is
    not empty` the results with a list that holds something. `is not null` is asked first, as main answers it."""
    order = list(ROWS)

    async def scenario(r):
        p = await plant(r)
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def filtered(conditions: dict[str, Any]) -> tuple[list[tuple[int, float]], dict[str, list[tuple[int, float]]]]:
            similar = Dataset(f'dataset-{EMPTY}').all().sort_by_similarity(vector=QUERY, limit=NEAREST)
            listed = [(sample.id, sample.distance) for sample in similar]
            kept = {}
            for name, build in conditions.items():
                kept[name] = [(sample.id, sample.distance) for sample in similar.filter(build())]
            return listed, kept

        listed, control = await asyncio.to_thread(
            filtered,
            {'is not null': lambda: Condition('inferred_tags', 'is_not_null', None)},
        )
        assert [p.names([sample_id]).pop() for sample_id, _ in listed] == order[:NEAREST], 'the nearest first'

        def among(names: set[str]) -> list[tuple[int, float]]:
            return [(sample_id, distance) for sample_id, distance in listed if p.names([sample_id]) <= names]

        # Asked first, as main answers it.
        assert control['is not null'] == among(INFERRED['is_not_null'])
        again, kept = await asyncio.to_thread(
            filtered,
            {
                'is empty, as a node': lambda: Condition('inferred_tags', 'is_empty', None),
                'is empty': lambda: F('inferred_tags').is_empty(),
                'is not empty': lambda: F('inferred_tags').is_not_empty(),
                'detections is empty': lambda: F('detections').is_empty(),
            },
        )
        assert again == listed
        for name, names in (
            ('is empty, as a node', INFERRED['is_empty']),
            ('is empty', INFERRED['is_empty']),
            ('is not empty', INFERRED['is_not_empty']),
            ('detections is empty', DETECTIONS['is_empty']),
        ):
            assert kept[name] == among(names), (name, [p.names([sample_id]).pop() for sample_id, _ in kept[name]])
        assert [p.names([sample_id]).pop() for sample_id, _ in kept['is empty']] == ['e1']

    regions_world(scenario)


# --- 7.6: the assistant ---


def test_7_6_the_assistants_tools_take_is_empty_and_is_not_empty(regions_world):
    """make_view takes the new tests in its filter text, writes them back as the language does, and counts their rows;
    refine_view adds one and turns it around. The untagged samples are still `tags is null`, asked first, as main
    answers it."""

    async def scenario(r):
        p = await plant(r)
        await open_door(r)

        async def view_of(**arguments: Any) -> tuple[dict[str, Any], set[str]]:
            answer = await result(ADMIN, 'refine_view' if 'view' in arguments else 'make_view', arguments)
            return answer, p.names(await view_ids(ADMIN, answer['view']))

        untagged, names = await view_of(dataset=EMPTY, filter='tags is null')
        assert (untagged['count'], names) == (len(UNTAGGED), UNTAGGED)
        for text, wanted, written in (
            ('inferred_tags is empty', INFERRED['is_empty'], True),
            ('inferred_tags is not empty', INFERRED['is_not_empty'], True),
            ('detections is empty', DETECTIONS['is_empty'], True),
            ('NOT (detections is empty)', DETECTIONS['is_not_empty'], True),
            ('tags is empty', UNTAGGED, False),
            ('detections.label is null', LABEL['is_null'], True),
        ):
            view, names = await view_of(dataset=EMPTY, filter=text)
            assert (view['count'], names) == (len(wanted), wanted), (text, view['count'], sorted(names))
            if written:
                assert view['filter'] == text, (text, view['filter'])
        whole, _ = await view_of(dataset=EMPTY)
        emptied, names = await view_of(view=whole['view'], add='inferred_tags is empty')
        assert (emptied['count'], names) == (len(INFERRED['is_empty']), INFERRED['is_empty'])
        turned, names = await view_of(view=emptied['view'], negate=[1])
        assert (turned['count'], names) == (len(INFERRED['is_not_empty']), INFERRED['is_not_empty'])

    regions_world(scenario)


def test_7_6_what_the_assistant_and_the_mcp_server_are_told_names_is_empty_and_is_not_empty():
    """Rule 2.5: the text a model is given before the tools (the MCP server's instructions too) and the tools' own
    words name the two new tests."""
    told = [assistant.instructions()]
    for tool in (*assistant.tools(), *assistant.admin_tools()):
        told += [tool.description, json.dumps(tool.input_schema)]
    text = '\n'.join(told)
    assert 'is not empty' in text, 'is not empty'
    # field_values' words "values is empty" (of its answer, on main) are not the test.
    assert re.search(r'(?<!values )\bis empty\b', text), 'is empty'
