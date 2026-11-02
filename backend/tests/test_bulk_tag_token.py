"""bulk-tag / tag-counts can target a selection TOKEN instead of an id list, so a huge
Select-All tags server-side (via region_selections) without shipping millions of ids. The
predicate must resolve the token's samples minus any exceptions, dataset-scoped.

Pure-function test of the predicate builder (the SQL round-trip is validated separately
against live Postgres). Self-contained.
"""

from backend.routers.datasets import BulkTagRequest, _bulk_target_predicate


def test_token_mode_predicate_scopes_to_token_minus_exclusions():
    req = BulkTagRequest(selection_token="abc123", excluded_ids=[7, 8], add_tags=["x"])
    where, args = _bulk_target_predicate(req, dataset_id=5)
    assert "region_selections WHERE token = $2" in where
    assert "dataset_id = $3" in where  # defense-in-depth dataset scope
    assert "NOT (id = ANY($4))" in where  # exceptions
    assert args == ["abc123", 5, [7, 8]]


def test_id_list_mode_predicate_uses_any():
    req = BulkTagRequest(sample_ids=[1, 2, 3], add_tags=["x"])
    where, args = _bulk_target_predicate(req, dataset_id=5)
    assert where == "id = ANY($2) AND dataset_id = $3"
    assert args == [[1, 2, 3], 5]
