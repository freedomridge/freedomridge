"""A map re-fit with its fit on a compute worker: the server's runner (server work only) exports the vectors, the
worker downloads them over the API, fits, uploads its outputs under its lease and completes, and the server swaps
the new map in. Here the test plays the worker over HTTP and runs the real fridge.map_fit with a fake reducer."""

import asyncio
import os
import secrets

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from fridge import map_fit
from fridge.core import dataset as sdk_dataset

from backend import map_refit
from backend.crypto import hash_token
from backend.map_artifacts import LocalArtifactStore, sha256
from backend.routers import auth, workers
from backend.tests import test_field_registry_postgres
from backend.tests.test_job_runner import make_worker
from backend.tests.test_map_refit import fake_reducer, map_points, map_state, seed, submit

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The model store both the API (FRIDGE_UMAP_MODEL_DIR) and the server's runner use, and the fake reducer."""
    store = LocalArtifactStore(tmp_path / 'models')
    monkeypatch.setenv('FRIDGE_UMAP_MODEL_DIR', str(store.root))
    monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
    return store


async def setup(database, monkeypatch) -> dict:
    for module in (auth, workers):
        monkeypatch.setattr(module, 'db', database)
    await database.execute("INSERT INTO users(id, username, role) VALUES (10, 'owner', 'power_user')")
    raw = secrets.token_urlsafe(32)
    await database.execute(
        "INSERT INTO api_tokens (user_id, token, name, scope) VALUES (10, $1, 'worker', 'worker')",
        hash_token(raw),
    )
    return {'Authorization': f'Bearer {raw}'}


def client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(workers.router, prefix='/api')
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')


async def server_runner(database, store, fits_here=False):
    placements = ('server', 'compute') if fits_here else ('server',)
    return await make_worker(
        database,
        {map_refit.KIND: map_refit.handler(database.pool, store, fits_here=fits_here)},
        policies={map_refit.KIND: map_refit.POLICY},
        placements=placements,
    )


async def job_row(database, job):
    return await database.fetchrow('SELECT * FROM jobs WHERE id = $1', job['id'])


async def hand_to_server(database, job):
    """What a worker's claim of a re-fit before its fit does (the first test does it over HTTP): queued as compute
    work, the job waits for something that takes compute work, and the claim hands its start to the server."""
    await database.execute("UPDATE jobs SET placement = 'server' WHERE id = $1", job['id'])


class Worker:
    """The test as a compute worker, over the API."""

    def __init__(self, api, headers):
        self.api, self.headers = api, headers

    async def register(self):
        registered = await self.api.post(
            '/api/workers/register',
            json={'name': 'desk', 'kinds': [map_refit.KIND]},
            headers=self.headers,
        )
        self.id = registered.json()['worker_id']

    async def claim(self):
        return await self.api.post(f'/api/workers/{self.id}/claim', json={}, headers=self.headers)

    def files(self, job, name):
        return f"/api/workers/{self.id}/jobs/{job['id']}/files/{name}"

    def leased(self, job, **extra):
        return {**self.headers, 'X-Fridge-Lease': job['lease_token'], **extra}

    async def fit(self, job, folder):
        """Download the vectors in two Range requests, then fit them with the real fridge.map_fit."""
        first = await self.api.get(self.files(job, 'vectors.bin'), headers=self.leased(job, Range='bytes=0-99'))
        rest = await self.api.get(self.files(job, 'vectors.bin'), headers=self.leased(job, Range='bytes=100-'))
        assert (first.status_code, rest.status_code, len(first.content)) == (206, 206, 100)
        folder.mkdir()
        (folder / 'vectors.bin').write_bytes(first.content + rest.content)
        output = folder / 'fit'
        await asyncio.to_thread(map_fit.main, ['--input', str(folder / 'vectors.bin'), '--output', str(output)])
        return output

    async def upload(self, job, output, names=map_refit.REMOTE_OUTPUTS):
        for name in names:
            data = (output / name).read_bytes()
            half = len(data) // 2
            for offset, chunk in ((0, data[:half]), (half, data[half:])):
                sent = await self.api.put(
                    f'{self.files(job, name)}?offset={offset}',
                    content=chunk,
                    headers=self.leased(job),
                )
                assert sent.status_code == 200 and sent.json() == {'size': offset + len(chunk)}, sent.text

    async def complete(self, job, output, wrong=None):
        """Complete with every output's sha256 (one of them `wrong`, to test the check)."""
        checksums = {name: sha256(output / name) for name in map_refit.REMOTE_OUTPUTS}
        if wrong:
            checksums[wrong] = '0' * 64
        return await self.api.post(
            f"/api/workers/{self.id}/jobs/{job['id']}/complete",
            json={'lease_token': job['lease_token'], 'result': {'checksums': checksums}},
            headers=self.headers,
        )


