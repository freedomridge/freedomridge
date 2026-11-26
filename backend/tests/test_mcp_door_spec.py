"""The MCP door: the `mcp` token kind, the MCP server at /mcp, and the log of the assistant's calls.

Written from the contract (m1-contract-mcp-door, leaning on the assistant core's section 1), not from the code, and
tested where an AI client meets it: real HTTP requests to /mcp on the app in the test process, with tokens made through
the API. The assistant core (`backend.assistant`) is the reference for what a tool answers.

What these tests rely on to speak MCP:
- the specification at modelcontextprotocol.io, revision 2026-07-28 (Streamable HTTP: POST only, the headers
  `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name`, `_meta` keys `io.modelcontextprotocol/protocolVersion`,
  `.../clientInfo` and `.../clientCapabilities` on every request, `server/discover` and its `supportedVersions`,
  `capabilities`, `instructions` and `_meta["io.modelcontextprotocol/serverInfo"]`; `ttlMs` and `cacheScope` on a
  cacheable result; tools with `annotations.readOnlyHint`; tool results with `content`, `structuredContent` and
  `isError`; -32602 for an unknown tool), and revisions 2025-11-25 and 2025-06-18 (an `initialize` handshake,
  `notifications/initialized` answered 202, the `MCP-Protocol-Version` header after it, and an optional
  `Mcp-Session-Id`);
- the installed SDK (`mcp` 2.3.0): `mcp.client.client.Client` in mode "auto" (it probes `server/discover`, so a
  modern server is spoken to in 2026-07-28) and in mode "legacy" (the `initialize` handshake, which offers 2025-11-25),
  over `mcp.client.streamable_http.streamable_http_client` with an `httpx2.AsyncClient` on the app's ASGI interface.
  The SDK's client offers no 2025-06-18 handshake, so that revision is spoken in raw HTTP.

Where the contract is silent these tests read it this way:
- A route of the app that answers a request with no valid token with a success (a public route) is compared by its
  status only, its body being free to vary between two requests (a time, a count).
- "Says that /mcp takes an MCP token and where one is made" is checked as the message naming an MCP token and the
  tokens page or the tokens route.
- The 403 for a foreign Origin is checked by its status; the 401, 403 (the door's refusals) and 405 answers as
  JSON-RPC error objects with id null.
- The log's facts are checked for calls made through /mcp; a call's `dataset_id` is checked where it names a dataset
  that exists. The 30-day housekeeping is the job service's hourly schedule entry, `assistant_calls.prune(pool)`;
  rows are aged by finding the log's table as the one with `door`, `tool` and `started_at` columns.
- "One connection" is one HTTP client, reused.

Not tested: two server processes behind one address (no way to run them behind one address in the test process
without the implementer's harness); a retryable error result (the assistant answers `busy` only under load).
"""

import json
import logging
import os
import re
import secrets
from typing import Any, Optional

import httpx
import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from backend import assistant, assistant_calls, main
from backend.passwords import hash_password
from backend.routers import datasets
from backend.tests import test_access_rule_spec, test_field_registry_postgres
from backend.tests.test_access_enforcement_spec import app_routes
from backend.tests.test_access_rule_spec import ADMIN, GUEST, OTHER_POWER, POWER, World
from backend.tests.test_dataset_deletion import finish as finish_deletion
from backend.version import VERSION

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

ADMIN_USER = {'id': ADMIN, 'role': 'admin', 'username': f'user-{ADMIN}'}
NEWEST, PREVIOUS, OLDEST = '2026-07-28', '2025-11-25', '2025-06-18'
MCP_OFF = 'The MCP server is switched off on this server'
NOT_LET_IN = 'Your account may not use the assistant on this server'
RETRY = ' (This call may be tried again.)'
CLIENT_INFO = {'name': 'spec-client', 'version': '1.0.0'}
FARM = 10
FOREIGN = 'https://pages.elsewhere.example'
UNTOUCHED = (
    'datasets',
    'samples',
    'dataset_fields',
    'views',
    'embedding_concepts',
    'dataset_versions',
    'dataset_access',
    'dataset_group_access',
    'groups',
    'group_members',
    'users',
)


