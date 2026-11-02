"""Managed-map writer contract, on disposable PostgreSQL."""

import asyncio
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from fastapi import HTTPException
from fridge.core import dataset as sdk_dataset

from backend import coordinates, map_refit
from backend.routers import embeddings
from backend.tests import test_field_registry_postgres
from backend.tests.test_map_refit import FIELD, USER, map_points, seed

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')
REFUSAL = 'This map is managed by scheduled re-fits; run a re-fit to change its layout.'


@pytest.fixture(autouse=True)
def cheap_fit(monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    monkeypatch.setitem(
        sys.modules,
        'umap',
        SimpleNamespace(UMAP=lambda **kw: SimpleNamespace(fit_transform=lambda v: np.ones((len(v), 2)))),
    )


async def manage(database, field=FIELD):
    model = uuid.uuid4()
    await database.execute(
        "INSERT INTO embedding_map_state(dataset_id,field_name,model_id,artifact_location,refitted_at,"
        "artifact_checksum,artifact_bytes,dimension,fit_size,points_at_refit,fit_bytes_per_point) "
        "VALUES(1,$1,$2,'managed/original.joblib',clock_timestamp(),'checksum',100,8,8,8,1000)",
        field,
        model,
    )
    return str(model)


async def write(writer, model=None, version=None, field=FIELD):
    if writer == 'reduce':
        return await embeddings.reduce_embeddings(1, embeddings.ReduceRequest(field_name=field), USER)
    return await embeddings.upload_coordinates(
        1,
        field,
        [embeddings.CoordinateUpdate(sample_id=1, x=9000, y=9000, vector_version=version)],
        USER,
        model_id=model,
    )


@pytest.mark.parametrize('writer', ['manual', 'reduce'])
def test_managed_full_replacement_refused(registry_database, writer):
    async def scenario(database):
        await seed(database, n=8)
        model = await manage(database)
        before = await map_points(database)
        with pytest.raises(HTTPException) as refused:
            await write(writer)
        assert refused.value.status_code == 409 and refused.value.detail == REFUSAL
        assert await map_points(database) == before
        assert str(await database.fetchval('SELECT model_id FROM embedding_map_state')) == model

    registry_database(scenario)


@pytest.mark.parametrize('identity', ['missing', 'stale', 'current'])
def test_incremental_identity(registry_database, identity):
    async def scenario(database):
        await seed(database, n=8)
        model = await manage(database)
        version = await database.fetchval('SELECT xmin::text FROM embeddings WHERE sample_id=1')
        before = await map_points(database)
        if identity == 'current':
            await write('upload', model, version)
            assert (await map_points(database))[1] == (9000, 9000)
        else:
            with pytest.raises(HTTPException) as refused:
                await write('upload', None if identity == 'missing' else str(uuid.uuid4()), version)
            assert refused.value.status_code == 409 and model in refused.value.detail
            assert await map_points(database) == before

    registry_database(scenario)


@pytest.mark.parametrize('writer', ['manual', 'reduce', 'incremental'])
@pytest.mark.parametrize('activated', [False, True])
def test_full_writers_refuse_queue_and_incremental_writers_refuse_activation(registry_database, writer, activated):
    async def scenario(database):
        await seed(database, n=8)
        # No map state yet: also cover the first re-fit before its activation step.
        async with database.pool.acquire() as conn, conn.transaction():
            job, _ = await map_refit.submit(conn, 1, FIELD)
        if activated:
            await database.execute(
                'INSERT INTO embedding_map_state(dataset_id,field_name,active_job_id) VALUES(1,$1,$2)',
                FIELD,
                job['id'],
            )
        before = await map_points(database)
        if writer == 'incremental' and not activated:
            await write(writer, version='1')
            return
        with pytest.raises(HTTPException) as refused:
            await write(writer, version='1' if writer == 'incremental' else None)
        assert refused.value.status_code == 409 and 'finishes' in refused.value.detail
        assert await map_points(database) == before

    registry_database(scenario)


def test_reduce_rechecks_after_fit(registry_database, monkeypatch):
    async def scenario(database):
        await seed(database, n=8)
        before = await map_points(database)
        loop = asyncio.get_running_loop()

        def fit(vectors):
            asyncio.run_coroutine_threadsafe(manage(database), loop).result(timeout=5)
            return np.ones((len(vectors), 2))

        monkeypatch.setitem(sys.modules, 'umap', SimpleNamespace(UMAP=lambda **kw: SimpleNamespace(fit_transform=fit)))
        with pytest.raises(HTTPException) as refused:
            await write('reduce')
        assert refused.value.status_code == 409 and refused.value.detail == REFUSAL
        assert await map_points(database) == before

    registry_database(scenario)


def test_patch_scale_reduce_checks_actual_fields(registry_database):
    async def scenario(database):
        tile = 'clip_patch_2x2_r0c0'
        await seed(database, n=8, field=tile)
        await manage(database, tile)
        with pytest.raises(HTTPException) as refused:
            await write('reduce', field='clip_patch_2x2')
        assert refused.value.status_code == 409 and refused.value.detail == REFUSAL

    registry_database(scenario)


@pytest.mark.parametrize('writer', ['manual', 'reduce'])
def test_unmanaged_writes_unchanged(registry_database, writer):
    async def scenario(database):
        await seed(database, n=8)
        await write(writer)
        assert (await map_points(database))[1] == ((1, 1) if writer == 'reduce' else (9000, 9000))
        assert await database.fetchval('SELECT count(*) FROM embedding_map_state') == 0

    registry_database(scenario)


def test_sdk_full_visualization_upload_cannot_replace_managed_map(registry_database, monkeypatch):
    """Real SDK fit/upload flow; its synchronous transport bridges to the real backend writer."""

    async def scenario(database):
        await seed(database, n=8)
        await manage(database)
        before = await map_points(database)
        loop = asyncio.get_running_loop()
        sdk = object.__new__(sdk_dataset.Dataset)
        sdk._id = 1

        def upload(dataset_id, field, batch, **kwargs):
            return asyncio.run_coroutine_threadsafe(
                embeddings.upload_coordinates(
                    dataset_id,
                    field,
                    [embeddings.CoordinateUpdate(**point) for point in batch],
                    USER,
                ),
                loop,
            ).result(timeout=10)

        sdk.client = SimpleNamespace(
            get_map_state=lambda *a, **kw: {'managed': True},
            get_vectors=lambda *a, **kw: {
                'total': 8,
                'items': []
                if kw.get('after_id')
                else [{'sample_id': i, 'vector': [float(i), 1.0]} for i in range(1, 9)],
            },
            upload_coordinates=upload,
            recompute_viz_rank=lambda *a, **kw: None,
        )
        monkeypatch.setattr(sdk_dataset, 'fit_reducer', lambda v, *a, **kw: (None, np.ones((len(v), 2))))
        result = await asyncio.to_thread(sdk.compute_visualization, FIELD, 8)
        assert result['skipped'] is True
        assert await map_points(database) == before

    registry_database(scenario)


def test_first_refit_admission_orders_after_coordinate_write(registry_database):
    async def scenario(database):
        await seed(database, n=8)
        entered = asyncio.Event()

        async def submit():
            async with database.pool.acquire() as conn, conn.transaction():
                entered.set()
                return await map_refit.submit(conn, 1, FIELD)

        task = None
        try:
            async with database.pool.acquire() as conn, conn.transaction():
                await coordinates.check_map_fence(conn, 1, FIELD, projected=False)
                task = asyncio.create_task(submit())
                await entered.wait()
                await asyncio.sleep(0.2)
                assert not task.done(), 'A first re-fit must not be admitted in the middle of a coordinate write'
                await conn.execute(coordinates.SAVE_COORDINATES, [1], [42.0], [42.0], 1, FIELD)
            await task
            assert (await map_points(database))[1] == (42, 42)
            with pytest.raises(HTTPException) as refused:
                await write('manual')
            assert refused.value.status_code == 409
        finally:
            if task is not None:
                await task

    registry_database(scenario)
