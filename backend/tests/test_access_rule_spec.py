"""Who may open a dataset, and at what level: the first step of access levels and groups (issue 12).

Written from the contract, not from the code, and tested where a user of the system meets it: the HTTP API, signed
in with real API tokens, and the rows of the tables the contract names. The rule:

- A share has a level: dataset_access.level, 1 view, 2 tag, 3 edit, 4 manage; a share written without one is edit.
- A user in a group has the group's shares (groups, group_members, dataset_group_access).
- A global dataset gives every signed-in user its everyone_level (1..3). It is view unless set: a dataset made global
  (created global, or made global by PATCH) starts at view, or at the level the request names, and a dataset made
  global again keeps the level it had. The upgrade gave the datasets that were global then edit, and the private ones
  view, so nobody's access changed.
- A user's level is the highest of their own share, their groups' shares and, on a global dataset, its everyone
  level. An admin has manage on every dataset; a guest has at most view.
- No level: 403 "Access denied for this dataset". Not signed in: 401, before the dataset is looked up. No such
  dataset: 404. The dataset list shows a user exactly the datasets they have a level in.
- owner_id only records who created a dataset: an owner has access through a share (creating a dataset writes one
  for its creator; the upgrade writes one for every existing owner), and without it is refused.
- Admins share datasets (POST/DELETE /datasets/{id}/access, PATCH /datasets/{id} everyone_level) and manage groups
  (/groups). Dataset responses carry the caller's access_level by name, and the everyone_level.
- POST /datasets/{id}/access names exactly one target, a user or a group (else 422, nothing written); an unknown
  user or group is a 404. Without a level a new share is edit and an existing one keeps its level; with a level the
  share is set to it. An explicit null for user_id, group or level reads as the key left out. A grant that changes a
  share's level records the caller as its granted_by_user_id; one that changes nothing leaves it.

Write endpoints still go by role in this step: a test here that writes uses only shares at edit, which every step
allows, so it holds after the next step too.

The contract leaves some shapes open; where it does, these tests read the answer either way or check the table rows
instead: a grant's level and everyone_level may come as a name or as its number; a group's id is read from the groups
table after POST /groups; a group grant in GET /datasets/{id}/access is found by its group_id or its name; a group is
renamed with PATCH /groups/{id} {"name": ...}.
"""

import os
import secrets
from typing import Any, Optional

import asyncpg
import httpx
import pytest

from backend import database as database_module
from backend import main
from backend.crypto import hash_token
from backend.tests import test_field_registry_postgres, test_upgrade_paths
from backend.tests.test_field_registry_postgres import ingest

registry_database = test_field_registry_postgres.registry_database
database = test_upgrade_paths.database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

LEVELS = ('view', 'tag', 'edit', 'manage')  # stored as 1..4
ADMIN, OTHER_ADMIN, POWER, OTHER_POWER, GUEST, OTHER_GUEST = 1, 2, 10, 11, 20, 21
ROLES = {
    ADMIN: 'admin',
    OTHER_ADMIN: 'admin',
    POWER: 'power_user',
    OTHER_POWER: 'power_user',
    GUEST: 'guest',
    OTHER_GUEST: 'guest',
}
DENIED = {'detail': 'Access denied for this dataset'}


def number(level: str) -> int:
    return LEVELS.index(level) + 1


def level_name(level: Any) -> str:
    """A level as its name, given as a name or as its number (the contract names access_level only)."""
    return dict(enumerate(LEVELS, start=1))[level] if isinstance(level, int) else level


def names_in(body: Any) -> set[str]:
    """Every "name" anywhere in a JSON body."""
    if isinstance(body, dict):
        own = {body['name']} if isinstance(body.get('name'), str) else set()
        return own.union(*(names_in(value) for value in body.values()))
    if isinstance(body, list):
        return set().union(*(names_in(item) for item in body))
    return set()


def group_grant_levels(body: Any, group_id: int, name: str) -> list[str]:
    """The levels GET /datasets/{id}/access gives the group: each object with a level that names the group, by its
    group_id or its name."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if 'level' in node and (node.get('group_id') == group_id or name in node.values()):
                found.append(level_name(node['level']))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return found


class World:
    """The whole app on the test's schema, with a user of each role signed in with an API token."""

    def __init__(self, database: Any, client: httpx.AsyncClient):
        self.database = database
        self.client = client
        self.tokens: dict[int, str] = {}

    async def user(self, user_id: int, role: str) -> None:
        # schema.sql seeds the admin as user 1, who must change the password before using the API.
        await self.database.execute(
            'INSERT INTO users (id, username, role) VALUES ($1, $2, $3) '
            'ON CONFLICT (id) DO UPDATE SET role = EXCLUDED.role, must_change_password = FALSE',
            user_id,
            f'user-{user_id}',
            role,
        )
        raw = secrets.token_urlsafe(32)
        await self.database.execute(
            "INSERT INTO api_tokens (user_id, token, name) VALUES ($1, $2, 'access-rule')",
            user_id,
            hash_token(raw),
        )
        self.tokens[user_id] = raw

    async def call(self, user_id: Optional[int], method: str, path: str, **kwargs: Any) -> httpx.Response:
        """The request as `user_id` (None: not signed in)."""
        headers = {} if user_id is None else {'Authorization': f'Bearer {self.tokens[user_id]}'}
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def dataset(
        self,
        dataset_id: int,
        *,
        is_global: bool = False,
        everyone: Optional[str] = None,
        owner: Optional[int] = None,
    ) -> None:
        """A dataset row; its everyone_level stays the column's default unless `everyone` is given."""
        await self.database.execute(
            'INSERT INTO datasets (id, name, is_global, owner_id) VALUES ($1, $2, $3, $4)',
            dataset_id,
            f'dataset-{dataset_id}',
            is_global,
            owner,
        )
        if everyone is not None:
            await self.database.execute(
                'UPDATE datasets SET everyone_level = $2 WHERE id = $1',
                dataset_id,
                number(everyone),
            )

    async def share(self, dataset_id: int, user_id: int, level: Optional[str] = None) -> None:
        """A dataset_access row; without a level, written as before levels existed."""
        if level is None:
            await self.database.execute(
                'INSERT INTO dataset_access (dataset_id, user_id) VALUES ($1, $2)',
                dataset_id,
                user_id,
            )
        else:
            await self.database.execute(
                'INSERT INTO dataset_access (dataset_id, user_id, level) VALUES ($1, $2, $3)',
                dataset_id,
                user_id,
                number(level),
            )

    async def group(self, name: str, *members: int) -> int:
        """A group an admin made with POST /groups, with `members` added; its id from the groups table."""
        created = await self.call(ADMIN, 'POST', '/api/groups', json={'name': name})
        assert created.is_success, created.text
        group_id = await self.database.fetchval('SELECT id FROM groups WHERE name = $1', name)
        assert group_id is not None
        for member in members:
            added = await self.call(ADMIN, 'PUT', f'/api/groups/{group_id}/members/{member}')
            assert added.is_success, added.text
        return group_id

    async def share_with_group(self, dataset_id: int, name: str, level: Optional[str] = None) -> None:
        body: dict[str, Any] = {'group': name} if level is None else {'group': name, 'level': level}
        shared = await self.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/access', json=body)
        assert shared.is_success, shared.text

    async def visible(self, user_id: int) -> set[int]:
        """The ids GET /datasets lists for the user, each listed once."""
        response = await self.call(user_id, 'GET', '/api/datasets')
        assert response.status_code == 200, response.text
        ids = [row['id'] for row in response.json()]
        assert len(ids) == len(set(ids)), f'a dataset listed twice for user {user_id}: {ids}'
        return set(ids)

    async def listed(self, user_id: int) -> dict[int, str]:
        """{dataset id: access_level} as GET /datasets lists them for the user, each listed once."""
        response = await self.call(user_id, 'GET', '/api/datasets')
        assert response.status_code == 200, response.text
        rows = response.json()
        ids = [row['id'] for row in rows]
        assert len(ids) == len(set(ids)), f'a dataset listed twice for user {user_id}: {ids}'
        return {row['id']: row['access_level'] for row in rows}

    async def level(self, user_id: int, dataset_id: int) -> Optional[str]:
        """The user's level in the dataset as GET /datasets/{id} gives it (None: refused, 403), which the dataset
        list must agree with."""
        response = await self.call(user_id, 'GET', f'/api/datasets/{dataset_id}')
        listed = await self.listed(user_id)
        if response.status_code == 403:
            assert response.json() == DENIED
            assert dataset_id not in listed, f'user {user_id} is refused dataset {dataset_id} but it is listed'
            return None
        assert response.status_code == 200, response.text
        level = response.json()['access_level']
        assert listed.get(dataset_id) == level, f'user {user_id}, dataset {dataset_id}: get {level}, list {listed}'
        return level


