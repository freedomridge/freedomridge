"""HTTP and PostgreSQL evidence for the field-ingest retry contract."""

import asyncio
import os

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from backend import diskspace
from backend.routers import datasets, embeddings
from backend.sample_ingest import insert_samples
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


def http_client():
    app = FastAPI()
    app.include_router(datasets.router, prefix='/api')
    app.include_router(embeddings.router, prefix='/api')
    # Exercise the real routes, with authentication supplied locally and no app startup. The
    # routers' own routes: FastAPI 0.137+ no longer lists included routes in app.routes.
    for route in [route for router in (datasets.router, embeddings.router) for route in router.routes]:
        if isinstance(route, APIRoute):
            for dependency in route.dependant.dependencies:
                assert dependency.call is not None
                app.dependency_overrides[dependency.call] = lambda: USER
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url='http://ingest.test',
    )


def batch(count=2):
    return [
        {
            'filepath': f's3://field/{index}.jpg',
            'tags': ['field'],
            'metadata': {'score': index, 'ground_truth': [{'label': 'car'}]},
            'embeddings': {'clip': [1.0, 0.0], 'dino': [0.0, 1.0, 0.0]},
            'hashes': {'phash': 'abcd'},
        }
        for index in range(count)
    ]


async def snapshot(database):
    # Compare all durable state, including work queues and watermarks, not just sample counts.
    tables = await database.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename",
    )
    return {
        row['tablename']: await database.fetchval(
            f'SELECT COALESCE(jsonb_agg(value ORDER BY value::text), \'[]\'::jsonb) '
            f'FROM (SELECT to_jsonb(t) value FROM "{row["tablename"]}" t) rows',
        )
        for row in tables
    }


@pytest.mark.parametrize('overlap', ['all', 'partial', 'within'])
def test_default_duplicates_commit_new_samples_and_report_existing(registry_database, overlap):
    async def scenario(database):
        async with http_client() as client:
            samples = batch()
            if overlap != 'within':
                response = await client.post('/api/datasets/1/samples', json={'samples': samples[:1]})
                assert response.status_code == 200
            before = await snapshot(database)
            submitted = samples[:1] if overlap == 'all' else samples
            if overlap == 'within':
                submitted = [samples[0], samples[0], samples[1]]
            response = await client.post('/api/datasets/1/samples', json={'samples': submitted})
            assert response.status_code == 409
            stored = {
                row['filepath']: row['id']
                for row in await database.fetch('SELECT id, filepath FROM samples ORDER BY id')
            }
            new_paths = [] if overlap == 'all' else [sample['filepath'] for sample in samples]
            if overlap == 'partial':
                new_paths = new_paths[1:]
            assert response.json() == {
                'message': '1 duplicate filepath(s)',
                'ids': [stored[path] for path in new_paths],
                'existing': {samples[0]['filepath']: stored[samples[0]['filepath']]},
            }
            assert len(stored) == (1 if overlap == 'all' else 2)
            assert await database.fetchval('SELECT count(*) FROM embeddings') == 2 * len(stored)
            assert await database.fetchval('SELECT count(*) FROM sample_hashes') == len(stored)
            assert await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE field_name = 'score'",
            ) == len(stored)
            after = await snapshot(database)
            if overlap == 'all':
                assert after == before
            else:
                assert all(row in after['samples'] for row in before['samples'])
            response = await client.post('/api/datasets/1/samples', json={'samples': submitted})
            assert response.status_code == 409
            assert response.json()['ids'] == []
            assert response.json()['existing'] == stored
            assert await snapshot(database) == after

    registry_database(scenario)


