"""Follower instances. On the primary, admins add followers and choose the datasets each follows, with an option each
for tags and views (Settings → Followers). A follower reads what changed through the /follower endpoints, with the
token it was given; no other endpoint accepts that token, and it accepts nothing else.

The change log (migration 0007) holds what changed in each followed dataset, each row stamped with the writing
transaction:
- the sample blocks (id // 4096) that changed: a follower re-reads such a block and compares it with its copy;
- the vector and hash fields per sample;
- dataset state.

The feed serves only transactions below the current snapshot's xmin. All of those have ended, so nothing can
later appear behind a cursor it has handed out.

A new follower of a dataset gets a start transaction once its subscription has committed. Writes from at or after
it are logged. It copies the dataset (blocks, then their samples and vectors) once the feed reports `ready`: by
then every transaction that might have written without seeing the dataset become followed has ended. After the
copy it reads the feed from start_txid - 1.
"""

import base64
import secrets
from typing import List, Literal

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..crypto import hash_token
from ..database import db
from ..models import ViewCreate
from . import datasets as datasets_router
from . import views as views_router
from .auth import get_follower_user, require_role

router = APIRouter()

BLOCK = 4096
STALE_DAYS = 7
FEED_TRANSACTIONS = 1000  # whole transactions per feed page
SAMPLE_BLOCKS = 16  # blocks per samples request
VECTOR_SAMPLES = 2000  # samples per vectors request
TAG_SAMPLES = 50000  # samples per tags request from a follower
# The change log's tables, each with dataset_id and transaction_id.
LOG_TABLES = ('sample_block_changes', 'vector_changes', 'dataset_changes', 'map_block_changes')


class FollowerCreate(BaseModel):
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,62}$')


class FollowOptions(BaseModel):
    tags: Literal['primary', 'both'] = 'primary'
    views: Literal['primary', 'both'] = 'primary'


class BlockRequest(BaseModel):
    blocks: List[int] = Field(min_length=1, max_length=SAMPLE_BLOCKS)


class VectorRequest(BaseModel):
    sample_ids: List[int] = Field(min_length=1, max_length=VECTOR_SAMPLES)
    fields: List[str] = Field(default_factory=list, max_length=64)


class Ack(BaseModel):
    cursor: int


class TagRequest(BaseModel):
    sample_ids: List[int] = Field(min_length=1, max_length=TAG_SAMPLES)
    field_name: str = 'tags'
    add_tags: List[str] = Field(default_factory=list)
    remove_tags: List[str] = Field(default_factory=list)


class ViewRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: dict


async def identity(source=None) -> str:
    """The primary's identity in a cursor: its cluster's system identifier, new after a restore into a new database."""
    return str(await (source or db).fetchval('SELECT system_identifier FROM pg_control_system()'))


async def prune(source, dataset_id: int) -> None:
    """Delete the dataset's log rows every follower of it has acknowledged; all of them when nobody follows it."""
    floor = await source.fetchrow(
        """SELECT count(*) AS followers,
                  min(COALESCE(acked_txid, start_txid - 1)) AS floor,
                  bool_or(start_txid IS NULL) AS starting
           FROM follower_datasets WHERE dataset_id = $1""",
        dataset_id,
    )
    if floor['followers'] and (floor['starting'] or floor['floor'] is None):
        return
    for table in LOG_TABLES:
        if floor['followers']:
            await source.execute(
                f'DELETE FROM {table} WHERE dataset_id = $1 AND transaction_id <= $2',
                dataset_id,
                floor['floor'],
            )
        else:
            await source.execute(f'DELETE FROM {table} WHERE dataset_id = $1', dataset_id)


# --- Admin: Settings → Followers -------------------------------------------------------------------------------


