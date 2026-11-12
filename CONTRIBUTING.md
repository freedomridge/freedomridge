# Contributing to Freedom Ridge

Follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities using
[SECURITY.md](SECURITY.md), rather than a public issue.

## Development setup

Use Python 3.14, Node.js 26 or later, Docker, and [uv](https://docs.astral.sh/uv/). Follow the
[Quick Start](docs/QUICK_START.md) for the database, backend, frontend and first login.
`uv sync --extra cpu` (or `--extra cuda`) installs the server, the SDK, the example importers and
the pinned development tools at the versions locked in `uv.lock`. Install the frontend
dependencies with `cd frontend && npm ci`. [Updating dependencies](docs/DEPENDENCIES.md) explains how versions
change. Only the map panel needs a Mapbox token; see [frontend/README.md](frontend/README.md).

## Checks

The local checks below follow `.github/workflows/ci.yml` job by job; run them before opening a
pull request. If the two ever disagree, `ci.yml` is right and this section needs fixing. Run every
section your change touches; for changes that cross areas, run all of them.
`scripts/ci_local.sh <commit>` runs every job in a fresh clone of that commit, with its own database
and ports, and prints a summary. The comment at the top of the script lists its options. The
sections below are the same steps by hand.

Two safety rules:

- Never point tests at a database or backend that holds real data. The steps below create a
  disposable PostgreSQL container on a private port, and the test harness refuses the default
  `freedomridge-db` container and host port 5432.
- Never run the Playwright suite with its defaults. `frontend/playwright.config.ts` targets
  http://localhost:3000 and http://localhost:8000/api, and its global setup **deletes every
  dataset named `e2e-smoke`** on that API. Section 7 points it at a disposable stack.

Run the commands from the repository root with the environment active, in an ordinary terminal
(not under `nohup`: one launcher test checks signal handling and fails when SIGHUP is ignored).

```bash
uv sync --extra cpu
source .venv/bin/activate
```

### 1. Lint, types and dependency audit (CI `lint` and `quality`)

```bash
ruff check backend/ python/ scripts/ examples/
ruff format --check backend/ python/ scripts/ examples/
flake8 backend/ python/ scripts/ examples/
mypy backend/ python/ examples/
uv build --out-dir /tmp/fridge-sdk python/ && twine check --strict /tmp/fridge-sdk/*  # the SDK package, as PyPI reads it
```

Fix lint and formatting with `ruff check --fix` and `ruff format` on the same paths.

The Python formatting rule: a call or definition stays on one line if it fits in 120 characters.
When it has to wrap, it is split with one argument per line and a trailing comma, never several
arguments sharing a wrapped line. Ruff enforces this through rule `COM812` (which adds the trailing
comma) and the formatter (which keeps the call exploded), so running `ruff check --fix` and then
`ruff format` produces the expected layout.

The Python dependency audit uses the ignore list from `ci.yml`; each ignored advisory is explained
in [Updating dependencies](docs/DEPENDENCIES.md#known-audit-exceptions):

```bash
req=$(mktemp)
uv export --frozen --extra cpu --all-groups --no-emit-workspace --no-hashes --format requirements-txt \
  | sed 's/+cpu / /' > "$req"
pip-audit --strict --no-deps --disable-pip -r "$req" $(grep -o -- '--ignore-vuln [A-Za-z0-9-]*' .github/workflows/ci.yml)
```

### 2. Example tests (CI `example-tests`)

These need no database and no network.

```bash
env -u PYTHONPATH python -m pytest examples/test_review_regressions.py -k script_help -q
PYTHONPATH=python:. HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=4 python -m pytest examples -q
```

### 3. A disposable database

Sections 4, 5 and 7 each start from a fresh database, as CI's separate jobs do. Define this once
per terminal. Keep a container name that starts with `fr-ci-` (the backup round-trip test accepts
only throwaway names) and pick any free host port other than 5432.

```bash
DB=fr-ci-db
PORT=55480
LOG=$(mktemp -d)
export FRIDGE_TEST_POSTGRES_DSN=postgres://freedomridge:test-only@127.0.0.1:$PORT/freedomridge
export FRIDGE_TEST_POSTGRES_CONTAINER=$DB FRIDGE_DB_CONTAINER=$DB FRIDGE_DB_USER=freedomridge
docker build -q -f Dockerfile.db -t freedomridge-ci-db .

fresh_db() {
  docker rm -fv "$DB" >/dev/null 2>&1
  docker run -d --name "$DB" -e POSTGRES_USER=freedomridge -e POSTGRES_PASSWORD=test-only \
    -e POSTGRES_DB=freedomridge -p "127.0.0.1:$PORT:5432" freedomridge-ci-db >/dev/null
  timeout 90 bash -c "until docker exec $DB psql -h 127.0.0.1 -U freedomridge -d freedomridge -c 'SELECT 1' >/dev/null 2>&1; do sleep 1; done"
  docker exec -i "$DB" psql -U freedomridge -d freedomridge -v ON_ERROR_STOP=1 -q < backend/db/schema.sql
}
```

When you are done, remove only the container you created: `docker rm -fv "$DB"`.

### 4. Backend tests (CI `backend-tests`)

```bash
fresh_db
python -m pytest -q
```

The harness checks that the DSN and container name the same disposable server before it writes
anything. For a focused run, pass the relevant `backend/tests/test_*.py` files. Some database
tests fail rather than skip when the test settings are missing.

Then run the SDK integration suites against a backend on that database. They create and delete
their own datasets.

```bash
fresh_db
docker exec "$DB" psql -U freedomridge -d freedomridge -c "UPDATE users SET must_change_password = false WHERE username = 'admin'"
DATABASE_URL=$FRIDGE_TEST_POSTGRES_DSN SESSION_COOKIE_SECURE=false STORAGE_ENCRYPTION_KEY=ci-only-not-a-secret \
  python -m backend.launch -- python -m uvicorn backend.main:app --host 127.0.0.1 --port 8091 > "$LOG/backend.log" 2>&1 &
BACKEND=$!
timeout 60 bash -c 'until curl -sf http://127.0.0.1:8091/api/datasets >/dev/null; do sleep 1; done'
for suite in test_sdk test_integrity test_map_contract test_objects test_synth_integration; do
  FRIDGE_TEST_URL=http://127.0.0.1:8091/api python -m "fridge.$suite" || echo "FAILED: $suite"
done
kill "$BACKEND"
```

### 5. Migrations and backup restore (CI `db-recovery`)

This runs the migration runner and the schema check on a database built from `schema.sql`, then
the backup round trip. The backup round-trip test refuses the test DSN variables, so the subshell
unsets them.

```bash
fresh_db
(
  set -e
  unset FRIDGE_TEST_POSTGRES_DSN FRIDGE_TEST_POSTGRES_CONTAINER
  ./scripts/apply_migrations.sh
  python -m backend.schema_check "postgresql://freedomridge:test-only@127.0.0.1:$PORT/freedomridge"
  ./scripts/test_backup_restore.sh
)
```

If you add a migration or change `scripts/apply_migrations.sh` or `backend/schema_check.py`, also
run the remaining `db-recovery` steps from `ci.yml` (a new file applied once, failed-file rollback,
function-body repair, the refusals) the same way.

### 6. Frontend (CI `frontend`, plus the eslint step of `quality`)

```bash
cd frontend
npm ci
npm audit --omit=dev --audit-level=high
npx tsc --noEmit
npm run lint
node --test tests/*.test.cjs
npm run build
cd ..
```

The `npx freedomridge` launcher's tests, in the same CI job. `launcher/release/` isn't in git: `npm pack` copies the
release files into it (`scripts/bundle.js`), and so do the tests. To run `launcher/bin/freedomridge.js` straight
from a checkout, run `node launcher/scripts/bundle.js` first.

```bash
cd launcher
node --test test/
cd ..
```

### 7. Browser tests (CI `e2e`)

Run these for any UI change. The backend runs in demo mode on a fresh database, and Playwright
starts its own frontend dev server on port 3091. Make sure nothing else listens on 3091 or 8091:
Playwright reuses a server it finds on its port. Stop any `npm run dev` running from this
checkout first, because two dev servers cannot share the `.next` directory. The API URL must use
`localhost`, not `127.0.0.1`, because the test setup stores the session cookie for that host name.
The map test skips itself unless `NEXT_PUBLIC_MAPBOX_TOKEN` is set. The tests share one backend
and database, and some interfere with each other when they run in parallel, so run them with one
worker. That takes about ten minutes.

```bash
fresh_db
DEMO_MODE=true DATABASE_URL=$FRIDGE_TEST_POSTGRES_DSN SESSION_COOKIE_SECURE=false \
  STORAGE_ENCRYPTION_KEY=ci-only-not-a-secret ALLOWED_ORIGINS=http://localhost:3091 \
  python -m backend.launch -- python -m uvicorn backend.main:app --host 127.0.0.1 --port 8091 > "$LOG/e2e-backend.log" 2>&1 &
BACKEND=$!
timeout 60 bash -c 'until curl -sf http://127.0.0.1:8091/api/datasets >/dev/null; do sleep 1; done'
cd frontend
npx playwright install chromium    # add --with-deps on a new machine; it installs system packages
E2E_BASE_URL=http://localhost:3091 E2E_API_URL=http://localhost:8091/api \
  FRIDGE_BACKEND_URL=http://127.0.0.1:8091 npx playwright test --workers=1
cd ..
kill "$BACKEND"
```

Use a fresh database for every run: demo mode limits guest accounts per IP address per hour, and
the suite creates guest accounts.

## Database migrations

To change the schema:

1. Add `backend/db/migrations/NNNN_short_name.sql` with the next free four-digit number (`0001_`,
   `0002_`, ...). Files hold the changes since 1.0.0, which the ledger records as `0000_baseline.sql`.
   Never edit, renumber or delete a file that has been released: a database from any release of the
   major version upgrades straight to the latest through all of them.
2. Make the same change in `backend/db/schema.sql` and add the filename to its `schema_migrations`
   list. New databases are created from it. `backend/tests/test_upgrade_paths.py` upgrades a
   database from every release of the major version with the migrations and fails unless it ends
   up identical to a fresh install from `schema.sql`, column order included: add a new column at the
   end of its `CREATE TABLE`. It reads old schemas from the release tags (`git fetch --tags`). If you
   change a function, regenerate `backend/db/functions.sql`
   (`python -m backend.schema_check --emit-functions > backend/db/functions.sql`).
3. Keep the file safe on a large database. It runs in one transaction with its ledger row, so set a
   short `lock_timeout` (for example `SET LOCAL lock_timeout = '2s';`) and fail rather than queue
   behind readers. Don't rewrite or backfill a large table inside it: do that as a job the runner
   queues, and keep that job kind working for the rest of the major version, since a database from
   that release can upgrade straight to any later one. Migrations only go forward; going back is
   restoring the backup taken before the upgrade.
   The one exception is `0021_embeddings_partitioned.sql`, which copies every vector into the
   partitioned table itself: copying while the app ran would have needed every vector write and query
   in two versions until it finished. It refuses to run while anything else is connected, checks the
   disk and the lock table first, and its progress notices start with its number (`0021: ...`), which
   `apply_migrations.sh` shows as they come. A migration that has to do the same follows that pattern,
   and its release's upgrade notes say to stop the app.
4. Think about followers ([docs/FOLLOWERS.md](docs/FOLLOWERS.md)) when a migration changes rows, not only the
   schema. Statements on the tables followers read (`samples`, `embeddings`, `sample_hashes`,
   `embedding_coordinates`, `dataset_fields`, `views`, `embedding_viz_meta`, `embedding_layouts` and `datasets`: the
   ones with the `follower_truncate_guard` trigger) run those tables' triggers like any other write. On a primary, each changed row of a followed dataset
   is logged, and every follower fetches it again. On a follower, a mirror's read-only guard refuses the
   statement and the migration fails. `embedding_layouts` has neither trigger: a migration that changes its rows logs
   a `'maps'` dataset change for each followed dataset it touches, as `bump_map_layout` does, so followers read the
   ids again. When the rows mean the same afterwards (a column filled in, a table
   rebuilt), keep the triggers from firing, for example with `ALTER TABLE ... DISABLE TRIGGER USER` around the
   statement or by creating the triggers after the rows are copied, and say in the file that it is on purpose.
5. Run sections 4 and 5.

To apply pending migrations to an existing database, run `scripts/apply_migrations.sh` from the
repository root. It records each file in the `schema_migrations` table in the same transaction as
the file itself, so a failed file is rolled back and retried on the next run. The target depends
on the installation:

```bash
# Quick Start database (or the container name you chose for a second copy)
FRIDGE_DB_CONTAINER=freedomridge-db ./scripts/apply_migrations.sh

# docker-compose.prod.yml deployment, from its checkout: finds the compose `db` service
./scripts/apply_migrations.sh

# Any database by URL (needs a local psql client)
./scripts/apply_migrations.sh --database-url "$DATABASE_URL"
```

`GET /api/health` lists pending migrations and out-of-date database functions. It also lists
migrations the database records but the code doesn't have: the database is newer than the code
(a newer version ran against it, or a newer backup was restored), and the runner refuses it.

## Conventions

- Filters go through `parse_filter_node()` in `backend/routers/datasets.py`. Extend the parser
  rather than writing filter SQL by hand.
- Column names cannot be SQL parameters. Validate dynamic names against `^[a-z][a-z0-9_]{0,62}$`,
  the reserved-name list and the field registry before using them.
- Routes under `/datasets/{id}` declare `dependencies=[Depends(require_dataset_access)]`; routes that
  take a dataset id in the body call `check_dataset_access()` (both in `backend/access.py`).
- Never fix performance by capping or silently truncating what a user can see. Signal limits and
  keep the full data reachable.
- Before changing the map panel or the `map_points` endpoint, read
  [the map requirements](docs/MAP_REQUIREMENTS.md).
- Copied links must keep opening. `frontend/tests/link-fixtures.json` holds frozen `q=` and `f=` links, and the
  frontend, backend and e2e tests open them. A change to the stage or filter shape, an operator, or the query
  language that breaks one either keeps reading the old shape (the fixtures stay), or gives `f=` a new version
  (`LINK_VERSION` in `frontend/lib/links.ts`) on purpose: it removes the old version's fixtures and says in the
  CHANGELOG's upgrade notes that links copied before the release no longer open.
- Commit messages are one short sentence, optionally prefixed with the area (`Sidebar: ...`).

## Pull requests

Keep changes focused and explain the problem, resulting behavior, and validation in the pull
request, including which of the checks above you ran. Include screenshots for visible UI changes
and a regression test when changing behavior. Update public documentation and migration notes
when applicable. Preserve existing data, and avoid unrelated formatting changes.

Do not commit credentials, private datasets, database dumps, model weights, or internal review
material. Contributions must be yours to submit under the project's [MIT license](LICENSE);
identify third-party code and its license.
