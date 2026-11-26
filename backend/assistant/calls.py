"""call_tool: one tool call for one user through one door (docs/ASSISTANT.md).

Each call reads the account and the assistant rule afresh (rule.verdict), checks the arguments against the tool's
schema, and runs the tool as the user. An administration tool (admin.py) also needs the administrator role and its
switch (rule.admin_refusal), and runs with Identity.admin, which each of its requests checks again. It answers
{'ok': True, 'result': ...} or {'ok': False, 'error': ...}, plain JSON values, and raises only for a server fault
(api.ToolFailed, or an error of the server's own).
"""

import json
import math
from typing import Any, Mapping, Optional

from . import rule
from .api import Api, ToolError
from .catalog import Tool
from .identity import Identity
from .registry import ADMIN_TOOLS, TOOLS

_JSON_TYPES = {
    'integer': 'a whole number',
    'number': 'a number',
    'string': 'text',
    'object': 'an object',
    'boolean': 'true or false',
    'array': 'a list',
}


def _type_ok(value: Any, kind: str) -> bool:
    if kind == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == 'number':  # a finite one: JSON has no NaN or infinity
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if kind == 'string':
        return isinstance(value, str)
    if kind == 'boolean':
        return isinstance(value, bool)
    if kind == 'object':
        return isinstance(value, dict)
    if kind == 'array':
        return isinstance(value, list)
    return True


def _check_value(where: str, spec: Mapping[str, Any], value: Any) -> Any:
    if not _type_ok(value, spec.get('type', '')):
        raise ToolError('invalid', f"{where}: must be {_JSON_TYPES.get(spec.get('type', ''), spec.get('type'))}")
    if 'enum' in spec and value not in spec['enum']:
        raise ToolError('invalid', f"{where}: one of {', '.join(str(each) for each in spec['enum'])} (got {value!r})")
    if 'minimum' in spec and value < spec['minimum']:
        raise ToolError('invalid', f"{where}: must be at least {spec['minimum']} (got {value})")
    if 'maximum' in spec and value > spec['maximum']:
        raise ToolError('invalid', f"{where}: must be at most {spec['maximum']} (got {value})")
    if 'maxLength' in spec and len(value) > spec['maxLength']:
        # `x-too-long`, where a schema has it: what to do instead (a long list of values goes in a value list).
        instead = f": {spec['x-too-long']}" if spec.get('x-too-long') else ''
        raise ToolError('invalid', f"{where}: at most {spec['maxLength']:,} characters (got {len(value):,}){instead}")
    if spec.get('type') == 'array':
        if 'maxItems' in spec and len(value) > spec['maxItems']:
            raise ToolError('invalid', f"{where}: at most {spec['maxItems']} items (got {len(value)})")
        if 'minItems' in spec and len(value) < spec['minItems']:
            raise ToolError('invalid', f"{where}: at least {spec['minItems']} item{'s' * (spec['minItems'] != 1)}")
        return [_check_value(f'{where}[{index}]', spec.get('items', {}), item) for index, item in enumerate(value)]
    if spec.get('type') == 'object' and 'properties' in spec:
        return _check_object(spec, value, f'{where}.')
    return value


def _check_object(schema: Mapping[str, Any], arguments: dict[str, Any], prefix: str = '') -> dict[str, Any]:
    properties: Mapping[str, Any] = schema.get('properties', {})
    unknown = sorted(set(arguments) - set(properties))
    if unknown and schema.get('additionalProperties') is False:
        known = ', '.join(properties) or 'none'
        what = 'argument' if not prefix else f'key of {prefix[:-1]}'
        raise ToolError('invalid', f"Unknown {what} {prefix}{unknown[0]!r}: it takes {known}")
    for name in schema.get('required', ()):
        if arguments.get(name) is None:
            raise ToolError('invalid', f'{prefix}{name}: required')
    checked = {}
    for name, spec in properties.items():
        value = arguments.get(name)
        if value is None:
            if 'default' in spec:
                checked[name] = spec['default']
            continue
        checked[name] = _check_value(f'{prefix}{name}', spec, value)
    return checked


def check_arguments(schema: Mapping[str, Any], arguments: Any) -> dict[str, Any]:
    """`arguments` checked against the tool's schema (the subset of JSON Schema the tools use), with each default
    filled in. Raises ToolError('invalid') naming the argument that doesn't fit."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError('invalid', 'The arguments must be an object of named values')
    return _check_object(schema, arguments)


def find_tool(name: str) -> Optional[Tool]:
    return next((tool for tool in (*TOOLS, *ADMIN_TOOLS) if tool.name == name), None)


def _failure(error: ToolError) -> dict[str, Any]:
    return {'ok': False, 'error': error.answer()}


async def call_tool(*, user_id: int, door: str, name: str, arguments: Optional[Mapping[str, Any]] = None) -> dict:
    """Run tool `name` for `user_id` through `door`. Never holds a database connection while the tool runs."""
    verdict = await rule.verdict(user_id, door)
    if verdict.refusal is not None:
        return _failure(ToolError('not_allowed', verdict.refusal))
    tool = find_tool(name)
    if tool is None:
        return _failure(ToolError('unknown_tool', f"There is no tool named {name!r}"))
    admin = any(tool is each for each in ADMIN_TOOLS)
    refusal = rule.admin_refusal(verdict) if admin else None
    if refusal is not None:  # an administration tool is never unknown: it is refused, saying why
        return _failure(ToolError('not_allowed', refusal))
    try:
        checked = check_arguments(tool.input_schema, arguments)
        identity = Identity(user_id, door, seal_key=verdict.seal_key, admin=admin)
        result = await tool.run(Api(identity, tool.routes), checked)
    except ToolError as error:
        return _failure(error)
    # Plain JSON values only: what json.dumps writes as it is (a tool builds its result from the API's JSON).
    return {'ok': True, 'result': json.loads(json.dumps(result))}
