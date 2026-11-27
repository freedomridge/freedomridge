"""The assistant core (milestone 1): the tool list, tools that read as the signed-in user and only read, who may use
the assistant, and the first two tools.

Written from the contract (m1-contract-assistant-core), not from the code. The tools are called where the doors will
call them, `backend.assistant` in the test process, against the test database with real users, groups and shares made
the way the access-rule tests make them; the settings through their HTTP endpoints, signed in with real API tokens.
What a tool answers is compared with what the API answers the same user at the same moment.

Where the contract is silent these tests read it this way:
- An error's `retryable` is false for every code but `busy`; `not_found` names the dataset by its id, and says
  "deleted" for one being deleted.
- `list_datasets` orders by name; datasets whose names differ only in their numbers are given zero-padded numbers.
- `dataset.samples` is the count the API's samples query gives; `saved_views` the length of the API's view list for
  that user; `embedding_fields` the vector fields the dataset holds; `concepts` its concepts; `undescribed_fields` the
  schema's fields whose description_source is null.
- `type` is the filter language's kind (docs/FILTER_QUERY.md): a metadata `string` is `text`, a native array `list`.
- `values` for a boolean field gives each value as true/false or as its text; a list field's elements are its values.
- `samples_with_value` for `tags` and a registered array field is null or the number of samples holding an
  element; the statistics' row count of the array column is not that number.
- A field whose statistics have not been counted yet (samples added after the last count) is not whole: `complete`
  false. Statistics are counted by running the statistics job the way the other statistics tests do. `complete` is
  false when samples were added or changed after the count. A field with more values than the statistics keep a
  list of (statistics_measure's limit, plus 50) is `many`, and complete. `examples` are checked as real values of the
  field (their order isn't known from outside).
- `dataset.samples` counts the rows that aren't objects (media_type 'object'); `dataset.objects` counts those.
- The cost rule is observed from outside as: the statements every connection runs (counted on asyncpg's connection
  class) for a page of 2 fields and of 50 differ by at most one; describing leaves the kept statistics and the job
  queue as they were.
- The settings refusals are the API's usual ones for an administrators' route: 403 for a signed-in non-administrator,
  401 signed out; "learns nothing" is checked as no setting appearing in the refusal.
- A hostile argument is any value of the wrong type, out of range, or text meant to break a query; each answers
  `ok` or an error, never raises.
"""

import importlib
import json
import os
import subprocess
import sys
from typing import Any, Optional

import asyncpg
import pytest

from backend import statistics_measure
from backend.routers import datasets
from backend.tests import test_access_rule_spec, test_field_registry_postgres, test_upgrade_paths
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_access_enforcement_spec import (
    CALLERS,
    DENIED,
    SEALED,
    app_routes,
    detail,
    needs,
    open_dataset,
    prepare,
    set_share,
)
from backend.tests.test_access_rule_spec import ADMIN, GUEST, OTHER_POWER, POWER, World
from backend.tests.test_upgrade_paths import ROOT

assistant: Any = importlib.import_module('backend.assistant')

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
database = test_upgrade_paths.database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

ADMIN_USER = {'id': ADMIN, 'role': 'admin', 'username': f'user-{ADMIN}'}
# A power user made by a test that needs the least-privileged user the assistant lets in (view where shared, nothing
# else); a guest, who once stood in for that user, may never use the MCP server.
VIEWER = 12
CODES = {'not_allowed', 'refused', 'not_found', 'invalid', 'busy', 'unknown_tool'}
MCP_OFF = 'The MCP server is switched off on this server'
CHAT_OFF = 'The assistant is switched off on this server'
NOT_LET_IN = 'Your account may not use the assistant on this server'
CLOSED = 'The assistant may not read this dataset'
SETTINGS = '/api/admin/assistant'
SHIPPED = {
    'mcp_enabled': False,
    'roles': {'admin': True, 'power_user': False, 'guest': False},
    'groups': [],
    'users': [],
    'new_datasets_closed': False,
    'sample_fields': False,  # the working-views contract's one more key
    'admin_tools': False,  # m2-contract-admin-read-tools 1.1
}
# The POST routes that only read (docs, the access spec's view-level reads): any other POST writes.
READ_POSTS = {
    '/api/datasets/{id}/samples/query',
    '/api/datasets/{id}/samples/lookup',
    '/api/datasets/{id}/field-stats',
    '/api/datasets/{id}/histogram',
    '/api/datasets/{id}/histogram/preview',
    '/api/datasets/{id}/aggregate',
    '/api/datasets/{id}/map_points',
    '/api/datasets/{id}/map_extent',
    '/api/datasets/{id}/filter_query/parse',
    '/api/datasets/{id}/filter_query/format',
    '/api/datasets/{id}/fields/{field_name}/autocomplete',
    '/api/datasets/{id}/samples/tag-counts',
    '/api/datasets/{id}/embeddings/query',
    '/api/datasets/{id}/embeddings/lookup',
    '/api/datasets/{id}/versions/{version_id}/samples/query',
    '/api/datasets/{id}/versions/{version_id}/aggregate',
    '/api/datasets/{id}/embeddings/lookup-patches',
    '/api/datasets/{id}/embeddings/vectors/by-ids',
    '/api/datasets/{id}/hashes/by-ids',
    '/api/datasets/{id}/hashes/stats',
    '/api/datasets/{id}/hashes/duplicates',
    '/api/datasets/{id}/hashes/sample-duplicates',
    '/api/datasets/{id}/quality/representativeness/centroid',
    '/api/datasets/{id}/quality/duplicates/groups',
    '/api/datasets/{dataset_id}/sparse-sample',
    '/api/search',
    '/api/search/by-image',
    '/api/search/patches',
    '/api/search/object-to-patches',
}


# --- Calling the core ---


def plain(answer: Any) -> Any:
    """The answer is plain JSON: json.dumps takes it as it is."""
    json.dumps(answer)
    return answer


async def call(user_id: int, name: str, arguments: Optional[dict] = None, door: str = 'mcp') -> dict[str, Any]:
    answer = plain(await assistant.call_tool(user_id=user_id, door=door, name=name, arguments=arguments or {}))
    assert isinstance(answer, dict) and answer.get('ok') in (True, False), answer
    if answer['ok']:
        assert set(answer) == {'ok', 'result'}, answer
    else:
        assert set(answer) == {'ok', 'error'}, answer
        error = answer['error']
        assert set(error) == {'code', 'message', 'retryable'}, error
        assert error['code'] in CODES, error
        assert isinstance(error['message'], str) and error['message'].strip(), error
        assert error['retryable'] is (error['code'] == 'busy'), error
    return answer


async def result(user_id: int, name: str, arguments: Optional[dict] = None) -> dict[str, Any]:
    answer = await call(user_id, name, arguments)
    assert answer['ok'], (user_id, name, arguments, answer)
    return answer['result']


async def error(user_id: int, name: str, arguments: Optional[dict] = None, door: str = 'mcp') -> dict[str, Any]:
    answer = await call(user_id, name, arguments, door)
    assert not answer['ok'], (user_id, name, arguments, answer)
    return answer['error']


async def settings(w: World, change: Optional[dict] = None) -> dict[str, Any]:
    if change is not None:
        response = await w.call(ADMIN, 'PATCH', SETTINGS, json=change)
        assert response.status_code == 200, response.text
    response = await w.call(ADMIN, 'GET', SETTINGS)
    assert response.status_code == 200, response.text
    return response.json()


async def open_door(w: World, **roles: bool) -> None:
    """The MCP door on, with these roles let in (all three unless named)."""
    await settings(w, {'mcp_enabled': True, 'roles': roles or {'admin': True, 'power_user': True, 'guest': True}})


