"""Access levels (backend/access.py): a user's level in a dataset is the highest of their own share, their groups'
shares and, for a global dataset, its everyone level; a guest views at most, an admin manages every dataset. A
dataset's shares and everyone level are managed by those with manage in it (the sharing endpoints), groups by admins
(the groups endpoints), and a dataset's creator gets an edit share."""

import os
from typing import Any, Optional
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request

from backend import access
from backend.routers import auth, datasets, users
from backend.tests.test_field_registry_postgres import USER, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

OWN, GROUP, TWO_GROUPS, EVERYONE, BELOW_EVERYONE, GUEST, NOBODY = 10, 11, 12, 13, 14, 15, 16
ROLES = {OWN: 'power_user', GROUP: 'power_user', TWO_GROUPS: 'power_user', EVERYONE: 'power_user'}
ROLES.update({BELOW_EVERYONE: 'power_user', GUEST: 'guest', NOBODY: 'power_user'})
PRIVATE, OPEN = 1, 2  # the fixture's datasets: 1 made private below, 2 global with its everyone level lowered


def who(user_id):
    return {'id': user_id, 'role': ROLES.get(user_id, 'admin')}


async def seed(database):
    """Private dataset 1: an own share (edit), a group's (tag), two groups' (view and manage), a guest's (edit).
    Global dataset 2, everyone level tag: an own share below it (view)."""
    await database.execute(
        'INSERT INTO users (id, username, role) VALUES '
        + ', '.join(f"({user_id}, 'user-{user_id}', '{role}')" for user_id, role in ROLES.items()),
    )
    await database.execute('UPDATE datasets SET is_global = FALSE WHERE id = 1')
    await database.execute('UPDATE datasets SET everyone_level = 2 WHERE id = 2')
    await database.execute("INSERT INTO groups (id, name) VALUES (1, 'taggers'), (2, 'viewers'), (3, 'managers')")
    await database.execute("SELECT setval('groups_id_seq', 3)")
    await database.execute(
        f'INSERT INTO group_members (group_id, user_id) VALUES (1, {GROUP}), (2, {TWO_GROUPS}), (3, {TWO_GROUPS})',
    )
    await database.execute(
        'INSERT INTO dataset_group_access (dataset_id, group_id, level) VALUES (1, 1, 2), (1, 2, 1), (1, 3, 4)',
    )
    await database.execute(
        f'INSERT INTO dataset_access (dataset_id, user_id, level) VALUES (1, {OWN}, 3), (1, {GUEST}, 3), '
        f'(2, {BELOW_EVERYONE}, 1)',
    )


async def level(dataset_id, user):
    row = await access.check_dataset_access(dataset_id, user)
    found = await access.effective_level(row, user)
    return None if found is None else access.level_name(found)


def test_level_names_and_numbers():
    assert [access.level_number(name) for name in access.LEVELS] == [1, 2, 3, 4]
    assert [access.level_name(number) for number in (1, 2, 3, 4)] == ['view', 'tag', 'edit', 'manage']


