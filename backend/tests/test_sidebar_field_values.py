"""The sidebar's values of a field (docs/FILTER_QUERY.md, "A field's values"): on a dataset with objects they come from
the rows that have the field for the view, either kind, read as the grids read a row with its partner; and a field too
varied to count in time still answers some of its values.

The world: six images (`colour`, `region`: images only; `score`: both kinds) and ten objects (`label`, `occluded`,
`weight`: objects only), three of the objects with no image. Each reader is checked against the grid itself, through
the samples query: every value offered, added to the view as `f = v` (a stage beside the others, as the page applies
a tick), shows a row, and a complete answer misses no value that would.
"""

import asyncio
import json
import os
import time
from typing import Any

import pytest

from backend import field_kinds
from backend.routers import datasets
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_kinds import AND, NOT, OR, eq, found, images, leaf, objects
from backend.tests.test_object_parent_column import add, ids

registry_database = registry_helpers.registry_database
USER = registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
IMAGES: list[dict[str, Any]] = [
    {'colour': 'red', 'score': 0.9, 'region': 1},
    {'colour': 'red', 'region': 2},
    {'colour': 'blue', 'region': 3},
    {},
    {'colour': 'red'},
    {'colour': 'green'},
]
# (image index or None, metadata)
OBJECTS: list[tuple[Any, dict[str, Any]]] = [
    (0, {'label': 'car', 'occluded': True, 'score': 0.5, 'weight': 10}),
    (0, {'label': 'truck', 'occluded': False, 'weight': 20}),
    (1, {'label': 'truck', 'occluded': True, 'weight': 30}),
    (2, {'label': 'car', 'occluded': False, 'score': 0.7, 'weight': 40}),
    (3, {'label': 'car', 'weight': 50}),
    (5, {'label': 'person', 'occluded': True, 'weight': 60}),
    (5, {'label': 'cat', 'occluded': False, 'score': 0.2, 'weight': 70}),
    (None, {'label': 'bus', 'occluded': True, 'weight': 80}),
    (None, {'label': 'car', 'weight': 90}),
    (None, {'label': 'van', 'occluded': False, 'weight': 100}),
]
KINDS = {'label': str, 'occluded': bool, 'weight': float, 'colour': str, 'region': float, 'score': float}
BASES: dict[str, list] = {
    'nothing': [],
    'colour = red': [eq('colour', 'red')],
    'label = car': [eq('label', 'car')],
    'NOT (label = car)': [NOT(eq('label', 'car'))],
    'label != car': [leaf('label', '!=', 'car')],
    'colour = red AND label = car': [eq('colour', 'red'), eq('label', 'car')],
    'occluded = true': [eq('occluded', True)],
    'colour = red AND NOT (label = car)': [eq('colour', 'red'), NOT(eq('label', 'car'))],
    'label = car OR colour = green': [OR(eq('label', 'car'), eq('colour', 'green'))],
    'label is null': [leaf('label', 'is_null')],
    'colour is null': [leaf('colour', 'is_null')],
    'score > 0.6': [leaf('score', '>', 0.6)],
    'a filter card: AND (colour = red)': [AND(eq('colour', 'red'))],
}


async def world(database) -> None:
    await add(1, [(None, {**metadata, 'img': f'img{n}'}) for n, metadata in enumerate(IMAGES)], 'g12/img')
    image_ids = await ids(database, 1, 'g12/img')
    await add(
        1,
        [
            ('object', {**metadata, **({'parent_id': image_ids[image]} if image is not None else {})})
            for image, metadata in OBJECTS
        ],
        'g12/obj',
    )


def view(grid: str, base: list) -> dict:
    return images(*base) if grid == 'image' else objects(*base)


def typed(field: str, text: str) -> Any:
    kind = KINDS[field]
    return text == 'true' if kind is bool else kind(text)


