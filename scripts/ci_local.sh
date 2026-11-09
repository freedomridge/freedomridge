#!/usr/bin/env bash
# Runs the jobs of .github/workflows/ci.yml on this machine against one commit, in a fresh clone, and
# prints a summary. CONTRIBUTING.md ("Checks") describes the same steps by hand.
#
# Usage: scripts/ci_local.sh <commit> [jobs]
#   commit  anything `git rev-parse` resolves in this repository, such as origin/my-branch
#   jobs    a comma list of lint, example-tests, backend-tests, db-recovery, frontend, quality, e2e
#           (default: all of them)
# Run it in an ordinary shell, not under nohup: a launcher test fails when SIGHUP is ignored.
#
# Environment:
#   CI_LOCAL_DIR     where the clone and its logs go (default: ${TMPDIR:-/tmp}/freedomridge-ci). A clone
#                    with its environments and node_modules takes about 3 GB; delete it once you have
#                    read the logs.
#   CI_LOCAL_PORT    two digits NN: the run's database, API and frontend listen on loopback ports 554NN,
#                    80NN and 30NN (default 93). Give runs at the same time different values.
#   NODE_BIN         a directory to put first on PATH, for the Node major version that ci.yml sets up.
#   MAPBOX_ENV_FILE  an .env file with NEXT_PUBLIC_MAPBOX_TOKEN, so the map e2e test runs instead of
#                    skipping itself. The token goes only into this run's frontend build. Its URL
#                    restrictions must allow localhost; an e2e run checks a map tile first.
#   BACKEND_TESTS    pytest arguments to run instead of the whole backend suite (skips the SDK suites).
#   E2E_SPECS        Playwright spec files to run instead of the whole e2e suite.
#
# Differences from GitHub Actions:
# - Every database is a new container on a loopback port, kept in memory. DATABASE_URL, FRIDGE_DATA_ROOT
#   and the e2e URLs point at this run's database, clone and servers, and the run refuses ports that are
#   already in use, so nothing reaches a database or server holding real data.
# - The jobs run one after another, and the Python jobs share uv environments synced from uv.lock.
# - e2e serves a production build with `next start` instead of Playwright starting `next dev`.
#
# The steps follow ci.yml. When ci.yml changes, update them to match and set CI_YML_SHA256 to its
# `sha256sum`; a run stops while the two disagree.
CI_YML_SHA256=a6b8dc7a5447f25e5c7f56aec8ef861ca650cfb1ce44218385db368cdff280d3
set -u

JOBS=${2:-lint,example-tests,backend-tests,db-recovery,frontend,quality,e2e}
P=${CI_LOCAL_PORT:-93}
[[ $P =~ ^[0-9][0-9]$ ]] || { echo "CI_LOCAL_PORT must be two digits" >&2; exit 2; }
DBPORT=554$P APIPORT=80$P FEPORT=30$P
for port in "$DBPORT" "$APIPORT" "$FEPORT"; do
  if (exec 3<> "/dev/tcp/127.0.0.1/$port") 2> /dev/null; then
    echo "Port $port is in use; choose another CI_LOCAL_PORT" >&2
    exit 2
  fi
done
for job in ${JOBS//,/ }; do
  case $job in
    lint | example-tests | backend-tests | db-recovery | frontend | quality | e2e) ;;
    *) echo "Unknown job: $job" >&2; exit 2 ;;
  esac
done

