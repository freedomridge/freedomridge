"""must_change_password is enforced SERVER-SIDE.

A user flagged must_change_password may reach only change-password / me / logout — every other route
403s. Without this, a seeded default credential (admin/admin123 bootstrap) could use the API (mint a
non-expiring token, read/write data) before rotating the password; the frontend modal is UX, not a
security boundary.
"""

from typing import cast

import pytest
from fastapi import HTTPException, Request

from backend.routers.auth import _enforce_password_change


def _req(path: str) -> Request:
    """Minimal stand-in for a fastapi Request — only request.url.path is read by the gate."""
    return cast("Request", type("R", (), {"url": type("U", (), {"path": path})()})())


def test_flagged_user_blocked_on_non_exempt_routes():
    # data, token-mint, and any other authenticated route must 403 until the password is changed.
    for path in (
        "/api/datasets/1/samples/query",
        "/api/auth/tokens",
        "/api/datasets/1/sparse-sample",
        "/api/users",
    ):
        with pytest.raises(HTTPException) as e:
            _enforce_password_change({"must_change_password": True}, _req(path))
        assert e.value.status_code == 403


def test_flagged_user_allowed_to_change_check_logout():
    for path in ("/api/auth/change-password", "/api/auth/me", "/api/auth/logout"):
        _enforce_password_change({"must_change_password": True}, _req(path))  # must NOT raise


def test_normal_user_unaffected():
    _enforce_password_change({"must_change_password": False}, _req("/api/datasets/1/samples/query"))
    _enforce_password_change({}, _req("/api/datasets/1/samples/query"))  # missing key → falsy, allowed
