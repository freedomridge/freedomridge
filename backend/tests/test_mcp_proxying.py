"""/mcp is routed to the backend by the shipped web server, unbuffered, and by the frontend's own server."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_nginx_sends_mcp_to_the_backend_without_buffering():
    config = (ROOT / 'nginx.conf').read_text()
    location = re.search(r'location = /mcp \{(.*?)\}', config, re.S)
    assert location is not None, 'nginx.conf has no location for /mcp: it would reach the frontend'
    body = location.group(1)
    assert 'proxy_pass http://$backend_upstream;' in body
    # A slow call is answered as a stream with keepalives; a buffer would hold them back.
    assert 'proxy_buffering off;' in body
    assert 'limit_req zone=api_limit' in body


def test_the_frontend_server_passes_mcp_to_the_backend():
    # The token page names this page's origin + /mcp; without nginx in front (development, `next start`) the
    # frontend's server must pass it on.
    config = (ROOT / 'frontend/next.config.ts').read_text()
    assert re.search(r"source: '/mcp',\s*destination: `\$\{backend\}/mcp`", config)
