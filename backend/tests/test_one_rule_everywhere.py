"""One rule for a missing value, everywhere: a row with no value for a field never matches a comparison of that field,
and never matches the NOT of one; only `is null` and `not exists` find it (docs/FILTER_QUERY.md, "Operators").

The implementer's tests, beside the spec's. Each names the rule it holds. The plain dataset's rows have, for a text
field, a number field, a list of labels, tags, a concept's score, a location and a lasso's point: a value, another
value, a JSON null and no key (and, for the rule on a value of another kind, text where a number is compared). The same
readings are asked on a dataset with objects through the page's wrapping, with the record of which kind of row has
which field (backend/field_kinds.py) and without it.
"""

import asyncio
import os
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from backend import field_kinds, negation
from backend.routers import datasets, embeddings
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_assistant_core_spec import open_door
from backend.tests.test_assistant_views_spec import result
from backend.tests.test_broad_scatter import populate
from backend.tests.test_field_kinds import AND, NOT, OR, eq, found, images, leaf, objects
from backend.tests.test_object_parent_column import add, ids
from backend.tests.test_regions_spec import (
    ADMIN_USER,
    LOCATED,
    Regions,
    assert_every_reader,
    drop,
    excluded,
    farm,
    grain,
    inside,
    keep,
    loaded,
    nodes,
    parse,
    post,
    selected,
)

registry_database = registry_helpers.registry_database
database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)

SQUARE = [[0, 0], [5, 0], [5, 5], [0, 5]]
LASSO = [[0, 0], [10, 0], [10, 10], [0, 10]]
# The plain dataset's rows: name -> metadata. Tags, scores, locations and lasso points are set apart (below).
ROWS: dict[str, dict[str, Any]] = {
    'v': {'colour': 'red', 'n': 1, 'detections': [{'label': 'car'}], 'segmentation': {'labels': ['a']}},
    'w': {'colour': 'blue', 'n': 2, 'detections': [{'label': 'truck'}], 'segmentation': {'labels': ['b']}},
    'null': {'colour': None, 'n': None, 'detections': None, 'segmentation': None},
    'none': {},
    'empty': {'detections': [], 'segmentation': {'labels': []}},
    'nolabel': {'detections': [{'confidence': 0.5}]},
    'text': {'colour': 7, 'n': 'abc'},  # values of another kind than the one compared
}
EVERY = set(ROWS)
TAGS = {'v': ['rejected'], 'w': ['checked']}
SCORES = {'v': 0.9, 'w': 0.1}
PLACES = {'v': (2, 2), 'w': (10, 10)}
POINTS = {'v': (1, 1), 'w': (20, 20)}


async def plain(database) -> dict[str, int]:
    """Dataset 2, without objects: ROWS, with their tags, a concept's scores, locations and points on the clip map."""
    names = list(ROWS)
    await add(2, [(None, ROWS[name]) for name in names], 'plain/row')
    found_ids = await ids(database, 2, 'plain/row')
    by_name = dict(zip(names, found_ids))
    for name, tags in TAGS.items():
        await database.execute('UPDATE samples SET tags = $2 WHERE id = $1', by_name[name], tags)
    for name, (lon, lat) in PLACES.items():
        await database.execute(
            'UPDATE samples SET location = ST_SetSRID(ST_MakePoint($2, $3), 4326) WHERE id = $1',
            by_name[name],
            float(lon),
            float(lat),
        )
    concept = await database.fetchval(
        'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
        "VALUES (2, 'x', 'clip', 'seed', '[1,0]', 0.5) RETURNING id",
    )
    for name, score in SCORES.items():
        await database.execute(
            'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, $3)',
            concept,
            by_name[name],
            score,
        )
    await points(database, {by_name[name]: point for name, point in POINTS.items()}, 'clip')
    return {**by_name, '_concept': concept}


async def points(database, at: dict[int, tuple[float, float]], field: str) -> None:
    for sample_id, (x, y) in at.items():
        await database.execute(
            'INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) '
            "SELECT dataset_id, id, $2, '[1,2,3]'::vector FROM samples WHERE id = $1",
            sample_id,
            field,
        )
        await database.execute(
            'INSERT INTO embedding_coordinates (sample_id, field_name, x, y) VALUES ($1, $2, $3, $4)',
            sample_id,
            field,
            float(x),
            float(y),
        )


def concept_test(concept: int, operator: str, threshold: float = 0.5, op: str = '>') -> dict:
    return {
        'field': '_concept',
        'operator': operator,
        'value': {'concept_id': concept, 'op': op, 'threshold': threshold},
    }


def names_of(rows: dict[str, int], found_ids: set[int]) -> set[str]:
    back = {sample_id: name for name, sample_id in rows.items() if not name.startswith('_')}
    return {back[sample_id] for sample_id in found_ids}


async def shown(rows: dict[str, int], node: dict) -> set[str]:
    return names_of(rows, await found(2, node))


# --- Section 1 on a dataset without objects, through the samples query and its count ---


def test_rule_1_1_a_negation_of_a_field_shows_only_the_rows_with_another_value(registry_database):
    async def scenario(database):
        rows = await plain(database)
        for node in (
            leaf('colour', '!=', 'red'),
            leaf('colour', 'not in', ['red']),
            leaf('n', '!=', 1),
            leaf('n', 'not in', [1]),
        ):
            assert await shown(rows, node) == {'w', 'text'}, node
        assert await shown(rows, leaf('n', '!=', 2)) == {'v', 'text'}  # `text`'s "abc" is a value (rule 1.7)
        # An empty list: every row with a value.
        assert await shown(rows, leaf('colour', 'not in', [])) == {'v', 'w', 'text'}
        # The old meaning written out gives main's rows for the bare form.
        assert await shown(rows, OR(leaf('colour', '!=', 'red'), leaf('colour', 'is_null'))) == EVERY - {'v'}

    registry_database(scenario)


