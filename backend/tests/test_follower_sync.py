"""The follower's sync (follower_sync.py) against a real primary: one test database plays both parts. Dataset 1 is
the primary's; the sync copies it into a mirror beside it through the real follower endpoints (an in-process HTTP
client bound to the app), then applies the primary's feed to it."""

import asyncio
import io
import os
import uuid
from typing import cast
from unittest.mock import AsyncMock, Mock

import asyncpg
import httpx
import numpy as np
import pytest
from fastapi import HTTPException, Request

from backend import follower_sync, sample_ids
from backend.crypto import decrypt_secret, encrypt_secret
from backend.models import ViewCreate
from backend.routers import auth, datasets, followers, primaries, schedules, views
from backend.tests import test_bulk_jobs
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

    def connect(link, instance=None):
        headers = {'Authorization': f"Bearer {decrypt_secret(link['token_encrypted'])}"}
        if instance:
            headers['X-Fridge-Instance'] = instance
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=link['url'], headers=headers)

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


async def follower_headers(created) -> dict:
    """What a follower's instance sends its primary: its token and its installation id (the token is bound to it)."""
    return {
        'Authorization': f"Bearer {created['token']}",
        'X-Fridge-Instance': await follower_sync.installation_id(vars(followers)['db']),  # the test's database
    }


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
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[0.5,0.25]'), "
            "((SELECT dataset_id FROM samples WHERE id = $2), $2, 'clip', '[1,0]')",
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
                "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
                "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', $2::vector)",
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
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[5,1]')",
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


def test_a_mirror_shows_the_images_its_primary_serves_when_their_option_is_on(monkeypatch, registry_database, tmp_path):
    from PIL import Image

    from backend import main

    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        # The primary's image is a file on its disk; the follower (the same process here) is made unable to read it.
        Image.new('RGB', (40, 30), (200, 40, 40)).save(tmp_path / 'a-0.jpg')
        monkeypatch.setattr(main, 'db', database)
        monkeypatch.setattr(main, '_resolve_local_path', lambda path: str(tmp_path / os.path.basename(path)))
        monkeypatch.setattr(main, 'CROP_CACHE_PATH', str(tmp_path / 'crop'))
        monkeypatch.setattr(main, 'S3_LOCAL_PATH', str(tmp_path / 'cache'))
        monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value=USER))
        monkeypatch.setattr(main, 'check_dataset_access', AsyncMock())
        monkeypatch.setattr(main, '_readable_here', AsyncMock(return_value=False))
        monkeypatch.setattr(main, '_queue_media_cache', main._write_media_cache)  # written at once
        requests = []
        connect = follower_sync.connect

        def counted(link, instance=None):
            requests.append(link)
            return connect(link, instance)

        monkeypatch.setattr(follower_sync, 'connect', counted)
        main._primary_clients.clear()

        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='/fr-images/a')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        mirror = await mirror_of(database)
        [sample] = (await copied(database, mirror)).values()
        assert mirror['images'] is False

        # Images off: nothing is asked of the primary (here the file is still found on this machine's own disk).
        requests.clear()  # the sync's own connections
        await main.serve_sample_media(sample['id'], AsyncMock(), w=32)
        assert requests == []
        sub = {'id': follower}
        with pytest.raises(HTTPException) as off:
            await followers.follower_media(1, sample['origin_id'], AsyncMock(), w=32, follower=sub)
        assert off.value.status_code == 403 and 'images option is off' in off.value.detail

        # On: the sync takes the option, and the mirror's thumbnail comes from the primary, then from the cache.
        await followers.follow_dataset(follower, 1, followers.FollowOptions(images=True), user=USER)
        await sync(database, link_id)
        assert (await mirror_of(database))['images'] is True
        requests.clear()
        first = await main.serve_sample_media(sample['id'], AsyncMock(), w=32)
        assert first.media_type == 'image/jpeg' and 'X-Cache' not in first.headers and len(requests) == 1
        red = np.asarray(Image.open(io.BytesIO(first.body)).convert('RGB')).mean(axis=(0, 1))
        assert red[0] > 150 and red[1] < 90  # the primary's red image
        again = await main.serve_sample_media(sample['id'], AsyncMock(), w=32)
        assert again.headers['X-Cache'] == 'HIT' and again.body == first.body and len(requests) == 1

        # The primary serves only the samples of a dataset the follower follows.
        await ingest([{'crop': 'beet'}], dataset_id=2, prefix='/fr-images/b')
        other = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 2 LIMIT 1')
        with pytest.raises(HTTPException) as elsewhere:
            await followers.follower_media(1, other, AsyncMock(), follower=sub)
        assert elsewhere.value.status_code == 404

    registry_database(scenario)


def test_a_restore_is_noticed_and_its_subscriptions_start_again_in_the_restored_database(
    monkeypatch,
    registry_database,
):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        sub = dict(await database.fetchrow('SELECT * FROM followers WHERE id = $1', follower))

        # The identity is the cluster's and the database's: dropdb + createdb changes the second.
        system_id, oid = (await followers.identity(database)).split(':')
        assert system_id == str(await database.fetchval('SELECT system_identifier FROM pg_control_system()'))
        assert oid == str(await database.fetchval('SELECT oid FROM pg_database WHERE datname = current_database()'))
        cursor = (await mirror_of(database))['feed_cursor']
        with pytest.raises(HTTPException) as restored:
            await followers.follower_changes(1, cursor, f'{system_id}:1', 1000, follower=sub)
        assert restored.value.status_code == 410

        # Restored from another cluster: the subscription's txids are another database's. It starts again here.
        await database.execute(
            "UPDATE follower_datasets SET start_txid = start_txid + 10000000, acked_txid = start_txid + 10000000, "
            "start_identity = '1:1'",
        )
        [entry] = (await followers.follower_config(follower=sub))['datasets']
        row = await database.fetchrow('SELECT * FROM follower_datasets')
        assert entry['ready'] and row['acked_txid'] is None
        assert row['start_identity'] == await followers.identity(database)
        assert row['start_txid'] <= await database.fetchval('SELECT txid_snapshot_xmax(txid_current_snapshot())')

        # A subscription whose start was never set (its second statement failed) gets one too.
        await database.execute('UPDATE follower_datasets SET start_txid = NULL, start_identity = NULL')
        [entry] = (await followers.follower_config(follower=sub))['datasets']
        assert entry['ready'] and entry['start_txid'] is not None

    registry_database(scenario)


