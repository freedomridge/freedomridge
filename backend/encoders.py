"""Optional in-process CLIP encoder — powers text→embedding search, text concepts,
and reverse-IMAGE search (upload/URL → similar) from the UI/API.

Deliberately optional: torch/transformers are NOT in the server's base dependencies;
they come with pyproject.toml's cpu/cuda extras (the uploader-owns-derived-fields policy
keeps ML out of the core server). When they're
importable the encoder lazy-loads the needed tower — the TEXT tower
(CLIPTextModelWithProjection, ~250MB) for text search, the VISION tower
(CLIPVisionModelWithProjection, ~350MB) for image search — on first use and caches
it per checkpoint; when they aren't, callers get a 501 naming the exact pip install.
Encoding runs in a worker thread so the event loop never blocks (first call pays the
model load).

A query embedding is only comparable to stored embeddings from the SAME CLIP
checkpoint — resolve the field's checkpoint via the embedding_field_models registry
before calling; fields embedded by text-less/CLIP-less models (DINO
etc.) must be refused by the caller, not encoded anyway.
"""

import asyncio
import io
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from fastapi import HTTPException
from PIL import Image

# JPEG draft decoding reduces pixels before allocation. Other formats must decode
# the full frame before thumbnailing, so their header limit is deliberately lower.
_MAX_QUERY_JPEG_PIXELS = 120_000_000
_MAX_QUERY_IMAGE_PIXELS = 40_000_000
_QUERY_IMAGE_SIZE = (1024, 1024)


def _decode_query_image(image_bytes: bytes) -> Image.Image:
    too_large = (
        "Query image is too large; upload a smaller image (JPEG: 120 million pixels; other formats: 40 million)."
    )
    try:
        with warnings.catch_warnings():
            # Allow large JPEG headers without changing Pillow's global media guard.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as image:
                is_jpeg = image.format == 'JPEG'
                limit = _MAX_QUERY_JPEG_PIXELS if is_jpeg else _MAX_QUERY_IMAGE_PIXELS
                if image.width * image.height > limit:
                    raise HTTPException(
                        400,
                        f"Query image exceeds {limit // 1_000_000} million pixels; upload a smaller image.",
                    )
                if is_jpeg:
                    image.draft("RGB", _QUERY_IMAGE_SIZE)
                image.thumbnail(_QUERY_IMAGE_SIZE, Image.Resampling.LANCZOS)
                return image.convert("RGB")
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(400, too_large)
    except Exception:
        raise HTTPException(400, "Could not decode the provided image.")


# The server default matches the SDK's compute_embeddings default checkpoint
# (sentence-transformers "clip-ViT-B-32" wraps the same OpenAI weights).
DEFAULT_TEXT_MODEL = "openai/clip-vit-base-patch32"

# Common aliases seen in the registry → HF checkpoint ids.
_MODEL_ALIASES = {
    "clip-ViT-B-32": "openai/clip-vit-base-patch32",
    "clip-ViT-B-16": "openai/clip-vit-base-patch16",
    "clip-ViT-L-14": "openai/clip-vit-large-patch14",
}

_cache: Dict[str, tuple] = {}  # hf checkpoint -> (tokenizer, text_model)
_image_cache: Dict[str, tuple] = {}  # hf checkpoint -> (image_processor, vision_model)
_lock = threading.Lock()
# Dedicated single worker: encodes never compete for (or starve) the shared
# asyncio.to_thread pool that serves media/imaging work, and torch inference on one
# module stays serialized without holding a lock across the forward pass.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clip-text")


def resolve_checkpoint(registered_model: Optional[str]) -> str:
    """Registry value → HF checkpoint for the text tower. None (unregistered field)
    falls back to the default CLIP — the pragmatic choice for pre-registry datasets;
    callers should refuse KNOWN non-CLIP models before getting here."""
    if not registered_model:
        return DEFAULT_TEXT_MODEL
    return _MODEL_ALIASES.get(registered_model, registered_model)


def has_text_tower(registered_model: Optional[str]) -> bool:
    """False for models we KNOW have no text tower (image-only SSL families)."""
    name = (registered_model or "").lower()
    return not any(fam in name for fam in ("dino", "mae", "sam", "ijepa", "resnet", "efficientnet"))