@pytest.mark.parametrize('measured', [False, True])
def test_skip_replay_and_partial_overlap_only_write_new_samples(registry_database, measured):
    async def scenario(database):
        async with http_client() as client:
            samples = batch()
            response = await client.post(
                '/api/datasets/1/samples',
                json={'samples': samples[:1], 'on_duplicate': 'skip'},
            )
            assert response.status_code == 200
            if measured:
                await run_refresh(database)
            concept_id = await database.fetchval(
                """INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight)
                   VALUES (1, 'car', 'clip', 'seed', '[1,0]') RETURNING id""",
            )
            await database.execute('SELECT score_concept($1, TRUE)', concept_id)
            before = await snapshot(database)
            response = await client.post(
                '/api/datasets/1/samples',
                json={'samples': samples[:1], 'on_duplicate': 'skip'},
            )
            assert response.json()['ids'] == [] and response.json()['skipped'] == 1
            assert await snapshot(database) == before
            samples[0]['metadata'] = {'phantom': 100}
            samples[0]['tags'] = ['changed']
            samples[0]['embeddings']['clip'] = [0, 1]
            samples[0]['hashes']['phash'] = 'ffff'
            response = await client.post(
                '/api/datasets/1/samples',
                json={'samples': samples, 'on_duplicate': 'skip'},
            )
            assert response.status_code == 200 and len(response.json()['ids']) == 1
            assert response.json()['skipped'] == 1
            assert await database.fetchval('SELECT count(*) FROM samples') == 2
            assert await database.fetchval('SELECT count(*) FROM embeddings') == 4
            assert await database.fetchval('SELECT count(*) FROM sample_hashes') == 2
            assert not await database.fetchval("SELECT 1 FROM dataset_fields WHERE field_name = 'phantom'")
            after = await snapshot(database)
            for table in ('samples', 'embeddings', 'sample_hashes'):
                assert all(row in after[table] for row in before[table])
            before = await snapshot(database)
            response = await client.post(
                '/api/datasets/1/samples',
                json={'samples': samples, 'on_duplicate': 'skip'},
            )
            assert response.status_code == 200 and response.json()['skipped'] == 2
            assert await snapshot(database) == before

    registry_database(scenario)


@pytest.mark.parametrize('mode', ['error', 'update'])
def test_failed_mixed_batch_restores_existing_sample_and_artifacts(registry_database, mode):
    async def scenario(database):
        await datasets.add_samples(1, {'samples': batch(1)}, USER)
        await database.execute(
            """CREATE FUNCTION refuse_hash() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN RAISE EXCEPTION 'injected hash failure'; END $$;
               CREATE TRIGGER refuse_hash BEFORE INSERT ON sample_hashes
               FOR EACH ROW EXECUTE FUNCTION refuse_hash()""",
        )
        samples = batch()
        samples[0]['metadata'] = {'replacement': 'changed'}
        samples[0]['tags'] = ['changed']
        samples[0]['embeddings']['clip'] = [0, 1]
        samples[0]['hashes']['phash'] = 'ffff'
        before = await snapshot(database)
        async with http_client() as client:
            response = await client.post(
                '/api/datasets/1/samples',
                json={'samples': samples, 'on_duplicate': mode},
            )
        assert response.status_code == 500
        assert await snapshot(database) == before

    registry_database(scenario)


