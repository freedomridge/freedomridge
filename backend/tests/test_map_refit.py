"""Scheduled map re-fit: export, memory-capped fit, atomic publish, projection fencing."""

import asyncio
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import numpy as np
import pytest
from fastapi import HTTPException
from fridge import map_fit
from fridge.core import dataset as sdk_dataset

from backend import coordinates, job_queue, map_placement, map_refit
from backend.map_artifacts import LocalArtifactStore, model_key
from backend.routers import embeddings
from backend.tests import test_field_registry_postgres
from backend.tests.test_job_runner import make_worker

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')
ROOT = Path(__file__).resolve().parents[2]
FIELD = 'clip'
USER = {'id': 1, 'role': 'admin'}
OLD_X = -100.0  # the old map lives at x < -50; the fake fit places points at x >= 1000


class FakeModel:
    """A picklable stand-in for a fitted reducer: deterministic, far from the old map."""

    def __init__(self, shift=1000.0):
        self.shift = shift
        self.n_features_in_ = None

    def transform(self, vectors):
        vectors = np.asarray(vectors, dtype=np.float64)
        return np.stack([self.shift + vectors[:, 0], vectors[:, 1]], axis=1)


def fake_reducer(vectors, fit_sample_size, method='umap', timings=None):
    model = FakeModel()
    model.n_features_in_ = vectors.shape[1]
    return model, model.transform(vectors).astype(np.float32)


@pytest.fixture
def fake_fit(monkeypatch):
    """Replace only the child process: the real export file is read and real outputs are written."""
    calls = []

    async def run_fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
        calls.append(allowance)
        monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
        await asyncio.to_thread(map_fit.main, ['--input', str(source), '--output', str(output)])

    monkeypatch.setattr(map_refit, 'run_fit', run_fit)
    return calls


async def seed(db, n=200, dim=8, dataset_id=1, start=1, field=FIELD):
    """n samples with random vectors and an old map (x < -50), fully ranked."""
    rng = np.random.default_rng(dataset_id * 1000 + start)
    ids = list(range(start, start + n))
    await db.execute(
        "INSERT INTO samples(id,dataset_id,filepath,media_type) "
        "SELECT i,$1,'image-' || i,'image' FROM unnest($2::int[]) i",
        dataset_id,
        ids,
    )
    vectors = rng.normal(size=(n, dim))
    await db.execute(
        "INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) "
        "SELECT (SELECT dataset_id FROM samples WHERE id = i),i,$3,v::vector FROM unnest($1::int[],$2::text[]) u(i,v)",
        ids,
        [str(list(map(float, row))) for row in vectors],
        field,
    )
    await db.execute(
        "INSERT INTO embedding_coordinates(sample_id,field_name,x,y) SELECT i,$2,$3 - (i % 40),i % 13 "
        "FROM unnest($1::int[]) i",
        ids,
        field,
        OLD_X,
    )
    await db.execute('SELECT recompute_viz_rank($1,$2,TRUE)', dataset_id, field)
    return ids


async def map_state(db, dataset_id=1, field=FIELD) -> dict:
    state = await map_refit.read_state(db, dataset_id, field)
    assert state is not None
    return state


