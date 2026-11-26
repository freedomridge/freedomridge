"""Read-only administration tools for the assistant (milestone 2): system_status, backup_status, list_jobs and
index_advice, for administrators only, behind a switch of their own (`admin_tools`) that is off as shipped; and the
route list_jobs stands on, GET /api/admin/jobs/all.

Written from the contract (m2-contract-admin-read-tools, standing on m1-contract-assistant-core, with the survey of the
administrators' routes beside it), not from the code. The tools are called where the doors call them,
`backend.assistant.call_tool`, and through the MCP server at /mcp with real MCP tokens, against the test database with
the users of the access-rule tests; the settings and the administrators' routes over HTTP with real API tokens. What
must not leak is planted the ways the contract's 6.4 allows: job rows written into the jobs table, a field marked
private and a dataset closed through the API, indexes made by SQL; a stored index review is a succeeded index_review
job's result (a real review is run too, as the index advisor's own tests run one); backups are a folder written the way
scripts/backup_local.py writes one. Each answer is compared with what the administrators' own route answers for the
same server state, and every answer is at most 20 KB (20,000 bytes of the call's JSON, as the other assistant specs)
with only the keys the contract names (NAMED below, written from its examples and bullets).

The contract states (after the first round of these tests): refusals in the order the door's own, then "switched
off", then "for administrators", before the arguments are looked at; a job is about the dataset it belongs to or names
(an index review); `backup_status.running` is `{job, state, started_at}` or null, and `recent_runs` is `[]` with
`not_configured`; `list_jobs` gives `progress` as `{done, total, message}`, the message null when kept back, and
`error_withheld` null whenever nothing is kept back; `index_advice.last_failure` is `{finished_at, error,
error_withheld}`; an index's definition is given whole for the app's own kinds (core, field, ann, array), and the
quoted-text rule applies to kind `other` only. Then (third round): migration 0040 adds the column and nothing else and
0041 adds two indexes on the jobs table; the window bounds ended jobs only (a queued or running job is in it however
old); `kinds` is for the window whatever the other filters; `started_by` may be "Deleted user"; the route's item for a
backup job carries `backup` ({file, size_bytes, tables}, null until it has succeeded); a job about a dataset that no
longer exists is kept back with its own sentence; free text has look-alikes of 3.3 replaced by a placeholder. Then
(fourth round): a follower's sync job is about every mirror of its link and kept back when any is closed or has a
private field; a job of no dataset has its words given only for the kinds named as about no dataset (backup, region
load, selection cleanup, the server's own index review, an index removal or restore of no dataset), others kept back
as "not yet known"; an index's definition is judged by what it quotes whatever kind the advisor calls it (whole for
field names in the server's forms, a vector index's field_name, storage parameters in WITH (…), and the indexes
schema.sql ships), quoted texts in the words about a withheld index written '…', and past 2,000 characters withheld.
Then (fifth round): restoring an index is not a kind known to be about no dataset; a sync whose link has no mirror
left reads as about a dataset that is gone; a shipped index's name is whole only when it quotes nothing schema.sql's
own statement for it doesn't. Then (sixth round): a constant is a quoted text or a number; a number is the server's
own only in `dataset_id = N`, inside `WITH (…)` and as a type's modifier; numbers are masked with the quoted texts in
the words about a withheld index. Then (seventh round): a type's modifier is recognised only right after its type's
name, so a number as a function's argument after a cast and a word (`::boolean AND f(4417)`) is a value.

Where the contract is still unclear these tests read it this way (each is listed for the contract's owner):
- `tools_for` gives tool objects like `tools()`; compared by name, description and schema. The MCP server answers
  `tools/list` afresh at each request (the five-minute keep of 2.4 is the client's), so a change shows at the next list.
- A job is about a dataset when its dataset_id is that dataset, or, having none, its arguments' dataset_id is (an
  index review). `list_jobs(dataset=N)` and the route's `dataset_id=N` take the jobs about N; the route item's
  `dataset_id`/`dataset_name` and the tool's `dataset` name the dataset the job is about.
- A tool's job `error` and `progress.message` are strings, the text (an error perhaps after its type), cut to 500
  characters: checked as holding the first 400 characters of the planted text and nothing of its end. A job kept back
  has `error_withheld` set when it has an error, or is running with a message; a finished job's message, not kept
  back, may be given or null.
- The window (`since_hours`, the route's `since`) is on created_at; planted finished jobs have all their times within
  seconds of each other, so a window on any of them reads the same. `counts` doesn't depend on `state`; `kinds` is
  checked as a set.
- A job about a dataset that no longer exists: its `dataset` is null or carries the id (no name can be known), its
  link the status page or /datasets/<id>; `list_jobs(dataset=<that id>)` is not_found, as for any unknown dataset.
- A follower's sync job: its `dataset` is null or one of its mirrors, its link the status page or a mirror's page;
  one naming a link that no longer exists is accepted with either "no longer exists" or "not yet known"; whether
  `list_jobs(dataset=<a mirror>)` lists it is not asserted.
- An index's definition is judged as `withheld_by_rule` reads 3.4: a definition past 2,000 characters is kept back;
  one of schema.sql's `CREATE INDEX` names on its table is whole when its quoted texts are among those of that
  statement (compared with spaces taken out: PostgreSQL prints a jsonb constant back with a space after its colon);
  otherwise, with `WITH (…)`, `metadata ->>/->/?
  '…'` (with or without `::text`) and `field_name = '…'` taken out, any quote left keeps it back. Each index of a
  real review is checked against what the route prints by that reading.
- Numbers (`NUMBER`): digits, with a fraction, standing alone; digits right after a letter, a digit, an underscore
  or a dot are part of a name (`idx_samples_meta_7_crop`, `embeddings_d7`, `float8`, `int4`, a schema's hex name),
  and quoted texts are taken out first. A type's modifier is `::`, a type name, then at once `(digits)` or
  `(digits,digits)`; the type name is one word, perhaps schema-qualified, or `character varying` / `bit varying`
  (`timestamp(3) without time zone` and `time(3) with time zone` carry the modifier after their first word), so no
  other words may stand between the cast and the parenthesis. A shipped name is whole when its quoted texts and
  numbers are among its statement's. The planted numbers are looked for as whole numbers (no digit or dot beside
  them), and are first checked absent from the administrators' routes' answers before the indexes holding them are
  made (the tools can't be asked before the switch exists).
- The words about a withheld index (`needed`, an overlap's `reason`) are checked by `masked_like`: no constant of
  the original left, whatever the mask, and every other piece of the sentence still there in order.
- Look-alikes are checked by what must be gone (the URL, the address, the password and token values, the IP address,
  the path, the hash, the connection string) and what must stay (a 7-digit count, a byte size, ordinary words, a path
  inside the backup folder); the placeholder's own words are not asserted.
- `link`: list_jobs and index_advice with `dataset` link to /datasets/<id>, everything else to /settings/status; a job's
  own link is its dataset's page, or the status page for a job about none.
- backup_status: `running` is the backup the route reports as its active job. `recent_runs[].file`/`size_bytes` are
  the backup job's result's. `not_configured`: every key but level, note and link is null or empty. `unreadable`:
  level, folder and error as the route answers them; nothing else is asserted.
- index_advice: an index entry has exactly the example's keys (plus idle_days and invalid in possibly_unused);
  `definition_withheld` is false when the definition is given. Without `section` the lists other than
  overlapping, possibly_unused (and suggested for a dataset) are not given (absent or empty); with one, the others are
  not given. `totals` has no key but the seven; each list the review holds is counted. A dataset closed to the
  assistant is answered, with its indexes (3.5); its jobs' and its review's messages are kept back.
- `note` is checked by a word: no review yet, "review"; not enough history, "unused". Descriptions likewise:
  system_status's speaks of migrations, a restart and a release; index_advice's of duplicate, operators, prefix,
  history, needed and removing.
- The four take exactly the arguments the contract names, none required, and list the routes it names them standing on.
- GET /api/admin/jobs/all refuses a power user and a guest with 403 and a detail saying nothing of the jobs, signed out
  401; its `since` is an ISO time; its `error` is checked to carry the text for a job about none or the ordinary
  dataset.
- "Read afresh at every request the tool makes" is checked by changing the role or the switch at the tool's first
  in-process request: that call then answers not_allowed or refused, or raises (a refused inner request is the tool's
  own fault, which call_tool raises).
- 3.3's IP address: no IPv4 address in any answer; a commit or tree hash: no run of 40 hex digits, nor the planted
  short commits, in system_status.
Not tested: "well under a second on a million finished jobs" (the implementer measures); an MCP client's five-minute
keep of the list.
"""

import asyncio
import base64
import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest

from backend import backups, index_actions, index_advisor, index_jobs, job_queue, main, system_status
from backend import database as database_module
from backend.assistant import inner, links
from backend.assistant.api import NOT_ALLOWED_CODE, ROUTE_REFUSED_CODE
from backend.assistant.identity import SCOPE_KEY, Identity
from backend.crypto import hash_token
from backend.database import init_connection
from backend.routers import datasets
from backend.tests import job_helpers, test_access_rule_spec, test_field_registry_postgres, test_upgrade_paths
from backend.tests.test_access_enforcement_spec import app_routes
from backend.tests.test_access_rule_spec import ADMIN, GUEST, OTHER_ADMIN, POWER, World
from backend.tests.test_assistant_core import _raw
from backend.tests.test_assistant_core_spec import (
    COLUMNS,
    HOSTILE,
    MCP_OFF,
    MIGRATIONS,
    NOT_LET_IN,
    SETTINGS,
    SHIPPED,
    TABLES,
    apply_before,
    apply_migration,
    assistant,
    call,
    plain,
    rows_as_they_are,
    settings,
)
from backend.tests.test_dataset_deletion import finish as finish_deletion
from backend.tests.test_mcp_door_spec import Door, make_token
from backend.tests.test_upgrade_paths import ROOT

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
database = test_upgrade_paths.database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

# The thirteen tools, in their order, as main lists them (m2 2.1: unchanged).
THIRTEEN = [
    'list_datasets',
    'describe_dataset',
    'make_view',
    'refine_view',
    'field_values',
    'field_summary',
    'get_samples',
    'find_place',
    'list_concepts',
    'concept_preview',
    'search_similar',
    'list_saved_views',
    'open_saved_view',
]
FOUR = ['system_status', 'backup_status', 'list_jobs', 'index_advice']
SWITCHED_OFF = 'The administration tools are switched off on this server'
FOR_ADMINISTRATORS = 'This tool is for administrators'
KEPT_CLOSED = "This job's messages are kept back: its dataset is closed to the assistant"
KEPT_PRIVATE = "This job's messages are kept back: its dataset has fields whose values are kept from the assistant"
NOT_CONFIGURED = (
    'This server takes no backups through the app (no backup folder is set). A deployment may back up another way; '
    'see the backup docs.'
)
ANSWER_LIMIT = 20_000
TEXT_LIMIT = 500
STATES = ('failed', 'running', 'queued', 'succeeded', 'cancelled')
SECTIONS = ('overlapping', 'possibly_unused', 'suggested', 'needed', 'too_new', 'removed')
ALL_JOBS = '/api/admin/jobs/all'

# Three datasets: one with a field marked private, one closed to the assistant, one ordinary.
PRIVATE_DS, CLOSED_DS, ORDINARY_DS = 300, 301, 302
NAMES = {PRIVATE_DS: 'trials-with-a-private-field', CLOSED_DS: 'closed-to-the-assistant', ORDINARY_DS: 'ordinary-plots'}
KEPT_GONE = "This job's messages are kept back: its dataset no longer exists"
KEPT_UNKNOWN = "This job's messages are kept back: this kind of job is not yet known to be about no dataset"
FOLLOWER_SYNC = 'follower_sync'
GONE_DS, GONE_REVIEWED_DS = 777, 778  # dataset ids no dataset has: jobs about them name a deleted dataset
GONE_VALUES = ['ZQXGONE-01-ROW', 'ZQXGONE-02-ROW']
PRIVATE_VALUES = [f'ZQXPRIV-{n:02d}-PLOT' for n in range(10)]  # the private field's values
CLOSED_VALUES = [f'ZQXSHUT-{n:02d}-ROW' for n in range(10)]  # values in the closed dataset's samples
IDX_VALUES = [f'ZQXIDX-{n:02d}-SITE' for n in range(10)]  # quoted in hand-made index definitions
NEVER_VALUES = [*PRIVATE_VALUES, *CLOSED_VALUES, *GONE_VALUES, *IDX_VALUES, 'ZQXPRIV', 'ZQXSHUT', 'ZQXGONE', 'ZQXIDX']
RESULT_MARK = 'RESULTMARK-7731'  # in jobs' results, which no answer gives
ARG_MARK = 'ARGMARK-5531'  # in a job's arguments, which no answer gives


def long_text(head: str, tail: str, size: int = 2000) -> str:
    body = head + ' '.join(f'step {n:04d} could not finish;' for n in range(200))
    return body[: size - len(tail)] + tail


ORD_ERROR = long_text('ORDERR-HEAD ', ' ORDERR-TAIL')
NONE_ERROR = long_text('NONEERR-HEAD ', ' NONEERR-TAIL')
ORD_PROGRESS = long_text('ORDPROG-HEAD ', ' ORDPROG-TAIL')
TAILS = ('ORDERR-TAIL', 'NONEERR-TAIL', 'ORDPROG-TAIL', 'REVIEWERR-TAIL', 'BACKUPERR-TAIL')

# What the server holds that no answer may carry (3.3), all made up here.
EMAIL_DOMAIN = 'mail.admin-spec.example'
WORKER = 'spec-worker-1'
HOST = 'zqxhost-7781.lab.admin-spec.example'
COMPUTE_HOST = 'zqxhost-gpu-4402.lab.admin-spec.example'
STORE_KEY = 'AKIAZQXSTORE00429911'
STORE_SECRET = 'ZQXSTORESECRET-0042'
PASSWORD_MARK = 'ZQXPASSWORDHASH'
CACHE_MARK = 'zqxcache'
COMMIT, CHECKOUT_COMMIT = 'c0ffee1', 'd00dfee'
SUBJECT = 'ZQXSUBJECT of the running commit'
RUNNING_TREE, CHECKOUT_TREE = '5a' * 20, 'b6' * 20
BACKUP_FAILURE = 'BackupError: free space 3 GB, need 40 GB'
HEX_RUN = re.compile(r'[0-9a-fA-F]{40}')
OCTET = r'(?:25[0-5]|2[0-4]\d|1?\d?\d)'
IPV4 = re.compile(rf'(?<![\d.]){OCTET}(?:\.{OCTET}){{3}}(?![\d.])')

# The keys the contract names for each tool's answer, by where they sit ('' the top, 'x[]' an item of list x, 'x.*'
# any key of x). ANY: the contract names no keys there (a usage's name).
ANY: Optional[set[str]] = None
INDEX_ENTRY = {
    'name',
    'table',
    'kind',
    'method',
    'dataset',
    'field',
    'size_bytes',
    'scans',
    'last_scan',
    'valid',
    'needed',
    'definition',
    'definition_withheld',
}
DATASET = {'id', 'name'}


def _entries(*paths: str, extra: frozenset = frozenset()) -> dict[str, Optional[set[str]]]:
    found: dict[str, Optional[set[str]]] = {}
    for path in paths:
        found[path] = INDEX_ENTRY | extra
        found[f'{path}.dataset'] = DATASET
    return found


NAMED: dict[str, dict[str, Optional[set[str]]]] = {
    'system_status': {
        '': {'checked_at', 'server', 'database', 'database_disk', 'cache_disks', 'workers', 'backups', 'link'},
        'server': {'version', 'restart_needed'},
        'database': {'reachable', 'size_bytes', 'pending_migrations', 'unknown_migrations', 'stale_functions', 'error'},
        'database_disk': {'total_bytes', 'free_bytes', 'level', 'low_below_bytes', 'ingest_paused_below_bytes'},
        'cache_disks[]': {'folders', 'total_bytes', 'free_bytes', 'level'},
        'workers': {'server', 'compute', 'available', 'error'},
        'workers.server': {'online', 'last_seen'},
        'workers.compute': {'online', 'last_seen'},
        'backups': {'level', 'latest_at', 'next_run_at'},
    },
    'backup_status': {
        '': {
            'level',
            'folder',
            'latest',
            'last_failure',
            'kept',
            'unverified_dumps',
            'disk',
            'schedule',
            'running',
            'recent_runs',
            'link',
            'note',
            'error',
        },
        'latest': {'file', 'created_at', 'size_bytes', 'dump_seconds', 'verify_seconds', 'tables'},
        'last_failure': {'failed_at', 'error'},
        'kept': {'count', 'total_bytes', 'oldest_created_at'},
        'disk': {'total_bytes', 'free_bytes'},
        'schedule': {'interval_hours', 'start_hour', 'keep', 'next_run_at'},
        'running': {'job', 'state', 'started_at'},
        'recent_runs[]': {'job', 'state', 'started_at', 'finished_at', 'file', 'size_bytes', 'error'},
    },
    'list_jobs': {
        '': {'jobs', 'total', 'offset', 'next_offset', 'counts', 'kinds', 'since', 'link'},
        'jobs[]': {
            'id',
            'kind',
            'state',
            'dataset',
            'field',
            'started_by',
            'created_at',
            'started_at',
            'finished_at',
            'progress',
            'failures',
            'max_failures',
            'error',
            'error_withheld',
            'link',
        },
        'jobs[].dataset': DATASET,
        'jobs[].progress': {'done', 'total', 'message'},
        'counts': set(STATES),
    },
    'index_advice': {
        '': {
            'scope',
            'dataset',
            'reviewed_at',
            'enough_history',
            'window_days',
            'idle_days_required',
            'review_running',
            'last_failure',
            'totals',
            *SECTIONS,
            'schedule',
            'pending_actions',
            'offset',
            'next_offset',
            'link',
            'note',
        },
        'dataset': DATASET,
        'last_failure': {'finished_at', 'error', 'error_withheld'},
        'totals': {*SECTIONS, 'redundant_bytes'},
        'overlapping[]': {'redundant', 'kept', 'rule', 'reason'},
        **_entries('overlapping[].redundant', 'overlapping[].kept', 'needed[]', 'too_new[]'),
        **_entries('possibly_unused[]', extra=frozenset({'idle_days', 'invalid'})),
        'suggested[]': {'field', 'type', 'reason', 'excess_seconds', 'build_seconds', 'pays_back', 'usages'},
        'suggested[].usages': ANY,
        'suggested[].usages.*': {'uses', 'mean_ms', 'p50_ms', 'p95_ms', 'reference_ms', 'slower'},
        'removed[]': {'index_name', 'table', 'field', 'size_bytes', 'reason', 'removed_by', 'removed_at', 'state'},
        'schedule': {'enabled', 'frequency', 'weekday', 'time', 'timezone', 'next_run_at', 'last_run_at'},
    },
}


def only_named_keys(tool: str, answer: Any) -> None:
    """Every object in the answer has only keys the contract names for that place (3.2: never passed through)."""
    named = NAMED[tool]

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            assert path in named, f'{tool}: an object at {path or "the top"} where the contract names none: {node}'
            allowed = named[path]
            if allowed is not None:
                extra = sorted(set(node) - allowed)
                assert not extra, f'{tool}: {path or "the top"} has keys the contract does not name: {extra}'
            for key, value in node.items():
                walk(value, f'{path}.*' if allowed is None else (f'{path}.{key}' if path else key))
        elif isinstance(node, list):
            for item in node:
                walk(item, f'{path}[]')

    walk(answer, '')


def keys_in(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node).union(*(keys_in(value) for value in node.values()))
    if isinstance(node, list):
        return set().union(*(keys_in(item) for item in node))
    return set()


# --- Calling ---


def names(tools: Any) -> list[str]:
    return [tool.name for tool in tools]


async def listed_for(user_id: int, door: str = 'mcp') -> list[str]:
    return names(await assistant.tools_for(user_id=user_id, door=door))


async def admin(name: str, arguments: Optional[dict[str, Any]] = None, user_id: int = ADMIN) -> dict[str, Any]:
    """An administration tool's result: at most 20 KB, only named keys."""
    answer = await call(user_id, name, arguments or {})
    assert answer['ok'], (name, arguments, answer)
    assert len(json.dumps(answer)) <= ANSWER_LIMIT, (name, arguments, len(json.dumps(answer)))
    only_named_keys(name, answer['result'])
    return answer['result']


async def refusal(user_id: int, name: str, arguments: Optional[dict[str, Any]] = None) -> str:
    answer = await call(user_id, name, arguments or {})
    assert not answer['ok'], (user_id, name, answer)
    assert answer['error']['code'] == 'not_allowed', (user_id, name, answer)
    return answer['error']['message']


async def pages(name: str, arguments: dict[str, Any], limit: int = 50, user_id: int = ADMIN) -> list[dict[str, Any]]:
    """Every page of a list, following next_offset; each page's offset is the one asked for."""
    found: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await admin(name, {**arguments, 'limit': limit, 'offset': offset}, user_id)
        assert page['offset'] == offset, (name, arguments, page['offset'])
        found.append(page)
        if page['next_offset'] is None:
            return found
        assert page['next_offset'] > offset, (name, arguments, page['next_offset'])
        offset = page['next_offset']
        assert len(found) < 500, (name, arguments)


async def switch_on(w: World) -> None:
    """The MCP server on, administrators and power users let in, the administration tools on."""
    await settings(
        w,
        {'mcp_enabled': True, 'admin_tools': True, 'roles': {'admin': True, 'power_user': True, 'guest': False}},
    )


def address(path: str) -> str:
    return f'{links.public_address()}{path}'


STATUS_PAGE = '/settings/status'


def moment(value: Any) -> Optional[datetime]:
    """A time given as ISO text or read from the database, to the whole second (the contract fixes no precision)."""
    if value is None:
        return None
    found = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return found.replace(microsecond=0)


def near(value: Any, then: datetime) -> bool:
    """An ISO time within five minutes of `then`."""
    found = moment(value)
    return found is not None and abs(found - then) < timedelta(minutes=5)


def close(a: Optional[int], b: Optional[int], within: int) -> bool:
    return a is None and b is None or (a is not None and b is not None and abs(a - b) <= within)


def cut_text(given: Any, planted: str) -> bool:
    """`given` is the planted text, cut to 500 characters at most (perhaps after the error's type)."""
    return isinstance(given, str) and len(given) <= TEXT_LIMIT and planted[: min(len(planted), 400)] in given


# Free text holding what looks like something of 3.3 (each replaced by a placeholder), and ordinary words and numbers
# (each left as it is): 3.4's second line of defence.
SCRUB_KEPT = ['Backup failed:', 'counted 1234567 rows', '20040000000 bytes', 'could not finish the dump']
SCRUB_TEXT = (
    'Backup failed: counted 1234567 rows, 20040000000 bytes; could not finish the dump. '
    'Upload to https://files.admin-spec.example/dump?sig=abc123 refused for ops-person@mail.admin-spec.example '
    'with password=ZqxPassValue7 token=ZqxTokenValue8 from 10.20.30.40 while writing '
    '/var/lib/zqxdata/pgdata/base/16384 (object deadbeef1234cafe) over '
    'postgresql://fr:secretpw@db.admin-spec.example:5432/fr'
)
SCRUB_AWAY = [
    'files.admin-spec.example',
    'sig=abc123',
    'ops-person',
    'mail.admin-spec.example',
    'ZqxPassValue7',
    'ZqxTokenValue8',
    '10.20.30.40',
    'zqxdata',
    'deadbeef1234cafe',
    'secretpw',
    'db.admin-spec.example',
    'postgresql://',
]