async def suggest(field: str, node: Any, q: str = '') -> dict:
    return await datasets.autocomplete_field_post(1, field, datasets.AutocompleteRequest(q=q, filters=node))


async def every_value(field: str) -> list[str]:
    return [entry['value'] for entry in (await suggest(field, None))['values']]


async def showing(field: str, grid: str, base: list) -> set[str]:
    """The values of the field whose tick (a stage beside the view's) shows a row of the grid."""
    return {
        text for text in await every_value(field) if await found(1, view(grid, [*base, eq(field, typed(field, text))]))
    }


def test_the_values_of_a_field_follow_the_grid_both_ways_for_each_reader(registry_database):
    """Rule 1.3's property against the view as sent, for the suggestions, the list (lightning off and the opened
    field's facet) and the range, under both kinds."""

    async def scenario(database):
        await world(database)
        await run_refresh(database, 1)
        problems: list[tuple] = []
        for grid in ('image', 'object'):
            for name, base in BASES.items():
                node = view(grid, base)
                eager = {entry['name']: entry for entry in await datasets._get_dataset_field_stats_impl(1, node, False)}
                for field in KINDS:
                    expected = await showing(field, grid, base)
                    answer = await suggest(field, node)
                    offered = {entry['value'] for entry in answer['values']}
                    if offered != expected or answer['exhaustive'] is not True:
                        problems.append((grid, name, field, 'suggestions', sorted(offered), sorted(expected)))
                    facet = (await datasets._get_dataset_field_stats_impl(1, node, True, [field]))[0]
                    for reader, entry in (('list', eager[field]), ('opened', facet)):
                        if KINDS[field] is str and set(entry.get('values') or []) != expected:
                            problems.append((grid, name, field, reader, entry.get('values'), sorted(expected)))
                        if KINDS[field] is float and expected:
                            numbers = sorted(float(text) for text in expected)
                            if (entry.get('min'), entry.get('max')) != (numbers[0], numbers[-1]):
                                problems.append((grid, name, field, reader, entry.get('min'), entry.get('max')))
                        if KINDS[field] is float and not expected:
                            # No value for the view: no range, the view's own answer (not the dataset's, not kept).
                            answer_range = (entry.get('min'), entry.get('max'), entry.get('provenance'))
                            if answer_range != (None, None, 'exact'):
                                problems.append((grid, name, field, reader, answer_range))
        assert not problems, problems

    registry_database(scenario)


def test_objects_with_no_image_count_under_the_objects_kind_only(registry_database):
    async def scenario(database):
        await world(database)
        under_images = {entry['value']: entry['count'] for entry in (await suggest('label', images()))['values']}
        assert under_images == {'car': 3, 'truck': 2, 'person': 1, 'cat': 1}
        under_objects = {entry['value']: entry['count'] for entry in (await suggest('label', objects()))['values']}
        assert under_objects == {'car': 4, 'truck': 2, 'person': 1, 'cat': 1, 'bus': 1, 'van': 1}
        weight = (await datasets._get_dataset_field_stats_impl(1, images(), False, ['weight']))[0]
        assert (weight['min'], weight['max'], weight['provenance']) == (10.0, 70.0, 'exact')

    registry_database(scenario)


