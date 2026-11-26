"""The job runner queues the bundled region loads when it starts (docs/REGIONS.md, "Loading a set"): once the
migration ledger is ready, again after a failure, and not again once it has succeeded. No database: the runner,
its pool and every other schedule are stand-ins, and the loop's clock is driven by the test."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    from collections.abc import Callable

from backend import (
    assistant_calls,
    auth_cleanup,
    compute_launcher,
    concept_jobs,
    dataset_deletion,
    dataset_schedules,
    duplicate_jobs,
    follower_search,
    follower_sync,
    index_jobs,
    job_queue,
    job_service,
    map_placement,
    map_refit,
    regions,
    statistics,
)


def test_the_runner_queues_the_bundled_region_loads_once_when_it_starts(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        original_wait = asyncio.wait
        original_wait_for = asyncio.wait_for
        original_time = loop.time
        elapsed = 0.0
        ledger_reads = iter([False, True])  # pending migrations at first, then ready
        ledger_ready = False
        calls: list[tuple[float, bool]] = []
        outcomes = iter([ConnectionError('database unreachable'), []])
        stops: dict[int, Callable[[], None]] = {}
        runner_stopped = asyncio.Event()

        async def fetchval(sql, *args, **kwargs):
            nonlocal ledger_ready
            if 'restored_work_pending' in sql:
                return False
            ledger_ready = next(ledger_reads, True)
            return ledger_ready

        pool = SimpleNamespace(fetchval=fetchval)

        async def run():
            await runner_stopped.wait()

        @asynccontextmanager
        async def local_runner(*args, **kwargs):
            yield SimpleNamespace(workers=[SimpleNamespace(pool=pool)], run=run, stop=runner_stopped.set)

        async def queue_bundled_loads(given):
            calls.append((elapsed, ledger_ready and given is pool))
            outcome = next(outcomes, [])
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async def nothing(*args, **kwargs):
            return []

        async def wait_for(awaitable, timeout):
            if asyncio.iscoroutine(awaitable):  # the readiness pause: over at once
                awaitable.close()
                raise asyncio.TimeoutError
            return await original_wait_for(awaitable, timeout)

        async def wait(tasks, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
            nonlocal elapsed
            if return_when != asyncio.FIRST_COMPLETED:  # restore recovery's own pause, cancelled at shutdown
                return await original_wait(tasks, timeout=timeout, return_when=return_when)
            elapsed += timeout  # the scheduling loop's pause: the clock jumps to its next schedule
            if elapsed > 120:
                stops[next(iter(stops))]()
            return await original_wait(tasks, timeout=0, return_when=return_when)

        monkeypatch.setattr(loop, 'add_signal_handler', lambda signum, callback: stops.update({signum: callback}))
        monkeypatch.setattr(loop, 'remove_signal_handler', lambda signum: None)
        monkeypatch.setattr(loop, 'time', lambda: original_time() + elapsed)
        monkeypatch.setattr(asyncio, 'wait', wait)
        monkeypatch.setattr(asyncio, 'wait_for', wait_for)
        monkeypatch.setattr(job_service, 'local_runner', local_runner)
        monkeypatch.setattr(job_queue, 'check_transaction_high_water', AsyncMock(return_value=False))
        monkeypatch.setattr(regions, 'queue_bundled_loads', queue_bundled_loads)
        for module, name in [
            (job_service, 'schedule_cleanup'),
            (statistics, 'schedule_statistics'),
            (concept_jobs, 'schedule_concepts'),
            (concept_jobs, 'dispatch_changes'),
            (duplicate_jobs, 'schedule_duplicates'),
            (index_jobs, 'sweep_invalid_indexes'),
            (dataset_deletion, 'sweep_partitions'),
            (index_jobs, 'queue_rebuilds'),
            (dataset_schedules, 'schedule_dataset_jobs'),
            (map_refit, 'sweep'),
            (map_refit, 'heal_unranked_maps'),
            (map_refit, 'expire_unclaimed_fits'),
            (map_placement, 'schedule_placements'),
            (follower_sync, 'schedule_sync'),
            (follower_search, 'serve'),
            (compute_launcher, 'schedule'),
            (auth_cleanup, 'scheduled_cleanup'),
            (assistant_calls, 'prune'),
        ]:
            monkeypatch.setattr(module, name, nothing)
        await job_service.run_service('unused')
        return calls

    calls = asyncio.run(scenario())
    # At the start, with the runner's pool and only once the ledger is ready; after the failure, once more (5 s
    # later, the schedule's backoff); then never again, though the runner ran on for two minutes.
    assert [(round(when), right) for when, right in calls] == [(0, True), (5, True)]
