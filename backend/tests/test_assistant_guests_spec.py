"""Guests and the MCP door: a user whose role is guest may never use the MCP server, whatever the assistant's settings
say, while the settings keep their shape for the chat in the app; and `GET /api/auth/me?fresh=1`, which reads the MCP
switch from the database for that request.

Written from the contract (m1-contract-assistant-guests-token-link, sections 1, 3.3 and 4), not from the code, and
tested where it is met: `backend.assistant.door_refusal` and `assistant.call_tool` in the test process,
`GET /api/auth/me`, the MCP server at /mcp with an MCP token, and the settings' HTTP endpoints, signed in with real API
tokens, with the users, groups and helpers of the access-rule, assistant-core and MCP-door spec tests.

Where the contract is silent or unclear these tests read it this way:
- "Whichever listed group" is a group the guest is in, ticked under the groups that let their members in; "whatever
  the guest's own answer is" is no answer, `allowed: true` and `allowed: false`. Each way is checked alone and all
  three at once, and beside it a power user let in the same way, so the setting is shown to work.
- A guest is refused at /mcp, for any request (discovery, the tool list, a call), with 403 and the sentence, as the
  MCP-door contract's section 1.4 reads every refusal of `door_refusal`. A guest can't make an MCP token through the
  API (section 1.5), so the guest's token in the first test is written into the tokens table as the API writes one
  (a token made before the account became a guest, which the demotion test makes through the API).
- The demotion is an administrator's `PATCH /api/users/{id}` with `{"role": "guest"}`, which revokes no token (only
  deactivating does), so the token made as a power user is still listed and active; "made a power user again" is the
  same request back to `power_user`.
- "The order of refusals is unchanged": for a guest let in every way at once, the door switched off gives the door's
  sentence, and an inactive guest or one owing a password change gives the words the API refuses that account with (as
  the core spec reads them), not the guest sentence.
- Section 1.4 says `GET` and `PATCH /api/admin/assistant` read and write `roles.guest`, a guest's own answer and groups
  with guests in them. `PATCH` takes no `users` or `groups` key today (the core contract's settings write them through
  `PUT`/`DELETE /api/admin/assistant/users/{id}` and `.../groups/{id}`), so these tests write `roles.guest` with
  `PATCH`, a guest's own answer and a group with a guest in it with those routes, and read all three with `GET`.
- "A guest makes none" is a 403 from `POST /api/tokens` for each kind the API takes (`full`, `worker`, `mcp`, and the
  kind left out), with no token row made, while the settings let the guest in every way.
- Section 4 is checked lightly, as the repository checks docs elsewhere: docs/ASSISTANT.md's "Who may use it" no longer
  gives guests' having no tokens as what keeps them out, says in a sentence that a guest never (or can't, or may not)
  uses it, and names the chat; CHANGELOG.md's Unreleased section mentions guests.
- Section 3.3's test surface: the process is made to remember the switch by answering a plain `GET /api/auth/me`
  (after the settings API, which makes it read the switch again), and the switch is changed behind its back by
  updating `mcp_enabled` on the `assistant_settings` row in the database directly. The plain request answered then is
  checked only for answering 200 with the same keys; "changes nothing else" is every key but `assistant` equal to the
  plain answer's at the same moment.
- The chat door is not tested: it is off in this milestone, so its refusal comes first for everyone.
"""

import os
import re
import secrets
from typing import Any

import pytest

from backend import assistant
from backend.crypto import hash_token
from backend.tests import test_access_rule_spec, test_field_registry_postgres
from backend.tests.changelog_helpers import release_heading
from backend.tests.test_access_rule_spec import ADMIN, GUEST, OTHER_GUEST, OTHER_POWER, POWER, World
from backend.tests.test_assistant_core_spec import (
    MCP_OFF,
    NOT_LET_IN,
    SETTINGS,
    SHIPPED,
    api_refusal_of,
    call,
    error,
    open_door,
    settings,
)
from backend.tests.test_mcp_door_spec import FARM, Door, farm, make_token, refusal
from backend.tests.test_upgrade_paths import ROOT

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

