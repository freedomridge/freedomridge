"""Bulk jobs and the SDK's queries on a filter holding a lasso drawn on an earlier layout of its map (stale_lassos.py):
a delete (of samples or of vectors) is refused when submitted, and fails at run time if the map is laid out again in
between, deleting nothing more; a tag or a metadata edit goes ahead and names the maps (`stale_lassos`); so does a
samples query, for the SDK to warn."""

import json
import os
from unittest.mock import AsyncMock

import pytest

from backend import bulk_jobs, stale_lassos
from backend.api_errors import CodedHTTPException
from backend.routers import datasets, embeddings
from backend.tests import test_field_registry_postgres
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_job_runner import make_worker
from backend.tests.test_layout_coordinates import seed, upload

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')
USER = test_field_registry_postgres.USER
# upload() puts sample i at x = i, y = i / 3: this holds samples 1 to 4.
POLYGON = [[0.5, -1], [4.5, -1], [4.5, 3], [0.5, 3]]


def lasso(field='clip', **recorded):
    return {'field': field, 'operator': 'embedding_polygon', 'value': POLYGON, **recorded}


def view(*filters):
    """The filter a saved view's stages make: their AND."""
    return {'logic': 'AND', 'filters': list(filters)}


def test_the_rule_is_the_app_s():
    """The cases of frontend/tests/stale-lasso.test.cjs."""
    assert not stale_lassos.lasso_is_stale('a', 'a')
    assert stale_lassos.lasso_is_stale('a', 'b')
    assert not stale_lassos.lasso_is_stale(None, None)  # saved before the upgrade, map not laid out again
    assert stale_lassos.lasso_is_stale(None, 'b')  # the map was laid out again since
    assert not stale_lassos.lasso_is_stale('a', None)  # can't be told: a mirror without its primary's id yet
    nested = {
        'logic': 'AND',
        'filters': [
            {'field': 'n', 'operator': '>', 'value': 1},
            {'logic': 'NOT', 'filters': [lasso(operator='embedding_polygon_exclude', layout_id='x')]},
            {'field': '_cgself', 'operator': 'cg_self', 'value': view(lasso('clip_patch_4x4'))},
        ],
    }
    assert stale_lassos.lassos(nested) == [('clip', 'x'), ('clip_patch_4x4', None)]
    assert stale_lassos.lassos({'field': 'n', 'operator': '>', 'value': 1, 'layout_id': 'x'}) == []
    assert stale_lassos.describe(['clip']) == 'the clip map'
    assert stale_lassos.describe(['a', 'b', 'c']) == 'the a, b and c maps'


async def mapped(db):
    await seed(db)
    await seed(db, field='dino')
    await upload(range(1, 9))
    await upload(range(1, 9), field='dino')  # a map never laid out again: no id
    return await db.fetchval("SELECT bump_map_layout(1, 'clip')::text")


async def counts(db):
    return await db.fetchval(
        "SELECT (SELECT count(*) FROM samples WHERE dataset_id = 1) || '/' || (SELECT count(*) FROM embeddings)",
    )


