"""Sparse-sample returns distinct samples and refuses empty requests.

Farthest-point sampling never excluded already-chosen points, so degenerate features (every
candidate at one position) returned the same id k times — "Selected 5 samples" was one sample —
and count/percentage <= 0 each silently returned one sample.
"""

import numpy as np
import pytest
from pydantic import ValidationError

from backend.routers.sampling import SparseSampleRequest, farthest_point_sampling


def test_degenerate_features_still_give_k_distinct_points():
    picked = farthest_point_sampling(np.zeros((24, 3)), 5)
    assert len(picked) == 5 and len(set(picked.tolist())) == 5


def test_duplicates_are_exhausted_before_any_point_repeats():
    feats = np.array([[0.0], [0.0], [10.0], [10.0], [5.0]])
    picked = farthest_point_sampling(feats, 5)
    assert sorted(picked.tolist()) == [0, 1, 2, 3, 4]


def test_spread_points_still_come_first():
    feats = np.array([[0.0], [0.1], [0.2], [10.0], [-10.0]])
    picked = farthest_point_sampling(feats, 3).tolist()
    assert {3, 4} <= set(picked)  # the two extremes are picked before the clustered middle


@pytest.mark.parametrize(
    "kw",
    [{"count": 0}, {"count": -5}, {"percentage": 0}, {"percentage": -10}, {"percentage": 101}],
)
def test_empty_or_impossible_requests_are_refused(kw):
    with pytest.raises(ValidationError):
        SparseSampleRequest(fields=["clip"], **kw)
    assert SparseSampleRequest(fields=["clip"], count=1).count == 1