async def run_job(database, job, store, *, max_units=50, **worker_options):
    worker = await make_worker(
        database,
        {map_refit.KIND: map_refit.handler(database.pool, store)},
        policies={map_refit.KIND: map_refit.POLICY},
        placements=('server', 'compute'),
        step_budget_seconds=0.001,
        **worker_options,
    )
    for _ in range(max_units):
        state = await database.fetchrow(
            'SELECT state, available_at > clock_timestamp() AS later FROM jobs WHERE id=$1',
            job['id'],
        )
        if state['state'] in ('succeeded', 'failed', 'cancelled'):
            break
        if state['later']:
            await database.execute('UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1', job['id'])
        await worker.run_once()
    return dict(await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id']))


async def submit(database, dataset_id=1, field=FIELD, **options):
    async with database.pool.acquire() as connection, connection.transaction():
        job, _ = await map_refit.submit(connection, dataset_id, field, **options)
    return job


async def map_points(db, dataset_id=1, field=FIELD):
    return {
        row['sample_id']: (row['x'], row['y'])
        for row in await db.fetch(
            'SELECT c.sample_id,c.x,c.y FROM embedding_coordinates c JOIN samples s ON s.id=c.sample_id '
            'WHERE s.dataset_id=$1 AND c.field_name=$2',
            dataset_id,
            field,
        )
    }


async def assert_ranks_exact(db, dataset_id=1, field=FIELD):
    ranked = await db.fetchval(
        'SELECT count(*) FROM embedding_viz_rank WHERE dataset_id=$1 AND field_name=$2',
        dataset_id,
        field,
    )
    placed = len(await map_points(db, dataset_id, field))
    meta = await db.fetchrow(
        'SELECT total_points, rank_stale FROM embedding_viz_meta WHERE dataset_id=$1 AND field_name=$2',
        dataset_id,
        field,
    )
    assert ranked == placed == meta['total_points']
    mismatched = await db.fetchval(
        """SELECT count(*) FROM embedding_viz_rank r JOIN embedding_coordinates c
           ON c.sample_id=r.sample_id AND c.field_name=r.field_name
           WHERE r.dataset_id=$1 AND r.field_name=$2 AND (r.x,r.y) IS DISTINCT FROM (c.x::real,c.y::real)""",
        dataset_id,
        field,
    )
    assert mismatched == 0
    return meta


def test_real_fit_in_memory_capped_child_publishes_a_complete_new_map(registry_database, tmp_path):
    """The real child (systemd-run scope, umap-learn) end to end on a small field."""
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        ids = await seed(database, n=600, dim=16)
        job = await submit(database)
        finished = await run_job(database, job, store)
        assert finished['state'] == 'succeeded', finished['error']
        points = await map_points(database)
        assert set(points) == set(ids)
        assert all(np.isfinite(x) and x > -50 for x, _ in points.values())  # every point moved to the new map
        meta = await assert_ranks_exact(database)
        assert meta['rank_stale'] is False
        state = await map_state(database)
        assert str(state['model_id']) == finished['result']['model_id']
        assert state['points_at_refit'] == 600 and state['active_job_id'] is None and not state['active']
        assert state['artifact_location'] is None and list(store.models()) == []  # no model is kept
        assert finished['result']['fit_size'] == 600
        assert not store.work_dir(job['id']).exists()

    registry_database(scenario)


async def add_sample(db, sample_id, vector, dataset_id=1, field=FIELD):
    await db.execute(
        "INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES ($1,$2,'image-' || $1::int,'image')",
        sample_id,
        dataset_id,
    )
    await db.execute(
        'INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) VALUES ('
        '(SELECT dataset_id FROM samples WHERE id = $1),$1,$2,$3::vector)',
        sample_id,
        field,
        str(vector),
    )


async def pending(db, field=FIELD):
    return {
        row['sample_id']
        for row in await db.fetch(
            'SELECT sample_id FROM embedding_projection_pending WHERE field_name=$1 ORDER BY 1',
            field,
        )
    }


def test_changes_during_the_fit_are_skipped_and_queued_for_the_new_map(registry_database, tmp_path, monkeypatch):
    """Vector changed, sample deleted, new sample, identical re-upload: none keeps a coordinate from
    either the old map or the stale fit, and all are placed on the new map afterwards."""
    store = LocalArtifactStore(tmp_path)
    monkeypatch.setattr(map_placement, 'require_free_space', AsyncMock())

    async def scenario(database):
        ids = await seed(database, n=200, dim=8)
        changed, deleted, retried, new = ids[10], ids[20], ids[30], 5000

        async def run_fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
            monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
            await asyncio.to_thread(map_fit.main, ['--input', str(source), '--output', str(output)])
            # Ingest continues while the fit runs (all after the export snapshot).
            await database.execute(
                "UPDATE embeddings SET vector='[9,9,9,9,9,9,9,9]' WHERE sample_id=$1 AND field_name=$2",
                changed,
                FIELD,
            )
            await database.execute('DELETE FROM samples WHERE id=$1', deleted)
            await database.execute(
                'UPDATE embeddings SET vector=vector WHERE sample_id=$1 AND field_name=$2',
                retried,
                FIELD,
            )
            await add_sample(database, new, [1, 2, 3, 4, 5, 6, 7, 8])

        monkeypatch.setattr(map_refit, 'run_fit', run_fit)
        job = await submit(database)
        finished = await run_job(database, job, store)
        assert finished['state'] == 'succeeded', finished['error']
        result = finished['result']
        assert result['exported'] == 200
        assert result['skipped_changed'] == 3  # changed, deleted, retried
        assert result['removed_coordinates'] == 1  # the retried sample's old-map coordinate
        points = await map_points(database)
        assert set(points) == set(ids) - {changed, deleted, retried}
        assert all(x > 900 for x, _ in points.values())  # nothing from the old map survives
        assert await pending(database) == {changed, retried, new}
        await assert_ranks_exact(database)
        state = await map_state(database)
        assert state['points_at_refit'] == 197

        # The job runner places them on the new map, by neighbours.
        [placement] = await map_placement.schedule_placements(database.pool)
        worker = await make_worker(
            database,
            {map_placement.KIND: map_placement.handler(database.pool)},
            policies={map_placement.KIND: map_placement.POLICY},
            placements=('server',),
        )
        await worker.run_once()
        assert (await database.fetchval('SELECT state FROM jobs WHERE id=$1', placement['id'])) == 'succeeded'
        assert await pending(database) == set()
        points = await map_points(database)
        assert all(x > 900 for x, _ in points.values())  # on the new map, among its points
        meta = await assert_ranks_exact(database)
        assert meta['total_points'] == 200  # 199 seeded samples remain, plus the new one

    registry_database(scenario)


def test_placement_pauses_during_a_refit_and_resumes_after_cancel(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)
    monkeypatch.setattr(map_placement, 'require_free_space', AsyncMock())
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    started = asyncio.Event()

    async def scenario(database):
        monkeypatch.setattr(embeddings, 'db', database)
        await seed(database, n=40)
        await add_sample(database, 900, [1, 1, 1, 1, 1, 1, 1, 1])

        async def run_fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
            started.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(map_refit, 'run_fit', run_fit)
        job = await submit(database)
        worker = await make_worker(
            database,
            {map_refit.KIND: map_refit.handler(database.pool, store)},
            policies={map_refit.KIND: map_refit.POLICY},
            placements=('server', 'compute'),
            step_budget_seconds=3600,
            lease_seconds=4,
            heartbeat_seconds=0.2,
        )
        running = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), 30)
        assert (await map_state(database))['active']
        # Placement and the API hold back.
        assert await map_placement.schedule_placements(database.pool) == []
        assert await pending(database) == {900}
        update = [embeddings.CoordinateUpdate(sample_id=900, x=1, y=1, vector_version='1')]
        for kwargs in ({}, {'model_id': str(uuid.uuid4())}):
            with pytest.raises(HTTPException) as refused:
                await embeddings.upload_coordinates(1, FIELD, update, USER, **kwargs)
            assert refused.value.status_code == 409 and 'in progress' in refused.value.detail
        with pytest.raises(HTTPException):  # a manual layout upload waits too
            await embeddings.upload_coordinates(1, FIELD, [embeddings.CoordinateUpdate(sample_id=1, x=1, y=1)], USER)

        await job_queue.cancel_job(database.pool, job['id'])
        await asyncio.wait_for(running, 30)
        assert (await database.fetchrow('SELECT state FROM jobs WHERE id=$1', job['id']))['state'] == 'cancelled'
        assert not (await map_state(database))['active']
        points = await map_points(database)
        assert len(points) == 40 and all(x < -50 for x, _ in points.values())  # old map untouched
        # Placement resumes, on the map that was never re-fitted.
        [placement] = await map_placement.schedule_placements(database.pool)
        placer = await make_worker(
            database,
            {map_placement.KIND: map_placement.handler(database.pool)},
            policies={map_placement.KIND: map_placement.POLICY},
            placements=('server',),
        )
        await placer.run_once()
        assert (await database.fetchval('SELECT state FROM jobs WHERE id=$1', placement['id'])) == 'succeeded'
        assert await pending(database) == set()
        assert (await map_points(database))[900][0] < -50  # on the old map, among its points
        await assert_ranks_exact(database)

    registry_database(scenario)


