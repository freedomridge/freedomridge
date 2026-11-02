#!/usr/bin/env python3
"""Compute perceptual hashes for a dataset (needs imagehash and Pillow; `uv sync` installs them)."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import connect, get_client  # noqa: E402

from examples._shared.importer import credentials, existing_dataset, login  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset')
    parser.add_argument('--algorithms', nargs='+', default=['dhash_8', 'phash_8'])
    parser.add_argument('--api-url', default=os.getenv('FRIDGE_API_URL', 'http://localhost:8000/api'))
    credentials(parser)
    args = parser.parse_args()
    connect(args.api_url)
    client = get_client()
    login(client, args)
    existing_dataset(client, args.dataset).compute_hashes(algorithms=args.algorithms)


if __name__ == '__main__':
    main()
