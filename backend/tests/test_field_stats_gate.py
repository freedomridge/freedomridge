"""Field-stats selectivity gate — the fix that makes Simple Filters reflect the active global filter.

The old gate (`recompute_filtered and (is_promoted or not lightning)`) showed UNFILTERED min/max for
the datetime/number sliders, so adjusting one and hitting Apply WIDENED the global filter (a datetime
lower bound snapping back to the dataset minimum). These lock the corrected decision:
  - a small/selective filtered set recomputes ANY field's MIN/MAX ("at a lower size, do all fields");
  - an indexed field's MIN/MAX recomputes even on a broad set (an index probe is cheap);
  - a broad set with a non-indexed field is capped (the ~12s-per-field DISTINCT freeze);
  - DISTINCT values stay small-set-only even for indexed fields.
"""

from backend.routers.datasets import _field_stats_filter_decision as decide


def test_selective_set_recomputes_all_fields():
    # not broad (small/selective set, or lightning off) -> filter BOTH min/max and distinct for ANY
    # field, including non-indexed ones. This is the core "simple filter reflects the global filter" fix.
    assert decide(filter_active=True, filtered_broad=False, is_promoted=False) == (True, True)
    assert decide(filter_active=True, filtered_broad=False, is_promoted=True) == (True, True)


def test_broad_set_filters_indexed_minmax_only():
    # broad + indexed -> MIN/MAX recomputed (index probe is cheap even when broad); DISTINCT capped.
    assert decide(filter_active=True, filtered_broad=True, is_promoted=True) == (True, False)


def test_broad_set_non_indexed_is_capped():
    # broad + non-indexed -> recompute NOTHING (serve cached) — the freeze the cap exists to prevent.
    assert decide(filter_active=True, filtered_broad=True, is_promoted=False) == (False, False)


def test_no_active_filter_recomputes_nothing():
    # No active filter -> always cached, regardless of index status.
    assert decide(filter_active=False, filtered_broad=False, is_promoted=True) == (False, False)
    assert decide(filter_active=False, filtered_broad=False, is_promoted=False) == (False, False)
