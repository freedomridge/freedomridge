"""Map re-fit regressions: free space, Run now, backups, writers during the swap, foreign locks and uploads."""

import asyncio
import math
import os
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import HTTPException
from fridge import map_fit
from fridge.core import dataset as sdk_dataset

from backend import backups, field_registry, job_queue, map_refit
from backend.map_artifacts import LocalArtifactStore
from backend.routers import embeddings, jobs
from backend.tests import test_field_registry_postgres
from backend.tests.test_map_refit import FIELD, fake_reducer, run_job, seed, submit
from backend.tests.test_map_refit_review import assert_grid_ranks

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')
DSN = os.getenv('FRIDGE_TEST_POSTGRES_DSN', '')
ADMIN = {'id': 1, 'role': 'admin'}
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


async def unranked_map(database, store, monkeypatch, n=300):
    """A re-fit whose rank step failed for good: the new map is in, unranked."""
    await seed(database, n=n, dim=4)
    original = map_refit._rank

    async def failing(pool, job, store_):
        raise map_refit.MapRefitError('the rank step failed')

    monkeypatch.setattr(map_refit, '_rank', failing)
    failed = await run_job(database, await submit(database), store)
    monkeypatch.setattr(map_refit, '_rank', original)
    assert failed['state'] == 'failed'
    return failed


async def queued_heal(database):
    assert await map_refit.heal_unranked_maps(database.pool) == 1
    return dict(await database.fetchrow("SELECT * FROM jobs WHERE kind='map_refit' AND state='queued'"))


async def later(database, minutes):
    """Move the clock on by moving the ended heal jobs back."""
    await database.execute(
        "UPDATE jobs SET finished_at = finished_at - make_interval(mins => $1) WHERE args ? 'rank_only'",
        minutes,
    )


async def map_state(database):
    state = await map_refit.read_state(database, 1, FIELD)
    assert state is not None
    return state


async def ranks(database):
    return await database.fetchval('SELECT count(*) FROM embedding_viz_rank WHERE field_name=$1', FIELD)


# --- no free space, no rank; heal backs off; a queued rank job can be cancelled; projection pauses ----


def test_the_rank_step_refuses_without_free_space_and_writes_nothing(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,
):
    store = LocalArtifactStore(tmp_path)
    asked: list = []

    async def full(connection, needed, what):
        asked.append((needed, what))
        raise HTTPException(status_code=507, detail=f'Not enough free disk on the database volume for {what}')

    monkeypatch.setattr(map_refit, 'require_free_space', full, raising=False)

    async def scenario(database):
        await seed(database, n=200, dim=4)
        job = await run_job(database, await submit(database), store)
        assert job['state'] == 'failed', job['state']
        assert job['error']['type'] == 'MapRefitError' and 'Not enough free disk' in job['error']['message']
        assert asked and 're-rank' in asked[0][1]
        assert await ranks(database) == 0  # the swap dropped the old ranks; no rank was written
        assert (await map_state(database))['active'] is False

    registry_database(scenario)


def test_heal_backs_off_and_a_queued_rank_job_can_be_cancelled(registry_database, tmp_path, monkeypatch, fake_fit):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr('backend.access.db', database)
        await unranked_map(database, store, monkeypatch)
        # The re-fit was a day ago (the test moves the heal jobs' ends back in time, after it).
        await database.execute("UPDATE embedding_map_state SET refitted_at = refitted_at - interval '1 day'")
        heal = await queued_heal(database)
        # Queued (not running), the rank job can be cancelled, and the Jobs panel offers it.
        page = await jobs.list_active_dataset_jobs(1, limit=25, cursor=None, user=ADMIN)
        assert [item.can_cancel for item in page.waiting] == [True]
        cancelled = await jobs.request_job_cancellation(heal['id'], ADMIN)
        assert cancelled.state == 'cancelled'
        # Then the heal waits an hour before it offers the map again...
        assert await map_refit.heal_unranked_maps(database.pool) == 0
        await later(database, 61)
        heal = await queued_heal(database)
        # ...and a heal that fails doubles the gap (two hours after the second).
        monkeypatch.setattr(map_refit, 'RANK_SQL', 'SELECT recompute_viz_rank($1, $2, TRUE) / (SELECT 0)')
        assert (await run_job(database, heal, store))['state'] == 'failed'
        await later(database, 61)
        assert await map_refit.heal_unranked_maps(database.pool) == 0
        await later(database, 60)
        heal = await queued_heal(database)
        monkeypatch.setattr(map_refit, 'RANK_SQL', 'SELECT recompute_viz_rank($1, $2, TRUE)')
        assert (await run_job(database, heal, store))['state'] == 'succeeded'
        await assert_grid_ranks(database, 300)
        assert await map_refit.heal_unranked_maps(database.pool) == 0

    registry_database(scenario)


