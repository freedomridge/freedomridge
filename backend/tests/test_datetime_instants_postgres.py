"""The datetime rule in SQL against the same corpus as Python (backend/datetimes.py), and the histogram that reads it.
Only an explicitly supplied disposable database is used."""

import datetime
import json
import os

import asyncpg
import pytest

from backend.database import PLANNING, init_connection
from backend.datetimes import indexed_instant_sql, instant_sql, parse_instant, valid_sql, window_sql
from backend.schema_check import expected_functions, fingerprint
from backend.tests import test_field_registry_postgres
from backend.tests.datetime_corpus import CASES, corpus
from backend.tests.test_histogram_postgres import MISSING, histogram, seed

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)

UTC = datetime.timezone.utc
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=UTC)
MICROSECOND = datetime.timedelta(microseconds=1)
# Python's datetime covers years 1-9999; SQL's instants go further.
PYTHON_RANGE = range(
    (datetime.datetime(1, 1, 1, tzinfo=UTC) - EPOCH) // MICROSECOND,
    (datetime.datetime.max.replace(tzinfo=UTC) - EPOCH) // MICROSECOND + 1,
)
# In the grammar, with an instant before year 1 or after 9999 (datetime_corpus.py adds them).
OUTSIDE_PYTHON = ['0001-01-01T00:00+01:00', '0001-01-01T00:00+15:59', '9999-12-31T23:59-00:01']
# Sessions that read text without an offset, field order, zone words and interval text differently.
SETTINGS = [
    {'TimeZone': 'UTC', 'DateStyle': 'ISO, MDY', 'IntervalStyle': 'postgres'},
    {'TimeZone': 'America/Chicago', 'DateStyle': 'SQL, DMY', 'IntervalStyle': 'sql_standard'},
    {'TimeZone': 'Asia/Kolkata', 'DateStyle': 'German, DMY', 'timezone_abbreviations': 'India'},
    {'TimeZone': 'Pacific/Chatham', 'DateStyle': 'Postgres, YMD', 'IntervalStyle': 'iso_8601'},
]
EVERY_FORM = (
    f"SELECT (extract(epoch FROM {indexed_instant_sql('v')}) * 1000000)::int8 AS indexed,"
    f" (extract(epoch FROM {instant_sql('v')}) * 1000000)::int8 AS scan"
    ' FROM unnest($1::text[]) WITH ORDINALITY AS t(v, n) ORDER BY n'
)


def micros(value):
    return None if value is None else (value - EPOCH) // MICROSECOND


async def instants(database, texts, settings):
    """Both SQL forms' instants of ``texts`` (microseconds since 1970) in a session with ``settings``."""
    answers = []
    async with database.pool.acquire() as connection, connection.transaction():
        for name, value in settings.items():
            await connection.execute(f"SELECT set_config('{name}', '{value}', true)")
        for start in range(0, len(texts), 50_000):  # each statement well inside the fixture's timeout
            answers += [tuple(row) for row in await connection.fetch(EVERY_FORM, texts[start : start + 50_000])]
    return answers


def test_python_and_both_sql_forms_agree_on_the_corpus(registry_database):
    texts = corpus()
    python = [micros(parse_instant(text)) for text in texts]

    async def scenario(database):
        # Any text that raised would fail its whole statement.
        answers = await instants(database, texts, SETTINGS[0])
        for text, mine, (indexed, scan) in zip(texts, python, answers):
            assert scan == indexed, text
            assert mine == (indexed if indexed is None or indexed in PYTHON_RANGE else None), text
        assert sum(indexed is not None for indexed, _ in answers) > 100_000
        by_text = dict(zip(texts, answers))
        for text in OUTSIDE_PYTHON:
            assert by_text[text][0] is not None and by_text[text][0] not in PYTHON_RANGE, text
        for settings in SETTINGS[1:]:
            assert await instants(database, texts, settings) == answers, settings

    registry_database(scenario)


