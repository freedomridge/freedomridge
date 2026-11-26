"""Four backend fixes: `id contains`, metadata keys named like a sample's own columns, what `not` keeps on a dataset
with objects, and how often describe_dataset reads the samples.

Written from the contract (f3-contract-backend-fixes), not from the code, and tested where a user meets each rule: the
HTTP API signed in with real API tokens (the access-rule tests' World, through the region tests' regions_world: a
database of its own per test), `assistant.call_tool` for the assistant's tools, and docs/FILTER_QUERY.md's text. The
SDK's side of section 2 is a case in python/fridge/test_sdk.py, against a running server. Every expected answer is
computed from the rows planted here, or is what main answers today where a line says an answer is unchanged.

1. `{"field": "id", "operator": "contains", "value": "12"}` keeps the samples whose id, written in decimal, contains
   the text, in every reader of filters: samples/query (the list and its count), aggregate, histogram, field-stats (the
   sidebar's facet) and a field's autocomplete (the sidebar's values), map_points and map_extent, a selection token,
   /api/search, a version's query and aggregate, and a bulk tag (its job's count and the rows it tags). Its negation
   keeps every other sample; text that isn't digits keeps none and is no error; as text it is `id contains "12"`.
   Every other operator on `id`, and `contains` on other fields, answer as main does.
2. Replaced by the protected-keys contract (test_protected_metadata_keys_spec.py). Here only the five keys main
   refuses (`id`, `dataset_id`, `filepath`, `created_at`, `last_modified_at`) stay pinned: refused where samples are
   added, with main's status and sentence, storing nothing.
3. On a dataset without objects a negation (`not concept "x" > t`, `!=`, `NOT (... = ...)`, `not in`) keeps the images
   that have no score or no such field too; with objects, the image grid keeps only the images that have one. The
   docs say so.
4. describe_dataset answers as main does, and one call sends at most one statement that counts or scans the samples
   (one answered from the ends of an index, the smallest and largest id or created_at, doesn't count).

Where the contract is unclear, or main is not as it says, these tests read it this way:
- Section 1: the sidebar's "does not contain" is the shape frontend/lib/filter-utils.ts makes: a NOT around the
  contains, and a NOT around an OR of the contains (and an `in`) for a text filter with several terms. Text that is
  "not digits" is any text with a character that isn't 0-9 (`ab`, `1a`, `12 `, `-12`, `1.2`). The empty text is a part
  of every id: `id contains ""` keeps every sample, as `contains ""` does on other fields (pinned there). On a dataset
  with objects, `NOT (id contains ...)` is dropped for the other grain, as main drops `id != x`, so the image grid
  keeps the images whose own id lacks the text (an object's id never brings its image back). No test depends on the
  ids a run hands out: expected samples are computed from the ids the database gave, and the texts the objects test
  uses are chosen from them (each held by some image's id but not its object's, and the other way round; images and
  objects are added 40 at a time, at most 5 times, until such texts exist). Two tests also run with the ids moved
  (from 1195, 4321 and 99970, where they grow a digit) so a different set of ids is exercised every run. A positive
  `id contains` in the image grid is left untested (the cross-grain rule reads an object's id there, as it does for
  `id = x`). `id contains 12` with a bare number, and `like` in the language, are left untested. A bulk edit's count is
  the bulk tag job's `updated_count`, beside the rows it tagged. The sidebar's facet is checked by its min and max
  when some sample is kept (main answers its cached numbers for a filter that keeps none). The same checks run on the
  same ids listed with `in`, which pass today, so a failure of the `contains` tests is the filter's, not the checks'.
- Section 2 (now the protected-keys contract): main refuses five keys, not four, and only where samples are added
  (POST /samples): PATCH /samples/{id} and POST /samples/bulk-set (each of its three modes) store a `created_at` key
  in metadata today. So "refused exactly as before" is pinned where samples are added only; what the edit routes do
  with those keys is not tested either way.
- Section 3: "the image grid's query" is the filter itself on a dataset without objects, and on one with objects the
  filter the dataset page wraps it in (test_assistant_views_spec.image_grain). Objects here carry none of their
  images' fields or scores. The docs test is light: no sentence of docs/FILTER_QUERY.md keeps "the samples the concept
  has no score for" without naming the kind of dataset (it says objects); the rule for datasets with objects is a
  sentence naming objects, a negation, "only" and the field or the score; the "Concept scores" section and the
  "Operators" section (where `NOT` is described; its table aside) both speak of objects.
- Section 3.2's added points: a negated place is asked on the same images without objects, with objects that carry
  no location, and with objects at their image's location, compared by a planted `plot`, in samples/query and its
  count and in aggregate's count; without objects every negation keeps the images with no location (main, pinned).
  As decided, the exclusion as the whole filter (the text `location not within`, `geo_region_exclude`,
  `geo_polygon_exclude`) is a requirement; a NOT around the place itself, the exclusion inside an OR group or inside
  the app's coupled form of one filter card, and a NOT over two fields are pins of today, named as pins expected to
  change.
- Section 3.4: "a place exclusion" is `geo_region_exclude` or `geo_polygon_exclude` (a lasso on an embedding map
  is not a place); "however it is wrapped" is as the only rule of an AND group, once or twice. The server's part is
  tested here (a lifted exclusion keeps the five images whatever the grouping of its place test); the assistant's
  wrapper through a saved view's stage and open_saved_view; both wrappers' output through the cases they share
  (frontend/tests/fixtures/image-grain-cases.json: a wrapped exclusion grains exactly as the bare one, and a group of
  two rules stays coupled as the group); the page in the browser (frontend/e2e/place-exclusion-builder.spec.ts).
- Section 3.3: concept_preview's `above`, `below` and `unscored` are the images scoring above the cut, at or below it,
  and with no score, on both datasets (its description says "no score").
- Section 4: "reads the samples" is counted in the test process, for the length of the one call: every statement any
  asyncpg connection is handed through its public methods (execute, executemany, fetch, fetchrow, fetchval,
  fetchmany, prepare, cursor, copy_from_query; a pool's methods reach these) is recorded, and one reads the samples
  when its text, with quoted literals and comments taken out, names the table `samples` (the word alone, so
  `sample_hashes` or `total_samples` don't count). Of those, one is exempt (contract 4.1: answered from the ends of an
  index, not a scan) exactly when its text, literals and comments out and spaces and case aside, is
  `SELECT <items> FROM samples [alias] WHERE dataset_id = <$n or a number>` and every item is `min(...)` or `max(...)`
  of `id` or `created_at` (qualified by the alias or not), with or without a column alias: the dataset's smallest
  and largest id or created_at. Anything more makes it count: another column or expression, another condition, a
  join, DISTINCT, a count, a subquery, GROUP BY or ORDER BY, or no dataset condition. At most one statement that
  names the samples table and is not exempt may be sent. A statement that only calls a function reading samples
  inside the database would not be seen. The counter and the exemption are checked on statements of their own.
  "None where the server keeps the number already" is not tested. The answer is pinned for each of
  the four datasets (with and without objects, with and without locations and embeddings): main's answer, field by
  field, built from the planted rows, with the statistics counted first ("while the kept statistics are current");
  where main answers null (a built-in field's samples_with_value for tags, created_at and last_modified_at) the pin
  is null. Before a first count and while changes wait to be counted, `complete` is checked to be right for tags and
  media_type: true only with the whole values; indexed fields are not checked there.
"""

import contextlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

import asyncpg
import httpx
import pytest

from backend import assistant
from backend.field_descriptions import DEFAULTS
from backend.routers import datasets
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_assistant_conversation_spec import count_statistics
from backend.tests.test_assistant_core_spec import open_door
from backend.tests.test_assistant_guests_spec import section
from backend.tests.test_assistant_views_spec import call, image_grain, result, view_ids
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_regions_spec import (
    ADMIN_USER,
    Regions,
    loaded,
    mistake,
    nodes,
    parse,
    post,
    selected,
    text_of,
)
from backend.tests.test_upgrade_paths import ROOT