def test_rule_1_2_not_needs_a_value_for_every_field_compared_inside_it(registry_database):
    async def scenario(database):
        rows = await plain(database)
        assert await shown(rows, NOT(eq('colour', 'red'))) == {'w', 'text'}
        assert await shown(rows, NOT(AND(eq('colour', 'red'), eq('n', 1)))) == {'w', 'text'}
        assert await shown(rows, NOT(OR(eq('colour', 'red'), eq('n', 2)))) == {'text'}
        assert await shown(rows, NOT(NOT(eq('colour', 'red')))) == {'v'}
        assert await shown(rows, NOT(leaf('colour', '!=', 'red'))) == {'v'}
        # An absence test inside a NOT adds no field (rule 1.7).
        assert await shown(rows, NOT(leaf('colour', 'is_null'))) == {'v', 'w', 'text'}
        assert await shown(rows, NOT(OR(eq('colour', 'red'), leaf('note', 'exists')))) == {'w', 'text'}
        # A test that compares nothing (a range with no value) adds no field either.
        assert await shown(rows, NOT(AND(eq('colour', 'red'), leaf('n', '>', '')))) == {'w', 'text'}

    registry_database(scenario)


def test_rule_1_3_a_concepts_exclude_shows_only_the_rows_with_a_score(registry_database):
    async def scenario(database):
        rows = await plain(database)
        concept = rows['_concept']
        assert await shown(rows, concept_test(concept, 'concept_exclude')) == {'w'}
        assert await shown(rows, NOT(concept_test(concept, 'concept_select'))) == {'w'}
        assert await shown(rows, concept_test(concept, 'concept_select')) == {'v'}
        assert await shown(rows, concept_test(concept, 'concept_exclude', op='<=')) == {'v'}

    registry_database(scenario)


def test_the_list_tests_a_not_needs_are_true_or_false_never_null(registry_database):
    """`_list_sql` (an element match's NOT needs a list) and `_labels_valued_sql` (a label list's NOT needs the list,
    empty or holding a label) answer TRUE or FALSE on every row, never NULL, so a caller may negate them."""

    async def scenario(database):
        values: dict[str, dict[str, Any]] = {
            'nokey': {},
            'null': {'f': None},
            'text': {'f': 'a list'},
            'empty': {'f': []},
            'labelled': {'f': [{'label': 'car'}]},
            'unlabelled': {'f': [{'confidence': 0.5}]},
        }
        names = list(values)
        await add(2, [(None, {'name': name, **values[name]}) for name in names], 'total/row')
        rows = await database.fetch(
            f"SELECT metadata->>'name' AS name, {datasets._list_sql("metadata->'f'")} AS listed, "
            f"{datasets._labels_valued_sql('f', 'label')} AS labelled FROM samples "
            "WHERE dataset_id = 2 AND filepath LIKE 'total/row-%'",
        )
        answers = {row['name']: (row['listed'], row['labelled']) for row in rows}
        assert answers == {
            'nokey': (False, False),
            'null': (False, False),
            'text': (False, False),
            'empty': (True, True),
            'labelled': (True, True),
            'unlabelled': (True, False),
        }

    registry_database(scenario)


def test_rule_1_4_a_list_of_labels_needs_the_list_empty_or_with_a_label(registry_database):
    """2026-11-26: an empty list is a value (nothing was found, so no car); a list whose elements have no label at all
    is not (something was found and what it is isn't known); no list, a JSON null, no key: no value."""

    async def scenario(database):
        rows = await plain(database)
        for operator, value in (
            ('!=', 'car'),
            ('not in', ['car']),
            ('<>', 'car'),
            ('nin', ['car']),
            ('not_in', ['car']),
        ):
            assert await shown(rows, leaf('detections.label', operator, value)) == {'w', 'empty'}, operator
        assert await shown(rows, NOT(eq('detections.label', 'car'))) == {'w', 'empty'}
        assert await shown(rows, leaf('segmentation.labels', '!=', 'a')) == {'w', 'empty'}
        assert await shown(rows, leaf('segmentation.labels', 'not in', ['a'])) == {'w', 'empty'}
        # Contract g9 rule 1.2 (2026-11-26): `is null` of a list of labels shows the rows with no value for it (no
        # list, a null, no key, elements none of which has a label), never an empty list; `is not null` the rows `!=`
        # can show. A list of labels has no key of its own: `exists` and `not exists` are the same two tests (1.7).
        no_label = {'null', 'none', 'nolabel', 'text'}
        valued = {'v', 'w', 'empty'}
        assert await shown(rows, leaf('detections', 'is_null')) == {'null', 'none', 'text'}
        for field in ('detections.label', 'segmentation.labels'):
            assert await shown(rows, leaf(field, 'is_null')) == no_label, field
            assert await shown(rows, leaf(field, 'not_exists')) == no_label, field
            assert await shown(rows, leaf(field, 'is_not_null')) == valued, field
            assert await shown(rows, leaf(field, 'exists')) == valued, field
        # Inside a match_elements condition, an element with no label is never "not in" a list.
        inner = {'field': 'label', 'operator': 'not_in', 'value': ['car']}
        assert await shown(rows, leaf('detections', 'match_elements', inner)) == {'w'}
        # A NOT around an element match needs the list, empty or not (no list, a null, no key: not shown); an element
        # without the key the condition reads counts, and never meets it.
        car, truck = eq('label', 'car'), eq('label', 'truck')
        assert await shown(rows, NOT(leaf('detections', 'match_elements', car))) == {'w', 'empty', 'nolabel'}
        assert await shown(rows, NOT(leaf('detections', 'elem_match', truck))) == {'v', 'empty', 'nolabel'}

    registry_database(scenario)


def test_rule_1_5_tags_need_at_least_one_tag(registry_database):
    async def scenario(database):
        rows = await plain(database)
        for node in (
            leaf('tags', 'not in', ['rejected']),
            leaf('tags', '!=', 'rejected'),
            NOT(leaf('tags', 'in', ['rejected'])),
            NOT(leaf('tags', 'contains', 'rejected')),
        ):
            assert await shown(rows, node) == {'w'}, node
        assert await shown(rows, leaf('tags', 'is_null')) == EVERY - {'v', 'w'}
        assert await shown(rows, leaf('tags', 'is_not_null')) == {'v', 'w'}
        assert await shown(rows, OR(leaf('tags', 'not in', ['rejected']), leaf('tags', 'is_null'))) == EVERY - {'v'}

    registry_database(scenario)


