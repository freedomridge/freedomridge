"""An SDK and a server of different major versions refuse each other: every answer carries X-Fridge-Version and
the SDK compares it with its installed version (python/fridge/core/client.py); every SDK request carries
X-Fridge-SDK-Version and the server refuses another major version with 426 (backend/main.py)."""

import pytest
import requests
from fastapi.testclient import TestClient
from fridge.core import client as client_module

from backend.main import app
from backend.version import VERSION


def answer(version=None):
    resp = requests.Response()
    resp.status_code = 200
    resp.url = 'https://fridge.example/api/datasets'
    if version is not None:
        resp.headers['X-Fridge-Version'] = version
    return resp


def session_answering(monkeypatch, resp, sdk='2.0.0'):
    monkeypatch.setattr(requests.Session, 'request', lambda self, *args, **kwargs: resp)
    monkeypatch.setattr(client_module, '_SDK_VERSION', sdk)
    return client_module._Session()


def test_every_answer_says_which_release_made_it():
    http = TestClient(app)  # no lifespan: these answers need no database
    for path, status in (('/api/datasets/1/jobs/active', 401), ('/api/does-not-exist', 404)):
        answered = http.get(path)
        assert (answered.status_code, answered.headers['x-fridge-version']) == (status, VERSION)


@pytest.mark.parametrize('server', ['2.0.0', '2.7.1'])
def test_a_server_of_the_same_major_version_is_used(monkeypatch, server):
    session = session_answering(monkeypatch, answer(server))
    assert session.request('GET', 'https://fridge.example/api/datasets').status_code == 200


@pytest.mark.parametrize('server', ['1.4.0', '3.0.0'])
def test_a_server_of_another_major_version_is_refused_with_what_to_install(monkeypatch, server):
    session = session_answering(monkeypatch, answer(server))
    with pytest.raises(client_module.IncompatibleServerError) as refused:
        session.request('GET', 'https://fridge.example/api/datasets')
    assert str(refused.value) == (
        f'This Freedom Ridge SDK is 2.0.0, and the server at https://fridge.example runs {server}. '
        f'An SDK works only with a server of its own major version: pip install "freedomridge=={server}"'
    )


def test_nothing_to_compare_lets_the_answer_through(monkeypatch):
    # A server before 2.0 sends no header; an SDK run from a source tree has no installed version.
    assert session_answering(monkeypatch, answer()).request('GET', 'https://fridge.example/api/x').ok
    assert session_answering(monkeypatch, answer('9.0.0'), sdk=None).request('GET', 'https://fridge.example/api/x').ok


def test_the_sdk_says_its_version_and_the_server_refuses_another_major_one(monkeypatch):
    monkeypatch.setattr(client_module, '_SDK_VERSION', '2.0.0')
    assert client_module._Session().headers['X-Fridge-SDK-Version'] == '2.0.0'

    http = TestClient(app)
    major = int(VERSION.split('.')[0])
    for sdk in (f'{major}.0.0', f'{major}.9.3', None):  # the same major, or an SDK before 2.0 (no header)
        headers = {'X-Fridge-SDK-Version': sdk} if sdk else {}
        assert http.get('/api/does-not-exist', headers=headers).status_code == 404
    other = f'{major + 1}.0.0'
    refused = http.get('/api/does-not-exist', headers={'X-Fridge-SDK-Version': other})
    assert (refused.status_code, refused.headers['x-fridge-version']) == (426, VERSION)
    assert refused.json()['detail'] == (
        f'This server runs Freedom Ridge {VERSION}, and this SDK is {other}. An SDK works only with a server of '
        f'its own major version: pip install "freedomridge=={VERSION}"'
    )
