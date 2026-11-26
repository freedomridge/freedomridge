"""The record of which kind of sample (image, object) has been seen with which field (backend/field_kinds.py, schema.sql
0044), and the reader's use of it, for speed only: each write path marks the kind in its own transaction and a filter
finds the value at once, the job examines a dataset from before the record and clears a mark once no sample of the kind
has the field, a first value racing that clearing keeps its mark, a reading whose record changed reads it again, and
a filter gives the same rows with the record as without it."""

import asyncio
import io
import json
import os
import random
from typing import Any

import asyncpg
import pytest

from backend import bulk_jobs, field_kinds, job_queue, statistics
from backend.routers import datasets, versions
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_object_parent_column import add, ids

registry_database = registry_helpers.registry_database
USER = registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
SQUARE = [[0, 0], [5, 0], [5, 5], [0, 5]]


def wrapped(stages: list, kind: str, partner: str) -> dict:
    """The filter the page's grid sends for these stages (FilterContext.withGrain, a dataset with objects)."""
    predicate = {'field': 'media_type', 'operator': '=' if kind == 'object' else '!=', 'value': 'object'}
    if not stages:
        return {'logic': 'AND', 'filters': [predicate]}
    base = {'logic': 'AND', 'filters': stages}
    coupled = [
        {'field': '_cgself', 'operator': 'cg_self', 'value': base},
        {'field': '_crossgrain', 'operator': partner, 'value': base},
    ]
    return {'logic': 'AND', 'filters': [predicate, {'logic': 'OR', 'filters': coupled}]}


def images(*stages: dict) -> dict:
    return wrapped(list(stages), 'image', 'has_matching_object')


def objects(*stages: dict) -> dict:
    return wrapped(list(stages), 'object', 'parent_matches')


def eq(field: str, value: Any) -> dict:
    return {'field': field, 'operator': '=', 'value': value}


def leaf(field: str, operator: str, value: Any = None) -> dict:
    return {'field': field, 'operator': operator, 'value': value}


def NOT(*parts: dict) -> dict:
    return {'logic': 'NOT', 'filters': list(parts)}


def AND(*parts: dict) -> dict:
    return {'logic': 'AND', 'filters': list(parts)}


def OR(*parts: dict) -> dict:
    return {'logic': 'OR', 'filters': list(parts)}


async def found(dataset_id: int, node: dict) -> set[int]:
    """The rows the samples query lists for the filter, its count agreeing."""
    body = await datasets.list_samples_post(dataset_id, datasets.SamplesRequest(filters=node, limit=1000))
    rows = {item['id'] for item in body['items']}
    assert body['total'] == len(rows) == len(body['items']), node
    return rows


async def record(database, dataset_id: int = 1) -> dict[str, tuple[bool, bool]]:
    rows = await database.fetch(
        'SELECT field_name, on_images, on_objects FROM dataset_field_kinds WHERE dataset_id = $1',
        dataset_id,
    )
    return {row['field_name']: (row['on_images'], row['on_objects']) for row in rows}


async def members(dataset_id: int = 1) -> dict[str, tuple[bool, bool]]:
    """The API's on_images and on_objects, by field (field-stats)."""
    fields = await datasets._get_dataset_field_stats_impl(dataset_id, None)
    return {field['name']: (field['on_images'], field['on_objects']) for field in fields if 'sample_count' in field}


async def run_jobs(database, kind: str, handler: Any) -> list[dict]:
    """The queued jobs of the kind, each run to its end as a runner slot runs it."""
    finished = []
    while job := await job_queue.claim_job(database.pool, 'field-kinds-test', kinds=[kind]):
        for _ in range(1000):
            unit = await handler(job)
            job = await job_queue.publish_step(
                database.pool,
                job['id'],
                job['lease_token'],
                checkpoint=unit.checkpoint,
                done=unit.done,
                total=unit.total,
                message=unit.message,
                result=unit.result,
                finished=unit.finished,
                publisher=unit.publish,
            )
            if job['state'] != 'running':
                break
        assert job['state'] == 'succeeded', job
        finished.append(job)
    return finished


async def run_job(database, monkeypatch) -> list[dict]:
    """The field kinds jobs the scheduler queues now (datasets to examine, notes left over), run to their end."""
    monkeypatch.setattr(field_kinds, 'LEFTOVER_SECONDS', 0)
    await field_kinds.schedule(database.pool)
    return await run_jobs(database, field_kinds.KIND, field_kinds.handler(database.pool))


async def count_statistics(database, dataset_id: int = 1) -> dict:
    """The dataset's statistics counted, as the scheduler queues them after a write, then the clearing check the count
    queues right after it (field_kinds.queue_check), as the job runner runs it next."""
    await statistics.request_refresh(database.pool, dataset_id)
    (job,) = await run_jobs(database, statistics.KIND, statistics.handler(database.pool))
    await run_jobs(database, field_kinds.KIND, field_kinds.handler(database.pool))
    return job


async def farm(database) -> dict[str, int]:
    """Dataset 1: two images (red and blue) with a car on the red one and a truck on the blue one."""
    await add(1, [(None, {'colour': 'red'}), (None, {'colour': 'blue'})], 'farm/img')
    red, blue = await ids(database, 1, 'farm/img')
    await add(
        1,
        [('object', {'parent_id': red, 'label': 'car'}), ('object', {'parent_id': blue, 'label': 'truck'})],
        'farm/obj',
    )
    car, truck = await ids(database, 1, 'farm/obj')
    return {'red': red, 'blue': blue, 'car': car, 'truck': truck}


# --- Each write path marks the kind in its transaction, and a filter finds the value at once (contract 1.3, 2.3) ---


def test_a_new_dataset_is_examined_from_its_start_and_an_ingest_marks_each_kind(registry_database):
    async def scenario(database):
        assert await database.fetchval('SELECT count(*) FROM dataset_field_kinds_state WHERE dataset_id = 1') == 1
        rows = await farm(database)
        assert await record(database) == {
            'colour': (True, False),
            'label': (False, True),
            'parent_id': (False, True),
        }
        assert (await members())['colour'] == (True, False)
        assert (await members())['label'] == (False, True)
        # The first `weight` any object gets, found at once on both grids.
        await add(1, [('object', {'parent_id': rows['red'], 'weight': 7})], 'farm/heavy')
        (heavy,) = await ids(database, 1, 'farm/heavy')
        assert (await record(database))['weight'] == (False, True)
        assert await found(1, objects(eq('weight', 7))) == {heavy}
        assert await found(1, images(eq('weight', 7))) == {rows['red']}
        # The first `colour` any object gets (an image field until now).
        await add(1, [('object', {'parent_id': rows['blue'], 'colour': 'green'})], 'farm/green')
        (green,) = await ids(database, 1, 'farm/green')
        assert (await record(database))['colour'] == (True, True)
        assert await found(1, objects(eq('colour', 'green'))) == {green}
        assert await found(1, images(eq('colour', 'green'))) == {rows['blue']}

    registry_database(scenario)


def test_a_sample_edit_marks_a_field_and_a_location_and_a_filter_finds_them_at_once(registry_database):
    async def scenario(database):
        rows = await farm(database)
        await datasets.update_sample(1, rows['blue'], datasets.SampleUpdate(metadata={'label': 'kite'}), user=USER)
        assert (await record(database))['label'] == (True, True)
        assert await found(1, images(eq('label', 'kite'))) == {rows['blue']}
        assert await found(1, objects(eq('label', 'kite'))) == {rows['truck']}
        assert await found(1, images(leaf('label', 'is_null'))) == set()
        point = {'type': 'Point', 'coordinates': [1, 1]}
        await datasets.update_sample(1, rows['car'], datasets.SampleUpdate(location=point), user=USER)
        assert (await record(database))['location'] == (False, True)
        place = leaf('location', 'geo_polygon', SQUARE)
        assert await found(1, objects(place)) == {rows['car']}
        assert await found(1, images(place)) == {rows['red']}

    registry_database(scenario)


