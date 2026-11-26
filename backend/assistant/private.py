"""`values_private`, kept in one place for every request made for a tool (docs/ASSISTANT.md, "Keeping a field's values
back"): a value of a private field never reaches a tool as itself, and a tool can't turn a token back into one.

inner.request runs every answer of every route a tool calls through `seal_answer`, which replaces each value of the
request's dataset's private fields with a token (`pv1.<...>`): AES-SIV encryption of the value, padded to a fixed
length so a token doesn't tell a value's length, bound to its dataset and field (the associated data), with a random
key the server made for itself and keeps in assistant_settings.seal_key (migration 0036; never derived from a
configured secret, which has a public default). Equal values of one field give equal tokens, so counts beside them say
how often each (unnamed) value occurs and which samples share one, never what it is.

A token opens in exactly one place: the API's filter compiler, as the value of an `=`, `!=`, `in` or `not in` test of
its own field in a request about its own dataset (guard.py, which also refuses every other way of questioning a
private field). Anywhere else a token is the literal string it is, which matches nothing and shows nothing. One sealed
under an earlier key doesn't open: its test matches nothing.

The people's endpoints (GET /api/assistant/views/{handle} and /lists/{handle}/csv, on the working-views branch) open
tokens for the person, who may see the values; no tool may list them, and a request made for a tool is refused there.

Which parts of an answer are values is read from its shape, not from each route, and each route a tool may list
declares that its answer's shape is one this reads (COVERED_ROUTES; inner.request refuses any other):
- a dict that names a field (`field`, `name`, `field_name`): every value under its value keys, counts kept;
- the route's own field (a path parameter, or `field` in the query or body), for the answer as a whole;
- a key that is a private field's name or its alias (`detections_label` for `detections.label`, `@local_x` for
  `@local.x`), anywhere: its whole value (a sample's `metadata` and own fields, a flat row of a scatter);
- a saved filter stage's `description` (the chip's words, `serial = SN-0007`) when its filter tests a private field:
  written again with the field's values kept back.
The filter query's formatting, whose answer is text, writes a private field's values as "(n values kept from the
assistant)" for such a request (filter_query.format_filter with `kept`).
"""

import base64
import json
from dataclasses import dataclass
from typing import Any, Collection, Iterable, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV

from .. import database

PREFIX = 'pv1.'
# A value's JSON is sealed with a 4-byte length in front and zeros after, to a bucket of 32 bytes, or the next power of
# two above it: every value up to 28 bytes (every boolean, number, date and short code) seals to one length, and a
# longer one tells only its power of two.
_BUCKET = 32
_LENGTH = 4
# Keys whose values are values of the field the dict names; their subtrees are sealed, except counts.
VALUE_KEYS = frozenset(
    {
        'value',
        'values',
        'min',
        'max',
        'range',
        'buckets',
        'top',
        'examples',
        'unique_values',
        'key',
        'label',
        'bounds',
        'min_label',
        'max_label',
    },
)
# A private field's order statistics: the key itself says which value is the smallest or the largest, so a token in
# its place would still rank the samples. Dropped, never sealed.
ORDER_KEYS = frozenset({'min', 'max', 'range', 'bounds', 'min_label', 'max_label'})
# Counts, which say how many and never which: kept wherever they are.
COUNT_KEYS = frozenset(
    {
        'count',
        'total',
        'missing',
        'other_count',
        'distinct_total',
        'sample_count',
        'null_count',
        'bins',
        'present',
        'distinct',
        'samples_with_value',
    },
)
# Aggregations whose `value` is a count.
COUNT_OPS = frozenset({'count', 'distinct_count'})
NAME_KEYS = ('field', 'name', 'field_name')
# Every route a tool may list, with the shape of its answer that the walk below covers (inner.request refuses any
# other, and a test plants values for each and reads its answer). A new route is added here with that test.
COVERED_ROUTES: dict[tuple[str, str], str] = {
    ('GET', '/api/datasets'): 'datasets: names, descriptions, levels; no field values',
    ('GET', '/api/datasets/{id}'): "the dataset; its default view's stages (values by the field a filter names)",
    ('GET', '/api/datasets/{id}/schema'): 'one entry per field, by name: min, max, unique values, examples',
    ('GET', '/api/datasets/{id}/field-stats'): 'one entry per field, by field_name: values, top, ranges',
    ('GET', '/api/datasets/{id}/coverage'): 'one entry per field, by name: counts',
    ('GET', '/api/datasets/{id}/concepts'): 'concepts: names, ids, states; no field values',
    ('GET', '/api/datasets/{id}/views'): 'saved views: stages (values by the field a filter names, descriptions)',
    ('POST', '/api/datasets/{id}/aggregate'): 'one result per aggregation, by field: values, counts',
    ('POST', '/api/datasets/{id}/samples/query'): "samples: metadata and own fields by key",
    ('POST', '/api/datasets/{id}/histogram'): "the route's own field: buckets (refused for a private field)",
    ('POST', '/api/datasets/{id}/filter_query/parse'): "the request's own text as filters, by field",
    ('POST', '/api/datasets/{id}/filter_query/format'): 'text, private values written as kept',
    ('GET', '/api/datasets/{id}/concepts/{concept_id}/boundary'): 'sample ids and scores; no field values',
    ('GET', '/api/regions'): "regions: keys, kinds and names; of no dataset, no field values",
    ('POST', '/api/search'): "samples: metadata and own fields by key (the dataset its body's dataset_id names)",
    ('GET', '/api/views/{slug}'): (
        'a saved view: its stages (values by the field a filter names, descriptions), sealed for the dataset the '
        'route resolved'
    ),
}
# Covered routes that name no dataset in their path, and where the dataset whose private fields are sealed comes from
# (every other covered route names it as its `{id}`):
# - 'none': the route answers about no dataset and holds no field's values;
# - 'body': its body's `dataset_id` (request_dataset);
# - 'resolved': the dataset the route itself looked up and checked (check_dataset_access with the request's state),
#   read back after it answered (inner.request); a success that resolved none is refused.
DATASET_FROM: dict[tuple[str, str], str] = {
    ('GET', '/api/regions'): 'none',
    ('POST', '/api/search'): 'body',
    ('GET', '/api/views/{slug}'): 'resolved',
}
# Routes whose answer is built from the request alone. A private field's values in it are the request's own, and are
# dropped, never sealed: a token is made only from what a dataset holds, so a request can't have its own guess turned
# into a token that opens. An error answer is treated the same way, for it may echo the request.
ECHO_ROUTES = frozenset({('POST', '/api/datasets/{id}/filter_query/parse')})


