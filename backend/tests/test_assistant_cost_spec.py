"""The assistant knows what is quick and what is slow, and its counting tools read a view once (milestone 1): the
`indexed` flag of describe_dataset's fields, the instructions' words about cost, the aggregate route answering the
operations of one field from one read (and its two new operations, `with_value` and `histogram`), a number summary
whose second read runs out of time, and what a `too_slow` refusal says.

Written from the contract (m1-contract-assistant-cost, standing on m1-contract-assistant-core and
m1-contract-working-views), not from the code. The tools are called where the doors call them,
`backend.assistant.call_tool`; the aggregate and histogram routes over HTTP with real API tokens (the access-rule
tests' World), and, for a request made for the assistant, through the in-process request the private-values tests
use. A field is indexed and its index removed through the app's own routes (POST .../fields/{field}/promote and
.../demote), each job run to its end the way the index tests run it (job_helpers.finish).

The aggregate route's rewrite is guarded by equivalence: a dataset with a field of every kind (text with ties in its
counts, a number with repeats and nulls, a number held partly as text that reads as a number and partly as text that
doesn't, a datetime, a boolean, a list kept in metadata, the built-in tags, a drawing's labels, a field on three
samples only, the built-in media_type and filepath, and a field no sample has), under four filters (none, one that
keeps a part, a range on a number, one that keeps nothing).

Where the contract is silent or unclear these tests read it this way:
- "Each existing operation asked alone gives what it gives today" is pinned by computing each answer in this file
  from the planted data (an oracle) and checking the route's answer against it. These assertions pass on the commit
  before the piece. The oracle orders text the way the database does (its collation, asked through
  `ORDER BY` on the same texts), as the route's "ties by value" and "in value order" do; count_values' ties put a
  value that reads as a number first, in its number's order. Floats of sum, mean and std are compared to 12
  significant digits, quantiles to 12 as well; everything else exactly.
- "Asked together or each alone, every operation's answer is the same" (3.1): every operation, asked in one request
  with others on the same field (all of them, in order, reversed and shuffled; and every pair), and with operations
  on another field or on every field, gives per operation exactly what it gives asked alone in the same run: the
  same keys and values in the same order. A mean, a sum and a standard deviation are compared to 12 significant
  digits (the contract's "last two digits"); everything else exactly.
- The equivalence fields include the built-in `id`, `created_at` and `last_modified_at` (whose values and numbers
  come from different places: today `count_values` and `distinct` of a timestamp read the metadata, so they are
  empty, while its numbers come from the column; that is pinned as it is), a segmentation mask's `labels` and a
  registered list field (`pests`) besides `tags`. The oracle takes each sample's id and timestamps from the server;
  floats from a timestamp column are compared to 12 digits, and a spread of instants to a microsecond.
- 3.9: text "too large to be a number" is text the route's number pattern matches but that is not a finite number
  (`0347677e05719710`, `1e999999`: both overflow every number type). It is counted and listed as the text it is;
  in count_values' ties it is ordered among the texts (after the values that read as numbers); number operations,
  the histogram included, pass over it like any other text. Checked on a text field and on a field typed number,
  through the route and through field_values and field_summary.
- 3.8: an unlimited list is pinned at a size a test can afford: 10,050 distinct values of 200 characters (more than
  a value list keeps), every one exactly, in order, through the live route, a version's route and the value list's
  CSV download after field_values.
- `with_value` answers like `count`: `{"op": "with_value", "field": f, "value": n}`, n the total that
  `POST .../samples/query` gives with `{"field": f, "operator": "exists"}` ANDed to the request's filter (and the
  same as the route's own `count` under that filter). `exists` counts a sample whose value is JSON null as having the
  field; so does `with_value`.
- `{"field": f, "op": "histogram", "bins": n}` answers its buckets as its `value`: either the list of buckets, or
  an object holding them under `buckets`. Either way the buckets are those of `POST .../histogram` with the same
  field, filter and `bins`, key for key, in the same order; when it is an object, the keys it shares with the
  route's answer have the route's values. On a field held only as JSON numbers that is checked directly.
- 3.3 on a field typed number that holds JSON numbers, numeric text and other text (made with the app's own
  `PATCH .../fields/{field}/type`): the histogram operation's buckets count exactly the values `bounds` counts,
  checked three ways: their counts add up to the number of values `bounds` counts (the oracle's), the first bucket
  starts at `bounds`' smallest and the last ends at its largest, and the buckets are the ones `POST .../histogram`
  gives for a twin field holding exactly those values as JSON numbers. `POST .../histogram` on the field itself is
  unchanged: it still counts only the JSON numbers.
- 3.2 says "the buckets POST .../histogram gives for the same field", not only for a number: a datetime field (which
  field_summary also bins, 4.3) is checked the same way, in a test of its own.
- An unknown operation is refused as today: 400 with the detail `Unknown aggregation op: '<op>'`, alone or after
  valid operations, on the live route and on a version's.
- "Asked for the assistant" (3.6) is a request made through the in-process door the private-values tests use
  (`inner.request` with the user's Identity and the seal key), on a field marked `values_private` through the
  field's description endpoint. `histogram` is "refused as bounds is": the same status (403) and the same refusal
  code as `bounds` of that field gets; the words may differ.
- Running into the statement limit is contract 7.3's way: a short statement limit on the pool, and a lock held on
  `samples` from another connection. For the tools of 5.3 the lock is held for the whole call, as in the spec tests.
  Elsewhere it is held only while chosen requests of the tool run, the tool's own requests watched at the app's ASGI
  stack (main.app, which every tool request goes through, where the test sees each request's method, path and body):
  - a counting tool's read is a request to the aggregate or histogram route; a number summary's first and second
    such requests are its first and second reads; the view's count is a samples query that counts (no
    `"skip_count": true`), made before them;
  - make_view's, refine_view's and get_samples' counts are samples queries that count;
  - 4.4 locks the second read on (every later read); 5.1 the first read only; 5.4's other half every count of the
    view; 5.2 every count; 5.5 every samples query that passes `"skip_count": true` (a page without a count, such as
    the tools' check for object rows). Every other request runs freely.
- "The fields the filter (or the sort) uses" (5.2) are those of the view's stages and the `sort_by` field, never the
  image-grain condition the tools add to every count themselves (`media_type != "object"`): `media_type` is never
  named in a message about a filter that didn't use it.
- The partial summary (4.4): `samples_with_value`, `samples_without`, `min`, `max`, `mean`, `field`, `type` and
  `unit` are what the whole summary gives; `quantiles`, `histogram` and `distinct` are present and null (a number's
  summary carries `distinct` today: pinned); `note` holds the view's count as a number and the word "narrow"; it has
  every key the whole summary has, and `note` besides.
- 4.1's correction, through field_summary, on `gain` (typed a number, some of its numbers held as text): the
  histogram's counts add up to the number of values the smallest, largest and quantiles count, from the smallest to
  the largest.
- 5.5: a statement that runs out of time in a samples query's page fetch (`"skip_count": true`, so the count isn't
  what runs out) answers exactly as the same route answers when its count runs out, status and body (an error id
  aside), for an administrator and a power user with real API tokens, on the slim page and on the page of whole
  rows, under the server's own statement limit. That is where the two differ today: the count's error reaches the
  app's own error handler (500 with an error id, which a tool calls too_slow), the page's is swallowed into 500
  "Internal query error" (which a tool reports as a server fault). A request with a statement limit of its own (a
  guest's in public mode, a tool's 45 seconds) is answered 504 by the request-limits middleware whichever statement
  ran out, so there the page already answers as the count does. And a tool whose skip-count samples query runs out
  of time answers `too_slow`, never a server fault (call_tool raising).
- A `too_slow` message is checked by its sense:
  - "advice to index" is the word "administrator" or the bare word "index", "indexes" or "indexing"; "indexed"
    alone ("whether or not the field is indexed", "an indexed field") is not advice;
  - from field_values and field_summary (5.1): the view's count as a number (make_view's count for a view, the
    dataset's for a whole dataset), "every" or "all" with "sample", "indexed" (indexed or not), "narrow", and no
    advice to index; when the count of the view is what ran out (5.4), the same without any number: no digit at all;
  - from make_view, refine_view and get_samples on fields not indexed (5.2), tested with `=`, `in` or a range, or
    sorted by: each such field's name, "narrow", "every" or "all", "administrator" and the bare word "index"; tested
    only with `contains`, `exists` or `not exists`: the field's name, "narrow", "indexed" (by an indexed field),
    "every" or "all", and no advice to index; with every field indexed: "narrow" and no advice to index;
  - from another tool (5.3): "narrow" and no advice to index;
  - no planted value of a private field anywhere, its name allowed.
- The instructions and the tool descriptions (section 2) are checked by their sense, sentence by sentence (a
  sentence ends at ". ", "! ", "? " or a line break):
  - the instructions contain the word "indexed";
  - a sentence, or two in a row, names both field_values and field_summary and says "every" or "all" with "sample";
  - the instructions name `too_slow`;
  - no sentence of the instructions or of any tool's description promises that indexing makes field_values or
    field_summary faster: none has a mention of one of them (their names, "summary"/"summaries", or "values" of a
    field: "field's values", "values of a field", "count the values"), the word "index..." and a word of speed
    ("faster", "quicker", "speeds up", "quick", "fast") without a word that negates or dismisses it ("not", "n't",
    "no", "never", "nothing", "neither", "nor", "whether", "regardless", "without").
- describe_dataset's description says what `indexed` means: a sentence with "indexed", "filter" and "quick" or
  "fast". A metadata field's `indexed` is the schema's `is_promoted`; the six built-in fields' booleans are not
  asserted, only that each is a boolean, the same on two datasets, and unchanged when a metadata field is indexed.
"""

