"""Batch ingest must never downgrade a promoted field type.

refresh_stale_datasets_stats() promotes ISO-datetime string fields to 'datetime' (and
label fields to 'enum') in dataset_field_stats. add_samples re-infers types from raw
batch values — which can only yield string/bool/int/float/list/dict — and used to write
that inferred type back on every batch, resetting 'datetime' to 'string' (and wiping the
[min, max] bounds stored in unique_values) on every sync run. The datetime filter then
rendered as a free-text box until the next FULL stats refresh, at most once per 24h;
incremental refreshes never re-promote.

Covers _merge_batch_field_stats, the pure merge step of the add_samples stats upsert.
"""

from backend.field_registry import infer_fields
from backend.routers.datasets import _merge_batch_field_stats


def _batch(values, type_='string', min_=None, max_=None):
    return {'type': type_, 'min': min_, 'max': max_, 'unique_values': set(values), 'count': len(values)}


def _row(field_type, unique_values=None, min_value=None, max_value=None):
    return {
        'field_type': field_type,
        'unique_values': unique_values,
        'min_value': min_value,
        'max_value': max_value,
    }


def test_datetime_type_preserved_and_bounds_extended():
    current = _row('datetime', unique_values=['2026-01-05T08:00:00', '2026-06-01T12:00:00'])
    batch = _batch({'2025-12-25T00:00:00', '2026-07-06T20:17:00'})
    field_type, _, _, uniques = _merge_batch_field_stats(batch, current)
    assert field_type == 'datetime'
    assert uniques == ['2025-12-25T00:00:00.000000Z', '2026-07-06T20:17:00.000000Z']


def test_datetime_bounds_stay_two_elements_on_large_batch():
    # >100 distinct raw values must NOT null out unique_values — for datetime it holds
    # [min, max], not the value set.
    current = _row('datetime', unique_values=['2026-01-01T00:00:00', '2026-01-02T00:00:00'])
    batch = _batch({f'2026-03-01T00:00:{s:02d}.{ms:03d}' for s in range(60) for ms in range(3)})
    field_type, _, _, uniques = _merge_batch_field_stats(batch, current)
    assert field_type == 'datetime'
    assert uniques == ['2026-01-01T00:00:00.000000Z', '2026-03-01T00:00:59.002000Z']


def test_datetime_survives_previously_wiped_bounds():
    # A row clobbered by the old code path can have unique_values = NULL; the merge
    # must still keep the type and rebuild bounds from the batch.
    current = _row('datetime', unique_values=None)
    batch = _batch({'2026-05-01T00:00:00', '2026-05-02T00:00:00'})
    field_type, _, _, uniques = _merge_batch_field_stats(batch, current)
    assert field_type == 'datetime'
    assert uniques == ['2026-05-01T00:00:00.000000Z', '2026-05-02T00:00:00.000000Z']


def test_datetime_bounds_are_the_earliest_and_latest_instants():
    # Text order isn't time order with offsets: 10:00+02:00 is 08:00 UTC, before 09:00Z. Junk has no say.
    current = _row('datetime', unique_values=['2026-01-01T09:00:00Z', '2026-01-01T09:30:00Z'])
    batch = _batch({'2026-01-01T10:00:00+02:00', 'unknown', '2026-02-30', '2026-01-01T05:00:00-05:00'})
    _, _, _, uniques = _merge_batch_field_stats(batch, current)
    assert uniques == ['2026-01-01T08:00:00.000000Z', '2026-01-01T10:00:00.000000Z']


def test_enum_type_preserved_with_merged_values():
    current = _row('enum', unique_values=['CAR', 'TRUCK'])
    batch = _batch({'BUS'})
    field_type, _, _, uniques = _merge_batch_field_stats(batch, current)
    assert field_type == 'enum'
    assert sorted(uniques) == ['BUS', 'CAR', 'TRUCK']


def test_plain_string_merge_unchanged():
    current = _row('string', unique_values=['a', 'b'])
    batch = _batch({'b', 'c'})
    field_type, _, _, uniques = _merge_batch_field_stats(batch, current)
    assert field_type == 'string'
    assert sorted(uniques) == ['a', 'b', 'c']


def test_plain_string_truncates_over_100_uniques():
    current = _row('string', unique_values=['a'])
    batch = _batch({f'v{i}' for i in range(120)})
    field_type, _, _, uniques = _merge_batch_field_stats(batch, current)
    assert field_type == 'string'
    assert uniques is None


def test_new_field_uses_inferred_type():
    field_type, new_min, new_max, _ = _merge_batch_field_stats(_batch(set(), type_='float', min_=1.5, max_=9.0), None)
    assert (field_type, new_min, new_max) == ('float', 1.5, 9.0)


def test_numeric_minmax_merge_unchanged():
    current = _row('float', min_value=0.5, max_value=4.0)
    _, new_min, new_max, _ = _merge_batch_field_stats(_batch(set(), type_='float', min_=1.0, max_=9.0), current)
    assert (new_min, new_max) == (0.5, 9.0)


def test_new_field_all_iso_values_is_datetime_at_first_sight():
    # No existing row + every batch value is an ISO datetime → typed 'datetime'
    # immediately (bounds = [min, max]), not 'string'-until-the-next-full-refresh.
    batch = _batch({'2026-07-09T08:00:00', '2025-01-01T00:00:00'})
    batch['type'] = infer_fields({'field': value} for value in batch['unique_values'])['field']
    field_type, _, _, uniques = _merge_batch_field_stats(batch, None)
    assert field_type == 'datetime'
    assert uniques == ['2025-01-01T00:00:00.000000Z', '2026-07-09T08:00:00.000000Z']


def test_new_field_date_only_values_are_datetime():
    batch = _batch({'2026-07-09', '2026-01-01'})
    batch['type'] = infer_fields({'field': value} for value in batch['unique_values'])['field']
    field_type, _, _, _ = _merge_batch_field_stats(batch, None)
    assert field_type == 'datetime'


def test_new_field_mixed_values_stays_string():
    field_type, _, _, _ = _merge_batch_field_stats(_batch({'2026-07-09T08:00:00', 'not a date'}), None)
    assert field_type == 'string'


def test_existing_string_row_is_not_promoted_by_ingest():
    # Promotion of an EXISTING field stays the full refresh's job — a later batch can't
    # see historical values, so first-sight logic must not fire against a current row.
    current = _row('string', unique_values=['hello'])
    field_type, _, _, _ = _merge_batch_field_stats(_batch({'2026-07-09T08:00:00'}), current)
    assert field_type == 'string'


def test_sdk_serializes_datetime_objects_to_iso():
    # Ergonomics: metadata={"captured_at": datetime.now()} must not crash
    # at request time — it serializes to ISO strings, which first-sight detection then
    # types as 'datetime' on ingest. A naive datetime gets the machine's offset.
    import json
    from datetime import date, datetime

    from fridge.core.sample import Sample

    s = Sample(
        filepath='/x.jpg',
        metadata={'captured_at': datetime(2026, 7, 9, 8, 0), 'day': date(2026, 7, 9), 'nested': [datetime(2026, 1, 1)]},
    )
    meta = json.loads(json.dumps(s.to_dict()))['metadata']
    assert meta['captured_at'] == datetime(2026, 7, 9, 8, 0).astimezone().isoformat()
    assert meta['day'] == '2026-07-09'
    assert meta['nested'] == [datetime(2026, 1, 1).astimezone().isoformat()]
