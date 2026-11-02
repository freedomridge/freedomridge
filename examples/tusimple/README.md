# TuSimple lanes and velocity in Freedom Ridge

**LOCAL-ONLY — TuSimple is not for the public demo. Use these importers only for
private/local evaluation; the supplied dataset terms do not clear public hosting.**

Import the annotated frame from each clip, with typed labels, metadata and a
locally computed CLIP embedding in the same sample-creation request. The scripts
read the original ZIPs without unpacking videos. Sources and cached model files
are read-only; only the selected JPEGs are copied into `--data-root`.

## Perceptual hashes and duplicates

Imports include `dhash_8` and `phash_8` on image rows so the **Duplicates** panel
and **collapse duplicates** work for matching images. Object rows receive no hashes.
Use `--hash-algorithms dhash_8 phash_8` to choose algorithms or `--no-hashes` to omit
them; both flags also work with `examples/import_demo_data.py`.

Resume skips existing rows without rehashing. Backfill an older dataset from the
repository root using [the SDK recipe](../sdk/compute_hashes.py), with API credentials
and access to the media files:

```bash
python examples/sdk/compute_hashes.py tusimple-lanes --algorithms dhash_8 phash_8
python examples/sdk/compute_hashes.py tusimple-velocity --algorithms dhash_8 phash_8
```