import asyncio
import contextlib
import itertools
import json
import math
import os
import random
import re
from collections import Counter
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import asyncpg
import httpx
import pytest

from backend import index_actions, index_jobs, main
from backend.assistant import private, rule
from backend.tests import (
    job_helpers,
    test_access_rule_spec,
    test_assistant_views_spec,
    test_field_registry_postgres,
    test_upgrade_paths,
)
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_access_rule_spec import ADMIN, POWER, World
from backend.tests.test_assistant_core_spec import add, assistant, every_field, open_door
from backend.tests.test_assistant_private_values import _answer
from backend.tests.test_assistant_views_spec import (
    LIMIT_MS,
    SECRETS,
    V,
    call,
    csv_rows,
    everything,
    farm,
    make,
    result,
)

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
database = test_upgrade_paths.database
limited_world = test_assistant_views_spec.limited_world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

A, B, C, L = 950, 951, 952, 953  # the aggregate tests', describe_dataset's second, 3.9's and 3.8's datasets
COUNT = 24
BUILT_IN = ('id', 'filepath', 'created_at', 'last_modified_at', 'tags', 'media_type')
AGGREGATE = '/api/datasets/{id}/aggregate'
QUERY = '/api/datasets/{id}/samples/query'

# --- The aggregate tests' dataset ---

CROPS = ['corn'] * 5 + ['soy'] * 5 + ['rye'] * 3 + ['oat'] * 3 + ['10', '10', '9', '9', 'flax']  # ties in counts
NUMBERS = [1, 2, 2, 3, 3, 3, 2.5, -4, 10, 0.25, 7, 7, 1000, 2, -0.5, 3, 12, 12, 5, 6]  # repeats; then 2 nulls
# A number held partly as text that reads as a number, partly as text that doesn't; then a null.
GAINS: list[Any] = [1, 2.5, '12.5', '-3', 'abc', 'N/A', ' 4 ', '.5', '1e3', 7, 8, '1.2.3', 9, '20', -2, 'x7']
ANIMALS = ('cat', 'dog', 'cow')
BOX = [0.1, 0.1, 0.2, 0.2]
# What the route reads as a number (and what bounds, mean and quantiles count), and what count_values' ties read so.
NUMBER_TEXT = re.compile(r'^[-+]?\d+(\.\d+)?([eE][+-]?\d+)?$')
TIE_NUMBER = re.compile(r'^\s*[-+]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?\s*$')
DATETIMES = ('seen_at',)
NATIVE_TIMES = ('created_at', 'last_modified_at')  # columns of the sample, read as numbers from the column
PESTS = [['aphid'], ['aphid', 'mite'], ['beetle'], []]  # a registered list field's values
FIELDS = (
    'crop',
    'n',
    'gain',
    'seen_at',
    'flag',
    'labels',
    'tags',
    'detections.label',
    'sparse',
    'media_type',
    'filepath',
    'absent',
    # The built-in fields whose values and numbers come from different places, a mask's labels, a registered list.
    'id',
    'created_at',
    'last_modified_at',
    'segmentation.labels',
    'pests',
)


def sample_row(i: int) -> dict[str, Any]:
    """Sample i: its filepath, tags and metadata (a key left out: the sample has no such field)."""
    metadata: dict[str, Any] = {'group': 'b' if i % 3 == 0 else 'a'}
    if i < len(CROPS):
        metadata['crop'] = CROPS[i]
    elif i == len(CROPS):
        metadata['crop'] = None
    if i < len(NUMBERS):
        metadata['n'] = NUMBERS[i]
    elif i < len(NUMBERS) + 2:
        metadata['n'] = None
    if i < len(GAINS):
        metadata['gain'] = GAINS[i]
        if is_number_text(GAINS[i]):
            metadata['gain_numbers'] = float(GAINS[i]) if isinstance(GAINS[i], str) else GAINS[i]
    elif i == len(GAINS):
        metadata['gain'] = None
    if i < 14:  # sample 13 is the same instant as sample 6, written with an offset
        metadata['seen_at'] = '2026-03-07T10:00:00+02:00' if i == 13 else f'2026-03-{i % 7 + 1:02d}T08:00:00Z'
    elif i == 14:
        metadata['seen_at'] = None
    if i < 18:
        metadata['flag'] = i % 2 == 0
    elif i < 20:
        metadata['flag'] = None
    if i < 10:
        metadata['labels'] = ['weed'] if i % 2 else ['weed', 'stone']
    elif i == 10:
        metadata['labels'] = []
    if i < 15:
        metadata['detections'] = [{'label': ANIMALS[(i + k) % 3], 'bounding_box': BOX} for k in range(i % 3)]
    elif i < 18:
        metadata['detections'] = []
    if i in (5, 11, 17):
        metadata['sparse'] = 'here' if i == 11 else 'only'
    if i < 9:
        metadata['segmentation'] = {'maskPath': f'mask-{i}.png', 'labels': ['road', 'sky'] if i % 2 else ['road']}
    tags = [['dry', 'late'], ['dry'], ['dry'], []][i % 4]
    sample: dict[str, Any] = {
        'filepath': f'agg-{i:02d}.jpg',
        'tags': tags,
        'metadata': metadata,
        'created_at': f'2026-01-{i % 20 + 1:02d}T06:00:00Z',  # some the same
    }
    if i < 12:
        sample['array_fields'] = {'pests': PESTS[i % 4]}
    return sample


def finite(text: str) -> bool:
    """Text too large to be a number (`0347677e05719710`, `1e999999`) is text (contract 3.9)."""
    return math.isfinite(float(text.strip()))


def is_number_text(value: Any) -> bool:
    """A value the aggregate route reads as a number: a JSON number, or text it reads as one."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and NUMBER_TEXT.match(value) is not None and finite(value)


ROWS = [sample_row(i) for i in range(COUNT)]


async def plant(w: World, dataset_id: int = A) -> None:
    """ROWS as samples, `pests` a registered list field; `gain` declared a number with the app's own route (its
    values stay as they are)."""
    await w.dataset(dataset_id)
    registered = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/array-fields', json={'field_name': 'pests'})
    assert registered.is_success, registered.text
    await add(dataset_id, json.loads(json.dumps(ROWS)))
    typed = await w.call(
        ADMIN,
        'PATCH',
        f'/api/datasets/{dataset_id}/fields/gain/type',
        json={'field_type': 'number'},
    )
    assert typed.status_code == 200, typed.text
    await run_refresh(w.database, dataset_id)


FILTERS: dict[str, tuple[Optional[dict[str, Any]], Callable[[dict[str, Any]], bool]]] = {
    'everything': (None, lambda row: True),
    'group b': ({'field': 'group', 'operator': '=', 'value': 'b'}, lambda row: row['metadata']['group'] == 'b'),
    'n > 2': (
        {'field': 'n', 'operator': '>', 'value': 2},
        lambda row: is_number_text(row['metadata'].get('n')) and row['metadata']['n'] > 2,
    ),
    'nothing': ({'field': 'group', 'operator': '=', 'value': 'nowhere'}, lambda row: False),
}

# Every existing operation, with the parameters they take.
SPECS: list[dict[str, Any]] = [
    {'op': 'count'},
    {'op': 'sum'},
    {'op': 'mean'},
    {'op': 'std'},
    {'op': 'min'},
    {'op': 'max'},
    {'op': 'bounds'},
    {'op': 'quantiles'},
    {'op': 'quantiles', 'quantiles': [0.1, 0.5, 0.9]},
    {'op': 'quantiles', 'quantiles': [0.0, 1.0]},
    {'op': 'count_values'},
    {'op': 'count_values', 'limit': 2},
    {'op': 'distinct'},
    {'op': 'distinct', 'limit': 3},
    {'op': 'distinct_count'},
    {'op': 'values'},
    {'op': 'values', 'limit': 5},
]
FLOATING = ('sum', 'mean', 'std')  # added up in some order: compared to 12 significant digits


def spec_of(field: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {'field': field, **spec}


def key_of(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True)


async def aggregate(
    w: World,
    specs: list[dict[str, Any]],
    filters: Optional[dict[str, Any]] = None,
    dataset_id: int = A,
    version: Optional[int] = None,
) -> list[dict[str, Any]]:
    path = f'/api/datasets/{dataset_id}' + (f'/versions/{version}' if version is not None else '') + '/aggregate'
    body: dict[str, Any] = {'aggregations': specs}
    if filters is not None:
        body['filters'] = filters
    response = await w.call(ADMIN, 'POST', path, json=body)
    assert response.status_code == 200, (specs, filters, response.text)
    answered = response.json()
    assert set(answered) == {'results'} and len(answered['results']) == len(specs), answered
    return answered['results']


async def histogram_route(
    w: World,
    field: str,
    filters: Optional[dict[str, Any]],
    bins: int,
    dataset_id: int = A,
) -> dict[str, Any]:
    body: dict[str, Any] = {'field': field, 'bins': bins}
    if filters is not None:
        body['filters'] = filters
    response = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/histogram', json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def total(w: World, filters: Optional[dict[str, Any]], dataset_id: int = A) -> int:
    response = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/samples/query',
        json={'limit': 0, 'filters': filters},
    )
    assert response.status_code == 200, response.text
    return response.json()['total']


def both(first: Optional[dict[str, Any]], second: dict[str, Any]) -> dict[str, Any]:
    return second if first is None else {'logic': 'AND', 'filters': [first, second]}


def same(alone: dict[str, Any], together: dict[str, Any], where: Any) -> None:
    """One operation's answer asked together with others is what it is asked alone."""
    assert set(alone) == set(together), (where, alone, together)
    for key, value in alone.items():
        if alone['op'] in FLOATING and key == 'value' and value is not None and together[key] is not None:
            assert math.isclose(value, together[key], rel_tol=1e-12), (where, alone, together)
        else:
            assert together[key] == value, (where, alone, together)


