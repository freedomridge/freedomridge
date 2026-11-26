"""A text form for the concept filter in the filter query language (docs/FILTER_QUERY.md).

Written from the contract, not from the code, and tested where a user meets it: the HTTP API (filter_query/parse,
filter_query/format and samples/query), signed in with a real API token, on the test database. The rule:

- `concept "equipment" > 0.62` keeps the samples whose score for the dataset's concept named equipment passes the
  comparison: >, >=, < or <=. The name is quoted, or bare when it is a plain word.
- `not concept "equipment" > 0.62`, and `NOT (concept "equipment" > 0.62)` (brackets around a lone concept test), are
  ONE concept_exclude node: it hides those samples and keeps the rest, samples the concept has no score for included,
  as the hide filter does. A NOT around anything larger is a NOT of its parts.
- `concept "equipment"` alone means the concept's saved comparison and threshold at the moment the text is parsed. A
  concept stores only a threshold, and the app compares with `>`, so it is `> <the saved threshold>`.
- parse returns the nodes the app makes from ids, {field: "_concept", operator: concept_select | concept_exclude,
  value: {concept_id, op, threshold}}; format writes one back with the concept's current name and always an explicit
  comparison and threshold (`not concept "equipment" > 0.62` for a hide node). A node whose concept no longer exists
  has no text form (null). Moving a concept's saved threshold changes neither a parsed node nor its text.
- A concept part combines like any other (AND, OR, brackets), and counts as one comparison toward the 500.
- Mistakes are 400s shaped as the language's others: an unknown name (suggesting the dataset's close names), a
  comparison other than the four, a threshold that isn't a number. A number that isn't finite (`1e999`), a threshold
  or any other, is such a mistake too, never a server error.
- `concept` is a contextual keyword: followed by one of the language's operators it is the field named concept, exactly
  as before, with no backticks; followed by a quoted string or a plain word it is a concept test. A concept whose name
  is one of the language's words is written quoted.
- Only the dataset's own concepts are matched. Renaming a concept changes what format writes, and makes the old text an
  unknown-concept error whose suggestions include the new name when it is close.
- Round trip: parse(format(node)) is the node, and the samples the parsed text selects are exactly those the same filter
  made from ids selects.

Concepts and their scores are rows written straight into embedding_concepts and embedding_concept_scores, as the other
concept tests seed them: filtering reads stored scores, so no model or embedding is needed. Every score, and every
threshold a filter here runs with, is exact in single precision (0.125, 0.625, ...), so a score equal to a threshold
tells > from >=. A concept is renamed with an UPDATE (there is no rename route), its saved threshold moved with the
threshold-only rebuild route, and it is deleted with the delete route.

Where the contract leaves something open, these tests read it this way: an unknown-name error may point at the
`concept` keyword or at the name; a wrong comparison points at the comparison and a wrong number (a threshold or not) at
the number, as the language's other mistakes point at the word that is wrong.
"""

import operator
import os
from typing import Any, Callable, Optional

import httpx
import pytest

from backend.routers import datasets
from backend.tests import test_access_rule_spec, test_field_registry_postgres
from backend.tests.test_access_rule_spec import ADMIN, World

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

ADMIN_USER = {'id': ADMIN, 'role': 'admin', 'username': f'user-{ADMIN}'}
FARM, OTHER = 10, 11  # the dataset the tests query, and another with concepts of its own
OPS = ('>', '>=', '<', '<=')
COMPARE: dict[str, Callable[[float, float], bool]] = {
    '>': operator.gt,
    '>=': operator.ge,
    '<': operator.lt,
    '<=': operator.le,
}
# The farm's samples by name: their split, and the text field named `concept` (None: the sample has no such field).
FARM_SAMPLES: dict[str, tuple[str, Optional[str]]] = {
    's0': ('train', 'a'),
    's1': ('train', 'b'),
    's2': ('val', 'xyz'),
    's3': ('train', None),
    's4': ('val', 'a'),
    's5': ('train', None),
    's6': ('val', 'axe'),
    's7': ('train', 'b'),
}
# Each concept's scores by sample; a sample left out has no score for the concept.
SCORES: dict[str, dict[str, float]] = {
    'equipment': {'s0': 0.875, 's1': 0.625, 's2': 0.25, 's3': 0.125, 's6': 0.75, 's7': 0.5},
    'weeds': {'s0': 0.125, 's1': 0.5, 's3': 0.75, 's4': 0.875, 's6': 0.25, 's7': 0.625},
}
ODD = 'big "rig" \\ yard'  # a name only a quoted string can hold
SAVED = 0.5  # the saved threshold of every concept made here
TOO_MANY = 'a query takes at most 500 comparisons'


