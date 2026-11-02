"""Regression coverage for dataset schedules and map re-fitting."""

import asyncio
import json
import math
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import numpy as np
import pytest
import requests
from fastapi import HTTPException
from fridge import map_fit
from fridge.core import client as sdk_client
from fridge.core import dataset as sdk_dataset

from backend import backups, dataset_schedules, field_registry, job_queue, map_refit
from backend.map_artifacts import LocalArtifactStore, model_key
from backend.routers import datasets, embeddings, jobs
from backend.tests import test_field_registry_postgres
from backend.tests.test_dataset_schedules import add_schedule, local
from backend.tests.test_map_refit import (
    FIELD,
    USER,
    assert_ranks_exact,
    fake_reducer,
    map_points,
    map_state,
    run_job,
    seed,
    submit,
)

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


def write_outputs(monkeypatch, source, output, *extra):
    monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
    map_fit.main(['--input', str(source), '--output', str(output), *extra])
    return json.loads((Path(output) / 'manifest.json').read_text())


# --- the swap's ingest pause --------------------------------------------------------------------


async def hold_like_a_swap(database, dataset_id, seconds_left):
    """A session that holds the dataset's gate exclusively, named as the publish names itself."""
    name = field_registry.MAP_SWAP_APPLICATION
    connection = await database.pool.acquire()
    transaction = connection.transaction()
    await transaction.start()
    now = await connection.fetchval('SELECT extract(epoch FROM clock_timestamp())::float8')
    await connection.execute(f"SET LOCAL application_name = '{name} {math.ceil(now + seconds_left)}'")
    await connection.execute('SELECT pg_advisory_xact_lock(20261016, $1)', dataset_id)
    return connection, transaction


def test_bounded_writers_are_told_how_long_a_map_swap_holds_the_dataset(registry_database, monkeypatch):
    monkeypatch.setattr(field_registry, 'BOUNDED_LIFECYCLE_WAIT_SECONDS', 0.2)

    async def scenario(database):
        holder, transaction = await hold_like_a_swap(database, 1, 240)
        try:
            async with database.pool.acquire() as connection, connection.transaction():
                with pytest.raises(HTTPException) as refused:
                    await field_registry.lock_field_lifecycle(connection, 1, exclusive=True, wait=False)
            assert refused.value.status_code == 409
            headers = refused.value.headers or {}
            seconds = int(headers[field_registry.MAP_SWAP_HEADER])
            assert 230 <= seconds <= 241
            assert headers['Retry-After'] == '5'
            assert 'map is being swapped in' in refused.value.detail
            # Another dataset is not held, and an ordinary holder gets the usual short answer.
            async with database.pool.acquire() as connection, connection.transaction():
                await field_registry.lock_field_lifecycle(connection, 2, exclusive=True, wait=False)
        finally:
            await transaction.rollback()
            await database.pool.release(holder)
        async with database.pool.acquire() as ordinary, ordinary.transaction():
            await ordinary.execute('SELECT pg_advisory_xact_lock(20261016, 1)')
            async with database.pool.acquire() as connection, connection.transaction():
                with pytest.raises(HTTPException) as refused:
                    await field_registry.lock_field_lifecycle(connection, 1, exclusive=True, wait=False)
            headers = refused.value.headers or {}
            assert field_registry.MAP_SWAP_HEADER not in headers
            assert headers['Retry-After'] == '1'

    registry_database(scenario)