@pytest.fixture
def world(registry_database, monkeypatch):
    """world(scenario) runs `scenario(World)`. registry_database's datasets 1 and 2 are made private, unshared."""

    def run(scenario):
        async def with_app(database):
            # Modules registry_database doesn't point at the test's schema (auth, access, users, and wherever the
            # groups routes are) use the app's shared Database: it gets the test's pool.
            monkeypatch.setattr(database_module.db, 'pool', database.pool)
            await database.execute('UPDATE datasets SET is_global = FALSE WHERE id IN (1, 2)')
            await database.execute("SELECT setval(pg_get_serial_sequence('datasets', 'id'), 1000)")
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                world = World(database, client)
                for user_id, role in ROLES.items():
                    await world.user(user_id, role)
                await scenario(world)

        registry_database(with_app)

    return run


# --- 401, 403, 404 ---


def test_not_signed_in_is_401_before_the_dataset_is_looked_up(world):
    async def scenario(w):
        await w.dataset(10, is_global=True)
        await w.dataset(11)
        for path in ('/api/datasets/10', '/api/datasets/11', '/api/datasets/999999', '/api/datasets/999999/access'):
            assert (await w.call(None, 'GET', path)).status_code == 401, path

    world(scenario)


def test_a_signed_in_user_gets_404_for_a_dataset_that_does_not_exist(world):
    async def scenario(w):
        for user_id in (ADMIN, POWER, GUEST):
            assert (await w.call(user_id, 'GET', '/api/datasets/999999')).status_code == 404

    world(scenario)


def test_without_any_level_a_user_is_refused_with_403(world):
    async def scenario(w):
        await w.dataset(10)
        await w.share(10, OTHER_POWER)  # someone else's share
        for user_id in (POWER, GUEST):
            response = await w.call(user_id, 'GET', '/api/datasets/10')
            assert (response.status_code, response.json()) == (403, DENIED)
            assert 10 not in await w.visible(user_id)

    world(scenario)


# --- Each source of a level ---


def test_an_own_share_gives_its_level(world):
    async def scenario(w):
        for index, level in enumerate(LEVELS):
            await w.dataset(10 + index)
            await w.share(10 + index, POWER, level)
        assert [await w.level(POWER, 10 + index) for index in range(4)] == list(LEVELS)
        rows = await w.database.fetch('SELECT dataset_id, level FROM dataset_access ORDER BY dataset_id')
        assert [(row['dataset_id'], row['level']) for row in rows] == [(10, 1), (11, 2), (12, 3), (13, 4)]
        for index in range(4):
            assert await w.level(OTHER_POWER, 10 + index) is None  # a share is the one user's

    world(scenario)


def test_a_share_written_without_a_level_means_edit(world):
    async def scenario(w):
        await w.dataset(10)
        await w.database.execute('INSERT INTO dataset_access (dataset_id, user_id) VALUES (10, $1)', POWER)
        level = await w.database.fetchval(
            'SELECT level FROM dataset_access WHERE dataset_id = 10 AND user_id = $1',
            POWER,
        )
        assert level == 3
        assert await w.level(POWER, 10) == 'edit'

    world(scenario)


def test_a_group_share_gives_its_members_its_level(world):
    async def scenario(w):
        await w.dataset(10)
        await w.dataset(11)  # the group has no share on it
        team = await w.group('team', POWER)
        await w.share_with_group(10, 'team', 'tag')
        stored = await w.database.fetchval(
            'SELECT level FROM dataset_group_access WHERE dataset_id = 10 AND group_id = $1',
            team,
        )
        assert stored == 2
        assert await w.level(POWER, 10) == 'tag'
        assert await w.level(POWER, 11) is None
        assert await w.level(OTHER_POWER, 10) is None  # not a member

    world(scenario)


def test_a_global_dataset_gives_every_signed_in_user_its_everyone_level(world):
    async def scenario(w):
        await w.dataset(10, is_global=True)  # everyone_level not given: view
        assert await w.database.fetchval('SELECT everyone_level FROM datasets WHERE id = 10') == 1
        assert await w.level(POWER, 10) == 'view'
        for level in ('view', 'tag', 'edit'):
            await w.database.execute('UPDATE datasets SET everyone_level = $1 WHERE id = 10', number(level))
            assert await w.level(POWER, 10) == level
            assert await w.level(OTHER_POWER, 10) == level

    world(scenario)


def test_the_everyone_level_counts_only_on_a_global_dataset(world):
    async def scenario(w):
        await w.dataset(10, everyone='edit')
        await w.dataset(11, everyone='tag')
        await w.share(11, POWER, 'view')
        assert await w.level(POWER, 10) is None
        assert await w.level(GUEST, 10) is None
        assert await w.level(POWER, 11) == 'view'

    world(scenario)


