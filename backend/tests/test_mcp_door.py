"""The MCP server at /mcp (backend/mcp_door.py), served by the real app over real HTTP: a client of each of the three
protocol revisions discovers it, lists the tools and calls one; two server processes behind one address answer any
request; the 2026-07-28 tool list may be cached, privately; another origin is refused before anything else; a browser
session counts for nothing; and call_tool's answers become tool results as the door's contract says (section 2.4).

Every test but the two-process one swaps the assistant for a fake, so they test the door alone; the two-process one
runs the real assistant core, with the door switched on for its user.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from backend import access, assistant_calls, main, mcp_door, request_limits
from backend import assistant as assistant_module
from backend import database as database_module
from backend.assistant.identity import scope_identity
from backend.crypto import hash_token
from backend.routers import assistant_calls as assistant_calls_router
from backend.routers import auth, tokens, users
from backend.tests.test_field_registry_postgres import registry_database as _registry_database
from backend.version import VERSION

registry_database = _registry_database

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')

ROOT = Path(__file__).resolve().parents[2]
MODERN, LEGACY, OLDER = '2026-07-28', '2025-11-25', '2025-06-18'
ACCEPT = 'application/json, text/event-stream'
ELSEWHERE = 'https://elsewhere.example'


class FakeAssistant:
    """The core's surface (tools(), instructions(), call_tool(), door_refusal()), answering what a test sets in
    `answers` and `refusal`."""

    def __init__(self):
        page = {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}, 'offset': {'type': 'integer'}}
        self.tool_list = (
            SimpleNamespace(
                name='list_datasets',
                description='The datasets you can open.',
                input_schema={'type': 'object', 'properties': {'search': {'type': 'string'}, **page}},
                kind='read',
                level=None,
                routes=(('GET', '/api/datasets'),),
            ),
            SimpleNamespace(
                name='describe_dataset',
                description="One dataset's fields.",
                input_schema={
                    'type': 'object',
                    'properties': {'dataset': {'type': 'integer'}},
                    'required': ['dataset'],
                },
                kind='read',
                level='view',
                routes=(('GET', '/api/datasets/{dataset_id}'),),
            ),
        )
        self.answers: dict = {}
        self.calls: list = []
        self.refusal: str | None = None
        self.asked: list = []

    def tools(self):
        return self.tool_list

    def instructions(self):
        return 'Freedom Ridge holds image datasets; start with list_datasets.'

    async def call_tool(self, *, user_id, door, name, arguments):
        self.calls.append({'user_id': user_id, 'door': door, 'name': name, 'arguments': arguments})
        return self.answers.get(name, {'ok': True, 'result': {'datasets': [], 'total': 0, 'next_offset': None}})

    async def door_refusal(self, *, user_id, door):
        self.asked.append((user_id, door))
        return self.refusal


@pytest.fixture
def assistant(monkeypatch):
    fake = FakeAssistant()
    monkeypatch.setattr(mcp_door, 'assistant', fake)
    return fake


async def account(database, name='mcp-user', role='power_user'):
    """A user with a session and a token of each kind; {'id', 'session', <kind>: raw token}."""
    user_id = await database.fetchval(
        'INSERT INTO users (username, role, password_hash) VALUES ($1, $2, $3) RETURNING id',
        name,
        role,
        'unused',
    )
    made = {'id': user_id, 'session': f'{name}-session'}
    await database.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES ($1, $2, NOW() + INTERVAL '1 day')",
        made['session'],
        user_id,
    )
    for scope in ('mcp', 'full', 'worker', 'follower'):
        made[scope] = secrets.token_urlsafe(32)
        await database.execute(
            'INSERT INTO api_tokens (user_id, token, name, scope) VALUES ($1, $2, $3, $4)',
            user_id,
            hash_token(made[scope]),
            f'{scope} token',
            scope,
        )
    return made


@contextlib.asynccontextmanager
async def serving(app):
    """`app` on a loopback port in this event loop; yields its base address."""
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='error'))
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    await asyncio.sleep(0.01)
            yield f'http://127.0.0.1:{listener.getsockname()[1]}'
        finally:
            server.should_exit = True
            await task


@contextlib.asynccontextmanager
async def door(database):
    """The real app, reading the test database; yields the address of /mcp."""
    with pytest.MonkeyPatch.context() as patch:
        for module in (
            auth,
            tokens,
            users,
            access,
            mcp_door,
            assistant_calls_router,
            database_module,
        ):
            patch.setattr(module, 'db', database)
        async with serving(main.app) as base:
            yield base + '/mcp'


@contextlib.asynccontextmanager
async def sdk_client(url, token, mode):
    """The MCP SDK's own client: mode 'auto' speaks 2026-07-28 (it asks server/discover first), 'legacy' 2025-11-25."""
    async with httpx2.AsyncClient(headers={'Authorization': f'Bearer {token}'}, timeout=30) as http:
        async with Client(streamable_http_client(url, http_client=http), mode=mode) as client:
            yield client


def messages(response):
    """The JSON-RPC messages of an answer, JSON or an SSE stream. No answer of a stateless server names a session."""
    assert 'mcp-session-id' not in response.headers
    if response.headers.get('content-type', '').startswith('text/event-stream'):
        return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
    return [response.json()]


async def rpc(http, url, token, message, version=None):
    """POST one JSON-RPC message the way a client of `version` does; the answer to it."""
    headers = {'Authorization': f'Bearer {token}', 'Accept': ACCEPT}
    if version == MODERN:
        params = message.setdefault('params', {})
        params['_meta'] = {
            'io.modelcontextprotocol/protocolVersion': MODERN,
            'io.modelcontextprotocol/clientCapabilities': {},
            'io.modelcontextprotocol/clientInfo': {'name': 'raw-test-client', 'version': '1'},
        }
        headers['Mcp-Method'] = message['method']
        if 'name' in params or 'uri' in params:
            headers['Mcp-Name'] = params.get('name', params.get('uri'))
    if version is not None:
        headers['MCP-Protocol-Version'] = version
    response = await http.post(url, json=message, headers=headers)
    if 'id' not in message:
        assert response.status_code == 202, response.text
        return None
    [answer] = [item for item in messages(response) if item.get('id') == message['id']]
    return answer


async def older_client(http, url, token, calls=1):
    """A 2025-06-18 client, by hand (the SDK's client offers only 2025-11-25 in a handshake): initialize, then list
    and call. Returns the three answers."""
    started = await rpc(
        http,
        url,
        token,
        {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {'protocolVersion': OLDER, 'capabilities': {}, 'clientInfo': {'name': 'older', 'version': '1'}},
        },
    )
    await rpc(http, url, token, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, OLDER)
    listed = await rpc(http, url, token, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, OLDER)
    called = None
    for number in range(calls):
        call = {'name': 'list_datasets', 'arguments': {'limit': 5}}
        called = await rpc(
            http,
            url,
            token,
            {'jsonrpc': '2.0', 'id': 3 + number, 'method': 'tools/call', 'params': call},
            OLDER,
        )
    return started, listed, called


