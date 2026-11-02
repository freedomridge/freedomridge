"""With FRIDGE_API_TOKEN set, the demo importers use the token and never ask for a password."""

import sys
from argparse import Namespace

from examples._shared import importer as common


class FakeClient:
    def __init__(self):
        self.logins = []

    def login(self, username, password):
        self.logins.append((username, password))


def args():
    return Namespace(username='admin', password=None)


def test_token_skips_the_password_everywhere(monkeypatch):
    monkeypatch.setenv('FRIDGE_API_TOKEN', 'fixture-token')
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    assert common.password(args()) is None
    for helper in (common.login,):
        client = FakeClient()
        helper(client, args())
        assert client.logins == []


def test_without_a_token_a_password_is_still_required(monkeypatch):
    monkeypatch.delenv('FRIDGE_API_TOKEN', raising=False)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    for call in (
        lambda: common.password(args()),
        lambda: common.login(FakeClient(), args()),
        lambda: common.login(FakeClient(), args()),
    ):
        try:
            call()
        except (RuntimeError, ValueError):
            continue
        raise AssertionError('a missing password must fail without a token')
    client = FakeClient()
    common.login(client, Namespace(username='admin', password='explicit'))
    assert client.logins == [('admin', 'explicit')]
