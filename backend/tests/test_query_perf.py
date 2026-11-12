"""Query-perf fixes: promoted numeric sort must ride its index, and per-keystroke
autocomplete must be budget-bounded with a cached fallback.

Self-contained (asyncio.run + stubs), matching the suite.
"""

import asyncio

import asyncpg

from backend.routers import datasets

# --- promoted numeric sort uses the float index --------------------------
# A promoted numeric field's index is ((metadata->>'f')::float); the guarded CASE
# expression doesn't match it, so promoted sorts emit the bare cast. DESC NULLS LAST doesn't
# match the btree's null order, so the pages are read in two parts (datasets._sorted_page_ids)
# rather than every sorted page seq-scanning and sorting 2M rows.


def test_promoted_numeric_sort_uses_bare_cast_and_missing_values_last():
    # Missing values last in both directions (the owner's rule); the pages still ride the index, in two parts
    # (datasets._sorted_page_ids).
    asc = datasets._numeric_sort_order_by("year", True, "ASC")
    desc = datasets._numeric_sort_order_by("year", True, "DESC")
    assert asc == "ORDER BY (metadata->>'year')::float ASC NULLS LAST"
    assert desc == "ORDER BY (metadata->>'year')::float DESC NULLS LAST"
    assert "CASE" not in asc and "CASE" not in desc  # bare cast matches the index


def test_unpromoted_numeric_sort_keeps_guarded_case():
    # No index to ride → keep the cast-safe CASE (mixed data won't 500) and NULLS LAST.
    clause = datasets._numeric_sort_order_by("some_field", False, "DESC")
    assert "CASE" in clause
    assert clause.endswith("DESC NULLS LAST")


# --- autocomplete budget + cached fallback -------------------------------


class _TimeoutThenCacheDB:
    """db.fetch raises (→ _safe_filter_query returns None, simulating a budget
    timeout on an unindexed field); db.fetchval returns the cached value preview."""

    def __init__(self, cached):
        self._cached = cached

    async def fetch(self, query, *args):
        raise asyncpg.PostgresError("simulated autocomplete timeout")

    async def fetchval(self, query, *args):
        if "is_promoted" in query:
            return False  # unpromoted: no indexed exact probe, straight to the bounded scan
        return self._cached


def test_autocomplete_falls_back_to_cached_preview_on_timeout(monkeypatch):
    async def _no_array_cols(dataset_id):
        return set()

    monkeypatch.setattr(datasets, "get_dataset_array_columns", _no_array_cols)
    monkeypatch.setattr(datasets, "db", _TimeoutThenCacheDB(["Argentina", "Australia", "Brazil"]))

    out = asyncio.run(datasets._autocomplete_impl(7, "country", "AL", 100, None))
    # From the cached preview, matched anywhere in the value and case-insensitively (it once
    # matched only the start), so Australia but not Argentina or Brazil.
    assert [o["value"] for o in out["values"]] == ["Australia"]
    # Preview rows carry no true count, and the response says the scan was NOT exhaustive.
    assert all(o["count"] is None for o in out["values"])
    assert out["exhaustive"] is False
