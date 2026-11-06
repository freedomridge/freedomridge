"""Compute workers: machines that connect with a worker token (python -m fridge.worker) to run compute jobs
for the server, starting with map re-fits. They register and heartbeat into the workers table, which the
server's own job runners write too; system status counts the compute ones online."""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import map_refit
from ..database import db
from ..validation import clean_name
from .auth import get_worker_user, require_role

router = APIRouter()

# The job kinds a compute worker can take.
COMPUTE_KINDS = (map_refit.KIND,)
# How often a worker heartbeats; system status and the list count a worker heard from in the last 60 s online.
HEARTBEAT_SECONDS = 20


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
