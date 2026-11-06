import secrets
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..crypto import hash_token
from ..database import db
from ..validation import clean_name
from .auth import get_current_user, require_role

router = APIRouter()


class TokenCreate(BaseModel):
    name: str
    # Default 90 days; null = never expires. 0 used to mean "never" too and -1 made a token that was
    # born expired — both are now refused.
    expires_days: Optional[int] = Field(90, ge=1, le=36500)
    # 'worker': for a compute worker (python -m fridge.worker); only the worker endpoints accept it.
    scope: Literal['full', 'worker'] = 'full'


class TokenResponse(BaseModel):
    id: int
    name: str
    token: Optional[str] = None  # Only returned on creation
    created_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    is_active: bool
    scope: str


@router.post("/tokens", response_model=TokenResponse)
async def create_token(data: TokenCreate, user: dict = Depends(require_role("power_user"))):
    """
    Create new API token.

    Token inherits user's role permissions (admin, power_user, guest).
    """
    # Generate secure random token (43 chars base64url). Only its hash is stored;
    # the plaintext is returned exactly once here and never persisted.
    name = clean_name(data.name, "Token name")
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_token(raw_token)

    # Compute expiry with the DB clock (NOW()) so it matches the NOW()-based
    # token-expiry check in get_current_user (Python local time expired tokens
    # ~TZ-offset hours early). NULL expires_days → non-expiring token.
    expires_days = float(data.expires_days) if data.expires_days else None

    query = """
        INSERT INTO api_tokens (user_id, token, name, expires_at, scope)
        VALUES ($1, $2, $3,
            CASE WHEN $4::double precision IS NULL THEN NULL
                 ELSE NOW() + $4::double precision * INTERVAL '1 day' END, $5)
        RETURNING id, name, created_at, last_used_at, expires_at, is_active, scope
    """

    row = await db.fetchrow(query, user['id'], token_hash, name, expires_days, data.scope)
    result = dict(row)
    result['token'] = raw_token  # shown once to the caller; DB holds only the hash
    return result


@router.get("/tokens", response_model=List[TokenResponse])
async def list_tokens(user: dict = Depends(get_current_user)):
    """
    List user's API tokens.

    Token values are NOT included in list (only shown on creation).
    """
    query = """
        SELECT id, name, created_at, last_used_at, expires_at, is_active, scope
        FROM api_tokens
        WHERE user_id = $1
        ORDER BY created_at DESC
    """

    rows = await db.fetch(query, user['id'])
    return [dict(row) for row in rows]


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: int, user: dict = Depends(get_current_user)):
    """
    Revoke an API token.

    Users can only revoke their own tokens.
    """
    # Verify ownership
    check = await db.fetchrow("SELECT user_id FROM api_tokens WHERE id = $1", token_id)

    if not check:
        raise HTTPException(status_code=404, detail="Token not found")

    if check['user_id'] != user['id']:
        raise HTTPException(status_code=403, detail="Not your token")

    # Mark as inactive (soft delete)
    await db.execute("UPDATE api_tokens SET is_active = FALSE WHERE id = $1", token_id)

    return {"message": "Token revoked successfully"}
