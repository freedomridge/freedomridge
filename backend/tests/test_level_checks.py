"""The helpers that check a caller's level (backend/access.py) and the level each request needs where it depends on
the request: its body (a sample's tags, bulk tags, a dataset's settings), a view's owner, a job's kind and starter."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request

from backend import access
from backend.routers import datasets, jobs, views, workers
from backend.tests.test_field_registry_postgres import USER, registry_database

__all__ = ['registry_database']

POWER = {'id': 7, 'role': 'power_user'}


def run(coroutine):
    return asyncio.run(coroutine)


def test_a_level_refusal_is_not_hidden_as_a_missing_row():
    refusal = access.level_refusal('edit')
    assert (refusal.status_code, refusal.detail) == (403, 'This needs edit access to the dataset')
    with pytest.raises(access.LevelRefused):
        with access.missing_unless_allowed('View not found'):
            raise refusal
    for status in (403, 404):  # no access to the dataset, or it is gone: as a missing row
        with pytest.raises(HTTPException) as error:
            with access.missing_unless_allowed('View not found'):
                raise HTTPException(status_code=status, detail='Access denied for this dataset')
        assert (error.value.status_code, error.value.detail) == (404, 'View not found')


def test_checks_level_marks_the_function_it_returns():
    async def handler():
        pass

    assert access.checks_level('tag', while_deleting='why')(handler) is handler
    assert (handler.__fridge_level__, handler.__fridge_while_deleting__) == ('tag', 'why')  # type: ignore[attr-defined]


def _request(method: str) -> Request:
    return Request({'type': 'http', 'method': method, 'path_params': {'id': '1'}, 'endpoint': None})


def test_require_level_refuses_a_write_to_a_dataset_being_deleted(monkeypatch):
    asked = []

    async def check(dataset_id, user, level, state):
        asked.append((dataset_id, level))
        return {'id': dataset_id, 'deleting': True}

    monkeypatch.setattr(access, 'check_dataset_access', check)
    dependency = access.require_level('manage')
    with pytest.raises(HTTPException) as refused:
        run(dependency(_request('POST'), USER))
    assert (refused.value.status_code, refused.value.detail) == (409, access._DELETING)
    assert run(dependency(_request('GET'), USER)) is USER  # a read: the access list of a dataset being deleted
    assert asked == [(1, 'manage'), (1, 'manage')]


def test_caller_level_reuses_the_requests_lookup(monkeypatch):
    check = AsyncMock(return_value={'id': 1, 'is_global': True, 'everyone_level': 2})
    effective = AsyncMock(return_value=3)
    monkeypatch.setattr(access, 'check_dataset_access', check)
    monkeypatch.setattr(access, 'effective_level', effective)
    assert run(access.caller_level(1, USER)) == access.MANAGE  # an administrator: no lookup
    assert not check.called
    state = SimpleNamespace(dataset_level=None)
    assert run(access.caller_level(1, POWER, state)) == 3
    assert run(access.caller_level(1, POWER, state)) == 3
    assert effective.await_count == 1 and state.dataset_level == 3


def _asked_level(monkeypatch, module):
    asked = []

    async def check(dataset_id, user, level='view', state=None):
        asked.append(level)
        return {'id': dataset_id}

    monkeypatch.setattr(module, 'check_dataset_access', check)
    return asked


def test_a_sample_update_of_its_tags_alone_needs_tag(monkeypatch):
    asked = _asked_level(monkeypatch, datasets)
    for body, level in (
        ({'tags': ['a']}, 'tag'),
        ({'tags': []}, 'tag'),
        ({'array_fields': {'@local.tags': ['b']}}, 'tag'),  # a mirror's own tags
        ({'tags': ['a'], 'metadata': {'x': 1}}, 'edit'),
        ({'tags': ['a'], 'metadata': None}, 'edit'),  # any other field sent, even null
        ({'tags': ['a'], 'array_fields': {'@local.tags': ['b']}}, 'edit'),  # both: refused as mixed anyway
        ({'array_fields': {'@local.tags': ['b'], 'labels': ['c']}}, 'edit'),
        ({'array_fields': {}}, 'edit'),
        ({'metadata': {'@local.x': 1}}, 'edit'),  # another of a mirror's own fields
        ({'tags': None}, 'edit'),
        ({'heading': 3.0}, 'edit'),
        ({}, 'edit'),
    ):
        assert run(datasets._sample_update_level(1, datasets.SampleUpdate(**body), _request('PATCH'), POWER)) is POWER
        assert asked.pop() == level, body


def test_bulk_tags_need_tag_for_the_tags_fields_only(monkeypatch):
    asked = _asked_level(monkeypatch, datasets)
    for field, level in (('tags', 'tag'), ('@local.tags', 'tag'), ('inferred_tags', 'edit')):
        body = datasets.BulkTagRequest(sample_ids=[1], add_tags=['x'], field_name=field)
        run(datasets._bulk_tag_level(1, body, _request('POST'), POWER))
        assert asked.pop() == level, field


def test_a_dataset_update_needs_the_highest_level_of_the_fields_sent(monkeypatch):
    asked = _asked_level(monkeypatch, datasets)
    for body, level in (
        ({'description': 'd'}, 'edit'),
        ({'default_view_id': None}, 'edit'),
        ({'description': 'd', 'is_global': True}, 'manage'),
        ({'name': 'n'}, 'manage'),
        ({'everyone_level': 'view'}, 'manage'),
        ({'name': None}, 'edit'),  # an explicit null name changes nothing
        ({}, 'edit'),
    ):
        run(datasets._dataset_update_level(1, datasets.DatasetUpdate(**body), _request('PATCH'), POWER))
        assert asked.pop() == level, body
    # The storage config stays an administrator's, whatever the caller's level.
    for storage in ({'storage_config_id': 3}, {'storage_config_id': None, 'description': 'd'}):
        with pytest.raises(HTTPException) as refused:
            run(datasets._dataset_update_level(1, datasets.DatasetUpdate(**storage), _request('PATCH'), POWER))
        assert refused.value.status_code == 403
    assert run(datasets._dataset_update_level(1, datasets.DatasetUpdate(storage_config_id=3), _request('PATCH'), USER))


def test_another_users_view(monkeypatch):
    looked_up = AsyncMock(return_value=3)
    monkeypatch.setattr(views, 'caller_level', looked_up)
    public = {'user_id': 3, 'dataset_id': 1, 'is_public': True}
    private = {**public, 'is_public': False}
    own = {**public, 'user_id': POWER['id']}
    # The level is looked up only for someone else's public view, the one case it decides.
    for view, user in ((own, POWER), (public, USER), (private, POWER)):
        assert run(views._view_level(view, user, None)) is None
    assert not looked_up.called
    assert run(views._view_level(public, POWER, None)) == 3
    views._require_view_writer(own, POWER, None, 'delete')  # their own
    views._require_view_writer(private, USER, None, 'delete')  # an administrator
    views._require_view_writer(public, POWER, 3, 'delete')  # edit
    with pytest.raises(access.LevelRefused):
        views._require_view_writer(public, POWER, 2, 'delete')
    with pytest.raises(HTTPException) as refused:
        views._require_view_writer(private, POWER, 4, 'share')  # a private one: not even a manager
    assert (refused.value.status_code, refused.value.detail) == (403, 'Not authorized to share this view')


def test_who_may_stop_a_job():
    def job(kind, created_by=POWER['id'], tags_only=False):
        return {'kind': kind, 'created_by': created_by, 'tags_only': tags_only}

    view, tag, edit, manage = 1, 2, 3, 4
    assert jobs._kind_level(job('bulk_mutation', tags_only=True)) == 'tag'
    assert jobs._kind_level(job('bulk_mutation')) == 'edit'  # a bulk delete too
    assert jobs._kind_level(job('dataset_deletion')) == 'manage'
    assert jobs._kind_level(job('patch_vector_import')) is None
    for case, allowed in (
        ((job('bulk_mutation', tags_only=True), POWER, tag), True),  # its starter, at its kind's level
        ((job('bulk_mutation', tags_only=True), POWER, view), False),  # who has lost it
        ((job('bulk_mutation'), POWER, tag), False),
        ((job('bulk_mutation'), POWER, edit), True),
        ((job('bulk_mutation', created_by=3), POWER, edit), False),  # someone else's
        ((job('bulk_mutation', created_by=3), POWER, manage), True),  # a manager of its dataset
        ((job('dataset_deletion'), POWER, edit), False),
        ((job('map_refit', created_by=None), POWER, edit), True),  # a schedule's re-fit: anyone with edit
        ((job('map_refit', created_by=None), POWER, tag), False),
        ((job('patch_vector_import'), POWER, edit), False),  # an administrator's kind
        ((job('patch_vector_import'), POWER, manage), False),  # not even its starter, managing its dataset
        ((job('follower_sync', created_by=None), POWER, manage), False),
        ((job('statistics', created_by=None), POWER, None), False),  # no level
        ((job('statistics', created_by=None), USER, None), True),  # an administrator
    ):
        assert jobs._can_cancel(*case) is allowed, case


def test_an_administrators_job_says_so_to_a_manager(monkeypatch):
    monkeypatch.setattr(
        jobs,
        '_authorized_job',
        AsyncMock(return_value={'kind': 'patch_vector_import', 'dataset_id': 1, 'created_by': 7}),
    )
    monkeypatch.setattr(jobs, '_level_in', AsyncMock(return_value=access.MANAGE))
    with pytest.raises(HTTPException) as refused:
        run(jobs._controlled_job(uuid4(), POWER))
    assert (refused.value.status_code, refused.value.detail) == (
        403,
        'Only an administrator can stop, resume or discard this job',
    )


def test_a_workers_owner_needs_edit():
    assert workers._editable(USER, 1) == ('4 >= 3', [])  # an administrator
    sql, args = workers._editable(POWER, 2)
    assert sql.endswith(' >= 3') and '$2' in sql and args == [POWER['id']]


def test_a_jobs_viewer_level_is_the_highest_their_dataset_check_grants(monkeypatch):
    def granting(have):
        async def check(dataset_id, user, level='view', state=None):
            if have is None:
                raise HTTPException(status_code=404, detail='Dataset not found')
            if access.level_number(level) > have:
                raise access.level_refusal(level)
            return {'id': dataset_id}

        return check

    for have in (1, 2, 3, 4, None):  # None: the dataset is gone
        monkeypatch.setattr(jobs, 'check_dataset_access', granting(have))
        assert run(jobs._level_in(1, POWER, None)) == have
    assert run(jobs._level_in(1, USER, None)) == access.MANAGE  # an administrator: no check


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
def test_share_candidates_are_people(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(datasets, 'db', database)
        await database.execute(
            """INSERT INTO users (id, username, role, is_active, is_demo_account) VALUES
               (20, 'ana', 'power_user', TRUE, FALSE), (21, 'left', 'power_user', FALSE, FALSE),
               (22, 'follower:lab', 'power_user', TRUE, FALSE), (23, 'demo-7', 'guest', TRUE, TRUE),
               (24, 'bo', 'guest', TRUE, NULL)""",
        )
        await database.execute("INSERT INTO followers (name, user_id) VALUES ('lab', 22)")
        await database.execute("INSERT INTO groups (id, name) VALUES (1, 'team')")
        found = await datasets.list_access_candidates(1, USER)
        assert found['groups'] == [{'id': 1, 'name': 'team'}]
        # Not the inactive account, the follower's, nor the demo guest's.
        assert {row['username'] for row in found['users']} >= {'ana', 'bo'}
        assert not {row['username'] for row in found['users']} & {'left', 'follower:lab', 'demo-7'}
        assert all(row.keys() == {'id', 'username'} for row in found['users'])

    registry_database(scenario)


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
def test_a_regrant_that_changes_the_level_records_who_set_it(registry_database, monkeypatch):
    other = {'id': 20, 'role': 'power_user'}

    async def scenario(database):
        monkeypatch.setattr(datasets, 'db', database)
        await database.execute("INSERT INTO users (id, username, role) VALUES (20, 'other', 'power_user')")
        await database.execute("INSERT INTO users (id, username, role) VALUES (21, 'target', 'power_user')")
        await database.execute("INSERT INTO groups (id, name) VALUES (1, 'team')")
        for target, table, column, key in (
            ({'user_id': 21}, 'dataset_access', 'user_id', 21),
            ({'group': 'team'}, 'dataset_group_access', 'group_id', 1),
        ):

            async def grant(level, user):
                body = datasets.GrantAccessRequest(**target, level=level)
                return await datasets.grant_dataset_access(1, body, user)

            async def granter():
                return await database.fetchval(
                    f'SELECT granted_by_user_id FROM {table} WHERE dataset_id = 1 AND {column} = $1',
                    key,
                )

            await grant('view', USER)
            await grant('view', other)  # the same level: who set it stays
            assert await granter() == USER['id']
            assert (await grant(None, other))['level'] == 'view'  # no level: left as it is
            assert await granter() == USER['id']
            assert (await grant('edit', other))['level'] == 'edit'
            assert await granter() == other['id']

    registry_database(scenario)
