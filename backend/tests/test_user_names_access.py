"""Auth contract for the user-directory endpoints.

/users/names must be reachable by ANY logged-in user — the view Share modal builds
its target list from it. An admin-only gate here silently breaks per-user sharing
for non-admin owners (shipped as a real bug: the modal swallowed the 403 and showed
"No other users found"). The full /users listing (roles, emails, activity) must
stay admin-only via require_admin.

Self-contained (asyncio.run + stubs, no TestClient/httpx) to match the rest of the
suite and CI's minimal deps.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend.routers import users as users_router

GUEST = {"id": 5, "username": "guest", "role": "guest"}
ADMIN = {"id": 1, "username": "admin", "role": "admin"}


class FakeDB:
    async def fetch(self, query, *args):
        return [{"id": 1, "username": "alice"}, {"id": 5, "username": "guest"}]


def test_names_has_no_admin_gate_and_returns_directory(monkeypatch):
    # list_user_names depends only on get_current_user (login), not require_admin —
    # so a non-admin gets the id+username directory.
    monkeypatch.setattr(users_router, "db", FakeDB())
    rows = asyncio.run(users_router.list_user_names(user=GUEST))
    assert rows == [{"id": 1, "username": "alice"}, {"id": 5, "username": "guest"}]


def test_full_user_list_stays_admin_only():
    # require_admin (guarding GET /users) rejects non-admins and admits admins.
    with pytest.raises(HTTPException) as ei:
        users_router.require_admin(user=GUEST)
    assert ei.value.status_code == 403
    assert users_router.require_admin(user=ADMIN) is ADMIN
