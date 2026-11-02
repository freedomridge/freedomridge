#!/usr/bin/env python3
"""
Compute patch embeddings for a Freedom Ridge dataset.

Single machine:
    python compute_patch_embeddings.py --dataset MyDataset --scales 2x2 4x4

Distributed (run on each worker):
    python compute_patch_embeddings.py --dataset MyDataset --partition 1 --total-partitions 10
    python compute_patch_embeddings.py --dataset MyDataset --partition 2 --total-partitions 10
    ...

After all partitions finish, run UMAP reduction:
    python compute_patch_embeddings.py --dataset MyDataset --reduce-only
"""

import argparse
import io
import os
import sys
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import connect, get_client  # noqa: E402

from examples._shared.importer import credentials, existing_dataset, login  # noqa: E402


def public_https_loader(filepath: str) -> Image.Image:
    """s3://bucket/key (public) -> fetch over https://bucket.s3.amazonaws.com/key."""
    if not filepath.startswith(("s3://", "http://", "https://")):
        with Image.open(filepath) as image:
            return image.convert("RGB")
    if filepath.startswith("s3://"):
        bucket, key = filepath[len("s3://") :].split("/", 1)
        url = f"https://{bucket}.s3.amazonaws.com/{key}"
    else:
        url = filepath
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def main():
    parser = argparse.ArgumentParser(description="Compute patch embeddings for a Freedom Ridge dataset")
    parser.add_argument("--dataset", required=True, help="Dataset name")
    parser.add_argument("--scales", nargs="+", default=["2x2", "4x4"], help="Patch scales (default: 2x2 4x4)")
    parser.add_argument("--field-name", default="clip", help="Embedding field name (default: clip)")
    parser.add_argument("--model", default="openai/clip-vit-base-patch32", help="CLIP model name")
    parser.add_argument("--batch-size", type=int, default=50, help="Images per GPU batch")
    parser.add_argument("--threads", type=int, default=4, help="Image loading threads")
    parser.add_argument("--partition", type=int, default=None, help="Partition number (1-indexed)")
    parser.add_argument("--total-partitions", type=int, default=None, help="Total number of partitions")
    parser.add_argument("--reduce-only", action="store_true", help="Only run UMAP reduction (after all partitions)")
    parser.add_argument("--no-reduce", action="store_true", help="Skip UMAP reduction")
    parser.add_argument(
        "--api-url",
        default=None,
        help="Freedom Ridge API URL (default: FRIDGE_API_URL env or localhost:8000)",
    )

    parser.add_argument('--public-s3', action='store_true', help='Use anonymous HTTPS for public S3 objects')
    credentials(parser)
    args = parser.parse_args()
    os.environ['HF_HUB_OFFLINE'] = os.environ['TRANSFORMERS_OFFLINE'] = '1'

    api_url = args.api_url or os.getenv("FRIDGE_API_URL", "http://localhost:8000/api")
    connect(api_url)

    client = get_client()
    login(client, args)

    ds = existing_dataset(client, args.dataset)

    if args.reduce_only:
        for scale in args.scales:
            patch_field = f"{args.field_name}_patch_{scale}"
            print(f"Running UMAP reduction for {patch_field}...")
            ds.compute_visualization(field_name=patch_field)
        print("Done!")
        return

    ds.compute_patch_embeddings(
        model_name=args.model,
        image_loader=public_https_loader if args.public_s3 else None,
        field_name=args.field_name,
        scales=args.scales,
        batch_size=args.batch_size,
        num_threads=args.threads,
        partition=args.partition,
        total_partitions=args.total_partitions,
        reduce=not args.no_reduce,
    )


if __name__ == "__main__":
    main()
