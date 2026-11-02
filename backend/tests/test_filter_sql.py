"""Pure-function regression tests for parse_filter_node SQL generation.

These need NO database — parse_filter_node builds a SQL string + an args list, and
_get_array_columns_sync() falls back to {'tags'} without a populated cache. They lock these
fixes:
  * label-array  !=/not_in  must mean "contains NONE"  (was returning the OPPOSITE rows),
  * is_null/is_not_null on native columns must check the column, not metadata->>,
  * unknown operators on a label element must 400, not silently match,
  * a malformed created_at must 400, not pass a bad string to Postgres (→ 500).
"""

import os
import re
from typing import Any

import pytest
from fastapi import HTTPException

from backend.label_fields import DatasetArrayColumns
from backend.routers.datasets import (
    _LABEL_ARRAY_FIELDS,
    _LABEL_NEG_OPS,
    SAMPLES_JOIN_FIELD_MAPPING,
    _element_predicate_sql,
    parse_filter_node,
)

# The positive spellings _element_predicate_sql accepts, for the mirror-case test below.
_LABEL_POS_OPS_FOR_TEST = ('=', '==', 'in')


def _sql(field, operator, value=None):
    args: list[Any] = []
    sql, _ = parse_filter_node(
        {"field": field, "operator": operator, "value": value},
        args,
        1,
        array_columns=DatasetArrayColumns(
            {'tags'},
            label_fields={'detections', 'polygons', 'ground_truth', 'predictions'},
        ),
    )
    return sql, args


# ---- label-array equality is positive containment ----


def test_detections_label_eq_is_positive_containment():
    sql, args = _sql("detections.label", "=", "car")
    assert "@>" in sql
    assert not sql.lstrip().startswith("(NOT")
    assert args == ["car"]


def test_detections_label_in_ors_each_value():
    sql, args = _sql("detections.label", "in", ["car", "bus"])
    assert sql.count("@>") == 2 and " OR " in sql
    assert "NOT" not in sql
    assert args == ["car", "bus"]


# ---- the headline bug: negation must wrap, not flip ----


@pytest.mark.parametrize("field", ["detections.label", "polygons.label"])
def test_label_neq_is_negated_containment(field):
    sql, args = _sql(field, "!=", "car")
    assert sql.lstrip().startswith("(NOT "), sql
    assert "@>" in sql
    assert args == ["car"]


@pytest.mark.parametrize("field", ["detections.label", "polygons.label"])
def test_label_not_in_is_negated_containment(field):
    sql, args = _sql(field, "not_in", ["car", "bus"])
    assert sql.lstrip().startswith("(NOT "), sql
    assert sql.count("@>") == 2
    assert args == ["car", "bus"]


# ---- EVERY negation spelling must work on EVERY label field ----
# `_LABEL_NEG_OPS` lists five spellings, but the generic dotted-label branch's pos_op map only
# handled four — and the missing one, 'not in' (with a space), is exactly what the frontend emits
# for facet/multi-select excludes (filter-utils.ts) and histogram double-click-to-exclude. So
# `ground_truth.label not in [...]` raised HTTP 400 from _element_predicate_sql while
# `detections.label` (which has its own @> branch) worked, making the failure look arbitrary.
# The old tests covered 'not_in' and '!=' only, i.e. 2 of the 5 spellings.


@pytest.mark.parametrize("field", _LABEL_ARRAY_FIELDS)
@pytest.mark.parametrize("op", _LABEL_NEG_OPS)
def test_every_label_negation_spelling_negates(field, op):
    sql, _ = _sql(f"{field}.label", op, ["car"])
    assert sql.lstrip().startswith("(NOT "), f"{field}.label {op!r} did not negate: {sql!r}"


@pytest.mark.parametrize("field", _LABEL_ARRAY_FIELDS)
@pytest.mark.parametrize("op", _LABEL_POS_OPS_FOR_TEST)
def test_every_label_positive_spelling_stays_positive(field, op):
    # The mirror case: a positive op must NOT come back negated.
    value = ["car"] if op == "in" else "car"
    sql, _ = _sql(f"{field}.label", op, value)
    assert not sql.lstrip().startswith("(NOT "), f"{field}.label {op!r} was negated: {sql!r}"


