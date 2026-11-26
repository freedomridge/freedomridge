"""The assistant's core (docs/ASSISTANT.md): its tools, the text a model is given before them, who may use it, and a
tool call made as the signed-in user. The doors (the MCP server, later the chat) stand on these:

    from backend import assistant

    assistant.tools()                                   # the tools, in their fixed order
    assistant.admin_tools()                             # the administration tools, in their order
    await assistant.tools_for(user_id=7, door='mcp')    # what that user could call through that door now
    assistant.instructions()                            # the text a model is given before the tools
    await assistant.door_refusal(user_id=7, door='mcp')  # None, or why that user may not use that door now
    await assistant.call_tool(user_id=7, door='mcp', name='list_datasets', arguments={})

The tools and the call are imported when first used: the routers this package's tools read through import the
authentication, which imports the rule from here.
"""

from typing import Any, Mapping, Optional

from . import rule
from .rule import door_refusal

__all__ = ['admin_tools', 'call_tool', 'door_refusal', 'instructions', 'tools', 'tools_for']


def tools() -> tuple[Any, ...]:
    """The tools, in one fixed order: each has .name, .description, .input_schema, .kind, .level and .routes."""
    from .registry import TOOLS

    return TOOLS


def admin_tools() -> tuple[Any, ...]:
    """The administration tools (admin.py), in their order: for an administrator while they are switched on."""
    from .registry import ADMIN_TOOLS

    return ADMIN_TOOLS


async def tools_for(*, user_id: int, door: str) -> tuple[Any, ...]:
    """What `user_id` could call through `door` right now: the tools, then the administration tools when the rule lets
    that user call them (rule.admin_refusal, read afresh)."""
    from .registry import ADMIN_TOOLS, TOOLS

    allowed = rule.admin_refusal(await rule.verdict(user_id, door)) is None
    return (*TOOLS, *ADMIN_TOOLS) if allowed else TOOLS


def instructions() -> str:
    """The short text a model is given before the tools."""
    from .registry import INSTRUCTIONS

    return INSTRUCTIONS


async def call_tool(
    *,
    user_id: int,
    door: str,
    name: str,
    arguments: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """{'ok': True, 'result': {...}} or {'ok': False, 'error': {'code', 'message', 'retryable'}}."""
    from .calls import call_tool as call

    return await call(user_id=user_id, door=door, name=name, arguments=arguments)
