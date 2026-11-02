"""Guard-clause tests for the concepts router. These call the handlers
directly with a tiny stubbed `db` (FastAPI deps are bypassed — the user dict is passed in),
so they need no backend or real DB. The full happy path (create → score → exclude → delete)
is exercised live against a real backend+DB by fridge.test_sdk and the Playwright e2e.

What they lock: the soft cap, duplicate-name conflict, probe-not-yet, seed-needs-ids, and
the SQL-injection guard on the preview comparison op.
"""

import pytest
from fastapi import HTTPException

from backend.routers import concepts
from backend.routers.concepts import ConceptCreate


class _DB:
    """Returns the first configured value whose key is a substring of the query, else None."""

    def __init__(self, vals=None):
        self.vals = vals or {}

    async def fetchval(self, query, *args):
        for k, v in self.vals.items():
            if k in query:
                return v
        return None

    async def fetch(self, query, *args):
        return []

    async def fetchrow(self, query, *args):
        return None


@pytest.mark.asyncio
async def test_soft_cap_blocks_create(monkeypatch):
    monkeypatch.setattr(concepts, "db", _DB({"count(*) FROM embedding_concepts": concepts.MAX_CONCEPTS_PER_DATASET}))
    with pytest.raises(HTTPException) as e:
        await concepts.create_concept(1, ConceptCreate(name="x", seed_ids=[1]), user={"id": 1})
    assert e.value.status_code == 400 and "limit" in e.value.detail.lower()


@pytest.mark.asyncio
async def test_duplicate_name_conflicts(monkeypatch):
    monkeypatch.setattr(
        concepts,
        "db",
        _DB({"count(*) FROM embedding_concepts": 0, "SELECT 1 FROM embedding_concepts": 1}),
    )
    with pytest.raises(HTTPException) as e:
        await concepts.create_concept(1, ConceptCreate(name="dup", seed_ids=[1]), user={"id": 1})
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_probe_not_available_yet(monkeypatch):
    monkeypatch.setattr(concepts, "db", _DB({"count(*) FROM embedding_concepts": 0}))
    with pytest.raises(HTTPException) as e:
        await concepts.create_concept(1, ConceptCreate(name="p", kind="probe"), user={"id": 1})
    assert e.value.status_code == 400 and "probe" in e.value.detail.lower()


@pytest.mark.asyncio
async def test_seed_requires_ids(monkeypatch):
    monkeypatch.setattr(concepts, "db", _DB({"count(*) FROM embedding_concepts": 0}))
    with pytest.raises(HTTPException) as e:
        await concepts.create_concept(1, ConceptCreate(name="s", kind="seed"), user={"id": 1})
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_preview_rejects_injection_op(monkeypatch):
    monkeypatch.setattr(concepts, "db", _DB({}))
    with pytest.raises(HTTPException) as e:
        await concepts.preview_concept(1, 5, op="; DROP TABLE", threshold=0.5)
    assert e.value.status_code == 400
