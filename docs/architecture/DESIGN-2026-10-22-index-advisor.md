# Index advisor: design (2026-10-22)

Product decisions are approved and not reopened: a read-only review job; every change is an admin click
that runs a job; per-dataset view plus a system-wide view of core indexes; on demand plus optional weekly.
Branch `feature/index-advisor` from main `db1c8a4`; migration **066**.

## 1. Data model (066, mirrored in schema.sql, with a rollback)

- **`field_usage_daily`**: telemetry rows (§2).
  - Columns: `dataset_id` (FK, ON DELETE CASCADE), `usage` (filter, sort, facet or histogram), `field_name`
    (`''` means the request had no field in that role, which is the unfiltered baseline), `day` (UTC),
    `uses`, `total_ms`, `latency_log2 int8[16]`.
  - Primary key `(dataset_id, usage, field_name, day)`, plus an index on `day` for pruning.
- **`removed_indexes`**: intentional removals (§6).
  - What was removed: `index_name`, `table_name`, `definition` (its `pg_get_indexdef`), `kind`
    (core, field, ann, array or other), `dataset_id`/`field_name` for field indexes.
  - Its state when removed: `size_bytes`, `scans`, `last_scan`, `observed_since`, `reason`.
  - Removal: who, when, `dropped_at`, the drop job.
  - Restore: who, when, the restore job.
  - At most one open record per name.
- **`index_observations(index_oid, system_identifier, index_name, first_seen_at, last_scans)`**: when the
  advisor first saw each physical index (§4).
- **`system_job_schedules`**: one row for `index_review`, seeded off, weekly Sunday 03:00 America/Chicago.
  It has the same rule columns as `dataset_job_schedules`, which is the simplest system-wide equivalent.
- **`dataset_job_schedules.job_type`**: the CHECK gains `'index_review'` (target `'indexes'`). This is the
  per-dataset weekly review, reusing #137.
- **Advisory registry**: a new key **20261067** covers drop and restore of core, array and other indexes, by
  `hashtext(name)`. Field indexes keep 20261017 per dataset; ANN indexes keep 20261066 per field.
- **Review results**: kept in the review job's `result`. The advisor reads the latest succeeded review for a
  scope; there is no review table.

## 2. Usage telemetry (app side; `pg_stat_statements` stays off)

- **Shape**: each entry is uses, total ms and a log2-ms latency histogram (16 buckets; the last is ≥ 16 s), for
  one request role and field. Percentiles come from the buckets. Nothing is thresholded when it is recorded.
- **Recorded**:
  - Filter and sort: `POST /samples/query` and `GET /samples`, using the whole request time. Filter counts only
    the operators a field index serves: `=`, `in`, `<`, `<=`, `>`, `>=`. A request with no such field records
    the field `''`.
  - Facet: each field's own sidebar query (`_facet_query`), plus autocomplete.
  - Histogram: `GET /histogram`.
  - Scatter, map, aggregate and tag counts are not recorded; they reuse the same filters.
- **Cost**:
  - On the hot path, one dict update per request (no I/O).
  - Each API worker flushes once a minute in a single `INSERT … SELECT unnest(…) ON CONFLICT DO UPDATE`
    (sums, element-wise histogram adds).
  - The flush joins `dataset_fields` and non-deleting `datasets`, so unknown or forged fields are dropped.
  - A crash loses at most one minute of telemetry.
- **Bounded**: the flusher deletes rows older than W (§4) once a day. That leaves at most fields used × 4 × 8
  rows per dataset.

## 3. Jobs

| Kind | dataset_id | Lane | Lock (session lock on its own connection) | Policy |
|---|---|---|---|---|
| `index_review` | NULL (`args.scope`, `args.dataset_id`) | user | none | 300 s unit |
| `index_drop` | the dataset for field indexes, otherwise NULL | user | 20261017·ds / 20261066·field / 20261067·name | 12 h liveness (as for backups) |
| `index_restore` | NULL | user | same as drop | 12 h |

"Index this field" and restoring a field index use the existing `field_index_build`, through promote.

