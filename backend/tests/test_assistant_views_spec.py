"""Working views, value lists and their five tools (milestone 1, PR E1): make_view, refine_view, field_values,
field_summary and get_samples, the two endpoints for people, links, and the administrator's sample_fields setting.

Written from the contract (m1-contract-working-views, standing on m1-contract-assistant-core), not from the code. The
tools are called where the doors call them, `backend.assistant.call_tool`, against the test database with real users
and shares (the access-rule tests' World); the people's endpoints over HTTP, with real API tokens and with a browser
session made by signing in. Every number a tool gives is checked against what this file computes from the data it
planted (ROWS), and against what the API answers the same user.

Where the contract is silent these tests read it this way:
- A view's `filter` is its stages' texts joined with " AND ", the texts being what the parse route writes for each
  part (`parts`). An empty filter is the whole dataset: no stages, `filter` "", and the link `?q=` empty.
- A view's stages are numbered 1, 2, 3 ... in order in every view, a refined one included. The stage numbers in one
  refine_view call name the stages of the view being refined. (The order test removes stage 2 and negates stage 1, so
  it holds whichever numbering the negate reads.)
- A handle is at least 16 characters after its prefix, and handles share no long beginning (they can't be guessed).
- The arguments of the five tools are exactly the ones the contract names; `make_view` requires `dataset`,
  `refine_view` `view`, `field_values` and `field_summary` `field`, and nothing more is required (view and dataset
  are one-of).
- `field_values` gives `values` as `{"value", "count"}`, booleans and numbers as JSON values; with `search`, the
  values, `distinct` and the kept list are those of the matching values. `complete` is true for a count that is
  whole. `field_summary`'s `top` has the same entries, ties by value. `samples_without` is the view's count less
  `samples_with_value`.
- A quantile is the interpolated (percentile_cont) or the nearest-rank (percentile_disc) value; either is accepted.
  A histogram's bins are contiguous and each counts the values inside it; a value on an edge between two bins counts
  in one of them.
- A datetime's minimum and maximum are ISO text.
- `get_samples` with fields shows each field either as a key of the sample or under the sample's `fields`;
  `withheld` is given on the answer or on each sample; a sample without a field leaves it out or gives null.
- An `in_list` / `not_in_list` stage selects exactly what the app's own filter `<field> in (...)` / `<field> not in
  (...)` with the list's values selects through the API (the values read from the list's download by its person),
  whatever that does with samples that have no value. A short value list on a field whose values aren't private can
  be written as text, so its view's link is `?q=`; a list of 10,000 values can't (too long), so it is `?w=`.
- The link of a `?q=` view is at most the app's longest link, 6,000 characters (frontend/lib/links.ts
  MAX_LINK_LENGTH; docs/DATASET_MANAGEMENT.md). The server's public address is FRIDGE_PUBLIC_URL, else the first
  entry of ALLOWED_ORIGINS, else http://localhost:3000 (told by the coordinator): the link test sets each in turn and
  checks every link starts with it; elsewhere every link has one origin, never the address the test's requests came
  in on (http://test).
- `GET /api/assistant/views/{handle}` gives stages as a saved view keeps them ({type, description, filter, ...}):
  the view is the AND of their filters. For another signed-in user it is the contract's own 404 sentence, which
  differs from an unknown handle's (the contract says so); the tools' and the download's answers to another user's
  handle are exactly an unknown handle's.
- Signed out, both endpoints answer the API's usual 401 (they learn nothing); an `mcp` token is tried only if the
  branch can make one (POST /api/tokens with scope "mcp"), and is then refused with 401 or 403.
- A user who loses access to the dataset, or is lowered out of it, gets neither endpoint's answer (anything but
  200): a handle gives nothing its user couldn't get without it. A dataset closed to the assistant is checked
  through the tools only (closing changes nothing outside the assistant).
- "Lowered" is an administrator, who opens every dataset by role, made a power user with no share.
- "The server's housekeeping" is the scheduled cleanup main already runs for what expires
  (auth_cleanup.scheduled_cleanup, as the job service runs it). The rows the assistant keeps for its views and lists
  are found from outside as the tables that grow when a view and a list are made and don't when the same dataset is
  only read; 30 days going by is every time in those rows moved 30-odd days into the past.
- Deleting a dataset removes its lists too: a list of a deleted dataset is `not_found`, even in another dataset's
  refine_view.
- `field_summary` of a private field answers its counts (not a refusal); asking anything else of it (a sort, a
  search, a comparison) is refused.
- The refusal for questioning a private field names the field and says "assistant"; for sample fields it says
  "administrator"; `too_slow` says "narrow" or "index"; `list_note` gives the number of values and says "narrow" or
  "search"; `link_note` says "30 days" and "save".
- `too_slow` is made honestly enough: the server's statement time limit set short on the pool (as an operator
  could), and the count made to wait on a lock held on the samples table, so the statement runs into the limit.
"""

import asyncio
import contextlib
import csv
import io
import json
import math
import os
import secrets
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

import asyncpg
import bcrypt
import httpx
import pytest

from backend import auth_cleanup, main
from backend import database as database_module
from backend.database import PLANNING, Database, init_connection
from backend.routers import datasets
from backend.tests import (
    test_access_rule_spec,
    test_dataset_deletion,
    test_field_registry_postgres,
    test_regions_spec,
    test_upgrade_paths,
)
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_access_enforcement_spec import detail, set_share
from backend.tests.test_access_rule_spec import ADMIN, GUEST, LEVELS, OTHER_ADMIN, OTHER_POWER, POWER, ROLES, World
from backend.tests.test_assistant_core_spec import (
    CLOSED,
    HOSTILE,
    NOT_LET_IN,
    READ_POSTS,
    SETTINGS,
    VIEWER,
    add,
    assistant,
    open_door,
    plain,
    settings,
    snapshot,
)
from backend.tests.test_upgrade_paths import ROOT

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

V, ELSEWHERE, BIG, PAGED, OBJECTS, LISTS = 700, 701, 702, 703, 704, 705
N = 30
STATES = ['Kansas', 'Kansas', 'Kansas', 'Iowa', 'Nebraska']
OWNERS = [f'ZQV-OWN-{n:03d}' for n in range(7)]  # a private text field's values
SALARY = [873219.75 + 13 * i for i in range(25)]  # a private number field's values
PRIVATE = ('owner_code', 'salary')
METADATA = ('state', 'serial', 'brightness', 'flag', 'seen_at', 'note', 'owner_code', 'salary', 'box')
LINK_LIMIT = 6000
KEPT = 'values kept from the assistant'
SOMEONE_ELSES = (
    "This link belongs to someone else's conversation with the assistant. Ask them to save a view and share that."
)
NO_SUCH_VIEW = (
    "This link's view doesn't exist or has expired: the assistant keeps the views it makes for 30 days after they "
    "were last used."
)
CODES = {'not_allowed', 'refused', 'not_found', 'invalid', 'busy', 'unknown_tool', 'too_slow'}
VIEW_KEYS = {'view', 'dataset', 'count', 'filter', 'stages', 'link'}
FIVE = ('make_view', 'refine_view', 'field_values', 'field_summary', 'get_samples')


def planted(i: int) -> dict[str, Any]:
    """Sample i's values (a key left out: the sample has no such field)."""
    row: dict[str, Any] = {
        'i': i,
        'state': STATES[i % 5],
        'serial': f'SN-{i:04d}',
        'tags': ['dry', 'late'] if i % 3 == 0 else ['dry'],
        'score': (i % 10) / 10,  # the concept "weeds"
    }
    if i < 27:
        row['brightness'] = 1.0 + 3.5 * i
    if i < 20:
        row['flag'] = i % 4 == 0
    if i < 12:
        row['seen_at'] = f'2026-03-{i + 1:02d}T08:00:00Z'
    if i == 7:
        row['note'] = 'a,"b"'
    elif i in (8, 9):
        row['note'] = 'plain'
    if i < 21:
        row['owner_code'] = OWNERS[i % 7]
    if i < 25:
        row['salary'] = SALARY[i]
    if i < 3:
        row['box'] = {'x': i}
    return row


ROWS = [planted(i) for i in range(N)]
SECRETS = [*OWNERS, 'ZQV-OWN', *[json.dumps(value) for value in SALARY], *[str(int(value)) for value in SALARY]]