# One request of each kind the MCP-door contract checks the refusal at.
ASKS = [
    ('server/discover', {}),
    ('tools/list', {}),
    ('tools/call', {'name': 'list_datasets', 'arguments': {}}),
]


async def me(w: World, user_id: int) -> dict[str, Any]:
    response = await w.call(user_id, 'GET', '/api/auth/me')
    assert response.status_code == 200, response.text
    return response.json()['assistant']


async def refused(w: World, user_id: int, step: str, words: str = NOT_LET_IN) -> None:
    """The user may not use the MCP door, with these words, in the core and as auth/me says."""
    assert await assistant.door_refusal(user_id=user_id, door='mcp') == words, (step, user_id)
    failure = await error(user_id, 'list_datasets')
    assert failure == {'code': 'not_allowed', 'message': words, 'retryable': False}, (step, user_id, failure)
    assert await me(w, user_id) == {'mcp': False}, (step, user_id)


async def let_in(w: World, user_id: int, step: str) -> None:
    assert await assistant.door_refusal(user_id=user_id, door='mcp') is None, (step, user_id)
    assert (await call(user_id, 'list_datasets'))['ok'], (step, user_id)
    assert await me(w, user_id) == {'mcp': True}, (step, user_id)


async def put(w: World, path: str, body: Any = None) -> dict[str, Any]:
    response = await w.call(ADMIN, 'PUT', f'{SETTINGS}{path}', **({} if body is None else {'json': body}))
    assert response.status_code == 200, (path, response.text)
    return response.json()


async def delete(w: World, path: str) -> dict[str, Any]:
    response = await w.call(ADMIN, 'DELETE', f'{SETTINGS}{path}')
    assert response.status_code == 200, (path, response.text)
    return response.json()


async def mcp_token_row(w: World, user_id: int) -> str:
    """An MCP token for the user, written as the tokens table holds one."""
    raw = secrets.token_urlsafe(32)
    await w.database.execute(
        "INSERT INTO api_tokens (user_id, token, name, scope) VALUES ($1, $2, 'made before', 'mcp')",
        user_id,
        hash_token(raw),
    )
    return raw


async def every_way(w: World, team: int) -> None:
    """roles.guest ticked, a group the guest is in ticked, and the guest's own answer `allowed: true`."""
    await settings(w, {'roles': {'guest': True}})
    await put(w, f'/groups/{team}')
    await put(w, f'/users/{GUEST}', {'allowed': True})
    state = await settings(w)
    assert state['roles']['guest'] is True and team in [group['id'] for group in state['groups']], state
    assert {'id': GUEST, 'username': f'user-{GUEST}', 'allowed': True} in state['users'], state


# --- 1.1 A guest may never use the mcp door ---


def test_a_guest_is_refused_the_mcp_door_with_the_sentence_under_every_way_of_being_let_in(world):
    async def scenario(w):
        team = await w.group('crew', GUEST, POWER)
        await settings(w, {'mcp_enabled': True})
        token = await mcp_token_row(w, GUEST)
        door = Door(w)

        async def guest_refused(step: str) -> None:
            await refused(w, GUEST, step)
            for method, params in ASKS:
                response, _ = await door.ask(method, params, token)
                assert refusal(response, 403) == NOT_LET_IN, (step, method)

        await guest_refused('as shipped')
        # roles.guest ticked: every guest is still refused.
        await settings(w, {'roles': {'guest': True}})
        assert (await settings(w))['roles'] == {'admin': True, 'power_user': False, 'guest': True}
        await guest_refused('roles.guest ticked')
        await refused(w, OTHER_GUEST, 'roles.guest ticked')
        await settings(w, {'roles': {'guest': False}})
        # A listed group the guest is in: it lets its power user in, never its guest.
        await put(w, f'/groups/{team}')
        await let_in(w, POWER, 'a power user in the listed group')
        await guest_refused('in a listed group')
        await delete(w, f'/groups/{team}')
        await refused(w, POWER, 'the group taken out')
        # The guest's own answer, allowed: true; the same answer lets a power user in.
        assert {'id': GUEST, 'username': f'user-{GUEST}', 'allowed': True} in (
            await put(w, f'/users/{GUEST}', {'allowed': True})
        )['users']
        await put(w, f'/users/{OTHER_POWER}', {'allowed': True})
        await let_in(w, OTHER_POWER, 'a power user with their own answer allowed: true')
        await guest_refused('their own answer allowed: true')
        # All three at once.
        await every_way(w, team)
        await let_in(w, POWER, 'all three at once: the power user in the group')
        await guest_refused('roles.guest, the group and their own answer at once')
        await refused(w, OTHER_GUEST, 'roles.guest ticked, in no group, no answer of their own')
        # Their own answer the other way: the same sentence.
        await put(w, f'/users/{GUEST}', {'allowed': False})
        await guest_refused('their own answer allowed: false')
        await delete(w, f'/users/{GUEST}')
        await guest_refused('roles.guest and the group, no answer of their own')

    world(scenario)


