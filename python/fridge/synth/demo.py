"""Build a full-featured synthetic DEMO dataset for exploring the whole app with fake data.

On top of the images + every annotation type + fake lat/long from ``generate``, this
adds: image hashes (duplicates panel), a whole-image CLIP similarity index (embeddings
panel + find-similar), grid patch embeddings (2x2/4x4), and a bounding-box patch dataset
(crops), optionally with its own similarity index.

    from fridge import connect
    from fridge.core.client import get_client
    from fridge.synth import build_demo

    connect("http://localhost:8000/api"); get_client().login("admin", "admin123")
    build_demo("synth-demo", num_images=48)

Whole-image embeddings reuse the openai/clip-vit-base-patch32 model via transformers
(the same one compute_patch_embeddings uses) — no sentence-transformers required. Each
optional step is skipped with a message if its dependency (torch/transformers, imagehash,
umap) is missing, so the base dataset always builds.
"""

from __future__ import annotations

import os
from typing import List

from PIL import Image

from ..core.clip import image_vectors
from ..core.dataset import Dataset
from .generator import SyntheticSpec, generate


def _local_loader(fp: str) -> Image.Image:
    return Image.open(fp).convert("RGB")


def _clip_image_embeddings(filepaths: List[str], batch_size: int = 32) -> List[list]:
    """Whole-image CLIP embeddings via transformers (reuses the cached patch model)."""
    import torch
    from transformers import CLIPModel, CLIPProcessor

    model_id = "openai/clip-vit-base-patch32"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = CLIPModel.from_pretrained(model_id).to(device).eval()
    proc = CLIPProcessor.from_pretrained(model_id)
    vecs: List[list] = []
    for i in range(0, len(filepaths), batch_size):
        imgs = [_local_loader(p) for p in filepaths[i : i + batch_size]]
        inputs = proc(images=imgs, return_tensors="pt").to(device)
        with torch.no_grad():
            feats = image_vectors(model.get_image_features(**inputs))
        feats = torch.nn.functional.normalize(feats, dim=-1)
        vecs.extend(feats.cpu().tolist())
    return vecs


def _upload_image_embeddings(ds: Dataset) -> int:
    samples = ds.all().to_list()
    ids = [s.id for s in samples]
    fps = [s.filepath for s in samples]
    if not ids:
        return 0
    vecs = _clip_image_embeddings(fps)
    ds.client.upload_embeddings(
        ds._id,
        [{"sample_id": sid, "vector": v, "field_name": "clip"} for sid, v in zip(ids, vecs)],
    )
    ds.compute_visualization("clip")  # UMAP reduce so it shows in the embeddings panel
    return len(ids)


def build_demo(
    name: str = "synth-demo",
    num_images: int = 48,
    seed: int = 0,
    with_hashes: bool = True,
    with_embeddings: bool = True,
    with_patches: bool = True,
    with_bbox_patches: bool = True,
) -> Dataset:
    """Create a fully-featured synthetic demo dataset (idempotent on re-run)."""
    gen = generate(SyntheticSpec(num_images=num_images, seed=seed))
    ds = Dataset(name)
    ds.add_samples(gen.fridge_samples(), on_duplicate="skip")
    print(f"[demo] '{name}': {len(gen.samples)} samples (images + all annotation types + lat/long)")

    if with_hashes:
        try:
            ds.compute_hashes(image_loader=_local_loader)
            print("[demo] image hashes computed (dhash/phash) → duplicates panel")
        except Exception as e:
            print(f"[demo] hashes SKIPPED: {e}")

    if with_embeddings:
        try:
            n = _upload_image_embeddings(ds)
            print(f"[demo] whole-image CLIP similarity index ('clip') + UMAP on {n} samples")
        except Exception as e:
            print(f"[demo] whole-image embeddings SKIPPED: {e}")

    if with_patches:
        try:
            ds.compute_patch_embeddings(scales=["2x2", "4x4"], image_loader=_local_loader, reduce=True)
            print("[demo] grid patch embeddings (2x2, 4x4) → patch mode + patch search")
        except Exception as e:
            print(f"[demo] grid patch embeddings SKIPPED: {e}")

    if with_bbox_patches:
        try:
            patches_dir = os.path.join(gen.data_root, "synth", f"{name}_bbox_patches")
            crops = ds.to_patches("ground_truth", f"{name}-patches", patches_dir, image_loader=_local_loader)
            total = crops.client.query_samples(crops._id, limit=1)["total"]
            print(f"[demo] bounding-box patch dataset '{name}-patches': {total} crops")
            if with_embeddings and total:
                n = _upload_image_embeddings(crops)
                print(f"[demo] CLIP similarity index on {n} bbox patches")
        except Exception as e:
            print(f"[demo] bbox patches SKIPPED: {e}")

    print(f"[demo] done — explore '{name}' (map/grid/embeddings/duplicates) and '{name}-patches'.")
    return ds
