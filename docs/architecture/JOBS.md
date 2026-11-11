# Job runner foundation

Freedom Ridge needs background work that survives a disconnected browser or a restarted
worker without making ordinary dataset edits wait behind bookkeeping. The foundation uses
the existing PostgreSQL database and a separate co-located worker process. It does not
require Redis, a remote machine, or a second deployment for a local installation.

## Current scope

The foundation, local supervision, selection cleanup, execution-slot health, dataset indicator,
cancellation, history, unit deadlines, logical lane ownership and incremental statistics are merged.
Concept and exact-duplicate upkeep use the same runner. The hourly Docker worker now only
prunes disk caches; it no longer performs database maintenance.

A singleton row records a transaction high-water mark and the PostgreSQL system identifier.
A changed identifier, a backwards counter, or a missing marker requires recovery. This detects
logical restores even when the target counter falls between the dumped mark and later writers.
Physical replica promotion keeps the identifier; pg_upgrade changes it and safely normalizes once.
Claims read the marker without allocating an xid or acquiring its lock. While recovery is pending,
statistics, concept and duplicate claims and scheduling wait; backups and selection cleanup continue,
except a backup that had already started.
A separate runner task checks and refreshes the marker every five seconds. Competing recovery tasks
use a nonblocking advisory lock. Queue visibility retains exact saved-snapshot checks.

Recovery locks and rewrites the five pending queues once, merging their keys under past xid 3.
It has its own transaction, with a 30-second lock wait and a one-hour statement budget, outside the
short queue transaction deadline. Ingest pauses during the rewrite. Recovery does not lock the jobs
table; only affected job rows are updated after queue merging, so backup claims remain available.
The runner cancels and rolls back recovery on shutdown and retries failures with backoff.
Statistics field names are unioned (NULL wins), count deltas are summed, manual-scoring
watermarks are cleared, and concept definition writer IDs reset to 3. Unfinished statistics,
concept and duplicate jobs keep their IDs and
requests but lose their leases and checkpoints and receive fresh barriers and current upper
bounds. Failed/cancelled jobs lose resumable source snapshots too. A started backup that is queued, or
running with an expired lease, is cancelled (`restored_backup`), not re-run: every dump the runner makes
holds its own backup job mid-run. A restore into the same cluster isn't detected and resumes that job;
the backup handler finishes any resumed attempt from the verified dump whose manifest names the job
(`job_id`), and dumps again when the folder has none. Cancellation requests stay
in force. The mark resets in the same transaction; failure rolls back the whole recovery.
Warnings identify missing markers, recovery start and completion; PostgreSQL logs each merged queue row count. Work is proportional to pending queues and job
metadata, with indexed upper-bound probes; samples and embeddings are not rebuilt by recovery.
An interrupted full bootstrap restarts its scan. Normal restarts and counter advances in the same cluster leave work unchanged. A different cluster
always normalizes, even when its counter is higher than all saved writer IDs.

After a restore, start the runner before resuming application traffic.

The dataset indicator (below) shows running and waiting work on current datasets and saved-version
browsing. It is read-only: it does not convert synchronous operations or change their locking behavior.

## Execution contract

1. **Submission.** An operation submits a `jobs` row with a `lane`: `user` for work a person asked for, or
   `maintenance` for scheduled upkeep. Deduplication joins only an identical request that is still
   queued, unstarted and not being cancelled, from the same submitter (`dedupe_owner`).
   - Generic submissions with a different `barrier` queue a follow-up. Statistics explicitly
     coalesce a later barrier into the same submitter's unstarted, uncancelled queued refresh.
     A started job's barrier is immutable; new work then gets a follow-up.
   - Reusing a key with different arguments is an error.
   - So a dedupe key prevents a duplicate *queued* job, not a second run: an identical request after the first
     started runs again. That is right for refresh-like work, which must see newer writes. A non-idempotent
     operation (for example, version creation as a job) needs a per-kind policy that also joins the
     started job.
2. **Claiming.** A short `SKIP LOCKED` claim gives one slot a time-limited, random lease token. Each
   dataset has one logical operation owner per `lane` value. Ownership starts at the first claim and
   lasts until success, failure or cancellation, including queued retry/yield intervals. The unique
   `idx_jobs_lane_owner` index enforces this using existing job state and `started_at`; there is no
   second durable owner record to keep in sync. Short `dataset_job_lanes` row locks serialize claims;
   candidate filtering materializes the owner set once per poll and excludes blocked followers before
   the candidate limit, rather than rescanning job history for each waiting job. After locking a lane,
   the claim rechecks ownership to cover concurrent claims.
   Maintenance and user lanes remain independent. Server and compute jobs share those lanes.
   Ordinary edits never take a job-lane lock.
3. **Slots.** A runner has N slots (`JOB_WORKER_SLOTS`, default 3). With more than one slot, slot 0
   takes only `user` work, so long maintenance units can't occupy every slot. Its handler pool is explicit
   (`JOB_POOL_MAX_SIZE`, default 2 per slot), plus one connection reserved for restore recovery.
   - Slots share one event loop and that pool. A handler that blocks the loop, or holds many connections,
     still delays the user slot, hence the handler rules below.
4. **Measurement.** The handler computes one resumable unit without holding a job row lock and returns
   its next checkpoint, progress, and an optional bounded publication callback. An omitted checkpoint
   keeps the previous one.
   - Each kind has a `JobPolicy`, including `max_unit_seconds`. A unit past its wall time is
     interrupted. This deadline covers measurement, not subsequent notification cleanup or
     publication. Each unit owns its timeout signal and records completion as the handler returns;
     a delayed notification cannot fail a later unit. The lease thread stops renewing while an
     overdue handler is still running (including cancellation cleanup), so uncooperative work
     cannot retain its lease indefinitely. Publication has its own bounded budget below.
   - A durable cancel request seen by the heartbeat interrupts measurement at once. Effects only
     happen at publication, so interrupting is safe.
5. **Publication.** Publication locks and validates the lease, writes the unit's effects, and records
   its checkpoint in one short transaction (5 s budget). A 10 s `idle_in_transaction_session_timeout`
   ends a stopped owner's transaction so it cannot keep the row locked. It stays above the budget, so
   a slow but live publication is not killed. An expired or replaced owner cannot publish.
   Queue transactions also have a 10 s client wall-time guard that terminates their owned connection.
   A silently dropped socket must not trap claim, submit, cancel or rollback until a TCP timeout.
6. **Cancellation.** Cancel requests live in `job_controls`, separate from the job row, so a cancel
   never waits for an in-flight publication. A request recorded before publication prevents it; one
   arriving during publication takes effect at the end of that unit.
   Each claim pass also finalizes up to 32 queued cancellations independently of supported kinds,
   placement, ready time and lane ownership. A locked row is skipped and reconsidered on a later pass.
7. **Leases.** Each running slot owns a dedicated thread and reuses one healthy connection across jobs.
   A failed renewal or telemetry operation discards that connection; a new attempt also refuses a
   session with an open transaction. The lease is renewed there, so a handler that
   blocks its event loop or exhausts the handler pool cannot lose its lease. A renewal error that is
   transient (connection loss, too many connections, admin shutdown, a lock or statement timeout) is
   retried until the lease would actually expire. A session lost mid-transaction counts as transient
   too, although asyncpg reports it with client errors that have no SQL state. Such a connection is
   terminated, not returned to the pool.
   Renewal and telemetry have separate wall-time budgets; settings must leave room for the heartbeat
   interval, telemetry, and two renewal attempts including connection establishment, the renewal query
   and the retry delay. Slow telemetry cannot consume a whole lease.
   A wall-time timeout closes the socket before cancelling the operation or waiting for rollback.
   A cancelled BEGIN can otherwise leave an apparently open session whose later renewals are nested
   savepoints, never committed. Cancellation cleanup cannot be allowed to wait on that same socket.
   The lease connection sets its server-side idle-transaction limit at connection startup as well,
   covering an interrupted BEGIN before the queue can issue its transaction-local settings.
8. **Yielding.** A time slice ends after a completed unit, never halfway through one. The job is
   requeued behind other ready work on other datasets or the other lane, retaining its own lane.
   This releases the worker slot, not operation ordering; no transaction stays open between slices.
9. **Failures.** `failures` counts failures since the job last made progress, separately from `attempt`
   (every claim, including yields). Publishing a new checkpoint resets it, so a job that keeps advancing between
   crashes is not killed by their total, while a unit that crashes before publishing still reaches the limit.
   - A transient error, or an expired lease, defers the job with exponential backoff (`retry_seconds`
     doubling up to `max_retry_seconds`).
   - Database lock contention defers the unit without increasing `failures`. Statement timeouts and
     resource/connection failures still consume the bounded failure budget; a slow unit must not
     retry forever just because lock contention is harmless.
   - Any other error, or reaching `max_failures`, fails it with the last error.
   - A queued owner whose kind/placement has no worker still retains its lane. Restore a compatible
     worker or explicitly cancel it; silently allowing a conflicting operation to overtake its partial
     effects would be unsafe. Other datasets, the other lane, reads and ordinary edits remain available.

Queue transitions use bounded lock waits. Handlers must do measurement and scans before publication,
not inside its callback. A repeatedly timing-out unit needs smaller work subdivision; retry backoff
alone is not a solution to oversized units.

**Rules for handlers:**
- **Never block the event loop.** All slots in a runner share one loop, so a synchronous section stalls every
  slot, including the one reserved for user work. Run CPU work and blocking calls with `asyncio.to_thread`
  or in a subprocess.
- Cooperate with task cancellation. A stray handler or publication callback `CancelledError` fails
  that job, not its execution slot. Stray claim/telemetry cancellation is isolated from the slot loop.
  Actual supervisor cancellation still stops the slot and rolls back in-flight publication.
  Non-cooperative Python code is not
  forcibly interruptible by an asyncio timeout; the outer process supervisor bounds shutdown.
- **Bind each kind to one lane.** Lanes exclude only within a lane, so the same kind submitted to both would
  run twice at once on one dataset (two statistics passes could certify an older block after a newer one).
- **Maintenance results must check the dataset hasn't changed under them.** A maintenance unit can overlap
  a user job on the same dataset, so its publication validates a dataset generation (or the field
  registry's definition identity) before writing. A kind that needs the whole dataset to itself (such
  as version revert as a job) must also wait for the maintenance lane to be idle.

Delivery is at least once. A killed process leaves its lease to expire, and another worker resumes the
stored checkpoint. Transactional effects belong in the fenced callback. External effects must be
idempotent and keyed so a retried unit does not duplicate them.

### Long atomic operations

Version create and revert are still synchronous endpoints. Running them as jobs needs distinct
protocols, not a larger `publish_step` timeout:

- **Create captures one coherent snapshot.** Measure the sample count, field registry, annotations,
  and sample contents in the same repeatable-read snapshot, as the current synchronous endpoint does.
  Repeated READ COMMITTED queries can otherwise disagree as ingest commits between them. Stage the
  capture privately under its attempt/lease identity, with no visible saved version yet. A separate,
  short publication transaction validates the current lease and cancellation, then makes that complete
  capture visible atomically. Abandoned attempts are disposable; an expired worker must never publish
  its capture. Do not try to lock the heartbeat-updated job row inside the older capture snapshot.
- **Revert replaces live state atomically.** It uses the explicit destructive lifecycle gate and a
  READ COMMITTED mutation transaction, without holding a job-row lock during expensive work. A separate
  connection renews the lease. At the end, the mutation transaction locks the latest job row, validates
  the lease and cancellation, records the terminal result, and commits immediately. Losing the fence
  rolls back all live effects. Calling pool-based completion inside that transaction breaks atomicity.

Ordinary edits and ingest can continue during capture. Revert is the explicit destructive exception:
writers use the lifecycle gate's existing bounded wait/retry behavior. Replacement workers must try/defer
rather than wait behind an expired owner's open transaction; the old handler must observe lease loss and
cancel/roll back promptly. That conversion needs tests for capture coherence, visibility,
abandoned-stage cleanup, cancellation and atomic commit. These are conversion requirements, not
implemented job modes; the map re-fit's swap (below) already uses the revert-style fence.

## History and access

Jobs retain the immutable dataset ID when a dataset is deleted, so a delete job can finish
and clients can still read its result. New submissions require an existing dataset. Other
queued work for a deleted target is terminalized instead of blocking unrelated datasets.

### Dataset deletion

`DELETE /api/datasets/{id}` now returns **202** with `{job_id,state}`. A short,
nonwaiting lifecycle/index gate marks the dataset `deleting` and submits exactly one
`dataset_deletion` job in the user lane. Repeated DELETE returns the same job. The dataset
remains listed, marked as deleting, until teardown finishes; its existing Jobs panel
provides progress and cancellation. The list and dataset page also show stopped deletion
and an administrator's resume action. Busy admission returns 409 with `Retry-After: 1`.
Names are unique among non-deleting datasets only: sealing immediately frees the name
for a replacement, while the old row keeps its name and ID for progress and resume.
SDK and operator-script name lookups choose the active dataset. E2e fixture cleanup
waits on the returned job and resumes stopped cleanup left by a crashed run.

The SDK's `client.delete_dataset(id, wait=True, timeout=1800)` and `Dataset.delete()`
poll the job and preserve the former `{"message":"Dataset N deleted successfully"}`
result. `wait=False` returns a `Job`; a waiting timeout does not cancel server work.
Raw HTTP clients must follow the returned job rather than treating 202 as completion.
After the dataset disappears, the submitter or administrator can still read `/api/jobs/{id}`.

