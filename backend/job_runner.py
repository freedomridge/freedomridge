"""Bounded local worker slots with independent leases and fenced publication."""

import asyncio
import logging
import math
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

import asyncpg

from . import job_queue
from .database import init_connection
from .job_heartbeat import (
    Connector,
    LeaseMonitor,
    LeaseSession,
    current_lease,
    heartbeat_budgets,
    heartbeat_connector,
    worker_heartbeat,
)
from .job_queue import JobPolicy
from .job_tasks import FailureBackoff, run_isolated

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    """One resumable unit; omitted checkpoints preserve previously committed work."""

    checkpoint: Optional[dict] = None
    done: Optional[int] = None
    total: Optional[int] = None
    message: Optional[str] = None
    result: Any = None
    finished: bool = False
    publish: Optional[Callable[[asyncpg.Connection], Awaitable[None]]] = None
    # The handler already ended the job in its own fenced transaction (job_queue.finish_locked).
    committed: bool = False


class UnitCancelled(Exception):
    """The durable cancellation signal interrupted measurement."""


class ServerWorker:
    """One execution slot; effects belong only in bounded publish callbacks."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        handlers: Mapping[str, Callable[[dict], Awaitable[StepResult]]],
        *,
        heartbeat_connect: Connector,
        policies: Mapping[str, JobPolicy] | None = None,
        lanes: tuple[str, ...] | None = None,
        placements: tuple[str, ...] = ('server',),
        worker_id: Optional[str] = None,
        lease_seconds: float = 60,
        heartbeat_seconds: float = 10,
        step_budget_seconds: float = 10,
        poll_seconds: float = 1,
        retry_seconds: float = 5,
    ):
        durations = (lease_seconds, heartbeat_seconds, step_budget_seconds, poll_seconds, retry_seconds)
        if any(not math.isfinite(duration) or duration <= 0 for duration in durations):
            raise ValueError('Worker durations must be finite and positive')
        if heartbeat_seconds * 2 >= lease_seconds:
            raise ValueError('Heartbeat interval must be less than half the lease duration')
        if heartbeat_seconds >= 30:
            raise ValueError('Heartbeat interval must be below 30 seconds for worker-health reporting')
        heartbeat_budgets(lease_seconds, heartbeat_seconds)
        if not handlers:
            raise ValueError('A worker must have at least one registered handler')
        if policies and set(policies) - set(handlers):
            raise ValueError('Policies must name registered handlers')
        self.pool = pool
        self.lanes = lanes
        self.placements = placements
        self.handlers = dict(handlers)
        self.policies = {kind: (policies or {}).get(kind, JobPolicy(retry_seconds=retry_seconds)) for kind in handlers}
        self.heartbeat_connect = heartbeat_connect
        self.worker_id = worker_id or f'{socket.gethostname()}:{uuid.uuid4()}'
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.step_budget_seconds = step_budget_seconds
        self.poll_seconds = poll_seconds
        self.stopping = asyncio.Event()
        self.lease_session: LeaseSession | None = None

    def stop(self):
        """Finish the current bounded unit, then leave its checkpoint queued."""
        self.stopping.set()

    async def heartbeat(self, current_job_id=None):
        await worker_heartbeat(self.pool, self.worker_id, self.handlers, current_job_id)

    async def _telemetry(self, current_job_id=None):
        try:
            await run_isolated(self.heartbeat(current_job_id), name='Worker telemetry')
        except Exception:
            logger.debug('Worker telemetry is unavailable; execution remains independent', exc_info=True)

    async def _measure(self, job, monitor):
        if monitor.lost.is_set():
            raise job_queue.LeaseLost(f"Lease lost for job {job['id']}")
        if monitor.cancelled.is_set():
            raise UnitCancelled()
        duration = self.policies[job['kind']].max_unit_seconds
        unit = monitor.begin_unit(duration)

        async def measure():
            try:
                return await self.handlers[job['kind']](job)
            finally:
                monitor.finish_unit(unit)

        measuring = asyncio.create_task(measure())
        expired = asyncio.create_task(monitor.lost.wait())
        cancelled = asyncio.create_task(monitor.cancelled.wait())
        timed_out = asyncio.create_task(unit.timed_out.wait())
        tasks = (measuring, expired, cancelled, timed_out)
        try:
            await asyncio.wait(tasks, timeout=duration, return_when=asyncio.FIRST_COMPLETED)
            if monitor.lost.is_set():
                raise job_queue.LeaseLost(f"Lease lost for job {job['id']}")
            if monitor.cancelled.is_set():
                raise UnitCancelled()
            if unit.completed_at is None or unit.completed_at >= unit.deadline:
                raise TimeoutError(f'Unit exceeded its {duration:g}s wall-time limit')
            if measuring.cancelled():
                raise RuntimeError('Handler cancelled its own measurement task')
            outcome = await measuring
            if not isinstance(outcome, StepResult):
                raise TypeError('A job handler must return StepResult')
            return outcome
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                if measuring.done():
                    monitor.finish_unit(unit)

    async def _record_end(self, monitor, record, name):
        """Record how an attempt ended. The lease heartbeat stops first: each renewal locks the job row too,
        and one whose commit waits on a slow disk outlasts the queue's lock timeout (the contended publication
        is then often the error being recorded). Any other short holder of the row is retried at the
        heartbeat's retry gap, but only while the lease as last renewed holds. Every attempt is fenced by the
        lease token, so a lost lease ends the retries; past the lease the job belongs to the expiry path."""
        await monitor.close()
        while True:
            try:
                return await run_isolated(record(), name=name)
            except Exception as error:
                if not job_queue.is_transient(error) or time.monotonic() + monitor.retry_gap >= monitor.renew_by:
                    raise
            await asyncio.sleep(monitor.retry_gap)

    async def _execute(self, job):
        monitor = LeaseMonitor(
            self.lease_session,
            job,
            self.worker_id,
            self.handlers,
            self.lease_seconds,
            self.heartbeat_seconds,
        )
        started = time.monotonic()
        lease = current_lease.set(monitor)  # the unit task inherits it: handler fences pause renewals too
        try:
            await monitor.start()
            while True:
                outcome = await self._measure(job, monitor)
                if outcome.committed:
                    return
                should_yield = self.stopping.is_set() or time.monotonic() - started >= self.step_budget_seconds
                # The job's own renewal must not hold its row when the publication's fence arrives.
                async with monitor.paused():
                    job = await run_isolated(
                        job_queue.publish_step(
                            self.pool,
                            job['id'],
                            job['lease_token'],
                            checkpoint=outcome.checkpoint,
                            done=outcome.done,
                            total=outcome.total,
                            message=outcome.message,
                            result=outcome.result,
                            finished=outcome.finished,
                            release=should_yield and not outcome.finished,
                            publisher=outcome.publish,
                        ),
                        name='Job publication',
                    )
                if job['state'] != 'running':
                    return
        except (UnitCancelled, job_queue.CancelRequested):
            try:
                await self._record_end(
                    monitor,
                    lambda: job_queue.publish_step(self.pool, job['id'], job['lease_token'], release=True),
                    'Job cancellation acknowledgement',
                )
            except job_queue.LeaseLost:
                # A handler's own fenced transaction finished the job just before the cancel arrived.
                logger.info('Job %s ended before its cancellation could be acknowledged', job['id'])
            except Exception as error:
                if not job_queue.is_transient(error):
                    raise
                # The expiry sweep cancels it: the request is durable.
                logger.warning(
                    'Job %s could not acknowledge its cancellation; its lease will expire',
                    job['id'],
                    exc_info=True,
                )
        except job_queue.LeaseLost:
            logger.warning('Worker %s stopped after losing job %s', self.worker_id, job['id'])
        except Exception as error:
            try:
                detail = {'type': type(error).__name__, 'message': str(error)}
                if job_queue.is_transient(error):
                    count_failure = not isinstance(error, job_queue.QueueContended)
                    await self._record_end(
                        monitor,
                        lambda: job_queue.defer_job(
                            self.pool,
                            job['id'],
                            job['lease_token'],
                            detail,
                            delay_seconds=self.policies[job['kind']].retry_seconds,
                            count_failure=count_failure,
                        ),
                        'Job deferral',
                    )
                else:
                    logger.exception('Job %s failed', job['id'])
                    await self._record_end(
                        monitor,
                        lambda: job_queue.fail_job(self.pool, job['id'], job['lease_token'], detail),
                        'Job failure recording',
                    )
            except Exception:
                logger.warning('Job %s could not record failure; its lease will expire', job['id'], exc_info=True)
        finally:
            current_lease.reset(lease)
            await monitor.close()

    async def run_once(self):
        """Run one fair time slice; telemetry never strands an already claimed job."""
        if self.lease_session is None:
            self.lease_session = LeaseSession(self.heartbeat_connect, self.worker_id)
            await self.lease_session.start()
            try:
                return await self._run_once()
            finally:
                await self.lease_session.close()
                self.lease_session = None
        return await self._run_once()

    async def _run_once(self):
        job = await run_isolated(
            job_queue.claim_job(
                self.pool,
                self.worker_id,
                placements=self.placements,
                kinds=sorted(self.handlers),
                policies=self.policies,
                lanes=self.lanes,
                lease_seconds=self.lease_seconds,
            ),
            name='Job claim',
        )
        if job is None:
            await self._telemetry()
            return False
        try:
            await self._execute(job)
        finally:
            await self._telemetry()
        return True

    async def run(self):
        self.lease_session = LeaseSession(self.heartbeat_connect, self.worker_id)
        await self.lease_session.start()
        try:
            await self._run_loop()
        finally:
            await self.lease_session.close()
            self.lease_session = None

    async def _run_loop(self):
        retries = FailureBackoff(logger, f'Worker {self.worker_id} iteration', self.poll_seconds)
        while not self.stopping.is_set():
            delay = self.poll_seconds
            try:
                worked = await self.run_once()
                retries.recovered()
                if worked:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as error:
                delay = retries.failed(error)
            try:
                await asyncio.wait_for(self.stopping.wait(), delay)
            except (TimeoutError, asyncio.TimeoutError):
                pass


class JobRunner:
    """A configurable number of slots in one co-located process."""

    def __init__(self, pool, handlers, *, heartbeat_connect, slots: int = 3, **options):
        if not isinstance(slots, int) or isinstance(slots, bool) or not 1 <= slots <= 32:
            raise ValueError('Worker slots must be between 1 and 32')
        identifier = f'{socket.gethostname()}:{uuid.uuid4()}'
        self.workers = [
            ServerWorker(
                pool,
                handlers,
                heartbeat_connect=heartbeat_connect,
                worker_id=f'{identifier}:{slot}',
                lanes=('user',) if slots > 1 and slot == 0 else None,
                **options,
            )
            for slot in range(slots)
        ]

    def stop(self):
        for worker in self.workers:
            worker.stop()

    async def run(self):
        tasks = [asyncio.create_task(worker.run()) for worker in self.workers]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if not all(worker.stopping.is_set() for worker in self.workers):
                for task in tasks:
                    if task.done():
                        await task
                raise RuntimeError('A worker slot exited unexpectedly')
            await asyncio.gather(*tasks)
        finally:
            self.stop()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def local_runner(
    dsn,
    handlers,
    *,
    server_settings=None,
    slots=None,
    placements=('server', 'compute'),
    **options,
):
    """Explicit worker connection budget, independent of the API's pool settings. The co-located runner
    also takes compute work (map re-fits) unless a deployment leaves that to separate compute workers."""
    count = int(os.getenv('JOB_WORKER_SLOTS', '3')) if slots is None else slots
    maximum = int(os.getenv('JOB_POOL_MAX_SIZE', str(count * 2)))
    if not 1 <= count <= 32 or maximum < count or maximum > 64:
        raise ValueError('Use 1–32 worker slots and a handler pool between slots and 64 connections')
    pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        # Recovery can hold a connection for the whole queue rewrite, even with one slot.
        max_size=maximum + 1,
        command_timeout=300,
        init=init_connection,
        server_settings=server_settings,
    )
    try:
        yield JobRunner(
            pool,
            handlers,
            slots=count,
            heartbeat_connect=heartbeat_connector(dsn, server_settings=server_settings),
            placements=placements,
            **options,
        )
    finally:
        await pool.close()