def test_rule_1_6_an_excluded_place_or_lasso_shows_only_the_rows_with_a_location_or_a_point(registry_database):
    async def scenario(database):
        rows = await plain(database)
        located = {'v', 'w'}
        for node in (
            leaf('location', 'geo_polygon_exclude', SQUARE),
            NOT(leaf('location', 'geo_polygon', SQUARE)),
            NOT(leaf('location', 'geo_near', {'point': [2, 2], 'max_distance': 1000})),
        ):
            assert await shown(rows, node) == {'w'}, node
        # Across the antimeridian, the exclusion's own forms (one copy, two copies) need a location too.
        across = [[355, 0], [365, 0], [365, 5], [355, 5]]  # around longitude 0, one turn on: holds v
        assert await shown(rows, leaf('location', 'geo_polygon_exclude', across)) == {'w'}
        wide = [[-5, 0], [400, 0], [400, 5], [-5, 5]]  # more than a turn wide: two copies
        assert await shown(rows, leaf('location', 'geo_polygon_exclude', wide)) == {'w'}
        assert await shown(rows, leaf('location', 'geo_polygon_exclude', [[0, 0], [1, 1]])) == EVERY  # no polygon
        assert await shown(rows, leaf('location', 'is_null')) == EVERY - located
        assert await shown(rows, leaf('location', 'is_not_null')) == located
        # The old meaning written out.
        old = OR(leaf('location', 'geo_polygon_exclude', SQUARE), leaf('location', 'is_null'))
        assert await shown(rows, old) == EVERY - {'v'}
        # A lasso: the rows with a point on that map, none inside.
        assert await shown(rows, leaf('clip', 'embedding_polygon_exclude', LASSO)) == {'w'}
        assert await shown(rows, NOT(leaf('clip', 'embedding_polygon', LASSO))) == {'w'}
        assert await shown(rows, NOT(AND(AND(leaf('clip', 'embedding_polygon', LASSO))))) == {'w'}  # one-rule groups
        assert await shown(rows, leaf('clip', 'embedding_polygon_exclude', [[0, 0], [1, 1]])) == EVERY
        assert await shown(rows, NOT(AND(leaf('clip', 'embedding_polygon', [[0, 0], [1, 1]])))) == EVERY
        # A NOT with the lasso and another part needs the point and the other field.
        assert await shown(rows, NOT(leaf('clip', 'embedding_polygon', LASSO), eq('colour', 'blue'))) == {'v', 'w'}
        # A patch-scale map has a point per tile: a row with one, and none of them inside.
        await points(database, {rows['v']: (1, 1), rows['w']: (30, 30)}, 'clip_patch_2x2_r0c0')
        await points(database, {rows['v']: (20, 20), rows['w']: (20, 20)}, 'clip_patch_2x2_r0c1')
        assert await shown(rows, leaf('clip_patch_2x2', 'embedding_polygon', LASSO)) == {'v'}
        assert await shown(rows, leaf('clip_patch_2x2', 'embedding_polygon_exclude', LASSO)) == {'w'}
        assert await shown(rows, NOT(leaf('clip_patch_2x2', 'embedding_polygon', LASSO))) == {'w'}

    registry_database(scenario)


def test_rule_1_1_inside_an_element_match_not_in_and_not_equal_take_every_spelling(registry_database):
    async def scenario(database):
        rows = await plain(database)
        for inner in (
            leaf('label', 'not in', ['car']),
            leaf('label', 'not_in', ['car']),
            leaf('label', 'nin', ['car']),
            leaf('label', '!=', 'car'),
            leaf('label', '<>', 'car'),
        ):
            assert await shown(rows, leaf('detections', 'match_elements', inner)) == {'w'}, inner

    registry_database(scenario)


def test_rule_1_7_a_value_of_another_kind_is_a_value_and_built_in_fields_are_unchanged(registry_database):
    """A pin: passes on main too."""

    async def scenario(database):
        rows = await plain(database)
        # `n` holds the text "abc" on `text`: `!=` with a number shows it, as on main (and `v`, with another number).
        for node in (leaf('n', '!=', 2), leaf('n', 'not in', [2])):
            assert {'v', 'text'} <= await shown(rows, node), node
            assert 'w' not in await shown(rows, node), node
        # `colour` holds the number 7 on `text`: `!=` with text shows it.
        assert 'text' in await shown(rows, leaf('colour', '!=', 'red'))
        assert await shown(rows, leaf('id', 'not in', [rows['v']])) == EVERY - {'v'}
        assert await shown(rows, leaf('id', '!=', rows['v'])) == EVERY - {'v'}
        path = await database.fetchval('SELECT filepath FROM samples WHERE id = $1', rows['v'])
        assert await shown(rows, leaf('filepath', '!=', path)) == EVERY - {'v'}
        assert await shown(rows, leaf('media_type', '!=', 'object')) == EVERY
        assert await shown(rows, leaf('created_at', '!=', '2001-01-01')) == EVERY
        assert await shown(rows, NOT(leaf('filepath', 'contains', 'row-0'))) == EVERY - {'v'}

    registry_database(scenario)


def test_rule_2_1_the_standing_alone_wrapping_operators_read_by_the_same_rule(registry_database):
    """cg_self alone reads the row as any filter does; has_matching_object and parent_matches alone read the partner by
    the same rule (no exclusion is dropped for the partner any more)."""

    async def scenario(database):
        await plain(database)
        for node in (leaf('colour', '!=', 'red'), leaf('tags', 'not in', ['rejected']), NOT(eq('n', 1))):
            alone = {'field': '_cgself', 'operator': 'cg_self', 'value': node}
            assert await found(2, alone) == await found(2, node), node
        await add(1, [(None, {'colour': 'red'}), (None, {'colour': 'blue'})], 'lone/img')
        red, blue = await ids(database, 1, 'lone/img')
        await add(1, [('object', {'parent_id': red, 'label': 'car'}), ('object', {'parent_id': blue})], 'lone/obj')
        car, unlabelled = await ids(database, 1, 'lone/obj')
        partner = {'field': '_x', 'operator': 'has_matching_object', 'value': leaf('label', '!=', 'truck')}
        assert await found(1, partner) == {red}  # the unlabelled object has no label
        image = {'field': '_x', 'operator': 'parent_matches', 'value': leaf('colour', '!=', 'blue')}
        assert await found(1, image) == {car}
        assert unlabelled not in await found(1, image)

    registry_database(scenario)


