# Continuous field-ingest soak

`scripts/soak_ingest.py` uses the Python SDK's real add endpoint with two inline
normalized vectors (512 and 1024 dimensions), tags, typed metadata, GPS points and
paired perceptual hashes. Every tenth batch is replayed with `on_duplicate='skip'`.
It creates one private `soak-ingest-<UTC>-<random>` dataset, unless `--dataset-id` names an
existing dataset of a disposable restored copy (for example a large dataset at full size, so upkeep runs
against the real indexes, concepts and duplicate configs). `--cleanup` deletes only the
dataset created by that invocation and is refused with `--dataset-id`.

Use a disposable stack, optionally restored from a full-size backup. Start its API,
job runner and the applicable maintenance workers before the run. Install the SDK
and `asyncpg` (already backend dependencies); no extra monitoring service is needed.
Create an admin API token on that stack and export `FRIDGE_API_TOKEN`. The monitoring
connection uses read-only transactions. It needs access to catalog/statistics views,
the job table, and pending queues. Both URLs must include explicit ports; the API URL
must include `/api`. Redirects are disabled.

Example ten-minute run, including a one-minute final drain:

```bash
export DATABASE_URL=postgres://freedomridge:password@127.0.0.1:55460/freedomridge
export FRIDGE_TEST_POSTGRES_DSN="$DATABASE_URL"
export FRIDGE_DB_CONTAINER=fr-cx-soak-db
export FRIDGE_TEST_POSTGRES_CONTAINER=fr-cx-soak-db
export PYTHONPATH=python
# Set FRIDGE_API_TOKEN to the token created on this disposable stack.
python scripts/soak_ingest.py \
  --api-url http://127.0.0.1:18460/api --container fr-cx-soak-db \
  --i-know-this-is-not-live --output /tmp/fr-cx-soak-results \
  --rate 10 --batch-size 25 --duration 540 \
  --burst-every 120 --burst-seconds 20 --burst-multiplier 5 \
  --drain-seconds 60 --recovery-minutes 1 --sample-seconds 10 \
  --retry-every 5 --cleanup
```

Every run requires `--i-know-this-is-not-live`. Even with that flag, port 8000 and
the host/container name `freedomridge-db` are always refused. URL query overrides
and implicit ports are refused. The container argument is a safety declaration,
not a Docker operation or proof of the service behind a hostname. Verify the port
mappings yourself; the script also checks that its newly created dataset is visible
in the telemetry database. It cannot detect arbitrary aliases or proxies for live.
It does not start, stop, restore or modify containers.

Default load is 0.7 samples/second, with fivefold bursts. `--rate` is the base rate;
bursts increase the average. Batches are paced, and a slow API does not cause an
unbounded catch-up flood. Synthetic payload construction is outside ingest latency;
SDK retries and JSON/network work are inside it. Telemetry and UI requests run
independently of ingestion. `--retry-every 0` disables deliberate replays.

For realistic HNSW costs, provision the two indexes on the disposable stack before
the run. `--clip-field` and `--dino-field` can select existing indexed field names
in a restored copy; defaults are `soak_clip_512` and `soak_dino_1024`. The harness
does not build indexes or scan historical samples. New points are placed on a dataset's maps by the
server's `map_place` jobs (from their nearest mapped neighbours), so placement is exercised only for a
restored dataset that has maps (`--dataset-id`); the harness fits none.

Outputs are flushed throughout the run:

* `batches.csv`: each original/replay request, inserted count, latency and error.
* `samples.csv`: queue counts and oldest timestamps, jobs by kind/state, global
  database/table/index/TOAST sizes, individual HNSW sizes, WAL LSN deltas, estimated
  dead tuples and last autovacuum, health and UI page/filtered-count latency.
  Variable sets of queues, indexes and job kinds are JSON columns.
* `summary.md`: verdict, limits, ingest p50/p95/max and final telemetry. Exit status
  is 0 for a measured pass, 1 for failure, 2 for invalid arguments.

Checks fail on any ingest/retry error, health or telemetry error, UI read failure,
or observed failed job (including pre-existing failures). Ingest p95 must be below
`--p95-seconds`. Every burst and the final stop must have an observation where all
discovered queues are at most `--near-zero` within `--recovery-minutes`.
The final drain must cover that deadline. Use a sampling interval appreciably
shorter than the recovery deadline.

Disk evaluation divides the ingestion intervals into thirds and compares aggregate
growth per newly inserted sample in the early and late thirds, including samples,
embeddings and their indexes/TOAST. The late slope must be at most `--growth-ratio`
times the early slope. Six measured growth intervals and a positive early slope
are required; an inconclusive short run fails. This is a bloat indicator, not proof
that an index has no bloat. Restored free space, vacuum and other workloads can
distort the measurement; use a longer run when allocation is lumpy.

Queue discovery recognizes `dataset_stats_pending` and tables containing
`concept`/`dup` plus `pending`/`queue`. Queue lag uses `requested_at`, `queued_at`,
`created_at` or `updated_at` when available, otherwise the age is null. Queue/job
metrics and sizes cover the entire disposable database so restored workloads remain
visible. Queue queries scale with pending work; storage and vacuum reads use catalogs.

This checkout has no concept or duplicate pending tables: those operations still
use the legacy periodic worker, while the job runner schedules statistics and backups.
Missing queues are explicitly reported and are not certified by a passing result.
An empty synthetic dataset has no saved concepts or duplicate-collapse configuration;
prepare those on the newly created soak dataset if those workers must be exercised.
Backups require a configured disposable backup destination and schedule. A run with
no `FRIDGE_BACKUP_DIR`, as in the short validation, does not exercise backups.
Small synthetic runs cannot certify production-scale performance or upkeep scaling.