class Farm:
    """The two datasets, as an admin sees them: the farm's sample ids by name, and each dataset's concept ids."""

    def __init__(self, w: World):
        self.w = w
        self.ids: dict[str, int] = {}
        self.concepts: dict[str, int] = {}  # the farm's
        self.others: dict[str, int] = {}  # the other dataset's

    def of(self, names: set[str]) -> set[int]:
        return {self.ids[name] for name in names}

    @property
    def everything(self) -> set[int]:
        return set(self.ids.values())

    def passing(self, concept: str, op: str, threshold: float) -> set[int]:
        """The farm's samples whose score for `concept` passes `op threshold`; a sample with no score never does."""
        return self.of({name for name, score in SCORES[concept].items() if COMPARE[op](score, threshold)})

    def split(self, value: str) -> set[int]:
        return self.of({name for name, (split, _) in FARM_SAMPLES.items() if split == value})

    def text(self, test: Callable[[Optional[str]], bool]) -> set[int]:
        return self.of({name for name, (_, text) in FARM_SAMPLES.items() if test(text)})


async def make_concept(w: World, dataset_id: int, name: str, scores: dict[int, float]) -> int:
    concept_id = await w.database.fetchval(
        'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
        "VALUES ($1, $2, 'clip', 'seed', '[1,0]', $3) RETURNING id",
        dataset_id,
        name,
        SAVED,
    )
    for sample_id, score in scores.items():
        await w.database.execute(
            'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, $3)',
            concept_id,
            sample_id,
            score,
        )
    return concept_id


async def add(dataset_id: int, prefix: str, metadata: list[dict[str, Any]]) -> None:
    samples = [{'filepath': f'{prefix}-{index}.jpg', 'metadata': item} for index, item in enumerate(metadata)]
    await datasets.add_samples(dataset_id, {'samples': samples}, user=ADMIN_USER)


async def farm(w: World) -> Farm:
    """The farm (private; the admin manages it) with its samples and concepts, and the other dataset with its own,
    some of the same name."""
    f = Farm(w)
    await w.dataset(FARM)
    await w.dataset(OTHER)
    metadata = []
    for rows, (name, (split, text)) in enumerate(FARM_SAMPLES.items()):
        item: dict[str, Any] = {'split': split, 'plot': name, 'rows': rows}
        if text is not None:
            item['concept'] = text
        metadata.append(item)
    await add(FARM, 'farm', metadata)
    rows = await w.database.fetch("SELECT id, metadata->>'plot' AS plot FROM samples WHERE dataset_id = $1", FARM)
    f.ids = {row['plot']: row['id'] for row in rows}
    assert set(f.ids) == set(FARM_SAMPLES)
    for name, scores in SCORES.items():
        f.concepts[name] = await make_concept(w, FARM, name, {f.ids[plot]: score for plot, score in scores.items()})
    for name in ('in', 'heavy machinery', ODD):
        f.concepts[name] = await make_concept(w, FARM, name, {f.ids['s0']: 0.875})
    await add(OTHER, 'other', [{'split': 'train'} for _ in range(3)])
    other_ids = [row['id'] for row in await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1', OTHER)]
    for name in ('equipment', 'equipmant', 'tractor', 'weeds'):
        f.others[name] = await make_concept(w, OTHER, name, {sample_id: 0.875 for sample_id in other_ids})
    # The app lists each dataset's concepts, at the saved threshold.
    for dataset_id, made in ((FARM, f.concepts), (OTHER, f.others)):
        listed = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/concepts')
        assert listed.status_code == 200, listed.text
        assert {row['name']: (row['id'], row['threshold']) for row in listed.json()} == {
            name: (concept_id, SAVED) for name, concept_id in made.items()
        }
    return f


# --- The HTTP API ---


async def parse(w: World, query: str, dataset_id: int = FARM) -> httpx.Response:
    return await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/filter_query/parse',
        json={'query': query, 'current': []},
    )


