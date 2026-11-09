"""compute_image_patch_vectors gives one image's patch vectors exactly as Dataset.compute_patch_embeddings stores
them (python/fridge/core/patches.py), so vectors computed next to the images and imported later match ones
computed through a server."""

import json
import os
from unittest.mock import Mock

import numpy as np
import pytest
from fridge.core import patches
from fridge.core.patches import PATCH_MODEL_ID, PATCH_MODEL_REVISION, compute_image_patch_vectors, patch_boxes
from PIL import Image

from backend.tests.test_sdk_ingest_retries import dataset


@pytest.mark.parametrize('size', [(1001, 803), (640, 480), (37, 29)])
def test_tiles_cover_every_pixel_once_in_row_order(size):
    width, height = size
    boxes = patch_boxes(width, height, 4, 4)
    covered = np.zeros((height, width), dtype=int)
    for x0, y0, x1, y1 in boxes:
        covered[y0:y1, x0:x1] += 1
    assert (covered == 1).all()
    # r0c0, r0c1, ..., r3c3: tiles of width // 4 by height // 4, the last column and row reaching the edges.
    corners = [(col * (width // 4), row * (height // 4)) for row in range(4) for col in range(4)]
    assert [box[:2] for box in boxes] == corners


def test_only_rgb_images():
    with pytest.raises(ValueError, match='RGB'):
        compute_image_patch_vectors(Image.new('RGBA', (8, 8)), model=object())


@pytest.fixture
def tiny_clip(tmp_path):
    """A real CLIP model, processor and tokenizer, small enough to build here: real transformers code, no download."""
    transformers = pytest.importorskip('transformers')
    pytest.importorskip('torch')
    (tmp_path / 'vocab.json').write_text(json.dumps({'<|startoftext|>': 0, '<|endoftext|>': 1, 'a</w>': 2}))
    (tmp_path / 'merges.txt').write_text('#version: 0.2\n')
    processor = transformers.CLIPProcessor(
        image_processor=transformers.CLIPImageProcessor(size={'shortest_edge': 8}, crop_size={'height': 8, 'width': 8}),
        tokenizer=transformers.CLIPTokenizer(str(tmp_path / 'vocab.json'), str(tmp_path / 'merges.txt')),
    )
    tower = {'hidden_size': 8, 'intermediate_size': 16, 'num_hidden_layers': 1, 'num_attention_heads': 2}
    config = transformers.CLIPConfig(
        text_config={**tower, 'vocab_size': 16, 'bos_token_id': 0, 'eos_token_id': 1, 'projection_dim': 4},
        vision_config={**tower, 'image_size': 8, 'patch_size': 4, 'projection_dim': 4},
        projection_dim=4,
    )
    return transformers.CLIPModel(config).eval(), processor, 'cpu'


def test_matches_what_compute_patch_embeddings_stores(monkeypatch, tiny_clip):
    image = Image.fromarray(np.random.default_rng(0).integers(0, 255, (29, 37, 3), dtype=np.uint8))
    loaded = []

    def load(*args):
        loaded.append(args)
        return tiny_clip

    monkeypatch.setattr(patches, 'load_patch_model', load)
    ds = dataset()
    ds.client.get_samples = Mock(
        side_effect=lambda dataset_id, limit, offset: {
            'total': 1,
            'items': [{'id': 7, 'filepath': 'a.jpg'}][offset : offset + limit],
        },
    )
    stored = []

    def upload(dataset_id, rows):
        stored.extend(rows)
        return {'total': len(rows), 'written': len(rows), 'dropped': 0, 'dropped_sample_ids': []}

    ds.client.clone = Mock(return_value=Mock(upload_patch_embeddings=Mock(side_effect=upload)))
    ds.compute_patch_embeddings(scales=['4x4'], image_loader=lambda filepath: image, reduce=False, num_threads=1)

    assert loaded == [(PATCH_MODEL_ID, PATCH_MODEL_REVISION)]  # the default model, at its pinned revision
    vectors = compute_image_patch_vectors(image, '4x4', model=tiny_clip)
    assert vectors.shape == (16, 4) and vectors.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1, rtol=0, atol=1e-6)
    assert [(row['patch_row'], row['patch_col']) for row in stored] == [(i // 4, i % 4) for i in range(16)]
    assert {(row['sample_id'], row['field_name'], row['patch_scale']) for row in stored} == {(7, 'clip', '4x4')}
    np.testing.assert_allclose([row['vector'] for row in stored], vectors, rtol=0, atol=1e-6)


def test_loading_the_patch_model_downloads_only_the_weights_it_loads(monkeypatch, tmp_path, tiny_clip):
    # The patch model's revision has only pytorch_model.bin; transformers would also download the conversion bot's
    # safetensors copy, which it never loads, unless its switch is on when the model loads.
    model, processor, _ = tiny_clip
    model.save_pretrained(tmp_path / 'clip')
    processor.save_pretrained(tmp_path / 'clip')
    monkeypatch.delenv('DISABLE_SAFETENSORS_CONVERSION', raising=False)
    loaded, _, device = patches.load_patch_model(str(tmp_path / 'clip'), revision=None, device='cpu')
    assert os.environ['DISABLE_SAFETENSORS_CONVERSION'] == '1' and device == 'cpu'
    assert loaded.config.projection_dim == 4
    # Someone who set it keeps their setting.
    monkeypatch.setenv('DISABLE_SAFETENSORS_CONVERSION', '0')
    patches.load_patch_model(str(tmp_path / 'clip'), revision=None, device='cpu')
    assert os.environ['DISABLE_SAFETENSORS_CONVERSION'] == '0'
