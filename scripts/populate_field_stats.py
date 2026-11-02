#!/usr/bin/env python3
"""Request full statistics rebuilds using the same durable jobs as the API and SDK.

The co-located runner must be running. Waiting does not execute a second statistics engine.
Run from the repository root with python -m scripts.populate_field_stats [dataset_id].
"""

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

DATABASE_URL = os.getenv('DATABASE_URL', 'postgres://freedomridge:password@localhost:5432/freedomridge')


async def populate_field_stats(dataset_id: int | None = None):
    from backend.database import init_connection
    from backend.statistics import request_refresh, wait_for_refresh

    async with asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2, init=init_connection) as pool:
        ids = (
            [dataset_id]
            if dataset_id is not None
            else [row['id'] for row in await pool.fetch('SELECT id FROM datasets WHERE NOT deleting ORDER BY id')]
        )
        jobs = []
        for identifier in ids:
            if dataset_id is not None and await pool.fetchval('SELECT deleting FROM datasets WHERE id=$1', identifier):
                raise RuntimeError(f'Dataset {identifier} is being deleted; finish or resume deletion first')
            try:
                jobs.append(await request_refresh(pool, identifier, force=True))
            except (asyncpg.ObjectNotInPrerequisiteStateError, LookupError):
                # A dataset can be sealed/removed after enumeration. Do not prevent
                # other datasets from refreshing, or mask errors on an active one.
                deleting = await pool.fetchval('SELECT deleting FROM datasets WHERE id=$1', identifier)
                if dataset_id is not None or deleting is False:
                    raise
                print(f'Skipping dataset {identifier}: being deleted or already removed', file=sys.stderr)
        failures = []
        for job in jobs:
            print(f"Dataset {job['dataset_id']}: waiting for statistics job {job['id']}", flush=True)
            try:
                result = await wait_for_refresh(pool, job['id'])
                print(result)
            except (TimeoutError, RuntimeError) as error:
                failures.append(str(error))
                print(error, file=sys.stderr)
        if failures:
            raise RuntimeError(f'{len(failures)} statistics refreshes did not complete')


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    asyncio.run(populate_field_stats(int(sys.argv[1]) if len(sys.argv) > 1 else None))
