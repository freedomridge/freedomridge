"""The MCP server at /mcp: the door an outside AI client (Claude Code, Codex, any MCP client) comes through to the
assistant's tools (docs/ASSISTANT.md).

- The official MCP SDK (the `mcp` package) speaks the protocol: the 2026-07-28 revision, and the initialize-handshake
  revisions before it (2025-11-25, 2025-06-18), from the one address.
- Nothing is kept between requests. The SDK runs stateless: it issues no session id, opens no standing stream, and
  each request is served by an SDK manager made for it and closed with it. Any process behind the address can answer
  any request, and nothing has to be started with the app.
- Only an MCP token opens it (routers/auth.py, get_mcp_user). A browser's session cookie counts for nothing here, so
  a web page can't drive /mcp through someone's signed-in browser.

A request to /mcp is checked in this order: its Origin (403 unless it is one of the server's own allowed origins,
before anything else is looked at), its token (401), whether its user may use the door right now (403, in the words
of the assistant's door_refusal), its method (405 unless POST), its size (413 over MAX_BODY_BYTES), and only then is it
handed to the SDK. Each tool call
leaves a row in the assistant's call log (backend/assistant_calls.py). McpDoor sits outside the app's other
middleware, so no CORS answer ever grants a page permission here and the cookie-based CSRF guard has nothing to guard.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Collection
from datetime import datetime, timezone
from typing import Any

import mcp_types as types
from fastapi import HTTPException
from mcp.server.caching import CacheHint
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import assistant, assistant_calls
from .database import db
from .routers import auth
from .version import VERSION

PATH = '/mcp'
# How long a 2026-07-28 client may reuse the tool list before asking again, and only for the caller that asked: the
# list is fixed for a release, so a few minutes of reuse costs nothing, and an upgrade reaches clients soon after.
TOOL_LIST_CACHE_MS = 5 * 60 * 1000
# Where the signed-in user travels from the token check to the tool handlers, on the request's ASGI scope.
_USER = 'fridge.mcp_user'
# What a call answers when the server failed while the tool ran: nothing of the fault itself, which goes to the log.
SERVER_FAILED = 'The server failed to answer this call'
# Its error code, in the tool result and in the call log.
FAILED_CODE = 'failed'

# The largest request body /mcp reads: a tool's arguments are a few small values. Counted while reading, whatever
# Content-Length says, so a proxy in front that doesn't cap bodies (the production nginx caps at 1 MB) changes nothing.
MAX_BODY_BYTES = 256 * 1024


def _setting(name: str, default: int) -> int:
    return max(1, int(os.getenv(name) or default))


# A model can loop, and a power user or an administrator has no guest request slots, so tool calls have limits of
# their own (docs/ASSISTANT.md, "Limits"). Kept in this process, with no shared state: a server of N processes lets
# one user run up to N times CALLS_AT_ONCE calls at once, and every call is bounded by CALL_SECONDS wherever it runs.
# Tool calls of one user running at once in this process; the next is answered `busy` at once.
CALLS_AT_ONCE = _setting('FRIDGE_ASSISTANT_CALLS_AT_ONCE', 4)
# Tool calls of all users running at once in this process, so many users can't fill it; the next is `busy` too.
CALLS_PER_PROCESS = _setting('FRIDGE_ASSISTANT_CALLS_PER_PROCESS', 16)
CALL_SECONDS = 60.0  # the longest one tool call runs: then its work is cancelled and it answers TOO_LONG
TOO_MANY = f'You have {CALLS_AT_ONCE} assistant calls running already; try again when one has finished.'
PROCESS_FULL = 'The server is running as many assistant calls as it takes at once; try again in a moment.'
TOO_LONG = f'This call took longer than {CALL_SECONDS:g} seconds and was stopped. Ask for less at once.'
TOO_LONG_CODE = 'timeout'
# A call cancelled before it answered (its client went away): its code in the call log.
CANCELLED_CODE = 'cancelled'
# A `busy` refusal is logged at most once a minute per user in this process: enough to show an administrator a loop,
# while a client refused in a tight loop can't fill the log between two cleanups.
BUSY_ROW_SECONDS = 60.0
_running: dict[int, int] = {}  # user id -> their tool calls running in this process
_busy_logged: dict[int, float] = {}  # user id -> when this process last logged a `busy` refusal of theirs
_log_writes: set[asyncio.Task[None]] = set()  # log rows being written, kept until done

logger = logging.getLogger('fridge.mcp')


class McpDoor:
    """ASGI middleware answering /mcp and passing everything else to the app. Added last, so it is the outermost:
    `allowed_origins` is the set the app's CSRF guard uses (ALLOWED_ORIGINS), and `served` is what answers a request
    whose origin passed (the endpoint, inside the app's version header and request limits)."""

    def __init__(self, app: ASGIApp, *, allowed_origins: Collection[str], served: ASGIApp) -> None:
        self.app = app
        self.allowed_origins = allowed_origins
        self.served = served

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http' or scope['path'] != PATH:
            return await self.app(scope, receive, send)
        origin = Headers(scope=scope).get('origin')
        if origin is not None and origin.rstrip('/') not in self.allowed_origins:
            return await _refusal(403, 'This origin may not use the MCP server')(scope, receive, send)
        await self.served(scope, receive, send)


async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
    """One request to /mcp, after its origin passed: the token, then the method, then the SDK."""
    request = Request(scope, receive)
    try:
        # Each takes a pooled connection and gives it back before anything below runs: a tool call must never wait
        # on the pool while this request holds a connection of it.
        user = await auth.get_mcp_user(request)
        # Whether this user may use the door right now: asked afresh on every request, whatever it asks.
        refusal = await assistant.door_refusal(user_id=user['id'], door='mcp')
    except HTTPException as refused:
        return await _refusal(refused.status_code, str(refused.detail))(scope, receive, send)
    if refusal is not None:
        return await _refusal(403, refusal)(scope, receive, send)
    if request.method != 'POST':
        # The earlier revisions' standing GET stream and session DELETE: this server keeps neither (both revisions
        # allow 405 for them, and 2026-07-28 asks for it).
        return await _refusal(405, 'The MCP server takes POST requests only')(scope, receive, send)
    body = await _read_body(scope, receive)
    if body is None:
        return await _refusal(413, f'The MCP server takes requests of at most {MAX_BODY_BYTES // 1024} KB')(
            scope,
            receive,
            send,
        )
    scope[_USER] = user
    manager = StreamableHTTPSessionManager(
        _server(),
        stateless=True,
        # The Origin check is McpDoor's, against the app's own list, and comes before the token. The SDK's
        # Host/Origin allow-lists would need the public host name, which only the proxy in front knows.
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    delivered = False

    async def replay() -> Message:
        # The body read above, then the client's own messages (the SDK watches them for a disconnect).
        nonlocal delivered
        if delivered:
            return await receive()
        delivered = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    async with manager.run():
        await manager.handle_request(scope, replay, send)


async def _read_body(scope: Scope, receive: Receive) -> bytes | None:
    """The request's body, or None when it is larger than MAX_BODY_BYTES: said so by Content-Length, or found while
    reading, however the body comes (chunked, or longer than declared)."""
    declared = Headers(scope=scope).get('content-length')
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    while True:
        message = await receive()
        if message['type'] != 'http.request':
            return bytes(body)
        body += message.get('body', b'')
        if len(body) > MAX_BODY_BYTES:
            return None
        if not message.get('more_body', False):
            return bytes(body)


def _refusal(status: int, message: str) -> Response:
    """A refusal before the SDK, as a JSON-RPC error without an id, which MCP clients show as the request's error."""
    body = {'jsonrpc': '2.0', 'id': None, 'error': {'code': types.INVALID_REQUEST, 'message': message}}
    # 401 says which scheme to use, and points at nothing else: this server offers no OAuth.
    headers = {401: {'WWW-Authenticate': 'Bearer'}, 405: {'Allow': 'POST'}}.get(status)
    return JSONResponse(body, status_code=status, headers=headers)


def _server() -> Server[Any]:
    return Server(
        'freedomridge',
        version=VERSION,
        instructions=assistant.instructions(),
        # 2026-07-28 results carry these on tools/list; the SDK leaves them out for the earlier revisions.
        cache_hints={'tools/list': CacheHint(ttl_ms=TOOL_LIST_CACHE_MS, scope='private')},
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


async def _list_tools(
    ctx: ServerRequestContext[Any, Any],
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    read_only = types.ToolAnnotations(read_only_hint=True)
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                annotations=read_only,
            )
            for tool in assistant.tools()
        ],
    )


async def _call_tool(ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams) -> types.CallToolResult:
    if params.name not in {tool.name for tool in assistant.tools()}:
        raise MCPError(code=types.INVALID_PARAMS, message=f'Unknown tool: {params.name}')
    assert ctx.request is not None  # the HTTP transports always attach the request
    user = ctx.request.scope[_USER]
    arguments = params.arguments or {}
    started_at, clock = datetime.now(timezone.utc), time.monotonic()
    answer = None
    try:
        answer = await _limited_call(user['id'], params.name, arguments)
    finally:
        # Every call leaves its row, a call cancelled when its client went away too: written after the call, with no
        # connection held while it ran, and shielded, so it finishes even while this request is being cancelled.
        code = CANCELLED_CODE if answer is None else None if answer['ok'] else answer['error']['code']
        if code != 'busy' or _busy_row_due(user['id']):
            row = asyncio.ensure_future(
                _log_call(
                    user_id=user['id'],
                    tool=params.name,
                    arguments=arguments,
                    started_at=started_at,
                    duration_ms=round((time.monotonic() - clock) * 1000),
                    error_code=code,
                ),
            )
            _log_writes.add(row)
            row.add_done_callback(_log_writes.discard)
            await asyncio.shield(row)
    return _tool_result(answer)


def _busy_row_due(user_id: int) -> bool:
    """Whether this `busy` refusal is logged: the first of each minute, per user, in this process."""
    now = time.monotonic()
    if now - _busy_logged.get(user_id, -BUSY_ROW_SECONDS) < BUSY_ROW_SECONDS:
        return False
    _busy_logged[user_id] = now
    return True


async def _log_call(**row: Any) -> None:
    """Write a call's log row (never its arguments or result). A failure to write it is the server's to log: it
    never turns the answer the tool gave into an error."""
    try:
        await assistant_calls.record(db.pool, door='mcp', **row)
    except Exception:
        logger.exception('The assistant call log could not record a call of %s', row['tool'])


async def _limited_call(user_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """call_tool within the door's limits: at most CALLS_AT_ONCE of the user's calls at once in this process (the
    next is answered `busy` at once, without waiting), and CALL_SECONDS for one call, after which its work, awaited
    here, is cancelled (the in-process requests of the tool and their queries with it)."""
    if _running.get(user_id, 0) >= CALLS_AT_ONCE:
        return _error('busy', TOO_MANY, retryable=True)
    if sum(_running.values()) >= CALLS_PER_PROCESS:
        return _error('busy', PROCESS_FULL, retryable=True)
    _running[user_id] = _running.get(user_id, 0) + 1
    try:
        # wait_for (the backend keeps to Python 3.10's asyncio) runs the call in a task of its own, which starts with
        # a copy of this request's context, so the request limits' context reaches the tool's in-process requests.
        # At CALL_SECONDS it cancels that task, and waits for it to end, before raising TimeoutError here; when this
        # request is cancelled (its client went away), it cancels the task too and the CancelledError goes on up.
        return await asyncio.wait_for(_call(user_id, name, arguments), CALL_SECONDS)
    except asyncio.TimeoutError:
        return _error(TOO_LONG_CODE, TOO_LONG)
    except Exception:
        # call_tool raises only for a server fault (a route answering 500 or not JSON, a fault of the server's
        # own): the model gets a tool result it can read and go on from, and the fault goes to the server's log.
        logger.exception('MCP call of %s failed', name)
        return _error(FAILED_CODE, SERVER_FAILED)
    finally:
        _running[user_id] -= 1
        if not _running[user_id]:
            del _running[user_id]


class _ToolTimeout(Exception):
    """A TimeoutError raised inside the call itself: a fault of the server's, told apart from the door's deadline."""


async def _call(user_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return await assistant.call_tool(user_id=user_id, door='mcp', name=name, arguments=arguments)
    except asyncio.TimeoutError as error:
        raise _ToolTimeout(str(error)) from error


def _error(code: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {'ok': False, 'error': {'code': code, 'message': message, 'retryable': retryable}}


def _tool_result(answer: dict[str, Any]) -> types.CallToolResult:
    """call_tool's answer as a tool result: a refusal or failure is a result marked as an error, which a model reads
    and goes on from, not a failed request; only an unknown tool is the protocol's own error."""
    if answer['ok']:
        result = answer['result']
        return types.CallToolResult(
            content=[types.TextContent(text=json.dumps(result))],
            structured_content=result,
        )
    error = answer['error']
    if error['code'] == 'unknown_tool':
        raise MCPError(code=types.INVALID_PARAMS, message=error['message'])
    text = error['message']
    if error.get('retryable'):
        text += ' (This call may be tried again.)'
    return types.CallToolResult(
        content=[types.TextContent(text=text)],
        structured_content={'error': error},
        is_error=True,
    )
