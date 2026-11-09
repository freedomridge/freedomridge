"""The follower's sync (follower_sync.py) against a real primary: one test database plays both parts. Dataset 1 is
the primary's; the sync copies it into a mirror beside it through the real follower endpoints (an in-process HTTP
client bound to the app), then applies the primary's feed to it."""

import os
from unittest.mock import AsyncMock

import asyncpg
import httpx
import numpy as np
import pytest
from fastapi import HTTPException

from backend import follower_sync
from backend.crypto import decrypt_secret, encrypt_secret
from backend.models import ViewCreate
from backend.routers import auth, datasets, followers, primaries, schedules, views
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
ingest, USER = registry_helpers.ingest, registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
CONCEPT_FILTER = {'field': 'clip', 'operator': 'concept_select', 'value': {'concept_id': 1, 'threshold': 0.5}}


async def setup(monkeypatch, database):
    """A follower 'lab' of dataset 1 on the primary, and this instance's link to it."""
    from backend.main import app

    monkeypatch.setattr(followers, 'db', database)
    monkeypatch.setattr(auth, 'db', database)
    monkeypatch.setattr(follower_sync, 'require_free_space', AsyncMock())
    # Datasets 1 and 2 were inserted with explicit ids; the mirror takes the next one.
    await database.execute("SELECT setval(pg_get_serial_sequence('datasets', 'id'), 2)")
    created = await followers.create_follower(followers.FollowerCreate(name='lab'), user=USER)
    link_id = await database.fetchval(
        "INSERT INTO primary_links (url, token_encrypted, created_by) VALUES ('http://primary/api', $1, 1) "
        'RETURNING id',
        encrypt_secret(created['token']),
    )

    def connect(link):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=link['url'],
            headers={'Authorization': f"Bearer {decrypt_secret(link['token_encrypted'])}"},
        )

    monkeypatch.setattr(follower_sync, 'connect', connect)
    return created, link_id


async def sync(database, link_id):
    """One sync job, step by step, as the runner runs it (with its own pool, not the routers' db): each step's
    publish commits in its own transaction."""
    step = follower_sync.handler(database.pool)
    checkpoint: dict = {}
    for _ in range(200):
        result = await step({'args': {'link_id': link_id}, 'checkpoint': checkpoint})
        if result.publish is not None:
            async with database.pool.acquire() as connection, connection.transaction():
                await result.publish(connection)
        if result.checkpoint is not None:
            checkpoint = result.checkpoint
        if result.finished:
            return result
    raise AssertionError('The sync did not finish')


async def mirror_of(database):
    return dict(await database.fetchrow('SELECT * FROM mirrors ORDER BY dataset_id DESC LIMIT 1'))


async def copied(database, mirror):
    rows = await database.fetch(
        """SELECT m.origin_id, s.id, s.filepath, s.tags, s.metadata
           FROM mirror_samples m JOIN samples s ON s.id = m.sample_id
           WHERE m.dataset_id = $1 ORDER BY m.origin_id""",
        mirror['dataset_id'],
    )
    return {row['origin_id']: dict(row) for row in rows}


