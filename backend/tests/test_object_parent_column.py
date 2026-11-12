"""samples.parent_id (0029): an object's image, kept by triggers from metadata.parent_id, which stays what clients
write and read. The column names the sample metadata.parent_id names when that is a non-object of the object's own
dataset, and is NULL otherwise and on every non-object, whichever is written first and whatever writes it. No write
fails because of it. Nothing reads it yet."""

import os
import time

import asyncpg
import pytest
from fastapi import HTTPException

from backend import follower_sync
from backend.routers import datasets, followers, versions
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_follower_sync import copied, mirror_of, setup, sync

registry_database = registry_helpers.registry_database
ingest, USER = registry_helpers.ingest, registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def add(dataset_id, samples, prefix):
    """add_samples, as a client sends them: an object's filepath and media_type, and its metadata."""
    return await datasets.add_samples(
        dataset_id,
        {
            'samples': [
                {'filepath': f'{prefix}-{index}.jpg', **({'media_type': kind} if kind else {}), 'metadata': metadata}
                for index, (kind, metadata) in enumerate(samples)
            ],
        },
        user=USER,
    )


async def ids(database, dataset_id, prefix):
    return [
        row['id']
        for row in await database.fetch(
            'SELECT id FROM samples WHERE dataset_id = $1 AND filepath LIKE $2 ORDER BY filepath',
            dataset_id,
            prefix + '-%',
        )
    ]


async def column(database, *sample_ids):
    found = dict(await database.fetch('SELECT id, parent_id FROM samples WHERE id = ANY($1::int[])', sample_ids))
    return [found[sample_id] for sample_id in sample_ids]


def test_the_column_follows_every_write_of_an_object_and_its_image_and_never_fails_one(registry_database):
    async def scenario(database):
        await add(1, [(None, {}), (None, {})], 's3://p/img')
        await add(2, [(None, {})], 's3://p/other')
        first, second = await ids(database, 1, 's3://p/img')
        (other,) = await ids(database, 2, 's3://p/other')
        await add(1, [('object', {'parent_id': first, 'label': 'weed'})], 's3://p/a')
        (on_first,) = await ids(database, 1, 's3://p/a')
        await add(
            1,
            [
                ('object', {'parent_id': str(second)}),  # a string names it too
                ('object', {'parent_id': other}),  # an image of another dataset
                ('object', {'parent_id': on_first}),  # another object
                ('object', {'parent_id': 99_999_999}),  # nothing
                ('object', {'parent_id': 99_999_999_999}),  # beyond INTEGER
                ('object', {'parent_id': -first}),
                ('object', {'parent_id': first + 0.5}),
                ('object', {'parent_id': 'abc'}),
                ('object', {'parent_id': None}),
                ('object', {'label': 'standalone'}),
                (None, {'parent_id': first}),  # a crop made by to_patches: an image, never linked
            ],
            's3://p/b',
        )
        made = await ids(database, 1, 's3://p/b')
        assert await column(database, first, second, on_first, *made) == [None, None, first, second] + [None] * 10

        # metadata.parent_id changed through the API: a sample's PATCH, a bulk set, a removed key.
        update = datasets.SampleUpdate
        await datasets.update_sample(1, on_first, update(metadata={'parent_id': second}), user=USER)
        assert await column(database, on_first) == [second]
        await datasets.update_sample(1, on_first, update(metadata={'parent_id': 'junk'}), user=USER)
        assert await column(database, on_first) == [None]
        bulk = datasets.BulkSetRequest
        await datasets.bulk_set_metadata(1, bulk(sample_ids=[on_first], metadata={'parent_id': first}), user=USER)
        assert await column(database, on_first) == [first]
        await datasets.update_sample(1, on_first, update(remove_metadata_keys=['parent_id']), user=USER)
        assert await column(database, on_first) == [None]
        await datasets.update_sample(1, on_first, update(metadata={'parent_id': first}), user=USER)

        # The object's own kind, and a direct write of the column, which the rule overrides.
        await database.execute("UPDATE samples SET media_type = 'image' WHERE id = $1", on_first)
        assert await column(database, on_first) == [None]
        await database.execute("UPDATE samples SET media_type = 'object' WHERE id = $1", on_first)
        assert await column(database, on_first) == [first]
        await database.execute('UPDATE samples SET parent_id = $2 WHERE id = $1', on_first, second)
        await database.execute('UPDATE samples SET parent_id = $2 WHERE id = $1', first, second)
        await database.execute('UPDATE samples SET parent_id = 12345 WHERE id = $1', made[0])
        assert await column(database, on_first, first, made[0]) == [first, None, second]
        await database.execute(
            "INSERT INTO samples (dataset_id, filepath, media_type, parent_id) "
            "VALUES (1, 's3://p/c-0.jpg', 'image', $1)",
            first,
        )
        assert await column(database, *await ids(database, 1, 's3://p/c')) == [None]

        # The image's side: it stops being one, comes back, moves to another dataset.
        await database.execute("UPDATE samples SET media_type = 'object' WHERE id = $1", first)
        assert await column(database, on_first) == [None]
        await database.execute("UPDATE samples SET media_type = 'image' WHERE id = $1", first)
        assert await column(database, on_first) == [first]
        await add(2, [('object', {'parent_id': first})], 's3://p/d')
        (waiting_in_two,) = await ids(database, 2, 's3://p/d')
        assert await column(database, waiting_in_two) == [None]
        # An image with objects stays in its dataset: the key refuses the move (0030). Without them it moves, and the
        # objects that name it in its new dataset gain it.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await database.execute('UPDATE samples SET dataset_id = 2 WHERE id = $1', first)
        await datasets.update_sample(1, on_first, update(metadata={'parent_id': second}), user=USER)
        await database.execute('UPDATE samples SET dataset_id = 2 WHERE id = $1', first)
        assert await column(database, on_first, waiting_in_two) == [second, first]

    registry_database(scenario)


