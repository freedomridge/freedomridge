"""A follower's side (docs/FOLLOWERS.md): the primaries this instance follows, and the state of its mirrors.

An admin adds a primary with its API address and the follower token the primary's admin made for this instance. The
token is checked against the primary before it's saved, and stored encrypted. The job runner then syncs every minute
(follower_sync.py). Removing a primary here stops its mirrors, which stay as read-only copies to keep or delete.
"""

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import follower_sync
from ..crypto import encrypt_secret
from ..database import db
from . import followers
from .auth import require_role

router = APIRouter()

# How long Stop following waits for the primary to hear it: it stops here either way.
LEAVE_SECONDS = 5


class PrimaryCreate(BaseModel):
    url: str = Field(pattern=r'^https?://[^\s/]+(/[^\s]*)?$', max_length=500)
    token: str = Field(min_length=1, max_length=500)


@router.post('/primaries', status_code=201)
async def add_primary(data: PrimaryCreate, user: dict = Depends(require_role('admin'))):
    """Follow a primary: its datasets shared with this follower start copying within a minute."""
    url = data.url.rstrip('/')
    link = {'url': url, 'token_encrypted': encrypt_secret(data.token)}
    async with follower_sync.connect(link, await follower_sync.installation_id(db)) as client:
        try:
            config = await follower_sync.call(client, 'GET', '/follower/config')
        except follower_sync.PrimaryRefused as error:
            raise HTTPException(status_code=400, detail=f'The primary refused that token: {error.detail}') from None
        except follower_sync.PrimaryUnavailable as error:
            raise HTTPException(status_code=502, detail=str(error)) from None
        identity = follower_sync.primary_identity(config)
        async with db.pool.acquire() as connection, connection.transaction():
            # One add at a time, from the check to the insert: two at once (the same primary at another address)
            # both found no link to it.
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('primary_links:add', 0))")
            # One link per primary, by its identity, not its address: two (a LAN address and a DNS name, say)
            # would share one subscription there, and one's acknowledgements would prune what the other hasn't read.
            for other in await connection.fetch('SELECT url, identity FROM primary_links'):
                if followers.same_identity(other['identity'], identity):
                    raise HTTPException(
                        status_code=409,
                        detail=f"This instance already follows that primary, at {other['url']}",
                    )
            # Back after Stop following: what it followed then comes back (docs/FOLLOWERS.md). After the check above,
            # so a refused add restores nothing. An older primary has no such call (404). Briefly: another add waits
            # on the lock meanwhile.
            client.timeout = httpx.Timeout(follower_sync.RETURN_SECONDS)
            try:
                restored = (await follower_sync.call(client, 'POST', '/follower/return'))['restored']
            except follower_sync.PrimaryRefused as error:
                if error.status != 404:
                    raise HTTPException(
                        status_code=400,
                        detail=f'The primary refused that token: {error.detail}',
                    ) from None
                restored = []
            except follower_sync.PrimaryUnavailable as error:
                raise HTTPException(status_code=502, detail=str(error)) from None
            try:
                row = await connection.fetchrow(
                    """INSERT INTO primary_links (url, token_encrypted, follower_name, identity, created_by)
                       VALUES ($1, $2, $3, $4, $5) RETURNING id, url, follower_name, created_at""",
                    url,
                    link['token_encrypted'],
                    config['name'],
                    identity,
                    user['id'],
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(status_code=409, detail='This instance already follows that primary') from None
    return {**dict(row), 'datasets': len(config['datasets']) + len(restored)}


@router.get('/primaries')
async def list_primaries(user: dict = Depends(require_role('admin'))):
    """Each primary with its last sync, its last error, and the mirrors of its datasets here."""
    links = await db.fetch(
        """SELECT id, url, follower_name, created_at, last_synced_at, last_error FROM primary_links ORDER BY id""",
    )
    mirrors = await db.fetch(
        """SELECT m.link_id, m.dataset_id, d.name, m.origin_dataset_id, m.state, m.tags, m.views, m.images,
                  m.copy_total,
                  cardinality(m.copy_blocks) AS copy_left, m.concept_views, m.last_synced_at, m.last_error,
                  m.retry_after, m.stopped_at, m.keep_separate, m.primary_identity, l.identity AS link_identity,
                  (SELECT count(*) FROM mirror_samples s WHERE s.dataset_id = m.dataset_id) AS samples
           FROM mirrors m JOIN datasets d ON d.id = m.dataset_id LEFT JOIN primary_links l ON l.id = m.link_id
           ORDER BY d.name""",
    )
    stopped: dict[int, list] = {}
    for row in await db.fetch(f'{follower_sync.RESUMABLE} {follower_sync.RESUMABLE_ORDER}'):
        stopped.setdefault(row['origin_dataset_id'], []).append(row)
    live = [mirror for mirror in mirrors if mirror['state'] != 'stopped']
    # A copy held after its primary was restored, and the question its admin answers (docs/FOLLOWERS.md).
    held = {
        row['dataset_id']: dict(row)
        for row in await db.fetch(
            """SELECT dataset_id, origin_name, hash_field, total, matched, matched_hash, only_here, phase,
                      decision, computed_at IS NOT NULL AS compared, last_error, retry_after
               FROM mirror_restores""",
        )
    }
    listed = [{**dict(link), 'mirrors': []} for link in links]
    by_id = {link['id']: link for link in listed}
    unlinked: list = []
    for mirror in mirrors:
        entry = dict(mirror)
        link_identity, own_identity = entry.pop('link_identity'), entry.pop('primary_identity')
        identity = link_identity or own_identity
        origin = mirror['origin_dataset_id']
        # The copy a new copy of its dataset would resume into, as configure picks it: the latest stopped one of the
        # same primary database (its link's, or its own once the link is gone). Not while a copy of that database
        # still follows the dataset: that one would stop and be resumed instead.
        picked = None if identity is None else follower_sync.resumed_from(stopped.get(origin, []), identity)
        entry['resumable'] = (
            picked is not None
            and picked['dataset_id'] == mirror['dataset_id']
            and not any(
                other['origin_dataset_id'] == origin and followers.same_identity(other['primary_identity'], identity)
                for other in live
            )
        )
        entry['restore'] = held.get(mirror['dataset_id'])
        (by_id[mirror['link_id']]['mirrors'] if mirror['link_id'] in by_id else unlinked).append(entry)
    return {'primaries': listed, 'unlinked_mirrors': unlinked}


@router.post('/primaries/mirrors/{dataset_id}/keep-separate')
async def keep_mirror_separate(dataset_id: int, user: dict = Depends(require_role('admin'))):
    """Keep a stopped copy as it is: a new copy of its dataset from its primary makes a new dataset instead of
    resuming into this one (which would delete the samples gone there, with what this instance added to them)."""
    name = await db.fetchval(
        """UPDATE mirrors m SET keep_separate = TRUE FROM datasets d
           WHERE m.dataset_id = $1 AND d.id = m.dataset_id AND m.state = 'stopped' RETURNING d.name""",
        dataset_id,
    )
    if name is None:
        raise HTTPException(status_code=409, detail='Only a stopped copy can be kept as a separate copy')
    return {'kept': name}


class RestoreDecision(BaseModel):
    decision: str = Field(pattern='^(resume|new)$')


@router.post('/primaries/mirrors/{dataset_id}/restore-decision')
async def decide_restore(dataset_id: int, request: RestoreDecision, user: dict = Depends(require_role('admin'))):
    """Answer the question a copy held after its primary was restored asks: resume into it (its matched samples
    keep what this instance added to them; the others go, and the primary's unmatched ones are copied fresh), or make
    a new copy and keep this one as it is."""
    if request.decision == 'resume' and await db.fetchval(
        "SELECT matched = 0 FROM mirror_restores WHERE dataset_id = $1 AND phase = 'ask'",
        dataset_id,
    ):
        raise HTTPException(
            status_code=409,
            detail='Nothing in this copy matches the restored primary: resuming would only delete it. Make a new copy',
        )
    row = await db.fetchrow(
        """UPDATE mirror_restores SET decision = $2, decided_by = $3, decided_at = now(),
                  phase = CASE WHEN $2 = 'resume' THEN 'unmatched' ELSE 'map' END
           WHERE dataset_id = $1 AND phase = 'ask' AND decision IS NULL RETURNING link_id""",
        dataset_id,
        request.decision,
        user['id'],
    )
    if row is None:
        raise HTTPException(
            status_code=409,
            detail='This copy has no question waiting: it may be comparing, or answered',
        )
    await follower_sync.queue_sync(db.pool, row['link_id'])
    return {'decision': request.decision}


@router.delete('/primaries/{link_id}')
async def remove_primary(link_id: int, user: dict = Depends(require_role('admin'))):
    """Stop following a primary. Its mirrors stay as read-only copies, marked as no longer following. Then the primary
    is told, so it stops keeping its change log for this instance: `told_primary`, with the reason when it wasn't."""
    async with db.pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """UPDATE mirrors SET state = 'stopped', last_error = $2, stopped_at = now()
               WHERE link_id = $1 AND state <> 'stopped'""",
            link_id,
            'No longer following: the primary was removed on this instance',
        )
        link = await connection.fetchrow(
            'DELETE FROM primary_links WHERE id = $1 RETURNING url, token_encrypted',
            link_id,
        )
    if link is None:
        raise HTTPException(status_code=404, detail='No such primary')
    # After the commit: no new sync of this link starts, and one still running finds its mirrors stopped.
    detail = await tell_primary_left(link)
    return {'removed': link_id, 'told_primary': detail is None, **({'detail': detail} if detail else {})}


async def tell_primary_left(link) -> str | None:
    """Tell the primary this instance stopped following it (POST /follower/leave): None once it knows, else why not."""
    try:
        async with follower_sync.connect(link, await follower_sync.installation_id(db)) as client:
            client.timeout = httpx.Timeout(LEAVE_SECONDS)
            await follower_sync.call(client, 'POST', '/follower/leave')
    except follower_sync.PrimaryRefused as error:
        if error.code in follower_sync.FINAL_REFUSALS:
            return None  # removed there already: it keeps nothing for this instance
        if error.status == 404:
            return "The primary runs an older release, which can't be told"
        return f'The primary refused: {error.detail}'
    except follower_sync.PrimaryUnavailable as error:
        return str(error)
    return None
