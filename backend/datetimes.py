"""One rule for metadata datetimes: which stored text is a datetime, and the instant it names.

A datetime is ``YYYY-MM-DD``, then optionally ``T`` or a space, ``hh:mm``, an optional ``:ss``, an optional
``.fraction`` (any number of digits; the first six count, the rest are dropped) and an optional offset: ``Z``,
``±hh``, ``±hhmm`` or ``±hh:mm``. No offset is UTC; a date alone is midnight UTC. Nothing else is a datetime: no
year 0000, no day past the end of its month (``2026-02-30``), no hour 24, no leap second ``:60``, no offset past
``±15:59``, no lower-case ``t`` or ``z``, no comma fraction, no hour without minutes (``2026-01-01T10``), no basic
format (``T1000``), no space before the offset and no offset on a date alone. Text that isn't a datetime has no
instant (NULL, ``None``) and never raises.

The rule is written three times and the tests run all three over one corpus
(backend/tests/test_datetime_instants_postgres.py):

- :func:`parse_instant`, in Python: ingest types a new field ``datetime`` only when its values follow the rule.
- ``metadata_instant_v1(text)``, in SQL (backend/db/schema.sql): IMMUTABLE, so a datetime field's index can be built
  on it (:func:`indexed_instant_sql`). Indexes store its answers, so it never changes: a new rule is a new function
  (``metadata_instant_v2``) and the indexes rebuilt on it.
- :func:`instant_sql`, a plain SQL expression on PostgreSQL's own parser, for a field without an index: the same
  answers, at about three fifths of the cost per row.
"""

import datetime
import re
from typing import Any, Optional

# The grammar, with each number's range: month 01-12, day 01-31, hour 00-23, minute and second 00-59, and an offset
# up to 15:59. Year 0000 and a day past the end of its month are refused after the match. ASCII digits only.
_GRAMMAR = re.compile(
    r'[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])'
    r'([T ]([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]+)?)?(Z|[+-](0[0-9]|1[0-5])(:?[0-5][0-9])?)?)?',
)


def parse_instant(text: Any) -> Optional[datetime.datetime]:
    """``metadata_instant_v1`` in Python: the instant ``text`` names, as an aware UTC datetime, or None.

    Also None where the instant falls outside Python's years 1-9999, which SQL can represent: a date within 16 hours
    of either end, with an offset that crosses it (``0001-01-01T00:00+01:00``).
    """
    # Ingest runs this on every text value it sees, so most text is refused before the regex: the grammar's lengths
    # are 10, 16, 17 and 19 on, with dashes at 5 and 8.
    if not isinstance(text, str) or not (len(text) in (10, 16, 17) or len(text) >= 19):
        return None
    if text[4] != '-' or text[7] != '-' or not _GRAMMAR.fullmatch(text):
        return None
    # The grammar decides; Python's ISO parser reads every text in it as the rule does (no offset is UTC, fraction
    # digits past the sixth dropped), checked against the SQL function and an independent reading in the tests.
    try:
        instant = datetime.datetime.fromisoformat(text)
    except ValueError:  # year 0000, or a day past the end of its month
        return None
    if instant.tzinfo is None:
        return instant.replace(tzinfo=datetime.timezone.utc)
    try:
        return instant.astimezone(datetime.timezone.utc)
    except OverflowError:  # outside years 1-9999 once the offset is applied
        return None


UTC = datetime.timezone.utc
# A grammar text's length without its offset, for each unit a value written without a fraction means.
_UNITS = {10: datetime.timedelta(days=1), 16: datetime.timedelta(minutes=1), 19: datetime.timedelta(seconds=1)}