def test_the_level_columns_hold_only_levels(world):
    """dataset_access.level is one of 1..4 and everyone_level one of 1..3: manage for everyone isn't a setting."""

    async def scenario(w):
        await w.dataset(10, is_global=True)
        for bad in (0, 4):
            with pytest.raises(asyncpg.IntegrityConstraintViolationError):
                await w.database.execute('UPDATE datasets SET everyone_level = $1 WHERE id = 10', bad)
        for bad in (0, 5):
            with pytest.raises(asyncpg.IntegrityConstraintViolationError):
                await w.database.execute(
                    'INSERT INTO dataset_access (dataset_id, user_id, level) VALUES (10, $1, $2)',
                    POWER,
                    bad,
                )
        assert await w.level(POWER, 10) == 'view'  # the everyone level as it was: the column's default

    world(scenario)


# --- The highest level wins ---

# (dataset, global, everyone_level, POWER's own share, red's share, blue's share, POWER's level)
HIGHEST = [
    (10, True, 'edit', 'view', None, None, 'edit'),  # an own share below the everyone level
    (11, True, 'view', 'edit', None, None, 'edit'),  # the everyone level below an own share
    (12, False, None, 'view', 'tag', 'manage', 'manage'),  # two groups above the own share
    (13, False, None, 'manage', 'view', None, 'manage'),  # an own share above a group's
    (14, False, None, None, 'view', 'tag', 'tag'),  # the higher of two groups
    (15, True, 'view', None, 'tag', None, 'tag'),  # a group above the everyone level
    (16, True, 'tag', None, 'view', 'view', 'tag'),  # the everyone level above two groups
    (17, False, 'edit', 'view', None, None, 'view'),  # a private dataset's everyone level counts for nothing
    (18, True, 'view', 'view', 'view', 'view', 'view'),  # every source the same
    (19, False, None, None, None, 'edit', 'edit'),  # one group only
    (20, False, None, None, None, None, None),  # nothing
]


def test_the_highest_level_wins(world):
    """POWER and GUEST are in red and blue and have the same own shares; OTHER_POWER is in no group."""

    async def scenario(w):
        await w.group('red', POWER, GUEST)
        await w.group('blue', POWER, GUEST)
        for dataset_id, is_global, everyone, own, red, blue, _ in HIGHEST:
            await w.dataset(dataset_id, is_global=is_global, everyone=everyone)
            if own:
                await w.share(dataset_id, POWER, own)
                await w.share(dataset_id, GUEST, own)
            for name, level in (('red', red), ('blue', blue)):
                if level:
                    await w.share_with_group(dataset_id, name, level)
        for dataset_id, is_global, everyone, _, _, _, expected in HIGHEST:
            assert await w.level(POWER, dataset_id) == expected, dataset_id
            assert await w.level(GUEST, dataset_id) == ('view' if expected else None), dataset_id
            assert await w.level(OTHER_POWER, dataset_id) == ((everyone or 'view') if is_global else None), dataset_id
            assert await w.level(ADMIN, dataset_id) == 'manage', dataset_id

    world(scenario)


def test_a_share_below_the_everyone_level_does_not_hide_it(world):
    async def scenario(w):
        await w.group('readers', POWER)
        await w.dataset(10, is_global=True, everyone='edit')
        await w.share(10, POWER, 'view')
        await w.dataset(11, is_global=True, everyone='tag')
        await w.share_with_group(11, 'readers', 'view')
        assert await w.level(POWER, 10) == 'edit'
        assert await w.level(POWER, 11) == 'tag'

    world(scenario)


def test_a_user_in_two_groups_gets_the_higher_share_and_sees_the_dataset_once(world):
    async def scenario(w):
        await w.dataset(10, is_global=True, everyone='view')
        await w.share(10, POWER, 'view')
        await w.group('red', POWER, OTHER_POWER)
        await w.group('blue', POWER)
        await w.share_with_group(10, 'red', 'tag')
        await w.share_with_group(10, 'blue', 'manage')
        assert await w.level(POWER, 10) == 'manage'  # level() also checks the list holds it once
        assert await w.level(OTHER_POWER, 10) == 'tag'
        assert await w.listed(ADMIN) == {1: 'manage', 2: 'manage', 10: 'manage'}

    world(scenario)


def test_a_guest_is_capped_at_view_even_in_a_manage_group(world):
    async def scenario(w):
        for dataset_id in (10, 11, 13, 14):
            await w.dataset(dataset_id)
        await w.dataset(12, is_global=True, everyone='edit')
        await w.group('leads', GUEST)
        await w.group('taggers', GUEST)
        await w.share_with_group(10, 'leads', 'manage')
        await w.share(11, GUEST, 'manage')
        await w.share_with_group(13, 'taggers', 'tag')
        levels = {dataset_id: await w.level(GUEST, dataset_id) for dataset_id in (10, 11, 12, 13, 14)}
        assert levels == {10: 'view', 11: 'view', 12: 'view', 13: 'view', 14: None}
        sample = (await ingest([{}], dataset_id=10))['ids'][0]
        tagged = await w.call(GUEST, 'PATCH', f'/api/datasets/10/samples/{sample}', json={'tags': ['seen']})
        assert tagged.status_code == 403

    world(scenario)


def test_an_admin_has_manage_on_every_dataset(world):
    async def scenario(w):
        await w.dataset(10)
        await w.dataset(11, is_global=True, everyone='view')
        await w.dataset(12)
        await w.share(12, ADMIN, 'view')  # an admin's own lower share doesn't lower them
        await w.dataset(13)
        await w.group('viewers', ADMIN)
        await w.share_with_group(13, 'viewers', 'view')
        for dataset_id in (1, 2, 10, 11, 12, 13):
            assert await w.level(ADMIN, dataset_id) == 'manage'
        assert await w.listed(ADMIN) == dict.fromkeys((1, 2, 10, 11, 12, 13), 'manage')

    world(scenario)


# --- Owners ---


def test_an_owner_without_a_share_is_refused(world):
    async def scenario(w):
        await w.dataset(10, owner=POWER)  # owner_id set by hand, no share
        await w.dataset(11, owner=GUEST)
        for user_id, dataset_id in ((POWER, 10), (GUEST, 11)):
            response = await w.call(user_id, 'GET', f'/api/datasets/{dataset_id}')
            assert (response.status_code, response.json()) == (403, DENIED)
            assert dataset_id not in await w.visible(user_id)

    world(scenario)


