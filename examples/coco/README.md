# COCO val2017, local and complete

`ingest.py` imports COCO validation images with every annotation from the three
official annotation files. By default it skips photos under a No-Derivatives
license, which leaves 3,340 of the 5,000 images (see below); `--licenses all`
imports all 5,000. The default `--limit` is 100. Images stay in place. Labels,
metadata and a normalized CLIP vector arrive together in each `add_samples` request.

## Perceptual hashes and duplicates

Imports include `dhash_8` and `phash_8` on image rows so the **Duplicates** panel
and **collapse duplicates** work for matching images. Object rows receive no hashes.
Use `--hash-algorithms dhash_8 phash_8` to choose algorithms or `--no-hashes` to omit
them; both flags also work with `examples/import_demo_data.py`.

Resume skips existing rows without rehashing. Backfill an older dataset from the
repository root using [the SDK recipe](../sdk/compute_hashes.py), with API credentials
and access to the media files:

```bash
python examples/sdk/compute_hashes.py coco-val2017 --algorithms dhash_8 phash_8
```

Backfill skips objects and images with all requested hashes, computing only missing
fields. See [shared hash options](../README.md#perceptual-hashes-and-duplicates).

## Source, licensing and attribution

Source: [COCO 2017](https://cocodataset.org/#download).
COCO annotations are distributed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Photographs retain their individual Flickr licenses; the annotation JSON's
`licenses` table is authoritative for each sample. This includes noncommercial,
no-derivatives, share-alike, public-domain and other categories. There is no
single photograph license for the entire dataset.

The app serves resized thumbnails, object crops and patch tiles, which are
derivatives. So `--licenses` defaults to `no-derivatives`: every license except
the two NoDerivs ones. In val2017 those are ID 3, Attribution-NonCommercial-NoDerivs
(1,414 images), and ID 6, Attribution-NoDerivs (246 images).

| `--licenses` | Imports |
|---|---|
| `no-derivatives` (default) | Every license whose name does not contain "NoDeriv": 3,340 images, 24,645 boxes (296 crowds) |
| `all` | All 5,000 images, 36,781 boxes (446 crowds) |
| Comma-separated IDs, e.g. `4,5,7,8` | Only those license IDs from `instances_val2017.json` |

The importer prints the selected and excluded IDs at the start. `--limit` counts
only images with a selected license. A rerun never deletes rows, so use a new
`--dataset-name` when you narrow the selection of an existing dataset.

Suggested dataset attribution: “Images from the COCO 2017 validation dataset,
credited to their original Flickr photographers under the license linked on each
image. Annotations: COCO Consortium, CC BY 4.0. Freedom Ridge generates CLIP
embeddings and mask visualizations.” Display/link `flickr_url`, `license_name`,
`license_url` and `coco_url` with each image. The official COCO files do not include
photographer names; the Flickr landing page supplies that attribution.

The exact download URLs in `examples/download_demo_data.py` are:

```text
http://images.cocodataset.org/zips/val2017.zip
http://images.cocodataset.org/annotations/annotations_trainval2017.zip
```

The importer never downloads or extracts anything. That annotation archive also
contains training files; this importer reads only the three validation files.

## Layout and generated assets

```text
<data-root>/                         # e.g. $FRIDGE_DATA_ROOT/coco
  val2017/000000000139.jpg            # 5,000 JPEGs (imported per --licenses)
  annotations/
    instances_val2017.json
    person_keypoints_val2017.json
    captions_val2017.json
  derived/coco/<image-id>/<annotation-id>.png
  derived/objects/<image-id>/<detection-id>.jpg
  .work/fridge-index-*/annotations.sqlite   # temporary; removed at exit
```

Source files are left intact. Set `--data-root` inside the backend's
`FRIDGE_DATA_ROOT`. All generated mask PNGs, crop JPEGs and temporary indexes
stay under `--data-root`; resolved paths outside that root are rejected. The
backend serves their local paths through its authenticated media endpoint.
After the first image batch the importer fetches an image through that API and
stops with a clear error on 403. No side asset service is needed.

## What becomes which field

| Source | Freedom Ridge representation |
|---|---|
| Instance boxes | `detections`: normalized `Detection` boxes; category name, stable annotation ID; category ID, supercategory, area, iscrowd retained directly on each label for element filters |
| Polygon segmentations | `polygons`: closed, filled `Polygon` with all disconnected rings, normalized coordinates and source annotation ID |
| Crowd RLE (also non-crowd RLE, if present) | Independent `Segmentation` fields `crowd_mask_000`, etc.; indexed PNG masks linked to the matching detection; overlapping masks are never flattened together |
| Person poses | `keypoints`: every person annotation, including zero-visible-point records; all 17 source names, raw coordinates, visibility 0/1/2 and zero-based skeleton edges retained |
| Visible pose skeleton | `skeletons`: one two-point open polyline per visible bone; visibility=0 points are omitted from rendering, visibility=1 occluded points remain |
| Captions | `captions`: every caption string (usually five, occasionally more); `caption_text`: newline-joined searchable text |
| Attribution | `license_id`, `license_name`, `license_url`, `coco_url`, `flickr_url`, plus original image information including capture date |
| Class facets | Native, indexed arrays `class_names`, `supercategory_names`; `classes`/`supercategories` also retained as metadata lists |
| Embeddings | Inline normalized CLIP vectors in `clip` (or `--field-name`); model registered for matching text-tower search |

All label IDs are deterministic. Mask slots are per-image overlay positions, not
class IDs; `labels` records each slot's class. The current frontend draws keypoints
as dots, hence the separate skeleton polylines. Dataset segmentation settings assign distinct slot colors and class hover names.

## Run

Use the repository SDK plus `numpy`, `Pillow`, `torch` and `transformers` in the
Python environment. The model and processor must already exist in the Hugging Face
cache: both are loaded with `local_files_only=True`. No pycocotools/ijson dependency
is needed. Run from the repository root:

```bash
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 USE_TF=0 \
python examples/coco/ingest.py \
  --data-root $FRIDGE_DATA_ROOT/coco \
  --dataset-name coco-val2017 --limit 0 --device cpu --batch-size 16 \
  --api-url "$FRIDGE_API_URL"
```

`--limit 100` imports the first 100 selected images in numeric ID order; `0` imports
every image with a selected license.
Defaults: limit 100, batch size 16, `clip`, `openai/clip-vit-base-patch32`, CUDA if
available otherwise CPU, API from `FRIDGE_API_URL` or `http://localhost:8000/api`,
credentials from `FRIDGE_USERNAME` (default `admin`) and `FRIDGE_PASSWORD`.
Preferred: set `FRIDGE_API_TOKEN` to an API token created in **Settings → API Tokens** (revocable and expiring; no password involved); the importer then skips the password login.
There is no default password; an interactive terminal prompts if it is unset.
CLI overrides remain available. Complete the first-login password change in the app
before creating a token or using password authentication.
`--no-reduce` skips UMAP. Stats refresh at the end; each embedding field is
reduced only if this run wrote rows for that field. A no-op resume skips UMAP.
Maps are built client-side at any dataset size: the UMAP fit scales to available RAM,
and every embedded image and object gets coordinates.
`--work-dir` controls temporary SQLite placement inside `--data-root`.

`--on-duplicate skip` is the default: batches look up stable filepaths before image
decoding and CLIP inference. Reruns do not add duplicate samples or label objects.
Use `--on-duplicate update` to replace an earlier partial import's metadata and
vectors, or `error` to stop on conflicts. Required missing images/annotations fail
with their paths; they are never silently discarded. Earlier completed batches
remain resumable. Keep the same `--limit` when testing skip; increase it or use 0
to continue to the full split.

The JSON parser streams top-level array elements into a temporary SQLite join
index. Only the small taxonomy/license tables, one image's annotations, and one
image/embedding batch reside in memory. This also handles unsorted annotations.

Examples using an authenticated SDK client `client` and dataset `ds`:

```python
client.query_samples(ds._id, filters={"field": "license_name", "operator": "contains", "value": "Attribution"})
client.query_samples(ds._id, filters={"field": "class_names", "operator": "contains", "value": "person"})
client.query_samples(ds._id, filters={"field": "caption_text", "operator": "contains", "value": "bicycle"})
client.query_samples(
    ds._id,
    filters={
        "field": "detections",
        "operator": "match_elements",
        "value": {"field": "iscrowd", "operator": "=", "value": 1},
    },
)
```

## Objects and resume

Every box creates an object row **in the same dataset**, after its parent image
has an ID. This includes crowd regions (`is_crowd=true`). Each object has its own
JPEG under `derived/objects/<image-id>/<detection-id>.jpg`, `media_type=object`,
`parent_id`, normalized displayed-frame `crop_bbox`, scalar `label`, source
attributes and `[label]` tags. Objects carry normalized `clip_obj` vectors and a
separate UMAP; that field name is reserved and cannot be used for image vectors.
See [OBJECT_PATCH_VIEW.md](../../docs/OBJECT_PATCH_VIEW.md).

Resume looks up parent and crop filepaths independently, so an interruption
between parent insertion and object insertion is recoverable. Complete reruns
need no decoding or CLIP inference. `update` replaces existing metadata/vectors.
Original source files are never overwritten; derived JPEGs use quality 95.

## Verification and capacity

```bash
PYTHONPATH=python:. pytest -q examples/coco/test_coco.py examples/test_demo_import_regressions.py
python examples/_shared/verify.py --api-url "$FRIDGE_API_URL" \
  --dataset-name coco-val2017 --kind coco --expected-count 100
```

The verifier checks image/object counts, parent links, every crop and mask through
the API, local asset paths, filters, and `clip`/`clip_obj` UMAP coverage. Attribution
URLs remain source references; they are not asset-serving URLs.

The full split contains 5,000 images and 36,781 boxes, including 446 crowds. The
default license selection imports 3,340 images and 24,645 boxes (296 crowds).
Budget for all those CLIP inputs and crop JPEGs; benchmark `--limit 100` on the
deployment machine instead of extrapolating image-only timings. The download step takes
about 2.6 GB with archives retained (the ZIPs may be deleted after extraction); reserve additional space for crops, the temporary SQLite index,
CLIP weights, and database vectors/indexes/WAL. Temporary indexes are removed on
exit; derived assets must remain for browsing.