def test_auth_me_says_false_for_a_let_in_guest_and_true_for_a_power_user_let_in_the_same_way(world):
    async def scenario(w):
        team = await w.group('both', GUEST, POWER)
        await settings(w, {'mcp_enabled': True})
        assert (await me(w, GUEST), await me(w, POWER)) == ({'mcp': False}, {'mcp': False})
        # By role.
        await settings(w, {'roles': {'power_user': True, 'guest': True}})
        assert (await me(w, GUEST), await me(w, POWER)) == ({'mcp': False}, {'mcp': True}), 'by role'
        await settings(w, {'roles': {'power_user': False, 'guest': False}})
        # By a listed group they are both in.
        await put(w, f'/groups/{team}')
        assert (await me(w, GUEST), await me(w, POWER)) == ({'mcp': False}, {'mcp': True}), 'by group'
        await delete(w, f'/groups/{team}')
        # By their own answer.
        await put(w, f'/users/{GUEST}', {'allowed': True})
        await put(w, f'/users/{POWER}', {'allowed': True})
        assert (await me(w, GUEST), await me(w, POWER)) == ({'mcp': False}, {'mcp': True}), 'by own answer'
        # All of it at once.
        await settings(w, {'roles': {'power_user': True, 'guest': True}})
        await put(w, f'/groups/{team}')
        assert (await me(w, GUEST), await me(w, POWER)) == ({'mcp': False}, {'mcp': True}), 'every way'
        # The server off: neither.
        await settings(w, {'mcp_enabled': False})
        assert (await me(w, GUEST), await me(w, POWER)) == ({'mcp': False}, {'mcp': False}), 'off'

    world(scenario)


# --- 1.2 The order of refusals ---


def test_the_door_off_and_the_accounts_own_state_come_before_the_guest_refusal(world):
    """Every refusal that comes before the guest's own (which the first test checks), for a guest let in every way."""

    async def scenario(w):
        team = await w.group('crew', GUEST)
        token = await mcp_token_row(w, GUEST)
        door = Door(w)
        # Let in every way while the door is off: the door's sentence, at the core and at /mcp.
        await every_way(w, team)
        await refused(w, GUEST, 'the door off', MCP_OFF)
        for method, params in ASKS:
            response, _ = await door.ask(method, params, token)
            assert refusal(response, 403) == MCP_OFF, method
        await settings(w, {'mcp_enabled': True})
        # An inactive guest: the account's words, not the guest sentence; the door switched off still comes first.
        await w.database.execute('UPDATE users SET is_active = FALSE WHERE id = $1', GUEST)
        inactive = await api_refusal_of(w, GUEST)
        assert inactive not in (MCP_OFF, NOT_LET_IN), inactive
        await refused_by_the_account(w, GUEST, inactive, 'inactive')
        await settings(w, {'mcp_enabled': False})
        assert await assistant.door_refusal(user_id=GUEST, door='mcp') == MCP_OFF
        await settings(w, {'mcp_enabled': True})
        await w.database.execute('UPDATE users SET is_active = TRUE WHERE id = $1', GUEST)
        # A guest owing a password change: the account's words.
        await w.database.execute('UPDATE users SET must_change_password = TRUE WHERE id = $1', GUEST)
        owes = await api_refusal_of(w, GUEST)
        assert owes not in (MCP_OFF, NOT_LET_IN, inactive), owes
        await refused_by_the_account(w, GUEST, owes, 'owes a password change')
        await settings(w, {'mcp_enabled': False})
        assert await assistant.door_refusal(user_id=GUEST, door='mcp') == MCP_OFF

    world(scenario)


