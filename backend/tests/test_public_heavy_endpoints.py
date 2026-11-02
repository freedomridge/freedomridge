"""Admission, cancellation, cache bounds and query-image guards for public browsing."""

import asyncio
import base64
import io
import struct
import sys
import threading
import zlib
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from fastapi import HTTPException, Request
from PIL import Image
from pydantic import ValidationError

from backend import encoders, main
from backend.database import db
from backend.media_cache import write_cache
from backend.models import ImageSearchRequest
from backend.routers import sampling, search


def png(width=600, height=300):
    output = io.BytesIO()
    Image.new('RGB', (width, height), 'red').save(output, format='PNG')
    return output.getvalue()


@pytest.fixture
def cache(monkeypatch, tmp_path):
    for key, name in [('CROP_CACHE_PATH', 'crop'), ('COMPOSITE_CACHE_PATH', 'composite'), ('S3_LOCAL_PATH', 's3')]:
        monkeypatch.setattr(main, key, str(tmp_path / name))
    monkeypatch.setattr(main, '_CACHE_MAX_BYTES', 100_000)
    monkeypatch.setattr(main, '_fetch_media_bytes', AsyncMock(return_value=(png(), 'image/png')))
    return tmp_path


async def test_width_buckets_and_pixel_crop_equivalence(cache):
    first = await main._serve_crop_resize(1, 1, 'image.png', 'filepath', '0,0,0.5,1', 257)
    second = await main._serve_crop_resize(1, 1, 'image.png', 'filepath', '0,0,300,300', 384)
    assert first.body == second.body
    assert first.background is not None
    await first.background()
    assert second.background is not None
    await second.background()
    assert len(list(cache.rglob('*.jpg'))) == 2
    full = await main._serve_crop_resize(1, 1, 'image.png', 'filepath', None, None)
    assert Image.open(io.BytesIO(full.body)).size == (600, 300)


def test_all_thumbnail_widths_use_only_grid_buckets_or_full_size():
    assert {main._thumbnail_width(width) for width in range(1, 4097)} == {*range(256, 1025, 128), None}


def test_crop_extreme_finite_values_clamp_without_allocating_padding():
    assert main._crop_pixels(png(), (0, 0, 1e308, 1e308)) == (0, 0, 600, 300)


@pytest.mark.parametrize('crop', ['nan,0,1,1', '0,inf,1,1', '0,0,-1,1', '0,0,1e309,1'])
def test_bad_crop_is_400(crop):
    with pytest.raises(HTTPException) as error:
        main._parse_crop_spec(crop)
    assert error.value.status_code == 400


async def test_overlay_canonicalization_and_validation(cache, monkeypatch):
    monkeypatch.setattr(
        db,
        'fetchrow',
        AsyncMock(return_value={'hm': {'type': 'heatmap', 'map_path': 'heat.png', 'range': [0, 255]}}),
    )
    first = await main._serve_composite(1, 1, 'base.png', 'heat', ' Viridis ', 0.60001, 0.001, 255, 257)
    assert first.background is not None
    await first.background()
    second = await main._serve_composite(1, 1, 'base.png', 'heat', 'viridis', 0.60, 0, 255, 384)
    assert first.body == second.body and second.headers['X-Cache'] == 'HIT'
    assert len(list(cache.rglob('*.jpg'))) == 1
    # Inverting a heatmap remains supported.
    assert (await main._serve_composite(1, 1, 'base.png', 'heat', 'gray', 0.6, 255, 0)).status_code == 200
    for colormap, alpha, start in [('gray', float('nan'), 0), ('gray', 0.6, float('inf'))]:
        with pytest.raises(HTTPException) as error:
            await main._serve_composite(1, 1, 'base.png', 'heat', colormap, alpha, start)
        assert error.value.status_code == 400


async def test_oversized_derived_image_is_still_served(cache, monkeypatch):
    monkeypatch.setattr(main, '_CACHE_MAX_BYTES', 1)
    result = await main._serve_crop_resize(1, 1, 'image.png', 'filepath', None, None)
    assert result.status_code == 200 and Image.open(io.BytesIO(result.body)).size == (600, 300)
    assert not list(cache.rglob('*.jpg'))