def test_the_sdk_keeps_retrying_until_the_advertised_end_of_the_swap(monkeypatch):
    clock = [0.0]
    calls = []

    def response(status, headers):
        reply = requests.Response()
        reply.status_code = status
        reply.headers.update(headers)
        return reply

    def fake_request(self, *args, **kwargs):
        calls.append(clock[0])
        if clock[0] < 200:  # the swap holds the dataset for 200 s
            return response(409, {'Retry-After': '5', 'X-Map-Swap-Seconds': str(int(240 - clock[0]))})
        return response(200, {})

    monkeypatch.setattr(requests.Session, 'request', fake_request)
    monkeypatch.setattr(sdk_client.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(sdk_client.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    session = sdk_client._Session()
    assert session.busy_retry_seconds < 200
    reply = session.request('POST', 'http://api/datasets/1/samples', json={})
    assert reply.status_code == 200 and calls[-1] >= 200
    # Bounded: a swap that does not end by its deadline (plus the usual window) is given up on.
    clock[0], calls[:] = 0.0, []

    def never_ends(self, *args, **kwargs):
        calls.append(clock[0])
        return response(409, {'Retry-After': '5', 'X-Map-Swap-Seconds': '30'})

    monkeypatch.setattr(requests.Session, 'request', never_ends)
    assert session.request('POST', 'http://api/x').status_code == 409
    assert calls[-1] <= 30 + session.busy_retry_seconds + 12


def test_the_publish_advertises_its_deadline_and_ends_the_swap_by_it(
    registry_database,
    tmp_path,
    monkeypatch,
):
    store = LocalArtifactStore(tmp_path)
    advertised = []

    async def fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
        await asyncio.to_thread(write_outputs, monkeypatch, source, output)

    monkeypatch.setattr(map_refit, 'run_fit', fit)

    async def scenario(database):
        await seed(database, n=60)
        original_take = map_refit._take_gate

        async def take_and_look(connection, dataset_id, exclusive=True):
            await original_take(connection, dataset_id, exclusive=exclusive)
            if exclusive:  # the swap (the rank step takes it shared)
                async with database.pool.acquire() as other:
                    advertised.append(await field_registry.map_swap_seconds(other, dataset_id))

        monkeypatch.setattr(map_refit, '_take_gate', take_and_look)
        assert (await run_job(database, await submit(database), store))['state'] == 'succeeded'
        assert advertised and (advertised[0] or 0) >= map_refit.PUBLISH_MIN_SECONDS
        # A swap that runs past its deadline is ended (rolled back) and retried, never held longer.
        before = await map_points(database)
        monkeypatch.setattr(map_refit, 'PUBLISH_MIN_SECONDS', 1.0)
        monkeypatch.setattr(map_refit, 'GATE_WAIT_SECONDS', 0.5)

        async def slow_swap(connection, dataset_id, exclusive=True):  # slow under the swap's gate
            await original_take(connection, dataset_id, exclusive=exclusive)
            if exclusive:
                await asyncio.sleep(3)

        monkeypatch.setattr(map_refit, '_take_gate', slow_swap)
        job = await run_job(database, await submit(database), store, max_units=4)
        assert job['state'] == 'queued' and job['error']['type'] == 'TimeoutError'
        assert 'not swapped in within' in job['error']['message']
        assert await map_points(database) == before
        async with database.pool.acquire() as other:
            assert await field_registry.map_swap_seconds(other, 1) is None  # the gate was released

    registry_database(scenario)


async def assert_grid_ranks(database, total):
    """recompute_viz_rank's invariants: within each of the 200x200 cells of the map's extents the ranks
    are 0..n-1, rank-0 rows = occupied cells, extents = the placed points', total exact, not stale."""
    meta = await database.fetchrow('SELECT * FROM embedding_viz_meta WHERE dataset_id=1 AND field_name=$1', FIELD)
    extents = await database.fetchrow(
        'SELECT min(x) x0, max(x) x1, min(y) y0, max(y) y1 FROM embedding_coordinates WHERE field_name=$1',
        FIELD,
    )
    assert (meta['x_min'], meta['x_max'], meta['y_min'], meta['y_max']) == tuple(extents)
    cells = await database.fetch(
        """SELECT count(*) n, min(rank) low, max(rank) high, count(DISTINCT rank) distinct_ranks FROM (
               SELECT rank, LEAST(GREATEST(FLOOR((x - $2) / NULLIF($3 - $2, 0) * 200)::int, 0), 199) cx,
                      LEAST(GREATEST(FLOOR((y - $4) / NULLIF($5 - $4, 0) * 200)::int, 0), 199) cy
               FROM embedding_viz_rank WHERE dataset_id=1 AND field_name=$1) r GROUP BY cx, cy""",
        FIELD,
        *extents,
    )
    assert all(cell['low'] == 0 and cell['high'] == cell['n'] - 1 == cell['distinct_ranks'] - 1 for cell in cells)
    assert meta['occupied_cells'] == len(cells) and meta['total_points'] == total and not meta['rank_stale']
    await assert_ranks_exact(database)


def test_ranks_are_prepared_before_the_gate_and_match_a_full_re_rank(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)

    async def fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
        await asyncio.to_thread(write_outputs, monkeypatch, source, output)

    monkeypatch.setattr(map_refit, 'run_fit', fit)

    async def scenario(database):
        await seed(database, n=3000, dim=4)
        finished = await run_job(database, await submit(database), store)
        assert finished['state'] == 'succeeded'
        assert finished['result']['publish_seconds']['gate_held'] >= 0
        await assert_grid_ranks(database, 3000)

    registry_database(scenario)


def test_a_point_changed_during_the_fit_falls_back_to_an_exact_re_rank(registry_database, tmp_path, monkeypatch):
    """The prepared ranks assume every exported point is kept; when some changed during the fit (here:
    deleted, including the map's extreme points), the publish ranks the kept points exactly."""
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=2000, dim=4)

        async def fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
            await asyncio.to_thread(write_outputs, monkeypatch, source, output)
            coords = np.load(Path(output) / 'coords.npy')
            ids = np.load(Path(output) / 'ids.npy')
            extremes = {int(ids[i]) for i in (coords[:, 0].argmin(), coords[:, 0].argmax(), coords[:, 1].argmax())}
            gone = sorted(extremes | {int(i) for i in ids[:22]})
            await database.execute('DELETE FROM samples WHERE id = ANY($1::int[])', gone)
            removed.extend(gone)

        removed: list = []
        monkeypatch.setattr(map_refit, 'run_fit', fit)
        finished = await run_job(database, await submit(database), store)
        assert finished['state'] == 'succeeded' and finished['result']['skipped_changed'] == len(removed)
        await assert_grid_ranks(database, 2000 - len(removed))

    registry_database(scenario)


# --- a lasso drawn on the previous map ---------------------------------------------------------


def test_a_lasso_drawn_on_a_previous_map_is_refused_not_silently_reused(registry_database):
    async def scenario(database):
        await seed(database, n=40)
        polygon = [[-200, -100], [0, -100], [0, 100], [-200, 100]]

        async def count(node):
            args = [1]
            sql, _ = datasets.parse_filter_node(node, args, 2, None, None, 1)
            return await database.fetchval(f'SELECT count(*) FROM samples WHERE dataset_id=$1 AND {sql}', *args)

        lasso = {'field': FIELD, 'operator': 'embedding_polygon', 'value': polygon}
        assert await count(lasso) == 40  # a field without a re-fitted map accepts any polygon
        current = uuid.uuid4()
        await database.execute(
            """INSERT INTO embedding_map_state (dataset_id, field_name, model_id, artifact_location, refitted_at)
               VALUES (1, $1, $2, $3, '2026-10-21 08:58+00')""",
            FIELD,
            current,
            model_key(1, FIELD, current),
        )
        assert await count({**lasso, 'model_id': str(current)}) == 40
        for stale in ({**lasso, 'model_id': str(uuid.uuid4())}, lasso):
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='re-fitted on 2026-10-21 08:58'):
                await count(stale)
            # Inside OR/NOT too, and without a known dataset (checked through the sample).
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                args: list = [1]
                sql, _ = datasets.parse_filter_node({'logic': 'NOT', 'filters': [stale]}, args, 2)
                await database.fetchval(f'SELECT count(*) FROM samples WHERE dataset_id=$1 AND {sql}', *args)

    registry_database(scenario)