async def parsed(w: World, query: str, dataset_id: int = FARM) -> dict[str, Any]:
    """The parse of a query that must parse: `filter` is the AND of `filters`, and each of `parts` is the part as
    format writes it."""
    response = await parse(w, query, dataset_id)
    assert response.status_code == 200, (query, response.text)
    body = response.json()
    assert body['filter'] == {'logic': 'AND', 'filters': body['filters']}, query
    assert len(body['parts']) == len(body['filters']), query
    return body


async def nodes(w: World, query: str, dataset_id: int = FARM) -> list[dict[str, Any]]:
    return (await parsed(w, query, dataset_id))['filters']


async def formatted(w: World, filters: list[dict[str, Any]], dataset_id: int = FARM) -> dict[str, Any]:
    response = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/filter_query/format',
        json={'filters': filters},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body['parts']) == len(filters)
    assert body['query'] == '\nAND '.join(part for part in body['parts'] if part is not None)
    return body


async def text_of(w: World, node: dict[str, Any], dataset_id: int = FARM) -> Optional[str]:
    return (await formatted(w, [node], dataset_id))['parts'][0]


async def selected(w: World, filter_node: dict[str, Any], dataset_id: int = FARM) -> set[int]:
    """The ids of the samples a filter selects, as samples/query answers them."""
    response = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/samples/query',
        json={'limit': 1000, 'filters': filter_node},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    ids = {item['id'] for item in body['items']}
    assert body['total'] == len(ids) == len(body['items'])
    return ids


async def selected_by(w: World, query: str, dataset_id: int = FARM) -> set[int]:
    return await selected(w, (await parsed(w, query, dataset_id))['filter'], dataset_id)


async def assert_written_back(w: World, query: str) -> None:
    """Each part of the query has a text form, and the parts written back parse to the same filters."""
    body = await parsed(w, query)
    assert None not in body['parts'], (query, body['parts'])
    assert await nodes(w, '\nAND '.join(body['parts'])) == body['filters'], (query, body['parts'])


def keep(concept_id: int, op: str, threshold: float) -> dict[str, Any]:
    """The keep filter the app makes from a concept's id."""
    return {
        'field': '_concept',
        'operator': 'concept_select',
        'value': {'concept_id': concept_id, 'op': op, 'threshold': threshold},
    }


def hide(concept_id: int, op: str, threshold: float) -> dict[str, Any]:
    """The hide filter the app makes from a concept's id."""
    return {**keep(concept_id, op, threshold), 'operator': 'concept_exclude'}


def mistake(response: httpx.Response, line: int) -> dict[str, Any]:
    """The one error of a refused query, shaped as the language reports its other mistakes."""
    assert response.status_code == 400, response.text
    detail = response.json()['detail']
    assert set(detail) == {'message', 'errors'}, detail
    [error] = detail['errors']
    assert set(error) == {'message', 'line', 'column', 'suggestions'}, error
    assert isinstance(error['message'], str) and error['message'], error
    assert isinstance(error['column'], int) and isinstance(error['suggestions'], list), error
    assert error['line'] == line, error
    assert detail['message'] == f"line {error['line']}, column {error['column']}: {error['message']}"
    return error


# --- Parse and format ---


def test_a_concept_test_parses_to_the_filter_the_app_makes_from_ids(world):
    async def scenario(w):
        f = await farm(w)
        equipment = f.concepts['equipment']
        for op in OPS:
            for query in (
                f'concept "equipment" {op} 0.62',
                f'concept equipment {op} 0.62',
                f"concept 'equipment' {op} 0.62",
            ):
                assert await nodes(w, query) == [keep(equipment, op, 0.62)], query
        assert await nodes(w, 'concept equipment >= -0.5') == [keep(equipment, '>=', -0.5)]
        assert await nodes(w, 'concept\n  "equipment"\n  <= 1') == [keep(equipment, '<=', 1)]
        assert await nodes(w, 'CONCEPT "equipment" < 0.25') == [keep(equipment, '<', 0.25)]  # keywords: any case
        # Names that aren't plain words, quoted: one with a space, one of the language's words, one with quotes.
        assert await nodes(w, 'concept "heavy machinery" > 0.5') == [keep(f.concepts['heavy machinery'], '>', 0.5)]
        assert await nodes(w, 'concept "in" > 0.5') == [keep(f.concepts['in'], '>', 0.5)]
        assert await nodes(w, 'concept "big \\"rig\\" \\\\ yard" > 0.5') == [keep(f.concepts[ODD], '>', 0.5)]
        # Each part is written back as format writes it.
        body = await parsed(w, 'concept equipment >= 0.62 AND not concept weeds < 0.25')
        assert body['parts'] == [
            await text_of(w, keep(equipment, '>=', 0.62)),
            await text_of(w, hide(f.concepts['weeds'], '<', 0.25)),
        ]

    world(scenario)


