"""The legacy S3 proxy (/media/s3/<key>: env-var bucket, no per-dataset access check) is gone in 2.0; every image
is served by the access-checked /api/samples/{id}/media."""

from fastapi.testclient import TestClient

from backend.main import app


def test_the_legacy_s3_proxy_is_gone():
    http = TestClient(app)  # no lifespan: an unknown route needs no database
    assert http.get('/media/s3/some/key.jpg').status_code == 404
    assert not [route for route in app.routes if getattr(route, 'path', '').startswith('/media/')]
