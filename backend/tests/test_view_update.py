"""Updating a saved view (PATCH /datasets/{id}/views/{view_id}/content, the app's "Update this view"): its filters are
replaced in place, keeping its name, slug, sharing and default-view status; its owner or an admin may, never on a
mirror's copy of the primary's view, and not over filters changed since the client read them; followers take the new
filters. The database stamps updated_at for every writer (stamp_view_updated_at)."""

import os
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request

from backend import follower_sync, public_settings
from backend.api_errors import CodedHTTPException
from backend.models import ViewContentUpdate
from backend.routers import datasets, versions, views
from backend.tests.test_default_view import CONTENT
from backend.tests.test_field_registry_postgres import USER, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

OWNER = {'id': 10, 'role': 'power_user'}
OTHER = {'id': 11, 'role': 'power_user'}
# A lasso redrawn on the map as it is now, beside the view's filter.
LASSO = {'field': 'clip', 'operator': 'embedding_polygon', 'value': [[0, 0], [2, 0], [2, 2]], 'layout_id': 'now'}
SPLIT = {'field': 'split', 'operator': '==', 'value': 'train'}  # CONTENT's filter
NEW = {
    'filters': {'logic': 'AND', 'filters': [SPLIT, LASSO]},
    'viewStages': [
        {'type': 'filter', 'description': 'split = train', 'filter': SPLIT},
        {'type': 'filter', 'description': 'clip lasso', 'filter': LASSO},
    ],
}


@pytest.fixture
def view_database(registry_database, monkeypatch):
    def run(scenario):
        async def wrapped(database_):
            monkeypatch.setattr(views, 'db', database_)
            monkeypatch.setattr(datasets, 'get_optional_user', AsyncMock(return_value=USER))
            monkeypatch.setattr(datasets, 'check_dataset_access', AsyncMock())
            await database_.execute(
                "INSERT INTO users (id, username, role) VALUES (10, 'owner', 'power_user'), "
                "(11, 'other', 'power_user')",
            )
            await scenario(database_)

        registry_database(wrapped)

    return run


async def _view(database_, dataset_id=1, name='train', content=CONTENT):
    return await database_.fetchval(
        """INSERT INTO views (dataset_id, user_id, name, slug, content, is_public, shared_with_user_ids)
           VALUES ($1, 10, $2, $3, $4, TRUE, '{11}') RETURNING id""",
        dataset_id,
        name,
        f'slug-{name}',
        content,
    )


def update(view_id, content, user, dataset_id=1, **read):
    """read: the updated_at the client read, when it sends one."""
    return views.update_view_content(dataset_id, view_id, ViewContentUpdate(content=content, **read), user)


async def stamp(database_, view_id):
    return await database_.fetchval('SELECT updated_at FROM views WHERE id = $1', view_id)


def test_its_owner_replaces_its_filters_and_its_name_link_sharing_and_default_stay(view_database):
    async def scenario(database_):
        view_id = await _view(database_, content={**CONTENT, 'sort': 'id', 'made_on': 'field-laptop'})
        await datasets.update_dataset(1, datasets.DatasetUpdate(default_view_id=view_id), USER)
        await database_.execute('UPDATE datasets SET followed = TRUE WHERE id = 1')
        before = await database_.fetchrow('SELECT * FROM views WHERE id = $1', view_id)
        assert before['updated_at'] is not None  # stamped as it was made

        answer = await update(view_id, {**NEW, 'made_on': 'forged'}, OWNER, updated_at=before['updated_at'])

        # The filters given replace the view's; a key not given (a sort) stays, and who made it on a follower can't be
        # set here.
        assert answer['content'] == {**NEW, 'sort': 'id', 'made_on': 'field-laptop'}
        after = await database_.fetchrow('SELECT * FROM views WHERE id = $1', view_id)
        assert after['content'] == answer['content']
        for kept in ('name', 'slug', 'user_id', 'is_public', 'shared_with_user_ids', 'created_at'):
            assert after[kept] == before[kept], kept
        assert after['updated_at'] > before['updated_at'] and answer['updated_at'] == after['updated_at']
        assert await database_.fetchval('SELECT default_view_id FROM datasets WHERE id = 1') == view_id
        # The app opens the dataset on the new filters, and knows who may update them.
        default = (await datasets.get_dataset(1, Request({'type': 'http'})))['default_view']
        assert default['content'] == answer['content'] and default['updated_at'] == after['updated_at']
        assert default['user_id'] == 10 and default['from_primary'] is False
        # Followers are told the dataset's views changed (views_log_update), and read them again.
        assert await database_.fetchval("SELECT count(*) FROM dataset_changes WHERE dataset_id = 1 AND kind = 'views'")
        listed = await views.list_dataset_views(1, OWNER)
        assert [(v['id'], v['updated_at']) for v in listed] == [(view_id, after['updated_at'])]

    view_database(scenario)