# --- On a dataset with objects, through the page's wrapping: rules 1.5 and 1.6, with and without the record ---

# Dataset 1. The images: name -> (location, tags); each image's objects: name -> (location, tags, point on clip_obj).
IMAGES: dict[str, tuple[Optional[tuple[float, float]], list[str]]] = {
    'A': ((2, 2), ['rejected']),
    'B': (None, ['checked']),
    'C': (None, []),
    'D': ((10, 10), []),
    'E': (None, []),
    'F': ((10, 10), []),
}
OBJECTS: dict[str, tuple[str, Optional[tuple[float, float]], list[str], Optional[tuple[float, float]]]] = {
    'a1': ('A', None, [], (1, 1)),
    'b1': ('B', (3, 3), [], (20, 20)),
    'c1': ('C', (10, 10), [], None),
    'd1': ('D', (3, 3), ['checked'], None),
}


async def coupled(database) -> dict[str, int]:
    names = list(IMAGES)
    await add(1, [(None, {'name': name}) for name in names], 'pair/img')
    by_name = dict(zip(names, await ids(database, 1, 'pair/img')))
    children = list(OBJECTS)
    await add(
        1,
        [('object', {'name': name, 'parent_id': by_name[OBJECTS[name][0]]}) for name in children],
        'pair/obj',
    )
    by_name.update(zip(children, await ids(database, 1, 'pair/obj')))
    for name, (place, tags) in [*IMAGES.items(), *((n, (o[1], o[2])) for n, o in OBJECTS.items())]:
        if place is not None:
            await database.execute(
                'UPDATE samples SET location = ST_SetSRID(ST_MakePoint($2, $3), 4326) WHERE id = $1',
                by_name[name],
                float(place[0]),
                float(place[1]),
            )
        await database.execute('UPDATE samples SET tags = $2 WHERE id = $1', by_name[name], tags)
    await points(database, {by_name[n]: o[3] for n, o in OBJECTS.items() if o[3] is not None}, 'clip_obj')
    return by_name


async def both_ways(monkeypatch, dataset_id: int, node: dict) -> set[int]:
    """The rows with the record, which must be the rows without it."""
    with_record = await found(dataset_id, node)

    async def no_record(source: Any, dataset: int) -> None:
        return None

    real = field_kinds.load
    monkeypatch.setattr(field_kinds, 'load', no_record)
    try:
        assert await found(dataset_id, node) == with_record, node
    finally:
        monkeypatch.setattr(field_kinds, 'load', real)
    return with_record


def test_rule_1_6_through_the_wrapping_the_row_as_a_whole_has_a_location_and_none_is_inside(
    registry_database,
    monkeypatch,
):
    async def scenario(database):
        rows = await coupled(database)
        back = {sample_id: name for name, sample_id in rows.items()}

        async def grid(node: dict) -> set[str]:
            return {back[sample_id] for sample_id in await both_ways(monkeypatch, 1, node)}

        place = leaf('location', 'geo_polygon', SQUARE)
        exclusion = leaf('location', 'geo_polygon_exclude', SQUARE)
        # E has no location, nor any object of it: out (it was in).
        for node in (excluded([place], 'image'), grain([exclusion], 'image'), grain([NOT(place)], 'image')):
            assert await grid(node) == {'C', 'F'}, node
        for node in (excluded([place], 'object'), grain([exclusion], 'object'), grain([NOT(place)], 'object')):
            assert await grid(node) == {'c1'}, node
        # Inside an OR and sharing a filter card, the same rows.
        never = eq('name', 'nobody')
        assert await grid(grain([OR(exclusion, never)], 'image')) == {'C', 'F'}
        assert await grid(grain([AND(exclusion, leaf('name', 'is_not_null'))], 'image')) == {'C', 'F'}
        # A lasso through the wrapping is read as a place: a point on that map (it or a partner), none inside.
        lasso = leaf('clip_obj', 'embedding_polygon', LASSO)
        assert await grid(excluded([lasso], 'image')) == {'B'}
        assert await grid(excluded([lasso], 'object')) == {'b1'}
        assert await grid(grain([NOT(lasso)], 'image')) == {'B'}
        # A polygon that is no polygon is still no filter.
        assert await grid(grain([leaf('location', 'geo_polygon_exclude', [[0, 0], [1, 1]])], 'image')) == set(IMAGES)
        # `location is null` finds what an exclusion no longer shows: the row as a whole has none.
        assert await grid(grain([leaf('location', 'is_null')], 'image')) == {'E'}

    registry_database(scenario)


def test_rule_1_2_a_not_around_the_wrapping_needs_what_the_wrapped_filter_compares(registry_database, monkeypatch):
    """NOT (cg_self(B) OR partner(B)), as the page writes an excluded region: the row as a whole has a value for what
    B compares, and B, read for it with the pair, doesn't hold. B of built-in tests only (an id list) needs nothing:
    the NOT hides an image whose object is listed, as before."""

    async def scenario(database):
        rows = await coupled(database)
        back = {sample_id: name for name, sample_id in rows.items()}

        async def grid(node: dict) -> set[str]:
            return {back[sample_id] for sample_id in await both_ways(monkeypatch, 1, node)}

        listed = leaf('id', 'in', [rows['a1']])
        assert await grid(excluded([listed], 'image')) == set(IMAGES) - {'A'}
        assert await grid(excluded([leaf('id', 'in', [rows['A']])], 'object')) == set(OBJECTS) - {'a1'}
        # A field B compares: the row as a whole needs it (A and B have tags of their own, D through its object).
        tagged = leaf('tags', 'in', ['rejected'])
        assert await grid(excluded([tagged], 'image')) == {'B', 'D'}

    registry_database(scenario)


