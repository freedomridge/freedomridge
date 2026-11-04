"""Deleting one embedding field's vectors: chunked and resumable, with map data and indexes left consistent."""

import json
import os
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import HTTPException

from backend import bulk_jobs, job_queue
from backend.routers import datasets, embeddings
from backend.tests import test_field_registry_postgres
from backend.tests.test_bulk_jobs import claim, finish, step, submitted
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

FIELD = 'embeddings_model_a'
OTHER_FIELD = 'embeddings_model_b'


async def seed(db, count=7):
    """Samples with a vector in two fields, a map for FIELD and metadata that must survive."""
    ids = (await ingest([{'uniqueness_model_a': index / 10} for index in range(count)]))['ids']
    for field in (FIELD, OTHER_FIELD):
        await db.execute(
            'INSERT INTO embeddings(sample_id,field_name,vector) SELECT id,$2,ARRAY[id,1,2]::real[]::vector '
            'FROM unnest($1::int[]) id',
            ids,
            field,
        )
    await db.execute(
        'INSERT INTO embedding_coordinates(sample_id,field_name,x,y) SELECT id,$2,id,id FROM unnest($1::int[]) id',
        ids,
        FIELD,
    )
    await db.execute("SELECT recompute_viz_rank(1,$1)", FIELD)
    return ids


async def map_rows(db, field=FIELD):
    return {
        'embeddings': sorted(
            await db.fetchval('SELECT array_agg(sample_id) FROM embeddings WHERE field_name=$1', field) or [],
        ),
        'coordinates': await db.fetchval('SELECT count(*) FROM embedding_coordinates WHERE field_name=$1', field),
        'pending': sorted(
            await db.fetchval(
                'SELECT array_agg(sample_id) FROM embedding_projection_pending WHERE field_name=$1',
                field,
            )
            or [],
        ),
        'ranks': await db.fetchval('SELECT count(*) FROM embedding_viz_rank WHERE field_name=$1', field),
        'meta_points': await db.fetchval(
            'SELECT total_points FROM embedding_viz_meta WHERE dataset_id=1 AND field_name=$1',
            field,
        ),
    }


async def delete_request(**target):
    return submitted(await datasets.bulk_delete_embeddings(1, FIELD, datasets.BulkTargetRequest(**target), USER))


async def missing_ids(field=FIELD):
    page = await datasets.dataset_missing(1, 'embedding', field=field, limit=1000)
    return [item['id'] for item in page['items']]


def test_chunked_and_resumed_delete_then_refill(registry_database, monkeypatch):
    monkeypatch.setattr(bulk_jobs, 'BATCH_SIZE', 2)
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        ids = await seed(db)
        metadata = await db.fetch('SELECT id,metadata FROM samples ORDER BY id')
        other = await map_rows(db, OTHER_FIELD)
        assert (await map_rows(db))['meta_points'] == 7
        chosen = ids[:5]

        job_id = await delete_request(sample_ids=chosen)
        job = await step(db, await claim(db))
        assert job['result'] == {'deleted': 2}
        await job_queue.cancel_job(db.pool, job_id)
        job = await step(db, job)
        assert job['state'] == 'cancelled'
        await bulk_jobs.resume(db.pool, job_id)
        job = await finish(db)
        assert job['result'] == {'deleted': 5}
        assert job['checkpoint']['examined'] == 5

        # Only the chosen vectors went, with their coordinates and rank rows; the count follows the rows.
        assert await map_rows(db) == {
            'embeddings': ids[5:],
            'coordinates': 2,
            'pending': [],
            'ranks': 2,
            'meta_points': 2,
        }
        assert await map_rows(db, OTHER_FIELD) == other
        assert await db.fetch('SELECT id,metadata FROM samples ORDER BY id') == metadata
        assert await missing_ids() == chosen
        assert await missing_ids(OTHER_FIELD) == []

        # The same dimension is accepted again, queued for projection like a new vector, with no stale position.
        await embeddings.upload_embeddings(
            1,
            [
                embeddings.EmbeddingUpload(sample_id=sid, field_name=FIELD, vector=[9.0, 9.0, float(sid)])
                for sid in chosen
            ],
            USER,
        )
        assert await missing_ids() == []
        refilled = await map_rows(db)
        assert refilled['pending'] == chosen
        assert refilled['coordinates'] == 2 and refilled['ranks'] == 2 and refilled['meta_points'] == 2
        await embeddings.upload_coordinates(
            1,
            FIELD,
            [embeddings.CoordinateUpdate(sample_id=sid, x=100.0 + sid, y=0.0) for sid in chosen],
            USER,
        )
        placed = await map_rows(db)
        assert placed['pending'] == [] and placed['coordinates'] == 7
        assert placed['ranks'] == 7 and placed['meta_points'] == 7

    registry_database(scenario)