def test_an_owner_gets_the_level_of_their_share_and_no_more(world):
    async def scenario(w):
        await w.dataset(10, owner=POWER)
        await w.share(10, POWER, 'view')
        await w.dataset(11, is_global=True, everyone='tag', owner=POWER)
        assert await w.level(POWER, 10) == 'view'
        assert await w.level(POWER, 11) == 'tag'

    world(scenario)


def test_creating_a_dataset_shares_it_with_its_creator_and_removing_that_share_refuses_them(world):
    async def scenario(w):
        created = await w.call(OTHER_ADMIN, 'POST', '/api/datasets', json={'name': 'made-by-an-admin'})
        assert created.is_success, created.text
        dataset_id = created.json()['id']
        assert await w.database.fetchval('SELECT owner_id FROM datasets WHERE id = $1', dataset_id) == OTHER_ADMIN
        shares = 'SELECT count(*) FROM dataset_access WHERE dataset_id = $1 AND user_id = $2'
        assert await w.database.fetchval(shares, dataset_id, OTHER_ADMIN) == 1
        # Only admins create datasets: the creator stops being one, and keeps access by the share.
        demoted = await w.call(ADMIN, 'PATCH', f'/api/users/{OTHER_ADMIN}', json={'role': 'power_user'})
        assert demoted.status_code == 200, demoted.text
        assert await w.level(OTHER_ADMIN, dataset_id) is not None
        removed = await w.call(ADMIN, 'DELETE', f'/api/datasets/{dataset_id}/access/{OTHER_ADMIN}')
        assert removed.is_success, removed.text
        assert await w.level(OTHER_ADMIN, dataset_id) is None
        assert await w.database.fetchval('SELECT owner_id FROM datasets WHERE id = $1', dataset_id) == OTHER_ADMIN

    world(scenario)


# --- The dataset list ---


def test_the_dataset_list_is_exactly_the_datasets_with_a_level(world):
    async def scenario(w):
        await w.group('red', POWER, GUEST)
        await w.group('blue', OTHER_POWER)
        await w.dataset(10)
        await w.share(10, POWER, 'view')
        await w.dataset(11)
        await w.share_with_group(11, 'red', 'edit')
        await w.dataset(12, is_global=True, everyone='view')
        await w.dataset(13)  # nobody's
        await w.dataset(14, owner=POWER)  # an owner without a share
        await w.dataset(15)
        await w.share(15, OTHER_POWER)
        await w.dataset(16)
        await w.share_with_group(16, 'blue', 'tag')
        await w.dataset(17)
        await w.share(17, POWER, 'tag')
        await w.share_with_group(17, 'red', 'manage')
        assert await w.listed(POWER) == {10: 'view', 11: 'edit', 12: 'view', 17: 'manage'}
        assert await w.listed(GUEST) == {11: 'view', 12: 'view', 17: 'view'}
        assert await w.listed(OTHER_POWER) == {12: 'view', 15: 'edit', 16: 'tag'}
        assert await w.listed(OTHER_GUEST) == {12: 'view'}
        assert await w.listed(ADMIN) == dict.fromkeys((1, 2, *range(10, 18)), 'manage')

    world(scenario)


# --- Losing access ---


def test_removing_a_share_removes_access(world):
    async def scenario(w):
        await w.dataset(10)
        await w.share(10, POWER, 'manage')
        await w.share(10, OTHER_POWER, 'view')
        assert await w.level(POWER, 10) == 'manage'
        removed = await w.call(ADMIN, 'DELETE', f'/api/datasets/10/access/{POWER}')
        assert removed.is_success, removed.text
        assert await w.level(POWER, 10) is None
        assert await w.level(OTHER_POWER, 10) == 'view'

    world(scenario)


def test_removing_a_member_removes_their_access_only(world):
    async def scenario(w):
        await w.dataset(10)
        red = await w.group('red', POWER, OTHER_POWER)
        await w.share_with_group(10, 'red', 'edit')
        removed = await w.call(ADMIN, 'DELETE', f'/api/groups/{red}/members/{POWER}')
        assert removed.is_success, removed.text
        assert await w.level(POWER, 10) is None
        assert await w.level(OTHER_POWER, 10) == 'edit'
        members = await w.database.fetch('SELECT user_id FROM group_members WHERE group_id = $1', red)
        assert [row['user_id'] for row in members] == [OTHER_POWER]

    world(scenario)


def test_leaving_one_of_two_groups_leaves_the_other_groups_level(world):
    async def scenario(w):
        await w.dataset(10)
        red = await w.group('red', POWER)
        blue = await w.group('blue', POWER)
        await w.share_with_group(10, 'red', 'manage')
        await w.share_with_group(10, 'blue', 'view')
        assert await w.level(POWER, 10) == 'manage'
        assert (await w.call(ADMIN, 'DELETE', f'/api/groups/{red}/members/{POWER}')).is_success
        assert await w.level(POWER, 10) == 'view'
        assert (await w.call(ADMIN, 'DELETE', f'/api/groups/{blue}/members/{POWER}')).is_success
        assert await w.level(POWER, 10) is None

    world(scenario)


def test_removing_a_group_share_removes_its_members_access_there(world):
    async def scenario(w):
        await w.dataset(10)
        await w.dataset(11)
        red = await w.group('red', POWER)
        await w.share_with_group(10, 'red', 'edit')
        await w.share_with_group(11, 'red', 'tag')
        removed = await w.call(ADMIN, 'DELETE', f'/api/datasets/10/access/groups/{red}')
        assert removed.is_success, removed.text
        assert await w.level(POWER, 10) is None
        assert await w.level(POWER, 11) == 'tag'  # the group's other share stays
        assert await w.database.fetchval('SELECT count(*) FROM group_members WHERE group_id = $1', red) == 1

    world(scenario)


def test_deleting_a_group_removes_its_members_access_for_good(world):
    async def scenario(w):
        await w.dataset(10)
        red = await w.group('red', POWER)
        await w.share_with_group(10, 'red', 'edit')
        deleted = await w.call(ADMIN, 'DELETE', f'/api/groups/{red}')
        assert deleted.is_success, deleted.text
        assert await w.level(POWER, 10) is None
        assert await w.database.fetchval('SELECT count(*) FROM groups WHERE id = $1', red) == 0
        assert await w.database.fetchval('SELECT count(*) FROM group_members WHERE group_id = $1', red) == 0
        assert await w.database.fetchval('SELECT count(*) FROM dataset_group_access WHERE group_id = $1', red) == 0
        await w.group('red', POWER)  # a new group of the same name has none of the old one's shares
        assert await w.level(POWER, 10) is None

    world(scenario)