def test_each_revision_discovers_lists_and_calls(registry_database, assistant):
    async def scenario(database):
        user = await account(database)
        expected_tools = [(tool.name, tool.description, tool.input_schema) for tool in assistant.tool_list]
        async with door(database) as url:
            # 2026-07-28: no handshake; the client asks server/discover first.
            async with sdk_client(url, user['mcp'], 'auto') as client:
                assert client.session.protocol_version == MODERN
                assert client.session.server_info.name == 'freedomridge'
                assert client.session.server_info.version == VERSION
                assert client.session.discover_result.instructions == assistant.instructions()
                listed = await client.list_tools()
                assert [(t.name, t.description, t.input_schema) for t in listed.tools] == expected_tools
                assert all(tool.annotations.read_only_hint for tool in listed.tools)
                called = await client.call_tool('list_datasets', {'limit': 5})
                assert not called.is_error
                assert called.structured_content == {'datasets': [], 'total': 0, 'next_offset': None}

            # 2025-11-25: the initialize handshake, then the same.
            async with sdk_client(url, user['mcp'], 'legacy') as client:
                started = client.session.initialize_result
                assert started.protocol_version == LEGACY
                assert (started.server_info.name, started.server_info.version) == ('freedomridge', VERSION)
                assert started.instructions == assistant.instructions()
                listed = await client.list_tools()
                assert [(t.name, t.description, t.input_schema) for t in listed.tools] == expected_tools
                assert all(tool.annotations.read_only_hint for tool in listed.tools)
                called = await client.call_tool('list_datasets', {'limit': 5})
                assert not called.is_error and called.structured_content['total'] == 0

            # 2025-06-18, by hand.
            async with httpx.AsyncClient(timeout=30) as http:
                started, listed, called = await older_client(http, url, user['mcp'])
            assert started['result']['protocolVersion'] == OLDER
            assert started['result']['serverInfo'] == {'name': 'freedomridge', 'version': VERSION}
            assert started['result']['instructions'] == assistant.instructions()
            tools = listed['result']['tools']
            assert [(t['name'], t['description'], t['inputSchema']) for t in tools] == expected_tools
            assert all(tool['annotations'] == {'readOnlyHint': True} for tool in tools)
            assert called['result']['isError'] is False
            assert called['result']['structuredContent']['total'] == 0

        # Every call ran as the token's user, through the MCP door, with the arguments as sent.
        assert (
            assistant.calls
            == [
                {'user_id': user['id'], 'door': 'mcp', 'name': 'list_datasets', 'arguments': {'limit': 5}},
            ]
            * 3
        )

    registry_database(scenario)


def test_the_2026_tool_list_may_be_cached_privately(registry_database, assistant):
    async def scenario(database):
        user = await account(database)
        async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
            listed = await rpc(http, url, user['mcp'], {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, MODERN)
            result = listed['result']
            assert result['ttlMs'] == mcp_door.TOOL_LIST_CACHE_MS > 0
            assert result['cacheScope'] == 'private'
            assert result['resultType'] == 'complete'
            assert result['_meta']['io.modelcontextprotocol/serverInfo'] == {'name': 'freedomridge', 'version': VERSION}
            # The handshake revisions have no such fields, and get none.
            started, older, _ = await older_client(http, url, user['mcp'])
            assert not {'ttlMs', 'cacheScope', 'resultType'} & set(older['result'])

    registry_database(scenario)


def test_another_origin_is_refused_before_anything_else(registry_database, assistant):
    async def scenario(database):
        user = await account(database)
        listing = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
            bearer = {'Authorization': f'Bearer {user["mcp"]}', 'Accept': ACCEPT, 'MCP-Protocol-Version': LEGACY}
            for headers in ({}, bearer, {'Cookie': f'session_id={user["session"]}'}):
                response = await http.post(url, json=listing, headers={**headers, 'Origin': ELSEWHERE})
                assert response.status_code == 403, response.text
            for method in ('GET', 'DELETE', 'PUT'):
                assert (await http.request(method, url, headers={'Origin': ELSEWHERE})).status_code == 403
            preflight = {'Origin': ELSEWHERE, 'Access-Control-Request-Method': 'POST'}
            response = await http.options(url, headers=preflight)
            assert response.status_code == 403
            assert not [name for name in response.headers if name.startswith('access-control-')]

            if main._ALLOWED_ORIGINS:
                # The server's own origins (ALLOWED_ORIGINS, as the CSRF guard reads it) pass with a token...
                own = sorted(main._ALLOWED_ORIGINS)[0]
                response = await http.post(url, json=listing, headers={**bearer, 'Origin': own})
                assert response.status_code == 200, response.text
                # ...but even they get no cross-origin permission: a preflight carries no token.
                response = await http.options(url, headers={**preflight, 'Origin': own})
                assert response.status_code >= 400
                assert not [name for name in response.headers if name.startswith('access-control-')]
        assert assistant.calls == []

    registry_database(scenario)


def test_a_browser_session_alone_is_401(registry_database, assistant):
    async def scenario(database):
        user = await account(database)
        listing = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        call = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'list_datasets'}}
        cookie = {'Cookie': f'session_id={user["session"]}', 'Accept': ACCEPT, 'MCP-Protocol-Version': LEGACY}
        async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
            # The session is a live one: the API takes it.
            me = await http.get(url.replace('/mcp', '/api/auth/me'), headers={'Cookie': cookie['Cookie']})
            assert me.status_code == 200 and me.json()['id'] == user['id']
            origins: list[dict[str, str]] = [{}] + [{'Origin': own} for own in sorted(main._ALLOWED_ORIGINS)[:1]]
            for origin in origins:
                for message in (listing, call):
                    response = await http.post(url, json=message, headers={**cookie, **origin})
                    assert response.status_code == 401, response.text
                    # Bearer, and no pointer to OAuth metadata: this server has none.
                    assert response.headers['www-authenticate'] == 'Bearer'
                    assert response.json()['error']['message'] == auth.MCP_TOKEN_NEEDED
        assert assistant.calls == []

    registry_database(scenario)


