"""The server-side ANN-index build validates its inputs before touching DDL, as a runner job.

The job interpolates field + dim into CREATE INDEX (identifier + literal), so an unsafe field
name must be rejected outright (422, before anything is queued), a field with no vectors is a
404, mixed dimensions a 422, and an already-indexed field is a cheap no-op that reports
built=False. The request answers within five seconds; the build itself is a job.
"""

import asyncio
import json
import os
import time

import pytest
from fastapi import HTTPException

from backend import access, index_jobs, job_queue, job_wait
from backend.routers import jobs as jobs_router
from backend.routers import quality
from backend.tests import job_helpers
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
USER = {'id': 1, 'role': 'admin'}
KIND = index_jobs.ANN_KIND
needs_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


def _run(field):
    return asyncio.run(quality.build_ann_index(1, quality.AnnIndexRequest(field=field), user=USER))


def test_unsafe_field_name_rejected_before_any_query(monkeypatch):
    monkeypatch.setattr(quality, "db", None)
    with pytest.raises(HTTPException) as ei:
        _run("clip'; DROP TABLE embeddings; --")
    assert ei.value.status_code == 422


async def vectors(database, field, values):
    for index, value in enumerate(values):
        sample_id = await database.fetchval(
            "INSERT INTO samples(dataset_id,filepath,media_type) VALUES(1,$1,'image') RETURNING id",
            f'{field}-{index}',
        )
        await database.execute(
            'INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) VALUES('
            '(SELECT dataset_id FROM samples WHERE id = $1),$1,$2,$3::text::vector)',
            sample_id,
            field,
            value,
        )


async def index_state(database, name):
    return await database.fetchval(
        'SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid '
        'WHERE c.relname=$1 AND c.relnamespace=current_schema()::regnamespace',
        name,
    )


@needs_database
def test_no_embeddings_mixed_dimensions_and_existing_index(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(quality, 'db', database)
        await vectors(database, 'mixed', ['[1,0]', '[1,0,0]'])
        await vectors(database, 'clip', ['[1,0]', '[0,1]', '[1,1]'])
        # As after the upgrade (0021): each build removes its field's row once it has an index or nothing to index.
        await database.execute(
            "INSERT INTO ann_index_rebuilds (dataset_id, field_name) VALUES (1, 'absent'), (1, 'mixed'), (1, 'clip')",
        )
        async with job_helpers.runner(database, kinds=[KIND]):
            with pytest.raises(HTTPException) as missing:
                await quality.build_ann_index(1, quality.AnnIndexRequest(field='absent'), user=USER)
            assert missing.value.status_code == 404
            with pytest.raises(HTTPException) as mixed:
                await quality.build_ann_index(1, quality.AnnIndexRequest(field='mixed'), user=USER)
            assert mixed.value.status_code == 422 and 'mixed dimensions' in mixed.value.detail
            built = await quality.build_ann_index(1, quality.AnnIndexRequest(field='clip'), user=USER)
            assert set(built) == {'field', 'dim', 'index', 'built', 'seconds'}
            assert built['field'] == 'clip' and built['dim'] == 2 and built['built'] is True
            assert await index_state(database, 'hnsw_emb_d1_clip') is True
            again = await quality.build_ann_index(1, quality.AnnIndexRequest(field='clip'), user=USER)
            assert again == {'field': 'clip', 'dim': 2, 'built': False, 'detail': 'index already exists'}
            # Mixed dimensions stay queued, to be tried again once they are cleaned up.
            assert await database.fetchval('SELECT array_agg(field_name) FROM ann_index_rebuilds') == ['mixed']
            # The jobs list names each build's field, which its Retry builds again.
            monkeypatch.setattr(jobs_router, 'db', database)
            monkeypatch.setattr(access, 'db', database)
            page = await jobs_router.list_dataset_jobs(1, limit=10, cursor=None, user=USER)
            assert {(job.field, job.state) for job in page.items} >= {('mixed', 'failed'), ('clip', 'succeeded')}
        # The session that built the index left no lock or settings behind.
        async with database.pool.acquire() as connection:
            assert await connection.fetchval('SHOW maintenance_work_mem') != '256MB'

    registry_database(scenario)


@needs_database
def test_slow_build_answers_202_and_leaves_no_invalid_index_when_cancelled(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(quality, 'db', database)
        monkeypatch.setattr(job_wait, 'ANSWER_SECONDS', 0.3)
        await vectors(database, 'clip', ['[1,0]', '[0,1]'])
        # An open snapshot makes CREATE INDEX CONCURRENTLY wait in its last phase, with the
        # invalid index already in the catalog — the state a crash or cancel would leave.
        blocker = await database.pool.acquire()
        transaction = blocker.transaction(isolation='repeatable_read')
        await transaction.start()
        await blocker.fetchval('SELECT count(*) FROM datasets')
        try:
            async with job_helpers.runner(database, kinds=[KIND]):
                response = await quality.build_ann_index(1, quality.AnnIndexRequest(field='clip'), user=USER)
                assert response.status_code == 202
                job_id = json.loads(response.body)['job_id']

                async def building():
                    while await index_state(database, 'hnsw_emb_d1_clip') is not False:
                        await asyncio.sleep(0.05)

                await asyncio.wait_for(building(), 20)
                await job_queue.cancel_job(database.pool, job_id)

                async def ended():
                    while await database.fetchval('SELECT state FROM jobs WHERE id=$1::uuid', job_id) == 'running':
                        await asyncio.sleep(0.05)

                await asyncio.wait_for(ended(), 20)
        finally:
            await transaction.rollback()
            await database.pool.release(blocker)
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1::uuid', job_id) == 'cancelled'
        assert await index_state(database, 'hnsw_emb_d1_clip') is None

    registry_database(scenario)


@needs_database
def test_a_cancelled_hnsw_build_blocked_by_a_reader_is_swept_later(registry_database, monkeypatch):
    """An INVALID HNSW carcass is maintained on every insert into the field."""

    async def scenario(database):
        monkeypatch.setattr(quality, 'db', database)
        monkeypatch.setattr(job_wait, 'ANSWER_SECONDS', 0.2)
        await vectors(database, 'clip', ['[1,0]', '[0,1]', '[1,1]'])
        reader = await database.pool.acquire()
        transaction = reader.transaction(isolation='repeatable_read')
        await transaction.start()
        await reader.fetchval('SELECT count(*) FROM embeddings')
        try:
            async with job_helpers.runner(database, kinds=[KIND]):
                response = await quality.build_ann_index(1, quality.AnnIndexRequest(field='clip'), user=USER)
                job_id = json.loads(response.body)['job_id']

                async def building():
                    while await index_state(database, 'hnsw_emb_d1_clip') is not False:
                        await asyncio.sleep(0.05)

                await asyncio.wait_for(building(), 20)
                await job_queue.cancel_job(database.pool, job_id)

                async def ended():
                    while await database.fetchval('SELECT state FROM jobs WHERE id=$1::uuid', job_id) == 'running':
                        await asyncio.sleep(0.1)

                await asyncio.wait_for(ended(), 60)
            assert await index_state(database, 'hnsw_emb_d1_clip') is False
            started = time.monotonic()
            assert await index_jobs.sweep_invalid_indexes(database.pool) == []
            # A busy table must not stall the job service's scheduling loop (previously 5 s each).
            assert time.monotonic() - started < 2
        finally:
            await transaction.rollback()
            await database.pool.release(reader)
        assert await index_jobs.sweep_invalid_indexes(database.pool) == ['hnsw_emb_d1_clip']
        assert await index_state(database, 'hnsw_emb_d1_clip') is None

    registry_database(scenario)