def test_a_follower_copies_a_dataset_and_then_follows_its_changes(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/a')
        corn, soy = [
            row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = 1 ORDER BY id')
        ]
        await ingest([{'parent_id': corn, 'label': 'weed'}], dataset_id=1, prefix='s3://images/objects')
        child = await database.fetchval("SELECT id FROM samples WHERE metadata->>'label' = 'weed'")
        await database.execute("UPDATE samples SET tags = '{reviewed}' WHERE id = $1", corn)
        await database.execute(
            "INSERT INTO embeddings (sample_id, field_name, vector) VALUES ($1, 'clip', '[0.5,0.25]'), "
            "($2, 'clip', '[1,0]')",
            corn,
            soy,
        )
        await database.execute(
            "INSERT INTO sample_hashes (sample_id, field_name, hash_hex, hash_bits, prefix) "
            "VALUES ($1, 'dhash_8', 'ff00', B'1111111100000000', B'1111111100000000')",
            corn,
        )
        corn_view = await database.fetchval(
            """INSERT INTO views (dataset_id, name, slug, content, is_public)
               VALUES (1, 'Corn', 'corn', $1::jsonb, TRUE) RETURNING id""",
            {'filters': {'logic': 'AND', 'filters': [{'field': 'id', 'operator': 'in', 'value': [corn, 999999]}]}},
        )
        await database.execute(
            """INSERT INTO views (dataset_id, name, slug, content, is_public)
               VALUES (1, 'Weedy', 'weedy', $1::jsonb, TRUE)""",
            {'filters': CONCEPT_FILTER},
        )
        await database.execute("INSERT INTO views (dataset_id, name, slug, content) VALUES (1, 'Mine', 'mine', '{}')")
        await database.execute('UPDATE datasets SET default_view_id = $1 WHERE id = 1', corn_view)
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(tags='both'), user=USER)

        # The first sync copies everything.
        await sync(database, link_id)
        mirror = await mirror_of(database)
        assert (mirror['state'], mirror['origin_dataset_id'], mirror['tags']) == ('following', 1, 'both')
        local = await database.fetchrow('SELECT * FROM datasets WHERE id = $1', mirror['dataset_id'])
        assert local['name'] == 'training (from primary)'  # 'training' is taken here: it's the primary itself
        samples = await copied(database, mirror)
        assert sorted(samples) == [corn, soy, child]
        assert samples[corn]['tags'] == ['reviewed'] and samples[corn]['filepath'] == 's3://images/a-0.jpg'
        assert samples[child]['metadata']['parent_id'] == samples[corn]['id']  # the parent's id here, not there
        vectors = {
            row['sample_id']: np.array([float(v) for v in row['vector'].strip('[]').split(',')])
            for row in await database.fetch(
                "SELECT sample_id, vector::text FROM embeddings "
                "WHERE field_name = 'clip' AND sample_id = ANY($1::int[])",
                [samples[corn]['id'], samples[soy]['id']],
            )
        }
        assert vectors[samples[corn]['id']].tolist() == [0.5, 0.25]
        assert (
            await database.fetchval(
                'SELECT hash_hex FROM sample_hashes WHERE sample_id = $1',
                samples[corn]['id'],
            )
            == 'ff00'
        )
        assert {
            row['field_name']
            for row in await database.fetch(
                'SELECT field_name FROM dataset_fields WHERE dataset_id = $1',
                mirror['dataset_id'],
            )
        } >= {'crop', 'label', 'parent_id'}
        views = await database.fetch('SELECT * FROM views WHERE dataset_id = $1', mirror['dataset_id'])
        assert [view['name'] for view in views] == ['Corn']  # shared only, and the concept view stays there
        assert views[0]['content']['filters']['filters'][0]['value'] == [samples[corn]['id']]
        assert local['default_view_id'] == views[0]['id']
        assert mirror['concept_views'] == ['Weedy'] and mirror['vector_fields'] == ['clip']

        # Changes on the primary come through the feed: a new sample, a deleted one, a vector and a rename.
        await ingest([{'crop': 'wheat'}], dataset_id=1, prefix='s3://images/b')
        wheat = await database.fetchval("SELECT id FROM samples WHERE metadata->>'crop' = 'wheat' AND dataset_id = 1")
        await database.execute('DELETE FROM samples WHERE id = $1', soy)
        await database.execute("UPDATE embeddings SET vector = '[0,1]' WHERE sample_id = $1", corn)
        await database.execute("UPDATE views SET name = 'Corn only' WHERE id = $1", corn_view)
        await database.execute("UPDATE datasets SET description = 'Field trial' WHERE id = 1")
        await sync(database, link_id)
        samples = await copied(database, await mirror_of(database))
        assert sorted(samples) == [corn, child, wheat]
        assert (
            await database.fetchval(
                "SELECT vector::text FROM embeddings WHERE sample_id = $1 AND field_name = 'clip'",
                samples[corn]['id'],
            )
            == '[0,1]'
        )
        assert (
            await database.fetchval(
                'SELECT name FROM views WHERE dataset_id = $1',
                mirror['dataset_id'],
            )
            == 'Corn only'
        )
        assert await database.fetchval('SELECT description FROM datasets WHERE id = $1', mirror['dataset_id']) == (
            'Field trial'
        )
        # The next sync acknowledges what was applied, so the primary prunes its log.
        await sync(database, link_id)
        held = (await followers.list_followers(user=USER))[0]['datasets'][0]['held_rows']
        assert held == 0

    registry_database(scenario)


