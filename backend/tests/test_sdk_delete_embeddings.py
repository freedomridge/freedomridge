"""The SDK sends delete_embeddings targets as the server's bulk target, in bounded requests."""

import uuid

import pytest
import requests
from fridge.core import client as sdk_client
from fridge.core.view import DatasetView

from backend.tests.test_sdk_ingest_retries import dataset, response

URL = 'http://ingest.test/api/datasets/1/embeddings/emb/delete'


@pytest.fixture
def server(monkeypatch):
    """Answers every request with a job; waiting on it reports as many deletions as ids it was sent."""
    sent = []

    def request(_session, method, url, **kwargs):
        sent.append((url, kwargs['json']))
        return response(202, {'job_id': str(uuid.uuid4()), 'state': 'queued'})

    class FakeJob:
        def __init__(self, _client, job_id):
            self.id = job_id

        def wait(self, timeout):
            return {'deleted': len(sent[-1][1]['sample_ids'])}

    monkeypatch.setattr(requests.Session, 'request', request)
    monkeypatch.setattr(sdk_client, 'Job', FakeJob)
    return sent


def test_ids_are_sent_in_chunks_and_the_counts_added(server, monkeypatch):
    monkeypatch.setattr(sdk_client, 'DELETE_EMBEDDINGS_IDS_PER_REQUEST', 3)
    assert dataset().delete_embeddings('emb', list(range(8))) == {'deleted': 8}
    assert [(url, body['sample_ids'], body['filters']) for url, body in server] == [
        (URL, [0, 1, 2], None),
        (URL, [3, 4, 5], None),
        (URL, [6, 7], None),
    ]


def test_one_request_returns_the_job_result_as_is(server):
    assert dataset().delete_embeddings('emb', [4, 5]) == {'deleted': 2}
    assert len(server) == 1


def test_the_field_delete_sends_an_empty_filter(server):
    dataset().delete_embedding_field('emb')
    assert server[0][1]['filters'] == {}
    assert server[0][1]['sample_ids'] == []


def test_no_target_raises_without_sending_a_request(server):
    with pytest.raises(TypeError, match='delete_embedding_field'):
        dataset().delete_embeddings('emb', None)
    with pytest.raises(ValueError, match='filters'):
        dataset().client.delete_embeddings(1, 'emb')
    assert server == []


def test_a_view_sends_its_filter(server):
    ds = dataset()
    view = DatasetView(ds)
    view._filters = {'field': 'label', 'operator': '=', 'value': 'bad'}
    view.delete_embeddings('emb')
    assert server[0][1]['filters'] == {'field': 'label', 'operator': '=', 'value': 'bad'}
    assert server[0][1]['sample_ids'] == []


def test_wait_false_returns_the_job_for_one_request_and_refuses_several(server, monkeypatch):
    job = dataset().delete_embeddings('emb', [1], wait=False)
    assert isinstance(job, sdk_client.Job)
    monkeypatch.setattr(sdk_client, 'DELETE_EMBEDDINGS_IDS_PER_REQUEST', 2)
    with pytest.raises(ValueError, match='wait=False'):
        dataset().delete_embeddings('emb', [1, 2, 3], wait=False)
    assert len(server) == 1


def test_an_empty_id_list_deletes_nothing_and_sends_nothing(server):
    assert dataset().delete_embeddings('emb', []) == {'deleted': 0}
    assert server == []


def test_a_view_without_a_filter_is_refused_not_sent_as_the_whole_field(server):
    ds = dataset()
    with pytest.raises(ValueError, match='delete_embedding_field'):
        DatasetView(ds).delete_embeddings('emb')
    with pytest.raises(ValueError, match='delete_embedding_field'):
        ds.delete_embeddings('emb', DatasetView(ds))
    assert server == []


def test_a_view_of_another_dataset_is_refused(server):
    other = dataset()
    other._id = 2
    view = DatasetView(other)
    view._filters = {'field': 'label', 'operator': '=', 'value': 'bad'}
    with pytest.raises(ValueError, match='belongs to dataset 2'):
        dataset().delete_embeddings('emb', view)
    assert server == []


def test_an_id_list_view_sends_only_its_ids(server):
    ds = dataset()
    view = DatasetView(ds)
    view._filters = {'field': 'id', 'operator': 'in', 'value': [3, 4]}
    assert view.delete_embeddings('emb') == {'deleted': 2}
    assert server[0][1]['sample_ids'] == [3, 4] and server[0][1]['filters'] is None


def test_ids_and_a_filter_together_are_refused(server):
    with pytest.raises(ValueError, match='not both'):
        dataset().client.delete_embeddings(1, 'emb', [1, 2], filters={'field': 'label', 'operator': '=', 'value': 'x'})
    assert server == []
