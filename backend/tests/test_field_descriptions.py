"""The pure parts of field descriptions (backend/field_descriptions.py): how written text is stored, and what a field
carries in the schema from its stored row and the server's defaults."""

import pytest

from backend.field_descriptions import DEFAULTS, DESCRIPTION_LIMIT, UNIT_LIMIT, clean_text, described


@pytest.mark.parametrize(
    'value, expected',
    [
        (None, None),
        ('', None),
        ('   \n\t ', None),
        ('  gain of the sensor  ', 'gain of the sensor'),
        ('first line\r\nsecond line', 'first line\nsecond line'),
        ('a\tb', 'a\tb'),
        ('x' * DESCRIPTION_LIMIT, 'x' * DESCRIPTION_LIMIT),
        ('  ' + 'x' * DESCRIPTION_LIMIT + '  ', 'x' * DESCRIPTION_LIMIT),
    ],
)
def test_a_description_is_trimmed_and_nothing_left_is_none(value, expected):
    assert clean_text(value, DESCRIPTION_LIMIT, 'A description', line_breaks=True) == expected


@pytest.mark.parametrize(
    'value',
    ['x' * (DESCRIPTION_LIMIT + 1), 'bell\x07', 'nul\x00', 'escape\x1b[0m', 'delete\x7f', 'c1\x85next', 'lone \ud800'],
)
def test_a_description_over_the_limit_or_with_a_control_character_is_refused(value):
    with pytest.raises(ValueError):
        clean_text(value, DESCRIPTION_LIMIT, 'A description', line_breaks=True)


@pytest.mark.parametrize('value', ['x' * (UNIT_LIMIT + 1), 'm\ns', 'm\ts', 'm\x00'])
def test_a_unit_takes_no_line_break_tab_or_other_control_character(value):
    with pytest.raises(ValueError):
        clean_text(value, UNIT_LIMIT, 'A unit', line_breaks=False)
    assert clean_text(' ' + 'u' * UNIT_LIMIT + ' ', UNIT_LIMIT, 'A unit', line_breaks=False) == 'u' * UNIT_LIMIT


def test_a_field_nobody_described_has_the_default_or_nothing():
    description, unit = DEFAULTS['tags']
    assert described('tags', None) == {
        'description': description,
        'unit': unit,
        'values_private': False,
        'description_source': 'default',
    }
    for name in ('digital_gain', 'width', 'brightness'):  # named as computed fields or not: none by name
        assert described(name, None) == {
            'description': None,
            'unit': None,
            'values_private': False,
            'description_source': None,
        }, name


def test_what_someone_wrote_replaces_the_default_and_each_falls_back_on_its_own():
    stored = {'description': 'Gain of the sensor', 'unit': None, 'values_private': True}
    assert described('tags', stored) == {
        'description': 'Gain of the sensor',
        'unit': DEFAULTS['tags'][1],
        'values_private': True,
        'description_source': 'set',
    }
    assert described('width', stored) == {
        'description': 'Gain of the sensor',
        'unit': None,
        'values_private': True,
        'description_source': 'set',
    }
    only_unit = {'description': None, 'unit': 'dB', 'values_private': False}
    assert described('tags', only_unit)['description_source'] == 'default'
    assert described('tags', only_unit)['unit'] == 'dB'
    assert described('width', only_unit) == {
        'description': None,
        'unit': 'dB',
        'values_private': False,
        'description_source': None,
    }
    assert described('digital_gain', only_unit) == {
        'description': None,
        'unit': 'dB',
        'values_private': False,
        'description_source': None,
    }


def test_the_defaults_are_one_sentence_within_the_limits():
    assert {'id', 'filepath', 'created_at', 'last_modified_at', 'tags', 'media_type'} <= DEFAULTS.keys()
    for name, (description, unit) in DEFAULTS.items():
        assert clean_text(description, DESCRIPTION_LIMIT, name, line_breaks=False) == description
        assert unit is None or clean_text(unit, UNIT_LIMIT, name, line_breaks=False) == unit
        assert description.endswith('.') and description.count('. ') == 0, name