def test_segmentation_labels_neq_is_negated():
    sql, args = _sql("segmentation.labels", "!=", "road")
    assert sql.lstrip().startswith("(NOT "), sql
    assert "?" in sql
    assert args == ["road"]


# ---- generic dotted path (ground_truth / predictions) ----


def test_ground_truth_label_eq_is_containment():
    sql, args = _sql("ground_truth.label", "=", "car")
    assert "@>" in sql and "EXISTS" not in sql
    assert args == ["car"]


def test_ground_truth_label_neq_is_negated_containment():
    sql, args = _sql("ground_truth.label", "!=", "car")
    assert sql.startswith("(NOT ") and "@>" in sql
    assert args == ["car"]


def test_predictions_confidence_gt_unaffected():
    sql, args = _sql("predictions.confidence", ">", 0.9)
    assert sql.startswith("EXISTS")
    assert ">" in sql and "NOT" not in sql


# ---- is_null / is_not_null must target the right column ----


@pytest.mark.parametrize("field", ["id", "created_at", "filepath", "media_type"])
def test_is_null_on_native_column_uses_column(field):
    sql, _ = _sql(field, "is_null")
    assert sql == f"{field} IS NULL", sql
    assert "metadata" not in sql


def test_is_not_null_on_native_column_uses_column():
    sql, _ = _sql("filepath", "is_not_null")
    assert sql == "filepath IS NOT NULL"


def test_is_null_on_metadata_field_still_uses_metadata():
    sql, _ = _sql("some_custom_field", "is_null")
    assert sql == "metadata->>'some_custom_field' IS NULL"


# ---- exists / not_exists must target the right column too (tags is a native TEXT[] column,
#      never a metadata key — the bug rendered `metadata ? 'tags'`, matching ZERO rows) ----


def test_exists_on_array_column_checks_the_array_not_metadata():
    sql, _ = _sql("tags", "exists")
    assert sql == "(tags IS NOT NULL AND array_length(tags, 1) > 0)", sql
    assert "metadata" not in sql  # the bug: `metadata ? 'tags'` matched nothing


def test_not_exists_on_array_column_checks_the_array():
    sql, _ = _sql("tags", "not_exists")
    assert sql == "array_length(tags, 1) IS NULL", sql
    assert "metadata" not in sql


@pytest.mark.parametrize("field", ["filepath", "media_type", "id", "created_at"])
def test_exists_on_native_scalar_uses_column(field):
    sql, _ = _sql(field, "exists")
    assert sql == f"{field} IS NOT NULL", sql
    assert "metadata" not in sql


def test_not_exists_on_native_scalar_uses_column():
    sql, _ = _sql("filepath", "not_exists")
    assert sql == "filepath IS NULL", sql


def test_exists_on_metadata_field_still_uses_jsonb_key():
    # Metadata key-existence is preserved — it detects complex values (heatmap overlays, etc.)
    # that is_not_null via metadata->>'field' would miss.
    sql, args = _sql("some_custom_field", "exists")
    assert sql == "(metadata ? $1)", sql
    assert args == ["some_custom_field"]


def test_not_exists_on_metadata_field_still_uses_jsonb_key():
    sql, args = _sql("some_custom_field", "not_exists")
    assert sql == "NOT (metadata ? $1)", sql
    assert args == ["some_custom_field"]


# ---- `=` on a native column must use the MAPPED col (s.id), not a bare ambiguous `id`/`created_at`,
#      in the embeddings/search JOIN (both tables have id+created_at) — every sibling op already did ----


def test_eq_on_id_uses_mapped_column_in_joined_query():
    args: list[Any] = []
    sql, _ = parse_filter_node({"field": "id", "operator": "=", "value": 5}, args, 1, field_mapping={"id": "s.id"})
    assert sql == "s.id = $1", sql  # bare `id` is ambiguous in `FROM embeddings e JOIN samples s` → 500


