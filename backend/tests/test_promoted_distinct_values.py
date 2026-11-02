"""A promoted (indexed) field must serve the COMPLETE distinct value set via an index
skip-scan, never the bounded dataset_field_stats.unique_values cache. That cache is built
from only the 100k most-recent rows, which silently dropped Brazil/France/Ukraine/Mexico
from the country field in a large dataset (their rows are all older than the recent window) even though the field
was indexed. These lock _promoted_distinct_values — the skip-scan primitive behind the fix.

Self-contained (asyncio.run + a db.fetch stub), matching the suite; no live DB needed.
"""

import asyncio

import asyncpg

from backend.routers import datasets


class _FetchDB:
    """Stub for datasets.db: db.fetch returns preset rows, or raises to exercise the
    error fallback. Records queries so we can assert the skip-scan (not the cache) ran."""

    def __init__(self, rows=None, error=False):
        self._rows = rows or []
        self._error = error
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        if self._error:
            raise asyncpg.PostgresError("simulated skip-scan failure")
        return self._rows


def _values(monkeypatch, rows_or_db, field, is_numeric):
    db = rows_or_db if isinstance(rows_or_db, _FetchDB) else _FetchDB(rows_or_db)
    monkeypatch.setattr(datasets, "db", db)
    result = asyncio.run(datasets._promoted_distinct_values(7, field, is_numeric))
    return result, db


def test_returns_complete_set_via_index_skipscan(monkeypatch):
    rows = [{"v": c} for c in ["Argentina", "Brazil", "United States of America"]]
    (values, truncated), db = _values(monkeypatch, rows, "country", False)
    assert values == ["Argentina", "Brazil", "United States of America"]
    assert truncated is False
    # Must skip-scan the index over the field expression — not read the cached list.
    assert "RECURSIVE" in db.queries[0]
    assert "metadata->>'country'" in db.queries[0]


def test_truncates_high_cardinality_field(monkeypatch):
    # A high-cardinality promoted field (e.g. source_id, ~millions distinct) must stop at the
    # cap and flag truncation rather than materialize the whole column.
    cap = datasets._ENUM_VALUE_CAP
    rows = [{"v": f"id{i:05d}"} for i in range(cap + 1)]
    (values, truncated), _ = _values(monkeypatch, rows, "source_id", False)
    assert len(values) == cap
    assert truncated is True


def test_formats_numeric_enum_values(monkeypatch):
    rows = [{"v": 1970.0}, {"v": 2025.0}, {"v": 2026.5}]
    (values, truncated), _ = _values(monkeypatch, rows, "year", True)
    assert values == ["1970", "2025", "2026.5"]  # whole numbers render without a trailing .0
    assert truncated is False


def test_falls_back_to_none_on_db_error(monkeypatch):
    # If the skip-scan errors or exceeds the budget (e.g. a field flagged promoted whose index
    # is missing degrades to a seq scan), return None so the caller keeps serving the cached
    # list instead of failing the whole sidebar.
    result, _ = _values(monkeypatch, _FetchDB(error=True), "country", False)
    assert result is None