# --- the sweep never deletes the model that just became current ---------------------------------


class ProxyConnection:
    def __init__(self, real, after_first_fetch):
        self.real, self.after, self.calls = real, after_first_fetch, 0

    async def fetch(self, *args, **kwargs):
        rows = await self.real.fetch(*args, **kwargs)
        self.calls += 1
        if self.calls == 1:
            await self.after()  # a publish commits right after the sweep's (first) read
        return rows


class ProxyPool:
    def __init__(self, pool, after):
        self.pool, self.after = pool, after

    def acquire(self, timeout=None):
        pool, after = self.pool, self.after

        class Context:
            async def __aenter__(self):
                self.connection = await pool.acquire()
                return ProxyConnection(self.connection, after)

            async def __aexit__(self, *exc):
                await pool.release(self.connection)

        return Context()


def test_the_sweep_keeps_a_model_published_while_it_reads(registry_database, tmp_path):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        old_model, new_model = uuid.uuid4(), uuid.uuid4()
        job = await job_queue.enqueue_job(database.pool, 'map_refit', dataset_id=1, lane='maintenance')
        await database.execute(
            """UPDATE jobs SET state='running', started_at=clock_timestamp(), worker_id='w',
               lease_token=gen_random_uuid(), lease_expires_at=clock_timestamp()+interval '1 hour', checkpoint=$2
               WHERE id=$1""",
            job['id'],
            {'phase': 'publish', 'model_id': str(new_model)},
        )
        await database.execute(
            """INSERT INTO embedding_map_state(dataset_id,field_name,model_id,artifact_location,active_job_id)
               VALUES (1,$1,$2,$3,$4)""",
            FIELD,
            old_model,
            model_key(1, FIELD, old_model),
            job['id'],
        )
        for model in (old_model, new_model):
            path = store.path(model_key(1, FIELD, model))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'model')

        async def publish():
            async with database.pool.acquire() as connection, connection.transaction():
                await connection.execute(
                    """UPDATE embedding_map_state SET model_id=$2, artifact_location=$3, active_job_id=NULL
                       WHERE dataset_id=1 AND field_name=$1""",
                    FIELD,
                    new_model,
                    model_key(1, FIELD, new_model),
                )
                await connection.execute(
                    """UPDATE jobs SET state='succeeded', finished_at=clock_timestamp(), lease_token=NULL,
                       lease_expires_at=NULL WHERE id=$1""",
                    job['id'],
                )

        await map_refit.sweep(ProxyPool(database.pool, publish), store)
        assert store.path(model_key(1, FIELD, new_model)).exists()

    registry_database(scenario)


