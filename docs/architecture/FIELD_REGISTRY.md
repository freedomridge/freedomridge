# Field registry contract

Freedom Ridge serves two workloads: fast exploration of large source datasets, and
curation of smaller training datasets whose saved versions reproduce their metadata
and annotations. Field discovery must not require a statistics refresh, and updating
statistics must not change a saved version's meaning. Bounded statistics jobs (`JOBS.md`) build
on this contract without introducing comprehensive audit logging.

## One definition per dataset field

`dataset_fields` owns the declared type, origin, display override, promotion state,
and overlay configuration. Declared types are `unknown`, `boolean`, `number`,
`string`, `datetime`, `list`, and `object`.

- `metadata` means a JSON metadata key. A JSON list is always `list`, never a column.
- `native_array` means a registered physical `TEXT[]` column belonging to this dataset.
  Only the native-array creation endpoint creates this origin.
- `system` means a built-in or derived field. These definitions are not versioned.
  Built-in tags and derived annotation labels may instead be computed views.

The global `sample_array_columns` catalog describes physical storage, not dataset
membership. Query builders receive dataset-scoped native-array names explicitly;
filters, projections, autocomplete, aggregations, and sampling must not infer
membership from another dataset's column.

`derived` is a frontend presentation type for coverage-presence controls, not another
declared metadata type. Its `embeddings.<field>`, `hashes.<field>`, and `location` presence
filters compose with native-array membership and JSON-list presence, including a same-named
native column owned by another dataset.

## Registration and writes

Registration shares the transaction that writes metadata. Ingest uses 500-row SQL
batches, preserves request order, and registers the effective metadata of successfully
written samples once per request. Skipped duplicates cannot introduce fields. The
single set-based registration orders new/unknown definitions by name; already-declared
fields are read without updating or locking their registry rows.

First-write inference considers every non-null value in the request, not just its first
row or first 500-row chunk. Extended ISO dates/timestamps infer `datetime`; heterogeneous
values infer `string`. Later batches cannot downgrade a concrete declaration. The only
automatic widening is `unknown` to a concrete type when a null-only field gains values.
Invalid non-finite or out-of-float8-range numeric JSON values are rejected before encoding.

An explicit correction uses `PATCH /datasets/{id}/fields/{name}/type` with
`{"field_type": "number"}` (or another canonical type), SDK
`dataset.set_field_type("score", "number")`, or Dataset Settings → Fields. This changes
interpretation, not stored sample values. It clears incompatible display/overlay settings
and cached measurements and requests a full refresh. Native/system types cannot change;
an indexed metadata field must be unindexed first. Statistics and quality jobs never
silently change a concrete declaration.

PATCH locks its sample while merging, preventing concurrent edits from losing each
other's keys. Bulk metadata edits, quality outputs, rename, clone, clear, and version
restore maintain their registry state transactionally. Value-list invalidation is part of
the data transaction. Writers record affected blocks in that transaction, coalescing only
their own pending work. Ordinary edits no longer request a full scan or take a shared
dataset bookkeeping row lock. Registry/type changes and explicit full requests also queue
durable work. The statistics job's lease/checkpoint publication owns acknowledgement.

Every supported writer takes the dataset lifecycle gate before sample or registry locks:
shared for additive writes, exclusive for destructive definition changes. Ordinary edits
remain concurrent. Interactive exclusive requests use a try-lock and return HTTP 409 when
busy; they never join the wait queue and block later ordinary writers. This includes sample
deletion, key removal, replacement ingest, and native-array registration. Re-registering an
existing native array is idempotent and preserves its cache. Only clear, rename, revert, and
explicit type correction may wait for the exclusive gate. These operations temporarily
block same-dataset writers, not readers or other datasets. On datasets larger than 1,000
samples, rename, clone and clear run as resumable bulk jobs (`JOBS.md`). SDK ingest
distinguishes a busy 409 from a duplicate result.
The namespace is `20261016`, keyed by dataset ID. Registration precedes cache invalidation
so unknown-type widening uses the same lock order across ingest and PATCH.

Writers pre-lock all affected existing cache rows, including derived labels, in field-name
order before changing any cache row. Missing parent-field caches receive an explicitly
unmeasured placeholder, so a concurrent refresh cannot miss an invalidation just because no
cache row existed. Bulk metadata writers first lock their target samples in ID order; heap
tuple movement by PATCH cannot change that order. Bulk uniform updates bind one patch for the ID set, not a
copy per sample; annotation ingest merges only effective written labels and never recounts
the source dataset.

