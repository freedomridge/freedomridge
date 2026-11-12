"""The SDK says when a filter it sent holds a lasso drawn on an earlier layout of its map (the answer's
`stale_lassos`): a StaleLassoWarning naming the map, once per call. A bulk delete by such a filter is refused by the
server, and the SDK raises its usual HTTPError with the server's message."""

import warnings
from unittest.mock import Mock

import fridge
import pytest
import requests
from fridge.core.client import Client, StaleLassoWarning
from fridge.core.dataset import Dataset
from fridge.core.job import Job
from fridge.core.view import DatasetView


def client_answering(status, body):
    client = Client.__new__(Client)
    client.base_url = 'http://test.invalid/api'
    response = Mock(ok=200 <= status < 300, status_code=status, reason='Conflict' if status == 409 else 'OK')
    response.json.return_value = body
    client.session = Mock()
    client.session.post.return_value = response
    return client


def test_a_query_whose_filter_holds_a_stale_lasso_warns_once_naming_the_map():
    lasso = {'field': 'clip', 'operator': 'embedding_polygon', 'value': [[0, 0], [1, 0], [1, 1]]}
    client = client_answering(200, {'items': [], 'total': 0, 'stale_lassos': ['clip']})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        assert client.query_samples(7, filters=lasso)['total'] == 0
    assert [type(w.message) for w in caught] == [StaleLassoWarning]
    assert 'earlier layout of the clip map' in str(caught[0].message)
    assert caught[0].filename == __file__  # points at the caller's line
    quiet = client_answering(200, {'items': [], 'total': 0})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        quiet.query_samples(7, filters=lasso)
    assert caught == []
    assert fridge.StaleLassoWarning is StaleLassoWarning


def test_a_tag_by_a_stale_lasso_warns_and_goes_ahead():
    client = client_answering(
        202,
        {'job_id': '00000000-0000-0000-0000-000000000001', 'state': 'queued', 'stale_lassos': ['clip', 'dino']},
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        job = client.bulk_tag_samples(7, add_tags=['near'], filters={'logic': 'AND', 'filters': []}, wait=False)
    assert isinstance(job, Job)
    assert len(caught) == 1 and 'the clip and dino maps' in str(caught[0].message)


def test_a_refused_delete_raises_the_server_s_message():
    detail = 'The filter holds a lasso drawn on an earlier layout of the clip map: it may select different samples now'
    client = client_answering(409, {'detail': detail})
    with pytest.raises(requests.HTTPError, match='earlier layout of the clip map'):
        client.bulk_delete_samples(7, filters={'field': 'clip', 'operator': 'embedding_polygon', 'value': []})


def test_a_view_s_count_and_pages_warn_too():
    """A view (a saved one loaded with load_view, say) counts and pages through POST /samples/query itself."""
    dataset = Dataset.__new__(Dataset)
    dataset._id = 7
    dataset.client = client_answering(200, {'items': [], 'total': 3, 'stale_lassos': ['clip']})
    view = DatasetView(dataset, {'field': 'clip', 'operator': 'embedding_polygon', 'value': [[0, 0], [1, 0], [1, 1]]})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        assert view.count() == 3
        assert list(view) == []
    assert [type(w.message) for w in caught] == [StaleLassoWarning, StaleLassoWarning]
