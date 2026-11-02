"""Round-three writer regressions; every database is explicitly disposable."""

import json
import os
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import asyncpg
import httpx
import numpy as np
import pytest
import requests
from fastapi import HTTPException
from fridge.core import client as sdk_client

from backend import access, dataset_schedules, map_refit
from backend.main import app
from backend.routers import auth, embeddings, schedules
from backend.tests import test_field_registry_postgres
from backend.tests.test_dataset_schedules import add_schedule
from backend.tests.test_map_refit import FIELD, USER, FakeModel, map_points, seed
from backend.tests.test_map_refit_writers import cheap_fit, manage, write
from backend.tests.test_map_writer_followup import sdk_fixture
from backend.tests.test_sdk_busy_retry import response, session_returning

registry_database = test_field_registry_postgres.registry_database
cheap_fit = cheap_fit
needs_database = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


async def post(database, monkeypatch, path, payload, params=None):
    monkeypatch.setattr(access, 'db', database)
    monkeypatch.setattr(schedules, 'db', database)
    monkeypatch.setattr(auth, 'get_optional_user', AsyncMock(return_value=USER))
    monkeypatch.setitem(app.dependency_overrides, auth.get_current_user, lambda: USER)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        return await client.post(f'/api/datasets/1/{path}', json=payload, params=params)


@needs_database
@pytest.mark.parametrize('phase', ['swap', 'rank'])
@pytest.mark.parametrize('writer', ['project_pending', 'compute_visualization'])
def test_sdk_waits_past_120_seconds_for_another_maps_phase(registry_database, monkeypatch, phase, writer):
    headers: dict[str, str] = {}

    async def scenario(database):
        await seed(database, n=8)
        version = await database.fetchval('SELECT xmin::text FROM embeddings WHERE sample_id=1')
        async with database.pool.acquire() as holder, holder.transaction():
            gate = 'pg_advisory_xact_lock' if phase == 'swap' else 'pg_advisory_xact_lock_shared'
            await holder.execute(f'SELECT {gate}(20261016, 1)')
            await holder.execute('SELECT pg_advisory_xact_lock(20261020, 1)')
            until = await holder.fetchval('SELECT ceil(extract(epoch FROM clock_timestamp()))::bigint + 600')
            await holder.execute(f"SET LOCAL application_name='fridge map {phase} until {until}'")
            with pytest.raises(HTTPException) as refused:
                await write(
                    'manual' if writer == 'compute_visualization' else 'incremental',
                    version=version if writer == 'project_pending' else None,
                )
            headers.update(refused.value.headers or {})
            assert 120 < int(headers.get('X-Map-Swap-Seconds', '0')) <= 601

    registry_database(scenario)
    ok = response(200)
    ok._content = b'{}'
    session, calls, sleeps = session_returning(monkeypatch, *[response(409, headers)] * 22, ok)
    clock = [0.0]
    monkeypatch.setattr(sdk_client.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(
        sdk_client.time,
        'sleep',
        lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds)),
    )
    client = sdk_client.Client('http://stub/api')
    client.session = session
    sdk, _ = sdk_fixture(monkeypatch)
    sdk.client.upload_coordinates = client.upload_coordinates
    if writer == 'compute_visualization':
        assert sdk.compute_visualization(FIELD, 8)['reduced'] == 8
    else:
        sdk.client.get_vectors.side_effect = [sdk.client.get_vectors.return_value, {'items': []}]
        assert sdk.project_pending(FIELD, FakeModel(0)) == 8
    assert sum(sleeps) > 120 and len(calls) == 23


@needs_database
@pytest.mark.parametrize('kind', ['managed', 'stale', 'active', 'own_rank', 'admission'])
def test_new_refusals_use_string_detail_and_top_level_code(registry_database, monkeypatch, kind):
    async def scenario(database):
        await seed(database, n=8)
        model = await manage(database) if kind in ('managed', 'stale', 'own_rank') else None
        if kind in ('active', 'own_rank'):
            async with database.pool.acquire() as conn, conn.transaction():
                job, _ = await map_refit.submit(conn, 1, FIELD)
            if kind == 'own_rank':
                await database.execute(
                    "UPDATE jobs SET state='running', worker_id='test', lease_token=gen_random_uuid(), "
                    "lease_expires_at=clock_timestamp()+interval '10 minutes', checkpoint=$2 WHERE id=$1",
                    job['id'],
                    {'phase': 'rank'},
                )
                await database.execute('UPDATE embedding_map_state SET active_job_id=$1', job['id'])
        async with database.pool.acquire() as holder, holder.transaction():
            if kind == 'admission':
                schedule = await add_schedule(database)
                await holder.execute(
                    'SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
                    json.dumps(['map_refit', 1, FIELD]),
                )
                result = await post(database, monkeypatch, f"schedules/{schedule['id']}/run", {})
            elif kind in ('stale', 'own_rank'):
                await holder.execute('SELECT pg_advisory_xact_lock_shared(20261016, 1)')
                until = await holder.fetchval('SELECT ceil(extract(epoch FROM clock_timestamp()))::bigint + 600')
                await holder.execute(f"SET LOCAL application_name='fridge map rank until {until}'")
                result = await post(
                    database,
                    monkeypatch,
                    'embeddings/coordinates',
                    [{'sample_id': 1, 'x': 1, 'y': 1}],
                    {'field_name': FIELD, 'model_id': model if kind == 'own_rank' else 'stale'},
                )
            else:
                result = await post(database, monkeypatch, 'embeddings/reduce', {'field_name': FIELD})
        assert result.status_code == 409, result.text
        body = result.json()
        assert isinstance(body['detail'], str), body
        assert (
            body['code']
            == {
                'managed': 'map_managed',
                'stale': 'map_model_changed',
                'active': 'map_refit_active',
                'own_rank': 'map_rank_busy',
                'admission': 'map_admission_busy',
            }[kind]
        )
        assert ('retry-after' in result.headers) == (kind in ('own_rank', 'admission'))
        if kind == 'own_rank':
            assert int(result.headers['X-Map-Swap-Seconds']) > 120
        sdk_response = response(409, result.headers)
        sdk_response._content = json.dumps(body).encode()
        with pytest.raises(requests.HTTPError) as error:
            sdk_client._raise_for_status(sdk_response)
        assert str(error.value).endswith(body['detail'])

    registry_database(scenario)