def test_format_writes_the_current_name_and_an_explicit_comparison(world):
    async def scenario(w):
        f = await farm(w)
        equipment = f.concepts['equipment']
        for op in OPS:
            assert await text_of(w, keep(equipment, op, 0.62)) == f'concept "equipment" {op} 0.62'
            assert await text_of(w, hide(equipment, op, 0.62)) == f'not concept "equipment" {op} 0.62'
        assert await text_of(w, keep(f.concepts['in'], '>=', 0.5)) == 'concept "in" >= 0.5'
        assert await text_of(w, hide(f.concepts['heavy machinery'], '<', 0.25)) == (
            'not concept "heavy machinery" < 0.25'
        )
        # Beside filters that always had a text form, and one that has none.
        body = await formatted(
            w,
            [
                {'field': 'split', 'operator': '=', 'value': 'train'},
                hide(equipment, '>', 0.62),
                {'field': 'id', 'operator': 'in_selection', 'value': 'token'},
            ],
        )
        assert body == {
            'parts': ['split = "train"', 'not concept "equipment" > 0.62', None],
            'query': 'split = "train"\nAND not concept "equipment" > 0.62',
        }

    world(scenario)


def test_parse_of_format_gives_the_node_back(world):
    async def scenario(w):
        f = await farm(w)
        for name in ('equipment', 'in', 'heavy machinery', ODD):
            for op in OPS:
                for threshold in (0.62, -0.5, 0, 2.5):
                    for node in (keep(f.concepts[name], op, threshold), hide(f.concepts[name], op, threshold)):
                        text = await text_of(w, node)
                        assert text is not None, node
                        assert await nodes(w, text) == [node], text

    world(scenario)


def test_not_and_brackets_around_one_concept_test_are_one_hide_filter(world):
    async def scenario(w):
        f = await farm(w)
        equipment = f.concepts['equipment']
        for op in OPS:
            for query in (
                f'not concept "equipment" {op} 0.62',
                f'NOT concept equipment {op} 0.62',
                f'NOT (concept "equipment" {op} 0.62)',
                f'not (concept equipment {op} 0.62)',
                f'NOT ( concept "equipment" {op} 0.62 )',
            ):
                body = await parsed(w, query)
                assert body['filters'] == [hide(equipment, op, 0.62)], query
                assert body['parts'] == [f'not concept "equipment" {op} 0.62'], query
            # Brackets alone keep it a keep filter.
            assert await nodes(w, f'(concept "equipment" {op} 0.62)') == [keep(equipment, op, 0.62)]

    world(scenario)


def test_the_bare_form_takes_the_saved_comparison_and_threshold_when_parsed(world):
    async def scenario(w):
        f = await farm(w)
        equipment = f.concepts['equipment']
        saved = keep(equipment, '>', SAVED)
        [parsed_before] = await nodes(w, 'concept "equipment"')
        assert parsed_before == saved
        assert await nodes(w, 'concept equipment') == [saved]
        assert await nodes(w, 'concept equipment AND split = "train"') == [
            saved,
            {'field': 'split', 'operator': '=', 'value': 'train'},
        ]
        assert await nodes(w, 'split = "val" AND concept "equipment"') == [
            {'field': 'split', 'operator': '=', 'value': 'val'},
            saved,
        ]
        assert await nodes(w, 'not concept "equipment"') == [hide(equipment, '>', SAVED)]
        assert await nodes(w, 'NOT (concept equipment)') == [hide(equipment, '>', SAVED)]
        # Format writes the explicit form for it.
        assert (await parsed(w, 'concept "equipment"'))['parts'] == ['concept "equipment" > 0.5']
        assert await text_of(w, saved) == 'concept "equipment" > 0.5'
        assert await text_of(w, hide(equipment, '>', SAVED)) == 'not concept "equipment" > 0.5'
        assert await selected_by(w, 'concept "equipment"') == f.passing('equipment', '>', SAVED)

        # The saved threshold moved: the node parsed before, and its text, are as they were; parsing again takes the
        # new one.
        moved = await w.call(
            ADMIN,
            'POST',
            f'/api/datasets/{FARM}/concepts/{equipment}/rebuild',
            json={'threshold': 0.75},
        )
        assert moved.status_code == 200, moved.text
        assert parsed_before == saved
        assert await text_of(w, parsed_before) == 'concept "equipment" > 0.5'
        assert await selected(w, parsed_before) == f.passing('equipment', '>', SAVED)
        assert await nodes(w, 'concept "equipment"') == [keep(equipment, '>', 0.75)]
        assert await nodes(w, 'not concept equipment') == [hide(equipment, '>', 0.75)]
        assert await text_of(w, keep(equipment, '>', 0.75)) == 'concept "equipment" > 0.75'
        assert await selected_by(w, 'concept "equipment"') == f.passing('equipment', '>', 0.75)

    world(scenario)


