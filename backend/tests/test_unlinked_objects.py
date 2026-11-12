"""Objects with no image (unlinked_objects.py): finding them by kind, the filter that shows them, deleting a kind as
a bulk delete, the parent_id field operations refused on a dataset with objects, and parent_id kept out of the
sidebar."""

import json
import os

import pytest
from fastapi import HTTPException

from backend import index_advisor, unlinked_objects
from backend.routers import datasets, versions
from backend.tests import test_bulk_jobs
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_object_parent_column import add, column, ids

registry_database = registry_helpers.registry_database
USER = registry_helpers.USER
VIEWER = {**USER, 'role': 'viewer'}
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
EVERY_KIND = {'image_gone': 2, 'other_dataset': 1, 'object': 1, 'not_a_sample_id': 3, 'no_parent_id': 2}


async def every_kind(database) -> dict:
    """Dataset 1: an image with a linked object, and objects with no image of every kind, plus one whose image is
    there but isn't linked (written with the triggers off, as nothing in the app can). Returns their ids by name."""
    await add(1, [(None, {'crop': 'corn'})], 's3://u/img')
    (image,) = await ids(database, 1, 's3://u/img')
    await add(2, [(None, {})], 's3://u/other')
    (elsewhere,) = await ids(database, 2, 's3://u/other')
    await add(1, [('object', {'parent_id': image, 'label': 'linked'})], 's3://u/linked')
    (linked,) = await ids(database, 1, 's3://u/linked')
    named = {
        'gone': {'parent_id': 999999},
        'zero': {'parent_id': '0'},  # names no sample: ids start at 1
        'elsewhere': {'parent_id': elsewhere},
        'on_object': {'parent_id': linked},
        'text': {'parent_id': 'abc'},
        'negative': {'parent_id': -1},
        'fraction': {'parent_id': 1.5},
        'missing': {'label': 'standalone'},
        'null': {'parent_id': None},
    }
    found = {'image': image, 'linked': linked}
    for name, metadata in named.items():
        await add(1, [('object', metadata)], f's3://u/{name}')
        (found[name],) = await ids(database, 1, f's3://u/{name}')
    async with database.pool.acquire() as connection, connection.transaction():
        await connection.execute('ALTER TABLE samples DISABLE TRIGGER USER')
        found['linkable'] = await connection.fetchval(
            "INSERT INTO samples (dataset_id, filepath, media_type, metadata) "
            "VALUES (1, 's3://u/linkable-0.jpg', 'object', jsonb_build_object('parent_id', $1::int)) RETURNING id",
            image,
        )
        await connection.execute('ALTER TABLE samples ENABLE TRIGGER USER')
    return found


async def matching(database, node) -> list:
    response = await datasets.list_samples_post(1, datasets.SamplesRequest(filters=node, limit=100))
    return sorted(sample['id'] for sample in response['items'])


def test_the_finder_counts_each_kind_links_what_it_can_and_its_filter_shows_them(registry_database):
    async def scenario(database):
        found = await every_kind(database)
        # Someone who can't edit the dataset sees the object waiting for its link, apart.
        report = await datasets.find_unlinked_objects(1, VIEWER)
        assert (report['kinds'], report['total'], report['linkable'], report['linked']) == (EVERY_KIND, 9, 1, 0)
        assert await column(database, found['linkable']) == [None]
        # An editor's request links it first: it is never counted, listed or deleted.
        report = await datasets.find_unlinked_objects(1, USER)
        assert (report['kinds'], report['total'], report['linkable'], report['linked']) == (EVERY_KIND, 9, 0, 1)
        assert await column(database, found['linkable']) == [found['image']]
        assert report['mirror'] is False

        unlinked = [found[name] for name in ('gone', 'zero', 'elsewhere', 'on_object', 'text', 'negative')]
        unlinked += [found[name] for name in ('fraction', 'missing', 'null')]
        assert await matching(database, report['filter']) == sorted(unlinked)
        by_kind = {
            'image_gone': ['gone', 'zero'],
            'other_dataset': ['elsewhere'],
            'object': ['on_object'],
            'not_a_sample_id': ['text', 'negative', 'fraction'],
            'no_parent_id': ['missing', 'null'],
        }
        for kind, names in by_kind.items():
            leaf = unlinked_objects.filter_leaf([kind])
            assert await matching(database, leaf) == sorted(found[name] for name in names), kind
        # Under NOT, it is the complement (the images, and the linked objects).
        complement = await matching(database, {'logic': 'NOT', 'filters': [report['filter']]})
        assert complement == sorted([found['image'], found['linked'], found['linkable']])
        with pytest.raises(HTTPException) as refused:
            await matching(database, {'field': 'parent_id', 'operator': 'unlinked_object', 'value': ['lost']})
        assert refused.value.status_code == 400

    registry_database(scenario)


