#!/usr/bin/env bash
# A backup restores every row into an EMPTY database (the SDK helper refuses a schema-initialized
# one), and the migration runner that ends the restore brings it current.
# Usage: FRIDGE_DB_CONTAINER=<your-throwaway-container> scripts/test_backup_restore.sh
set -euo pipefail

CONTAINER="${FRIDGE_DB_CONTAINER:?set FRIDGE_DB_CONTAINER to your throwaway container}"
case "$CONTAINER" in
    fr-cx-*|fr-ci-*|fr-rv-*|fr-restore-test-*|ci-db) ;;
    *) echo "Refusing container '$CONTAINER'; use a fr-cx-*, fr-ci-*, fr-rv-*, fr-restore-test-* or ci-db throwaway" >&2; exit 1 ;;
esac
PGUSER_="${FRIDGE_DB_USER:-freedomridge}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEMA="$REPO/backend/db/schema.sql"
SRC_DB=_bkrt_src
DST_DB=_bkrt_dst
WORK=$(mktemp -d)
DUMP="$WORK/dump.sql.gz"

fail() { echo "FAIL: $*" >&2; exit 1; }
psql_() { docker exec -i "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$PGUSER_" "$@"; }
admin() { psql_ -d postgres -tAc "$1" >/dev/null; }

cleanup() {
    local status=$?
    trap - EXIT
    admin "DROP DATABASE IF EXISTS $SRC_DB" 2>/dev/null || status=1
    admin "DROP DATABASE IF EXISTS $DST_DB" 2>/dev/null || status=1
    rm -rf "$WORK" || status=1
    exit "$status"
}
trap cleanup EXIT

docker exec "$CONTAINER" true 2>/dev/null || fail "container '$CONTAINER' not running (set FRIDGE_DB_CONTAINER)"
[ -f "$SCHEMA" ] || fail "schema not found at $SCHEMA"

# Read the sidecar's REAL dump flags rather than duplicating them, so this test tracks whatever
# scripts/backup_loop.sh actually runs. If someone drops --clean, this test starts failing.
PGDUMP_FLAGS="$(sed -n 's/^PGDUMP_FLAGS="\(.*\)"$/\1/p' "$REPO/scripts/backup_loop.sh")"
[ -n "$PGDUMP_FLAGS" ] || fail "could not read PGDUMP_FLAGS from scripts/backup_loop.sh"
echo "== dump flags (from backup_loop.sh): $PGDUMP_FLAGS"

# The SDK path (python -m fridge.backup) builds its own pg_dump argv and is exercised separately by
# operators; assert it carries the same load-bearing flags so the two paths can't diverge silently.
grep -q -- '"--clean"' "$REPO/python/fridge/backup.py" && grep -q -- '"--if-exists"' "$REPO/python/fridge/backup.py" \
    || fail "python/fridge/backup.py lost --clean/--if-exists — its dumps will not restore"

echo "== building source DB ($SRC_DB) from schema.sql"
admin "DROP DATABASE IF EXISTS $SRC_DB"
admin "DROP DATABASE IF EXISTS $DST_DB"
admin "CREATE DATABASE $SRC_DB"
psql_ -d "$SRC_DB" -v ON_ERROR_STOP=1 -q < "$SCHEMA" >/dev/null || fail "schema.sql failed to load into $SRC_DB"
# A drifted function body in the dump: the migration runner that ends the restore must repair it.
psql_ -d "$SRC_DB" -q -c 'CREATE OR REPLACE FUNCTION mark_dataset_stats_stale() RETURNS TRIGGER AS $f$ BEGIN RETURN NEW; END; $f$ LANGUAGE plpgsql'

echo "== seeding"
psql_ -d "$SRC_DB" -v ON_ERROR_STOP=1 -q >/dev/null <<'SQL' || fail "seed failed"
INSERT INTO users (username, password_hash, role) VALUES ('bkrt_alice','x','power_user'), ('bkrt_bob','x','guest');
INSERT INTO datasets (name, description, owner_id) VALUES ('bkrt','backup round-trip',1);
INSERT INTO dataset_access (dataset_id, user_id, granted_by_user_id)
    SELECT d.id, u.id, 1 FROM datasets d, users u WHERE d.name='bkrt' AND u.username='bkrt_alice';
INSERT INTO samples (dataset_id, filepath, media_type, tags, metadata)
    SELECT d.id, 's3://bkrt/img_'||g||'.jpg', 'image', ARRAY['seed'],
           jsonb_build_object('idx', g, 'score', g * 0.5)
    FROM datasets d, generate_series(1, 1000) g WHERE d.name='bkrt';
