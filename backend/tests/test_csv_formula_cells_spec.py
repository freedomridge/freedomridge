"""The value list download (`GET /api/assistant/lists/{handle}/csv`) never writes a cell a spreadsheet would run as a
formula, quotes every text, and leaves a number bare (contract "a CSV the app writes can't run as a formula",
sections 1 to 3; surface 4.3).

Written from the contract, not from the code. A list is made by `field_values` and downloaded by its person, as
backend/tests/test_assistant_views_spec.py does. The cases are frontend/tests/fixtures/csv-formula-cells.json, the
file the Export dialog's tests read too (the rule is the same in both places): every case whose value is a text and
that holds for both places, each a value of one text field. Each written cell is compared, quotes included, with the
file read by RFC 4180's rules (quoted cells, doubled quotes, line breaks inside quotes).

Where the contract is silent these tests read it this way:
- A number field's values are numbers, written bare, and a text value can still reach it (a later write of text to a
  field first written with numbers). `field_values` gives a number field's values as numbers where it can read them
  so: the text "-8" below comes back as the number -8, and is written bare as a number field's value. Its other text
  values are texts, quoted and guarded by 1.1. A text such as "+3.2" or "-inf" there would check that conversion,
  not the cell rule; those are checked in the text field.
- The download's header is always `"value","count"` (section 3's note), its counts bare (numbers by type, 1.4), and
  its file name `<field>-values.csv` (1.5).
- What the download already did for a value that starts a formula (the apostrophe) still holds: those cases are
  among the ones below, and pass today.
- Each row is found by its value with every apostrophe left out (no planted value holds one), so a row whose
  apostrophes are wrong still shows what it held.
- The cells the fixture describes rather than the contract gives are read as the unit test's comment says
  (frontend/tests/csv-cells-spec.test.cjs).
- 1.6, the docs: a light check, in the way other docs tests here read the docs (test_assistant_guests_spec): the
  paragraph of docs/ASSISTANT.md that describes the download, and a paragraph of the docs that describes the Export
  dialog's file, say the apostrophe, the `;`, and that a program reading the file sees it.
"""

import json
import os
import pathlib
import re
from typing import Any, Optional
from urllib.parse import urlsplit

import pytest

from backend.tests import test_access_rule_spec, test_field_registry_postgres, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN, World
from backend.tests.test_assistant_core_spec import add, open_door
from backend.tests.test_assistant_views_spec import result

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
database = test_upgrade_paths.database
postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

TEXTS, NUMBERS = 760, 761
ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'frontend/tests/fixtures/csv-formula-cells.json'
CASES = json.loads(FIXTURE.read_text(encoding='utf-8'))['cases']
TEXT_CASES = [case for case in CASES if case['where'] == 'both' and isinstance(case['value'], str)]

# A number field: (what, value, written cell). First written with numbers alone, so the field is a number field;
# then the text values.
NUMBER_VALUES: list[tuple[str, Any, str]] = [
    ('the number -5', -5, '-5'),
    ('the number 3.2', 3.2, '3.2'),
    ('the number -0.001', -0.001, '-0.001'),
    ('the number 7', 7, '7'),
]
NUMBER_TEXTS: list[tuple[str, Any, str]] = [
    ('the text -8 (comes back as a number)', '-8', '-8'),
    ('the text -5 apples', '-5 apples', '"\'-5 apples"'),
    ('the text --5', '--5', '"\'--5"'),
    ('the text - 5', '- 5', '"\'- 5"'),
    ('the text =1+1', '=1+1', '"\'=1+1"'),
    ('the text -5;=1', '-5;=1', '"\'-5;\'=1"'),
]


