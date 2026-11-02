"""The SDK exposes explicit declaration corrections without rewriting sample values."""

from unittest.mock import Mock

import pytest
import requests
from fridge.core.client import Client
from fridge.core.dataset import Dataset


@pytest.mark.parametrize('field_type', ['unknown', 'boolean', 'number', 'string', 'datetime', 'list', 'object'])
def test_client_sets_declared_type_with_patch(field_type):
    client = Client.__new__(Client)
    client.base_url = 'http://test.invalid/api'
    response = Mock()
    response.ok = True
    response.json.return_value = {'status': 'ok', 'field_name': 'score', 'field_type': field_type}
    client.session = Mock()
    client.session.patch.return_value = response
    assert client.set_field_type(7, 'score', field_type) == response.json.return_value
    client.session.patch.assert_called_once_with(
        'http://test.invalid/api/datasets/7/fields/score/type',
        json={'field_type': field_type},
    )


def test_client_type_change_preserves_actionable_server_errors():
    client = Client.__new__(Client)
    client.base_url = 'http://test.invalid/api'
    response = Mock(ok=False, status_code=409, reason='Conflict')
    response.json.return_value = {'detail': 'Remove the index before changing the declared type'}
    client.session = Mock()
    client.session.patch.return_value = response
    with pytest.raises(requests.HTTPError, match='Remove the index'):
        client.set_field_type(7, 'score', 'string')


def test_dataset_type_change_marks_local_state_dirty_only_on_success():
    dataset = Dataset.__new__(Dataset)
    dataset._id = 7
    dataset._dirty = False
    dataset.client = Mock()
    dataset.client.set_field_type.return_value = {'status': 'ok'}
    assert dataset.set_field_type('score', 'number') == {'status': 'ok'}
    dataset.client.set_field_type.assert_called_once_with(7, 'score', 'number')
    assert dataset._dirty is True
    dataset._dirty = False
    dataset.client.set_field_type.side_effect = requests.HTTPError('remove the index first')
    with pytest.raises(requests.HTTPError, match='remove the index first'):
        dataset.set_field_type('score', 'string')
    assert dataset._dirty is False
