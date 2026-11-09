"""Hugging Face model downloads: only the weights a model loads."""

import os


def download_only_loaded_weights() -> None:
    """Keep transformers from downloading a second copy of a model's weights.

    When a model revision has only ``pytorch_model.bin`` (openai/clip-vit-base-patch32, the patch model, is one),
    transformers loads it, then downloads a safetensors copy from the Hub's conversion bot in a background thread,
    for next time. A pinned revision never loads that copy: it's 600 MB more in the cache for CLIP ViT-B/32.
    transformers' own switch turns it off. An explicit setting of the switch is kept.
    """
    os.environ.setdefault("DISABLE_SAFETENSORS_CONVERSION", "1")
