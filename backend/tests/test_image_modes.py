"""16-bit and float greyscale images must be neither white nor dark when converted to 8-bit RGB.

Pillow opens 16-bit greyscale PNG/TIFF as mode I;16 and Image.convert('RGB') clips at 255, so
thumbnails, overlay bases, query images, metrics and embeddings of a real 16-bit image (a
multispectral band, say) saw a pure-white picture. to_rgb (backend and SDK twins) stretches each
image's 0.5th to 99.5th percentile over 0..255 instead, so a 12-bit sensor's images aren't dark, and
the full view of such a sample image is converted the same way.
"""

import io
import os
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from fridge.core.image_modes import to_rgb as sdk_to_rgb
from PIL import Image

from backend import compositing, encoders, main
from backend.database import db
from backend.image_modes import to_rgb as backend_to_rgb

VALUES = np.array([[0, 255, 256, 13000, 32768, 47000, 65280, 65535]], dtype=np.uint16)


def sixteen_bit(mode='I;16', values=VALUES):
    if mode == 'I;16B':
        return Image.frombytes(mode, values.shape[::-1], values.astype('>u2').tobytes())
    if mode == 'I':
        return Image.fromarray(values.astype(np.int32))
    return Image.fromarray(values)


def png_bytes(image):
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    return buf.getvalue()


TWELVE_BIT_RAMP = np.tile(np.arange(4096, dtype=np.uint16), (4, 1))


@pytest.mark.parametrize('to_rgb', [backend_to_rgb, sdk_to_rgb], ids=['backend', 'sdk'])
@pytest.mark.parametrize('mode', ['I;16', 'I;16B', 'I'])
def test_a_twelve_bit_image_spans_the_whole_range_not_dark(to_rgb, mode):
    image = sixteen_bit(mode, TWELVE_BIT_RAMP)
    assert image.mode == mode
    rgb = to_rgb(image)
    assert rgb.mode == 'RGB'
    pixels = np.asarray(rgb)
    assert (pixels[:, :, 0] == pixels[:, :, 1]).all() and (pixels[:, :, 0] == pixels[:, :, 2]).all()
    assert pixels.min() == 0 and pixels.max() == 255 and abs(float(pixels.mean()) - 127.5) < 2


@pytest.mark.parametrize('to_rgb', [backend_to_rgb, sdk_to_rgb], ids=['backend', 'sdk'])
def test_a_few_hot_pixels_dont_set_the_range(to_rgb):
    values = np.full((50, 50), 1500, dtype=np.uint16)
    values[:25] = 1000
    values[0, :5] = 65535  # 0.2% of the pixels, under the 0.5% cut
    pixels = np.asarray(to_rgb(Image.fromarray(values)))[:, :, 0]
    assert pixels[1:25].max() < 5 and pixels[25:].min() > 245 and pixels[0, :5].min() == 255


@pytest.mark.parametrize('to_rgb', [backend_to_rgb, sdk_to_rgb], ids=['backend', 'sdk'])
def test_float_and_signed_images_are_stretched_too(to_rgb):
    floats = np.linspace(-20.5, 40.0, 1000, dtype=np.float32).reshape(10, 100)  # a thermal frame in degrees
    pixels = np.asarray(to_rgb(Image.fromarray(floats, 'F')))
    assert pixels.min() == 0 and pixels.max() == 255
    signed = Image.fromarray(np.arange(-100, 100, dtype=np.int32).reshape(10, 20))
    assert np.asarray(to_rgb(signed)).min() == 0 and np.asarray(to_rgb(signed)).max() == 255


@pytest.mark.parametrize('to_rgb', [backend_to_rgb, sdk_to_rgb], ids=['backend', 'sdk'])
def test_a_single_value_is_mid_grey(to_rgb):
    assert set(np.asarray(to_rgb(sixteen_bit(values=np.full((3, 3), 4000, dtype=np.uint16)))).ravel()) == {128}


@pytest.mark.parametrize('to_rgb', [backend_to_rgb, sdk_to_rgb], ids=['backend', 'sdk'])
@pytest.mark.parametrize('mode', ['L', 'LA', 'P', 'RGBA', 'CMYK', 'RGB'])
def test_eight_bit_modes_convert_exactly_as_before(to_rgb, mode):
    rng = np.random.default_rng(0)
    image = Image.fromarray(rng.integers(0, 256, (6, 5, 3), dtype=np.uint8), 'RGB').convert(mode)
    assert to_rgb(image).tobytes() == image.convert('RGB').tobytes()