def test_effective_level_is_the_highest_share_capped_for_guests(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(access, 'db', database)
        await seed(database)
        assert await level(PRIVATE, who(OWN)) == 'edit'  # own share
        assert await level(PRIVATE, who(GROUP)) == 'tag'  # a group's share
        assert await level(PRIVATE, who(TWO_GROUPS)) == 'manage'  # two groups: the highest
        assert await level(PRIVATE, who(GUEST)) == 'view'  # an edit share, capped for a guest
        assert await level(PRIVATE, USER) == 'manage'  # admin
        assert await level(OPEN, who(EVERYONE)) == 'tag'  # the everyone level
        assert await level(OPEN, who(BELOW_EVERYONE)) == 'tag'  # own share below it: the highest
        assert await level(OPEN, who(GUEST)) == 'view'
        await database.execute(f'INSERT INTO dataset_access (dataset_id, user_id, level) VALUES (2, {OWN}, 4)')
        assert await level(OPEN, who(OWN)) == 'manage'  # own share above it

        for dataset_id, user, status, detail in (
            (PRIVATE, who(NOBODY), 403, 'Access denied for this dataset'),
            (PRIVATE, None, 401, 'Authentication required'),
            (999, who(OWN), 404, 'Dataset not found'),
        ):
            with pytest.raises(HTTPException) as refused:
                await access.check_dataset_access(dataset_id, user)
            assert (refused.value.status_code, refused.value.detail) == (status, detail)

        # Below the level asked for: 403 naming it. At or above: the row.
        below: list[tuple[int, int, access.Level]] = [
            (PRIVATE, GROUP, 'edit'),
            (PRIVATE, OWN, 'manage'),
            (PRIVATE, GUEST, 'tag'),
            (OPEN, EVERYONE, 'edit'),
            (OPEN, GUEST, 'tag'),
        ]
        for dataset_id, user_id, needed in below:
            with pytest.raises(HTTPException) as refused:
                await access.check_dataset_access(dataset_id, who(user_id), needed)
            assert refused.value.status_code == 403
            assert refused.value.detail == f'This needs {needed} access to the dataset'
        assert (await access.check_dataset_access(PRIVATE, who(GROUP), 'tag'))['id'] == PRIVATE
        assert (await access.check_dataset_access(PRIVATE, who(TWO_GROUPS), 'manage'))['id'] == PRIVATE
        assert (await access.check_dataset_access(OPEN, who(BELOW_EVERYONE), 'tag'))['id'] == OPEN

    registry_database(scenario)


def test_a_group_share_lasts_while_the_group_and_the_membership_do(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(access, 'db', database)
        monkeypatch.setattr(datasets, 'get_optional_user', AsyncMock(return_value=who(GROUP)))
        await seed(database)
        listed = await datasets.list_datasets(Request({'type': 'http'}))
        assert [(dataset['id'], dataset['access_level']) for dataset in listed] == [(1, 'tag'), (2, 'tag')]
        await database.execute(f'DELETE FROM group_members WHERE user_id = {GROUP}')  # removed from the group
        assert [dataset['id'] for dataset in await datasets.list_datasets(Request({'type': 'http'}))] == [2]
        with pytest.raises(HTTPException) as refused:
            await access.check_dataset_access(PRIVATE, who(GROUP))
        assert refused.value.status_code == 403

        assert await level(PRIVATE, who(TWO_GROUPS)) == 'manage'
        await database.execute('DELETE FROM groups WHERE id = 3')  # the managers group is deleted
        assert await level(PRIVATE, who(TWO_GROUPS)) == 'view'
        await database.execute('DELETE FROM groups WHERE id = 2')
        with pytest.raises(HTTPException) as refused:
            await access.check_dataset_access(PRIVATE, who(TWO_GROUPS))
        assert refused.value.status_code == 403
        assert await database.fetchval('SELECT count(*) FROM dataset_group_access') == 1  # the shares went too

    registry_database(scenario)


def test_the_dataset_list_and_get_give_each_callers_level_in_one_query(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(access, 'db', database)
        await seed(database)
        await database.execute("INSERT INTO datasets (id, name) VALUES (3, 'unshared')")
        for user, expected in (
            (USER, [(1, 'manage'), (2, 'manage'), (3, 'manage')]),
            (who(OWN), [(1, 'edit'), (2, 'tag')]),
            (who(TWO_GROUPS), [(1, 'manage'), (2, 'tag')]),
            (who(GUEST), [(1, 'view'), (2, 'view')]),
            (who(NOBODY), [(2, 'tag')]),
            (None, []),
        ):
            monkeypatch.setattr(datasets, 'get_optional_user', AsyncMock(return_value=user))
            calls = []
            fetch = database.fetch

            async def counted(query, *args):
                calls.append(query)
                return await fetch(query, *args)

            monkeypatch.setattr(database, 'fetch', counted)
            listed = await datasets.list_datasets(Request({'type': 'http'}))
            monkeypatch.setattr(database, 'fetch', fetch)
            assert len(calls) == 1  # no query per dataset
            assert [(dataset['id'], dataset['access_level']) for dataset in listed] == expected, user
            # Each dataset's stored everyone level: the fixture's (edit), the one set (tag), the default (view).
            everyone = {1: 'edit', 2: 'tag', 3: 'view'}
            assert {dataset['id']: dataset['everyone_level'] for dataset in listed} == {
                dataset_id: everyone[dataset_id] for dataset_id, _ in expected
            }
            for dataset_id, name in expected:
                got = await datasets.get_dataset(dataset_id, Request({'type': 'http'}))
                assert (got['access_level'], got['everyone_level']) == (name, everyone[dataset_id])

    registry_database(scenario)


def test_require_level_answers_from_one_dataset_lookup(registry_database, monkeypatch):
    async def scenario(database):
        await seed(database)
        lookups = {'datasets': 0, 'shares': 0}

        class Counted:
            async def fetchrow(self, query, *args):
                lookups['datasets'] += 'FROM datasets' in query
                return await database.fetchrow(query, *args)

            async def fetchval(self, query, *args):
                lookups['shares'] += 'dataset_access' in query
                return await database.fetchval(query, *args)

        monkeypatch.setattr(access, 'db', Counted())
        caller: dict[str, Optional[dict]] = {}
        monkeypatch.setattr(auth, 'get_optional_user', AsyncMock(side_effect=lambda request: caller.get('user')))
        app = FastAPI()
        app.dependency_overrides[auth.get_current_user] = lambda: caller['user']

        @app.post('/api/datasets/{id}/edit', dependencies=[Depends(access.require_dataset_access)])
        async def edit(id: int, user: dict = Depends(access.require_level('edit'))):
            return {'user': user['id']}

        @app.post('/api/datasets/{dataset_id}/tag')
        async def tag(dataset_id: int, user: dict = Depends(access.require_level('tag'))):
            return {'user': user['id']}

        with pytest.raises(ValueError):
            access.require_level('own')  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for user, path, status, detail, queries in (
                (who(OWN), '1/edit', 200, None, {'datasets': 1, 'shares': 1}),
                (who(GROUP), '1/edit', 403, 'This needs edit access to the dataset', {'datasets': 1, 'shares': 1}),
                (who(GROUP), '1/tag', 200, None, {'datasets': 1, 'shares': 1}),
                (who(NOBODY), '1/edit', 403, 'Access denied for this dataset', {'datasets': 1, 'shares': 1}),
                (who(GUEST), '1/tag', 403, 'This needs tag access to the dataset', {'datasets': 1, 'shares': 1}),
                (USER, '1/edit', 200, None, {'datasets': 1, 'shares': 0}),
                # The global dataset's everyone level (tag) answers a view or tag check with no shares query; an edit
                # check needs one.
                (who(EVERYONE), '2/tag', 200, None, {'datasets': 1, 'shares': 0}),
                (who(EVERYONE), '2/edit', 403, 'This needs edit access to the dataset', {'datasets': 1, 'shares': 1}),
                (who(OWN), '999/edit', 404, 'Dataset not found', {'datasets': 1, 'shares': 0}),
            ):
                caller['user'] = user
                lookups.update(datasets=0, shares=0)
                response = await client.post(f'/api/datasets/{path}')
                assert response.status_code == status, (user, path, response.text)
                if detail:
                    assert response.json()['detail'] == detail
                else:
                    assert response.json() == {'user': user['id']}
                assert lookups == queries, (user, path)
            caller['user'] = None
            response = await client.post('/api/datasets/1/edit')
            assert response.status_code == 401

    registry_database(scenario)


def test_sharing_endpoints_take_levels_and_groups(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(access, 'db', database)
        await seed(database)
        app = FastAPI()
        caller = {'user': USER}
        app.dependency_overrides[auth.get_current_user] = lambda: caller['user']
        app.include_router(datasets.router, prefix='/api')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:

            async def grant(body, status=201):
                response = await client.post('/api/datasets/1/access', json=body)
                assert response.status_code == status, response.text
                return response.json()

            async def listing():
                response = await client.get('/api/datasets/1/access')
                assert response.status_code == 200
                return response.json()

            # Without a level: a new share is edit, as before, and the answer keeps its keys.
            assert await grant({'user_id': NOBODY}) == {'dataset_id': 1, 'user_id': NOBODY, 'level': 'edit'}
            # With one: set, and set again on a re-grant; a re-grant without one leaves it.
            assert (await grant({'user_id': NOBODY, 'level': 'view'}))['level'] == 'view'
            assert (await grant({'user_id': NOBODY}))['level'] == 'view'
            assert (await grant({'user_id': NOBODY, 'level': 'manage'}))['level'] == 'manage'
            grants = {row['user_id']: row for row in (await listing())['grants']}
            assert grants[NOBODY]['level'] == 'manage' and grants[NOBODY]['username'] == f'user-{NOBODY}'
            assert grants[NOBODY]['granted_by_user_id'] == USER['id']
            assert grants[OWN]['level'] == 'edit'

            # A group, by its name.
            assert await grant({'group': 'taggers', 'level': 'edit'}) == {
                'dataset_id': 1,
                'group_id': 1,
                'group': 'taggers',
                'level': 'edit',
            }
            await database.execute("INSERT INTO groups (id, name) VALUES (4, 'new')")
            assert (await grant({'group': 'new'}))['level'] == 'edit'
            listed = await listing()
            assert {row['name']: row['level'] for row in listed['group_grants']} == {
                'managers': 'manage',
                'new': 'edit',
                'taggers': 'edit',
                'viewers': 'view',
            }
            assert (listed['is_global'], listed['everyone_level']) == (False, 'edit')
            assert (await client.delete('/api/datasets/1/access/groups/4')).status_code == 204
            assert 'new' not in {row['name'] for row in (await listing())['group_grants']}
            assert (await client.delete(f'/api/datasets/1/access/{NOBODY}')).status_code == 204
            assert NOBODY not in {row['user_id'] for row in (await listing())['grants']}

            for body, status in (
                ({'user_id': NOBODY, 'level': 'owner'}, 422),
                ({'user_id': NOBODY, 'group': 'taggers'}, 422),
                ({}, 422),
                ({'group': 'nobody-made-it'}, 404),
                ({'user_id': 9999}, 404),
            ):
                await grant(body, status)
            response = await client.post('/api/datasets/999/access', json={'user_id': OWN})
            assert response.status_code == 404

            # The everyone level: view, tag or edit, not manage.
            response = await client.patch('/api/datasets/1', json={'everyone_level': 'view'})
            assert response.status_code == 200 and response.json()['everyone_level'] == 'view'
            assert (await listing())['everyone_level'] == 'view'
            assert (await client.patch('/api/datasets/1', json={'everyone_level': 'manage'})).status_code == 422
            assert await database.fetchval('SELECT everyone_level FROM datasets WHERE id = 1') == 1

            # Manage in the dataset, not the admin role: an edit share (or a guest's, at view) is refused with the
            # manage message and changes nothing; a manager through a group's share may.
            shares = 'SELECT user_id, level FROM dataset_access WHERE dataset_id = 1 ORDER BY user_id'
            group_shares = 'SELECT group_id, level FROM dataset_group_access WHERE dataset_id = 1 ORDER BY group_id'

            async def rows():
                return [[tuple(row) for row in await database.fetch(query)] for query in (shares, group_shares)]

            before = await rows()
            calls: list[tuple[str, str, Any]] = [
                ('GET', '/api/datasets/1/access', None),
                ('POST', '/api/datasets/1/access', {'user_id': NOBODY}),
                ('DELETE', '/api/datasets/1/access/groups/1', None),
                ('PATCH', '/api/datasets/1', {'everyone_level': 'edit'}),
            ]
            for user_id in (OWN, GUEST):
                caller['user'] = who(user_id)
                for method, path, body in calls:
                    kwargs: dict[str, Any] = {} if body is None else {'json': body}
                    response = await client.request(method, path, **kwargs)
                    assert (response.status_code, response.json()) == (
                        403,
                        {'detail': 'This needs manage access to the dataset'},
                    ), (user_id, method, path)
            assert await rows() == before
            assert await database.fetchval('SELECT everyone_level FROM datasets WHERE id = 1') == 1
            caller['user'] = who(TWO_GROUPS)  # manage through the managers group
            assert (await client.get('/api/datasets/1/access')).status_code == 200
            assert (await grant({'user_id': NOBODY}))['level'] == 'edit'
            assert (await client.delete('/api/datasets/1/access/groups/1')).status_code == 204
            response = await client.patch('/api/datasets/1', json={'everyone_level': 'edit'})
            assert response.status_code == 200 and response.json()['everyone_level'] == 'edit'
            assert 'taggers' not in {row['name'] for row in (await listing())['group_grants']}

    registry_database(scenario)


def test_groups_endpoints(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(users, 'db', database)
        await seed(database)
        app = FastAPI()
        caller = {'user': USER}
        app.dependency_overrides[auth.get_current_user] = lambda: caller['user']
        app.include_router(users.router, prefix='/api')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/groups', json={'name': '  team two  '})
            assert response.status_code == 201
            made = response.json()
            assert made['name'] == 'team two'
            assert (await client.post('/api/groups', json={'name': 'team two'})).status_code == 400
            assert (await client.post('/api/groups', json={'name': '   '})).status_code == 400
            # Unique as written, as usernames are.
            assert (await client.post('/api/groups', json={'name': 'Team Two'})).status_code == 201

            group = made['id']
            for user_id in (OWN, GUEST, OWN):  # adding twice is fine
                assert (await client.put(f'/api/groups/{group}/members/{user_id}')).status_code == 204
            assert (await client.put(f'/api/groups/{group}/members/9999')).status_code == 404
            assert (await client.put(f'/api/groups/9999/members/{OWN}')).status_code == 404
            listed = {row['name']: row for row in (await client.get('/api/groups')).json()}
            assert [member['id'] for member in listed['team two']['members']] == [OWN, GUEST]
            assert listed['team two']['created_by'] == USER['id']
            assert listed['Team Two']['members'] == []
            assert [member['username'] for member in listed['managers']['members']] == [f'user-{TWO_GROUPS}']

            assert (await client.delete(f'/api/groups/{group}/members/{GUEST}')).status_code == 204
            response = await client.patch(f'/api/groups/{group}', json={'name': 'team 2'})
            assert response.json() == {'id': group, 'name': 'team 2'}
            assert (await client.patch(f'/api/groups/{group}', json={'name': 'taggers'})).status_code == 400
            assert (await client.patch('/api/groups/9999', json={'name': 'x'})).status_code == 404
            listed = {row['name']: row for row in (await client.get('/api/groups')).json()}
            assert [member['id'] for member in listed['team 2']['members']] == [OWN]

            # Deleting a user or a group takes the memberships (and a group's shares) with it.
            await database.execute(f'DELETE FROM users WHERE id = {GROUP}')
            assert await database.fetchval('SELECT count(*) FROM group_members WHERE group_id = 1') == 0
            assert (await client.delete('/api/groups/3')).status_code == 204
            assert (await client.delete('/api/groups/3')).status_code == 404
            assert await database.fetchval('SELECT count(*) FROM group_members WHERE group_id = 3') == 0
            assert await database.fetchval('SELECT count(*) FROM dataset_group_access WHERE group_id = 3') == 0

            caller['user'] = who(OWN)
            for method, path in (
                ('GET', '/api/groups'),
                ('POST', '/api/groups'),
                ('PATCH', f'/api/groups/{group}'),
                ('DELETE', f'/api/groups/{group}'),
                ('PUT', f'/api/groups/{group}/members/{OWN}'),
                ('DELETE', f'/api/groups/{group}/members/{OWN}'),
            ):
                response = await client.request(method, path, json={'name': 'x'})
                assert response.status_code == 403, (method, path)

    registry_database(scenario)


def test_a_new_dataset_is_its_creators_and_stays_theirs_without_admin(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(access, 'db', database)
        await database.execute("SELECT setval('datasets_id_seq', 2)")  # the fixture seeds ids 1 and 2
        await database.execute("INSERT INTO users (id, username, role) VALUES (20, 'maker', 'admin')")
        created = await datasets.create_dataset(datasets.DatasetCreate(name='fresh'), {'id': 20, 'role': 'admin'})
        assert (created['owner_id'], created['everyone_level']) == (20, 'view')  # private, at where it'd start
        share = await database.fetchrow('SELECT level, granted_by_user_id FROM dataset_access WHERE user_id = 20')
        assert dict(share) == {'level': 3, 'granted_by_user_id': None}
        demoted = {'id': 20, 'role': 'power_user'}
        assert await level(created['id'], demoted) == 'edit'
        monkeypatch.setattr(datasets, 'get_optional_user', AsyncMock(return_value=demoted))
        listed = await datasets.list_datasets(Request({'type': 'http'}))
        assert [(dataset['id'], dataset['access_level']) for dataset in listed] == [
            (1, 'edit'),
            (2, 'edit'),
            (created['id'], 'edit'),
        ]
        # owner_id gives no access by itself: revoking the creator's share takes theirs away.
        await database.execute('DELETE FROM dataset_access WHERE user_id = 20')
        with pytest.raises(HTTPException) as refused:
            await access.check_dataset_access(created['id'], demoted)
        assert refused.value.status_code == 403

    registry_database(scenario)