def test_with_nothing_filtered_an_objects_image_is_its_parent_id_without_a_join(registry_database):
    """The images' kind with nothing filtered asks nothing of an object's image but that it exists: read from the
    object's parent_id, which the triggers set only to a non-object sample of its dataset. An object naming another
    object, an image of another dataset, or no sample at all is no image's partner, as the images' grid shows."""

    async def scenario(database):
        await world(database)
        await add(2, [(None, {'colour': 'grey'})], 'other/img')
        (elsewhere,) = await ids(database, 2, 'other/img')
        (an_object, *_) = await ids(database, 1, 'g12/obj')
        strays = [an_object, elsewhere, 999999]
        await add(
            1,
            [('object', {'label': f'kite{n}', 'parent_id': parent}) for n, parent in enumerate(strays)],
            'g12/stray',
        )
        source, _ = datasets.field_value_source(
            datasets.ValueView('image', None, []),
            images(),
            'label',
            1,
            [1],
            2,
            value="metadata->>'label'",
            present="metadata->>'label' IS NOT NULL",
            numeric_indexed_fields=set(),
            array_columns={'tags'},
        )
        assert 'samples.parent_id IS NOT NULL' in source and 'EXISTS' not in source
        under_images = {entry['value']: entry['count'] for entry in (await suggest('label', images()))['values']}
        assert under_images == {'car': 3, 'truck': 2, 'person': 1, 'cat': 1}
        assert not await found(1, images(eq('label', 'kite0')))
        under_objects = {entry['value'] for entry in (await suggest('label', objects()))['values']}
        assert {'kite0', 'kite1', 'kite2'} <= under_objects

    registry_database(scenario)


def test_a_reading_that_fails_never_calls_its_values_complete(registry_database, monkeypatch):
    """An unexpected error compiling the view's reading: the dataset's values, without counts and not complete (the
    kept list's form), never the dataset's values called the view's."""

    async def scenario(database):
        await world(database)

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError('a reading made to fail')

        monkeypatch.setattr(datasets, 'field_value_source', broken)
        answer = await suggest('label', images(eq('colour', 'blue')))
        assert answer['exhaustive'] is False
        assert answer['values'] and all(entry['count'] is None for entry in answer['values'])
        assert {entry['value'] for entry in answer['values']} == {'car', 'truck', 'person', 'cat', 'bus', 'van'}

    registry_database(scenario)


def test_a_range_the_view_has_no_value_for_is_no_range(registry_database):
    """img4 has no object, no region and no score: under a view of it alone the three ranges are null, the view's own
    answer, for the list (lightning off) and the opened field; the dataset's range (weight 10 to 100) is not given."""

    async def scenario(database):
        await world(database)
        await run_refresh(database, 1)
        node = images(eq('img', 'img4'))
        eager = {entry['name']: entry for entry in await datasets._get_dataset_field_stats_impl(1, node, False)}
        for field in ('weight', 'region', 'score'):
            facet = (await datasets._get_dataset_field_stats_impl(1, node, True, [field]))[0]
            for entry in (eager[field], facet):
                assert (entry['min'], entry['max'], entry['provenance']) == (None, None, 'exact'), (field, entry)

    registry_database(scenario)


def test_a_range_the_view_has_no_value_for_is_no_range_on_every_dataset(registry_database):
    """The same on a dataset without objects, as the page sends its view (the kind predicate and the stages), and for a
    request with no kind on either dataset. A range fast mode doesn't compute for the filter keeps the dataset's,
    labelled so."""

    async def scenario(database):
        await world(database)
        await add(2, [(None, {'split': 'train', 'n': n}) for n in (5, 7)] + [(None, {'split': 'test'})], 'plain')
        await run_refresh(database)
        test_split = [eq('split', 'test')]
        views = [
            (2, AND(leaf('media_type', '!=', 'object'), *test_split)),  # the page's view without objects
            (2, AND(*test_split)),  # no kind
            (1, AND(eq('img', 'img4'))),  # no kind, on the dataset with objects
        ]
        for dataset, node in views:
            field = 'n' if dataset == 2 else 'weight'
            eager = {
                entry['name']: entry for entry in await datasets._get_dataset_field_stats_impl(dataset, node, False)
            }
            facet = (await datasets._get_dataset_field_stats_impl(dataset, node, True, [field]))[0]
            for entry in (eager[field], facet):
                assert (entry['min'], entry['max'], entry['provenance']) == (None, None, 'exact'), (node, entry)
        # Lightning's sidebar request computes no facet for a stage: the dataset's range, not called the view's.
        sidebar = {entry['name']: entry for entry in await datasets._get_dataset_field_stats_impl(2, views[0][1], True)}
        assert (sidebar['n']['min'], sidebar['n']['max']) == (5.0, 7.0)
        assert sidebar['n']['provenance'] != 'exact'

    registry_database(scenario)


