"""A version records the dataset's public saved views: they are read back from it, and a view added, removed or
given other filters counts as a change since the version."""

import json
import os

import pytest
from fastapi import HTTPException

from backend.routers import versions
from backend.tests.test_field_registry_postgres import USER, ingest, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

NO_CHANGES: dict[str, list[str]] = {'added': [], 'removed': [], 'filters_changed': []}


def split_is(value):
    return {'viewStages': [{'type': 'filter', 'filter': {'field': 'split', 'operator': '=', 'value': value}}]}


async def save_view(database, name, content, is_public=True, dataset_id=1):
    await database.execute(
        'INSERT INTO views (dataset_id, name, slug, content, is_public) VALUES ($1, $2, $3, $4, $5)',
        dataset_id,
        name,
        f'{dataset_id}-{name}',
        content,
        is_public,
    )


async def snapshot(name, **options):
    return await versions.create_version(1, versions.CreateVersionRequest(name=name, **options))


def test_a_version_records_the_public_views(registry_database):
    async def scenario(database):
        await ingest([{'split': 'train'}])
        await save_view(database, 'train', split_is('train'))
        # A row from before content was stored as an object: the JSON text of one.
        await save_view(database, 'legacy', json.dumps(split_is('val')))
        await save_view(database, 'mine', split_is('test'), is_public=False)
        await save_view(database, 'elsewhere', split_is('train'), dataset_id=2)

        # The view the dataset opens on is recorded as that.
        await database.execute("UPDATE datasets SET default_view_id = (SELECT id FROM views WHERE name = 'train')")

        full = await snapshot('v1')
        assert full['saved_view_count'] == 2
        recorded = {'description': None, 'user_id': None}
        assert (await versions.get_version_views(1, full['id']))['views'] == [
            {**recorded, 'name': 'legacy', 'content': split_is('val'), 'slug': '1-legacy', 'is_default': False},
            {**recorded, 'name': 'train', 'content': split_is('train'), 'slug': '1-train', 'is_default': True},
        ]
        assert (await versions.version_status(1))['current_version']['name'] == 'v1'

        hash_only = await snapshot('hashes', include_metadata=False)
        assert hash_only['saved_view_count'] is None
        assert await versions.get_version_views(1, hash_only['id']) == {'views': None}

    registry_database(scenario)


def test_a_recorded_view_shows_the_samples_it_held_at_the_version(registry_database):
    async def scenario(database):
        await ingest([{'split': 'train'}, {'split': 'val'}, {'split': 'train'}])
        await save_view(database, 'train', split_is('train'))
        await save_view(database, 'everything', {'viewStages': []})
        version = (await snapshot('v1'))['id']
        # Since the version, a sample changed split and the view was given other filters.
        await database.execute("""UPDATE samples SET metadata = '{"split": "val"}' WHERE filepath = 'sample-0.jpg'""")
        await database.execute("UPDATE views SET content = $1 WHERE name = 'train'", split_is('val'))

        shown = await versions.get_version_samples(1, version, full=True, view='train')
        assert shown['total'] == 2
        assert [item['filepath'] for item in shown['items']] == ['sample-0.jpg', 'sample-2.jpg']
        # With the page's search, and its paging.
        searched = await versions.get_version_samples(1, version, view='train', q='sample-2')
        assert searched == {'sample_ids': [shown['items'][1]['id']], 'total': 1}
        paged = await versions.get_version_samples(1, version, view='train', limit=1, offset=1)
        assert paged == {'sample_ids': [shown['items'][1]['id']], 'total': 2}
        assert (await versions.get_version_samples(1, version, view='everything'))['total'] == 3

        with pytest.raises(HTTPException) as missing:
            await versions.get_version_samples(1, version, view='nope')
        assert missing.value.status_code == 404 and "'nope'" in missing.value.detail

    registry_database(scenario)


def test_a_changed_public_view_is_a_change_since_the_version(registry_database):
    async def scenario(database):
        await ingest([{'split': 'train'}])
        await save_view(database, 'train', split_is('train'))
        await save_view(database, 'val', split_is('val'))
        first = (await snapshot('v1'))['id']

        await save_view(database, 'mine', split_is('test'), is_public=False)
        assert (await versions.version_status(1))['current_version']['name'] == 'v1'

        await save_view(database, 'test', split_is('test'))
        status = await versions.version_status(1)
        assert status['has_changes'] and status['current_version'] is None
        assert status['view_changes'] == {**NO_CHANGES, 'added': ['test']}

        await database.execute("UPDATE views SET content = $1 WHERE name = 'train'", split_is('training'))
        await database.execute("DELETE FROM views WHERE name = 'val'")
        changes = {'added': ['test'], 'removed': ['val'], 'filters_changed': ['train']}
        assert (await versions.version_status(1))['view_changes'] == changes
        assert (await versions.diff_current(1, first))['view_changes'] == changes

        second = (await snapshot('v2'))['id']
        assert (await versions.version_status(1))['current_version']['name'] == 'v2'
        assert (await versions.diff_versions(1, first, second))['view_changes'] == changes
        assert (await versions.diff_versions(1, second, first))['view_changes'] == {
            'added': ['val'],
            'removed': ['test'],
            'filters_changed': ['train'],
        }

    registry_database(scenario)


