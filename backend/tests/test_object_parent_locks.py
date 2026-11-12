"""An edit of an image and the objects written for it don't wait for each other. Writing an object checks its image
through the parent key (0030), which takes FOR KEY SHARE on the image's row; the edits that lock a sample before
writing it (one sample's PATCH, a tag edit, a metadata set) never write id or dataset_id, so they lock FOR NO KEY
UPDATE, which that check doesn't wait for. Two edits of one sample still serialize."""

import os

import asyncpg
import pytest

from backend.routers import datasets
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_object_parent_column import add, ids

registry_database = registry_helpers.registry_database
USER = registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


class _Locking:
    """A connection of the edit under test: right after its statement that locks samples rows, `held` runs, while the
    edit's transaction (and its lock) is still open."""

    def __init__(self, connection, held):
        self._connection = connection
        self._held = held

    def __getattr__(self, name):
        return getattr(self._connection, name)

    async def _run(self, method, query, *args, **kwargs):
        result = await getattr(self._connection, method)(query, *args, **kwargs)
        if 'FROM samples' in query and ' FOR ' in query and 'UPDATE' in query:
            await self._held()
        return result

    async def fetch(self, query, *args, **kwargs):
        return await self._run('fetch', query, *args, **kwargs)

    async def fetchrow(self, query, *args, **kwargs):
        return await self._run('fetchrow', query, *args, **kwargs)

    async def fetchval(self, query, *args, **kwargs):
        return await self._run('fetchval', query, *args, **kwargs)


class _Acquire:
    def __init__(self, pool, held):
        self._context = pool.acquire()
        self._held = held

    async def __aenter__(self):
        return _Locking(await self._context.__aenter__(), self._held)

    async def __aexit__(self, *exc):
        return await self._context.__aexit__(*exc)


class _Pool:
    def __init__(self, pool, held):
        self._pool = pool
        self._held = held

    def __getattr__(self, name):
        return getattr(self._pool, name)

    def acquire(self, *args, **kwargs):
        return _Acquire(self._pool, self._held)


class _Database:
    def __init__(self, database, held):
        self._database = database
        self.pool = _Pool(database.pool, held)

    def __getattr__(self, name):
        return getattr(self._database, name)


EDITS = {
    'one sample': lambda image: datasets.update_sample(
        1,
        image,
        datasets.SampleUpdate(metadata={'checked': True}),
        user=USER,
    ),
    'tags': lambda image: datasets.bulk_tag_samples(
        1,
        datasets.BulkTagRequest(sample_ids=[image], add_tags=['checked']),
        USER,
    ),
    'metadata': lambda image: datasets.bulk_set_metadata(
        1,
        datasets.BulkSetRequest(sample_ids=[image], metadata={'checked': True}),
        USER,
    ),
}


@pytest.mark.parametrize('edit', list(EDITS))
def test_an_object_written_while_its_image_is_edited_does_not_wait_and_edits_still_serialize(
    registry_database,
    monkeypatch,
    edit,
):
    seen: dict = {}

    async def scenario(database):
        await add(1, [(None, {'crop': 'corn'})], 's3://l/img')
        (image,) = await ids(database, 1, 's3://l/img')

        async def held():
            if seen:
                return
            async with database.pool.acquire() as other:
                async with other.transaction():
                    await other.execute("SET LOCAL lock_timeout = '300ms'")
                    await other.execute(
                        "INSERT INTO samples (dataset_id, filepath, media_type, metadata) "
                        "VALUES (1, 's3://l/obj-0.jpg', 'object', jsonb_build_object('parent_id', $1::int))",
                        image,
                    )
                seen['object'] = 'written'
                try:
                    async with other.transaction():
                        await other.execute("SET LOCAL lock_timeout = '300ms'")
                        await other.execute("UPDATE samples SET heading = 90 WHERE id = $1", image)
                    seen['second edit'] = 'written'
                except asyncpg.LockNotAvailableError:
                    seen['second edit'] = 'waited'

        monkeypatch.setattr(datasets, 'db', _Database(database, held))
        await EDITS[edit](image)
        (obj,) = await ids(database, 1, 's3://l/obj')
        assert await database.fetchval('SELECT parent_id FROM samples WHERE id = $1', obj) == image

    registry_database(scenario)
    assert seen == {'object': 'written', 'second edit': 'waited'}