**Reviews are dataset-less.** That way they never wait behind a dataset's lane owner, such as a bulk job or an
hour-long build. The user lane only lets slot 0 take a click instead of queueing behind backups and fits.

**Reviews write bookkeeping only, never DDL:**
- They upsert `index_observations`.
- They close a removal record whose index has reappeared, noting it in the result as "re-created outside the
  advisor".

**Drop:**
1. Unit 1 refuses an index that is needed, building, or whose definition is no longer `args.definition` (what
   the admin saw). Its fenced publication inserts the record. For a field index it also sets
   `is_promoted=FALSE` under the shared lifecycle try-lock. This happens *before* the drop: a promoted field
   without its index turns facet index-descent into one scan per value.
2. Unit 2 defers, without spending failures, while a backup is queued or running (`_wait_for_backup`). It then
   takes the lock, re-checks, and runs `DROP INDEX CONCURRENTLY IF EXISTS`:
   - `client_connection_check_interval` is set.
   - There is no lock or statement timeout. SHARE UPDATE EXCLUSIVE blocks neither reads nor ingest.
   - The fenced publication sets `dropped_at`.

**Restore:**
1. Requires free space for the recorded size (`require_free_space`) and waits out a backup.
2. Takes the lock and looks for an existing index with that name:
   - valid, same definition: finish;
   - INVALID: drop the carcass;
   - different definition: fail.
3. Rebuilds from the recorded definition, rewritten to `CREATE INDEX CONCURRENTLY IF NOT EXISTS`.
   It goes through `index_jobs._build`, which verifies `indisvalid` and drops a carcass on failure or
   cancel. ANN restores keep the ANN memory settings.
4. Checks that `pg_get_indexdef` equals the recorded definition, then publishes `restored_at`.

## 4. Rules (thresholds derived)

**Window W = one review period = 7 days**, taken from the approved weekly cadence. W is used three ways: the
telemetry window, the minimum observation time, and the payback horizon.

**Statistics start S** = `GREATEST(pg_stat_database.stats_reset, pg_stat_bgwriter.stats_reset)`. Measured
today on PG 16.15:
- A fresh cluster has a NULL database timestamp and a stamped bgwriter timestamp.
- A clean restart keeps the counters and both timestamps.
- Crash recovery zeroes the index counters and stamps only bgwriter's timestamp.
- `pg_stat_reset()` stamps the database's timestamp.

**Observed since** = `GREATEST(S, first_seen)`, where `first_seen` is keyed by OID and the cluster's system
identifier:
- First deploy, with no observations at all: the newest creation hint, else S. Hints are the latest
  succeeded build job, or for core indexes the ledger `applied_at` of the newest migration naming them.
- Only another system identifier has observations (a dump restore or `pg_upgrade`): now.
- A new OID, or a counter that went down (per-table reset): now.

**Needed** (shown with the reason; no Remove):
- primary key, unique, exclusion, constraint-backed (`conindid`), replica identity;
- the *only* valid index whose leading keys are a foreign key's columns. Example: `idx_dup_secondary_sample`.
  Without it, sample deletes scan the child table.

**Possibly unused:**
- An index qualifies when it is valid, not needed, not building, and idle ≥ W, where
  idle = now − `COALESCE(last_idx_scan, observed_since)`.
- If observed < W, the section says *"not enough history: statistics since {date}"*.
- Columns: size, uses since the observed date, last use.
- Sorted by size; the UI pages 10 at a time.
- INVALID indexes are always listed: they are never used and are still written while `indisready`.

**Overlapping**: same table, access method and predicate, and one of:
- (a) identical keys and opclasses (a duplicate);
- (b) btree, where one index's keys are a leading prefix of the other's;
- (c) the same keys, where one opclass's operator family is a subset of the other's in `pg_amop`.
  Example: `jsonb_path_ops` {@>, @?, @@} ⊂ `jsonb_ops`.

The redundant member gets Remove.

**Suggested** (per dataset):
- **Candidates**: fields of a type promote can index that have no valid index, or are promoted but their
  index is missing.
- **Reference R(usage)**: the mean latency on this dataset's indexed fields over W. If none of them has uses,
  it is the dataset's mean for that usage, including the `''` baseline.
