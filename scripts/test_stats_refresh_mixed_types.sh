#!/usr/bin/env bash
set -euo pipefail
: "${FRIDGE_TEST_POSTGRES_DSN:?Set FRIDGE_TEST_POSTGRES_DSN to an explicitly disposable PostgreSQL database}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec "${PYTHON:-python3}" -m pytest -q backend/tests/test_statistics_isolation.py "$@"
