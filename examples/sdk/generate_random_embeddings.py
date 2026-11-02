#!/usr/bin/env python3
"""
Upload random unit-length embeddings, through the API, for samples that have none.

Lets you try the embeddings panel and similarity search without a model. For real
vectors use compute_embeddings.py (CLIP), or upload your own model's vectors the same way.

Usage:
    FRIDGE_API_TOKEN=... python examples/sdk/generate_random_embeddings.py --dataset my-dataset
    python examples/sdk/compute_visualization.py my-dataset random_512   # then place them on the map

A field name's dimension is fixed server-wide by its first vectors, so keep --field-name
for random vectors only.
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import connect, get_client  # noqa: E402

from examples._shared.importer import credentials, existing_dataset, login, positive  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dataset', required=True, help='Name of an existing dataset')
    parser.add_argument('--field-name', default='random_512')
    parser.add_argument('--dim', type=positive, default=512)
    parser.add_argument('--batch-size', type=positive, default=1000)
    parser.add_argument('--api-url', default=os.getenv('FRIDGE_API_URL', 'http://localhost:8000/api'))
    credentials(parser)
    args = parser.parse_args()

    connect(args.api_url)
    client = get_client()
    login(client, args)
    dataset = existing_dataset(client, args.dataset)
    rng = np.random.default_rng()

    written = 0
    while True:
        # Samples still lacking this field; uploaded ones drop out of the next page.
        page = dataset.missing('embedding', field=args.field_name, limit=args.batch_size)
        items = page['items']
        if not items:
            break
        vectors = rng.standard_normal((len(items), args.dim)).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        result = client.upload_embeddings(
            dataset.id,
            [
                {'sample_id': item['id'], 'field_name': args.field_name, 'vector': vector.tolist()}
                for item, vector in zip(items, vectors, strict=True)
            ],
        )
        if not result['written']:
            break
        written += result['written']
        print(f"{written:,} written, {page['total'] - len(items):,} still missing", flush=True)

    print(f"Done: {written:,} random {args.dim}-d vectors in '{args.field_name}' for '{args.dataset}'")


if __name__ == '__main__':
    main()
