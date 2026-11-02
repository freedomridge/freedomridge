# Freedom Ridge Design Decisions

Decisions made during development, with reasoning. Reference this before proposing changes.

## Core Principle: SDK First

The Python SDK is a primary product, not an afterthought. Every feature must have SDK methods. If a bulk operation is too slow through the SDK, the SDK is broken and must be fixed — never bypass it with raw SQL or direct database access. Integration scripts, migration tools, and user workflows all go through the SDK.

## Data Model

### Array Fields as Real Columns, Not JSONB
**Decision:** Tag-like array fields (tags, inferred_tags, etc.) are stored as native PostgreSQL `TEXT[]` columns with GIN indexes, NOT inside the JSONB `metadata` column.

**Why:** Native arrays avoid repeated JSON extraction and support PostgreSQL array operators directly. Appropriate GIN indexes accelerate containment and overlap filters.

**How:** `sample_array_columns` registry tracks which TEXT[] columns exist. New columns added via `ALTER TABLE ADD COLUMN ... DEFAULT '{}'` (instant in PostgreSQL 11+). `CREATE INDEX CONCURRENTLY` for GIN index.

**Constraint:** Max 20 array columns. Column names validated with `^[a-z][a-z0-9_]{0,62}$` regex + reserved name blocklist.

### Metadata in JSONB for Flexible Key-Value Data
**Decision:** Non-array metadata stays in JSONB. Array fields get promoted to real columns.

**Why:** JSONB is flexible (any field, any type, no migrations) but slow for indexed operations. The hybrid approach: JSONB for flexibility, real columns for performance-critical array fields.

### Hashes in Separate Table
**Decision:** Image hashes stored in `sample_hashes` table with `bit varying` column, not in metadata.

**Why:** Hamming distance queries require bitwise XOR operations (`bit_count(h1 # h2)`) which can't be done on JSONB values. Separate table allows proper bit indexing.

## Query Architecture

### Slim Mode for SDK
**Decision:** `/samples/query` accepts `slim=true` for SDK bulk operations. Only fetches requested columns, skips Pydantic validation, skips COUNT(*).

**Why:** The UI needs full sample data, while SDK bulk operations often need only a few fields. Selecting those columns reduces database reads, serialization and transfer work.

**Safety:** UI never sends `slim`. Both paths return `{items, total}`. Frontend is completely unaffected.

### Prepared Statement Loops for Bulk Lookups
**Decision:** `/samples/lookup` uses server-side prepared statement loops instead of `= ANY($1::text[])`.

**Why:** A large `ANY()` expression can lead the planner to choose a sequential scan. Individual prepared lookups keep each request selective and allow reuse of the lookup plan.

### Grid-Stratified Sampling for Embeddings
**Decision:** The embeddings panel uses a 200x200 grid with adaptive `per_cell` (max_points / occupied_cells), not random sampling.

**Why:** Random sampling over-represents dense clusters and misses sparse outlier regions. Grid stratification ensures spatial coverage — every occupied area of the embedding space gets represented.

### Scatter Coordinates Live in Two Places — the Database Keeps Them Equal
**Decision:** `embedding_viz_rank` (narrow: sample, rank, x, y) is the scatter's read table; `embeddings.x/y`
is what lookup, lasso and the `embedding_polygon` filter read. A statement-level trigger on `embeddings`
mirrors every x/y change into the rank table in the same transaction, so the two can never
disagree at a commit boundary. Coordinate writers do NOT have to remember a refresh call.

**Why:** The rank table exists because the even-coverage sample must be an index range scan, not a window
sort over the wide `embeddings` heap. Keeping it in sync by convention failed: three
writers rewrote `embeddings.x/y`, the separate re-rank call crashed, and the production CLIP scatter drew
July's layout while every lasso was evaluated against August's (100% mismatch, no error).

**Staleness:** only the even-coverage ORDER can go stale (a re-projection moves points between grid cells).
`embedding_viz_meta.rank_stale` marks it; `/embeddings/query` reports it; the UI shows "coverage stale";
a full `recompute_viz_rank(dataset, field, TRUE)` clears it. Coordinates themselves are always current.

### Sample Percentage (Not Fixed Point Count)
**Decision:** Embeddings sampling uses a percentage (5%-100%) not a fixed number.

**Why:** Fixed numbers (50K, 100K) are meaningless without knowing dataset size. 30% of a 60K dataset is 18K. 30% of 2M is 600K. Percentage scales to any dataset.

**Persistence:** Saved per-user per-dataset in localStorage. Only persisted after successful canvas render (prevents crash loops). "All" (100%) is session-only.

## Filter Architecture

### Global Filters vs Staged Filters
- **globalFilters:** View stages — applied to map, embeddings, duplicates panels
- **stagedFilters:** globalFilters + sidebar drafts — applied to the image grid only
- **Clear All:** Only clears view stages, preserves sidebar drafts and selections

### Array Field Filtering
All array fields (tags, inferred_tags, etc.) use identical PostgreSQL array operators:
- `=` → `$N = ANY(field)` (contains value)
- `!=` → `NOT ($N = ANY(field))`
- `in` → `field && $N::text[]` (overlap)
- `not in` → `NOT (field && $N::text[])`

No special-casing per field. The `_get_array_columns_sync()` set determines which fields get array treatment.

## Performance

### PostgreSQL Tuning
Default PostgreSQL is catastrophically slow for 1M+ row datasets. Critical settings:
- `shared_buffers`: 25% of RAM (8GB for 32GB machine)
- `work_mem`: RAM/256 (128MB for 32GB) — prevents GROUP BY/sort spill to disk
- `effective_cache_size`: 75% of RAM
- `random_page_cost`: 1.1 (data in RAM, prefer index scans)
- `max_parallel_workers_per_gather`: 4

Without tuning: 27s for hash stats. With tuning: 2-3s.

### React Query Caching
- `staleTime: 30_000` for data queries (grid, map, embeddings)
- `staleTime: 60_000` for metadata queries (dataset, schema, fields)
- `refetchOnWindowFocus: false` everywhere
- Exponential retry delay: 1s, 2s, 4s (capped 10s), max 3 retries

### Canvas Rendering
Embeddings panel uses batched `ctx.rect()` with color-bucketed coordinate arrays instead of per-point `ctx.arc()`. Single pass fills typed arrays, one `ctx.fill()` per color bucket. Handles 500K+ points.

## Versioning

### What Versioning Tracks
- Sample metadata (md5 hash)
- Sample tags
- Image hashes (md5 of concatenated hash values)
- Dynamic array fields (via `array_fields_snapshot` JSONB column)
- NOT: embeddings (recomputable, not user data)

### Crash-Safe Sample Percentage
The embeddings panel persists the sample percentage to localStorage only after the canvas renders successfully. If a percentage crashes the browser, the previous safe value remains.

## SDK Design

### iter_samples Behavior
- Default batch_size: 1000 (was 100)
- First batch: gets COUNT for progress bar
- Subsequent batches: `skip_count=True` (no redundant COUNT)
- With `selected_fields`: uses slim mode (17x faster)
- End of data detected by `len(items) < batch_size`

### Sample Field Routing
- Core fields (id, filepath, tags, metadata, etc.): direct attributes
- Known array fields: `sample.array_fields` dict
- Everything else: `sample.metadata` dict
- `__setattr__` routes based on `Sample._known_array_fields` class set
- `__getattr__` checks array_fields first, then metadata
