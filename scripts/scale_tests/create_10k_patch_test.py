#!/usr/bin/env python3
"""
Copy the first 10K samples of an existing dataset into a new one and compute patch embeddings.

This creates a smaller dataset for testing the patch-based similarity search
feature without waiting for a full embedding computation.

Usage:
    python scripts/scale_tests/create_10k_patch_test.py --source-dataset bdd100k

Prerequisites:
    - the source dataset already loaded
    - pip install -e "python/[patch-embeddings]" (and cached CLIP weights)
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import connect, get_client  # noqa: E402

from examples._shared.importer import login  # noqa: E402

API_URL = os.getenv("FRIDGE_API_URL", "http://localhost:8000/api")
SOURCE_DATASET_ID = 0  # set from --source-dataset
SUBSET_SIZE = 10000
DATASET_NAME = ""
SCALES = ["2x2", "4x4"]


def create_subset():
    """Create a 10K subset dataset from BDD100K."""
    client = get_client()

    # Check if already exists
    datasets = client.get_datasets()
    for ds in datasets:
        if ds['name'] == DATASET_NAME:
            print(f"Dataset '{DATASET_NAME}' already exists (id={ds['id']})")
            return ds['id']

    # Fetch sample IDs from source
    print(f"Fetching samples from dataset {SOURCE_DATASET_ID}...")
    resp = client.session.get(
        f"{client.base_url}/datasets/{SOURCE_DATASET_ID}/samples",
        params={"limit": SUBSET_SIZE, "offset": 0},
    )
    resp.raise_for_status()
    data = resp.json()
    samples = data['items']
    print(f"Got {len(samples)} samples")

    # Create new dataset
    print(f"Creating dataset '{DATASET_NAME}'...")
    ds = client.create_dataset(
        name=DATASET_NAME,
        description=f"{SUBSET_SIZE:,}-sample subset of dataset {SOURCE_DATASET_ID} for patch testing. Scales: {SCALES}",
    )
    dataset_id = ds['id']
    print(f"Created dataset id={dataset_id}")

    # Add samples in batches
    batch_size = 500
    for i in range(0, len(samples), batch_size):
        batch = samples[i : i + batch_size]
        payload = []
        for s in batch:
            entry = {
                "filepath": s['filepath'],
                "media_type": s.get('media_type', 'image'),
                "metadata": s.get('metadata', {}),
                "tags": s.get('tags', []),
            }
            if s.get('location'):
                entry['location'] = s['location']
            payload.append(entry)

        resp = client.session.post(
            f"{client.base_url}/datasets/{dataset_id}/samples",
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"  Batch {i // batch_size + 1}: {result.get('count', len(batch))} samples added")

    print(f"Subset created: {len(samples)} samples in dataset {dataset_id}")
    return dataset_id


def compute_patches(dataset_id):
    """Compute patch embeddings at configured scales."""
    from fridge.core.dataset import Dataset

    ds = Dataset(DATASET_NAME)

    print(f"\nComputing patch embeddings for dataset {dataset_id}")
    print(f"Scales: {SCALES}")
    patches_per_image = sum(int(s.split('x')[0]) * int(s.split('x')[1]) for s in SCALES)
    print(f"Expected patches: {SUBSET_SIZE} x {patches_per_image} = {SUBSET_SIZE * patches_per_image:,}")

    ds.compute_patch_embeddings(
        model_name="openai/clip-vit-base-patch32",
        field_name="clip",
        scales=SCALES,
        batch_size=25,  # Small batches to avoid OOM
    )

    # Verify
    client = get_client()
    resp = client.session.get(
        f"{client.base_url}/datasets/{dataset_id}/embeddings/patch-scales",
        params={"field_name": "clip"},
    )
    resp.raise_for_status()
    scales = resp.json()
    print(f"\nPatch scales available: {scales}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dataset", required=True, help="Name of the existing dataset to copy samples from")
    parser.add_argument("--name", help="Name of the new dataset (default: <source>-10k-patches)")
    parser.add_argument("--limit", type=int, default=SUBSET_SIZE, help=f"Samples to copy (default {SUBSET_SIZE:,})")
    parser.add_argument("--scales", nargs="+", default=SCALES)
    parser.add_argument("--api-url", default=API_URL)
    args = parser.parse_args()
    SUBSET_SIZE, SCALES = args.limit, args.scales
    DATASET_NAME = args.name or f"{args.source_dataset}-10k-patches"

    connect(args.api_url)
    client = get_client()
    login(client)
    source = client.get_dataset(args.source_dataset)
    if source is None:
        sys.exit(f"No dataset named {args.source_dataset!r} at {args.api_url}")
    SOURCE_DATASET_ID = source["id"]

    print("=" * 60)
    print(f"{SUBSET_SIZE:,}-sample patch test dataset from '{args.source_dataset}'")
    print("=" * 60)

    dataset_id = create_subset()
    compute_patches(dataset_id)

    print("\nDone! You can now test patch similarity search in the Freedom Ridge UI.")
    print(f"Dataset ID: {dataset_id}")