@router.post('/followers', status_code=201)
async def create_follower(data: FollowerCreate, user: dict = Depends(require_role('admin'))):
    """Add a follower: its account (follower:<name>, no password, so no login) and its token, returned only here."""
    raw = secrets.token_urlsafe(32)
    async with db.pool.acquire() as connection, connection.transaction():
        if await connection.fetchval('SELECT 1 FROM followers WHERE name = $1', data.name):
            raise HTTPException(status_code=409, detail=f'A follower named {data.name} already exists')
        if await connection.fetchval('SELECT 1 FROM users WHERE username = $1', f'follower:{data.name}'):
            raise HTTPException(status_code=409, detail=f'An account named follower:{data.name} already exists')
        account = await connection.fetchval(
            """INSERT INTO users (username, role, password_hash, must_change_password, is_active)
               VALUES ($1, 'power_user', NULL, FALSE, TRUE) RETURNING id""",
            f'follower:{data.name}',
        )
        token_id = await connection.fetchval(
            "INSERT INTO api_tokens (user_id, token, name, expires_at, scope) "
            "VALUES ($1, $2, $3, NULL, 'follower') RETURNING id",
            account,
            hash_token(raw),
            f'follower {data.name}',
        )
        row = await connection.fetchrow(
            'INSERT INTO followers (name, user_id, token_id, created_by) VALUES ($1, $2, $3, $4) RETURNING *',
            data.name,
            account,
            token_id,
            user['id'],
        )
    return {'id': row['id'], 'name': row['name'], 'created_at': row['created_at'], 'token': raw}


@router.get('/followers')
async def list_followers(user: dict = Depends(require_role('admin'))):
    """Every follower with its datasets and options, when it last checked in (stale after STALE_DAYS) and the log
    rows the primary keeps for it."""
    followers = await db.fetch(
        f"""SELECT id, name, created_at, last_seen_at,
                   last_seen_at IS NULL OR last_seen_at < now() - interval '{STALE_DAYS} days' AS stale
            FROM followers ORDER BY name""",
    )
    held = ' + '.join(
        f'(SELECT count(*) FROM {table} c WHERE c.dataset_id = f.dataset_id '
        'AND c.transaction_id > COALESCE(f.acked_txid, f.start_txid - 1, 0))'
        for table in LOG_TABLES
    )
    datasets = await db.fetch(
        f"""SELECT f.follower_id, f.dataset_id, d.name, f.tags, f.views, f.start_txid, f.acked_txid, f.subscribed_at,
                   {held} AS held_rows
            FROM follower_datasets f JOIN datasets d ON d.id = f.dataset_id ORDER BY d.name""",
    )
    result = []
    for follower in followers:
        entry = dict(follower)
        entry['datasets'] = [
            {key: row[key] for key in row.keys() if key != 'follower_id'}
            for row in datasets
            if row['follower_id'] == follower['id']
        ]
        result.append(entry)
    return result


@router.put('/followers/{follower_id}/datasets/{dataset_id}')
async def follow_dataset(
    follower_id: int,
    dataset_id: int,
    options: FollowOptions,
    user: dict = Depends(require_role('admin')),
):
    """Follow a dataset, or change its options. A new subscription gets its start transaction just after it commits:
    from then on the dataset is followed, and only writes that started before may have missed the flag."""
    async with db.pool.acquire() as connection:
        async with connection.transaction():
            if not await connection.fetchval('SELECT 1 FROM followers WHERE id = $1', follower_id):
                raise HTTPException(status_code=404, detail='No such follower')
            # Serializes with unfollowing and removal, which recompute datasets.followed from the subscriptions.
            if not await connection.fetchval(
                'SELECT 1 FROM datasets WHERE id = $1 AND NOT deleting FOR NO KEY UPDATE',
                dataset_id,
            ):
                raise HTTPException(status_code=404, detail='No such dataset')
            # No chains: a mirror is its primary's copy, and an instance that wants it follows the primary.
            if await connection.fetchval('SELECT 1 FROM mirrors WHERE dataset_id = $1', dataset_id):
                raise HTTPException(
                    status_code=409,
                    detail="A mirror can't be followed: follow the dataset on its primary instead",
                )
            await connection.execute(
                """INSERT INTO follower_datasets (follower_id, dataset_id, tags, views) VALUES ($1, $2, $3, $4)
                   ON CONFLICT (follower_id, dataset_id) DO UPDATE SET tags = EXCLUDED.tags, views = EXCLUDED.views""",
                follower_id,
                dataset_id,
                options.tags,
                options.views,
            )
        # A new transaction, after the subscription is visible: every transaction that could have missed it has an
        # id below this snapshot's xmax.
        row = await connection.fetchrow(
            """UPDATE follower_datasets
               SET start_txid = COALESCE(start_txid, txid_snapshot_xmax(txid_current_snapshot()))
               WHERE follower_id = $1 AND dataset_id = $2 RETURNING *""",
            follower_id,
            dataset_id,
        )
    return dict(row)


