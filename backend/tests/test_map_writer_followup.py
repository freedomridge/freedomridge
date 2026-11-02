"""Regression coverage for managed-map writers, using disposable PostgreSQL."""

import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import asyncpg
import numpy as np
import pytest
from fastapi import HTTPException
from fridge.core import dataset as sdk_dataset

from backend import coordinates, dataset_schedules, map_refit
from backend.api_errors import CodedHTTPException
from backend.routers import embeddings, schedules
from backend.tests import test_field_registry_postgres
from backend.tests.test_dataset_schedules import add_schedule
from backend.tests.test_map_refit import FIELD, USER, FakeModel, seed
from backend.tests.test_map_refit_writers import cheap_fit, manage, write
from backend.tests.test_sdk_busy_retry import response, session_returning

registry_database = test_field_registry_postgres.registry_database
cheap_fit = cheap_fit
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')
REFUSAL = 'This map is managed by scheduled re-fits; run a re-fit to change its layout.'


def test_only_refits_can_change_managed_layout(registry_database):
    async def scenario(database):
        await seed(database, n=8)
        await manage(database)
        with pytest.raises(HTTPException) as refused:
            await write('manual')
        assert REFUSAL in str(refused.value.detail)
        assert not any(getattr(route, 'path', '').endswith('/map-state/release') for route in embeddings.router.routes)

    registry_database(scenario)


@pytest.mark.parametrize('managed', [False, True])
def test_queued_refit_allows_incremental_but_not_replacement(registry_database, managed):
    async def scenario(database):
        await seed(database, n=8)
        model = await manage(database) if managed else None
        async with database.pool.acquire() as connection, connection.transaction():
            await map_refit.submit(connection, 1, FIELD)
        version = await database.fetchval('SELECT xmin::text FROM embeddings WHERE sample_id=1')
        await write('incremental', model, version)
        with pytest.raises(HTTPException):
            await write('manual')
        assert await database.fetchval('SELECT x FROM embedding_coordinates WHERE sample_id=1') == 9000

    registry_database(scenario)


def test_incremental_transaction_does_not_block_refit_admission(registry_database):
    async def scenario(database):
        await seed(database, n=8)
        async with database.pool.acquire() as writer, writer.transaction():
            await coordinates.check_map_fence(writer, 1, FIELD)
            async with database.pool.acquire() as submitter, submitter.transaction():
                await submitter.execute("SET LOCAL lock_timeout='200ms'")
                await map_refit.submit(submitter, 1, FIELD)

    registry_database(scenario)


def test_schedule_contention_leaves_only_its_schedule_due(registry_database, monkeypatch):
    async def scenario(database):
        await seed(database, n=30)
        await seed(database, n=30, dataset_id=2, start=1001)
        first = await add_schedule(database, min_new_samples=1, min_days_between=None)
        second = await add_schedule(database, dataset_id=2, min_new_samples=1, min_days_between=None)
        await database.execute(
            "INSERT INTO dataset_job_schedules(dataset_id,job_type,target,enabled,frequency,weekday,"
            "time_of_day,next_run_at) "
            "VALUES(2,'index_review','indexes',TRUE,'daily',NULL,'03:00',$1)",
            first['next_run_at'],
        )
        now = first['next_run_at']
        async with database.pool.acquire() as writer, writer.transaction():
            await coordinates.check_map_fence(writer, 1, FIELD, projected=False)
            queued = await dataset_schedules.schedule_dataset_jobs(database.pool, now)
            assert {job['kind'] for job in queued} == {'map_refit', 'index_review'}
            assert {job['dataset_id'] for job in queued if job['kind'] == 'map_refit'} == {2}
            assert (
                await database.fetchval('SELECT next_run_at FROM dataset_job_schedules WHERE id=$1', first['id']) == now
            )
            monkeypatch.setattr(schedules, 'db', database)
            with pytest.raises(HTTPException) as busy:
                await schedules.run_schedule(1, first['id'], USER)
            assert busy.value.status_code == 409 and (busy.value.headers or {})['Retry-After']
            assert 'layout write' in str(busy.value.detail)
        jobs = await dataset_schedules.schedule_dataset_jobs(database.pool, now)
        assert [job['dataset_id'] for job in jobs] == [1]
        assert await database.fetchval('SELECT last_job_id FROM dataset_job_schedules WHERE id=$1', second['id'])

    registry_database(scenario)