def test_an_older_primary_s_identity_is_the_same_primary(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        mirror = await mirror_of(database)
        # Linked before the identity named the database: the bare system identifier.
        system_id = await database.fetchval('SELECT system_identifier::text FROM pg_control_system()')
        await database.execute('UPDATE primary_links SET identity = $1', system_id)
        await sync(database, link_id)
        assert (await mirror_of(database))['dataset_id'] == mirror['dataset_id']  # no new copy
        assert (await mirror_of(database))['state'] == 'following'
        assert await database.fetchval('SELECT identity FROM primary_links') == await followers.identity(database)

    registry_database(scenario)


def test_a_follower_before_2_6_1_keeps_following_an_upgraded_primary(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        sub = dict(await database.fetchrow('SELECT * FROM followers WHERE id = $1', follower))
        system_id, _ = (await followers.identity(database)).split(':')

        # An older follower compares `identity` with the bare identifier it stored, and copies again on any change.
        config = await followers.follower_config(follower=sub)
        assert config['identity'] == system_id
        assert config['database_identity'] == await followers.identity(database)

        # Its cursor carries the bare identifier, also mid-sync across the primary's upgrade: still this primary.
        cursor = (await mirror_of(database))['feed_cursor']
        await followers.follower_changes(1, after=cursor, identity_=system_id, transactions=10, follower=sub)
        with pytest.raises(HTTPException) as other:
            await followers.follower_changes(1, after=cursor, identity_='1', transactions=10, follower=sub)
        assert other.value.status_code == 410

    registry_database(scenario)


def test_one_failing_mirror_does_not_hold_up_the_other_mirrors_of_its_primary(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        await ingest([{'crop': 'beet'}], dataset_id=2, prefix='s3://images/b')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await followers.follow_dataset(follower, 2, followers.FollowOptions(), user=USER)
        original = follower_sync.write_vectors

        async def clashing(connection, dataset_id, named, vectors):
            # The mirror of dataset 1 fails in its writes, as a vector field clash would, after writing something.
            if (
                await connection.fetchval('SELECT origin_dataset_id FROM mirrors WHERE dataset_id = $1', dataset_id)
                == 1
            ):
                await connection.execute("UPDATE mirrors SET copy_total = -1 WHERE dataset_id = $1", dataset_id)
                raise follower_sync.MirrorFieldClash("The primary's vector field 'clip' has 2 dimensions...")
            await original(connection, dataset_id, named, vectors)

        monkeypatch.setattr(follower_sync, 'write_vectors', clashing)
        result = await sync(database, link_id)
        mirrors = {row['origin_dataset_id']: dict(row) for row in await database.fetch('SELECT * FROM mirrors')}
        assert mirrors[2]['state'] == 'following' and mirrors[2]['last_error'] is None
        assert mirrors[1]['state'] == 'copying' and 'MirrorFieldClash' in mirrors[1]['last_error']
        assert mirrors[1]['copy_total'] != -1  # its failed step's writes were undone
        assert result.result == {'failed': [mirrors[1]['dataset_id']], 'waiting': 0}
        assert '1 mirror(s) failed' in result.message

        # It waits a minute before its next try, and the next run passes it over.
        waits = 'SELECT failures, extract(epoch FROM retry_after - now()) AS wait FROM mirrors WHERE dataset_id = $1'
        row = await database.fetchrow(waits, mirrors[1]['dataset_id'])
        assert row['failures'] == 1 and 50 < row['wait'] <= 60
        result = await sync(database, link_id)
        assert result.result == {'failed': [], 'waiting': 1} and '1 wait(s) to try again' in result.message
        await database.execute("UPDATE mirrors SET retry_after = now() - interval '1 second'")

        # A failure before any write is set aside the same way, and waits twice as long.
        monkeypatch.setattr(follower_sync, 'write_vectors', original)
        copy_step = follower_sync.copy_step

        async def refused(pool, link, client, mirror, checkpoint):
            raise follower_sync.PrimaryRefused(400, 'Not that block')

        monkeypatch.setattr(follower_sync, 'copy_step', refused)
        result = await sync(database, link_id)
        assert 'Not that block' in await database.fetchval(
            'SELECT last_error FROM mirrors WHERE dataset_id = $1',
            mirrors[1]['dataset_id'],
        )
        row = await database.fetchrow(waits, mirrors[1]['dataset_id'])
        assert row['failures'] == 2 and 110 < row['wait'] <= 120
        await database.execute("UPDATE mirrors SET retry_after = now() - interval '1 second'")

        # Fixed: the next run copies it, its error goes, and its next failure waits a minute again.
        monkeypatch.setattr(follower_sync, 'copy_step', copy_step)
        result = await sync(database, link_id)
        row = await database.fetchrow('SELECT * FROM mirrors WHERE dataset_id = $1', mirrors[1]['dataset_id'])
        assert (row['state'], row['last_error'], row['failures'], row['retry_after']) == ('following', None, 0, None)
        assert result.result == {'failed': [], 'waiting': 0}

    registry_database(scenario)


def test_only_a_final_refusal_stops_the_mirrors(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        call = follower_sync.call

        async def proxy_forbids(client, method, path, **kwargs):
            if path == '/follower/config':
                raise follower_sync.PrimaryRefused(403, 'Forbidden by the proxy')  # no code: not the primary's word
            return await call(client, method, path, **kwargs)

        monkeypatch.setattr(follower_sync, 'call', proxy_forbids)
        with pytest.raises(follower_sync.PrimaryRefused):
            await sync(database, link_id)
        assert (await mirror_of(database))['state'] == 'following'
        assert 'Forbidden by the proxy' in await database.fetchval('SELECT last_error FROM primary_links')

        # The primary's own refusal says it's final.
        monkeypatch.setattr(follower_sync, 'call', call)
        await database.execute('DELETE FROM followers')  # removed there
        with pytest.raises(HTTPException) as removed:
            await followers.current_follower(
                Mock(headers={}),
                account={'id': await database.fetchval('SELECT id FROM users LIMIT 1')},
            )
        assert removed.value.status_code == 403 and getattr(removed.value, 'code', None) == 'follower_removed'

    registry_database(scenario)


def test_the_tags_option_covers_tags_only_and_a_concept_view_stays_where_it_was_made(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        monkeypatch.setattr(views, 'db', database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(tags='both', views='both'), user=USER)
        await sync(database, link_id)
        mirror = (await mirror_of(database))['dataset_id']
        [sample] = (await copied(database, {'dataset_id': mirror})).values()
        sub = dict(await database.fetchrow('SELECT * FROM followers WHERE id = $1', follower))

        # Another array field of the primary's: refused on both sides.
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='labels'), user=USER)
        await sync(database, link_id)
        with pytest.raises(ValueError):
            followers.TagRequest(sample_ids=[sample['origin_id']], field_name='labels', add_tags=['x'])
        with pytest.raises(HTTPException) as other:
            await datasets.bulk_tag_samples(
                mirror,
                datasets.BulkTagRequest(sample_ids=[sample['id']], field_name='labels', add_tags=['x']),
                user=USER,
            )
        assert other.value.status_code == 409

        # A view using a concept: refused by the primary, and kept on the mirror, shared here.
        concept = {'filters': CONCEPT_FILTER}
        with pytest.raises(HTTPException) as refused:
            await followers.follower_create_view(1, followers.ViewRequest(name='Weedy', content=concept), follower=sub)
        assert refused.value.status_code == 400
        view = await views.create_view(mirror, ViewCreate(name='Weedy', content=concept, is_public=True), user=USER)
        assert not view.get('from_primary') and view['is_public']
        assert not await database.fetchval("SELECT 1 FROM views WHERE dataset_id = 1 AND name = 'Weedy'")
        await sync(database, link_id)
        assert await database.fetchval("SELECT 1 FROM views WHERE dataset_id = $1 AND name = 'Weedy'", mirror)

    registry_database(scenario)


def test_shared_views_translate_parent_ids_and_a_range_on_ids_stays_where_it_was_made(
    monkeypatch,
    registry_database,
):
    import json

    def leaf(content):
        content = json.loads(content) if isinstance(content, str) else content
        return content['filters']['filters'][0]

    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        monkeypatch.setattr(views, 'db', database)
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/p')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(views='both'), user=USER)
        await sync(database, link_id)
        mirror = (await mirror_of(database))['dataset_id']
        local = {row['origin_id']: row['id'] for row in (await copied(database, {'dataset_id': mirror})).values()}
        image = min(local)
        sub = dict(await database.fetchrow('SELECT * FROM followers WHERE id = $1', follower))

        # The primary's view of one image's objects: its parent id becomes this instance's.
        objects = {
            'filters': {
                'logic': 'AND',
                'filters': [{'field': 'metadata.parent_id', 'operator': '=', 'value': image}],
            },
        }
        await views.create_view(1, ViewCreate(name='Objects', content=objects, is_public=True), user=USER)
        await sync(database, link_id)
        here = await database.fetchval("SELECT content FROM views WHERE dataset_id = $1 AND name = 'Objects'", mirror)
        assert leaf(here)['value'] == local[image]

        # A range on ids can't be translated: refused by the primary, kept on the mirror, the primary's not copied.
        ranged = {'filters': {'logic': 'AND', 'filters': [{'field': 'id', 'operator': '>', 'value': image}]}}
        with pytest.raises(HTTPException) as refused:
            await followers.follower_create_view(1, followers.ViewRequest(name='Later', content=ranged), follower=sub)
        assert refused.value.status_code == 400 and 'sample ids' in refused.value.detail
        view = await views.create_view(mirror, ViewCreate(name='Later', content=ranged, is_public=True), user=USER)
        assert not view.get('from_primary') and view['is_public']
        await views.create_view(1, ViewCreate(name='Range', content=ranged, is_public=True), user=USER)
        await sync(database, link_id)
        assert not await database.fetchval("SELECT 1 FROM views WHERE dataset_id = $1 AND name = 'Range'", mirror)
        assert (await mirror_of(database))['concept_views'] == ['Range']

    registry_database(scenario)


async def test_a_vector_field_clash_says_which_field_and_why(monkeypatch):
    class Connection:  # a vector index of this name at another size refuses the primary's vectors
        async def execute(self, sql, *args):
            if sql.lstrip().startswith('INSERT INTO embeddings'):
                raise asyncpg.exceptions.DataError('expected 768 dimensions, not 2')

        async def copy_to_table(self, *args, **kwargs):
            pass

        async def fetchval(self, sql, *args):
            return []  # no field of this instance's under that name

    monkeypatch.setattr(follower_sync, 'require_free_space', AsyncMock())
    monkeypatch.setattr(follower_sync, 'clear_placement', AsyncMock())
    with pytest.raises(follower_sync.MirrorFieldClash) as clash:
        await follower_sync.write_vectors(Connection(), 5, [(1, 'clip')], {'clip': [(1, [0.5, 0.25])]})
    assert "vector field 'clip' has 2 dimensions" in str(clash.value) and 'expected 768' in str(clash.value)


def test_a_primary_field_replaces_all_of_a_same_named_field_the_mirror_had(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/a')
        corn, soy = [r['id'] for r in await database.fetch('SELECT id FROM samples WHERE dataset_id = 1 ORDER BY id')]
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        mirror = (await mirror_of(database))['dataset_id']
        local = {row['origin_id']: row['id'] for row in (await copied(database, {'dataset_id': mirror})).values()}
        # This instance's own vectors and map, on both samples, under a name the primary doesn't have yet.
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
            "SELECT s.dataset_id, s.id, t.f, t.v::vector FROM samples s JOIN (VALUES ($1::int, 'dino', '[9,9]'), "
            "($2::int, 'dino', '[8,8]'), ($2::int, 'dino_patch_2x2_r0c0', '[7,7]')) AS t(id, f, v) ON t.id = s.id",
            local[corn],
            local[soy],
        )
        # A patch map is kept under its scale's name.
        await database.execute(
            "INSERT INTO embedding_viz_meta (dataset_id, field_name, total_points) "
            "VALUES ($1, 'dino', 2), ($1, 'dino_patch_2x2', 1)",
            mirror,
        )

        # The primary makes a field of that name, on one sample: all of the mirror's own goes, its map too.
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
            "SELECT dataset_id, id, f, v::vector FROM samples, "
            "(VALUES ('dino', '[1,0]'), ('dino_patch_2x2_r0c0', '[0,1]')) "
            "AS t(f, v) WHERE id = $1",
            corn,
        )
        await sync(database, link_id)
        rows = {
            row['sample_id']: row['vector']
            for row in await database.fetch(
                "SELECT sample_id, vector::text FROM embeddings "
                "WHERE field_name = 'dino' AND sample_id = ANY($1::int[])",
                [local[corn], local[soy]],
            )
        }
        assert rows == {local[corn]: '[1,0]'}  # soy's own vector didn't stay beside the primary's field
        assert 'dino' in (await mirror_of(database))['vector_fields']
        assert not await database.fetchval(
            "SELECT 1 FROM embedding_viz_meta WHERE dataset_id = $1 AND field_name IN ('dino', 'dino_patch_2x2')",
            mirror,
        )

    registry_database(scenario)


def test_a_mirror_s_own_vectors_can_be_deleted_and_the_primary_s_cannot(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        monkeypatch.setattr(datasets, 'db', database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        corn = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 1')
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[1,0]')",
            corn,
        )
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        mirror = (await mirror_of(database))['dataset_id']
        local = await database.fetchval('SELECT id FROM samples WHERE dataset_id = $1', mirror)
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'patches', '[0,1]')",
            local,
        )
        everything = datasets.BulkTargetRequest(filters={})
        submitted = await datasets.bulk_delete_embeddings(mirror, 'patches', everything, user=USER)
        assert submitted.status_code == 202  # this instance's own field
        assert (await test_bulk_jobs.finish(database))['result'] == {'deleted': 1}
        fields = 'SELECT array_agg(field_name ORDER BY field_name) FROM embeddings WHERE sample_id = $1'
        assert await database.fetchval(fields, local) == ['clip']  # the primary's field is untouched
        with pytest.raises(HTTPException) as theirs:
            await datasets.bulk_delete_embeddings(mirror, 'clip', everything, user=USER)
        assert theirs.value.status_code == 409 and 'comes from its primary' in theirs.value.detail

    registry_database(scenario)


def test_two_adds_of_one_primary_at_once_make_one_link(monkeypatch, registry_database):
    async def scenario(database):
        created, _ = await setup(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        await database.execute('DELETE FROM primary_links')
        # Both read the primary's config before either checks for a link to it: at two addresses, both passed.
        real_call, both_read = follower_sync.call, asyncio.Barrier(2)

        async def call(client, method, path, **kwargs):
            answer = await real_call(client, method, path, **kwargs)
            if path == '/follower/config':
                await both_read.wait()
            return answer

        monkeypatch.setattr(follower_sync, 'call', call)
        added = await asyncio.wait_for(
            asyncio.gather(
                *(
                    primaries.add_primary(primaries.PrimaryCreate(url=url, token=created['token']), user=USER)
                    for url in ('http://primary/api', 'http://primary.example.org/api')
                ),
                return_exceptions=True,
            ),
            20,
        )
        refused = [answer for answer in added if isinstance(answer, HTTPException)]
        assert len(refused) == 1 and refused[0].status_code == 409, added
        assert await database.fetchval('SELECT count(*) FROM primary_links') == 1

    registry_database(scenario)


def test_one_link_per_primary_and_one_instance_per_follower_token(monkeypatch, registry_database):
    async def scenario(database):
        created, link_id = await setup(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        await sync(database, link_id)  # records the primary's identity, as adding it does
        # The same primary under another address: refused, by its identity.
        with pytest.raises(HTTPException) as again:
            await primaries.add_primary(
                primaries.PrimaryCreate(url='http://primary-by-another-name.example/api', token=created['token']),
                user=USER,
            )
        assert again.value.status_code == 409 and 'already follows that primary' in again.value.detail

        # The primary bound the token to the first instance that used it: this one, by its installation id, which
        # a dump carries (a restored or moved follower is the same instance).
        this = await follower_sync.installation_id(database)
        assert await database.fetchval('SELECT instance FROM followers') == this
        account = {'id': await database.fetchval('SELECT user_id FROM followers')}
        await followers.current_follower(Mock(headers={'X-Fridge-Instance': this}), account=account)
        # Bound, so this follower sends its installation id: a call without one is from somewhere else.
        with pytest.raises(HTTPException) as headerless:
            await followers.current_follower(Mock(headers={}), account=account)
        assert headerless.value.status_code == 409
        assert getattr(headerless.value, 'code', None) == 'follower_instance_mismatch'
        reinstalled = str(uuid.uuid4())
        with pytest.raises(HTTPException) as other:
            await followers.current_follower(Mock(headers={'X-Fridge-Instance': reinstalled}), account=account)
        assert other.value.status_code == 409 and getattr(other.value, 'code', None) == 'follower_instance_mismatch'
        assert await database.fetchval('SELECT instance FROM followers') == this

        # Reinstalled from scratch: an admin there resets it, and the next installation that calls takes it.
        monkeypatch.setattr(followers, 'db', database)
        [listed] = await followers.list_followers(user=USER)
        assert listed['bound']
        await followers.reset_follower_instance(listed['id'], user=USER)
        await followers.current_follower(Mock(headers={'X-Fridge-Instance': reinstalled}), account=account)
        assert await database.fetchval('SELECT instance FROM followers') == reinstalled

    registry_database(scenario)


def test_the_feed_fetches_vectors_only_for_new_samples_and_keeps_the_id_sequence(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        corn = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 1')
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[1,0]')",
            corn,
        )
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        asked = []
        fetch_vectors = follower_sync.fetch_vectors

        async def recorded(client, origin, origin_ids, fields=None):
            asked.append(sorted(origin_ids))
            return await fetch_vectors(client, origin, origin_ids, fields)

        monkeypatch.setattr(follower_sync, 'fetch_vectors', recorded)
        # A tag changes corn's block, and a new sample arrives in it with a vector: only the new one's are fetched.
        await database.execute("UPDATE samples SET tags = '{reviewed}' WHERE id = $1", corn)
        await ingest([{'crop': 'soy'}], dataset_id=1, prefix='s3://images/b')
        soy = await database.fetchval("SELECT id FROM samples WHERE metadata->>'crop' = 'soy'")
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[0,1]')",
            soy,
        )
        await sync(database, link_id)
        assert corn not in [origin_id for request in asked for origin_id in request]
        mirror = await mirror_of(database)
        local = {row['origin_id']: row['id'] for row in (await copied(database, mirror)).values()}
        vectors = dict(
            await database.fetch(
                "SELECT sample_id, vector::text FROM embeddings "
                "WHERE field_name = 'clip' AND sample_id = ANY($1::int[])",
                [local[corn], local[soy]],
            ),
        )
        assert vectors == {local[corn]: '[1,0]', local[soy]: '[0,1]'}  # corn's kept, soy's came

        # A changed vector updates the mirror's row without taking a sequence value.
        before = await database.fetchval('SELECT last_value FROM embeddings_id_seq')
        await database.execute("UPDATE embeddings SET vector = '[0.5,0.5]' WHERE sample_id = $1", corn)
        await sync(database, link_id)
        assert await database.fetchval('SELECT vector::text FROM embeddings WHERE sample_id = $1', local[corn]) == (
            '[0.5,0.5]'
        )
        assert await database.fetchval('SELECT last_value FROM embeddings_id_seq') == before

    registry_database(scenario)


def test_a_follower_can_ask_for_feed_pages_bounded_by_vector_changes(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'a'}, {'crop': 'b'}, {'crop': 'c'}], dataset_id=1, prefix='s3://images/a')
        ids = [row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = 1 ORDER BY id')]
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        cursor = (await mirror_of(database))['feed_cursor']
        for sample_id in ids:  # three transactions, one vector change each
            await database.execute(
                "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
                "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[1,1]')",
                sample_id,
            )
        sub = dict(await database.fetchrow('SELECT * FROM followers WHERE id = $1', follower))
        ident = await followers.identity(database)
        page = await followers.follower_changes(1, cursor, ident, 1000, vector_limit=1, follower=sub)
        assert page['more'] and [v['sample_id'] for v in page['vectors']] == ids[:1]
        rest = await followers.follower_changes(1, page['cursor'], ident, 1000, vector_limit=2, follower=sub)
        assert not rest['more'] and [v['sample_id'] for v in rest['vectors']] == ids[1:]
        whole = await followers.follower_changes(1, cursor, ident, 1000, follower=sub)  # an older follower
        assert not whole['more'] and len(whole['vectors']) == 3

    registry_database(scenario)


def test_a_follower_reports_the_patch_scales_it_holds_itself(monkeypatch, registry_database):
    async def scenario(database):
        from backend import patch_search
        from backend.routers import embeddings as embeddings_router

        _, link_id = await setup(monkeypatch, database)
        monkeypatch.setattr(embeddings_router, 'db', database)
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/a')
        corn = await database.fetchval("SELECT id FROM samples WHERE dataset_id = 1 AND metadata->>'crop' = 'corn'")
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[1,0]')",
            corn,
        )
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        mirror = await mirror_of(database)
        assert mirror['state'] == 'following' and mirror['vector_fields'] == ['clip']
        # Nothing of its own yet: the report is empty, and the primary keeps nothing for it.
        assert await database.fetchval('SELECT count(*) FROM follower_fields') == 0
        assert await database.fetchval('SELECT fields_reported_at FROM mirrors') is not None

        # Patch vectors computed here, on the mirror's samples: a 2x2 scale only this instance has.
        local = [
            row['id']
            for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = $1', mirror['dataset_id'])
        ]
        for sample_id in local:
            for tile in ('r0c0', 'r0c1', 'r1c0', 'r1c1'):
                await database.execute(
                    "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
                    "(SELECT dataset_id FROM samples WHERE id = $1), $1, $2, '[0.5,0.5,0.5]')",
                    sample_id,
                    f'clip_patch_2x2_{tile}',
                )
        # Reported again once its last report is old enough.
        await database.execute("UPDATE mirrors SET fields_reported_at = now() - interval '1 hour'")
        await sync(database, link_id)
        rows = await database.fetch('SELECT base_field, scale, dims, indexed FROM follower_fields')
        assert [dict(row) for row in rows] == [{'base_field': 'clip', 'scale': '2x2', 'dims': 3, 'indexed': False}]

        listed = await followers.list_followers(user=USER)
        assert [(own['base_field'], own['scale']) for own in listed[0]['datasets'][0]['own_scales']] == [
            ('clip', '2x2'),
        ]
        async with database.pool.acquire() as connection:
            scales = await patch_search.patch_scales(connection, 1, 'clip')
        assert scales['2x2']['local'] is False
        assert [(p['name'], p['online'], p['dims']) for p in scales['2x2']['providers']] == [('lab', True, 3)]
        # Offered with the follower that holds it: a patch search on it goes there.
        offered = await embeddings_router.get_patch_scales(1, 'clip')
        assert [(s['scale'], s['local'], s['provider'], s['online']) for s in offered] == [('2x2', False, 'lab', True)]

        # Stopping the follow drops what it reported.
        await followers.unfollow_dataset(follower, 1, user=USER)
        assert await database.fetchval('SELECT count(*) FROM follower_fields') == 0

    registry_database(scenario)


