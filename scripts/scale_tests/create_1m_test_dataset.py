#!/usr/bin/env python3
"""
Generate a 1 million sample test dataset from BDD100K for performance testing.

This script creates a large test dataset by:
1. Loading a subset of BDD100K samples
2. Duplicating them with variations to reach 1M samples. Each copy gets its own
   synthetic filepath (filepaths are unique per dataset); the real image path is
   kept in the "image" media field, which the grid and modal display.
3. Adding fake random embeddings (512-dim) to each sample, sent inline
4. Ingesting via the Freedom Ridge SDK in efficient batches

This allows performance testing without needing to compute real embeddings.
"""

import json
import os
import random
import sys
from argparse import Namespace
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Dataset, connect, get_client  # noqa: E402

from examples._shared.importer import login  # noqa: E402

# Add parent directory to path to import fridge SDK

# Configuration


# Configuration
# Use absolute paths for robust resolution
DEFAULT_DATA_ROOT = str((Path(__file__).parent.parent.parent / "data").resolve())
DATA_ROOT = os.getenv("FRIDGE_DATA_ROOT", DEFAULT_DATA_ROOT)
BDD100K_PATH = os.path.join(DATA_ROOT, "bdd100k")
IMAGES_DIR = os.path.join(BDD100K_PATH, "100k_images")
ANNOTATIONS_DIR = os.path.join(BDD100K_PATH, "100k_annotations")
DATA_DIR = os.path.join(BDD100K_PATH, "100k_data")

# Target dataset size
API_URL = os.getenv("FRIDGE_API_URL", "http://localhost/api")

TARGET_SAMPLES = 1_000_000
BATCH_SIZE = 1000  # Insert in batches for memory efficiency
EMBEDDING_DIM = 512  # CLIP embedding dimension

# How many samples to load from BDD100K as base (across splits)
BASE_SAMPLES_LIMIT = 10000  # Load 10K base samples, duplicate ~100x = 1M


