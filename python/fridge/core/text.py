"""Client-side CLIP text encoding — the fallback when the server can't encode.

Text search prefers the SERVER encoder (POST /search with text=..., which also knows
the field's registered checkpoint); when the server answers 501 (transformers not
installed there), the SDK encodes locally with sentence-transformers — the same
package and default checkpoint compute_embeddings uses, so prompts land in the same
space as locally-computed image vectors.
"""

from typing import Any, Dict

_cache: Dict[str, Any] = {}

DEFAULT_TEXT_MODEL = "clip-ViT-B-32"  # sentence-transformers name; == openai/clip-vit-base-patch32


def encode_text(prompt: str, model_name: str = DEFAULT_TEXT_MODEL):
    """Encode a prompt into the CLIP joint space. Returns a list[float]."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise ImportError(
            "Client-side text encoding needs sentence-transformers: "
            'pip install sentence-transformers (or: pip install "freedomridge[embeddings]")',
        )
    if model_name not in _cache:
        _cache[model_name] = SentenceTransformer(model_name)
    return _cache[model_name].encode([prompt])[0].tolist()