def test_rule_1_5_through_the_wrapping_a_list_is_answered_by_the_grids_row_with_the_guard(
    registry_database,
    monkeypatch,
):
    async def scenario(database):
        rows = await coupled(database)
        back = {sample_id: name for name, sample_id in rows.items()}

        async def grid(node: dict) -> set[str]:
            return {back[sample_id] for sample_id in await both_ways(monkeypatch, 1, node)}

        for test in (
            leaf('tags', 'not in', ['rejected']),
            NOT(leaf('tags', 'in', ['rejected'])),
            leaf('tags', '!=', 'rejected'),
        ):
            assert await grid(grain([test], 'image')) == {'B'}, test
            assert await grid(grain([test], 'object')) == {'d1'}, test
        # `is null` is read for the image as a whole, as #521 has it: D's object has a tag.
        assert await grid(grain([leaf('tags', 'is_null')], 'image')) == {'C', 'E', 'F'}

    registry_database(scenario)


def test_the_record_never_changes_an_answer_of_the_new_readings(registry_database, monkeypatch):
    """The readings this piece changes, on a dataset with objects, both grids: the same rows with the record as
    without it."""

    async def scenario(database):
        await add(
            1,
            [
                (None, {'colour': 'red', 'detections': [{'label': 'car'}]}),
                (None, {'colour': None}),
                (None, {}),
            ],
            'kinds/img',
        )
        img = await ids(database, 1, 'kinds/img')
        await add(
            1,
            [
                ('object', {'parent_id': img[0], 'label': 'car'}),
                ('object', {'parent_id': img[1], 'label': None}),
                ('object', {'parent_id': img[2]}),
            ],
            'kinds/obj',
        )
        await database.execute(
            'UPDATE samples SET location = ST_SetSRID(ST_MakePoint(2, 2), 4326) WHERE id = $1',
            img[0],
        )
        concept = await database.fetchval(
            'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
            "VALUES (1, 'k', 'clip', 'seed', '[1,0]', 0.5) RETURNING id",
        )
        await database.execute(
            'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, 0.9)',
            concept,
            img[0],
        )
        shapes = [
            [leaf('colour', '!=', 'blue')],
            [NOT(AND(eq('colour', 'red'), eq('label', 'car')))],
            [leaf('label', 'not in', ['truck'])],
            [leaf('location', 'geo_polygon_exclude', SQUARE)],
            [NOT(leaf('location', 'geo_polygon', SQUARE))],
            [leaf('location', 'is_null')],
            [leaf('tags', 'not in', ['x'])],
            [leaf('detections.label', 'is_null')],
            [leaf('detections.label', '!=', 'bus')],
            [concept_test(concept, 'concept_exclude')],
            [NOT(concept_test(concept, 'concept_select'))],
        ]
        for stages in shapes:
            await both_ways(monkeypatch, 1, images(*stages))
            await both_ways(monkeypatch, 1, objects(*stages))

    registry_database(scenario)


# --- The broad scatter's rejected set (embeddings.py) agrees with the general path ---

CONCEPT = {'field': '_concept', 'operator': 'concept_exclude', 'value': {'concept_id': 1, 'threshold': 0.5}}
DUPS = {'field': '_duplicates', 'operator': 'collapse_duplicates', 'value': '_any'}
TAG = {'field': 'tags', 'operator': 'not in', 'value': ['bad']}
GRAIN = {'field': 'media_type', 'operator': '!=', 'value': 'object'}


def conjunction(*nodes: dict) -> dict:
    return {'logic': 'AND', 'filters': list(nodes)}


NOT_BAD = {'logic': 'NOT', 'filters': [{'field': 'tags', 'operator': 'in', 'value': ['bad']}]}


@pytest.mark.parametrize(
    'filters, tagged, fast',
    [
        (CONCEPT, True, True),
        (conjunction(GRAIN, CONCEPT, DUPS), True, True),
        (TAG, True, True),
        (NOT_BAD, True, True),
        (conjunction(GRAIN, CONCEPT, DUPS, TAG), True, True),
        # Most samples untagged: the tags exclusion rejects most of them, so the admission sends it to the general path.
        (TAG, False, False),
        (conjunction(GRAIN, CONCEPT, TAG), False, False),
    ],
)
def test_the_broad_scatter_rejects_the_samples_with_no_tags_or_no_score(
    registry_database,
    monkeypatch,
    filters,
    tagged,
    fast,
):
    async def scenario(db):
        await populate(db, 40)  # dataset 1: samples 1 to 40, points for 1 to 39, concept 1 on clip, `_any` duplicates
        # The tags and scores this test reads, whatever populate leaves. Samples 2 and 3 are tagged `bad`, 4 and 6
        # have no tags (4 a NULL array), and with `tagged` every other sample is tagged `ok`; without it they have
        # none. Concept 1 scores 1 and 2 above the cut, 6 and 7 not at all, every other sample below it. So the
        # exclusions stay broad (with `tagged`) and the fast path answers them.
        await db.execute(
            "UPDATE samples SET tags = CASE WHEN id IN (2, 3) THEN ARRAY['bad'] WHEN id = 4 THEN NULL "
            "WHEN id = 6 OR NOT $1 THEN '{}'::text[] ELSE ARRAY['ok'] END WHERE dataset_id = 1",
            tagged,
        )
        await db.execute('DELETE FROM embedding_concept_scores WHERE concept_id = 1')
        await db.execute(
            'INSERT INTO embedding_concept_scores SELECT 1, id, CASE WHEN id IN (1, 2) THEN 0.9 ELSE 0.2 END '
            'FROM samples WHERE dataset_id = 1 AND id NOT IN (6, 7)',
        )
        request = embeddings.EmbeddingsRequest(filters=filters, sample_pct=100)
        original = embeddings._query_broad_exclusions
        calls = []

        async def optimized(*args):
            answer = await original(*args)
            calls.append(answer is not None)
            return answer

        monkeypatch.setattr(embeddings, '_query_broad_exclusions', optimized)
        actual = await embeddings.get_embeddings_post(1, request)
        assert calls == [fast], 'which path answers'
        monkeypatch.setattr(embeddings, '_query_broad_exclusions', AsyncMock(return_value=None))
        expected = await embeddings.get_embeddings_post(1, request)
        shown_ids = {point['sample_id'] for point in actual['points']}
        assert shown_ids == {point['sample_id'] for point in expected['points']}
        if TAG in filters.get('filters', [filters]) or filters == NOT_BAD:
            assert not shown_ids & {4, 6}, shown_ids  # no tags
            assert not shown_ids & {2, 3}, shown_ids  # tagged bad
        if CONCEPT in filters.get('filters', [filters]):
            assert not shown_ids & {6, 7}, shown_ids  # no score
            assert not shown_ids & {1, 2}, shown_ids  # a score above the cut

    registry_database(scenario)