def literal_window(value: Any) -> Optional[tuple[datetime.datetime, Optional[datetime.datetime]]]:
    """A filter value as ``(start, end)``: the instant it names, and the end of the unit it was written to, or None.

    - Text in the grammar: no offset is UTC; written to the day, minute or second with no fraction, ``end`` is where
      the next UTC day, minute or second starts (``2026-01-01`` is that whole UTC day); with a fraction, None (exact).
    - Other text Python's ISO parser reads (``2026-01-01T10``, which filters accepted before): exact; no offset is UTC.
    - Anything else isn't a datetime: None.
    """
    instant = parse_instant(value)
    if instant is not None:
        sign = max(value.find('+', 16), value.find('-', 16))
        unit = _UNITS.get(sign if sign > 0 else len(value.removesuffix('Z')))
        try:
            return instant, instant + unit if unit else None
        except OverflowError:  # the last unit of year 9999
            return instant, None
    if not isinstance(value, str):
        return None
    try:
        read = datetime.datetime.fromisoformat(value)
        return (read.astimezone(UTC) if read.tzinfo is not None else read.replace(tzinfo=UTC)), None
    except (ValueError, OverflowError):
        return None


def utc_text(instant: datetime.datetime) -> str:
    """An instant as the statistics write datetime bounds: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``, in UTC."""
    instant = instant.astimezone(UTC)
    return f'{instant.year:04d}-{instant:%m-%dT%H:%M:%S}.{instant.microsecond:06d}Z'


# The same text, written by SQL, from a timestamptz.
UTC_TEXT_SQL = """to_char(({}) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')"""


def indexed_instant_sql(text_sql: str) -> str:
    """SQL for the instant of a text expression (such as ``metadata->>'captured_at'``), exactly as an index on a
    datetime field is built: use it where such an index should serve the query."""
    return f"metadata_instant_v1({text_sql})"


# Every spelling the grammar allows with at most six fraction digits, with each digit written 9 and the separator T.
_DATE = '9999-99-99'
_CLOCKS = ('T99:99', 'T99:99:99', *(f'T99:99:99.{"9" * digits}' for digits in range(1, 7)))
_OFFSETS = ('', 'Z', *(sign + digits for sign in '+-' for digits in ('99', '9999', '99:99')))
_SHAPES = (_DATE, *(_DATE + clock + offset for clock in _CLOCKS for offset in _OFFSETS))


def instant_sql(text_sql: str) -> str:
    """SQL giving exactly :func:`indexed_instant_sql`'s answer, refusals included, at a little over half the cost per
    row: for a field without an index. STABLE (it calls the timestamptz input function), never raises, and doesn't
    depend on any setting, because the text reaching the cast always carries an explicit offset.

    How: ``translate`` writes every digit as 9 and the separator as T, and the result must be one of the grammar's
    spellings with at most six fraction digits (a hashed lookup), so the text is in the grammar. The cast reads such
    text as the rule does, except two things it accepts that the rule refuses: hour 24 (``24:00`` is the next day)
    and second 60 (the next minute), refused first. Everything else the rule refuses (month, day, minute, offset out
    of range, year 0000) the cast refuses too, and ``pg_input_is_valid`` turns that into NULL. Text with no offset
    (or ``Z``) gets ``+00``. Grammar text with more than six fraction digits, which the cast would round instead of
    truncate, is left to ``metadata_instant_v1``; any other text isn't a datetime.

    ``text_sql`` appears several times in the result: pass a cheap expression, such as a column or
    ``metadata->>'field'``.

    Cost, on the 1,000,000 rows of :func:`window_sql`'s table (one dataset, about ten metadata keys, six spellings in
    turn: ``Z``, ``+02:00``, ``-05:00``, none, ``+0530``, ``.123456Z``; PostgreSQL 18, one process, median of five
    runs, JIT off / on): a whole pass with one bound takes 2.5 / 2.5 s with this expression and 4.5 / 4.5 s with
    ``metadata_instant_v1``, against 0.23 / 0.22 s for 2.10.x's text comparison (whose answers differ on offsets);
    two bounds as one ``<@ tstzrange(lo, hi)`` 2.5 / 2.6 s (3.9 / 4.1 s as two comparisons); newest first, first
    page of 50, 2.6 / 2.6 s. The readers don't pay that on a full pass: :func:`window_sql` decides most rows by their
    written date and computes this only for rows within a day of a bound, and the newest-first page takes two passes
    by written date (1.6 / 1.5 s, 0.67 / 0.74 s for a year, 0.82 / 0.85 s, in its table). An index on
    ``metadata_instant_v1`` answers all of these in milliseconds.
    """
    value = f'({text_sql})'
    utc = f"pg_catalog.rtrim({value}, 'Z') || '+00'"
    return (
        f"CASE WHEN pg_catalog.translate({value}, '0123456789 ', '9999999999T') = ANY ('{{{','.join(_SHAPES)}}}')"
        f" THEN CASE WHEN {value} LIKE '___________24%' OR {value} LIKE '_________________60%' THEN NULL"
        f" WHEN {value} LIKE '%+%' OR {value} LIKE '________________%-%'"
        f" THEN CASE WHEN pg_catalog.pg_input_is_valid({value}, 'timestamptz') THEN ({value})::timestamptz END"
        f" WHEN pg_catalog.pg_input_is_valid({utc}, 'timestamptz') THEN ({utc})::timestamptz END"
        f" WHEN {value} LIKE '___________________._______%' THEN {indexed_instant_sql(text_sql)} END"
    )


