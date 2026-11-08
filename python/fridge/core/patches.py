"""Patch vectors: CLIP vectors of an image's grid tiles, as ``Dataset.compute_patch_embeddings`` stores them.

``compute_image_patch_vectors`` computes one image's, with no client and no network beyond loading the
model, so it can run next to the images (an AWS job, say) and write vectors a server imports later. The
model loads offline from a Hugging Face cache warmed beforehand (``HF_HOME``, then ``HF_HUB_OFFLINE=1``).

Vectors are interchangeable when ``PATCH_MODEL_ID``, ``PATCH_MODEL_REVISION`` and ``PATCH_CROP_VERSION``
match. ``PATCH_CROP_VERSION`` changes only when the tiles or their preprocessing do.
"""

import re
import threading
from typing import Any, List, Optional, Tuple

import numpy as np

PATCH_MODEL_ID = "openai/clip-vit-base-patch32"
# The Hugging Face commit of PATCH_MODEL_ID that patch vectors are computed with (unchanged since 2024-02).
PATCH_MODEL_REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
PATCH_CROP_VERSION = 1


def parse_scale(scale: str) -> Tuple[int, int]:
    """``"4x4"`` → ``(4, 4)``: rows, then columns."""
    match = re.fullmatch(r"(\d+)x(\d+)", scale)
    if not match:
        raise ValueError(f"Invalid scale format '{scale}', expected NxN (e.g. '2x2', '4x4')")
    return int(match.group(1)), int(match.group(2))


def patch_boxes(width: int, height: int, rows: int, cols: int) -> List[Tuple[int, int, int, int]]:
    """Crop boxes ``(x0, y0, x1, y1)`` of the grid's tiles in row order: r0c0, r0c1, ..., r(rows-1)c(cols-1).

    Each tile is ``width // cols`` by ``height // rows`` pixels; the last column and row reach the image's
    right and bottom edges, so every pixel is in exactly one tile, whatever the size.
    """
    tile_width, tile_height = width // cols, height // rows
    boxes = []
    for row in range(rows):
        for col in range(cols):
            x0, y0 = col * tile_width, row * tile_height
            x1 = width if col == cols - 1 else x0 + tile_width
            y1 = height if row == rows - 1 else y0 + tile_height
            boxes.append((x0, y0, x1, y1))
    return boxes


def load_patch_model(model_id: str = PATCH_MODEL_ID, revision: Optional[str] = PATCH_MODEL_REVISION, device=None):
    """The CLIP model and processor patch vectors use, on CUDA when there is one: ``(model, processor, device)``.

    A Dockerfile can call it once to warm the Hugging Face cache with exactly these weights.
    """
    import torch
    from transformers import CLIPModel, CLIPProcessor

    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    # transformers 5's @wraps(Module.to) hides the bound self from mypy.
    # "main" is what Hugging Face loads without a revision.
    model = CLIPModel.from_pretrained(model_id, revision=revision or "main").to(device)  # type: ignore[arg-type]
    processor = CLIPProcessor.from_pretrained(model_id, revision=revision or "main")
    return model, processor, device


def encode_patches(model, processor, device, crops) -> np.ndarray:
    """L2-normalised CLIP image vectors of RGB crops, one float32 row per crop, in one forward pass."""
    import torch

    from .clip import image_vectors, pixel_inputs

    inputs = pixel_inputs(processor, crops, padding=True).to(device)
    with torch.no_grad():
        features = image_vectors(model.get_image_features(**inputs))
        vectors = (features / features.norm(dim=-1, keepdim=True)).cpu().numpy()
    return np.asarray(vectors, dtype=np.float32)


_default_model: Any = None
_default_model_lock = threading.Lock()


def compute_image_patch_vectors(image, scale: str = "4x4", *, model=None) -> np.ndarray:
    """One image's patch vectors: an array of ``rows × cols`` L2-normalised float32 rows in tile order
    (``patch_boxes``), the same vectors ``Dataset.compute_patch_embeddings`` stores as
    ``<field>_patch_<scale>`` tiles.

    Args:
        image: A PIL RGB image of any size.
        scale: The grid, ``"ROWSxCOLS"`` (``"4x4"`` gives 16 rows of 512 values).
        model: ``(model, processor, device)`` from ``load_patch_model``. By default the pinned model is loaded
            once and reused.
    """
    if image.mode != "RGB":
        raise ValueError(f"compute_image_patch_vectors takes an RGB image, not {image.mode}: use image.convert('RGB')")
    rows, cols = parse_scale(scale)
    if model is None:
        global _default_model
        with _default_model_lock:
            if _default_model is None:
                _default_model = load_patch_model()
            model = _default_model
    clip_model, processor, device = model
    width, height = image.size
    crops = [image.crop(box) for box in patch_boxes(width, height, rows, cols)]
    return encode_patches(clip_model, processor, device, crops)
