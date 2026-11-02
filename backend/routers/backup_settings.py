"""Settings → System status → Backups: the schedule and "Back up now" (admins only).

The job runner does the work (backend/backups.py); these endpoints only save the schedule and queue a
backup job, which then shows in the Jobs list like any other.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictInt

from .. import backups
from ..database import db
from .auth import require_role

router = APIRouter(prefix='/admin/backups', tags=['backups'])


class BackupSchedule(BaseModel):
    # All three are required, so a partial body can't turn backups off or reset the start hour.
    interval_hours: Optional[StrictInt] = Field(..., ge=1, le=8760)  # None: no scheduled backups
    start_hour: StrictInt = Field(..., ge=0, le=23)
    keep: StrictInt = Field(..., ge=1, le=100)


@router.get('/schedule')
async def get_schedule(user: dict = Depends(require_role('admin'))):
    return await db.fetchrow('SELECT interval_hours, start_hour, keep, timing_changed_at FROM backup_schedule')


@router.put('/schedule')
async def put_schedule(body: BackupSchedule, user: dict = Depends(require_role('admin'))):
    # The right-hand sides read the old row: timing_changed_at moves only when the timing does.
    return await db.fetchrow(
        """UPDATE backup_schedule SET interval_hours=$1, start_hour=$2, keep=$3, updated_by=$4,
               timing_changed_at=CASE WHEN (interval_hours, start_hour) IS DISTINCT FROM ($1, $2)
                                      THEN now() ELSE timing_changed_at END
           RETURNING interval_hours, start_hour, keep, timing_changed_at""",
        body.interval_hours,
        body.start_hour,
        body.keep,
        user['id'],
    )


@router.post('/run', status_code=202)
async def run_backup(user: dict = Depends(require_role('admin'))):
    if not backups.BACKUP_DIR:
        raise HTTPException(status_code=409, detail='Set FRIDGE_BACKUP_DIR in .env and restart the backend first')
    job, started = await backups.run_now(db.pool, user['id'])
    return {'job_id': str(job['id']), 'state': job['state'], 'started': started}