def test_deleting_takes_only_the_kinds_named_as_a_bulk_delete_and_never_an_object_whose_image_came(registry_database):
    async def scenario(database):
        found = await every_kind(database)
        await database.execute('UPDATE datasets SET followed = TRUE WHERE id = 1')
        for request, status in (([], 400), (['lost'], 400)):
            with pytest.raises(HTTPException) as refused:
                await datasets.delete_unlinked_objects(1, datasets.UnlinkedObjectsDelete(kinds=request), USER)
            assert refused.value.status_code == status

        # The default: only the objects whose image is gone. The one whose image is there is linked first.
        answer = await datasets.delete_unlinked_objects(1, datasets.UnlinkedObjectsDelete(), USER)
        assert answer.status_code == 202
        body = json.loads(answer.body)
        assert (body['kinds'], body['expected'], body['linked']) == (['image_gone'], 2, 1)
        # Before the job runs, the image one of them names arrives: it is linked, and no unit deletes it.
        await database.execute(
            "INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) "
            "VALUES (999999, 1, 'late.jpg', 'image', '{}')",
        )
        assert await column(database, found['gone']) == [999999]
        job = await test_bulk_jobs.finish(database)
        assert (job['result']['deleted_count'], job['result']['objects_deleted']) == (1, 0)
        assert await database.fetchval('SELECT count(*) FROM samples WHERE id = $1', found['zero']) == 0
        assert await database.fetchval('SELECT count(*) FROM samples WHERE id = $1', found['gone']) == 1
        assert await database.fetchval('SELECT count(*) FROM sample_block_changes WHERE dataset_id = 1') > 0

        # Other kinds only when named.
        named = datasets.UnlinkedObjectsDelete(kinds=['not_a_sample_id', 'no_parent_id'])
        body = json.loads((await datasets.delete_unlinked_objects(1, named, USER)).body)
        assert body['expected'] == 5
        job = await test_bulk_jobs.finish(database)
        assert job['result']['deleted_count'] == 5
        report = await datasets.find_unlinked_objects(1, USER)
        assert report['kinds'] == {**dict.fromkeys(unlinked_objects.KINDS, 0), 'other_dataset': 1, 'object': 1}
        # None left of a kind: no job.
        answer = await datasets.delete_unlinked_objects(1, datasets.UnlinkedObjectsDelete(), USER)
        assert answer == {'kinds': ['image_gone'], 'linked': 0, 'expected': 0, 'deleted_count': 0, 'objects_deleted': 0}

    registry_database(scenario)


def test_a_version_query_refuses_the_filter_as_a_live_one(registry_database):
    """The filter asks whether an object has an image now: a version's query would judge it against the snapshot's
    rows instead, so it is refused like the cross-grain operators."""

    async def scenario(database):
        await every_kind(database)
        version = (await versions.create_version(1, versions.CreateVersionRequest(name='v1')))['id']
        request = versions.VersionSamplesQueryRequest(filters=unlinked_objects.filter_leaf(['image_gone']))
        with pytest.raises(HTTPException) as refused:
            await versions.query_version_samples(1, version, request)
        assert refused.value.status_code == 400
        assert refused.value.detail == (
            "Filter on 'parent_id' (unlinked_object) reads live data that a version does not store"
        )

    registry_database(scenario)


def test_a_mirror_reports_its_objects_with_no_image_and_refuses_to_delete_them(registry_database):
    async def scenario(database):
        await every_kind(database)
        await database.execute(
            """INSERT INTO primary_links (url, token_encrypted, identity)
               VALUES ('https://primary.example/api', 'x', 'p');
               INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, state, feed_cursor)
               SELECT 1, id, 7, 'following', 1 FROM primary_links""",
        )
        report = await datasets.find_unlinked_objects(1, USER)
        # Its sync links a mirror's objects: the finder writes nothing there.
        assert (report['mirror'], report['total'], report['linkable'], report['linked']) == (True, 9, 1, 0)
        with pytest.raises(HTTPException) as refused:
            await datasets.delete_unlinked_objects(1, datasets.UnlinkedObjectsDelete(), USER)
        assert refused.value.status_code == 409
        assert 'change only through its sync, so delete them on the primary' in refused.value.detail

    registry_database(scenario)


