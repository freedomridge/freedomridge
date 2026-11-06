"""Round-three writer contracts: HTTP errors, sibling pauses, and scheduler failures."""

import asyncio
import json
import os
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import httpx
import pytest
import requests
from fridge.core import client as sdk_client

from backend import access, coordinates, dataset_schedules, job_queue, main, map_refit
from backend.routers import auth, schedules
from backend.tests import test_field_registry_postgres
from backend.tests.test_dataset_schedules import add_schedule
from backend.tests.test_map_refit import FIELD, USER, map_points, seed
from backend.tests.test_map_refit_writers import cheap_fit, manage
from backend.tests.test_map_writer_followup import sdk_fixture

registry_database = test_field_registry_postgres.registry_database
cheap_fit = cheap_fit
requires_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Disposable PostgreSQL required',
)


@pytest.fixture
def http(monkeypatch):
    """Exercise production error handlers without starting the application's services."""
    monkeypatch.setattr(auth, 'get_optional_user', AsyncMock(return_value=USER))
    monkeypatch.setitem(main.app.dependency_overrides, auth.get_current_user, lambda: USER)

    async def request(database, path, payload=None, params=None, method='POST'):
        monkeypatch.setattr(access, 'db', database)
        monkeypatch.setattr(schedules, 'db', database)
        transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            return await client.request(method, f'/api/datasets/1/{path}', json=payload, params=params)

    return request


@requires_database
@pytest.mark.parametrize('phase', ['swap', 'rank'])
def test_sdk_waits_past_two_minutes_for_a_siblings_phase(registry_database, monkeypatch, http, phase):
    """Real DB refusal + HTTP body + SDK retry loop; virtual time covers a three-minute pause."""

    async def scenario(database):
        await seed(database, n=8)
        await seed(database, n=8, start=101, field='dino')
        sdk, _ = sdk_fixture(monkeypatch)
        sdk.client = sdk_client.Client(base_url='http://test/api')
        items = [{'sample_id': i, 'vector': [i, 1]} for i in range(1, 9)]
        for item in items:
            item['vector_version'] = await database.fetchval(
                'SELECT xmin::text FROM embeddings WHERE sample_id=$1',
                item['sample_id'],
            )
        monkeypatch.setattr(
            sdk.client,
            'get_vectors',
            lambda *a, **kw: {'total': 8, 'items': [] if kw.get('after_id') else items},
        )
        monkeypatch.setattr(sdk.client, 'recompute_viz_rank', Mock())
        clock = [0.0]
        monkeypatch.setattr(
            sdk_client,
            'time',
            SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
        )
        monkeypatch.setattr(sdk_client.random, 'uniform', lambda low, high: low)
        loop = asyncio.get_running_loop()
        async with database.pool.acquire() as holder:
            transaction = holder.transaction()
            await transaction.start()
            released = False
            refusal = None

            async def send(method, url, **kwargs):
                nonlocal released, refusal
                path = url.split('/api/datasets/1/')[1]
                if path == 'embeddings/coordinates' and refusal is not None:
                    if clock[0] < 180:
                        return refusal
                    if not released:
                        await transaction.rollback()
                        released = True
                result = await http(database, path, kwargs.get('json'), kwargs.get('params'), method)
                response = requests.Response()
                response.status_code = result.status_code
                response.headers.update(result.headers)
                response._content = result.content
                response.url = url
                if result.status_code == 409:
                    refusal = response
                return response

            def transport(session, method, url, **kwargs):
                return asyncio.run_coroutine_threadsafe(send(method, url, **kwargs), loop).result(timeout=15)

            monkeypatch.setattr(requests.Session, 'request', transport)
            gate = 'pg_advisory_xact_lock' if phase == 'swap' else 'pg_advisory_xact_lock_shared'
            await holder.execute(f'SELECT {gate}(20261016, 1)')
            await holder.execute('SELECT pg_advisory_xact_lock(20261020, 1)')
            until = await holder.fetchval('SELECT ceil(extract(epoch FROM clock_timestamp()))::bigint + 600')
            await holder.execute(f"SET LOCAL application_name='fridge map {phase} until {until}'")
            try:
                result = await asyncio.to_thread(sdk.compute_visualization, FIELD, 8)
                assert result['reduced'] == 8
                assert clock[0] >= 180
                assert refusal is not None and int(refusal.headers['X-Map-Swap-Seconds']) > 500
                assert refusal.json()['code'] == f'map_{phase}_busy'
                assert min(x for x, y in (await map_points(database)).values()) > 0
            finally:
                if not released:
                    await transaction.rollback()

    registry_database(scenario)


