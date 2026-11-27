"""`is null` means no value; an empty list has a test of its own, `is empty` (docs/FILTER_QUERY.md, "Operators").

The implementer's tests, beside the spec's. A value is there and isn't a JSON null, and an empty list written in a
field is a value: the list was looked at and holds nothing. So `is null` shows the rows with no list, `is empty` the
rows whose list is empty, and `is not empty` (the NOT of `is empty`) the rows whose value isn't an empty list. Tags
(`tags`, `@local.tags`) are the exception: their empty list is no value, and there `is empty` and `is not empty` are
`is null` and `is not null`. A list field kept in a column of its own is read as a list in a field is: never set (NULL)
is no value, its empty list is one.
"""

import os
from typing import Any

import pytest
from fastapi import HTTPException
from fridge.core.dataset import _FILTER_UNARY_OPS
from fridge.core.view import FieldExpression

from backend import filter_query, negation
from backend.filter_query import LIST, NUMBER, TEXT, FilterQueryError, format_filter, parse_query
from backend.routers import datasets
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_field_kinds import AND, NOT, OR, eq, found, images, leaf, objects
from backend.tests.test_object_parent_column import add, ids

registry_database = registry_helpers.registry_database
needs_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)

BOX = [0.1, 0.1, 0.2, 0.2]
# The rows of a dataset without objects: `seen` a plain list, `detections` a list of labels, `note` a list on one row
# and text on another (a key not known as a list).
ROWS: dict[str, dict[str, Any]] = {
    'none': {},
    'null': {'seen': None, 'detections': None, 'note': None, 'segmentation': None},
    'empty': {'seen': [], 'detections': [], 'note': [], 'segmentation': {'labels': []}},
    'one': {
        'seen': ['a'],
        'detections': [{'label': 'car', 'bbox': BOX}],
        'note': 'text',
        'segmentation': {'labels': ['a']},
    },
    'nolabel': {'seen': ['b'], 'detections': [{'score': 0.5, 'bbox': BOX}], 'segmentation': {}},
}
EVERY = set(ROWS)
TAGS = {'one': ['t']}
PESTS = {
    'null': [],
    'empty': [],
    'one': ['x'],
}  # a list field kept in a column of its own; 'none' and 'nolabel' never set


async def plain(database) -> dict[str, int]:
    names = list(ROWS)
    await add(2, [(None, ROWS[name]) for name in names], 'plain/row')
    by_name = dict(zip(names, await ids(database, 2, 'plain/row')))
    await datasets.create_array_field(
        2,
        datasets.CreateArrayFieldRequest(field_name='pests'),
        user=registry_helpers.USER,
    )
    for name, tags in TAGS.items():
        await database.execute('UPDATE samples SET tags = $2 WHERE id = $1', by_name[name], tags)
    for name, pests in PESTS.items():
        await database.execute('UPDATE samples SET pests = $2 WHERE id = $1', by_name[name], pests)
    return by_name


async def shown(rows: dict[str, int], node: dict, dataset_id: int = 2) -> set[str]:
    back = {sample_id: name for name, sample_id in rows.items()}
    return {back[sample_id] for sample_id in await found(dataset_id, node)}


# --- On a dataset without objects ---


@needs_database
def test_is_null_shows_the_rows_with_no_list_and_is_empty_those_whose_list_is_empty(registry_database):
    async def scenario(database):
        rows = await plain(database)
        for field in ('seen', 'detections'):
            assert await shown(rows, leaf(field, 'is_null')) == {'none', 'null'}, field
            assert await shown(rows, leaf(field, 'is_not_null')) == {'empty', 'one', 'nolabel'}, field
            assert await shown(rows, leaf(field, 'is_empty')) == {'empty'}, field
            assert await shown(rows, leaf(field, 'is_not_empty')) == {'one', 'nolabel'}, field
            assert await shown(rows, NOT(leaf(field, 'is_empty'))) == {'one', 'nolabel'}, field
            # An empty list exists; no key doesn't (a JSON null under the key counts as there).
            assert await shown(rows, leaf(field, 'exists')) == {'null', 'empty', 'one', 'nolabel'}, field
            assert await shown(rows, leaf(field, 'not_exists')) == {'none'}, field
        # A key not known as a list is read row by row: text is a value that is no empty list.
        assert await shown(rows, leaf('note', 'is_empty')) == {'empty'}
        assert await shown(rows, leaf('note', 'is_not_empty')) == {'one'}
        assert await shown(rows, leaf('note', 'is_null')) == {'none', 'null', 'nolabel'}

    registry_database(scenario)


