"""A private field can't be questioned either (docs/ASSISTANT.md, "Keeping a field's values back"): the rule the API's
filter compiler, its sorting and its summaries keep for a request made for a tool.

Sealing answers keeps a private field's values out of what a tool reads; this keeps them out of what a tool may ask.
A model that may count `salary > 100000` finds every value by halving, and one that may count `diagnosis = "x"`
confirms a guess. So, for a request made for a tool, a private field (one marked values_private, or a path inside
one) may be filtered only:
- for having a value or not (`exists`, `not_exists`, `is_null`, `is_not_null`), and
- for being, or not being, one of the values it was given as tokens (`=`, `!=`, `in`, `not in`), every value a
  token the server sealed for that field of that dataset: a literal the request wrote is refused, also for equality.
Every other test of it (`<`, `>`, a range, `contains`, a region, an element match, ...), sorting by it, a histogram of
it and a summary of its values (min, max, bounds, quantiles, mean, sum, std) are refused, with words that say why and
what works instead. People in the app are not affected: their requests carry no guard.

This is also where a token opens: the compiler gives the guarded tree its plain values, in the places above and
nowhere else (inside a cross-grain sub-filter too). A token that doesn't open (another field's, another dataset's,
one sealed under an earlier key) makes its test match nothing, and is counted for the tool to say so.

inner.request sets the guard (the request's dataset, its private fields, the server's key) for the request it makes;
`filters` runs at the start of every filter compile (routers/datasets.parse_filter_node), `sort`, `aggregation` and
`histogram` where those routes read the field.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from fastapi import HTTPException

from . import private

CODE = 'assistant_private_field'
PRESENCE = frozenset({'exists', 'not_exists', 'is_null', 'is_not_null'})
BY_TOKEN = frozenset({'=', '==', '!=', '<>', 'in', 'not in', 'not_in', 'nin'})
# Operators whose value is itself a filter tree, compiled by the same compiler.
SUB_FILTERS = frozenset({'cg_self', 'parent_matches', 'has_matching_object'})
# Aggregations that count or list a field's values (which the answer's sealing turns into tokens) without ranking them.
COUNTING = frozenset({'count', 'count_values', 'distinct_count', 'distinct', 'values'})
# The operator a test whose token didn't open becomes: it matches nothing (routers/datasets._compile_filter_node).
NOTHING = 'assistant_unreadable'


@dataclass
class Guard:
    """One request made for a tool: the dataset it names, that dataset's private fields, and the server's key."""

    dataset_id: Optional[int]
    fields: frozenset[str]  # its private fields
    seal: private.Seal
    opened: int = 0
    unreadable: int = 0


_GUARD: ContextVar[Optional[Guard]] = ContextVar('fridge_assistant_guard', default=None)


@contextmanager
def guarding(guard: Guard) -> Iterator[Guard]:
    token = _GUARD.set(guard)
    try:
        yield guard
    finally:
        _GUARD.reset(token)


def refused(message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={'code': CODE, 'message': message})


def _kept(name: str) -> str:
    return f"The values of '{name}' are kept from the assistant"


_INSTEAD = 'test whether it has a value (exists, not_exists), or filter it by a value list, by its handle'


def _guard(dataset_id: Optional[int]) -> Optional[Guard]:
    guard = _GUARD.get()
    if guard is not None and (guard.dataset_id is None or (dataset_id is not None and dataset_id != guard.dataset_id)):
        # A request reads the dataset it names, whose private fields the guard holds; anything else is refused.
        raise refused('A request made for the assistant reads only the dataset it names')
    return guard


def filters(node: Any, dataset_id: Optional[int]) -> Any:
    """The filter tree `node`, as the compiler may compile it for this request (see the module's words)."""
    guard = _guard(dataset_id)
    if guard is None:
        return node
    return _node(node, guard)


def _node(node: Any, guard: Guard) -> Any:
    if isinstance(node, list):
        return [_node(item, guard) for item in node]
    if not isinstance(node, dict):
        return node
    if 'logic' in node or 'filters' in node:
        return {**node, 'filters': _node(node['filters'], guard)} if 'filters' in node else node
    name, op = node.get('field'), str(node.get('operator', '=')).lower()
    if isinstance(name, str) and private.covered(name, guard.fields):
        return _private_leaf(node, name, op, guard)
    if op in SUB_FILTERS and isinstance(node.get('value'), dict):
        return {**node, 'value': _node(node['value'], guard)}
    return node


def _private_leaf(node: dict[str, Any], name: str, op: str, guard: Guard) -> dict[str, Any]:
    if op in PRESENCE:
        return node
    if op not in BY_TOKEN:
        raise refused(f'{_kept(name)}: it can\'t be tested with {op!r}. Instead, {_INSTEAD}')
    value = node.get('value')
    values = value if isinstance(value, list) else [value]
    # Every value is looked at before any is opened, so whether a request is refused never depends on what a
    # token holds.
    if not all(private.is_sealed(item) for item in values):
        raise refused(
            f'{_kept(name)}: it is filtered only by the values the assistant was given, never by a value written in '
            f'a request. Instead, {_INSTEAD}',
        )
    opened = []
    for item in values:
        ok, plain = guard.seal.open(item, name)
        if not ok:
            guard.unreadable += 1
            return {'field': name, 'operator': NOTHING}
        opened.append(plain)
    guard.opened += len(opened)
    return {**node, 'value': opened if isinstance(value, list) else opened[0]}


def _private(name: Any) -> Optional[Guard]:
    guard = _GUARD.get()
    return guard if guard is not None and private.covered(name, guard.fields) else None


def sort(name: Any) -> None:
    """Sorting by a private field ranks its values: refused."""
    if _private(name) is not None:
        raise refused(f"{_kept(name)}: samples can't be sorted by it")


def aggregation(name: Any, op: str) -> None:
    """A summary of a private field's values is refused; counting them isn't."""
    if op not in COUNTING and _private(name) is not None:
        raise refused(f"{_kept(name)}: the {op} of its values can't be given. Counts of it can")


def histogram(name: Any) -> None:
    """A histogram's bins and edges are a private field's values: refused (field_values counts them instead)."""
    if _private(name) is not None:
        raise refused(f"{_kept(name)}: a histogram of it can't be given. Counts of its values can")
