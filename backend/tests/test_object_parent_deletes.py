"""Deleting an image deletes its objects (0030), whatever deletes it. The key (dataset_id, parent_id) cascades for any
delete, psql included, and an object it deletes loses what a direct delete of it removes. The app's own paths delete
the objects first: a sample's delete and a bulk delete count them apart from what was asked, and keep the field caches
right; a dataset's deletion keeps its units within budget; a revert removes only the samples added since the version.
Every delete is logged for followers and statistics."""

import asyncio
import json
import os
import time

import asyncpg
import pytest

from backend import bulk_jobs, dataset_deletion, index_advisor, main
from backend.routers import datasets, versions
from backend.tests import test_bulk_jobs, test_dataset_deletion
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_object_parent_column import add, column, ids

registry_database = registry_helpers.registry_database
USER = registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
HELD = ('samples', 'embeddings', 'embedding_coordinates', 'sample_hashes')


async def image_with_objects(database, dataset_id, prefix, count=2, crop='corn'):
    """An image and `count` objects of it, each object with a vector, a map point and a hash."""
    await add(dataset_id, [(None, {'crop': crop})], f'{prefix}-img')
    (image,) = await ids(database, dataset_id, f'{prefix}-img')
    await add(
        dataset_id,
        [('object', {'parent_id': image, 'label': 'weed', 'only_on_objects': 1}) for _ in range(count)],
        f'{prefix}-obj',
    )
    objects = await ids(database, dataset_id, f'{prefix}-obj')
    await held(database, objects)
    return image, objects


async def held(database, sample_ids):
    """A vector, a map point and a hash for each of these samples."""
    await database.execute(
        "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
        "SELECT dataset_id, id, 'clip_obj', '[1,0]' FROM samples WHERE id = ANY($1::int[])",
        sample_ids,
    )
    await database.execute(
        "INSERT INTO embedding_coordinates (sample_id, field_name, x, y) "
        "SELECT id, 'clip_obj', 0.5, 0.5 FROM samples WHERE id = ANY($1::int[])",
        sample_ids,
    )
    await database.execute(
        "INSERT INTO sample_hashes (sample_id, field_name, hash_hex, hash_bits, prefix) "
        "SELECT id, 'dhash_8', 'ff00', B'1111111100000000', B'1111111100000000' FROM samples WHERE id = ANY($1::int[])",
        sample_ids,
    )


async def left(database, sample_ids) -> dict:
    return {
        table: await database.fetchval(f'SELECT count(*) FROM {table} WHERE sample_id = ANY($1::int[])', sample_ids)
        if table != 'samples'
        else await database.fetchval('SELECT count(*) FROM samples WHERE id = ANY($1::int[])', sample_ids)
        for table in HELD
    }


async def logged(database) -> dict:
    """What followers, statistics and duplicate work were told, by dataset and block."""
    queries = {
        'followers': 'SELECT DISTINCT dataset_id, block_id FROM sample_block_changes',
        'statistics': 'SELECT DISTINCT dataset_id, block_id FROM dataset_stats_pending WHERE block_id >= 0',
        'duplicates': 'SELECT DISTINCT dataset_id, block_id FROM duplicate_pending',
    }
    return {name: sorted(tuple(row) for row in await database.fetch(query)) for name, query in queries.items()}


async def forget(database):
    for table in ('sample_block_changes', 'dataset_stats_pending', 'duplicate_pending'):
        await database.execute(f'DELETE FROM {table}')


def test_deleting_an_image_deletes_its_objects_and_says_how_many(registry_database):
    async def scenario(database):
        await database.execute('UPDATE datasets SET followed = TRUE WHERE id = 1')
        image, objects = await image_with_objects(database, 1, 's3://d/a', count=3)
        await add(1, [(None, {'crop': 'soy'})], 's3://d/lone')
        (lone,) = await ids(database, 1, 's3://d/lone')
        await forget(database)
        answer = await datasets.delete_sample(1, image, USER)
        assert answer == {'message': 'Sample and its 3 objects deleted', 'objects_deleted': 3}
        assert await left(database, [image, *objects]) == dict.fromkeys(HELD, 0)
        # Logged for followers and statistics like the image (one block here: ids are below 4,096).
        assert (await logged(database))['followers'] == [(1, 0)]
        assert (await logged(database))['statistics'] == [(1, 0)]
        # The field caches: a field only the objects had is unregistered; the image's own stays.
        registered = await database.fetch('SELECT field_name FROM dataset_fields WHERE dataset_id = 1')
        fields = {row['field_name'] for row in registered}
        assert 'only_on_objects' not in fields and 'crop' in fields
        assert await datasets.delete_sample(1, lone, USER) == {'message': 'Sample deleted', 'objects_deleted': 0}

        image, objects = await image_with_objects(database, 1, 's3://d/b', count=2)
        assert await datasets.delete_sample(1, objects[0], USER) == {'message': 'Sample deleted', 'objects_deleted': 0}
        assert await column(database, objects[1]) == [image]

    registry_database(scenario)