def valid_sql(text_sql: str) -> str:
    """SQL that is TRUE exactly where :func:`instant_sql` has an instant, else FALSE (never NULL): the same checks
    without the cast, for rows whose date text alone decides a comparison (:func:`range_sql`)."""
    value = f'({text_sql})'
    return (
        f"CASE WHEN pg_catalog.translate({value}, '0123456789 ', '9999999999T') = ANY ('{{{','.join(_SHAPES)}}}')"
        f" THEN NOT ({value} LIKE '___________24%' OR {value} LIKE '_________________60%')"
        f" AND pg_catalog.pg_input_is_valid(CASE WHEN {value} LIKE '%+%' OR {value} LIKE '________________%-%'"
        f" THEN {value} ELSE pg_catalog.rtrim({value}, 'Z') || '+00' END, 'timestamptz')"
        f" WHEN {value} LIKE '___________________._______%' THEN {indexed_instant_sql(text_sql)} IS NOT NULL"
        " ELSE FALSE END"
    )


def instant_expr(text_sql: str, indexed: bool) -> str:
    """A datetime field's instant: the indexed function where the field's index is built on it, so the query rides
    the index, else the cheaper :func:`instant_sql`."""
    return indexed_instant_sql(text_sql) if indexed else instant_sql(text_sql)


def date_sql(text_sql: str) -> str:
    """A value's written date (its first ten characters), compared as bytes."""
    return f'pg_catalog.left({text_sql}, 10) COLLATE "C"'


def instants_from(text_sql: str, rows_sql: str, columns: tuple = ()) -> str:
    """A FROM item for a reader of a field without an index that scans many rows: the rows of ``rows_sql`` (such as
    ``samples WHERE ...``) with `columns`, the field's text as ``_text`` and its instant as ``_instant``.

    :func:`instant_sql` names its text a dozen times, and the planner copies a subquery's output into every reference
    unless the subquery is fenced (OFFSET 0). Each copy of ``metadata->>'field'`` reads the metadata again, which for
    a sample whose metadata is stored compressed (over about 2 kB: a COCO image's detections) means decompressing all
    of it: a sidebar's earliest and latest values read it 33 times a row. Here the text is read once a row, and the
    instant computed once from it (measured: COCO's dates 1.3 s -> 0.14 s, 98,000 small iNat records 0.34 -> 0.22 s).

    Filters (:func:`window_sql`) are a WHERE clause's part and still read it a few times a row: reading it once there
    takes a subquery per row, which halved COCO's (0.30 -> 0.15 s) but doubled small records' (iNat 0.04 -> 0.08 s).
    """
    keep = ''.join(f'{column}, ' for column in columns)
    return (
        f"(SELECT {keep}_text, {instant_sql('_text')} AS _instant"
        f" FROM (SELECT {keep}{text_sql} AS _text FROM {rows_sql} OFFSET 0) AS _texts OFFSET 0) AS _instants"
    )