def has_image_tower(registered_model: Optional[str]) -> bool:
    """Whether a query IMAGE can be encoded into this field's space server-side.

    CLIP-family only for now: we load the CLIP *vision* tower, which lands in the same
    space as the stored CLIP image embeddings. Image-only SSL families (DINOv3 etc.)
    DO have image towers, but their weights aren't reachable in the server, so reverse
    image search against those fields is refused until that model is added — the same
    set has_text_tower already excludes.
    """
    return has_text_tower(registered_model)


def _encode_sync(prompt: str, checkpoint: str) -> List[float]:
    try:
        import torch
        from transformers import CLIPTextConfig, CLIPTextModelWithProjection, CLIPTokenizerFast
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail=(
                "Text encoding needs torch + transformers on the server "
                "(pip install torch transformers). Alternatively encode client-side: "
                "fridge's sort_by_similarity(text=...) / create_concept(prompt=...)."
            ),
        )
    # Lock only guards the cache load — never the forward pass (a cold multi-second
    # model download inside the lock would serialize *and* block unrelated encodes).
    with _lock:
        cached = _cache.get(checkpoint)
    if cached is None:
        tokenizer = CLIPTokenizerFast.from_pretrained(checkpoint)
        # Pass the tower's own config. transformers 4.57 takes each class's config_class from
        # its `config:` annotation in cls.__dict__, which Python 3.14's lazy annotations leave
        # empty, so the towers inherit the full CLIPConfig and loading fails (no hidden_size).
        config = CLIPTextConfig.from_pretrained(checkpoint)
        model = CLIPTextModelWithProjection.from_pretrained(checkpoint, config=config).eval()
        with _lock:
            cached = _cache.setdefault(checkpoint, (tokenizer, model))
    tokenizer, model = cached
    inputs = tokenizer([prompt], padding=True, truncation=True, return_tensors="pt")
    with torch.no_grad():
        out = model(**inputs)
    return out.text_embeds[0].tolist()


async def encode_text(prompt: str, registered_model: Optional[str] = None) -> List[float]:
    """Encode a prompt with the field's CLIP text tower (dedicated worker thread)."""
    checkpoint = resolve_checkpoint(registered_model)
    return await asyncio.get_running_loop().run_in_executor(_executor, _encode_sync, prompt, checkpoint)


def _encode_image_sync(image_bytes: bytes, checkpoint: str) -> List[float]:
    try:
        import torch
        from transformers import CLIPImageProcessor, CLIPVisionConfig, CLIPVisionModelWithProjection
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail=(
                "Image encoding needs torch + transformers + pillow on the server "
                "(pip install torch transformers pillow). Alternatively encode client-side "
                "and pass the vector to /search."
            ),
        )
    image = _decode_query_image(image_bytes)
    # Lock only guards the cache load — never the forward pass (see _encode_sync).
    with _lock:
        cached = _image_cache.get(checkpoint)
    if cached is None:
        processor = CLIPImageProcessor.from_pretrained(checkpoint)
        # As with text, do not let from_pretrained infer the full CLIPConfig.
        config = CLIPVisionConfig.from_pretrained(checkpoint)
        model = CLIPVisionModelWithProjection.from_pretrained(checkpoint, config=config).eval()
        with _lock:
            cached = _image_cache.setdefault(checkpoint, (processor, model))
    processor, model = cached
    # channels-last: left to infer, a 1- or 3-pixel side makes transformers misread the axes.
    inputs = processor(images=image, return_tensors="pt", input_data_format="channels_last")
    with torch.no_grad():
        out = model(**inputs)
    # .image_embeds is the projected embedding — same space as the stored CLIP image
    # vectors and as text_embeds, so cosine KNN against the field is meaningful.
    return out.image_embeds[0].tolist()


async def encode_image(image_bytes: bytes, registered_model: Optional[str] = None) -> List[float]:
    """Encode an image with the field's CLIP vision tower (dedicated worker thread)."""
    checkpoint = resolve_checkpoint(registered_model)
    return await asyncio.get_running_loop().run_in_executor(_executor, _encode_image_sync, image_bytes, checkpoint)