# --- fit size from the cap and the measured cost per point ---------------------------------------


def test_the_default_cost_per_point_is_calibrated_and_mirrored_by_the_runner():
    """Measured (see FIT_VECTOR_COPIES): 512-d fits cost up to ~27 KB per fit point at the
    tree-count plateau, so the default with its 25% headroom must cover that; the runner's skip check
    uses the same default."""
    assert map_refit.FIT_VECTOR_COPIES == sdk_dataset.FIT_VECTOR_COPIES
    assert sdk_dataset.default_fit_bytes_per_point(512) / 0.75 >= 27_000 + 512 * 4
    assert sdk_dataset.default_fit_bytes_per_point(1024) == 2 * sdk_dataset.default_fit_bytes_per_point(512)
    rows, dim = 4_000_000, 512
    assert map_refit.minimum_fit_bytes(rows, dim) == (
        rows * dim * 4 + int(50_000 * dim * 4 * 13 / 0.75) + rows * 24 + map_refit.CHILD_BASELINE_BYTES
    )
    assert map_refit.minimum_fit_bytes(rows, dim, 60_000) > map_refit.minimum_fit_bytes(rows, dim)


def test_the_fit_size_uses_a_measured_cost_per_point_instead_of_the_default(monkeypatch):
    monkeypatch.setattr(sdk_dataset, '_available_ram_bytes', lambda: 40 * 1024**3)
    default = sdk_dataset._auto_fit_sample_size(10_000_000, 512)
    assert default == int(40 * 1024**3 * 0.75) // sdk_dataset.default_fit_bytes_per_point(512)
    measured = sdk_dataset._auto_fit_sample_size(10_000_000, 512, bytes_per_point=40_000)
    assert measured == int(40 * 1024**3 * 0.75) // 40_000
    assert sdk_dataset._auto_fit_sample_size(100_000, 512, bytes_per_point=40_000) == 100_000  # never above total