def test_a_bulk_delete_by_a_stale_lasso_is_refused_and_one_by_a_current_lasso_runs(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        now = await mapped(db)
        before = await counts(db)
        # Drawn on an earlier layout, or saved without an id before the map was laid out again; alone or in a view.
        for stale in (lasso(layout_id='an-earlier-layout'), lasso(), view(lasso(layout_id='an-earlier-layout'))):
            for submit in (
                lambda f: datasets.bulk_delete_samples(1, datasets.BulkTargetRequest(filters=f), USER),
                lambda f: datasets.bulk_delete_embeddings(1, 'dino', datasets.BulkTargetRequest(filters=f), USER),
            ):
                with pytest.raises(CodedHTTPException) as refused:
                    await submit(stale)
                assert refused.value.status_code == 409 and refused.value.code == 'stale_lasso'
                assert 'earlier layout of the clip map' in refused.value.detail
        assert await counts(db) == before
        # On the current layout, or without an id on a map without one: the job runs.
        current = await datasets.bulk_delete_embeddings(
            1,
            'dino',
            datasets.BulkTargetRequest(filters=view(lasso(layout_id=now))),
            USER,
        )
        assert 'stale_lassos' not in json.loads(current.body)
        assert (await finish(db))['result'] == {'deleted': 4}
        # x 4.5 to 8.5: samples 5 to 8, whose dino points the delete above left.
        later = {**lasso('dino'), 'value': [[4.5, 1], [8.5, 1], [8.5, 3], [4.5, 3]]}
        await datasets.bulk_delete_samples(1, datasets.BulkTargetRequest(filters=later), USER)
        assert (await finish(db))['result'] == {'deleted_count': 4, 'objects_deleted': 0}

    registry_database(scenario)


def test_a_tag_or_an_edit_by_a_stale_lasso_goes_ahead_and_names_the_map(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        now = await mapped(db)
        tagged = await datasets.bulk_tag_samples(
            1,
            datasets.BulkTagRequest(filters=view(lasso()), add_tags=['near']),
            USER,
        )
        assert json.loads(tagged.body)['stale_lassos'] == ['clip']
        assert (await finish(db))['result'] == {
            'updated_count': 4,
            'message': 'Updated tags for 4 samples',
            'stale_lassos': ['clip'],
        }
        edited = await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(filters=lasso(layout_id='an-earlier-layout'), metadata={'near': True}),
            USER,
        )
        assert json.loads(edited.body)['stale_lassos'] == ['clip']
        assert (await finish(db))['result']['stale_lassos'] == ['clip']
        current = await datasets.bulk_tag_samples(
            1,
            datasets.BulkTagRequest(filters=lasso(layout_id=now), add_tags=['current']),
            USER,
        )
        assert 'stale_lassos' not in json.loads(current.body)
        assert 'stale_lassos' not in (await finish(db))['result']

    registry_database(scenario)


def test_a_delete_whose_map_is_laid_out_again_before_it_runs_fails_and_deletes_nothing(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        now = await mapped(db)
        before = await counts(db)
        submitted = await datasets.bulk_delete_samples(
            1,
            datasets.BulkTargetRequest(filters=lasso(layout_id=now)),
            USER,
        )
        job_id = json.loads(submitted.body)['job_id']
        await upload([1, 2], shift=100)  # a new layout, between the submit and the job's units
        assert await db.fetchval("SELECT layout_id::text FROM embedding_layouts WHERE field_name = 'clip'") != now
        worker = await make_worker(
            db,
            {bulk_jobs.KIND: bulk_jobs.handler(db.pool)},
            policies={bulk_jobs.KIND: bulk_jobs.POLICY},
        )
        for _ in range(20):
            job = await db.fetchrow('SELECT state, error FROM jobs WHERE id = $1::uuid', job_id)
            if job['state'] in ('succeeded', 'failed', 'cancelled'):
                break
            await worker.run_once()
        assert job['state'] == 'failed'
        assert 'laid out again after this delete was submitted' in job['error']['message']
        assert await counts(db) == before

    registry_database(scenario)


def test_a_samples_query_names_the_maps_of_its_stale_lassos(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        now = await mapped(db)

        async def query(filters):
            return await datasets.list_samples_post(1, datasets.SamplesRequest(limit=10, filters=filters))

        answer = await query(view(lasso(), lasso('dino')))
        assert answer['stale_lassos'] == ['clip'] and answer['total'] == 4
        assert 'stale_lassos' not in await query(lasso(layout_id=now))
        assert 'stale_lassos' not in await query({'field': 'id', 'operator': '>', 'value': 0})

    registry_database(scenario)


def test_on_a_mirror_its_own_ids_decide(registry_database):
    async def scenario(db):
        await db.execute(
            'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor, vector_fields) '
            "VALUES (2, 7, 0, '{clip}')",
        )
        await db.execute(
            'INSERT INTO embedding_layouts (dataset_id, field_name, layout_id, pending_layout_id, pending_txid) '
            "VALUES (2, 'clip', 'f81d4fae-7dec-11d0-a765-00a0c91e6bf6', 'a81d4fae-7dec-11d0-a765-00a0c91e6bf6', 99)",
        )
        held = lasso(layout_id='f81d4fae-7dec-11d0-a765-00a0c91e6bf6')
        arriving = lasso(layout_id='a81d4fae-7dec-11d0-a765-00a0c91e6bf6')  # the primary's, not taken here yet
        assert await stale_lassos.stale_maps(db, 2, held) == []
        assert await stale_lassos.stale_maps(db, 2, arriving) == ['clip']
        assert await stale_lassos.stale_maps(db, 1, arriving) == []  # another dataset: no id for its map

    registry_database(scenario)