def test_a_compute_worker_fits_the_map_and_the_server_swaps_it_in(registry_database, store, tmp_path, monkeypatch):
    async def scenario(database):
        headers = await setup(database, monkeypatch)
        await seed(database, n=120)
        old = await map_points(database)
        job = await submit(database)
        server = await server_runner(database, store)
        async with client() as api:
            worker = Worker(api, headers)
            await worker.register()
            # Claimed at its start, the re-fit goes straight back to the server: start and export are its work.
            assert (await worker.claim()).status_code == 204
            row = await job_row(database, job)
            assert (row['state'], row['placement']) == ('queued', 'server')

            # The server starts and exports it, then hands the fit to compute work, where it waits.
            await server.run_once()
            row = await job_row(database, job)
            assert (row['placement'], row['checkpoint']['phase']) == ('compute', 'fit')
            assert row['message'].startswith('Waiting for a compute worker to fit the map on 120 vectors')

            claimed = (await worker.claim()).json()
            assert claimed['checkpoint']['phase'] == 'fit'
            output = await worker.fit(claimed, tmp_path / 'worker')
            assert (tmp_path / 'worker' / 'vectors.bin').read_bytes() == (
                store.work_dir(job['id']) / 'vectors.bin'
            ).read_bytes()
            await worker.upload(claimed, output)
            # Sent again from the start, a chunk says where to resume instead.
            again = await api.put(
                f"{worker.files(claimed, 'coords.npy')}?offset=0",
                content=b'x',
                headers=worker.leased(claimed),
            )
            assert again.status_code == 409
            assert again.json()['detail']['size'] == (output / 'coords.npy').stat().st_size
            # No model crosses: it is a pickle, which runs code when loaded.
            model = await api.put(
                f"{worker.files(claimed, 'model.joblib')}?offset=0",
                content=b'x',
                headers=worker.leased(claimed),
            )
            assert model.status_code == 404
            done = await worker.complete(claimed, output)
            assert done.json() == {'state': 'queued'}
            row = await job_row(database, job)
            assert (row['placement'], row['checkpoint']['phase'], row['checkpoint']['remote']) == (
                'server',
                'publish',
                True,
            )

        await server.run_once()  # the swap and the ranks
        row = await job_row(database, job)
        assert row['state'] == 'succeeded', row['error']
        new = await map_points(database)
        assert set(new) == set(old) and new != old
        state = await map_state(database)
        assert state['model_id'] is not None and state['artifact_location'] is None  # a new map, with no model
        assert list(store.models()) == []
        assert not store.work_dir(job['id']).exists()

    registry_database(scenario)


def test_uploads_belong_to_their_lease_and_are_checked_before_the_server_takes_them(
    registry_database,
    store,
    tmp_path,
    monkeypatch,
):
    async def scenario(database):
        headers = await setup(database, monkeypatch)
        await seed(database, n=120)
        old = await map_points(database)
        job = await submit(database)
        server = await server_runner(database, store)
        async with client() as api:
            worker = Worker(api, headers)
            await worker.register()
            await worker.claim()  # handed back to the server
            await server.run_once()
            first = (await worker.claim()).json()
            output = await worker.fit(first, tmp_path / 'worker')
            await worker.upload(first, output, names=map_refit.REMOTE_OUTPUTS[:-1])  # no manifest
            incomplete = await worker.complete(first, output)
            assert incomplete.status_code == 400 and 'manifest.json' in incomplete.json()['detail']

            # The lease ends (a laptop asleep): the next attempt gets a new lease and its own staging folder.
            await database.execute(
                "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = $1",
                job['id'],
            )
            assert (await worker.claim()).status_code == 204  # requeued, after the retry delay
            await database.execute('UPDATE jobs SET available_at = clock_timestamp() WHERE id = $1', job['id'])
            second = (await worker.claim()).json()
            assert second['lease_token'] != first['lease_token']
            stale = await api.put(
                f"{worker.files(first, 'manifest.json')}?offset=0",
                content=b'{}',
                headers=worker.leased(first),
            )
            assert stale.status_code == 409  # the old lease can't add to anything
            stale_download = await api.get(worker.files(first, 'vectors.bin'), headers=worker.leased(first))
            assert stale_download.status_code == 409
            assert (await worker.complete(second, output)).status_code == 400  # nothing uploaded under this lease

            await worker.upload(second, output)
            wrong = await worker.complete(second, output, wrong='coords.npy')
            assert wrong.status_code == 400 and "coords.npy doesn't match" in wrong.json()['detail']
            row = await job_row(database, job)
            assert (row['state'], row['placement']) == ('running', 'compute')  # still the worker's to finish
            assert (await worker.complete(second, output)).json() == {'state': 'queued'}
        assert await map_points(database) == old  # the swap is still the server's to do

    registry_database(scenario)


