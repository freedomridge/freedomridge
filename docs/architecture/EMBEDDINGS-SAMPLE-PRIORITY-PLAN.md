# Plan: fast even-coverage embeddings scatter — precomputed sample priority + binary transfer

Status: ✅ IMPLEMENTED 2026-06-11 (migration 030 + backend/SDK/daemon/frontend). Verified on
a 2.4M-sample dataset that ingests continuously.

## Implementation notes (what shipped vs. this plan)

**Key deviation — the rank lives in a DEDICATED NARROW TABLE, not a column on `embeddings`.**
The plan's `embeddings.viz_cell_rank` column was built first and measured: a full re-rank UPDATE
on 2.4M rows took **>7.5 min** (cancelled). `embeddings` is wide (a TOASTed 2KB `vector`) and
carries 5 indexes; updating an indexed column rewrites every row across all indexes (no HOT),
and re-fits are periodic for this streaming dataset. The sort itself was only 1.6s — the cost was
heap+index churn. Switched to `embedding_viz_rank(dataset_id, field_name, sample_id, rank, x, y)`
rebuilt via DELETE+INSERT (one index, x/y denormalized so the scatter needs no join):
- full re-rank: **52 s** (was >7.5 min)
- unfiltered 30% scatter query: **~0.19 s index-only scan** (was 7.6 s window-sort)
- filtered 30% sampled query: **~0.37 s** (path total ~2.8s incl. the existing _fids build+count; was ~12s)
- incremental (rank only new points, anti-join): **~1.25 s** once per projection run

**Other choices made at implementation:**
- `recompute_viz_rank(dataset, field, full)` SQL function is the single source of truth, called by
  BOTH projection paths: the SDK (`compute_visualization`→full, `project_pending`→incremental, via
  the new `POST /embeddings/recompute-viz-rank`) and the direct-DB daemon `umap_project_job.py`
  (calls the function directly, no API auth).
