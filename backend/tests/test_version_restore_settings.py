"""A revert leaves the dataset's settings as they are unless asked to restore them, and never restores its name."""

import os
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fridge.core.dataset import Dataset

from backend.routers import versions
from backend.tests import test_access_rule_spec
from backend.tests.test_access_rule_spec import ADMIN, GUEST, POWER
from backend.tests.test_field_registry_postgres import USER, ingest, registry_database

__all__ = ['registry_database']
world = test_access_rule_spec.world
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


@needs_postgres
def test_reverting_and_making_a_version_take_edit_and_deleting_one_takes_manage(world):
    """A revert can delete the samples added since, change public views whoever owns them and restore the display
    settings: edit, as each of those is (it was an admin's). A deleted version can't be brought back: manage. Making
    a version: edit. A guest, at view, does none of them; an admin does all."""

    async def scenario(w):
        cases = [
            # (caller, their level, make, revert, delete)
            (POWER, 'tag', 403, 403, 403),
            (POWER, 'edit', 200, 200, 403),
            (POWER, 'manage', 200, 200, 200),
            (GUEST, 'manage', 403, 403, 403),
            (ADMIN, None, 200, 200, 200),
        ]
        for dataset_id, (caller, level, make, revert, delete) in enumerate(cases, start=10):
            await w.dataset(dataset_id)
            await ingest([{}], dataset_id=dataset_id)
            if level is not None:
                await w.share(dataset_id, caller, level)
            made = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/versions', json={'name': 'v1'})
            assert made.status_code == 200, made.text
            version = await w.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', dataset_id)
            base = f'/api/datasets/{dataset_id}/versions'
            calls = [
                ('POST', base, {'json': {'name': 'v2'}}, make, 'edit'),
                ('POST', f'{base}/{version}/revert', {}, revert, 'edit'),
                ('DELETE', f'{base}/{version}', {}, delete, 'manage'),
            ]
            for method, path, kwargs, wanted, needed in calls:
                response = await w.call(caller, method, path, **kwargs)
                assert response.status_code == wanted, (caller, level, method, path, response.text)
                if wanted == 403:
                    assert response.json() == {'detail': f'This needs {needed} access to the dataset'}, response.text
            remaining = await w.database.fetchval('SELECT count(*) FROM dataset_versions WHERE id = $1', version)
            assert remaining == (0 if delete == 200 else 1), (caller, level)

    world(scenario)


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
