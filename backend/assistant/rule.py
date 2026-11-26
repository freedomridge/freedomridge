"""Who may use the assistant, and through which door: the one place the rule is answered (docs/ASSISTANT.md).

The doors: 'mcp', the MCP server, which an administrator switches on in Settings → Assistant, and 'chat', the chat
in the app, which is always off in this release. For a door that is on, in order: a user's own answer
(assistant_users) decides; otherwise a user in a listed group (assistant_groups) may; otherwise their role decides.
A demo account never may, nor a follower instance's account, nor an inactive account or one that owes a password
change. A guest never may use the MCP server, whatever the settings say of them (kept for the chat in the app).

`door_refusal` answers it for the doors, `call_tool` before each tool call, `get_current_user` for each request a
tool makes, and `GET /api/auth/me` for the signed-in user's own account. Each reads the database afresh, through the
handle its caller passes (`source`: the authentication passes its own) or the application's (database.db), read at
the call. Only `GET /api/auth/me`, a hint for the app, first asks `mcp_switched_on`, which reads the switch at most
once every few seconds per process: while the MCP server is off, that page load costs no query. Asked with
`?fresh=1`, it skips that memory and asks the rule.

The administration tools (admin.py) need more: the administrator role, and their own switch (admin_tools), off as
shipped. `admin_refusal` answers that from the same reading, before each call of one and for each request it makes.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .. import database

Door = Literal['mcp', 'chat']
DOORS: tuple[Door, ...] = ('mcp', 'chat')

# The sentences a refusal says, checked in this order (the contract's table). The account's own state is refused in
# the API's own words: get_current_user refuses such an account with the same two.
MCP_OFF = 'The MCP server is switched off on this server'
CHAT_OFF = 'The assistant is switched off on this server'
ACCOUNT_INACTIVE = 'Account is inactive'
PASSWORD_CHANGE_REQUIRED = 'Password change required before using the API — call /api/auth/change-password.'
NOT_LET_IN = 'Your account may not use the assistant on this server'
# An administration tool, for someone who may use the door: its switch off, or someone who isn't an administrator.
ADMIN_TOOLS_OFF = 'The administration tools are switched off on this server'
ADMINS_ONLY = 'This tool is for administrators'

# As shipped, and for a server whose settings row is missing: the door off, administrators let in.
DEFAULTS: dict[str, bool] = {
    'mcp_enabled': False,
    'admin_allowed': True,
    'power_user_allowed': False,
    'guest_allowed': False,
    'new_datasets_closed': False,
    'sample_fields': False,
    'admin_tools': False,
}
ROLES = ('admin', 'power_user', 'guest')

_VERDICT = """
    SELECT s.mcp_enabled, s.admin_allowed, s.power_user_allowed, s.guest_allowed, s.seal_key, s.admin_tools,
           u.id, u.username, u.email, u.role, u.must_change_password, u.is_active, u.is_demo_account,
           (SELECT own.allowed FROM assistant_users own WHERE own.user_id = u.id) AS own_answer,
           EXISTS (SELECT 1 FROM assistant_groups listed JOIN group_members m ON m.group_id = listed.group_id
                   WHERE m.user_id = u.id) AS in_listed_group,
           EXISTS (SELECT 1 FROM followers WHERE followers.user_id = u.id) AS follower_account
    FROM (SELECT 1) one
    LEFT JOIN assistant_settings s ON s.singleton
    LEFT JOIN users u ON u.id = $1