@pytest.mark.parametrize('phase', ['rank', 'swap'])
@pytest.mark.parametrize('refusal', ['managed', 'stale'])
def test_permanent_refusals_keep_code_and_never_retry_during_rank(registry_database, monkeypatch, refusal, phase):
    async def scenario(database):
        await seed(database, n=8)
        await manage(database)
        version = await database.fetchval('SELECT xmin::text FROM embeddings WHERE sample_id=1')
        async with database.pool.acquire() as holder, holder.transaction():
            gate = 'pg_advisory_xact_lock_shared' if phase == 'rank' else 'pg_advisory_xact_lock'
            await holder.execute(f'SELECT {gate}(20261016, 1)')
            until = await holder.fetchval('SELECT ceil(extract(epoch FROM clock_timestamp()))::bigint + 600')
            await holder.execute(f"SET LOCAL application_name='fridge map {phase} until {until}'")
            with pytest.raises(CodedHTTPException) as error:
                await write(
                    'manual' if refusal == 'managed' else 'incremental',
                    str(uuid.uuid4()) if refusal == 'stale' else None,
                    version if refusal == 'stale' else None,
                )
            assert not (error.value.headers or {}).get('Retry-After')
            assert error.value.code == ('map_managed' if refusal == 'managed' else 'map_model_changed')
            session, calls, sleeps = session_returning(
                monkeypatch,
                response(409, error.value.headers),
                response(200),
            )
            assert session.request('POST', 'http://stub/coordinates').status_code == 409
            assert len(calls) == 1 and not sleeps

    registry_database(scenario)


