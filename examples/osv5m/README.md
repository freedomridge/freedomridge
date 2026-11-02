# OSV-5M test: street imagery on the map

Import [OpenStreetView-5M](https://huggingface.co/datasets/osv5m/osv5m)'s **210,122-image test
split**. The default target is 100 samples spread across countries; `--limit 0` selects
all available images. GPS, all CSV metadata and CLIP embeddings arrive in the same sample
transaction. Statistics refresh makes the fields available to the Filters panel.

## Perceptual hashes and duplicates

Imports include `dhash_8` and `phash_8` on image rows so the **Duplicates** panel
and **collapse duplicates** work for matching images. Object rows receive no hashes.
Use `--hash-algorithms dhash_8 phash_8` to choose algorithms or `--no-hashes` to omit
them; both flags also work with `examples/import_demo_data.py`.

Resume skips existing rows without rehashing. Backfill an older dataset from the
repository root using [the SDK recipe](../sdk/compute_hashes.py), with API credentials
and access to the media files:

```bash
python examples/sdk/compute_hashes.py osv5m-test --algorithms dhash_8 phash_8
```

Backfill skips objects and images with all requested hashes, computing only missing
fields. See [shared hash options](../README.md#perceptual-hashes-and-duplicates).

## Source, license and attribution

The downloaded dataset card declares **CC BY-SA 4.0**:
<https://creativecommons.org/licenses/by-sa/4.0/>. Credit the images' Mapillary contributors
and the dataset authors. Each sample's `attribution` includes its `creator_username` (or
“Unknown contributor”), Mapillary, an image link derived from the Mapillary ID, and OSV-5M:

> <creator_username>; Mapillary (https://www.mapillary.com/app/?pKey=<image_id>);
> OSV-5M authors (Astruc et al., CVPR 2024); CC-BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/)

Keep `creator_username`, `creator_id`, `image_id`, `license`, `license_url`, and the attribution
with displayed images. The source `thumb_original_url` is retained but never fetched; its
signed link may have expired. Original local JPEGs are served without rewriting them.

Dataset citation: Guillaume Astruc, Nicolas Dufour, Ioannis Siglidis, Constantin Aronssohn,
Nacim Bouia, Stephanie Fu, Romain Loiseau, Van Nguyen Nguyen, Charles Raude, Elliot Vincent,
Lintao Xu, Hongyu Zhou, and Loic Landrieu, *OpenStreetView-5M: The Many Roads to Global Visual
Geolocation*, CVPR 2024. Source: the locally downloaded dataset card.

## Download provenance and layout

Exact URLs from `examples/download_demo_data.py`:

- <https://huggingface.co/datasets/osv5m/osv5m/resolve/main/test.csv>
- <https://huggingface.co/datasets/osv5m/osv5m/resolve/main/README.md>
- <https://huggingface.co/datasets/osv5m/osv5m/resolve/main/images/test/00.zip>
- <https://huggingface.co/datasets/osv5m/osv5m/resolve/main/images/test/01.zip>
- <https://huggingface.co/datasets/osv5m/osv5m/resolve/main/images/test/02.zip>
- <https://huggingface.co/datasets/osv5m/osv5m/resolve/main/images/test/03.zip>
- <https://huggingface.co/datasets/osv5m/osv5m/resolve/main/images/test/04.zip>

The observed archive **contains a shard directory**. Extracting `00.zip` in `images/test`
produces `images/test/00/<id>.jpg`, with 50,000 images in the first shard:

```text
$FRIDGE_DATA_ROOT/osv5m/
├── test.csv
├── README.md
└── images/test/
    ├── 00.zip                     # optional retained archive
    ├── 00/1000055667540054.jpg
    ├── 01/<id>.jpg                # later shards, as available
    ├── 02/<id>.jpg
    ├── 03/<id>.jpg
    └── 04/<id>.jpg
```

Flattened `images/test/<id>.jpg` is supported too. Startup inventories extracted JPEGs
recursively; duplicate IDs in two paths are rejected. Missing files and invalid/missing GPS
are counted and skipped. No point is fabricated. **No downloads, extraction or data-root
writes occur.** A partial first shard is sufficient for an import.

## Every CSV column is filterable

The actual CSV has 26 columns. All are preserved as top-level metadata, with these types:

| CSV columns | Field/type |
|---|---|
| `id` | `image_id`: string (source ID, distinct from Freedom Ridge's native sample ID) |
| `latitude`, `longitude` | Numbers, also `location=(longitude, latitude)` |
| `captured_at` | UTC datetime, converted from Unix **milliseconds** |
| `road_index`, `dist_sea` | Nullable numbers, retaining source units/scaling |
| `country`, `region`, `sub-region`, `city` | Category strings; `sub-region` becomes `sub_region` |
| `unique_country`, `unique_region`, `unique_sub-region`, `unique_city` | Strings; `unique_sub-region` becomes `unique_sub_region` |
| `land_cover`, `drive_side`, `climate`, `soil` | Categorical code strings, not continuous numbers |
| `lon_bin`, `lat_bin`, `cell`, `quadtree_10_1000` | Categorical grid/code strings |
| `sequence`, `creator_username`, `creator_id`, `thumb_original_url` | Strings, preserving IDs without float conversion |
| Added provenance | `license`, `license_url`, `attribution`: strings |

Numeric category codes are retained verbatim; the importer does not guess class-name
mappings. Only empty text cells become null; `Nan` (Thailand's province/city), `NaN`,
`null`, `None`, and country code `NA` (Namibia) remain text. Numeric missing/non-finite
values become null. Unknown additional CSV columns are retained as strings. The hyphen substitutions
are necessary for the filter API; column-name collisions are rejected. Null-only fields
receive explicit types. OSV provides contextual fields, not a species/object classification,
so no classification label is invented.

## Deterministic sampling and resumability

The importer streams CSV rows, keeping only an ID/path index and country buckets. It sorts
countries and their available image IDs by SHA-256, then selects in round-robin order across
countries until the limit is reached. Small countries exhaust naturally; larger ones fill
the remaining slots. The resulting subset is independent of CSV row order and Python's hash
seed. With a 100-image target and at least 100 available countries, it selects 100 countries.
This deliberately balances country representation rather than reproducing the source mix.

The available-file inventory is fixed at run startup. A rerun against the same extracted
files chooses the same subset. More extracted files can change a limited subset: reruns add
new members and keep old ones, so the accumulated dataset can exceed the limit. Wait for all
shards before selecting a fixed public-demo subset, or use `--limit 0` to resume toward all
images. Invalid/unreadable selected files can leave a run below its target until repaired.

Default `--on-duplicate skip` checks filepaths in batches before image decoding/model loading.
Completed batches survive interruption. `update` recomputes embeddings and refreshes values;
`error` stops on duplicate paths, retaining any earlier successful batches. Keep the data root,
model and embedding field consistent across resumptions. No local checkpoint is needed.

## Run

Requirements: repository SDK in `python/fridge`, Python 3.12+, Pillow, torch and transformers (`uv sync` installs all of them);
pytest for tests. The script adds the local SDK to its import path and uses the shared
helper [shared importer](../_shared/importer.py). CLIP weights and processor
must already be cached (`local_files_only=True`) or supplied as a local model directory.
The backend's `FRIDGE_DATA_ROOT` must include these same absolute image paths.
After the first written batch, an API image fetch checks access. A 403 stops the run with
a `FRIDGE_DATA_ROOT` diagnostic; the first batch remains committed for resuming.

Preferred: set `FRIDGE_API_TOKEN` to an API token created in **Settings → API Tokens** (revocable and expiring; no password involved); the importer then skips the password login.
Set `FRIDGE_USERNAME` (defaults to `admin`) and `FRIDGE_PASSWORD` in the environment for
both the importer and verifier. An interactive terminal prompts securely if no password is
set; noninteractive runs fail. There is no default password. Complete the first-login
password change in the app before importing.

Full import (use `--limit 100` for a smoke run):

```bash
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 python examples/osv5m/ingest.py \
  --data-root $FRIDGE_DATA_ROOT/osv5m \
  --dataset-name osv5m-test --limit 0 \
  --device cpu --batch-size 16 --api-url "$FRIDGE_API_URL"
```

Other defaults: `--field-name clip`, `--model openai/clip-vit-base-patch32`, `--device`
auto CUDA/CPU, `--batch-size 32`. Optional `--username`/`--password` override the environment.
`--api-url` defaults to `FRIDGE_API_URL`, otherwise `http://localhost:8000/api`.
Use `--limit 100` for a smoke run; `--limit 0` imports all geolocated available test images.

The importer builds the embedding map client-side after stats refresh, including the full
210,122-image split. `Dataset.compute_visualization()` scales the UMAP fit to available RAM
and places every embedded image, at any dataset size. `--no-reduce` skips map building.
Failures leave imported samples intact and print the command to retry. Runs that write
nothing preserve the existing map layout; managed maps remain under scheduled re-fits.
To rebuild a map for an older dataset in an authenticated SDK session:
`Dataset("osv5m-test").compute_visualization(field_name="clip")`.

## Verification and capacity

```bash
python -m pytest -q examples/osv5m/test_osv5m.py
python examples/_shared/verify.py --kind osv5m \
  --dataset-name osv5m-test --api-url "$FRIDGE_API_URL" --expected-count 100
```

Measurements with four CPU threads, batch size 16 and cached ViT-B/32 reached
**22–28 images/s** across these importers (OSV: 21.7 images/s on a cold HDD, including decode).
Budget roughly **1.0–1.3 CPU hours per 100k**, or **2.1–2.7 CPU hours for all 210,122**,
plus UMAP. A CUDA smoke imported 1,000 OSV images in 27.4 seconds. These measurements are
not full-import benchmarks; storage and backend contention affect throughput. Duplicate-only
reruns load no CLIP model and skip reduction. Reserve 10–60 minutes for 100k-point UMAP;
full-split client reduction needs a separate hardware-specific benchmark.

Disk: the measured first shard is 2.25 GB zipped and about 2.35 GB extracted. Extrapolate
**9–11 GB of extracted images plus 111.1 MiB CSV**, or **19–22 GB retaining all five ZIPs**.
Allow **0.5–2 GB of Postgres data/indexes per 100k samples** (roughly 1–4 GB for the full split),
excluding WAL/backups and the existing 1.2 GB CLIP cache. The importer does not copy images
or write derived media.