def records(text: str) -> list[list[tuple[str, str]]]:
    """The file's records by RFC 4180's rules, each cell as (its text as written, its value)."""
    out: list[list[tuple[str, str]]] = []
    record: list[tuple[str, str]] = []
    i = 0
    while True:
        start = i
        if text[i : i + 1] == '"':
            i += 1
            value: list[str] = []
            while True:
                assert i < len(text), f'a quoted cell never closes: {text[start : start + 60]!r}'
                if text[i] == '"':
                    if text[i + 1 : i + 2] == '"':
                        value.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                value.append(text[i])
                i += 1
            assert i == len(text) or text[i] in ',\r\n', f'text after a closing quote: {text[start : i + 20]!r}'
            cell = ''.join(value)
        else:
            while i < len(text) and text[i] not in ',\r\n':
                assert text[i] != '"', f'a quote inside an unquoted cell: {text[start : i + 20]!r}'
                i += 1
            cell = text[start:i]
        record.append((text[start:i], cell))
        if i == len(text):
            out.append(record)
            break
        if text[i] == ',':
            i += 1
            continue
        i += 2 if text[i : i + 2] == '\r\n' else 1
        out.append(record)
        record = []
        if i == len(text):  # a line break after the last row
            break
    return out


Check = tuple[str, Optional[str], str]  # (what, the cell as written, the cell the contract writes)


async def download(w: World, dataset_id: int, field: str) -> tuple[list[tuple[str, str]], list[Check]]:
    """The field's value list made by field_values and downloaded by its person, after checking what a program parses
    from it: each value row's first cell as (written, value), and the header's and the counts' cells to check."""
    listed = await result(ADMIN, 'field_values', {'dataset': dataset_id, 'field': field, 'limit': 50})
    assert listed['list'] is not None, listed
    response = await w.call(ADMIN, 'GET', urlsplit(listed['download']).path)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('text/csv'), response.headers
    assert f'filename="{field}-values.csv"' in response.headers['content-disposition'], response.headers
    header, *rows = records(response.text)
    assert [value for _, value in header] == ['value', 'count'], header
    assert all(len(row) == 2 and row[1][1] == '1' for row in rows), rows  # each value planted once
    checks: list[Check] = [('header: value', header[0][0], '"value"'), ('header: count', header[1][0], '"count"')]
    checks += [(f'the count of {row[0][1]!r} (a number)', row[1][0], '1') for row in rows]
    return [row[0] for row in rows], checks


def compare(rows: list[tuple[str, str]], cases: list[tuple[str, Any, str]], checks: list[Check]) -> None:
    """Each case's row, found by its value with every apostrophe left out, written as expected; and the other cells."""
    by_value = {value.replace("'", ''): written for written, value in rows}
    found: list[Check] = []
    for what, value, expected in cases:
        text = value if isinstance(value, str) else json.dumps(value)
        found.append((what, by_value.pop(text, None), expected))
    lines = [
        f'{"ok  " if held == expected else "FAIL"} {what}: held {held!r}, expected {expected!r}'
        for what, held, expected in found + checks
    ]
    lines += [f'FAIL a row that is no case: {written!r}' for written in by_value.values()]
    wrong = sum(held != expected for _, held, expected in found + checks)
    assert not wrong and not by_value and len(rows) == len(cases), '\n'.join(
        [f'{wrong} of {len(found + checks)} cells are not as the contract writes them:', *lines],
    )


@postgres
def test_a_text_fields_value_list_writes_every_cell_by_the_rule(world):
    async def scenario(w: World):
        await w.dataset(TEXTS)
        samples = [
            {'filepath': f'text-{n:02d}.jpg', 'metadata': {'note': c['value']}} for n, c in enumerate(TEXT_CASES)
        ]
        await add(TEXTS, samples)
        # Guards the guard: every value is in the dataset as planted, and the field is a text field.
        held = await w.database.fetch("SELECT metadata->>'note' AS note FROM samples WHERE dataset_id = $1", TEXTS)
        assert sorted(row['note'] for row in held) == sorted(c['value'] for c in TEXT_CASES)
        kind = await w.database.fetchval(
            "SELECT field_type FROM dataset_fields WHERE dataset_id = $1 AND field_name = 'note'",
            TEXTS,
        )
        assert kind == 'string', kind
        await open_door(w)
        rows, checks = await download(w, TEXTS, 'note')
        compare(rows, [(f"{c['case']} ({c['contract']})", c['value'], c['cell']) for c in TEXT_CASES], checks)

    world(scenario)


