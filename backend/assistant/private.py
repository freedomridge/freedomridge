"""`values_private`, kept in one place for every request made for a tool (docs/ASSISTANT.md, "Keeping a field's values
back"): a value of a private field never reaches a tool as itself, and a tool can't turn a token back into one.

inner.request runs every answer of every route a tool calls through `seal_answer`, which replaces each value of the
request's dataset's private fields with a token (`pv1.<...>`): AES-SIV encryption of the value, padded to a fixed
length so a token doesn't tell a value's length, bound to its dataset and field (the associated data), with a random
key the server made for itself and keeps in assistant_settings.seal_key (migration 0036; never derived from a
configured secret, which has a public default). Equal values of one field give equal tokens, so counts beside them say
how often each (unnamed) value occurs and which samples share one, never what it is.

A token opens in exactly one place: the API's filter compiler, as the value of an `=`, `!=`, `in` or `not in` test of
its own field in a request about its own dataset (guard.py, which also refuses every other way of questioning a
private field). Anywhere else a token is the literal string it is, which matches nothing and shows nothing. One sealed
under an earlier key doesn't open: its test matches nothing.

The people's endpoints (GET /api/assistant/views/{handle} and /lists/{handle}/csv, on the working-views branch) open
tokens for the person, who may see the values; no tool may list them, and a request made for a tool is refused there.

Which parts of an answer are values is read from its shape, not from each route, and each route a tool may list
declares that its answer's shape is one this reads (COVERED_ROUTES; inner.request refuses any other):
- a dict that names a field (`field`, `name`, `field_name`): every value under its value keys, counts kept;
- the route's own field (a path parameter, or `field` in the query or body), for the answer as a whole;
- a key that is a private field's name or its alias (`detections_label` for `detections.label`, `@local_x` for
  `@local.x`), anywhere: its whole value (a sample's `metadata` and own fields, a flat row of a scatter);
- a saved filter stage's `description` (the chip's words, `serial = SN-0007`) when its filter tests a private field:
  written again with the field's values kept back.
The filter query's formatting, whose answer is text, writes a private field's values as "(n values kept from the
assistant)" for such a request (filter_query.format_filter with `kept`).
"""

import base64
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Collection, Iterable, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV

from .. import database

