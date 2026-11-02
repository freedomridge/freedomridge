"""The backend knows whether the database is current.

- schema.sql must seed the ledger with the baseline and EVERY migration file the code ships, so a
  fresh install is current and a forgotten entry fails here, not in production.
- pending_migrations(): files not in the ledger; a database without the ledger table, or without
  the baseline row, was not created from schema.sql.
- unknown_migrations(): recorded files the code doesn't ship — the database is newer than the code.
- stale_functions(): every function body schema.sql defines is fingerprinted against pg_proc —
  the ledger knows which FILES ran, not whether a later re-run undid them (2026-10-10).
- backend/db/functions.sql is schema.sql's function definitions extracted for the runner to
  re-apply; it must stay byte-identical to what schema.sql says.
- GET /api/health: 200 when current, 503 + the lists when not — the deploy checklist / LB probe.
"""

import asyncio
import re
from pathlib import Path

import pytest

from backend import schema_check
from backend.schema_check import (
    BASELINE,
    FUNCTIONS_SQL,
    LEDGER_MISSING,
    NOT_FROM_SCHEMA,
    expected_functions,
    fingerprint,
    functions_sql,
    migration_files,
    pending_migrations,
    stale_functions,
    unknown_migrations,
)
from backend.version import VERSION

ROOT = Path(__file__).resolve().parents[2]


class FakeDB:
    """Answers the two queries the checks make: the ledger and pg_proc."""

    def __init__(self, rows=None, fail=False, proc_rows=None, proc_fail=False):
        self.rows, self.fail = rows or [], fail
        self.proc_rows, self.proc_fail = proc_rows, proc_fail

    async def fetch(self, query, *args):
        if "pg_proc" in query:
            if self.proc_fail:
                raise RuntimeError("connection refused")
            if self.proc_rows is None:  # default: everything matches schema.sql
                return [{"proname": n, "fp": fingerprint(b)} for n, b in expected_functions().items()]
            return self.proc_rows
        if self.fail:
            raise RuntimeError('relation "schema_migrations" does not exist')
        return self.rows


def _seeded(sql_text: str) -> set:
    m = re.search(r"INSERT INTO schema_migrations \(filename\) VALUES\s*(.*?)\s*ON CONFLICT", sql_text, re.S)
    assert m, "ledger seed not found"
    return set(re.findall(r"\('([^']+)'\)", m.group(1)))


def _ledger(*filenames: str) -> FakeDB:
    return FakeDB(rows=[{"filename": f} for f in filenames])


def _all_recorded() -> FakeDB:
    return _ledger(BASELINE, *migration_files())


@pytest.fixture
def two_migrations(tmp_path, monkeypatch):
    """The code ships two migrations after the baseline."""
    for name in ("0001_a.sql", "0002_b.sql"):
        (tmp_path / name).write_text("SELECT 1;\n")
    monkeypatch.setattr(schema_check, "MIGRATIONS_DIR", tmp_path)
    return ["0001_a.sql", "0002_b.sql"]


# --- ledger -------------------------------------------------------------------------------


def test_schema_sql_seeds_the_baseline_and_every_migration_file():
    files = {BASELINE, *migration_files()}
    seeded = _seeded((ROOT / "backend" / "db" / "schema.sql").read_text())
    assert seeded == files, (
        f"schema.sql ledger seed is out of date — missing {sorted(files - seeded)}, stale {sorted(seeded - files)}"
    )


def test_fresh_schema_records_migrations_only_after_all_ddl():
    schema = (ROOT / 'backend/db/schema.sql').read_text()
    after_ledger = schema.split('INSERT INTO schema_migrations', 1)[1]
    assert re.search(r'^(CREATE|ALTER|DROP)\b', after_ledger, re.M) is None


def test_the_runner_and_the_check_agree_on_the_baseline():
    runner = (ROOT / "scripts" / "apply_migrations.sh").read_text()
    assert f"BASELINE={BASELINE}\n" in runner


def test_pending_is_the_set_difference(two_migrations):
    assert asyncio.run(pending_migrations(_ledger(BASELINE, "0002_b.sql"))) == ["0001_a.sql"]
    assert asyncio.run(pending_migrations(_ledger(BASELINE, *two_migrations))) == []


def test_missing_ledger_table_is_reported():
    assert asyncio.run(pending_migrations(FakeDB(fail=True))) == [LEDGER_MISSING]
    assert asyncio.run(unknown_migrations(FakeDB(fail=True))) == []


def test_a_ledger_without_the_baseline_was_not_created_from_schema_sql(two_migrations):
    old_chain = _ledger("0001_a.sql", "001_old_chain.sql")
    assert asyncio.run(pending_migrations(old_chain)) == [NOT_FROM_SCHEMA]
    # Not "newer than the code": the runner reports the missing baseline first, and so does the API.
    assert asyncio.run(unknown_migrations(old_chain)) == []
    msg = schema_check.log_pending([NOT_FROM_SCHEMA])
    assert msg and "NOT CREATED FROM backend/db/schema.sql" in msg and "apply_migrations.sh" not in msg