async def every_dataset(user_id: int, **arguments: Any) -> list[dict[str, Any]]:
    """Every page of list_datasets, following next_offset, the total the same on each page."""
    found: list[dict[str, Any]] = []
    offset, total = 0, None
    while True:
        page = await result(user_id, 'list_datasets', {**arguments, 'offset': offset, 'limit': 50})
        assert set(page) == {'datasets', 'total', 'offset', 'next_offset'}, page
        assert page['offset'] == offset and len(page['datasets']) <= 50
        assert total is None or page['total'] == total
        total = page['total']
        found.extend(page['datasets'])
        if page['next_offset'] is None:
            break
        assert page['next_offset'] == offset + len(page['datasets'])
        offset = page['next_offset']
    assert len(found) == total
    return found


async def api_count(w: World, user_id: int, dataset_id: int) -> int:
    response = await w.call(user_id, 'POST', f'/api/datasets/{dataset_id}/samples/query', json={'limit': 0})
    assert response.status_code == 200, response.text
    return response.json()['total']


async def api_refusal_of(w: World, user_id: int) -> str:
    """The words the API refuses this account with."""
    response = await w.call(user_id, 'GET', '/api/datasets')
    assert response.status_code == 403, response.text
    return detail(response)


async def add(dataset_id: int, samples: list[dict[str, Any]]) -> list[int]:
    return (await datasets.add_samples(dataset_id, {'samples': samples}, user=ADMIN_USER))['ids']


# --- 1. The surface: the tool list ---


def test_the_tool_list_is_fixed_read_only_and_each_schema_marks_what_is_required():
    tools = list(assistant.tools())
    names = [tool.name for tool in tools]
    assert {'list_datasets', 'describe_dataset'} <= set(names), names
    assert len(names) == len(set(names))
    again = list(assistant.tools())
    assert [(t.name, t.description, t.input_schema, t.kind, t.level, list(t.routes)) for t in again] == [
        (t.name, t.description, t.input_schema, t.kind, t.level, list(t.routes)) for t in tools
    ]
    served = app_routes()
    for tool in tools:
        assert isinstance(tool.description, str) and len(tool.description.split()) >= 5, tool.name
        assert tool.kind == 'read', tool.name
        assert tool.level in (None, 'view', 'tag', 'edit', 'manage'), tool.name
        schema = plain(tool.input_schema)
        assert schema.get('type') == 'object' and isinstance(schema.get('properties', {}), dict), (tool.name, schema)
        for argument, rule in schema.get('properties', {}).items():
            assert isinstance(rule, dict), (tool.name, argument)
            kinds = rule.get('type')
            for kind in kinds if isinstance(kinds, list) else [kinds] if kinds else []:
                assert kind in ('string', 'integer', 'number', 'boolean', 'array', 'object', 'null'), (tool.name, kind)
        required = schema.get('required', [])
        assert isinstance(required, list) and set(required) <= set(schema.get('properties', {})), (tool.name, schema)
        assert tool.routes, tool.name
        for method, template in tool.routes:
            path = template if template.startswith('/api') else '/api' + template
            assert (method, path) in served, (tool.name, method, template)
            assert method == 'GET' or (method == 'POST' and path in READ_POSTS), (tool.name, method, template)
    by_name = {tool.name: tool for tool in tools}
    assert by_name['describe_dataset'].input_schema.get('required') == ['dataset']
    assert not by_name['list_datasets'].input_schema.get('required')
    assert set(by_name['list_datasets'].input_schema['properties']) >= {'search', 'limit', 'offset'}
    assert set(by_name['describe_dataset'].input_schema['properties']) >= {'dataset', 'search', 'limit', 'offset'}
    instructions = assistant.instructions()
    assert isinstance(instructions, str) and instructions.strip() and instructions == assistant.instructions()


def test_no_tool_lists_an_administrators_route_or_the_assistants_own():
    fenced = {(method, path) for method, path in app_routes() if path.startswith(('/api/admin', '/api/assistant'))}
    assert ('PATCH', SETTINGS) in fenced  # the settings are served, and fenced
    for tool in assistant.tools():
        for method, template in tool.routes:
            path = template if template.startswith('/api') else '/api' + template
            assert not path.startswith(('/api/admin', '/api/assistant')), (tool.name, method, template)
            assert (method, path) not in fenced, (tool.name, method, template)


def test_the_tool_list_is_the_same_in_another_process():
    def listing(tools: Any) -> list:
        return [[t.name, t.description, t.input_schema, t.kind, t.level, [list(r) for r in t.routes]] for t in tools]

    script = (
        'import json\nfrom backend import assistant\n'
        'print(json.dumps([[t.name, t.description, t.input_schema, t.kind, t.level, [list(r) for r in t.routes]] '
        'for t in assistant.tools()]))'
    )
    other = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True, text=True, check=True)
    assert json.loads(other.stdout.strip().splitlines()[-1]) == json.loads(json.dumps(listing(assistant.tools())))


# --- 3. Who may use it ---


def test_a_fresh_server_has_the_door_off_and_the_shipped_settings(world):
    async def scenario(w):
        assert await settings(w) == SHIPPED
        for user_id in (ADMIN, POWER, GUEST):
            assert await assistant.door_refusal(user_id=user_id, door='mcp') == MCP_OFF
            assert await assistant.door_refusal(user_id=user_id, door='chat') == CHAT_OFF
            failure = await error(user_id, 'list_datasets')
            assert (failure['code'], failure['message']) == ('not_allowed', MCP_OFF)
            me = await w.call(user_id, 'GET', '/api/auth/me')
            assert me.status_code == 200 and me.json()['assistant'] == {'mcp': False}, me.text

    world(scenario)


