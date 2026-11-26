"""Who an assistant's request is made for, and how it travels: on the ASGI scope, never in a header.

A tool reads data through the HTTP API, in process (backend/assistant/inner.py). Its request carries the user it is
made for under SCOPE_KEY, a key of the ASGI scope that only code in this process can set: a server builds a scope
from the network with the ASGI keys alone, and a header can't name a scope key. get_current_user reads it first
(backend/routers/auth.py), checks the account and the assistant rule afresh, and marks the user it answers as the
assistant's (ASSISTANT_KEY), which the access rule reads (backend/access.py). No token and no cookie is ever put on
such a request.

The administrators' routes (administrators_route) answer across datasets and know nothing of a dataset closed to the
assistant or of a field's values kept from it. get_current_user refuses them, by path, to every tool but an
administration tool (backend/assistant/admin.py), whose request carries Identity.admin and its user ADMIN_TOOL_KEY.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, FrozenSet, Mapping, Optional, Tuple

SCOPE_KEY = 'fridge.assistant'
# The key of the user dict get_current_user answers for an assistant's request: the door it came through.
ASSISTANT_KEY = 'assistant_door'
# Also set for an administration tool's request: such a request reads a dataset closed to the assistant through the
# routes its tool lists (backend/access.py), whose answers the tool keeps to named facts (backend/assistant/private.py).
ADMIN_TOOL_KEY = 'assistant_admin_tool'
# The administrators' routes by path: everything under /api/admin, and the administrators-only reads elsewhere (users,
# groups, followers, primaries, storage, the compute launcher, workers, a dataset's index advice).
ADMINISTRATORS_PATHS = (
    '/api/admin',
    '/api/users',
    '/api/groups',
    '/api/followers',
    '/api/primaries',
    '/api/storage/configs',
    '/api/compute-launcher',
    '/api/workers',
)
_DATASET_INDEX_ADVISOR = re.compile(r'/api/datasets/[^/]+/index-advisor(/.*)?')


@dataclass(frozen=True)
class Identity:
    """The user an assistant's request is made for, the door ('mcp' or 'chat') it came through, and the routes the
    tool that makes it lists, as (method, handler): get_current_user refuses the request when another handler serves
    it."""

    user_id: int
    door: str
    routes: FrozenSet[Tuple[str, Callable[..., Any]]] = field(default_factory=frozenset)
    # The server's key for sealing private values (private.py), read with the rule's query when the call began.
    seal_key: Optional[bytes] = field(default=None, repr=False)
    # An administration tool's request: get_current_user checks the role and the tools' switch afresh, and lets it
    # reach the administrators' routes the tool lists.
    admin: bool = False


def administrators_route(path: str) -> bool:
    """Whether `path` is an administrators' route (ADMINISTRATORS_PATHS, or a dataset's index advice)."""
    return any(path == prefix or path.startswith(prefix + '/') for prefix in ADMINISTRATORS_PATHS) or bool(
        _DATASET_INDEX_ADVISOR.fullmatch(path),
    )


def scope_identity(scope: Mapping[str, Any]) -> Optional[Identity]:
    """The identity an in-process request carries; None for any other request. Only an Identity counts: nothing a
    request from outside can bring (a header, a query parameter) is one."""
    identity = scope.get(SCOPE_KEY)
    return identity if isinstance(identity, Identity) else None


def assistant_door(user: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The door of a user dict that get_current_user answered for an assistant's request; None for anyone else."""
    return None if user is None else user.get(ASSISTANT_KEY)


def admin_tool(user: Optional[Mapping[str, Any]]) -> bool:
    """Whether a user dict is the one get_current_user answered for an administration tool's request."""
    return bool(user is not None and user.get(ASSISTANT_KEY) and user.get(ADMIN_TOOL_KEY) is True)