def test_only_an_mcp_token_opens_the_door(registry_database, assistant):
    async def scenario(database):
        user = await account(database)
        listing = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
            for scope in ('full', 'worker', 'follower'):
                response = await http.post(url, json=listing, headers={'Authorization': f'Bearer {user[scope]}'})
                assert response.status_code == 401, scope
                assert response.json()['error']['message'] == auth.MCP_TOKEN_NEEDED
            for header in (None, '', 'Bearer', f'Basic {user["mcp"]}', 'Bearer not-a-token'):
                headers = {} if header is None else {'Authorization': header}
                assert (await http.post(url, json=listing, headers=headers)).status_code == 401, header
            listed = await rpc(http, url, user['mcp'], listing, LEGACY)
            assert [tool['name'] for tool in listed['result']['tools']] == ['list_datasets', 'describe_dataset']
            # Nothing is kept between requests: no standing GET stream, no session to DELETE.
            for method in ('GET', 'DELETE'):
                response = await http.request(method, url, headers={'Authorization': f'Bearer {user["mcp"]}'})
                assert response.status_code == 405 and response.headers['allow'] == 'POST'
                assert response.json()['error']['message'] == 'The MCP server takes POST requests only'
            # An expired or revoked MCP token is one that doesn't exist.
            await database.execute("UPDATE api_tokens SET expires_at = NOW() - INTERVAL '1 minute' WHERE scope = 'mcp'")
            assert (
                await http.post(url, json=listing, headers={'Authorization': f'Bearer {user["mcp"]}'})
            ).status_code == 401
            await database.execute("UPDATE api_tokens SET expires_at = NULL, is_active = FALSE WHERE scope = 'mcp'")
            response = await http.post(url, json=listing, headers={'Authorization': f'Bearer {user["mcp"]}'})
            assert response.status_code == 401 and response.json()['error']['message'] == 'Invalid or expired token'

    registry_database(scenario)


def comparable(response):
    """An answer without what differs between two identical refusals: the error id of a 500."""
    try:
        body = response.json()
    except ValueError:
        return response.status_code, response.text
    if isinstance(body, dict):
        body = {key: value for key, value in body.items() if key != 'error_id'}
        if isinstance(body.get('detail'), str):
            body['detail'] = body['detail'].split(' [error_id=')[0]
    return response.status_code, body


