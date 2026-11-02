import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from fridge.core.dataset import Dataset
from fridge.core.sample import Sample

from backend.routers import versions


def test_checkout_cannot_save_to_live_dataset():
    client = SimpleNamespace(
        get_version_samples=Mock(
            return_value={
                'items': [{'id': 7, 'filepath': 'image.jpg', 'metadata': {'label': 'old'}, 'tags': ['train']}],
            },
        ),
        update_sample=Mock(),
    )
    dataset = Dataset.__new__(Dataset)
    dataset._id = 42
    dataset.client = client
    sample = next(dataset.checkout(version_id=3))
    sample.metadata['label'] = 'new'
    with pytest.raises(ValueError, match='read-only'):
        sample.save()
    client.update_sample.assert_not_called()


def test_live_samples_remain_editable():
    dataset = SimpleNamespace(_id=42, client=SimpleNamespace(update_sample=Mock()))
    sample = Sample.from_row({'id': 7, 'filepath': 'image.jpg', 'metadata': {'label': 'old'}}, dataset)
    sample.metadata['label'] = 'new'
    sample.save()
    assert dataset.client.update_sample.call_args.kwargs['metadata'] == {'label': 'new'}


def test_snapshot_hydrates_arrays_missing_from_current_registry():
    sample = Sample.from_row(
        {
            'id': 7,
            'filepath': 'image.jpg',
            'version_id': 3,
            'array_fields': {'retired_field': ['accepted'], 'nullable_field': None},
            'heading': 120,
        },
    )
    assert sample.array_fields == {'retired_field': ['accepted'], 'nullable_field': None}
    assert sample.heading == 120
    assert sample.location is None


def test_checkout_uses_keyset_paging():
    rows = [{'id': sample_id, 'filepath': f'{sample_id}.jpg'} for sample_id in range(1, 501)]
    client = SimpleNamespace(
        get_version_samples=Mock(side_effect=[{'items': rows, 'snapshot_format': 3}, {'items': []}]),
    )
    dataset = Dataset.__new__(Dataset)
    dataset._id = 42
    dataset.client = client
    assert len(list(dataset.checkout(version_id=3))) == 500
    assert client.get_version_samples.call_args_list[1].kwargs['after_id'] == 500


def test_checkout_paging_remains_compatible_with_older_backends():
    rows = [{'id': sample_id, 'filepath': f'{sample_id}.jpg'} for sample_id in range(1, 501)]
    client = SimpleNamespace(get_version_samples=Mock(side_effect=[{'items': rows}, {'items': []}]))
    dataset = Dataset.__new__(Dataset)
    dataset._id = 42
    dataset.client = client
    assert len(list(dataset.checkout(version_id=3))) == 500
    assert client.get_version_samples.call_args_list[1].kwargs['offset'] == 500
    assert client.get_version_samples.call_args_list[1].kwargs['after_id'] is None


def test_checkout_refuses_a_nonadvancing_cursor():
    rows = [{'id': sample_id, 'filepath': f'{sample_id}.jpg'} for sample_id in range(1, 501)]
    client = SimpleNamespace(get_version_samples=Mock(return_value={'items': rows, 'snapshot_format': 3}))
    dataset = Dataset.__new__(Dataset)
    dataset._id = 42
    dataset.client = client
    with pytest.raises(ValueError, match='cursor did not advance'):
        list(dataset.checkout(version_id=3))


def test_hash_only_cannot_be_read_as_historical_samples(monkeypatch):
    monkeypatch.setattr(versions, 'get_version', AsyncMock(return_value={'include_metadata': False}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(versions.get_version_samples(1, 2, full=True))
    assert error.value.status_code == 400


@pytest.mark.parametrize('limit,offset', [(0, 0), (10001, 0), (10, -1)])
def test_snapshot_rejects_invalid_paging(monkeypatch, limit, offset):
    monkeypatch.setattr(versions, 'get_version', AsyncMock(return_value={'include_metadata': True}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(versions.get_version_samples(1, 2, limit=limit, offset=offset))
    assert error.value.status_code == 400


def test_historical_media_checks_current_dataset_access(monkeypatch):
    from backend import main

    database = SimpleNamespace(fetchrow=AsyncMock(return_value={'dataset_id': 42, 'path': 'image.jpg'}))
    monkeypatch.setattr(main, 'db', database)
    monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value={'id': 9}))
    access = AsyncMock(side_effect=HTTPException(status_code=403, detail='No access'))
    monkeypatch.setattr(main, 'check_dataset_access', access)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.serve_sample_media(7, Mock(), version_id=3))
    assert error.value.status_code == 403
    access.assert_awaited_once_with(42, {'id': 9})
    assert database.fetchrow.call_args.args[1:] == (7, 3)


@pytest.mark.parametrize('transform', ['overlay', 'crop'])
def test_historical_media_passes_version_to_derived_renderers(monkeypatch, transform):
    from backend import main

    monkeypatch.setattr(
        main,
        'db',
        SimpleNamespace(
            fetchrow=AsyncMock(
                return_value={
                    'dataset_id': 42,
                    'path': 'image.jpg',
                },
            ),
        ),
    )
    monkeypatch.setattr(main, 'get_optional_user', AsyncMock(return_value={'id': 9}))
    monkeypatch.setattr(main, 'check_dataset_access', AsyncMock())
    renderer = AsyncMock(return_value='historical image')
    monkeypatch.setattr(main, '_serve_composite' if transform == 'overlay' else '_serve_crop_resize', renderer)
    result = asyncio.run(
        main.serve_sample_media(
            7,
            Mock(),
            version_id=3,
            overlay='heatmap' if transform == 'overlay' else None,
            crop='meta' if transform == 'crop' else None,
        ),
    )
    assert result == 'historical image'
    assert renderer.call_args.kwargs['version_id'] == 3
