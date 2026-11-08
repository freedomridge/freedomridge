"""`python -m fridge.worker` does a map re-fit's fit: the SDK worker against the worker API, in this process.

The worker's HTTP calls (requests) are answered by the worker router on the test's event loop, while the worker
runs in a thread, so its whole flow runs as it would against a server: claim, lease heartbeats, download,
the fit, upload, complete; and the server's runner then swaps the new map in.
"""

import asyncio
import json
import os
import signal
import threading
import uuid

import httpx
import pytest
import requests
from fastapi import FastAPI
from fridge import map_fit
from fridge import worker as sdk_worker
from fridge.core.client import Client

from backend import job_queue, map_refit
from backend.routers import workers
from backend.tests import test_field_registry_postgres
from backend.tests.test_map_refit import map_points, map_state, seed, submit
from backend.tests.test_map_refit_remote import server_runner, setup, store  # noqa: F401 (store: fixture)

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


@pytest.fixture
def sdk_over_app(monkeypatch, tmp_path):
    """A fridge Client whose requests the worker router answers here; work files under tmp_path."""
    app = FastAPI()
    app.include_router(workers.router, prefix='/api')
    loop: dict = {}
    monkeypatch.setenv('FRIDGE_WORKER_DIR', str(tmp_path / 'worker'))
    monkeypatch.setattr(workers, 'JOB_HEARTBEAT_SECONDS', 0.2)  # leases are renewed within the test's time

    def transport(session, method, url, **kwargs):
        prepared = session.prepare_request(
            requests.Request(
                method,
                url,
                params=kwargs.get('params'),
                data=kwargs.get('data'),
                json=kwargs.get('json'),
                headers=kwargs.get('headers'),
            ),
        )

        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
                return await api.request(
                    prepared.method,
                    prepared.url,
                    content=prepared.body,
                    headers=dict(prepared.headers),
                )

        answer = asyncio.run_coroutine_threadsafe(call(), loop['loop']).result(timeout=120)
        response = requests.Response()
        response.status_code = answer.status_code
        response.headers.update(answer.headers)
        response._content = answer.content
        response._content_consumed = True
        response.url = prepared.url
        response.request = prepared
        return response

    monkeypatch.setattr(requests.Session, 'request', transport)

    def make(headers):
        loop['loop'] = asyncio.get_running_loop()
        return Client(base_url='http://test/api', api_token=headers['Authorization'].removeprefix('Bearer '))

    return make


async def fit_phase(database, store, client):  # noqa: F811
    """A queued re-fit, exported by the server and waiting for a compute worker: (worker id, claimed job)."""
    await seed(database, n=120)
    job = await submit(database)
    server = await server_runner(database, store)
    worker = await asyncio.to_thread(client.register_worker, 'desk', ['map_refit'])
    assert await asyncio.to_thread(client.claim_worker_job, worker['worker_id'], 0) is None  # its start: the server's
    await server.run_once()  # the export, then the fit is compute work
    claimed = await asyncio.to_thread(client.claim_worker_job, worker['worker_id'], 0)
    assert claimed['id'] == str(job['id']) and claimed['checkpoint']['phase'] == 'fit'
    return worker['worker_id'], claimed, server


async def job_row(database, job):
    return await database.fetchrow('SELECT * FROM jobs WHERE id = $1', uuid.UUID(job['id']))


def in_process_fit(calls, outcomes=()):
    """A run_fit that runs fridge.map_fit here (with the test's fake reducer) after the planned `outcomes`."""
    planned = list(outcomes)

    def run_fit(source, output, log, max_fit_size, stop):
        calls.append(max_fit_size)
        if planned:
            return planned.pop(0)(output)
        map_fit.main(['--input', str(source), '--output', str(output)])
        return 0

    return run_fit


def killed_at(fit_size, rows):
    """An attempt the kernel killed for memory, after planning `fit_size` of `rows` points."""

    def outcome(output):
        output.mkdir(parents=True)
        (output / 'plan.json').write_text(json.dumps({'fit_size': fit_size, 'rows': rows}))
        return -signal.SIGKILL

    return outcome


