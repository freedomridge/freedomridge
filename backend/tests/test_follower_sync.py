"""The follower's sync (follower_sync.py) against a real primary: one test database plays both parts. Dataset 1 is
the primary's; the sync copies it into a mirror beside it through the real follower endpoints (an in-process HTTP
client bound to the app), then applies the primary's feed to it."""

import os
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from fastapi import HTTPException

from backend import follower_sync
from backend.crypto import decrypt_secret, encrypt_secret
from backend.routers import auth, datasets, followers, primaries
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
