"""The rule for a missing value, for a NOT (docs/FILTER_QUERY.md, "Operators").

A row with no value for a field never matches a comparison of that field, and never matches the NOT of one; only
`is null` and `not exists` find it. So `NOT (X)` shows a row when the row has a value for everything X compares, and X
does not show it. `f != v` is `NOT (f = v)`, `f not in (...)` is `NOT (f in (...))`, an excluded place or lasso is the
NOT of the place or the lasso, and a concept's exclude is the NOT of its show, with the score as the value.

This module is the one place that knows what a NOT needs a value for (`needs`). The filter compiler
(routers/datasets.py) asks it for a NOT read on one row, and the reader of a dataset with objects (grain_reading.py)
for a NOT read on an image with its objects or an object with its image; each turns a need into SQL for its rows with
the same `Rows.has_need`.

Some leaf forms keep the rule in their own SQL instead, as their plans depend on its shape; a change to the rule
changes them too (routers/datasets.py unless named):
- `geo_polygon_exclude` within -180 to 180, and across the antimeridian (`_polygon_across_leaf`: one copy, the
  MULTIPOLYGON behind its boxes, the copies' OR): `location IS NOT NULL AND NOT ...`;
- `geo_region_exclude` (`_region_leaf`), in its anti-join form at the top of a statement and its row form;
- `embedding_polygon_exclude`: a point on the map outside the lasso (one probe where a sample has one point on it),
  or for a patch-scale map a point (`_points_sql`) and none inside;
- `concept_exclude`: a score that doesn't pass;
- the broad scatter's rejected set (routers/embeddings.py `_scatter_rejections`).

An empty list written in a field is a value: it says the list was looked at and holds nothing. A list kept in a column
of its own (`tags`, `@local.tags`, a registered array field) is another matter: every sample holds the column, the
empty list until one is written, so its empty list can't be told from one never set, and is no value. So:
- ('value', field): a value of the field, the rows `is not null` shows: what the compiler's `_value_sql` says, the one
  place that knows it for each kind of list (there and not a JSON null; for a list kept in a column of its own at
  least one element; for a list of labels, `detections.label`, a list that is empty or holds a label).
- ('elements', field): the field is a list, empty or not (an element match, `match_elements`).
- ('location',): the sample's own location (a place: `location within`, a drawn polygon, `geo_near`).
- ('points', field): a point on that embedding field's map (a lasso).
- ('score', concept id): a score for the concept.

`is empty` asks whether a field's value is a list that holds nothing, and `is not empty` is its NOT: a test of the
value, so a NOT around it needs a value. On a list kept in a column of its own, whose empty list is no value, they are
`is null` and `is not null` (EMPTINESS), and add nothing.
"""

from typing import Any, Protocol, TypeAlias

Need: TypeAlias = tuple

# The negations and the comparison each is the NOT of.
NEGATIONS = {'!=': '=', 'not in': 'in'}
# Taken on a list of labels only (a label path, `segmentation.labels`); the compiler refuses them elsewhere.
LABEL_NEGATIONS = {'<>': '=', 'not_in': 'in', 'nin': 'in'}
# The exclusions and the test each is the NOT of.
EXCLUSIONS = {
    'geo_polygon_exclude': 'geo_polygon',
    'geo_region_exclude': 'geo_region',
    'embedding_polygon_exclude': 'embedding_polygon',
}
COMPARISONS = {
    '=',
    '==',
    '!=',
    '<>',
    'in',
    'not in',
    'not_in',
    'nin',
    '<',
    '<=',
    '>',
    '>=',
    'contains',
    'match_elements',
    'elem_match',
}
# A condition on each element of a list: met by one of them, so its NOT needs the list (an empty one included).
ELEMENT_MATCHES = {'match_elements', 'elem_match'}
# `is empty` and `is not empty`, and what each is on a list kept in a column of its own (its empty list is no value).
EMPTINESS = {'is_empty': 'is_null', 'is_not_empty': 'is_not_null'}
PLACES = {'geo_polygon', 'geo_polygon_exclude', 'geo_region', 'geo_region_exclude', 'geo_near'}
LASSOS = {'embedding_polygon', 'embedding_polygon_exclude'}
CONCEPTS = {'concept_select', 'concept_exclude'}


class Fields(Protocol):
    """What `needs` asks of the compiler about a dataset's fields."""

    def builtin(self, field: Any) -> bool:
        """A field every row has a value for (`id`, `filepath`, `media_type`, `created_at`, `last_modified_at`)."""
        ...

    def filters_nothing(self, node: Any) -> bool:
        """A test that compares nothing: a range with no value, a place or a lasso whose value is no polygon (or, for
        `geo_near`, no point)."""
        ...

    def row_field(self, field: Any) -> Any:
        """'builtin' for a column every row has, 'list' for a list kept in a column of its own (`tags`, `@local.tags`,
        a registered array field), else None."""
        ...


def needs(node: Any, fields: Fields) -> list[Need]:
    """What `NOT (node)` needs a row to have a value for: every field compared inside it, nested NOTs included. An
    absence test (`is null`, `is not null`, `exists`, `not exists`, and `is empty` / `is not empty` of a list kept in a
    column of its own) adds nothing, nor does a test of a built-in field, a selection, the duplicates' collapse or a
    wrapping operator (it reaches across on its own)."""
    found: dict[Need, None] = {}

    def walk(item: Any) -> None:
        if not isinstance(item, dict):
            return
        if 'logic' in item or 'filters' in item:
            children = item.get('filters')
            for child in children if isinstance(children, list) else []:
                walk(child)
            return
        operator, field, value = str(item.get('operator', '=')).lower(), item.get('field'), item.get('value')
        if fields.filters_nothing(item):
            return
        if operator in CONCEPTS:
            concept = value.get('concept_id') if isinstance(value, dict) else None
            if isinstance(concept, (int, str)) and not isinstance(concept, bool):
                found[('score', concept)] = None
        elif operator in PLACES:
            found[('location',)] = None
        elif operator in LASSOS:
            if isinstance(field, str):
                found[('points', field)] = None
        elif operator in ELEMENT_MATCHES:
            if isinstance(field, str):
                found[('elements', field)] = None
        elif operator in COMPARISONS and isinstance(field, str) and not fields.builtin(field):
            found[('value', field)] = None
        elif operator in EMPTINESS and isinstance(field, str) and fields.row_field(field) is None:
            found[('value', field)] = None

    walk(node)
    return list(found)