def test_cancel_stops_the_real_fit_process(registry_database, tmp_path, monkeypatch):
    """The job's own stop ends its child; nothing else is touched and the old map stays."""
    store = LocalArtifactStore(tmp_path)
    marker = f'sleep {300 + uuid.uuid4().int % 1000}.{os.getpid() % 97}'

    def alive():
        found = []
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                try:
                    if (entry / 'cmdline').read_bytes().replace(b'\0', b' ').strip().decode() == marker:
                        found.append(int(entry.name))
                except OSError:
                    pass
        return found

    async def scenario(database):
        await seed(database, n=40)
        real_containment = map_refit.containment(map_refit.fit_allowance()[0])
        assert real_containment is not None, 'systemd-run --user --scope must work on this machine'
        monkeypatch.setattr(
            map_refit,
            'containment',
            lambda allowance: [*real_containment, 'bash', '-c', f'exec {marker}', '--'],
        )
        job = await submit(database)
        worker = await make_worker(
            database,
            {map_refit.KIND: map_refit.handler(database.pool, store)},
            policies={map_refit.KIND: map_refit.POLICY},
            placements=('server', 'compute'),
            step_budget_seconds=3600,
            lease_seconds=4,
            heartbeat_seconds=0.2,
        )
        running = asyncio.create_task(worker.run_once())
        for _ in range(300):
            if alive():
                break
            await asyncio.sleep(0.1)
        assert alive(), 'the fit process did not start'
        await job_queue.cancel_job(database.pool, job['id'])
        await asyncio.wait_for(running, 30)
        assert alive() == []
        assert (await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id'])) == 'cancelled'
        assert all(x < -50 for x, _ in (await map_points(database)).values())
        assert (await map_state(database))['model_id'] is None

    registry_database(scenario)


def test_oom_kill_ends_only_the_child_and_the_runner_keeps_working(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)

    def oom_kills():
        path = Path('/proc/self/cgroup').read_text().split('::', 1)[1].strip()
        events = dict(line.split() for line in (Path('/sys/fs/cgroup') / path.lstrip('/') / 'memory.events').open())
        return int(events.get('oom_kill', 0))

    async def scenario(database):
        await seed(database, n=600, dim=16)
        await seed(database, n=40, dataset_id=2, start=5001)
        before = oom_kills()
        total = map_refit.host_memory()[0]
        monkeypatch.setattr(map_refit, 'fit_allowance', lambda: (150 * 1024**2, total))  # far below an import
        monkeypatch.setattr(map_refit, 'minimum_fit_bytes', lambda rows, dim, bytes_per_point=None: 1)
        job = await submit(database)
        failed = await run_job(database, job, store)
        assert failed['state'] == 'failed'
        assert failed['error']['type'] == 'FitOutOfMemory'
        assert 'stopped by the kernel' in failed['error']['message'] and '0.1 GB' in failed['error']['message']
        assert oom_kills() == before  # this process's cgroup saw no OOM kill
        assert all(x < -50 for x, _ in (await map_points(database)).values())
        assert not (await map_state(database))['active']
        # The runner goes on: the next fit (another dataset, enough memory) succeeds.
        monkeypatch.undo()
        monkeypatch.setattr(map_refit, 'run_fit', fake_fit_runner(monkeypatch))
        other = await submit(database, dataset_id=2)
        assert (await run_job(database, other, store))['state'] == 'succeeded'

    registry_database(scenario)


def fake_fit_runner(monkeypatch):
    async def run_fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
        monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
        await asyncio.to_thread(map_fit.main, ['--input', str(source), '--output', str(output)])

    return run_fit


@pytest.mark.parametrize('reason', ['memory', 'containment'])
def test_insufficient_memory_or_no_containment_skips_with_the_reason(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,
    reason,
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=60)
        schedule_id = await database.fetchval(
            "INSERT INTO dataset_job_schedules (dataset_id, job_type, target) VALUES (1,'map_refit',$1) RETURNING id",
            FIELD,
        )
        if reason == 'memory':
            monkeypatch.setattr(map_refit, 'fit_allowance', lambda: (512 * 1024**2, 128 * 1024**3))
        else:

            def unavailable(allowance):
                raise map_refit.ContainmentUnavailable('systemd-run is not installed')

            monkeypatch.setattr(map_refit, 'containment', unavailable)
        async with database.pool.acquire() as connection, connection.transaction():
            job, _ = await map_refit.submit(connection, 1, FIELD, schedule={'id': schedule_id})
        await database.execute('UPDATE dataset_job_schedules SET last_job_id=$1 WHERE id=$2', job['id'], schedule_id)
        finished = await run_job(database, job, store)
        assert finished['state'] == 'succeeded'
        skipped = finished['result']['skipped']
        if reason == 'memory':
            assert skipped.startswith('Not enough memory to fit this map: it needs at least')
            assert 'while keeping 20% of 128.0 GB RAM free' in skipped
        else:
            assert 'systemd-run' in skipped
        assert finished['message'] == f'Skipped: {skipped}'
        assert fake_fit == []  # no fit ran
        # Skipped in the first step: projection was never paused and nothing was exported.
        assert await map_refit.read_state(database, 1, FIELD) is None
        assert all(x < -50 for x, _ in (await map_points(database)).values())
        assert (await database.fetchval('SELECT last_result FROM dataset_job_schedules WHERE id=$1', schedule_id)) == {
            'skipped': skipped,
        }

    registry_database(scenario)


@pytest.mark.parametrize('failure', ['error', 'cancel', 'lease'])
def test_failure_at_the_final_fence_rolls_back_the_whole_swap(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,
    failure,
):
    """A first managed map, then a second re-fit that fails at the last moment: coordinates, ranks,
    the model pointer and the job all stay as they were, and projection keeps the first model."""
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=120)
        first = await run_job(database, await submit(database), store)
        assert first['state'] == 'succeeded'
        before_points = await map_points(database)
        before_ranks = await database.fetch('SELECT sample_id, rank, x, y FROM embedding_viz_rank ORDER BY sample_id')
        before_state = await map_state(database)
        original = job_queue.checkpoint_locked  # the swap's final fence (the re-rank step follows it)

        async def failing_fence(connection, job_id, lease_token, **kwargs):
            if failure == 'error':
                raise RuntimeError('injected failure after the swap')
            if failure == 'cancel':
                await database.execute('UPDATE job_controls SET cancel_requested=TRUE WHERE job_id=$1', job_id)
            else:
                await database.execute(
                    "UPDATE jobs SET lease_expires_at=clock_timestamp() - interval '1 second' WHERE id=$1",
                    job_id,
                )
            return await original(connection, job_id, lease_token, **kwargs)

        monkeypatch.setattr(job_queue, 'checkpoint_locked', failing_fence)
        # Four units: start, export, fit, publish.
        second = await run_job(database, await submit(database), store, max_units=4)
        if failure == 'lease':
            assert second['state'] in ('running', 'queued')  # an expired owner is retried, never published
        else:
            assert second['state'] == {'error': 'failed', 'cancel': 'cancelled'}[failure]
        if failure == 'error':
            assert 'injected failure' in second['error']['message']
        assert await map_points(database) == before_points
        assert (
            await database.fetch('SELECT sample_id, rank, x, y FROM embedding_viz_rank ORDER BY sample_id')
            == before_ranks
        )
        state = await map_state(database)
        assert state['model_id'] == before_state['model_id']
        assert state['refitted_at'] == before_state['refitted_at']
        await assert_ranks_exact(database)
        if failure != 'lease':
            assert not state['active']
            async with database.pool.acquire() as connection, connection.transaction():
                await coordinates.check_map_fence(connection, 1, FIELD, before_state['model_id'])

    registry_database(scenario)


