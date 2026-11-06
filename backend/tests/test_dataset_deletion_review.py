"""Dataset deletion: temporary reads, name reuse and operator workflows."""

import os
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
from fastapi import FastAPI, HTTPException

from backend import access, job_queue
from backend.routers import auth, datasets, users
from backend.tests.test_dataset_deletion import finish
from backend.tests.test_field_registry_postgres import USER, ingest, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


def test_temporary_results_are_not_dataset_writes(registry_database):
    async def scenario(database):
        ids = (await ingest([{}, {}]))['ids']
        await datasets.delete_dataset(1, USER)
        # No guard or transition-table scan on Select All, including stopped deletions.
        assert not await database.fetchval(
            """SELECT EXISTS(SELECT 1 FROM pg_trigger
               WHERE tgrelid IN ('region_selections'::regclass,'quality_dup_pairs'::regclass)
                 AND tgname LIKE 'deletion_guard_%')""",
        )
        await database.execute("INSERT INTO region_selections(token,sample_id) VALUES('read',$1)", ids[0])
        await database.execute("INSERT INTO quality_dup_pairs(token,a,b) VALUES('read',$1,$2)", *ids)
        await database.execute("UPDATE region_selections SET created_at=now() WHERE token='read'")
        await database.execute("UPDATE quality_dup_pairs SET created_at=now() WHERE token='read'")
        await database.execute("DELETE FROM region_selections WHERE token='read'")
        await database.execute("DELETE FROM quality_dup_pairs WHERE token='read'")

    registry_database(scenario)


def test_name_reusable_while_cancelled_deletion_keeps_identity(registry_database):
    async def scenario(database):
        await database.execute("SELECT setval('datasets_id_seq',2)")
        submitted = await datasets.delete_dataset(1, USER)
        await job_queue.cancel_job(database.pool, submitted['job_id'])
        replacement = await datasets.create_dataset(datasets.DatasetCreate(name='training'), USER)
        assert replacement['id'] != 1
        assert not replacement['deleting']
        assert await database.fetchval('SELECT name FROM datasets WHERE id=1') == 'training'
        with pytest.raises(HTTPException):
            await datasets.create_dataset(datasets.DatasetCreate(name='TRAINING'), USER)
        with pytest.raises(asyncpg.UniqueViolationError):
            await database.execute("INSERT INTO datasets(name) VALUES('training')")
        resumed = await datasets.resume_dataset_deletion(1, USER)
        assert resumed['job_id'] == submitted['job_id']
        await finish(database)
        assert await database.fetchval('SELECT name FROM datasets WHERE id=$1', replacement['id']) == 'training'

    registry_database(scenario)


def test_deleting_dataset_post_reads_and_access_controls(registry_database, monkeypatch):
    async def scenario(database):
        ids = (
            await ingest(
                [
                    {'score': 1, 'heatmap': {'type': 'heatmap', 'map_path': 's3://read-only/map.npy', 'range': [0, 1]}},
                    {'score': 2},
                ],
            )
        )['ids']
        await database.execute('UPDATE datasets SET is_global=FALSE WHERE id=1')
        submitted = await datasets.delete_dataset(1, USER)
        await job_queue.cancel_job(database.pool, submitted['job_id'])
        monkeypatch.setattr(access, 'db', database)
        monkeypatch.setattr(auth, 'get_optional_user', AsyncMock(return_value=USER))
        app = FastAPI()
        app.dependency_overrides[auth.get_current_user] = lambda: USER
        app.include_router(datasets.router, prefix='/api')
        before = await database.fetch('SELECT * FROM dataset_field_stats WHERE dataset_id=1 ORDER BY id')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for path, body in (
                ('samples/query', {}),
                ('samples/lookup', {'field': 'score', 'values': [1, 2]}),
                ('field-stats', {}),
                ('fields/score/autocomplete', {'q': ''}),
                ('map_points', {'points': True}),
                ('map_extent', {}),
                ('selection_token', {}),
                ('aggregate', {'aggregations': [{'field': 'score', 'op': 'count'}]}),
                ('samples/tag-counts', {'sample_ids': ids}),
            ):
                response = await client.post(f'/api/datasets/1/{path}', json=body)
                assert response.status_code == 200, (path, response.text)
                if path == 'field-stats':
                    assert next(field for field in response.json() if field['name'] == 'heatmap')['type'] == 'heatmap'
            assert await database.fetch('SELECT * FROM dataset_field_stats WHERE dataset_id=1 ORDER BY id') == before
            response = await client.post('/api/datasets/1/samples', json={'samples': []})
            assert response.status_code == 409 and 'being deleted' in response.text
            monkeypatch.setattr(auth, 'get_optional_user', AsyncMock(return_value=None))
            response = await client.post('/api/datasets/1/samples/query', json={})
            assert response.status_code == 401
            monkeypatch.setattr(auth, 'get_optional_user', AsyncMock(return_value={'id': 9999, 'role': 'guest'}))
            response = await client.post('/api/datasets/1/samples/query', json={})
            assert response.status_code == 403

    registry_database(scenario)


