"""Lock the collapse_duplicates filter operator (the "collapse
duplicates" toggle). It must emit a NOT EXISTS against sample_dup_secondary, correlate
on the OUTER sample id the way the caller does (bare `id` for the unaliased samples
query, `s.id` when field_mapping says so), and parameterize the hash_config.

Pure function — the collapse branch returns before any DB access, so no DB / no async.
"""

from typing import Any

from backend.routers.datasets import parse_filter_node


def test_collapse_bare_id_unaliased_main_query():
    args: list[Any] = []
    sql, idx = parse_filter_node(
        {'field': '_collapse_duplicates', 'operator': 'collapse_duplicates', 'value': 'phash_8'},
        args,
        1,
    )
    assert 'NOT EXISTS' in sql and 'sample_dup_secondary' in sql
    assert '_dup.sample_id = id' in sql  # bare id (FROM samples, unaliased)
    assert '_dup.hash_config = $1' in sql
    assert args == ['phash_8']
    assert idx == 2


def test_collapse_mapped_id_joined_context():
    args: list[Any] = []
    sql, idx = parse_filter_node(
        {'field': '_collapse_duplicates', 'operator': 'collapse_duplicates', 'value': '_all'},
        args,
        3,
        field_mapping={'id': 's.id'},
    )
    assert '_dup.sample_id = s.id' in sql  # embeddings/search join → s.id
    assert '_dup.hash_config = $3' in sql
    assert args == ['_all']
    assert idx == 4


def test_collapse_empty_value_is_noop():
    args: list[Any] = []
    sql, idx = parse_filter_node(
        {'field': '_collapse_duplicates', 'operator': 'collapse_duplicates', 'value': ''},
        args,
        1,
    )
    assert sql == 'TRUE' and idx == 1 and args == []
