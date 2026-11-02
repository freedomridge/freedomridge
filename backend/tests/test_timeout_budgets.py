"""The three timeout layers must not drift apart.

A request to a long analytical endpoint passes through three independent budgets:

    SDK read timeout (python/fridge/core/client.py)
        -> nginx proxy_read_timeout (nginx.conf)
            -> server-side asyncpg `timeout=` / pool command_timeout

nginx used to cap ALL of /api/ at 600s while the SDK and backend had agreed on 1800s, 3600s and
7200s for refresh-stats, quality/ann-index and embeddings/reduce. Behind the shipped nginx those
504'd at 600s — and because uvicorn does not cancel the ASGI task when the upstream disconnects, the
work COMPLETED and committed while the caller got a garbled HTML 504 and could not tell success from
failure. That was a verbatim repeat of the
sync-tail-timeout bug the 1800s SDK timeout was added to fix in the first place.

Nothing connects those three files, so nothing stops them drifting again — except this. The rule:
**nginx must cover every SDK call that can legitimately run longer than the default.**
"""

import ast
import os
import re

import pytest

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
NGINX = os.path.join(REPO, "nginx.conf")
CLIENT = os.path.join(REPO, "python", "fridge", "core", "client.py")

# The pool's command_timeout (backend/database.py) — the longest an ordinary request's statements
# can run, and therefore what the generic /api/ location has to cover.
DEFAULT_BUDGET_S = 600


def _nginx_locations() -> list[tuple[str, int]]:
    """[(location regex, proxy_read_timeout seconds)] for every `location ~ ...` block."""
    with open(NGINX) as fh:
        text = fh.read()
    out = []
    for m in re.finditer(r"location\s+~\s+(\S+)\s*\{", text):
        # Scan forward to this block's proxy_read_timeout (blocks here are flat, no nesting).
        tail = text[m.end() : m.end() + 4000]
        end = tail.find("\n        }")
        body = tail if end == -1 else tail[:end]
        t = re.search(r"proxy_read_timeout\s+(\d+)s", body)
        if t:
            out.append((m.group(1), int(t.group(1))))
    return out


def _generic_api_timeout() -> int:
    with open(NGINX) as fh:
        text = fh.read()
    m = re.search(r"location\s+/api/\s*\{(.*?)\n        \}", text, re.S)
    assert m, "could not find the generic `location /api/` block in nginx.conf"
    t = re.search(r"proxy_read_timeout\s+(\d+)s", m.group(1))
    assert t, "the generic /api/ block has no proxy_read_timeout"
    return int(t.group(1))


def _url_from_fstring(node: ast.AST) -> str | None:
    """Rebuild a request path from `f"{self.base_url}/datasets/{dataset_id}/refresh-stats"`.

    base_url already ends in /api, so everything after the first interpolation is the path.
    Remaining interpolations are ids -> a concrete number so the path can be regex-matched.
    """
    if not isinstance(node, ast.JoinedStr):
        return None
    parts, seen_base = [], False
    for v in node.values:
        if isinstance(v, ast.FormattedValue):
            if not seen_base:
                seen_base = True  # this is base_url itself
            else:
                parts.append("123")
        elif isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
    return "/api" + "".join(parts) if seen_base else None


def _sdk_calls() -> list[tuple[str, int, int]]:
    """[(path, read_timeout_s, lineno)] for every SDK request that passes an explicit timeout."""
    with open(CLIENT) as fh:
        tree = ast.parse(fh.read(), filename=CLIENT)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kw = next((k for k in node.keywords if k.arg == "timeout"), None)
        if kw is None:
            continue
        # requests accepts `timeout=N` or `timeout=(connect, read)`; the read half is what matters.
        val = kw.value
        if isinstance(val, ast.Tuple) and len(val.elts) == 2:
            val = val.elts[1]
        if not (isinstance(val, ast.Constant) and isinstance(val.value, (int, float))):
            continue
        read = int(val.value)
        path = _url_from_fstring(node.args[0]) if node.args else None
        if path:
            calls.append((path, read, node.lineno))
    return calls


def test_long_route_covers_every_slow_sdk_call():
    """Any SDK call allowed to run past the default must match a long-timeout nginx location."""
    slow = [(p, t, ln) for p, t, ln in _sdk_calls() if t > DEFAULT_BUDGET_S]
    assert slow, "expected some >600s SDK calls (refresh-stats, ann-index, reduce) — parser broken?"

    long_locs = [(rx, t) for rx, t in _nginx_locations() if t > DEFAULT_BUDGET_S]
    assert long_locs, "nginx.conf has no location with a proxy_read_timeout above the default"

    uncovered = []
    for path, timeout, lineno in slow:
        if not any(re.search(rx, path) for rx, _ in long_locs):
            uncovered.append(f"client.py:{lineno} {path} (read timeout {timeout}s)")
    assert not uncovered, (
        "These SDK calls allow more than "
        f"{DEFAULT_BUDGET_S}s but nginx would cut them off at the generic /api/ timeout — the work "
        "still completes server-side, so the caller gets an HTML 504 it cannot distinguish from a "
        "real failure. Extend the long-analytical location regex in nginx.conf:\n  " + "\n  ".join(uncovered)
    )


