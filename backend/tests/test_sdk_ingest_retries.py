"""The SDK must surface ingest errors and send inline artifacts in one request."""

import json
from unittest.mock import Mock

import pytest
import requests
from fridge.core.client import Client
from fridge.core.dataset import Dataset
from fridge.core.sample import Sample


def response(status, body, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    result.headers.update(headers or {})
    result.url = 'http://ingest.test/api/datasets/1/samples'
    return result


def dataset():
    result = Dataset.__new__(Dataset)
    result.client = Client(base_url='http://ingest.test/api')
    result._id = 1
    result.name = 'field'
    result._dirty = False
    return result


@pytest.mark.parametrize('status', [400, 401, 403, 404, 409, 422, 500, 503, 507])
@pytest.mark.parametrize('mode', ['error', 'skip', 'update'])
def test_add_raises_every_http_failure(monkeypatch, status, mode):
    ds = dataset()
    send = Mock(return_value=response(status, {'detail': 'failed'}))
    monkeypatch.setattr(requests.Session, 'request', send)
    with pytest.raises(requests.HTTPError) as error:
        ds.add_samples([Sample('old.jpg'), Sample('new.jpg')], on_duplicate=mode)
    assert error.value.response is not None
    assert error.value.response.status_code == status
    assert send.call_count == 1
    assert ds._dirty is False


@pytest.mark.parametrize('ids', [[], [2]])
def test_default_add_returns_duplicate_conflict_and_tracks_new_writes(monkeypatch, ids):
    ds = dataset()
    conflict = {'ids': ids, 'existing': {'old.jpg': 1}, 'message': '1 duplicate filepath(s)'}
    send = Mock(return_value=response(409, conflict))
    monkeypatch.setattr(requests.Session, 'request', send)
    assert ds.add_samples([Sample('old.jpg'), Sample('new.jpg')]) == conflict
    assert send.call_count == 1
    assert send.call_args.kwargs['json']['on_duplicate'] == 'error'
    assert ds._dirty is bool(ids)


@pytest.mark.parametrize('mode', ['skip', 'update'])
def test_duplicate_conflict_raises_outside_error_mode(monkeypatch, mode):
    ds = dataset()
    conflict = {'ids': [2], 'existing': {'old.jpg': 1}, 'message': '1 duplicate filepath(s)'}
    send = Mock(return_value=response(409, conflict))
    monkeypatch.setattr(requests.Session, 'request', send)
    with pytest.raises(requests.HTTPError):
        ds.add_samples([Sample('old.jpg'), Sample('new.jpg')], on_duplicate=mode)
    assert send.call_count == 1
    assert ds._dirty is False


@pytest.mark.parametrize('failure', [requests.ConnectionError, requests.ConnectTimeout, requests.ReadTimeout])
def test_add_propagates_transport_failure_without_hidden_retries(monkeypatch, failure):
    ds = dataset()
    send = Mock(side_effect=failure('API unavailable or response lost'))
    monkeypatch.setattr(requests.Session, 'request', send)
    with pytest.raises(failure):
        ds.add_samples([Sample('field.jpg')], on_duplicate='skip')
    assert send.call_count == 1


def test_add_sends_inline_artifacts_in_one_request(monkeypatch):
    ds = dataset()
    send = Mock(return_value=response(200, {'ids': [1]}))
    monkeypatch.setattr(requests.Session, 'request', send)
    sample = Sample(
        's3://field/1.jpg',
        metadata={'ground_truth': [{'label': 'car'}]},
        tags=['field'],
        embeddings={'clip': [1, 0], 'dino': [1, 0, 0]},
        hashes={'phash': 'abcd'},
        array_fields={'reviewed': ['yes']},
    )
    assert ds.add_samples([sample], on_duplicate='skip') == {'ids': [1]}
    assert send.call_count == 1
    assert send.call_args.kwargs['json'] == {'samples': [sample.to_dict()], 'on_duplicate': 'skip'}


def test_empty_legacy_embedding_reaches_api_validation(monkeypatch):
    ds = dataset()
    send = Mock(return_value=response(422, {'detail': 'embedding must not be empty'}))
    monkeypatch.setattr(requests.Session, 'request', send)
    with pytest.raises(requests.HTTPError):
        ds.add_samples([Sample('field.jpg', embedding=[])], on_duplicate='skip')
    assert send.call_args.kwargs['json']['samples'][0]['embeddings'] == {'clip': []}


@pytest.mark.parametrize('location', [[], [1], [1, 2, 3], '', 'invalid', 0])
def test_invalid_location_is_not_silently_dropped(monkeypatch, location):
    ds = dataset()
    send = Mock(return_value=response(200, {'ids': [1, 2]}))
    monkeypatch.setattr(requests.Session, 'request', send)
    with pytest.raises(ValueError, match=r"bad\.jpg.*location"):
        ds.add_samples([Sample('good.jpg'), Sample('bad.jpg', location=location)], on_duplicate='skip')
    send.assert_not_called()
    assert ds._dirty is False


def test_busy_retry_exhaustion_still_raises(monkeypatch):
    ds = dataset()
    ds.client.session.busy_retry_seconds = 0
    send = Mock(return_value=response(409, {'detail': 'retry'}, {'Retry-After': '1'}))
    monkeypatch.setattr(requests.Session, 'request', send)
    with pytest.raises(requests.HTTPError):
        ds.add_samples([Sample('field.jpg')], on_duplicate='skip')
    assert send.call_count == 1


def test_busy_retry_resends_same_payload_then_returns_success(monkeypatch):
    ds = dataset()
    send = Mock(
        side_effect=[
            response(409, {'detail': 'retry'}, {'Retry-After': '1'}),
            response(200, {'ids': [1]}),
        ],
    )
    monkeypatch.setattr(requests.Session, 'request', send)
    monkeypatch.setattr('fridge.core.client.time.sleep', lambda seconds: None)
    assert ds.add_samples([Sample('field.jpg')], on_duplicate='skip') == {'ids': [1]}
    assert send.call_count == 2
    assert send.call_args_list[0] == send.call_args_list[1]


def test_skipped_replay_does_not_schedule_context_manager_refresh(monkeypatch):
    ds = dataset()
    send = Mock(return_value=response(200, {'ids': [], 'skipped': 1}))
    monkeypatch.setattr(requests.Session, 'request', send)
    ds.refresh_stats = Mock()
    with ds:
        ds.add_samples([Sample('field.jpg')], on_duplicate='skip')
    ds.refresh_stats.assert_not_called()
    assert ds._dirty is False
    # A no-op replay must not clear work from an earlier successful write.
    ds._dirty = True
    ds.add_samples([Sample('field.jpg')], on_duplicate='skip')
    assert ds._dirty is True


@pytest.mark.parametrize('patches', [False, True])
def test_sdk_rejects_legacy_partial_embedding_success(monkeypatch, patches):
    client = Client(base_url='http://ingest.test/api')
    send = Mock(return_value=response(200, {'written': 0, 'submitted': 1, 'skipped': 1}))
    monkeypatch.setattr(requests.Session, 'request', send)
    upload = client.upload_patch_embeddings if patches else client.upload_embeddings
    with pytest.raises(requests.HTTPError, match='persisted'):
        upload(1, [{'sample_id': -1, 'vector': [1, 0]}])
    assert send.call_count == 1