async def refused_by_the_account(w: World, user_id: int, words: str, step: str) -> None:
    """Refused with the account's words, at the core (auth/me can't be asked: the API refuses the account)."""
    assert await assistant.door_refusal(user_id=user_id, door='mcp') == words, step
    failure = await error(user_id, 'list_datasets')
    assert failure == {'code': 'not_allowed', 'message': words, 'retryable': False}, (step, failure)


# --- 1.3 Read afresh at every request: a power user made a guest, and back ---


def test_a_power_user_made_a_guest_is_refused_at_the_next_request_and_let_in_again_when_made_a_power_user(world):
    async def scenario(w):
        await farm(w)  # dataset FARM, which POWER can view
        await open_door(w)  # every role ticked
        await put(w, f'/users/{POWER}', {'allowed': True})
        door = Door(w)
        made = await make_token(w, POWER)  # through the API, as a power user
        token = made['token']
        for method, params in ASKS:
            response, message = await door.ask(method, params, token)
            assert response.status_code == 200 and message is not None and 'result' in message, (method, response.text)
        described = await door.call(token, 'describe_dataset', {'dataset': FARM})
        assert described.get('isError') in (None, False), described
        await let_in(w, POWER, 'a power user')

        # Made a guest: the next request is refused, whatever it asks.
        demoted = await w.call(ADMIN, 'PATCH', f'/api/users/{POWER}', json={'role': 'guest'})
        assert demoted.status_code == 200, demoted.text
        for method, params in ASKS:
            response, _ = await door.ask(method, params, token)
            assert refusal(response, 403) == NOT_LET_IN, method
        await refused(w, POWER, 'made a guest')
        failure = await error(POWER, 'describe_dataset', {'dataset': FARM})
        assert failure == {'code': 'not_allowed', 'message': NOT_LET_IN, 'retryable': False}, failure
        # Nothing happened to the token: the rule refuses it.
        listed = {row['id']: row for row in (await w.call(POWER, 'GET', '/api/tokens')).json()}
        assert (listed[made['id']]['scope'], listed[made['id']]['is_active']) == ('mcp', True), listed
        # The settings that let them in are kept as they were.
        state = await settings(w)
        assert state['roles'] == {'admin': True, 'power_user': True, 'guest': True}, state
        assert {'id': POWER, 'username': f'user-{POWER}', 'allowed': True} in state['users'], state

        # Made a power user again: the same token works again, with nothing else changed.
        promoted = await w.call(ADMIN, 'PATCH', f'/api/users/{POWER}', json={'role': 'power_user'})
        assert promoted.status_code == 200, promoted.text
        for method, params in ASKS:
            response, message = await door.ask(method, params, token)
            assert response.status_code == 200 and message is not None and 'result' in message, (method, response.text)
        assert await door.call(token, 'describe_dataset', {'dataset': FARM}) == described
        await let_in(w, POWER, 'a power user again')
        # And the settings decide for them as before: their answer taken back and power users unticked, refused;
        # in a ticked group, let in.
        await delete(w, f'/users/{POWER}')
        await settings(w, {'roles': {'power_user': False}})
        response, _ = await door.ask('tools/list', {}, token)
        assert refusal(response, 403) == NOT_LET_IN
        await refused(w, POWER, 'a power user nothing lets in')
        team = await w.group('crew', POWER)
        await put(w, f'/groups/{team}')
        response, message = await door.ask('tools/list', {}, token)
        assert response.status_code == 200 and message is not None and 'result' in message, response.text
        await let_in(w, POWER, 'a power user in a ticked group')

    world(scenario)


# --- 1.4 The settings keep their shape ---