The deletion handler drains concept scores/pending work, version snapshots, dataset
caches/definitions, then sample children and samples. Duplicate memberships are drained
before their config registry; coordinates and projection-pending rows are drained before
embeddings, so neither config nor embedding cascades hide unbounded fan-out. Each fenced
publication deletes at most 1,000 rows and 8 MiB of measured row data and commits its checkpoint in the same
transaction. Patch embeddings and hashes have their own child-row batches: the sample
page alone never bounds fan-out. Sample IDs remain in the checkpoint until their children
and all page samples are removed. Per-table keys seek past deleted index entries even
before vacuum runs; concept/version child cursors reset for each parent.
A single row larger than the byte budget fails explicitly
instead of silently admitting an unbounded unit. Failure retries shrink the row batch.
No table-wide DELETE, statistics rebuild, or final full-dataset count is needed.

Dataset-owned promoted sample indexes are dropped one at a time with `DROP INDEX
CONCURRENTLY`, outside publication transactions. Their dataset-qualified names and the
existing index advisory lock isolate them from other datasets. Lease/cancel checks precede
the DDL; a crash after a successful drop simply finds the next index on retry. This
idempotent external effect, like backup file work, does not require a new runner primitive.
The final fenced transaction removes the dataset row (only empty child relations and
the two lane rows remain) and publishes success. Shared/global indexes remain intact.

Cancel stops at a unit boundary and leaves a **partially deleted, read-only dataset**.
It does not restore removed data. `POST /api/datasets/{id}/deletion/resume` returns 202
and requeues the same cancelled/failed job without changing its checkpoint.
The SDK exposes `client.resume_dataset_deletion(id, wait=True, timeout=1800)` and
`Dataset.resume_deletion()`. A pending cancellation must finish before resume.
Authorized GET and read-only POST endpoints (including the grid, field statistics and
selection queries) remain readable. Results reflect whichever rows remain; deletion
does not preserve a read snapshot. Field-stat reads return uncached values if sealing
prevents cache publication. Mutation endpoints still return 409.

Datasets have lifecycle columns and database write guards. The guards cover direct
SQL/SDK writes as well as routers, and a locking lifecycle read fences writers using an
older repeatable-read snapshot. Other job submissions/publications reject a deleting
dataset. The guard takes a waiting `FOR KEY SHARE` lock on the dataset row, compatible
with ordinary metadata updates, under the caller's existing timeouts. It adds no
advisory gate: existing writers retain their lifecycle locking and deferral behavior.
INSERT and DELETE guards are statement triggers over transition tables (bounded
statements). UPDATE guards are row triggers with no transition tables, so a
whole-dataset metadata update copies no rows to temporary files. A transaction
remembers the dataset it has key-share locked (`fridge.writable_dataset`, transaction-local
and released with the lock); a dataset-keyed row runs the check only when its dataset
differs, so a bulk update checks once. Sample-, concept- and version-keyed rows need a
parent lookup: they are checked one by one only while some dataset is deleting, under
repeatable read, or when the row moves to another parent. An in-place update that began
before a concurrent seal committed only changes rows the deletion removes; work it
queues still passes the locking INSERT/DELETE guards.
Deletion admission's `FOR UPDATE` lock orders sealing against these writers.
It must precede the flag update: it makes PostgreSQL mark that update as a key update,
which forces old repeatable-read writers to fail at `FOR KEY SHARE`. A plain flag
update does not provide that protection. Tests cover inserts, tags and vectors with
an old snapshot, including an intervening ordinary dataset metadata update.
Admission cancels existing lane owners; claim cleanup terminalizes unstarted
and expired work for that dataset. Maintenance schedulers, including duplicate cleanup
and bootstrap admission, skip deleting datasets.
Only the fenced deletion callback sets a transaction-local deletion context; its deletes
skip statistics, concept delivery/count, duplicate, and coordinate/rank/projection trigger work because
those children are explicitly drained. Ephemeral region selections and quality pairs
have no dataset foreign key and retain their existing bounded TTL cleanup. Expiring these
temporary query results, creating them and updating them are exempt from the write guard.
Their consumers still enforce dataset access and write fencing; materializing Select All
does not scan a transition table just to recheck writability.

Deleting a user associated with a deleting dataset returns an actionable 409: finish or
resume the dataset deletion first, or deactivate the user meanwhile. The user deletion
transaction rolls back its view/access changes. `populate_field_stats` skips deleting
datasets in all-dataset mode, including datasets sealed after enumeration, and continues
refreshing the rest. An explicit deleting dataset ID reports the lifecycle error.

Tests cover process death, fencing, cancel/resume, write rejection, concurrent unrelated ingest, and
retained job status. The opt-in scale test uses 500,000 samples, 512-/1024-dimensional
vector fields, one million concept scores, coordinates/projection queues and duplicate
configs/memberships (`FRIDGE_TEST_DELETION_SCALE=1`).

Status and history require current dataset access. After deletion, only the submitting
user or an administrator may read the retained job. Global jobs are administrator-only.
Cancellation additionally requires an administrator or the submitting power user.
Status responses omit internal arguments, checkpoints, and lease credentials. Dataset job
history uses a bounded, chronological keyset cursor rather than offset pagination.
Raw errors and results are included only for administrators and the job's submitter, even
when another viewer has access to the dataset. Progress messages must be safe for dataset
viewers; private diagnostics belong in the error or result, not in a progress message.

The SDK exposes `client.get_job(id)` and `Job.status()`, `Job.cancel()`, and `Job.wait()`.
Waiting returns the exact job result, raises the actual failure or cancellation, and has
a finite timeout. Timing out locally does not cancel server work. Existing blocking APIs
will keep their signatures and return shapes when their operations are converted.

### Dataset indicator

The dataset and saved-version pages poll `GET /api/datasets/{id}/jobs/active` every five seconds while
visible. Its quiet, expandable indicator shows running work, submitters, progress,
and waiting work, including cancellation requests and retry delays. Maintenance is
not a warning. A failed status read says status is unavailable rather than pretending
there is no work; it does not block dataset use.

Expired leases read as stalled, not running, even if no worker is alive to reap them.
The database supplies stalled and retry-delayed flags, so browser clock skew cannot
change those labels. The response contains only public progress and display state,
never raw errors, results, resource requirements, or internal user IDs.

This endpoint checks current dataset access, returns all lane owners and a bounded
keyset page of unstarted waiting work, and never exposes arguments, checkpoints or lease credentials.
`running` contains owners with a worker lease; `resuming` contains started owners that yielded
or are waiting to retry. Both are returned on every page, independently of the waiting cursor.
An owner waiting to resume never labels itself as waiting behind its own operation. Other
jobs in its lane still identify that owner as the blocker, including during retry backoff.
A queued-dataset partial index keeps polling from scanning terminal job
history. Next/previous controls page through waiting jobs; only the current page polls,
so visiting more pages does not multiply polling requests. The unique-owner
partial index bounds the owner lookup to two rows per dataset, without scanning old history.
An emptied waiting page returns to page one. Newer terminal jobs cannot hide an
older unfinished job. Waiting order follows availability time, not a promise of a strict
FIFO position: placement, lanes, retries and available worker kinds also affect claims.

This is read-only visibility, not a new execution gate. Ordinary edits continue; a
whole-dataset job in the same lane waits behind the unfinished owner, including remote compute.
Maintenance and user lanes remain independent. Synchronous operation endpoints
appear here only once they run on the job runner.

### Cancel and history

The jobs UI offers:
- **Cancel.** The indicator offers a cancel control only where the cancel endpoint would accept
  it: administrators, and the power user who submitted the job. The active-jobs response carries
  `can_cancel`, computed by the same rule the endpoint enforces. Confirmation is built into the
  page. A running job stops after its current step and shows "Cancellation requested" until then;
  a 409 means the job row was briefly busy, and the person is asked to retry.
- **Dataset history.** A Job history button beside dataset settings, and a link in the expanded
  indicator, open the existing `GET /api/datasets/{id}/jobs`, newest first, page by page. Each job
  shows what it was, who started it (`started_by`: a username, System, or Deleted user), when, how
  long it took, how it ended, and its error only where the API returns one (admins and the
  submitter). It is fetched and polled only while open.
- **Background jobs.** `GET /api/admin/jobs` (administrators only) lists jobs that belong to no
  dataset, such as scheduled cleanup, on the System status page.

## Incremental exact-duplicate upkeep

`sample_dup_secondary` holds each config's hidden secondaries. Named-field configs
use exact hash text, `_all` uses the existing sorted concatenation of present fields, and
`_any` is the union of per-field secondaries, not connected components. The representative
is the minimum sample ID per key. This does not maintain embedding near-duplicate groups.

`duplicate_configs` explicitly records enabled configs, including empty hidden sets.
Migration seeding uses successive seeks in the existing `(dataset_id,hash_config,sample_id)`
index, not a DISTINCT scan of hidden rows. Configs previously built with zero hidden rows
cannot be inferred; register those explicitly with refresh or the backfill CLI.
`duplicate_members` retains keys, including representatives, after source deletion. It has
no sample FK: deleting that evidence would prevent promotion of the next member. `_any`
has one membership per field/hash, encoded as a JSON array to preserve field boundaries.
Sample pages select distinct IDs before limiting membership rows.

Statement transition-table triggers record hash insert/change/delete and sample deletion;
a column-limited row trigger records actual dataset moves without materializing transition
tables for metadata/tag updates. Hash recorders skip empty statements, cache singleton plans,
and replan bulk joins via EXECUTE. `duplicate_pending` holds one row per dataset, 1024-ID block,
and writer transaction. Unchanged hashes and unrelated sample metadata do not queue work.
Recording covers unconfigured datasets too, so a writer whose snapshot predates registration
cannot fall through a gap. The scheduler cheaply drains unconfigured work. Autocommitted
per-hash uploads still have one writer transaction per hash; their atomicity is unchanged.
A truncate records a rebuild marker, handled after the current duplicate job finishes.
The scheduler durably marks every config `restart_required`; the next bootstrap clears that
flag atomically with submission and starts from zero with a fresh upper bound, ignoring any
pre-truncate cancelled/failed checkpoint. Ordinary interrupted bootstraps still resume.

`duplicate_maintenance` always owns the maintenance lane, including explicit requests. Its
finite transaction-snapshot barrier excludes late commits even if their IDs/XIDs are smaller.
A dirty block is processed across its captured config list in pages of at most 256 sample IDs.
Publication changes only actual membership/hidden-row differences. Indexed old/new minimum
probes limit secondary recomputation to dirty-page IDs plus old and new representatives; no
whole group is rewritten. Even unchanged cached keys have their hidden flag reconciled, since
deleting and re-creating a sample ID can cascade away its hidden row. Only the selected pending
transaction keys are acknowledged, after
all configs/pages publish. Writes after measurement remain pending for a subsequent job.
Measurement uses short repeatable-read snapshots, halves timed-out pages, and has a 60-second
unit policy; the shared fenced publication callback has its existing five-second budget.
Lease loss/cancellation rolls back unpublished effects. Publication holds a short dataset key
lock against deletion; if the dataset disappeared after measurement, it cancels the job in the
fenced commit instead of leaving a failed job. No foreground writer takes job locks.

An explicit config request registers it and runs a bounded full bootstrap to a fixed upper
sample ID. The first phase builds/repairs memberships while leaving existing hidden rows
untouched. The second phase reconciles the existing hidden set in bounded batches, inserting
missing secondaries and removing obsolete ones without rewriting unchanged rows. This phase
is progressively visible; there is no generation or atomic swap. A final separately budgeted
count preserves the endpoint's `{config, hidden}` result. Bootstrap checkpoints resume after
cancellation/failure; expired leases resume the same job. Uninitialized configs are completed
before routine work drains. Queued changes during bootstrap are retained for catch-up.

The TEXT btree keys have a conservative 2000-byte combined config/key limit, checked before
publication and in the table constraint. A sample with any oversized key is skipped for that
config and logged with its dataset/config/sample ID; other samples, blocks, and configs continue.
The skipped sample stays visible for that config. Shortening its hash and uploading again
restores normal membership; no hash or equality truncation is used.
The 500k-sample two-field fixture measured a 69-byte combined
hash (73 bytes including `_all`). Operators should check their actual hash-field widths before
the first build. No dictionary or fingerprint index
is introduced. Derived membership storage and first-build work are proportional to samples
and registered configs; ordinary upkeep only visits dirty blocks and affected indexed keys.

The API refresh and `python scripts/backfill_dup_secondary.py` submit jobs to the existing
runner. The API follows the five-second rule (below): `{config, hidden}` when the job finished,
otherwise 202 `{job_id,state}`; the wait never cancels the job. The CLI prints job IDs rather than running
a second maintenance engine. Automatic
successes use the shared statistics/concept history filter and 24-hour/1000-row retention;
`include_system_statistics=true` also exposes these jobs. Full bootstraps and explicit requests
remain visible and unpruned. `refresh_dup_secondary` remains solely as the test oracle.

Duplicate configs are initialized by bounded runner jobs, never inside DDL. The Docker `worker`
only runs `prune_cache.py` (disk-cache pruning).

Acceptance tests cover randomized oracle equivalence, representative promotion, `_any` paging,
no-op writes, late/out-of-order/same-block writers, registration races, cascade deletion/moves,
bootstrap cancellation and lease loss, history retention, migration parity, and rollback.

## Incremental concept upkeep

Routine upkeep processes changed inputs, not the whole dataset. Embedding changes record one
pending row per dataset, field, 1024-sample-ID block and writer transaction. Insert/delete statement
triggers skip empty transition tables, cache singleton plans, and use dynamic SQL for larger
transitions so plans reflect the actual batch size. Updates use
a row trigger limited to vector, sample ID and field; layout-coordinate writes do not invoke it.
Recording covers every field, even before a concept exists: a repeatable-read writer whose snapshot
predates a concept must still be caught after the initial scan. Writers coalesce only within their
own transaction, without updating a shared concept row.

