"""Is the database current with the code? Two checks, used at startup (loud log) and by
GET /api/health (503 + the lists), so a backend started against a stale database is caught by the
deploy checklist / load-balancer health check instead of 500ing one feature at a time:

1. The ``schema_migrations`` ledger. ``schema.sql`` is the full schema and records :data:`BASELINE`
   (the 1.0.0 schema) plus every migration file; backend/db/migrations/*.sql holds the changes since.
   ``pending_migrations()``: files not recorded yet (a restart onto a database that is missing a
   migration), or a database not created from ``schema.sql`` at all. ``unknown_migrations()``:
   recorded files this code doesn't have, i.e. a database newer than the code (an older version
   started after an upgrade, or a newer backup restored).
2. ``stale_functions()`` — the body of every function ``schema.sql`` defines vs ``pg_proc``.
   The ledger only knows which FILES ran, not whether something rewrote a function since: a hand
   edit, or a file re-executed out of order. With an "ok" ledger /api/health would otherwise pass a
   function that silently drops fields from every dataset's stats. A body fingerprint catches that.

``schema.sql`` is the source of truth for function bodies. ``backend/db/functions.sql`` is the
same definitions extracted (``python -m backend.schema_check --emit-functions``) so that
``scripts/apply_migrations.sh`` can re-apply them, idempotently, at the end of every run — the
repair for whatever the fingerprint check reports. ``test_schema_check`` keeps the two in sync.

CLI: ``python -m backend.schema_check <dsn>`` prints the lists as JSON and exits 1 when any is
non-empty.
"""

import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("fridge.schema")

DB_DIR = Path(__file__).resolve().parent / "db"
MIGRATIONS_DIR = DB_DIR / "migrations"
SCHEMA_SQL = DB_DIR / "schema.sql"
FUNCTIONS_SQL = DB_DIR / "functions.sql"
# The 1.0.0 schema's ledger row. Keep in step with schema.sql's seed and scripts/apply_migrations.sh.
BASELINE = "0000_baseline.sql"
NOT_FROM_SCHEMA = f"{BASELINE} (this database was not created from backend/db/schema.sql)"
LEDGER_MISSING = "schema_migrations (ledger table missing: create the database from backend/db/schema.sql)"

# One CREATE [OR REPLACE] FUNCTION statement: name, the dollar-quote tag, the body between the
# tags (byte-for-byte what pg_proc.prosrc stores), through the terminating semicolon.
_FUNCTION_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(.*?\)"
    r".*?\bAS\s+(\$[A-Za-z_]*\$)(.*?)\2[^;]*;",
    re.S | re.I,
)
_FUNCTIONS_SQL_HEADER = (
    "-- GENERATED from backend/db/schema.sql — do not edit by hand.\n"
    "-- Regenerate after changing a function in schema.sql:\n"
    "--     python -m backend.schema_check --emit-functions > backend/db/functions.sql\n"
    "-- scripts/apply_migrations.sh re-applies this file at the end of every run (CREATE OR REPLACE is\n"
    "-- idempotent), so a function body that drifted from schema.sql — a re-executed old migration, a\n"
    "-- hand edit — is restored by the next run. GET /api/health reports the drift (stale_functions).\n"
)


def migration_files() -> list[str]:
    """Every migration file the code ships, in the runner's (byte) order."""
    return sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))


async def _recorded(db: Any) -> Optional[set[str]]:
    try:
        rows = await db.fetch("SELECT filename FROM schema_migrations")
    except Exception as e:  # relation does not exist, or no DB — either way not provably current
        logger.warning("schema_migrations ledger unreadable: %s", e)
        return None
    return {r["filename"] for r in rows}


async def pending_migrations(db: Any) -> list[str]:
    """Migration files not recorded as applied. A database without the ledger table, or whose
    ledger lacks the baseline, was not created from schema.sql: no migration file can fix that."""
    applied = await _recorded(db)
    if applied is None:
        return [LEDGER_MISSING]
    if BASELINE not in applied:
        return [NOT_FROM_SCHEMA]
    return [f for f in migration_files() if f not in applied]


async def unknown_migrations(db: Any) -> list[str]:
    """Recorded migrations this code doesn't ship: the database is newer than the code. Only for a
    database on the baseline; one without it is reported by pending_migrations instead."""
    applied = await _recorded(db)
    if applied is None or BASELINE not in applied:
        return []
    return sorted(applied - {BASELINE, *migration_files()})


def function_statements(sql_text: Optional[str] = None) -> list[str]:
    """The full CREATE OR REPLACE FUNCTION statements of schema.sql (or ``sql_text``), in order."""
    text = SCHEMA_SQL.read_text() if sql_text is None else sql_text
    return [m.group(0) for m in _FUNCTION_RE.finditer(text)]


