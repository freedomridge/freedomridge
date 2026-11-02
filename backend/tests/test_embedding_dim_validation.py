"""Embedding uploads must reject a wrong-dimension vector at ingest — a wrong-dim vector
silently poisons the field (search/concepts/quality cast to vector(D) probed from an existing
row). A brand-new field is accepted and establishes the dimension.

Self-contained: db.fetchval is stubbed with the field's established dimension.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend.routers import embeddings as emb


class _DimDB:
    """Stubs db.fetchval to return the established dim for a field (None = new field)."""

    def __init__(self, established):
        self._established = established

    async def fetchval(self, query, *args):
        return self._established


def _run(field_dims, established, monkeypatch):
    monkeypatch.setattr(emb, "db", _DimDB(established))
    return asyncio.run(emb.validate_embedding_dims(field_dims))


def test_matching_dimension_accepted(monkeypatch):
    _run({"clip": {512}}, established=512, monkeypatch=monkeypatch)  # must not raise


def test_new_field_establishes_dimension(monkeypatch):
    _run({"new_model": {768}}, established=None, monkeypatch=monkeypatch)  # no established dim -> accept


def test_dimension_mismatch_rejected(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _run({"clip": {1024}}, established=512, monkeypatch=monkeypatch)
    assert ei.value.status_code == 422
    assert "512" in ei.value.detail and "1024" in ei.value.detail


def test_inconsistent_within_request_rejected(monkeypatch):
    # Two different dims for one field in a single request — rejected before any DB probe.
    with pytest.raises(HTTPException) as ei:
        _run({"clip": {512, 1024}}, established=None, monkeypatch=monkeypatch)
    assert ei.value.status_code == 422
    assert "Inconsistent" in ei.value.detail
