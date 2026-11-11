"""/api/samples/N/media asks who is calling before it looks anything up, and checks dataset access before it says
anything about the field. It answered 404 for a missing sample, 401 for an existing one, and another 404 for an
existing sample whose field was empty: sample ids and field presence could be probed with no credentials."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend import main


def _samples(row):
    class Samples:
        async def fetchrow(self, query, *args):
            return row

    return Samples()


@pytest.mark.parametrize('row', [None, {'dataset_id': 1, 'path': '/data/a.jpg'}, {'dataset_id': 1, 'path': None}])
def test_anonymous_caller_cannot_tell_which_samples_or_fields_exist(monkeypatch, row):
    monkeypatch.setattr(main, 'db', _samples(row))
    monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.serve_media(7, MagicMock(), field='secret_label'))
    assert exc.value.status_code == 401


def test_a_user_without_access_learns_nothing_of_a_sample_s_fields(monkeypatch):
    monkeypatch.setattr(main, 'db', _samples({'dataset_id': 1, 'path': None}))
    monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value={'id': 9, 'role': 'user'}))
    monkeypatch.setattr(
        main,
        'check_dataset_access',
        AsyncMock(side_effect=HTTPException(status_code=403, detail='no')),
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.serve_media(7, MagicMock(), field='secret_label'))
    # was 404 "has no value for field 'secret_label'"; then 403, which said the sample exists
    assert (exc.value.status_code, exc.value.detail) == (404, 'Sample not found')


def test_a_follower_s_authorized_request_is_not_asked_who_it_is(monkeypatch):
    """check_access off (the followers router has authorized it): no user is looked up, and a missing sample is 404."""
    monkeypatch.setattr(main, 'db', _samples(None))
    asked = AsyncMock(return_value=None)
    monkeypatch.setattr(main, 'get_optional_user', asked)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.serve_media(7, MagicMock(), check_access=False))
    assert exc.value.status_code == 404 and asked.await_count == 0