# --- The oracle: what each operation answers today, from the planted data ---


def as_text(value: Any) -> Optional[str]:
    """A metadata value as the route reads it as text (`->>`)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return value
    return json.dumps(value)


def texts_of(row: dict[str, Any], field: str) -> list[str]:
    """The values a sample gives a field's count_values, distinct and values (one per element for a list column)."""
    if field == 'tags':
        return list(row['tags'])
    if field == 'detections.label':
        drawn = row['metadata'].get('detections')
        return [item['label'] for item in drawn] if isinstance(drawn, list) else []
    if field == 'media_type':
        return ['image']
    if field == 'filepath':
        return [row['filepath']]
    if field == 'id':
        return [str(row['id'])]
    if field == 'segmentation.labels':
        mask = row['metadata'].get('segmentation')
        return list(mask['labels']) if isinstance(mask, dict) else []
    if field == 'pests':
        return list(row.get('array_fields', {}).get('pests', []))
    text = as_text(row['metadata'].get(field))  # created_at and last_modified_at too: today they're read here
    return [] if text is None else [text]


def instant(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def numbers_of(row: dict[str, Any], field: str) -> list[float]:
    """The numbers a sample gives a field's numeric operations: a datetime's instant in seconds since 1970."""
    if field == 'id':
        return [float(row['id'])]
    if field in NATIVE_TIMES:
        return [] if row[field] is None else [row[field].timestamp()]
    if field in ('tags', 'detections.label', 'media_type', 'filepath', 'segmentation.labels', 'pests'):
        return []  # nothing of them is a metadata value that reads as a number
    text = as_text(row['metadata'].get(field))
    if text is None:
        return []
    if field in DATETIMES:
        return [instant(text)]
    return [float(text)] if NUMBER_TEXT.match(text) and finite(text) else []


def percentile(ordered: list[float], q: float) -> float:
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def expected(rows: list[dict[str, Any]], field: str, spec: dict[str, Any], rank: dict[str, int]) -> dict[str, Any]:
    """The operation's answer as the route gives it today, computed from the planted data."""
    op = spec['op']
    entry: dict[str, Any] = {'op': op, 'field': field}
    if op == 'count':
        entry['value'] = len(rows)
        return entry
    numbers = sorted(x for row in rows for x in numbers_of(row, field))
    if op == 'bounds':
        entry['value'] = [numbers[0], numbers[-1]] if numbers else [None, None]
    elif op in ('min', 'max'):
        entry['value'] = (numbers[0] if op == 'min' else numbers[-1]) if numbers else None
    elif op == 'sum':
        entry['value'] = math.fsum(numbers) if numbers else None
    elif op == 'mean':
        entry['value'] = math.fsum(numbers) / len(numbers) if numbers else None
    elif op == 'std':
        if len(numbers) < 2:
            entry['value'] = None
        else:
            mean = math.fsum(numbers) / len(numbers)
            entry['value'] = math.sqrt(math.fsum((x - mean) ** 2 for x in numbers) / (len(numbers) - 1))
    elif op == 'quantiles':
        qs = spec.get('quantiles') or [0.25, 0.5, 0.75]
        entry['quantiles'] = qs
        entry['value'] = [percentile(numbers, q) for q in qs] if numbers else []
    else:
        texts = [text for row in rows for text in texts_of(row, field)]
        limit = spec.get('limit')

        def in_order(found: list[str]) -> list[str]:
            if field in DATETIMES:
                return sorted(found, key=lambda text: (instant(text), rank[text]))
            return sorted(found, key=lambda text: rank[text])

        if op == 'count_values':
            counted = Counter(texts)

            def tie(text: str) -> tuple[Any, ...]:
                if TIE_NUMBER.match(text) and finite(text):
                    return (-counted[text], 0, Decimal(text.strip()), rank[text])
                return (-counted[text], 1, Decimal(0), rank[text])

            listed: list[Any] = [{'value': text, 'count': counted[text]} for text in sorted(counted, key=tie)]
        elif op == 'distinct_count':
            entry['value'] = len(set(texts))
            return entry
        elif op == 'distinct':
            listed = in_order(list(set(texts)))
        else:  # values: every value, in order
            listed = in_order(texts)
        entry['value'] = listed[:limit] if limit is not None else listed
    return entry


def close_to(answer: dict[str, Any], wanted: dict[str, Any], where: Any) -> None:
    """The route's answer is the oracle's: floats of sums, means, deviations and quantiles to 12 digits."""
    assert set(answer) == set(wanted), (where, answer, wanted)
    for key, value in wanted.items():
        given = answer[key]
        tolerant = wanted['op'] in (*FLOATING, 'quantiles') or (
            wanted['field'] in NATIVE_TIMES and wanted['op'] in ('bounds', 'min', 'max')  # a column's instant
        )
        if key == 'value' and tolerant and value is not None:
            values = value if isinstance(value, list) else [value]
            givens = given if isinstance(given, list) else [given]
            assert len(values) == len(givens), (where, answer, wanted)
            for one, other in zip(values, givens):
                if one is None:
                    assert other is None, (where, answer, wanted)
                else:
                    # The spread of instants (seconds near 1.8e9) carries their rounding: a microsecond.
                    spread = 1e-6 if wanted['op'] == 'std' and wanted['field'] in (*NATIVE_TIMES, *DATETIMES) else 1e-9
                    assert other is not None and math.isclose(one, other, rel_tol=1e-12, abs_tol=spread), (
                        where,
                        answer,
                    )
        else:
            assert given == value, (where, answer, wanted)


async def stored_rows(w: World, dataset_id: int = A) -> list[dict[str, Any]]:
    """ROWS with what the server gave each sample: its id, its created_at and last_modified_at."""
    stored = {
        row['filepath']: row
        for row in await w.database.fetch(
            'SELECT id, filepath, created_at, last_modified_at FROM samples WHERE dataset_id = $1',
            dataset_id,
        )
    }
    return [{**row, **{key: stored[row['filepath']][key] for key in ('id', *NATIVE_TIMES)}} for row in ROWS]


async def text_rank(w: World, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Every planted text's place in the database's own order of text (its collation), as the route sorts them."""
    texts = sorted({text for row in rows for field in FIELDS for text in texts_of(row, field)})
    ordered = await w.database.fetch('SELECT v FROM unnest($1::text[]) AS v ORDER BY v', texts)
    return {row['v']: place for place, row in enumerate(ordered)}


async def alone_answers(
    w: World,
    filters: Optional[dict[str, Any]],
    specs: list[dict[str, Any]],
    version: Optional[int] = None,
) -> dict[str, dict[str, Any]]:
    """Each operation asked alone, keyed by its request."""
    answers = {}
    for spec in specs:
        (answer,) = await aggregate(w, [spec], filters, version=version)
        answers[key_of(spec)] = answer
    return answers


def every_spec(fields: tuple[str, ...] = FIELDS) -> list[dict[str, Any]]:
    return [spec_of(field, spec) for field in fields for spec in SPECS]


async def check_together(
    w: World,
    filters: Optional[dict[str, Any]],
    specs: list[dict[str, Any]],
    alone: dict[str, dict[str, Any]],
    version: Optional[int] = None,
) -> None:
    together = await aggregate(w, specs, filters, version=version)
    for spec, answer in zip(specs, together):
        same(alone[key_of(spec)], answer, (filters, spec))


# --- 1. describe_dataset says which fields are indexed ---


async def index_field(w: World, dataset_id: int, field: str) -> None:
    """Index a field through the app's route, its index job run to the end."""
    asked = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/fields/{field}/promote', params={'wait': 'false'})
    assert asked.status_code in (200, 202), asked.text
    assert (await job_helpers.finish(w.database, index_jobs.FIELD_KIND))['state'] == 'succeeded'


async def unindex_field(w: World, dataset_id: int, field: str) -> None:
    asked = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/fields/{field}/demote', params={'wait': 'false'})
    assert asked.status_code in (200, 202), asked.text
    assert (await job_helpers.finish(w.database, index_actions.DROP_KIND))['state'] == 'succeeded'


async def indexed_flags(w: World, dataset_id: int) -> dict[str, bool]:
    """Each field's `indexed`, checked a boolean, and the schema's is_promoted where the schema gives it."""
    fields = await every_field(ADMIN, dataset=dataset_id)
    flags = {}
    for field in fields:
        assert isinstance(field.get('indexed'), bool), field
        flags[field['name']] = field['indexed']
    schema = (await w.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/schema')).json()
    for entry in schema:
        if 'is_promoted' in entry:
            assert flags[entry['name']] is bool(entry['is_promoted']), (entry, flags[entry['name']])
    assert set(BUILT_IN) <= set(flags), flags
    return flags


def test_every_field_says_whether_it_is_indexed_as_the_server_keeps_it(world):
    async def scenario(w):
        await plant(w, A)
        await w.dataset(B)
        await add(B, [{'filepath': f'b-{i}.jpg', 'metadata': {'height': i, 'kind': 'b'}} for i in range(4)])
        await open_door(w)
        before, other = await indexed_flags(w, A), await indexed_flags(w, B)
        assert before['n'] is False and before['crop'] is False, before
        built_in = {name: before[name] for name in BUILT_IN}
        assert {name: other[name] for name in BUILT_IN} == built_in  # the same on any dataset
        await index_field(w, A, 'n')  # the statistics aren't counted again: it is what the server keeps
        indexed = await indexed_flags(w, A)
        assert indexed['n'] is True, indexed
        assert {name: flag for name, flag in indexed.items() if name != 'n'} == {
            name: flag for name, flag in before.items() if name != 'n'
        }
        assert await indexed_flags(w, B) == other
        await unindex_field(w, A, 'n')
        assert await indexed_flags(w, A) == before

    world(scenario)


def sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r'(?<=[.!?])\s+|\n+', text) if part.strip()]


def test_describe_datasets_description_says_what_indexed_means():
    described = {tool.name: tool for tool in assistant.tools()}['describe_dataset'].description
    assert any(
        'indexed' in sentence.lower()
        and 'filter' in sentence.lower()
        and re.search(r'\b(quick|fast)', sentence.lower())
        for sentence in sentences(described)
    ), described


# --- 2. The instructions say what is quick and what is slow ---

NAMES_A_COUNTING_TOOL = re.compile(
    r"field_values|field_summary|\bsummar(y|ies)\b|field'?s values|values of (a|the|one|each) field|count(ing)? "
    r'(the |its )?values',
    re.IGNORECASE,
)
SPEED = re.compile(r'\b(faster|quicker|speeds? up|quick|quickly|fast)\b', re.IGNORECASE)
DISMISSED = re.compile(r"\b(not|no|never|nothing|neither|nor|whether|regardless|without)\b|n't", re.IGNORECASE)


def promises_indexing_speeds_up_counting(sentence: str) -> bool:
    return bool(
        NAMES_A_COUNTING_TOOL.search(sentence)
        and re.search(r'\bindex', sentence, re.IGNORECASE)
        and SPEED.search(sentence)
        and not DISMISSED.search(sentence),
    )


def test_the_instructions_say_what_is_quick_and_what_is_slow():
    words = assistant.instructions()
    assert 'indexed' in words.lower(), words
    parts = sentences(words)
    windows = [' '.join(parts[i : i + 2]) for i in range(len(parts))]
    assert any(
        'field_values' in window
        and 'field_summary' in window
        and re.search(r'\b(every|all)\b', window.lower())
        and 'sample' in window.lower()
        for window in windows
    ), words
    assert 'too_slow' in words, words


def test_nothing_says_indexing_makes_the_counting_tools_faster():
    for source, text in [('instructions', assistant.instructions())] + [
        (tool.name, tool.description) for tool in assistant.tools()
    ]:
        promised = [sentence for sentence in sentences(text) if promises_indexing_speeds_up_counting(sentence)]
        assert not promised, (source, promised)


# --- 3. The aggregate route ---


def test_each_operation_asked_alone_answers_what_it_answers_today(world):
    async def scenario(w):
        await plant(w)
        planted = await stored_rows(w)
        rank = await text_rank(w, planted)
        for name, (filters, keep) in FILTERS.items():
            rows = [row for row in planted if keep(row)]
            for spec in every_spec():
                (answer,) = await aggregate(w, [spec], filters)
                close_to(answer, expected(rows, spec['field'], spec, rank), (name, spec))

    world(scenario)


def test_operations_on_one_field_asked_together_answer_what_each_answers_alone(world):
    async def scenario(w):
        await plant(w)
        shuffled = random.Random(20261119)
        for name, (filters, _) in FILTERS.items():
            specs = every_spec()
            alone = await alone_answers(w, filters, specs)
            for field in FIELDS:
                mine = [spec_of(field, spec) for spec in SPECS]
                orders = [mine, mine[::-1], shuffled.sample(mine, len(mine))]
                # The same operation twice, with different parameters, in one request.
                orders.append([spec_of(field, {'op': 'count_values', 'limit': 2}), *mine])
                for order in orders:
                    await check_together(w, filters, order, alone)
                if name in ('everything', 'group b'):
                    for pair in itertools.combinations(mine, 2):
                        await check_together(w, filters, list(pair), alone)

    world(scenario)


def test_operations_on_two_fields_or_on_every_field_in_one_request_answer_what_each_answers_alone(world):
    async def scenario(w):
        await plant(w)
        shuffled = random.Random(1119)
        for filters, _ in FILTERS.values():
            specs = every_spec()
            alone = await alone_answers(w, filters, specs)
            for first, second in itertools.combinations(FIELDS, 2):
                interleaved = [
                    spec
                    for pair in zip(every_spec((first,)), every_spec((second,)))
                    for spec in pair  # first's and second's operations taking turns
                ]
                await check_together(w, filters, interleaved, alone)
            await check_together(w, filters, shuffled.sample(specs, len(specs)), alone)

    world(scenario)


def test_with_value_is_the_count_the_exists_filter_gives(world):
    async def scenario(w):
        await plant(w)
        for name, (filters, _) in FILTERS.items():
            for field in FIELDS:
                having = both(filters, {'field': field, 'operator': 'exists'})
                wanted = await total(w, having)
                (counted,) = await aggregate(w, [{'field': field, 'op': 'count'}], having)
                assert counted['value'] == wanted, (name, field, counted)  # the route's own count agrees
                (answer,) = await aggregate(w, [{'field': field, 'op': 'with_value'}], filters)
                assert answer == {'op': 'with_value', 'field': field, 'value': wanted}, (name, field, answer)

    world(scenario)


def buckets_of(entry: dict[str, Any]) -> list[dict[str, Any]]:
    value = entry['value']
    if isinstance(value, dict):
        assert isinstance(value.get('buckets'), list), entry
        return value['buckets']
    assert isinstance(value, list), entry
    return value


def check_histogram_entry(entry: dict[str, Any], field: str, route: dict[str, Any], where: Any) -> None:
    assert (entry['op'], entry['field']) == ('histogram', field), (where, entry)
    assert buckets_of(entry) == route['buckets'], (where, entry, route)
    if isinstance(entry['value'], dict):
        shared = set(entry['value']) & set(route) - {'buckets'}
        assert {key: entry['value'][key] for key in shared} == {key: route[key] for key in shared}, (where, entry)


BINS = (1, 3, 7, 20)


def test_the_histogram_operation_gives_the_histogram_routes_buckets_for_a_field_of_numbers(world):
    async def scenario(w):
        await plant(w)
        for name, (filters, _) in FILTERS.items():
            for bins in BINS:
                route = await histogram_route(w, 'n', filters, bins)
                (entry,) = await aggregate(w, [{'field': 'n', 'op': 'histogram', 'bins': bins}], filters)
                check_histogram_entry(entry, 'n', route, (name, bins))

    world(scenario)


def test_the_histogram_operation_of_a_datetime_gives_the_histogram_routes_buckets(world):
    """3.2 names no kind of field: a datetime's buckets (which field_summary bins too) are the route's."""

    async def scenario(w):
        await plant(w)
        for name, (filters, _) in FILTERS.items():
            for bins in BINS[:3]:
                route = await histogram_route(w, 'seen_at', filters, bins)
                (entry,) = await aggregate(w, [{'field': 'seen_at', 'op': 'histogram', 'bins': bins}], filters)
                check_histogram_entry(entry, 'seen_at', route, (name, bins))

    world(scenario)


def test_the_histogram_operation_counts_the_values_bounds_counts(world):
    """3.3, on `gain`: typed a number, holding JSON numbers, text that reads as a number and text that doesn't."""

    async def scenario(w):
        await plant(w)
        kind = await w.database.fetchval(
            "SELECT field_type FROM dataset_fields WHERE dataset_id = $1 AND field_name = 'gain'",
            A,
        )
        assert kind == 'number', kind
        for name, (filters, keep) in FILTERS.items():
            counted = sorted(x for row in ROWS if keep(row) for x in numbers_of(row, 'gain'))
            (bounds,) = await aggregate(w, [{'field': 'gain', 'op': 'bounds'}], filters)
            assert bounds['value'] == ([counted[0], counted[-1]] if counted else [None, None]), (name, bounds)
            for bins in BINS:
                (entry,) = await aggregate(w, [{'field': 'gain', 'op': 'histogram', 'bins': bins}], filters)
                buckets = buckets_of(entry)
                assert sum(bucket['count'] for bucket in buckets) == len(counted), (name, bins, buckets)
                if counted:
                    assert buckets[0]['range']['min'] == counted[0] and buckets[-1]['range']['max'] == counted[-1]
                twin = await histogram_route(w, 'gain_numbers', filters, bins)
                assert buckets == twin['buckets'], (name, bins, buckets, twin)

    world(scenario)


def test_the_histogram_route_itself_still_counts_only_json_numbers(world):
    async def scenario(w):
        await plant(w)
        for name, (filters, keep) in FILTERS.items():
            json_numbers = [
                row['metadata']['gain']
                for row in ROWS
                if keep(row)
                and isinstance(row['metadata'].get('gain'), (int, float))
                and not isinstance(row['metadata'].get('gain'), bool)
            ]
            for bins in BINS:
                route = await histogram_route(w, 'gain', filters, bins)
                assert sum(bucket['count'] for bucket in route['buckets']) == len(json_numbers), (name, bins, route)
                if json_numbers:
                    assert route['buckets'][0]['range']['min'] == min(json_numbers), (name, bins, route)

    world(scenario)


def test_the_new_operations_ride_in_one_request_with_the_others(world):
    async def scenario(w):
        await plant(w)
        shuffled = random.Random(19)
        for filters, _ in FILTERS.values():
            new = [spec_of(field, {'op': 'with_value'}) for field in FIELDS]
            binned = ('n', 'gain', 'seen_at', 'id', 'created_at', 'last_modified_at')
            new += [spec_of(field, {'op': 'histogram', 'bins': 5}) for field in binned]
            specs = every_spec() + new
            alone = await alone_answers(w, filters, specs)
            for field in FIELDS:
                mine = [spec for spec in specs if spec['field'] == field]
                await check_together(w, filters, mine, alone)
                await check_together(w, filters, mine[::-1], alone)
            await check_together(w, filters, shuffled.sample(specs, len(specs)), alone)

    world(scenario)


def test_a_versions_aggregate_route_answers_as_before(world):
    async def scenario(w):
        await plant(w)
        planted = await stored_rows(w)
        rank = await text_rank(w, planted)
        made = await w.call(ADMIN, 'POST', f'/api/datasets/{A}/versions', json={'name': 'v1'})
        assert made.status_code == 200, made.text
        version = made.json()['id']
        for name, (filters, keep) in FILTERS.items():
            rows = [row for row in planted if keep(row)]
            alone = await alone_answers(w, filters, every_spec(), version=version)
            for spec in every_spec():
                close_to(alone[key_of(spec)], expected(rows, spec['field'], spec, rank), (name, spec))
            for field in FIELDS:
                await check_together(w, filters, every_spec((field,)), alone, version=version)

    world(scenario)


def test_an_unknown_operation_is_still_refused_as_today(world):
    async def scenario(w):
        await plant(w)
        made = await w.call(ADMIN, 'POST', f'/api/datasets/{A}/versions', json={'name': 'v1'})
        assert made.status_code == 200, made.text
        for path in (f'/api/datasets/{A}/aggregate', f'/api/datasets/{A}/versions/{made.json()["id"]}/aggregate'):
            for op in ('nope', 'Histogram', 'with-value', 'median'):
                for specs in (
                    [{'field': 'n', 'op': op}],
                    [{'field': 'n', 'op': 'count'}, {'field': 'crop', 'op': 'count_values'}, {'field': 'n', 'op': op}],
                ):
                    refused = await w.call(ADMIN, 'POST', path, json={'aggregations': specs})
                    assert refused.status_code == 400, (path, specs, refused.text)
                    assert refused.json() == {'detail': f'Unknown aggregation op: {op!r}'}, (path, specs, refused.text)

    world(scenario)


# --- 3.9 Text that looks like a number but is too large to be one is text ---

HUGE = ('0347677e05719710', '1e999999')  # a hex id, and a number too large for any number type
HEXES = [HUGE[0], HUGE[1], HUGE[0], 'abc', '12', '7.5', '12', 'zz', HUGE[0], HUGE[1], 'abc', '12']
READINGS: list[Any] = [1.5, 2, HUGE[0], 3, HUGE[1], 4.25, '8', 'n/a', 6, HUGE[0], -2.5, 10]  # typed a number
HEX_ROWS = [
    {
        'filepath': f'hex-{i:02d}.jpg',
        'tags': [],
        'metadata': {'group': 'b' if i % 3 == 0 else 'a', 'hexid': HEXES[i], 'reading': READINGS[i]},
    }
    for i in range(len(HEXES))
]
HEX_FIELDS = ('hexid', 'reading')
VALUE_OPS = ('count_values', 'distinct', 'distinct_count', 'values')


async def plant_hexes(w: World) -> dict[str, int]:
    """HEX_ROWS as samples of dataset C, `reading` declared a number with the app's own route; the database's order
    of their texts."""
    await w.dataset(C)
    await add(C, json.loads(json.dumps(HEX_ROWS)))
    typed = await w.call(ADMIN, 'PATCH', f'/api/datasets/{C}/fields/reading/type', json={'field_type': 'number'})
    assert typed.status_code == 200, typed.text
    texts = sorted({text for row in HEX_ROWS for field in HEX_FIELDS for text in texts_of(row, field)})
    ordered = await w.database.fetch('SELECT v FROM unnest($1::text[]) AS v ORDER BY v', texts)
    return {row['v']: place for place, row in enumerate(ordered)}


def test_text_too_large_to_be_a_number_is_counted_and_listed_as_text(world):
    async def scenario(w):
        rank = await plant_hexes(w)
        for name in ('everything', 'group b'):
            filters, keep = FILTERS[name]
            rows = [row for row in HEX_ROWS if keep(row)]
            specs = [spec_of(field, spec) for field in HEX_FIELDS for spec in SPECS if spec['op'] in VALUE_OPS]
            for spec in specs:
                (answer,) = await aggregate(w, [spec], filters, dataset_id=C)
                close_to(answer, expected(rows, spec['field'], spec, rank), (name, spec))
            listed = await aggregate(w, specs, filters, dataset_id=C)
            for spec, answer in zip(specs, listed):
                close_to(answer, expected(rows, spec['field'], spec, rank), (name, 'together', spec))

    world(scenario)


def test_number_operations_pass_over_text_too_large_to_be_a_number(world):
    async def scenario(w):
        rank = await plant_hexes(w)
        for name in ('everything', 'group b'):
            filters, keep = FILTERS[name]
            rows = [row for row in HEX_ROWS if keep(row)]
            specs = [spec_of(field, spec) for field in HEX_FIELDS for spec in SPECS if spec['op'] not in VALUE_OPS]
            for spec in specs:
                (answer,) = await aggregate(w, [spec], filters, dataset_id=C)
                close_to(answer, expected(rows, spec['field'], spec, rank), (name, spec))
            numbers = sorted(x for row in rows for x in numbers_of(row, 'reading'))
            (entry,) = await aggregate(w, [{'field': 'reading', 'op': 'histogram', 'bins': 4}], filters, dataset_id=C)
            buckets = buckets_of(entry)
            assert sum(bucket['count'] for bucket in buckets) == len(numbers), (name, buckets)
            assert buckets[0]['range']['min'] == numbers[0] and buckets[-1]['range']['max'] == numbers[-1], buckets

    world(scenario)


def test_the_counting_tools_answer_on_text_too_large_to_be_a_number(world):
    async def scenario(w):
        rank = await plant_hexes(w)
        await open_door(w)
        counted = expected(HEX_ROWS, 'hexid', {'op': 'count_values'}, rank)['value']
        values = await result(ADMIN, 'field_values', {'dataset': C, 'field': 'hexid', 'limit': 50})
        assert sorted(map(json.dumps, values['values'])) == sorted(map(json.dumps, counted)), values
        assert [item['count'] for item in values['values']] == [item['count'] for item in counted], values
        assert (values['distinct'], values['samples_with_value']) == (len(counted), len(HEX_ROWS)), values
        summary = await result(ADMIN, 'field_summary', {'dataset': C, 'field': 'hexid'})
        assert (summary['distinct'], summary['samples_with_value'], summary['samples_without']) == (
            len(counted),
            len(HEX_ROWS),
            0,
        ), summary
        assert [item['count'] for item in summary['top']] == [item['count'] for item in counted[:5]], summary
        assert all(item in counted for item in summary['top']), summary
        numbers = sorted(x for row in HEX_ROWS for x in numbers_of(row, 'reading'))
        summary = await result(ADMIN, 'field_summary', {'dataset': C, 'field': 'reading'})
        assert (summary['min'], summary['max']) == (numbers[0], numbers[-1]), summary
        assert math.isclose(summary['mean'], math.fsum(numbers) / len(numbers), rel_tol=1e-12), summary
        assert sum(item['count'] for item in summary['histogram']) == len(numbers), summary

    world(scenario)


# --- 3.8 A list answer has no ceiling of the server's own ---

LONG_COUNT = 10_050  # more distinct values than a value list keeps (10,000), each 200 characters
LONG_VALUES = [f'{i:05d}-' + 'w' * 194 for i in range(LONG_COUNT)]
TWICE = 50  # the first ones are held by two samples each


async def plant_long(w: World) -> list[str]:
    """Dataset L: a field `long` with LONG_COUNT distinct values; the database's order of them."""
    await w.dataset(L)
    held = LONG_VALUES + LONG_VALUES[:TWICE]
    for start in range(0, len(held), 2500):
        chunk = held[start : start + 2500]
        samples = [
            {'filepath': f'long-{start + i:05d}.jpg', 'metadata': {'long': value}} for i, value in enumerate(chunk)
        ]
        await add(L, samples)
    ordered = await w.database.fetch('SELECT v FROM unnest($1::text[]) AS v ORDER BY v', LONG_VALUES)
    return [row['v'] for row in ordered]


def test_a_list_answer_without_a_limit_has_every_value(world):
    async def scenario(w):
        in_order = await plant_long(w)
        twice = set(LONG_VALUES[:TWICE])
        wanted: dict[str, list[Any]] = {
            'count_values': [{'value': v, 'count': 2} for v in in_order if v in twice]
            + [{'value': v, 'count': 1} for v in in_order if v not in twice],
            'distinct': in_order,
            'values': [v for value in in_order for v in ([value, value] if value in twice else [value])],
        }
        made = await w.call(ADMIN, 'POST', f'/api/datasets/{L}/versions', json={'name': 'v1'})
        assert made.status_code == 200, made.text
        for version in (None, made.json()['id']):
            for op, listed in wanted.items():
                (answer,) = await aggregate(w, [{'field': 'long', 'op': op}], dataset_id=L, version=version)
                assert answer['value'] == listed, (version, op, len(answer['value']), len(listed))
        # The value list's download: too many values to keep, so it counts them again when opened.
        await open_door(w)
        found = await result(ADMIN, 'field_values', {'dataset': L, 'field': 'long'})
        assert (found['list'], found['distinct']) == (None, LONG_COUNT), {k: found[k] for k in ('list', 'distinct')}
        download = csv_rows(await w.call(ADMIN, 'GET', urlsplit(found['download']).path))
        assert sorted(download) == sorted((item['value'], item['count']) for item in wanted['count_values'])

    world(scenario)


# --- 3.6 A private field, asked for the assistant ---

PRIVATE_FIELDS = ('crop', 'n', 'labels', 'detections', 'tags')
ASKED_PRIVATE = (*PRIVATE_FIELDS, 'detections.label')
# The private fields' planted values: each as a whole value of an answer, and the words among them inside its text.
PRIVATE_TEXTS = {
    text for row in ROWS for field in (*PRIVATE_FIELDS, 'detections.label') for text in texts_of(row, field)
} | {'weed', 'stone'}
PRIVATE_WORDS = re.compile(r'\b(corn|soy|rye|oat|flax|weed|stone|cat|dog|cow|dry|late)\b')
PRIVATE_NUMBERS = {1000, 0.25, -0.5, 2.5, -4}  # n's values that no count of 24 samples can be


async def plant_private(w: World) -> bytes:
    await plant(w)
    for field in PRIVATE_FIELDS:
        marked = await w.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{A}/fields/{field}/description',
            json={'values_private': True},
        )
        assert marked.status_code == 200, marked.text
    await open_door(w)
    seal_key = (await rule.verdict(ADMIN, 'mcp')).seal_key
    assert seal_key is not None
    return seal_key


