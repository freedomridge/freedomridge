"""A mirror and the key that deletes an image's objects with it (0030). The mirror deletes what its primary no longer
has, and before it deletes an image it detaches the objects it keeps (metadata.parent_id set to JSON null, selected
by samples.parent_id), so the key never deletes an object the primary still has: a primary on 2.10.x or older keeps an
image's objects when it deletes the image; a 2.11 one deletes them through their own blocks, or re-points them. The
objects it keeps keep their sample ids, and with them what this instance added (tags, `@local.` fields)."""

import json
import os

import pytest

from backend import follower_sync
from backend.follower_sync import SAMPLE_COLUMNS
from backend.routers import datasets, followers
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_follower_sync import copied, mirror_of, setup, sync
from backend.tests.test_object_parent_column import add, column, ids

registry_database = registry_helpers.registry_database
USER = registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def followed(monkeypatch, database):
    """A primary dataset 1 with images corn and soy and three objects of corn, copied to a mirror. Returns the link,
    the primary's ids by name, and the mirror's dataset id."""
    _, link_id = await setup(monkeypatch, database)
    await add(1, [(None, {'crop': 'corn'}), (None, {'crop': 'soy'})], 's3://m/img')
    corn, soy = await ids(database, 1, 's3://m/img')
    await add(1, [('object', {'parent_id': corn, 'label': f'weed{n}'}) for n in range(3)], 's3://m/obj')
    objects = await ids(database, 1, 's3://m/obj')
    follower = await database.fetchval('SELECT id FROM followers')
    await followers.follow_dataset(follower, 1, followers.FollowOptions(tags='both'), user=USER)
    await sync(database, link_id)
    mirror = (await mirror_of(database))['dataset_id']
    here = await copied(database, await mirror_of(database))
    assert await column(database, *(here[o]['id'] for o in objects)) == [here[corn]['id']] * 3
    return link_id, {'corn': corn, 'soy': soy, 'objects': objects}, mirror


async def same_as_primary(database, mirror):
    """The mirror's rows, by the primary's ids, as the primary has them (parent ids translated)."""
    here = await copied(database, await mirror_of(database))
    primary = {
        row['id']: row
        for row in await database.fetch('SELECT id, filepath, metadata FROM samples WHERE dataset_id = 1 ORDER BY id')
    }
    assert sorted(here) == sorted(primary)
    local_id = {origin: row['id'] for origin, row in here.items()}
    for origin, row in primary.items():
        expected = dict(row['metadata'] or {})
        if 'parent_id' in expected:
            expected['parent_id'] = local_id.get(expected['parent_id'])
        assert here[origin]['metadata'] == expected, origin
    return here


def test_an_image_its_primary_deletes_goes_from_the_mirror_with_its_objects(monkeypatch, registry_database):
    async def scenario(database):
        link_id, primary, mirror = await followed(monkeypatch, database)
        assert (await datasets.delete_sample(1, primary['corn'], USER))['objects_deleted'] == 3
        await sync(database, link_id)
        here = await same_as_primary(database, mirror)
        assert sorted(here) == [primary['soy']]

    registry_database(scenario)


def test_the_objects_its_primary_keeps_or_re_points_stay_on_the_mirror_as_they_were(monkeypatch, registry_database):
    """One sync: the primary re-pointed one object to another image and detached another, then deleted their image
    (and with it the third). The mirror's two keep their sample ids and what this instance added to them."""

    async def scenario(database):
        link_id, primary, mirror = await followed(monkeypatch, database)
        moved, kept, gone = primary['objects']
        before = await copied(database, await mirror_of(database))
        local = datasets.SampleUpdate(metadata={'@local.checked': True})
        await datasets.update_sample(mirror, before[moved]['id'], local, user=USER)
        await datasets.update_sample(mirror, before[kept]['id'], local, user=USER)
        update = datasets.SampleUpdate
        await datasets.update_sample(1, moved, update(metadata={'parent_id': primary['soy']}), user=USER)
        await datasets.update_sample(1, kept, update(metadata={'parent_id': None}), user=USER)
        assert (await datasets.delete_sample(1, primary['corn'], USER))['objects_deleted'] == 1
        await sync(database, link_id)
        here = await same_as_primary(database, mirror)
        assert sorted(here) == sorted([primary['soy'], moved, kept])
        assert (here[moved]['id'], here[kept]['id']) == (before[moved]['id'], before[kept]['id'])
        assert await column(database, here[moved]['id'], here[kept]['id']) == [here[primary['soy']]['id'], None]
        assert [
            row['local_metadata']
            for row in await database.fetch(
                'SELECT local_metadata FROM samples WHERE id = ANY($1::int[]) ORDER BY id',
                [here[moved]['id'], here[kept]['id']],
            )
        ] == [{'checked': True}] * 2

    registry_database(scenario)