@pytest.mark.parametrize('existing', [False, True])
def test_cached_read_does_not_publish_after_seal(registry_database, monkeypatch, existing):
    from backend.field_registry import cached_distinct_values

    async def scenario(database):
        if existing:
            await database.execute(
                """INSERT INTO dataset_field_stats(dataset_id,field_name,field_type,unique_values)
                   VALUES(1,'category','enum',ARRAY['before'])""",
            )
        original = asyncpg.Connection.fetch
        query = "SELECT 'after' AS val"

        async def seal_during_measurement(connection, sql, *args, **kwargs):
            rows = await original(connection, sql, *args, **kwargs)
            if sql == query:
                await datasets.delete_dataset(1, USER)
            return rows

        monkeypatch.setattr(asyncpg.Connection, 'fetch', seal_during_measurement)
        assert await cached_distinct_values(database.pool, 1, 'category', query, []) == (['after'], False)
        assert await database.fetchval(
            "SELECT unique_values FROM dataset_field_stats WHERE dataset_id=1 AND field_name='category'",
        ) != ['after']

    registry_database(scenario)


def test_user_delete_explains_deletion_and_rolls_back(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(users, 'db', database)
        uid = await database.fetchval(
            "INSERT INTO users(username,password_hash,role) VALUES('owner','x','power_user') RETURNING id",
        )
        await database.execute('UPDATE datasets SET owner_id=$1 WHERE id=1', uid)
        await database.execute(
            "INSERT INTO views(dataset_id,user_id,name,slug) VALUES(2,$1,'private','private')",
            uid,
        )
        await datasets.delete_dataset(1, USER)
        with pytest.raises(HTTPException) as error:
            await users.delete_user(uid, USER)
        assert error.value.status_code == 409
        assert 'Finish or resume' in error.value.detail
        assert await database.fetchval('SELECT count(*) FROM users WHERE id=$1', uid) == 1
        assert await database.fetchval('SELECT count(*) FROM views WHERE user_id=$1', uid) == 1
        await finish(database)
        assert (await users.delete_user(uid, USER))['message'] == 'User deleted successfully'

    registry_database(scenario)


@pytest.mark.parametrize(
    'write',
    [
        "INSERT INTO samples(dataset_id,filepath,media_type) VALUES(1,'late','image')",
        "UPDATE samples SET tags=ARRAY['late'] WHERE dataset_id=1",
        "UPDATE embeddings SET vector='[0,1]' WHERE sample_id IN (SELECT id FROM samples WHERE dataset_id=1)",
    ],
)
def test_seal_for_update_fences_old_snapshots_after_nonkey_update(registry_database, write):
    async def scenario(database):
        sample_id = (await ingest([{}]))['ids'][0]
        await database.execute(
            "INSERT INTO embeddings(sample_id,field_name,vector) VALUES($1,'clip','[1,0]')",
            sample_id,
        )
        async with database.pool.acquire() as writer, writer.transaction(isolation='repeatable_read'):
            await writer.fetchval('SELECT count(*) FROM samples')
            await database.execute("UPDATE datasets SET description='new metadata' WHERE id=1")
            await datasets.delete_dataset(1, USER)
            with pytest.raises(asyncpg.SerializationError):
                await writer.execute(write)

    registry_database(scenario)


def test_statistics_script_skips_deleting_and_continues_after_sealing_race(registry_database, monkeypatch, capsys):
    from backend import statistics
    from scripts import populate_field_stats

    async def scenario(database):
        await datasets.delete_dataset(1, USER)

        # The fixture uses an isolated schema; give the script its existing pool.
        class PoolContext:
            async def __aenter__(self):
                return database.pool

            async def __aexit__(self, *args):
                pass

        monkeypatch.setattr(asyncpg, 'create_pool', lambda *args, **kwargs: PoolContext())
        request = statistics.request_refresh

        async def seal_before_request(pool, identifier, **kwargs):
            if identifier == 2:
                await datasets.delete_dataset(2, USER)
            return await request(pool, identifier, **kwargs)

        await database.execute("INSERT INTO datasets(id,name) VALUES(3,'third')")
        monkeypatch.setattr(statistics, 'request_refresh', seal_before_request)
        waited = AsyncMock(return_value={'ok': True})
        monkeypatch.setattr(statistics, 'wait_for_refresh', waited)
        await populate_field_stats.populate_field_stats()
        assert waited.await_count == 1
        assert await database.fetchval("SELECT dataset_id FROM jobs WHERE kind='statistics'") == 3
        with pytest.raises(RuntimeError, match='being deleted'):
            await populate_field_stats.populate_field_stats(1)
        assert 'Skipping dataset 2' in capsys.readouterr().err

    registry_database(scenario)
