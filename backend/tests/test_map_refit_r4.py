"""Regression coverage for the map swap and re-rank split."""

import asyncio
import os
import time
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import HTTPException
from fridge import map_fit
from fridge.core import dataset as sdk_dataset

from backend import access, field_registry, job_queue, map_refit
from backend.map_artifacts import LocalArtifactStore
from backend.routers import datasets, embeddings, jobs
from backend.tests import test_field_registry_postgres
from backend.tests.test_map_refit import FIELD, fake_reducer, map_points, map_state, run_job, seed, submit
from backend.tests.test_map_refit_review import assert_grid_ranks

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')
DSN = os.getenv('FRIDGE_TEST_POSTGRES_DSN')
ADMIN = {'id': 1, 'role': 'admin'}
# The rank step holds the gate shared and the rank fence, then pauses (writers are looked at meanwhile).
SLOW_RANK = (
    'SELECT r FROM (SELECT recompute_viz_rank($1, $2, TRUE) AS r) ranked, '
    'LATERAL (SELECT pg_sleep({seconds}) WHERE ranked.r IS NOT NULL) pause'
)


@pytest.fixture
def fake_fit(monkeypatch):
    async def run_fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
        monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
        await asyncio.to_thread(map_fit.main, ['--input', str(source), '--output', str(output)])

    monkeypatch.setattr(map_refit, 'run_fit', run_fit)


async def connect(database, **settings):
    schema = await database.fetchval('SELECT current_schema()')
    return await asyncpg.connect(DSN, server_settings={'search_path': f'{schema},public', **settings})


# --- an exclusive writer is not starved by overlapping ingest --------------------------------------


def test_an_exclusive_writer_gets_the_gate_under_overlapping_ingest(registry_database):
    """Two ingest clients whose 2.5 s transactions overlap: a field operation (exclusive, wait=True)
    keeps its place in the queue and gets the gate after the current batches (before the fix: never in 30 s)."""
    hold = 2.5

    async def scenario(database):
        connections = [await connect(database) for _ in range(3)]
        stop = asyncio.Event()

        async def ingest(connection, offset):
            await asyncio.sleep(offset)
            while not stop.is_set():
                async with connection.transaction():
                    await field_registry.lock_field_lifecycle(connection, 1)
                    await connection.execute(f'SELECT pg_sleep({hold})')

        clients = [asyncio.create_task(ingest(c, i * hold / 2)) for i, c in enumerate(connections[:2])]
        await asyncio.sleep(hold + 0.3)
        started = time.monotonic()
        try:
            async with connections[2].transaction():
                await asyncio.wait_for(
                    field_registry.lock_field_lifecycle(connections[2], 1, exclusive=True),
                    timeout=15,
                )
                acquired = time.monotonic() - started
        finally:
            stop.set()
            await asyncio.gather(*clients, return_exceptions=True)
            for connection in connections:
                await connection.close()
        assert acquired < 2 * hold + 1

    registry_database(scenario)


# --- a cancelled wait ends cancelled, not with a masking error ------------------------------------


def test_a_cancelled_wait_ends_cancelled(registry_database, monkeypatch):
    async def scenario(database):
        holder = await connect(database)
        waiter = await connect(database)
        holding = holder.transaction()
        await holding.start()
        await holder.execute('SELECT pg_advisory_xact_lock(20261016, 1)')
        originals = (field_registry.map_swap_seconds, getattr(field_registry, 'map_swap_holder', None))

        async def slow_probe(connection, dataset_id):  # a probe that is slow (a big lock table)
            await connection.execute('SELECT pg_sleep(1.5)')
            original = originals[1] or originals[0]
            return await original(connection, dataset_id)

        monkeypatch.setattr(field_registry, 'map_swap_seconds', slow_probe)
        monkeypatch.setattr(field_registry, 'map_swap_holder', slow_probe, raising=False)
        outcome = None
        try:
            async with waiter.transaction():
                task = asyncio.create_task(field_registry.lock_field_lifecycle(waiter, 1))
                await asyncio.sleep(0.5)  # inside the probe
                task.cancel()
                try:
                    await task
                except BaseException as error:  # noqa: BLE001
                    outcome = error
        except BaseException:  # noqa: BLE001
            pass
        finally:
            await holding.rollback()
            await holder.close()
            await waiter.close()
        assert isinstance(outcome, asyncio.CancelledError), repr(outcome)

    registry_database(scenario)


