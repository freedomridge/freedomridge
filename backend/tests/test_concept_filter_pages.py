"""A filter naming a concept the dataset doesn't have answers 400, on every path, and never names the concept.

A page read without its count (skip_count, as the app's grid asks after its first page), a page of chosen fields, and
the GET sample list each caught every query error as a 500 "Internal query error"; the guard's refusal of a concept
that isn't the dataset's (SQLSTATE 22023, the app's 400) was one. The compiled filter's two concept guards are pinned
here too: ownership first, then readiness, so another dataset's concept is refused with the 400 even while it is being
rescored (readiness's 409 would name it), and the dataset's own concept being rescored keeps its 409.
"""

import json
import os
from typing import Any

import asyncpg
import pytest

from backend.routers import datasets, embeddings
from backend.tests import test_access_rule_spec, test_field_registry_postgres
from backend.tests.test_access_rule_spec import ADMIN

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

ADMIN_USER = {'id': ADMIN, 'role': 'admin', 'username': f'user-{ADMIN}'}
FARM, OTHER = 10, 11


def concept_filter(concept_id: int) -> dict[str, Any]:
    return {
        'field': '_concept',
        'operator': 'concept_select',
        'value': {'concept_id': concept_id, 'op': '>', 'threshold': 0.5},
    }


def test_the_compiled_filter_checks_the_concepts_are_the_datasets_before_the_readiness_guard():
    args: list[Any] = [FARM]
    sql, next_idx = datasets.parse_filter_node(concept_filter(4), args, 2, dataset_id=FARM)
    assert sql.startswith(
        '((SELECT require_dataset_concepts($4::int[], $5::int)) AND (SELECT require_concept_scores($4::int[])) AND (',
    )
    assert sql.count('require_dataset_concepts') == 1 and sql.count('require_concept_scores') == 1
    assert args[3:] == [[4], FARM] and next_idx == 6
    # Without a dataset, the readiness guard alone, as before.
    bare: list[Any] = []
    sql, _ = datasets.parse_filter_node(concept_filter(4), bare, 1)
    assert 'require_dataset_concepts' not in sql and bare[-1] == [4]


def test_a_page_naming_a_concept_that_is_not_the_datasets_is_a_400_not_a_500(world):
    async def scenario(w):
        for dataset_id in (FARM, OTHER):
            await w.dataset(dataset_id)
        samples = [{'filepath': f'page-{i}.jpg', 'metadata': {'n': i}} for i in range(3)]
        ids = (await datasets.add_samples(FARM, {'samples': samples}, user=ADMIN_USER))['ids']
        await datasets.add_samples(OTHER, {'samples': samples[:1]}, user=ADMIN_USER)

        async def concept(dataset_id: int, name: str) -> int:
            return await w.database.fetchval(
                'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) '
                "VALUES ($1, $2, 'clip', 'seed', '[1,0]') RETURNING id",
                dataset_id,
                name,
            )

        own, others, gone = await concept(FARM, 'weeds'), await concept(OTHER, 'weeds'), await concept(FARM, 'stones')
        for sample_id in ids:
            await w.database.execute(
                'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, 0.75)',
                own,
                sample_id,
            )
        await w.database.execute('DELETE FROM embedding_concepts WHERE id = $1', gone)

        def pages(concept_id: int):
            node = concept_filter(concept_id)
            wrapped = {'logic': 'AND', 'filters': [{'field': 'media_type', 'operator': '!=', 'value': 'object'}, node]}
            base = f'/api/datasets/{FARM}/samples'
            return [
                (
                    'page without its count',
                    'POST',
                    f'{base}/query',
                    {'json': {'filters': wrapped, 'limit': 200, 'skip_count': True}},
                ),
                (
                    'page of chosen fields',
                    'POST',
                    f'{base}/query',
                    {'json': {'filters': node, 'limit': 10, 'skip_count': True, 'fields': 'id,metadata.n'}},
                ),
                ('GET sample list', 'GET', base, {'params': {'filters': json.dumps(node), 'limit': 10}}),
            ]

        for name, method, path, request in pages(own):
            answer = await w.call(ADMIN, method, path, **request)
            assert answer.status_code == 200 and len(answer.json()['items']) == 3, (name, answer.text)
        for concept_id in (gone, others, 987654):
            for name, method, path, request in pages(concept_id):
                answer = await w.call(ADMIN, method, path, **request)
                assert answer.status_code == 400, (name, concept_id, answer.status_code, answer.text)
                assert f'Concept {concept_id} doesn' in answer.json()['detail'], (name, answer.text)

        # Being rescored is still the readiness guard's 409, also for the dataset's own concept.
        await w.database.execute('UPDATE embedding_concepts SET scores_ready = FALSE WHERE id = $1', own)
        for name, method, path, request in pages(own):
            answer = await w.call(ADMIN, method, path, **request)
            assert answer.status_code == 409, (name, answer.text)

    world(scenario)


