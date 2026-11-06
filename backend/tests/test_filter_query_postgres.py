"""The filter query endpoints on a real dataset: its own fields decide what a query may say, and the
filters they return select the samples the text describes."""

import os
from typing import Any

import pytest
from fastapi import HTTPException

from backend.filter_query import DATETIME, LIST, NUMBER, TEXT
from backend.routers import datasets
from backend.routers import filter_query as endpoints
from backend.tests.test_field_registry_postgres import ingest, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

SAMPLES = [
    {'serial': 103000752, 'split': 'train', 'score': 0.9},
    {'serial': 103001025, 'split': 'val', 'score': 0.1},
    {'serial': 103004080, 'split': 'train', 'score': 0.4, 'note': 'x'},
    {'serial': 103009999, 'split': 'train', 'score': 0.7},
]


@pytest.fixture
def query_database(registry_database, monkeypatch):
    def run(scenario):
        async def wrapped(database_):
            monkeypatch.setattr(endpoints, 'db', database_)
            await ingest(SAMPLES)
            await scenario(database_)

        registry_database(wrapped)

    return run


async def count(filter_node):
    response = await datasets.list_samples_post(1, datasets.SamplesRequest(limit=0, filters=filter_node))
    return response['total']


async def parse(query, current=()):
    return await endpoints.parse_filter_query(1, endpoints.FilterQueryParse(query=query, current=list(current)))


def test_the_datasets_own_fields_and_kinds_are_what_a_query_may_name(query_database):
    async def scenario(database_):
        kinds = await endpoints.field_kinds(1)
        assert {name: kinds[name] for name in ('serial', 'split', 'score', 'note')} == {
            'serial': NUMBER,
            'split': TEXT,
            'score': NUMBER,
            'note': TEXT,
        }
        assert {name: kinds[name] for name in ('id', 'filepath', 'created_at', 'tags')} == {
            'id': NUMBER,
            'filepath': TEXT,
            'created_at': DATETIME,
            'tags': LIST,
        }

    query_database(scenario)


def test_a_parsed_query_selects_the_samples_it_describes(query_database):
    async def scenario(database_):
        parsed = await parse('serial in (103000752  103001025\n103004080) AND split = train')
        assert parsed['filters'] == [
            {'field': 'serial', 'operator': 'in', 'value': [103000752, 103001025, 103004080]},
            {'field': 'split', 'operator': '=', 'value': 'train'},
        ]
        assert await count(parsed['filter']) == 2
        assert await count((await parse('NOT (score < 0.5 OR note exists)'))['filter']) == 2
        assert await count((await parse('score >= 0.4 AND score <= 0.9 AND note not exists'))['filter']) == 2

    query_database(scenario)


def test_a_mistake_is_a_400_with_where_and_suggestions(query_database):
    async def scenario(database_):
        with pytest.raises(HTTPException) as refused:
            await parse('split = train\nAND seriel in (1 2)')
        detail: Any = refused.value.detail
        assert refused.value.status_code == 400
        assert detail == {
            'message': "line 2, column 5: unknown field 'seriel'",
            'errors': [{'message': "unknown field 'seriel'", 'line': 2, 'column': 5, 'suggestions': ['serial']}],
        }

    query_database(scenario)


def test_an_unchanged_part_keeps_its_chip_and_format_leaves_out_what_text_cannot_say(query_database):
    async def scenario(database_):
        concept = {'field': '_concept', 'operator': 'concept_exclude', 'value': {'concept_id': 3, 'threshold': 0.9}}
        current = [
            concept,
            {'logic': 'AND', 'filters': [{'field': 'split', 'operator': '==', 'value': 'train'}]},
            {'field': 'serial', 'operator': 'in', 'value': ['103000752']},
        ]
        formatted = await endpoints.format_filter_query(1, endpoints.FilterQueryFormat(filters=current))
        assert formatted == {
            'parts': [None, 'split = "train"', 'serial in ("103000752")'],
            'query': 'split = "train"\nAND serial in ("103000752")',
        }
        # Built differently (== and an AND around one filter) but the same filter: the chip is kept.
        parsed = await parse(formatted['query'] + '\nAND score > 0.5', current)
        assert parsed['reuse'] == [1, 2, None]

    query_database(scenario)
