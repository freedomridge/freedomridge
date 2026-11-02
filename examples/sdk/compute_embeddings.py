#!/usr/bin/env python3
"""Compute or repair CLIP vectors for any existing dataset, using authenticated media."""

import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import connect, get_client  # noqa: E402

from examples._shared.importer import ClipEncoder, existing_dataset, login, parser, reduce_if_written  # noqa: E402


def main():
    ap = parser(
        __doc__,
        None,
        batch_size=32,
        require_data_root=False,
        limit_help='First N existing samples in ID order (default 0 = all)',
    )
    ap.set_defaults(limit=0)
    ap.add_argument('--dataset', dest='dataset_name', required=True)
    args = ap.parse_args()
    connect(args.api_url)
    client = get_client()
    login(client, args)
    dataset = existing_dataset(client, args.dataset_name)
    encoder = ClipEncoder(args)
    written = selected = after_id = 0
    while not args.limit or selected < args.limit:
        size = min(args.batch_size, args.limit - selected) if args.limit else args.batch_size
        page = client.query_samples(dataset._id, limit=size, after_id=after_id)
        rows = page['items']
        if not rows:
            break
        images = []
        try:
            for row in rows:
                response = client.session.get(f"{client.base_url}/samples/{row['id']}/media", timeout=60)
                response.raise_for_status()
                with Image.open(io.BytesIO(response.content)) as source:
                    images.append(source.convert('RGB'))
            vectors = encoder(images)
            client.upload_embeddings(
                dataset._id,
                [
                    {'sample_id': row['id'], 'field_name': args.field_name, 'vector': vector}
                    for row, vector in zip(rows, vectors, strict=True)
                ],
            )
            written += len(rows)
        finally:
            for image in images:
                image.close()
        selected += len(rows)
        after_id = rows[-1]['id']
        print(f'{written} vectors written', flush=True)
    reduce_if_written(client, dataset, args, args.field_name, written)


if __name__ == '__main__':
    main()