# --- Speaking MCP in raw HTTP ---


def envelope(version: str) -> dict[str, Any]:
    return {
        'io.modelcontextprotocol/protocolVersion': version,
        'io.modelcontextprotocol/clientInfo': CLIENT_INFO,
        'io.modelcontextprotocol/clientCapabilities': {},
    }


def messages(response: httpx.Response) -> list[dict[str, Any]]:
    """The JSON-RPC messages of an answer: one JSON object, or the data of each SSE event."""
    if response.headers.get('content-type', '').startswith('text/event-stream'):
        found, data = [], []
        for line in [*response.text.splitlines(), '']:
            if line.startswith('data:'):
                data.append(line[5:].strip())
            elif not line.strip() and data:
                found.append(json.loads('\n'.join(data)))
                data = []
        return found
    return [response.json()] if response.content else []


class Door:
    """Requests to /mcp, every answer kept to check what no answer may carry."""

    def __init__(self, w: World):
        self.w = w
        self.answers: list[httpx.Response] = []
        self.next_id = 0

    async def send(
        self,
        body: Any,
        token: Optional[str],
        version: Optional[str] = NEWEST,
        headers: Optional[dict[str, str]] = None,
        method: str = 'POST',
        client: Optional[httpx.AsyncClient] = None,
    ) -> httpx.Response:
        sent = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        if token is not None:
            sent['Authorization'] = f'Bearer {token}'
        if version:
            sent['MCP-Protocol-Version'] = version
        if version == NEWEST and isinstance(body, dict) and isinstance(body.get('method'), str):
            sent['Mcp-Method'] = body['method']
            params = body.get('params')
            if body['method'] == 'tools/call' and isinstance(params, dict) and isinstance(params.get('name'), str):
                sent['Mcp-Name'] = params['name']
        sent.update(headers or {})
        content = None if body is None else json.dumps(body)
        response = await (client or self.w.client).request(method, '/mcp', headers=sent, content=content)
        self.answers.append(response)
        return response

    async def ask(
        self,
        rpc: str,
        params: Optional[dict[str, Any]],
        token: Optional[str],
        version: str = NEWEST,
        **options: Any,
    ) -> tuple[httpx.Response, Optional[dict[str, Any]]]:
        """A JSON-RPC request; the answer and its response message (None when there is none)."""
        self.next_id += 1
        given = dict(params or {})
        if version == NEWEST:
            given['_meta'] = envelope(NEWEST)
        body = {'jsonrpc': '2.0', 'id': self.next_id, 'method': rpc, 'params': given}
        response = await self.send(body, token, version, **options)
        found = [m for m in messages(response) if m.get('id') == self.next_id]
        return response, (found[0] if found else None)

    async def result(self, rpc: str, params: Optional[dict[str, Any]], token: str, version: str = NEWEST) -> Any:
        response, message = await self.ask(rpc, params, token, version)
        assert response.status_code == 200 and message is not None and 'result' in message, (rpc, response.text)
        return message['result']

    async def initialize(self, token: str, version: str) -> dict[str, Any]:
        """A handshake-era client's opening: initialize, then notifications/initialized."""
        params = {'protocolVersion': version, 'capabilities': {}, 'clientInfo': CLIENT_INFO}
        result = await self.result('initialize', params, token, version=version)
        assert result['protocolVersion'] == version, result
        sent = await self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, token, version)
        assert sent.status_code == 202, sent.text
        return result

    async def call(self, token: str, name: str, arguments: Any = None, version: str = NEWEST) -> dict[str, Any]:
        params: dict[str, Any] = {'name': name}
        if arguments is not None:
            params['arguments'] = arguments
        return await self.result('tools/call', params, token, version)


def refusal(response: httpx.Response, status: int) -> str:
    """A 401, 403 or 405 from /mcp: a JSON-RPC error object with id null; its words."""
    assert response.status_code == status, (status, response.status_code, response.text)
    body = response.json()
    assert body.get('jsonrpc') == '2.0' and body.get('id') is None and 'result' not in body, body
    assert isinstance(body['error']['code'], int) and isinstance(body['error']['message'], str), body
    return body['error']['message']


