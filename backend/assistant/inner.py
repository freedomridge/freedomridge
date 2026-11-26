"""A tool's request to the HTTP API, made in process as the user (docs/ASSISTANT.md, "How a tool reads").

The request goes through the whole application, every middleware and dependency a browser's request goes through,
so every rule the API keeps holds for it and none is written twice. It carries the user on the ASGI scope
(identity.SCOPE_KEY), never a token or a cookie, and no client address (request.client is None).

Two fences keep a tool to the routes it lists (Tool.routes): before the request is sent, its (method, route
template) must be one of them and name a route of the application; while it is served, get_current_user refuses it
unless the route serving it is one of them (Identity.routes holds their handlers). Only JSON answers are read: a
route that streams is refused.

Private values (private.py, guard.py): only a route whose answer's shape the sealing covers may be called
(private.COVERED_ROUTES). A route that names no dataset in its path says where its dataset comes from
(private.DATASET_FROM): none, its body, or the one it resolved itself, whose private fields seal its answer.
The request runs under a guard holding its dataset's private fields and the server's key, so
the API's filter compiler refuses any way of questioning a private field but by the tokens it was given, which open
there. Every answer reaches the tool with each value of its dataset's private fields sealed, here. An error answer of a
request that opened a token says only that, for its words could hold the opened values.

Nothing here holds a pooled database connection while a request runs: the request takes its own, as any other does.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import quote, urlencode

import anyio
import asyncpg

from . import guard, private
from .identity import SCOPE_KEY, Identity

# The most of an answer read: the largest JSON answer a tool asks for (a dataset's schema) is far smaller.
MAX_ANSWER_BYTES = 64 * 1024 * 1024
# The host an in-process request names. Nothing builds a link from it (links come from elsewhere).
INNER_HOST = b'assistant.invalid'


class RouteNotListed(Exception):
    """A tool asked for a route it doesn't list, or one the application doesn't have: refused before it is sent."""


class NotJson(Exception):
    """The route answered something other than JSON (a stream, a file)."""


@dataclass(frozen=True)
class Answer:
    status: int
    body: Any  # the parsed JSON; None for an empty answer
    headers: Mapping[str, str]
    unreadable: int = 0  # tokens in filter values that didn't open (another field's or dataset's, or an earlier key's)


# The error a request that opened a token answers with, in place of the route's own words.
OPENED_ERROR = 'The request could not be answered as it was made (its words are kept: they could show kept values)'


def _walk(routes: Iterable[Any], prefix: str = '') -> Iterable[tuple[str, Any]]:
    """Every APIRoute of the application with its full path (FastAPI lists an included router, not its routes)."""
    from fastapi.routing import APIRoute

    for route in routes:
        if hasattr(route, 'original_router'):
            yield from _walk(route.original_router.routes, prefix + route.include_context.prefix)
        elif isinstance(route, APIRoute):
            yield prefix + route.path, route


@lru_cache(maxsize=1)
def _route_table() -> dict[tuple[str, str], Callable[..., Any]]:
    """(method, full route template) -> the handler serving it."""
    from ..main import app

    return {(method, path): route.endpoint for path, route in _walk(app.routes) for method in route.methods or ()}


def route_handler(method: str, template: str) -> Callable[..., Any]:
    """The handler of a (method, route template) pair; RouteNotListed when the application has none."""
    handler = _route_table().get((method, template))
    if handler is None:
        raise RouteNotListed(f'The API has no route {method} {template}')
    return handler


def _path(template: str, params: Mapping[str, Any]) -> str:
    """The template with its parameters filled in, each a whole path segment."""
    values = {}
    for name, value in params.items():
        text = str(value)
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not text or '/' in text:
            raise ValueError(f'{name} must be a single path segment, not {value!r}')
        values[name] = quote(text, safe='')
    try:
        return template.format(**values)
    except KeyError as missing:
        raise ValueError(f'{template} needs {missing}') from None


async def request(
    identity: Identity,
    listed: Iterable[tuple[str, str]],
    method: str,
    template: str,
    *,
    path_params: Optional[Mapping[str, Any]] = None,
    query: Optional[Mapping[str, Any]] = None,
    body: Any = None,
) -> Answer:
    """`method template` as `identity`'s user. `listed`: the (method, template) pairs the calling tool lists; any
    other pair is refused (RouteNotListed) before anything is sent."""
    from ..main import app

    listed = frozenset(listed)
    if (method, template) not in listed:
        raise RouteNotListed(f'{method} {template} is not one of the routes this tool lists')
    if (method, template) not in private.COVERED_ROUTES:
        raise RouteNotListed(f"{method} {template} doesn't declare that its answer's shape is sealed")
    handlers = frozenset((each_method, route_handler(each_method, each)) for each_method, each in listed)
    path = _path(template, path_params or {})
    dataset_id = private.request_dataset(dict(path_params or {}), body)
    kept = await private.private_fields(dataset_id) if dataset_id is not None else frozenset()
    seal = private.Seal(identity.seal_key, dataset_id if dataset_id is not None else 0)
    payload = b'' if body is None else json.dumps(body).encode()
    headers = [(b'host', INNER_HOST), (b'accept', b'application/json')]
    if body is not None:
        headers += [(b'content-type', b'application/json'), (b'content-length', str(len(payload)).encode())]
    scope: dict[str, Any] = {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1',
        'method': method,
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode(),
        'root_path': '',
        'query_string': urlencode(
            {key: value for key, value in (query or {}).items() if value is not None},
            doseq=True,
        ).encode(),
        'headers': headers,
        'client': None,
        'server': None,
        SCOPE_KEY: Identity(identity.user_id, identity.door, handlers, identity.seal_key),
        # request.state: check_dataset_access keeps the dataset it resolved here, read back below.
        'state': {},
    }
    sent = False
    finished = anyio.Event()
    status = 500
    answer_headers: dict[str, str] = {}
    chunks: list[bytes] = []
    size = 0

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': payload, 'more_body': False}
        # The caller stays until the answer is complete: a disconnect now would cancel a guest's request.
        await finished.wait()
        return {'type': 'http.disconnect'}

    async def send(message: Mapping[str, Any]) -> None:
        nonlocal status, size
        if message['type'] == 'http.response.start':
            status = message['status']
            answer_headers.update(
                (key.decode('latin-1').lower(), value.decode('latin-1')) for key, value in message.get('headers', [])
            )
        elif message['type'] == 'http.response.body':
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > MAX_ANSWER_BYTES:
                raise NotJson(f'{method} {template} answered more than {MAX_ANSWER_BYTES:,} bytes')
            chunks.append(chunk)

    try:
        with guard.guarding(guard.Guard(dataset_id, kept, seal)) as guarded:
            await app(scope, receive, send)
    except asyncpg.QueryCanceledError:
        # The statement ran into the server's time limit (statement_timeout), which the API leaves to its error
        # handler but for a guest (request_limits answers 504): the same 504 here, which a counting read calls
        # too_slow. Never a partial answer.
        status, chunks = 504, [json.dumps({'detail': 'This query took too long'}).encode()]
        answer_headers['content-type'] = 'application/json'
    finally:
        finished.set()
    resolved = scope['state'].get('dataset')
    resolves = private.DATASET_FROM.get((method, template)) == 'resolved'
    if resolves and dataset_id is None and resolved is not None:
        # A route that names no dataset in its path (a saved view by its slug): its answer is sealed for the dataset
        # the route itself looked up and checked.
        dataset_id = resolved['id']
        kept = await private.private_fields(dataset_id)
        seal = private.Seal(identity.seal_key, dataset_id)
    elif resolved is not None and resolved['id'] != dataset_id:
        raise RouteNotListed(f'{method} {template} read dataset {resolved["id"]}, not the dataset it names')
    if resolves and resolved is None and 200 <= status < 300:
        raise RouteNotListed(f'{method} {template} answered without resolving the dataset its answer is sealed for')
    raw = b''.join(chunks)
    content_type = answer_headers.get('content-type', '')
    if raw and not content_type.startswith('application/json'):
        raise NotJson(f'{method} {template} answered {content_type or "no content type"}, not JSON')
    answer = json.loads(raw) if raw else None
    if guarded.opened and not 200 <= status < 300:
        # The guard's own code survives (it holds no value): the tool still knows a private field was questioned.
        detail = answer.get('detail') if isinstance(answer, dict) else None
        questioned = isinstance(detail, dict) and detail.get('code') == guard.CODE
        answer = {'detail': {'code': guard.CODE, 'message': OPENED_ERROR} if questioned else OPENED_ERROR}
    about = private.answer_field(dict(path_params or {}), dict(query or {}), body)
    echo = (method, template) in private.ECHO_ROUTES or not 200 <= status < 300
    answer = private.seal_answer(answer, kept, seal, about, echo=echo)
    return Answer(status, answer, answer_headers, guarded.unreadable)
