"""After 0042, no path brings a metadata key named tags or media_type back into samples: a revert to a version saved
before it, with the samples it re-creates, and a bulk edit queued before the keys were refused leave them out, and a
rename queued onto one of the names fails instead of writing it. A sample that still carries one is edited like any
other, and the key can be removed."""

import json
import os

import pytest

from backend.routers import datasets, versions
from backend.tests import test_field_registry_postgres
from backend.tests.test_bulk_jobs import claim, finish, step, submitted
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

OLD = '{"tags": "old", "media_type": "scan"}'  # as an older release stored them


async def metadata(database, ids):
    rows = await database.fetch('SELECT id, metadata FROM samples WHERE id = ANY($1::int[]) ORDER BY id', ids)
    return [row['metadata'] for row in rows]


def test_a_revert_to_a_version_saved_with_the_keys_leaves_them_out(registry_database):
    async def scenario(database):
        ids = (await ingest([{'n': 0}, {'n': 1}, {'n': 2}]))['ids']
        await database.execute(f"UPDATE samples SET metadata = metadata || '{OLD}'::jsonb WHERE id = ANY($1)", ids)
        version = await versions.create_version(1, versions.CreateVersionRequest(name='with the keys'))
        # The migration's change, then a sample deleted and another changed after it.
        await database.execute("UPDATE samples SET metadata = metadata - ARRAY['tags', 'media_type']")
        await database.execute('DELETE FROM samples WHERE id = $1', ids[2])
        await database.execute("UPDATE samples SET metadata = metadata || '{\"n\": 10}' WHERE id = $1", ids[1])
        stamped = await database.fetchval('SELECT last_modified_at FROM samples WHERE id = $1', ids[0])

        await versions.revert_to_version(1, version['id'], restore_deleted=True, user=USER)

        assert await metadata(database, ids) == [{'n': 0}, {'n': 1}, {'n': 2}]  # the re-created one too
        # A row whose saved metadata differs only by the keys is no change: not stamped modified.
        assert await database.fetchval('SELECT last_modified_at FROM samples WHERE id = $1', ids[0]) == stamped

    registry_database(scenario)


def test_bulk_edits_queued_before_the_refusal_leave_the_keys_out(registry_database):
    async def scenario(database):
        ids = (await ingest([{'n': 0}, {'n': 1}]))['ids']
        # Same value for every sample: queued with the keys, as an older release queued it.
        job = submitted(
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(background=True, sample_ids=[ids[0]], metadata={'kept': 1}),
                USER,
            ),
        )
        await database.execute(
            f"UPDATE jobs SET args = jsonb_set(args, '{{metadata}}', args->'metadata' || '{OLD}'::jsonb) WHERE id = $1",
            job,
        )
        await finish(database)
        # Each its own value: the queued patches carry the keys.
        updates = [datasets.BulkSetUpdate(sample_id=ids[1], metadata={'kept': 2})]
        job = submitted(
            await datasets.bulk_set_metadata(1, datasets.BulkSetRequest(background=True, updates=updates), USER),
        )
        await database.execute(f"UPDATE bulk_job_targets SET patch = patch || '{OLD}'::jsonb WHERE job_id = $1", job)
        await finish(database)

        assert await metadata(database, ids) == [{'n': 0, 'kept': 1}, {'n': 1, 'kept': 2}]
        fields = {row['field_name'] for row in await database.fetch('SELECT field_name FROM dataset_fields')}
        assert {'tags', 'media_type'}.isdisjoint(fields)

    registry_database(scenario)


def test_a_rename_queued_onto_a_protected_name_fails_and_writes_nothing(registry_database):
    async def scenario(database):
        ids = (await ingest([{'note': 'a'}, {'note': 'b'}]))['ids']
        job = submitted(
            await datasets.rename_field(1, 'note', datasets.FieldRenameRequest(new_name='memo', background=True), USER),
        )
        await database.execute("UPDATE jobs SET args = jsonb_set(args, '{new_name}', '\"tags\"') WHERE id = $1", job)
        claimed = await claim(database)
        with pytest.raises(ValueError, match=r"\['tags'\] name sample columns"):
            for _ in range(20):  # the units before the one that writes
                claimed = await step(database, claimed)
        assert await metadata(database, ids) == [{'note': 'a'}, {'note': 'b'}]

    registry_database(scenario)


def test_a_sample_still_carrying_a_key_is_edited_with_other_keys_and_the_key_removed(registry_database):
    async def scenario(database):
        ids = (await ingest([{'n': 0}]))['ids']
        await database.execute(f"UPDATE samples SET metadata = metadata || '{OLD}'::jsonb WHERE id = $1", ids[0])
        await datasets.update_sample(1, ids[0], datasets.SampleUpdate(metadata={'n': 5}), USER)
        assert await metadata(database, ids) == [json.loads('{"n": 5, "tags": "old", "media_type": "scan"}')]
        await datasets.update_sample(1, ids[0], datasets.SampleUpdate(remove_metadata_keys=['tags']), USER)
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=ids, remove_metadata_keys=['media_type']),
            USER,
        )
        assert await metadata(database, ids) == [{'n': 5}]
        with pytest.raises(ValueError, match='name sample columns'):
            datasets.SampleUpdate(metadata={'tags': 'new'})

    registry_database(scenario)


def test_names_that_only_look_alike_are_ordinary_keys():
    ordinary = {'@local.tags': ['mine'], 'source_tags': 1, 'tags_v2': 2, 'media_type_guess': 'image', 'heading': 90}
    assert datasets.SampleUpdate(metadata=ordinary).metadata == ordinary  # @local.tags: a mirror's own tags field
    assert datasets.BulkSetRequest(sample_ids=[1], metadata=ordinary).metadata == ordinary
