"""A follower's side (docs/FOLLOWERS.md): the primaries this instance follows, and the state of its mirrors.

An admin adds a primary with its API address and the follower token the primary's admin made for this instance. The
token is checked against the primary before it's saved, and stored encrypted. The job runner then syncs every minute
(follower_sync.py). Removing a primary here stops its mirrors, which stay as read-only copies to keep or delete.
"""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import follower_sync
from ..crypto import encrypt_secret
from ..database import db
from .auth import require_role

router = APIRouter()


class PrimaryCreate(BaseModel):
    url: str = Field(pattern=r'^https?://[^\s/]+(/[^\s]*)?$', max_length=500)
    token: str = Field(min_length=1, max_length=500)


@router.post('/primaries', status_code=201)
async def add_primary(data: PrimaryCreate, user: dict = Depends(require_role('admin'))):
    """Follow a primary: its datasets shared with this follower start copying within a minute."""
    url = data.url.rstrip('/')
    link = {'url': url, 'token_encrypted': encrypt_secret(data.token)}
    async with follower_sync.connect(link) as client:
        try:
            config = await follower_sync.call(client, 'GET', '/follower/config')
        except follower_sync.PrimaryRefused as error:
            raise HTTPException(status_code=400, detail=f'The primary refused that token: {error.detail}') from None
        except follower_sync.PrimaryUnavailable as error:
            raise HTTPException(status_code=502, detail=str(error)) from None
    try:
        row = await db.fetchrow(
            """INSERT INTO primary_links (url, token_encrypted, follower_name, identity, created_by)
               VALUES ($1, $2, $3, $4, $5) RETURNING id, url, follower_name, created_at""",
            url,
            link['token_encrypted'],
            config['name'],
            config['identity'],
            user['id'],
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail='This instance already follows that primary') from None
    return {**dict(row), 'datasets': len(config['datasets'])}


@router.get('/primaries')
async def list_primaries(user: dict = Depends(require_role('admin'))):
    """Each primary with its last sync, its last error, and the mirrors of its datasets here."""
    links = await db.fetch(
        """SELECT id, url, follower_name, created_at, last_synced_at, last_error FROM primary_links ORDER BY id""",
    )
    mirrors = await db.fetch(
        """SELECT m.link_id, m.dataset_id, d.name, m.origin_dataset_id, m.state, m.tags, m.views, m.copy_total,
                  cardinality(m.copy_blocks) AS copy_left, m.concept_views, m.last_synced_at, m.last_error,
                  (SELECT count(*) FROM mirror_samples s WHERE s.dataset_id = m.dataset_id) AS samples
           FROM mirrors m JOIN datasets d ON d.id = m.dataset_id
           ORDER BY d.name""",
    )
    listed = [{**dict(link), 'mirrors': []} for link in links]
    by_id = {link['id']: link for link in listed}
    unlinked: list = []
    for mirror in mirrors:
        entry = dict(mirror)
        (by_id[mirror['link_id']]['mirrors'] if mirror['link_id'] in by_id else unlinked).append(entry)
    return {'primaries': listed, 'unlinked_mirrors': unlinked}


@router.delete('/primaries/{link_id}')
async def remove_primary(link_id: int, user: dict = Depends(require_role('admin'))):
    """Stop following a primary. Its mirrors stay as read-only copies, marked as no longer following."""
    async with db.pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "UPDATE mirrors SET state = 'stopped', last_error = $2 WHERE link_id = $1 AND state <> 'stopped'",
            link_id,
            'No longer following: the primary was removed on this instance',
        )
        removed = await connection.fetchval('DELETE FROM primary_links WHERE id = $1 RETURNING id', link_id)
    if removed is None:
        raise HTTPException(status_code=404, detail='No such primary')
    return {'removed': link_id}