def test_a_mirror_stops_when_the_primary_removes_the_follower(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        # No chains: the mirror can't be followed in turn.
        with pytest.raises(HTTPException) as chained:
            await followers.follow_dataset(
                follower,
                (await mirror_of(database))['dataset_id'],
                followers.FollowOptions(),
                user=USER,
            )
        assert chained.value.status_code == 409 and "A mirror can't be followed" in chained.value.detail
        await followers.remove_follower(follower, user=USER)
        result = await sync(database, link_id)
        mirror = await mirror_of(database)
        assert mirror['state'] == 'stopped' and mirror['last_error'].startswith('No longer following')
        assert 'No longer following' in result.message
        # The copy stays, read-only.
        assert len(await copied(database, mirror)) == 1

    registry_database(scenario)


def test_admins_add_a_primary_see_its_mirrors_and_remove_it(monkeypatch, registry_database):
    async def scenario(database):
        created, _ = await setup(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        await database.execute('DELETE FROM primary_links')

        # The token is checked against the primary before anything is saved.
        with pytest.raises(HTTPException) as refused:
            await primaries.add_primary(primaries.PrimaryCreate(url='http://primary/api', token='wrong'), user=USER)
        assert refused.value.status_code == 400 and 'refused that token' in refused.value.detail
        assert await database.fetchval('SELECT count(*) FROM primary_links') == 0
        token = created['token']
        added = await primaries.add_primary(primaries.PrimaryCreate(url='http://primary/api/', token=token), user=USER)
        assert (added['url'], added['follower_name']) == ('http://primary/api', 'lab')
        assert await database.fetchval('SELECT token_encrypted FROM primary_links') != token
        with pytest.raises(HTTPException) as again:
            await primaries.add_primary(primaries.PrimaryCreate(url='http://primary/api', token=token), user=USER)
        assert again.value.status_code == 409

        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(views='both'), user=USER)
        await sync(database, added['id'])
        listed = (await primaries.list_primaries(user=USER))['primaries'][0]
        mirror = listed['mirrors'][0]
        assert (mirror['state'], mirror['samples'], mirror['views'], mirror['copy_left']) == ('following', 1, 'both', 0)
        assert listed['last_synced_at'] is not None and listed['last_error'] is None
        select = f'SELECT {datasets._DATASET_SELECT} FROM datasets WHERE id = $1'
        row = await database.fetchrow(select, mirror['dataset_id'])
        assert row['mirror']['state'] == 'following' and row['mirror']['primary_url'] == 'http://primary/api'
        assert (await database.fetchrow(select, 2))['mirror'] is None

        # A mirror that's still following can't be deleted here; once its primary is removed, it stops.
        with pytest.raises(HTTPException) as following:
            await datasets.delete_dataset(mirror['dataset_id'], user=USER)
        assert following.value.status_code == 409
        await primaries.remove_primary(added['id'], user=USER)
        unlinked = (await primaries.list_primaries(user=USER))['unlinked_mirrors']
        assert [(entry['dataset_id'], entry['state']) for entry in unlinked] == [(mirror['dataset_id'], 'stopped')]
        assert unlinked[0]['last_error'] == 'No longer following: the primary was removed on this instance'

    registry_database(scenario)


async def coordinates_of(database, dataset_id):
    rows = await database.fetch(
        """SELECT m.origin_id, c.x, c.y FROM embedding_coordinates c
           JOIN mirror_samples m ON m.sample_id = c.sample_id
           WHERE m.dataset_id = $1 AND c.field_name = 'clip' ORDER BY m.origin_id""",
        dataset_id,
    )
    return {row['origin_id']: (row['x'], row['y']) for row in rows}


async def rank_stale(database, dataset_id):
    return await database.fetchval(
        "SELECT rank_stale FROM embedding_viz_meta WHERE dataset_id = $1 AND field_name = 'clip'",
        dataset_id,
    )


def test_a_mirror_copies_the_primary_maps_and_re_ranks_when_the_primary_does(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}, {'crop': 'oat'}], dataset_id=1, prefix='s3://images/a')
        ids = [row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = 1 ORDER BY id')]
        for index, sample_id in enumerate(ids):
            await database.execute(
                "INSERT INTO embeddings (sample_id, field_name, vector) VALUES ($1, 'clip', $2::vector)",
                sample_id,
                f'[{index},1]',
            )
            await database.execute(
                "INSERT INTO embedding_coordinates (sample_id, field_name, x, y) VALUES ($1, 'clip', $2, $3)",
                sample_id,
                float(index),
                float(-index),
            )
        await database.execute("SELECT recompute_viz_rank(1, 'clip', TRUE)")
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)

        await sync(database, link_id)
        mirror = (await mirror_of(database))['dataset_id']
        assert await coordinates_of(database, mirror) == {sample_id: (i, -i) for i, sample_id in enumerate(ids)}
        # Ranked here as the primary is, and nothing waits for placement on the mirror: the primary places.
        assert await rank_stale(database, mirror) is False
        assert await database.fetchval('SELECT count(*) FROM embedding_viz_rank WHERE dataset_id = $1', mirror) == 3
        assert (
            await database.fetchval('SELECT count(*) FROM embedding_projection_pending WHERE dataset_id = $1', mirror)
            == 0
        )

        # A coordinate moved there comes through on its own; the ranking goes stale on both, and isn't redone here
        # until the primary re-ranks.
        await database.execute(
            "UPDATE embedding_coordinates SET x = 9 WHERE sample_id = $1 AND field_name = 'clip'",
            ids[0],
        )
        await sync(database, link_id)
        assert (await coordinates_of(database, mirror))[ids[0]] == (9.0, 0.0)
        assert await rank_stale(database, 1) is True and await rank_stale(database, mirror) is True
        await database.execute("SELECT recompute_viz_rank(1, 'clip', TRUE)")
        await sync(database, link_id)
        assert await rank_stale(database, mirror) is False

        # A new sample's vector arrives before the primary places it; then its coordinates follow.
        await ingest([{'crop': 'rye'}], dataset_id=1, prefix='s3://images/b')
        rye = await database.fetchval("SELECT id FROM samples WHERE metadata->>'crop' = 'rye'")
        await database.execute(
            "INSERT INTO embeddings (sample_id, field_name, vector) VALUES ($1, 'clip', '[5,1]')",
            rye,
        )
        await sync(database, link_id)
        assert rye not in await coordinates_of(database, mirror)
        assert (
            await database.fetchval('SELECT count(*) FROM embedding_projection_pending WHERE dataset_id = $1', mirror)
            == 0
        )
        await database.execute(
            "INSERT INTO embedding_coordinates (sample_id, field_name, x, y) VALUES ($1, 'clip', 5, 5)",
            rye,
        )
        await sync(database, link_id)
        assert (await coordinates_of(database, mirror))[rye] == (5.0, 5.0)

        # The mirror's copy of the map is read-only outside the sync.
        local_rye = await database.fetchval('SELECT sample_id FROM mirror_samples WHERE origin_id = $1', rye)
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError) as refused:
            await database.execute(
                "UPDATE embedding_coordinates SET x = 0 WHERE sample_id = $1 AND field_name = 'clip'",
                local_rye,
            )
        assert 'This map came from another Freedom Ridge instance' in str(refused.value)
        # And it isn't re-fitted here: a re-fit schedule for it is refused when it's made.
        monkeypatch.setattr(schedules, 'db', database)
        with pytest.raises(HTTPException) as scheduled:
            await schedules.create_schedule(mirror, schedules.ScheduleCreate(target='clip'), user=USER)
        assert scheduled.value.status_code == 409 and "comes from this mirror's primary" in scheduled.value.detail

    registry_database(scenario)