def test_the_settings_still_read_and_write_roles_guest_a_guests_own_answer_and_a_group_with_a_guest_in_it(world):
    async def scenario(w):
        team = await w.group('guests-too', GUEST, POWER)
        written = await settings(w, {'roles': {'guest': True}})
        assert written['roles'] == {'admin': True, 'power_user': False, 'guest': True}, written
        answered = await put(w, f'/users/{GUEST}', {'allowed': True})
        assert answered['users'] == [{'id': GUEST, 'username': f'user-{GUEST}', 'allowed': True}], answered
        ticked = await put(w, f'/groups/{team}')
        assert ticked['groups'] == [{'id': team, 'name': 'guests-too'}], ticked
        assert await settings(w) == {
            **SHIPPED,
            'roles': {'admin': True, 'power_user': False, 'guest': True},
            'groups': [{'id': team, 'name': 'guests-too'}],
            'users': [{'id': GUEST, 'username': f'user-{GUEST}', 'allowed': True}],
        }
        # The whole roles object, and the door, in one PATCH; the rest stays.
        both = await settings(w, {'mcp_enabled': True, 'roles': {'admin': True, 'power_user': True, 'guest': True}})
        assert (both['mcp_enabled'], both['roles']) == (True, {'admin': True, 'power_user': True, 'guest': True})
        again = await settings(w)
        assert again['roles']['guest'] is True and again['groups'] == [{'id': team, 'name': 'guests-too'}]
        assert again['users'] == [{'id': GUEST, 'username': f'user-{GUEST}', 'allowed': True}]
        # Written back the other way, and removed.
        answered = await put(w, f'/users/{GUEST}', {'allowed': False})
        assert answered['users'] == [{'id': GUEST, 'username': f'user-{GUEST}', 'allowed': False}], answered
        assert (await settings(w, {'roles': {'guest': False}}))['roles'] == {
            'admin': True,
            'power_user': True,
            'guest': False,
        }
        assert (await delete(w, f'/users/{GUEST}'))['users'] == []
        assert (await delete(w, f'/groups/{team}'))['groups'] == []
        await let_in(w, POWER, 'power users ticked')  # the guest's entries went, nobody else's settings changed

    world(scenario)


# --- 1.5 A guest makes no token ---


def test_a_guest_makes_no_token_of_any_kind_whatever_the_assistants_settings_say(world):
    async def scenario(w):
        team = await w.group('crew', GUEST)
        await settings(w, {'mcp_enabled': True})
        await every_way(w, team)
        count = 'SELECT count(*) FROM api_tokens WHERE user_id = $1'
        before = await w.database.fetchval(count, GUEST)
        for scope in (None, 'full', 'worker', 'mcp'):
            body = {'name': f'guest-{scope}'} if scope is None else {'name': f'guest-{scope}', 'scope': scope}
            response = await w.call(GUEST, 'POST', '/api/tokens', json=body)
            assert response.status_code == 403, (scope, response.text)
            assert 'token' not in response.json(), (scope, response.text)
        assert await w.database.fetchval(count, GUEST) == before
        # A power user made a guest makes none either.
        demoted = await w.call(ADMIN, 'PATCH', f'/api/users/{POWER}', json={'role': 'guest'})
        assert demoted.status_code == 200, demoted.text
        response = await w.call(POWER, 'POST', '/api/tokens', json={'name': 'after', 'scope': 'mcp'})
        assert response.status_code == 403, response.text

    world(scenario)


# --- 3.3 GET /api/auth/me?fresh=1 reads the switch from the database ---


async def me_answer(w: World, user_id: int, fresh: bool = False) -> dict[str, Any]:
    response = await w.call(user_id, 'GET', '/api/auth/me', params={'fresh': '1'} if fresh else None)
    assert response.status_code == 200, response.text
    return response.json()


