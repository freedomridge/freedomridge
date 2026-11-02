#!/usr/bin/env bash
# Dev backend launcher. Loads .env and uses explicit ALLOWED_ORIGINS, defaulting
# to localhost. For remote development, list the frontend host's origins in .env, e.g.
# ALLOWED_ORIGINS=http://localhost:3000,http://dev.example.test:3000
# Or opt into peer discovery with FRIDGE_TAILSCALE_ORIGINS=1 (see docs/QUICK_START.md,
# "Browser origins for remote development").
# Usage: ./scripts/run_backend.sh (with your virtual environment active).
# For production use docs/DOCKER_DEPLOYMENT.md or docs/DEPLOY_TO_AWS.md, which set
# FRIDGE_ENV=production, STORAGE_ENCRYPTION_KEY and only the deployed frontend origin.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-$SCRIPT_DIR/..}"
cd "$ROOT_DIR" || { echo "run_backend: cannot cd to $ROOT_DIR" >&2; exit 1; }

# 1. Load .env (POSTGRES_*, S3_*, SESSION_COOKIE_SECURE, base ALLOWED_ORIGINS, ...).
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

FRONTEND_PORT="${FRIDGE_FRONTEND_PORT:-3000}"
WORKERS="${FRIDGE_WORKERS:-4}"

# 2. Use the configured allowlist or the local development origin.
ORIGINS="${ALLOWED_ORIGINS:-http://localhost:${FRONTEND_PORT}}"

# Optional, best-effort discovery on every launch. Keep the status parsing and
# append order compatible with the original launcher; do not sort or deduplicate.
# No set -e/pipefail: discovery failure must not prevent the backend from starting.
if [ "${FRIDGE_TAILSCALE_ORIGINS:-0}" = "1" ] && command -v tailscale >/dev/null 2>&1; then
    TS_STATUS="$(tailscale status 2>/dev/null)"
    DNS_SUFFIX="$(printf '%s\n' "$TS_STATUS" | awk '$3 ~ /\.ts\.net$/ { sub(/^[^.]+\./, "", $3); print $3; exit }')"
    # Split on whitespace without expanding globs against files in the working directory.
    read -r -a SKIP_PATTERNS <<< "${FRIDGE_TAILSCALE_SKIP:-}"
    while read -r ip host; do
        [ -z "$ip" ] && continue
        for pattern in "${SKIP_PATTERNS[@]}"; do
            case "$host" in $pattern) continue 2 ;; esac
        done
        ORIGINS="${ORIGINS},http://${ip}:${FRONTEND_PORT},http://${host}:${FRONTEND_PORT}"
        [ -n "$DNS_SUFFIX" ] && ORIGINS="${ORIGINS},http://${host}.${DNS_SUFFIX}:${FRONTEND_PORT}"
    done < <(printf '%s\n' "$TS_STATUS" | awk '$1 ~ /^100\./ && NF>=2 { print $1, $2 }')
fi

export ALLOWED_ORIGINS="$ORIGINS"
export SESSION_COOKIE_SECURE="${SESSION_COOKIE_SECURE:-false}"
export FRIDGE_ENV="${FRIDGE_ENV:-development}"

echo "Freedom Ridge backend starting — $(printf '%s' "$ORIGINS" | tr ',' '\n' | grep -c .) configured allowed origins."
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import uvicorn' >/dev/null 2>&1; then
    echo "run_backend: 'python3' with the uvicorn module is required — activate your venv first." >&2
    exit 127
fi
# Keep a copy of everything the backend prints (incl. the traceback a 500's error_id points at) in a
# size-rotated file (50 MB x 5) — it logged only to this terminal. The terminal output is unchanged.
# FRIDGE_LOG_FILE= (empty) turns it off.
LOG_FILE="${FRIDGE_LOG_FILE-logs/backend.log}"
if [ -n "$LOG_FILE" ]; then
    exec > >(python3 "$SCRIPT_DIR/logtee.py" "$LOG_FILE") 2>&1
    echo "Backend log: $LOG_FILE"
fi
exec python3 -m backend.launch -- python3 -m uvicorn backend.main:app --host "${FRIDGE_BACKEND_HOST:-0.0.0.0}" --port "${FRIDGE_BACKEND_PORT:-8000}" --workers "$WORKERS"
