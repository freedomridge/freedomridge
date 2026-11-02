#!/usr/bin/env python3
"""Update existing demo descriptions by exact name, without re-importing any samples.

Requires an admin FRIDGE_API_TOKEN. Explicitly choose a target, for example:
  PYTHONPATH=python:. python scripts/backfill_dataset_credits.py --api-url http://127.0.0.1:8098/api --dry-run
Omit --dry-run to apply. Unchanged, absent and deleting datasets are skipped.
"""

import argparse
import os

from fridge.core.client import Client
from requests import HTTPError

from examples._shared.credits import DATASET_CREDITS


def backfill(client, names, dry_run=False):
    datasets = {dataset['name']: dataset for dataset in client.get_datasets() if not dataset.get('deleting')}
    changed = []
    for name in names:
        dataset = datasets.get(name)
        if dataset is None:
            print(f'{name}: absent; skipped')
            continue
        description = DATASET_CREDITS[name]
        if dataset.get('description') == description:
            print(f'{name}: already current')
            continue
        if not dry_run:
            client.update_dataset(dataset['id'], description=description)
        changed.append(name)
        print(f'{name}: {"would update" if dry_run else "updated"}')
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', required=True, help='Explicit backend API URL (including /api)')
    parser.add_argument(
        '--dataset',
        action='append',
        choices=tuple(DATASET_CREDITS),
        help='Default: all known demo names',
    )
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not os.getenv('FRIDGE_API_TOKEN'):
        parser.error('Set FRIDGE_API_TOKEN to an admin API token')
    client = Client(base_url=args.api_url)
    try:
        backfill(client, args.dataset or DATASET_CREDITS, args.dry_run)
    except HTTPError as error:
        if error.response is not None and error.response.status_code in (401, 403):
            parser.error('Dataset credits require an admin API token. Set FRIDGE_API_TOKEN to a valid admin token.')
        raise
    finally:
        client.session.close()


if __name__ == '__main__':
    main()