def leaked(answer: Any) -> list[Any]:
    """A private field's planted values in an answer: a whole value, a number, or a word inside a text. A sealed
    token is a value kept back, whatever letters it happens to hold."""
    found: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and not private.is_sealed(node):
            if node in PRIVATE_TEXTS or PRIVATE_WORDS.search(node):
                found.append(node)
        elif isinstance(node, (int, float)) and not isinstance(node, bool) and node in PRIVATE_NUMBERS:
            found.append(node)

    walk(answer)
    return found


def test_with_value_counts_a_private_field_for_the_assistant(world):
    async def scenario(w):
        seal_key = await plant_private(w)
        for filters in (
            None,
            {'field': 'group', 'operator': '=', 'value': 'b'},
            {'field': 'crop', 'operator': 'exists'},
            {'field': 'gain', 'operator': '>', 'value': 2},
        ):
            for field in ASKED_PRIVATE:
                having = both(filters, {'field': field, 'operator': 'exists'})
                counted = await _answer(seal_key, 'POST', QUERY, A, {'filters': having, 'limit': 0})
                assert counted.status == 200, counted.body
                assert counted.body['total'] == await total(w, having)  # what the person counts
                specs = [{'field': field, 'op': 'with_value'}, {'field': field, 'op': 'count_values'}]
                body: dict[str, Any] = {'aggregations': specs}
                if filters is not None:
                    body['filters'] = filters
                answer = await _answer(seal_key, 'POST', AGGREGATE, A, body)
                assert answer.status == 200, (field, filters, answer.status, answer.body)
                assert answer.body['results'][0] == {'op': 'with_value', 'field': field, 'value': counted.body['total']}
                assert not leaked(answer.body), (field, leaked(answer.body))
        # A field that isn't private is binned for the assistant as for a person.
        answer = await _answer(
            seal_key,
            'POST',
            AGGREGATE,
            A,
            {'aggregations': [{'field': 'gain_numbers', 'op': 'histogram', 'bins': 4}]},
        )
        assert answer.status == 200, answer.body
        assert buckets_of(answer.body['results'][0]) == (await histogram_route(w, 'gain_numbers', None, 4))['buckets']

    world(scenario)


