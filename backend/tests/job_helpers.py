"""Run job-backed endpoints against a real runner slot in the test process."""

import asyncio
from contextlib import asynccontextmanager

from backend import concept_jobs, duplicate_jobs, index_actions, index_jobs, job_queue
from backend.tests.test_job_runner import make_worker


def handlers(pool):
    return {
        concept_jobs.EXPLICIT_KIND: concept_jobs.explicit_handler(pool),
        index_jobs.FIELD_KIND: index_jobs.field_handler(pool),
        index_jobs.ANN_KIND: index_jobs.ann_handler(pool),
        duplicate_jobs.KIND: duplicate_jobs.handler(pool),
        index_actions.DROP_KIND: index_actions.drop_handler(pool),
        index_actions.RESTORE_KIND: index_actions.restore_handler(pool),
    }


POLICIES = {
    concept_jobs.EXPLICIT_KIND: concept_jobs.EXPLICIT_POLICY,
    index_jobs.FIELD_KIND: index_jobs.FIELD_POLICY,
    index_jobs.ANN_KIND: index_jobs.ANN_POLICY,
    duplicate_jobs.KIND: duplicate_jobs.POLICY,
    index_actions.DROP_KIND: index_actions.POLICY,
    index_actions.RESTORE_KIND: index_actions.POLICY,
}


@asynccontextmanager
async def runner(database, *, kinds=None):
    """One slot polling every 50 ms, so a small job finishes inside the endpoints' five seconds."""
    selected = {kind: handler for kind, handler in handlers(database.pool).items() if kinds is None or kind in kinds}
    worker = await make_worker(
        database,
        selected,
        policies={kind: POLICIES[kind] for kind in selected},
        poll_seconds=0.05,
    )
    task = asyncio.create_task(worker.run())
    try:
        yield worker
    finally:
        worker.stop()
        await asyncio.wait_for(task, 60)


async def step(database, job, handler, *, release=False):
    """Measure and publish one unit, exactly as a slot would."""
    unit = await handler(job)
    return await job_queue.publish_step(
        database.pool,
        job['id'],
        job['lease_token'],
        checkpoint=unit.checkpoint,
        done=unit.done,
        total=unit.total,
        message=unit.message,
        result=unit.result,
        finished=unit.finished,
        release=release and not unit.finished,
        publisher=unit.publish,
    )


async def claim(database, kind, *, lease_seconds=600):
    return await job_queue.claim_job(database.pool, 'long-endpoint-test', kinds=[kind], lease_seconds=lease_seconds)


async def finish(database, kind, job=None, *, limit=10000):
    """Claim (unless given a claimed job) and run units until the job ends."""
    handler = handlers(database.pool)[kind]
    job = job or await claim(database, kind)
    assert job is not None, 'no claimable job'
    for _ in range(limit):
        job = await step(database, job, handler)
        if job['state'] != 'running':
            return job
    raise AssertionError('job did not finish')


def with_runner(scenario, *, kinds=None):
    """Wrap a registry_database scenario so job-backed endpoints answer from a live slot."""

    async def wrapped(database):
        async with runner(database, kinds=kinds):
            await scenario(database)

    return wrapped
