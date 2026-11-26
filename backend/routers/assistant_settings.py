"""Settings → Assistant, for administrators (docs/ASSISTANT.md): whether the MCP server is on, who may use the
assistant (each role, the groups that let their members in, and each user's own answer), whether new datasets
start closed to it, and whether administrators may use the administration tools. This server's own: nothing here is
synced to followers.

Every route answers the whole settings, as GET does. The rule that reads them is backend/assistant/rule.py."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from ..assistant import rule
from ..database import db
from .users import require_admin

router = APIRouter(prefix='/admin/assistant', tags=['assistant'])


class RolesUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    admin: Optional[bool] = None
    power_user: Optional[bool] = None
    guest: Optional[bool] = None


class SettingsUpdate(BaseModel):
    """Keys left out don't change."""

    model_config = ConfigDict(extra='forbid')

    mcp_enabled: Optional[bool] = None
    roles: Optional[RolesUpdate] = None
    new_datasets_closed: Optional[bool] = None
    sample_fields: Optional[bool] = None  # the assistant may read samples' own fields and file paths
    admin_tools: Optional[bool] = None  # an administrator's AI client may read status, jobs, backups and indexes


class UserAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid')

    allowed: bool


async def settings_answer() -> dict[str, Any]:
    settings = await rule.read_settings()
    groups = await db.fetch(
        'SELECT g.id, g.name FROM assistant_groups a JOIN groups g ON g.id = a.group_id ORDER BY g.name, g.id',
    )
    users = await db.fetch(
        'SELECT u.id, u.username, a.allowed FROM assistant_users a JOIN users u ON u.id = a.user_id '
        'ORDER BY u.username, u.id',
    )
    return {
        'mcp_enabled': settings['mcp_enabled'],
        'roles': {role: settings[f'{role}_allowed'] for role in rule.ROLES},
        'groups': [dict(row) for row in groups],
        'users': [dict(row) for row in users],
        'new_datasets_closed': settings['new_datasets_closed'],
        'sample_fields': settings['sample_fields'],
        'admin_tools': settings['admin_tools'],
    }


@router.get('')
async def get_assistant_settings(admin: dict = Depends(require_admin)):
    return await settings_answer()


@router.patch('')
async def update_assistant_settings(update: SettingsUpdate, admin: dict = Depends(require_admin)):
    columns: dict[str, bool] = {}
    if update.mcp_enabled is not None:
        columns['mcp_enabled'] = update.mcp_enabled
    if update.new_datasets_closed is not None:
        columns['new_datasets_closed'] = update.new_datasets_closed
    if update.sample_fields is not None:
        columns['sample_fields'] = update.sample_fields
    if update.admin_tools is not None:
        columns['admin_tools'] = update.admin_tools
    if update.roles is not None:
        for role in rule.ROLES:
            value = getattr(update.roles, role)
            if value is not None:
                columns[f'{role}_allowed'] = value
    if columns:
        names = list(columns)
        assignments = ', '.join(f'{name} = ${index}' for index, name in enumerate(names, start=2))
        # The one row; written back if someone removed it, so the settings saved are the ones read.
        await db.execute('INSERT INTO assistant_settings (singleton) VALUES (TRUE) ON CONFLICT DO NOTHING')
        await db.execute(
            f'UPDATE assistant_settings SET {assignments}, updated_by = $1, updated_at = now() WHERE singleton',
            admin['id'],
            *[columns[name] for name in names],
        )
        rule.forget_switch()
    return await settings_answer()


async def _require(table: str, row_id: int, what: str) -> None:
    if not await db.fetchval(f'SELECT EXISTS (SELECT 1 FROM {table} WHERE id = $1)', row_id):
        raise HTTPException(status_code=404, detail=f'{what} not found')


@router.put('/groups/{group_id}')
async def let_group_in(group_id: int, admin: dict = Depends(require_admin)):
    """The group's members may use the assistant (a group only ever lets people in)."""
    await _require('groups', group_id, 'Group')
    await db.execute(
        'INSERT INTO assistant_groups (group_id, added_by) VALUES ($1, $2) ON CONFLICT (group_id) DO NOTHING',
        group_id,
        admin['id'],
    )
    return await settings_answer()


@router.delete('/groups/{group_id}')
async def take_group_out(group_id: int, admin: dict = Depends(require_admin)):
    await _require('groups', group_id, 'Group')
    await db.execute('DELETE FROM assistant_groups WHERE group_id = $1', group_id)
    return await settings_answer()


@router.put('/users/{user_id}')
async def set_user_answer(user_id: int, answer: UserAnswer, admin: dict = Depends(require_admin)):
    """The user's own answer, which decides for them before their groups and their role."""
    await _require('users', user_id, 'User')
    await db.execute(
        """INSERT INTO assistant_users (user_id, allowed, set_by) VALUES ($1, $2, $3)
           ON CONFLICT (user_id) DO UPDATE SET allowed = EXCLUDED.allowed, set_by = EXCLUDED.set_by, set_at = now()""",
        user_id,
        answer.allowed,
        admin['id'],
    )
    return await settings_answer()


@router.delete('/users/{user_id}')
async def remove_user_answer(user_id: int, admin: dict = Depends(require_admin)):
    """Their groups and their role decide again."""
    await _require('users', user_id, 'User')
    await db.execute('DELETE FROM assistant_users WHERE user_id = $1', user_id)
    return await settings_answer()
