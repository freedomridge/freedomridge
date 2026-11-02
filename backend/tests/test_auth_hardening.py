"""Login throttle + oracle hardening .

regression: the throttle keyed on client-supplied X-Real-IP / X-Forwarded-For from ANY peer, so a client
of the directly-served dev backend sent a fresh IP per attempt and was never throttled.
regression: a deactivated account answered "inactive" before the password was checked (an existence
oracle that never counted toward the throttle); an anonymous caller got 404-vs-401 per dataset id.

Self-contained (asyncio.run) so it needs no pytest-asyncio plugin.
"""

import asyncio
import ipaddress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import bcrypt
import pytest
from fastapi import HTTPException, Response

from backend import access, public_settings
from backend.routers import auth


def _req(peer, **headers):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers={k.replace("_", "-"): v for k, v in headers.items()},
    )


def test_forwarding_headers_ignored_from_an_untrusted_peer(monkeypatch):
    monkeypatch.setattr(auth, "_TRUSTED_PROXIES", [])
    assert auth._client_ip(_req("100.64.1.2", x_real_ip="10.0.0.7")) == "100.64.1.2"
    assert auth._client_ip(_req("100.64.1.2", x_forwarded_for="1.1.1.1, 10.0.0.7")) == "100.64.1.2"


def test_forwarding_headers_honoured_from_a_trusted_proxy(monkeypatch):
    monkeypatch.setattr(auth, "_TRUSTED_PROXIES", [ipaddress.ip_network("172.16.0.0/12")])
    assert auth._client_ip(_req("172.18.0.5", x_real_ip="203.0.113.9")) == "203.0.113.9"
    assert auth._client_ip(_req("172.18.0.5", x_forwarded_for="1.1.1.1, 203.0.113.9")) == "203.0.113.9"
    assert auth._client_ip(_req("172.18.0.5")) == "172.18.0.5"


def test_trusted_proxies_env_parsing(monkeypatch):
    monkeypatch.setenv("FRIDGE_TRUSTED_PROXIES", " 10.0.0.0/8, bogus ,::1 ")
    nets = auth._trusted_proxy_networks()
    assert [str(n) for n in nets] == ["10.0.0.0/8", "::1/128"]
    monkeypatch.delenv("FRIDGE_TRUSTED_PROXIES")
    assert auth._trusted_proxy_networks() == []  # default: trust no proxy


class _FakeDB:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, query, *args):
        return self.row


_HASH = bcrypt.hashpw(b"right-password", bcrypt.gensalt(rounds=4)).decode()


def _login(monkeypatch, password, is_active=False):
    monkeypatch.setattr(auth, "db", _FakeDB({"id": 3, "password_hash": _HASH, "is_active": is_active}))
    monkeypatch.setattr(auth, "_TRUSTED_PROXIES", [])
    creds = auth.LoginRequest(username="zzz_inactive", password=password)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.login(creds, _req("100.64.9.9"), Response()))
    return exc.value


def test_inactive_account_with_wrong_password_is_just_invalid_credentials(monkeypatch):
    auth._LOGIN_FAILS.clear()
    err = _login(monkeypatch, "wrong")
    assert (err.status_code, err.detail) == (401, "Invalid credentials")
    assert len(auth._LOGIN_FAILS["100.64.9.9:zzz_inactive"]) == 1  # counted toward the throttle


def test_inactive_account_with_right_password_says_inactive(monkeypatch):
    auth._LOGIN_FAILS.clear()
    err = _login(monkeypatch, "right-password")
    assert (err.status_code, err.detail) == (403, "Account is inactive")


def test_anonymous_caller_cannot_tell_which_dataset_ids_exist(monkeypatch):
    class NoDataset:
        async def fetchrow(self, query, *args):
            return None

    monkeypatch.setattr(access, "db", NoDataset())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(access.check_dataset_access(999999, None))
    assert exc.value.status_code == 401  # was 404 for a missing id, 401 for an existing one


@pytest.fixture
def demo_connection(monkeypatch):
    monkeypatch.setenv('DEMO_MODE', 'true')
    monkeypatch.setattr(auth, '_TRUSTED_PROXIES', [ipaddress.ip_network('172.16.0.0/12')])
    monkeypatch.setattr(
        public_settings,
        'settings',
        public_settings.PublicSettings(
            demo_account_cap=5,
            demo_ip_hourly=1,
            demo_ip_live=1,
            demo_ip_allowlist=(ipaddress.ip_network('203.0.113.0/24'), ipaddress.ip_network('2001:db8::/32')),
        ),
    )
    connection = MagicMock()
    connection.fetchrow = AsyncMock(return_value={'recent': 1, 'live': 1})
    connection.fetchval = AsyncMock(side_effect=[0, 42])
    connection.execute = AsyncMock()
    database = MagicMock()
    database.pool.acquire.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(auth, 'db', database)
    return connection


@pytest.mark.parametrize('client_ip', ['203.0.113.9', '2001:db8::9'])
def test_allowlisted_demo_bypasses_both_ip_limits_and_records_admission(demo_connection, client_ip):
    response = Response()
    result = asyncio.run(auth.demo_login(_req('172.18.0.5', x_real_ip=client_ip), response))
    assert result['message'] == 'Demo account created'
    assert 'session_id=' in response.headers['set-cookie']
    demo_connection.execute.assert_any_await(
        'INSERT INTO demo_admissions(client_ip,user_id) VALUES ($1,$2)',
        client_ip,
        42,
    )
    assert any('INSERT INTO sessions' in call.args[0] for call in demo_connection.execute.await_args_list)


@pytest.mark.parametrize('client_ip', ['203.0.113.9', '192.0.2.9'])
def test_global_demo_cap_applies_to_every_ip(demo_connection, client_ip):
    demo_connection.fetchval.side_effect = [5]
    if client_ip == '192.0.2.9':
        demo_connection.fetchrow.return_value = {'recent': 0, 'live': 0}
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.demo_login(_req('172.18.0.5', x_real_ip=client_ip), Response()))
    assert error.value.status_code == 503
    assert 'guest account limit' in error.value.detail
    assert error.value.headers == {'Retry-After': '300'}
    assert not any('INSERT' in call.args[0] for call in demo_connection.execute.await_args_list)


@pytest.mark.parametrize('recent,live,retry_after', [(1, 0, '3600'), (0, 1, '300')])
@pytest.mark.parametrize('peer,forwarded', [('172.18.0.5', '192.0.2.9'), ('192.0.2.9', '203.0.113.9')])
def test_unlisted_demo_ip_still_limited(demo_connection, recent, live, retry_after, peer, forwarded):
    demo_connection.fetchrow.return_value = {'recent': recent, 'live': live}
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.demo_login(_req(peer, x_real_ip=forwarded), Response()))
    assert error.value.status_code == 429
    assert error.value.headers == {'Retry-After': retry_after}
    assert not any('INSERT' in call.args[0] for call in demo_connection.execute.await_args_list)
