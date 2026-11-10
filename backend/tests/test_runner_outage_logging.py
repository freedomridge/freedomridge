"""Persistent infrastructure failures change state instead of flooding the shared log."""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

from backend import (
    auth_cleanup,
    compute_launcher,
    concept_jobs,
    dataset_schedules,
    duplicate_jobs,
    follower_search,
    follower_sync,
    index_jobs,
    job_queue,
    job_runner,
    job_service,
    map_placement,
    map_refit,
    statistics,
)


def test_failure_backoff_logs_transitions_and_resets(caplog):
    from backend.job_tasks import FailureBackoff

    caplog.set_level(logging.INFO)
    retry = FailureBackoff(logging.getLogger('outage-test'), 'Test operation', 1)
    delays = []
    for failure in [ConnectionError] * 7 + [OSError] * 2:
        try:
            raise failure('outage')
        except Exception as error:
            delays.append(retry.failed(error))
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30, 30]
    retry.recovered()
    retry.recovered()
    assert len(caplog.records) == 3
    assert sum(record.exc_info is not None for record in caplog.records) == 1
    assert 'failure changed' in caplog.records[1].message
    assert 'recovered' in caplog.records[2].message
    try:
        raise ConnectionError('new outage')
    except Exception as error:
        assert retry.failed(error) == 1
    assert sum(record.exc_info is not None for record in caplog.records) == 2


def test_failed_claims_back_off_and_recovery_restores_idle_polling(monkeypatch, caplog):
    async def scenario():
        caplog.set_level(logging.INFO)
        outcomes: Iterator[Exception | None] = iter([ConnectionError('outage')] * 7 + [None, ConnectionError(), None])
        delays: list[float] = []
        worker = job_runner.ServerWorker.__new__(job_runner.ServerWorker)
        worker.worker_id = 'outage-test'
        worker.poll_seconds = 1
        worker.stopping = asyncio.Event()

        async def run_once():
            outcome = next(outcomes)
            if outcome is not None:
                raise outcome
            if len(delays) == 9:
                worker.stop()
            return False

        async def wait_for(awaitable, timeout):
            awaitable.close()
            delays.append(timeout)
            raise asyncio.TimeoutError

        monkeypatch.setattr(worker, 'run_once', run_once)
        monkeypatch.setattr(asyncio, 'wait_for', wait_for)
        await worker._run_loop()
        assert delays == [1, 2, 4, 8, 16, 30, 30, 1, 1, 1]
        assert sum(record.exc_info is not None for record in caplog.records) == 2
        assert sum('recovered' in record.message for record in caplog.records) == 2

    asyncio.run(scenario())


def test_service_logs_migration_and_outage_transitions_only(monkeypatch, caplog):
    async def scenario():
        caplog.set_level(logging.INFO)
        readiness: Iterator[Exception | bool] = iter([ConnectionError(), ConnectionError(), False, False, False])
        scheduling: Iterator[Exception | None] = iter(
            [ConnectionError(), ConnectionError(), None, ConnectionError(), None],
        )
        ready_delays: list[float] = []
        scheduled_at: list[float] = []
        callbacks: dict[int, Callable[[], None]] = {}
        runner_stopped = asyncio.Event()
        original_wait_for = asyncio.wait_for
        original_wait = asyncio.wait
        loop = asyncio.get_running_loop()
        elapsed = 0.0
        original_time = loop.time

        async def fetchval(*args, **kwargs):
            if 'restored_work_pending' in args[0]:
                return False
            result = next(readiness, True)
            if isinstance(result, Exception):
                raise result
            return result

        async def run():
            await runner_stopped.wait()

        @asynccontextmanager
        async def local_runner(*args, **kwargs):
            yield SimpleNamespace(
                workers=[SimpleNamespace(pool=SimpleNamespace(fetchval=fetchval))],
                run=run,
                stop=runner_stopped.set,
            )

        async def schedule(*args, **kwargs):
            scheduled_at.append(elapsed)
            result = next(scheduling)
            if result is not None:
                raise result
            if len(scheduled_at) == 5:
                next(iter(callbacks.values()))()

        async def wait_for(awaitable, timeout):
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
                ready_delays.append(timeout)
                raise asyncio.TimeoutError
            return await original_wait_for(awaitable, timeout)

        async def wait(tasks, *, timeout, return_when):
            nonlocal elapsed
            elapsed += timeout
            return await original_wait(tasks, timeout=0, return_when=return_when)

        async def schedule_statistics(*args, **kwargs):
            return []

        async def dispatch_concepts(*args, **kwargs):
            return 0

        monkeypatch.setattr(loop, 'add_signal_handler', lambda signum, callback: callbacks.update({signum: callback}))
        monkeypatch.setattr(loop, 'remove_signal_handler', lambda signum: None)
        monkeypatch.setattr(job_service, 'local_runner', local_runner)
        monkeypatch.setattr(job_queue, 'check_transaction_high_water', AsyncMock(return_value=False))
        monkeypatch.setattr(job_service, 'schedule_cleanup', schedule)
        monkeypatch.setattr(statistics, 'schedule_statistics', schedule_statistics)
        monkeypatch.setattr(concept_jobs, 'schedule_concepts', schedule_statistics)
        monkeypatch.setattr(concept_jobs, 'dispatch_changes', dispatch_concepts)
        monkeypatch.setattr(duplicate_jobs, 'schedule_duplicates', schedule_statistics)
        monkeypatch.setattr(index_jobs, 'sweep_invalid_indexes', schedule_statistics)
        monkeypatch.setattr(dataset_schedules, 'schedule_dataset_jobs', schedule_statistics)
        monkeypatch.setattr(map_refit, 'sweep', schedule_statistics)
        monkeypatch.setattr(map_refit, 'heal_unranked_maps', schedule_statistics)
        monkeypatch.setattr(map_refit, 'expire_unclaimed_fits', schedule_statistics)
        monkeypatch.setattr(map_placement, 'schedule_placements', schedule_statistics)
        monkeypatch.setattr(follower_sync, 'schedule_sync', schedule_statistics)
        monkeypatch.setattr(follower_search, 'serve', schedule_statistics)
        monkeypatch.setattr(compute_launcher, 'schedule', schedule_statistics)
        monkeypatch.setattr(auth_cleanup, 'scheduled_cleanup', schedule_statistics)
        monkeypatch.setenv('JOB_STATISTICS_SECONDS', '1000000')
        monkeypatch.setenv('JOB_CONCEPT_SECONDS', '1000000')
        monkeypatch.setattr(loop, 'time', lambda: original_time() + elapsed)
        monkeypatch.setattr(asyncio, 'wait_for', wait_for)
        monkeypatch.setattr(asyncio, 'wait', wait)
        await job_service.run_service('unused')
        assert ready_delays == [5, 10, 5, 5, 5]
        assert [round(when) for when in scheduled_at] == [0, 5, 15, 20, 25]
        assert sum('Waiting for the database to record' in record.message for record in caplog.records) == 1
        assert sum('Runner migrations are ready' in record.message for record in caplog.records) == 1
        assert sum(record.exc_info is not None for record in caplog.records) == 3
        assert sum('recovered' in record.message for record in caplog.records) == 3

    asyncio.run(scenario())
