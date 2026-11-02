"""Dataset access control — login is required for ALL access (no anonymous reads,
even of is_global "public" datasets). Guards the auth-gate added 2026-06-11.

Self-contained (asyncio.run) so it needs no pytest-asyncio plugin.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend import access


class FakeDB:
    """Stub for check_dataset_access's two queries: the dataset row (fetchrow) and
    the dataset_access grant lookup (fetchval)."""

    def __init__(self, dataset_row, grant=None):
        self._dataset_row = dataset_row
        self._grant = grant

    async def fetchrow(self, query, *args):
        return self._dataset_row

    async def fetchval(self, query, *args):
        return self._grant


GLOBAL = {"id": 1, "name": "g", "storage_config_id": None, "owner_id": 9, "is_global": True}
PRIVATE = {"id": 2, "name": "p", "storage_config_id": None, "owner_id": 9, "is_global": False}
GUEST = {"id": 5, "role": "guest"}
ADMIN = {"id": 1, "role": "admin"}
OWNER = {"id": 9, "role": "guest"}


def _access(monkeypatch, dataset_row, user, grant=None):
    monkeypatch.setattr(access, "db", FakeDB(dataset_row, grant))
    return asyncio.run(access.check_dataset_access(dataset_row["id"], user))


def test_anonymous_denied_even_for_global(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _access(monkeypatch, GLOBAL, None)
    assert ei.value.status_code == 401  # 401 -> frontend redirects to login


def test_anonymous_denied_for_private(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _access(monkeypatch, PRIVATE, None)
    assert ei.value.status_code == 401


def test_logged_in_user_reads_global(monkeypatch):
    assert _access(monkeypatch, GLOBAL, GUEST)["id"] == 1


def test_non_member_denied_for_private(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _access(monkeypatch, PRIVATE, GUEST, grant=None)
    assert ei.value.status_code == 403


def test_owner_reads_private(monkeypatch):
    assert _access(monkeypatch, PRIVATE, OWNER)["id"] == 2


def test_granted_user_reads_private(monkeypatch):
    assert _access(monkeypatch, PRIVATE, GUEST, grant=1)["id"] == 2


def test_admin_reads_private(monkeypatch):
    assert _access(monkeypatch, PRIVATE, ADMIN)["id"] == 2


def test_filter_anonymous_sees_nothing():
    sql, args = asyncio.run(access.filter_datasets_for_user(None))
    assert sql == "FALSE" and args == []


def test_filter_admin_sees_all():
    sql, args = asyncio.run(access.filter_datasets_for_user(ADMIN))
    assert sql == "TRUE"
