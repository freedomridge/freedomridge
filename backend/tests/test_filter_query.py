"""The filter query language (backend/filter_query.py, docs/FILTER_QUERY.md): text to filters, back again,
and errors that say where and what to write instead."""

import pytest

from backend.filter_query import BOOLEAN, DATETIME, LIST, NUMBER, OBJECT, TEXT, FilterQueryError, format_filter
from backend.filter_query import parse_query as parse

FIELDS = {
    'id': NUMBER,
    'serial_number': TEXT,
    'serial': NUMBER,
    'split': TEXT,
    'score': NUMBER,
    'note': TEXT,
    'ok': BOOLEAN,
    'tags': LIST,
    'created_at': DATETIME,
    'detections': OBJECT,
    'detections.label': TEXT,
    'odd name': TEXT,
    'in': TEXT,
}
SERIALS = '''
103000752  103001025  103004080
103004120,103004128
'''


def leaf(field, operator, value=None):
    return {'field': field, 'operator': operator, 'value': value}


def test_a_pasted_list_with_any_separators_is_one_in_filter():
    for field, value in (('serial_number', '103000752'), ('serial', 103000752)):
        [node] = parse(f'{field} in ({SERIALS})', FIELDS)
        assert node['operator'] == 'in' and len(node['value']) == 5 and node['value'][0] == value
    # A JSON list pasted as is works too, and a text field keeps leading zeros.
    assert parse('serial_number in ["007", 8]', FIELDS) == [leaf('serial_number', 'in', ['007', '8'])]


def test_each_top_level_and_part_is_one_filter_and_brackets_keep_a_group_together():
    assert parse('split = train AND score >= 0.5\nAND NOT (note exists OR ok = true)', FIELDS) == [
        leaf('split', '=', 'train'),
        leaf('score', '>=', 0.5),
        {'logic': 'NOT', 'filters': [{'logic': 'OR', 'filters': [leaf('note', 'exists'), leaf('ok', '=', True)]}]},
    ]
    # AND binds tighter than OR; a bracketed AND stays one part.
    assert parse('split = a OR split = b AND score > 1', FIELDS) == [
        {
            'logic': 'OR',
            'filters': [
                leaf('split', '=', 'a'),
                {
                    'logic': 'AND',
                    'filters': [
                        leaf('split', '=', 'b'),
                        leaf('score', '>', 1),
                    ],
                },
            ],
        },
    ]
    assert parse('(split = a AND score > 1) AND note is null', FIELDS) == [
        {'logic': 'AND', 'filters': [leaf('split', '=', 'a'), leaf('score', '>', 1)]},
        leaf('note', 'is_null'),
    ]


def test_every_operator_and_value_form():
    query = '''
        # comments run to the end of the line
        score == 1 and score != 2 and score <> 3 and score > -1.5 and score < 1e3 and score <= +2
        and split contains "a \\"quoted\\" b" and tags in (x) and tags not in ('y')
        and note not exists and note is not null and detections exists
        and created_at >= 2024-01-01T00:00:00Z and ok != false
        and `odd name` = 'x' and `in` = y and detections.label in (car "fire truck")
    '''
    assert [(node['field'], node['operator'], node['value']) for node in parse(query, FIELDS)] == [
        ('score', '=', 1),
        ('score', '!=', 2),
        ('score', '!=', 3),
        ('score', '>', -1.5),
        ('score', '<', 1000.0),
        ('score', '<=', 2),
        ('split', 'contains', 'a "quoted" b'),
        ('tags', 'in', ['x']),
        ('tags', 'not in', ['y']),
        ('note', 'not_exists', None),
        ('note', 'is_not_null', None),
        ('detections', 'exists', None),
        ('created_at', '>=', '2024-01-01T00:00:00Z'),
        ('ok', '!=', False),
        ('odd name', '=', 'x'),
        ('in', '=', 'y'),
        ('detections.label', 'in', ['car', 'fire truck']),
    ]
    assert parse('', FIELDS) == [] and parse('  # nothing\n', FIELDS) == []
    assert parse('id in (12345678901234567890)', FIELDS)[0]['value'] == [12345678901234567890]  # no float rounding