def test_eq_on_created_at_uses_mapped_column_in_joined_query():
    args: list[Any] = []
    sql, _ = parse_filter_node(
        {"field": "created_at", "operator": "=", "value": "2024-01-15T00:00:00Z"},
        args,
        1,
        field_mapping={"id": "s.id", "created_at": "s.created_at"},
    )
    assert sql == "s.created_at = $1", sql


def test_eq_on_id_grid_path_stays_bare():
    # No field_mapping (the unaliased grid query) → bare `id` is correct (single table, no ambiguity).
    sql, _ = _sql("id", "=", 5)
    assert sql == "id = $1", sql


# ---- ARCHITECTURE LOCK-IN: EVERY native-column operator must emit the field_mapping-resolved
#      column (s.id / s.created_at), never a bare ambiguous one. `=` regressed once and 500'd the
#      JOIN queries; this matrix makes a future branch that forgets {col} fail loudly. ----

_NATIVE_OP_VALUES = {
    "=": 5,
    "!=": 5,
    ">": 5,
    ">=": 5,
    "<": 5,
    "<=": 5,
    "in": [5, 6],
    "not in": [5, 6],
    "is_null": None,
    "is_not_null": None,
    "exists": None,
    "not_exists": None,
}
_BARE_ID = re.compile(r"(?<![\w.])id(?![\w])")  # a standalone `id` token not part of `s.id`


@pytest.mark.parametrize("op,value", list(_NATIVE_OP_VALUES.items()))
def test_native_id_op_uses_mapped_column_never_bare(op, value):
    args: list[Any] = []
    sql, _ = parse_filter_node(
        {"field": "id", "operator": op, "value": value},
        args,
        1,
        field_mapping={"id": "s.id"},
    )
    assert "s.id" in sql, f"{op}: expected mapped s.id, got {sql!r}"
    assert _BARE_ID.search(sql) is None, f"{op}: bare ambiguous `id` leaked into JOIN SQL: {sql!r}"


# ---- EVERY router that joins `samples s` must qualify the ambiguous native columns ----
# parse_filter_node emits native columns BARE unless field_mapping qualifies them. `embeddings`
# and `sample_hashes` both have their own `id`, and `embeddings` also has `created_at`, so an
# unqualified one is `column reference "created_at" is ambiguous` — a 500. The mapping used to be
# hand-copied per call site and the `created_at` half landed at only half of them, so the Sparse
# Sample panel 500'd on the most common filter on a streamed dataset while search/embeddings were
# fine. These tests lock in the shared constant AND that nobody re-inlines a partial copy.


@pytest.mark.parametrize("field", sorted(SAMPLES_JOIN_FIELD_MAPPING))
@pytest.mark.parametrize("op", [">", ">=", "<", "<=", "=", "!="])
def test_join_mapping_qualifies_every_ambiguous_column(field, op):
    value = "2024-01-15T00:00:00Z" if field == "created_at" else 5
    args: list[Any] = []
    sql, _ = parse_filter_node(
        {"field": field, "operator": op, "value": value},
        args,
        1,
        field_mapping=SAMPLES_JOIN_FIELD_MAPPING,
    )
    assert f"s.{field}" in sql, f"{field} {op!r} emitted an ambiguous bare column: {sql!r}"


def test_join_mapping_covers_columns_shared_with_joined_tables():
    # `embeddings` has id + created_at; `sample_hashes` has id. Both are joined to `samples s` by
    # the filter-taking endpoints. If a future join shares another native samples column, add it
    # to SAMPLES_JOIN_FIELD_MAPPING — this assertion is the reminder.
    assert set(SAMPLES_JOIN_FIELD_MAPPING) == {"id", "created_at"}
    assert all(v == f"s.{k}" for k, v in SAMPLES_JOIN_FIELD_MAPPING.items())