NAME = 'secret-orchard'  # another dataset's concept: its name must never reach the farm's reader


def test_another_datasets_concept_being_rescored_is_the_400_on_every_path_and_never_named(world):
    async def scenario(w):
        for dataset_id in (FARM, OTHER):
            await w.dataset(dataset_id)
        samples = [{'filepath': f'rescored-{i}.jpg', 'metadata': {'n': i, 'grp': 'ab'[i % 2]}} for i in range(3)]
        await datasets.add_samples(FARM, {'samples': samples}, user=ADMIN_USER)
        other_ids = (await datasets.add_samples(OTHER, {'samples': samples[:1]}, user=ADMIN_USER))['ids']
        others = await w.database.fetchval(
            'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) '
            "VALUES ($1, $2, 'clip', 'seed', '[1,0]') RETURNING id",
            OTHER,
            NAME,
        )
        await w.database.execute(
            'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, 0.75)',
            others,
            other_ids[0],
        )
        await w.database.execute('UPDATE embedding_concepts SET scores_ready = FALSE WHERE id = $1', others)
        node = {'logic': 'AND', 'filters': [{'field': 'grp', 'operator': '=', 'value': 'a'}, concept_filter(others)]}
        base = f'/api/datasets/{FARM}'
        paths: list[tuple[str, str, str, dict[str, Any]]] = [
            ('count', 'POST', f'{base}/samples/query', {'json': {'filters': node, 'limit': 0}}),
            (
                'page without its count',
                'POST',
                f'{base}/samples/query',
                {'json': {'filters': node, 'skip_count': True}},
            ),
            ('GET sample list', 'GET', f'{base}/samples', {'params': {'filters': json.dumps(node)}}),
            ('histogram', 'POST', f'{base}/histogram', {'json': {'field': 'n', 'filters': node}}),
            ('map points', 'POST', f'{base}/map_points', {'json': {'filters': node, 'points': True}}),
            ('selection token', 'POST', f'{base}/selection_token', {'json': {'filters': node}}),
            ('sidebar', 'POST', f'{base}/field-stats', {'json': {'filters': node}}),
            ('sidebar facet', 'POST', f'{base}/field-stats', {'json': {'filters': node, 'fields': ['n']}}),
            ('autocomplete', 'POST', f'{base}/fields/grp/autocomplete', {'json': {'q': 'a', 'filters': node}}),
            (
                'bulk tag, when submitted',
                'POST',
                f'{base}/samples/bulk-tag',
                {'json': {'filters': node, 'add_tags': ['x']}},
            ),
        ]
        jobs = await w.database.fetchval('SELECT count(*) FROM jobs')
        for name, method, path, request in paths:
            answer = await w.call(ADMIN, method, path, **request)
            assert answer.status_code == 400, (name, answer.status_code, answer.text)
            assert f"Concept {others} doesn't exist in this dataset" in answer.text, (name, answer.text)
            assert NAME not in answer.text, (name, answer.text)
        assert await w.database.fetchval('SELECT count(*) FROM jobs') == jobs

    world(scenario)


def test_the_scatter_checks_ownership_before_readiness_on_both_its_paths(registry_database, monkeypatch):
    """The scatter's fast path compiles its own concept sources: its ownership check runs before its statement."""
    from unittest.mock import AsyncMock

    from backend.tests.test_broad_scatter import CONCEPT, populate

    async def scenario(database):
        await populate(database, 40)  # concept 2, 'unrelated', is dataset 2's
        await database.execute('UPDATE embedding_concepts SET scores_ready = FALSE WHERE id = 2')
        other = {**CONCEPT, 'value': {'concept_id': 2, 'op': '<=', 'threshold': 1}}
        request = embeddings.EmbeddingsRequest(filters=other, sample_pct=100)
        with pytest.raises(asyncpg.InvalidParameterValueError) as fast:
            await embeddings._query_broad_exclusions(1, request)
        monkeypatch.setattr(embeddings, '_query_broad_exclusions', AsyncMock(return_value=None))
        with pytest.raises(asyncpg.InvalidParameterValueError) as general:
            await embeddings.get_embeddings_post(1, request)
        for refused in (fast.value, general.value):
            assert "Concept 2 doesn't exist in this dataset" in str(refused) and 'unrelated' not in str(refused)

    registry_database(scenario)
