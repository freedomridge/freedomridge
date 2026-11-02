"""Every FK column referencing samples(id) must be the LEADING column of some index.

Postgres enforces each FK with a per-deleted-row lookup on the referencing table; without an
index led by the FK column that lookup is a sequential scan. Found 2026-07-06: three tables
referenced samples with only differently-led indexes (embedding_viz_rank 250ms/row,
embedding_concept_scores 148ms/row, sample_dup_secondary), making `DELETE FROM samples` cost
~0.4s per row — a 5k-sample dataset delete ground for ~35 minutes (schema.sql has the
indexes). Scoped to REFERENCES samples: it's the hot bulk-delete path (dataset delete, bulk
sample delete, version revert); dataset_id FKs all lead their tables' PKs already.

Static schema.sql parse (comment-stripped), like test_schema_parity.py — the same check any
new migration's table must pass, since schema.sql mirrors migrations. Partial (WHERE) indexes
don't count: the FK lookup has no predicate, so the planner can't use them.
"""

import re
from pathlib import Path

_SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema.sql"

_LINE_COMMENT = re.compile(r"--[^\n]*")
_CREATE_TABLE = re.compile(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.I)
_FK_SAMPLES = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+\w+.*\bREFERENCES\s+samples\b", re.I | re.M)
_PRIMARY_KEY = re.compile(r"PRIMARY KEY\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)", re.I)
_UNIQUE = re.compile(r"UNIQUE\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)", re.I)
_CREATE_INDEX = re.compile(
    r"CREATE(?:\s+UNIQUE)?\s+INDEX(?:\s+IF NOT EXISTS)?\s+\S+\s+ON\s+([A-Za-z_][A-Za-z0-9_]*)"
    r"\s*(?:USING\s+\w+\s*)?\(\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.I,
)


def _table_body(sql, start):
    """Return the parenthesized CREATE TABLE body starting at the '(' at `start`."""
    depth = 0
    for i in range(start, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return sql[start : i + 1]
    raise AssertionError("unbalanced parens in schema.sql CREATE TABLE")


def test_every_samples_fk_has_a_leading_index():
    sql = _LINE_COMMENT.sub("", _SCHEMA.read_text())

    # (table, fk_column) pairs for every inline `col ... REFERENCES samples`; PRIMARY KEY /
    # UNIQUE table constraints both create an index led by their first column.
    fks = []
    leading = set()
    for m in _CREATE_TABLE.finditer(sql):
        table = m.group(1).lower()
        body = _table_body(sql, m.end() - 1)
        fks += [(table, col.lower()) for col in _FK_SAMPLES.findall(body)]
        leading |= {(table, col.lower()) for col in _PRIMARY_KEY.findall(body)}
        leading |= {(table, col.lower()) for col in _UNIQUE.findall(body)}
    assert ("embeddings", "sample_id") in fks, "parser broke: embeddings.sample_id FK not found"

    # leading column of every full (non-partial) index; index statements are single-statement
    # in schema.sql so a WHERE before the next ';' marks a partial index.
    for m in _CREATE_INDEX.finditer(sql):
        stmt = sql[m.start() : sql.index(";", m.start())]
        if re.search(r"\bWHERE\b", stmt, re.I):
            continue
        leading.add((m.group(1).lower(), m.group(2).lower()))

    missing = [(t, c) for t, c in fks if (t, c) not in leading]
    assert not missing, (
        f"FK columns referencing samples(id) with no leading index (sample deletes will "
        f"seq-scan these tables per deleted row): {missing}. Add an index on (col) in the "
        f"migration AND schema.sql."
    )