Index build/drop, clear, and rename use a separate try-only operation mutex (`20261017`,
keyed by dataset ID), acquired before any lifecycle gate and held through old-index
cleanup. Conflicting operations return HTTP 409 and can be retried. Ordinary sample writes
remain concurrent with index builds. No lifecycle gate or row transaction is held during
`CREATE/DROP INDEX CONCURRENTLY`; a final short lifecycle check prevents a completed build
from promoting a removed or retyped definition. Rename releases the operation mutex before
promoting the new name. This prevents delayed cleanup from dropping a recreated field's index
without introducing a waiting advisory-lock snapshot that can deadlock a concurrent build.

Restore no longer depends on the retired refresher's dataset mutex. It can run during a
read-only statistics measurement. A publication encountering restore's lifecycle gate defers
without charging a failure; changed definitions, caches and full-request counters prevent it
from certifying pre-restore measurements. A subsequent job processes the restored data.

## Statistics and presentation

Readers start with the registry and LEFT JOIN `dataset_field_stats`. Missing measurements
produce `stats_pending`, not missing fields. Stale cache rows cannot resurrect cleared or
renamed fields. Statistics refresh never writes field definitions or determines their type.

`presentation_type` maps declaration, display override, and cached values to the UI control.
A categorical string remains an enum even when its preview is incomplete. Completeness
does not mean cardinality: an over-cap string has no cached value list and remains a string,
so the query builder retains `contains`. Subsequent small ingest batches cannot repopulate
that list until measurement establishes that the complete set fits again. Transient invalidation
of a small categorical list keeps its preview and presentation type. Completeness
chooses checkboxes versus server search, never exact versus substring semantics. All
listed-value selections use exact membership. Scalar metadata text pickers also offer an
explicit first row, `Contains "text"`; clicking it or pressing Enter adds a substring term,
never an exact value inferred from typing. Exact values and substring terms on one field form
one OR group; Exclude negates that whole group. Chips identify `contains` terms, and draft
preview and Apply share the same conversion. Small complete lists retain their checkboxes.
Numeric enums, native arrays, and derived label lists remain exact-only. Query-builder
`contains` remains available. Promoted strings retain the index skip-scan. A cached preview is never proof that an
uncached value does not exist: incomplete tag, label, and enum pickers search the server
even when some cached values already match. Failed/budget-limited searches are not
presented as an authoritative "no matches" result.

Incremental statistics merge bounded new values without discarding existing completeness
proofs. Writers invalidate changed keys atomically; a missing cache is not proof of an empty
field. Scalar previews use a 100-value cap, tags/labels/native arrays 200, with cap-plus-one
queries detecting overflow. Numeric enums preserve PostgreSQL's JSONB text representation,
so displayed choices use the same equality representation as filtering.
Promoted numeric choices retain index-based numeric equality across grid, scatter, lasso,
similarity search and filtered facets, and the same matching (a numeric string compares as a
number) in hash duplicates, sampling, bulk jobs, mirror tag forwarding and version queries, which
read the promoted fields from the dataset's array context. The existing 300 ms autocomplete debounce is retained;
superseded requests are aborted, and filter changes restart rather than strand the search.

Cold label/array caches use optimistic publication. A missing row is first committed under
a try-only exclusive lifecycle gate; when busy, the read returns an explicitly incomplete
preview without writing a placeholder. The subsequent scan holds no row or lifecycle lock.
Publication uses a try-only shared gate and checks that the cache row's PostgreSQL MVCC identity (`xmin`) is unchanged.
A concurrent write or delete makes publication fail safely and the response incomplete;
there is no full-scan retry, monotonic checkpoint, audit log, or history table. A measured type
mismatch is not pending: incompatible bounds are hidden until an explicit type correction
or compatible measurements exist. Native arrays are never pending merely for lacking cache.

The statistics job measures bounded blocks and combines cached summaries in bounded pages,
without holding publication locks during sample scans. A saved MVCC transaction snapshot
identifies committed work; an increasing sample ID is not a commit-order checkpoint. Work
committing during a measurement stays pending unless that measurement observed it. Completed
block summaries survive failure, while publication never writes field definitions. Declaration
identity excludes index/display state; overlay field-name indexes revalidate current membership
before publishing. See `JOBS.md` for finite refresh barriers, local scheduling and deployment.

Revert preserves measured numeric/date ranges as stale cache instead of making every field
pending. It clears categorical previews and completeness, and discards obsolete or ephemeral
discovery caches. The restored registry still controls interpretation: incompatible measured
types cannot supply bounds. Media types use a complete exact query when their cache is missing
or incomplete, so ingestion's bounded cache cannot hide a media type.

