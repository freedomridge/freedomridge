# Freedom Ridge Object / Patch View

> 2026-06-16. **Objects are a lens on the image dataset, not a separate dataset.**
> See the [Open Images example](../examples/open_images/README.md) for an importer using
> parent images and object samples in the same dataset.

## The model

Each detection becomes a real `samples` row **in the same `dataset_id` as its parent image**, flagged `media_type='object'`:

| column | value |
|---|---|
| `dataset_id` | **same as the parent image** — this is what makes it "one dataset" |
| `media_type` | `'object'` — the only new convention |
| `filepath` | the **parent image's** filepath (`s3://…`); the grid crops it on the fly |
| `metadata.parent_id` | the parent image's `samples.id` |
| `metadata.crop_bbox` | the box `[x,y,w,h]` (≤2 ⇒ normalized) — the `?crop=meta` endpoint reads this |
| `metadata.label` / `.confidence` | denormalized scalars |
| `location` | inherited from the parent (shares the parent's map point) |

The per-object CLIP vector lives in `embeddings` as `(object_row.sample_id, field_name='clip_obj')` — a **distinct field** (not `media_type`-filtered `clip`).

**Why this works:** Freedom Ridge's grid, viz-rank scatter, concepts, faceting, map, and the embedding lasso are all keyed on `(sample_id, field_name)` and **dataset-scoped**, so object rows that are *real sample rows* flow through them unchanged — no parallel tables, no parallel viz/kNN/concept code.

**Why a distinct `clip_obj` field (not a `media_type` filter on `clip`):** two systems are NOT dataset-scoped and force it —
- the **HNSW kNN index** is partial `WHERE field_name='…'` on the shared `embeddings` table with no `dataset_id` predicate, so a shared `clip` field would mix image+object graphs;
- the **default (unfiltered) scatter** reads `embedding_viz_rank` by `(dataset_id, field_name)` only.

With a distinct field, the object scatter is just `field='clip_obj'` — its own rank set, its own disjoint HNSW index, **zero backend change**. (The earlier draft's "add `media_type` to the `embedding_viz_rank` WHERE" was wrong: that table has no `media_type` column, and the filtered scatter path already routes `media_type` through its `_fids` temp table.)

The grain switch is therefore **two derived effects of one toggle**: a `media_type='object'` filter for the grid/facets/map, and an `embeddingField: 'clip' → 'clip_obj'` swap for the scatter.

## The 4 bidirectional behaviors

1. **Select object → open its image** — read `metadata.parent_id`, open the parent modal (existing `GET sample`); highlight the box from `metadata.crop_bbox`. Frontend only.
2. **Object similarity → parent images** — `POST /search` on `clip_obj` returns nearest object rows; **client-side** dedup to first-occurrence `metadata.parent_id`; feed to the existing `order_by_ids` pivot, grid flips to image grain via an explicit "Showing parent images | Show matched objects" header.
3. **Select image(s) → its objects** — a **pivot action** (not a selection): clear `selectedIds`, set `grainMode='object'`, write `{field:'parent_id', operator:'in', value:[ids]}` (plain field, not dotted) into a dedicated `grainPivotFilter` slot; breadcrumb chip restores image grain.
4. **Selection sync (scatter ↔ grid)** — free: object rows have real `sample.id`s, so the existing sample-id selection store works unchanged. Map cross-highlight derives `distinct(parent_id)` of selected objects.

## Key files
- `backend/db/schema.sql` — migration (the 2 indexes).
- `backend/main.py` — existing `?crop=meta` endpoint (reads `metadata.crop_bbox`).
- `backend/routers/embeddings.py` — **no change** (distinct `clip_obj`).
- `backend/routers/datasets.py` — plain `parent_id in` handler, `order_by_ids`, `embedding_polygon` `=`-branch.
- `backend/routers/search.py` — object kNN via `clip_obj` (dataset-scoped).
- `frontend/lib/media-utils.ts` — `getSampleMediaUrl` emits `crop`/`w`.
- `frontend/components/grid-panel.tsx` — Image|Object grain toggle.
- `frontend/components/dataset-grid.tsx` — `getImageSrc` uses `crop=meta` for object cells.
- `frontend/contexts/FilterContext.tsx` — `grainMode` + `grainPivotFilter`.
- `frontend/components/embeddings-panel.tsx` — scatter field swap to `clip_obj`.
- `frontend/app/datasets/[id]/page.tsx` — behaviors (1)/(2)/(3) wiring.
- `scripts/copy_objects.py` — re-homes object rows from a sibling objects dataset (non-destructive).