def test_a_number_counts_the_rows_that_have_the_value(registry_database):
    """Objects for an object's field under the images' kind; images, each once, for an image's field under the objects'
    kind; both for a field both kinds have."""

    async def scenario(database):
        await world(database)
        labels = {
            entry['value']: entry['count'] for entry in (await suggest('label', images(eq('colour', 'red'))))['values']
        }
        assert labels == {'truck': 2, 'car': 1}  # the objects of img0 and img1, not the two images
        colours = {entry['value']: entry['count'] for entry in (await suggest('colour', objects()))['values']}
        assert colours == {'red': 2, 'blue': 1, 'green': 1}  # img0 has two objects in the view, counted once
        scores = {entry['value']: entry['count'] for entry in (await suggest('score', images()))['values']}
        assert scores == {'0.9': 1, '0.5': 1, '0.7': 1, '0.2': 1}

    registry_database(scenario)


def test_the_fields_own_rule_is_kept(registry_database):
    """A tick is applied as a stage beside the field's applied rule: a value offered must show a row with it."""

    async def scenario(database):
        await world(database)
        assert [entry['value'] for entry in (await suggest('label', images(eq('label', 'car'))))['values']] == ['car']
        # A request with no kind that carries a filter (a script): kept too.
        assert [entry['value'] for entry in (await suggest('label', AND(eq('label', 'car'))))['values']] == ['car']
        await add(2, [(None, {'split': 'train'}), (None, {'split': 'test'})], 'plain/img')
        # As the page sends it on a dataset without objects: the kind predicate and the stages, unwrapped.
        plain = AND(leaf('media_type', '!=', 'object'), eq('split', 'train'))
        answer = await datasets.autocomplete_field_post(2, 'split', datasets.AutocompleteRequest(filters=plain))
        assert [entry['value'] for entry in answer['values']] == ['train']
        no_kind = AND(eq('split', 'train'))
        answer = await datasets.autocomplete_field_post(2, 'split', datasets.AutocompleteRequest(filters=no_kind))
        assert [entry['value'] for entry in answer['values']] == ['train']

    registry_database(scenario)


def test_a_part_left_out_by_the_record_is_guarded(registry_database, monkeypatch):
    """Under the images' kind with nothing filtered the images' part of `label` is left out (no image has one); the
    first `label` an image gets is found at once, with the record from before: the statement is refused and read
    again."""

    async def scenario(database):
        await world(database)
        before = await field_kinds.load(database, 1)
        assert before is not None and not before.seen('label', 'image')
        (img0, *_) = await ids(database, 1, 'g12/img')
        await datasets.update_sample(1, img0, datasets.SampleUpdate(metadata={'label': 'kite'}), user=USER)
        loads: list[int] = []

        async def stale(source: Any, dataset_id: int) -> Any:
            loads.append(dataset_id)
            return before

        monkeypatch.setattr(field_kinds, 'load', stale)
        offered = {entry['value'] for entry in (await suggest('label', images()))['values']}
        assert 'kite' in offered and 'car' in offered
        assert len(loads) == 2  # refused twice, read the third time without the record

    registry_database(scenario)


