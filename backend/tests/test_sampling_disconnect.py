"""Real TCP disconnects through the app's HTTP middleware must stop sampling work."""

import asyncio
import socket
import threading
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn

from backend import main
from backend.access import require_dataset_access
from backend.database import db
from backend.routers import sampling
from backend.routers.auth import get_optional_user


@pytest.mark.parametrize(
    'role,env,demo,disconnect',
    [
        ('admin', 'development', 'false', True),
        ('power_user', 'development', 'false', True),
        ('guest', 'development', 'true', True),
        ('guest', 'development', 'true', False),
        ('admin', 'production', 'false', False),
    ],
)
async def test_sampling_stops_through_real_middleware(monkeypatch, role, env, demo, disconnect):
    monkeypatch.setattr(sampling, '_SAMPLING_SLOTS', asyncio.Semaphore(1))
    monkeypatch.setenv('FRIDGE_ENV', env)
    monkeypatch.setenv('DEMO_MODE', demo)
    monkeypatch.setenv('FRIDGE_SAMPLING_BUDGET_SECONDS', '3' if disconnect else '0.2')
    monkeypatch.setattr(sampling, 'get_dataset_array_columns', AsyncMock(return_value=[]))
    monkeypatch.setattr(db, 'fetch', AsyncMock(return_value=[{'id': 1, 'meta_score': '2'}]))
    started, finished = threading.Event(), threading.Event()

    def calculate(rows, dimensions, request, stop, t0):
        started.set()
        try:
            assert stop.wait(5), 'Worker outlived disconnect/budget'
            sampling._check_cancelled(stop)
        finally:
            finished.set()

    monkeypatch.setattr(sampling, '_calculate_sample', calculate)
    monkeypatch.setitem(main.app.dependency_overrides, require_dataset_access, lambda: None)
    monkeypatch.setitem(main.app.dependency_overrides, get_optional_user, lambda: {'role': role})
    server = uvicorn.Server(uvicorn.Config(main.app, lifespan='off', log_level='error'))
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(3):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}') as client:
                request = {'fields': ['score'], 'count': 1}
                if disconnect:
                    with pytest.raises(httpx.ReadTimeout):
                        await client.post('/api/datasets/1/sparse-sample', json=request, timeout=0.3)
                else:
                    response = await client.post('/api/datasets/1/sparse-sample', json=request, timeout=2)
                    assert response.status_code == 503
                    assert response.json()['detail'] == 'Sparse sample took too long; try a smaller count or percentage'
                    assert response.headers['Retry-After'] == '5'
                assert started.is_set()
                async with asyncio.timeout(1):
                    while not finished.is_set() or sampling._SAMPLING_SLOTS.locked():
                        await asyncio.sleep(0.005)
                monkeypatch.setattr(sampling, '_calculate_sample', lambda *args: {'sample_ids': [1], 'count': 1})
                response = await client.post('/api/datasets/1/sparse-sample', json=request)
                assert response.status_code == 200 and response.json()['sample_ids'] == [1]
        finally:
            server.should_exit = True
            await task


@pytest.mark.parametrize('role', ['admin', 'power_user', 'guest'])
@pytest.mark.parametrize('env', ['development', 'production', 'prod'])
@pytest.mark.parametrize('demo', ['false', 'true'])
@pytest.mark.parametrize('configured', [False, True])
async def test_sampling_budget_is_scoped_without_truncating_results(monkeypatch, role, env, demo, configured):
    from fastapi import HTTPException

    monkeypatch.setenv('FRIDGE_ENV', env)
    monkeypatch.setenv('DEMO_MODE', demo)
    monkeypatch.delenv('FRIDGE_SAMPLING_BUDGET_SECONDS', raising=False)
    if configured:
        monkeypatch.setenv('FRIDGE_SAMPLING_BUDGET_SECONDS', '0.01')
    user = {'role': role}
    limited = (demo == 'true' and role == 'guest') or (env in ('production', 'prod') and configured)
    assert sampling._sampling_budget(user) == ((0.01 if configured else 60) if limited else None)
    monkeypatch.setattr(sampling, '_SAMPLING_SLOTS', asyncio.Semaphore(1))
    result = {'sample_ids': list(range(100)), 'count': 100, 'candidate_count': 100}
    stops = []

    async def calculate(dataset_id, request, stop):
        stops.append(stop)
        await asyncio.sleep(0.03)
        return result

    monkeypatch.setattr(sampling, '_sparse_sample', calculate)
    request = sampling.SparseSampleRequest(fields=['score'], percentage=100)
    if limited and configured:
        with pytest.raises(HTTPException) as error:
            await sampling.sparse_sample(1, request, user=user)
        assert error.value.status_code == 503
    else:
        assert await sampling.sparse_sample(1, request, user=user) == result
    assert stops[0].is_set()
    assert not sampling._SAMPLING_SLOTS.locked()
