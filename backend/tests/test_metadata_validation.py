"""Malformed numeric metadata must return a useful 422, never a JSON-encoding 500."""

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from backend.main import validation_error_handler
from backend.routers.datasets import BulkSetRequest, SampleUpdate


@pytest.mark.parametrize('number', ['NaN', 'Infinity', '-Infinity', '1e400'])
@pytest.mark.parametrize('model', [SampleUpdate, BulkSetRequest])
def test_nonfinite_metadata_returns_serializable_validation_error(number, model):
    app = FastAPI()
    app.exception_handler(RequestValidationError)(validation_error_handler)

    async def write(body):
        return body

    write.__annotations__['body'] = model
    app.post('/metadata')(write)
    response = TestClient(app).post(
        '/metadata',
        content='{"metadata":{"nested":[{"score":' + number + '}]}}',
        headers={'Content-Type': 'application/json'},
    )
    assert response.status_code == 422
    assert 'nested[0].score' in response.json()['detail'][0]['msg']
