"""Facet slots: the database's slots are shared per waiting request, whichever
worker serves it; a field's slot check reads pg_locks only when another worker waits; and
turning parallel query off does not leave the sidebar one scan for everyone. Also: no slot is
given back twice, a worker at its capacity keeps its share, and
slots a stalled waiter cannot use are not left idle.
"""

import asyncio
import collections
import math
import os
import time

import asyncpg

from backend.routers import datasets
from backend.tests.test_field_registry_postgres import registry_database  # noqa: F401
from backend.tests.test_sidebar_facet_r3 import worker_pool


async def four(connection):
    return 4


async def eight(connection):
    return 8


async def facet_locks(connection) -> int:
    """Every slot and "wanted" lock of the facet slots, in any session."""
    return await connection.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = $1::int::oid",
        datasets._FACET_SLOT_LOCK,
    )


async def assert_all_given_back(database, *slots):
    await asyncio.sleep(0.2)  # the dispatchers stop saying that they wait
    for each in slots:
        assert not each.held and not each.wanting and each.connection is None
    async with database.pool.acquire() as connection:
        assert await facet_locks(connection) == 0


def test_waiting_requests_share_the_slots_whatever_their_worker(registry_database, monkeypatch):  # noqa: F811
    """regression: the "wanted" lock was one per worker, so the slots were shared per worker: a request
    alone in its worker ran about as many fields as three requests of another worker together (2-2.8x
    the fields of each). Each waiting request now gets the same share, whichever worker serves it."""

    async def scenario(database):
        monkeypatch.setattr(datasets, '_database_slot_count', four)
        other = await worker_pool()
        crowded_slots, alone_slots = datasets._DatabaseSlots(database.pool), datasets._DatabaseSlots(other)
        crowded, alone = datasets._FacetSlots(8, crowded_slots), datasets._FacetSlots(8, alone_slots)
        started = dict.fromkeys(['a1', 'a2', 'a3', 'b'], 0)
        state = {'counting': False}

        async def field(worker, owner):
            async with worker.slot(owner):
                if state['counting']:
                    started[owner] += 1
                await asyncio.sleep(0.1)

        requests = [(crowded, 'a1'), (crowded, 'a2'), (crowded, 'a3'), (alone, 'b')]
        fields = [asyncio.ensure_future(field(worker, owner)) for _ in range(100) for worker, owner in requests]
        try:
            await asyncio.sleep(0.5)  # the shares settle
            state['counting'] = True
            await asyncio.sleep(2.0)
            state['counting'] = False
            # 4 slots for 4 requests: one each, about 20 fields of 0.1 s each in 2 s.
            assert max(started.values()) <= 1.3 * min(started.values()), started
            assert sum(started.values()) >= 0.6 * 4 * 2.0 / 0.1, started  # and the slots stay busy
        finally:
            for each in fields:
                each.cancel()
            await asyncio.gather(*fields, return_exceptions=True)
        try:
            await assert_all_given_back(database, crowded_slots, alone_slots)
        finally:
            await crowded_slots._drop()  # a failed check must not leave a connection out of its pool
            await alone_slots._drop()
            await other.close()

    registry_database(scenario)


def test_a_small_request_runs_at_its_share_beside_a_big_one_in_another_worker(
    registry_database,  # noqa: F811
    monkeypatch,
):
    """regression (fair.py): a small request asked while a big one in another worker holds every slot
    waits for one of the big request's fields, then runs on its half of the slots without giving them
    back after each field. Before, every field end of either request gave its slot back to a race, so
    the small request paid a pause per field and lost about half of the races (1.4-2.3 s here)."""

    async def scenario(database):
        monkeypatch.setattr(datasets, '_database_slot_count', four)
        other = await worker_pool()
        big_slots, small_slots = datasets._DatabaseSlots(database.pool), datasets._DatabaseSlots(other)
        big, small = datasets._FacetSlots(8, big_slots), datasets._FacetSlots(8, small_slots)
        hold, count = 0.05, 40

        async def field(worker, owner):
            async with worker.slot(owner):
                await asyncio.sleep(hold)

        fields = [asyncio.ensure_future(field(big, 'big')) for _ in range(1000)]
        try:
            await asyncio.sleep(1.5 * hold)  # the big request holds every slot
            asked = time.monotonic()
            await asyncio.wait_for(asyncio.gather(*[field(small, 'small') for _ in range(count)]), 30)
            took = time.monotonic() - asked
            share = 4 // 2
            ideal = hold + math.ceil(count / share) * hold  # one big field, then its half of the slots
            assert took < ideal + 0.3, (took, ideal)
            assert sum(each.done() for each in fields) < 1000 / 4  # long before the big request ends
        finally:
            for each in fields:
                each.cancel()
            await asyncio.gather(*fields, return_exceptions=True)
        try:
            await assert_all_given_back(database, big_slots, small_slots)
        finally:
            await big_slots._drop()
            await small_slots._drop()
            await other.close()

    registry_database(scenario)


