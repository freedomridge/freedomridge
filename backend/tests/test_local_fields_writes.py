"""Writing a mirror's own fields (docs/FOLLOWERS.md): `@local.<name>` keys through update-sample and bulk-set, and
`@local.tags` through bulk-tag, inline and as a background job. They change only local_metadata and local_tags,
only on a mirror that still follows, and never in the same request as the primary's fields."""

import os

import asyncpg
import pytest
from fastapi import HTTPException

from backend.routers import datasets
from backend.tests import test_bulk_jobs
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
ingest, USER = registry_helpers.ingest, registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
CORN = {'field': 'crop', 'operator': '=', 'value': 'corn'}
FIELDS = 'SELECT field_name, field_type, origin, owner FROM dataset_fields WHERE dataset_id = 2 ORDER BY field_name'


async def mirror(database, count=3):
    await ingest([{'crop': 'corn'} for _ in range(count)], dataset_id=2, prefix='s3://images/m')
    await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')
    # Ingested before it became a mirror: its fields marked the primary's, as migration 0022 does.
    await database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = 2")
    return [row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = 2 ORDER BY id')]


async def rows(database):
    return await database.fetch(
        'SELECT id, metadata, local_metadata, local_tags, tags, last_modified_at FROM samples WHERE dataset_id = 2 '
        'ORDER BY id',
    )


def test_update_sample_writes_this_servers_own_fields(registry_database):
    async def scenario(database):
        first, _, _ = await mirror(database)
        before = await rows(database)
        update = datasets.SampleUpdate
        await datasets.update_sample(2, first, update(metadata={'@local.weeds': 3, '@local.kind': 'dock'}), user=USER)
        await datasets.update_sample(2, first, update(array_fields={'@local.tags': [' checked ']}), user=USER)
        after = await rows(database)
        assert (after[0]['local_metadata'], after[0]['local_tags']) == ({'weeds': 3, 'kind': 'dock'}, ['checked'])
        # The primary's columns, last_modified_at included, are as they were.
        for key in ('metadata', 'tags', 'last_modified_at'):
            assert after[0][key] == before[0][key]
        assert {row['field_name']: tuple(row)[1:] for row in await database.fetch(FIELDS)} == {
            '@local.kind': ('string', 'local', 'self'),
            '@local.tags': ('list', 'local', 'self'),
            '@local.weeds': ('number', 'local', 'self'),
            'crop': ('string', 'metadata', 'primary'),
        }

        # Removing a field from its last sample unregisters it.
        await datasets.update_sample(2, first, update(remove_metadata_keys=['@local.kind']), user=USER)
        assert (await rows(database))[0]['local_metadata'] == {'weeds': 3}
        assert '@local.kind' not in {row['field_name'] for row in await database.fetch(FIELDS)}

        # What a local write may not be.
        refusals = [
            (2, update(metadata={'@local.weeds': 1, 'crop': 'rye'}), 409, 'changes nothing else'),
            (2, update(metadata={'@local.weeds': 1}, tags=['x']), 409, 'changes nothing else'),
            (2, update(metadata={'@local.tags': ['x']}), 400, 'bulk-tag'),
            (2, update(metadata={'@local.a.b': 1}), 400, 'Invalid field name'),
            (1, update(metadata={'@local.weeds': 1}), 400, 'Only a mirror'),
        ]
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/p')
        own = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 1')
        for dataset_id, request, status, detail in refusals:
            with pytest.raises(HTTPException) as refused:
                await datasets.update_sample(dataset_id, own if dataset_id == 1 else first, request, user=USER)
            assert (refused.value.status_code, detail in refused.value.detail) == (status, True), refused.value
        # The primary's fields of a mirror stay read-only.
        with pytest.raises((HTTPException, asyncpg.PostgresError)):
            await datasets.update_sample(2, first, update(metadata={'crop': 'rye'}), user=USER)
        # A mirror that stopped following keeps its own fields read-only too.
        await database.execute("UPDATE mirrors SET state = 'stopped' WHERE dataset_id = 2")
        with pytest.raises(HTTPException) as refused:
            await datasets.update_sample(2, first, update(metadata={'@local.weeds': 4}), user=USER)
        assert refused.value.status_code == 409 and 'no longer follows' in refused.value.detail
        assert (await rows(database))[0]['local_metadata'] == {'weeds': 3}

    registry_database(scenario)


def test_bulk_set_writes_this_servers_own_fields_inline_and_in_a_job(registry_database):
    async def scenario(database):
        first, second, third = await mirror(database)
        before = await rows(database)
        bulk = datasets.BulkSetRequest
        update = datasets.BulkSetUpdate
        await datasets.bulk_set_metadata(2, bulk(sample_ids=[first, second], metadata={'@local.weeds': 2}), user=USER)
        per_sample = [update(sample_id=first, metadata={'@local.score': 0.5}), update(sample_id=third, metadata={})]
        await datasets.bulk_set_metadata(2, bulk(updates=per_sample), user=USER)
        # In a background job: every sample of the filter.
        queued = await datasets.bulk_set_metadata(
            2,
            bulk(filters={'field': 'crop', 'operator': '=', 'value': 'corn'}, metadata={'@local.reviewed': True}),
            user=USER,
        )
        assert queued.status_code == 202
        await test_bulk_jobs.finish(database)
        after = await rows(database)
        assert [row['local_metadata'] for row in after] == [
            {'weeds': 2, 'score': 0.5, 'reviewed': True},
            {'weeds': 2, 'reviewed': True},
            {'reviewed': True},
        ]
        assert [row['metadata'] for row in after] == [row['metadata'] for row in before]
        assert [row['last_modified_at'] for row in after] == [row['last_modified_at'] for row in before]
        kinds = {row['field_name']: row['field_type'] for row in await database.fetch(FIELDS)}
        assert (kinds['@local.weeds'], kinds['@local.score'], kinds['@local.reviewed']) == (
            'number',
            'number',
            'boolean',
        )

        # Removed by a job from every sample, the field goes from the registry.
        await datasets.bulk_set_metadata(2, bulk(filters={}, remove_metadata_keys=['@local.weeds']), user=USER)
        await test_bulk_jobs.finish(database)
        assert all('weeds' not in row['local_metadata'] for row in await rows(database))
        assert '@local.weeds' not in {row['field_name'] for row in await database.fetch(FIELDS)}

        with pytest.raises(HTTPException) as refused:
            await datasets.bulk_set_metadata(
                2,
                bulk(sample_ids=[first], metadata={'@local.x': 1, 'crop': 'x'}),
                user=USER,
            )
        assert refused.value.status_code == 409

    registry_database(scenario)


def test_bulk_tag_writes_this_servers_own_tags_and_never_the_primarys(registry_database):
    async def scenario(database):
        first, second, third = await mirror(database)
        # A tags option both ways sends `tags` edits to the primary; `@local.tags` stays here whatever it says.
        await database.execute("UPDATE mirrors SET tags = 'both' WHERE dataset_id = 2")
        tag = datasets.BulkTagRequest
        await datasets.bulk_tag_samples(
            2,
            tag(field_name='@local.tags', sample_ids=[first, second], add_tags=['checked', 'redo']),
            user=USER,
        )
        await datasets.bulk_tag_samples(
            2,
            tag(field_name='@local.tags', sample_ids=[second], remove_tags=['redo']),
            user=USER,
        )
        queued = await datasets.bulk_tag_samples(
            2,
            tag(field_name='@local.tags', filters=CORN, add_tags=['seen']),
            user=USER,
        )
        assert queued.status_code == 202
        await test_bulk_jobs.finish(database)
        after = await rows(database)
        assert [sorted(row['local_tags']) for row in after] == [
            ['checked', 'redo', 'seen'],
            ['checked', 'seen'],
            ['seen'],
        ]
        assert all(row['tags'] == [] for row in after)
        registered = await database.fetch(
            "SELECT tag FROM dataset_tags WHERE dataset_id = 2 AND field_name = '@local.tags' ORDER BY tag",
        )
        assert [row['tag'] for row in registered] == ['checked', 'redo', 'seen']

        with pytest.raises(HTTPException) as refused:
            await datasets.bulk_tag_samples(
                1,
                tag(field_name='@local.tags', sample_ids=[first], add_tags=['x']),
                user=USER,
            )
        assert refused.value.status_code == 400 and 'Only a mirror' in refused.value.detail

    registry_database(scenario)


def test_a_removed_local_field_is_unregistered_in_bounded_windows_and_never_the_primarys(
    registry_database,
    monkeypatch,
):
    """local_metadata has no index, so whether a sample still holds a removed field is read in windows of ids: a
    request reads a bounded number of them, and a job walks the rest one window per step. A job removing
    `@local.weeds` unregisters only that, never the primary's `weeds`, even with no sample holding it."""
    from backend import local_writes

    async def scenario(database):
        first, second, _ = await mirror(database)
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('fridge.mirror_sync', '2', TRUE)")
            await connection.execute(
                "INSERT INTO dataset_fields (dataset_id, field_name, field_type) VALUES (2, 'weeds', 'number')",
            )
        monkeypatch.setattr(local_writes, 'PRUNE_WINDOW', 1)  # one sample per window: the walk takes steps
        bulk, update = datasets.BulkSetRequest, datasets.SampleUpdate
        await datasets.bulk_set_metadata(2, bulk(sample_ids=[first, second], metadata={'@local.weeds': 1}), user=USER)

        async def registered():
            return {row['field_name']: row['owner'] for row in await database.fetch(FIELDS)}

        # Still held by the second sample: it stays.
        await datasets.update_sample(2, first, update(remove_metadata_keys=['@local.weeds']), user=USER)
        assert '@local.weeds' in await registered()
        # Held by none, but the request reads one window only: it stays until a job reads the rest.
        monkeypatch.setattr(local_writes, 'INLINE_PRUNE_ROWS', 1)
        await datasets.update_sample(2, second, update(remove_metadata_keys=['@local.weeds']), user=USER)
        assert '@local.weeds' in await registered()
        await datasets.bulk_set_metadata(2, bulk(filters={}, remove_metadata_keys=['@local.weeds']), user=USER)
        await test_bulk_jobs.finish(database)
        owners = await registered()
        assert '@local.weeds' not in owners and owners['weeds'] == 'primary'

        # A write of the field into a window the walk has already read keeps it registered.
        await datasets.bulk_set_metadata(2, bulk(sample_ids=[second], metadata={'@local.kind': 'dock'}), user=USER)
        await datasets.bulk_set_metadata(2, bulk(filters={}, remove_metadata_keys=['@local.kind']), user=USER)
        job = await test_bulk_jobs.claim(database)
        while not (job['checkpoint'] or {}).get('prune_after'):
            job = await test_bulk_jobs.step(database, job)
        await datasets.update_sample(2, first, update(metadata={'@local.kind': 'thistle'}), user=USER)
        await test_bulk_jobs.finish(database, job)
        assert '@local.kind' in await registered()
        assert (await rows(database))[0]['local_metadata'] == {'kind': 'thistle'}

    registry_database(scenario)