@needs_database
def test_a_list_of_labels_has_a_value_when_its_list_is_empty_or_holds_a_label(registry_database):
    async def scenario(database):
        rows = await plain(database)
        assert await shown(rows, leaf('detections.label', 'is_null')) == {'none', 'null', 'nolabel'}
        assert await shown(rows, leaf('detections.label', 'is_not_null')) == {'empty', 'one'}
        # A list of labels has no key to find: `exists` and `not exists` are `is not null` and `is null` (rule 1.7).
        assert await shown(rows, leaf('detections.label', 'exists')) == {'empty', 'one'}
        assert await shown(rows, leaf('detections.label', 'not_exists')) == {'none', 'null', 'nolabel'}
        # `segmentation.labels` (a list in an object) the same: it matched no sample with either before.
        assert await shown(rows, leaf('segmentation.labels', 'is_not_null')) == {'empty', 'one'}
        assert await shown(rows, leaf('segmentation.labels', 'exists')) == {'empty', 'one'}
        assert await shown(rows, leaf('segmentation.labels', 'is_null')) == {'none', 'null', 'nolabel'}
        assert await shown(rows, leaf('segmentation.labels', 'not_exists')) == {'none', 'null', 'nolabel'}
        # Inside an element match the tests of an element's own key are untouched.
        unlabelled = leaf('detections', 'match_elements', leaf('label', 'not_exists'))
        assert await shown(rows, unlabelled) == {'nolabel'}
        # A comparison and its NOT show only rows `is not null` shows (unchanged since #524).
        assert await shown(rows, leaf('detections.label', '!=', 'car')) == {'empty'}
        assert await shown(rows, NOT(eq('detections.label', 'car'))) == {'empty'}

    registry_database(scenario)


@needs_database
def test_tags_read_an_empty_list_as_no_value_and_a_list_column_as_a_list_in_a_field(registry_database):
    async def scenario(database):
        rows = await plain(database)
        untouched = await shown(rows, leaf('tags', 'is_null'))
        assert untouched == EVERY - {'one'}
        assert await shown(rows, leaf('tags', 'is_not_null')) == {'one'}
        assert await shown(rows, leaf('tags', 'is_empty')) == untouched
        assert await shown(rows, leaf('tags', 'is_not_empty')) == {'one'}
        # As `is null` in every respect: inside a NOT it adds no field.
        assert await shown(rows, NOT(leaf('tags', 'is_empty'))) == {'one'}
        assert await shown(rows, NOT(leaf('tags', 'is_not_empty'))) == untouched
        assert await shown(rows, NOT(AND(leaf('tags', 'is_empty'), eq('seen', 'a')))) == {'empty', 'one', 'nolabel'}
        # A list field kept in a column of its own: never set is no value, the empty list is one.
        assert await shown(rows, leaf('pests', 'is_null')) == {'none', 'nolabel'}
        assert await shown(rows, leaf('pests', 'is_not_null')) == {'null', 'empty', 'one'}
        assert await shown(rows, leaf('pests', 'is_empty')) == {'null', 'empty'}
        assert await shown(rows, leaf('pests', 'is_not_empty')) == {'one'}
        # A comparison inside a NOT: it needs a value, of both fields.
        assert await shown(rows, NOT(leaf('pests', 'is_empty'))) == {'one'}
        assert await shown(rows, NOT(leaf('pests', 'is_not_empty'))) == {'null', 'empty'}
        assert await shown(rows, NOT(AND(leaf('pests', 'is_empty'), eq('seen', 'a')))) == {'empty', 'one'}

    registry_database(scenario)