@router.delete('/followers/{follower_id}/datasets/{dataset_id}')
async def unfollow_dataset(follower_id: int, dataset_id: int, user: dict = Depends(require_role('admin'))):
    async with db.pool.acquire() as connection, connection.transaction():
        await connection.execute('SELECT 1 FROM datasets WHERE id = $1 FOR NO KEY UPDATE', dataset_id)
        deleted = await connection.fetchval(
            'DELETE FROM follower_datasets WHERE follower_id = $1 AND dataset_id = $2 RETURNING 1',
            follower_id,
            dataset_id,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail='The follower does not follow that dataset')
        await prune(connection, dataset_id)
    return {'unfollowed': dataset_id}


@router.delete('/followers/{follower_id}')
async def remove_follower(follower_id: int, user: dict = Depends(require_role('admin'))):
    """Remove a follower: everything the primary keeps for it. Its token stops working, its log rows go, and a
    dataset nobody else follows stops logging. Its account stays, renamed and inactive, for what it wrote."""
    async with db.pool.acquire() as connection, connection.transaction():
        follower = await connection.fetchrow('SELECT * FROM followers WHERE id = $1 FOR UPDATE', follower_id)
        if follower is None:
            raise HTTPException(status_code=404, detail='No such follower')
        datasets = [
            row['dataset_id']
            for row in await connection.fetch(
                'SELECT dataset_id FROM follower_datasets WHERE follower_id = $1',
                follower_id,
            )
        ]
        await connection.execute(
            'SELECT 1 FROM datasets WHERE id = ANY($1::int[]) ORDER BY id FOR NO KEY UPDATE',
            datasets,
        )
        await connection.execute('DELETE FROM followers WHERE id = $1', follower_id)
        await connection.execute('UPDATE api_tokens SET is_active = FALSE WHERE user_id = $1', follower['user_id'])
        await connection.execute(
            'UPDATE users SET is_active = FALSE, username = username || $2 WHERE id = $1',
            follower['user_id'],
            f':removed:{follower_id}',
        )
        for dataset_id in datasets:
            await prune(connection, dataset_id)
    return {'removed': follower['name']}


# --- The follower's own endpoints ------------------------------------------------------------------------------


async def current_follower(account: dict = Depends(get_follower_user)) -> dict:
    """The follower behind the token, stamped as seen now."""
    row = await db.fetchrow('UPDATE followers SET last_seen_at = now() WHERE user_id = $1 RETURNING *', account['id'])
    if row is None:
        raise HTTPException(status_code=403, detail='This follower has been removed on the primary')
    return dict(row)


async def subscription(source, follower: dict, dataset_id: int) -> dict:
    row = await source.fetchrow(
        'SELECT * FROM follower_datasets WHERE follower_id = $1 AND dataset_id = $2',
        follower['id'],
        dataset_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail='This follower does not follow that dataset')
    return dict(row)


@router.get('/follower/config')
async def follower_config(follower: dict = Depends(current_follower)):
    """What this follower follows and with which options. A dataset is `ready` to copy once every transaction that
    might have written to it without logging has ended."""
    xmin = await db.fetchval('SELECT txid_snapshot_xmin(txid_current_snapshot())')
    rows = await db.fetch(
        """SELECT f.dataset_id, d.name, f.tags, f.views, f.start_txid, f.acked_txid
           FROM follower_datasets f JOIN datasets d ON d.id = f.dataset_id WHERE f.follower_id = $1 ORDER BY d.name""",
        follower['id'],
    )
    return {
        'name': follower['name'],
        'identity': await identity(),
        'datasets': [
            {**dict(row), 'ready': row['start_txid'] is not None and xmin >= row['start_txid']} for row in rows
        ],
    }


