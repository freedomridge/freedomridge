# Upgrading

Freedom Ridge has one version number for the server, the web app and the Python SDK, in the
`VERSION` file. Each release is tagged `vX.Y.Z`. [CHANGELOG.md](../CHANGELOG.md) says what changed,
with **upgrade notes** when a release needs more than the steps below: a migration that takes a
while on a large database, downtime, a configuration change or an SDK change.
`GET /api/health` and Settings → System status show the version a server runs.

## What is supported

- **Upgrade from any earlier release straight to the latest one** within the same major version
  (or as its release notes say). You don't need to install the versions in between: the migration
  runner applies every migration the database lacks, in order. CI checks this for each release: it
  upgrades a database created by that release and compares it with a fresh install.
- A new major version may start from a new baseline schema. Its release notes then name the release
  to upgrade to first.
- **Migrations only go forward.** Going back is restoring the backup taken before the upgrade
  and running the previous version (below).
- Versions follow [Semantic Versioning](https://semver.org): a release that breaks the SDK, the
  HTTP API or this upgrade path is a new major version.

## A Docker server

From the checkout the server runs, with `docker-compose.prod.yml`:

1. Read the CHANGELOG entries between your version and the new one.
2. Back up the database as in [Backup and migration](BACKUP_AND_MIGRATION.md), and keep the dump
   until you're happy with the new version.
3. Check out the release and pull its images from ghcr.io (each release publishes them):
   ```bash
   git fetch --tags
   git checkout vX.Y.Z
   docker compose -f docker-compose.prod.yml pull
   ```
   To build them from the checkout instead, as releases before 2.2.0 need, run
   `docker compose -f docker-compose.prod.yml build --pull`.
4. Stop the app, apply the migrations with it stopped, then recreate the containers. Crossing 2.9.0, read
   [Upgrading across 2.9.0](#upgrading-across-290) first: its migration takes longer.
   ```bash
   docker compose -f docker-compose.prod.yml stop backend job-runner worker backup frontend
   FRIDGE_DB_CONTAINER=$(docker compose -f docker-compose.prod.yml ps -q db) \
     bash scripts/apply_migrations.sh
   docker compose -f docker-compose.prod.yml up -d
   ```
   Running, the old version would go on serving requests and running jobs against the changed schema until it is
   replaced: its API checks the schema only in `/api/health`, and its job runner only when it starts. That is
   every service but the database and nginx: a migration may refuse to run while anything else is connected, and
   a scheduled dump that ran during the migrations would make them wait behind it, or fail. `up -d` starts all
   five again. The server is down from the `stop` until the new version answers in step 5.
5. Check `https://YOUR_DOMAIN/api/health`. It answers `"status": "ok"` with the new `version`.
   While it answers 503 (and the backend container reports unhealthy), its lists say why:
   `pending_migrations` or `stale_functions` (run step 4 again), or `unknown_migrations` (the
   database is newer than the code that's running).

Machines that use the Python SDK should install it from the same release:
`pip install "freedomridge==X.Y.Z"`.

Each build leaves layers in Docker's build cache, which Docker trims once it passes its
garbage-collection limits (`docker buildx inspect` shows them). To free the disk space sooner, run
`docker builder prune --filter until=24h` soon after an upgrade. It removes what only earlier
builds used and keeps the new build's cache, so the next upgrade still builds quickly.

## Primaries and followers

A primary and its [followers](FOLLOWERS.md) are separate installations: each has its own database and runs
its own migrations, and they talk only through the follower API. So:

- **Upgrade them one at a time, in either order.** Any two releases from 2.6.0 on follow each other: a follower
  on an older or newer release than its primary keeps its mirrors in sync. A feature that needs both sides (the changelog says so) starts working once both
  have it.
- **While a primary is being upgraded** its followers keep their mirrors, read-only as always, show the
  primary's last error on Settings → Primaries, and catch up from its change log when it answers again.
- **Going back on a primary** is a restored primary to its followers: each stops its copy and takes a new one,
  as a new dataset. Restore into a new or empty database, as the backup from step 2 is restored. A restore over
  the same database isn't detected, and followers would keep rows the primary no longer has
  ([Followers](FOLLOWERS.md#the-followers-protocol)).
- **Going back on a follower** (restoring its backup) leaves its mirrors behind what it had told the primary
  it held. The primary refuses the older position, and the follower copies those datasets again into the same
  mirrors, reading every block again: hours for a large dataset. A dataset it started following after the backup
  is copied as a new one.

## Upgrading across 2.9.0

2.9.0's migration `0021_embeddings_partitioned.sql` copies every vector into a table with one partition per
dataset. Nothing else may write meanwhile, so it refuses to run while anything else is connected to the
database, and names what is. Step 4 stops the backend, the job runner, the `worker` and `backup` services and
the frontend, which is every service of `docker-compose.prod.yml` but the database and nginx; stop anything else
that connects too (a compute worker on another machine, a script, a `psql` session). A backup's dump started
first makes the migration refuse to start, and one started during the copy waits for it and then fails (its
snapshot is older than the new tables) until its next run. The launcher (`npx freedomridge`) stops the app itself
before it migrates. In a development checkout, stop the backend, the job runner and any workers.

- **How long:** about a minute per million vectors (9.4 minutes for 9.7 million, measured), then the app
  starts. `apply_migrations.sh` prints the estimate and each dataset's progress.
- **Disk:** about the size of the vectors and their plain indexes, plus `max_wal_size` and a quarter again. The
  migration checks first and says how much it needs.
- **If it stops:** a failure or an interruption (over ssh, run it in tmux) rolls it back, and it runs again
  from the start. If the database server itself crashes or is killed during the copy, the rollback still
  happens, but PostgreSQL leaves the partial copy's files on disk: up to the size of the vectors copied so far,
  which `pg_database_size` shows and which the next attempt needs room beside. Only dropping that database frees
  them. With room for a second copy, restore the backup from step 2 into a new database, switch to it, then drop
  the old one. Without it, first check that the step-2 backup is complete and stored off this disk, then drop the
  database and restore it.
- **After:** the vector indexes are built again in the background, one per dataset and field, smallest
  dataset first (Background jobs shows them). A dataset's similarity searches are exact until its index is
  built, and slower: on 3.7 million 1,024-dimension vectors, measured, 8 s warm, up to 90 s cold, and 20 to 30 s
  while another index builds. A large dataset's fields come last and one at a time: on 3.7 million vectors, the
  512-dimension field took 40 minutes to 2 hours and the 1,024-dimension one 1.5 to 4 hours after it, depending on
  `maintenance_work_mem`. So its searches can be slow for 2 to 6 hours, and a guest's search there can hit its
  30 s limit meanwhile. A build that fails three times stays failed on Background jobs;
  build it again with `dataset.build_ann_index(field)` in the SDK.

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
# When the pull brought new backend/db/migrations (the Quick Start's database container), with the
# backend, the job runner and the frontend stopped:
FRIDGE_DB_CONTAINER=freedomridge-db ./scripts/apply_migrations.sh
```

Then start the backend, the job runner and the frontend again. How the dependency versions themselves are
chosen and updated: [Updating dependencies](DEPENDENCIES.md).