@pytest.mark.parametrize(
    'scored, passing, fast',
    [
        (range(1, 41), (1, 2), True),  # every point scored, two above the cut: broad
        (range(1, 41), range(1, 21), False),  # 20 of 39 points above the cut: more than half, counted first
        (range(1, 16), (), False),  # 24 of 39 points unscored: the anti-join, cut at the majority, finds it
    ],
)
def test_a_concepts_exclude_that_rejects_most_points_falls_back_and_answers_the_same(
    registry_database,
    monkeypatch,
    scored,
    passing,
    fast,
):
    """A concept's exclude rejecting more than half the map's points (passing scores, or no score) is answered by the
    general path; each way of finding it out answers what the general path answers."""

    async def scenario(db):
        await populate(db, 40)
        await db.execute('DELETE FROM embedding_concept_scores WHERE concept_id = 1')
        await db.execute(
            'INSERT INTO embedding_concept_scores SELECT 1, id, CASE WHEN id = ANY($2::int[]) THEN 0.9 ELSE 0.2 END '
            'FROM samples WHERE dataset_id = 1 AND id = ANY($1::int[])',
            list(scored),
            list(passing),
        )
        request = embeddings.EmbeddingsRequest(filters=CONCEPT, sample_pct=100)
        assert (await embeddings._query_broad_exclusions(1, request) is not None) == fast
        actual = await embeddings.get_embeddings_post(1, request)
        monkeypatch.setattr(embeddings, '_query_broad_exclusions', AsyncMock(return_value=None))
        expected = await embeddings.get_embeddings_post(1, request)
        assert {p['sample_id'] for p in actual['points']} == {p['sample_id'] for p in expected['points']}

    registry_database(scenario)


# --- The one knowledge of what a NOT needs, and the SDK's similarity results ---


class _Fields:
    def builtin(self, field: Any) -> bool:
        return field in ('id', 'filepath', 'media_type', 'created_at', 'last_modified_at')

    def filters_nothing(self, node: Any) -> bool:
        return node.get('operator') == '>' and node.get('value') in (None, '')

    def row_field(self, field: Any) -> Any:
        return 'builtin' if self.builtin(field) else 'list' if field == 'tags' else None


def test_the_needs_of_a_not_are_every_field_it_compares():
    node = AND(
        eq('colour', 'red'),
        OR(leaf('n', '>', 1), leaf('note', 'exists'), leaf('id', '=', 3)),
        NOT(leaf('tags', 'in', ['x'])),
        leaf('location', 'geo_region', 'US-KS'),
        leaf('clip', 'embedding_polygon', LASSO),
        leaf('detections', 'match_elements', eq('label', 'car')),
        concept_test(4, 'concept_exclude'),
        leaf('score', '>', ''),
        {'field': '_x', 'operator': 'cg_self', 'value': eq('label', 'car')},
    )
    assert negation.needs(node, _Fields()) == [
        ('value', 'colour'),
        ('value', 'n'),
        ('value', 'tags'),
        ('location',),
        ('points', 'clip'),
        ('elements', 'detections'),
        ('score', 4),
    ]


class _IdServer:
    """A session that answers a similarity result's question (its filter AND `id in` a page): the page's ids that are
    in `shows`. It records every request."""

    def __init__(self, shows: set[int]):
        self.shows = shows
        self.sent: list[dict] = []

    def post(self, url: str, json: Optional[dict] = None, **_kwargs: Any) -> Any:
        import json as json_module

        import requests

        assert url.endswith('/datasets/1/samples/query') and json is not None
        self.sent.append(json)
        page = json['filters']['filters'][1]['value']
        response = requests.models.Response()
        response.status_code = 200
        response._content = json_module.dumps({'items': [{'id': i} for i in page if i in self.shows]}).encode()
        return response


class _ServedDataset:
    _id = 1

    def __init__(self, session: _IdServer):
        self.client = type('C', (), {'base_url': 'http://t/api', 'session': session})()


