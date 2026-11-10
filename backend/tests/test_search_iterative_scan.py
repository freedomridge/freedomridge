"""Filtered vector search must not be truncated by pgvector's post-filtering.

A plain HNSW scan yields at most hnsw.ef_search candidates and the WHERE clause is applied
afterwards, so a selective filter returned a handful of rows for LIMIT 50 with no signal
when the initial candidate set had too few matching rows. The indexed branch must enable pgvector's iterative scan
with a tuple budget; the exact branch (limit > 1000) disables the index instead and must
not touch the HNSW GUCs. python/fridge/test_sdk.py "Filtered similarity search" guards the
result count end to end; this pins the statements even when a small fixture does not
reproduce candidate exhaustion.
"""

import asyncio

import pytest

from backend.models import SearchRequest
from backend.routers import search as search_router


class _Ctx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, found=0, matching=0):
        self.executed: list[str] = []
        self.found, self.matching = found, matching
        self.fetched: list[str] = []
        self.counted = 0

    async def execute(self, query, *args):
        self.executed.append(query)

    async def fetch(self, query, *args):
        self.fetched.append(query)
        return [{'id': i, 'metadata': None, 'location': None} for i in range(self.found)]

    async def fetchval(self, query, *args):
        self.counted += 1
        return self.matching

    def transaction(self):
        return _Ctx()


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _DB:
    """Stubs search.db for the vector path: dim probe answers 8, everything else None."""

    def __init__(self, conn):
        self.pool = _Pool(conn)

    async def fetchval(self, query, *args):
        return 8 if "vector_dims" in query else None

    async def fetchrow(self, query, *args):
        return None


def _run(limit, monkeypatch, conn=None):
    conn = conn or _Conn()
    monkeypatch.setattr(search_router, "db", _DB(conn))

    class _Req:
        headers: dict = {}
        cookies: dict = {}

    async def fake_user(request):
        return {"id": 1, "role": "admin"}

    async def fake_access(dataset_id, user):
        return None

    monkeypatch.setattr(search_router, "get_optional_user", fake_user)
    monkeypatch.setattr(search_router, "check_dataset_access", fake_access)
    req = SearchRequest(dataset_id=1, vector=[0.1] * 8, field_name="clip", limit=limit)
    asyncio.run(search_router.vector_search(req, _Req()))  # type: ignore[arg-type]
    return conn.executed


@pytest.mark.parametrize("limit", [1, 50, 1000])
def test_indexed_branch_enables_the_iterative_scan_with_a_budget(limit, monkeypatch):
    executed = _run(limit, monkeypatch)
    assert any(q.startswith("SET LOCAL hnsw.ef_search = ") for q in executed), executed
    assert "SET LOCAL hnsw.iterative_scan = relaxed_order" in executed, executed
    budget = [q for q in executed if q.startswith("SET LOCAL hnsw.max_scan_tuples = ")]
    assert budget == [f"SET LOCAL hnsw.max_scan_tuples = {search_router._HNSW_MAX_SCAN_TUPLES}"], executed
    # the GUCs are only visible once the vector type is touched on the pooled connection
    assert executed.index("SELECT '[0]'::vector(1)") < executed.index("SET LOCAL hnsw.iterative_scan = relaxed_order")


def test_exact_branch_disables_the_index_and_leaves_hnsw_gucs_alone(monkeypatch):
    executed = _run(1001, monkeypatch)
    assert "SET LOCAL enable_indexscan = off" in executed, executed
    assert not any("hnsw." in q for q in executed), executed


def test_scan_budget_is_well_above_pgvectors_default():
    # 20,000 is pgvector's default and is exhausted by a 1% filter at ~200 survivors.
    assert search_router._HNSW_MAX_SCAN_TUPLES >= 100_000


def test_a_short_indexed_answer_is_ranked_again_only_when_matches_were_left_behind(monkeypatch):
    """Past its tuple budget the index scan stops with matches left, and the short answer looked like "no more
    match". A short answer is checked with a count that stops at the limit, and ranked again off the vector index
    only when more match than came back."""
    left_behind = _Conn(found=49, matching=50)
    _run(50, monkeypatch, left_behind)
    assert left_behind.counted == 1 and len(left_behind.fetched) == 2
    assert "+ 0 ASC" in left_behind.fetched[1] and "+ 0 ASC" not in left_behind.fetched[0]

    all_there = _Conn(found=49, matching=49)
    _run(50, monkeypatch, all_there)
    assert all_there.counted == 1 and len(all_there.fetched) == 1

    full = _Conn(found=50)
    _run(50, monkeypatch, full)
    assert full.counted == 0 and len(full.fetched) == 1

    exact = _Conn(found=10)  # limit > 1000 is exact already
    _run(1001, monkeypatch, exact)
    assert exact.counted == 0 and len(exact.fetched) == 1
