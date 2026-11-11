"""Followers on the primary (routers/followers.py): an admin adds a follower and the datasets it follows; the
follower's token reaches only the follower endpoints; the feed serves committed changes in transaction order, never
behind a cursor it has handed out, and the log is pruned below every follower's acknowledgement."""

import base64
import os

import numpy as np
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.routers import auth, followers
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
ingest, USER = registry_helpers.ingest, registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


def bearer(token):
    return Request({'type': 'http', 'headers': [(b'authorization', f'Bearer {token}'.encode())]})


async def setup(monkeypatch, database):
    monkeypatch.setattr(followers, 'db', database)
    monkeypatch.setattr(auth, 'db', database)
    created = await followers.create_follower(followers.FollowerCreate(name='lab'), user=USER)
    follower = dict(await database.fetchrow('SELECT * FROM followers WHERE id = $1', created['id']))
    return created, follower


async def feed(follower, after, dataset_id=1):
    return await followers.follower_changes(
        dataset_id,
        after=after,
        identity_=await followers.identity(),
        transactions=followers.FEED_TRANSACTIONS,
        follower=follower,
    )


def test_the_follower_token_reaches_only_the_follower_endpoints(monkeypatch, registry_database):
    async def scenario(database):
        created, follower = await setup(monkeypatch, database)
        account = await auth.get_follower_user(bearer(created['token']))
        assert account['username'] == 'follower:lab' and account['id'] == follower['user_id']
        with pytest.raises(HTTPException) as refused:
            await auth.get_current_user(bearer(created['token']))
        assert refused.value.status_code == 403 and 'follower token' in refused.value.detail
        # The account can't log in (no password), and a follower's name is taken once.
        assert await database.fetchval('SELECT password_hash FROM users WHERE id = $1', follower['user_id']) is None
        with pytest.raises(HTTPException) as taken:
            await followers.create_follower(followers.FollowerCreate(name='lab'), user=USER)
        assert taken.value.status_code == 409

    registry_database(scenario)


def test_the_feed_serves_blocks_vectors_and_state_and_acknowledgements_prune(monkeypatch, registry_database):
    async def scenario(database):
        _, follower = await setup(monkeypatch, database)
        subscribed = await followers.follow_dataset(follower['id'], 1, followers.FollowOptions(tags='both'), user=USER)
        assert subscribed['tags'] == 'both' and subscribed['start_txid'] is not None
        assert await database.fetchval('SELECT followed FROM datasets WHERE id = 1') is True
        assert await database.fetchval('SELECT followed FROM datasets WHERE id = 2') is False
        config = await followers.follower_config(follower=follower)
        assert [(entry['dataset_id'], entry['ready']) for entry in config['datasets']] == [(1, True)]
        start = subscribed['start_txid'] - 1

        await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/a')
        ids = [row['id'] for row in await database.fetch('SELECT id FROM samples WHERE dataset_id = 1 ORDER BY id')]
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES ("
            "(SELECT dataset_id FROM samples WHERE id = $1), $1, 'clip', '[0.5,0.25]')",
            ids[0],
        )
        page = await feed(follower, start)
        block = ids[0] // followers.BLOCK
        assert page['blocks'] == [block] and page['more'] is False
        assert page['vectors'] == [{'sample_id': ids[0], 'kind': 'vector', 'field_name': 'clip'}]
        assert page['dataset'] == ['fields']  # the ingest registered the crop field
        assert (await feed(follower, page['cursor']))['blocks'] == []

        listed = await followers.follower_blocks(1, follower=follower)
        assert listed['blocks'] == [{'block': block, 'samples': 2}]
        samples = await followers.follower_block_samples(1, followers.BlockRequest(blocks=[block]), follower=follower)
        assert [sample['id'] for sample in samples['samples']] == ids
        vectors = await followers.follower_vectors(1, followers.VectorRequest(sample_ids=ids), follower=follower)
        vector = vectors['vectors'][0]
        decoded = np.frombuffer(base64.b64decode(vector['vector']), dtype='<f4')
        assert (vector['sample_id'], vector['field_name'], decoded.tolist()) == (ids[0], 'clip', [0.5, 0.25])
        state = await followers.follower_state(1, follower=follower)
        assert state['dataset']['id'] == 1 and 'crop' in [field['field_name'] for field in state['fields']]

        # A deleted sample's block comes round again, and the block no longer holds it.
        cursor = page['cursor']
        await database.execute('DELETE FROM samples WHERE id = $1', ids[1])
        page = await feed(follower, cursor)
        assert page['blocks'] == [block]
        samples = await followers.follower_block_samples(1, followers.BlockRequest(blocks=[block]), follower=follower)
        assert [sample['id'] for sample in samples['samples']] == [ids[0]]

        # Acknowledging prunes what the follower has applied, and the admin list shows what is kept.
        held = (await followers.list_followers(user=USER))[0]['datasets'][0]['held_rows']
        assert held > 0
        await followers.follower_ack(1, followers.Ack(cursor=page['cursor']), follower=follower)
        assert await database.fetchval('SELECT count(*) FROM sample_block_changes WHERE dataset_id = 1') == 0
        assert (await followers.list_followers(user=USER))[0]['datasets'][0]['held_rows'] == 0

    registry_database(scenario)


