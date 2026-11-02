# iNaturalist 2021 validation: species on the map

Import the 100,000-image, 10,000-species validation split from
[Visipedia's iNaturalist competition](https://github.com/visipedia/inat_comp/tree/master/2021).
Each sample contains a species classification, taxonomy, attribution, observation time,
GPS location and a 512-dimensional CLIP embedding in the same ingest transaction.
The map works immediately; the script refreshes filter statistics and optionally runs UMAP.

## Perceptual hashes and duplicates

Imports include `dhash_8` and `phash_8` on image rows so the **Duplicates** panel
and **collapse duplicates** work for matching images. Object rows receive no hashes.
Use `--hash-algorithms dhash_8 phash_8` to choose algorithms or `--no-hashes` to omit
them; both flags also work with `examples/import_demo_data.py`.

Resume skips existing rows without rehashing. Backfill an older dataset from the
repository root using [the SDK recipe](../sdk/compute_hashes.py), with API credentials
and access to the media files:

```bash
python examples/sdk/compute_hashes.py inaturalist-2021-val --algorithms dhash_8 phash_8
```

Backfill skips objects and images with all requested hashes, computing only missing
fields. See [shared hash options](../README.md#perceptual-hashes-and-duplicates).

## Source, licenses and attribution

The actual `val.json` has `info`, `images`, `categories`, `annotations`, and `licenses`.
Images carry `date`, `latitude`, `longitude`, `location_uncertainty`, `license`, and
`rights_holder`. Annotations join `image_id` to `category_id`; array order is irrelevant.
Species names come from `categories.name`, common names from `categories.common_name`.

There is **no single image license**. The IDs in this release are:

| ID | License | Images in the supplied metadata |
|---|---|---:|
| 0 | CC BY 4.0 | 5,227 |
| 1 | CC BY-NC 4.0 | 89,150 |
| 2 | CC BY-NC-ND 4.0 | 1,591 |
| 3 | CC BY-NC-SA 4.0 | 1,365 |
| 4 | CC0 1.0 | 1,866 |
| 5 | CC BY-ND 4.0 | 101 |
| 6 | CC BY-SA 4.0 | 700 |

`--licenses no-derivatives` is the default: every license except the two NoDerivatives ones
(IDs 2 and 5, 1,692 images), because the app serves resized thumbnails and crops. The demo is
non-commercial, so NC images are included with their attribution. `--licenses all` imports
everything; `--licenses 0,4,6` keeps only licenses that allow commercial use and derivatives.
IDs are validated against the JSON license table.
With all JPEGs available, `--limit 0` and the default license selection import **97,992 images**:
100,000 minus 323 without GPS minus 1,685 NoDerivatives images with GPS. Seven of the
1,692 NoDerivatives images also lack GPS, so they are counted only in the GPS exclusion.
Use a **new dataset name** when narrowing the license selection: reruns do not remove
previously imported samples. A public site's chosen image policy remains a publishing decision.

Credit the dataset as “iNaturalist 2021, Grant Van Horn and the Visipedia Team.” For each
displayed image retain its `rights_holder`, license name and license URL. The stored attribution
text is “`<rights_holder>; iNaturalist 2021; <license_url>`”. Original images are served unchanged;
the script only decodes them in memory for CLIP. License names/URLs are taken from the source
table, including CC0's public-domain dedication URL.

## Download provenance and layout

These are the exact URLs in `examples/download_demo_data.py`:

- Metadata: <https://ml-inat-competition-datasets.s3.amazonaws.com/2021/val.json.tar.gz>
- Images: <https://ml-inat-competition-datasets.s3.amazonaws.com/2021/val.tar.gz>

The importer **does not download or extract anything**, and never writes under `--data-root`.
The orchestrator extracts both archives into:

```text
$FRIDGE_DATA_ROOT/inaturalist/
├── val.json
├── val.json.tar.gz                 # optional retained archive
├── val.tar.gz                      # optional retained archive
└── val/
    └── 03938_Animalia_Chordata_Aves_Passeriformes_Meliphagidae_Ptilotula_penicillata/
        └── df8edd4c-fbb4-4886-8600-a429e5efac23.jpg
```

The supplied metadata contains **323 images without coordinates**. They are skipped and
counted; no image receives a fabricated point. Invalid/out-of-range coordinates are also
skipped. Missing files, unreadable/partially written images and license exclusions have
separate counters. Startup lists the number of JPEGs currently available.

## Labels and filterable fields

| Source | Sample destination/type |
|---|---|
| `categories.name` | `species`: `Classification` object (overlay); `scientific_name`: string (exact filter) |
| Category taxonomy | `kingdom`, `phylum`, `class`, `order`, `family`, `genus`: strings |
| Other category attributes | `common_name`, `specific_epithet`, `supercategory`, `image_dir_name`: strings |
| Image `date` | `date`: UTC datetime (missing stays null; timezone-less values assume UTC) |
| Image latitude/longitude | `location=(longitude, latitude)` plus numeric `latitude`, `longitude` fields |
| Image `location_uncertainty` | Nullable number, preserved in source units |
| Image license ID + license table | `license_id`: number; `license`, `license_url`: strings |
| Image attribution | `rights_holder`, `attribution`: strings |
| Source identity | `image_id`, `annotation_id`, `category_id`: numbers; `file_name`: string |
| Image dimensions | `width`, `height`: numbers |
| CLIP features | `embeddings[--field-name]`, default `clip` |

All metadata is top-level so it is filterable. Toggle `species` in Overlays to render the
classification; filter on `scientific_name` (exact match with typeahead) to pick a species.
Explicit field types also cover columns that happen to be all null in a small
subset. JSON arrays are decoded incrementally using the standard library; only category,
license and annotation lookup tables are retained, plus one image/embedding batch.

## Run

Use the repository SDK (`python/fridge`, added to the import path by the example), Python
3.12+, Pillow, torch and transformers (`uv sync` installs all of them). Tests additionally use pytest. Both CLIP processor
and weights must already be in the local HF cache, or pass a local model directory. Loading
uses `local_files_only=True`. The backend must serve the same absolute paths beneath its
`FRIDGE_DATA_ROOT`; no images are uploaded/copied by this example.
After the first written batch, the importer fetches an image through the API. A 403 stops
the run with a `FRIDGE_DATA_ROOT` diagnostic; that first batch remains committed for resuming.

Preferred: set `FRIDGE_API_TOKEN` to an API token created in **Settings → API Tokens** (revocable and expiring; no password involved); the importer then skips the password login.
Set `FRIDGE_USERNAME` (defaults to `admin`) and `FRIDGE_PASSWORD` in the environment for
both the importer and verifier. Without a password, an interactive terminal prompts securely;
noninteractive runs fail with a clear message. There is no default password. Complete the first-login
password change in the app before importing.

From the repository root, against a backend you intend to populate:

```bash
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 python examples/inaturalist/ingest.py \
  --data-root $FRIDGE_DATA_ROOT/inaturalist \
  --dataset-name inaturalist-2021-val --limit 100 \
  --device cpu --batch-size 16 --api-url "$FRIDGE_API_URL" --no-reduce
```

The default limit is **100**. For a full import with the default license selection:

```bash
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 python examples/inaturalist/ingest.py \
  --data-root $FRIDGE_DATA_ROOT/inaturalist \
  --dataset-name inaturalist-2021-val --limit 0 \
  --batch-size 32 --api-url "$FRIDGE_API_URL"
```

The limit applies after license/GPS/file checks, in source JSON order. An unreadable selected
file may leave the run below the limit until retried. Counters scan all image records even
after reaching the limit. As extraction progresses, the available subset can change; reruns
add newly selected files and never delete old ones, so a growing download can make the final
dataset larger than a limited run's target. For a fixed exact subset, wait for extraction.

Shared flags: `--field-name clip`, `--model openai/clip-vit-base-patch32`, `--device`
(auto CUDA/CPU), `--batch-size 32`, optional `--username`/`--password` overrides,
`--on-duplicate {skip,update,error}`, and `--no-reduce`.
`--api-url` defaults to `FRIDGE_API_URL`, otherwise `http://localhost:8000/api`.

Default duplicate mode `skip` looks up each batch's filepaths **before loading images or CLIP**.
Committed batches survive interruption; rerun the same command to resume. `update` recomputes
embeddings and refreshes sample values; `error` stops when a duplicate is found (earlier batches
may already be committed). Missing/unreadable files are retried on subsequent runs. Keep the
data root, field name and model consistent across resumptions; changing the model requires
`update` or a new dataset/field. Stats refresh runs even on a completed rerun.
If no samples were written, reduction is skipped to preserve the existing map layout.

Omit `--no-reduce` to build the map client-side at any dataset size: `compute_visualization`
scales the UMAP fit to available RAM and places every embedded row. Map failures are
reported separately from successful ingestion, with a command to retry. To rebuild a map
for an older dataset in an authenticated SDK session, run
`Dataset("inaturalist-2021-val").compute_visualization(field_name="clip")` with your dataset name. CPU thread
counts above match the smoke benchmark; tune threads and batch size for your hardware.
The shared batch/CLIP helper is [shared importer](../_shared/importer.py), used by every importer.

## Verification and capacity

```bash
python -m pytest -q examples/inaturalist/test_inaturalist.py
python examples/_shared/verify.py --kind inaturalist \
  --dataset-name inaturalist-2021-val --api-url "$FRIDGE_API_URL" --expected-count 100
```

The API verifier checks every declared metadata field with an actual filter query, the
datetime type, sample/embedding counts and exact coordinates returned by `map_points`.
Measurements with four CPU threads and batch size 16 reached **22–28 images/s**
across these importers (iNaturalist: 27.7 images/s, including decoding). Budget roughly
**1.0–1.3 CPU hours** for the full 97,992-image selection, plus UMAP. A CUDA smoke imported
1,000 images in 28.6 seconds, including about 17 seconds scanning all metadata; extrapolating
the remaining work gives **20–25 minutes on GPU** for the full iNaturalist selection.
These are estimates, not full-import benchmarks. A duplicate-only rerun still scans all
100,000 metadata records, but loads no CLIP model and skips reduction. Budget another
10–60 minutes for roughly 100k-point UMAP depending on importer hardware.

Disk: 8.93 GB compressed image archive, **8.98 GB measured extracted JPEGs**, 43 MiB
JSON plus its 9.4 MiB archive; budget **18–21 GB if retaining archives**. Allow an additional
**0.5–2 GB of Postgres data/indexes per 100k samples**, excluding WAL/backups and the existing
1.2 GB CLIP cache. Images are not duplicated, and the importer creates no derived files.