def test_the_sdk_worker_fits_a_map_with_the_real_child_and_the_server_swaps_it_in(
    registry_database,
    store,  # noqa: F811
    monkeypatch,
    sdk_over_app,
    tmp_path,
):
    async def scenario(database):
        client = sdk_over_app(await setup(database, monkeypatch))
        worker_id, job, server = await fit_phase(database, store, client)
        old = await map_points(database)  # exported, not yet replaced
        # The real `python -m fridge.map_fit` child (umap-learn), as on a worker machine.
        await asyncio.to_thread(sdk_worker.do_job, client, worker_id, job)
        row = await job_row(database, job)
        assert (row['placement'], row['checkpoint']['phase'], row['checkpoint']['remote']) == (
            'server',
            'publish',
            True,
        )
        assert not (tmp_path / 'worker' / job['id']).exists()  # its work folder is gone
        await server.run_once()  # the swap and the ranks
        row = await job_row(database, job)
        assert row['state'] == 'succeeded', row['error']
        new = await map_points(database)
        assert set(new) == set(old) and new != old
        assert (await map_state(database))['artifact_location'] is None and list(store.models()) == []

    registry_database(scenario)


def test_after_an_out_of_memory_kill_the_worker_fits_fewer_points(
    registry_database,
    store,  # noqa: F811
    monkeypatch,
    sdk_over_app,
):
    async def scenario(database):
        client = sdk_over_app(await setup(database, monkeypatch))
        worker_id, job, server = await fit_phase(database, store, client)
        calls: list = []
        monkeypatch.setattr(sdk_worker, 'run_fit', in_process_fit(calls, [killed_at(80_000, 100_000)]))
        await asyncio.to_thread(sdk_worker.do_job, client, worker_id, job)
        assert calls == [None, 60_000]  # 75% of the killed attempt, then done
        assert (await job_row(database, job))['checkpoint']['phase'] == 'publish'

    registry_database(scenario)


def test_an_out_of_memory_kill_at_the_smallest_fit_fails_the_job(
    registry_database,
    store,  # noqa: F811
    monkeypatch,
    sdk_over_app,
):
    async def scenario(database):
        client = sdk_over_app(await setup(database, monkeypatch))
        worker_id, job, server = await fit_phase(database, store, client)
        calls: list = []
        monkeypatch.setattr(sdk_worker, 'run_fit', in_process_fit(calls, [killed_at(120, 120)]))
        await asyncio.to_thread(sdk_worker.do_job, client, worker_id, job)
        row = await job_row(database, job)
        assert calls == [None] and row['state'] == 'failed'
        assert 'ran out of memory on this worker at 120 points' in row['error']['message']

    registry_database(scenario)


@pytest.mark.parametrize('ending', ['lease lost', 'cancelled'])
def test_a_lost_lease_or_a_cancel_stops_the_fit_at_once(
    registry_database,
    store,  # noqa: F811
    monkeypatch,
    sdk_over_app,
    ending,
):
    async def scenario(database):
        client = sdk_over_app(await setup(database, monkeypatch))
        worker_id, job, server = await fit_phase(database, store, client)
        fitting = threading.Event()
        stopped: list = []

        def long_fit(source, output, log, max_fit_size, stop):
            fitting.set()
            for _ in range(600):  # a fit that would take a minute, unless stopped
                if stop():
                    stopped.append(True)
                    return None
                threading.Event().wait(0.1)
            return 0

        monkeypatch.setattr(sdk_worker, 'run_fit', long_fit)
        working = asyncio.create_task(asyncio.to_thread(sdk_worker.do_job, client, worker_id, job))
        await asyncio.to_thread(fitting.wait, 30)
        if ending == 'lease lost':  # the lease ran out (a laptop asleep): the job is no longer this worker's
            await database.execute(
                "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = $1::uuid",
                job['id'],
            )
        else:
            await job_queue.cancel_job(database.pool, uuid.UUID(job['id']))
        await asyncio.wait_for(working, 60)
        assert stopped == [True]
        row = await job_row(database, job)
        assert row['state'] == ('cancelled' if ending == 'cancelled' else 'running')  # the expiry path takes it back
        assert row['checkpoint']['phase'] == 'fit'  # nothing was published

    registry_database(scenario)