# --- writers during the rank step ----------------------------------------------------------


def test_writers_during_the_rank_step(registry_database, tmp_path, monkeypatch, fake_fit):
    """While the rank step holds the gate shared and the rank fence: default ingest and new embeddings
    commit at once; the map stays active, so projection waits and a projected upload is refused
    fast; a manual coordinate upload and a vector change to a placed point get a 409 with the
    re-rank's end after a few seconds, never a 500 or a wait for the whole rank; an exclusive
    writer's 409 says ingest goes on and gives an end close to the rank's budget."""
    monkeypatch.setattr(map_refit, 'RANK_SQL', SLOW_RANK.format(seconds=10))
    for module in (embeddings, datasets):
        monkeypatch.setattr(module, 'require_free_space', AsyncMock())
        monkeypatch.setattr(module, 'validate_embedding_dims', AsyncMock())
    store = LocalArtifactStore(tmp_path)
    seen: dict = {}

    async def scenario(database):
        monkeypatch.setattr(embeddings, 'db', database)
        monkeypatch.setattr(datasets, 'db', database)
        ids = await seed(database, n=200, dim=4)
        await database.execute("SELECT setval(pg_get_serial_sequence('samples', 'id'), 10000)")
        await database.execute("INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES (5001,1,'u.jpg','image')")
        await database.execute(
            "INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = 5001),5001,$1,'[1,2,3,4]')",
            FIELD,
        )
        await database.execute(
            "INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = 5001),5001,'other','[1,2,3,4]')",
        )
        original = map_refit._rank

        async def timed(name, coroutine):
            started = time.monotonic()
            try:
                await coroutine
                seen[name] = ('ok', round(time.monotonic() - started, 1), None)
            except HTTPException as error:
                seen[name] = (error.status_code, round(time.monotonic() - started, 1), error)
            except Exception as error:  # noqa: BLE001
                seen[name] = (type(error).__name__, round(time.monotonic() - started, 1), error)

        async def observed(pool, job, store_):
            ranking = asyncio.create_task(original(pool, job, store_))
            await asyncio.sleep(1.5)  # the rank step holds the gate shared and the fence (sleeping)
            state = await map_refit.read_state(database, 1, FIELD)
            assert state is not None
            seen['active'] = state['active']
            await asyncio.gather(
                timed('add_samples', datasets.add_samples(1, {'samples': [{'filepath': 'new.jpg'}]}, user=ADMIN)),
                timed(
                    'new embedding',
                    embeddings.upload_embeddings(
                        1,
                        [embeddings.EmbeddingUpload(sample_id=5001, field_name='third', vector=[1.0, 1.0, 1.0, 1.0])],
                        ADMIN,
                    ),
                ),
                timed(
                    'projected upload',
                    embeddings.upload_coordinates(
                        1,
                        FIELD,
                        [embeddings.CoordinateUpdate(sample_id=5001, x=1000.0, y=1.0)],
                        ADMIN,
                        model_id=str(state['model_id']),
                    ),
                ),
                timed(
                    'manual upload',
                    embeddings.upload_coordinates(
                        1,
                        'other',
                        [embeddings.CoordinateUpdate(sample_id=5001, x=1.0, y=1.0)],
                        ADMIN,
                    ),
                ),
                timed(
                    'changed vector',
                    embeddings.upload_embeddings(
                        1,
                        [embeddings.EmbeddingUpload(sample_id=ids[3], field_name=FIELD, vector=[9.0, 9.0, 9.0, 9.0])],
                        ADMIN,
                    ),
                ),
                timed('delete', datasets.delete_sample(1, ids[4], ADMIN)),
            )
            seen['rank_running'] = not ranking.done()
            return await ranking

        monkeypatch.setattr(map_refit, '_rank', observed)
        finished = await run_job(database, await submit(database), store)
        assert finished['state'] == 'succeeded'
        assert seen['active'] is True and seen['rank_running'] is True
        assert seen['add_samples'][0] == 'ok' and seen['add_samples'][1] < 1.5
        assert seen['new embedding'][0] == 'ok' and seen['new embedding'][1] < 1.5
        assert seen['projected upload'][0] == 409 and seen['projected upload'][1] < 1.5  # map active
        for name, limit in (('manual upload', 7), ('changed vector', 4)):  # bounded, never a 500
            status, seconds, error = seen[name]
            assert status == 409 and seconds < limit, (name, seen[name])
            assert field_registry.MAP_SWAP_HEADER in (error.headers or {}), name
        status, seconds, error = seen['delete']
        assert status == 409 and seconds < 4
        assert 'ingest goes on' in error.detail
        assert int(error.headers[field_registry.MAP_SWAP_HEADER]) <= map_refit.PUBLISH_MIN_SECONDS + 10
        assert (await map_state(database))['active_job_id'] is None  # the rank step ended the hold
        await assert_grid_ranks(
            database,
            await database.fetchval('SELECT count(*) FROM embedding_coordinates WHERE field_name=$1', FIELD),
        )

    # Each writer above holds a connection while it waits (the refused ones for 2 to 5 s), next to the rank
    # step's. With fewer than one each, a writer's time would include waiting for a free connection.
    registry_database(scenario, pool_size=8)


