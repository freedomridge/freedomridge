"""Compute workers: machines that connect with a worker token (python -m fridge.worker) to run compute jobs
for the server, starting with map re-fits. They register and heartbeat into the workers table, which the
server's own job runners write too; system status counts the compute ones online."""

import asyncio
import fcntl
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import job_queue, map_refit
from ..access import filter_datasets_for_user
from ..database import db
from ..job_queue import JobPolicy
from ..map_artifacts import LocalArtifactStore
from ..validation import clean_name
from .auth import get_worker_user, require_role

router = APIRouter()

# The job kinds a compute worker can take.
COMPUTE_KINDS = (map_refit.KIND,)


@dataclass(frozen=True)
class RemoteKind:
    """A compute kind a worker can claim: its queue policy, which of its steps run on the worker (the others go
    back to the server's runners), the job files the worker may download and upload, and how a completed step
    becomes the server's next one: `finish(job, staged folder, result)` returns its checkpoint. Without
    `finish` the job simply ends with the worker's result. `output_limit(job, name)` caps each output's size."""

    policy: JobPolicy
    runs_remotely: Callable[[Dict[str, Any]], bool] = lambda job: True
    inputs: tuple = ()
    outputs: tuple = ()
    finish: Optional[Callable[[Dict[str, Any], Path, Any], Dict[str, Any]]] = None
    output_limit: Callable[[Dict[str, Any], str], int] = lambda job, name: 0


# The compute kinds a worker can claim now. A map re-fit's worker does its fit; the server exports the vectors
# before and swaps the map in after.
REMOTE_KINDS: Dict[str, RemoteKind] = {
    map_refit.KIND: RemoteKind(
        policy=map_refit.POLICY,
        runs_remotely=lambda job: (job['checkpoint'] or {}).get('phase') == 'fit',
        inputs=map_refit.REMOTE_INPUTS,
        outputs=map_refit.REMOTE_OUTPUTS,
        finish=map_refit.remote_fit_checkpoint,
        output_limit=map_refit.remote_output_limit,
    ),
}
# The largest upload chunk (nginx allows 70 MB on /api/workers/).
UPLOAD_CHUNK_BYTES = 64 * 1024**2
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
# The same for the worker (it drops this job and goes on claiming others), when its owner lost access to the dataset.
ACCESS_LOST = {
    'code': 'lease_lost',
    'message': "This worker's owner can no longer access the job's dataset: stop without reporting",
}


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
    job = await db.fetchrow('SELECT kind, worker_id, dataset_id FROM jobs WHERE id = $1', job_id)
    if job is None or job['worker_id'] != worker_id:
        raise HTTPException(status_code=409, detail=LEASE_LOST)
    return dict(job)