@needs_database
def test_every_row_is_in_exactly_one_of_null_empty_and_not_empty_and_of_null_and_not_null(registry_database):
    async def scenario(database):
        rows = await plain(database)
        for field in ('seen', 'detections', 'note', 'detections.label', 'tags', 'pests', 'id'):
            null = await shown(rows, leaf(field, 'is_null'))
            valued = await shown(rows, leaf(field, 'is_not_null'))
            assert null | valued == EVERY and not null & valued, field
            value = 1 if field == 'id' else 'a'
            for comparison in (eq(field, value), leaf(field, '!=', value), NOT(eq(field, value))):
                assert not (await shown(rows, comparison)) & null, (field, comparison)
        for field in ('seen', 'detections', 'note', 'pests'):
            parts = [await shown(rows, leaf(field, op)) for op in ('is_null', 'is_empty', 'is_not_empty')]
            assert set().union(*parts) == EVERY and sum(map(len, parts)) == len(EVERY), field

    registry_database(scenario)


@needs_database
def test_is_empty_is_refused_where_no_list_can_be_and_on_a_path_through_a_list(registry_database):
    async def scenario(database):
        rows = await plain(database)
        for field in ('id', 'filepath', 'media_type', 'created_at', 'last_modified_at', 'location', 'embeddings.clip'):
            with pytest.raises(HTTPException) as refused:
                await shown(rows, leaf(field, 'is_empty'))
            assert refused.value.status_code == 400
            assert f"'{field}' holds no list" in refused.value.detail, refused.value.detail
        for operator in ('is_empty', 'is_not_empty'):
            with pytest.raises(HTTPException) as refused:
                await shown(rows, leaf('detections.label', operator))
            words = operator.replace('_', ' ')
            assert refused.value.status_code == 400
            assert f'ask it of the list, detections {words}' in refused.value.detail, refused.value.detail
        # `segmentation.labels` is a list in an object, not a path through a list: asked, not refused.
        assert await shown(rows, leaf('segmentation.labels', 'is_empty')) == {'empty'}
        assert await shown(rows, leaf('segmentation.labels', 'is_not_empty')) == {'one'}

    registry_database(scenario)


# --- Through the page's wrapping on a dataset with objects (#521's reading, the same rows asked) ---

IMAGES: dict[str, dict[str, Any]] = {
    'i-empty': {'seen': []},
    'i-one': {'seen': ['a'], 'colour': 'red'},
    'i-none': {},
    'i-alone': {'seen': []},
}
OBJECTS: dict[str, tuple[str, dict[str, Any]]] = {
    'o-empty': ('i-empty', {'parts': [], 'boxes': []}),
    'o-one': ('i-empty', {'parts': ['x'], 'boxes': [{'label': 'car', 'bbox': BOX}]}),
    'o-none': ('i-one', {}),
    'o-empty2': ('i-none', {'parts': [], 'boxes': [{'score': 1, 'bbox': BOX}]}),
}


