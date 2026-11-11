"""Media is streamed, and renders wait for a slot (SEC-6).

A raw S3 original used to be read whole into memory before the answer started, so many large requests at once could
exhaust the API's memory. It now goes out a chunk at a time, byte for byte and with the headers it had, and into the
media cache as it passes. Thumbnails, crops and overlays need the decoded pixels: how many render at once is bounded,
and a request past the bound waits for a slot. Nothing is capped: no size limit, no refusal.
"""

import asyncio
import concurrent.futures
import io
import os
import threading
import time
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from PIL import Image

from backend import compositing, image_modes, main
from backend.storage import StorageRoute

JPEG = b'\xff\xd8\xff\xe0' + bytes(range(256)) * 13_000  # 3.3 MB, a JPEG by its first bytes
PAGE = b'<!doctype html><script>fetch("/api/users")</script>'
MEDIA_HEADERS = {'x-content-type-options': 'nosniff', 'content-security-policy': 'sandbox'}


class Body:
    """A botocore StreamingBody: what was read, and how."""

    def __init__(self, data, fail_after=None):
        self.data, self.position, self.reads, self.closed, self.fail_after = data, 0, [], False, fail_after

    def read(self, amt=None):
        self.reads.append(amt)
        if self.fail_after is not None and self.position >= self.fail_after:
            raise ConnectionResetError('the storage dropped the connection')
        end = len(self.data) if amt is None else min(len(self.data), self.position + amt)
        chunk, self.position = self.data[self.position : end], end
        return chunk

    def close(self):
        self.closed = True


class S3:
    def __init__(self, objects):
        self.objects, self.bodies, self.ranges = objects, [], []

    def get_object(self, Bucket, Key, Range=None):
        data, content_type = self.objects[(Bucket, Key)]
        answer = {'ContentType': content_type}
        if Range is not None:
            start, end = (int(value) for value in Range.removeprefix('bytes=').split('-'))
            end = min(end, len(data) - 1)
            self.ranges.append((start, end))
            answer['ContentRange'] = f'bytes {start}-{end}/{len(data)}'
            data = data[start : end + 1]
        body = Body(data)
        self.bodies.append(body)
        return {**answer, 'Body': body, 'ContentLength': len(data)}


class Inline:
    """The cache writers, run at once, so a test sees what they wrote."""

    def submit(self, fn, *args):
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except BaseException as error:  # pragma: no cover - surfaced through the future
            future.set_exception(error)
        return future


@pytest.fixture
def media(monkeypatch, tmp_path):
    """serve_media for one sample, on an S3 bucket this dataset may read or under the data root."""
    for key, name in [('CROP_CACHE_PATH', 'crop'), ('COMPOSITE_CACHE_PATH', 'composite'), ('S3_LOCAL_PATH', 's3')]:
        monkeypatch.setattr(main, key, str(tmp_path / name))
    monkeypatch.setattr(main, 'DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.setattr(main, '_CACHE_MAX_BYTES', 10_000_000)
    monkeypatch.setattr(main, '_CACHE_WRITERS', Inline())
    monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value={'id': 1, 'role': 'admin'}))
    monkeypatch.setattr(main, 'check_dataset_access', AsyncMock())
    monkeypatch.setattr(main, '_readable_here', AsyncMock(return_value=True))  # not a mirror's
    monkeypatch.setattr(main, 'route_s3_bucket', AsyncMock(return_value=StorageRoute(7, None)))
    s3 = S3({('images', 'a.jpg'): (JPEG, 'image/jpeg'), ('images', 'notes.html'): (PAGE, 'text/html')})
    monkeypatch.setattr(main, 'backend_for_route', AsyncMock(return_value=Mock(s3_client=s3)))
    (tmp_path / 'data').mkdir()

    def sample(path):
        monkeypatch.setattr(main, 'db', Mock(fetchrow=AsyncMock(return_value={'dataset_id': 1, 'path': path})))

    return Mock(s3=s3, sample=sample, root=tmp_path)


def _request(**headers):
    scope = {
        'type': 'http',
        'method': 'GET',
        'path': '/api/samples/7/media',
        'headers': [(k.replace('_', '-').encode(), v.encode()) for k, v in headers.items()],
    }
    return scope, Mock(headers={k.replace('_', '-'): v for k, v in headers.items()})


