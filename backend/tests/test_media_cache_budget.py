"""Cache maintenance must never serialize requests, expose partial files or cap media."""

import asyncio
import fcntl
import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from backend import main, media_cache
from scripts.prune_cache import prune


def _writer(root, number):
    # Independent workers can overshoot the soft budget between maintenance passes.
    for _ in range(20):
        media_cache.write_cache(f'{root}/shared', bytes([number]) * 4096, (root,), 8192)


def test_atomic_concurrent_process_writes_and_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(media_cache, '_track_write', lambda *args: None)
    root = str(tmp_path)
    processes = [multiprocessing.get_context('fork').Process(target=_writer, args=(root, i)) for i in range(4)]
    for process in processes:
        process.start()
    observed = []
    while any(process.is_alive() for process in processes):
        content = media_cache.read_cache(f'{root}/shared')
        if content is not None:
            observed.append(content)
            assert len(content) == 4096 and len(set(content)) == 1
        prune([root], 8192)
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    assert observed
    assert not list(tmp_path.rglob('*.tmp'))
    assert prune([root], 0) == 0


def test_write_never_walks_or_takes_eviction_lock_and_failure_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(media_cache, '_track_write', lambda *args: None)

    def forbidden(*args):
        raise AssertionError('No cache traversal or global flock in write_cache')

    monkeypatch.setattr(os, 'walk', forbidden)
    monkeypatch.setattr(fcntl, 'flock', forbidden)
    target = tmp_path / 'image'
    media_cache.write_cache(str(target), b'old', (str(tmp_path),), 100)
    monkeypatch.setattr(os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        media_cache.write_cache(str(target), b'new contents', (str(tmp_path),), 100)
    assert target.read_bytes() == b'old'
    assert not list(tmp_path.glob('*.tmp'))


def test_background_reconciliation_evicts_oldest_and_rate_limits_workers(tmp_path, monkeypatch):
    roots = tuple(str(tmp_path / name) for name in ['crop', 'composite', 's3'])
    monkeypatch.setattr(media_cache, '_track_write', lambda *args: None)
    for i, root in enumerate(roots):
        media_cache.write_cache(f'{root}/item', b'x' * 30, roots, 100)
        os.utime(f'{root}/item', (i + 1, i + 1))
    media_cache.write_cache(f'{roots[1]}/new', b'y' * 40, roots, 100)
    tracker = media_cache._Budget(roots, 100)
    assert tracker.reconcile(130) == 100
    assert not (tmp_path / 'crop/item').exists()
    assert (tmp_path / 's3/item').exists()
    assert media_cache._Budget(roots, 100).reconcile(1) == 101
    with open(tmp_path / 'crop/.cache-budget.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert tracker.reconcile(1) is None  # non-blocking, including between processes
    media_cache.write_cache(f'{roots[0]}/oversized', b'z' * 101, roots, 100)
    assert not (tmp_path / 'crop/oversized').exists()


def _background_budget_probe(root):
    os.environ['FRIDGE_CACHE_MAX_BYTES'] = '100'
    os.environ.pop('FRIDGE_CACHE_MAINTENANCE_SECONDS', None)
    media_cache._MAINTENANCE_SECONDS = 0.05
    for i in range(6):
        media_cache.write_cache(f'{root}/{i}', b'x' * 60, (root,), 100)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        files = [p for p in Path(root).iterdir() if p.name.isdigit()]
        if files and sum(p.stat().st_size for p in files if p.exists()) <= 100:
            return
        time.sleep(0.02)
    raise AssertionError('Background trigger did not reconcile and prune')


def test_incremental_budget_triggers_eventual_background_eviction(tmp_path):
    process = multiprocessing.get_context('spawn').Process(target=_background_budget_probe, args=(str(tmp_path),))
    process.start()
    process.join(10)
    assert process.exitcode == 0


@pytest.mark.parametrize(
    'settings,enabled',
    [
        ({}, False),
        ({'FRIDGE_ENV': 'development', 'DEMO_MODE': 'false'}, False),
        ({'FRIDGE_CACHE_MAINTENANCE_SECONDS': '0.01'}, False),
        ({'FRIDGE_ENV': 'production'}, True),
        ({'FRIDGE_ENV': 'prod'}, True),
        ({'DEMO_MODE': 'true'}, True),
        ({'FRIDGE_CACHE_MAX_BYTES': '100'}, True),
    ],
)
def test_background_maintenance_is_opt_in_locally(tmp_path, monkeypatch, settings, enabled):
    for name in ('FRIDGE_ENV', 'DEMO_MODE', 'FRIDGE_CACHE_MAX_BYTES', 'FRIDGE_CACHE_MAINTENANCE_SECONDS'):
        monkeypatch.delenv(name, raising=False)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    thread = Mock()
    monkeypatch.setattr(threading, 'Thread', thread)
    monkeypatch.setattr(media_cache, '_budgets', {})
    walk = Mock(side_effect=AssertionError('No walk on local writes'))
    monkeypatch.setattr(os, 'walk', walk)
    media_cache.write_cache(str(tmp_path / 'image'), b'pixels', (str(tmp_path),), 100)
    assert (tmp_path / 'image').read_bytes() == b'pixels'
    assert thread.call_count == int(enabled)
    assert len(media_cache._budgets) == int(enabled)
    walk.assert_not_called()


def test_shared_estimate_skips_walk_until_over_budget_and_uses_monotonic_time(tmp_path, monkeypatch):
    monkeypatch.setenv('FRIDGE_CACHE_MAINTENANCE_SECONDS', '900')
    now = [1000.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(time, 'time', lambda: 100000.0)
    prune_mock = Mock(return_value=70)
    monkeypatch.setattr(media_cache, 'prune', prune_mock)
    first = media_cache._Budget((str(tmp_path),), 100)
    second = media_cache._Budget((str(tmp_path),), 100)
    assert first.reconcile(60) == 60
    now[0] += 900
    assert second.reconcile(40) == 100
    prune_mock.assert_not_called()
    assert first.reconcile(1) == 70
    assert prune_mock.call_count == 1
    assert second.reconcile(31) == 101  # shared scan interval still applies above budget
    assert prune_mock.call_count == 1
    monkeypatch.setattr(time, 'time', lambda: 1.0)  # wall clock steps backwards
    now[0] += 900
    assert second.reconcile(0) == 70
    assert prune_mock.call_count == 2
    now[0] = 10.0  # monotonic origin resets on reboot; persisted metadata must recover
    assert first.reconcile(31) == 70
    assert prune_mock.call_count == 3


def test_maintenance_interval_defaults_to_fifteen_minutes_and_is_configurable(monkeypatch):
    monkeypatch.delenv('FRIDGE_CACHE_MAINTENANCE_SECONDS', raising=False)
    assert media_cache._Budget(('unused',), 100).interval == 900
    monkeypatch.setenv('FRIDGE_CACHE_MAINTENANCE_SECONDS', '1800')
    assert media_cache._Budget(('unused',), 100).interval == 1800


def test_prune_removes_only_hour_old_tmp_files_even_under_budget(tmp_path):
    now = time.time()
    for name, age in [('orphan.tmp', 3601), ('active.tmp', 300), ('cached', 7200)]:
        path = tmp_path / name
        path.write_bytes(b'pixels')
        os.utime(path, (now - age, now - age))
    assert prune([str(tmp_path)], 100) == len(b'pixels')
    assert not (tmp_path / 'orphan.tmp').exists()
    assert (tmp_path / 'active.tmp').read_bytes() == b'pixels'
    assert (tmp_path / 'cached').read_bytes() == b'pixels'


async def test_slow_writes_start_after_response_and_do_not_starve_cache_reads(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_write(*args):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(main, '_write_media_cache', slow_write)
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(main, '_CACHE_WRITERS', executor)
    source = tmp_path / 'hit'
    source.write_bytes(b'cached')
    from starlette.background import BackgroundTask
    from starlette.responses import Response

    sent = []

    async def send(message):
        sent.append(message['type'])
        assert not entered.is_set()

    response = Response(b'image', background=BackgroundTask(main._cache_media_later, str(source), b'image'))
    task = asyncio.create_task(response({'type': 'http'}, AsyncMock(), send))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        assert sent == ['http.response.start', 'http.response.body']
        assert await asyncio.wait_for(asyncio.to_thread(media_cache.read_cache, str(source)), 0.2) == b'cached'
    finally:
        release.set()
        await task
        executor.shutdown()
