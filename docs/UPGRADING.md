# Upgrading

Freedom Ridge has one version number for the server, the web app and the Python SDK, in the
`VERSION` file. Each release is tagged `vX.Y.Z`. [CHANGELOG.md](../CHANGELOG.md) says what changed,
with **upgrade notes** when a release needs more than the steps below: a migration that takes a
while on a large database, downtime, a configuration change or an SDK change.
`GET /api/health` and Settings → System status show the version a server runs.

## What is supported

- **Upgrade from any earlier release straight to the latest one** within the same major version
  (or as its release notes say). You don't need to install the versions in between.
- **Migrations only go forward.** Going back is restoring the backup taken before the upgrade
  and running the previous version (below).
- Versions follow [Semantic Versioning](https://semver.org): a release that breaks the SDK, the
  HTTP API or this upgrade path is a new major version.

## A Docker server

From the checkout the server runs, with `docker-compose.prod.yml`:

1. Read the CHANGELOG entries between your version and the new one.
2. Back up the database as in [Backup and migration](BACKUP_AND_MIGRATION.md), and keep the dump
   until you're happy with the new version.
3. Check out the release and build it:
   ```bash
   git fetch --tags
   git checkout vX.Y.Z
   docker compose -f docker-compose.prod.yml build --pull
   ```
4. Apply the migrations, then recreate the containers:
   ```bash
   FRIDGE_DB_CONTAINER=$(docker compose -f docker-compose.prod.yml ps -q db) \
     bash scripts/apply_migrations.sh
   docker compose -f docker-compose.prod.yml up -d
   ```
5. Check `https://YOUR_DOMAIN/api/health`. It answers `"status": "ok"` with the new `version`.
   While it answers 503 (and the backend container reports unhealthy), its lists say why:
   `pending_migrations` or `stale_functions` (run step 4 again), or `unknown_migrations` (the
   database is newer than the code that's running).

Machines that use the Python SDK should install it from the same release.

Each build leaves layers in Docker's build cache, which Docker trims once it passes its
garbage-collection limits (`docker buildx inspect` shows them). To free the disk space sooner, run
`docker builder prune --filter until=24h` soon after an upgrade. It removes what only earlier
builds used and keeps the new build's cache, so the next upgrade still builds quickly.

## Going back

Stop the services and restore the backup from step 2 into an empty database
([restore procedure](BACKUP_AND_MIGRATION.md)). Then check out the previous tag, build and start.
Don't run an older version against the upgraded database: its migration runner and
`/api/health` refuse a database that records migrations the code doesn't have.

## A development checkout

```bash
git pull --ff-only
uv sync --extra cuda          # or --extra cpu
cd frontend && npm ci && cd ..
# When the pull brought new backend/db/migrations (the Quick Start's database container):
FRIDGE_DB_CONTAINER=freedomridge-db ./scripts/apply_migrations.sh
```

Then restart the backend and the frontend. How the dependency versions themselves are chosen and
updated: [Updating dependencies](DEPENDENCIES.md).