def test_a_field_too_varied_to_count_answers_some_values_then_all_or_the_kept_list(registry_database, monkeypatch):
    """The counted reading's limit at zero: the uncounted reading answers. Its rows' limit made small, so the five
    values are more than it reads. Both limits at zero: the kept list."""

    async def scenario(database):
        names = ['b-5', 'B-4', 'a-3', 'Z-2', 'c-1']
        await add(1, [(None, {'name': name, 'n': k}) for k, name in enumerate(names)], 'many')
        await run_refresh(database, 1)
        counted = await suggest('name', None)
        assert counted == {
            'values': [{'value': v, 'count': 1} for v in ['B-4', 'Z-2', 'a-3', 'b-5', 'c-1']],
            'exhaustive': True,
        }

        monkeypatch.setattr(datasets, '_AUTOCOMPLETE_BUDGET_S', 0)
        assert await suggest('name', None) == counted  # every row read: the counted answer exactly
        monkeypatch.setattr(datasets, '_AUTOCOMPLETE_UNCOUNTED_ROWS', 3)
        some = await suggest('name', None)
        assert some['exhaustive'] is False and all(entry['count'] is None for entry in some['values'])
        values = [entry['value'] for entry in some['values']]
        assert len(values) == 3 and set(values) <= set(names) and values == sorted(values, key=str.encode)
        assert (await suggest('name', None, q='-'))['exhaustive'] is False
        assert await suggest('name', None, q='zzz') == {'values': [], 'exhaustive': True}  # read to the end: none

        monkeypatch.setattr(datasets, '_AUTOCOMPLETE_UNCOUNTED_BUDGET_S', 0)
        kept = await suggest('name', None)
        assert kept['exhaustive'] is False
        assert [entry['value'] for entry in kept['values']] == sorted(names)
        assert all(entry['count'] is None for entry in kept['values'])

    registry_database(scenario)


async def post_raw(path: str, body: dict, gone_after: Any) -> tuple[int, bytes]:
    """One raw ASGI request through the real app (its HTTP middleware hides disconnects from is_disconnected()), whose
    client disconnects `gone_after` seconds after sending it, or stays (None)."""
    from backend.main import app

    messages = [{'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}]
    sent: list[dict] = []

    async def receive() -> Any:
        if messages:
            return messages.pop(0)
        if gone_after is None:
            await asyncio.Event().wait()
        await asyncio.sleep(gone_after)
        return {'type': 'http.disconnect'}

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': '1.1',
        'method': 'POST',
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode(),
        'query_string': b'',
        'root_path': '',
        'headers': [(b'content-type', b'application/json'), (b'host', b'test')],
        'client': ('127.0.0.1', 1),
        'server': ('test', 80),
    }
    await app(scope, receive, send)
    status = next(message['status'] for message in sent if message['type'] == 'http.response.start')
    return status, b''.join(message.get('body', b'') for message in sent if message['type'] == 'http.response.body')


def test_a_suggestions_request_the_client_gave_up_is_stopped(registry_database, monkeypatch):
    """The picker gives its request up on the next keystroke: the server cancels the statement and returns its
    connection then, not at the end of the counted and uncounted readings' limits."""
    from backend.access import require_dataset_access
    from backend.main import app

    async def scenario(database):
        await world(database)
        monkeypatch.setitem(app.dependency_overrides, require_dataset_access, lambda: None)
        original = database.fetch

        async def fetch(sql: str, *args: Any) -> Any:
            if 'AS cnt' in sql:  # the counted reading, made slow
                return await original("SELECT 'car' AS val, 1::bigint AS cnt FROM pg_sleep(5) gone")
            return await original(sql, *args)

        monkeypatch.setattr(database, 'fetch', fetch)
        body = {'q': '', 'limit': 100, 'filters': images()}
        started = time.monotonic()
        status, _ = await post_raw('/api/datasets/1/fields/label/autocomplete', body, 0.3)
        elapsed = time.monotonic() - started
        assert status == 499
        assert elapsed < 1.5, elapsed  # not the counted reading's 2 s
        assert database.pool.get_idle_size() == database.pool.get_size()
        running = await database.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND state <> 'idle' "
            "AND query LIKE '%pg_sleep(5) gone%'",
        )
        assert running == 0
        # A client that stays gets its answer.
        monkeypatch.setattr(database, 'fetch', original)
        status, payload = await post_raw('/api/datasets/1/fields/label/autocomplete', body, None)
        assert status == 200
        assert {entry['value'] for entry in json.loads(payload)['values']} == {'car', 'truck', 'person', 'cat'}

    registry_database(scenario)