def test_a_histogram_of_a_private_field_is_refused_for_the_assistant_as_bounds_is(world):
    async def scenario(w):
        seal_key = await plant_private(w)
        for field in ASKED_PRIVATE:
            bounds = await _answer(seal_key, 'POST', AGGREGATE, A, {'aggregations': [{'field': field, 'op': 'bounds'}]})
            assert bounds.status == 403, (field, bounds.body)
            for specs in (
                [{'field': field, 'op': 'histogram', 'bins': 5}],
                [{'field': field, 'op': 'with_value'}, {'field': field, 'op': 'histogram', 'bins': 3}],
                [{'field': 'gain', 'op': 'count'}, {'field': field, 'op': 'histogram', 'bins': 20}],
            ):
                answer = await _answer(seal_key, 'POST', AGGREGATE, A, {'aggregations': specs})
                assert answer.status == bounds.status, (field, specs, answer.status, answer.body)
                assert answer.body['detail']['code'] == bounds.body['detail']['code'], (field, answer.body)
                assert 'results' not in answer.body and not leaked(answer.body), (field, answer.body)

    world(scenario)


# --- Running into the statement limit (the spec tests' way: a short limit on the pool, a lock held on samples) ---

INDEX_ADVICE = re.compile(r'\badministrator|\bindex(es|ing)?\b', re.IGNORECASE)
READS = ('/aggregate', '/histogram')


