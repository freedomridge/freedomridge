"""Lock the concept_select / concept_exclude filter operators (embedding
"concepts"). They must emit a semi-join (EXISTS) or anti-join (NOT EXISTS) against
embedding_concept_scores, correlate on the OUTER sample id the way the caller does (bare
`id` for the unaliased samples query, `s.id` when field_mapping says so), parameterize the
concept_id and threshold, and validate the comparison op against an allow-list (it is
interpolated, so an unknown op must 400 rather than reach SQL).

Pure function — the concept branch returns before any DB access, so no DB / no async.
"""

import os
from typing import Any

import pytest
from fastapi import HTTPException

from backend.routers.datasets import parse_filter_node
from backend.tests import test_field_registry_postgres

registry_database = test_field_registry_postgres.registry_database


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
def test_concept_exclude_bare_id_unaliased(registry_database):
    """The exclude on the unaliased samples query reads each sample's own score. Its text changed with contract g7
    (one rule everywhere, rule 1.3), so it is asked as rows: it hides the samples above the cut and, now, the samples
    the concept has no score for (it kept those); the concept and the threshold are its own (another concept's
    scores change nothing)."""

    async def scenario(database):
        await database.execute("""
            INSERT INTO samples (id, dataset_id, filepath, media_type) VALUES
                (1, 1, 'high', 'image'), (2, 1, 'low', 'image'), (3, 1, 'at', 'image'), (4, 1, 'unscored', 'image');
            INSERT INTO embedding_concepts (id, dataset_id, name, embedding_field, kind, weight) VALUES
                (7, 1, 'sky', 'clip', 'seed', '[1,0]'), (8, 1, 'road', 'clip', 'seed', '[0,1]');
            INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES
                (7, 1, 0.9), (7, 2, 0.5), (7, 3, 0.75), (8, 4, 0.1), (8, 2, 0.95);
        """)
        args: list[Any] = [1]
        node = {
            'field': '_concept',
            'operator': 'concept_exclude',
            'value': {'concept_id': 7, 'op': '>', 'threshold': 0.8},
        }
        sql, _ = parse_filter_node(node, args, 2, None, None, 1)
        rows = await database.fetch(f'SELECT filepath FROM samples WHERE dataset_id = $1 AND {sql} ORDER BY id', *args)
        assert [row['filepath'] for row in rows] == ['low', 'at']

    registry_database(scenario)


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