@dataclass(frozen=True)
class Seal:
    """The server's key, and the dataset a request is about: what binds its tokens."""

    key: Optional[bytes]
    dataset_id: int

    def _data(self, field: str) -> list[bytes]:
        return [b'pv1', str(self.dataset_id).encode(), field.encode()]

    def seal(self, value: Any, field: str) -> Optional[str]:
        """`value` of `field` as a token. None without a key: the value is dropped, never shown."""
        if self.key is None:
            return None
        sealed = AESSIV(self.key).encrypt(_pad(json.dumps(value, sort_keys=True).encode()), self._data(field))
        return PREFIX + base64.urlsafe_b64encode(sealed).decode().rstrip('=')

    def open(self, token: Any, field: str) -> tuple[bool, Any]:
        """(True, the value) when `token` was sealed for `field` of this dataset with this key; (False, None) else."""
        if self.key is None or not is_sealed(token):
            return False, None
        raw = token[len(PREFIX) :]
        try:
            data = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
            return True, json.loads(_unpad(AESSIV(self.key).decrypt(data, self._data(field))))
        except (InvalidTag, ValueError):
            return False, None


def _pad(data: bytes) -> bytes:
    size, bucket = _LENGTH + len(data), _BUCKET
    while bucket < size:
        bucket *= 2
    return len(data).to_bytes(_LENGTH, 'big') + data + bytes(bucket - size)


def _unpad(data: bytes) -> bytes:
    size = int.from_bytes(data[:_LENGTH], 'big')
    if _LENGTH + size > len(data):
        raise ValueError('not a padded value')
    return data[_LENGTH : _LENGTH + size]


