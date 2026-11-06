"""Compute workers: machines that connect with a worker token (python -m fridge.worker) to run compute jobs
for the server, starting with map re-fits. They register and heartbeat into the workers table, which the
server's own job runners write too; system status counts the compute ones online."""

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .. import job_queue, map_refit
from ..access import filter_datasets_for_user
from ..database import db
from ..job_queue import JobPolicy
from ..validation import clean_name
from .auth import get_worker_user, require_role

router = APIRouter()

# The job kinds a compute worker can take.
COMPUTE_KINDS = (map_refit.KIND,)
# The compute kinds a worker can claim now, with their queue policy. A kind joins with its handler on the worker
# and its hand-off on the server (the map re-fit next); until then its jobs stay with the server's runner.
REMOTE_KINDS: Dict[str, JobPolicy] = {}
# How often a worker heartbeats; system status and the list count a worker heard from in the last 60 s online.
HEARTBEAT_SECONDS = 20
# A claimed job's lease, and how often the worker renews it: a worker that stops renewing (a laptop asleep, the
# network down) loses the job after LEASE_SECONDS, and it goes back to the queue at its last checkpoint.
LEASE_SECONDS = 120
JOB_HEARTBEAT_SECONDS = 30
# The longest a claim waits for a job (long polling), within nginx's 60 s proxy read timeout.
CLAIM_WAIT_SECONDS = 25


class WorkerCapabilities(BaseModel):
    memory_bytes: Optional[int] = Field(None, ge=0)
    cpus: Optional[int] = Field(None, ge=1)
    gpu: bool = False


class WorkerRegistration(BaseModel):
    name: str
    hostname: str = Field('', max_length=255)
    sdk_version: str = Field('', max_length=64)
    kinds: List[str] = Field(min_length=1, max_length=len(COMPUTE_KINDS))
    capabilities: WorkerCapabilities = Field(default_factory=WorkerCapabilities)


