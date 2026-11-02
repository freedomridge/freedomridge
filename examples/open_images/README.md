# Open Images V7 validation from local files

`ingest.py` can import the entire 41,620-image validation split (the default
`--limit` is 100), including images without boxes, and every official annotation
family. Optional patch embeddings use the generic SDK recipe below.

## Perceptual hashes and duplicates

Imports include `dhash_8` and `phash_8` on image rows so the **Duplicates** panel
and **collapse duplicates** work for matching images. Object rows receive no hashes.
Use `--hash-algorithms dhash_8 phash_8` to choose algorithms or `--no-hashes` to omit
them; both flags also work with `examples/import_demo_data.py`.
Hashes use the saved rotated JPEG pixels, matching an SDK backfill of the served file.

Resume skips existing rows without rehashing. Backfill an older dataset from the
repository root using [the SDK recipe](../sdk/compute_hashes.py), with API credentials
and access to the media files:

```bash
python examples/sdk/compute_hashes.py open-images-v7-val --algorithms dhash_8 phash_8
```

Backfill skips objects and images with all requested hashes, computing only missing
fields. See [shared hash options](../README.md#perceptual-hashes-and-duplicates).

## Source, license and attribution

Source: [Google Open Images V7](https://storage.googleapis.com/openimages/web/index.html),
with images from the [CVDF mirror](https://github.com/cvdfoundation/open-images-dataset)
and captions from [Localized Narratives](https://google.github.io/localized-narratives/).
Open Images annotations and Localized Narratives annotations are CC BY 4.0.
The image metadata records each photograph's license (normally CC BY 2.0) and
creator; retain those individual credits and original links.

Suggested attribution: “Open Images V7 validation. Photographs © their respective
creators under the license linked on each image. Annotations: Google Open Images,
CC BY 4.0; captions: Localized Narratives, CC BY 4.0. Freedom Ridge applies the
provided image rotation and generates CLIP embeddings.” For each image display
`Title` by `Author`, linked to `OriginalLandingURL`/`OriginalURL`, and `License`.
`AuthorProfileURL` is retained as well. Rotated JPEG derivatives are marked by
`rotation_applied` and retain their original source filepath and attribution.

These are the **exact download URLs from `examples/download_demo_data.py`**. The importer
never downloads, extracts or modifies them:

```text
https://s3.amazonaws.com/open-images-dataset/tar/validation.tar.gz
https://storage.googleapis.com/openimages/v5/validation-annotations-bbox.csv
https://storage.googleapis.com/openimages/v5/validation-annotations-human-imagelabels-boxable.csv
https://storage.googleapis.com/openimages/v7/oidv7-val-annotations-human-imagelabels.csv
https://storage.googleapis.com/openimages/v7/oidv7-val-annotations-point-labels.csv
https://storage.googleapis.com/openimages/v5/validation-annotations-object-segmentation.csv
https://storage.googleapis.com/openimages/v6/oidv6-validation-annotations-vrd.csv
https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions.csv
https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions-boxable.csv
https://storage.googleapis.com/openimages/2018_04/bbox_labels_600_hierarchy.json
https://storage.googleapis.com/openimages/2018_04/validation/validation-images-with-rotation.csv
https://storage.googleapis.com/localized-narratives/annotations/open_images_validation_localized_narratives.jsonl
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-0.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-1.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-2.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-3.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-4.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-5.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-6.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-7.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-8.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-9.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-a.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-b.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-c.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-d.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-e.zip
https://storage.googleapis.com/openimages/v5/validation-masks/validation-masks-f.zip
```

## Expected layout

```text
<data-root>/                            # $FRIDGE_DATA_ROOT/open_images_v7
  validation/<ImageID>.jpg               # extracted validation.tar.gz
  annotations/<the CSV/JSON/JSONL files above>
  masks/<ImageID>_<class>_<BoxID>.png      # extracted 16 mask zips, flat
  derived/open_images/<ImageID>-r90.jpg # only nonzero Rotation images
  derived/objects/<ImageID>/<box-id>.jpg
  .work/fridge-index-*/annotations.sqlite
```

The downloaded mask archives use a flat extracted PNG layout; lowercase
`masks/<first-ID-character>/<MaskPath>` is also accepted. The extracted layout was verified: **41,620** JPEGs in
`validation/` and **24,730** PNGs directly in `masks/`. Missing files fail with paths, rather than
silently dropping annotations or samples. Narratives arrive last: the normal
import requires them. `--allow-missing-narratives` explicitly permits a temporary
caption-free import; rerun with **`--on-duplicate update`** after arrival. A skip
rerun deliberately leaves already imported samples unchanged.

## Mapping

| Source | Freedom Ridge fields |
|---|---|
| Bounding boxes | `detections`: named `Detection` labels, normalized XYWH; all five flags (`IsOccluded`, `IsTruncated`, `IsGroupOf`, `IsDepiction`, `IsInside`) retained as integers, with Source/Confidence and MID |
| Both verified image-label CSVs | `positive_labels`: positive classifications; `negative_classes`: plain filterable strings. `verified_absent` retains class/source provenance without classification-shaped `label` keys. Repeated verification records across CSVs are retained, never silently deduplicated |
| V7 point labels | `points`: one named `Keypoint` per row, including **yes, no and unsure**; coordinates, EstimatedYesNo, Source, votes, TextLabel and original MID retained |
| Instance masks | `instance_mask_000`, etc.: independent full-frame `Segmentation` overlays pointing to indexed local PNGs; `mask_instances`: matching detection IDs, BoxID, PredictedIoU, Clicks and mask field. No compositing that loses overlaps |
| Visual relationships | `relationships`: one union-box `Detection` per relation, labeled with the predicate, with subject/object names and their separate rotated boxes and original MIDs |
| Localized Narratives | `captions` text list, searchable `caption_text`, and `narratives` plain caption strings; mouse traces, timed captions and voice links discarded before indexing |
| Image metadata | All metadata CSV columns retained, including OriginalURL, OriginalLandingURL, AuthorProfileURL, Author, License, Title, OriginalMD5/OriginalSize, Thumbnail300KURL; Rotation numeric, blank interpreted as 0 |
| Rotation | Apply the metadata angle counterclockwise to source JPEG pixels before embedding; quality-95 rotated JPEGs are persisted under `--data-root/derived`. Boxes, points, relationship boxes and mask PNGs are transformed by the same angle |
| Class names/hierarchy | Both description files accept headers or no header; `Subcategory` ancestors include transitive parents. `Part` is not treated as an is-a parent |
| Facets | Native indexed arrays: `class_names`, `parent_classes`, `negative_parent_classes`, `positive_classes`, `negative_classes`, `point_classes`, `point_states`, `mask_classes`, `relationship_predicates` |
| Embeddings | Normalized inline CLIP vectors in `clip` by default, with the matching model registered |

Label IDs use source file/row identities (BoxID where supplied). Source attributes
are stored once, directly on each label for filtering. Coordinates use the
displayed frame; the original files retain raw coordinates. Narrative mouse,
voice and timing data are omitted. Author and Title are HTML-unescaped.
Negative hierarchy facets stay separate from positive classes. The official v6
attribute names are bundled, and an optional local
`annotations/oidv6-attributes-description.csv` can override them.

The current frontend has no per-detection mask renderer. Masks therefore retain
independent slots, with exact box links in both directions. Binary foregrounds
are encoded as stable class IDs. Each slot has a distinct dataset-level color
and class hover targets set with `update_dataset`; banks of 255 classes avoid
byte-ID collisions when a slot represents different classes in different images.
Overlapping masks remain separate. All derived masks are local PNGs.

## Run offline

Use the repository SDK with `numpy`, `Pillow`, `torch`, `transformers`. The CLIP
checkpoint and processor must already be cached; loading uses
`local_files_only=True`. CSV and JSONL are streamed into a temporary SQLite index,
then joined one image at a time. Even the 1.1 GB narratives and large point CSV
never accumulate in memory. Unsorted files work. RAM grows with one batch and the
small class hierarchy, not with the dataset.

Set `--data-root` inside the backend's `FRIDGE_DATA_ROOT`. The importer leaves
sources intact and writes all derivatives and its temporary index under that
root; its write guard checks resolved path containment. After the first image
batch it fetches an image through the API and stops on 403 with a root-mismatch
message. Preferred: set `FRIDGE_API_TOKEN` to an API token created in **Settings → API Tokens** (revocable and expiring; no password involved); the importer then skips the password login. Otherwise set `FRIDGE_USERNAME` and `FRIDGE_PASSWORD` before running; no password
is built in (a terminal prompts if unset).

```bash
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 USE_TF=0 \
python examples/open_images/ingest.py \
  --data-root $FRIDGE_DATA_ROOT/open_images_v7 \
  --dataset-name open-images-v7-val --limit 100 --device cpu --batch-size 16 \
  --api-url "$FRIDGE_API_URL"
```

Defaults: limit 100 (first images in ID order; 0 = all), batch 16, `clip`,
`openai/clip-vit-base-patch32`, auto CUDA/CPU, username `admin`, API from
`FRIDGE_API_URL` or `http://localhost:8000/api`. Other options: `--model`,
`--device`, `--field-name`, `--username`, `--password`, `--work-dir` (inside the
root), `--no-reduce`, `--on-duplicate {skip,update,error}`. Stats refresh always
runs; UMAP runs only for fields whose rows were written. A no-op resume skips it.
The importer builds both `clip` and `clip_obj` maps client-side at any dataset size:
`Dataset.compute_visualization()` scales its fit to available RAM and places every
embedded row, including the full object-crop selection. Managed maps are left to scheduled
re-fits. To rebuild a map for an older dataset in an authenticated SDK session, run
`Dataset("open-images-v7-val").compute_visualization(field_name="clip_obj")`
with your dataset name (use `clip` for images).

Default `skip` looks up parent and crop filepaths independently before decoding
or embedding. Interrupted imports resume missing objects even when their parents
already exist. `update` refreshes existing rows, including late captions; `error`
stops on duplicates. Complete the first-login password change in the app before
creating a token or using password authentication.

Filter examples with an authenticated SDK `client` and dataset `ds`:

```python
client.query_samples(ds._id, filters={"field": "parent_classes", "operator": "contains", "value": "Animal"})
client.query_samples(ds._id, filters={"field": "negative_classes", "operator": "contains", "value": "Dog"})
client.query_samples(ds._id, filters={"field": "Author", "operator": "contains", "value": "Smith"})
client.query_samples(ds._id, filters={"field": "caption_text", "operator": "contains", "value": "street"})
client.query_samples(
    ds._id,
    filters={
        "field": "detections",
        "operator": "match_elements",
        "value": {
            "logic": "AND",
            "filters": [
                {"field": "label", "operator": "=", "value": "Person"},
                {"field": "IsOccluded", "operator": "=", "value": 1},
            ],
        },
    },
)
```

## Object and patch views

Every detection, including `IsGroupOf` regions, creates an object row **inside
the same dataset**. Each crop JPEG has a stable local path under
`derived/objects/<ImageID>/<box-id>.jpg`. Objects explicitly use `media_type=object`,
parent IDs, normalized `crop_bbox` in the rotated displayed frame, `label`, source
attributes, `[label]` tags, and filterable `is_group_of`. Their CLIP field is
exactly `clip_obj`, with its own UMAP. No sibling dataset is needed.
See [OBJECT_PATCH_VIEW.md](../../docs/OBJECT_PATCH_VIEW.md).

Fixed-grid patch embeddings are an optional step for finding unlabelled
look-alikes. They use the same CLIP-512 space as the image and object vectors:

```bash
python examples/sdk/compute_patch_embeddings.py --dataset open-images-v7-val --scales 4x4 8x8
```

Use `--partition`/`--total-partitions` for workers, then `--reduce-only` after all
partitions. See the [SDK index](../sdk/README.md).

## Verification and capacity

```bash
PYTHONPATH=python:. pytest -q examples/open_images/test_open_images.py examples/test_demo_import_regressions.py
python examples/_shared/verify.py --api-url "$FRIDGE_API_URL" \
  --dataset-name open-images-v7-val --kind open_images --expected-count 100
```

The verifier checks image/object counts, exact parent/box links, all crop and mask
responses, local asset paths, negative-label shapes, filters and `clip_obj` UMAP.
Source attribution URLs remain as references and are never used to serve assets.

The full split has 41,620 images, 303,980 boxes, and 24,730 masks. Budget for CLIP
on images **and** crops; benchmark `--limit 100` on the deployment machine. Sources
use roughly 12–14 GB JPEGs and 1.3 GB annotations; allow several GB for crop JPEGs,
SQLite workspace, vectors/indexes/WAL and cached CLIP weights. Rotated JPEGs and
indexed PNGs stay under `derived`; SQLite is removed at exit.
