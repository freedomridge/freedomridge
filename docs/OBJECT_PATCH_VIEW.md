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
| `metadata.parent_id` | the parent image's `samples.id`: what clients write and read |
| `parent_id` (a column) | the same id, kept by triggers from `metadata.parent_id` when that names an image (any non-object) of the same dataset, whichever of the two is written first; empty otherwise, and on every image. A direct write of it is overridden by that rule. |
| `metadata.crop_bbox` | the box `[x,y,w,h]` (≤2 ⇒ normalized) — the `?crop=meta` endpoint reads this |
| `metadata.label` / `.confidence` | denormalized scalars |
| `location` | inherited from the parent (shares the parent's map point) |

The per-object CLIP vector lives in `embeddings` as `(object_row.sample_id, field_name='clip_obj')` — a **distinct field** (not `media_type`-filtered `clip`).

**Deleting an image deletes its objects** (2.11.0, migration 0030), whatever deletes it: the column has a key,
`(dataset_id, parent_id)` → the image's `(dataset_id, id)`, `ON DELETE CASCADE`, so a delete in `psql` takes them too,
with their vectors, map points and hashes, and followers and statistics are told as for any delete. The app's own
deletes take them first, to count them:

- `DELETE /datasets/{id}/samples/{sample_id}` answers `objects_deleted`.
- A bulk delete's result counts the samples it targeted in `deleted_count` and the objects that went with their
  images in `objects_deleted`; job history says "N samples deleted, and M objects with their images." An object goes
  with its image even when the target leaves it out (a filter it doesn't match, `excluded_ids`).
- Restoring a version with exact membership removes the samples added since, an added image's objects with it; its
  `samples_removed` counts them. An object the version has whose image was added since stays, with no image.
- A dataset's deletion deletes the objects before their images, within each unit's budgets.

An object that names no image (its image is gone from a primary on 2.10.x or older, a junk value, an image of another
dataset) has an empty column and nothing deletes it on its own. Moving an image with objects to another dataset is
refused; nothing in the app moves samples. An object written while its image is being deleted is refused (409,
`Retry-After`; the SDK retries), and written again it has no image. A follower's mirror follows its primary: before
its sync deletes an image whose objects the primary keeps (a primary on 2.10.x or older keeps them; a 2.11 one deletes
them, or re-points them), it clears their `metadata.parent_id`, so they stay, with their sample ids and their
`@local.` fields.

**Objects with no image** (`backend/unlinked_objects.py`). Ingest accepts an object whose `parent_id` names no image
of its dataset: its image may come in a later request, and then links it. What stays unlinked is found and, on
request, deleted. Each object with no image is of one kind:

| kind | its `metadata.parent_id` |
|---|---|
| `image_gone` | a sample id no sample has, in any dataset (its image was deleted, or never came) |
| `other_dataset` | names a sample of another dataset |
| `object` | names another object of its dataset |
| `not_a_sample_id` | isn't a sample id (text, a negative number, a fraction) |
| `no_parent_id` | is missing, or null |

- `POST /datasets/{id}/objects/unlinked` reports them by kind (`total`, `kinds`, and the `filter` that shows them).
  For a user who may edit the dataset it first links the objects whose image is there but not linked yet
  (`linked`); anyone else sees those as `linkable`. `idx_samples_unlinked_objects` holds only these objects, so the
  count is free. The SDK: `dataset.unlinked_objects()` and `dataset.unlinked_objects_view(kinds)`.
- The filter is `{"field": "parent_id", "operator": "unlinked_object", "value": [kinds]}` (all kinds when empty).
  It has no form in the query text or the filter builder: its chip in the app is read-only. It asks about live
  data, so a version's query refuses it (400), as it does the cross-grain operators.
- `POST /datasets/{id}/objects/unlinked/delete` (admin) with `{"kinds": [...]}`, by default only `image_gone`,
  links first, then deletes them as a bulk delete of that filter: resumable units, logged for followers and
  statistics like any delete, each unit deleting only what still matches (an object whose image arrived meanwhile
  stays). The answer names the kinds and the `expected` count; the job's result has `deleted_count`. Never automatic.
  The SDK: `dataset.delete_unlinked_objects(kinds=('image_gone',))`.
- On a mirror the report works (its sync keeps the links, so nothing is linked there), and the delete is refused:
  its objects change only through its sync, so they go when the primary deletes them.
- In the app, the Objects grid's header says "⚠ N objects have no image" when there are any (the tooltip counts each
  kind); clicking it shows them as a view. An admin, on a dataset that isn't a mirror, also gets **Delete…** for the
  objects whose image is gone: the dialog says how many go and which objects with no image stay, and Job history
  shows the result.

**The `parent_id` field** can't be renamed or cleared, and no field can be renamed or copied over it, on a dataset
that has object samples: each object would lose its image at once (409, saying why). Writing one sample's
`parent_id` (PATCH, a bulk set) re-points that object and stays allowed. The filter sidebar doesn't list
`parent_id` and computes nothing for it; it stays in the schema, so the stage editor's field search, the query
text, links and the SDK filter on it, and the sample modal shows it with the rest of the metadata.

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
- `backend/db/schema.sql` — the indexes, and the `parent_id` column with the triggers that keep it (migration 0029)
  and the key that deletes an image's objects with it (0030).
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
