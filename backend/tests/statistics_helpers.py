"""Drive the real fenced statistics handler without a live runner or database."""

import asyncio
import time
from unittest.mock import patch

from backend import statistics, statistics_measure


async def run_refresh(database, dataset_id=None, *, force=False):
    from backend.tests.test_job_runner import make_worker

    ids = (
        [dataset_id]
        if dataset_id is not None
        else [
            row['id']
            for row in await database.fetch(
                """SELECT id FROM datasets WHERE EXISTS(SELECT 1 FROM samples WHERE dataset_id=datasets.id)
               OR EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=datasets.id)
               OR EXISTS(SELECT 1 FROM dataset_stats_state WHERE dataset_id=datasets.id) ORDER BY id""",
            )
        ]
    )
    jobs = [await statistics.request_refresh(database.pool, identifier, force=force) for identifier in ids]
    worker = await make_worker(
        database,
        {statistics.KIND: statistics.handler(database.pool)},
        policies={statistics.KIND: statistics.POLICY},
    )
    results = []
    remaining = {job['id'] for job in jobs}
    deadline = time.monotonic() + 30
    while remaining:
        assert time.monotonic() < deadline, 'Statistics test jobs failed to finish within 30 seconds'
        await worker.run_once()
        for job in await database.fetch('SELECT * FROM jobs WHERE id=ANY($1::uuid[])', list(remaining)):
            if job['state'] in ('succeeded', 'failed', 'cancelled'):
                assert job['state'] == 'succeeded', dict(job)
                results.extend(job['result']['datasets'])
                remaining.remove(job['id'])
        await asyncio.sleep(0)
    return results


async def refresh_during(database, during, *, phase='after_measurement', dataset_id=1, all_datasets=False):
    original = statistics_measure.measure_part
    changed = False

    async def measurement(pool, target, *args):
        nonlocal changed
        if target != dataset_id or changed:
            return await original(pool, target, *args)
        changed = True
        if phase == 'before_measurement':
            await during()
            return await original(pool, target, *args)
        measured = await original(pool, target, *args)
        await during()
        return measured

    with patch.object(statistics_measure, 'measure_part', measurement):
        result = await run_refresh(database, None if all_datasets else dataset_id, force=True)
    assert changed, 'The refresh never reached its intended measurement interleaving'
    return result
