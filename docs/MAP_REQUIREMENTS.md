# Map panel requirements — DO NOT BREAK THESE

These are hard requirements for the map (`frontend/components/map-panel.tsx` and the
backend `map_points` endpoint in `backend/routers/datasets.py`). They were each added
deliberately, in response to explicit user feedback, and have been *repeatedly*
regressed by well-meaning refactors. **Before changing anything in the map data path,
re-read this file and verify every item below still holds afterward.**

Key code:
- Backend: `_get_dataset_map_points_impl`, constants `_MAP_POINT_ZOOM`,
  `_MAP_CLUSTER_MIN_POINTS`, `_cluster_cell_deg` in `backend/routers/datasets.py`.
- Frontend: `frontend/components/map-panel.tsx`, `getMapPoints` in `frontend/lib/api.ts`.

---

## 0. Core principle — VIEW EVERYTHING

Never silently cap, truncate, or hide user-visible data to make the map fast. The full
dataset must always be *reachable*. Achieve performance with server-side clustering,
viewport filtering (GiST bbox), debounce, and virtualization — **never** by dropping
points the user can't get back. Any limit must be reachable/escapable and signalled to
the user (e.g. a "zoom in" prompt), never a silent cut. It is OK for the view to be
*bounded-compute* (cluster bubbles) as long as every point is represented and reachable.

## 1. Auto-show ALL individual points past a zoom threshold  ← MOST-BROKEN

At **zoom ≥ `_MAP_POINT_ZOOM` (currently 12)** the map renders **every individual
point automatically** — the equivalent of mapbox's old `clusterMaxZoom`. The user does
NOT have to click "De-cluster view" to see points when zoomed in. This is enforced
**server-side** (`auto_points = zoom >= _MAP_POINT_ZOOM`) so a frontend change can't
silently kill it.

History: introduced deliberately, then dropped by the hybrid rewrite, then restored. Do not
remove it again.

## 2. Per-cell hybrid clustering (below the zoom threshold)

Below `_MAP_POINT_ZOOM`, each grid cell is decided independently:
- cell with **≥ `_MAP_CLUSTER_MIN_POINTS` (100)** points → **one cluster bubble**.
- sparser cell → its **individual points**.

So sparse areas show real points at *any* zoom (you don't have to reach point-view to
see a handful of points in an empty region). A single response is a **mix** of bubbles
and points.

**A bubble takes in the points drawn under it.** A bubble sits at its own points' average
position and is 52–84 px in radius, while a cell is 128 px wide, so it spills into neighbouring
cells. Their loose points used to be drawn on top of it, as if they were part of the cluster but
left out of its count. A loose point within a bubble's on-screen radius at the request's zoom now
counts in the nearest such bubble instead of being drawn. Points outside every bubble stay
individual, and every sample is still represented. The server sends each bubble's `radius`
(`_map_bubble_radius_px`) and the map draws exactly that, so the bubble absorbs exactly the
points it covers. This is done on the cells after the single GROUP BY, looking only at a dense
cell's 8 neighbours. Timed on 3.75M synthetic samples at zooms 3–11.5, it made no measurable
difference. Tests: `backend/tests/test_map_clusters.py`.

## 3. A cluster bubble ALWAYS represents ≥ `_MAP_CLUSTER_MIN_POINTS` samples

No "1" / "12" bubbles, ever, in any code path. Every clustering path uses the one
per-cell rule (`_hybrid_items`). There is no separate "cluster everything with no
minimum" path. When too many loose points to ship, **coarsen the grid** (bigger cells
absorb more points into bubbles) — never drop the floor.

## 4. No silent cap on points

If the individual-point count for a viewport exceeds the ship cap (`POINT_CAP`, 65000),
do **not** truncate the list. Fall back to the clustered hybrid (every point still
represented as a ≥100 bubble), set `capped: true`, and let the UI prompt the user to
zoom in. The user must never be shown a partial set as if it were complete.

## 5. Cluster count labels — 3 significant figures

Counts on bubbles are abbreviated to ~3 significant figures: `218`, `218k`, `1.51M`.
NOT `217.736k`. NOT `2M` when it's actually `1.51M`. (See `clusterCountLayer`.)

## 6. De-cluster / Cluster toggle is state-aware

A button forces individual points. Its label reflects what's on screen: **"De-cluster
view"** when clusters are showing, **"Cluster"** when points are showing. At
zoom ≥ `_MAP_POINT_ZOOM` points are forced automatically, so the toggle is effectively
inert there — that is expected, not a bug.

## 7. "Zoom to Data" works — including after clearing filters

The "Zoom to Data" button fits the viewport to the **full data extent for the current
filters**. It must work after global filters are cleared (fetch the real extent; don't
rely on whatever points happen to be loaded). Fit to the TRUE min/max lon/lat
(`/map_extent`), NOT the bbox of coarse cluster centroids — those collapse to a single
point for a small/polygon selection and make it over-zoom and miss most of the data.

