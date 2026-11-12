"""A dataset's default view: the public saved view the app opens the dataset on (datasets.default_view_id)."""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request

from backend.routers import datasets, views
from backend.tests.test_field_registry_postgres import USER, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

TRAINING, OTHER = 1, 2  # the datasets registry_database seeds
REQUEST = Request({'type': 'http'})  # get_dataset's caller is mocked below
CONTENT = {
    'filters': {'logic': 'AND', 'filters': [{'field': 'split', 'operator': '==', 'value': 'train'}]},
    'viewStages': [
        {
            'type': 'filter',
            'description': 'split = train',
            'filter': {'field': 'split', 'operator': '==', 'value': 'train'},
        },
    ],
}


@pytest.fixture
def view_database(registry_database, monkeypatch):
    """registry_database with the views router on it, and GET /datasets/{id} open to the test's admin."""

    def run(scenario):
        async def wrapped(database_):
            monkeypatch.setattr(views, 'db', database_)
            monkeypatch.setattr(datasets, 'get_optional_user', AsyncMock(return_value=USER))
            monkeypatch.setattr(datasets, 'check_dataset_access', AsyncMock())
            await scenario(database_)

        registry_database(wrapped)

    return run


async def _view(database_, dataset_id, name, *, public=True):
    return await database_.fetchval(
        "INSERT INTO views (dataset_id, name, slug, content, is_public) VALUES ($1, $2, $3, $4, $5) RETURNING id",
        dataset_id,
        name,
        f'slug-{name}',
        CONTENT,
        public,
    )


async def _waits_on_lock(database_, task, statement):
    """Whether `task` gets to wait on a row lock in a statement containing `statement` (pg_stat_activity),
    rather than finishing or never getting there."""
    for _ in range(250):
        if task.done():
            return False
        if await database_.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND position($1 in query) > 0",
            statement,
        ):
            return True
        await asyncio.sleep(0.02)
    return False


async def _set_default(dataset_id, view_id):
    return await datasets.update_dataset(dataset_id, datasets.DatasetUpdate(default_view_id=view_id), USER)


def test_a_public_view_of_the_dataset_becomes_its_default_and_the_dataset_returns_it(view_database):
    async def scenario(database_):
        clean = await _view(database_, TRAINING, 'clean')
        assert (await datasets.get_dataset(TRAINING, REQUEST))['default_view'] is None

        assert (await _set_default(TRAINING, clean))['default_view_id'] == clean
        got = await datasets.get_dataset(TRAINING, REQUEST)
        assert got['default_view_id'] == clean
        assert got['default_view'] == {
            'id': clean,
            'name': 'clean',
            'slug': 'slug-clean',
            'content': CONTENT,
            'user_id': None,  # who may update it (with an admin): its owner
            'updated_at': await database_.fetchval('SELECT updated_at FROM views WHERE id = $1', clean),
            'from_primary': False,
        }
        # Changing another setting keeps it; null removes it.
        renamed = await datasets.update_dataset(TRAINING, datasets.DatasetUpdate(description='x'), USER)
        assert renamed['default_view_id'] == clean
        assert (await _set_default(TRAINING, None))['default_view_id'] is None
        assert (await datasets.get_dataset(TRAINING, REQUEST))['default_view'] is None

    view_database(scenario)


def test_a_private_view_or_another_datasets_view_is_refused(view_database):
    async def scenario(database_):
        private = await _view(database_, TRAINING, 'mine', public=False)
        elsewhere = await _view(database_, OTHER, 'elsewhere')
        for view_id, detail in (
            (private, "Share the view 'mine' with everyone first"),
            (elsewhere, f'View {elsewhere} is not a saved view of this dataset'),
            (999_999, 'View 999999 is not a saved view of this dataset'),
        ):
            with pytest.raises(HTTPException) as refused:
                await _set_default(TRAINING, view_id)
            assert refused.value.status_code == 400
            assert detail in refused.value.detail
        assert await database_.fetchval('SELECT default_view_id FROM datasets WHERE id = $1', TRAINING) is None
        with pytest.raises(HTTPException) as unknown:
            await _set_default(999, private)
        assert (unknown.value.status_code, unknown.value.detail) == (404, 'Dataset not found')

    view_database(scenario)


