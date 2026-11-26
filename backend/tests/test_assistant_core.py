"""The assistant's core (backend/assistant, docs/ASSISTANT.md): who may use it, the settings, a dataset closed to it,
the two tools, and the fences around a tool's requests: it reads in process as the user, only through the routes it
lists, never holding a pooled connection across a request, and writes nothing."""

import hashlib
import inspect
import json
import os
import secrets
from typing import Any, Optional

import httpx
import pytest

from backend import access, assistant, main
from backend import database as database_module
from backend.assistant import api as assistant_api
from backend.assistant import inner, private, rule
from backend.assistant.identity import SCOPE_KEY, Identity
from backend.crypto import hash_token
from backend.routers import auth
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

ADMIN, POWER, GUEST, OTHER = 1, 10, 20, 30
ROLES = {ADMIN: 'admin', POWER: 'power_user', GUEST: 'guest', OTHER: 'power_user'}
TRIALS, CLOSED, HIDDEN = 1, 2, 3  # dataset ids: one shared, one closed to the assistant, one not shared
CLOSED_WORDS = 'The assistant may not read this dataset'


class World:
    def __init__(self, database: Any, client: httpx.AsyncClient):
        self.database = database
        self.client = client
        self.tokens: dict[int, str] = {}

    async def user(self, user_id: int, role: str) -> None:
        await self.database.execute(
            'INSERT INTO users (id, username, role) VALUES ($1, $2, $3) '
            'ON CONFLICT (id) DO UPDATE SET role = EXCLUDED.role, must_change_password = FALSE',
            user_id,
            f'person-{user_id}',
            role,
        )
        raw = secrets.token_urlsafe(32)
        await self.database.execute(
            "INSERT INTO api_tokens (user_id, token, name) VALUES ($1, $2, 'assistant-core')",
            user_id,
            hash_token(raw),
        )
        self.tokens[user_id] = raw

    async def api(self, user_id: Optional[int], method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {} if user_id is None else {'Authorization': f'Bearer {self.tokens[user_id]}'}
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def share(self, dataset_id: int, user_id: int, level: str) -> None:
        await self.database.execute(
            'INSERT INTO dataset_access (dataset_id, user_id, level) VALUES ($1, $2, $3) '
            'ON CONFLICT (dataset_id, user_id) DO UPDATE SET level = EXCLUDED.level',
            dataset_id,
            user_id,
            access.level_number(level),
        )

    async def switch_on(self, **roles: bool) -> None:
        body: dict[str, Any] = {'mcp_enabled': True}
        if roles:
            body['roles'] = roles
        response = await self.api(ADMIN, 'PATCH', '/api/admin/assistant', json=body)
        assert response.status_code == 200, response.text

    async def tool(self, user_id: int, name: str, door: str = 'mcp', **arguments: Any) -> dict[str, Any]:
        answer = await assistant.call_tool(user_id=user_id, door=door, name=name, arguments=arguments)
        json.dumps(answer)  # plain JSON values, as it is
        return answer


@pytest.fixture
def world(registry_database, monkeypatch):
    def run(scenario, pool_size=5):
        async def with_app(database):
            monkeypatch.setattr(database_module.db, 'pool', database.pool)
            await database.execute("UPDATE datasets SET is_global = FALSE, name = 'field-trials' WHERE id = 1")
            await database.execute("UPDATE datasets SET is_global = FALSE, name = 'Closed one' WHERE id = 2")
            await database.execute("INSERT INTO datasets (id, name) VALUES (3, 'not shared')")
            await database.execute("SELECT setval(pg_get_serial_sequence('datasets', 'id'), 1000)")
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                world = World(database, client)
                for user_id, role in ROLES.items():
                    await world.user(user_id, role)
                for dataset_id in (TRIALS, CLOSED):
                    await world.share(dataset_id, POWER, 'view')
                await database.execute('UPDATE datasets SET assistant_closed = TRUE WHERE id = $1', CLOSED)
                await scenario(world)

        registry_database(with_app, pool_size=pool_size)

    return run


def refused(answer: dict[str, Any], code: str) -> str:
    assert answer['ok'] is False, answer
    assert answer['error']['code'] == code, answer
    assert isinstance(answer['error']['retryable'], bool)
    return answer['error']['message']


# --- who may use it ---


def test_a_fresh_server_has_the_mcp_door_off_and_administrators_let_in(world):
    async def scenario(world):
        settings = await world.api(ADMIN, 'GET', '/api/admin/assistant')
        assert settings.status_code == 200
        assert settings.json() == {
            'mcp_enabled': False,
            'roles': {'admin': True, 'power_user': False, 'guest': False},
            'groups': [],
            'users': [],
            'new_datasets_closed': False,
            'sample_fields': False,
        }
        for user_id in ROLES:
            assert await assistant.door_refusal(user_id=user_id, door='mcp') == rule.MCP_OFF
            assert refused(await world.tool(user_id, 'list_datasets'), 'not_allowed') == rule.MCP_OFF
        me = await world.api(ADMIN, 'GET', '/api/auth/me')
        assert me.json()['assistant'] == {'mcp': False}

    world(scenario)


def test_the_rule_in_its_order(world):
    async def scenario(world):
        await world.switch_on()
        assert await assistant.door_refusal(user_id=ADMIN, door='mcp') is None
        assert await assistant.door_refusal(user_id=POWER, door='mcp') == rule.NOT_LET_IN
        assert await assistant.door_refusal(user_id=GUEST, door='mcp') == rule.NOT_LET_IN
        # The chat door is off whatever the settings say.
        assert await assistant.door_refusal(user_id=ADMIN, door='chat') == rule.CHAT_OFF
        assert refused(await world.tool(ADMIN, 'list_datasets', door='chat'), 'not_allowed') == rule.CHAT_OFF
        # A listed group lets its members in.
        group = await world.database.fetchval("INSERT INTO groups (name) VALUES ('agronomy') RETURNING id")
        await world.database.execute('INSERT INTO group_members (group_id, user_id) VALUES ($1, $2)', group, POWER)
        assert (await world.api(ADMIN, 'PUT', f'/api/admin/assistant/groups/{group}')).status_code == 200
        assert await assistant.door_refusal(user_id=POWER, door='mcp') is None
        # A user's own answer decides before their groups and role, both ways.
        await world.api(ADMIN, 'PUT', f'/api/admin/assistant/users/{POWER}', json={'allowed': False})
        await world.api(ADMIN, 'PUT', f'/api/admin/assistant/users/{ADMIN}', json={'allowed': False})
        await world.api(ADMIN, 'PUT', f'/api/admin/assistant/users/{GUEST}', json={'allowed': True})
        assert await assistant.door_refusal(user_id=POWER, door='mcp') == rule.NOT_LET_IN
        assert await assistant.door_refusal(user_id=ADMIN, door='mcp') == rule.NOT_LET_IN
        assert await assistant.door_refusal(user_id=GUEST, door='mcp') is None
        await world.api(ADMIN, 'DELETE', f'/api/admin/assistant/users/{ADMIN}')
        assert await assistant.door_refusal(user_id=ADMIN, door='mcp') is None
        # A demo account never may, whatever its own answer says.
        await world.database.execute('UPDATE users SET is_demo_account = TRUE WHERE id = $1', GUEST)
        assert await assistant.door_refusal(user_id=GUEST, door='mcp') == rule.NOT_LET_IN
        # The account's own state, in the API's own words; after the door, before the rest.
        await world.database.execute('UPDATE users SET must_change_password = TRUE WHERE id = $1', ADMIN)
        assert await assistant.door_refusal(user_id=ADMIN, door='mcp') == rule.PASSWORD_CHANGE_REQUIRED
        assert (await world.api(ADMIN, 'GET', '/api/datasets')).json()['detail'] == rule.PASSWORD_CHANGE_REQUIRED
        await world.database.execute('UPDATE users SET is_active = FALSE WHERE id = $1', POWER)
        assert await assistant.door_refusal(user_id=POWER, door='mcp') == rule.ACCOUNT_INACTIVE
        assert (await world.api(POWER, 'GET', '/api/datasets')).json()['detail'] == rule.ACCOUNT_INACTIVE
        assert await assistant.door_refusal(user_id=987654, door='mcp') == rule.ACCOUNT_INACTIVE
        odd_ids: tuple[Any, ...] = (2**40, -1, '7', None, True)
        for odd in odd_ids:
            assert await assistant.door_refusal(user_id=odd, door='mcp') == rule.ACCOUNT_INACTIVE
        await world.database.execute('UPDATE assistant_settings SET mcp_enabled = FALSE')
        assert await assistant.door_refusal(user_id=POWER, door='mcp') == rule.MCP_OFF

    world(scenario)


def test_each_call_reads_the_account_afresh(world):
    async def scenario(world):
        await world.switch_on(power_user=True)
        assert (await world.tool(POWER, 'list_datasets'))['ok']
        await world.database.execute('UPDATE users SET is_active = FALSE WHERE id = $1', POWER)
        assert refused(await world.tool(POWER, 'list_datasets'), 'not_allowed') == rule.ACCOUNT_INACTIVE
        await world.database.execute('UPDATE users SET is_active = TRUE WHERE id = $1', POWER)
        await world.database.execute('DELETE FROM dataset_access WHERE user_id = $1 AND dataset_id = $2', POWER, TRIALS)
        assert refused(await world.tool(POWER, 'describe_dataset', dataset=TRIALS), 'refused') == (
            'Access denied for this dataset'
        )

    world(scenario)


def test_the_settings_are_for_administrators(world):
    async def scenario(world):
        for method, path, body in (
            ('GET', '/api/admin/assistant', None),
            ('PATCH', '/api/admin/assistant', {'mcp_enabled': True}),
            ('PUT', '/api/admin/assistant/groups/1', None),
            ('DELETE', '/api/admin/assistant/groups/1', None),
            ('PUT', f'/api/admin/assistant/users/{POWER}', {'allowed': True}),
            ('DELETE', f'/api/admin/assistant/users/{POWER}', None),
        ):
            for user_id in (POWER, GUEST):
                response = await world.api(user_id, method, path, json=body)
                assert response.status_code == 403, (method, path, response.text)
                assert response.json() == {'detail': 'Requires admin privileges'}
            assert (await world.api(None, method, path, json=body)).status_code == 401
        assert (await world.api(ADMIN, 'GET', '/api/admin/assistant')).json()['mcp_enabled'] is False
        assert (await world.api(ADMIN, 'PUT', '/api/admin/assistant/groups/98765')).status_code == 404
        assert (await world.api(ADMIN, 'DELETE', '/api/admin/assistant/groups/98765')).status_code == 404
        missing = await world.api(ADMIN, 'PUT', '/api/admin/assistant/users/98765', json={'allowed': True})
        assert missing.status_code == 404
        assert (await world.api(ADMIN, 'DELETE', '/api/admin/assistant/users/98765')).status_code == 404

    world(scenario)


def test_patch_changes_only_what_it_names_and_entries_go_with_their_group_or_user(world):
    async def scenario(world):
        response = await world.api(ADMIN, 'PATCH', '/api/admin/assistant', json={'roles': {'guest': True}})
        assert response.json()['roles'] == {'admin': True, 'power_user': False, 'guest': True}
        assert response.json()['mcp_enabled'] is False
        response = await world.api(ADMIN, 'PATCH', '/api/admin/assistant', json={'new_datasets_closed': True})
        assert response.json()['roles']['guest'] is True and response.json()['new_datasets_closed'] is True
        unknown = await world.api(ADMIN, 'PATCH', '/api/admin/assistant', json={'roles': {'owner': True}})
        assert unknown.status_code == 422
        group = await world.database.fetchval("INSERT INTO groups (name) VALUES ('agronomy') RETURNING id")
        response = await world.api(ADMIN, 'PUT', f'/api/admin/assistant/groups/{group}')
        assert response.json()['groups'] == [{'id': group, 'name': 'agronomy'}]
        response = await world.api(ADMIN, 'PUT', f'/api/admin/assistant/users/{OTHER}', json={'allowed': False})
        assert response.json()['users'] == [{'id': OTHER, 'username': f'person-{OTHER}', 'allowed': False}]
        assert (await world.api(ADMIN, 'DELETE', f'/api/groups/{group}')).status_code == 204
        assert (await world.api(ADMIN, 'DELETE', f'/api/users/{OTHER}')).status_code == 200
        response = await world.api(ADMIN, 'GET', '/api/admin/assistant')
        assert response.json()['groups'] == [] and response.json()['users'] == []

    world(scenario)


def test_auth_me_says_whether_the_mcp_door_is_open_to_its_user(world):
    async def scenario(world):
        await world.switch_on()
        assert (await world.api(ADMIN, 'GET', '/api/auth/me')).json()['assistant'] == {'mcp': True}
        assert (await world.api(POWER, 'GET', '/api/auth/me')).json()['assistant'] == {'mcp': False}

    world(scenario)


# --- datasets closed to it ---


def test_closing_needs_manage_and_changes_nothing_outside_the_assistant(world):
    async def scenario(world):
        await world.share(TRIALS, OTHER, 'edit')
        response = await world.api(OTHER, 'PATCH', f'/api/datasets/{TRIALS}', json={'assistant_closed': True})
        assert response.status_code == 403
        assert response.json()['detail'] == 'This needs manage access to the dataset'
        assert await world.database.fetchval('SELECT assistant_closed FROM datasets WHERE id = $1', TRIALS) is False
        await world.share(TRIALS, OTHER, 'manage')
        response = await world.api(OTHER, 'PATCH', f'/api/datasets/{TRIALS}', json={'assistant_closed': True})
        assert response.status_code == 200 and response.json()['assistant_closed'] is True
        # Every answer that describes a dataset carries it; the same user still reads the dataset.
        assert (await world.api(OTHER, 'GET', f'/api/datasets/{TRIALS}')).json()['assistant_closed'] is True
        assert {
            row['id']: row['assistant_closed'] for row in (await world.api(OTHER, 'GET', '/api/datasets')).json()
        } == {
            TRIALS: True,
        }
        assert (await world.api(POWER, 'GET', f'/api/datasets/{CLOSED}/schema')).status_code == 200

    world(scenario)


def test_new_datasets_start_closed_only_while_the_setting_is_on(world):
    async def scenario(world):
        made = await world.api(ADMIN, 'POST', '/api/datasets', json={'name': 'open by default'})
        assert made.status_code == 200 and made.json()['assistant_closed'] is False
        await world.api(ADMIN, 'PATCH', '/api/admin/assistant', json={'new_datasets_closed': True})
        made = await world.api(ADMIN, 'POST', '/api/datasets', json={'name': 'closed from the start'})
        assert made.json()['assistant_closed'] is True
        assert await world.database.fetchval('SELECT assistant_closed FROM datasets WHERE id = $1', TRIALS) is False

    world(scenario)


def test_a_mirror_keeps_its_own_switch(world):
    async def scenario(world):
        await world.database.execute(
            'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES ($1, 5, 0)',
            TRIALS,
        )
        renamed = await world.api(ADMIN, 'PATCH', f'/api/datasets/{TRIALS}', json={'name': 'renamed here'})
        assert renamed.status_code == 409  # the primary's settings stay the primary's
        closed = await world.api(ADMIN, 'PATCH', f'/api/datasets/{TRIALS}', json={'assistant_closed': True})
        assert closed.status_code == 200 and closed.json()['assistant_closed'] is True

    world(scenario)


def test_a_closed_dataset_is_refused_and_left_out_however_a_tool_comes_to_it(world, monkeypatch):
    async def scenario(world):
        await world.switch_on(power_user=True)
        for user_id in (ADMIN, POWER):
            listed = await world.tool(user_id, 'list_datasets')
            assert CLOSED not in [entry['id'] for entry in listed['result']['datasets']]
            assert refused(await world.tool(user_id, 'describe_dataset', dataset=CLOSED), 'refused') == CLOSED_WORDS
        # Not to someone who can't open it: the API's own refusal.
        assert refused(await world.tool(POWER, 'describe_dataset', dataset=HIDDEN), 'refused') == (
            'Access denied for this dataset'
        )
        await world.database.execute('UPDATE datasets SET assistant_closed = TRUE WHERE id = $1', HIDDEN)
        assert refused(await world.tool(POWER, 'describe_dataset', dataset=HIDDEN), 'refused') == (
            'Access denied for this dataset'
        )
        # Through a saved view of it (GET /views/{slug} finds the dataset through the view's row).
        await world.database.execute(
            "INSERT INTO views (dataset_id, user_id, name, slug, content, is_public) "
            "VALUES ($1, $2, 'wet plots', 'wet-plots', '{}', TRUE)",
            CLOSED,
            ADMIN,
        )
        identity = Identity(POWER, 'mcp')
        # No tool lists it (its answer's shape isn't declared sealed): declared here, to reach the access layer.
        monkeypatch.setitem(private.COVERED_ROUTES, ('GET', '/api/views/{slug}'), 'a saved view, for this test')
        view = await inner.request(
            identity,
            [('GET', '/api/views/{slug}')],
            'GET',
            '/api/views/{slug}',
            path_params={'slug': 'wet-plots'},
        )
        assert (view.status, view.body['detail']) == (403, CLOSED_WORDS)
        # Through the dataset's own routes, with the admin's shortcut too.
        for user_id in (ADMIN, POWER):
            schema = await inner.request(
                Identity(user_id, 'mcp'),
                [('GET', '/api/datasets/{id}/schema')],
                'GET',
                '/api/datasets/{id}/schema',
                path_params={'id': CLOSED},
            )
            assert (schema.status, schema.body['detail']) == (403, CLOSED_WORDS)
        # The same users, through the API: as before.
        assert (await world.api(POWER, 'GET', '/api/views/wet-plots')).status_code == 200
        assert (await world.api(ADMIN, 'GET', f'/api/datasets/{CLOSED}')).status_code == 200

    world(scenario)


# --- the tools ---


def test_the_tool_list_is_fixed_and_every_tool_reads(world):
    first, second = assistant.tools(), assistant.tools()
    names = ['list_datasets', 'describe_dataset', 'make_view', 'refine_view', 'field_values', 'field_summary']
    finding = [
        'find_place',
        'list_concepts',
        'concept_preview',
        'search_similar',
        'list_saved_views',
        'open_saved_view',
    ]
    assert [tool.name for tool in first] == [*names, 'get_samples', *finding] == [tool.name for tool in second]
    for tool in first:
        assert tool.kind == 'read' and tool.description and tool.input_schema['type'] == 'object'
        assert tool.level in (None, 'view', 'tag', 'edit', 'manage')
    assert assistant.tools()[1].input_schema['required'] == ['dataset']
    assert isinstance(assistant.instructions(), str) and 'list_datasets' in assistant.instructions()


def test_every_route_a_read_tool_lists_reads():
    """A GET, or one of the POSTs the access-levels guard counts as reads (access._READ_ONLY_DATASET_POSTS, or a search
    whose dataset is in its body: test_route_levels.BODY_READS); never an administrators' route; and one that
    identifies its caller, so both fences and the closed rule apply to it."""
    from backend.tests.test_route_levels import BODY_READS, _calls

    reads = access._READ_ONLY_DATASET_POSTS | BODY_READS
    for tool in assistant.tools():
        for method, template in tool.routes:
            handler = inner.route_handler(method, template)
            assert method == 'GET' or handler.__name__ in reads, (tool.name, method, template)
            assert not template.startswith('/api/admin'), (tool.name, template)
            route = next(
                route
                for path, route in inner._walk(main.app.routes)
                if path == template and method in (route.methods or ())
            )
            calls = _calls(route)
            source = inspect.getsource(inspect.unwrap(handler))
            identifies = any(
                call in (auth.get_current_user, auth.get_optional_user, access.require_dataset_access) for call in calls
            ) or any(f'{name}(' in source for name in ('get_current_user', 'get_optional_user'))
            assert identifies, f'{tool.name}: {method} {template} never identifies its caller'
            # Its answer's shape is one the sealing covers (private.COVERED_ROUTES, each with a test that plants values
            # and reads its answer: test_assistant_private_values), and its `{id}` is a dataset's, whose private
            # fields are the ones sealed: a dataset route, or the datasets' list, which has no field values. A route
            # that names no dataset in its path declares where its dataset comes from (private.DATASET_FROM: none,
            # its body, or the one it resolved), each tested below and in test_assistant_finding.
            assert (method, template) in private.COVERED_ROUTES, f'{tool.name}: {method} {template} declares no seal'
            assert (
                template == '/api/datasets'
                or template.split('/')[:4] == ['', 'api', 'datasets', '{id}']
                or (method, template) in private.DATASET_FROM
            ), template


def test_each_route_naming_no_dataset_says_where_its_dataset_comes_from():
    """private.DATASET_FROM: the regions name none and carry no dataset's values; the search names its dataset in its
    body (request_dataset reads it); a saved view by its slug keeps the dataset it checked on the request's state,
    which inner.request seals its answer for. Each is a covered route with no `{id}`."""
    assert set(private.DATASET_FROM.values()) <= {'none', 'body', 'resolved'}
    for method, template in private.DATASET_FROM:
        assert (method, template) in private.COVERED_ROUTES and '{id}' not in template, template
        inner.route_handler(method, template)
    assert private.request_dataset({}, {'dataset_id': 7}) == 7
    assert private.DATASET_FROM[('GET', '/api/views/{slug}')] == 'resolved'
    source = inspect.getsource(inspect.unwrap(inner.route_handler('GET', '/api/views/{slug}')))
    assert 'state=request.state' in source, 'the saved view route must keep the dataset it resolves'


# Routes that take a field's values as input outside the filter compiler, where the guard (assistant/guard.py) doesn't
# see them: an exact-match lookup and prefix autocompletes.
_VALUES_OUTSIDE_THE_COMPILER = {
    ('POST', '/api/datasets/{id}/samples/lookup'),
    ('GET', '/api/datasets/{id}/fields/{field_name}/autocomplete'),
    ('POST', '/api/datasets/{id}/fields/{field_name}/autocomplete'),
}


def test_no_route_that_takes_values_outside_the_compiler_is_covered():
    for method, template in sorted(_VALUES_OUTSIDE_THE_COMPILER & set(private.COVERED_ROUTES)):
        pytest.fail(
            f"{method} {template} takes a field's values outside the filter compiler: before it may be covered, guard "
            'that input as the compiler is guarded (only tokens for a private field; no prefix search of one)',
        )
    for method, template in _VALUES_OUTSIDE_THE_COMPILER:
        inner.route_handler(method, template)  # still a route of the application, so the check above still means it


# What tool modules may import: the tools' own helpers, and three constants of the filter language. Anything else that
# reads data (the database, a router's function) would bypass the sealing in inner.request.
_TOOL_IMPORTS = {
    'api': None,
    'catalog': None,
    'grain': None,
    'links': None,
    'private': None,
    'rule': None,
    'store': None,
    'working': None,  # a tool module itself, checked here: the finding tools stand on its views
    'field_registry': {'canonical_type'},
    'filter_query': {'TEXT'},
    'routers.filter_query': {'_KINDS', '_SYSTEM_FIELDS'},
}
_TOOL_MODULES = ('catalog', 'working', 'finding')
_STANDARD = {'datetime', 'dataclasses', 'typing', 'json', 're', 'math', 'collections', 'itertools'}
# Helpers of private.py a tool may use; its database readers are for inner.request only.
_PRIVATE_HELPERS = {'covered', 'is_sealed', 'sealed_in'}


def test_tool_code_reaches_data_only_through_the_api():
    import ast
    import pathlib

    folder = pathlib.Path(assistant.__file__).parent
    for module in _TOOL_MODULES:
        path = folder / f'{module}.py'
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split('.')[0] in _STANDARD, f'{module}.py imports {alias.name}'
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    assert (node.module or '').split('.')[0] in _STANDARD, f'{module}.py imports from {node.module}'
                    continue
                names = {alias.name for alias in node.names}
                if node.module is None:  # from . import a, b
                    for name in names:
                        assert name in _TOOL_IMPORTS and _TOOL_IMPORTS[name] is None, f'{module}.py imports {name}'
                    continue
                allowed = _TOOL_IMPORTS.get(node.module, ())
                assert allowed is None or names <= set(allowed), f'{module}.py imports {names} from {node.module}'
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'private':
                assert node.attr in _PRIVATE_HELPERS, f'{module}.py uses private.{node.attr}'


def test_no_administrators_route_answers_a_tool_whoever_the_user_is(world):
    async def scenario(world):
        await world.switch_on()
        for path, name in (('/api/admin/status', 'admin_status'), ('/api/admin/assistant', 'get_assistant_settings')):
            handler = next(r.endpoint for p, r in inner._walk(main.app.routes) if p == path and 'GET' in r.methods)
            assert handler.__name__ == name
            answer = await _raw(Identity(ADMIN, 'mcp', frozenset({('GET', handler)})), 'GET', path)
            assert answer[0] == 403 and answer[1]['code'] == assistant_api.ROUTE_REFUSED_CODE, (path, answer)
        # A job of no dataset reads as one that doesn't exist (it can name datasets closed to the assistant).
        from fastapi import HTTPException

        from backend import job_queue
        from backend.routers import jobs

        job = await job_queue.enqueue_job(world.database.pool, 'backup', created_by=ADMIN)
        with pytest.raises(HTTPException) as raised:
            await jobs._authorized_job(job['id'], {'id': ADMIN, 'role': 'admin', 'assistant_door': 'mcp'})
        assert raised.value.status_code == 404
        assert (await jobs._authorized_job(job['id'], {'id': ADMIN, 'role': 'admin'}))['id'] == job['id']

    world(scenario)


def test_a_follower_instances_account_may_not_use_the_assistant(world):
    async def scenario(world):
        await world.switch_on(power_user=True)
        assert await assistant.door_refusal(user_id=OTHER, door='mcp') is None
        await world.database.execute(
            "INSERT INTO followers (name, user_id) VALUES ('field-office', $1)",
            OTHER,
        )
        assert await assistant.door_refusal(user_id=OTHER, door='mcp') == rule.NOT_LET_IN

    world(scenario)


def test_the_schema_says_what_is_stale_only_when_asked(world):
    async def scenario(world):
        await ingest([{'score': 1}], dataset_id=TRIALS)
        plain = (await world.api(ADMIN, 'GET', f'/api/datasets/{TRIALS}/schema')).json()
        asked = (await world.api(ADMIN, 'GET', f'/api/datasets/{TRIALS}/schema', params={'stale': 'true'})).json()
        assert not any('stats_stale' in field for field in plain)
        assert {field['name']: field.get('stats_stale') for field in asked}['score'] is True
        assert [{k: v for k, v in f.items() if k != 'stats_stale'} for f in asked] == plain

    world(scenario)


def test_unknown_tools_and_arguments_that_do_not_fit(world):
    async def scenario(world):
        await world.switch_on()
        assert refused(await world.tool(ADMIN, 'delete_everything'), 'unknown_tool') == (
            "There is no tool named 'delete_everything'"
        )
        assert 'limit' in refused(await world.tool(ADMIN, 'list_datasets', limit=51), 'invalid')
        assert 'limit' in refused(await world.tool(ADMIN, 'list_datasets', limit='ten'), 'invalid')
        assert 'dataset' in refused(await world.tool(ADMIN, 'describe_dataset'), 'invalid')
        assert 'colour' in refused(await world.tool(ADMIN, 'describe_dataset', dataset=TRIALS, colour=1), 'invalid')
        assert refused(await world.tool(ADMIN, 'describe_dataset', dataset=987654), 'not_found') == (
            'Dataset not found: id 987654'
        )

    world(scenario)


def test_list_datasets_by_name_a_page_at_a_time(world):
    async def scenario(world):
        await world.switch_on(power_user=True)
        await ingest([{'score': n} for n in range(3)], dataset_id=TRIALS)
        for index in range(4):
            dataset_id = await world.database.fetchval(
                'INSERT INTO datasets (name) VALUES ($1) RETURNING id',
                f'Batch {index}',
            )
            await world.share(dataset_id, POWER, 'edit' if index else 'tag')
        await world.database.execute("UPDATE datasets SET deleting = TRUE WHERE name = 'Batch 3'")
        everything = (await world.tool(POWER, 'list_datasets'))['result']
        names = [entry['name'] for entry in everything['datasets']]
        assert names == ['Batch 0', 'Batch 1', 'Batch 2', 'field-trials']
        assert everything['total'] == 4 and everything['next_offset'] is None and everything['offset'] == 0
        trials = everything['datasets'][-1]
        assert trials == {'id': TRIALS, 'name': 'field-trials', 'description': None, 'level': 'view'}  # no counts
        assert everything['datasets'][0]['level'] == 'tag'
        page = (await world.tool(POWER, 'list_datasets', search='BATCH', limit=2))['result']
        assert [entry['name'] for entry in page['datasets']] == ['Batch 0', 'Batch 1']
        assert (page['total'], page['next_offset']) == (3, 2)
        rest = (await world.tool(POWER, 'list_datasets', search='batch', limit=2, offset=2))['result']
        assert [entry['name'] for entry in rest['datasets']] == ['Batch 2'] and rest['next_offset'] is None
        admin = (await world.tool(ADMIN, 'list_datasets'))['result']
        assert 'not shared' in [entry['name'] for entry in admin['datasets']]
        assert {entry['level'] for entry in admin['datasets']} == {'manage'}

    world(scenario)


def test_describe_dataset_from_the_schema_and_the_statistics(world):
    async def scenario(world):
        await world.switch_on(power_user=True)
        await ingest(
            [
                {
                    'brightness': 10.5 + n,
                    'serial_number': f'SN-{n % 2}',
                    'plot': f'P{n % 2}',
                    'wet': n == 0,
                    'taken': f'2026-01-0{n + 1}',
                }
                for n in range(3)
            ],
            dataset_id=TRIALS,
        )
        await world.database.execute(
            "INSERT INTO samples (dataset_id, filepath, media_type) VALUES ($1, 'an-object', 'object')",
            TRIALS,
        )
        await run_refresh(world.database, TRIALS)
        await world.api(
            ADMIN,
            'PATCH',
            f'/api/datasets/{TRIALS}/fields/serial_number/description',
            json={'description': 'The unit that took it', 'values_private': True},
        )
        await world.database.execute(
            "INSERT INTO views (dataset_id, user_id, name, slug, content, is_public) VALUES "
            "($1, $2, 'mine', 'mine-1', '{}', FALSE), ($1, $3, 'theirs', 'theirs-1', '{}', FALSE)",
            TRIALS,
            POWER,
            ADMIN,
        )
        answer = await world.tool(POWER, 'describe_dataset', dataset=TRIALS)
        assert answer['ok'], answer
        result = answer['result']
        assert result['dataset'] == {
            'id': TRIALS,
            'name': 'field-trials',
            'description': None,
            'samples': 3,
            'objects': 1,
            'level': 'view',
        }
        fields = {field['name']: field for field in result['fields']}
        assert result['fields_total'] == len(result['fields']) and result['next_offset'] is None
        brightness = fields['brightness']
        assert brightness['type'] == 'number' and brightness['range'] == {'min': 10.5, 'max': 12.5}
        assert brightness['description_source'] == 'default' and brightness['samples_with_value'] == 3
        assert brightness['values'] is None and brightness['complete'] is True
        # The values the statistics keep, in their order, without counts; a boolean's aren't in the schema's answer.
        assert fields['plot']['values'] == {'distinct': 2, 'examples': ['P0', 'P1']} and fields['plot']['complete']
        wet = fields['wet']
        assert wet['type'] == 'boolean' and wet['values'] is None and wet['complete'] is True
        serial = fields['serial_number']
        assert serial['values_private'] is True and serial['range'] is None
        assert serial['values'] == {'distinct': 2, 'examples': []}
        assert 'SN-0' not in json.dumps(result) and 'SN-1' not in json.dumps(result)
        assert fields['taken']['type'] == 'datetime'
        assert fields['tags']['type'] == 'list' and fields['tags']['values'] == {'distinct': 0, 'examples': []}
        assert fields['media_type']['values'] == {'distinct': 2, 'examples': ['image', 'object']}
        assert fields['id']['samples_with_value'] == 4  # every row has an id, the object's too
        assert result['saved_views'] == 1  # the user's own, not another's private one
        assert result['concepts'] == 0 and result['embedding_fields'] == [] and result['samples_with_location'] == 0
        assert result['undescribed_fields'] == sum(
            1 for field in result['fields'] if field['description_source'] is None
        )
        searched = (await world.tool(POWER, 'describe_dataset', dataset=TRIALS, search='TOOK', limit=1))['result']
        assert [field['name'] for field in searched['fields']] == ['serial_number'] and searched['fields_total'] == 1
        paged = (await world.tool(POWER, 'describe_dataset', dataset=TRIALS, limit=2, offset=1))['result']
        assert [field['name'] for field in paged['fields']] == [field['name'] for field in result['fields']][1:3]
        assert paged['next_offset'] == 3

    world(scenario)


def test_a_field_whose_statistics_are_pending_is_not_complete(world):
    async def scenario(world):
        await world.switch_on()
        await ingest([{'score': 1}, {'score': 2}], dataset_id=TRIALS)
        await world.database.execute('DELETE FROM dataset_field_stats WHERE dataset_id = $1', TRIALS)
        result = (await world.tool(ADMIN, 'describe_dataset', dataset=TRIALS, search='score'))['result']
        assert [(field['name'], field['complete']) for field in result['fields']] == [('score', False)]

    world(scenario)


def test_a_field_added_after_the_last_count_is_not_complete_until_it_is_counted(world):
    """Ingest gives a new field a statistics row at once (its values merged, no count yet): the change waiting to be
    counted (dataset_stats_pending) says its statistics are not whole."""

    async def scenario(world):
        await world.switch_on()
        await ingest([{'score': n, 'crop': 'wheat'} for n in range(3)], dataset_id=TRIALS)
        await run_refresh(world.database, TRIALS)
        await ingest([{'late': f'v{n}'} for n in range(3)], dataset_id=TRIALS, prefix='late')

        async def by_name():
            result = (await world.tool(ADMIN, 'describe_dataset', dataset=TRIALS, limit=100))['result']
            return {field['name']: field for field in result['fields']}

        fields = await by_name()
        late = fields['late']
        assert late['complete'] is False and late['samples_with_value'] == 0
        # The values ingest merged in are examples; how many there are waits for the count.
        assert late['values']['distinct'] is None and sorted(late['values']['examples']) == ['v0', 'v1', 'v2']
        assert 'many' not in late['values']
        assert fields['score']['complete'] is True  # the new samples changed no score
        await run_refresh(world.database, TRIALS)
        fields = await by_name()
        assert fields['late']['complete'] is True and fields['late']['samples_with_value'] == 3
        assert fields['late']['values'] == {'distinct': 3, 'examples': ['v0', 'v1', 'v2']}

    world(scenario)


def test_describe_dataset_makes_the_same_requests_for_five_fields_as_for_a_hundred(world, monkeypatch):
    async def scenario(world):
        await world.switch_on()
        await ingest([{f'field_{k:03}': n * k for k in range(110)} for n in range(3)], dataset_id=TRIALS)
        await run_refresh(world.database, TRIALS)
        made: list[str] = []
        real = inner.request

        async def counted(identity, listed, method, template, **kwargs):
            made.append(f'{method} {template}')
            return await real(identity, listed, method, template, **kwargs)

        monkeypatch.setattr(inner, 'request', counted)
        five = await world.tool(ADMIN, 'describe_dataset', dataset=TRIALS, limit=5)
        assert len(five['result']['fields']) == 5
        requests_for_five, made[:] = list(made), []
        hundred = await world.tool(ADMIN, 'describe_dataset', dataset=TRIALS, limit=100)
        assert len(hundred['result']['fields']) == 100
        assert made == requests_for_five and len(made) == 6, made
        assert not any('histogram' in each or 'aggregate' in each for each in made)

    world(scenario)


# --- the fences ---


def test_a_route_the_tool_does_not_list_is_refused_before_it_is_sent(world, monkeypatch):
    async def scenario(world):
        await world.switch_on()
        sent = []
        inner.route_handler('GET', '/api/datasets')  # the route table, read from the real application
        real = main.app

        async def watched(scope, receive, send):
            sent.append(scope['path'])
            await real(scope, receive, send)

        monkeypatch.setattr(main, 'app', watched)
        try:
            with pytest.raises(inner.RouteNotListed):
                await inner.request(
                    Identity(ADMIN, 'mcp'),
                    [('GET', '/api/datasets')],
                    'DELETE',
                    '/api/datasets/{id}',
                    path_params={'id': TRIALS},
                )
            with pytest.raises(inner.RouteNotListed):
                await inner.request(Identity(ADMIN, 'mcp'), [('GET', '/api/no/such')], 'GET', '/api/no/such')
        finally:
            monkeypatch.setattr(main, 'app', real)
        assert sent == []
        assert await world.database.fetchval('SELECT NOT deleting FROM datasets WHERE id = $1', TRIALS)

    world(scenario)


def test_a_request_served_by_a_route_the_tool_does_not_list_is_refused(world):
    async def scenario(world):
        await world.switch_on()
        listed = frozenset({('GET', inner.route_handler('GET', '/api/datasets'))})
        answer = await _raw(Identity(ADMIN, 'mcp', listed), 'GET', f'/api/datasets/{TRIALS}')
        assert answer[0] == 403 and answer[1]['code'] == assistant_api.ROUTE_REFUSED_CODE
        assert (await _raw(Identity(ADMIN, 'mcp', listed), 'GET', '/api/datasets'))[0] == 200
        # Only a tool's own fault reaches it: a server fault, not an answer to the model.
        api = assistant_api.Api(Identity(ADMIN, 'mcp'), [('GET', '/api/datasets')])

        async def refused(*args, **kwargs):
            return inner.Answer(403, {'detail': 'no', 'code': assistant_api.ROUTE_REFUSED_CODE}, {})

        real = inner.request
        inner.request = refused
        try:
            with pytest.raises(assistant_api.ToolFailed):
                await api.get('/api/datasets')
        finally:
            inner.request = real

    world(scenario)


async def _raw(identity: Identity, method: str, path: str) -> tuple[int, Any]:
    messages: list[dict[str, Any]] = []
    scope: dict[str, Any] = {
        'type': 'http',
        'method': method,
        'path': path,
        'raw_path': path.encode(),
        'root_path': '',
        'query_string': b'',
        'headers': [],
        'client': None,
        SCOPE_KEY: identity,
    }

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        messages.append(message)

    await main.app(scope, receive, send)
    body = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
    return messages[0]['status'], json.loads(body)


def test_nothing_from_outside_is_an_identity(world):
    async def scenario(world):
        await world.switch_on()
        # A header of the same name, and a scope key holding anything but an Identity, authenticate nobody.
        response = await world.client.get('/api/datasets', headers={SCOPE_KEY: str(ADMIN), 'x-fridge-assistant': '1'})
        assert response.status_code == 200 and response.json() == []
        assert (await _raw_with(SCOPE_KEY, {'user_id': ADMIN, 'door': 'mcp'}))[0] == 401

    world(scenario)


async def _raw_with(key: str, value: Any) -> tuple[int, Any]:
    messages: list[dict[str, Any]] = []
    scope: dict[str, Any] = {'type': 'http', 'method': 'GET', 'path': '/api/datasets/1', 'raw_path': b'/api/datasets/1'}
    scope.update({'root_path': '', 'query_string': b'', 'headers': [], 'client': None, key: value})

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        messages.append(message)

    await main.app(scope, receive, send)
    return messages[0]['status'], None


def test_no_pooled_connection_is_held_across_a_tool_request(world, monkeypatch):
    async def scenario(world):
        await world.switch_on()
        await ingest([{'score': 1, 'label': 'a'}], dataset_id=TRIALS)
        pool = world.database.pool
        held = []
        inner.route_handler('GET', '/api/datasets')  # the route table, read from the real application
        real = main.app

        async def watched(scope, receive, send):
            held.append(pool.get_size() - pool.get_idle_size())
            await real(scope, receive, send)

        monkeypatch.setattr(main, 'app', watched)
        try:
            assert (await world.tool(ADMIN, 'list_datasets'))['ok']
            assert (await world.tool(ADMIN, 'describe_dataset', dataset=TRIALS))['ok']
        finally:
            monkeypatch.setattr(main, 'app', real)
        assert held and set(held) == {0}, held

    world(scenario)


def test_a_busy_answer_is_busy_and_retryable(monkeypatch):
    async def busy(*args, **kwargs):
        return inner.Answer(503, {'detail': 'Too many simultaneous guest queries. Please try again shortly.'}, {})

    monkeypatch.setattr(inner, 'request', busy)
    api = assistant_api.Api(Identity(GUEST, 'mcp'), [('GET', '/api/datasets')])
    import asyncio

    with pytest.raises(assistant_api.ToolError) as raised:
        asyncio.run(api.get('/api/datasets'))
    assert raised.value.answer() == {
        'code': 'busy',
        'message': 'Too many simultaneous guest queries. Please try again shortly.',
        'retryable': True,
    }


# What a tool must never change. Left out on purpose: what a read does on the side for anyone, as opening the dataset's
# sidebar does (dataset_field_stats, whose caches field-stats fills, and jobs, where it can queue one full statistics
# recount, never a second while one is queued or running), and the assistant's own rows.
_TABLES = (
    'datasets',
    'samples',
    'dataset_fields',
    'views',
    'dataset_access',
    'dataset_group_access',
    'embeddings',
    'embedding_concepts',
    'dataset_versions',
    'users',
    'groups',
    'group_members',
    'assistant_settings',
    'assistant_groups',
    'assistant_users',
)


async def _snapshot(database) -> dict[str, str]:
    found = {}
    for table in _TABLES:
        rows = await database.fetch(f'SELECT to_jsonb(t)::text AS row FROM {table} t ORDER BY 1')
        found[table] = hashlib.sha256('\n'.join(row['row'] for row in rows).encode()).hexdigest()
    return found


def test_calling_the_tools_writes_nothing(world):
    async def scenario(world):
        await world.switch_on(power_user=True)
        await ingest([{'score': n, 'label': f'l{n}'} for n in range(4)], dataset_id=TRIALS)
        await run_refresh(world.database, TRIALS)
        before = await _snapshot(world.database)
        for user_id in (ADMIN, POWER, GUEST):
            for arguments in ({}, {'search': 'x'}, {'limit': 1, 'offset': 3}, {'limit': 0}):
                await world.tool(user_id, 'list_datasets', **arguments)
            for dataset_id in (TRIALS, CLOSED, HIDDEN, 987654):
                await world.tool(user_id, 'describe_dataset', dataset=dataset_id)
                await world.tool(user_id, 'describe_dataset', dataset=dataset_id, search='score', limit=1)
        assert await _snapshot(world.database) == before

    world(scenario)


def test_a_guest_in_public_mode_takes_guest_slots_and_a_full_queue_is_busy(world, monkeypatch):
    import asyncio
    import dataclasses

    from backend import public_settings, request_limits

    async def scenario(world):
        await world.switch_on(guest=True)
        await world.database.execute('UPDATE datasets SET is_global = TRUE WHERE id = $1', TRIALS)
        await ingest([{'score': 1}], dataset_id=TRIALS)
        monkeypatch.setenv('DEMO_MODE', 'true')
        monkeypatch.setattr(
            public_settings,
            'settings',
            dataclasses.replace(public_settings.settings, guest_queue_seconds=0.2),
        )
        # A whole tool call as a guest, every heavy request through the guest's slots: nothing waits on a disconnect.
        answer = await asyncio.wait_for(world.tool(GUEST, 'describe_dataset', dataset=TRIALS), 30)
        assert answer['ok'] and answer['result']['dataset']['samples'] == 1
        assert not request_limits._guest_slots
        # The guest's slots all taken: the field statistics wait their turn, then the queue's 503 is busy, retryable.
        monkeypatch.setitem(
            request_limits._guest_slots,
            GUEST,
            request_limits.GuestSlots(asyncio.Semaphore(0), requests=1),
        )
        answer = await world.tool(GUEST, 'describe_dataset', dataset=TRIALS)
        assert answer['ok'] is False and answer['error']['code'] == 'busy', answer
        assert answer['error']['retryable'] is True
        assert answer['error']['message'] == 'Too many simultaneous guest queries. Please try again shortly.'

    world(scenario)


def test_a_drawing_fields_labels_come_from_its_statistics(world):
    async def scenario(world):
        await world.switch_on()
        box = [0.1, 0.1, 0.2, 0.2]
        await ingest(
            [
                {'detections': [{'label': 'cat', 'bounding_box': box}, {'label': 'dog', 'bounding_box': box}]},
                {'detections': [{'label': 'cat', 'bounding_box': box}]},
                {'detections': []},
            ],
            dataset_id=TRIALS,
        )
        await run_refresh(world.database, TRIALS)
        result = (await world.tool(ADMIN, 'describe_dataset', dataset=TRIALS, search='detections'))['result']
        fields = {field['name']: field for field in result['fields']}
        assert fields['detections']['values'] is None  # drawings: no values to count
        labels = fields['detections.label']
        assert labels['type'] == 'text' and labels['complete'] is True, labels
        assert labels['values'] == {'distinct': 2, 'examples': ['cat', 'dog']}
        # Kept back with its field: the label list holds the field's values.
        await world.api(
            ADMIN,
            'PATCH',
            f'/api/datasets/{TRIALS}/fields/detections/description',
            json={'values_private': True},
        )
        result = (await world.tool(ADMIN, 'describe_dataset', dataset=TRIALS, search='detections'))['result']
        labels = {field['name']: field for field in result['fields']}['detections.label']
        assert labels['values_private'] is True and labels['values'] == {'distinct': 2, 'examples': []}
        assert '"cat"' not in json.dumps(result) and '"dog"' not in json.dumps(result)

    world(scenario)


def test_auth_me_reads_nothing_of_the_assistant_while_it_is_off_and_never_fails_for_it(world, monkeypatch):
    async def scenario(world):
        handle = database_module.db
        asked: list[str] = []
        for method in ('fetch', 'fetchrow', 'fetchval'):
            real = getattr(handle, method)

            def counting(query, *args, real=real):
                if 'assistant' in query:
                    asked.append(query)
                return real(query, *args)

            monkeypatch.setattr(handle, method, counting)
        rule.forget_switch()
        for _ in range(3):
            me = await world.api(POWER, 'GET', '/api/auth/me')
            assert me.status_code == 200 and me.json()['assistant'] == {'mcp': False}
        assert len(asked) == 1, asked  # the switch, once; then nothing while it is off
        await world.switch_on(power_user=True)
        assert (await world.api(POWER, 'GET', '/api/auth/me')).json()['assistant'] == {'mcp': True}

        async def broken(*args, **kwargs):
            raise RuntimeError('the rule could not be read')

        monkeypatch.setattr(rule, 'verdict', broken)
        me = await world.api(POWER, 'GET', '/api/auth/me')
        assert me.status_code == 200 and me.json()['assistant'] == {'mcp': False}
        assert me.json()['username'] == f'person-{POWER}'

    world(scenario)


def test_a_change_made_by_another_process_stops_the_next_call(world):
    """The doors and call_tool read the switch and the rule from the database on every call: only /auth/me's hint
    reads a kept switch. Each change here is written to the table directly, as another server process would."""

    async def scenario(world):
        await world.switch_on(power_user=True)
        # /auth/me has read the switch on, and keeps that reading for a few seconds.
        assert (await world.api(POWER, 'GET', '/api/auth/me')).json()['assistant'] == {'mcp': True}
        assert await assistant.door_refusal(user_id=POWER, door='mcp') is None
        assert (await world.tool(POWER, 'list_datasets'))['ok']
        await world.database.execute('UPDATE assistant_settings SET mcp_enabled = FALSE')
        assert await assistant.door_refusal(user_id=POWER, door='mcp') == rule.MCP_OFF
        assert refused(await world.tool(POWER, 'list_datasets'), 'not_allowed') == rule.MCP_OFF
        await world.database.execute('UPDATE assistant_settings SET mcp_enabled = TRUE')
        assert await assistant.door_refusal(user_id=POWER, door='mcp') is None
        assert (await world.tool(POWER, 'list_datasets'))['ok']
        # A person taken out, then their role.
        await world.database.execute('INSERT INTO assistant_users (user_id, allowed) VALUES ($1, FALSE)', POWER)
        assert await assistant.door_refusal(user_id=POWER, door='mcp') == rule.NOT_LET_IN
        assert refused(await world.tool(POWER, 'list_datasets'), 'not_allowed') == rule.NOT_LET_IN
        await world.database.execute('DELETE FROM assistant_users WHERE user_id = $1', POWER)
        assert (await world.tool(POWER, 'list_datasets'))['ok']
        await world.database.execute('UPDATE assistant_settings SET power_user_allowed = FALSE')
        assert await assistant.door_refusal(user_id=POWER, door='mcp') == rule.NOT_LET_IN
        assert refused(await world.tool(POWER, 'list_datasets'), 'not_allowed') == rule.NOT_LET_IN

    world(scenario)


def test_a_jobs_level_lookup_keeps_the_closed_refusal(world):
    """jobs._level_in tries manage, edit and tag, passing over a level refusal: the closed one is no such refusal.
    (Below tag it answers view without a check: its callers check view first, and that check refuses.)"""
    from fastapi import HTTPException

    from backend.routers import jobs

    async def scenario(world):
        await world.share(CLOSED, OTHER, 'tag')
        await world.share(CLOSED, POWER, 'manage')
        for user_id, role in ((ADMIN, 'admin'), (POWER, 'power_user'), (OTHER, 'power_user')):
            user = {'id': user_id, 'role': role, 'assistant_door': 'mcp'}
            with pytest.raises(HTTPException) as raised:
                await jobs._level_in(CLOSED, user, None)
            assert isinstance(raised.value, access.AssistantClosed), (user_id, raised.value.detail)
            # The same person outside the assistant: their level, as before.
            plain = {'id': user_id, 'role': role}
            assert await jobs._level_in(CLOSED, plain, None) == access.level_number(
                {ADMIN: 'manage', POWER: 'manage', OTHER: 'tag'}[user_id],
            )

    world(scenario)


def test_a_field_with_more_values_than_the_server_keeps_says_many(world):
    async def scenario(world):
        await world.switch_on()
        await ingest(
            [{'serial': f'SN-{n:04}', 'kind': f'k{n % 3}', 'secret_serial': f'S-{n}'} for n in range(260)],
            dataset_id=TRIALS,
        )
        await world.api(
            ADMIN,
            'PATCH',
            f'/api/datasets/{TRIALS}/fields/secret_serial/description',
            json={'values_private': True},
        )
        await run_refresh(world.database, TRIALS)
        result = (await world.tool(ADMIN, 'describe_dataset', dataset=TRIALS, limit=100))['result']
        fields = {field['name']: field for field in result['fields']}
        serial = fields['serial']
        assert serial['complete'] is True and serial['samples_with_value'] == 260
        assert serial['values']['many'] is True and serial['values']['distinct'] is None
        assert len(serial['values']['examples']) <= 5
        assert fields['kind']['values'] == {'distinct': 3, 'examples': ['k0', 'k1', 'k2']}
        hidden = fields['secret_serial']
        assert hidden['values'] == {'distinct': None, 'examples': [], 'many': True}
        assert '"S-1"' not in json.dumps(result)

    world(scenario)


def test_describe_dataset_finds_fields_by_any_word_of_its_search(world):
    async def scenario(w: World):
        await w.switch_on(power_user=True)
        await ingest([{'darkness': n, 'shine': n, 'gain_db': n, 'crop': 'x'} for n in range(3)], TRIALS, prefix='words')
        await run_refresh(w.database, TRIALS)
        described = await w.api(
            ADMIN,
            'PATCH',
            f'/api/datasets/{TRIALS}/fields/shine/description',
            json={'description': 'How bright the image is'},
        )
        assert described.status_code == 200, described.text
        answer = await w.tool(POWER, 'describe_dataset', dataset=TRIALS, search='DARK bright  gain')
        names = {field['name'] for field in answer['result']['fields']}
        assert names == {'darkness', 'shine', 'gain_db'}, names
        assert answer['result']['fields_total'] == 3
        one = await w.tool(POWER, 'describe_dataset', dataset=TRIALS, search='dark')
        assert {field['name'] for field in one['result']['fields']} == {'darkness'}

    world(scenario)