def test_a_bulk_set_by_ids_and_a_bulk_edit_by_filter_mark_the_kind(registry_database):
    async def scenario(database):
        rows = await farm(database)
        request = datasets.BulkSetRequest(sample_ids=[rows['car']], metadata={'colour': 'silver'})
        await datasets.bulk_set_metadata(1, request, user=USER)
        assert (await record(database))['colour'] == (True, True)
        assert await found(1, objects(eq('colour', 'silver'))) == {rows['car']}
        assert await found(1, images(eq('colour', 'silver'))) == {rows['red']}
        # By filter, in a job: the images of the truck get a first `checked`.
        by_filter = datasets.BulkSetRequest(
            filters=images(eq('label', 'truck')),
            metadata={'checked': True},
            background=True,
        )
        await datasets.bulk_set_metadata(1, by_filter, user=USER)
        await finish(database)
        assert (await record(database))['checked'] == (True, False)
        assert await found(1, images(eq('checked', True))) == {rows['blue']}
        assert await found(1, objects(eq('checked', True))) == {rows['truck']}
        assert bulk_jobs.KIND  # the job ran through bulk_jobs' own handler (finish)

    registry_database(scenario)


def test_a_field_clone_and_a_rename_mark_the_new_names_kinds(registry_database):
    async def scenario(database):
        rows = await farm(database)
        await datasets.clone_field(1, 'label', datasets.FieldRenameRequest(new_name='kind'), user=USER)
        assert (await record(database))['kind'] == (False, True)
        assert await found(1, objects(eq('kind', 'car'))) == {rows['car']}
        await datasets.rename_field(1, 'colour', datasets.FieldRenameRequest(new_name='hue'), user=USER)
        assert (await record(database))['hue'] == (True, False)
        assert await found(1, images(eq('hue', 'blue'))) == {rows['blue']}
        assert await found(1, objects(eq('hue', 'blue'))) == {rows['truck']}

    registry_database(scenario)


def test_a_mirror_sync_marks_the_kinds_on_the_mirror(monkeypatch, registry_database):
    from backend.routers import followers
    from backend.tests.test_follower_sync import mirror_of, setup, sync

    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        rows = await farm(database)
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(tags='both'), user=USER)
        await sync(database, link_id)
        mirror = (await mirror_of(database))['dataset_id']
        assert await record(database, mirror) == {
            'colour': (True, False),
            'label': (False, True),
            'parent_id': (False, True),
        }
        # The primary's truck gets the first `colour` an object has; the next sync updates the mirror's row.
        request = datasets.BulkSetRequest(sample_ids=[rows['truck']], metadata={'colour': 'white'})
        await datasets.bulk_set_metadata(1, request, user=USER)
        await sync(database, link_id)
        assert (await record(database, mirror))['colour'] == (True, True)
        (here,) = await found(mirror, objects(eq('colour', 'white')))
        assert await database.fetchval('SELECT metadata->>$2 FROM samples WHERE id = $1', here, 'label') == 'truck'
        # A mirror's own field, written on one of its objects: marked for objects, found at once.
        local = datasets.SampleUpdate(metadata={'@local.checked': True})
        await datasets.update_sample(mirror, here, local, user=USER)
        assert (await record(database, mirror))['@local.checked'] == (False, True)
        assert (await members(mirror))['@local.checked'] == (False, True)
        assert await found(mirror, objects(eq('@local.checked', True))) == {here}
        image = await database.fetchval('SELECT parent_id FROM samples WHERE id = $1', here)
        assert await found(mirror, images(eq('@local.checked', True))) == {image}

    registry_database(scenario)


def test_a_version_revert_marks_a_kind_again_after_the_statistics_cleared_it(registry_database):
    async def scenario(database):
        rows = await farm(database)
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=[rows['car']], metadata={'score': 0.5}),
            user=USER,
        )
        version = await versions.create_version(1, versions.CreateVersionRequest(name='with score'), 'user-1')
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=[rows['car']], remove_metadata_keys=['score']),
            user=USER,
        )
        await count_statistics(database)
        assert 'score' not in await record(database)
        assert await found(1, objects(eq('score', 0.5))) == set()
        # The revert (one SERIALIZABLE transaction) brings the score back, and marks objects with it.
        await versions.revert_to_version(1, version['id'], user={**USER, 'username': 'user-1'})
        assert (await record(database))['score'] == (False, True)
        assert await found(1, objects(eq('score', 0.5))) == {rows['car']}
        assert await found(1, images(eq('score', 0.5))) == {rows['red']}

    registry_database(scenario)


def test_the_object_copy_script_marks_the_objects_it_copies(registry_database):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        'copy_objects',
        Path(datasets.__file__).parents[2] / 'scripts/copy_objects.py',
    )
    assert spec is not None and spec.loader is not None
    copy_objects = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copy_objects)

    async def scenario(database):
        await add(1, [(None, {'colour': 'red'})], 'copy/img')
        (red,) = await ids(database, 1, 'copy/img')
        await add(2, [(None, {'parent_id': red, 'label': 'crop-car', 'area': 3})], 'copy/crop')
        async with database.pool.acquire() as connection:
            assert await copy_objects.copy_samples(connection, 2, 1) == 1
        assert (await record(database))['area'] == (False, True)
        (crop,) = await ids(database, 1, 'copy/crop')
        assert await found(1, objects(eq('area', 3))) == {crop}
        assert await found(1, images(eq('area', 3))) == {red}

    registry_database(scenario)


def test_every_kind_of_write_to_samples_marks_through_the_triggers(registry_database):
    """Whatever path writes the rows (an insert, a copy, an upsert, an update adding a key or a location, a sample
    moved to the other kind or another dataset, a REPEATABLE READ writer), the kinds are marked in its transaction."""

    async def scenario(database):
        rows = await farm(database)
        await database.execute(
            """INSERT INTO samples (dataset_id, filepath, media_type, metadata)
               VALUES (1, 'sql/a.jpg', 'object', '{"inserted": 1}')""",
        )
        async with database.pool.acquire() as connection:
            await connection.copy_to_table(
                'samples',
                source=io.BytesIO(b'1\tsql/b.jpg\timage\t{"copied": 1}\n'),
                columns=['dataset_id', 'filepath', 'media_type', 'metadata'],
            )
        await database.execute(
            """INSERT INTO samples (dataset_id, filepath, media_type, metadata)
               VALUES (1, 'sql/b.jpg', 'image', '{"upserted": 1}')
               ON CONFLICT ON CONSTRAINT samples_dataset_filepath_unique
               DO UPDATE SET metadata = samples.metadata || EXCLUDED.metadata""",
        )
        await database.execute(
            """UPDATE samples SET metadata = metadata || '{"updated": 1}',
                   location = ST_SetSRID(ST_MakePoint(2, 2), 4326) WHERE id = $1""",
            rows['truck'],
        )
        await database.execute("UPDATE samples SET media_type = 'image' WHERE filepath = 'sql/a.jpg'")
        await database.execute("UPDATE samples SET dataset_id = 2 WHERE filepath = 'sql/b.jpg'")
        async with database.pool.acquire() as connection, connection.transaction(isolation='repeatable_read'):
            await connection.execute(
                """INSERT INTO samples (dataset_id, filepath, media_type, metadata)
                   VALUES (1, 'sql/c.jpg', 'object', '{"repeatable": 1, "label": "bus"}')""",
            )
        kinds = await record(database)
        assert kinds['inserted'] == (True, True)  # an object, then the same sample as an image
        assert kinds['copied'] == (True, False) and kinds['upserted'] == (True, False)
        assert kinds['updated'] == (False, True) and kinds['location'] == (False, True)
        assert kinds['repeatable'] == (False, True)
        assert (await record(database, 2))['upserted'] == (True, False)  # moved with its keys
        # Each write that may have taken a kind's last value of a field is noted for the job: the kind and the
        # dataset changes above.
        assert await database.fetchval('SELECT count(*) FROM dataset_field_kinds_pending WHERE dataset_id = 1') == 2

    registry_database(scenario)


# --- The upgrade, and clearing ---