class Farm:
    def __init__(self, ids: list[int]):
        self.ids = ids  # by planted index

    def of(self, keep: Callable[[dict[str, Any]], bool]) -> set[int]:
        return {self.ids[row['i']] for row in ROWS if keep(row)}

    def rows(self, keep: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        return [row for row in ROWS if keep(row)]


def everything(row: dict[str, Any]) -> bool:
    return True


async def farm(w: World, dataset_id: int = V, statistics: bool = True) -> Farm:
    """ROWS as samples, a concept "weeds" scoring them, two private fields, counted statistics. The power user and the
    guest can view it; the other power user can't."""
    await w.dataset(dataset_id)
    samples = [
        {
            'filepath': f'v{dataset_id}-{row["i"]:02d}.jpg',
            'tags': row['tags'],
            'metadata': {key: row[key] for key in METADATA if key in row},
        }
        for row in ROWS
    ]
    await add(dataset_id, samples)
    by_path = {
        row['filepath']: row['id']
        for row in await w.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', dataset_id)
    }
    ids = [by_path[f'v{dataset_id}-{i:02d}.jpg'] for i in range(N)]
    vectors = [{'sample_id': s, 'vector': [1.0, i / 100, 0.0, 0.0], 'field_name': 'clip'} for i, s in enumerate(ids)]
    assert (await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/embeddings', json=vectors)).is_success
    concept = await w.database.fetchval(
        "INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) "
        "VALUES ($1, 'weeds', 'clip', 'seed', '[1,0,0,0]') RETURNING id",
        dataset_id,
    )
    await w.database.execute(
        'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) '
        'SELECT $1, unnest($2::bigint[]), unnest($3::float8[])',
        concept,
        ids,
        [row['score'] for row in ROWS],
    )
    for field in PRIVATE:
        marked = await w.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{dataset_id}/fields/{field}/description',
            json={'values_private': True},
        )
        assert marked.status_code == 200, marked.text
    await w.share(dataset_id, POWER, 'view')
    await w.share(dataset_id, GUEST, 'view')
    if statistics:
        await run_refresh(w.database, dataset_id)
    return Farm(ids)


# --- Calling the tools ---


async def call(user_id: int, name: str, arguments: Optional[dict] = None) -> dict[str, Any]:
    answer = plain(await assistant.call_tool(user_id=user_id, door='mcp', name=name, arguments=arguments or {}))
    assert isinstance(answer, dict) and answer.get('ok') in (True, False), answer
    if answer['ok']:
        assert set(answer) == {'ok', 'result'}, answer
        return answer
    assert set(answer) == {'ok', 'error'}, answer
    error = answer['error']
    assert error['code'] in CODES, error
    assert set(error) - {'details'} == {'code', 'message', 'retryable'}, error
    assert isinstance(error['message'], str) and error['message'].strip(), error
    assert error['retryable'] is (error['code'] == 'busy'), error
    if 'details' in error:
        assert error['code'] == 'invalid' and set(error['details']) == {'line', 'column', 'suggestions'}, error
    return answer


async def result(user_id: int, name: str, arguments: Optional[dict] = None) -> dict[str, Any]:
    answer = await call(user_id, name, arguments)
    assert answer['ok'], (user_id, name, arguments, answer)
    return answer['result']


async def error(user_id: int, name: str, arguments: Optional[dict] = None) -> dict[str, Any]:
    answer = await call(user_id, name, arguments)
    assert not answer['ok'], (user_id, name, arguments, answer)
    return answer['error']


def masked(answer: Any, *texts: str) -> str:
    written = json.dumps(answer, sort_keys=True)
    for text in texts:
        written = written.replace(text, '<handle>')
    return written


def origin(link: str) -> str:
    parts = urlsplit(link)
    assert parts.scheme in ('http', 'https') and parts.netloc, link
    return f'{parts.scheme}://{parts.netloc}'


def check_view(answer: dict[str, Any], dataset_id: int, refined: Optional[str] = None) -> dict[str, Any]:
    """A view answer's shape: its handle, numbered stages, and a link of one of the two kinds."""
    assert set(answer) - {'link_note'} == VIEW_KEYS | ({'from'} if refined else set()), answer
    assert answer['dataset'] == dataset_id and isinstance(answer['count'], int), answer
    handle = answer['view']
    assert isinstance(handle, str) and handle.startswith('wv_') and len(handle) >= 19, handle
    assert [stage['n'] for stage in answer['stages']] == list(range(1, len(answer['stages']) + 1)), answer
    assert all(set(stage) == {'n', 'text'} and isinstance(stage['text'], str) for stage in answer['stages']), answer
    if refined:
        assert answer['from'] == refined and handle != refined, answer
    link = urlsplit(answer['link'])
    assert origin(answer['link']) != 'http://test', answer['link']  # never the address a request came in on
    assert link.path.endswith(f'/datasets/{dataset_id}'), answer['link']
    params = parse_qs(link.query, keep_blank_values=True)
    if 'q' in params:
        assert set(params) == {'q'} and 'link_note' not in answer, answer
        assert params['q'] == [answer['filter']], answer
        assert len(answer['link']) <= LINK_LIMIT, len(answer['link'])
    else:
        assert params == {'w': [handle]}, answer['link']
        note = answer['link_note']
        assert '30 days' in note and 'save' in note.lower(), note
    return answer


async def make(user_id: int, dataset_id: int, text: Optional[str] = None) -> dict[str, Any]:
    arguments: dict[str, Any] = {'dataset': dataset_id}
    if text is not None:
        arguments['filter'] = text
    return check_view(await result(user_id, 'make_view', arguments), dataset_id)


async def refine(user_id: int, view: dict[str, Any], **change: Any) -> dict[str, Any]:
    answer = await result(user_id, 'refine_view', {'view': view['view'], **change})
    return check_view(answer, view['dataset'], refined=view['view'])


async def view_ids(user_id: int, handle: str) -> set[int]:
    """Every sample of the view, through get_samples' pages; the total the same on every page."""
    found: list[int] = []
    offset, total = 0, None
    while True:
        page = await result(user_id, 'get_samples', {'view': handle, 'limit': 50, 'offset': offset})
        assert page['offset'] == offset and (total is None or page['total'] == total), page
        total = page['total']
        found.extend(sample['id'] for sample in page['samples'])
        if page['next_offset'] is None:
            break
        assert page['next_offset'] == offset + len(page['samples']), page
        offset = page['next_offset']
    assert len(found) == len(set(found)) == total, (len(found), total)
    return set(found)


# --- What the API answers ---


async def api_ids(w: World, user_id: int, dataset_id: int, node: Optional[dict[str, Any]]) -> set[int]:
    response = await w.call(
        user_id,
        'POST',
        f'/api/datasets/{dataset_id}/samples/query',
        json={'limit': 1000, 'filters': node},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    ids = {item['id'] for item in body['items']}
    assert body['total'] == len(ids), body['total']
    return ids


async def api_total(w: World, user_id: int, dataset_id: int, node: Optional[dict[str, Any]]) -> int:
    response = await w.call(
        user_id,
        'POST',
        f'/api/datasets/{dataset_id}/samples/query',
        json={'limit': 0, 'filters': node},
    )
    assert response.status_code == 200, response.text
    return response.json()['total']


async def parsed(w: World, user_id: int, dataset_id: int, text: str) -> dict[str, Any]:
    response = await w.call(
        user_id,
        'POST',
        f'/api/datasets/{dataset_id}/filter_query/parse',
        json={'query': text, 'current': []},
    )
    assert response.status_code == 200, (text, response.text)
    return response.json()


async def opened(w: World, user_id: int, view: dict[str, Any], grain: bool = False) -> set[int]:
    """What the view's link opens for `user_id`: a ?q= text parsed by the server's parse route, or a ?w= handle's
    stages read through GET /api/assistant/views/{handle}. With `grain`, as the dataset page asks for it on a
    dataset with objects: its images (image_grain)."""
    params = parse_qs(urlsplit(view['link']).query, keep_blank_values=True)
    node: Optional[dict[str, Any]]
    if 'q' in params:
        text = params['q'][0]
        node = (await parsed(w, user_id, view['dataset'], text))['filter'] if text else None
    else:
        stages = await stages_of(w, user_id, view['view'])
        node = {'logic': 'AND', 'filters': stages} if stages else None
    return await api_ids(w, user_id, view['dataset'], image_grain(node) if grain else node)


# The excluded regions the app couples as NOT (the row or its partner is inside), and what "inside" is for each.
INSIDE = {
    'geo_polygon_exclude': 'geo_polygon',
    'embedding_polygon_exclude': 'embedding_polygon',
    'geo_region_exclude': 'geo_region',
}


def image_grain(base: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The filter the dataset page sends for its images on a dataset with objects: main's FilterContext.withGrain
    (frontend/contexts/FilterContext.tsx) for the image grain. Not an object row; and the image matches, or one of its
    objects does; an excluded region as NOT (the image or one of its objects is inside)."""
    filters = (base or {}).get('filters') or []
    parts: list[dict[str, Any]] = [{'field': 'media_type', 'operator': '!=', 'value': 'object'}]

    def coupled(node: dict[str, Any]) -> dict[str, Any]:
        return {
            'logic': 'OR',
            'filters': [
                {'field': '_cgself', 'operator': 'cg_self', 'value': node},
                {'field': '_crossgrain', 'operator': 'has_matching_object', 'value': node},
            ],
        }

    rest = [item for item in filters if item.get('operator') not in INSIDE]
    if rest:
        parts.append(coupled({'logic': 'AND', 'filters': rest}))
    for region in (item for item in filters if item.get('operator') in INSIDE):
        inside = {'logic': 'AND', 'filters': [{**region, 'operator': INSIDE[region['operator']]}]}
        parts.append({'logic': 'NOT', 'filters': [coupled(inside)]})
    return {'logic': 'AND', 'filters': parts}


async def stages_of(w: World, user_id: int, handle: str) -> list[dict[str, Any]]:
    response = await w.call(user_id, 'GET', f'/api/assistant/views/{handle}')
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {'dataset_id', 'stages', 'created_at', 'expires_at'}, body
    return [stage['filter'] for stage in body['stages']]


async def app_selects(w: World, dataset_id: int, field: str, operator: str, listed: dict[str, Any]) -> set[int]:
    """What the app's own filter `<field> <operator> (<the list's values>)` selects through the API, for the list's
    person (the values read from the list's download)."""
    values = [value for value, _ in csv_rows(await w.call(ADMIN, 'GET', urlsplit(listed['download']).path))]
    written = ', '.join(json.dumps(value) for value in values)
    node = (await parsed(w, ADMIN, dataset_id, f'{field} {operator} ({written})'))['filter']
    return await api_ids(w, ADMIN, dataset_id, node)


def csv_rows(response: httpx.Response) -> list[tuple[str, int]]:
    assert response.status_code == 200, response.text
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ['value', 'count'], rows[:2]
    return [(value, int(count)) for value, count in rows[1:]]


def tally(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    """The field's values among the rows (a list's elements), commonest first, ties by value."""
    counts: dict[Any, int] = {}
    for row in rows:
        if field in row:
            for value in row[field] if isinstance(row[field], list) else [row[field]]:
                counts[value] = counts.get(value, 0) + 1
    return [{'value': value, 'count': count} for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def having(rows: list[dict[str, Any]], field: str) -> int:
    return sum(1 for row in rows if field in row)


# --- Rows the assistant keeps for itself ---


async def table_counts(w: World) -> dict[str, int]:
    names = [
        row['table_name']
        for row in await w.database.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
            "AND table_type = 'BASE TABLE'",
        )
    ]
    return {name: await w.database.fetchval(f'SELECT count(*) FROM "{name}"') for name in names}


async def kept_tables(w: World, user_id: int, dataset_id: int) -> set[str]:
    """The tables the assistant keeps views and value lists in: they grow when a view and a list are made, and don't
    when the same dataset is only read."""
    for _ in range(2):  # the first round fills whatever a read fills
        before = await table_counts(w)
        await call(user_id, 'field_summary', {'dataset': dataset_id, 'field': 'state'})
        await call(user_id, 'get_samples', {'dataset': dataset_id})
        await call(user_id, 'describe_dataset', {'dataset': dataset_id})
    reads = {table for table, count in (await table_counts(w)).items() if count != before[table]}
    before = await table_counts(w)
    view = await make(user_id, dataset_id, 'state exists')
    await result(user_id, 'field_values', {'view': view['view'], 'field': 'state'})
    kept = {table for table, count in (await table_counts(w)).items() if count > before[table]} - reads
    assert kept, 'a view and a value list were made, and nothing was kept for them'
    return kept


async def counted(w: World, tables: set[str]) -> dict[str, int]:
    return {table: await w.database.fetchval(f'SELECT count(*) FROM "{table}"') for table in sorted(tables)}


async def age(w: World, tables: set[str], days: int) -> None:
    """`days` going by for what the assistant keeps: every time in its rows moved that far into the past."""
    for table in tables:
        columns = await w.database.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = $1 AND data_type IN ('timestamp with time zone', 'timestamp without time zone', 'date')",
            table,
        )
        for column in (row['column_name'] for row in columns):
            await w.database.execute(
                f'UPDATE "{table}" SET "{column}" = "{column}" - make_interval(days => $1)',
                days,
            )


async def housekeeping(w: World) -> None:
    """The server's scheduled cleanup of what expires, as the job service runs it."""
    await auth_cleanup.scheduled_cleanup(w.database.pool)


@contextlib.asynccontextmanager
async def signed_in(w: World, user_id: int) -> AsyncIterator[httpx.AsyncClient]:
    """A browser of its own, signed in as the user with a password (a session cookie, no token)."""
    password = secrets.token_urlsafe(12)
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    username = await w.database.fetchval(
        'UPDATE users SET password_hash = $2 WHERE id = $1 RETURNING username',
        user_id,
        hashed,
    )
    transport = httpx.ASGITransport(app=main.app)
    # https: the session cookie is a secure one, which a browser sends back only over https.
    async with httpx.AsyncClient(transport=transport, base_url='https://test') as browser:
        response = await browser.post('/api/auth/login', json={'username': username, 'password': password})
        assert response.status_code == 200, response.text
        yield browser


# --- 2. The tool list ---


def test_the_five_tools_are_listed_as_reads_with_the_arguments_the_contract_names():
    tools = {tool.name: tool for tool in assistant.tools()}
    expected = {
        'make_view': ({'dataset', 'filter'}, {'dataset'}),
        'refine_view': ({'view', 'add', 'remove', 'negate', 'in_list', 'not_in_list'}, {'view'}),
        'field_values': ({'view', 'dataset', 'field', 'search', 'limit', 'offset'}, {'field'}),
        'field_summary': ({'view', 'dataset', 'field'}, {'field'}),
        'get_samples': ({'view', 'dataset', 'limit', 'offset', 'sort_by', 'descending', 'fields'}, set()),
    }
    assert {'list_datasets', 'describe_dataset', *expected} <= set(tools), sorted(tools)
    for name, (properties, required) in expected.items():
        tool = tools[name]
        schema = plain(tool.input_schema)
        assert set(schema['properties']) == properties, (name, sorted(schema['properties']))
        assert set(schema.get('required', [])) == required, (name, schema.get('required'))
        assert tool.kind == 'read' and tool.level in (None, 'view'), (name, tool.kind, tool.level)
        assert tool.routes, name
        for method, template in tool.routes:
            path = template if template.startswith('/api') else '/api' + template
            assert method == 'GET' or (method == 'POST' and path in READ_POSTS), (name, method, template)
            assert not path.startswith(('/api/admin', '/api/assistant')), (name, method, template)


# --- 1. A working view, and its handle ---


def test_make_view_counts_exactly_and_writes_its_stages_as_the_server_writes_them(world):
    # Rewritten for contract g7 (one rule everywhere, rule 1.2): the NOT around `state` and `flag` keeps only samples
    # that have a flag (samples 20 to 29 have none; it kept 21, 24 and 27).
    cases: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        ('', everything),
        ('state = Kansas', lambda r: r['state'] == 'Kansas'),
        ('state = Kansas AND brightness < 40', lambda r: r['state'] == 'Kansas' and r.get('brightness', 99) < 40),
        (
            'tags in (late) AND NOT (state = Iowa OR flag = true)',
            lambda r: 'late' in r['tags'] and 'flag' in r and not (r['state'] == 'Iowa' or r.get('flag') is True),
        ),
        ('serial contains "SN-001"', lambda r: r['serial'].startswith('SN-001')),
        ('seen_at >= 2026-03-05', lambda r: r.get('seen_at', '') >= '2026-03-05'),
        ('owner_code exists', lambda r: 'owner_code' in r),
        (
            'owner_code not exists AND brightness >= 50',
            lambda r: 'owner_code' not in r and r.get('brightness', 0) >= 50,
        ),
        ('concept weeds > 0.5', lambda r: r['score'] > 0.5),
        ('not concept weeds > 0.5 AND state != Kansas', lambda r: not r['score'] > 0.5 and r['state'] != 'Kansas'),
        (
            'brightness between 10 and 30\nAND # a comment\nstate in (Iowa, Nebraska)',
            lambda r: 10 <= r.get('brightness', 0) <= 30 and r['state'] in ('Iowa', 'Nebraska'),
        ),
    ]

    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        origins = set()
        for text, keep in cases:
            view = await make(ADMIN, V, text)
            expected = f.of(keep)
            assert view['count'] == len(expected), (text, view)
            parts = (await parsed(w, ADMIN, V, text))['parts'] if text else []
            assert [stage['text'] for stage in view['stages']] == parts, (text, view['stages'])
            assert view['filter'] == ' AND '.join(parts), (text, view['filter'])
            assert await view_ids(ADMIN, view['view']) == expected, text
            # The link is permanent, for anyone who can open the dataset: the power user opens it too.
            assert await opened(w, POWER, view) == expected, text
            origins.add(origin(view['link']))
        assert len(origins) == 1, origins

    world(scenario)


def test_handles_are_new_each_time_and_cant_be_guessed(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        handles = [(await make(ADMIN, V, 'state = Kansas'))['view'] for _ in range(6)]
        assert len(set(handles)) == len(handles), handles
        for one in handles:
            for other in handles:
                if one != other:
                    shared = len(os.path.commonprefix([one[3:], other[3:]]))
                    assert shared < 8, (one, other)

    world(scenario)


def test_another_users_handle_is_not_found_exactly_like_one_that_never_existed(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        mine = await make(ADMIN, V, 'state = Kansas')
        listed = await result(ADMIN, 'field_values', {'view': mine['view'], 'field': 'state'})
        never_view, never_list = 'wv_' + secrets.token_urlsafe(18), 'vl_' + secrets.token_urlsafe(18)
        # The other administrator reads the dataset as well: only whose handle it is differs.
        for name, arguments in (
            ('refine_view', {'add': 'brightness < 40'}),
            ('refine_view', {'remove': [1]}),
            ('field_values', {'field': 'state'}),
            ('field_summary', {'field': 'brightness'}),
            ('get_samples', {}),
        ):
            theirs = await error(OTHER_ADMIN, name, {'view': mine['view'], **arguments})
            never = await error(OTHER_ADMIN, name, {'view': never_view, **arguments})
            assert theirs['code'] == 'not_found' and '30 days' in theirs['message'], (name, theirs)
            assert masked(theirs, mine['view']) == masked(never, never_view), (name, theirs, never)
        whole = await make(OTHER_ADMIN, V)
        for key in ('in_list', 'not_in_list'):
            theirs = await error(
                OTHER_ADMIN,
                'refine_view',
                {'view': whole['view'], key: {'field': 'state', 'list': listed['list']}},
            )
            never = await error(
                OTHER_ADMIN,
                'refine_view',
                {'view': whole['view'], key: {'field': 'state', 'list': never_list}},
            )
            assert theirs['code'] == 'not_found', (key, theirs)
            assert masked(theirs, listed['list']) == masked(never, never_list), (key, theirs, never)
        # The download: anyone else gets what an unknown handle gets.
        download = urlsplit(listed['download']).path
        theirs_csv = await w.call(OTHER_ADMIN, 'GET', download)
        never_csv = await w.call(OTHER_ADMIN, 'GET', download.replace(listed['list'], never_list))
        assert theirs_csv.status_code == never_csv.status_code == 404, (theirs_csv.text, never_csv.text)
        assert theirs_csv.text.replace(listed['list'], '<h>') == never_csv.text.replace(never_list, '<h>')
        # The view endpoint: the contract's own sentence for someone else's handle; 30 days for an unknown one.
        theirs_view = await w.call(OTHER_ADMIN, 'GET', f'/api/assistant/views/{mine["view"]}')
        assert theirs_view.status_code == 404 and detail(theirs_view) == SOMEONE_ELSES, theirs_view.text
        never_answer = await w.call(OTHER_ADMIN, 'GET', f'/api/assistant/views/{never_view}')
        assert never_answer.status_code == 404 and detail(never_answer) == NO_SUCH_VIEW, never_answer.text
        # Its own user has all of it.
        assert await view_ids(ADMIN, mine['view']) == f.of(lambda r: r['state'] == 'Kansas')
        assert sorted(csv_rows(await w.call(ADMIN, 'GET', download))) == [('Kansas', 18)]
        assert await stages_of(w, ADMIN, mine['view'])

    world(scenario)


def test_a_handle_gives_nothing_its_user_could_not_get_without_it(world):
    """Lost access, lowered, or the dataset closed to the assistant between making a view and using it: refused, with
    the API's words, from every tool the view or its list goes to."""

    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        kansas = f.of(lambda r: r['state'] == 'Kansas')

        async def made(user_id: int) -> tuple[str, str, str]:
            view = await make(user_id, V, 'state = Kansas')
            listed = await result(user_id, 'field_values', {'view': view['view'], 'field': 'serial'})
            return view['view'], listed['list'], urlsplit(listed['download']).path

        async def refused_everywhere(user_id: int, view: str, listed: str, words: str) -> None:
            for name, arguments in (
                ('refine_view', {'view': view, 'add': 'brightness < 40'}),
                ('refine_view', {'view': view, 'negate': [1]}),
                ('refine_view', {'view': view, 'in_list': {'field': 'serial', 'list': listed}}),
                ('field_values', {'view': view, 'field': 'state'}),
                ('field_summary', {'view': view, 'field': 'brightness'}),
                ('get_samples', {'view': view}),
            ):
                refused = await error(user_id, name, arguments)
                assert (refused['code'], refused['message']) == ('refused', words), (user_id, name, refused)

        async def api_words(user_id: int) -> str:
            response = await w.call(user_id, 'POST', f'/api/datasets/{V}/samples/query', json={'limit': 0})
            assert response.status_code == 403, response.text
            return detail(response)

        # Loses access: the power user's share taken away. Given back, the handle works again.
        view, listed, download = await made(POWER)
        await set_share(w, V, POWER, None)
        await refused_everywhere(POWER, view, listed, await api_words(POWER))
        assert (await w.call(POWER, 'GET', f'/api/assistant/views/{view}')).status_code != 200
        assert (await w.call(POWER, 'GET', download)).status_code != 200
        await set_share(w, V, POWER, 'view')
        assert await view_ids(POWER, view) == kansas
        # Lowered: an administrator (every dataset, by role) made a power user with no share.
        view, listed, download = await made(OTHER_ADMIN)
        await w.database.execute("UPDATE users SET role = 'power_user' WHERE id = $1", OTHER_ADMIN)
        await refused_everywhere(OTHER_ADMIN, view, listed, await api_words(OTHER_ADMIN))
        assert (await w.call(OTHER_ADMIN, 'GET', f'/api/assistant/views/{view}')).status_code != 200
        assert (await w.call(OTHER_ADMIN, 'GET', download)).status_code != 200
        # Closed to the assistant: the core's words, for a user who still reads the dataset through the API.
        view, listed, download = await made(ADMIN)
        closed = await w.call(ADMIN, 'PATCH', f'/api/datasets/{V}', json={'assistant_closed': True})
        assert closed.status_code == 200, closed.text
        await refused_everywhere(ADMIN, view, listed, CLOSED)
        assert await api_total(w, ADMIN, V, None) == N

    world(scenario)


def test_deleting_a_dataset_removes_its_views_and_value_lists(world):
    async def scenario(w):
        await farm(w)
        elsewhere = await farm(w, ELSEWHERE)
        await open_door(w)
        kept = await kept_tables(w, ADMIN, ELSEWHERE)
        stays = await make(ADMIN, ELSEWHERE, 'state = Iowa')
        await result(ADMIN, 'field_values', {'view': stays['view'], 'field': 'serial'})
        before = await counted(w, kept)
        doomed = await make(ADMIN, V, 'state = Iowa')
        doomed_refined = await refine(ADMIN, doomed, add='brightness < 60')
        doomed_list = await result(ADMIN, 'field_values', {'view': doomed['view'], 'field': 'serial'})
        assert await counted(w, kept) != before
        assert (await w.call(ADMIN, 'DELETE', f'/api/datasets/{V}')).is_success
        await test_dataset_deletion.finish(w.database)
        assert (await w.call(ADMIN, 'GET', f'/api/datasets/{V}')).status_code == 404
        for handle in (doomed['view'], doomed_refined['view']):
            for name, arguments in (
                ('get_samples', {}),
                ('field_values', {'field': 'state'}),
                ('refine_view', {'add': 'state exists'}),
            ):
                gone = await error(ADMIN, name, {'view': handle, **arguments})
                assert gone['code'] == 'not_found', (name, gone)
            removed = await w.call(ADMIN, 'GET', f'/api/assistant/views/{handle}')
            assert removed.status_code == 404 and detail(removed) == NO_SUCH_VIEW, removed.text
        assert (await w.call(ADMIN, 'GET', urlsplit(doomed_list['download']).path)).status_code == 404
        listed = {'field': 'serial', 'list': doomed_list['list']}
        assert (await error(ADMIN, 'refine_view', {'view': stays['view'], 'in_list': listed}))['code'] == 'not_found'
        assert await counted(w, kept) == before  # its views and lists are gone; the other dataset's are kept
        assert await view_ids(ADMIN, stays['view']) == elsewhere.of(lambda r: r['state'] == 'Iowa')

    world(scenario)


def test_refining_a_view_never_changes_it(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        first = await make(ADMIN, V, 'state = Kansas AND brightness < 60')
        selected = f.of(lambda r: r['state'] == 'Kansas' and r.get('brightness', 99) < 60)
        stages = await stages_of(w, ADMIN, first['view'])
        second = await refine(ADMIN, first, add='flag = true')
        third = await refine(ADMIN, second, remove=[1])
        fourth = await refine(ADMIN, first, negate=[2])
        assert len({first['view'], second['view'], third['view'], fourth['view']}) == 4
        assert second['count'] == len(
            f.of(lambda r: r['state'] == 'Kansas' and r.get('brightness', 99) < 60 and r.get('flag') is True),
        )
        assert third['count'] == len(f.of(lambda r: r.get('brightness', 99) < 60 and r.get('flag') is True))
        # Rewritten for contract g7 (one rule everywhere, rule 1.2): the negated stage needs a brightness (it kept the
        # samples with none).
        assert fourth['count'] == len(
            f.of(lambda r: r['state'] == 'Kansas' and 'brightness' in r and not r['brightness'] < 60),
        )
        # The view refined three times over selects what it did, stage for stage.
        assert await view_ids(ADMIN, first['view']) == selected
        assert await stages_of(w, ADMIN, first['view']) == stages
        assert await view_ids(ADMIN, second['view']) == f.of(
            lambda r: r['state'] == 'Kansas' and r.get('brightness', 99) < 60 and r.get('flag') is True,
        )

    world(scenario)


def test_views_and_lists_are_kept_thirty_days_after_their_last_use(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        kept = await kept_tables(w, ADMIN, V)
        used = await make(ADMIN, V, 'state = Kansas')
        used_list = await result(ADMIN, 'field_values', {'view': used['view'], 'field': 'serial'})
        idle = await make(ADMIN, V, 'state = Iowa')
        idle_list = await result(ADMIN, 'field_values', {'view': idle['view'], 'field': 'serial'})
        await age(w, kept, 29)
        await housekeeping(w)
        # 29 days on, both are there; using them is a use.
        assert await view_ids(ADMIN, used['view']) == f.of(lambda r: r['state'] == 'Kansas')
        await refine(ADMIN, used, in_list={'field': 'serial', 'list': used_list['list']})
        await age(w, kept, 2)
        before = await counted(w, kept)
        await housekeeping(w)
        # 31 days after its last use the idle view and its list are gone; 2 days after, the used ones are kept.
        assert sum((await counted(w, kept)).values()) < sum(before.values()), before
        for name, arguments in (('get_samples', {}), ('field_values', {'field': 'state'})):
            gone = await error(ADMIN, name, {'view': idle['view'], **arguments})
            assert gone['code'] == 'not_found' and '30 days' in gone['message'], gone
        stale = await error(
            ADMIN,
            'refine_view',
            {'view': used['view'], 'in_list': {'field': 'serial', 'list': idle_list['list']}},
        )
        assert stale['code'] == 'not_found', stale
        idle_endpoint = await w.call(ADMIN, 'GET', f'/api/assistant/views/{idle["view"]}')
        assert idle_endpoint.status_code == 404 and detail(idle_endpoint) == NO_SUCH_VIEW, idle_endpoint.text
        assert (await w.call(ADMIN, 'GET', urlsplit(idle_list['download']).path)).status_code == 404
        assert await view_ids(ADMIN, used['view']) == f.of(lambda r: r['state'] == 'Kansas')
        assert len(csv_rows(await w.call(ADMIN, 'GET', urlsplit(used_list['download']).path))) == 18

    world(scenario)


# --- 4.4 Private fields ---


def test_no_value_of_a_private_field_appears_in_any_result_stage_filter_or_link(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        await settings(w, {'sample_fields': True})
        answers: list[Any] = []
        links: list[str] = []
        for user_id in (ADMIN, POWER):
            answers.append(await call(user_id, 'list_datasets'))
            answers.append(await call(user_id, 'describe_dataset', {'dataset': V, 'limit': 100}))
            iowa = await make(user_id, V, 'state = Iowa')
            present = await make(user_id, V, 'owner_code exists AND salary exists')
            answers += [iowa, present]
            owners = await result(user_id, 'field_values', {'view': iowa['view'], 'field': 'owner_code'})
            salaries = await result(user_id, 'field_values', {'dataset': V, 'field': 'salary', 'limit': 50})
            answers += [owners, salaries]
            kept = await refine(user_id, present, in_list={'field': 'owner_code', 'list': owners['list']})
            dropped = await refine(user_id, kept, not_in_list={'field': 'salary', 'list': salaries['list']})
            answers += [kept, dropped]
            for view in (iowa, present, kept, dropped):
                links.append(view['link'])
                for field in (*PRIVATE, 'state', 'brightness'):
                    answers.append(await call(user_id, 'field_summary', {'view': view['view'], 'field': field}))
                    answers.append(await call(user_id, 'field_values', {'view': view['view'], 'field': field}))
                page = await call(
                    user_id,
                    'get_samples',
                    {
                        'view': view['view'],
                        'limit': 50,
                        'fields': ['state', 'filepath', *PRIVATE],
                        'sort_by': 'brightness',
                    },
                )
                answers.append(page)
                if page['ok']:
                    links += [sample['link'] for sample in page['result']['samples']]
            links += [owners['download'], salaries['download']]
        written = json.dumps(answers) + '\n'.join(unquote(link) for link in links)
        for secret in SECRETS:
            assert secret not in written, secret

    world(scenario)


QUESTIONS = [
    'owner_code = "ZQV-OWN-001"',
    'owner_code != "ZQV-OWN-001"',
    'owner_code = ZQV',
    'owner_code in ("ZQV-OWN-001", "x")',
    'owner_code not in ("x")',
    'owner_code contains "ZQV"',
    'owner_code like "%OWN%"',
    'salary > 873300',
    'salary <= 1',
    'salary between 873000 and 874000',
    'salary = 873219.75',
    'salary in (873219.75)',
    'state = Kansas AND owner_code = "ZQV-OWN-001"',
    'NOT (salary > 873300)',
    'brightness > 1 OR owner_code contains "Q"',
    '(owner_code = "a" OR owner_code exists)',
]


def test_questioning_a_private_field_is_refused_and_makes_no_view(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        await settings(w, {'sample_fields': True})
        kept = await kept_tables(w, ADMIN, V)
        whole = await make(ADMIN, V)
        before = await counted(w, kept)
        for text in QUESTIONS:
            field = 'owner_code' if 'owner_code' in text else 'salary'
            for name, arguments in (
                ('make_view', {'dataset': V, 'filter': text}),
                ('refine_view', {'view': whole['view'], 'add': text}),
            ):
                refused = await error(ADMIN, name, arguments)
                assert refused['code'] == 'refused', (text, name, refused)
                assert field in refused['message'] and 'assistant' in refused['message'].lower(), (text, refused)
        for name, arguments in (
            ('field_values', {'dataset': V, 'field': 'owner_code', 'search': 'ZQV'}),
            ('field_values', {'view': whole['view'], 'field': 'salary', 'search': '87'}),
            ('get_samples', {'dataset': V, 'sort_by': 'salary'}),
            ('get_samples', {'view': whole['view'], 'sort_by': 'owner_code', 'descending': True}),
        ):
            refused = await error(ADMIN, name, arguments)
            assert refused['code'] == 'refused', (name, refused)
        assert await counted(w, kept) == before  # no view made, no list kept
        # Having a value or not is no question.
        for text, keep in (
            ('owner_code exists', lambda r: 'owner_code' in r),
            ('owner_code not exists', lambda r: 'owner_code' not in r),
            ('salary exists AND state = Kansas', lambda r: 'salary' in r and r['state'] == 'Kansas'),
        ):
            view = await make(ADMIN, V, text)
            assert view['count'] == len(f.of(keep)), (text, view)
            assert await view_ids(ADMIN, view['view']) == f.of(keep), text
        # A summary of a private field is its counts, nothing more.
        for field in PRIVATE:
            summary = await result(ADMIN, 'field_summary', {'dataset': V, 'field': field})
            assert not {'min', 'max', 'mean', 'quantiles', 'histogram', 'top'} & set(summary), summary
            assert (summary['samples_with_value'], summary['samples_without'], summary['distinct']) == (
                having(ROWS, field),
                N - having(ROWS, field),
                len(tally(ROWS, field)),
            ), summary

    world(scenario)


def test_value_lists_filter_a_private_field_exactly_and_show_no_value(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        iowa_rows = f.rows(lambda r: r['state'] == 'Iowa')
        iowa = await make(ADMIN, V, 'state = Iowa')
        owners = await result(ADMIN, 'field_values', {'view': iowa['view'], 'field': 'owner_code'})
        codes = {row['owner_code'] for row in iowa_rows if 'owner_code' in row}
        assert owners['values'] == [], owners
        assert (owners['distinct'], owners['samples_with_value']) == (len(codes), having(iowa_rows, 'owner_code'))
        assert owners['list'].startswith('vl_') and owners['complete'] is True, owners
        whole_values = await result(ADMIN, 'field_values', {'dataset': V, 'field': 'owner_code'})
        assert (whole_values['values'], whole_values['distinct'], whole_values['samples_with_value']) == ([], 7, 21)
        whole = await make(ADMIN, V)
        listed = {'field': 'owner_code', 'list': owners['list']}
        kept = await refine(ADMIN, whole, in_list=listed)
        dropped = await refine(ADMIN, whole, not_in_list=listed)
        assert await app_selects(w, V, 'owner_code', 'in', owners) == f.of(lambda r: r.get('owner_code') in codes)
        for view, operator in ((kept, 'in'), (dropped, 'not in')):
            expected = await app_selects(w, V, 'owner_code', operator, owners)
            text = f'owner_code {operator} ({len(codes)} {KEPT})'
            assert [stage['text'] for stage in view['stages']] == [text], view
            assert view['filter'] == text and 'w' in parse_qs(urlsplit(view['link']).query), view
            assert view['count'] == len(expected), (operator, view)
            assert await view_ids(ADMIN, view['view']) == expected, operator
            assert await opened(w, ADMIN, view) == expected, operator  # the person's ?w= stages select them too
        # A private field's list filters that field only.
        for key in ('in_list', 'not_in_list'):
            elsewhere = await error(
                ADMIN,
                'refine_view',
                {'view': whole['view'], key: {'field': 'serial', 'list': owners['list']}},
            )
            assert elsewhere['code'] == 'invalid', (key, elsewhere)
        # A private number field, through a list of its values in a narrower view.
        dim = await make(ADMIN, V, 'brightness < 20')
        salaries = await result(ADMIN, 'field_values', {'view': dim['view'], 'field': 'salary'})
        paid = {row['salary'] for row in f.rows(lambda r: r.get('brightness', 99) < 20) if 'salary' in row}
        assert (salaries['values'], salaries['distinct']) == ([], len(paid)), salaries
        by_salary = await refine(ADMIN, whole, in_list={'field': 'salary', 'list': salaries['list']})
        assert by_salary['stages'][0]['text'] == f'salary in ({len(paid)} {KEPT})', by_salary
        assert await view_ids(ADMIN, by_salary['view']) == await app_selects(w, V, 'salary', 'in', salaries)
        assert await app_selects(w, V, 'salary', 'in', salaries) == f.of(lambda r: r.get('salary') in paid)

    world(scenario)


def test_the_download_still_gives_the_person_every_value_of_a_private_field(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        iowa = await make(ADMIN, V, 'state = Iowa')
        owners = await result(ADMIN, 'field_values', {'view': iowa['view'], 'field': 'owner_code'})
        salaries = await result(ADMIN, 'field_values', {'dataset': V, 'field': 'salary'})
        expected_owners = sorted(
            (item['value'], item['count']) for item in tally(f.rows(lambda r: r['state'] == 'Iowa'), 'owner_code')
        )
        path = urlsplit(owners['download']).path
        assert sorted(csv_rows(await w.call(ADMIN, 'GET', path))) == expected_owners
        async with signed_in(w, ADMIN) as browser:
            assert sorted(csv_rows(await browser.get(path))) == expected_owners  # the person, in the browser
        got = csv_rows(await w.call(ADMIN, 'GET', urlsplit(salaries['download']).path))
        assert sorted((float(value), count) for value, count in got) == sorted((value, 1) for value in SALARY)

    world(scenario)


def test_halving_a_private_number_or_guessing_a_private_text_learns_nothing(world):
    """An attacker's questions: no answer depends on the value guessed, and none carries a count."""

    async def scenario(w):
        await farm(w)
        await open_door(w)
        await settings(w, {'sample_fields': True})
        whole = await make(ADMIN, V)

        async def probes(written: str, *guesses: str) -> list[str]:
            """The answers to one question, written text and guessed values masked: what is left can't depend on
            the guess unless the answer does."""
            answers = []
            for name, arguments in (
                ('make_view', {'dataset': V, 'filter': written}),
                ('refine_view', {'view': whole['view'], 'add': written}),
            ):
                answer = await call(ADMIN, name, arguments)
                assert not answer['ok'] and answer['error']['code'] == 'refused', (written, answer)
                answers.append(masked(answer, written, *guesses))
            return answers

        # Halving the salaries: every step is refused alike, wherever the midpoint falls.
        low, high = 873000, 874000
        first: Optional[list[str]] = None
        for _ in range(12):
            middle = (low + high) // 2
            seen = []
            for written in (f'salary > {middle}', f'salary <= {middle}', f'salary between {low} and {middle}'):
                seen += await probes(written, str(middle), str(low))
            assert first is None or seen == first, (middle, seen, first)
            first = first or seen
            low = middle  # an attacker gets nothing to steer by; go on as if told "above"
        # Guessing an owner: a value that is there and one that isn't get the same answers.
        real, fake = OWNERS[3], 'ZQV-OWN-999'
        for template in ('owner_code = "{}"', 'owner_code in ("{}")', 'owner_code contains "{}"', 'owner_code != "{}"'):
            assert await probes(template.format(real), real) == await probes(template.format(fake), fake), template
        for guess_real, guess_fake in ((real, fake), (real[:7], 'QQQ-')):
            one = await call(ADMIN, 'field_values', {'dataset': V, 'field': 'owner_code', 'search': guess_real})
            other = await call(ADMIN, 'field_values', {'dataset': V, 'field': 'owner_code', 'search': guess_fake})
            assert not one['ok'] and masked(one, guess_real) == masked(other, guess_fake), (one, other)
        # Sorting by it would order the samples by its values: refused alike, whichever way.
        up = await call(ADMIN, 'get_samples', {'dataset': V, 'sort_by': 'salary'})
        down = await call(ADMIN, 'get_samples', {'dataset': V, 'sort_by': 'salary', 'descending': True})
        assert not up['ok'] and up == down, (up, down)

    world(scenario)


# --- 2. Counting: field_values and field_summary ---


def test_field_values_counts_every_value_commonest_first(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        for text, keep in (
            ('', everything),
            ('brightness < 40', lambda r: r.get('brightness', 99) < 40),
            ('concept weeds > 0.5', lambda r: r['score'] > 0.5),
        ):
            view = await make(ADMIN, V, text)
            rows = f.rows(keep)
            for field in ('state', 'tags', 'flag', 'brightness', 'serial', 'note'):
                expected = tally(rows, field)
                answer = await result(ADMIN, 'field_values', {'view': view['view'], 'field': field, 'limit': 50})
                assert answer['field'] == field, answer
                assert answer['values'] == expected, (text, field, answer['values'])
                assert (answer['distinct'], answer['samples_with_value']) == (len(expected), having(rows, field))
                assert answer['complete'] is True and answer['list'].startswith('vl_'), answer
                assert origin(answer['download']) == origin(view['link']), answer['download']
                assert urlsplit(answer['download']).path.endswith(f'/api/assistant/lists/{answer["list"]}/csv')
        # The default page is 20, `limit` takes another; distinct is all of them.
        page = await result(ADMIN, 'field_values', {'dataset': V, 'field': 'serial'})
        assert page['values'] == tally(ROWS, 'serial')[:20] and page['distinct'] == N, page
        page = await result(ADMIN, 'field_values', {'dataset': V, 'field': 'serial', 'limit': 5})
        assert page['values'] == tally(ROWS, 'serial')[:5] and page['distinct'] == N, page
        # `search`: values containing the text, any letter case.
        for search in ('ka', 'KA', 'Ka'):
            found = await result(ADMIN, 'field_values', {'dataset': V, 'field': 'state', 'search': search})
            assert found['values'] == [v for v in tally(ROWS, 'state') if 'ka' in v['value'].lower()], found
            assert found['distinct'] == 2, found
            assert found['samples_with_value'] == having(ROWS, 'state'), found  # search doesn't change it
        # What field_values doesn't take.
        for field in ('seen_at', 'box'):
            wrong = await error(ADMIN, 'field_values', {'dataset': V, 'field': field})
            assert wrong['code'] == 'invalid', (field, wrong)
        assert 'field_summary' in (await error(ADMIN, 'field_values', {'dataset': V, 'field': 'seen_at'}))['message']
        missing = await error(ADMIN, 'field_values', {'dataset': V, 'field': 'no_such_field'})
        assert missing['code'] == 'not_found' and 'no_such_field' in missing['message'], missing
        for limit in (0, 51):
            assert (await error(ADMIN, 'field_values', {'dataset': V, 'field': 'state', 'limit': limit}))[
                'code'
            ] == 'invalid'

    world(scenario)


def test_field_values_pages_through_every_value_once_in_order_with_one_list(world):
    async def scenario(w):
        await w.dataset(PAGED)
        counts = {f'C{k:03d}': k % 4 + 1 for k in range(130)}  # more than a page holds, uneven
        await add(
            PAGED,
            [
                {'filepath': f'paged-{code}-{n}.jpg', 'metadata': {'code': code}}
                for code, many in counts.items()
                for n in range(many)
            ],
        )
        await open_door(w)
        expected = sorted(counts.items(), key=lambda item: (-item[1], item[0]))  # commonest first, ties by value
        for limit in (50, 7):
            seen: list[tuple[str, int]] = []
            lists, downloads = set(), set()
            offset: Optional[int] = 0
            while offset is not None:
                arguments = {'dataset': PAGED, 'field': 'code', 'limit': limit, 'offset': offset}
                page = await result(ADMIN, 'field_values', arguments)
                assert (page['offset'], page['distinct']) == (offset, len(counts)), page
                assert len(page['values']) == min(limit, len(counts) - offset), (offset, len(page['values']))
                seen += [(item['value'], item['count']) for item in page['values']]
                lists.add(page['list'])
                downloads.add(page['download'])
                offset = page['next_offset']
                assert offset is None or offset == page['offset'] + len(page['values']), page['next_offset']
            assert seen == expected, limit  # every value exactly once, in order
            assert len(lists) == len(downloads) == 1 and None not in lists, lists  # one list for every page
            (download,) = downloads
            assert sorted(csv_rows(await w.call(ADMIN, 'GET', urlsplit(download).path))) == sorted(expected)
        past = await result(ADMIN, 'field_values', {'dataset': PAGED, 'field': 'code', 'offset': len(counts)})
        assert (past['values'], past['next_offset'], past['distinct']) == ([], None, len(counts)), past

    world(scenario)


def test_a_metadata_list_is_one_value_while_tags_and_array_fields_count_by_element(world):
    """A list kept in metadata (not a registered array field) is one value: each value comes back in the form the app's
    own `=` filter on the field matches, so used through the list handle, or written back into a filter, it selects
    exactly the samples counted for it. No type is asserted for it. Tags and a registered array field count by
    element."""

    async def scenario(w):
        await w.dataset(LISTS)
        registered = await w.call(ADMIN, 'POST', f'/api/datasets/{LISTS}/array-fields', json={'field_name': 'pests'})
        assert registered.is_success, registered.text
        kinds: list[Optional[list[str]]] = [['a', 'b']] * 3 + [['a']] * 2 + [['b', 'c']] + [None] * 2
        await add(
            LISTS,
            [
                {
                    'filepath': f'list-{n}.jpg',
                    'tags': kind or ['z'],
                    'metadata': {'kinds': kind} if kind else {},
                    'array_fields': {'pests': kind or ['z']},
                }
                for n, kind in enumerate(kinds)
            ],
        )
        by_path = {
            row['filepath']: row['id']
            for row in await w.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', LISTS)
        }
        holding = {
            count: {by_path[f'list-{n}.jpg'] for n, kind in enumerate(kinds) if kind == whole}
            for whole, count in ((['a', 'b'], 3), (['a'], 2), (['b', 'c'], 1))
        }  # each list's samples, by how many hold it (the counts differ, so a count names its list)
        await open_door(w)
        whole_lists = await result(ADMIN, 'field_values', {'dataset': LISTS, 'field': 'kinds'})
        assert [item['count'] for item in whole_lists['values']] == [3, 2, 1], whole_lists['values']
        assert (whole_lists['distinct'], whole_lists['samples_with_value']) == (3, 6), whole_lists
        # (a) Through the list handle: the samples holding any of the lists (not the two without the field) ...
        whole = await make(ADMIN, LISTS)
        listed = await refine(ADMIN, whole, in_list={'field': 'kinds', 'list': whole_lists['list']})
        assert await view_ids(ADMIN, listed['view']) == set().union(*holding.values()), listed
        # ... and a narrower list, from a view, its one list's samples.
        narrow = await make(ADMIN, LISTS, 'tags in (c)')
        one = await result(ADMIN, 'field_values', {'view': narrow['view'], 'field': 'kinds'})
        assert [item['count'] for item in one['values']] == [1], one
        just = await refine(ADMIN, whole, in_list={'field': 'kinds', 'list': one['list']})
        assert await view_ids(ADMIN, just['view']) == holding[1], just
        # (b) Each value as returned, written back into an `=` filter and parsed by the server: its counted samples.
        for item in whole_lists['values']:
            value = item['value']
            text = f'kinds = {json.dumps(value)}'
            response = await w.call(
                ADMIN,
                'POST',
                f'/api/datasets/{LISTS}/filter_query/parse',
                json={'query': text, 'current': []},
            )
            if response.status_code == 400:  # the language can't write this value into `=`: (a) is the check then
                continue
            assert response.status_code == 200, (text, response.text)
            assert await api_ids(w, ADMIN, LISTS, response.json()['filter']) == holding[item['count']], text
        for field in ('tags', 'pests'):
            elements = await result(ADMIN, 'field_values', {'dataset': LISTS, 'field': field})
            assert elements['values'] == [
                {'value': 'a', 'count': 5},
                {'value': 'b', 'count': 4},
                {'value': 'z', 'count': 2},
                {'value': 'c', 'count': 1},
            ], (field, elements['values'])
            assert (elements['distinct'], elements['samples_with_value']) == (4, 8), (field, elements)

    world(scenario)


def quantile_choices(values: list[float], p: float) -> tuple[float, float]:
    ordered = sorted(values)
    position = p * (len(ordered) - 1)
    low, high = math.floor(position), math.ceil(position)
    interpolated = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    nearest = ordered[max(0, math.ceil(p * len(ordered)) - 1)]
    return interpolated, nearest


def check_histogram(histogram: list[dict[str, Any]], values: list[float]) -> None:
    assert 1 <= len(histogram) <= 20, histogram
    assert sum(item['count'] for item in histogram) == len(values), histogram  # every sample with a value
    for left, right in zip(histogram, histogram[1:]):
        assert left['to'] == right['from'], histogram
    assert histogram[0]['from'] <= min(values) and histogram[-1]['to'] >= max(values), histogram
    for item in histogram:
        inside = sum(1 for value in values if item['from'] < value < item['to'])
        edges = sum(1 for value in values if value in (item['from'], item['to']))
        assert inside <= item['count'] <= inside + edges, (item, histogram)


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def test_field_summary_gives_exact_numbers_over_the_view(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        for text, keep in (('', everything), ('state = Kansas', lambda r: r['state'] == 'Kansas')):
            view = await make(ADMIN, V, text)
            rows = f.rows(keep)
            values = [row['brightness'] for row in rows if 'brightness' in row]
            summary = await result(ADMIN, 'field_summary', {'view': view['view'], 'field': 'brightness'})
            assert (summary['field'], summary['type']) == ('brightness', 'number'), summary
            assert (summary['samples_with_value'], summary['samples_without']) == (len(values), len(rows) - len(values))
            assert (summary['min'], summary['max']) == (min(values), max(values)), summary
            assert math.isclose(summary['mean'], sum(values) / len(values), rel_tol=1e-9), summary
            assert set(summary['quantiles']) == {'p5', 'p25', 'p50', 'p75', 'p95'}, summary
            for key, value in summary['quantiles'].items():
                choices = quantile_choices(values, int(key[1:]) / 100)
                assert any(math.isclose(value, choice, rel_tol=1e-9, abs_tol=1e-9) for choice in choices), (key, value)
            check_histogram(summary['histogram'], values)
            # A datetime: no mean.
            times = [instant(row['seen_at']) for row in rows if 'seen_at' in row]
            summary = await result(ADMIN, 'field_summary', {'view': view['view'], 'field': 'seen_at'})
            assert 'mean' not in summary and summary['type'] == 'datetime', summary
            assert (summary['samples_with_value'], summary['samples_without']) == (len(times), len(rows) - len(times))
            assert (instant(summary['min']), instant(summary['max'])) == (min(times), max(times)), summary
            assert 1 <= len(summary['histogram']) <= 20 and sum(b['count'] for b in summary['histogram']) == len(times)
            # Text, list and boolean fields: counts and the five commonest.
            for field in ('state', 'tags', 'flag', 'serial'):
                expected = tally(rows, field)
                summary = await result(ADMIN, 'field_summary', {'view': view['view'], 'field': field})
                assert (summary['samples_with_value'], summary['samples_without'], summary['distinct']) == (
                    having(rows, field),
                    len(rows) - having(rows, field),
                    len(expected),
                ), (field, summary)
                assert summary['top'] == expected[:5], (field, summary['top'])
                assert not {'min', 'max', 'mean', 'quantiles', 'histogram'} & set(summary), summary

    world(scenario)


# --- refine_view ---


def test_refine_view_applies_remove_negate_add_and_the_lists_in_that_order(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        start = await make(ADMIN, V, 'state != Nebraska AND brightness < 80 AND tags in (dry)')
        owners = await result(
            ADMIN,
            'field_values',
            {'view': (await make(ADMIN, V, 'state = Nebraska'))['view'], 'field': 'owner_code'},
        )
        salaries = await result(
            ADMIN,
            'field_values',
            {'view': (await make(ADMIN, V, 'brightness > 60'))['view'], 'field': 'salary'},
        )
        codes = {row['owner_code'] for row in ROWS if row['state'] == 'Nebraska' and 'owner_code' in row}
        paid = {row['salary'] for row in ROWS if row.get('brightness', 0) > 60 and 'salary' in row}
        # Stage 2 removed, stage 1 turned around (the same stage whichever numbering the negate reads), then the
        # text, then the two lists, in that order.
        refined = await refine(
            ADMIN,
            start,
            remove=[2],
            negate=[1],
            add='flag exists',
            in_list={'field': 'owner_code', 'list': owners['list']},
            not_in_list={'field': 'salary', 'list': salaries['list']},
        )
        texts = [stage['text'] for stage in refined['stages']]
        assert len(texts) == 5, texts
        assert texts[0] != start['stages'][0]['text'] and texts[1] == start['stages'][2]['text'], texts
        assert texts[2] == (await parsed(w, ADMIN, V, 'flag exists'))['parts'][0], texts
        assert texts[3:] == [f'owner_code in ({len(codes)} {KEPT})', f'salary not in ({len(paid)} {KEPT})'], texts
        expected = (
            f.of(lambda r: r['state'] == 'Nebraska' and 'dry' in r['tags'] and 'flag' in r)
            & await app_selects(w, V, 'owner_code', 'in', owners)
            & await app_selects(w, V, 'salary', 'not in', salaries)
        )
        assert expected == {f.ids[i] for i in (4, 9, 14)}  # the data selects something at every step
        assert refined['count'] == len(expected) and await view_ids(ADMIN, refined['view']) == expected
        # Negating a stage twice gives back the selection.
        once = await refine(ADMIN, start, negate=[2])
        twice = await refine(ADMIN, once, negate=[2])
        original = f.of(lambda r: r['state'] != 'Nebraska' and r.get('brightness', 99) < 80 and 'dry' in r['tags'])
        assert await view_ids(ADMIN, twice['view']) == await view_ids(ADMIN, start['view']) == original
        # Rewritten for contract g7 (one rule everywhere, rule 1.2): the negated stage keeps only samples that have a
        # brightness (27 to 29 have none; it kept 27 and 28).
        assert await view_ids(ADMIN, once['view']) == f.of(
            lambda r: (
                r['state'] != 'Nebraska' and 'brightness' in r and not r['brightness'] < 80 and 'dry' in r['tags']
            ),
        )
        # Appended stages take the next numbers.
        more = await refine(ADMIN, start, add='flag = true AND serial exists')
        assert [stage['n'] for stage in more['stages']] == [1, 2, 3, 4, 5], more

    world(scenario)


def test_what_refine_view_cant_do_is_invalid_and_makes_no_view(world):
    async def scenario(w):
        await farm(w)
        await farm(w, ELSEWHERE)
        await open_door(w)
        kept = await kept_tables(w, ADMIN, V)
        view = await make(ADMIN, V, 'state = Kansas AND brightness < 60')
        theirs = await result(ADMIN, 'field_values', {'dataset': ELSEWHERE, 'field': 'serial'})
        before = await counted(w, kept)
        wrongs: list[dict[str, Any]] = [
            {'remove': [3]},
            {'negate': [0]},
            {'remove': [1], 'negate': [7]},
            {},
            {'in_list': {'field': 'serial', 'list': theirs['list']}},  # a list made for another dataset
            {'not_in_list': {'field': 'serial', 'list': theirs['list']}},
        ]
        for change in wrongs:
            wrong = await error(ADMIN, 'refine_view', {'view': view['view'], **change})
            assert wrong['code'] == 'invalid', (change, wrong)
        for name, arguments in (
            ('make_view', {}),
            ('field_values', {'view': view['view'], 'dataset': V, 'field': 'state'}),
            ('field_values', {'field': 'state'}),
            ('field_summary', {'view': view['view'], 'dataset': V, 'field': 'state'}),
            ('field_summary', {'field': 'state'}),
            ('get_samples', {'view': view['view'], 'dataset': V}),
            ('get_samples', {}),
        ):
            assert (await error(ADMIN, name, arguments))['code'] == 'invalid', (name, arguments)
        assert await counted(w, kept) == before

    world(scenario)


def test_filter_text_the_server_cant_take_is_invalid_with_its_details_and_makes_no_view(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        kept = await kept_tables(w, ADMIN, V)
        whole = await make(ADMIN, V)
        before = await counted(w, kept)
        for text in (
            'seriel = 1',
            'state = Kansas\nAND brightnes < 3',
            'brightness > abc',
            'state = Kansas AND (',
            'concept "nope" > 0.5',
        ):
            response = await w.call(
                ADMIN,
                'POST',
                f'/api/datasets/{V}/filter_query/parse',
                json={'query': text, 'current': []},
            )
            assert response.status_code == 400, response.text
            server = response.json()['detail']['errors'][0]
            for name, arguments in (
                ('make_view', {'dataset': V, 'filter': text}),
                ('refine_view', {'view': whole['view'], 'add': text}),
            ):
                wrong = await error(ADMIN, name, arguments)
                assert wrong['code'] == 'invalid' and server['message'] in wrong['message'], (text, wrong, server)
                assert wrong['details'] == {key: server[key] for key in ('line', 'column', 'suggestions')}, (
                    wrong,
                    server,
                )
        assert await counted(w, kept) == before

    world(scenario)


# --- 3. Value lists ---


def test_a_value_list_is_the_whole_list_as_it_was_when_the_tool_answered(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        whole = await make(ADMIN, V)
        iowa = await make(ADMIN, V, 'state = Iowa')
        serials = await result(ADMIN, 'field_values', {'view': iowa['view'], 'field': 'serial', 'limit': 2})
        assert len(serials['values']) == 2 and serials['distinct'] == 6, serials
        listed = await refine(ADMIN, whole, in_list={'field': 'serial', 'list': serials['list']})
        assert await view_ids(ADMIN, listed['view']) == await app_selects(w, V, 'serial', 'in', serials)
        assert await app_selects(w, V, 'serial', 'in', serials) == f.of(lambda r: r['state'] == 'Iowa')  # all six
        assert sorted(csv_rows(await w.call(ADMIN, 'GET', urlsplit(serials['download']).path))) == sorted(
            (row['serial'], 1) for row in ROWS if row['state'] == 'Iowa'
        )
        # Kept as the field was: a value changed afterwards changes neither the list nor its download.
        dim = await make(ADMIN, V, 'brightness < 20')
        states = await result(ADMIN, 'field_values', {'view': dim['view'], 'field': 'state'})
        then = sorted(
            (item['value'], item['count']) for item in tally(f.rows(lambda r: r.get('brightness', 99) < 20), 'state')
        )
        moved = f.ids[3]  # an Iowa sample under 20
        changed = await w.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{V}/samples/{moved}',
            json={'metadata': {'state': 'Utah'}},
        )
        assert changed.status_code == 200, changed.text
        assert await w.database.fetchval("SELECT metadata->>'state' FROM samples WHERE id = $1", moved) == 'Utah'
        assert sorted(csv_rows(await w.call(ADMIN, 'GET', urlsplit(states['download']).path))) == then
        by_state = await refine(ADMIN, whole, in_list={'field': 'state', 'list': states['list']})
        now = await app_selects(w, V, 'state', 'in', states)  # the values it had, as the samples hold them now
        assert moved not in now and await view_ids(ADMIN, by_state['view']) == now

    world(scenario)


def test_more_than_ten_thousand_values_are_counted_and_downloaded_but_not_kept_for_filtering(world):
    async def scenario(w):
        await w.dataset(BIG)
        rows: list[dict[str, Any]] = [
            {
                'filepath': f'big-{n:05d}.jpg',
                'metadata': {'code': f'C{n:05d}', **({'code10k': f'C{n:05d}'} if n < 10_000 else {})},
            }
            for n in range(10_001)
        ]
        for start in range(0, len(rows), 2000):
            await add(BIG, rows[start : start + 2000])
        await open_door(w)
        over = await result(ADMIN, 'field_values', {'dataset': BIG, 'field': 'code'})
        assert over['list'] is None and over['distinct'] == 10_001, {k: v for k, v in over.items() if k != 'values'}
        note = over['list_note']
        assert ('10001' in note or '10,001' in note) and ('narrow' in note.lower() or 'search' in note.lower()), note
        assert len(csv_rows(await w.call(ADMIN, 'GET', urlsplit(over['download']).path))) == 10_001
        at = await result(ADMIN, 'field_values', {'dataset': BIG, 'field': 'code10k'})
        assert at['list'] is not None and at['distinct'] == 10_000 and 'list_note' not in at, at['distinct']
        whole = await make(ADMIN, BIG)
        listed = await refine(ADMIN, whole, in_list={'field': 'code10k', 'list': at['list']})
        assert listed['count'] == 10_000, listed['count']
        assert 'w' in parse_qs(urlsplit(listed['link']).query), listed['link']  # far too long for an address
        stages = await stages_of(w, ADMIN, listed['view'])
        assert await api_total(w, ADMIN, BIG, {'logic': 'AND', 'filters': stages}) == 10_000

    world(scenario)


def test_the_download_is_a_csv_of_every_value_for_its_user_only(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        notes = await result(ADMIN, 'field_values', {'dataset': V, 'field': 'note'})
        path = urlsplit(notes['download']).path
        expected = [('plain', 2), ('a,"b"', 1)]
        assert sorted(csv_rows(await w.call(ADMIN, 'GET', path))) == sorted(expected)  # quoted as CSV quotes
        async with signed_in(w, ADMIN) as browser:
            assert sorted(csv_rows(await browser.get(path))) == sorted(expected)
        for user_id in (OTHER_ADMIN, POWER, GUEST):
            assert (await w.call(user_id, 'GET', path)).status_code == 404, user_id
        assert (await w.call(None, 'GET', path)).status_code == 401
        made = await w.call(ADMIN, 'POST', '/api/tokens', json={'name': 'views-mcp', 'scope': 'mcp'})
        if made.status_code == 200 and made.json().get('scope') == 'mcp':  # only where the mcp token kind exists
            raw = made.json()['token']
            refused = await w.client.get(path, headers={'Authorization': f'Bearer {raw}'})
            assert refused.status_code in (401, 403), refused.text

    world(scenario)


# --- 4. Links and the view endpoint ---


PUBLIC = 'https://fields.example.org'


@pytest.mark.parametrize(
    ('environment', 'address'),
    [
        ({'FRIDGE_PUBLIC_URL': PUBLIC, 'ALLOWED_ORIGINS': 'https://other.example.org'}, PUBLIC),
        ({'ALLOWED_ORIGINS': f'{PUBLIC},https://second.example.org'}, PUBLIC),
    ],
)
def test_links_open_exactly_the_views_samples_from_the_servers_address(world, monkeypatch, environment, address):
    """The address is the configured one (FRIDGE_PUBLIC_URL, else the first allowed origin), whatever host the
    requests come in on."""
    for name in ('FRIDGE_PUBLIC_URL', 'ALLOWED_ORIGINS'):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        await settings(w, {'sample_fields': True})
        origins = set()
        # Too long for an address: a ?w= link, with the note, and the person's stages select the view.
        serials = ' '.join(f'"SN-{n:04d}"' for n in range(5, 600))
        long_view = await make(ADMIN, V, f'serial in ({serials})')
        assert 'w' in parse_qs(urlsplit(long_view['link']).query), long_view['link']
        assert long_view['count'] == N - 5
        assert await opened(w, ADMIN, long_view) == f.of(lambda r: r['i'] >= 5)
        origins.add(origin(long_view['link']))
        # A short list on a field whose values aren't private is text: ?q=, which anyone who can open the dataset opens.
        iowa = await make(ADMIN, V, 'state = Iowa')
        serial_list = await result(ADMIN, 'field_values', {'view': iowa['view'], 'field': 'serial'})
        listed = await refine(
            ADMIN,
            await make(ADMIN, V, 'brightness < 90'),
            in_list={'field': 'serial', 'list': serial_list['list']},
        )
        expected = f.of(lambda r: r.get('brightness', 99) < 90) & await app_selects(w, V, 'serial', 'in', serial_list)
        assert 'q' in parse_qs(urlsplit(listed['link']).query), listed['link']
        assert await opened(w, POWER, listed) == expected
        # Every view answer, every sample's link and every download start with the same address.
        for view in (iowa, listed):
            origins.add(origin(view['link']))
            page = await result(ADMIN, 'get_samples', {'view': view['view'], 'limit': 50})
            for sample in page['samples']:
                origins.add(origin(sample['link']))
                link = urlsplit(sample['link'])
                assert link.path.endswith(f'/datasets/{V}') and parse_qs(link.query) == {'sample': [str(sample['id'])]}
        origins.add(origin(serial_list['download']))
        assert origins == {address}, origins
        for link in (long_view['link'], iowa['link'], listed['link'], serial_list['download']):
            assert link.startswith(address + '/'), link

    world(scenario)


def test_the_view_endpoint_answers_its_user_alone(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        view = await make(POWER, V, 'state = Kansas AND brightness < 60')
        expected = f.of(lambda r: r['state'] == 'Kansas' and r.get('brightness', 99) < 60)
        path = f'/api/assistant/views/{view["view"]}'
        by_token = await w.call(POWER, 'GET', path)
        assert by_token.status_code == 200, by_token.text
        body = by_token.json()
        assert set(body) == {'dataset_id', 'stages', 'created_at', 'expires_at'} and body['dataset_id'] == V, body
        now = datetime.now(timezone.utc)
        assert instant(body['created_at']) <= now + timedelta(minutes=1), body
        assert now + timedelta(days=29) < instant(body['expires_at']) <= now + timedelta(days=30, minutes=1), body
        assert len(body['stages']) == 2
        assert (
            await api_ids(w, POWER, V, {'logic': 'AND', 'filters': [s['filter'] for s in body['stages']]}) == expected
        )
        async with signed_in(w, POWER) as browser:
            by_session = await browser.get(path)
            assert by_session.status_code == 200 and by_session.json()['stages'] == body['stages'], by_session.text
        for user_id in (ADMIN, OTHER_POWER, GUEST):
            someone_else = await w.call(user_id, 'GET', path)
            assert someone_else.status_code == 404 and detail(someone_else) == SOMEONE_ELSES, (
                user_id,
                someone_else.text,
            )
        assert (await w.call(None, 'GET', path)).status_code == 401
        made = await w.call(POWER, 'POST', '/api/tokens', json={'name': 'views-mcp', 'scope': 'mcp'})
        if made.status_code == 200 and made.json().get('scope') == 'mcp':  # only where the mcp token kind exists
            refused = await w.client.get(path, headers={'Authorization': f'Bearer {made.json()["token"]}'})
            assert refused.status_code in (401, 403), refused.text

    world(scenario)


# --- get_samples and the sample_fields setting ---


def test_get_samples_pages_and_sorts_and_shows_fields_only_when_an_administrator_allows(world):
    async def scenario(w):
        f = await farm(w)
        await open_door(w)
        bright = await make(ADMIN, V, 'brightness exists')
        ranked = sorted(f.rows(lambda r: 'brightness' in r), key=lambda r: -r['brightness'])
        order = [f.ids[row['i']] for row in ranked]
        seen: list[int] = []
        offset: Optional[int] = 0
        while offset is not None:
            page = await result(
                ADMIN,
                'get_samples',
                {'view': bright['view'], 'limit': 7, 'offset': offset, 'sort_by': 'brightness', 'descending': True},
            )
            assert set(page) == {'samples', 'total', 'offset', 'next_offset'} and page['total'] == len(order), page
            assert all(set(sample) == {'id', 'link'} for sample in page['samples']), page
            seen += [sample['id'] for sample in page['samples']]
            offset = page['next_offset']
        assert seen == order
        default = await result(ADMIN, 'get_samples', {'dataset': V})
        assert [s['id'] for s in default['samples']] == sorted(f.ids)[:10] and default['next_offset'] == 10, default
        for limit in (0, 51):
            assert (await error(ADMIN, 'get_samples', {'dataset': V, 'limit': limit}))['code'] == 'invalid'
        # Off, as shipped: asking for fields is refused, saying an administrator hasn't allowed it.
        refused = await error(ADMIN, 'get_samples', {'dataset': V, 'fields': ['state']})
        assert refused['code'] == 'refused' and 'administrator' in refused['message'].lower(), refused
        # On: the fields asked for, the file path when asked, and a private field withheld by name.
        await settings(w, {'sample_fields': True})
        asked = ['state', 'brightness', 'tags', 'filepath', *PRIVATE]
        page = await result(ADMIN, 'get_samples', {'view': bright['view'], 'limit': 50, 'fields': asked})
        withheld = page.get('withheld')
        for sample in page['samples']:
            row = ROWS[f.ids.index(sample['id'])]
            shown = (
                sample['fields']
                if isinstance(sample.get('fields'), dict)
                else {k: v for k, v in sample.items() if k not in ('id', 'link', 'withheld')}
            )
            assert not set(PRIVATE) & set(shown), shown
            assert shown['state'] == row['state'] and shown['brightness'] == row['brightness'], (shown, row)
            assert sorted(shown['tags']) == sorted(row['tags']) and shown['filepath'] == f'v{V}-{row["i"]:02d}.jpg'
            assert sorted(withheld if withheld is not None else sample['withheld']) == sorted(PRIVATE), (page, sample)

    world(scenario)


def test_sample_fields_is_a_setting_for_administrators(world):
    async def scenario(w):
        state = await settings(w)
        assert state['sample_fields'] is False, state
        assert (await settings(w, {'sample_fields': True}))['sample_fields'] is True
        assert (await settings(w, {'mcp_enabled': True}))['sample_fields'] is True  # left out, unchanged
        for user_id, status in ((POWER, 403), (GUEST, 403), (None, 401)):
            response = await w.call(user_id, 'PATCH', SETTINGS, json={'sample_fields': False})
            assert response.status_code == status, (user_id, response.text)
        assert (await settings(w))['sample_fields'] is True

    world(scenario)


# --- The core's section 2, for the five tools ---


def test_the_five_tools_answer_each_user_what_the_api_answers_them(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        text = 'state = Kansas AND brightness < 60'
        node = (await parsed(w, ADMIN, V, text))['filter']
        calls: list[tuple[str, dict[str, Any]]] = [
            ('make_view', {'dataset': V, 'filter': text}),
            ('field_values', {'dataset': V, 'field': 'state'}),
            ('field_summary', {'dataset': V, 'field': 'brightness'}),
            ('get_samples', {'dataset': V}),
        ]
        # A let-in power user with the guest's view and nothing else is checked in the guest's place; the guest, whom
        # the MCP door never lets in, is refused (below).
        await w.user(VIEWER, 'power_user')
        cases = [
            (ADMIN, None),
            (VIEWER, 'view'),
            (OTHER_POWER, None),
            *[(POWER, level) for level in LEVELS],
        ]
        for user_id, level in cases:
            if user_id in (POWER, VIEWER):
                await set_share(w, V, user_id, level)
            response = await w.call(
                user_id,
                'POST',
                f'/api/datasets/{V}/samples/query',
                json={'limit': 1000, 'filters': node},
            )
            if response.status_code == 403:
                for name, arguments in calls:
                    refused = await error(user_id, name, arguments)
                    assert (refused['code'], refused['message']) == ('refused', detail(response)), (user_id, name)
                continue
            assert response.status_code == 200, response.text
            items = response.json()['items']
            view = await make(user_id, V, text)
            assert view['count'] == len(items) and await view_ids(user_id, view['view']) == {i['id'] for i in items}
            states = await result(user_id, 'field_values', {'view': view['view'], 'field': 'state'})
            assert states['values'] == [{'value': 'Kansas', 'count': len(items)}], (user_id, states)
            everything_ = await result(user_id, 'get_samples', {'dataset': V})
            assert everything_['total'] == await api_total(w, user_id, V, None), user_id
        # The API answers the guest, who can view the dataset; the MCP door never lets a guest in, whatever the
        # settings tick.
        assert await api_total(w, GUEST, V, None) == N
        for name, arguments in calls:
            refused = await error(GUEST, name, arguments)
            assert refused == {'code': 'not_allowed', 'message': NOT_LET_IN, 'retryable': False}, name
        # Closed to the assistant: every tool refuses, for every user; the API still answers them.
        assert (await w.call(ADMIN, 'PATCH', f'/api/datasets/{V}', json={'assistant_closed': True})).status_code == 200
        for user_id in (ADMIN, POWER):
            for name, arguments in calls:
                refused = await error(user_id, name, arguments)
                assert (refused['code'], refused['message']) == ('refused', CLOSED), (user_id, name, refused)
            assert await api_total(w, user_id, V, None) == N

    world(scenario)


def test_nothing_the_seven_tools_do_writes(world):
    async def scenario(w):
        await farm(w)
        await open_door(w)
        await settings(w, {'sample_fields': True})
        before = await snapshot(w)
        view = await make(ADMIN, V, 'state = Kansas')
        listed = await result(ADMIN, 'field_values', {'view': view['view'], 'field': 'serial'})
        valid: dict[str, dict[str, Any]] = {
            'list_datasets': {},
            'describe_dataset': {'dataset': V},
            'make_view': {'dataset': V, 'filter': 'brightness < 40'},
            'refine_view': {
                'view': view['view'],
                'add': 'flag = true',
                'in_list': {'field': 'serial', 'list': listed['list']},
            },
            'field_values': {'view': view['view'], 'field': 'state'},
            'field_summary': {'view': view['view'], 'field': 'brightness'},
            'get_samples': {'view': view['view'], 'fields': ['state', 'owner_code'], 'sort_by': 'brightness'},
        }
        tools = {tool.name: tool for tool in assistant.tools()}
        for name, arguments in valid.items():
            for user_id in (ADMIN, POWER, OTHER_POWER):
                await call(user_id, name, arguments)
            for argument in plain(tools[name].input_schema).get('properties', {}):
                for value in HOSTILE:
                    await call(ADMIN, name, {**arguments, argument: value})
        assert await snapshot(w) == before

    world(scenario)


# --- too_slow ---

LIMIT_MS = 1500


@pytest.fixture
def limited_world(database, monkeypatch):
    """A World whose pool has a short statement time limit (the server's own, as an operator could set it)."""
    make = database[0]

    def run(scenario):
        name = make((ROOT / 'backend/db/schema.sql').read_text())
        url = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()
        monkeypatch.setattr(database_module, 'DATABASE_URL', url)
        monkeypatch.setattr(datasets, '_array_columns_cache', None)
        monkeypatch.setattr(datasets, 'require_free_space', AsyncMock())
        monkeypatch.setattr(datasets, 'validate_embedding_dims', AsyncMock())

        async def body() -> None:
            pool = await asyncpg.create_pool(
                url,
                min_size=1,
                max_size=8,
                init=init_connection,
                server_settings={'statement_timeout': str(LIMIT_MS), **PLANNING},
            )
            monkeypatch.setattr(database_module.db, 'pool', pool)
            database_ = Database()
            database_.pool = pool
            try:
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                    w = World(database_, client)
                    for user_id, role in ROLES.items():
                        await w.user(user_id, role)
                    await scenario(w, url)
            finally:
                await pool.close()

        asyncio.run(body())

    return run


def test_a_count_that_runs_into_the_time_limit_is_too_slow_and_never_partial(limited_world):
    async def scenario(w, url):
        await farm(w, statistics=False)  # nothing here reads the counted statistics
        await open_door(w)
        view = await make(ADMIN, V, 'state = Kansas')
        holder = await asyncpg.connect(url)
        try:
            for name, arguments in (
                ('field_values', {'view': view['view'], 'field': 'state'}),
                ('field_summary', {'dataset': V, 'field': 'brightness'}),
                ('make_view', {'dataset': V, 'filter': 'state = Iowa'}),
            ):
                async with holder.transaction():
                    await holder.execute('LOCK TABLE samples IN ACCESS EXCLUSIVE MODE')
                    answer = await asyncio.wait_for(call(ADMIN, name, arguments), timeout=8 * LIMIT_MS / 1000)
                assert not answer['ok'], (name, answer)
                slow = answer['error']
                assert (slow['code'], slow['retryable']) == ('too_slow', False), (name, slow)
                assert 'narrow' in slow['message'].lower() or 'index' in slow['message'].lower(), slow
        finally:
            await holder.close()

    limited_world(scenario)


# --- Region and concept stages ---


def test_region_and_concept_stages_count_exactly(regions_world):
    from backend.tests.test_regions_spec import LOCATED, POINTS, D, inside, loaded

    rank = {name: index for index, (name, _, _, _) in enumerate(POINTS, start=1)}

    def score(name: str) -> float:
        return (rank[name] % 10) / 10

    async def scenario(r):
        await test_regions_spec.farm(r)
        await loaded(r)
        names = list(r.ids)
        vectors = [
            {'sample_id': r.ids[n], 'vector': [1.0, i / 100, 0.0, 0.0], 'field_name': 'clip'}
            for i, n in enumerate(names)
        ]
        assert (await r.call(ADMIN, 'POST', f'/api/datasets/{D}/embeddings', json=vectors)).is_success
        concept = await r.database.fetchval(
            "INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) "
            "VALUES ($1, 'weeds', 'clip', 'seed', '[1,0,0,0]') RETURNING id",
            D,
        )
        await r.database.execute(
            'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) '
            'SELECT $1, unnest($2::bigint[]), unnest($3::float8[])',
            concept,
            [r.ids[n] for n in names],
            [score(n) for n in names],
        )
        await open_door(r)
        grp = {name: 'a' if rank[name] % 2 else 'b' for name in names}
        for text, keep, first_text in (
            ('location within Isla', lambda n: n in inside('XA-IS'), 'location within "XA-IS"'),
            # Rewritten for contract g7 (one rule everywhere, rule 1.6): only samples that have a location (p21, in
            # group a, has none; it was kept).
            (
                'location not within "XA" AND grp = a',
                lambda n: n in LOCATED and n not in inside('XA') and grp[n] == 'a',
                None,
            ),
            ('concept weeds > 0.5', lambda n: score(n) > 0.5, None),
            (
                'not concept weeds > 0.5 AND location within "XA:midlands"',
                lambda n: not score(n) > 0.5 and n in inside('XA:midlands'),
                None,
            ),
        ):
            view = check_view(await result(ADMIN, 'make_view', {'dataset': D, 'filter': text}), D)
            expected = r.of({n for n in names if keep(n)})
            assert view['count'] == len(expected), (text, view)
            assert await view_ids(ADMIN, view['view']) == expected, text
            assert await opened(r, ADMIN, view) == expected, text
            if first_text:
                assert view['stages'][0]['text'] == first_text, view  # a region written by name comes back by key
            groups = await result(ADMIN, 'field_values', {'view': view['view'], 'field': 'grp'})
            counts = {g: sum(1 for n in names if keep(n) and grp[n] == g) for g in ('a', 'b')}
            ordered = sorted((g for g in counts if counts[g]), key=lambda g: (-counts[g], g))
            assert groups['values'] == [{'value': g, 'count': counts[g]} for g in ordered], (text, groups)

    regions_world(scenario)


# --- 1.7 A view is of the samples, not the object rows ---

LABELS = ('tractor', 'weed')


async def farm_with_objects(r: Any) -> tuple[Farm, list[int]]:
    """ROWS as images (the first 20 at the region tests' points), the concept scoring them, and under image i, i % 3
    objects: each carries its image's fields with the same values, its location and its score, and fields of its own
    (label, area)."""
    await r.dataset(OBJECTS)
    points = test_regions_spec.POINTS

    def carried(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row[key] for key in METADATA if key in row}

    def placed(sample: dict[str, Any], i: int) -> dict[str, Any]:
        if i < 20:
            sample['location'] = {'type': 'Point', 'coordinates': [points[i][1], points[i][2]]}
        return sample

    await add(
        OBJECTS,
        [
            placed({'filepath': f'o-{row["i"]:02d}.jpg', 'tags': row['tags'], 'metadata': carried(row)}, row['i'])
            for row in ROWS
        ],
    )
    by_path = {
        item['filepath']: item['id']
        for item in await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', OBJECTS)
    }
    ids = [by_path[f'o-{i:02d}.jpg'] for i in range(N)]
    objects = [
        placed(
            {
                'filepath': f'o-{row["i"]:02d}-{k}.jpg',
                'media_type': 'object',
                'tags': row['tags'],
                'metadata': {
                    **carried(row),
                    'parent_id': ids[row['i']],
                    'label': LABELS[(row['i'] + k) % 2],
                    'area': 10.0 * (k + 1),
                },
            },
            row['i'],
        )
        for row in ROWS
        for k in range(row['i'] % 3)
    ]
    await add(OBJECTS, objects)
    rows = await r.database.fetch(
        "SELECT id, parent_id AS parent FROM samples WHERE dataset_id = $1 AND media_type = 'object'",
        OBJECTS,
    )
    assert len(rows) == len(objects) and {row['parent'] for row in rows} <= set(ids), rows
    vectors = [{'sample_id': s, 'vector': [1.0, i / 100, 0.0, 0.0], 'field_name': 'clip'} for i, s in enumerate(ids)]
    assert (await r.call(ADMIN, 'POST', f'/api/datasets/{OBJECTS}/embeddings', json=vectors)).is_success
    concept = await r.database.fetchval(
        "INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) "
        "VALUES ($1, 'weeds', 'clip', 'seed', '[1,0,0,0]') RETURNING id",
        OBJECTS,
    )
    score_of = {ids[row['i']]: row['score'] for row in ROWS}
    scored = [*score_of, *(row['id'] for row in rows)]
    scores = [*score_of.values(), *(score_of[row['parent']] for row in rows)]  # an object scores as its image
    await r.database.execute(
        'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) '
        'SELECT $1, unnest($2::bigint[]), unnest($3::float8[])',
        concept,
        scored,
        scores,
    )
    for field in PRIVATE:
        marked = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{OBJECTS}/fields/{field}/description',
            json={'values_private': True},
        )
        assert marked.status_code == 200, marked.text
    return Farm(ids), [row['id'] for row in rows]


def test_a_view_counts_the_images_never_the_object_rows(regions_world):
    """Every number a tool gives for a view is over its images, what the dataset page shows (its image grain), and the
    whole dataset counts what describe_dataset calls samples."""

    async def scenario(r):
        f, object_ids = await farm_with_objects(r)
        await test_regions_spec.loaded(r)
        await open_door(r)
        names = [point[0] for point in test_regions_spec.POINTS]
        in_aldoria = test_regions_spec.inside('XA')
        described = (await result(ADMIN, 'describe_dataset', {'dataset': OBJECTS}))['dataset']
        assert (described['samples'], described['objects']) == (N, len(object_ids)), described
        whole = await make(ADMIN, OBJECTS)
        assert whole['count'] == described['samples'], whole
        assert await opened(r, ADMIN, whole, grain=True) == set(f.ids)
        iowa = await make(ADMIN, OBJECTS, 'state = Iowa')
        serials = await result(ADMIN, 'field_values', {'view': iowa['view'], 'field': 'serial'})
        iowa_serials = {row['serial'] for row in ROWS if row['state'] == 'Iowa'}
        views: list[tuple[str, dict[str, Any], Callable[[dict[str, Any]], bool]]] = [
            ('a comparison', await make(ADMIN, OBJECTS, 'state = Kansas'), lambda row: row['state'] == 'Kansas'),
            (
                'a region',
                await make(ADMIN, OBJECTS, 'location within "XA"'),
                lambda row: row['i'] < 20 and names[row['i']] in in_aldoria,
            ),
            ('a concept', await make(ADMIN, OBJECTS, 'concept weeds > 0.5'), lambda row: row['score'] > 0.5),
            (
                'a value list',
                await refine(ADMIN, whole, in_list={'field': 'serial', 'list': serials['list']}),
                lambda row: row['serial'] in iowa_serials,
            ),
            ('a negated stage', await refine(ADMIN, iowa, negate=[1]), lambda row: row['state'] != 'Iowa'),
            (
                'two stages',
                await make(ADMIN, OBJECTS, 'state = Kansas AND brightness < 60'),
                lambda row: row['state'] == 'Kansas' and row.get('brightness', 99) < 60,
            ),
        ]
        objects = set(object_ids)
        for name, view, keep in views:
            rows, expected = f.rows(keep), f.of(keep)
            assert view['count'] == len(expected), (name, view['count'], len(expected))
            assert await opened(r, ADMIN, view, grain=True) == expected, name  # what the dataset page shows
            shown = await view_ids(ADMIN, view['view'])
            assert shown == expected and not shown & objects, name
            for field in ('state', 'serial', 'tags', 'brightness'):
                values = await result(ADMIN, 'field_values', {'view': view['view'], 'field': field, 'limit': 50})
                counted = tally(rows, field)
                assert values['values'] == counted, (name, field, values['values'])
                assert (values['distinct'], values['samples_with_value']) == (len(counted), having(rows, field))
            listed = await result(ADMIN, 'field_values', {'view': view['view'], 'field': 'serial', 'limit': 50})
            download = csv_rows(await r.call(ADMIN, 'GET', urlsplit(listed['download']).path))
            assert sorted(download) == sorted((item['value'], item['count']) for item in tally(rows, 'serial')), name
            brightness = [row['brightness'] for row in rows if 'brightness' in row]
            summary = await result(ADMIN, 'field_summary', {'view': view['view'], 'field': 'brightness'})
            assert (summary['samples_with_value'], summary['samples_without']) == (
                len(brightness),
                len(rows) - len(brightness),
            )
            if brightness:
                assert (summary['min'], summary['max']) == (min(brightness), max(brightness)), (name, summary)
                assert math.isclose(summary['mean'], sum(brightness) / len(brightness), rel_tol=1e-9), (name, summary)
                check_histogram(summary['histogram'], brightness)
            page = await result(ADMIN, 'get_samples', {'view': view['view'], 'limit': 50})
            assert page['total'] == len(expected) and {item['id'] for item in page['samples']} <= set(f.ids), name
        # A field only objects carry: none of the images has it, and a filter on it is what the page shows.
        labels = await result(ADMIN, 'field_values', {'view': whole['view'], 'field': 'label'})
        assert (labels['values'], labels['distinct'], labels['samples_with_value']) == ([], 0, 0), labels
        for text in ('label = tractor', 'label exists', 'area > 15'):
            view = await make(ADMIN, OBJECTS, text)
            page_shows = await opened(r, ADMIN, view, grain=True)
            assert view['count'] == len(page_shows), (text, view['count'], len(page_shows))
            assert await view_ids(ADMIN, view['view']) == page_shows, text

    regions_world(scenario)