PREFIX = 'pv1.'
# A value's JSON is sealed with a 4-byte length in front and zeros after, to a bucket of 32 bytes, or the next power of
# two above it: every value up to 28 bytes (every boolean, number, date and short code) seals to one length, and a
# longer one tells only its power of two.
_BUCKET = 32
_LENGTH = 4
# Keys whose values are values of the field the dict names; their subtrees are sealed, except counts.
VALUE_KEYS = frozenset(
    {
        'value',
        'values',
        'min',
        'max',
        'range',
        'buckets',
        'top',
        'examples',
        'unique_values',
        'key',
        'label',
        'bounds',
        'min_label',
        'max_label',
    },
)
# A private field's order statistics: the key itself says which value is the smallest or the largest, so a token in
# its place would still rank the samples. Dropped, never sealed.
ORDER_KEYS = frozenset({'min', 'max', 'range', 'bounds', 'min_label', 'max_label'})
# Counts, which say how many and never which: kept wherever they are.
COUNT_KEYS = frozenset(
    {
        'count',
        'total',
        'missing',
        'other_count',
        'distinct_total',
        'sample_count',
        'null_count',
        'bins',
        'present',
        'distinct',
        'samples_with_value',
    },
)
# Aggregations whose `value` is a count.
COUNT_OPS = frozenset({'count', 'with_value', 'distinct_count'})
NAME_KEYS = ('field', 'name', 'field_name')
# Every route a tool may list, with the shape of its answer that the walk below covers (inner.request refuses any
# other, and a test plants values for each and reads its answer). A new route is added here with that test.
COVERED_ROUTES: dict[tuple[str, str], str] = {
    ('GET', '/api/datasets'): 'datasets: names, descriptions, levels; no field values',
    ('GET', '/api/datasets/{id}'): "the dataset; its default view's stages (values by the field a filter names)",
    ('GET', '/api/datasets/{id}/schema'): 'one entry per field, by name: min, max, unique values, examples',
    ('GET', '/api/datasets/{id}/field-stats'): 'one entry per field, by field_name: values, top, ranges',
    ('GET', '/api/datasets/{id}/coverage'): 'one entry per field, by name: counts',
    ('GET', '/api/datasets/{id}/concepts'): 'concepts: names, ids, states; no field values',
    ('GET', '/api/datasets/{id}/views'): 'saved views: stages (values by the field a filter names, descriptions)',
    ('POST', '/api/datasets/{id}/aggregate'): 'one result per aggregation, by field: values, counts',
    ('POST', '/api/datasets/{id}/samples/query'): "samples: metadata and own fields by key",
    ('POST', '/api/datasets/{id}/histogram'): "the route's own field: buckets (refused for a private field)",
    ('POST', '/api/datasets/{id}/filter_query/parse'): "the request's own text as filters, by field",
    ('POST', '/api/datasets/{id}/filter_query/format'): 'text, private values written as kept',
    ('GET', '/api/datasets/{id}/concepts/{concept_id}/boundary'): 'sample ids and scores; no field values',
    ('GET', '/api/regions'): "regions: keys, kinds and names; of no dataset, no field values",
    ('POST', '/api/search'): "samples: metadata and own fields by key (the dataset its body's dataset_id names)",
    ('GET', '/api/views/{slug}'): (
        'a saved view: its stages (values by the field a filter names, descriptions), sealed for the dataset the '
        'route resolved'
    ),
}
# Covered routes that name no dataset in their path, and where the dataset whose private fields are sealed comes from
# (every other covered route names it as its `{id}`):
# - 'none': the route answers about no dataset and holds no field's values;
# - 'body': its body's `dataset_id` (request_dataset);
# - 'resolved': the dataset the route itself looked up and checked (check_dataset_access with the request's state),
#   read back after it answered (inner.request); a success that resolved none is refused.
DATASET_FROM: dict[tuple[str, str], str] = {
    ('GET', '/api/regions'): 'none',
    ('POST', '/api/search'): 'body',
    ('GET', '/api/views/{slug}'): 'resolved',
}
# Routes whose answer is built from the request alone. A private field's values in it are the request's own, and are
# dropped, never sealed: a token is made only from what a dataset holds, so a request can't have its own guess turned
# into a token that opens. An error answer is treated the same way, for it may echo the request.
ECHO_ROUTES = frozenset({('POST', '/api/datasets/{id}/filter_query/parse')})