def scrubbed(given: Any) -> bool:
    """SCRUB_TEXT as an answer gives it: cut to 500 characters, every look-alike gone, the ordinary text kept."""
    return (
        isinstance(given, str)
        and len(given) <= TEXT_LIMIT
        and not [piece for piece in SCRUB_AWAY if piece in given]
        and all(piece in given for piece in SCRUB_KEPT)
    )


# --- Planting ---


async def add(dataset_id: int, samples: list[dict[str, Any]]) -> list[int]:
    # The administrator as the API knows them (their username read from the server, never made up).
    username = await database_module.db.fetchval('SELECT username FROM users WHERE id = $1', ADMIN)
    user = {'id': ADMIN, 'role': 'admin', 'username': username}
    return (await datasets.add_samples(dataset_id, {'samples': samples}, user=user))['ids']


async def three_datasets(w: World) -> None:
    """The dataset with a private field, the closed one and the ordinary one, each with samples."""
    for dataset_id, name in NAMES.items():
        await w.dataset(dataset_id)
        await w.database.execute('UPDATE datasets SET name = $2 WHERE id = $1', dataset_id, name)
    await add(
        PRIVATE_DS,
        [
            {'filepath': f'private-{n}.jpg', 'metadata': {'plot_code': v, 'crop': 'corn'}}
            for n, v in enumerate(PRIVATE_VALUES)
        ],
    )
    await add(
        CLOSED_DS,
        [
            {'filepath': f'closed-{n}.jpg', 'metadata': {'secret': v, 'crop': 'soy'}}
            for n, v in enumerate(CLOSED_VALUES)
        ],
    )
    await add(
        ORDINARY_DS,
        [
            {
                'filepath': f'ordinary-{n}.jpg',
                'metadata': {'crop': ('corn', 'soy')[n % 2], 'score': n, 'seen_at': f'2026-03-0{n + 1}T08:00:00Z'},
            }
            for n in range(6)
        ],
    )
    marked = await w.call(
        ADMIN,
        'PATCH',
        f'/api/datasets/{PRIVATE_DS}/fields/plot_code/description',
        json={'values_private': True},
    )
    assert marked.status_code == 200, marked.text
    closed = await w.call(ADMIN, 'PATCH', f'/api/datasets/{CLOSED_DS}', json={'assistant_closed': True})
    assert closed.status_code == 200 and closed.json()['assistant_closed'] is True, closed.text


JOB_ROW = """
INSERT INTO jobs (id, kind, dataset_id, lane, args, state, progress_done, progress_total, message, result, error,
                  created_by, created_at, updated_at, available_at, started_at, finished_at, heartbeat_at,
                  lease_token, lease_expires_at, worker_id, failures, max_failures, dedupe_owner)
SELECT gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, t, t, t,
       CASE WHEN $5 = 'queued' THEN NULL ELSE t + interval '1 second' END,
       CASE WHEN $5 IN ('succeeded', 'failed', 'cancelled') THEN t + interval '2 seconds' END,
       CASE WHEN $5 = 'running' THEN clock_timestamp() END,
       CASE WHEN $5 = 'running' THEN gen_random_uuid() END,
       CASE WHEN $5 = 'running' THEN clock_timestamp() + interval '1 hour' END,
       CASE WHEN $5 = 'running' THEN $12 END,
       $13, $14, CASE WHEN $11::int IS NULL THEN 'system' ELSE 'user:' || $11::text END
FROM (SELECT clock_timestamp() - $15::interval AS t) moment
RETURNING id::text
"""


async def job(
    w: World,
    kind: str,
    state: str,
    *,
    dataset: Optional[int] = None,
    args: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    message: Optional[str] = None,
    result: Any = None,
    by: Optional[int] = None,
    age: timedelta = timedelta(minutes=5),
    lane: str = 'user',
    done: int = 0,
    total: Optional[int] = None,
    failures: int = 0,
    max_failures: int = 5,
) -> str:
    """A job row written into the jobs table (6.4); created `age` ago, started a second and finished two after."""
    return await w.database.fetchval(
        JOB_ROW,
        kind,
        dataset,
        lane,
        args or {},
        state,
        done,
        total,
        message,
        result,
        None if error is None else {'type': 'RuntimeError', 'message': error},
        by,
        WORKER,
        failures,
        max_failures,
        age,
    )


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)


async def plant_jobs(w: World) -> dict[str, str]:
    """Jobs whose free text quotes planted values, about each of the three datasets and about none."""
    ids = {}
    ids['private_failed'] = await job(
        w,
        'field_index_build',
        'failed',
        dataset=PRIVATE_DS,
        args={'field_name': 'plot_code', 'index_name': 'idx_planted_build', 'field_type': 'number', 'note': ARG_MARK},
        error=f'invalid input syntax for type double precision: "{PRIVATE_VALUES[0]}"',
        message=f'casting {PRIVATE_VALUES[1]}',
        age=minutes(50),
        failures=3,
        max_failures=3,
        by=ADMIN,
    )
    ids['closed_failed'] = await job(
        w,
        'bulk_mutation',
        'failed',
        dataset=CLOSED_DS,
        error=f'value {CLOSED_VALUES[0]} was refused',
        message=f'at {CLOSED_VALUES[1]}',
        age=minutes(49),
    )
    ids['ordinary_failed'] = await job(
        w,
        'duplicate_maintenance',
        'failed',
        dataset=ORDINARY_DS,
        error=ORD_ERROR,
        age=minutes(48),
        failures=2,
        max_failures=2,
    )
    ids['none_failed'] = await job(w, 'selection_cleanup', 'failed', error=NONE_ERROR, age=minutes(47))
    ids['private_running'] = await job(
        w,
        'statistics',
        'running',
        dataset=PRIVATE_DS,
        message=f'counting {PRIVATE_VALUES[2]}',
        age=minutes(46),
        lane='maintenance',
        done=3,
        total=10,
    )
    ids['closed_running'] = await job(
        w,
        'statistics',
        'running',
        dataset=CLOSED_DS,
        message=f'counting {CLOSED_VALUES[2]}',
        age=minutes(45),
        lane='maintenance',
    )
    ids['ordinary_running'] = await job(
        w,
        'concept_scoring',
        'running',
        dataset=ORDINARY_DS,
        message=ORD_PROGRESS,
        age=minutes(44),
        done=5,
    )
    ids['private_result'] = await job(
        w,
        'map_refit',
        'succeeded',
        dataset=PRIVATE_DS,
        result={'kept': PRIVATE_VALUES[3]},
        age=minutes(43),
    )
    ids['none_result'] = await job(
        w,
        'region_load',
        'succeeded',
        result={'note': RESULT_MARK, 'closed': CLOSED_VALUES[3], 'private': PRIVATE_VALUES[4]},
        age=minutes(42),
    )
    ids['ordinary_result'] = await job(
        w,
        'map_refit',
        'succeeded',
        dataset=ORDINARY_DS,
        result={'note': RESULT_MARK},
        age=minutes(41),
    )
    # Index reviews belong to no dataset and are about one (contract 3.4).
    ids['private_review'] = await job(
        w,
        index_advisor.KIND,
        'failed',
        args=index_advisor.scope_args(PRIVATE_DS),
        error=f'could not read {PRIVATE_VALUES[5]}',
        age=minutes(40),
    )
    ids['closed_review'] = await job(
        w,
        index_advisor.KIND,
        'failed',
        args=index_advisor.scope_args(CLOSED_DS),
        error=f'could not read {CLOSED_VALUES[4]}',
        age=minutes(39),
    )
    ids['ordinary_queued'] = await job(w, 'bulk_mutation', 'queued', dataset=ORDINARY_DS, age=minutes(38))
    ids['ordinary_cancelled'] = await job(
        w,
        'bulk_mutation',
        'cancelled',
        dataset=ORDINARY_DS,
        by=ADMIN,
        age=minutes(37),
    )
    # About a dataset that no longer exists: by its dataset_id, and an index review naming it.
    ids['gone_failed'] = await job(
        w,
        'bulk_mutation',
        'failed',
        dataset=GONE_DS,
        error=f'value {GONE_VALUES[0]} was refused',
        age=minutes(36),
    )
    ids['gone_review'] = await job(
        w,
        index_advisor.KIND,
        'failed',
        args=index_advisor.scope_args(GONE_REVIEWED_DS),
        error=f'could not read {GONE_VALUES[1]}',
        age=minutes(35),
    )
    # Started by a person whose account has since been deleted (created_by set null, the owner kept).
    ids['deleted_user'] = await job(w, 'map_refit', 'succeeded', dataset=ORDINARY_DS, by=ADMIN, age=minutes(34))
    await w.database.execute(
        "UPDATE jobs SET created_by = NULL, dedupe_owner = 'user:4242' WHERE id = $1::uuid",
        ids['deleted_user'],
    )
    return ids


# A hand-made index (by SQL) whose predicate quotes a planted value, for each of the two datasets kept from the model.
HAND_MADE = {
    'idx_hand_private': f"CREATE INDEX idx_hand_private ON samples ((metadata->>'crop')) "
    f"WHERE metadata->>'plot_code' = '{PRIVATE_VALUES[6]}'",
    'idx_hand_closed': f"CREATE INDEX idx_hand_closed ON samples ((metadata->>'crop')) "
    f"WHERE metadata->>'secret' = '{CLOSED_VALUES[5]}'",
}
# A hand-made index whose only quoted texts are field names in the server's forms: its definition is given.
HAND_MADE_NAMES = 'idx_hand_fields'
HAND_MADE_NAMES_SQL = "CREATE INDEX idx_hand_fields ON samples ((metadata->>'crop')) WHERE metadata ? 'score'"
# Made by a person and kept back whatever kind the advisor guesses from the name (3.4): named like a list column's
# index, named like a field's index, a partial unique index, an exclusion constraint, and one past 2,000 characters.
LOOKALIKES = {
    'idx_samples_site_gin': f"CREATE INDEX idx_samples_site_gin ON samples USING gin (tags) "
    f"WHERE metadata->>'site' = '{IDX_VALUES[3]}'",
    f'idx_samples_meta_{ORDINARY_DS}_site': f'CREATE INDEX idx_samples_meta_{ORDINARY_DS}_site ON samples '
    f"((metadata->>'site')) WHERE dataset_id = {ORDINARY_DS} AND metadata->>'site' = '{IDX_VALUES[4]}'",
    'uq_hand_site': f"CREATE UNIQUE INDEX uq_hand_site ON samples (id) WHERE metadata->>'site' = '{IDX_VALUES[5]}'",
    'ex_hand_site': 'ALTER TABLE samples ADD CONSTRAINT ex_hand_site EXCLUDE USING btree (id WITH =) '
    f"WHERE (metadata->>'site' = '{IDX_VALUES[6]}')",
    'idx_hand_long': 'CREATE INDEX idx_hand_long ON samples (id) WHERE id <> ALL (ARRAY[{}])'.format(
        ', '.join(str(n) for n in range(1, 700)),
    ),
}
# The indexes schema.sql ships, by (name, table): whole, whatever they quote.
SHIPPED_STATEMENT = re.compile(
    r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(?:ONLY\s+)?(\w+)[^;]*;',
    re.I,
)
QUOTED_TEXT = re.compile(r"'((?:[^']|'')*)'")


def quoted_texts(sql: str) -> set[str]:
    """The quoted texts of a statement, spaces taken out: PostgreSQL prints a constant as it reads it back (a jsonb
    `'{"full":true}'` in schema.sql comes back `'{"full": true}'::jsonb`)."""
    return {re.sub(r'\s+', '', text) for text in QUOTED_TEXT.findall(sql)}


# A number: digits (with a fraction) standing alone. Digits after a letter, a digit, an underscore or a dot belong to
# a name (idx_samples_meta_7_crop, embeddings_d7, float8, int4, vector_l2_ops, a schema's hex name) and are not
# numbers; quoted texts are taken out before numbers are looked for.
NUMBER = re.compile(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])')
# Where a number is the server's own (3.4): a storage parameter, dataset_id = N, a type's modifier after ::.
SERVER_NUMBERS = (
    re.compile(r'WITH \([^)]*\)'),
    re.compile(r'\bdataset_id\s*=\s*\d+'),
    # A type's modifier: `::`, a type name, then at once `(digits)` or `(digits,digits)`. The type name is one word,
    # perhaps after a schema and a dot (`numeric`, `timestamp`, `time`, `vector`, `pg_catalog.numeric`), or one of
    # PostgreSQL's two-word names that take a modifier, `character varying` and `bit varying`. Nothing else may stand
    # between the cast and the parenthesis, so `::boolean AND f(4417)` is no modifier and 4417 stays a number.
    # (`double precision` takes none; `timestamp(3) without time zone` and `time(3) with time zone` carry it after
    # their first word.)
    re.compile(r'::\s*(?:(?:[a-z_]\w*\.)?[a-z_]\w*|character varying|bit varying)\(\s*\d+(?:\s*,\s*\d+)?\s*\)', re.I),
)


def numbers_in(sql: str) -> set[str]:
    return set(NUMBER.findall(QUOTED_TEXT.sub('', sql)))


# The indexes schema.sql ships, by (name, table): the quoted texts and the numbers its own statement for each holds.
SHIPPED_INDEXES = {
    (found.group(1), found.group(2)): (quoted_texts(found.group(0)), numbers_in(found.group(0)))
    for found in SHIPPED_STATEMENT.finditer((ROOT / 'backend/db/schema.sql').read_text())
}


def shipped_statement(name: str) -> str:
    """schema.sql's own statement for one of its indexes, read at run time."""
    found = [
        m.group(0)
        for m in SHIPPED_STATEMENT.finditer((ROOT / 'backend/db/schema.sql').read_text())
        if m.group(1) == name
    ]
    assert len(found) == 1, (name, found)
    return found[0]


def withheld_by_rule(name: str, table: str, definition: str) -> bool:
    """3.4: a definition past 2,000 characters is kept back. One of schema.sql's indexes (its name, on its table) is
    whole when it holds no quoted text and no number its statement there doesn't. Any other is whole only when, with
    storage parameters, `dataset_id = N`, type modifiers, `metadata ->>/->/? '…'` and `field_name = '…'` taken out,
    no quote and no number is left."""
    if len(definition) > 2000:
        return True
    shipped = SHIPPED_INDEXES.get((name, table))
    if shipped is not None and quoted_texts(definition) <= shipped[0] and numbers_in(definition) <= shipped[1]:
        return False
    rest = definition
    for pattern in SERVER_NUMBERS:
        rest = pattern.sub('', rest)
    rest = re.sub(r"metadata (?:->>|->|\?) '(?:[^']|'')*'(?:::text)?", '', rest)
    rest = re.sub(r"field_name = '(?:[^']|'')*'(?:::text)?", '', rest)
    return "'" in rest or bool(numbers_in(rest))


# Numbers planted in hand-made definitions, none of them likely by chance: no answer may hold one.
NUMBERS_PRIVATE: dict[str, Any] = {
    'plot': '441791',
    'yield': '90017.25',
    'farm': ['120031', '310047', '407119'],
    'ratio': '250019',
}
NUMBERS_CLOSED: dict[str, Any] = {
    'plot': '552803',
    'yield': '80029.75',
    'farm': ['130043', '320057', '417131'],
    'ratio': '260027',
}
NUMBERS_STORED: dict[str, Any] = {
    'plot': '663407',
    'yield': '70043.5',
    'farm': ['140053', '330061', '427147'],
    'ratio': '270031',
}
SWAPPED_NUMBER = '470113'
# A number as a function's argument right after a cast and a word: `::boolean AND spec_flag(N)`.
FLAG_NUMBERS = {'priv_and': '538141', 'priv_or': '538163', 'priv_bare': '538187', 'shut_and': '629153'}
FLAG_STORED = '731167'
PLANTED_NUMBERS = [
    number
    for planted in (NUMBERS_PRIVATE, NUMBERS_CLOSED, NUMBERS_STORED)
    for value in planted.values()
    for number in (value if isinstance(value, list) else [value])
] + [SWAPPED_NUMBER, *FLAG_NUMBERS.values(), FLAG_STORED]
FLAG_FUNCTION = 'CREATE FUNCTION spec_flag(integer) RETURNS boolean IMMUTABLE LANGUAGE sql AS $$ SELECT $1 > 0 $$'
FLAG_REAL = {
    'idx_priv_flag': f"CREATE INDEX idx_priv_flag ON samples (dataset_id) WHERE dataset_id = {PRIVATE_DS} "
    f"AND (metadata->>'flag')::boolean AND spec_flag({FLAG_NUMBERS['priv_and']})",
    'idx_priv_flag_or': f"CREATE INDEX idx_priv_flag_or ON samples (dataset_id) WHERE dataset_id = {PRIVATE_DS} "
    f"AND ((metadata->>'flag')::boolean OR spec_flag({FLAG_NUMBERS['priv_or']}))",
    'idx_priv_flag_bare': "CREATE INDEX idx_priv_flag_bare ON samples (dataset_id) "
    f"WHERE (metadata->>'flag')::boolean AND spec_flag({FLAG_NUMBERS['priv_bare']})",
    'idx_shut_flag': f"CREATE INDEX idx_shut_flag ON samples (dataset_id) WHERE dataset_id = {CLOSED_DS} "
    f"AND (metadata->>'flag')::boolean AND spec_flag({FLAG_NUMBERS['shut_and']})",
}
FLAG_STORED_DEFINITION = (
    'CREATE INDEX idx_stored_flag ON public.samples USING btree (dataset_id) '
    f"WHERE (((metadata ->> 'flag'::text))::boolean AND spec_flag({FLAG_STORED}))"
)


def planted_numbers_in(text: str) -> list[str]:
    """The planted numbers in a text, each as a whole number (a byte size holding its digits doesn't count)."""
    return [n for n in PLANTED_NUMBERS if re.search(rf'(?<![\d.]){re.escape(n)}(?![\d])', text)]


def numeric_indexes(prefix: str, numbers: dict[str, Any], dataset: Optional[int] = None) -> dict[str, str]:
    """Hand-made indexes holding a field's value as a bare number: SQL to make them (`dataset` given), else
    definitions as PostgreSQL prints them."""
    farm = ', '.join(numbers['farm'])
    if dataset is not None:
        only = f'dataset_id = {dataset} AND '
        return {
            f'idx_{prefix}_plot': f"CREATE INDEX idx_{prefix}_plot ON samples (((metadata->>'plot')::integer)) "
            f"WHERE {only}(metadata->>'plot')::integer = {numbers['plot']}",
            f'idx_{prefix}_yield': f'CREATE INDEX idx_{prefix}_yield ON samples (dataset_id) '
            f"WHERE {only}(metadata->>'yield')::float > {numbers['yield']}",
            f'idx_{prefix}_farm': f'CREATE INDEX idx_{prefix}_farm ON samples (dataset_id) '
            f"WHERE {only}(metadata->>'farm')::integer = ANY (ARRAY[{farm}])",
            f'idx_{prefix}_ratio': f"CREATE INDEX idx_{prefix}_ratio ON samples (((metadata->>'yield')::float "
            f"/ {numbers['ratio']})) WHERE dataset_id = {dataset}",
        }
    cast = "(((metadata ->> '{}'::text))::{})"
    return {
        f'idx_{prefix}_plot': f'CREATE INDEX idx_{prefix}_plot ON public.samples USING btree '
        f"(({cast.format('plot', 'integer')})) WHERE ({cast.format('plot', 'integer')} = {numbers['plot']})",
        f'idx_{prefix}_yield': f'CREATE INDEX idx_{prefix}_yield ON public.samples USING btree (dataset_id) '
        f"WHERE ({cast.format('yield', 'double precision')} > {numbers['yield']})",
        f'idx_{prefix}_farm': f'CREATE INDEX idx_{prefix}_farm ON public.samples USING btree (dataset_id) '
        f"WHERE ({cast.format('farm', 'integer')} = ANY (ARRAY[{farm}]))",
        f'idx_{prefix}_ratio': f'CREATE INDEX idx_{prefix}_ratio ON public.samples USING btree '
        f"(({cast.format('yield', 'double precision')} / ({numbers['ratio']})::double precision))",
    }


# Hand-made indexes that hold no value: whole. One with no constant at all, one with a type's modifier.
HAND_PLAIN_PRINTED = 'CREATE INDEX idx_hand_plain ON public.samples USING btree (dataset_id, created_at)'
WHOLE_HAND_MADE = {
    'idx_hand_plain': 'CREATE INDEX idx_hand_plain ON samples (dataset_id, created_at)',
    'idx_hand_typmod': "CREATE INDEX idx_hand_typmod ON samples (((metadata->>'x')::numeric(10,2)))",
    'idx_hand_varchar': "CREATE INDEX idx_hand_varchar ON samples (((metadata->>'x')::character varying(255)))",
    # users.last_login is a timestamp without time zone: its cast to timestamp(3) is immutable (a text's isn't).
    'idx_hand_ts': 'CREATE INDEX idx_hand_ts ON users ((last_login::timestamp(3) without time zone))',
}


async def run_review(w: World, dataset_id: Optional[int]) -> None:
    """A real index review, run as the index advisor's tests run one."""
    queued, _ = await index_advisor.submit(w.database.pool, dataset_id, ADMIN)
    claimed = await job_queue.claim_job(w.database.pool, 'admin-tools-spec', kinds=[index_advisor.KIND])
    assert claimed is not None and claimed['id'] == queued['id'], (queued, claimed)
    finished = await job_helpers.step(w.database, claimed, index_advisor.handler(w.database.pool))
    assert finished['state'] == 'succeeded', finished


async def index_names(w: World) -> set[str]:
    return {row['indexname'] for row in await w.database.fetch(INDEX_NAMES)}


INDEX_NAMES = 'SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()'


NUMERIC_REAL = {
    **numeric_indexes('priv', NUMBERS_PRIVATE, PRIVATE_DS),
    **numeric_indexes('shut', NUMBERS_CLOSED, CLOSED_DS),
    **FLAG_REAL,
}


async def numbers_before(w: World) -> None:
    """The planted numbers are in no answer of the administrators' routes before the indexes holding them are made
    (so finding one later can't be chance)."""
    for path in (
        '/api/admin/index-advisor',
        '/api/admin/status',
        '/api/admin/jobs?limit=100',
        *(f'/api/datasets/{dataset_id}/index-advisor' for dataset_id in NAMES),
    ):
        response = await w.call(ADMIN, 'GET', path)
        assert response.status_code == 200, (path, response.text)
        assert not planted_numbers_in(response.text), (path, planted_numbers_in(response.text))