# --- the rank step cannot be cancelled; a failed one is healed; the counter stays right ---------


def test_the_rank_step_cannot_be_cancelled(registry_database, tmp_path, monkeypatch, fake_fit):
    monkeypatch.setattr(map_refit, 'RANK_SQL', SLOW_RANK.format(seconds=3))
    store = LocalArtifactStore(tmp_path)
    seen: dict = {}

    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await seed(database, n=300, dim=4)
        original = map_refit._rank

        async def observed(pool, job, store_):
            ranking = asyncio.create_task(original(pool, job, store_))
            await asyncio.sleep(1.0)
            page = await jobs.list_active_dataset_jobs(1, limit=25, cursor=None, user=ADMIN)
            seen['can_cancel'] = [item.can_cancel for item in page.running]
            seen['message'] = page.running[0].progress.message
            with pytest.raises(HTTPException) as refused:
                await jobs.request_job_cancellation(job['id'], ADMIN)
            seen['refused'] = (refused.value.status_code, refused.value.detail)
            return await ranking

        monkeypatch.setattr(map_refit, '_rank', observed)
        finished = await run_job(database, await submit(database), store)
        assert finished['state'] == 'succeeded'
        assert seen['can_cancel'] == [False] and 'cannot be stopped' in seen['message']
        assert seen['refused'][0] == 409 and 'being ranked' in seen['refused'][1]
        await assert_grid_ranks(database, 300)

    registry_database(scenario)


def test_a_rank_step_that_fails_for_good_is_healed_and_the_counter_stays_right(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=400, dim=4)
        original_rank = map_refit._rank

        async def failing(pool, job, store_):
            raise map_refit.MapRefitError('the rank step failed for good')

        monkeypatch.setattr(map_refit, '_rank', failing)
        failed = await run_job(database, await submit(database), store)
        assert failed['state'] == 'failed'
        assert all(x > 900 for x, _ in (await map_points(database)).values())  # the new map is in
        state = await map_refit.read_state(database, 1, FIELD)
        assert state is not None
        assert state['active'] is False  # the failed job no longer holds projection
        # The counter still counts the map's points (plus ones placed since), so schedules see new samples.
        assert await map_refit.current_points(database, 1, FIELD) == 400
        await database.execute("INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES (7001,1,'p.jpg','image')")
        await database.execute(
            "INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = 7001),7001,$1,'[1,2,3,4]')",
            FIELD,
        )
        await database.execute(
            "INSERT INTO embedding_coordinates(sample_id,field_name,x,y) VALUES (7001,$1,1000.5,1)",
            FIELD,
        )
        assert await map_refit.current_points(database, 1, FIELD) == 401
        # The sweep's heal queues a re-rank; running it ranks the map exactly.
        monkeypatch.setattr(map_refit, '_rank', original_rank)
        assert await map_refit.heal_unranked_maps(database.pool) == 1
        assert await map_refit.heal_unranked_maps(database.pool) == 0  # not twice
        job = await database.fetchrow("SELECT * FROM jobs WHERE kind='map_refit' AND state='queued'")
        healed = await run_job(database, dict(job), store)
        assert healed['state'] == 'succeeded' and healed['result']['ranked'] == 401
        await assert_grid_ranks(database, 401)
        assert await map_refit.heal_unranked_maps(database.pool) == 0
        # A map left unranked with its counter at 0 (as an older publish left it) is healed too.
        await database.execute("UPDATE embedding_viz_meta SET occupied_cells = 0, rank_stale = TRUE, total_points = 0")
        assert await map_refit.heal_unranked_maps(database.pool) == 1
        job = await database.fetchrow("SELECT * FROM jobs WHERE kind='map_refit' AND state='queued'")
        assert (await run_job(database, dict(job), store))['result']['ranked'] == 401
        assert await map_refit.current_points(database, 1, FIELD) == 401

    registry_database(scenario)