def test_concept_scores_of_deleted_vectors_are_queued_for_rescoring(registry_database):
    async def scenario(db):
        ids = await seed(db, 3)
        await db.execute(
            "INSERT INTO embedding_concepts(dataset_id,name,embedding_field,kind,weight) "
            "VALUES(1,'c',$1,'seed','[1,0,0]')",
            FIELD,
        )
        assert await db.fetchval('SELECT score_concept(1)') == 3
        await db.execute('DELETE FROM concept_embedding_pending')
        await delete_request(sample_ids=ids[:1])
        await finish(db)
        # Concept scores belong to samples, not vectors: they do not cascade. The deletion queues the rescoring
        # that removes the score of a sample without a vector.
        assert await db.fetchval('SELECT count(*) FROM concept_embedding_pending WHERE field_name=$1', FIELD) == 1
        assert await db.fetchval('SELECT count(*) FROM embedding_concept_scores') == 3
        await db.execute('SELECT score_concept(1, FALSE)')
        assert (
            await db.fetchval('SELECT array_agg(sample_id ORDER BY sample_id) FROM embedding_concept_scores') == ids[1:]
        )

    registry_database(scenario)


def test_explicit_ids_in_other_datasets_and_unknown_fields_delete_nothing(registry_database):
    async def scenario(db):
        ids = await seed(db, 2)
        foreign = (await ingest([{}], dataset_id=2, prefix='other'))['ids']
        await db.execute(
            "INSERT INTO embeddings(sample_id,field_name,vector) SELECT unnest($1::int[]),$2,'[1,2,3]'",
            foreign,
            FIELD,
        )
        await delete_request(sample_ids=[*foreign, ids[0]])
        assert (await finish(db))['result'] == {'deleted': 1}
        assert await db.fetchval('SELECT count(*) FROM embeddings WHERE sample_id=ANY($1::int[])', foreign) == 1
        with pytest.raises(HTTPException) as refused:
            await datasets.bulk_delete_embeddings(1, 'bad name; drop', datasets.BulkTargetRequest(filters={}), USER)
        assert refused.value.status_code == 400
        with pytest.raises(HTTPException) as untargeted:
            await datasets.bulk_delete_embeddings(1, FIELD, datasets.BulkTargetRequest(), USER)
        assert untargeted.value.status_code == 400

    registry_database(scenario)


def test_route_requires_admin_and_a_writable_dataset(registry_database):
    import httpx
    from fastapi import FastAPI

    from backend.access import require_dataset_access
    from backend.routers.auth import get_current_user

    async def scenario(db):
        await seed(db, 2)
        app = FastAPI()
        app.include_router(datasets.router)
        app.dependency_overrides[require_dataset_access] = lambda: None
        app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'role': 'power_user'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            url = f'/datasets/1/embeddings/{FIELD}/delete'
            assert (await client.post(url, json={'filters': {}})).status_code == 403
            app.dependency_overrides[get_current_user] = lambda: USER
            await db.execute("INSERT INTO datasets(id,name,is_global) VALUES(3,'being-removed',TRUE)")
            await db.execute('UPDATE datasets SET deleting=TRUE WHERE id=3')
            with pytest.raises(asyncpg.PostgresError, match='being deleted'):
                await client.post(f'/datasets/3/embeddings/{FIELD}/delete', json={'filters': {}})
            response = await client.post(url, json={'filters': {}})
            assert response.status_code == 202
            assert json.loads(response.content)['state'] == 'queued'
        assert await db.fetchval('SELECT count(*) FROM embeddings WHERE field_name=$1', FIELD) == 2

    registry_database(scenario)


def test_emptied_field_with_an_ann_index_refuses_another_dimension_with_a_clear_message(registry_database):
    async def scenario(db):
        await seed(db, 3)
        await db.execute(
            f"CREATE INDEX hnsw_emb_{FIELD} ON embeddings USING hnsw ((vector::vector(3)) vector_cosine_ops) "
            f"WHERE field_name = '{FIELD}'",
        )
        await delete_request(sample_ids=[1, 2, 3])
        await finish(db)
        # PostgreSQL itself rejects the other dimension on insert (a bare 500 for the caller).
        with pytest.raises(asyncpg.PostgresError, match='expected 3 dimensions'):
            await db.execute("INSERT INTO embeddings(sample_id,field_name,vector) VALUES(1,$1,'[1,2,3,4]')", FIELD)
        with pytest.raises(HTTPException) as refused:
            await embeddings.validate_embedding_dims({FIELD: {4}})
        assert refused.value.status_code == 422
        assert f'hnsw_emb_{FIELD}' in refused.value.detail and '3-d' in refused.value.detail
        await embeddings.validate_embedding_dims({FIELD: {3}})
        # Without an index the dimension is free once no vector remains.
        await db.execute(f'DROP INDEX hnsw_emb_{FIELD}')
        await embeddings.validate_embedding_dims({FIELD: {4}})

    registry_database(scenario)


