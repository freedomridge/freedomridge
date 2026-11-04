"""GET /datasets/{id}/missing: the after_id cursor, checked against the SQL it sends (no database)."""

import pytest

from backend.routers import datasets


class RecordingDB:
    def __init__(self):
        self.fetches = []
        self.fetchvals = []

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        return [{"id": 11, "filepath": "a.jpg"}]

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        return 5


@pytest.fixture
def recording_db(monkeypatch):
    fake = RecordingDB()
    monkeypatch.setattr(datasets, "db", fake)
    return fake


async def call(**kwargs):
    # Bypass the route decorators: they only add access checks and the guest-heavy gate.
    endpoint = getattr(datasets.dataset_missing, "__wrapped__", datasets.dataset_missing)
    return await endpoint(id=7, kind="embedding", field="clip", **kwargs)


@pytest.mark.asyncio
async def test_after_id_adds_an_id_cursor_to_the_page_but_not_the_total(recording_db):
    out = await call(limit=50, offset=0, after_id=10)
    sql, args = recording_db.fetches[0]
    assert "AND s.id > $3" in sql and "ORDER BY s.id LIMIT $4 OFFSET $5" in sql
    assert args == (7, "clip", 10, 50, 0)
    total_sql, total_args = recording_db.fetchvals[0]
    assert "s.id >" not in total_sql and total_args == (7, "clip")
    assert out == {"items": [{"id": 11, "filepath": "a.jpg"}], "total": 5}


@pytest.mark.asyncio
async def test_without_after_id_the_query_is_unchanged(recording_db):
    await call(limit=50, offset=3)
    sql, args = recording_db.fetches[0]
    assert "s.id >" not in sql and "LIMIT $3 OFFSET $4" in sql
    assert args == (7, "clip", 50, 3)


@pytest.mark.asyncio
async def test_location_kind_with_cursor(recording_db):
    endpoint = getattr(datasets.dataset_missing, "__wrapped__", datasets.dataset_missing)
    await endpoint(id=7, kind="location", field=None, limit=10, offset=0, after_id=4)
    sql, args = recording_db.fetches[0]
    assert "s.location IS NULL AND s.id > $2" in sql
    assert args == (7, 4, 10, 0)
