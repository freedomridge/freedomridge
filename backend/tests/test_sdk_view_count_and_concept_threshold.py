"""Two SDK calls that silently did something other than what the caller asked.

- ``len(view)`` / ``view.count()`` ignored ``limit()`` and ``skip()``: they sent only the filters,
  so ``dataset.all().limit(3).count()`` reported the whole dataset.
- ``Dataset.create_concept`` always sent ``threshold=0.5``, overriding the server's
  range-derived default (0.5 matches almost every sample of a text concept).

Stubbed and self-contained (no DB, no TestClient), matching the rest of the suite.
"""

import json

import pytest
import requests
from fridge.core.client import Client
from fridge.core.dataset import Dataset
from fridge.core.sample import Sample
from fridge.core.view import DatasetView, SimilarityResultView

from backend.filter_query import TEXT, parse_query


def _response(body, status=200):
    response = requests.models.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.headers['Content-Type'] = 'application/json'
    return response


class _Recorder:
    """A session that answers every POST with a fixed body and records the JSON sent."""

    def __init__(self, body):
        self.body = body
        self.sent = []

    def post(self, url, json=None, **_kwargs):
        self.sent.append(json)
        return _response(self.body)


class _Ds:
    _id = 1

    def __init__(self, session):
        self.client = type("C", (), {"base_url": "http://t/api", "session": session})()


@pytest.mark.parametrize(
    "build,expected",
    [
        (lambda v: v, 10),
        (lambda v: v.limit(3), 3),
        (lambda v: v.limit(20), 10),
        (lambda v: v.skip(8), 2),
        (lambda v: v.skip(2).limit(3), 3),
        (lambda v: v.skip(9).limit(3), 1),
        (lambda v: v.skip(12), 0),
    ],
)
def test_view_count_applies_skip_and_limit(build, expected):
    session = _Recorder({"items": [], "total": 10})
    view = build(DatasetView(_Ds(session)))
    assert view.count() == expected
    assert len(view) == expected
    # Still a count query: no rows are fetched, whatever the view's window.
    assert all(sent["limit"] == 0 and sent["offset"] == 0 for sent in session.sent)


def test_view_count_keeps_the_filters():
    session = _Recorder({"items": [], "total": 4})
    view = DatasetView(_Ds(session))
    view._filters = {"field": "scene", "operator": "=", "value": "highway"}
    assert view.limit(3).count() == 3
    assert session.sent[-1]["filters"] == {"field": "scene", "operator": "=", "value": "highway"}


@pytest.mark.parametrize(
    "build,expected",
    [
        (lambda v: v, 5),
        (lambda v: v.limit(3), 3),
        (lambda v: v.skip(4), 1),
        (lambda v: v.skip(1).limit(2), 2),
        (lambda v: v.skip(7), 0),
    ],
)
def test_similarity_view_count_applies_skip_and_limit(build, expected):
    samples = []
    for index in range(5):
        sample = Sample(f"/img_{index}.jpg")
        sample.id = index + 1
        sample.distance = index / 10
        samples.append(sample)
    view = build(SimilarityResultView(samples, _Ds(_Recorder({}))))
    assert view.count() == expected
    assert len(view) == expected
    assert len(view.to_list()) == expected


def _concept_client():
    client = Client("http://t/api", api_token="test-token")
    client.session = _Recorder({"id": 7, "name": "c"})
    return client


def _dataset(client):
    dataset = Dataset.__new__(Dataset)  # no network
    dataset._id = 1
    dataset.client = client
    return dataset


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": "a river crossing the landscape"},
        {"seed_ids": [1, 2, 3]},
        {"seed_ids": [1, 2], "neg_ids": [3], "kind": "probe"},
    ],
)
def test_create_concept_leaves_the_threshold_to_the_server(kwargs):
    client = _concept_client()
    assert _dataset(client).create_concept("c", **kwargs) == {"id": 7, "name": "c"}
    assert "threshold" not in client.session.sent[-1], "the server derives the default from the score range"


def test_create_concept_sends_an_explicit_threshold():
    client = _concept_client()
    _dataset(client).create_concept("c", seed_ids=[1], threshold=0.7)
    assert client.session.sent[-1]["threshold"] == 0.7


# --------------------------------------------------------------------------------------------
# query("x IS NULL") must mean what filter(x__is_null=...) means: a missing key counts as null.
# It became `x = null` (matches nothing) and `x != null` (matches every sample).
# --------------------------------------------------------------------------------------------


QUERY_FIELDS = {"reviewed": TEXT, "scene": TEXT}


def _filter_dict(**kwargs):
    dataset = Dataset.__new__(Dataset)  # no network: filter() only builds a dict
    return Dataset.filter(dataset, **kwargs)._filters


@pytest.mark.parametrize(
    "query,kwargs",
    [
        ("reviewed IS NULL", {"reviewed__is_null": True}),
        ("reviewed is null", {"reviewed__is_null": True}),
        ("reviewed IS NOT NULL", {"reviewed__is_null": False}),
        ("reviewed is not null", {"reviewed__is_null": False}),
    ],
)
def test_query_is_null_matches_filter_is_null(query, kwargs):
    # dataset.query() sends the text to the server's parser (backend/filter_query.py).
    assert parse_query(query, QUERY_FIELDS) == [_filter_dict(**kwargs)]


def test_query_is_null_combines_with_other_conditions():
    assert parse_query("scene = 'highway' AND reviewed IS NOT NULL", QUERY_FIELDS) == [
        {"field": "scene", "operator": "=", "value": "highway"},
        {"field": "reviewed", "operator": "is_not_null", "value": None},
    ]