def test_no_router_hand_copies_the_join_mapping():
    """A partial inline copy is exactly how `created_at` went missing at 4 of 8 call sites."""
    routers = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "routers")
    offenders = []
    for name in os.listdir(routers):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(routers, name)) as fh:
            for lineno, line in enumerate(fh, 1):
                if "field_mapping={" in line:
                    offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, "inline field_mapping dict(s) — import SAMPLES_JOIN_FIELD_MAPPING instead:\n" + "\n".join(
        offenders,
    )


# ---- regression: unknown / malformed inputs raise 400, never silently match ----


def test_unknown_element_operator_400():
    with pytest.raises(HTTPException) as e:
        _element_predicate_sql("label", "starts_with", "ca", [], 1)
    assert e.value.status_code == 400


def test_malformed_created_at_400():
    with pytest.raises(HTTPException) as e:
        _sql("created_at", "=", "not-a-date")
    assert e.value.status_code == 400


def test_valid_created_at_ok():
    sql, args = _sql("created_at", "=", "2026-06-08T00:00:00Z")
    assert "created_at = " in sql
    # coerced to a datetime, not left as a string
    import datetime

    assert isinstance(args[0], datetime.datetime)


# ---- datetime metadata range ops compare as ISO text so they use the promoted index ----
# (a ::timestamp cast can't use the metadata->>'field' btree and seq-scans millions of rows;
#  ISO-8601 text sorts chronologically, so text comparison is both correct and index-friendly)


@pytest.mark.parametrize("op", [">", ">=", "<", "<="])
def test_datetime_metadata_range_uses_indexed_text(op):
    sql, args = _sql("local_datetime", op, "2024-03-01T00:00:00.000Z")
    assert sql == f"metadata->>'local_datetime' {op} $1"
    # must NOT cast — a cast would defeat the promoted text index
    assert "::timestamp" not in sql and "::float" not in sql
    assert args == ["2024-03-01T00:00:00.000Z"]


def test_numeric_metadata_range_still_numeric():
    # a plain number bound must still use the numeric (float) path
    sql, _ = _sql("brightness", ">", 0.5)
    assert "::timestamp" not in sql
    assert "::float" in sql


# ---- an INCOMPLETE range rule (empty / None value) must be a no-op, not a 500 ----
# A datetime-local picker with a date but no time yet emits '' — which would fail the
# datetime heuristic, fall to the numeric path, and bind '' to a ::float cast (asyncpg
# DataError: "must be real number, not str"). It must be treated as TRUE (no constraint).


@pytest.mark.parametrize("field", ["local_datetime", "datetime", "brightness"])
@pytest.mark.parametrize("op", [">", ">=", "<", "<="])
def test_empty_range_value_is_noop(field, op):
    sql, args = _sql(field, op, "")
    assert sql == "TRUE"
    assert args == []


@pytest.mark.parametrize("op", [">", ">=", "<", "<="])
def test_none_range_value_is_noop(op):
    sql, args = _sql("local_datetime", op, None)
    assert sql == "TRUE"
    assert args == []


def test_whitespace_only_range_value_is_noop():
    sql, args = _sql("datetime", ">=", "   ")
    assert sql == "TRUE"
    assert args == []


# ---- a DATE-ONLY value ('2025-01-01', no 'T') must compare as TEXT, not crash ----
# The old heuristic required a 'T' to treat a value as datetime; a date-only string fell to the
# numeric path, cast the column to ::float, and bound the date string to a real-number param —
# asyncpg 500 ("must be real number, not str"). The decision is now "is the value numeric?", so a
# date (or full datetime) compares as text and never reaches a ::float bind.


@pytest.mark.parametrize("val", ["2025-01-01", "2025-01-01T00:00", "2025-01-01T15:02:03.5"])
@pytest.mark.parametrize("op", [">", ">=", "<", "<="])
def test_date_or_datetime_value_compares_as_text(op, val):
    sql, args = _sql("local_datetime", op, val)
    assert "::float" not in sql and "::timestamp" not in sql
    # A partial-precision value means its whole day/minute: `<=` ends
    # at, and `>` starts at, the next unit; `>=`/`<` already sit on the unit's start. A value
    # with a fraction (full precision) stays exactly as given.
    nxt = {"2025-01-01": "2025-01-02", "2025-01-01T00:00": "2025-01-01T00:01"}.get(val)
    if nxt and op in ("<=", ">"):
        assert sql == f"metadata->>'local_datetime' {'<' if op == '<=' else '>='} $1"
        assert args == [nxt]
    else:
        assert sql == f"metadata->>'local_datetime' {op} $1"
        assert args == [val]


