"""Edits made on a mirror (docs/FOLLOWERS.md) that its primary takes: tags when the dataset's tags option is both
ways, shared views when its views option is. The primary makes them as this follower's account. Tags come back
through the feed, and a sync is queued at once; a shared view is also written here straight away, as the sync would.
"""

from urllib.parse import urlparse

import asyncpg
from fastapi import HTTPException

from . import follower_sync
from .routers import datasets as datasets_router
from .routers import followers


async def mirror_of(source, dataset_id: int):
    row = await source.fetchrow(
        """SELECT m.*, l.url, l.token_encrypted FROM mirrors m LEFT JOIN primary_links l ON l.id = m.link_id
           WHERE m.dataset_id = $1""",
        dataset_id,
    )
    return dict(row) if row is not None else None


def host_of(mirror: dict) -> str:
    return (urlparse(mirror['url']).hostname or mirror['url']) if mirror['url'] else 'its primary'


def require_both_ways(mirror: dict, option: str, what: str) -> None:
    if mirror['state'] != 'following' or mirror['link_id'] is None:
        doing = 'still copying from' if mirror['state'] == 'copying' else 'no longer following'
        raise HTTPException(
            status_code=409,
            detail=f"This mirror is {doing} {host_of(mirror)}: its {what} can't change",
        )
    if mirror[option] != 'both':
        raise HTTPException(
            status_code=409,
            detail=f"This mirror's {what} come from {host_of(mirror)}: change them there, or ask its admin to sync "
            f'{what} both ways (Settings → Followers)',
        )


async def send(client, mirror: dict, method: str, path: str, body=None):
    """A call to the primary, its refusals passed on to the user."""
    try:
        return await follower_sync.call(client, method, path, body=body)
    except follower_sync.PrimaryRefused as error:
        status = error.status if error.status in (400, 403, 404, 409, 422) else 502
        raise HTTPException(status_code=status, detail=f'{host_of(mirror)} refused it: {error.detail}') from None
    except follower_sync.PrimaryUnavailable as error:
        raise HTTPException(status_code=502, detail=str(error)) from None


async def forward_tags(source, dataset_id: int, request) -> dict:
    """A bulk tag edit on a mirror, by ids, a selection or filters: sent to the primary for the same samples there."""
    mirror = await mirror_of(source, dataset_id)
    assert mirror is not None
    require_both_ways(mirror, 'tags', 'tags')
    if request.filters is not None:
        args: list = []
        predicate, _ = datasets_router.parse_filter_node(
            request.filters,
            args,
            1,
            None,
            None,
            dataset_id,
            array_columns=await datasets_router.get_dataset_array_columns(dataset_id),
        )
        args.append(dataset_id)
        target = f'({predicate}) AND dataset_id = ${len(args)}'
    else:
        target, args = datasets_router._bulk_target_predicate(request, dataset_id, start_index=1)
    rows = await source.fetch(
        f"""SELECT origin_id FROM mirror_samples WHERE dataset_id = ${len(args) + 1}
            AND sample_id IN (SELECT id FROM samples WHERE {target}) ORDER BY 1""",
        *args,
        dataset_id,
    )
    origin_ids = [row['origin_id'] for row in rows]
    updated = queued = skipped = 0
    async with follower_sync.connect(mirror) as client:
        for start in range(0, len(origin_ids), followers.TAG_SAMPLES):
            chunk = origin_ids[start : start + followers.TAG_SAMPLES]
            try:
                result = await send(
                    client,
                    mirror,
                    'POST',
                    f"/follower/datasets/{mirror['origin_dataset_id']}/tags",
                    body={
                        'sample_ids': chunk,
                        'field_name': request.field_name,
                        'add_tags': request.add_tags,
                        'remove_tags': request.remove_tags,
                    },
                )
            except HTTPException as error:
                if start:
                    error.detail = f'{error.detail} ({start:,} of {len(origin_ids):,} samples were sent before it)'
                raise
            if 'updated_count' in result:
                updated += result['updated_count']
                skipped += result.get('skipped', 0)
            else:  # queued there as a bulk job
                queued += len(chunk)
    await follower_sync.queue_sync(source.pool, mirror['link_id'])
    host = host_of(mirror)
    message = f'Sent to {host}: {updated:,} samples tagged there'
    if queued:
        message += f', {queued:,} more queued there as a bulk edit'
    message += '. They show here after the next sync, within a minute.'
    return {
        'updated_count': updated,
        'queued_on_primary': queued,
        'skipped': skipped,
        'forwarded_to': host,
        'message': message,
    }