# The routes an administration tool may list (backend/assistant/admin.py), and no other: inner.request refuses it every
# route not here (and every other tool a route not in COVERED_ROUTES), and get_current_user refuses the administrators'
# routes among them to every other tool by path. They answer across datasets and know neither rule above (an
# administration tool's request reads closed datasets too: backend/access.py), and their free text can quote data (a
# failed index build's database error quotes the value it could not read). So the tool copies into its answer only the
# facts it names, and before it reads an answer, inner.request takes out with `keep_back` whatever could hold a private
# field's value or anything of a closed dataset's samples:
# - a job's words (its error, its progress message) unless the job is about datasets that are all open to the assistant
#   and have no private field, or is of a kind known to be about no dataset (`withheld` says why, when there was
#   something to take out). A job is about the dataset it belongs to, the one its arguments name, and, for a follower's
#   sync, every mirror of its link, which it writes;
# - an index's definition when it holds a constant (a quoted text or a number) other than one the server writes (a
#   field's name, a vector index's field, a storage parameter, a field index's dataset, a type's modifier), whatever
#   kind the advisor guessed from its name, unless it is one of the indexes schema.sql ships holding only constants
#   schema.sql's statement holds; or when it is longer than DEFINITION_LIMIT (`definition` null, `definition_withheld`
#   true). The words about such an index (an overlap's reason, why it is needed) write each such constant '…' or ….
JOBS_ALL = '/api/admin/jobs/all'
SYSTEM_INDEX_ADVICE = '/api/admin/index-advisor'
DATASET_INDEX_ADVICE = '/api/datasets/{id}/index-advisor'
SPANNING_ROUTES: dict[tuple[str, str], str] = {
    ('GET', '/api/datasets'): 'every dataset, closed ones too: an administration tool copies only ids and names',
    ('GET', '/api/admin/status'): 'the server: disks, database, workers, backups; of no dataset',
    ('GET', '/api/admin/backups/schedule'): 'the backup schedule; of no dataset',
    ('GET', JOBS_ALL): "jobs of every dataset: a job's words kept back by the dataset it is about",
    (
        'GET',
        SYSTEM_INDEX_ADVICE,
    ): "the server's indexes: a definition holding a value (a quoted text or a number) kept back",
    ('GET', DATASET_INDEX_ADVICE): (
        "one dataset's indexes: a definition holding a value kept back, its review's failure by the dataset"
    ),
}
# Why a job's words were kept back (`withheld`): its dataset is closed to the assistant, has fields marked
# values_private, or no longer exists (what it held can't be known); or it belongs to no dataset and its kind is not one
# known to be about none.
CLOSED, PRIVATE_FIELDS, GONE, UNKNOWN_KIND = 'closed', 'private_fields', 'gone', 'unknown_kind'
# The kinds of job known to be about no dataset when they belong to none (job_service's handlers). A kind belongs here
# only when the job reads no sample's values, or its messages are written by the app from counts and names alone:
# - backup: pg_dump, verified by counting the dump's rows; pg_dump's errors don't quote rows;
# - region_load: reads a boundary file into the region tables;
# - selection_cleanup: deletes expired selections;
# - index_review (the server's own; a dataset's names it in its arguments): reads the catalogs only;
# - index_drop of an index of no dataset (a vector index's names its dataset): DROP INDEX evaluates no row, and its
#   messages name blocking sessions by pid, application, state and age.
# Not index_restore: it builds an index again from its stored definition, reading every row it covers, and a hand-made
# expression index fails quoting a sample's value from any dataset. Any other kind that belongs to no dataset, a kind
# added later included, is kept back until it is named here.
ABOUT_NO_DATASET = frozenset({'backup', 'region_load', 'selection_cleanup', 'index_review', 'index_drop'})
# A follower's sync names only its link, and writes that link's mirror datasets.
FOLLOWER_SYNC = 'follower_sync'
# The longest definition given: one index never fills an answer.
DEFINITION_LIMIT = 2000
# The constants of SQL as PostgreSQL prints a definition: a quoted text ('...' with '' inside, which E'', U&'', B''
# and X'' constants end in too; a dollar-quoted $$...$$, $tag$...$tag$), or a number, printed bare (4417, 9000.5,
# ARRAY[12, 31, 407]). Strings, dates, intervals, arrays and jsonb are quoted, so these two are every constant, but for
# the words true, false and null. A "quoted identifier" is a name, not a constant, and is skipped (a collation such as
# "C.UTF-8"). A number is found only where it stands alone: digits inside a name (idx_samples_meta_7_crop,
# embeddings_d7, int4_ops, metadata_instant_v1) and a $1 are not numbers; a minus sign before one leaves it a number.
_TOKENS = re.compile(r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|\$([A-Za-z_][A-Za-z0-9_]*)?\$.*?\$\1\$""", re.S)
_NUMBER = re.compile(r'(?<![\w.$])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\w.])')
# What stands before a quoted text the server writes itself, each time in the form PostgreSQL prints it (::text after a
# literal): a field's name (metadata ->> 'name', metadata -> 'name', metadata ? 'name'; not local_metadata, not
# E'...'), a vector index's field (WHERE (field_name = 'name'::text)), or a storage parameter's value inside WITH (...)
# (WITH (m='16', ef_construction='64')).
_SERVER_WRITTEN_BEFORE = (
    re.compile(r'(?<![\w."$])metadata\s*(?:->>|->|\?)\s*$'),
    re.compile(r'(?<![\w."$])field_name\s*=\s*$'),
    re.compile(r"\bWITH \((?:\w+=(?:'(?:[^']|'')*'|[\w.+-]+), )*\w+=$"),
)
# What stands before a number the server writes itself: a field index's WHERE (dataset_id = N), a storage parameter
# inside WITH (...), a type's modifier after :: (numeric(10,2), vector(4), public.vector(4)). Of the type names
# PostgreSQL prints with a modifier, only these have words between the name's first word and the parenthesis:
# character varying(255), bit varying(8), and an interval's fields (interval second(3), interval day to second(3));
# timestamp(3) without time zone and time(3) with time zone print the modifier straight after the first word, and
# double precision takes none. Any other word there (::boolean AND f(4417)) is not a type's.
_SERVER_NUMBER_BEFORE = (
    re.compile(r'(?<![\w."$])dataset_id\s*=\s*$'),
    _SERVER_WRITTEN_BEFORE[2],
    re.compile(
        r'::[A-Za-z_][\w."]*(?: (?:varying|year|month|day|hour|minute|second|to))*\(\s*(?:\d+\s*,\s*)*$',
    ),
)
KEPT_TEXT, KEPT_NUMBER = "'…'", '…'


def _held(quoted: str) -> str:
    """What a quoted text holds (a JSON object or list written one way: PostgreSQL prints '{"a": 1}' for '{"a":1}')."""
    value = quoted[1:-1].replace("''", "'") if quoted.startswith("'") else re.sub(r'^\$\w*\$|\$\w*\$$', '', quoted)
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    return json.dumps(parsed, sort_keys=True) if isinstance(parsed, (dict, list)) else value


def _stray(gap: str) -> Optional[int]:
    return min((at for at in (gap.find("'"), gap.find('"'), gap.find('$$')) if at >= 0), default=None)


def _constants(text: str) -> tuple[list[tuple[int, int, str, bool]], Optional[int]]:
    """The constants of SQL `text`, each (start, end, what it holds, whether the server writes it there); and where a
    quote that never closes starts (None when every quote closes)."""
    found: list[tuple[int, int, str, bool]] = []
    end = 0

    def numbers(stop: int) -> None:
        for number in _NUMBER.finditer(text, end, stop):
            own = any(before.search(text[: number.start()]) for before in _SERVER_NUMBER_BEFORE)
            found.append((number.start(), number.end(), number.group(0), own))

    for token in _TOKENS.finditer(text):
        stray = _stray(text[end : token.start()])
        if stray is not None:
            numbers(end + stray)
            return found, end + stray
        numbers(token.start())
        if not token.group(0).startswith('"'):
            own = any(before.search(text[: token.start()]) for before in _SERVER_WRITTEN_BEFORE)
            found.append((token.start(), token.end(), _held(token.group(0)), own))
        end = token.end()
    stray = _stray(text[end:])
    numbers(len(text) if stray is None else end + stray)
    return found, None if stray is None else end + stray


def definition_kept(definition: str) -> bool:
    """Whether an index's SQL definition holds a constant (a quoted text or a number) other than one the server writes
    itself (a field's name, a vector index's field, a storage parameter, a field index's dataset, a type's modifier),
    or a quote that never closes, so it is kept from an administration tool."""
    found, stray = _constants(definition)
    return stray is not None or any(not own for *_, own in found)


def quoted_kept(text: str) -> str:
    """`text` (words that quote SQL) with each constant other than one the server writes itself written '…' (a quoted
    text) or … (a number), and a quote that never closes written '…' to the end."""
    found, stray = _constants(text)
    if stray is not None:
        text = text[:stray] + KEPT_TEXT
    for start, end, _, own in reversed(found):
        if not own:
            text = text[:start] + (KEPT_TEXT if text[start] in "'$" else KEPT_NUMBER) + text[end:]
    return text


def _shipped(entry: dict[str, Any], shipped: dict[str, tuple[str, set[str]]]) -> bool:
    """Whether an index is one schema.sql ships: its name on the same table, holding no constant of its own (beyond
    those the server writes) that its statement in schema.sql doesn't hold."""
    name = entry.get('name')
    known = shipped.get(name) if isinstance(name, str) else None
    if known is None or known[0] != entry.get('table'):
        return False
    found, stray = _constants(entry['definition'])
    return stray is None and {value for _, _, value, own in found if not own} <= known[1]


def _definition_withheld(entry: dict[str, Any], shipped: dict[str, tuple[str, set[str]]]) -> bool:
    """Whether an index entry's definition is kept back: longer than DEFINITION_LIMIT; else, unless it is one of the
    indexes schema.sql ships (_shipped: their quoted texts are the app's own constants), when it quotes a text the
    server doesn't write itself. The advisor's kind is a guess from the name: never trusted."""
    definition = entry['definition']
    if len(definition) > DEFINITION_LIMIT:
        return True
    return not _shipped(entry, shipped) and definition_kept(definition)


def _index_words_kept(node: Any, shipped: dict[str, tuple[str, set[str]]]) -> Any:
    """An index advice answer with each index's definition kept back as _definition_withheld says, and the words about
    such an index (an overlap's reason, why it is needed) with each quoted text written '…'. `shipped`: schema.sql's
    indexes, name -> (table, what its statement quotes)."""
    if isinstance(node, list):
        return [_index_words_kept(item, shipped) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: _index_words_kept(value, shipped) for key, value in node.items()}
    withheld = False
    if isinstance(node.get('definition'), str):
        withheld = _definition_withheld(node, shipped)
        out['definition'] = None if withheld else node['definition']
        out['definition_withheld'] = withheld
    # Only an index entry's own words (why it is needed) and an overlap's reason, which names both indexes' columns,
    # quote an index: never a list of entries that has the same name (a system review's `needed`). A side without a
    # marked definition counts as kept back.
    if 'redundant' in node and 'kept' in node:
        quoting = any(_dict_of(out.get(side)).get('definition_withheld') is not False for side in ('redundant', 'kept'))
    else:
        quoting = withheld
    if quoting and isinstance(out.get('reason'), str):
        out['reason'] = quoted_kept(out['reason'])
    if quoting and isinstance(out.get('needed'), list):
        out['needed'] = [quoted_kept(item) if isinstance(item, str) else item for item in out['needed']]
    return out


def _dict_of(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _job_reason(job: dict[str, Any], reasons: dict[int, str], syncs: dict[str, Optional[str]]) -> Optional[str]:
    """Why a job's words are kept back, or None: by the dataset it is about (it belongs to it, or its arguments name
    it); for a follower's sync, by its link's mirrors (`syncs`, GONE when unknown); for any other job of no dataset,
    by its kind (ABOUT_NO_DATASET, else UNKNOWN_KIND)."""
    about = job.get('dataset_id')
    if type(about) is int:  # a dataset's id (never True or False)
        return reasons.get(about)
    if job.get('kind') == FOLLOWER_SYNC:
        return syncs.get(str(job.get('id')), GONE)
    return None if job.get('kind') in ABOUT_NO_DATASET else UNKNOWN_KIND


def _worst(found: Iterable[Optional[str]]) -> Optional[str]:
    """The reason that says most, of a job about several datasets: closed, then private fields, then gone."""
    present = set(found)
    return next((reason for reason in (CLOSED, PRIVATE_FIELDS, GONE) if reason in present), None)


def _job_kept(job: Any, reason: Optional[str]) -> Any:
    """A job with its words taken out when `reason` keeps them back; `withheld` says why, when there were any."""
    if not isinstance(job, dict):
        return job
    progress = job.get('progress')
    message = progress.get('message') if isinstance(progress, dict) else None
    if reason is None:
        return {**job, 'withheld': None}
    return {
        **job,
        'error': None,
        'progress': {**progress, 'message': None} if isinstance(progress, dict) else progress,
        'withheld': reason if job.get('error') is not None or message is not None else None,
    }


async def sync_mirrors(job_ids: Collection[str], source: Any = None) -> dict[str, Optional[list[int]]]:
    """For each follower's sync job, the mirror datasets of its link on this server, which it writes (mirrors.link_id);
    None when its link no longer exists, or the job doesn't name one."""
    known = [uuid.UUID(job_id) for job_id in job_ids if _is_uuid(job_id)]
    if not known:
        return {}
    rows = await (source if source is not None else database.db).fetch(
        """SELECT j.id::text AS job, l.id IS NOT NULL AS linked,
                  array_remove(array_agg(m.dataset_id ORDER BY m.dataset_id), NULL) AS mirrors
           FROM jobs j
           LEFT JOIN primary_links l ON l.id::text = j.args->>'link_id'
           LEFT JOIN mirrors m ON m.link_id = l.id
           WHERE j.id = ANY($1::uuid[])
           GROUP BY j.id, l.id""",
        known,
    )
    return {row['job']: list(row['mirrors']) if row['linked'] else None for row in rows}


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


async def kept_datasets(dataset_ids: Collection[int], source: Any = None) -> dict[int, str]:
    """For each of these datasets whose jobs' words an administration tool may not read, why (CLOSED, PRIVATE_FIELDS
    or GONE); a dataset open to the assistant with no private field is left out."""
    if not dataset_ids:
        return {}
    rows = await (source if source is not None else database.db).fetch(
        'SELECT d.id, d.assistant_closed, EXISTS (SELECT 1 FROM dataset_fields f WHERE f.dataset_id = d.id '
        'AND f.values_private) AS private FROM datasets d WHERE d.id = ANY($1::int[])',
        sorted(dataset_ids),
    )
    found = {row['id']: row for row in rows}
    reasons = {}
    for dataset_id in dataset_ids:
        row = found.get(dataset_id)
        if row is None:
            reasons[dataset_id] = GONE
        elif row['assistant_closed']:
            reasons[dataset_id] = CLOSED
        elif row['private']:
            reasons[dataset_id] = PRIVATE_FIELDS
    return reasons


async def keep_back(method: str, template: str, path_params: dict[str, Any], answer: Any) -> Any:
    """A SPANNING_ROUTES answer with what could hold a private value or a closed dataset's samples taken out (see
    SPANNING_ROUTES). inner.request applies it to every such answer, before the administration tool reads it."""
    if (method, template) not in SPANNING_ROUTES or not isinstance(answer, dict):
        return answer
    if template == JOBS_ALL and isinstance(answer.get('jobs'), list):
        jobs = [job for job in answer['jobs'] if isinstance(job, dict)]
        syncing = [
            job['id']
            for job in jobs
            if job.get('kind') == FOLLOWER_SYNC and job.get('dataset_id') is None and isinstance(job.get('id'), str)
        ]
        mirrors = await sync_mirrors(syncing)
        named = {job['dataset_id'] for job in jobs if type(job.get('dataset_id')) is int}
        named |= {dataset_id for found in mirrors.values() for dataset_id in found or ()}
        reasons = await kept_datasets(named)
        # No mirror left reads as gone: a mirror's row goes with its dataset, so a past sync may quote a deleted one's.
        syncs = {
            job_id: GONE if not found else _worst(reasons.get(dataset_id) for dataset_id in found)
            for job_id, found in mirrors.items()
        }
        return {
            **answer,
            'jobs': [
                _job_kept(job, _job_reason(job, reasons, syncs) if isinstance(job, dict) else None)
                for job in answer['jobs']
            ],
        }
    if template in (SYSTEM_INDEX_ADVICE, DATASET_INDEX_ADVICE):
        from ..index_advisor import core_index_statements, core_indexes

        statements = core_index_statements()
        shipped = {
            name: (table, {value for _, _, value, _ in _constants(statements.get(name, ''))[0]})
            for name, table in core_indexes().items()
        }
        answer = _index_words_kept(answer, shipped)
        dataset_id = request_dataset(path_params, None)
        failure = answer.get('last_failure')
        # The system's review is about no dataset; a dataset's review is about that dataset.
        if dataset_id is not None and isinstance(failure, dict):
            reason = (await kept_datasets({dataset_id})).get(dataset_id)
            if reason is not None:
                had = failure.get('message') is not None
                answer['last_failure'] = {**failure, 'message': None, 'withheld': reason if had else None}
        return answer
    return answer


@dataclass(frozen=True)
class Seal:
    """The server's key, and the dataset a request is about: what binds its tokens."""

    key: Optional[bytes]
    dataset_id: int

    def _data(self, field: str) -> list[bytes]:
        return [b'pv1', str(self.dataset_id).encode(), field.encode()]

    def seal(self, value: Any, field: str) -> Optional[str]:
        """`value` of `field` as a token. None without a key: the value is dropped, never shown."""
        if self.key is None:
            return None
        sealed = AESSIV(self.key).encrypt(_pad(json.dumps(value, sort_keys=True).encode()), self._data(field))
        return PREFIX + base64.urlsafe_b64encode(sealed).decode().rstrip('=')

    def open(self, token: Any, field: str) -> tuple[bool, Any]:
        """(True, the value) when `token` was sealed for `field` of this dataset with this key; (False, None) else."""
        if self.key is None or not is_sealed(token):
            return False, None
        raw = token[len(PREFIX) :]
        try:
            data = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
            return True, json.loads(_unpad(AESSIV(self.key).decrypt(data, self._data(field))))
        except (InvalidTag, ValueError):
            return False, None


def _pad(data: bytes) -> bytes:
    size, bucket = _LENGTH + len(data), _BUCKET
    while bucket < size:
        bucket *= 2
    return len(data).to_bytes(_LENGTH, 'big') + data + bytes(bucket - size)


def _unpad(data: bytes) -> bytes:
    size = int.from_bytes(data[:_LENGTH], 'big')
    if _LENGTH + size > len(data):
        raise ValueError('not a padded value')
    return data[_LENGTH : _LENGTH + size]


def is_sealed(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def covered(name: Any, private: Collection[str]) -> bool:
    """Whether field `name` holds values of a private field: the field itself, or a path inside one (a drawing's
    label list `detections.label`, a mirror's own `@local.` field named by its key)."""
    if not isinstance(name, str) or not private:
        return False
    parts = name.split('.')
    return any('.'.join(parts[:end]) in private for end in range(1, len(parts) + 1))


def _seal_all(node: Any, seal: Optional[Seal], name: str) -> Any:
    """Every value under `node` sealed for field `name`, counts kept, order statistics dropped, and every list put in
    an order that says nothing of the values: by count (most first) where its items have one, then by token. With no
    seal (an answer that may echo the request), the values are dropped instead."""
    if isinstance(node, dict):
        return {
            key: value if key in COUNT_KEYS else _seal_all(value, seal, name)
            for key, value in node.items()
            if key not in ORDER_KEYS
        }
    if isinstance(node, list):
        return sorted((_seal_all(value, seal, name) for value in node), key=_unranked)
    if node is None or seal is None:
        return None
    return seal.seal(node, name)


def _unranked(item: Any) -> tuple[float, str]:
    """The order of a sealed list: its items' counts, most first, then their tokens' text."""
    count = item.get('count') if isinstance(item, dict) else None
    rest = {key: value for key, value in item.items() if key not in COUNT_KEYS} if isinstance(item, dict) else item
    return (-count if isinstance(count, (int, float)) and not isinstance(count, bool) else 0, json.dumps(rest))


def _keyed(key: str, private: Collection[str]) -> Optional[str]:
    """The private field whose values a dict key holds: the key is its name, a path inside it, or its alias (`.`
    written `_`: `detections_label`, `@local_x`). Never a count's key or a key that names a field."""
    if key in COUNT_KEYS or key in NAME_KEYS:
        return None
    for name in (key, key.replace('_', '.')):
        if covered(name, private):
            return name
    return None


def _fields_of(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        if isinstance(node.get('field'), str):
            yield node['field']
        for value in node.values():
            yield from _fields_of(value)
    elif isinstance(node, list):
        for value in node:
            yield from _fields_of(value)


def _described(stage: dict[str, Any], private: Collection[str]) -> Optional[str]:
    """A saved stage's description, written again from its filter with private values kept back (None when the filter
    language can't write it)."""
    from ..filter_query import format_filter

    try:
        return format_filter(stage['filter'], kept=private)
    except Exception:  # noqa: BLE001 - a stage the language can't write has no description here
        return None


def _walk(node: Any, private: Collection[str], seal: Optional[Seal], about: Optional[str]) -> Any:
    if isinstance(node, list):
        return [_walk(value, private, seal, about) for value in node]
    if not isinstance(node, dict):
        return node
    named = next((node[key] for key in NAME_KEYS if isinstance(node.get(key), str)), None)
    here = about if about is not None else named if covered(named, private) else None
    out: dict[str, Any] = {}
    for key, value in node.items():
        keyed = _keyed(key, private)
        if key in ('metadata', 'local_metadata') and isinstance(value, dict):
            local = '@local.' if key == 'local_metadata' else ''
            out[key] = {
                name: _seal_all(item, seal, local + name)
                if covered(local + name, private)
                else _walk(item, private, seal, None)
                for name, item in value.items()
            }
        elif here is not None and key in ORDER_KEYS:
            continue
        elif here is not None and key in VALUE_KEYS and not (key == 'value' and node.get('op') in COUNT_OPS):
            out[key] = _seal_all(value, seal, here)
        elif keyed is not None:
            out[key] = _seal_all(value, seal, keyed)
        elif (
            key == 'description'
            and isinstance(node.get('filter'), dict)
            and any(covered(name, private) for name in _fields_of(node['filter']))
        ):
            out[key] = _described(node, private)
        else:
            out[key] = _walk(value, private, seal, None)
    return out


def seal_answer(
    answer: Any,
    private: Collection[str],
    seal: Seal,
    field: Optional[str] = None,
    *,
    echo: bool = False,
) -> Any:
    """`answer` with every value of a private field sealed. `field`: the field the route answers about as a whole
    (its path parameter, or `field` in its query or body). `echo`: the answer may hold the request's own values (an
    ECHO_ROUTES route's, or an error), which are dropped instead."""
    if not private:
        return answer
    return _walk(answer, private, None if echo else seal, field if covered(field, private) else None)


async def private_fields(dataset_id: int, source: Any = None) -> frozenset[str]:
    """The dataset's fields marked values_private (dataset_fields; a built-in field's row has origin 'system')."""
    rows = await (source if source is not None else database.db).fetch(
        'SELECT field_name FROM dataset_fields WHERE dataset_id = $1 AND values_private',
        dataset_id,
    )
    return frozenset(row['field_name'] for row in rows)


async def server_key(source: Any = None) -> Optional[bytes]:
    """The key tokens are sealed with (assistant_settings.seal_key, random, made by the server); None without one."""
    key = await (source if source is not None else database.db).fetchval(
        'SELECT seal_key FROM assistant_settings WHERE singleton',
    )
    return bytes(key) if key else None


def answer_field(path_params: dict[str, Any], query: Optional[dict[str, Any]], body: Any) -> Optional[str]:
    """The field a route answers about as a whole, from its request: a path parameter, else `field` in the query or
    the body."""
    for key in ('field_name', 'field'):
        if isinstance(path_params.get(key), str):
            return path_params[key]
    for source in (query or {}, body if isinstance(body, dict) else {}):
        if isinstance(source.get('field'), str):
            return source['field']
    return None


def request_dataset(path_params: dict[str, Any], body: Any) -> Optional[int]:
    """The dataset a request is about: its path's, else its body's `dataset_id`."""
    candidates = [path_params.get('id'), path_params.get('dataset_id')]
    if isinstance(body, dict):
        candidates.append(body.get('dataset_id'))
    for value in candidates:
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def open_filters(node: Any, seal: Seal) -> Any:
    """For the person (the people's endpoints, never a tool's request): `node`, a filter tree a working view keeps,
    with each token that is a test's value for its own field opened. One that doesn't open stays as it is."""
    if isinstance(node, list):
        return [open_filters(item, seal) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: open_filters(value, seal) if key != 'value' else value for key, value in node.items()}
    name, value = node.get('field'), node.get('value')
    if isinstance(name, str) and 'value' in node:

        def opened(item: Any) -> Any:
            ok, plain = seal.open(item, name)
            return plain if ok else item

        out['value'] = [opened(item) for item in value] if isinstance(value, list) else opened(value)
        if isinstance(value, dict):
            out['value'] = open_filters(value, seal)  # a cross-grain operator's sub-filter
    return out


def sealed_in(node: Any) -> Iterable[str]:
    """The tokens in `node`, for tests."""
    if is_sealed(node):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from sealed_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from sealed_in(value)