def test_an_out_of_memory_kill_retries_smaller_and_the_next_fit_starts_there(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)
    attempts = []

    async def fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
        attempts.append((Path(output).name, bytes_per_point))
        if len(attempts) == 1:  # the kernel kills the first attempt at its cap, mid-fit
            Path(output).mkdir(parents=True, exist_ok=True)
            plan = {'rows': 3_000_000, 'fit_size': 400_000, 'bytes_per_point': 30_000}
            (Path(output) / 'plan.json').write_text(json.dumps(plan))
            raise map_refit.FitOutOfMemory('The fit was stopped by the kernel')
        manifest = await asyncio.to_thread(write_outputs, monkeypatch, source, output)
        return manifest['anon_before_fit'] + 5_000 * manifest['fit_size'] if manifest['anon_before_fit'] else None

    monkeypatch.setattr(map_refit, 'run_fit', fit)

    async def scenario(database):
        await seed(database, n=200)
        finished = await run_job(database, await submit(database), store)
        assert finished['state'] == 'succeeded', finished['error']
        raised = math.ceil(30_000 / map_refit.FIT_BUDGET_FRACTION)
        assert [size for _, size in attempts] == [None, raised]  # 75% of the killed attempt's fit
        assert attempts[0][0] != attempts[1][0]  # each attempt writes its own folder
        state = await map_state(database)
        if finished['result']['fit_peak_bytes'] is not None:  # measured in a cgroup v2 scope
            assert state['fit_bytes_per_point'] == 5_000
        # The next scheduled fit of this map is sized from what this one measured (or the raised value).
        attempts.clear()
        assert (await run_job(database, await submit(database), store))['state'] == 'succeeded'
        assert attempts[0][1] == state['fit_bytes_per_point'] and state['fit_bytes_per_point'] is not None

    registry_database(scenario)


# --- never publish during a backup, and no backup starts during a swap ---------------------------


def test_publish_waits_for_a_backup_and_a_backup_waits_for_a_publish(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)

    async def fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
        await asyncio.to_thread(write_outputs, monkeypatch, source, output)

    monkeypatch.setattr(map_refit, 'run_fit', fit)

    async def scenario(database):
        await seed(database, n=40)
        before = await map_points(database)
        job = await submit(database)
        await run_job(database, job, store, max_units=3)  # start, export, fit
        assert (
            await database.fetchval('SELECT checkpoint->>$2 FROM jobs WHERE id=$1', job['id'], 'phase')
        ) == 'publish'
        backup = await job_queue.enqueue_job(database.pool, 'backup', lane='maintenance')
        waiting = await run_job(database, job, store, max_units=1)
        assert waiting['state'] == 'queued' and waiting['error']['type'] == 'QueueContended'
        assert await map_points(database) == before
        await database.execute(
            "UPDATE jobs SET state='succeeded', finished_at=clock_timestamp() WHERE id=$1",
            backup['id'],
        )
        # While the swap runs, the backup scheduler holds tonight's backup back.
        await database.execute(
            """UPDATE jobs SET state='running', worker_id='w', lease_token=gen_random_uuid(),
               lease_expires_at=clock_timestamp()+interval '1 minute' WHERE id=$1""",
            job['id'],
        )
        await database.execute(
            'UPDATE backup_schedule SET interval_hours=1, timing_changed_at=now() - interval \'3 hours\'',
        )
        await database.execute("DELETE FROM jobs WHERE kind='backup'")
        assert await backups.schedule_backups(database.pool, str(tmp_path)) is None
        await database.execute(
            "UPDATE jobs SET state='queued', worker_id=NULL, lease_token=NULL, lease_expires_at=NULL WHERE id=$1",
            job['id'],
        )
        assert await backups.schedule_backups(database.pool, str(tmp_path)) is not None

    registry_database(scenario)