@router.get('/follower/datasets/{dataset_id}/blocks')
async def follower_blocks(dataset_id: int, follower: dict = Depends(current_follower)):
    """Every block of the dataset with its sample count: the plan of a full copy."""
    await subscription(db, follower, dataset_id)
    rows = await db.fetch(
        f'SELECT id / {BLOCK} AS block, count(*) AS samples FROM samples WHERE dataset_id = $1 GROUP BY 1 ORDER BY 1',
        dataset_id,
    )
    return {'block_size': BLOCK, 'blocks': [dict(row) for row in rows]}


@router.get('/follower/datasets/{dataset_id}/changes')
async def follower_changes(
    dataset_id: int,
    after: int,
    identity_: str = Query(alias='identity'),
    transactions: int = Query(FEED_TRANSACTIONS, ge=1, le=FEED_TRANSACTIONS),
    follower: dict = Depends(current_follower),
):
    """What changed in transactions after `after` and below the snapshot's xmin, whole transactions only, at most
    `transactions` of them. The cursor returned is the next `after`. 410 when the primary is no longer the one the
    cursor came from (restored into a new database): copy again."""
    async with db.pool.acquire() as connection, connection.transaction(isolation='repeatable_read'):
        if identity_ != await identity(connection):
            raise HTTPException(status_code=410, detail='The primary was restored or replaced: copy the dataset again')
        sub = await subscription(connection, follower, dataset_id)
        if sub['start_txid'] is None or after < sub['start_txid'] - 1:
            raise HTTPException(
                status_code=409,
                detail='Copy the dataset first, then read the feed from start_txid - 1',
            )
        xmin = await connection.fetchval('SELECT txid_snapshot_xmin(txid_current_snapshot())')
        logged = ' UNION '.join(
            f'SELECT transaction_id FROM {table} WHERE dataset_id = $1 AND transaction_id > $2 AND transaction_id < $3'
            for table in LOG_TABLES
        )
        found = await connection.fetch(
            f'SELECT transaction_id FROM ({logged}) t ORDER BY 1 LIMIT $4',
            dataset_id,
            after,
            xmin,
            transactions + 1,
        )
        more = len(found) > transactions
        upto = found[transactions - 1]['transaction_id'] if more else max(after, xmin - 1)
        blocks = await connection.fetch(
            'SELECT DISTINCT block_id FROM sample_block_changes '
            'WHERE dataset_id = $1 AND transaction_id > $2 AND transaction_id <= $3 ORDER BY 1',
            dataset_id,
            after,
            upto,
        )
        vectors = await connection.fetch(
            """SELECT DISTINCT sample_id, kind, field_name FROM vector_changes
               WHERE dataset_id = $1 AND transaction_id > $2 AND transaction_id <= $3 ORDER BY 1, 2, 3""",
            dataset_id,
            after,
            upto,
        )
        maps = await connection.fetch(
            'SELECT DISTINCT field_name, block_id FROM map_block_changes '
            'WHERE dataset_id = $1 AND transaction_id > $2 AND transaction_id <= $3 ORDER BY 1, 2',
            dataset_id,
            after,
            upto,
        )
        state = await connection.fetch(
            'SELECT DISTINCT kind FROM dataset_changes '
            'WHERE dataset_id = $1 AND transaction_id > $2 AND transaction_id <= $3 ORDER BY 1',
            dataset_id,
            after,
            upto,
        )
    return {
        'cursor': upto,
        'more': more,
        'blocks': [row['block_id'] for row in blocks],
        'vectors': [dict(row) for row in vectors],
        'maps': [{'field_name': row['field_name'], 'block': row['block_id']} for row in maps],
        'dataset': [row['kind'] for row in state],
    }