@pytest.mark.parametrize(
    'kwargs',
    [
        {'fields': []},
        {'fields': ['score'] * 65},
        {'fields': [f'score{i}' for i in range(33)]},
        {'fields': ['score', 'score']},
        {'fields': ['score'], 'weights': {'score': float('inf')}},
        {'fields': ['score'], 'weights': {'unknown': 1}},
    ],
)
def test_sparse_fields_are_bounded_and_validated(kwargs):
    with pytest.raises(ValidationError):
        sampling.SparseSampleRequest(count=3, **kwargs)


@pytest.mark.parametrize('field', ["score'); DROP TABLE samples; --", 'score, id', '', 'x' * 129])
def test_sparse_field_identifiers_are_rejected_before_query(field):
    with pytest.raises(HTTPException) as error:
        sampling.SparseSampleRequest(count=1, fields=[field])
    assert error.value.status_code == 400


async def test_sparse_queues_off_loop_times_out_and_stops_on_disconnect(monkeypatch):
    monkeypatch.setattr(sampling, '_SAMPLING_SLOTS', asyncio.Semaphore(1))
    monkeypatch.setattr(sampling, '_QUEUE_SECONDS', 0.03)
    monkeypatch.setattr(sampling, 'get_dataset_array_columns', AsyncMock(return_value=[]))
    fetch = AsyncMock(return_value=[{'id': 1, 'meta_score': '2'}])
    monkeypatch.setattr(db, 'fetch', fetch)
    started, finished = threading.Event(), threading.Event()
    worker_thread = []

    def calculate(rows, dimensions, request, stop, t0):
        worker_thread.append(threading.get_ident())
        started.set()
        try:
            while not stop.wait(0.005):
                pass
            sampling._check_cancelled(stop)
        finally:
            finished.set()

    monkeypatch.setattr(sampling, '_calculate_sample', calculate)
    disconnected = asyncio.Event()

    async def receive():
        await disconnected.wait()
        return {'type': 'http.disconnect'}

    client = Request({'type': 'http'}, receive)
    request = sampling.SparseSampleRequest(count=1, fields=['score'])
    first = asyncio.create_task(sampling.sparse_sample(1, request, client))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.005)
        assert worker_thread == [worker_thread[0]] and worker_thread[0] != threading.get_ident()
        with pytest.raises(HTTPException) as error:
            await sampling.sparse_sample(1, request)
        assert error.value.status_code == 503 and 'busy, retry' in error.value.detail
        assert fetch.await_count == 1  # queued calls don't load all candidates
        disconnected.set()
        response = await first
        assert response.status_code == 499 and finished.is_set()
        assert not sampling._SAMPLING_SLOTS.locked()
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_sparse_full_success_keeps_all_requested_results(monkeypatch):
    monkeypatch.setattr(sampling, '_SAMPLING_SLOTS', asyncio.Semaphore(1))
    monkeypatch.setattr(sampling, 'get_dataset_array_columns', AsyncMock(return_value=[]))
    monkeypatch.setattr(
        db,
        'fetch',
        AsyncMock(return_value=[{'id': i, 'meta_score': str(i)} for i in range(100)]),
    )
    response = await sampling.sparse_sample(1, sampling.SparseSampleRequest(percentage=100, fields=['score']))
    assert response['sample_ids'] == list(range(100))
    assert response['count'] == response['candidate_count'] == 100


@pytest.mark.parametrize('algorithm', [sampling.farthest_point_sampling, sampling.kmeans_diverse_sample])
def test_sampling_algorithms_honor_cancellation(algorithm):
    stop = threading.Event()
    stop.set()
    with pytest.raises(asyncio.CancelledError):
        algorithm(np.arange(100).reshape(-1, 1), 10, stop=stop)


@pytest.mark.parametrize('role', ['guest', 'admin', 'power_user'])
async def test_image_url_is_guest_only_restriction(monkeypatch, role):
    monkeypatch.setattr(search, 'get_optional_user', AsyncMock(return_value={'role': role}))
    monkeypatch.setattr(search, 'check_dataset_access', AsyncMock())
    fetch = AsyncMock(return_value=b'image')
    monkeypatch.setattr(search, '_fetch_image_from_url', fetch)
    monkeypatch.setattr(db, 'fetchval', AsyncMock(return_value=None))
    monkeypatch.setattr(search, 'encode_image', AsyncMock(return_value=[1.0]))
    monkeypatch.setattr(search, 'vector_search', AsyncMock(return_value=[{'id': 1}]))
    request = ImageSearchRequest(dataset_id=1, image_url='https://example.org/image.png')
    if role == 'guest':
        with pytest.raises(HTTPException) as error:
            await search.search_by_image(request, Request({'type': 'http', 'headers': []}))
        assert error.value.status_code == 403 and 'Upload an image' in error.value.detail
        fetch.assert_not_called()
    else:
        assert await search.search_by_image(request, Request({'type': 'http', 'headers': []})) == [{'id': 1}]
        fetch.assert_awaited_once()
    request = ImageSearchRequest(dataset_id=1, image_base64=base64.b64encode(b'image').decode())
    assert await search.search_by_image(request, Request({'type': 'http', 'headers': []})) == [{'id': 1}]


