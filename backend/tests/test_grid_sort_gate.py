"""Gate for the filtered-grid ORDER BY flip (see _filter_first_id_order).

With the default `ORDER BY id LIMIT n`, a selective filter whose matches sit late in id
order makes the planner walk the pkey through millions of non-matches; flipping to
`ORDER BY id + 0` resolves the filter through its own index first. The flip must only
fire for selective filters — broad ones are dense in id order and the pkey walk wins.
"""

from backend.routers.datasets import _filter_first_id_order


def test_selective_filter_flips():
    assert _filter_first_id_order(True, 396_516, 2_700_000) is True


def test_broad_filter_keeps_pkey_walk():
    assert _filter_first_id_order(True, 2_400_000, 2_700_000) is False
    assert _filter_first_id_order(True, 1_350_000, 2_700_000) is False  # exactly half


def test_no_filter_never_flips():
    assert _filter_first_id_order(False, 100, 2_700_000) is False


def test_unknown_total_or_dataset_size_never_flips():
    assert _filter_first_id_order(True, -1, 2_700_000) is False  # skip_count
    assert _filter_first_id_order(True, 0, 2_700_000) is False  # empty result
    assert _filter_first_id_order(True, 100, None) is False  # no stats cache
    assert _filter_first_id_order(True, 100, 0) is False