def test_unknown_is_what_the_database_records_and_the_code_does_not_ship(two_migrations):
    db = _ledger(BASELINE, "0001_a.sql", "0003_newer.sql", "001_older_chain.sql")
    assert asyncio.run(unknown_migrations(db)) == ["0003_newer.sql", "001_older_chain.sql"]
    assert asyncio.run(pending_migrations(db)) == ["0002_b.sql"]
    assert asyncio.run(unknown_migrations(_ledger(BASELINE, *two_migrations))) == []


# --- function bodies ----------------------------------------------------------------------


def test_schema_sql_defines_the_functions_the_app_relies_on():
    names = set(expected_functions())
    assert {
        "refresh_stale_datasets_stats",
        "mark_dataset_stats_stale",
        "recompute_viz_rank",
        "samples_set_lonlat",
    } <= names
    assert len(names) >= 9
    body = expected_functions()["refresh_stale_datasets_stats"]
    assert "durable jobs" in body and "RAISE EXCEPTION" in body
    assert "record_stats_work" in expected_functions()["mark_dataset_stats_stale"]


def test_the_last_migration_defining_a_function_matches_schema_sql():
    """A function's final migration rewrite and schema.sql must agree — otherwise a chain-built
    database and a schema.sql-built one differ (the runner re-applies functions.sql last, so the
    database ends up right either way, but the migration would then be lying)."""
    mig_dir = ROOT / "backend" / "db" / "migrations"
    last: dict[str, tuple[str, str]] = {}
    for f in migration_files():
        for name, body in expected_functions((mig_dir / f).read_text()).items():
            last[name] = (f, body)
    for name, body in expected_functions().items():
        if name in last:
            assert fingerprint(last[name][1]) == fingerprint(body), (
                f"{name}: body in {last[name][0]} differs from schema.sql"
            )


def test_functions_sql_is_schema_sql_extracted():
    assert FUNCTIONS_SQL.read_text() == functions_sql(), (
        "backend/db/functions.sql is out of sync with schema.sql — regenerate it: "
        "python -m backend.schema_check --emit-functions > backend/db/functions.sql"
    )
    # round-trip: applying functions.sql yields the bodies the fingerprint check expects
    assert expected_functions(FUNCTIONS_SQL.read_text()) == expected_functions()


def test_stale_functions_reports_differing_and_missing_bodies():
    expected = expected_functions()
    names = sorted(expected)
    rows = [{"proname": n, "fp": fingerprint(expected[n])} for n in names]
    rows[0]["fp"] = "0" * 32  # a downgraded body
    rows.pop(1)  # a dropped function
    stale = asyncio.run(stale_functions(FakeDB(proc_rows=rows)))
    assert set(stale) == {f"{names[0]} (body differs from schema.sql)", f"{names[1]} (missing)"}
    assert asyncio.run(stale_functions(FakeDB())) == []


def test_unreadable_pg_proc_is_not_provably_current():
    stale = asyncio.run(stale_functions(FakeDB(proc_fail=True)))
    assert stale and all(s.endswith("(pg_proc unreadable)") for s in stale)


# --- the health route + startup log -------------------------------------------------------


def test_health_route_is_503_with_the_lists_when_behind(monkeypatch, two_migrations):
    from backend import main

    monkeypatch.setattr(main, "db", _ledger(BASELINE, "0001_a.sql"))
    resp = asyncio.run(main.health())
    assert resp.status_code == 503
    assert resp.body and b'"pending_migrations":["0002_b.sql"]' in resp.body

    monkeypatch.setattr(main, "db", _all_recorded())
    assert asyncio.run(main.health()) == {
        "status": "ok",
        "version": VERSION,
        "pending_migrations": [],
        "unknown_migrations": [],
        "stale_functions": [],
    }


def test_health_route_is_503_when_the_database_is_newer(monkeypatch, two_migrations):
    from backend import main

    monkeypatch.setattr(main, "db", _ledger(BASELINE, *two_migrations, "0003_newer.sql"))
    resp = asyncio.run(main.health())
    assert resp.status_code == 503
    assert b'"pending_migrations":[]' in resp.body and b'"unknown_migrations":["0003_newer.sql"]' in resp.body


def test_health_route_is_503_when_a_function_body_is_stale(monkeypatch):
    from backend import main

    rows = [{"proname": n, "fp": fingerprint(b)} for n, b in expected_functions().items()]
    rows[0]["fp"] = "f" * 32
    db = FakeDB(rows=_all_recorded().rows, proc_rows=rows)
    monkeypatch.setattr(main, "db", db)
    resp = asyncio.run(main.health())
    assert resp.status_code == 503
    assert b"body differs from schema.sql" in resp.body and b'"pending_migrations":[]' in resp.body


def test_log_pending_message_names_the_fix():
    msg = schema_check.log_pending(["0004_x.sql"])
    assert msg and "0004_x.sql" in msg and "apply_migrations.sh" in msg
    msg = schema_check.log_pending([], [], ["0009_newer.sql"])
    assert msg and "0009_newer.sql" in msg and "NEWER THAN THE CODE" in msg and "apply_migrations.sh" not in msg
    msg = schema_check.log_pending([], ["refresh_stale_datasets_stats (body differs from schema.sql)"])
    assert msg and "refresh_stale_datasets_stats" in msg and "functions.sql" in msg
    assert schema_check.log_pending([], []) is None
