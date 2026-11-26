"""A concept filter that names a concept the dataset does not have is refused, never applied as "matches nothing" or
"matches everything".

Written from the rule, not from the code, and tested where a user meets it: every reader of filters over the HTTP API,
signed in with a real API token, and the bulk edits a filter can drive. A concept filter is {"field": "_concept",
"operator": "concept_select" | "concept_exclude", "value": {"concept_id", "op", "threshold"}}. For a concept that was
deleted, one that never existed, and one of ANOTHER dataset the same user can read:

- each reader answers 400 with a message that says the concept doesn't exist, whether the filter stands alone, sits
  under OR, or under NOT;
- a bulk tag, set or delete submitted with it is refused when it is submitted: nothing is queued, nothing changes.

A filter on the dataset's own concept works in each reader, as a check that the reader reads the filter at all.
No route of the server applies a saved view's filters itself (the app and the SDK send them to the readers below), so
saved views are covered by the readers. A version's readers refuse every concept filter, a concept's scores being live
data a version does not store, so they are not readers of concept filters.

Where the rule leaves the words open, "says the concept doesn't exist" is read as: the message names a concept and
says it doesn't exist, is unknown or isn't found.
"""

import json
import os
from typing import Any

import httpx
import pytest

from backend import concept_jobs
from backend.routers import datasets
from backend.tests import job_helpers, test_access_rule_spec, test_field_registry_postgres
from backend.tests.test_access_rule_spec import ADMIN, POWER, World

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

ADMIN_USER = {'id': ADMIN, 'role': 'admin', 'username': f'user-{ADMIN}'}
FARM, OTHER = 10, 11
NEVER = 987654
GONE_WORDS = ("doesn't exist", 'does not exist', 'not exist', 'unknown', 'not found', 'no such', 'no longer')


def concept_filter(concept_id: int, operator: str = 'concept_select') -> dict[str, Any]:
    return {'field': '_concept', 'operator': operator, 'value': {'concept_id': concept_id, 'op': '>', 'threshold': 0.5}}


def positions(concept_id: int) -> list[tuple[str, dict[str, Any]]]:
    """The concept filter alone, under OR, under NOT, and deeper."""
    a = {'field': 'grp', 'operator': '=', 'value': 'a'}
    found = []
    for operator in ('concept_select', 'concept_exclude'):
        node = concept_filter(concept_id, operator)
        found += [
            (f'{operator} alone', node),
            (f'{operator} under OR', {'logic': 'OR', 'filters': [a, node]}),
            (f'{operator} under NOT', {'logic': 'NOT', 'filters': [node]}),
            (
                f'{operator} under NOT under AND',
                {'logic': 'AND', 'filters': [a, {'logic': 'NOT', 'filters': [{'logic': 'OR', 'filters': [node, a]}]}]},
            ),
        ]
    return found


class Farm:
    def __init__(self, w: World):
        self.w = w
        self.ids: list[int] = []
        self.own = 0  # the farm's concept
        self.gone = 0  # one the farm had, deleted
        self.others = 0  # the other dataset's


async def farm(w: World) -> Farm:
    """The farm and another dataset, both readable by the power user (edit in the farm, view in the other)."""
    f = Farm(w)
    for dataset_id in (FARM, OTHER):
        await w.dataset(dataset_id)
    await w.share(FARM, POWER, 'edit')
    await w.share(OTHER, POWER, 'view')
    samples = [
        {
            'filepath': f'farm-{index}.jpg',
            'tags': ['seen'],
            'metadata': {'grp': 'ab'[index % 2], 'n': index},
            'location': {'type': 'Point', 'coordinates': [10.0 + index, 50.0]},
        }
        for index in range(6)
    ]
    f.ids = (await datasets.add_samples(FARM, {'samples': samples}, user=ADMIN_USER))['ids']
    other_ids = (await datasets.add_samples(OTHER, {'samples': samples[:2]}, user=ADMIN_USER))['ids']
    vectors = [{'sample_id': s, 'vector': [1.0, i / 10, 0.0, 0.0], 'field_name': 'clip'} for i, s in enumerate(f.ids)]
    assert (await w.call(ADMIN, 'POST', f'/api/datasets/{FARM}/embeddings', json=vectors)).is_success
    coordinates = [{'sample_id': s, 'x': float(i), 'y': float(i % 3)} for i, s in enumerate(f.ids)]
    placed = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{FARM}/embeddings/coordinates',
        params={'field_name': 'clip'},
        json=coordinates,
    )
    assert placed.is_success, placed.text

    async def concept(dataset_id: int, name: str, sample_ids: list[int]) -> int:
        concept_id = await w.database.fetchval(
            'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) '
            "VALUES ($1, $2, 'clip', 'seed', '[1,0,0,0]') RETURNING id",
            dataset_id,
            name,
        )
        for index, sample_id in enumerate(sample_ids):
            await w.database.execute(
                'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, $3)',
                concept_id,
                sample_id,
                0.25 + 0.5 * (index % 2),
            )
        return concept_id

    f.own = await concept(FARM, 'weeds', f.ids)
    f.gone = await concept(FARM, 'stones', f.ids)
    f.others = await concept(OTHER, 'weeds', other_ids)
    deleted = await w.call(ADMIN, 'DELETE', f'/api/datasets/{FARM}/concepts/{f.gone}')
    assert deleted.status_code == 200, deleted.text
    return f