def test_publish_is_atomic_for_concurrent_readers(registry_database, tmp_path, monkeypatch, fake_fit):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        monkeypatch.setattr(embeddings, 'db', database)
        await seed(database, n=20000, dim=4)
        job = await submit(database)
        done = asyncio.Event()
        seen = {'old': 0, 'new': 0}

        async def read():
            request = embeddings.EmbeddingsRequest(field=FIELD, sample_pct=100)
            while not done.is_set():
                response = await embeddings.get_embeddings_post(1, request)
                xs = [point['x'] for point in response['points']]
                assert len(xs) == response['total'] == 20000
                kind = 'old' if max(xs) < -50 else 'new' if min(xs) > 900 else 'mixed'
                assert kind != 'mixed'
                assert (response['map_state'] is None) == (kind == 'old')
                seen[kind] += 1
                async with database.pool.acquire() as connection:
                    ranks = await connection.fetchrow(
                        'SELECT count(*) AS n, min(x) AS low, max(x) AS high '
                        'FROM embedding_viz_rank WHERE dataset_id=1',
                    )
                # Old ranks, or none between the swap and the re-rank (the scatter then reads the new
                # coordinates), or the new map's ranks: never a mix.
                assert ranks['n'] == 0 or (ranks['n'] == 20000 and (ranks['high'] < -50 or ranks['low'] > 900))

        async def refit():
            try:
                return await run_job(database, job, store)
            finally:
                done.set()

        readers = [asyncio.create_task(read()) for _ in range(3)]
        finished = await refit()
        await asyncio.gather(*readers)
        assert finished['state'] == 'succeeded'
        assert seen['old'] > 0 and seen['new'] >= 0
        response = await embeddings.get_embeddings_post(1, embeddings.EmbeddingsRequest(field=FIELD, sample_pct=100))
        assert response['map_state']['model_id'] == finished['result']['model_id']

    registry_database(scenario)