def unauthorized(response: httpx.Response) -> str:
    words = refusal(response, 401)
    assert response.headers.get('www-authenticate') == 'Bearer', response.headers
    return words


async def make_token(w: World, user_id: int, scope: str = 'mcp', name: Optional[str] = None) -> dict[str, Any]:
    made = await w.call(
        user_id,
        'POST',
        '/api/tokens',
        json={'name': name or f'{scope}-{secrets.token_hex(3)}', 'scope': scope},
    )
    assert made.status_code == 200, made.text
    return made.json()


async def settings(w: World, change: dict[str, Any]) -> None:
    response = await w.call(ADMIN, 'PATCH', '/api/admin/assistant', json=change)
    assert response.status_code == 200, response.text


async def open_door(w: World) -> None:
    await settings(w, {'mcp_enabled': True, 'roles': {'admin': True, 'power_user': True, 'guest': True}})


async def farm(w: World) -> None:
    await w.dataset(FARM)
    await w.database.execute("UPDATE datasets SET description = 'RESULT-MARK-5521 trials' WHERE id = $1", FARM)
    await w.share(FARM, POWER, 'view')
    samples = [{'filepath': f'farm-{i}.jpg', 'metadata': {'crop': 'corn' if i % 2 else 'soy'}} for i in range(4)]
    await datasets.add_samples(FARM, {'samples': samples}, user=ADMIN_USER)


async def snapshot(w: World) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for table in UNTOUCHED:
        state[table] = await w.database.fetchval(
            f'SELECT md5(string_agg(t::text, $$|$$ ORDER BY t::text)) FROM {table} t',
        )
    return state


async def tables_holding(w: World, text: str, but: tuple[str, ...] = ()) -> list[str]:
    """The tables of the test's schema with a row whose text holds `text`."""
    names = [
        row['table_name']
        for row in await w.database.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
            "AND table_type = 'BASE TABLE'",
        )
    ]
    found = []
    for name in names:
        if name in but:
            continue
        hit = await w.database.fetchval(f'SELECT 1 FROM "{name}" t WHERE strpos(t::text, $1) > 0 LIMIT 1', text)
        if hit:
            found.append(name)
    return found


# --- 1. The token ---


def test_an_mcp_token_is_made_and_listed_by_whoever_makes_ordinary_tokens(world):
    async def scenario(w):
        for user_id in (POWER, ADMIN):
            made = await make_token(w, user_id, name='for an AI client')
            assert made['scope'] == 'mcp' and made['token'], made
            listed = await w.call(user_id, 'GET', '/api/tokens')
            assert {row['id']: row['scope'] for row in listed.json()}[made['id']] == 'mcp', listed.text
        for scope in ('full', 'mcp'):
            refused = await w.call(GUEST, 'POST', '/api/tokens', json={'name': 'x', 'scope': scope})
            assert refused.status_code == 403, (scope, refused.text)

    world(scenario)


def concrete(path: str) -> str:
    return re.sub(r'\{[^}]+\}', '1', path)


def test_an_mcp_token_opens_no_other_route(world):
    """Every route of the app but /mcp, reads included, answers an mcp token exactly as a token that doesn't exist."""

    async def scenario(w):
        await farm(w)
        await open_door(w)
        token = (await make_token(w, POWER))['token']
        nobody = secrets.token_urlsafe(32)
        differ = []
        routes = sorted(route for route in app_routes() if not route[1].startswith('/mcp'))
        assert len(routes) > 100
        for method, path in routes:
            answers = []
            for given in (token, nobody):
                response = await w.client.request(
                    method,
                    concrete(path),
                    headers={'Authorization': f'Bearer {given}'},
                    json={} if method in ('POST', 'PUT', 'PATCH') else None,
                )
                answers.append((response.status_code, response.text))
            (status, text), (other_status, other_text) = answers
            if status != other_status or (not 200 <= status < 300 and text != other_text):
                differ.append(
                    f'{method} {path}: mcp token {status} {text[:100]} / '
                    f'no such token {other_status} {other_text[:100]}',
                )
        assert not differ, '\n'.join(differ)

    world(scenario)