def test_an_image_written_after_its_objects_links_them(registry_database):
    """Two requests in either order, a script, a restore: the objects wait with no image, and its arrival links the
    ones of its dataset (an insert of images with none waiting writes nothing)."""

    async def scenario(database):
        upcoming = await database.fetchval("SELECT last_value + 100 FROM samples_id_seq")
        await add(1, [('object', {'parent_id': upcoming}), ('object', {'parent_id': str(upcoming)})], 's3://w/a')
        await add(2, [('object', {'parent_id': upcoming})], 's3://w/b')
        waiting = await ids(database, 1, 's3://w/a') + await ids(database, 2, 's3://w/b')
        assert await column(database, *waiting) == [None, None, None]
        await database.execute("SELECT setval('samples_id_seq', $1)", upcoming - 1)
        await add(1, [(None, {})], 's3://w/img')
        assert await ids(database, 1, 's3://w/img') == [upcoming]
        assert await column(database, *waiting) == [upcoming, upcoming, None]  # not the other dataset's

        # In one statement, the object before its image.
        await database.execute(
            """INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES
               (900001, 1, 's3://w/o.jpg', 'object', '{"parent_id": 900002}'),
               (900002, 1, 's3://w/i.jpg', 'image', '{}')""",
        )
        assert await column(database, 900001) == [900002]

    registry_database(scenario)