def test_any_delete_of_an_image_deletes_its_objects_with_what_they_hold(registry_database):
    """A delete outside the app (psql, a script): the key cascades, and each object loses its vector, map point and
    hash as a direct delete of it would. The cascaded rows fire the row and statement triggers: followers, statistics
    and duplicate work see their blocks."""

    async def scenario(database):
        await database.execute('UPDATE datasets SET followed = TRUE WHERE id = 1')
        # The objects in another block than their image, so their own logging shows.
        await database.execute(
            """INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES
               (10, 1, 's3://c/i.jpg', 'image', '{}'),
               (9000, 1, 's3://c/o0', 'object', '{"parent_id": 10}'),
               (9001, 1, 's3://c/o1', 'object', '{"parent_id": "0010"}'),
               (9500, 1, 's3://c/direct', 'object', '{"label": "standalone"}')""",
        )
        await held(database, [9000, 9001, 9500])
        await forget(database)
        await database.execute('DELETE FROM samples WHERE id = 10')
        await database.execute('DELETE FROM samples WHERE id = 9500')
        assert await left(database, [10, 9000, 9001, 9500]) == dict.fromkeys(HELD, 0)
        assert await logged(database) == {
            'followers': [(1, 0), (1, 2)],
            'statistics': [(1, 0), (1, 2)],
            'duplicates': [(1, 0), (1, 8), (1, 9)],
        }

    registry_database(scenario)


def test_a_bulk_delete_takes_its_images_objects_and_counts_each_row_once(registry_database, monkeypatch):
    """deleted_count is what the request selected, objects_deleted the objects that went with their images without
    being selected. An object the request selects in a later unit than its image's still counts as selected."""
    monkeypatch.setattr(bulk_jobs, 'BATCH_SIZE', 2)

    async def delete(**request):
        job_id = test_bulk_jobs.submitted(
            await datasets.bulk_delete_samples(1, datasets.BulkTargetRequest(**request), USER),
        )
        job = await test_bulk_jobs.finish(database_ref[0])
        assert job['id'] == job_id
        return {key: job['result'][key] for key in ('deleted_count', 'objects_deleted')}

    database_ref = []

    async def scenario(database):
        database_ref.append(database)
        corn_a, of_a = await image_with_objects(database, 1, 's3://b/a', count=3)
        corn_b, of_b = await image_with_objects(database, 1, 's3://b/b', count=3)
        soy, of_soy = await image_with_objects(database, 1, 's3://b/c', count=1, crop='soy')
        # Images of a filter in image grain: their objects go too, counted apart.
        images = {
            'logic': 'AND',
            'filters': [
                {'field': 'media_type', 'operator': '!=', 'value': 'object'},
                {'field': 'crop', 'operator': '=', 'value': 'corn'},
            ],
        }
        assert await delete(filters=images) == {'deleted_count': 2, 'objects_deleted': 6}
        assert await left(database, [corn_a, corn_b, *of_a, *of_b]) == dict.fromkeys(HELD, 0)
        assert await column(database, *of_soy) == [soy]
        # The image and its object, by id: the object was selected.
        assert await delete(sample_ids=[soy, *of_soy]) == {'deleted_count': 2, 'objects_deleted': 0}

        # Everything: every object is selected too, though an image's unit comes first and takes them.
        await image_with_objects(database, 1, 's3://b/d', count=3)
        await image_with_objects(database, 1, 's3://b/e', count=1)
        assert await delete(filters={}) == {'deleted_count': 6, 'objects_deleted': 0}

        # An object the request excludes goes with its image all the same (deleting an image deletes its objects).
        image, objects = await image_with_objects(database, 1, 's3://b/f', count=2)
        assert await delete(sample_ids=[image, objects[0]], excluded_ids=[objects[1]]) == {
            'deleted_count': 2,
            'objects_deleted': 1,
        }
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 1') == 0

    registry_database(scenario)