## 8. NO auto-zoom on POLYGON delete

Deleting a **drawn polygon / lasso** selection must **not** change the zoom or recenter
the map. (User was explicit: "I do not want any auto zooming when deleting the
polygon.") Note this is about the map's drawn polygon — it does NOT override #12
(selecting samples in the grid DOES fit the map). Drawing a polygon may fit to that
polygon; deleting it must not move the map.

## 9. Loading indicator (initial AND background loads)

Show a spinner / loading state while map data loads. Never a blank map with no feedback
during the initial or post-filter load. **Also** show feedback for slow *background*
loads over already-visible data — clicking a big cluster (re-clusters finer, ~3s for a
whole-US bubble) or "Zoom to Data" must not look inert. Drive a subtle indicator off
`isFetching` + the zoom-to-data fetch, shown after a short delay (~150ms) so fast
zoomed-in loads (~100ms) don't flicker, and hidden the MOMENT fresh data renders
(geojson changes) rather than when the network settles — so it doesn't linger after the
new data is already visible.

## 10. Map resize is MANUAL

Panel-size changes are handled by the manual **"Resize Map"** button (calls
`map.resize()`). Do **not** add a container `ResizeObserver` / auto-resize to the map —
it has caused instability and was explicitly declined. (Re-fitting on browser *window*
resize is fine.)

## 11. Performance / scale

Server-side clustering must handle the full dataset (millions of located samples) within a
bounded payload. It's acceptable for a zoomed-out / whole-dataset view to take a couple
seconds; it is **not** acceptable to cap the view to make it fast (see #0).

The clustered query groups on the stored `samples.lon`/`lat` columns (schema.sql,
trigger-maintained from `location` — never stale) so the whole-world view runs as an
**index-only scan** over `idx_samples_lonlat_cover` (~750ms vs the old ~2.6s geometry
scan). Two things this depends on — don't undo them:
- `_hybrid_items` must group with `round(lon/cell)` / `round(lat/cell)` (pure arithmetic
  on the columns), NOT `ST_SnapToGrid(location...)` — referencing `location` drops the
  index-only scan back to a full heap scan. `round()` matches the grid `ST_SnapToGrid`
  used (verified bit-identical totals).
- The index-only scan only skips the heap for all-visible pages, so the per-table
  autovacuum setting (insert threshold 50000, set in schema.sql) must stay — without
  it heavy ingestion lets the visibility map go stale and the query slows (heap fetches).
  This affects speed only, never which samples appear (see the lon/lat columns are
  trigger-maintained, so a new sample is on the map the instant it commits).

## 12. Selecting samples fits the map to them  ← was undocumented, got dropped

When samples are selected (grid checkboxes → `selectedIds`), the map zooms to show
them: **one sample → fly in close** (zoom ~18) on it; **multiple → `fitBounds` so all
selected are in view**. It updates live as the selection changes ("2 selected samples
should be in view"). Selected points are also highlighted (#3 color), but highlight is
NOT enough — the map must MOVE to the selection.

Under server-side clustering the full point set is not on the client, so the selected
samples' locations are fetched on demand (`getMapPoints` with an `id in [...]` filter,
`points: true`). Fires only when the selection actually changes; skipped for very large
selections (lasso-scale) where auto-fitting would just zoom out to nothing. This is
distinct from #8 (that is about deleting a drawn polygon).

History: existed pre-regression, silently died in the
server-clustering rewrite because it read coordinates from the old client-side point
set. Restored by fetching locations. **Do not remove it again.**

## 13. Viewport tiling + reuse of seen views

Don't refetch on every pixel of pan, and don't reload a view you've already seen.
- **Tiling:** snap the fetched bbox to a fixed per-zoom world grid (`tileBbox`) so panning
  within a tile produces the same request and hits the cache. The grid is zoom-only, so
  tiles recur as you pan, and the whole-world view snaps to one stable tile. Tiling is
  skipped at points zooms (>= `MAP_POINT_ZOOM`) so the fetch region isn't enlarged past
  the point cap (#1/#4).
- **Retention:** the map query keeps a long `gcTime` so seen tiles — especially the
  whole-dataset "all samples" view — stay cached for the session and return INSTANTLY
  (react-query re-shows cached data immediately, then background-refreshes after
  `staleTime`). The cache key includes the FULL filter, so it can never serve one
  filter's data under another. Only staleness vector: data changed underneath — bounded
  by `staleTime` background refresh and a page reload.
This is client-side and per-session (in-memory) — NOT a persisted/cross-session cache,
and NOT a server-side precompute. Don't add cross-session persistence without an
explicit invalidation story (that's the staleness footgun to avoid).

---

### Tunables
- `_MAP_POINT_ZOOM` (default 12): zoom at/after which all points show automatically.
- `_MAP_CLUSTER_MIN_POINTS` (default 100): per-cell threshold for bubble-vs-points.
- `POINT_CAP` (default 65000): max individual points shipped before falling back to
  coarsened clusters.