@needs_database
def test_through_the_wrapping_is_empty_is_a_test_and_is_not_empty_its_not(registry_database):
    async def scenario(database):
        names = list(IMAGES)
        await add(1, [(None, {'name': name, **IMAGES[name]}) for name in names], 'pair/img')
        rows = dict(zip(names, await ids(database, 1, 'pair/img')))
        children = list(OBJECTS)
        await add(
            1,
            [('object', {'parent_id': rows[OBJECTS[name][0]], **OBJECTS[name][1]}) for name in children],
            'pair/obj',
        )
        rows.update(zip(children, await ids(database, 1, 'pair/obj')))

        async def grids(node: dict) -> tuple[set[str], set[str]]:
            return await shown(rows, images(node), 1), await shown(rows, objects(node), 1)

        # A list of an image.
        assert await grids(leaf('seen', 'is_empty')) == ({'i-empty', 'i-alone'}, {'o-empty', 'o-one'})
        assert await grids(leaf('seen', 'is_not_empty')) == ({'i-one'}, {'o-none'})
        assert await grids(leaf('seen', 'is_null')) == ({'i-none'}, {'o-empty2'})
        # A list of an object: the image as a whole is empty when one of its objects is.
        assert await grids(leaf('parts', 'is_empty')) == ({'i-empty', 'i-none'}, {'o-empty', 'o-empty2'})
        assert await grids(leaf('parts', 'is_not_empty')) == (set(), {'o-one'})
        assert await grids(NOT(leaf('parts', 'is_empty'))) == (set(), {'o-one'})
        assert await grids(leaf('parts', 'is_null')) == ({'i-one', 'i-alone'}, {'o-none'})
        # A list of labels of an object: `exists` and `not exists` are its `is not null` and `is null`, through the
        # wrapping as on one row.
        no_label = ({'i-one', 'i-none', 'i-alone'}, {'o-none', 'o-empty2'})
        assert await grids(leaf('boxes.label', 'is_null')) == no_label
        assert await grids(leaf('boxes.label', 'not_exists')) == no_label
        assert await grids(leaf('boxes.label', 'exists')) == await grids(leaf('boxes.label', 'is_not_null'))
        assert await shown(rows, leaf('boxes.label', 'not_exists'), 1) == {'o-none', 'o-empty2', *IMAGES}
        assert await grids(OR(leaf('parts', 'is_empty'), eq('colour', 'red'))) == (
            {'i-empty', 'i-none', 'i-one'},
            {'o-empty', 'o-empty2', 'o-none'},
        )

    registry_database(scenario)


@needs_database
def test_through_the_wrapping_not_is_empty_of_a_list_column_is_its_is_not_empty_by_the_grids_own_row(
    registry_database,
):
    """Section 9.10: `is not empty` of a list field kept in a column of its own, written so or as `NOT (f is empty)`,
    is answered by the grid's own row: a list with at least one element there. A partner's list counts for neither."""

    async def scenario(database):
        await datasets.create_array_field(
            1,
            datasets.CreateArrayFieldRequest(field_name='pests'),
            user=registry_helpers.USER,
        )
        await add(1, [(None, {'name': 'i-never'}), (None, {'name': 'i-x'})], 'wrap/img')
        image_never, image_x = await ids(database, 1, 'wrap/img')
        await add(
            1,
            [
                ('object', {'parent_id': image_never}),  # holds ['x'] under an image never set
                ('object', {'parent_id': image_x}),  # holds [] under an image holding ['x']
                ('object', {'parent_id': image_x}),  # never set under an image holding ['x']
            ],
            'wrap/obj',
        )
        object_x, object_empty, object_never = await ids(database, 1, 'wrap/obj')
        await database.execute('UPDATE samples SET pests = $2 WHERE id = $1', image_x, ['x'])
        await database.execute('UPDATE samples SET pests = $2 WHERE id = $1', object_x, ['x'])
        await database.execute('UPDATE samples SET pests = $2 WHERE id = $1', object_empty, [])
        rows = {
            'i-never': image_never,
            'i-x': image_x,
            'o-x': object_x,
            'o-empty': object_empty,
            'o-never': object_never,
        }
        assert await shown(rows, leaf('pests', 'is_null'), 1) == {'i-never', 'o-never'}
        for node in (NOT(leaf('pests', 'is_empty')), leaf('pests', 'is_not_empty')):
            assert await shown(rows, images(node), 1) == {'i-x'}, node
            assert await shown(rows, objects(node), 1) == {'o-x'}, node

    registry_database(scenario)


# --- The text, the NOT's needs and the SDK (no database) ---