def test_leading_zeros_name_the_same_image_whichever_is_written_first(registry_database):
    """The object's own trigger reads "0012" as 12, and so does its image's arrival or change: the waiting objects are
    found by their parent_id with leading zeros trimmed (idx_samples_parent_id). A value of zeros names nothing (ids
    start at 1); "+12", " 12" and "12.0" are no id to either side."""

    async def scenario(database):
        upcoming = await database.fetchval("SELECT last_value + 100 FROM samples_id_seq")
        values = [f'00{upcoming}', f'0{upcoming}', '000', '0', 0, f'+{upcoming}', f' {upcoming}', f'{upcoming}.0']
        await add(1, [('object', {'parent_id': value}) for value in values], 's3://z/o')
        waiting = await ids(database, 1, 's3://z/o')
        assert await column(database, *waiting) == [None] * len(values)
        await database.execute("SELECT setval('samples_id_seq', $1)", upcoming - 1)
        await add(1, [(None, {})], 's3://z/img')
        assert await ids(database, 1, 's3://z/img') == [upcoming]
        assert await column(database, *waiting) == [upcoming, upcoming] + [None] * 6
        await add(1, [('object', {'parent_id': f'000{upcoming}'})], 's3://z/late')
        assert await column(database, *await ids(database, 1, 's3://z/late')) == [upcoming]
        await database.execute("UPDATE samples SET media_type = 'object' WHERE id = $1", upcoming)
        assert await column(database, *waiting[:2]) == [None, None]
        await database.execute("UPDATE samples SET media_type = 'image' WHERE id = $1", upcoming)
        assert await column(database, *waiting) == [upcoming, upcoming] + [None] * 6

    registry_database(scenario)


def test_an_image_never_writes_another_datasets_objects(registry_database):
    """Objects of other datasets whose junk parent_id is the id a new image gets aren't written by its insert: a
    followed dataset logs nothing for them, and a mirror or a dataset being deleted doesn't refuse the insert."""

    async def scenario(database):
        upcoming = await database.fetchval("SELECT last_value + 100 FROM samples_id_seq")
        await database.execute("INSERT INTO datasets (id, name) VALUES (3, 'mirror'), (4, 'going')")
        for dataset_id in (2, 3, 4):
            await add(dataset_id, [('object', {'parent_id': upcoming})], f's3://x/{dataset_id}')
        others = [(await ids(database, dataset_id, f's3://x/{dataset_id}'))[0] for dataset_id in (2, 3, 4)]
        await database.execute('UPDATE datasets SET followed = TRUE WHERE id = 2')
        await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (3, 7, 0)')
        await database.execute('UPDATE datasets SET deleting = TRUE WHERE id = 4')
        await database.execute('DELETE FROM sample_block_changes')
        versions = 'SELECT id, xmin::text FROM samples WHERE id = ANY($1::int[]) ORDER BY id'
        before = await database.fetch(versions, others)
        await database.execute("SELECT setval('samples_id_seq', $1)", upcoming - 1)
        await add(1, [(None, {})], 's3://x/img')
        assert await ids(database, 1, 's3://x/img') == [upcoming]
        assert await database.fetch(versions, others) == before  # the same row versions: never written
        assert await database.fetchval('SELECT count(*) FROM sample_block_changes') == 0
        assert await column(database, *others) == [None, None, None]

    registry_database(scenario)


def test_a_revert_that_restores_an_image_and_its_objects_links_them(registry_database):
    async def scenario(database):
        # The objects come first by id, so the restore writes them before their image, which links them as it
        # arrives, one named with zeros in front; link_object_parents after the restore is a safety net.
        upcoming = await database.fetchval("SELECT last_value + 100 FROM samples_id_seq")
        await add(1, [('object', {'parent_id': upcoming}), ('object', {'parent_id': f'00{upcoming}'})], 's3://r/o')
        await database.execute("SELECT setval('samples_id_seq', $1)", upcoming - 1)
        await add(1, [(None, {})], 's3://r/img')
        objects = await ids(database, 1, 's3://r/o')
        assert await column(database, *objects) == [upcoming, upcoming]
        version = await versions.create_version(1, versions.CreateVersionRequest(name='with them'))
        await database.execute('DELETE FROM samples WHERE dataset_id = 1')
        for exact in (False, True):
            restored = await versions._restore_version(1, version['id'], True, USER, exact)
            assert restored['samples_restored'] == 3
            assert await column(database, *objects) == [upcoming, upcoming]
            await database.execute('DELETE FROM samples WHERE dataset_id = 1')

    registry_database(scenario)


