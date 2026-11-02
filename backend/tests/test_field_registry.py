"""Registration is the only type authority, regardless of later batches or cache contents."""

import pytest

from backend.field_registry import FIELD_TYPES, canonical_type, infer_fields, presentation_type, validate_metadata


@pytest.mark.parametrize(
    'values, expected',
    [
        ([None], 'unknown'),
        ([True, False], 'boolean'),
        ([1, 2.5], 'number'),
        (['2026-01-01', '2026-04-02T12:00:00Z'], 'datetime'),
        (['2026-01-01', 'not a date'], 'string'),
        (['2026-99-99'], 'string'),
        ([1, '1'], 'string'),
        ([False, 1], 'string'),
        ([{}, []], 'string'),
        ([[1], [2]], 'list'),
        ([{'label': 'a'}], 'object'),
        ([None, 3], 'number'),
    ],
)
def test_first_write_infers_all_values_independent_of_order(values, expected):
    assert infer_fields({'field': value} for value in values) == {'field': expected}
    assert infer_fields({'field': value} for value in reversed(values)) == {'field': expected}


@pytest.mark.parametrize(
    'legacy, expected',
    [
        ('int', 'number'),
        ('float', 'number'),
        ('integer', 'number'),
        ('bool', 'boolean'),
        ('date', 'datetime'),
        ('array', 'list'),
        ('dict', 'object'),
        ('enum', 'string'),
    ],
)
def test_canonical_vocabulary(legacy, expected):
    assert canonical_type(legacy) == expected
    assert all(canonical_type(kind) == kind for kind in FIELD_TYPES)


@pytest.mark.parametrize(
    'complete, promoted, expected',
    [
        (False, False, 'enum'),
        (True, False, 'enum'),
        (False, True, 'enum'),
    ],
)
def test_string_value_pickers_remain_exact_when_previews_are_incomplete(complete, promoted, expected):
    assert (
        presentation_type(
            {
                'field_type': 'string',
                'unique_values': ['first'],
                'values_complete': complete,
                'is_promoted': promoted,
            },
        )
        == expected
    )


def test_list_never_implies_native_storage():
    assert presentation_type({'field_type': 'list', 'origin': 'metadata'}) == 'list'
    assert presentation_type({'field_type': 'list', 'origin': 'native_array'}) == 'array'


@pytest.mark.parametrize('number', [float('nan'), float('inf'), -float('inf'), 10**400])
def test_invalid_numbers_rejected_recursively(number):
    with pytest.raises(ValueError, match=r'metadata nested\[0\].score'):
        validate_metadata({'nested': [{'score': number}]})


def test_system_names_not_registered_as_metadata():
    assert infer_fields([{'tags': 'a', 'media_type': 'image', 'detections.label': 'cat'}]) == {}
