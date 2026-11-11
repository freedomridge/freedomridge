"""A revert leaves the dataset's settings as they are unless asked to restore them, and never restores its name."""

import os
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fridge.core.dataset import Dataset

from backend.routers import versions
from backend.tests.test_field_registry_postgres import USER, ingest, registry_database

__all__ = ['registry_database']
needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


@needs_postgres
def test_a_revert_restores_the_settings_only_when_asked(registry_database):
    async def scenario(database):
        async def settings():
            row = await database.fetchrow(
                """SELECT name, description, media_fields, grid_media_field, modal_media_field, segmentation_config
                   FROM datasets WHERE id = 1""",
            )
            return dict(row)

        await ingest([{'label': 'original'}])
        await database.execute(
            """UPDATE datasets SET description = 'as versioned', media_fields = '{filepath,nir}',
                      grid_media_field = 'nir', segmentation_config = '{"mask_targets": {"mask": {"1": "crop"}}}'
               WHERE id = 1""",
        )
        recorded = await settings()
        version = (await versions.create_version(1, versions.CreateVersionRequest(name='v1')))['id']
        await database.execute(
            """UPDATE datasets SET name = 'renamed', description = 'later', media_fields = '{filepath}',
                      grid_media_field = 'filepath', segmentation_config = '{}'
               WHERE id = 1""",
        )
        later = await settings()

        kept = await versions.revert_to_version(1, version, user=USER)
        assert kept['settings_restored'] is False
        assert await settings() == later

        restored = await versions.revert_to_version(1, version, user=USER, restore_settings=True)
        assert restored['settings_restored'] is True
        assert await settings() == {**recorded, 'name': 'renamed'}

        # A version from before versions recorded settings has none to restore, and says so before changing anything.
        await database.execute('UPDATE dataset_versions SET dataset_snapshot = NULL WHERE id = $1', version)
        saved = await database.fetchval('SELECT count(*) FROM dataset_versions')
        with pytest.raises(HTTPException) as refused:
            await versions.revert_to_version(1, version, user=USER, restore_settings=True)
        assert refused.value.status_code == 400 and 'settings' in refused.value.detail
        assert await database.fetchval('SELECT count(*) FROM dataset_versions') == saved

    registry_database(scenario)


def test_reverting_and_deleting_take_an_admin_and_making_a_version_does_not():
    def role_guard(endpoint):
        routes = [route for route in versions.router.routes if isinstance(route, APIRoute)]
        route = next(route for route in routes if route.endpoint is endpoint)
        guards = [
            dependency.call
            for dependency in route.dependant.dependencies
            if 'require_role' in getattr(dependency.call, '__qualname__', '')
        ]
        assert len(guards) == 1, route.path
        return guards[0]

    power_user, admin = {'id': 2, 'role': 'power_user'}, {'id': 1, 'role': 'admin'}
    # A revert can delete samples, change views whoever owns them, and restore settings: each an admin's to do.
    revert = role_guard(versions.revert_to_version)
    with pytest.raises(HTTPException) as refused:
        revert(power_user)
    assert refused.value.status_code == 403
    assert revert(admin) == admin
    # A deleted version can't be brought back.
    delete = role_guard(versions.delete_version)
    with pytest.raises(HTTPException) as refused:
        delete(power_user)
    assert refused.value.status_code == 403
    assert delete(admin) == admin
    assert role_guard(versions.post_version)(power_user) == power_user


def test_the_sdk_says_when_the_server_did_not_restore_the_settings():
    dataset = Dataset.__new__(Dataset)
    dataset._id = 7
    dataset.client = Mock()
    dataset.client.revert_version.return_value = {'settings_restored': True}

    dataset.revert(version_id=3, exact_membership=True, restore_settings=True)
    dataset.client.revert_version.assert_called_with(
        7,
        3,
        restore_deleted=False,
        exact_membership=True,
        restore_settings=True,
    )

    # A server from before reverts could restore settings ignores the option.
    dataset.client.revert_version.return_value = {'samples_reverted': 5}
    with pytest.raises(ValueError, match='settings were not restored'):
        dataset.revert(version_id=3, restore_settings=True)
