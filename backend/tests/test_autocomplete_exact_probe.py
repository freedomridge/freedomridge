"""Autocomplete on a high-cardinality promoted field: a value that EXISTS must never be
reported as "no match".

The bug: the typeahead's prefix GROUP BY cannot use the field's btree (non-C collation), so on
a field where every value is unique it blows the per-keystroke budget, falls back to the cached
top-N preview (which cannot contain the value) and returned ``[]`` — indistinguishable from
"no such value" — while pressing Enter (an indexed ``=`` filter) found the sample instantly.

Fix: (1) for promoted scalar fields an indexed exact-match probe runs first, so a pasted full
identifier is returned even when the scan times out; (2) the response says whether the scan was
exhaustive, so the UI can say "too many values to search" instead of "no matches".
"""

import asyncio

import asyncpg

from backend.routers import datasets

FIELD = "source_id"
VALUE = "8242eacf-33ef-4d0e-9ea6-5496d39cc792-rgb.png"


class FakeDB:
    """Dispatches on the SQL text: is_promoted lookup, the COUNT(*) exact probe, the cached
    preview, and the bounded scan (which can simulate a budget timeout)."""

    def __init__(self, *, promoted, exact_count=0, cached=(), scan_rows=None, scan_times_out=False):
        self.promoted = promoted
        self.exact_count = exact_count
        self.cached = list(cached)
        self.scan_rows = scan_rows or []
        self.scan_times_out = scan_times_out
        self.probes: list = []  # (query, args) of every exact probe issued

    async def fetchval(self, query, *args):
        if "is_promoted" in query:
            return self.promoted
        if "COUNT(*)" in query:
            self.probes.append((query, args))
            return self.exact_count
        if "unique_values" in query:
            return self.cached
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetch(self, query, *args):
        if self.scan_times_out:
            raise asyncpg.PostgresError("simulated autocomplete budget timeout")
        return self.scan_rows


def _run(db, q=VALUE, field=FIELD, monkeypatch=None):
    async def _no_array_cols(dataset_id):
        return set()

    monkeypatch.setattr(datasets, "get_dataset_array_columns", _no_array_cols)
    monkeypatch.setattr(datasets, "db", db)
    return asyncio.run(datasets._autocomplete_impl(7, field, q, 100, None))


def test_existing_value_is_returned_when_the_scan_times_out(monkeypatch):
    # THE bug: promoted, every value unique → the preview can't hold it, the scan times out.
    db = FakeDB(promoted=True, exact_count=1, cached=["aaa", "bbb"], scan_times_out=True)
    out = _run(db, monkeypatch=monkeypatch)
    assert out["values"] == [{"value": VALUE, "count": 1}]
    assert out["exhaustive"] is False
    # The probe binds the typed value verbatim as the last argument (an indexed `=`).
    assert len(db.probes) == 1
    assert db.probes[0][1][-1] == VALUE
    assert "= $" in db.probes[0][0]


def test_timeout_without_an_exact_hit_is_reported_as_not_exhaustive(monkeypatch):
    # No hit and no preview match: the list is empty but the response must NOT claim "no match".
    db = FakeDB(promoted=True, exact_count=0, cached=["aaa"], scan_times_out=True)
    out = _run(db, monkeypatch=monkeypatch)
    assert out == {"values": [], "exhaustive": False}


def test_unpromoted_field_issues_no_probe(monkeypatch):
    # No index → an exact probe would be a second full scan per keystroke. Keep the old path.
    db = FakeDB(promoted=False, exact_count=1, cached=["Argentina", "Australia"], scan_times_out=True)
    out = _run(db, q="a", field="country", monkeypatch=monkeypatch)
    assert db.probes == []
    assert [v["value"] for v in out["values"]] == ["Argentina", "Australia"]
    assert out["exhaustive"] is False


def test_completed_scan_is_exhaustive_and_does_not_duplicate_the_hit(monkeypatch):
    db = FakeDB(
        promoted=True,
        exact_count=1,
        scan_rows=[{"val": VALUE, "cnt": 1}, {"val": VALUE + ".bak", "cnt": 1}],
    )
    out = _run(db, monkeypatch=monkeypatch)
    assert out["exhaustive"] is True
    assert [v["value"] for v in out["values"]] == [VALUE, VALUE + ".bak"]
    assert out["values"][0]["count"] == 1


def test_empty_query_issues_no_probe(monkeypatch):
    db = FakeDB(promoted=True, exact_count=5, scan_rows=[{"val": "x", "cnt": 2}])
    out = _run(db, q="", monkeypatch=monkeypatch)
    assert db.probes == []
    assert out == {"values": [{"value": "x", "count": 2}], "exhaustive": True}
