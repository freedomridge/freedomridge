"""A mirror's layout ids (follower_sync.write_layouts): the primary sends each map's id with the transaction that made
it, and the mirror takes it once its feed has passed that transaction, never before. Its copy of the primary's
coordinates never makes an id; its own maps count here; a primary before 2.11 sends none, and the mirror's stay."""

import os
import uuid

import pytest

from backend import follower_sync
from backend.routers import followers
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_follower_sync import mirror_of, setup, sync

registry_database = registry_helpers.registry_database
ingest, USER = registry_helpers.ingest, registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def layout(database, dataset_id, field='clip'):
    return await database.fetchval(
        'SELECT layout_id::text FROM embedding_layouts WHERE dataset_id = $1 AND field_name = $2',
        dataset_id,
        field,
    )


async def mapped(database, fields=('clip',), dataset_id=1, prefix='s3://images/a'):
    """Samples of a dataset on a map of each field, written without a new layout (as before the upgrade)."""
    await ingest([{'crop': 'corn'}, {'crop': 'soy'}, {'crop': 'oat'}], dataset_id=dataset_id, prefix=prefix)
    ids = [
        row['id']
        for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id', dataset_id)
    ]
    for field in fields:
        await embed(database, ids, field)
    return ids


async def embed(database, ids, field):
    await database.execute(
        "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
        "SELECT s.dataset_id, s.id, $2, ARRAY[s.id, 1]::real[]::vector FROM samples s WHERE s.id = ANY($1::int[])",
        ids,
        field,
    )
    await database.execute(
        'INSERT INTO embedding_coordinates (sample_id, field_name, x, y) SELECT i, $2, i, -i FROM unnest($1::int[]) i',
        ids,
        field,
    )


async def new_layout(database, field='clip', dataset_id=1, shift=100.0):
    """A new layout of a map on the primary: its points move and it gets a new id, in one transaction."""
    async with database.pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """UPDATE embedding_coordinates c SET x = c.x + $3 FROM samples s
               WHERE s.id = c.sample_id AND s.dataset_id = $1 AND c.field_name = $2""",
            dataset_id,
            field,
            shift,
        )
        made = await connection.fetchval('SELECT bump_map_layout($1, $2)::text', dataset_id, field)
        return made, await connection.fetchval('SELECT txid_current()')


async def mirror_xs(database, mirror, field='clip'):
    return sorted(
        row['x']
        for row in await database.fetch(
            'SELECT c.x FROM embedding_coordinates c JOIN samples s ON s.id = c.sample_id '
            'WHERE s.dataset_id = $1 AND c.field_name = $2',
            mirror,
            field,
        )
    )


async def follow(database, link_id):
    follower = await database.fetchval('SELECT id FROM followers')
    await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
    await sync(database, link_id)
    return (await mirror_of(database))['dataset_id']


