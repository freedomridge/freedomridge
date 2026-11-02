"""Lock the concept_select / concept_exclude filter operators (embedding
"concepts"). They must emit a semi-join (EXISTS) or anti-join (NOT EXISTS) against
embedding_concept_scores, correlate on the OUTER sample id the way the caller does (bare
`id` for the unaliased samples query, `s.id` when field_mapping says so), parameterize the
concept_id and threshold, and validate the comparison op against an allow-list (it is
interpolated, so an unknown op must 400 rather than reach SQL).

Pure function — the concept branch returns before any DB access, so no DB / no async.
"""

from typing import Any

import pytest
from fastapi import HTTPException

from backend.routers.datasets import parse_filter_node


def test_concept_exclude_bare_id_unaliased():
    args: list[Any] = []
    sql, idx = parse_filter_node(
        {'field': '_concept', 'operator': 'concept_exclude', 'value': {'concept_id': 7, 'op': '>', 'threshold': 0.8}},
        args,
        1,
    )
    # One guard for the statement's concepts in front, then the anti-join.
    assert sql.startswith('((SELECT require_concept_scores($3::int[])) AND (NOT EXISTS')
    assert 'embedding_concept_scores' in sql
    assert '_cs.sample_id = id' in sql  # bare id (FROM samples, unaliased)
    assert '_cs.concept_id = $1' in sql
    assert '_cs.score > $2' in sql
    assert args == [7, 0.8, [7]]
    assert idx == 4


def test_concept_select_mapped_id_joined_context():
    args: list[Any] = []
    sql, idx = parse_filter_node(
        {'field': '_concept', 'operator': 'concept_select', 'value': {'concept_id': 3, 'op': '<=', 'threshold': 0.2}},
        args,
        5,
        field_mapping={'id': 's.id'},
    )
    assert sql.startswith('((SELECT require_concept_scores($7::int[])) AND (EXISTS')  # select = semi-join
    assert '_cs.sample_id = s.id' in sql  # embeddings/search join → s.id
    assert '_cs.concept_id = $5' in sql
    assert '_cs.score <= $6' in sql
    assert args == [3, 0.2, [3]]
    assert idx == 8


def test_concept_defaults_op_and_threshold():
    args: list[Any] = []
    sql, idx = parse_filter_node(
        {'field': '_concept', 'operator': 'concept_exclude', 'value': {'concept_id': 9}},
        args,
        1,
    )
    assert '_cs.score > $2' in sql  # default op '>'
    assert args == [9, 0.5, [9]]  # default threshold 0.5
    assert idx == 4


def test_concept_missing_id_is_noop():
    for value in ({}, None, {'op': '>', 'threshold': 0.5}):
        args: list[Any] = []
        sql, idx = parse_filter_node(
            {'field': '_concept', 'operator': 'concept_exclude', 'value': value},
            args,
            1,
        )
        assert sql == 'TRUE' and idx == 1 and args == []


def test_concept_invalid_op_rejected():
    # The comparison op is interpolated into SQL, so anything off the allow-list must 400.
    with pytest.raises(HTTPException):
        parse_filter_node(
            {'field': '_concept', 'operator': 'concept_exclude', 'value': {'concept_id': 1, 'op': '; DROP TABLE'}},
            [],
            1,
        )