def test_mcp_takes_an_mcp_token_and_nothing_else(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        mcp = await make_token(w, POWER)
        assert (await door.result('tools/list', {}, mcp['token']))['tools']
        # No token, a malformed header, an unknown, expired or revoked one: 401.
        expired, revoked = await make_token(w, POWER), await make_token(w, POWER)
        await w.database.execute(
            "UPDATE api_tokens SET expires_at = now() - interval '1 day' WHERE id = $1",
            expired['id'],
        )
        assert (await w.call(POWER, 'DELETE', f"/api/tokens/{revoked['id']}")).is_success
        for headers in (
            {},
            {'Authorization': f"Basic {mcp['token']}"},
            {'Authorization': 'Bearer'},
            {'Authorization': f"Bearer {mcp['token']} extra"},
            {'Authorization': f'Bearer {secrets.token_urlsafe(32)}'},
            {'Authorization': f"Bearer {expired['token']}"},
            {'Authorization': f"Bearer {revoked['token']}"},
        ):
            response = await door.send(
                {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {'_meta': envelope(NEWEST)}},
                None,
                headers=headers,
            )
            unauthorized(response)
        # An ordinary, a worker or a follower token: 401, saying /mcp takes an MCP token and where one is made.
        full = (await make_token(w, POWER, scope='full'))['token']
        worker = (await make_token(w, POWER, scope='worker'))['token']
        # Room in the users' ids for the follower's account (the test's users were given theirs by hand).
        await w.database.execute("SELECT setval(pg_get_serial_sequence('users', 'id'), 50000)")
        follower = await w.call(ADMIN, 'POST', '/api/followers', json={'name': 'lab'})
        assert follower.is_success, follower.text
        for kind, other in (('full', full), ('worker', worker), ('follower', follower.json()['token'])):
            response, _ = await door.ask('tools/list', {}, other)
            words = unauthorized(response).lower()
            assert 'mcp' in words and 'token' in words, (kind, words)
            assert any(where in words for where in ('api tokens', 'tokens page', '/api/tokens', 'settings')), (
                kind,
                words,
            )
        # A signed-in browser session, no token: 401.
        username = await w.database.fetchval(
            'UPDATE users SET password_hash = $2 WHERE id = $1 RETURNING username',
            POWER,
            await hash_password('a long pass phrase'),
        )
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url='https://test') as browser:
            signed_in = await browser.post(
                '/api/auth/login',
                json={'username': username, 'password': 'a long pass phrase'},
            )
            assert signed_in.is_success, signed_in.text
            assert (await browser.get('/api/auth/me')).status_code == 200
            response = await door.send(
                {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {'_meta': envelope(NEWEST)}},
                None,
                client=browser,
            )
            unauthorized(response)

    world(scenario)


