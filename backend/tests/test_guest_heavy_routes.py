"""The guest limiter must recognise every heavy route as the router actually resolves it."""

import re

import pytest

from backend.request_limits import GUEST_HEAVY_ENDPOINTS

# Every route whose handler is marked @guest_heavy (backend/request_limits.py says what belongs here).
HEAVY_ROUTES = {
    ('GET', '/api/datasets/{id}/samples'),  # also the paginated CSV export path
    ('POST', '/api/datasets/{id}/samples/query'),
    ('POST', '/api/datasets/{id}/samples/lookup'),
    ('POST', '/api/datasets/{id}/samples/tag-counts'),
    ('GET', '/api/datasets/{id}/field-stats'),
    ('POST', '/api/datasets/{id}/field-stats'),
    ('GET', '/api/datasets/{id}/schema'),
    ('GET', '/api/datasets/{id}/stats'),
    ('GET', '/api/datasets/{id}/coverage'),
    ('GET', '/api/datasets/{id}/missing'),
    ('GET', '/api/datasets/{id}/fields/{field_name}/autocomplete'),
    ('POST', '/api/datasets/{id}/fields/{field_name}/autocomplete'),
    ('GET', '/api/datasets/{id}/histogram'),
    ('GET', '/api/datasets/{id}/histogram/preview'),
    ('POST', '/api/datasets/{id}/aggregate'),
    ('POST', '/api/datasets/{id}/versions/{version_id}/aggregate'),
    ('POST', '/api/datasets/{id}/versions/{version_id}/samples/query'),
    ('GET', '/api/datasets/{id}/map_points'),
    ('POST', '/api/datasets/{id}/map_points'),
    ('POST', '/api/datasets/{id}/map_extent'),
    ('POST', '/api/datasets/{id}/selection_token'),
    ('GET', '/api/datasets/{id}/embeddings'),
    ('POST', '/api/datasets/{id}/embeddings/query'),
    ('POST', '/api/datasets/{id}/embeddings/select-region'),
    ('POST', '/api/datasets/{id}/embeddings/lookup'),
    ('POST', '/api/datasets/{id}/embeddings/lookup-patches'),
    ('GET', '/api/datasets/{id}/embeddings/vectors'),  # paginated vector export
    ('GET', '/api/datasets/{id}/hashes/export'),
    ('GET', '/api/datasets/{id}/hashes/stats'),
    ('POST', '/api/datasets/{id}/hashes/stats'),
    ('POST', '/api/datasets/{id}/hashes/duplicates'),
    ('POST', '/api/search'),
    ('POST', '/api/search/by-image'),
    ('POST', '/api/search/patches'),
    ('POST', '/api/search/object-to-patches'),
    ('POST', '/api/datasets/{dataset_id}/sparse-sample'),
}


async def resolved_endpoint(app, method, template):
    """Route a concrete request and return scope['endpoint'], as set_request_user reads it."""
    path = re.sub(r'\{[^}]+\}', '1', template)
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': '1.1',
        'method': method,
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode(),
        'root_path': '',
        'query_string': b'',
        'headers': [(b'host', b'test')],
        'client': ('127.0.0.1', 1),
        'server': ('test', 80),
        'app': app,
        'state': {},
    }

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        pass

    try:
        await app.router(scope, receive, send)
    except Exception:
        pass  # unauthenticated, no database: only the routing result matters
    return scope.get('endpoint')


@pytest.mark.asyncio
async def test_heavy_routes_resolve_to_marked_handlers():
    # The limiter matches the handler, not the route template: FastAPI 0.137+ reports included
    # routes without their /api prefix, which silently turned a template allowlist off.
    from backend.main import app

    resolved = {route: await resolved_endpoint(app, *route) for route in HEAVY_ROUTES}
    assert sorted(route for route, endpoint in resolved.items() if endpoint not in GUEST_HEAVY_ENDPOINTS) == []
    # No other router handler is marked: a new heavy route is listed here too. (Tests mark
    # their own stand-in handlers.)
    marked = {endpoint for endpoint in GUEST_HEAVY_ENDPOINTS if endpoint.__module__.startswith('backend.routers.')}
    assert marked == set(resolved.values())