def test_a_dataset_not_yet_examined_counts_every_field_as_seen_on_both_and_the_job_examines_it(
    monkeypatch,
    registry_database,
):
    async def scenario(database):
        rows = await farm(database)
        await add(1, [(None, {'colour': 'grey', 'n': None})], 'farm/null')
        # As a dataset from before the record: nothing examined, marks only from writes since.
        await database.execute('DELETE FROM dataset_field_kinds WHERE dataset_id = 1')
        await database.execute('DELETE FROM dataset_field_kinds_state WHERE dataset_id = 1')
        assert set((await members()).values()) == {(True, True)}
        assert await field_kinds.load(database, 1) is None
        assert await found(1, objects(leaf('label', '!=', 'car'))) == {rows['truck']}
        (job,) = await run_job(database, monkeypatch)
        assert job['result']['examined'] is True
        assert await record(database) == {
            'colour': (True, False),
            'n': (True, False),  # a JSON null under the key counts as seen
            'label': (False, True),
            'parent_id': (False, True),
        }
        assert (await members())['label'] == (False, True)
        # The examination leaves a note, so a pass then checks every mark (round three: a mark a write without the
        # triggers left has a note waiting too); here every mark is needed.
        (check,) = await run_job(database, monkeypatch)
        assert check['result'] == {'cleared': [], 'held': False}
        assert await run_job(database, monkeypatch) == []  # examined once

    registry_database(scenario)


def test_counting_the_statistics_clears_a_kind_once_its_last_value_is_gone_and_no_answer_changes(registry_database):
    async def scenario(database):
        rows = await farm(database)
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=[rows['red'], rows['car']], metadata={'source': 'drone'}),
            user=USER,
        )
        assert (await record(database))['source'] == (True, True)
        before = {
            node: await found(1, grid(leaf('source', 'is_null'))) for node, grid in (('i', images), ('o', objects))
        }
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=[rows['car']], remove_metadata_keys=['source']),
            user=USER,
        )
        assert (await record(database))['source'] == (True, True)  # until the statistics are counted
        await count_statistics(database)
        assert (await record(database))['source'] == (True, False)
        assert (await members())['source'] == (True, False)
        assert await database.fetchval('SELECT count(*) FROM dataset_field_kinds_pending') == 0
        assert await found(1, images(leaf('source', 'is_null'))) == {rows['blue']}
        assert await found(1, objects(leaf('source', 'is_null'))) == {rows['truck']}
        assert before == {'i': {rows['blue']}, 'o': {rows['truck']}}
        # The truck's image goes, and with it the truck: no object has a label or a parent any more.
        await datasets.delete_sample(1, rows['blue'], USER)
        await database.execute('DELETE FROM samples WHERE id = $1', rows['car'])
        await count_statistics(database)
        assert 'label' not in await record(database) and 'parent_id' not in await record(database)
        assert (await members())['label'] == (False, False)
        assert await found(1, images(eq('label', 'car'))) == set()

    registry_database(scenario)


def test_a_check_a_write_held_off_is_left_noted_and_the_job_does_it_later(monkeypatch, registry_database):
    async def scenario(database):
        rows = await farm(database)
        await database.execute('DELETE FROM samples WHERE id = $1', rows['car'])
        await database.execute('DELETE FROM samples WHERE id = $1', rows['truck'])
        async with database.pool.acquire() as writer, writer.transaction():
            # A write marking a kind holds the lock shared until it ends: the count's check can't clear meanwhile.
            await writer.execute('SELECT pg_advisory_xact_lock_shared(20261125, 1)')
            await count_statistics(database)
        assert (await record(database))['label'] == (False, True)
        assert await database.fetchval('SELECT count(*) FROM dataset_field_kinds_pending WHERE dataset_id = 1') > 0
        # The held pass backs off (review M1, tested below): as if its 20 seconds had passed.
        await database.execute(
            "UPDATE dataset_field_kinds_state SET retry_after = clock_timestamp() - interval '1 second'",
        )
        (job,) = await run_job(database, monkeypatch)
        assert sorted(job['result']['cleared']) == [['label', 'object'], ['parent_id', 'object']]
        assert 'label' not in await record(database)
        assert await database.fetchval('SELECT count(*) FROM dataset_field_kinds_pending') == 0

    registry_database(scenario)


def test_an_ingest_that_updates_a_sample_into_the_other_kind_marks_every_field_it_holds(registry_database):
    async def scenario(database):
        rows = await farm(database)
        await datasets.add_samples(
            1,
            {
                'samples': [
                    {
                        'filepath': 'farm/img-1.jpg',
                        'media_type': 'object',
                        'metadata': {'colour': 'blue', 'parent_id': rows['red'], 'size': 2},
                    },
                ],
                'on_duplicate': 'update',
            },
            user=USER,
        )
        kinds = await record(database)
        assert kinds['colour'] == (True, True) and kinds['size'] == (False, True)
        assert await found(1, objects(eq('size', 2))) == {rows['blue']}
        assert await found(1, objects(eq('colour', 'blue'))) == {rows['blue']}
        assert await found(1, images(eq('size', 2))) == {rows['red']}

    registry_database(scenario)


async def check_one(pool: Any, name: str, kind: str) -> dict:
    """A pass over one mark as the job runs it (check_some, to its end)."""
    state = {'snapshot': '', 'marks': [[name, kind]], 'next': 0, 'walk': None, 'cleared': [], 'held': False}
    while not field_kinds.checked_all(state):
        state = await field_kinds.check_some(pool, 1, state)
    return state


def test_a_first_value_racing_the_clearing_check_always_keeps_its_mark(registry_database):
    """Contract 1.4 and 5.2: a row written while the check runs leaves the mark set. Repeated with the writer and a
    pass over the mark (A, B, C, D as the job runs them) in random order, under READ COMMITTED and REPEATABLE READ (a
    writer the check cleared under is refused)."""

    async def scenario(database):
        await farm(database)
        pool = database.pool
        refused = kept = 0
        for round_ in range(40):
            # A stale mark: objects were seen with `x`, none has it now.
            await database.execute(
                """INSERT INTO dataset_field_kinds (dataset_id, field_name, on_images, on_objects)
                   VALUES (1, 'x', FALSE, TRUE) ON CONFLICT (dataset_id, field_name) DO UPDATE SET on_objects = TRUE""",
            )
            isolation = 'repeatable_read' if round_ % 2 else 'read_committed'

            async def writer() -> None:
                nonlocal refused
                await asyncio.sleep(random.uniform(0, 0.03))
                try:
                    async with pool.acquire() as connection, connection.transaction(isolation=isolation):
                        await connection.fetchval('SELECT 1')  # the snapshot of a REPEATABLE READ writer
                        await asyncio.sleep(random.uniform(0, 0.03))
                        await connection.execute(
                            """INSERT INTO samples (dataset_id, filepath, media_type, metadata)
                               VALUES (1, $1, 'object', '{"x": 1}')""",
                            f'race/{round_}.jpg',
                        )
                        await asyncio.sleep(random.uniform(0, 0.03))
                except asyncpg.SerializationError:
                    refused += 1

            async def clearer() -> None:
                await asyncio.sleep(random.uniform(0, 0.05))
                await check_one(pool, 'x', 'object')

            await asyncio.gather(writer(), clearer())
            written = await database.fetchval(
                "SELECT count(*) FROM samples WHERE dataset_id = 1 AND media_type = 'object' AND metadata ? 'x'",
            )
            marked = await database.fetchval(
                "SELECT on_objects FROM dataset_field_kinds WHERE dataset_id = 1 AND field_name = 'x'",
            )
            assert written == 0 or marked, f'round {round_}: an object has x, the record says no object does'
            kept += bool(written)
            await database.execute("DELETE FROM samples WHERE dataset_id = 1 AND metadata ? 'x'")
        assert kept > 0

    registry_database(scenario)


# --- The reader: skips only, never another answer ---


def test_a_kind_never_seen_with_a_field_is_not_asked(registry_database):
    async def scenario(database):
        await farm(database)

        async def compiled(node: dict) -> str:
            out: dict[str, str] = {}

            async def run() -> None:
                out['sql'], _ = datasets.parse_filter_node(node, [1], 2, None, set(), 1, array_columns={'tags'})

            await field_kinds.reading(database, 1, node, run)
            return out['sql']

        label = leaf('label', '!=', 'car')
        assert ' s2 ' not in await compiled(objects(label))  # images never had a label: no image is looked up
        assert 'require_field_kinds' in await compiled(objects(label))
        assert ' o ' not in await compiled(images(eq('colour', 'red')))  # no object is looked up for a colour
        assert ' o ' in await compiled(images(eq('label', 'car')))
        await database.execute('DELETE FROM dataset_field_kinds_state WHERE dataset_id = 1')
        assert ' s2 ' in await compiled(objects(label))  # not examined: nothing skipped
        assert 'require_field_kinds' not in await compiled(objects(label))

    registry_database(scenario)


