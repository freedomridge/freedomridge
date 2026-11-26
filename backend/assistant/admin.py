"""The administration tools (docs/ASSISTANT.md, "Administration tools"): four tools that read this server's status, its
backups, its background jobs and the index advisor's last review, for an administrator, while the administration tools
are switched on (Settings → Assistant). They only read: each answer links to the page where an administrator acts.

Each reads through the API in process as the administrator, like every tool (api.py, inner.py), and only the routes of
private.SPANNING_ROUTES, which answer across datasets and know nothing of a dataset closed to the assistant or of a
field's values kept from it. So:
- An answer is built from named facts, never passed through: every key below is copied by name, and a key a route adds
  later appears in no answer until it is named here.
- What could hold a private field's value or anything of a closed dataset's samples is taken out before a tool reads
  an answer (private.keep_back, in inner.request): a job's words by the datasets it is about (or, of no dataset, by its
  kind), an index's definition when it holds a constant (a quoted text or a number) the server doesn't write itself.
- Every free text copied (an error, a message, a reason) is cut to TEXT_LIMIT characters, with what an answer never
  holds written as what it is: an email or IP address, a web or connection address, a file-system path other than the
  backup folder, a hash, a secret (`_said`).
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from . import links, private
from .api import Api, ToolError, cut
from .catalog import Tool

STATUS = '/api/admin/status'
BACKUP_SCHEDULE = '/api/admin/backups/schedule'
DATASETS = '/api/datasets'
JOBS = private.JOBS_ALL
SYSTEM_INDEXES = private.SYSTEM_INDEX_ADVICE
DATASET_INDEXES = private.DATASET_INDEX_ADVICE

TEXT_LIMIT = 500
# The most a page of a list takes as JSON, so that an answer stays under 20 KB with its other keys (as working.py): a
# page of long entries holds fewer than its `limit`, and next_offset says where the rest start.
_PAGE_BYTES = 15_000
# Without a section, index_advice's lists share less, so that one entry more of each still fits.
_LISTS_BYTES = 12_000
_DATASET_ID = {'type': 'integer', 'minimum': 1, 'maximum': 2_147_483_647}


# --- free text ---


def _not_bare(label: str) -> Callable[[re.Match[str]], str]:
    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(0) == '::' else label

    return replace


# What an answer never holds (contract 3.3), in the order they are looked for (after paths): an address before the
# email in it, a secret's name before its value.
_NEVER: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\'"<>()]+'), '[address]'),
    (re.compile(r'[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+'), '[email address]'),
    (
        re.compile(
            r'(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key(?:[_-]?id)?|'
            r'secret[_-]?(?:access[_-]?)?key|authorization)(\s*[=:]\s*)("[^"]*"|\'[^\']*\'|\S+)',
        ),
        r'\1\2[secret]',
    ),
    (re.compile(r'(?i)\bbearer\s+\S+'), 'Bearer [secret]'),
    (re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'), '[storage key]'),
    (re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d|\w)'), '[IP address]'),
)
_IPV6 = re.compile(
    r'(?<![\w:.])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}'
    r'|(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?)(?![\w:])',
)
_PATH = re.compile(r'(?<![\w.~:/-])(?:~|\.{1,2})?(?:/[\w.@%+=,:~-]+)+/?|(?<!\w)[A-Za-z]:\\[^\s\'"]+')
# A hash: 12 hexadecimal characters or more, with a digit and a letter a to f (never an ordinary number or word).
_HASH = re.compile(r'(?<![\w-])(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{12,}(?![\w-])')
_TOKEN = re.compile(r'(?<![\w/+=-])(?=[\w/+=-]*[A-Z])(?=[\w/+=-]*[a-z])(?=[\w/+=-]*\d)[\w/+=-]{24,}(?![\w/+=-])')


def _said(text: Any, folder: Optional[str] = None) -> Optional[str]:
    """Free text as an answer may give it: what is never given (paths other than `folder` and what is inside it,
    _NEVER, IPv6 addresses, hashes, token-like strings) written as what it is, then cut to TEXT_LIMIT characters. A
    path in `folder` is set aside first, so nothing after reads part of it as something else."""
    if text is None:
        return None
    if not isinstance(text, str):
        text = json.dumps(text)
    root = folder.rstrip('/') if folder else None
    aside: list[str] = []

    def path(match: re.Match[str]) -> str:
        found = match.group(0)
        if root and (found.rstrip('/') == root or found.startswith(root + '/')):
            aside.append(found)
            return f'\x00{len(aside) - 1}\x00'
        return '[path]'

    text = _PATH.sub(path, text.replace('\x00', ''))
    for pattern, label in _NEVER:
        text = pattern.sub(label, text)
    text = _IPV6.sub(_not_bare('[IP address]'), text)
    text = _HASH.sub('[hash]', text)
    text = _TOKEN.sub('[secret]', text)
    text = re.sub('\x00(\\d+)\x00', lambda match: aside[int(match.group(1))], text)
    return cut(text, TEXT_LIMIT)


def _error_text(error: Any) -> Optional[str]:
    """A job's error as a sentence: its type and message (the runner records both), or its code and message."""
    if error is None:
        return None
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message, kind = error.get('message'), error.get('type') or error.get('code')
        if isinstance(message, str) and message:
            named = isinstance(kind, str) and kind and not message.startswith(kind)
            return f'{kind}: {message}' if named else message
        if isinstance(kind, str):
            return kind
    return json.dumps(error)