@pytest.mark.parametrize(
    'table',
    ['samples', 'dataset_fields', 'embeddings', 'sample_hashes', 'dataset_field_stats', 'dataset_stats_pending'],
)
@pytest.mark.parametrize('mode', ['error', 'skip', 'update'])
def test_write_failure_rolls_back_and_retry_stores_once(registry_database, table, mode):
    async def scenario(database):
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='reviewed'), USER)
        # 501 samples cross the set-based insert's 500-row chunk boundary.
        condition = "IF NEW.filepath <> 's3://field/500.jpg' THEN RETURN NEW; END IF;" if table == 'samples' else ''
        await database.execute(
            f"""CREATE FUNCTION refuse_ingest() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN {condition} RAISE EXCEPTION 'injected ingest failure'; END $$;
                CREATE TRIGGER refuse_ingest BEFORE INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION refuse_ingest()""",
        )
        samples = batch(501)
        for sample in samples:
            sample['array_fields'] = {'reviewed': ['yes']}
        async with http_client() as client:
            before = await snapshot(database)
            response = await client.post(
                '/api/datasets/1/samples',
                json={'samples': samples, 'on_duplicate': mode},
            )
            assert response.status_code == 500
            assert await snapshot(database) == before
            await database.execute(f'DROP TRIGGER refuse_ingest ON {table}')
            response = await client.post(
                '/api/datasets/1/samples',
                json={'samples': samples, 'on_duplicate': mode},
            )
            assert response.status_code == 200 and len(response.json()['ids']) == 501
            assert await database.fetchval('SELECT count(*) FROM samples') == 501
            assert await database.fetchval('SELECT count(*) FROM embeddings') == 1002
            assert await database.fetchval('SELECT count(*) FROM sample_hashes') == 501
            assert await database.fetchval("SELECT count(*) FROM samples WHERE reviewed = ARRAY['yes']") == 501
            before = await snapshot(database)
            response = await client.post(
                '/api/datasets/1/samples',
                json={'samples': samples, 'on_duplicate': 'skip'},
            )
            assert response.status_code == 200 and response.json()['skipped'] == 501
            assert await snapshot(database) == before

    registry_database(scenario)


@pytest.mark.parametrize(
    'invalid',
    [
        {'filepath': None},
        {'metadata': {'id': 1}},
        {'hashes': {'phash': 'not hex'}},
        {'embeddings': {'clip': []}},
        {'embeddings': None, 'embedding': []},
        {'array_fields': {'unregistered': ['x']}},
    ],
)
def test_invalid_sample_refuses_whole_request(registry_database, invalid):
    async def scenario(database):
        samples = batch()
        samples[-1].update(invalid)
        before = await snapshot(database)
        async with http_client() as client:
            response = await client.post('/api/datasets/1/samples', json={'samples': samples})
        assert response.status_code == 422
        assert await snapshot(database) == before

    registry_database(scenario)


