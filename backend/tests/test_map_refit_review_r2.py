"""Scheduled map re-fit recovery and artifact contract regressions."""

import asyncio
import json
import math
import os
import signal
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import numpy as np
import pytest
from fridge import map_fit
from fridge.core import dataset as sdk_dataset

from backend import field_registry, main, map_refit
from backend.map_artifacts import LocalArtifactStore, model_key
from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_map_refit import FIELD, fake_reducer, map_points, run_job, seed, submit

registry_database = test_field_registry_postgres.registry_database
BACKEND = Path(__file__).resolve().parents[1]
needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


def write_outputs(monkeypatch, source, output, *extra):
    monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
    map_fit.main(['--input', str(source), '--output', str(output), *extra])
    return json.loads((Path(output) / 'manifest.json').read_text())


async def managed_map(database, model=None):
    model = model or uuid.uuid4()
    await database.execute(
        """INSERT INTO embedding_map_state (dataset_id, field_name, model_id, artifact_location, refitted_at)
           VALUES (1, $1, $2, $3, '2026-10-21 08:58+00')""",
        FIELD,
        model,
        model_key(1, FIELD, model),
    )
    return model


def lasso(model):
    leaf = {'field': FIELD, 'operator': 'embedding_polygon', 'value': [[-200, -100], [0, -100], [0, 100], [-200, 100]]}
    return {'logic': 'AND', 'filters': [{**leaf, 'model_id': None if model is None else str(model)}]}


# --- one map check per request, and the query keeps its parallel plan ----------------------------


# The call-site guard lives in test_map_refit_review_r3.py (it also catches None and aliases).


@needs_postgres
def test_the_lasso_map_check_runs_once_and_keeps_the_parallel_plan(registry_database):
    async def scenario(database):
        await seed(database, n=2000)
        model = await managed_map(database)
        parallel = await database.fetchval(
            "SELECT proparallel::text FROM pg_proc WHERE proname = 'require_current_map'",
        )
        assert parallel == 's'
        args: list = [1]
        sql, _ = datasets.parse_filter_node(lasso(model), args, 2, None, None, 1)
        query = f'SELECT count(*) FROM samples WHERE dataset_id = $1 AND {sql}'
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SET LOCAL track_functions = 'pl'")
            assert await connection.fetchval(query, *args) == 2000
            calls = await connection.fetchval(
                "SELECT calls FROM pg_stat_xact_user_functions WHERE funcname = 'require_current_map'",
            )
            plan = '\n'.join(row[0] for row in await connection.fetch(f'EXPLAIN {query}', *args))
        assert calls == 1
        assert 'InitPlan' in plan and 'SubPlan' not in plan

    registry_database(scenario)


# --- the sidebar refuses a stale lasso instead of showing unfiltered values ---------------------


@needs_postgres
def test_the_sidebar_refuses_a_stale_lasso_up_front(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(datasets, 'db', database)
        await seed(database, n=40)
        model = await managed_map(database)
        await datasets.require_current_lassos(1, lasso(model))  # current: accepted
        stale = {'logic': 'NOT', 'filters': [lasso(uuid.uuid4())]}
        for call in (
            lambda: datasets._get_dataset_field_stats_impl(1, stale, lightning_mode=True),
            lambda: datasets._get_dataset_field_stats_impl(1, stale, lightning_mode=False),
            lambda: datasets._autocomplete_impl(1, 'k', '', 10, stale),
            lambda: datasets.get_dataset_schema(1, json.dumps(stale)),
        ):
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='re-fitted'):
                await call()

    registry_database(scenario)


@needs_postgres
def test_the_refusal_tells_the_ui_which_lasso_to_drop(registry_database):
    async def scenario(database):
        await seed(database, n=10)
        model = await managed_map(database)
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError) as refused:
            await database.fetchval("SELECT require_current_map(1, $1, 'an old model')", FIELD)
        response = await main.dataset_lifecycle_error(None, refused.value)  # type: ignore[arg-type]
        body = json.loads(bytes(response.body))
        assert response.status_code == 409
        assert body['code'] == 'map_refitted' and body['dataset_id'] == 1 and body['field'] == FIELD
        assert body['model_id'] == str(model)
        assert 'draw the selection again' in body['detail']

    registry_database(scenario)


# --- the swap ends by its advertised deadline, even inside a running statement ------------------