def test_query_pixels_rejected_before_rgb_allocation(monkeypatch):
    monkeypatch.setattr(encoders, '_MAX_QUERY_IMAGE_PIXELS', 50)
    with pytest.raises(HTTPException) as error:
        encoders._decode_query_image(png(10, 10))
    assert error.value.status_code == 400 and 'smaller image' in error.value.detail
    assert encoders._decode_query_image(png(5, 5)).size == (5, 5)
    with pytest.raises(HTTPException) as error:
        encoders._decode_query_image(b'bad image')
    assert error.value.status_code == 400


async def test_eviction_during_full_media_response_preserves_ranges(tmp_path):
    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    source = tmp_path / 'image.jpg'
    source.write_bytes(b'0123456789')
    responses = []

    @app.get('/media')
    async def media(request: Request):
        response = main._cached_media_response(request, str(source), 'image.jpg')
        source.unlink()
        responses.append(response)
        return response

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/media', headers={'Range': 'bytes=2-4'})
    assert response.status_code == 206 and response.content == b'234'
    assert response.headers['Content-Range'] == 'bytes 2-4/10'
    assert isinstance(responses[0], main._CachedFileResponse)
    assert responses[0].cached_file.closed


def test_hourly_prune_preserves_budget_lock(tmp_path):
    from scripts.prune_cache import prune

    write_cache(str(tmp_path / 'image'), b'123', (str(tmp_path),), 10)
    (tmp_path / '.cache-budget.lock').touch()
    prune([str(tmp_path)], 0)
    assert (tmp_path / '.cache-budget.lock').exists()
    assert not (tmp_path / 'image').exists()


@pytest.mark.parametrize('crop', ['0,0,0.5,1', '0,0,300,300', 'meta'])
async def test_crop_hit_never_fetches_source(cache, monkeypatch, crop):
    monkeypatch.setattr(db, 'fetchrow', AsyncMock(return_value={'metadata': {'crop_bbox': [0, 0, 0.5, 1]}}))
    first = await main._serve_crop_resize(1, 1, 'image.png', 'filepath', crop, 384)
    assert first.background is not None
    await first.background()
    fetch = AsyncMock(side_effect=AssertionError('Cache hit must not fetch local or S3 source bytes'))
    monkeypatch.setattr(main, '_fetch_media_bytes', fetch)
    second = await main._serve_crop_resize(1, 1, 'image.png', 'filepath', crop, 384)
    assert second.headers['X-Cache'] == 'HIT'
    assert second.body == first.body
    fetch.assert_not_awaited()


async def test_unknown_colormap_reuses_viridis_cache(cache, monkeypatch):
    heatmap = {'type': 'heatmap', 'map_path': 'heat.png', 'range': [0, 255], 'colormap': 'jet'}
    monkeypatch.setattr(db, 'fetchrow', AsyncMock(return_value={'hm': heatmap}))
    first = await main._serve_composite(1, 1, 'base.png', 'heat', 'viridis', 0.6, width=384)
    assert first.background is not None
    await first.background()
    for colormap in ['jet', 'another_unknown', None]:
        result = await main._serve_composite(1, 1, 'base.png', 'heat', colormap, 0.6, width=384)
        assert result.headers['X-Cache'] == 'HIT'
        assert result.body == first.body
    assert len(list(cache.rglob('*.jpg'))) == 1