async def real_indexes(w: World) -> dict[str, str]:
    """Indexes made by SQL (the hand-made ones: quoting values, holding numbers on a private field and on a closed
    dataset's field, holding no value, and the look-alikes); the app's own, made through its routes: a field's index
    of each type (text, number, datetime), a registered list column's, and a vector index; then a real review of the
    system and of the ordinary dataset. The app's indexes' names, by what they are."""
    await numbers_before(w)
    for dataset_id, numbers in ((PRIVATE_DS, NUMBERS_PRIVATE), (CLOSED_DS, NUMBERS_CLOSED)):
        samples = [
            {
                'filepath': f'numbers-{dataset_id}-{n}.jpg',
                'metadata': {
                    'plot': int(numbers['plot']) if n == 0 else 1000 + n,
                    'yield': float(numbers['yield']) + n,
                    'farm': int(numbers['farm'][n]),
                    'flag': n % 2 == 0,
                },
            }
            for n in range(3)
        ]
        await add(dataset_id, samples)
    for field in ('plot', 'yield', 'farm', 'flag'):
        marked = await w.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{PRIVATE_DS}/fields/{field}/description',
            json={'values_private': True},
        )
        assert marked.status_code == 200, marked.text
    await w.database.execute(FLAG_FUNCTION)
    for statement in (
        *HAND_MADE.values(),
        HAND_MADE_NAMES_SQL,
        *LOOKALIKES.values(),
        *NUMERIC_REAL.values(),
        *WHOLE_HAND_MADE.values(),
    ):
        await w.database.execute(statement)
    built: dict[str, str] = {}
    for kind, field in (('field', 'crop'), ('field_number', 'score'), ('field_datetime', 'seen_at')):
        promoted = await w.call(
            ADMIN,
            'POST',
            f'/api/datasets/{ORDINARY_DS}/fields/{field}/promote',
            params={'wait': 'false'},
        )
        assert promoted.status_code in (200, 202), promoted.text
        finished = await job_helpers.finish(w.database, index_jobs.FIELD_KIND)
        assert finished['state'] == 'succeeded', finished
        built[kind] = await w.database.fetchval("SELECT args->>'index_name' FROM jobs WHERE id = $1", finished['id'])
    before = await index_names(w)
    listed = await w.call(ADMIN, 'POST', f'/api/datasets/{ORDINARY_DS}/array-fields', json={'field_name': 'pests'})
    assert listed.is_success, listed.text
    (built['array'],) = await index_names(w) - before
    ids = [row['id'] for row in await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1', ORDINARY_DS)]
    vectors = [{'sample_id': i, 'field_name': 'clip', 'vector': [1.0, n / 10]} for n, i in enumerate(ids)]
    stored = await w.call(ADMIN, 'POST', f'/api/datasets/{ORDINARY_DS}/embeddings', json=vectors)
    assert stored.is_success, stored.text
    before = await index_names(w)
    asked = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{ORDINARY_DS}/quality/ann-index',
        params={'wait': 'false'},
        json={'field': 'clip'},
    )
    assert asked.status_code in (200, 202), asked.text
    finished = await job_helpers.finish(w.database, index_jobs.ANN_KIND)
    assert finished['state'] == 'succeeded', finished
    (built['vector'],) = await index_names(w) - before
    for name in built.values():
        assert await w.database.fetchval('SELECT to_regclass($1) IS NOT NULL', name), name
    await run_review(w, None)
    await run_review(w, ORDINARY_DS)
    return built


async def plant_server(w: World, root: Path, monkeypatch: Any) -> str:
    """The backup folder (two verified dumps, one unverified, a newer failure), a backup schedule, the cache folders,
    the code the server runs and its checkout (different backend trees: a restart is needed), and two workers. The
    backup folder."""
    folder = root / 'backup-folder'
    folder.mkdir()
    now = datetime.now(timezone.utc)

    def stamp(hours: int) -> str:
        return (now - timedelta(hours=hours)).strftime('%Y%m%dT%H%M%SZ')

    for hours, size in ((72, 1200), (48, 3400)):
        dump = f'freedomridge_{stamp(hours)}.sql.gz'
        (folder / dump).write_bytes(b'0' * size)
        manifest = {
            'verified': True,
            'size_bytes': size,
            'dump_seconds': 61,
            'verify_seconds': 7,
            'row_counts': {f'table_{n}': n for n in range(30)},
            'schema_version': '0039_region_sets_bundled.sql',
            'migrations_applied': 40,
            'server_version': '18.1',
        }
        (folder / f'{dump}.manifest.json').write_text(json.dumps(manifest))
    (folder / f'freedomridge_{stamp(36)}.sql.gz').write_bytes(b'1' * 10)  # no manifest: not verified
    (folder / f'freedomridge_{stamp(24)}.failed.json').write_text(json.dumps({'error': BACKUP_FAILURE}))
    monkeypatch.setattr(backups, 'BACKUP_DIR', str(folder))
    for attribute in ('S3_LOCAL_PATH', 'CROP_CACHE_PATH', 'COMPOSITE_CACHE_PATH'):
        cache = root / f'{CACHE_MARK}-{attribute.lower().replace("_", "-")}'
        cache.mkdir()
        monkeypatch.setattr(main, attribute, str(cache))
    running = {'commit': COMMIT, 'subject': SUBJECT, 'committed_at': '2026-11-18T10:00:00+00:00'}
    monkeypatch.setattr(system_status, '_RUNNING', {**running, 'backend_tree': RUNNING_TREE})
    monkeypatch.setattr(
        system_status,
        '_git_commit',
        lambda: {**running, 'commit': CHECKOUT_COMMIT, 'backend_tree': CHECKOUT_TREE},
    )
    schedule = await w.call(
        ADMIN,
        'PUT',
        '/api/admin/backups/schedule',
        json={'interval_hours': 24, 'start_hour': 2, 'keep': 7},
    )
    assert schedule.status_code == 200, schedule.text
    await w.database.execute(
        'INSERT INTO workers (id, placement, name, hostname, heartbeat_at) VALUES '
        "($1, 'server', 'runner-1', $2, clock_timestamp()), "
        "('spec-compute-1', 'compute', 'gpu-1', $3, clock_timestamp() - interval '2 hours')",
        WORKER,
        HOST,
        COMPUTE_HOST,
    )
    return str(folder)


async def plant_secrets(w: World) -> list[str]:
    """Email addresses, a password hash and a storage configuration with a key: what the server holds and no answer
    may carry (3.3). The values to look for."""
    await w.database.execute("UPDATE users SET email = 'person-' || id || '@' || $1::text", EMAIL_DOMAIN)
    await w.database.execute(
        'UPDATE users SET password_hash = $2 WHERE id = $1',
        OTHER_ADMIN,
        f'$argon2id${PASSWORD_MARK}',
    )
    await w.database.execute(
        'INSERT INTO storage_configs (name, bucket_name, access_key_id, secret_access_key_encrypted, endpoint_url) '
        "VALUES ('spec-store', 'spec-bucket', $1, $2, 'https://storage.admin-spec.example')",
        STORE_KEY,
        STORE_SECRET,
    )
    seal = await w.database.fetchval('SELECT seal_key FROM assistant_settings')
    return [
        EMAIL_DOMAIN,
        PASSWORD_MARK,
        STORE_KEY,
        STORE_KEY[:4] + '...' + STORE_KEY[-4:],
        STORE_SECRET,
        'storage.admin-spec.example',
        HOST,
        COMPUTE_HOST,
        WORKER,
        CACHE_MARK,
        COMMIT,
        CHECKOUT_COMMIT,
        SUBJECT,
        RUNNING_TREE,
        CHECKOUT_TREE,
        bytes(seal).hex(),
        base64.b64encode(bytes(seal)).decode()[:40],
        os.environ['FRIDGE_TEST_POSTGRES_DSN'],
        'postgres://',
        'postgresql://',
        '127.0.0.1',
        'X-Amz-Signature',
        str(ROOT),
        *w.tokens.values(),
    ]


async def plant_everything(w: World, root: Path, monkeypatch: Any) -> dict[str, Any]:
    await three_datasets(w)
    built = await real_indexes(w)
    folder = await plant_server(w, root, monkeypatch)
    never = await plant_secrets(w)
    jobs = await plant_jobs(w)
    return {'field_index': built['field'], 'built': built, 'folder': folder, 'never': never, 'jobs': jobs}


def index_entry(
    name: str,
    *,
    table: str = 'samples',
    kind: str = 'other',
    method: str = 'btree',
    dataset: Optional[int] = None,
    field: Optional[str] = None,
    definition: Optional[str] = None,
    size: int = 8192,
    scans: int = 0,
    valid: bool = True,
    needed: tuple[str, ...] = (),
) -> dict[str, Any]:
    """An index as a stored review lists it (the survey's Entry)."""
    return {
        'name': name,
        'table': table,
        'kind': kind,
        'method': method,
        'dataset_id': dataset,
        'field': field,
        'orphan': False,
        'definition': definition
        if definition is not None
        else f'CREATE INDEX {name} ON public.{table} USING {method} (id)',
        'size_bytes': size,
        'scans': scans,
        'last_scan': None,
        'observed_since': '2026-09-01T03:00:00+00:00',
        'valid': valid,
        'needed': list(needed),
    }


def field_index(
    dataset_id: int,
    field: str,
    name: Optional[str] = None,
    definition: Optional[str] = None,
    **options: Any,
) -> dict[str, Any]:
    name = name or f'idx_samples_meta_{dataset_id}_{field}'
    definition = definition or (
        f"CREATE INDEX {name} ON public.samples USING btree (((metadata ->> '{field}'::text))) "
        f'WHERE (dataset_id = {dataset_id})'
    )
    return index_entry(name, kind='field', dataset=dataset_id, field=field, definition=definition, **options)


def review(dataset_id: Optional[int] = None, *, enough_history: bool = True, **lists: Any) -> dict[str, Any]:
    """A stored review's result (the survey's Review)."""
    found: dict[str, Any] = {
        'scope': 'system' if dataset_id is None else 'dataset',
        'dataset_id': dataset_id,
        'reviewed_at': '2026-11-18T03:00:04+00:00',
        'window_days': 7,
        'idle_days_required': 14,
        'stats_since': '2026-09-01T00:00:00+00:00',
        'history_since': '2026-09-01T00:00:00+00:00',
        'enough_history': enough_history,
        'possibly_unused': [],
        'too_new': [],
        'overlapping': [],
        'building': [],
        'recreated': [],
    }
    if dataset_id is None:
        found.update(needed=[], missing_core=[])
    else:
        found.update(telemetry_since='2026-11-11', suggested=[], build_seconds=None)
    found.update(lists)
    return found


TWIN_DEFINITION = (
    "CREATE INDEX idx_twin ON public.samples USING btree (((metadata ->> 'crop'::text))) "
    f"WHERE ((metadata ->> 'plot_code'::text) = '{PRIVATE_VALUES[6]}'::text)"
)
# The indexes of system_review whose definitions are kept back (3.4: judged by what they quote, whatever kind the
# advisor calls them): a quoted value; one named like a list column's index; one named like a field's index; a
# constraint-backed one; one longer than 2,000 characters; idx_samples_labels_gin, which quotes 'object' and is not
# one schema.sql ships.
WITHHELD = {
    'idx_twin',
    'idx_samples_labels_gin',
    'idx_samples_site_gin',
    f'idx_samples_meta_{ORDINARY_DS}_site',
    'uq_hand_site',
    'idx_hand_long',
    *numeric_indexes('stored', NUMBERS_STORED),
    'idx_stored_flag',
}
STORED_NUMERIC = numeric_indexes('stored', NUMBERS_STORED)
QUOTED = re.compile(r"'(?:[^']|'')*'")
CONSTANT = re.compile(r"'(?:[^']|'')*'|(?<![\w.])\d+(?:\.\d+)?(?![\w.])")


def masked_like(given: Any, original: str) -> bool:
    """`given` is `original` with every constant masked (3.4: the words about a withheld index): no constant of it
    left, whatever the mask; every other piece of the sentence still there, in order."""
    if not isinstance(given, str):
        return False
    for constant in CONSTANT.findall(original):
        value = constant.strip("'")
        if value and re.search(rf'(?<![\w.]){re.escape(value)}(?![\w.])', given):
            return False
    position = 0
    for piece in CONSTANT.split(original):
        piece = piece.strip()
        if piece:
            found = given.find(piece, position)
            if found < 0:
                return False
            position = found + len(piece)
    return True


class Masked:
    """Compares equal to a sentence (or a list of them) that is `original` with its constants masked."""

    def __init__(self, original: Any):
        self.original = original

    def __eq__(self, other: object) -> bool:
        if isinstance(self.original, list):
            return (
                isinstance(other, list)
                and len(other) == len(self.original)
                and all(masked_like(given, sentence) for given, sentence in zip(other, self.original))
            )
        return masked_like(other, self.original)

    def __repr__(self) -> str:
        return f'Masked({self.original!r})'


LONG_DEFINITION = 'CREATE INDEX idx_hand_long ON public.samples USING btree (id) WHERE (id <> ALL (ARRAY[{}]))'.format(
    ', '.join(str(n) for n in range(1, 700)),
)


def system_review() -> dict[str, Any]:
    """A system review with every list, a closed dataset's indexes among them."""
    twin = index_entry('idx_twin', definition=TWIN_DEFINITION, size=70_000)
    vectors = 'CREATE INDEX {name} ON public.embeddings_d301 USING hnsw (vector {ops})'
    return review(
        overlapping=[
            {
                'redundant': index_entry(
                    'idx_lead_a',
                    definition='CREATE INDEX idx_lead_a ON public.samples USING btree (dataset_id)',
                    size=540_000,
                    scans=12,
                ),
                'kept': index_entry(
                    'idx_kept_b',
                    definition='CREATE INDEX idx_kept_b ON public.samples USING btree (dataset_id, id)',
                    size=900_000,
                    scans=40,
                ),
                'rule': 'prefix',
                'reason': 'Its columns (dataset_id) lead idx_kept_b (dataset_id, id), which serves the same searches '
                '(reading a larger index)',
            },
            {
                'redundant': twin,
                'kept': index_entry('idx_twin_kept', size=70_000, scans=3),
                'rule': 'duplicate',
                'reason': 'Same columns, operators, order and predicate as idx_twin_kept',
            },
            {
                'redundant': index_entry(
                    'embeddings_d301_l2',
                    table='embeddings_d301',
                    kind='ann',
                    method='hnsw',
                    dataset=CLOSED_DS,
                    definition=vectors.format(name='embeddings_d301_l2', ops='vector_l2_ops'),
                    size=3_000_000,
                ),
                'kept': index_entry(
                    'embeddings_d301_cos',
                    table='embeddings_d301',
                    kind='ann',
                    method='hnsw',
                    dataset=CLOSED_DS,
                    definition=vectors.format(name='embeddings_d301_cos', ops='vector_cosine_ops'),
                    size=3_000_000,
                    scans=9,
                ),
                'rule': 'operators',
                'reason': 'Serves the same searches as embeddings_d301_cos with the same operators',
            },
            {
                'redundant': field_index(
                    ORDINARY_DS,
                    'site',
                    definition=f'CREATE INDEX idx_samples_meta_{ORDINARY_DS}_site ON public.samples USING btree '
                    f"(((metadata ->> 'site'::text))) WHERE ((dataset_id = {ORDINARY_DS}) AND "
                    f"((metadata ->> 'site'::text) = '{IDX_VALUES[2]}'::text))",
                    size=33_000,
                ),
                'kept': field_index(ORDINARY_DS, 'crop'),
                'rule': 'prefix',
                'reason': f"Its columns lead idx_samples_meta_{ORDINARY_DS}_crop where site = '{IDX_VALUES[2]}'",
            },
            {
                'redundant': index_entry(
                    'idx_stored_yield',
                    definition=STORED_NUMERIC['idx_stored_yield'],
                    size=45_000,
                ),
                'kept': index_entry('idx_hand_plain', definition=HAND_PLAIN_PRINTED, size=90_000, scans=4),
                'rule': 'prefix',
                'reason': f"Its columns (dataset_id) lead idx_hand_plain where yield > {NUMBERS_STORED['yield']}",
            },
        ],
        possibly_unused=[
            {**index_entry('idx_idle_big', size=2_000_000), 'invalid': False, 'idle_days': 31.5},
            {**twin, 'invalid': False, 'idle_days': 20.0},  # also overlapping's: counted once in redundant_bytes
            {**index_entry('idx_broken', valid=False, size=4096), 'invalid': True, 'idle_days': None},
        ],
        too_new=[
            index_entry('idx_recent'),
            index_entry(
                'idx_arrow',
                definition="CREATE INDEX idx_arrow ON public.samples USING gin ((metadata -> 'labels'::text))",
            ),
            index_entry(
                'idx_has',
                definition="CREATE INDEX idx_has ON public.samples USING btree (id) "
                "WHERE (metadata ? 'plot_code'::text)",
            ),
            index_entry(
                'idx_plain_names',
                definition="CREATE INDEX idx_plain_names ON public.samples USING btree ((metadata ->> 'crop')) "
                "WHERE (metadata ? 'score')",
            ),
            index_entry(
                'idx_samples_labels_gin',
                kind='array',
                method='gin',
                definition='CREATE INDEX idx_samples_labels_gin ON public.samples USING gin (labels) '
                "WHERE (media_type <> 'object'::text)",
            ),
            index_entry(
                'idx_samples_site_gin',
                kind='array',
                method='gin',
                definition='CREATE INDEX idx_samples_site_gin ON public.samples USING gin (tags) '
                f"WHERE ((metadata ->> 'site'::text) = '{IDX_VALUES[0]}'::text)",
            ),
            index_entry(
                'hnsw_emb_d301_clip',
                table='embeddings_d301',
                kind='ann',
                method='hnsw',
                dataset=CLOSED_DS,
                definition='CREATE INDEX hnsw_emb_d301_clip ON public.embeddings_d301 USING hnsw '
                "(((vector)::vector(4)) vector_cosine_ops) WITH (m='16', ef_construction='64') "
                "WHERE (field_name = 'clip'::text)",
            ),
            index_entry('idx_hand_long', definition=LONG_DEFINITION),
            index_entry('idx_stored_farm', definition=STORED_NUMERIC['idx_stored_farm']),
            index_entry('idx_stored_ratio', definition=STORED_NUMERIC['idx_stored_ratio']),
            index_entry(
                'idx_hand_typmod',
                definition='CREATE INDEX idx_hand_typmod ON public.samples USING btree '
                "((((metadata ->> 'x'::text))::numeric(10,2)))",
            ),
            index_entry('idx_stored_flag', definition=FLAG_STORED_DEFINITION),
            index_entry(
                'idx_hand_varchar',
                definition='CREATE INDEX idx_hand_varchar ON public.samples USING btree '
                "((((metadata ->> 'x'::text))::character varying(255)))",
            ),
            index_entry(
                'idx_hand_ts',
                table='users',
                definition='CREATE INDEX idx_hand_ts ON public.users USING btree '
                '(((last_login)::timestamp(3) without time zone))',
            ),
            field_index(
                ORDINARY_DS,
                'score',
                definition=f'CREATE INDEX idx_samples_meta_{ORDINARY_DS}_score ON public.samples USING btree '
                f"((((metadata ->> 'score'::text))::double precision)) WHERE (dataset_id = {ORDINARY_DS})",
            ),
        ],
        needed=[
            index_entry(
                'samples_pkey',
                kind='core',
                definition='CREATE UNIQUE INDEX samples_pkey ON public.samples USING btree (id)',
                needed=('primary key',),
            ),
            index_entry(  # one schema.sql ships, on its table: whole, whatever it quotes
                'idx_jobs_ready',
                table='jobs',
                kind='core',
                definition='CREATE INDEX idx_jobs_ready ON public.jobs USING btree (placement, available_at, '
                "created_at, id) WHERE (state = 'queued'::text)",
                needed=('only index for the queue: without it, claiming a job scans jobs',),
            ),
            index_entry(  # constraint-backed, made by a person, quoting a value: its words keep it back too
                'uq_hand_site',
                kind='core',
                definition='CREATE UNIQUE INDEX uq_hand_site ON public.samples USING btree (dataset_id) '
                f"WHERE ((metadata ->> 'site'::text) = '{IDX_VALUES[1]}'::text)",
                needed=(f"backs the constraint uq_hand_site (WHERE site = '{IDX_VALUES[1]}')",),
            ),
            index_entry(  # constraint-backed, quoting a number
                'idx_stored_plot',
                kind='core',
                definition=STORED_NUMERIC['idx_stored_plot'],
                needed=(f"backs the constraint plot_once (WHERE plot = {NUMBERS_STORED['plot']})",),
            ),
        ],
        building=[index_entry('idx_building_now')],
        missing_core=[{'name': 'idx_missing_core', 'table': 'samples'}],
        recreated=[
            {
                'index_name': 'idx_back_again',
                'removed_at': '2026-11-01T00:00:00+00:00',
                'note': f'made again by hand with {PRIVATE_VALUES[7]}',
            },
        ],
    )


SYSTEM_REDUNDANT_BYTES = 540_000 + 70_000 + 3_000_000 + 33_000 + 45_000 + 2_000_000 + 4096


def dataset_review(dataset_id: int) -> dict[str, Any]:
    usage = {'uses': 40, 'mean_ms': 3050.0, 'p50_ms': 4096, 'p95_ms': 8192, 'reference_ms': 50.0, 'slower': True}
    return review(
        dataset_id,
        suggested=[
            {
                'field': 'score',
                'type': 'number',
                'reason': 'index_missing',
                'usages': {'filter': usage},
                'excess_seconds': 120.0,
                'build_seconds': None,
                'pays_back': None,
            },
            {
                'field': 'crop',
                'type': 'string',
                'reason': 'slow',
                'usages': {'filter': {**usage, 'uses': 12}, 'sort': {**usage, 'slower': False}},
                'excess_seconds': 30.5,
                'build_seconds': 4.5,
                'pays_back': True,
            },
        ],
        possibly_unused=[{**field_index(dataset_id, 'old', size=24576), 'invalid': False, 'idle_days': 19.5}],
        too_new=[field_index(dataset_id, 'crop')],
        overlapping=[
            {
                'redundant': field_index(
                    dataset_id,
                    'crop',
                    name=f'idx_samples_meta_{dataset_id}_crop_again',
                    size=9000,
                ),
                'kept': field_index(dataset_id, 'crop'),
                'rule': 'duplicate',
                'reason': f'Same columns, operators, order and predicate as idx_samples_meta_{dataset_id}_crop',
            },
        ],
    )


async def review_job(
    w: World,
    dataset_id: Optional[int],
    *,
    result: Any = None,
    state: str = 'succeeded',
    error: Optional[str] = None,
    age: timedelta = timedelta(hours=3),
) -> str:
    """A review of the system (dataset_id None) or of a dataset, as the job the advisor reads it from."""
    return await job(
        w,
        index_advisor.KIND,
        state,
        args=index_advisor.scope_args(dataset_id),
        result=result,
        error=error,
        age=age,
    )


async def removal(
    w: World,
    name: str,
    *,
    kind: str = 'other',
    dataset: Optional[int] = None,
    field: Optional[str] = None,
) -> None:
    """An index removed through the advisor (its definition quotes a planted value: no answer gives it)."""
    await w.database.execute(
        'INSERT INTO removed_indexes (index_name, table_name, definition, kind, dataset_id, field_name, size_bytes, '
        "scans, reason, removed_by, removed_by_name, dropped_at) VALUES ($1, 'samples', $2, $3, $4, $5, 12288, 0, "
        "'unused', $6, $7, clock_timestamp())",
        name,
        f"CREATE INDEX {name} ON public.samples USING btree (id) WHERE ((metadata ->> 'plot_code'::text) = "
        f"'{PRIVATE_VALUES[8]}'::text)",
        kind,
        dataset,
        field,
        ADMIN,
        await w.database.fetchval('SELECT username FROM users WHERE id = $1', ADMIN),
    )


async def dataset_names(w: World) -> dict[int, str]:
    return {row['id']: row['name'] for row in await w.database.fetch('SELECT id, name FROM datasets')}


