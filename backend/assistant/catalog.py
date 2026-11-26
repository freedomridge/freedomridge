"""The assistant's tools, in their fixed order (docs/ASSISTANT.md). Each reads through the API as the user (api.py)
and calls only the routes it lists; every tool here is a read.

A result is small and never quietly cut: each list has an upper bound, and comes with the true total and the offset
that gets the rest.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Mapping, Optional

from .api import Api, ToolError

Kind = Literal['read', 'propose']
Level = Optional[Literal['view', 'tag', 'edit', 'manage']]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    kind: Kind
    level: Level
    routes: tuple[tuple[str, str], ...]
    run: Callable[[Api, dict[str, Any]], Awaitable[dict[str, Any]]]


def _page(offset: int, limit: int, total: int) -> Optional[int]:
    """The offset of the next page; None on the last."""
    return offset + limit if offset + limit < total else None


def _matches(search: Optional[str], *texts: Optional[str]) -> bool:
    if not search:
        return True
    needle = search.casefold()
    return any(text is not None and needle in text.casefold() for text in texts)


def _any_word(search: Optional[str], *texts: Optional[str]) -> bool:
    """Whether any word of `search` is in one of `texts`, in any letter case (all match without a search)."""
    words = (search or '').casefold().split()
    return not words or any(text is not None and any(word in text.casefold() for word in words) for text in texts)


# --- list_datasets ---

DATASETS = '/api/datasets'
DATASET = '/api/datasets/{id}'


def _dataset_entry(dataset: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'id': dataset['id'],
        'name': dataset['name'],
        'description': dataset.get('description'),
        'level': dataset.get('access_level'),
    }


async def list_datasets(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    search, limit, offset = arguments.get('search'), arguments.get('limit', 25), arguments.get('offset', 0)
    # The datasets the user can open; one closed to the assistant isn't among them (access.filter_datasets_for_user).
    datasets = [
        dataset
        for dataset in await api.get(DATASETS)
        if not dataset.get('deleting') and _matches(search, dataset['name'])
    ]
    datasets.sort(key=lambda dataset: (dataset['name'].casefold(), dataset['id']))
    page = datasets[offset : offset + limit]
    # No counts: the app's own list has none, and counting every dataset of a page is a scan each.
    entries = [_dataset_entry(dataset) for dataset in page]
    return {
        'datasets': entries,
        'total': len(datasets),
        'offset': offset,
        'next_offset': _page(offset, limit, len(datasets)),
    }


# --- describe_dataset ---

SCHEMA = '/api/datasets/{id}/schema'
FIELD_STATS = '/api/datasets/{id}/field-stats'
COVERAGE = '/api/datasets/{id}/coverage'
CONCEPTS = '/api/datasets/{id}/concepts'
VIEWS = '/api/datasets/{id}/views'

# Fields every sample has: a column that is never empty.
_ALWAYS_SET = ('id', 'filepath', 'media_type')
# Schema types whose values are drawings or files, not values a filter compares.
_NO_VALUES = frozenset(
    {
        'filepath',
        'detections',
        'polygons',
        'keypoints',
        'cuboids',
        'heatmap',
        'segmentation',
        'classification',
        'class_probabilities',
    },
)
EXAMPLES = 5


def _kind(field: Mapping[str, Any]) -> str:
    """The kind the filter language gives the field: number, datetime, text, list, boolean or object
    (routers/filter_query.field_kinds, from what the schema answers)."""
    from ..field_registry import canonical_type
    from ..filter_query import TEXT
    from ..routers.filter_query import _KINDS, _SYSTEM_FIELDS

    name = field['name']
    if name in _SYSTEM_FIELDS:
        return _SYSTEM_FIELDS[name]
    if field.get('type') == 'array' or field.get('origin') == 'native_array':
        return 'list'
    if field.get('type') == 'filepath':
        return TEXT
    return _KINDS.get(canonical_type(field.get('declared_type') or 'string'), TEXT)


# The built-in fields an index of the samples table serves a filter on within a dataset (backend/db/schema.sql):
# equality and ranges on (dataset_id, id), (dataset_id, filepath) unique, (dataset_id, created_at) and
# (dataset_id, media_type), and `in` (has any of these values, an overlap) on tags' GIN index, which doesn't serve `=`
# (a test of each sample's list). last_modified_at has no index: one would make every edit's update slower.
INDEXED_BUILT_IN = {
    'id': True,
    'filepath': True,
    'created_at': True,
    'media_type': True,
    'last_modified_at': False,
    'tags': True,
}


def list_column(field: Mapping[str, Any]) -> bool:
    """Whether the field is a list the samples table keeps in a column of its own (tags, a registered array field),
    whose index serves `in` but not `=`."""
    return field.get('type') == 'array' or field.get('origin') == 'native_array'


def indexed(field: Mapping[str, Any]) -> bool:
    """Whether a filter on the field with =, in or a narrow range (`in`, for a list_column) is served by an index, from
    what the schema already says (no read of the samples): a built-in field by INDEXED_BUILT_IN, a registered array
    field always (its column gets its GIN index with it), and any other field when the dataset has an index on it
    (is_promoted, what the app shows as indexed)."""
    if field['name'] in INDEXED_BUILT_IN:
        return INDEXED_BUILT_IN[field['name']]
    if field.get('origin') == 'native_array':
        return True
    return bool(field.get('is_promoted'))


def _private_names(schema: list[dict[str, Any]]) -> set[str]:
    """Fields whose values are kept from the assistant: those marked, and the label lists of a marked field
    (`detections.label` holds values of `detections`)."""
    marked = {field['name'] for field in schema if field.get('values_private')}
    return marked | {
        field['name'] for field in schema if '.' in field['name'] and field['name'].rsplit('.', 1)[0] in marked
    }


def _values(field: Mapping[str, Any], kind: str, private: bool, up_to_date: bool) -> Optional[dict[str, Any]]:
    """The field's `values`, from the list of them the schema answers, which the statistics keep.

    Up to date, the statistics either hold the whole list (`values_complete`; the built-in tags and media_type the
    schema reads whole each time) or have more different values than they keep a list of: a text field's list is
    dropped past the cap (no `values` at all), another kind's cut at it. That is `many`, and no fault. A list held only
    in part because the count is behind comes with changes waiting to be counted: not `up_to_date`, so no `distinct`.
    A boolean's values are true and false: null, as for a field whose values aren't values a filter compares."""
    if kind not in ('text', 'list') or field.get('type') in _NO_VALUES:
        return None
    if kind == 'list' and field.get('type') != 'array':
        return None  # a JSON list's values are lists
    held = field.get('values')
    if not isinstance(held, list):
        if field.get('type') != 'string':
            return None  # no list kept for this kind of field
        held = []  # text whose list the statistics dropped, or haven't made yet
    examples = [] if private else held[:EXAMPLES]
    if not up_to_date:
        return {'distinct': None, 'examples': examples}
    if field.get('type') == 'string' or field.get('values_complete', True) is False:
        return {'distinct': None, 'examples': examples, 'many': True}
    return {'distinct': len(held), 'examples': examples}


async def describe_dataset(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    """Reads only what the server keeps about the dataset: six requests, however many fields the page has."""
    dataset_id = arguments['dataset']
    search, limit, offset = arguments.get('search'), arguments.get('limit', 50), arguments.get('offset', 0)
    dataset = await api.get(DATASET, id=dataset_id, missing=f'Dataset not found: id {dataset_id}')
    if dataset.get('deleting'):
        raise ToolError('not_found', f"Dataset '{dataset['name']}' (id {dataset_id}) is being deleted")
    # The field statistics first: they find a drawing field's labels (`detections.label`), which the schema lists after.
    stats = {field['name']: field for field in await api.get(FIELD_STATS, id=dataset_id)}
    schema = await api.get(SCHEMA, id=dataset_id, query={'stale': 'true'})
    coverage = await api.get(COVERAGE, id=dataset_id)
    concepts = await api.get(CONCEPTS, id=dataset_id)
    views = await api.get(VIEWS, id=dataset_id)

    private = _private_names(schema)
    matched = [field for field in schema if _any_word(search, field['name'], field.get('description'))]
    fields = []
    for field in matched[offset : offset + limit]:
        name, kind = field['name'], _kind(field)
        is_private = name in private
        sample_count = stats.get(name, {}).get('sample_count')
        if sample_count is None and name in _ALWAYS_SET:
            sample_count = coverage['total_samples']
        value_range = None
        if kind in ('number', 'datetime') and not is_private and field.get('min') is not None:
            value_range = {'min': field['min'], 'max': field.get('max')}
        # Up to date: counted, and no change waits to be counted that the field's numbers would show.
        complete = not field.get('stats_pending', False) and not field.get('stats_stale', False)
        values = _values(field, kind, is_private, complete)
        fields.append(
            {
                'name': name,
                'type': kind,
                'indexed': indexed(field),
                'description': field.get('description'),
                'unit': field.get('unit'),
                'description_source': field.get('description_source'),
                'samples_with_value': sample_count,
                'range': value_range,
                'values': values,
                'values_private': is_private,
                'complete': complete,
            },
        )
    # What a view of the whole dataset counts (the dataset's page, no filter): its images and other media, not the
    # object rows beside them.
    objects = coverage.get('total_objects') or 0
    return {
        'dataset': {
            'id': dataset['id'],
            'name': dataset['name'],
            'description': dataset.get('description'),
            'samples': coverage['total_samples'] - objects,
            'objects': objects,
            'level': dataset.get('access_level'),
        },
        'fields': fields,
        'fields_total': len(matched),
        'offset': offset,
        'next_offset': _page(offset, limit, len(matched)),
        'undescribed_fields': sum(1 for field in schema if field.get('description_source') is None),
        'samples_with_location': coverage['location']['present'],
        'embedding_fields': [entry['field'] for entry in coverage['embeddings']],
        'concepts': len(concepts),
        'saved_views': len(views),
    }


# --- the registry ---

_LIST_DATASETS = Tool(
    name='list_datasets',
    description=(
        'List the datasets you can read, by name: each with its id, description and your access level in it (view, '
        'tag, edit or manage). `search` keeps those whose name contains the text, in any letter case. A page holds '
        'at most `limit` datasets; `total` counts all that match, and `next_offset` (null on the last page) is the '
        '`offset` that gets the next page. Use a dataset id from here with describe_dataset, which also gives its '
        'counts.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'search': {
                'type': 'string',
                'maxLength': 200,
                'description': 'Text the dataset name must contain, in any letter case.',
            },
            'limit': {
                'type': 'integer',
                'minimum': 1,
                'maximum': 50,
                'default': 25,
                'description': 'How many datasets at most (1 to 50).',
            },
            'offset': {
                'type': 'integer',
                'minimum': 0,
                'default': 0,
                'description': 'How many matching datasets to skip: the next_offset of the previous page.',
            },
        },
        'required': [],
        'additionalProperties': False,
    },
    kind='read',
    level=None,
    routes=(('GET', DATASETS),),
    run=list_datasets,
)

_DESCRIBE_DATASET = Tool(
    name='describe_dataset',
    description=(
        "Describe one dataset and its fields, a page at a time, in the schema's order, from the statistics the "
        'server keeps (quick on any dataset; nothing is counted per value). Each field has its type in the filter '
        'language (number, datetime, text, list, boolean or object), what it means (description and unit, when '
        'someone wrote one or the server has a default), how many rows have it (object rows included), its smallest '
        'and largest value (number and datetime fields), or, for text and list fields, how many distinct values it '
        'has and up to five examples of them, without counts. `indexed: true` means a filter on the field with =, in '
        'or a narrow range is quick however large the dataset; for a list field it is `in` that is quick (= on a list '
        'is not served by its index). `many: true` means more different values than the '
        'server keeps a list of (serial numbers, file names): `distinct` is null and the examples are a few of them. '
        '`complete: false` on a field means its statistics are not up to date (samples changed since they were '
        'counted, or they are being counted): its numbers are what the server has now, not the whole, and `distinct` '
        'is null. A field with `values_private: true` keeps its values from you: you get its description and counts, '
        'never a value. `search` keeps the fields whose name or description contains any of its words (`dark bright '
        'gain` finds the fields about any of them in one call); `fields_total` counts '
        'them, and `next_offset` (null on the last page) gets the next page. Also: the dataset\'s samples (what a '
        'view of the whole dataset counts) and object rows, how many samples have a location, the embedding fields, '
        'and how many concepts and saved views the dataset has.'
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
            'search': {
                'type': 'string',
                'maxLength': 200,
                'description': (
                    "One or more words: a field is kept when any of them is in its name or description, in any "
                    'letter case.'
                ),
            },
            'limit': {
                'type': 'integer',
                'minimum': 1,
                'maximum': 100,
                'default': 50,
                'description': 'How many fields at most (1 to 100).',
            },
            'offset': {
                'type': 'integer',
                'minimum': 0,
                'default': 0,
                'description': 'How many matching fields to skip: the next_offset of the previous page.',
            },
        },
        'required': ['dataset'],
        'additionalProperties': False,
    },
    kind='read',
    level='view',
    routes=(
        ('GET', DATASET),
        ('GET', SCHEMA),
        ('GET', FIELD_STATS),
        ('GET', COVERAGE),
        ('GET', CONCEPTS),
        ('GET', VIEWS),
    ),
    run=describe_dataset,
)

LIST_DATASETS, DESCRIBE_DATASET = _LIST_DATASETS, _DESCRIBE_DATASET
