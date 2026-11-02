"""Externally visible job contracts also runnable against the pre-jobs application."""

import fridge
import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.mark.parametrize(
    'method,path',
    [
        ('GET', '/api/jobs/813b5d30-e738-4d1d-9ace-0acf1e6466fd'),
        ('GET', '/api/datasets/1/jobs'),
        ('POST', '/api/jobs/813b5d30-e738-4d1d-9ace-0acf1e6466fd/cancel'),
    ],
)
def test_job_endpoints_exist_and_require_authentication(method, path):
    response = TestClient(app).request(method, path)
    assert response.status_code == 401


def test_sdk_exports_reconnectable_job_handles_and_explicit_outcome_errors():
    for name in ('Job', 'JobFailed', 'JobCancelled', 'JobTimeout'):
        assert hasattr(fridge, name), f'The SDK does not expose {name}'
