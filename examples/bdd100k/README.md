# BDD100K driving keyframes

BDD100K requires a **free account** at [bdd-data.berkeley.edu](https://bdd-data.berkeley.edu/)
and acceptance of its non-commercial research terms. There is **no automated
download**. The importer code's license does not license the data. Do not use it
as a public hosted demo without appropriate dataset permission.

Download the 100k image keyframes (~7 GB), JSON labels and, optionally, the info
archive (~40 MB) for GPS/heading. Arrange the extracted files as follows:

```text
$FRIDGE_DATA_ROOT/
  bdd100k/
    100k_images/{train,val,test}/*.jpg
    100k_annotations/{train,val,test}/*.json
    100k_data/{train,val,test}/*.json       # optional telemetry
```

The full release has 100,000 keyframes: 70k train, 10k val and 20k test. Only
available per-image annotations are imported. Each image retains `split`,
`weather`, `scene`, `timeofday`, pixel-coordinate `detections`, normalized
`polygons` (lanes/drivable areas), and optional speed, timestamp, altitude,
GPS accuracy, accelerometer and gyroscope metadata. GPS becomes `location`;
course becomes the top-level `heading`. No location is invented when telemetry
is absent. Boxes retain the existing label/score shape; this importer creates
image rows, not separate object rows.

Follow the [demo prerequisites](../README.md), then:

```bash
python examples/import_demo_data.py --datasets bdd100k --limit 100
# Direct CLI: --data-root is the PARENT of bdd100k, preserving existing paths.
python examples/bdd100k/ingest.py --data-root "$FRIDGE_DATA_ROOT" --limit 0
```

Default dataset name is `BDD100K`, limit **100 per split**, batch size 32, CLIP
field `clip`. CLIP is now inline, cached-only and resumable through the shared
importer toolkit; no separate `--compute-embeddings` pass is needed. Full CPU/GPU
timings were not recorded; measure a small subset first. `--no-reduce` separates
ingestion timing from UMAP. To recompute existing vectors, use
`examples/sdk/compute_embeddings.py --dataset BDD100K --limit 0`.

Local filepaths remain `bdd100k/100k_images/<split>/<name>.jpg`, relative to the
backend's root, so old imports still resume. Both original frame/object labels
and image-level `labels` exports are accepted. Use `--on-duplicate update` to
refresh metadata, or a new `--dataset-name` for another selection.

## Perceptual hashes and duplicates

Imports include `dhash_8` and `phash_8` on image rows so the **Duplicates** panel
and **collapse duplicates** work for matching images. Object rows receive no hashes.
Use `--hash-algorithms dhash_8 phash_8` to choose algorithms or `--no-hashes` to omit
them; both flags also work with `examples/import_demo_data.py`.

Resume skips existing rows without rehashing. Backfill an older dataset from the
repository root using [the SDK recipe](../sdk/compute_hashes.py), with API credentials
and access to the media files:

```bash
python examples/sdk/compute_hashes.py BDD100K --algorithms dhash_8 phash_8
```

Backfill skips objects and images with all requested hashes, computing only missing
fields. See [shared hash options](../README.md#perceptual-hashes-and-duplicates).

## Optional S3 storage

The generic bucket uploader was retained in
[`scripts/scale_tests/upload_bdd100k_to_s3.py`](../../scripts/scale_tests/upload_bdd100k_to_s3.py).
It takes your bucket/prefix and credentials; it has no private infrastructure
binding. Check dataset permissions before copying images to a hosted bucket.
The upload and importer both anchor keys to the parent data root:

```bash
python scripts/scale_tests/upload_bdd100k_to_s3.py --data-root "$FRIDGE_DATA_ROOT" \
  --s3-bucket your-bucket --dry-run
python scripts/scale_tests/upload_bdd100k_to_s3.py --data-root "$FRIDGE_DATA_ROOT" \
  --s3-bucket your-bucket --create-fridge-storage-config
python examples/bdd100k/ingest.py --data-root "$FRIDGE_DATA_ROOT" --s3-bucket your-bucket \
  --storage-config-id 1 --limit 100
```

Use the storage config ID printed by the uploader. Pass the same `--s3-prefix`
to both scripts. The backend serves S3 media using that config; CLIP reads your
local images. The shared verifier checks media and vector coverage:

```bash
python examples/_shared/verify.py --kind bdd100k --dataset-name BDD100K \
  --api-url http://localhost:8000/api --expected-count 300 --expect-reduced
```

Adjust the count to the annotations actually available. Offline mapping tests:
`PYTHONPATH=python:. python -m pytest examples/bdd100k -q`.