- **Slow**: the field's mean > R.
- **Often**: E = Σ uses × (mean − R) over W is at least B.
  - B is the measured build time: the median seconds per heap byte of recorded field builds, times today's
    `samples` heap size. Builds now record `build_seconds` and `table_bytes`.
  - Heap size is the right base because a partial-index CREATE INDEX CONCURRENTLY scans the whole heap twice.
- In plain words: last week, users waited longer on this field than an index takes to build.
- While B is unmeasured, slow fields are listed by E and marked "build time not measured yet".

## 5. API and permissions (all `require_role('admin')`)

- **Read the advisor**: `GET /api/datasets/{id}/index-advisor` and `GET /api/admin/index-advisor`. Both return
  the latest review, the active review job, the schedule, open removals, missing core indexes and S.
- **Start a review**: `POST …/index-advisor/review` queues one, or joins the one already queued for that
  scope (dedupe). It follows the five-second rule.
- **Remove or restore**: `POST /api/admin/indexes/{name}/remove {definition}` and `…/restore` return 202
  `{job_id,state}`. They answer 409 when the index is needed or building, its definition changed, or a
  drop/restore/build of it is unfinished.
- **Index this field, or restore a field index**: the existing `POST /fields/{f}/promote`.
- **Schedules**:
  - System: `PUT /api/admin/index-advisor/schedule`.
  - Per dataset: the existing `/datasets/{id}/schedules` with `job_type='index_review'` (admin only).
  - The scheduler pass (lock 20261064, `next_slot`) also handles `system_job_schedules`.
  - Index reviews have no conditions or start window; a missed slot runs on the next pass.

## 6. UI, and how removed core indexes are represented

