"""The filter query language over HTTP (docs/FILTER_QUERY.md): text to filters and back, checked against
the dataset's own fields and concepts. The filter bar's query editor, the SDK and AI tools all use these."""

from typing import Any, Dict, List, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import regions
from ..access import require_dataset_access
from ..assistant.identity import scope_identity
from ..assistant.private import private_fields
from ..database import db
from ..field_registry import canonical_type, field_stats
from ..filter_query import (
    BOOLEAN,
    DATETIME,
    LIST,
    NUMBER,
    OBJECT,
    TEXT,
    Concept,
    FilterQueryError,
    RegionMatch,
    format_filter,
    parse_query,
    region_names,
)
from .datasets import get_dataset_array_columns

router = APIRouter()

# Fields every dataset has, and the samples columns behind them.
_SYSTEM_FIELDS = {
    'id': NUMBER,
    'filepath': TEXT,
    'media_type': TEXT,
    'created_at': DATETIME,
    'last_modified_at': DATETIME,
    'tags': LIST,
}
_KINDS = {'number': NUMBER, 'datetime': DATETIME, 'boolean': BOOLEAN, 'list': LIST, 'object': OBJECT}


async def field_kinds(dataset_id: int) -> Dict[str, str]:
    """Every field a filter on this dataset can name, with its kind: the system fields, its array
    columns and media fields, and its registered fields (label fields as `name.label`)."""
    kinds = dict(_SYSTEM_FIELDS)
    kinds.update({column: LIST for column in await get_dataset_array_columns(dataset_id)})
    media_fields = await db.fetchval('SELECT media_fields FROM datasets WHERE id = $1', dataset_id)
    kinds.update({name: TEXT for name in media_fields or []})
    for stat in await field_stats(db, dataset_id):
        name = stat['field_name']
        if name.startswith('__') or name in kinds:
            continue
        native_array = stat.get('origin') == 'native_array'
        kinds[name] = LIST if native_array else _KINDS.get(canonical_type(stat['field_type']), TEXT)
    return kinds


async def dataset_concepts(dataset_id: int) -> List[Concept]:
    """The dataset's concepts, which a query names as `concept "name"`."""
    rows = await db.fetch('SELECT id, name, threshold FROM embedding_concepts WHERE dataset_id = $1', dataset_id)
    return [Concept(row['id'], row['name'], row['threshold']) for row in rows]


class FilterQueryParse(BaseModel):
    query: str = Field(max_length=1_000_000)
    # The filters already applied, one per chip: a part of the query equal to one of them is reported
    # in `reuse`, so the caller keeps that chip (its name, its default-view mark) instead of a new one.
    current: List[Dict[str, Any]] = Field(default_factory=list, max_length=1000)


class FilterQueryFormat(BaseModel):
    filters: List[Dict[str, Any]] = Field(max_length=1000)


@router.post('/datasets/{id}/filter_query/parse', dependencies=[Depends(require_dataset_access)])
async def parse_filter_query(id: int, request: FilterQueryParse):
    """The query's top-level AND parts as filters (one chip each), each written back as text (`parts`), and
    their AND as `filter`. A mistake is a 400 whose detail has `message` and `errors`:
    [{message, line, column, suggestions}]."""
    kinds = await field_kinds(id)
    concepts = await dataset_concepts(id)
    # The regions the text names after `within`, and those of the current filters (each read back as text below).
    written = region_names(request.query)
    for text in filter(None, (format_filter(node, concepts=concepts) for node in request.current)):
        written += [name for name in region_names(text) if name not in written]
    named = await regions.match_names(db, written)
    try:
        filters = parse_query(request.query, kinds, regions=named, concepts=concepts)
    except FilterQueryError as error:
        raise HTTPException(status_code=400, detail={'message': str(error), 'errors': [error.detail]})
    current = [_normalized(node, kinds, named, concepts) for node in request.current]
    reuse: List[Optional[int]] = []
    matched: set = set()
    for node in filters:
        match = next((i for i, known in enumerate(current) if i not in matched and known == node), None)
        reuse.append(match)
        if match is not None:
            matched.add(match)
    return {
        'filters': filters,
        'parts': [format_filter(node, concepts=concepts) for node in filters],
        'filter': {'logic': 'AND', 'filters': filters},
        'reuse': reuse,
    }


@router.post('/datasets/{id}/filter_query/format', dependencies=[Depends(require_dataset_access)])
async def format_filter_query(
    id: int,
    request: FilterQueryFormat,
    http_request: Request = None,  # type: ignore[assignment]  # FastAPI always passes it; a direct caller may not
):
    """Each filter as one part of a query (`parts`, null where the language can't express it, such as a region
    drawn on the map or a concept that no longer exists) and the expressible ones joined with AND (`query`).
    For a request made for the assistant, a private field's values are written as kept from it
    (backend/assistant/private.py): text is the one answer whose values no sealing can find."""
    concepts = await dataset_concepts(id)
    identity = scope_identity(http_request.scope) if http_request is not None else None
    # A private field's values written as kept from the assistant.
    kept = await private_fields(id) if identity is not None else frozenset()
    parts = [format_filter(node, concepts=concepts, kept=kept) for node in request.filters]
    return {'parts': parts, 'query': '\nAND '.join(part for part in parts if part is not None)}


def _normalized(
    node: Dict[str, Any],
    kinds: Dict[str, str],
    named: Mapping[str, RegionMatch],
    concepts: List[Concept],
) -> Optional[Dict[str, Any]]:
    """`node` as the parser writes it, so equal filters compare equal however they were built."""
    text = format_filter(node, concepts=concepts)
    if text is None:
        return None
    try:
        parts = parse_query(text, kinds, regions=named, concepts=concepts)
    except FilterQueryError:
        return None
    return parts[0] if len(parts) == 1 else None