@pytest.mark.parametrize('role', ['guest', 'power_user', 'admin'])
async def test_phone_photo_48mp_reaches_encoder_for_every_role(monkeypatch, role):
    from PIL import JpegImagePlugin

    photo = io.BytesIO()
    with Image.new('RGB', (8000, 6000), 'red') as image:
        image.save(photo, format='JPEG')
    decoded_sizes = []
    original_load = JpegImagePlugin.JpegImageFile.load

    def load(image, *args, **kwargs):
        decoded_sizes.append(image.size)
        return original_load(image, *args, **kwargs)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, 'load', load)

    async def encode(data, model):
        result = encoders._decode_query_image(data)
        assert result.mode == 'RGB' and max(result.size) <= 1024
        return [1.0]

    monkeypatch.setattr(search, 'get_optional_user', AsyncMock(return_value={'role': role}))
    monkeypatch.setattr(search, 'check_dataset_access', AsyncMock())
    monkeypatch.setattr(search, 'encode_image', encode)
    monkeypatch.setattr(db, 'fetchval', AsyncMock(return_value=None))
    monkeypatch.setattr(search, 'vector_search', AsyncMock(return_value=[{'id': 1}]))
    request = ImageSearchRequest(dataset_id=1, image_base64=base64.b64encode(photo.getvalue()).decode())
    assert await search.search_by_image(request, Request({'type': 'http', 'headers': []})) == [{'id': 1}]
    assert decoded_sizes and all(w * h < 48_000_000 for w, h in decoded_sizes)


def test_png_query_downscales_and_pillow_bomb_is_readable(monkeypatch):
    assert encoders._decode_query_image(png(3000, 2000)).size == (1024, 683)
    monkeypatch.setattr(Image, 'open', lambda *args: (_ for _ in ()).throw(Image.DecompressionBombError()))
    with pytest.raises(HTTPException) as error:
        encoders._decode_query_image(b'huge dimensions')
    assert error.value.status_code == 400
    assert '120 million' in error.value.detail


@pytest.mark.parametrize('size', [(15000, 10000), (20000, 10000)])
def test_query_header_limit_and_pillow_bomb_do_not_decode_pixels(monkeypatch, size):
    data = bytearray(png(1, 1))
    data[16:24] = struct.pack('>II', *size)
    data[29:33] = struct.pack('>I', zlib.crc32(data[12:29]))
    monkeypatch.setattr(Image.Image, 'convert', lambda *args: pytest.fail('Oversized image allocated RGB'))
    with pytest.raises(HTTPException) as error:
        encoders._decode_query_image(bytes(data))
    assert error.value.status_code == 400 and 'smaller image' in error.value.detail


@pytest.mark.parametrize('size', [(10900, 10900), (8000, 6000)])
def test_large_rgba_png_rejected_from_header_before_pixel_allocation(monkeypatch, size):
    from PIL import PngImagePlugin

    # Build a valid, highly compressed RGBA PNG one row at a time; the test itself
    # must not allocate the hundreds of MB that a full-frame decode would require.
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))

    compressor = zlib.compressobj()
    row = bytes(1 + size[0] * 4)  # PNG filter byte followed by transparent RGBA pixels
    compressed = b''.join(compressor.compress(row) for _ in range(size[1])) + compressor.flush()
    data = (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', *size, 8, 6, 0, 0, 0))
        + chunk(b'IDAT', compressed)
        + chunk(b'IEND', b'')
    )
    load = Mock(side_effect=AssertionError('Oversized PNG must not load pixels'))
    monkeypatch.setattr(PngImagePlugin.PngImageFile, 'load', load)
    with pytest.raises(HTTPException) as error:
        encoders._decode_query_image(data)
    assert error.value.status_code == 400 and '40 million pixels' in error.value.detail
    load.assert_not_called()


@pytest.mark.parametrize('missing', ['torch', 'transformers'])
async def test_image_search_without_encoder_returns_501_before_decode(monkeypatch, missing):
    monkeypatch.setitem(sys.modules, missing, None)
    if missing == 'transformers':
        monkeypatch.setitem(sys.modules, 'torch', Mock())
    decode = Mock(side_effect=AssertionError('Unavailable encoder must not decode the upload'))
    monkeypatch.setattr(encoders, '_decode_query_image', decode)
    monkeypatch.setattr(search, 'get_optional_user', AsyncMock(return_value={'role': 'guest'}))
    monkeypatch.setattr(search, 'check_dataset_access', AsyncMock())
    monkeypatch.setattr(db, 'fetchval', AsyncMock(return_value=None))
    request = ImageSearchRequest(dataset_id=1, image_base64=base64.b64encode(b'image').decode())
    with pytest.raises(HTTPException) as error:
        await search.search_by_image(request, Request({'type': 'http', 'headers': []}))
    assert error.value.status_code == 501 and 'torch + transformers' in error.value.detail
    decode.assert_not_called()
