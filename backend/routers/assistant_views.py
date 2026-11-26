"""The assistant's working views and value lists, for the person whose conversation made them (docs/ASSISTANT.md,
"Working views"): what the app needs to open a `?w=` link, and a value list's download.

Each is for the view's or the list's own user, signed in with the browser's session or an ordinary API token, and
reads the dataset as that person now (check_dataset_access). Each opens the tokens of private fields for the person,
who may see the values the assistant may not (private.open_filters). A request made for a tool is refused on every
/api/assistant/ route (auth._assistant_user), so no tool reaches these.
"""

import csv
import io
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ..access import check_dataset_access
from ..assistant import private, store
from ..filter_query import format_filter
from .auth import get_current_user

router = APIRouter(prefix='/assistant', tags=['assistant'])

LINK_GONE = (
    "This link's view doesn't exist or has expired: the assistant keeps the views it makes for 30 days after they were "
    'last used.'
)
LIST_GONE = (
    "This download's list doesn't exist or has expired: the assistant keeps the lists it makes for 30 days after they "
    'were last used.'
)
SOMEONE_ELSES = (
    "This link belongs to someone else's conversation with the assistant. Ask them to save a view and share that."
)


def _description(stage: dict[str, Any], node: dict[str, Any]) -> str:
    if stage['kind'] == 'text':
        return f'NOT ({stage["text"]})' if stage.get('negated') else stage['text']
    try:
        written = format_filter(node)
    except Exception:  # noqa: BLE001 - a stage the language can't write keeps its label
        written = None
    return written if written is not None and len(written) <= 200 else stage.get('label') or 'Filter'


def _negated(node: dict[str, Any]) -> dict[str, Any]:
    from ..assistant.working import _negated as negated

    return negated(node)


@router.get('/views/{handle}')
async def get_working_view(handle: str, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """A working view's stages as the app's filter bar keeps them ({type, description, filter}), with every value
    as the dataset holds it, for its own user."""
    view = await store.view_for_link(handle)
    if view is None:
        raise HTTPException(status_code=404, detail=LINK_GONE)
    if view['user_id'] != user['id']:
        raise HTTPException(status_code=404, detail=SOMEONE_ELSES)
    await check_dataset_access(view['dataset_id'], user, 'view')
    await store.touch_view(handle)
    seal = private.Seal(await private.server_key(), view['dataset_id'])
    lists = await store.list_values(view['lists'])
    stages = []
    for stage in view['stages']:
        if stage['kind'] == 'list':
            entries = (lists.get(stage['list']) or {}).get('entries') or []
            node = {
                'field': stage['field'],
                'operator': 'not in' if stage.get('exclude') else 'in',
                'value': [value for value, _ in entries],
            }
        else:
            node = _negated(stage['filter']) if stage.get('negated') else stage['filter']
        node = private.open_filters(node, seal)
        stages.append({'type': 'filter', 'description': _description(stage, node), 'filter': node})
    return {
        'dataset_id': view['dataset_id'],
        'stages': stages,
        'created_at': view['created_at'],
        'expires_at': view['used_at'] + timedelta(days=store.KEPT_DAYS),
    }


@router.get('/lists/{handle}/csv')
async def download_value_list(handle: str, user: dict = Depends(get_current_user)) -> Response:
    """A value list as CSV (`value,count`, one row per distinct value, however many), for its own user."""
    kept = await store.list_for_link(handle)
    if kept is None or kept['user_id'] != user['id']:
        raise HTTPException(status_code=404, detail=LIST_GONE)
    await check_dataset_access(kept['dataset_id'], user, 'view')
    await store.touch_list(handle)
    seal = private.Seal(await private.server_key(), kept['dataset_id'])
    if kept['entries'] is not None:
        entries = kept['entries']
    else:
        # More values than a list keeps: counted again, over the filter it was counted over, by the aggregate's own
        # code (the route the tool used).
        from .datasets import AggregationSpec, build_sample_query_parts, run_aggregations

        filters = private.open_filters((kept['source'] or {}).get('filters'), seal)
        where, args, _ = build_sample_query_parts(kept['dataset_id'], filters_json(filters))
        counted = await run_aggregations(
            kept['dataset_id'],
            [AggregationSpec(field=kept['field_name'], op='count_values')],
            where,
            args,
        )
        entries = [[item['value'], item['count']] for item in counted['results'][0]['value']]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['value', 'count'])
    # A cell a spreadsheet would run as a formula gets a leading apostrophe. Numbers are written as they are (a
    # negative number is a number, not a formula); a text value that starts with one of the signs is prefixed.
    formula = ('=', '+', '-', '@', '\t', '\r')
    for value, count in entries:
        ok, plain = seal.open(value, kept['field_name'])
        value = plain if ok else value
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if kept['value_kind'] == 'number' and isinstance(value, str):
            try:
                float(value)
                number = True
            except ValueError:
                number = False
        if not number and isinstance(value, str) and value.startswith(formula):
            value = "'" + value
        writer.writerow([value, count])
    name = ''.join(char if char.isalnum() or char in '-_' else '_' for char in kept['field_name'])
    return Response(
        content=out.getvalue(),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{name}-values.csv"'},
    )


def filters_json(filters: Any) -> Any:
    import json

    return json.dumps(filters) if filters else None