database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(
    not (os.getenv('FRIDGE_TEST_POSTGRES_DSN') and os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')),
    reason='Requires disposable PostgreSQL',
)


def contains(field: str, value: Any) -> dict[str, Any]:
    return {'field': field, 'operator': 'contains', 'value': value}


def negated(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'NOT', 'filters': list(parts)}


def either(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'OR', 'filters': list(parts)}


async def ok(response: httpx.Response, where: Any = None) -> Any:
    assert response.status_code == 200, (where, response.status_code, response.text)
    return response.json()


# --- 1. `id contains` ---

FILLER, NUMBERED = 30, 31  # a dataset whose samples come first (so the numbered ones don't start at 1), and the one
N = 130  # samples in the numbered dataset: some ids hold "12", most don't


@dataclass
class Row:
    rank: int
    plot: str
    note: str
    location: Optional[tuple[float, float]]


def located(k: int) -> Optional[tuple[float, float]]:
    """Every fifth sample has no location; the others are spread on exact binary fractions."""
    return None if k % 5 == 0 else (-20 + 0.25 * k, 10 + 0.125 * k)


class Numbered:
    """The numbered dataset's samples by id, and the version saved after they were added."""

    def __init__(self, rows: dict[int, Row], version: int):
        self.rows, self.version = rows, version

    @property
    def everything(self) -> set[int]:
        return set(self.rows)

    def holding(self, *texts: str) -> set[int]:
        """The samples whose id, written in decimal, contains any of the texts."""
        return {sample_id for sample_id in self.rows if any(text in str(sample_id) for text in texts)}

    def where(self, keep: Callable[[int, Row], bool]) -> set[int]:
        return {sample_id for sample_id, row in self.rows.items() if keep(sample_id, row)}


async def from_id(r: Regions, start: Optional[int]) -> None:
    """The next sample gets the id `start` (None: whatever the database gives next)."""
    if start is not None:
        await r.database.execute("SELECT setval(pg_get_serial_sequence('samples', 'id'), $1, FALSE)", start)


async def numbered(r: Regions, start: Optional[int] = None) -> Numbered:
    await r.dataset(FILLER)
    filler = [{'filepath': f'filler-{k}.jpg'} for k in range(7)]
    await datasets.add_samples(FILLER, {'samples': filler}, user=ADMIN_USER)
    await r.dataset(NUMBERED)
    await from_id(r, start)
    samples = []
    for k in range(N):
        sample: dict[str, Any] = {
            'filepath': f'n-{k:03d}.jpg',
            'metadata': {'plot': f'p{k:03d}', 'rank': k, 'note': f'Note {k}'},
        }
        point = located(k)
        if point is not None:
            sample['location'] = {'type': 'Point', 'coordinates': list(point)}
        samples.append(sample)
    await datasets.add_samples(NUMBERED, {'samples': samples}, user=ADMIN_USER)
    stored = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', NUMBERED)
    rows = {}
    for item in stored:
        k = int(item['filepath'][2:5])
        rows[item['id']] = Row(k, f'p{k:03d}', f'Note {k}', located(k))
    assert len(rows) == N and min(rows) > 7, sorted(rows)
    vectors = [
        {'sample_id': sample_id, 'vector': [1.0, row.rank / 1000, 0.0, 0.0], 'field_name': 'clip'}
        for sample_id, row in rows.items()
    ]
    assert (await post(r, f'/api/datasets/{NUMBERED}/embeddings', vectors)).is_success
    await ok(await post(r, f'/api/datasets/{NUMBERED}/versions', {'name': 'numbered'}))
    version = await r.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', NUMBERED)
    return Numbered(rows, version)


def digit_strings(ids: Any) -> list[str]:
    """Every string of one or two digits found in the ids, shortest first."""
    found = {str(i)[a : a + size] for i in ids for size in (1, 2) for a in range(len(str(i)) - size + 1)}
    return sorted(found, key=lambda text: (len(text), text))


def extent(n: Numbered, ids: set[int]) -> dict[str, Any]:
    points = [point for sample_id in ids if (point := n.rows[sample_id].location) is not None]
    if not points:
        return {'count': 0}
    lons, lats = [lon for lon, _ in points], [lat for _, lat in points]
    return {'w': min(lons), 's': min(lats), 'e': max(lons), 'n': max(lats), 'count': len(points)}


async def assert_every_reader(r: Regions, n: Numbered, node: dict[str, Any], expected: set[int]) -> None:
    """Each reader of filters keeps exactly the samples `expected` for the filter."""
    where = json.dumps(node)
    assert await selected(r, node, NUMBERED) == expected, where
    plots = sorted(n.rows[sample_id].plot for sample_id in expected)
    ranks = [n.rows[sample_id].rank for sample_id in expected]
    aggregations = [{'field': 'plot', 'op': 'count'}, {'field': 'plot', 'op': 'values'}]
    body = await ok(
        await post(r, f'/api/datasets/{NUMBERED}/aggregate', {'filters': node, 'aggregations': aggregations}),
    )
    count, values = body['results']
    assert (count['value'], sorted(values['value'])) == (len(expected), plots), where
    body = await ok(
        await post(r, f'/api/datasets/{NUMBERED}/histogram', {'field': 'rank', 'filters': node, 'bins': 20}),
    )
    assert body['total'] == len(expected), where
    facet = await ok(await post(r, f'/api/datasets/{NUMBERED}/field-stats', {'filters': node, 'fields': ['rank']}))
    if expected:
        [entry] = facet
        assert (entry['min'], entry['max']) == (min(ranks), max(ranks)), (where, entry)
    listed = await ok(
        await post(r, f'/api/datasets/{NUMBERED}/fields/plot/autocomplete', {'filters': node, 'limit': 1000}),
    )
    assert sorted(item['value'] for item in listed['values']) == plots, where
    points = await ok(await post(r, f'/api/datasets/{NUMBERED}/map_points', {'filters': node, 'points': True}))
    with_location = {sample_id for sample_id in expected if n.rows[sample_id].location is not None}
    assert {item['id'] for item in points['items']} == with_location, where
    box = await ok(await post(r, f'/api/datasets/{NUMBERED}/map_extent', {'filters': node}))
    assert box == pytest.approx(extent(n, expected)), where
    token = await ok(await post(r, f'/api/datasets/{NUMBERED}/selection_token', {'filters': node}))
    assert token['count'] == len(expected), where
    in_token = {'field': 'id', 'operator': 'in_selection', 'value': token['token']}
    assert await selected(r, in_token, NUMBERED) == expected, where
    search = {'dataset_id': NUMBERED, 'vector': [1.0, 0.0, 0.0, 0.0], 'field_name': 'clip', 'limit': 1000}
    found = await ok(await post(r, '/api/search', {**search, 'filters': node}))
    assert {item['id'] for item in found} == expected, where
    base = f'/api/datasets/{NUMBERED}/versions/{n.version}'
    rows = await ok(await post(r, f'{base}/samples/query', {'filters': node, 'limit': 1000}))
    assert {item['id'] for item in rows['items']} == expected, where
    saved = await ok(
        await post(r, f'{base}/aggregate', {'filters': node, 'aggregations': [{'field': 'plot', 'op': 'values'}]}),
    )
    assert sorted(saved['results'][0]['value']) == plots, where


async def assert_bulk_tag(r: Regions, node: dict[str, Any], expected: set[int], tag: str) -> None:
    """A bulk tag by the filter, run as the job runner would: its count, and the rows it tagged."""
    tagged = await post(r, f'/api/datasets/{NUMBERED}/samples/bulk-tag', {'filters': node, 'add_tags': [tag]})
    assert tagged.status_code in (200, 202), tagged.text
    if tagged.status_code == 202:
        job = await finish(r.database)
        assert job['result']['updated_count'] == len(expected), job['result']
    else:
        assert tagged.json()['updated_count'] == len(expected), tagged.text
    rows = await r.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 AND $2 = ANY(tags)', NUMBERED, tag)
    assert {row['id'] for row in rows} == expected, tag


@pytest.mark.parametrize('start', [None, 1195], ids=['ids-as-given', 'ids-from-1195'])
def test_id_contains_keeps_the_samples_whose_id_holds_the_digits_in_every_reader(regions_world, start):
    async def scenario(r):
        n = await numbered(r, start)
        # Texts chosen from the ids this run has: some ids hold each, some don't (whichever ids the database gave).
        splitting = [text for text in digit_strings(n.rows) if 0 < len(n.holding(text)) < N][:3]
        assert splitting, sorted(n.rows)
        for text in dict.fromkeys(('12', '3', '0', '1', '', *splitting)):  # the empty text is a part of every id
            await assert_every_reader(r, n, contains('id', text), n.holding(text))
        # Under AND and OR, beside another field.
        early = {'field': 'rank', 'operator': '<', 'value': 60}
        cases = [
            (
                {'logic': 'AND', 'filters': [contains('id', '1'), early]},
                n.holding('1') & n.where(lambda _, row: row.rank < 60),
            ),
            (either(contains('id', '12'), contains('id', '99')), n.holding('12', '99')),
        ]
        for node, expected in cases:
            await assert_every_reader(r, n, node, expected)
        await assert_bulk_tag(r, contains('id', '12'), n.holding('12'), 'twelve')

    regions_world(scenario)


def test_the_negation_of_id_contains_keeps_every_other_sample_in_every_reader(regions_world):
    async def scenario(r):
        n = await numbered(r)
        twelve, three = n.holding('12'), n.holding('3')
        first = min(n.rows)
        cases = [
            (negated(contains('id', '12')), n.everything - twelve),  # the sidebar's "does not contain", and NOT
            (negated(either(contains('id', '12'), contains('id', '3'))), n.everything - twelve - three),
            (
                negated(either({'field': 'id', 'operator': 'in', 'value': [first]}, contains('id', '12'))),
                n.everything - twelve - {first},
            ),
            (negated(negated(contains('id', '12'))), twelve),
        ]
        for node, expected in cases:
            await assert_every_reader(r, n, node, expected)
        await assert_bulk_tag(r, negated(contains('id', '12')), n.everything - twelve, 'not-twelve')

    regions_world(scenario)


PARENTS = 33  # a dataset with objects: one object under each image
BATCH, BATCHES = 40, 5  # images (each with its object) added at a time, and at most how many times


def telling(child: dict[int, int]) -> list[str]:
    """Digit strings that some image's id holds while its object's lacks, and some object's id holds while its
    image's lacks: on these an image grid that read the object's id instead would answer differently."""
    return [
        text
        for text in digit_strings([*child, *child.values()])
        if any(text in str(image) and text not in str(own) for image, own in child.items())
        and any(text not in str(image) and text in str(own) for image, own in child.items())
    ]


@pytest.mark.parametrize('start', [None, 4321, 99_970], ids=['ids-as-given', 'ids-from-4321', 'ids-from-99970'])
def test_on_a_dataset_with_objects_not_id_contains_keeps_the_images_whose_own_id_lacks_the_text(regions_world, start):
    async def scenario(r):
        await r.dataset(PARENTS)
        await from_id(r, start)
        child: dict[int, int] = {}  # each image's id: its object's id, as the database gave them
        for batch in range(BATCHES):  # until this run's ids offer texts that tell an image's id from its object's
            images = [{'filepath': f'img-{batch}-{k}.jpg'} for k in range(BATCH)]
            await datasets.add_samples(PARENTS, {'samples': images}, user=ADMIN_USER)
            made = {
                row['filepath']: row['id']
                for row in await r.database.fetch(
                    'SELECT id, filepath FROM samples WHERE dataset_id = $1 AND filepath LIKE $2',
                    PARENTS,
                    f'img-{batch}-%',
                )
            }
            objects = [
                {'filepath': f'obj-{batch}-{k}.jpg', 'media_type': 'object', 'metadata': {'parent_id': made[path]}}
                for k, path in enumerate(sorted(made, key=made.__getitem__))
            ]
            await datasets.add_samples(PARENTS, {'samples': objects}, user=ADMIN_USER)
            child = {
                row['parent_id']: row['id']
                for row in await r.database.fetch(
                    "SELECT id, parent_id FROM samples WHERE dataset_id = $1 AND media_type = 'object'",
                    PARENTS,
                )
            }
            if telling(child):
                break
        texts = telling(child)[:3]
        assert texts, f'{BATCHES * BATCH} images and their objects offered no text: {sorted(child.items())}'
        ids = set(child)

        async def image_grid(*filters: dict[str, Any]) -> set[int]:
            return await selected(r, image_grain({'logic': 'AND', 'filters': list(filters)}), PARENTS)

        # As main reads `id != x` there (pinned): the image's own id, never its object's.
        first = min(ids)
        assert await image_grid({'field': 'id', 'operator': '!=', 'value': first}) == ids - {first}
        for text in texts:
            lacking = {i for i in ids if text not in str(i)}
            assert await image_grid(negated(contains('id', text))) == lacking, text

    regions_world(scenario)


def test_every_reader_keeps_those_samples_when_their_ids_are_listed_with_in(regions_world):
    """Pins main, and the checks above: the same ids, listed with `in`, in every reader, and a bulk tag by them."""

    async def scenario(r):
        n = await numbered(r)
        twelve = n.holding('12')
        listed = {'field': 'id', 'operator': 'in', 'value': sorted(twelve)}
        await assert_every_reader(r, n, listed, twelve)
        await assert_every_reader(r, n, negated(listed), n.everything - twelve)
        await assert_every_reader(r, n, {'field': 'id', 'operator': 'in', 'value': []}, set())
        await assert_bulk_tag(r, {'logic': 'AND', 'filters': [listed]}, twelve, 'listed')

    regions_world(scenario)


def test_id_contains_text_that_is_not_digits_keeps_nothing_and_is_no_error(regions_world):
    async def scenario(r):
        n = await numbered(r)
        for text in ('ab', '1a', '12 ', '-12', '1.2'):
            await assert_every_reader(r, n, contains('id', text), set())
            assert await selected(r, negated(contains('id', text)), NUMBERED) == n.everything, text
        await assert_every_reader(r, n, negated(contains('id', 'ab')), n.everything)

    regions_world(scenario)


def test_id_contains_as_text_parses_formats_and_selects_as_the_filter_does(regions_world):
    async def scenario(r):
        n = await numbered(r)
        node = contains('id', '12')
        assert await nodes(r, 'id contains "12"', NUMBERED) == [node]
        assert await nodes(r, "id CONTAINS '12'", NUMBERED) == [node]
        assert await text_of(r, node, NUMBERED) == 'id contains "12"'
        written = await text_of(r, node, NUMBERED)
        assert written is not None and await nodes(r, written, NUMBERED) == [node]
        assert await nodes(r, 'NOT (id contains "12")', NUMBERED) == [negated(node)]
        for query, expected in (
            ('id contains "12"', n.holding('12')),
            ('NOT (id contains "12")', n.everything - n.holding('12')),
            ('id contains "1" AND rank < 60', n.holding('1') & n.where(lambda _, row: row.rank < 60)),
            ('id contains "12" OR id contains "99"', n.holding('12', '99')),
            ('id contains "ab"', set()),
        ):
            filters = await nodes(r, query, NUMBERED)
            assert await selected(r, {'logic': 'AND', 'filters': filters}, NUMBERED) == expected, query

    regions_world(scenario)


def test_every_other_operator_on_id_answers_as_today(regions_world):
    """Pins main: these pass before the fix."""

    async def scenario(r):
        n = await numbered(r)
        ids = sorted(n.rows)
        a, b, c = ids[3], ids[40], ids[100]
        node: dict[str, Any]
        for operator_, value, expected in (
            ('=', a, {a}),
            ('!=', a, n.everything - {a}),
            ('>', b, {i for i in ids if i > b}),
            ('>=', b, {i for i in ids if i >= b}),
            ('<', b, {i for i in ids if i < b}),
            ('<=', b, {i for i in ids if i <= b}),
            ('in', [a, c], {a, c}),
            ('not in', [a, c], n.everything - {a, c}),
            ('exists', None, n.everything),
            ('not_exists', None, set()),
            ('is_null', None, set()),
            ('is_not_null', None, n.everything),
        ):
            node = {'field': 'id', 'operator': operator_, 'value': value}
            assert await selected(r, node, NUMBERED) == expected, node
            assert await selected(r, negated(node), NUMBERED) == n.everything - expected, node
        # What main refuses on id, refused the same way.
        for unknown, given in (
            ('==', a),
            ('<>', a),
            ('not_in', [a]),
            ('nin', [a]),
            ('between', [a, b]),
            ('like', '12'),
        ):
            node = {'field': 'id', 'operator': unknown, 'value': given}
            response = await post(r, f'/api/datasets/{NUMBERED}/samples/query', {'filters': node})
            refusal = {'detail': f'Unknown filter operator: {unknown!r}'}
            assert (response.status_code, response.json()) == (400, refusal), response.text
        response = await post(
            r,
            f'/api/datasets/{NUMBERED}/samples/query',
            {'filters': {'field': 'id', 'operator': '=', 'value': '12'}},
        )
        assert response.status_code == 400 and response.json()['detail'].startswith('Invalid input'), response.text
        # As text.
        for query, node in (
            (f'id = {a}', {'field': 'id', 'operator': '=', 'value': a}),
            (f'id != {a}', {'field': 'id', 'operator': '!=', 'value': a}),
            (f'id > {b}', {'field': 'id', 'operator': '>', 'value': b}),
            (f'id in ({a}, {c})', {'field': 'id', 'operator': 'in', 'value': [a, c]}),
            (f'id not in ({a})', {'field': 'id', 'operator': 'not in', 'value': [a]}),
            ('id exists', {'field': 'id', 'operator': 'exists', 'value': None}),
        ):
            assert await nodes(r, query, NUMBERED) == [node], query
            assert await text_of(r, node, NUMBERED) == query, query

    regions_world(scenario)


def test_contains_on_a_text_and_a_number_metadata_field_answers_as_today(regions_world):
    """Pins main: these pass before the fix."""

    async def scenario(r):
        n = await numbered(r)
        # A text field: case-insensitive, as text too.
        note = contains('note', 'note 1')
        expected = n.where(lambda _, row: 'note 1' in row.note.lower())
        assert await selected(r, note, NUMBERED) == expected
        assert await selected(r, negated(note), NUMBERED) == n.everything - expected
        assert await nodes(r, 'note contains "note 1"', NUMBERED) == [note]
        assert await text_of(r, note, NUMBERED) == 'note contains "note 1"'
        for field in ('note', 'rank', 'plot'):  # the empty text is a part of every value
            assert await selected(r, contains(field, ''), NUMBERED) == n.everything, field
        # A number field: the filter matches its value written as text; the language refuses it, format writes it.
        rank = contains('rank', '1')
        expected = n.where(lambda _, row: '1' in str(row.rank))
        assert await selected(r, rank, NUMBERED) == expected
        assert await selected(r, negated(rank), NUMBERED) == n.everything - expected
        body = await ok(
            await post(
                r,
                f'/api/datasets/{NUMBERED}/aggregate',
                {'filters': rank, 'aggregations': [{'field': 'plot', 'op': 'count'}]},
            ),
        )
        assert body['results'][0]['value'] == len(expected)
        error = mistake(await parse(r, 'rank contains "1"', NUMBERED), line=1)
        assert "'contains' does not apply to the number field 'rank'" in error['message'], error
        assert await text_of(r, rank, NUMBERED) == 'rank contains "1"'

    regions_world(scenario)


# --- 2. Metadata keys named like a sample's own columns ---

KEYED = 50
OLDER = ('id', 'dataset_id', 'filepath', 'created_at', 'last_modified_at')  # what main refuses


def sentence(*keys: str) -> str:
    """Main's sentence for a refused key (as it answers for created_at), naming the keys."""
    return f'metadata key(s) {sorted(keys)!r} name sample columns — set them on the sample itself'


def refused_at(index: int, *keys: str) -> dict[str, Any]:
    """Main's whole answer when the sample at `index` of an upload carries the keys (422)."""
    return {'detail': [{'type': 'value_error', 'loc': [index, 'metadata'], 'msg': f'Value error, {sentence(*keys)}'}]}


async def stored(r: Regions) -> list[tuple]:
    rows = await r.database.fetch(
        'SELECT id, filepath, media_type, tags, metadata, last_modified_at FROM samples WHERE dataset_id = $1 '
        'ORDER BY id',
        KEYED,
    )
    return [tuple(row.values()) for row in rows]


async def keyed(r: Regions) -> list[int]:
    """The dataset with three samples, each with a metadata field `ok`."""
    await r.dataset(KEYED)
    samples = [{'filepath': f'k{k}.jpg', 'metadata': {'ok': k}, 'tags': ['kept']} for k in range(3)]
    await datasets.add_samples(KEYED, {'samples': samples}, user=ADMIN_USER)
    return [
        row['id'] for row in await r.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id', KEYED)
    ]


async def upload(r: Regions, samples: list[dict[str, Any]], **options: Any) -> httpx.Response:
    return await post(r, f'/api/datasets/{KEYED}/samples', {'samples': samples, **options})


def test_the_keys_main_refuses_are_refused_where_samples_are_added_as_today(regions_world):
    """Pins main: these pass before the fix."""

    async def scenario(r):
        await keyed(r)
        before = await stored(r)
        for key in OLDER:
            response = await upload(r, [{'filepath': f'{key}.jpg', 'metadata': {key: 'x', 'ok': 9}}])
            assert (response.status_code, response.json()) == (422, refused_at(0, key)), response.text
            response = await upload(
                r,
                [{'filepath': 'fine.jpg', 'metadata': {'ok': 9}}, {'filepath': f'{key}.jpg', 'metadata': {key: 'x'}}],
            )
            assert (response.status_code, response.json()) == (422, refused_at(1, key)), response.text
        response = await upload(r, [{'filepath': 'two.jpg', 'metadata': {'filepath': 'x', 'created_at': 'y'}}])
        assert (response.status_code, response.json()) == (422, refused_at(0, 'created_at', 'filepath'))
        response = await upload(r, [{'filepath': 'k1.jpg', 'metadata': {'created_at': 'x'}}], on_duplicate='update')
        assert (response.status_code, response.json()) == (422, refused_at(0, 'created_at')), response.text
        assert await stored(r) == before  # nothing added, nothing changed

    regions_world(scenario)


# --- 3. What `not` keeps on a dataset with objects ---

PLAIN, WITH_OBJECTS = 60, 61
CUT = 0.5
# The images of both datasets: (score for the concept x (None: no score), crop (None: no such field)). Every score is
# exact in single precision.
IMAGES: list[tuple[Optional[float], Optional[str]]] = [
    (0.25, 'corn'),
    (0.75, 'wheat'),
    (0.375, None),
    (None, 'wheat'),
    (None, None),
    (0.875, 'corn'),
    (0.5, 'barley'),  # at the cut: not above it
    (None, 'corn'),
]


async def planted_images(r: Regions, dataset_id: int, objects: bool) -> tuple[list[int], int]:
    """The images (and, with `objects`, one object under each, with none of the image's fields or scores), and the
    concept x scoring some images. Returns the images' ids in IMAGES' order and the concept's id."""
    await r.dataset(dataset_id)
    samples = []
    for k, (_, crop) in enumerate(IMAGES):
        samples.append({'filepath': f'i{k}.jpg', 'metadata': {} if crop is None else {'crop': crop}})
    await datasets.add_samples(dataset_id, {'samples': samples}, user=ADMIN_USER)
    by_path = {
        row['filepath']: row['id']
        for row in await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', dataset_id)
    }
    ids = [by_path[f'i{k}.jpg'] for k in range(len(IMAGES))]
    if objects:
        children = [
            {'filepath': f'o{k}.jpg', 'media_type': 'object', 'metadata': {'parent_id': ids[k], 'label': 'box'}}
            for k in range(len(IMAGES))
        ]
        await datasets.add_samples(dataset_id, {'samples': children}, user=ADMIN_USER)
        parents = await r.database.fetch(
            "SELECT parent_id FROM samples WHERE dataset_id = $1 AND media_type = 'object'",
            dataset_id,
        )
        assert sorted(row['parent_id'] for row in parents) == sorted(ids)
    concept = await r.database.fetchval(
        'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
        "VALUES ($1, 'x', 'clip', 'seed', '[1,0]', $2) RETURNING id",
        dataset_id,
        CUT,
    )
    for k, (score, _) in enumerate(IMAGES):
        if score is not None:
            await r.database.execute(
                'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, $3)',
                concept,
                ids[k],
                score,
            )
    return ids, concept


def kept(ids: list[int], keep: Callable[[Optional[float], Optional[str]], bool]) -> set[int]:
    return {ids[k] for k, (score, crop) in enumerate(IMAGES) if keep(score, crop)}


async def grid(r: Regions, dataset_id: int, query: str, objects: bool) -> set[int]:
    """What the image grid shows for the query: the filter itself, or on a dataset with objects the filter the dataset
    page sends for its images."""
    node = {'logic': 'AND', 'filters': await nodes(r, query, dataset_id)}
    return await selected(r, image_grain(node) if objects else node, dataset_id)


def test_a_negation_keeps_the_images_without_the_score_or_the_field_only_on_a_dataset_without_objects(regions_world):
    """Pins main: these pass before the fix (no behaviour changes)."""

    async def scenario(r):
        plain, _ = await planted_images(r, PLAIN, objects=False)
        coupled, _ = await planted_images(r, WITH_OBJECTS, objects=True)
        scored, cropped = 'score', 'crop'
        # (the query, what a row must have for the negation to hold there, which of those it keeps)
        cases = [
            (f'not concept "x" > {CUT}', scored, lambda score, _: score <= CUT),
            (f'NOT (concept "x" > {CUT})', scored, lambda score, _: score <= CUT),
            ('crop != "corn"', cropped, lambda _, crop: crop != 'corn'),
            ('NOT (crop = "corn")', cropped, lambda _, crop: crop != 'corn'),
            ('crop not in ("corn", "barley")', cropped, lambda _, crop: crop == 'wheat'),
        ]
        for query, needs, passes in cases:
            index = 0 if needs == scored else 1
            having = {k for k, image in enumerate(IMAGES) if image[index] is not None}
            with_one = {k for k in having if passes(*IMAGES[k])}
            without_objects = with_one | (set(range(len(IMAGES))) - having)
            assert with_one < without_objects, query  # the two kinds of dataset differ here
            assert await grid(r, PLAIN, query, objects=False) == {plain[k] for k in without_objects}, query
            assert await grid(r, WITH_OBJECTS, query, objects=True) == {coupled[k] for k in with_one}, query
        # The positive forms keep the same images on both.
        for query, keep in (
            (f'concept "x" > {CUT}', lambda score, _: score is not None and score > CUT),
            ('crop = "corn"', lambda _, crop: crop == 'corn'),
        ):
            assert await grid(r, PLAIN, query, objects=False) == kept(plain, keep), query
            assert await grid(r, WITH_OBJECTS, query, objects=True) == kept(coupled, keep), query

    regions_world(scenario)


def test_concept_preview_counts_the_unscored_images_on_both_kinds_of_dataset(regions_world):
    """Pins main (contract 3.3): concept_preview's unscored stays what its description says."""

    async def scenario(r):
        await planted_images(r, PLAIN, objects=False)
        await planted_images(r, WITH_OBJECTS, objects=True)
        await open_door(r)
        described = {tool.name: tool for tool in assistant.tools()}['concept_preview'].description
        assert 'no score' in described, described
        above = sum(1 for score, _ in IMAGES if score is not None and score > CUT)
        below = sum(1 for score, _ in IMAGES if score is not None and score <= CUT)
        unscored = sum(1 for score, _ in IMAGES if score is None)
        for dataset_id in (PLAIN, WITH_OBJECTS):
            answer = await result(ADMIN, 'concept_preview', {'dataset': dataset_id, 'concept': 'x', 'threshold': CUT})
            counts = (answer['view_count'], answer['above'], answer['below'], answer['unscored'])
            assert counts == (len(IMAGES), above, below, unscored), (dataset_id, answer)
            assert (answer['keep'], answer['remove']) == (f'concept "x" > {CUT}', f'not concept "x" > {CUT}'), answer

    regions_world(scenario)


# A negated place, and a NOT over two fields, on the same images with and without objects (contract 3.2).
PLACE_PLAIN, PLACE_BARE, PLACE_LOCATED = 70, 71, 72  # no objects; objects with no location; objects at their image
# The images: (plot, location). XA-NM is the region test_regions_spec loads as [0, 5] x [0, 5].
PLACES: list[tuple[str, Optional[tuple[float, float]]]] = [
    ('in-a', (2.5, 2.5)),
    ('in-b', (1.0, 1.0)),
    ('out-a', (32.0, 2.5)),
    ('out-b', (100.0, 50.0)),
    ('out-c', (7.5, 2.5)),  # next door, in XA-LF
    ('none-a', None),
    ('none-b', None),
]
INSIDE_PLACE = {plot for plot, point in PLACES if point is not None and point[0] <= 5}
NO_PLACE = {plot for plot, point in PLACES if point is None}
SQUARE = [[0, 0], [5, 0], [5, 5], [0, 5]]  # drawn over XA-NM
NEAR = {'point': [2.5, 2.5], 'max_distance': 300_000}  # metres: in-a and in-b, not out-c (555 km)


def place(operator: str, value: Any) -> dict[str, Any]:
    return {'field': 'location', 'operator': operator, 'value': value}


async def placed_images(r: Regions, dataset_id: int, objects: Optional[str]) -> dict[int, str]:
    """The images of PLACES, and with `objects` one object under each: 'bare' with no location, 'located' at its
    image's location. Returns the images' plots by id."""
    await r.dataset(dataset_id)
    samples = []
    for plot, point in PLACES:
        sample: dict[str, Any] = {'filepath': f'{plot}.jpg', 'metadata': {'plot': plot}}
        if point is not None:
            sample['location'] = {'type': 'Point', 'coordinates': list(point)}
        samples.append(sample)
    await datasets.add_samples(dataset_id, {'samples': samples}, user=ADMIN_USER)
    rows = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', dataset_id)
    plots = {row['id']: row['filepath'][: -len('.jpg')] for row in rows}
    if objects is not None:
        children = []
        for image, plot in plots.items():
            child: dict[str, Any] = {
                'filepath': f'{plot}-object.jpg',
                'media_type': 'object',
                'metadata': {'parent_id': image, 'label': 'crate'},  # a field only the objects have
            }
            point = dict(PLACES)[plot]
            if objects == 'located' and point is not None:
                child['location'] = {'type': 'Point', 'coordinates': list(point)}
            children.append(child)
        await datasets.add_samples(dataset_id, {'samples': children}, user=ADMIN_USER)
        stored = await r.database.fetch(
            "SELECT parent_id, location IS NOT NULL AS located FROM samples WHERE dataset_id = $1 "
            "AND media_type = 'object'",
            dataset_id,
        )
        assert sorted(row['parent_id'] for row in stored) == sorted(plots)
        assert sum(row['located'] for row in stored) == (len(PLACES) - len(NO_PLACE) if objects == 'located' else 0)
    return plots


async def kept_plots(
    r: Regions,
    dataset_id: int,
    plots: dict[int, str],
    node: dict[str, Any],
    objects: bool,
) -> set[str]:
    """The images the grid keeps, by plot: the filter itself, or with objects the filter the dataset page sends; its
    count (samples/query's total, checked by `selected`, and aggregate's) agrees."""
    query = image_grain(node) if objects else node
    ids = await selected(r, query, dataset_id)
    body = await ok(
        await post(
            r,
            f'/api/datasets/{dataset_id}/aggregate',
            {'filters': query, 'aggregations': [{'field': 'plot', 'op': 'count'}]},
        ),
    )
    assert body['results'][0]['value'] == len(ids), (dataset_id, node, body)
    return {plots[i] for i in ids}


async def placed(r: Regions) -> dict[str, tuple[int, dict[int, str], bool]]:
    """The three datasets of PLACES, the region set loaded: name -> (dataset id, its images' plots by id, objects)."""
    made = {
        'plain': (PLACE_PLAIN, await placed_images(r, PLACE_PLAIN, None), False),
        'bare': (PLACE_BARE, await placed_images(r, PLACE_BARE, 'bare'), True),
        'located': (PLACE_LOCATED, await placed_images(r, PLACE_LOCATED, 'located'), True),
    }
    await loaded(r)
    return made


async def grid_plots(r: Regions, dataset: tuple[int, dict[int, str], bool], form: Any) -> set[str]:
    """What the image grid keeps for a form: query text read into its top-level parts by the parse route (as the
    SDK, the API and a link make stages of it), or one filter node."""
    dataset_id, plots, objects = dataset
    filters = await nodes(r, form, dataset_id) if isinstance(form, str) else [form]
    return await kept_plots(r, dataset_id, plots, {'logic': 'AND', 'filters': filters}, objects)


EVERY_PLACE = {plot for plot, _ in PLACES}
OUTSIDE_PLACE = EVERY_PLACE - INSIDE_PLACE  # the images with no location among them


def test_a_place_excluded_as_the_whole_filter_keeps_the_same_images_with_and_without_objects(regions_world):
    """A requirement (contract 3.2's second point, as decided): the text `location not within`, `geo_region_exclude`
    and `geo_polygon_exclude`, each the whole filter, and the place itself, keep on a dataset whose objects carry no
    location, and on one whose objects carry their image's, exactly the images they keep on the same images without
    objects. Without objects an exclusion keeps the images with no location too (main, pinned)."""

    async def scenario(r):
        made = await placed(r)
        for name, form, kept in (
            ('text: location not within "XA-NM"', 'location not within "XA-NM"', OUTSIDE_PLACE),
            ('geo_region_exclude', place('geo_region_exclude', 'XA-NM'), OUTSIDE_PLACE),
            ('geo_polygon_exclude', place('geo_polygon_exclude', SQUARE), OUTSIDE_PLACE),
            ('text: location within "XA-NM"', 'location within "XA-NM"', INSIDE_PLACE),
            ('geo_region', place('geo_region', 'XA-NM'), INSIDE_PLACE),
            ('geo_polygon', place('geo_polygon', SQUARE), INSIDE_PLACE),
            ('geo_near', place('geo_near', NEAR), INSIDE_PLACE),
        ):
            for which, dataset in made.items():
                assert await grid_plots(r, dataset, form) == kept, (name, which)

    regions_world(scenario)


def test_today_a_not_around_a_place_brings_back_images_inside_through_objects_with_no_location_pin_expected_to_change(
    regions_world,
):
    """A pin of today's behaviour, expected to change when a NOT on a dataset with objects is guarded (a piece of its
    own, with the NOT over two fields): a NOT around the place itself keeps, on the dataset whose objects carry no
    location, the images inside the place too (their objects, with no location, are not inside it); without objects,
    and with objects at their image's location, it keeps what the exclusion keeps."""

    async def scenario(r):
        made = await placed(r)
        for name, form in (
            ('text: NOT (location within "XA-NM")', 'NOT (location within "XA-NM")'),
            ('NOT geo_region', negated(place('geo_region', 'XA-NM'))),
            ('NOT geo_polygon', negated(place('geo_polygon', SQUARE))),
            ('NOT geo_near', negated(place('geo_near', NEAR))),
        ):
            found = {which: await grid_plots(r, dataset, form) for which, dataset in made.items()}
            assert found == {'plain': OUTSIDE_PLACE, 'bare': EVERY_PLACE, 'located': OUTSIDE_PLACE}, (name, found)

    regions_world(scenario)


def test_today_an_excluded_place_inside_a_larger_condition_pin_expected_to_change(regions_world):
    """A pin of today's behaviour, expected to change with the same piece. The exclusion holds where it is the whole
    filter or a top-level AND part (each part a stage, as the parse route, the SDK and the assistant make them); it is
    not guarded inside an OR group, nor inside the app's coupled form of one filter card holding a whole text (the
    card's parts ANDed into one chip, as frontend/components/filter-stage-editor.tsx makes it, wrapped by the page's
    withGrain, which test_assistant_views_spec.image_grain writes again). The objects all have `label = "crate"`, a
    field the images lack, so a form naming it is asked of the two datasets with objects only."""

    async def scenario(r):
        made = await placed(r)
        never = ('plot = "no-such-plot"', {'field': 'plot', 'operator': '=', 'value': 'no-such-plot'})
        exclusion = place('geo_region_exclude', 'XA-NM')
        # (the form, what the plain, bare-objects and located-objects datasets keep; None: not asked)
        cases: list[tuple[str, Any, tuple[Optional[set[str]], set[str], set[str]]]] = [
            (
                'top-level AND: location not within AND plot != "no-such-plot"',
                'location not within "XA-NM" AND plot != "no-such-plot"',
                (OUTSIDE_PLACE, OUTSIDE_PLACE, OUTSIDE_PLACE),
            ),
            (
                'text: (location not within OR plot = "no-such-plot")',
                f'(location not within "XA-NM" OR {never[0]})',
                (OUTSIDE_PLACE, EVERY_PLACE, OUTSIDE_PLACE),
            ),
            (
                'JSON: OR [geo_region_exclude, plot = "no-such-plot"]',
                either(exclusion, never[1]),
                (OUTSIDE_PLACE, EVERY_PLACE, OUTSIDE_PLACE),
            ),
            (
                'top-level AND: location not within AND label = "crate"',
                'location not within "XA-NM" AND label = "crate"',
                (None, OUTSIDE_PLACE, OUTSIDE_PLACE),
            ),
        ]
        for name, form, (plain, bare, located) in cases:
            asked = {'bare': bare, 'located': located, **({'plain': plain} if plain is not None else {})}
            found = {which: await grid_plots(r, made[which], form) for which in asked}
            assert found == asked, (name, found)
        # One filter card holding the last text: one chip, the AND of its parts, which the page couples as a whole.
        for which in ('bare', 'located'):
            dataset_id, plots, _ = made[which]
            parts = await nodes(r, 'location not within "XA-NM" AND label = "crate"', dataset_id)
            card = {'logic': 'AND', 'filters': parts}
            kept = await kept_plots(r, dataset_id, plots, {'logic': 'AND', 'filters': [card]}, True)
            assert kept == (EVERY_PLACE if which == 'bare' else OUTSIDE_PLACE), (which, kept)
        # The assistant, given the same text: its stages are the text's top-level parts.
        await open_door(r)
        for which in ('bare', 'located'):
            dataset_id, plots, _ = made[which]
            text = 'location not within "XA-NM" AND label = "crate"'
            view = await result(ADMIN, 'make_view', {'dataset': dataset_id, 'filter': text})
            kept = {plots[i] for i in await view_ids(ADMIN, view['view'])}
            assert (view['count'], kept) == (len(OUTSIDE_PLACE), OUTSIDE_PLACE), (which, view['count'], kept)

    regions_world(scenario)


# Contract 3.4: a filter that is one place exclusion is guarded however it is wrapped.
REGION_OUT = place('geo_region_exclude', 'XA-NM')
POLYGON_OUT = place('geo_polygon_exclude', SQUARE)


def one_rule(node: dict[str, Any]) -> dict[str, Any]:
    """The node as the only rule of an AND group, as the Builder tab saves a lone filter."""
    return {'logic': 'AND', 'filters': [node]}


# (what the stage is, the stage's filter): the bare exclusions, and each as the only rule of a group (once, twice)
LONE_EXCLUSIONS: list[tuple[str, dict[str, Any]]] = [
    ('bare geo_region_exclude', REGION_OUT),
    ('geo_region_exclude, the only rule of an AND group', one_rule(REGION_OUT)),
    ('geo_region_exclude, in a one-rule group in a one-rule group', one_rule(one_rule(REGION_OUT))),
    ('bare geo_polygon_exclude', POLYGON_OUT),
    ('geo_polygon_exclude, the only rule of an AND group', one_rule(POLYGON_OUT)),
]


def test_the_server_keeps_the_same_images_for_a_lifted_place_exclusion_however_its_inside_is_grouped(regions_world):
    """What the server itself decides (contract 3.4), a requirement that passes on main. The wrappers (the page's
    withGrain, the assistant's image_grain) lift a place exclusion out and couple it as NOT (the image or one of its
    objects is inside); a wrapper that lifts a wrapped one may hand the server the place test still in its groups. The
    server keeps the five images for that form whatever the grouping, on both datasets with objects.

    Not tested here: whether a wrapper lifts a wrapped exclusion. The suite's own copy of the page's wrapper
    (test_assistant_views_spec.image_grain) lifts only bare exclusions, as main's withGrain does, so a test through it
    would test the copy, not the page, and would need the same fix in the copy to pass. The real wrappers are tested
    where they run: the assistant's through a saved view (the next test), both through the cases they share
    (frontend/tests/fixtures/image-grain-cases.json), and the page in the browser
    (frontend/e2e/place-exclusion-builder.spec.ts)."""

    async def scenario(r):
        made = await placed(r)
        insides = [
            ('the region', place('geo_region', 'XA-NM')),
            ('the region in a group', one_rule(place('geo_region', 'XA-NM'))),
            ('the region in a group in a group', one_rule(one_rule(place('geo_region', 'XA-NM')))),
            ('the polygon in a group', one_rule(place('geo_polygon', SQUARE))),
        ]
        for name, inside in insides:
            node = {'logic': 'AND', 'filters': [inside]}
            lifted = {
                'logic': 'AND',
                'filters': [
                    {'field': 'media_type', 'operator': '!=', 'value': 'object'},
                    {
                        'logic': 'NOT',
                        'filters': [
                            either(
                                {'field': '_cgself', 'operator': 'cg_self', 'value': node},
                                {'field': '_crossgrain', 'operator': 'has_matching_object', 'value': node},
                            ),
                        ],
                    },
                ],
            }
            for which in ('bare', 'located'):
                dataset_id, plots, _ = made[which]
                assert await kept_plots(r, dataset_id, plots, lifted, False) == OUTSIDE_PLACE, (name, which)

    regions_world(scenario)


def test_a_saved_view_whose_stage_is_a_wrapped_place_exclusion_keeps_for_the_assistant_what_the_bare_one_keeps(
    regions_world,
):
    """Contract 3.4, a requirement (fails on main for the wrapped stages on the dataset whose objects carry no
    location). A wrapped exclusion reaches the assistant's wrapper (backend/assistant/grain.py) through its public
    surface as a saved view's stage, which open_saved_view opens as a working view: its count and its samples
    (get_samples) are the five images on every dataset, as for the bare stage. A polygon has no text form, so only a
    saved view, an f= link or another program writes one wrapped; it is checked the same way."""

    async def scenario(r):
        made = await placed(r)
        await open_door(r)
        for number, (name, stage) in enumerate(LONE_EXCLUSIONS):
            for which, (dataset_id, plots, _) in made.items():
                content = {
                    'filters': {'logic': 'AND', 'filters': [stage]},
                    'viewStages': [{'type': 'map_region', 'description': 'Not in XA-NM', 'filter': stage}],
                }
                saved = await ok(
                    await post(
                        r,
                        f'/api/datasets/{dataset_id}/views',
                        {'name': f'out-{number}-{which}', 'content': content, 'is_public': False},
                    ),
                )
                view = await result(ADMIN, 'open_saved_view', {'slug': saved['slug']})
                kept = {plots[i] for i in await view_ids(ADMIN, view['view'])}
                assert (view['count'], kept) == (len(OUTSIDE_PLACE), OUTSIDE_PLACE), (name, which, view['count'], kept)

    regions_world(scenario)


TWO_PLAIN, TWO_OBJECTS = 80, 81
# The images: (plot, colour, n, whether it has an object (on the dataset with objects)); None: no such field.
TWO_FIELDS: list[tuple[str, Optional[str], Optional[int], bool]] = [
    ('red-0', 'red', 0, True),
    ('red-0-alone', 'red', 0, False),
    ('red-1', 'red', 1, True),
    ('blue-0', 'blue', 0, True),
    ('neither', None, None, True),
]


def test_today_a_not_over_two_fields_brings_an_image_back_through_its_object_pin_expected_to_change(regions_world):
    """A pin of today's behaviour (contract 3.2's first point), expected to change when the guard covers a NOT over
    several fields: on a dataset with objects `NOT (colour = "red" AND n = 0)` is true of an object that has neither
    field, so it keeps the image whose own metadata is colour red and n 0 when that image has an object; on the same
    images without objects that image is dropped."""

    async def scenario(r):
        found = {}
        for dataset_id, objects in ((TWO_PLAIN, False), (TWO_OBJECTS, True)):
            await r.dataset(dataset_id)
            samples = []
            for plot, colour, n, _ in TWO_FIELDS:
                metadata: dict[str, Any] = {'plot': plot}
                if colour is not None:
                    metadata['colour'] = colour
                if n is not None:
                    metadata['n'] = n
                samples.append({'filepath': f'{plot}.jpg', 'metadata': metadata})
            await datasets.add_samples(dataset_id, {'samples': samples}, user=ADMIN_USER)
            rows = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', dataset_id)
            plots = {row['id']: row['filepath'][: -len('.jpg')] for row in rows}
            if objects:
                having = {plot for plot, _, _, has in TWO_FIELDS if has}
                children = [
                    {'filepath': f'{plot}-object.jpg', 'media_type': 'object', 'metadata': {'parent_id': image}}
                    for image, plot in plots.items()
                    if plot in having
                ]
                await datasets.add_samples(dataset_id, {'samples': children}, user=ADMIN_USER)
            filters = await nodes(r, 'NOT (colour = "red" AND n = 0)', dataset_id)
            assert len(filters) == 1 and filters[0]['logic'] == 'NOT', filters
            found[objects] = await kept_plots(r, dataset_id, plots, {'logic': 'AND', 'filters': filters}, objects)
        assert found[False] == {'red-1', 'blue-0', 'neither'}, found[False]
        # Today: red-0 comes back through its object, which has neither field; red-0-alone, with no object, doesn't.
        assert found[True] == {'red-0', 'red-1', 'blue-0', 'neither'}, found[True]

    regions_world(scenario)


def sentences(text: str) -> list[str]:
    return re.split(r'(?<=[.;:])\s+', ' '.join(text.split()))


def prose(text: str) -> str:
    """The text without its tables (the operators table's "all but objects" is about fields of the object kind)."""
    return '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('|'))


NEGATION = re.compile(r'\b(negation|negated|not|NOT)\b|!=')


def test_the_docs_say_what_a_negation_keeps_on_each_kind_of_dataset():
    text = (ROOT / 'docs/FILTER_QUERY.md').read_text()
    # No sentence keeps "the samples the concept has no score for" without saying which kind of dataset.
    for found in sentences(text):
        if re.search(r'no score', found, re.I) and re.search(r'\b(keep|keeps|kept|includ\w*)\b', found, re.I):
            assert re.search(r'object', found, re.I), (
                f'says what a concept keeps without naming the kind of dataset: {found}'
            )
    # The rule for datasets with objects, in a sentence.
    rule = [
        found
        for found in sentences(prose(text))
        if re.search(r'objects?\b', found, re.I)
        and NEGATION.search(found)
        and re.search(r'\bonly\b', found, re.I)
        and re.search(r'\b(field|score)s?\b', found, re.I)
    ]
    assert rule, 'no sentence says that on a dataset with objects a negation keeps only the samples that have one'
    # Where a concept is described, and where NOT is.
    concepts = prose(section(text, '## Concept scores'))
    assert re.search(r'\bobjects?\b', concepts, re.I), concepts
    operators = prose(section(text, '## Operators'))
    assert re.search(r'\bobjects\b', operators, re.I), operators


# --- 4. describe_dataset ---

DESCRIBED = 40
# The images: (crop, yield, wet, tags); None: no such field.
DESCRIBED_IMAGES: list[tuple[Optional[str], Optional[float], Optional[bool], list[str]]] = [
    ('barley', 0.5, True, ['checked']),
    ('barley', 2.0, False, []),
    ('barley', 3.5, True, ['Review']),
    ('corn', 1.25, None, []),
    ('corn', None, False, ['checked']),
    ('wheat', 7.75, True, []),
    (None, 4.0, None, []),
]
# The objects: (the image it is under, its label).
DESCRIBED_OBJECTS: list[tuple[int, str]] = [(0, 'crate'), (2, 'bale'), (3, 'crate'), (3, 'bale'), (5, 'crate')]
LOCATED_IMAGES, LOCATED_OBJECTS = (0, 1, 3), (0,)  # with locations and embeddings: which rows have a location
EMBEDDED = {'clip': (0, 1, 2, 3, 4), 'dino': (6,)}  # and which images have a vector of each field
COMBINATIONS = [(False, False), (True, False), (False, True), (True, True)]  # (objects, locations and embeddings)
COMBINATION_IDS = ['plain', 'objects', 'located-embedded', 'objects-located-embedded']


def taken(k: int) -> datetime:
    return datetime(2026, 3, 1 + k, 8, 30, tzinfo=timezone.utc)


@dataclass
class Described:
    objects: bool
    extras: bool
    images: list[int]
    children: list[int]  # the objects' ids, in DESCRIBED_OBJECTS' order


async def described_dataset(r: Regions, objects: bool, extras: bool) -> Described:
    await r.dataset(DESCRIBED)
    samples = []
    for k, (crop, harvest, wet, tags) in enumerate(DESCRIBED_IMAGES):
        metadata: dict[str, Any] = {}
        for key, value in (('crop', crop), ('yield', harvest), ('wet', wet)):
            if value is not None:
                metadata[key] = value
        sample: dict[str, Any] = {
            'filepath': f'd{k}.jpg',
            'metadata': metadata,
            'tags': tags,
            'created_at': taken(k).isoformat(),
        }
        if extras and k in LOCATED_IMAGES:
            sample['location'] = {'type': 'Point', 'coordinates': [1.0 + k, 2.0 + k]}
        samples.append(sample)
    await datasets.add_samples(DESCRIBED, {'samples': samples}, user=ADMIN_USER)
    by_path = {
        row['filepath']: row['id']
        for row in await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', DESCRIBED)
    }
    images = [by_path[f'd{k}.jpg'] for k in range(len(DESCRIBED_IMAGES))]
    children: list[int] = []
    if objects:
        rows = []
        for j, (parent, label) in enumerate(DESCRIBED_OBJECTS):
            row: dict[str, Any] = {
                'filepath': f'd-object-{j}.jpg',
                'media_type': 'object',
                'metadata': {'parent_id': images[parent], 'label': label},
                'created_at': taken(10 + j).isoformat(),
            }
            if extras and j in LOCATED_OBJECTS:
                row['location'] = {'type': 'Point', 'coordinates': [-5.0, -6.0]}
            rows.append(row)
        await datasets.add_samples(DESCRIBED, {'samples': rows}, user=ADMIN_USER)
        by_path = {
            row['filepath']: row['id']
            for row in await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', DESCRIBED)
        }
        children = [by_path[f'd-object-{j}.jpg'] for j in range(len(DESCRIBED_OBJECTS))]
    if extras:
        vectors = [
            {'sample_id': images[k], 'vector': [1.0, 0.5 * k, 0.0], 'field_name': field}
            for field, which in EMBEDDED.items()
            for k in which
        ]
        assert (await post(r, f'/api/datasets/{DESCRIBED}/embeddings', vectors)).is_success
        await r.database.execute(
            'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
            "VALUES ($1, 'equipment', 'clip', 'seed', '[1,0,0]', 0.5)",
            DESCRIBED,
        )
        await r.database.execute(
            'INSERT INTO views (dataset_id, user_id, name, slug, content, is_public) '
            'VALUES ($1, $2, $3, $4, $5, FALSE)',
            DESCRIBED,
            ADMIN,
            'Barley',
            'barley-40',
            {'filters': []},
        )
    await count_statistics(r, DESCRIBED)
    await open_door(r)
    return Described(objects, extras, images, children)


def built_in(name: str, kind: str, indexed: bool, count: Optional[int], value_range: Any, values: Any) -> dict:
    return {
        'name': name,
        'type': kind,
        'indexed': indexed,
        'description': DEFAULTS[name][0],
        'unit': DEFAULTS[name][1],
        'description_source': 'default',
        'samples_with_value': count,
        'range': value_range,
        'values': values,
        'values_private': False,
        'complete': True,
    }


def own(name: str, kind: str, count: int, value_range: Any = None, values: Any = None) -> dict:
    return {
        'name': name,
        'type': kind,
        'indexed': False,
        'description': None,
        'unit': None,
        'description_source': None,
        'samples_with_value': count,
        'range': value_range,
        'values': values,
        'values_private': False,
        'complete': True,
    }


def expected_answer(d: Described) -> dict[str, Any]:
    """describe_dataset's answer as main gives it, from the planted rows."""
    rows = len(d.images) + len(d.children)
    every_id = d.images + d.children
    kinds = ['image', 'object'] if d.objects else ['image']
    tags = sorted({tag for _, _, _, tags in DESCRIBED_IMAGES for tag in tags}, key=str.lower)
    times = [taken(k) for k in range(len(DESCRIBED_IMAGES))] + [taken(10 + j) for j in range(len(d.children))]
    crops = [crop for crop, _, _, _ in DESCRIBED_IMAGES if crop is not None]
    yields = [harvest for _, harvest, _, _ in DESCRIBED_IMAGES if harvest is not None]
    wets = [wet for _, _, wet, _ in DESCRIBED_IMAGES if wet is not None]
    fields = [  # the built-in fields, in main's order
        built_in('media_type', 'text', True, rows, None, {'distinct': len(kinds), 'examples': kinds}),
        built_in('tags', 'list', True, None, None, {'distinct': len(tags), 'examples': tags[:5]}),
        built_in(
            'created_at',
            'datetime',
            True,
            None,
            {'min': min(times).isoformat(), 'max': max(times).isoformat()},
            None,
        ),
        built_in('last_modified_at', 'datetime', False, None, None, None),
        built_in('id', 'number', True, rows, {'min': min(every_id), 'max': max(every_id)}, None),
        built_in('filepath', 'text', True, rows, None, None),
    ]
    own_fields = {
        'crop': own('crop', 'text', len(crops), values={'distinct': len(set(crops)), 'examples': sorted(set(crops))}),
        'yield': own('yield', 'number', len(yields), {'min': min(yields), 'max': max(yields)}),
        'wet': own('wet', 'boolean', len(wets)),
    }
    if d.objects:
        labels = sorted({label for _, label in DESCRIBED_OBJECTS})
        parents = [d.images[parent] for parent, _ in DESCRIBED_OBJECTS]
        own_fields['label'] = own(
            'label',
            'text',
            len(d.children),
            values={'distinct': len(labels), 'examples': labels},
        )
        own_fields['parent_id'] = own(
            'parent_id',
            'number',
            len(d.children),
            {'min': min(parents), 'max': max(parents)},
        )
    fields += [own_fields[name] for name in sorted(own_fields)]
    located = (len(LOCATED_IMAGES) + (len(LOCATED_OBJECTS) if d.objects else 0)) if d.extras else 0
    return {
        'dataset': {
            'id': DESCRIBED,
            'name': f'dataset-{DESCRIBED}',
            'description': None,
            'samples': len(d.images),
            'objects': len(d.children),
            'level': 'manage',
        },
        'fields': fields,
        'fields_total': len(fields),
        'offset': 0,
        'next_offset': None,
        'undescribed_fields': len(own_fields),
        'samples_with_location': located,
        'embedding_fields': sorted(EMBEDDED) if d.extras else [],
        'concepts': 1 if d.extras else 0,
        'saved_views': 1 if d.extras else 0,
    }


@pytest.mark.parametrize(('objects', 'extras'), COMBINATIONS, ids=COMBINATION_IDS)
def test_describe_dataset_answers_as_today(regions_world, objects, extras):
    """Pins main: passes before the fix. Key for key and value for value, the fields in main's order."""

    async def scenario(r):
        d = await described_dataset(r, objects, extras)
        answer = await result(ADMIN, 'describe_dataset', {'dataset': DESCRIBED, 'limit': 100})
        expected = expected_answer(d)
        assert set(answer) == set(expected), answer
        for key in expected:
            if key != 'fields':
                assert answer[key] == expected[key], (key, answer[key])
        assert [field['name'] for field in answer['fields']] == [field['name'] for field in expected['fields']]
        for found, wanted in zip(answer['fields'], expected['fields']):
            assert found == wanted, (found, wanted)
        # A page of it is the same fields.
        page = await result(ADMIN, 'describe_dataset', {'dataset': DESCRIBED, 'limit': 2, 'offset': 1})
        assert page['fields'] == expected['fields'][1:3] and page['next_offset'] == 3, page

    regions_world(scenario)


PENDING = 41


def test_describe_dataset_says_complete_only_when_the_tags_and_media_types_it_shows_are_whole(regions_world):
    """Contract 4.2: before a dataset's first statistics count, and while changes wait to be counted, `complete` is
    false whenever the values shown for tags and media_type may be missing one. Read as: `complete: true` means the
    values are whole (`distinct` is the number of different values the rows have, the examples are those values, at
    most five); `complete: false` means no `distinct` and examples that are values the rows have."""

    async def scenario(r):
        await r.dataset(PENDING)
        await open_door(r)

        async def check(when: str) -> None:
            answer = await result(ADMIN, 'describe_dataset', {'dataset': PENDING, 'limit': 100})
            fields = {field['name']: field for field in answer['fields']}
            rows = await r.database.fetch('SELECT media_type, tags FROM samples WHERE dataset_id = $1', PENDING)
            for name, whole in (
                ('media_type', {row['media_type'] for row in rows}),
                ('tags', {tag for row in rows for tag in row['tags']}),
            ):
                field = fields[name]
                values = field['values']
                if field['complete']:
                    assert values['distinct'] == len(whole), (when, field, sorted(whole))
                    assert set(values['examples']) == whole and len(whole) <= 5, (when, field, sorted(whole))
                else:
                    assert values is None or values['distinct'] is None, (when, field)
                    assert values is None or set(values['examples']) <= whole, (when, field, sorted(whole))

        samples = [{'filepath': f'p{k}.jpg', 'metadata': {'k': k}, 'tags': ['alpha'] if k else []} for k in range(4)]
        await datasets.add_samples(PENDING, {'samples': samples}, user=ADMIN_USER)
        await check('before the first count')
        await count_statistics(r, PENDING)
        await check('counted')
        late = [{'filepath': 'late.mp4', 'media_type': 'video', 'metadata': {'k': 9}, 'tags': ['Zeta']}]
        await datasets.add_samples(PENDING, {'samples': late}, user=ADMIN_USER)
        await check('a new tag and a new media type waiting to be counted')
        await count_statistics(r, PENDING)
        await check('counted again')

    regions_world(scenario)


PINNED_KEYS = {'name', 'type', 'values', 'min', 'max', 'sample_count', 'null_count'}
DATASET_KEYS = {
    'id',
    'deleting',
    'deletion_job_id',
    'name',
    'description',
    'media_fields',
    'grid_media_field',
    'modal_media_field',
    'segmentation_config',
    'storage_config_id',
    'owner_id',
    'is_global',
    'everyone_level',
    'assistant_closed',
    'access_level',
    'created_at',
    'sidebar_expand_all',
    'default_view_id',
    'default_view',
    'map_layouts',
    'mirror',
}


def pinned(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in entry.items() if key in PINNED_KEYS} for entry in entries]


