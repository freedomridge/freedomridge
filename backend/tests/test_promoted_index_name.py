"""Promoted-field index names must never exceed Postgres' 63-char identifier limit.

A field name can be up to 128 chars, so the historical idx_samples_meta_{id}_{field}
could overflow and be silently truncated — which desynced promote/verify/demote (they
reconstructed the untruncated name and no longer matched the real index, 500ing the
promote and orphaning the index on demote) and could collide two long field names onto
one index. _promoted_index_name must stay <= 63, keep short names verbatim (so existing
indexes still match with no migration), and be deterministic + collision-free.
"""

from backend.routers.datasets import _MAX_IDENTIFIER_LEN
from backend.routers.datasets import _promoted_index_name as name


def test_short_field_keeps_legacy_name():
    # Back-compat: every existing promoted index was created under this exact form.
    assert name(7, "country") == "idx_samples_meta_7_country"
    assert name(42, "snapshot_trigger_name") == "idx_samples_meta_42_snapshot_trigger_name"


def test_long_field_stays_within_identifier_limit():
    for field in ("a" * 70, "x" * 128, "field_" + "y" * 120):
        assert len(name(7, field)) <= _MAX_IDENTIFIER_LEN


def test_deterministic_so_promote_and_demote_agree():
    long = "z" * 90
    assert name(7, long) == name(7, long)


def test_distinct_long_fields_get_distinct_names():
    # Two long names that share a truncated prefix must not collide onto one index.
    a, b = "a" * 80, "a" * 79 + "b"
    assert name(7, a) != name(7, b)
    # Same field name, different datasets, also distinct.
    assert name(7, a) != name(8, a)