class RecordingPool:
    """A pool whose connections record every statement they run."""

    def __init__(self, pool):
        self.pool = pool
        self.statements: list = []

    async def acquire(self):
        return Recording(await self.pool.acquire(), self.statements)

    async def release(self, connection):
        await self.pool.release(connection.connection)


class Recording:
    def __init__(self, connection, statements):
        self.connection = connection
        self.statements = statements

    def __getattr__(self, name):
        attribute = getattr(self.connection, name)
        if name not in ('execute', 'fetch', 'fetchrow', 'fetchval'):
            return attribute

        async def recorded(query, *args):
            self.statements.append(query)
            return await attribute(query, *args)

        return recorded


def test_a_field_reads_pg_locks_only_when_another_worker_waits(registry_database, monkeypatch):  # noqa: F811
    """regression: after every field, keep() read pg_locks, which materializes every lock of the
    server (0.15 ms idle, 7-10 ms with 12,000 locks held). An O(1) lock probe now answers while nobody
    else waits, and pg_locks is read only when somebody does."""

    async def scenario(database):
        monkeypatch.setattr(datasets, '_database_slot_count', four)
        recording = RecordingPool(database.pool)
        slots = datasets._DatabaseSlots(recording)
        worker = datasets._FacetSlots(8, slots)
        other = await worker_pool()
        waiting = datasets._DatabaseSlots(other)
        kept = []
        keep = slots.keep

        async def counted(key, *args):
            kept.append(await keep(key, *args))
            return kept[-1]

        slots.keep = counted  # type: ignore[method-assign]

        async def field():
            async with worker.slot('one'):
                await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(asyncio.gather(*[field() for _ in range(40)]), 30)
            assert len(kept) >= 30 and all(kept)  # the fields handed their slots on
            assert not [statement for statement in recording.statements if 'pg_locks' in statement]

            await waiting.want(1)  # another worker's request waits
            await asyncio.wait_for(asyncio.gather(*[field() for _ in range(8)]), 30)
            assert [statement for statement in recording.statements if 'pg_locks' in statement]
            await waiting.want(0)
            await assert_all_given_back(database, slots, waiting)
        finally:
            await slots._drop()
            await waiting._drop()
            await other.close()

    registry_database(scenario)


def test_parallel_query_off_does_not_leave_the_sidebar_one_scan(registry_database):  # noqa: F811
    """The slot count was max_parallel_workers, so turning parallel query off (0) left one facet
    scan at a time for every user. It is then max_worker_processes, the server's pool that parallel
    workers come from. A configured count is still the count, up to that pool, as the server applies it
    (64 parallel workers on 8 processes once made K 64)."""

    async def scenario(database):
        dsn = os.environ['FRIDGE_TEST_POSTGRES_DSN']
        for configured in ('0', '3', '64'):
            # A setting given at connection is the session's reset value, as the server's would be.
            connection = await asyncpg.connect(dsn, server_settings={'max_parallel_workers': configured})
            try:
                processes = int(await connection.fetchval('SHOW max_worker_processes'))
                expected = min(int(configured), processes) or processes
                assert processes < 64  # the test server's pool, so that 64 is capped
                assert await datasets._database_slot_count(connection) == max(1, expected)
            finally:
                await connection.close()

    registry_database(scenario)