"""
USER_KEYS = ('id', 'username', 'email', 'role', 'must_change_password', 'is_active', 'is_demo_account')


@dataclass(frozen=True)
class Verdict:
    """The user as the database has them now (None: no such account), and why they may not use the door (None:
    they may)."""

    user: Optional[dict[str, Any]]
    refusal: Optional[str]
    seal_key: Optional[bytes] = field(default=None, repr=False)  # the server's key for private values (private.py)
    admin_tools: bool = False  # whether the administration tools are switched on


def _setting(row: Any, key: str) -> bool:
    value = row[key]
    return DEFAULTS[key] if value is None else bool(value)


def decide(row: Any, door: str) -> Optional[str]:
    """The refusal for one row of _VERDICT, or None."""
    if door == 'chat':
        return CHAT_OFF
    if not _setting(row, 'mcp_enabled'):
        return MCP_OFF
    if row['id'] is None or not row['is_active']:
        return ACCOUNT_INACTIVE
    if row['must_change_password']:
        return PASSWORD_CHANGE_REQUIRED
    if row['is_demo_account'] or row['follower_account']:  # a public demo's guest, or a follower instance's machine
        return NOT_LET_IN
    if door == 'mcp' and row['role'] == 'guest':  # whatever the settings say: guests get only the chat in the app
        return NOT_LET_IN
    if row['own_answer'] is not None:
        return None if row['own_answer'] else NOT_LET_IN
    if row['in_listed_group']:
        return None
    role = row['role']
    return None if role in ROLES and _setting(row, f'{role}_allowed') else NOT_LET_IN


def _database(source: Any) -> Any:
    """The handle a query goes through: the caller's, else the application's as it is now (database.db)."""
    return source if source is not None else database.db


async def verdict(user_id: int, door: str, source: Any = None) -> Verdict:
    """Whether `user_id` may use `door` now, read afresh. One query, its connection released before this returns."""
    if door not in DOORS:
        raise ValueError(f"Unknown assistant door {door!r}: one of {', '.join(DOORS)}")
    # Anything but a user id the users table can hold reads as no such account, which is an inactive one.
    known = isinstance(user_id, int) and not isinstance(user_id, bool) and 0 < user_id < 2**31
    row = await _database(source).fetchrow(_VERDICT, user_id if known else None)
    user = None if row['id'] is None else {key: row[key] for key in USER_KEYS}
    seal_key = bytes(row['seal_key']) if row['seal_key'] else None
    return Verdict(user, decide(row, door), seal_key, _setting(row, 'admin_tools'))


def admin_refusal(verdict: Verdict) -> Optional[str]:
    """Why the user of `verdict` may not call an administration tool now (None: they may): the door's own refusal
    first, then the administration tools' switch, then the role."""
    if verdict.refusal is not None or verdict.user is None:
        return verdict.refusal or ACCOUNT_INACTIVE
    if not verdict.admin_tools:
        return ADMIN_TOOLS_OFF
    return None if verdict.user['role'] == 'admin' else ADMINS_ONLY


async def door_refusal(*, user_id: int, door: str, source: Any = None) -> Optional[str]:
    """None when this user may use this door right now, else the sentence that says why not."""
    return (await verdict(user_id, door, source)).refusal


# How long one process trusts the MCP switch it read, for GET /api/auth/me alone (never for a tool call). The
# administrator who changes it sees the change at once (forget_switch); another process within these seconds.
SWITCH_SECONDS = 5.0
_generation = 0  # moved on by forget_switch


async def mcp_switched_on(source: Any = None) -> bool:
    """Whether the MCP server is switched on, read at most once every SWITCH_SECONDS through one database handle. The
    reading is kept on the handle with its pool, so another pool (another database) reads it again."""
    handle = _database(source)
    pool = getattr(handle, 'pool', None)
    now = time.monotonic()
    known = getattr(handle, '_assistant_switch', None)
    if known is not None and known[0] is pool and known[1] == _generation and now - known[2] < SWITCH_SECONDS:
        return bool(known[3])
    value = await handle.fetchval('SELECT mcp_enabled FROM assistant_settings WHERE singleton')
    switched_on = DEFAULTS['mcp_enabled'] if value is None else bool(value)
    try:
        setattr(handle, '_assistant_switch', (pool, _generation, now, switched_on))
    except AttributeError:
        pass  # a handle that keeps no attributes: read every time
    return switched_on


def forget_switch() -> None:
    """The settings changed here: the next mcp_switched_on reads them again."""
    global _generation
    _generation += 1


async def read_settings(source: Any = None) -> dict[str, bool]:
    """The settings row (the shipped defaults where it is missing)."""
    row = await _database(source).fetchrow(
        'SELECT mcp_enabled, admin_allowed, power_user_allowed, guest_allowed, new_datasets_closed, sample_fields, '
        'admin_tools FROM assistant_settings WHERE singleton',
    )
    return dict(DEFAULTS) if row is None else {key: bool(row[key]) for key in DEFAULTS}


# The value a new dataset's assistant_closed takes, as SQL for its INSERT: new_datasets_closed, FALSE without a row.
NEW_DATASET_CLOSED_SQL = 'COALESCE((SELECT new_datasets_closed FROM assistant_settings WHERE singleton), FALSE)'