def test_a_user_who_may_not_use_the_door_is_refused_with_its_sentence_at_every_request(world):
    async def scenario(w):
        await farm(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        asks = [
            ('server/discover', {}),
            ('tools/list', {}),
            ('tools/call', {'name': 'list_datasets', 'arguments': {}}),
        ]
        for method, params in asks:
            response, _ = await door.ask(method, params, token)
            assert refusal(response, 403) == MCP_OFF, method
        await open_door(w)
        assert (await door.ask('tools/list', {}, token))[0].status_code == 200
        # Let in at one request, taken out before the next: refused at the next, on the same client.
        await settings(w, {'roles': {'power_user': False}})
        for method, params in asks:
            response, _ = await door.ask(method, params, token)
            assert refusal(response, 403) == NOT_LET_IN, method
        await settings(w, {'roles': {'power_user': True}})
        assert (await door.ask('tools/list', {}, token))[0].status_code == 200
        # The account's own state: the API's words.
        await w.database.execute('UPDATE users SET must_change_password = TRUE WHERE id = $1', POWER)
        words = await assistant.door_refusal(user_id=POWER, door='mcp')
        assert words and words not in (MCP_OFF, NOT_LET_IN)
        response, _ = await door.ask('tools/list', {}, token)
        assert refusal(response, 403) == words
        await w.database.execute('UPDATE users SET must_change_password = FALSE WHERE id = $1', POWER)
        await settings(w, {'mcp_enabled': False})
        response, _ = await door.ask('tools/list', {}, token)
        assert refusal(response, 403) == MCP_OFF

    world(scenario)


def test_deactivating_the_user_ends_their_mcp_tokens(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        assert (await door.ask('tools/list', {}, token))[0].status_code == 200
        assert (await w.call(ADMIN, 'PATCH', f'/api/users/{POWER}', json={'is_active': False})).is_success
        unauthorized((await door.ask('tools/list', {}, token))[0])
        assert (await w.call(ADMIN, 'PATCH', f'/api/users/{POWER}', json={'is_active': True})).is_success
        unauthorized((await door.ask('tools/list', {}, token))[0])  # ended, not paused

    world(scenario)


def test_the_token_is_kept_nowhere_but_the_tokens_table_and_logged_nowhere(world, caplog, capfd):
    async def scenario(w):
        caplog.set_level(logging.DEBUG)
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        capfd.readouterr()
        await door.result('server/discover', {}, token)
        await door.result('tools/list', {}, token)
        for arguments in ({}, {'dataset': FARM}, {'dataset': 999999}, {'dataset': 'x'}):
            name = 'list_datasets' if not arguments else 'describe_dataset'
            await door.ask('tools/call', {'name': name, 'arguments': arguments}, token)
        await door.ask('tools/call', {'name': 'no_such_tool', 'arguments': {}}, token)
        await door.ask('tools/list', {}, token, method='GET')
        assert await tables_holding(w, token, but=('api_tokens',)) == []
        printed = ''.join(capfd.readouterr())
        logged = '\n'.join(record.getMessage() + ' ' + repr(record.args) for record in caplog.records)
        assert token not in printed and token not in logged

    world(scenario)


# --- 2. The server at /mcp ---


def http2_client(token: str) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=main.app),
        base_url='http://test',
        headers={'Authorization': f'Bearer {token}'},
    )


def plain(model: Any) -> Any:
    return model.model_dump(by_alias=True, exclude_none=True, mode='json')


def expected_tools() -> list[tuple[str, Optional[str], dict[str, Any]]]:
    return [(tool.name, tool.description, json.loads(json.dumps(tool.input_schema))) for tool in assistant.tools()]


