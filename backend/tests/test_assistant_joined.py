"""The MCP door (backend/mcp_door.py) and the tools added beside it (working views, finding tools) in one tree: built
apart, on the assistant core's registry and its rule for a request made for a tool. These test them together: the door
lists and calls every tool the registry holds, however many, a request made for a tool is refused by the door's
administrators' route and by the views' people's routes, an MCP token opens neither of those, and the call log names a
tool's dataset as it names any tool's: from a `dataset` argument, and none for a call that names only a view."""

import os
import secrets
from typing import Any

import pytest

from backend import assistant
from backend.assistant import api as assistant_api
from backend.assistant import inner
from backend.assistant.identity import Identity
from backend.main import app
from backend.tests import test_assistant_core
from backend.tests.test_access_enforcement_spec import app_routes
from backend.tests.test_assistant_core import ADMIN, POWER, TRIALS, World, _raw
from backend.tests.test_assistant_private_values import CONCEPT, Examples, _argument_sets, _text_model
from backend.tests.test_assistant_private_values import _plant as plant_every_kind
from backend.tests.test_assistant_views import _plant
from backend.tests.test_mcp_door_spec import NEWEST, envelope, messages

registry_database = test_assistant_core.registry_database
world = test_assistant_core.world
__all__ = ['_text_model']  # text search's model stood in for, as the private-values test does (autouse)
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

VIEW_ROUTE, LIST_ROUTE = '/api/assistant/views/{handle}', '/api/assistant/lists/{handle}/csv'
CALLS_ROUTE = '/api/admin/assistant/calls'
NO_SUCH_TOKEN = (401, {'detail': 'Invalid or expired token'})


@pytest.fixture(autouse=True)
def _address(monkeypatch):
    monkeypatch.setenv('FRIDGE_PUBLIC_URL', 'https://data.example.org')


async def mcp_token(w: World, user_id: int) -> str:
    made = await w.api(user_id, 'POST', '/api/tokens', json={'name': f'mcp-{secrets.token_hex(3)}', 'scope': 'mcp'})
    assert made.status_code == 200, made.text
    return made.json()['token']


async def handles(w: World) -> tuple[str, str]:
    """A working view of POWER's and a value list of POWER's, made by the tools in process."""
    view = await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop = "wheat"')
    values = await w.tool(POWER, 'field_values', view=view['result']['view'], field='crop')
    return view['result']['view'], values['result']['list']


class Door:
    """JSON-RPC requests to /mcp as a 2026-07-28 client makes them (no handshake), through the world's client."""

    def __init__(self, w: World, token: str):
        self.w, self.token, self.next_id = w, token, 0

    async def result(self, rpc: str, params: dict[str, Any]) -> Any:
        self.next_id += 1
        headers = {
            'Accept': 'application/json, text/event-stream',
            'Authorization': f'Bearer {self.token}',
            'MCP-Protocol-Version': NEWEST,
            'Mcp-Method': rpc,
        }
        if rpc == 'tools/call':
            headers['Mcp-Name'] = params['name']
        body = {'jsonrpc': '2.0', 'id': self.next_id, 'method': rpc, 'params': {**params, '_meta': envelope(NEWEST)}}
        response = await self.w.client.post('/mcp', json=body, headers=headers)
        [answer] = [message for message in messages(response) if message.get('id') == self.next_id]
        assert response.status_code == 200 and 'result' in answer, (rpc, response.text)
        return answer['result']

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.result('tools/call', {'name': name, 'arguments': arguments})


def handler(path: str):
    return next(route.endpoint for found, route in inner._walk(app.routes) if found == path and 'GET' in route.methods)


def test_the_door_lists_and_calls_every_tool_the_registry_holds(world):
    """However many tools the registry holds: the door lists them as the registry has them, in its order, and each
    answers a call through /mcp. The arguments are the private-values test's (Examples, _argument_sets: an argument
    they have no example for fails by name), on its dataset, which has what every tool needs to answer (vectors, a
    concept with scores, saved views), so a tool added later is called here without editing this test."""

    async def scenario(w: World):
        await w.switch_on(power_user=True)
        examples = Examples(await plant_every_kind(w))
        door = Door(w, await mcp_token(w, ADMIN))
        tools = assistant.tools()
        listed = await door.result('tools/list', {})
        assert [(t['name'], t['description'], t['inputSchema']) for t in listed['tools']] == [
            (tool.name, tool.description, tool.input_schema) for tool in tools
        ]
        assert all(t['annotations']['readOnlyHint'] for t in listed['tools'])
        answered: dict[str, dict[str, Any]] = {}
        for _ in range(2):  # the second round has the handles, slugs and samples the first gave
            for tool in tools:
                for arguments in [] if tool.name in answered else _argument_sets(tool, examples):
                    result = await door.call(tool.name, arguments)
                    examples.learn(result.get('structuredContent'))
                    if not result.get('isError'):
                        answered[tool.name] = arguments
                        break
        missing = [tool.name for tool in tools if tool.name not in answered]
        assert not missing, f'no call through /mcp answered for {missing}'

    world(scenario)