@needs_postgres
def test_the_swap_deadline_ends_a_statement_still_running_on_the_server(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)

    async def fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
        await asyncio.to_thread(write_outputs, monkeypatch, source, output)

    monkeypatch.setattr(map_refit, 'run_fit', fit)
    monkeypatch.setattr(map_refit, 'PUBLISH_MIN_SECONDS', 10.0)
    monkeypatch.setattr(map_refit, 'GATE_WAIT_SECONDS', 0.5)
    original_take = map_refit._take_gate

    async def slow_swap(connection, dataset_id, exclusive=True):
        await original_take(connection, dataset_id, exclusive=exclusive)
        if exclusive:  # the swap
            await asyncio.sleep(7)  # inside the gate, then a statement that runs past the deadline (10.5 s)
            await connection.execute('SELECT pg_sleep(9)')  # within its own statement_timeout (10 s)

    monkeypatch.setattr(map_refit, '_take_gate', slow_swap)

    async def scenario(database):
        await seed(database, n=60)
        before = await map_points(database)
        late: dict = {}

        async def watch():
            deadline = None
            async with database.pool.acquire() as other:
                while True:
                    seconds = await field_registry.map_swap_seconds(other, 1)
                    held = await other.fetchval(
                        """SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = 20261016
                           AND objid = 1 AND objsubid = 2 AND mode = 'ExclusiveLock' AND granted""",
                    )
                    now = time.monotonic()
                    if seconds is not None and deadline is None:
                        deadline = now + seconds
                    if deadline is not None and not held:
                        late['seconds'] = now - deadline
                        return
                    await asyncio.sleep(0.1)

        watcher = asyncio.create_task(watch())
        job = await run_job(database, await submit(database), store, max_units=4)
        await asyncio.wait_for(watcher, 30)
        assert job['state'] == 'queued' and job['error']['type'] == 'TimeoutError'
        assert late['seconds'] <= 2.5  # was ~6 s: until the statement ended on its own
        assert await map_points(database) == before

    registry_database(scenario)