def test_projection_pauses_during_a_heal_jobs_rank(registry_database, tmp_path, monkeypatch, fake_fit):
    store = LocalArtifactStore(tmp_path)
    seen: dict = {}

    async def scenario(database):
        await unranked_map(database, store, monkeypatch)
        heal = await queued_heal(database)
        assert (await map_state(database))['active'] is False
        monkeypatch.setattr(map_refit, 'RANK_SQL', SLOW_RANK.format(seconds=3))
        original = map_refit._rank

        async def observed(pool, job, store_):
            ranking = asyncio.create_task(original(pool, job, store_))
            await asyncio.sleep(1.5)
            seen['active'] = (await map_state(database))['active']
            return await ranking

        monkeypatch.setattr(map_refit, '_rank', observed)
        assert (await run_job(database, heal, store))['state'] == 'succeeded'
        assert seen['active'] is True  # SDK project_pending and the projection worker wait
        state = await map_state(database)
        assert state['active'] is False and state['active_job_id'] is None
        await assert_grid_ranks(database, 300)

    registry_database(scenario)


# --- Run now / a schedule tick is not swallowed by a heal job ------------------------------------------


def test_run_now_while_a_heal_job_waits_queues_a_refit(registry_database, tmp_path, monkeypatch, fake_fit):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await unranked_map(database, store, monkeypatch)
        heal = await queued_heal(database)
        before = await map_state(database)
        async with database.pool.acquire() as connection, connection.transaction():
            refit, created = await map_refit.submit(connection, 1, FIELD, created_by=1)
        assert created and refit['id'] != heal['id']
        assert 'rank_only' not in refit['args'] and refit['resources'] == map_refit.RESOURCES
        # The heal still runs (it is first in the dataset's maintenance lane), then the re-fit.
        assert await map_refit.heal_unranked_maps(database.pool) == 0
        assert (await run_job(database, heal, store))['state'] == 'succeeded'
        assert (await run_job(database, refit, store))['state'] == 'succeeded'
        after = await map_state(database)
        assert after['model_id'] != before['model_id'] and after['refitted_at'] > before['refitted_at']
        await assert_grid_ranks(database, 300)

    registry_database(scenario)


# --- the rank step and backups keep apart -----------------------------------------------------------


