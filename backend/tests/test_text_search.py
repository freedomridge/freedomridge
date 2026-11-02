"""Text→embedding search guards: the prompt path must refuse what it can't answer
honestly — a field embedded by a text-less model (DINO etc.), or a text tower whose
output dimension doesn't match the field — and must plumb the encoded vector into
the exact same search the vector= path uses.

The encoder itself is exercised through stubs (transformers isn't a backend dep and
CI doesn't install it — the 501 contract covers that); resolve_checkpoint /
has_text_tower are pure and tested directly. Self-contained, matching the suite.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend import encoders
from backend.models import SearchRequest
from backend.routers import search as search_router


def test_alias_resolution_and_default():
    assert encoders.resolve_checkpoint(None) == encoders.DEFAULT_TEXT_MODEL
    assert encoders.resolve_checkpoint("clip-ViT-B-32") == "openai/clip-vit-base-patch32"
    assert encoders.resolve_checkpoint("laion/CLIP-ViT-H-14") == "laion/CLIP-ViT-H-14"


def test_text_tower_families():
    assert encoders.has_text_tower(None)  # unregistered → assume CLIP (pre-registry datasets)
    assert encoders.has_text_tower("openai/clip-vit-base-patch32")
    assert not encoders.has_text_tower("facebook/dinov2-base")
    assert not encoders.has_text_tower("dinov3")


class _DB:
    """Stubs search.db: fetchval answers the registry probe then the dim probe."""

    def __init__(self, registered=None, dim=512):
        self._answers = {"embedding_field_models": registered, "vector_dims": dim}

    async def fetchval(self, query, *args):
        for key, val in self._answers.items():
            if key in query:
                return val
        return None


def _search(req, db, monkeypatch, encoded=None):
    monkeypatch.setattr(search_router, "db", db)

    async def fake_encode(prompt, registered_model=None):
        return encoded if encoded is not None else [0.1] * 512

    monkeypatch.setattr(search_router, "encode_text", fake_encode)

    class _Req:
        headers: dict = {}
        cookies: dict = {}

    async def fake_user(request):
        return {"id": 1, "role": "admin"}

    async def fake_access(dataset_id, user):
        return None

    monkeypatch.setattr(search_router, "get_optional_user", fake_user)
    monkeypatch.setattr(search_router, "check_dataset_access", fake_access)
    return asyncio.run(search_router.vector_search(req, _Req()))  # type: ignore[arg-type]


def test_text_against_textless_field_refused(monkeypatch):
    req = SearchRequest(dataset_id=1, text="a river", field_name="dinov3")
    with pytest.raises(HTTPException) as ei:
        _search(req, _DB(registered="facebook/dinov2-base"), monkeypatch)
    assert ei.value.status_code == 400
    assert "no text tower" in ei.value.detail


def test_dim_mismatch_refused(monkeypatch):
    req = SearchRequest(dataset_id=1, text="a river", field_name="bigclip")
    with pytest.raises(HTTPException) as ei:
        _search(req, _DB(registered=None, dim=1024), monkeypatch, encoded=[0.1] * 512)
    assert ei.value.status_code == 400
    assert "1024" in ei.value.detail and "512" in ei.value.detail


def test_missing_all_inputs_names_text(monkeypatch):
    req = SearchRequest(dataset_id=1)
    with pytest.raises(HTTPException) as ei:
        _search(req, _DB(), monkeypatch)
    assert ei.value.status_code == 400
    assert "text" in ei.value.detail
