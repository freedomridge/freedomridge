"""All example tests are offline except their ephemeral localhost HTTP servers."""

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))


@pytest.fixture(autouse=True)
def local_network_only(monkeypatch):
    connect = socket.socket.connect

    def guarded(sock, address):
        if isinstance(address, tuple):
            host, port = address[:2]
            if host not in ('127.0.0.1', '::1', 'localhost') or port in (8000, 3000, 5432, 8084):
                raise AssertionError(f'Example test attempted a non-fixture connection: {host}:{port}')
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