# --- start window and no head-of-line blocking ----------------------------------------------------


def test_a_due_run_is_queued_while_another_datasets_refit_is_stuck(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        await seed(database, n=30, dataset_id=2, start=1001)
        # Dataset 2's re-fit is queued but cannot start: a stuck started job holds its maintenance lane.
        stuck = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=2, lane='maintenance')
        await database.execute(
            "UPDATE jobs SET started_at=clock_timestamp(), available_at=clock_timestamp()+interval '1 day' WHERE id=$1",
            stuck['id'],
        )
        async with database.pool.acquire() as connection, connection.transaction():
            await map_refit.submit(connection, 2, FIELD)
        schedule = await add_schedule(database, min_new_samples=None, next_run_at=local(2026, 10, 20, 3))
        jobs = await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 20, 3, 1))
        assert [job['dataset_id'] for job in jobs] == [1]
        claimed = await job_queue.claim_job(database.pool, 'w', placements=['compute'], kinds=['map_refit'])
        assert claimed is not None and claimed['dataset_id'] == 1
        row = await database.fetchrow('SELECT last_check FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['last_check']['ran'] is True

    registry_database(scenario)


def test_a_slot_missed_by_more_than_its_window_is_skipped_and_said_so(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        schedule = await add_schedule(database, min_new_samples=None, next_run_at=local(2026, 10, 20, 3))
        # The runner comes back at 14:07, long after the 03:00-06:00 window.
        assert await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 20, 14, 7)) == []
        row = await database.fetchrow('SELECT * FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert row['last_check']['ran'] is False and row['last_check']['reason'].startswith('Missed Oct 20 03:00')
        assert row['next_run_at'] == local(2026, 10, 21, 3)
        assert await database.fetchval("SELECT count(*) FROM jobs WHERE kind='map_refit'") == 0

    registry_database(scenario)


def test_a_queued_run_that_could_not_start_inside_its_window_is_skipped(registry_database, tmp_path):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=30)
        schedule = dict(await add_schedule(database, min_new_samples=None))
        async with database.pool.acquire() as connection, connection.transaction():
            job, _ = await map_refit.submit(
                connection,
                1,
                FIELD,
                schedule=schedule,
                window_end=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        finished = await run_job(database, job, store)
        assert finished['state'] == 'succeeded' and 'Missed its start window' in finished['result']['skipped']
        assert await map_refit.read_state(database, 1, FIELD) is None  # projection was never paused
        assert not store.work_dir(job['id']).exists()

    registry_database(scenario)


def test_a_waiting_run_says_what_it_waits_for(registry_database):
    async def scenario(database):
        await seed(database, n=30)
        await seed(database, n=30, dataset_id=2, start=1001)
        async with database.pool.acquire() as connection, connection.transaction():
            other, _ = await map_refit.submit(connection, 2, FIELD)
        await job_queue.claim_job(database.pool, 'w', placements=['compute'], kinds=['map_refit'])
        schedule = await add_schedule(database, min_new_samples=None, next_run_at=local(2026, 10, 20, 3))
        await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 20, 3, 1))
        row = await database.fetchrow('SELECT last_check FROM dataset_job_schedules WHERE id=$1', schedule['id'])
        assert "it starts after dataset 2's (clip)" in row['last_check']['reason']
        assert 'skipped if that is after Oct 20 06:00' in row['last_check']['reason']
        assert other['dataset_id'] == 2

    registry_database(scenario)


# --- containment is verified, and checked before the export ---------------------------------


def test_containment_requires_the_cap_to_be_applied_inside_the_scope(monkeypatch):
    assert map_refit.containment(2 * 1024**3)[:3] == ['systemd-run', '--user', '--scope']  # systemd delegates
    for output in ('max\n0\n', f'{2 * 1024**3}\nmax\n', ''):
        monkeypatch.setattr(
            subprocess,
            'run',
            lambda *args, output=output, **kwargs: subprocess.CompletedProcess(args, 0, output, ''),
        )
        with pytest.raises(map_refit.ContainmentUnavailable, match='not applied'):
            map_refit.containment(2 * 1024**3)