@requires_database
@pytest.mark.parametrize('refusal', ['managed', 'changed', 'active', 'busy', 'admission', 'queue'])
def test_writer_refusals_use_string_detail_and_top_level_code(registry_database, http, monkeypatch, refusal):
    async def scenario(database):
        await seed(database, n=8)
        path, params = 'embeddings/reduce', None
        payload: dict | list | None = {'field_name': FIELD}
        expected = {
            'managed': 'map_managed',
            'changed': 'map_model_changed',
            'active': 'map_refit_active',
            'busy': 'dataset_busy',
            'admission': 'map_admission_busy',
            'queue': 'job_queue_busy',
        }[refusal]
        if refusal in ('managed', 'changed'):
            await manage(database)
        if refusal == 'changed':
            path, payload = 'embeddings/coordinates', [{'sample_id': 1, 'x': 1, 'y': 1}]
            params = {'field_name': FIELD, 'model_id': str(uuid.uuid4())}
        if refusal == 'active':
            async with database.pool.acquire() as conn, conn.transaction():
                await map_refit.submit(conn, 1, FIELD)
        if refusal in ('admission', 'queue'):
            schedule = await add_schedule(database)
            path, payload = f"schedules/{schedule['id']}/run", None
        if refusal == 'queue':
            monkeypatch.setattr(dataset_schedules, 'run_now', AsyncMock(side_effect=job_queue.QueueBusy()))
        async with database.pool.acquire() as holder, holder.transaction():
            if refusal == 'busy':
                await holder.execute('SELECT pg_advisory_xact_lock(20261016, 1)')
            elif refusal == 'admission':
                await coordinates.check_map_fence(holder, 1, FIELD, projected=False)
            response = await http(database, path, payload, params)
        assert response.status_code == 409, response.text
        body = response.json()
        assert isinstance(body['detail'], str) and body['detail']
        assert body['code'] == expected
        assert bool(response.headers.get('Retry-After')) == (refusal in ('busy', 'admission', 'queue'))
        sdk_response = requests.Response()
        sdk_response.status_code = 409
        sdk_response._content = json.dumps(body).encode()
        with pytest.raises(requests.HTTPError) as error:
            sdk_client._raise_for_status(sdk_response)
        assert str(error.value).endswith(body['detail'])

    registry_database(scenario)


@requires_database
def test_reduce_rank_failure_reports_saved_coordinates_and_can_retry_only_ranking(registry_database, monkeypatch, http):
    async def scenario(database):
        await seed(database, n=8)
        original = asyncpg.Connection.fetchval

        async def fail_rank(connection, query, *args, **kwargs):
            if query.startswith('SELECT recompute_viz_rank'):
                raise asyncpg.QueryCanceledError('statement timeout')
            return await original(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetchval', fail_rank)
        response = await http(database, 'embeddings/reduce', {'field_name': FIELD})
        assert response.status_code == 503, response.text
        assert response.json()['code'] == 'map_rank_failed'
        assert 'coordinates saved; ranking failed' in response.json()['detail'].lower()
        assert 'recompute-viz-rank' in response.json()['detail']
        assert 'automatically' not in response.json()['detail']
        assert 'Retry-After' not in response.headers  # repeating the reduction would refit the saved layout
        points = await map_points(database)
        assert set(points.values()) == {(1.0, 1.0)}
        assert await database.fetchval('SELECT rank_stale FROM embedding_viz_meta')
        assert await map_refit.heal_unranked_maps(database.pool) == 0  # unmanaged maps have no automatic heal
        monkeypatch.setattr(asyncpg.Connection, 'fetchval', original)
        ranked = await http(database, 'embeddings/recompute-viz-rank', params={'field_name': FIELD, 'full': True})
        assert ranked.status_code == 200
        assert not await database.fetchval('SELECT rank_stale FROM embedding_viz_meta')
        assert await map_points(database) == points

    registry_database(scenario)


@requires_database
def test_scheduler_logs_traceback_once_per_failure_streak(registry_database, monkeypatch, caplog):
    async def scenario(database):
        await seed(database, n=8)
        first = await add_schedule(database, min_new_samples=None, min_days_between=None)
        await add_schedule(database, dataset_id=2, min_new_samples=None, min_days_between=None)
        original = dataset_schedules._queue_refit
        monkeypatch.setattr(dataset_schedules, '_queue_refit', AsyncMock(side_effect=RuntimeError('schedule failed')))
        for _ in range(3):
            assert await dataset_schedules.schedule_dataset_jobs(database.pool, first['next_run_at']) == []
        logs = [record for record in caplog.records if record.name == dataset_schedules.__name__]
        assert len(logs) == 2 and all(record.exc_info for record in logs)
        monkeypatch.setattr(dataset_schedules, '_queue_refit', original)
        assert len(await dataset_schedules.schedule_dataset_jobs(database.pool, first['next_run_at'])) == 2
        monkeypatch.setattr(dataset_schedules, '_queue_refit', AsyncMock(side_effect=RuntimeError('failed again')))
        await dataset_schedules.schedule_dataset_jobs(database.pool, first['next_run_at'] + timedelta(days=1))
        logs = [record for record in caplog.records if record.name == dataset_schedules.__name__]
        assert len(logs) == 4 and all(record.exc_info for record in logs)

    registry_database(scenario)


@requires_database
def test_contended_schedule_missed_window_does_not_blame_runner_downtime(registry_database):
    async def scenario(database):
        await seed(database, n=8)
        schedule = await add_schedule(database, min_new_samples=None, min_days_between=None)
        async with database.pool.acquire() as writer, writer.transaction():
            await coordinates.check_map_fence(writer, 1, FIELD, projected=False)
            assert await dataset_schedules.schedule_dataset_jobs(database.pool, schedule['next_run_at']) == []
        await dataset_schedules.schedule_dataset_jobs(database.pool, schedule['next_run_at'] + timedelta(hours=4))
        check = await database.fetchval('SELECT last_check FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert check['ran'] is False and 'Missed' in check['reason']
        assert 'job runner was not running' not in check['reason']
        assert 'layout write' in check['reason']

    registry_database(scenario)


@pytest.mark.parametrize('status', [404, 403, 500])
def test_sdk_compute_visualization_supports_servers_without_map_state(monkeypatch, status):
    sdk, fit = sdk_fixture(monkeypatch)
    response = requests.Response()
    response.status_code = status
    sdk.client.get_map_state.side_effect = requests.HTTPError('map-state unavailable', response=response)
    if status == 404:
        assert sdk.compute_visualization(FIELD, 8)['reduced'] == 8
        fit.assert_called_once()
        sdk.client.upload_coordinates.assert_called_once()
    else:
        with pytest.raises(requests.HTTPError):
            sdk.compute_visualization(FIELD, 8)
        fit.assert_not_called()