def test_a_report_is_checked_and_only_for_a_followed_dataset(monkeypatch, registry_database):
    async def scenario(database):
        created, _ = await setup(monkeypatch, database)
        from backend.main import app

        with pytest.raises(ValueError):
            followers.ReportedScale(base_field='clip', scale='0x2', dims=3, indexed=False)
        with pytest.raises(ValueError):
            followers.ReportedScale(base_field='clip; DROP', scale='2x2', dims=3, indexed=False)
        headers = await follower_headers(created)
        body = {'scales': [{'base_field': 'clip', 'scale': '2x2', 'dims': 3, 'indexed': True}]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://primary/api') as client:
            refused = await client.post('/follower/datasets/1/fields', json=body, headers=headers)
            assert refused.status_code == 404  # not followed
            config = await client.get('/follower/config', headers=headers)
            assert config.json()['protocol'] == followers.PROTOCOL
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://primary/api') as client:
            twice = {'scales': body['scales'] * 2}  # the same scale twice counts once
            accepted = await client.post('/follower/datasets/1/fields', json=twice, headers=headers)
        assert accepted.status_code == 200 and accepted.json() == {'scales': 1}
        assert await database.fetchval('SELECT indexed FROM follower_fields') is True
        first = await database.fetchval('SELECT first_reported_at FROM follower_fields')
        # Reported again (no index now) and with another scale: the first keeps its place among providers.
        again = {'scales': [{**body['scales'][0], 'indexed': False}, {**body['scales'][0], 'scale': '4x4'}]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://primary/api') as client:
            assert (await client.post('/follower/datasets/1/fields', json=again, headers=headers)).status_code == 200
        rows = {r['scale']: r for r in await database.fetch('SELECT * FROM follower_fields')}
        assert rows['2x2']['first_reported_at'] == first and rows['2x2']['indexed'] is False
        assert rows['2x2']['reported_at'] > first and set(rows) == {'2x2', '4x4'}
        # A scale no longer reported goes.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://primary/api') as client:
            assert (await client.post('/follower/datasets/1/fields', json=body, headers=headers)).status_code == 200
        assert [r['scale'] for r in await database.fetch('SELECT scale FROM follower_fields')] == ['2x2']

    registry_database(scenario)


async def mirror_with_own_patches(monkeypatch, database):
    """Dataset 1 on the primary with three images, a mirror of it, and 2x2 patch vectors computed on the mirror only,
    reported to the primary."""
    from backend import follower_search

    created, link_id = await setup(monkeypatch, database)
    monkeypatch.setattr(follower_search, 'db', database)
    await ingest([{'crop': 'corn'}, {'crop': 'soy'}, {'crop': 'oat'}], dataset_id=1, prefix='s3://images/p')
    follower = await database.fetchval('SELECT id FROM followers')
    await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
    await sync(database, link_id)
    mirror = await mirror_of(database)
    local = [
        r['id']
        for r in await database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id', mirror['dataset_id'])
    ]
    for index, sample_id in enumerate(local):
        for tile, (row, col) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
            vector = f'[{1 + index},{tile + 1},{(index * 3 + tile) % 5 + 1}]'
            await database.execute(
                'INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ('
                '(SELECT dataset_id FROM samples WHERE id = $1), $1, $2, $3::text::vector)',
                sample_id,
                f'clip_patch_2x2_r{row}c{col}',
                vector,
            )
    await database.execute('UPDATE mirrors SET fields_reported_at = NULL')
    await sync(database, link_id)
    assert await database.fetchval('SELECT count(*) FROM follower_fields') == 1
    return created, link_id, mirror


class _Request:
    async def is_disconnected(self):
        return False


def fake_request() -> Request:
    """A request that stays connected, for calling the relay directly."""
    return cast('Request', _Request())


async def serve_once(database, created, link_id):
    """The follower's side, once: claim the relayed search through the endpoint, run it here, answer it."""
    from backend import follower_search
    from backend.main import app

    headers = await follower_headers(created)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://primary/api') as client:
        item = (await client.get('/follower/searches', params={'wait': 10}, headers=headers)).json()
        results = await follower_search.run_search(database.pool, link_id, item)
        answered = await client.post(f"/follower/searches/{item['id']}", json={'results': results}, headers=headers)
        assert answered.status_code == 200, answered.text
        return item, results


def test_a_patch_search_on_a_follower_s_scale_runs_there_and_matches_a_local_search(monkeypatch, registry_database):
    async def scenario(database):
        from backend import follower_search, patch_search

        created, link_id, mirror = await mirror_with_own_patches(monkeypatch, database)
        seed = await database.fetchval('SELECT min(id) FROM samples WHERE dataset_id = 1')
        body = {
            'sample_id': seed,
            'base_field': 'clip',
            'scale': '2x2',
            'row': 0,
            'col': 0,
            'limit': 10,
            'per_image': 1,
        }
        relayed, (item, _) = await asyncio.gather(
            follower_search.relay(fake_request(), 1, 'clip', '2x2', 'patch', body),
            serve_once(database, created, link_id),
        )
        assert item['kind'] == 'patch' and item['dataset_id'] == 1
        # The same search run on the mirror directly, in the primary's ids.
        local_seed = await database.fetchval(
            'SELECT sample_id FROM mirror_samples WHERE dataset_id = $1 AND origin_id = $2',
            mirror['dataset_id'],
            seed,
        )
        async with database.pool.acquire() as connection:
            vector = await connection.fetchval(
                "SELECT vector FROM embeddings WHERE sample_id = $1 AND field_name = 'clip_patch_2x2_r0c0'",
                local_seed,
            )
            direct = await patch_search.nearest_patches(
                connection,
                dataset_id=mirror['dataset_id'],
                fields=None,
                like_pattern='clip_patch_2x2_%',
                seed_vector=vector,
                dims=3,
                limit=10,
                per_image=1,
                exclude=(local_seed, 'clip_patch_2x2_r0c0'),
            )
            to_origin = dict(await connection.fetch('SELECT sample_id, origin_id FROM mirror_samples'))
        assert [(r['sample_id'], r['field_name']) for r in relayed] == [
            (to_origin[r['sample_id']], r['field_name']) for r in direct
        ]
        assert all(r['filepath'].startswith('s3://images/p') for r in relayed)  # the primary's own samples
        assert await database.fetchval('SELECT state FROM follower_searches') == 'answered'

    registry_database(scenario)


def test_a_relayed_search_is_refused_offline_busy_or_with_a_filter_the_follower_can_t_run(
    monkeypatch,
    registry_database,
):
    async def scenario(database):
        from backend import follower_search

        created, link_id, mirror = await mirror_with_own_patches(monkeypatch, database)
        seed = await database.fetchval('SELECT min(id) FROM samples WHERE dataset_id = 1')
        body = {'sample_id': seed, 'base_field': 'clip', 'scale': '2x2', 'row': 0, 'col': 0, 'limit': 5}
        follower = await database.fetchval('SELECT id FROM followers')

        # Busy: PENDING_PER_FOLLOWER searches already queued for it.
        for _ in range(follower_search.PENDING_PER_FOLLOWER):
            await database.execute(
                "INSERT INTO follower_searches (follower_id, dataset_id, kind, request) VALUES ($1, 1, 'patch', '{}')",
                follower,
            )
        with pytest.raises(HTTPException) as busy:
            await follower_search.relay(fake_request(), 1, 'clip', '2x2', 'patch', body)
        assert busy.value.status_code == 429
        await database.execute('DELETE FROM follower_searches')

        # Offline: not seen within ONLINE_SECONDS.
        await database.execute("UPDATE followers SET last_seen_at = now() - interval '1 hour'")
        with pytest.raises(HTTPException) as offline:
            await follower_search.relay(fake_request(), 1, 'clip', '2x2', 'patch', body)
        assert offline.value.status_code == 503 and "'lab'" in offline.value.detail

        # Filters a mirror can't evaluate as the primary would: refused, never passed on.
        item = {'dataset_id': 1, 'kind': 'patch', 'request': {**body, 'filters': CONCEPT_FILTER}}
        with pytest.raises(follower_search.RelayRefused, match='concept'):
            await follower_search.run_search(database.pool, link_id, item)
        for node in (
            {'field': 'id', 'operator': 'in_selection', 'value': 'token'},
            {'field': 'crop', 'operator': 'sql', 'value': '1=1'},
        ):
            with pytest.raises(follower_search.RelayRefused):
                follower_search.check_filters(node)
        # Sample ids are translated to the mirror's.
        follower_search.check_filters({'logic': 'AND', 'filters': [{'field': 'id', 'operator': 'in', 'value': [seed]}]})
        translated = sample_ids.translate_ids(
            {'field': 'id', 'operator': 'in', 'value': [seed, 999999]},
            {seed: 7},
        )
        assert translated['value'] == [7]

    registry_database(scenario)


def test_a_relayed_range_on_sample_ids_is_refused():
    from backend import follower_search

    # A mirror numbers its samples in another order, so a range on ids can't be translated, only refused.
    refused: tuple[dict, ...] = (
        {'field': 'id', 'operator': '>', 'value': 15},
        {'field': 'parent_id', 'operator': '<=', 'value': 3},
        {'field': 'metadata.parent_id', 'operator': 'contains', 'value': 1},
        {'field': 'tags', 'operator': 'parent_matches', 'value': {'field': 'id', 'operator': '>=', 'value': 2}},
        {
            'logic': 'OR',
            'filters': [
                {'field': 'crop', 'operator': '=', 'value': 'corn'},
                {'field': 'id', 'operator': '<', 'value': 9},
            ],
        },
    )
    for node in refused:
        with pytest.raises(follower_search.RelayRefused, match='sample ids'):
            follower_search.check_filters(node)
    # Membership is translated, whether a parent is set doesn't depend on ids, and a label element's id is its own.
    allowed: tuple[dict, ...] = (
        {'field': 'id', 'operator': 'not in', 'value': [1, 2]},
        {'field': 'parent_id', 'operator': 'is_null'},
        {'field': 'parent_id', 'operator': 'exists'},
        {'field': 'detections', 'operator': 'elem_match', 'value': {'field': 'id', 'operator': '>', 'value': 3}},
        {'field': 'score', 'operator': '>', 'value': 0.5},
    )
    for node in allowed:
        follower_search.check_filters(node)


def test_a_follower_s_answer_is_its_own_and_reads_only_the_searched_dataset(monkeypatch, registry_database):
    async def scenario(database):
        from backend import follower_search

        created, link_id, mirror = await mirror_with_own_patches(monkeypatch, database)
        await ingest([{'crop': 'secret'}], dataset_id=2, prefix='s3://images/other')
        other = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 2')
        mine = await database.fetchval('SELECT min(id) FROM samples WHERE dataset_id = 1')
        # A sample of another dataset in the answer is dropped: only the searched dataset's samples are shown.
        rows = await follower_search.hydrate(
            1,
            [
                {'origin_id': other, 'field': 'clip_patch_2x2_r0c0', 'distance': 0.1},
                {'origin_id': mine, 'field': 'clip_patch_2x2_r0c1', 'distance': 0.2},
            ],
        )
        assert [r['sample_id'] for r in rows] == [mine]
        # Answers only for a search it claimed, and only once.
        follower = dict(await database.fetchrow('SELECT * FROM followers'))
        search_id = await database.fetchval(
            "INSERT INTO follower_searches (follower_id, dataset_id, kind, request) "
            "VALUES ($1, 1, 'patch', '{\"limit\": 5}') RETURNING id",
            follower['id'],
        )
        assert await follower_search.answer(follower, str(search_id), [], None) is False  # not claimed yet
        claimed = await follower_search.claim(follower, 0)
        assert claimed is not None and claimed['id'] == str(search_id)
        too_many = [{'origin_id': mine, 'field': 'f', 'distance': 0.0}] * 6
        assert await follower_search.answer(follower, str(search_id), too_many, None) is True
        assert await database.fetchval('SELECT state FROM follower_searches') == 'failed'  # over its limit
        assert await follower_search.answer(follower, str(search_id), [], None) is False  # already answered

    registry_database(scenario)


def test_unfollowing_a_dataset_ends_its_relayed_searches(monkeypatch, registry_database):
    async def scenario(database):
        from backend import follower_search

        await mirror_with_own_patches(monkeypatch, database)
        follower = dict(await database.fetchrow('SELECT * FROM followers'))
        seed = await database.fetchval('SELECT min(id) FROM samples WHERE dataset_id = 1')
        body = {'sample_id': seed, 'base_field': 'clip', 'scale': '2x2', 'row': 0, 'col': 0, 'limit': 10}

        async def queued(state='pending'):
            return await database.fetchval(
                "INSERT INTO follower_searches (follower_id, dataset_id, kind, request, state) "
                "VALUES ($1, 1, 'patch', '{\"limit\": 5}', $2) RETURNING id",
                follower['id'],
                state,
            )

        # A search the primary is waiting on ends at once, saying why.
        relayed = asyncio.create_task(follower_search.relay(fake_request(), 1, 'clip', '2x2', 'patch', body))
        for _ in range(100):
            if await database.fetchval("SELECT count(*) FROM follower_searches WHERE state = 'pending'"):
                break
            await asyncio.sleep(0.05)
        running = await queued('claimed')
        await followers.unfollow_dataset(follower['id'], 1, user=USER)
        with pytest.raises(HTTPException) as ended:
            await asyncio.wait_for(relayed, 10)
        assert ended.value.status_code == 503 and 'no longer follows this dataset' in ended.value.detail
        assert await database.fetchval("SELECT count(*) FROM follower_searches WHERE state <> 'cancelled'") == 0
        assert await follower_search.answer(follower, str(running), [], None) is False
        # One queued as the subscription went (the relay had just checked its providers) isn't claimed.
        await queued()
        assert await follower_search.claim(follower, 0) is None

    registry_database(scenario)


def test_patch_search_relays_only_when_the_seed_s_scale_isn_t_held_here(monkeypatch, registry_database):
    async def scenario(database):
        from backend import follower_search
        from backend.models import PatchSearchRequest
        from backend.routers import search

        created, link_id, mirror = await mirror_with_own_patches(monkeypatch, database)
        monkeypatch.setattr(search, 'db', database)
        monkeypatch.setattr(search, 'get_optional_user', AsyncMock(return_value=None))
        monkeypatch.setattr(search, 'check_dataset_access', AsyncMock())
        relayed = AsyncMock(return_value=[])
        monkeypatch.setattr(follower_search, 'relay', relayed)
        seed = await database.fetchval('SELECT min(id) FROM samples WHERE dataset_id = 1')
        request = PatchSearchRequest(dataset_id=1, sample_id=seed, patch_scale='2x2', limit=5)
        assert await search.patch_search(request, _Request()) == []
        assert relayed.await_args is not None and relayed.await_args.args[1:5] == (1, 'clip', '2x2', 'patch')
        # A scale nobody holds: 404, as before.
        relayed.reset_mock()
        with pytest.raises(HTTPException) as missing:
            await search.patch_search(PatchSearchRequest(dataset_id=1, sample_id=seed, patch_scale='4x4'), _Request())
        assert missing.value.status_code == 404 and not relayed.await_count
        # A scale this dataset holds itself, on other samples: a seed lacking its tile isn't sent to the follower.
        other = await database.fetchval('SELECT max(id) FROM samples WHERE dataset_id = 1')
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip_patch_2x2_r0c0', '[1,1,1]')",
            other,
        )
        follower_search._held_here.clear()
        with pytest.raises(HTTPException) as held:
            await search.patch_search(request, _Request())
        assert held.value.status_code == 404 and not relayed.await_count

    registry_database(scenario)


def test_relayed_filters_keep_element_conditions_and_requests_are_capped(monkeypatch, registry_database):
    async def scenario(database):
        from backend import follower_search

        created, link_id, mirror = await mirror_with_own_patches(monkeypatch, database)
        element = {
            'field': 'detections',
            'operator': 'elem_match',
            'value': {'logic': 'AND', 'filters': [{'field': 'id', 'operator': '=', 'value': 5}]},
        }
        follower_search.check_filters({'logic': 'AND', 'filters': [element]})  # the grid's facets: allowed
        # An element's own `id` is not a sample id: left as it is.
        assert sample_ids.translate_ids(element, {5: 99}) == element
        assert sample_ids.sample_ids_in(element, set()) == set()
        seed = await database.fetchval('SELECT min(id) FROM samples WHERE dataset_id = 1')
        request = {'sample_id': seed, 'base_field': 'clip', 'scale': '2x2', 'row': 0, 'col': 0}
        for limit, per_image in ((follower_search.MAX_LIMIT + 1, 1), (5, follower_search.MAX_PER_IMAGE + 1), (0, 1)):
            item = {'dataset_id': 1, 'kind': 'patch', 'request': {**request, 'limit': limit, 'per_image': per_image}}
            with pytest.raises(follower_search.RelayRefused, match='more than a follower runs'):
                await follower_search.run_search(database.pool, link_id, item)

    registry_database(scenario)


def test_a_follower_answers_running_searches_before_it_reconnects(monkeypatch):
    """A failed poll (the primary restarting, say) closes the follower's client: the searches already running answer
    through it first, instead of failing and leaving the primary's users to wait for a 504."""
    from backend import follower_search

    events = []
    stopping = asyncio.Event()

    class Pool:
        async def fetchrow(self, query, *args):
            return {'id': 1, 'url': 'http://primary/api', 'token_encrypted': 'x'}

        async def fetchval(self, query, *args):
            return 'installation-1'

    class Client:
        closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.closed = True
            events.append('closed')

    client = Client()
    polls = iter([{'id': 'search-1', 'dataset_id': 1, 'kind': 'patch', 'request': {}}])

    async def call(the_client, method, path, **kwargs):
        if method == 'GET':
            try:
                return next(polls)
            except StopIteration:
                stopping.set()
                raise follower_sync.PrimaryUnavailable('the primary is restarting')
        events.append('answered on a closed client' if the_client.closed else 'answered')
        return {}

    async def run_search(pool, link_id, item):
        await asyncio.sleep(0.2)  # still running when the next poll fails
        return []

    monkeypatch.setattr(follower_sync, 'connect', lambda link, instance=None: client)
    monkeypatch.setattr(follower_sync, 'call', call)
    monkeypatch.setattr(follower_search, 'run_search', run_search)
    asyncio.run(asyncio.wait_for(follower_search.serve_link(Pool(), 1, stopping), 10))
    assert events == ['answered', 'closed']


async def mirror_with_own_field(monkeypatch, database):
    """Dataset 1 (corn, soy) copied into a mirror, with a vector field only this instance has on both samples."""
    created, link_id = await setup(monkeypatch, database)
    await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/r')
    follower = await database.fetchval('SELECT id FROM followers')
    await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
    await sync(database, link_id)
    mirror = await mirror_of(database)
    for row in (await copied(database, mirror)).values():
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'mine', '[1,2]')",
            row['id'],
        )
    return created, link_id, follower, mirror


