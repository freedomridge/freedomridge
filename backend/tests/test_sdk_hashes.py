"""Hash backfills skip object crops and use bounded exports to avoid reopening images."""

import sys
from unittest.mock import Mock

import pytest
from fridge.core import dataset as dataset_module
from fridge.core.hashes import resolve_hash_algorithms
from fridge.core.sample import Sample
from PIL import Image

from backend.tests.test_sdk_ingest_retries import dataset


def hash_dataset(rows, stored):
    ds = dataset()
    ds.iter_samples = Mock(side_effect=lambda **kwargs: iter(Sample.from_row(row) for row in rows))

    def export(dataset_id, field, after_id, limit):
        return {
            'items': [
                {'sample_id': sid, 'field_name': field, 'hash_hex': value}
                for (sid, name), value in sorted(stored.items())
                if name == field and sid > after_id
            ][:limit],
        }

    def upload(dataset_id, payload):
        stored.update({(item['sample_id'], item['field_name']): item['hash_hex'] for item in payload})

    ds.client.export_hashes = Mock(side_effect=export)
    ds.client.upload_hashes = Mock(side_effect=upload)
    return ds


def test_backfill_skips_objects_complete_images_and_existing_fields(monkeypatch):
    rows = [{'id': sid, 'filepath': f'{sid}.jpg', 'media_type': 'image'} for sid in range(1, 9)]
    rows[1]['media_type'] = rows[6]['media_type'] = 'object'
    stored = {(sid, field): '0011223344556677' for sid in (1, 3, 5, 8) for field in ('dhash_8', 'phash_8')}
    stored[4, 'dhash_8'] = 'abcdef0123456789'
    ds = hash_dataset(rows, stored)
    loader = Mock(side_effect=lambda path: Image.new('L', (16, 16), 127))
    result = ds.compute_hashes(image_loader=loader, batch_size=2)
    assert result == dict(computed=2, skipped=4, objects_skipped=2, errors=0)
    assert [call.args[0] for call in loader.call_args_list] == ['4.jpg', '6.jpg']
    assert stored[4, 'dhash_8'] == 'abcdef0123456789'
    assert not any(sid in (2, 7) for sid, field in stored)
    assert any(call.kwargs['after_id'] >= 5 for call in ds.client.export_hashes.call_args_list)
    assert all(call.kwargs['limit'] == 2 for call in ds.client.export_hashes.call_args_list)
    assert len(stored) == 12

    def forbidden():
        pytest.fail('A complete backfill must not initialize an image loader')

    monkeypatch.setattr(dataset_module, '_default_s3_image_loader', forbidden)
    ds.client.upload_hashes.reset_mock()
    assert ds.compute_hashes(batch_size=2) == dict(computed=0, skipped=6, objects_skipped=2, errors=0)
    ds.client.upload_hashes.assert_not_called()


def test_named_algorithms_and_explicit_overwrite():
    rows = [{'id': 1, 'filepath': '1.png'}]
    stored = {(1, 'dhash_8'): 'deadbeef'}
    ds = hash_dataset(rows, stored)
    loader = Mock(side_effect=lambda path: Image.new('RGB', (16, 16)))
    ds.compute_hashes(image_loader=loader, algorithms=['dhash_8'], overwrite=True)
    assert stored[1, 'dhash_8'] == '0000000000000000'
    ds.client.export_hashes.assert_not_called()
    ds.compute_hashes(image_loader=loader, algorithms=['dhash_8', 'phash_8', 'ahash_16', 'whash_8'])
    assert {field for sid, field in stored} == {'dhash_8', 'phash_8', 'ahash_16', 'whash_8'}
    assert len(stored[1, 'ahash_16']) == 64


def test_custom_hash_functions_are_rejected():
    ds = hash_dataset([{'id': 1, 'filepath': '1.png'}], {})
    with pytest.raises(ValueError, match='standard hash names'):
        ds.compute_hashes(algorithms={'custom': lambda image: '00001234'})
    ds.client.upload_hashes.assert_not_called()


@pytest.mark.parametrize('names', [[], ['bogus_8'], ['phash_no'], ['dhash_0'], ['whash_3']])
def test_invalid_names_fail_before_loading(names):
    with pytest.raises(ValueError):
        resolve_hash_algorithms(names)


def test_errors_are_reported_and_can_be_retried():
    rows = [{'id': sid, 'filepath': f'{sid}.png'} for sid in (1, 2, 3)]
    stored: dict[tuple[int, str], str] = {}
    ds = hash_dataset(rows, stored)
    loader = Mock(side_effect=[OSError('bad image'), None, Image.new('RGBA', (16, 16))])
    assert ds.compute_hashes(image_loader=loader) == dict(computed=1, skipped=0, objects_skipped=0, errors=2)
    assert {sid for sid, field in stored} == {3}


@pytest.mark.parametrize('relative', [False, True])
def test_default_backfill_loader_is_local_and_resolves_data_root(tmp_path, monkeypatch, relative):
    path = tmp_path / 'demo.jpg'
    with Image.new('RGB', (16, 16), 'red') as image:
        image.save(path)
    monkeypatch.setenv('FRIDGE_DATA_ROOT', str(tmp_path))
    monkeypatch.setitem(sys.modules, 'boto3', None)  # Local hashes do not need the S3 dependency.
    filepath = path.name if relative else str(path)
    stored: dict[tuple[int, str], str] = {}
    ds = hash_dataset([{'id': 1, 'filepath': filepath}], stored)
    assert ds.compute_hashes() == dict(computed=1, skipped=0, objects_skipped=0, errors=0)
    assert set(stored) == {(1, 'dhash_8'), (1, 'phash_8')}


def test_default_loader_initializes_s3_only_when_requested(monkeypatch):
    from io import BytesIO

    with BytesIO() as buffer, Image.new('RGB', (16, 16), 'red') as image:
        image.save(buffer, format='PNG')
        content = buffer.getvalue()
    s3 = Mock()
    s3.download_fileobj.side_effect = lambda **kwargs: kwargs['Fileobj'].write(content)
    boto3 = Mock()
    boto3.client.return_value = s3
    monkeypatch.setitem(sys.modules, 'boto3', boto3)
    loader = dataset_module._default_s3_image_loader()
    boto3.client.assert_not_called()
    for path in ('s3://fixture/one.png', 's3://fixture/two.png'):
        with loader(path) as image:
            assert image.mode == 'RGB' and image.size == (16, 16)
    boto3.client.assert_called_once_with('s3')
    assert s3.download_fileobj.call_count == 2