def test_an_mcp_token_anywhere_else_is_a_token_that_does_not_exist(registry_database, assistant):
    """Every route of the app answers an MCP token exactly as it answers a token nobody made, and doesn't mark it used.
    (Reads included: a token pasted into an AI client opens /mcp and nothing else.)"""
    from backend.tests.test_access_enforcement_spec import app_routes

    async def scenario(database):
        user = await account(database)
        nobody = secrets.token_urlsafe(32)
        routes = sorted(app_routes())
        assert len(routes) > 100
        async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
            base = url.removesuffix('/mcp')
            for method, path in routes:
                address = base + re.sub(r'\{[^}]*\}', '1', path)
                answers = [
                    comparable(await http.request(method, address, headers={'Authorization': f'Bearer {token}'}))
                    for token in (user['mcp'], nobody)
                ]
                assert answers[0] == answers[1], (method, path, answers)
                if path in ('/api/auth/me', '/api/datasets', '/api/tokens'):
                    assert answers[0] == (401, {'detail': 'Invalid or expired token'}), (method, path)
            assert await database.fetchval("SELECT last_used_at FROM api_tokens WHERE scope = 'mcp'") is None
            # At /mcp it works, and is marked used.
            listed = await rpc(http, url, user['mcp'], {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, LEGACY)
            assert listed['result']['tools']
            assert await database.fetchval("SELECT last_used_at FROM api_tokens WHERE scope = 'mcp'") is not None

    registry_database(scenario)


def test_the_door_is_asked_on_every_request(registry_database, assistant):
    async def scenario(database):
        user = await account(database)
        sentence = 'The MCP server is switched off on this server'
        async with door(database) as url:
            async with sdk_client(url, user['mcp'], 'auto') as client:
                assert len((await client.list_tools()).tools) == 2
                # Taken out between two requests: refused at the next, whatever it asks. (The client keeps the tool
                # list five minutes, as the server says it may: asked again past its cache, it is refused too.)
                assistant.refusal = sentence
                assert len((await client.list_tools()).tools) == 2
                for ask in (
                    lambda: client.list_tools(cache_mode='bypass'),
                    lambda: client.call_tool('list_datasets', {}),
                ):
                    with pytest.raises(MCPError) as raised:
                        await ask()
                    assert raised.value.message == sentence
            async with httpx.AsyncClient(timeout=30) as http:
                for message, version in (
                    ({'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover'}, MODERN),
                    ({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, MODERN),
                    ({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}, LEGACY),
                    ({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'list_datasets'}}, LEGACY),
                ):
                    headers = {
                        'Authorization': f'Bearer {user["mcp"]}',
                        'Accept': ACCEPT,
                        'MCP-Protocol-Version': version,
                    }
                    response = await http.post(url, json=message, headers=headers)
                    assert response.status_code == 403, message
                    assert response.json() == {
                        'jsonrpc': '2.0',
                        'id': None,
                        'error': {'code': -32600, 'message': sentence},
                    }
                # Let in again: the next request goes through.
                assistant.refusal = None
                listed = await rpc(http, url, user['mcp'], {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list'}, LEGACY)
                assert len(listed['result']['tools']) == 2
        assert assistant.calls == []
        assert set(assistant.asked) == {(user['id'], 'mcp')}

    registry_database(scenario)


def test_an_mcp_token_is_made_listed_and_ended_like_the_others(registry_database, assistant):
    async def scenario(database):
        user = await account(database, 'mcp-maker')
        admin = await account(database, 'mcp-admin', role='admin')
        guest = await account(database, 'mcp-guest', role='guest')
        async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
            api = url.removesuffix('/mcp') + '/api'
            made = await http.post(
                api + '/tokens',
                json={'name': 'my AI client', 'scope': 'mcp'},
                headers={'Cookie': f'session_id={user["session"]}'},
            )
            assert made.status_code == 200, made.text
            assert made.json()['scope'] == 'mcp' and made.json()['token']
            raw = made.json()['token']
            listed = await http.get(api + '/tokens', headers={'Cookie': f'session_id={user["session"]}'})
            assert {token['name']: token['scope'] for token in listed.json()}['my AI client'] == 'mcp'
            refused = await http.post(
                api + '/tokens',
                json={'name': 'not for guests', 'scope': 'mcp'},
                headers={'Cookie': f'session_id={guest["session"]}'},
            )
            assert refused.status_code == 403
            assert (await rpc(http, url, raw, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, LEGACY))['result']

            # Deactivating the account ends its MCP tokens as it ends the others, and says how many.
            deactivated = await http.patch(
                f'{api}/users/{user["id"]}',
                json={'is_active': False},
                headers={'Cookie': f'session_id={admin["session"]}'},
            )
            assert deactivated.status_code == 200, deactivated.text
            assert deactivated.json()['revoked_tokens'] == {'full': 1, 'follower': 1, 'worker': 1, 'mcp': 2}
            for token in (raw, user['mcp']):
                response = await http.post(
                    url,
                    json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    headers={'Authorization': f'Bearer {token}'},
                )
                assert response.status_code == 401

    registry_database(scenario)


def test_each_tool_call_leaves_one_row_and_never_its_arguments(registry_database, assistant, caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario(database):
        user = await account(database)
        secret = 'SN-0042-SECRET'
        assistant.answers['list_datasets'] = {'ok': True, 'result': {'datasets': [{'name': secret}], 'total': 1}}
        error = {'code': 'refused', 'message': 'You have no access to this dataset', 'retryable': False}
        assistant.answers['describe_dataset'] = {'ok': False, 'error': error}
        async with door(database) as url:
            async with sdk_client(url, user['mcp'], 'auto') as client:
                assert not (await client.call_tool('list_datasets', {'search': secret})).is_error
                assert (await client.call_tool('describe_dataset', {'dataset': 4, 'search': secret})).is_error
                with pytest.raises(MCPError):
                    await client.call_tool('no_such_tool', {'search': secret})
            async with sdk_client(url, user['mcp'], 'legacy') as client:
                assert not (await client.call_tool('list_datasets', {'dataset': True})).is_error
        rows = await database.fetch('SELECT * FROM assistant_calls ORDER BY id')
        assert [
            (row['user_id'], row['door'], row['tool'], row['dataset_id'], row['ok'], row['error_code']) for row in rows
        ] == [
            (user['id'], 'mcp', 'list_datasets', None, True, None),
            (user['id'], 'mcp', 'describe_dataset', 4, False, 'refused'),
            (user['id'], 'mcp', 'list_datasets', None, True, None),
        ]
        for row in rows:
            assert row['started_at'].tzinfo is not None and row['duration_ms'] >= 0
            assert secret not in str(dict(row)) and user['mcp'] not in str(dict(row))
        # The token is never passed on, and is in no log line.
        assert all(set(call) == {'user_id', 'door', 'name', 'arguments'} for call in assistant.calls)
        assert user['mcp'] not in caplog.text

    registry_database(scenario)


async def logged(database, user_id, tool, *, days_ago=0.0, dataset_id=None, error_code=None):
    return await database.fetchval(
        'INSERT INTO assistant_calls (user_id, door, tool, dataset_id, started_at, duration_ms, ok, error_code) '
        "VALUES ($1, 'mcp', $2, $3, NOW() - $4::float * INTERVAL '1 day', 12, $5, $6) RETURNING id",
        user_id,
        tool,
        dataset_id,
        days_ago,
        error_code is None,
        error_code,
    )


def test_administrators_read_the_recent_calls(registry_database, assistant):
    async def scenario(database):
        admin = await account(database, 'calls-admin', role='admin')
        user = await account(database, 'calls-user')
        gone = await account(database, 'calls-gone')
        ids = [
            await logged(database, user['id'], 'list_datasets', days_ago=3),
            await logged(database, gone['id'], 'describe_dataset', days_ago=2, dataset_id=1, error_code='refused'),
            await logged(database, user['id'], 'describe_dataset', days_ago=1, dataset_id=2),
        ]
        await database.execute('DELETE FROM users WHERE id = $1', gone['id'])
        async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
            calls = url.removesuffix('/mcp') + '/api/admin/assistant/calls'
            as_admin = {'Cookie': f'session_id={admin["session"]}'}
            first = await http.get(calls, params={'limit': 2}, headers=as_admin)
            assert first.status_code == 200, first.text
            page = first.json()
            assert (page['total'], page['offset'], page['next_offset']) == (3, 0, 2)
            assert [call['id'] for call in page['calls']] == [ids[2], ids[1]]
            assert set(page['calls'][0]) == {
                'id',
                'user_id',
                'username',
                'door',
                'tool',
                'dataset_id',
                'started_at',
                'duration_ms',
                'ok',
                'error_code',
            }
            assert page['calls'][0]['username'] == 'calls-user' and page['calls'][0]['dataset_id'] == 2
            # A deleted user's row stays, with nobody on it.
            assert (page['calls'][1]['user_id'], page['calls'][1]['username']) == (None, None)
            assert page['calls'][1]['error_code'] == 'refused' and page['calls'][1]['ok'] is False
            last = (await http.get(calls, params={'limit': 2, 'offset': 2}, headers=as_admin)).json()
            assert ([call['id'] for call in last['calls']], last['next_offset']) == ([ids[0]], None)
            assert (await http.get(calls, headers=as_admin)).json()['total'] == 3
            for limit in (0, 201):
                assert (await http.get(calls, params={'limit': limit}, headers=as_admin)).status_code == 422
            assert (await http.get(calls, headers={'Cookie': f'session_id={user["session"]}'})).status_code == 403
            assert (await http.get(calls)).status_code == 401
            assert (await http.get(calls, headers={'Authorization': f'Bearer {admin["mcp"]}'})).status_code == 401

    registry_database(scenario)


def test_the_log_keeps_thirty_days_and_outlives_deletes(registry_database, assistant):
    async def scenario(database):
        user = await account(database)
        old = await logged(database, user['id'], 'list_datasets', days_ago=30.01)
        kept = await logged(database, user['id'], 'describe_dataset', days_ago=29.9, dataset_id=1)
        assert await assistant_calls.prune(database.pool) == 1
        assert [row['id'] for row in await database.fetch('SELECT id FROM assistant_calls')] == [kept]
        assert old != kept
        # Deleting the dataset and the user doesn't fail because of the row, and the row stays.
        await database.execute('DELETE FROM datasets WHERE id = 1')
        await database.execute('DELETE FROM users WHERE id = $1', user['id'])
        row = await database.fetchrow('SELECT user_id, dataset_id, tool FROM assistant_calls')
        assert dict(row) == {'user_id': None, 'dataset_id': 1, 'tool': 'describe_dataset'}

    registry_database(scenario)


ERRORS = {
    'not_allowed': 'Your account may not use the assistant on this server',
    'refused': 'You have no access to this dataset',
    'not_found': 'No such dataset',
    'invalid': 'limit: must be at most 50',
    'busy': 'The server is at its limit for you right now; try again shortly',
}


def test_call_tool_answers_become_tool_results(registry_database, assistant):
    async def scenario(database):
        user = await account(database)
        result = {'datasets': [{'id': 4, 'name': 'field-trials', 'level': 'edit'}], 'total': 1, 'next_offset': None}
        async with door(database) as url:
            async with sdk_client(url, user['mcp'], 'auto') as client:
                assistant.answers['list_datasets'] = {'ok': True, 'result': result}
                called = await client.call_tool('list_datasets', {'search': 'field'})
                assert not called.is_error
                assert called.structured_content == result
                assert [json.loads(block.text) for block in called.content] == [result]

                for code, message in ERRORS.items():
                    error = {'code': code, 'message': message, 'retryable': code == 'busy'}
                    assistant.answers['describe_dataset'] = {'ok': False, 'error': error}
                    called = await client.call_tool('describe_dataset', {'dataset': 4})
                    assert called.is_error, code
                    assert called.structured_content == {'error': error}
                    [text] = [block.text for block in called.content]
                    if code == 'busy':
                        assert text.startswith(message) and 'tried again' in text
                    else:
                        assert text == message

                # A tool the core says doesn't exist, and a name not in the list: the protocol's own error.
                assistant.answers['describe_dataset'] = {
                    'ok': False,
                    'error': {'code': 'unknown_tool', 'message': 'No tool describe_dataset', 'retryable': False},
                }
                for name in ('describe_dataset', 'no_such_tool'):
                    with pytest.raises(MCPError) as raised:
                        await client.call_tool(name, {'dataset': 4})
                    assert raised.value.code == -32602

            # An error answer is a tool result in the handshake revisions too: the connection carries on.
            assistant.answers['describe_dataset'] = {
                'ok': False,
                'error': {'code': 'busy', 'message': ERRORS['busy'], 'retryable': True},
            }
            async with sdk_client(url, user['mcp'], 'legacy') as client:
                called = await client.call_tool('describe_dataset', {'dataset': 4})
                assert called.is_error and called.structured_content['error']['code'] == 'busy'
                assert not (await client.call_tool('list_datasets', {})).is_error

            # Arguments that aren't an object: the protocol's own error, before the core is asked.
            async with httpx.AsyncClient(timeout=30) as http:
                for version in (MODERN, LEGACY):
                    params = {'name': 'list_datasets', 'arguments': ['not', 'an', 'object']}
                    request = {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call', 'params': params}
                    answer = await rpc(http, url, user['mcp'], request, version)
                    assert answer['error']['code'] == -32602, version

        assert [call['name'] for call in assistant.calls].count('no_such_tool') == 0
        assert all(isinstance(call['arguments'], dict) for call in assistant.calls)
        assert {call['user_id'] for call in assistant.calls} == {user['id']}

    registry_database(scenario)


def test_a_server_fault_in_a_call_is_a_tool_result(registry_database, assistant):
    """call_tool raises only for a server fault: the model gets a tool result marked as an error, with none of the
    fault's text, and the connection carries on. The call is logged as failed."""

    async def fails(**call):
        raise RuntimeError('inner GET /api/datasets answered 500: the fault itself')

    async def scenario(database):
        user = await account(database)
        async with door(database) as url:
            async with sdk_client(url, user['mcp'], 'auto') as client:
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(assistant, 'call_tool', fails)
                    called = await client.call_tool('list_datasets', {})
                    assert called.is_error
                    assert called.structured_content == {
                        'error': {'code': 'failed', 'message': mcp_door.SERVER_FAILED, 'retryable': False},
                    }
                    assert [block.text for block in called.content] == [mcp_door.SERVER_FAILED]
                assert len((await client.list_tools(cache_mode='bypass')).tools) == 2
        row = await database.fetchrow('SELECT tool, ok, error_code FROM assistant_calls')
        assert dict(row) == {'tool': 'list_datasets', 'ok': False, 'error_code': 'failed'}

    registry_database(scenario)


class Blocking:
    """A call_tool that runs until released, counting the calls inside it and seeing each one cancelled."""

    def __init__(self):
        self.inside = 0
        self.cancelled = 0
        self.release = asyncio.Event()
        self.contexts: list = []
        self.connections_in_use: list[int] = []

    async def call_tool(self, *, user_id, door, name, arguments):
        self.inside += 1
        self.contexts.append(request_limits.request_context.get())
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.inside -= 1
        return {'ok': True, 'result': {'released': True}}


async def wait_until(condition, seconds=10.0):
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.01)


def test_a_users_fifth_call_at_once_is_busy_and_the_others_go_on(registry_database, assistant):
    """At most CALLS_AT_ONCE of one user's calls run at once in a process: the next is answered at once, `busy` and
    retryable, without waiting; another user's calls aren't held back; and once one finishes the next runs."""

    async def scenario(database):
        user = await account(database)
        other = await account(database, 'mcp-other')
        blocking = Blocking()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(assistant, 'call_tool', blocking.call_tool)
            async with door(database) as url, httpx.AsyncClient(timeout=30) as http:

                def call(token, number):
                    params = {'name': 'list_datasets', 'arguments': {}}
                    message = {'jsonrpc': '2.0', 'id': number, 'method': 'tools/call', 'params': params}
                    return asyncio.create_task(rpc(http, url, token, message, MODERN))

                running = [call(user['mcp'], number) for number in range(mcp_door.CALLS_AT_ONCE)]
                await wait_until(lambda: blocking.inside == mcp_door.CALLS_AT_ONCE)
                async with asyncio.timeout(5):  # answered at once, not queued behind the four
                    refused = await call(user['mcp'], 9)
                result = refused['result']
                assert result['isError'] is True
                assert result['structuredContent'] == {
                    'error': {'code': 'busy', 'message': mcp_door.TOO_MANY, 'retryable': True},
                }
                assert result['content'][0]['text'] == mcp_door.TOO_MANY + ' (This call may be tried again.)'
                # Another user isn't held back by this one's calls.
                theirs = call(other['mcp'], 10)
                await wait_until(lambda: blocking.inside == mcp_door.CALLS_AT_ONCE + 1)
                blocking.release.set()
                answers = await asyncio.gather(*running, theirs)
                assert all(answer['result']['structuredContent'] == {'released': True} for answer in answers)
                assert mcp_door._running == {}
                again = await call(user['mcp'], 11)
                assert again['result']['isError'] is False
        logged = await database.fetch('SELECT error_code FROM assistant_calls WHERE user_id = $1', user['id'])
        assert sorted(str(row['error_code']) for row in logged) == ['None'] * (mcp_door.CALLS_AT_ONCE + 1) + ['busy']

    registry_database(scenario)


def test_a_call_that_runs_too_long_is_stopped_and_its_work_cancelled(registry_database, assistant, monkeypatch):
    monkeypatch.setattr(mcp_door, 'CALL_SECONDS', 0.5)

    async def scenario(database):
        user = await account(database)
        blocking = Blocking()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(assistant, 'call_tool', blocking.call_tool)
            async with door(database) as url:
                async with sdk_client(url, user['mcp'], 'auto') as client:
                    called = await client.call_tool('describe_dataset', {'dataset': 1})
                    assert called.is_error
                    assert called.structured_content == {
                        'error': {'code': 'timeout', 'message': mcp_door.TOO_LONG, 'retryable': False},
                    }
                    assert [block.text for block in called.content] == [mcp_door.TOO_LONG]
                    # The work was cancelled, not left running, and the user's count is back to nothing.
                    assert (blocking.cancelled, blocking.inside, mcp_door._running) == (1, 0, {})
                    patch.setattr(assistant, 'call_tool', FakeAssistant().call_tool)
                    assert not (await client.call_tool('list_datasets', {})).is_error
        row = await database.fetchrow(
            "SELECT ok, error_code, duration_ms FROM assistant_calls WHERE tool = 'describe_dataset'",
        )
        assert (row['ok'], row['error_code']) == (False, 'timeout') and row['duration_ms'] >= 500

    registry_database(scenario)


def test_the_door_holds_no_connection_and_no_guest_slot_while_a_tool_runs(registry_database, assistant, monkeypatch):
    """A guest's /mcp request in public mode (guest slots and statement budgets on) takes no guest slot of its own,
    and the door holds no pooled connection while call_tool runs: the tool's own requests take what they need."""
    monkeypatch.setenv('DEMO_MODE', 'true')

    async def scenario(database):
        guest = await account(database, 'mcp-guest', role='guest')
        blocking = Blocking()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(assistant, 'call_tool', blocking.call_tool)
            async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
                params = {'name': 'list_datasets', 'arguments': {}}
                message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params}
                call = asyncio.create_task(rpc(http, url, guest['mcp'], message, MODERN))
                await wait_until(lambda: blocking.inside == 1)
                pool = database.pool
                assert pool.get_size() - pool.get_idle_size() == 0, 'a connection is held while the tool runs'
                context = blocking.contexts[0]
                assert context is not None and context.user['id'] == guest['id']
                assert (context.slots, context.semaphore) == (None, None)
                assert guest['id'] not in request_limits._guest_slots
                blocking.release.set()
                assert (await call)['result']['isError'] is False

    registry_database(scenario)


def test_tools_reach_the_app_only_through_call_tool_on_scopes_of_their_own(registry_database):
    """With the real assistant core: every request that reaches the app while a tool runs is either the client's own
    request to /mcp or one the core built for the tool, a new scope carrying the user as the core's Identity, and
    nothing of the outer request: no token, no cookie, not the door's own key."""

    async def scenario(database):
        user = await account(database)
        await database.execute('UPDATE assistant_settings SET mcp_enabled = TRUE')
        await database.execute('INSERT INTO assistant_users (user_id, allowed) VALUES ($1, TRUE)', user['id'])
        seen: list = []
        stack = main.app.middleware_stack or main.app.build_middleware_stack()

        async def spy(scope, receive, send):
            seen.append(scope)
            await stack(scope, receive, send)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(main.app, 'middleware_stack', spy)
            async with door(database) as url:
                async with sdk_client(url, user['mcp'], 'auto') as client:
                    called = await client.call_tool('list_datasets', {})
                    assert not called.is_error, called.content
        outer = [scope for scope in seen if scope['path'] == '/mcp']
        inner = [scope for scope in seen if scope['path'] != '/mcp']
        assert outer and inner
        for scope in inner:
            assert not any(scope is each for each in outer)
            assert scope['path'].startswith('/api/')
            identity = scope_identity(scope)
            assert identity is not None and (identity.user_id, identity.door) == (user['id'], 'mcp')
            assert 'fridge.mcp_user' not in scope
            assert not {name for name, _ in scope['headers']} & {b'authorization', b'cookie'}

    registry_database(scenario)


def test_the_door_offers_tools_only(registry_database):
    """Discovery in each revision lists no capability but tools, and a request for any other feature (resources,
    prompts, completions, logging, subscriptions) gets the protocol's own answer for a method the server doesn't
    have. The instructions are the assistant's fixed text, with nothing of a dataset in them."""

    async def scenario(database):
        user = await account(database)
        await database.execute('UPDATE assistant_settings SET mcp_enabled = TRUE')
        await database.execute('INSERT INTO assistant_users (user_id, allowed) VALUES ($1, TRUE)', user['id'])
        await database.execute(
            "INSERT INTO datasets (id, name, description) VALUES (30, 'orchard-secret-plot', 'row 12 blight')",
        )
        others = {
            'resources/list': {},
            'resources/templates/list': {},
            'resources/read': {'uri': 'file:///etc/hosts'},
            'prompts/list': {},
            'prompts/get': {'name': 'anything'},
            'completion/complete': {
                'ref': {'type': 'ref/prompt', 'name': 'anything'},
                'argument': {'name': 'a', 'value': 'b'},
            },
        }
        async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
            token = user['mcp']
            discovered = await rpc(http, url, token, {'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover'}, MODERN)
            offered = {MODERN: discovered['result']}
            for version in (LEGACY, OLDER):
                started = await rpc(
                    http,
                    url,
                    token,
                    {
                        'jsonrpc': '2.0',
                        'id': 2,
                        'method': 'initialize',
                        'params': {
                            'protocolVersion': version,
                            'capabilities': {},
                            'clientInfo': {'name': 'c', 'version': '1'},
                        },
                    },
                )
                assert started['result']['protocolVersion'] == version
                offered[version] = started['result']
            for version, result in offered.items():
                assert set(result['capabilities']) == {'tools'}, (version, result['capabilities'])
                instructions = result['instructions']
                assert instructions == assistant_module.instructions(), version
                assert 'orchard-secret-plot' not in instructions and 'blight' not in instructions, (
                    'the instructions are fixed text: nothing of a dataset is in them'
                )

            asked = dict(others, **{'subscriptions/listen': {'notifications': {'toolsListChanged': True}}})
            for number, (method, params) in enumerate(asked.items(), start=10):
                answer = await rpc(
                    http,
                    url,
                    token,
                    {'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params},
                    MODERN,
                )
                assert answer['error']['code'] == -32601, (MODERN, method, answer)
            for version in (LEGACY, OLDER):
                legacy = dict(others, **{'logging/setLevel': {'level': 'debug'}})
                for number, (method, params) in enumerate(legacy.items(), start=30):
                    message = {'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params}
                    answer = await rpc(http, url, token, message, version)
                    assert answer['error']['code'] == -32601, (version, method, answer)

    registry_database(scenario)


@pytest.fixture(autouse=True)
def busy_rows_unlogged(monkeypatch):
    """Each test starts with no `busy` refusal logged lately (the minute's mark is kept per process)."""
    monkeypatch.setattr(mcp_door, '_busy_logged', {})


def test_a_body_over_the_cap_is_refused_however_it_comes(registry_database, assistant):
    """At most MAX_BODY_BYTES is read: a larger Content-Length is refused before reading, and a body without one
    (chunked), or longer than its Content-Length says, is cut off while it is read. 413, as a JSON-RPC error."""
    refused = {
        'jsonrpc': '2.0',
        'id': None,
        'error': {
            'code': -32600,
            'message': f'The MCP server takes requests of at most {mcp_door.MAX_BODY_BYTES // 1024} KB',
        },
    }
    big = {'name': 'list_datasets', 'arguments': {'search': 'x' * (mcp_door.MAX_BODY_BYTES + 10)}}
    big_call = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': big}

    async def scenario(database):
        user = await account(database)
        headers = {'Authorization': f'Bearer {user["mcp"]}', 'Accept': ACCEPT, 'MCP-Protocol-Version': LEGACY}
        async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
            response = await http.post(url, json=big_call, headers=headers)
            assert response.status_code == 413 and response.json() == refused

            async def chunks():
                encoded = json.dumps(big_call).encode()
                for start in range(0, len(encoded), 16 * 1024):
                    yield encoded[start : start + 16 * 1024]

            response = await http.post(url, content=chunks(), headers={**headers, 'Content-Type': 'application/json'})
            assert 'content-length' not in response.request.headers
            assert response.status_code == 413 and response.json() == refused

            # Just under the cap goes through.
            small = {'name': 'list_datasets', 'arguments': {'search': 'x' * (mcp_door.MAX_BODY_BYTES - 1024)}}
            message = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': small}
            assert (await rpc(http, url, user['mcp'], message, LEGACY))['result']['isError'] is False

        # A Content-Length that says less than the body: counted while read all the same.
        sent: list[dict] = []
        pieces = [json.dumps(big_call).encode()[start : start + 64 * 1024] for start in range(0, 300 * 1024, 64 * 1024)]
        messages = [{'type': 'http.request', 'body': piece, 'more_body': True} for piece in pieces]
        scope = {
            'type': 'http',
            'method': 'POST',
            'path': '/mcp',
            'query_string': b'',
            'headers': [
                (b'authorization', f'Bearer {user["mcp"]}'.encode()),
                (b'content-type', b'application/json'),
                (b'content-length', b'10'),
            ],
        }

        async def receive():
            return messages.pop(0) if messages else {'type': 'http.disconnect'}

        async def send(message):
            sent.append(message)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(auth, 'db', database)
            await mcp_door.endpoint(scope, receive, send)
        assert sent[0]['status'] == 413
        assert json.loads(b''.join(item.get('body', b'') for item in sent[1:])) == refused
        assert len(assistant.calls) == 1

    registry_database(scenario)


def test_a_batch_is_refused_by_the_sdk_in_every_revision_it_answers(registry_database, assistant):
    """A JSON-RPC batch (an array body) is refused (400, a JSON-RPC error) in every revision the server answers, the
    older ones the SDK also speaks included: one request can never carry several calls."""

    async def scenario(database):
        user = await account(database)
        call = {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'list_datasets', 'arguments': {}}}
        batch = [dict(call, id=number) for number in range(1, 4)]
        async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
            for version in (MODERN, LEGACY, OLDER, '2025-03-26', '2024-11-05', None):
                headers = {'Authorization': f'Bearer {user["mcp"]}', 'Accept': ACCEPT}
                if version:
                    headers['MCP-Protocol-Version'] = version
                if version == MODERN:
                    headers['Mcp-Method'] = 'tools/call'
                response = await http.post(url, json=batch, headers=headers)
                assert response.status_code == 400, (version, response.text)
                assert response.json()['error']['code'] in (-32600, -32602), version
        assert assistant.calls == []

    registry_database(scenario)


def test_a_call_whose_client_goes_away_is_cancelled_and_logged(registry_database, assistant):
    async def scenario(database):
        user = await account(database)
        blocking = Blocking()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(assistant, 'call_tool', blocking.call_tool)
            async with door(database) as url:
                async with httpx.AsyncClient(timeout=30) as http:
                    params = {'name': 'describe_dataset', 'arguments': {'dataset': 2}}
                    message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params}
                    call = asyncio.create_task(rpc(http, url, user['mcp'], message, MODERN))
                    await wait_until(lambda: blocking.inside == 1)
                    call.cancel()  # the client goes away mid-call
                    with contextlib.suppress(asyncio.CancelledError):
                        await call
                await wait_until(lambda: blocking.cancelled == 1)

                async def logged():
                    return await database.fetchrow('SELECT * FROM assistant_calls')

                row = None
                async with asyncio.timeout(10):
                    while row is None:
                        row = await logged()
                        await asyncio.sleep(0.05)
        assert (row['tool'], row['dataset_id'], row['ok'], row['error_code']) == (
            'describe_dataset',
            2,
            False,
            'cancelled',
        )
        assert mcp_door._running == {}

    registry_database(scenario)


def test_a_log_that_fails_to_write_never_fails_the_call(registry_database, assistant, caplog):
    async def fails(*args, **kwargs):
        raise ConnectionError('the log is unreachable')

    async def scenario(database):
        user = await account(database)
        assistant.answers['list_datasets'] = {'ok': True, 'result': {'datasets': [], 'total': 0}}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(assistant_calls, 'record', fails)
            async with door(database) as url, sdk_client(url, user['mcp'], 'auto') as client:
                called = await client.call_tool('list_datasets', {})
        assert not called.is_error and called.structured_content == {'datasets': [], 'total': 0}
        assert 'The assistant call log could not record a call of list_datasets' in caplog.text

    registry_database(scenario)


def test_the_process_takes_a_bounded_number_of_calls_at_once_from_all_users(registry_database, assistant, monkeypatch):
    monkeypatch.setattr(mcp_door, 'CALLS_PER_PROCESS', 3)

    async def scenario(database):
        first = await account(database, 'mcp-first')
        second = await account(database, 'mcp-second')
        blocking = Blocking()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(assistant, 'call_tool', blocking.call_tool)
            async with door(database) as url, httpx.AsyncClient(timeout=30) as http:

                def call(token, number):
                    params = {'name': 'list_datasets', 'arguments': {}}
                    message = {'jsonrpc': '2.0', 'id': number, 'method': 'tools/call', 'params': params}
                    return asyncio.create_task(rpc(http, url, token, message, MODERN))

                running = [call(first['mcp'], 1), call(first['mcp'], 2), call(second['mcp'], 3)]
                await wait_until(lambda: blocking.inside == 3)
                refused = (await call(second['mcp'], 4))['result']
                assert refused['structuredContent'] == {
                    'error': {'code': 'busy', 'message': mcp_door.PROCESS_FULL, 'retryable': True},
                }
                blocking.release.set()
                await asyncio.gather(*running)

    registry_database(scenario)


def test_a_loop_of_busy_refusals_leaves_one_row_a_minute(registry_database, assistant, monkeypatch):
    monkeypatch.setattr(mcp_door, 'CALLS_AT_ONCE', 1)

    async def scenario(database):
        user = await account(database)
        blocking = Blocking()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(assistant, 'call_tool', blocking.call_tool)
            async with door(database) as url, httpx.AsyncClient(timeout=30) as http:
                params = {'name': 'list_datasets', 'arguments': {}}
                message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params}
                running = asyncio.create_task(rpc(http, url, user['mcp'], message, MODERN))
                await wait_until(lambda: blocking.inside == 1)
                for number in range(2, 22):
                    answer = await rpc(http, url, user['mcp'], dict(message, id=number), MODERN)
                    assert answer['result']['structuredContent']['error']['code'] == 'busy'
                blocking.release.set()
                await running
        codes = [
            row['error_code'] for row in await database.fetch('SELECT error_code FROM assistant_calls ORDER BY id')
        ]
        assert sorted(codes, key=str) == sorted(['busy', None], key=str)

    registry_database(scenario)


def test_the_request_context_reaches_the_tools_requests_through_the_deadline(registry_database, monkeypatch):
    """With the real core, for a guest in public mode: the call runs in the deadline's own task, and every in-process
    request the tool makes still starts with the /mcp request's context (the request limits' context, its user the
    guest, so the guest statement limit applies), sees the user's call counted, and is itself signed in as the guest,
    which gives it the guest's budgets."""
    monkeypatch.setenv('DEMO_MODE', 'true')

    async def scenario(database):
        guest = await account(database, 'mcp-context-guest', role='guest')
        await database.execute('UPDATE assistant_settings SET mcp_enabled = TRUE')
        await database.execute('INSERT INTO assistant_users (user_id, allowed) VALUES ($1, TRUE)', guest['id'])
        entered: list = []
        signed_in: list = []
        stack = main.app.middleware_stack or main.app.build_middleware_stack()
        original = request_limits.set_request_user

        async def spy(scope, receive, send):
            if scope['path'] != '/mcp':
                entered.append((request_limits.request_context.get(), dict(mcp_door._running)))
            await stack(scope, receive, send)

        async def recorded(user):
            context = request_limits.request_context.get()
            signed_in.append((context.scope.get('path') if context else None, user['id'], user['role']))
            await original(user)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(main.app, 'middleware_stack', spy)
            patch.setattr(auth, 'set_request_user', recorded)
            async with door(database) as url, sdk_client(url, guest['mcp'], 'auto') as client:
                called = await client.call_tool('list_datasets', {})
                assert not called.is_error, called.content
        assert entered
        for context, running in entered:
            assert context is not None, 'the in-process request lost the /mcp request context'
            assert context.scope['path'] == '/mcp' and context.user['id'] == guest['id']
            assert running == {guest['id']: 1}
        inner = [each for each in signed_in if each[0] != '/mcp']
        assert inner and all(each[1:] == (guest['id'], 'guest') for each in inner)

    registry_database(scenario)


def test_a_timeout_inside_the_call_is_a_server_fault_not_the_deadline(registry_database, assistant):
    async def times_out(**call):
        raise asyncio.TimeoutError('a statement inside the tool timed out')

    async def scenario(database):
        user = await account(database)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(assistant, 'call_tool', times_out)
            async with door(database) as url, sdk_client(url, user['mcp'], 'auto') as client:
                called = await client.call_tool('list_datasets', {})
        assert called.structured_content == {
            'error': {'code': 'failed', 'message': mcp_door.SERVER_FAILED, 'retryable': False},
        }
        assert mcp_door._running == {}
        row = await database.fetchrow('SELECT error_code FROM assistant_calls')
        assert row['error_code'] == 'failed'

    registry_database(scenario)


class RoundRobin:
    """A reverse proxy in front of several backends: each request goes to the next one in turn. `served` records
    (backend index, JSON-RPC method) for every request."""

    def __init__(self, backends):
        self.backends = backends
        self.served: list[tuple[int, str]] = []
        self.http = httpx.AsyncClient(timeout=60)

    async def __call__(self, scope, receive, send):
        body = b''
        while True:
            message = await receive()
            body += message.get('body', b'')
            if not message.get('more_body'):
                break
        index = len(self.served) % len(self.backends)
        self.served.append((index, json.loads(body)['method'] if body else scope['method']))
        headers = [(key, value) for key, value in scope['headers'] if key not in (b'host', b'content-length')]
        request = self.http.build_request(
            scope['method'],
            self.backends[index] + scope['path'],
            headers=headers,
            content=body,
        )
        response = await self.http.send(request, stream=True)
        try:
            passed = [
                (key.encode(), value.encode())
                for key, value in response.headers.items()
                if key not in ('content-length', 'transfer-encoding', 'connection')
            ]
            await send({'type': 'http.response.start', 'status': response.status_code, 'headers': passed})
            async for chunk in response.aiter_raw():
                await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
            await send({'type': 'http.response.body', 'body': b''})
        finally:
            await response.aclose()


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


async def backend_process(environment, log):
    """`uvicorn backend.main:app` as its own process on a free port; (process, base address) once it is healthy."""
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', str(port)],
        cwd=ROOT,
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f'http://127.0.0.1:{port}'
    async with httpx.AsyncClient(timeout=2) as http:
        for _ in range(240):
            if process.poll() is not None:
                break
            with contextlib.suppress(httpx.HTTPError):
                if (await http.get(base + '/api/health')).status_code == 200:
                    return process, base
            await asyncio.sleep(0.25)
    process.kill()
    process.wait()
    raise AssertionError(f'The backend on port {port} did not start: see {log.name}')


def test_two_processes_behind_one_address_answer_any_request(registry_database, tmp_path):
    """Each client's requests alternate between two backend processes, and every one succeeds: a handshake made with
    one process holds at the other, and both processes served tool calls of every revision."""

    async def scenario(database):
        user = await account(database)
        # The real core ships the door off: switch it on, and let this user in.
        await database.execute('UPDATE assistant_settings SET mcp_enabled = TRUE')
        await database.execute('INSERT INTO assistant_users (user_id, allowed) VALUES ($1, TRUE)', user['id'])
        namespace = await database.fetchval('SELECT current_schema()')
        dsn = os.environ['FRIDGE_TEST_POSTGRES_DSN']
        environment = {
            **os.environ,
            # The test's own schema in the disposable database, and a small pool per process.
            'DATABASE_URL': f'{dsn}{"&" if "?" in dsn else "?"}search_path={namespace},public',
            'DB_POOL_MIN_SIZE': '1',
            'DB_POOL_MAX_SIZE': '3',
            'FRIDGE_ENV': 'development',
            'DEMO_MODE': 'false',
            'TMPDIR': str(tmp_path),
            'FRIDGE_DATA_ROOT': str(tmp_path),
        }
        processes = []
        logs = [(tmp_path / f'backend-{number}.log').open('w') for number in range(2)]
        try:
            for log in logs:
                processes.append(await backend_process(environment, log))
            assert processes[0][0].pid != processes[1][0].pid
            proxy = RoundRobin([base for _, base in processes])
            async with serving(proxy) as address:
                url = address + '/mcp'
                served_by = {}

                async with sdk_client(url, user['mcp'], 'auto') as client:
                    assert client.session.protocol_version == MODERN
                    assert len((await client.list_tools()).tools) > 0
                    for _ in range(3):
                        called = await client.call_tool('list_datasets', {'limit': 5})
                        assert not called.is_error, called.content
                        assert called.structured_content['total'] >= 0
                served_by[MODERN], proxy.served = proxy.served, []

                async with sdk_client(url, user['mcp'], 'legacy') as client:
                    assert client.session.initialize_result.protocol_version == LEGACY
                    assert len((await client.list_tools()).tools) > 0
                    for _ in range(2):
                        called = await client.call_tool('list_datasets', {'limit': 5})
                        assert not called.is_error, called.content
                served_by[LEGACY], proxy.served = proxy.served, []

                async with httpx.AsyncClient(timeout=30) as http:
                    started, listed, called = await older_client(http, url, user['mcp'], calls=2)
                assert started['result']['protocolVersion'] == OLDER
                assert listed['result']['tools'] and called['result']['isError'] is False
                served_by[OLDER] = proxy.served

            for version, served in served_by.items():
                assert {index for index, method in served if method == 'tools/call'} == {0, 1}, (version, served)
        finally:
            for process, _ in processes:
                process.terminate()
            for process, _ in processes:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for log in logs:
                log.close()

    registry_database(scenario)