@pytest.mark.parametrize(
    ('query', 'line', 'column', 'message', 'suggestions'),
    [
        ('splt = a', 1, 1, "unknown field 'splt'", ['split']),
        ('Split = a', 1, 1, "unknown field 'Split'", ['split']),
        ('split = a\nAND score > abc', 2, 13, "'abc' is not a number, and 'score' is a number field", []),
        ('split > 1', 1, 7, "'>' does not apply to the text field 'split'; use one of: =, !=, in,", []),
        ('detections = 1', 1, 12, "'=' does not apply to the object field 'detections'", []),
        ('ok = maybe', 1, 6, "'ok' is true or false, not 'maybe'", []),
        ('serial in ()', 1, 11, 'a list needs at least one value', []),
        ('serial in (1 2', 1, 11, 'this list has no closing )', []),
        ('serial in 1', 1, 11, 'expected a list after in', []),
        ('(split = a', 1, 11, 'expected ) to close the ( at line 1, column 1', []),
        ('split = a)', 1, 10, 'expected AND, OR or the end of the query, found \')\' (a closing bracket', []),
        ('split = "a', 1, 9, 'this " has no closing " on its line', []),
        ('split ! a', 1, 7, "'!' on its own isn't an operator", []),
        ('split', 1, 6, "expected an operator after 'split'", []),
        ('note is nothing', 1, 9, "expected null or not null after is, found 'nothing'", []),
        ('split = and', 1, 9, "expected a value for 'split', found 'and'", []),
        ('AND split = a', 1, 1, "expected a field name, found 'AND'", []),
        ('split = a split = b', 1, 11, "expected AND, OR or the end of the query, found 'split'", []),
        ('score between 1 2', 1, 17, "expected AND in score between ... AND ..., found '2'", []),
        ('split between 1 and 2', 1, 7, "'between' does not apply to the text field 'split'", []),
        ('split like "a%b"', 1, 12, "like only takes '%text%' (contains text); use contains", []),
        ('created_at >= abc', 1, 15, "'abc' is not a date and time; 'created_at' takes ISO", []),
        ('(' * 101 + 'split = a' + ')' * 101, 1, 101, 'brackets and NOTs nest at most 100 deep', []),
        ('NOT ' * 101 + 'split = a', 1, 401, 'brackets and NOTs nest at most 100 deep', []),
        (' AND '.join(['score > 1'] * 501), 1, 7001, 'a query takes at most 500 comparisons; put many values', []),
    ],
)
def test_a_mistake_says_where_and_what_to_write(query, line, column, message, suggestions):
    with pytest.raises(FilterQueryError) as raised:
        parse(query, FIELDS)
    detail = raised.value.detail
    assert (detail['line'], detail['column']) == (line, column), detail
    assert detail['message'].startswith(message), detail
    assert detail['suggestions'] == suggestions
    assert str(raised.value).startswith(f'line {line}, column {column}: ')


ROUND_TRIP = [
    leaf('serial_number', 'in', ['103000752', '007']),
    leaf('serial', 'not in', [1, 2.5]),
    leaf('split', '=', 'train'),
    leaf('split', 'contains', 'line\nbreak "and" quotes \\ \u0001'),
    leaf('split', '=', 'a\r\nb\bc\fd/e\tf'),  # CRLF text from a CSV import, and every escape json.dumps writes
    leaf('note', 'is_null'),
    leaf('ok', '=', True),
    leaf('odd name', '!=', 'x'),
    leaf('in', '=', 'keyword field'),
    {'logic': 'NOT', 'filters': [leaf('score', '<', 0.2)]},
    {'logic': 'AND', 'filters': [leaf('score', '>=', 1), leaf('score', '<=', 2)]},
    {
        'logic': 'OR',
        'filters': [
            leaf('split', '=', 'a'),
            {'logic': 'AND', 'filters': [leaf('split', '=', 'b'), leaf('tags', 'contains', 'x')]},
        ],
    },
]


@pytest.mark.parametrize('node', ROUND_TRIP)
def test_a_filter_survives_being_written_out_and_read_back(node):
    text = format_filter(node)
    assert text is not None
    assert parse(text, FIELDS) == [node]


def test_the_limits_leave_room_for_real_queries():
    # 100 levels of brackets, 500 comparisons, and a pasted list of any length (one comparison).
    assert parse('(' * 100 + 'split = a' + ')' * 100, FIELDS) == [leaf('split', '=', 'a')]
    assert len(parse(' AND '.join(['score > 1'] * 500), FIELDS)) == 500
    serials = ' '.join(str(103000000 + i) for i in range(20000))
    assert len(parse(f'serial in ({serials})', FIELDS)[0]['value']) == 20000
    assert parse('created_at >= 2026-01-31 AND created_at < "2026-02-01T00:00:00+01:00"', FIELDS)[0]['value'] == (
        '2026-01-31'
    )


def test_the_sdks_older_between_and_like_still_work():
    assert parse('score BETWEEN 0.3 AND 0.8 AND split LIKE "%img_00%"', FIELDS) == [
        {'logic': 'AND', 'filters': [leaf('score', '>=', 0.3), leaf('score', '<=', 0.8)]},
        leaf('split', 'contains', 'img_00'),
    ]


def test_formatting_writes_one_part_per_filter_and_leaves_out_what_it_cannot_express():
    assert format_filter(leaf('split', '==', 'a')) == 'split = "a"'
    assert format_filter(leaf('tags', 'nin', 'x')) == 'tags not in ("x")'
    assert format_filter({'logic': 'AND', 'filters': [leaf('split', '=', 'a')]}) == 'split = "a"'
    assert format_filter({'logic': 'AND', 'filters': [leaf('a', '=', 1), leaf('b', '=', 2)]}) == '(a = 1 AND b = 2)'
    concept = {'field': '_concept', 'operator': 'concept_exclude', 'value': {'concept_id': 3, 'threshold': 0.9}}
    region = {'field': 'location', 'operator': 'geo_polygon', 'value': [[0, 0], [1, 0], [1, 1]]}
    for node in (
        concept,
        region,
        {'field': 'id', 'operator': 'in_selection', 'value': 'token'},
        leaf('split', '=', None),
        leaf('split', '=', {'nested': 1}),
        leaf('score', '=', float('nan')),
        {'logic': 'AND', 'filters': []},
        {'logic': 'OR', 'filters': [leaf('split', '=', 'a'), concept]},
    ):
        assert format_filter(node) is None, node