def test_scatter_reports_the_map_in_json_and_binary_header(registry_database, tmp_path, monkeypatch, fake_fit):
    from backend.tests.test_embeddings_binary import _parse

    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        monkeypatch.setattr(embeddings, 'db', database)
        await seed(database, n=30)
        request = embeddings.EmbeddingsRequest(field=FIELD, sample_pct=100)
        assert (await embeddings.get_embeddings_post(1, request))['map_state'] is None
        finished = await run_job(database, await submit(database), store)
        state = await map_state(database)
        expected = {'model_id': finished['result']['model_id'], 'refitted_at': state['refitted_at'].isoformat()}
        assert (await embeddings.get_embeddings_post(1, request))['map_state'] == expected
        header, ids, xs, ys = _parse(bytes((await embeddings.get_embeddings_post(1, request, format='bin')).body))
        assert header['map_state'] == expected and len(ids) == 30
        public = await embeddings.get_map_state(1, FIELD, USER)
        assert public['managed'] and public['refitted_at'] == expected['refitted_at']
        assert public['points_at_refit'] == public['current_points'] == 30 and not public['active']

    registry_database(scenario)


def test_backup_holds_the_refit_without_spending_its_failure_budget(registry_database, tmp_path, fake_fit):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=30)
        backup = await job_queue.enqueue_job(database.pool, 'backup', lane='maintenance')
        job = await submit(database)
        waiting = await run_job(database, job, store, max_units=1)
        assert waiting['state'] == 'queued' and waiting['started_at'] is not None
        assert waiting['error']['type'] == 'QueueContended' and 'backup' in waiting['error']['message']
        assert waiting['failures'] == 0 and waiting['contentions'] == 1
        assert not (await map_refit.read_state(database, 1, FIELD) or {}).get('active')  # nothing paused yet
        await database.execute(
            "UPDATE jobs SET state='succeeded', finished_at=clock_timestamp() WHERE id=$1",
            backup['id'],
        )
        assert (await run_job(database, job, store))['state'] == 'succeeded'

    registry_database(scenario)