def test_who_may_use_the_assistant_follows_the_rule_in_its_order(world):
    async def scenario(w):
        team = await w.group('agronomy', POWER)
        await settings(w, {'mcp_enabled': True})

        async def may(user_id: int) -> bool:
            refusal = await assistant.door_refusal(user_id=user_id, door='mcp')
            assert refusal in (None, NOT_LET_IN), refusal
            me = (await w.call(user_id, 'GET', '/api/auth/me')).json()
            assert me['assistant'] == {'mcp': refusal is None}, (user_id, me)
            answer = await call(user_id, 'list_datasets')
            assert answer['ok'] is (refusal is None), answer
            if refusal is not None:
                assert answer['error'] == {'code': 'not_allowed', 'message': NOT_LET_IN, 'retryable': False}
            return refusal is None

        # As shipped: administrators yes, power users and guests no.
        assert [await may(u) for u in (ADMIN, POWER, OTHER_POWER, GUEST)] == [True, False, False, False]
        # Roles decide when nothing else does.
        await settings(w, {'roles': {'power_user': True}})
        assert (await settings(w))['roles'] == {'admin': True, 'power_user': True, 'guest': False}
        assert [await may(u) for u in (POWER, OTHER_POWER, GUEST)] == [True, True, False]
        await settings(w, {'roles': {'power_user': False}})
        # A listed group lets its members in, only them.
        put = await w.call(ADMIN, 'PUT', f'{SETTINGS}/groups/{team}')
        assert put.is_success, put.text
        assert (await settings(w))['groups'] == [{'id': team, 'name': 'agronomy'}]
        assert [await may(u) for u in (POWER, OTHER_POWER)] == [True, False]
        # A user's own answer goes over groups and role, either way. A guest's is kept (it lets no guest in: below).
        for user_id, allowed in ((POWER, False), (ADMIN, False), (OTHER_POWER, True)):
            put = await w.call(ADMIN, 'PUT', f'{SETTINGS}/users/{user_id}', json={'allowed': allowed})
            assert put.is_success, put.text
            assert await may(user_id) is allowed, user_id
        put = await w.call(ADMIN, 'PUT', f'{SETTINGS}/users/{GUEST}', json={'allowed': True})
        assert put.is_success, put.text
        users = {entry['id']: entry for entry in (await settings(w))['users']}
        assert {user_id: users[user_id]['allowed'] for user_id in users} == {
            POWER: False,
            ADMIN: False,
            OTHER_POWER: True,
            GUEST: True,
        }
        assert users[GUEST]['username'] == f'user-{GUEST}'
        # Removing a user's answer gives the group and role their say back.
        for user_id, then in ((POWER, True), (ADMIN, True), (OTHER_POWER, False), (GUEST, False)):
            removed = await w.call(ADMIN, 'DELETE', f'{SETTINGS}/users/{user_id}')
            assert removed.is_success, removed.text
            assert await may(user_id) is then, user_id
        removed = await w.call(ADMIN, 'DELETE', f'{SETTINGS}/groups/{team}')
        assert removed.is_success, removed.text
        assert await may(POWER) is False
        # Deleting a group or a user removes its entry.
        await w.call(ADMIN, 'PUT', f'{SETTINGS}/groups/{team}')
        await w.call(ADMIN, 'PUT', f'{SETTINGS}/users/{OTHER_POWER}', json={'allowed': True})
        assert (await w.call(ADMIN, 'DELETE', f'/api/groups/{team}')).is_success
        assert (await w.call(ADMIN, 'DELETE', f'/api/users/{OTHER_POWER}')).is_success
        state = await settings(w)
        assert (state['groups'], state['users']) == ([], [])
        # Unknown ones are 404.
        for method, path, body in (
            ('PUT', f'{SETTINGS}/groups/999999', None),
            ('DELETE', f'{SETTINGS}/groups/999999', None),
            ('PUT', f'{SETTINGS}/users/999999', {'allowed': True}),
            ('DELETE', f'{SETTINGS}/users/999999', None),
        ):
            response = await w.call(ADMIN, method, path, **({} if body is None else {'json': body}))
            assert response.status_code == 404, (method, path, response.text)
        # A guest's own answer `allowed: true` never lets a guest in.
        put = await w.call(ADMIN, 'PUT', f'{SETTINGS}/users/{GUEST}', json={'allowed': True})
        assert put.is_success, put.text
        assert await may(GUEST) is False

    world(scenario)


def test_a_demo_inactive_or_password_owing_account_never_may_and_the_checks_go_in_order(world):
    async def scenario(w):
        await w.user(30, 'guest')
        await w.database.execute('UPDATE users SET is_demo_account = TRUE WHERE id = 30')
        # Door off: that sentence first, for everyone.
        await w.database.execute('UPDATE users SET is_active = FALSE WHERE id = $1', OTHER_POWER)
        for user_id in (30, OTHER_POWER, 999999):
            assert await assistant.door_refusal(user_id=user_id, door='mcp') == MCP_OFF, user_id
        await open_door(w)
        await w.call(ADMIN, 'PUT', f'{SETTINGS}/users/30', json={'allowed': True})
        # A demo account never, whatever the settings say.
        assert await assistant.door_refusal(user_id=30, door='mcp') == NOT_LET_IN
        assert (await error(30, 'list_datasets'))['message'] == NOT_LET_IN
        # Inactive, or owing a password change: the API's own words, before whether they are let in.
        inactive = await api_refusal_of(w, OTHER_POWER)
        assert await assistant.door_refusal(user_id=OTHER_POWER, door='mcp') == inactive
        assert await assistant.door_refusal(user_id=999999, door='mcp') == inactive  # an unknown user
        await w.call(ADMIN, 'PUT', f'{SETTINGS}/users/{POWER}', json={'allowed': True})
        await w.database.execute('UPDATE users SET must_change_password = TRUE WHERE id = $1', POWER)
        owes = await api_refusal_of(w, POWER)
        assert await assistant.door_refusal(user_id=POWER, door='mcp') == owes
        failure = await error(POWER, 'list_datasets')
        assert (failure['code'], failure['message']) == ('not_allowed', owes)
        failure = await error(999999, 'describe_dataset', {'dataset': 1})
        assert (failure['code'], failure['message']) == ('not_allowed', inactive)
        # The chat door is always off in this milestone.
        assert await assistant.door_refusal(user_id=ADMIN, door='chat') == CHAT_OFF
        failure = await error(ADMIN, 'list_datasets', door='chat')
        assert (failure['code'], failure['message']) == ('not_allowed', CHAT_OFF)

    world(scenario)


def test_only_administrators_reach_the_settings(world):
    async def scenario(w):
        team = await w.group('agronomy')
        await w.dataset(40)
        await w.share(40, POWER, 'manage')
        requests = [
            ('GET', SETTINGS, None),
            ('PATCH', SETTINGS, {'mcp_enabled': True}),
            ('PUT', f'{SETTINGS}/groups/{team}', None),
            ('DELETE', f'{SETTINGS}/groups/{team}', None),
            ('PUT', f'{SETTINGS}/users/{POWER}', {'allowed': True}),
            ('DELETE', f'{SETTINGS}/users/{POWER}', None),
        ]
        for user_id, status in ((POWER, 403), (GUEST, 403), (None, 401)):
            for method, path, body in requests:
                response = await w.call(user_id, method, path, **({} if body is None else {'json': body}))
                assert response.status_code == status, (user_id, method, path, response.text)
                for word in ('mcp_enabled', 'new_datasets_closed', 'agronomy', 'power_user'):
                    assert word not in response.text, (user_id, method, path, response.text)
        assert await settings(w) == SHIPPED
        # PATCH changes the keys sent, and only those.
        assert await settings(w, {'new_datasets_closed': True}) == {**SHIPPED, 'new_datasets_closed': True}
        assert (await settings(w, {'roles': {'guest': True}}))['roles'] == {
            'admin': True,
            'power_user': False,
            'guest': True,
        }

    world(scenario)


def test_closing_a_dataset_needs_manage_and_new_ones_follow_the_setting(world):
    async def scenario(w):
        await prepare(w)
        await w.dataset(40)
        await w.dataset(41)
        for level in ('view', 'tag', 'edit'):
            await set_share(w, 40, POWER, level)
            response = await w.call(POWER, 'PATCH', '/api/datasets/40', json={'assistant_closed': True})
            assert (response.status_code, detail(response)) == (403, needs('manage')), (level, response.text)
            assert (await w.call(ADMIN, 'GET', '/api/datasets/40')).json()['assistant_closed'] is False
        await set_share(w, 40, POWER, 'manage')
        response = await w.call(POWER, 'PATCH', '/api/datasets/40', json={'assistant_closed': True})
        assert response.status_code == 200 and response.json()['assistant_closed'] is True, response.text
        # Every answer that describes a dataset carries it.
        assert (await w.call(POWER, 'GET', '/api/datasets/40')).json()['assistant_closed'] is True
        listed = {row['id']: row for row in (await w.call(ADMIN, 'GET', '/api/datasets')).json()}
        assert (listed[40]['assistant_closed'], listed[41]['assistant_closed']) == (True, False)
        made = await w.call(ADMIN, 'POST', '/api/datasets', json={'name': 'open-by-default'})
        assert made.is_success and made.json()['assistant_closed'] is False, made.text
        # New datasets start closed while the setting is on; existing ones don't change.
        await settings(w, {'new_datasets_closed': True})
        made = await w.call(ADMIN, 'POST', '/api/datasets', json={'name': 'closed-from-the-start'})
        assert made.is_success and made.json()['assistant_closed'] is True, made.text
        listed = {row['id']: row for row in (await w.call(ADMIN, 'GET', '/api/datasets')).json()}
        assert (listed[40]['assistant_closed'], listed[41]['assistant_closed']) == (True, False)
        # A dataset being deleted refuses the write, as other writes.
        await w.call(ADMIN, 'DELETE', '/api/datasets/41')
        response = await w.call(ADMIN, 'PATCH', '/api/datasets/41', json={'assistant_closed': True})
        assert (response.status_code, detail(response)) == (409, SEALED), response.text

    world(scenario)


