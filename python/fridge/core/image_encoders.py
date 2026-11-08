"""Whole-image encoders for Dataset.compute_embeddings; importing the SDK does not load torch."""

MODEL_SOURCES = ('sentence-transformers', 'huggingface')


def load_image_encoder(model_name, model_source='sentence-transformers'):
    """Load a model; returns encode(list of RGB PIL images) -> array of shape (len(images), dim).

    sentence-transformers: vectors are L2-normalized. huggingface: any Transformers image model
    with a pooled output (DINOv2, DINOv3, ViT; DINOv3's is its CLS token) or with
    get_image_features (CLIP, SigLIP), run through the checkpoint's own processor. Its vectors are
    the model's output as is: Freedom Ridge compares embeddings by cosine, and other tools that run
    the same checkpoint (FiftyOne, for one) store them unscaled, so vectors stay identical whichever
    tool computed them.
    """
    if model_source == 'sentence-transformers':
        return _sentence_transformers_encoder(model_name)
    if model_source == 'huggingface':
        return _huggingface_encoder(model_name)
    raise ValueError(f'model_source must be one of {MODEL_SOURCES}, got {model_source!r}')


def _sentence_transformers_encoder(model_name):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise ImportError(
            "Please install sentence-transformers: pip install sentence-transformers Pillow "
            "(or: pip install \"freedomridge[all]\")",
        )
    model = SentenceTransformer(model_name)
    return lambda images: model.encode(images, normalize_embeddings=True)


def _huggingface_encoder(model_name):
    try:
        import torch
        import torchvision  # noqa: F401  (fast image processors, the only kind DINOv3 has, need it)
        from transformers import AutoModel, AutoProcessor
    except ImportError:
        raise ImportError(
            'Hugging Face image models need torch, torchvision and transformers: '
            'pip install "freedomridge[huggingface-embeddings]"',
        )
    from .clip import image_vectors

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    processor = AutoProcessor.from_pretrained(model_name, backend='torchvision')  # the fast processor
    model = AutoModel.from_pretrained(model_name).to(device).eval()

    def encode(images):
        # Not clip.pixel_inputs: its channels-last hint is for slow processors, which take PIL images as
        # height x width x 3 arrays. Fast ones convert them to channels-first tensors, so the hint is wrong.
        inputs = processor(images=images, return_tensors='pt').to(device)
        with torch.no_grad():
            if hasattr(model, 'get_image_features'):  # CLIP-style: the projected image embedding
                vectors = image_vectors(model.get_image_features(pixel_values=inputs['pixel_values']))
            else:
                vectors = getattr(model(**inputs), 'pooler_output', None)
                if vectors is None:
                    raise ValueError(f'{model_name} has no pooled output to use as an image embedding')
        return vectors.float().cpu().numpy()

    return encode