def test_ann_index_returns_only_the_refilled_vectors_without_a_rebuild(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        ids = await seed(db, 40)
        await db.execute(
            f"CREATE INDEX hnsw_emb_{FIELD} ON embeddings USING hnsw ((vector::vector(3)) vector_cosine_ops) "
            f"WHERE field_name = '{FIELD}'",
        )
        chosen = ids[:10]
        await delete_request(sample_ids=chosen)
        await finish(db)
        await embeddings.upload_embeddings(
            1,
            [
                embeddings.EmbeddingUpload(sample_id=sid, field_name=FIELD, vector=[-5.0, 3.0, float(sid)])
                for sid in chosen
            ],
            USER,
        )

        async def nearest(vector, limit):
            async with db.pool.acquire() as connection, connection.transaction():
                await connection.execute('SET LOCAL enable_seqscan=off')
                await connection.execute('SET LOCAL enable_indexscan=on')
                query = (
                    'SELECT sample_id FROM embeddings WHERE field_name=$1 '
                    'ORDER BY vector::vector(3) <=> $2::vector(3) LIMIT $3'
                )
                plan = '\n'.join(r[0] for r in await connection.fetch('EXPLAIN ' + query, FIELD, vector, limit))
                assert f'hnsw_emb_{FIELD}' in plan, plan
                return sorted(r['sample_id'] for r in await connection.fetch(query, FIELD, vector, limit))

        assert await nearest('[-5,3,1]', 10) == chosen
        assert await nearest('[%s,1,2]' % ids[-1], 30) == ids[10:]

    registry_database(scenario)


def test_deleting_every_vector_leaves_the_map_state_and_counts(registry_database):
    async def scenario(db):
        await seed(db, 3)
        await db.execute("INSERT INTO embedding_map_state(dataset_id,field_name,points_at_refit) VALUES(1,$1,3)", FIELD)
        await db.execute("INSERT INTO embedding_field_models(field_name,model) VALUES($1,'some/checkpoint')", FIELD)
        await datasets.bulk_delete_embeddings(1, FIELD, datasets.BulkTargetRequest(filters={}), USER)
        assert (await finish(db))['result'] == {'deleted': 3}
        assert (await map_rows(db))['embeddings'] == []
        assert await db.fetchval('SELECT count(*) FROM embedding_map_state') == 1
        assert await db.fetchval('SELECT total_points FROM embedding_viz_meta WHERE field_name=$1', FIELD) == 0
        assert await db.fetchval('SELECT model FROM embedding_field_models') == 'some/checkpoint'
        assert (await map_rows(db, OTHER_FIELD))['embeddings'] == [1, 2, 3]

    registry_database(scenario)


@pytest.mark.parametrize(
    'predicate,every',
    [
        ('TRUE', True),
        ('(TRUE AND TRUE)', True),
        ('((TRUE) AND (TRUE)) OR s.id = $1', True),
        ("(metadata->>'label' = $1 OR TRUE)", True),
        ("(metadata->>'label' = $1 AND TRUE)", False),
        ("metadata->>'label' = $1", False),
        ("NOT (metadata->>'label' = $1)", False),
        ('FALSE', False),
    ],
)
def test_a_filter_that_cannot_exclude_a_sample_is_recognised(predicate, every):
    assert bulk_jobs._matches_every_sample(predicate) is every


def test_a_filter_without_values_is_refused_instead_of_deleting_the_whole_field(registry_database):
    async def scenario(db):
        await seed(db, 3)
        for incomplete in (
            {'field': 'uniqueness_model_a', 'operator': '<', 'value': None},
            {'logic': 'AND', 'filters': [{'field': 'uniqueness_model_a', 'operator': '>=', 'value': ''}]},
            {
                'logic': 'OR',
                'filters': [
                    {'field': 'uniqueness_model_a', 'operator': '<', 'value': None},
                    {'field': 'uniqueness_model_a', 'operator': '>', 'value': 0.1},
                ],
            },
        ):
            with pytest.raises(HTTPException) as refused:
                await delete_request(filters=incomplete)
            assert refused.value.status_code == 400 and 'select every sample' in refused.value.detail
        assert await db.fetchval('SELECT count(*) FROM jobs WHERE kind=$1', bulk_jobs.KIND) == 0
        # Values are bound parameters, so a filter on the value True (or the string 'TRUE') is an ordinary filter.
        for value in (True, 'TRUE'):
            await delete_request(filters={'field': 'flag', 'operator': '=', 'value': value})
            assert (await finish(db))['result'] == {'deleted': 0}  # no sample has the field
        # A complete filter is accepted and deletes only what it matches.
        await delete_request(filters={'field': 'uniqueness_model_a', 'operator': '>', 'value': 0.1})
        assert (await finish(db))['result'] == {'deleted': 1}  # values 0, 0.1, 0.2: only 0.2 matches
        assert len((await map_rows(db))['embeddings']) == 2

    registry_database(scenario)


def test_resuming_an_embedding_delete_requires_an_administrator(registry_database, monkeypatch):
    from backend import access
    from backend.routers import jobs as jobs_router

    async def scenario(db):
        monkeypatch.setattr(jobs_router, 'db', db)
        monkeypatch.setattr(access, 'db', db)
        ids = await seed(db, 2)
        job_id = await delete_request(sample_ids=[ids[0]])
        demoted = {'id': USER['id'], 'role': 'power_user'}  # the submitter, since demoted
        with pytest.raises(HTTPException) as refused:
            await jobs_router.resume_bulk_job(job_id, user=demoted)
        assert refused.value.status_code == 403

    registry_database(scenario)