def test_on_a_mirror_the_switch_is_this_servers_own(world):
    async def scenario(w):
        await w.dataset(40)
        await add(40, [{'filepath': 'mirror-0.jpg', 'metadata': {'crop': 'corn'}}])
        await w.database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (40, 9, 0)')
        await w.database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = 40")
        response = await w.call(ADMIN, 'PATCH', '/api/datasets/40', json={'assistant_closed': True})
        assert response.status_code == 200 and response.json()['assistant_closed'] is True, response.text
        await open_door(w)
        failure = await error(ADMIN, 'describe_dataset', {'dataset': 40})
        assert (failure['code'], failure['message']) == ('refused', CLOSED)

    world(scenario)


# --- 2. A tool reads as the user ---


def test_each_tool_shows_a_user_exactly_what_the_api_shows_them(world):
    """Every caller of the access matrix (roles, own shares, group shares, the everyone level, no access) in a dataset
    of their own: each user's list and each dataset's description against the API's answers to that user. The guest,
    whom the MCP door never lets in, is refused every one of them; a let-in power user with the guest's low access
    (view in the datasets shared with the guest, no share elsewhere) is checked in the guest's place."""

    async def scenario(w):
        await prepare(w)
        await w.user(VIEWER, 'power_user')
        made = []
        for caller in CALLERS:
            c = await open_dataset(w, caller)
            made.append(c.d)
            if caller.guest and caller.source in ('own', 'plain'):
                await w.share(c.d, VIEWER, 'view')
        await open_door(w)
        for user_id in (ADMIN, POWER, VIEWER, OTHER_POWER):
            api = {row['id']: row for row in (await w.call(user_id, 'GET', '/api/datasets')).json()}
            listed = {row['id']: row for row in await every_dataset(user_id)}
            assert set(listed) == {i for i, row in api.items() if not row.get('deleting')}, user_id
            for dataset_id, row in listed.items():
                assert set(row) == {'id', 'name', 'description', 'level'}, row  # no sample count
                assert (row['name'], row['description'], row['level']) == (
                    api[dataset_id]['name'],
                    api[dataset_id]['description'],
                    api[dataset_id]['access_level'],
                ), (user_id, dataset_id)
            for dataset_id in [*made, 999999]:
                response = await w.call(user_id, 'GET', f'/api/datasets/{dataset_id}')
                answer = await call(user_id, 'describe_dataset', {'dataset': dataset_id})
                if response.status_code == 200:
                    assert answer['ok'], (user_id, dataset_id, answer)
                    described = answer['result']['dataset']
                    own = response.json()
                    assert {k: described[k] for k in ('id', 'name', 'description', 'level')} == {
                        'id': dataset_id,
                        'name': own['name'],
                        'description': own['description'],
                        'level': own['access_level'],
                    }, (user_id, dataset_id)
                    assert described['samples'] == await api_count(w, user_id, dataset_id)
                elif response.status_code == 403:
                    assert answer['error']['code'] == 'refused', (user_id, dataset_id, answer)
                    assert answer['error']['message'] == detail(response) == DENIED, (user_id, dataset_id)
                else:
                    assert response.status_code == 404, response.text
                    assert answer['error']['code'] == 'not_found', (user_id, dataset_id, answer)
                    assert str(dataset_id) in answer['error']['message'], answer
        # The guest, whom the API shows their datasets, is refused every tool.
        assert (await w.call(GUEST, 'GET', '/api/datasets')).status_code == 200
        for name, arguments in [('list_datasets', {}), *[('describe_dataset', {'dataset': d}) for d in made]]:
            failure = await error(GUEST, name, arguments)
            assert failure == {'code': 'not_allowed', 'message': NOT_LET_IN, 'retryable': False}, (name, arguments)

    world(scenario)


def test_every_call_checks_the_account_the_door_and_the_access_afresh(world):
    async def scenario(w):
        await w.dataset(40)
        await add(40, [{'filepath': 'a.jpg', 'metadata': {'crop': 'corn'}}])
        await w.share(40, POWER, 'edit')
        await open_door(w)
        assert (await result(POWER, 'describe_dataset', {'dataset': 40}))['dataset']['level'] == 'edit'
        # Deactivated between two calls.
        await w.database.execute('UPDATE users SET is_active = FALSE WHERE id = $1', POWER)
        inactive = await api_refusal_of(w, POWER)
        assert await error(POWER, 'describe_dataset', {'dataset': 40}) == {
            'code': 'not_allowed',
            'message': inactive,
            'retryable': False,
        }
        await w.database.execute('UPDATE users SET is_active = TRUE WHERE id = $1', POWER)
        assert (await call(POWER, 'list_datasets'))['ok']
        # Made to change the password.
        await w.database.execute('UPDATE users SET must_change_password = TRUE WHERE id = $1', POWER)
        owes = await api_refusal_of(w, POWER)
        assert (await error(POWER, 'list_datasets'))['message'] == owes
        await w.database.execute('UPDATE users SET must_change_password = FALSE WHERE id = $1', POWER)
        # Access taken away.
        await set_share(w, 40, POWER, None)
        failure = await error(POWER, 'describe_dataset', {'dataset': 40})
        assert (failure['code'], failure['message']) == ('refused', DENIED)
        assert 40 not in {row['id'] for row in await every_dataset(POWER)}
        await set_share(w, 40, POWER, 'view')
        assert (await result(POWER, 'describe_dataset', {'dataset': 40}))['dataset']['level'] == 'view'
        # No longer let in, then the door switched off.
        await settings(w, {'roles': {'power_user': False}})
        assert (await error(POWER, 'list_datasets'))['message'] == NOT_LET_IN
        await settings(w, {'roles': {'power_user': True}, 'mcp_enabled': False})
        assert (await error(POWER, 'list_datasets'))['message'] == MCP_OFF
        assert (await error(ADMIN, 'list_datasets'))['message'] == MCP_OFF

    world(scenario)


def test_a_closed_dataset_is_refused_and_left_out_and_still_read_through_the_api(world):
    async def scenario(w):
        for dataset_id in (40, 41):
            await w.dataset(dataset_id)
            await w.share(dataset_id, POWER, 'manage')
            await add(dataset_id, [{'filepath': f'{dataset_id}.jpg', 'metadata': {'crop': 'corn'}}])
        view = await w.database.fetchval(
            "INSERT INTO views (dataset_id, user_id, name, slug, content, is_public) VALUES (40, $1, 'v', 'v40', $2, "
            'TRUE) RETURNING id',
            POWER,
            {'filters': []},
        )
        assert view
        await open_door(w)
        closed = await w.call(POWER, 'PATCH', '/api/datasets/40', json={'assistant_closed': True})
        assert closed.status_code == 200, closed.text
        for user_id in (POWER, ADMIN):
            failure = await error(user_id, 'describe_dataset', {'dataset': 40})
            assert (failure['code'], failure['message']) == ('refused', CLOSED), user_id
            assert {row['id'] for row in await every_dataset(user_id)} >= {41}
            assert 40 not in {row['id'] for row in await every_dataset(user_id)}, user_id
            assert 40 not in {row['id'] for row in await every_dataset(user_id, search='dataset-40')}
        # The API reads it as before.
        assert (await w.call(POWER, 'GET', '/api/datasets/40')).status_code == 200
        assert 40 in {row['id'] for row in (await w.call(POWER, 'GET', '/api/datasets')).json()}
        assert await api_count(w, POWER, 40) == 1
        assert (await w.call(POWER, 'GET', '/api/datasets/40/schema')).status_code == 200
        assert len((await w.call(POWER, 'GET', '/api/datasets/40/views')).json()) == 1
        # Opened again, it reads again.
        await w.call(POWER, 'PATCH', '/api/datasets/40', json={'assistant_closed': False})
        assert (await result(POWER, 'describe_dataset', {'dataset': 40}))['dataset']['id'] == 40

    world(scenario)