def test_tags_and_shared_views_made_on_a_mirror_go_to_the_primary_when_they_sync_both_ways(
    monkeypatch,
    registry_database,
):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        monkeypatch.setattr(views, 'db', database)
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/a')
        corn, soy = [
            row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = 1 ORDER BY id')
        ]
        await database.execute(
            "INSERT INTO views (dataset_id, user_id, name, slug, content, is_public) "
            "VALUES (1, 1, 'Made there', 'made-there', '{}', TRUE)",
        )
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(tags='both', views='both'), user=USER)
        await sync(database, link_id)
        mirror = (await mirror_of(database))['dataset_id']
        local = {row['origin_id']: row['id'] for row in (await copied(database, {'dataset_id': mirror})).values()}

        # Tags: sent there as the follower's account, and back through the feed.
        result = await datasets.bulk_tag_samples(
            mirror,
            datasets.BulkTagRequest(sample_ids=[local[corn]], add_tags=['weedy']),
            user=USER,
        )
        assert (result['updated_count'], result['forwarded_to']) == (1, 'primary')
        assert await database.fetchval('SELECT tags FROM samples WHERE id = $1', corn) == ['weedy']
        # Made there as the follower's own account.
        account = await database.fetchval('SELECT user_id FROM followers')
        assert await database.fetchval('SELECT modified_by_user_id FROM samples WHERE id = $1', corn) == account
        assert await database.fetchval("SELECT count(*) FROM jobs WHERE kind = 'follower_sync'") == 1  # queued at once
        await sync(database, link_id)
        assert await database.fetchval('SELECT tags FROM samples WHERE id = $1', local[corn]) == ['weedy']

        # A shared view: made there with its sample ids, marked as made here, and copied here at once.
        content = {'filters': {'field': 'id', 'operator': 'in', 'value': [local[soy]]}}
        view = await views.create_view(mirror, ViewCreate(name='Soy', content=content, is_public=True), user=USER)
        assert view['from_primary'] is True and view['content']['filters']['value'] == [local[soy]]
        there = await database.fetchrow("SELECT * FROM views WHERE dataset_id = 1 AND name = 'Soy'")
        assert there['content']['filters']['value'] == [soy] and there['content']['made_on'] == 'lab'
        assert there['is_public'] and there['user_id'] == await database.fetchval('SELECT user_id FROM followers')
        await sync(database, link_id)
        names = sorted(
            row['name'] for row in await database.fetch('SELECT name FROM views WHERE dataset_id = $1', mirror)
        )
        assert names == ['Made there', 'Soy']  # the sync found the copy already there
        # A private view stays here, until it's shared with everyone: then it's made there, and its copy replaces it.
        mine = await views.create_view(mirror, ViewCreate(name='Mine', content={}), user=USER)
        assert not await database.fetchval("SELECT 1 FROM views WHERE dataset_id = 1 AND name = 'Mine'")
        shared = await views.share_view(
            mirror,
            mine['id'],
            views.ViewShare(is_public=True, shared_with_user_ids=[]),
            user=USER,
        )
        assert shared['from_primary'] is True and shared['id'] != mine['id']
        assert (
            await database.fetchval("SELECT content->>'made_on' FROM views WHERE dataset_id = 1 AND name = 'Mine'")
            == 'lab'
        )
        assert not await database.fetchval('SELECT 1 FROM views WHERE id = $1', mine['id'])

        # Deleting: the follower deletes there only the views it made, and here only their owner or an admin may.
        made_there = await database.fetchval(
            "SELECT id FROM views WHERE dataset_id = $1 AND name = 'Made there'",
            mirror,
        )
        other = await database.fetchval(
            "INSERT INTO users (username, role, password_hash) VALUES ('viewer', 'power_user', 'x') RETURNING id",
        )
        with pytest.raises(HTTPException) as not_theirs:
            await views.delete_view(view['id'], user={'id': other, 'role': 'power_user'})
        assert not_theirs.value.status_code == 403
        assert await database.fetchval("SELECT 1 FROM views WHERE dataset_id = 1 AND name = 'Soy'")  # still there
        with pytest.raises(HTTPException) as refused:
            await views.delete_view(made_there, user=USER)
        assert refused.value.status_code == 403 and 'Not authorized' in refused.value.detail
        await views.delete_view(view['id'], user=USER)
        assert not await database.fetchval("SELECT 1 FROM views WHERE name = 'Soy'")

        # Other bulk edits of a mirror are refused before they're queued.
        with pytest.raises(HTTPException) as bulk:
            await datasets._bulk_response(mirror, USER, 'set', {})
        assert bulk.value.status_code == 409 and 'read-only mirror' in bulk.value.detail

    registry_database(scenario)