async def forward_view(source, dataset_id: int, view, user: dict):
    """A shared view made on a mirror: created on the primary with its sample ids, and written here as its copy."""
    mirror = await mirror_of(source, dataset_id)
    assert mirror is not None
    require_both_ways(mirror, 'views', 'shared views')
    named = sorted(follower_sync.sample_ids_in(view.content, set()))
    to_primary = {
        row['sample_id']: row['origin_id']
        for row in await source.fetch(
            'SELECT sample_id, origin_id FROM mirror_samples WHERE dataset_id = $1 AND sample_id = ANY($2::int[])',
            dataset_id,
            named,
        )
    }
    async with follower_sync.connect(mirror) as client:
        created = await send(
            client,
            mirror,
            'POST',
            f"/follower/datasets/{mirror['origin_dataset_id']}/views",
            body={'name': view.name, 'content': follower_sync.translate_ids(view.content, to_primary)},
        )
    content = {**view.content, 'made_on': created['content'].get('made_on')}
    async with source.pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT set_config('fridge.mirror_sync', $1, TRUE)", str(dataset_id))
        existing = await connection.fetchval(
            'SELECT view_id FROM mirror_views WHERE dataset_id = $1 AND origin_view_id = $2',
            dataset_id,
            created['id'],
        )
        if existing is None:  # the sync hasn't brought it yet
            try:
                existing = await connection.fetchval(
                    """INSERT INTO views (dataset_id, user_id, name, slug, content, is_public, name_scope)
                       VALUES ($1, $5, $2, $3, $4::jsonb, TRUE, -1) RETURNING id""",
                    dataset_id,
                    view.name,
                    f"mirror-{dataset_id}-{created['id']}",
                    content,
                    user['id'],
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    status_code=409,
                    detail=f'{host_of(mirror)} made the view, and this mirror already has a shared view of that name',
                ) from None
            await connection.execute(
                'INSERT INTO mirror_views (dataset_id, origin_view_id, view_id) VALUES ($1, $2, $3)',
                dataset_id,
                created['id'],
                existing,
            )
        row = await connection.fetchrow(
            """SELECT id, dataset_id, user_id, name, description, slug, content, is_public, shared_with_user_ids,
                      created_at FROM views WHERE id = $1""",
            existing,
        )
    return {**dict(row), 'from_primary': True}


async def forward_view_deletion(source, view_id: int):
    """Deleting a view a mirror copied from its primary: deleted there (its account deletes only the views it made),
    then here. None when the view isn't a copy."""
    mapped = await source.fetchrow('SELECT dataset_id, origin_view_id FROM mirror_views WHERE view_id = $1', view_id)
    if mapped is None:
        return None
    mirror = await mirror_of(source, mapped['dataset_id'])
    assert mirror is not None
    require_both_ways(mirror, 'views', 'shared views')
    async with follower_sync.connect(mirror) as client:
        await send(
            client,
            mirror,
            'DELETE',
            f"/follower/datasets/{mirror['origin_dataset_id']}/views/{mapped['origin_view_id']}",
        )
    async with source.pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT set_config('fridge.mirror_sync', $1, TRUE)", str(mapped['dataset_id']))
        await connection.execute('DELETE FROM views WHERE id = $1', view_id)
    return {'message': 'View deleted'}