@pytest.mark.parametrize(('objects', 'extras'), COMBINATIONS, ids=COMBINATION_IDS)
def test_the_routes_describe_dataset_reads_answer_the_app_as_today(regions_world, objects, extras):
    """Pins main (contract 4.3): passes before the fix. The counts and values of GET /datasets/{id}, /schema (as
    describe_dataset asks for it), /field-stats and /coverage, from the planted rows."""

    async def scenario(r):
        d = await described_dataset(r, objects, extras)
        rows = len(d.images) + len(d.children)
        every_id = d.images + d.children
        kinds = ['image', 'object'] if d.objects else ['image']
        tags = {tag for _, _, _, tags in DESCRIBED_IMAGES for tag in tags}
        times = [taken(k) for k in range(len(DESCRIBED_IMAGES))] + [taken(10 + j) for j in range(len(d.children))]
        created = {'min': min(times).isoformat(), 'max': max(times).isoformat()}
        crops = [crop for crop, _, _, _ in DESCRIBED_IMAGES if crop is not None]
        yields = [harvest for _, harvest, _, _ in DESCRIBED_IMAGES if harvest is not None]
        wets = [wet for _, _, wet, _ in DESCRIBED_IMAGES if wet is not None]
        # (name, schema's type and values or range, field-stats' count) of the dataset's own fields
        own_fields: dict[str, tuple[dict[str, Any], int]] = {
            'crop': ({'type': 'enum', 'values': sorted(set(crops))}, len(crops)),
            'yield': ({'type': 'number', 'min': min(yields), 'max': max(yields)}, len(yields)),
            'wet': ({'type': 'boolean'}, len(wets)),
        }
        if d.objects:
            parents = [d.images[parent] for parent, _ in DESCRIBED_OBJECTS]
            labels = sorted({label for _, label in DESCRIBED_OBJECTS})
            own_fields['label'] = ({'type': 'enum', 'values': labels}, len(d.children))
            own_fields['parent_id'] = ({'type': 'number', 'min': min(parents), 'max': max(parents)}, len(d.children))
        names = sorted(own_fields)

        dataset = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{DESCRIBED}'))
        assert set(dataset) == DATASET_KEYS, dataset
        assert (dataset['id'], dataset['access_level'], dataset['sidebar_expand_all']) == (DESCRIBED, 'manage', True)

        schema = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{DESCRIBED}/schema', params={'stale': 'true'}))
        assert pinned(schema) == [
            {'name': 'media_type', 'type': 'enum', 'values': kinds},
            {'name': 'tags', 'type': 'array', 'values': sorted(tags, key=str.lower)},
            {'name': 'created_at', 'type': 'datetime', **created},
            {'name': 'last_modified_at', 'type': 'datetime'},
            {'name': 'id', 'type': 'number', 'min': min(every_id), 'max': max(every_id)},
            {'name': 'filepath', 'type': 'filepath'},
            *({'name': name, **own_fields[name][0]} for name in names),
        ]

        stats = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{DESCRIBED}/field-stats'))
        counted = []
        for name in names:
            described, count = own_fields[name]
            counted.append({'name': name, **described, 'sample_count': count, 'null_count': rows - count})
        assert pinned(stats) == [
            {'name': 'id', 'type': 'number'},
            {'name': 'filepath', 'type': 'filepath'},
            {'name': 'created_at', 'type': 'datetime', **created},
            {'name': 'last_modified_at', 'type': 'datetime'},
            {'name': 'media_type', 'type': 'enum', 'values': kinds},
            {'name': 'tags', 'type': 'enum', 'values': sorted(tags)},
            *counted,
        ]

        located = (len(LOCATED_IMAGES) + (len(LOCATED_OBJECTS) if d.objects else 0)) if d.extras else 0
        embedded = sorted(EMBEDDED.items()) if d.extras else []
        coverage = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{DESCRIBED}/coverage'))
        assert coverage == {
            'total_samples': rows,
            'total_objects': len(d.children),
            'embeddings': [
                {'field': field, 'present': len(which), 'missing': rows - len(which)} for field, which in embedded
            ],
            'hashes': [],
            'location': {'present': located, 'missing': rows - located},
        }

    regions_world(scenario)


