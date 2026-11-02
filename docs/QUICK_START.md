# Quick Start Guide

This is the canonical development setup. Use Python 3.14, Node.js 26+ (`nvm use` reads
`.nvmrc`), Docker with Compose v2, and [uv](https://docs.astral.sh/uv/) for the Python
environment.

## 1. Clone

```bash
git clone https://github.com/freedomridge/freedomridge.git
cd freedomridge
```

If you work from a fork, clone your fork instead.

## 2. Start the database

> **Already running Freedom Ridge on this machine?** Stop here and read
> [Running a second copy](#running-a-second-copy) first. With the default
> `COMPOSE_PROJECT_NAME`, the commands below address the existing install's database
> container, and `docker compose up` would recreate it.

```bash
cp .env.example .env
# Keep COMPOSE_PROJECT_NAME stable: it identifies the database volume.
docker compose up -d --build db
# TCP is ready only after initdb and the schema have finished.
until docker compose exec -T db psql -h 127.0.0.1 -U freedomridge -d freedomridge \
  -c 'SELECT 1' >/dev/null 2>&1; do sleep 1; done
```

`Dockerfile.db` includes PostGIS and pgvector. Compose loads `backend/db/schema.sql`
automatically on the first start of an empty volume. Do not apply it a second time.
The defaults remain container `freedomridge-db`, host port `5432`, database/user
`freedomridge`, password `password`. Use these credentials only for local development.
For an existing database, apply new migrations as in [Upgrading](UPGRADING.md#a-development-checkout).

Compose reads the root `.env`; a backend launched from the shell does **not** load
that file. Export backend settings in its terminal. Frontend settings belong in
`frontend/.env.local`.

## 3. Start the backend

From the repository root:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # once: installs uv (Python package manager)
uv sync --extra cpu      # with an NVIDIA GPU (driver 560+): uv sync --extra cuda
source .venv/bin/activate
export FRIDGE_DATA_ROOT="$PWD/data"
mkdir -p "$FRIDGE_DATA_ROOT"
export DATABASE_URL=postgres://freedomridge:password@127.0.0.1:5432/freedomridge
python -m backend.launch -- python -m uvicorn backend.main:app \
  --reload --reload-dir backend --host 127.0.0.1 --port 8000
```

The API is at http://localhost:8000 and API documentation at http://localhost:8000/docs.
Reuse the absolute `FRIDGE_DATA_ROOT` in importer terminals. `--reload-dir backend`
prevents package installs in the repository's venv from repeatedly restarting the
API. `uv sync` installs the exact versions in `uv.lock` (server, SDK, importers, dev
tools) into `.venv`; [Updating dependencies](DEPENDENCIES.md) explains how versions change.

The launcher starts one job runner. Reload restarts only the API; restart the
launcher after changing runner/job-handler code. If using the Compose `jobs`
profile instead, export `FRIDGE_RUNNER_ENABLED=false` for the launcher.

## 4. Start the frontend

In a second terminal:

```bash
cd frontend
npm ci
cp .env.local.example .env.local
npm run dev
```

Open http://localhost:3000. For maps, put a real Mapbox token in
`NEXT_PUBLIC_MAPBOX_TOKEN` in `.env.local` and restart the frontend. Do not use the
literal placeholder `your_token_here`. Grid, annotations and other panels work
without a map token; the map panel requires it.

## 5. Log in and create a token

Log in as `admin` / `admin123`, then complete the required password change (at least
8 characters). In **Settings → API Tokens**, create a token and copy it when it is
shown; its full value is only shown once. In the importer terminal export
`FRIDGE_API_TOKEN` and `FRIDGE_API_URL=http://localhost:8000/api`.

## 6. Try demo data

Activate the Python environment, reuse the backend's `FRIDGE_DATA_ROOT`, and follow
[the demo-data prerequisites](../examples/README.md#prerequisites). The environment
from step 3 already has the importers' dependencies and the CLIP models' PyTorch, which
the backend also uses for UI text search. Allow 1.2 GB for the CLIP model cache and up
to about 7 GB for a CUDA-enabled Python environment.

```bash
python examples/download_demo_data.py --datasets coco
python examples/import_demo_data.py --datasets coco --limit 100
python examples/import_demo_data.py --datasets coco --full
```

COCO occupies about 2.6 GB before derived crops and database storage. Start with 100
images, then resume to the full 5,000 with boxes, masks, poses and captions. See
[all demo datasets](../examples/README.md) for disk estimates, licenses and limits.
For a small example without CLIP:

```bash
python examples/sdk/generate_synthetic_dataset.py --api-url "$FRIDGE_API_URL"
```

## Running a second copy

Use another checkout and a different Compose project, container names and ports.
Set these in its root `.env` **before** the first `docker compose up`:

```dotenv
COMPOSE_PROJECT_NAME=freedomridge-demo2
FRIDGE_DB_CONTAINER=freedomridge-demo2-db
FRIDGE_DB_PORT=127.0.0.1:55486
FRIDGE_WORKER_CONTAINER=freedomridge-demo2-worker
```

`FRIDGE_DB_PORT` accepts `host:port`; binding to `127.0.0.1` keeps the development
password off the network. This gives the second copy its own `postgres_data` volume. Keep the project name
stable after initialization. Follow step 2 with the same Compose commands, then
use these exports in the second backend terminal (with its own data root):

```bash
export FRIDGE_DATA_ROOT="$PWD/data-demo2"
mkdir -p "$FRIDGE_DATA_ROOT"
export DATABASE_URL=postgres://freedomridge:password@127.0.0.1:55486/freedomridge
export ALLOWED_ORIGINS=http://localhost:3086
python -m backend.launch -- python -m uvicorn backend.main:app \
  --reload --reload-dir backend --host 127.0.0.1 --port 8086
```

In its frontend terminal:

```bash
cd frontend
FRIDGE_BACKEND_URL=http://127.0.0.1:8086 npm run dev -- -p 3086
```

In its importer terminal, export `FRIDGE_API_URL=http://127.0.0.1:8086/api` and an API
token created in **this copy's** UI. The usual defaults are database port 5432,
backend port 8000, frontend port 3000, and allowed origins on 3000/5173. Changing one
does not change the others. Stop only processes and Compose projects you started;
choose unused ports instead of terminating unrelated services.

## Browser origins for remote development

`ALLOWED_ORIGINS` is the comma-separated allowlist for CORS and for writes made with
the session cookie. Without it the backend accepts `http://localhost:3000` and
`http://localhost:5173`. To use the frontend from another address, list the exact
scheme, host and port the browser shows, for every address form you use:

```bash
export ALLOWED_ORIGINS=http://localhost:3000,http://dev.example.test:3000
```

Export it in the backend terminal before the step 3 command. `scripts/run_backend.sh`,
the alternative launcher, loads it from the repository-root `.env` instead (default
`http://localhost:3000`). That launcher can also add Tailscale peers. This is **off by
default**: set `FRIDGE_TAILSCALE_ORIGINS=1` to run `tailscale status` on every launch
and append each peer's IP, short name and full MagicDNS name, using HTTP and
`FRIDGE_FRONTEND_PORT` (default `3000`). Configured origins stay first. If the command
is missing or finds no peers, only the configured origins remain.
`FRIDGE_TAILSCALE_SKIP` excludes peer short names by shell glob, or several
space-separated globs. Quote the value in `.env`; an empty value skips nothing:

```bash
FRIDGE_TAILSCALE_ORIGINS=1
FRIDGE_TAILSCALE_SKIP='build-* temporary-*'
```

These variables apply only to the development launcher. A production deployment
sets the exact deployed origin instead. For the Next.js development server, also set
`ALLOWED_DEV_ORIGINS` in `frontend/.env.local` to the hostnames or addresses you use,
without a scheme or port.

## Troubleshooting and next steps

- Database startup: `docker compose logs db`; use the TCP query in step 2 to check readiness.
  The development PostgreSQL configuration targets a large-memory machine
  (`shared_buffers=8GB`); adjust `backend/db/postgresql.conf` for smaller hosts.
- API startup: activate the venv, confirm `DATABASE_URL` and the data directory,
  and inspect the launcher's log. Existing installations may need migrations.
- Frontend: use the pinned lockfile with `npm ci`. Review audit findings before
  production deployment; dependency installs do not resolve every advisory.
- Production: [Deploy to AWS](DEPLOY_TO_AWS.md) for the hosted demo on one EC2 instance,
  or [Docker deployment](DOCKER_DEPLOYMENT.md) for another host.
