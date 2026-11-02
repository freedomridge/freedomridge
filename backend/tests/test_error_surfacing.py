"""Errors must carry their cause to the user, not strip it.

Three traps this locks (all hit for real during the eurosat showcase build):
  1. SDK: requests' default HTTPError discards the server's JSON `detail` — a broken
     endpoint read as literally "Internal Server Error". `_raise_for_status` keeps it.
  2. SDK: a Secure session cookie issued over plain http is silently dropped, so login
     "succeeds" and every later call 401s with no explanation. login() must fail loudly
     at the point of cause.
  3. Backend: an unhandled exception returned a bare string with no diagnosable content.
     The global handler returns {detail, error_id} and logs the traceback under that id.

Self-contained (synthetic responses / direct handler call), matching the suite; no live server.
"""

import asyncio
import json

import pytest
import requests
from fridge.core.client import Client, _raise_for_status
from starlette.requests import Request

from backend.main import unhandled_exception_handler


def _resp(status, body=b"", url="http://h/api/thing", method="POST", reason="Server Error"):
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.url = url
    r.reason = reason
    r.request = requests.Request(method=method, url=url).prepare()
    return r


def test_json_detail_reaches_the_message():
    resp = _resp(500, b'{"detail": "vector_dims mismatch: expected 512"}')
    with pytest.raises(requests.HTTPError) as ei:
        _raise_for_status(resp)
    msg = str(ei.value)
    assert "vector_dims mismatch: expected 512" in msg
    assert "500" in msg and "POST" in msg and "http://h/api/thing" in msg
    assert ei.value.response is resp  # same type + response attr callers already rely on


def test_non_json_body_reaches_the_message():
    resp = _resp(502, b"upstream exploded", reason="Bad Gateway")
    with pytest.raises(requests.HTTPError, match="upstream exploded"):
        _raise_for_status(resp)


def test_401_includes_the_auth_hint():
    resp = _resp(401, b'{"detail": "Not authenticated"}', reason="Unauthorized")
    with pytest.raises(requests.HTTPError, match="FRIDGE_API_TOKEN"):
        _raise_for_status(resp)


def test_ok_response_does_not_raise():
    _raise_for_status(_resp(200, b"{}", reason="OK"))


def _login_resp(secure):
    r = _resp(200, b'{"message": "ok"}', url="http://h/api/auth/login", reason="OK")
    r.cookies.set("session_id", "abc", secure=secure)
    return r


def test_login_rejects_secure_cookie_over_http(monkeypatch):
    client = Client(base_url="http://h/api")
    monkeypatch.setattr(client.session, "post", lambda *a, **kw: _login_resp(secure=True))
    with pytest.raises(RuntimeError, match="SESSION_COOKIE_SECURE"):
        client.login("admin", "pw")


def test_login_accepts_normal_cookie_over_http(monkeypatch):
    client = Client(base_url="http://h/api")
    monkeypatch.setattr(client.session, "post", lambda *a, **kw: _login_resp(secure=False))
    assert client.login("admin", "pw") == {"message": "ok"}


def _invoke_handler():
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/datasets/1/embeddings/reduce",
        "headers": [],
        "query_string": b"",
        "server": ("test", 80),
        "scheme": "http",
    }
    exc = ValueError("Numba needs NumPy 2.3 or less")
    response = asyncio.run(unhandled_exception_handler(Request(scope), exc))
    return response, json.loads(response.body)


def test_unhandled_exception_returns_diagnosable_json(monkeypatch):
    monkeypatch.delenv("FRIDGE_ENV", raising=False)
    response, body = _invoke_handler()
    assert response.status_code == 500
    assert "ValueError: Numba needs NumPy 2.3 or less" in body["detail"]
    assert body["error_id"] in body["detail"]  # greppable in the server log


def test_production_withholds_exception_text_but_keeps_error_id(monkeypatch):
    monkeypatch.setenv("FRIDGE_ENV", "production")
    _, body = _invoke_handler()
    assert "Numba" not in body["detail"]
    assert body["error_id"] and body["error_id"] in body["detail"]


def test_prod_spelling_also_withholds_exception_text(monkeypatch):
    # crypto.py accepts FRIDGE_ENV=prod as production (strict key checks boot), so the
    # 500 handler must treat it as production too — not leak internals on that spelling.
    monkeypatch.setenv("FRIDGE_ENV", "prod")
    _, body = _invoke_handler()
    assert "Numba" not in body["detail"]