# Every public method of an asyncpg connection that is handed a statement's text (a pool's methods reach these).
STATEMENT_METHODS = (
    'execute',
    'executemany',
    'fetch',
    'fetchrow',
    'fetchval',
    'fetchmany',
    'prepare',
    'cursor',
    'copy_from_query',
)
QUOTED = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)
SAMPLES_TABLE = re.compile(r'\bsamples\b', re.I)


def reads_samples(statement: str) -> bool:
    """Whether the statement's text names the table `samples` (quoted literals and comments taken out)."""
    return bool(SAMPLES_TABLE.search(QUOTED.sub(' ', statement)))


# One selected item that an index's ends answer: min or max of id or created_at, maybe qualified, maybe named.
INDEX_END = r'(?:min|max)\(\s*(?:\w+\.)?(?:id|created_at)\s*\)(?:\s+(?:as\s+)?(?!from\b)\w+)?'
INDEX_ENDS_ONLY = re.compile(
    rf'select\s+{INDEX_END}(?:\s*,\s*{INDEX_END})*\s+from\s+(?:public\.)?samples(?:\s+(?:as\s+)?(?!where\b)\w+)?'
    r'\s+where\s+(?:\w+\.)?dataset_id\s*=\s*(?:\$\d+|\d+)\s*;?',
)