- Incremental rank = `floor(random()*avg_cell_size)` (avg = total/occupied) — inclusion prob ≈
  budget/total with no per-cell scan; density-weighted for new points only, the next full reduce
  rebalances. (The plan's per-cell-append needed a per-cell scan; this is cheaper and good enough.)
- Filtered fast path reuses the existing `_fids` temp table (joins ranks to it); `per_cell` is
  scaled up by `global_total/filtered_total` to fill the budget.
- Patch-scale scatter (LIKE across r/c field_names) keeps the grid window-sort fallback — no single
  meta row. Fallback also covers any field not yet backfilled.
- Binary transfer (Part 2): `format=bin` on `/query`, base coords only (no include_fields/patch);
  4-byte LE header-len + space-padded JSON header + int32 ids / float32 x / float32 y. Frontend
  branches on response content-type so patch/include_fields (which stay JSON) still work. Format
  locked by `backend/tests/test_embeddings_binary.py`.
- Coords stored float4 (REAL) in the rank table — viz doesn't need float8; halves the row width.

Backfill existing data: `DATABASE_URL=... python3 scripts/backfill_viz_rank.py`. Open follow-up
lever if the incremental anti-join bites: pass projected sample_ids
to the function instead of the 2.4M-vs-2.4M anti-join.

---

(Original plan below — Part 1 describes the column approach that was superseded by the narrow table;
the even-coverage math, budget→per_cell mapping, and Part 2 are as implemented.)

Status: PLANNED (not started). Authored 2026-06-11.

## Problem

The embeddings scatter panel samples points before sending them to the client. The sample
exists to bound the **client** cost — transferring + `JSON.parse`-ing + rendering + holding
millions of points can OOM/lag the browser. So the point cap must stay.

But the *current* sampler is implemented as a query-time grid window-sort and is pathologically
slow. Measured on a 2.4M-sample dataset (a CLIP ViT-B/32 field), server
query time only:

| approach (30% unless noted) | server query | notes |
|---|---|---|
| **current grid sampler** `ROW_NUMBER() OVER (PARTITION BY cell ORDER BY random())` | **7.6 s** | + a separate `COUNT(DISTINCT cell)` "occupied" query ~1.7 s |
| uniform random `WHERE random() < .3` | 3.3 s | density-weighted (loses sparse clusters) |
| TABLESAMPLE BERNOULLI(30) | 2.3 s | density-weighted |
| fetch ALL (100%) | 1.6 s | server only; real load adds ~46 MB+ transfer + JSON.parse + 2.4M render → much slower end-to-end, and can OOM the client |

Conclusion: the grid sampler is the worst option (slowest AND fewer points). "Just fetch all"
is not viable because of the client cost. We want: **keep the point cap, keep EVEN spatial
coverage (so sparse/outlier clusters stay visible — the detail that matters for curation),
make it fast, and keep it correct under live ingestion** (points are projected continuously).

Code today: `backend/routers/embeddings.py` → `_run_grid_sampled_query()` (the slow path) and
`_query_embeddings_no_filter` / `_query_embeddings_with_filter`; endpoint `POST /datasets/{id}/embeddings/query`.
Coords are produced by `python/fridge/core/dataset.py` `compute_visualization()` (full reduce)
and `project_pending()` (incremental), driven by `python/fridge/umap_worker.py`.

---

## Part 1 — Precompute an even-coverage "sample priority" at projection time

### Core idea
When a point's UMAP coords (x,y) are set, also assign it an indexed **within-cell rank**.
The scatter query then becomes `WHERE viz_cell_rank < per_cell` — an index range scan,
sub-second — instead of a full window-sort. Even coverage is preserved because each grid cell
contributes its first `per_cell` points.

### Why within-cell rank gives even coverage
- Lay a G×G grid (e.g. 200×200) over the coord extent.
- `cell(p) = (floor((x-xmin)/xrange*G), floor((y-ymin)/yrange*G))`, clamped to `[0,G-1]`.
- Within each cell, randomly shuffle the points and number them `0,1,2,…` → that integer is
  `viz_cell_rank`.
- `WHERE viz_cell_rank < r` returns the first `r` (random) points of EVERY occupied cell → an
  even spatial spread of ≈ `r × occupied_cells` points. Increasing `r` adds detail uniformly.

### Mapping a point budget → `per_cell`
`per_cell = max(1, ceil(budget / occupied_cells))`, then `... LIMIT budget` as a safety cap.
So we must store `occupied_cells` (and the grid `extent`) per (dataset, field).

### Schema
- `ALTER TABLE embeddings ADD COLUMN viz_cell_rank INTEGER;` (NULL until coords/rank computed).
- Index: `CREATE INDEX idx_embeddings_viz_rank ON embeddings (field_name, viz_cell_rank, sample_id) WHERE x IS NOT NULL;`
  (covers the scatter's `field_name = ? AND viz_cell_rank < ?` then join to samples on sample_id).
- New tiny table for the budget→per_cell mapping:
  `embedding_viz_meta(dataset_id, field_name, grid INT, xmin/xmax/ymin/ymax DOUBLE PRECISION, occupied_cells INT, updated_at, PRIMARY KEY(dataset_id, field_name))`.
- Mirror ALL of the above into `backend/db/schema.sql` (test_schema_parity enforces it) AND a
  numbered migration (e.g. `030_embeddings_viz_rank.sql`), idempotent (`IF NOT EXISTS`).
- Note: `field_name` is shared across datasets, so `viz_cell_rank < per_cell` matches low-rank
  points from every dataset using that field; the join to `samples` (dataset_id) filters them
  out. Fine when one dataset dominates; revisit if many large datasets share a field.

### Computing the rank
- **Full reduce** (`compute_visualization`, all points): after coords are computed, compute the
  extent, bucket points into cells, shuffle each cell, assign `viz_cell_rank`, bulk-write it,
  and upsert `embedding_viz_meta` (extent + occupied_cells). Cheapest to do in Python alongside
  the coords it already holds (the float32 array), then one bulk UPDATE (or write rank with the
  coords upload).
- **Incremental** (`project_pending`, only new points): for each new point compute its cell from
  the *stored* extent (clamp if it lands outside — happens when new data extends the manifold).
  Assign `viz_cell_rank = (current count of that cell) + within-batch offset`. Get current
  per-cell counts with one grouped query over the affected cells (bounded, cheap for a batch).
  Bump `occupied_cells` if new cells appear. A periodic full reduce (already a maintenance task)
  re-balances extent + ranks cleanly.

### Query change (`backend/routers/embeddings.py`)
Replace `_run_grid_sampled_query`'s window-sort path:
- `total <= cap` (or pct ≥ 100 and small): fetch all (existing behavior, fine).
- else: read `occupied_cells` from `embedding_viz_meta`; `per_cell = max(1, ceil(budget/occupied_cells))`;
  `SELECT x,y,sample_id FROM embeddings e JOIN samples s ON e.sample_id=s.id
   WHERE s.dataset_id=$1 AND e.field_name=$2 AND e.viz_cell_rank < $per_cell [AND <filters>]
   LIMIT $budget`. Index range scan → sub-second.
- Filtered case: the user filter narrows `samples` first; if the filtered count ≤ cap → fetch
  all; else apply the same `viz_cell_rank < per_cell` on the filtered set. `occupied_cells` is
  the global value (approximate for a tight filter) — acceptable, bounded by LIMIT; or recompute
  occupied for the filter (still far cheaper than the old window-sort).

### Backfill
One-time script (reuse the rank logic) to populate `viz_cell_rank` + `embedding_viz_meta` for
existing embeddings, per (dataset, field). Until backfilled, fall back to the old path or fetch-all.

### Verification
- `EXPLAIN ANALYZE` the new query on 2.4M → target sub-second (vs 7.6s).
- Visual diff: sparse/outlier clusters still appear vs the old grid sampler (even coverage held).
- Live ingestion: project a new batch → new points get a rank and show up in the sample.

---

## Part 2 — Binary coords transfer (independent; helps every point count)

### Idea
Return scatter coords as a packed binary payload instead of JSON → ~½ the bytes and near-zero
parse (no `JSON.parse` of a big object array).

### Backend (`/embeddings/query`)
- On `Accept: application/octet-stream` (or `?format=bin`), respond `application/octet-stream`:
  a tiny header (count + field list, e.g. as a JSON line or fixed preamble) followed by columnar
  binary: `x: float32[count]`, `y: float32[count]`, `sample_id: int32[count]` (+ extra numeric
  fields appended). Build with `numpy.tobytes()` / `struct`.
- Keep the JSON path as fallback (patch results, extra non-numeric fields, older clients).

### Frontend (`getEmbeddings` in `lib/api.ts` + `embeddings-panel.tsx`)
- Fetch `arrayBuffer()`; create `Float32Array` views for x/y and `Int32Array` for ids directly
  over the buffer — drop straight into the existing Float32Array scatter storage (the render path
  already uses Float32Arrays). No object array, no JSON.parse.

### Payload math
~12 bytes/point binary (x,y float32 + id int32) vs ~25–40 bytes JSON → ~2–3× smaller transfer +
eliminates the big-array parse. Matters more the higher the sample %.

---

## Rollout order
1. **Part 1** first — it removes the 7.6s server stall (the dominant cost for the 30% case).
2. **Part 2** after — independent; compounds the win, especially at higher %.

## Files to touch
- `backend/db/schema.sql` + `backend/db/migrations/030_*.sql` (column, index, viz_meta table)
- `backend/routers/embeddings.py` (`_run_grid_sampled_query` → rank query; binary response)
- `python/fridge/core/dataset.py` (`compute_visualization`, `project_pending` → compute/write rank + meta)
- `python/fridge/umap_worker.py` (drives projection; ensure rank/meta get written)
- backfill script under `scripts/`
- `frontend/lib/api.ts` + `frontend/components/embeddings-panel.tsx` (binary fetch/parse)

## Open questions to resolve at implementation
- Grid resolution G (200 today) vs the typical client budget — pick so `per_cell` is a useful integer.
- Shared field_name across datasets and the index (see Schema note) — fine for now; flag if multi-dataset-per-field becomes common.
- Whether to compute rank in the SDK (Python, has the coords in hand) or server-side after upload.