def test_a_runner_that_leaves_fits_to_workers_does_not_measure_its_own_memory(registry_database, store, monkeypatch):
    async def scenario(database):
        await seed(database, n=120)
        monkeypatch.setattr(map_refit, 'fit_allowance', lambda: (1024**2, 1024**3))  # far too little to fit here
        skipped = await submit(database)
        await (await server_runner(database, store, fits_here=True)).run_once()
        row = await job_row(database, skipped)
        assert row['state'] == 'succeeded' and 'Not enough memory' in row['result']['skipped']
        waiting = await submit(database)
        await hand_to_server(database, waiting)
        await (await server_runner(database, store, fits_here=False)).run_once()
        row = await job_row(database, waiting)
        assert (row['state'], row['placement'], row['checkpoint']['phase']) == ('queued', 'compute', 'fit')

    registry_database(scenario)


def test_a_fit_no_worker_takes_ends_after_twelve_hours(registry_database, store):
    async def scenario(database):
        await seed(database, n=120)
        job = await submit(database)
        await hand_to_server(database, job)
        await (await server_runner(database, store)).run_once()
        assert (await map_state(database))['active'] is True  # new samples wait for the new map
        assert await map_refit.expire_unclaimed_fits(database.pool) == 0
        await database.execute(
            "UPDATE jobs SET updated_at = clock_timestamp() - interval '13 hours' WHERE id = $1",
            job['id'],
        )
        assert await map_refit.expire_unclaimed_fits(database.pool) == 1
        row = await job_row(database, job)
        assert row['state'] == 'cancelled' and 'No compute worker took the fit within 12 hours' in row['message']
        assert (await map_state(database))['active'] is False  # projection resumes with the old model

    registry_database(scenario)


def test_uploads_stop_at_their_limit_append_once_and_never_carry_a_pickle(
    registry_database,
    store,
    tmp_path,
    monkeypatch,
):
    async def scenario(database):
        headers = await setup(database, monkeypatch)
        await seed(database, n=120)
        job = await submit(database)
        server = await server_runner(database, store)
        async with client() as api:
            worker = Worker(api, headers)
            await worker.register()
            await worker.claim()  # handed back to the server
            await server.run_once()
            claimed = (await worker.claim()).json()

            def put(name, content, offset=0):
                return api.put(
                    f'{worker.files(claimed, name)}?offset={offset}',
                    content=content,
                    headers=worker.leased(claimed),
                )

            # An output can't grow past what the export's rows allow.
            limit = map_refit.remote_output_limit({'checkpoint': claimed['checkpoint']}, 'coords.npy')
            assert (await put('coords.npy', b'x' * (limit + 1))).status_code == 413
            assert (await put('manifest.json', b'x' * (64 * 1024 + 1))).status_code == 413
            assert not (store.staging_dir(job['id'], claimed['lease_token']) / 'coords.npy').exists()

            # The same chunk sent twice at once is appended once; the other upload is told where to resume.
            responses = await asyncio.gather(put('ids.npy', b'y' * 1000), put('ids.npy', b'y' * 1000))
            assert sorted(response.status_code for response in responses) == [200, 409]
            assert (store.staging_dir(job['id'], claimed['lease_token']) / 'ids.npy').stat().st_size == 1000

            # A pickled array is refused at completion: outputs are read with pickles off. (A new lease, so its
            # staging folder starts empty.)
            output = await worker.fit(claimed, tmp_path / 'worker')
            np.save(output / 'coords.npy', np.array([{'x': 1}] * 120, dtype=object), allow_pickle=True)
            await database.execute(
                "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = $1",
                job['id'],
            )
            assert (await worker.claim()).status_code == 204
            await database.execute('UPDATE jobs SET available_at = clock_timestamp() WHERE id = $1', job['id'])
            fresh = (await worker.claim()).json()
            await worker.upload(fresh, output)
            refused = await worker.complete(fresh, output)
            assert refused.status_code == 400 and 'coords.npy is not a plain array' in refused.json()['detail']

    registry_database(scenario)


@pytest.mark.parametrize('change', ['other ids', 'another order'])
def test_a_worker_whose_rows_are_not_the_export_changes_nothing(
    registry_database,
    store,
    tmp_path,
    monkeypatch,
    change,
):
    async def scenario(database):
        headers = await setup(database, monkeypatch)
        await seed(database, n=120)
        old = await map_points(database)
        job = await submit(database)
        server = await server_runner(database, store)
        async with client() as api:
            worker = Worker(api, headers)
            await worker.register()
            await worker.claim()  # handed back to the server
            await server.run_once()
            claimed = (await worker.claim()).json()
            output = await worker.fit(claimed, tmp_path / 'worker')
            ids = np.load(output / 'ids.npy')
            if change == 'other ids':
                np.save(output / 'ids.npy', ids + 10_000_000)  # would keep none of the map's points
            else:
                np.save(output / 'ids.npy', ids[::-1].copy())  # would put each point at another's place
            await worker.upload(claimed, output)
            assert (await worker.complete(claimed, output)).json() == {'state': 'queued'}

        await server.run_once()
        row = await job_row(database, job)
        assert row['state'] != 'succeeded' and "rows don't match the export" in row['error']['message']
        assert await map_points(database) == old

    registry_database(scenario)
