"""Pure-function tests for sparse-sample feature building (_build_diversity_matrix).

Locks the fix for the reported bug: a sample MISSING a chosen field must be imputed on that field's
axes (diversified on the fields it has), not dropped — so combining fields that don't co-occur (e.g.
an image-grain embedding + an object-grain number) no longer collapses to an empty intersection. Also
locks that weights take effect (they're applied AFTER normalization; before, normalization cancelled
them) and that a row is dropped only when it has NONE of the chosen fields.
"""

import numpy as np

from backend.routers.sampling import _build_diversity_matrix

# field -> aliases; the helper only uses the field NAMES (it reconstructs alias keys), so the alias
# lists just mirror what the endpoint builds.
FIELDS = {"clip": ["clip_x", "clip_y", "meta_clip"], "score": ["score_x", "score_y", "meta_score"]}


def _row(id, clip=None, score=None, loc=None):
    r = {
        "id": id,
        "clip_x": None,
        "clip_y": None,
        "meta_clip": None,
        "score_x": None,
        "score_y": None,
        "meta_score": None,
        "lon": None,
        "lat": None,
    }
    if clip is not None:
        r["clip_x"], r["clip_y"] = clip
    if score is not None:
        r["meta_score"] = score
    if loc is not None:
        r["lon"], r["lat"] = loc
    return r


def test_missing_field_is_imputed_not_dropped():
    # clip is an embedding (x/y); score is numeric (meta only). r3 has score but NOT clip — under the
    # old intersection logic, selecting [clip, score] dropped it. It must now survive.
    rows = [
        _row(1, clip=(0.0, 0.0)),  # has clip, missing score
        _row(2, clip=(1.0, 1.0)),  # has clip, missing score
        _row(3, score="10"),  # has score, missing clip  <-- the cross-grain case
        _row(4),  # has NOTHING -> dropped
    ]
    ids, feats = _build_diversity_matrix(rows, FIELDS, {})
    assert ids == [1, 2, 3], f"row 4 (no fields) dropped; others kept, got {ids}"
    assert feats.shape == (3, 3), "clip(2 dims) + score(1 dim)"
    assert not np.isnan(feats).any(), "missing dims must be imputed, never NaN"


def test_row_with_no_chosen_field_is_dropped():
    ids, feats = _build_diversity_matrix([_row(1), _row(2)], FIELDS, {})
    assert ids == [] and feats is None


def test_field_with_no_usable_values_is_omitted():
    # Only clip has values; score is absent everywhere -> score omitted, clip still works.
    rows = [_row(1, clip=(0.0, 0.0)), _row(2, clip=(1.0, 1.0))]
    ids, feats = _build_diversity_matrix(rows, FIELDS, {})
    assert ids == [1, 2]
    assert feats.shape == (2, 2), "only clip's 2 dims remain"


def test_weights_apply_after_normalization():
    rows = [_row(1, clip=(0.0, 0.0)), _row(2, clip=(1.0, 1.0))]
    base = _build_diversity_matrix(rows, FIELDS, {})[1]
    weighted = _build_diversity_matrix(rows, FIELDS, {"clip": 2.0})[1]
    # Normalized clip spans [0,1]; with weight 2 it must span [0,2] (old code normalized the weight away).
    assert np.isclose(base[:, 0].max(), 1.0)
    assert np.isclose(weighted[:, 0].max(), 2.0)


def test_numeric_only_subset_still_samples():
    # score present on a subset; selecting [clip, score] keeps the score-only rows too.
    rows = [_row(1, clip=(0.1, 0.2)), _row(2, score="5"), _row(3, score="9")]
    ids, feats = _build_diversity_matrix(rows, FIELDS, {})
    assert set(ids) == {1, 2, 3}
    assert feats.shape == (3, 3)
    assert not np.isnan(feats).any()


# location is gated like the other kinds; aliases are [lon, lat] keyed off the field name "location".
LOC_FIELDS = {"location": ["lon", "lat"], "score": ["score_x", "score_y", "meta_score"]}


def test_location_with_no_located_rows_is_gated_out_not_all_nan():
    # The regression: location was added to specs unconditionally. With [location, score] where NO
    # row has a location, the location columns were all-NaN -> NaN matrix -> FPS collapses to one
    # point. Now location must be omitted entirely and score still drives a finite, diverse result.
    rows = [_row(1, score="3"), _row(2, score="7"), _row(3, score="9")]
    ids, feats = _build_diversity_matrix(rows, LOC_FIELDS, {})
    assert set(ids) == {1, 2, 3}
    assert feats.shape == (3, 1), "location gated out (no located rows); only score's 1 dim remains"
    assert not np.isnan(feats).any()


def test_location_combined_with_field_imputes_missing_location():
    # Some rows have location, some don't but have score: all are kept, missing location imputed.
    rows = [_row(1, loc=(10.0, 20.0)), _row(2, score="5"), _row(3, loc=(11.0, 21.0), score="8")]
    ids, feats = _build_diversity_matrix(rows, LOC_FIELDS, {})
    assert set(ids) == {1, 2, 3}
    assert feats.shape == (3, 3), "location(2) + score(1)"
    assert not np.isnan(feats).any()


def test_multifield_weight_alignment():
    # clip (embedding, 2 dims) + score (numeric, 1 dim); weight only score. The weight vector must
    # stay column-aligned across the differing widths: clip cols unweighted, score col x3.
    rows = [_row(1, clip=(0.0, 0.0), score="0"), _row(2, clip=(1.0, 1.0), score="10")]
    _, feats = _build_diversity_matrix(rows, FIELDS, {"score": 3.0})
    assert np.isclose(feats[:, 0].max(), 1.0) and np.isclose(feats[:, 1].max(), 1.0), "clip cols at weight 1"
    assert np.isclose(feats[:, 2].max(), 3.0), "score col (index 2) scaled by 3"


def test_single_row_is_finite():
    ids, feats = _build_diversity_matrix([_row(1, clip=(0.5, 0.5))], FIELDS, {})
    assert ids == [1]
    assert feats.shape == (1, 2) and np.isfinite(feats).all()
