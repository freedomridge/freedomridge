"""Every write route says who may call it (backend/access.py), as exactly one of:

- read: a POST for a large query body, not a write (access._READ_ONLY_DATASET_POSTS, or BODY_READS below);
- level: a level in its dataset, from require_level or a check marked with access.checks_level;
- admin: the administrator role;
- machine: a follower's or compute worker's token (MACHINE says why that decides);
- self: the caller's own account (SELF).

A new write route without one fails here, with its path and handler. `python -m backend.tests.test_route_levels`
prints the table of every write route and who may call it.
"""

import ast
import inspect
import textwrap

from fastapi.routing import APIRoute

from backend import access
from backend.main import app
from backend.routers import auth, storage, users

SELF = {
    'login': 'signs in',
    'logout': 'signs out',
    'demo_login': 'signs in as a new demo guest',
    'change_password': 'their own password',
    'create_token': 'their own API token (power users and administrators)',
    'revoke_token': 'their own API token',
    'create_view': 'their own saved view, in a dataset they can open (view)',
}
# POSTs that name their dataset in the body and check it at view in the handler: reads.
BODY_READS = {'vector_search', 'search_by_image', 'patch_search', 'object_to_patch_search'}
MACHINE = {
    auth.get_follower_user: "a follower's token: what the administrator's subscription of it to the dataset allows",
    auth.get_worker_user: "a compute worker's token: the jobs it claimed, on datasets its owner may edit",
}
ADMIN = (users.require_admin, storage.require_admin)


def _routes() -> list[tuple[str, APIRoute]]:
    """Every route that isn't a GET, with its full path."""

    def walk(routes, prefix=''):
        for route in routes:
            if hasattr(route, 'original_router'):  # FastAPI 0.137+ lists an included router, not its routes
                yield from walk(route.original_router.routes, prefix + route.include_context.prefix)
            elif isinstance(route, APIRoute) and (route.methods or set()) - {'GET', 'HEAD', 'OPTIONS'}:
                yield prefix + route.path, route

    found = list(walk(app.routes))
    assert len(found) > 100  # the walk still finds them
    return found


def _calls(route: APIRoute) -> list:
    """The handler and every dependency it has, nested ones too."""

    def walk(dependant):
        for dependency in dependant.dependencies:
            yield dependency.call
            yield from walk(dependency)

    return [route.endpoint, *walk(route.dependant)]


def _role(call) -> str | None:
    """The role a dependency requires, if it is a role check."""
    if call in ADMIN:
        return 'admin'
    if getattr(call, '__qualname__', '') == 'require_role.<locals>.check_role':
        return inspect.getclosurevars(call).nonlocals['role']
    return None


def classes(route: APIRoute) -> dict[str, str]:
    """Each class the route is in, with who may call it."""
    calls = _calls(route)
    name = route.endpoint.__name__
    found = {}
    roles = {_role(call) for call in calls}
    if name in access._READ_ONLY_DATASET_POSTS or name in BODY_READS:
        found['read'] = 'view, not guests' if 'power_user' in roles else 'view'
    levels = [call.__fridge_level__ for call in calls if getattr(call, '__fridge_level__', None)]
    if levels:
        found['level'] = '; '.join(levels)
    if 'admin' in roles:
        found['admin'] = 'the administrator role'
    machine = [MACHINE[call] for call in calls if call in MACHINE]
    if machine:
        found['machine'] = machine[0]
    if name in SELF:
        found['self'] = SELF[name]
    return found


def _describe(path: str, route: APIRoute) -> str:
    return f"{','.join(sorted(route.methods or ()))} {path} ({route.endpoint.__module__}.{route.endpoint.__name__})"


def test_every_write_route_says_who_may_call_it():
    wrong = [
        f'{_describe(path, route)}: {sorted(classes(route)) or "unclassified"}'
        for path, route in _routes()
        if len(classes(route)) != 1
    ]
    assert not wrong, 'Each write route needs exactly one class:\n' + '\n'.join(wrong)


def _checks_level(function, depth: int = 2) -> bool:
    """Whether `function`, or a function of its module it calls (to `depth`), checks a level: check_dataset_access
    with a level, or caller_level."""
    function = inspect.unwrap(function)
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(function)))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        if name == 'caller_level' or (
            name == 'check_dataset_access'
            and (len(node.args) >= 3 or any(keyword.arg == 'level' for keyword in node.keywords))
        ):
            return True
        callee = function.__globals__.get(name) if isinstance(node.func, ast.Name) else None
        if depth and inspect.isfunction(callee) and callee.__module__ == function.__module__:
            if _checks_level(callee, depth - 1):
                return True
    return False


def test_a_marked_level_check_is_made():
    marked = {call for _, route in _routes() for call in _calls(route) if getattr(call, '__fridge_level__', None)}
    assert getattr(access.require_level('edit'), '__fridge_level__') == 'edit'
    unchecked = [f'{call.__module__}.{call.__qualname__}' for call in marked if not _checks_level(call)]
    assert not unchecked, 'Marked with checks_level, but no level is checked:\n' + '\n'.join(sorted(unchecked))


def test_the_check_scan_needs_a_level():
    async def view_only(id, user):
        await access.check_dataset_access(id, user)

    async def edit(id, user, state):
        await access.check_dataset_access(id, user, 'edit', state)

    assert not _checks_level(view_only) and _checks_level(edit)


def test_no_level_route_writes_to_a_dataset_being_deleted_unless_it_says_why():
    """require_dataset_access and require_level refuse a write to a dataset being deleted (409); a level route
    without either says why it runs then."""
    silent = []
    for path, route in _routes():
        calls = _calls(route)
        if not any(getattr(call, '__fridge_level__', None) for call in calls):
            continue
        refused = access.require_dataset_access in calls or any(
            getattr(call, '__qualname__', '') == 'require_level.<locals>.check_level' for call in calls
        )
        if not refused and not any(getattr(call, '__fridge_while_deleting__', None) for call in calls):
            silent.append(_describe(path, route))
    assert not silent, 'Level routes that never refuse a dataset being deleted:\n' + '\n'.join(silent)


def route_table() -> str:
    """Markdown: every write route and who may call it, by class."""
    order = ['level', 'read', 'self', 'admin', 'machine']
    rows = []
    for path, route in _routes():
        [(kind, who)] = classes(route).items()
        rows.append(
            (order.index(kind), path, ','.join(sorted(route.methods or ())), kind, who, route.endpoint.__name__),
        )
    lines = ['| Method | Path | Class | Who may call it | Handler |', '|---|---|---|---|---|']
    for _, path, methods, kind, who, name in sorted(rows):
        lines.append(f'| {methods} | `{path}` | {kind} | {who} | `{name}` |')
    return '\n'.join(lines)


if __name__ == '__main__':
    print(route_table())