async def test_grid_thumbnail_of_a_16_bit_png_is_not_white(monkeypatch, tmp_path):
    body = png_bytes(sixteen_bit(values=np.full((40, 60), 32768, dtype=np.uint16)))
    monkeypatch.setattr(db, 'fetchrow', AsyncMock(return_value={'dataset_id': 8, 'path': '/data/band_NIR.png'}))
    monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value={'id': 1}))
    monkeypatch.setattr(main, 'check_dataset_access', AsyncMock())
    monkeypatch.setattr(main, '_fetch_media_bytes', AsyncMock(return_value=(body, 'image/png')))
    monkeypatch.setattr(main, '_readable_here', AsyncMock(return_value=True))  # not a mirror's
    monkeypatch.setattr(main, 'CROP_CACHE_PATH', str(tmp_path))
    response = await main.serve_sample_media(7, Mock(), w=32)
    assert response.media_type == 'image/jpeg'  # the resize path, not the raw PNG
    thumbnail = np.asarray(Image.open(io.BytesIO(response.body)))
    assert abs(float(thumbnail.mean()) - 128) < 3


def test_query_image_of_a_16_bit_png_is_not_white():
    image = encoders._decode_query_image(png_bytes(sixteen_bit()))
    assert image.mode == 'RGB'
    # Eight pixels: the cut keeps every one, so the image's lowest and highest values span 0..255.
    np.testing.assert_allclose(np.asarray(image)[0, :, 0], VALUES[0].astype(float) * 255 / 65535, atol=1)


def test_overlay_base_of_a_16_bit_png_is_not_white():
    base = sixteen_bit(values=np.full((4, 4), 32768, dtype=np.uint16))
    empty_heatmap = Image.new('L', (4, 4), 0)  # 0 is transparent: the base shows through
    out = compositing.composite_heatmap(png_bytes(base), png_bytes(empty_heatmap), 1, 255, 'viridis', 0.6)
    assert np.asarray(Image.open(io.BytesIO(out)).convert('RGB')).max() == 128


def test_sdk_metrics_of_a_16_bit_image_describe_the_image_not_white():
    pytest.importorskip('cv2')
    from fridge import compute_image_metrics

    metrics = compute_image_metrics(sixteen_bit(values=np.full((32, 32), 32768, dtype=np.uint16)))
    assert abs(metrics['brightness'] - 128) < 1
    assert metrics['pct_overexposed'] == 0


def media_route(monkeypatch, tmp_path, path):
    monkeypatch.setattr(db, 'fetchrow', AsyncMock(return_value={'dataset_id': 8, 'path': path}))
    monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value={'id': 1}))
    monkeypatch.setattr(main, 'check_dataset_access', AsyncMock())
    monkeypatch.setattr(main, '_resolve_local_path', lambda value: str(tmp_path / os.path.basename(value)))
    monkeypatch.setattr(main, 'CROP_CACHE_PATH', str(tmp_path / 'crop'))


async def test_the_full_view_of_a_16_bit_image_is_converted_as_its_thumbnails(monkeypatch, tmp_path):
    sixteen_bit(values=TWELVE_BIT_RAMP).save(tmp_path / 'band_NIR.png')
    Image.new('L', (8, 8), 200).save(tmp_path / 'photo.png')
    media_route(monkeypatch, tmp_path, '/data/band_NIR.png')
    assert await main._is_high_bit_depth(8, '/data/band_NIR.png')
    assert not await main._is_high_bit_depth(8, '/data/photo.png')
    assert not await main._is_high_bit_depth(8, '/data/photo.jpg')  # only PNG and TIFF are looked at
    response = await main.serve_sample_media(7, Mock())  # no w: the full view
    assert response.media_type == 'image/jpeg'
    full = np.asarray(Image.open(io.BytesIO(response.body)))
    assert full.shape[:2] == TWELVE_BIT_RAMP.shape and full.max() > 250  # full size, and not dark


async def test_a_16_bit_mask_keeps_its_raw_values(monkeypatch, tmp_path):
    # Class ids, not brightness: only a sample's own image (field=filepath) is converted.
    media_route(monkeypatch, tmp_path, '/data/mask.png')
    monkeypatch.setattr(main, '_is_high_bit_depth', AsyncMock(return_value=True))
    monkeypatch.setattr(main, '_serve_crop_resize', AsyncMock(side_effect=AssertionError('converted')))
    (tmp_path / 'mask.png').write_bytes(png_bytes(sixteen_bit(values=np.full((4, 4), 300, dtype=np.uint16))))
    monkeypatch.setattr(main, '_full_media_file_response', lambda request, resolved, **kwargs: ('raw', resolved))
    assert await main.serve_sample_media(7, Mock(), field='ground_truth.mask_path') == (
        'raw',
        str(tmp_path / 'mask.png'),
    )