def test_a_reading_whose_record_a_first_value_changed_reads_it_again(monkeypatch, registry_database):
    async def scenario(database):
        rows = await farm(database)
        stale = await field_kinds.load(database, 1)
        await datasets.update_sample(1, rows['blue'], datasets.SampleUpdate(metadata={'label': 'kite'}), user=USER)
        real, loads = field_kinds.load, []

        async def stale_first(source: Any, dataset_id: int) -> Any:
            loads.append(dataset_id)
            return stale if len(loads) == 1 else await real(source, dataset_id)

        monkeypatch.setattr(field_kinds, 'load', stale_first)
        assert await found(1, images(eq('label', 'kite'))) == {rows['blue']}
        assert len(loads) == 2  # the statement compiled with the stale record was refused, and read again

    registry_database(scenario)


def test_a_dataset_without_objects_never_reads_the_record(monkeypatch, registry_database):
    async def scenario(database):
        await add(2, [(None, {'colour': 'red'}), (None, {})], 'plain/img')
        red, _ = await ids(database, 2, 'plain/img')

        async def refuse(source: Any, dataset_id: int) -> Any:
            raise AssertionError('the record was read')

        monkeypatch.setattr(field_kinds, 'load', refuse)
        plain = {
            'logic': 'AND',
            'filters': [{'field': 'media_type', 'operator': '!=', 'value': 'object'}, eq('colour', 'red')],
        }
        assert await found(2, plain) == {red}

    registry_database(scenario)


def test_the_record_never_changes_an_answer(monkeypatch, registry_database):
    """Many filter shapes on both grids: the rows with the record are the rows without it (as for a dataset not yet
    examined), and the record skipped something for most of them."""

    async def scenario(database):
        await add(
            1,
            [
                (
                    None,
                    {'colour': 'red', 'n': 1, 'source': 'drone', 'detections': [{'label': 'cat', 'confidence': 0.9}]},
                ),
                (None, {'colour': 'red', 'n': 2, 'source': 'tractor'}),
                (None, {'colour': 'blue', 'n': 3, 'source': None}),
                (None, {'n': 4}),
                (None, {'colour': 'red', 'n': 5}),
                (None, {'colour': 'blue', 'n': 6}),
            ],
            'all/img',
        )
        img = await ids(database, 1, 'all/img')
        await database.execute(
            'UPDATE samples SET location = ST_SetSRID(ST_MakePoint(2, 2), 4326) WHERE id = ANY($1::int[])',
            img[:2],
        )
        await add(
            1,
            [
                ('object', {'parent_id': img[0], 'label': 'car', 'score': 0.9}),
                ('object', {'parent_id': img[0], 'label': 'truck', 'score': 0.2, 'source': 'drone'}),
                ('object', {'parent_id': img[1], 'label': 'truck', 'occluded': True}),
                ('object', {'parent_id': img[2], 'label': 'car', 'score': None}),
                ('object', {'parent_id': img[3], 'label': 'car', 'score': 0.6}),
                ('object', {'parent_id': img[5], 'score': 0.4}),
                ('object', {'label': 'bike'}),
            ],
            'all/obj',
        )
        obj = await ids(database, 1, 'all/obj')
        await database.execute(
            'UPDATE samples SET location = ST_SetSRID(ST_MakePoint(3, 3), 4326) WHERE id = $1',
            obj[2],
        )
        place = leaf('location', 'geo_polygon', SQUARE)
        shapes = [
            [eq('colour', 'red')],
            [eq('label', 'car')],
            [eq('colour', 'red'), eq('label', 'car')],
            [leaf('colour', '!=', 'red')],
            [leaf('label', '!=', 'car')],
            [leaf('label', 'not in', ['car', 'truck'])],
            [NOT(eq('label', 'car'))],
            [NOT(AND(eq('colour', 'red'), eq('label', 'car')))],
            [NOT(AND(eq('label', 'car'), leaf('score', '>', 0.5)))],
            [leaf('colour', 'is_null')],
            [leaf('label', 'is_null')],
            [leaf('score', 'is_null')],
            [leaf('label', 'is_not_null')],
            [leaf('label', 'exists')],
            [leaf('colour', 'not_exists')],
            [leaf('score', 'exists')],
            [leaf('score', '>', 0.5)],
            [leaf('score', '>=', 0.2), leaf('score', '<=', 0.8)],
            [leaf('score', '>', None)],
            [leaf('n', '>=', 3)],
            [leaf('label', 'contains', 'ar')],
            [leaf('colour', 'in', ['red', 'blue'])],
            [eq('source', 'drone')],
            [leaf('source', '!=', 'drone')],
            [leaf('source', 'is_null')],
            [place],
            [NOT(place)],
            [leaf('location', 'geo_polygon_exclude', SQUARE)],
            [leaf('location', 'is_null')],
            [eq('detections.label', 'cat')],
            [leaf('detections.confidence', '>', 0.5)],
            [OR(eq('colour', 'red'), eq('label', 'truck'))],
            [eq('label', 'car'), NOT(eq('occluded', True))],
            [eq('label', 'car'), leaf('score', 'is_null')],
            [leaf('id', 'in', [img[0], obj[2], obj[6]])],
            [leaf('id', 'not in', [img[0], obj[0]]), eq('label', 'car')],
            [leaf('tags', 'not in', ['x'])],
            [eq('nope', 1)],
            [leaf('nope', 'is_null')],
            [NOT(eq('nope', 1))],
            [],
        ]
        guards = []
        real_guard, real_load = field_kinds.guarded, field_kinds.load

        def counted(*args: Any) -> Any:
            guards.append(args[-1])
            return real_guard(*args)

        monkeypatch.setattr(field_kinds, 'guarded', counted)
        with_record: dict = {}
        for index, stages in enumerate(shapes):
            for name, grid in (('images', images), ('objects', objects)):
                with_record[index, name] = await found(1, grid(*stages))
        skipped = len(guards)

        async def none(source: Any, dataset_id: int) -> None:
            return None

        monkeypatch.setattr(field_kinds, 'load', none)
        for index, stages in enumerate(shapes):
            for name, grid in (('images', images), ('objects', objects)):
                assert await found(1, grid(*stages)) == with_record[index, name], (stages, name)
        assert len(guards) == skipped and skipped > len(shapes)
        monkeypatch.setattr(field_kinds, 'load', real_load)

    registry_database(scenario)


# --- Second round: a held pass backs off, and a sample moved to no dataset ---