# Tables whose rows a tool must leave as they were.
UNTOUCHED = (
    'datasets',
    'samples',
    'dataset_fields',
    'views',
    'embedding_concepts',
    'dataset_versions',
    'version_snapshots',
    'dataset_access',
    'dataset_group_access',
    'groups',
    'group_members',
    'users',
)

HOSTILE = [
    -1,
    0,
    2**31,
    2**63,
    1.5,
    '1',
    '1; DROP TABLE samples',
    "'; DELETE FROM samples; --",
    None,
    True,
    [],
    [1],
    {'$ne': 1},
    '',
    '%',
    '_',
    '\\',
    'x' * 100_000,
    '\u0000',
    '😀',
]


async def snapshot(w: World) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for table in UNTOUCHED:
        state[table] = await w.database.fetchval(
            f'SELECT md5(string_agg(t::text, $$|$$ ORDER BY t::text)) FROM {table} t',
        )
    response = await w.call(ADMIN, 'GET', SETTINGS)
    state['settings'] = response.json()
    return state


def test_nothing_a_tool_does_writes(world):
    async def scenario(w):
        await prepare(w)
        for caller in CALLERS[:6]:
            await open_dataset(w, caller)
        await w.dataset(40)
        await add(40, [{'filepath': f'p{i}.jpg', 'metadata': {'crop': 'corn', 'n': i}} for i in range(5)])
        await w.database.execute(
            "INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) VALUES (40, 'c', 'clip', "
            "'seed', '[1,0]')",
        )
        await add_view(w, 40, ADMIN, False, 'mine')
        made = await w.call(ADMIN, 'POST', '/api/datasets/40/versions', json={'name': 'v1'})
        assert made.is_success, made.text
        await open_door(w)
        before = await snapshot(w)
        tools = [tool.name for tool in assistant.tools()]
        arguments: list[dict[str, Any]] = [{}, {'dataset': 40}, {'dataset': 100}, {'dataset': 999999}]
        for name in ('dataset', 'search', 'limit', 'offset'):
            arguments += [{name: value} for value in HOSTILE]
            arguments += [{'dataset': 40, name: value} for value in HOSTILE]
        arguments += [{'dataset': 40, 'unknown': 'x'}, {'dataset': 40, 'filters': {'operator': 'delete'}}]
        for _ in range(2):
            for user_id in (ADMIN, POWER, 999999):
                for name in [*tools, 'drop_dataset', '', 'list_datasets; DROP TABLE samples']:
                    for given in arguments:
                        await call(user_id, name, given)
        for name in ('drop_dataset', 'list_datasets; DROP TABLE samples'):
            failure = await error(ADMIN, name)
            assert failure['code'] == 'unknown_tool' and name in failure['message'], failure
        assert await snapshot(w) == before

    world(scenario)


async def add_view(w: World, dataset_id: int, owner: int, public: bool, name: str) -> None:
    await w.database.execute(
        'INSERT INTO views (dataset_id, user_id, name, slug, content, is_public) VALUES ($1, $2, $3, $4, $5, $6)',
        dataset_id,
        owner,
        name,
        f'{name}-{dataset_id}',
        {'filters': []},
        public,
    )


def test_arguments_that_break_a_schema_are_invalid(world):
    async def scenario(w):
        await w.dataset(40)
        await add(40, [{'filepath': 'a.jpg', 'metadata': {'crop': 'corn'}}])
        await open_door(w)
        for name, arguments in (
            ('describe_dataset', {}),
            ('describe_dataset', {'dataset': 'forty'}),
            ('describe_dataset', {'dataset': None}),
            ('describe_dataset', {'dataset': [40]}),
            ('describe_dataset', {'dataset': 40, 'limit': 0}),
            ('describe_dataset', {'dataset': 40, 'limit': 101}),
            ('describe_dataset', {'dataset': 40, 'limit': 'ten'}),
            ('describe_dataset', {'dataset': 40, 'offset': -1}),
            ('describe_dataset', {'dataset': 40, 'search': 5}),
            ('list_datasets', {'limit': 0}),
            ('list_datasets', {'limit': 51}),
            ('list_datasets', {'offset': -1}),
            ('list_datasets', {'search': ['a']}),
        ):
            failure = await error(ADMIN, name, arguments)
            assert failure['code'] == 'invalid', (name, arguments, failure)
        failure = await error(ADMIN, 'no_such_tool')
        assert failure['code'] == 'unknown_tool' and 'no_such_tool' in failure['message'], failure

    world(scenario)


# --- 4. The two tools ---


def test_list_datasets_pages_by_name_with_the_true_total_and_leaves_out_one_being_deleted(world):
    async def scenario(w):
        for number in range(30):
            await w.dataset(200 + number)
            await w.database.execute(
                'UPDATE datasets SET name = $2, description = $3 WHERE id = $1',
                200 + number,
                f'Field-Trials-{number:02d}' if number % 3 == 0 else f'plot-{number:02d}',
                f'about {number}',
            )
        await add(200, [{'filepath': f'{i}.jpg', 'metadata': {}} for i in range(7)])
        await open_door(w)
        first = await result(ADMIN, 'list_datasets')
        total = first['total']
        assert total >= 30 and len(first['datasets']) == 25 and first['next_offset'] == 25, first
        everything = await every_dataset(ADMIN)
        names = [row['name'] for row in everything]
        assert names == sorted(names), names
        assert len(everything) == total
        assert all('samples' not in row for row in everything)
        assert all(row['level'] == 'manage' for row in everything)
        last = await result(ADMIN, 'list_datasets', {'offset': 25, 'limit': 50})
        assert last['next_offset'] is None and len(last['datasets']) == total - 25
        found = await every_dataset(ADMIN, search='TRIALS')
        assert [row['id'] for row in found] == [200 + n for n in range(0, 30, 3)]
        page = await result(ADMIN, 'list_datasets', {'search': 'trials', 'limit': 4})
        assert (page['total'], len(page['datasets']), page['next_offset']) == (10, 4, 4)
        deleting = await w.call(ADMIN, 'DELETE', '/api/datasets/203')
        assert deleting.is_success, deleting.text
        assert 203 not in {row['id'] for row in await every_dataset(ADMIN)}
        assert (await result(ADMIN, 'list_datasets', {'search': 'trials'}))['total'] == 9

    world(scenario)


FARM = 500
CROPS = {'corn': 6, 'soy': 5, 'rye': 4, 'oat': 3, 'flax': 2, 'hemp': 1}
SECRETS = [f'ZQX-PLOT-{n:04d}' for n in range(12)]
SECRET_NUMBERS = [987654.25 + n for n in range(12)]
PESTS = [['aphid'], ['aphid', 'mite'], ['beetle']]  # a registered array field's values, on samples 0-8


