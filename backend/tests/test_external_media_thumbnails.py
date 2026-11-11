"""External demo assets must render in the grid, whose media requests always include w."""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from backend import main
from backend.database import db


@pytest.fixture
def external_media(monkeypatch):
    url = 'https://assets.example.org/rotated.png'
    monkeypatch.setattr(db, 'fetchrow', AsyncMock(return_value={'dataset_id': 8, 'path': url}))
    monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value={'id': 1}))
    access = AsyncMock()
    monkeypatch.setattr(main, 'check_dataset_access', access)
    fetch = AsyncMock(side_effect=AssertionError('External media must not be fetched by the backend'))
    monkeypatch.setattr(main, '_fetch_media_bytes', fetch)
    return url, access, fetch


@pytest.mark.parametrize('width', [None, 256, 1024])
async def test_full_image_and_grid_thumbnail_redirect_after_access_check(external_media, width):
    url, access, fetch = external_media
    response = await main.serve_sample_media(7, Mock(), w=width)
    assert response.status_code == 307
    assert response.headers['location'] == url
    access.assert_awaited_once_with(8, {'id': 1})
    fetch.assert_not_awaited()


async def test_external_crops_remain_rejected(external_media):
    _, _, fetch = external_media
    with pytest.raises(HTTPException) as error:
        await main.serve_sample_media(7, Mock(), crop='0,0,0.5,0.5', w=256)
    assert error.value.status_code == 400
    fetch.assert_not_awaited()


async def test_thumbnail_redirect_does_not_bypass_dataset_access(external_media):
    _, access, fetch = external_media
    access.side_effect = HTTPException(status_code=403)
    with pytest.raises(HTTPException) as error:
        await main.serve_sample_media(7, Mock(), w=256)
    assert error.value.status_code == 404  # as for a missing sample
    fetch.assert_not_awaited()