def test_the_rank_step_waits_for_a_backup_and_a_backup_waits_for_the_rank(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await unranked_map(database, store, monkeypatch)
        heal = await queued_heal(database)
        backup = await job_queue.enqueue_job(database.pool, 'backup', lane='maintenance')
        waiting = await run_job(database, heal, store, max_units=1)
        assert waiting['state'] == 'queued' and waiting['error']['type'] == 'QueueContended'
        assert waiting['failures'] == 0 and await ranks(database) == 0
        await database.execute("DELETE FROM jobs WHERE id=$1", backup['id'])
        # While a rank runs, the backup scheduler holds tonight's backup back.
        await database.execute(
            """UPDATE jobs SET state='running', worker_id='w', lease_token=gen_random_uuid(),
               lease_expires_at=clock_timestamp()+interval '1 minute' WHERE id=$1""",
            heal['id'],
        )
        await database.execute(
            "UPDATE backup_schedule SET interval_hours=1, timing_changed_at=now() - interval '3 hours'",
        )
        assert await backups.schedule_backups(database.pool, str(tmp_path)) is None
        await database.execute(
            "UPDATE jobs SET state='queued', worker_id=NULL, lease_token=NULL, lease_expires_at=NULL WHERE id=$1",
            heal['id'],
        )
        assert await backups.schedule_backups(database.pool, str(tmp_path)) is not None

    registry_database(scenario)


# --- a writer held up between its look and its queueing does not sit out the swap --------------------


async def hold_swap(database, seconds, taken):
    connection = await connect(database, statement_timeout='0')
    try:
        async with connection.transaction():
            now = await connection.fetchval('SELECT extract(epoch FROM clock_timestamp())::float8')
            name = f'{field_registry.MAP_SWAP_APPLICATION} {math.ceil(now + 60)}'
            await connection.execute(f"SET LOCAL application_name = '{name}'")
            await map_refit._take_gate(connection, 1)
            taken.set()
            await asyncio.sleep(seconds)
    finally:
        await connection.close()


def test_a_writer_stalled_between_its_look_and_its_queueing_does_not_sit_out_the_swap(registry_database, monkeypatch):
    """An ingest batch holds the gate; an exclusive writer looks (no re-fit), then its event loop is busy
    for 0.5 s (past the swap's 0.2 s grace) while a swap takes the gate: it learns of the swap in about a
    second (the bounded first wait), not after the swap's 8 s."""
    original = field_registry.map_swap_holder
    looks: list = []

    async def stalled(connection, dataset_id):
        holder = await original(connection, dataset_id)
        looks.append(holder)
        if holder is None and len(looks) == 1:
            await asyncio.sleep(0.5)
        return holder

    monkeypatch.setattr(field_registry, 'map_swap_holder', stalled)

    async def scenario(database):
        ingest = await connect(database)
        writer = await connect(database)
        taken = asyncio.Event()
        try:

            async def batch():
                async with ingest.transaction():
                    await ingest.execute('SELECT pg_advisory_xact_lock_shared(20261016, 1)')
                    await asyncio.sleep(0.3)

            async def exclusive_writer():
                await asyncio.sleep(0.05)
                started = time.monotonic()
                try:
                    async with writer.transaction():
                        await field_registry.lock_field_lifecycle(writer, 1, exclusive=True)
                        return 'gate', time.monotonic() - started
                except HTTPException as error:
                    return error.status_code, time.monotonic() - started

            async def swap():
                await asyncio.sleep(0.1)
                await hold_swap(database, 8.0, taken)

            _, (outcome, seconds), _ = await asyncio.gather(batch(), exclusive_writer(), swap())
            assert taken.is_set()
            # A 409 with the swap's end, or the gate if the swap saw it queue and let it go first.
            assert outcome in (409, 'gate') and seconds < 4.5, (outcome, seconds)
        finally:
            await ingest.close()
            await writer.close()

    registry_database(scenario)


def test_a_writer_cancelled_in_its_first_wait_ends_cancelled(registry_database):
    async def scenario(database):
        holder = await connect(database)
        waiter = await connect(database)
        holding = holder.transaction()
        await holding.start()
        await holder.execute('SELECT pg_advisory_xact_lock(20261016, 1)')
        outcome = None
        try:
            async with waiter.transaction():
                task = asyncio.create_task(field_registry.lock_field_lifecycle(waiter, 1))
                await asyncio.sleep(0.5)  # in the bounded first wait
                task.cancel()
                try:
                    await task
                except BaseException as error:  # noqa: BLE001
                    outcome = error
        except BaseException:  # noqa: BLE001
            pass
        finally:
            await holding.rollback()
        # And a wait granted in its first second leaves lock_timeout as it was.
        holding = holder.transaction()
        await holding.start()
        await holder.execute('SELECT pg_advisory_xact_lock(20261016, 1)')

        async def release():
            await asyncio.sleep(0.3)
            await holding.rollback()

        releasing = asyncio.create_task(release())
        async with waiter.transaction():
            await waiter.execute("SET LOCAL lock_timeout = '7s'")
            await field_registry.lock_field_lifecycle(waiter, 1)
            assert await waiter.fetchval("SELECT current_setting('lock_timeout')") == '7s'
        await releasing
        await holder.close()
        await waiter.close()
        assert isinstance(outcome, asyncio.CancelledError), repr(outcome)

    registry_database(scenario)


# --- another database's locks are not this one's -----------------------------------------------------


def test_locks_in_another_database_are_ignored(registry_database):
    async def scenario(database):
        name = 'fr_other_' + uuid.uuid4().hex[:8]
        admin = await asyncpg.connect(DSN)
        await admin.execute(f'CREATE DATABASE {name}')
        other_dsn = DSN.rsplit('/', 1)[0] + '/' + name
        holder = await asyncpg.connect(other_dsn)
        queued = await asyncpg.connect(other_dsn)
        waiting = None
        try:
            async with holder.transaction():
                now = await holder.fetchval('SELECT extract(epoch FROM clock_timestamp())::float8')
                await holder.execute(
                    f"SET LOCAL application_name = '{field_registry.MAP_SWAP_APPLICATION} {math.ceil(now + 60)}'",
                )
                await holder.execute('SELECT pg_advisory_xact_lock(20261016, 1)')
                waiting = asyncio.create_task(queued.execute('SELECT pg_advisory_xact_lock(20261016, 1)'))
                await asyncio.sleep(0.5)
                async with database.pool.acquire() as connection:
                    assert await field_registry.map_swap_holder(connection, 1) is None
                    assert await connection.fetchval(map_refit._GATE_WAITERS, 1) is False
        finally:
            if waiting is not None:
                await waiting
            await holder.close()
            await queued.close()
            await admin.execute(f'DROP DATABASE {name}')
            await admin.close()

    registry_database(scenario)


# --- a coordinate upload's 409 does not need a second pooled connection ------------------------------


def test_a_coordinate_upload_refused_on_a_lock_needs_no_second_connection(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(database):
        ids = await seed(database, n=20, dim=4)
        schema = await database.fetchval('SELECT current_schema()')
        single = await asyncpg.create_pool(
            DSN,
            min_size=1,
            max_size=1,
            server_settings={'search_path': f'{schema},public'},
        )
        monkeypatch.setattr(embeddings, 'db', SimpleNamespace(pool=single))
        fence = await connect(database)
        try:
            async with fence.transaction():
                await fence.execute('SELECT pg_advisory_xact_lock(20261020, 1)')  # a full re-rank's fence
                with pytest.raises(HTTPException) as refused:
                    await asyncio.wait_for(
                        embeddings.upload_coordinates(
                            1,
                            FIELD,
                            [embeddings.CoordinateUpdate(sample_id=ids[0], x=5.0, y=5.0)],
                            ADMIN,
                        ),
                        15,
                    )
            assert refused.value.status_code == 409 and (refused.value.headers or {}).get('Retry-After') == '5'
        finally:
            await fence.close()
            await single.close()

    registry_database(scenario)
