import json
import secrets
from typing import Any, List

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel

from .. import public_settings
from ..access import check_dataset_access, missing_unless_allowed, require_dataset_access
from ..database import db
from ..models import View, ViewCreate
from ..security import private_guest, public_mode
from ..validation import clean_name
from .auth import get_current_user, get_optional_user

MAX_VIEW_BODY_BYTES = 256 * 1024


class ViewRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            if (
                public_mode()
                and request.method in ('POST', 'PATCH', 'PUT')
                and private_guest(await get_current_user(request))
            ):
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_VIEW_BODY_BYTES:
                        raise HTTPException(status_code=413, detail='Saved view payload exceeds 256 KB')
                    body.extend(chunk)
                # Cache only the bounded bytes before FastAPI decodes JSON, including chunked bodies.
                request._body = bytes(body)
            return await handler(request)

        return bounded


router = APIRouter(route_class=ViewRoute)

# A view's marks of the follower it came from and who made it there (docs/FOLLOWERS.md). Only the primary sets them,
# when a follower sends a view up (followers.follower_create_view), so no one can make a view look like a follower's.
FOLLOWER_MARKS = ('made_on', 'made_by')


def generate_slug():
    return secrets.token_urlsafe(8)


def _decode_content(content):
    """JSONB `content` comes back as a dict from the asyncpg codec. Legacy rows
    that were double-encoded (json.dumps + codec) come back as a JSON string —
    accept both so old views keep loading until the backfill normalizes them."""
    if isinstance(content, str):
        return json.loads(content)
    return content


TEMPORARY_SELECTION_DETAIL = (
    "A saved view can't keep a temporary selection (a Select All over 50,000 samples, or a version "
    "comparison's samples): it expires after 2 hours. Save the filters instead — Select All is what they "
    "already show. To keep an exact set, tag it and filter by the tag."
)


