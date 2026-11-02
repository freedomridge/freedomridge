"""Optional CLIP compatibility helpers; importing the SDK does not load torch."""


def image_vectors(output):
    """Transformers 4 returns a tensor; 5 may return a pooled model output."""
    import torch

    if torch.is_tensor(output):
        return output
    for field in ('image_embeds', 'pooler_output'):
        vectors = getattr(output, field, None)
        if torch.is_tensor(vectors):
            return vectors
    raise TypeError('CLIP output must be a tensor or contain image_embeds/pooler_output')


def pixel_inputs(processor, images, **kwargs):
    """Run a CLIP processor on RGB images, always declaring them channels-last.

    Left to infer, transformers guesses the channel axis from the array shape, and a
    side of 1 or 3 pixels (a thin object crop, a tiny patch) makes it guess wrong and
    fail. PIL RGB images are always height x width x 3.
    """
    return processor(images=images, return_tensors='pt', input_data_format='channels_last', **kwargs)