def test_running_fields_never_share_a_slot_with_fractional_shares(registry_database, monkeypatch):  # noqa: F811
    """keep() gave an over-share slot back itself and _hand_on then gave the
    same key number back again; when _run's take() had won that number back meanwhile, the second
    give-back freed the slot of a running field, and the database ran K+1 or K+2 scans. Integer shares
    hid it (take() refuses the re-won key); fractional ones (three requests on 4 or 8 slots) do not."""

    async def scenario(database):
        others = [await worker_pool(), await worker_pool()]
        try:
            for k, layout, hold in ((4, (3, 3, 3), 0.05), (8, (1, 1, 1), 0.1)):

                async def slot_count(connection, k=k):
                    return k

                monkeypatch.setattr(datasets, '_database_slot_count', slot_count)
                db_slots = [datasets._DatabaseSlots(pool) for pool in [database.pool, *others]]
                workers = [datasets._FacetSlots(12, each) for each in db_slots]
                keys: dict = {}  # field task -> the slot key it was granted
                running: collections.Counter = collections.Counter()
                seen = {'peak': 0, 'shared': 0, 'fields': 0}

                def record_keys(worker):
                    acquire = worker._acquire

                    async def recorded(owner):
                        keys[asyncio.current_task()] = await acquire(owner)
                        return keys[asyncio.current_task()]

                    worker._acquire = recorded

                for worker in workers:
                    record_keys(worker)

                async def field(worker, owner):
                    async with worker.slot(owner):
                        key = keys[asyncio.current_task()]
                        running[key] += 1
                        seen['fields'] += 1
                        seen['shared'] += running[key] > 1
                        seen['peak'] = max(seen['peak'], sum(running.values()))
                        try:
                            await asyncio.sleep(hold)
                        finally:
                            running[key] -= 1

                fields = [
                    asyncio.ensure_future(field(worker, f'{index}-{request}'))
                    for _ in range(200)
                    for index, worker in enumerate(workers)
                    for request in range(layout[index])
                ]
                try:
                    await asyncio.sleep(3.0)
                finally:
                    for each in fields:
                        each.cancel()
                    await asyncio.gather(*fields, return_exceptions=True)
                    await asyncio.sleep(0.2)
                    for slot in db_slots:
                        await slot._drop()
                assert seen['fields'] > 2 * k / hold, (k, layout, seen)  # the slots stayed busy
                assert seen['shared'] == 0 and seen['peak'] <= k, (k, layout, seen)
        finally:
            for pool in others:
                await pool.close()

    registry_database(scenario)


def counting_give_backs(slots: datasets._DatabaseSlots, state: dict) -> list:
    """The slots `slots` gave back from keep() while state['counting']."""
    given: list = []
    keep = slots.keep

    async def counted(key, *args):
        kept = await keep(key, *args)
        if not kept and state['counting']:
            given.append(key)
        return kept

    slots.keep = counted  # type: ignore[method-assign]
    return given


def test_a_worker_at_its_capacity_keeps_its_share(registry_database, monkeypatch):  # noqa: F811
    """At its local capacity (a quarter of its pool) a worker holds no "wanted" lock,
    so that the others do not give it slots it cannot use, but its own share then counted one request
    for all it serves: it gave back slots it was owed and took them back, over and over (K = 4, capacity
    3: 2.2 slots in use and 21 give-backs in 3 s). Its share counts every waiting request it serves."""

    async def scenario(database):
        monkeypatch.setattr(datasets, '_database_slot_count', four)
        other = await worker_pool()
        crowded_slots, alone_slots = datasets._DatabaseSlots(database.pool), datasets._DatabaseSlots(other)
        crowded, alone = datasets._FacetSlots(3, crowded_slots), datasets._FacetSlots(8, alone_slots)
        started = dict.fromkeys(['a1', 'a2', 'a3', 'b'], 0)
        state = {'counting': False}
        given = counting_give_backs(crowded_slots, state)

        async def field(worker, owner):
            async with worker.slot(owner):
                if state['counting']:
                    started[owner] += 1
                await asyncio.sleep(0.1)

        requests = [(crowded, 'a1'), (crowded, 'a2'), (crowded, 'a3'), (alone, 'b')]
        fields = [asyncio.ensure_future(field(worker, owner)) for _ in range(100) for worker, owner in requests]
        try:
            await asyncio.sleep(0.5)  # the shares settle: 3 slots for the 3 requests at capacity, 1 for b
            state['counting'] = True
            await asyncio.sleep(3.0)
            state['counting'] = False
            in_use = sum(started[owner] for owner in ('a1', 'a2', 'a3')) * 0.1 / 3.0
            assert in_use >= 2.6 and len(given) <= 3, (in_use, len(given), started)
        finally:
            for each in fields:
                each.cancel()
            await asyncio.gather(*fields, return_exceptions=True)
        try:
            await assert_all_given_back(database, crowded_slots, alone_slots)
        finally:
            await crowded_slots._drop()
            await alone_slots._drop()
            await other.close()

    registry_database(scenario)


