"""Bulk tagging stamps who/when on the samples it touches and keeps the per-dataset tag registry:
first sight = created_at/created_by, every later apply = last_used_at. tag-counts
returns that registry info and registers tags it has never seen with unknown dates.
Fake-DB tests of the SQL the handlers issue; the round-trip is validated live by test_sdk."""

import asyncio
import datetime

from backend.routers import datasets
from backend.routers.datasets import BulkTagRequest, TagCountsRequest

USER = {"id": 42, "username": "alex", "role": "admin"}


class _NullCM:
    def __init__(self, v):
        self._v = v

    async def __aenter__(self):
        return self._v

    async def __aexit__(self, *exc):
        return False


class FakeDB:
    def __init__(self, registry_rows=None):
        self.calls: list = []
        self.registry_rows = registry_rows or []
        self.pool = self

    def acquire(self):
        return _NullCM(self)

    def transaction(self):
        return _NullCM(None)

    async def execute(self, query, *args, **kw):
        self.calls.append(("execute", " ".join(query.split()), args))
        return "UPDATE 3"

    async def fetchval(self, query, *args, **kw):
        self.calls.append(("fetchval", " ".join(query.split()), args))
        return 3  # sample-count check in bulk-tag

    async def fetchrow(self, query, *args, **kw):
        self.calls.append(("fetchrow", " ".join(query.split()), args))
        return {'updated_count': 3, 'selection_live': True}

    async def fetch(self, query, *args, **kw):
        q = " ".join(query.split())
        self.calls.append(("fetch", q, args))
        if "FROM dataset_tags t" in q:
            return self.registry_rows
        if "SELECT DISTINCT unnest" in q:
            return [{"tag": "seed"}, {"tag": "dated"}]
        if "count(DISTINCT s.id)" in q:
            return [{"tag": "dated", "cnt": 2}]
        return []


def _setup(monkeypatch, db):
    async def _cols():
        return {"tags", "inferred_tags"}

    monkeypatch.setattr(datasets, "get_array_columns", _cols)
    monkeypatch.setattr(datasets, "db", db)


def _executes(db, needle):
    return [c for c in db.calls if c[0] == "execute" and needle in c[1]]


def test_add_tags_stamps_the_rows_and_registers_the_tags(monkeypatch):
    db = FakeDB()
    _setup(monkeypatch, db)
    req = BulkTagRequest(sample_ids=[1, 2, 3], add_tags=["dated", "other"])
    out = asyncio.run(datasets.bulk_tag_samples(7, req, user=USER))
    assert out["updated_count"] == 3
    (upd,) = [call for call in db.calls if call[0] == 'fetchrow' and 'UPDATE samples' in call[1]]
    assert 'ORDER BY id FOR UPDATE' in upd[1]
    assert "last_modified_at = NOW(), modified_by_user_id = $5" in upd[1]
    assert upd[2] == ([1, 2, 3], 7, ["dated", "other"], [], 42)
    (reg,) = _executes(db, "INSERT INTO dataset_tags")
    assert "ON CONFLICT (dataset_id, field_name, tag) DO UPDATE SET last_used_at" in reg[1]
    assert reg[2] == (7, "tags", ["dated", "other"], 42)


def test_remove_tags_stamps_the_rows_too(monkeypatch):
    db = FakeDB()
    _setup(monkeypatch, db)
    req = BulkTagRequest(selection_token="tok", excluded_ids=[9], remove_tags=["dated"])
    asyncio.run(datasets.bulk_tag_samples(7, req, user=USER))
    (upd,) = [call for call in db.calls if call[0] == 'fetchrow' and 'EXCEPT SELECT unnest' in call[1]]
    assert "last_modified_at = NOW(), modified_by_user_id = $6" in upd[1]
    assert upd[2][-1] == 42
    assert _executes(db, "INSERT INTO dataset_tags") == []  # removing never (re)registers


def test_tag_counts_returns_registry_info_and_registers_unknown_tags(monkeypatch):
    created = datetime.datetime(2026, 10, 3, 12, 0, tzinfo=datetime.timezone.utc)
    db = FakeDB(
        registry_rows=[
            {"tag": "dated", "created_at": created, "last_used_at": created, "created_by": "alex"},
            {"tag": "seed", "created_at": None, "last_used_at": None, "created_by": None},
        ],
    )
    _setup(monkeypatch, db)
    out = asyncio.run(datasets.get_tag_counts(7, TagCountsRequest(sample_ids=[1, 2])))
    assert out["all_tags"] == ["seed", "dated"]
    assert out["tag_counts"] == {"seed": 0, "dated": 2}
    assert out["tag_info"]["dated"] == {
        "created_at": "2026-10-03T12:00:00+00:00",
        "created_by": "alex",
        "last_used_at": "2026-10-03T12:00:00+00:00",
    }
    # A tag the registry never saw (ingested with the sample) is honest: unknown, not missing.
    assert out["tag_info"]["seed"] == {"created_at": None, "created_by": None, "last_used_at": None}
    (heal,) = _executes(db, "INSERT INTO dataset_tags (dataset_id, field_name, tag) SELECT $1, $2, unnest")
    assert heal[2] == (7, "tags", ["seed", "dated"])


def test_tag_counts_survives_a_registry_failure(monkeypatch):
    db = FakeDB()

    async def boom(query, *args, **kw):
        if "dataset_tags" in query:
            raise RuntimeError("relation does not exist")
        return "INSERT 0 0"

    db.execute = boom  # type: ignore[method-assign]
    _setup(monkeypatch, db)
    out = asyncio.run(datasets.get_tag_counts(7, TagCountsRequest(sample_ids=[1])))
    assert out["tag_counts"] == {"seed": 0, "dated": 2}
    assert out["tag_info"] == {}