def test_rule_2_2_the_sdks_similarity_results_ask_the_server_once_in_pages():
    """A similarity result's filter is read by the server: the filter AND `id in` a page of the results' ids, ids
    only back, once per view; the results keep their order, their distances, and a sample listed twice."""
    from fridge import Condition, F, match_elements
    from fridge.core.sample import Sample
    from fridge.core.view import SimilarityResultView

    samples = []
    for number in range(2500):
        sample = Sample(f'{number}.jpg')
        sample.id = number + 1
        sample.distance = number / 2500
        samples.append(sample)
    samples.append(samples[7])
    server = _IdServer({8, 3, 1200, 2500})
    similar = SimilarityResultView(samples, _ServedDataset(server))
    assert similar.ids() == [*range(1, 2501), 8] and server.sent == []  # no filter: no request
    assert SimilarityResultView([], _ServedDataset(server)).filter(F('colour') != 'red').ids() == []
    assert server.sent == []

    view = similar.filter(F('colour') != 'red')
    assert [sample.id for sample in view] == [3, 8, 1200, 2500, 8]
    assert [sample.distance for sample in view] == [2 / 2500, 7 / 2500, 1199 / 2500, 2499 / 2500, 7 / 2500]
    assert len(server.sent) == 3  # 2,500 ids in pages of 1,000
    for sent in server.sent:
        assert sent['filters']['filters'][0] == {'field': 'colour', 'operator': '!=', 'value': 'red'}
        assert (sent['fields'], sent['slim'], sent['skip_count'], sent['after_id']) == ('id', True, True, 0)
        assert len(sent['filters']['filters'][1]['value']) == sent['limit'] <= 1000
    assert len(view) == 5 and view.first().id == 3 and len(server.sent) == 3  # asked once
    assert view.limit(2).ids() == [3, 8] and len(server.sent) == 6  # a new view asks again
    assert similar.limit(3).ids() == [1, 2, 3] and len(server.sent) == 6  # unfiltered: no request
    server.shows = {3, 2500}
    assert view.filter(F('n') > 1).ids() == [3, 2500] and len(server.sent) == 9  # each filter its own answer
    assert server.sent[-1]['filters']['filters'][0] == {
        'logic': 'AND',
        'filters': [{'field': 'colour', 'operator': '!=', 'value': 'red'}, {'field': 'n', 'operator': '>', 'value': 1}],
    }
    server.shows = {8, 3, 1200, 2500}

    # The similarity distance is no field of the dataset: refused before any request, wherever the filter names it.
    partner = Condition('_crossgrain', 'has_matching_object', {'field': 'distance', 'operator': '<', 'value': 1})
    for condition in (F('distance') < 0.3, ~(F('distance') < 0.3), (F('colour') == 'x') | (F('distance') > 1), partner):
        with pytest.raises(NotImplementedError, match='sample.distance'):
            similar.filter(condition).ids()
    assert len(server.sent) == 9
    # An element's own key named distance is the elements' (the server reads it).
    assert similar.filter(match_elements('vehicles', F('distance') < 30)).ids() == [3, 8, 1200, 2500, 8]
    assert len(server.sent) == 12


# --- Every reader, the filter text, the SDK and the assistant (the HTTP API with real tokens) ---


def test_rule_2_1_every_reader_follows_the_rule(regions_world):
    async def scenario(r: Regions):
        await farm(r)
        # p01: a note of "x"; p02: a note of null; p03 has the note "within" (farm); p21 (no location): tagged; the
        # rest: no note, no tags.
        await r.database.execute(
            'UPDATE samples SET metadata = metadata || \'{"note": "x"}\' WHERE id = $1',
            r.ids['p01'],
        )
        await r.database.execute(
            'UPDATE samples SET metadata = metadata || \'{"note": null}\' WHERE id = $1',
            r.ids['p02'],
        )
        await r.database.execute("UPDATE samples SET tags = ARRAY['ok'] WHERE id = $1", r.ids['p21'])
        vectors = [{'sample_id': sample_id, 'vector': [1.0, 0.0, 0.0, 0.0]} for sample_id in r.ids.values()]
        added = await post(
            r,
            f'/api/datasets/{test_regions_spec.D}/embeddings',
            [{**v, 'field_name': 'clip'} for v in vectors],
        )
        assert added.is_success, added.text
        await loaded(r)
        # `within` is "within" on p01 and "outside" on p02 (farm): `!=` shows p02 only.
        await assert_every_reader(r, leaf('within', '!=', 'within'), {'p02'})
        await assert_every_reader(r, leaf('note', 'not in', ['y']), {'p01', 'p03'})
        await assert_every_reader(r, leaf('tags', 'not in', ['nope']), {'p21'})
        await assert_every_reader(r, drop('XA'), LOCATED - inside('XA'))
        # A NOT (assert_every_reader names a leaf's operator, so the NOT is put in a leaf's place: cg_self alone reads
        # it on the row, as the NOT itself is read).
        for node, names in ((NOT(eq('within', 'within')), {'p02'}), (NOT(keep('XA')), LOCATED - inside('XA'))):
            await assert_every_reader(r, {'field': '_cgself', 'operator': 'cg_self', 'value': node}, names)
        # The exclude ANDed at the top (an anti-join) and nested under OR (each row tested) agree.
        never = eq('plot', 'nobody')
        assert await selected(r, OR(drop('XA'), never)) == r.of(LOCATED - inside('XA'))
        assert await selected(r, AND(drop('XA'), leaf('plot', 'is_not_null'))) == r.of(LOCATED - inside('XA'))

    regions_world(scenario)


def test_rule_1_6_the_filter_text_takes_location_is_null_and_the_old_meaning_written_out(regions_world):
    async def scenario(r: Regions):
        await farm(r)
        await loaded(r)
        unlocated = set(r.ids) - LOCATED
        for text, names in (
            ('location is null', unlocated),
            ('location not exists', unlocated),
            ('location is not null', LOCATED),
            ('location exists', LOCATED),
            ('location not within "XA"', LOCATED - inside('XA')),
            ('location not within "XA" OR location is null', set(r.ids) - inside('XA')),
        ):
            parsed = await nodes(r, text)
            assert await selected(r, {'logic': 'AND', 'filters': parsed}) == r.of(names), text
        written = await post(
            r,
            f'/api/datasets/{test_regions_spec.D}/filter_query/format',
            {'filters': [leaf('location', 'is_null')]},
        )
        assert written.json()['parts'] == ['location is null'], written.text
        refused = await parse(r, 'location = 3')
        assert refused.status_code == 400 and 'is null' in refused.text, refused.text

    regions_world(scenario)