def test_too_little_memory_skips_before_pausing_projection_or_exporting(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)
    monkeypatch.setattr(map_refit, 'fit_allowance', lambda: (256 * 1024**2, 128 * 1024**3))
    exported = []
    original_export = map_refit._export

    async def export(*args):
        exported.append(True)
        return await original_export(*args)

    monkeypatch.setattr(map_refit, '_export', export)

    async def scenario(database):
        await seed(database, n=60, dim=16)
        job = await submit(database)
        finished = await run_job(database, job, store)
        assert finished['state'] == 'succeeded' and finished['result']['skipped'].startswith('Not enough memory')
        assert exported == [] and not store.work_dir(job['id']).exists()
        assert await map_refit.read_state(database, 1, FIELD) is None

    registry_database(scenario)


# --- power users stop the system re-fit of their dataset -----------------------------------------


def test_power_users_can_cancel_map_refits_but_not_others_jobs():
    power = {'id': 7, 'role': 'power_user'}
    assert jobs._can_cancel({'kind': 'map_refit', 'created_by': None}, power)
    assert jobs._can_cancel({'kind': 'map_refit', 'created_by': 3}, power)
    assert not jobs._can_cancel({'kind': 'statistics', 'created_by': None}, power)
    assert not jobs._can_cancel({'kind': 'bulk_mutation', 'created_by': 3}, power)
    assert not jobs._can_cancel({'kind': 'map_refit', 'created_by': None}, {'id': 8, 'role': 'guest'})


# --- an identical re-send keeps the exported row version ---------------------------------------


def test_an_identical_vector_re_send_keeps_its_row_version(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    monkeypatch.setattr(embeddings, 'validate_embedding_dims', AsyncMock())

    async def scenario(database):
        monkeypatch.setattr(embeddings, 'db', database)
        await database.execute("INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES (1,1,'a.jpg','image')")
        vector = [0.5, 0.25, 0.125]
        upload = [embeddings.EmbeddingUpload(sample_id=1, field_name=FIELD, vector=vector)]
        await embeddings.upload_embeddings(1, upload, USER)
        version = await database.fetchval('SELECT xmin::text FROM embeddings WHERE sample_id=1')
        await embeddings.upload_embeddings(1, upload, USER)
        await datasets.add_samples(
            1,
            {'samples': [{'filepath': 'a.jpg', 'embeddings': {FIELD: vector}}], 'on_duplicate': 'update'},
            user=USER,
        )
        assert await database.fetchval('SELECT xmin::text FROM embeddings WHERE sample_id=1') == version
        changed = [embeddings.EmbeddingUpload(sample_id=1, field_name=FIELD, vector=[1.0, 2.0, 3.0])]
        await embeddings.upload_embeddings(1, changed, USER)
        assert await database.fetchval('SELECT xmin::text FROM embeddings WHERE sample_id=1') != version

    registry_database(scenario)


def test_an_identical_re_send_during_the_fit_keeps_the_point_on_the_new_map(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    monkeypatch.setattr(embeddings, 'validate_embedding_dims', AsyncMock())

    async def scenario(database):
        monkeypatch.setattr(embeddings, 'db', database)
        ids = await seed(database, n=50)
        resent = ids[7]
        vector = await database.fetchval('SELECT vector::text FROM embeddings WHERE sample_id=$1', resent)

        async def fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
            await asyncio.to_thread(write_outputs, monkeypatch, source, output)
            upload = [embeddings.EmbeddingUpload(sample_id=resent, field_name=FIELD, vector=json.loads(vector))]
            await embeddings.upload_embeddings(1, upload, USER)  # the sync re-sends an unchanged vector

        monkeypatch.setattr(map_refit, 'run_fit', fit)
        finished = await run_job(database, await submit(database), store)
        assert finished['result']['skipped_changed'] == 0
        assert resent in await map_points(database)

    registry_database(scenario)