def readers(f: Farm, node: dict[str, Any]) -> list[tuple[str, str, str, dict[str, Any]]]:
    """(name, method, path, request) of each reader of filters, asked with `node`."""
    base = f'/api/datasets/{FARM}'
    query = {'filters': json.dumps(node)}
    return [
        ('samples query', 'POST', f'{base}/samples/query', {'json': {'filters': node, 'limit': 10}}),
        ('count', 'POST', f'{base}/samples/query', {'json': {'filters': node, 'limit': 0}}),
        ('map points', 'POST', f'{base}/map_points', {'json': {'filters': node, 'points': True}}),
        ('map extent', 'POST', f'{base}/map_extent', {'json': {'filters': node}}),
        ('histogram', 'POST', f'{base}/histogram', {'json': {'field': 'n', 'filters': node}}),
        ('histogram, GET', 'GET', f'{base}/histogram', {'params': {'field': 'n', **query}}),
        ('field statistics', 'POST', f'{base}/field-stats', {'json': {'filters': node}}),
        ('field statistics, a facet', 'POST', f'{base}/field-stats', {'json': {'filters': node, 'fields': ['n']}}),
        ('field statistics, GET', 'GET', f'{base}/field-stats', {'params': query}),
        ('schema', 'GET', f'{base}/schema', {'params': query}),
        (
            'aggregate',
            'POST',
            f'{base}/aggregate',
            {'json': {'filters': node, 'aggregations': [{'field': 'n', 'op': 'count'}]}},
        ),
        ('autocomplete', 'POST', f'{base}/fields/grp/autocomplete', {'json': {'q': 'a', 'filters': node}}),
        ('embeddings scatter', 'POST', f'{base}/embeddings/query', {'json': {'field': 'clip', 'filters': node}}),
        (
            'similarity search',
            'POST',
            '/api/search',
            {'json': {'dataset_id': FARM, 'vector': [1.0, 0.0, 0.0, 0.0], 'field_name': 'clip', 'filters': node}},
        ),
        ('selection token', 'POST', f'{base}/selection_token', {'json': {'filters': node}}),
    ]


def says_the_concept_is_gone(response: httpx.Response) -> bool:
    text = response.text.lower()
    return response.status_code == 400 and 'concept' in text and any(words in text for words in GONE_WORDS)


async def misread(w: World, f: Farm, concept_id: int) -> list[str]:
    """Each (reader, position) that did not refuse the concept as unknown."""
    failures = []
    for position, node in positions(concept_id):
        for name, method, path, request in readers(f, node):
            response = await w.call(POWER, method, path, **request)
            if not says_the_concept_is_gone(response):
                failures.append(f'{name}, {position}: {response.status_code} {response.text[:160]}')
    return failures


def test_a_filter_on_the_datasets_own_concept_is_read_by_every_reader(world):
    async def scenario(w):
        f = await farm(w)
        failures = []
        for position, node in positions(f.own):
            for name, method, path, request in readers(f, node):
                response = await w.call(POWER, method, path, **request)
                if not response.is_success:
                    failures.append(f'{name}, {position}: {response.status_code} {response.text[:160]}')
        assert not failures, '\n'.join(failures)

    world(scenario)


def test_a_deleted_concept_is_refused_by_every_reader(world):
    async def scenario(w):
        f = await farm(w)
        failures = await misread(w, f, f.gone)
        assert not failures, '\n'.join(failures)

    world(scenario)


def test_a_concept_that_never_existed_is_refused_by_every_reader(world):
    async def scenario(w):
        f = await farm(w)
        failures = await misread(w, f, NEVER)
        assert not failures, '\n'.join(failures)

    world(scenario)


def test_another_datasets_concept_is_refused_by_every_reader(world):
    """The power user reads the other dataset too: its concept is still not the farm's."""

    async def scenario(w):
        f = await farm(w)
        reachable = await w.call(POWER, 'GET', f'/api/datasets/{OTHER}/concepts')
        assert reachable.status_code == 200 and f.others in {row['id'] for row in reachable.json()}
        failures = await misread(w, f, f.others)
        assert not failures, '\n'.join(failures)

    world(scenario)


async def state(w: World) -> tuple:
    jobs = await w.database.fetchval('SELECT count(*) FROM jobs')
    samples = await w.database.fetch(
        'SELECT id, tags, metadata FROM samples WHERE dataset_id IN ($1, $2) ORDER BY id',
        FARM,
        OTHER,
    )
    return jobs, [tuple(row) for row in samples]


