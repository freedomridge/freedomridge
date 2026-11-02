from datetime import datetime
from typing import Any, List, Literal, Optional

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from ..auth_policy import validate_new_password
from ..database import db
from ..passwords import hash_password
from ..security import private_guest
from ..validation import clean_name
from .auth import get_current_user

router = APIRouter()


# The users_role_check constraint's values. An unknown role used to reach the INSERT and 500 with
# the failing row — the new password's bcrypt hash included — in the error detail.
Role = Literal["admin", "power_user", "guest"]


class UserCreate(BaseModel):
    username: str
    email: Optional[str] = None
    role: Role = "guest"
    password: str


class UserUpdate(BaseModel):
    role: Optional[Role] = None
    is_active: Optional[bool] = None
    email: Optional[str] = None


class UserListResponse(BaseModel):
    id: int
    username: str
    email: Optional[str]
    role: str
    is_active: bool
    last_login: Optional[datetime]
    created_at: datetime


def require_admin(user: dict = Depends(get_current_user)):
    if user['role'] != 'admin':
        raise HTTPException(status_code=403, detail="Requires admin privileges")
    return user


@router.get("/users", response_model=List[UserListResponse])
async def list_users(admin: dict = Depends(require_admin)):
    query = """
        SELECT id, username, email, role, is_active, last_login, created_at
        FROM users
        ORDER BY id
    """
    rows = await db.fetch(query)
    return [dict(row) for row in rows]


class UserNameResponse(BaseModel):
    id: int
    username: str


@router.get("/users/names", response_model=List[UserNameResponse])
async def list_user_names(user: dict = Depends(get_current_user)):
    """Minimal user directory (id + username) for any logged-in user.

    Exists so non-admins can pick share targets in the view Share modal —
    the full GET /users (roles, emails, activity) stays admin-only.
    """
    if private_guest(user):
        raise HTTPException(status_code=403, detail="Only power users can look up view sharing targets")
    rows = await db.fetch("SELECT id, username FROM users WHERE is_active ORDER BY username")
    return [dict(row) for row in rows]


@router.post("/users")
async def create_user(user: UserCreate, admin: dict = Depends(require_admin)):
    # "" and "   " used to be accepted — listed, offered as share targets, able to log in.
    user.username = clean_name(user.username, "Username")
    # Check if username exists
    exists = await db.fetchval("SELECT 1 FROM users WHERE username = $1", user.username)
    if exists:
        raise HTTPException(status_code=400, detail="Username already exists")

    validate_new_password(user.password, user.username)

    # Hash password
    password_hash = await hash_password(user.password)

    query = """
        INSERT INTO users (username, email, role, password_hash, must_change_password)
        VALUES ($1, $2, $3, $4, true)
        RETURNING id
    """

    user_id = await db.fetchval(query, user.username, user.email, user.role, password_hash)

    return {"id": user_id, "message": "User created successfully"}


@router.patch("/users/{id}")
async def update_user(id: int, update: UserUpdate, admin: dict = Depends(require_admin)):
    # Prevent locking everyone out: an admin can't strip their own admin role or deactivate
    # themselves (mirrors the self-delete guard below). Since only an admin can reach this
    # endpoint, blocking self-demotion guarantees at least one admin always remains.
    if id == admin['id']:
        if update.role is not None and update.role != 'admin':
            raise HTTPException(status_code=400, detail="Cannot remove your own admin role")
        if update.is_active is False:
            raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    # Build query dynamically
    fields = []
    values: list[Any] = []
    idx = 1

    if update.role is not None:
        fields.append(f"role = ${idx}")
        values.append(update.role)
        idx += 1

    if update.is_active is not None:
        fields.append(f"is_active = ${idx}")
        values.append(update.is_active)
        idx += 1

    if update.email is not None:
        fields.append(f"email = ${idx}")
        values.append(update.email)
        idx += 1

    if not fields:
        return {"message": "No changes requested"}

    values.append(id)
    query = f"UPDATE users SET {', '.join(fields)} WHERE id = ${idx}"

    if await db.execute(query, *values) == "UPDATE 0":  # said "updated successfully"
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": "User updated successfully"}


@router.delete("/users/{id}")
async def delete_user(id: int, admin: dict = Depends(require_admin)):
    if id == admin['id']:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    try:
        assert db.pool is not None  # connected at startup (lifespan)
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                # Their PRIVATE views would be orphaned (views.user_id ON DELETE SET NULL): listed for
                # nobody, deletable by nobody. Public views stay (an admin can delete them);
                # the id leaves every share list. All undone if the user can't be deleted (409 below).
                await conn.execute("DELETE FROM views WHERE user_id = $1 AND is_public IS NOT TRUE", id)
                await conn.execute(
                    "UPDATE views SET shared_with_user_ids = array_remove(shared_with_user_ids, $1) "
                    "WHERE $1 = ANY(shared_with_user_ids)",
                    id,
                )
                deleted = await conn.execute("DELETE FROM users WHERE id = $1", id)
    except asyncpg.ObjectNotInPrerequisiteStateError:
        raise HTTPException(
            status_code=409,
            detail=(
                "This user is associated with a dataset being deleted. Finish or resume that dataset's "
                "deletion before deleting the user. You can deactivate the account in the meantime."
            ),
        )
    except asyncpg.ForeignKeyViolationError:
        # samples.created_by/modified_by, storage_configs.created_by and dataset_access.granted_by
        # are NO ACTION on purpose: deleting the user would erase who did what. This used to
        # surface as an unhandled 500.
        raise HTTPException(
            status_code=409,
            detail=(
                "This user has authored samples, storage configs or access grants and cannot be "
                "deleted without losing that history. Deactivate the account instead (edit user -> inactive)."
            ),
        )
    if deleted == "DELETE 0":  # said "deleted successfully"
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": "User deleted successfully"}


@router.post("/users/{id}/reset-password")
async def reset_password(
    id: int,
    # Read from the JSON body ({"password": "..."}), which is what the frontend
    # sends. Previously declared as a bare `str`, which FastAPI treats as a query
    # parameter — so the body was ignored and the endpoint never worked.
    password: str = Body(..., embed=True),
    admin: dict = Depends(require_admin),
):
    target_username = await db.fetchval("SELECT username FROM users WHERE id = $1", id)
    if target_username is None:
        raise HTTPException(status_code=404, detail="User not found")
    validate_new_password(password, target_username)
    password_hash = await hash_password(password)

    await db.execute(
        """
        UPDATE users
        SET password_hash = $1, must_change_password = true
        WHERE id = $2
        """,
        password_hash,
        id,
    )

    # Force the target user to re-authenticate: drop any sessions still tied to
    # the old password so an admin reset actually locks out an active session.
    await db.execute("DELETE FROM sessions WHERE user_id = $1", id)

    return {"message": "Password reset successfully"}