A one-second delivery tick transfers committed source blocks to enabled automatic concepts and
removes the source rows atomically. It cycles through datasets, claiming up to 4096 source blocks
per batch, at most 16 batches, and starts no new batch after half a second. Within a dataset it
prefers older transaction IDs (approximately FIFO; commit order can differ), using a delivery
index and SKIP LOCKED. Fields without enabled concepts are drained too. Disabled concepts receive
no pending blocks; disabling clears their queue, and re-enabling requires one complete rebuild.
A stopped runner leaves source work durably queued.

`backend.concept_jobs` schedules enabled concepts with pending work or an unfinished initial build
every `JOB_CONCEPT_SECONDS` (default 60 seconds). Each concept has at most one uncancelled automatic
job. Its finite transaction snapshot and sample extent exclude work arriving after submission;
new arrivals remain pending for the next pass. A unit re-reads a touched block, in batches of up to
1024 samples. Publication checks the lease and definition revision, takes try-only lifecycle and
NOWAIT row locks, upserts changed scores, removes scores for missing vectors, and acknowledges only
the measured block's work visible to both the job barrier and measurement. Bootstrap scans may
acknowledge the whole scanned range. Unchanged scores keep their tuple versions.
Definition changes, manual rescores, disabling, and deletion fence older publications.

Sample reads have a five-second budget. Timeouts shrink the batch, with at most about 25 seconds
spent retrying within one unit; a database stall defers without spending the failure budget, even
at one sample. Four consecutive successful units double the batch back toward 1024; a timeout
resets that streak. The final bootstrap count is a separate checkpointed unit with a 45-second
database budget; its timeout never shrinks sample batches. Three failed count attempts fail the
job visibly and release the lane. Failed or cancelled initial builds keep their published cursor,
barrier and count phase; the next job resumes them
when the definition revision is unchanged. Permanent invalid-input failures remain suppressed
until that revision changes, rather than creating another failed job every five minutes. Delivery
also skips these invalid concepts until their definition changes.

Statement triggers on the scores table record insert/delete count deltas, including sample-delete
cascades. Publication settles at most 1000 deltas at a time without recounting the dataset.
Pending blocks and count deltas have no concept FK, avoiding a concept-row lock during sample
cascades. A concept-delete trigger removes them; bounded scheduler cleanup catches orphan records
from transactions that overlap deletion or disabling. The initial count discards only deltas
visible to its snapshot, so a concurrent deletion is not counted twice.

Enabled concepts get a bounded initial pass that repairs missing scores without rewriting unchanged
scores. **Initial builds hold the dataset's maintenance lane across yields**, so statistics for that
dataset wait until the build finishes. Other datasets can use the other maintenance slot. Allowing
started jobs to release their lane requires a separate change to the shared queue's ordering
contract. Routine successful system concept jobs use
the same default-history filter, 24-hour retention cleanup and partial indexes as statistics jobs.
The existing `include_system_statistics` history option includes both kinds.

Explicit create/rebuild/refine/rescore are user-lane jobs (see "Long-running endpoints"
below). The SQL `score_concept()` remains as the test oracle and for direct SQL callers: it
neither dispatches the source backlog to other concepts nor locks its rows; its incremental mode
reads the concept's pending blocks plus the committed source blocks in one snapshot. Manual
concepts' "score N new" keeps the embedding-ID watermark contract: it is proportional to newly
appended embeddings and intentionally does not repair in-place changes or late commits below that
watermark. Use a full rescore or enable automatic scoring for that repair.

## Incremental statistics

`backend.statistics` owns submission, scheduling and fenced publication; `statistics_measure`
owns read-only measurements. There is one statistics engine. The old SQL
`refresh_stale_datasets_stats()` fails with an explicit migration message instead of running a
competing unbounded scan. `scripts/populate_field_stats.py` submits and waits on the same jobs.

- A row trigger records disposable work keyed by dataset, 4096-ID block and the writer's
  **transaction ID**. Conflicts coalesce only within that transaction; concurrent writers never
  update one shared dirty row. There are no transition tables containing whole sample rows and
  no permanent per-sample audit history. Processed work is deleted.
- First-time metadata registration can use the incremental path only when its own transaction
  already recorded sample work naming that field and no previous block summaries exist for it.
  After separately committed backfill batches, registration in a later transaction that does not
  modify samples requests a full measurement, even if the worker already consumed those batches.
  Scripts mixing registration with batch writes or deletes, including crash retries, must explicitly
  request a full refresh after all batches; the shortcut does not prove coverage of earlier writes.
  Definition changes and adopting an existing native column still request a rebuild.
- A refresh captures PostgreSQL's committed-transaction snapshot, a finite sample extent and
  `stats_full_requests`. IDs bound scans; they are **not** committed-work checkpoints. A lower-ID
  transaction that commits late stays pending. A queued refresh can absorb a later snapshot;
  an already-started refresh never chases new arrivals indefinitely.
- Each block is measured in one read-only repeatable-read snapshot. Queries have a 10 s server
  budget and reads an 11 s client budget, with connection termination before timeout cleanup.
  An oversized block is subdivided down to one sample. Unmeasurable single samples fail visibly,
  rather than being retried forever. Other datasets retain access to execution slots.
- Fenced publication replaces a leaf summary and advances its checkpoint atomically. A split
  block acknowledges only transactions visible to its **first** leaf snapshot, after all leaves
  finish. Later commits remain pending. A cancelled or failed incremental pass retains its completed
  blocks and durable unfinished work; it does not force a dataset-wide rebuild. Retries resume the
  checkpoint. Cancelling an unfinished full rebuild leaves its full request pending for another pass.
- Aggregate units read at most 128 cached parts for one field, with adaptive page-size reduction.
  The job captures a finite field list. Aggregate checkpoints survive retries without double
  counting. Final per-field publication uses a shared try-only lifecycle gate and NOWAIT cache
  locks, never an interactive exclusive gate or a registry write.
- Publication validates registry row ID, declared type and origin. Index/display changes are not
  semantic identity changes. Removed definitions cannot be resurrected; deleted caches are not
  overwritten. Previews with unmeasured writes merge conservatively. Pending work may retain a complete
  value list only when the locked cache row still certifies that ingest merged every arriving value;
  edits/removals invalidate that certification. Counts can remain pending while the list is complete.
  Ordinary string/number previews cap at 100; tags, labels and native arrays
  at 200. Structural overlay field-name indexes are not truncated.
- Bounds/counts describe observed block states, not one global historical dataset snapshot.
  Each stable sample ID belongs to exactly one block. A finite refresh covers the captured work
  but may also observe newer commits; later arrivals can still be pending when it succeeds.
  Use saved dataset versions, not a statistics barrier, for reproducible training inputs.
- Registry changes and full-refresh requests schedule measurement automatically, including type
  corrections. A full timestamp advances only if its request counter is unchanged and no work
  remains. Routine pending work uses the quiet dataset job indicator, not a permanent warning.
- Lock contention backs off exponentially without consuming the infrastructure-failure budget.
  Successful checkpoint progress resets its contention counter. A failed dataset gets a five-minute
  automatic scheduling cooldown, independent of other datasets; explicit requests can retry sooner.

`dataset.refresh_stats()` retains blocking success/result behavior by submitting once and polling
a durable job. `wait=False` returns a `Job`; `force=True` requests a full rebuild. Timeout does not
cancel the job and exposes its ID. The HTTP endpoint also supports `wait=false`, returning 202 with
`job_id`; a blocking HTTP wait that expires returns that handle without claiming a completed refresh.
Cancelling a maintenance job stops that job, not future automatically scheduled upkeep.

The admin bulk endpoint, `POST /api/datasets/refresh-stats`, skips datasets with a queued or running
statistics job. Its response includes only jobs it submits (or their results with `wait=true`), not
existing jobs or a list of skipped datasets. `wait=true` does not wait for those existing jobs, so an
empty jobs/results list can mean statistics work is already active, not that every dataset is current.
Use the per-dataset endpoint with `wait=false`, or `dataset.refresh_stats(wait=False)`, when a specific
dataset's job handle is needed.

### Overlay discovery caches

Filter compilation uses only the cached overlay field lists and the legacy label parents
(`detections`, `polygons`, `ground_truth`, `predictions`). This includes the grid, counts,
autocomplete, histograms, aggregation, search, map and bulk jobs. A missing cache never makes
those requests discover fields. Until publication, custom label paths are unavailable.
Classification lists retain their `.label` facets and filters while rendering classification badges;
single classification objects do not get list vocabularies.

Only `/field-stats` discovers missing shapes interactively. Each of the six caches has a
transaction-scoped PostgreSQL advisory lock keyed by dataset and cache name, with a second cache
check under the lock. Concurrent API workers share the result. Discovery uses a local 10-second
statement timeout (the same server budget as statistics measurements; the older sidebar discovery
scans had no server timeout). A timeout returns no discovered fields, releases the lock and leaves
publication to statistics or a later sidebar request. Empty published lists are cache hits.
Statistics publishes all six discovery caches, including empty ones, before dependent `.label`
rows, in deterministic C-collation order.

**Overlay deployment:** no migration is required. Pause every ingest source (sync jobs and
importers) before restarting the backend and resume it only after the script below finishes: an
incremental statistics run on blocks measured by older code would otherwise publish partial
discovery caches until the forced refresh repairs them. With the updated runner running, before
reopening dataset traffic, use the existing script with an explicit database URL:

```sh
DATABASE_URL='postgresql://USER:PASSWORD@HOST/DATABASE' python -m scripts.populate_field_stats
```

Without a dataset argument the script submits a **forced full refresh for every non-deleting
dataset** and waits for the jobs. This is the chosen deploy step: old block summaries must be
remeasured to contain the new discoveries; a routine incremental pass on a quiet existing dataset
is not a substitute. Fresh datasets publish the caches during their first full statistics run.
The jobs remain bounded and resumable, using the normal maintenance lane.

A forced full refresh measures every block. Its duration depends on row count, metadata size,
disk throughput and concurrent writes. Measure it on a representative isolated copy before
scheduling a deployment window. The script waits for completion rather than imposing a
benchmark-derived deadline.

Discovery writes remain append-only. Statistics owns removals. Until the next relevant statistics
run, these accepted limitations remain:

- An ingest racing a first scan while its cache is absent can miss that scan's snapshot and cannot
  append to an absent row; the new overlay is temporarily undiscovered.
- Reusing a removed field name with a different shape can retain its previous overlay type.
- A restored field absent from discovery can appear as an ordinary list until the requested full
  refresh finishes.
- Deleting the final sample can leave an orphan `.label` row until statistics cleans it up; registry
  filtering hides removed fields in the meantime.

Writes schedule statistics automatically. Full restore refreshes can take minutes on large datasets.
These limitations deliberately avoid blanket invalidation and repeated whole-dataset scans during ingest.

First-seen metadata fields use the sample writes' existing pending blocks, not a full rebuild. A
re-created definition with old block summaries, a type correction, or adoption of a native column
still requests a full measurement so historical data cannot be silently omitted.

Bootstrap is resumable background work; no operator-maintained second worker,
remote compute machine, Redis, or manual refresh loop is needed for a local deployment.

## Local startup and scheduling

`scripts/run_backend.sh` starts `backend.launch`, which supervises one uvicorn master and one
`python -m backend.job_service` process with the same environment and database. API worker count
does not multiply runners. SIGINT/SIGTERM/SIGHUP stops both process groups, including API children.
An unexpected API exit stops the runner and exits nonzero. A runner exit does not stop the API:
the launcher cleans up that runner's process group and restarts it with exponential backoff from
one to thirty seconds, reset after thirty seconds of stable execution. The error is logged and
worker health remains visible. A missing runner migration or a transient connection failure must
not take browsing and ingest offline; after the normal migration process catches up, the runner
can recover without restarting the API. Pending runner migrations make the same runner process wait
without claiming work; it rechecks the ledger every five seconds. The launcher never applies migrations.

Measurements get a bounded graceful shutdown; committed checkpoints survive forced worker
termination and are resumed after lease expiry. A second SIGINT skips the remaining grace
period. Forced application termination returns a nonzero launcher status, not a successful shutdown.

Only a repeated SIGINT forces an early stop. Duplicate SIGHUP/SIGTERM delivery retains the grace
period, and an inherited ignored SIGHUP (such as `nohup`) remains ignored. API `--reload` never
reloads the runner; restart the launcher after changing runner or handler code.

Failed claim, scheduling and schema-readiness polls log one traceback per outage, then only
failure-type changes and recovery. Retries back off exponentially to thirty seconds (or the configured
poll interval if longer), resetting after success. A pending migration warning appears only when
entering that state, not every readiness poll. Shutdown interrupts these waits immediately.
Publication callback errors, timeouts and cancellation terminate their connection before callback
cleanup or transaction rollback, so a silently dropped socket cannot strand a worker slot.
Disposed-connection cleanup preserves the original failure or cancellation rather than replacing it
with a retryable asyncpg connection error.

Each managed process group also gets a tiny automatic parent-liveness guard. A standard-library-only
bootstrap forks it before execing the API/runner, preserving the managed PID. The guard blocks on one
pipe whose only writer is the launcher, with all other descriptors closed; it does not poll, connect to
PostgreSQL or claim jobs. If the launcher is hard-killed, EOF kills the whole managed group, including
descendants and processes ignoring SIGTERM. This avoids relying on a parent-death signal delivered only
to the API master. Normal shutdown remains graceful; the launcher then removes the guards with their
groups. There is no extra service, command or configuration for a local installation. This launcher uses
POSIX process groups and fork, like the existing Linux deployment.