def index_ends_only(statement: str) -> bool:
    """Whether the statement only reads the dataset's smallest and largest id or created_at (see the docstring)."""
    return bool(INDEX_ENDS_ONLY.fullmatch(' '.join(QUOTED.sub(' ', statement).split()).lower()))


@contextlib.contextmanager
def statements() -> Iterator[list[str]]:
    """The text of every statement any asyncpg connection in this process is handed while the block runs."""
    seen: list[str] = []

    def recording(real: Callable[..., Any]) -> Callable[..., Any]:
        def method(self: Any, *args: Any, **kwargs: Any) -> Any:
            text = args[0] if args else kwargs.get('query')
            seen.append(str(text))
            return real(self, *args, **kwargs)

        return method

    with pytest.MonkeyPatch.context() as patch:
        for name in STATEMENT_METHODS:
            patch.setattr(asyncpg.connection.Connection, name, recording(getattr(asyncpg.connection.Connection, name)))
        yield seen


def test_statements_are_counted_where_they_are_sent():
    """The counter itself: only statements naming the samples table read it, and of those only a read of the ends of
    id or created_at for the dataset is exempt."""
    assert reads_samples('SELECT count(*) FROM samples WHERE dataset_id = $1')
    assert reads_samples('SELECT h.x FROM sample_hashes h JOIN samples s ON h.sample_id = s.id')
    assert reads_samples('SELECT min(id) FROM public.samples')
    assert not reads_samples('SELECT sample_count, total_samples FROM dataset_field_stats')
    assert not reads_samples("SELECT 1 -- from samples\nWHERE name = 'samples'")
    for exempt in (
        'SELECT MIN(created_at) AS mn, MAX(created_at) AS mx FROM samples WHERE dataset_id = $1',
        'SELECT min(created_at) as min_val, max(created_at) as max_val FROM samples WHERE dataset_id = $1',
        'SELECT min(id) as min_val, max(id) as max_val FROM samples WHERE dataset_id = $1',
        'SELECT min(s.id), max(s.created_at) last FROM samples s WHERE s.dataset_id = $2;',
        'SELECT max(id)\nFROM samples\nWHERE dataset_id = 40',
    ):
        assert reads_samples(exempt) and index_ends_only(exempt), exempt
    for counted in (
        'SELECT count(*) FROM samples WHERE dataset_id = $1',
        'SELECT count(*) FROM (SELECT 1 FROM samples WHERE dataset_id = $1 LIMIT $2) s',
        'SELECT DISTINCT media_type FROM samples WHERE dataset_id = $1',
        'SELECT min(id) FROM samples WHERE dataset_id = $1 AND location IS NOT NULL',
        'SELECT min(id), max(id), count(*) FROM samples WHERE dataset_id = $1',
        'SELECT min(lon), max(lat) FROM samples WHERE dataset_id = $1',
        'SELECT min(id) FROM samples',
        'SELECT min(id) FROM samples WHERE dataset_id = $1 GROUP BY media_type',
        'SELECT min(s.id) FROM samples s JOIN sample_hashes h ON h.sample_id = s.id WHERE s.dataset_id = $1',
        'SELECT (SELECT count(*) FROM samples WHERE dataset_id = $1) AS n, min(id) FROM samples WHERE dataset_id = $1',
        "SELECT (SELECT count(*) FROM samples WHERE dataset_id = $1 AND location IS NOT NULL) AS located, count(*) AS "
        "total, count(*) FILTER (WHERE media_type = 'object') AS objects FROM samples WHERE dataset_id = $1",
    ):
        assert reads_samples(counted) and not index_ends_only(counted), counted


@pytest.mark.parametrize(('objects', 'extras'), COMBINATIONS, ids=COMBINATION_IDS)
def test_describe_dataset_counts_or_scans_the_samples_at_most_once(regions_world, objects, extras):
    async def scenario(r):
        await described_dataset(r, objects, extras)
        count = 'SELECT count(*) FROM samples WHERE dataset_id = $1'
        with statements() as seen:
            pooled = await r.database.fetchval(count, DESCRIBED)
        assert pooled > 0 and [text for text in seen if reads_samples(text)] == [count], seen  # the pool is seen
        with statements() as seen:
            answer = await call(ADMIN, 'describe_dataset', {'dataset': DESCRIBED, 'limit': 100})
        assert answer['ok'], answer
        reading = [' '.join(text.split()) for text in seen if reads_samples(text)]
        counting = [text for text in reading if not index_ends_only(text)]
        assert len(counting) <= 1, (
            f'{len(counting)} of the {len(reading)} statements naming the samples table count or scan them:\n'
            + '\n'.join(counting)
        )

    regions_world(scenario)
