#!/usr/bin/env python3
"""
Example: Prepare embeddings visualization for any dataset

Usage:
    python3 examples/sdk/compute_visualization.py DATASET_NAME [FIELD_NAME] [--all]

Examples:
    python3 examples/sdk/compute_visualization.py my-dataset                 # field "clip"
    python3 examples/sdk/compute_visualization.py my-dataset clip --all      # fit on every point
    python3 examples/sdk/compute_visualization.py my-dataset clip_patch_4x4  # a patch field
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import connect, get_client  # noqa: E402

from examples._shared.importer import existing_dataset, login  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset_name')
    parser.add_argument('field_name', nargs='?', default='clip')
    parser.add_argument('--all', '-a', action='store_true', help='Fit UMAP on every point (needs the most memory)')
    parser.add_argument(
        '--fit-sample-size',
        type=int,
        help='Fit UMAP on this many points for speed (default: as many as 75%% of free RAM allows); '
        'every point is placed either way',
    )
    parser.add_argument('--api-url', default=os.getenv('FRIDGE_API_URL', 'http://localhost:8000/api'))
    args = parser.parse_args()
    dataset_name, field_name = args.dataset_name, args.field_name
    fit_sample_size = -1 if args.all else (args.fit_sample_size or 'auto')

    # Connect to API
    connect(args.api_url)
    client = get_client()
    login(client)

    # Get dataset
    print(f"Loading dataset '{dataset_name}'...")
    ds = existing_dataset(client, dataset_name)

    # Compute UMAP visualization for existing embeddings
    print(f"Computing UMAP reduction for '{field_name}' embeddings...")
    if fit_sample_size == -1:
        print("Fitting on ALL embeddings...")
    elif fit_sample_size == 'auto':
        print("Scaling the fit to available RAM; placing every embedded point...")
    else:
        print(f"Fitting on {fit_sample_size:,} points; placing every embedded point...")
    print("This will take several minutes for large datasets...")

    result = ds.compute_visualization(field_name=field_name, fit_sample_size=fit_sample_size)
    print(f"\n✅ Done! Result: {result}")
    print("\nYou can now view the embeddings panel in the UI!")


if __name__ == "__main__":
    main()
