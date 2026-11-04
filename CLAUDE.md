# Freedom Ridge — project instructions for Claude

## Map panel — READ BEFORE TOUCHING

The map data path (`frontend/components/map-panel.tsx` and the `map_points` endpoint in
`backend/routers/datasets.py`) has a written contract that has been regressed many
times. **Before changing anything in the map, read [docs/MAP_REQUIREMENTS.md](docs/MAP_REQUIREMENTS.md)
and verify every requirement still holds after your change.** The most-broken ones:

1. **Auto-show ALL individual points at zoom ≥ `_MAP_POINT_ZOOM` (12)** — the old
   `clusterMaxZoom`. Enforced server-side. Never remove it.
2. **Per-cell hybrid below that zoom**: cell with ≥ `_MAP_CLUSTER_MIN_POINTS` (100) →
   one bubble; sparser cell → individual points.
3. **A bubble always represents ≥ 100 samples** — no "1"/"12" bubbles, in any path.
   Overflow coarsens the grid; it never drops the floor or truncates points.
4. **View everything**: never silently cap/truncate visible data; signal limits and
   keep the full data reachable.
5. **Selecting samples (grid checkboxes) fits the map to them** — 1 → fly in close,
   N → fitBounds; live as selection changes. (Distinct from: no auto-zoom when a drawn
   polygon is deleted.)
6. **"Zoom to Data" must work after clearing filters**; map resize is the manual
   "Resize Map" button (no auto ResizeObserver).

See the doc for the full, authoritative list and the tunables.

## Python formatting — one argument per line when wrapped

The Python code (`backend/`, `python/`, `scripts/`) is ruff-formatted and lint-clean
with **zero** rule ignores beyond `E203`/`W503` (formatter-compat) and `__init__.py`
`F401` (re-exports). The CI `lint` job is blocking: it runs `ruff check`,
`ruff format --check`, and `flake8` (all pinned).

**Formatting rule — enforced, don't fight it:** a function call or definition stays on
one line if it fits in 120 chars (one-liners with several args are fine). When it must
wrap for length, it is exploded **one argument per line** with a trailing comma — never
the "several args sharing a wrapped line" layout. This is enforced by ruff's `COM812`
(adds the magic trailing comma) plus the formatter (keeps it exploded) plus
`ruff format --check` in CI.

After editing any Python file, run from the repo root:

```
ruff format backend/ python/ scripts/      # apply formatting (incl. one-per-line)
ruff check --fix backend/ python/ scripts/ # imports, trailing commas, etc.
```

Then `ruff check`, `ruff format --check`, and `flake8 backend/ python/ scripts/` must all
be clean before committing. (mypy is gated separately by the `quality` job; keep
`mypy backend/ python/fridge/` clean too.)
