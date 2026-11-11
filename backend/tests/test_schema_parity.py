"""Guard against a deploy-bug class: a migration object missing from
schema.sql. Fresh prod DBs init from schema.sql directly (no migration runs on a new database),
so anything a migration creates — a TABLE, an added COLUMN, a FUNCTION, a TRIGGER, or a
(non-concurrent) INDEX — must also live in schema.sql, or the feature breaks on a fresh deploy.

This bit us repeatedly: region_selections + quality_dup_pairs (tables), then lon/lat (columns),
samples_set_lonlat (function+trigger), overlay_config (column). The check is a presence test
(comment-stripped) — heuristic but it has caught every real gap so far.

CONCURRENTLY indexes are intentionally EXCLUDED: they can't run inside schema.sql init and are
built online as ops steps (idx_samples_lonlat_cover via backfill_sample_lonlat.py; the HNSW/ANN
indexes via build_ann_indexes.py).
"""

import re
from pathlib import Path

_DB_DIR = Path(__file__).resolve().parents[1] / "db"
_SCHEMA = _DB_DIR / "schema.sql"
_MIGRATIONS = _DB_DIR / "migrations"

_LINE_COMMENT = re.compile(r"--[^\n]*")
_RE = {
    "table": re.compile(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
    "column": re.compile(
        r"ALTER TABLE\s+[A-Za-z_][A-Za-z0-9_]*\s+ADD COLUMN(?:\s+IF NOT EXISTS)?\s+([A-Za-z_][A-Za-z0-9_]*)",
        re.I,
    ),
    "function": re.compile(r"CREATE(?:\s+OR REPLACE)?\s+FUNCTION\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
    "trigger": re.compile(r"CREATE TRIGGER\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
    # CREATE INDEX but NOT CONCURRENTLY (those are online/ops-built, intentionally not in schema.sql)
    "index": re.compile(r"CREATE INDEX\s+(?!CONCURRENTLY)(?:IF NOT EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.I),
}


def _strip(sql: str) -> str:
    return _LINE_COMMENT.sub("", sql)


def _objects(sql: str) -> dict:
    sql = _strip(sql)
    return {kind: {m.group(1).lower() for m in rx.finditer(sql)} for kind, rx in _RE.items()}


# Migration objects intentionally NOT ported to schema.sql, with the reason. Keep this list
# tiny and justified — every entry is a hole the test would otherwise catch.
_ALLOWLIST: set[str] = set()


def _present(name: str, schema: str) -> bool:
    # word-boundary presence check (comment-stripped schema)
    return re.search(rf"\b{re.escape(name)}\b", schema, re.I) is not None


def test_region_selections_and_quality_dup_pairs_in_schema():
    schema = _strip(_SCHEMA.read_text())
    for tbl in ("region_selections", "quality_dup_pairs"):
        assert _present(tbl, schema), f"{tbl} table missing from schema.sql"


def test_lonlat_and_overlay_config_in_schema():
    schema = _strip(_SCHEMA.read_text())
    for obj in ("lon", "lat", "overlay_config", "samples_set_lonlat", "trg_samples_set_lonlat"):
        assert _present(obj, schema), f"{obj} missing from schema.sql"


def test_every_migration_object_is_in_schema():
    schema = _strip(_SCHEMA.read_text())
    missing: dict[str, str] = {}
    for mig in sorted(_MIGRATIONS.glob("*.sql")):
        objs = _objects(mig.read_text())
        for kind, names in objs.items():
            for name in names:
                key = f"{kind}:{name}"
                if key not in _ALLOWLIST and not _present(name, schema):
                    missing.setdefault(key, mig.name)
    assert not missing, (
        "Migration objects absent from schema.sql (a fresh prod init would lack them; "
        "CONCURRENTLY indexes are excluded by design): " + repr(missing)
    )
