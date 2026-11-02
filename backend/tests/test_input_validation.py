"""Names and tags are validated at the API edge.

Previously, empty / whitespace usernames could log in; an unknown role 500'd with the new
password's bcrypt hash in the detail; blank or case-duplicate
dataset / version / view / token names, unknown share targets, tokens that never or already
expired; updating or deleting a missing user said "success"; tags stored
verbatim.
"""

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.routers import datasets, tokens, users, views
from backend.validation import NAME_MAX, clean_name, clean_tags

ADMIN = {"id": 1, "role": "admin"}


class FakeDB:
    def __init__(self, fetchval=None, execute="UPDATE 1", fetch=None):
        self._fetchval, self._execute, self._fetch = fetchval, execute, fetch or []
        self.queries: list[str] = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return self._fetchval

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        return {"user_id": 1}

    async def fetch(self, query, *args):
        self.queries.append(query)
        return self._fetch

    async def execute(self, query, *args):
        self.queries.append(query)
        return self._execute

    # delete_user runs in a transaction on an acquired connection: the fake is its own pool/conn.
    @property
    def pool(self):
        return self

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_blank_names_are_refused(raw):
    with pytest.raises(HTTPException) as exc:
        clean_name(raw, "Username")
    assert exc.value.status_code == 400 and exc.value.detail == "Username must not be empty"


def test_names_are_trimmed_and_bounded():
    assert clean_name("  rv_a ", "View name") == "rv_a"
    assert clean_name("x" * NAME_MAX, "View name") == "x" * NAME_MAX
    with pytest.raises(HTTPException):
        clean_name("x" * (NAME_MAX + 1), "View name")


def test_tags_are_trimmed_deduplicated_and_never_blank():
    assert clean_tags(["a", "a", "", "  ", " b ", "b"]) == ["a", "b"]
    assert datasets.SampleCreate(filepath="x.jpg", tags=["a", "a", "", "  ", "b"]).tags == ["a", "b"]


def test_blank_username_never_reaches_the_db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(users, "db", fake)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(users.create_user(users.UserCreate(username="   ", password="longenough1"), admin=ADMIN))
    assert exc.value.status_code == 400 and fake.queries == []


def test_unknown_role_is_a_422_before_any_hashing():
    with pytest.raises(ValidationError):
        users.UserCreate(username="zzz", password="longenough1", role="superuser")
    with pytest.raises(ValidationError):
        users.UserUpdate(role="superuser")


def test_updating_or_deleting_a_missing_user_is_a_404(monkeypatch):
    monkeypatch.setattr(users, "db", FakeDB(execute="UPDATE 0"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(users.update_user(999999, users.UserUpdate(role="guest"), admin=ADMIN))
    assert exc.value.status_code == 404
    monkeypatch.setattr(users, "db", FakeDB(execute="DELETE 0"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(users.delete_user(999999, admin=ADMIN))
    assert exc.value.status_code == 404


@pytest.mark.parametrize("days", [0, -1])
def test_token_expiry_must_be_at_least_a_day(days):
    with pytest.raises(ValidationError):
        tokens.TokenCreate(name="t", expires_days=days)
    assert tokens.TokenCreate(name="t", expires_days=None).expires_days is None  # null = never
    assert tokens.TokenCreate(name="t").expires_days == 90


def test_dataset_name_taken_ignoring_case(monkeypatch):
    monkeypatch.setattr(datasets, "db", FakeDB(fetchval=1))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(datasets._clean_dataset_name("TEST"))
    assert exc.value.status_code == 400
    monkeypatch.setattr(datasets, "db", FakeDB(fetchval=None))
    assert asyncio.run(datasets._clean_dataset_name("  test  ")) == "test"


def test_sharing_with_an_unknown_user_is_refused(monkeypatch):
    monkeypatch.setattr(views, "db", FakeDB(fetch=[{"id": 15}]))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(views._require_known_users([15, 999999]))
    assert exc.value.status_code == 400 and "999999" in exc.value.detail
    asyncio.run(views._require_known_users([15]))