def test_one_fit_at_a_time_even_across_yields_and_concurrent_claims(registry_database):
    async def scenario(database):
        await seed(database, n=10)
        await seed(database, n=10, dataset_id=2, start=101)
        first = await submit(database)
        second = await submit(database, dataset_id=2)
        other = await job_queue.enqueue_job(database.pool, 'example', dataset_id=2, placement='compute')
        # Two claimers race for the two unstarted fits: exactly one gets one.
        claims = await asyncio.gather(
            *[
                job_queue.claim_job(database.pool, f'w{index}', placements=['compute'], kinds=['map_refit'])
                for index in range(2)
            ],
        )
        claimed = [claim for claim in claims if claim is not None]
        assert len(claimed) == 1
        owner = claimed[0]
        # The owner yields between steps: still nothing else may start a fit, other work may run.
        await job_queue.publish_step(
            database.pool,
            owner['id'],
            owner['lease_token'],
            checkpoint={'phase': 'export'},
            release=True,
        )
        assert await job_queue.claim_job(database.pool, 'w2', placements=['compute'], kinds=['map_refit']) is not None
        resumed = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', owner['id'])
        assert resumed['state'] == 'running'  # the started owner resumes
        unrelated = await job_queue.claim_job(database.pool, 'w3', placements=['compute'], kinds=['example'])
        assert unrelated is not None and unrelated['id'] == other['id']
        assert await job_queue.claim_job(database.pool, 'w4', placements=['compute'], kinds=['map_refit']) is None
        await job_queue.complete_job(database.pool, owner['id'], resumed['lease_token'])
        follower = await job_queue.claim_job(database.pool, 'w5', placements=['compute'], kinds=['map_refit'])
        assert follower is not None and follower['id'] == ({first['id'], second['id']} - {owner['id']}).pop()

    registry_database(scenario)