def farm_samples() -> list[dict[str, Any]]:
    crops = [crop for crop, count in CROPS.items() for _ in range(count)]  # 21
    samples = []
    for index in range(24):
        metadata: dict[str, Any] = {'yield': 1.5 + index * 0.25 if index < 20 else None}
        if metadata['yield'] is None:
            metadata.pop('yield')
        if index < len(crops):
            metadata['crop'] = crops[index]
        if index < 7:
            metadata['checked'] = index < 4
        if index < 12:
            metadata['plot_code'] = SECRETS[index]
            metadata['yield_secret'] = SECRET_NUMBERS[index]
        if index < 5:
            metadata['seen_at'] = f'2026-03-0{index + 1}T08:00:00Z'
        if index < 6:
            metadata['labels'] = ['weed', 'stone'] if index % 2 else ['weed']
        if index < 3:
            metadata['box'] = {'x': index}
        sample: dict[str, Any] = {
            'filepath': f'farm-{index:02d}.jpg',
            'metadata': metadata,
            'tags': ['sunny', 'windy'] if index % 3 == 0 else ['sunny'],
        }
        if index < 9:
            sample['array_fields'] = {'pests': PESTS[index % 3]}
        if index < 10:
            sample['location'] = {'type': 'Point', 'coordinates': [10.0 + index, 50.0]}
        samples.append(sample)
    return samples


async def farm(w: World) -> list[int]:
    """The dataset the describe tests read: planted values, two private fields, counted statistics."""
    await w.dataset(FARM)
    await w.database.execute(
        "UPDATE datasets SET name = 'field-trials', description = 'Trials of four crops' WHERE id = $1",
        FARM,
    )
    registered = await w.call(ADMIN, 'POST', f'/api/datasets/{FARM}/array-fields', json={'field_name': 'pests'})
    assert registered.is_success, registered.text
    ids = await add(FARM, farm_samples())
    vectors = [{'sample_id': s, 'vector': [1.0, i / 100, 0.0, 0.0], 'field_name': 'clip'} for i, s in enumerate(ids)]
    assert (await w.call(ADMIN, 'POST', f'/api/datasets/{FARM}/embeddings', json=vectors)).is_success
    for name in ('weeds', 'stones'):
        await w.database.execute(
            "INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) VALUES ($1, $2, 'clip', "
            "'seed', '[1,0,0,0]')",
            FARM,
            name,
        )
    await add_view(w, FARM, ADMIN, False, 'mine')
    await add_view(w, FARM, OTHER_POWER, True, 'theirs-public')
    await add_view(w, FARM, OTHER_POWER, False, 'theirs-private')
    for field, body in (
        ('crop', {'description': 'What was sown in the plot.'}),
        ('yield', {'description': 'Harvest per crop row.', 'unit': 't/ha'}),
        ('plot_code', {'values_private': True}),
        ('yield_secret', {'values_private': True, 'description': 'A number nobody should see.'}),
    ):
        response = await w.call(ADMIN, 'PATCH', f'/api/datasets/{FARM}/fields/{field}/description', json=body)
        assert response.status_code == 200, response.text
    await run_refresh(w.database, FARM)
    return ids


async def schema_of(w: World, user_id: int = ADMIN) -> list[dict[str, Any]]:
    response = await w.call(user_id, 'GET', f'/api/datasets/{FARM}/schema')
    assert response.status_code == 200, response.text
    return response.json()


