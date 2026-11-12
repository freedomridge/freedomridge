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
   [Upgrading across 2.9.0](#upgrading-across-290) first: its migration takes longer. Crossing 2.11.0, read
   [Upgrading to 2.11.0](#upgrading-to-2110).
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
  From 2.11.0 a datetime filter can count differently on each side until both run it
  ([Upgrading to 2.11.0](#upgrading-to-2110)).
- **Lassos across 2.11.** A lasso drawn on 2.11 records its map's layout (`layout_id`) and, until 2.12, its model
  (`model_id`) beside it, so a follower on 2.10.x or earlier, which refuses a whole-image lasso without a model on a
  map its primary re-fitted, still accepts it in a relayed search. 2.12 stops writing `model_id`: upgrade followers
  to 2.11 or later before their primary takes 2.12. A 2.11 follower of a 2.10.x primary gets no layout ids from it,
  so a lasso on its copy of one of the primary's maps doesn't warn when the primary lays that map out again, until
  the primary runs 2.11. Lassos on the follower's own maps warn as on any 2.11 server.
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

## Upgrading to 2.11.0

Two of its migrations need the app stopped, and its datetime indexes are rebuilt in the background afterwards.

### Objects and their images: two migrations with the app stopped

2.11.0's migrations `0029_object_parent_column.sql` and `0030_object_parent_key.sql` give every object sample its
image in a column of its own, and a key that deletes an image's objects with it. Like `0021`, each refuses to run
while anything else is connected to the database and names what is: step 4 stops the app, and both run in that one
stop.

- **How long:** `0029` links about 13,000 objects a second (3.2 million in 4 minutes, measured) and indexes them
  again (7 s for 3.2 million); other samples cost about 0.6 s a million. `0030` checks every object's image
  in one pass: 2.3 s for 3.2 million objects among 4 million samples.
- **Disk:** `0029` rewrites every object's row, so it checks the free disk first and says how much it needs (about the
  objects' share of the table and its indexes, plus WAL). The key `0030` adds takes no space.
- **What they delete:** `0029` deletes the object samples whose image no longer exists (their `metadata.parent_id` is
  an id no sample has, in any dataset), and `0030` any object whose image was deleted between the two. Each prints,
  per dataset, how many. A follower's mirrors keep theirs until their primary deletes them.
- **If it stops:** a failure or an interruption rolls back that migration, and it runs again from the start.
- **After:** run `VACUUM (ANALYZE) samples;` in psql once the app is up: every object `0029` linked leaves an old row
  version behind until then.
- **From then on, deleting an image deletes its objects**, in psql too. A script that deletes images and keeps their
  objects must clear the objects' `metadata.parent_id` first ([Object view](OBJECT_PATCH_VIEW.md)).

### Dates and times: indexes rebuilt after the upgrade

Dates and times in metadata compare as moments from 2.11.0 (the changelog says what changes for a filter).

- **Datetime indexes are rebuilt after the upgrade.** The migration `0032_datetime_index_rebuilds.sql` marks every
  datetime field that had an index as not indexed and queues its rebuild; it builds and drops nothing itself.
  The app rebuilds each on the field's moment in the background, under the same index name, smallest dataset
  first, one index build at a time on the server, a field rebuild before any vector rebuild (two
  `CREATE INDEX CONCURRENTLY` builds wait for each other's snapshots even on different tables, so the second would
  only wait, and could run out its budget). A build someone starts meanwhile waits for its turn, unstarted, shown
  as waiting in Background jobs; creating an array field is refused (409) until no index build runs.
- **Meanwhile** results stay correct. Only filters, sorts and bounds on those fields are slower, since each
  sample's value is read without an index.
- **How long:** each rebuild reads the whole `samples` table, as the field's first indexing did, and takes
  somewhat longer. To estimate before upgrading, multiply the number of indexed datetime fields (`SELECT
  dataset_id, field_name FROM dataset_fields WHERE is_promoted AND field_type = 'datetime'`) by the time of a
  recent field index build in Background jobs.
- **Progress:** Dataset Settings → Indexes shows **Rebuilding** until a field is done (click it to build that
  field now), the dataset's index review lists the fields being rebuilt, and Background jobs shows each build. A
  field whose rebuild waits shows **Waiting** with the reason: another index build running, or an unfinished
  rename, clone or clear on its dataset (a stopped one too), whose fields are rebuilt once that job is resumed and
  done, or discarded.
- **If a rebuild fails** it is tried again a day later. Cancelling one stops it for good: index the field again
  from the Indexes tab.
- **Upgrading from before 2.9.0 straight to 2.11.0:** the datetime rebuilds go first; the vector rebuilds start
  when they are done.
- **Followers:** upgrade them soon after their primary. A shared view, and a relayed patch search, is evaluated
  by the server that runs it, so until both sides run 2.11.0 the same filter on a datetime field whose values
  carry offsets or mixed spellings can count differently on each, and a filter on `last_modified_at` (filterable
  from 2.11.0) matches nothing on the side still on 2.10.x. Nothing breaks, and the answers agree once both are
  upgraded. A follower's indexes are its own: its migration queues the rebuilds of its own indexed datetime
  fields, which it runs as a primary does.

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