def too_slow(name: str, arguments: dict[str, Any], answer: dict[str, Any]) -> str:
    assert not answer['ok'], (name, arguments, answer)
    refusal = answer['error']
    assert (refusal['code'], refusal['retryable']) == ('too_slow', False), (name, arguments, refusal)
    assert not [secret for secret in SECRETS if secret in refusal['message']], refusal
    return refusal['message']


async def slow(url: str, name: str, arguments: dict[str, Any]) -> str:
    """The tool's too_slow message, the samples locked for the whole call (the spec tests' way)."""
    holder = await asyncpg.connect(url)
    try:
        async with holder.transaction():
            await holder.execute('LOCK TABLE samples IN ACCESS EXCLUSIVE MODE')
            answer = await asyncio.wait_for(call(ADMIN, name, arguments), timeout=8 * LIMIT_MS / 1000)
    finally:
        await holder.close()
    return too_slow(name, arguments, answer)


class Reads:
    def __init__(self) -> None:
        self.made = 0  # reads the tool made
        self.locked = 0  # of which ran with samples locked


@contextlib.asynccontextmanager
async def locking_reads(
    url: str,
    locked: Callable[[int], bool],
    paths: tuple[str, ...] = READS,
    pages: bool = False,
) -> AsyncIterator[Reads]:
    """While open, each read a tool makes is numbered 1, 2, ...: a POST to a route ending in one of `paths` (by default
    the aggregate and histogram routes) that counts, i.e. doesn't pass `"skip_count": true` (the samples query's own
    flag: a page without a count); with `pages`, instead, one that does pass it. While a read that `locked(number)`
    picks runs, samples is locked, so it runs into the statement limit. Every other request runs freely. The requests
    are watched at the app's ASGI stack, which every tool request goes through (contract 7.3)."""
    stack = main.app.middleware_stack or main.app.build_middleware_stack()
    holder = await asyncpg.connect(url)
    one_at_a_time = asyncio.Lock()
    reads = Reads()

    async def watching(scope: Any, receive: Any, send: Any) -> None:
        if not (scope['type'] == 'http' and scope['method'] == 'POST' and scope['path'].endswith(paths)):
            await stack(scope, receive, send)
            return
        received = []  # the body, read here and given to the route as it came
        while True:
            message = await receive()
            received.append(message)
            if message['type'] != 'http.request' or not message.get('more_body'):
                break
        body = b''.join(message.get('body', b'') for message in received)
        unread = iter(received)

        async def again() -> Any:
            return next(unread, None) or await receive()

        try:
            asked = json.loads(body or b'{}')
        except ValueError:
            asked = {}
        if (isinstance(asked, dict) and asked.get('skip_count') is True) is not pages:
            await stack(scope, again, send)
            return
        reads.made += 1
        if not locked(reads.made):
            await stack(scope, again, send)
            return
        async with one_at_a_time, holder.transaction():
            await holder.execute('LOCK TABLE samples IN ACCESS EXCLUSIVE MODE')
            reads.locked += 1
            await stack(scope, again, send)

    main.app.middleware_stack = watching
    try:
        yield reads
    finally:
        main.app.middleware_stack = stack
        await holder.close()