# --- The samples the text selects ---


def test_keep_and_hide_select_the_samples_the_filters_from_ids_select(world):
    async def scenario(w):
        f = await farm(w)
        equipment = f.concepts['equipment']
        unscored = f.of({'s4', 's5'})  # equipment has no score for them
        cases = [(op, threshold) for op in OPS for threshold in (0.625, 0.5, 0.125)]
        # The filters made from ids select what the seeded scores say.
        for op, threshold in cases:
            kept = f.passing('equipment', op, threshold)
            assert await selected(w, keep(equipment, op, threshold)) == kept, (op, threshold)
            assert unscored <= f.everything - kept
            assert await selected(w, hide(equipment, op, threshold)) == f.everything - kept, (op, threshold)
        # The text selects the same.
        for op, threshold in cases:
            kept = f.passing('equipment', op, threshold)
            assert await selected_by(w, f'concept "equipment" {op} {threshold}') == kept, (op, threshold)
            for query in (
                f'not concept "equipment" {op} {threshold}',
                f'NOT (concept equipment {op} {threshold})',
            ):
                assert await selected_by(w, query) == f.everything - kept, query
        # > and >= differ on a score equal to the threshold.
        assert f.ids['s1'] not in await selected_by(w, 'concept "equipment" > 0.625')
        assert f.ids['s1'] in await selected_by(w, 'concept "equipment" >= 0.625')

    world(scenario)


def test_a_not_around_more_than_one_concept_test_is_the_complement_of_its_parts(world):
    async def scenario(w):
        f = await farm(w)
        for inner, expected in (
            ('concept "equipment" > 0.5 AND split = "train"', f.passing('equipment', '>', 0.5) & f.split('train')),
            (
                'concept equipment >= 0.625 OR concept weeds < 0.5',
                f.passing('equipment', '>=', 0.625) | f.passing('weeds', '<', 0.5),
            ),
            (
                'not concept "weeds" > 0.5 AND split = "val"',
                (f.everything - f.passing('weeds', '>', 0.5)) & f.split('val'),
            ),
        ):
            assert await selected_by(w, f'({inner})') == expected, inner
            assert await selected_by(w, f'NOT ({inner})') == f.everything - expected, inner
            assert len(await nodes(w, f'NOT ({inner})')) == 1, inner
            await assert_written_back(w, f'NOT ({inner})')

    world(scenario)