@pytest.mark.parametrize("val", [0.5, "0.5", "2024", 2024])
def test_numeric_value_still_uses_float_path(val):
    # Genuinely numeric values (number or numeric string) keep the ::float index path.
    sql, _ = _sql("brightness", ">", val)
    assert "::float" in sql


# ---- regression: a numeric STRING on a range op must bind a number ----
# The float() probe only chose the ::float path; the untouched str was then bound to a
# ::float parameter -> asyncpg DataError -> 500 from the Add-Filter editor on any
# string-typed numeric field (and from dataset.filter(f__gt="0.5")).


@pytest.mark.parametrize("op", [">", ">=", "<", "<="])
@pytest.mark.parametrize("val", ["0.5", "2024", "-3", "1e-3"])
def test_numeric_string_on_range_op_binds_a_float(op, val):
    sql, args = _sql("brightness", op, val)
    assert "::float" in sql
    assert args == [float(val)]
    assert isinstance(args[0], float)


def test_numeric_string_on_promoted_range_op_binds_a_float():
    args: list[Any] = []
    sql, _ = parse_filter_node({"field": "brightness", "operator": ">", "value": "0.5"}, args, 1, None, {"brightness"})
    assert sql == "(metadata->>'brightness')::float > $1"
    assert args == [0.5]


@pytest.mark.parametrize("val", [0.5, 2024])
def test_real_numbers_on_range_ops_are_bound_unchanged(val):
    _, args = _sql("brightness", ">", val)
    assert args == [val]


def test_label_element_range_op_numeric_string_binds_a_float():
    sql, args = _sql("detections.confidence", ">", "0.9")
    assert "::double precision" in sql
    assert args[-1] == 0.9 and isinstance(args[-1], float)


def test_label_element_range_op_non_numeric_string_is_a_400_not_a_500():
    with pytest.raises(HTTPException) as e:
        _sql("detections.confidence", ">", "high")
    assert e.value.status_code == 400


# ---- typed comparisons ----
# Each was a text comparison where the user thinks in numbers / dates / literal text. These
# lock the SQL.


def _sql_promoted(field, operator, value):
    args: list[Any] = []
    sql, _ = parse_filter_node(
        {"field": field, "operator": operator, "value": value},
        args,
        1,
        numeric_indexed_fields={field},
    )
    return sql, args


@pytest.mark.parametrize("val", [1, 1.0, 2024, -0.5])
def test_number_equality_compares_numerically(val):
    # Regression: stored 1.0 renders as the text '1.0' and never equalled '1' -> `aspect_ratio = 1`
    # matched nothing on eurosat. A JSON number now compares as a number.
    sql, args = _sql("aspect_ratio", "=", val)
    assert "::float" in sql and "->>'aspect_ratio' = $1" not in sql
    assert args == [float(val)]
    sql, args = _sql("aspect_ratio", "!=", val)
    assert "::float" in sql and "IS DISTINCT FROM $1" in sql
    assert args == [float(val)]


def test_number_list_ops_compare_numerically():
    sql, args = _sql("aspect_ratio", "in", [1, 2])
    assert sql.endswith("= ANY($1::float8[])") and args == [[1.0, 2.0]]
    sql, args = _sql("aspect_ratio", "not in", [1])
    assert "IS NULL OR NOT" in sql and "::float8[]" in sql and args == [[1.0]]