def parse_bdd100k_label(label_path: str) -> Optional[Dict]:
    """Parse a BDD100K JSON label file."""
    try:
        with open(label_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error parsing {label_path}: {e}")
        return None


def get_video_metadata_at_10s(video_data_path: str) -> Optional[Dict]:
    """Extract metadata at 10-second mark from video data JSON."""
    try:
        with open(video_data_path, 'r') as f:
            video_data = json.load(f)

        metadata = {}
        start_time = video_data.get('startTime', 0)
        target_time = start_time + 10000

        locations = video_data.get('locations', [])
        if locations:
            closest_loc = min(locations, key=lambda x: abs(x.get('timestamp', 0) - target_time))

            if 'latitude' in closest_loc and 'longitude' in closest_loc:
                metadata['location'] = {
                    'type': 'Point',
                    'coordinates': [closest_loc['longitude'], closest_loc['latitude']],
                }
                metadata['gps_accuracy'] = closest_loc.get('accuracy', None)

            if 'speed' in closest_loc:
                metadata['speed'] = closest_loc['speed']

            if 'course' in closest_loc:
                metadata['heading'] = closest_loc['course']

            if 'timestamp' in closest_loc:
                metadata['timestamp'] = closest_loc['timestamp'] / 1000.0

        return metadata if metadata else None

    except Exception:
        return None


def convert_bbox_to_detections(labels: List[Dict]) -> List[Dict]:
    """Convert BDD100K bounding boxes to Freedom Ridge detection format."""
    detections = []
    for label in labels:
        if 'box2d' in label:
            bbox = label['box2d']
            x1, y1 = bbox['x1'], bbox['y1']
            x2, y2 = bbox['x2'], bbox['y2']

            detections.append(
                {
                    'label': label['category'],
                    'bbox': [x1, y1, x2 - x1, y2 - y1],
                    'score': 1.0,
                },
            )

    return detections


def convert_poly2d_to_polygons(objects: List[Dict]) -> List[Dict]:
    """Convert BDD100K poly2d annotations to Freedom Ridge polygon format."""
    polygons = []
    IMG_W = 1280
    IMG_H = 720

    for obj in objects:
        if 'poly2d' in obj:
            points = [[pt[0] / IMG_W, pt[1] / IMG_H] for pt in obj['poly2d']]

            polygons.append(
                {
                    'label': obj['category'],
                    'points': points,
                    'closed': True,
                    'id': obj.get('id'),
                },
            )

    return polygons


def generate_fake_embedding(base_seed: int, variation: int) -> List[float]:
    """
    Generate a fake but deterministic embedding.

    Uses base_seed from original sample and variation number to create
    similar but distinct embeddings for duplicates.
    """
    # Set seed for reproducibility
    rng = np.random.RandomState(seed=base_seed + variation)

    # Generate random unit vector
    embedding = rng.randn(EMBEDDING_DIM)
    # Normalize to unit length (like real embeddings)
    embedding = embedding / np.linalg.norm(embedding)

    return embedding.tolist()


def add_variation_to_metadata(metadata: Dict, variation: int) -> Dict:
    """
    Add slight variations to metadata to make duplicates more realistic.

    This simulates different conditions/times for the same location.
    """
    varied = metadata.copy()

    # Vary timestamp if present (add hours)
    if 'timestamp' in varied:
        varied['timestamp'] = varied['timestamp'] + (variation * 3600)  # Add hours

    # Vary speed slightly if present
    if 'speed' in varied:
        varied['speed'] = max(0, varied['speed'] + random.uniform(-5, 5))

    # Add a variation marker
    varied['duplicate_id'] = variation

    return varied


def add_location_jitter(location: Dict, seed: int) -> Dict:
    """
    Add small random offset to GPS coordinates using polar coordinates.
    Ensures a minimum distance from the original point.
    """
    if location['type'] != 'Point':
        return location

    rng = np.random.RandomState(seed=seed)
    lon, lat = location['coordinates']

    theta = rng.rand() * 2 * np.pi

    # Polar coordinates for valid range distribution
    # 1 deg latitude ~= 69 miles ~= 111km
    # Min: 2 meters ~= 0.000018 degrees
    # Max: 5 miles ~= 0.072 degrees
    min_deg = 0.000018
    max_deg = 0.072

    # Random radius between min and max
    r = min_deg + rng.rand() * (max_deg - min_deg)

    # Convert back to Cartesian offsets
    lon_offset = r * np.cos(theta)
    lat_offset = r * np.sin(theta)

    new_loc = location.copy()
    new_loc['coordinates'] = [lon + lon_offset, lat + lat_offset]
    return new_loc


def load_base_samples(limit: int = BASE_SAMPLES_LIMIT, splits: List[str] | None = None) -> List[Dict]:
    """
    Load base samples from BDD100K dataset.

    Returns a list of sample dictionaries ready for duplication.
    """
    splits = splits or ['train']
    print(f"📁 Loading {limit} base samples from BDD100K across splits: {', '.join(splits)}")

    base_samples: List[Dict] = []
    base_seed = 0

    for split in splits:
        images_path = os.path.join(IMAGES_DIR, split)
        annotations_path = os.path.join(ANNOTATIONS_DIR, split)
        data_path = os.path.join(DATA_DIR, split)

        if not os.path.exists(images_path):
            print(f"❌ Images directory not found: {images_path}")
            continue

        # Get label files for this split
        label_files = list(Path(annotations_path).glob("*.json"))

        # Respect global limit across splits
        if limit:
            remaining = max(limit - len(base_samples), 0)
            if remaining == 0:
                break
            label_files = label_files[:remaining]

        print(f"Processing {len(label_files)} annotations from {split}...")

        for i, label_path in enumerate(label_files):
            if (len(base_samples) + 1) % 1000 == 0:
                print(f"  Loaded {len(base_samples) + 1} total base samples...")

            label_data = parse_bdd100k_label(str(label_path))
            if not label_data:
                continue

            image_name = label_data['name']
            if not image_name.endswith('.jpg'):
                image_path = os.path.join(images_path, f"{image_name}.jpg")
            else:
                image_path = os.path.join(images_path, image_name)

            if not os.path.exists(image_path):
                continue

            # Extract metadata
            attributes = label_data.get('attributes', {})
            metadata = {
                'split': split,
                'weather': attributes.get('weather', 'unknown'),
                'scene': attributes.get('scene', 'unknown'),
                'timeofday': attributes.get('timeofday', 'unknown'),
            }

            # Get video metadata
            video_id = Path(image_name).stem
            video_data_path = os.path.join(data_path, f"{video_id}.json")

            heading_value = None
            if os.path.exists(video_data_path):
                video_metadata = get_video_metadata_at_10s(video_data_path)
                if video_metadata:
                    heading_value = video_metadata.get('heading')
                    if heading_value is not None:
                        video_metadata.pop('heading', None)
                    metadata.update(video_metadata)

            # Get annotations
            objects_to_convert = []
            if 'frames' in label_data and label_data['frames']:
                for frame in label_data['frames']:
                    if frame.get('timestamp') == 10000:
                        objects_to_convert = frame.get('objects', [])
                        break

            if objects_to_convert:
                detections = convert_bbox_to_detections(objects_to_convert)
                if detections:
                    metadata['detections'] = detections

                polygons = convert_poly2d_to_polygons(objects_to_convert)
                if polygons:
                    metadata['polygons'] = polygons

            # Calculate relative path for storage
            # The backend expects paths relative to FRIDGE_DATA_ROOT (e.g. bdd100k/100k_images/...)
            try:
                # Ensure both are absolute to avoid matching issues
                abs_image_path = os.path.abspath(image_path)
                abs_data_root = os.path.abspath(DATA_ROOT)
                rel_path = os.path.relpath(abs_image_path, abs_data_root)
            except ValueError as e:
                print(f"⚠️ Error calculating relpath for {image_path}: {e}")
                rel_path = image_path

            # Create base sample
            sample = {
                'filepath': rel_path,
                'media_type': 'image',
                'tags': [split, attributes.get('weather', ''), attributes.get('scene', '')],
                'metadata': metadata,
                'base_seed': base_seed,  # Store for embedding generation
            }

            if 'location' in metadata:
                sample['location'] = metadata['location']
                del metadata['location']

            if heading_value is not None:
                sample['heading'] = heading_value

            base_samples.append(sample)
            base_seed += 1

            if limit and len(base_samples) >= limit:
                break

        if limit and len(base_samples) >= limit:
            break

    print(f"✓ Loaded {len(base_samples)} base samples")
    if base_samples:
        print(f"  First sample filepath: {base_samples[0]['filepath']}")
    return base_samples


def generate_duplicates(base_samples: List[Dict], target_count: int) -> List[Dict]:
    """
    Generate duplicates with variations to reach target count.

    Returns samples with fake embeddings included.
    """
    print(f"\n🔄 Generating {target_count:,} samples with variations...")

    duplicates_per_sample = target_count // len(base_samples)
    print(f"  Creating ~{duplicates_per_sample}x duplicates per base sample")

    all_samples: list[dict] = []

    for i, base_sample in enumerate(base_samples):
        if (i + 1) % 500 == 0:
            print(f"  Duplicated {i + 1}/{len(base_samples)} base samples ({len(all_samples):,} total)...")

        # Create variations
        for variation in range(duplicates_per_sample):
            # Copy sample
            sample = base_sample.copy()

            # Vary metadata
            sample['metadata'] = add_variation_to_metadata(base_sample['metadata'], variation)

            # Filepaths are unique per dataset: give each copy its own, and keep the real
            # image in the "image" media field so the copy still displays.
            sample['filepath'] = f"scale-test/copy-{variation:04d}/{base_sample['filepath']}"
            sample['metadata']['image'] = base_sample['filepath']

            # Add location jitter so points aren't stacked
            if 'location' in sample:
                sample['location'] = add_location_jitter(sample['location'], variation)

            # Generate fake embedding
            embedding = generate_fake_embedding(base_sample['base_seed'], variation)
            sample['embedding'] = embedding

            # Remove base_seed (not needed in DB)
            sample.pop('base_seed', None)

            all_samples.append(sample)

            # Stop if we hit target
            if len(all_samples) >= target_count:
                break

        if len(all_samples) >= target_count:
            break

    print(f"✓ Generated {len(all_samples):,} samples")
    return all_samples[:target_count]


def ingest_large_dataset(
    username: str = "admin",
    password: str | None = None,
    target_samples: int = TARGET_SAMPLES,
    base_samples_limit: int = BASE_SAMPLES_LIMIT,
    splits: List[str] | None = None,
):
    """
    Create and ingest a large test dataset.
    """
    splits = splits or ['train']
    print(f"🚀 Creating {target_samples:,} sample test dataset...")
    print(f"   Base samples: {'Unlimited' if base_samples_limit == 0 else f'{base_samples_limit:,}'}")
    if base_samples_limit > 0:
        print(f"   Duplication factor: ~{target_samples // base_samples_limit}x")
    else:
        print("   Duplication factor: (calculated after loading)")
    print(f"   Splits: {', '.join(splits)}")

    # Connect
    print(f"🔗 Connecting to {API_URL}...")
    connect(API_URL)
    client = get_client()

    # Login
    print(f"\n🔐 Logging in as {username}...")
    try:
        login(client, Namespace(username=username, password=password))
        print("✓ Authenticated")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return None

    # Create dataset
    dataset_name = "BDD100K_1M_Test"
    print(f"\n📊 Creating dataset: {dataset_name}")

    existing = client.get_dataset(dataset_name)
    if existing:
        print(f"⚠️  Dataset already exists (ID: {existing['id']})")
        response = input("Delete and recreate? [y/N]: ")
        if response.lower() == 'y':
            client.delete_dataset(existing['id'])
            print("✓ Deleted existing dataset")
        else:
            print("❌ Aborted")
            return None

    dataset = client.create_dataset(
        name=dataset_name,
        description=f"BDD100K performance test dataset with {target_samples:,} samples "
        f"(duplicated with fake embeddings for scale testing)",
        media_fields=["image", "filepath"],
    )
    client.update_dataset(dataset['id'], grid_media_field="image", modal_media_field="image")
    print(f"✓ Created dataset (ID: {dataset['id']})")

    # Load base samples
    base_samples = load_base_samples(base_samples_limit, splits=splits)
    if not base_samples:
        print("❌ No base samples loaded")
        return None

    # Generate duplicates with embeddings
    all_samples = generate_duplicates(base_samples, target_samples)

    # Ingest in batches. Each sample carries its vector inline ("embedding" is stored as
    # the "clip" field), so no ids have to be matched up afterwards. "skip" makes a re-run
    # safe; the count comes from the ids the server actually created.
    print(f"\n📥 Ingesting {len(all_samples):,} samples in batches of {BATCH_SIZE}...")

    total_ingested = 0
    for batch_start in range(0, len(all_samples), BATCH_SIZE):
        batch = all_samples[batch_start : batch_start + BATCH_SIZE]

        try:
            result = client.add_samples(dataset['id'], batch, on_duplicate='skip')
        except Exception as e:
            print(f"  ❌ Batch failed: {e}")
            continue
        total_ingested += len(result.get('ids', []))
        print(
            f"  ✓ Batch {batch_start // BATCH_SIZE + 1}: {total_ingested:,}/{len(all_samples):,} samples",
        )

    print("\n✅ Successfully created test dataset!")
    print(f"   Dataset: {dataset_name} (ID: {dataset['id']})")
    print(f"   Samples (with embeddings): {total_ingested:,}")

    # Build the embeddings map. compute_visualization fits on the client, sized to RAM, and
    # places every sample; the server-side reduce refuses more than 200,000 vectors.
    try:
        print("\n🎯 Computing the embeddings map...")
        Dataset.from_id(dataset['id']).compute_visualization(field_name="clip")
    except Exception as e:
        print(f"⚠️ Visualization failed: {e}")

    print(f"\n🌐 View at: http://localhost:3000/datasets/{dataset['id']}")

    return dataset


def main():
    """Main workflow."""
    import argparse

    parser = argparse.ArgumentParser(description="Generate 1M sample test dataset from BDD100K")
    parser.add_argument(
        '--target',
        type=int,
        default=TARGET_SAMPLES,
        help=f"Target number of samples (default: {TARGET_SAMPLES:,})",
    )
    parser.add_argument(
        '--base-limit',
        type=int,
        default=BASE_SAMPLES_LIMIT,
        help=f"Number of base samples to load from BDD100K (default: {BASE_SAMPLES_LIMIT:,})",
    )
    parser.add_argument(
        '--splits',
        type=str,
        default='train',
        help="Comma-separated list of splits to use for base samples (default: train)",
    )
    parser.add_argument(
        '--username',
        default=os.getenv('FRIDGE_USERNAME', 'admin'),
        help="API username (default: admin)",
    )
    parser.add_argument(
        '--password',
        default=os.getenv('FRIDGE_PASSWORD'),
        help="API password (or FRIDGE_PASSWORD / terminal prompt)",
    )

    args = parser.parse_args()

    # Sanity check
    if args.target < args.base_limit:
        print(f"❌ Target ({args.target:,}) must be >= base-limit ({args.base_limit:,})")
        return

    ingest_large_dataset(
        username=args.username,
        password=args.password,
        target_samples=args.target,
        base_samples_limit=args.base_limit,
        splits=[s.strip() for s in args.splits.split(',') if s.strip()],
    )

    print("\n" + "=" * 70)
    print("Performance test dataset created!")
    print("Use this to test:")
    print("  - Multi-user concurrent queries")
    print("  - Large dataset pagination performance")
    print("  - Vector search at scale")
    print("  - Filter query optimization")
    print("=" * 70)


if __name__ == "__main__":
    main()