def test_a_concept_part_combines_with_and_or_and_brackets(world):
    async def scenario(w):
        f = await farm(w)
        equipment, weeds = f.concepts['equipment'], f.concepts['weeds']
        train = {'field': 'split', 'operator': '=', 'value': 'train'}
        body = await parsed(w, 'split = "train" AND not concept "equipment" > 0.62')
        assert body['filters'] == [train, hide(equipment, '>', 0.62)]
        assert body['parts'] == ['split = "train"', 'not concept "equipment" > 0.62']

        cases = [
            (
                'split = "train" AND not concept "equipment" > 0.625',
                f.split('train') - f.passing('equipment', '>', 0.625),
                2,
            ),
            ('concept "equipment" >= 0.625 OR split = "val"', f.passing('equipment', '>=', 0.625) | f.split('val'), 1),
            (
                '(split = "val" OR concept weeds > 0.5) AND concept "equipment" <= 0.5',
                (f.split('val') | f.passing('weeds', '>', 0.5)) & f.passing('equipment', '<=', 0.5),
                2,
            ),
            (
                'concept "equipment" > 0.125 AND not concept "weeds" > 0.5',
                f.passing('equipment', '>', 0.125) - f.passing('weeds', '>', 0.5),
                2,
            ),
            (
                'concept "equipment" > 0.5  # parked machines\nOR (not concept weeds <= 0.5 AND split = "train")',
                f.passing('equipment', '>', 0.5) | ((f.everything - f.passing('weeds', '<=', 0.5)) & f.split('train')),
                1,
            ),
        ]
        for query, expected, parts in cases:
            assert len(await nodes(w, query)) == parts, query
            assert await selected_by(w, query) == expected, query
            await assert_written_back(w, query)
        # The same parts made from ids select the same samples.
        assert await selected(
            w,
            {'logic': 'AND', 'filters': [keep(equipment, '>', 0.125), hide(weeds, '>', 0.5)]},
        ) == await selected_by(w, 'concept "equipment" > 0.125 AND not concept "weeds" > 0.5')

    world(scenario)


# --- Mistakes ---


def test_an_unknown_concept_is_a_400_suggesting_the_datasets_close_names(world):
    async def scenario(w):
        f = await farm(w)
        error = mistake(await parse(w, 'split = "train"\nAND concept "equipmnt" > 0.5'), line=2)
        assert error['column'] in (5, 13), error  # the `concept` keyword, or the name
        assert 'equipmnt' in error['message'], error
        assert 'equipment' in error['suggestions'], error
        assert set(error['suggestions']) <= set(f.concepts), error  # the farm's names, not the other dataset's
        error = mistake(await parse(w, 'concept equipmnt'), line=1)
        assert 'equipment' in error['suggestions'], error
        # A name only the other dataset has.
        error = mistake(await parse(w, 'concept "tractor" > 0.5'), line=1)
        assert 'tractor' in error['message'] and set(error['suggestions']) <= set(f.concepts), error

    world(scenario)


def test_a_comparison_that_is_not_one_of_the_four_is_a_400(world):
    async def scenario(w):
        await farm(w)
        for query in (
            'concept "equipment" = 0.5',
            'concept "equipment" == 0.5',
            'concept "equipment" != 0.5',
            'concept "equipment" <> 0.5',
            'concept "equipment" in (0.5 0.6)',
            'concept "equipment" not in (0.5)',
            'concept "equipment" contains "0.5"',
            'concept "equipment" between 0.1 and 0.5',
            'concept "equipment" is null',
        ):
            assert mistake(await parse(w, query), line=1)['column'] == 21, query  # the comparison

    world(scenario)


def test_a_threshold_that_is_not_a_number_is_a_400(world):
    async def scenario(w):
        await farm(w)
        for query in (
            'concept "equipment" > abc',
            'concept "equipment" > "abc"',
            'concept "equipment" > true',
            'concept "equipment" > null',
            'concept "equipment" > 1x',
        ):
            assert mistake(await parse(w, query), line=1)['column'] == 23, query  # the threshold
        mistake(await parse(w, 'concept "equipment" >'), line=1)
        mistake(await parse(w, 'concept "equipment" > AND split = "train"'), line=1)
        error = mistake(await parse(w, 'split = "train"\nAND\nconcept "equipment" >= high'), line=3)
        assert error['column'] == 24, error

    world(scenario)


def test_a_number_that_is_not_finite_is_a_mistake_not_a_server_error(world):
    """For a concept's threshold and for an ordinary number field (`rows`) alike: a 400 that says where."""

    async def scenario(w):
        f = await farm(w)
        # A large number that is finite is a number.
        assert await nodes(w, 'rows < 1e308') == [{'field': 'rows', 'operator': '<', 'value': 1e308}]
        for query, line, column in (
            ('rows > 1e999', 1, 8),
            ('rows < -1e999', 1, 8),
            ('rows = "1e999"', 1, 8),
            ('rows in (1 1e999)', 1, 12),
            ('rows between 0 and 1e999', 1, 20),
            ('split = "train"\nAND rows > 1e999', 2, 12),
            ('concept "equipment" > 1e999', 1, 23),
            ('concept equipment >= -1e999', 1, 22),
            ('not concept equipment <= -1e999', 1, 26),
            ('split = "train"\nAND concept "equipment" < 1e400', 2, 27),
        ):
            assert mistake(await parse(w, query), line=line)['column'] == column, query
        assert await nodes(w, 'concept "equipment" < 1e308') == [keep(f.concepts['equipment'], '<', 1e308)]

    world(scenario)


