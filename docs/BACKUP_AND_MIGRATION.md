# Freedom Ridge Backup and Migration

## Full Database Backup

PostgreSQL holds datasets, samples, metadata, embeddings, hashes, user accounts and settings.
A recoverable installation also needs its encryption key, deployment configuration and external
media; keep these off-box as described below.

### Create a backup

> Keep `--clean --if-exists` on full dumps and always restore with `ON_ERROR_STOP=1`.
> These flags do **not** make a newer, populated schema a safe restore target: the dump cannot
> remove foreign keys belonging to tables created after it was taken. Restore into an empty DB.
> `scripts/test_backup_restore.sh` tests the supported recovery path.

```bash
# From the production checkout (select the correct Compose project/environment)
FRIDGE_DB_CONTAINER=$(docker compose -f docker-compose.prod.yml ps -q db)
docker exec "$FRIDGE_DB_CONTAINER" pg_dump -U freedomridge --clean --if-exists freedomridge > freedomridge_backup.sql

# Compressed (recommended for large datasets)
docker exec "$FRIDGE_DB_CONTAINER" pg_dump -U freedomridge --clean --if-exists freedomridge | gzip > freedomridge_backup_$(date +%Y%m%d).sql.gz
```

### Restore from backup

Restore into an **empty database**, then run the migration runner from the current checkout.
Do not load `schema.sql` first. A backup is older than the code: newer tables such as
`backup_schedule` reference the dump's tables, so its `--clean` step fails with
`cannot drop constraint users_pkey …`. Stop the backend, job runner, workers and backup scheduler
for the target before restoring. The following commands are for a replacement install; a drill
must use its own container name, port and data folder (see isolation below).

