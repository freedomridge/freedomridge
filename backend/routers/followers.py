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
import uuid
from typing import Annotated, List, Literal, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .. import follower_search
from ..api_errors import CodedHTTPException
from ..crypto import hash_token
from ..database import db
from ..models import ViewCreate
from ..sample_ids import ranges_on_ids
from . import datasets as datasets_router
from . import views as views_router
from .auth import get_follower_user, require_role

router = APIRouter()

BLOCK = 4096
STALE_DAYS = 7
FEED_TRANSACTIONS = 1000  # whole transactions per feed page
FEED_VECTOR_ROWS = 20000  # vector changes per page a follower asks for (vector_limit), in whole transactions
SAMPLE_BLOCKS = 16  # blocks per samples request
VECTOR_SAMPLES = 2000  # samples per vectors request
VECTOR_FIELDS = 64  # fields per vectors request
TAG_SAMPLES = 50000  # samples per tags request from a follower
# The follower API's version, in /follower/config. 2 adds the follower's reports of its own patch scales.
PROTOCOL = 2
REPORTED_SCALES = 100  # patch scales per report (base fields × scales)
# The change log's tables, each with dataset_id and transaction_id.
LOG_TABLES = ('sample_block_changes', 'vector_changes', 'dataset_changes', 'map_block_changes')


class FollowerCreate(BaseModel):
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,62}$')


class FollowOptions(BaseModel):
    tags: Literal['primary', 'both'] = 'primary'
    views: Literal['primary', 'both'] = 'primary'
    # Serve the dataset's images, thumbnails and overlays to the follower, for one that can't read them itself:
    # files on this server's disk, or a bucket it has no credentials for.
    images: bool = False


class BlockRequest(BaseModel):
    blocks: List[int] = Field(min_length=1, max_length=SAMPLE_BLOCKS)


class VectorRequest(BaseModel):
    sample_ids: List[int] = Field(min_length=1, max_length=VECTOR_SAMPLES)
    fields: List[str] = Field(default_factory=list, max_length=VECTOR_FIELDS)


class Ack(BaseModel):
    cursor: int