def test_a_bulk_edit_with_an_unknown_concept_is_refused_when_submitted(world):
    async def scenario(w):
        f = await farm(w)
        before = await state(w)
        failures = []
        for label, concept_id in (('deleted', f.gone), ('never existed', NEVER), ("another dataset's", f.others)):
            for position, node in positions(concept_id):
                for name, path, body in (
                    ('bulk tag', 'samples/bulk-tag', {'filters': node, 'add_tags': ['late']}),
                    ('bulk set', 'samples/bulk-set', {'filters': node, 'metadata': {'late': 1}}),
                    ('bulk delete', 'samples/bulk-delete', {'filters': node}),
                ):
                    response = await w.call(POWER, 'POST', f'/api/datasets/{FARM}/{path}', json=body)
                    if not says_the_concept_is_gone(response):
                        failures.append(f'{name}, {label}, {position}: {response.status_code} {response.text[:160]}')
                    if await state(w) != before:
                        failures.append(f'{name}, {label}, {position}: queued or changed something')
                        before = await state(w)
        assert not failures, '\n'.join(failures)

    world(scenario)


# --- A concept being rescored ---

HIDDEN_CONCEPT, HIDDEN_DATASET = 'ZEBRA-CONCEPT-9931', 'HIDDEN-DATASET-4417'


async def rescoring(w: World, dataset_id: int, concept_id: int) -> None:
    """A definition change submitted for the concept and its first unit run, as a runner would: the concept is being
    rescored (its scores aren't ready) until the job finishes, which it doesn't here."""
    sample_ids = [
        row['id'] for row in await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1', dataset_id)
    ]
    if not await w.database.fetchval('SELECT count(*) FROM embeddings WHERE dataset_id = $1', dataset_id):
        vectors = [
            {'sample_id': s, 'vector': [1.0, i / 10, 0.0, 0.0], 'field_name': 'clip'} for i, s in enumerate(sample_ids)
        ]
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/embeddings', json=vectors)).is_success
    submitted = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/concepts/{concept_id}/rebuild',
        params={'wait': 'false'},
        json={'weight': [0.0, 1.0, 0.0, 0.0]},
    )
    assert submitted.status_code == 202, submitted.text
    job = await job_helpers.claim(w.database, concept_jobs.EXPLICIT_KIND)
    job = await job_helpers.step(w.database, job, concept_jobs.explicit_handler(w.database.pool))
    assert job['state'] == 'running', job
    assert await w.database.fetchval('SELECT scores_ready FROM embedding_concepts WHERE id = $1', concept_id) is False


def test_another_datasets_concept_being_rescored_is_refused_as_unknown_and_named_nowhere(world):
    """Whatever state another dataset's concept is in, a filter naming it is refused as a concept this dataset doesn't
    have, and nothing about that concept, its name or its dataset, appears in the answer."""

    async def scenario(w):
        f = await farm(w)
        await w.database.execute('UPDATE datasets SET name = $2 WHERE id = $1', OTHER, HIDDEN_DATASET)
        hidden = await w.database.fetchval(
            'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) '
            "VALUES ($1, $2, 'clip', 'seed', '[1,0,0,0]') RETURNING id",
            OTHER,
            HIDDEN_CONCEPT,
        )
        await rescoring(w, OTHER, hidden)
        failures = []
        for position, node in positions(hidden):
            for name, method, path, request in readers(f, node):
                response = await w.call(POWER, method, path, **request)
                if not says_the_concept_is_gone(response):
                    failures.append(f'{name}, {position}: {response.status_code} {response.text[:160]}')
                if HIDDEN_CONCEPT in response.text or HIDDEN_DATASET in response.text:
                    failures.append(
                        f'{name}, {position}: names the other dataset or its concept: {response.text[:160]}',
                    )
        before = await state(w)
        for position, node in positions(hidden):
            for name, path, body in (
                ('bulk tag', 'samples/bulk-tag', {'filters': node, 'add_tags': ['late']}),
                ('bulk set', 'samples/bulk-set', {'filters': node, 'metadata': {'late': 1}}),
                ('bulk delete', 'samples/bulk-delete', {'filters': node}),
            ):
                response = await w.call(POWER, 'POST', f'/api/datasets/{FARM}/{path}', json=body)
                if not says_the_concept_is_gone(response):
                    failures.append(f'{name}, {position}: {response.status_code} {response.text[:160]}')
                if HIDDEN_CONCEPT in response.text or HIDDEN_DATASET in response.text:
                    failures.append(f'{name}, {position}: names the other dataset or its concept')
                if await state(w) != before:
                    failures.append(f'{name}, {position}: queued or changed something')
                    before = await state(w)
        assert not failures, '\n'.join(failures)

    world(scenario)


def test_the_datasets_own_concept_being_rescored_still_answers_409(world):
    async def scenario(w):
        f = await farm(w)
        await rescoring(w, FARM, f.own)
        failures = []
        for position, node in positions(f.own):
            for name, method, path, request in readers(f, node):
                response = await w.call(POWER, method, path, **request)
                if response.status_code != 409 or 'being rescored' not in response.text:
                    failures.append(f'{name}, {position}: {response.status_code} {response.text[:160]}')
        assert not failures, '\n'.join(failures)

    world(scenario)
