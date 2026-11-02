"""DELETE /api/users/{id} for a user who authored data is a 409 with guidance, not a 500.
The live lock is python/fridge/test_sdk.py "User delete".
"""

import asyncio

import asyncpg
import pytest
from fastapi import HTTPException

from backend.routers import users as users_router


class _DB:
    """Its own pool and connection; records statements and whether the transaction rolled back."""

    def __init__(self, raise_fk):
        self.raise_fk = raise_fk
        self.executed: list = []
        self.rolled_back = False

    async def execute(self, query, *args):
        self.executed.append(query)
        if self.raise_fk and query.startswith("DELETE FROM users"):
            raise asyncpg.ForeignKeyViolationError(
                "update or delete on table \"users\" violates foreign key constraint",
            )
        return "DELETE 1"

    @property
    def pool(self):
        return self

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, *rest):
        self.rolled_back = self.rolled_back or exc_type is not None
        return False


def test_referenced_user_is_a_409_with_deactivate_guidance(monkeypatch):
    monkeypatch.setattr(users_router, "db", _DB(raise_fk=True))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(users_router.delete_user(42, {"id": 1, "role": "admin"}))
    assert ei.value.status_code == 409
    assert "deactivate" in ei.value.detail.lower()


def test_view_cleanup_is_undone_when_the_user_cannot_be_deleted(monkeypatch):
    db = _DB(raise_fk=True)
    monkeypatch.setattr(users_router, "db", db)
    with pytest.raises(HTTPException):
        asyncio.run(users_router.delete_user(42, {"id": 1, "role": "admin"}))
    assert db.rolled_back  # the private views and share lists are left as they were


def test_unreferenced_user_still_deletes(monkeypatch):
    db = _DB(raise_fk=False)
    monkeypatch.setattr(users_router, "db", db)
    out = asyncio.run(users_router.delete_user(42, {"id": 1, "role": "admin"}))
    assert out["message"] == "User deleted successfully"
    # Regression: their private views go and their id leaves every share list, BEFORE the user row
    # (views.user_id is ON DELETE SET NULL: private views were left listed for nobody, undeletable).
    views_delete, share_prune, user_delete = db.executed
    assert views_delete.startswith("DELETE FROM views") and "is_public IS NOT TRUE" in views_delete
    assert "array_remove(shared_with_user_ids" in share_prune
    assert user_delete.startswith("DELETE FROM users")


def test_self_delete_is_still_refused_first(monkeypatch):
    monkeypatch.setattr(users_router, "db", _DB(raise_fk=True))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(users_router.delete_user(1, {"id": 1, "role": "admin"}))
    assert ei.value.status_code == 400