def test_concurrent_reductions_rank_both_fields_after_committing_writes(registry_database, monkeypatch):
    async def scenario(database):
        await seed(database, n=8)
        await seed(database, n=8, start=101, field='dino')
        original = asyncpg.Connection.fetchval
        barrier = asyncio.Barrier(2)

        async def fetchval(connection, query, *args, **kwargs):
            if query.startswith('SELECT recompute_viz_rank'):
                await asyncio.wait_for(barrier.wait(), 10)
            return await original(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetchval', fetchval)
        results = await asyncio.gather(write('reduce'), write('reduce', field='dino'))
        assert all(result['reduced'] == 8 for result in results)
        meta = await database.fetch(
            'SELECT field_name, rank_stale, occupied_cells FROM embedding_viz_meta WHERE dataset_id=1',
        )
        assert len(meta) == 2 and all(not row['rank_stale'] and row['occupied_cells'] > 0 for row in meta)

    registry_database(scenario)


def test_reduce_rank_failure_is_not_reported_as_success(registry_database, monkeypatch):
    async def scenario(database):
        await seed(database, n=8)
        original = asyncpg.Connection.fetchval

        async def fail_rank(connection, query, *args, **kwargs):
            if query.startswith('SELECT recompute_viz_rank'):
                raise RuntimeError('rank failed')
            return await original(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetchval', fail_rank)
        with pytest.raises(CodedHTTPException, match='Coordinates saved; ranking failed') as error:
            await write('reduce')
        assert error.value.status_code == 503 and error.value.code == 'map_rank_failed'

    registry_database(scenario)


def sdk_fixture(monkeypatch, managed=False, refuse=False):
    sdk = object.__new__(sdk_dataset.Dataset)
    sdk._id = 1
    sdk.client = SimpleNamespace(
        get_map_state=Mock(return_value={'managed': managed, 'model_id': str(uuid.uuid4()) if managed else None}),
        get_vectors=Mock(return_value={'total': 8, 'items': [{'sample_id': i, 'vector': [i, 1]} for i in range(1, 9)]}),
        upload_coordinates=Mock(side_effect=RuntimeError('upload refused') if refuse else None),
        recompute_viz_rank=Mock(),
    )
    fit = Mock(return_value=(FakeModel(0), np.ones((8, 2))))
    monkeypatch.setattr(sdk_dataset, 'fit_reducer', fit)
    return sdk, fit


def test_sdk_skips_managed_map_before_fetch_or_fit(monkeypatch, tmp_path, capsys):
    sdk, fit = sdk_fixture(monkeypatch, managed=True)
    result = sdk.compute_visualization(FIELD, 8, save_model_path=str(tmp_path / 'model.joblib'))
    assert result['skipped'] is True and 'managed' in capsys.readouterr().out
    fit.assert_not_called()
    sdk.client.get_vectors.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_sdk_does_not_save_a_refused_model(monkeypatch, tmp_path):
    sdk, _ = sdk_fixture(monkeypatch, refuse=True)
    path = tmp_path / 'model.joblib'
    path.write_bytes(b'previous model')
    with pytest.raises(RuntimeError, match='upload refused'):
        sdk.compute_visualization(FIELD, 8, save_model_path=str(path))
    assert path.read_bytes() == b'previous model'
    assert not Path(str(path) + '.meta.json').exists()


def test_sdk_compute_all_continues_after_managed_map_skip(monkeypatch):
    sdk, fit = sdk_fixture(monkeypatch, managed=True, refuse=True)
    monkeypatch.setattr(
        sdk_dataset.Dataset,
        'coverage',
        lambda self: {'total_samples': 8, 'embeddings': [], 'hashes': []},
    )
    sdk.compute_metadata = Mock()
    sdk.compute_embeddings = Mock()
    sdk.compute_hashes = Mock()
    sdk.compute_metrics = Mock()
    sdk.client.query_embeddings = Mock(return_value={'points': []})
    sdk.compute_all()
    sdk.compute_hashes.assert_called_once()
    sdk.compute_metrics.assert_called_once()
    fit.assert_not_called()


@pytest.mark.parametrize('phase', ['fit', 'rank'])
def test_active_refit_has_retry_deadline_only_in_its_own_rank_phase(registry_database, phase):
    async def scenario(database):
        await seed(database, n=8)
        model = await manage(database)
        async with database.pool.acquire() as connection, connection.transaction():
            job, _ = await map_refit.submit(connection, 1, FIELD)
        await database.execute(
            "UPDATE jobs SET state='running', worker_id='test', lease_token=gen_random_uuid(), "
            "lease_expires_at=clock_timestamp()+interval '10 minutes', checkpoint=$2 WHERE id=$1",
            job['id'],
            {'phase': phase},
        )
        await database.execute('UPDATE embedding_map_state SET active_job_id=$1', job['id'])
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.execute('SELECT pg_advisory_xact_lock_shared(20261016, 1)')
            until = await holder.fetchval('SELECT floor(extract(epoch FROM clock_timestamp()))::bigint + 60')
            await holder.execute(f"SET LOCAL application_name='fridge map rank until {until}'")
            with pytest.raises(CodedHTTPException) as error:
                await write('incremental', model)
            if phase == 'rank':
                assert (error.value.headers or {})['Retry-After'] == '5'
                assert 1 <= int((error.value.headers or {})['X-Map-Swap-Seconds']) <= 60
                assert error.value.code == 'map_rank_busy'
            else:
                assert not (error.value.headers or {}).get('Retry-After')
                assert error.value.code == 'map_refit_active'

    registry_database(scenario)


def test_reduce_waits_for_other_fields_rank_instead_of_failing_after_fit(registry_database, monkeypatch):
    async def scenario(database):
        await seed(database, n=8)
        fitted = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fit(vectors):
            loop.call_soon_threadsafe(fitted.set)
            return np.ones((len(vectors), 2))

        import sys

        monkeypatch.setitem(
            sys.modules,
            'umap',
            SimpleNamespace(UMAP=lambda **kwargs: SimpleNamespace(fit_transform=fit)),
        )
        task = None
        try:
            async with database.pool.acquire() as blocker, blocker.transaction():
                await blocker.execute('SELECT pg_advisory_xact_lock(20261020, 1)')
                task = asyncio.create_task(write('reduce'))
                await asyncio.wait_for(fitted.wait(), 10)
                await asyncio.sleep(5.3)  # The old write transaction's five-second lock timeout.
                assert not task.done(), task.exception() if task.done() else None
            result = await asyncio.wait_for(task, 10)
            assert result['reduced'] == 8
            assert not await database.fetchval('SELECT rank_stale FROM embedding_viz_meta WHERE dataset_id=1')
        finally:
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    registry_database(scenario)


def test_sdk_saves_model_only_after_all_uploads_succeed(monkeypatch, tmp_path):
    sdk, _ = sdk_fixture(monkeypatch)
    path = tmp_path / 'model.joblib'
    sdk.client.upload_coordinates.side_effect = lambda *args, **kwargs: (
        pytest.fail('model appeared before upload') if path.exists() else None
    )
    sdk.compute_visualization(FIELD, 8, save_model_path=str(path))
    assert path.exists() and Path(str(path) + '.meta.json').exists()


def test_sdk_skips_a_virtual_patch_fit_if_any_tile_is_managed(monkeypatch):
    sdk, fit = sdk_fixture(monkeypatch)
    sdk.client.get_map_state.side_effect = lambda dataset, field: {'managed': field.endswith('_r1c1')}
    assert sdk.compute_visualization('clip_patch_2x2', 8)['skipped'] is True
    sdk.client.get_vectors.assert_not_called()
    fit.assert_not_called()
