"""The finding tools (docs/ASSISTANT.md, "Finding"): the places a name could mean, a dataset's concepts and what one
would cut from a view, the images most like some words or some samples, and a dataset's saved views, opened as
working views.

Each reads through the API as the user (api.py), only the routes it lists. find_place reads the regions, which belong
to no dataset. The others read one dataset; what they count, they count at the image grain, as every working view does
(working.py, grain.py). Words a person or a model wrote never become a stage's words: a stage is worded by the server
(the filter language's writing, private values kept back) or by a fixed phrase for a kind with no text form.
"""

import json
from typing import Any, Iterable, Optional

from . import links
from .api import PRIVATE_FIELD_CODE, Api, ToolError
from .catalog import CONCEPTS, COVERAGE, DATASET, SCHEMA, VIEWS, Tool
from .working import (
    _VIEW_OR_DATASET,
    FORMAT,
    QUERY,
    _check_size,
    _count,
    _counting,
    _dataset,
    _fit,
    _images,
    _nodes,
    _target,
    _user,
    _view_answer,
)

REGIONS = '/api/regions'
BOUNDARY = '/api/datasets/{id}/concepts/{concept_id}/boundary'
SEARCH = '/api/search'
SAVED_VIEW = '/api/views/{slug}'

# A person's words shown as data (a saved view's name and description), cut past these lengths with "…".
NAME_LIMIT = 200
DESCRIPTION_LIMIT = 300


def _next(offset: int, shown: int, total: int) -> Optional[int]:
    return offset + shown if offset + shown < total else None


def _bounded(text: Any, limit: int) -> Optional[str]:
    if not isinstance(text, str):
        return None
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _limits(spec: dict[str, Any], low: int, high: int, default: int, what: str) -> dict[str, Any]:
    return {**spec, 'minimum': low, 'maximum': high, 'default': default, 'description': what}


_LIMIT = {'type': 'integer'}
_OFFSET = {
    'type': 'integer',
    'minimum': 0,
    'default': 0,
    'description': 'How many to skip: the next_offset of the previous page.',
}
_DATASET = {
    'type': 'integer',
    'minimum': 1,
    'maximum': 2_147_483_647,
    'description': "The dataset's id (from list_datasets).",
}


# --- find_place ---

_KINDS = ('country', 'state', 'group')
_REGION_PAGE = 100  # the most one search of the regions answers (GET /api/regions)
NO_BOUNDARIES = (
    'No boundary set is loaded on this server, so no place can be found by its name. An administrator loads '
    'boundaries under Settings → System status (Region boundaries).'
)


def _place(row: dict[str, Any]) -> dict[str, Any]:
    key = row['key']
    return {
        'key': key,
        'name': row['name'],
        'kind': row['kind'],
        'country': row.get('country_name') if row['kind'] != 'country' else None,
        # The filter language's own writing of a region test (filter_query._format).
        'use': f'location within {json.dumps(key, ensure_ascii=False)}',
    }