def _fit(entries: list[dict[str, Any]], budget: int = _PAGE_BYTES) -> list[dict[str, Any]]:
    """The first entries that fit in `budget` bytes of JSON (always at least one)."""
    kept: list[dict[str, Any]] = []
    size = 0
    for entry in entries:
        size += len(json.dumps(entry)) + 2
        if kept and size > budget:
            break
        kept.append(entry)
    return kept


def _dict(value: Any) -> dict[str, Any]:
    """A dict from a route, or an empty one for a value that isn't one."""
    return value if isinstance(value, dict) else {}


def _picked(source: Any, keys: tuple[str, ...]) -> Optional[dict[str, Any]]:
    """The named keys of a dict from a route (None for a value that isn't one)."""
    return {key: source.get(key) for key in keys} if isinstance(source, dict) else None


def _following(offset: int, shown: int, total: int) -> Optional[int]:
    return offset + shown if shown and offset + shown < total else None


def _strings(values: Any) -> list[str]:
    return [cut(value, 200) for value in values if isinstance(value, str)] if isinstance(values, list) else []


# --- system_status ---

_DISK = ('total_bytes', 'free_bytes', 'level', 'low_below_bytes', 'ingest_paused_below_bytes')


def _backups_short(backups: Mapping[str, Any]) -> dict[str, Any]:
    latest = _dict(backups.get('latest'))
    schedule = _dict(backups.get('schedule'))
    return {
        'level': backups.get('level'),
        'latest_at': latest.get('created_at'),
        'next_run_at': schedule.get('next_run_at'),
    }


