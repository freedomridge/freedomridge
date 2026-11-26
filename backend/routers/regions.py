"""Named regions over HTTP (docs/REGIONS.md): finding them, for anyone signed in, and loading their boundary sets,
for administrators. The filter that uses them is `geo_region` (backend/routers/datasets.py)."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import regions
from ..database import db
from .auth import get_current_user, require_role

router = APIRouter()


@router.get('/regions')
async def search_regions(
    search: str = Query('', max_length=200),
    kind: Optional[Literal['country', 'state', 'group']] = None,
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    """Regions whose key or name matches `search`: [{key, kind, name, country_key, country_name}], the best match
    first. Empty when no boundary set is loaded."""
    return await regions.search(db, search, kind, limit)


@router.get('/regions/{key}')
async def get_region(key: str, outline: bool = False, user: dict = Depends(get_current_user)):
    """A region by its key, in any case: its names, set, version and bounding box [west, south, east, north] (west >
    east across the antimeridian), and with `outline` a simplified GeoJSON geometry to draw."""
    found = await regions.region(db, key, with_outline=outline)
    if found is None:
        raise HTTPException(status_code=404, detail=await regions.unknown(db, key))
    return found


@router.get('/admin/regions')
async def list_region_sets(user: dict = Depends(require_role('admin'))):
    """The boundary sets this server can load, and what is loaded of each."""
    return {'sets': await regions.sets(db)}


class RegionLoad(BaseModel):
    set: str


@router.post('/admin/regions/load', status_code=202)
async def load_region_set(body: RegionLoad, user: dict = Depends(require_role('admin'))):
    """Start downloading and loading a boundary set (a job), or return the region load already running."""
    if body.set not in regions.SETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown boundary set {body.set!r}; the sets are: {', '.join(sorted(regions.SETS))}",
        )
    job, started = await regions.start_load(db.pool, body.set, user['id'])
    return {'job_id': str(job['id']), 'state': job['state'], 'started': started}
