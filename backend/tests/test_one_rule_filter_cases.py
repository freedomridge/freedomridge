"""One file of filter cases, shown the same by the server and by the SDK's filter of similarity results (contract g7,
one rule everywhere, 2.2, changed 2026-11-25), as frontend/tests/fixtures/image-grain-cases.json is for the page's
wrapping and the assistant's.

backend/tests/one_rule_filter_cases.json holds samples (a value, another value, a JSON null, no key, a value of another
kind, an empty list, a list of labels with and without a label, a value that is no list where a list is expected) and
filters, each with the samples it shows by the rule: a row with no value for a field never matches a comparison of
that field, nor the NOT of one; a list (tags, a list of labels) compares by its elements; tags have a value when
they hold one, a list in a field when it is there, empty or not (changed 2026-11-26: an empty list of labels is shown
by a negation, a list whose elements have no label is not); a NOT reads all its parts; an element match's NOT needs a
list, empty or not; `exists` asks for the key. The samples hold the rows of section 7.7: no key, a JSON null, `[]`, a
list with the value, a list without it, and a list whose elements have no label.

- `test_the_reference_reads_every_case_as_the_file_says` checks the file against the spec file's reference
  (test_one_rule_everywhere_spec.Reading), so a wrong expectation is the file's own mistake.
- The server test ingests the samples on a database (a dataset without objects) and asks each case through the
  samples query, its count agreeing.
- The SDK test filters a similarity result as a script does: `sort_by_similarity()` on the dataset (served by the app
  in-process) returns a SimilarityResultView of every sample, and each case's filter on it shows the file's samples.
  The SDK asks the server which of its samples the filter shows, so every case is answered: none is refused.
- So that a filter run on the whole dataset can't pass for the SDK's, a second SDK test keeps the NEAR nearest
  samples only (ORDER is the similarity order, nearest first): each case shows the file's samples among those, and
  none outside them, in their order with their distances. Many cases show samples outside the nearest too.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fridge import Condition, Dataset

from backend.routers import datasets
from backend.tests import test_one_rule_everywhere_spec as spec
from backend.tests.test_protected_metadata_keys_spec import sdk
from backend.tests.test_regions_spec import ADMIN_USER, Regions, post

database = spec.database
regions_world = spec.regions_world
pytestmark = spec.pytestmark

DOC = json.loads((Path(__file__).parent / 'one_rule_filter_cases.json').read_text())
SAMPLES: dict[str, dict[str, Any]] = DOC['samples']
CASES: list[dict[str, Any]] = DOC['cases']
CASE_IDS = [case['name'] for case in CASES]
DATASET = 76
QUERY = [1.0, 0.0, 0.0, 0.0]
ORDER = ['notalist', 'kind', 'null', 'blue', 'unlabelled', 'red', 'none', 'empty']  # nearest to QUERY first
NEAR = 5
ROWS = {name: spec.Row(dict(held['metadata']), tags=tuple(held['tags'])) for name, held in SAMPLES.items()}


class Written(Condition):
    """A filter as a script writes it in full: a Condition whose dict is the filter itself."""

    def __init__(self, node: dict[str, Any]):
        super().__init__('', '', None)
        self.node = node

    def to_dict(self) -> dict[str, Any]:
        return self.node


def test_the_reference_reads_every_case_as_the_file_says():
    for case in CASES:
        assert spec.RULE.rows(case['filter'], ROWS) == set(case['shows']), case['name']


def test_the_nearest_samples_are_a_part_and_cases_show_samples_on_both_sides_of_it():
    assert sorted(ORDER) == sorted(SAMPLES) and 0 < NEAR < len(ORDER)
    near = set(ORDER[:NEAR])
    both_sides = [case['name'] for case in CASES if set(case['shows']) & near and set(case['shows']) - near]
    assert len(both_sides) >= 10, both_sides


async def plant(r: Regions) -> spec.Planted:
    """The file's samples as a dataset without objects, each with a vector (for the similarity search)."""
    await r.dataset(DATASET)
    samples = [
        {'filepath': f'{name}.jpg', 'metadata': held['metadata'], 'tags': held['tags']}
        for name, held in SAMPLES.items()
    ]
    await datasets.add_samples(DATASET, {'samples': samples}, user=ADMIN_USER)
    stored = await r.database.fetch('SELECT id, filepath, metadata, tags FROM samples WHERE dataset_id = $1', DATASET)
    for row in stored:
        name = row['filepath'][: -len('.jpg')]
        metadata = row['metadata'] if isinstance(row['metadata'], dict) else json.loads(row['metadata'])
        assert (metadata, list(row['tags'] or [])) == (SAMPLES[name]['metadata'], SAMPLES[name]['tags']), name
    ids = {row['filepath'][: -len('.jpg')]: row['id'] for row in stored}
    vectors = [
        {'sample_id': ids[name], 'vector': [1.0, place / 100, 0.0, 0.0], 'field_name': 'clip'}
        for place, name in enumerate(ORDER)
    ]
    assert (await post(r, f'/api/datasets/{DATASET}/embeddings', vectors)).is_success
    return spec.Planted(DATASET, ids, 0)


@pytest.mark.parametrize('case', CASES, ids=CASE_IDS)
def test_the_server_shows_each_case_as_the_file_says(regions_world, case):
    async def scenario(r):
        p = await plant(r)
        assert await spec.shown(r, p, case['filter']) == set(case['shows']), case['name']

    regions_world(scenario)


@pytest.mark.parametrize('case', CASES, ids=CASE_IDS)
def test_the_sdks_similarity_results_show_each_case_as_the_file_says(regions_world, monkeypatch, case):
    async def scenario(r):
        p = await plant(r)
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def script() -> Any:
            similar = Dataset(f'dataset-{DATASET}').all().sort_by_similarity(vector=QUERY, limit=100)
            held = {sample.filepath: (sample.metadata, list(sample.tags)) for sample in similar}
            assert held == {f'{name}.jpg': (sample['metadata'], sample['tags']) for name, sample in SAMPLES.items()}, (
                'the similarity results are every sample, as stored'
            )
            return set(similar.filter(Written(case['filter'])).ids())

        answer = await asyncio.to_thread(script)
        assert p.names(answer) == set(case['shows']), (case['name'], sorted(p.names(answer)))

    regions_world(scenario)


@pytest.mark.parametrize('case', CASES, ids=CASE_IDS)
def test_the_sdks_filter_of_the_nearest_samples_shows_only_those_the_file_says_in_their_order(
    regions_world,
    monkeypatch,
    case,
):
    async def scenario(r):
        p = await plant(r)
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def script() -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
            similar = Dataset(f'dataset-{DATASET}').all().sort_by_similarity(vector=QUERY, limit=NEAR)
            listed = [(sample.id, sample.distance) for sample in similar]
            shown = [(sample.id, sample.distance) for sample in similar.filter(Written(case['filter']))]
            return listed, shown

        listed, shown = await asyncio.to_thread(script)
        assert [p.names([sample_id]).pop() for sample_id, _ in listed] == ORDER[:NEAR], 'the nearest first'
        wanted = [(sample_id, distance) for sample_id, distance in listed if p.names([sample_id]) <= set(case['shows'])]
        assert shown == wanted, (case['name'], [p.names([sample_id]).pop() for sample_id, _ in shown])

    regions_world(scenario)