def test_a_dataset_s_deletion_deletes_objects_before_their_images_within_its_budgets(registry_database):
    """An image with 5,000 objects, each with a vector and four patch tiles: the objects linked to an image go in a
    pass of their own, so no unit's delete of an image cascades past the unit's rows, bytes and time."""

    async def scenario(database):
        await database.execute(
            """INSERT INTO samples (id, dataset_id, filepath, media_type, metadata)
               VALUES (1, 2, 'big.jpg', 'image', '{}');
               SELECT setval('samples_id_seq', 1);
               INSERT INTO samples (dataset_id, filepath, media_type, metadata)
               SELECT 2, 'big.jpg#' || g, 'object', '{"parent_id": 1}' FROM generate_series(1, 5000) g;
               INSERT INTO embeddings (dataset_id, sample_id, field_name, vector)
               SELECT 2, s.id, f, '[1,0]' FROM samples s,
                      unnest(ARRAY['clip_obj', 'clip_patch_2x2_r0c0', 'clip_patch_2x2_r0c1', 'clip_patch_2x2_r1c0',
                                   'clip_patch_2x2_r1c1']) f
                WHERE s.dataset_id = 2""",
        )
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 2 AND parent_id = 1') == 5000
        await datasets.delete_dataset(2, USER)
        current = await test_dataset_deletion.claim(database)
        units = []
        while current['state'] != 'succeeded':
            before = await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 2')
            began = time.monotonic()
            current = await test_dataset_deletion.step(database, current)
            after = await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 2')
            units.append((time.monotonic() - began, before - after, current['checkpoint']))
        removing = [(seconds, gone, checkpoint) for seconds, gone, checkpoint in units if gone]
        # Each unit removed exactly the sample rows it named: no image's objects went with it.
        assert all(gone == checkpoint['last_unit_rows'] for _, gone, checkpoint in removing)
        assert all(gone <= dataset_deletion.BATCH_SIZE for _, gone, _ in removing)
        assert all(c.get('last_unit_bytes', 0) <= dataset_deletion.BYTE_BUDGET for _, _, c in units)
        assert all(seconds < dataset_deletion.POLICY.max_unit_seconds for seconds, _, _ in units)
        assert sum(gone for _, gone, _ in removing) == 5001
        assert removing[-1][1] == 1  # the image, last, alone: its objects had gone before
        assert await database.fetchval('SELECT count(*) FROM datasets WHERE id = 2') == 0

    registry_database(scenario)


def test_a_revert_removes_only_the_samples_added_since_and_keeps_the_objects_the_version_has(registry_database):
    """Exact membership: the images added since go with their objects, which were added since too and are counted.
    An object the version has whose image was added since (it was written first) stays, with its vector. An image
    deleted since comes back with its objects, linked again."""

    async def scenario(database):
        old, of_old = await image_with_objects(database, 1, 's3://v/a', count=1)
        upcoming = await database.fetchval('SELECT last_value + 100 FROM samples_id_seq')
        await add(1, [('object', {'parent_id': upcoming, 'label': 'early'})], 's3://v/early')
        (early,) = await ids(database, 1, 's3://v/early')
        await held(database, [early])
        version = await versions.create_version(1, versions.CreateVersionRequest(name='before'))
        await database.execute("SELECT setval('samples_id_seq', $1)", upcoming - 1)
        await add(1, [(None, {'crop': 'soy'})], 's3://v/new')
        (new,) = await ids(database, 1, 's3://v/new')
        assert new == upcoming and await column(database, early) == [new]
        await add(1, [('object', {'parent_id': new}) for _ in range(2)], 's3://v/new-obj')
        added = await ids(database, 1, 's3://v/new-obj')
        assert (await datasets.delete_sample(1, old, USER))['objects_deleted'] == 1

        restored = await versions._restore_version(1, version['id'], False, USER, True)
        assert restored['samples_removed'] == 3  # the new image and its two objects
        assert restored['samples_restored'] == 2  # the old image and its object
        assert await left(database, [new, *added]) == dict.fromkeys(HELD, 0)
        assert await left(database, [early]) == dict.fromkeys(HELD, 1)
        row = await database.fetchrow('SELECT metadata, parent_id FROM samples WHERE id = $1', early)
        assert (row['metadata'], row['parent_id']) == ({'parent_id': upcoming, 'label': 'early'}, None)
        assert await column(database, *of_old) == [old]

    registry_database(scenario)


def test_the_key_refuses_a_parent_the_triggers_would_not_give(registry_database):
    """Nothing in the app writes the column: a direct write is overridden by the object's trigger. With the triggers
    off (a migration's way), the key refuses an image that isn't there, or that is another dataset's."""

    async def scenario(database):
        image, objects = await image_with_objects(database, 1, 's3://k/a', count=1)
        await add(2, [(None, {})], 's3://k/other')
        (other,) = await ids(database, 2, 's3://k/other')
        await database.execute('UPDATE samples SET parent_id = 999999 WHERE id = $1', objects[0])
        assert await column(database, *objects) == [image]
        async with database.pool.acquire() as connection:
            for bad in (999999, other):
                with pytest.raises(asyncpg.ForeignKeyViolationError):
                    async with connection.transaction():
                        await connection.execute('ALTER TABLE samples DISABLE TRIGGER USER')
                        await connection.execute('UPDATE samples SET parent_id = $2 WHERE id = $1', objects[0], bad)
        assert await column(database, *objects) == [image]

    registry_database(scenario)