@needs_database
def test_reduce_reports_every_vector_when_max_embeddings_caps_the_fit(registry_database, monkeypatch):
    # total_embeddings used to be the capped row count, so a reduce of the first N vectors looked
    # complete ({"total_embeddings": N, "reduced": N}) while the rest had no coordinates.
    async def scenario(database):
        await seed(database, n=8)
        result = await post(database, monkeypatch, 'embeddings/reduce', {'field_name': FIELD, 'max_embeddings': 5})
        assert result.status_code == 200, result.text
        assert result.json() == {'total_embeddings': 8, 'reduced': 5}

    registry_database(scenario)


@needs_database
def test_reduce_reports_saved_coordinates_and_rank_recovery(registry_database, monkeypatch):
    async def scenario(database):
        await seed(database, n=8)
        original = asyncpg.Connection.fetchval

        async def fail_rank(connection, query, *args, **kwargs):
            if query.startswith('SELECT recompute_viz_rank'):
                raise asyncpg.QueryCanceledError('statement timeout')
            return await original(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetchval', fail_rank)
        result = await post(database, monkeypatch, 'embeddings/reduce', {'field_name': FIELD})
        assert 500 <= result.status_code < 600
        detail = result.json()['detail'].lower()
        assert 'coordinates saved' in detail and 'ranking failed' in detail and 'recompute-viz-rank' in detail
        assert 'retry-after' not in result.headers
        assert set((await map_points(database)).values()) == {(1.0, 1.0)}
        assert await database.fetchval('SELECT rank_stale AND occupied_cells=0 FROM embedding_viz_meta')
        assert await map_refit.heal_unranked_maps(database.pool) == 0  # no automatic retry for unmanaged maps

    registry_database(scenario)


@needs_database
def test_scheduler_logs_traceback_once_per_failure_streak(registry_database, monkeypatch, caplog):
    async def scenario(database):
        await seed(database, n=30)
        schedule = await add_schedule(database, min_new_samples=1, min_days_between=None)
        original = dataset_schedules._queue_refit

        async def broken(*args):
            raise RuntimeError('deliberate schedule bug')

        monkeypatch.setattr(dataset_schedules, '_queue_refit', broken)
        for _ in range(3):
            assert await dataset_schedules.schedule_dataset_jobs(database.pool, schedule['next_run_at']) == []
        records = [record for record in caplog.records if record.name == dataset_schedules.__name__]
        assert len(records) == 1 and records[0].exc_info
        check = await database.fetchval('SELECT last_check FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert 'queue' in check['reason'] and not check['ran']
        monkeypatch.setattr(dataset_schedules, '_queue_refit', original)
        assert len(await dataset_schedules.schedule_dataset_jobs(database.pool, schedule['next_run_at'])) == 1
        following = await database.fetchval('SELECT next_run_at FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        monkeypatch.setattr(dataset_schedules, '_queue_refit', broken)
        await dataset_schedules.schedule_dataset_jobs(database.pool, following)
        records = [record for record in caplog.records if record.name == dataset_schedules.__name__]
        assert len(records) == 2 and records[1].exc_info

    registry_database(scenario)


@needs_database
def test_missed_contended_schedule_keeps_the_real_reason(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        schedule = await add_schedule(database, min_new_samples=1, min_days_between=None)
        async with embeddings.coordinate_write(1, [FIELD]):
            await dataset_schedules.schedule_dataset_jobs(database.pool, schedule['next_run_at'])
        await dataset_schedules.schedule_dataset_jobs(database.pool, schedule['next_run_at'] + timedelta(hours=4))
        check = await database.fetchval('SELECT last_check FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert 'layout write' in check['reason']
        assert 'job runner was not running' not in check['reason'] and not check['ran']

    registry_database(scenario)


@pytest.mark.parametrize('field', [FIELD, 'clip_patch_2x2'])
@pytest.mark.parametrize('status', [404, 403, 500])
def test_compute_visualization_tolerates_only_missing_map_state(monkeypatch, field, status):
    sdk, fit = sdk_fixture(monkeypatch)
    fit.side_effect = lambda vectors, *args: (FakeModel(0), np.ones((len(vectors), 2)))
    sdk.client.get_map_state = Mock(side_effect=requests.HTTPError('map-state unavailable', response=response(status)))
    if status == 404:
        result = sdk.compute_visualization(field, 8)
        assert result['reduced'] == (32 if 'patch' in field else 8)
        fit.assert_called_once()
        assert sdk.client.upload_coordinates.called
    else:
        with pytest.raises(requests.HTTPError):
            sdk.compute_visualization(field, 8)
        fit.assert_not_called()
        sdk.client.get_vectors.assert_not_called()
