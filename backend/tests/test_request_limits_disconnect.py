"""A finished public-mode request must finish its middleware too, or a guest slot stays taken."""

import asyncio

import pytest
from fastapi import FastAPI

from backend.request_limits import RequestLimitsMiddleware

SCOPE = {
    'type': 'http',
    'asgi': {'version': '3.0'},
    'http_version': '1.1',
    'scheme': 'http',
    'path': '/read',
    'raw_path': b'/read',
    'root_path': '',
    'query_string': b'',
    'headers': [(b'host', b'test')],
    'client': ('127.0.0.1', 1),
    'server': ('test', 80),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['GET', 'POST'])
async def test_request_completes_when_receive_absorbs_the_cancel(monkeypatch, method):
    # In public mode the middleware reads ASGI receive in its own task and cancels that task
    # when the response is done. Behind backend/main.py's csrf_origin_guard, receive is
    # Starlette's BaseHTTPMiddleware receive_or_disconnect: it can absorb that cancel (its own
    # anyio scope is cancelled at the same moment) and return http.disconnect instead. A GET
    # handler never reads its request message, so the queue was still full and the receiver
    # waited on it forever: the request never finished and its guest slot was never released.
    monkeypatch.setenv('DEMO_MODE', 'true')
    app = FastAPI()
    app.add_middleware(RequestLimitsMiddleware)

    @app.api_route('/read', methods=['GET', 'POST'])
    async def read():
        return {'ok': True}

    for _ in range(3):
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {'type': 'http.disconnect'}  # the cancel is absorbed, as observed
            raise AssertionError('unreachable')

        messages = []

        async def send(message):
            messages.append(message)

        await asyncio.wait_for(app({**SCOPE, 'method': method}, receive, send), 2)
        assert messages[0]['status'] == 200
