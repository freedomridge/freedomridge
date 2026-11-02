#!/usr/bin/env python3
"""
Ingest EuroSAT (Sentinel-2 land-cover tiles) into Freedom Ridge — georeferenced, labeled,
CLIP-embedded, UMAP-reduced. One script, one dataset, ready to explore.

EuroSAT is 27,000 multispectral 64x64 Sentinel-2 GeoTIFF tiles across 10 land-cover
classes (AnnualCrop, Forest, ..., SeaLake), each georeferenced (UTM). Per tile we:
  1. read the GeoTIFF, compute the tile-center lat/lon (rasterio -> EPSG:4326),
  2. render an RGB JPEG from bands B04/B03/B02 for the grid + modal,
  3. CLIP-embed the RGB render (ViT-B/32, local, GPU if available),
  4. create the sample with label + location + embedding INLINE (one transaction).
Then: refresh stats (so `label` shows up in Filters / color-by) and run the
the RAM-scaled UMAP fit (`Dataset.compute_visualization`) so the Embeddings scatter is live.

Swap step 3 for any Earth-observation model (OlmoEarth, SatMAE, ...) by writing its
vectors to `embeddings={"<your_field>": vec}` — Freedom Ridge doesn't compute
embeddings for you and doesn't care where they come from; a new field name establishes
its own dimension (wrong-dim uploads are rejected with a 422).

Prereqs:
    uv sync --extra cpu               # from the repo root; SDK, rasterio, torch, transformers
    # EuroSAT all-bands zip (~2 GB), e.g. the torchgeo mirror:
    #   curl -L -o EuroSATallBands.zip \
    #     "https://huggingface.co/datasets/torchgeo/eurosat/resolve/main/EuroSATallBands.zip"
    #   unzip EuroSATallBands.zip     # -> ds/images/remote_sensing/otherDatasets/sentinel_2/tif/<Class>/*.tif

Point --data-root at a directory UNDER the backend's FRIDGE_DATA_ROOT (the RGB renders
are written to <data-root>/rgb/ and must be servable by the backend).

Usage:
    python examples/eurosat/ingest.py --data-root /data/freedomridge/eurosat --limit 50
    python examples/eurosat/ingest.py --data-root /data/freedomridge/eurosat --limit 0   # all 27k
"""

import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.warp import transform as warp_transform

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Sample  # noqa: E402

from examples._shared.importer import contained_path, ingest, parser, writable_directory  # noqa: E402

# EuroSAT MS band order: B01 B02 B03 B04 B05 B06 B07 B08 B08A B09 B10 B11 B12.
# RGB render = B04/B03/B02 (1-indexed bands 4, 3, 2), clipped to the customary
# 0..2750 reflectance range, then gamma-lifted — true-color vegetation is
# near-black without it (compared 1.0/1.5/1.8/2.2 visually; 1.5 keeps contrast).
RGB_BANDS = (4, 3, 2)
REFLECTANCE_MAX = 2750.0
GAMMA = 1.5


def tile_center_lonlat(src):
    """Center of the tile's bounds, reprojected to EPSG:4326 -> (lon, lat)."""
    cx = (src.bounds.left + src.bounds.right) / 2
    cy = (src.bounds.bottom + src.bounds.top) / 2
    lons, lats = warp_transform(src.crs, "EPSG:4326", [cx], [cy])
    return lons[0], lats[0]


def render_rgb(src, out_path):
    """B04/B03/B02 -> 8-bit JPEG at out_path."""
    bands = src.read(RGB_BANDS).astype(np.float32)  # (3, H, W)
    lin = np.clip(bands / REFLECTANCE_MAX, 0.0, 1.0)
    rgb = (np.power(lin, 1.0 / GAMMA) * 255.0).astype(np.uint8)
    Image.fromarray(rgb.transpose(1, 2, 0)).save(out_path, quality=92)


def iter_tifs(ms_root, limit_per_class):
    """Yield (class_name, tif_path), up to limit_per_class per class (0 = all)."""
    class_dirs = sorted(p for p in Path(ms_root).iterdir() if p.is_dir())
    if not class_dirs:
        sys.exit(f"No class directories under {ms_root} — point --data-root at the dir containing the class folders")
    for class_dir in class_dirs:
        tifs = sorted(class_dir.glob("*.tif"))
        if limit_per_class:
            tifs = tifs[:limit_per_class]
        for tif in tifs:
            yield class_dir.name, tif


def find_ms_root(data_root):
    """Locate the directory holding the 10 class folders under data_root."""
    candidates = [
        data_root / "tif",
        data_root / "ds" / "images" / "remote_sensing" / "otherDatasets" / "sentinel_2" / "tif",
        data_root,
    ]
    for c in candidates:
        if c.is_dir() and any(p.is_dir() and any(p.glob("*.tif")) for p in c.iterdir()):
            return c
    sys.exit(f"Could not find EuroSAT class folders under {data_root} (looked for */*.tif)")


def build_parser():
    result = parser(__doc__, 'eurosat', batch_size=128, limit_help='First N tiles per class; 0 = all')
    result.set_defaults(reduce_errors='warn')
    return result


def iter_samples(args):
    root = args.data_root.resolve()
    ms_root = find_ms_root(root)
    for class_name, tif in iter_tifs(ms_root, args.limit):
        tif = contained_path(tif, root)
        jpg = contained_path(root / 'rgb' / class_name / (tif.stem + '.jpg'), root)
        with rasterio.open(tif) as src:
            lon, lat = tile_center_lonlat(src)
        sample = Sample(
            str(jpg),
            location=(lon, lat),
            tags=[class_name],
            metadata={
                'label': class_name,
                'source_tif': str(tif),
            },
        )
        yield sample


def main():
    args = build_parser().parse_args()

    def prepare(sample):
        jpg = Path(sample.filepath)
        writable_directory(jpg.parent, args.data_root)
        if not jpg.exists():
            with rasterio.open(sample.metadata['source_tif']) as src:
                render_rgb(src, jpg)

    ingest(
        args,
        iter_samples(args),
        {},
        'EuroSAT: 27k georeferenced Sentinel-2 land-cover tiles, 10 classes (MIT, zenodo.org/records/7711810)',
        prepare=prepare,
    )


if __name__ == '__main__':
    main()