class _Released:
    def __init__(self):
        self.released = []

    def release_worker_job(self, worker_id, job_id, lease_token):
        self.released.append(job_id)


def test_stopping_the_worker_gives_its_job_back(monkeypatch, tmp_path):
    monkeypatch.setenv('FRIDGE_WORKER_DIR', str(tmp_path))
    client = _Released()

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(sdk_worker, 'fit_map', interrupted)
    job = {'id': 'job-1', 'kind': map_refit.KIND, 'checkpoint': {'phase': 'fit'}, 'lease_token': 'lease'}
    with pytest.raises(KeyboardInterrupt):
        sdk_worker.do_job(client, 'desk', job)
    assert client.released == ['job-1'] and not (tmp_path / 'job-1').exists()
    # A job the worker doesn't do is given back at once.
    sdk_worker.do_job(client, 'desk', {'id': 'job-2', 'kind': 'other', 'lease_token': 'lease'})
    assert client.released == ['job-1', 'job-2']


def test_outputs_the_server_refuses_fail_that_job_and_the_worker_goes_on(
    registry_database,
    store,  # noqa: F811
    monkeypatch,
    sdk_over_app,
):
    async def scenario(database):
        client = sdk_over_app(await setup(database, monkeypatch))
        worker_id, job, server = await fit_phase(database, store, client)
        monkeypatch.setattr(sdk_worker, 'run_fit', in_process_fit([]))
        monkeypatch.setattr(sdk_worker, '_sha256', lambda path: '0' * 64)  # outputs the server won't take
        assert await asyncio.to_thread(sdk_worker.do_job, client, worker_id, job) is True  # no exception
        row = await job_row(database, job)
        assert row['state'] == 'failed' and row['error']['type'] == 'WorkerError'
        assert "400: The uploaded ids.npy doesn't match its sha256" in row['error']['message']

    registry_database(scenario)


def _refusal(status, detail):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps({'detail': detail}).encode()
    return requests.HTTPError(f'{status}', response=response)


def test_only_a_refusal_of_the_worker_itself_stops_it():
    assert sdk_worker._about_the_worker(_refusal(401, 'Invalid token'))
    assert sdk_worker._about_the_worker(_refusal(403, 'Worker tokens only'))
    assert sdk_worker._about_the_worker(_refusal(404, 'Unknown worker: register again'))
    assert not sdk_worker._about_the_worker(_refusal(404, "This job has no input 'vectors.bin'"))
    assert not sdk_worker._about_the_worker(_refusal(413, 'coords.npy takes at most 0 more bytes here'))


def test_a_job_given_back_unworked_is_not_claimed_again_at_once():
    class Server:
        base_url = 'http://server/api'
        claims = 0

        def register_worker(self, *args):
            return {'worker_id': 'desk'}

        def claim_worker_job(self, worker_id, wait_seconds):
            self.claims += 1
            if self.claims > 2:
                raise _refusal(401, 'revoked')
            return {'id': f'job-{self.claims}'}

    waits: list = []
    with pytest.raises(requests.HTTPError):
        sdk_worker.run(Server(), 'desk', ['map_refit'], sleep=waits.append, work=lambda *args: False)
    assert waits == [sdk_worker.RETRY_SECONDS, sdk_worker.RETRY_SECONDS]


def test_an_unexpected_error_renewing_the_lease_stops_the_work():
    class Client:
        def worker_job_heartbeat(self, *args, **kwargs):
            raise RuntimeError('the server runs another major version')

    lease = sdk_worker._Lease(Client(), 'desk', {'id': 'job-1', 'lease_token': 'lease', 'heartbeat_seconds': 0.01})
    with lease:
        assert lease.lost.wait(5)
    assert lease.ended and 'another major version' in lease.reason


def test_a_refusal_whose_body_is_not_a_json_object_is_still_judged():
    response = requests.Response()
    response.status_code = 404
    response._content = b'["not", "an", "object"]'
    assert not sdk_worker._about_the_worker(requests.HTTPError('404', response=response))