@router.post('/workers/register')
async def register_worker(registration: WorkerRegistration, user: dict = Depends(get_worker_user)):
    """Register this machine as a compute worker of the token's user, as a new worker each time it starts."""
    unknown = sorted(set(registration.kinds) - set(COMPUTE_KINDS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Not compute job kinds: {', '.join(unknown)}. A worker can take: {', '.join(COMPUTE_KINDS)}",
        )
    worker_id = f'compute-{uuid.uuid4()}'
    capabilities = {**registration.capabilities.model_dump(), 'kinds': sorted(set(registration.kinds))}
    await db.execute(
        """INSERT INTO workers (id, placement, name, hostname, capabilities, user_id, sdk_version)
           VALUES ($1, 'compute', $2, $3, $4, $5, $6)""",
        worker_id,
        clean_name(registration.name, 'Worker name'),
        registration.hostname,
        capabilities,
        user['id'],
        registration.sdk_version,
    )
    return {'worker_id': worker_id, 'heartbeat_seconds': HEARTBEAT_SECONDS}


@router.post('/workers/{worker_id}/heartbeat')
async def heartbeat(worker_id: str, user: dict = Depends(get_worker_user)):
    """Keep the worker online. 404 when the server no longer knows it (its row is dropped after a day without a
    heartbeat): the worker registers again."""
    known = await db.fetchval(
        """UPDATE workers SET heartbeat_at = clock_timestamp()
           WHERE id = $1 AND placement = 'compute' AND user_id = $2 RETURNING id""",
        worker_id,
        user['id'],
    )
    if known is None:
        raise HTTPException(status_code=404, detail='Unknown worker: register again')
    return {'heartbeat_seconds': HEARTBEAT_SECONDS}


class ClaimRequest(BaseModel):
    wait_seconds: float = Field(0, ge=0, le=CLAIM_WAIT_SECONDS)


class JobLease(BaseModel):
    lease_token: uuid.UUID


class JobProgress(JobLease):
    done: Optional[int] = Field(None, ge=0)
    total: Optional[int] = Field(None, ge=0)
    message: Optional[str] = Field(None, max_length=500)


class JobOutcome(JobLease):
    result: Any = None


class JobError(BaseModel):
    type: str = Field(max_length=100)
    message: str = Field(max_length=2000)


class JobFailure(JobLease):
    error: JobError
    # A failure that may pass (the machine ran short, the network dropped): the job goes back to the queue,
    # until the kind's failure budget is spent.
    retryable: bool = False


LEASE_LOST = {'code': 'lease_lost', 'message': 'This worker no longer holds the job: stop without reporting'}


async def _own_worker(worker_id: str, user: dict) -> Dict[str, Any]:
    row = await db.fetchrow(
        "SELECT id, capabilities FROM workers WHERE id = $1 AND placement = 'compute' AND user_id = $2",
        worker_id,
        user['id'],
    )
    if row is None:
        raise HTTPException(status_code=404, detail='Unknown worker: register again')
    return dict(row)


async def _worker_job(worker_id: str, job_id: uuid.UUID, user: dict) -> Dict[str, Any]:
    """The job this worker claimed; 409 when it is someone else's now (or never was)."""
    await _own_worker(worker_id, user)
    job = await db.fetchrow('SELECT kind, worker_id FROM jobs WHERE id = $1', job_id)
    if job is None or job['worker_id'] != worker_id:
        raise HTTPException(status_code=409, detail=LEASE_LOST)
    return dict(job)


async def _queue(operation):
    """A queue call for a worker: a lost lease is a 409, a busy queue a 503 the worker retries."""
    try:
        return await operation
    except job_queue.LeaseLost:
        raise HTTPException(status_code=409, detail=LEASE_LOST)
    except (job_queue.QueueBusy, job_queue.QueueContended) as error:
        raise HTTPException(status_code=503, detail=str(error), headers={'Retry-After': '2'})


async def _done_with(worker_id: str, job_id: uuid.UUID) -> None:
    await db.execute(
        """UPDATE workers SET current_job_id = NULL, heartbeat_at = clock_timestamp()
           WHERE id = $1 AND current_job_id = $2""",
        worker_id,
        job_id,
    )


@router.post('/workers/{worker_id}/claim')
async def claim(worker_id: str, request: ClaimRequest, user: dict = Depends(get_worker_user)):
    """The next compute job this worker can take, waiting up to `wait_seconds` for one (long polling); 204 when
    none came. Only kinds the worker registered for that run remotely now (REMOTE_KINDS), and only jobs on
    datasets its owner can access (jobs on no dataset only for an admin's worker). The job comes with a lease
    of LEASE_SECONDS: renew it with the job heartbeat every JOB_HEARTBEAT_SECONDS."""
    worker = await _own_worker(worker_id, user)
    kinds = [kind for kind in worker['capabilities'].get('kinds', []) if kind in REMOTE_KINDS]
    admin = user['role'] == 'admin'
    dataset_ids = None
    if not admin:
        where, args = await filter_datasets_for_user(user)
        dataset_ids = [row['id'] for row in await db.fetch(f'SELECT id FROM datasets WHERE {where}', *args)]
    deadline = time.monotonic() + request.wait_seconds
    while True:
        job = None
        if kinds:
            try:
                job = await job_queue.claim_job(
                    db.pool,
                    worker_id,
                    placements=('compute',),
                    kinds=kinds,
                    policies=REMOTE_KINDS,
                    lease_seconds=LEASE_SECONDS,
                    dataset_ids=dataset_ids,
                    unscoped=admin,
                )
            except (job_queue.QueueBusy, job_queue.QueueContended):
                pass  # busy just now: look again within the wait
        if job is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Response(status_code=204)
        await asyncio.sleep(min(1.0, remaining))
    await db.execute(
        'UPDATE workers SET current_job_id = $2, heartbeat_at = clock_timestamp() WHERE id = $1',
        worker_id,
        job['id'],
    )
    return {
        'id': str(job['id']),
        'kind': job['kind'],
        'dataset_id': job['dataset_id'],
        'args': job['args'],
        'checkpoint': job['checkpoint'],
        'lease_token': str(job['lease_token']),
        'lease_seconds': LEASE_SECONDS,
        'heartbeat_seconds': JOB_HEARTBEAT_SECONDS,
    }


@router.post('/workers/{worker_id}/jobs/{job_id}/heartbeat')
async def job_heartbeat(worker_id: str, job_id: uuid.UUID, report: JobProgress, user: dict = Depends(get_worker_user)):
    """Renew the job's lease and report progress. With `cancel_requested`, stop and release the job. 409 when
    the lease is gone (it expired, or the job ended): stop without reporting."""
    await _worker_job(worker_id, job_id, user)
    renewed = await _queue(job_queue.heartbeat_job(db.pool, job_id, report.lease_token, lease_seconds=LEASE_SECONDS))
    if renewed is None:
        raise HTTPException(status_code=409, detail=LEASE_LOST)
    progress = report.done is not None or report.total is not None or report.message is not None
    if progress and not renewed['cancel_requested']:
        await _queue(
            job_queue.publish_step(
                db.pool,
                job_id,
                report.lease_token,
                done=report.done,
                total=report.total,
                message=report.message,
            ),
        )
    await db.execute(
        'UPDATE workers SET heartbeat_at = clock_timestamp(), current_job_id = $2 WHERE id = $1',
        worker_id,
        job_id,
    )
    return {'cancel_requested': renewed['cancel_requested'], 'lease_seconds': LEASE_SECONDS}


@router.post('/workers/{worker_id}/jobs/{job_id}/complete')
async def complete(worker_id: str, job_id: uuid.UUID, outcome: JobOutcome, user: dict = Depends(get_worker_user)):
    """Finish the job with its result. It ends cancelled instead when a cancel came in first."""
    await _worker_job(worker_id, job_id, user)
    job = await _queue(
        job_queue.publish_step(db.pool, job_id, outcome.lease_token, result=outcome.result, finished=True),
    )
    await _done_with(worker_id, job_id)
    return {'state': job['state']}


@router.post('/workers/{worker_id}/jobs/{job_id}/fail')
async def fail(worker_id: str, job_id: uuid.UUID, failure: JobFailure, user: dict = Depends(get_worker_user)):
    """Record the job's error. A retryable failure goes back to the queue after the kind's retry delay (until its
    failure budget is spent); any other fails the job."""
    job = await _worker_job(worker_id, job_id, user)
    error = failure.error.model_dump()
    if failure.retryable:
        policy = REMOTE_KINDS.get(job['kind'], JobPolicy())
        ended = await _queue(
            job_queue.defer_job(db.pool, job_id, failure.lease_token, error, delay_seconds=policy.retry_seconds),
        )
    else:
        ended = await _queue(job_queue.fail_job(db.pool, job_id, failure.lease_token, error))
    await _done_with(worker_id, job_id)
    return {'state': ended['state']}


@router.post('/workers/{worker_id}/jobs/{job_id}/release')
async def release(worker_id: str, job_id: uuid.UUID, lease: JobLease, user: dict = Depends(get_worker_user)):
    """Give the job back: it is queued again at its last checkpoint, or ends cancelled when a cancel came in.
    A worker that stops, or that was asked to cancel, releases its job."""
    await _worker_job(worker_id, job_id, user)
    job = await _queue(job_queue.publish_step(db.pool, job_id, lease.lease_token, release=True))
    await _done_with(worker_id, job_id)
    return {'state': job['state']}


@router.get('/workers')
async def list_workers(user: dict = Depends(require_role('power_user'))):
    """The compute workers and the server's job runners, most recently heard from first. An admin sees every
    worker; a power user sees their own compute workers."""
    rows = await db.fetch(
        """SELECT w.id, w.placement, w.name, w.hostname, w.capabilities, w.sdk_version, w.started_at,
                  w.heartbeat_at, w.current_job_id, u.username AS owner,
                  w.heartbeat_at > clock_timestamp() - interval '60 seconds' AS online
           FROM workers w LEFT JOIN users u ON u.id = w.user_id
           WHERE $1 OR w.user_id = $2
           ORDER BY w.heartbeat_at DESC""",
        user['role'] == 'admin',
        user['id'],
    )
    return [dict(row) for row in rows]
