"""Facet requests and #138's refusals (SQLSTATE 55000 -> 409): a concept being rescored or a lasso drawn on
a re-fitted map refuses the request before its stream starts (a 409), and refuses each field not yet
answered once it has started (a refusal line carrying the same 409 body), never "cached values".
"""

import asyncio
import json
import uuid

import asyncpg
import pytest

from backend import main
from backend.routers import datasets
from backend.tests.test_field_registry_postgres import registry_database  # noqa: F401
from backend.tests.test_sidebar_facet_stream import allow_access, lines_of, stream_post
from backend.tests.test_sidebar_on_open import SELECTIVE, seed

RESCORED = 'Concept "sky" is being rescored; use it again when its scoring job finishes'
LASSO_FIELD = 'clip_img'


def select_sky():
    return {'field': '_concept', 'operator': 'concept_select', 'value': {'concept_id': 4, 'threshold': 0.5}}


async def sky(database, ready):
    await database.execute(
        "INSERT INTO embedding_concepts (id,dataset_id,name,embedding_field,kind,weight,scores_ready) "
        "VALUES (4,1,'sky','clip','seed','[1,0]',$1)",
        ready,
    )
    await database.execute(
        'INSERT INTO embedding_concept_scores (concept_id,sample_id,score) SELECT 4,id,0.9 FROM samples',
    )


def refused_mid_stream(database, monkeypatch, field, raise_it):
    """`field`'s filtered MIN/MAX meets a refusal after the stream started (the check up front passed)."""
    original = datasets.compute_filtered_minmax

    async def minmax(dataset_id, field_name, filters):
        if field_name == field:
            await asyncio.sleep(0.2)
            await raise_it()
        return await original(dataset_id, field_name, filters)

    monkeypatch.setattr(datasets, 'compute_filtered_minmax', minmax)


def test_a_rescoring_concept_refuses_the_facet_request_before_its_stream(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, monkeypatch)
        await sky(database, ready=False)
        allow_access(monkeypatch)
        stage = {'logic': 'AND', 'filters': [select_sky()]}
        for stream in (True, False):
            body = {'filters': stage, 'lightning_mode': True, 'fields': ['score', 'brightness'], 'stream': stream}
            status, headers, chunks = await stream_post(body)
            answer = json.loads(b''.join(chunk for _, chunk in chunks))
            assert status == 409 and headers['content-type'].startswith('application/json')
            assert 'is being rescored' in answer['detail']

    registry_database(scenario)


def test_a_concept_rescored_mid_stream_refuses_each_field_not_yet_answered(
    registry_database,  # noqa: F811
    monkeypatch,
):
    async def scenario(database):
        await seed(database, monkeypatch)
        allow_access(monkeypatch)

        async def rescoring():
            raise asyncpg.ObjectNotInPrerequisiteStateError(RESCORED)

        refused_mid_stream(database, monkeypatch, 'brightness', rescoring)
        body = {
            'filters': SELECTIVE,
            'lightning_mode': True,
            'fields': ['score', 'brightness', 'color'],
            'stream': True,
        }
        status, _, chunks = await stream_post(body)
        entries = [entry for _, entry in lines_of(chunks)]
        assert status == 200
        assert entries[0]['name'] == 'score' and entries[0]['provenance'] == 'exact'  # answered before
        refused = {entry['name']: entry['refusal'] for entry in entries[1:-1]}
        assert sorted(refused) == ['brightness', 'color']
        assert all(refusal == {'status': 409, 'detail': RESCORED} for refusal in refused.values())
        assert entries[-1] == {'done': True}
        assert database.pool.get_idle_size() == database.pool.get_size()  # the other fields were stopped

    registry_database(scenario)


def test_a_lasso_refused_mid_stream_tells_the_ui_which_lasso_to_drop(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, monkeypatch)
        allow_access(monkeypatch)
        model = uuid.uuid4()
        await database.execute(
            "INSERT INTO embedding_map_state (dataset_id, field_name, model_id, artifact_location, refitted_at) "
            "VALUES (1, $1, $2, 'models/x', '2026-10-21 08:58+00')",
            LASSO_FIELD,
            model,
        )

        async def refitted():  # the real refusal, with its DETAIL
            await database.fetchval("SELECT require_current_map(1, $1, 'the old model')", LASSO_FIELD)

        refused_mid_stream(database, monkeypatch, 'brightness', refitted)
        body = {'filters': SELECTIVE, 'lightning_mode': True, 'fields': ['score', 'brightness'], 'stream': True}
        _, _, chunks = await stream_post(body)
        entries = {entry.get('name'): entry for _, entry in lines_of(chunks)}
        refusal = entries['brightness']['refusal']
        assert (refusal['status'], refusal['code'], refusal['dataset_id'], refusal['field']) == (
            409,
            'map_refitted',
            1,
            LASSO_FIELD,
        )
        assert refusal['model_id'] == str(model) and 're-fitted' in refusal['detail']
        # The same body the app's 409 handler gives for this refusal.
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError) as error:
            await refitted()
        response = await main.dataset_lifecycle_error(None, error.value)  # type: ignore[arg-type]
        assert {**json.loads(bytes(response.body)), 'status': response.status_code} == refusal

    registry_database(scenario)


@pytest.mark.parametrize('lightning_mode', [True, False])
def test_a_refused_field_query_refuses_the_sidebar_request(
    registry_database,  # noqa: F811
    monkeypatch,
    lightning_mode,
):
    """guarded() keeps a field's cached values when its query fails, but never for a refusal: that is the
    request's 409, not unfiltered values next to a grid that refuses the filter."""

    async def scenario(database):
        await seed(database, monkeypatch)

        async def rescoring():
            raise asyncpg.ObjectNotInPrerequisiteStateError(RESCORED)

        # The default view computes the promoted `score` in both modes (the seed's set counts as broad).
        refused_mid_stream(database, monkeypatch, 'score', rescoring)
        image = {'logic': 'AND', 'filters': [{'field': 'media_type', 'operator': '!=', 'value': 'object'}]}
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being rescored'):
            await datasets._get_dataset_field_stats_impl(1, image, lightning_mode=lightning_mode)
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being rescored'):
            await datasets._get_dataset_field_stats_impl(1, SELECTIVE, fields=['score', 'brightness'])

    registry_database(scenario)