def test_equal_counts_come_in_the_order_of_the_value_in_c(registry_database):
    async def scenario(database):
        await add(1, [(None, {'tag': tag}) for tag in ['b', 'a', 'B', 'a', 'Z', 'é', 'e']], 'tags')
        answer = await suggest('tag', None)
        assert [(entry['value'], entry['count']) for entry in answer['values']] == [
            ('a', 2),
            ('B', 1),
            ('Z', 1),
            ('b', 1),
            ('e', 1),
            ('é', 1),
        ]

    registry_database(scenario)


def test_a_list_field_never_set_or_empty_gives_no_value_under_either_kind(registry_database):
    """A list field kept in a column of its own (`parts`) holds a list, the empty list (a value with no element to
    offer) or nothing (never set). Images: a never set with an object ['door'], b [] with an object [], c ['wheel'] with
    an object never set, d never set with no object; an object ['roof'] with no image. Each reader offers only elements,
    and follows the grid both ways, under either kind, for views that test the list's emptiness."""

    async def scenario(database):
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='parts'), user=USER)
        lists: dict[str, Any] = {'a': None, 'b': [], 'c': ['wheel'], 'd': None}
        images_added = [
            {'filepath': f'parts/{name}.jpg', **({'array_fields': {'parts': parts}} if parts is not None else {})}
            for name, parts in lists.items()
        ]
        image_ids = (await datasets.add_samples(1, {'samples': images_added}, user=USER))['ids']
        parent = dict(zip(lists, image_ids))
        objects_added = [
            {
                'filepath': 'parts/a-obj.jpg',
                'media_type': 'object',
                'metadata': {'parent_id': parent['a']},
                'array_fields': {'parts': ['door']},
            },
            {
                'filepath': 'parts/b-obj.jpg',
                'media_type': 'object',
                'metadata': {'parent_id': parent['b']},
                'array_fields': {'parts': []},
            },
            {'filepath': 'parts/c-obj.jpg', 'media_type': 'object', 'metadata': {'parent_id': parent['c']}},
            {'filepath': 'parts/orphan.jpg', 'media_type': 'object', 'array_fields': {'parts': ['roof']}},
        ]
        await datasets.add_samples(1, {'samples': objects_added}, user=USER)
        await run_refresh(database, 1)
        under_images = {entry['value']: entry['count'] for entry in (await suggest('parts', images()))['values']}
        assert under_images == {'wheel': 1, 'door': 1}  # the orphan's roof is no image's
        under_objects = {entry['value']: entry['count'] for entry in (await suggest('parts', objects()))['values']}
        assert under_objects == {'door': 1, 'roof': 1, 'wheel': 1}
        everything = ['door', 'roof', 'wheel']
        problems = []
        for grid in ('image', 'object'):
            for base in ([], [leaf('parts', 'is_empty')], [leaf('parts', 'is_null')], [leaf('parts', 'is_not_null')]):
                node = view(grid, base)
                expected = {v for v in everything if await found(1, view(grid, [*base, leaf('parts', 'in', [v])]))}
                answer = await suggest('parts', node)
                if {entry['value'] for entry in answer['values']} != expected or answer['exhaustive'] is not True:
                    problems.append((grid, base, 'suggestions', answer, sorted(expected)))
                eager = {entry['name']: entry for entry in await datasets._get_dataset_field_stats_impl(1, node, False)}
                facet = (await datasets._get_dataset_field_stats_impl(1, node, True, ['parts']))[0]
                for reader, entry in (('list', eager['parts']), ('opened', facet)):
                    if set(entry.get('values') or []) != expected:
                        problems.append((grid, base, reader, entry.get('values'), sorted(expected)))
        assert not problems, problems

    registry_database(scenario)