@postgres
def test_a_number_fields_value_list_keeps_numbers_and_guards_its_texts(world):
    async def scenario(w: World):
        await w.dataset(NUMBERS)
        for prefix, cases in (('number', NUMBER_VALUES), ('text', NUMBER_TEXTS)):  # numbers first: a number field
            await add(
                NUMBERS,
                [{'filepath': f'{prefix}-{n}.jpg', 'metadata': {'amount': v}} for n, (_, v, _) in enumerate(cases)],
            )
        kind = await w.database.fetchval(
            "SELECT field_type FROM dataset_fields WHERE dataset_id = $1 AND field_name = 'amount'",
            NUMBERS,
        )
        assert kind == 'number', kind
        types = await w.database.fetch(
            "SELECT jsonb_typeof(metadata->'amount') AS kind, count(*) FROM samples WHERE dataset_id = $1 GROUP BY 1",
            NUMBERS,
        )
        assert {row['kind']: row['count'] for row in types} == {
            'number': len(NUMBER_VALUES),
            'string': len(NUMBER_TEXTS),
        }
        await open_door(w)
        rows, checks = await download(w, NUMBERS, 'amount')
        compare(rows, NUMBER_VALUES + NUMBER_TEXTS, checks)

    world(scenario)


def paragraphs(text: str) -> list[str]:
    """The text's paragraphs and list items, each on one line."""
    return [' '.join(part.split()) for part in re.split(r'\n\s*\n|\n(?=\s*[-*] )', text) if part.strip()]


def says_it(paragraph: str) -> bool:
    """The paragraph says a cell that would start a formula gets an apostrophe, inside a cell too, and that a program
    reading the file sees it (1.6)."""
    return (
        'apostrophe' in paragraph.lower()
        and ('`;`' in paragraph or 'semicolon' in paragraph.lower())  # inside a cell too, not prose's ;
        and re.search(r'\bprograms?\b', paragraph, re.I) is not None
    )


def sections(text: str) -> list[str]:
    """Each section of a markdown text from level 2 down: its heading and the text under it, to the next heading of the
    same or a higher level. Code fences are left out (a `#` line inside one is no heading)."""
    lines: list[str] = []
    headings: list[tuple[int, int]] = []  # (line, level)
    fence: Optional[str] = None
    for line in text.splitlines():
        marker = re.match(r'\s*(`{3,}|~{3,})', line)
        if fence is None and marker:
            fence = marker.group(1)
        elif fence is not None:
            if line.strip().startswith(fence[0] * len(fence)) and not line.strip().strip(fence[0]):
                fence = None
        else:
            heading = re.match(r'(#{1,6}) ', line)
            if heading:
                headings.append((len(lines), len(heading.group(1))))
            lines.append(line)
    found = []
    for n, (start, level) in enumerate(headings):
        if level < 2:  # a document's title holds all of it
            continue
        end = next((at for at, deeper in headings[n + 1 :] if deeper <= level), len(lines))
        found.append('\n'.join(lines[start:end]))
    return found


def test_the_docs_say_where_the_value_list_download_is_described_that_a_program_sees_the_apostrophes():
    assistant = paragraphs((ROOT / 'docs/ASSISTANT.md').read_text(encoding='utf-8'))
    download = [p for p in assistant if 'GET /api/assistant/lists/{handle}/csv' in p]
    assert download, 'docs/ASSISTANT.md no longer describes the value list download'
    assert any(says_it(p) for p in download), download


def test_the_docs_say_where_the_export_is_described_that_a_program_sees_the_apostrophes():
    """A section of the user docs (not docs/architecture) that mentions the export and CSV somewhere, and one of whose
    paragraphs, other than the value list download's, says it."""
    export = [
        (document.name, section)
        for document in [ROOT / 'README.md', *sorted((ROOT / 'docs').glob('*.md'))]
        for section in sections(document.read_text(encoding='utf-8'))
        if re.search(r'\bexport', section, re.I) and re.search(r'\bcsv\b', section, re.I)
    ]
    named = [(name, section.splitlines()[0]) for name, section in export]
    assert any(says_it(p) and 'assistant/lists' not in p for _, section in export for p in paragraphs(section)), (
        f'no section of the docs that describes the CSV export says it; the sections naming export and CSV: {named}'
    )