**Dataset Settings → Indexes** (admin only, below today's table):
- Header: *Last reviewed {time} · statistics since {S}* and **Review now**.
- *Suggested*: uses per role, p50/p95 against indexed fields, E against B, **Index this field**.
- *Possibly unused*: size, uses, last use, **Remove**.
- *Overlapping*: the pair, the reason, **Remove**.
- *Removed*: who, when, **Restore**.
- Remove opens an inline confirmation in the page (no `confirm()`): size, uses and window, "drops online;
  Restore rebuilds online".

**System status → Indexes card**: the same sections for every index a dataset does not own (core, ANN,
array GIN, other), plus *Needed* (collapsed), *Missing core indexes*, and *Weekly review: off/on {day} {time}*.
Job names and the Schedules job-type choice are added.

**Removed indexes** are the open rows of `removed_indexes`. Each place that could re-create one or misreport
it was checked:
- `schema_check` and `/api/health` check only the ledger and function bodies. A guard test keeps health at 200
  after a removal.
- `apply_migrations.sh` re-applies only `functions.sql`. The statistics index runs only while 054 is pending;
  catch-up runs only before 043. It never re-creates an index.
- A fresh install from schema.sql creates every core index, by design: a removal is this deployment's
  operational state, since indexes are not versioned.
- A dump lacks the index and carries its record. If it is restored into a database initialised from
  schema.sql (drill P1, unsupported), or an operator script runs (`copy_objects.py`,
  `backfill_sample_lonlat.py`, `build_ann_indexes.py`, `create_array_field`), the next review reports
  "re-created outside the advisor" and closes the record.
- The startup "No HNSW index" warning skips fields whose ANN index is recorded as removed.
- The system review lists *missing core indexes*: names created in schema.sql that are absent and not
  recorded as removed. In v1 this is a report with no button.

## 7. Edge cases

- **Builds in progress** (`pg_stat_progress_create_index`) are shown as "building", left out of the rules,
  and refused by remove and restore.
- **CONCURRENTLY failing part-way:**
  - A drop left INVALID keeps its record open with `dropped_at` NULL. "Remove again" retries, which is
    idempotent.
  - A restore drops its carcass. If that drop cannot get its locks in 5 s, the next attempt clears it first.
  - A killed runner's build aborts through `client_connection_check_interval`.
- **Cancel between drop units**: the index stays and the field stays unpromoted. The next review closes the
  record. Promote then only re-marks the field (IF NOT EXISTS).
- **Dump restore**: the new observations and S read "not enough history" for a week. That is correct, because
  the counters are new.
- **Dataset deleted**: its review finishes "dataset deleted", its drop is cancelled by orphan cleanup, and its
  telemetry and records cascade.
- **Very large tables:**
  - A review reads only catalogs, `pg_stat_*` and `pg_relation_size`.
  - A drop is just an unlink.
  - Restoring the 5.5 GB GIN scans the `samples` heap twice. Ingest and reads continue. Autovacuum and other
    builds on `samples` wait; a field build may hit its 1 h budget, which is documented.

## 8. Test plan

Backend tests run on a throwaway database. Each test fails on db1c8a4 and passes after.
1. **066**:
   - It applies on the 065 chain, can be replayed, and matches schema.sql.
   - The rollback refuses while advisor jobs are unfinished and prints open definitions.
   - The runner requires 066, and 20261067 is registered.
2. **Telemetry**:
   - Each hook writes the right (usage, field) rows; `contains` is not counted; `''` baselines appear.
   - Unknown fields and deleting datasets are dropped.
   - Concurrent flushes add up correctly, and pruning removes rows older than W.
3. **Rules** (pure functions with injected now and S, plus catalog fixtures):
   - each observation case, and "not enough history";
   - needed indexes, including the FK-only one and `idx_jobs_lane_owner`;
   - overlap (a), (b) and (c), with the live GIN pair as (c);
   - suggestions: payback, the reference fallbacks, unmeasured B, a promoted field with its index missing.
4. **Drop**:
   - A core GIN is dropped while another session holds an open INSERT transaction. The writer is never
     blocked, and the drop completes after its commit.
   - The record has the definition, who and when.
   - It refuses needed, building and changed indexes.
   - It defers during a backup without counting a failure.
   - Cancel between units works, and an INVALID retry completes.
   - For a field index, unpromote happens before the drop.
5. **Restore**:
   - The identical definition comes back valid.
   - A conflicting definition fails, and a carcass is dropped first.
   - A failure drops the carcass, and low disk space answers 507.
6. **Re-creation**:
   - A re-created index closes its record.
   - A core index dropped by hand is reported missing; one dropped by the advisor is not.
   - The startup warning skips removed ANN indexes.
   - A guard test keeps `/api/health` at 200.
7. **Schedules and roles**:
   - Both kinds of schedule queue a review when due and advance `next_run_at`.
   - Every advisor endpoint answers 403 to power users and viewers.
8. **e2e** (port suffix 83):
   - Dataset Indexes: review, then a seeded suggestion, then Index this field, then the field shows as indexed.
   - System status: review, then the overlapping GIN pair, then Remove, confirm, Removed, Restore, and the
     index is back.
   - A non-admin sees no advisor.

Ruff, flake8, mypy, tsc, eslint and the build pass. JOBS.md is updated.

## Review 2026-10-22 (applied)

An index advisor review established these rules:
- **F1, joint verdicts.** An overlap's kept index is never itself offered, and it must be used (or
  needed) at least as much as the redundant one. The removals offered together never leave a foreign
  key without an index.
- **F2, no back-dating.** First-seen starts when the advisor observes an index; 066 seeds it. A restore
  starts over, keyed by cluster and database identity.
- **F3, full key definitions.** Sort options, INCLUDE columns, collations and operator families are all
  compared.
- **F4, build time.** The measured build time excludes lock waits.
- **F5, telemetry.** Only completed requests and promotable names are recorded, and a request's time is
  shared among its fields.
- **F6, windows and slots.** The idle window is 14 days (two review periods). The review defaults to 06:00.
- **F7, rollback order.** The 064 rollback refuses while 066 is applied.
- **F9, unmeasured builds.** Without a measured build, only clearly slower (a whole latency bucket)
  promotable fields are suggested.
- **F10, robustness.** One review never aborts the scheduler pass. The system view lists orphan field
  indexes, and the empty-state text is accurate.

## Re-check of c238670 (applied)

