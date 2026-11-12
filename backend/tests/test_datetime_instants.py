"""The datetime rule (backend/datetimes.py) in Python, and the SQL function's definition pinned. The SQL forms are
tested against the same corpus in test_datetime_instants_postgres.py."""

import datetime
import hashlib
import re

import pytest

from backend import datetimes
from backend.datetimes import parse_instant
from backend.field_registry import infer_fields, value_type
from backend.schema_check import MIGRATIONS_DIR, SCHEMA_SQL, function_statements
from backend.tests.datetime_corpus import CASES, corpus

# metadata_instant_v1 must NEVER change once an index uses it: PostgreSQL doesn't rebuild an index when a function is
# replaced, and apply_migrations.sh re-applies functions.sql on every run, so a changed body would silently leave every
# datetime index out of step with the queries that read it. A different rule is a new function, metadata_instant_v2,
# with the indexes rebuilt on it. Don't update this hash to make the test pass.
METADATA_INSTANT_V1_SHA256 = '83e2f1383bfa49cecd976e615646fa32afe91ce2e2b1005d25de97521ca86863'
# The grammar without its ranges, at most six fraction digits: what the shape lookup in instant_sql must accept.
_SHAPED = re.compile(
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)?)?',
)


@pytest.fixture(scope='module')
def texts():
    return corpus()


def instant(expected):
    return None if expected is None else datetime.datetime.fromisoformat(expected.replace('Z', '+00:00'))


@pytest.mark.parametrize('text, expected', CASES)
def test_cases(text, expected):
    assert parse_instant(text) == instant(expected)


@pytest.mark.parametrize('value', [None, 20260101, 2026.0, True, ['2026-01-01'], {'at': '2026-01-01'}])
def test_only_text_has_an_instant(value):
    assert parse_instant(value) is None


def test_python_range_ends_have_no_instant_in_python():
    # SQL has them (1 BC, year 10000); ingest only needs to know they are datetimes in the grammar.
    assert parse_instant('0001-01-01T00:00+01:00') is None
    assert parse_instant('9999-12-31T23:59-00:01') is None
    assert parse_instant('0001-01-01T00:00-01:00') == datetime.datetime(1, 1, 1, 1, tzinfo=datetime.timezone.utc)


def _from_its_numbers(text):
    """The instant a grammar text names, computed from its digits the way metadata_instant_v1 does (parse_instant
    leaves that to Python's ISO parser): an independent reading."""
    if not datetimes._GRAMMAR.fullmatch(text):
        return None
    sign = max(text.find('+', 16), text.find('-', 16))  # the offset's sign, after the minutes
    clock, zone = (text[11:sign], text[sign:].replace(':', '')) if sign > 0 else (text[11:].removesuffix('Z'), '')
    try:
        local = datetime.datetime(
            int(text[0:4]),
            int(text[5:7]),
            int(text[8:10]),
            int(clock[0:2] or 0),
            int(clock[3:5] or 0),
            int(clock[6:8] or 0),
            int(clock[9:15].ljust(6, '0')),
        )
        offset = datetime.timedelta(hours=int(zone[1:3] or 0), minutes=int(zone[3:5] or 0))
        return (local + offset if zone.startswith('-') else local - offset).replace(tzinfo=datetime.timezone.utc)
    except (ValueError, OverflowError):
        return None


def test_accepted_text_is_the_instant_its_numbers_name(texts):
    accepted = 0
    for text in texts:
        parsed = parse_instant(text)
        assert parsed == _from_its_numbers(text), text
        accepted += parsed is not None
    assert accepted > 100_000


def test_instant_sql_shapes_are_the_grammar(texts):
    """instant_sql trusts the cast only for text whose translated shape is in its list, and hands text with more than
    six fraction digits to metadata_instant_v1. Both must cover the grammar exactly."""
    shape = str.maketrans('0123456789 ', '9999999999T')
    shapes = set(datetimes._SHAPES)
    assert len(shapes) == 65
    for text in texts:
        assert (text.translate(shape) in shapes) == bool(_SHAPED.fullmatch(text)), text
        if parse_instant(text) is not None and not _SHAPED.fullmatch(text):
            assert len(text) >= 27 and text[19] == '.', text  # its LIKE '___________________._______%'


def test_instant_sql_falls_back_to_the_indexed_function():
    sql = datetimes.instant_sql("metadata->>'captured_at'")
    assert "metadata_instant_v1(metadata->>'captured_at')" in sql
    assert datetimes.indexed_instant_sql('_value') == 'metadata_instant_v1(_value)'


@pytest.mark.parametrize(
    'value, kind',
    [
        ('2026-01-01', 'datetime'),
        ('2026-01-01 10:00', 'datetime'),
        ('2026-01-01T10:00:00Z', 'datetime'),
        ('2026-01-01T10:00:00+0530', 'datetime'),
        ('2026-01-01T10:00:00.123456789-05:00', 'datetime'),
        # Outside the grammar, though Python's own parser reads them (some only on recent versions).
        ('2026-01-01T10', 'string'),
        ('2026-01-01T1000', 'string'),
        ('2026-01-01T24:00:00', 'string'),
        ('2026-01-01T10:00:00,5', 'string'),
        ('2026-01-01T10:00:00+02:00:30', 'string'),
        ('2026-02-30', 'string'),
    ],
)
def test_new_fields_are_datetimes_only_in_the_grammar(value, kind):
    assert value_type(value) == kind
    assert infer_fields([{'captured_at': value}, {'captured_at': '2026-01-02'}]) == {'captured_at': kind}


def _definition(text):
    """metadata_instant_v1's comment and CREATE statement in a SQL file."""
    start = text.index("-- The instant a metadata datetime's text names")
    return text[start : text.index('$$;\n', start) + len('$$;\n')]


def test_metadata_instant_v1_never_changes():
    (statement,) = [s for s in function_statements() if 'FUNCTION metadata_instant_v1(' in s]
    assert hashlib.sha256(statement.encode()).hexdigest() == METADATA_INSTANT_V1_SHA256


def test_the_migration_creates_the_function_schema_sql_defines():
    migration = (MIGRATIONS_DIR / '0031_metadata_instant.sql').read_text()
    assert _definition(migration) == _definition(SCHEMA_SQL.read_text())
