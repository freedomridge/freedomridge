"""Links that must keep opening (frontend/tests/link-fixtures.json, frontend/lib/links.ts). A link holds a filter as
query text (`q=`) or as encoded stages (`f=`). The frontend test decodes every `f=`; these parse every `q=` with
today's query language and compile every link's filters with today's filter compiler, so a backend change that
would break links copied before it (an operator renamed or removed, a value form no longer read) fails here."""

import json
from pathlib import Path
from typing import Any

import pytest

from backend.filter_query import format_filter, parse_query
from backend.label_fields import DatasetArrayColumns
from backend.routers.datasets import parse_filter_node

FIXTURES = json.loads((Path(__file__).resolve().parents[2] / 'frontend' / 'tests' / 'link-fixtures.json').read_text())
LINKS = [(fixture['name'], [stage['filter'] for stage in fixture['stages']]) for fixture in FIXTURES['f']]
LINKS += [(fixture['name'], fixture['filters']) for fixture in FIXTURES['q']]
# The fixtures' declared datetime fields, so their filters compile as a dataset's would: by instant.
DATETIMES = {name for name, kind in FIXTURES['fields'].items() if kind == 'datetime'}


@pytest.mark.parametrize('fixture', FIXTURES['q'], ids=lambda fixture: fixture['name'])
def test_every_query_link_parses_to_its_chips(fixture):
    assert parse_query(fixture['q'], FIXTURES['fields']) == fixture['filters']


@pytest.mark.parametrize('fixture', FIXTURES['q'], ids=lambda fixture: fixture['name'])
def test_chips_written_as_copy_link_writes_them_read_back_as_the_same_chips(fixture):
    # Copy link writes each chip's filter as format_filter does and joins them with AND (frontend/lib/links.ts).
    query = ' AND '.join(format_filter(node) or '' for node in fixture['filters'])
    assert parse_query(query, FIXTURES['fields']) == fixture['filters']


@pytest.mark.parametrize(('name', 'filters'), LINKS, ids=[name for name, _ in LINKS])
def test_every_link_filter_still_compiles(name, filters):
    for node in filters:
        args: list[Any] = [1]
        sql, _ = parse_filter_node(
            node,
            args,
            2,
            dataset_id=1,
            array_columns=DatasetArrayColumns({'tags'}, label_fields=set(), datetime_fields=DATETIMES),
        )
        # A filter the compiler reads as nothing (TRUE) or as matching nothing (FALSE) has lost its meaning.
        assert sql.strip() not in ('', 'TRUE', 'FALSE'), (name, node)
        if any(field in json.dumps(node) for field in DATETIMES):
            assert 'metadata_instant_v1' in sql and "metadata->>'sampled_at' >=" not in sql, (name, node)


def test_the_fixtures_cover_every_operator_family_and_chip_kind():
    operators = json.dumps([filters for _, filters in LINKS])
    for operator in (
        '"="',
        '"!="',
        '">"',
        '">="',
        '"<"',
        '"<="',
        '"in"',
        '"not in"',
        '"contains"',
        '"exists"',
        '"not_exists"',
        '"is_null"',
        '"is_not_null"',
        '"NOT"',
        '"OR"',
        '"geo_polygon"',
        '"embedding_polygon"',
        '"concept_exclude"',
    ):
        assert operator in operators, operator
    assert {stage['type'] for fixture in FIXTURES['f'] for stage in fixture['stages']} == {
        'filter',
        'map_region',
        'selection',
        'similarity',
    }