async def _sent(response, scope):
    """What reaches the client: the status, the headers and the body, and each body message's size."""
    messages = []

    async def receive():
        await asyncio.Event().wait()  # the client stays

    async def send(message):
        messages.append(message)

    await response({**scope, 'asgi': {'spec_version': '2.4'}}, receive, send)
    bodies = [message.get('body', b'') for message in messages[1:]]
    headers = {key.decode(): value.decode() for key, value in messages[0]['headers']}
    return messages[0]['status'], headers, b''.join(bodies), [len(body) for body in bodies]


async def _serve(path, check_access=True, **headers):
    scope, request = _request(**headers)
    return await _sent(await main.serve_media(7, request, check_access=check_access), scope)


@pytest.mark.parametrize('check_access', [True, False])  # False: a follower's request, authorized by its router
async def test_an_s3_original_arrives_whole_with_the_headers_it_had(media, check_access):
    media.sample('s3://images/a.jpg')
    status, headers, body, _ = await _serve('s3://images/a.jpg', check_access)
    assert (status, body) == (200, JPEG)
    assert headers == {
        'content-length': str(len(JPEG)),
        'content-type': 'image/jpeg',
        'x-cache': 'MISS',
        'cache-control': 'private, max-age=3600',
        **MEDIA_HEADERS,
    }
    # The media cache holds it now, and serves it with ranges as before.
    assert (media.root / 's3' / '1' / 'images' / 'a.jpg').read_bytes() == JPEG
    status, headers, body, _ = await _serve('s3://images/a.jpg', check_access, range='bytes=10-19')
    assert (status, body, headers['x-cache']) == (206, JPEG[10:20], 'HIT')


async def test_an_s3_page_is_still_a_download(media):
    media.sample('s3://images/notes.html')
    status, headers, body, _ = await _serve('s3://images/notes.html')
    assert (status, body) == (200, PAGE)
    assert headers == {
        'content-length': str(len(PAGE)),
        'content-type': 'application/octet-stream',
        'content-disposition': 'attachment; filename="notes.html"',
        'x-cache': 'MISS',
        'cache-control': 'private, max-age=3600',
        **MEDIA_HEADERS,
    }


async def test_a_local_original_arrives_whole_with_the_headers_it_had(media):
    image = media.root / 'data' / 'a.jpg'
    image.write_bytes(JPEG)
    media.sample(str(image))
    status, headers, body, _ = await _serve(str(image))
    assert (status, body) == (200, JPEG)
    stat = os.stat(image)
    assert headers == {
        'content-length': str(len(JPEG)),
        'content-type': 'image/jpeg',
        'accept-ranges': 'bytes',
        'last-modified': headers['last-modified'],
        'etag': headers['etag'],
        'cache-control': 'private, max-age=3600',
        **MEDIA_HEADERS,
    }
    assert headers['etag'] and int(stat.st_mtime) > 0
    status, headers, body, _ = await _serve(str(image), range='bytes=100-199')
    assert (status, body, headers['content-range']) == (206, JPEG[100:200], f'bytes 100-199/{len(JPEG)}')