def test_only_its_owner_or_an_admin_may_and_only_a_view_of_that_dataset(view_database):
    async def scenario(database_):
        view_id = await _view(database_)
        with pytest.raises(HTTPException) as refused:
            await update(view_id, NEW, OTHER)  # shared with them, not theirs
        assert refused.value.status_code == 403
        with pytest.raises(HTTPException) as missing:
            await update(view_id, NEW, USER, dataset_id=2)
        assert missing.value.status_code == 404
        assert await database_.fetchval('SELECT content FROM views WHERE id = $1', view_id) == CONTENT
        assert (await update(view_id, NEW, USER))['content'] == NEW  # an admin

    view_database(scenario)


def test_a_temporary_selection_is_refused_as_when_saving_a_view(view_database):
    async def scenario(database_):
        view_id = await _view(database_)
        token = {'logic': 'AND', 'filters': [{'field': 'id', 'operator': 'in_selection', 'value': 'abc'}]}
        with pytest.raises(HTTPException) as refused:
            await update(view_id, {'filters': token}, OWNER)
        assert refused.value.status_code == 422
        assert await database_.fetchval('SELECT content FROM views WHERE id = $1', view_id) == CONTENT

    view_database(scenario)


def test_a_mirror_s_copy_of_its_primary_s_view_changes_there_and_takes_the_primary_s_update(view_database):
    async def scenario(database_):
        link = await database_.fetchval(
            "INSERT INTO primary_links (url, token_encrypted) VALUES ('https://primary.example/api', 'x') RETURNING id",
        )
        await database_.execute(
            "INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor, vector_fields, link_id) "
            "VALUES (2, 7, 0, '{}', $1)",
            link,
        )
        own = await _view(database_, dataset_id=2, name='made here')
        async with database_.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('fridge.mirror_sync', '2', TRUE)")
            await follower_sync.write_views(connection, 2, [{'id': 70, 'name': 'from primary', 'content': CONTENT}])
        copy = await database_.fetchval('SELECT view_id FROM mirror_views WHERE dataset_id = 2')

        with pytest.raises(CodedHTTPException) as refused:
            await update(copy, NEW, USER, dataset_id=2)  # not even an admin
        assert refused.value.status_code == 409 and refused.value.code == 'view_on_primary'
        assert refused.value.detail.startswith('This view is a copy of one on primary.example: update it there')
        assert [v['from_primary'] for v in await views.list_dataset_views(2, USER) if v['id'] == copy] == [True]
        # A view made on the mirror is this instance's own.
        assert (await update(own, NEW, OWNER, dataset_id=2))['content'] == NEW
        # The primary's update reaches the copy with the next sync.
        async with database_.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('fridge.mirror_sync', '2', TRUE)")
            await follower_sync.write_views(connection, 2, [{'id': 70, 'name': 'from primary', 'content': NEW}])
        assert await database_.fetchval('SELECT content FROM views WHERE id = $1', copy) == NEW

    view_database(scenario)


def test_a_guest_on_a_public_instance_updates_their_view_within_their_view_storage(view_database, monkeypatch):
    monkeypatch.setenv('DEMO_MODE', 'true')
    guest = {'id': 12, 'role': 'guest'}

    async def scenario(database_):
        await database_.execute("INSERT INTO users (id, username, role) VALUES (12, 'guest', 'guest')")
        view_id = await _view(database_)
        await database_.execute(
            'UPDATE views SET user_id = 12, is_public = FALSE, shared_with_user_ids = $2 WHERE id = $1',
            view_id,
            [],
        )
        size = await database_.fetchval(
            "SELECT octet_length($1::jsonb::text) + octet_length('train')",
            NEW,
        )
        monkeypatch.setattr(public_settings, 'settings', public_settings.PublicSettings(guest_view_bytes=size - 1))
        with pytest.raises(HTTPException) as refused:
            await update(view_id, NEW, guest)
        assert refused.value.status_code == 413
        assert await database_.fetchval('SELECT content FROM views WHERE id = $1', view_id) == CONTENT
        monkeypatch.setattr(public_settings, 'settings', public_settings.PublicSettings(guest_view_bytes=size))
        assert (await update(view_id, NEW, guest))['content'] == NEW

    view_database(scenario)