async def crops_with_own_field(database, dataset_id):
    return sorted(
        row['crop']
        for row in await database.fetch(
            """SELECT s.metadata->>'crop' AS crop FROM samples s JOIN embeddings e ON e.sample_id = s.id
               WHERE s.dataset_id = $1 AND e.field_name = 'mine'""",
            dataset_id,
        )
    )


async def resumable(database):
    """Which copies the Primaries page says a new copy resumes into, by dataset."""
    listed = await primaries.list_primaries(user=USER)
    mirrors = [m for p in listed['primaries'] for m in p['mirrors']] + listed['unlinked_mirrors']
    return {m['dataset_id']: m['resumable'] for m in mirrors}


def test_shared_again_the_copy_resumes_into_its_stopped_mirror_and_keeps_its_own_fields(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, follower, mirror = await mirror_with_own_field(monkeypatch, database)
        await followers.unfollow_dataset(follower, 1, user=USER)
        await sync(database, link_id)
        stopped = await mirror_of(database)
        assert stopped['state'] == 'stopped' and stopped['stopped_at'] is not None and stopped['primary_identity']
        # Deleted on the primary while not followed: it goes here when the copy resumes, with its own field.
        await database.execute("DELETE FROM samples WHERE dataset_id = 1 AND metadata->>'crop' = 'soy'")
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        resumed = await mirror_of(database)
        assert await database.fetchval('SELECT count(*) FROM mirrors') == 1  # no new dataset
        assert resumed['dataset_id'] == mirror['dataset_id'] and resumed['state'] == 'following'
        assert resumed['stopped_at'] is None
        assert await crops_with_own_field(database, mirror['dataset_id']) == ['corn']
        assert sorted(r['metadata']['crop'] for r in (await copied(database, resumed)).values()) == ['corn']

    registry_database(scenario)


def test_the_primary_added_again_resumes_its_copies(monkeypatch, registry_database):
    async def scenario(database):
        created, link_id, follower, mirror = await mirror_with_own_field(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        await primaries.remove_primary(link_id, user=USER)
        assert (await mirror_of(database))['link_id'] is None
        # Added again as an admin does: the primary gets back what this instance followed (POST /follower/return).
        again = (
            await primaries.add_primary(
                primaries.PrimaryCreate(url='http://primary/api', token=created['token']),
                user=USER,
            )
        )['id']
        await sync(database, again)
        resumed = await mirror_of(database)
        assert await database.fetchval('SELECT count(*) FROM mirrors') == 1
        assert (resumed['dataset_id'], resumed['link_id'], resumed['state']) == (
            mirror['dataset_id'],
            again,
            'following',
        )
        assert await crops_with_own_field(database, mirror['dataset_id']) == ['corn', 'soy']

    registry_database(scenario)


def test_removed_on_the_primary_then_followed_again_the_copy_resumes(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, follower, mirror = await mirror_with_own_field(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        # The primary's admin removes the follower: the copy stops on the refusal, and the link stays.
        await followers.remove_follower(follower, user=USER)
        await sync(database, link_id)
        stopped = await mirror_of(database)
        assert (stopped['state'], stopped['link_id']) == ('stopped', link_id)
        # Followed again: a new follower there, and this instance stops following and adds the primary with its token.
        created = await followers.create_follower(followers.FollowerCreate(name='lab-2'), user=USER)
        await followers.follow_dataset(
            await database.fetchval('SELECT id FROM followers'),
            1,
            followers.FollowOptions(),
            user=USER,
        )
        await primaries.remove_primary(link_id, user=USER)
        again = await database.fetchval(
            "INSERT INTO primary_links (url, token_encrypted, created_by) VALUES ('http://primary/api', $1, 1) "
            'RETURNING id',
            encrypt_secret(created['token']),
        )
        await sync(database, again)
        resumed = await mirror_of(database)
        assert await database.fetchval('SELECT count(*) FROM mirrors') == 1
        assert (resumed['dataset_id'], resumed['link_id'], resumed['state']) == (
            mirror['dataset_id'],
            again,
            'following',
        )
        assert await crops_with_own_field(database, mirror['dataset_id']) == ['corn', 'soy']

    registry_database(scenario)


async def restore_primary(database):
    """As a restore of the primary into a new database: its samples come back with ids handed out again, soy is gone
    and rice is new, and the follower's link knows the primary by another database identity."""
    await database.execute('DELETE FROM samples WHERE dataset_id = 1')
    await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/r')  # corn's path, a new id
    await ingest([{'crop': 'rice'}], dataset_id=1, prefix='s3://images/n')
    await database.execute("UPDATE primary_links SET identity = '1:1'")
    await database.execute("UPDATE mirrors SET primary_identity = '1:1'")


def test_after_a_primary_restore_the_copy_is_held_asks_and_resumes_into_itself(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, _, mirror = await mirror_with_own_field(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        monkeypatch.setattr(follower_sync, 'queue_sync', AsyncMock())
        await restore_primary(database)
        await sync(database, link_id)
        # Held, compared, and asking: nothing changed on the copy, and no new copy was made.
        held = dict(await database.fetchrow('SELECT * FROM mirror_restores'))
        assert (held['total'], held['matched'], held['only_here'], held['phase']) == (2, 1, 1, 'ask')
        assert await database.fetchval('SELECT count(*) FROM mirrors') == 1
        stopped = await mirror_of(database)
        assert stopped['state'] == 'stopped' and 'Primaries page' in stopped['last_error']
        assert await crops_with_own_field(database, mirror['dataset_id']) == ['corn', 'soy']
        listed = await primaries.list_primaries(user=USER)
        [entry] = listed['primaries'][0]['mirrors']
        assert entry['restore']['compared'] and entry['restore']['matched'] == 1 and not entry['resumable']

        # Its admin resumes into it: the matched sample keeps what was added here, soy goes, rice is copied.
        await primaries.decide_restore(mirror['dataset_id'], primaries.RestoreDecision(decision='resume'), user=USER)
        with pytest.raises(HTTPException) as again:
            await primaries.decide_restore(mirror['dataset_id'], primaries.RestoreDecision(decision='new'), user=USER)
        assert again.value.status_code == 409
        await sync(database, link_id)
        resumed = await mirror_of(database)
        assert (resumed['dataset_id'], resumed['state']) == (mirror['dataset_id'], 'following')
        assert await database.fetchval('SELECT count(*) FROM mirror_restores') == 0
        assert await database.fetchval('SELECT count(*) FROM mirror_restore_map') == 0
        assert sorted(r['metadata']['crop'] for r in (await copied(database, resumed)).values()) == ['corn', 'rice']
        assert await crops_with_own_field(database, mirror['dataset_id']) == ['corn']

    registry_database(scenario)


def test_after_a_primary_restore_a_new_copy_leaves_the_held_one_as_it_is(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, _, mirror = await mirror_with_own_field(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        monkeypatch.setattr(follower_sync, 'queue_sync', AsyncMock())
        await restore_primary(database)
        await sync(database, link_id)
        await primaries.decide_restore(mirror['dataset_id'], primaries.RestoreDecision(decision='new'), user=USER)
        await sync(database, link_id)
        await sync(database, link_id)
        states = dict(await database.fetch('SELECT dataset_id, state FROM mirrors'))
        assert states[mirror['dataset_id']] == 'stopped' and len(states) == 2
        assert await database.fetchval('SELECT keep_separate FROM mirrors WHERE dataset_id = $1', mirror['dataset_id'])
        fresh = await mirror_of(database)
        assert fresh['state'] == 'following'
        assert sorted(r['metadata']['crop'] for r in (await copied(database, fresh)).values()) == ['corn', 'rice']
        assert await crops_with_own_field(database, mirror['dataset_id']) == ['corn', 'soy']  # the old copy as it was

    registry_database(scenario)


async def sync_until(database, link_id, reached):
    """A sync job stepped until `reached(database)` holds after a step, then left there, as a runner that stopped."""
    step = follower_sync.handler(database.pool)
    checkpoint: dict = {}
    for _ in range(200):
        result = await step({'args': {'link_id': link_id}, 'checkpoint': checkpoint})
        if result.publish is not None:
            async with database.pool.acquire() as connection, connection.transaction():
                await result.publish(connection)
        if result.checkpoint is not None:
            checkpoint = result.checkpoint
        if await reached(database):
            return
        if result.finished:
            break
    raise AssertionError('The sync never got there')


def refusing_match_keys(monkeypatch, status, detail):
    real_call = follower_sync.call

    async def call(client, method, path, **kwargs):
        if path.endswith('/match-keys'):
            raise follower_sync.PrimaryRefused(status, detail, None)
        return await real_call(client, method, path, **kwargs)

    monkeypatch.setattr(follower_sync, 'call', call)


def test_a_held_copy_that_fails_does_not_hold_up_the_primary_s_other_mirrors(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, follower, _ = await mirror_with_own_field(monkeypatch, database)
        await restore_primary(database)
        await ingest([{'crop': 'oats'}], dataset_id=2, prefix='s3://images/o')
        await followers.follow_dataset(follower, 2, followers.FollowOptions(), user=USER)
        refusing_match_keys(monkeypatch, 400, 'the primary choked')
        await sync(database, link_id)
        # The held copy's error is its own, tried again later; the primary's other dataset is copied meanwhile.
        held = dict(await database.fetchrow('SELECT * FROM mirror_restores'))
        assert 'the primary choked' in held['last_error'] and held['retry_after'] is not None
        assert held['phase'] == 'compare' and held['failures'] == 1
        other = await database.fetchrow('SELECT * FROM mirrors WHERE origin_dataset_id = 2')
        assert other['state'] == 'following'
        assert await database.fetchval('SELECT last_error FROM primary_links') is None

    registry_database(scenario)


def test_a_primary_restored_onto_an_older_release_gets_a_new_copy(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, _, mirror = await mirror_with_own_field(monkeypatch, database)
        await restore_primary(database)
        refusing_match_keys(monkeypatch, 404, 'Not Found')  # a release without /match-keys
        await sync(database, link_id)
        await sync(database, link_id)
        old = await database.fetchrow('SELECT * FROM mirrors WHERE dataset_id = $1', mirror['dataset_id'])
        assert (
            old['state'] == 'stopped'
            and old['keep_separate']
            and old['last_error'] == follower_sync.OLDER_PRIMARY_REASON
        )
        assert await database.fetchval('SELECT count(*) FROM mirror_restores') == 0
        fresh = await mirror_of(database)
        assert fresh['dataset_id'] != mirror['dataset_id'] and fresh['state'] == 'following'

    registry_database(scenario)


def test_a_404_for_a_dataset_unshared_meanwhile_is_not_an_older_primary(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, _, mirror = await mirror_with_own_field(monkeypatch, database)
        await restore_primary(database)
        refusing_match_keys(monkeypatch, 404, 'This follower does not follow that dataset')
        await sync(database, link_id)
        held = dict(await database.fetchrow('SELECT * FROM mirror_restores'))
        assert 'does not follow' in held['last_error'] and held['decision'] is None
        assert not await database.fetchval(
            'SELECT keep_separate FROM mirrors WHERE dataset_id = $1',
            mirror['dataset_id'],
        )

    registry_database(scenario)


def test_a_held_copy_s_writes_that_fail_are_isolated(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, _, mirror = await mirror_with_own_field(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        monkeypatch.setattr(follower_sync, 'queue_sync', AsyncMock())
        await restore_primary(database)
        await sync(database, link_id)
        await primaries.decide_restore(mirror['dataset_id'], primaries.RestoreDecision(decision='resume'), user=USER)
        real = follower_sync.begin_mirror_write

        async def refusing(connection, dataset_id):
            if dataset_id == mirror['dataset_id']:
                raise ValueError('a guard refused it')
            await real(connection, dataset_id)

        monkeypatch.setattr(follower_sync, 'begin_mirror_write', refusing)
        await sync(database, link_id)  # finishes: the held copy's failure is its own
        held = dict(await database.fetchrow('SELECT * FROM mirror_restores'))
        assert 'a guard refused it' in held['last_error'] and held['phase'] == 'unmatched' and held['retry_after']
        assert await crops_with_own_field(database, mirror['dataset_id']) == ['corn', 'soy']  # nothing deleted

    registry_database(scenario)


def test_an_unshare_during_a_resume_leaves_the_copy_s_ids_whole(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, follower, mirror = await mirror_with_own_field(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        monkeypatch.setattr(follower_sync, 'queue_sync', AsyncMock())
        monkeypatch.setattr(follower_sync, 'RESTORE_ROWS', 1)
        await restore_primary(database)
        await sync(database, link_id)
        await primaries.decide_restore(mirror['dataset_id'], primaries.RestoreDecision(decision='resume'), user=USER)

        async def half_moved(db):
            return await db.fetchval('SELECT phase FROM mirror_restores') == 'negate' and bool(
                await db.fetchval('SELECT count(*) FROM mirror_samples WHERE origin_id < 0'),
            )

        await sync_until(database, link_id, half_moved)
        # The primary stops sharing the dataset meanwhile: the move finishes first, then the question goes.
        await followers.unfollow_dataset(follower, 1, user=USER)
        await sync(database, link_id)
        assert await database.fetchval('SELECT count(*) FROM mirror_restores') == 0
        assert await database.fetchval('SELECT count(*) FROM mirror_samples WHERE origin_id < 0') == 0
        assert (await mirror_of(database))['state'] == 'stopped'

    registry_database(scenario)


def test_a_cursor_too_old_copies_again_into_the_same_mirror(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, follower, mirror = await mirror_with_own_field(monkeypatch, database)
        # A cursor from before the copy's start: the feed answers 409, the copy stops and starts again, resumed.
        await database.execute('UPDATE mirrors SET feed_cursor = 0')
        await database.execute("UPDATE samples SET tags = '{fresh}' WHERE dataset_id = 1")
        await sync(database, link_id)
        resumed = await mirror_of(database)
        assert await database.fetchval('SELECT count(*) FROM mirrors') == 1
        assert resumed['dataset_id'] == mirror['dataset_id'] and resumed['state'] == 'following'
        assert await crops_with_own_field(database, mirror['dataset_id']) == ['corn', 'soy']

    registry_database(scenario)


def test_a_copy_kept_separate_is_never_resumed_and_the_latest_stopped_copy_is(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, follower, first = await mirror_with_own_field(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        await followers.unfollow_dataset(follower, 1, user=USER)
        await sync(database, link_id)
        with pytest.raises(HTTPException) as live:  # only a stopped copy
            await primaries.keep_mirror_separate(999999, user=USER)
        assert live.value.status_code == 409
        await primaries.keep_mirror_separate(first['dataset_id'], user=USER)
        listed = await primaries.list_primaries(user=USER)
        kept = next(m for m in listed['primaries'][0]['mirrors'] if m['dataset_id'] == first['dataset_id'])
        assert kept['keep_separate'] is True and kept['resumable'] is False
        # Shared again: a new dataset, the kept copy untouched.
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        second = await mirror_of(database)
        assert second['dataset_id'] != first['dataset_id'] and second['state'] == 'following'
        assert (await database.fetchrow('SELECT state FROM mirrors WHERE dataset_id = $1', first['dataset_id']))[
            'state'
        ] == 'stopped'
        # Both stopped, the first no longer kept apart: the latest stopped (the second) is the one resumed, and the
        # only one the page says resumes.
        await followers.unfollow_dataset(follower, 1, user=USER)
        await sync(database, link_id)
        await database.execute('UPDATE mirrors SET keep_separate = FALSE')
        assert await resumable(database) == {first['dataset_id']: False, second['dataset_id']: True}
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        states = dict(await database.fetch('SELECT dataset_id, state FROM mirrors'))
        assert states == {first['dataset_id']: 'stopped', second['dataset_id']: 'following'}
        # While the second follows, the first doesn't resume either: the second would stop and be resumed.
        assert await resumable(database) == {first['dataset_id']: False, second['dataset_id']: False}

    registry_database(scenario)


def test_a_copy_of_another_primary_database_is_not_resumed(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, follower, first = await mirror_with_own_field(monkeypatch, database)
        await followers.unfollow_dataset(follower, 1, user=USER)
        await sync(database, link_id)
        # As after a restore into a new database: its sample ids can't be trusted, so it isn't resumed.
        await database.execute("UPDATE mirrors SET primary_identity = '1:1'")
        monkeypatch.setattr(primaries, 'db', database)
        assert await resumable(database) == {first['dataset_id']: False}
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        assert await database.fetchval('SELECT count(*) FROM mirrors') == 2
        assert (await mirror_of(database))['dataset_id'] != first['dataset_id']

    registry_database(scenario)


async def as_follower(created, method, path, body=None):
    """A call to the primary's follower API with the follower's token, as its instance makes it."""
    from backend.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://primary/api') as client:
        response = await client.request(
            method,
            path,
            json=body,
            headers=await follower_headers(created),
        )
    assert response.status_code == 200, response.text
    return response.json()


async def log_rows(database, dataset_id):
    return sum(
        [
            await database.fetchval(f'SELECT count(*) FROM {table} WHERE dataset_id = $1', dataset_id)
            for table in followers.LOG_TABLES
        ],
    )


def test_stopping_tells_the_primary_and_adding_it_again_brings_the_datasets_back(monkeypatch, registry_database):
    async def scenario(database):
        created, link_id, follower, mirror = await mirror_with_own_field(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        await followers.follow_dataset(follower, 1, followers.FollowOptions(tags='both', images=True), user=USER)
        await database.execute(
            "INSERT INTO follower_fields (follower_id, dataset_id, base_field, scale, dims, indexed) "
            "VALUES ($1, 1, 'clip', '2x2', 3, FALSE)",
            follower,
        )
        waiting = await database.fetchval(
            "INSERT INTO follower_searches (follower_id, dataset_id, kind, request) VALUES ($1, 1, 'patch', '{}') "
            'RETURNING id',
            follower,
        )
        await database.execute("UPDATE samples SET tags = '{late}' WHERE dataset_id = 1")
        assert await log_rows(database, 1) > 0

        assert await primaries.remove_primary(link_id, user=USER) == {'removed': link_id, 'told_primary': True}
        # The primary keeps nothing for it: no subscription, log, reported scales or waiting search.
        assert await database.fetchval('SELECT count(*) FROM follower_datasets') == 0
        assert not await database.fetchval('SELECT followed FROM datasets WHERE id = 1')
        assert await log_rows(database, 1) == 0
        assert await database.fetchval('SELECT count(*) FROM follower_fields') == 0
        assert await database.fetchval('SELECT state FROM follower_searches WHERE id = $1', waiting) == 'cancelled'
        listed = (await followers.list_followers(user=USER))[0]
        assert listed['left_at'] is not None and listed['datasets'] == []
        assert [(d['dataset_id'], d['tags'], d['views'], d['images']) for d in listed['departures']] == [
            (1, 'both', 'primary', True),
        ]
        # Not logged while it's away, and a sync still running on the follower (its config call) restores nothing.
        await database.execute("UPDATE samples SET tags = '{away}' WHERE dataset_id = 1")
        assert await log_rows(database, 1) == 0
        assert (await as_follower(created, 'GET', '/follower/config'))['datasets'] == []
        assert await database.fetchval('SELECT count(*) FROM follower_datasets') == 0

        again = await primaries.add_primary(
            primaries.PrimaryCreate(url='http://primary/api', token=created['token']),
            user=USER,
        )
        assert again['datasets'] == 1
        back = await database.fetchrow('SELECT * FROM follower_datasets')
        assert (back['dataset_id'], back['tags'], back['images']) == (1, 'both', True)
        assert back['start_txid'] is not None
        assert await database.fetchval('SELECT count(*) FROM follower_departures') == 0
        assert await database.fetchval('SELECT left_at FROM followers') is None
        assert await as_follower(created, 'POST', '/follower/return') == {'restored': []}  # nothing more to restore
        await sync(database, again['id'])
        resumed = await mirror_of(database)
        assert (resumed['dataset_id'], resumed['state']) == (mirror['dataset_id'], 'following')
        assert await crops_with_own_field(database, mirror['dataset_id']) == ['corn', 'soy']

    registry_database(scenario)


def test_stopping_stops_here_when_the_primary_can_t_be_told(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id, follower, _ = await mirror_with_own_field(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        for answer, detail in (
            (None, 'The primary could not be reached'),
            (404, "The primary runs an older release, which can't be told"),
        ):

            def handler(request, answer=answer):
                if answer is None:
                    raise httpx.ConnectError('unreachable', request=request)
                return httpx.Response(answer, json={'detail': 'Not Found'})

            monkeypatch.setattr(
                follower_sync,
                'connect',
                lambda link, instance=None, handler=handler: httpx.AsyncClient(
                    transport=httpx.MockTransport(handler),
                    base_url=link['url'],
                ),
            )
            result = await primaries.remove_primary(link_id, user=USER)
            assert (result['removed'], result['told_primary']) == (link_id, False)
            assert result['detail'].startswith(detail)
            assert await database.fetchval('SELECT count(*) FROM primary_links') == 0
            assert (await mirror_of(database))['state'] == 'stopped'
            link_id = await database.fetchval(
                "INSERT INTO primary_links (url, token_encrypted, created_by) VALUES ('http://primary/api', 'x', 1) "
                'RETURNING id',
            )
        # The primary didn't hear: it still keeps the subscription, until its admin removes the follower.
        assert await database.fetchval('SELECT count(*) FROM follower_datasets WHERE follower_id = $1', follower) == 1

    registry_database(scenario)


def test_while_a_follower_is_away_shares_wait_and_only_followable_datasets_come_back(monkeypatch, registry_database):
    async def scenario(database):
        created, _ = await setup(monkeypatch, database)
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        assert await as_follower(created, 'POST', '/follower/leave') == {'left': [1]}

        # Shared while it's away: the dataset waits with the others instead of logging for no one.
        shared = await followers.follow_dataset(follower, 2, followers.FollowOptions(tags='both'), user=USER)
        assert shared['departed'] is True
        assert await database.fetchval('SELECT count(*) FROM follower_datasets') == 0
        assert not await database.fetchval('SELECT followed FROM datasets WHERE id = 2')
        # An admin can keep one from coming back; unfollowing it again finds nothing.
        await followers.unfollow_dataset(follower, 2, user=USER)
        with pytest.raises(HTTPException) as gone:
            await followers.unfollow_dataset(follower, 2, user=USER)
        assert gone.value.status_code == 404
        await followers.follow_dataset(follower, 2, followers.FollowOptions(tags='both'), user=USER)
        assert await database.fetchval('SELECT count(*) FROM follower_departures') == 2

        # Dataset 1 is being deleted: it doesn't come back (its deletion would remove the subscription anyway).
        await database.execute('UPDATE datasets SET deleting = TRUE WHERE id = 1')
        assert await as_follower(created, 'POST', '/follower/return') == {'restored': [2]}
        back = await database.fetchrow('SELECT dataset_id, tags, start_txid FROM follower_datasets')
        assert (back['dataset_id'], back['tags']) == (2, 'both') and back['start_txid'] is not None
        assert await database.fetchval('SELECT count(*) FROM follower_departures') == 0
        assert await database.fetchval('SELECT followed FROM datasets WHERE id = 2')

    registry_database(scenario)


def test_a_leave_that_arrives_after_the_primary_was_added_again_is_undone_on_the_next_sync(
    monkeypatch,
    registry_database,
):
    async def scenario(database):
        created, link_id, _, mirror = await mirror_with_own_field(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        # An earlier link's Stop following reaches the primary late, after this link was added: the primary has the
        # follower as left, though this instance still follows it.
        await as_follower(created, 'POST', '/follower/leave')
        assert await database.fetchval('SELECT count(*) FROM follower_datasets') == 0
        await sync(database, link_id)
        await sync(database, link_id)
        assert await database.fetchval('SELECT left_at FROM followers') is None
        assert await database.fetchval('SELECT count(*) FROM follower_datasets') == 1
        resumed = await mirror_of(database)
        assert await database.fetchval('SELECT count(*) FROM mirrors') == 1
        assert (resumed['dataset_id'], resumed['state']) == (mirror['dataset_id'], 'following')
        # Stop following here, for real: the link goes first, so a sync still running can't return.
        async with follower_sync.connect(
            await database.fetchrow('SELECT * FROM primary_links WHERE id = $1', link_id),
            await follower_sync.installation_id(database),
        ) as client:
            link = dict(await database.fetchrow('SELECT * FROM primary_links WHERE id = $1', link_id))
            await primaries.remove_primary(link_id, user=USER)
            assert await follower_sync.return_if_linked(database.pool, link, client) is False
        assert await database.fetchval('SELECT left_at FROM followers') is not None

    registry_database(scenario)


def test_a_late_sync_step_leaves_a_stopped_mirror_stopped(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        monkeypatch.setattr(primaries, 'db', database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/l')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        step = follower_sync.handler(database.pool)
        checkpoint: dict = {}
        for _ in range(50):
            result = await step({'args': {'link_id': link_id}, 'checkpoint': checkpoint})
            if result.message == 'Copy complete; following the feed':
                break
            if result.publish is not None:
                async with database.pool.acquire() as connection, connection.transaction():
                    await result.publish(connection)
            checkpoint = result.checkpoint if result.checkpoint is not None else checkpoint
        else:
            raise AssertionError('The copy did not complete')
        # The primary is removed here while that step runs; then the step publishes.
        await primaries.remove_primary(link_id, user=USER)
        async with database.pool.acquire() as connection, connection.transaction():
            await result.publish(connection)
            mirror = await mirror_of(database)
            await follower_sync.advance_cursor(connection, mirror['dataset_id'], 5)
            await follower_sync.note_failure(connection, mirror['dataset_id'], 'The primary answered 404')
        stopped = await mirror_of(database)
        assert stopped['state'] == 'stopped'
        assert stopped['last_error'] == 'No longer following: the primary was removed on this instance'
        assert stopped['feed_cursor'] == mirror['feed_cursor'] and stopped['failures'] == mirror['failures']

    registry_database(scenario)


def test_edits_sent_up_say_who_made_them_and_no_one_else_can(monkeypatch, registry_database):
    async def scenario(database):
        monkeypatch.setattr(views, 'db', database)
        monkeypatch.setattr(datasets, 'db', database)
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/m')
        follower = await database.fetchval('SELECT id FROM followers')
        both = followers.FollowOptions(tags='both', views='both')
        await followers.follow_dataset(follower, 1, both, user=USER)
        await sync(database, link_id)
        mirror = await mirror_of(database)
        alice = {**USER, 'username': 'alice'}
        local = sorted((await copied(database, mirror)).values(), key=lambda row: row['id'])

        await datasets.bulk_tag_samples(
            mirror['dataset_id'],
            datasets.BulkTagRequest(sample_ids=[local[0]['id']], add_tags=['weedy']),
            alice,
        )
        registry = await datasets._tag_registry_info(1, 'tags', ['weedy'], USER)
        assert registry['weedy']['created_by'] == 'alice on lab'
        # The person's name is display only: the follower's name comes from its account, not from what it sent.
        assert await database.fetchval("SELECT created_by_name FROM dataset_tags WHERE tag = 'weedy'") == 'alice'

        made = await views.create_view(
            mirror['dataset_id'],
            ViewCreate(name='Corn here', content={'filters': {}, 'made_on': 'elsewhere'}, is_public=True),
            user=alice,
        )
        assert (made['content']['made_on'], made['content']['made_by']) == ('lab', 'alice')
        on_primary = await database.fetchval("SELECT content FROM views WHERE dataset_id = 1 AND name = 'Corn here'")
        assert (on_primary['made_on'], on_primary['made_by']) == ('lab', 'alice')
        # Anyone else's view can't claim to come from a follower.
        own = await views.create_view(
            1,
            ViewCreate(name='Fake', content={'filters': {}, 'made_on': 'lab', 'made_by': 'bob'}, is_public=True),
            user=USER,
        )
        assert 'made_on' not in own['content'] and 'made_by' not in own['content']

        # A username the primary wouldn't accept (too long, say) is left out: the edit itself still goes through.
        long_name = {**USER, 'username': 'x' * 101}
        await datasets.bulk_tag_samples(
            mirror['dataset_id'],
            datasets.BulkTagRequest(sample_ids=[local[1]['id']], add_tags=['unnamed']),
            long_name,
        )
        assert await database.fetchval("SELECT created_by_name FROM dataset_tags WHERE tag = 'unnamed'") is None
        plain = await views.create_view(
            mirror['dataset_id'],
            ViewCreate(name='Soy here', content={'filters': {}}, is_public=True),
            user=long_name,
        )
        assert plain['content']['made_on'] == 'lab' and 'made_by' not in plain['content']

        # A large edit goes to a bulk job, which records the name too.
        response = await datasets.tag_samples(
            1,
            datasets.BulkTagRequest(sample_ids=[local[0]['origin_id']], add_tags=['queued'], background=True),
            USER,
            made_by='carol',
        )
        test_bulk_jobs.submitted(response)
        await test_bulk_jobs.finish(database)
        assert await database.fetchval("SELECT created_by_name FROM dataset_tags WHERE tag = 'queued'") == 'carol'

        assert followers.TagRequest(sample_ids=[1], made_by='  dee  ').made_by == 'dee'
        assert followers.TagRequest(sample_ids=[1], made_by='   ').made_by is None
        for bad in ('a\u202eb', 'x' * 101, 'tab\there'):
            with pytest.raises(ValueError):
                followers.TagRequest(sample_ids=[1], made_by=bad)

    registry_database(scenario)


def test_the_0017_backfill_gives_no_identity_to_a_stopped_copy_which_is_then_never_resumed(
    monkeypatch,
    registry_database,
):
    async def scenario(database):
        _, link_id, _, mirror = await mirror_with_own_field(monkeypatch, database)
        path = os.path.join(os.path.dirname(__file__), '..', 'db', 'migrations', '0017_mirror_resume.sql')
        with open(path) as migration:
            backfill = next(statement for statement in migration.read().split(';') if 'UPDATE mirrors' in statement)
        # A following copy gets its link's identity.
        await database.execute('UPDATE mirrors SET primary_identity = NULL')
        await database.execute(backfill)
        assert (await mirror_of(database))['primary_identity'] is not None
        # Stopped by a restore before 0017: its link holds the restored database's identity now, which isn't the
        # copy's, so it gets none.
        await database.execute(
            "UPDATE mirrors SET state = 'stopped', stopped_at = now(), primary_identity = NULL, last_error = $1",
            'The primary was restored into a new database: this copy stopped, and a new one started',
        )
        await database.execute(backfill)
        assert (await mirror_of(database))['primary_identity'] is None
        # The next copy makes a new dataset, though no newer stopped copy is there to be preferred.
        await sync(database, link_id)
        assert await database.fetchval('SELECT count(*) FROM mirrors') == 2
        assert (await mirror_of(database))['dataset_id'] != mirror['dataset_id']

    registry_database(scenario)


def test_a_cursor_behind_the_acknowledged_one_is_refused_and_the_mirror_copies_again(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        mirror = await mirror_of(database)
        backed_up = mirror['feed_cursor']  # the cursor in a backup of this follower, taken now

        # The primary changes; the follower applies it and acknowledges, so the primary prunes its log.
        await ingest([{'crop': 'soy'}], dataset_id=1, prefix='s3://images/b')
        await sync(database, link_id)
        await sync(database, link_id)
        acked = await database.fetchval('SELECT acked_txid FROM follower_datasets WHERE follower_id = $1', follower)
        assert acked > backed_up
        assert await database.fetchval('SELECT count(*) FROM sample_block_changes WHERE dataset_id = 1') == 0

        # The follower is restored from that backup: its cursor and its samples are the old ones, without soy.
        [soy] = [row['id'] for row in (await copied(database, mirror)).values() if row['metadata']['crop'] == 'soy']
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('fridge.mirror_sync', $1, TRUE)", str(mirror['dataset_id']))
            await connection.execute('DELETE FROM samples WHERE id = $1', soy)
            await connection.execute(
                'UPDATE mirrors SET feed_cursor = $2 WHERE dataset_id = $1',
                mirror['dataset_id'],
                backed_up,
            )
        # Its old cursor can't be answered: the rows between are gone. The cursor it acknowledged still reads.
        sub = dict(await database.fetchrow('SELECT * FROM followers WHERE id = $1', follower))
        identity = await followers.identity(database)
        with pytest.raises(HTTPException) as behind:
            await followers.follower_changes(1, backed_up, identity, 1000, follower=sub)
        assert behind.value.status_code == 409 and 'copy the dataset again' in behind.value.detail
        assert (await followers.follower_changes(1, acked, identity, 1000, follower=sub))['blocks'] == []

        # So its sync stops that copy rather than carrying on without soy, and a new copy resumes into the same
        # mirror: the new copy reads the feed from the subscription's start, which the primary takes once it has been
        # asked for the copy's plan.
        await sync(database, link_id)
        again = await mirror_of(database)
        assert again['dataset_id'] == mirror['dataset_id'] and again['state'] == 'following'
        assert sorted(row['metadata']['crop'] for row in (await copied(database, again)).values()) == ['corn', 'soy']

    registry_database(scenario)


async def test_a_primary_s_answer_is_never_served_as_a_page(monkeypatch, tmp_path):
    from backend import main

    page = b'<!doctype html><script>fetch("/api/users")</script>'
    answer = Mock(status_code=200, headers={'content-type': 'text/html; charset=utf-8'}, content=page)
    monkeypatch.setattr(main, 'S3_LOCAL_PATH', str(tmp_path))
    monkeypatch.setattr(main, '_queue_media_cache', main._write_media_cache)  # written at once
    monkeypatch.setattr(main, '_primary_client', AsyncMock(return_value=Mock(get=AsyncMock(return_value=answer))))
    source = {'dataset_id': 5, 'origin_id': 7, 'origin_dataset_id': 1, 'url': 'http://primary/api'}

    first = await main._media_from_primary(source, 's3://images/a-0.jpg', {})
    again = await main._media_from_primary(source, 's3://images/a-0.jpg', {})  # from the cache
    assert 'X-Cache' not in first.headers and again.headers['X-Cache'] == 'HIT'
    for response in (first, again):
        assert response.body == page and response.media_type == 'application/octet-stream'
        assert response.headers['Content-Security-Policy'] == 'sandbox'
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        assert response.headers['Content-Disposition'].startswith('attachment')


def test_a_sample_changed_in_more_fields_than_one_request_takes_still_syncs(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        corn = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 1')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)

        # 4x4 and 8x8 patch tiles are 80 fields on one sample: more than a vectors request takes.
        fields = [f'tile_{index:02d}' for index in range(followers.VECTOR_FIELDS + 6)]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
            "SELECT (SELECT dataset_id FROM samples WHERE id = $1), $1, name, '[1,0]' FROM unnest($2::text[]) name",
            corn,
            fields,
        )
        result = await sync(database, link_id)
        mirror = await mirror_of(database)
        assert mirror['last_error'] is None and not result.result['failed']
        [local] = (await copied(database, mirror)).values()
        held = await database.fetch('SELECT field_name FROM embeddings WHERE sample_id = $1 ORDER BY 1', local['id'])
        assert [row['field_name'] for row in held] == fields

    registry_database(scenario)


def test_coordinates_of_a_vector_added_to_a_mirrored_sample_arrive_with_it(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}], dataset_id=1, prefix='s3://images/a')
        corn = await database.fetchval('SELECT id FROM samples WHERE dataset_id = 1')
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)

        # One page of the feed: the sample's block (a tag), a vector it didn't have, and that vector's place on the
        # map. The block is applied before the vector is here, so its coordinates must be applied after.
        await database.execute("UPDATE samples SET tags = '{reviewed}' WHERE id = $1", corn)
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
            "SELECT dataset_id, id, 'clip', '[1,0]' FROM samples WHERE id = $1",
            corn,
        )
        await database.execute(
            "INSERT INTO embedding_coordinates (sample_id, field_name, x, y) VALUES ($1, 'clip', 3, 4)",
            corn,
        )
        await sync(database, link_id)
        mirror = await mirror_of(database)
        assert await coordinates_of(database, mirror['dataset_id']) == {corn: (3.0, 4.0)}

    registry_database(scenario)


def test_samples_that_swap_filepaths_keep_their_vectors_and_what_was_added_here(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/a')
        corn, soy = [
            row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = 1 ORDER BY id')
        ]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
            "SELECT dataset_id, id, 'clip', CASE id WHEN $1 THEN '[1,0]' ELSE '[0,1]' END::vector "
            "FROM samples WHERE id IN ($1, $2)",
            corn,
            soy,
        )
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=USER)
        await sync(database, link_id)
        mirror = await mirror_of(database)
        before = await copied(database, mirror)
        # A vector field of this instance's own, on the mirror's copy of corn.
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
            "SELECT dataset_id, id, 'mine', '[5,5]' FROM samples WHERE id = $1",
            before[corn]['id'],
        )

        # There, the two samples swap filepaths; their vectors don't change, so the feed names only their block.
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute("UPDATE samples SET filepath = 's3://images/swap' WHERE id = $1", corn)
            await connection.execute("UPDATE samples SET filepath = 's3://images/a-0.jpg' WHERE id = $1", soy)
            await connection.execute("UPDATE samples SET filepath = 's3://images/a-1.jpg' WHERE id = $1", corn)
        await sync(database, link_id)
        after = await copied(database, mirror)
        assert {origin: row['filepath'] for origin, row in after.items()} == {
            corn: 's3://images/a-1.jpg',
            soy: 's3://images/a-0.jpg',
        }
        assert {origin: row['id'] for origin, row in after.items()} == {
            origin: row['id'] for origin, row in before.items()
        }  # the same samples here, not new ones
        vectors = await database.fetch(
            'SELECT sample_id, field_name, vector::text FROM embeddings WHERE sample_id = ANY($1::int[]) ORDER BY 1, 2',
            [after[corn]['id'], after[soy]['id']],
        )
        assert [tuple(row) for row in vectors] == [
            (after[corn]['id'], 'clip', '[1,0]'),
            (after[corn]['id'], 'mine', '[5,5]'),
            (after[soy]['id'], 'clip', '[0,1]'),
        ]

    registry_database(scenario)


def test_a_long_copy_does_not_hold_up_the_other_mirrors_of_its_primary(monkeypatch, registry_database):
    async def scenario(database):
        _, link_id = await setup(monkeypatch, database)
        monkeypatch.setattr(follower_sync, 'COPY_BLOCKS', 1)  # each block in a step of its own
        # Two datasets of four blocks each on the primary.
        for dataset_id in (1, 2):
            for block in range(4):
                await database.execute(
                    "INSERT INTO samples (id, dataset_id, filepath, media_type) VALUES ($1, $2, $3, 'image')",
                    dataset_id * 100000 + block * follower_sync.BLOCK,
                    dataset_id,
                    f's3://images/{dataset_id}-{block}.jpg',
                )
        follower = await database.fetchval('SELECT id FROM followers')
        for dataset_id in (1, 2):
            await followers.follow_dataset(follower, dataset_id, followers.FollowOptions(), user=USER)

        # The sync job, step by step: when the later mirror has its first samples, the earlier one is still copying.
        step = follower_sync.handler(database.pool)
        checkpoint: dict = {}
        seen = None
        for _ in range(200):
            result = await step({'args': {'link_id': link_id}, 'checkpoint': checkpoint})
            if result.publish is not None:
                async with database.pool.acquire() as connection, connection.transaction():
                    await result.publish(connection)
            if result.checkpoint is not None:
                checkpoint = result.checkpoint
            mirrors = await database.fetch('SELECT dataset_id, state FROM mirrors ORDER BY dataset_id')
            if seen is None and len(mirrors) == 2:
                later = await database.fetchval(
                    'SELECT count(*) FROM mirror_samples WHERE dataset_id = $1',
                    mirrors[1]['dataset_id'],
                )
                if later:
                    seen = mirrors[0]['state']
            if result.finished:
                break
        assert seen == 'copying'
        assert [row['state'] for row in mirrors] == ['following', 'following']
        assert await database.fetchval('SELECT count(*) FROM mirror_samples') == 8

    registry_database(scenario)