def expected_functions(sql_text: Optional[str] = None) -> dict[str, str]:
    """name -> body for every function schema.sql (or ``sql_text``) defines; a later definition
    of the same name wins. The body is the text between the dollar quotes, exactly what
    ``pg_proc.prosrc`` holds, so ``md5(prosrc)`` in SQL equals :func:`fingerprint` of it."""
    text = SCHEMA_SQL.read_text() if sql_text is None else sql_text
    return {m.group(1): m.group(3) for m in _FUNCTION_RE.finditer(text)}


def fingerprint(body: str) -> str:
    return hashlib.md5(body.encode("utf-8")).hexdigest()


def functions_sql() -> str:
    """The content of backend/db/functions.sql: schema.sql's function definitions, verbatim."""
    return _FUNCTIONS_SQL_HEADER + "\n" + "\n\n".join(function_statements()) + "\n"


async def stale_functions(db: Any) -> list[str]:
    """Functions whose live body differs from schema.sql (or is missing), as
    ``"name (missing)"`` / ``"name (body differs from schema.sql)"``."""
    expected = expected_functions()
    try:
        rows = await db.fetch(
            "SELECT proname, md5(prosrc) AS fp FROM pg_proc WHERE proname = ANY($1::text[])",
            list(expected),
        )
    except Exception as e:  # no DB — not provably current
        logger.warning("pg_proc unreadable: %s", e)
        return [f"{name} (pg_proc unreadable)" for name in sorted(expected)]
    live: dict[str, set[str]] = {}
    for r in rows:
        live.setdefault(r["proname"], set()).add(r["fp"])
    stale = []
    for name, body in expected.items():
        if name not in live:
            stale.append(f"{name} (missing)")
        elif fingerprint(body) not in live[name]:
            stale.append(f"{name} (body differs from schema.sql)")
    return stale


def log_pending(
    pending: list[str],
    stale: Optional[list[str]] = None,
    unknown: Optional[list[str]] = None,
) -> Optional[str]:
    """Log the startup verdict; returns the message when something is pending, stale or unknown."""
    stale = stale or []
    unknown = unknown or []
    if unknown:
        msg = (
            f"DATABASE IS NEWER THAN THE CODE: it records {len(unknown)} migration(s) this version doesn't have: "
            f"{', '.join(unknown)}. A newer version ran against it, or a newer backup was restored. Run that "
            "version, or restore a backup taken with this one. Until then GET /api/health returns 503."
        )
        logger.error(msg)
        return msg
    if pending and pending[0] in (LEDGER_MISSING, NOT_FROM_SCHEMA):
        msg = (
            f"DATABASE WAS NOT CREATED FROM backend/db/schema.sql: {pending[0]}. No migration can fix that: "
            "create a new database from schema.sql, or restore a backup taken with this version. "
            "Until then GET /api/health returns 503."
        )
        logger.error(msg)
        return msg
    if not pending and not stale:
        logger.info(
            "schema check: database is current (%d migration files, %d function bodies)",
            len(migration_files()),
            len(expected_functions()),
        )
        return None
    parts = []
    if pending:
        parts.append(f"{len(pending)} migration(s) pending: {', '.join(pending)}")
    if stale:
        parts.append(f"{len(stale)} function body(ies) differ from schema.sql: {', '.join(stale)}")
    msg = (
        "DATABASE SCHEMA IS BEHIND THE CODE: " + "; ".join(parts) + ". "
        "Run scripts/apply_migrations.sh (it applies pending files AND re-applies backend/db/functions.sql; "
        "no restart needed). Until then GET /api/health returns 503 and features touching the stale "
        "schema will fail or return wrong data."
    )
    logger.error(msg)
    return msg


def _main(argv: list[str]) -> int:
    if argv[1:] == ["--emit-functions"]:
        sys.stdout.write(functions_sql())
        return 0
    if len(argv) != 2:
        sys.stderr.write("usage: python -m backend.schema_check <postgres dsn> | --emit-functions\n")
        return 2
    import asyncio

    import asyncpg

    async def run() -> tuple[list[str], list[str], list[str]]:
        conn = await asyncpg.connect(argv[1])
        try:
            return await pending_migrations(conn), await unknown_migrations(conn), await stale_functions(conn)
        finally:
            await conn.close()

    pending, unknown, stale = asyncio.run(run())
    print(json.dumps({"pending_migrations": pending, "unknown_migrations": unknown, "stale_functions": stale}))
    return 1 if pending or unknown or stale else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
