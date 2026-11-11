"""Large datasets under a continuous ingest soak: writers that waited on the
dataset gate without a bound sat out the whole 205 s swap (and hit client timeouts), and 118 s of that
swap was writing the new map's ranks under the gate. Writers now refuse fast behind a swap, and the
re-rank runs after the swap with the gate shared."""

import asyncio
import math
import os
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fridge import map_fit
from fridge.core import dataset as sdk_dataset

from backend import field_registry, map_refit
from backend.map_artifacts import LocalArtifactStore
from backend.routers import embeddings
from backend.tests import test_field_registry_postgres
from backend.tests.test_map_refit import FIELD, USER, fake_reducer, map_points, map_state, run_job, seed, submit
from backend.tests.test_map_refit_review import assert_grid_ranks

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


@pytest.fixture
def fake_fit(monkeypatch):
    """Stub the fit process and its host probe; read real exports and write real outputs."""

    async def run_fit(command, source, output, log_path, allowance, bytes_per_point=None, **sizing):
        monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
        await asyncio.to_thread(map_fit.main, ['--input', str(source), '--output', str(output)])

    # No child is launched, so these database contracts do not require host systemd/cgroups.
    monkeypatch.setattr(map_refit, 'containment', lambda allowance: [])
    monkeypatch.setattr(map_refit, 'run_fit', run_fit)


async def hold(database, *, swap: bool, exclusive: bool = True, seconds_left: float = 240):
    """A session holding the dataset's gate, named as a map swap names itself (or not)."""
    connection = await database.pool.acquire()
    transaction = connection.transaction()
    await transaction.start()
    if swap:
        now = await connection.fetchval('SELECT extract(epoch FROM clock_timestamp())::float8')
        name = field_registry.MAP_SWAP_APPLICATION
        await connection.execute(f"SET LOCAL application_name = '{name} {math.ceil(now + seconds_left)}'")
    function = 'pg_advisory_xact_lock' if exclusive else 'pg_advisory_xact_lock_shared'
    await connection.execute(f'SELECT {function}(20261016, 1)')
    return connection, transaction


async def release(database, held):
    connection, transaction = held
    await transaction.rollback()
    await database.pool.release(connection)


# --- 1. Writers behind a swap refuse fast, with its end; elsewhere they still wait --------------------


@pytest.mark.parametrize('exclusive', [False, True])
def test_an_unbounded_gate_wait_refuses_fast_behind_a_swap(registry_database, exclusive):
    async def scenario(database):
        held = await hold(database, swap=True)
        try:
            async with database.pool.acquire() as connection, connection.transaction():
                started = time.monotonic()
                with pytest.raises(HTTPException) as refused:
                    await asyncio.wait_for(
                        field_registry.lock_field_lifecycle(connection, 1, exclusive=exclusive),
                        timeout=10,
                    )
                assert time.monotonic() - started < field_registry.BOUNDED_LIFECYCLE_WAIT_SECONDS + 2
            headers = refused.value.headers or {}
            assert refused.value.status_code == 409 and 230 <= int(headers[field_registry.MAP_SWAP_HEADER]) <= 241
        finally:
            await release(database, held)

    registry_database(scenario)


def test_the_rank_step_holding_the_gate_shared_still_tells_exclusive_writers_its_end(registry_database):
    async def scenario(database):
        held = await hold(database, swap=True, exclusive=False)
        try:
            async with database.pool.acquire() as connection, connection.transaction():
                await field_registry.lock_field_lifecycle(connection, 1)  # shared ingest goes on
            async with database.pool.acquire() as connection, connection.transaction():
                with pytest.raises(HTTPException) as refused:
                    await field_registry.lock_field_lifecycle(connection, 1, exclusive=True, wait=False)
            assert field_registry.MAP_SWAP_HEADER in (refused.value.headers or {})
        finally:
            await release(database, held)

    registry_database(scenario)


def test_an_ordinary_holder_is_waited_for_and_a_queued_writer_is_not_overtaken_by_a_swap(registry_database):
    async def scenario(database):
        # An ordinary exclusive holder (a field operation): the writer waits in the queue, then gets the gate.
        held = await hold(database, swap=False)
        swap = None
        try:
            async with database.pool.acquire() as connection, connection.transaction():
                before = await connection.fetchval("SELECT current_setting('lock_timeout')")
                waiter = asyncio.create_task(field_registry.lock_field_lifecycle(connection, 1))
                await asyncio.sleep(0.5)
                assert not waiter.done()
                # A re-fit starting now cannot take the gate ahead of the queued writer.
                swap = await database.pool.acquire()
                swap_transaction = swap.transaction()
                await swap_transaction.start()
                taking = asyncio.create_task(map_refit._take_gate(swap, 1))
                await asyncio.sleep(0.5)
                await release(database, held)
                held = None
                await asyncio.wait_for(waiter, 5)
                assert not taking.done()  # the swap waits for the writer's transaction
                assert await connection.fetchval('SELECT 1') == 1  # the transaction is intact
                assert await connection.fetchval("SELECT current_setting('lock_timeout')") == before
            await asyncio.wait_for(taking, 5)  # then the swap gets it
        finally:
            if held is not None:
                await release(database, held)
            if swap is not None:
                await swap_transaction.rollback()
                await database.pool.release(swap)

    registry_database(scenario)