def test_a_cancel_during_the_swaps_commit_waits_for_it_and_is_then_refused(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,
):
    """The swap commits the uncancellable rank checkpoint with its coordinates; a cancel arriving in that
    instant cannot be recorded behind it (it waits, briefly, then is told to retry) and is then refused."""
    store = LocalArtifactStore(tmp_path)
    seen: dict = {}
    original = job_queue.checkpoint_locked

    async def scenario(database):
        await seed(database, n=200, dim=4)

        async def fence_then_cancel(connection, job_id, lease_token, **options):
            fenced = await original(connection, job_id, lease_token, **options)
            try:
                await job_queue.cancel_job(database.pool, job_id)
                seen['during'] = 'recorded'
            except job_queue.QueueBusy:
                seen['during'] = 'busy'
            return fenced

        monkeypatch.setattr(job_queue, 'checkpoint_locked', fence_then_cancel)
        job = await submit(database)
        finished = await run_job(database, job, store)
        assert seen['during'] == 'busy'
        assert finished['state'] == 'succeeded'
        await assert_grid_ranks(database, 200)
        assert not await database.fetchval('SELECT cancel_requested FROM job_controls WHERE job_id=$1', job['id'])

    registry_database(scenario)


@pytest.mark.parametrize('fence', ['checkpoint_locked', 'finish_locked'])
def test_a_busy_job_row_at_the_fence_is_contention_not_a_failure(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,
    fence,
):
    """The job's own lease renewal holding its row (a slow commit) past the fence's lock timeout: the swap
    (or the re-rank) rolls back and is retried as contention (failures stay 0), then succeeds."""
    store = LocalArtifactStore(tmp_path)
    original = getattr(job_queue, fence)
    seen: list = []

    async def scenario(database):
        await seed(database, n=200, dim=4)

        async def renewal(job_id, locked):
            async with database.pool.acquire() as holder, holder.transaction():
                await holder.execute('SELECT 1 FROM jobs WHERE id=$1 FOR UPDATE', job_id)
                locked.set()
                await asyncio.sleep(2.5)  # past the fence's 2 s lock timeout

        async def busy_fence(connection, job_id, lease_token, **options):
            if seen:  # the retry: the first attempt was deferred as contention
                row = await database.fetchrow('SELECT failures, contentions, error FROM jobs WHERE id=$1', job_id)
                seen.append(dict(row))
                return await original(connection, job_id, lease_token, **options)
            locked = asyncio.Event()
            holding = asyncio.create_task(renewal(job_id, locked))
            await locked.wait()
            try:
                return await original(connection, job_id, lease_token, **options)
            except BaseException as error:
                seen.append(type(error).__name__)
                raise
            finally:
                await holding

        monkeypatch.setattr(job_queue, fence, busy_fence)
        finished = await run_job(database, await submit(database), store)
        assert seen[0] == 'QueueContended', seen
        assert seen[1]['failures'] == 0 and seen[1]['contentions'] == 1, seen
        assert seen[1]['error']['type'] == 'QueueContended'
        assert finished['state'] == 'succeeded'
        await assert_grid_ranks(database, 200)

    registry_database(scenario)
