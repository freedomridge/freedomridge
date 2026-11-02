"""Guest admission must protect expensive queries without throttling browsing."""

import asyncio
import os
import time

import pytest
from fastapi import Depends
from PIL import Image

from backend.request_limits import _guest_slots, guest_heavy
from backend.routers import auth
from backend.tests.test_public_auth import account, login_as
from backend.tests.test_public_auth import auth_database as _auth_database
from backend.tests.test_public_auth import registry_database as _registry_database

registry_database = _registry_database
auth_database = _auth_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


async def wait_for_requests(user_id, count):
    async with asyncio.timeout(2):
        while user_id not in _guest_slots or _guest_slots[user_id].requests != count:
            await asyncio.sleep(0.001)


def test_media_variants_and_session_reads_bypass_busy_guest(auth_database, monkeypatch, tmp_path):
    from backend import main

    monkeypatch.setenv('FRIDGE_GUEST_CONCURRENCY', '1')
    monkeypatch.setenv('FRIDGE_GUEST_QUEUE_WAIT_SECONDS', '0.05')
    monkeypatch.setattr(main, 'DATA_ROOT', str(tmp_path))
    monkeypatch.setattr(main, 'CROP_CACHE_PATH', str(tmp_path / 'crops'))
    monkeypatch.setattr(main, 'COMPOSITE_CACHE_PATH', str(tmp_path / 'composites'))
    Image.new('RGB', (32, 32), 'green').save(tmp_path / 'sample.png')
    Image.new('L', (32, 32), 128).save(tmp_path / 'heatmap.png')

    async def scenario(database, client):
        monkeypatch.setattr(main, 'db', database)
        user_id = await account(database, 'media-guest', demo=True)
        await database.execute('UPDATE datasets SET is_global=true WHERE id=1')
        sample_id = await database.fetchval(
            "INSERT INTO samples(dataset_id,filepath,media_type,metadata) "
            "VALUES (1,'sample.png','image',$1) RETURNING id",
            {
                'heatmap': {'type': 'heatmap', 'map_path': 'heatmap.png', 'range': [0, 255]},
                'crop_bbox': [0, 0, 0.5, 0.5],
            },
        )
        app = client._transport.app
        app.add_api_route('/api/samples/{sample_id}/media', main.serve_sample_media, methods=['GET'])
        app.add_api_route('/api/config', main.get_config, methods=['GET'])
        app.add_api_route('/api/health', main.health, methods=['GET'])
        finish = asyncio.Event()

        @app.post('/api/search')
        @guest_heavy
        async def hold(user=Depends(auth.get_current_user)):
            await finish.wait()
            return {'ok': True}

        login_as(client, 'media-guest')
        first = asyncio.create_task(client.post('/api/search'))
        try:
            await wait_for_requests(user_id, 1)
            media = f'/api/samples/{sample_id}/media'
            paths = [media + suffix for suffix in ('', '?w=16', '?crop=meta&w=16', '?overlay=heatmap&w=16')]
            paths += ['/api/auth/me', '/api/config', '/api/health', '/api/datasets', '/api/datasets/1/views']
            responses = await asyncio.gather(*(client.get(path) for path in paths * 3))
            assert [(r.status_code, r.text[:100] if r.status_code != 200 else '') for r in responses] == [
                (200, ''),
            ] * 27
            assert _guest_slots[user_id].requests == 1
        finally:
            finish.set()
            assert (await first).status_code == 200
        assert not _guest_slots

    auth_database(scenario)


def test_queued_query_succeeds_without_holding_database_connection(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_GUEST_CONCURRENCY', '1')
    monkeypatch.setenv('FRIDGE_GUEST_QUEUE_WAIT_SECONDS', '1')
    monkeypatch.setenv('FRIDGE_DB_ACQUIRE_TIMEOUT_SECONDS', '0.03')

    async def scenario(database, client):
        user_id = await account(database, 'queued-guest')
        finish = asyncio.Event()
        entered = 0
        app = client._transport.app

        @app.post('/api/search')
        @guest_heavy
        async def hold(user=Depends(auth.get_current_user)):
            nonlocal entered
            entered += 1
            await finish.wait()
            return {'value': await database.fetchval('SELECT 42')}

        login_as(client, 'queued-guest')
        first = asyncio.create_task(client.post('/api/search'))
        second = None
        try:
            await wait_for_requests(user_id, 1)
            started = time.monotonic()
            second = asyncio.create_task(client.post('/api/search'))
            await wait_for_requests(user_id, 2)
            # A queue wait longer than the DB acquire budget is safe: no checkout
            # belongs to the waiter, and the holder can still acquire to finish.
            await asyncio.sleep(0.06)
            assert entered == 1 and not second.done()
            assert database.pool.get_idle_size() == database.pool.get_size()
            assert await database.fetchval('SELECT 1') == 1
            finish.set()
            response = await asyncio.wait_for(second, 1)
            assert response.status_code == 200 and response.json() == {'value': 42}
            assert time.monotonic() - started >= 0.06
        finally:
            finish.set()
            await first
            if second is not None:
                await second
        assert entered == 2 and not _guest_slots

    auth_database(scenario)


def test_cancelled_waiter_does_not_leak_or_split_guest_slots(auth_database, monkeypatch):
    monkeypatch.setenv('FRIDGE_GUEST_CONCURRENCY', '1')

    async def scenario(database, client):
        user_id = await account(database, 'cancel-guest')
        finish = asyncio.Event()
        app = client._transport.app

        @app.post('/api/search')
        @guest_heavy
        async def hold(user=Depends(auth.get_current_user)):
            await finish.wait()
            return {'ok': True}

        login_as(client, 'cancel-guest')
        first = asyncio.create_task(client.post('/api/search'))
        second = None
        try:
            await wait_for_requests(user_id, 1)
            slots = _guest_slots[user_id]
            second = asyncio.create_task(client.post('/api/search'))
            await wait_for_requests(user_id, 2)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert _guest_slots[user_id] is slots
            assert slots.requests == 1 and slots.semaphore.locked()
        finally:
            finish.set()
            await first
            if second is not None:
                second.cancel()
                await asyncio.gather(second, return_exceptions=True)
        assert not _guest_slots
        assert (await client.post('/api/search')).status_code == 200

    auth_database(scenario)


@pytest.mark.parametrize('demo,role,expected_peak', [(True, 'guest', 2), (False, 'guest', 6), (True, 'admin', 6)])
def test_heavy_burst_preserves_concurrency_and_local_development(auth_database, monkeypatch, demo, role, expected_peak):
    async def scenario(database, client):
        monkeypatch.setenv('DEMO_MODE', str(demo).lower())
        await account(database, 'burst-user', role=role)
        active = peak = 0
        app = client._transport.app

        @app.post('/api/search')
        @guest_heavy
        async def query(user=Depends(auth.get_current_user)):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.1)
                return {'ok': True}
            finally:
                active -= 1

        login_as(client, 'burst-user')
        responses = await asyncio.gather(*(client.post('/api/search') for _ in range(6)))
        assert [r.status_code for r in responses] == [200] * 6
        assert peak == expected_peak
        assert not _guest_slots

    auth_database(scenario)
