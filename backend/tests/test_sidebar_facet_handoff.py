"""Facet slots: a field that ends hands its slot to this worker's next
field unless another worker waits, a number field's scan reads each row only once,
and a stream line that cannot be written still answers its field.
"""

import asyncio
import time

from backend.routers import datasets
from backend.tests.test_field_registry_postgres import registry_database  # noqa: F401
from backend.tests.test_sidebar_facet_r3 import seed_nan, worker_pool
from backend.tests.test_sidebar_facet_stream import allow_access, lines_of, stream_post
from backend.tests.test_sidebar_on_open import SELECTIVE, seed


async def two(connection):
    return 2


def counting_takes(slots: datasets._DatabaseSlots) -> list:
    """The keys `slots` won from the database, in order."""
    won: list = []
    take = slots.take

    async def counted(*args):
        key = await take(*args)
        if key is not None:
            won.append(key)
        return key

    slots.take = counted  # type: ignore[method-assign]
    return won


async def wanted_locks(connection) -> int:
    return await connection.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = $1::int::oid "
        "AND objid = $2::int::oid AND objsubid = 2",
        datasets._FACET_SLOT_LOCK,
        datasets._FACET_SLOT_WANTED,
    )


def test_a_worker_alone_keeps_its_slots_for_its_next_fields(registry_database, monkeypatch):  # noqa: F811
    """Regression check: every field gave its slot back and waited up to 0.1 s to ask again, even with no
    other worker waiting: 2-2.5x slower facets for one user. Two requests still take turns."""

    async def scenario(database):
        monkeypatch.setattr(datasets, '_database_slot_count', two)
        database_slots = datasets._DatabaseSlots(database.pool)
        won = counting_takes(database_slots)
        slots = datasets._FacetSlots(4, database_slots)
        starts: list = []
        state = {'running': 0, 'peak': 0}

        async def field(owner):
            async with slots.slot(owner):
                starts.append(owner)
                state['running'] += 1
                state['peak'] = max(state['peak'], state['running'])
                await asyncio.sleep(0.02)
                state['running'] -= 1

        started = time.monotonic()
        try:
            await asyncio.wait_for(asyncio.gather(*[field(owner) for owner in 'AB' * 15]), 30)
            elapsed = time.monotonic() - started
            assert len(won) == 2  # one key per slot, each kept from field to field
            assert state['peak'] == 2
            assert elapsed < 30 * 0.02 / 2 + 0.25  # 15 rounds of 0.02 s, without a pause per field
            for n in range(1, len(starts) + 1):  # the requests still alternate
                assert abs(starts[:n].count('A') - starts[:n].count('B')) <= 1
            assert slots.busy == 0 and not slots.turns
            await asyncio.sleep(0.05)  # the dispatcher stops saying it waits
            assert not database_slots.held and not database_slots.wanting and database_slots.connection is None
        finally:
            await database_slots._drop()  # a failed check must not leave a connection out of its pool
        assert database.pool.get_idle_size() == database.pool.get_size()
        async with database.pool.acquire() as connection:
            assert await wanted_locks(connection) == 0

    registry_database(scenario)