API `declared_type` uses the canonical vocabulary. The existing `type` property remains
the presentation vocabulary (`enum`, `array`, `filepath`, overlay controls, etc.), keeping
SDK/UI compatibility without treating a widget name as a storage type.

## Saved versions

Legacy aliases normalize to the canonical vocabulary. Only registration owns types.

Snapshot comparison normalizes aliases and excludes system definitions. Legacy snapshots
did not record every implicit metadata definition, so comparison tolerates unconfigured
implicit definitions absent from those snapshots; sample hashes still detect data changes.
New snapshots mark the registry contract and compare their complete declared field sets.
Revert restores saved field semantics and retains definitions needed by samples outside
the reverted version. Pre-registry snapshots discover missing definitions during restore,
not during migration or an interactive schema read.

Indexes are **not part of a version** (by design). New snapshots omit
`is_promoted`; legacy snapshot flags are ignored by comparison and restore and omitted
from the version-detail response. Anywhere indexing is shown, including a saved-version
view, it comes from current `dataset_fields.is_promoted`, never historical snapshot data.
Creating or dropping an index does not dirty a version. Revert neither rebuilds nor drops
physical indexes and retains the current promotion flag for restored field definitions.

## Local workers are first class

The local launch command (`scripts/run_backend.sh`) starts the server maintenance worker
alongside the API, with managed shutdown and visible worker health. No other computer is
required. The co-located runner also claims compute jobs, such as map re-fits. Server and
compute are placement/capability boundaries, not a requirement for separate machines or
separate queues. A hosted profile can leave heavy compute disabled; remote workers remain
optional and are designed to use the same handlers, leases, checkpoints, and central artifacts
as local compute.

## Tests

The PostgreSQL suites need `FRIDGE_TEST_POSTGRES_DSN` pointing at a disposable database; each
test uses isolated schemas. Run lock-sensitive suites against their own database, because
advisory locks are database-wide even when sample tables live in separate schemas.

Backend (`backend/tests/`):

- `test_field_registry.py`: first-write type inference; registration is the only type authority.
- `test_field_registry_postgres.py`: missing statistics, mixed/unknown types, skipped duplicates,
  write rollback, concurrent PATCH, quality outputs, native-column isolation, embeddings queries,
  current-version stability, restore, legacy migration and bounded migration locks.
- `test_field_registry_integrity.py`: rename, clone, clear and promote guard rails.
- `test_field_type_sdk.py`: SDK type correction without rewriting sample values.
- `test_registry_review_completeness.py`: incremental merges keep valid completeness, transactional
  invalidation, bounded value lists and exact numeric enum representations.
- `test_registry_review_contracts.py`: dataset-owned native arrays (422 before writing), canonical
  derived schemas and truthful media-type completeness.
- `test_registry_review_coverage.py`: coverage presence filters compose with native-array and
  JSON-list filters.
- `test_registry_review_lifecycle.py`: shared/exclusive lifecycle gate, implicit-field pruning,
  concurrent clear/rename/PATCH/ingest, and no index DDL on restore.
- `test_registry_review_scale.py`: exact categorical selection on a 111,000-row fixture.
- `test_registry_review_type_correction.py`: explicit type correction, and full measurement after
  restore or edits to existing rows.
- `test_registry_review_bulk.py`: one bound patch for uniform bulk updates.
- `test_registry_review_scripts.py`: direct-SQL maintenance tools follow the same write contract.
- `test_registry_rereview_lifecycle.py`: try-only exclusive gates, incomplete cold reads,
  native-array re-registration, replacement-ingest pruning and derived-cache invalidation.
- `test_registry_rereview_refresh.py`: private measurement then validated publication, and bulk
  writers pre-locking targets in sample-ID order.
- `test_registry_rereview_revert.py`: revert keeps measured ranges and invalidates previews.
- `test_registry_rereview_sdk.py`: a busy 409 raises instead of reading as a duplicate result.
- `test_registry_followups_postgres.py`: publication under concurrent writes, revert previews,
  overlay discovery and refresh refusal reasons.

Browser (`frontend/e2e/`):

- `field-registry.spec.ts`: real ingest, PATCH and schema reads, and searching past tag previews.
- `registry-review.spec.ts`: type correction, picker request lifecycle (debounce, aborts, filter
  changes), color-by categories versus numeric gradients, and media-type completeness.
- `sidebar-contains.spec.ts`: contains terms combined with exact selections.
