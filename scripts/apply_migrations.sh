#!/bin/bash
# Apply all pending database migrations

set -e

usage_error() {
    echo "Usage: $0 [--database-url URL | --database-url=URL | --database-url-env]" >&2
    echo "URL mode requires a postgres:// or postgresql:// URL; --database-url-env reads DATABASE_URL." >&2
    exit 2
}

# Parse everything before resolving a container or opening a database. A typo in a URL flag
# must never turn a drill migration into a migration of the default Compose database.
DIRECT_URL=""
case "$#" in
    0) ;;
    1)
        case "$1" in
            --database-url=*) DIRECT_URL="${1#*=}" ;;
            --database-url-env) DIRECT_URL="${DATABASE_URL:-}" ;;
            *) usage_error ;;
        esac
        [ -n "$DIRECT_URL" ] || usage_error
        ;;
    2)
        [ "$1" = "--database-url" ] || usage_error
        DIRECT_URL="$2"
        [ -n "$DIRECT_URL" ] || usage_error
        ;;
    *) usage_error ;;
esac
if [ "$#" -gt 0 ]; then
    [[ "$DIRECT_URL" =~ ^postgres(ql)?://[^[:space:]]+$ ]] || usage_error
    command -v psql >/dev/null || { echo "Error: install a matching PostgreSQL psql client first" >&2; exit 1; }
    URL_CLIENT="${BASH_SOURCE[0]%/*}/psql_url.py"
    PYTHON="${FRIDGE_PYTHON:-python3}"
    command -v "$PYTHON" >/dev/null || { echo "Error: URL mode requires Python 3" >&2; exit 1; }
    DATABASE_URL="$DIRECT_URL" "$PYTHON" "$URL_CLIENT" --check || usage_error
    if [ -n "${FRIDGE_TEST_MODE:-}${PYTEST_CURRENT_TEST:-}${FRIDGE_TEST_POSTGRES_CONTAINER:-}${FRIDGE_TEST_POSTGRES_DSN:-}" ]; then
        DATABASE_URL="$DIRECT_URL" "$PYTHON" "${BASH_SOURCE[0]%/*}/db_test_safety.py" url
    fi
fi

DB_CONTAINER=""
if [ -z "$DIRECT_URL" ]; then
    # Pytest children must never discover the deployment's Compose service. Also
    # protect standalone test/drill runners that supply the disposable target.
    if [ -n "${FRIDGE_TEST_MODE:-}${PYTEST_CURRENT_TEST:-}${FRIDGE_TEST_POSTGRES_CONTAINER:-}${FRIDGE_TEST_POSTGRES_DSN:-}" ]; then
        case "${FRIDGE_DB_CONTAINER:-}" in
            ''|freedomridge-db|/freedomridge-db|fr-drill|/fr-drill|fr-drill2|/fr-drill2)
                echo "Error: Unsafe test database target; set FRIDGE_DB_CONTAINER to a disposable container" >&2
                exit 2
                ;;
        esac
        PYTHON="${FRIDGE_PYTHON:-python3}"
        DB_CONTAINER=$("$PYTHON" "${BASH_SOURCE[0]%/*}/db_test_safety.py" container)
    else
        DB_CONTAINER="${FRIDGE_DB_CONTAINER:-$(docker compose -f docker-compose.prod.yml ps -q db)}"
    fi
    if [ -z "$DB_CONTAINER" ]; then
        echo "Error: Database container not found"
        exit 1
    fi
fi

echo "Applying database migrations..."
DB_NAME="${FRIDGE_DB_NAME:-freedomridge}"
DB_USER="${FRIDGE_DB_USER:-freedomridge}"
MIGRATION_PGOPTIONS=""
MIGRATION_PGAPPNAME="psql"
MIGRATION_PGCONNECT_TIMEOUT=0

# The SDK restores over DATABASE_URL; migrations must use that exact same target.
# Container-based deployments keep their existing socket connection.
psql_() {
    if [ -n "$DIRECT_URL" ]; then
        DATABASE_URL="$DIRECT_URL" PGOPTIONS="$MIGRATION_PGOPTIONS" \
            PGAPPNAME="$MIGRATION_PGAPPNAME" PGCONNECT_TIMEOUT="$MIGRATION_PGCONNECT_TIMEOUT" \
            "$PYTHON" "$URL_CLIENT" -X "$@"
    else
        docker exec -i -e PGCONNECT_TIMEOUT="$MIGRATION_PGCONNECT_TIMEOUT" \
            -e PGOPTIONS="$MIGRATION_PGOPTIONS" -e PGAPPNAME="$MIGRATION_PGAPPNAME" \
            "$DB_CONTAINER" psql -X -U "$DB_USER" -d "$DB_NAME" "$@"
    fi
}

# Apply each migration in order. Each runs in a single transaction (--single-transaction) with
# ON_ERROR_STOP=1, together with its ledger row, so a migration either fully applies and is
# recorded or fully rolls back and is not; any error aborts the run with a non-zero exit.
# Apply in BYTE order, whatever the operator's locale: locale collation can ignore punctuation
# and put 0010_b before 0010-a, or a suffixed hot-fix before the file it fixes.
export LC_ALL=C
shopt -s nullglob

# A database created from backend/db/schema.sql records this baseline (the 1.0.0 schema) plus
# every later file; backend/db/migrations holds the changes since 1.0.0. Keep in step with
# backend/schema_check.py.
BASELINE=0000_baseline.sql
# Only stdout is the ledger: psql's stderr (its error, or a server warning such as a collation
# version mismatch) goes straight to the terminal.
if ! RECORDED=$(psql_ -v ON_ERROR_STOP=1 -Atc "SELECT filename FROM schema_migrations"); then
    echo "  ERROR: cannot read the schema_migrations ledger (above); nothing was applied." >&2
    echo "  A new database is created from backend/db/schema.sql." >&2
    exit 1
fi
if ! printf '%s\n' "$RECORDED" | grep -qxF "$BASELINE"; then
    echo "  ERROR: this database was not created from backend/db/schema.sql (its ledger has no $BASELINE); nothing was applied" >&2
    exit 1
fi
KNOWN=$(printf '%s\n' "$BASELINE"; for migration in backend/db/migrations/*.sql; do basename "$migration"; done)
UNKNOWN=$(printf '%s\n' "$RECORDED" | grep -vxF -f <(printf '%s\n' "$KNOWN") || true)
if [ -n "$UNKNOWN" ]; then
    echo "  ERROR: the database records migrations this code does not have: ${UNKNOWN//$'\n'/ }" >&2
    echo "  It is newer than this code (a newer version ran against it, or a newer backup was restored); nothing was applied." >&2
    exit 1
fi

APPLIED=()
SKIPPED=0
for migration in backend/db/migrations/*.sql; do
    filename=$(basename "$migration")
    if printf '%s\n' "$RECORDED" | grep -qxF "$filename"; then
        SKIPPED=$((SKIPPED + 1))
        continue
    fi
    echo "Applying $filename..."
    # The file and its ledger row commit TOGETHER (one psql, one transaction): if any statement
    # fails, Postgres rolls back the whole file and the row with it, so a half-applied or
    # rolled-back file can never be recorded as applied.
    if err=$({ cat "$migration"; printf "\nINSERT INTO schema_migrations (filename) VALUES ('%s') ON CONFLICT (filename) DO NOTHING;\n" "$filename"; } \
             | psql_ --single-transaction -v ON_ERROR_STOP=1 -q 2>&1); then
        APPLIED+=("$filename")
    else
        echo "  ERROR applying $filename — the file was rolled back and is NOT recorded as applied:" >&2
        echo "$err" >&2
        exit 1
    fi
done
# Function bodies are owned by backend/db/schema.sql; functions.sql is the same definitions
# extracted (see backend/schema_check.py). Re-applying them is idempotent and undoes any drift —
# a hand edit, a file re-executed out of order — which is exactly what GET /api/health's
# stale_functions check reports. This is what repairs it.
echo "Re-applying function definitions from backend/db/functions.sql..."
psql_ --single-transaction -v ON_ERROR_STOP=1 -q \
    < backend/db/functions.sql || { echo "  ERROR re-applying backend/db/functions.sql" >&2; exit 1; }
[ ${#APPLIED[@]} -gt 0 ] && echo "Recorded ${#APPLIED[@]} migration file(s) in schema_migrations"
[ "$SKIPPED" -gt 0 ] && echo "Skipped $SKIPPED file(s) already recorded in schema_migrations"
echo "✅ All migrations applied successfully"
