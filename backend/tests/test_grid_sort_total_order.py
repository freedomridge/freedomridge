"""Sorted grid/SDK paging must be a TOTAL order.

OFFSET paging over `ORDER BY <field>` with ties duplicated and skipped rows because each
page's bounded sort returned the tied rows in a different order (a large dataset sorted by a
10-value field: 6,043 rows shown twice, 6,043 never). Every ORDER BY the grid can emit now comes from
one builder, `_grid_order_by`, and ends in the `, id` tiebreaker. The live lock is
python/fridge/test_sdk.py "sorted paging"; this pins the SQL shape per branch.
"""

import pytest

from backend.routers.datasets import _NATIVE_SORT_COLUMNS, _grid_order_by


@pytest.mark.parametrize("direction", ["ASC", "DESC"])
@pytest.mark.parametrize(
    "sort_by,row",
    [
        ("created_at", None),
        ("last_modified_at", None),
        ("filepath", None),
        ("brightness", {"field_type": "number", "is_promoted": True}),
        ("brightness", {"field_type": "float", "is_promoted": False}),
        ("n_objects", {"field_type": "int", "is_promoted": False}),
        ("scene", {"field_type": "string", "is_promoted": False}),
        ("scene", {"field_type": "enum", "is_promoted": True}),
        ("captured", {"field_type": "datetime", "is_promoted": False}),
    ],
)
def test_every_sort_clause_ends_with_the_id_tiebreaker(sort_by, row, direction):
    clause = _grid_order_by(sort_by, direction, row)
    assert clause.startswith("ORDER BY "), clause
    assert clause.endswith(", id"), clause
    assert f" {direction}" in clause, clause
    assert clause.count("ORDER BY") == 1, clause


def test_sorting_by_id_itself_needs_no_tiebreaker():
    assert _grid_order_by("id", "ASC", None) == "ORDER BY id ASC"
    assert _grid_order_by("id", "DESC", None) == "ORDER BY id DESC"


def test_unknown_field_or_unsupported_type_falls_back_to_the_default_order():
    assert _grid_order_by("nope", "ASC", None) == "ORDER BY id"
    assert _grid_order_by("tags", "DESC", {"field_type": "array", "is_promoted": False}) == "ORDER BY id"


def test_promoted_numeric_puts_missing_values_last_in_both_directions_before_the_tiebreaker():
    # The pages still ride the index: _sorted_page_ids answers them in two parts.
    asc = _grid_order_by("brightness", "ASC", {"field_type": "number", "is_promoted": True})
    desc = _grid_order_by("brightness", "DESC", {"field_type": "number", "is_promoted": True})
    assert asc == "ORDER BY (metadata->>'brightness')::float ASC NULLS LAST, id"
    assert desc == "ORDER BY (metadata->>'brightness')::float DESC NULLS LAST, id"


def test_native_columns_are_exactly_the_four_the_handlers_accept():
    assert set(_NATIVE_SORT_COLUMNS) == {"id", "created_at", "last_modified_at", "filepath"}
