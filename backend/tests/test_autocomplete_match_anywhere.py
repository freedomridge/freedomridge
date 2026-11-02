"""The value typeahead matches ANYWHERE in a value.

The picker filters its cached list by substring, but the server scan it falls back to matched
only the start (`ILIKE 'q%'`), so "Forest" found nothing on `…/Forest/Forest_1011.tif` once the
cached list ran out — and so did the timeout fallback. Typed `%` / `_` must stay literal.
"""

import asyncio

import asyncpg

from backend.routers import datasets


class FakeDB:
    def __init__(self, *, cached=(), scan_times_out=False):
        self.cached = list(cached)
        self.scan_times_out = scan_times_out
        self.scans: list = []

    async def fetchval(self, query, *args):
        if "is_promoted" in query:
            return False
        if "unique_values" in query:
            return self.cached
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetch(self, query, *args):
        self.scans.append((query, args))
        if self.scan_times_out:
            raise asyncpg.PostgresError("simulated autocomplete budget timeout")
        return []


def _run(db, q, monkeypatch):
    async def _no_array_cols(dataset_id):
        return set()

    monkeypatch.setattr(datasets, "get_dataset_array_columns", _no_array_cols)
    monkeypatch.setattr(datasets, "db", db)
    return asyncio.run(datasets._autocomplete_impl(293, "source_tif", q, 100, None))


def test_the_scan_matches_anywhere_in_the_value(monkeypatch):
    db = FakeDB()
    _run(db, "Forest", monkeypatch)
    query, args = db.scans[0]
    assert "%Forest%" in args
    assert "ILIKE" in query and "ESCAPE" in query


def test_typed_wildcards_are_literal(monkeypatch):
    db = FakeDB()
    _run(db, "50%_x", monkeypatch)
    assert "%50\\%\\_x%" in db.scans[0][1]


def test_the_timeout_fallback_also_matches_anywhere(monkeypatch):
    cached = ["/data/Forest/Forest_1.tif", "/data/River/River_2.tif"]
    out = _run(FakeDB(cached=cached, scan_times_out=True), "forest", monkeypatch)
    assert [v["value"] for v in out["values"]] == ["/data/Forest/Forest_1.tif"]
    assert out["exhaustive"] is False