def test_a_deleted_and_recreated_user_has_none_of_the_old_access(world):
    async def scenario(w):
        await w.dataset(10)
        await w.dataset(11)
        await w.share(10, POWER, 'edit')
        await w.group('red', POWER)
        await w.share_with_group(11, 'red', 'tag')
        assert await w.listed(POWER) == {10: 'edit', 11: 'tag'}
        deleted = await w.call(ADMIN, 'DELETE', f'/api/users/{POWER}')
        assert deleted.status_code == 200, deleted.text
        await w.user(POWER, 'power_user')  # the same id and username again
        assert await w.level(POWER, 10) is None
        assert await w.level(POWER, 11) is None
        assert await w.listed(POWER) == {}

    world(scenario)


def test_making_a_dataset_private_ends_the_everyone_level(world):
    async def scenario(w):
        await w.dataset(10, is_global=True, everyone='tag')
        await w.share(10, POWER, 'view')
        assert await w.level(OTHER_POWER, 10) == 'tag'
        closed = await w.call(ADMIN, 'PATCH', '/api/datasets/10', json={'is_global': False})
        assert closed.status_code == 200, closed.text
        assert await w.level(OTHER_POWER, 10) is None
        assert await w.level(POWER, 10) == 'view'

    world(scenario)


# --- What dataset responses carry ---


def test_list_and_get_carry_access_level_and_everyone_level(world):
    async def scenario(w):
        await w.dataset(10, is_global=True, everyone='tag')
        await w.share(10, POWER, 'manage')
        await w.dataset(11)  # everyone_level not given: view
        await w.share(11, POWER, 'view')
        for user_id, dataset_id, access_level, everyone_level in (
            (POWER, 10, 'manage', 'tag'),
            (POWER, 11, 'view', 'view'),
            (OTHER_POWER, 10, 'tag', 'tag'),
            (GUEST, 10, 'view', 'tag'),
            (ADMIN, 11, 'manage', 'view'),
        ):
            got = (await w.call(user_id, 'GET', f'/api/datasets/{dataset_id}')).json()
            listed = (await w.call(user_id, 'GET', '/api/datasets')).json()
            entry = next(row for row in listed if row['id'] == dataset_id)
            for body in (got, entry):
                assert body['access_level'] == access_level, (user_id, dataset_id)
                assert level_name(body['everyone_level']) == everyone_level, (user_id, dataset_id)

    world(scenario)


# --- Sharing endpoints (admin only in this step) ---


def test_sharing_with_a_user_defaults_to_edit_and_changes_level(world):
    async def scenario(w):
        await w.dataset(10)
        stored = 'SELECT level FROM dataset_access WHERE dataset_id = 10 AND user_id = $1'
        shared = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json={'user_id': POWER})
        assert shared.is_success, shared.text
        assert await w.database.fetchval(stored, POWER) == 3
        assert await w.level(POWER, 10) == 'edit'
        for level in ('view', 'manage', 'tag'):
            changed = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json={'user_id': POWER, 'level': level})
            assert changed.is_success, changed.text
            assert await w.database.fetchval(stored, POWER) == number(level)
            assert await w.level(POWER, 10) == level
        shares = 'SELECT count(*) FROM dataset_access WHERE dataset_id = 10 AND user_id = $1'
        assert await w.database.fetchval(shares, POWER) == 1
        guest = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json={'user_id': GUEST, 'level': 'manage'})
        assert guest.is_success, guest.text
        assert await w.database.fetchval(stored, GUEST) == 4
        assert await w.level(GUEST, 10) == 'view'

    world(scenario)


def test_sharing_with_a_group_defaults_to_edit_and_changes_level(world):
    async def scenario(w):
        await w.dataset(10)
        red = await w.group('red', POWER)
        stored = 'SELECT level FROM dataset_group_access WHERE dataset_id = 10 AND group_id = $1'
        await w.share_with_group(10, 'red')
        assert await w.database.fetchval(stored, red) == 3
        assert await w.level(POWER, 10) == 'edit'
        for level in ('view', 'manage', 'tag'):
            await w.share_with_group(10, 'red', level)
            assert await w.database.fetchval(stored, red) == number(level)
            assert await w.level(POWER, 10) == level
        shares = 'SELECT count(*) FROM dataset_group_access WHERE dataset_id = 10 AND group_id = $1'
        assert await w.database.fetchval(shares, red) == 1

    world(scenario)


def test_get_access_lists_user_and_group_grants_with_levels_and_the_everyone_level(world):
    async def scenario(w):
        await w.dataset(10, is_global=True, everyone='tag')
        red = await w.group('red', OTHER_POWER)
        for body in ({'user_id': POWER, 'level': 'view'}, {'user_id': GUEST}):
            shared = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json=body)
            assert shared.is_success, shared.text
        await w.share_with_group(10, 'red', 'manage')
        response = await w.call(ADMIN, 'GET', '/api/datasets/10/access')
        assert response.status_code == 200, response.text
        body = response.json()
        users = {grant['user_id']: level_name(grant['level']) for grant in body['grants'] if grant.get('user_id')}
        assert users == {POWER: 'view', GUEST: 'edit'}
        assert group_grant_levels(body, red, 'red') == ['manage']
        assert level_name(body['everyone_level']) == 'tag'

    world(scenario)


def test_a_bad_level_is_refused_and_changes_nothing(world):
    async def scenario(w):
        await w.dataset(10)
        await w.share(10, POWER, 'view')
        red = await w.group('red')
        await w.share_with_group(10, 'red', 'view')
        for bad in ('owner', 'admin', 'everything'):
            for body in (
                {'user_id': POWER, 'level': bad},
                {'group': 'red', 'level': bad},
                {'user_id': GUEST, 'level': bad},
            ):
                refused = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json=body)
                assert refused.status_code in (400, 422), (body, refused.text)
        rows = await w.database.fetch('SELECT user_id, level FROM dataset_access WHERE dataset_id = 10')
        assert [(row['user_id'], row['level']) for row in rows] == [(POWER, 1)]
        groups = await w.database.fetch('SELECT group_id, level FROM dataset_group_access WHERE dataset_id = 10')
        assert [(row['group_id'], row['level']) for row in groups] == [(red, 1)]

    world(scenario)


def test_everyone_level_is_set_by_name_and_refuses_manage(world):
    async def scenario(w):
        await w.dataset(10, is_global=True)
        stored = 'SELECT everyone_level FROM datasets WHERE id = 10'
        for level in ('view', 'tag', 'edit'):
            changed = await w.call(ADMIN, 'PATCH', '/api/datasets/10', json={'everyone_level': level})
            assert changed.status_code == 200, changed.text
            assert level_name(changed.json()['everyone_level']) == level
            assert await w.database.fetchval(stored) == number(level)
            assert await w.level(OTHER_POWER, 10) == level
        for bad in ('manage', 'owner'):
            refused = await w.call(ADMIN, 'PATCH', '/api/datasets/10', json={'everyone_level': bad})
            assert refused.status_code in (400, 422), (bad, refused.text)
        assert await w.database.fetchval(stored) == 3
        # Set on a private dataset, it waits for the dataset to be made global.
        await w.dataset(11)
        assert (await w.call(ADMIN, 'PATCH', '/api/datasets/11', json={'everyone_level': 'tag'})).status_code == 200
        assert await w.level(OTHER_POWER, 11) is None
        assert (await w.call(ADMIN, 'PATCH', '/api/datasets/11', json={'is_global': True})).status_code == 200
        assert await w.level(OTHER_POWER, 11) == 'tag'

    world(scenario)