- Local default: run the existing script. `FRIDGE_BACKEND_PORT` and `FRIDGE_BACKEND_HOST` allow
  isolated development stacks. No remote worker or Redis is required.
- Docker production and the demo use the `job-runner` service in `docker-compose.prod.yml`.
  It runs the same `backend.job_service`, independently of API scaling. Both services mount the same
  read-only backend tree. Restart both after code updates; rebuild their images when dependencies change.
- Development compose normally supplies the database while the local launcher owns the runner.
  For a container-owned runner, use `docker compose --profile jobs up -d` and
  `FRIDGE_RUNNER_ENABLED=false ./scripts/run_backend.sh`; do not deliberately start both.
  That runner mounts this checkout's backend code too; restart it after code changes and rebuild when
  dependencies change, just as for the production pair.
  `false`, `0`, `no` and `off` disable the local runner, case-insensitively and ignoring surrounding spaces.
- `JOB_WORKER_SLOTS` defaults to 3, reserving one slot for user work. The runner's handler pool
  defaults to twice the slot count (`JOB_POOL_MAX_SIZE`); each active slot also keeps one lease
  connection. The default budget is at most 9 connections, separate from API pools.
- `JOB_SHUTDOWN_SECONDS` defaults to 20; the local supervisor's
  `FRIDGE_SHUTDOWN_SECONDS` defaults to 30. Compose uses a 30-second stop grace.

Transient-selection cleanup runs every 300 seconds by default
(`JOB_SELECTION_CLEANUP_SECONDS`). It deletes selections older than the job's fixed two-hour
cutoff in indexed, fenced batches of at most 1,000 rows. Recent selections and dataset contents
are untouched. A database scheduling gate and the latest job's durable state prevent overlapping
submissions across runners or restarts. Statistics scheduling runs every five seconds by default
(`JOB_STATISTICS_SECONDS`), with independent failure backoff. Concept scheduling runs every 60 seconds
(`JOB_CONCEPT_SECONDS`); committed embedding work is delivered every second. Dataset schedules
(map re-fits, below) are checked every 60 seconds.
Successful system-owned **incremental** statistics jobs are excluded from dataset history by default;
active, failed, cancelled, full-rebuild and user-requested jobs remain visible. The API and SDK `list_jobs` accept
`include_system_statistics=true` for diagnostics, and retained jobs remain accessible by ID.
The existing cleanup job also removes successful system incremental statistics older than 24 hours in
indexed, fenced batches of at most 1,000 rows. It uses the durable system-owner identity, not
`created_by IS NULL`, so deleting a user never makes that user's jobs eligible. Failed jobs
and explicit refresh requests are not pruned. The admin refresh endpoint records the requesting
administrator. Full rebuilds (`barrier.full=true`), including CLI and quality-triggered requests
without a user identity, remain visible and unpruned; initial automatic full measurements are also
retained. Maintenance cadence and the active-job indicator
are unchanged; job history reads use a partial index that skips routine successful refreshes.
Selection reads enforce the two-hour lifetime independently of pruning progress, so a half-pruned
expired token is never a smaller live selection. Bulk tagging validates expiry, captures ordered target
rows, and applies additions/removals in one statement snapshot. Tag counts validate and read a token in
one read-only repeatable-read snapshot; concurrent cleanup cannot make its counts disagree with its total.
These transient tokens are not yet durable job inputs; operation conversion must capture job-owned
selection membership rather than enqueue only an expiring token.

System Status labels online execution **slots**, not runner processes. No recent server heartbeat
is OFFLINE, an unreadable health check is UNKNOWN, and normal queued work creates no worker banner.
The hourly Docker `worker` remains solely for disk-cache pruning. Duplicate collapse is
scheduled by the co-located runner every five seconds. Moving cache pruning onto the runner
is a separate conversion. Merely starting an empty consumer does not satisfy this requirement. Missing
heartbeats must be visible in System status and the UI; normal pending work must not create
a permanent warning banner.

A plain `ServerWorker` claims only `server` jobs. The co-located runner (`local_runner`, used by
`job_service`) defaults to `placements=('server','compute')`, so it also runs compute jobs such as map
re-fits. `JOB_RUNNER_PLACEMENTS=server` leaves compute work to compute workers.

