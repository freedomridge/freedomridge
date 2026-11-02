"""Server-side password policy.

Only the React modal enforced a minimum; change-password, create-user and admin reset
accepted "" and "a" from the SDK / curl. One validator now guards all three, and it is
deliberately modest so the seeded dev/CI admin123 and the frontend's own minLength=8 keep
working. The live lock is python/fridge/test_sdk.py "Password policy".
"""

import asyncio

import bcrypt
import pytest
from fastapi import HTTPException

from backend.auth_policy import MIN_PASSWORD_LENGTH, validate_new_password
from backend.routers import auth as auth_router
from backend.routers import users as users_router


@pytest.mark.parametrize("bad", ["", "a", "short", "1234567"])
def test_too_short_is_rejected(bad):
    with pytest.raises(HTTPException) as ei:
        validate_new_password(bad, "alice")
    assert ei.value.status_code == 400 and "at least" in ei.value.detail


def test_username_as_password_is_rejected_case_insensitively():
    for pw in ("alexander", "ALEXANDER"):
        with pytest.raises(HTTPException) as ei:
            validate_new_password(pw, "alexander")
        assert ei.value.status_code == 400 and "username" in ei.value.detail


@pytest.mark.parametrize("pw", ["password", "Password1", "12345678", "qwertyui"])
def test_the_usual_suspects_are_rejected(pw):
    with pytest.raises(HTTPException) as ei:
        validate_new_password(pw, "alice")
    assert ei.value.status_code == 400 and "common" in ei.value.detail


@pytest.mark.parametrize("pw", ["admin123", "correct horse", "S3cure-pass!", "abcdefgh"])
def test_what_runs_today_still_passes(pw):
    # admin123 is the seeded dev/CI credential (e2e global-setup re-sets it via the API).
    validate_new_password(pw, "admin")


def test_min_length_matches_the_frontend_modals():
    assert MIN_PASSWORD_LENGTH == 8


class _Recorder:
    """Stubs db for the handlers: answers the row lookups, records writes."""

    def __init__(self, row=None, fetchval=None):
        self.row = row
        self._fetchval = fetchval
        self.executed: list = []

    async def fetchrow(self, query, *args):
        return self.row

    async def fetchval(self, query, *args):
        return self._fetchval

    async def execute(self, query, *args):
        self.executed.append(query)


def test_change_password_rejects_before_writing(monkeypatch):
    good_hash = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()
    rec = _Recorder(row={"password_hash": good_hash, "must_change_password": False, "is_demo_account": False})
    monkeypatch.setattr(auth_router, "db", rec)
    data = auth_router.ChangePasswordRequest(current_password="admin123", new_password="short")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(auth_router.change_password(data, None, {"id": 1, "username": "admin"}))  # type: ignore[arg-type]
    assert ei.value.status_code == 400
    assert rec.executed == []


def test_create_user_rejects_before_inserting(monkeypatch):
    rec = _Recorder(fetchval=None)  # username does not exist
    monkeypatch.setattr(users_router, "db", rec)
    user = users_router.UserCreate(username="bob", password="bob", role="guest")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(users_router.create_user(user, {"id": 1, "role": "admin"}))
    assert ei.value.status_code == 400
    assert rec.executed == []


def test_reset_password_rejects_the_username_and_unknown_users(monkeypatch):
    rec = _Recorder(fetchval="carol")
    monkeypatch.setattr(users_router, "db", rec)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(users_router.reset_password(7, "carol", {"id": 1, "role": "admin"}))
    assert ei.value.status_code == 400 and rec.executed == []
    monkeypatch.setattr(users_router, "db", _Recorder(fetchval=None))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(users_router.reset_password(7, "longenough1", {"id": 1, "role": "admin"}))
    assert ei.value.status_code == 404
