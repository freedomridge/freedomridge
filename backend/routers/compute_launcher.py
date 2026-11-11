"""On-demand compute workers' settings and machines (docs/ON_DEMAND_WORKERS.md), for admins: Settings → Compute
Workers. The launcher itself runs in the job runner (backend/compute_launcher.py)."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import compute_launcher
from ..crypto import encrypt_secret
from ..database import db
from .auth import require_role

router = APIRouter()
SHOWN = (
    'enabled, region, instance_type, subnet_id, security_group_ids, server_url, image, access_key_id, wait_minutes, '
    'idle_minutes, max_machines, max_hours_per_machine, monthly_hours, last_error, last_error_at, updated_at'
)


class LauncherSettings(BaseModel):
    enabled: bool = False
    region: str = Field('', pattern=r'^([a-z]{2}(-gov)?-[a-z]+-\d)?$')
    instance_type: str = Field('r7g.2xlarge', pattern=r'^[a-z][a-z0-9-]*\.[a-z0-9]+$')
    subnet_id: Optional[str] = Field(None, pattern=r'^subnet-[0-9a-f]+$')
    security_group_ids: List[str] = Field(default_factory=list, max_length=5)
    server_url: str = Field('', pattern=r'^(https?://[^\s/]+(/\S*)?)?$', max_length=500)
    image: Optional[str] = Field(None, pattern=r'^[a-z0-9][a-z0-9._/:@-]*$', max_length=300)
    access_key_id: Optional[str] = Field(None, pattern=r'^[A-Z0-9]{16,128}$')
    # None keeps the stored secret, "" removes it.
    secret_access_key: Optional[str] = Field(None, max_length=200)
    wait_minutes: int = Field(5, ge=1, le=1440)
    idle_minutes: int = Field(10, ge=1, le=1440)
    max_machines: int = Field(1, ge=1, le=100)
    max_hours_per_machine: float = Field(6, gt=0, le=168)
    monthly_hours: float = Field(40, gt=0, le=10000)


async def current() -> dict:
    row = await db.fetchrow(
        f'SELECT {SHOWN}, secret_access_key_encrypted IS NOT NULL AS has_secret FROM compute_launcher',
    )
    return dict(row)


@router.get('/compute-launcher')
async def get_launcher(user: dict = Depends(require_role('admin'))):
    """The settings (never the secret), this month's machine-hours, and the latest machines."""
    launches = await db.fetch(
        """SELECT id, job_id, instance_id, region, instance_type, market, worker_name, state, launched_at, connected_at,
                  ended_at, end_reason,
                  extract(epoch FROM COALESCE(ended_at, now()) - launched_at) / 3600 AS hours
           FROM compute_launches ORDER BY id DESC LIMIT 50""",
    )
    return {
        'settings': await current(),
        'hours_this_month': await compute_launcher.hours_this_month(db),
        'launches': [dict(row) for row in launches],
    }


@router.put('/compute-launcher')
async def set_launcher(settings: LauncherSettings, user: dict = Depends(require_role('admin'))):
    """Save the settings. Turning it on needs a region and the address the machines reach this server at; the
    machines' worker tokens belong to the admin who saved it last."""
    if any(not group.startswith('sg-') or not group[3:].isalnum() for group in settings.security_group_ids):
        raise HTTPException(status_code=400, detail='Security groups are ids like sg-0123456789abcdef0')
    if settings.enabled and not (settings.region and settings.server_url):
        raise HTTPException(status_code=400, detail='Give the AWS region and the server URL to turn it on')
    if settings.access_key_id and settings.secret_access_key == '':
        raise HTTPException(status_code=400, detail='An access key needs its secret')
    # A machine is looked at and ended with these: with another region or account, AWS wouldn't know it.
    stored = await db.fetchrow('SELECT region, access_key_id FROM compute_launcher')
    changes_aws = (
        settings.region != stored['region']
        or settings.access_key_id != stored['access_key_id']
        or settings.secret_access_key is not None
    )
    if changes_aws and await db.fetchval("SELECT EXISTS (SELECT 1 FROM compute_launches WHERE state <> 'ended')"):
        raise HTTPException(
            status_code=409,
            detail='Machines are still running: change the region or the AWS keys once they have ended. Turn it off '
            'to start no more; each ends when its worker is done.',
        )
    secret = settings.secret_access_key
    await db.execute(
        """UPDATE compute_launcher SET enabled = $1, region = $2, instance_type = $3, subnet_id = $4,
                  security_group_ids = $5, server_url = $6, image = $7, access_key_id = $8,
                  secret_access_key_encrypted = CASE WHEN $9::text IS NULL THEN secret_access_key_encrypted
                                                     WHEN $9 = '' THEN NULL ELSE $9 END,
                  wait_minutes = $10, idle_minutes = $11, max_machines = $12, max_hours_per_machine = $13,
                  monthly_hours = $14, updated_by = $15, updated_at = now()""",
        settings.enabled,
        settings.region,
        settings.instance_type,
        settings.subnet_id,
        settings.security_group_ids,
        settings.server_url.rstrip('/'),
        settings.image,
        settings.access_key_id,
        None if secret is None else (encrypt_secret(secret) if secret else ''),
        settings.wait_minutes,
        settings.idle_minutes,
        settings.max_machines,
        settings.max_hours_per_machine,
        settings.monthly_hours,
        user['id'],
    )
    return await current()


@router.post('/compute-launcher/check')
async def check_launcher(user: dict = Depends(require_role('admin'))):
    """Ask AWS whether it would start a machine with the saved settings (a dry run: nothing starts)."""
    settings = await db.fetchrow('SELECT * FROM compute_launcher')
    if not settings['region']:
        raise HTTPException(status_code=400, detail='Give the AWS region first')
    try:
        message = await compute_launcher.check(settings)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f'AWS would refuse it: {error}') from None
    return {'ok': True, 'message': message}