# Clone the commit, then run the copy of this script it contains, whose steps match its ci.yml.
if [ -z "${CI_LOCAL_CLONE:-}" ]; then
  [ $# -ge 1 ] || { echo "Usage: scripts/ci_local.sh <commit> [jobs]" >&2; exit 2; }
  src=$(cd "$(dirname "$0")/.." && pwd)
  commit=$(git -C "$src" rev-parse --verify --quiet "$1^{commit}") || { echo "Not a commit here: $1" >&2; exit 2; }
  root=${CI_LOCAL_DIR:-${TMPDIR:-/tmp}/freedomridge-ci}
  clone=$root/$(date +%Y%m%d-%H%M%S)-${commit:0:9}
  mkdir -p "$root" || exit 1
  git clone -q --no-hardlinks "$src" "$clone" && git -C "$clone" checkout -q --detach "$commit" || exit 1
  [ -f "$clone/scripts/ci_local.sh" ] || { echo "$1 has no scripts/ci_local.sh; rebase it onto main" >&2; exit 2; }
  CI_LOCAL_CLONE=$clone exec bash "$clone/scripts/ci_local.sh" "$@"
fi

W=$CI_LOCAL_CLONE
L=$W-logs
DB=fr-ci-${W##*/}
cd "$W" || exit 1
mkdir -p "$L" "$W/data"
: > "$L/summary"

actual=$(sha256sum .github/workflows/ci.yml | cut -d' ' -f1)
if [ "$actual" != "$CI_YML_SHA256" ]; then
  echo "ci.yml changed after scripts/ci_local.sh last followed it. Update the steps here to match it," >&2
  echo "then set CI_YML_SHA256=$actual" >&2
  exit 2
fi

failed=0
result() { # result <step> <exit status> [detail]
  local outcome=ok
  [ "$2" = 0 ] || { outcome="FAILED ($2)"; failed=$((failed + 1)); }
  echo "$(date +%H:%M:%S) $1: $outcome${3:+ - $3}" | tee -a "$L/summary"
}
want() { case ",$JOBS," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

[ -n "${NODE_BIN:-}" ] && export PATH=$NODE_BIN:$PATH
if want frontend || want quality || want e2e; then
  node_wanted=$(sed -n 's/^ *node-version: *//p' .github/workflows/ci.yml | sort -u)
  node_found=$(node --version 2> /dev/null | sed 's/^v\([0-9]*\).*/\1/')
  if [ "$node_found" != "$node_wanted" ]; then
    echo "ci.yml uses Node $node_wanted but node here is ${node_found:-missing}; set NODE_BIN" >&2
    exit 2
  fi
fi
if [ -n "${MAPBOX_ENV_FILE:-}" ]; then
  NEXT_PUBLIC_MAPBOX_TOKEN=$(sed -n 's/^NEXT_PUBLIC_MAPBOX_TOKEN=//p' "$MAPBOX_ENV_FILE" | head -1 | tr -d "\"'")
  export NEXT_PUBLIC_MAPBOX_TOKEN
  # A token's URL restrictions apply to map tiles, not to the style. When every tile is refused, the map
  # often never asks for its points and the map e2e tests fail now and then (a quarter of the runs).
  if want e2e; then
    tile=$(curl -s -o /dev/null -w '%{http_code}' -H "Referer: http://localhost:$FEPORT/" \
      "https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/0/0/0.vector.pbf?access_token=$NEXT_PUBLIC_MAPBOX_TOKEN")
    if [ "$tile" != 200 ]; then
      echo "Mapbox answers $tile for a map tile on http://localhost:$FEPORT with the token in $MAPBOX_ENV_FILE." >&2
      echo "Use a token whose URL restrictions allow localhost, or leave MAPBOX_ENV_FILE unset." >&2
      exit 2
    fi
  fi
fi

unset PYTHONPATH
export FRIDGE_DB_CONTAINER=$DB FRIDGE_DB_USER=freedomridge FRIDGE_DATA_ROOT=$W/data
export DATABASE_URL=postgres://freedomridge:password@127.0.0.1:$DBPORT/freedomridge
IMAGE=freedomridge-ci-db:$(sha256sum Dockerfile.db | cut -c1-12)

BACKEND= FRONTEND=
stop_group() { # stop_group <process group leader>
  [ -n "$1" ] || return 0
  kill -TERM -- "-$1" 2> /dev/null
  sleep 3
  kill -KILL -- "-$1" 2> /dev/null
}
cleanup() {
  stop_group "$BACKEND"
  stop_group "$FRONTEND"
  docker rm -fv "$DB" > /dev/null 2>&1
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP

start_db() { # a fresh database from schema.sql, as each ci.yml job starts one
  docker rm -fv "$DB" > /dev/null 2>&1
  docker run -d --name "$DB" --tmpfs /var/lib/postgresql/data:rw,size=12g \
    -e POSTGRES_USER=freedomridge -e POSTGRES_PASSWORD=password -e POSTGRES_DB=freedomridge \
    -p "127.0.0.1:$DBPORT:5432" "$IMAGE" > /dev/null &&
    timeout 90 bash -c "until docker exec -e PGPASSWORD=password $DB psql -h 127.0.0.1 -U freedomridge -d freedomridge -c 'SELECT 1' >/dev/null 2>&1; do sleep 1; done" &&
    docker exec -i "$DB" psql -U freedomridge -d freedomridge -v ON_ERROR_STOP=1 -q < backend/db/schema.sql
}
start_backend() { # start_backend <log name> <environment bin directory> [VAR=value ...]
  local name=$1 bin=$2
  shift 2
  env "$@" SESSION_COOKIE_SECURE=false STORAGE_ENCRYPTION_KEY=ci-only-not-a-secret \
    ALLOWED_ORIGINS="http://localhost:$FEPORT,http://127.0.0.1:$FEPORT" \
    setsid "$bin/python" -m backend.launch -- "$bin/python" -m uvicorn backend.main:app \
    --host 127.0.0.1 --port "$APIPORT" > "$L/$name-backend.log" 2>&1 &
  BACKEND=$!
  timeout 60 bash -c "until curl -sf http://127.0.0.1:$APIPORT/api/datasets >/dev/null; do sleep 1; done"
}
sync_env() { # sync_env <name> <uv sync arguments>
  local name=$1
  shift
  UV_PROJECT_ENVIRONMENT=$W/.venv-$name uv sync --frozen "$@" > "$L/uv-$name.log" 2>&1
  result "uv sync $name" $?
}
npm_installed=
npm_ci() {
  [ -n "$npm_installed" ] && return 0
  npm_installed=1
  (cd frontend && npm ci --no-audit --no-fund) > "$L/npm-ci.log" 2>&1
  result "npm ci" $?
}

CPU=$W/.venv-cpu/bin LEAN=$W/.venv-lean/bin E2EV=$W/.venv-e2e/bin
if want lint || want example-tests || want quality; then sync_env cpu --extra cpu; fi
if want backend-tests || want db-recovery; then sync_env lean --no-default-groups --group dev; fi
if want e2e; then sync_env e2e --no-default-groups; fi
if want backend-tests || want db-recovery || want e2e; then
  docker build -q -f Dockerfile.db -t "$IMAGE" . > "$L/db-image.log" 2>&1
  status=$?
  result "database image" $status
  [ $status = 0 ] || exit 1
fi

if want lint; then
  ("$CPU/ruff" check backend/ python/ scripts/ examples/ &&
    "$CPU/ruff" format --check backend/ python/ scripts/ examples/ &&
    "$CPU/flake8" backend/ python/ scripts/ examples/ &&
    uv build --out-dir "$L/sdk" python/ &&
    "$CPU/twine" check --strict "$L"/sdk/*) > "$L/lint.log" 2>&1
  result lint $?
fi

if want example-tests; then
  (env -u PYTHONPATH -u FRIDGE_DATA_ROOT "$CPU/python" -m pytest -p no:cacheprovider \
    examples/test_review_regressions.py -k script_help -q &&
    env -u FRIDGE_DATA_ROOT PYTHONPATH=python:. HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=4 \
      "$CPU/python" -m pytest -p no:cacheprovider examples backend/tests/test_encoders.py backend/tests/test_patch_vectors.py -q) \
    > "$L/example-tests.log" 2>&1
  result example-tests $? "$(tail -1 "$L/example-tests.log")"
fi

if want backend-tests; then
  start_db > "$L/backend-tests-db.log" 2>&1
  result "backend-tests database" $?
  # shellcheck disable=SC2086 # BACKEND_TESTS holds several arguments
  FRIDGE_TEST_POSTGRES_DSN=$DATABASE_URL FRIDGE_TEST_POSTGRES_CONTAINER=$DB \
    "$LEAN/python" -m pytest -q -p no:cacheprovider ${BACKEND_TESTS:-} > "$L/backend-tests.log" 2>&1
  result "backend-tests pytest" $? "$(tail -1 "$L/backend-tests.log")"
  if [ -z "${BACKEND_TESTS:-}" ]; then
    start_backend sdk "$LEAN"
    result "backend-tests backend start" $?
    docker exec "$DB" psql -U freedomridge -d freedomridge -qc \
      "UPDATE users SET must_change_password = false WHERE username = 'admin';"
    for suite in test_sdk test_integrity test_map_contract test_objects test_synth_integration; do
      FRIDGE_TEST_URL=http://127.0.0.1:$APIPORT/api "$LEAN/python" -m "fridge.$suite" > "$L/sdk-$suite.log" 2>&1
      result "backend-tests $suite" $? "$(grep -E 'passed|failed' "$L/sdk-$suite.log" | tail -1)"
    done
    stop_group "$BACKEND"
    BACKEND=
  fi
fi

if want db-recovery; then
  # The steps of ci.yml's db-recovery job, copied by hand: when a change to ci.yml touches that job,
  # diff this block against it as well.
  start_db > "$L/db-recovery-db.log" 2>&1
  result "db-recovery database" $?
  (
    set -eo pipefail
    python=$LEAN/python
    sql() { docker exec -i "$DB" psql -U freedomridge -d freedomridge -v ON_ERROR_STOP=1 -q -c "$1"; }
    value() { docker exec "$DB" psql -U freedomridge -d freedomridge -Atc "$1"; }

    echo "== A fresh install has nothing to apply and is current"
    ./scripts/apply_migrations.sh
    "$python" -m backend.schema_check "$DATABASE_URL"

    echo "== A new migration applies once, is skipped after, and the code without it refuses the database"
    printf 'ALTER TABLE samples ADD COLUMN ci_canary TEXT;\n' > backend/db/migrations/9001_ci_canary.sql
    ./scripts/apply_migrations.sh | tee "$L/db-first-run.log"
    grep -q "^Applying 9001_ci_canary.sql" "$L/db-first-run.log"
    test "$(value "SELECT count(*) FROM schema_migrations WHERE filename='9001_ci_canary.sql'")" = 1
    ./scripts/apply_migrations.sh | tee "$L/db-second-run.log"
    grep -q "Skipped $(ls backend/db/migrations/*.sql | wc -l) file(s) already recorded" "$L/db-second-run.log"
    if grep -qE "^Applying [^ ]+\.sql" "$L/db-second-run.log"; then echo "second run re-applied a file"; exit 1; fi
    rm backend/db/migrations/9001_ci_canary.sql
    if "$python" -m backend.schema_check "$DATABASE_URL"; then echo "a newer database must be reported"; exit 1; fi
    if ./scripts/apply_migrations.sh 2> "$L/db-refused.log"; then echo "runner must refuse a newer database"; exit 1; fi
    grep -q "does not have: 9001_ci_canary.sql" "$L/db-refused.log"
    sql "ALTER TABLE samples DROP COLUMN ci_canary; DELETE FROM schema_migrations WHERE filename='9001_ci_canary.sql'"
    "$python" -m backend.schema_check "$DATABASE_URL"

    echo "== A migration file that fails is NOT recorded as applied"
    printf 'CREATE INDEX idx_samples_dataset_id ON samples(dataset_id);\nALTER TABLE samples ADD COLUMN ci_canary TEXT;\n' \
      > backend/db/migrations/9002_ci_collision.sql
    if ./scripts/apply_migrations.sh; then echo "runner must fail on a rolled-back file"; exit 1; fi
    rm backend/db/migrations/9002_ci_collision.sql
    test "$(value "SELECT count(*) FROM schema_migrations WHERE filename='9002_ci_collision.sql'")" = 0
    test "$(value "SELECT count(*) FROM information_schema.columns WHERE table_name='samples' AND column_name='ci_canary'")" = 0

    echo "== A downgraded function body is reported by the schema check and healed by the next run"
    sql 'CREATE OR REPLACE FUNCTION mark_dataset_stats_stale() RETURNS TRIGGER AS $f$ BEGIN RETURN NEW; END; $f$ LANGUAGE plpgsql'
    if "$python" -m backend.schema_check "$DATABASE_URL"; then echo "stale body must be reported"; exit 1; fi
    ./scripts/apply_migrations.sh | tee "$L/db-heal.log"
    grep -q "Re-applying function definitions" "$L/db-heal.log"
    "$python" -m backend.schema_check "$DATABASE_URL"

    echo "== A database not created from schema.sql is refused"
    recorded=$(value "SELECT string_agg(quote_literal(filename), ',') FROM schema_migrations")
    sql "DELETE FROM schema_migrations"
    if "$python" -m backend.schema_check "$DATABASE_URL"; then echo "a database without the baseline must be reported"; exit 1; fi
    if ./scripts/apply_migrations.sh 2> "$L/db-refused2.log"; then echo "runner must refuse it"; exit 1; fi
    grep -q "not created from backend/db/schema.sql" "$L/db-refused2.log"
    sql "INSERT INTO schema_migrations (filename) SELECT unnest(ARRAY[$recorded])"
    "$python" -m backend.schema_check "$DATABASE_URL"

    echo "== Backup round-trip restores every row"
    ./scripts/test_backup_restore.sh
  ) > "$L/db-recovery.log" 2>&1
  result db-recovery $?
  # Each ci.yml job gets a fresh checkout. Remove what a failed run leaves behind: the e2e backend would
  # wait for a leftover migration to be applied and never run a job.
  rm -f backend/db/migrations/9001_ci_canary.sql backend/db/migrations/9002_ci_collision.sql
fi

if want frontend; then
  npm_ci
  (cd frontend && npm audit --omit=dev --audit-level=high) > "$L/npm-audit.log" 2>&1
  result "frontend npm audit" $?
  (cd frontend && npx tsc --noEmit) > "$L/tsc.log" 2>&1
  result "frontend tsc" $?
  (cd frontend && node --test tests/*.test.cjs) > "$L/frontend-unit.log" 2>&1
  result "frontend unit tests" $?
  (cd launcher && node --test test/ && mkdir -p "$L/launcher-pack" && npm pack --pack-destination "$L/launcher-pack" \
    && { out=$(npm publish --dry-run "$L"/launcher-pack/freedomridge-*.tgz 2>&1) \
         || { echo "$out"; echo "$out" | grep -q 'cannot publish over the previously published versions'; }; }) \
    > "$L/launcher.log" 2>&1
  result "launcher tests and package" $?
fi
if want frontend || want e2e; then
  npm_ci
  # The e2e job serves this build, so it proxies the API to this run's backend.
  (cd frontend && FRIDGE_BACKEND_URL=http://127.0.0.1:$APIPORT npm run build) > "$L/build.log" 2>&1
  result "frontend build" $?
fi

if want quality; then
  "$CPU/mypy" backend/ python/ examples/ > "$L/mypy.log" 2>&1
  result "quality mypy" $? "$(tail -1 "$L/mypy.log")"
  uv export --frozen --extra cpu --all-groups --no-emit-workspace --no-hashes --format requirements-txt |
    sed 's/+cpu / /' > "$L/requirements-audit.txt"
  # shellcheck disable=SC2046 # one word per argument
  "$CPU/pip-audit" --strict --no-deps --disable-pip -r "$L/requirements-audit.txt" \
    $(grep -o -- '--ignore-vuln [A-Za-z0-9-]*' .github/workflows/ci.yml) > "$L/pip-audit.log" 2>&1
  result "quality pip-audit" $? "$(tail -1 "$L/pip-audit.log")"
  npm_ci
  (cd frontend && npm run lint) > "$L/eslint.log" 2>&1
  result "quality eslint" $?
fi

if want e2e; then
  start_db > "$L/e2e-db.log" 2>&1
  result "e2e database" $?
  start_backend e2e "$E2EV" DEMO_MODE=true
  result "e2e backend start" $?
  cd "$W/frontend" || exit 1
  npx playwright install chromium > "$L/playwright-install.log" 2>&1
  FRIDGE_BACKEND_URL=http://127.0.0.1:$APIPORT setsid npx next start -p "$FEPORT" > "$L/next.log" 2>&1 &
  FRONTEND=$!
  timeout 60 bash -c "until curl -sf -o /dev/null http://localhost:$FEPORT/login; do sleep 1; done"
  result "e2e frontend start" $?
  # Playwright finds the server on E2E_BASE_URL and reuses it instead of starting `next dev`.
  # shellcheck disable=SC2086 # E2E_SPECS holds several arguments
  E2E_BASE_URL=http://localhost:$FEPORT E2E_API_URL=http://localhost:$APIPORT/api \
    npx playwright test ${E2E_SPECS:-} > "$L/e2e.log" 2>&1
  status=$?
  # A failed test's trace (its network requests too) and page snapshot, kept with the logs, so they outlive
  # the clone; ci.yml uploads the same folder.
  if [ "$status" != 0 ] && [ -d test-results ]; then cp -r test-results "$L/e2e-test-results"; fi
  counts=$(grep -E '^ *[0-9]+ (passed|failed|skipped|flaky)' "$L/e2e.log" | tr -s ' \n' ' ')
  result "e2e (next start)" $status "${counts:-$(tail -1 "$L/e2e.log")}"
  cd "$W" || exit 1
fi

echo "Logs: $L"
echo "When you are done with them, delete the clone: rm -rf $W"
if [ "$failed" = 0 ]; then
  echo "Every step passed."
else
  echo "$failed step(s) failed; see $L/summary."
  exit 1
fi