def test_a_copy_takes_the_primary_s_layout_ids_of_its_maps(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await mapped(database, ('clip', 'clip_patch_2x2_r0c0', 'dino'))
        clip, _ = await new_layout(database)
        patch, _ = await new_layout(database, 'clip_patch_2x2_r0c0')
        mirror = await follow(database, link_id)
        # Taken once the mirror's feed has passed the primary's read of them, in the sync that copies the dataset.
        assert await layout(database, mirror) == clip
        assert await layout(database, mirror, 'clip_patch_2x2') == patch
        # A map not laid out again since the primary's upgrade has no id there, and none here.
        assert await layout(database, mirror, 'dino') is None
        assert await database.fetchval('SELECT count(*) FROM embedding_layouts WHERE dataset_id = $1', mirror) == 2

    registry_database(scenario)


def test_a_new_layout_s_id_waits_until_the_feed_has_passed_it_and_the_copy_makes_none(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await mapped(database)
        first, _ = await new_layout(database)
        mirror = await follow(database, link_id)
        assert await layout(database, mirror) == first

        second, made_in = await new_layout(database)
        # The sync step by step, as the runner runs it: after every committed step, the mirror names the new layout
        # only once its feed has passed the transaction that made it, and then has all of its points.
        step = follower_sync.handler(database.pool)
        checkpoint: dict = {}
        waited = False
        for _ in range(200):
            result = await step({'args': {'link_id': link_id}, 'checkpoint': checkpoint})
            if result.publish is not None:
                async with database.pool.acquire() as connection, connection.transaction():
                    await result.publish(connection)
            if result.checkpoint is not None:
                checkpoint = result.checkpoint
            row = await database.fetchrow(
                "SELECT layout_id::text AS layout_id, pending_layout_id::text AS pending FROM embedding_layouts "
                "WHERE dataset_id = $1 AND field_name = 'clip'",
                mirror,
            )
            cursor = await database.fetchval('SELECT feed_cursor FROM mirrors WHERE dataset_id = $1', mirror)
            if row['layout_id'] == second:
                assert cursor >= made_in
                assert all(x >= 200 for x in await mirror_xs(database, mirror))
            else:
                assert row['layout_id'] == first
                waited = waited or row['pending'] == second  # read with the map's state, not taken yet
            if result.finished:
                break
        assert waited and await layout(database, mirror) == second

        # The primary's points move without a new layout: the mirror's copy moves them and makes no id of its own.
        ys = 'SELECT c.y FROM embedding_coordinates c JOIN samples s ON s.id = c.sample_id WHERE s.dataset_id = $1'
        before = sorted(row['y'] for row in await database.fetch(ys, mirror))
        await database.execute(
            """UPDATE embedding_coordinates c SET y = c.y - 7 FROM samples s
               WHERE s.id = c.sample_id AND s.dataset_id = 1 AND c.field_name = 'clip'""",
        )
        await sync(database, link_id)
        assert sorted(row['y'] for row in await database.fetch(ys, mirror)) == [y - 7 for y in before]
        assert await layout(database, mirror) == second

    registry_database(scenario)


def test_a_mirror_s_own_map_counts_here_until_a_primary_field_of_its_name_replaces_it(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await mapped(database)
        mirror = await follow(database, link_id)
        local = [row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = $1', mirror)]
        await embed(database, local, 'dino')  # this instance's own field and map
        own = await database.fetchval("SELECT bump_map_layout($1, 'dino')::text", mirror)
        clip, _ = await new_layout(database)
        await sync(database, link_id)
        assert await layout(database, mirror) == clip
        assert await layout(database, mirror, 'dino') == own

        # The primary makes a field of that name: the mirror's own map goes with its vectors, and the map gets a new
        # id with them, so a lasso drawn on this instance's map (with its id, or with none) warns...
        primary = [row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = 1')]
        await embed(database, primary, 'dino')
        await sync(database, link_id)
        assert 'dino' in (await mirror_of(database))['vector_fields']
        assert await layout(database, mirror, 'dino') not in (None, own)
        # ...and the primary's id follows once the primary lays its map out.
        theirs, _ = await new_layout(database, 'dino')
        await sync(database, link_id)
        assert await layout(database, mirror, 'dino') == theirs != own

    registry_database(scenario)


def test_ids_the_feed_has_passed_are_taken_at_once_and_others_when_it_passes_them(registry_database):
    async def scenario(database):
        # Dataset 2 mirrors a primary's clip map and a patch scale; dino is its own.
        await database.execute(
            'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor, vector_fields) '
            "VALUES (2, 7, 100, '{clip,clip_patch_2x2_r0c0}')",
        )
        own = await database.fetchval("SELECT bump_map_layout(2, 'dino')::text")
        await database.execute(
            'INSERT INTO embedding_layouts (dataset_id, field_name, layout_id) '
            "VALUES (2, 'clip_patch_2x2', gen_random_uuid())",
        )
        ahead, behind, patch = (str(uuid.uuid4()) for _ in range(3))

        async def write(layouts):
            async with database.pool.acquire() as connection, connection.transaction():
                await follower_sync.write_layouts(connection, 2, layouts)

        async def advance(cursor):
            async with database.pool.acquire() as connection, connection.transaction():
                await follower_sync.advance_cursor(connection, 2, cursor)

        await write(
            [
                {'field_name': 'clip', 'layout_id': ahead, 'txid': 150},
                {'field_name': 'dino', 'layout_id': str(uuid.uuid4()), 'txid': 50},  # the primary's, not this map
            ],
        )
        assert await layout(database, 2) is None  # made after the feed's cursor: it waits
        assert await layout(database, 2, 'dino') == own
        held = await layout(database, 2, 'clip_patch_2x2')
        assert held is not None  # not sent: kept (a copy started again forgets them, forget_primary_layouts)
        await advance(149)
        assert await layout(database, 2) is None
        await advance(150)
        assert await layout(database, 2) == ahead

        await write(
            [
                {'field_name': 'clip', 'layout_id': behind, 'txid': 120},
                {'field_name': 'clip_patch_2x2', 'layout_id': patch, 'txid': 150},
            ],
        )
        assert await layout(database, 2) == behind  # the feed passed it already
        assert await layout(database, 2, 'clip_patch_2x2') == patch
        assert not await database.fetchval('SELECT count(*) FROM embedding_layouts WHERE pending_layout_id IS NOT NULL')

    registry_database(scenario)


def test_a_primary_before_2_10_sends_no_ids_and_the_mirror_keeps_its_own(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        call = follower_sync.call

        async def older(client, method, path, **kwargs):
            answer = await call(client, method, path, **kwargs)
            if path.endswith('/state'):
                answer.pop('layouts')
            return answer

        monkeypatch.setattr(follower_sync, 'call', older)
        await mapped(database)
        await new_layout(database)  # an id the older primary wouldn't have, and doesn't send
        mirror = await follow(database, link_id)
        assert all(x >= 100 for x in await mirror_xs(database, mirror))
        assert await layout(database, mirror) is None
        local = [row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = $1', mirror)]
        await embed(database, local, 'dino')
        own = await database.fetchval("SELECT bump_map_layout($1, 'dino')::text", mirror)
        await new_layout(database)
        await sync(database, link_id)
        assert await layout(database, mirror) is None and await layout(database, mirror, 'dino') == own

    registry_database(scenario)


def test_two_layouts_committed_out_of_order_reach_the_mirror_only_with_all_their_points(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        ids = await mapped(database)
        # The case shows only with the two points in different sample blocks (4096 ids): the follower re-reads
        # coordinates a whole block at a time, so B's point in A's block would arrive with A's page anyway.
        await database.execute("SELECT setval(pg_get_serial_sequence('samples', 'id'), $1)", followers.BLOCK + 10)
        await ingest([{'crop': 'rye'}], dataset_id=1, prefix='s3://images/b')
        far = await database.fetchval('SELECT max(id) FROM samples WHERE dataset_id = 1')
        assert far // followers.BLOCK != ids[0] // followers.BLOCK
        await embed(database, [far], 'clip')
        first, _ = await new_layout(database)
        mirror = await follow(database, link_id)
        assert await layout(database, mirror) == first
        call = follower_sync.call

        async def one_per_page(client, method, path, **kwargs):
            if path.endswith('/changes'):  # one transaction per feed page: the cursor can stop between the two below
                kwargs['params'] = {**kwargs['params'], 'transactions': 1}
            return await call(client, method, path, **kwargs)

        monkeypatch.setattr(follower_sync, 'call', one_per_page)
        # A takes its transaction id first. B moves a point, lays the map out and commits; then A does the same. The map
        # ends with A's id, made by the older transaction, and the layout it names includes B's point.
        move = "UPDATE embedding_coordinates SET x = x + 100 WHERE sample_id = $1 AND field_name = 'clip'"
        async with database.pool.acquire() as a, database.pool.acquire() as b:
            open_a = a.transaction()
            await open_a.start()
            older = await a.fetchval('SELECT txid_current()')
            async with b.transaction():
                await b.execute(move, far)
                await b.execute("SELECT bump_map_layout(1, 'clip')")
                newer = await b.fetchval('SELECT txid_current()')
            await a.execute(move, ids[0])
            last = await a.fetchval("SELECT bump_map_layout(1, 'clip')::text")
            await open_a.commit()
        assert older < newer

        step = follower_sync.handler(database.pool)
        checkpoint: dict = {}
        waited = False
        for _ in range(400):
            result = await step({'args': {'link_id': link_id}, 'checkpoint': checkpoint})
            if result.publish is not None:
                async with database.pool.acquire() as connection, connection.transaction():
                    await result.publish(connection)
            if result.checkpoint is not None:
                checkpoint = result.checkpoint
            row = await database.fetchrow(
                "SELECT layout_id::text AS named, pending_layout_id::text AS pending FROM embedding_layouts "
                "WHERE dataset_id = $1 AND field_name = 'clip'",
                mirror,
            )
            if row['named'] == last:  # never before both moved points are here
                moved = await database.fetch(
                    """SELECT c.x FROM embedding_coordinates c JOIN mirror_samples m ON m.sample_id = c.sample_id
                       WHERE m.dataset_id = $1 AND c.field_name = 'clip' AND m.origin_id = ANY($2::int[])""",
                    mirror,
                    [ids[0], far],
                )
                assert sorted(row['x'] for row in moved) == sorted(float(i) + 200 for i in (ids[0], far))
            else:
                assert row['named'] == first
                waited = waited or row['pending'] == last
            if result.finished:
                break
        assert waited and await layout(database, mirror) == last

    registry_database(scenario)


def test_the_primary_stamps_each_id_with_the_last_transaction_its_read_could_see(monkeypatch, registry_database):
    async def scenario(database):
        await setup(monkeypatch, database)
        follower = dict(await database.fetchrow('SELECT * FROM followers'))
        await followers.follow_dataset(follower['id'], 1, followers.FollowOptions(), user=USER)
        async with database.pool.acquire() as connection, connection.transaction():
            made = await connection.fetchval("SELECT bump_map_layout(1, 'clip')::text")
            bumped = await connection.fetchval('SELECT txid_current()')
        # A later transaction, of anything: the read that serves the id sees it too.
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("UPDATE datasets SET description = 'later' WHERE id = 2")
            later = await connection.fetchval('SELECT txid_current()')
        state = await followers.follower_state(1, follower=follower)
        [entry] = state['layouts']
        assert entry['field_name'] == 'clip' and entry['layout_id'] == made
        assert entry['txid'] >= later > bumped

    registry_database(scenario)


def test_a_copy_started_again_forgets_the_primary_s_old_ids_and_takes_what_it_has_now(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await mapped(database, ('clip', 'dino'))
        await new_layout(database)
        dino, _ = await new_layout(database, 'dino')
        mirror = await follow(database, link_id)
        assert await layout(database, mirror) is not None and await layout(database, mirror, 'dino') == dino
        # As after a restore of the primary from before clip's id: it has no id for clip now. A cursor from before
        # the copy's start makes the copy start again, into the same mirror.
        await database.execute("DELETE FROM embedding_layouts WHERE dataset_id = 1 AND field_name = 'clip'")
        await database.execute('UPDATE mirrors SET feed_cursor = 0')
        await sync(database, link_id)
        assert (await mirror_of(database))['dataset_id'] == mirror
        assert await layout(database, mirror) is None
        assert await layout(database, mirror, 'dino') == dino

    registry_database(scenario)
