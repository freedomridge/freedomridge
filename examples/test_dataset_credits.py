"""Shared credits and idempotent description-only backfills, without a server."""

from importlib import import_module
from unittest.mock import Mock

import pytest
from requests import HTTPError, Response

from examples._shared.credits import DATASET_CREDITS, KITTI_ATTRIBUTION, OSV5M_ATTRIBUTION
from scripts.backfill_dataset_credits import backfill, main


def test_shared_credits_preserve_licenses_and_attribution():
    assert set(DATASET_CREDITS) == {'coco-val2017', 'open-images-v7-val', 'inaturalist-2021-val', 'kitti', 'osv5m-test'}
    assert 'annotations CC BY 4.0' in DATASET_CREDITS['coco-val2017']
    assert 'Flickr creators' in DATASET_CREDITS['coco-val2017']
    assert 'annotations CC BY 4.0' in DATASET_CREDITS['open-images-v7-val']
    assert 'per-image Author, License' in DATASET_CREDITS['open-images-v7-val']
    assert 'including non-commercial terms' in DATASET_CREDITS['inaturalist-2021-val']
    assert KITTI_ATTRIBUTION in DATASET_CREDITS['kitti']
    assert 'Non-commercial evaluation; crops share alike.' in DATASET_CREDITS['kitti']
    assert DATASET_CREDITS['osv5m-test'] == OSV5M_ATTRIBUTION
    for name, description in DATASET_CREDITS.items():
        if name != 'osv5m-test':
            assert 'https://' in description


def test_backfill_is_idempotent_by_name_and_never_creates_datasets():
    rows = [{'id': i, 'name': name, 'description': 'old text'} for i, name in enumerate(DATASET_CREDITS)]
    rows.extend([{'id': 99, 'name': 'personal', 'description': 'keep'}, {'id': 100, 'name': 'unrelated'}])
    client = Mock()
    client.get_datasets.return_value = rows
    client.update_dataset.side_effect = lambda dataset_id, **kw: rows[dataset_id].update(kw)
    assert backfill(client, DATASET_CREDITS, dry_run=True) == list(DATASET_CREDITS)
    client.update_dataset.assert_not_called()
    assert backfill(client, DATASET_CREDITS) == list(DATASET_CREDITS)
    assert client.update_dataset.call_count == 5
    assert backfill(client, DATASET_CREDITS) == []
    assert client.update_dataset.call_count == 5
    assert rows[-2]['description'] == 'keep'
    client.create_dataset.assert_not_called()
    client.get_datasets.return_value = [{'id': 1, 'name': 'kitti', 'deleting': True}]
    assert backfill(client, DATASET_CREDITS) == []


def test_backfill_uses_importer_token_auth(monkeypatch):
    from scripts import backfill_dataset_credits as script

    monkeypatch.setenv('FRIDGE_API_TOKEN', 'test-only-token')
    monkeypatch.setattr('sys.argv', ['backfill', '--api-url', 'http://127.0.0.1:8098/api', '--dry-run'])
    client = Mock()
    client.get_datasets.return_value = []
    constructor = Mock(return_value=client)
    monkeypatch.setattr(script, 'Client', constructor)
    main()
    constructor.assert_called_once_with(base_url='http://127.0.0.1:8098/api')
    client.login.assert_not_called()
    client.session.close.assert_called_once()


def test_every_demo_importer_uses_shared_credits_for_its_default_name():
    for name in ['coco', 'open_images', 'inaturalist', 'kitti', 'osv5m']:
        importer = import_module(f'examples.{name}.ingest')
        assert importer.DATASET_CREDITS is DATASET_CREDITS
        assert importer.build_parser().get_default('dataset_name') in DATASET_CREDITS


@pytest.mark.parametrize('status', [401, 403])
def test_backfill_auth_error_explains_admin_token_without_traceback(monkeypatch, capsys, status):
    from scripts import backfill_dataset_credits as script

    monkeypatch.setenv('FRIDGE_API_TOKEN', 'non-admin-test-token')
    monkeypatch.setattr('sys.argv', ['backfill', '--api-url', 'http://127.0.0.1:8088/api'])
    client = Mock()
    client.get_datasets.return_value = [{'id': 1, 'name': 'kitti', 'description': 'old'}]
    response = Response()
    response.status_code = status
    client.update_dataset.side_effect = HTTPError('Permission denied', response=response)
    monkeypatch.setattr(script, 'Client', Mock(return_value=client))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert 'require an admin API token' in message and 'FRIDGE_API_TOKEN' in message
    assert 'Traceback' not in message
    client.session.close.assert_called_once()