async def every_field(user_id: int, **arguments: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    offset, total = 0, None
    while True:
        page = await result(user_id, 'describe_dataset', {'dataset': FARM, **arguments, 'offset': offset})
        assert page['offset'] == offset and (total is None or page['fields_total'] == total)
        total = page['fields_total']
        found.extend(page['fields'])
        if page['next_offset'] is None:
            break
        assert page['next_offset'] == offset + len(page['fields'])
        offset = page['next_offset']
    assert len(found) == total
    return found


FIELD_KEYS = {
    'name',
    'type',
    'description',
    'unit',
    'description_source',
    'samples_with_value',
    'range',
    'values',
    'values_private',
    'complete',
    'indexed',  # m1-contract-assistant-cost 1.1
}
TYPES = {
    'id': 'number',
    'filepath': 'text',
    'media_type': 'text',
    'created_at': 'datetime',
    'last_modified_at': 'datetime',
    'tags': 'list',
    'crop': 'text',
    'yield': 'number',
    'checked': 'boolean',
    'plot_code': 'text',
    'yield_secret': 'number',
    'seen_at': 'datetime',
    'labels': 'list',
    'pests': 'list',
    'box': 'object',
}


def test_describe_dataset_gives_the_schemas_fields_with_their_true_numbers(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        page = await result(ADMIN, 'describe_dataset', {'dataset': FARM})
        assert set(page) == {
            'dataset',
            'fields',
            'fields_total',
            'offset',
            'next_offset',
            'undescribed_fields',
            'samples_with_location',
            'embedding_fields',
            'concepts',
            'saved_views',
        }, page
        assert page['dataset'] == {
            'id': FARM,
            'name': 'field-trials',
            'description': 'Trials of four crops',
            'samples': 24,
            'objects': 0,
            'level': 'manage',
        }
        schema = await schema_of(w)
        fields = await every_field(ADMIN)
        assert [field['name'] for field in fields] == [entry['name'] for entry in schema]  # the schema's, in order
        assert page['fields_total'] == len(schema) and page['offset'] == 0
        assert page['next_offset'] == (50 if len(schema) > 50 else None)
        assert page['undescribed_fields'] == sum(1 for entry in schema if entry['description_source'] is None)
        assert (page['samples_with_location'], page['embedding_fields'], page['concepts']) == (10, ['clip'], 2)
        await w.share(FARM, OTHER_POWER, 'view')
        for user_id in (ADMIN, OTHER_POWER):
            seen = len((await w.call(user_id, 'GET', f'/api/datasets/{FARM}/views')).json())
            assert (await result(user_id, 'describe_dataset', {'dataset': FARM}))['saved_views'] == seen, user_id
        by_name = {field['name']: field for field in fields}
        by_schema = {entry['name']: entry for entry in schema}
        for name, field in by_name.items():
            assert set(field) == FIELD_KEYS, field
            for key in ('description', 'unit', 'description_source', 'values_private'):
                assert field[key] == by_schema[name][key], (name, key)
            assert isinstance(field['complete'], bool), field
        for name, kind in TYPES.items():
            assert by_name[name]['type'] == kind, (name, by_name[name]['type'])
        crop = by_name['crop']
        assert (crop['samples_with_value'], crop['range'], crop['complete']) == (21, None, True)
        assert crop['values']['distinct'] == 6  # the kept list is whole
        assert_examples(crop['values'], set(CROPS), 5)
        crop_yield = by_name['yield']
        assert (crop_yield['samples_with_value'], crop_yield['values'], crop_yield['complete']) == (20, None, True)
        assert crop_yield['range'] == {'min': 1.5, 'max': 1.5 + 19 * 0.25}
        assert (crop_yield['description'], crop_yield['unit'], crop_yield['description_source']) == (
            'Harvest per crop row.',
            't/ha',
            'set',
        )
        checked = by_name['checked']
        assert checked['samples_with_value'] == 7 and checked['range'] is None
        assert checked['values'] is None, checked  # a boolean's values are true and false
        seen_at = by_name['seen_at']
        assert seen_at['samples_with_value'] == 5 and seen_at['values'] is None
        assert seen_at['range'] is not None and seen_at['range']['min'] < seen_at['range']['max']
        # List fields: samples_with_value is a true count of samples with a value, or null where the server keeps
        # none. A metadata list's kept count is true (6). The statistics count every row of an array column (tags,
        # a registered array field), which is not a count of samples with an element: null, or the true number
        # (every farm sample has a tag; 9 hold a pest), never the row count.
        kept = await w.database.fetchval(
            "SELECT sample_count FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = 'labels'",
            FARM,
        )
        assert kept == 6, kept
        assert by_name['labels']['samples_with_value'] == 6, by_name['labels']
        for name, holding in (('tags', 24), ('pests', 9)):
            assert by_name[name]['samples_with_value'] in (None, holding), by_name[name]
        for name in ('labels', 'tags', 'pests'):
            assert (by_name[name]['type'], by_name[name]['range']) == ('list', None), by_name[name]
        # A list kept in metadata: the server keeps no value list for it, so the tool gives none.
        assert by_name['labels']['values'] is None, by_name['labels']
        # The built-in tags and a registered array field: the statistics collect their elements.
        assert by_name['tags']['values']['distinct'] == 2, by_name['tags']
        assert_examples(by_name['tags']['values'], {'sunny', 'windy'}, 2)
        assert by_name['pests']['values']['distinct'] == 3, by_name['pests']
        assert_examples(by_name['pests']['values'], {'aphid', 'mite', 'beetle'}, 3)
        assert by_name['box']['range'] is None and by_name['box']['values'] is None
        assert by_name['id']['range'] is not None

    world(scenario)


def assert_examples(values: dict[str, Any], real: set[Any], how_many: int) -> None:
    """`values` has a distinct count and up to five examples, real values of the field, no two the same, no counts."""
    assert set(values) - {'many'} == {'distinct', 'examples'} and not values.get('many'), values
    examples = values['examples']
    assert isinstance(examples, list) and len(examples) == min(5, how_many), values
    assert len({json.dumps(example) for example in examples}) == len(examples), values  # distinct from each other
    assert set(examples) <= real, values
    assert 'count' not in json.dumps(values), values


def test_describe_dataset_searches_names_and_descriptions_and_pages(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        schema = await schema_of(w)
        matching = [
            entry['name']
            for entry in schema
            if 'crop' in entry['name'].lower() or 'crop' in (entry['description'] or '').lower()
        ]
        assert {'crop', 'yield'} <= set(matching)  # by name, and by its description
        for text in ('crop', 'CROP', 'Crop'):
            assert [field['name'] for field in await every_field(ADMIN, search=text)] == matching, text
        page = await result(ADMIN, 'describe_dataset', {'dataset': FARM, 'search': 'crop', 'limit': 1})
        assert (page['fields_total'], len(page['fields']), page['next_offset']) == (len(matching), 1, 1)
        assert page['undescribed_fields'] == sum(1 for entry in schema if entry['description_source'] is None)
        last = await result(ADMIN, 'describe_dataset', {'dataset': FARM, 'limit': 100, 'offset': len(schema) - 2})
        assert len(last['fields']) == 2 and last['next_offset'] is None
        assert (await result(ADMIN, 'describe_dataset', {'dataset': FARM, 'search': 'nothing-like-it'}))['fields'] == []
        # One or more words: a field matches when any of them is in its name or its description.

        def any_word(words: str) -> list[str]:
            return [
                entry['name']
                for entry in schema
                if any(
                    word in entry['name'].lower() or word in (entry['description'] or '').lower()
                    for word in words.lower().split()
                )
            ]

        for words in ('crop nothing-like-it', 'checked LABELS', 'harvest  sown', 'yield crop box'):
            expected = any_word(words)
            assert expected, words
            assert [field['name'] for field in await every_field(ADMIN, search=words)] == expected, words

    world(scenario)


def test_a_field_whose_statistics_are_not_whole_says_so(world):
    async def scenario(w):
        await farm(w)
        await add(FARM, [{'filepath': f'late-{i}.jpg', 'metadata': {'late': f'v{i}'}} for i in range(3)])
        await open_door(w)
        late = {field['name']: field for field in await every_field(ADMIN)}['late']
        assert late['complete'] is False, late
        assert late['values'] is None or late['values']['distinct'] is None, late  # not presented as the whole

    world(scenario)


def test_a_private_fields_values_appear_nowhere(world):
    async def scenario(w):
        await farm(w)
        await w.share(FARM, POWER, 'view')
        await open_door(w)
        answers: list[Any] = []
        for user_id in (ADMIN, POWER):
            answers.append(await call(user_id, 'list_datasets'))
            for arguments in (
                {},
                {'search': 'plot'},
                {'search': 'secret'},
                {'search': 'ZQX'},
                {'search': SECRETS[0]},
                {'limit': 1, 'offset': 0},
                {'limit': 100},
            ):
                answers.append(await call(user_id, 'describe_dataset', {'dataset': FARM, **arguments}))
            for tool in assistant.tools():  # every tool on the list, so one added later is searched too
                properties = plain(tool.input_schema).get('properties', {})
                for private in ('plot_code', 'yield_secret'):
                    known: dict[str, Any] = {'dataset': FARM, 'field': private}
                    named = {key: value for key, value in known.items() if key in properties}
                    answers.append(await call(user_id, tool.name, named))
            fields = {field['name']: field for field in await every_field(user_id)}
            for name in ('plot_code', 'yield_secret'):
                field = fields[name]
                assert field['values_private'] is True and field['range'] is None, field
                given = field['values']
                if given is not None and given.get('many') is False:
                    given = {key: value for key, value in given.items() if key != 'many'}
                assert given is None or given == {'distinct': 12, 'examples': []}, field
                assert field['samples_with_value'] == 12, field
        text = json.dumps(answers)
        for secret in [*SECRETS, *[json.dumps(n) for n in SECRET_NUMBERS], 'ZQX-PLOT', '98765']:
            assert secret not in text, secret

    world(scenario)


def test_describe_datasets_search_never_matches_a_private_fields_values(world):
    """A private field can't be questioned: `search` matches names and descriptions only, so the answer for a value
    the field holds is the answer for one it doesn't."""

    async def scenario(w):
        await farm(w)
        await open_door(w)
        nothing = await call(ADMIN, 'describe_dataset', {'dataset': FARM, 'search': 'ZQX-PLOT-9999'})
        assert nothing['ok'] and nothing['result']['fields'] == [] and nothing['result']['fields_total'] == 0, nothing
        for planted in (SECRETS[3], 'ZQX-PLOT', 'zqx', json.dumps(SECRET_NUMBERS[0]), '987654'):
            answer = await call(ADMIN, 'describe_dataset', {'dataset': FARM, 'search': planted})
            assert answer == nothing, (planted, answer)
        # Several words, the planted values among them: still names and descriptions only.
        nothing_of_two = await call(ADMIN, 'describe_dataset', {'dataset': FARM, 'search': 'ZQX-PLOT-9999 111111'})
        assert nothing_of_two == nothing, nothing_of_two
        assert await call(ADMIN, 'describe_dataset', {'dataset': FARM, 'search': f'{SECRETS[3]} 987654'}) == nothing
        crop = await call(ADMIN, 'describe_dataset', {'dataset': FARM, 'search': 'crop'})
        for words in (f'crop {SECRETS[3]}', 'crop ZQX-PLOT-9999', f'{json.dumps(SECRET_NUMBERS[0])} crop'):
            assert await call(ADMIN, 'describe_dataset', {'dataset': FARM, 'search': words}) == crop, words

    world(scenario)


def test_describe_dataset_counts_what_a_view_of_the_dataset_counts_and_its_objects_besides(world):
    async def scenario(w):
        await w.dataset(41)
        images = await add(41, [{'filepath': f'image-{i}.jpg', 'metadata': {'crop': 'corn'}} for i in range(3)])
        objects = [
            {'filepath': f'object-{i}.jpg', 'media_type': 'object', 'metadata': {'parent_id': images[i % 2]}}
            for i in range(5)
        ]
        await add(41, objects)
        assert (
            await w.database.fetchval("SELECT count(*) FROM samples WHERE dataset_id = 41 AND media_type = 'object'")
            == 5
        )
        await open_door(w)
        described_ = (await result(ADMIN, 'describe_dataset', {'dataset': 41}))['dataset']
        assert (described_['samples'], described_['objects']) == (3, 5), described_
        # Without objects, none besides.
        await w.dataset(42)
        await add(42, [{'filepath': 'only.jpg', 'metadata': {}}])
        described_ = (await result(ADMIN, 'describe_dataset', {'dataset': 42}))['dataset']
        assert (described_['samples'], described_['objects']) == (1, 0), described_

    world(scenario)


def test_a_dataset_that_is_missing_or_being_deleted_is_not_found(world):
    async def scenario(w):
        await w.dataset(40)
        await add(40, [{'filepath': 'a.jpg', 'metadata': {}}])
        await open_door(w)
        missing = await error(ADMIN, 'describe_dataset', {'dataset': 999999})
        assert missing['code'] == 'not_found' and '999999' in missing['message'], missing
        assert (await w.call(ADMIN, 'GET', '/api/datasets/999999')).status_code == 404
        assert (await w.call(ADMIN, 'DELETE', '/api/datasets/40')).is_success
        going = await error(ADMIN, 'describe_dataset', {'dataset': 40})
        assert going['code'] == 'not_found' and 'deleted' in going['message'].lower(), going

    world(scenario)


def test_the_checks_go_in_their_order(world):
    """not_allowed, then unknown_tool, then invalid, then the read itself."""

    async def scenario(w):
        await w.dataset(40)
        await w.dataset(41)
        await add(40, [{'filepath': 'a.jpg', 'metadata': {}}])
        # The door off: not_allowed before anything about the tool or its arguments.
        for name, arguments in (('no_such_tool', {}), ('describe_dataset', {'dataset': 'forty'})):
            assert (await error(ADMIN, name, arguments))['code'] == 'not_allowed', name
        await open_door(w, admin=True, power_user=False, guest=False)
        assert (await error(POWER, 'no_such_tool', {'dataset': 'forty'}))['code'] == 'not_allowed'
        # Then an unknown tool, whatever its arguments.
        assert (await error(ADMIN, 'no_such_tool', {'dataset': 'forty', 'limit': 0}))['code'] == 'unknown_tool'
        # Then arguments, before the read: a dataset that doesn't exist, is closed, or can't be opened.
        await w.call(ADMIN, 'PATCH', '/api/datasets/41', json={'assistant_closed': True})
        for dataset_id in (999999, 41, 40):
            failure = await error(ADMIN, 'describe_dataset', {'dataset': dataset_id, 'limit': 0})
            assert failure['code'] == 'invalid', (dataset_id, failure)
        await settings(w, {'roles': {'power_user': True}})
        assert (await error(POWER, 'describe_dataset', {'dataset': 40, 'limit': 0}))['code'] == 'invalid'
        assert (await error(POWER, 'describe_dataset', {'dataset': 40}))['code'] == 'refused'

    world(scenario)


WIDE = 600
# More different values than the statistics keep a list of, whatever kind of field they count it as.
SERIALS = max(statistics_measure.VALUE_LIMIT, statistics_measure.value_limit('enum')) + 50
STAT_COLUMNS = 'field_name, sample_count, unique_values, values_complete, min_value, max_value, last_updated'


class statements:
    """Count the statements every database connection runs, while open."""

    METHODS = ('fetch', 'fetchrow', 'fetchval', 'execute', 'prepare', 'cursor')

    def __enter__(self) -> list[str]:
        self.seen: list[str] = []
        self.originals = {name: getattr(asyncpg.Connection, name) for name in self.METHODS}
        for name, original in self.originals.items():
            setattr(asyncpg.Connection, name, self.wrap(original))
        return self.seen

    def wrap(self, original: Any) -> Any:
        seen = self.seen

        def record(connection: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(str(query))
            return original(connection, query, *args, **kwargs)

        return record

    def __exit__(self, *_: Any) -> None:
        for name, original in self.originals.items():
            setattr(asyncpg.Connection, name, original)


def test_describe_dataset_reads_the_kept_statistics_and_never_counts_the_samples(world):
    """From outside: a page of 2 fields and a page of 50 run as many statements; describing recounts nothing and
    queues nothing; a list the server holds only part of has no distinct count; and after a sample changes behind the
    statistics' back, the answer is the kept statistics', with `complete` as the sidebar reports the field."""

    async def scenario(w):
        await w.dataset(WIDE)
        samples = [
            {
                'filepath': f'wide-{i:04d}.jpg',
                'metadata': {'serial': f'SN-{i:04d}', **{f'f{n:02d}': f'v{(i + n) % 3}' for n in range(60)}},
            }
            for i in range(SERIALS)
        ]
        await add(WIDE, samples)
        await run_refresh(w.database, WIDE)
        await open_door(w)
        kept = 'SELECT {columns} FROM dataset_field_stats WHERE dataset_id = $1 ORDER BY field_name'.format(
            columns=STAT_COLUMNS,
        )
        before = [tuple(row) for row in await w.database.fetch(kept, WIDE)]
        jobs = await w.database.fetchval('SELECT count(*) FROM jobs')
        await result(ADMIN, 'describe_dataset', {'dataset': WIDE, 'limit': 2})  # warm
        with statements() as small:
            page = await result(ADMIN, 'describe_dataset', {'dataset': WIDE, 'limit': 2})
        with statements() as big:
            wide = await result(ADMIN, 'describe_dataset', {'dataset': WIDE, 'limit': 50})
        assert (len(page['fields']), len(wide['fields'])) == (2, 50)
        assert len(big) <= len(small) + 1, (len(small), len(big))
        assert [tuple(row) for row in await w.database.fetch(kept, WIDE)] == before  # nothing recounted
        assert await w.database.fetchval('SELECT count(*) FROM jobs') == jobs  # nothing queued
        # More serials than the server keeps a list of: `many`, not incomplete.
        serial = {field['name']: field for field in await every_field(ADMIN, dataset=WIDE)}['serial']
        assert serial['complete'] is True, serial
        values = serial['values']
        assert set(values) == {'distinct', 'examples', 'many'} and (values['distinct'], values['many']) == (None, True)
        examples = values['examples']
        assert len(examples) <= 5 and len(set(examples)) == len(examples), values
        assert set(examples) <= {f'SN-{i:04d}' for i in range(SERIALS)} and 'count' not in json.dumps(values), values
        assert serial['samples_with_value'] == SERIALS
        # A sample's value changed straight in the table, the statistics not counted again.
        first = await w.database.fetchval('SELECT min(id) FROM samples WHERE dataset_id = $1', WIDE)
        await w.database.execute(
            """UPDATE samples SET metadata = jsonb_set(metadata, '{f00}', '"brand-new"') WHERE id = $1""",
            first,
        )
        field = {item['name']: item for item in await every_field(ADMIN, dataset=WIDE)}['f00']
        assert field['complete'] is False, field  # changed since counted
        assert field['values'] is None or field['values']['distinct'] is None, field
        assert 'brand-new' not in json.dumps(field), field  # the kept statistics', not a count of the samples

    world(scenario)
