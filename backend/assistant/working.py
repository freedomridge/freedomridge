"""Working views, value lists and their tools (docs/ASSISTANT.md, "Working views"): a conversation narrows a dataset
step by step, each step kept on the server as a working view named by a handle (`wv_...`), and a field's values kept
as a value list (`vl_...`). Every use of a handle reads the dataset through the API as the user at that moment.
"""

import datetime
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import links, private, rule, store
from .api import TOO_SLOW, Api, ToolError, cut
from .catalog import _NO_VALUES, DATASET, SCHEMA, Tool, _kind, indexed, list_column
from .catalog import _private_names as catalog_private_names
from .grain import image_grain

PARSE = '/api/datasets/{id}/filter_query/parse'
FORMAT = '/api/datasets/{id}/filter_query/format'
QUERY = '/api/datasets/{id}/samples/query'
AGGREGATE = '/api/datasets/{id}/aggregate'

_VIEW_OR_DATASET = {
    'view': {'type': 'string', 'maxLength': 100, 'description': 'A working view\'s handle (wv_...), from make_view.'},
    'dataset': {
        'type': 'integer',
        'minimum': 1,
        'maximum': 2_147_483_647,
        'description': "A dataset's id, meaning the whole dataset. Give either view or dataset.",
    },
}
_LIST_STAGE = {
    'type': 'object',
    'properties': {
        'field': {'type': 'string', 'maxLength': 255, 'description': 'The field whose value must be in the list.'},
        'list': {
            'type': 'string',
            'maxLength': 100,
            'description': "A value list's handle (vl_...), from field_values.",
        },
    },
    'required': ['field', 'list'],
    'additionalProperties': False,
}
_STAGE_NUMBERS = {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}, 'maxItems': 100}


# --- shared ---

_KEPT_NOTE = (
    'This link opens the view for you only, for 30 days after its last use. Save it as a view in the app to make a '
    'permanent link.'
)
_TEXT_LIMIT = 1000  # longest stage text given in full; a longer one (a long value list) is summarised
# Longest text of a stage the model didn't write given in full (200 ids found by a search fit); a longer one (a saved
# selection of thousands of ids) is shown by its label.
_FILTER_TEXT_LIMIT = 5000
_FORMAT_LIST_LIMIT = 500  # a list stage with more values isn't written as text at all: no link could hold it
_COUNTED_KINDS = ('text', 'list', 'number', 'boolean')


@dataclass
class Target:
    dataset_id: int
    dataset: dict[str, Any]
    view: Optional[dict[str, Any]]  # the working view, or None for the whole dataset
    stages: list[dict[str, Any]]


def _user(api: Api) -> int:
    return api.identity.user_id


async def _dataset(api: Api, dataset_id: int) -> dict[str, Any]:
    dataset = await api.get(DATASET, id=dataset_id, missing=f'Dataset not found: id {dataset_id}')
    if dataset.get('deleting'):
        raise ToolError('not_found', f"Dataset '{dataset['name']}' (id {dataset_id}) is being deleted")
    return dataset


async def _target(api: Api, arguments: dict[str, Any]) -> Target:
    """The view or the whole dataset a tool reads: exactly one of `view` and `dataset`, read as the user now."""
    handle, dataset_id = arguments.get('view'), arguments.get('dataset')
    if handle is None and dataset_id is None:
        raise ToolError('invalid', "Give view (a working view's handle) or dataset (a dataset's id)")
    if handle is not None and dataset_id is not None:
        raise ToolError('invalid', 'Give view or dataset, not both: a view already names its dataset')
    view = None
    if handle is not None:
        view = await store.load_view(_user(api), handle)
        if view is None:
            raise ToolError('not_found', store.VIEW_GONE.format(handle=handle))
        dataset_id = view['dataset_id']
    assert isinstance(dataset_id, int)
    dataset = await _dataset(api, dataset_id)
    return Target(dataset_id, dataset, view, list(view['stages']) if view else [])