def test_an_array_field_named_parent_id_is_refused_here_and_from_a_primary(registry_database):
    async def scenario(database):
        # On a dataset of objects, where parent_id is also a metadata field, and on one without.
        await add(1, [(None, {}), ('object', {'parent_id': 1})], 's3://n/a')
        for dataset_id in (1, 2):
            with pytest.raises(HTTPException) as refused:
                request = datasets.CreateArrayFieldRequest(field_name='parent_id')
                await datasets.create_array_field(dataset_id, request, user=USER)
            assert refused.value.status_code == 400 and 'reserved' in refused.value.detail
        # A primary on 2.10.x or older may have one: the follower names it and says where to rename it.
        with pytest.raises(ValueError) as refused_sync:
            await follower_sync.ensure_array_columns(database.pool, 1, ['parent_id'])
        assert str(refused_sync.value) == (
            "The primary sent an array field this instance cannot hold: 'parent_id' is already a column of the "
            "samples here. Rename the field 'parent_id' on the primary (copy it to a field of another name there and "
            "delete it)."
        )
        assert await database.fetchval('SELECT array_agg(column_name) FROM sample_array_columns') == ['tags']

    registry_database(scenario)


def test_a_local_write_on_a_mirror_is_accepted_and_leaves_the_column(registry_database):
    """A write of a mirror's own fields may change local_metadata and local_tags only (samples_mirror_local_guard);
    the column isn't re-derived by it, even where the rule would give another value."""

    async def scenario(database):
        await add(2, [(None, {})], 's3://m/img')
        (image,) = await ids(database, 2, 's3://m/img')
        await add(2, [('object', {'parent_id': image}), ('object', {'parent_id': image})], 's3://m/o')
        objects = await ids(database, 2, 's3://m/o')
        await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')
        await database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = 2")
        # A column the rule would change (as a revert of this instance's own field never could): any re-derivation by
        # a local write would change the row beyond its own fields, and the guard would refuse it.
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('fridge.mirror_sync', '2', TRUE)")
            await connection.execute('ALTER TABLE samples DISABLE TRIGGER trg_samples_parent_update')
            await connection.execute('UPDATE samples SET parent_id = NULL WHERE id = ANY($1::int[])', objects)
            await connection.execute('ALTER TABLE samples ENABLE TRIGGER trg_samples_parent_update')
        await datasets.update_sample(2, objects[0], datasets.SampleUpdate(metadata={'@local.weeds': 3}), user=USER)
        bulk = datasets.BulkSetRequest
        await datasets.bulk_set_metadata(2, bulk(sample_ids=objects, metadata={'@local.checked': True}), user=USER)
        rows = await database.fetch(
            'SELECT local_metadata, parent_id, metadata FROM samples WHERE id = ANY($1::int[]) ORDER BY id',
            objects,
        )
        assert [row['local_metadata'] for row in rows] == [{'weeds': 3, 'checked': True}, {'checked': True}]
        assert [(row['parent_id'], row['metadata']) for row in rows] == [(None, {'parent_id': image})] * 2

    registry_database(scenario)


def test_on_a_mirror_an_object_copied_before_its_image_is_linked_when_it_arrives(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        upcoming = await database.fetchval("SELECT last_value + 100 FROM samples_id_seq")
        await add(1, [('object', {'parent_id': upcoming, 'label': 'weed'})], 's3://images/objects')
        (child,) = await ids(database, 1, 's3://images/objects')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(tags='both'), user=USER)
        await sync(database, link_id)
        mirror = await mirror_of(database)
        here = await copied(database, mirror)
        assert here[child]['metadata']['parent_id'] is None  # its image isn't on the primary yet
        assert await column(database, child, here[child]['id']) == [None, None]

        # The image arrives on the primary, which links its object there, and then here.
        await database.execute("SELECT setval('samples_id_seq', $1)", upcoming - 1)
        await ingest([{'crop': 'soy'}], dataset_id=1, prefix='s3://images/b')
        assert await column(database, child) == [upcoming]
        await sync(database, link_id)
        here = await copied(database, mirror)
        assert here[child]['metadata']['parent_id'] == here[upcoming]['id']
        assert await column(database, here[child]['id']) == [here[upcoming]['id']]

    registry_database(scenario)


