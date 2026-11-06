"""Bad input is a 4xx, not a 500.

Each of these reached Postgres (or a tuple unpack) and came back as a 500 with an error_id, which
the SDK reports as a server outage. These lock the validation.
"""

import asyncio
import json
import math

import asyncpg
import pytest
from pydantic import ValidationError
from starlette.requests import Request

from backend import main
from backend.models import ImageSearchRequest, PatchSearchRequest, SearchRequest
from backend.routers.datasets import SampleCreate, SamplesRequest, SampleUpdate, _point_location


@pytest.mark.parametrize(
    "loc",
    [
        {"type": "Point", "coordinates": [1, 2, 3]},  # ValueError: too many values to unpack
        {"coordinates": ["1", "2"]},  # DataError: must be real number, not str
        {"coordinates": [999, 999]},  # stored; map extent off the globe
        {"coordinates": [10, -95]},  # latitude out of range
        {"lat": 1, "lon": 2},  # no coordinates: dropped on ingest, (0, 0) on PATCH
        {"coordinates": [float("nan"), 1]},
        {"coordinates": [True, 1]},
    ],
)
def test_bad_locations_are_refused(loc):
    with pytest.raises(ValueError):
        _point_location(loc)
    with pytest.raises(ValidationError) as exc:
        SampleCreate(filepath="x.jpg", location=loc)
    # The 422 detail must be JSON: a ValueError object in `ctx` turned it into a 500.
    json.dumps(exc.value.errors(include_url=False, include_context=False))
    with pytest.raises(ValidationError):
        SampleUpdate(location=loc)


def test_good_locations_are_normalised():
    assert _point_location({"coordinates": [-73.99, 40.75]}) == {"type": "Point", "coordinates": [-73.99, 40.75]}
    assert _point_location({"type": "Point", "coordinates": [180, -90]}) == {
        "type": "Point",
        "coordinates": [180.0, -90.0],
    }
    assert _point_location(None) is None
    assert SampleCreate(filepath="x.jpg").location is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_embeddings_are_refused(bad):
    with pytest.raises(ValidationError):
        SampleCreate(filepath="x.jpg", embeddings={"clip": [0.1, bad]})
    assert SampleCreate(filepath="x.jpg", embeddings={"clip": [0.1, 0.2]}).embeddings == {"clip": [0.1, 0.2]}


def test_negative_limits_and_offsets_are_refused():
    for model in (SearchRequest, ImageSearchRequest):
        with pytest.raises(ValidationError):
            model(dataset_id=1, limit=-1)
    with pytest.raises(ValidationError):
        PatchSearchRequest(dataset_id=1, sample_id=1, patch_scale="2x2", patch_row=0, patch_col=0, limit=-1)
    with pytest.raises(ValidationError):
        SamplesRequest(offset=-5)
    assert SamplesRequest(limit=0).limit == 0  # count-only stays valid


def test_postgres_data_errors_are_a_400_without_row_data():
    request = Request({"type": "http", "method": "GET", "path": "/api/datasets/99999999999999", "headers": []})
    exc = asyncpg.DataError("invalid input for query argument $1: 99999999999999 (value out of int32 range)\nmore")
    resp = asyncio.run(main.data_error_handler(request, exc))
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["detail"].startswith("Invalid input: invalid input for query argument $1")
    assert "more" not in body["detail"]


def test_the_single_embedding_field_is_refused_with_the_fix():
    """Removed in 2.0: it was stored as 'clip' whatever model made it. Refused, so an old client's vectors are
    not silently dropped."""
    with pytest.raises(ValidationError) as refused:
        SampleCreate(filepath="x.jpg", embedding=[0.1, 0.2])
    assert "'embedding' was removed in 2.0: send 'embeddings': {field name: vector}" in str(refused.value)
    assert SampleCreate(filepath="x.jpg").embedding is None