def test_rule_2_1_a_version_the_sdk_and_the_assistant_read_by_the_rule(regions_world, monkeypatch):
    from backend.tests.test_protected_metadata_keys_spec import sdk

    async def scenario(r: Regions):
        await farm(r)
        await loaded(r)
        made = await post(r, f'/api/datasets/{test_regions_spec.D}/versions', {'name': 'v1'})
        assert made.status_code in (200, 201), made.text
        version = await r.database.fetchval(
            'SELECT id FROM dataset_versions WHERE dataset_id = $1',
            test_regions_spec.D,
        )
        answer = await post(
            r,
            f'/api/datasets/{test_regions_spec.D}/versions/{version}/samples/query',
            {'filters': leaf('within', '!=', 'within'), 'limit': 1000},
        )
        assert answer.status_code == 200, answer.text
        assert {item['id'] for item in answer.json()['items']} == {r.ids['p02']}
        loop = asyncio.get_running_loop()

        def script() -> tuple[set[int], set[int], set[int]]:
            from fridge import Condition, Dataset, geo_within

            sdk(r, loop, monkeypatch)
            s = Dataset(f'dataset-{test_regions_spec.D}').samples
            unequal = set(s[s.within != 'within'].ids())
            square = [[0, 0], [5, 0], [5, 5], [0, 5]]
            outside = set(s[~geo_within(square)].ids())
            condition = set(s[Condition('location', 'geo_polygon_exclude', square)].ids())
            return unequal, outside, condition

        unequal, outside, condition = await asyncio.to_thread(script)
        # Within the square's inside (a point on its edge, p03 and p04, is not within it).
        in_square = {
            name
            for name, lon, lat, _ in test_regions_spec.POINTS
            if lon is not None and lat is not None and 0 < lon < 5 and 0 < lat < 5
        }
        assert unequal == {r.ids['p02']}
        assert outside == condition == r.of(LOCATED - in_square)
        await open_door(r)
        made_view = await result(ADMIN, 'make_view', {'dataset': test_regions_spec.D, 'filter': 'within != "within"'})
        assert made_view['count'] == 1, made_view

    regions_world(scenario)


def test_rule_2_2_the_sdks_similarity_results_show_what_the_same_filter_shows(regions_world, monkeypatch):
    """Through the SDK served in-process: a similarity result filtered by a place, its exclusion and a negation shows
    the samples the same filter shows on the dataset, in the results' order, with their distances."""
    from backend.tests.test_protected_metadata_keys_spec import sdk

    async def scenario(r: Regions):
        await farm(r)
        await loaded(r)
        planted = sorted(r.ids.values())
        vectors = [
            {'sample_id': sample_id, 'vector': [1.0, number / 100, 0.0, 0.0], 'field_name': 'clip'}
            for number, sample_id in enumerate(planted)
        ]
        assert (await post(r, f'/api/datasets/{test_regions_spec.D}/embeddings', vectors)).is_success
        loop = asyncio.get_running_loop()

        def script() -> None:
            from fridge import Dataset, F, geo_within

            sdk(r, loop, monkeypatch)
            dataset = Dataset(f'dataset-{test_regions_spec.D}')
            similar = dataset.all().sort_by_similarity(vector=[1.0, 0.0, 0.0, 0.0], limit=100)
            distances = {sample.id: sample.distance for sample in similar}
            assert set(distances) == set(planted)
            square = [[0, 0], [5, 0], [5, 5], [0, 5]]
            few = dataset.all().sort_by_similarity(vector=[1.0, 0.0, 0.0, 0.0], limit=4)  # a strict part of them
            outside_few = False
            for condition in (geo_within(square), ~geo_within(square), F('within') != 'within'):
                anywhere = set(dataset.samples[condition].ids())
                for results in (similar, few):
                    order = {sample.id: sample.distance for sample in results}
                    shown = results.filter(condition).to_list()
                    assert [sample.id for sample in shown] == [i for i in order if i in anywhere], condition
                    assert all(sample.distance == order[sample.id] for sample in shown)
                outside_few = outside_few or bool(anywhere - {sample.id for sample in few})
            assert outside_few  # a filter shows samples beyond the results, which the results don't show

        await asyncio.to_thread(script)

    regions_world(scenario)


def test_concept_preview_remove_keeps_the_below_on_every_dataset(regions_world):
    """concept_preview's counts stay the images above, below and with no score; `remove` then shows `below`."""
    from backend.tests.test_backend_four_spec import CUT, PLAIN, WITH_OBJECTS, grid, planted_images
    from backend.tests.test_backend_four_spec import IMAGES as SCORED

    async def scenario(r: Regions):
        await planted_images(r, PLAIN, objects=False)
        await planted_images(r, WITH_OBJECTS, objects=True)
        await open_door(r)
        below = sum(1 for score, _ in SCORED if score is not None and score <= CUT)
        unscored = sum(1 for score, _ in SCORED if score is None)
        for dataset_id, objects_ in ((PLAIN, False), (WITH_OBJECTS, True)):
            answer = await result(ADMIN, 'concept_preview', {'dataset': dataset_id, 'concept': 'x', 'threshold': CUT})
            assert (answer['below'], answer['unscored']) == (below, unscored), answer
            assert len(await grid(r, dataset_id, answer['remove'], objects_)) == below, dataset_id

    regions_world(scenario)


def test_rule_1_6_an_excluded_region_on_a_dataset_with_objects_through_the_page_and_the_assistant(regions_world):
    """The page's NOT (the image or an object is inside) and the assistant's image_grain read as the row as a whole."""
    from backend.assistant.grain import image_grain

    async def scenario(r: Regions):
        await loaded(r)
        dataset_id = 80
        await r.dataset(dataset_id)
        images_ = [
            {'filepath': 'in.jpg', 'location': {'type': 'Point', 'coordinates': [2.5, 2.5]}},
            {'filepath': 'out.jpg', 'location': {'type': 'Point', 'coordinates': [32, 2.5]}},
            {'filepath': 'none.jpg'},
            {'filepath': 'child-in.jpg'},
        ]
        await datasets.add_samples(dataset_id, {'samples': images_}, user=ADMIN_USER)
        by_path = dict(await r.database.fetch('SELECT filepath, id FROM samples WHERE dataset_id = $1', dataset_id))
        children = [
            {
                'filepath': 'o.jpg',
                'media_type': 'object',
                'metadata': {'parent_id': by_path['child-in.jpg']},
                'location': {'type': 'Point', 'coordinates': [2.5, 2.5]},
            },
            {'filepath': 'p.jpg', 'media_type': 'object', 'metadata': {'parent_id': by_path['none.jpg']}},
        ]
        await datasets.add_samples(dataset_id, {'samples': children}, user=ADMIN_USER)
        expected = {by_path['out.jpg']}  # in: inside; none: no location anywhere; child-in: its object is inside
        assert await selected(r, excluded([keep('XA')], 'image'), dataset_id) == expected
        assert await selected(r, image_grain([drop('XA')], True), dataset_id) == expected

    regions_world(scenario)