def test_a_revert_puts_the_recorded_views_back(registry_database):
    async def scenario(database):
        async def views_now():
            rows = await database.fetch('SELECT name, slug, content FROM views WHERE dataset_id = 1 ORDER BY name')
            return {row['name']: (row['slug'], row['content']) for row in rows}

        await ingest([{'split': 'train'}])
        await save_view(database, 'train', split_is('train'))
        await save_view(database, 'val', split_is('val'))
        first = (await snapshot('v1'))['id']
        await database.execute("UPDATE views SET content = $1 WHERE name = 'train'", split_is('training'))
        await database.execute("DELETE FROM views WHERE name = 'val'")
        await save_view(database, 'test', split_is('test'))
        await save_view(database, 'mine', split_is('test'), is_public=False)

        # The recorded views come back, the deleted one with its link; what was added since stays.
        reverted = await versions.revert_to_version(1, first, user=USER)
        assert (reverted['views_restored'], reverted['views_removed']) == (2, 0)
        assert await views_now() == {
            'mine': ('1-mine', split_is('test')),
            'test': ('1-test', split_is('test')),
            'train': ('1-train', split_is('train')),
            'val': ('1-val', split_is('val')),
        }
        # The undo point recorded the public views as they were before.
        undo = (await versions.get_version_views(1, reverted['auto_snapshot_id']))['views']
        assert [(view['name'], view['content']) for view in undo] == [
            ('test', split_is('test')),
            ('train', split_is('training')),
        ]

        # Exact also removes the public view the version doesn't have, never a private one: the dataset is
        # the version again.
        exact = await versions.revert_to_version(1, first, user=USER, exact_membership=True)
        assert (exact['views_restored'], exact['views_removed']) == (0, 1)
        assert sorted(await views_now()) == ['mine', 'train', 'val']
        assert (await versions.version_status(1))['current_version']['name'] == 'v1'

        # A private view that now has a recorded view's name stops the restore, and nothing is kept of it.
        await database.execute("DELETE FROM views WHERE name = 'val'")
        await save_view(database, 'val', split_is('mine'), is_public=False)
        saved = await database.fetchval('SELECT count(*) FROM dataset_versions')
        with pytest.raises(HTTPException) as refused:
            await versions.revert_to_version(1, first, user=USER)
        assert refused.value.status_code == 409 and "['val']" in refused.value.detail
        assert await database.fetchval('SELECT count(*) FROM dataset_versions') == saved
        assert (await views_now())['val'] == ('1-val', split_is('mine'))

    registry_database(scenario)


def test_a_settings_restore_puts_the_default_view_back_by_name(registry_database):
    async def scenario(database):
        async def default_view():
            return await database.fetchval(
                'SELECT w.name FROM datasets d JOIN views w ON w.id = d.default_view_id WHERE d.id = 1',
            )

        async def open_on(name):
            await database.execute(
                'UPDATE datasets SET default_view_id = (SELECT id FROM views WHERE dataset_id = 1 AND name = $1) '
                'WHERE id = 1',
                name,
            )

        await ingest([{'split': 'train'}])
        await save_view(database, 'train', split_is('train'))
        await open_on('train')
        first = (await snapshot('v1'))['id']
        # The default view is deleted (which clears the default), and the dataset opens on a view made since.
        await database.execute("DELETE FROM views WHERE name = 'train'")
        await save_view(database, 'later', split_is('val'))
        await open_on('later')

        # Without its settings, an exact revert removes the view made since, and the dataset opens on none.
        await versions.revert_to_version(1, first, user=USER, exact_membership=True)
        assert await default_view() is None
        # With them, it opens on the recorded default again: the view made again, under its new id.
        await versions.revert_to_version(1, first, user=USER, exact_membership=True, restore_settings=True)
        assert await default_view() == 'train'

        # A version of a dataset that opened on no view restores that too.
        await open_on(None)
        none = (await snapshot('no default'))['id']
        await open_on('train')
        await versions.revert_to_version(1, none, user=USER, restore_settings=True)
        assert await default_view() is None

    registry_database(scenario)


def test_a_version_that_recorded_no_views_says_nothing_about_them(registry_database):
    async def scenario(database):
        await ingest([{'split': 'train'}])
        before = (await snapshot('before'))['id']
        # As a version made before versions recorded views.
        await database.execute('UPDATE dataset_versions SET saved_views = NULL WHERE id = $1', before)
        await save_view(database, 'train', split_is('train'))

        assert await versions.get_version_views(1, before) == {'views': None}
        assert (await versions.version_status(1))['current_version']['name'] == 'before'
        assert (await versions.diff_current(1, before))['view_changes'] is None
        after = (await snapshot('after'))['id']
        assert (await versions.diff_versions(1, before, after))['view_changes'] is None
        # Nor does a revert to it touch the views, even an exact one.
        reverted = await versions.revert_to_version(1, before, user=USER, exact_membership=True)
        assert (reverted['views_restored'], reverted['views_removed']) == (0, 0)
        assert await database.fetchval("SELECT count(*) FROM views WHERE name = 'train'") == 1

    registry_database(scenario)
