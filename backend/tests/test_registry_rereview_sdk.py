"""An ingest retry conflict is an error, not an acknowledged duplicate result."""

from unittest.mock import Mock

import pytest
import requests
from fridge.core.client import Client


@pytest.mark.parametrize('mode', ['error', 'skip', 'update'])
def test_busy_ingest_conflict_raises_in_every_duplicate_mode(mode):
    client = Client.__new__(Client)
    client.base_url = 'http://test.invalid/api'
    response = Mock(ok=False, status_code=409)
    response.json.return_value = {'detail': 'Dataset has an active field operation or write; retry'}
    client.session = Mock()
    client.session.post.return_value = response
    with pytest.raises(requests.HTTPError, match='retry') as error:
        client.add_samples(1, [{'filepath': 'image.jpg'}], on_duplicate=mode)
    assert error.value.response is response


def test_duplicate_conflict_retains_committed_ids_and_existing_matches():
    client = Client.__new__(Client)
    client.base_url = 'http://test.invalid/api'
    response = Mock(ok=False, status_code=409)
    response.json.return_value = {'ids': [2], 'existing': {'old.jpg': 1}, 'message': 'duplicate'}
    client.session = Mock()
    client.session.post.return_value = response
    assert client.add_samples(1, [{'filepath': 'old.jpg'}, {'filepath': 'new.jpg'}]) == response.json.return_value
