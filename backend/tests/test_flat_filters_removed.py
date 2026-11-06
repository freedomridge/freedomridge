"""Filters are a filter tree only. Removed in 2.0: the flat {field: value} form, read from GET ...?filters= as
metadata equalities and {min, max} ranges, and the samples endpoint's query parameters on fixed metadata keys
(file_date, date_min/max, brightness_min/max). An empty object still means no filter."""

import asyncio
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.routers import datasets


@pytest.mark.parametrize('flat', [{'split': 'train'}, {'score': {'min': 1}}, {'tags': ['a', 'b']}, ['split']])
def test_the_flat_form_is_refused_with_the_tree_form_to_use(flat):
    with pytest.raises(HTTPException) as refused:
        datasets.build_sample_query_parts(1, filters=json.dumps(flat))
    assert refused.value.status_code == 422
    assert refused.value.detail.endswith('The flat {field: value} form was removed in 2.0')


def test_a_tree_and_an_empty_object_still_work():
    where, args, _ = datasets.build_sample_query_parts(
        1,
        filters=json.dumps({'filters': [{'field': 'split', 'operator': '=', 'value': 'train'}]}),
    )
    assert 'split' in where and args[0] == 1 and 'train' in args
    assert datasets.build_sample_query_parts(1, filters='{}')[:2] == (' WHERE dataset_id = $1', [1])


def samples_request(query: str) -> Request:
    return Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/api/datasets/1/samples',
            'headers': [],
            'query_string': query.encode(),
        },
    )


@pytest.mark.parametrize(
    'query, detail',
    [
        (
            'brightness_min=0.3',
            'brightness_min: removed in 2.0. Send filters as a filter tree, for example '
            'filters={"field": "brightness", "operator": ">=", "value": 0.3}',
        ),
        (
            'limit=5&file_date=2024-05-01&date_max=2024-06-01',
            'file_date, date_max: removed in 2.0. Send filters as a '
            'filter tree, for example filters={"field": "file_date", "operator": "=", "value": "2024-05-01"}',
        ),
    ],
)
def test_the_fixed_key_query_parameters_are_refused_not_ignored(query, detail):
    # Refused before any database access: ignored, they would answer with the unfiltered dataset.
    with pytest.raises(HTTPException) as refused:
        asyncio.run(
            datasets.list_samples(
                1,
                samples_request(query),
                limit=50,
                offset=0,
                filters=None,
                sort_by=None,
                sort_order=None,
                fields=None,
            ),
        )
    assert (refused.value.status_code, refused.value.detail) == (422, detail)


def test_build_sample_query_parts_takes_only_filters():
    with pytest.raises(TypeError):
        datasets.build_sample_query_parts(1, brightness_min=0.3)  # type: ignore[call-arg]