def test_long_route_timeout_is_at_least_the_slowest_sdk_call():
    slow = [t for _, t, _ in _sdk_calls() if t > DEFAULT_BUDGET_S]
    worst = max(slow)
    best_nginx = max(t for _, t in _nginx_locations())
    assert best_nginx >= worst, (
        f"nginx's longest proxy_read_timeout is {best_nginx}s but the SDK waits up to {worst}s. "
        "nginx would 504 first, while the server-side work keeps running and commits."
    )


def test_generic_api_still_covers_the_pool_command_timeout():
    """The default must not drop below what an ordinary request's statements can take."""
    generic = _generic_api_timeout()
    assert generic >= DEFAULT_BUDGET_S, (
        f"generic /api/ proxy_read_timeout is {generic}s but the asyncpg pool allows "
        f"{DEFAULT_BUDGET_S}s per statement (backend/database.py)"
    )


def test_ingest_route_is_deliberately_not_in_the_long_group():
    """`POST /datasets/{id}/samples` stays at its own (shorter) budget — on purpose.

    Raising it alone would only let an oversized unchunked import hang longer; the real fix is SDK
    chunking (backlog I3 / finding B38). Production ingest streams one sample per request, so the
    shorter cap is not biting. This test exists so the decision is explicit rather than accidental.
    """
    locs = dict(_nginx_locations())
    ingest = [rx for rx in locs if "samples$" in rx]
    assert ingest, "the ingest location regex disappeared from nginx.conf"
    assert locs[ingest[0]] <= DEFAULT_BUDGET_S, (
        "the ingest route was given a long timeout without the SDK-side chunking fix (I3/B38); "
        "if that is intentional, update this test and the note in nginx.conf"
    )


@pytest.mark.parametrize(
    'filename, method, route',
    [
        ('client.py', 'lookup_samples', 'lookup'),
        ('client.py', 'bulk_set_metadata', 'bulk-set'),
        ('client.py', 'query_samples', 'query'),
        ('view.py', '_execute_query', 'query'),
        ('view.py', '_count', 'query'),
    ],
)
@pytest.mark.parametrize('trailing_slash', ['', '/'])
def test_bulk_json_locations_cover_sdk_post_urls(filename, method, route, trailing_slash):
    """Count/paging views also POST directly; all five writers need the right body/rate budget."""
    with open(os.path.join(os.path.dirname(CLIENT), filename)) as fh:
        tree = ast.parse(fh.read())
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == method)
    paths = [
        _url_from_fstring(node.args[0])
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'post'
        and node.args
    ]
    assert paths == [f'/api/datasets/123/samples/{route}']
    path = paths[0]
    assert path is not None
    path += trailing_slash

    with open(NGINX) as fh:
        text = fh.read()
    # nginx takes the first matching regex; catch a preceding location stealing this request too.
    location = next(
        match
        for match in re.finditer(r'location\s+~\s+(\S+)\s*\{(.*?)\n        \}', text, re.S)
        if re.search(match.group(1), path)
    )
    assert location.start() < text.index('location /api/')
    body = location.group(2)
    assert 'proxy_pass http://$backend_upstream;' in body
    if route == 'query':
        assert location.group(1) == r'^/api/datasets/[0-9]+/samples/query/?$'
        assert 'client_max_body_size 10m;' in body
        assert 'limit_req zone=api_limit burst=150 nodelay;' in body
        assert 'proxy_read_timeout 600s;' in body
        assert 'upload_limit' not in body
    else:
        assert location.group(1) == r'^/api/datasets/[0-9]+/samples/(lookup|bulk-set)/?$'
        assert 'client_max_body_size 100m;' in body
        assert 'limit_req zone=upload_limit burst=20 nodelay;' in body
        assert 'proxy_send_timeout 300s;' in body
        assert 'proxy_read_timeout 300s;' in body


@pytest.mark.parametrize(
    'path',
    [
        '/api/datasets/123/samples',
        '/api/datasets/123/samples/1',
        '/api/datasets/123/samples/bulk-tag',
        '/api/datasets/123/samples/bulk-delete',
        '/api/datasets/123/samples/lookup/extra',
        '/api/datasets/123/samples/bulk-set/extra',
        '/api/datasets/123/samples/query/extra',
        '/api/datasets/not-a-number/samples/lookup',
        '/api/datasets/not-a-number/samples/query',
    ],
)
def test_bulk_json_locations_do_not_widen_other_routes(path):
    locations = [rx for rx, _ in _nginx_locations() if '(lookup|bulk-set)' in rx or '/samples/query' in rx]
    assert len(locations) == 2
    assert not any(re.search(rx, path) for rx in locations)


def test_default_body_limit_remains_small():
    with open(NGINX) as fh:
        text = fh.read()
    assert 'client_max_body_size 1m;' in text[: text.index('server {')]
    generic = re.search(r'location /api/\s*\{(.*?)\n        \}', text, re.S)
    assert generic and 'client_max_body_size' not in generic.group(1)