def read_once(text_sql: str, name: str = '_text') -> str:
    """A join to put after ``samples`` that gives each row `text_sql` once, as `name`, for a sort on its instant.

    A function scan, as the histogram reads its values: the planner doesn't copy it into every reference, and unlike
    :func:`instants_from`'s fenced subqueries, the plan can still sort in parallel workers. Measured on a sort's
    instants: COCO's dates 0.66 -> 0.14 s, KITTI's 0.18 -> 0.06 s, and 98,000 small iNat records 0.15-0.16 ->
    0.17-0.18 s, where reading twelve times costs less than the join."""
    return f"CROSS JOIN LATERAL pg_catalog.unnest(ARRAY[{text_sql}]) AS _read_once({name})"


# No offset moves an instant more than 15:59 from its written date and time: a value written on date W names an
# instant in [W - 15:59, W + 1 day + 15:59). So against an instant on UTC date D, a value written on D + 2 or later is
# certainly after, and one written on D - 2 or earlier certainly before; only D - 1 to D + 1 need the instant.
_DECIDED = datetime.timedelta(days=2)


def window_sql(text_sql: str, indexed: bool, lower=None, upper=None) -> str:
    """SQL for ``lower <= instant < upper``, TRUE or FALSE (a row without an instant is FALSE, never NULL, so a NOT
    around it includes such rows, as ``!=`` does). ``lower`` and ``upper`` are ``(sql, utc_date)`` pairs, the SQL a
    timestamptz parameter and the date its instant falls on, or None for an open end.

    On an indexed field, plain comparisons the index serves. On another, the written date decides most rows with a
    text comparison: a row certainly inside needs only :func:`valid_sql`, one certainly outside nothing, and only
    rows dated within a day of a bound pay for the instant, once for both bounds.

    A whole pass over the same 1,000,000 rows as :func:`instant_sql`'s table (regenerated), one process, median of
    five runs, JIT off / on. The newest-first page is datasets._datetime_page_ids' two passes.

    ===============================  =============  ===============  =========================
    filter or sort                   today (text)   instant_sql      this (or the two passes)
    ===============================  =============  ===============  =========================
    one bound (``>= 2023-01-01``)    0.23 / 0.22 s  2.5 / 2.5 s      1.6 / 1.5 s
    two bounds (a year)              0.28 / 0.27 s  2.5 / 2.6 s (1)  0.67 / 0.74 s
    one week                         0.27 / 0.30 s  0.32 / 0.35 s    0.35 to 0.48 s
    newest first, first page of 50   0.34 / 0.30 s  2.6 / 2.6 s      0.82 / 0.85 s
    ===============================  =============  ===============  =========================

    (1) as one containment test; 3.9 / 4.1 s as two comparisons. The middle column's one-week row already had a
    date prefilter. Fixed-position range checks instead of ``pg_input_is_valid`` were slower (3.2 s against 2.6 s for
    one bound before anything else was added), so the parse is kept."""
    compares = [f"{{instant}} {sign} {bound[0]}" for sign, bound in ((">=", lower), ("<", upper)) if bound is not None]
    if indexed:
        instant = indexed_instant_sql(text_sql)
        return f"({' AND '.join(compares).format(instant=instant)} AND {instant} IS NOT NULL)"
    instant = instant_sql(text_sql)
    if lower is not None and upper is not None:
        compare = f"COALESCE({instant} <@ tstzrange({lower[0]}, {upper[0]}, '[)'), FALSE)"
    else:
        compare = f"COALESCE({compares[0].format(instant=instant)}, FALSE)"
    date = f'pg_catalog.left({text_sql}, 10) COLLATE "C"'
    outside, inside = [], []
    try:
        if lower is not None:
            outside.append(f"{date} <= '{lower[1] - _DECIDED}'")
            inside.append(f"{date} >= '{lower[1] + _DECIDED}'")
        if upper is not None:
            outside.append(f"{date} >= '{upper[1] + _DECIDED}'")
            inside.append(f"{date} <= '{upper[1] - _DECIDED}'")
    except OverflowError:  # a bound within two days of year 1 or 9999: every row by its instant
        return compare
    return (
        f"CASE WHEN {' OR '.join(outside)} THEN FALSE WHEN {' AND '.join(inside)} THEN {valid_sql(text_sql)}"
        f" ELSE {compare} END"
    )
