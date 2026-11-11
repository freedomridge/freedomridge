"""A cached thumbnail, crop or overlay is served only while the dataset may still read its source's bucket.

Restricting a storage configuration to other datasets stopped the originals (403) but not their derived images:
the crop and composite caches answered before the bucket was checked.
"""

import io
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from PIL import Image

from backend import main
from backend.database import db
from backend.storage import StorageRoute


def png(width=600, height=300):
    output = io.BytesIO()
    Image.new('RGB', (width, height), 'red').save(output, format='PNG')
    return output.getvalue()


@pytest.fixture
def storage(monkeypatch, tmp_path):
    """Derivative caches in a temporary folder, sources that read as a PNG, and buckets this dataset may use."""
    for key, name in [('CROP_CACHE_PATH', 'crop'), ('COMPOSITE_CACHE_PATH', 'composite'), ('S3_LOCAL_PATH', 's3')]:
        monkeypatch.setattr(main, key, str(tmp_path / name))
    monkeypatch.setattr(main, '_CACHE_MAX_BYTES', 1_000_000)
    monkeypatch.setattr(main, '_fetch_media_bytes', AsyncMock(return_value=(png(), 'image/png')))
    allowed = {'images', 'heatmaps'}

    async def route(dataset_id, bucket, source=None):
        return StorageRoute(7, None) if bucket in allowed else None

    monkeypatch.setattr(main, 'route_s3_bucket', route)
    return allowed


async def test_a_cached_thumbnail_stops_when_its_bucket_is_withdrawn(storage):
    first = await main._serve_crop_resize(1, 1, 's3://images/a.jpg', 'filepath', None, 384)
    assert first.background is not None
    await first.background()
    second = await main._serve_crop_resize(1, 1, 's3://images/a.jpg', 'filepath', None, 384)
    assert second.headers['X-Cache'] == 'HIT'

    storage.discard('images')  # an admin restricts the configuration to other datasets
    with pytest.raises(HTTPException) as refused:
        await main._serve_crop_resize(1, 1, 's3://images/a.jpg', 'filepath', None, 384)
    assert refused.value.status_code == 403


async def test_a_cached_overlay_stops_when_either_bucket_is_withdrawn(storage, monkeypatch):
    heatmap = {'type': 'heatmap', 'map_path': 's3://heatmaps/a.png', 'range': [0, 255]}
    monkeypatch.setattr(db, 'fetchrow', AsyncMock(return_value={'hm': heatmap}))
    first = await main._serve_composite(1, 1, 's3://images/a.jpg', 'heat', None, 0.6, width=384)
    assert first.background is not None
    await first.background()
    second = await main._serve_composite(1, 1, 's3://images/a.jpg', 'heat', None, 0.6, width=384)
    assert second.headers['X-Cache'] == 'HIT'

    storage.discard('heatmaps')  # only the overlay's bucket
    with pytest.raises(HTTPException) as refused:
        await main._serve_composite(1, 1, 's3://images/a.jpg', 'heat', None, 0.6, width=384)
    assert refused.value.status_code == 403


async def test_a_cached_thumbnail_of_a_file_outside_the_data_root_is_refused(storage, monkeypatch, tmp_path):
    source = str(tmp_path / 'data' / 'a.jpg')
    monkeypatch.setattr(main, 'DATA_ROOT', str(tmp_path / 'data'))
    first = await main._serve_crop_resize(1, 1, source, 'filepath', None, 384)
    assert first.background is not None
    await first.background()

    monkeypatch.setattr(main, 'DATA_ROOT', str(tmp_path / 'moved'))  # the data root no longer holds the file
    with pytest.raises(HTTPException) as refused:
        await main._serve_crop_resize(1, 1, source, 'filepath', None, 384)
    assert refused.value.status_code == 403
