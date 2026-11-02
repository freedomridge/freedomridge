"""Request-scoped database budgets without reducing the rows a guest can see."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps

import asyncpg
from fastapi import HTTPException
from starlette.responses import JSONResponse

from . import public_settings
from .security import private_guest, public_mode

# Deliberate allowlist of database-intensive reads: route handlers marked @guest_heavy.
# It covers sample
# queries/counts, facets/histograms, spatial queries, vector search/scatter, bulk
# exports and sparse sampling. These can scan whole datasets; no row cap is added.
# Schema/stats/coverage, autocomplete, duplicate and selection queries also scan.
# Media (including crop/composite/patch variants), auth/session/config/health,
# dataset metadata, single-sample reads and private views NEVER take these slots.
# Add new expensive routes explicitly; do not turn this into an /api-wide gate.
# The mark is on the handler, not the route template: FastAPI 0.137+ reports an included
# route's path without its /api prefix, which would silently turn a template list off.
GUEST_HEAVY_ENDPOINTS: set = set()


def guest_heavy(endpoint):
    """Mark a route handler as a heavy query: a guest waits for one of their slots before it runs."""
    GUEST_HEAVY_ENDPOINTS.add(endpoint)
    return endpoint


@dataclass
class GuestSlots:
    semaphore: asyncio.Semaphore
    requests: int = 0  # Holders AND waiters; retain the shared semaphore until both leave.


@dataclass
class RequestContext:
    scope: dict = field(default_factory=dict)
    user: dict | None = None
    failure: HTTPException | None = None
    slots: GuestSlots | None = None
    semaphore: asyncio.Semaphore | None = None
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)


request_context: ContextVar[RequestContext | None] = ContextVar('database_request', default=None)
_guest_slots: dict[int, GuestSlots] = {}


async def set_request_user(user):
    context = request_context.get()
    if context is None:
        return
    context.user = user
    if private_guest(user) and context.scope.get('endpoint') in GUEST_HEAVY_ENDPOINTS and context.slots is None:
        if context.disconnected.is_set():
            raise asyncio.CancelledError
        slots = _guest_slots.setdefault(
            user['id'],
            GuestSlots(asyncio.Semaphore(public_settings.settings.guest_concurrency)),
        )
        context.slots = slots
        slots.requests += 1
        # Authentication's DB checkout has already been released. Wait BEFORE
        # route dependencies/handlers can acquire a connection, so queued guests
        # cannot hold the pool needed by slot holders. This queue deadline and the
        # later DB acquire deadline are independent (10s and 5s by default).
        try:
            await asyncio.wait_for(slots.semaphore.acquire(), timeout=public_settings.settings.guest_queue_seconds)
        except asyncio.TimeoutError as error:
            raise unavailable(503, 'Too many simultaneous guest queries. Please try again shortly.') from error
        context.semaphore = slots.semaphore


def unavailable(status, detail):
    error = HTTPException(status_code=status, detail=detail, headers={'Retry-After': '5'})
    context = request_context.get()
    if context is not None:
        context.failure = error
    return error


class RequestLimitsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        # Routing fills in scope['endpoint'] before authentication runs.
        context = RequestContext(scope=scope)
        token = request_context.set(context)
        replaced = False
        started = False
        app_task = None
        receiver = None

        async def send_response(message):
            nonlocal replaced, started
            if message['type'] == 'http.response.start':
                started = True
                # Preserve successful partial/cached results when a route handles a timeout.
                # Routes that translate the unhandled error to another HTTP error still get 504.
                if message['status'] >= 400 and context.failure is not None:
                    error = context.failure
                    replaced = True
                    await JSONResponse({'detail': error.detail}, error.status_code, headers=error.headers)(
                        scope,
                        receive,
                        send,
                    )
            if not replaced:
                await send(message)

        # One reader owns ASGI receive. Backpressure bounds body buffering; after the final body
        # chunk we can watch for disconnect even when a route never reads request.receive().
        messages: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def receive_messages():
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    context.disconnected.set()
                    if app_task is not None and private_guest(context.user or {}):
                        app_task.cancel()
                    # Never wait here: the cancel below can be absorbed by an outer
                    # BaseHTTPMiddleware's receive, and a handler that never read its body
                    # leaves the queue full, so a blocking put kept the request (and its guest
                    # slot) forever. The client is gone; an unread body chunk no longer matters.
                    while messages.full():
                        messages.get_nowait()
                    messages.put_nowait(message)
                    return
                await messages.put(message)

        try:
            if public_mode():
                app_task = asyncio.create_task(self.app(scope, messages.get, send_response))
                receiver = asyncio.create_task(receive_messages())
                await app_task
            else:
                await self.app(scope, receive, send_response)
        except (asyncpg.QueryCanceledError, asyncio.TimeoutError):
            if context.failure is None or started:
                raise
            error = context.failure
            await JSONResponse({'detail': error.detail}, error.status_code, headers=error.headers)(scope, receive, send)
        except asyncio.CancelledError:
            if not context.disconnected.is_set():
                raise
        finally:
            if receiver is not None:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            if context.semaphore is not None:
                context.semaphore.release()
            if context.slots is not None:
                context.slots.requests -= 1
                if context.slots.requests == 0:
                    assert context.user is not None
                    _guest_slots.pop(context.user['id'], None)
            request_context.reset(token)


def guest_query(method):
    @wraps(method)
    async def bounded(connection, *args, **kwargs):
        context = request_context.get()
        if context is None or not private_guest(context.user or {}):
            return await method(connection, *args, **kwargs)
        # The session default set at checkout lets routes use a tighter SET LOCAL. An asyncpg
        # deadline also enforces the ceiling if a route raises/disables the server timeout.
        budget = public_settings.settings.guest_statement_seconds
        timeout = kwargs.get('timeout')
        kwargs['timeout'] = budget if timeout is None else min(timeout, budget)
        try:
            return await method(connection, *args, **kwargs)
        except (asyncpg.QueryCanceledError, asyncio.TimeoutError):
            unavailable(504, 'This query took too long. Please try again with narrower filters.')
            # Keep the original exception type for sidebar/field-discovery fallback handlers.
            raise

    return bounded


class RequestConnection(asyncpg.Connection):
    fetch = guest_query(asyncpg.Connection.fetch)
    fetchrow = guest_query(asyncpg.Connection.fetchrow)
    fetchval = guest_query(asyncpg.Connection.fetchval)
    execute = guest_query(asyncpg.Connection.execute)
    executemany = guest_query(asyncpg.Connection.executemany)


class _Acquire:
    def __init__(self, pool, timeout):
        self.pool = pool
        self.timeout = timeout
        self.connection = None

    async def __aenter__(self):
        timeout = self.timeout
        if public_mode():
            budget = public_settings.settings.acquire_seconds
            timeout = budget if timeout is None else min(timeout, budget)
        try:
            self.connection = await self.pool.acquire(timeout=timeout)
        except asyncio.TimeoutError as error:
            if not public_mode():
                raise
            raise unavailable(503, 'The database is busy. Please try again shortly.') from error
        context = request_context.get()
        if context is not None and private_guest(context.user or {}):
            try:
                # One setting per checkout, no per-statement BEGIN/SET/COMMIT. asyncpg's reset
                # on release clears the session setting before a privileged caller can reuse it.
                await self.connection.execute(
                    "SELECT set_config('statement_timeout', CASE "
                    "WHEN current_setting('statement_timeout')::interval = interval '0' "
                    "OR current_setting('statement_timeout')::interval > $1::text::interval "
                    "THEN $1 ELSE current_setting('statement_timeout') END, false)",
                    f'{max(1, int(public_settings.settings.guest_statement_seconds * 1000))}ms',
                )
            except BaseException:
                await self.pool.release(self.connection)
                raise
        return self.connection

    def __await__(self):
        return self.__aenter__().__await__()

    async def __aexit__(self, *exc):
        await self.pool.release(self.connection)


class RequestPool:
    """Keep asyncpg's await/context-manager acquire API, with a finite public-mode wait."""

    def __init__(self, pool):
        self.pool = pool

    def __getattr__(self, name):
        return getattr(self.pool, name)

    def acquire(self, *, timeout=None):
        return _Acquire(self.pool, timeout)

    fetch = asyncpg.Pool.fetch
    fetchrow = asyncpg.Pool.fetchrow
    fetchval = asyncpg.Pool.fetchval
    execute = asyncpg.Pool.execute
    executemany = asyncpg.Pool.executemany
