"""fridge.connect(timeout=...) sets the default timeout on every request the client sends."""

from unittest.mock import patch

import pytest
import requests
from fridge.core import client as client_module
from fridge.core.client import DEFAULT_TIMEOUT


@pytest.fixture(autouse=True)
def restore_global_client():
    saved = client_module._client
    yield
    client_module._client = saved


def sent_timeout(client, **kwargs):
    """The timeout requests.Session.request receives for one call through the client's session."""
    response = requests.Response()
    response.status_code = 200
    with patch.object(requests.Session, "request", return_value=response) as request:
        client.session.get("http://localhost:8000/api/health", **kwargs)
    return request.call_args.kwargs["timeout"]


def test_connect_without_timeout_keeps_default():
    client = client_module.connect("http://localhost:8000/api")
    assert client.session._default_timeout == DEFAULT_TIMEOUT
    assert sent_timeout(client) == DEFAULT_TIMEOUT


@pytest.mark.parametrize("timeout", [(120, 1800), 60, 7.5])
def test_connect_timeout_is_sent_with_requests(timeout):
    client = client_module.connect("http://localhost:8000/api", timeout=timeout)
    assert client_module.get_client() is client
    assert sent_timeout(client) == timeout


def test_explicit_per_call_timeout_still_wins():
    client = client_module.connect("http://localhost:8000/api", timeout=(120, 1800))
    assert sent_timeout(client, timeout=(10, 30)) == (10, 30)


def test_connect_timeout_is_independent_of_api_token():
    client = client_module.connect("http://localhost:8000/api", api_token="t", timeout=(120, 1800))
    assert sent_timeout(client) == (120, 1800)
    assert client.session.headers["Authorization"] == "Bearer t"
