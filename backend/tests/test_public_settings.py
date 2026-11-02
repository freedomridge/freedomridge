"""Public-demo settings fail closed before opening a database connection."""

import asyncio
import ipaddress
from unittest.mock import AsyncMock

import asyncpg
import pytest

from backend import public_settings
from backend.database import Database


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    monkeypatch.setattr(public_settings, 'settings', public_settings.settings)
    monkeypatch.delenv('FRIDGE_DEMO_IP_ALLOWLIST', raising=False)


def test_guest_ip_defaults(monkeypatch):
    monkeypatch.delenv('FRIDGE_DEMO_IP_HOURLY_LIMIT', raising=False)
    monkeypatch.delenv('FRIDGE_DEMO_IP_LIVE_CAP', raising=False)
    for settings in (public_settings.PublicSettings(), public_settings.load_settings()):
        assert settings.demo_ip_hourly == 5
        assert settings.demo_ip_live == 10
        assert settings.demo_ip_allowlist == ()


@pytest.mark.parametrize('value', [None, '', ' , \t\n, '])
def test_empty_allowlist(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv('FRIDGE_DEMO_IP_ALLOWLIST', value)
    settings = public_settings.load_settings()
    assert settings.demo_ip_allowlist == ()
    assert not settings.ip_allowlisted('203.0.113.1')


def test_allowlist_addresses_networks_and_separators(monkeypatch):
    monkeypatch.setenv('FRIDGE_DEMO_IP_ALLOWLIST', '203.0.113.7, 2001:db8::1\t192.0.2.0/24,\n2001:db8:1::/48')
    settings = public_settings.load_settings()
    assert settings.demo_ip_allowlist == tuple(
        ipaddress.ip_network(entry) for entry in ('203.0.113.7', '2001:db8::1', '192.0.2.0/24', '2001:db8:1::/48')
    )
    for address in ('203.0.113.7', '2001:db8::1', '192.0.2.99', '2001:db8:1::123'):
        assert settings.ip_allowlisted(address)
    for address in ('203.0.113.8', '2001:db8::2', '192.0.3.1', '2001:db8:2::1', '?'):
        assert not settings.ip_allowlisted(address)


@pytest.mark.parametrize(
    'entry',
    ['bogus', '999.1.2.3', '203.0.113.0/33', '2001:db8::/129', '0.0.0.0/0', '::/0', '192.0.2.1/24', '2001:db8::1/32'],
)
def test_invalid_allowlist_fails_before_database(monkeypatch, entry):
    monkeypatch.setenv('FRIDGE_DEMO_IP_ALLOWLIST', f'203.0.113.7, {entry}')
    pool = AsyncMock()
    monkeypatch.setattr(asyncpg, 'create_pool', pool)
    with pytest.raises(ValueError, match='FRIDGE_DEMO_IP_ALLOWLIST') as error:
        asyncio.run(Database().connect())
    assert entry in str(error.value)
    pool.assert_not_awaited()