@needs_postgres
def test_the_swap_409_says_how_long_a_swap_usually_takes(registry_database, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(field_registry, 'BOUNDED_LIFECYCLE_WAIT_SECONDS', 0.2)

    async def scenario(database):
        holder = await database.pool.acquire()
        transaction = holder.transaction()
        await transaction.start()
        try:
            now = await holder.fetchval('SELECT extract(epoch FROM clock_timestamp())::float8')
            await holder.execute(
                f"SET LOCAL application_name = '{field_registry.MAP_SWAP_APPLICATION} {math.ceil(now + 630)}'",
            )
            await holder.execute('SELECT pg_advisory_xact_lock(20261016, 1)')
            async with database.pool.acquire() as connection, connection.transaction():
                with pytest.raises(HTTPException) as refused:
                    await field_registry.lock_field_lifecycle(connection, 1, exclusive=True, wait=False)
            assert 'usually under a minute per million points' in refused.value.detail
            assert 'within at most' in refused.value.detail
        finally:
            await transaction.rollback()
            await database.pool.release(holder)

    registry_database(scenario)


# --- the non-fit points are projected on all cores --------------------------------------------


def test_the_projection_runs_parallel_queries(monkeypatch):
    """pynndescent answers a batch of queries on one thread unless parallel_batch_queries is set; the
    compiled search is dropped so it is rebuilt with the setting (a saved model keeps it: checked with a
    real model: saved, loaded in a fresh process and projected, 2026-10-21)."""
    import umap

    calls = []

    class Index:
        parallel_batch_queries = False

        def __init__(self):
            self._search_function = object()
            self._deheap_function = object()

    class FakeUMAP:
        def __init__(self, **options):
            self._knn_search_index = Index()

        def fit_transform(self, vectors):
            return np.zeros((len(vectors), 2), dtype=np.float32)

        def transform(self, vectors):
            calls.append(self._knn_search_index.parallel_batch_queries)
            assert '_search_function' not in self._knn_search_index.__dict__
            return np.ones((len(vectors), 2), dtype=np.float32)

    monkeypatch.setattr(umap, 'UMAP', FakeUMAP)
    timings: dict = {}
    reducer, coords = sdk_dataset.fit_reducer(np.zeros((120_000, 4), dtype=np.float32), 10_000, timings=timings)
    assert calls == [True, True, True]  # 110,000 projected in 50k batches, all with parallel queries
    assert set(timings) == {'fit_seconds', 'transform_seconds'} and coords.shape == (120_000, 2)


# --- a fit grows at most 1/0.75 beyond the size its cost was measured at ------------------------


@needs_postgres
def test_a_fit_grows_at_most_a_step_beyond_its_measured_size(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)
    sizes = []

    async def fit(command, source, output, log_path, allowance, bytes_per_point=None, max_fit_size=None, shared=False):
        sizes.append((bytes_per_point, max_fit_size))
        await asyncio.to_thread(write_outputs, monkeypatch, source, output)

    monkeypatch.setattr(map_refit, 'run_fit', fit)

    async def scenario(database):
        await seed(database, n=200)
        assert (await run_job(database, await submit(database), store))['state'] == 'succeeded'
        await database.execute(
            'UPDATE embedding_map_state SET fit_bytes_per_point = 20000, fit_size = 120 WHERE field_name = $1',
            FIELD,
        )
        assert (await run_job(database, await submit(database), store))['state'] == 'succeeded'
        assert sizes == [(None, None), (20000, 160)]  # ceil(120 / 0.75)

    registry_database(scenario)


# --- R7c/d: only the fit's own scope is sampled, and only an OOM kill counts as one ----------------


def export_file(path, rows, dim):
    row = np.dtype(
        [
            ('f', '>i2'),
            ('l1', '>i4'),
            ('id', '>i4'),
            ('l2', '>i4'),
            ('v', '>i8'),
            ('l3', '>i4'),
            ('dim', '>i2'),
            ('u', '>i2'),
            ('vec', '>f4', (dim,)),
        ],
    )
    rng = np.random.default_rng(rows)
    with open(path, 'wb') as out:
        out.write(b'PGCOPY\n\xff\r\n\x00' + struct.pack('>ii', 0, 0))
        for start in range(0, rows, 100_000):
            n = min(100_000, rows - start)
            block = np.zeros(n, dtype=row)
            block['f'], block['l1'], block['l2'], block['l3'], block['dim'] = 3, 4, 8, 4 + 4 * dim, dim
            block['id'] = np.arange(start + 1, start + n + 1)
            block['vec'] = rng.normal(size=(n, dim))
            out.write(block.tobytes())
        out.write(struct.pack('>h', -1))


def test_only_the_fits_own_scope_is_sampled():
    assert map_refit._scope_folder(os.getpid()) is None  # this test runs in a session or service cgroup
    child = subprocess.Popen([*map_refit.containment(1024**3), sys.executable, '-c', 'import time; time.sleep(5)'])
    try:
        deadline = time.monotonic() + 5
        folder = None
        while folder is None and time.monotonic() < deadline:
            folder = map_refit._scope_folder(child.pid)
            time.sleep(0.05)
        assert folder is not None and os.path.basename(folder).startswith('run-')
        assert map_refit._memory_value(os.path.join(folder, 'memory.stat'), 'anon') is not None
    finally:
        child.kill()
        child.wait()


def test_a_kill_that_is_not_for_memory_is_not_taken_for_one(tmp_path):
    source = tmp_path / 'vectors.bin'
    export_file(source, 20_000, 64)
    allowance = 2 * 1024**3

    async def killed():
        task = asyncio.create_task(
            map_refit.run_fit(map_refit.containment(allowance), source, tmp_path / 'a', tmp_path / 'a.log', allowance),
        )
        for _ in range(100):  # until the child is in its own scope (and sampled), then kill -9 from outside
            await asyncio.sleep(0.1)
            pids = subprocess.run(['pgrep', '-f', str(tmp_path / 'a')], capture_output=True, text=True).stdout.split()
            scoped = [int(pid) for pid in pids if map_refit._scope_folder(int(pid))]
            if scoped:
                await asyncio.sleep(1.5)
                os.kill(scoped[0], signal.SIGKILL)
                break
        return await task

    with pytest.raises(ConnectionError, match='not for lack of memory'):
        asyncio.run(killed())

    # A real out-of-memory kill inside the cap is still one: 200k x 256-d vectors need ~200 MB of a 150 MB cap.
    big = tmp_path / 'big.bin'
    export_file(big, 200_000, 256)
    cap = 150 * 1024**2
    with pytest.raises(map_refit.FitOutOfMemory):
        asyncio.run(map_refit.run_fit(map_refit.containment(cap), big, tmp_path / 'b', tmp_path / 'b.log', cap))