SQL

counts() { psql_ -d "$1" -tAc \
    "SELECT (SELECT count(*) FROM users)||'/'||(SELECT count(*) FROM datasets)||'/'||(SELECT count(*) FROM samples)||'/'||(SELECT count(*) FROM dataset_access)"; }
SRC_COUNTS="$(counts "$SRC_DB" | tr -d '[:space:]')"
echo "   source users/datasets/samples/access = $SRC_COUNTS"
[ "$SRC_COUNTS" = "3/1/1000/1" ] || fail "seed produced unexpected counts: $SRC_COUNTS"

echo "== dumping (same flags as the sidecar)"
# Connect over the unix socket (no -h) so this works regardless of how the container's pg_hba
# authenticates TCP — CI's container sets POSTGRES_PASSWORD, the dev one may not.
# shellcheck disable=SC2086  # deliberate word-split of the flag list
docker exec -i "$CONTAINER" pg_dump -U "$PGUSER_" -d "$SRC_DB" $PGDUMP_FLAGS \
    | gzip -9 > "$DUMP" || fail "pg_dump failed"
[ -s "$DUMP" ] || fail "dump is empty"
gunzip -t "$DUMP" || fail "dump is not a valid gzip"

# Exercise the shipped helper (python/fridge/backup.py). Use the container's client on hosts
# without psql.
mkdir "$WORK/bin"
cat > "$WORK/bin/psql" <<'SH'
#!/bin/sh
exec docker exec -i -e PGPASSWORD -e PGOPTIONS -e PGAPPNAME -e PGCONNECT_TIMEOUT \
    "$FRIDGE_DB_CONTAINER" psql "$@"
SH
chmod +x "$WORK/bin/psql"
restore_sdk() {
    PATH="$WORK/bin:$PATH" PYTHONPATH="$REPO/python" \
        DATABASE_URL="postgresql://$PGUSER_:unused@/$DST_DB" \
        "${PY:-python3}" "$REPO/python/fridge/backup.py" --restore --input "$DUMP"
}
admin "CREATE DATABASE $DST_DB"
psql_ -d "$DST_DB" -q < "$SCHEMA" >/dev/null
if restore_sdk > "$WORK/refused.log" 2>&1; then
    fail "SDK must refuse a schema-initialized target"
fi
grep -q 'EMPTY database' "$WORK/refused.log" \
    || { cat "$WORK/refused.log" >&2; fail "SDK lacks the empty-target guard"; }

echo "== restoring into an EMPTY database"
admin "DROP DATABASE $DST_DB"
admin "CREATE DATABASE $DST_DB"
if ! restore_sdk > "$WORK/restore.log" 2>&1; then
    tail -20 "$WORK/restore.log" >&2
    fail "restore exited non-zero"
fi
if grep -q '^ERROR' "$WORK/restore.log"; then
    grep '^ERROR' "$WORK/restore.log" | head -10 >&2
    fail "restore reported SQL errors"
fi

DST_COUNTS="$(counts "$DST_DB" | tr -d '[:space:]')"
echo "   restored users/datasets/samples/access = $DST_COUNTS"
[ "$SRC_COUNTS" = "$DST_COUNTS" ] || fail "DATA LOST IN RESTORE: source $SRC_COUNTS vs restored $DST_COUNTS"

# Spot-check content, not just cardinality — a restore that produced the right number of wrong rows
# would still be a failed restore.
SAMPLE_CHECK="$(psql_ -d "$DST_DB" -tAc \
    "SELECT count(*) FROM samples WHERE filepath LIKE 's3://bkrt/img_%' AND metadata->>'idx' IS NOT NULL AND 'seed' = ANY(tags)" | tr -d '[:space:]')"
[ "$SAMPLE_CHECK" = "1000" ] || fail "restored sample CONTENT is wrong (matched $SAMPLE_CHECK of 1000)"

[ "$(psql_ -d "$DST_DB" -Atc "SELECT count(*) FROM schema_migrations WHERE filename = '0000_baseline.sql'")" = 1 ] \
    || fail "the restored ledger lost the baseline"
BODY="$(psql_ -d "$DST_DB" -Atc "SELECT prosrc FROM pg_proc WHERE proname = 'mark_dataset_stats_stale'")"
case "$BODY" in
    *record_stats_work*) ;;
    *) fail "the migration runner did not repair the drifted function body" ;;
esac
[ "$(counts "$DST_DB" | tr -d '[:space:]')" = "$SRC_COUNTS" ] || fail "migrations changed restored data"
echo "PASS: backup restored into an empty database and brought current by the migration runner"