def _has_temporary_selection(node: Any) -> bool:
    """True when a view's content uses a selection token (`in_selection`). Tokens live 2 hours in
    region_selections, so a saved view holding one reopened as 0 samples with no explanation."""
    if isinstance(node, dict):
        return node.get("operator") == "in_selection" or any(_has_temporary_selection(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_temporary_selection(v) for v in node)
    return False


@router.post("/datasets/{id}/views", response_model=View, dependencies=[Depends(require_dataset_access)])
async def create_view(id: int, view: ViewCreate, user: dict = Depends(get_current_user)):
    if private_guest(user):
        view.is_public = False
        view.shared_with_user_ids = []
    if _has_temporary_selection(view.content):
        raise HTTPException(status_code=422, detail=TEMPORARY_SELECTION_DETAIL)
    # A follower's token reaches this only through follower_create_view, which sets the marks itself.
    if any(key in view.content for key in FOLLOWER_MARKS) and not await db.fetchval(
        'SELECT 1 FROM followers WHERE user_id = $1',
        user['id'],
    ):
        view.content = {key: value for key, value in view.content.items() if key not in FOLLOWER_MARKS}
    # Verify dataset exists
    dataset = await db.fetchrow("SELECT id FROM datasets WHERE id = $1", id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    # A shared view on a mirror whose views sync both ways is made on its primary (docs/FOLLOWERS.md), unless it uses
    # a concept or a range on sample ids, which mean something else there: that view stays here, shared on this
    # instance (follower_sync.instance_bound).
    from .. import follower_sync  # here: it imports the routers

    if (
        view.is_public
        and not follower_sync.instance_bound(view.content)
        and await db.fetchval("SELECT 1 FROM mirrors WHERE dataset_id = $1 AND views = 'both'", id)
    ):
        from .. import mirror_writes

        view.name = clean_name(view.name, "View name")
        return await mirror_writes.forward_view(db, id, view, user)

    # Blank names were accepted, and "rv_a " / "RV_A" sat next to "rv_a" as indistinguishable
    # entries: trim, and compare names case-insensitively.
    view.name = clean_name(view.name, "View name")
    await _require_known_users(view.shared_with_user_ids)
    scope = user['id'] if private_guest(user) else 0
    try:
        async with db.pool.acquire() as connection, connection.transaction():
            # Serialize quota + insert across API workers and datasets for this owner. Other
            # users never wait on a guest's quota lock. The unique index guards exact-name races.
            await connection.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE', user['id'])
            if await connection.fetchval(
                'SELECT 1 FROM views WHERE dataset_id=$1 AND name_scope=$2 AND lower(name)=lower($3)',
                id,
                scope,
                view.name,
            ):
                raise HTTPException(status_code=400, detail="View with this name already exists for this dataset")
            if private_guest(user):
                limits = public_settings.settings
                usage = await connection.fetchrow(
                    """SELECT count(*) AS count, COALESCE(sum(
                           octet_length(content::text) + octet_length(name) +
                           octet_length(COALESCE(description,''))),0) AS bytes
                       FROM views WHERE user_id=$1""",
                    user['id'],
                )
                size = await connection.fetchval(
                    "SELECT octet_length($1::jsonb::text) + octet_length($2::text) + octet_length($3::text)",
                    view.content,
                    view.name,
                    view.description or '',
                )
                if usage['count'] >= limits.guest_view_count:
                    raise HTTPException(
                        429,
                        'Guest saved-view count quota reached. Delete a saved view to save another.',
                    )
                if usage['bytes'] + size > limits.guest_view_bytes:
                    raise HTTPException(
                        413,
                        'Guest saved-view storage quota reached. Delete saved views to free space.',
                    )
            row = await connection.fetchrow(
                """INSERT INTO views
                       (dataset_id,user_id,name,description,slug,content,is_public,shared_with_user_ids,name_scope)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""",
                id,
                user['id'],
                view.name,
                view.description,
                generate_slug(),
                view.content,
                view.is_public,
                view.shared_with_user_ids,
                scope,
            )
        return {**dict(row), 'content': _decode_content(row['content'])}
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=400, detail="View with this name already exists for this dataset") from None


@router.get("/datasets/{id}/views", response_model=list[View], dependencies=[Depends(require_dataset_access)])
async def list_dataset_views(id: int, user: dict = Depends(get_current_user)):
    query = """
        SELECT id, dataset_id, user_id, name, description, slug, content, is_public, shared_with_user_ids, created_at,
               EXISTS (SELECT 1 FROM mirror_views m WHERE m.view_id = views.id) AS from_primary
        FROM views
        WHERE dataset_id = $1 AND (
            user_id = $2 OR
            is_public = true OR
            $2 = ANY(shared_with_user_ids)
        )
        ORDER BY created_at DESC
    """
    rows = await db.fetch(query, id, user['id'])
    results = []
    for row in rows:
        data = dict(row)
        data['content'] = _decode_content(data['content'])
        results.append(data)
    return results


@router.get("/views/{slug}", response_model=View)
async def get_view(slug: str, request: Request):
    # Login is required for ALL access (same model as datasets — see
    # access.check_dataset_access). A slug is a share link between logged-in
    # users, not an anonymous backdoor: the share UI promises "anyone with
    # access to this dataset", so gate on exactly that. Anonymous gets 401
    # BEFORE the row lookup so slugs can't even be probed for existence.
    user = await get_optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    row = await db.fetchrow(
        """
        SELECT id, dataset_id, user_id, name, description, slug, content,
               is_public, shared_with_user_ids, created_at
        FROM views
        WHERE slug = $1
    """,
        slug,
    )
    if not row:
        raise HTTPException(status_code=404, detail="View not found")

    # View visibility never exceeds dataset visibility. For an authenticated
    # caller every denial is a 404 (not 403) so the endpoint doesn't confirm
    # which slugs exist.
    try:
        await check_dataset_access(row['dataset_id'], user)
    except HTTPException as e:
        if e.status_code in (403, 404):
            raise HTTPException(status_code=404, detail="View not found")
        raise

    # is_public = visible to any logged-in user with dataset access; private /
    # specifically-shared views additionally require owner/admin/shared status.
    if not row['is_public']:
        allowed = (
            user.get('role') == 'admin'
            or user['id'] == row['user_id']
            or user['id'] in (row['shared_with_user_ids'] or [])
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="View not found")

    data = dict(row)
    data['content'] = _decode_content(data['content'])
    # shared_with_user_ids was only needed for the access check above; don't
    # expose the internal user IDs of everyone a public view was shared with to
    # whoever opens the slug. (Owners manage sharing via the list/share routes.)
    data['shared_with_user_ids'] = []
    return data


@router.delete("/views/{id}")
async def delete_view(id: int, user: dict = Depends(get_current_user)):
    return await remove_view(id, user)


async def remove_view(id: int, user: dict, *, check_access: bool = True):
    """delete_view's work. With check_access off, for a follower's request the followers router has authorized (a
    shared view of a dataset it follows), its account's dataset access isn't checked."""
    from .. import mirror_writes

    # Check ownership or admin
    view = await db.fetchrow("SELECT user_id, dataset_id FROM views WHERE id = $1", id)
    if not view:
        raise HTTPException(status_code=404, detail="View not found")

    if view['user_id'] != user['id'] and user['role'] != 'admin':
        if check_access:
            # Someone else's view in a dataset the caller can't open answers as a missing one: a 403 said it exists.
            with missing_unless_allowed("View not found"):
                await check_dataset_access(view['dataset_id'], user)
        raise HTTPException(status_code=403, detail="Not authorized to delete this view")
    # A view a mirror copied from its primary is deleted there, when the mirror's views sync both ways.
    forwarded = await mirror_writes.forward_view_deletion(db, id)
    if forwarded is not None:
        return forwarded

    query = "DELETE FROM views WHERE id = $1 RETURNING id"
    row = await db.fetchrow(query, id)
    if not row:
        raise HTTPException(status_code=404, detail="View not found")
    return {"message": "View deleted"}


async def _require_known_users(user_ids: List[int]) -> None:
    """400 naming any share target that is not a user (it was stored as given)."""
    if not user_ids:
        return
    known = {r['id'] for r in await db.fetch("SELECT id FROM users WHERE id = ANY($1::int[])", user_ids)}
    unknown = sorted(set(user_ids) - known)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown user id(s): {unknown}")


class ViewShare(BaseModel):
    is_public: bool
    shared_with_user_ids: List[int]


@router.patch("/datasets/{id}/views/{view_id}/share", dependencies=[Depends(require_dataset_access)])
async def share_view(id: int, view_id: int, share: ViewShare, user: dict = Depends(get_current_user)):
    if private_guest(user):
        raise HTTPException(status_code=403, detail='Guests cannot share views or make them public')
    # Check ownership
    view = await db.fetchrow("SELECT user_id FROM views WHERE id = $1 AND dataset_id = $2", view_id, id)
    if not view:
        raise HTTPException(status_code=404, detail="View not found")

    if view['user_id'] != user['id'] and user['role'] != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized to share this view")
    await _require_known_users(share.shared_with_user_ids)
    # Made public on a mirror whose views sync both ways: it's made on the primary, and this private one is replaced
    # by its copy (docs/FOLLOWERS.md). Not a view bound to this instance (a concept, or a range on sample ids).
    from .. import follower_sync  # here: it imports the routers

    content = _decode_content(await db.fetchval("SELECT content FROM views WHERE id = $1", view_id))
    if (
        share.is_public
        and not follower_sync.instance_bound(content)
        and await db.fetchval(
            """SELECT 1 FROM mirrors m WHERE m.dataset_id = $1 AND m.views = 'both'
           AND NOT EXISTS (SELECT 1 FROM mirror_views v WHERE v.view_id = $2)""",
            id,
            view_id,
        )
    ):
        from .. import mirror_writes

        local = await db.fetchrow("SELECT name, content FROM views WHERE id = $1", view_id)
        copy = await mirror_writes.forward_view(
            db,
            id,
            ViewCreate(name=local['name'], content=_decode_content(local['content']), is_public=True),
            user,
        )
        await db.execute("DELETE FROM views WHERE id = $1", view_id)
        return copy

    async with db.pool.acquire() as connection, connection.transaction():
        # FOR UPDATE first: a default-view change holds the view FOR SHARE until it commits, and then the
        # check below sees it.
        await connection.execute("SELECT 1 FROM views WHERE id = $1 FOR UPDATE", view_id)
        if not share.is_public and await connection.fetchval(
            "SELECT 1 FROM datasets WHERE default_view_id = $1",
            view_id,
        ):
            raise HTTPException(
                status_code=409,
                detail="This is the dataset's default view, which everyone who opens the dataset starts on, "
                "so it stays public until an administrator chooses another default view or removes this one "
                "as the default.",
            )
        await connection.execute(
            "UPDATE views SET is_public = $1, shared_with_user_ids = $2 WHERE id = $3",
            share.is_public,
            share.shared_with_user_ids,
            view_id,
        )
    return {"message": "View sharing updated"}