def clean_made_by(value: Optional[str]) -> Optional[str]:
    """Who made an edit on the follower: shown with it here, never used for access. Printable (no control or format
    characters, so no bidi overrides) and at most 100 characters; blank is none."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    if len(value) > 100 or not value.isprintable():
        raise ValueError('made_by must be at most 100 printable characters')
    return value


class MadeBy(BaseModel):
    # The username of whoever made the edit on the follower (an older follower sends none).
    made_by: Optional[str] = None

    @field_validator('made_by')
    @classmethod
    def printable(cls, value: Optional[str]) -> Optional[str]:
        return clean_made_by(value)


class TagRequest(MadeBy):
    sample_ids: List[int] = Field(min_length=1, max_length=TAG_SAMPLES)
    # The tags option covers tags only: no other array field of the primary's.
    field_name: Literal['tags'] = 'tags'
    add_tags: List[str] = Field(default_factory=list)
    remove_tags: List[str] = Field(default_factory=list)


class ViewRequest(MadeBy):
    name: str = Field(min_length=1, max_length=200)
    content: dict


class ReportedScale(BaseModel):
    base_field: str = Field(pattern=r'^[a-zA-Z0-9_]{1,100}$')
    scale: str = Field(pattern=r'^[1-9][0-9]?x[1-9][0-9]?$')
    dims: int = Field(ge=1, le=16000)
    indexed: bool


class FieldReport(BaseModel):
    scales: List[ReportedScale] = Field(max_length=REPORTED_SCALES)


class RelayedResult(BaseModel):
    origin_id: int
    field: str = Field(pattern=r'^[a-zA-Z0-9_]{1,200}$')
    distance: float


class SearchAnswer(BaseModel):
    results: Optional[List[RelayedResult]] = Field(None, max_length=10000)
    error: Optional[str] = Field(None, max_length=1000)


async def identity(source=None) -> str:
    """The primary's identity in a cursor: its cluster's system identifier and its database's oid. A restore into a
    new cluster changes the first, and one with dropdb and createdb (docs/DEPLOY_TO_AWS.md) the second. (A value
    stored in a table wouldn't do: a dump carries it.)"""
    return await (source or db).fetchval(
        """SELECT (SELECT system_identifier::text FROM pg_control_system()) || ':'
                  || (SELECT oid::text FROM pg_database WHERE datname = current_database())""",
    )


def same_identity(recorded: str | None, current: str) -> bool:
    """Whether an identity recorded earlier names this primary database. One without the database oid, from a
    primary or follower before 2.6.1 or a cursor from before the primary's upgrade, matches by the cluster alone."""
    if recorded is None:
        return False
    if ':' in recorded and ':' in current:
        return recorded == current
    return recorded.split(':')[0] == current.split(':')[0]


async def stamp_starts(source, follower_id: int, current: str) -> None:
    """Give a start to this follower's subscriptions that need one: none yet (its second statement failed), or one
    from another database (this one was restored), whose txids mean nothing here. The follower then copies again,
    which a restore makes it do anyway (410). As in follow_dataset, the start is the xmax of a snapshot taken after
    the subscription is visible."""
    await source.execute(
        """UPDATE follower_datasets
           SET start_txid = txid_snapshot_xmax(txid_current_snapshot()), acked_txid = NULL, start_identity = $2
           WHERE follower_id = $1 AND (start_txid IS NULL OR start_identity IS DISTINCT FROM $2)""",
        follower_id,
        current,
    )


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
        f"""SELECT id, name, created_at, last_seen_at, instance IS NOT NULL AS bound, left_at,
                   last_seen_at IS NULL OR last_seen_at < now() - interval '{STALE_DAYS} days' AS stale
            FROM followers ORDER BY name""",
    )
    held = ' + '.join(
        f'(SELECT count(*) FROM {table} c WHERE c.dataset_id = f.dataset_id '
        'AND c.transaction_id > COALESCE(f.acked_txid, f.start_txid - 1, 0))'
        for table in LOG_TABLES
    )
    datasets = await db.fetch(
        f"""SELECT f.follower_id, f.dataset_id, d.name, f.tags, f.views, f.images, f.start_txid, f.acked_txid,
                   f.subscribed_at,
                   {held} AS held_rows
            FROM follower_datasets f JOIN datasets d ON d.id = f.dataset_id ORDER BY d.name""",
    )
    reported = await db.fetch(
        """SELECT follower_id, dataset_id, base_field, scale, dims, indexed, reported_at
           FROM follower_fields ORDER BY base_field, scale""",
    )
    departures = await db.fetch(
        """SELECT p.follower_id, p.dataset_id, d.name, p.tags, p.views, p.images, p.left_at
           FROM follower_departures p JOIN datasets d ON d.id = p.dataset_id ORDER BY d.name""",
    )
    result = []
    for follower in followers:
        entry = dict(follower)
        entry['datasets'] = [
            {
                **{key: row[key] for key in row.keys() if key != 'follower_id'},
                'own_scales': [
                    {key: own[key] for key in ('base_field', 'scale', 'dims', 'indexed', 'reported_at')}
                    for own in reported
                    if own['follower_id'] == follower['id'] and own['dataset_id'] == row['dataset_id']
                ],
            }
            for row in datasets
            if row['follower_id'] == follower['id']
        ]
        # A follower that stopped following: what it will get back if it returns.
        entry['departures'] = [
            {key: row[key] for key in row.keys() if key != 'follower_id'}
            for row in departures
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
    from then on the dataset is followed, and only writes that started before may have missed the flag. For a
    follower that stopped following, the dataset waits with its others, for if it returns."""
    async with db.pool.acquire() as connection:
        async with connection.transaction():
            # Shared with leaving and returning, which lock the follower to move its subscriptions.
            follower = await connection.fetchrow('SELECT left_at FROM followers WHERE id = $1 FOR SHARE', follower_id)
            if follower is None:
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
            if follower['left_at'] is not None:
                await connection.execute(
                    """INSERT INTO follower_departures (follower_id, dataset_id, tags, views, images)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (follower_id, dataset_id)
                       DO UPDATE SET tags = EXCLUDED.tags, views = EXCLUDED.views, images = EXCLUDED.images""",
                    follower_id,
                    dataset_id,
                    options.tags,
                    options.views,
                    options.images,
                )
                return {'follower_id': follower_id, 'dataset_id': dataset_id, **options.model_dump(), 'departed': True}
            await connection.execute(
                """INSERT INTO follower_datasets (follower_id, dataset_id, tags, views, images)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (follower_id, dataset_id)
                   DO UPDATE SET tags = EXCLUDED.tags, views = EXCLUDED.views, images = EXCLUDED.images""",
                follower_id,
                dataset_id,
                options.tags,
                options.views,
                options.images,
            )
        # A new transaction, after the subscription is visible: every transaction that could have missed it has an
        # id below this snapshot's xmax.
        row = await connection.fetchrow(
            """UPDATE follower_datasets
               SET start_txid = COALESCE(start_txid, txid_snapshot_xmax(txid_current_snapshot())),
                   start_identity = COALESCE(start_identity, $3)
               WHERE follower_id = $1 AND dataset_id = $2 RETURNING *""",
            follower_id,
            dataset_id,
            await identity(connection),
        )
    if row is None:
        # Unfollowed, or the follower left, between the two statements.
        raise HTTPException(status_code=409, detail='The subscription changed meanwhile: reload and try again')
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
        if deleted:
            # Its searches of this dataset end with the subscription: the primary's wait hands each to another
            # follower holding the scale, or answers 503.
            await connection.execute(
                """UPDATE follower_searches SET state = 'cancelled'
                   WHERE follower_id = $1 AND dataset_id = $2 AND state IN ('pending', 'claimed')""",
                follower_id,
                dataset_id,
            )
        else:
            # A follower that stopped following: the dataset won't come back if it returns.
            deleted = await connection.fetchval(
                'DELETE FROM follower_departures WHERE follower_id = $1 AND dataset_id = $2 RETURNING 1',
                follower_id,
                dataset_id,
            )
        if not deleted:
            raise HTTPException(status_code=404, detail='The follower does not follow that dataset')
        await prune(connection, dataset_id)
    return {'unfollowed': dataset_id}


@router.post('/followers/{follower_id}/reset-instance')
async def reset_follower_instance(follower_id: int, user: dict = Depends(require_role('admin'))):
    """Let the follower's token be used by another installation: the next one that calls takes it. For a follower
    reinstalled from scratch (a new installation id) that should carry on, without removing it and copying again."""
    name = await db.fetchval('UPDATE followers SET instance = NULL WHERE id = $1 RETURNING name', follower_id)
    if name is None:
        raise HTTPException(status_code=404, detail='No such follower')
    return {'reset': name}


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


async def current_follower(request: Request, account: dict = Depends(get_follower_user)) -> dict:
    """The follower behind the token, stamped as seen now. Its token belongs to the first follower instance that used
    it, by the installation id it sends (X-Fridge-Instance). A follower before 2.6.1 sends none and so never binds its
    token; once a token is bound, a call without the header is another installation's too."""
    instance = request.headers.get('X-Fridge-Instance') or None
    row = await db.fetchrow(
        'UPDATE followers SET last_seen_at = now(), instance = COALESCE(instance, $2) WHERE user_id = $1 RETURNING *',
        account['id'],
        instance,
    )
    if row is not None and row['instance'] is not None and row['instance'] != instance:
        raise CodedHTTPException(
            409,
            "This follower's token is in use by another Freedom Ridge instance: each instance needs a follower of its "
            'own. If this instance was reinstalled, or went back to a release before 2.6.1, an admin on the primary '
            'can reset the follower on its Followers page',
            'follower_instance_mismatch',
        )
    if row is None:
        # The code tells the follower this is final: it stops its mirrors (other refusals may pass).
        raise CodedHTTPException(403, 'This follower has been removed on the primary', 'follower_removed')
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
    current = await identity()
    await stamp_starts(db, follower['id'], current)
    xmin = await db.fetchval('SELECT txid_snapshot_xmin(txid_current_snapshot())')
    rows = await db.fetch(
        # versions: how many versions this instance keeps of the dataset. They aren't sent; a mirror says they are here.
        """SELECT f.dataset_id, d.name, f.tags, f.views, f.images, f.start_txid, f.acked_txid,
                  (SELECT count(*) FROM dataset_versions v WHERE v.dataset_id = f.dataset_id) AS versions
           FROM follower_datasets f JOIN datasets d ON d.id = f.dataset_id WHERE f.follower_id = $1 ORDER BY d.name""",
        follower['id'],
    )
    return {
        'name': follower['name'],
        'protocol': PROTOCOL,
        # A follower before 2.6.1 compares `identity` exactly and copies everything again when it changes: it
        # keeps the cluster's identifier alone. Newer followers read the database's.
        'identity': current.split(':')[0],
        'database_identity': current,
        # Stopped following (POST /follower/leave). A follower that still has this primary returns: the leave came
        # from a link it has removed since, and arrived after the primary was added again.
        'left': follower['left_at'] is not None,
        'datasets': [
            {**dict(row), 'ready': row['start_txid'] is not None and xmin >= row['start_txid']} for row in rows
        ],
    }


@router.post('/follower/leave')
async def follower_leave(follower: dict = Depends(current_follower)):
    """The follower stopped following this primary (docs/FOLLOWERS.md). Its subscriptions move to
    follower_departures with their options, so a dataset nobody else follows stops logging and each one's log is
    pruned without it. Its reported patch scales go with them, and searches waiting for it are cancelled. It keeps
    its token: they come back if it returns."""
    async with db.pool.acquire() as connection, connection.transaction():
        # Taken first, as remove_follower does: a dataset shared meanwhile waits for this, then joins the departures.
        await connection.execute('SELECT 1 FROM followers WHERE id = $1 FOR UPDATE', follower['id'])
        await connection.execute(
            """SELECT 1 FROM datasets WHERE id IN (SELECT dataset_id FROM follower_datasets WHERE follower_id = $1)
               ORDER BY id FOR NO KEY UPDATE""",
            follower['id'],
        )
        # One statement, so no change to a subscription's options falls between the copy and the delete.
        moved = await connection.fetch(
            """WITH moved AS (
                   DELETE FROM follower_datasets WHERE follower_id = $1 RETURNING dataset_id, tags, views, images
               )
               INSERT INTO follower_departures (follower_id, dataset_id, tags, views, images)
               SELECT $1, dataset_id, tags, views, images FROM moved
               ON CONFLICT (follower_id, dataset_id) DO UPDATE
               SET tags = EXCLUDED.tags, views = EXCLUDED.views, images = EXCLUDED.images, left_at = now()
               RETURNING dataset_id""",
            follower['id'],
        )
        left = sorted(row['dataset_id'] for row in moved)
        for dataset_id in left:
            await prune(connection, dataset_id)
        await connection.execute(
            """UPDATE follower_searches SET state = 'cancelled'
               WHERE follower_id = $1 AND state IN ('pending', 'claimed')""",
            follower['id'],
        )
        await connection.execute('UPDATE followers SET left_at = now() WHERE id = $1', follower['id'])
    return {'left': left}


@router.post('/follower/return')
async def follower_return(follower: dict = Depends(current_follower)):
    """The follower added this primary again: the subscriptions it had when it left come back with their options,
    each with a new start, so it copies them again (into the mirrors it kept: docs/FOLLOWERS.md). A dataset that is
    being deleted, or has become a mirror, doesn't come back. The follower calls it when it adds this primary, and
    when a sync finds the config saying it left while it still has the primary (a late leave from a link it removed).
    The config itself never restores."""
    async with db.pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow('SELECT left_at FROM followers WHERE id = $1 FOR UPDATE', follower['id'])
            if row is None or row['left_at'] is None:  # removed meanwhile, or never left
                return {'restored': []}
            # follow_dataset's checks and lock, for each dataset.
            followable = [
                row['id']
                for row in await connection.fetch(
                    """SELECT d.id FROM datasets d
                       WHERE d.id IN (SELECT dataset_id FROM follower_departures WHERE follower_id = $1)
                         AND NOT d.deleting AND NOT EXISTS (SELECT 1 FROM mirrors m WHERE m.dataset_id = d.id)
                       ORDER BY d.id FOR NO KEY UPDATE OF d""",
                    follower['id'],
                )
            ]
            restored = await connection.fetch(
                """INSERT INTO follower_datasets (follower_id, dataset_id, tags, views, images)
                   SELECT follower_id, dataset_id, tags, views, images FROM follower_departures
                   WHERE follower_id = $1 AND dataset_id = ANY($2::int[])
                   RETURNING dataset_id""",
                follower['id'],
                followable,
            )
            await connection.execute('DELETE FROM follower_departures WHERE follower_id = $1', follower['id'])
            await connection.execute('UPDATE followers SET left_at = NULL WHERE id = $1', follower['id'])
        # Their starts, after they're visible (as follow_dataset's second statement).
        await stamp_starts(connection, follower['id'], await identity(connection))
    return {'restored': sorted(row['dataset_id'] for row in restored)}


@router.get('/follower/datasets/{dataset_id}/blocks')
async def follower_blocks(dataset_id: int, follower: dict = Depends(current_follower)):
    """Every block of the dataset with its sample count: the plan of a full copy. Asking for it starts a copy, whose
    feed is read from start_txid - 1 again, so what an earlier copy acknowledged no longer bounds this follower's
    cursor (follower_changes). Nothing pruned comes back, and nothing needs to: the copy holds it."""
    await subscription(db, follower, dataset_id)
    await db.execute(
        'UPDATE follower_datasets SET acked_txid = NULL WHERE follower_id = $1 AND dataset_id = $2',
        follower['id'],
        dataset_id,
    )
    rows = await db.fetch(
        f'SELECT id / {BLOCK} AS block, count(*) AS samples FROM samples WHERE dataset_id = $1 GROUP BY 1 ORDER BY 1',
        dataset_id,
    )
    return {'block_size': BLOCK, 'blocks': [dict(row) for row in rows]}


MATCH_KEYS = 5000  # samples per page of /match-keys
MATCH_HASH_FIELDS = 8  # hash fields one page may ask for


@router.get('/follower/datasets/{dataset_id}/match-keys')
async def follower_match_keys(
    dataset_id: int,
    after: int = Query(0, ge=0),
    limit: int = Query(MATCH_KEYS, ge=1, le=MATCH_KEYS),
    hash_fields: Annotated[List[str], Query(max_length=MATCH_HASH_FIELDS)] = [],
    follower: dict = Depends(current_follower),
):
    """A page of the dataset's samples, in id order, with what a follower matches its own copy by after this
    primary was restored (docs/FOLLOWERS.md): each sample's id, file path and its hashes among `hash_fields`. A key is
    an object, so it can carry more later (a digest of the copied columns, say). `next` is the next `after`, or null
    on the last page. The first page also says which of `hash_fields` the dataset has."""
    await subscription(db, follower, dataset_id)
    rows = await db.fetch(
        """SELECT s.id, s.filepath,
                  COALESCE((SELECT jsonb_object_agg(h.field_name, h.hash_hex) FROM sample_hashes h
                            WHERE h.sample_id = s.id AND h.field_name = ANY($4::text[])), '{}'::jsonb) AS hashes
           FROM samples s WHERE s.dataset_id = $1 AND s.id > $2 ORDER BY s.id LIMIT $3""",
        dataset_id,
        after,
        limit,
        hash_fields,
    )
    page = {'keys': [dict(row) for row in rows], 'next': rows[-1]['id'] if len(rows) == limit else None}
    if after == 0:  # which of the asked hash fields the dataset has at all, so the follower picks one both sides have
        page['hash_fields'] = [
            row['field_name']
            for row in await db.fetch(
                """SELECT f AS field_name FROM unnest($2::text[]) f
                   WHERE EXISTS (SELECT 1 FROM sample_hashes h JOIN samples s ON s.id = h.sample_id
                                 WHERE h.field_name = f AND s.dataset_id = $1)
                   ORDER BY 1""",
                dataset_id,
                hash_fields,
            )
        ]
    return page


@router.get('/follower/datasets/{dataset_id}/changes')
async def follower_changes(
    dataset_id: int,
    after: int,
    identity_: str = Query(alias='identity'),
    transactions: int = Query(FEED_TRANSACTIONS, ge=1, le=FEED_TRANSACTIONS),
    vector_limit: Annotated[Optional[int], Query(ge=1)] = None,
    follower: dict = Depends(current_follower),
):
    """What changed in transactions after `after` and below the snapshot's xmin, whole transactions only, at most
    `transactions` of them. The cursor returned is the next `after`. 410 when the primary is no longer the one the
    cursor came from (restored into a new database): copy again."""
    async with db.pool.acquire() as connection, connection.transaction(isolation='repeatable_read'):
        if not same_identity(identity_, await identity(connection)):
            raise HTTPException(status_code=410, detail='The primary was restored or replaced: copy the dataset again')
        sub = await subscription(connection, follower, dataset_id)
        if sub['start_txid'] is None or after < sub['start_txid'] - 1:
            raise HTTPException(
                status_code=409,
                detail='Copy the dataset first, then read the feed from start_txid - 1',
            )
        if sub['acked_txid'] is not None and after < sub['acked_txid']:
            # The log up to the acknowledged cursor is pruned: an answer from below it would silently miss changes.
            raise HTTPException(
                status_code=409,
                detail='This cursor is behind what this follower has acknowledged (a follower restored from an older '
                'backup): the changes between them are no longer kept, so copy the dataset again',
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
        if vector_limit is not None and found:
            # A follower that keeps the page in its checkpoint asks for a bounded one: whole transactions, up to the
            # first whose vector changes go over the limit, and always at least one transaction.
            counts = await connection.fetch(
                'SELECT transaction_id, count(*) AS rows FROM vector_changes '
                'WHERE dataset_id = $1 AND transaction_id > $2 AND transaction_id <= $3 GROUP BY 1 ORDER BY 1',
                dataset_id,
                after,
                upto,
            )
            total = 0
            for row in counts:
                total += row['rows']
                if total > vector_limit and row['transaction_id'] > found[0]['transaction_id']:
                    # This transaction goes to the next page: the page ends at the logged one before it.
                    upto = max(t['transaction_id'] for t in found if t['transaction_id'] < row['transaction_id'])
                    more = True
                    break
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
           FROM embeddings e JOIN samples s ON s.dataset_id = e.dataset_id AND s.id = e.sample_id
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
    # Each map's layout id, with the last transaction this read could see (its snapshot's xmax - 1, same statement):
    # every write behind the id, in whatever order they committed, is at or below it. A mirror takes the id once its
    # feed has passed it (follower_sync.write_layouts), so it never names a layout whose points haven't all arrived.
    layouts = await db.fetch(
        """SELECT field_name, layout_id::text AS layout_id, txid_snapshot_xmax(txid_current_snapshot()) - 1 AS txid
           FROM embedding_layouts WHERE dataset_id = $1 AND layout_id IS NOT NULL ORDER BY field_name""",
        dataset_id,
    )
    return {
        'dataset': dict(dataset),
        'fields': [dict(row) for row in fields],
        'views': [dict(row) for row in views],
        'maps': [dict(row) for row in maps],
        'layouts': [dict(row) for row in layouts],
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


# --- Images, for a dataset whose images option is on ----------------------------------------------------------------


@router.post('/follower/datasets/{dataset_id}/fields')
async def follower_fields_report(dataset_id: int, report: FieldReport, follower: dict = Depends(current_follower)):
    """The patch scales this follower holds of a dataset it follows, computed there and not sent by the primary: they
    replace its previous report. A patch search on such a scale can then go to it (docs/FOLLOWERS.md)."""
    unique = {(scale.base_field, scale.scale): scale for scale in report.scales}
    async with db.pool.acquire() as connection, connection.transaction():
        await subscription(connection, follower, dataset_id)
        # A scale reported again keeps its first_reported_at: providers are taken in that order, so the same search
        # keeps going to the same follower.
        await connection.execute(
            """DELETE FROM follower_fields WHERE follower_id = $1 AND dataset_id = $2
                 AND (base_field, scale) NOT IN (SELECT * FROM unnest($3::text[], $4::text[]))""",
            follower['id'],
            dataset_id,
            [base for base, _ in unique],
            [scale for _, scale in unique],
        )
        await connection.executemany(
            """INSERT INTO follower_fields (follower_id, dataset_id, base_field, scale, dims, indexed)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (follower_id, dataset_id, base_field, scale)
               DO UPDATE SET dims = EXCLUDED.dims, indexed = EXCLUDED.indexed, reported_at = now()""",
            [
                (follower['id'], dataset_id, scale.base_field, scale.scale, scale.dims, scale.indexed)
                for scale in unique.values()
            ],
        )
    return {'scales': len(unique)}


@router.get('/follower/searches')
async def follower_claim_search(
    wait: float = Query(0, ge=0, le=follower_search.CLAIM_WAIT_SECONDS),
    follower: dict = Depends(current_follower),
):
    """The oldest patch search relayed to this follower (docs/FOLLOWERS.md), now claimed by it, waiting up to `wait`
    seconds for one: {id, dataset_id, kind, request}, or {} when none came. Each poll counts as checking in."""
    return await follower_search.claim(follower, wait) or {}


@router.post('/follower/searches/{search_id}')
async def follower_answer_search(
    search_id: uuid.UUID,
    answer: SearchAnswer,
    follower: dict = Depends(current_follower),
):
    """This follower's answer to a search it claimed: its results in the primary's sample ids, or why it couldn't
    run it. 409 when the search isn't its any more (cancelled, or answered)."""
    results = [entry.model_dump() for entry in answer.results] if answer.results is not None else None
    if results is None and answer.error is None:
        raise HTTPException(status_code=400, detail='Send results or an error')
    if not await follower_search.answer(follower, str(search_id), results, answer.error):
        raise HTTPException(status_code=409, detail="This search isn't this follower's to answer any more")
    return {'answered': str(search_id)}


@router.get('/follower/datasets/{dataset_id}/samples/{sample_id}/media')
async def follower_media(
    dataset_id: int,
    sample_id: int,
    request: Request,
    field: str = 'filepath',
    overlay: Optional[str] = None,
    colormap: Optional[str] = None,
    alpha: float = 0.6,
    range_start: Optional[float] = None,
    range_end: Optional[float] = None,
    crop: Optional[str] = None,
    w: Optional[int] = None,
    follower: dict = Depends(current_follower),
):
    """A sample's image, thumbnail, crop or overlay, as GET /samples/{id}/media serves it here, for a follower that
    can't read the image itself. Only for a dataset it follows with images on, and only that dataset's samples."""
    sub = await subscription(db, follower, dataset_id)
    if not sub['images']:
        raise HTTPException(
            status_code=403,
            detail="This dataset's images aren't served to this follower: its images option is off on the primary",
        )
    if not await db.fetchval('SELECT 1 FROM samples WHERE id = $1 AND dataset_id = $2', sample_id, dataset_id):
        raise HTTPException(status_code=404, detail='No such sample in this dataset')
    from backend.main import serve_media  # main imports this router

    return await serve_media(
        sample_id,
        request,
        field=field,
        overlay=overlay,
        colormap=colormap,
        alpha=alpha,
        range_start=range_start,
        range_end=range_end,
        crop=crop,
        w=w,
        version_id=None,
        check_access=False,
    )


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
    result = await datasets_router.tag_samples(
        dataset_id,
        datasets_router.BulkTagRequest(
            sample_ids=present,
            field_name=request.field_name,
            add_tags=request.add_tags,
            remove_tags=request.remove_tags,
        ),
        account,
        made_by=request.made_by,
    )
    if isinstance(result, JSONResponse):
        return result
    return {**result, 'skipped': skipped}


@router.post('/follower/datasets/{dataset_id}/views', status_code=201)
async def follower_create_view(dataset_id: int, request: ViewRequest, follower: dict = Depends(current_follower)):
    """A shared view made on the follower, created here as its account and marked with where it was made and by
    whom there (made_on, made_by: set only here; create_view drops them from anyone else's content)."""
    account = await both_ways(follower, dataset_id, 'views')
    from ..follower_sync import uses_concept  # the follower side's check: the same on both

    if uses_concept(request.content):
        raise HTTPException(
            status_code=400,
            detail='A view that uses a concept stays on the instance that made it: concepts belong to each instance',
        )
    if ranges_on_ids(request.content):
        raise HTTPException(
            status_code=400,
            detail='A view with a range on sample ids stays on the instance that made it: each instance numbers its '
            'samples in its own order',
        )
    content = {key: value for key, value in request.content.items() if key not in views_router.FOLLOWER_MARKS}
    content['made_on'] = follower['name']
    if request.made_by:
        content['made_by'] = request.made_by
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
    return await views_router.remove_view(view_id, account, check_access=False)