def test_dimension_mismatch_rejects_whole_batch(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(datasets, 'validate_embedding_dims', embeddings.validate_embedding_dims)
        await datasets.add_samples(2, {'samples': batch(1)}, USER)
        before = await snapshot(database)
        samples = batch()
        samples[-1]['embeddings']['clip'] = [1, 0, 0]
        async with http_client() as client:
            response = await client.post('/api/datasets/2/samples', json={'samples': samples})
            assert response.status_code == 422
            # Consistent inside the request but different from the field's dimension in this dataset.
            response = await client.post('/api/datasets/2/samples', json={'samples': samples[-1:]})
            assert response.status_code == 422
        assert await snapshot(database) == before

    registry_database(scenario)


def test_concurrent_skip_replays_store_each_sample_once(registry_database):
    async def scenario(database):
        async with http_client() as client:
            responses = await asyncio.gather(
                *(
                    client.post('/api/datasets/1/samples', json={'samples': batch(), 'on_duplicate': 'skip'})
                    for _ in range(3)
                ),
            )
        assert all(response.status_code == 200 for response in responses)
        assert sum(len(response.json()['ids']) for response in responses) == 2
        assert await database.fetchval('SELECT count(*) FROM samples') == 2
        assert await database.fetchval('SELECT count(*) FROM embeddings') == 4
        assert await database.fetchval('SELECT count(*) FROM sample_hashes') == 2
        assert (
            await database.fetchval(
                "SELECT sample_count FROM dataset_field_stats WHERE field_name = 'score'",
            )
            == 2
        )

    registry_database(scenario)


@pytest.mark.parametrize('patches', [False, True])
def test_embedding_database_failure_rolls_back_whole_upload(registry_database, patches):
    async def scenario(database):
        ids = (await datasets.add_samples(1, {'samples': batch()}, USER))['ids']
        await database.execute(
            f"""CREATE FUNCTION refuse_embedding() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.sample_id = {ids[-1]} THEN RAISE EXCEPTION 'injected failure'; END IF;
                RETURN NEW; END $$;
                CREATE TRIGGER refuse_embedding BEFORE INSERT ON embeddings
                FOR EACH ROW EXECUTE FUNCTION refuse_embedding()""",
        )
        uploads = [{'sample_id': sample_id, 'vector': [0, 1]} for sample_id in ids]
        if patches:
            for item in uploads:
                item['patch_scale'] = '2x2'
        before = await snapshot(database)
        async with http_client() as client:
            response = await client.post('/api/datasets/1/embeddings' + ('/patches' if patches else ''), json=uploads)
        assert response.status_code == 500
        assert await snapshot(database) == before

    registry_database(scenario)


def test_disk_gate_is_http_507_without_writes(registry_database, monkeypatch):
    async def scenario(database):
        async def no_space(connection):
            return 0

        monkeypatch.setattr(diskspace, 'db_free_bytes', no_space)
        monkeypatch.setattr(datasets, 'require_free_space', diskspace.require_free_space)
        before = await snapshot(database)
        async with http_client() as client:
            response = await client.post('/api/datasets/1/samples', json={'samples': batch()})
        assert response.status_code == 507
        assert await snapshot(database) == before

    registry_database(scenario)


@pytest.mark.parametrize('timeout', ['lock_timeout', 'statement_timeout'])
def test_database_timeout_is_http_error_and_rolls_back(registry_database, monkeypatch, timeout):
    async def scenario(database):
        async def timed_insert(conn, *args):
            await conn.execute(f"SET LOCAL {timeout} = '50ms'")
            return await insert_samples(conn, *args)

        monkeypatch.setattr(datasets, 'insert_samples', timed_insert)
        before = await snapshot(database)
        async with database.pool.acquire() as blocker:
            async with blocker.transaction():
                await blocker.execute('LOCK TABLE embeddings IN ACCESS EXCLUSIVE MODE')
                async with http_client() as client:
                    response = await asyncio.wait_for(
                        client.post('/api/datasets/1/samples', json={'samples': batch()}),
                        timeout=3,
                    )
                assert response.status_code == 500
        assert await snapshot(database) == before

    registry_database(scenario)


@pytest.mark.parametrize('patches', [False, True])
@pytest.mark.parametrize('foreign', [False, True])
def test_embedding_upload_refuses_invalid_ids_without_partial_writes(registry_database, patches, foreign):
    async def scenario(database):
        ids = (await datasets.add_samples(1, {'samples': batch()}, USER))['ids']
        other = (await datasets.add_samples(2, {'samples': batch(1)}, USER))['ids'][0] if foreign else -1
        uploads = [{'sample_id': sample_id, 'field_name': 'extra', 'vector': [1, 0]} for sample_id in [ids[0], other]]
        if patches:
            for item in uploads:
                item['patch_scale'] = '2x2'
        before = await snapshot(database)
        async with http_client() as client:
            response = await client.post('/api/datasets/1/embeddings' + ('/patches' if patches else ''), json=uploads)
        assert response.status_code == 422
        assert response.json()['detail']['unknown_sample_ids'] == [other]
        assert await snapshot(database) == before

    registry_database(scenario)


@pytest.mark.parametrize('key', ['limit', 'offset', 'filters', 'sort_by', 'sort_order', 'fields'])
def test_samples_wrapper_cannot_be_dispatched_as_a_query(registry_database, key):
    async def scenario(database):
        async with http_client() as client:
            response = await client.post(
                '/api/datasets/1/samples',
                json={'samples': batch(), 'on_duplicate': 'skip', key: 1000},
            )
            assert response.status_code == 200
            assert len(response.json()['ids']) == 2
            assert await database.fetchval('SELECT count(*) FROM samples') == 2
            response = await client.post('/api/datasets/1/samples', json={'samples': None, key: 1000})
            assert response.status_code == 422

    registry_database(scenario)