def test_promoted_numeric_field_equality_rides_the_float_index():
    # Regression: `year = 2022` on main was a text compare -> 11.6 s seq scan; the float index
    # (metadata->>'year')::double precision is what `>=` already used.
    sql, args = _sql_promoted("year", "=", 2022)
    assert sql == "(metadata->>'year')::float = $1" and args == [2022.0]
    # On a promoted numeric field a numeric STRING (enum checkboxes send strings) is a number too.
    sql, args = _sql_promoted("year", "in", ["2022", "2023"])
    assert sql == "(metadata->>'year')::float = ANY($1::float8[])" and args == [[2022.0, 2023.0]]


@pytest.mark.parametrize("val", ["007", "2024", "Forest", True, "1e999"])
def test_strings_and_booleans_stay_exact_text_matches(val):
    # A string on a non-numeric field stays an exact text match ('007' must not match 7), so a
    # promoted string field keeps its btree index; booleans match the text 'true'/'false'.
    sql, args = _sql("device_id", "=", val)
    assert sql == "metadata->>'device_id' = $1"
    assert args == [("true" if val is True else val)]


def test_mixed_list_stays_text():
    sql, args = _sql("label", "in", ["Forest", 1])
    assert sql.endswith("= ANY($1::text[])") and args == [["Forest", "1"]]


@pytest.mark.parametrize(
    "val, lo, hi",
    [
        ("2023-08-14", "2023-08-14", "2023-08-15"),
        ("2023-08-14T17:39", "2023-08-14T17:39", "2023-08-14T17:40"),
        ("2023-08-14 23:59", "2023-08-14 23:59", "2023-08-15 00:00"),
        ("2023-08-14T17:39:32", "2023-08-14T17:39:32", "2023-08-14T17:39:33"),
        ("2024-02-28", "2024-02-28", "2024-02-29"),
    ],
)
def test_partial_datetime_equality_is_the_whole_unit(val, lo, hi):
    # Regression: the picker emits minute precision while stored values carry seconds, so `=` never
    # matched. The value now names its unit: [lo, hi) as a text range (btree-friendly).
    sql, args = _sql("datetime", "=", val)
    assert sql == "(metadata->>'datetime' >= $1 AND metadata->>'datetime' < $2)"
    assert args == [lo, hi]
    sql, args = _sql("datetime", "!=", val)
    assert sql == "(metadata->>'datetime' IS NULL OR NOT (metadata->>'datetime' >= $1 AND metadata->>'datetime' < $2))"
    assert args == [lo, hi]


@pytest.mark.parametrize("val", ["2023-08-14T17:39:32.5", "2023-08-14T17:39:32+00:00", "2024-02-30", "2023-8-14"])
def test_full_precision_or_non_dates_keep_exact_equality(val):
    sql, args = _sql("datetime", "=", val)
    assert sql == "metadata->>'datetime' = $1" and args == [val]


def test_created_at_partial_values_bind_datetimes():
    import datetime as dt

    utc = dt.timezone.utc
    sql, args = _sql("created_at", "<=", "2026-06-17")
    assert sql == "created_at < $1" and args == [dt.datetime(2026, 6, 18, tzinfo=utc)]
    sql, args = _sql("created_at", "=", "2026-06-17T12:00")
    assert sql == "(created_at >= $1 AND created_at < $2)"
    assert args == [dt.datetime(2026, 6, 17, 12, 0, tzinfo=utc), dt.datetime(2026, 6, 17, 12, 1, tzinfo=utc)]


@pytest.mark.parametrize(
    "val, pattern",
    [("%", "%\\%%"), ("a_b", "%a\\_b%"), ("50% off", "%50\\% off%"), ("C:\\x", "%C:\\\\x%"), ("plain", "%plain%")],
)
def test_contains_matches_the_literal_text(val, pattern):
    # Regression: `%` and `_` were pattern characters -> "contains %" matched every row.
    sql, args = _sql("source_tif", "contains", val)
    assert sql == "metadata->>'source_tif' ILIKE $1 ESCAPE '\\'"
    assert args == [pattern]
    inner, _ = _element_predicate_sql("label", "contains", val, [], 1)
    assert "ESCAPE '\\'" in inner