@router.post('/follower/datasets/{dataset_id}/blocks/samples')
async def follower_block_samples(dataset_id: int, request: BlockRequest, follower: dict = Depends(current_follower)):
    """The current samples of those blocks, with every column a mirror needs and their hashes. A sample the follower
    holds in one of these blocks but that isn't here has been deleted."""
    await subscription(db, follower, dataset_id)
    arrays = [
        row['field_name']
        for row in await db.fetch(
            "SELECT field_name FROM dataset_fields "
            "WHERE dataset_id = $1 AND origin = 'native_array' AND field_name <> 'tags'",
            dataset_id,
        )
    ]
    array_columns = ''.join(f', s."{name}"' for name in arrays)
    samples = await db.fetch(
        f"""SELECT s.id, s.filepath, s.media_type, s.tags, s.metadata, s.lon, s.lat, s.heading, s.created_at,
                   s.last_modified_at{array_columns}
            FROM unnest($2::bigint[]) b JOIN samples s ON s.id >= b * {BLOCK} AND s.id < (b + 1) * {BLOCK}
            WHERE s.dataset_id = $1 ORDER BY s.id""",
        dataset_id,
        request.blocks,
    )
    hashes = await db.fetch(
        'SELECT sample_id, field_name, hash_hex FROM sample_hashes WHERE sample_id = ANY($1::int[]) ORDER BY 1, 2',
        [row['id'] for row in samples],
    )
    return {
        'blocks': request.blocks,
        'array_fields': arrays,
        'samples': [dict(row) for row in samples],
        'hashes': [dict(row) for row in hashes],
    }


@router.post('/follower/datasets/{dataset_id}/vectors')
async def follower_vectors(dataset_id: int, request: VectorRequest, follower: dict = Depends(current_follower)):
    """Those samples' vectors (every field, or the ones named) as base64 float32, little-endian. A (sample, field)
    the feed named but that isn't here has been deleted."""
    await subscription(db, follower, dataset_id)
    rows = await db.fetch(
        """SELECT e.sample_id, e.field_name, e.vector::real[] AS vector
           FROM embeddings e JOIN samples s ON s.id = e.sample_id
           WHERE s.dataset_id = $1 AND e.sample_id = ANY($2::int[])
             AND (cardinality($3::text[]) = 0 OR e.field_name = ANY($3))
           ORDER BY 1, 2""",
        dataset_id,
        request.sample_ids,
        request.fields,
    )
    return {
        'vectors': [
            {
                'sample_id': row['sample_id'],
                'field_name': row['field_name'],
                'dimension': len(row['vector']),
                'vector': base64.b64encode(np.asarray(row['vector'], dtype='<f4').tobytes()).decode(),
            }
            for row in rows
        ],
    }


@router.post('/follower/datasets/{dataset_id}/coordinates')
async def follower_coordinates(dataset_id: int, request: BlockRequest, follower: dict = Depends(current_follower)):
    """The map coordinates of those blocks' samples, every field. A (sample, field) a mirror holds in one of these
    blocks but that isn't here has no coordinates any more."""
    await subscription(db, follower, dataset_id)
    rows = await db.fetch(
        f"""SELECT c.sample_id, c.field_name, c.x, c.y
            FROM unnest($2::bigint[]) b
            JOIN samples s ON s.id >= b * {BLOCK} AND s.id < (b + 1) * {BLOCK} AND s.dataset_id = $1
            JOIN embedding_coordinates c ON c.sample_id = s.id
            ORDER BY 1, 2""",
        dataset_id,
        request.blocks,
    )
    return {'blocks': request.blocks, 'coordinates': [dict(row) for row in rows]}


@router.get('/follower/datasets/{dataset_id}/state')
async def follower_state(dataset_id: int, follower: dict = Depends(current_follower)):
    """The dataset's settings, field registry and shared views, for a mirror."""
    await subscription(db, follower, dataset_id)
    dataset = await db.fetchrow(
        """SELECT id, name, description, media_fields, grid_media_field, modal_media_field, segmentation_config,
                  default_view_id FROM datasets WHERE id = $1""",
        dataset_id,
    )
    fields = await db.fetch(
        """SELECT field_name, field_type, origin, is_promoted, display_type, overlay_config
           FROM dataset_fields WHERE dataset_id = $1 ORDER BY field_name""",
        dataset_id,
    )
    views = await db.fetch(
        'SELECT id, name, slug, content FROM views WHERE dataset_id = $1 AND is_public ORDER BY id',
        dataset_id,
    )
    # The ranked maps: rank_stale is cleared only by a full re-rank, which logs a 'maps' change.
    maps = await db.fetch(
        'SELECT field_name, rank_stale FROM embedding_viz_meta WHERE dataset_id = $1 ORDER BY field_name',
        dataset_id,
    )
    return {
        'dataset': dict(dataset),
        'fields': [dict(row) for row in fields],
        'views': [dict(row) for row in views],
        'maps': [dict(row) for row in maps],
    }


