# Demo data for Freedom Ridge

Start with COCO to explore boxes, masks, poses, captions and image/object similarity.
Add geolocated datasets for the map, or KITTI for calibrated 3D boxes. Downloads
and imports are separate, resumable commands.

## Hosted-demo path

The hosted demo uses four datasets: COCO val2017, Open Images V7 validation,
iNaturalist 2021 validation and KITTI object training. Complete the
[prerequisites](#prerequisites) first, then run from the repository root:

```bash
python examples/download_demo_data.py --datasets coco,open_images,inaturalist,kitti
# KITTI GPS cache: needs the KITTI ZIPs above; run it before ANY KITTI import.
python examples/kitti/ingest.py --data-root "$FRIDGE_DATA_ROOT/kitti" --limit 0 --fetch-oxts-only
python examples/import_demo_data.py --datasets coco,open_images,inaturalist,kitti --limit 100
python examples/import_demo_data.py --datasets coco,open_images,inaturalist,kitti --full
```

The KITTI GPS step fetches about 12 MB of raw-drive GPS (OXTS) data and makes no API
calls. Run it before the first KITTI import, including the 100-image try. Without
it, KITTI imports with no GPS and no map points, and the runner still reports PASS;
the KITTI log only says `N frames lack cached GPS`. The [KITTI guide](kitti/README.md#run)
shows how to repair an import made without it. The runner always imports in the
order COCO, iNaturalist, Open Images, KITTI. The last command resumes the same
datasets to full size, skipping completed images and objects. A public deployment
then computes 4×4 patches and builds the search and map indexes:
[Deploy to AWS, section 9](../docs/DEPLOY_TO_AWS.md#9-compute-44-patches).

What to expect for the four datasets with the default license selections:

- **Download:** about 66 GB (COCO 1.07, Open Images 14.22, iNaturalist 8.94,
  KITTI 41.35 GB, of which 28.75 GB is LiDAR). That is about 10 minutes at 1 Gbit/s,
  or 1.5 hours at 100 Mbit/s.
- **Disk:** about 112–115 GB for archives plus extracted media (table below). Imports
  add Open Images and COCO crops and masks, about 520,000 database rows with vectors,
  indexes and WAL, the 1.2 GB CLIP cache and the Python environment. Plan at least
  160 GB, or 200 GB to be comfortable.
- **CPU import:** about 520,000 CLIP inputs (150,433 images and 369,188 object crops).
  At 22–28 inputs/s with four threads that is about 5–7 hours of encoding, plus map
  building (UMAP). Plan 7–9 hours. COCO takes about 20 minutes, iNaturalist 1–1.2
  hours, Open Images 3.4–4.4 hours and KITTI about 30–40 minutes plus LiDAR extraction.
- **GPU import:** encoding drops to about 2 hours; UMAP still runs on the CPU. Plan
  3–4 hours. A GPU is optional; ViT-B/32 is small.
- **RAM:** importers use bounded memory, but the map fit for Open Images' 304,000
  object crops wants about 11 GB free (see [How much memory does a map need?](#how-much-memory-does-a-map-need)).
  On one machine that also runs PostgreSQL, the backend and the frontend, 16 GB is
  the minimum and 32 GB is comfortable.
- **Result:** datasets `coco-val2017`, `open-images-v7-val`, `inaturalist-2021-val`
  and `kitti`. KITTI shows projected 3D boxes on images, map points from its GPS and
  an objects view. Its LiDAR is stored as file references only; the app has no
  point-cloud viewer.

These are estimates from the per-dataset measurements below, not a timed run of
all four together. The 4×4 patches on a public deployment add another 719,360
encodings.

## Prerequisites

1. Run the app and finish the first-login password change. See the
   [quick start](../docs/QUICK_START.md) for development, or the deploy guides for a
   server.
2. Set `FRIDGE_DATA_ROOT` to an absolute directory the backend can read, and
   `mkdir -p "$FRIDGE_DATA_ROOT"`. Importers store absolute paths, and the backend
   serves only files below its own `FRIDGE_DATA_ROOT`, so both must see the media
   at the **same absolute path**. For development, `export FRIDGE_DATA_ROOT="$PWD/data"`
   in both terminals. With the production Docker stack, set `FRIDGE_DATA_ROOT` and
   `DATA_VOLUME_PATH` in `.env` to the same absolute directory and export that path
   here; the AWS guide uses `/data/datasets`. **Export** it: the downloader reads
   `FRIDGE_DATA_ROOT` only from the shell, not from `--env-file`. The runner refuses
   to start without an existing root. With [media in S3](#media-in-s3) the backend
   never reads this directory, so it can be on another machine.
3. Create an API token in **Settings → API Tokens** and export `FRIDGE_API_TOKEN`.
   Alternatively set both `FRIDGE_USERNAME` and `FRIDGE_PASSWORD`. There is no
   importer default password. Direct importers can prompt in a terminal; the
   unattended runner requires credentials up front. The runner's `--env-file import.env`
   reads these variables from a file[^dotenv]. Credential `*.env` files are
   gitignored; protect them with `chmod 600 import.env`.
   Set `FRIDGE_API_URL` to your API: `http://localhost:8000/api` for development, or
   `https://YOUR_DOMAIN/api` for the production Docker stack, which publishes no
   port 8000 and refuses `http://localhost`. Every API recipe also accepts `--api-url`.
4. Install the locked Python environment from the repository root (the same one
   the backend uses, so UI CLIP text search works too). The CPU installation is also
   what CI uses:

   ```bash
   uv sync --extra cpu
   source .venv/bin/activate
   ```

   For NVIDIA GPUs use `uv sync --extra cuda` instead. It installs PyTorch's CUDA 12.6
   build, which needs NVIDIA driver 560 or newer (check with `nvidia-smi`); with an
   older driver use the CPU installation. The GPU environment takes roughly **7 GB**;
   the CPU environment is smaller. [Updating dependencies](../docs/DEPENDENCIES.md) covers newer
   CUDA builds.

5. Cache the exact CLIP model **once, on a connected machine**, in the same user's
   Hugging Face cache used by the importers. Allow **1.2 GB** for the model download
   (both weight formats may be cached):

   ```bash
   python -c "from transformers import CLIPModel, CLIPProcessor; CLIPModel.from_pretrained('openai/clip-vit-base-patch32'); CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')"
   ```

   Copy that cache to an offline machine if necessary. Importers use
   `local_files_only=True`; they do not download weights. A **GPU is optional**:
   `--device auto` checks CUDA execution and warns when NVIDIA hardware is present
   but CUDA cannot run, then uses CPU. On large CPU hosts,
   `OMP_NUM_THREADS=4 MKL_NUM_THREADS=4` avoids oversubscription.

[^dotenv]: `--env-file` uses python-dotenv quoting: `#` within a value and unquoted
    backslashes are preserved; whitespace before `#` starts an unquoted comment.
    Single/double quotes and `export KEY=value` are supported. Variables and shell
    commands are never expanded. Existing environment variables win.

## Choose a dataset

Image counts below are the full available/importable selection. “Objects” counts
annotations; COCO, Open Images and KITTI also materialize object rows. Other
importers keep their labels on image rows. Import times exclude download and UMAP
unless stated. CPU/GPU figures come from the per-dataset READMEs' measurements, not
promises for your hardware. **Unmeasured** means no comparable end-to-end timing exists.

| Dataset | What it demonstrates | Images; objects/labels | Download | CPU import | GPU import | Data license; public hosted demo |
|---|---|---|---|---|---|---|
| [COCO val2017](coco/README.md) | Boxes, polygons/masks, keypoints, skeletons, captions, objects view, image/object/text similarity | 5,000; 36,781 boxes (446 crowds). Default license selection: 3,340; 24,645 boxes | 1.07 GB download; **2.6 GB on disk** with ZIPs retained | Full image+object run unmeasured; small try takes minutes | Full run unmeasured; benchmark 100 first | Annotations CC BY 4.0; per-photo Flickr licenses. Default excludes NoDerivs photos (license IDs 3 and 6), because the app derives thumbnails, crops and patches; includes NC. **Non-commercial hosted demo only** with attribution under this selection. |
| [Open Images V7 validation](open_images/README.md) | Boxes, overlapping masks, point labels, relationships, negative labels, narratives/captions, objects view and optional patch search | 41,620; 303,980 boxes, 24,730 masks | 14.22 GB including narratives | Unmeasured for ~346k image+crop inputs | Unmeasured | Annotations CC BY 4.0; photos normally CC BY 2.0. **Conditional**: retain each photo's license, title, author and source link. |
| [iNaturalist 2021 val](inaturalist/README.md) | GPS/map, taxonomy/species, dates, attribution, similarity, duplicate observations | 97,992 eligible of 100,000; no object rows | 8.94 GB | 1.0–1.3 h estimate at 22–28 images/s | 20–25 min estimate; 1,000-image smoke 28.6 s | **Per-photo licenses**. Default excludes NoDerivatives, includes NC. **Non-commercial hosted demo only** with attribution under this selection; `--licenses 0,4,6` narrows to commercial/derivative-compatible photos. |
| [OSV-5M test](osv5m/README.md) | GPS/map, country-spread sampling, capture time, street/environment facets, similarity, duplicate sequence frames | 210,122; no object rows | 9.54 GB including 111.1 MiB CSV | 2.1–2.7 h estimate at 22–28 images/s | 1,000-image smoke 27.4 s; full run unmeasured | CC BY-SA 4.0. **Conditional**: contributor attribution and share-alike requirements. |
| [EuroSAT multispectral](eurosat/README.md) | Georeferenced satellite map, land-cover classes, RGB renders, similarity/concepts | 27,000; no object rows | 2.07 GB | Unmeasured | Full 27k import verified; elapsed time unrecorded | MIT release. **Yes**, retain license and attribution. |
| [KITTI object](kitti/README.md) | 3D boxes on images, 2D boxes, GPS/map, objects view, similarity, duplicate drive frames, LiDAR stored as file references | 7,481 labelled training images; 40,570 cuboids, 11,295 DontCare regions, 40,563 objects; also 7,518 test images | **41.35 GB retained ZIPs**, always including the 28.75 GB LiDAR ZIP; training extracts ~6 GB PNG + 14 GB BIN + 0.36 GB crops | 100 images + 435 crops in 24.6 s; full ~30–40 min extrapolated, excluding UMAP | Full run unmeasured; ~5 min CLIP design estimate plus extraction/upload/UMAP | **CC BY-NC-SA 3.0; non-commercial only**, attribution and share-alike; not for a commercial hosted demo. |
| [TuSimple lanes + velocity](tusimple/README.md) | Lane polylines, vehicle boxes, per-vehicle motion and same-object filters, similarity, duplicate clip frames | 6,408 lanes frames + 6,410 velocity frames; 23,747 lane polylines; velocity train 1,442 + supplement 29,115 boxes (plus test) | ~27.84 GB original archives | 5–10 min per dataset estimate, plus UMAP | Velocity 1,000-frame smoke 28.7 s; full run unmeasured | Dataset hosting permission unestablished; code Apache 2.0 does not establish data rights. **Local only.** |
| [BDD100K](bdd100k/README.md) | Driving boxes/polygons, weather/scene/time facets, optional GPS/heading, similarity | 100,000 keyframes; imports available annotations; box count varies | ~7 GB + labels/optional telemetry | Unmeasured | Unmeasured | Account-gated non-commercial research terms. **Not a default public hosted demo**; obtain appropriate permission. |

The license column summarizes each source's documentation; an importer does
not grant hosting rights. Keep source attributions visible. Use a **new dataset
name** when narrowing licensing rules: a resumable rerun never deletes old rows.
UMAP adds time: GPS datasets need 10–60 minutes around 100k points. COCO, KITTI and
Open Images reduce `clip` and `clip_obj` separately. Plan disk for archives **and**
extractions, derived masks/crops, CLIP cache and database indexes/WAL. At the
usual eight connections, assembly needs at most one extra range (~one eighth of
an archive), then removes each consumed part. A single connection needs no extra
assembly copy. The downloader prints and checks remaining download/assembly space
before each transfer, and exact missing-member space before extraction. It refuses
an operation that does not fit. This does not reserve space against other processes.

| Dataset | Plan for downloaded archives plus extraction (before import/database) |
|---|---|
| COCO | 2.6 GB |
| Open Images | About 30 GB; allow more for derived crops/masks |
| iNaturalist | 18–21 GB |
| OSV-5M | 19–22 GB |
| EuroSAT | About 6 GB; RGB renders are additional |
| KITTI | 41.35 GB retained ZIPs + ~20.4 GB training media/crops; at eight connections allow ~3.6 GB download assembly headroom |
| TuSimple | 27.84 GB sources plus 1.76 GB selectively extracted frames |
| BDD100K | Provider download/extraction sizes vary; budget separately for optional telemetry |

Per-dataset READMEs include measured import/database figures. The downloader
fetches KITTI only when it is named, always including the LiDAR ZIP, and never
extracts it; the importer extracts only the selected frames.

## Quick try: COCO

COCO validation has **5,000 images**, 3,340 of them under the default license
selection. Start with 100 images in minutes; then expand the same dataset to the
full selection. From the repository root, with the prerequisites above configured:

```bash
python examples/download_demo_data.py --datasets coco
python examples/import_demo_data.py --datasets coco --limit 100
python examples/import_demo_data.py --datasets coco --full
```

The last command skips completed images and objects, then adds the remaining
selection. The full run also embeds 24,645 crops, so allow longer than the quick
try. Open `coco-val2017` in the app; switch between images/objects, inspect overlays,
search captions and explore image/object similarity. All source media remain local.

## More datasets

Inspect the choices without making any requests:

```bash
python examples/download_demo_data.py --list
```

OSV-5M and EuroSAT add more map data and are not part of the hosted demo. Download
the automatic sources, then import selected datasets. The runner orders COCO,
iNaturalist, OSV-5M, Open Images, KITTI, EuroSAT, TuSimple lanes/velocity, BDD100K.
Choose licenses and hosting scope before including restricted data.

```bash
python examples/download_demo_data.py --datasets osv5m,eurosat
python examples/import_demo_data.py --datasets osv5m,eurosat --full --device auto
```

`--all` includes **every** importer, including local-only TuSimple and account-gated
BDD100K, so acquire those manually first. It does not grant permission to host them.
By default TuSimple archives go under `$FRIDGE_DATA_ROOT/tusimple/TusimpleLaneDetection` and
`.../TusimpleVelocity`; BDD100K goes under `$FRIDGE_DATA_ROOT/bdd100k`. Open Images
uses the `open_images_v7` directory.
Override TuSimple sources without moving archives:

```bash
python examples/import_demo_data.py --datasets tusimple --limit 100 \
  --tusimple-lanes-source /path/to/TusimpleLaneDetection \
  --tusimple-velocity-source /path/to/TusimpleVelocity
```

The equivalent variables are `FRIDGE_TUSIMPLE_LANES_SOURCE` and
`FRIDGE_TUSIMPLE_VELOCITY_SOURCE` (also accepted in `--env-file`). Explicit flags
win. Sources are read only; selected frames are extracted into
`$FRIDGE_DATA_ROOT/tusimple/{lanes,velocity}/`.

Each importer also runs directly, with dataset-specific options such as licenses,
splits and dataset names. EuroSAT's limit is **per class**; BDD100K's is **per
split**, so the runner's 100 may produce 1,000 and 300 images, respectively.
BDD100K's `--data-root` is the parent of its `bdd100k/` folder.
Run `python examples/<dataset>/ingest.py --help` for details.

## Perceptual hashes and duplicates

Every demo importer includes `dhash_8` and `phash_8` perceptual hashes on image rows
in the same upload as labels and embeddings. The **Duplicates** panel and **collapse
duplicates** work immediately when images share hashes, including repeated frames
in OSV-5M sequences, TuSimple clips and KITTI drives, or repeated iNaturalist observations.
Object crops are excluded. Open Images hashes its saved rotated JPEGs; EuroSAT hashes
its rendered RGB JPEGs. Both match the SDK's RGB conversion without EXIF transposition.

The runner and each direct importer accept `--hash-algorithms dhash_8 phash_8`
(the default) or `--no-hashes` to omit them. Named algorithms also include `ahash_N`
and `whash_N`; `N` is the hash size. A normal resume skips existing rows without
rehashing them. To backfill an older dataset, run from the repository root with
API credentials and access to its media files:

```bash
python examples/sdk/compute_hashes.py coco-val2017 --algorithms dhash_8 phash_8
```

Backfill skips object rows and images that already have all requested hashes;
partially hashed images compute only missing fields. Reruns do not open complete
images. To intentionally replace a hash field, use the SDK's
`Dataset(name).compute_hashes(overwrite=True)`.

## Media in S3

By default importers record local paths, which the backend serves from its own
`FRIDGE_DATA_ROOT`. With `--media-uri s3://BUCKET/PREFIX`, an importer still downloads
and processes the files locally, then uploads each batch's media (images, object crops,
masks and KITTI LiDAR) under that prefix before saving the batch, and records `s3://`
paths. The backend reads them from S3 ([media storage](../docs/S3_PERMISSIONS.md)). The
runner adds each dataset's folder to the prefix:

```bash
AWS_PROFILE=YOUR_PROFILE python examples/import_demo_data.py \
  --datasets coco,kitti --full --media-uri s3://YOUR_MEDIA_BUCKET/media \
  --api-url https://YOUR_DOMAIN/api
```

Uploads use boto3's usual credentials (`AWS_PROFILE` or environment variables) and need
`s3:PutObject` on the prefix. The data root can be anywhere, for example on a GPU
workstation, because the backend never reads it. Keys mirror the paths below the data
root, so rerunning with the same data root and prefix resumes: saved rows are skipped
and nothing is uploaded again. If the AWS login expires, the importer stops and says
so; log in again and rerun. Keep the local files until the import and its checks pass.
Running an importer directly, give each dataset its own prefix. The first batch checks
that the backend can read the bucket; a 403 means the bucket is not in its `S3_BUCKET`
setting or a storage configuration. BDD100K (which records its own S3 paths with
`--s3-bucket`) and TuSimple (whose terms keep it local) have no `--media-uri`.

## Resume, logs and verification

Downloads use parallel HTTP ranges, verify Content-Length and available published
MD5/SHA-256 headers and pinned release checksums, and preserve partial ranges
across interruption. Ctrl-C stops workers promptly; rerun to resume. Complete
files have `.download.json` verification receipts for offline reruns; extraction
receipts avoid rescanning unchanged archives when all recorded members exist.
Delete a receipt to force a fresh check. Symlinks leading to the data root are
supported; symlinks below that resolved root are refused. Archive
extraction rejects traversal, links and special files and **never overwrites** an
existing file. A remote validator change or wrong-size existing archive stops with
an error; inspect the named archive/`.parts` directory before removing it yourself.
Checksums unavailable from the source cannot be invented. The offline fixture tests
do not contact the public URLs.

Imports look up stable filepaths before loading CLIP, keep committed batches, and
resume interrupted object generation. The first batch checks actual backend media
access (including on resume); a 403 usually means a mismatch with backend
`FRIDGE_DATA_ROOT`, or with `--media-uri`, a bucket the backend may not read. Importers build embedding maps client-side with
`Dataset.compute_visualization()`: the UMAP fit scales to available RAM and every embedded
row gets coordinates, at any dataset size. This also covers image and object fields separately.
`--no-reduce` skips map building; a rerun that writes nothing keeps the existing map
(KITTI can recover pending coordinates). Scheduled, managed maps are left to their re-fit jobs.
Map failures leave imported rows intact. GPS and EuroSAT importers print a warning and a
command to retry; other importers fail. The runner continues other selected datasets and
returns failure if any importer failed. Logs and `status.txt` default to
`$FRIDGE_DATA_ROOT/.import-logs/<UTC timestamp>/`; override with `--log-dir`.

To rebuild a map for an older dataset, using the same API URL and credentials:

```bash
python examples/sdk/compute_visualization.py DATASET_NAME clip --api-url "$FRIDGE_API_URL"
```

Use `clip_obj` for object crops. In an authenticated SDK session, the equivalent is
`Dataset("DATASET_NAME").compute_visualization(field_name="clip")`.

### How much memory does a map need?

The map is fitted on the machine that runs the import (or the SDK call), not on the
server. The fit uses up to 75% of that machine's free RAM: it fits UMAP on as many
points as fit, then places **every** point on the map. A smaller machine rarely runs
out of memory: it fits on a random sample (at least 50,000 points), so the layout is
slightly less faithful but complete. For the 512-dimension CLIP maps here, each fitted
point costs about 27 KB:

| Machine | Free RAM (typical) | Fits UMAP on up to |
|---|---|---|
| 16 GB laptop | ~10 GB | ~280,000 points |
| 32 GB desktop | ~24 GB | ~675,000 points |
| 128 GB workstation | ~80 GB | ~2.3 million points |

Every demo dataset fits completely on a 16 GB machine, except Open Images' 304,000
object crops, which fit on about 280,000 and place all of them. Machines with more
memory matter for datasets in the millions. The importer prints the fit size it
chose (`Auto fit-size: ... fit UMAP on N/M`). To fit a big dataset on a bigger
machine, run `examples/sdk/compute_visualization.py` there against the same API URL.

A single read-only verifier covers all datasets. For example, after 100-image
COCO and iNaturalist imports (COCO/Open Images verification also requires completed UMAP):

```bash
python examples/_shared/verify.py --kind coco --dataset-name coco-val2017 \
  --api-url "$FRIDGE_API_URL" --expected-count 100
python examples/_shared/verify.py --kind inaturalist --dataset-name inaturalist-2021-val \
  --api-url "$FRIDGE_API_URL" --expected-count 100 --expect-reduced
```

Offline fixture tests use synthetic data and a localhost range server:

```bash
env -u PYTHONPATH python -m pytest examples -q
```

General SDK recipes, including optional **patch embeddings**, are indexed in
[sdk/README.md](sdk/README.md). Artificial million-row and patch load tests live in
[scripts/scale_tests](../scripts/scale_tests/README.md). They are separate from this
real-data demo kit.