def test_a_writer_queued_the_instant_a_swap_took_the_gate_goes_first(registry_database, monkeypatch):
    """A writer that looked for a re-fit just before one took the gate (and saw none) queues behind it:
    the re-fit notices within its grace and lets the writer go first."""
    monkeypatch.setattr(map_refit, 'GATE_GRACE_SECONDS', 1.0)

    async def scenario(database):
        swap = await database.pool.acquire()
        swap_transaction = swap.transaction()
        await swap_transaction.start()
        try:
            taking = asyncio.create_task(map_refit._take_gate(swap, 1))
            async with database.pool.acquire() as other:
                for _ in range(100):  # until the swap holds the gate (within its grace)
                    if await other.fetchval(
                        """SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid=20261016
                           AND objid=1 AND objsubid=2 AND mode='ExclusiveLock' AND granted)""",
                    ):
                        break
                    await asyncio.sleep(0.02)
                started = time.monotonic()
                async with other.transaction():
                    await other.execute('SELECT pg_advisory_xact_lock_shared(20261016, 1)')  # queues
                    waited = time.monotonic() - started
            assert waited < 2.0  # not the whole swap
            await asyncio.wait_for(taking, 5)
        finally:
            await swap_transaction.rollback()
            await database.pool.release(swap)

    registry_database(scenario)


def test_an_embedding_upload_during_a_swap_refuses_fast(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    monkeypatch.setattr(embeddings, 'validate_embedding_dims', AsyncMock())

    async def scenario(database):
        monkeypatch.setattr(embeddings, 'db', database)
        await database.execute("INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES (1,1,'a.jpg','image')")
        held = await hold(database, swap=True)
        try:
            upload = [embeddings.EmbeddingUpload(sample_id=1, field_name=FIELD, vector=[0.5, 0.25, 0.125])]
            started = time.monotonic()
            with pytest.raises(HTTPException) as refused:
                await asyncio.wait_for(embeddings.upload_embeddings(1, upload, USER), timeout=10)
            assert time.monotonic() - started < 5 and refused.value.status_code == 409
            assert field_registry.MAP_SWAP_HEADER in (refused.value.headers or {})
        finally:
            await release(database, held)
        assert await database.fetchval('SELECT count(*) FROM embeddings') == 0  # nothing was written

    registry_database(scenario)


# --- 2. The re-rank runs after the swap, with the gate shared ---------------------------------------


def test_the_rerank_runs_after_the_swap_with_ingest_flowing(registry_database, tmp_path, fake_fit):
    store = LocalArtifactStore(tmp_path)
    seen = {}

    async def scenario(database):
        await seed(database, n=3000, dim=4)
        original = map_refit._rank

        async def observe(pool, job, store_):
            # The swap has committed: new coordinates, no ranks yet (the scatter reads coordinates), the
            # gate free for ingest. A sample the projection places now is ranked by the step too.
            async with database.pool.acquire() as other, other.transaction():
                seen['shared'] = await other.fetchval('SELECT pg_try_advisory_xact_lock_shared(20261016, 1)')
            seen['meta'] = dict(
                await database.fetchrow(
                    'SELECT occupied_cells, rank_stale FROM embedding_viz_meta WHERE dataset_id=1 AND field_name=$1',
                    FIELD,
                ),
            )
            seen['ranks'] = await database.fetchval('SELECT count(*) FROM embedding_viz_rank')
            seen['phase'] = job['checkpoint']['phase']
            await database.execute(
                "INSERT INTO samples(id,dataset_id,filepath,media_type) VALUES (9001,1,'n.jpg','image')",
            )
            await database.execute(
                "INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) VALUES ("
                "(SELECT dataset_id FROM samples WHERE id = 9001),9001,$1,'[1,2,3,4]')",
                FIELD,
            )
            await database.execute(
                'INSERT INTO embedding_coordinates(sample_id,field_name,x,y) VALUES (9001,$1,1000.5,1.5)',
                FIELD,
            )
            return await original(pool, job, store_)

        map_refit._rank = observe
        try:
            finished = await run_job(database, await submit(database), store)
        finally:
            map_refit._rank = original
        assert finished['state'] == 'succeeded'
        assert seen == {
            'shared': True,
            'meta': {'occupied_cells': 0, 'rank_stale': True},
            'ranks': 0,
            'phase': 'rank',
        }
        seconds = finished['result']['publish_seconds']
        assert 'ranks' not in seconds and seconds['ranks_after_gate'] >= 0 and seconds['gate_held'] >= 0
        assert finished['result']['points'] == 3000 and finished['result']['ranked'] == 3001
        await assert_grid_ranks(database, 3001)
        assert (await map_state(database))['points_at_refit'] == 3000

    registry_database(scenario)


def test_a_runner_stopping_after_the_swap_resumes_at_the_rerank(registry_database, tmp_path, fake_fit):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=500, dim=4)
        original = map_refit._rank
        calls = []

        async def stopped_once(pool, job, store_):
            calls.append(job['checkpoint']['phase'])
            if len(calls) == 1:
                raise ConnectionError('the runner stopped right after the swap committed')
            return await original(pool, job, store_)

        map_refit._rank = stopped_once
        try:
            job = await submit(database)
            interrupted = await run_job(database, job, store, max_units=4)
            assert interrupted['state'] == 'queued' and interrupted['checkpoint']['phase'] == 'rank'
            assert all(x > 900 for x, _ in (await map_points(database)).values())  # the new map is in
            # Projection stays paused until the re-rank commits (map state still names the job).
            assert (await map_state(database))['active_job_id'] == interrupted['id']
            finished = await run_job(database, interrupted, store)
        finally:
            map_refit._rank = original
        assert finished['state'] == 'succeeded' and calls == ['rank', 'rank']
        await assert_grid_ranks(database, 500)

    registry_database(scenario)