def test_an_object_written_while_its_image_is_deleted_is_refused_for_a_retry_and_then_has_no_image(registry_database):
    """The object's trigger finds its image; the key finds it gone once the delete commits. The write is refused with a
    409 the SDK retries (Retry-After), and written again the object stays, with no image. Any other key is a fault."""

    async def scenario(database):
        await add(1, [(None, {})], 's3://r/img')
        (image,) = await ids(database, 1, 's3://r/img')
        async with database.pool.acquire() as deleting:
            delete = deleting.transaction()
            await delete.start()
            await deleting.execute('DELETE FROM samples WHERE id = $1', image)
            writing = asyncio.create_task(add(1, [('object', {'parent_id': image})], 's3://r/obj'))
            for _ in range(200):
                waiting = await database.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE '%samples%'",
                )
                if waiting:
                    break
                await asyncio.sleep(0.05)
            await delete.commit()
        with pytest.raises(asyncpg.ForeignKeyViolationError) as refused:
            await writing
        answer = await main.foreign_key_error(None, refused.value)  # type: ignore[arg-type]
        assert (answer.status_code, answer.headers['Retry-After']) == (409, '1')
        assert 'deleted while they were written; retry' in json.loads(answer.body)['detail']
        await add(1, [('object', {'parent_id': image})], 's3://r/obj')
        (kept,) = await ids(database, 1, 's3://r/obj')
        assert await column(database, kept) == [None]

        other = asyncpg.ForeignKeyViolationError('another key')
        other.constraint_name = 'embeddings_sample_id_fkey'
        assert (await main.foreign_key_error(_Request(), other)).status_code == 500  # type: ignore[arg-type]

    registry_database(scenario)


class _Request:
    method = 'POST'
    headers: dict = {}

    class url:
        path = '/test'


def test_the_index_advisor_counts_the_parent_index_as_the_keys(registry_database):
    async def scenario(database):
        snapshot = await index_advisor.collect(database.pool, None)
        review, _ = index_advisor.build_review(snapshot, None)
        needed = {item['name']: item['needed'] for item in review['needed']}
        assert any('samples_parent_fkey' in reason for reason in needed['idx_samples_dataset_parent'])

    registry_database(scenario)


def test_a_large_delete_of_images_deletes_and_logs_every_object(registry_database):
    """10,000 images with 10 objects each: a bulk delete of the images, and a plain one of 1,000 more whose objects go
    by the key. Every object is gone and every block of theirs is logged; the times are printed (-s)."""

    async def scenario(database):
        await database.execute('UPDATE datasets SET followed = TRUE WHERE id = 1')
        async with registry_helpers.bulk_setup(database) as connection:
            await connection.execute(
                """INSERT INTO samples (id, dataset_id, filepath, media_type, metadata)
                   SELECT g, 1, 'img-' || g || '.jpg', 'image', '{}' FROM generate_series(1, 11000) g;
                   INSERT INTO samples (id, dataset_id, filepath, media_type, metadata)
                   SELECT 100000 + 10 * g + k, 1, 'img-' || g || '.jpg#' || k, 'object',
                          jsonb_build_object('parent_id', g)
                     FROM generate_series(1, 11000) g, generate_series(0, 9) k""",
            )
        assert await database.fetchval('SELECT count(parent_id) FROM samples') == 110000
        await forget(database)
        began = time.monotonic()
        test_bulk_jobs.submitted(
            await datasets.bulk_delete_samples(
                1,
                datasets.BulkTargetRequest(filters={'field': 'id', 'operator': '<=', 'value': 10000}),
                USER,
            ),
        )
        job = await test_bulk_jobs.finish(database)
        bulk = time.monotonic() - began
        assert (job['result']['deleted_count'], job['result']['objects_deleted']) == (10000, 100000)
        began = time.monotonic()
        await database.execute('DELETE FROM samples WHERE id > 10000 AND id <= 11000')
        plain = time.monotonic() - began
        assert await database.fetchval('SELECT count(*) FROM samples') == 0
        blocks = {block for _, block in (await logged(database))['followers']}
        assert blocks >= {(100000 + 10 * g) // 4096 for g in range(1, 11001)}
        print(
            f'\n10,000 images, 100,000 objects by a bulk delete: {bulk:.1f} s; 1,000 and 10,000 by psql: {plain:.1f} s',
        )

    registry_database(scenario, pool_size=8)