def test_the_count_is_read_from_the_unlinked_objects_index_planned_either_way(registry_database):
    """The finder's count (asked when a dataset's object grid opens) reads idx_samples_unlinked_objects alone, with
    a plan for the call's dataset id and with a generic one, among 40,000 images and linked objects."""

    async def scenario(database):
        async with registry_helpers.bulk_setup(database) as connection:
            await connection.execute(
                """INSERT INTO samples (id, dataset_id, filepath, media_type, metadata)
                   SELECT g, 1 + g % 2, 'img-' || g, 'image', '{}' FROM generate_series(1, 20000) g;
                   SELECT setval('samples_id_seq', 20000);
                   INSERT INTO samples (dataset_id, filepath, media_type, metadata)
                   SELECT 1 + g % 2, 'img-' || g || '#0', 'object', jsonb_build_object('parent_id', g)
                     FROM generate_series(1, 20000) g;
                   INSERT INTO samples (dataset_id, filepath, media_type, metadata)
                   SELECT 1, 'gone-' || g, 'object', '{"parent_id": 999999}' FROM generate_series(1, 3) g;
                   ANALYZE samples""",
            )
        async with database.pool.acquire() as connection:
            for mode in ('force_custom_plan', 'force_generic_plan'):
                async with connection.transaction():
                    await connection.execute(f'SET LOCAL plan_cache_mode = {mode}')
                    await connection.execute(f'PREPARE unlinked_count AS {unlinked_objects.COUNT_SQL}')
                    try:
                        plan = await connection.fetchval('EXPLAIN (FORMAT JSON) EXECUTE unlinked_count(1)')
                        assert 'idx_samples_unlinked_objects' in plan, (mode, plan)
                        assert await connection.fetchval('EXECUTE unlinked_count(1)') == 3
                    finally:
                        await connection.execute('DEALLOCATE unlinked_count')
        assert (await datasets.find_unlinked_objects(1, USER))['kinds']['image_gone'] == 3

    registry_database(scenario, pool_size=8)


def test_the_index_advisor_keeps_the_unlinked_objects_index(registry_database):
    async def scenario(database):
        snapshot = await index_advisor.collect(database.pool, None)
        review, _ = index_advisor.build_review(snapshot, None)
        needed = {item['name']: item['needed'] for item in review['needed']}
        assert any('orphan finder' in reason for reason in needed['idx_samples_unlinked_objects'])

    registry_database(scenario)


def test_the_parent_id_field_is_not_renamed_cleared_or_overwritten_on_a_dataset_with_objects(registry_database):
    """parent_id names each object's image: renaming or clearing it, or copying another field over it, is refused
    when the dataset has object samples, in the background too; copying it elsewhere and re-pointing one object (a
    PATCH, a bulk set) stay allowed. A dataset without objects is unaffected."""

    async def scenario(database):
        await add(1, [(None, {'crop': 'corn', 'note': 'x'}), (None, {'crop': 'soy'})], 's3://f/img')
        corn, soy = await ids(database, 1, 's3://f/img')
        await add(1, [('object', {'parent_id': corn})], 's3://f/obj')
        (obj,) = await ids(database, 1, 's3://f/obj')
        rename = datasets.FieldRenameRequest
        refusals = [
            datasets.rename_field(1, 'parent_id', rename(new_name='image_id'), USER),
            datasets.rename_field(1, 'parent_id', rename(new_name='image_id', background=True), USER),
            datasets.rename_field(1, 'note', rename(new_name='parent_id', overwrite=True), USER),
            datasets.clone_field(1, 'note', rename(new_name='parent_id', overwrite=True), USER),
            datasets.clear_field(1, 'parent_id', USER),
            datasets.clear_field(1, 'parent_id', USER, background=True),
        ]
        for refusal in refusals:
            with pytest.raises(HTTPException) as refused:
                await refusal
            assert refused.value.status_code == 409
            assert "names each object sample's image" in refused.value.detail
        assert await column(database, obj) == [corn]
        # Allowed: a copy elsewhere, and re-pointing the object.
        await datasets.clone_field(1, 'parent_id', rename(new_name='parent_copy'), USER)
        await datasets.update_sample(1, obj, datasets.SampleUpdate(metadata={'parent_id': soy}), user=USER)
        assert await column(database, obj) == [soy]
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=[obj], metadata={'parent_id': corn}),
            USER,
        )
        assert await column(database, obj) == [corn]

        # Without objects: every operation as before.
        await add(2, [(None, {'parent_id': 7})], 's3://f/plain')
        await datasets.rename_field(2, 'parent_id', rename(new_name='source_id'), USER)
        await datasets.rename_field(2, 'source_id', rename(new_name='parent_id'), USER)
        await datasets.clear_field(2, 'parent_id', USER)

    registry_database(scenario)


def test_parent_id_stays_in_the_schema_but_out_of_the_sidebar_and_its_facets(registry_database):
    async def scenario(database):
        await add(1, [(None, {'crop': 'corn'})], 's3://s/img')
        (image,) = await ids(database, 1, 's3://s/img')
        await add(1, [('object', {'parent_id': image, 'label': 'weed'})], 's3://s/obj')
        grain = {'logic': 'AND', 'filters': [{'field': 'media_type', 'operator': '=', 'value': 'object'}]}
        for lightning in (True, False):
            schema = await datasets._get_dataset_field_stats_impl(1, grain, lightning)
            fields = {field['name']: field for field in schema}
            assert fields['parent_id']['sidebar'] is False
            assert fields['parent_id']['provenance'] == 'cached'
            assert 'sidebar' not in fields['label']
        # Asked for by name (a facet request), it is answered as any field.
        (facet,) = await datasets._get_dataset_field_stats_impl(1, grain, False, ['parent_id'])
        assert facet['name'] == 'parent_id' and 'sidebar' not in facet

    registry_database(scenario)