def test_ingest_throughput_is_unchanged(registry_database):
    """Images, then objects naming them, added through add_samples with what this column adds to each write (its
    triggers and its index) and without it, in turns. The image's side probes once per insert statement; an object's
    own trigger reads its image by primary key."""
    added = {
        'trg_samples_parent_insert': 'TRIGGER trg_samples_parent_insert ON samples',
        'trg_samples_parent_update': 'TRIGGER trg_samples_parent_update ON samples',
        'trg_samples_children_insert': 'TRIGGER trg_samples_children_insert ON samples',
        'trg_samples_children_update': 'TRIGGER trg_samples_children_update ON samples',
        'idx_samples_dataset_parent': 'INDEX idx_samples_dataset_parent',
    }

    async def scenario(database):
        definitions = [
            await database.fetchval(
                """SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgname = $1 AND tgrelid = 'samples'::regclass
                   UNION ALL SELECT indexdef FROM pg_indexes WHERE indexname = $1 AND schemaname = current_schema()""",
                name,
            )
            for name in added
        ]
        await add(1, [(None, {}) for _ in range(500)], 's3://t/warm')
        timings: dict = {'with': [], 'without': []}
        for round_ in range(3):
            for state in ('without', 'with') if round_ % 2 == 0 else ('with', 'without'):
                if state == 'without':
                    for target in added.values():
                        await database.execute(f'DROP {target}')
                prefix = f's3://t/{state}{round_}'
                began = time.perf_counter()
                await add(1, [(None, {'n': n}) for n in range(3000)], prefix + '-img')
                images = time.perf_counter() - began
                parents = await ids(database, 1, prefix + '-img')
                began = time.perf_counter()
                await add(1, [('object', {'parent_id': parent}) for parent in parents], prefix + '-obj')
                timings[state].append((images, time.perf_counter() - began))
                if state == 'without':
                    for definition in definitions:
                        await database.execute(definition)
        best = {state: [min(run[part] for run in runs) for part in (0, 1)] for state, runs in timings.items()}
        print(
            '\n3,000 images, then 3,000 objects, through add_samples (best of 3, s): without the column '
            f"{best['without'][0]:.3f} / {best['without'][1]:.3f}, with it {best['with'][0]:.3f} / "
            f"{best['with'][1]:.3f}",
        )
        for part in (0, 1):
            assert best['with'][part] < best['without'][part] * 1.25 + 0.2, timings
        linked = "SELECT count(*) FROM samples WHERE media_type = 'object' AND parent_id IS NOT NULL"
        assert await database.fetchval(linked) == 9000  # the objects added with it

    registry_database(scenario, pool_size=5)


def test_no_value_of_parent_id_fails_a_write(registry_database):
    """Any JSON a script can store there, on insert and on update: the column is NULL, and nothing raises."""

    async def scenario(database):
        values = ['1', '"1"', 'null', '"x"', '-5', '1.5', '99999999999', '"0x1f"', '[1]', '{"a": 1}', 'true', '""']
        for index, value in enumerate(values):
            await database.execute(
                "INSERT INTO samples (dataset_id, filepath, media_type, metadata) "
                "VALUES (1, $1, 'object', jsonb_build_object('parent_id', $2::jsonb))",
                f's3://j/{index}.jpg',
                value,
            )
        await database.execute(
            "UPDATE samples SET metadata = jsonb_build_object('parent_id', metadata->'parent_id', 'v', 2) "
            "WHERE filepath LIKE 's3://j/%'",
        )
        unlinked = "SELECT count(*) FROM samples WHERE filepath LIKE 's3://j/%' AND parent_id IS NULL"
        assert await database.fetchval(unlinked) == len(values)

    registry_database(scenario)
