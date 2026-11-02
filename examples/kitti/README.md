# KITTI object importer

Import the official KITTI object ZIPs with 3D boxes projected on camera images,
2D boxes, GPS/map, object crops, image/object similarity and LiDAR stored as file
references for a future point-cloud viewer (not yet in the app). The importer uses
the same helpers as COCO and Open Images in `examples/_shared/`. Follow the
[demo prerequisites](../README.md#prerequisites) for the Python environment and
locally cached CLIP weights. `--skip-embeddings`
omits CLIP and UMAP; normal import never downloads anything.

## Perceptual hashes and duplicates

Imports include `dhash_8` and `phash_8` on image rows so the **Duplicates** panel
and **collapse duplicates** work for matching images. Object rows receive no hashes.
Use `--hash-algorithms dhash_8 phash_8` to choose algorithms or `--no-hashes` to omit
them; both flags also work with `examples/import_demo_data.py`.

Resume skips existing rows without rehashing. Backfill an older dataset from the
repository root using [the SDK recipe](../sdk/compute_hashes.py), with API credentials
and access to the media files:

```bash
python examples/sdk/compute_hashes.py kitti --algorithms dhash_8 phash_8
```

Backfill skips objects and images with all requested hashes, computing only missing
fields. See [shared hash options](../README.md#perceptual-hashes-and-duplicates).

## Sources and disk

Keep these official archives together:

- `data_object_image_2.zip`
- `data_object_label_2.zip`
- `data_object_calib.zip`
- `devkit_object.zip` (including `mapping/train_mapping.txt` and `train_rand.txt`)
- `data_object_velodyne.zip` (LiDAR; `--no-lidar` ignores it even when present)

`python examples/download_demo_data.py --datasets kitti` fetches all five, always
including the 28.75 GB LiDAR ZIP; it has no option to skip it. To go without LiDAR,
fetch the other four ZIPs yourself, or pass `--no-lidar` at import. The downloader
retains these ZIPs without extracting them. `--source-root`
defaults to `--data-root` and can point to a separate read-only directory. The
importer streams selected members: only left colour PNGs and, with LiDAR, Velodyne
BINs are extracted; labels, calibration and mapping stay in ZIPs. All writes stay
under `--data-root`, which must be inside the backend's `FRIDGE_DATA_ROOT` at the
same absolute path. Existing complete media are reused; extractions and crops
are published atomically with mode 0644. Traversal and escaping symlinks are refused.

Training has **7,481 images, 40,570 cuboids and 11,295 DontCare regions**. Seven
cuboids have no image extent, so the full training set produces **40,563 object
rows**. Testing has 7,518 unlabelled images. Plan for **41.35 GB of retained ZIPs**
including 28.75 GB of LiDAR, plus about **6 GB of training PNGs, 14 GB of
training BINs and about 0.36 GB of crops** (40,563 crops at ~8.8 KB, JPEG q95). Without LiDAR, allow about 12.6 GB of ZIPs
plus 6.2 GB of extracted images/crops. Model cache, database indexes and WAL need
additional space. A limit extracts only the selected frames.

A CPU run imported 100 images and 435 crops in 24.6 seconds (44.8 seconds
including UMAP); **about 30–40 minutes for the full training split on CPU
is an extrapolation**.
Full GPU import is unmeasured; the design estimate is roughly 5 minutes of CLIP
plus extraction/upload and UMAP. Benchmark a small selection on your machine.
The importer builds maps client-side at any dataset size, scaling the UMAP fit to
available RAM and placing every embedded image and object.
Use `OMP_NUM_THREADS=4 MKL_NUM_THREADS=4` to avoid CPU oversubscription.

## Run

Preferred: export `FRIDGE_API_TOKEN` from **Settings → API Tokens**. Otherwise
set `FRIDGE_USERNAME` and `FRIDGE_PASSWORD`; a direct interactive import can prompt
for a password. There is no default importer password. Set `FRIDGE_API_URL` to
your backend's `/api` URL (or pass `--api-url`); the shared default is
`http://localhost:8000/api`. Set `FRIDGE_DATA_ROOT` to the backend's media root.
Credential environment files are supported by the runner's `--env-file` option.

Fetch the GPS cache first, before any import. It needs the downloaded ZIPs
(`data_object_image_2.zip` and `devkit_object.zip`) and outbound HTTPS. Without the
cache, frames import with `gps_available=false` and no map location; the importer
prints `N frames lack cached GPS` but still succeeds, and so does the runner.

```bash
python examples/download_demo_data.py --datasets kitti
# Fetch GPS on a connected machine. No credentials, API calls, extraction or CLIP.
python examples/kitti/ingest.py \
  --data-root "$FRIDGE_DATA_ROOT/kitti" --limit 0 --fetch-oxts-only

# Quick try, using cached GPS and cached model weights.
python examples/kitti/ingest.py \
  --data-root "$FRIDGE_DATA_ROOT/kitti" --limit 100 --device cpu

# Full training import through the common runner.
python examples/import_demo_data.py --datasets kitti --full --device cpu \
  --env-file import.env
```

`--device auto` selects CUDA when usable, otherwise CPU. The default is 100
training frames sorted by ID; `--limit 0` selects all. `--split testing` includes
calibration and LiDAR (unless `--no-lidar`) without labels, raw mapping or GPS, and cannot be
combined with `--fetch-oxts-only`. `--source-root /path/to/kitti` selects a separate
ZIP directory when running the importer directly. `--batch-size` bounds API and
embedding batches; `--no-reduce` retains vectors without computing UMAP.

`--on-duplicate skip` is the default. Resume checks existing rows before loading
CLIP, repairs missing media, and creates missing objects from the **persisted**
parent annotations and GPS. Existing crops are not re-rendered on a no-op rerun.
Every run refreshes stats and restores the `occluded` enum display. A rerun also
recovers UMAP when at least five embeddings exist and some lack coordinates,
even after a crash following the last upload. Completed reductions are reused
when no rows were written. `--on-duplicate update` rewrites both grains and
re-encodes them; use it to add GPS to frames imported before the cache existed.
Fetch the cache, then rerun with the same `--limit` as that import (`0` for all):

```bash
python examples/kitti/ingest.py --data-root "$FRIDGE_DATA_ROOT/kitti" \
  --limit 0 --device cpu --on-duplicate update
```

`error` refuses duplicate paths.
Numba and Matplotlib caches default under `<data-root>/.cache/`.

The first parent image is checked through the media API, including on resume;
a 403 stops the run with instructions to fix the served root. Committed batches
remain available. The final counts distinguish image/object writes and skips
regardless of whether embeddings were enabled, and report `objects_without_extent`.

## Inspect and verify

- `ground_truth` holds normalized KITTI 2D boxes, including DontCare.
  `ground_truth_3d` holds cuboids with stable `<frame>-<line>` IDs and the original
  annotated `bbox_2d`. `calibration` retains P2, image size, the composed
  `lidar->camera` transform and raw rectification/sensor transforms.
- Object rows use projected crops under `derived/objects/<parent-id>/`, with
  parent/cuboid/field links, `crop_bbox`, `bbox_2d`, class, difficulty, occlusion,
  truncation, dimensions, distance and yaw. They inherit GPS, heading, capture
  time, speed, altitude and attribution. `num_lidar_points` is present with LiDAR.
  Boxes fully outside the image stay on the parent and are counted/reported;
  they cannot have a visible crop or object row.
- Image summaries include `num_cuboids`, `num_dontcare`, `num_easy`,
  `num_moderate`, `num_hard`, `num_unrated`, `num_occluded_0` through
  `num_occluded_3`, `occlusion_max`, `truncation_mean` and `truncation_max`.
  Difficulty follows the **devkit evaluator's strict height thresholds**:
  easy >40 px, occlusion ≤0, truncation ≤0.15; moderate >25 px, ≤1, ≤0.3;
  hard >25 px, ≤2, ≤0.5. Levels are **exclusive**, assigned to the easiest
  qualifying level. The benchmark Moderate set includes Easy; filtering only
  `difficulty=moderate` does not reproduce that benchmark set.
- GPS includes `drive`, `date`, `raw_frame`, `gps_available`, `captured_at`,
  `oxts_timestamp_raw`, `timestamp_timezone`, `speed_kmh` and `altitude_m`.
  Location is longitude/latitude; compass heading is `(90 - yaw_degrees) % 360`.
- Embeddings use `clip` (or `--field-name`) and `clip_obj`, each reduced separately.
  `lidar` contains an absolute path, float32 x/y/z/intensity format, point count,
  frame and attribution. The app does not display point clouds yet; there is no
  point-cloud serving, media switcher or bird's-eye view.

Before upload, a projection self-check compares visible, untruncated boxes
against their annotated 2D boxes, excluding Pedestrians whose annotations differ
from their 3D extent. It fails below median IoU 0.9 **only with at least 20 eligible
boxes**; smaller selections report “not measurable”.

```bash
python examples/_shared/verify.py --kind kitti --dataset-name kitti \
  --api-url "$FRIDGE_API_URL" --expected-count 100 --expect-reduced
```

The shared verifier checks both grains, cuboid/crop links and projections, media,
GPS/map points, schema/display types, embedding counts and optional UMAP coverage.
For imports made with `--skip-embeddings`, pass that flag to the verifier too.
`--expected-count` counts images, not image plus object rows.

## GPS cache and time

Object frame *i* maps through the 1-based `train_rand[i]` entry to
`train_mapping.txt`, which identifies the raw drive and frame. Fetch mode reads
the raw ZIP tail, central directory and contiguous OXTS span using HTTP Range.
It keeps **only** `oxts/timestamps.txt` and `oxts/data/NNNNNNNNNN.txt`; directory
entries, `dataformat.txt` and other members are ignored. It refuses ignored
ranges, noncontiguous sensor data and oversized reads. No full raw ZIP is fetched.
A complete cache usually needs 3–4 requests per drive, about 12 MB across all
141 drives (measured: the full cache took 3.5 minutes). Transient HTTP/transport errors get three attempts with 1 s and 2 s
backoff. Failed drives do not prevent attempts for the remaining drives.

Each completed drive is atomically saved in
`<data-root>/oxts-cache/<date>_drive_<NNNN>_sync.json` with `version: 1`, `drive`,
`source_url` and a `members` dictionary of original packet/timestamp text.
Valid cached drives make zero requests. Corrupt or old-version cache entries
are refetched; failed fetches preserve completed caches and return failure so a
rerun can retry. Ordinary import never fetches: missing/invalid GPS leaves frames
present with `gps_available=false`. Copy `oxts-cache` to import offline.

**Time is CEST / Europe/Berlin local time**, corroborated by sun position in a
reviewed frame. The source text has no timezone; `captured_at` attaches
`+02:00` for KITTI recording dates and truncates nanoseconds to microseconds
without rounding. `oxts_timestamp_raw` preserves
all nine fractional digits followed by ` (Europe/Berlin local)`, keeping it a
string instead of allowing backend datetime detection to reinterpret it as UTC.
The cache retains the exact original timestamp lines.

## Attribution and license

[KITTI Vision Benchmark Suite](https://www.cvlibs.net/datasets/kitti/), by Andreas
Geiger, Philip Lenz, Christoph Stiller and Raquel Urtasun, is licensed under
[CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/).
**Non-commercial use only.** Preserve attribution and the same license on crops
and other derivatives. The KITTI website asks you to register and accept its terms
before downloading; the downloader fetches the same official ZIPs directly, so
accept those terms first. Image/object rows, the dataset description and LiDAR
references carry attribution. No KITTI-derived data belongs in this repository.

Please cite Geiger, Lenz and Urtasun, “Are we ready for Autonomous Driving? The
KITTI Vision Benchmark Suite,” CVPR 2012; and Geiger, Lenz, Stiller and Urtasun,
“Vision meets Robotics: The KITTI Dataset,” IJRR 2013.

## Offline tests

```bash
PYTHONPATH=python:. python -m pytest -q examples/kitti/test_kitti.py
```

Fixtures are synthetic; Range tests use only localhost. The example CI suite
includes these tests and runs `ingest.py --help` without PYTHONPATH.