@router.post('/follower/datasets/{dataset_id}/ack')
async def follower_ack(dataset_id: int, ack: Ack, follower: dict = Depends(current_follower)):
    """The follower has applied everything up to `cursor`: the log below every follower's acknowledgement is pruned."""
    async with db.pool.acquire() as connection, connection.transaction():
        await subscription(connection, follower, dataset_id)
        xmin = await connection.fetchval('SELECT txid_snapshot_xmin(txid_current_snapshot())')
        if ack.cursor >= xmin:
            raise HTTPException(status_code=400, detail='That cursor is ahead of anything the feed has served')
        await connection.execute(
            'UPDATE follower_datasets SET acked_txid = GREATEST(COALESCE(acked_txid, 0), $3) '
            'WHERE follower_id = $1 AND dataset_id = $2',
            follower['id'],
            dataset_id,
            ack.cursor,
        )
        await prune(connection, dataset_id)
    return {'acked': ack.cursor}


# --- Edits a follower sends up (its tags and views options both ways) ---------------------------------------------


async def both_ways(follower: dict, dataset_id: int, option: str) -> dict:
    """The follower's account, as which its edits are made here, once the dataset's option allows them."""
    sub = await subscription(db, follower, dataset_id)
    if sub[option] != 'both':
        raise HTTPException(
            status_code=403,
            detail=f'The {option} of this dataset come from the primary only: this follower cannot change them',
        )
    return dict(await db.fetchrow('SELECT id, username, role FROM users WHERE id = $1', follower['user_id']))


@router.post('/follower/datasets/{dataset_id}/tags')
async def follower_tags(dataset_id: int, request: TagRequest, follower: dict = Depends(current_follower)):
    """Tags edited on the follower: made here as its account, and back to it through the feed. Samples deleted here
    since the follower copied them are skipped. A large edit is queued as a bulk job here (202)."""
    account = await both_ways(follower, dataset_id, 'tags')
    present = [
        row['id']
        for row in await db.fetch(
            'SELECT id FROM samples WHERE dataset_id = $1 AND id = ANY($2::int[])',
            dataset_id,
            request.sample_ids,
        )
    ]
    skipped = len(set(request.sample_ids)) - len(present)
    if not present:
        return {'updated_count': 0, 'skipped': skipped}
    result = await datasets_router.bulk_tag_samples(
        dataset_id,
        datasets_router.BulkTagRequest(
            sample_ids=present,
            field_name=request.field_name,
            add_tags=request.add_tags,
            remove_tags=request.remove_tags,
        ),
        user=account,
    )
    if isinstance(result, JSONResponse):
        return result
    return {**result, 'skipped': skipped}


@router.post('/follower/datasets/{dataset_id}/views', status_code=201)
async def follower_create_view(dataset_id: int, request: ViewRequest, follower: dict = Depends(current_follower)):
    """A shared view made on the follower, created here as its account and marked with where it was made."""
    account = await both_ways(follower, dataset_id, 'views')
    content = {**request.content, 'made_on': follower['name']}
    return await views_router.create_view(
        dataset_id,
        ViewCreate(name=request.name, content=content, is_public=True),
        user=account,
    )


@router.delete('/follower/datasets/{dataset_id}/views/{view_id}')
async def follower_delete_view(dataset_id: int, view_id: int, follower: dict = Depends(current_follower)):
    """A shared view deleted on the follower. As anywhere, its account deletes only the views it made."""
    account = await both_ways(follower, dataset_id, 'views')
    shared = await db.fetchval(
        'SELECT 1 FROM views WHERE id = $1 AND dataset_id = $2 AND is_public',
        view_id,
        dataset_id,
    )
    if not shared:
        raise HTTPException(status_code=404, detail='No such shared view in this dataset')
    return await views_router.delete_view(view_id, user=account)
