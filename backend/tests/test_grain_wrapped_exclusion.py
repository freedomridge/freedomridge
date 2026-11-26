"""An excluded place wrapped as a filter card's Builder tab saves it (the one rule of an AND group) takes the
assistant's image grain as the bare one does (backend/assistant/grain.py): NOT (the image or one of its objects is
inside). A group of two rules, an OR group and a NOT around a place stay filters like the others. The app's withGrain
does the same (frontend/tests/wrapped-region-exclude.test.cjs, and the shared fixture both read)."""

from typing import Any

import pytest

from backend.assistant.grain import image_grain

NOT_KANSAS = {'field': 'location', 'operator': 'geo_region_exclude', 'value': 'US-KS'}
KANSAS = {**NOT_KANSAS, 'operator': 'geo_region'}
POLYGON = {'field': 'location', 'operator': 'geo_polygon_exclude', 'value': [[0, 0], [1, 0], [1, 1]]}
LASSO = {'field': 'clip_img', 'operator': 'embedding_polygon_exclude', 'value': [[0, 0], [1, 0], [1, 1]]}
CROP = {'field': 'crop', 'operator': '=', 'value': 'wheat'}


def wrapped(*filters: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'AND', 'filters': list(filters)}


@pytest.mark.parametrize('rule', [NOT_KANSAS, POLYGON, LASSO], ids=lambda rule: rule['operator'])
def test_a_wrapped_excluded_place_takes_the_grain_of_the_bare_one(rule):
    bare = image_grain([CROP, rule], True)
    assert image_grain([CROP, wrapped(rule)], True) == bare
    assert image_grain([CROP, wrapped(wrapped(rule))], True) == bare
    assert image_grain([wrapped(rule)], True) == image_grain([rule], True)
    # Without objects every stage is sent as it is.
    assert image_grain([wrapped(rule)], False) == {
        'logic': 'AND',
        'filters': [{'field': 'media_type', 'operator': '!=', 'value': 'object'}, wrapped(rule)],
    }


def test_a_wrapped_polygon_of_fewer_than_three_points_excludes_nothing():
    short = {**POLYGON, 'value': [[0, 0], [1, 1]]}
    assert image_grain([CROP, wrapped(short)], True) == image_grain([CROP, short], True) == image_grain([CROP], True)


@pytest.mark.parametrize(
    'stage',
    [wrapped(NOT_KANSAS, CROP), {'logic': 'OR', 'filters': [NOT_KANSAS]}, {'logic': 'NOT', 'filters': [KANSAS]}],
    ids=['two rules', 'an OR of one rule', 'a NOT around a place'],
)
def test_other_groups_holding_a_place_stay_filters_like_the_others(stage):
    images, coupled = image_grain([stage], True)['filters']
    assert images == {'field': 'media_type', 'operator': '!=', 'value': 'object'}
    assert coupled['logic'] == 'OR'
    assert [arm['value'] for arm in coupled['filters']] == [wrapped(stage), wrapped(stage)]