def test_a_client_of_each_revision_discovers_lists_and_calls(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        token = (await make_token(w, POWER))['token']
        reference = await assistant.call_tool(
            user_id=POWER,
            door='mcp',
            name='describe_dataset',
            arguments={'dataset': FARM},
        )
        assert reference['ok'], reference
        # The SDK's client: 2026-07-28 (it probes server/discover) and 2025-11-25 (the initialize handshake).
        for mode, version in (('auto', NEWEST), ('legacy', PREVIOUS)):
            async with http2_client(token) as http:
                transport = streamable_http_client('http://test/mcp', http_client=http, terminate_on_close=False)
                async with Client(transport, mode=mode, cache=None) as client:
                    assert client.protocol_version == version, mode
                    info = client.server_info
                    assert info is not None and (info.name, info.version) == ('freedomridge', VERSION), (mode, info)
                    assert client.instructions == assistant.instructions(), mode
                    listed = await client.list_tools()
                    assert [(t.name, t.description, t.input_schema) for t in listed.tools] == expected_tools(), mode
                    by_sdk = await client.call_tool('describe_dataset', {'dataset': FARM})
                    assert by_sdk.is_error is False and by_sdk.structured_content == reference['result'], mode
        # 2025-06-18, in raw HTTP.
        door = Door(w)
        opened = await door.initialize(token, OLDEST)
        assert opened['serverInfo'] == {'name': 'freedomridge', 'version': VERSION}, opened
        assert opened['instructions'] == assistant.instructions()
        listed = await door.result('tools/list', {}, token, OLDEST)
        assert [(t['name'], t.get('description'), t['inputSchema']) for t in listed['tools']] == expected_tools()
        called = await door.call(token, 'describe_dataset', {'dataset': FARM}, OLDEST)
        assert called['structuredContent'] == reference['result'] and not called.get('isError'), called
        # And 2026-07-28 in raw HTTP.
        found = await door.result('server/discover', {}, token)
        assert NEWEST in found['supportedVersions'], found
        assert found['_meta']['io.modelcontextprotocol/serverInfo'] == {'name': 'freedomridge', 'version': VERSION}
        assert found['instructions'] == assistant.instructions()
        assert 'tools' in found['capabilities'], found

    world(scenario)


def test_no_session_is_issued_and_an_invented_one_is_ignored(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        await door.initialize(token, PREVIOUS)
        await door.initialize(token, OLDEST)
        plain_list = await door.result('tools/list', {}, token)
        response, message = await door.ask('tools/list', {}, token, headers={'Mcp-Session-Id': 'invented-session-77'})
        assert response.status_code == 200 and message is not None and message['result'] == plain_list
        for version in (PREVIOUS, OLDEST):
            response, message = await door.ask('tools/list', {}, token, version, headers={'Mcp-Session-Id': 'made-up'})
            assert response.status_code == 200 and message is not None, (version, response.text)
        async with http2_client(token) as http:
            transport = streamable_http_client('http://test/mcp', http_client=http, terminate_on_close=False)
            async with Client(transport, mode='legacy', cache=None) as client:
                await client.list_tools()
        assert door.answers and all('mcp-session-id' not in answer.headers for answer in door.answers)

    world(scenario)


def test_the_tool_list_is_the_assistants_read_only_with_a_private_five_minute_hint_in_the_newest_revision(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        newest = await door.result('tools/list', {}, token)
        assert (newest.get('ttlMs'), newest.get('cacheScope')) == (300000, 'private'), newest
        for version in (PREVIOUS, OLDEST):
            older = await door.result('tools/list', {}, token, version)
            assert 'ttlMs' not in older and 'cacheScope' not in older, (version, older)
            assert older['tools'] == newest['tools'], version
        assert [(t['name'], t.get('description'), t['inputSchema']) for t in newest['tools']] == expected_tools()
        assert all(t.get('annotations', {}).get('readOnlyHint') is True for t in newest['tools']), newest['tools']
        assert newest.get('nextCursor') is None

    world(scenario)


def test_a_call_answers_the_tools_result_or_its_error_as_the_contract_says(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        for version in (NEWEST, PREVIOUS, OLDEST):
            for name, arguments in (
                ('list_datasets', {}),
                ('describe_dataset', {'dataset': FARM}),
                ('describe_dataset', {'dataset': 999999}),
                ('describe_dataset', {'dataset': FARM, 'limit': 0}),
            ):
                answer = await assistant.call_tool(user_id=POWER, door='mcp', name=name, arguments=arguments)
                called = await door.call(token, name, arguments, version)
                text = [block['text'] for block in called['content'] if block.get('type') == 'text']
                assert len(text) == 1, called
                if answer['ok']:
                    assert called.get('isError') in (None, False), called
                    assert called['structuredContent'] == answer['result'] and json.loads(text[0]) == answer['result']
                else:
                    error = answer['error']
                    assert called['isError'] is True, called
                    assert text[0] == error['message'] + (RETRY if error['retryable'] else ''), (text, error)
                    assert called['structuredContent'] == {'error': error}, called
        # Arguments left out or null are an empty object.
        empty = await door.call(token, 'list_datasets', {})
        assert await door.call(token, 'list_datasets') == empty
        assert await door.result('tools/call', {'name': 'list_datasets', 'arguments': None}, token) == empty

    world(scenario)


def test_an_unknown_tool_or_arguments_that_are_not_an_object_are_the_protocols_error(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        for version in (NEWEST, OLDEST):
            for params in (
                {'name': 'no_such_tool', 'arguments': {}},
                {'name': 'list_datasets', 'arguments': []},
                {'name': 'list_datasets', 'arguments': 'all'},
                {'name': 'describe_dataset', 'arguments': 10},
            ):
                _, message = await door.ask('tools/call', params, token, version)
                assert message is not None and message.get('error', {}).get('code') == -32602, (
                    version,
                    params,
                    message,
                )

    world(scenario)


def test_a_repeated_call_answers_the_same_and_changes_nothing(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        before = await snapshot(w)
        for name, arguments in (('list_datasets', {}), ('describe_dataset', {'dataset': FARM})):
            first = await door.call(token, name, arguments)
            assert await door.call(token, name, arguments) == first, name
        assert await snapshot(w) == before

    world(scenario)


def test_origin_is_checked_first_then_the_token_then_the_method_and_no_answer_permits_another_origin(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {'_meta': envelope(NEWEST)}}
        # A foreign origin: 403, whatever the token and method.
        for given, method in ((token, 'POST'), (None, 'POST'), (None, 'GET'), (token, 'DELETE')):
            response = await door.send(body, given, method=method, headers={'Origin': FOREIGN})
            assert response.status_code == 403, (given is not None, method, response.text)
        # Then the token: 401 whatever the method.
        for method in ('GET', 'DELETE', 'PUT', 'POST'):
            unauthorized(await door.send(body, None, method=method))
        # Then the method: only POST.
        for method in ('GET', 'DELETE', 'PUT', 'PATCH'):
            refusal(await door.send(body, token, method=method), 405)
        allowed = sorted(main._ALLOWED_ORIGINS)
        if allowed:
            response = await door.send(body, token, headers={'Origin': allowed[0]})
            assert response.status_code == 200, response.text
        # A preflight is given no permission, from any origin.
        for origin in (FOREIGN, *allowed[:1]):
            await door.send(
                None,
                None,
                version=None,
                method='OPTIONS',
                headers={
                    'Origin': origin,
                    'Access-Control-Request-Method': 'POST',
                    'Access-Control-Request-Headers': 'authorization',
                },
            )
        for answer in door.answers:
            assert not [h for h in answer.headers if h.lower().startswith('access-control-')], dict(answer.headers)

    world(scenario)


HOSTILE: list[Any] = [-1, 0, 2**63, 1.5, '1; DROP TABLE samples', None, [], {'$ne': 1}, '%', 'x' * 20000, '😀']


def test_nothing_at_mcp_writes(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        admin_token = (await make_token(w, ADMIN))['token']
        before = await snapshot(w)
        arguments: list[Any] = [{}, {'dataset': FARM}, None, [], 'x']
        for key in ('dataset', 'search', 'limit', 'offset'):
            arguments += [{key: value} for value in HOSTILE]
            arguments += [{'dataset': FARM, key: value} for value in HOSTILE]
        for user_token in (token, admin_token):
            for name in [tool.name for tool in assistant.tools()] + ['drop_dataset', 'delete_samples']:
                for given in arguments:
                    await door.ask('tools/call', {'name': name, 'arguments': given}, user_token)
            for method in ('resources/list', 'prompts/list', 'tools/delete', 'samples/update'):
                await door.ask(method, {}, user_token)
        assert await snapshot(w) == before

    world(scenario)


# --- 3. The log ---

CALLS = '/api/admin/assistant/calls'


async def log(w: World, **params: Any) -> dict[str, Any]:
    response = await w.call(ADMIN, 'GET', CALLS, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_each_call_through_the_door_leaves_one_row_of_exactly_the_listed_facts(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        before = (await log(w))['total']
        made = [
            ('list_datasets', {}, None, True, None),
            ('describe_dataset', {'dataset': FARM}, FARM, True, None),
            ('describe_dataset', {'dataset': FARM, 'limit': 0}, FARM, False, 'invalid'),
        ]
        for index, (name, arguments, _, _, _) in enumerate(made, start=1):
            await door.call(token, name, arguments)
            assert (await log(w))['total'] == before + index, name  # one row per call
        rows = (await log(w, limit=len(made)))['calls']
        username = await w.database.fetchval('SELECT username FROM users WHERE id = $1', POWER)
        for row, (name, _arguments, dataset_id, ok, code) in zip(rows, reversed(made)):
            assert set(row) == {
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
            }, row
            assert (row['user_id'], row['username'], row['door'], row['tool']) == (POWER, username, 'mcp', name)
            assert (row['dataset_id'], row['ok'], row['error_code']) == (dataset_id, ok, code), row
            assert (
                isinstance(row['started_at'], str) and isinstance(row['duration_ms'], int) and row['duration_ms'] >= 0
            )

    world(scenario)


def test_no_argument_and_no_result_is_kept_in_the_log(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        planted = 'PLANTED-SERIAL-77341'
        await door.call(token, 'describe_dataset', {'dataset': FARM, 'search': planted})
        await door.call(token, 'list_datasets', {'search': planted})
        described = await door.call(token, 'describe_dataset', {'dataset': FARM})
        assert 'RESULT-MARK-5521' in json.dumps(described)
        assert await tables_holding(w, planted) == []
        assert await tables_holding(w, 'RESULT-MARK-5521') == ['datasets']
        listing = json.dumps(await log(w, limit=200))
        assert planted not in listing and 'RESULT-MARK-5521' not in listing

    world(scenario)


def test_the_log_is_listed_newest_first_with_its_total_a_page_at_a_time_for_administrators_only(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        for _ in range(7):
            await door.call(token, 'list_datasets', {})
        everything = await log(w, limit=200)
        total = everything['total']
        assert total >= 7 and len(everything['calls']) == total and everything['next_offset'] is None
        ids = [row['id'] for row in everything['calls']]
        started = [row['started_at'] for row in everything['calls']]
        assert started == sorted(started, reverse=True) and ids == sorted(ids, reverse=True)
        first = await log(w, limit=3)
        assert (first['offset'], first['next_offset'], first['total']) == (0, 3, total)
        assert [row['id'] for row in first['calls']] == ids[:3]
        rest = await log(w, limit=3, offset=3)
        assert [row['id'] for row in rest['calls']] == ids[3:6]
        default = await log(w)
        assert len(default['calls']) == min(50, total)
        for limit in (0, 201, -1):
            assert (await w.call(ADMIN, 'GET', CALLS, params={'limit': limit})).status_code == 422, limit
        for user_id, status in ((POWER, 403), (GUEST, 403), (None, 401)):
            response = await w.call(user_id, 'GET', CALLS)
            assert response.status_code == status and 'list_datasets' not in response.text, (user_id, response.text)

    world(scenario)


def test_deleting_a_user_or_a_dataset_keeps_their_rows(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        await w.share(FARM, OTHER_POWER, 'view')
        door = Door(w)
        token = (await make_token(w, OTHER_POWER))['token']
        await door.call(token, 'describe_dataset', {'dataset': FARM})
        await door.call(token, 'list_datasets', {})
        before = {row['id'] for row in (await log(w, limit=200))['calls']}
        assert (await w.call(ADMIN, 'DELETE', f'/api/users/{OTHER_POWER}')).is_success
        deleting = await w.call(ADMIN, 'DELETE', f'/api/datasets/{FARM}')
        assert deleting.is_success, deleting.text
        await finish_deletion(w.database)
        assert await w.database.fetchval('SELECT count(*) FROM datasets WHERE id = $1', FARM) == 0
        assert {row['id'] for row in (await log(w, limit=200))['calls']} == before

    world(scenario)


def test_rows_older_than_thirty_days_are_removed_by_the_housekeeping(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        for _ in range(3):
            await door.call(token, 'list_datasets', {})
        table = await w.database.fetchval(
            "SELECT table_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND column_name IN ('door', 'tool', 'started_at') GROUP BY table_name HAVING count(*) = 3",
        )
        assert table, 'no table holds the call log'
        ids = [row['id'] for row in (await log(w, limit=200))['calls']]
        await w.database.execute(
            f"UPDATE \"{table}\" SET started_at = now() - interval '31 days' WHERE id = $1",
            ids[0],
        )
        await w.database.execute(
            f"UPDATE \"{table}\" SET started_at = now() - interval '29 days' WHERE id = $1",
            ids[1],
        )
        await assistant_calls.prune(w.database.pool)
        assert {row['id'] for row in (await log(w, limit=200))['calls']} == set(ids[1:])

    world(scenario)
