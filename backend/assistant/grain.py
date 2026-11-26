"""A view is of the dataset's images, not its object rows (m1-contract-working-views, section 1, rule 7): the image
grain the app's dataset page shows, applied to a view's stages before every request a working-view tool makes.

This is the frontend's `withGrain(base, 'image', null, hasObjects)` (frontend/contexts/FilterContext.tsx), written
again here: object rows are left out (`media_type != object`), and on a dataset with object rows each filter is
coupled across the grains, so an image passes when it matches itself (`cg_self`) or one of its objects matches
(`has_matching_object`); an excluded region is coupled as NOT (the image or one of its objects is inside it), and a
lasso on the objects' map couples on its own. The stages a view keeps stay as they are: the app wraps them itself
when it opens the view's link. frontend/tests/fixtures/image-grain-cases.json holds cases both sides must reproduce
exactly (frontend/tests/image-grain-cases.test.cjs runs the real withGrain, backend/tests/test_assistant_grain.py
this), so the two can't drift apart.
"""

from typing import Any

_IMAGES = {'field': 'media_type', 'operator': '!=', 'value': 'object'}
_REGION_EXCLUDES = {
    'geo_polygon_exclude': 'geo_polygon',
    'geo_region_exclude': 'geo_region',
    'embedding_polygon_exclude': 'embedding_polygon',
}
_CROSS = 'has_matching_object'


def _sole_rule(node: dict[str, Any]) -> dict[str, Any]:
    """The rule a stage comes down to: itself, or the one rule of an AND group around it, however deeply wrapped (a
    filter card saved from its Builder tab wraps a lone rule in an AND group), as the app's soleRule
    (frontend/lib/filter-query.ts). An OR or a NOT is never seen through."""
    while node.get('logic') == 'AND' and len(node.get('filters') or []) == 1:
        node = node['filters'][0]
    return node


def _coupled(node: dict[str, Any], field: str = '_crossgrain') -> list[dict[str, Any]]:
    return [
        {'field': '_cgself', 'operator': 'cg_self', 'value': node},
        {'field': field, 'operator': _CROSS, 'value': node},
    ]


def image_grain(stages: list[dict[str, Any]], has_objects: bool) -> dict[str, Any]:
    """The filter the app's image grid sends for these stages (each a filter node, ANDed)."""
    if not stages:
        return {'logic': 'AND', 'filters': [dict(_IMAGES)]}
    if not has_objects:
        return {'logic': 'AND', 'filters': [dict(_IMAGES), *stages]}

    def region_exclude(node: dict[str, Any]) -> bool:  # wrapped or not: the same filter and the same chip
        return _sole_rule(node).get('operator') in _REGION_EXCLUDES

    def off_grain_lasso(node: dict[str, Any]) -> bool:  # a lasso on the objects' map (clip_obj)
        return node.get('operator') == 'embedding_polygon' and node.get('field') == 'clip_obj'

    # A polygon of fewer than 3 points is no polygon and excludes nothing; a named region's value is its key.
    excluded = [
        rule
        for rule in (_sole_rule(node) for node in stages if region_exclude(node))
        if rule.get('operator') == 'geo_region_exclude'
        or (isinstance(rule.get('value'), list) and len(rule['value']) >= 3)
    ]
    lassos = [node for node in stages if not region_exclude(node) and off_grain_lasso(node)]
    rest = [node for node in stages if not region_exclude(node) and not off_grain_lasso(node)]
    parts: list[dict[str, Any]] = [dict(_IMAGES)]
    if rest:
        parts.append({'logic': 'OR', 'filters': _coupled({'logic': 'AND', 'filters': rest})})
    for lasso in lassos:
        parts.append({'field': '_crossemb', 'operator': _CROSS, 'value': {'logic': 'AND', 'filters': [lasso]}})
    for region in excluded:
        inside = {'logic': 'AND', 'filters': [{**region, 'operator': _REGION_EXCLUDES[region['operator']]}]}
        parts.append({'logic': 'NOT', 'filters': [{'logic': 'OR', 'filters': _coupled(inside)}]})
    return {'logic': 'AND', 'filters': parts}