Backfill skips objects and images with all requested hashes, computing only missing
fields. See [shared hash options](../README.md#perceptual-hashes-and-duplicates).

## Sources and license verdict

Source: TuSimple's CVPR 2017 benchmark, supplied locally with the
`tusimple-benchmark` repository. The upstream reference is
<https://github.com/TuSimple/tusimple-benchmark>.

**Public hosted demo: not cleared by the supplied data terms. Keep the data in
private/local evaluation until the dataset rights holder confirms public display
and redistribution rights.** This is a conservative deployment verdict, not a
claim that the files contain an express “local use only” restriction.

The local `TusimpleLaneDetection/tusimple-benchmark/LICENSE` is **Apache 2.0**:

- Section 2 grants a perpetual, worldwide, non-exclusive, no-charge, royalty-free,
  irrevocable copyright license to reproduce, prepare derivatives, publicly
  display, publicly perform, sublicense and distribute the licensed Work.
- Section 4 requires a license copy for recipients, prominent modification notices,
  preservation of applicable copyright/patent/trademark/attribution notices, and
  preservation of any supplied NOTICE attribution. Sections 3 and 6 cover the
  patent grant/termination and absence of a general trademark grant; sections 7–8
  disclaim warranties and limit liability.
- Those permissions apply to the licensed Work. The repository license does not
  explicitly identify these separately distributed image/annotation archives as
  part of that Work. It is insufficient evidence to license the dataset itself.

All six ZIP central directories and their embedded readmes were inspected, along
with the local benchmark README and lane/velocity docs. No archive contains a
LICENSE, NOTICE, COPYING or separate terms file. The lane ZIP readmes and velocity
train/test readmes describe the challenge and data formats without a public
redistribution/display grant. The supplement's `README.md` says exactly:
**“You can use the data freely during your training process”**. That sentence
permits training use; it does not expressly grant public hosting. The lane docs
link to `http://benchmark.tusimple.ai/#/term` for prize rules, but no copy of those
terms is supplied locally. No noncommercial-only, research-only, or local-only
clause was found in the inspected files. Download availability and the repository
Apache license are not treated as evidence of dataset hosting permission.

## Archive inventory

These counts are from the actual local files, not just the readmes. Sizes below
are compressed archive sizes, rounded to decimal GB.

| Source folder (`--source-root`) | Contents and use |
| --- | --- |
| `TusimpleLaneDetection/train_set.zip` (11.68 GB) | 3,626 clips × 20 JPEGs, three `label_data_*.json` JSONL files, readme. Import only each labelled `20.jpg`. |
| `TusimpleLaneDetection/test_set.zip` (11.47 GB) | 2,782 clips × 20 JPEGs, `test_tasks_0627.json` submission template and readme. Import `20.jpg` using external `test_label.json`. |
| `TusimpleLaneDetection/test_label.json` | 2,782 ground-truth JSONL records. |
| `TusimpleLaneDetection/test_baseline.json` | Baseline predictions, not ground truth; unused. |
| `TusimpleLaneDetection/archive.zip` (23.24 GB) | A repack: 134,580 files, 24,304,438,810 uncompressed bytes. `TUSimple/train_set/` and `TUSimple/test_set/` duplicate all 128,166 original files (every CRC and size matches). Also includes identical `TUSimple/test_label.json`, 6,408 segmentation PNGs, two split lists, two combined annotation files under `train_set/seg_label/`, and `test_label_new.json`. The latter has the same 2,782 annotations with rewritten image paths. Unused: it adds segmentation derivatives, not another lane benchmark. |
| `TusimpleLaneDetection/tusimple-benchmark/` | Apache-licensed benchmark repository, documentation, examples and evaluation code. |
| `TusimpleVelocity/benchmark_velocity_train.zip` (3.41 GB) | 1,074 clips × 40 JPEGs; per-clip `annotation.json` with boxes, velocity and position; camera calibration and readme. Import `040.jpg`. Actual annotation count: 1,442 vehicles (the repository readme's 3,222 does not match these files). |
| `TusimpleVelocity/benchmark_velocity_test.zip` (0.84 GB) | 269 clips × 40 JPEGs; per-clip boxes only, calibration and readme. Join external `gt.json` for motion and import `040.jpg`. |
| `TusimpleVelocity/gt.json` | JSON array of 269 vehicle lists, ordered by numeric clip ID starting at 1, with 375 vehicles. The importer checks every corresponding box before accepting a record, and rejects mismatched lengths/order. |
| `TusimpleVelocity/baseline.json` | Baseline predictions; unused. |
| `TusimpleVelocity/benchmark_velocity_supp.zip` (0.44 GB) | **5,067** distinct labelled JPEG stills in `supp_img/`, `annotation.json`, readme; 29,115 vehicle boxes. Included as `supp`. Readmes say 5,066, but this archive has 5,067 images and 5,067 matching records. No velocity/position ground truth. |

The current velocity archives contain 40 frames per clip. The older
`doc/velocity_estimation/guideline.md` describes a different 60-frame release;
these importers follow the supplied ZIPs and their readmes.

## Labels and fields

Both datasets store `split`, `clip_id`, `source_file`, `width`, `height`, plus a
split tag. `clip` is a 512-dimensional whole-frame CLIP embedding by default.
They are image samples; detections are inline labels, not extra object samples.
Every sample also stores `attribution="TuSimple benchmark"` and a `license`
summary: the supplement permits training use, public display/redistribution is
not expressly granted by the supplied dataset terms, and the code's Apache-2.0
license is not established for the archives. The summary records our local-only
policy separately from those terms.

- **Lanes:** `lanes` is a `Polylines` list. Each gap-separated run becomes its own
  `Polyline` with one normalized `[x / width, y / height]` path. Every `-2` splits
  a run; gaps are never bridged. Single-point runs remain as data, although they
  cannot draw a line. Every label stores `num_points` in its attributes and as a
  direct key for filtering. Empty lanes create no label. `lane_count` counts
  source lanes, including empty ones; `lane_index` and zero-based `segment_index`
  identify the source lane and run in each label's attributes. Labels are open/unfilled.
  Across all source records, 24,284 source lanes yield 23,747 nonempty polylines.
  Eight have one point; the real dataset has no interior gaps.
- **Velocity:** `vehicles` is a `Detections` list. Boxes are normalized
  `[left / width, top / height, (right-left) / width, (bottom-top) / height]`.
  `vehicle_count` is the number of annotations on the selected frame.
- Each train/test vehicle has attributes `vehicle_index`, `velocity_forward` and
  `velocity_right` in **m/s**, `position_forward` and `position_right` in **metres**,
  `speed` in m/s and `distance` in metres. TuSimple's source order is
  `[forward, right]`: element 0 follows the camera optical axis, element 1 points
  right. Signs and source meanings are preserved. The ambiguous `velocity` and
  `position` pairs are removed. `speed = hypot(velocity_forward, velocity_right)`
  and `distance = hypot(position_forward, position_right)` are planar magnitudes.
  Velocity is relative to the camera, not absolute road speed; position describes
  the nearest point on the vehicle.
- `has_motion=false` for the supplement. Its detections omit motion components,
  speed and distance entirely. No zeros are invented. The 49 zero-area source
  boxes are retained unchanged and flagged `attributes.degenerate_bbox=true`;
  inverted/nonfinite boxes still raise an error.
- Label IDs are deterministic per benchmark/split/clip/index/segment. The first
  segment retains the previous lane ID. `refresh_stats()` registers sample fields
  for filtering. The meaningful field names `lanes` and `vehicles` are retained.

**Per-vehicle filtering:** the four motion components, `speed` and `distance` are mirrored as direct
keys on each motion detection. The backend's `match_elements` operator evaluates
direct element keys; it does not traverse `attributes.speed`. These scalar
mirrors are the filter interface. They are not sample-level averages and are not
separate automatic Filters-sidebar fields. `vehicle_count`, `has_motion` and
`split` are ordinary discoverable sample fields.

For example, this SDK/API filter returns images with at least one **same vehicle**
closer than 20 m and moving faster than 1 m/s relative to the camera:

```python
import os
from fridge import Dataset, connect, get_client

connect(os.environ["FRIDGE_API_URL"])  # choose your backend explicitly
client = get_client()
client.login(os.environ.get("FRIDGE_USERNAME", "admin"), os.environ["FRIDGE_PASSWORD"])
dataset = Dataset("tusimple-velocity")
result = client.query_samples(
    dataset.id,
    filters={
        "field": "vehicles",
        "operator": "match_elements",
        "value": {
            "logic": "AND",
            "filters": [
                {"field": "distance", "operator": "<", "value": 20},
                {"field": "speed", "operator": ">", "value": 1},
            ],
        },
    },
)
print(result["total"])
```

This filters parent images and returns their full labels. Two independent
`match_elements` predicates can match different vehicles; group them inside one
operator when both conditions must hold for the same vehicle.

To find single-point lanes, use the same operator with
`{"field": "lanes", "operator": "match_elements", "value":
{"field": "num_points", "operator": "=", "value": 1}}`.

## Run

Use an environment with the Freedom Ridge SDK dependencies, Pillow, PyTorch and
Transformers already installed, and `openai/clip-vit-base-patch32` in the local HF
cache. The scripts add this checkout's `python/` SDK to their import path. Model
and processor loading use `local_files_only=True`; no download fallback exists.
`--model` can also name a local CLIP model directory.

Pass the folder holding the original archives as a read-only `--source-root` (default: `./TusimpleLaneDetection` for lanes, `./TusimpleVelocity` for velocity). Set the
backend's `FRIDGE_DATA_ROOT` to a directory containing the extraction destination;
that directory must be visible at the same absolute path to importer and backend.
After the first batch, the importer always fetches one image through the API,
including on duplicate-only reruns. HTTP 403 stops the run with a message to
check the backend's `FRIDGE_DATA_ROOT` and access permissions. The first batch
may already be committed; later batches and UMAP are not run on failure.
An optional local `FRIDGE_DATA_ROOT` additionally catches mismatches before import.

Expected extracted layout:

```text
<data-root>/
  lanes/train/clips/<date>/<clip>/20.jpg
  lanes/test/clips/<date>/<clip>/20.jpg
  velocity/train/clips/<numeric-id>/imgs/040.jpg
  velocity/test/clips/<numeric-id>/imgs/040.jpg
  velocity/supp/supp_img/<id>.jpg
```

From the repository root, with a private local backend configured to serve
`$FRIDGE_DATA_ROOT` (see the disposable setup below):

```bash
export FRIDGE_API_URL=http://localhost:8000/api   # your backend's /api URL
export FRIDGE_DATA_ROOT="$PWD/data"               # the backend's FRIDGE_DATA_ROOT
export FRIDGE_USERNAME=admin
# Set FRIDGE_PASSWORD securely, or let the importer prompt in a terminal.
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
# Limiting CPU worker threads avoids oversubscription on large hosts.
export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4

python examples/tusimple/ingest_lanes.py \
  --source-root /path/to/TusimpleLaneDetection \
  --data-root "$FRIDGE_DATA_ROOT/tusimple" --dataset-name tusimple-lanes \
  --device cpu --batch-size 16 --limit 0

python examples/tusimple/ingest_velocity.py \
  --source-root /path/to/TusimpleVelocity \
  --data-root "$FRIDGE_DATA_ROOT/tusimple" --dataset-name tusimple-velocity \
  --device cpu --batch-size 16 --limit 0
```

Defaults: `--limit 100` total records, `--batch-size 32`, `--field-name clip`,
`--device auto` (CUDA if available), `--on-duplicate skip`.
Preferred: set `FRIDGE_API_TOKEN` to an API token created in **Settings → API Tokens** (revocable and expiring; no password involved); the importer then skips the password login.
Credentials use `FRIDGE_USERNAME` (fallback `admin`) and `FRIDGE_PASSWORD`, with
**no default password**. Without a password, terminal runs prompt using `getpass`;
noninteractive runs fail clearly before login. Explicit credential flags override
the environment but expose passwords in shell history/process arguments. The
verifier uses the same credential handling. `--api-url` defaults to `FRIDGE_API_URL`, then
`http://localhost:8000/api`. The seeded admin must already have passed password
change, or have that gate cleared in a disposable test instance.

Records are interleaved across selected splits. Thus a 100-sample lane run is
50 train/50 test; velocity is 34 train/33 test/33 supp. Use `--splits train test`
to omit supplementary stills, or a single split to import it alone. `--limit 0`
means all records; the limit includes already imported records on reruns.

The import uses bounded image/API batches and streamed JSON annotations (ZIP
central directories remain in memory). Only selected frames are extracted, with
atomic replacement of missing/truncated files. Reruns with the same dataset name,
root and field skip existing filepaths before loading CLIP, while repairing
missing local frames. `--on-duplicate update` recomputes embeddings and overwrites
the selected samples; use it after changing mappings, model or embedding field.
`error` fails on duplicates; completed earlier batches remain resumable. Keep one
writer per dataset/root. At completion, stats are refreshed and the map is built client-side
only when samples were written and `--no-reduce` was not supplied. The UMAP fit scales to
available RAM and places every embedded row, at any dataset size.
A no-op run prints `Skipping reduction / UMAP: no samples written`, preserving the existing map.
To rebuild a map for an older dataset in an authenticated SDK session, use
`Dataset("tusimple-lanes").compute_visualization(field_name="clip")` with your dataset name.

## Tests and verification

The fixture suite needs neither a backend nor original archives:

```bash
PYTHONPATH=python:. python -m pytest examples/tusimple -q
```

It covers lane segmentation, vehicle motion, label identities and attributes,
selective extraction, interrupted/resumed imports, lazy CLIP loading, credentials,
and first-batch media failures. All importers use the same toolkit and verifier.

For API checks, use a disposable backend with its own database, data root and ports
(see `.github/workflows/ci.yml` for database/backend startup). Set `FRIDGE_API_URL`
explicitly and create a token through that app's **Settings → API Tokens**. Import
100 frames per benchmark, then verify the exact source mapping:

```bash
python examples/import_demo_data.py --datasets tusimple --limit 100
# Or use --tusimple-lanes-source /path/to/TusimpleLaneDetection
# and --tusimple-velocity-source /path/to/TusimpleVelocity.
python examples/_shared/verify.py --kind tusimple_lanes --dataset-name tusimple-lanes \
  --api-url "$FRIDGE_API_URL" --data-root "$FRIDGE_DATA_ROOT/tusimple" \
  --source-root "$FRIDGE_DATA_ROOT/tusimple/TusimpleLaneDetection" --expected-count 100
python examples/_shared/verify.py --kind tusimple_velocity --dataset-name tusimple-velocity \
  --api-url "$FRIDGE_API_URL" --data-root "$FRIDGE_DATA_ROOT/tusimple" \
  --source-root "$FRIDGE_DATA_ROOT/tusimple/TusimpleVelocity" --expected-count 100
```

The verifier checks labels, attribution, field discovery, 512-dimensional vectors,
media, point-count filters, and same-vehicle motion filters. Repeat the import to
verify counts and label IDs remain unchanged and no model/reduction runs.

## Full-import budget

Exact selected JPEG sizes from ZIP metadata:

| Dataset | Frames | JPEG bytes | JPEG disk use |
| --- | --- | --- | --- |
| lanes | 6,408 | 1,209,507,240 | 1.13 GiB |
| velocity (train/test/supp) | 6,410 | 555,366,211 | 0.52 GiB |
| total | 12,818 | 1,764,873,451 | 1.64 GiB |

Allow roughly **3–4 GiB total additional disk** for extracted images, Postgres
labels/embeddings/indexes/WAL and caches, excluding the already present source
archives and HF model cache. The 512-dimensional float32 vector payload alone is
about 25 MiB for both datasets. Database/WAL growth depends on retention and
indexing; 3–4 GiB is a planning allowance, not a measured full import.

Measurements with four CPU threads and batches of 16 put CLIP plus decode
at approximately **22–28 images/s** on comparable image datasets. That is about
**4–5 minutes per 6,400 frames**, or **8–10 minutes combined**, before extraction,
upload, stats and UMAP. Budget roughly **5–10 minutes per dataset** for CPU
ingestion on similar hardware; storage and contention can change this. These
are throughput-based estimates, not measured full TuSimple imports.

A CUDA velocity smoke run imported 1,000 frames in 28.7 s, including
startup and API work. Model startup is a fixed cost and must not be multiplied
for every 100-frame batch when estimating a full run. UMAP's first JIT compilation
and full-dataset reduction add separate time; use `--no-reduce` to measure
ingestion alone.

Runner source overrides also accept `FRIDGE_TUSIMPLE_LANES_SOURCE` and
`FRIDGE_TUSIMPLE_VELOCITY_SOURCE` (including in `--env-file`). Without overrides,
they default to `$FRIDGE_DATA_ROOT/tusimple/TusimpleLaneDetection` and
`$FRIDGE_DATA_ROOT/tusimple/TusimpleVelocity`. Explicit flags win over the environment.
