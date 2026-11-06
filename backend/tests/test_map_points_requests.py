"""map_points answers for a zoom, or every point with `points` up to the cap, never every located sample at once:
the GET form and a request with neither are gone in 2.0 (docs/MAP_REQUIREMENTS.md #0, #4)."""

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.main import app
from backend.routers import datasets


def test_a_request_without_a_zoom_or_points_is_refused():
    with pytest.raises(HTTPException) as refused:
        asyncio.run(datasets.get_dataset_map_points_post(1, datasets.MapPointsRequest(filters=None)))
    assert (refused.value.status_code, refused.value.detail) == (
        422,
        'map_points needs the map\'s zoom, or "points": true',
    )


def test_the_get_form_is_gone():
    # Only POST is left at this path, so a GET is "method not allowed" (before any access check or database).
    assert TestClient(app).get('/api/datasets/1/map_points').status_code == 405