async def slow_first_read(url: str, name: str, arguments: dict[str, Any]) -> str:
    """The counting tool's too_slow message when its first read of the samples runs into the statement limit, and
    nothing else does (so the view can still be counted, before or after, or its count kept)."""
    async with locking_reads(url, lambda number: number == 1) as reads:
        answer = await asyncio.wait_for(call(ADMIN, name, arguments), timeout=8 * LIMIT_MS / 1000)
    assert reads.locked == 1, (name, arguments, 'the tool made no read through the aggregate or histogram route')
    return too_slow(name, arguments, answer)


async def slow_count(url: str, name: str, arguments: dict[str, Any]) -> str:
    """The tool's too_slow message when its counts (samples queries that count: make_view's, refine_view's and
    get_samples' count over a filter; a counting tool's count of the view, made before its reads) run into the
    statement limit, and nothing else does (so the tool can still read the dataset, its schema and the filter's
    reading)."""
    async with locking_reads(url, lambda number: True, paths=('/samples/query',)) as reads:
        answer = await asyncio.wait_for(call(ADMIN, name, arguments), timeout=8 * LIMIT_MS / 1000)
    assert reads.locked >= 1, (name, arguments, 'the tool made no count through the samples query route')
    return too_slow(name, arguments, answer)


def counts(message: str, count: int) -> bool:
    return re.search(rf'(?<![\d,.]){count:,}(?![\d,])|(?<![\d,.]){count}(?![\d,])', message) is not None


# --- 4. A number's summary whose second read runs out of time ---

FIRST_READ = ('field', 'type', 'unit', 'samples_with_value', 'samples_without', 'min', 'max', 'mean')


def test_a_number_summary_whose_second_read_runs_out_of_time_gives_the_first_reads_numbers(limited_world):
    async def scenario(w, url):
        await farm(w, statistics=False)
        await open_door(w)
        view = await make(ADMIN, V, 'state = Kansas')
        for target, count in (({'view': view['view']}, view['count']), ({'dataset': V}, test_assistant_views_spec.N)):
            arguments = {**target, 'field': 'brightness'}
            whole = await result(ADMIN, 'field_summary', arguments)
            async with locking_reads(url, lambda number: number >= 2) as reads:
                answer = await asyncio.wait_for(call(ADMIN, 'field_summary', arguments), timeout=8 * LIMIT_MS / 1000)
            assert reads.locked >= 1, 'the summary of a number made no second read'
            assert answer['ok'], (arguments, answer)
            partial = answer['result']
            for key in FIRST_READ:
                if key == 'mean':
                    assert math.isclose(partial['mean'], whole['mean'], rel_tol=1e-12), (partial, whole)
                else:
                    assert partial[key] == whole[key], (key, partial, whole)
            for key in ('quantiles', 'histogram', 'distinct'):
                assert key in partial and partial[key] is None, (key, partial)
            assert set(partial) == set(whole) | {'note'}, (partial, whole)
            note = partial['note']
            assert isinstance(note, str) and counts(note, count) and 'narrow' in note.lower(), note

    limited_world(scenario)


def test_a_number_summary_carries_its_distinct_count(world):
    """4.4: `distinct`, which a number's summary has today, and keeps."""

    async def scenario(w):
        planted = await farm(w)
        await open_door(w)
        view = await make(ADMIN, V, 'state = Kansas')
        for target, keep in (
            ({'dataset': V}, everything),
            ({'view': view['view']}, lambda row: row['state'] == 'Kansas'),
        ):
            values = {row['brightness'] for row in planted.rows(keep) if 'brightness' in row}
            summary = await result(ADMIN, 'field_summary', {**target, 'field': 'brightness'})
            assert summary['distinct'] == len(values), (target, summary)

    world(scenario)


def test_a_number_summarys_histogram_counts_the_values_its_smallest_largest_and_quantiles_count(world):
    """4.1's correction (3.3): on `gain`, a number field holding some of its numbers as text."""

    async def scenario(w):
        await plant(w)
        await open_door(w)
        view = await make(ADMIN, A, 'group = b')
        for target, (_, keep) in (
            ({'dataset': A}, FILTERS['everything']),
            ({'view': view['view']}, FILTERS['group b']),
        ):
            counted = sorted(x for row in ROWS if keep(row) for x in numbers_of(row, 'gain'))
            summary = await result(ADMIN, 'field_summary', {**target, 'field': 'gain'})
            assert (summary['min'], summary['max']) == (counted[0], counted[-1]), (target, summary)
            assert all(counted[0] <= value <= counted[-1] for value in summary['quantiles'].values()), summary
            histogram = summary['histogram']
            assert sum(item['count'] for item in histogram) == len(counted), (target, histogram)
            assert histogram[0]['from'] <= counted[0] and histogram[-1]['to'] >= counted[-1], (target, histogram)

    world(scenario)


# --- 5. too_slow says what ran out and what to do ---


def check_counting_message(message: str, count: int) -> None:
    """5.1: the view's count, every sample read whether indexed or not, narrow first, no advice to index."""
    lower = message.lower()
    assert counts(message, count), (count, message)
    assert re.search(r'\b(every|all)\b', lower) and 'sample' in lower, message
    assert 'indexed' in lower and 'narrow' in lower, message
    assert not INDEX_ADVICE.search(message), message


def test_too_slow_from_field_values_and_field_summary_gives_the_views_count_and_never_advises_indexing(limited_world):
    async def scenario(w, url):
        await farm(w, statistics=False)
        await open_door(w)
        view = await make(ADMIN, V, 'state = Kansas')
        for target, count in (({'view': view['view']}, view['count']), ({'dataset': V}, test_assistant_views_spec.N)):
            for name, field in (
                ('field_values', 'state'),
                ('field_values', 'serial'),
                ('field_summary', 'brightness'),
                ('field_summary', 'state'),
                ('field_values', 'owner_code'),  # a private field
            ):
                check_counting_message(await slow_first_read(url, name, {**target, 'field': field}), count)

    limited_world(scenario)