def every_index_entry(node: Any) -> list[dict[str, Any]]:
    """Every index entry anywhere in an answer or a review (an object with a name and a definition key)."""
    found = []
    if isinstance(node, dict):
        if 'definition' in node and 'name' in node:
            found.append(node)
        for value in node.values():
            found.extend(every_index_entry(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(every_index_entry(item))
    return found


# --- Reading the jobs table, the truth list_jobs and the route answer from ---

ABOUT = "COALESCE(dataset_id, CASE WHEN args->>'dataset_id' ~ '^[0-9]+$' THEN (args->>'dataset_id')::int END)"
JOB_FACTS = (
    'SELECT id::text AS id, kind, state, dataset_id, args, created_at, started_at, finished_at, progress_done, '
    f'progress_total, message, error, failures, max_failures, created_by, dedupe_owner, {ABOUT} AS about FROM jobs'
)
# The window bounds the jobs that have ended; a job still queued or running is in it however old it is.
IN_WINDOW = "(state IN ('queued', 'running') OR created_at >= now() - make_interval(secs => $1))"


async def truth(
    w: World,
    *,
    states: tuple[str, ...] = ('failed', 'queued', 'running'),
    kind: Optional[str] = None,
    dataset: Optional[int] = None,
    hours: float = 168,
) -> dict[str, Any]:
    """The jobs that match, newest first; counts by state for the same kind, dataset and window; and the kinds in the
    window, whatever the other filters."""
    window = f'{IN_WINDOW} AND ($2::text IS NULL OR kind = $2) AND ($3::int IS NULL OR {ABOUT} = $3)'
    rows = await w.database.fetch(
        f'SELECT id::text AS id, state, kind FROM jobs WHERE {window} ORDER BY created_at DESC, id DESC',
        hours * 3600,
        kind,
        dataset,
    )
    counts = {state: 0 for state in STATES}
    for row in rows:
        counts[row['state']] += 1
    kinds = await w.database.fetch(f'SELECT DISTINCT kind FROM jobs WHERE {IN_WINDOW}', hours * 3600)
    return {
        'ids': [row['id'] for row in rows if row['state'] in states],
        'counts': counts,
        'kinds': {row['kind'] for row in kinds},
    }


async def facts(w: World, job_id: str) -> Any:
    row = await w.database.fetchrow(f'{JOB_FACTS} WHERE id = $1::uuid', job_id)
    assert row is not None, job_id
    return row


# The kinds the contract names as about no dataset: their words are given when the job belongs to none.
KNOWN_ABOUT_NONE = {
    'backup',
    'region_load',
    'selection_cleanup',
    index_advisor.KIND,
    index_actions.DROP_KIND,
}


async def kept_back(w: World, dataset_id: int) -> Optional[str]:
    """Why a job about this dataset has its words kept back (3.4), or None."""
    row = await w.database.fetchrow(
        'SELECT assistant_closed, EXISTS (SELECT 1 FROM dataset_fields f WHERE f.dataset_id = d.id '
        'AND f.values_private) AS private FROM datasets d WHERE d.id = $1',
        dataset_id,
    )
    if row is None:
        return KEPT_GONE
    return KEPT_CLOSED if row['assistant_closed'] else KEPT_PRIVATE if row['private'] else None


async def expected_kept(w: World, row: Any) -> tuple[set[str], list[int]]:
    """The sentences any one of which may keep the job's words back (none: given), and the datasets it is about."""
    args = row['args'] or {}
    if row['kind'] == FOLLOWER_SYNC and row['dataset_id'] is None:  # about every mirror of its link: it writes them
        if not await w.database.fetchval('SELECT 1 FROM primary_links WHERE id = $1', args.get('link_id')):
            return {KEPT_GONE, KEPT_UNKNOWN}, []
        mirrors = [
            m['dataset_id']
            for m in await w.database.fetch('SELECT dataset_id FROM mirrors WHERE link_id = $1', args['link_id'])
        ]
        if not mirrors:  # no mirror left: read as about a dataset that is gone
            return {KEPT_GONE}, []
        reasons = {reason for reason in [await kept_back(w, mirror) for mirror in mirrors] if reason}
        return reasons, mirrors
    if row['about'] is not None:
        reason = await kept_back(w, row['about'])
        return ({reason} if reason else set()), [row['about']]
    return (set() if row['kind'] in KNOWN_ABOUT_NONE else {KEPT_UNKNOWN}), []


async def check_job_entry(w: World, entry: dict[str, Any], dataset_names_: dict[int, str]) -> None:
    """A list_jobs entry against its row: the named facts, the free text given or kept back by 3.4, no result."""
    row = await facts(w, entry['id'])
    about = row['about']
    sentences, about_all = await expected_kept(w, row)
    syncing = row['kind'] == FOLLOWER_SYNC and row['dataset_id'] is None
    gone = about is not None and about not in dataset_names_
    assert (entry['kind'], entry['state']) == (row['kind'], row['state']), entry
    if syncing:  # about every mirror of its link: the contract names no one dataset for it
        assert entry['dataset'] is None or entry['dataset'].get('id') in about_all, entry
    elif gone:  # the contract names no name for a dataset that no longer exists: null, or its id
        assert entry['dataset'] is None or entry['dataset'].get('id') == about, entry
    else:
        assert entry['dataset'] == (None if about is None else {'id': about, 'name': dataset_names_[about]}), entry
    built = row['kind'] in ('field_index_build', 'ann_index_build')
    args = row['args'] or {}
    assert entry['field'] == ((args.get('field') or args.get('field_name')) if built else None), entry
    if row['created_by'] is not None:
        starter = await w.database.fetchval('SELECT username FROM users WHERE id = $1', row['created_by'])
    else:
        starter = 'System' if row['dedupe_owner'] == 'system' else 'Deleted user'
    assert entry['started_by'] == starter, entry
    for key in ('created_at', 'started_at', 'finished_at'):
        assert moment(entry[key]) == moment(row[key]), (key, entry[key], row[key])
    assert set(entry['progress']) == {'done', 'total', 'message'}, entry
    assert (entry['progress']['done'], entry['progress']['total']) == (row['progress_done'], row['progress_total'])
    assert (entry['failures'], entry['max_failures']) == (row['failures'], row['max_failures']), entry
    if syncing or gone:
        assert entry['link'] in [address(STATUS_PAGE), *(address(f'/datasets/{d}') for d in about_all)], entry
    else:
        assert entry['link'] == address(STATUS_PAGE if about is None else f'/datasets/{about}'), entry
    error = (row['error'] or {}).get('message') if isinstance(row['error'], dict) else None
    message, running = row['message'], row['state'] == 'running'
    given = entry['progress']['message']
    if sentences:
        assert entry['error'] is None and given is None, entry
        if error or (running and message):
            assert entry['error_withheld'] in sentences, (entry, sentences)  # even with no error yet
        elif not message:
            assert entry['error_withheld'] is None, entry  # nothing kept back
        text = json.dumps(entry)
        for planted in (error, message):
            assert not planted or planted not in text, (planted, entry)
    else:
        assert entry['error_withheld'] is None, entry
        if error == SCRUB_TEXT:
            assert scrubbed(entry['error']), entry
        else:
            assert cut_text(entry['error'], error) if error else entry['error'] is None, entry
        if message and running:
            assert cut_text(given, message), entry
        elif message:
            assert given is None or cut_text(given, message), entry
        else:
            assert given is None, entry


# --- 1. Who may use them, and the switch ---


def test_a_fresh_server_answers_admin_tools_false_and_patch_turns_it_on_and_off_changing_nothing_else(world):
    async def scenario(w):
        assert await settings(w) == {**SHIPPED, 'admin_tools': False}
        stored = "SELECT to_jsonb(s) - 'admin_tools' - 'updated_at' - 'updated_by' FROM assistant_settings s"
        before = await w.database.fetchval(stored)
        assert await settings(w, {'admin_tools': True}) == {**SHIPPED, 'admin_tools': True}
        assert await w.database.fetchval(stored) == before  # nothing else stored changed
        answered = await w.call(ADMIN, 'PATCH', SETTINGS, json={'admin_tools': False})
        assert answered.status_code == 200 and answered.json() == {**SHIPPED, 'admin_tools': False}, answered.text
        # Other keys leave it as it is; it leaves them as they are.
        await settings(w, {'admin_tools': True})
        assert (await settings(w, {'mcp_enabled': True, 'sample_fields': True}))['admin_tools'] is True
        assert await settings(w, {'admin_tools': False}) == {**SHIPPED, 'mcp_enabled': True, 'sample_fields': True}
        assert (await settings(w, {'roles': {'power_user': True}}))['admin_tools'] is False

    world(scenario)


def test_only_administrators_reach_the_admin_tools_switch(world):
    async def scenario(w):
        for user_id, status in ((POWER, 403), (GUEST, 403), (None, 401)):
            for method, body in (('GET', None), ('PATCH', {'admin_tools': True})):
                response = await w.call(user_id, method, SETTINGS, **({} if body is None else {'json': body}))
                assert response.status_code == status, (user_id, method, response.text)
                assert 'admin_tools' not in response.text, (user_id, method, response.text)
        assert (await settings(w)).get('admin_tools') is False

    world(scenario)


def test_a_server_whose_settings_row_is_missing_reads_admin_tools_false(world):
    async def scenario(w):
        await w.database.execute('DELETE FROM assistant_settings')
        assert (await settings(w)).get('admin_tools') is False
        assert await listed_for(ADMIN) == THIRTEEN
        assert (await settings(w, {'admin_tools': True})).get('admin_tools') is True  # PATCH puts the row back
        assert await w.database.fetchval('SELECT count(*) FROM assistant_settings') == 1

    world(scenario)


def test_who_may_call_an_administration_tool_in_every_combination_with_its_sentence(world):
    """Door on or off, administrator / power user / guest, let in or not, the switch on or off: only a let-in
    administrator with the switch on calls them; every refusal is not_allowed with exactly its sentence, the door's own
    first. tools_for agrees at every step, and the thirteen tools don't depend on the switch."""

    async def scenario(w):
        callers = {'admin': OTHER_ADMIN, 'power_user': POWER, 'guest': GUEST}
        for door_on in (False, True):
            for switch in (False, True):
                await settings(w, {'mcp_enabled': door_on, 'admin_tools': switch})
                for role, user_id in callers.items():
                    for let_in in (True, False):
                        put = await w.call(ADMIN, 'PUT', f'{SETTINGS}/users/{user_id}', json={'allowed': let_in})
                        assert put.status_code == 200, put.text
                        step = (door_on, switch, role, let_in)
                        if not door_on:
                            expected: Optional[str] = MCP_OFF
                        elif role == 'guest' or not let_in:
                            expected = NOT_LET_IN
                        elif not switch:
                            expected = SWITCHED_OFF
                        elif role != 'admin':
                            expected = FOR_ADMINISTRATORS
                        else:
                            expected = None
                        for name in FOUR:
                            answer = await call(user_id, name, {})
                            if expected is None:
                                assert answer['ok'], (step, name, answer)
                            else:
                                assert answer['error'] == {
                                    'code': 'not_allowed',
                                    'message': expected,
                                    'retryable': False,
                                }, (step, name, answer)
                        assert await listed_for(user_id) == THIRTEEN + (FOUR if expected is None else []), step
                        if door_on and let_in and role != 'guest':
                            assert (await call(user_id, 'list_datasets'))['ok'], step

    world(scenario)


def test_a_role_or_the_switch_changed_between_two_calls_decides_the_next_call(world):
    async def scenario(w):
        await switch_on(w)
        assert (await call(OTHER_ADMIN, 'system_status'))['ok']
        # An administrator made a power user (power users are let in): the next call is refused.
        made = await w.call(ADMIN, 'PATCH', f'/api/users/{OTHER_ADMIN}', json={'role': 'power_user'})
        assert made.status_code == 200, made.text
        for name in FOUR:
            assert await refusal(OTHER_ADMIN, name) == FOR_ADMINISTRATORS, name
        assert await listed_for(OTHER_ADMIN) == THIRTEEN
        assert (await call(OTHER_ADMIN, 'list_datasets'))['ok']
        # Made an administrator again: allowed again.
        made = await w.call(ADMIN, 'PATCH', f'/api/users/{OTHER_ADMIN}', json={'role': 'admin'})
        assert made.status_code == 200, made.text
        assert (await call(OTHER_ADMIN, 'list_jobs'))['ok']
        # The switch turned off, then on again.
        await settings(w, {'admin_tools': False})
        for name in FOUR:
            assert await refusal(OTHER_ADMIN, name) == SWITCHED_OFF, name
        assert await listed_for(OTHER_ADMIN) == THIRTEEN
        await settings(w, {'admin_tools': True})
        assert (await call(OTHER_ADMIN, 'backup_status'))['ok']
        assert await listed_for(OTHER_ADMIN) == THIRTEEN + FOUR
        # Made a power user while power users aren't let in: the door's own sentence comes first.
        await settings(w, {'roles': {'power_user': False}})
        made = await w.call(ADMIN, 'PATCH', f'/api/users/{OTHER_ADMIN}', json={'role': 'power_user'})
        assert made.status_code == 200, made.text
        assert await refusal(OTHER_ADMIN, 'index_advice') == NOT_LET_IN

    world(scenario)


def test_a_role_or_the_switch_changed_while_a_tool_reads_refuses_its_next_request(world, monkeypatch):
    """Read afresh at every request the tool makes: the change is made when the tool's first in-process request
    arrives, before the app serves it; that request is refused, so the call is not answered."""

    async def scenario(w):
        await switch_on(w)
        changes = {
            'made a power user': ('PATCH', f'/api/users/{OTHER_ADMIN}', {'role': 'power_user'}),
            'switched off': ('PATCH', SETTINGS, {'admin_tools': False}),
        }
        undo = {
            'made a power user': ('PATCH', f'/api/users/{OTHER_ADMIN}', {'role': 'admin'}),
            'switched off': ('PATCH', SETTINGS, {'admin_tools': True}),
        }
        real = main.app
        for what, (method, path, body) in changes.items():
            for name in ('system_status', 'backup_status', 'list_jobs', 'index_advice'):
                assert (await call(OTHER_ADMIN, name))['ok'], (what, name)
                seen: list[str] = []
                changed: list[int] = []
                lock = asyncio.Lock()

                async def watched(scope, receive, send):
                    if scope.get('type') == 'http' and SCOPE_KEY in scope:
                        seen.append(scope['path'])
                        async with lock:  # every request of the tool waits until the change is made
                            if not changed:
                                made = await w.call(ADMIN, method, path, json=body)
                                assert made.status_code == 200, made.text
                                changed.append(made.status_code)
                    await real(scope, receive, send)

                monkeypatch.setattr(main, 'app', watched)
                try:
                    try:
                        answer: Optional[dict[str, Any]] = await assistant.call_tool(
                            user_id=OTHER_ADMIN,
                            door='mcp',
                            name=name,
                            arguments={},
                        )
                    except Exception:  # a refused inner request is the tool's own fault: call_tool may raise
                        answer = None
                finally:
                    monkeypatch.setattr(main, 'app', real)
                assert seen, f'{name} made no request through the app'
                assert answer is None or (
                    answer['ok'] is False and answer['error']['code'] in ('not_allowed', 'refused')
                ), (
                    what,
                    name,
                    answer,
                )
                back_method, back_path, back_body = undo[what]
                restored = await w.call(ADMIN, back_method, back_path, json=back_body)
                assert restored.status_code == 200, restored.text

    world(scenario)


RELEASE_BEFORE_0040 = 'v2.13.2'
INDEXES = "SELECT tablename || ' ' || indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1"


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'), reason='Requires disposable PostgreSQL')
def test_0040_adds_the_switch_off_and_leaves_every_row_as_it_was(database, monkeypatch):
    make, psql, _, migrate = database
    old_schema = subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{RELEASE_BEFORE_0040}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = make(old_schema + "INSERT INTO datasets (id, name) VALUES (1, 'before'), (2, 'also-before');")
    apply_before(psql, name, '0040')
    psql(name, 'UPDATE assistant_settings SET mcp_enabled = TRUE, sample_fields = TRUE')
    tables = set(psql(name, TABLES).split(',')) - {'schema_migrations'}
    columns = {table: psql(name, COLUMNS.format(table=table)) for table in tables}
    rows = rows_as_they_are(psql, name, columns)
    indexes = set(psql(name, INDEXES).splitlines())
    (own,) = MIGRATIONS.glob('0040_*.sql')
    apply_migration(psql, name, own)
    assert rows_as_they_are(psql, name, columns) == rows  # every table's rows, on the columns it had
    # The column and nothing else: no table, no index, no other column.
    assert set(psql(name, TABLES).split(',')) - {'schema_migrations'} == tables
    grown = {table: psql(name, COLUMNS.format(table=table)) for table in tables}
    assert {table for table in tables if grown[table] != columns[table]} == {'assistant_settings'}
    assert len(grown['assistant_settings'].split(', ')) == len(columns['assistant_settings'].split(', ')) + 1
    assert grown['assistant_settings'].startswith(columns['assistant_settings'])
    assert set(psql(name, INDEXES).splitlines()) == indexes
    outcome = migrate(name)
    assert outcome.returncode == 0, f'apply_migrations.sh failed\n{outcome.stdout}\n{outcome.stderr}'

    async def after() -> None:
        dsn = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, init=init_connection)
        monkeypatch.setattr(database_module.db, 'pool', pool)
        monkeypatch.setattr(datasets, '_array_columns_cache', None)
        try:
            raw = secrets.token_urlsafe(32)
            await pool.execute('UPDATE users SET must_change_password = FALSE WHERE id = $1', ADMIN)
            await pool.execute(
                "INSERT INTO api_tokens (user_id, token, name) VALUES ($1, $2, 'upgrade')",
                ADMIN,
                hash_token(raw),
            )
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                state = (await client.get(SETTINGS, headers={'Authorization': f'Bearer {raw}'})).json()
            assert state.get('admin_tools') is False, state
            assert (state['mcp_enabled'], state['sample_fields']) == (True, True), state  # kept as they were
            assert await listed_for(ADMIN) == THIRTEEN
        finally:
            await pool.close()

    asyncio.run(after())


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'), reason='Requires disposable PostgreSQL')
def test_0041_adds_two_indexes_on_the_jobs_table_and_changes_no_row_and_no_column(database):
    make, psql, _, migrate = database
    old_schema = subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{RELEASE_BEFORE_0040}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = make(
        old_schema + "INSERT INTO datasets (id, name) VALUES (1, 'before');"
        "INSERT INTO jobs (id, kind, dataset_id, state) VALUES (gen_random_uuid(), 'statistics', 1, 'queued'), "
        "(gen_random_uuid(), 'backup', NULL, 'queued');",
    )
    apply_before(psql, name, '0041')
    tables = set(psql(name, TABLES).split(',')) - {'schema_migrations'}
    columns = {table: psql(name, COLUMNS.format(table=table)) for table in tables}
    rows = rows_as_they_are(psql, name, columns)
    indexes = set(psql(name, INDEXES).splitlines())
    (own,) = MIGRATIONS.glob('0041_*.sql')
    apply_migration(psql, name, own)
    assert rows_as_they_are(psql, name, columns) == rows  # no row changed
    assert set(psql(name, TABLES).split(',')) - {'schema_migrations'} == tables
    assert {table: psql(name, COLUMNS.format(table=table)) for table in tables} == columns  # no column
    now = set(psql(name, INDEXES).splitlines())
    assert indexes <= now, sorted(indexes - now)  # none removed or changed
    added = sorted(now - indexes)
    assert len(added) == 2 and all(line.startswith('jobs ') for line in added), added
    outcome = migrate(name)
    assert outcome.returncode == 0, f'apply_migrations.sh failed\n{outcome.stdout}\n{outcome.stderr}'


# --- 2. How they are listed ---


def test_the_thirteen_tools_are_unchanged_in_their_order():
    assert names(assistant.tools()) == THIRTEEN
    assert names(assistant.tools()) == names(assistant.tools())


def test_admin_tools_are_the_four_read_only_tools_with_the_arguments_and_routes_the_contract_names():
    tools = list(assistant.admin_tools())
    assert names(tools) == FOUR
    assert names(assistant.admin_tools()) == FOUR
    assert not set(FOUR) & set(names(assistant.tools()))
    served = app_routes()
    by_name = {tool.name: tool for tool in tools}
    for tool in tools:
        assert tool.kind == 'read', tool.name
        assert isinstance(tool.description, str) and len(tool.description.split()) >= 5, tool.name
        schema = plain(tool.input_schema)
        assert schema.get('type') == 'object', (tool.name, schema)
        assert not schema.get('required'), (tool.name, schema)
        assert tool.routes, tool.name
        for method, template in tool.routes:
            path = template if template.startswith('/api') else '/api' + template
            assert method == 'GET' and (method, path) in served, (tool.name, method, template)
    properties = {tool.name: set(plain(tool.input_schema).get('properties', {})) for tool in tools}
    assert properties == {
        'system_status': set(),
        'backup_status': set(),
        'list_jobs': {'state', 'kind', 'dataset', 'since_hours', 'limit', 'offset'},
        'index_advice': {'dataset', 'section', 'limit', 'offset'},
    }

    def routes(name: str) -> set[tuple[str, str]]:
        return {(m, t if t.startswith('/api') else '/api' + t) for m, t in by_name[name].routes}

    assert ('GET', '/api/admin/status') in routes('system_status')
    assert {('GET', '/api/admin/status'), ('GET', '/api/admin/backups/schedule')} <= routes('backup_status')
    assert ('GET', ALL_JOBS) in routes('list_jobs')
    assert {('GET', '/api/admin/index-advisor'), ('GET', '/api/datasets/{id}/index-advisor')} <= routes('index_advice')
    status = by_name['system_status'].description.lower()
    assert all(word in status for word in ('migration', 'restart', 'release')), status
    advice = by_name['index_advice'].description.lower()
    assert all(word in advice for word in ('duplicate', 'operator', 'prefix', 'history', 'needed', 'remov')), advice


def test_tools_for_is_the_thirteen_then_the_four_only_for_a_let_in_administrator_with_the_switch_on(world):
    async def scenario(w):
        await settings(w, {'mcp_enabled': True, 'roles': {'admin': True, 'power_user': True, 'guest': True}})
        for switch in (False, True):
            await settings(w, {'admin_tools': switch})
            assert await listed_for(ADMIN) == THIRTEEN + (FOUR if switch else []), switch
            for user_id in (POWER, GUEST, 999999):
                assert await listed_for(user_id) == THIRTEEN, (switch, user_id)
            assert await listed_for(ADMIN, door='chat') == THIRTEEN, switch  # the chat door lets nobody in
        given = list(await assistant.tools_for(user_id=ADMIN, door='mcp'))

        def described(tools: list[Any]) -> list[Any]:
            return [(tool.name, tool.description, plain(tool.input_schema)) for tool in tools]

        assert described(given) == described([*assistant.tools(), *assistant.admin_tools()])
        await settings(w, {'mcp_enabled': False})
        assert await listed_for(ADMIN) == THIRTEEN

    world(scenario)


def expected_listing(tools: Any) -> list[Any]:
    return [(tool.name, tool.description, json.loads(json.dumps(tool.input_schema))) for tool in tools]


def test_mcp_lists_the_thirteen_for_a_power_user_and_for_an_administrator_with_the_switch_off(world):
    async def scenario(w):
        await settings(w, {'mcp_enabled': True, 'roles': {'admin': True, 'power_user': True}})
        door = Door(w)
        for user_id in (ADMIN, POWER):
            token = (await make_token(w, user_id))['token']
            listed = await door.result('tools/list', {}, token)
            assert [tool['name'] for tool in listed['tools']] == THIRTEEN, user_id

    world(scenario)


def test_mcp_lists_the_four_after_the_thirteen_for_an_administrator_with_the_switch_on_each_read_only(world):
    async def scenario(w):
        await switch_on(w)
        door = Door(w)
        mine = (await make_token(w, ADMIN))['token']
        theirs = (await make_token(w, POWER))['token']
        listed = (await door.result('tools/list', {}, mine))['tools']
        assert [tool['name'] for tool in listed] == THIRTEEN + FOUR
        given = await assistant.tools_for(user_id=ADMIN, door='mcp')
        assert [(t['name'], t.get('description'), t['inputSchema']) for t in listed] == expected_listing(given)
        assert all(t.get('annotations', {}).get('readOnlyHint') is True for t in listed), listed
        assert [tool['name'] for tool in (await door.result('tools/list', {}, theirs))['tools']] == THIRTEEN
        # A call through the door is the core's answer.
        called = await door.call(mine, 'list_jobs', {})
        reference = await call(ADMIN, 'list_jobs', {})
        assert reference['ok'] and called.get('isError') in (None, False), called
        assert {k: v for k, v in called['structuredContent'].items() if k != 'since'} == {
            k: v for k, v in reference['result'].items() if k != 'since'
        }
        # The switch off: listed no more, and a call is refused with its sentence.
        await settings(w, {'admin_tools': False})
        assert [tool['name'] for tool in (await door.result('tools/list', {}, mine))['tools']] == THIRTEEN
        refused = await door.call(mine, 'list_jobs', {})
        assert refused['isError'] is True, refused
        assert refused['structuredContent'] == {
            'error': {'code': 'not_allowed', 'message': SWITCHED_OFF, 'retryable': False},
        }, refused

    world(scenario)


def test_mcp_an_administration_tool_called_by_a_power_user_is_a_refused_tool_result_not_an_unknown_tool(world):
    async def scenario(w):
        await switch_on(w)
        door = Door(w)
        token = (await make_token(w, POWER))['token']
        for name in FOUR:
            for arguments in ({}, {'limit': 0}):
                called = await door.call(token, name, arguments)
                text = [block['text'] for block in called['content'] if block.get('type') == 'text']
                assert called['isError'] is True and text == [FOR_ADMINISTRATORS], (name, called)
                assert called['structuredContent'] == {
                    'error': {'code': 'not_allowed', 'message': FOR_ADMINISTRATORS, 'retryable': False},
                }, (name, called)
        # A name that is no tool at all is still the protocol's error.
        _, message = await door.ask('tools/call', {'name': 'no_such_tool', 'arguments': {}}, token)
        assert message is not None and message.get('error', {}).get('code') == -32602, message

    world(scenario)


def test_mcp_a_person_made_one_from_the_other_gets_the_others_list_and_calls(world):
    async def scenario(w):
        await switch_on(w)
        door = Door(w)
        was_admin = (await make_token(w, OTHER_ADMIN))['token']
        was_power = (await make_token(w, POWER))['token']
        made = await w.call(ADMIN, 'PATCH', f'/api/users/{OTHER_ADMIN}', json={'role': 'power_user'})
        assert made.status_code == 200, made.text
        made = await w.call(ADMIN, 'PATCH', f'/api/users/{POWER}', json={'role': 'admin'})
        assert made.status_code == 200, made.text
        assert [t['name'] for t in (await door.result('tools/list', {}, was_admin))['tools']] == THIRTEEN
        refused = await door.call(was_admin, 'list_jobs', {})
        assert refused['isError'] is True and refused['structuredContent']['error']['message'] == FOR_ADMINISTRATORS
        assert [t['name'] for t in (await door.result('tools/list', {}, was_power))['tools']] == THIRTEEN + FOUR
        allowed = await door.call(was_power, 'list_jobs', {})
        assert allowed.get('isError') in (None, False) and 'jobs' in allowed['structuredContent'], allowed

    world(scenario)


# --- 3. What they may read, and what they never show ---


def test_the_planted_rows_are_where_the_administrators_routes_read_them(world, tmp_path, monkeypatch):
    """The fixtures of section 3 really are in the database and on disk, and the administrators' own routes give them
    as written: the private and closed values in job errors and index definitions, the paths, the hashes."""

    async def scenario(w):
        planted = await plant_everything(w, tmp_path, monkeypatch)
        jobs = planted['jobs']
        global_jobs = (await w.call(ADMIN, 'GET', '/api/admin/jobs', params={'limit': 100})).json()['items']
        by_id = {item['id']: item for item in global_jobs}
        assert PRIVATE_VALUES[5] in json.dumps(by_id[jobs['private_review']]['error'])
        assert NONE_ERROR in json.dumps(by_id[jobs['none_failed']]['error'])
        assert RESULT_MARK in json.dumps(by_id[jobs['none_result']]['result'])
        private_jobs = (await w.call(ADMIN, 'GET', f'/api/datasets/{PRIVATE_DS}/jobs', params={'limit': 100})).json()
        private_by_id = {item['id']: item for item in private_jobs['items']}
        assert PRIVATE_VALUES[0] in json.dumps(private_by_id[jobs['private_failed']]['error'])
        assert jobs['private_review'] in private_by_id  # the review names the dataset
        active = (await w.call(ADMIN, 'GET', f'/api/datasets/{PRIVATE_DS}/jobs/active')).json()
        assert any(PRIVATE_VALUES[2] in (item['progress']['message'] or '') for item in active['running']), active
        closed_jobs = (await w.call(ADMIN, 'GET', f'/api/datasets/{CLOSED_DS}/jobs', params={'limit': 100})).json()
        assert CLOSED_VALUES[0] in json.dumps(closed_jobs)
        # The hand-made indexes in the system review, the app's in the ordinary dataset's, as PostgreSQL prints them.
        system = (await w.call(ADMIN, 'GET', '/api/admin/index-advisor')).json()
        entries = {entry['name']: entry for entry in every_index_entry(system['review'])}
        assert PRIVATE_VALUES[6] in entries['idx_hand_private']['definition'], entries.get('idx_hand_private')
        assert CLOSED_VALUES[5] in entries['idx_hand_closed']['definition'], entries.get('idx_hand_closed')
        ordinary = (await w.call(ADMIN, 'GET', f'/api/datasets/{ORDINARY_DS}/index-advisor')).json()
        made = {entry['name']: entry for entry in every_index_entry(ordinary['review'])}
        assert "'crop'::text" in made[planted['field_index']]['definition'], made.get(planted['field_index'])
        both = {**entries, **made}
        for name in (*LOOKALIKES, *NUMERIC_REAL, *WHOLE_HAND_MADE, *planted['built'].values(), HAND_MADE_NAMES):
            assert name in both, (name, sorted(both))
        for name in NUMERIC_REAL:  # each holds its planted number, as PostgreSQL prints it
            assert planted_numbers_in(both[name]['definition']), both[name]
            assert withheld_by_rule(name, both[name]['table'], both[name]['definition']), both[name]
        for name in (*WHOLE_HAND_MADE, *planted['built'].values()):
            assert not withheld_by_rule(name, both[name]['table'], both[name]['definition']), both[name]
        assert 'numeric(10,2)' in both['idx_hand_typmod']['definition'], both['idx_hand_typmod']
        # The forms PostgreSQL prints: a number as a function's argument right after a cast and a word; a type's
        # modifier on a character varying and on a timestamp.
        for name in (*FLAG_REAL, 'idx_hand_varchar', 'idx_hand_ts'):
            print(f'{name}: {await w.database.fetchval("SELECT pg_get_indexdef($1::regclass)", name)}')
        for name, number in (
            ('idx_priv_flag', FLAG_NUMBERS['priv_and']),
            ('idx_priv_flag_or', FLAG_NUMBERS['priv_or']),
            ('idx_priv_flag_bare', FLAG_NUMBERS['priv_bare']),
            ('idx_shut_flag', FLAG_NUMBERS['shut_and']),
        ):
            printed = both[name]['definition']
            assert re.search(rf'::boolean (?:AND|OR) (?:\w+\.)?spec_flag\({number}\)', printed), printed
        assert '::character varying(255)' in both['idx_hand_varchar']['definition'], both['idx_hand_varchar']
        assert '::timestamp(3) without time zone' in both['idx_hand_ts']['definition'], both['idx_hand_ts']
        for name in LOOKALIKES:
            assert withheld_by_rule(name, both[name]['table'], both[name]['definition']), both[name]
        assert IDX_VALUES[4] in both[f'idx_samples_meta_{ORDINARY_DS}_site']['definition']
        assert len(both['idx_hand_long']['definition']) > 2000
        assert both['ex_hand_site']['kind'] == 'core' and both['idx_samples_site_gin']['kind'] == 'array'
        vector = both[planted['built']['vector']]
        assert vector['kind'] == 'ann' and "field_name = 'clip'" in vector['definition'], vector
        # The status page reads the paths and hashes; the backups card the folder.
        status = await w.call(ADMIN, 'GET', '/api/admin/status')
        assert CACHE_MARK in status.text and RUNNING_TREE in status.text and CHECKOUT_TREE in status.text
        assert status.json()['backups']['folder'] == planted['folder'] and status.json()['backups']['level'] == 'failed'
        assert status.json()['server']['restart_needed'] is True
        assert HOST in (await w.call(ADMIN, 'GET', '/api/workers')).text
        assert EMAIL_DOMAIN in (await w.call(ADMIN, 'GET', '/api/users')).text
        assert STORE_KEY[-4:] in (await w.call(ADMIN, 'GET', '/api/storage/configs')).text
        # The dataset with a private field, and the closed one.
        schema = (await w.call(ADMIN, 'GET', f'/api/datasets/{PRIVATE_DS}/schema')).json()
        assert {entry['name']: entry['values_private'] for entry in schema}['plot_code'] is True
        assert (await w.call(ADMIN, 'GET', f'/api/datasets/{CLOSED_DS}')).json()['assistant_closed'] is True

    world(scenario)


async def check_real_definitions(w: World, answers: list[Any], built: dict[str, str]) -> None:
    """In answers drawn from the real reviews, every index has its definition kept back or given whole exactly as 3.4
    judges what it quotes, against what the routes print: the hand-made ones quoting values and the look-alikes kept
    back; the one quoting field names, every index schema.sql ships (one whose predicate quotes a constant among
    them) and the app's own (a field's, a list column's, a vector index) whole."""
    printed: dict[str, str] = {}
    tables: dict[str, str] = {}
    for path in ('/api/admin/index-advisor', f'/api/datasets/{ORDINARY_DS}/index-advisor'):
        for entry in every_index_entry((await w.call(ADMIN, 'GET', path)).json()['review']):
            printed[entry['name']] = entry['definition']
            tables[entry['name']] = entry['table']
    withheld = {name for name in printed if withheld_by_rule(name, tables[name], printed[name])}
    kept_back = {*HAND_MADE, *LOOKALIKES, *NUMERIC_REAL}
    whole = {HAND_MADE_NAMES, *WHOLE_HAND_MADE, *built.values()}
    assert kept_back <= withheld, sorted(kept_back - withheld)
    assert not whole & withheld, sorted(whole & withheld)
    assert not planted_numbers_in(json.dumps(answers)), planted_numbers_in(json.dumps(answers))
    seen = {name: 0 for name in (*kept_back, *whole)}
    shipped_quoting = set()
    for entry in every_index_entry(answers):
        name = entry['name']
        assert name in printed, name
        if name in withheld:
            assert entry['definition'] is None and entry['definition_withheld'] is True, entry
        else:
            assert (entry['definition'], entry['definition_withheld']) == (printed[name], False), entry
        if name in seen:
            seen[name] += 1
        if (name, tables[name]) in SHIPPED_INDEXES and "'" in printed[name]:
            shipped_quoting.add(name)
    assert all(seen.values()), seen
    assert shipped_quoting, 'no index schema.sql ships with a quoted constant was answered'


async def every_answer() -> dict[str, list[Any]]:
    """Every tool, with every argument that can reach the planted rows, every page: whole answers, errors included."""
    found: dict[str, list[Any]] = {name: [] for name in FOUR}
    for name in ('system_status', 'backup_status'):
        found[name].append(await call(ADMIN, name, {}))

    async def walk(name: str, arguments: dict[str, Any]) -> None:
        offset = 0
        for _ in range(200):
            answer = await call(ADMIN, name, {**arguments, 'limit': 50, 'offset': offset})
            found[name].append(answer)
            if not answer['ok'] or answer['result'].get('next_offset') is None:
                return
            offset = answer['result']['next_offset']

    for state in (None, 'needs_attention', *STATES):
        for dataset in (None, PRIVATE_DS, CLOSED_DS, ORDINARY_DS):
            for hours in (None, 8760):
                given = {'state': state, 'dataset': dataset, 'since_hours': hours}
                await walk('list_jobs', {key: value for key, value in given.items() if value is not None})
    for kind in ('field_index_build', 'bulk_mutation', 'statistics', 'index_review', 'selection_cleanup'):
        for state in ('needs_attention', 'failed', 'running', 'succeeded'):
            await walk('list_jobs', {'kind': kind, 'state': state, 'since_hours': 8760})
    for dataset in (None, PRIVATE_DS, CLOSED_DS, ORDINARY_DS):
        for section_ in (None, *SECTIONS):
            given = {'dataset': dataset, 'section': section_}
            await walk('index_advice', {key: value for key, value in given.items() if value is not None})
    return found


def results(answers: list[Any]) -> list[dict[str, Any]]:
    return [answer['result'] for answer in answers if answer['ok']]


def test_no_answer_holds_a_private_value_or_anything_of_a_closed_dataset(world, tmp_path, monkeypatch):
    async def scenario(w):
        planted = await plant_everything(w, tmp_path, monkeypatch)
        jobs = planted['jobs']
        await switch_on(w)
        answers = await every_answer()
        text = json.dumps(answers)
        for value in (*NEVER_VALUES, RESULT_MARK, ARG_MARK, *TAILS):
            assert value not in text, value
        assert not planted_numbers_in(text), planted_numbers_in(text)
        for name, given in answers.items():
            ok = results(given)
            assert ok, f'{name} never answered'
            for result in ok:
                assert len(json.dumps(result)) <= ANSWER_LIMIT, name
                only_named_keys(name, result)
                assert 'result' not in keys_in(result), name  # a job's result is never given
        # Each planted job, wherever it was listed: kept back, or given cut to 500 characters.
        listed: dict[str, list[dict[str, Any]]] = {}
        for result in results(answers['list_jobs']):
            for entry in result['jobs']:
                listed.setdefault(entry['id'], []).append(entry)
        assert set(jobs.values()) <= set(listed), set(jobs) - {k for k, v in jobs.items() if v in listed}
        names_ = await dataset_names(w)
        for key, job_id in jobs.items():
            for entry in listed[job_id]:
                await check_job_entry(w, entry, names_)
        for key, sentence in (
            ('private_failed', KEPT_PRIVATE),
            ('private_running', KEPT_PRIVATE),
            ('private_review', KEPT_PRIVATE),
            ('closed_failed', KEPT_CLOSED),
            ('closed_running', KEPT_CLOSED),
            ('closed_review', KEPT_CLOSED),
            ('gone_failed', KEPT_GONE),
            ('gone_review', KEPT_GONE),
        ):
            assert all(
                e['error'] is None and e['progress']['message'] is None and e['error_withheld'] == sentence
                for e in listed[jobs[key]]
            ), key
        assert all(cut_text(e['error'], ORD_ERROR) for e in listed[jobs['ordinary_failed']])
        assert all(cut_text(e['error'], NONE_ERROR) for e in listed[jobs['none_failed']])
        assert all(cut_text(e['progress']['message'], ORD_PROGRESS) for e in listed[jobs['ordinary_running']])
        # The hand-made definitions quoting values are kept back; the one quoting field names only is given, and so is
        # every index of the app's own kinds, whole, as PostgreSQL printed it.
        await check_real_definitions(w, results(answers['index_advice']), planted['built'])

    world(scenario)


def test_no_answer_carries_a_secret_an_address_a_host_a_hash_or_a_path_but_the_backup_folder(
    world,
    tmp_path,
    monkeypatch,
):
    async def scenario(w):
        planted = await plant_everything(w, tmp_path, monkeypatch)
        await switch_on(w)
        never = [
            *planted['never'],
            (await make_token(w, ADMIN))['token'],
            (await make_token(w, ADMIN, 'full'))['token'],
        ]
        never = [value for value in never if value not in planted['folder']]  # the backup folder may be given
        answers: dict[str, list[Any]] = {
            'system_status': [await admin('system_status')],
            'backup_status': [await admin('backup_status')],
            'list_jobs': await pages('list_jobs', {'state': 'failed', 'since_hours': 8760}),
            'index_advice': [await admin('index_advice'), await admin('index_advice', {'dataset': ORDINARY_DS})],
        }
        for state in ('running', 'succeeded', 'queued'):
            answers['list_jobs'] += await pages('list_jobs', {'state': state, 'since_hours': 8760})
        for section_ in ('needed', 'too_new', 'removed', 'possibly_unused', 'overlapping'):
            answers['index_advice'] += await pages('index_advice', {'section': section_})
        for name, given in answers.items():
            text = json.dumps(given)
            for value in never:
                assert value not in text, (name, value)
            assert not IPV4.search(text), (name, IPV4.search(text))
        status = json.dumps(answers['system_status'])
        assert not HEX_RUN.search(status), HEX_RUN.search(status)
        assert answers['system_status'][0]['server']['restart_needed'] is True
        assert [disk['folders'] for disk in answers['system_status'][0]['cache_disks']] == [
            ['S3 image cache', 'Crop cache', 'Composite cache'],
        ]  # the folders' labels, not their paths
        assert answers['backup_status'][0]['folder'] == planted['folder']  # the backup folder may be given

    world(scenario)


def test_a_closed_dataset_is_named_by_its_id_and_name_wherever_datasets_are_named(world):
    async def scenario(w):
        await three_datasets(w)
        jobs = await plant_jobs(w)
        await review_job(w, None, result=system_review())
        await review_job(w, CLOSED_DS, result=review(CLOSED_DS, too_new=[field_index(CLOSED_DS, 'crop')]))
        assert (await w.call(ADMIN, 'GET', '/api/admin/index-advisor')).json()['review'] == system_review()
        closed_route = (await w.call(ADMIN, 'GET', f'/api/datasets/{CLOSED_DS}/index-advisor')).json()
        assert [entry['dataset_id'] for entry in closed_route['review']['too_new']] == [CLOSED_DS], closed_route
        await switch_on(w)
        named = {'id': CLOSED_DS, 'name': NAMES[CLOSED_DS]}
        listed = await admin('list_jobs', {'dataset': CLOSED_DS, 'state': 'failed', 'since_hours': 8760})
        mine = {entry['id']: entry for entry in listed['jobs']}
        for key in ('closed_failed', 'closed_review'):
            assert mine[jobs[key]]['dataset'] == named, mine[jobs[key]]
        system = await admin('index_advice')
        closed = [entry for entry in every_index_entry(system) if entry['name'].startswith('embeddings_d301')]
        assert closed and all(entry['dataset'] == named for entry in closed), closed
        one = await admin('index_advice', {'dataset': CLOSED_DS, 'section': 'too_new'})
        assert one['dataset'] == named and one['link'] == address(f'/datasets/{CLOSED_DS}'), one
        assert [entry['dataset'] for entry in one['too_new']] == [named], one

    world(scenario)


async def many_jobs(w: World, kind: str, dataset: Optional[int], how_many: int, text: str) -> None:
    await w.database.execute(
        'INSERT INTO jobs (id, kind, dataset_id, state, error, created_at, updated_at, available_at, started_at, '
        'finished_at, failures, max_failures) '
        "SELECT gen_random_uuid(), $1, $2, 'failed', jsonb_build_object('type', 'RuntimeError', 'message', "
        "$3 || ' ' || n || ' ' || repeat('x', 1400)), t, t, t, t, t, 1, 5 "
        'FROM generate_series(1, $4) n, LATERAL (SELECT clock_timestamp() - make_interval(secs => n * 7) AS t) moment',
        kind,
        dataset,
        text,
        how_many,
    )


def test_every_answer_is_at_most_20_kb_and_its_lists_page_with_true_totals(world, tmp_path, monkeypatch):
    async def scenario(w):
        await three_datasets(w)
        await plant_server(w, tmp_path, monkeypatch)
        await many_jobs(w, 'duplicate_maintenance', ORDINARY_DS, 300, 'ordinary failure')
        await many_jobs(w, 'selection_cleanup', None, 300, 'cleanup failure')
        await many_jobs(w, 'backup', None, 40, long_text('BackupError: ', ' BACKUPERR-TAIL'))
        long_definition = 'CREATE INDEX {name} ON public.samples USING btree (dataset_id, id, created_at) ' + (
            'WHERE (dataset_id IS NOT NULL) ' * 8
        )

        def big(prefix: str, n: int) -> dict[str, Any]:
            name = f'{prefix}_{n:04d}'
            return index_entry(name, definition=long_definition.format(name=name), size=1000 + n)

        lists = {
            'overlapping': [
                {'redundant': big('idx_red', n), 'kept': big('idx_kept', n), 'rule': 'prefix', 'reason': 'a prefix'}
                for n in range(250)
            ],
            'possibly_unused': [{**big('idx_idle', n), 'invalid': False, 'idle_days': 20.0} for n in range(250)],
            'needed': [big('idx_need', n) for n in range(250)],
            'too_new': [big('idx_new', n) for n in range(250)],
        }
        stored = review()
        stored.update(lists)
        await review_job(w, None, result=stored)
        # Planted where the administrators' routes read them.
        assert (await w.call(ADMIN, 'GET', '/api/admin/index-advisor')).json()['review'] == stored
        global_jobs = (await w.call(ADMIN, 'GET', '/api/admin/jobs', params={'limit': 100})).json()
        assert len(global_jobs['items']) == 100 and global_jobs['next_cursor'], global_jobs['next_cursor']
        ordinary = (await w.call(ADMIN, 'GET', f'/api/datasets/{ORDINARY_DS}/jobs', params={'limit': 100})).json()
        assert len(ordinary['items']) == 100 and ordinary['next_cursor'], ordinary['next_cursor']
        await switch_on(w)
        for name in ('system_status', 'backup_status'):
            await admin(name)  # at most 20 KB, checked by admin()
        backup = await admin('backup_status')
        assert len(backup['recent_runs']) == 10, len(backup['recent_runs'])
        # Jobs: every page fits, and together the pages are every job that matches, once each.
        cases: list[dict[str, Any]] = [{}, {'state': 'failed'}, {'dataset': ORDINARY_DS}, {'kind': 'selection_cleanup'}]
        for arguments in cases:
            for limit in (20, 50):
                found = await pages('list_jobs', arguments, limit=limit)
                expected = await truth(
                    w,
                    states=('failed', 'queued', 'running'),
                    kind=arguments.get('kind'),
                    dataset=arguments.get('dataset'),
                )
                assert [entry['id'] for page in found for entry in page['jobs']] == expected['ids'], arguments
                assert {page['total'] for page in found} == {len(expected['ids'])}, arguments
                assert all(len(page['jobs']) <= limit for page in found)
        # Index advice: each section pages through all of its list, in the review's order.
        default = await admin('index_advice')
        for key in ('overlapping', 'possibly_unused'):
            assert default[key] and len(default[key]) <= 20, key
            given = [entry['redundant']['name'] if key == 'overlapping' else entry['name'] for entry in default[key]]
            whole = [entry['redundant']['name'] if key == 'overlapping' else entry['name'] for entry in stored[key]]
            assert given == whole[: len(given)], key  # the first entries
        assert {key: default['totals'][key] for key in lists} == {key: 250 for key in lists}, default['totals']
        for key in lists:
            found = await pages('index_advice', {'section': key})
            given = [
                entry['redundant']['name'] if key == 'overlapping' else entry['name']
                for page in found
                for entry in page[key]
            ]
            whole = [entry['redundant']['name'] if key == 'overlapping' else entry['name'] for entry in stored[key]]
            assert given == whole, key

    world(scenario)


# Tables an administration tool must leave as they were (3.7): settings, jobs, indexes, backups, datasets.
UNTOUCHED = (
    'datasets',
    'samples',
    'dataset_fields',
    'users',
    'jobs',
    'job_controls',
    'assistant_settings',
    'backup_schedule',
    'removed_indexes',
    'system_job_schedules',
    'dataset_job_schedules',
    'index_observations',
    'workers',
    'storage_configs',
)


async def snapshot(w: World, folder: str) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for table in UNTOUCHED:
        state[table] = await w.database.fetchval(
            f'SELECT md5(string_agg(t::text, $$|$$ ORDER BY t::text)) FROM {table} t',
        )
    state['indexes'] = await w.database.fetchval(
        "SELECT md5(string_agg(indexname || ' ' || indexdef, '|' ORDER BY indexname)) FROM pg_indexes "
        'WHERE schemaname = current_schema()',
    )
    state['settings'] = (await w.call(ADMIN, 'GET', SETTINGS)).json()
    state['backups'] = sorted((path.name, path.stat().st_size) for path in Path(folder).iterdir())
    return state


def test_nothing_an_administration_tool_does_writes_and_each_is_safe_to_call_twice(world, tmp_path, monkeypatch):
    async def scenario(w):
        planted = await plant_everything(w, tmp_path, monkeypatch)
        await review_job(w, None, result=system_review(), age=timedelta(0))  # newer than the real review
        assert (await w.call(ADMIN, 'GET', '/api/admin/index-advisor')).json()['review'] == system_review()
        await switch_on(w)
        before = await snapshot(w, planted['folder'])
        tools = list(assistant.admin_tools())
        arguments: list[dict[str, Any]] = [{}]
        for tool in tools:
            for key in plain(tool.input_schema).get('properties', {}):
                arguments += [{key: value} for value in HOSTILE]
        arguments += [
            {'dataset': PRIVATE_DS},
            {'dataset': CLOSED_DS},
            {'dataset': 999999},
            {'state': 'failed', 'kind': 'bulk_mutation', 'since_hours': 8760},
            {'section': 'removed'},
            {'section': 'needed', 'limit': 50, 'offset': 1},
            {'unknown': 'x'},
        ]
        for _ in range(2):
            for user_id in (ADMIN, POWER, 999999):
                for tool in tools:
                    for given in arguments:
                        await call(user_id, tool.name, given)
        assert await snapshot(w, planted['folder']) == before
        # Twice the same.
        for name, given in (
            ('list_jobs', {'since_hours': 8760}),
            ('index_advice', {}),
            ('index_advice', {'dataset': ORDINARY_DS}),
        ):
            first, second = await admin(name, given), await admin(name, given)
            assert {k: v for k, v in first.items() if k != 'since'} == {
                k: v for k, v in second.items() if k != 'since'
            }, name

    world(scenario)


def test_free_text_has_what_looks_like_a_secret_replaced_and_ordinary_words_and_numbers_kept(
    world,
    tmp_path,
    monkeypatch,
):
    async def scenario(w):
        folder = await plant_server(w, tmp_path, monkeypatch)
        failed = await job(w, 'selection_cleanup', 'failed', error=SCRUB_TEXT, age=minutes(30))
        running = await job(w, 'region_load', 'running', message=SCRUB_TEXT, age=minutes(25))
        in_folder = f'BackupError: could not write {folder}/freedomridge_20261101T020000Z.sql.gz'
        await job(w, 'backup', 'failed', error=SCRUB_TEXT, age=minutes(20), lane='maintenance')
        await job(w, 'backup', 'failed', error=in_folder, age=minutes(10), lane='maintenance')
        await review_job(w, None, state='failed', error=SCRUB_TEXT, age=minutes(5))
        # As written, where the administrators' routes read it.
        global_jobs = (await w.call(ADMIN, 'GET', '/api/admin/jobs', params={'limit': 100})).json()['items']
        by_id = {item['id']: item for item in global_jobs}
        assert by_id[failed]['error']['message'] == SCRUB_TEXT and by_id[running]['progress']['message'] == SCRUB_TEXT
        advisor = (await w.call(ADMIN, 'GET', '/api/admin/index-advisor')).json()
        assert advisor['last_failure']['message'] == SCRUB_TEXT, advisor['last_failure']
        await switch_on(w)
        listed = {entry['id']: entry for entry in (await admin('list_jobs', {'since_hours': 8760}))['jobs']}
        assert scrubbed(listed[failed]['error']), listed[failed]
        assert scrubbed(listed[running]['progress']['message']), listed[running]
        backup = await admin('backup_status')
        newest, older = backup['recent_runs'][0], backup['recent_runs'][1]
        assert cut_text(newest['error'], in_folder), newest  # a path inside the backup folder is kept
        assert scrubbed(older['error']), older
        advice = await admin('index_advice')
        assert scrubbed(advice['last_failure']['error']), advice['last_failure']
        text = json.dumps([listed, backup, advice])
        assert not [piece for piece in SCRUB_AWAY if piece in text], text
        assert not IPV4.search(text), IPV4.search(text)

    world(scenario)


SYNC_VALUES = {key: f'ZQXSYNC-{key}-VALUE' for key in ('closed', 'private', 'gone')}
SYNC_GIVEN = 'SYNCOK-PLAIN-MIRRORS'


async def mirror(w: World, dataset_id: int, link: int, origin: int) -> None:
    await w.database.execute(
        'INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, feed_cursor) VALUES ($1, $2, $3, 0)',
        dataset_id,
        link,
        origin,
    )


def test_a_follower_sync_job_is_about_every_mirror_of_its_link_and_kept_back_by_any_of_them(world):
    """A follower's sync job belongs to no dataset and names only its link; it writes that link's mirrors. Its words
    are kept back when a mirror is closed (a), when one has a private field (b), and when its link no longer exists
    (d: "no longer exists" or "not yet known" is accepted); with plain mirrors (c) they are given."""

    async def scenario(w):
        await three_datasets(w)
        links = {}
        for key in ('closed', 'private', 'plain'):
            links[key] = await w.database.fetchval(
                "INSERT INTO primary_links (url, token_encrypted) VALUES ($1, 'sealed') RETURNING id",
                f'https://primary-{key}.admin-spec.example/api',
            )
        links['gone'] = 987654  # a link no row has
        # (a) a closed mirror beside a plain one; (b) a mirror with a private field; (c) two plain mirrors.
        for dataset_id, key, origin in ((310, 'closed', 1), (311, 'closed', 2), (312, 'private', 1)):
            await w.dataset(dataset_id)
            await add(dataset_id, [{'filepath': f'm{dataset_id}.jpg', 'metadata': {'site': f'site-{dataset_id}'}}])
            await mirror(w, dataset_id, links[key], origin)
        for dataset_id, origin in ((313, 1), (314, 2)):
            await w.dataset(dataset_id)
            await mirror(w, dataset_id, links['plain'], origin)
        closed = await w.call(ADMIN, 'PATCH', '/api/datasets/310', json={'assistant_closed': True})
        assert closed.status_code == 200, closed.text
        marked = await w.call(
            ADMIN,
            'PATCH',
            '/api/datasets/312/fields/site/description',
            json={'values_private': True},
        )
        assert marked.status_code == 200, marked.text
        jobs = {}
        for key, link in links.items():
            text = SYNC_VALUES.get(key, SYNC_GIVEN)
            jobs[(key, 'failed')] = await job(
                w,
                FOLLOWER_SYNC,
                'failed',
                args={'link_id': link},
                error=f'could not write {text}',
                age=minutes(20),
            )
            jobs[(key, 'running')] = await job(
                w,
                FOLLOWER_SYNC,
                'running',
                args={'link_id': link},
                message=f'writing {text}',
                age=minutes(10),
            )
        # As written, where the administrators' routes read them.
        global_jobs = (await w.call(ADMIN, 'GET', '/api/admin/jobs', params={'limit': 100})).json()['items']
        assert all(value in json.dumps(global_jobs) for value in SYNC_VALUES.values())
        await switch_on(w)
        answers: list[Any] = [await admin('system_status'), await admin('backup_status'), await admin('index_advice')]
        for state in ('needs_attention', 'failed', 'running'):
            answers += await pages('list_jobs', {'state': state, 'since_hours': 8760})
            for dataset_id in (310, 311, 312, 313, 314):
                answers += await pages('list_jobs', {'state': state, 'dataset': dataset_id, 'since_hours': 8760})
        text = json.dumps(answers)
        assert not [value for value in SYNC_VALUES.values() if value in text], text
        listed = {entry['id']: entry for page in answers if 'jobs' in page for entry in page['jobs']}
        names_ = await dataset_names(w)
        for (key, state), job_id in jobs.items():
            entry = listed[job_id]
            await check_job_entry(w, entry, names_)
            given = entry['error'] if state == 'failed' else entry['progress']['message']
            if key == 'plain':
                assert given is not None and SYNC_GIVEN in given and entry['error_withheld'] is None, entry
            else:
                assert entry['error'] is None and entry['progress']['message'] is None, entry
                allowed = {'closed': {KEPT_CLOSED}, 'private': {KEPT_PRIVATE}, 'gone': {KEPT_GONE, KEPT_UNKNOWN}}[key]
                assert entry['error_withheld'] in allowed, entry

    world(scenario)


UNKNOWN_KIND = 'zqx_kind_not_yet_named'


def test_a_job_of_no_dataset_has_its_words_given_only_for_the_kinds_known_to_be_about_none(world):
    async def scenario(w):
        await three_datasets(w)
        unknown = {
            'made_up': await job(w, UNKNOWN_KIND, 'failed', error='UNKNOWNKIND-ERROR-1', age=minutes(30)),
            'made_up_running': await job(w, UNKNOWN_KIND, 'running', message='UNKNOWNKIND-PROGRESS-2', age=minutes(29)),
            # A real kind main always queues with a dataset, written here without one.
            'real_unlisted': await job(w, 'map_place', 'failed', error='UNKNOWNKIND-ERROR-3', age=minutes(28)),
            # Restoring an index reads every row it covers: not on the list, whatever its kind of index.
            'index_restore': await job(
                w,
                index_actions.RESTORE_KIND,
                'failed',
                args={'kind': 'other', 'index_name': 'idx_back'},
                error='UNKNOWNKIND-restore: invalid input syntax for type double precision',
                age=minutes(27),
            ),
            'index_restore_running': await job(
                w,
                index_actions.RESTORE_KIND,
                'running',
                args={'kind': 'core', 'index_name': 'idx_back_too'},
                message='UNKNOWNKIND-restoring idx_back_too',
                age=minutes(26),
            ),
        }
        known = {
            'backup': await job(w, 'backup', 'failed', error='KNOWNKIND-backup', age=minutes(20)),
            'region_load': await job(w, 'region_load', 'failed', error='KNOWNKIND-region', age=minutes(19)),
            'selection_cleanup': await job(
                w,
                'selection_cleanup',
                'failed',
                error='KNOWNKIND-cleanup',
                age=minutes(18),
            ),
            'index_review': await review_job(w, None, state='failed', error='KNOWNKIND-review', age=minutes(17)),
            'index_drop': await job(
                w,
                index_actions.DROP_KIND,
                'failed',
                args={'kind': 'other', 'index_name': 'idx_gone'},
                error='KNOWNKIND-drop',
                age=minutes(16),
            ),
        }
        global_jobs = (await w.call(ADMIN, 'GET', '/api/admin/jobs', params={'limit': 100})).json()['items']
        assert {*unknown.values(), *known.values()} <= {item['id'] for item in global_jobs}
        await switch_on(w)
        answers = [*await pages('list_jobs', {'state': 'failed', 'since_hours': 8760})]
        answers += await pages('list_jobs', {'state': 'running', 'since_hours': 8760})
        listed = {entry['id']: entry for page in answers for entry in page['jobs']}
        names_ = await dataset_names(w)
        for job_id in unknown.values():
            entry = listed[job_id]
            await check_job_entry(w, entry, names_)
            assert entry['error'] is None and entry['progress']['message'] is None, entry
            assert entry['error_withheld'] == KEPT_UNKNOWN, entry
        assert 'UNKNOWNKIND' not in json.dumps(answers)
        for key, job_id in known.items():
            entry = listed[job_id]
            await check_job_entry(w, entry, names_)
            assert entry['error_withheld'] is None and 'KNOWNKIND' in (entry['error'] or ''), (key, entry)

    world(scenario)


def test_a_follower_sync_whose_link_has_no_mirror_left_is_read_as_about_a_dataset_that_is_gone(world):
    """A real link with no mirror on this server, and one whose only mirror was deleted through the app after its
    jobs ran (the mirror's row goes with its dataset): both read as gone. A link with plain mirrors is given (the
    first follower test's case c)."""

    async def scenario(w):
        await three_datasets(w)
        links = {}
        for key in ('empty', 'emptied'):
            links[key] = await w.database.fetchval(
                "INSERT INTO primary_links (url, token_encrypted) VALUES ($1, 'sealed') RETURNING id",
                f'https://primary-{key}.admin-spec.example/api',
            )
        await w.dataset(315)
        await add(315, [{'filepath': 'm315.jpg', 'metadata': {'site': 'site-315'}}])
        # Stopped (as after its sharing was stopped), so the app lets it be deleted; still its link's mirror.
        await w.database.execute(
            "INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, feed_cursor, state) VALUES (315, $1, 1, 0, "
            "'stopped')",
            links['emptied'],
        )
        jobs = {}
        for key, link in links.items():
            value = f'ZQXSYNC-{key}-VALUE'
            jobs[(key, 'failed')] = await job(
                w,
                FOLLOWER_SYNC,
                'failed',
                args={'link_id': link},
                error=f'could not write {value}',
                age=minutes(20),
            )
            jobs[(key, 'running')] = await job(
                w,
                FOLLOWER_SYNC,
                'running',
                args={'link_id': link},
                message=f'writing {value}',
                age=minutes(10),
            )
        deleting = await w.call(ADMIN, 'DELETE', '/api/datasets/315')
        assert deleting.is_success, deleting.text
        await finish_deletion(w.database)
        assert await w.database.fetchval('SELECT count(*) FROM mirrors WHERE link_id = $1', links['emptied']) == 0
        global_jobs = (await w.call(ADMIN, 'GET', '/api/admin/jobs', params={'limit': 100})).json()['items']
        assert 'ZQXSYNC-emptied-VALUE' in json.dumps(global_jobs) and 'ZQXSYNC-empty-VALUE' in json.dumps(global_jobs)
        await switch_on(w)
        answers = await every_answer()
        assert 'ZQXSYNC' not in json.dumps(answers)
        listed = {entry['id']: entry for result in results(answers['list_jobs']) for entry in result['jobs']}
        names_ = await dataset_names(w)
        for which, job_id in jobs.items():
            entry = listed[job_id]
            await check_job_entry(w, entry, names_)
            assert entry['error'] is None and entry['progress']['message'] is None, (which, entry)
            assert entry['error_withheld'] == KEPT_GONE, (which, entry)

    world(scenario)


GAIN_VALUES = ['ZQXGAIN-01-NA', 'ZQXGAIN-02-NA', 'ZQXGAIN-03-NA']


async def settled(w: World, job_id: str, seconds: float = 120) -> str:
    """The job's state once it has ended (a runner is working meanwhile)."""
    for _ in range(int(seconds * 10)):
        state = await w.database.fetchval('SELECT state FROM jobs WHERE id = $1::uuid', job_id)
        if state in ('succeeded', 'failed', 'cancelled'):
            return state
        await asyncio.sleep(0.1)
    raise AssertionError(f'job {job_id} did not end in {seconds} s')


def test_restoring_a_hand_made_index_is_kept_back_though_it_belongs_to_no_dataset(world):
    """A hand-made expression index removed through the administrators' route, then restored over a value it can't
    read (in a closed dataset, then in a private field): each restore fails; its words are kept back as a kind not
    known to be about no dataset. Main's build reports such a failure in words of its own, without the value, so a
    failed restore quoting one is planted beside them."""

    async def scenario(w):
        await three_datasets(w)
        await add(ORDINARY_DS, [{'filepath': f'gain-{n}.jpg', 'metadata': {'gain': 1.5 + n}} for n in range(3)])
        await w.database.execute("CREATE INDEX idx_hand_gain ON samples (((metadata->>'gain')::float))")
        definition = await w.database.fetchval("SELECT pg_get_indexdef('idx_hand_gain'::regclass)")
        removing = await w.call(
            ADMIN,
            'POST',
            '/api/admin/indexes/idx_hand_gain/remove',
            params={'wait': 'false'},
            json={'definition': definition, 'reason': 'unused'},
        )
        assert removing.status_code in (200, 202), removing.text
        dropped = await job_helpers.finish(w.database, index_actions.DROP_KIND)
        assert dropped['state'] == 'succeeded', dropped
        stored = await w.database.fetchval(
            'SELECT definition FROM removed_indexes WHERE index_name = $1 AND restored_at IS NULL',
            'idx_hand_gain',
        )
        assert stored == definition, stored
        restores = []
        for dataset_id, value in ((CLOSED_DS, GAIN_VALUES[0]), (PRIVATE_DS, GAIN_VALUES[1])):
            (bad,) = await add(dataset_id, [{'filepath': f'bad-gain-{dataset_id}.jpg', 'metadata': {'gain': value}}])
            if dataset_id == PRIVATE_DS:
                marked = await w.call(
                    ADMIN,
                    'PATCH',
                    f'/api/datasets/{PRIVATE_DS}/fields/gain/description',
                    json={'values_private': True},
                )
                assert marked.status_code == 200, marked.text
            asked = await w.call(ADMIN, 'POST', '/api/admin/indexes/idx_hand_gain/restore', params={'wait': 'false'})
            assert asked.status_code == 202, asked.text
            job_id = asked.json()['job_id']
            async with job_helpers.runner(w.database, kinds=[index_actions.RESTORE_KIND]):
                assert await settled(w, job_id) == 'failed'
            restores.append(job_id)
            await w.database.execute('DELETE FROM samples WHERE id = $1', bad)
        quoted = await job(
            w,
            index_actions.RESTORE_KIND,
            'failed',
            args={'kind': 'other', 'index_name': 'idx_hand_gain'},
            error=f'invalid input syntax for type double precision: "{GAIN_VALUES[2]}"',
        )
        global_jobs = (await w.call(ADMIN, 'GET', '/api/admin/jobs', params={'limit': 100})).json()['items']
        by_id = {item['id']: item for item in global_jobs}
        assert all(by_id[job_id]['dataset_id'] is None and by_id[job_id]['error'] for job_id in restores), by_id
        assert GAIN_VALUES[2] in json.dumps(by_id[quoted]['error'])
        await switch_on(w)
        answers = await every_answer()
        assert 'ZQXGAIN' not in json.dumps(answers)
        listed = {entry['id']: entry for result in results(answers['list_jobs']) for entry in result['jobs']}
        names_ = await dataset_names(w)
        for job_id in (*restores, quoted):
            entry = listed[job_id]
            await check_job_entry(w, entry, names_)
            assert entry['error'] is None and entry['progress']['message'] is None, entry
            assert entry['error_withheld'] == KEPT_UNKNOWN, entry

    world(scenario)


def test_the_tests_own_reading_of_the_definition_rule_is_not_fooled_by_words_between_a_cast_and_a_parenthesis():
    """withheld_by_rule, the reading every index check here stands on: a number after `::type word f(` is a value, not
    a type's modifier; a modifier right after its type name is the server's own."""

    def index(body: str) -> str:
        return f'CREATE INDEX idx_x ON public.samples USING btree {body}'

    kept_back = [
        "(dataset_id) WHERE (((metadata ->> 'a'::text))::boolean AND f(4417))",
        "(dataset_id) WHERE (((metadata ->> 'a'::text))::boolean OR spec_flag(4417))",
        "(dataset_id) WHERE (((metadata ->> 'a'::text))::integer = 4417)",
        "((((metadata ->> 'a'::text))::numeric(10,2))) WHERE (((metadata ->> 'a'::text))::numeric(10,2) > 4417.5)",
        "(dataset_id) WHERE (((metadata ->> 'a'::text))::integer = ANY (ARRAY[12, 31, 407]))",
        "(dataset_id) WHERE ((dataset_id = 7) AND (((metadata ->> 'a'::text))::bigint = 9))",
    ]
    whole = [
        "((((metadata ->> 'a'::text))::numeric(10,2)))",
        "((((metadata ->> 'a'::text))::character varying(255)))",
        "((((metadata ->> 'a'::text))::bit varying(8)))",
        "((((metadata ->> 'a'::text))::pg_catalog.numeric(10,2)))",
        '(((last_login)::timestamp(3) without time zone))',
        '(((stamp)::time(3) with time zone))',
        "((((metadata ->> 'a'::text))::double precision)) WHERE (dataset_id = 7)",
        "(dataset_id, created_at) WITH (fillfactor='70')",
    ]
    for body in kept_back:
        assert withheld_by_rule('idx_x', 'samples', index(body)), body
    for body in whole:
        assert not withheld_by_rule('idx_x', 'samples', index(body)), body


# --- 3.1 The fence around the thirteen tools ---

# The administrators-only read routes the survey names (its sections 1-4, 6 and 9).
INSIDE_ADMIN = {
    '/api/admin/status': '/api/admin/status',
    '/api/admin/index-advisor': '/api/admin/index-advisor',
    '/api/admin/jobs': '/api/admin/jobs',
    '/api/admin/backups/schedule': '/api/admin/backups/schedule',
    '/api/admin/regions': '/api/admin/regions',
    '/api/admin/assistant': '/api/admin/assistant',
    '/api/admin/assistant/calls': '/api/admin/assistant/calls',
}
OUTSIDE_ADMIN = {
    '/api/datasets/{id}/index-advisor': f'/api/datasets/{ORDINARY_DS}/index-advisor',
    '/api/users': '/api/users',
    '/api/groups': '/api/groups',
    '/api/followers': '/api/followers',
    '/api/primaries': '/api/primaries',
    '/api/storage/configs': '/api/storage/configs',
    '/api/storage/configs/{config_id}': '/api/storage/configs/{config}',
    '/api/compute-launcher': '/api/compute-launcher',
    '/api/workers': '/api/workers',
}


def test_none_of_the_thirteen_tools_lists_an_administrators_only_route():
    fenced = {('GET', template) for template in (*INSIDE_ADMIN, *OUTSIDE_ADMIN)}
    for tool in assistant.tools():
        for method, template in tool.routes:
            path = template if template.startswith('/api') else '/api' + template
            assert (method, path) not in fenced and not path.startswith('/api/admin'), (tool.name, method, template)


async def tool_request(user_id: int, template: str, path: str) -> tuple[int, Any]:
    """A request made for one of the thirteen tools (an Identity that lists the route, as a tool lists its routes)."""
    handler = inner.route_handler('GET', template)
    return await _raw(Identity(user_id, 'mcp', frozenset({('GET', handler)})), 'GET', path)


async def fence_holds(w: World, routes: dict[str, str]) -> None:
    await settings(w, {'mcp_enabled': True, 'roles': {'admin': True, 'power_user': True}})
    config = await w.database.fetchval(
        "INSERT INTO storage_configs (name, bucket_name) VALUES ('fence-store', 'fence-bucket') RETURNING id",
    )
    await w.dataset(ORDINARY_DS)
    for template, path in routes.items():
        concrete = path.format(config=config)
        for user_id in (ADMIN, POWER):
            status, body = await tool_request(user_id, template, concrete)
            assert (status, body.get('code')) == (403, ROUTE_REFUSED_CODE), (user_id, template, status, body)
        status, body = await tool_request(GUEST, template, concrete)  # never let in: the door refuses first
        assert status == 403 and body.get('code') in (NOT_ALLOWED_CODE, ROUTE_REFUSED_CODE), (template, status, body)


def test_the_administrators_routes_refuse_a_request_made_for_one_of_the_thirteen_tools_whoever_the_user_is(world):
    async def scenario(w):
        await fence_holds(w, INSIDE_ADMIN)

    world(scenario)


def test_the_administrators_only_routes_outside_api_admin_refuse_it_too(world):
    async def scenario(w):
        await fence_holds(w, {**OUTSIDE_ADMIN, ALL_JOBS: ALL_JOBS})

    world(scenario)


# --- 4. The tools ---


def test_system_status_is_what_the_status_page_reads(world, tmp_path, monkeypatch):
    async def scenario(w):
        await plant_server(w, tmp_path, monkeypatch)
        route = (await w.call(ADMIN, 'GET', '/api/admin/status')).json()
        assert (route['server']['restart_needed'], route['backups']['level']) == (True, 'failed'), route
        assert (route['workers']['server']['online'], route['workers']['compute']['online']) == (1, 0), route
        await switch_on(w)
        answer = await admin('system_status')
        assert set(answer) == NAMED['system_status']['']
        assert near(answer['checked_at'], datetime.now(timezone.utc)), answer['checked_at']
        assert answer['server'] == {'version': route['server']['version'], 'restart_needed': True}
        database_ = answer['database']
        assert database_['reachable'] is True and route['database']['reachable'] is True
        for key in ('pending_migrations', 'unknown_migrations', 'stale_functions'):
            assert database_[key] == route['database'][key], key
        assert close(database_['size_bytes'], route['database']['size_bytes'], 64 * 1024**2), database_
        disk, theirs = answer['database_disk'], route['database_disk']
        assert set(disk) == NAMED['system_status']['database_disk']
        assert {k: disk[k] for k in ('total_bytes', 'level', 'low_below_bytes', 'ingest_paused_below_bytes')} == {
            k: theirs[k] for k in ('total_bytes', 'level', 'low_below_bytes', 'ingest_paused_below_bytes')
        }
        assert close(disk['free_bytes'], theirs['free_bytes'], 2 * 1024**3), (disk, theirs)
        assert len(answer['cache_disks']) == len(route['cache_disks']) == 1
        for given, read in zip(answer['cache_disks'], route['cache_disks']):
            assert given['folders'] == [folder['label'] for folder in read['folders']], given
            assert (given['total_bytes'], given['level']) == (read['total_bytes'], read['level'])
            assert close(given['free_bytes'], read['free_bytes'], 2 * 1024**3)
        for placement in ('server', 'compute'):
            given, read = answer['workers'][placement], route['workers'][placement]
            assert given['online'] == read['online'] and moment(given['last_seen']) == moment(read['last_seen'])
        assert (answer['workers']['server']['online'], answer['workers']['compute']['online']) == (1, 0)
        backups_ = route['backups']
        assert answer['backups']['level'] == backups_['level'] == 'failed'
        assert moment(answer['backups']['latest_at']) == moment(backups_['latest']['created_at'])
        assert moment(answer['backups']['next_run_at']) == moment(backups_['schedule']['next_run_at'])
        assert answer['link'] == address(STATUS_PAGE)

    world(scenario)


def backup_run(row: Any) -> dict[str, Any]:
    """A backup job as backup_status lists it under recent_runs."""
    result = row['result'] if isinstance(row['result'], dict) else {}
    return {
        'job': row['id'],
        'state': row['state'],
        'started_at': moment(row['started_at']),
        'finished_at': moment(row['finished_at']),
        'file': result.get('file'),
        'size_bytes': result.get('size_bytes'),
    }


def test_backup_status_is_what_the_backups_card_reads_with_the_last_ten_runs(world, tmp_path, monkeypatch):
    async def scenario(w):
        folder = await plant_server(w, tmp_path, monkeypatch)
        texts = {}
        for n in range(12):  # oldest first; the newest is queued, so a backup is running or waiting
            age = timedelta(hours=12 - n)
            if n == 11:
                await job(w, 'backup', 'queued', age=age, lane='maintenance')
            elif n % 3 == 0:
                texts[n] = long_text(f'BackupError: pg_dump exited 1 (run {n}) ', ' BACKUPERR-TAIL')
                await job(
                    w,
                    'backup',
                    'failed',
                    error=texts[n],
                    age=age,
                    lane='maintenance',
                    failures=2,
                    max_failures=2,
                )
            elif n % 3 == 1:
                result = {'file': f'freedomridge_run{n:02d}.sql.gz', 'size_bytes': 1000 + n, 'tables': 30}
                await job(w, 'backup', 'succeeded', result=result, age=age, lane='maintenance')
            else:
                await job(w, 'backup', 'cancelled', age=age, lane='maintenance')
        route = (await w.call(ADMIN, 'GET', '/api/admin/status')).json()['backups']
        schedule = (await w.call(ADMIN, 'GET', '/api/admin/backups/schedule')).json()
        assert (route['level'], route['latest']['size_bytes'], route['active_job']['state']) == (
            'failed',
            3400,
            'queued',
        )
        listed = (await w.call(ADMIN, 'GET', '/api/admin/jobs', params={'limit': 100})).json()['items']
        assert [item['kind'] for item in listed].count('backup') == 12
        await switch_on(w)
        answer = await admin('backup_status')
        assert (answer['level'], answer['folder']) == (route['level'], route['folder']) == ('failed', folder)
        latest = {
            k: v
            for k, v in route['latest'].items()
            if k not in ('schema_version', 'migrations_applied', 'server_version')
        }
        assert {**answer['latest'], 'created_at': moment(answer['latest']['created_at'])} == {
            **latest,
            'created_at': moment(latest['created_at']),
        }
        assert answer['latest']['size_bytes'] == 3400 and answer['latest']['tables'] == 30
        assert answer['last_failure']['error'] == route['last_failure']['error'] == BACKUP_FAILURE
        assert moment(answer['last_failure']['failed_at']) == moment(route['last_failure']['failed_at'])
        assert answer['kept']['count'] == route['kept']['count'] == 2
        assert answer['kept']['total_bytes'] == route['kept']['total_bytes'] == 4600
        assert moment(answer['kept']['oldest_created_at']) == moment(route['kept']['oldest_created_at'])
        assert answer['unverified_dumps'] == route['unverified_dumps'] == 1
        assert answer['disk']['total_bytes'] == route['disk']['total_bytes']
        assert close(answer['disk']['free_bytes'], route['disk']['free_bytes'], 2 * 1024**3)
        assert (
            {k: answer['schedule'][k] for k in ('interval_hours', 'start_hour', 'keep')}
            == {k: schedule[k] for k in ('interval_hours', 'start_hour', 'keep')}
            == {'interval_hours': 24, 'start_hour': 2, 'keep': 7}
        )
        assert moment(answer['schedule']['next_run_at']) == moment(route['schedule']['next_run_at'])
        active = route['active_job']
        assert active is not None and answer['running'] is not None, answer['running']
        assert set(answer['running']) == {'job', 'state', 'started_at'}, answer['running']
        assert (answer['running']['job'], answer['running']['state']) == (str(active['id']), active['state'])
        assert moment(answer['running']['started_at']) == moment(active['started_at'])
        rows = await w.database.fetch(
            "SELECT id::text AS id, state, started_at, finished_at, result, error FROM jobs WHERE kind = 'backup' "
            'ORDER BY created_at DESC LIMIT 10',
        )
        assert len(answer['recent_runs']) == 10
        for given, row in zip(answer['recent_runs'], rows):
            expected = backup_run(row)
            got = {key: given[key] for key in expected}
            got.update(started_at=moment(given['started_at']), finished_at=moment(given['finished_at']))
            assert got == expected, (given, expected)
            if row['state'] == 'failed':
                assert cut_text(given['error'], row['error']['message']), given
            else:
                assert given['error'] is None, given
        assert answer['link'] == address(STATUS_PAGE)

    world(scenario)


def test_backup_status_without_a_backup_folder_says_not_configured_and_of_an_unreadable_one_why(
    world,
    tmp_path,
    monkeypatch,
):
    async def scenario(w):
        # Backup jobs exist (run while a folder was set): with none set, recent_runs is still [].
        for n, state in enumerate(('succeeded', 'failed', 'queued')):
            await job(w, 'backup', state, error='BackupError: old' if state == 'failed' else None, age=minutes(30 - n))
        missing = str(tmp_path / 'no-such-folder')
        monkeypatch.setattr(backups, 'BACKUP_DIR', missing)
        route = (await w.call(ADMIN, 'GET', '/api/admin/status')).json()['backups']
        assert route['level'] == 'unreadable' and route['folder'] == missing and route['error'], route
        monkeypatch.setattr(backups, 'BACKUP_DIR', None)
        assert (await w.call(ADMIN, 'GET', '/api/admin/status')).json()['backups'] == {
            'level': 'not_configured',
            'folder': None,
        }
        await switch_on(w)
        answer = await admin('backup_status')
        assert (answer['level'], answer['note'], answer['link']) == (
            'not_configured',
            NOT_CONFIGURED,
            address(STATUS_PAGE),
        )
        for key, value in answer.items():
            if key not in ('level', 'note', 'link'):
                assert value in (None, [], {}), (key, value)
        assert answer['recent_runs'] == [] and answer['running'] is None, answer
        monkeypatch.setattr(backups, 'BACKUP_DIR', missing)
        answer = await admin('backup_status')
        assert (answer['level'], answer['folder'], answer['error']) == ('unreadable', missing, route['error'])
        assert answer['link'] == address(STATUS_PAGE)

    world(scenario)


def test_list_jobs_filters_pages_and_counts_as_the_jobs_table_holds_and_as_the_route_lists(world):
    async def scenario(w):
        await three_datasets(w)
        await plant_jobs(w)
        await job(w, 'selection_cleanup', 'failed', error='a day old', age=timedelta(hours=30))
        await job(w, 'backup', 'succeeded', result={'file': 'f.sql.gz'}, age=timedelta(hours=100))
        await job(w, 'selection_cleanup', 'failed', error='past the default window', age=timedelta(hours=200))
        await job(w, 'selection_cleanup', 'failed', error='past the longest window', age=timedelta(hours=9000))
        # Still queued and still running, created 30 days ago: in every window.
        old_queued = await job(w, 'region_load', 'queued', age=timedelta(days=30))
        old_running = await job(w, 'backup', 'running', message='dumping', age=timedelta(days=30))
        old_failed = await job(w, 'selection_cleanup', 'failed', error='ended 30 days ago', age=timedelta(days=30))
        global_jobs = (await w.call(ADMIN, 'GET', '/api/admin/jobs', params={'limit': 100})).json()['items']
        failures = {item['error']['message'] for item in global_jobs if item['error']}
        assert {'a day old', 'past the default window', 'past the longest window'} <= failures, failures
        assert {old_queued, old_running, old_failed} <= {item['id'] for item in global_jobs}
        await switch_on(w)
        default = await pages('list_jobs', {})
        given = {entry['id'] for page in default for entry in page['jobs']}
        assert {old_queued, old_running} <= given and old_failed not in given, given
        recent = await admin('list_jobs', {'since_hours': 1, 'state': 'queued'})
        assert old_queued in {entry['id'] for entry in recent['jobs']}, recent
        counted = (await truth(w, hours=1))['counts']
        assert recent['counts'] == counted and counted['queued'] >= 1 and counted['running'] >= 1, counted
        since = (await w.database.fetchval('SELECT now()') - timedelta(hours=1)).isoformat()
        routed = await w.call(ADMIN, 'GET', ALL_JOBS, params={'since': since, 'limit': 100})
        assert routed.status_code == 200, routed.text
        routed_ids = {item['id'] for item in routed.json()['jobs']}
        assert {old_queued, old_running} <= routed_ids and old_failed not in routed_ids, routed_ids
        assert routed.json()['counts'] == counted, routed.json()['counts']
        names_ = await dataset_names(w)
        cases: list[dict[str, Any]] = [
            {},
            {'state': 'needs_attention'},
            *({'state': state} for state in STATES),
            {'kind': 'bulk_mutation'},
            {'kind': index_advisor.KIND},
            {'dataset': ORDINARY_DS},
            {'dataset': PRIVATE_DS},
            {'dataset': CLOSED_DS},
            {'since_hours': 1},
            {'since_hours': 24},
            {'since_hours': 8760},
            {'state': 'succeeded', 'dataset': ORDINARY_DS, 'since_hours': 8760},
            {'state': 'failed', 'kind': 'selection_cleanup', 'since_hours': 8760},
        ]
        for arguments in cases:
            state = arguments.get('state', 'needs_attention')
            states = ('failed', 'queued', 'running') if state == 'needs_attention' else (state,)
            hours = arguments.get('since_hours', 168)
            expected = await truth(
                w,
                states=states,
                kind=arguments.get('kind'),
                dataset=arguments.get('dataset'),
                hours=hours,
            )
            found = await pages('list_jobs', arguments, limit=3)
            assert [entry['id'] for page in found for entry in page['jobs']] == expected['ids'], arguments
            for page in found:
                assert page['total'] == len(expected['ids']), arguments
                assert page['counts'] == expected['counts'], (arguments, page['counts'])
                assert near(page['since'], datetime.now(timezone.utc) - timedelta(hours=hours)), page['since']
                link = STATUS_PAGE if 'dataset' not in arguments else f'/datasets/{arguments["dataset"]}'
                assert page['link'] == address(link), page['link']
                if page['next_offset'] is not None:
                    assert page['next_offset'] == page['offset'] + len(page['jobs']), page
                assert set(page['kinds']) == expected['kinds'], (arguments, page['kinds'])  # whatever the filters
                for entry in page['jobs']:
                    await check_job_entry(w, entry, names_)
            # The route lists the same jobs in the same order.
            route_states = list(states)
            since = (await w.database.fetchval('SELECT now()') - timedelta(hours=hours)).isoformat()
            params: dict[str, Any] = {'state': route_states, 'since': since, 'limit': 100}
            if 'kind' in arguments:
                params['kind'] = arguments['kind']
            if 'dataset' in arguments:
                params['dataset_id'] = arguments['dataset']
            listed = await w.call(ADMIN, 'GET', ALL_JOBS, params=params)
            assert listed.status_code == 200, listed.text
            assert [item['id'] for item in listed.json()['jobs']] == expected['ids'], arguments
        # The default page is 20; a page may be asked for at its offset.
        everything = await truth(w)
        assert len(everything['ids']) > 3
        first = await admin('list_jobs')
        assert [entry['id'] for entry in first['jobs']] == everything['ids'][:20]
        assert first['next_offset'] == (20 if len(everything['ids']) > 20 else None)
        second = await admin('list_jobs', {'limit': 2, 'offset': 2})
        assert [entry['id'] for entry in second['jobs']] == everything['ids'][2:4] and second['offset'] == 2

    world(scenario)


def test_list_jobs_unknown_dataset_is_not_found_and_unknown_kind_is_an_empty_list(world):
    async def scenario(w):
        await three_datasets(w)
        await plant_jobs(w)
        await switch_on(w)
        for unknown in (999999, GONE_DS):  # no such dataset; one whose jobs outlive it
            missing = await call(ADMIN, 'list_jobs', {'dataset': unknown})
            assert not missing['ok'] and missing['error']['code'] == 'not_found', missing
            assert str(unknown) in missing['error']['message'], missing
        nothing = await admin('list_jobs', {'kind': 'no_such_kind'})
        assert (nothing['jobs'], nothing['total'], nothing['next_offset']) == ([], 0, None), nothing

    world(scenario)


def test_arguments_out_of_range_are_invalid(world):
    async def scenario(w):
        await three_datasets(w)
        await switch_on(w)
        for name, arguments in (
            ('list_jobs', {'state': 'stuck'}),
            ('list_jobs', {'state': 3}),
            ('list_jobs', {'kind': 7}),
            ('list_jobs', {'dataset': 'seven'}),
            ('list_jobs', {'since_hours': 0}),
            ('list_jobs', {'since_hours': 8761}),
            ('list_jobs', {'limit': 0}),
            ('list_jobs', {'limit': 51}),
            ('list_jobs', {'offset': -1}),
            ('index_advice', {'section': 'everything'}),
            ('index_advice', {'dataset': 'seven'}),
            ('index_advice', {'limit': 0}),
            ('index_advice', {'limit': 51}),
            ('index_advice', {'offset': -1}),
        ):
            answer = await call(ADMIN, name, arguments)
            assert not answer['ok'] and answer['error']['code'] == 'invalid', (name, arguments, answer)
        for limit in (1, 50):
            assert (await call(ADMIN, 'list_jobs', {'limit': limit}))['ok']
            assert (await call(ADMIN, 'index_advice', {'limit': limit}))['ok']
        for hours in (1, 8760):
            assert (await call(ADMIN, 'list_jobs', {'since_hours': hours}))['ok']

    world(scenario)


def advised(entry: dict[str, Any], names_: dict[int, str]) -> dict[str, Any]:
    """A review's index entry as index_advice gives it."""
    keys = ('name', 'table', 'kind', 'method', 'field', 'size_bytes', 'scans', 'last_scan', 'valid', 'needed')
    out = {key: entry[key] for key in keys}
    owner = entry['dataset_id']
    out['dataset'] = None if owner is None else {'id': owner, 'name': names_[owner]}
    withheld = entry['name'] in WITHHELD
    out['definition'] = None if withheld else entry['definition']
    out['definition_withheld'] = withheld
    if withheld and entry['needed']:
        out['needed'] = Masked(list(entry['needed']))
    if 'idle_days' in entry:
        out.update(idle_days=entry['idle_days'], invalid=entry['invalid'])
    return out


def advised_list(key: str, entries: list[dict[str, Any]], names_: dict[int, str]) -> list[dict[str, Any]]:
    if key == 'overlapping':
        return [
            {
                'redundant': advised(pair['redundant'], names_),
                'kept': advised(pair['kept'], names_),
                'rule': pair['rule'],
                'reason': Masked(pair['reason'])
                if {pair['redundant']['name'], pair['kept']['name']} & WITHHELD
                else pair['reason'],
            }
            for pair in entries
        ]
    keys: tuple[str, ...]
    if key == 'removed':
        keys = ('index_name', 'table', 'field', 'size_bytes', 'reason', 'removed_by', 'removed_at', 'state')
        return [{k: entry[k] for k in keys} for entry in entries]
    if key == 'suggested':
        keys = ('field', 'type', 'reason', 'excess_seconds', 'build_seconds', 'pays_back', 'usages')
        return [{k: entry[k] for k in keys} for entry in entries]
    return [advised(entry, names_) for entry in entries]


def same_entries(given: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    def normal(entry: dict[str, Any]) -> dict[str, Any]:
        return {k: moment(v) if k == 'removed_at' else v for k, v in entry.items()}

    return [normal(entry) for entry in given] == [normal(entry) for entry in expected]


async def check_sections(answer_of: Any, route_lists: dict[str, list[dict[str, Any]]], names_: dict[int, str]) -> None:
    """Each section alone, two at a time, against the route's list; the other lists not given."""
    for key, entries in route_lists.items():
        expected = advised_list(key, entries, names_)
        offset, collected = 0, []
        while True:
            page = await answer_of({'section': key, 'limit': 2, 'offset': offset})
            assert page['offset'] == offset
            assert len(page[key]) <= 2 and same_entries(page[key], expected[offset : offset + len(page[key])]), key
            for other in SECTIONS:
                if other != key:
                    assert not page.get(other), (key, other)
            collected += page[key]
            if page['next_offset'] is None:
                break
            assert page['next_offset'] == offset + len(page[key]), page['next_offset']
            offset = page['next_offset']
        assert same_entries(collected, expected), key


def test_index_advice_for_the_system_is_what_the_status_page_reads_from_its_stored_review(world):
    async def scenario(w):
        await three_datasets(w)
        await review_job(w, None, result=system_review(), age=timedelta(hours=3))
        failure = long_text('RuntimeError: the review could not read the catalog ', ' REVIEWERR-TAIL')
        await review_job(w, None, state='failed', error=failure, age=timedelta(hours=2))
        await review_job(w, None, state='queued', age=minutes(1))
        await removal(w, 'idx_gone_by_hand')
        await job(w, index_actions.DROP_KIND, 'queued', args={'kind': 'other', 'index_name': 'idx_pending_drop'})
        saved = await w.call(
            ADMIN,
            'PUT',
            '/api/admin/index-advisor/schedule',
            json={'enabled': True, 'frequency': 'weekly', 'weekday': 0, 'time': '03:00', 'timezone': 'UTC'},
        )
        assert saved.status_code == 200, saved.text
        route = (await w.call(ADMIN, 'GET', '/api/admin/index-advisor')).json()
        stored = route['review']
        assert stored == system_review() and route['active_job'] and route['last_failure'], route  # as planted
        assert route['removed'] and route['pending_actions'] == ['idx_pending_drop'], route
        # The withheld side of this review is what 3.4's rule says it is.
        judged = {
            e['name'] for e in every_index_entry(stored) if withheld_by_rule(e['name'], e['table'], e['definition'])
        }
        assert judged == WITHHELD, sorted(judged ^ WITHHELD)
        await switch_on(w)
        names_ = await dataset_names(w)
        answer = await admin('index_advice')
        assert (answer['scope'], answer['dataset']) == ('system', None)
        assert moment(answer['reviewed_at']) == moment(stored['reviewed_at'])
        assert (answer['enough_history'], answer['window_days'], answer['idle_days_required']) == (
            True,
            route['window_days'],
            14,
        )
        assert answer['review_running'] is True
        assert set(answer['last_failure']) == {'finished_at', 'error', 'error_withheld'}
        assert moment(answer['last_failure']['finished_at']) == moment(route['last_failure']['finished_at'])
        assert cut_text(answer['last_failure']['error'], failure), answer['last_failure']
        assert answer['last_failure']['error_withheld'] is None, answer['last_failure']
        totals = answer['totals']
        expected_totals = {
            'overlapping': 5,
            'possibly_unused': 3,
            'needed': 4,
            'too_new': 15,
            'removed': 1,
            'redundant_bytes': SYSTEM_REDUNDANT_BYTES,
        }
        assert {key: totals.get(key) for key in expected_totals} == expected_totals, totals
        assert totals.get('suggested', 0) == 0, totals
        assert same_entries(answer['overlapping'], advised_list('overlapping', stored['overlapping'], names_))
        assert same_entries(
            answer['possibly_unused'],
            advised_list('possibly_unused', stored['possibly_unused'], names_),
        )
        for other in ('needed', 'too_new', 'removed', 'suggested'):
            assert not answer.get(other), other  # without a section, only the first two lists
        assert answer['schedule'] == route['schedule'] and answer['schedule']['enabled'] is True
        assert answer['pending_actions'] == route['pending_actions']
        assert answer['link'] == address(STATUS_PAGE)
        # Each list alone, paged; the removed list is the route's.
        lists = {key: stored[key] for key in ('overlapping', 'possibly_unused', 'needed', 'too_new')}

        async def answer_of(arguments: dict[str, Any]) -> dict[str, Any]:
            return await admin('index_advice', arguments)

        await check_sections(answer_of, {**lists, 'removed': route['removed']}, names_)
        everything = [answer, *[page for key in lists for page in await pages('index_advice', {'section': key})]]
        assert not {value for value in NEVER_VALUES if value in json.dumps(everything)}
        assert not planted_numbers_in(json.dumps(everything)), planted_numbers_in(json.dumps(everything))

    world(scenario)


def test_index_advice_for_a_dataset_is_what_its_indexes_tab_reads_from_its_stored_review(world):
    async def scenario(w):
        await three_datasets(w)
        stored = dataset_review(ORDINARY_DS)
        await review_job(w, ORDINARY_DS, result=stored)
        await removal(w, f'idx_samples_meta_{ORDINARY_DS}_gone', kind='field', dataset=ORDINARY_DS, field='gone')
        # The dataset with a private field: its review's failure is kept back.
        await review_job(w, PRIVATE_DS, result=dataset_review(PRIVATE_DS), age=timedelta(hours=3))
        await review_job(
            w,
            PRIVATE_DS,
            state='failed',
            error=f'could not read {PRIVATE_VALUES[9]}',
            age=timedelta(hours=1),
        )
        # And the closed one: a review that failed, and none that succeeded.
        await review_job(w, CLOSED_DS, state='failed', error=f'could not read {CLOSED_VALUES[9]}')
        route = (await w.call(ADMIN, 'GET', f'/api/datasets/{ORDINARY_DS}/index-advisor')).json()
        assert route['review'] == stored and route['removed'], route
        for dataset_id, value in ((PRIVATE_DS, PRIVATE_VALUES[9]), (CLOSED_DS, CLOSED_VALUES[9])):
            failed = (await w.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/index-advisor')).json()['last_failure']
            assert failed and value in failed['message'], failed
        await switch_on(w)
        names_ = await dataset_names(w)
        answer = await admin('index_advice', {'dataset': ORDINARY_DS})
        assert (answer['scope'], answer['dataset']) == ('dataset', {'id': ORDINARY_DS, 'name': NAMES[ORDINARY_DS]})
        assert answer['review_running'] is False and answer['last_failure'] is None
        assert answer['link'] == address(f'/datasets/{ORDINARY_DS}')
        expected_totals = {
            'overlapping': 1,
            'possibly_unused': 1,
            'suggested': 2,
            'too_new': 1,
            'removed': 1,
            'redundant_bytes': 9000 + 24576,
        }
        assert {key: answer['totals'].get(key) for key in expected_totals} == expected_totals, answer['totals']
        assert answer['totals'].get('needed', 0) == 0
        for key in ('overlapping', 'possibly_unused', 'suggested'):
            assert same_entries(answer[key], advised_list(key, stored[key], names_)), key
        for other in ('needed', 'too_new', 'removed'):
            assert not answer.get(other), other
        rule = route['schedule']
        assert answer['schedule'] == (None if rule is None else {k: v for k, v in rule.items() if k != 'id'})
        assert answer['pending_actions'] == route['pending_actions']

        async def answer_of(arguments: dict[str, Any]) -> dict[str, Any]:
            return await admin('index_advice', {'dataset': ORDINARY_DS, **arguments})

        lists = {key: stored[key] for key in ('overlapping', 'possibly_unused', 'suggested', 'too_new')}
        await check_sections(answer_of, {**lists, 'removed': route['removed']}, names_)
        for dataset_id, sentence in ((PRIVATE_DS, KEPT_PRIVATE), (CLOSED_DS, KEPT_CLOSED)):
            kept = await admin('index_advice', {'dataset': dataset_id})
            assert kept['last_failure'] is not None, kept
            assert set(kept['last_failure']) == {'finished_at', 'error', 'error_withheld'}, kept['last_failure']
            assert (kept['last_failure']['error'], kept['last_failure']['error_withheld']) == (None, sentence), kept
            assert PRIVATE_VALUES[9] not in json.dumps(kept) and CLOSED_VALUES[9] not in json.dumps(kept)

    world(scenario)


def test_index_advice_with_no_review_or_not_enough_history_says_so_and_an_unknown_dataset_is_not_found(world):
    async def scenario(w):
        await three_datasets(w)
        await review_job(
            w,
            CLOSED_DS,
            result=review(CLOSED_DS, enough_history=False, too_new=[field_index(CLOSED_DS, 'crop')]),
        )
        for path in ('/api/admin/index-advisor', f'/api/datasets/{ORDINARY_DS}/index-advisor'):
            assert (await w.call(ADMIN, 'GET', path)).json()['review'] is None, path
        young_route = (await w.call(ADMIN, 'GET', f'/api/datasets/{CLOSED_DS}/index-advisor')).json()
        assert young_route['review']['enough_history'] is False, young_route
        await switch_on(w)
        for arguments in ({}, {'dataset': ORDINARY_DS}):
            none_yet = await admin('index_advice', arguments)
            assert none_yet['reviewed_at'] is None, none_yet
            for key in SECTIONS:
                assert not none_yet.get(key), (arguments, key)
            assert isinstance(none_yet.get('note'), str) and 'review' in none_yet['note'].lower(), none_yet
        young = await admin('index_advice', {'dataset': CLOSED_DS})
        assert young['enough_history'] is False and young['reviewed_at'] is not None, young
        assert isinstance(young.get('note'), str) and 'unused' in young['note'].lower(), young
        missing = await call(ADMIN, 'index_advice', {'dataset': 999999})
        assert not missing['ok'] and missing['error']['code'] == 'not_found', missing

    world(scenario)


def test_index_advice_on_a_real_review_keeps_back_a_hand_made_definition_quoting_a_value_and_gives_the_rest(world):
    async def scenario(w):
        await three_datasets(w)
        made = await real_indexes(w)
        system_route = (await w.call(ADMIN, 'GET', '/api/admin/index-advisor')).json()
        dataset_route = (await w.call(ADMIN, 'GET', f'/api/datasets/{ORDINARY_DS}/index-advisor')).json()
        printed = {entry['name']: entry['definition'] for entry in every_index_entry(dataset_route['review'])}
        held = {entry['name']: entry['definition'] for entry in every_index_entry(system_route['review'])}
        assert PRIVATE_VALUES[6] in held['idx_hand_private'] and CLOSED_VALUES[5] in held['idx_hand_closed'], held
        assert "'crop'::text" in printed[made['field']], printed
        assert IDX_VALUES[3] in held['idx_samples_site_gin'] and IDX_VALUES[6] in held['ex_hand_site'], held
        await switch_on(w)
        answers = []
        for arguments in ({}, {'dataset': ORDINARY_DS}):
            answers.append(await admin('index_advice', arguments))
            for key in SECTIONS:
                answer = await call(ADMIN, 'index_advice', {**arguments, 'section': key, 'limit': 50})
                if answer['ok']:
                    answers += [answer['result'], *await pages('index_advice', {**arguments, 'section': key})]
        await check_real_definitions(w, answers, made)
        assert not {value for value in NEVER_VALUES if value in json.dumps(answers)}

    world(scenario)


# --- 6. GET /api/admin/jobs/all ---

ROUTE_ITEM = {
    'id',
    'kind',
    'state',
    'dataset_id',
    'dataset_name',
    'field',
    'started_by',
    'created_at',
    'started_at',
    'finished_at',
    'progress',
    'failures',
    'max_failures',
    'cancel_requested',
    'error',
}


def test_the_all_jobs_route_is_for_administrators_only(world):
    async def scenario(w):
        await three_datasets(w)
        jobs = await plant_jobs(w)
        for user_id, status in ((POWER, 403), (GUEST, 403), (None, 401)):
            response = await w.call(user_id, 'GET', ALL_JOBS)
            assert response.status_code == status, (user_id, response.text)
            assert set(response.json()) <= {'detail', 'code'}, response.text
            assert not {job_id for job_id in jobs.values() if job_id in response.text}, response.text
        assert (await w.call(ADMIN, 'GET', ALL_JOBS)).status_code == 200

    world(scenario)


def test_the_all_jobs_route_answers_each_parameter_with_exactly_the_named_keys(world):
    async def scenario(w):
        await three_datasets(w)
        jobs = await plant_jobs(w)
        await job(w, 'selection_cleanup', 'failed', error='an old failure', age=timedelta(hours=300))
        old_queued = await job(w, 'region_load', 'queued', age=timedelta(days=30))  # in every window: not ended
        dump = {'file': 'freedomridge_20261101T020000Z.sql.gz', 'size_bytes': 3400, 'tables': 30}
        backed_up = await job(w, 'backup', 'succeeded', result={**dump, 'note': RESULT_MARK}, age=minutes(3))
        backup_failed = await job(w, 'backup', 'failed', error='BackupError: no space', age=minutes(2))
        backup_queued = await job(w, 'backup', 'queued', age=minutes(1), lane='maintenance')
        names_ = await dataset_names(w)
        now = await w.database.fetchval('SELECT now()')

        async def listed(**params: Any) -> dict[str, Any]:
            response = await w.call(ADMIN, 'GET', ALL_JOBS, params=params)
            assert response.status_code == 200, (params, response.text)
            body = response.json()
            assert set(body) == {'jobs', 'total', 'counts', 'kinds', 'offset', 'next_offset'}, body.keys()
            for item in body['jobs']:
                named = ROUTE_ITEM | {'backup'} if item['kind'] == 'backup' else ROUTE_ITEM
                assert set(item) == named, sorted(set(item) ^ named)
            assert RESULT_MARK not in response.text and ARG_MARK not in response.text
            assert set(body['kinds']) == (await truth(w, hours=hours_of(params)))['kinds'], params  # whatever filters
            return body

        def hours_of(params: dict[str, Any]) -> float:
            if 'since' not in params:
                return 1e6
            return (now - datetime.fromisoformat(params['since'])).total_seconds() / 3600

        since = (now - timedelta(hours=400)).isoformat()
        whole = await listed(since=since, limit=100)
        expected = await truth(w, states=STATES, hours=400)
        assert [item['id'] for item in whole['jobs']] == expected['ids']
        assert (whole['total'], whole['offset'], whole['next_offset']) == (len(expected['ids']), 0, None)
        assert whole['counts'] == expected['counts'] and set(whole['kinds']) == expected['kinds']
        for item in whole['jobs']:
            row = await facts(w, item['id'])
            about = row['about']
            assert (item['kind'], item['state'], item['dataset_id']) == (row['kind'], row['state'], about), item
            assert item['dataset_name'] == (None if about is None else names_.get(about)), item
            assert (item['failures'], item['max_failures'], item['cancel_requested']) == (
                row['failures'],
                row['max_failures'],
                False,
            )
            for key in ('created_at', 'started_at', 'finished_at'):
                assert moment(item[key]) == moment(row[key]), (key, item)
        by_id = {item['id']: item for item in whole['jobs']}
        assert by_id[jobs['private_failed']]['field'] == 'plot_code'
        # started_by as the jobs page shows it: what the existing job route answers for the same job.
        for item in whole['jobs']:
            shown = await w.call(ADMIN, 'GET', f"/api/jobs/{item['id']}")
            assert shown.status_code == 200, shown.text
            assert item['started_by'] == shown.json()['started_by'], (item, shown.json()['started_by'])
        users = {row['id']: row['username'] for row in (await w.call(ADMIN, 'GET', '/api/users')).json()}
        assert by_id[jobs['private_failed']]['started_by'] == users[ADMIN]
        assert by_id[jobs['none_failed']]['started_by'] == 'System'
        assert by_id[jobs['deleted_user']]['started_by'] == 'Deleted user'
        assert by_id[jobs['private_review']]['dataset_id'] == PRIVATE_DS  # the dataset it is about
        assert (by_id[jobs['gone_failed']]['dataset_id'], by_id[jobs['gone_failed']]['dataset_name']) == (GONE_DS, None)
        # A backup's own {file, size_bytes, tables}, null until it has succeeded; no other kind has the key.
        assert by_id[backed_up]['backup'] == dump, by_id[backed_up]
        assert by_id[backup_failed]['backup'] is None and by_id[backup_queued]['backup'] is None
        assert old_queued in by_id and by_id[old_queued]['state'] == 'queued'
        assert ORD_ERROR[:400] in json.dumps(by_id[jobs['ordinary_failed']]['error'])
        assert NONE_ERROR[:400] in json.dumps(by_id[jobs['none_failed']]['error'])
        # state, repeated; counts stay by state for the same kind, dataset and window.
        for states in (('failed',), ('failed', 'running'), ('queued',), ('succeeded', 'cancelled')):
            body = await listed(state=list(states), since=since, limit=100)
            assert [item['id'] for item in body['jobs']] == (await truth(w, states=states, hours=400))['ids'], states
            assert body['counts'] == expected['counts'], states
        # kind, dataset_id, since.
        for params, kind, dataset in (
            ({'kind': 'bulk_mutation'}, 'bulk_mutation', None),
            ({'dataset_id': ORDINARY_DS}, None, ORDINARY_DS),
            ({'dataset_id': PRIVATE_DS}, None, PRIVATE_DS),
            ({'kind': index_advisor.KIND, 'dataset_id': CLOSED_DS}, index_advisor.KIND, CLOSED_DS),
        ):
            body = await listed(since=since, limit=100, **params)
            sure = await truth(w, states=STATES, kind=kind, dataset=dataset, hours=400)
            assert [item['id'] for item in body['jobs']] == sure['ids'] and body['counts'] == sure['counts'], params
        recent = await listed(since=(now - timedelta(hours=1)).isoformat(), limit=100)
        assert [item['id'] for item in recent['jobs']] == (await truth(w, states=STATES, hours=1))['ids']
        assert len(recent['jobs']) == len(whole['jobs']) - 1  # the old failure is outside the hour
        assert old_queued in {item['id'] for item in recent['jobs']}  # still queued: inside every window
        # limit (1 to 100, 50 as given none), offset and next_offset.
        assert len((await listed(since=since))['jobs']) == min(50, len(expected['ids']))
        offset, collected = 0, []
        while True:
            body = await listed(since=since, limit=4, offset=offset)
            assert body['offset'] == offset and len(body['jobs']) <= 4
            collected += [item['id'] for item in body['jobs']]
            if body['next_offset'] is None:
                break
            assert body['next_offset'] == offset + len(body['jobs'])
            offset = body['next_offset']
        assert collected == expected['ids']
        for params in ({'limit': 0}, {'limit': 101}):
            assert (await w.call(ADMIN, 'GET', ALL_JOBS, params=params)).status_code == 422, params
        assert (await w.call(ADMIN, 'GET', ALL_JOBS, params={'limit': 100})).status_code == 200

    world(scenario)


# What the existing job routes answer on main, pinned (m2 4: "The existing job routes are unchanged").
JOB_STATUS_KEYS = {
    'id',
    'kind',
    'dataset_id',
    'placement',
    'lane',
    'resources',
    'field',
    'state',
    'progress',
    'result',
    'error',
    'created_by',
    'created_at',
    'updated_at',
    'started_at',
    'finished_at',
    'heartbeat_at',
    'attempt',
    'failures',
    'max_failures',
    'available_at',
    'cancel_requested',
    'started_by',
    'waiting_for',
}
ACTIVE_KEYS = {
    'id',
    'kind',
    'placement',
    'lane',
    'state',
    'started_by',
    'progress',
    'cancel_requested',
    'can_cancel',
    'stalled',
    'retry_delayed',
    'dataset_lane',
    'waiting_for',
}


def test_the_existing_job_routes_answer_as_before(world):
    async def scenario(w):
        await three_datasets(w)
        jobs = await plant_jobs(w)
        backup = await job(
            w,
            'backup',
            'failed',
            error='BackupError: pg_dump exited 1',
            result={'file': 'freedomridge_x.sql.gz'},
            failures=2,
            max_failures=2,
        )
        await w.share(ORDINARY_DS, POWER, 'view')
        listed = (await w.call(ADMIN, 'GET', '/api/admin/jobs', params={'limit': 100})).json()
        assert set(listed) == {'items', 'next_cursor'}
        item = {entry['id']: entry for entry in listed['items']}[backup]
        assert set(item) == JOB_STATUS_KEYS | {'can_control'}, sorted(set(item) ^ (JOB_STATUS_KEYS | {'can_control'}))
        assert {k: item[k] for k in ('kind', 'dataset_id', 'state', 'field', 'started_by', 'can_control')} == {
            'kind': 'backup',
            'dataset_id': None,
            'state': 'failed',
            'field': None,
            'started_by': 'System',
            'can_control': True,
        }
        assert item['error'] == {'type': 'RuntimeError', 'message': 'BackupError: pg_dump exited 1'}
        assert item['result'] == {'file': 'freedomridge_x.sql.gz'}
        assert item['progress'] == {'done': 0, 'total': None, 'message': None}
        assert (item['failures'], item['max_failures'], item['cancel_requested'], item['waiting_for']) == (
            2,
            2,
            False,
            None,
        )
        assert {entry['id'] for entry in listed['items']} >= {jobs['none_failed'], jobs['private_review']}
        one = (await w.call(ADMIN, 'GET', f'/api/jobs/{backup}')).json()
        assert set(one) == JOB_STATUS_KEYS and one['error'] == item['error'] and one['result'] == item['result']
        # A dataset's jobs, for an administrator and for a power user who views it.
        theirs = (await w.call(ADMIN, 'GET', f'/api/datasets/{ORDINARY_DS}/jobs', params={'limit': 100})).json()
        by_id = {entry['id']: entry for entry in theirs['items']}
        assert {jobs['ordinary_failed'], jobs['ordinary_running'], jobs['ordinary_cancelled']} <= set(by_id)
        assert set(by_id[jobs['ordinary_failed']]) == JOB_STATUS_KEYS | {'can_control'}
        assert by_id[jobs['ordinary_failed']]['error'] == {'type': 'RuntimeError', 'message': ORD_ERROR}
        assert by_id[jobs['ordinary_running']]['progress'] == {'done': 5, 'total': None, 'message': ORD_PROGRESS}
        viewer = (await w.call(POWER, 'GET', f'/api/datasets/{ORDINARY_DS}/jobs', params={'limit': 100})).json()
        seen = {entry['id']: entry for entry in viewer['items']}[jobs['ordinary_cancelled']]
        assert set(seen) == (JOB_STATUS_KEYS - {'result', 'error'}) | {'can_control'}, sorted(seen)
        assert seen['can_control'] is False
        active = (await w.call(ADMIN, 'GET', f'/api/datasets/{ORDINARY_DS}/jobs/active')).json()
        assert set(active) == {'running', 'resuming', 'waiting', 'next_cursor'}
        running = {entry['id']: entry for entry in active['running']}[jobs['ordinary_running']]
        assert set(running) == ACTIVE_KEYS, sorted(running)
        assert running['progress'] == {'done': 5, 'total': None, 'message': ORD_PROGRESS}
        waiting = {entry['id'] for entry in active['waiting']}
        assert jobs['ordinary_queued'] in waiting

    world(scenario)


# --- 5. Words and docs ---


def docs_section(text: str, heading: str) -> str:
    """The text under `heading` (a whole line) up to the next heading of the same or a higher level; a `#` line inside
    a fenced code block is no heading."""
    lines = text.splitlines()
    starts = [n for n, line in enumerate(lines) if line.rstrip() == heading]
    assert starts, heading
    level = len(heading) - len(heading.lstrip('#'))
    found: list[str] = []
    fenced = False
    for line in lines[starts[0] + 1 :]:
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
        elif not fenced and re.match(rf'#{{1,{level}}} ', line):
            break
        found.append(line)
    return '\n'.join(found)


def test_the_docs_describe_the_administration_tools():
    text = (ROOT / 'docs/ASSISTANT.md').read_text()
    heading = re.search(r'^(#+) Administration tools\s*$', text, re.M)
    assert heading, 'docs/ASSISTANT.md has no section "Administration tools"'
    part = docs_section(text, heading.group(0).strip())
    for name in FOUR:
        assert name in part, name
    lowered = ' '.join(part.lower().split())
    assert 'administrator' in lowered and 'switch' in lowered, part[:500]
    assert all(word in lowered for word in ('password', 'token', 'closed')), part[:500]
    # A field's values kept from the assistant, in the app's own words or as "private".
    assert 'private' in lowered or 'kept from' in lowered, part[:500]
    developers = docs_section(text, '## How a tool reads (for developers)').lower()
    assert 'administration tool' in developers, 'the fence paragraph does not say which tools may read those routes'
    unreleased = docs_section((ROOT / 'CHANGELOG.md').read_text(), '## Unreleased').lower()
    assert 'administration tool' in unreleased, 'CHANGELOG.md Unreleased says nothing of the administration tools'


# --- 3.4: a shipped index's name is not enough (last in the file; each test has a schema of its own, so the swap
# below is seen by no other test) ---

# The swapped shipped indexes, each on a table nothing in these tests reads or writes, so the moment one is missing
# or replaced can disturb nothing (the job queue's own indexes, idx_jobs_ready among them, are left alone):
# compute_launches_live, whose predicate quotes a constant ('ended'), and idx_storage_configs_has_patterns, the one
# index schema.sql ships whose statement holds a number outside dataset_id = N (`array_length(bucket_patterns, 1)
# > 0`). Each is remade under its name with a value of the test's own: a quoted text, a number.
SHIPPED_SWAPPED = {
    'compute_launches_live': (
        "CREATE INDEX compute_launches_live ON compute_launches (launched_at) WHERE state = '{}'".format(IDX_VALUES[9]),
        IDX_VALUES[9],
    ),
    'idx_storage_configs_has_patterns': (
        'CREATE INDEX idx_storage_configs_has_patterns ON storage_configs ((array_length(bucket_patterns, 1))) '
        f'WHERE array_length(bucket_patterns, 1) > {SWAPPED_NUMBER}',
        SWAPPED_NUMBER,
    ),
}


def test_an_index_under_a_shipped_name_holding_a_value_of_its_own_is_kept_back_and_the_shipped_one_is_whole(world):
    async def scenario(w):
        shipped = {name: shipped_statement(name) for name in SHIPPED_SWAPPED}
        assert "'ended'" in shipped['compute_launches_live'], shipped
        assert numbers_in(shipped['idx_storage_configs_has_patterns']) == {'1', '0'}, shipped
        try:
            for name, (statement, _) in SHIPPED_SWAPPED.items():
                await w.database.execute(f'DROP INDEX {name}')
                await w.database.execute(statement)
            await run_review(w, None)
            route = (await w.call(ADMIN, 'GET', '/api/admin/index-advisor')).json()
            held = {entry['name']: entry for entry in every_index_entry(route['review'])}
            for name, (_, value) in SHIPPED_SWAPPED.items():
                assert value in held[name]['definition'], held.get(name)
                assert withheld_by_rule(name, held[name]['table'], held[name]['definition']), held[name]
            await switch_on(w)
            answers = await index_answers()
            text = json.dumps(answers)
            for name, (_, value) in SHIPPED_SWAPPED.items():
                entries = [entry for entry in every_index_entry(answers) if entry['name'] == name]
                assert entries and all(e['definition'] is None and e['definition_withheld'] for e in entries), name
                assert value not in text if not value.isdigit() else not planted_numbers_in(text), (name, value)
        finally:
            for name, statement in shipped.items():
                await w.database.execute(f'DROP INDEX IF EXISTS {name}')
                await w.database.execute(statement)  # exactly as schema.sql defines it
        await run_review(w, None)
        route = (await w.call(ADMIN, 'GET', '/api/admin/index-advisor')).json()
        printed = {entry['name']: entry for entry in every_index_entry(route['review'])}
        answers = await index_answers()
        for name in SHIPPED_SWAPPED:
            assert not withheld_by_rule(name, printed[name]['table'], printed[name]['definition']), printed[name]
            entries = [entry for entry in every_index_entry(answers) if entry['name'] == name]
            assert entries, f'{name} was not answered'
            whole = (printed[name]['definition'], False)
            assert all((e['definition'], e['definition_withheld']) == whole for e in entries), name

    world(scenario)


async def index_answers() -> list[Any]:
    """index_advice of the system: the first page and every page of each section."""
    answers: list[Any] = [await admin('index_advice')]
    for key in SECTIONS:
        answer = await call(ADMIN, 'index_advice', {'section': key, 'limit': 50})
        if answer['ok']:
            answers += await pages('index_advice', {'section': key})
    return answers