def is_sealed(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def covered(name: Any, private: Collection[str]) -> bool:
    """Whether field `name` holds values of a private field: the field itself, or a path inside one (a drawing's
    label list `detections.label`, a mirror's own `@local.` field named by its key)."""
    if not isinstance(name, str) or not private:
        return False
    parts = name.split('.')
    return any('.'.join(parts[:end]) in private for end in range(1, len(parts) + 1))


def _seal_all(node: Any, seal: Optional[Seal], name: str) -> Any:
    """Every value under `node` sealed for field `name`, counts kept, order statistics dropped, and every list put in
    an order that says nothing of the values: by count (most first) where its items have one, then by token. With no
    seal (an answer that may echo the request), the values are dropped instead."""
    if isinstance(node, dict):
        return {
            key: value if key in COUNT_KEYS else _seal_all(value, seal, name)
            for key, value in node.items()
            if key not in ORDER_KEYS
        }
    if isinstance(node, list):
        return sorted((_seal_all(value, seal, name) for value in node), key=_unranked)
    if node is None or seal is None:
        return None
    return seal.seal(node, name)


def _unranked(item: Any) -> tuple[float, str]:
    """The order of a sealed list: its items' counts, most first, then their tokens' text."""
    count = item.get('count') if isinstance(item, dict) else None
    rest = {key: value for key, value in item.items() if key not in COUNT_KEYS} if isinstance(item, dict) else item
    return (-count if isinstance(count, (int, float)) and not isinstance(count, bool) else 0, json.dumps(rest))


def _keyed(key: str, private: Collection[str]) -> Optional[str]:
    """The private field whose values a dict key holds: the key is its name, a path inside it, or its alias (`.`
    written `_`: `detections_label`, `@local_x`). Never a count's key or a key that names a field."""
    if key in COUNT_KEYS or key in NAME_KEYS:
        return None
    for name in (key, key.replace('_', '.')):
        if covered(name, private):
            return name
    return None


def _fields_of(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        if isinstance(node.get('field'), str):
            yield node['field']
        for value in node.values():
            yield from _fields_of(value)
    elif isinstance(node, list):
        for value in node:
            yield from _fields_of(value)


def _described(stage: dict[str, Any], private: Collection[str]) -> Optional[str]:
    """A saved stage's description, written again from its filter with private values kept back (None when the filter
    language can't write it)."""
    from ..filter_query import format_filter

    try:
        return format_filter(stage['filter'], kept=private)
    except Exception:  # noqa: BLE001 - a stage the language can't write has no description here
        return None


def _walk(node: Any, private: Collection[str], seal: Optional[Seal], about: Optional[str]) -> Any:
    if isinstance(node, list):
        return [_walk(value, private, seal, about) for value in node]
    if not isinstance(node, dict):
        return node
    named = next((node[key] for key in NAME_KEYS if isinstance(node.get(key), str)), None)
    here = about if about is not None else named if covered(named, private) else None
    out: dict[str, Any] = {}
    for key, value in node.items():
        keyed = _keyed(key, private)
        if key in ('metadata', 'local_metadata') and isinstance(value, dict):
            local = '@local.' if key == 'local_metadata' else ''
            out[key] = {
                name: _seal_all(item, seal, local + name)
                if covered(local + name, private)
                else _walk(item, private, seal, None)
                for name, item in value.items()
            }
        elif here is not None and key in ORDER_KEYS:
            continue
        elif here is not None and key in VALUE_KEYS and not (key == 'value' and node.get('op') in COUNT_OPS):
            out[key] = _seal_all(value, seal, here)
        elif keyed is not None:
            out[key] = _seal_all(value, seal, keyed)
        elif (
            key == 'description'
            and isinstance(node.get('filter'), dict)
            and any(covered(name, private) for name in _fields_of(node['filter']))
        ):
            out[key] = _described(node, private)
        else:
            out[key] = _walk(value, private, seal, None)
    return out


def seal_answer(
    answer: Any,
    private: Collection[str],
    seal: Seal,
    field: Optional[str] = None,
    *,
    echo: bool = False,
) -> Any:
    """`answer` with every value of a private field sealed. `field`: the field the route answers about as a whole
    (its path parameter, or `field` in its query or body). `echo`: the answer may hold the request's own values (an
    ECHO_ROUTES route's, or an error), which are dropped instead."""
    if not private:
        return answer
    return _walk(answer, private, None if echo else seal, field if covered(field, private) else None)


async def private_fields(dataset_id: int, source: Any = None) -> frozenset[str]:
    """The dataset's fields marked values_private (dataset_fields; a built-in field's row has origin 'system')."""
    rows = await (source if source is not None else database.db).fetch(
        'SELECT field_name FROM dataset_fields WHERE dataset_id = $1 AND values_private',
        dataset_id,
    )
    return frozenset(row['field_name'] for row in rows)


async def server_key(source: Any = None) -> Optional[bytes]:
    """The key tokens are sealed with (assistant_settings.seal_key, random, made by the server); None without one."""
    key = await (source if source is not None else database.db).fetchval(
        'SELECT seal_key FROM assistant_settings WHERE singleton',
    )
    return bytes(key) if key else None


def answer_field(path_params: dict[str, Any], query: Optional[dict[str, Any]], body: Any) -> Optional[str]:
    """The field a route answers about as a whole, from its request: a path parameter, else `field` in the query or
    the body."""
    for key in ('field_name', 'field'):
        if isinstance(path_params.get(key), str):
            return path_params[key]
    for source in (query or {}, body if isinstance(body, dict) else {}):
        if isinstance(source.get('field'), str):
            return source['field']
    return None


def request_dataset(path_params: dict[str, Any], body: Any) -> Optional[int]:
    """The dataset a request is about: its path's, else its body's `dataset_id`."""
    candidates = [path_params.get('id'), path_params.get('dataset_id')]
    if isinstance(body, dict):
        candidates.append(body.get('dataset_id'))
    for value in candidates:
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def open_filters(node: Any, seal: Seal) -> Any:
    """For the person (the people's endpoints, never a tool's request): `node`, a filter tree a working view keeps,
    with each token that is a test's value for its own field opened. One that doesn't open stays as it is."""
    if isinstance(node, list):
        return [open_filters(item, seal) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: open_filters(value, seal) if key != 'value' else value for key, value in node.items()}
    name, value = node.get('field'), node.get('value')
    if isinstance(name, str) and 'value' in node:

        def opened(item: Any) -> Any:
            ok, plain = seal.open(item, name)
            return plain if ok else item

        out['value'] = [opened(item) for item in value] if isinstance(value, list) else opened(value)
        if isinstance(value, dict):
            out['value'] = open_filters(value, seal)  # a cross-grain operator's sub-filter
    return out


def sealed_in(node: Any) -> Iterable[str]:
    """The tokens in `node`, for tests."""
    if is_sealed(node):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from sealed_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from sealed_in(value)