async def _nodes(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each stage as the filter node the API compiles. A stage is one of:
    - text the model wrote: `{"kind": "text", "filter", "text", "negated"}`, `text` as the server read it;
    - a filter node the model didn't write (from a saved view, a similarity search): `{"kind": "filter", "filter",
      "label", "negated"}`, written by the server when it has a text form, its private values kept back;
    - a value list's: `{"kind": "list", "list", "field", "exclude"}`, holding the list's values (sealed tokens for a
      private field, which only the API's compiler opens)."""
    lists = await store.list_values(stage['list'] for stage in stages if stage['kind'] == 'list')
    nodes = []
    for stage in stages:
        if stage['kind'] == 'list':
            entries = (lists.get(stage['list']) or {}).get('entries') or []  # a list that's gone matches nothing
            operator = 'not in' if stage.get('exclude') else 'in'
            nodes.append({'field': stage['field'], 'operator': operator, 'value': [value for value, _ in entries]})
        else:
            nodes.append(_negated(stage['filter']) if stage.get('negated') else stage['filter'])
    return nodes


async def _images(api: Api, dataset_id: int, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """The filter for a view's images: its stages at the app's image grain (grain.py), on a dataset with object rows
    or without, which is asked once per tool call (one row is enough) and shared by its requests."""
    key = f'objects:{dataset_id}'
    if key not in api.facts:
        found = await api.post(
            QUERY,
            {
                'filters': {'field': 'media_type', 'operator': '=', 'value': 'object'},
                'limit': 1,
                'skip_count': True,
                'fields': 'id',
                'slim': True,
            },
            counting=True,
            id=dataset_id,
        )
        api.facts[key] = bool(found['items'])
    return image_grain(nodes, api.facts[key])


def _and(tree: dict[str, Any], *nodes: dict[str, Any]) -> dict[str, Any]:
    """`tree` and conditions on each image itself (it has the field, its value contains the search)."""
    return {'logic': 'AND', 'filters': [tree, *nodes]}


async def _count(api: Api, dataset_id: int, filters: Optional[dict[str, Any]]) -> int:
    """The grid's own count for the filters (one count)."""
    answer = await api.post(QUERY, {'filters': filters, 'limit': 0}, counting=True, id=dataset_id)
    return int(answer['total'])


def _negated(node: dict[str, Any]) -> dict[str, Any]:
    """The stage turned around; twice gives the original."""
    if node.get('logic') == 'NOT' and len(node.get('filters') or []) == 1:
        return node['filters'][0]
    for one, other in (('concept_select', 'concept_exclude'), ('geo_region', 'geo_region_exclude')):
        if node.get('operator') in (one, other):
            return {**node, 'operator': other if node['operator'] == one else one}
    return {'logic': 'NOT', 'filters': [node]}


def _sealed(node: Any) -> bool:
    return any(True for _ in private.sealed_in(node))


def _fields_of(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        if isinstance(node.get('field'), str):
            yield node['field']
        for value in node.values():
            yield from _fields_of(value)
    elif isinstance(node, list):
        for value in node:
            yield from _fields_of(value)


def _private_names(schema: list[dict[str, Any]]) -> set[str]:
    return catalog_private_names(schema)


async def _parse(api: Api, dataset_id: int, text: str) -> list[dict[str, Any]]:
    """Filter text as stages, one per top-level AND part, each with the server's writing of it."""
    parsed = await api.post(PARSE, {'query': text}, id=dataset_id)
    return [{'kind': 'text', 'filter': node, 'text': part} for node, part in zip(parsed['filters'], parsed['parts'])]


async def _texts(
    api: Api,
    dataset_id: int,
    stages: list[dict[str, Any]],
    formless: Optional[set[int]] = None,
) -> tuple[list[str], Optional[list[str]]]:
    """Each stage's text for the answer, and the stages' texts for a `?q=` link: None when a stage can't be in one
    (no text form, a list too long to write, or a stage the model didn't write on a private field, whose text keeps its
    values back). Text the model wrote is given as the server read it; every other stage is written by the server, or
    shown by its label (the server's own words: no text form, or a text too long to show). `formless`, when given,
    gets the indexes of the stages shown by their label."""
    nodes = await _nodes(stages)
    hidden: set[str] = set()
    if any(stage['kind'] != 'text' for stage in stages):
        hidden = _private_names(await api.get(SCHEMA, id=dataset_id))
    texts: list[str] = [''] * len(stages)
    to_format: list[tuple[int, dict[str, Any]]] = []
    linkable = True
    for index, (stage, node) in enumerate(zip(stages, nodes)):
        if stage['kind'] == 'text':
            texts[index] = f'NOT ({stage["text"]})' if stage.get('negated') else stage['text']
            continue
        kept = any(private.covered(name, hidden) for name in _fields_of(node)) or _sealed(node)
        linkable = linkable and not kept
        if stage['kind'] == 'list' and (kept or len(node['value']) > _FORMAT_LIST_LIMIT):
            texts[index] = _list_summary(stage, len(node['value']), kept=kept)
            linkable = False
            continue
        to_format.append((index, node))
    if to_format:
        answer = await api.post(FORMAT, {'filters': [node for _, node in to_format]}, id=dataset_id)
        for (index, _), part in zip(to_format, answer['parts']):
            if part is None:
                linkable = False
                part = stages[index].get('label') or '(a stage with no text form)'
                if formless is not None:
                    formless.add(index)
            texts[index] = part
    shown = []
    for index, (stage, node, text) in enumerate(zip(stages, nodes, texts)):
        if stage['kind'] == 'list' and len(text) > _TEXT_LIMIT:
            text = _list_summary(stage, len(node['value']), kept=False)
        elif stage['kind'] == 'filter' and len(text) > _FILTER_TEXT_LIMIT and stage.get('label'):
            text = stage['label']  # a long list of ids (a saved selection): the link still holds its text
            if formless is not None:
                formless.add(index)
        shown.append(text)
    return shown, texts if linkable else None


def _list_summary(stage: dict[str, Any], count: int, *, kept: bool) -> str:
    operator = 'not in' if stage.get('exclude') else 'in'
    what = f'{count:,} value{"" if count == 1 else "s"}'
    source = 'kept from the assistant' if kept else f'of the value list {stage["list"]}'
    return f'{stage["field"]} {operator} ({what} {source})'


# One view's bounds (m1-contract-working-views, section 3, item 7): no chain of refinements builds a filter the server
# then expands on every count. A list stage counts its list's values, once per stage that uses it.
MAX_STAGES = 50
MAX_LIST_VALUES = 50_000


async def _check_size(stages: list[dict[str, Any]]) -> None:
    if len(stages) > MAX_STAGES:
        raise ToolError(
            'invalid',
            f'A view holds at most {MAX_STAGES} stages, and this one would hold {len(stages)}: remove stages '
            '(refine_view with remove), or make a new view from a narrower filter',
        )
    lists = await store.list_values(stage['list'] for stage in stages if stage['kind'] == 'list')
    total = sum(
        (lists.get(stage['list']) or {}).get('distinct_count') or 0 for stage in stages if stage['kind'] == 'list'
    )
    if total > MAX_LIST_VALUES:
        raise ToolError(
            'invalid',
            f"A view's value lists hold at most {MAX_LIST_VALUES:,} values in all, and this one's would hold "
            f'{total:,}: narrow the view or search the field before taking its list, or remove a list stage',
        )


async def _view_answer(
    api: Api,
    dataset_id: int,
    stages: list[dict[str, Any]],
    *,
    from_handle: Optional[str] = None,
    grows: bool = True,
    permanent: Optional[str] = None,
    labelled: bool = False,
) -> dict[str, Any]:
    """Count the stages, keep them as a new view, and answer it. A view that grows (every one but a refinement that
    only removes or negates stages) is checked against the view's bounds first, and none is made past them.
    `permanent`: the view's own permanent link (a saved view's), given in place of one made from its stages.
    `labelled`: a stage shown by its label comes as `"text": null` and its `label` (open_saved_view)."""
    if grows:
        await _check_size(stages)
    try:
        count = await _count(api, dataset_id, await _images(api, dataset_id, await _nodes(stages)))
    except ToolError as error:
        if error.code != 'too_slow':
            raise
        raise await _too_slow_filter(api, dataset_id, stages, None, 'Counting this view') from None
    formless: set[int] = set()
    shown, full = await _texts(api, dataset_id, stages, formless)
    handle = await store.save_view(_user(api), dataset_id, stages, from_handle)
    link = permanent or (links.text_link(dataset_id, ' AND '.join(full)) if full is not None else None)
    answer: dict[str, Any] = {
        'view': handle,
        'dataset': dataset_id,
        'count': count,
        'filter': ' AND '.join(shown),
        'stages': [
            {'n': index + 1, 'text': None, 'label': text}
            if labelled and index in formless
            else {'n': index + 1, 'text': text}
            for index, text in enumerate(shown)
        ],
        'link': link or links.view_link(dataset_id, handle),
    }
    if link is None:
        answer['link_note'] = _KEPT_NOTE
    if from_handle is not None:
        answer['from'] = from_handle
    if api.unreadable:
        answer['note'] = (
            f'{api.unreadable} value{"" if api.unreadable == 1 else "s"} of these stages could not be read back '
            '(kept for another field or dataset, or before the server changed its key): they match nothing.'
        )
    _within_size(answer)
    return answer


# The most a view answer takes as JSON: past it, long stage texts are shown cut (the view keeps them whole, and a
# `?q=` link still has them). Filter text is at most FILTER_TEXT_LIMIT characters, but quotes and non-ASCII letters
# grow when written as JSON, and a view can hold 50 stages.
_ANSWER_BYTES = 19_000
FILTER_TEXT_LIMIT = 20_000
_FILTER_TEXT = {
    'type': 'string',
    'maxLength': FILTER_TEXT_LIMIT,
    'x-too-long': (
        'a long list of values goes in a value list instead (field_values keeps one; refine_view in_list filters by it)'
    ),
}


def _within_size(answer: dict[str, Any]) -> None:
    if len(json.dumps(answer)) <= _ANSWER_BYTES:
        return
    whole = [stage.get('text') for stage in answer['stages']]
    limit = 2000
    while limit >= 25:
        for stage, text in zip(answer['stages'], whole):
            if text is not None:
                stage['text'] = cut(text, limit)
        answer['filter'] = ' AND '.join(
            stage['text'] if stage['text'] is not None else stage['label'] for stage in answer['stages']
        )
        if len(json.dumps(answer)) <= _ANSWER_BYTES:
            break
        limit //= 2
    said = 'Long stage texts are shown cut here; the view keeps them whole.'
    answer['note'] = f"{answer['note']} {said}" if answer.get('note') else said


# The most a page of entries takes as JSON, so that an answer stays well under 20 KB with its other keys: a page of
# long entries holds fewer than its `limit`, and next_offset says where the rest start.
_PAGE_BYTES = 15_000


def _fit(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first entries that fit in _PAGE_BYTES of JSON (always at least one)."""
    kept: list[dict[str, Any]] = []
    size = 0
    for entry in entries:
        size += len(json.dumps(entry)) + 2
        if kept and size > _PAGE_BYTES:
            break
        kept.append(entry)
    return kept


# --- make_view ---


async def make_view(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    dataset_id = arguments['dataset']
    await _dataset(api, dataset_id)
    text = (arguments.get('filter') or '').strip()
    stages = await _parse(api, dataset_id, text) if text else []
    return await _view_answer(api, dataset_id, stages)


# --- refine_view ---


async def refine_view(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    target = await _target(api, {'view': arguments['view']})
    assert target.view is not None
    stages, count = target.stages, len(target.stages)
    remove, negate = set(arguments.get('remove') or []), set(arguments.get('negate') or [])
    add = (arguments.get('add') or '').strip()
    listed = [(key, arguments[key]) for key in ('in_list', 'not_in_list') if arguments.get(key)]
    if not (remove or negate or add or listed):
        raise ToolError('invalid', 'Say what to change: add, remove, negate, in_list or not_in_list')
    for number in sorted(remove | negate):
        if number > count:
            raise ToolError('invalid', f'The view has no stage {number}: its stages are 1 to {count}')
    if remove & negate:
        raise ToolError('invalid', f'Stage {min(remove & negate)} is both removed and negated')
    new: list[dict[str, Any]] = []
    for number, stage in enumerate(stages, start=1):
        if number in remove:
            continue
        if number in negate:
            flag = 'exclude' if stage['kind'] == 'list' else 'negated'
            stage = {**stage, flag: not stage.get(flag)}
        new.append(stage)
    if add:
        new += await _parse(api, target.dataset_id, add)
    if listed:
        schema = await api.get(SCHEMA, id=target.dataset_id)
        names = {field['name'] for field in schema}
        for key, spec in listed:
            kept = await store.load_list(_user(api), spec['list'])
            if kept is None:
                raise ToolError('not_found', store.LIST_GONE.format(handle=spec['list']))
            if kept['dataset_id'] != target.dataset_id:
                raise ToolError('invalid', f"The value list {spec['list']} was made for another dataset")
            if kept['entries'] is None:
                raise ToolError(
                    'invalid',
                    f"The value list {spec['list']} has {kept['distinct_count']:,} values, more than the "
                    f'{store.LIST_LIMIT:,} kept for filtering: narrow the view or search the field first',
                )
            if spec['field'] not in names:
                raise ToolError('not_found', f"Field '{spec['field']}' is not a field of this dataset")
            if spec['field'] != kept['field_name'] and _sealed(kept['entries']):
                raise ToolError(
                    'invalid',
                    f"The value list {spec['list']} holds values of '{kept['field_name']}', which are kept from the "
                    f"assistant: it filters that field only",
                )
            new.append({'kind': 'list', 'list': spec['list'], 'field': spec['field'], 'exclude': key == 'not_in_list'})
    return await _view_answer(
        api,
        target.dataset_id,
        new,
        from_handle=arguments['view'],
        grows=bool(add or listed),
    )


# --- field_values and field_summary ---


@dataclass
class Field:
    name: str
    kind: str
    entry: dict[str, Any]
    private: bool


async def _fields(api: Api, dataset_id: int) -> list[dict[str, Any]]:
    """The dataset's fields, without the lists of values the schema would read from the samples: no read of them."""
    fields: list[dict[str, Any]] = await api.get(SCHEMA, id=dataset_id, query={'values': 'false'})
    return fields


async def _field(api: Api, dataset_id: int, name: str) -> Field:
    schema = await _fields(api, dataset_id)
    entry = next((field for field in schema if field['name'] == name), None)
    if entry is None:
        raise ToolError('not_found', f"Field '{name}' is not a field of this dataset")
    return Field(name, _kind(entry), entry, private.covered(name, _private_names(schema)))


def _presence(name: str) -> str:
    """The field whose samples have the field: a drawing's label list counts the samples with the drawing."""
    return name.rsplit('.', 1)[0] if name.endswith('.label') or name == 'segmentation.labels' else name


def _about(target: Target) -> str:
    return 'this view' if target.view else 'this dataset'


async def _counted_view(api: Api, target: Target, tool: str) -> tuple[dict[str, Any], int]:
    """The view's filter at the image grain, and its exact count, made before field_values' and field_summary's reads
    (a samples query that counts, not a read of the field), so a read that runs out of time can say how many samples
    it had to read."""
    try:
        base = await _images(api, target.dataset_id, await _nodes(target.stages))
        return base, await _count(api, target.dataset_id, base)
    except ToolError as error:
        if error.code != 'too_slow':
            raise
        raise _too_slow_reads(target, None, tool) from None


def _too_slow_reads(target: Target, total: Optional[int], tool: str) -> ToolError:
    """too_slow from field_values or field_summary: the tool reads every sample of the view whatever the field, so the
    message gives their number (none when counting them is what ran out) and says to narrow the view, never to index
    the field."""
    what = f'every one of the {total:,} samples of' if total is not None else 'every sample of'
    ran_out = 'that' if total is not None else 'even counting them'
    return ToolError(
        'too_slow',
        f'{tool} reads {what} {_about(target)}, whether or not the field is indexed, and {ran_out} took longer than '
        'the server allows for one statement, so there is no answer, not even a partial one. Narrow the view first '
        '(make_view or refine_view), then ask again.',
    )


def _tests_of(node: Any) -> Iterable[tuple[str, str]]:
    """Each test of a filter tree, as (its field, its operator)."""
    if isinstance(node, dict):
        if isinstance(node.get('field'), str):
            yield node['field'], str(node.get('operator', '=')).lower()
        for value in node.values():
            yield from _tests_of(value)
    elif isinstance(node, list):
        for value in node:
            yield from _tests_of(value)


def _quoted(names: list[str]) -> str:
    return ', '.join(f"'{name}'" for name in names)


# The tests an index on the field serves (and a sort): only for a field tested so is an index worth asking for.
# contains (ILIKE '%...%'), exists and not_exists read every sample whether or not the field has one.
_INDEX_SERVES = frozenset({'=', '==', 'in', '<', '<=', '>', '>='})


async def _too_slow_filter(
    api: Api,
    dataset_id: int,
    stages: list[dict[str, Any]],
    sort_by: Optional[str],
    doing: str,
) -> ToolError:
    """too_slow from a count over a view's filter, or a page of it sorted or filtered: names what it uses that no index
    serves, for which every sample of the dataset was read: a field that is not indexed, and a list field (tags, a
    registered array field: its index serves `in`) tested otherwise or sorted by. It advises asking for an index only
    for a field tested in a way one would serve (=, in, a range) or sorted by. When it uses none, it says to narrow the
    view without advising an index. A private field's name may be given; never a value."""
    tests = [test for node in await _nodes(stages) for test in _tests_of(node)]
    try:
        schema = {field['name']: field for field in await _fields(api, dataset_id)}
    except ToolError as error:
        if error.code != 'too_slow':
            raise
        return ToolError('too_slow', TOO_SLOW)
    sorted_by = schema.get(sort_by) if sort_by else None
    plain_filter = [name for name, _ in tests if name in schema and not indexed(schema[name])]
    plain_sort = [sort_by] if sort_by and sorted_by is not None and not indexed(sorted_by) else []
    plain = list(dict.fromkeys(plain_filter + plain_sort))
    lists = [name for name, operator in tests if name in schema and list_column(schema[name]) and operator != 'in'] + (
        [sort_by] if sort_by and sorted_by is not None and list_column(sorted_by) else []
    )
    lists = [name for name in dict.fromkeys(lists) if name not in plain]
    ran_out = (
        f'{doing} took longer than the server allows for one statement, so there is no answer, not even a partial one.'
    )
    if not plain and not lists:
        return ToolError(
            'too_slow',
            f'{ran_out} Narrow the view: a filter on an indexed field with =, in or a narrow range (on a list field, '
            'in) is quick, but a contains or a range that keeps a large share of the samples reads every sample all '
            'the same.',
        )
    said = []
    if plain:
        if plain_filter and plain_sort:
            subject = 'Its filter and its sort use'
        else:
            subject = 'Its sort uses' if plain_sort else 'Its filter uses'
        said.append(f'{subject} {_quoted(plain)}, which {"is" if len(plain) == 1 else "are"} not indexed')
    if lists:
        whose = 'its' if len(lists) == 1 else 'their'
        said.append(
            f'{"it also" if plain else "It"} uses the list field{"" if len(lists) == 1 else "s"} {_quoted(lists)} '
            f'otherwise than with in (with =, say, or to sort), which {whose} index doesn\'t serve',
        )
    advice = 'Narrow the view by an indexed field first (describe_dataset marks them indexed: true)'
    servable = {name for name, operator in tests if operator in _INDEX_SERVES} | set(plain_sort)
    to_index = [name for name in plain if name in servable]
    if to_index:
        advice += f', or ask an administrator to index {_quoted(to_index)}'
    if lists:
        advice += '; test a list field with in'
    read_for = 'it' if len(plain) + len(lists) == 1 else 'them'
    return ToolError(
        'too_slow',
        f'{ran_out} {", and ".join(said)}: every sample of the dataset was read for {read_for}. {advice}.',
    )


def _typed(value: Any, kind: str) -> Any:
    """A value as the aggregate route writes it (text), as its field holds it."""
    if not isinstance(value, str) or private.is_sealed(value):
        return value
    if kind == 'number':
        try:
            number = float(value)
        except ValueError:
            return value
        return int(number) if number.is_integer() and 'e' not in value.lower() and '.' not in value else number
    if kind == 'boolean' and value in ('true', 'false'):
        return value == 'true'
    return value


def _check_counted(field: Field, tool: str) -> None:
    if field.entry.get('type') in _NO_VALUES - {'filepath'}:
        raise ToolError(
            'invalid',
            f"'{field.name}' is a drawing ({field.entry.get('type')}): ask about its labels ('{field.name}.label')",
        )
    if field.kind == 'datetime' and tool == 'field_values':
        raise ToolError(
            'invalid',
            f"'{field.name}' is a datetime field: field_summary gives its earliest, latest and spread",
        )
    if field.kind not in (*_COUNTED_KINDS, 'datetime'):
        raise ToolError(
            'invalid',
            f"'{field.name}' holds objects, which have no values to count: ask about one of its parts",
        )


async def field_values(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    target = await _target(api, arguments)
    field = await _field(api, target.dataset_id, arguments['field'])
    _check_counted(field, 'field_values')
    search, limit, offset = arguments.get('search'), arguments.get('limit', 20), arguments.get('offset', 0)
    if search and field.private:
        raise ToolError(
            'refused',
            f"The values of '{field.name}' are kept from the assistant: they can't be searched. Its value list (by "
            'its handle) filters the field, and exists or not_exists test it',
        )
    base, total = await _counted_view(api, target, 'field_values')
    # Text a value must contain: the server matches it on a text field (any letter case); on a list, a number or a
    # boolean, among the values counted.
    searched = bool(search) and field.kind == 'text'
    counted = _and(base, {'field': field.name, 'operator': 'contains', 'value': search}) if searched else base
    with_value_op = {'field': _presence(field.name), 'op': 'with_value'}
    aggregations = [
        # The whole list when it can be kept, and at least as far as the page asked for.
        {'field': field.name, 'op': 'count_values', 'limit': max(store.LIST_LIMIT + 1, offset + limit)},
        {'field': field.name, 'op': 'distinct_count'},
    ]
    try:
        # One read of the view's samples for a field with one value a sample. samples_with_value is over the view,
        # whatever the search, so a searched text field counts it in a read of its own.
        answer = await api.post(
            AGGREGATE,
            {'aggregations': aggregations + ([] if searched else [with_value_op]), 'filters': counted},
            counting=True,
            id=target.dataset_id,
        )
        if searched:
            present = await api.post(
                AGGREGATE,
                {'aggregations': [with_value_op], 'filters': base},
                counting=True,
                id=target.dataset_id,
            )
            with_value = int(present['results'][0]['value'])
        else:
            with_value = int(answer['results'][2]['value'])
    except ToolError as error:
        if error.code != 'too_slow':
            raise
        raise _too_slow_reads(target, total, 'field_values') from None
    entries = [[_typed(item['value'], field.kind), item['count']] for item in answer['results'][0]['value']]
    distinct = int(answer['results'][1]['value'])
    if search and field.kind != 'text':
        if len(entries) > store.LIST_LIMIT:
            raise ToolError(
                'invalid',
                f"'{field.name}' has more than {store.LIST_LIMIT:,} values here: narrow the view before searching it",
            )
        needle = search.casefold()
        entries = [entry for entry in entries if needle in str(entry[0]).casefold()]
        distinct = len(entries)
    whole = distinct <= store.LIST_LIMIT
    value_kind = field.kind if field.kind in ('number', 'boolean') else 'text'
    handle = await store.save_list(
        _user(api),
        target.dataset_id,
        field.name,
        value_kind,
        distinct,
        entries if whole else None,
        None if whole else {'filters': counted},
        search,
    )
    result: dict[str, Any] = {
        'field': field.name,
        'view': target.view['handle'] if target.view else None,
        'dataset': target.dataset_id,
        'samples_with_value': with_value,
        'distinct': distinct,
        'values': [] if field.private else [{'value': v, 'count': c} for v, c in entries[offset : offset + limit]],
        'offset': offset,
        'next_offset': None if field.private else _next(offset, limit, distinct),
        'list': handle if whole else None,
        'download': links.list_download(handle),
        'complete': True,
    }
    if not whole:
        result['list_note'] = (
            f'This field has {distinct:,} different values here, more than the {store.LIST_LIMIT:,} a value list keeps '
            'for filtering: narrow the view, or search the field, first. The download has every value.'
        )
    return result


def _next(offset: int, limit: int, total: int) -> Optional[int]:
    return offset + limit if offset + limit < total else None


QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def _instant(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


async def _aggregate(api: Api, target: Target, base: dict[str, Any], aggregations: list[dict[str, Any]]) -> dict:
    """The aggregations over the view, by operation (field_summary asks each operation once a request)."""
    answer = await api.post(
        AGGREGATE,
        {'aggregations': aggregations, 'filters': base},
        counting=True,
        id=target.dataset_id,
    )
    return {item['op']: item for item in answer['results']}


async def field_summary(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    """The view's count, then one read of its samples, two for a number or a datetime: first the samples with a value,
    the smallest, the largest and the mean, then the quantiles, the histogram and the distinct count. When only the
    second runs out of time, the first's numbers are the answer, each exact, and a note says what is missing and why."""
    target = await _target(api, arguments)
    field = await _field(api, target.dataset_id, arguments['field'])
    _check_counted(field, 'field_summary')
    base, total = await _counted_view(api, target, 'field_summary')
    numeric = field.kind in ('number', 'datetime') and not field.private
    first: list[dict[str, Any]] = [{'field': _presence(field.name), 'op': 'with_value'}]
    if not numeric:
        first.append({'field': field.name, 'op': 'distinct_count'})
        if not field.private:
            first.append({'field': field.name, 'op': 'count_values', 'limit': 5})
    else:
        first.append({'field': field.name, 'op': 'bounds'})
        if field.kind == 'number':
            first.append({'field': field.name, 'op': 'mean'})
    try:
        results = await _aggregate(api, target, base, first)
    except ToolError as error:
        if error.code != 'too_slow':
            raise
        raise _too_slow_reads(target, total, 'field_summary') from None
    with_value = int(results['with_value']['value'])
    result: dict[str, Any] = {
        'field': field.name,
        'type': field.kind,
        'unit': field.entry.get('unit'),
        'samples_with_value': with_value,
        'samples_without': max(0, total - with_value),
        'distinct': int(results['distinct_count']['value']) if 'distinct_count' in results else None,
    }
    if field.private:
        return result
    if not numeric:
        result['top'] = [
            {'value': _typed(item['value'], field.kind), 'count': item['count']}
            for item in results['count_values']['value']
        ]
        return result
    show = _instant if field.kind == 'datetime' else (lambda value: value)
    low, high = results['bounds']['value']
    result['min'], result['max'] = show(low), show(high)
    if field.kind == 'number':
        result['mean'] = results['mean']['value']
    second: list[dict[str, Any]] = [
        {'field': field.name, 'op': 'quantiles', 'quantiles': list(QUANTILES)},
        {'field': field.name, 'op': 'histogram', 'bins': 20},
        {'field': field.name, 'op': 'distinct_count'},
    ]
    try:
        results = await _aggregate(api, target, base, second)
    except ToolError as error:
        if error.code != 'too_slow':
            raise
        result.update(quantiles=None, histogram=None)
        result['note'] = (
            f'The quantiles, the histogram and the distinct count ran out of time on the {total:,} samples of '
            f'{_about(target)}: narrow the view to get them. The other numbers are exact.'
        )
        return result
    result['distinct'] = int(results['distinct_count']['value'])
    marks = results['quantiles']['value']
    result['quantiles'] = {f'p{round(q * 100)}': show(value) for q, value in zip(QUANTILES, marks)} if marks else None
    result['histogram'] = [
        {'from': show(bucket['range']['min']), 'to': show(bucket['range']['max']), 'count': bucket['count']}
        for bucket in results['histogram']['value']
        if isinstance(bucket.get('range'), dict)
    ]
    return result


# --- get_samples ---

_NOT_ALLOWED_FIELDS = (
    "An administrator has not allowed the assistant to read samples' fields (Settings → Assistant): it can give "
    'their ids and links only.'
)


async def get_samples(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    target = await _target(api, arguments)
    fields = list(dict.fromkeys(arguments.get('fields') or []))
    limit, offset = arguments.get('limit', 10), arguments.get('offset', 0)
    sort_by, descending = arguments.get('sort_by'), arguments.get('descending', False)
    if fields and not (await rule.read_settings())['sample_fields']:
        raise ToolError('refused', _NOT_ALLOWED_FIELDS)
    # Sorting by a private field is refused by the API itself (assistant/guard.py), in its own words.
    private_names = _private_names(await api.get(SCHEMA, id=target.dataset_id)) if fields else set()
    try:
        body: dict[str, Any] = {
            'filters': await _images(api, target.dataset_id, await _nodes(target.stages)),
            'limit': limit,
            'offset': offset,
            'sort_by': sort_by,
            'sort_order': 'desc' if descending else 'asc',
        }
        if not fields:
            body.update(fields='id', slim=True)
        page = await api.post(QUERY, body, counting=True, id=target.dataset_id)
    except ToolError as error:
        if error.code != 'too_slow' or not (target.stages or sort_by):
            raise
        raise await _too_slow_filter(api, target.dataset_id, target.stages, sort_by, 'Reading this page') from None
    shown = [name for name in fields if not private.covered(name, private_names)]
    samples = []
    for item in page['items']:
        sample: dict[str, Any] = {'id': item['id'], 'link': links.sample_link(target.dataset_id, item['id'])}
        for name in shown:
            value = item.get(name) if name in item and name != 'metadata' else (item.get('metadata') or {}).get(name)
            sample[name] = None if private.is_sealed(value) else value
        samples.append(sample)
    total = int(page['total'])
    samples = _fit(samples)  # long fields: fewer to a page, and next_offset where the rest start
    result: dict[str, Any] = {
        'samples': samples,
        'total': total,
        'offset': offset,
        'next_offset': offset + len(samples) if samples and offset + len(samples) < total else None,
    }
    if len(shown) < len(fields):
        result['withheld'] = [name for name in fields if name not in shown]
    return result


def _counting(run: Any) -> Any:
    """`run` as a tool whose every read counts through the samples, so running into the statement time limit is
    too_slow (api.Api.counting)."""

    async def counted(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
        api.counting = True
        result: dict[str, Any] = await run(api, arguments)
        return result

    return counted


MAKE_VIEW = Tool(
    name='make_view',
    description=(
        'Make a working view of a dataset from filter text in the filter language, such as '
        '`state = "Kansas" AND brightness < 40`, `location within "US-KS"` or `not concept "equipment" > 0.62`. '
        'Each top-level AND part is a stage. Answers the view\'s handle (wv_...), its exact count, the filter as the '
        'server writes it, its stages and a link that opens it in the app. Pass the handle to the other tools.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'dataset': {
                'type': 'integer',
                'minimum': 1,
                'maximum': 2_147_483_647,
                'description': "The dataset's id (from list_datasets).",
            },
            'filter': {
                **_FILTER_TEXT,
                'description': (
                    'Filter text in the filter language, at most 20,000 characters; empty or left out means the whole '
                    'dataset.'
                ),
            },
        },
        'required': ['dataset'],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(('GET', DATASET), ('GET', SCHEMA), ('POST', PARSE), ('POST', FORMAT), ('POST', QUERY)),
    run=_counting(make_view),
)

REFINE_VIEW = Tool(
    name='refine_view',
    description=(
        'Make a new working view from one: add filter text as new stages, remove or negate stages by their number '
        '(n), or keep (in_list) or remove (not_in_list) the samples whose field has a value in a value list '
        '(vl_..., from field_values). The view refined stays as it was; the answer is the new view, with `from`.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'view': _VIEW_OR_DATASET['view'],
            'add': {**_FILTER_TEXT, 'description': 'Filter text to add as new stages, at most 20,000 characters.'},
            'remove': {**_STAGE_NUMBERS, 'description': 'Stage numbers (n) to drop.'},
            'negate': {**_STAGE_NUMBERS, 'description': 'Stage numbers (n) to turn around.'},
            'in_list': {**_LIST_STAGE, 'description': "Keep the samples whose field has one of the list's values."},
            'not_in_list': {**_LIST_STAGE, 'description': "Remove the samples whose field has one of its values."},
        },
        'required': ['view'],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(('GET', DATASET), ('GET', SCHEMA), ('POST', PARSE), ('POST', FORMAT), ('POST', QUERY)),
    run=_counting(refine_view),
)

FIELD_VALUES = Tool(
    name='field_values',
    description=(
        'The distinct values of one field among a view\'s samples (or a whole dataset\'s), with how many samples '
        'have each, commonest first, and how many distinct values there are. Keeps the whole list as a value list '
        '(vl_...) for refine_view and gives a download link for the person. For a field whose values are kept from '
        'you, values is empty and search is refused; its list still filters it.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            **_VIEW_OR_DATASET,
            'field': {'type': 'string', 'maxLength': 255, 'description': 'The field.'},
            'search': {'type': 'string', 'maxLength': 200, 'description': 'Text a value must contain.'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 20, 'description': 'Values shown.'},
            'offset': {
                'type': 'integer',
                'minimum': 0,
                'default': 0,
                'description': 'Values to skip: the next_offset of the previous page. Every page names the same list.',
            },
        },
        'required': ['field'],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(('GET', DATASET), ('GET', SCHEMA), ('POST', AGGREGATE), ('POST', QUERY)),
    run=_counting(field_values),
)

FIELD_SUMMARY = Tool(
    name='field_summary',
    description=(
        'Summarise one field over a view\'s samples (or a whole dataset\'s): for numbers and datetimes the smallest, '
        'largest, mean, quantiles and a histogram; for text, lists and booleans the distinct count and the five '
        'commonest values; and how many samples have the field.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            **_VIEW_OR_DATASET,
            'field': {'type': 'string', 'maxLength': 255, 'description': 'The field.'},
        },
        'required': ['field'],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(('GET', DATASET), ('GET', SCHEMA), ('POST', AGGREGATE), ('POST', QUERY)),
    run=_counting(field_summary),
)

GET_SAMPLES = Tool(
    name='get_samples',
    description=(
        'A page of a view\'s samples (or a whole dataset\'s), each with its id and a link that opens it in the app; '
        'with `fields`, also those fields, when an administrator allows the assistant to read samples\' fields. '
        'Fields whose values are kept from you are left out (withheld), and samples can\'t be sorted by them.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            **_VIEW_OR_DATASET,
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 10, 'description': 'Samples shown.'},
            'offset': {'type': 'integer', 'minimum': 0, 'default': 0, 'description': 'Samples to skip.'},
            'sort_by': {'type': 'string', 'maxLength': 255, 'description': 'A field to sort by (default: the id).'},
            'descending': {'type': 'boolean', 'default': False, 'description': 'Sort from the largest.'},
            'fields': {
                'type': 'array',
                'items': {'type': 'string', 'maxLength': 255},
                'maxItems': 50,
                'description': 'Fields to show for each sample.',
            },
        },
        'required': [],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(('GET', DATASET), ('GET', SCHEMA), ('POST', QUERY)),
    run=_counting(get_samples),
)

WORKING_TOOLS: tuple[Tool, ...] = (MAKE_VIEW, REFINE_VIEW, FIELD_VALUES, FIELD_SUMMARY, GET_SAMPLES)