def test_auth_me_fresh_reads_the_mcp_switch_from_the_database_for_that_request(world):
    """A process that has just answered auth/me remembers the switch for a few seconds; a change made in the database
    directly (as another process's change arrives) is seen at once by `fresh=1`, which changes nothing else in the
    answer and is open to anyone signed in. The plain request may say either, so only its shape is checked."""

    async def scenario(w):
        team = await w.group('crew', GUEST)
        await every_way(w, team)  # the guest let in every way
        await put(w, f'/users/{POWER}', {'allowed': True})  # a power user let in
        switch = 'UPDATE assistant_settings SET mcp_enabled = $1 WHERE singleton'
        assert await w.database.fetchval('SELECT mcp_enabled FROM assistant_settings WHERE singleton') is False

        # Off, and this process has just said so: switched on behind its back.
        assert (await me_answer(w, ADMIN))['assistant'] == {'mcp': False}
        await w.database.execute(switch, True)
        fresh = await me_answer(w, ADMIN, fresh=True)
        assert fresh['assistant'] == {'mcp': True}, fresh
        plain = await me_answer(w, ADMIN)
        assert set(plain) == set(fresh), (plain, fresh)
        assert {k: v for k, v in fresh.items() if k != 'assistant'} == {
            k: v for k, v in plain.items() if k != 'assistant'
        }
        # Anyone signed in may send it: the power user let in may use it; the guest, let in every way, may not.
        for user_id, may in ((POWER, True), (GUEST, False)):
            theirs = await me_answer(w, user_id, fresh=True)
            assert theirs['assistant'] == {'mcp': may}, (user_id, theirs)
            ordinary = await me_answer(w, user_id)
            assert {k: v for k, v in theirs.items() if k != 'assistant'} == {
                k: v for k, v in ordinary.items() if k != 'assistant'
            }, user_id

        # The other way: on, and this process has just said so (the settings API makes it read the switch again);
        # switched off behind its back.
        await settings(w, {'mcp_enabled': True})
        assert (await me_answer(w, ADMIN))['assistant'] == {'mcp': True}
        await w.database.execute(switch, False)
        for user_id in (ADMIN, POWER, GUEST):
            assert (await me_answer(w, user_id, fresh=True))['assistant'] == {'mcp': False}, user_id

    world(scenario)


# --- 4. Docs ---


def section(text: str, heading: str) -> str:
    """The text under `heading` (a whole line), up to the next heading of the same or a higher level. A line inside a
    fenced code block (```) is never a heading: a code comment such as `# {...}` doesn't end the section."""
    level = len(heading) - len(heading.lstrip('#'))
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.rstrip() == heading]
    assert starts, heading
    taken: list[str] = []
    fenced = False
    for line in lines[starts[0] + 1 :]:
        if line.lstrip().startswith('```'):
            fenced = not fenced
        elif not fenced and re.match(rf'#{{1,{level}}} ', line):
            break
        taken.append(line)
    return '\n'.join(taken)


def test_a_comment_in_a_fenced_code_block_does_not_end_a_section():
    text = (
        '## Part\n### Wanted\nbefore the code\n```python\n# a comment, not a heading\ninside = 1\n```\n'
        'after the code\n#### A lower heading stays in\n### Next\nnot wanted\n'
    )
    found = section(text, '### Wanted')
    for kept in ('before the code', '# a comment, not a heading', 'inside = 1', 'after the code', 'A lower heading'):
        assert kept in found, (kept, found)
    assert 'not wanted' not in found and 'Next' not in found, found


def test_the_docs_say_a_guest_cant_use_the_mcp_server_as_a_rule_and_keep_the_guests_setting_for_the_chat():
    who = section((ROOT / 'docs/ASSISTANT.md').read_text(), '### Who may use it')
    # Not only because guests make no tokens.
    assert 'lets no guest use the MCP server today' not in who, who
    sentences = re.split(r'(?<=[.;:])\s+', ' '.join(who.split()))
    rule = [s for s in sentences if re.search(r"\bguests?\b.*\b(never|can't|can’t|cannot|may not)\b", s, re.I)]
    assert rule, f'no sentence of "Who may use it" says a guest never may: {who}'
    assert 'chat' in who.lower(), who
    unreleased = section((ROOT / 'CHANGELOG.md').read_text(), release_heading('2.14.0'))
    assert re.search(r'\bguests?\b', unreleased, re.I), 'the Unreleased entry says nothing of guests'