def test_a_mirror_refuses_tags_and_shared_views_that_come_from_the_primary_only(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        monkeypatch.setattr(views, 'db', database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        mirror = (await mirror_of(database))['dataset_id']
        sample = await database.fetchval('SELECT sample_id FROM mirror_samples')
        with pytest.raises(HTTPException) as tags:
            await datasets.bulk_tag_samples(
                mirror,
                datasets.BulkTagRequest(sample_ids=[sample], add_tags=['x']),
                user=USER,
            )
        assert tags.value.status_code == 409 and "tags come from primary" in tags.value.detail
        # With views from the primary only, a shared view made here stays here.
        await views.create_view(mirror, ViewCreate(name='Local', content={}, is_public=True), user=USER)
        assert await database.fetchval("SELECT dataset_id FROM views WHERE name = 'Local'") == mirror

    registry_database(scenario)


def test_the_sync_survives_a_filepath_moved_between_blocks_and_views_that_swap_names(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        monkeypatch.setattr(follower_sync, 'COPY_BLOCKS', 1)  # each block in a step of its own
        await database.execute(
            "INSERT INTO samples (id, dataset_id, filepath, media_type) VALUES (9000, 1, 's3://i/x.jpg', 'image')",
        )
        for name in ('a', 'b'):
            await database.execute(
                "INSERT INTO views (dataset_id, name, slug, content, is_public) VALUES (1, $1, $1, '{}', TRUE)",
                name,
            )
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        mirror = (await mirror_of(database))['dataset_id']

        # There, a restore brings the path back as a lower id, in an earlier block; and the views swap names.
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute('DELETE FROM samples WHERE id = 9000')
            await connection.execute(
                "INSERT INTO samples (id, dataset_id, filepath, media_type) VALUES (100, 1, 's3://i/x.jpg', 'image')",
            )
            await connection.execute("UPDATE views SET name = 'swap' WHERE dataset_id = 1 AND name = 'a'")
            await connection.execute("UPDATE views SET name = 'a' WHERE dataset_id = 1 AND name = 'b'")
            await connection.execute("UPDATE views SET name = 'b' WHERE dataset_id = 1 AND name = 'swap'")
        await sync(database, link_id)
        samples = await copied(database, {'dataset_id': mirror})
        assert [(origin, row['filepath']) for origin, row in samples.items()] == [(100, 's3://i/x.jpg')]
        names = await database.fetch(
            """SELECT m.origin_view_id, v.name FROM mirror_views m JOIN views v ON v.id = m.view_id
               WHERE m.dataset_id = $1 ORDER BY 1""",
            mirror,
        )
        there = await database.fetch('SELECT id, name FROM views WHERE dataset_id = 1 ORDER BY id')
        assert [(row['origin_view_id'], row['name']) for row in names] == [(row['id'], row['name']) for row in there]
        assert [row['name'] for row in there] == ['b', 'a']

    registry_database(scenario)