def test_a_dataset_made_global_starts_at_view_unless_a_level_is_named_and_made_global_again_keeps_its_level(world):
    """A dataset created global, or made global by PATCH, gives everyone view unless the request names a level; a
    private dataset holds view (its list says so); one made private and global again keeps the level it had, unless the
    request names another. Shares, and the creator's, are still edit."""

    async def scenario(w):
        stored = 'SELECT is_global, everyone_level FROM datasets WHERE id = $1'

        async def created(body):
            response = await w.call(ADMIN, 'POST', '/api/datasets', json=body)
            assert response.is_success, (body, response.text)
            return response.json()['id']

        async def patched(dataset_id, body):
            response = await w.call(ADMIN, 'PATCH', f'/api/datasets/{dataset_id}', json=body)
            assert response.status_code == 200, (body, response.text)
            return level_name(response.json()['everyone_level'])

        # Created global: view, or the level named.
        made_open = await created({'name': 'made-open', 'is_global': True})
        assert tuple(await w.database.fetchrow(stored, made_open)) == (True, 1)
        assert await w.level(OTHER_POWER, made_open) == 'view'
        made_at_tag = await created({'name': 'made-at-tag', 'is_global': True, 'everyone_level': 'tag'})
        assert tuple(await w.database.fetchrow(stored, made_at_tag)) == (True, 2)
        assert await w.level(OTHER_POWER, made_at_tag) == 'tag'
        # Created private: it holds view, as its access list says, and its creator's share is edit.
        private = await created({'name': 'made-private'})
        assert tuple(await w.database.fetchrow(stored, private)) == (False, 1)
        listed = await w.call(ADMIN, 'GET', f'/api/datasets/{private}/access')
        assert listed.status_code == 200, listed.text
        assert (listed.json()['is_global'], level_name(listed.json()['everyone_level'])) == (False, 'view')
        share = 'SELECT level FROM dataset_access WHERE dataset_id = $1 AND user_id = $2'
        assert await w.database.fetchval(share, private, ADMIN) == 3
        # Made global by PATCH: view; with a level named, that level.
        assert await patched(private, {'is_global': True}) == 'view'
        assert await w.level(OTHER_POWER, private) == 'view'
        named = await created({'name': 'made-private-then-named'})
        assert await patched(named, {'is_global': True, 'everyone_level': 'edit'}) == 'edit'
        assert await w.level(OTHER_POWER, named) == 'edit'
        # Global at edit, private, global again: still edit; unless the request names another level.
        assert await patched(named, {'is_global': False}) == 'edit'
        assert await w.level(OTHER_POWER, named) is None
        assert await patched(named, {'is_global': True}) == 'edit'
        assert await w.level(OTHER_POWER, named) == 'edit'
        await patched(named, {'is_global': False})
        assert await patched(named, {'is_global': True, 'everyone_level': 'tag'}) == 'tag'
        assert await w.level(OTHER_POWER, named) == 'tag'
        # A share written without a level is still edit.
        granted = await w.call(ADMIN, 'POST', f'/api/datasets/{private}/access', json={'user_id': POWER})
        assert granted.is_success, granted.text
        assert await w.database.fetchval(share, private, POWER) == 3

    world(scenario)


def test_sharing_with_no_one_or_someone_unknown_is_refused(world):
    async def scenario(w):
        await w.dataset(10)
        for nobody in ({}, {'level': 'view'}):
            refused = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json=nobody)
            assert refused.status_code == 422, (nobody, refused.text)
        for unknown in ({'user_id': 999999}, {'group': 'no-such-group'}):
            refused = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json=unknown)
            assert refused.status_code == 404, (unknown, refused.text)
        assert await w.database.fetchval('SELECT count(*) FROM dataset_access WHERE dataset_id = 10') == 0
        assert await w.database.fetchval('SELECT count(*) FROM dataset_group_access WHERE dataset_id = 10') == 0
        missing = await w.call(ADMIN, 'POST', '/api/datasets/999999/access', json={'user_id': POWER})
        assert missing.status_code == 404

    world(scenario)


async def snapshot(w: World) -> list[Any]:
    """The rows sharing and groups write."""
    return [
        [tuple(row) for row in await w.database.fetch(query)]
        for query in (
            'SELECT dataset_id, user_id, level FROM dataset_access ORDER BY 1, 2',
            'SELECT id, name FROM groups ORDER BY 1',
            'SELECT group_id, user_id FROM group_members ORDER BY 1, 2',
            'SELECT dataset_id, group_id, level FROM dataset_group_access ORDER BY 1, 2',
            'SELECT id, is_global, everyone_level FROM datasets ORDER BY 1',
        )
    ]


def test_non_admins_are_refused_by_the_sharing_and_group_endpoints(world):
    async def scenario(w):
        await w.dataset(10, is_global=True, everyone='view')
        await w.share(10, POWER)  # edit: a non-admin who can open the dataset
        await w.share(10, OTHER_POWER)
        red = await w.group('red', OTHER_POWER)
        await w.share_with_group(10, 'red', 'tag')
        before = await snapshot(w)
        for user_id in (POWER, GUEST, None):
            calls: list[tuple[str, str, Any]] = [
                ('GET', '/api/datasets/10/access', None),
                ('POST', '/api/datasets/10/access', {'user_id': user_id or POWER, 'level': 'manage'}),
                ('POST', '/api/datasets/10/access', {'group': 'red', 'level': 'manage'}),
                ('DELETE', f'/api/datasets/10/access/{OTHER_POWER}', None),
                ('DELETE', f'/api/datasets/10/access/groups/{red}', None),
                ('PATCH', '/api/datasets/10', {'everyone_level': 'edit'}),
                ('GET', '/api/groups', None),
                ('POST', '/api/groups', {'name': 'mine'}),
                ('PATCH', f'/api/groups/{red}', {'name': 'renamed'}),
                ('DELETE', f'/api/groups/{red}', None),
                ('PUT', f'/api/groups/{red}/members/{user_id or POWER}', None),
                ('DELETE', f'/api/groups/{red}/members/{OTHER_POWER}', None),
            ]
            for method, path, body in calls:
                kwargs = {} if body is None else {'json': body}
                response = await w.call(user_id, method, path, **kwargs)
                assert response.status_code == (401 if user_id is None else 403), (user_id, method, path, response.text)
        assert await snapshot(w) == before

    world(scenario)


