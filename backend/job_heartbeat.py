"""Lease renewal owns a thread and connection, independent of handler capacity."""

import asyncio
import logging
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import asyncpg

from . import job_queue
from .database import init_connection
from .job_tasks import run_isolated

logger = logging.getLogger(__name__)
Connector = Callable[[], Awaitable[asyncpg.Connection]]
LEASE_SHUTDOWN_SECONDS = 2


def retry_gap(heartbeat_seconds):
    """The pause before retrying a lease operation (renewal, or recording an attempt's end) after a transient
    error."""
    return min(heartbeat_seconds, 1)


def renewal_reserve(renewal, telemetry, heartbeat_seconds):
    """Lease time a renewal needs before expiry: two bounded attempts (connect and renew each), the
    telemetry and the retry gap between them."""
    return 4 * renewal + telemetry + retry_gap(heartbeat_seconds)


def heartbeat_budgets(lease_seconds, heartbeat_seconds):
    renewal = min(5, lease_seconds / 8)
    telemetry = min(1, lease_seconds / 8)
    if heartbeat_seconds + renewal_reserve(renewal, telemetry, heartbeat_seconds) >= lease_seconds:
        raise ValueError('Lease must cover heartbeat interval, telemetry and two bounded renewal attempts')
    return renewal, telemetry


def heartbeat_connector(dsn: str, *, server_settings: dict | None = None) -> Connector:
    async def connect():
        settings = {
            **(server_settings or {}),
            'idle_in_transaction_session_timeout': f'{job_queue.IDLE_TRANSACTION_TIMEOUT_MS}ms',
        }
        connection = await asyncpg.connect(dsn, timeout=1, command_timeout=5, server_settings=settings)
        try:
            await init_connection(connection)
        except BaseException:
            connection.terminate()
            raise
        return connection

    return connect


# Rows of workers not heard from for this long are dropped: every runner start registers new ids.
WORKER_RETENTION = '1 day'


async def worker_heartbeat(source, worker_id, kinds, current_job_id=None):
    async with job_queue._transaction(source) as connection:
        await connection.execute(
            f"DELETE FROM workers WHERE heartbeat_at < clock_timestamp() - interval '{WORKER_RETENTION}'",
        )
        await connection.execute(
            """INSERT INTO workers (id, placement, name, hostname, capabilities, current_job_id)
               VALUES ($1, 'server', $1, $2, $3, $4)
               ON CONFLICT (id) DO UPDATE SET
                   heartbeat_at=clock_timestamp(),current_job_id=EXCLUDED.current_job_id,
                   capabilities=EXCLUDED.capabilities""",
            worker_id,
            socket.gethostname(),
            {'kinds': sorted(kinds)},
            current_job_id,
        )


class LeaseSession:
    """One thread and reusable connection for the lifetime of an execution slot."""

    def __init__(self, connector, worker_id):
        self.connector = connector
        self.connection = None
        self.loop = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f'lease:{worker_id}', daemon=True)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.discard()
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.wait(pending, timeout=LEASE_SHUTDOWN_SECONDS))
            self.loop.close()

    async def start(self):
        self.thread.start()
        await asyncio.to_thread(self.ready.wait)

    async def connect(self):
        if self.connection is not None and not self.connection.is_closed() and self.connection.is_in_transaction():
            self.discard()
        if self.connection is None or self.connection.is_closed():
            self.connection = await self.connector()
        return self.connection

    def discard(self):
        """A cancelled transaction handshake makes a reusable session unsafe."""
        if self.connection is not None:
            self.connection.terminate()
            self.connection = None

    async def run_bounded(self, operation, timeout):
        """Close the socket before cancelling a timed-out transaction or waiting for rollback."""
        return await run_isolated(
            operation,
            name='Lease connection operation',
            timeout=timeout,
            on_error=self.discard,
        )

    def submit(self, coroutine):
        if self.loop is None:
            raise RuntimeError('Lease session is not started')
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    async def _disconnect(self):
        if self.connection is not None:
            try:
                await self.connection.close(timeout=1)
            except Exception:
                logger.debug('Lease connection could not close cleanly; terminating it', exc_info=True)
            finally:
                self.discard()

    async def close(self):
        if self.loop is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.wrap_future(self.submit(self._disconnect())),
                LEASE_SHUTDOWN_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning('Lease connection shutdown timed out; stopping its thread')
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            await asyncio.to_thread(self.thread.join, LEASE_SHUTDOWN_SECONDS)
            if self.thread.is_alive():
                logger.error('Lease thread did not stop within its shutdown budget')


def _settle(future):
    if not future.done():
        future.set_result(None)


@dataclass
class UnitDeadline:
    deadline: float
    timed_out: asyncio.Event = field(default_factory=asyncio.Event)
    completed_at: float | None = None
    notified: bool = False