def test_a_mirror_of_an_older_primary_keeps_the_objects_it_keeps_when_it_deletes_their_image(
    monkeypatch,
    registry_database,
):
    """A primary on 2.10.x or older has no key: it deletes the image and keeps its objects, naming the image still. Its
    feed sends the image's block without the image, and with the objects of that block as they were; an object of
    another block isn't sent at all. The mirror deletes the image only: each object stays, with its sample id, its own
    fields and no image, as a copy of an object whose image isn't here.

    The detach, as settled: the children are selected by the column (an object naming a deleted object has no image,
    so its metadata is left as the primary has it); in the transaction that deletes, before the delete (rolled back,
    nothing changed; and the key would have deleted the children with their image otherwise); metadata.parent_id
    becomes JSON null; the children keep their ids and their `@local.` fields."""

    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await database.execute(
            """INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES
               (10, 1, 's3://o/i.jpg', 'image', '{}'),
               (11, 1, 's3://o/i.jpg#0', 'object', '{"parent_id": 10}'),
               (14, 1, 's3://o/i.jpg#2', 'object', '{"label": "weed"}'),
               (5000, 1, 's3://o/i.jpg#1', 'object', '{"parent_id": 10}'),
               (5001, 1, 's3://o/i.jpg#2#0', 'object', '{"parent_id": 14}')""",
        )
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(tags='both'), user=USER)
        await sync(database, link_id)
        mirror = (await mirror_of(database))['dataset_id']
        here = await copied(database, await mirror_of(database))
        image, near, far, named = here[10]['id'], here[11]['id'], here[5000]['id'], here[5001]['id']
        assert await column(database, near, far, named) == [image, image, None]
        for sample in (near, far):
            await datasets.update_sample(mirror, sample, datasets.SampleUpdate(metadata={'@local.weeds': 2}), user=USER)
        rows_of = 'SELECT id, metadata, parent_id, local_metadata FROM samples WHERE id = ANY($1::int[]) ORDER BY id'
        before = {row['id']: dict(row) for row in await database.fetch(rows_of, [image, near, far, named])}
        assert before[named]['metadata'] == {'parent_id': here[14]['id']}

        # Block 0 as a primary on 2.10.x or older sends it after deleting the image and object 14: object 11 only,
        # still naming 10.
        # Block 1 (5000, and 5001 naming the deleted object 14) isn't sent: nothing there changed.
        columns = ', '.join(name for name, _ in SAMPLE_COLUMNS)
        rows = [
            json.loads(row[0])
            for row in await database.fetch(
                f'SELECT row_to_json(s)::text FROM (SELECT {columns} FROM samples WHERE id = 11) s',
            )
        ]
        feed = {'array_fields': [], 'samples': rows}

        class Abandoned(Exception):
            pass

        with pytest.raises(Abandoned):
            async with database.pool.acquire() as connection, connection.transaction():
                await follower_sync.begin_mirror_write(connection, mirror)
                await follower_sync.write_samples(connection, mirror, [0], feed)
                raise Abandoned
        assert {row['id']: dict(row) for row in await database.fetch(rows_of, [image, near, far, named])} == before

        async with database.pool.acquire() as connection, connection.transaction():
            await follower_sync.begin_mirror_write(connection, mirror)
            await follower_sync.write_samples(connection, mirror, [0], feed)

        assert (
            await database.fetchval('SELECT count(*) FROM samples WHERE id = ANY($1::int[])', [image, here[14]['id']])
            == 0
        )
        kept = {row['id']: row for row in await database.fetch(rows_of, [near, far, named])}
        assert sorted(kept) == sorted([near, far, named])
        assert [kept[sample]['parent_id'] for sample in (near, far, named)] == [None, None, None]
        assert [kept[sample]['local_metadata'] for sample in (near, far, named)] == [{'weeds': 2}, {'weeds': 2}, {}]
        # JSON null, not a missing key; the object that named an object is as the primary has it.
        nulls = "SELECT id FROM samples WHERE id = ANY($1::int[]) AND jsonb_typeof(metadata -> 'parent_id') = 'null'"
        assert sorted(row['id'] for row in await database.fetch(nulls, [near, far, named])) == [near, far]
        assert kept[named]['metadata'] == before[named]['metadata']
        origin = {
            row['sample_id']: row['origin_parent_id']
            for row in await database.fetch(
                'SELECT sample_id, origin_parent_id FROM mirror_samples WHERE dataset_id = $1',
                mirror,
            )
        }
        assert (origin[near], origin[far]) == (10, 10)  # a re-point if the primary restores the image

    registry_database(scenario)