def test_a_pass_a_write_held_off_is_not_tried_again_at_once_and_its_checkpoint_stays_small(
    monkeypatch,
    registry_database,
):
    """Review M1: after a pass a write held off, neither the schedule nor the statistics count queues another one until
    the back-off ends (20 s, doubling with each held pass in a row); a pass carries the snapshot it began with, not the
    notes, so its checkpoint stays small however many there are."""

    async def scenario(database):
        rows = await farm(database)
        await database.execute('DELETE FROM samples WHERE id = $1', rows['car'])
        await database.execute('DELETE FROM samples WHERE id = $1', rows['truck'])
        await database.execute(
            'INSERT INTO dataset_field_kinds_pending (dataset_id) SELECT 1 FROM generate_series(1, 500)',
        )

        async def backoff():
            return await database.fetchrow(
                """SELECT held_passes, extract(epoch FROM retry_after - clock_timestamp())::float AS wait
                   FROM dataset_field_kinds_state WHERE dataset_id = 1""",
            )

        async def passes() -> int:
            return await database.fetchval("SELECT count(*) FROM jobs WHERE kind = 'field_kinds' AND dataset_id = 1")

        async def back_off_ends() -> None:
            await database.execute(
                "UPDATE dataset_field_kinds_state SET retry_after = clock_timestamp() - interval '1 second'",
            )

        async with database.pool.acquire() as writer, writer.transaction():
            await writer.execute('SELECT pg_advisory_xact_lock_shared(20261125, 1)')
            (job,) = await run_job(database, monkeypatch)
            assert job['result'] == {'cleared': [], 'held': True}
            # The pass keeps the snapshot it began with (round three, rule 1.4c), not the notes.
            assert set(job['checkpoint']['state']) == {'snapshot', 'marks', 'next', 'walk', 'cleared', 'held'}
            assert len(json.dumps(job['checkpoint'])) < 400, job['checkpoint']
            first = await backoff()
            assert first['held_passes'] == 1 and 15 < first['wait'] <= 20, dict(first)
            # Not tried again at once.
            assert await field_kinds.schedule(database.pool) == []
            assert await field_kinds.check_start(database.pool, 1) is None
            before = await passes()
            await count_statistics(database)
            assert await passes() == before
            # Held again once the back-off ends: twice as long.
            await back_off_ends()
            (job,) = await run_job(database, monkeypatch)
            assert job['result']['held']
            second = await backoff()
            assert second['held_passes'] == 2 and 35 < second['wait'] <= 40, dict(second)
            assert await database.fetchval('SELECT count(*) FROM dataset_field_kinds_pending') == 502
        await back_off_ends()
        (job,) = await run_job(database, monkeypatch)
        assert sorted(job['result']['cleared']) == [['label', 'object'], ['parent_id', 'object']]
        assert await database.fetchval('SELECT count(*) FROM dataset_field_kinds_pending') == 0
        assert dict(await backoff()) == {'held_passes': 0, 'wait': None}

    registry_database(scenario)


def test_a_sample_moved_to_no_dataset_is_noted_and_one_moved_back_is_marked(registry_database):
    """Review S3: the record's trigger accepts an update that sets a sample's dataset to NULL, noting it for its old
    dataset, and moving it back marks its fields. (main refuses that update too, in its statistics trigger, which a
    NULL dataset can't be recorded for: it is disabled here, in a transaction that is rolled back.)"""

    class RolledBack(Exception):
        pass

    async def scenario(database):
        await farm(database)
        await add(1, [(None, {'wheels': 4})], 'farm/wheel')
        (wheel,) = await ids(database, 1, 'farm/wheel')
        notes = 'SELECT count(*) FROM dataset_field_kinds_pending WHERE dataset_id = 1'
        marked = "SELECT on_images FROM dataset_field_kinds WHERE dataset_id = 1 AND field_name = 'wheels'"
        before = await database.fetchval(notes)
        with pytest.raises(RolledBack):
            async with database.pool.acquire() as connection, connection.transaction():
                await connection.execute('ALTER TABLE samples DISABLE TRIGGER trigger_stats_updated')
                await connection.execute('UPDATE samples SET dataset_id = NULL WHERE id = $1', wheel)
                assert await connection.fetchval(notes) == before + 1
                # As if the check had cleared it since: moving the sample back marks it again.
                await connection.execute(
                    "DELETE FROM dataset_field_kinds WHERE dataset_id = 1 AND field_name = 'wheels'",
                )
                await connection.execute('UPDATE samples SET dataset_id = 1 WHERE id = $1', wheel)
                assert await connection.fetchval(marked) is True
                raise RolledBack
        assert await database.fetchval(notes) == before
        assert (await record(database))['wheels'] == (True, False)

    registry_database(scenario)


def test_an_update_of_many_rows_marks_each_kind_and_a_rolled_back_mark_is_made_again(registry_database):
    """Second round: the UPDATE trigger's WHEN compares key lists (field_kinds_same_keys) and its function skips a row
    whose new keys the transaction already marked for its dataset and kind. One statement over both kinds marks each
    kind; a savepoint rolled back takes its marks with it, so the next row marks again; a removal is noted once; and a
    filter finds each value at once."""

    class RolledBack(Exception):
        pass

    async def scenario(database):
        rows = await farm(database)
        await database.execute('''UPDATE samples SET metadata = metadata || '{"wet": true}' WHERE dataset_id = 1''')
        assert (await record(database))['wet'] == (True, True)
        assert await found(1, objects(eq('wet', True))) == {rows['car'], rows['truck']}
        async with database.pool.acquire() as connection, connection.transaction():
            with pytest.raises(RolledBack):
                async with connection.transaction():
                    await connection.execute(
                        '''UPDATE samples SET metadata = metadata || '{"hot": 1}' WHERE id = $1''',
                        rows['red'],
                    )
                    raise RolledBack
            await connection.execute(
                '''UPDATE samples SET metadata = metadata || '{"hot": 1}' WHERE id = $1''',
                rows['blue'],
            )
        assert (await record(database))['hot'] == (True, False)
        assert await found(1, images(eq('hot', 1))) == {rows['blue']}
        notes = 'SELECT count(*) FROM dataset_field_kinds_pending WHERE dataset_id = 1'
        before = await database.fetchval(notes)
        await database.execute("UPDATE samples SET metadata = metadata - 'wet' WHERE dataset_id = 1")
        assert await database.fetchval(notes) == before + 1
        assert await database.fetchval("SELECT count(*) FROM samples WHERE dataset_id = 1 AND metadata ? 'wet'") == 0
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = 1') == 4

    registry_database(scenario)


def test_a_count_queues_the_clearing_pass_only_for_notes_and_the_pass_checks_every_mark(registry_database):
    """Review S1: the statistics count queues the clearing pass instead of running it, and only when a write was noted:
    a count with nothing noted adds no job to the dataset's list. The pass checks every mark, so a mark left with no
    note of its own (here written directly) goes at the next pass a note brings."""

    async def scenario(database):
        rows = await farm(database)
        await count_statistics(database)
        await database.execute(
            "INSERT INTO dataset_field_kinds (dataset_id, field_name, on_images, on_objects) "
            "VALUES (1, 'ghost', FALSE, TRUE)",
        )
        await database.execute('DELETE FROM dataset_field_kinds_pending')
        passes = "SELECT count(*) FROM jobs WHERE kind = 'field_kinds' AND dataset_id = 1"
        before = await database.fetchval(passes)
        await count_statistics(database)
        assert await database.fetchval(passes) == before
        assert (await record(database))['ghost'] == (False, True)
        await database.execute('DELETE FROM samples WHERE id = $1', rows['truck'])
        await count_statistics(database)
        assert await database.fetchval(passes) == before + 1
        kinds = await record(database)
        assert 'ghost' not in kinds and kinds['label'] == (False, True)

    registry_database(scenario)


# --- Round three: the lock is held only for one-row statements (rule 1.4a), a mark can always be cleared (1.4b), and
# a note is forgotten only by a pass that began after its write committed (1.4c) ---

ISOLATIONS = ('read_committed', 'repeatable_read')


async def stale_mark(database, name: str = 'x') -> None:
    """Objects marked with `name`, which no object has."""
    await database.execute(
        """INSERT INTO dataset_field_kinds (dataset_id, field_name, on_images, on_objects) VALUES (1, $1, FALSE, TRUE)
           ON CONFLICT (dataset_id, field_name) DO UPDATE SET on_objects = TRUE""",
        name,
    )


async def mark_row(database, name: str = 'x'):
    return await database.fetchrow(
        'SELECT on_objects, checking_objects IS NOT NULL FROM dataset_field_kinds '
        'WHERE dataset_id = 1 AND field_name = $1',
        name,
    )


WRITE_X = """INSERT INTO samples (dataset_id, filepath, media_type, metadata) VALUES (1, $1, 'object', '{"x": 1}')"""


@pytest.mark.parametrize('isolation', ISOLATIONS)
def test_a_first_value_committed_between_the_flag_and_the_clear_keeps_the_mark(isolation, registry_database):
    """B flags the mark, C finds no object with `x`, then a writer commits an object with it (its snapshot after B):
    it reads the flag set, writes the mark again with the flag off, and D keeps the mark."""

    async def scenario(database):
        await farm(database)
        await stale_mark(database)
        pool = database.pool
        token = await field_kinds._flag(pool, 1, 'x', 'object')
        assert token
        assert tuple(await mark_row(database)) == (True, True)
        assert await field_kinds._present(pool, 1, 'x', 'object') is False
        async with pool.acquire() as writer, writer.transaction(isolation=isolation):
            await writer.execute(WRITE_X, 'race/between.jpg')
        assert tuple(await mark_row(database)) == (True, False)
        assert await field_kinds._clear_flagged(pool, 1, 'x', 'object', token) is False
        assert tuple(await mark_row(database)) == (True, False)
        written = await database.fetchval("SELECT id FROM samples WHERE filepath = 'race/between.jpg'")
        assert await found(1, objects(eq('x', 1))) == {written}

    registry_database(scenario)