def test_a_request_made_for_a_tool_is_refused_by_the_call_log_route_and_the_peoples_routes(world):
    async def scenario(w: World):
        await _plant(w)
        view, values = await handles(w)
        administrators, peoples = "administrators' routes", "people's assistant routes"
        for user_id, template, path, words in (
            (ADMIN, CALLS_ROUTE, CALLS_ROUTE, administrators),
            (POWER, VIEW_ROUTE, f'/api/assistant/views/{view}', peoples),
            (POWER, LIST_ROUTE, f'/api/assistant/lists/{values}/csv', peoples),
        ):
            # The person reads it; a tool, even one listing that very route, is refused by the route's place.
            assert (await w.api(user_id, 'GET', path)).status_code == 200, path
            listed = frozenset({('GET', handler(template))})
            status, body = await _raw(Identity(user_id, 'mcp', listed), 'GET', path)
            assert (status, body['code']) == (403, assistant_api.ROUTE_REFUSED_CODE), (path, body)
            assert words in body['detail'], (path, body)

    world(scenario)


def test_an_mcp_token_on_the_peoples_assistant_routes_is_a_token_that_does_not_exist(world):
    async def scenario(w: World):
        await _plant(w)
        view, values = await handles(w)
        token, nobody = await mcp_token(w, POWER), secrets.token_urlsafe(32)
        # The door's every-route walks (test_mcp_door, test_mcp_door_spec) read app_routes(): these are among them.
        assert {('GET', VIEW_ROUTE), ('GET', LIST_ROUTE), ('GET', CALLS_ROUTE)} <= set(app_routes())
        for path in (f'/api/assistant/views/{view}', f'/api/assistant/lists/{values}/csv'):
            assert (await w.api(POWER, 'GET', path)).status_code == 200, path
            for given in (token, nobody):
                answer = await w.client.get(path, headers={'Authorization': f'Bearer {given}'})
                assert (answer.status_code, answer.json()) == NO_SUCH_TOKEN, (path, answer.text)
        assert await w.database.fetchval("SELECT last_used_at FROM api_tokens WHERE scope = 'mcp'") is None

    world(scenario)


def test_the_log_names_a_tools_dataset_from_its_dataset_argument_and_none_for_a_view_handle(world):
    async def scenario(w: World):
        await w.switch_on(power_user=True)
        await plant_every_kind(w)
        door = Door(w, await mcp_token(w, POWER))
        made = (await door.call('make_view', {'dataset': TRIALS}))['structuredContent']
        before = await w.database.fetchval('SELECT coalesce(max(id), 0) FROM assistant_calls')
        calls: list[tuple[str, dict[str, Any], Any]] = [
            ('refine_view', {'view': made['view'], 'add': 'crop = "wheat"'}, None),
            ('field_values', {'dataset': TRIALS, 'field': 'crop'}, TRIALS),
            ('field_values', {'view': made['view'], 'field': 'crop'}, None),
            ('field_summary', {'view': made['view'], 'field': 'score'}, None),
            ('get_samples', {'dataset': TRIALS, 'limit': 2}, TRIALS),
            ('get_samples', {'view': made['view'], 'limit': 2}, None),
            # Finding tools: one naming the dataset, one a view.
            ('list_concepts', {'dataset': TRIALS}, TRIALS),
            ('concept_preview', {'concept': CONCEPT, 'view': made['view']}, None),
            ('make_view', {'dataset': TRIALS, 'filter': 'crop = '}, TRIALS),  # refused, still the dataset it named
        ]
        for name, arguments, _ in calls:
            await door.call(name, arguments)
        rows = await w.database.fetch(
            'SELECT door, tool, dataset_id, user_id, ok, error_code FROM assistant_calls WHERE id > $1 ORDER BY id',
            before,
        )
        assert [(r['door'], r['tool'], r['dataset_id'], r['user_id']) for r in rows] == [
            ('mcp', name, dataset, POWER) for name, _, dataset in calls
        ]
        assert [(r['ok'], r['error_code']) for r in rows] == [(True, None)] * (len(calls) - 1) + [(False, 'invalid')]

    world(scenario)
