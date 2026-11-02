"""The SDK retries busy/rate-limited refusals (409/429 + Retry-After), and nothing else."""

import io

import pytest
import requests
from fridge.core import client as client_module


def response(status, headers=None):
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    return resp


def session_returning(monkeypatch, *responses):
    queue = list(responses)
    calls: list = []
    sleeps: list = []

    def fake_request(self, *args, **kwargs):
        calls.append(kwargs)
        return queue.pop(0)

    monkeypatch.setattr(requests.Session, 'request', fake_request)
    monkeypatch.setattr(client_module.time, 'sleep', sleeps.append)
    monkeypatch.setattr(client_module.random, 'uniform', lambda low, high: low)  # no jitter: exact sleeps
    return client_module._Session(), calls, sleeps


BUSY = {'Retry-After': '1'}


@pytest.mark.parametrize('status', [409, 429])
def test_refusal_is_retried_until_it_goes_through(monkeypatch, status):
    session, calls, sleeps = session_returning(
        monkeypatch,
        response(status, BUSY),
        response(status, BUSY),
        response(200),
    )
    assert session.request('POST', 'http://api/datasets/1/samples', json={'samples': []}).status_code == 200
    assert len(calls) == 3 and sleeps == [1.0, 2.0]
    assert calls[0] == calls[1] == calls[2]


@pytest.mark.parametrize('status', [409, 429])
def test_a_refusal_without_retry_after_is_returned_at_once(monkeypatch, status):
    session, calls, sleeps = session_returning(monkeypatch, response(status), response(200))
    assert session.request('POST', 'http://api/datasets/1/samples', json={}).status_code == status
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize('status', [409, 429])
@pytest.mark.parametrize('upload', ['files', 'data'])
def test_a_streamed_upload_is_never_resent(monkeypatch, status, upload):
    session, calls, sleeps = session_returning(monkeypatch, response(status, BUSY), response(200))
    body = {'f': b'x'} if upload == 'files' else io.BytesIO(b'x')
    assert session.request('POST', 'http://api/upload', **{upload: body}).status_code == status
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize('status', [409, 429])
def test_retrying_stops_at_the_budget(monkeypatch, status):
    session, calls, sleeps = session_returning(monkeypatch, *[response(status, BUSY)] * 50)
    monkeypatch.setattr(session, 'busy_retry_seconds', 10)
    clock = [0.0]
    monkeypatch.setattr(client_module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(
        client_module.time,
        'sleep',
        lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds)),
    )
    assert session.request('DELETE', 'http://api/datasets/1/samples/2').status_code == status
    assert sum(sleeps) <= 10 and sleeps == [1.0, 2.0, 4.0]
    assert len(calls) == 4


@pytest.mark.parametrize('status', [409, 429])
def test_backoff_is_jittered(monkeypatch, status):
    session, _, sleeps = session_returning(monkeypatch, response(status, BUSY), response(200))
    monkeypatch.setattr(client_module.random, 'uniform', lambda low, high: high)
    session.request('DELETE', 'http://api/datasets/1/samples/2')
    assert sleeps == [1.5]


@pytest.mark.parametrize('status', [400, 401, 403, 404, 413, 422, 500, 502, 503, 504, 507])
def test_other_statuses_are_not_retried_even_with_retry_after(monkeypatch, status):
    session, calls, sleeps = session_returning(monkeypatch, response(status, BUSY), response(200))
    assert session.request('POST', 'http://api/datasets/1/samples', json={}).status_code == status
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize('status', [409, 429])
def test_retry_can_be_disabled(monkeypatch, status):
    session, calls, sleeps = session_returning(monkeypatch, response(status, BUSY), response(200))
    assert session.request('GET', 'http://api/jobs/1', retry_busy=False).status_code == status
    assert len(calls) == 1 and not sleeps
    assert 'retry_busy' not in calls[0]


def test_nginx_retry_after_controls_the_pause(monkeypatch):
    session, calls, sleeps = session_returning(monkeypatch, response(429, {'Retry-After': '10'}), response(200))
    assert session.request('POST', 'http://api/datasets/1/samples', json={}).status_code == 200
    assert len(calls) == 2 and sleeps == [10.0]