@pytest.mark.parametrize('isolation', ISOLATIONS)
def test_a_first_value_in_flight_at_the_flag_or_at_the_clear_holds_the_pass_off(isolation, registry_database):
    """A writer holds the lock shared from its mark to its end: in flight at B, B is held and sets nothing; in flight
    at D, D is held. One that commits keeps the mark; one that rolls back takes its unset flag with it, and D clears."""

    class RolledBack(Exception):
        pass

    async def scenario(database):
        await farm(database)
        await stale_mark(database)
        pool = database.pool
        async with pool.acquire() as writer, writer.transaction(isolation=isolation):
            await writer.execute(WRITE_X, 'race/at-b.jpg')
            assert await field_kinds._flag(pool, 1, 'x', 'object') is None
        assert tuple(await mark_row(database)) == (True, False)
        await database.execute("DELETE FROM samples WHERE filepath = 'race/at-b.jpg'")
        # In flight at D, then rolled back: its unset flag goes with it, and D clears.
        token = await field_kinds._flag(pool, 1, 'x', 'object')
        assert token
        with pytest.raises(RolledBack):
            async with pool.acquire() as writer, writer.transaction(isolation=isolation):
                await writer.execute(WRITE_X, 'race/at-d.jpg')
                assert await field_kinds._clear_flagged(pool, 1, 'x', 'object', token) is None
                raise RolledBack
        assert tuple(await mark_row(database)) == (True, True)
        assert await field_kinds._clear_flagged(pool, 1, 'x', 'object', token) is True
        assert await mark_row(database) is None
        # In flight at D, then committed: D is held, and afterwards keeps the mark.
        await stale_mark(database)
        token = await field_kinds._flag(pool, 1, 'x', 'object')
        assert token
        async with pool.acquire() as writer, writer.transaction(isolation=isolation):
            await writer.execute(WRITE_X, 'race/at-d.jpg')
            assert await field_kinds._clear_flagged(pool, 1, 'x', 'object', token) is None
        assert await field_kinds._clear_flagged(pool, 1, 'x', 'object', token) is False
        assert tuple(await mark_row(database)) == (True, False)

    registry_database(scenario)


def test_a_repeatable_read_writer_whose_snapshot_is_older_than_the_flag_is_refused(registry_database):
    """Its upsert meets the mark row B changed after its snapshot and is refused (could not serialize): it never
    commits a value the clear doesn't see. The pass then clears the mark."""

    async def scenario(database):
        await farm(database)
        await stale_mark(database)
        pool = database.pool
        tokens: list[str] = []
        with pytest.raises(asyncpg.SerializationError):
            async with pool.acquire() as writer, writer.transaction(isolation='repeatable_read'):
                await writer.fetchval('SELECT count(*) FROM samples')  # its snapshot, before B
                tokens.append(await field_kinds._flag(pool, 1, 'x', 'object') or '')
                assert tokens[0]
                await writer.execute(WRITE_X, 'race/old-snapshot.jpg')
        token = tokens[0]
        assert await field_kinds._present(pool, 1, 'x', 'object') is False
        assert await field_kinds._clear_flagged(pool, 1, 'x', 'object', token) is True
        assert await mark_row(database) is None
        assert await database.fetchval("SELECT count(*) FROM samples WHERE metadata ? 'x'") == 0

    registry_database(scenario)


def test_a_note_committed_while_a_pass_runs_survives_it_and_the_next_pass_clears_the_mark(
    monkeypatch,
    registry_database,
):
    """Rule 1.4c: a delete noted before the pass began (a lower note id) but committed while it ran, after the pass
    found its object still there, keeps its note; the next pass clears the mark with no further write."""

    async def scenario(database):
        rows = await farm(database)
        await add(1, [('object', {'parent_id': rows['red'], 'x': 1})], 'farm/x')
        (x,) = await ids(database, 1, 'farm/x')
        pool = database.pool
        async with pool.acquire() as deleter:
            transaction = deleter.transaction()
            await transaction.start()
            await deleter.execute('DELETE FROM samples WHERE id = $1', x)  # its note, not yet committed
            await database.execute('INSERT INTO dataset_field_kinds_pending (dataset_id) VALUES (1)')  # a later id
            state = await field_kinds.check_start(pool, 1)
            assert state is not None
            while not field_kinds.checked_all(state):
                state = await field_kinds.check_some(pool, 1, state)
            assert state['cleared'] == [] and not state['held']  # the object still had `x` for the pass
            await transaction.commit()
        async with pool.acquire() as connection, connection.transaction():
            await field_kinds.finish_check(connection, 1, state)
        assert await database.fetchval('SELECT count(*) FROM dataset_field_kinds_pending WHERE dataset_id = 1') == 1
        assert (await record(database))['x'] == (False, True)
        (job,) = await run_job(database, monkeypatch)
        assert job['result']['cleared'] == [['x', 'object']]
        assert 'x' not in await record(database)
        assert await database.fetchval('SELECT count(*) FROM dataset_field_kinds_pending WHERE dataset_id = 1') == 0

    registry_database(scenario)


def test_a_check_that_runs_out_of_time_still_ends_in_a_cleared_mark(monkeypatch, registry_database):
    """Rule 1.4b: when the single statement runs out of time (here a tiny limit on a statement that sleeps), C walks the
    dataset's rows in id order, a few a statement and a step a unit of the job: a present field is found by the walk and
    kept, an absent one is cleared."""

    async def scenario(database):
        rows = await farm(database)
        await stale_mark(database)
        await database.execute('DELETE FROM samples WHERE id = $1', rows['truck'])  # a note
        real = field_kinds._present_sql

        def sleepy(name: str, kind: str) -> tuple[str, list]:
            sql, args = real(name, kind)
            return 'SELECT EXISTS (SELECT FROM pg_sleep(0.3)) AND ' + sql[len('SELECT ') :], args

        walked = []
        real_walk = field_kinds._walk

        async def counted(*args: Any) -> Any:
            walked.append(args[2:4])
            return await real_walk(*args)

        monkeypatch.setattr(field_kinds, '_present_sql', sleepy)
        monkeypatch.setattr(field_kinds, 'CHECK_TIMEOUT_SECONDS', 0.05)
        monkeypatch.setattr(field_kinds, 'CHECK_SECONDS', 0)
        monkeypatch.setattr(field_kinds, 'WALK_ROWS', 1)
        monkeypatch.setattr(field_kinds, '_walk', counted)
        (job,) = await run_job(database, monkeypatch)
        assert job['result'] == {'cleared': [['x', 'object']], 'held': False}, job['result']
        assert ('x', 'object') in walked and walked.count(('x', 'object')) >= 3  # one row a statement, to the end
        kinds = await record(database)
        assert 'x' not in kinds and kinds['label'] == (False, True) and kinds['colour'] == (True, False)
        flags = await database.fetchval(
            'SELECT count(*) FROM dataset_field_kinds WHERE dataset_id = 1 '
            'AND (checking_images IS NOT NULL OR checking_objects IS NOT NULL)',
        )
        assert flags == 0

    registry_database(scenario)