async def test_an_s3_original_is_read_a_chunk_at_a_time(media):
    media.sample('s3://images/a.jpg')
    _, _, body, sizes = await _serve('s3://images/a.jpg')
    assert body == JPEG
    [read] = media.s3.bodies
    chunk = main._MEDIA_CHUNK_BYTES
    assert read.reads == [chunk] * (len(JPEG) // chunk + 2)  # never the whole object at once; the last read is empty
    assert max(sizes) <= chunk and read.closed


async def test_a_client_that_leaves_early_still_leaves_the_whole_object_in_the_cache(media):
    media.sample('s3://images/a.jpg')
    response = await main.serve_media(7, _request()[1])
    first = await anext(response.body_iterator)
    await response.body_iterator.aclose()  # the client went after the first chunk
    assert first == JPEG[: main._MEDIA_CHUNK_BYTES]
    assert (media.root / 's3' / '1' / 'images' / 'a.jpg').read_bytes() == JPEG  # as before, the whole object
    assert media.s3.bodies[0].closed


async def test_an_object_over_the_cache_budget_streams_without_a_copy(media, monkeypatch):
    monkeypatch.setattr(main, '_CACHE_MAX_BYTES', len(JPEG) - 1)
    media.sample('s3://images/a.jpg')
    response = await main.serve_media(7, _request()[1])
    first = await anext(response.body_iterator)
    await response.body_iterator.aclose()  # the client left: nothing more is read
    assert first == JPEG[: main._MEDIA_CHUNK_BYTES] and media.s3.bodies[0].reads == [main._MEDIA_CHUNK_BYTES]
    assert media.s3.bodies[0].closed and not (media.root / 's3').exists()
    _, _, body, _ = await _serve('s3://images/a.jpg')
    assert body == JPEG and not (media.root / 's3').exists()


async def test_a_storage_failure_mid_stream_leaves_no_cache_copy(media, monkeypatch):
    slots = main._CACHE_WRITE_SLOTS._value
    media.sample('s3://images/a.jpg')
    failing = Body(JPEG, fail_after=main._MEDIA_CHUNK_BYTES)
    monkeypatch.setattr(media.s3, 'get_object', lambda **_: {'Body': failing, 'ContentLength': len(JPEG)})
    response = await main.serve_media(7, _request()[1])
    with pytest.raises(ConnectionResetError):
        async for _ in response.body_iterator:
            pass
    folder = media.root / 's3' / '1' / 'images'
    assert failing.closed and list(folder.iterdir()) == []  # no copy, and no temporary file left
    assert main._CACHE_WRITE_SLOTS._value == slots


async def test_the_bit_depth_of_an_s3_png_is_read_from_its_header_alone(media):
    # A 16-bit PNG: its full view is converted. Finding that out reads the header, not the whole object.
    picture = io.BytesIO()
    noise = np.random.default_rng(0).integers(0, 65535, (1024, 1024), dtype=np.uint16)
    Image.fromarray(noise).save(picture, format='PNG')
    media.s3.objects[('images', 'band.png')] = (picture.getvalue(), 'image/png')
    assert len(picture.getvalue()) > 1_000_000
    assert await main._is_high_bit_depth(1, 's3://images/band.png')
    fetched = sum(end + 1 - start for start, end in media.s3.ranges)
    assert all(read is not None for body in media.s3.bodies for read in body.reads)
    assert 0 < fetched <= 128 * 1024
    media.s3.objects[('images', 'photo.png')] = (_png(), 'image/png')
    assert not await main._is_high_bit_depth(1, 's3://images/photo.png')


def _png(width=600, height=300, mode='RGB'):
    output = io.BytesIO()
    Image.new(mode, (width, height), 'red' if mode == 'RGB' else 128).save(output, format='PNG')
    return output.getvalue()


async def test_renders_past_the_limit_wait_for_a_slot_and_all_succeed(media, monkeypatch):
    # Two slots; two thumbnails and an overlay at once. At most two decode together, and all three are answered.
    monkeypatch.setattr(main, '_MEDIA_RENDERS', 2, raising=False)
    monkeypatch.setattr(main, '_render_limit', None, raising=False)
    decoding, most, lock, to_rgb = [0], [0], threading.Lock(), image_modes.to_rgb

    def counted(image):
        with lock:
            decoding[0] += 1
            most[0] = max(most[0], decoding[0])
        time.sleep(0.2)
        with lock:
            decoding[0] -= 1
        return to_rgb(image)

    monkeypatch.setattr(image_modes, 'to_rgb', counted)
    monkeypatch.setattr(compositing, 'to_rgb', counted)
    heatmap = {'type': 'heatmap', 'map_path': 's3://images/heat.png', 'range': [0, 255]}
    monkeypatch.setattr(main, 'db', Mock(fetchrow=AsyncMock(return_value={'hm': heatmap})))
    for name in ('a.png', 'b.png'):
        media.s3.objects[('images', name)] = (_png(), 'image/png')
    media.s3.objects[('images', 'heat.png')] = (_png(mode='L'), 'image/png')

    answers = await asyncio.gather(
        main._serve_crop_resize(1, 1, 's3://images/a.png', 'filepath', None, 256),
        main._serve_crop_resize(2, 1, 's3://images/b.png', 'filepath', None, 256),
        main._serve_composite(3, 1, 's3://images/a.png', 'heat', None, 0.6, width=256),
    )
    assert [(answer.status_code, answer.media_type) for answer in answers] == [(200, 'image/jpeg')] * 3
    assert most[0] == 2