def test_cases_in_sql(registry_database):
    async def scenario(database):
        answers = await instants(database, [text for text, _ in CASES], SETTINGS[1])
        for (text, expected), answer in zip(CASES, answers):
            instant = None if expected is None else datetime.datetime.fromisoformat(expected.replace('Z', '+00:00'))
            assert answer == (micros(instant), micros(instant)), text

    registry_database(scenario)


def test_metadata_instant_v1_is_declared_as_pinned(registry_database):
    """The live function is schema.sql's, IMMUTABLE, STRICT and PARALLEL SAFE (test_datetime_instants.py pins the
    text; this checks what the database made of it)."""

    async def scenario(database):
        row = await database.fetchrow(
            "SELECT md5(prosrc) AS body, provolatile::text, proisstrict, proparallel::text,"
            " prorettype::regtype::text AS returns, pg_get_function_arguments(oid) AS arguments,"
            " (SELECT lanname FROM pg_language WHERE oid = prolang) AS lang"
            " FROM pg_proc WHERE oid = 'metadata_instant_v1(text)'::regprocedure",
        )
        assert dict(row) == {
            'body': fingerprint(expected_functions()['metadata_instant_v1']),
            'provolatile': 'i',
            'proisstrict': True,
            'proparallel': 's',
            'returns': 'timestamp with time zone',
            'arguments': 'value text',
            'lang': 'plpgsql',
        }

    registry_database(scenario)


def test_no_search_path_changes_what_the_function_calls(registry_database):
    """Every function and operator inside is named with its schema: decoys earlier on the search_path change nothing,
    and an empty search_path (as when a restore rebuilds an index) works."""
    texts = [text for text, _ in CASES]

    async def scenario(database):
        namespace = (await database.fetchval('SHOW search_path')).split(',')[0].strip()
        query = f'SELECT {namespace}.metadata_instant_v1(v) FROM unnest($1::text[]) v'
        expected = await database.fetch(query, texts)
        await database.execute(f"""
            CREATE FUNCTION {namespace}.substr(text, integer, integer) RETURNS text LANGUAGE sql IMMUTABLE
                AS $$ SELECT '01' $$;
            CREATE FUNCTION {namespace}.lie(integer, integer) RETURNS boolean LANGUAGE sql IMMUTABLE
                AS $$ SELECT false $$;
            CREATE FUNCTION {namespace}.never(text, text) RETURNS boolean LANGUAGE sql IMMUTABLE
                AS $$ SELECT false $$;
            CREATE OPERATOR {namespace}.= (LEFTARG = integer, RIGHTARG = integer, FUNCTION = {namespace}.lie);
            CREATE OPERATOR {namespace}.!~ (LEFTARG = text, RIGHTARG = text, FUNCTION = {namespace}.never);
        """)
        for path in (f'{namespace}, pg_catalog', "''"):
            # A new connection, so no plan cached under the usual path is reused.
            connection = await asyncpg.connect(os.environ['FRIDGE_TEST_POSTGRES_DSN'])
            try:
                await connection.execute(f'SET search_path = {path}')
                got = await connection.fetch(query, texts)
            finally:
                await connection.close()
            assert [row[0] for row in got] == [row[0] for row in expected], path

    registry_database(scenario)


def test_an_index_on_the_function_builds_and_serves_a_range(registry_database):
    async def scenario(database):
        await seed(
            database,
            [f'2026-01-{day:02d}T10:00:00+02:00' for day in range(1, 29)] + ['2026-02-30', 'unknown', None, MISSING],
            'datetime',
            'captured_at',
        )
        expression = indexed_instant_sql("metadata->>'captured_at'")
        await database.execute(f'CREATE INDEX instant_test ON samples (({expression})) WHERE dataset_id = 1')
        await database.execute('ANALYZE samples')
        query = f"SELECT count(*) FROM samples WHERE dataset_id = 1 AND {expression} >= '2026-01-20T08:00:00Z'"
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute('SET LOCAL enable_seqscan = off')
            plan = json.loads(await connection.fetchval(f'EXPLAIN (FORMAT JSON) {query}'))
            assert 'instant_test' in json.dumps(plan)
            assert await connection.fetchval(query) == 9

    registry_database(scenario)


