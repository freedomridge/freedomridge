"""The assistant's working views and value lists, for the person whose conversation made them (docs/ASSISTANT.md,
"Working views"): what the app needs to open a `?w=` link, and a value list's download.

Each is for the view's or the list's own user, signed in with the browser's session or an ordinary API token, and
reads the dataset as that person now (check_dataset_access). Each opens the tokens of private fields for the person,
who may see the values the assistant may not (private.open_filters). A request made for a tool is refused on every
/api/assistant/ route (auth._assistant_user), so no tool reaches these.
"""

import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from .. import field_kinds
from ..access import check_dataset_access
from ..assistant import private, store
from ..database import db
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

        async def count() -> Any:
            where, args, _ = build_sample_query_parts(kept['dataset_id'], filters_json(filters))
            return await run_aggregations(
                kept['dataset_id'],
                [AggregationSpec(field=kept['field_name'], op='count_values')],
                where,
                args,
            )

        # With the record of which kind of row has which field, as the aggregate route reads it (field_kinds.reading).
        counted = await field_kinds.reading(db, kept['dataset_id'], filters, count)
        entries = [[item['value'], item['count']] for item in counted['results'][0]['value']]
    # Written row by row as the csv module writes them (`\r\n` after each), each cell by _cell: every text in quotes, a
    # number bare, an empty value as an empty cell, the same as the Export dialog writes them.
    lines = [f'{_cell("value")},{_cell("count")}']
    for value, count in entries:
        ok, plain = seal.open(value, kept['field_name'])
        lines.append(f'{_cell(plain if ok else value)},{_cell(count)}')
    name = ''.join(char if char.isalnum() or char in '-_' else '_' for char in kept['field_name'])
    return Response(
        content=''.join(line + '\r\n' for line in lines),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{name}-values.csv"'},
    )


# A cell a spreadsheet would run as a formula starts with one of these signs, after any leading white space (some
# spreadsheets trim it, and some take the look-alikes too), or starts with a tab or a carriage return whatever follows.
# A spreadsheet reading the file with another separator (`;` where that is the regional list separator, or a tab)
# begins a new cell after each `;` and tab, and a new row after each line break, inside our quotes too, so each piece
# of the text between them is checked on its own. A text that is nothing but one number (`-5`, `+3.2`, `-1e-3`) is a
# number, not a formula. The Export dialog keeps the same rule (frontend/lib/csv.ts).
_FORMULA_SIGNS = (
    '=',
    '+',
    '-',
    '@',
    '\uff1d',  # their full-width forms
    '\uff0b',
    '\uff0d',
    '\uff20',
    '\u2212',  # the minus sign, and the small plus, hyphen and equals
    '\ufe62',
    '\ufe63',
    '\ufe66',
)
# Where one piece ends and the next begins: a `;`, a tab or a line break.
_PIECE_BREAK = re.compile('([;\t\n\r\u0085\u2028\u2029])')
# Skipped before the sign: a piece's leading white space, one explicit set (never the language's own trim; a tab or a
# line break ends a piece), and its leading double quotes.
_SKIPPED = ' \v\f\u00a0\u3000\u200b\ufeff"'
_ONE_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')


def _guarded(text: str) -> str:
    """`text` with an apostrophe in front of each piece that begins with a formula sign (unless every piece is one
    number or empty: `-5;3` is left alone, but `-5`, a line break, `*X(A1)` is one formula), and at the very start
    when it starts with a tab or a carriage return, whatever follows."""
    parts = _PIECE_BREAK.split(text)  # the pieces, with the break between each two
    numbers = all(part == '' or _ONE_NUMBER.fullmatch(part) for part in parts[::2])
    out = ["'" if text.startswith(('\t', '\r')) else '']
    for i, part in enumerate(parts):
        sign = i % 2 == 0 and not numbers and part.lstrip(_SKIPPED).startswith(_FORMULA_SIGNS)
        out.append("'" + part if sign else part)
    return ''.join(out)


def _cell(value: Any) -> str:
    """`value` as its CSV cell. A number is written as it is, and None or an empty text as an empty cell. Anything
    else is its text, guarded against formulas, and always in double quotes: a spreadsheet that reads the file with
    `;` takes a line with no `;` for one cell, with the comma as its decimal mark, so a number followed by an unquoted
    text could read as one formula."""
    if value is None:
        return ''
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    if text == '':
        return ''
    return '"' + _guarded(text).replace('"', '""') + '"'


def filters_json(filters: Any) -> Any:
    import json

    return json.dumps(filters) if filters else None