**Remote compute workers over HTTP** (`backend/routers/workers.py`). A machine runs
`python -m fridge.worker` with a worker token (`api_tokens.scope = 'worker'`, which only these endpoints
accept). It registers (`POST /api/workers/register`) as a `placement='compute'` row in `workers`, with
its owner, and heartbeats (`POST /api/workers/{id}/heartbeat`). The job endpoints wrap the queue
functions above, so their semantics are the same as a local slot's:
- `POST /api/workers/{id}/claim {wait_seconds ≤ 25}` long-polls `claim_job` with `placements=('compute',)`,
  the kinds the worker registered that run remotely (`REMOTE_KINDS`, with each kind's `JobPolicy`), and
  `dataset_ids` = the datasets its owner can access (`filter_datasets_for_user`); a job on no dataset only
  for an admin's worker. 200 is the job with a 120 s lease; 204 is none.
- `POST .../jobs/{job_id}/heartbeat {lease_token, done?, total?, message?}`: `heartbeat_job`, then the
  progress through `publish_step`. It returns `cancel_requested`.
- `.../complete {lease_token, result}`: `publish_step(finished=True)`. `.../fail {lease_token, error,
  retryable}`: `defer_job` (the kind's retry delay) or `fail_job`. `.../release {lease_token}`:
  `publish_step(release=True)`, queued again or cancelled when a cancel came in.
- Each checks that the job's `worker_id` is the caller's worker. A lost lease is 409 (`lease_lost`: stop
  without reporting), and a busy queue is 503 with Retry-After. A worker that stops renewing loses the job
  when its lease expires: the next claim requeues it with a failure, as for a local slot.
- Heartbeat, complete and the file transfers also check, as a claim does, that the worker's owner may still
  access the job's dataset; when they can't (an admin removed their access), the answer is the same 409
  `lease_lost`, so the worker drops the job and goes on claiming, and its lease expires. Fail and release stay
  open, to give the job back.

`REMOTE_KINDS` lists what a worker can claim, with each kind's policy, which of its steps the worker runs,
the job files it may download (`GET .../jobs/{job_id}/files/{name}`, Range requests resume it) and upload
(`PUT .../files/{name}?offset=`, chunks of at most 64 MiB, append-only, a 409 gives the size to resume at),
and how `complete` hands the job back. Each transfer carries the lease in `X-Fridge-Lease` and is refused
unless that lease is live. Uploads are staged per lease (`staging_dir`: `work/<job>/remote-<lease>/`), so a
worker whose lease ended can never mix its files into a later attempt's.

**The map re-fit** is the first remote kind. Each step names where the next runs (`StepResult.placement`):
- The job is queued as compute work, so nothing starts (no projection pause, no export) until something that
  takes compute work claims it. A worker's claim of its start hands it straight to the server
  (`runs_remotely` is true only in the fit phase).
- The server runs `_start` and `_export`. A runner with `fits_here=False` (`JOB_RUNNER_PLACEMENTS=server`)
  doesn't measure its own memory, and says the fit waits for a compute worker. `_export` makes the work folder
  writable by the API, which runs as another user in Docker.
- The worker downloads `vectors.bin`, runs `fridge.map_fit`, uploads `REMOTE_OUTPUTS` (`ids.npy`,
  `versions.npy`, `coords.npy`, `manifest.json`) and completes with each one's sha256. No model crosses: a
  model is a pickle, which runs code when loaded. Each upload is capped by what the exported rows allow
  (`remote_output_limit`), checked before anything is written, and appended under a file lock.
- `remote_fit_checkpoint` checks every output against its sha256, reads the arrays with pickles off, checks
  their shapes and types against the exported rows (and the manifest's), and hands the job back to the server
  in the publish phase. A failed check is a 400, and the job stays the worker's.
- `_publish` requires the worker's ids and versions to be the export's, in order (other ids would drop the
  map's points, another order would misplace them), then swaps and ranks as for a local fit. The map is
  published with no model file (`artifact_location` NULL; migration 0004 allows that).
- A fit no worker claims within `UNCLAIMED_FIT_HOURS` (12) is cancelled by `expire_unclaimed_fits` (every
  10 minutes on the server runner), with the reason in its message. The old map stays, and placement of new
  samples resumes on it.

**Remote fencing (design, not built yet).** The lease fence covers only SQL inside the
publication transaction. A remote worker uploads results over HTTP, so a laptop whose lease
expired could wake up and keep uploading, mixing two non-reproducible fits.
- Every remote upload carries `(job_id, lease_token)` and is accepted only while that lease is live.
- Staged uploads are keyed by lease, not by job. The fenced "complete" call validates the lease and swaps in
  only that lease's uploads, so a woken stale laptop's files can never be mixed in.
- The lease check and the staged write happen in the same transaction.
- A checkpoint may reference only outputs that were themselves published through the fence (e.g. the
  fitted model), never a stale owner's unpublished files.
- Worker owner/token, SDK version, token scope and an access filter on claim are additive columns.
- **Scheduling policy: a running remote compute job keeps its dataset lane.**
  Whole-dataset operations in that lane wait, while ordinary edits continue. The dataset indicator
  shows the running job and waiting work. Running compute alongside those operations and acquiring
  the lane only for a fenced, generation-checked swap remains a likely later improvement, not current behavior.

## Dataset schedules and the map re-fit

Each dataset's Jobs panel has a **Schedules** tab (`/api/datasets/{id}/schedules`): recurring jobs
for that dataset. The first and only job type is **Map re-fit** (kind `map_refit`): refit one embedding
field's UMAP map from all of its vectors and swap the new map in at once. Viewers see schedules;
power users and admins add, edit, switch on/off, delete and **Run now** (which ignores the conditions,
and returns the re-fit already queued or running for that map instead of queueing a second). Power users
may also cancel a `map_refit` job, which the system owns; other jobs stay cancellable only by their
creator or an admin.

- **Rule.** Daily, or weekly on one day, at a wall-clock time in the schedule's time zone (default
  America/Chicago): a daily 03:00 stays at 03:00 across daylight-saving changes; a time the spring-forward
  skips runs an hour later that day; a repeated fall-back time runs at its first occurrence. A run must
  start within `window_minutes` of its time (default 180): a slot the runner was down for is recorded as
  missed in `last_check`, and a queued run that could not start by then finishes **skipped** before it
  pauses projection, so a fit and its swap never land in working hours by catching up.
- **Conditions** (each optional): at least `min_new_samples` more points on the map since the last
  re-fit (the exact `embedding_viz_meta.total_points` minus `points_at_refit`; never a scan),
  and at least `min_days_between` calendar days since `refitted_at`. Defaults: **weekly on
  Sunday at 03:00, only when >= 20,000 new samples, no minimum gap** (the gap is available in the form).
  The count is the net change of placed points (deleted samples subtract; samples still waiting for the projection do not
  count yet). The gap is not checked night by night: after each re-fit (scheduled or Run now), and
  whenever a due check finds the gap not over, `next_run_at` goes straight to the first time on or after
  the day the gap ends (in the schedule's zone), so "Next check" shows that date and no skip is recorded
  in between. The sample condition is checked at each time: not met, the skip is recorded and the next
  time is checked.
- **Scheduler** (`dataset_schedules.schedule_dataset_jobs`, every 60 s, advisory key 20261064): for
  every enabled schedule whose `next_run_at` has passed it checks the conditions, queues the job when
  they hold (with its window's end in the job's `args.window_end`), else records why in `last_check`,
  and moves `next_run_at` to the next time. All due runs are queued; the exclusive claim below runs
  them one at a time, so a re-fit that cannot start (its lane held by a stuck job) never holds up
  another dataset's. When a queued run must wait, `last_check` says for what: the database backup, or
  the re-fit of dataset N's field.
- **One fit at a time.** Re-fits carry `resources={"exclusive":"map_fit"}`; `claim_job` starts at most
  one unfinished dataset job per exclusive resource (try-lock 20261065 + recheck), including across
  the job's yields between steps, so a waiting fit never takes its dataset's lane early.

The job is `placement='compute'` in the dataset's **maintenance** lane (statistics, concepts and
duplicate upkeep of that dataset wait for it; other datasets do not). Four checkpointed units (the last one in two committed steps):

1. **start**: finish skipped when past `window_end`; wait (without spending its failure budget) while
   a backup is queued or running; estimate the fit's memory from the map's point count and the vector
   dimension and verify containment (below), finishing **skipped** with the reason if either fails;
   only then, in a fenced publication, mark the job as the field's active re-fit
   (`embedding_map_state.active_job_id`). From here projection of that field pauses.
2. **export**: one `COPY (sample_id, xmin, vector) TO STDOUT (FORMAT binary)` (one statement, one
   snapshot) into `FRIDGE_UMAP_MODEL_DIR/work/<job>/vectors.bin`, after a free-space check.
3. **fit**: `python -m fridge.map_fit` under `systemd-run --user --scope -p MemoryMax=<allowance>
   -p MemorySwapMax=0`. Containment is verified, not assumed: a probe scope must read back its
   `memory.max` (to the page) and `memory.swap.max = 0`, else the job is skipped ("not applied"). The
   child runs under `taskset -c <fit_cpus()>`: one CPU of each core (its first SMT sibling) on every core
   but at least 20% of them (rounded up, with at least one core left to the fit). Those cores stay entirely
   free for PostgreSQL and the app. The free cores are spread evenly over the core numbering. Where the
   numbering runs one L3 domain after another, each domain gets its share: for example, 16 cores x 2 gives
   12 CPUs, and cores 2, 6, 10 and 14 stay free. Its thread pools (numba, joblib, OpenBLAS) size themselves
   to those CPUs. Without `taskset` (util-linux without it) the fit runs on every CPU, as before, with a
   warning. Small hosts give up a larger share: 2 cores x 2 leave the fit 1 CPU of 4, 4 x 2 leave it 3 of
   8, and a fit there may be compute-bound and take longer.

   Reserving entire cores avoids sharing SMT siblings with latency-sensitive database work.
   A fit can also compete for memory bandwidth and evict database pages, so limiting CPU threads
   alone does not guarantee ingest latency. CPU controllers may be unavailable in user scopes;
   explicit CPU affinity provides the core reservation independently of those controllers.

   The memory allowance is MemAvailable minus 20% of MemTotal; below the smallest fit (the vectors plus
   50,000 fit points, or all of them if fewer) the job finishes **skipped** with the reason in its message and
   result, and the old map stays. Inside the scope `_auto_fit_sample_size` sizes the fit as 75% of what
   the cap leaves after the vectors divided by the fit's memory per point, with no fixed cap. That
   cost per point starts from `default_fit_bytes_per_point(dim)`, 13 float32 vectors of the dimension
   (including room for neighbor-search structures and a sampled fit's vector copy), and
   is then **measured**: the runner samples the anonymous memory of the fit's own scope (a
   `run-*.scope`; samples taken while the child is still in the caller's cgroup are ignored) every
   second, and the publish stores `(peak - memory before the fit) / fit points` and the fit size in
   `embedding_map_state`, which size the next fit of that map. Because the cost per point grows with
   the fit size, a fit that followed a memory-limited one (a sample of the map) grows at most 1/0.75
   beyond the size its cost was measured at (a relative step: it still reaches what the memory allows
   within a few nights); never below the 50,000-point floor, and not after a fit of every point. An out-of-memory kill ends only
   the child (it sets `oom_score_adj=1000`); it is told from other kills by the `oom_kill` counter of
   the scope's parent (`memory.events`, hierarchical): only then does the job retry at once in a new
   folder (`fit-<attempt>`) with at most 75% of the killed attempt's points and its cost per point
   divided by 0.75 (kept for the next night), and fail after the job's failure budget or at
   the 50,000-point floor; any other kill (kill -9, a stopped scope) is retried as it is. After the fit,
   the other rows are projected through the model in 50k batches with pynndescent's
   `parallel_batch_queries` on (using every CPU the fit may use); the manifest and the job result record
   `umap_fit_seconds` and `transform_seconds`. A backend restart's SIGTERM is retried;
   cancel/timeout/lease loss stop the child. The model is not kept: new samples are placed on a re-fitted
   map by their nearest mapped neighbours (**Placing new samples**, below).
4. **publish**: waits (unbudgeted) while a backup is queued or running, and the backup scheduler holds
   a backup back while a publish (or a rank) runs. One transaction. Stage the coordinates in a temp table
   **before** the gate. Then name the session `fridge map swap until <epoch>` (the deadline: the 30 s
   gate wait plus the swap's budget, on the database clock) and take the dataset's lifecycle gate
   (20261016) exclusively by polling for up to 30 s (otherwise defer; writers hold it shared only per
   transaction), and the rank fence 20261020. Keep exactly the exported rows whose embedding still has
   the exported row version; drop the field's rank rows; delete every coordinate of the field outside
   that set (the coordinate delete trigger re-queues those that still have vectors); upsert the set; set the
   map's `embedding_viz_meta.occupied_cells = 0` (every reader then draws it from its coordinates until
   it is re-ranked) and its point counter to the points just written (the coordinate trigger keeps
   counting points placed after it); record the new map's id (`model_id`, with no model file: the
   `artifact_*` columns are cleared, and a model file kept from before is deleted after the commit),
   `refitted_at`, `points_at_refit` and the measured cost per fit point, keeping the job as the map's
   active re-fit (placement stays paused);
   then `job_queue.checkpoint_locked` locks the job row, validates lease and cancellation and moves the
   job to its **rank** step in the same transaction (the long-operation contract above), with an
   `uncancellable` reason in its checkpoint. The fence re-reads the cancel flag under a share lock on the
   job's control row held to the commit, and `cancel_job` locks that row before it reads the job, so a
   cancel either lands first (and rolls the swap back) or waits for the commit (a 409 "retry" past
   100 ms) and is then refused. A lock
   timeout at the fence (the job's own lease renewal mid-commit on a slow disk) is contention
   (`QueueContended`: retried with backoff, no failure counted), as at the rank step's
   `finish_locked`. Any error, cancel or lost lease rolls everything back, and a swap
   still running at its deadline is ended and retried, so the gate is never held past the advertised
   time: the client closes the connection (the session sets `client_connection_check_interval = 1s`, so
   the server aborts even a running statement within a second) and ends the session with
   `pg_terminate_backend` from another connection (in case a proxy hides the close). The deadline guard
   is cancelled once only the COMMIT is left, so a swap that made it is never reported as rolled back.
   `POST /embeddings` takes the lifecycle gate shared, like sample ingest, so no vector changes between
   that check and the commit.
5. **rank** (right after the swap commits, in the same unit; a runner that stops in between resumes
   here). First, like the publish, it waits (unbudgeted) while a backup is queued or running (and the
   backup scheduler holds a backup back while a rank runs); then it checks the database volume by the
   API's full re-rank rule (`estimate_viz_rank_bytes` + 1 GB headroom: too little is a failure that
   wrote nothing). The session names itself `fridge map rank until <epoch>` (the 30 s gate wait plus
   the budget, then the budget once the gate is held), takes the gate **shared** (the same try-lock
   taker as the swap) and runs `recompute_viz_rank(…, TRUE)`, which holds the dataset's rank fence
   20261020. Meanwhile:

   | writer | during the rank |
   |---|---|
   | default ingest, new embeddings (inserts) | go on |
   | deletes, update-mode ingest, removed keys, field operations | 409 with the rank's end (`X-Map-Swap-Seconds`, about the budget), "ingest goes on" |
   | placement of new samples (`map_place`) | paused: the map is still active, so it places nothing |
   | a projected `POST /coordinates` | 409 at once (the map is active) |
   | other coordinate uploads, a vector change to a placed point | wait for the fence at most 5 s / 2 s, then a 409 with the rank's end (never a 500) |

   It ranks every coordinate present, sets the exact point counter and grid, ends the re-fit's hold on
   projection (`active_job_id`), and finishes the job, in one transaction. **While it runs it cannot be
   cancelled**: the new map is already in use, so the Jobs panel hides Cancel and
   `POST /jobs/{id}/cancel` answers 409 with the checkpoint's reason (it cannot race the swap's commit,
   above). Queued between attempts (after a failure or a backup wait) it can be cancelled; the map then
   stays unranked until the heal. If it keeps failing (its failure budget), the job fails with the map
   unranked (slower scatter, correct counter).

   The runner's **unranked map heal** (every 10 minutes) queues a rank-only `map_refit` job (no
   `map_fit` resource) for any re-fitted map with `occupied_cells = 0` (and points, or stale ranks)
   and no map job queued or running: at once the first time; after a heal job failed or was cancelled,
   1 h after the last one, doubling up to a day (so a rank that cannot succeed, say for want of disk,
   is not retried every few minutes, and cancelling a queued heal job stops it for a while). A heal
   job's rank pauses projection like a re-fit's (it marks itself the map's active job before ranking).
   "Run now" and schedule ticks do not count a heal job as the map's re-fit: they queue a re-fit, which
   runs after the heal in the dataset's maintenance lane. Ranking outside the exclusive gate keeps
   that whole-map operation from pausing ingest for its duration.

**Writes during the swap.** A write that cannot get the gate is answered 409 with `Retry-After: 5` and
`X-Map-Swap-Seconds: <seconds until the deadline>` (the detail says swaps usually take under a minute
per million points, and at most that long), found from the holder's `application_name` in
`pg_locks`/`pg_stat_activity` (nothing stored, so nothing goes stale; the stats snapshot is cleared
first). That includes writers that otherwise wait for the gate without a bound (default ingest,
`POST /embeddings`, tags, field operations): they look for a re-fit, and behind anything else they
queue: once for at most a second (`QUEUE_LOOK_SECONDS`), then, after a second look, for as long as
it takes, keeping their place, so a field operation is never starved by overlapping ingest, and a
writer held up between its first look and its queueing (a busy event loop) learns of a re-fit that
got the gate meanwhile within a second. A
re-fit cannot get ahead of a queued writer: it takes the gate only with try-locks (which fail against
a queued conflicting request), and a writer that queued in the instant after it got the gate (having
looked a moment before) makes it let go and try again (`GATE_GRACE_SECONDS`), so no writer sits out
the swap. The SDK keeps retrying such a 409 until that
end plus its usual 120 s window (taken from the first answer, so it stays bounded); other 409s keep the
120 s window. The swap grows with the map (about 25 s per million points for the coordinates); a
generation swap (writing the new map beside the old one and switching a pointer) would make it
constant.

Samples whose vector changed, that were deleted, or added during the fit never keep a coordinate from
the old map or the stale fit: they stay queued and are placed on the new map after the swap. Re-sending an identical vector (sample ingest in update mode, `POST /embeddings`) does not
rewrite the row (`WHERE vector IS DISTINCT FROM EXCLUDED.vector`), so the point keeps its place on the
new map. Readers see the old map or the new one:
each scatter query is one snapshot, and the response's `map_state` (`{model_id, refitted_at}`, JSON
and binary header) is read before and after the points, which are read again if a swap landed between.
The map panel shows "Map re-fitted on <date>".

**Lasso selections** (`embedding_polygon`) are coordinates on one map. The panel sends the map's
`model_id` with the polygon; on a field that has been re-fitted, a polygon drawn on another map (or
sent without a model id) is refused with 409 (`require_current_map`, SQLSTATE 55000) instead of
silently selecting other samples. The check is one InitPlan per query: every dataset-scoped caller of
`parse_filter_node` passes `dataset_id` (a test walks the call sites), and the function is
`PARALLEL SAFE`, so lasso queries keep their parallel plans (a lasso over 88% of 1M samples: 0.88 s, unchanged by the check). The
sidebar's best-effort endpoints (field-stats, schema, autocomplete) check the request's lassos up front
(`require_current_lassos`), so they refuse too rather than fall back to unfiltered values, and the
samples paths (page-only, keyset, slim, GET) let the refusal through their generic 500 handlers. The 409
carries `code: map_refitted` with the dataset, field and current model; the UI drops every stale lasso
of that field (an applied "Embeddings Region" stage, one restored from a saved view or history, the
draft), says so once, and the grid, sidebar and scatter load the new map, where the selection is drawn
again. The panel also clears its own draft lasso when the map changes under it. Fields never re-fitted
accept polygons as before.

**Placing new samples** on a map (`backend/map_placement.py`, kind `map_place`): every whole-image map, re-fitted
or made by the SDK (`compute_visualization`), meaning every `embedding_viz_meta` row with points. Patch maps
aren't placed. No map keeps a model; each new sample goes where the images most like it already are:
- The field's vector index finds the 15 mapped samples of the dataset with the closest vectors (cosine), one
  index search per sample (a `LATERAL` join; ordering a plain join by the distance would sort every vector of
  the field). The index is partial (`WHERE field_name = ...`), so the lookup runs on a plan made for the
  field's name (`plan_cache_mode = force_custom_plan`), with similarity search's iterative scan. A sample the
  index comes up short for (a small dataset in a field shared by large ones) is looked up exactly, among its
  dataset's mapped samples.
- On the map the neighbours usually sit together but can be split between groups. The sample goes to the
  densest group: the neighbour whose similarity-weighted map distances to the others are smallest, then the
  similarity-weighted mean of the neighbours within twice their median distance from it (`place`).
- Measured on 2,000 mapped images each, treated as new, against where UMAP's fit put them: on Open Images
  (42k) and on both maps of a 3.7M-image dataset (CLIP 512-d, DINOv3 1024-d), 9 in 10 land within 1.0–1.3%
  of the map's width, the median within 0.2–0.3%, and 99 in 100 within 12–24% (images whose neighbours sit in
  separate groups). The plain mean of the neighbours is two to four times worse at the 90th percentile.
  Against a UMAP fit with images held out (on Open Images and iNaturalist), it keeps more of each held-out
  image's embedding neighbours near it on the map than UMAP's `transform` does, a measure that favours
  placing by neighbours.
- `schedule_placements` (every 5 s, in the job runner) queues one job per map with samples waiting
  in `embedding_projection_pending`, in the maintenance lane, with the newest waiting sample as its barrier:
  the job ends even while ingest continues, and the dataset's other maintenance gets its turn. It skips a map
  whose re-fit is active, one whose placement is queued or running, and, for 5 minutes, one whose last
  placement failed or found no neighbours for anything. A field with no map yet gets nothing placed: its first
  fit gives every sample a spot. Like statistics, its successful
  runs stay out of the dataset's job history and are deleted after a day (migration 0005).
- Each step looks up one batch, 64 samples per statement, with as many statements at once as the slot's share
  of the runner's pool (2 by default). On the 3.7M dataset that is about 42 samples a second for DINOv3 and
  140 for CLIP. The write runs in the step's publication, under `check_map_fence` with the map id read before
  the lookups, and with each sample's vector version: a batch looked up on a map that is being re-fitted or
  was replaced meanwhile writes nothing (its samples stay queued, for the new map), and a sample whose vector
  changed is skipped and queued again. Only samples still waiting are written, with their queue rows locked
  (`FOR SHARE SKIP LOCKED`), so a sample another writer placed meanwhile keeps its spot.