BAD_DATETIMES = ['2026-02-30', '2026-01-01 n/a', '2026-01-01T24:00:00Z', 'unknown', '']


@pytest.mark.parametrize('bad', BAD_DATETIMES)
def test_histogram_counts_text_that_isnt_a_datetime_as_missing(registry_database, bad):
    """It used to fail the request (a 500) on any of these: a prefix check let them reach the timestamptz cast."""

    async def scenario(database):
        await seed(database, ['2026-01-01T00:00:00Z', '2026-01-02T00:00:00+02:00', bad, None, MISSING], 'datetime')
        result = await histogram(bins=2)
        assert result['total'] == 5 and result['missing'] == 3
        assert [bucket['count'] for bucket in result['buckets']] == [1, 1]
        assert result['buckets'][0]['range']['min_label'] == '2026-01-01T00:00:00.000000'
        assert result['buckets'][1]['range']['max_label'] == '2026-01-01T22:00:00.000000'

    registry_database(scenario)


def test_histogram_reads_no_offset_as_utc_in_any_session(registry_database):
    values = ['2026-01-01', '2026-01-01T06:00', '2026-01-01 12:00:00', '2026-01-01T18:00:00Z', '2026-01-02T00:00+02:00']

    async def scenario(database):
        await seed(database, values, 'datetime')
        expected = await histogram(bins=4)
        assert expected['buckets'][0]['range']['min_label'] == '2026-01-01T00:00:00.000000'
        search_path = await database.fetchval('SHOW search_path')
        chicago = await asyncpg.create_pool(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            min_size=1,
            max_size=2,
            init=init_connection,
            server_settings={'search_path': search_path, 'TimeZone': 'America/Chicago', **PLANNING},
        )
        original, database.pool = database.pool, chicago
        try:
            assert await database.fetchval('SHOW TimeZone') == 'America/Chicago'
            assert await histogram(bins=4) == expected
        finally:
            database.pool = original
            await chicago.close()

    registry_database(scenario)


# Bounds on dates the corpus is dense around, a leap day, and the ends of the range.
WINDOW_BOUNDS = [
    ('2026-01-01T00:00:00Z', None),
    (None, '2026-01-01T08:00:00.000001Z'),
    ('2024-02-28T15:59:00Z', '2024-03-01T08:01:00Z'),
    ('2025-12-31T23:59:59.999999Z', '2026-01-02T00:00:00Z'),
    ('0001-01-02T00:00:00Z', None),
    (None, '9999-12-30T00:00:00Z'),
]


def test_cheaper_forms_agree_with_the_indexed_function(registry_database):
    """valid_sql is exactly 'has an instant', and a window decided mostly by the written date is exactly the
    comparison of instants, on every text of the corpus, refusals included."""
    texts = corpus()

    async def scenario(database):
        windows = []
        args: list = [None]
        for lower, upper in WINDOW_BOUNDS:
            bounds: list = []
            for value in (lower, upper):
                if value is None:
                    bounds.append(None)
                    continue
                instant = parse_instant(value)
                assert instant is not None
                args.append(instant)
                bounds.append((f'${len(args)}::timestamptz', instant.date()))
            windows.append((window_sql('v', False, *bounds), window_sql('v', True, *bounds)))
        columns = ', '.join(f'{cheap} AS cheap{n}, {index} AS indexed{n}' for n, (cheap, index) in enumerate(windows))
        query = (
            f'SELECT {valid_sql("v")} AS valid, {indexed_instant_sql("v")} IS NOT NULL AS has_instant, {columns} '
            'FROM unnest($1::text[]) WITH ORDINALITY AS t(v, n) ORDER BY n'
        )
        for start in range(0, len(texts), 50_000):
            args[0] = texts[start : start + 50_000]
            for text, row in zip(args[0], await database.fetch(query, *args)):
                assert row['valid'] is row['has_instant'], text
                for n in range(len(windows)):
                    assert row[f'cheap{n}'] is row[f'indexed{n}'], (text, WINDOW_BOUNDS[n])

    registry_database(scenario)