async def system_status(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    status = await api.get(STATUS)
    database = _dict(status.get('database'))
    if database.get('reachable'):
        health: dict[str, Any] = {
            'reachable': True,
            'size_bytes': database.get('size_bytes'),
            'pending_migrations': _strings(database.get('pending_migrations')),
            'unknown_migrations': _strings(database.get('unknown_migrations')),
            'stale_functions': _strings(database.get('stale_functions')),
        }
    else:
        health = {'reachable': False, 'error': _said(database.get('error'))}
    workers = _dict(status.get('workers'))
    if workers.get('available'):
        runners: dict[str, Any] = {
            placement: _picked(workers.get(placement), ('online', 'last_seen')) for placement in ('server', 'compute')
        }
    else:
        runners = {'available': False, 'error': _said(workers.get('error'))}
    server = _dict(status.get('server'))
    backups = _dict(status.get('backups'))
    return {
        'checked_at': status.get('checked_at'),
        'server': {'version': server.get('version'), 'restart_needed': server.get('restart_needed')},
        'database': health,
        'database_disk': _picked(status.get('database_disk'), _DISK),
        'cache_disks': [
            {
                # The folders' labels, never their paths.
                'folders': [folder.get('label') for folder in disk.get('folders') or [] if isinstance(folder, dict)],
                'total_bytes': disk.get('total_bytes'),
                'free_bytes': disk.get('free_bytes'),
                'level': disk.get('level'),
            }
            for disk in status.get('cache_disks') or []
            if isinstance(disk, dict)
        ],
        'workers': runners,
        'backups': _backups_short(backups),
        'link': links.status_page(),
    }


# --- backup_status ---

NOT_CONFIGURED = (
    'This server takes no backups through the app (no backup folder is set). A deployment may back up another way; '
    'see the backup docs.'
)
_LATEST = ('file', 'created_at', 'size_bytes', 'dump_seconds', 'verify_seconds', 'tables')
RECENT_RUNS = 10


def _run(job: Mapping[str, Any], folder: Optional[str]) -> dict[str, Any]:
    """One backup job, with the file and size of its own backup once it has succeeded (the jobs route's `backup`)."""
    made = _dict(job.get('backup'))
    file = made.get('file')
    return {
        'job': job.get('id'),
        'state': job.get('state'),
        'started_at': job.get('started_at'),
        'finished_at': job.get('finished_at'),
        'file': cut(file, 200) if isinstance(file, str) else None,
        'size_bytes': made.get('size_bytes'),
        # A backup job is about no dataset (keep_back gives its words), in pg_dump's and the script's own words.
        'error': None if job.get('withheld', 'unknown') is not None else _said(_error_text(job.get('error')), folder),
    }


async def backup_status(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    status = await api.get(STATUS)
    backups = _dict(status.get('backups'))
    level = backups.get('level')
    answer: dict[str, Any] = {
        'level': level,
        'folder': None,
        'latest': None,
        'last_failure': None,
        'kept': None,
        'unverified_dumps': None,
        'disk': None,
        'schedule': None,
        'running': None,
        'recent_runs': [],
        'link': links.status_page(),
    }
    if level == 'not_configured':
        return {**answer, 'note': NOT_CONFIGURED}
    folder = backups.get('folder') if isinstance(backups.get('folder'), str) else None
    answer['folder'] = folder
    if level == 'unreadable':
        answer['error'] = _said(backups.get('error'), folder)
    else:
        answer['latest'] = _picked(backups.get('latest'), _LATEST)
        failure = backups.get('last_failure')
        if isinstance(failure, dict):
            answer['last_failure'] = {
                'failed_at': failure.get('failed_at'),
                'error': _said(failure.get('error'), folder),
            }
        answer['kept'] = _picked(backups.get('kept'), ('count', 'total_bytes', 'oldest_created_at'))
        answer['unverified_dumps'] = backups.get('unverified_dumps')
        answer['disk'] = _picked(backups.get('disk'), ('total_bytes', 'free_bytes'))
    active = backups.get('active_job')
    if isinstance(active, dict):
        answer['running'] = {
            'job': active.get('id'),
            'state': active.get('state'),
            'started_at': active.get('started_at'),
        }
    schedule = await api.get(BACKUP_SCHEDULE)
    if isinstance(schedule, dict):
        upcoming = _dict(backups.get('schedule'))
        answer['schedule'] = {
            'interval_hours': schedule.get('interval_hours'),
            'start_hour': schedule.get('start_hour'),
            'keep': schedule.get('keep'),
            'next_run_at': upcoming.get('next_run_at'),
        }
    runs = await api.get(JOBS, query={'kind': 'backup', 'limit': RECENT_RUNS})
    answer['recent_runs'] = [_run(job, folder) for job in runs['jobs'] if isinstance(job, dict)]
    return answer


# --- list_jobs ---

NEEDS_ATTENTION = ('failed', 'queued', 'running')
_STATES = ('failed', 'running', 'queued', 'succeeded', 'cancelled')
# Why a job's messages aren't given (private.keep_back's `withheld`).
WITHHELD = {
    private.CLOSED: "This job's messages are kept back: its dataset is closed to the assistant",
    private.PRIVATE_FIELDS: (
        "This job's messages are kept back: its dataset has fields whose values are kept from the assistant"
    ),
    private.GONE: "This job's messages are kept back: its dataset no longer exists",
    private.UNKNOWN_KIND: "This job's messages are kept back: this kind of job is not yet known to be about no dataset",
}
_KEPT_BACK = "This job's messages are kept back"


def _job(job: Mapping[str, Any]) -> dict[str, Any]:
    about = job.get('dataset_id')
    # Missing only if keep_back never saw the job: then nothing of its words is given.
    withheld = job.get('withheld', 'unknown')
    progress = _dict(job.get('progress'))
    return {
        'id': job.get('id'),
        'kind': job.get('kind'),
        'state': job.get('state'),
        'dataset': {'id': about, 'name': job.get('dataset_name')} if isinstance(about, int) else None,
        'field': job.get('field'),
        'started_by': job.get('started_by'),
        'created_at': job.get('created_at'),
        'started_at': job.get('started_at'),
        'finished_at': job.get('finished_at'),
        'progress': {
            'done': progress.get('done'),
            'total': progress.get('total'),
            'message': None if withheld is not None else _said(progress.get('message')),
        },
        'failures': job.get('failures'),
        'max_failures': job.get('max_failures'),
        'error': None if withheld is not None else _said(_error_text(job.get('error'))),
        'error_withheld': None if withheld is None else WITHHELD.get(withheld, _KEPT_BACK),
        'link': links.dataset_page(about) if isinstance(about, int) else links.status_page(),
    }


async def list_jobs(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    state = arguments.get('state', 'needs_attention')
    dataset_id, limit, offset = arguments.get('dataset'), arguments.get('limit', 20), arguments.get('offset', 0)
    since = datetime.now(timezone.utc) - timedelta(hours=arguments.get('since_hours', 168))
    page = await api.get(
        JOBS,
        missing=f'Dataset not found: id {dataset_id}',
        query={
            'state': list(NEEDS_ATTENTION) if state == 'needs_attention' else [state],
            'kind': arguments.get('kind') or None,
            'dataset_id': dataset_id,
            'since': since.isoformat(),
            'limit': limit,
            'offset': offset,
        },
    )
    jobs = _fit([_job(job) for job in page['jobs'] if isinstance(job, dict)])
    counts = _dict(page.get('counts'))
    return {
        'jobs': jobs,
        'total': page['total'],
        'offset': offset,
        'next_offset': _following(offset, len(jobs), page['total']),
        'counts': {name: counts.get(name, 0) for name in _STATES},
        'kinds': _strings(page.get('kinds')),
        'since': since.isoformat(),
        'link': links.dataset_page(dataset_id) if dataset_id is not None else links.status_page(),
    }


# --- index_advice ---

SECTIONS = ('overlapping', 'possibly_unused', 'suggested', 'needed', 'too_new', 'removed')
_SCHEDULE = ('enabled', 'frequency', 'weekday', 'time', 'timezone', 'next_run_at', 'last_run_at')
_USAGE = ('uses', 'mean_ms', 'p50_ms', 'p95_ms', 'reference_ms', 'slower')
_REMOVED = ('index_name', 'table', 'field', 'size_bytes', 'reason', 'removed_by', 'removed_at', 'state')


def _index(entry: Any, names: Mapping[int, Optional[str]]) -> dict[str, Any]:
    entry = _dict(entry)
    owner = entry.get('dataset_id')
    # keep_back marks every definition; one it didn't mark is kept back too.
    withheld = entry.get('definition_withheld') is not False or not isinstance(entry.get('definition'), str)
    return {
        'name': entry.get('name'),
        'table': entry.get('table'),
        'kind': entry.get('kind'),
        'method': entry.get('method'),
        'dataset': {'id': owner, 'name': names.get(owner)} if isinstance(owner, int) else None,
        'field': entry.get('field'),
        'size_bytes': entry.get('size_bytes'),
        'scans': entry.get('scans'),
        'last_scan': entry.get('last_scan'),
        'valid': entry.get('valid'),
        'needed': [_said(reason) for reason in entry.get('needed') or [] if isinstance(reason, str)],
        # Given whole or kept back whole (private.keep_back): SQL, not free text, so never cut or rewritten.
        'definition': None if withheld else entry['definition'],
        'definition_withheld': withheld,
    }


def _unused(entry: Any, names: Mapping[int, Optional[str]]) -> dict[str, Any]:
    entry = _dict(entry)
    return {**_index(entry, names), 'idle_days': entry.get('idle_days'), 'invalid': entry.get('invalid')}


def _pair(pair: Any, names: Mapping[int, Optional[str]]) -> dict[str, Any]:
    pair = _dict(pair)
    return {
        'redundant': _index(pair.get('redundant'), names),
        'kept': _index(pair.get('kept'), names),
        'rule': pair.get('rule'),
        'reason': _said(pair.get('reason')),
    }


def _suggestion(item: Any, names: Mapping[int, Optional[str]]) -> dict[str, Any]:
    item = _dict(item)
    usages = _dict(item.get('usages'))
    return {
        'field': item.get('field'),
        'type': item.get('type'),
        'reason': item.get('reason'),
        'excess_seconds': item.get('excess_seconds'),
        'build_seconds': item.get('build_seconds'),
        'pays_back': item.get('pays_back'),
        'usages': {str(usage)[:50]: _picked(numbers, _USAGE) for usage, numbers in usages.items()},
    }


def _removal(item: Any, names: Mapping[int, Optional[str]]) -> dict[str, Any]:
    removal = _picked(item, _REMOVED) or dict.fromkeys(_REMOVED)
    return {**removal, 'reason': _said(removal['reason'])}


_SHAPES: dict[str, Callable[[Any, Mapping[int, Optional[str]]], dict[str, Any]]] = {
    'overlapping': _pair,
    'possibly_unused': _unused,
    'suggested': _suggestion,
    'needed': _index,
    'too_new': _index,
    'removed': _removal,
}


def _owners(items: list[Any]) -> set[int]:
    """The datasets the index entries among `items` belong to."""
    found = set()
    for item in items:
        for entry in (item, item.get('redundant'), item.get('kept')) if isinstance(item, dict) else ():
            if isinstance(entry, dict) and isinstance(entry.get('dataset_id'), int):
                found.add(entry['dataset_id'])
    return found


async def index_advice(api: Api, arguments: dict[str, Any]) -> dict[str, Any]:
    dataset_id, section = arguments.get('dataset'), arguments.get('section')
    limit, offset = arguments.get('limit', 20), arguments.get('offset', 0)
    if offset and section is None:
        raise ToolError('invalid', 'offset pages one list: give section with it')
    if dataset_id is not None:
        state = await api.get(DATASET_INDEXES, id=dataset_id, missing=f'Dataset not found: id {dataset_id}')
    else:
        state = await api.get(SYSTEM_INDEXES)
    review = _dict(state.get('review'))
    lists: dict[str, list[Any]] = {
        name: [item for item in (review.get(name) or []) if isinstance(item, dict)] for name in SECTIONS
    }
    lists['removed'] = [item for item in state.get('removed') or [] if isinstance(item, dict)]
    redundant: dict[Any, int] = {}
    for entry in [pair.get('redundant') for pair in lists['overlapping']] + lists['possibly_unused']:
        if isinstance(entry, dict):
            redundant[entry.get('name')] = entry.get('size_bytes') or 0  # each index once
    shown_lists = [section] if section else ['overlapping', 'possibly_unused', *(['suggested'] if dataset_id else [])]
    pages = {name: lists[name][offset : offset + limit] if section else lists[name][:limit] for name in shown_lists}
    dataset = _picked(state.get('dataset'), ('id', 'name')) if dataset_id is not None else None
    if dataset is not None:
        names: dict[int, Optional[str]] = {dataset['id']: dataset['name']}
    elif _owners([item for page in pages.values() for item in page]):
        names = {item['id']: item.get('name') for item in await api.get(DATASETS) if isinstance(item, dict)}
    else:
        names = {}
    failure = state.get('last_failure') if isinstance(state.get('last_failure'), dict) else None
    last_failure = None
    if failure is not None:
        withheld = failure.get('withheld')
        last_failure = {
            'finished_at': failure.get('finished_at'),
            'error': None if withheld else _said(failure.get('message')),
            'error_withheld': WITHHELD.get(withheld, _KEPT_BACK) if withheld else None,
        }
    answer: dict[str, Any] = {
        'scope': 'dataset' if dataset_id is not None else 'system',
        'dataset': dataset,
        'reviewed_at': review.get('reviewed_at'),
        'enough_history': review.get('enough_history'),
        'window_days': state.get('window_days'),
        'idle_days_required': review.get('idle_days_required'),
        'review_running': state.get('active_job') is not None,
        'last_failure': last_failure,
        'totals': {**{name: len(items) for name, items in lists.items()}, 'redundant_bytes': sum(redundant.values())},
    }
    cut_short = False
    budget = _PAGE_BYTES if section else _LISTS_BYTES
    for name in shown_lists:
        shown = _fit([_SHAPES[name](item, names) for item in pages[name]], max(budget, 1))
        budget -= len(json.dumps(shown))
        answer[name] = shown
        cut_short = cut_short or len(shown) < len(pages[name])
    following = _following(offset, len(answer[section]), len(lists[section])) if section else None
    notes = []
    if not review and answer['review_running']:
        notes.append('No review has finished yet: one is running now.')
    elif not review:
        where = "the dataset's page (Dataset Settings → Indexes)" if dataset_id else 'the System status page (Indexes)'
        notes.append(f'No review has run yet: an administrator starts one on {where}.')
    elif review.get('enough_history') is False:
        notes.append(
            'The advisor has not watched the indexes long enough to call one unused (it needs '
            f"{review.get('idle_days_required')} days): possibly_unused holds only what it could judge.",
        )
    if cut_short and not section:
        notes.append('Some lists are longer than shown: totals counts each; give section to page through one.')
    elif cut_short:
        notes.append('This page holds fewer than limit entries to stay small: next_offset gets the rest.')
    answer.update(
        {
            'schedule': _picked(state.get('schedule'), _SCHEDULE),
            'pending_actions': _strings(state.get('pending_actions')),
            'offset': offset,
            'next_offset': following,
            'link': links.dataset_page(dataset_id) if dataset_id is not None else links.status_page(),
        },
    )
    if notes:
        answer['note'] = ' '.join(notes)
    return answer


# --- the registry ---

_LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 20}
_OFFSET = {'type': 'integer', 'minimum': 0, 'default': 0}

SYSTEM_STATUS = Tool(
    name='system_status',
    description=(
        "For administrators: this server's health, as Settings → System status shows it. database_disk.level is ok; "
        'low (free space below low_below_bytes: the app warns); paused (below ingest_paused_below_bytes: new samples '
        "are refused until space is freed); or unknown (the free space can't be read). Each disk of the image caches "
        'is ok or low. database: whether it answers, its size, and what is pending: pending_migrations are database '
        "migrations this server's code has and the database hasn't run, unknown_migrations ones the database ran that "
        'the code doesn\'t know (the code is older than the database), stale_functions database functions older than '
        "the code's; server.restart_needed means the code on disk is newer than the code running. Pending means "
        'migrations and a restart, never a newer release: nothing checks for one. workers: how many of the server\'s '
        'job runners and of the compute workers sent a heartbeat in the last minute. backups: the backups\' level (ok, '
        'overdue, failed, none, not_configured or unreadable), when the newest verified backup was made and when the '
        'next is due; backup_status has the rest. link opens the page where an administrator acts. It only reads.'
    ),
    input_schema={'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False},
    kind='read',
    level=None,
    routes=(('GET', STATUS),),
    run=system_status,
)

BACKUP_STATUS = Tool(
    name='backup_status',
    description=(
        "For administrators: this server's backups, as Settings → System status → Backups shows them, with the last "
        'ten backup jobs. level is ok; overdue (the newest verified backup is older than the schedule allows); failed '
        '(a backup failed after the newest verified one: last_failure says why); none (no verified backup yet); '
        "unreadable (the backup folder can't be read: error says why); or not_configured (no backup folder is set, so "
        'this server takes no backups through the app, though a deployment may back up another way: note says so). '
        'latest is the newest backup that was verified when it was made (its rows counted again from the file): its '
        'file, when, its size, how long the dump and the check took, and its tables. kept: the verified backups in '
        'the folder. unverified_dumps: dumps without a passed check. schedule: a backup every interval_hours from '
        'start_hour, keeping the newest keep, and when the next is due. running: a backup queued or running now. '
        'recent_runs: the last ten backup jobs, newest first, each with its file and size once it succeeded and its '
        'error when it failed. Errors are cut to 500 characters. link opens the page where an '
        'administrator backs up now or changes the schedule. It only reads.'
    ),
    input_schema={'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False},
    kind='read',
    level=None,
    routes=(('GET', STATUS), ('GET', BACKUP_SCHEDULE), ('GET', JOBS)),
    run=backup_status,
)

LIST_JOBS = Tool(
    name='list_jobs',
    description=(
        "For administrators: background jobs across the whole server, every dataset's and those of no dataset "
        '(backups, cleanups, the index advisor\'s reviews), newest first. state: needs_attention (the default: '
        'failed, queued and running), or one of failed, running, queued, succeeded, cancelled. since_hours is the '
        'window (default 168, a week): the jobs that ended among those created in it, and every queued or running '
        'job, however old. kind: one kind of job (kinds lists the kinds in the window; an unknown kind gives an empty '
        "list). dataset: one dataset's jobs, with the jobs of no dataset that "
        'name it. total counts the jobs that match; counts gives each state\'s count for the same kind, dataset and '
        'window. A page holds at most limit jobs; next_offset (null on the last page) is the offset of the next. Each '
        'job has its kind, state, dataset (id and name), the field of an index build, who started it (a username, or '
        'System), when it was created, started and finished, its progress (done, total and its last message), how '
        'many tries failed of max_failures, and its error, cut to 500 characters. A job\'s messages are kept back '
        "when a dataset it is about (a follower's sync is about its link's mirrors) is closed to the assistant or has "
        'fields whose values are kept from it, or when it belongs to no dataset and is of a kind not known to be '
        "about none: error and the progress message are null and error_withheld says why, and the job's link shows "
        "them to the person. A job's result is never given. link opens the job's dataset or System status, where an "
        'administrator stops, resumes or discards a job. It only reads.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'state': {
                'type': 'string',
                'enum': ['needs_attention', *_STATES],
                'default': 'needs_attention',
                'description': 'needs_attention (failed, queued and running), or one state.',
            },
            'kind': {'type': 'string', 'maxLength': 200, 'description': 'One kind of job, such as backup.'},
            'dataset': {**_DATASET_ID, 'description': "A dataset's id: its jobs, and those that name it."},
            'since_hours': {
                'type': 'integer',
                'minimum': 1,
                'maximum': 8760,
                'default': 168,
                'description': 'The window, in hours back from now (1 to 8760).',
            },
            'limit': {**_LIMIT, 'description': 'How many jobs at most (1 to 50).'},
            'offset': {
                **_OFFSET,
                'description': 'How many matching jobs to skip: the next_offset of the previous page.',
            },
        },
        'required': [],
        'additionalProperties': False,
    },
    kind='read',
    level=None,
    routes=(('GET', JOBS),),
    run=list_jobs,
)

INDEX_ADVICE = Tool(
    name='index_advice',
    description=(
        "For administrators: what the index advisor found at its last review, of the server's own indexes, or of "
        "one dataset's (dataset). It reviews on its schedule or when an administrator asks on the linked page; this "
        'tool never starts one. overlapping: pairs where one index (redundant) is not needed because another (kept) '
        'serves the same searches, by one of three rules: duplicate (the same columns, operators, order and '
        'predicate), operators (the same columns, with an operator class whose operators the other\'s include), or '
        'prefix (a B-tree whose columns lead the other\'s). possibly_unused: indexes no search used for idle_days, or '
        'invalid ones, which nothing can use; the advisor calls an index unused only once it has watched long enough '
        '(enough_history true, after idle_days_required days). suggested (a dataset\'s): fields whose searches are '
        'slower than its indexed fields\', with how much an index would save. needed: indexes that must stay, each '
        'with why (a primary key, the only index of a foreign key). too_new: indexes watched too briefly to judge. '
        'removed: indexes removed through the advisor and not restored. totals counts each list; redundant_bytes is '
        'the space the redundant and possibly unused indexes take. Without section: the first limit of overlapping '
        'and possibly_unused (and suggested); with section: that list a page at a time (offset, next_offset). An '
        "index's definition is null, with definition_withheld true, when it holds a value (a quoted text or a number "
        "the server doesn't write itself, such as a field's value in a partial index's condition) and is not one of "
        "the server's own, or when it is longer than 2,000 characters. This tool removes nothing: an administrator "
        'removes or restores an index on the linked page. It only reads.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'dataset': {**_DATASET_ID, 'description': "A dataset's id: its indexes. Left out, the server's own."},
            'section': {
                'type': 'string',
                'enum': list(SECTIONS),
                'description': 'One list, a page at a time.',
            },
            'limit': {**_LIMIT, 'description': 'How many entries of each list at most (1 to 50).'},
            'offset': {**_OFFSET, 'description': "How many of the section's entries to skip: its next_offset."},
        },
        'required': [],
        'additionalProperties': False,
    },
    kind='read',
    level=None,
    routes=(('GET', SYSTEM_INDEXES), ('GET', DATASET_INDEXES), ('GET', DATASETS)),
    run=index_advice,
)

ADMIN_TOOLS: tuple[Tool, ...] = (SYSTEM_STATUS, BACKUP_STATUS, LIST_JOBS, INDEX_ADVICE)