def test_a_share_naming_both_a_user_and_a_group_is_refused_and_writes_nothing(world):
    """POST /datasets/{id}/access names exactly one target: with both user_id and group it is refused with 422,
    with or without a level, and neither share is created or changed."""

    async def scenario(w):
        await w.dataset(10)  # no shares yet
        await w.dataset(11)  # POWER's own share at view and red's at manage
        await w.group('red', OTHER_POWER)
        await w.share(11, POWER, 'view')
        await w.share_with_group(11, 'red', 'manage')
        before = await snapshot(w)
        for dataset_id in (10, 11):
            for body in (
                {'user_id': POWER, 'group': 'red'},
                {'user_id': POWER, 'group': 'red', 'level': 'tag'},  # would raise POWER's share and lower red's
                {'user_id': POWER, 'group': 'red', 'level': 'edit'},
                {'user_id': POWER, 'group': 'red', 'level': 'manage'},
                {'user_id': 999999, 'group': 'no-such-group'},
            ):
                refused = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/access', json=body)
                assert refused.status_code == 422, (dataset_id, body, refused.text)
        assert await snapshot(w) == before
        assert await w.level(POWER, 10) is None
        assert await w.level(OTHER_POWER, 10) is None
        assert await w.level(POWER, 11) == 'view'
        assert await w.level(OTHER_POWER, 11) == 'manage'

    world(scenario)


def test_sharing_with_a_user_again_without_a_level_keeps_the_level_it_has(world):
    """Without a level a new share is edit and an existing one keeps its level: an old script granting again never
    raises a share lowered to view, nor lowers a manage share. With a level the share is set to it, up or down."""

    async def scenario(w):
        await w.dataset(10)
        stored = 'SELECT level FROM dataset_access WHERE dataset_id = 10 AND user_id = $1'
        await w.share(10, POWER, 'view')  # lowered to view earlier
        managing = await w.call(
            ADMIN,
            'POST',
            '/api/datasets/10/access',
            json={'user_id': OTHER_POWER, 'level': 'manage'},
        )
        assert managing.is_success, managing.text
        for _ in range(2):
            for user_id in (POWER, OTHER_POWER, OTHER_GUEST):
                again = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json={'user_id': user_id})
                assert again.is_success, again.text
            assert await w.database.fetchval(stored, POWER) == 1
            assert await w.database.fetchval(stored, OTHER_POWER) == 4
            assert await w.database.fetchval(stored, OTHER_GUEST) == 3  # new: edit, and kept on the second grant
            assert await w.level(POWER, 10) == 'view'
            assert await w.level(OTHER_POWER, 10) == 'manage'
        for user_id, level in ((POWER, 'manage'), (OTHER_POWER, 'view')):  # up and down
            changed = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json={'user_id': user_id, 'level': level})
            assert changed.is_success, changed.text
            assert await w.database.fetchval(stored, user_id) == number(level)
            assert await w.level(user_id, 10) == level
        rows = 'SELECT count(*) FROM dataset_access WHERE dataset_id = 10'
        assert await w.database.fetchval(rows) == 3

    world(scenario)


def test_sharing_with_a_group_again_without_a_level_keeps_the_level_it_has(world):
    """The same for a group: without a level a new group share is edit and an existing one keeps its level; with a
    level it is set to it, up or down."""

    async def scenario(w):
        await w.dataset(10)
        viewers = await w.group('viewers', POWER)
        managers = await w.group('managers', OTHER_POWER)
        newcomers = await w.group('newcomers', GUEST)
        stored = 'SELECT level FROM dataset_group_access WHERE dataset_id = 10 AND group_id = $1'
        await w.share_with_group(10, 'viewers', 'view')
        await w.share_with_group(10, 'managers', 'manage')
        for _ in range(2):
            for name in ('viewers', 'managers', 'newcomers'):
                await w.share_with_group(10, name)
            assert await w.database.fetchval(stored, viewers) == 1
            assert await w.database.fetchval(stored, managers) == 4
            assert await w.database.fetchval(stored, newcomers) == 3  # new: edit, and kept on the second grant
            assert await w.level(POWER, 10) == 'view'
            assert await w.level(OTHER_POWER, 10) == 'manage'
        for group_id, name, level, member in (
            (viewers, 'viewers', 'manage', POWER),
            (managers, 'managers', 'view', OTHER_POWER),
        ):
            await w.share_with_group(10, name, level)  # up and down
            assert await w.database.fetchval(stored, group_id) == number(level)
            assert await w.level(member, 10) == level
        rows = 'SELECT count(*) FROM dataset_group_access WHERE dataset_id = 10'
        assert await w.database.fetchval(rows) == 3

    world(scenario)


def test_a_grant_that_changes_the_level_records_who_changed_it(world):
    """granted_by_user_id names who set the share's level: granting again with another level makes it the caller; a
    grant that changes nothing (the same level, no level, a null level) leaves it. For a user's share and a group's."""

    async def scenario(w):
        await w.dataset(10)
        red = await w.group('red', OTHER_POWER)
        user_row = 'SELECT level, granted_by_user_id FROM dataset_access WHERE dataset_id = 10 AND user_id = $1'
        group_row = 'SELECT level, granted_by_user_id FROM dataset_group_access WHERE dataset_id = 10 AND group_id = $1'
        for target, query, key in (({'user_id': POWER}, user_row, POWER), ({'group': 'red'}, group_row, red)):
            for caller, given, wanted in (
                (ADMIN, {'level': 'view'}, (1, ADMIN)),  # a new share
                (OTHER_ADMIN, {'level': 'view'}, (1, ADMIN)),  # the same level: nothing changes
                (OTHER_ADMIN, {}, (1, ADMIN)),  # no level: the level stays, and who set it
                (OTHER_ADMIN, {'level': None}, (1, ADMIN)),
                (OTHER_ADMIN, {'level': 'edit'}, (3, OTHER_ADMIN)),  # changed: the caller set it
                (ADMIN, {'level': 'edit'}, (3, OTHER_ADMIN)),
                (ADMIN, {'level': 'tag'}, (2, ADMIN)),
            ):
                granted = await w.call(caller, 'POST', '/api/datasets/10/access', json={**target, **given})
                assert granted.is_success, (target, given, granted.text)
                assert tuple(await w.database.fetchrow(query, key)) == wanted, (target, caller, given)

    world(scenario)


