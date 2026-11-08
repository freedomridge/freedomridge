"""Dataset.compute_embeddings(model_source="huggingface") with no server, database or ML packages.

torch, torchvision and transformers are replaced by fakes that record what the SDK asks of them, so the
tests show which output becomes the vector and that it is uploaded as the model produced it.
"""

import contextlib
import sys
import types
from typing import Any

import numpy as np
import pytest

from backend.tests.test_compute_embeddings_skip_existing import FakeClient, FakeImage, make_dataset

MODEL = "facebook/dinov3-vitl16-pretrain-sat493m"


class FakeTensor:
    def __init__(self, rows):
        self.rows = np.asarray(rows, dtype=float)

    def float(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.rows


class FakeInputs(dict):
    def to(self, device):
        return self


@pytest.fixture
def transformers_calls(monkeypatch):
    """Install fake torch/torchvision/transformers; returns the calls they saw. Set calls["output"]."""
    calls: dict[str, Any] = {"processor": [], "model_inputs": [], "output": "pooled"}

    class FakeProcessor:
        def __call__(self, images, **kwargs):
            calls["processor"].append({"n": len(images), **kwargs})
            return FakeInputs(pixel_values=f"pixels of {len(images)}")

    class FakeModel:
        def to(self, device):
            return self

        def eval(self):
            return self

        def __call__(self, **inputs):
            calls["model_inputs"].append(inputs)
            n = int(inputs["pixel_values"].split()[-1])
            pooled = FakeTensor([[3.0, 4.0]] * n) if calls["output"] == "pooled" else None
            return types.SimpleNamespace(pooler_output=pooled, last_hidden_state="tokens")

    class FakeClipModel(FakeModel):
        def get_image_features(self, pixel_values):
            n = int(pixel_values.split()[-1])
            return FakeTensor([[6.0, 8.0]] * n)

    def from_pretrained_model(name):
        calls["model"] = name
        return FakeClipModel() if calls["output"] == "clip" else FakeModel()

    def from_pretrained_processor(name, **kwargs):
        calls["processor_kwargs"] = {"name": name, **kwargs}
        return FakeProcessor()

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    torch.no_grad = contextlib.nullcontext  # type: ignore[attr-defined]
    torch.is_tensor = lambda value: isinstance(value, FakeTensor)  # type: ignore[attr-defined]
    transformers = types.ModuleType("transformers")
    transformers.AutoModel = types.SimpleNamespace(from_pretrained=from_pretrained_model)  # type: ignore[attr-defined]
    transformers.AutoProcessor = types.SimpleNamespace(  # type: ignore[attr-defined]
        from_pretrained=from_pretrained_processor,
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torchvision", types.ModuleType("torchvision"))
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    return calls


def run(client, **kwargs):
    loaded: list[str] = []

    def loader(path):
        loaded.append(path)
        return FakeImage()

    result = make_dataset(client).compute_embeddings(
        model_name=MODEL,
        field_name="dinov3",
        image_loader=loader,
        model_source="huggingface",
        **kwargs,
    )
    return result, loaded


def test_huggingface_model_uploads_its_pooled_output_as_is(transformers_calls):
    client = FakeClient(range(1, 6))
    result, _ = run(client, batch_size=2)
    assert transformers_calls["model"] == MODEL
    assert transformers_calls["processor_kwargs"] == {"name": MODEL, "backend": "torchvision"}
    # Batched through the checkpoint's processor, with no channel-format hint (wrong for fast processors).
    assert transformers_calls["processor"] == [{"n": n, "return_tensors": "pt"} for n in (2, 2, 1)]
    rows = [row for batch in client.uploads for row in batch]
    assert [row["sample_id"] for row in rows] == [1, 2, 3, 4, 5]
    assert all(row["vector"] == [3.0, 4.0] and row["field_name"] == "dinov3" for row in rows)  # not normalized
    assert result["written"] == 5
    assert client.registered == [("dinov3", MODEL)]


def test_clip_style_model_uses_its_image_features(transformers_calls):
    transformers_calls["output"] = "clip"
    client = FakeClient([1])
    run(client)
    assert client.uploads[0][0]["vector"] == [6.0, 8.0]
    assert transformers_calls["model_inputs"] == []  # get_image_features, not the full forward pass


def test_model_without_a_pooled_output_is_an_error(transformers_calls):
    transformers_calls["output"] = None
    client = FakeClient([1])
    with pytest.raises(ValueError, match="no pooled output"):
        run(client)
    assert client.uploads == []


def test_unknown_model_source_is_refused_before_loading_any_image():
    client = FakeClient([1])
    loaded: list[str] = []
    with pytest.raises(ValueError, match="model_source"):
        make_dataset(client).compute_embeddings(image_loader=loaded.append, model_source="timm")
    assert loaded == []
