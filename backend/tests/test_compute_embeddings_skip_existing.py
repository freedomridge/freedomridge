"""Dataset.compute_embeddings(skip_existing=...) and the /missing id cursor, with no server or database.

The client is a stub and sentence-transformers is replaced by a fake module, so the tests show which
samples are loaded, encoded and uploaded.
"""

import sys
import types

import numpy as np
import pytest
from fridge.core.dataset import Dataset

FIELD = "clip"


class FakeClient:
    """Holds `have` (ids that already carry the embedding) and serves /missing like the backend."""

    def __init__(self, ids, have=()):
        self.ids = list(ids)
        self.have = set(have)
        self.uploads = []
        self.missing_calls = []
        self.registered = []

    def get_missing(self, dataset_id, kind, field=None, limit=1000, offset=0, after_id=None):
        self.missing_calls.append({"kind": kind, "field": field, "limit": limit, "after_id": after_id})
        missing = [i for i in sorted(self.ids) if i not in self.have]
        page = [i for i in missing if after_id is None or i > after_id][:limit]
        return {"items": [{"id": i, "filepath": f"img{i}.jpg"} for i in page], "total": len(missing)}

    def query_samples(self, dataset_id, limit=1000, offset=0, **kwargs):
        raise AssertionError("skip_existing must not fetch samples through query_samples")

    def upload_embeddings(self, dataset_id, payload):
        self.uploads.append(payload)
        return {"written": len(payload), "dropped": 0, "dropped_sample_ids": []}

    def register_field_model(self, dataset_id, field_name, model_name, force=False):
        self.registered.append((field_name, model_name))


class AllSamplesDataset(Dataset):
    """iter_samples yields every sample, as the real one does (it is what skip_existing=False uses)."""

    def iter_samples(self, selected_fields=None, progress=False, batch_size=1000, filters=None):
        for i in self.client.ids:
            yield types.SimpleNamespace(id=i, filepath=f"img{i}.jpg")


class FakeImage:
    mode = "RGB"


@pytest.fixture
def encoded(monkeypatch):
    """Replace sentence_transformers with a model that records the images it is asked to encode."""
    seen = []

    class FakeModel:
        def __init__(self, name):
            self.name = name

        def encode(self, imgs, normalize_embeddings=False):
            assert normalize_embeddings
            seen.extend(imgs)
            return np.ones((len(imgs), 4))

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return seen


def make_dataset(client):
    ds = AllSamplesDataset.__new__(AllSamplesDataset)
    ds.client = client
    ds._id = 1
    return ds


def run(client, loaded, **kwargs):
    def loader(path):
        loaded.append(path)
        return FakeImage()

    return make_dataset(client).compute_embeddings(field_name=FIELD, image_loader=loader, **kwargs)


def uploaded_ids(client):
    return [row["sample_id"] for batch in client.uploads for row in batch]


def test_skip_existing_embeds_only_missing_samples(encoded):
    client = FakeClient(range(1, 8), have={1, 2, 3, 5})
    loaded: list[str] = []
    result = run(client, loaded, skip_existing=True, batch_size=2)
    assert loaded == ["img4.jpg", "img6.jpg", "img7.jpg"]
    assert len(encoded) == 3
    assert uploaded_ids(client) == [4, 6, 7]
    assert [len(b) for b in client.uploads] == [2, 1]
    assert result["total"] == 3 and result["written"] == 3
    assert client.registered == [(FIELD, "clip-ViT-B-32")]


def test_default_embeds_every_sample(encoded):
    client = FakeClient(range(1, 8), have={1, 2, 3, 5})
    loaded: list[str] = []
    result = run(client, loaded)
    assert len(loaded) == 7 and len(encoded) == 7
    assert uploaded_ids(client) == list(range(1, 8))
    assert result["total"] == 7
    assert client.missing_calls == []


def test_nothing_missing_loads_encodes_and_uploads_nothing(encoded):
    client = FakeClient(range(1, 4), have={1, 2, 3})
    loaded: list[str] = []
    result = run(client, loaded, skip_existing=True)
    assert loaded == [] and encoded == [] and client.uploads == []
    assert result == {"total": 0, "written": 0, "dropped": 0, "dropped_sample_ids": []}
    assert client.registered == []  # nothing was written, so no model registration


def test_unreadable_image_is_not_listed_again(encoded):
    # An image that fails to load stays missing on the server; the id cursor walks past it.
    client = FakeClient(range(1, 2502))

    def loader(path):
        if path == "img2.jpg":
            return None
        return FakeImage()

    result = make_dataset(client).compute_embeddings(field_name=FIELD, image_loader=loader, skip_existing=True)
    assert result["written"] == 2500
    assert 2 not in uploaded_ids(client)
    cursors = [c["after_id"] for c in client.missing_calls]
    assert cursors == [None, 1000, 2000, 2501]
    assert all(c["kind"] == "embedding" and c["field"] == FIELD for c in client.missing_calls)