def test_an_explicit_null_reads_as_the_key_left_out(world):
    """user_id, group or level sent as null is as if it weren't sent: one target is still needed, and a null level
    is no level (a new share edit, an existing one unchanged)."""

    async def scenario(w):
        await w.dataset(10)
        red = await w.group('red', OTHER_POWER)
        stored = 'SELECT level FROM dataset_access WHERE dataset_id = 10 AND user_id = $1'
        group_stored = 'SELECT level FROM dataset_group_access WHERE dataset_id = 10 AND group_id = $1'
        for nobody in ({'user_id': None, 'group': None}, {'user_id': None, 'group': None, 'level': 'view'}):
            refused = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json=nobody)
            assert refused.status_code == 422, (nobody, refused.text)
        assert await w.database.fetchval('SELECT count(*) FROM dataset_access WHERE dataset_id = 10') == 0
        assert await w.database.fetchval('SELECT count(*) FROM dataset_group_access WHERE dataset_id = 10') == 0
        new = await w.call(
            ADMIN,
            'POST',
            '/api/datasets/10/access',
            json={'user_id': POWER, 'group': None, 'level': None},
        )
        assert new.is_success, new.text
        assert await w.database.fetchval(stored, POWER) == 3
        lowered = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json={'user_id': POWER, 'level': 'view'})
        assert lowered.is_success, lowered.text
        for body in ({'user_id': POWER, 'level': None}, {'user_id': POWER, 'group': None, 'level': None}):
            again = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json=body)
            assert again.is_success, (body, again.text)
            assert await w.database.fetchval(stored, POWER) == 1, body
        new = await w.call(ADMIN, 'POST', '/api/datasets/10/access', json={'group': 'red', 'user_id': None})
        assert new.is_success, new.text
        assert await w.database.fetchval(group_stored, red) == 3
        await w.share_with_group(10, 'red', 'tag')
        again = await w.call(
            ADMIN,
            'POST',
            '/api/datasets/10/access',
            json={'group': 'red', 'user_id': None, 'level': None},
        )
        assert again.is_success, again.text
        assert await w.database.fetchval(group_stored, red) == 2
        assert await w.level(POWER, 10) == 'view'
        assert await w.level(OTHER_POWER, 10) == 'tag'

    world(scenario)


# --- Groups endpoints (admin only) ---


def test_an_admin_creates_lists_renames_fills_and_deletes_a_group(world):
    async def scenario(w):
        await w.dataset(10)
        red = await w.group('red')
        listed = await w.call(ADMIN, 'GET', '/api/groups')
        assert listed.status_code == 200, listed.text
        assert 'red' in names_in(listed.json())
        for _ in range(2):  # adding a member twice leaves one membership
            added = await w.call(ADMIN, 'PUT', f'/api/groups/{red}/members/{POWER}')
            assert added.is_success, added.text
        members = 'SELECT user_id FROM group_members WHERE group_id = $1'
        assert [row['user_id'] for row in await w.database.fetch(members, red)] == [POWER]
        await w.share_with_group(10, 'red', 'tag')
        renamed = await w.call(ADMIN, 'PATCH', f'/api/groups/{red}', json={'name': 'crimson'})
        assert renamed.is_success, renamed.text
        assert await w.database.fetchval('SELECT name FROM groups WHERE id = $1', red) == 'crimson'
        names = names_in((await w.call(ADMIN, 'GET', '/api/groups')).json())
        assert 'crimson' in names and 'red' not in names
        assert await w.level(POWER, 10) == 'tag'  # a renamed group keeps its members and shares
        removed = await w.call(ADMIN, 'DELETE', f'/api/groups/{red}/members/{POWER}')
        assert removed.is_success, removed.text
        assert await w.database.fetch(members, red) == []
        deleted = await w.call(ADMIN, 'DELETE', f'/api/groups/{red}')
        assert deleted.is_success, deleted.text
        assert await w.database.fetchval('SELECT count(*) FROM groups') == 0

    world(scenario)


def test_group_names_are_unique(world):
    async def scenario(w):
        await w.group('red')
        blue = await w.group('blue')
        again = await w.call(ADMIN, 'POST', '/api/groups', json={'name': 'red'})
        assert 400 <= again.status_code < 500, again.text
        renamed = await w.call(ADMIN, 'PATCH', f'/api/groups/{blue}', json={'name': 'red'})
        assert 400 <= renamed.status_code < 500, renamed.text
        rows = await w.database.fetch('SELECT name FROM groups ORDER BY name')
        assert [row['name'] for row in rows] == ['blue', 'red']

    world(scenario)


# --- Today's rows behave as today ---


def test_with_only_todays_rows_each_role_sees_and_does_what_it_did_before(world):
    """Plain shares (no level) and global datasets at the everyone level the upgrade gives them (edit): what each role
    saw and could do before levels, plus the display settings, which were an admin's and are edit now (one of the six
    actions power users gain)."""

    async def scenario(w):
        await w.dataset(10, is_global=True, everyone='edit')
        await w.dataset(11)
        await w.share(11, POWER)
        await w.share(11, GUEST)
        await w.dataset(12)
        visible = {
            ADMIN: {1, 2, 10, 11, 12},
            POWER: {10, 11},
            GUEST: {10, 11},
            OTHER_POWER: {10},
            OTHER_GUEST: {10},
        }
        samples = {dataset_id: (await ingest([{}], dataset_id=dataset_id))['ids'][0] for dataset_id in (10, 11, 12)}
        for user_id, expected in visible.items():
            assert await w.visible(user_id) == expected, user_id
            for dataset_id in (10, 11, 12):
                opened = await w.call(user_id, 'GET', f'/api/datasets/{dataset_id}')
                assert opened.status_code == (200 if dataset_id in expected else 403), (user_id, dataset_id)
                # Writes go by role: admins and power users write in what they can open; guests don't write.
                tagged = await w.call(
                    user_id,
                    'PATCH',
                    f'/api/datasets/{dataset_id}/samples/{samples[dataset_id]}',
                    json={'tags': ['seen']},
                )
                writes = dataset_id in expected and ROLES[user_id] != 'guest'
                assert tagged.status_code == (200 if writes else 403), (user_id, dataset_id, tagged.text)
        # A plain share is edit: the display settings are open to it; who has access (manage) is not.
        for user_id, described_status, access_status in ((POWER, 200, 403), (GUEST, 403, 403), (ADMIN, 200, 200)):
            described = await w.call(user_id, 'PATCH', '/api/datasets/11', json={'description': 'notes'})
            assert described.status_code == described_status, (user_id, described.text)
            access = await w.call(user_id, 'GET', '/api/datasets/11/access')
            assert access.status_code == access_status, (user_id, access.text)
        grants = (await w.call(ADMIN, 'GET', '/api/datasets/11/access')).json()['grants']
        assert {grant['user_id'] for grant in grants if grant.get('user_id')} == {POWER, GUEST}

    world(scenario)