# --- `concept` the field ---


def test_a_field_named_concept_reads_as_it_did_beside_concept_tests(world):
    """`concept` followed by an operator is the field, with no backticks: parsed, formatted and filtered as before."""

    async def scenario(w):
        f = await farm(w)
        # (query, its filter's operator and value, the filter as format writes it, the samples it selects)
        cases: list[tuple[str, dict[str, Any], str, Optional[set[int]]]] = [
            ('concept = "a"', {'operator': '=', 'value': 'a'}, 'concept = "a"', f.text(lambda v: v == 'a')),
            (
                'concept in ("a", "b")',
                {'operator': 'in', 'value': ['a', 'b']},
                'concept in ("a", "b")',
                f.text(lambda v: v in ('a', 'b')),
            ),
            (
                'concept exists',
                {'operator': 'exists', 'value': None},
                'concept exists',
                f.text(lambda v: v is not None),
            ),
            (
                'concept contains "x"',
                {'operator': 'contains', 'value': 'x'},
                'concept contains "x"',
                f.text(lambda v: v is not None and 'x' in v),
            ),
            ('concept like "%x%"', {'operator': 'contains', 'value': 'x'}, 'concept contains "x"', None),
            ('concept != "a"', {'operator': '!=', 'value': 'a'}, 'concept != "a"', None),
            ('concept not in ("a")', {'operator': 'not in', 'value': ['a']}, 'concept not in ("a")', None),
            ('concept not exists', {'operator': 'not_exists', 'value': None}, 'concept not exists', None),
            ('concept is null', {'operator': 'is_null', 'value': None}, 'concept is null', None),
            ('concept is not null', {'operator': 'is_not_null', 'value': None}, 'concept is not null', None),
            ('`concept` = "a"', {'operator': '=', 'value': 'a'}, 'concept = "a"', None),
        ]
        for query, node, text, samples in cases:
            field_node = {'field': 'concept', **node}
            assert await nodes(w, query) == [field_node], query
            assert await text_of(w, field_node) == text, query
            if samples is not None:
                assert await selected_by(w, query) == samples, query

        # Beside concept tests in one query.
        query = (
            '(concept = "a" OR concept "equipment" > 0.5)\n'
            'AND concept in ("a", "b")\n'
            'AND not concept "weeds" >= 0.5\n'
            'AND concept exists\n'
            'AND concept contains "a"'
        )
        body = await parsed(w, query)
        assert body['filters'][1:] == [
            {'field': 'concept', 'operator': 'in', 'value': ['a', 'b']},
            hide(f.concepts['weeds'], '>=', 0.5),
            {'field': 'concept', 'operator': 'exists', 'value': None},
            {'field': 'concept', 'operator': 'contains', 'value': 'a'},
        ]
        assert body['filters'][0] == {
            'logic': 'OR',
            'filters': [
                {'field': 'concept', 'operator': '=', 'value': 'a'},
                keep(f.concepts['equipment'], '>', 0.5),
            ],
        }
        expected = (
            (f.text(lambda v: v == 'a') | f.passing('equipment', '>', 0.5))
            & f.text(lambda v: v in ('a', 'b'))
            & (f.everything - f.passing('weeds', '>=', 0.5))
            & f.text(lambda v: v is not None and 'a' in v)
        )
        assert expected  # the query selects something
        assert await selected_by(w, query) == expected
        await assert_written_back(w, query)

        # A comparison the text field doesn't take is the same mistake as before.
        error = mistake(await parse(w, 'concept > 0.5'), line=1)
        assert (error['message'], error['column']) == (
            "'>' does not apply to the text field 'concept'; use one of: "
            '=, !=, in, not in, contains, like, exists, not exists, is null, is not null',
            9,
        )
        # In a dataset with no field named concept, it is an unknown field, as before, at the same place; the message
        # may go on (to say how a concept's score is written).
        error = mistake(await parse(w, 'concept = "a"', OTHER), line=1)
        assert error['column'] == 1, error
        assert error['message'].startswith("unknown field 'concept'"), error

    world(scenario)