For the shipped AWS stack, prefer the [AWS restore procedure](DEPLOY_TO_AWS.md#12-backups-and-recovery).
The manual procedure below intentionally creates a standalone `freedomridge-restore`
container; it is not the automatically named production Compose DB.

1. **Pick the dump.** Prefer the newest verified `freedomridge_*.sql.gz` with its
   `<file>.manifest.json`. The manifest records `schema_version`, `migrations_applied` and every
   table's `row_counts`. Sidecar/SDK dumps have no manifest; retain independent source counts for
   verification. Keep the original dump untouched throughout recovery. Use a checkout at least as
   new as the dump: compare its migration files with the manifest's `schema_version`. The migration
   runner and `/api/health` refuse a database that records migrations the checkout doesn't have.
2. **Start an empty database on the disk that will hold it.** Build `Dockerfile.db`, which includes
   pgvector and PostGIS. Do not mount `schema.sql`; the dump creates its own extensions. Run from
   the repository root, after creating a new empty pgdata folder on the mounted data disk.
   This procedure stores the restored data in the **bind folder `/path/to/new/pgdata`**, not in
   Compose's `postgres_data` named volume. Step 7 must retain that bind mount for dev **and prod**:

   ```bash
   docker build -f Dockerfile.db -t freedomridge-restore:pg18 -t freedomridge-db:pg18 .
   docker run -d --name freedomridge-restore --shm-size=32g -p 127.0.0.1:5432:5432 \
     -e POSTGRES_USER=freedomridge -e POSTGRES_PASSWORD='<password>' -e POSTGRES_DB=freedomridge \
     --mount type=bind,source="$PWD/backend/db/postgresql.conf",target=/etc/postgresql/postgresql.conf,readonly \
     --mount type=bind,source=/path/to/new/pgdata,target=/var/lib/postgresql/data \
     freedomridge-restore:pg18 postgres -c config_file=/etc/postgresql/postgresql.conf -c max_connections=250 \
     -c maintenance_work_mem=24GB -c max_parallel_maintenance_workers=8
   until docker exec freedomridge-restore pg_isready -U freedomridge; do sleep 1; done
   ```
   The command above is an example for a large-memory host, not a default for every machine.
   Budget `maintenance_work_mem` at roughly RAM/5, leave headroom for the OS and other services,
   and size `--shm-size` for parallel HNSW builds. The normal 1 GB shared-memory limit is too
   small for these example settings. If the data disk's mount point is owned by root, create the
   folder with `sudo`. Verify the disk mount first; do not let a missing disk become a fresh DB
   on the root filesystem.

   If reusing an already initialized target, explicitly empty **that target** first. This
   destroys its contents; check the container name and stop its clients before running:
   ```bash
   RESTORE_CONTAINER='<replacement-container-you-verified>'
   RESTORE_DB='<database-you-intend-to-replace>'
   docker exec "$RESTORE_CONTAINER" dropdb -U freedomridge "$RESTORE_DB" && \
     docker exec "$RESTORE_CONTAINER" createdb -U freedomridge "$RESTORE_DB"
   ```
3. **Restore**:
   ```bash
   set -o pipefail
   gunzip -c freedomridge_<time>.sql.gz | docker exec -i freedomridge-restore \
     psql -X -U freedomridge -v ON_ERROR_STOP=1 -d freedomridge > restore.log 2> restore-errors.log
   echo "${PIPESTATUS[@]}"  # immediately after the pipeline: both must be 0
   ```
   For plain SQL, use input redirection instead of `gunzip`. A nonzero exit means failure;
   restart from step 2 with an empty database, not a partly restored one. Use PostgreSQL's
   matching client (16.10+ for dumps containing `\restrict`).
4. **ANALYZE**, to restore planner statistics:
   ```bash
   docker exec freedomridge-restore psql -X -U freedomridge -d freedomridge -v ON_ERROR_STOP=1 -c ANALYZE
   ```
5. **Verify before migrating.** Compare every table's `count(*)` with the manifest's `row_counts`;
   `schema_migrations` must contain `migrations_applied` rows, ending at `schema_version`.
   Check sequences against their table IDs and that no indexes are invalid/not ready:
   ```sql
   SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid OR NOT indisready;
   ```
6. **Migrate to the current code**:
   ```bash
   FRIDGE_DB_CONTAINER=freedomridge-restore ./scripts/apply_migrations.sh
   ```
   Set `FRIDGE_DB_NAME` and `FRIDGE_DB_USER` too if they differ from `freedomridge`.
   This applies pending migrations, re-applies `functions.sql`, and sets the database's own settings
   (`database_settings.sql`), which a dump doesn't carry. Never re-apply `schema.sql`
   to a restored database. If a migration fails, fix the reported problem and rerun the runner;
   its ledger records only completed migrations.
7. **Return to normal database settings on the restored data.** Use **Compose v2** (`docker compose`),
   not v1 (`docker-compose`): v1 can stop/rename the DB and then crash with
   `KeyError: 'ContainerConfig'`. Keep clients stopped, allow a long database shutdown, and retain
   the bind folder from step 2:
   ```bash
   docker stop -t 120 freedomridge-restore
   docker rm freedomridge-restore
   ```
   For a development stack, persist these settings in its `.env`, then start only the DB:
   ```bash
   export COMPOSE_FILE=docker-compose.yml:docker-compose.pgdata.yml
   export FRIDGE_PGDATA_DIR=/path/to/new/pgdata
   docker compose up -d --no-deps --timeout 120 db
   ```
   For a production stack, persist **both files and the same folder**, plus the production
   credentials and the existing `COMPOSE_PROJECT_NAME`, in its `.env`:
   ```bash
   export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.pgdata.yml
   export FRIDGE_PGDATA_DIR=/path/to/new/pgdata
   docker compose up -d --no-deps --build --timeout 120 db
   ```
   **Do not omit `docker-compose.pgdata.yml` in production.** Without it, production uses `POSTGRES_VOLUME_PATH`
   (or the default `postgres_data` named volume), leaves the restored bind folder unused, and can start an empty
   database with green health. Keep this override on every subsequent stack command. Inspect
   `docker compose config` before starting and `docker inspect "$(docker compose ps -q db)" --format '{{json .Mounts}}'`
   afterwards: `/var/lib/postgresql/data` must map to `/path/to/new/pgdata`. Check known dataset/sample
   counts again before enabling clients. Initdb should not run against the restored folder.
   If deliberately restoring into a **named volume** instead, mount that exact volume in step 2
   (`--mount type=volume,source=<project>_postgres_data,target=/var/lib/postgresql/data`) and use the
   same Compose project/volume name in step 7, without the bind override. Do not switch between
   a named volume and a bind folder at handover.
   After an image version change, follow the extension-update checks below before starting services.
8. **Start services with the source's `STORAGE_ENCRYPTION_KEY`.** Stored S3 credentials cannot be
   decrypted with a newly generated key. `/api/health` should report
   `{"status":"ok","pending_migrations":[],"unknown_migrations":[],"stale_functions":[]}`.
9. **Let statistics bootstrap finish.** The job runner rebuilds any dataset's statistics that aren't
   initialized. Allow for a full dataset scan; already initialized statistics do not require another
   full bootstrap.
   Check `SELECT dataset_id, initialized, publish_pending FROM dataset_stats_state`: each dataset
   should reach `t | f`, with `dataset_stats_pending` empty. Do not call the retired
   `refresh_stale_datasets_stats()` function. Subsequent upkeep processes new/changed samples.

The SDK helper, run from a source checkout with `psql` on PATH, performs empty-target validation,
SQL restore, `ANALYZE` and migrations using the **same `DATABASE_URL`**:
```bash
DATABASE_URL='postgres://freedomridge:<password>@127.0.0.1:5432/freedomridge' \
  python -m fridge.backup --restore -i freedomridge_<time>.sql.gz
```
It refuses an initialized target without changing it. Empty/recreate the target explicitly as above.
It accepts extension-only databases and both `.sql` and `.sql.gz`; it does not verify a manifest.
For pre-migration manifest/version comparison use the manual steps above. A failed migration exits
nonzero. The restore itself doesn't compare versions: the migration runner it ends with refuses a
dump newer than the checkout only after loading it, so verify the checkout first.
The direct migration runner accepts `--database-url URL` and `--database-url=URL`. For scripts, prefer
`--database-url-env` with an explicit `DATABASE_URL` so credentials stay off the runner's own argv;
URL mode requires Python 3 and a matching `psql`. All forms remove the password from psql's argv.

### Database image naming and planned recreation

The development Compose DB service names `freedomridge-db:pg18` **and** builds `Dockerfile.db`
(PostgreSQL 18 with pgvector and PostGIS). The name is local: no registry has it, so Compose builds
the image instead of pulling a different one. On a **new machine**, explicitly build our Dockerfile
before starting the dev DB:
```bash
docker build -f Dockerfile.db -t freedomridge-db:pg18 .
docker compose up -d --no-deps --timeout 120 db
```
Production has a build-only DB service; explicitly build it with
`docker compose -f docker-compose.prod.yml build db`. The standalone restore image
`freedomridge-restore:pg18` is another local tag for our build, not a change to the Compose service.

The base-image digest pin affects explicit builds; it does not itself replace a running container.
PostGIS/PROJ packages still come from apt, so a rebuild can change binaries. Retain the tested
image artifact/digest and check PostgreSQL/extension versions before adopting a rebuilt image.
Even a small Compose change can cause the next `up` (including a dependent service's `up`) to
recreate the DB container.

**Any planned DB recreation must use Compose v2**, after a backup and with clients stopped.
Compose v1 (1.29.2) on newer Docker can stop and rename the old DB, then crash with
`KeyError: 'ContainerConfig'`. Switching from a v1-created container to v2 can itself cause a
recreation, so plan that transition too. Use the same project, compose overrides and data mount:
```bash
docker compose version  # must report v2
# Stop backend/runner/worker/backup clients first; use the installation's existing compose files.
docker compose stop -t 120 db
# Build explicitly only when adopting the tested new binaries is intended.
docker compose build db
docker compose up -d --no-deps --force-recreate --timeout 120 db
```
A 10-second default can SIGKILL PostgreSQL during its shutdown checkpoint. Allow at least 120 seconds
(or longer for a large dirty buffer pool); `docker stop -t 120 <db-container>` is the standalone
alternative. Recreating the container preserves its **existing** bind folder or named volume only
when that same mount is selected. Never delete pgdata or use `down -v` for this operation.

When adopting a newer image, compare extension catalog versions with available versions and update
in the replacement database while clients remain stopped:
```bash
docker exec '<replacement-db-container>' psql -X -U freedomridge -d freedomridge -v ON_ERROR_STOP=1 \
  -c "SELECT name, installed_version, default_version FROM pg_available_extensions WHERE name IN ('vector','postgis')" \
  -c 'ALTER EXTENSION vector UPDATE' -c 'SELECT postgis_extensions_upgrade()'
```
Verify extension versions, index validity, representative similarity/PostGIS queries and health
before starting clients. An extension update does not rebuild HNSW graphs; pgvector 0.8.3's release
notes include a vacuum-related HNSW corruption fix, so assess existing indexes separately and plan
any rebuild as explicit maintenance. Do not infer that changing the extension version repaired an
already damaged graph.

### Drill and second-backend isolation

Use a separate container, loopback port, pgdata folder and backend port. Do not start a second copy
from the production `.env` or share its caches, AWS credentials or backup directory. From the scratch checkout:
```bash
mkdir -p /path/to/drill/tmp /path/to/drill/home
# A separate key deliberately prevents this drill from decrypting source S3 credentials.
env -i PATH="$PATH" HOME=/path/to/drill/home LANG=C.UTF-8 \
  DATABASE_URL='postgres://freedomridge:<drill-password>@127.0.0.1:<db-port>/freedomridge' \
  FRIDGE_DB_CONTAINER='<drill-container>' FRIDGE_RUNNER_ENABLED=false SESSION_COOKIE_SECURE=false \
  STORAGE_ENCRYPTION_KEY=drill-only-not-a-secret \
  TMPDIR=/path/to/drill/tmp FRIDGE_CONFIG=/path/to/drill/empty-credentials \
  AWS_SHARED_CREDENTIALS_FILE=/path/to/drill/empty-credentials \
  AWS_CONFIG_FILE=/path/to/drill/empty-config AWS_EC2_METADATA_DISABLED=true \
  python -m backend.launch -- python -m uvicorn backend.main:app --host 127.0.0.1 --port '<backend-port>'
```
`TMPDIR` isolates the S3, composite and crop caches created at import time. `FRIDGE_CONFIG` isolates
Freedom Ridge's credential fallback; the AWS variables and isolated HOME also block ambient SDK
credentials. Leave `FRIDGE_BACKUP_DIR` unset even if the restored DB has an enabled backup schedule.
The example disables the job runner. Inspect restored queued and running jobs and disable/cancel
unwanted work in the **drill DB** before restarting with `FRIDGE_RUNNER_ENABLED=true`. Only then will
statistics bootstrap and other queued jobs run. A DB-only drill does not verify S3 decryption/access.
Test that separately with explicitly scoped credentials; do not grant a drill automatic access to live
storage.

A dump the job runner made also holds the `backup` job that made it, still `running`. In a drill or a
real restore, it needs no action:
- **Restored into a different PostgreSQL cluster** (a new container or machine): restore recovery
  detects the restore and cancels the job (`restored_backup`).
- **Restored into the same cluster** (`dropdb`/`createdb` in an existing container), where detection
  may not fire: the runner resumes the job. If the backup folder still holds that
  job's own verified dump (its manifest's `job_id` names the job; it is the dump you restored), the job
  finishes as succeeded with that file and makes no new dump. Otherwise it makes a new dump at once: the
  dump was pruned or came from elsewhere, or its manifest predates `job_id`. A newer dump in the folder
  never counts. Without `FRIDGE_BACKUP_DIR`, as in a drill, it just fails.

After a cancel the schedule catches up as usual: if a scheduled slot has passed since the dump was made,
the runner starts that slot's backup within a minute. A resumed job that finished from the existing dump
covers the slot.

### Planning recovery time

Measure recovery on an isolated copy with representative data and hardware. Record restore,
ANALYZE, migration, startup and statistics-bootstrap times separately, and verify table counts
and indexes against the backup manifest. Index builds, disk throughput, metadata size and
concurrent workloads can dominate recovery time; compressed dump size alone is not a reliable
estimate. Keep the measurements with your recovery notes.

### Automated daily backups

The `backup` sidecar in `docker-compose.prod.yml` already does this on a schedule
(`scripts/backup_loop.sh`). It runs `pg_dump` every `BACKUP_INTERVAL_SECONDS` (default daily),
checks the gzip stream, copies each dump to `BACKUP_S3_URI` when set, and keeps the newest
`BACKUP_KEEP` local dumps (2 in the production compose file), deleting older
`freedomridge_*.sql.gz` files by modification time. Its dumps have no manifest and no row-count
check.

**The Backups card is empty on the production stack.** Settings → System status → Backups reads
only `FRIDGE_BACKUP_DIR`, which `docker-compose.prod.yml` does not pass to the backend, and the
backend container has no backups volume. So the card shows **NOT SET UP** there, and its hint to
set `FRIDGE_BACKUP_DIR` in `.env` does not apply to that stack. Check the sidecar's log and the S3
objects instead ([Deploy to AWS](DEPLOY_TO_AWS.md#12-backups-and-recovery)). The card and the
scheduled backup job below are for an installation started with `scripts/run_backend.sh`.

For a local install started with `scripts/run_backend.sh`, the job runner makes the backups:
1. Set `FRIDGE_BACKUP_DIR` in `.env` to an existing folder, ideally on another disk, and restart the
   backend.
2. On Settings → System status → Backups, choose how often (every N hours or days, starting at an hour
   of the server's clock) and how many to keep, then Save. "Only when I ask" turns scheduled backups
   off; **Back up now** makes one at any time, for example once for a dataset that won't change.

Each backup is a `backup` job: it shows under Background jobs, and the card follows it until it ends.
It runs `scripts/backup_local.py` and appends its output to `backup.log` in the folder. A new interval
or start hour starts at the next slot (changing only how many to keep doesn't move it); a runner that
was down at a slot makes that backup once when it comes back, and a "Back up now" still running at a
slot counts for it. A backup that a backend restart interrupted runs again when the runner is back,
unless the folder already holds its verified dump (the manifest records the job as `job_id`): then it
finishes with that dump. A failed backup is not retried until the next slot. There is no Cancel button
yet; an admin can cancel with `POST /api/jobs/<id>/cancel`, and the card then shows the stopped run as
FAILED until the next backup.

Slots are counted from 2026-01-01 at the start hour, so an interval that doesn't divide 24 hours
doesn't always land on the start hour, and a weekly backup always runs on a Thursday. The card's "Next
backup" shows the real time.

Only one backend should have `FRIDGE_BACKUP_DIR`. The job backs up `FRIDGE_DB_CONTAINER` /
`FRIDGE_DB_NAME` (by default `freedomridge-db` / `freedomridge`), not whatever database that backend uses,
so a second backend started from the same `.env` (a preview, a restore drill) would back up the live
database into the live folder too.

Without the job runner, run the same script from cron on the machine that runs the database container
(and leave the schedule off, so the two don't both run):
```
0 2 * * * /usr/bin/python3 /path/to/freedomridge/scripts/backup_local.py --dir /path/to/backups >> /path/to/backups/backup.log 2>&1
```
The job uses `docker exec` into the database container, like the script. The Docker `job-runner`
service has no Docker CLI, so in a compose deployment keep the `backup` sidecar and leave the schedule
off.

**How a dump is made and checked**
- It dumps with `pg_dump` inside the container, using the sidecar's flags, so it needs no password and
  the client always matches the server.
- It compresses with pigz on the host, or gzip if pigz isn't installed.
- It keeps the dump only after writing it to disk and reading the whole file back:
  - the gzip stream decompresses cleanly;
  - the dump's closing `\unrestrict` token matches its opening `\restrict` token;
  - every table's row count matches the database. The counts come from the same snapshot the dump
    used, so writes during the backup don't cause false alarms.
- Each verified dump gets a `<file>.manifest.json` with its row counts, size, timings, Postgres
  version, schema version and source database.

**Retention**
- The newest 7 verified dumps are kept (`--keep`).
- The largest verified dump is also kept, so a database emptied by mistake can't push the last full
  backup out. A dump less than half the size of the previous one logs a warning.
- Pruning only deletes dumps that have the script's manifest, plus partial dumps more than 6 hours old
  left by a run that died. Nothing else in the folder is deleted.

**Failures**
- A failed or stopped run ends its `pg_dump`, deletes its partial dump, writes
  `freedomridge_<time>.failed.json` with the reason, and exits 1.
- The folder must already exist. If the backup drive isn't mounted, the run fails rather than writing
  to the disk underneath.
- A lock skips a run if the previous one is still going.
- Settings → System status has a Backups card. It reads `FRIDGE_BACKUP_DIR` (with cron, the same folder
  as `--dir`). It shows the newest verified dump, and turns:
  - red (FAILED) when a failure record is newer than it, with the reason;
  - amber (OVERDUE) when the newest verified dump is older than 1.5 times the schedule (36 hours for a
    daily one). With no schedule it never goes overdue;
  - red (CAN'T READ) when the folder is missing, for example an unmounted drive.

  It only reads the folder: it doesn't check the dumps again, and it only counts `freedomridge_*` files.
  Details are still in `backup.log`.
- Nothing notifies you: the card only shows a problem when someone opens System status.

**Effect on the app**
- Like any `pg_dump`, the run holds a read lock on every table until it finishes. Reads and writes
  carry on as normal, but an `ALTER TABLE` (for example, adding an array field) waits for the dump to
  end, and queries on that table queue behind it.
- If an `ALTER TABLE` arrives just as the dump starts, `pg_dump` gives up after 30 seconds. The backup
  fails rather than hanging, and the next night's run tries again.

Measure dump and verification time separately for your deployment, and budget enough disk
space for the configured retention count plus an in-progress backup.

## Migrating a Dataset to Another Freedom Ridge Instance

### Option 1: Full database copy (fastest)

Best when you want to clone the entire Freedom Ridge instance to another machine.

```bash
# On source machine, select the production Compose database
FRIDGE_DB_CONTAINER=$(docker compose -f docker-compose.prod.yml ps -q db)
docker exec "$FRIDGE_DB_CONTAINER" pg_dump -U freedomridge --clean --if-exists freedomridge | gzip > freedomridge_full.sql.gz

# Copy to target
scp freedomridge_full.sql.gz target-machine:/path/to/

# On the target, follow "Restore from backup" above:
# empty DB (no schema.sql) → restore → ANALYZE → verify → apply_migrations.sh → start services.
```

### Option 2: Dataset sync via SDK (selective, works across different instances)

Use this to copy one dataset to another running instance. It copies samples,
metadata, array fields, embedding vectors and perceptual hashes through the API; no
database access is needed. Run it from a source checkout with the locked environment
active (`uv sync --extra cpu`, then `source .venv/bin/activate`):

```bash
read -rsp 'Source admin password: ' SOURCE_PASS; echo
read -rsp 'Target admin password: ' TARGET_PASS; echo
python -m fridge.sync_between_instances \
  --source https://SOURCE_DOMAIN/api --target https://TARGET_DOMAIN/api \
  --dataset DATASET_NAME \
  --source-pass "$SOURCE_PASS" --target-pass "$TARGET_PASS"
unset SOURCE_PASS TARGET_PASS
```

- The tool logs in with a username and password on both sides; it has no API-token
  option. Always pass `--source-pass` and `--target-pass`: their default is the seeded
  `admin123`, which a production instance never accepts. The passwords stay out of
  shell history but are visible in the process list while it runs. `--source-user`
  and `--target-user` default to `admin`; each account must have finished its
  first-login password change.
- It creates the target dataset if it is missing and skips samples whose filepath
  already exists there. It prints `Sync complete` only when every
  sample, vector and hash arrived, and exits 1 otherwise. `--skip-embeddings` and
  `--skip-hashes` make a faster metadata-only copy.
- Media is not copied. The target must reach the same files at the same absolute
  paths (or the same S3 buckets).
- Maps are not copied. Rebuild them on the target with
  `Dataset("DATASET_NAME").compute_visualization(field_name="clip")` (and `clip_obj`).
- Object rows keep their source `parent_id`, so their links to parent images break
  whenever the target assigns different sample IDs. For datasets with object rows
  (COCO, Open Images, KITTI), use the full database copy above.

A `pg_dump --data-only` of selected tables is not a migration path: it does not
carry the dynamic array-field columns, and the target's triggers reject the rows it
loads. Use one of the two options above.

## What's Included in a Backup

| Component | Stored in | Included in pg_dump? |
|-----------|-----------|---------------------|
| Datasets (name, settings, media fields) | `datasets` table | Yes |
| Samples (filepath, metadata, location, heading) | `samples` table | Yes |
| Embedding vectors (CLIP, DINOv2) | `embeddings` table | Yes |
| Map coordinates and scatter ranking | `embedding_coordinates`, `embedding_viz_rank`, `embedding_viz_meta`, `embedding_layouts` tables | Yes |
| Image hashes (dhash, phash) | `sample_hashes` table | Yes |
| Field indexes and display settings | `dataset_fields` table | Yes |
| Field statistics cache | `dataset_field_stats` table | Yes |
| User accounts and sessions | `users`, `sessions` tables | Yes |
| Saved views | `views` table | Yes |
| **Images and other media** | **Local files or S3 (external)** | **No** |

Media stays where the sample filepaths point: local files below `FRIDGE_DATA_ROOT`,
or S3 objects. A restored database loads its media only when the target sees the
same files at the same absolute paths, or has access to the same buckets. The S3
download cache (`S3_LOCAL_CACHE_PATH`) is not included; it refills as media is viewed.

## Secrets and configuration to keep off-box

Store an access-controlled, encrypted recovery bundle alongside (separately protected from) the
backups. A database dump contains encrypted `storage_configs` secrets but **not their encryption key**.
Keep:

- The exact effective `STORAGE_ENCRYPTION_KEY` that encrypted those rows, including historical keys
  needed by retained backups. Record whether it came from `.env`, a service environment or a secret
  manager. An absent `.env` entry does not prove which key the running process uses; verify its
  effective configuration before changing anything. If the
  installation used the built-in development default, preserve that fact and its code version for
  recovery. Set a private production key only with a plan to decrypt/re-save or re-enter existing
  storage credentials; simply changing the key makes existing ciphertext unreadable.
- The deployment `.env` and service/Compose environment: `DATABASE_URL`, `POSTGRES_USER`,
  `POSTGRES_PASSWORD`, `POSTGRES_DB`, `COMPOSE_PROJECT_NAME`, `COMPOSE_FILE`, `FRIDGE_PGDATA_DIR`,
  `FRIDGE_DB_CONTAINER`, `FRIDGE_DB_NAME`, `FRIDGE_DB_USER`, `HOST_DATA_ROOT`, `TZ`, and database memory overrides. Database
  roles/passwords are not included in a single-database `pg_dump`.
- Storage configuration and access: `STORAGE_TYPE`, `FRIDGE_DATA_ROOT`, `DATA_VOLUME_PATH`,
  `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`,
  `FRIDGE_CONFIG`/AWS credential files, IAM role/policy recovery instructions, and any backup-bucket
  credentials (`BACKUP_S3_URI` and AWS configuration). Local media needs its own backup; S3 media
  needs independent bucket durability/versioning.
- Backup destinations/schedules (`FRIDGE_BACKUP_DIR`, sidecar `BACKUP_*` settings), security/network
  settings (`FRIDGE_ENV`, `SESSION_COOKIE_SECURE`, `ALLOWED_ORIGINS`, `FRIDGE_TRUSTED_PROXIES`, TLS keys/certificates and DNS),
  and other configured integration tokens such as `NEXT_PUBLIC_MAPBOX_TOKEN` and ingest clients'
  `FRIDGE_API_TOKEN`s. Restored databases accept the source's valid API tokens, so isolate drills.
- The code revision, image digest/artifact and extension versions used by the source. Keep the
  manifest with its dump. Do not copy the live `.env` wholesale into a drill.

## Disaster Recovery Checklist

1. Restore the latest verified dump into an **empty database**, with `ON_ERROR_STOP=1`, following
   the procedure above; run ANALYZE and compare table counts and the ledger with the manifest.
2. Run `scripts/apply_migrations.sh` against the restored target. It applies pending migrations and
   repairs function definitions; do not re-apply `schema.sql`.
3. Recover the **same storage encryption key**, AWS access and other off-box configuration. Verify
   stored S3 credentials can decrypt and representative media can load in the replacement install.
4. Return to normal DB settings and start backend, job runner and frontend; check `/api/health`.
5. Monitor automatic statistics bootstrap through `dataset_stats_state`, `dataset_stats_pending`
   and Background jobs. It takes longer for larger datasets; measure it in a drill. Do not call the
   retired refresh function.