async def _owner_may_access(dataset_id: Optional[int], user: dict) -> None:
    """409 (ACCESS_LOST) unless the worker's owner may still access the job's dataset, as a claim requires. A lease
    stands for the job, not for access the owner has lost since the claim: when an admin removes their access to a
    private dataset, the worker can't download more of its data, upload a result for it or keep the lease. Giving
    the job back (fail, release) stays open; otherwise the lease expires and the job goes back to the queue."""
    if dataset_id is None:
        allowed = user['role'] == 'admin'  # as for a claim: a job on no dataset is for an admin's worker
    else:
        where, args = await filter_datasets_for_user(user)
        allowed = await db.fetchval(
            f'SELECT EXISTS (SELECT 1 FROM datasets WHERE id = ${len(args) + 1} AND {where})',
            *args,
            dataset_id,
        )
    if not allowed:
        raise HTTPException(status_code=409, detail=ACCESS_LOST)


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
    # A waiting worker claims again every CLAIM_WAIT_SECONDS at most: each claim keeps it online.
    await db.execute('UPDATE workers SET heartbeat_at = clock_timestamp() WHERE id = $1', worker_id)
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
                    policies={kind: remote.policy for kind, remote in REMOTE_KINDS.items()},
                    lease_seconds=LEASE_SECONDS,
                    dataset_ids=dataset_ids,
                    unscoped=admin,
                )
            except (job_queue.QueueBusy, job_queue.QueueContended):
                pass  # busy just now: look again within the wait
        if job is not None and not REMOTE_KINDS[job['kind']].runs_remotely(job):
            # A step of the job that isn't the worker's (a map re-fit's start and export): back to the server.
            try:
                await job_queue.publish_step(db.pool, job['id'], job['lease_token'], release=True, placement='server')
            except (job_queue.LeaseLost, job_queue.QueueBusy):
                pass  # it expires back to the queue instead
            job = None
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
    the lease is gone (it expired, or the job ended), or its owner can no longer access the job's dataset: stop
    without reporting."""
    claimed = await _worker_job(worker_id, job_id, user)
    await _owner_may_access(claimed['dataset_id'], user)
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
    """Finish the worker's step with its result. A kind whose work goes on on the server (a map re-fit's swap)
    hands the job back with its uploads; any other ends with the result. It ends cancelled instead when a cancel
    came in first. 400 when the uploads aren't complete; 409 when its owner can no longer access the job's dataset."""
    claimed = await _worker_job(worker_id, job_id, user)
    await _owner_may_access(claimed['dataset_id'], user)
    remote = REMOTE_KINDS.get(claimed['kind'])
    if remote is not None and remote.finish is not None:
        job = await db.fetchrow('SELECT * FROM jobs WHERE id = $1', job_id)
        staged = LocalArtifactStore().staging_dir(job_id, outcome.lease_token)
        try:
            checkpoint = await asyncio.to_thread(remote.finish, dict(job), staged, outcome.result)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        step = job_queue.publish_step(
            db.pool,
            job_id,
            outcome.lease_token,
            checkpoint=checkpoint,
            release=True,
            placement='server',
        )
    else:
        step = job_queue.publish_step(db.pool, job_id, outcome.lease_token, result=outcome.result, finished=True)
    job = await _queue(step)
    await _done_with(worker_id, job_id)
    return {'state': job['state']}


@router.post('/workers/{worker_id}/jobs/{job_id}/fail')
async def fail(worker_id: str, job_id: uuid.UUID, failure: JobFailure, user: dict = Depends(get_worker_user)):
    """Record the job's error. A retryable failure goes back to the queue after the kind's retry delay (until its
    failure budget is spent); any other fails the job."""
    job = await _worker_job(worker_id, job_id, user)
    error = failure.error.model_dump()
    if failure.retryable:
        policy = REMOTE_KINDS[job['kind']].policy if job['kind'] in REMOTE_KINDS else JobPolicy()
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


async def _live_lease(worker_id: str, job_id: uuid.UUID, lease_token: uuid.UUID, user: dict):
    """(the job's RemoteKind, the job) while this worker holds its live lease and its owner may still access the
    job's dataset; 409 otherwise. Every file transfer checks it."""
    await _own_worker(worker_id, user)
    job = await db.fetchrow(
        """SELECT kind, checkpoint, dataset_id FROM jobs WHERE id = $1 AND worker_id = $2 AND lease_token = $3
             AND state = 'running' AND lease_expires_at > clock_timestamp()""",
        job_id,
        worker_id,
        lease_token,
    )
    if job is None or job['kind'] not in REMOTE_KINDS:
        raise HTTPException(status_code=409, detail=LEASE_LOST)
    await _owner_may_access(job['dataset_id'], user)
    return REMOTE_KINDS[job['kind']], dict(job)


@router.get('/workers/{worker_id}/jobs/{job_id}/files/{name}')
async def download(
    worker_id: str,
    job_id: uuid.UUID,
    name: str,
    x_fridge_lease: uuid.UUID = Header(),
    user: dict = Depends(get_worker_user),
):
    """One of the job's inputs (a map re-fit's vectors.bin), for the lease holder. Range requests resume it."""
    remote, _ = await _live_lease(worker_id, job_id, x_fridge_lease, user)
    path = LocalArtifactStore().work_dir(job_id) / name
    if name not in remote.inputs or not path.is_file():
        raise HTTPException(status_code=404, detail=f'This job has no input {name!r}')
    return FileResponse(path, media_type='application/octet-stream')


class _Offset(Exception):
    def __init__(self, size: int):
        self.size = size


def _append(path: Path, offset: int, chunk: bytes) -> int:
    """Append `chunk` if the file is `offset` bytes long, under an exclusive lock: two uploads of the same chunk
    at once add it once, and the other gets _Offset with the size to resume at."""
    path.parent.mkdir(exist_ok=True)
    with open(path, 'ab') as target:
        fcntl.flock(target, fcntl.LOCK_EX)
        size = os.fstat(target.fileno()).st_size
        if size != offset:
            raise _Offset(size)
        target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
        return size + len(chunk)


@router.put('/workers/{worker_id}/jobs/{job_id}/files/{name}')
async def upload(
    worker_id: str,
    job_id: uuid.UUID,
    name: str,
    request: Request,
    offset: int = Query(0, ge=0),
    x_fridge_lease: uuid.UUID = Header(),
    user: dict = Depends(get_worker_user),
):
    """Append a chunk (at most 64 MiB) to one of the job's outputs, staged under this lease, up to the output's
    size limit. `offset` must be the size uploaded so far; a 409 says the size, to resume from after a dropped
    connection."""
    remote, job = await _live_lease(worker_id, job_id, x_fridge_lease, user)
    if name not in remote.outputs:
        raise HTTPException(status_code=404, detail=f'This job takes no output {name!r}')
    # Never more than the chunk limit, nor past what the output can hold: checked before the body is read.
    room = min(UPLOAD_CHUNK_BYTES, remote.output_limit(job, name) - offset)
    declared = request.headers.get('content-length')
    if room < 0 or (declared is not None and declared.isdigit() and int(declared) > room):
        raise HTTPException(status_code=413, detail=f'{name} takes at most {max(0, room)} more bytes here')
    path = LocalArtifactStore().staging_dir(job_id, x_fridge_lease) / name
    size = path.stat().st_size if path.is_file() else 0
    if size != offset:  # before reading the chunk; _append checks again under its lock
        raise HTTPException(status_code=409, detail={'code': 'offset', 'message': 'Resume at size', 'size': size})
    chunk = bytearray()
    async for piece in request.stream():
        chunk += piece
        if len(chunk) > room:
            raise HTTPException(status_code=413, detail=f'{name} takes at most {room} more bytes here')
    try:
        # A lease that ends meanwhile only leaves this lease's folder behind: completion reads its own lease's.
        return {'size': await asyncio.to_thread(_append, path, offset, bytes(chunk))}
    except _Offset as error:
        raise HTTPException(status_code=409, detail={'code': 'offset', 'message': 'Resume at size', 'size': error.size})


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