KINDS = {'seen': LIST, 'detections': LIST, 'detections.label': TEXT, 'tags': LIST, 'score': NUMBER, 'empty': TEXT}
PATHS = {'detections.label': 'detections'}


def parsed(text: str) -> list[dict]:
    return parse_query(text, {**KINDS, 'id': NUMBER, 'filepath': TEXT}, paths=PATHS)


def test_the_text_takes_is_empty_and_is_not_empty_and_writes_them_back():
    for text, operator in (('seen is empty', 'is_empty'), ('seen is not empty', 'is_not_empty')):
        [node] = parsed(text)
        assert node == {'field': 'seen', 'operator': operator, 'value': None}
        assert format_filter(node) == text
    [node] = parsed('NOT (seen is empty) OR (tags is not empty AND score > 1)')
    assert format_filter(node) == '(NOT (seen is empty) OR (tags is not empty AND score > 1))'
    assert parsed('seen IS NOT EMPTY') == parsed('seen is not empty')
    # A field named empty, and a value written empty, are read as they were.
    assert parsed('empty = empty') == [{'field': 'empty', 'operator': '=', 'value': 'empty'}]
    assert parsed('empty is empty') == [{'field': 'empty', 'operator': 'is_empty', 'value': None}]


def test_the_text_refuses_is_empty_where_the_server_does():
    with pytest.raises(FilterQueryError) as refused:
        parsed('detections.label is empty')
    assert refused.value.detail['message'].endswith('ask it of the list, detections is empty')
    assert refused.value.detail['suggestions'] == ['detections']
    for field in ('id', 'filepath'):
        with pytest.raises(FilterQueryError) as refused:
            parsed(f'{field} is not empty')
        assert f"'{field}' holds no list" in refused.value.detail['message']
    with pytest.raises(FilterQueryError) as refused:
        parse_query('location is empty', KINDS)
    assert "'location' holds no list" in refused.value.detail['message']
    with pytest.raises(FilterQueryError) as refused:
        parse_query('concept "x" is not empty', KINDS, concepts=[filter_query.Concept(1, 'x', 0.5)])
    assert "a concept's score is no list" in refused.value.detail['message']
    with pytest.raises(FilterQueryError) as refused:
        parsed('seen is nothing')
    assert 'null, not null, empty or not empty' in refused.value.detail['message']
    with pytest.raises(FilterQueryError) as refused:
        parsed('score > 1 AND seen')
    assert 'is empty, is not empty' in refused.value.detail['message']
    assert 'is empty' in filter_query.OPERATORS_BY_KIND[NUMBER]


class _Fields:
    def builtin(self, field: Any) -> bool:
        return field == 'id'

    def filters_nothing(self, node: Any) -> bool:
        return False

    def row_field(self, field: Any) -> Any:
        return 'builtin' if field == 'id' else 'list' if field in ('tags', 'pests') else None


def test_a_not_around_is_empty_needs_a_value_except_of_tags():
    node = AND(
        leaf('seen', 'is_empty'),
        leaf('parts', 'is_not_empty'),
        leaf('tags', 'is_empty'),
        leaf('pests', 'is_empty'),
        leaf('n', 'is_null'),
    )
    assert negation.needs(node, _Fields()) == [('value', 'seen'), ('value', 'parts'), ('value', 'pests')]


def test_the_sdk_has_a_condition_for_each_new_test():
    assert FieldExpression('seen').is_empty().to_dict() == {'field': 'seen', 'operator': 'is_empty', 'value': None}
    assert FieldExpression('seen').is_not_empty().to_dict() == {
        'field': 'seen',
        'operator': 'is_not_empty',
        'value': None,
    }
    assert _FILTER_UNARY_OPS['is_empty'] == ('is_empty', 'is_not_empty')


def test_nothing_of_the_old_list_reading_is_left():
    assert not hasattr(datasets._GrainRows, 'valued')
    assert not hasattr(datasets, '_nonempty_list_sql')
