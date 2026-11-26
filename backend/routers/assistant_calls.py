"""The assistant's recent calls, for administrators (backend/assistant_calls.py)."""

from fastapi import APIRouter, Depends, Query

from .. import assistant_calls
from ..database import db
from .auth import require_role

router = APIRouter()


@router.get("/admin/assistant/calls")
async def list_assistant_calls(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_role("admin")),
):
    """The tool calls made through the assistant's doors in the last 30 days, newest first: who, which door and tool,
    which dataset the arguments named, when, how long, and whether it succeeded. Never the arguments or the result.
    A deleted user's rows have no user_id or username."""
    return await assistant_calls.page(db.pool, limit=limit, offset=offset)