def test_an_update_from_filters_read_before_the_view_changed_is_refused_not_written_over_them(view_database):
    async def scenario(database_):
        view_id = await _view(database_)
        read = await stamp(database_, view_id)
        # Someone else replaces its filters (the API without the value it read: the last writer wins).
        theirs = {**CONTENT, 'filters': {'logic': 'AND', 'filters': [LASSO]}}
        assert (await update(view_id, theirs, USER))['content'] == theirs
        changed = await stamp(database_, view_id)
        assert changed > read
        with pytest.raises(CodedHTTPException) as refused:
            await update(view_id, NEW, OWNER, updated_at=read)
        assert refused.value.status_code == 409 and refused.value.code == 'view_changed'
        assert 'changed since you opened it' in refused.value.detail
        assert await database_.fetchval('SELECT content FROM views WHERE id = $1', view_id) == theirs
        with pytest.raises(CodedHTTPException):
            await update(view_id, NEW, OWNER, updated_at=None)  # "never changed", but it has been
        # From what it is now, the update goes ahead.
        assert (await update(view_id, NEW, OWNER, updated_at=changed))['content'] == NEW

    view_database(scenario)


def test_a_version_restore_moves_updated_at_when_it_changes_the_filters_and_only_then(view_database):
    """The database stamps it for every writer (stamp_view_updated_at), only when the filters change: here a version
    restore, which rewrites a view in place (versions._restore_saved_views)."""

    async def scenario(database_):
        restored = await _view(database_, name='restored')
        read = await stamp(database_, restored)

        async def revert_to(content, name):
            recorded = {
                'name': 'restored',
                'description': 'described again',
                'content': content,
                'slug': 'slug-restored',
                'user_id': 10,
                'is_default': False,
            }
            version = await database_.fetchval(
                "INSERT INTO dataset_versions (dataset_id, name, sample_count, saved_views) "
                "VALUES (1, $1, 0, $2) RETURNING id",
                name,
                [recorded],
            )
            async with database_.pool.acquire() as connection, connection.transaction():
                return await versions._restore_saved_views(connection, 1, version, USER, False)

        assert await revert_to(CONTENT, 'v1') == (1, 0)  # the same filters, another description
        await database_.execute(
            "UPDATE views SET name = 'renamed', shared_with_user_ids = '{}' WHERE id = $1",
            restored,
        )
        await database_.execute("UPDATE views SET name = 'restored' WHERE id = $1", restored)
        await database_.execute("UPDATE views SET updated_at = '2000-01-01' WHERE id = $1", restored)  # nobody sets it
        assert await stamp(database_, restored) == read
        assert await revert_to(NEW, 'v2') == (1, 0)
        assert await database_.fetchval('SELECT content FROM views WHERE id = $1', restored) == NEW
        assert await stamp(database_, restored) > read

    view_database(scenario)


def test_a_mirror_s_sync_moves_the_updated_at_of_a_copy_whose_filters_changed_only(view_database):
    """The primary updates its default view; the mirror's sync gives its copy a new value, which the app's dataset
    answer carries (its default view is new), and leaves an unchanged copy's alone."""

    async def scenario(database_):
        await database_.execute(
            "INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor, vector_fields) VALUES (2, 7, 0, '{}')",
        )
        primary = [{'id': 70, 'name': 'default', 'content': CONTENT}, {'id': 71, 'name': 'other', 'content': CONTENT}]

        async def sync(views_):
            async with database_.pool.acquire() as connection, connection.transaction():
                await connection.execute("SELECT set_config('fridge.mirror_sync', '2', TRUE)")
                await follower_sync.write_views(connection, 2, views_)
                await connection.execute(
                    'UPDATE datasets SET default_view_id = '
                    '(SELECT view_id FROM mirror_views WHERE origin_view_id = 70) WHERE id = 2',
                )

        await sync(primary)
        copies = dict(await database_.fetch('SELECT origin_view_id, view_id FROM mirror_views WHERE dataset_id = 2'))
        before = {origin: await stamp(database_, copy) for origin, copy in copies.items()}
        app_before = (await datasets.get_dataset(2, Request({'type': 'http'})))['default_view']['updated_at']
        await sync([{**primary[0], 'content': NEW}, primary[1]])
        assert await stamp(database_, copies[70]) > before[70]
        assert await stamp(database_, copies[71]) == before[71]
        app_after = (await datasets.get_dataset(2, Request({'type': 'http'})))['default_view']
        assert app_after['content'] == NEW and app_after['updated_at'] > app_before

    view_database(scenario)