def test_two_checks_of_one_mark_overlapping_with_a_writer_between_never_clear_a_needed_mark(
    monkeypatch,
    registry_database,
):
    """Review R3-M1: P1 writes its token on `x` (B); P1's C and P2's A find no object with it; a writer puts `x` on an
    object and commits, taking the token off; P2 writes its own token. P1's D finds a token that isn't its own: it
    clears nothing (a boolean flag would have cleared) and has decided nothing, so it counts as held; P2's C finds the
    object and keeps the mark. Without a writer, the first check's D gives way to the second, whose D clears. A whole
    pass whose D meets another check's token forgets no note (rule 1.4c)."""

    async def scenario(database):
        await farm(database)
        await stale_mark(database)
        pool = database.pool
        first = await field_kinds._flag(pool, 1, 'x', 'object')
        assert first
        assert await field_kinds._present(pool, 1, 'x', 'object') is False  # P1's C, and P2's A
        async with pool.acquire() as writer, writer.transaction():
            await writer.execute(WRITE_X, 'race/overlap.jpg')
        second = await field_kinds._flag(pool, 1, 'x', 'object')
        assert second and second != first
        assert await field_kinds._clear_flagged(pool, 1, 'x', 'object', first) is None  # undecided, as held
        assert tuple(await mark_row(database)) == (True, True)
        assert await field_kinds._present(pool, 1, 'x', 'object') is True  # P2's C
        await field_kinds._unflag(pool, 1, 'x', 'object', second)
        assert tuple(await mark_row(database)) == (True, False)
        written = await database.fetchval("SELECT id FROM samples WHERE filepath = 'race/overlap.jpg'")
        assert await found(1, objects(eq('x', 1))) == {written}
        await stale_mark(database, 'y')
        first = await field_kinds._flag(pool, 1, 'y', 'object')
        second = await field_kinds._flag(pool, 1, 'y', 'object')
        assert first and second
        assert await field_kinds._clear_flagged(pool, 1, 'y', 'object', first) is None
        assert await field_kinds._clear_flagged(pool, 1, 'y', 'object', second) is True
        assert await mark_row(database, 'y') is None
        # A pass whose D meets another check's token counts the mark as held: it forgets no note and backs off, so
        # the mark has a note waiting even if the other check is then held off.
        await stale_mark(database, 'z')
        await database.execute('DELETE FROM samples WHERE id = $1', written)  # a note
        notes = 'SELECT count(*) FROM dataset_field_kinds_pending WHERE dataset_id = 1'
        before = await database.fetchval(notes)
        real_clear = field_kinds._clear_flagged

        async def overlapped(pool_: Any, dataset_id: int, name: str, kind: str, token: str) -> Any:
            if name == 'z':
                assert await field_kinds._flag(pool_, dataset_id, name, kind)  # the other check's B
            return await real_clear(pool_, dataset_id, name, kind, token)

        monkeypatch.setattr(field_kinds, '_clear_flagged', overlapped)
        state = await field_kinds.check_start(pool, 1)
        assert state is not None
        while not field_kinds.checked_all(state):
            state = await field_kinds.check_some(pool, 1, state)
        assert state['held'] and ['z', 'object'] not in state['cleared'], state
        async with pool.acquire() as connection, connection.transaction():
            await field_kinds.finish_check(connection, 1, state)
        assert await database.fetchval(notes) == before
        assert tuple(await mark_row(database, 'z')) == (True, True)
        monkeypatch.setattr(field_kinds, '_clear_flagged', real_clear)
        await database.execute("UPDATE dataset_field_kinds_state SET retry_after = clock_timestamp() - interval '1 s'")
        (job,) = await run_job(database, monkeypatch)
        assert ['z', 'object'] in job['result']['cleared'] and not job['result']['held'], job['result']
        assert await mark_row(database, 'z') is None
        assert await database.fetchval(notes) == 0

    registry_database(scenario)


def test_a_read_committed_writer_rewrites_a_mark_being_checked_and_takes_off_only_its_kinds_token(registry_database):
    """Review R3-S1a: under READ COMMITTED "already marked" is "on, with no token": a writer of a field whose mark on
    its kind carries a token writes the row (on, that kind's token off) and leaves the other kind's token; with no
    token, a writer doesn't write the row at all."""

    async def scenario(database):
        rows = await farm(database)
        await add(1, [(None, {'both': 1}), ('object', {'parent_id': rows['red'], 'both': 2})], 'farm/both')
        pool = database.pool
        image_token = await field_kinds._flag(pool, 1, 'both', 'image')
        object_token = await field_kinds._flag(pool, 1, 'both', 'object')
        assert image_token and object_token
        version = "SELECT xmin::text FROM dataset_field_kinds WHERE dataset_id = 1 AND field_name = 'both'"
        tokens = (
            "SELECT checking_images::text, checking_objects::text FROM dataset_field_kinds "
            "WHERE dataset_id = 1 AND field_name = 'both'"
        )
        before = await database.fetchval(version)
        async with pool.acquire() as writer, writer.transaction(isolation='read_committed'):
            await writer.execute(
                """INSERT INTO samples (dataset_id, filepath, media_type, metadata)
                   VALUES (1, 'farm/both-again.jpg', 'object', '{"both": 3}')""",
            )
        assert tuple(await database.fetchrow(tokens)) == (image_token, None)
        written = await database.fetchval(version)
        assert written != before
        await database.execute(
            """INSERT INTO samples (dataset_id, filepath, media_type, metadata)
               VALUES (1, 'farm/both-third.jpg', 'object', '{"both": 4}')""",
        )
        assert await database.fetchval(version) == written  # on, no token: nothing written

    registry_database(scenario)


def test_readers_read_a_mark_being_checked_as_a_mark(registry_database):
    """Review R3-S1b: load, require_field_kinds and the API's on_images / on_objects read only the mark: while a check's
    token is on `label` (objects), the field is seen on objects and both grids find it."""

    async def scenario(database):
        rows = await farm(database)
        token = await field_kinds._flag(database.pool, 1, 'label', 'object')
        assert token
        kinds = await field_kinds.load(database, 1)
        assert kinds is not None and kinds.seen('label', 'object') and not kinds.seen('label', 'image')
        assert (await members())['label'] == (False, True)
        assert await found(1, objects(eq('label', 'car'))) == {rows['car']}
        assert await found(1, images(eq('label', 'car'))) == {rows['red']}
        with pytest.raises(asyncpg.SerializationError):
            await database.fetchval("SELECT require_field_kinds(1, '{}'::text[], ARRAY['label'])")
        assert await database.fetchval("SELECT require_field_kinds(1, ARRAY['label'], '{}'::text[])") is True

    registry_database(scenario)


def test_a_version_revert_whose_snapshot_is_older_than_the_flag_is_refused_retried_and_keeps_its_mark(
    monkeypatch,
    registry_database,
):
    """Review R3-S1c: a check writes its token on `score` (objects) after the revert's SERIALIZABLE snapshot was taken;
    the revert's mark meets that newer row version, is refused (40001), and the revert is run again, which writes the
    mark (token off). The check's D then keeps it, and the value is found."""

    async def scenario(database):
        rows = await farm(database)
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=[rows['car']], metadata={'score': 0.5}),
            user=USER,
        )
        version = await versions.create_version(1, versions.CreateVersionRequest(name='with score'), 'user-1')
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=[rows['car']], remove_metadata_keys=['score']),
            user=USER,
        )
        assert (await record(database))['score'] == (False, True)  # no object has it; not cleared yet
        tokens: list = []
        attempts: list = []
        real = getattr(versions, 'lock_field_lifecycle')

        async def locked_then_checked(connection: Any, *args: Any, **kwargs: Any) -> None:
            await real(connection, *args, **kwargs)  # the revert's snapshot is taken by now
            attempts.append(1)
            if not tokens:
                tokens.append(await field_kinds._flag(database.pool, 1, 'score', 'object'))

        monkeypatch.setattr(versions, 'lock_field_lifecycle', locked_then_checked)
        await versions.revert_to_version(1, version['id'], user={**USER, 'username': 'user-1'})
        assert len(attempts) == 2 and tokens[0]
        assert tuple(await mark_row(database, 'score')) == (True, False)
        assert await field_kinds._clear_flagged(database.pool, 1, 'score', 'object', tokens[0]) is False
        assert await found(1, objects(eq('score', 0.5))) == {rows['car']}

    registry_database(scenario)