def test_a_transaction_that_commits_late_is_still_served(monkeypatch, registry_database):
    async def scenario(database):
        _, follower = await setup(monkeypatch, database)
        subscribed = await followers.follow_dataset(follower['id'], 1, followers.FollowOptions(), user=USER)
        cursor = subscribed['start_txid'] - 1
        early = await database.pool.acquire()
        try:
            transaction = early.transaction()
            await transaction.start()
            # The lower transaction id writes first and stays open.
            await early.execute(
                "INSERT INTO samples (dataset_id, filepath, media_type) VALUES (1, 's3://i/early.jpg', 'image')",
            )
            # A later transaction writes a sample in another block and commits.
            later = await database.fetchval(
                "INSERT INTO samples (id, dataset_id, filepath, media_type) "
                "VALUES (50000, 1, 's3://i/late.jpg', 'image') RETURNING id",
            )
            page = await feed(follower, cursor)
            assert page['blocks'] == []  # the later commit is above the open transaction: not served yet
            assert page['cursor'] < await early.fetchval('SELECT txid_current()')
            cursor = page['cursor']
            await transaction.commit()
        finally:
            await database.pool.release(early)
        early_id = await database.fetchval("SELECT id FROM samples WHERE filepath = 's3://i/early.jpg'")
        page = await feed(follower, cursor)
        assert page['blocks'] == sorted({early_id // followers.BLOCK, later // followers.BLOCK})

    registry_database(scenario)


def test_ready_waits_for_writes_already_in_flight(monkeypatch, registry_database):
    async def scenario(database):
        _, follower = await setup(monkeypatch, database)
        writer = await database.pool.acquire()
        try:
            transaction = writer.transaction()
            await transaction.start()
            await writer.execute(
                "INSERT INTO samples (dataset_id, filepath, media_type) VALUES (1, 's3://i/a.jpg', 'image')",
            )
            await followers.follow_dataset(follower['id'], 1, followers.FollowOptions(), user=USER)
            # That write started before the dataset was followed, so it isn't logged: no copy may start before it ends.
            assert (await followers.follower_config(follower=follower))['datasets'][0]['ready'] is False
            await transaction.commit()
        finally:
            await database.pool.release(writer)
        assert (await followers.follower_config(follower=follower))['datasets'][0]['ready'] is True

    registry_database(scenario)


def test_identity_order_and_subscription_checks(monkeypatch, registry_database):
    async def scenario(database):
        _, follower = await setup(monkeypatch, database)
        subscribed = await followers.follow_dataset(follower['id'], 1, followers.FollowOptions(), user=USER)

        async def status(call):
            with pytest.raises(HTTPException) as refused:
                await call
            return refused.value.status_code

        start = subscribed['start_txid']
        assert (
            await status(
                followers.follower_changes(1, after=start - 1, identity_='another', transactions=10, follower=follower),
            )
            == 410
        )
        assert await status(feed(follower, start - 5)) == 409  # before its copy
        assert await status(feed(follower, start, dataset_id=2)) == 404  # not followed
        assert await status(followers.follower_ack(1, followers.Ack(cursor=start + 10**9), follower=follower)) == 400

    registry_database(scenario)


def test_unfollowing_and_removing_a_follower_leave_nothing_behind(monkeypatch, registry_database):
    async def scenario(database):
        created, follower = await setup(monkeypatch, database)
        await followers.follow_dataset(follower['id'], 1, followers.FollowOptions(), user=USER)
        await followers.follow_dataset(follower['id'], 2, followers.FollowOptions(views='both'), user=USER)
        await ingest([{}], dataset_id=1, prefix='s3://images/a')
        await ingest([{}], dataset_id=2, prefix='s3://images/b')
        assert await database.fetchval('SELECT count(*) FROM sample_block_changes') == 2

        await followers.unfollow_dataset(follower['id'], 2, user=USER)
        assert await database.fetchval('SELECT followed FROM datasets WHERE id = 2') is False
        assert await database.fetchval('SELECT count(*) FROM sample_block_changes WHERE dataset_id = 2') == 0

        await followers.remove_follower(follower['id'], user=USER)
        assert await database.fetchval('SELECT followed FROM datasets WHERE id = 1') is False
        assert await database.fetchval('SELECT count(*) FROM sample_block_changes') == 0
        account = await database.fetchrow('SELECT username, is_active FROM users WHERE id = $1', follower['user_id'])
        assert (account['username'], account['is_active']) == (f'follower:lab:removed:{follower["id"]}', False)
        with pytest.raises(HTTPException) as gone:
            await auth.get_follower_user(bearer(created['token']))
        assert gone.value.status_code == 401
        # The name is free again.
        again = await followers.create_follower(followers.FollowerCreate(name='lab'), user=USER)
        assert again['name'] == 'lab'

    registry_database(scenario)
