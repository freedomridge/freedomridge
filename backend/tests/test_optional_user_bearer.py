"""get_optional_user: which failed authentications degrade to anonymous."""

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.routers import auth


def _request(headers=None, cookies=None):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        raw_headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw_headers})


def _get_current_user_raising(status_code, detail):
    async def get_current_user(request):
        raise HTTPException(status_code=status_code, detail=detail)

    return get_current_user


def _optional_user(monkeypatch, request, get_current_user):
    monkeypatch.setattr(auth, "get_current_user", get_current_user)
    return asyncio.run(auth.get_optional_user(request))


def test_no_credentials_is_anonymous(monkeypatch):
    request = _request()

    assert _optional_user(monkeypatch, request, _get_current_user_raising(401, "Not authenticated")) is None


def test_stale_session_cookie_is_anonymous(monkeypatch):
    request = _request(cookies={"session_id": "expired"})

    assert _optional_user(monkeypatch, request, _get_current_user_raising(401, "Session expired")) is None


def test_invalid_bearer_token_is_rejected(monkeypatch):
    request = _request(headers={"Authorization": "Bearer not-a-real-token"})

    with pytest.raises(HTTPException) as raised:
        _optional_user(monkeypatch, request, _get_current_user_raising(401, "Invalid or expired token"))
    assert raised.value.status_code == 401


def test_inactive_account_is_rejected(monkeypatch):
    request = _request(cookies={"session_id": "valid"})

    with pytest.raises(HTTPException) as raised:
        _optional_user(monkeypatch, request, _get_current_user_raising(403, "Account is inactive"))
    assert raised.value.status_code == 403


def test_valid_credentials_return_the_user(monkeypatch):
    user = {"id": 1, "username": "admin"}

    async def get_current_user(request):
        return user

    request = _request(headers={"Authorization": "Bearer valid-token"})

    assert _optional_user(monkeypatch, request, get_current_user) == user