async def find_place(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    text, limit = arguments['text'].strip(), arguments.get('limit', 5)
    if not text:
        raise ToolError('invalid', "text: the place's name or key")
    found = await api.get(REGIONS, query={'search': text, 'limit': _REGION_PAGE})
    # An empty search finds any region: none at all means no boundary set is loaded.
    if not found and not await api.get(REGIONS, query={'search': '', 'limit': 1}):
        return {'places': [], 'total': 0, 'loaded': False, 'note': NO_BOUNDARIES}
    total: Optional[int] = len(found)
    if len(found) >= _REGION_PAGE:  # more than one search answers: counted kind by kind
        by_kind = [
            len(await api.get(REGIONS, query={'search': text, 'kind': kind, 'limit': _REGION_PAGE})) for kind in _KINDS
        ]
        total = sum(by_kind) if max(by_kind) < _REGION_PAGE else None
    result: dict[str, Any] = {'places': [_place(row) for row in found[:limit]], 'total': total, 'loaded': True}
    if total is None:
        result['note'] = (
            f'More than {_REGION_PAGE} regions of one kind match {text!r}, too many to count here: write more of the '
            'name, or its key.'
        )
    return result


# --- list_concepts ---


def _concept_test(concept: dict[str, Any], op: str, threshold: float, *, exclude: bool = False) -> dict[str, Any]:
    """The filter `concept "name" <op> threshold` (concept_exclude: its `not`), as the filter language makes it."""
    return {
        'field': '_concept',
        'operator': 'concept_exclude' if exclude else 'concept_select',
        'value': {'concept_id': concept['id'], 'op': op, 'threshold': float(threshold)},
    }


async def _written(api: Api, dataset_id: int, nodes: list[dict[str, Any]]) -> list[Optional[str]]:
    """Each filter as the server writes it in the filter language."""
    if not nodes:
        return []
    return list((await api.post(FORMAT, {'filters': nodes}, id=dataset_id))['parts'])


def _by_name(concepts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(concepts, key=lambda concept: (str(concept['name']).casefold(), concept['id']))


async def list_concepts(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    dataset_id, limit, offset = arguments['dataset'], arguments.get('limit', 25), arguments.get('offset', 0)
    await _dataset(api, dataset_id)
    concepts = _by_name(await api.get(CONCEPTS, id=dataset_id))
    page = concepts[offset : offset + limit]
    uses = await _written(api, dataset_id, [_concept_test(concept, '>', concept['threshold']) for concept in page])
    entries = _fit(
        [
            {
                'name': concept['name'],
                'threshold': concept['threshold'],
                'ready': bool(concept.get('scores_ready', True)),
                'use': use,
            }
            for concept, use in zip(page, uses)
        ],
    )
    return {
        'concepts': entries,
        'total': len(concepts),
        'offset': offset,
        'next_offset': _next(offset, len(entries), len(concepts)),
    }


# --- concept_preview ---

BOUNDARY_NOTE = (
    'The samples nearest the cut are kept for the whole dataset only: ask about the dataset (or a view with no stages) '
    'to see them. The counts are exact for this view.'
)


def _no_concept(name: str, concepts: list[dict[str, Any]]) -> str:
    names = [concept['name'] for concept in _by_name(concepts)]
    if not names:
        return f'This dataset has no concept named {name!r}: it has no concepts'
    more = f' (the first 20 of {len(names)})' if len(names) > 20 else ''
    shown = ', '.join(json.dumps(each, ensure_ascii=False) for each in names[:20])
    return f'This dataset has no concept named {name!r}. Its concepts{more}: {shown}'


async def _image_ids(api: Api, dataset_id: int, ids: list[int]) -> set[int]:
    """Which of `ids` are the dataset's images (not object rows)."""
    if not ids:
        return set()
    filters = {
        'logic': 'AND',
        'filters': [
            {'field': 'id', 'operator': 'in', 'value': ids},
            {'field': 'media_type', 'operator': '!=', 'value': 'object'},
        ],
    }
    page = await api.post(
        QUERY,
        {'filters': filters, 'fields': 'id', 'slim': True, 'limit': len(ids)},
        id=dataset_id,
    )
    return {item['id'] for item in page['items']}


async def _nearest(
    api: Api,
    dataset_id: int,
    concept_id: int,
    threshold: float,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The `limit` images nearest the cut on each side, nearest first, from the concept's boundary (which keeps the
    whole dataset's, object rows included): asked again for more while object rows take the places."""
    half = limit
    while True:
        rows = await api.get(
            BOUNDARY,
            id=dataset_id,
            concept_id=concept_id,
            query={'op': '>', 'threshold': threshold, 'limit': 2 * half},
        )
        above = sorted((row for row in rows if row['score'] > threshold), key=lambda row: row['score'])
        below = sorted((row for row in rows if row['score'] <= threshold), key=lambda row: -row['score'])
        images = await _image_ids(api, dataset_id, [row['sample_id'] for row in rows])
        shown_above = [row for row in above if row['sample_id'] in images][:limit]
        shown_below = [row for row in below if row['sample_id'] in images][:limit]
        if (len(shown_above) >= limit or len(above) < half) and (len(shown_below) >= limit or len(below) < half):
            break
        half *= 4

    def entry(row: dict[str, Any]) -> dict[str, Any]:
        return {'id': row['sample_id'], 'score': row['score'], 'link': links.sample_link(dataset_id, row['sample_id'])}

    return [entry(row) for row in shown_above], [entry(row) for row in shown_below]


async def concept_preview(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    target = await _target(api, arguments)
    dataset_id, name, limit = target.dataset_id, arguments['concept'], arguments.get('limit', 5)
    concepts = await api.get(CONCEPTS, id=dataset_id)
    concept = next((each for each in concepts if each['name'] == name), None)
    if concept is None:
        raise ToolError('not_found', _no_concept(name, concepts))
    given = arguments.get('threshold')
    threshold = float(concept['threshold'] if given is None else given)
    keep, at_or_below = _concept_test(concept, '>', threshold), _concept_test(concept, '<=', threshold)
    # Each count is the grid's for the view with the test added (a concept being rescored: the API's refusal).
    nodes = await _nodes(target.stages)
    viewed = await _images(api, dataset_id, nodes)
    over, under = await _images(api, dataset_id, [*nodes, keep]), await _images(api, dataset_id, [*nodes, at_or_below])
    view_count, above, below = [await _count(api, dataset_id, tree) for tree in (viewed, over, under)]
    if api.facts.get(f'objects:{dataset_id}', True):
        # With object rows, an image is above the cut when it or one of its objects scores above it, and below when it
        # or one of them scores at or below it (the image grain): it can be both. The rest is counted, never inferred.
        rest = {
            'logic': 'AND',
            'filters': [viewed, {'logic': 'NOT', 'filters': [over]}, {'logic': 'NOT', 'filters': [under]}],
        }
        unscored = await _count(api, dataset_id, rest)
    else:
        unscored = view_count - above - below  # an image's own score is above, at or below, or missing
    kept, removed = await _written(api, dataset_id, [keep, _concept_test(concept, '>', threshold, exclude=True)])
    result: dict[str, Any] = {
        'concept': concept['name'],
        'threshold': threshold,
        'view_count': view_count,
        'above': above,
        'below': below,
        'unscored': unscored,
        'just_above': None,
        'just_below': None,
        'keep': kept,
        'remove': removed,
    }
    both = above + below + unscored - view_count
    if both:
        result['note'] = (
            f'{both:,} of these images count on both sides of the cut: on this dataset an image is above it when it or '
            'one of its objects scores above it, and below it when it or one of them scores at or below it.'
        )
    if target.stages:
        result['boundary_note'] = BOUNDARY_NOTE
    else:
        result['just_above'], result['just_below'] = await _nearest(api, dataset_id, concept['id'], threshold, limit)
    return result


# --- search_similar ---

SAMPLES_SHOWN = 20
SEARCH_NOTE = (
    'The search stopped before it had looked at every image the view selects, so more may match: narrow the view to '
    'search all of it.'
)


def _default_field(fields: list[str]) -> str:
    """The field the app's own text search uses: clip, else the first whole-image field."""
    whole = [name for name in fields if '_patch_' not in name and name != 'clip_obj']
    return 'clip' if 'clip' in fields else (whole or fields)[0]


async def search_similar(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    target = await _target(api, arguments)
    dataset_id = target.dataset_id
    text, seeds = arguments.get('text'), arguments.get('sample_ids')
    if (text is None) == (seeds is None):
        raise ToolError(
            'invalid',
            'Give text (words to search for) or sample_ids (samples to find more like), '
            + ('not both' if text is not None else 'one of the two'),
        )
    if text is not None and not text.strip():
        raise ToolError('invalid', 'text: say what to search for')
    # The view's bounds with the found stage added, before anything is searched (checked again when it is kept).
    await _check_size([*target.stages, {'kind': 'filter', 'filter': {}, 'label': '', 'negated': False}])
    embedded = [entry['field'] for entry in (await api.get(COVERAGE, id=dataset_id))['embeddings']]
    if not embedded:
        raise ToolError('invalid', "This dataset has no embedding field, so it can't be searched for similar samples")
    field = arguments.get('field') or _default_field(embedded)
    if field not in embedded:
        raise ToolError(
            'invalid',
            f"'{field}' is not an embedding field of this dataset: its embedding fields are {', '.join(embedded)}",
        )
    body: dict[str, Any] = {'dataset_id': dataset_id, 'field_name': field, 'limit': arguments.get('limit', 50)}
    if seeds is not None:
        unique = list(dict.fromkeys(seeds))
        filters = {'field': 'id', 'operator': 'in', 'value': unique}
        page = await api.post(
            QUERY,
            {'filters': filters, 'fields': 'id', 'slim': True, 'limit': len(unique)},
            id=dataset_id,
        )
        known = {item['id'] for item in page['items']}
        missing = [str(each) for each in unique if each not in known]
        if missing:
            raise ToolError(
                'invalid',
                f"Not samples of this dataset: {', '.join(missing)}. Give the ids of its samples (get_samples, or an "
                'earlier answer).',
            )
        body['sample_ids'] = unique
    else:
        body['text'] = text  # sent to the search alone: it never becomes a stage's words
    # Among the images the view selects, as the app searches within its filters.
    body['filters'] = await _images(api, dataset_id, await _nodes(target.stages))
    searched = await _count(api, dataset_id, body['filters'])
    try:
        found = await api.post(SEARCH, body)
    except ToolError as error:
        if error.code != 'not_found':
            raise
        # The samples given have no vector in the field (the search's 404): what can't be searched for.
        raise ToolError('invalid', f'{error.message}: give samples that have one, or text') from None
    incomplete = api.headers.get('x-search-incomplete') == '1'
    ids = [item['id'] for item in found]
    found_stage = {
        'kind': 'filter',
        'filter': {'field': 'id', 'operator': 'in', 'value': ids},
        'label': f'a similarity search ({len(ids):,} sample{"" if len(ids) == 1 else "s"})',
        'negated': False,
    }
    stages = [*target.stages, found_stage]
    answer = await _view_answer(
        api,
        dataset_id,
        stages,
        from_handle=target.view['handle'] if target.view else None,
    )
    answer.update(
        samples=[
            {
                'id': item['id'],
                'score': None if item.get('distance') is None else 1 - item['distance'],
                'link': links.sample_link(dataset_id, item['id']),
            }
            for item in found[:SAMPLES_SHOWN]
        ],
        found_stage=len(stages),
        searched=searched,
        field=field,
    )
    if incomplete:
        answer['search_note'] = SEARCH_NOTE
    return answer


def _add_note(answer: dict[str, Any], note: str) -> None:
    """`note` added to the answer's (a view answer may already have one: values that couldn't be read back)."""
    answer['note'] = f"{answer['note']} {note}" if answer.get('note') else note


# --- list_saved_views and open_saved_view ---


def _held_stages(content: Any) -> list[tuple[Any, dict[str, Any]]]:
    """A saved view's stages, (the chip's type, its filter), as the app opens it: its viewStages, else one stage of its
    compiled filter (a view saved before views kept their stages), else none."""
    content = content if isinstance(content, dict) else {}
    held = content.get('viewStages')
    if isinstance(held, list):
        return [
            (stage.get('type'), stage['filter'])
            for stage in held
            if isinstance(stage, dict) and isinstance(stage.get('filter'), dict)
        ]
    compiled = content.get('filters')
    if isinstance(compiled, dict) and (compiled.get('filters') or compiled.get('field')):
        return [('filter', compiled)]
    return []


def _walk(node: Any) -> Iterable[dict[str, Any]]:
    """Every filter node in a tree."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)


def _concept_ids(node: dict[str, Any]) -> set[Any]:
    return {
        each['value'].get('concept_id')
        for each in _walk(node)
        if each.get('operator') in ('concept_select', 'concept_exclude') and isinstance(each.get('value'), dict)
    }


# Fixed words for a stage the filter language can't write, by what it tests (never the chip's own words, which a
# saved view keeps as written: they can hold kept values, or anything at all).
_NO_TEXT_FORM = (
    ('geo_polygon_exclude', 'outside a shape drawn on the map'),
    ('geo_polygon', 'a shape drawn on the map'),
    ('embedding_polygon_exclude', 'outside a lasso on the embedding map'),
    ('embedding_polygon', 'a lasso on the embedding map'),
    ('in_selection', 'picked samples (a temporary selection)'),
)
_BY_TYPE = {
    'similarity': 'a similarity search',
    'selection': 'picked samples',
    'map_region': 'a shape drawn on the map',
}


def _label(kind: Any, node: dict[str, Any]) -> str:
    operators = {each.get('operator') for each in _walk(node)}
    phrase = next((words for operator, words in _NO_TEXT_FORM if operator in operators), None)
    if phrase is None:
        phrase = (_BY_TYPE.get(kind) if isinstance(kind, str) else None) or 'a stage with no text form'
    ids = sum(
        len(each['value'])
        for each in _walk(node)
        if each.get('field') == 'id' and each.get('operator') == 'in' and isinstance(each.get('value'), list)
    )
    return f'{phrase} ({ids:,} samples)' if ids and phrase in ('a similarity search', 'picked samples') else phrase


async def list_saved_views(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    dataset_id, limit, offset = arguments['dataset'], arguments.get('limit', 25), arguments.get('offset', 0)
    dataset = await _dataset(api, dataset_id)
    default_id = (dataset.get('default_view') or {}).get('id')
    views = sorted(await api.get(VIEWS, id=dataset_id), key=lambda view: (view['name'].casefold(), view['id']))
    me = _user(api)
    entries = _fit(
        [
            {
                'slug': view['slug'],
                'name': _bounded(view['name'], NAME_LIMIT),
                'description': _bounded(view.get('description'), DESCRIPTION_LIMIT),
                'mine': view.get('user_id') == me,
                'default': view['id'] == default_id,
                'stages': len(_held_stages(view.get('content'))),
                'link': links.saved_view_link(dataset_id, view['slug']),
            }
            for view in views[offset : offset + limit]
        ],
    )
    return {
        'views': entries,
        'total': len(views),
        'offset': offset,
        'next_offset': _next(offset, len(entries), len(views)),
    }


def _no_saved_view(slug: str) -> str:
    return f'No saved view {slug!r} that you can open: list_saved_views gives a dataset\'s saved views and their slugs'


async def open_saved_view(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    slug = arguments['slug']
    if not slug.strip() or '/' in slug:
        raise ToolError('not_found', _no_saved_view(slug))
    # One the user can't see answers as one that doesn't exist (the route's 404 for both).
    saved = await api.get(SAVED_VIEW, slug=slug, missing=_no_saved_view(slug))
    dataset_id = saved['dataset_id']
    await _dataset(api, dataset_id)
    held = _held_stages(saved.get('content'))
    gone: list[str] = []
    if any(_concept_ids(node) for _, node in held):
        # As the app opens it: a stage naming a concept the dataset no longer has is left out, and said.
        known = {concept['id'] for concept in await api.get(CONCEPTS, id=dataset_id)}
        kept = []
        for number, (kind, node) in enumerate(held, start=1):
            missing = sorted(str(each) for each in _concept_ids(node) - known)
            if missing:
                gone.append(
                    f'Stage {number} as saved was left out, as the app leaves it out: it tests concept '
                    f'{", ".join(missing)}, which this dataset no longer has.',
                )
            else:
                kept.append((kind, node))
        held = kept
    stages = [{'kind': 'filter', 'filter': node, 'label': _label(kind, node), 'negated': False} for kind, node in held]
    link = links.saved_view_link(dataset_id, saved['slug'])
    try:
        answer = await _view_answer(api, dataset_id, stages, permanent=link, labelled=True)
    except ToolError as error:
        if error.code == 'refused' and error.reason == PRIVATE_FIELD_CODE:
            raise ToolError('refused', _kept_from_assistant(saved['name'], link)) from None
        raise
    answer['saved'] = {'slug': saved['slug'], 'name': _bounded(saved['name'], NAME_LIMIT)}
    for note in gone:
        _add_note(answer, note)
    return answer


def _kept_from_assistant(name: str, link: str) -> str:
    """Why a saved view that ranges over or searches within a private field (what the API refuses for a tool) can't
    be opened here, and where it can be: naming the view, and nothing of its stages."""
    return (
        f'The saved view {_bounded(name, NAME_LIMIT)!r} filters a field whose values are kept from the assistant, in '
        f'a way only a person can apply: open it in the app instead, {link}'
    )


# --- the registry ---

FIND_PLACE = Tool(
    name='find_place',
    description=(
        'The regions a place name could mean (countries, states and groups of a boundary set the server has loaded), '
        'best match first, each with its key, kind, the country it belongs to, and `use`: the filter text that keeps '
        'the samples inside it (`location within "US-KS"`; `location not within` removes them). When several match '
        '(Georgia is a country and a state), ask the person which is meant. Use it when the samples have a location '
        'and no field names the place; it reads no dataset.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'text': {'type': 'string', 'maxLength': 200, 'description': "The place's name, or its key."},
            'limit': _limits(_LIMIT, 1, 10, 5, 'How many places at most (1 to 10).'),
        },
        'required': ['text'],
        'additionalProperties': False,
    },
    kind='read',
    level=None,
    routes=(('GET', REGIONS),),
    run=find_place,
)

LIST_CONCEPTS = Tool(
    name='list_concepts',
    description=(
        "A dataset's concepts by name: learned scores that pick out a kind of image (equipment, people, blur). Each "
        'has its saved cut (`threshold`), whether its scores are ready, and `use`, the filter text that keeps the '
        'images above the cut (`concept "equipment" > 0.62`; `not ...` removes them). concept_preview shows what a '
        'cut would do to a view first.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'dataset': _DATASET,
            'limit': _limits(_LIMIT, 1, 50, 25, 'How many concepts at most (1 to 50).'),
            'offset': _OFFSET,
        },
        'required': ['dataset'],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(('GET', DATASET), ('GET', CONCEPTS), ('POST', FORMAT)),
    run=list_concepts,
)

CONCEPT_PREVIEW = Tool(
    name='concept_preview',
    description=(
        "What a concept's cut would do to a view (or a whole dataset) before applying it: exactly how many of its "
        'images score above the cut, at or below it, and have no score; for a whole dataset (or a view with no '
        'stages) the images nearest the cut on each side, with their scores and links, for the person to judge it. '
        '`keep` and `remove` are the filter texts for refine_view\'s add.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            **_VIEW_OR_DATASET,
            'concept': {'type': 'string', 'maxLength': 1000, 'description': "The concept's name (from list_concepts)."},
            'threshold': {
                'type': 'number',
                'description': "The cut to preview; the concept's saved cut when left out.",
            },
            'limit': _limits(_LIMIT, 1, 10, 5, 'How many images nearest the cut on each side (1 to 10).'),
        },
        'required': ['concept'],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(('GET', DATASET), ('GET', CONCEPTS), ('GET', BOUNDARY), ('POST', QUERY), ('POST', FORMAT)),
    run=_counting(concept_preview),
)

SEARCH_SIMILAR = Tool(
    name='search_similar',
    description=(
        "The images of a view (or a whole dataset) most like some words (`text`) or like some samples "
        '(`sample_ids`), most similar first, by an embedding field. Answers a working view: the stages of the view '
        'searched, then one stage (numbered `found_stage`) holding the ids found; refine_view with '
        '`negate: [found_stage]` is the view searched without them ("remove the ones like this"). `samples` shows the '
        'first 20 with their scores and links; `searched` is how many images were searched.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            **_VIEW_OR_DATASET,
            'text': {'type': 'string', 'maxLength': 1000, 'description': 'Words describing the images to find.'},
            'sample_ids': {
                'type': 'array',
                'items': {'type': 'integer', 'minimum': 1, 'maximum': 2**63 - 1},
                'minItems': 1,
                'maxItems': 20,
                'description': 'Samples to find more like (1 to 20 ids). Give text or sample_ids.',
            },
            'limit': _limits(_LIMIT, 1, 200, 50, 'How many images to find (1 to 200).'),
            'field': {
                'type': 'string',
                'maxLength': 255,
                'description': "The embedding field to search by; the dataset's default when left out.",
            },
        },
        'required': [],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(
        ('GET', DATASET),
        ('GET', SCHEMA),
        ('GET', COVERAGE),
        ('POST', QUERY),
        ('POST', FORMAT),
        ('POST', SEARCH),
    ),
    run=_counting(search_similar),
)

LIST_SAVED_VIEWS = Tool(
    name='list_saved_views',
    description=(
        "A dataset's saved views that the person can see (their own, those shared with them and the public ones), by "
        'name: each with its slug, the description its author wrote, whether it is theirs, whether it is the '
        "dataset's default view, how many stages it has, and its own permanent link. open_saved_view opens one."
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'dataset': _DATASET,
            'limit': _limits(_LIMIT, 1, 50, 25, 'How many views at most (1 to 50).'),
            'offset': _OFFSET,
        },
        'required': ['dataset'],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(('GET', DATASET), ('GET', VIEWS)),
    run=list_saved_views,
)

OPEN_SAVED_VIEW = Tool(
    name='open_saved_view',
    description=(
        'A saved view, by its slug, as a working view to count, describe or refine like any other: the answer of '
        'make_view, plus `saved` (its slug and name), with the saved view\'s own permanent link. A stage the filter '
        'language can\'t write (a shape drawn on the map, a lasso, a similarity search) has `"text": null` and a '
        '`label`. Opening it changes nothing.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'slug': {
                'type': 'string',
                'maxLength': 200,
                'description': "The saved view's slug (from list_saved_views).",
            },
        },
        'required': ['slug'],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(
        ('GET', SAVED_VIEW),
        ('GET', DATASET),
        ('GET', SCHEMA),
        ('GET', CONCEPTS),
        ('POST', QUERY),
        ('POST', FORMAT),
    ),
    run=_counting(open_saved_view),
)

FINDING_TOOLS: tuple[Tool, ...] = (
    FIND_PLACE,
    LIST_CONCEPTS,
    CONCEPT_PREVIEW,
    SEARCH_SIMILAR,
    LIST_SAVED_VIEWS,
    OPEN_SAVED_VIEW,
)
