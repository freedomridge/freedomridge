"""The SDK's per-image statistic helpers must be correct on known inputs.

These are the module-level pure functions behind Dataset.compute_metadata() (width/
height/aspect/megapixels/file size) and the new compute_metrics() fields (saturation,
exposure clipping). PIL + numpy only — deliberately no cv2, so they run in CI's lean
env. Synthetic images with known properties make the expected values exact.
"""

import numpy as np
import pytest
from fridge.core import dataset
from fridge.core.dataset import _exposure_clipping, _hsv_saturation, _mean_hsv_saturation, _media_metadata, _saturation
from PIL import Image


def test_media_metadata_dimensions_and_aspect(tmp_path):
    f = tmp_path / "img.png"
    Image.new("RGB", (200, 100)).save(f)
    with Image.open(f) as img:
        meta = _media_metadata(img, str(f))
    assert meta["width"] == 200
    assert meta["height"] == 100
    assert meta["aspect_ratio"] == 2.0
    assert meta["megapixels"] == 0.02
    assert meta["file_size_bytes"] == f.stat().st_size


def test_media_metadata_remote_path_omits_file_size():
    meta = _media_metadata(Image.new("RGB", (64, 64)), "s3://bucket/key.jpg")
    assert "file_size_bytes" not in meta
    assert meta["width"] == 64


def test_saturation_extremes():
    vivid = Image.new("RGB", (32, 32), (255, 0, 0))  # pure red — fully saturated
    gray = Image.new("RGB", (32, 32), (128, 128, 128))  # no chroma at all
    assert _saturation(vivid) == 255.0
    assert _saturation(gray) == 0.0


def test_hsv_saturation_matches_pil_for_every_rgb_value():
    # All 2**24 RGB triples, one per pixel: the numpy S channel must equal PIL's HSV conversion everywhere.
    values = np.arange(1 << 24, dtype=np.uint32)
    rgb = np.stack([values >> 16, (values >> 8) & 255, values & 255], axis=-1).astype(np.uint8).reshape(4096, 4096, 3)
    expected = np.asarray(Image.fromarray(rgb, "RGB").convert("HSV"))[:, :, 1]
    actual = _hsv_saturation(rgb)
    assert actual.dtype == np.uint8 and actual.shape == expected.shape
    mismatched = np.flatnonzero(actual != expected)
    examples = rgb.reshape(-1, 3)[mismatched[:5]].tolist()
    assert mismatched.size == 0, f"{mismatched.size} RGB values differ, e.g. {examples}"


@pytest.mark.parametrize("strip_pixels", [1, 7, 97, 3000, 1 << 16])
@pytest.mark.parametrize("height, width", [(1, 1), (1, 97), (97, 1), (61, 53), (240, 320)])
def test_mean_hsv_saturation_in_strips_equals_the_whole_image_mean(monkeypatch, strip_pixels, height, width):
    # Strips of one row, of several rows with a shorter last one, and the whole image in one strip.
    monkeypatch.setattr(dataset, "_SATURATION_STRIP_PIXELS", strip_pixels)
    rgb = np.random.default_rng(height * width).integers(0, 256, (height, width, 3), dtype=np.uint8)
    expected = float(_hsv_saturation(rgb).mean())
    assert _mean_hsv_saturation(rgb) == expected
    assert _saturation(Image.fromarray(rgb, "RGB")) == expected


def test_exposure_clipping_black_white_and_mid():
    black = Image.new("L", (32, 32), 0)
    white = Image.new("L", (32, 32), 255)
    mid = Image.new("L", (32, 32), 128)
    assert _exposure_clipping(black) == (100.0, 0.0)
    assert _exposure_clipping(white) == (0.0, 100.0)
    assert _exposure_clipping(mid) == (0.0, 0.0)


def test_exposure_clipping_partial():
    # Top half crushed to black, bottom half mid-gray → exactly 50% underexposed.
    arr = np.full((32, 32), 128, dtype=np.uint8)
    arr[:16, :] = 0
    under, over = _exposure_clipping(Image.fromarray(arr, mode="L"))
    assert under == 50.0
    assert over == 0.0