def test_the_default_view_stays_public_until_it_is_no_longer_the_default(view_database):
    async def scenario(database_):
        clean = await _view(database_, TRAINING, 'clean')
        await _set_default(TRAINING, clean)
        with pytest.raises(HTTPException) as refused:
            await views.share_view(TRAINING, clean, views.ViewShare(is_public=False, shared_with_user_ids=[]), USER)
        assert refused.value.status_code == 409
        assert await database_.fetchval('SELECT is_public FROM views WHERE id = $1', clean) is True

        # Keeping it public is fine, and once it isn't the default it can be made private.
        await views.share_view(TRAINING, clean, views.ViewShare(is_public=True, shared_with_user_ids=[1]), USER)
        await _set_default(TRAINING, None)
        await views.share_view(TRAINING, clean, views.ViewShare(is_public=False, shared_with_user_ids=[]), USER)
        assert await database_.fetchval('SELECT is_public FROM views WHERE id = $1', clean) is False

    view_database(scenario)


def test_making_the_view_private_waits_for_a_default_view_change_and_then_is_refused(view_database):
    async def scenario(database_):
        clean = await _view(database_, TRAINING, 'clean')
        async with database_.pool.acquire() as connection, connection.transaction():
            await datasets._require_default_view_candidate(connection, TRAINING, clean)
            # Made private between the check and the update: it waits for the change to commit.
            unshare = asyncio.create_task(
                views.share_view(TRAINING, clean, views.ViewShare(is_public=False, shared_with_user_ids=[]), USER),
            )
            assert await _waits_on_lock(database_, unshare, 'FOR UPDATE')
            await connection.execute('UPDATE datasets SET default_view_id = $1 WHERE id = $2', clean, TRAINING)
        with pytest.raises(HTTPException) as refused:
            await unshare
        assert refused.value.status_code == 409
        assert await database_.fetchval('SELECT is_public FROM views WHERE id = $1', clean) is True

    view_database(scenario)


def test_a_default_view_change_waits_for_the_view_being_made_private_and_then_is_refused(view_database):
    async def scenario(database_):
        clean = await _view(database_, TRAINING, 'clean')
        async with database_.pool.acquire() as connection, connection.transaction():
            # share_view's transaction, holding the row it is making private.
            await connection.execute('SELECT 1 FROM views WHERE id = $1 FOR UPDATE', clean)
            await connection.execute('UPDATE views SET is_public = false WHERE id = $1', clean)
            make_default = asyncio.create_task(_set_default(TRAINING, clean))
            assert await _waits_on_lock(database_, make_default, 'FOR SHARE')
        with pytest.raises(HTTPException) as refused:
            await make_default
        assert refused.value.status_code == 400  # the locked row is read again once it is committed
        assert await database_.fetchval('SELECT default_view_id FROM datasets WHERE id = $1', TRAINING) is None

    view_database(scenario)


def test_deleting_the_view_or_the_dataset_clears_or_takes_the_default(view_database):
    async def scenario(database_):
        clean = await _view(database_, TRAINING, 'clean')
        await _set_default(TRAINING, clean)
        await views.delete_view(clean, USER)
        assert await database_.fetchval('SELECT default_view_id FROM datasets WHERE id = $1', TRAINING) is None

        # The dataset's own row goes in the same statement that cascades to its views.
        other = await _view(database_, OTHER, 'other-clean')
        await _set_default(OTHER, other)
        await database_.execute('DELETE FROM datasets WHERE id = $1', OTHER)
        assert await database_.fetchval('SELECT count(*) FROM views WHERE id = $1', other) == 0

    view_database(scenario)
