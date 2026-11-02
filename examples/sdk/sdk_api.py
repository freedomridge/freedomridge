#!/usr/bin/env python3
"""
Example: Freedom Ridge SDK API

Demonstrates the SDK API including:
- Dynamic field assignment using sample.field or sample.metadata["field"]
- Efficient iteration with iter_samples()
- Field selection for performance
- Progress bars for large datasets

The dataset must already exist (--dataset); generate_synthetic_dataset.py makes one.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Sample, connect  # noqa: E402

from examples._shared.importer import existing_dataset, login  # noqa: E402

# Add SDK to path


# Configuration
API_URL = os.getenv("FRIDGE_API_URL", "http://localhost:8000/api")


def main():
    global API_URL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', default=os.getenv('FRIDGE_API_URL', 'http://localhost:8000/api'))
    parser.add_argument('--dataset', required=True, help='Name of an existing dataset')
    args = parser.parse_args()
    API_URL = args.api_url
    print("=" * 70)
    print("Freedom Ridge SDK API Examples")
    print("=" * 70)
    print()

    # Connect to API
    print(f"1. Connecting to Freedom Ridge API at {API_URL}...")
    connect(API_URL)

    # Login
    from fridge import get_client

    client = get_client()
    try:
        login(client)
        print("   ✓ Authenticated successfully\n")
    except Exception as e:
        print(f"   ✗ Login failed: {e}")
        print("   Please ensure the backend is running and credentials are correct\n")
        return

    # Load the dataset (never creates one)
    print("2. Loading dataset...")
    dataset = existing_dataset(client, args.dataset)
    print(f"   ✓ Dataset ready (ID: {dataset._id})\n")

    print("=" * 70)
    print("FEATURE 1: Dynamic Field Assignment")
    print("=" * 70)
    print()

    # Create sample with dynamic fields
    print("Creating sample with dynamic field assignment...")
    sample = Sample(filepath="/path/to/image.jpg")

    # Method 1: Direct attribute assignment
    sample.weather = "sunny"
    sample.temperature = 75.5
    sample.scene = "highway"

    # Method 2: Via metadata dictionary (also works!)
    sample.metadata["speed"] = 65
    sample.metadata["num_vehicles"] = 3

    print("\n✓ Fields assigned:")
    print(f"  - sample.weather = {sample.weather}")
    print(f"  - sample.temperature = {sample.temperature}")
    print(f"  - sample.scene = {sample.scene}")
    print(f"  - sample.speed = {sample.speed}")
    print(f"  - sample.num_vehicles = {sample.num_vehicles}")

    print("\n✓ Both syntaxes work equivalently:")
    print(f"  - sample.weather == sample.metadata['weather']: {sample.weather == sample.metadata['weather']}")
    print(f"  - sample.speed == sample.metadata['speed']: {sample.speed == sample.metadata['speed']}")

    print("\n✓ All custom fields stored in metadata:")
    print(f"  - Metadata keys: {list(sample.metadata.keys())}")

    print("\n" + "=" * 70)
    print("FEATURE 2: Efficient Dataset Iteration")
    print("=" * 70)
    print()

    # Check if dataset has samples
    sample_count = dataset.all().count()

    if sample_count == 0:
        print("⚠ Dataset is empty. Add some samples first to see iteration features.")
        print("\nExample: generate a small synthetic dataset:")
        print("  python examples/sdk/generate_synthetic_dataset.py --name synth-demo --num 24\n")
    else:
        print(f"Dataset has {sample_count} samples\n")

        # Example 1: Basic iteration with progress bar
        print("Example 1: Iterate with progress bar")
        print("-" * 40)
        count = 0
        for sample in dataset.iter_samples(progress=True):
            count += 1
            if count >= 10:  # Just show first 10
                break
        print(f"✓ Iterated through {count} samples\n")

        # Example 2: Field selection for performance
        print("Example 2: Iterate with field selection (faster!)")
        print("-" * 40)
        print("Fetching only 'filepath' and 'tags' fields...")
        count = 0
        for sample in dataset.iter_samples(
            selected_fields=["filepath", "tags"],
            progress=True,
        ):
            count += 1
            if count <= 3:
                print(f"  Sample {count}: {sample.filepath}, tags={sample.tags}")
            if count >= 10:
                break
        print(f"✓ Fetched {count} samples with selected fields only\n")

        # Example 3: Iterate through metadata fields
        print("Example 3: Iterate with metadata field selection")
        print("-" * 40)
        print("Fetching 'filepath' and 'metadata.weather'...")
        count = 0
        for sample in dataset.iter_samples(
            selected_fields=["filepath", "metadata.weather"],
            progress=True,
        ):
            count += 1
            if count <= 3:
                weather = sample.metadata.get('weather', 'N/A')
                print(f"  Sample {count}: {Path(sample.filepath).name}, weather={weather}")
            if count >= 10:
                break
        print(f"✓ Fetched {count} samples\n")

    # --- Compute image hashes (for duplicate detection & similarity) ---
    print("-" * 70)
    print("Computing Image Hashes")
    print("-" * 70)
    print()
    print("dataset.compute_hashes() computes perceptual hashes (dhash, phash)")
    print("for exact-duplicate detection.")
    print()
    print("  # Default: dhash_8 + phash_8")
    print("  dataset.compute_hashes()")
    print()
    print("  # Other standard hashes (dhash_N, phash_N, ahash_N, whash_N):")
    print("  dataset.compute_hashes(algorithms=['dhash_16', 'phash_16'])")
    print()

    print("=" * 70)
    print("Summary")
    print("=" * 70)
    print()
    print("✓ Dynamic field assignment works")
    print("  - Use sample.field_name = value")
    print("  - Or sample.metadata['field_name'] = value")
    print("  - Both work equivalently!")
    print()
    print("✓ Efficient iteration with iter_samples()")
    print("  - progress=True for progress bars")
    print("  - selected_fields=[...] for faster queries")
    print("  - Great for large datasets!")
    print()
    print("✓ Compute hashes with dataset.compute_hashes()")
    print("  - Enables duplicate detection panel in UI")
    print()


if __name__ == "__main__":
    main()