def test_sweep_keeps_current_and_unfinished_artifacts_only(registry_database, tmp_path, fake_fit):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=30)
        finished = await run_job(database, await submit(database), store)
        # A model kept from before re-fits stopped keeping one, still the map's current one.
        current = model_key(1, FIELD, (await map_state(database))['model_id'])
        store.path(current).parent.mkdir(parents=True)
        store.path(current).write_bytes(b'current')
        await database.execute(
            'UPDATE embedding_map_state SET artifact_location=$2 WHERE dataset_id=1 AND field_name=$1',
            FIELD,
            current,
        )
        unfinished = await submit(database)
        await database.execute(
            "UPDATE jobs SET checkpoint=jsonb_build_object('model_id',$2::text) WHERE id=$1",
            unfinished['id'],
            str(uuid.uuid4()),
        )
        model_id = await database.fetchval("SELECT checkpoint->>'model_id' FROM jobs WHERE id=$1", unfinished['id'])
        keep_model = store.root / 'managed' / f'1__{FIELD}' / f'{model_id}.joblib'
        keep_model.write_bytes(b'in progress')
        stale_model = store.root / 'managed' / f'1__{FIELD}' / f'{uuid.uuid4()}.joblib'
        stale_model.write_bytes(b'replaced')
        orphan = store.root / 'managed' / f'99__{FIELD}' / f'{uuid.uuid4()}.joblib'
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b'deleted dataset')
        store.work_dir(unfinished['id']).mkdir(parents=True)
        (store.work_dir(unfinished['id']) / 'vectors.bin').write_bytes(b'exporting')
        finished_work = store.work_dir(finished['id']) / 'fit'
        finished_work.mkdir(parents=True)
        (finished_work / 'coords.npy').write_bytes(b'left over')
        assert await map_refit.sweep(database.pool, store) == 3
        assert store.path(current).exists() and keep_model.exists()
        assert not stale_model.exists() and not orphan.exists() and not orphan.parent.exists()
        assert store.work_dir(unfinished['id']).exists() and not store.work_dir(finished['id']).exists()

    registry_database(scenario)


def test_the_fit_size_formula_sees_the_memory_cap_not_the_host():
    """Inside its scope the child sizes the fit from what the cap leaves (same formula, no fixed cap)."""
    import subprocess
    import sys

    prefix = map_refit.containment(2 * 1024**3)
    assert prefix is not None, 'systemd-run --user --scope must work on this machine'
    probe = (
        'from fridge.core.dataset import _auto_fit_sample_size, _available_ram_bytes; '
        'print(_available_ram_bytes(), _auto_fit_sample_size(10_000_000, 512))'
    )
    output = subprocess.run(
        [*prefix, sys.executable, '-c', probe],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, 'PYTHONPATH': str(map_refit.SDK_ROOT)},
        check=True,
    ).stdout.split()
    available, fit_size = int(output[-2]), int(output[-1])
    assert 0 < available <= 2 * 1024**3
    # The formula (75% of what is available, FIT_VECTOR_COPIES vectors per point), from the cap.
    assert fit_size == pytest.approx(int(available * 0.75) // (512 * 4 * map_refit.FIT_VECTOR_COPIES), rel=0.02)
    total, host_available = map_refit.host_memory()
    assert host_available > 4 * 1024**3  # the host would have allowed far more