class LeaseMonitor:
    """Thread-safe signals; no handler-loop pool or connection enters this thread."""

    def __init__(self, session, job, worker_id, kinds, lease_seconds, heartbeat_seconds):
        self.session = session
        self.job = job
        self.worker_id = worker_id
        self.kinds = kinds
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.renewal_budget, self.telemetry_budget = heartbeat_budgets(lease_seconds, heartbeat_seconds)
        self.retry_gap = retry_gap(heartbeat_seconds)
        self.renewal_reserve = renewal_reserve(self.renewal_budget, self.telemetry_budget, heartbeat_seconds)
        self.loop = asyncio.get_running_loop()
        self.lost = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.ready = asyncio.Event()
        self.closed = threading.Event()
        self._unit_lock = threading.Lock()
        self._unit: UnitDeadline | None = None
        self.renew_by = time.monotonic() + max(0, (job['lease_expires_at'] - job['heartbeat_at']).total_seconds())
        self.future = None
        self.wake = None
        self._pause_lock = threading.Lock()
        self._pauses = 0  # nested pauses (a publication, a handler fence): renewals wait while any is held
        self._renewing = False
        self._idle: asyncio.Future | None = None

    def signal(self, event):
        self.loop.call_soon_threadsafe(event.set)

    async def pause(self):
        """Hold renewals while the slot publishes: a renewal and its telemetry lock the job row too, and under
        slow commits they outlast the publication's lock timeout. Waits for a renewal already running (at most
        until the lease would expire; a stalled lease thread leaves the publication to its own fence). A
        renewal the lease cannot wait for, within the renewal reserve of expiry, still runs. Pauses nest: each
        is ended by its own resume()."""
        with self._pause_lock:
            self._pauses += 1
            if not self._renewing:
                return
            if self._idle is None:
                self._idle = self.loop.create_future()
            idle = self._idle
        try:
            await asyncio.wait_for(asyncio.shield(idle), max(0.0, self.renew_by - time.monotonic()))
        except (asyncio.TimeoutError, TimeoutError):
            pass

    def resume(self):
        with self._pause_lock:
            self._pauses = max(0, self._pauses - 1)

    @asynccontextmanager
    async def paused(self):
        try:
            await self.pause()
            yield
        finally:
            self.resume()

    def _begin_renewal(self, now):
        with self._pause_lock:
            if self._pauses and self.renew_by - now > self.renewal_reserve:
                return False
            self._renewing = True
            return True

    def _end_renewal(self):
        with self._pause_lock:
            self._renewing = False
            idle, self._idle = self._idle, None
        if idle is not None:
            self.loop.call_soon_threadsafe(_settle, idle)

    def begin_unit(self, duration):
        unit = UnitDeadline(time.monotonic() + duration)
        with self._unit_lock:
            if self._unit is not None:
                raise RuntimeError('A measurement unit is already active')
            self._unit = unit
        return unit

    def finish_unit(self, unit):
        completed_at = time.monotonic()
        with self._unit_lock:
            if unit.completed_at is None:
                unit.completed_at = completed_at
            if self._unit is unit:
                self._unit = None

    def _unit_overdue(self, now):
        with self._unit_lock:
            unit = self._unit
            if unit is None or now < unit.deadline:
                return False
            if not unit.notified:
                unit.notified = True
                self.signal(unit.timed_out)
            return True

    async def start(self):
        self.future = self.session.submit(self._renew())
        await self.ready.wait()

    async def close(self):
        self.closed.set()
        if self.wake is not None:
            self.session.loop.call_soon_threadsafe(self.wake.set)
        if self.future is not None:
            completion = asyncio.wrap_future(self.future)
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                await completion
                raise

    async def _renew(self):
        self.wake = asyncio.Event()
        next_renewal = 0.0
        try:
            while not self.closed.is_set():
                now = time.monotonic()
                if now >= self.renew_by:
                    self.signal(self.lost)
                    return
                overdue = self._unit_overdue(now)
                due = now >= next_renewal
                if overdue or not due or not self._begin_renewal(now):
                    delay = 0.1 if overdue or due else min(next_renewal - now, 0.1)
                    try:
                        await asyncio.wait_for(self.wake.wait(), delay)
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
                    continue
                try:
                    connection = await self.session.run_bounded(self.session.connect(), self.renewal_budget)
                    renewed_at = time.monotonic()
                    renewed = await self.session.run_bounded(
                        job_queue.heartbeat_job(
                            connection,
                            self.job['id'],
                            self.job['lease_token'],
                            lease_seconds=self.lease_seconds,
                        ),
                        self.renewal_budget,
                    )
                    if renewed is None:
                        self.signal(self.lost)
                        return
                    self.renew_by = renewed_at + self.lease_seconds
                    if renewed['cancel_requested']:
                        self.signal(self.cancelled)
                    self.signal(self.ready)
                    next_renewal = time.monotonic() + self.heartbeat_seconds
                    try:
                        await self.session.run_bounded(
                            worker_heartbeat(connection, self.worker_id, self.kinds, self.job['id']),
                            self.telemetry_budget,
                        )
                    except Exception:
                        self.session.discard()
                        logger.debug('Worker telemetry failed; the job lease is renewed', exc_info=True)
                except Exception as error:
                    self.session.discard()
                    if not job_queue.is_transient(error):
                        logger.exception('Unexpected lease-monitor failure for %s', self.job['id'])
                        self.signal(self.lost)
                        return
                    next_renewal = time.monotonic() + self.retry_gap
                finally:
                    self._end_renewal()
        except BaseException:
            self.session.discard()
            self.signal(self.lost)
            raise
        finally:
            self.signal(self.ready)


# The lease of the job a slot is running, set by the slot; its unit task inherits it.
current_lease: ContextVar[LeaseMonitor | None] = ContextVar('current_lease', default=None)


@asynccontextmanager
async def renewals_paused():
    """For a handler's own fenced transaction (checkpoint_locked, finish_locked): hold the job's lease
    renewals from just before its job-row fence until after the COMMIT, as the slot does around its own
    publications, so the job's own renewal is not mid-commit on the row when the fence arrives. Bounded by
    the lease like that pause. Outside a slot (a handler called directly) it does nothing."""
    monitor = current_lease.get()
    if monitor is None:
        yield
        return
    async with monitor.paused():
        yield
