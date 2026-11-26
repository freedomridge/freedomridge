"""The assistant's core (docs/ASSISTANT.md): its tools, the text a model is given before them, who may use it, and a
tool call made as the signed-in user. The doors (the MCP server, later the chat) stand on these four:

    from backend import assistant

    assistant.tools()                                   # the tools, in their fixed order
    assistant.instructions()                            # the text a model is given before the tools
    await assistant.door_refusal(user_id=7, door='mcp')  # None, or why that user may not use that door now
    await assistant.call_tool(user_id=7, door='mcp', name='list_datasets', arguments={})

The tools and the call are imported when first used: the routers this package's tools read through import the
authentication, which imports the rule from here.
"""

from typing import Any, Mapping, Optional

from .rule import door_refusal

__all__ = ['call_tool', 'door_refusal', 'instructions', 'tools']


def tools() -> tuple[Any, ...]:
    """The tools, in one fixed order: each has .name, .description, .input_schema, .kind, .level and .routes."""
    from .registry import TOOLS

    return TOOLS


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