# --- The limit ---


def test_a_concept_test_counts_as_one_comparison(world):
    async def scenario(w):
        await farm(w)
        split = 'split = "train"'
        for concept_test in ('concept "equipment" > 0.5', 'not concept "equipment" > 0.5', 'concept equipment'):
            assert len(await nodes(w, ' AND '.join([split] * 499 + [concept_test]))) == 500, concept_test
            error = mistake(await parse(w, ' AND '.join([split] * 500 + [concept_test])), line=1)
            assert TOO_MANY in error['message'], error
        assert len(await nodes(w, ' AND '.join(['concept "equipment" > 0.5'] * 500))) == 500
        error = mistake(await parse(w, ' AND '.join(['concept "equipment" > 0.5'] * 501)), line=1)
        assert TOO_MANY in error['message'], error

    world(scenario)


# --- Whose concept, and which name ---


def test_only_the_datasets_own_concepts_are_matched(world):
    async def scenario(w):
        f = await farm(w)
        assert f.concepts['equipment'] != f.others['equipment']
        assert await nodes(w, 'concept "equipment" > 0.5') == [keep(f.concepts['equipment'], '>', 0.5)]
        assert await nodes(w, 'concept "equipment" > 0.5', OTHER) == [keep(f.others['equipment'], '>', 0.5)]
        assert await nodes(w, 'not concept weeds', OTHER) == [hide(f.others['weeds'], '>', SAVED)]
        mistake(await parse(w, 'concept "heavy machinery" > 0.5', OTHER), line=1)
        # Another dataset's concept has no text form here: its text would name this dataset's concept of that name.
        assert await text_of(w, keep(f.others['equipment'], '>', 0.5)) is None
        assert await text_of(w, hide(f.others['tractor'], '>', 0.5)) is None
        assert await text_of(w, keep(f.others['equipment'], '>', 0.5), OTHER) == 'concept "equipment" > 0.5'
        assert await text_of(w, keep(f.concepts['equipment'], '>', 0.5), OTHER) is None
        # The other dataset's scores, high on all its samples, select none of the farm's.
        assert await selected_by(w, 'concept "equipment" > 0.5') == f.passing('equipment', '>', 0.5)

    world(scenario)


def test_a_renamed_concept_is_written_with_its_new_name(world):
    async def scenario(w):
        f = await farm(w)
        equipment = f.concepts['equipment']
        before = await selected(w, keep(equipment, '>', 0.5))
        await w.database.execute("UPDATE embedding_concepts SET name = 'equipments' WHERE id = $1", equipment)
        assert await selected(w, keep(equipment, '>', 0.5)) == before  # a new name, the same concept
        assert await text_of(w, keep(equipment, '>', 0.62)) == 'concept "equipments" > 0.62'
        assert await text_of(w, hide(equipment, '>', 0.62)) == 'not concept "equipments" > 0.62'
        error = mistake(await parse(w, 'concept "equipment" > 0.62'), line=1)
        assert 'equipments' in error['suggestions'], error
        assert await nodes(w, 'concept "equipments" > 0.62') == [keep(equipment, '>', 0.62)]
        assert await selected_by(w, 'concept equipments > 0.5') == before

    world(scenario)


def test_a_filter_whose_concept_is_gone_has_no_text_form(world):
    async def scenario(w):
        f = await farm(w)
        weeds, equipment = f.concepts['weeds'], f.concepts['equipment']
        deleted = await w.call(ADMIN, 'DELETE', f'/api/datasets/{FARM}/concepts/{weeds}')
        assert deleted.status_code == 200, deleted.text
        body = await formatted(
            w,
            [
                {'field': 'split', 'operator': '=', 'value': 'train'},
                hide(weeds, '>', 0.5),
                keep(equipment, '>', 0.5),
                keep(999999, '>', 0.5),
            ],
        )
        assert body == {
            'parts': ['split = "train"', None, 'concept "equipment" > 0.5', None],
            'query': 'split = "train"\nAND concept "equipment" > 0.5',
        }
        mistake(await parse(w, 'concept "weeds" > 0.5'), line=1)

    world(scenario)
