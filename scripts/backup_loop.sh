#!/bin/sh
# Scheduled logical database backup (pg_dump) — the self-hosted / single-EC2 floor against data loss.
#
# Backs up whatever DATABASE_URL points at: the in-container Postgres today, OR a managed RDS endpoint
# later (pg_dump works against RDS too). On RDS you ALSO get native automated backups + PITR, so you
# can keep this sidecar for portable off-instance dumps or retire it. See docs/DEPLOY_TO_AWS.md.
#
# Plain SQL + gzip, so it restores with the existing tooling:
#   gunzip -c freedomridge_<ts>.sql.gz | psql -v ON_ERROR_STOP=1 "$DATABASE_URL"
#   (or: python -m fridge.backup --restore -i <file>)
#
# Full dumps retain --clean --if-exists, but restore into an EMPTY database: newer tables
# created by schema.sql may have foreign keys the dump cannot drop. After restoring with
# ON_ERROR_STOP=1, ANALYZE, verify and run scripts/apply_migrations.sh before starting services.
# See docs/BACKUP_AND_MIGRATION.md. Guarded by scripts/test_backup_restore.sh.
#
# NOTE: no `set -e` — a failed cycle must NOT kill the daemon; it logs loudly and retries next cycle.
set -u
set -o pipefail 2>/dev/null || true  # busybox ash supports it; so a failed pg_dump isn't masked by gzip

: "${DATABASE_URL:?DATABASE_URL must be set}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"   # default daily
KEEP="${BACKUP_KEEP:-7}"                        # retain the newest N local dumps
S3_URI="${BACKUP_S3_URI:-}"                     # optional off-instance copy, e.g. s3://bucket/prefix

# Declared on its own line so scripts/test_backup_restore.sh can read the REAL flags out of this
# file and round-trip with them — the test then tracks whatever the sidecar actually does, instead
# of asserting against its own hard-coded copy. See the --clean/--if-exists rationale above.
PGDUMP_FLAGS="--no-owner --no-acl --clean --if-exists"

mkdir -p "$BACKUP_DIR"
echo "[backup] start: interval=${INTERVAL}s keep=${KEEP} dir=${BACKUP_DIR} s3=${S3_URI:-<none>}"

while true; do
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    out="${BACKUP_DIR}/freedomridge_${ts}.sql.gz"
    tmp="${out}.partial"
    echo "[backup] ${ts} dumping -> ${out}"
    # Dump to a .partial first; only an atomic rename promotes it, so a crash mid-dump can never
    # leave a truncated file that looks like a good backup. gunzip -t + -s validate completeness.
    # shellcheck disable=SC2086  # PGDUMP_FLAGS is a deliberate word-split list of flags
    if pg_dump "$DATABASE_URL" $PGDUMP_FLAGS | gzip -9 > "$tmp" &&
        gunzip -t "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
        mv -f "$tmp" "$out"
        echo "[backup] ok: ${out} ($(du -h "$out" | cut -f1))"
        if [ -n "$S3_URI" ]; then
            if aws s3 cp "$out" "${S3_URI%/}/freedomridge_${ts}.sql.gz"; then
                echo "[backup] synced -> ${S3_URI%/}/freedomridge_${ts}.sql.gz"
            else
                echo "[backup] WARNING: S3 upload failed (local copy retained)"
            fi
        fi
        # Retention: delete all but the newest $KEEP local dumps. Failure here is non-fatal.
        ls -1t "${BACKUP_DIR}"/freedomridge_*.sql.gz "${BACKUP_DIR}"/aurora_*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
            rm -f "$old" && echo "[backup] pruned ${old}"
        done
    else
        rm -f "$tmp"
        echo "[backup] ERROR: pg_dump failed (server unreachable / version mismatch / disk) — retrying next cycle"
    fi
    sleep "$INTERVAL"
done
