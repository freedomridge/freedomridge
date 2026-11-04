"""CLIP tower loading regressions (issue #5); no model downloads required."""

import io
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from backend import encoders


@pytest.fixture(autouse=True)
def empty_caches(monkeypatch):
    monkeypatch.setattr(encoders, '_cache', {})
    monkeypatch.setattr(encoders, '_image_cache', {})


def image_bytes():
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8)).save(buffer, format='PNG')
    return buffer.getvalue()


@pytest.mark.parametrize('tower', ['text', 'vision'])
async def test_explicit_tower_config_and_cached_projected_embedding(monkeypatch, tower):
    """The lean backend CI must guard the config argument even without ML extras."""
    config = object()
    config_class = Mock()
    config_class.from_pretrained.return_value = config
    vector = Mock()
    vector.tolist.return_value = [0.25, -0.5]
    model = Mock(return_value=SimpleNamespace(**{f'{"text" if tower == "text" else "image"}_embeds': [vector]}))
    model_class = Mock()

    def load(checkpoint, *, config):
        # A missing/wrong config reproduces the production loading failure.
        assert config is config_class.from_pretrained.return_value
        return SimpleNamespace(eval=lambda: model)

    model_class.from_pretrained.side_effect = load
    processor_class = Mock()
    processor_class.from_pretrained.return_value = Mock(return_value={'input': object()})
    prefix = 'Text' if tower == 'text' else 'Vision'
    processor_name = 'CLIPTokenizerFast' if tower == 'text' else 'CLIPImageProcessor'
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(no_grad=nullcontext))
    monkeypatch.setitem(
        sys.modules,
        'transformers',
        SimpleNamespace(
            **{
                f'CLIP{prefix}Config': config_class,
                f'CLIP{prefix}ModelWithProjection': model_class,
                processor_name: processor_class,
            },
        ),
    )
    for _ in range(2):
        if tower == 'text':
            assert await encoders.encode_text('a mountain', 'clip-ViT-L-14') == [0.25, -0.5]
        else:
            assert await encoders.encode_image(image_bytes(), 'clip-ViT-L-14') == [0.25, -0.5]
    checkpoint = 'openai/clip-vit-large-patch14'
    config_class.from_pretrained.assert_called_once_with(checkpoint)
    model_class.from_pretrained.assert_called_once_with(checkpoint, config=config)
    processor_class.from_pretrained.assert_called_once_with(checkpoint)
    assert model.call_count == 2


def test_real_towers_match_full_clip_checkpoint(tmp_path, monkeypatch):
    """With ML extras installed, check actual config extraction, projection weights and image preprocessing."""
    torch = pytest.importorskip('torch')
    transformers = pytest.importorskip('transformers')
    config = transformers.CLIPConfig(
        text_config={
            'vocab_size': 16,
            'hidden_size': 8,
            'intermediate_size': 16,
            'num_hidden_layers': 1,
            'num_attention_heads': 2,
            'projection_dim': 4,
        },
        vision_config={
            'hidden_size': 8,
            'intermediate_size': 16,
            'num_hidden_layers': 1,
            'num_attention_heads': 2,
            'image_size': 8,
            'patch_size': 4,
            'projection_dim': 4,
        },
        projection_dim=4,
    )
    model = transformers.CLIPModel(config).eval()
    model.save_pretrained(tmp_path)
    # A real saved processor, so the server's own resize/crop/normalize path runs.
    processor = transformers.CLIPImageProcessor(size={'shortest_edge': 8}, crop_size={'height': 8, 'width': 8})
    processor.save_pretrained(tmp_path)
    image = Image.new('RGB', (12, 10), (200, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    text_inputs = {'input_ids': torch.tensor([[1, 3, 2]])}
    image_inputs = processor(images=image, return_tensors='pt')
    monkeypatch.setattr(
        transformers.CLIPTokenizerFast,
        'from_pretrained',
        lambda *args: lambda *args, **kwargs: text_inputs,
    )
    with torch.no_grad():
        expected_text = model.get_text_features(**text_inputs)[0].tolist()
        expected_image = model.get_image_features(**image_inputs)[0].tolist()
    # The towers and CLIPModel compute the same values through separate modules, so float noise
    # varies by CPU (3.6e-7 relative locally, just over the default 1e-6 on a CI runner).
    assert encoders._encode_sync('a mountain', str(tmp_path)) == pytest.approx(expected_text, abs=1e-5)
    assert encoders._encode_image_sync(buffer.getvalue(), str(tmp_path)) == pytest.approx(expected_image, abs=1e-5)
    # Each tower loaded with its own config, not the parent CLIPConfig.
    assert type(encoders._cache[str(tmp_path)][1].config) is transformers.CLIPTextConfig
    assert type(encoders._image_cache[str(tmp_path)][1].config) is transformers.CLIPVisionConfig