def test_too_slow_when_the_views_count_runs_out_says_every_sample_without_a_number(limited_world):
    """5.4: the count of the view is what ran out, so there is no N to give."""

    async def scenario(w, url):
        await farm(w, statistics=False)
        await open_door(w)
        view = await make(ADMIN, V, 'state = Kansas')
        for target in ({'view': view['view']}, {'dataset': V}):
            for name, field in (('field_values', 'state'), ('field_summary', 'brightness'), ('field_summary', 'state')):
                message = await slow_count(url, name, {**target, 'field': field})
                lower = message.lower()
                assert not re.search(r'\d', message), (name, target, message)
                assert re.search(r'\b(every|all)\b', lower) and 'sample' in lower, (name, target, message)
                assert 'indexed' in lower and 'narrow' in lower, (name, target, message)
                assert not INDEX_ADVICE.search(message), (name, target, message)

    limited_world(scenario)


def test_too_slow_on_fields_that_are_not_indexed_names_them_and_says_what_to_do(limited_world):
    async def scenario(w, url):
        await farm(w, statistics=False)
        await open_door(w)
        await index_field(w, V, 'state')  # one field indexed, so the others are told apart from it
        view = await make(ADMIN, V, 'state = Kansas')
        for name, arguments, plain_fields in (
            ('make_view', {'dataset': V, 'filter': 'serial = "SN-0001"'}, ['serial']),
            ('make_view', {'dataset': V, 'filter': 'state = Iowa AND brightness < 40'}, ['brightness']),
            ('make_view', {'dataset': V, 'filter': 'serial in ("SN-0001", "SN-0002")'}, ['serial']),
            ('refine_view', {'view': view['view'], 'add': 'flag = true'}, ['flag']),
            ('get_samples', {'view': view['view'], 'sort_by': 'brightness'}, ['brightness']),
            ('get_samples', {'dataset': V, 'sort_by': 'serial'}, ['serial']),
        ):
            message = await slow_count(url, name, arguments)
            lower = message.lower()
            assert all(field in message for field in plain_fields), (name, arguments, message)
            assert 'narrow' in lower and re.search(r'\b(every|all)\b', lower), (name, message)
            assert 'administrator' in lower and re.search(r'\bindex\b', lower), (name, message)
            assert 'media_type' not in message, (name, message)  # the tools' own condition, not the model's filter

    limited_world(scenario)


def test_too_slow_on_a_field_tested_only_with_contains_or_exists_names_it_without_advice_to_index(limited_world):
    """5.2: an index on the field would not serve `contains`, `exists` or `not exists`."""

    async def scenario(w, url):
        await farm(w, statistics=False)
        await open_door(w)
        await index_field(w, V, 'state')
        view = await make(ADMIN, V, 'state = Kansas')
        for name, arguments, field in (
            ('make_view', {'dataset': V, 'filter': 'serial contains "SN-00"'}, 'serial'),
            ('make_view', {'dataset': V, 'filter': 'serial exists'}, 'serial'),
            ('make_view', {'dataset': V, 'filter': 'serial not exists'}, 'serial'),
            ('make_view', {'dataset': V, 'filter': 'state = Iowa AND note contains "plain"'}, 'note'),
            ('make_view', {'dataset': V, 'filter': 'owner_code exists'}, 'owner_code'),  # private: its name only
            ('refine_view', {'view': view['view'], 'add': 'serial contains "SN-00"'}, 'serial'),
        ):
            message = await slow_count(url, name, arguments)
            lower = message.lower()
            assert field in message, (name, arguments, message)
            assert 'narrow' in lower and 'indexed' in lower and re.search(r'\b(every|all)\b', lower), (name, message)
            assert not INDEX_ADVICE.search(message), (name, arguments, message)
            assert 'media_type' not in message, (name, message)

    limited_world(scenario)


def test_too_slow_with_every_field_indexed_says_to_narrow_and_never_advises_indexing(limited_world):
    async def scenario(w, url):
        await farm(w, statistics=False)
        await open_door(w)
        for field in ('state', 'brightness'):
            await index_field(w, V, field)
        view = await make(ADMIN, V, 'state = Kansas')
        for name, arguments in (
            ('make_view', {'dataset': V, 'filter': 'state = Iowa'}),
            ('make_view', {'dataset': V, 'filter': 'state = Iowa AND brightness < 40'}),
            ('refine_view', {'view': view['view'], 'add': 'brightness > 10'}),
            ('get_samples', {'view': view['view'], 'sort_by': 'brightness'}),
        ):
            message = await slow_count(url, name, arguments)
            assert 'narrow' in message.lower() and not INDEX_ADVICE.search(message), (name, arguments, message)
            assert 'media_type' not in message, (name, message)
        # The counting tools, on an indexed field: they read every sample all the same.
        for name, field in (('field_values', 'state'), ('field_summary', 'brightness')):
            message = await slow_first_read(url, name, {'view': view['view'], 'field': field})
            check_counting_message(message, view['count'])

    limited_world(scenario)


def test_too_slow_from_another_tool_says_to_narrow_without_advice_to_index(limited_world):
    async def scenario(w, url):
        planted = await farm(w, statistics=False)
        await open_door(w)
        for name, arguments in (
            ('concept_preview', {'dataset': V, 'concept': 'weeds'}),
            ('search_similar', {'dataset': V, 'sample_ids': [planted.ids[0]]}),
        ):
            message = await slow(url, name, arguments)
            assert 'narrow' in message.lower() and not INDEX_ADVICE.search(message), (name, message)

    limited_world(scenario)


# --- 5.5 A statement that runs out of time in any request a tool makes is too_slow ---


async def query_running_out(w: World, url: str, user_id: int, body: dict[str, Any]) -> tuple[int, Any]:
    """`POST .../samples/query` as a person, with a real token, the samples locked while it runs: its status and its
    body as a client gets them (the app's own answer to an error it didn't expect included), an error id aside."""
    holder = await asyncpg.connect(url)
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            async with holder.transaction():
                await holder.execute('LOCK TABLE samples IN ACCESS EXCLUSIVE MODE')
                response = await asyncio.wait_for(
                    client.post(
                        f'/api/datasets/{V}/samples/query',
                        json=body,
                        headers={'Authorization': f'Bearer {w.tokens[user_id]}'},
                    ),
                    timeout=8 * LIMIT_MS / 1000,
                )
    finally:
        await holder.close()
    answer = response.json()
    if isinstance(answer, dict):
        answer.pop('error_id', None)
        if isinstance(answer.get('detail'), str):
            answer['detail'] = re.sub(r' \[error_id=\w+\]', '', answer['detail'])
    return response.status_code, answer


def test_a_samples_query_whose_page_runs_out_of_time_answers_as_its_count_does(limited_world):
    """5.5 at the route, under the server's own statement limit (the spec tests' way). A request with a limit of its
    own (a guest's in public mode, a tool's) is answered 504 by the request-limits middleware whichever statement ran
    out, so the page and the count already agree there."""

    async def scenario(w, url):
        await farm(w, statistics=False)
        for user_id in (ADMIN, POWER):
            counted = await query_running_out(w, url, user_id, {'limit': 0})
            assert counted[0] >= 500, (user_id, counted)  # it ran out: not an answer
            for body in (
                {'limit': 5, 'skip_count': True, 'fields': 'id', 'slim': True},  # the slim page
                {'limit': 5, 'skip_count': True},  # the page of whole rows
            ):
                paged = await query_running_out(w, url, user_id, body)
                assert paged == counted, (user_id, body, paged, counted)
                assert paged != (500, {'detail': 'Internal query error'}), (user_id, body)

    limited_world(scenario)


def test_a_tool_whose_page_without_a_count_runs_out_of_time_answers_too_slow(limited_world):
    async def scenario(w, url):
        await farm(w, statistics=False)
        await open_door(w)
        view = await make(ADMIN, V, 'state = Kansas')
        checked = []
        for name, arguments in (
            ('make_view', {'dataset': V, 'filter': 'state = Iowa'}),
            ('refine_view', {'view': view['view'], 'add': 'flag = true'}),
            ('get_samples', {'view': view['view']}),
            ('field_values', {'view': view['view'], 'field': 'state'}),
            ('field_summary', {'view': view['view'], 'field': 'brightness'}),
        ):
            async with locking_reads(url, lambda number: True, paths=('/samples/query',), pages=True) as reads:
                try:
                    answer = await asyncio.wait_for(call(ADMIN, name, arguments), timeout=8 * LIMIT_MS / 1000)
                except asyncio.TimeoutError:
                    raise
                except Exception as fault:
                    pytest.fail(
                        f'{name}: a samples query without a count ran out of time and the tool raised {fault!r}',
                    )
            if reads.locked:
                checked.append(name)
                too_slow(name, arguments, answer)
        assert checked, 'no tool made a samples query without a count (contract 7.3): nothing was checked'

    limited_world(scenario)
