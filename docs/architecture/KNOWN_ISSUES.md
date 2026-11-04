# Known Issues & Gotchas

## PostgreSQL

### JSONB Expression Indexes Don't Work for Bulk Lookups
B-tree indexes on `metadata->>'field'` work for single-value lookups but PostgreSQL's planner chooses sequential scan for `= ANY()` with >50 values. The fix: join the values in as `unnest($2::text[])` (see the `/datasets/{id}/samples/lookup` endpoint).

### Default PostgreSQL Config is Catastrophic
Docker PostgreSQL defaults (4MB work_mem, 160MB shared_buffers) cause all GROUP BY and sort operations to spill to disk on datasets >100K rows. See `backend/db/postgresql.conf` for proper settings per RAM tier.

### `CREATE INDEX CONCURRENTLY` Can't Run in a Transaction
The `create_array_field` endpoint runs `CREATE INDEX CONCURRENTLY` which can't be inside a transaction block. If the index creation fails, the column exists but the index doesn't. Re-running the endpoint is safe (both use `IF NOT EXISTS`), but a failed concurrent build can leave an INVALID index under the same name, which `IF NOT EXISTS` then keeps: drop it (`DROP INDEX CONCURRENTLY idx_samples_<name>_gin`) before re-running.

## Frontend

### Duplicate map_points Queries on Page Load
The MapPanel fires its query twice on initial page load, likely due to the `dynamic()` import causing a mount/unmount/remount cycle. The `staleTime: 30_000` means the second query hits cache, so it's wasteful but not harmful.

### Browser Can Crash on Large Embeddings
Rendering 1M+ points on a Canvas can OOM the browser tab. The sample percentage system prevents this. The percentage is persisted only after a successful canvas render, and a load that crashed the tab is detected on the next open, which starts at 5% instead of crashing again.

### Native `<select>` Can Stop Working
Observed once where all `<select>` elements on the page became unclickable. Closing and reopening the browser tab fixed it. Cause unknown — possibly a browser rendering bug triggered by heavy Canvas operations.

## SDK

### `Sample._known_array_fields` is Class-Level
The set of known array fields is shared across all Sample instances. It's populated when a Dataset loads. If array fields are created after the client initializes, new fields won't be routed to `array_fields` dict until the Dataset is reloaded.

### `iter_samples` Progress Bar Needs First Batch
The progress bar total comes from the first batch's COUNT. If the first batch fails, there's no progress bar. Subsequent batches skip COUNT for performance.

### Legacy `/media/...` filepath handling (planned removal)
The SDK image loader still special-cases legacy `/media/s3/<key>` and `/media/<rel>` filepaths. Modern ingest writes canonical `s3://...` / root-relative local paths. Removing the legacy branches requires migrating any old datasets' stored `filepath`s first.

## Data Pipeline

### Versioning and Array Fields
Version snapshots DO capture dynamic array fields (user-created TEXT[] columns) in
`version_snapshots.array_fields_snapshot`, and `status`/`diff`/`revert` compare and
restore them. Comparison is snapshot-aware: a snapshot only knows about the array
columns that existed when it was taken, so a column ADDED LATER is ignored by the
diff (and left untouched by revert) rather than making every row appear "modified".