- **A full layout holds placement** (`embedding_viz_meta.placement_held_until`, migration 0006). A layout
  written over time would otherwise leave every sample ingested after its vectors were read at a spot taken
  from the old layout, kept on the new one. The SDK's `compute_visualization` takes the hold
  (`PUT .../embeddings/placement-hold`) before it reads the vectors, renews it from a thread every
  `HOLD_RENEW_SECONDS` (120) while it fits and uploads, and releases it (`DELETE`) once the new layout is
  ranked, or when it fails. `/reduce` holds it from its read to its ranking. A hold lasts
  `PLACEMENT_HOLD_SECONDS` (600) unless renewed, so a script that dies holds placement up for at most ten
  minutes. The scheduler skips a held map, and the write re-checks the hold with the map's
  `embedding_viz_meta` row share-locked: a batch looked up before a hold writes nothing, and a hold taken
  during a write waits for it (the layout's read then includes those samples). Samples that arrive meanwhile
  wait and are placed on the new layout. Any other writer of a full layout should take the hold too. If
  renewals fail for longer than the hold lasts (the server unreachable, or a long call holding the GIL), the
  hold lapses and samples may have been placed from the old layout meanwhile. The SDK notices: its deadline
  is kept on its own monotonic clock from before each renewal, and before writing the first coordinate it
  raises with the map unchanged ("run it again"). A lapse during the upload is reported before ranking.
- Measured against a fresh fit while a map grows (an arrival-order replay on the 3.7M dataset; [issue 20](https://github.com/freedomridge/freedomridge/issues/20)):
  placed images keep 2–3 times more of a full fit's local structure than placing from the fitted points alone,
  and 3–4 times more than UMAP's `transform`, but they drift as growth compounds. Re-fit a map that has grown
  well past its fit.

There is no other projection path: the projection daemon, `Dataset.project_pending` and saved model files
are gone (2.0). A projected `POST .../coordinates` must name the map's current id. Failure or cancel of a
re-fit leaves the old map current; a job that is no longer queued or running is not active, so nothing needs cleaning for
placement to resume. The runner's sweep (every 10 minutes) deletes work folders of finished re-fits and model
files that are neither current nor owned by an unfinished re-fit (models of maps fitted before re-fits
stopped keeping one); it reads both sets in one statement (one snapshot).

**Manual map writers.** Once `embedding_map_state.model_id` is set, incremental coordinate uploads
must supply that current identity. Full manual layouts and `/reduce` return 409: "This map is managed
by scheduled re-fits; run a re-fit to change its layout." Permanent `map_managed` and
`map_model_changed` refusals carry no retry headers. Only this map's active publication/rank can turn
its `map_refit_active` fence into `map_swap_busy`/`map_rank_busy` with a retry deadline.
Lock contention is dataset-wide: a sibling map's swap or rank retains that deadline too, so SDK
writes wait through the pause. Writer refusals use a human-readable string `detail` and a top-level
`code`; the retry headers keep their meaning.

Incremental writers fence from activation, not enqueue time: export happens after activation and
publication replaces every exported vector's position. Full replacements also fence queued re-fits
and hold the admission key shared while writing. Incremental writers do not hold that key. All writers
take the lifecycle gate before the map row; coordinate triggers separately take the rank fence.
Each scheduler admission has a savepoint and tries the admission lock: a contended schedule stays due
without rolling back other schedules. Run now returns a retryable 409 when admission is contended.
The scheduler records the failed admission reason and logs its traceback once per failure streak;
a missed start window retains that reason rather than assuming the runner was down.

`/reduce` checks before fitting and at coordinate publication, including each actual tile for patch
reductions. It commits its coordinates as rank-stale before re-ranking in a separate transaction,
waiting for other rank work. A rank failure returns 503 (`map_rank_failed`), explicitly saying the
coordinates were saved and pointing to `/recompute-viz-rank?field_name=...&full=true`. These unmanaged
maps are not covered by the automatic managed-map heal; retry ranking without repeating the fit. Patch
scatter uses its existing grid sampler. The SDK checks management before fitting and skips managed
maps; `compute_all` still completes hashes and metrics. Saved model files are published only after
all coordinate batches succeed.
SDK visualization treats a server without `/map-state` (404) as unmanaged.

**The worker** (`python/fridge/worker.py`) claims in a loop (each claim long-polls and counts as its
heartbeat). For a map re-fit's fit it downloads `vectors.bin` (resuming with Range), runs `fridge.map_fit` in
a child process (which sizes itself to the machine's free memory and sets `oom_score_adj=1000`), and after an
out-of-memory kill runs it again on 75% of the planned points, as the server's runner does, failing at the
smallest useful fit. It uploads `REMOTE_OUTPUTS` in chunks (resuming at the size the server reports) and
completes with each one's sha256. A thread renews the lease every `heartbeat_seconds`; a lost lease or a
cancel kills the child at once (a lost lease reports nothing; a cancel releases the job), and Ctrl-C or
SIGTERM releases it. A transfer or report that fails for a moment is tried again while the lease lasts.

Still to build for remote compute workers: object storage behind `map_artifacts` (the database already
stores keys, not paths), and starting/stopping machines on demand. The export and publish steps stay on the
server; only `fit` moves.

## Index advisor: the review

An administrator's **Index review** reads
the catalogs, PostgreSQL's per-index usage counters and the API's own field usage telemetry, and lists
suggested, possibly unused and overlapping indexes. It never creates or drops an index; every change is
a separate administrator action that runs as its own job (the removal and restore jobs come with the
actions). Views: Dataset Settings → Indexes (that dataset's field indexes and suggestions) and Settings →
System status → Indexes (every index no dataset owns: core indexes from schema.sql, ANN, array GINs,
hand-made ones). Both are administrator-only (`/api/datasets/{id}/index-advisor`, `/api/admin/index-advisor`).

- **Job.** `index_review`, server placement, user lane, **no dataset** (`dataset_id` NULL; a dataset
  review names its dataset in `args.dataset_id`). A read-only review therefore never waits behind a
  dataset's lane owner (a bulk edit, an hour-long index build), and the user lane lets slot 0 run a click
  promptly. The dataset's Jobs panel and history still list its reviews (`dataset_lane: false`: such a
  job neither waits behind nor holds up the lane). Review now joins the review already queued or running
  for that scope, whoever asked, and follows the five-second rule. One unit reads one repeatable-read
  snapshot (catalogs, `pg_stat_*`, `pg_relation_size`; no table scan; 5 s lock timeout); its fenced
  publication only records `index_observations`. The result is the job's `result`; the views read the
  newest one per scope.
- **Telemetry** (`backend/field_usage.py`, `field_usage_daily`). The grid (filter fields compared with
  `=`, `in`, `<`, `<=`, `>`, `>=`, and the sort field; `''` when none, the dataset's baseline), each
  field's filtered sidebar facet and autocomplete, and histograms record (dataset, usage, field, UTC day):
  uses, total ms and a log2-ms histogram. Only completed requests are recorded, only field names promote
  could index, and a request's time is shared equally among the fields it uses in one role (its waiting
  is counted once). Recording is an in-memory count in the API worker: no I/O, no await, no database or
  advisory lock, and its errors are swallowed; a stuck flush cannot slow a request. Each worker flushes
  once a minute in one statement that keeps only fields the dataset defines and skips deleting datasets;
  a crash loses at most a minute. Rows older than the window are pruned daily.
- **Rules**. One review period, 7 days, is the
  telemetry window and the payback horizon; an index is called unused only after **two** of them (14
  days: more than a whole cycle of the weekly jobs, with room for a missed run). Statistics start =
  the later of `pg_stat_database.stats_reset` and `pg_stat_bgwriter.stats_reset` (PostgreSQL stamps the
  latter whenever the counters start from zero: a new cluster, crash recovery; verified on 16.15). An
  index is observed since the later of that and when the advisor first observed it
  (`index_observations`, keyed by OID, cluster identifier and database OID, seeded when 066 is applied).
  First-seen is never back-dated: after a deploy or any restore (a new cluster, or `dropdb`/`createdb`
  in the same one) "possibly unused" says "not enough history" for 14 days. Verdicts are joint: an
  overlap is offered only when its kept index is in use (or needed) at least as much as the redundant
  one, the kept index is never itself offered, and the removals offered together never leave a foreign
  key without an index (the best supporter is kept as needed). *Needed* (never offered): primary key,
  unique, exclusion, constraint-backed, replica identity, and the only usable index (valid, not
  partial, B-tree or hash, leading with the key's columns) for a foreign key. *Overlapping* compares
  whole key definitions: columns, operator families, collations, sort options (equal, or all reversed)
  and INCLUDE columns, on the same table, method and predicate: a duplicate, a B-tree prefix, or an
  operator class whose operators are a subset of the other's (for example `idx_samples_metadata_gin`,
  jsonb_path_ops, within `idx_samples_metadata`, jsonb_ops). Overlaps are structural and shown at once.
  *Suggested*: slower than this dataset's indexed fields (or its own average without any) and, over the
  window, more extra waiting than a field index build measured here takes. Build jobs record
  `build_seconds` without their lock waits (sampled from `pg_stat_activity` every 0.25 s, one short
  borrowed connection per sample: a pg_dump's snapshot, another build's table lock; floored at that
  resolution, and nothing is recorded when the build's backend could not be watched), `waited_seconds`
  and the heap size in their checkpoint. Before any build is measured, only fields whose requests take
  on average at least twice the reference's average are listed, and never a name promote refuses. A
  request's time is shared among its filter fields, so a field always filtered alone is compared at its
  full request time (an accepted bias toward solo-used fields). The system view also lists field indexes
  whose dataset no longer exists.
- **Schedules.** A dataset's weekly review is a dataset schedule of type `index_review` (target
  `indexes`; admins only; no conditions or start window: a missed review runs at the next pass; a review
  that cannot be queued stays due and never aborts the pass). The system-wide one is the single
  `system_job_schedules` row, edited on the System status card; the same 60-second scheduler pass (lock
  20261064) handles both. The review's default slot (06:00) is when the default re-fit's start window
  closes; a re-fit may still be running then, which is harmless for a read-only review.
- **Keep the database's counters across restarts.** A clean shutdown keeps every index usage counter; a
  killed Postgres (crash recovery) zeroes them and restarts the 14-day clock. Docker gives a stopping
  container 10 s before SIGKILL, and a shutdown checkpoint writes up to shared_buffers (8 GB, about 80 s
  at 100 MB/s), so stop the database with `docker stop -t 120 freedomridge-db`. The compose files are
  deliberately not given a `stop_grace_period`: any change to the db service would make the next
  `docker compose up` recreate the database container (guarded by
  `test_database_compose_config_does_not_trigger_a_recreate`). `docker compose stop`, `down` or
  `restart` without `-t 120`, and a host shutdown, still kill Postgres after 10 s: the counters reset and
  the advisor's 14-day clock restarts, so the next reviews say "not enough history" and offer no unused
  index until it has passed again (safe, only later).
- **Removed indexes** (`removed_indexes`): an open row is an intentional removal; the system review's
  "missing core indexes" (schema.sql names absent from the database) honours it. `schema_check` and
  `/api/health` check only migrations and function bodies, never indexes.

## Index advisor: remove and restore

Every change the advisor offers is an administrator's click that runs as a job
(`backend/index_actions.py`; `POST /api/admin/indexes/{name}/remove {definition, reason}` and
`.../restore`, five-second rule). Indexes that are needed (constraints, the only index for a foreign key)
and indexes being built are refused (409), as is an index whose definition changed since the
administrator saw it. Vector (ANN: HNSW, ivfflat) indexes are not removed by the advisor (shown with
"managed by the embedding index controls"): rebuilding a large one with the serial, bounded-memory
settings takes hours, and even with tens of GB of maintenance memory a multi-gigabyte HNSW index
takes over half an hour.
Array-field GIN indexes are not either: their field would stay marked indexed.

- **One lock per table for every concurrent index build or drop** (`index_jobs.TABLE_LOCK`, 20261067
  by `hashtext(table)`): field builds, ANN builds, advisor removals and restores, demote, the carcass
  sweep and the dataset deletion's index drops take it with a try-lock and defer when it is busy (no
  failure counted); rename and clear of a promoted field and the creation of an array column answer 409.
  No CREATE or DROP INDEX CONCURRENTLY ever waits for the table's SHARE UPDATE EXCLUSIVE lock while
  holding a snapshot, which PostgreSQL would resolve by killing the other build's last phase as a
  deadlock (an hour-long restore after its full build). Promotes, demotes and Index this field therefore
  wait their turn while a restore runs.
- **`index_drop`**, user lane; a dataset job for a field index (it changes the field), no dataset
  otherwise. Unit 1 re-checks the index and, in one fenced publication, records the removal in
  `removed_indexes` (its `pg_get_indexdef`, size, usage and window, who and when) and, for a field
  index, marks the field not indexed under the shared lifecycle try-lock: a promoted field without its
  index would turn the sidebar's index descent into one scan per value. Unit 2 waits while a database
  backup is queued or running (deferred, no failure counted), takes the index's own lock (a field index:
  its dataset's 20261017) and the table's, re-checks that the index is not needed now (two removals can
  never together leave a foreign key without an index), and runs `DROP INDEX CONCURRENTLY IF EXISTS` on
  its own connection with a 12-hour budget (never the pool's 300 s command timeout): SHARE UPDATE
  EXCLUSIVE blocks neither reads nor ingest, and the drop waits as long as transactions already using
  the table need. It then publishes `dropped_at`. An interrupted drop can leave the index INVALID; the
  retry or Remove again finishes it. Cancelled between the units, the record shows "removal stopped" and
  the next review closes it. Submissions are serialised per index: two clicks queue one removal, and a
  demote never joins an advisor removal (409). Until its first unit records it, a queued removal shows only
  in the GET's `pending_actions`; the page polls while any removal or restore is pending, and refreshes
  after a Restore that another administrator finished first (404). While the drop (or a restore's build)
  waits for older transactions, its job message names them, sampled every 2 s from `pg_blocking_pids`
  and `pg_stat_activity`: pid, application name, state and transaction age (a forgotten `BEGIN` holds
  every index change on the table for up to 12 h). Only administrators see those; anyone else following
  the job sees that it waits for older transactions.
- A field-index removal, like every job of the dataset's user lane, holds that lane while it waits out
  a backup: later bulk edits or promotes of that dataset wait for it (the claim marks a job started,
  so deferring before unit 1 would not help).
- An orphan field index (its dataset was deleted) is removed like any other index (kind `other`).
- **`index_restore`**, user lane, no dataset: checks room for the index, its validation temp sort and
  its WAL, by access method as measured on PostgreSQL 16.15 (GIN `size × 1.89`: temp 0.57×, WAL 0.32×;
  btree `size × 2.57`: temp 0.88×, WAL 0.69×, its build sort spills; other methods take the larger)
  plus the usual headroom (507 at the request and in the job), waits out a backup, takes the same locks,
  and runs `CREATE INDEX CONCURRENTLY IF NOT EXISTS` from the recorded definition through
  `index_jobs._build` (an INVALID leftover is dropped first; a failure or cancel removes its own),
  verified valid and identical, then `restored_at`. A field index is restored by the field index build
  (promote), which re-derives the index from the field's current definition and closes the record; the
  record shows "restoring" meanwhile. A field record whose field was renamed, cleared or removed is
  closed by the next review (nothing to restore).
- **The Indexes tab's remove-index toggle** (`POST /datasets/{id}/fields/{f}/demote`) runs the same
  `index_drop` job without a removal record (the SDK's `demote_field` waits on it). It used to run a
  plain `DROP INDEX`, whose ACCESS EXCLUSIVE request queued behind any long reader (a pg_dump) and
  stalled every later read and write of `samples`, ingest included, for the rest of the backup. Adding
  an index (promote) already built with `CREATE INDEX CONCURRENTLY`; rename and clear already dropped
  concurrently. Creating an array column now runs its `ALTER TABLE samples` with a 2-second lock
  timeout (409 when busy) instead of queueing ACCESS EXCLUSIVE behind a long reader.
- A review closes only the stale removal records of its own scope (a dataset's field records, or the
  system's), so every closed record is reported by some review.
- **What could bring a removed core index back:** `schema_check`/`/api/health` never check indexes;
  `apply_migrations.sh` re-applies only functions;
  a fresh install from schema.sql creates every core index by design (a removal is this deployment's
  operational state); a dump carries the record and lacks the index. A restore into a database
  initialised from schema.sql, an operator script (`copy_objects.py`, `backfill_sample_lonlat.py`,
  `build_ann_indexes.py`) or a hand-made CREATE INDEX re-creates it: the next review reports "re-created
  outside the advisor" and closes the record. The startup "No HNSW index" warning skips an ANN index
  removed on purpose.

## Backups

`backup` is a dataset-less job that runs `scripts/backup_local.py` into `FRIDGE_BACKUP_DIR` (see
docs/BACKUP_AND_MIGRATION.md). The one-row `backup_schedule` table holds
`interval_hours` (NULL means no scheduled backups), `start_hour` in the server's local time, `keep`, and
`timing_changed_at`, which moves only when the interval or start hour changes.
`backups.schedule_backups` runs every 60 seconds under an advisory lock, on the database's clock. It
queues one system job per slot (`start_hour` plus each `interval_hours` from 2026-01-01, counted on the
wall clock so a daily 02:00 stays at 02:00 across daylight-saving changes). It skips a slot the timing
changed after, a slot that already has a backup job created at or after it or one that succeeded after
it, and anything while one is queued or running. `POST /api/admin/backups/run` queues one for an admin
or returns the active one. Only one runs at a time; the script's lock would fail a second.

The policy allows a 12-hour unit and two attempts. A failed backup (an unmounted drive, a full disk)
fails at once and waits for the next slot. A backend restart signals the runner's whole process group,
so the script stops itself with "stopped by SIGTERM"; the job didn't stop it, so it raises a transient
error and the restarted runner runs it again. A 12-hour timeout or a lost lease also gets the second
attempt. When the job stops the script (cancel, timeout, lost lease) it sends SIGTERM; the script kills
pg_dump, ends its sessions in the container, deletes the partial dump and writes a failure record
within the 8 s grace, which fits the runner's 20 s shutdown. When the script fails without a record
(another run holds the lock, the folder vanished), the job's error is the script's last log line.

## Acceptance coverage

| Requirement | Coverage or remaining work |
| --- | --- |
| Short claims, deterministic exclusion, active dedupe | Real PostgreSQL queue tests, including competing claims and mismatched dedupe arguments. |
| Fencing and cancellation | Expired/replaced tokens, publication rollback, cancellation races, bounded lock waits, and expiry while waiting for a lock. |
| Recovery and fairness | A real subprocess is killed after its first checkpoint, then resumes without duplicate effects; timed-out work yields to another dataset. |
| Placement | Server runner leaves compute and unsupported kinds queued; an already-started operation retains its lane across lease recovery until resumed or explicitly ended. |
| Status and SDK | Access-control, keyset pagination, error propagation, timeout and cancellation tests. Deleted-target job history remains available to authorized users. |
| Worker down | Backend heartbeat tests and System Status UI tests cover offline, unavailable and recovered slot health. |
| Statistics-specific acceptance | Real PostgreSQL tests cover late commits, finite barriers, coalescing, mixed types, concurrent edits/restore, cancellation, retained blocks, paging/subdivision, socket loss and independent datasets. An opt-in 58-field scale diagnostic measures counts/ranges, write overhead and temporary bytes. |
| Supported local deployments | Managed local runner and real multi-worker API startup/shutdown tests; dev/prod/demo compose use the same entry point. |
| Later operation conversions | Explicit concept operations, field/ANN index builds and the duplicate refresh are jobs with the five-second rule; bulk edits/delete and dataset deletion are converted; version create/revert, manual layouts (`/reduce`) and remote compute are not. |

Local synthetic diagnostics do not certify production-scale latency.
A trigger initializes `job_controls` for every inserted job (including direct SQL), and an index
on kind and creation time serves the scheduler. The runner checks the migrations ledger and never
applies migrations itself: started before the database records every migration it ships, it waits
without claiming work, leaving its API available, and resumes once they are applied.
Finished-job retention covers only the routine successful system jobs described above, so total
history storage is not bounded, and idle telemetry still writes to the database.

A unique index covers every started, unfinished dataset operation, so two cannot interleave in the
same dataset/lane.

### Bulk view mutations

`POST /datasets/{id}/samples/bulk-tag`, `bulk-set`, and the new admin-only
`bulk-delete` submit `bulk_mutation` jobs in the user lane. Field rename, clone,
and clear also use this handler for datasets larger than 1,000 samples. Small
explicit-ID edits keep their synchronous result; `background: true` forces a job
(`?background=true` for clear). A submitted operation returns 202 `{job_id,state}`.
Tag and metadata targets accept `sample_ids`, `selection_token`, `excluded_ids`,
and a frozen `filters` object; `{}` explicitly selects the dataset. Delete requires
one of these targets and uses the same administrator policy as single delete.

`POST /datasets/{id}/embeddings/{field}/delete` takes the same target and administrator policy and
submits a `bulk_mutation` job with operation `delete_embeddings`, whose result is `{deleted: n}`. Each unit
locks its samples `FOR NO KEY UPDATE`, takes the lifecycle gate and the dataset rank fence (20261020)
shared like sample deletion, and deletes the field's rows from `embeddings`; coordinates, projection
queue, rank rows and counts follow by cascade and trigger. Nothing else is removed, also when the field is
emptied: its `embedding_map_state` and `embedding_viz_meta` rows stay. Only `filters: {}` selects the whole
field: a non-empty filter that compiles to match every sample (its conditions have no value, so
`parse_filter_node` gives `TRUE`) is refused with 400. Resuming it requires an administrator, like sample
deletion.

Submission polls the lifecycle gate for up to two seconds before capturing the maximum
committed sample ID. Ingest allocates IDs under the shared gate, so submission waits
briefly for an uncommitted low-ID writer, then returns a retryable 409 if it is still
busy. Frozen filter expressions and typed parameters belong to the job; datetime
parameters retain timezone and microseconds through JSON storage and resume. Older
JSON-only jobs remain readable. The filter is compiled with the dataset ID, exactly as the
grid compiles it, so cross-grain subqueries (`has_matching_object`, `parent_matches`) are
scoped to this dataset whatever value the filter binds first. Pure ID-membership filters
(the SDK's id views) become explicit targets and keep no filter: membership is the target
list, intersected with `sample_ids` when both are sent. ID values must be 32-bit integers
(400 otherwise, before anything is queued). Explicit IDs and per-sample
patches live in `bulk_job_targets`. The lifecycle gate is held only to capture the cutoff
and queue the job; targets are then inserted after it is released, at most 50,000 per
transaction (units ignore targets above the cutoff, so they need no gate). Until staging
finishes the job is unclaimable (`available_at` infinity, checkpoint `staging`). If staging
fails (including the dataset being sealed for deletion, checked per batch), the job is
recorded as cancelled and discarded (`result.discarded.reason`) and the request fails, with a
retryable 409 for contention; if the API process dies mid-staging, the job stays waiting until someone cancels and
discards it (resume refuses it). Selection tokens (including nested filter
references) are pinned before acknowledgement. Queued, failed and cancelled jobs
retain their pins for resume, even after the token's ordinary two-hour lifetime.
Selection cleanup seeks past a pinned token/timestamp group using an expiry index,
rather than scanning every pinned sample on every cleanup pass. Token creation no
longer performs synchronous global expiry sweeps. Dataset deletion drains these
job-owned targets and pins in bounded units too.

Each unit seeks at most 1,000 candidate IDs, evaluates the frozen filter, and locks
only matching rows in ID order. PostgreSQL rechecks concurrently updated rows when
acquiring their locks; a fresh statement then rechecks the filter and its subqueries
under those locks. Nonmatches do not acquire row locks or dirty heap pages. It atomically commits
sample changes, the last examined ID, examined count, and actual affected count
through the runner's existing lease/cancellation fence. Missing rows are no-ops;
arrivals above the captured maximum are untouched. Statistics, concept and duplicate
maintenance retain their ordinary per-chunk triggers; the handler does not request
a full rebuild. Field-definition copies suppress their full-refresh trigger because
the job records work for every captured block. Final pruning uses one indexed
key-existence probe under a short try-only definition guard, retaining definitions
needed by later arrivals. Large field operations require promoted source fields to
be demoted first; they do not hide an unbounded index rebuild inside a runner unit.
Bulk delete retains removed field names in its checkpoint and prunes unused implicit
definitions in that same final phase; declared/promoted fields and later values survive.
Promotion/demotion refuses unfinished bulk field jobs. Version revert refuses all
unfinished bulk jobs, including stopped jobs whose targets are retained for resume.
The 409 names up to three blocking jobs (operation, ID, state, submitter) and says how to
unblock: let queued/running jobs finish or cancel them, then resume or discard stopped
ones. `POST /jobs/{id}/discard` (Job history's Discard button, `client.discard_bulk_job`)
is open to an administrator or the submitting power user, like cancel and resume, and only
for a failed/cancelled bulk job. It keeps the committed chunks, records
`result.discarded = {by, user_id, at}` and the message `Discarded by ...` in job history,
releases the job's selection pins at once, and makes the job non-resumable and invisible to
these guards. Its remaining targets are inert and are deleted by the maintenance selection
cleanup in bounded units (1,000 rows). A power user cannot resume or discard another user's
job (403, the cancel policy); the 409 names the submitter to ask. Submission, index
operations and revert serialize on the index-operation guard, acquired before a revert
takes its SERIALIZABLE snapshot. A refused submission's 409 now names the cause.

Each unit runs with `plan_cache_mode = force_custom_plan`: after five executions on a
pooled connection PostgreSQL would otherwise switch to a generic plan, in which a long
bound ID list (exclusions, `in` filters) is scanned linearly for every row (4.1 s per
1,000-row unit at 200k IDs). Edit units lock matching rows `FOR NO KEY UPDATE`, so FK
writers (embedding, hash and concept-score uploads) for those samples do not wait; delete
units keep `FOR UPDATE`. Delete units also take the per-dataset map rank fence (20261020)
shared, try-only, right after the lifecycle lock: the cascade to map coordinates takes it
in the coordinate triggers, and a full rank rebuild holds 20261016 shared then 20261020
exclusive, so taking it inside the cascade could deadlock.

Filter views apply to samples that existed at submission (ID at or below the captured
cutoff) and match the filter when their 1,000-ID unit runs; this is by design. New arrivals are never included, but an existing sample
that is edited or re-ingested with `on_duplicate='update'` (same ID) before its unit runs
is judged by its values at that time. Explicit IDs, pure ID views and Select-All tokens
(everything the UI sends) are exact.

Cancel retains the committed prefix. `POST /jobs/{id}/resume` requeues the same
failed/cancelled bulk job and cursor under a row lock. Only an administrator or the
submitting power user may resume; deletion still requires an administrator.
Resuming a job whose dataset is gone returns 404. Expired leases recover the last committed unit; stale publishers cannot commit. Per-sample
target cleanup is itself bounded and restartable. Ordinary writes remain available
between units. Pathological per-sample cascade fan-out is subject to the runner's
five-second publication deadline and adaptive subdivision, and may fail resumably.

Per-sample patch requests of up to 5,000 samples (the SDK's batch size) stay synchronous;
larger ones, or `background: true`, become jobs. Same-value requests keep the
1,000-sample threshold.

The SDK blocks by default and returns the prior dict/count shape. `wait=False`
returns a `Job`, and `client.resume_bulk_job` continues a stopped operation.
`DatasetView.tag_samples`, `untag_samples`, `set_values`, and `delete_samples`, plus
the corresponding Dataset methods, send unrestricted view filters directly. Limited,
offset and materialized views resolve their exact membership to explicit IDs first;
ordering is preserved when it affects membership. Per-sample metadata writes submit
at most `min(batch_size, 5000)` patches per request and wait for each batch by default,
summing the affected counts. Completed batches survive a later failure;
runner jobs additionally retain their committed prefix. `wait=False` returns a single
`Job` for one batch, a list of `Job`s for multiple batches, or an empty list for no
patches. The single-field setter also constructs patches incrementally. The tag
modal announces submission through Jobs; job history shows
committed counts, prefix cancellation semantics and authorized resume. Terminal bulk
jobs invalidate the dataset's sample and derived UI caches, including fast jobs that
finish between active-job polls after a local submission. The first history response
seeds completion tracking without invalidating the page. History polls every 5 seconds
while bulk work is visible and every 30 seconds otherwise, so jobs submitted elsewhere
(SDK, another tab) that finish between active polls still refresh the page;
disappearance from the active list requests final history.
Refresh invalidation matches the dataset ID position in query keys, not arbitrary
numeric key components.

Known limits:
- Every submission still try-locks the index-operation guard (20261017), so tag/set/delete
  submissions get a retryable 409 while a promote's `CREATE INDEX CONCURRENTLY`, a demote or
  a revert runs. Only field operations need to exclude index builds, but revert relies on
  the same guard to see submissions before its SERIALIZABLE snapshot; splitting it needs a new
  revert/submission key and a revert locking change, so it is left for a separate change.
  The 409 now says why.
- Per-sample setters with `wait=False` return a `Job` for one batch and a list otherwise;
  if a later batch's submission raises, the already-queued jobs keep running but their
  handles are not returned; `timeout` applies per batch. No in-repo caller uses
  `wait=False` there. NumPy integer batch sizes are accepted.

Bulk jobs keep their targets and pins in their own tables, with the scoped field-statistics
trigger context; they perform no sample backfill. The operator statistics script skips deleting
datasets (including sealing races), and an explicit request for a deleting dataset fails clearly.

### Long-running endpoints (five-second rule, 065)

An endpoint whose work can take long waits **at most five seconds** for its job
(`backend/job_wait.py`, measured from the start of the request). A job that finished answers
with the final JSON exactly as the synchronous endpoint did; a job that failed answers with the
status the synchronous code used (input errors 400, missing inputs 404, conflicts 409,
unprocessable 422, otherwise 500) and the job's message; anything still queued or running
answers **202 `{job_id,state}`**. `?wait=false` answers 202 at once. Waiting never cancels the
job. The SDK submits once and waits on the job by default (`timeout=`), returning the former
shape; `wait=False` returns a `Job`. Unmodified HTTP clients must follow a 202. Version revert is
deliberately unchanged (all-or-nothing). Converted:

| Endpoint | Job kind, lane | Units |
| --- | --- | --- |
| `POST /datasets/{id}/concepts`, `/concepts/{c}/rebuild` (new direction), `/refine`, `/rescore` | `concept_rescore`, user | definition → score → count → finish |
| `POST /datasets/{id}/hashes/refresh-duplicates` | `duplicate_maintenance`, maintenance (unchanged job) | as before |
| `POST /datasets/{id}/fields/{f}/promote` | `field_index_build`, user | one build unit + fenced publication |
| `POST /datasets/{id}/quality/ann-index` | `ann_index_build`, user | one build unit |

**Explicit concept operations.** The request does the bounded part synchronously: validation,
the soft cap and name check, seed means, explicit weights and text encoding (so their errors stay
synchronous 400/409s). Unbounded training reads (the probe's random background, the refine's
current-cut count and self-training samples) and the probe fit run in the job's first unit,
with the fit in a thread. That unit's fenced publication writes the definition, sets
`scores_ready=FALSE`, and records the scan boundary (`upper_id`, the embedding-ID watermark) in
the job barrier from the same transaction, so anything committed earlier is scanned and anything
later reaches the concept as pending automatic work (this closes the creation race). Scoring then
reuses the incremental concept scorer in bounded units (up to 4096 samples, halved on timeouts): changed scores
are upserted, scores of missing vectors removed, nothing is deleted wholesale. Modes: full
(samples and existing scores up to the boundary), pending (an automatic concept's own pending
blocks plus the field's source blocks; only the concept's own rows are acknowledged, by the
measurement snapshot) and appended (a manual concept's embeddings above its watermark, at most
65,536 IDs per unit). A full pass ends with a count unit (45 s budget) and a finish unit that
calibrates the threshold (create without a threshold: into the score band; refine: preserving
the cut level measured before the refit), writes the result, sets `scores_ready`, clears the
owner, marks the concept initialized and advances the manual watermark. The explicit kind has no
txid-snapshot barrier, so restore recovery needs no rewrite for it; claims pause during recovery
because it reads the pending queues.

One owner per concept: submission takes the per-concept advisory lock that automatic submission
uses, locks the concept row, refuses (409) while another explicit job is queued or running, sets
`embedding_concepts.score_job_id`, and bumps `score_revision`. Every explicit publication takes
the shared lifecycle try-lock and checks `score_job_id` and the revision under a NOWAIT row lock,
so a superseded or deleted concept fails the job visibly (`JobConflict`). Automatic scheduling,
submission, measurement and publication skip a concept while its owner job is queued or running,
or while `scores_ready` is FALSE (a stopped definition change is resumed, not overtaken); the
revision bump fences an automatic unit measured before the explicit submission. Toggling
auto-scoring while an owner runs is a 409 (it would void the owner's revision).
`require_concept_scores(concept)` (SQLSTATE 55000 → 409) is evaluated once per query by the
`concept_select`/`concept_exclude` filters and the scatter's concept rejection, so a concept whose
definition changed is never filtered with a mix of old and new scores. Every read path answers
that 409: the grid's page and SDK slim reads re-raise it past their generic 500 wrappers, the
sidebar's best-effort filtered statistics check the filter's concepts first instead of falling
back to unfiltered values, and preview/boundary/step refuse too. The grid shows the refusal
("Concept … is being rescored…", Retry) instead of "Loading samples…" and reloads when the job
ends. The concept list reports `scores_ready` and `score_job {id,state}`. Only a full pass (every
definition change runs one) sets `scores_ready`: a non-full rescore of a not-ready concept is a
409 pointing at Resume or a full rescore, and a non-full pass never marks scores ready.
Raw weights are dimension-checked before submission (400, as before). Training samples are cast
to text only after `ORDER BY random() LIMIT`, avoiding conversion of vectors that the
training sample will not use.

Cancellation and failures keep the committed units. `POST /api/jobs/{id}/resume` (an
administrator or the submitting power user, as for cancel) requeues the same stopped job and
checkpoint while it still owns the concept, bumping the revision under the row lock; the concept
card offers Resume. The resumed job is unstarted again, so it queues behind another started job of
the dataset's user lane instead of colliding with it (a bulk resume in that state answers 409).
A new explicit operation on a concept whose owner stopped takes it over.
Deleting the concept fails its job ("deleted while it was being scored").

**Index builds.** Promote validates as before and refuses a competing index operation at
submission (try-lock of 20261017, 409), joins an unfinished build of the same field, and checks
unfinished bulk field jobs. The unit takes a pool connection of its own and holds the session
advisory lock for the whole build (promote: 20261017 per dataset; ANN: 20261066 keyed by
`hashtext(field)`, registered in schema.sql); contention defers without spending failures. It
sets `client_connection_check_interval`, so a killed runner's backend notices the lost client and
aborts the build, releasing the lock. With the lock held, it drops an INVALID leftover
concurrently (5 s lock timeout; a busy table leaves it for the next attempt), builds with
`CREATE INDEX CONCURRENTLY IF NOT EXISTS` (a server-side 3600 s `statement_timeout` on the build
session; asyncpg's client timeout is a fallback a minute later; ANN keeps 256 MB
`maintenance_work_mem` and no parallel workers), verifies `indisvalid`, and drops the carcass on
failure or cancellation (task cancellation cancels the server command first). An exceeded budget
fails the job without a retry (the old client timeout was transient and rebuilt three times).
Bad values fail with the former 409 message. A cleanup that cannot get its locks within 5 s (a
`pg_dump` or another long reader holds the table) leaves the INVALID index, which is still
maintained on every write; the runner sweeps such indexes every 60 s
(`index_jobs.sweep_invalid_indexes`): our naming patterns only, skipping any index a build is
creating (`pg_stat_progress_create_index`) or whose advisory lock is held, dropping concurrently
under that lock with the same 5 s bound and retrying on the next sweep. Promote re-reads the definition after the build: a changed
definition drops the unpromoted index and fails (409); the fenced publication marks the field
promoted only if the definition and a valid index are still there (otherwise it defers and the
retry fails visibly). Expected refusals release the lock cleanly; any other failure terminates
the session. The ANN dimension scan also moved into the job. Promote, ANN and duplicate refresh
join an unfinished job of the same target whoever submitted it, so any reader of the dataset sees
those jobs' results; their errors are shown only when they are requester-facing refusals
(otherwise "The job failed; an administrator can see why").

**Frontend.** A 202 from these endpoints is announced (`fridge:job-submitted`), so the dataset's
Jobs indicator and history refresh at once, and followed to the job's result. Terminal
`concept_rescore`, index and user duplicate jobs refresh the dataset's queries like bulk jobs,
including jobs submitted elsewhere. The concept card shows "Scoring…" or "Scoring stopped" with
Resume and disables filtering modes until the scores are ready.

**Deployment.** Don't run runners of different versions side by side: an older runner doesn't
know the job ownership columns.

### Layout coordinate storage

Coordinate writes now upsert `embedding_coordinates`, keyed by `(sample_id, field_name)`,
without creating new embedding tuples or HNSW entries. The existing embeddings unique
constraint supplies its cascading FK. The table stores complete, finite float8 pairs; absence
means unprojected. The scatter rank cache (`embedding_viz_rank`) remains in place, synchronized by statement-level
coordinate triggers. Moves preserve rank and mark coverage stale; clears remove the rank row
and decrement its count. Equal-value uploads are no-ops. Sample/embedding deletes cascade,
including removal of pending work. Coordinate-only disk estimates no longer include vector
index amplification.

`embedding_projection_pending` is populated in the embedding INSERT/UPDATE transaction by
independent statement triggers with transition tables. Empty transitions return immediately,
singleton statements use cached plans, and bulk statements are dynamically planned. A vector
change clears its old projection and queues it; an identical retry does neither. The changed
point disappears from the map, lasso and polygon filters until it is re-projected, and its
field's coverage is marked stale. These triggers are independent of the concept-maintenance
triggers. The pending API and direct UMAP worker
read the dataset/field queue index and fetch only those vectors. Successful coordinate writes
remove the corresponding pending rows. Pending vector pages include a `vector_version` (the
embedding's `xmin`); the SDK and direct worker send it back with their coordinates. Publication
locks the embedding row in SHARE mode and checks that version, skipping stale results without
acknowledging the queue. A vector update that follows publication clears and requeues the point.
These locks create no new embedding tuple or HNSW entry. The SDK uses a keyset cursor after
its first page, so it does not recount the remaining queue for every batch. A lower-ID change arriving behind
that cursor remains queued for the next pass. `recompute_viz_rank(..., FALSE)` is a constant
cost compatibility no-op for ranked fields: coordinate writes already maintain them. If a
field has no metadata, this call performs a full rank once, recovering from a failed initial
rank after a fit. Until then, scatter uses its grid fallback. No routine full-field anti-join
or periodic refit is performed.

A disposable PostgreSQL 16/pgvector probe on tmpfs migrated 600,000 pairs (300,000 samples,
512/1024-D fields) in 1.49 s, generating 184.0 MB WAL and 145.9 MB
coordinate storage. Before moving the FK/index after the copy, the same fixture took 5.17 s,
342.7 MB WAL and 167.4 MB storage. Rank checksums, float8 pairs and embedding heap size were
unchanged; the revised copy also left every embedding's xmax untouched.
Extrapolated linearly, tmpfs needs about 2.5 s, 0.31 GB WAL and 0.24 GB new storage per million
pairs. On real disks, budget about **15–20 s, 0.6 GB WAL and 0.3 GB new storage per million pairs**
for the deploy window, allowing for disk I/O and a larger working set.
Allow additional space for WAL retention and any unprojected backlog. These are planning
estimates, not a production SLA. Pending page/count work remains bounded by the queue.

Legacy `embeddings.x/y` columns remain frozen and unused; a later migration will drop them.
Triggers reject any UPDATE naming x/y and INSERT with either coordinate non-NULL, with an explicit
`coordinates live in embedding_coordinates, not embeddings.x/y` error. This prevents old binaries from silently
writing dead columns.

Ingest API/SDK shapes are unchanged, including callers outside this repo. Pending vector responses
and projection uploads add an optional `vector_version` (a version list in the binary header).
Deploy the matching SDK for projection race protection; older SDKs and manual coordinate uploads
without versions retain their existing unconditional-write behavior.
All supported coordinate writers use the new table; direct SQL clients must stop writing the
legacy columns. Atomic re-fits and model publication fencing belong to the map re-fit job;
this storage change does not add generations, staging or frontend layout tokens.