def test_slots_a_stalled_waiter_cannot_use_are_not_left_idle(registry_database, monkeypatch):  # noqa: F811
    """A worker that says its requests wait but does not ask for slots (its event
    loop stalled) capped every other worker at its share, leaving the rest idle: with a stalled waiter of
    3 requests on K = 8, one request ran on 2 slots. Slots left unclaimed beyond the share are taken."""

    async def scenario(database):
        monkeypatch.setattr(datasets, '_database_slot_count', eight)
        other = await worker_pool()
        active_slots, stalled = datasets._DatabaseSlots(database.pool), datasets._DatabaseSlots(other)
        active = datasets._FacetSlots(12, active_slots)
        state = {'running': 0}
        samples: list = []

        async def field():
            async with active.slot('a'):
                state['running'] += 1
                try:
                    await asyncio.sleep(0.5)
                finally:
                    state['running'] -= 1

        await stalled.want(3)  # says that 3 requests wait, and never asks for a slot
        fields = [asyncio.ensure_future(field()) for _ in range(200)]
        try:
            await asyncio.sleep(0.5)
            for _ in range(300):  # 3 s
                samples.append(state['running'])
                await asyncio.sleep(0.01)
            # Its share is 8 x 1 / (1 + 3) = 2; the other 6 slots wait for nobody who can use them.
            assert max(samples) <= 8 and sum(samples) / len(samples) >= 4, (max(samples), sum(samples) / len(samples))
        finally:
            for each in fields:
                each.cancel()
            await asyncio.gather(*fields, return_exceptions=True)
        await stalled.want(0)
        try:
            await assert_all_given_back(database, active_slots, stalled)
        finally:
            await active_slots._drop()
            await stalled._drop()
            await other.close()

    registry_database(scenario)


def test_a_field_granted_before_a_lost_connection_does_not_pass_its_slot_on(
    registry_database,  # noqa: F811
    monkeypatch,
):
    """After the slot connection was lost, _run won the same key numbers
    again on a new connection; a field granted before the loss then ended, keep() found its number held
    (the new grant) and handed it on while the new field also ran on it, and both lineages went on
    handing it on: one worker alone ran over K for the rest of its requests. A grant now carries the
    connection it was won on, and a field from a lost one neither hands its number on nor gives it back."""

    async def scenario(database):
        monkeypatch.setattr(datasets, '_database_slot_count', four)
        slots = datasets._DatabaseSlots(database.pool)
        worker = datasets._FacetSlots(8, slots)
        keys: dict = {}  # field task -> the slot key it was granted
        running: collections.Counter = collections.Counter()
        seen = {'counting': False, 'peak': 0, 'shared': 0, 'fields': 0}
        acquire = worker._acquire

        async def recorded(owner):
            keys[asyncio.current_task()] = await acquire(owner)
            return keys[asyncio.current_task()]

        worker._acquire = recorded  # type: ignore[method-assign]

        async def field(owner, hold):
            async with worker.slot(owner):
                key = keys[asyncio.current_task()]
                running[key] += 1
                if seen['counting']:
                    seen['fields'] += 1
                    seen['shared'] += running[key] > 1
                    seen['peak'] = max(seen['peak'], sum(running.values()))
                try:
                    await asyncio.sleep(hold)
                finally:
                    running[key] -= 1

        # Staggered fields, so that some granted before the loss are still running when the same numbers
        # are won again, and end one by one with fields still waiting.
        fields = [
            asyncio.ensure_future(field(owner, 0.1 + 0.1 * (index % 3)))
            for index in range(40)
            for owner in ('a', 'b', 'c')
        ]
        try:
            await asyncio.sleep(0.6)
            async with database.pool.acquire() as connection:
                await connection.execute('SELECT pg_terminate_backend($1)', slots.connection.get_server_pid())
            await asyncio.sleep(0.5)  # the fields granted before the loss have ended (0.3 s at most)
            seen['counting'] = True
            await asyncio.sleep(2.0)
            seen['counting'] = False
            assert seen['fields'] > 2 * 4 / 0.3, seen  # the slots stayed busy after the reconnection
            assert seen['shared'] == 0 and seen['peak'] <= 4, seen
        finally:
            for each in fields:
                each.cancel()
            await asyncio.gather(*fields, return_exceptions=True)
        try:
            await assert_all_given_back(database, slots)
        finally:
            await slots._drop()

    registry_database(scenario)


def test_a_bare_slot_number_is_not_a_grant(registry_database, monkeypatch):  # noqa: F811
    """A bare number could stand for an old grant of it, so keep() and give() refuse it
    (treating it as the current grant would reopen the double give-back without a test failing)."""

    async def scenario(database):
        monkeypatch.setattr(datasets, '_database_slot_count', four)
        slots = datasets._DatabaseSlots(database.pool)
        try:
            grant = await slots.take()
            assert grant is not None and grant == 0 and not await slots.keep(0)
            await slots.give(0)
            assert slots.held == {0: grant.serial}  # neither gave the slot back
            assert await slots.keep(grant)
            await slots.give(grant)
            assert not slots.held and slots.connection is None
        finally:
            await slots._drop()

    registry_database(scenario)