def test_a_waiting_worker_makes_the_others_give_their_slots_back(registry_database, monkeypatch):  # noqa: F811
    """Fairness across workers is kept: a worker whose fields wait says so, and the worker holding every
    slot gives one back after its next field instead of keeping it."""

    async def scenario(database):
        monkeypatch.setattr(datasets, '_database_slot_count', two)
        other = await worker_pool()
        first_slots, second_slots = datasets._DatabaseSlots(database.pool), datasets._DatabaseSlots(other)
        try:
            spare, key = await first_slots.take(), await first_slots.take()
            assert key is not None and spare is not None
            assert await first_slots.keep(key)
            await first_slots.want(True)
            assert await first_slots.keep(key)  # this worker's own waiting fields do not count
            await second_slots.want(True)
            assert not await first_slots.keep(key)  # both slots, where its share is one
            assert await first_slots.keep(spare)  # ... and kept the other
            await second_slots.want(False)
            assert await first_slots.keep(spare) and second_slots.connection is None
            await first_slots.want(False)
            await first_slots.give(spare)
            assert first_slots.connection is None

            big = datasets._FacetSlots(4, first_slots)
            small = datasets._FacetSlots(4, second_slots)
            done: dict = {}

            async def field(worker, owner, hold):
                async with worker.slot(owner):
                    await asyncio.sleep(hold)
                done.setdefault(owner, []).append(time.monotonic())

            fields = [asyncio.ensure_future(field(big, 'big', 0.1)) for _ in range(30)]
            await asyncio.sleep(0.05)  # the big request holds both slots
            asked = time.monotonic()
            await asyncio.wait_for(asyncio.gather(*[field(small, 'small', 0.1) for _ in range(2)]), 30)
            # 30 fields of 0.1 s on 2 slots take 1.5 s; the small request is not left for the end.
            assert done['small'][-1] - asked < 1.0
            assert len(done.get('big', [])) < 25
            await asyncio.wait_for(asyncio.gather(*fields), 30)
            await asyncio.sleep(0.05)
            assert not first_slots.held and not second_slots.held
            assert not first_slots.wanting and not second_slots.wanting
            async with database.pool.acquire() as connection:
                assert await wanted_locks(connection) == 0
        finally:
            await first_slots._drop()  # a failed check must not leave a connection out of its pool
            await second_slots._drop()
            await other.close()

    registry_database(scenario)


def test_a_line_that_cannot_be_written_still_answers_its_field(registry_database, monkeypatch):  # noqa: F811
    """Regression check: a field was marked answered before its line was written, so a line that failed to
    serialize left the field with no line at all in a stream that still ended `done`."""

    async def scenario(database):
        await seed(database, monkeypatch)
        allow_access(monkeypatch)

        async def unwritable_first(id, filters, lightning_mode, fields, *, known_broad=None, emit=None, report=None):
            # A non-finite number outside min/max: JSON has no NaN.
            emit({'name': 'score', 'type': 'number', 'min': 18, 'max': 20, 'mean': float('nan'), 'provenance': 'exact'})
            emit({'name': 'brightness', 'type': 'number', 'min': 1, 'max': 2, 'provenance': 'exact'})

        monkeypatch.setattr(datasets, '_get_dataset_field_stats_impl', unwritable_first)
        body = {'filters': SELECTIVE, 'lightning_mode': True, 'fields': ['score', 'brightness'], 'stream': True}
        status, _, chunks = await stream_post(body)
        entries = [entry for _, entry in lines_of(chunks)]
        assert status == 200 and entries[-1] == {'done': True}
        answered = {entry['name']: entry for entry in entries[:-1]}
        assert sorted(answered) == ['brightness', 'score']
        assert answered['score']['error']

    registry_database(scenario)


def recording_fetchrow(database, monkeypatch) -> list:
    queries: list = []
    fetchrow = database.fetchrow

    async def recorded(query, *args):
        queries.append(query)
        return await fetchrow(query, *args)

    monkeypatch.setattr(database, 'fetchrow', recorded)
    return queries


def test_finite_number_bounds_take_one_plain_scan(registry_database, monkeypatch):  # noqa: F811
    """The guards against 'NaN'/'Infinity' text read the field from each
    row's metadata twice more in every number field's filtered scan, about 1.7x the time per field.
    They now run only when the plain bounds are not finite."""

    async def scenario(database):
        await seed(database, monkeypatch)
        queries = recording_fetchrow(database, monkeypatch)
        assert await datasets.compute_filtered_minmax(1, 'brightness', SELECTIVE) == {'min': 180, 'max': 200}
        assert len(queries) == 1 and 'Infinity' not in queries[0]
        assert queries[0].count("metadata->>'brightness'") == 3

    registry_database(scenario)


def test_non_finite_number_text_still_leaves_finite_bounds(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed_nan(database)
        queries = recording_fetchrow(database, monkeypatch)
        stage = {'logic': 'AND', 'filters': [{'field': 'level', 'operator': '>=', 'value': 5}]}
        assert await datasets.compute_filtered_minmax(1, 'score', stage) == {'min': 2.5, 'max': 20.0}
        assert len(queries) == 2 and 'Infinity' in queries[1]

    registry_database(scenario)