- **R1, "twice as slow".** It means twice the measured mean, not histogram bucket edges.
- **R2, tests.** New tests kill the three surviving mutations.
- **R3, the lock-wait sampler.**
  - It borrows a connection per sample.
  - An unwatchable backend records no build time.
  - A sub-second build is floored at the sampling resolution.
- **R4, clean shutdowns.** The fix is documented, not configured: stop the database with
  `docker stop -t 120 freedomridge-db`, so it is never SIGKILLed mid-checkpoint and keeps the index
  counters. Adding `stop_grace_period` to the compose db service would recreate the database container
  at the next `docker compose up`, which an existing guard forbids (decision pending).
- **R5, the review slot.** The default slot carries past midnight, and the wording now says the start
  window bounds a re-fit's start, not its run.
- **R6, noted.** Sharing a request's time among its filter fields makes a field's mean depend on the
  fields used with it. A field always filtered alone is compared at its full request time, against
  indexed fields often used in combination. This biases suggestions toward fields used alone. It is
  accepted as a heuristic and documented with the rule.

## Re-check of 02fb3d4 (PR B)

- **R1, a waiting drop names its blocker.** While a removal's DROP or a restore's build waits, a sampler
  (every 2 s, the page's polling interval) writes the blocking sessions into the job's message: pid,
  application name, state and transaction age. The jobs API shows that detail to administrators only;
  other readers of a field removal (a dataset job) see "waiting for older transactions on the table".
- **R2, footprint by access method.** A restore reserves GIN 1.89× and btree 2.57× the index (measured);
  other methods take the larger.
- **R5, the ANN build's table lock** has a test: a second embeddings build during a waiting one defers;
  without the lock it ends in `DeadlockDetectedError`.
- **Follow-ups (not fixed):**
  - **R3.** The drop and the restore wait out a backup only before they start. A scheduled backup that
    starts during one stretches it by the whole dump (the build's last phase waits for pg_dump's
    snapshot), holding the table's index lock meanwhile. Fix: let the backup scheduler also skip while
    an `index_restore` or `index_drop` runs, as it already does for a re-fit's publication.
  - **R4.** Rename or clear of a promoted field, and a new array column, answer 409 (`Retry-After: 30`)
    during any index build or removal on `samples`, including another dataset's ordinary promote. The
    SDK retries for 120 s. Syncs are unaffected (an existing array column never reaches the DDL). Say so
    in JOBS.md and in the 409 detail ("an index is being built on samples, by any dataset").

## Choices taken (none blocks; veto any)

1. **Admin only.** Viewing, reviewing, acting and index-review schedules are admin only. Power users keep
   the existing promote toggle.
2. **FK-only indexes are needed.** They are shown with no Remove: dropping one brings back the 0.4 s/row
   delete bug.
3. **One W.** A single 7-day W comes from the weekly cadence. The latency buckets are a resolution, not a
   threshold, and 10 rows is paging.
4. **Reviews are dataset-less.** They never wait behind a dataset's lane. Decided 10-22: they are still
   shown where the user acts. The Indexes tab shows the last review, its status and a spinner while one
   runs. The dataset's Jobs panel and history list it through `args.dataset_id`, with
   `dataset_lane: false`, so it neither waits behind the lane's owner nor holds the lane up.
5. **The demote toggle now drops online** (decided 10-22; built in PR B). It used to run a plain
   `DROP INDEX`, which takes ACCESS EXCLUSIVE on `samples` and so queues behind a pg_dump, stalling every
   read and write behind it. It now runs the same `index_drop` job as the advisor (marks the field not
   indexed, waits out backups, then `DROP INDEX CONCURRENTLY`), without a removal record. The add-index
   path (promote) already used `CREATE INDEX CONCURRENTLY`.
6. **Live expectation for `idx_samples_metadata_gin`:**
   - It appears under *Overlapping*, by rule (c).
   - It appears under *Possibly unused* (0 uses, 5.5 GB) if S on live is at least 7 days old.
   - Remove frees 5.5 GB online.
   - Restore needs about 5.5 GB plus headroom, and a CREATE INDEX CONCURRENTLY of a length not yet measured.