def test_the_flag_and_the_clear_change_one_row_within_a_second_and_the_flag_needs_an_on_mark(registry_database):
    """Review R3-S1d: B writes a token only where the mark is on (and only that kind's); a row lock on the mark makes B
    and D give up after their 1 s limit (held), never wait longer."""

    async def scenario(database):
        await farm(database)
        pool = database.pool
        assert await field_kinds._flag(pool, 1, 'colour', 'object') == ''  # colour is not marked on objects
        assert await field_kinds._flag(pool, 1, 'nothing', 'object') == ''
        token = await field_kinds._flag(pool, 1, 'colour', 'image')
        assert token
        row = await database.fetchrow(
            "SELECT checking_images::text, checking_objects FROM dataset_field_kinds WHERE dataset_id = 1 "
            "AND field_name = 'colour'",
        )
        assert tuple(row) == (token, None)
        loop = asyncio.get_running_loop()
        async with pool.acquire() as holder, holder.transaction():
            await holder.execute(
                "SELECT 1 FROM dataset_field_kinds WHERE dataset_id = 1 AND field_name = 'label' FOR UPDATE",
            )
            started = loop.time()
            assert await field_kinds._flag(pool, 1, 'label', 'object') is None
            # Not blocked by a lock on another row (and a token that isn't on the mark clears nothing).
            other = '00000000-0000-0000-0000-000000000000'
            assert await field_kinds._clear_flagged(pool, 1, 'parent_id', 'object', other) is False
            await holder.execute(
                "SELECT 1 FROM dataset_field_kinds WHERE dataset_id = 1 AND field_name = 'colour' FOR UPDATE",
            )
            assert await field_kinds._clear_flagged(pool, 1, 'colour', 'image', token) is None
            assert loop.time() - started < 4  # two 1 s limits and two short statements
        assert (await record(database))['colour'][0] is True

    registry_database(scenario)


def test_restore_recovery_lets_a_pass_forget_a_note_from_the_dump_and_restarts_a_pass_in_flight(
    monkeypatch,
    registry_database,
):
    """Review R4-M1, after a restore into a cluster whose transaction counter is below the dump's: a note's xact would
    never be visible to any pass, and a pass in flight would resume with the other cluster's snapshot. Until recovery
    runs the claim skips the kind; recovery sets the notes' xact to 3 and restarts the field_kinds job with an empty
    checkpoint. The pass then forgets the note, clears the mark no row needs, and the schedule queues nothing more."""

    async def scenario(database):
        rows = await farm(database)
        await stale_mark(database)
        await database.execute('DELETE FROM samples WHERE id = $1', rows['truck'])  # a note
        # As in a dump from a cluster whose counter is past this one's: the note's transaction, and a pass mid-check
        # (a unit done, waiting for the next) with that cluster's snapshot.
        future = str(await database.fetchval('SELECT txid_current() + 100000'))
        await database.execute('UPDATE dataset_field_kinds_pending SET xact = $1::text::xid8', future)
        job = await job_queue.enqueue_job(
            database.pool,
            field_kinds.KIND,
            dataset_id=1,
            lane='maintenance',
            dedupe_key=f'{field_kinds.KIND}:1',
        )
        state = {'snapshot': f'{future}:{future}:', 'marks': [['x', 'object']], 'next': 0, 'walk': None}
        foreign = {'phase': 'check', 'state': {**state, 'cleared': [], 'held': False}}
        await database.execute(
            'UPDATE jobs SET checkpoint = $2, started_at = clock_timestamp() WHERE id = $1',
            job['id'],
            foreign,
        )
        await database.execute('DELETE FROM transaction_high_water')  # restore detection, as for a missing mark
        assert await database.fetchval('SELECT restored_work_pending()')
        assert await job_queue.claim_job(database.pool, 'field-kinds-test', kinds=[field_kinds.KIND]) is None
        assert await job_queue.check_transaction_high_water(database.pool)
        assert await database.fetchval('SELECT xact::text FROM dataset_field_kinds_pending WHERE dataset_id = 1') == '3'
        restarted = await database.fetchrow('SELECT state, checkpoint FROM jobs WHERE id = $1', job['id'])
        assert (restarted['state'], restarted['checkpoint']) == ('queued', {})
        (done,) = await run_jobs(database, field_kinds.KIND, field_kinds.handler(database.pool))
        assert done['id'] == job['id'] and ['x', 'object'] in done['result']['cleared'], done['result']
        assert await database.fetchval('SELECT count(*) FROM dataset_field_kinds_pending WHERE dataset_id = 1') == 0
        monkeypatch.setattr(field_kinds, 'LEFTOVER_SECONDS', 0)
        assert await field_kinds.schedule(database.pool) == []

    registry_database(scenario)


# --- Round five: a database the migration hasn't run on yet, and the migration run a second time (contract 1.5) ---

# What a database looks like before 0044 runs, made inside the test's own database: the record's tables, the triggers
# that fill them and its functions gone (dropping a trigger function drops its trigger).
UNMIGRATED = """
DROP TABLE dataset_field_kinds, dataset_field_kinds_state, dataset_field_kinds_pending;
DROP FUNCTION field_kinds_inserted(), field_kinds_updated(), field_kinds_deleted(), field_kinds_truncated(),
    field_kinds_dataset_created() CASCADE;
DROP FUNCTION field_kinds_mark(integer, text[], text[]), field_kinds_note(integer), field_kinds_keys(jsonb),
    field_kinds_same_keys(jsonb, jsonb), require_field_kinds(integer, text[], text[]);
"""


def test_a_database_the_migration_has_not_run_on_reads_as_before_and_counts_its_statistics(registry_database):
    """Contract 1.5, added after the first full run: before 0044 runs (the server started before
    scripts/apply_migrations.sh), no dataset is examined and nothing is skipped. The field-stats answer on_images and
    on_objects true, a wrapped filter on a dataset with objects reads the same rows as on the migrated database, and
    the statistics count ends."""

    async def scenario(database):
        rows = await farm(database)
        await add(1, [('object', {'parent_id': rows['red'], 'score': 2})], 'farm/score')
        readings = [
            images(eq('label', 'car')),
            images(leaf('label', '!=', 'car')),
            objects(eq('colour', 'red')),
            objects(eq('score', 2)),
            images(eq('colour', 'blue'), eq('label', 'truck')),
            objects(NOT(AND(eq('colour', 'red'), eq('label', 'car')))),
        ]
        migrated = [await found(1, node) for node in readings]
        assert (await members())['label'] == (False, True)
        await database.execute(UNMIGRATED)
        assert await field_kinds.load(database, 1) is None
        fields = await members()
        assert fields and set(fields.values()) == {(True, True)}, fields
        assert [await found(1, node) for node in readings] == migrated
        job = await count_statistics(database)
        assert job['state'] == 'succeeded'
        await add(1, [(None, {'colour': 'green'})], 'farm/green')
        assert len(await found(1, images(eq('colour', 'green')))) == 1

    registry_database(scenario)


def test_the_migration_run_a_second_time_changes_nothing(registry_database):
    """Contract 1.5: 0044 runs a second time (as apply_migrations.sh runs a file, in one transaction) and leaves every
    row of the record's three tables as it was, marks, a note and a check's token included; the triggers still mark."""
    from pathlib import Path

    async def scenario(database):
        rows = await farm(database)
        await stale_mark(database)
        await database.execute('DELETE FROM samples WHERE id = $1', rows['truck'])  # a note
        assert await field_kinds._flag(database.pool, 1, 'label', 'object')  # a token

        async def contents() -> list:
            return [
                [tuple(row) for row in await database.fetch(sql)]
                for sql in (
                    'SELECT * FROM dataset_field_kinds ORDER BY dataset_id, field_name',
                    'SELECT * FROM dataset_field_kinds_state ORDER BY dataset_id',
                    'SELECT * FROM dataset_field_kinds_pending ORDER BY dataset_id, id',
                )
            ]

        before = await contents()
        assert before[0] and before[1] and before[2]
        migration = (Path(datasets.__file__).parents[1] / 'db/migrations/0044_field_kinds.sql').read_text()
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute(migration)
        assert await contents() == before
        triggers = await database.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_%field_kinds%' AND NOT tgisinternal "
            "AND tgrelid IN ('samples'::regclass, 'datasets'::regclass)",
        )
        assert triggers == 5
        await add(1, [('object', {'parent_id': rows['red'], 'fresh': 1})], 'farm/fresh')
        assert (await record(database))['fresh'] == (False, True)

    registry_database(scenario)
