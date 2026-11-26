"""A private field's values appear in no tool's answer (docs/ASSISTANT.md, "Keeping a field's values back").

Generated from `assistant.tools()`: every tool is called with every argument it takes, filled from the dataset below
(each of its fields, private and not; the handles earlier answers gave), and the whole of every answer, results and
errors alike, is searched for values planted in private fields:
- text,
- a number,
- an element of a list kept in the samples' metadata,
- a tag (the built-in tags marked private),
- a drawing's label (its label list is the field's values),
- a saved view's filter and the dataset's default view's filter, saved as the app saves them (the chip's words name
  the value).
The dataset also has vectors and a concept scoring its samples, and text search's model is stood in for, so the tools
that search, preview a concept and open a saved view run for real. A tool added later is called the same way without
anyone editing this test. An argument this test has no example for fails it, naming the argument: add an example,
never skip the tool.

The filter text given to the tools names private fields but never a planted value. A private field can't be
questioned either (guard.py): every way a request can test, sort, bin or summarise one with a value it wrote is
refused, the same way whatever the value, and a halving attack learns nothing.
"""

import itertools
import json
import os
import re
from typing import Any, Iterable

import pytest
from fastapi import HTTPException

from backend import assistant
from backend.assistant import guard, private, rule
from backend.routers import datasets
from backend.routers import search as search_router
from backend.tests import test_assistant_core
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_assistant_core import ADMIN, TRIALS, World
from backend.tests.test_field_registry_postgres import USER

registry_database = test_assistant_core.registry_database
world = test_assistant_core.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

TEXT = ['ZQXSECRET-ALPHA', 'ZQXSECRET-BRAVO']
NUMBERS = [987654.25, 987655.25]
ELEMENT = 'ZQXELEMENT'
TAG = 'ZQXTAG'
LABEL = 'ZQXLABEL'
SAVED = 'ZQXSAVEDVIEW'
DEFAULT = 'ZQXDEFAULTVIEW'
PLANTED = [*TEXT, *(json.dumps(number) for number in NUMBERS), ELEMENT, TAG, LABEL, SAVED, DEFAULT]
PRIVATE = ('secret_code', 'secret_number', 'secret_list', 'detections', 'tags')
# Filter text a model could write: it names private fields, never a planted value.
FILTERS = [
    '',
    'secret_code exists',
    'secret_number > 0 AND crop = "wheat"',
    'score >= 1',
    'NOT (secret_code exists)',
    'tags exists',
]
CONCEPT_ID, CONCEPT = 4242, 'scrap'  # a concept scoring the samples


@pytest.fixture(autouse=True)
def _text_model(monkeypatch):
    """Text search's model, stood in for where main's own text-search tests stand in for it (4-d vectors here)."""

    async def stand_in(prompt: str, registered_model: Any = None) -> list[float]:
        return [0.0, 1.0, 0.0, 0.0]

    monkeypatch.setattr(search_router, 'encode_text', stand_in)


def _samples() -> list[dict[str, Any]]:
    box = [0.1, 0.1, 0.2, 0.2]
    return [
        {
            'filepath': f'plot-{n}.jpg',
            'tags': [TAG, 'kept'],
            'metadata': {
                'secret_code': TEXT[n % 2],
                'secret_number': NUMBERS[n % 2],
                'secret_list': [ELEMENT, 'common'],
                'detections': [{'label': LABEL, 'bounding_box': box}],
                'crop': 'wheat' if n % 3 else 'barley',
                'score': n,
            },
        }
        for n in range(6)
    ]


async def _plant(w: World) -> list[str]:
    """The dataset: planted values, five private fields, a saved view and a default view, counted statistics."""
    await datasets.add_samples(TRIALS, {'samples': _samples()}, user=USER)
    for field in PRIVATE:
        marked = await w.api(
            ADMIN,
            'PATCH',
            f'/api/datasets/{TRIALS}/fields/{field}/description',
            json={'values_private': True},
        )
        assert marked.status_code == 200, marked.text
    for name, value in (('saved-secret', SAVED), ('default-secret', DEFAULT)):
        # As the app saves a chip: its words are describeFilter's (frontend/lib/filter-utils.ts), and the view keeps
        # its stages and their compiled filter (frontend/components/ViewManager.tsx).
        stage = {
            'type': 'filter',
            'description': f'secret_code = {value}',
            'filter': {'field': 'secret_code', 'operator': '=', 'value': value},
        }
        content = {'filters': {'logic': 'AND', 'filters': [stage['filter']]}, 'viewStages': [stage]}
        made = await w.api(
            ADMIN,
            'POST',
            f'/api/datasets/{TRIALS}/views',
            json={'name': name, 'content': content, 'is_public': True},
        )
        assert made.status_code == 200, made.text
        # A slug of its own name, so a request can name it (ROUTE_EXAMPLES).
        await w.database.execute('UPDATE views SET slug = $1 WHERE id = $2', name, made.json()['id'])
        if value == DEFAULT:
            set_default = await w.api(
                ADMIN,
                'PATCH',
                f'/api/datasets/{TRIALS}',
                json={'default_view_id': made.json()['id']},
            )
            assert set_default.status_code == 200, set_default.text
    # Vectors, and a concept scoring the samples: what the search and the concept tools read.
    ids = [row['id'] for row in await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1', TRIALS)]
    vectors = [
        {'sample_id': sample_id, 'field_name': 'clip', 'vector': [1.0, k / 10, 0.5, 0.0]}
        for k, sample_id in enumerate(ids)
    ]
    stored = await w.api(ADMIN, 'POST', f'/api/datasets/{TRIALS}/embeddings', json=vectors)
    assert stored.is_success, stored.text
    await w.database.execute(
        'INSERT INTO embedding_concepts (id, dataset_id, name, embedding_field, kind, weight, threshold) '
        "VALUES ($1, $2, $3, 'clip', 'seed', '[1,0,0,0]', 0.5)",
        CONCEPT_ID,
        TRIALS,
        CONCEPT,
    )
    await w.database.execute(
        'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) '
        'SELECT $1, unnest($2::bigint[]), unnest($3::float8[])',
        CONCEPT_ID,
        ids,
        [k / len(ids) for k in range(len(ids))],
    )
    await run_refresh(w.database, TRIALS)
    # Samples' own fields, where a setting lets the assistant read them (a server without it answers 422).
    await w.api(ADMIN, 'PATCH', '/api/admin/assistant', json={'sample_fields': True})
    schema = (await w.api(ADMIN, 'GET', f'/api/datasets/{TRIALS}/schema')).json()
    return [field['name'] for field in schema]


class Examples:
    """Arguments for every tool, by the argument's name: the dataset's, and the handles earlier answers gave."""

    def __init__(self, fields: list[str]):
        self.fields = fields
        self.views: list[str] = []
        self.lists: list[str] = []
        self.slugs: list[str] = []
        self.samples: list[int] = []

    def learn(self, answer: Any) -> None:
        text = json.dumps(answer)
        for handle in re.findall(r'"(wv_[A-Za-z0-9_-]+)"', text):
            if handle not in self.views:
                self.views.append(handle)
        for handle in re.findall(r'"(vl_[A-Za-z0-9_-]+)"', text):
            if handle not in self.lists:
                self.lists.append(handle)
        for slug in re.findall(r'"slug": "([^"]+)"', text):  # a saved view's, from list_saved_views
            if slug not in self.slugs:
                self.slugs.append(slug)
        for sample in re.findall(r'\?sample=(\d+)"', text):  # a sample's link
            if int(sample) not in self.samples:
                self.samples.append(int(sample))

    def of(self, name: str, spec: dict[str, Any]) -> list[Any]:
        lists = [{'field': field, 'list': handle} for handle in self.lists[-3:] for field in PRIVATE[:2] + ('crop',)]
        known: dict[str, list[Any]] = {
            'dataset': [TRIALS],
            'view': self.views[-4:],
            'field': self.fields,
            'fields': [self.fields],
            'filter': FILTERS,
            'add': FILTERS[1:],
            'search': ['ZQX', 'zqx', 'e'],
            'remove': [[1]],
            'negate': [[1], [2]],
            'in_list': lists,
            'not_in_list': lists,
            'sort_by': ['secret_number', 'score'],
            'descending': [True, False],
            'limit': [spec.get('maximum', 50)],
            'offset': [0, 1],
            # The finding tools': a place, words to search for, a concept, a cut, samples, a saved view.
            'text': ['Kansas', 'ZQX', 'e'],
            'concept': [CONCEPT, 'ZQX'],
            'threshold': [0.5, 0.2],
            'sample_ids': [self.samples[:3] or [1]],
            'slug': [*self.slugs[-3:], 'no-such-view'],
        }
        if name not in known:
            pytest.fail(f'No example for the argument {name!r}: add some to Examples.of in this test')
        return known[name]


def _argument_sets(tool: Any, examples: Examples) -> list[dict[str, Any]]:
    """Each required argument with each of its examples, and on top of each, each optional argument with each of
    its examples. A tool that reads a view or a whole dataset gets each in turn."""
    properties: dict[str, Any] = tool.input_schema.get('properties', {})
    required = list(tool.input_schema.get('required', []))
    bases: list[dict[str, Any]] = [
        dict(zip(required, values)) for values in itertools.product(*(examples.of(n, properties[n]) for n in required))
    ]
    if 'view' in properties and 'dataset' in properties and not {'view', 'dataset'} & set(required):
        bases = [
            {**base, **target}
            for base in bases
            for target in [{'dataset': TRIALS}, *({'view': v} for v in examples.of('view', {}))]
        ]
    sets = list(bases)
    for name, spec in properties.items():
        if name in required or (name in ('view', 'dataset') and 'view' in properties and 'dataset' in properties):
            continue
        for value in examples.of(name, spec):
            sets.extend({**base, name: value} for base in bases[:6])
    return sets


def _leaks(answer: Any) -> list[str]:
    text = json.dumps(answer)
    return [value for value in PLANTED if value in text]


def test_no_tool_answers_a_private_fields_values(world):
    async def scenario(w: World):
        await w.switch_on(power_user=True)
        fields = await _plant(w)
        assert set(PRIVATE) <= set(fields), fields
        examples = Examples(fields)
        calls = 0
        answered: set[str] = set()  # the tools that answered at least once (not only with errors)
        for _ in range(2):  # the second round has the handles the first gave
            for tool in assistant.tools():
                for arguments in _argument_sets(tool, examples):
                    answer = await assistant.call_tool(user_id=ADMIN, door='mcp', name=tool.name, arguments=arguments)
                    calls += 1
                    leaked = _leaks(answer)
                    assert not leaked, f'{tool.name}({arguments}) answered {leaked}: {json.dumps(answer)[:2000]}'
                    examples.learn(answer)
                    if answer['ok']:
                        answered.add(tool.name)
        assert calls > 20, calls
        assert answered == {tool.name for tool in assistant.tools()}, 'every tool ran for real at least once'

    world(scenario)


KEY = bytes(range(64))


def test_sealing_finds_values_in_every_shape_an_answer_has():
    kept = frozenset({'serial', 'detections', 'tags', '@local.x'})
    seal = private.Seal(KEY, 7)
    stage = {
        'type': 'filter',
        'description': 'serial = SN-3',
        'filter': {'field': 'serial', 'operator': '=', 'value': 'SN-3'},
    }
    answer = {
        'fields': [
            {'name': 'serial', 'values': ['SN-1'], 'min': 3, 'values_complete': True, 'sample_count': 4},
            {'name': 'plain', 'values': ['ok']},
            {'name': 'detections.label', 'values': ['cat']},
        ],
        'results': [
            {'op': 'count_values', 'field': 'serial', 'value': [{'value': 'SN-1', 'count': 2}]},
            {'op': 'distinct_count', 'field': 'serial', 'value': 7},
        ],
        'items': [{'id': 1, 'tags': ['t'], 'filepath': 'a.jpg', 'metadata': {'serial': 'SN-2', 'plain': 'ok'}}],
        'default_view': {'content': {'stages': [stage]}},
        # Flat rows keyed by a field's name or its alias (the embeddings scatter's include_fields and colour-by).
        'points': [{'id': 1, 'x': 0.5, 'serial': 'SN-5', 'detections_label': ['dog'], '@local_x': 'LX', 'crop': 'w'}],
        'by_field': {'serial': {'top': ['SN-6'], 'count': 3}},
    }
    sealed = private.seal_answer(answer, kept, seal)
    text = json.dumps(sealed)
    for value in ('SN-1', 'SN-2', 'SN-3', 'SN-5', 'SN-6', '"cat"', '"dog"', '"t"', 'LX'):
        assert value not in text, value
    assert sealed['results'][1]['value'] == 7 and sealed['fields'][0]['sample_count'] == 4
    assert sealed['fields'][1]['values'] == ['ok'] and sealed['items'][0]['metadata']['plain'] == 'ok'
    assert (
        sealed['points'][0]['crop'] == 'w'
        and sealed['points'][0]['x'] == 0.5
        and sealed['by_field']['serial']['count'] == 3
    )
    # A saved stage's words, written again with the values kept back.
    assert sealed['default_view']['content']['stages'][0]['description'] == 'serial = (a value kept from the assistant)'
    token = sealed['items'][0]['metadata']['serial']
    assert private.is_sealed(token) and seal.open(token, 'serial') == (True, 'SN-2')
    assert seal.seal('SN-2', 'serial') == token  # equal values, equal tokens
    # Bound to its field and dataset: anywhere else it doesn't open.
    assert seal.open(token, 'plain') == (False, None)
    assert private.Seal(KEY, 8).open(token, 'serial') == (False, None)
    assert private.Seal(bytes(64), 7).open(token, 'serial') == (False, None)  # another key: as after a new one
    # The route's own field: an autocomplete or a histogram answers about one field as a whole.
    about = private.seal_answer({'values': [{'value': 'SN-4', 'count': 1}], 'exhaustive': True}, kept, seal, 'serial')
    assert 'SN-4' not in json.dumps(about) and about['values'][0]['count'] == 1
    # A real value that looks like a token is sealed as any other.
    assert seal.open(
        private.seal_answer({'metadata': {'serial': 'pv1.abc'}}, kept, seal)['metadata']['serial'],
        'serial',
    ) == (True, 'pv1.abc')


def test_sealed_values_come_unranked():
    """A private field's order statistics are dropped, not sealed; every sealed list is in an order that says nothing
    of the values: by count, most first, then by token."""
    seal = private.Seal(KEY, 7)
    values = [5, 1, 9, 3, 7, 2, 8, 4, 6]
    answer = {
        'fields': [{'name': 'salary', 'min': 1, 'max': 9, 'range': [1, 9], 'unique_values': sorted(values)}],
        'results': [
            {'op': 'values', 'field': 'salary', 'value': sorted(values)},
            {'op': 'count_values', 'field': 'salary', 'value': [{'value': v, 'count': 1 + v % 2} for v in values]},
        ],
        'by_field': {'salary': {'min': 1, 'max_label': '9', 'top': sorted(values)}},
    }
    sealed = private.seal_answer(answer, frozenset({'salary'}), seal)
    entry = sealed['fields'][0]
    assert not set(private.ORDER_KEYS) & set(entry) and not set(private.ORDER_KEYS) & set(sealed['by_field']['salary'])
    for tokens in (entry['unique_values'], sealed['results'][0]['value'], sealed['by_field']['salary']['top']):
        assert tokens == sorted(tokens) and len(tokens) == len(values)
    counted = sealed['results'][1]['value']
    assert [(-item['count'], item['value']) for item in counted] == sorted(
        (-item['count'], item['value']) for item in counted
    )


def test_a_token_does_not_tell_its_values_length():
    """Every value up to 28 bytes of JSON seals to one length; a longer one tells only its power of two."""
    seal = private.Seal(KEY, 7)
    short = [True, False, None, 0, 7, -3.5, 987654.25, 1e300, 'yes', 'no', 'M', 'Female', '', 'SN-0007', 'x' * 26]

    def length(value: Any) -> int:
        return len(seal.seal(value, 'f') or '')

    lengths = {length(value) for value in short}
    assert len(lengths) == 1, lengths
    assert {length('x' * n) for n in (40, 50, 58)} == {length('x' * 30)}
    assert length('x' * 70) > length('x' * 58)
    for value in [*short, 'x' * 5000, {'a': [1, 2]}]:
        assert seal.open(seal.seal(value, 'f'), 'f') == (True, value)


def test_a_token_opens_only_in_a_test_of_its_own_field_and_a_literal_is_refused():
    seal = private.Seal(KEY, 7)
    token, other = seal.seal('SN-2', 'serial'), seal.seal('SN-2', 'note')

    def guarded(node: Any, dataset_id: int = 7) -> Any:
        with guard.guarding(guard.Guard(7, frozenset({'serial'}), seal)) as one:
            return guard.filters(node, dataset_id), one

    tree = {
        'logic': 'AND',
        'filters': [
            {'field': 'serial', 'operator': 'in', 'value': [token, token]},
            {'field': 'note', 'operator': '=', 'value': token},
            {'logic': 'NOT', 'filters': [{'field': 'serial', 'operator': '!=', 'value': token}]},
            {'field': 'serial', 'operator': 'exists'},
            {'field': 'id', 'operator': 'cg_self', 'value': {'field': 'serial', 'operator': '=', 'value': token}},
        ],
    }
    opened, one = guarded(tree)
    first, note, negated, exists, crossed = opened['filters']
    assert first['value'] == ['SN-2', 'SN-2'] and negated['filters'][0]['value'] == 'SN-2'
    assert note['value'] == token and exists == tree['filters'][3] and crossed['value']['value'] == 'SN-2'
    assert one.opened == 4 and one.unreadable == 0
    # Another field's token, or one under another key: the test matches nothing.
    for wrong in (other, private.Seal(bytes(64), 7).seal('SN-2', 'serial')):
        nothing, one = guarded({'field': 'serial', 'operator': '=', 'value': wrong})
        assert nothing == {'field': 'serial', 'operator': guard.NOTHING} and one.unreadable == 1
    # A literal, an order, a search: refused, whatever the value.
    for node in (
        {'field': 'serial', 'operator': '=', 'value': 'SN-2'},
        {'field': 'serial', 'operator': 'in', 'value': [token, 'SN-9']},
        {'field': 'serial', 'operator': '>', 'value': token},
        {'field': 'serial.part', 'operator': 'contains', 'value': 'S'},
        {'logic': 'OR', 'filters': [{'field': 'serial', 'operator': 'between', 'value': [1, 2]}]},
    ):
        with pytest.raises(HTTPException) as refusal:
            guarded(node)
        detail: Any = refusal.value.detail
        assert refusal.value.status_code == 403 and detail['code'] == guard.CODE
    with pytest.raises(HTTPException):
        guarded({'field': 'note', 'operator': '=', 'value': 'x'}, dataset_id=8)  # another dataset
    # A person's request has no guard: nothing changes.
    assert guard.filters(tree, 7) is tree


OTHER_DATASET = 3  # test_assistant_core's dataset not shared with anyone: the administrator's still
INNER_ROUTES = list(private.COVERED_ROUTES)
QUERY = '/api/datasets/{id}/samples/query'
AGGREGATE = '/api/datasets/{id}/aggregate'
HISTOGRAM = '/api/datasets/{id}/histogram'
PARSE = '/api/datasets/{id}/filter_query/parse'


async def _answer(seal_key: bytes, method: str, template: str, dataset_id: int = TRIALS, body: Any = None) -> Any:
    """The route's answer to a request made for a tool, with no tool's own handling in the way."""
    from backend.assistant import inner
    from backend.assistant.identity import Identity

    path_params: dict[str, Any] = {'id': dataset_id} if '{id}' in template else {}
    if method == 'GET' and isinstance(body, dict):  # a GET's example names its other path parameters
        path_params, body = {**path_params, **body}, None
    return await inner.request(
        Identity(ADMIN, 'mcp', seal_key=seal_key),
        INNER_ROUTES,
        method,
        template,
        path_params=path_params,
        body=body,
    )


async def _inner(seal_key: bytes, method: str, template: str, dataset_id: int, body: Any = None) -> Any:
    return (await _answer(seal_key, method, template, dataset_id, body)).body


def test_a_token_is_no_oracle(world):
    """Every token any answer holds, sent back in every argument of every tool and in every position of a filter node
    and of filter text, for its own field, another field and another dataset: no answer and no error shows a planted
    value, and a token outside its own field and dataset selects nothing, as a literal string would."""

    async def scenario(w: World):
        await w.switch_on(power_user=True)
        fields = await _plant(w)
        # The same values in a field that isn't private, here and in another dataset.
        await w.database.execute(
            "UPDATE samples SET metadata = metadata || jsonb_build_object('mirror_code', metadata->'secret_code') "
            'WHERE dataset_id = $1',
            TRIALS,
        )
        await datasets.add_samples(
            OTHER_DATASET,
            {'samples': [{'filepath': f'other-{n}.jpg', 'metadata': {'secret_code': TEXT[n % 2]}} for n in range(4)]},
            user=USER,
        )
        seal_key = (await rule.verdict(ADMIN, 'mcp')).seal_key
        assert seal_key is not None and len(seal_key) == 64
        # Every token the API gives a tool, with no tool's own stripping in the way.
        tokens: set[str] = set()
        for template in ('/api/datasets/{id}', '/api/datasets/{id}/schema', '/api/datasets/{id}/views'):
            tokens.update(private.sealed_in(await _inner(seal_key, 'GET', template, TRIALS)))
        for field in PRIVATE:
            answer = await _inner(
                seal_key,
                'POST',
                '/api/datasets/{id}/aggregate',
                TRIALS,
                {'aggregations': [{'field': field, 'op': 'count_values'}]},
            )
            assert not _leaks(answer), answer
            tokens.update(private.sealed_in(answer))
        examples = Examples(fields)
        for tool in assistant.tools():
            for arguments in _argument_sets(tool, examples):
                answer = await assistant.call_tool(user_id=ADMIN, door='mcp', name=tool.name, arguments=arguments)
                tokens.update(private.sealed_in(answer))
                examples.learn(answer)
        own = private.Seal(seal_key, TRIALS)
        code_tokens = {
            token: own.open(token, 'secret_code')[1] for token in tokens if own.open(token, 'secret_code')[0]
        }
        # The two planted texts, and the values of the saved view's and the default view's filters.
        assert set(code_tokens.values()) == {*TEXT, SAVED, DEFAULT}, code_tokens
        assert len(tokens) > len(code_tokens)

        # In every position of a filter node, through the routes that echo, evaluate and parse filters.
        for token in sorted(tokens):
            for field in ('secret_code', 'mirror_code', 'crop', 'secret_number', 'tags'):
                for node in (
                    {'field': field, 'operator': '=', 'value': token},
                    {'field': field, 'operator': 'in', 'value': [token, 'x']},
                    {'logic': 'NOT', 'filters': [{'field': field, 'operator': '!=', 'value': token}]},
                ):
                    for dataset_id in (TRIALS, OTHER_DATASET):
                        # The answers' own values of fields that aren't private (mirror_code here, secret_code
                        # in the other dataset) are the planted texts, shown as they may be: those are left out.
                        requests: list[tuple[str, Any]] = [
                            ('/api/datasets/{id}/filter_query/format', {'filters': [node]}),
                            ('/api/datasets/{id}/filter_query/parse', {'query': 'crop exists', 'current': [node]}),
                            (
                                '/api/datasets/{id}/samples/query',
                                {'filters': node, 'limit': 50, 'fields': 'id', 'slim': True},
                            ),
                        ]
                        if dataset_id == TRIALS and field in PRIVATE:
                            requests.append(
                                (
                                    '/api/datasets/{id}/aggregate',
                                    {'aggregations': [{'field': field, 'op': 'count_values'}], 'filters': node},
                                ),
                            )
                        for template, body in requests:
                            answer = await _inner(seal_key, 'POST', template, dataset_id, body)
                            leaked = _leaks(answer)
                            assert not leaked, (
                                f'{template} on {dataset_id} with {node}: {leaked}: {json.dumps(answer)[:1500]}'
                            )
                # Outside its own field and dataset, a token selects what a literal string would: nothing.
                if token in code_tokens and field != 'secret_code':
                    for dataset_id in (TRIALS, OTHER_DATASET):
                        counted = await _inner(
                            seal_key,
                            'POST',
                            '/api/datasets/{id}/samples/query',
                            dataset_id,
                            {'filters': {'field': field, 'operator': '=', 'value': token}, 'limit': 0},
                        )
                        assert counted['total'] == 0, (field, dataset_id)
            if token in code_tokens:
                counted = await _inner(
                    seal_key,
                    'POST',
                    '/api/datasets/{id}/samples/query',
                    OTHER_DATASET,
                    {'filters': {'field': 'secret_code', 'operator': '=', 'value': token}, 'limit': 0},
                )
                assert counted['total'] == 0, 'a token of another dataset'
                # And in its own place it selects the samples with that value.
                counted = await _inner(
                    seal_key,
                    'POST',
                    '/api/datasets/{id}/samples/query',
                    TRIALS,
                    {'filters': {'field': 'secret_code', 'operator': '=', 'value': token}, 'limit': 0},
                )
                assert counted['total'] == (3 if code_tokens[token] in TEXT else 0)

        # In every argument of every tool, and in filter text.
        texts = [f'{field} = "{token}"' for token in sorted(code_tokens) for field in ('secret_code', 'mirror_code')]
        for tool in assistant.tools():
            properties = tool.input_schema.get('properties', {})
            for name, spec in properties.items():
                for token in sorted(tokens)[:6]:
                    value: Any = None
                    if spec.get('type') == 'string':
                        value = token
                    elif spec.get('type') == 'array' and spec.get('items', {}).get('type') == 'string':
                        value = [token]
                    if value is None:
                        continue
                    for base in _argument_sets(tool, examples)[:4]:
                        arguments = {**base, name: value}
                        answer = await assistant.call_tool(
                            user_id=ADMIN,
                            door='mcp',
                            name=tool.name,
                            arguments=arguments,
                        )
                        assert not _leaks(answer), f'{tool.name}({arguments}): {_leaks(answer)}'
                if name in ('filter', 'add'):
                    for text in texts:
                        for base in _argument_sets(tool, examples)[:2]:
                            answer = await assistant.call_tool(
                                user_id=ADMIN,
                                door='mcp',
                                name=tool.name,
                                arguments={**base, name: text},
                            )
                            assert not _leaks(answer), f'{tool.name}({name}={text}): {_leaks(answer)}'

    world(scenario)


# Every way a request can test a field's value, by filter node: each is refused on a private field with a value the
# request wrote (the four that take tokens too).
QUESTIONS = [
    '=',
    '==',
    '!=',
    '<>',
    '<',
    '<=',
    '>',
    '>=',
    'in',
    'not in',
    'not_in',
    'nin',
    'contains',
    'like',
    'between',
    'starts_with',
    'match_elements',
    'elem_match',
    'geo_region',
    'geo_region_exclude',
    'geo_near',
    'geo_polygon',
    'cg_self',
]
SUMMARIES = ['sum', 'mean', 'std', 'min', 'max', 'bounds', 'quantiles']
COUNTS = ['count', 'count_values', 'distinct']
QUESTIONED = (*PRIVATE, 'detections.label')
LITERALS: list[Any] = [TEXT[0], 'not-planted', NUMBERS[0], 1, True, [TEXT[0]], [NUMBERS[0], 2.5], {'field': 'x'}]
# Filter text a model could write about a private field, `{f}` its name.
TEXTS = [
    '{f} > 1',
    '{f} < 987655',
    '{f} >= 987654.25',
    '{f} = "ZQXSECRET-ALPHA"',
    '{f} != "x"',
    '{f} = 987654.25',
    '{f} contains "ZQX"',
    '{f} in ("ZQXSECRET-ALPHA", "b")',
    '{f} not in (1, 2)',
    'NOT ({f} = "ZQXSECRET-BRAVO")',
    'crop = "wheat" OR {f} > 5',
]


def _refusal(answer: Any) -> str:
    assert answer.status == 403, (answer.status, answer.body)
    assert answer.body['detail']['code'] == guard.CODE, answer.body
    assert not _leaks(answer.body), answer.body
    return json.dumps(answer.body)


def test_a_private_field_cant_be_questioned(world):
    """For every private field and every operator, comparison, sort, summary and histogram a request can express,
    by filter node and by filter text: the answer is the refusal, the same words whatever value was asked about, and
    never a count."""

    async def scenario(w: World):
        await w.switch_on(power_user=True)
        await _plant(w)
        seal_key = (await rule.verdict(ADMIN, 'mcp')).seal_key
        assert seal_key is not None
        body: Any
        for field in QUESTIONED:
            for op in QUESTIONS:
                words = set()
                for literal in LITERALS:
                    node = {'field': field, 'operator': op, 'value': literal}
                    for tree in (node, {'logic': 'NOT', 'filters': [node]}, {'logic': 'OR', 'filters': [node]}):
                        words.add(_refusal(await _answer(seal_key, 'POST', QUERY, body={'filters': tree, 'limit': 0})))
                        body = {'aggregations': [{'field': 'crop', 'op': 'count'}], 'filters': tree}
                        words.add(_refusal(await _answer(seal_key, 'POST', AGGREGATE, body=body)))
                        body = {'field': 'crop', 'filters': tree}
                        words.add(_refusal(await _answer(seal_key, 'POST', HISTOGRAM, body=body)))
                assert len(words) == 1, (field, op, words)
            for op in sorted(guard.PRESENCE):
                body = {'filters': {'field': field, 'operator': op}, 'limit': 0}
                assert (await _answer(seal_key, 'POST', QUERY, body=body)).status == 200, (field, op)
            _refusal(await _answer(seal_key, 'POST', QUERY, body={'sort_by': field, 'limit': 1}))
            for op in SUMMARIES:
                _refusal(
                    await _answer(seal_key, 'POST', AGGREGATE, body={'aggregations': [{'field': field, 'op': op}]}),
                )
            for op in COUNTS:
                counted = await _answer(
                    seal_key,
                    'POST',
                    AGGREGATE,
                    body={'aggregations': [{'field': field, 'op': op}]},
                )
                assert counted.status == 200 and not _leaks(counted.body), counted.body
            for body in ({'field': field}, {'field': field, 'bins': 10}, {'field': field, 'as_number': True}):
                _refusal(await _answer(seal_key, 'POST', HISTOGRAM, body=body))

        # By filter text: the server reads it, and the count it would give is refused.
        parsed_any = 0
        for field in PRIVATE:
            for text in TEXTS:
                written = text.format(f=field)
                parsed = await _answer(seal_key, 'POST', PARSE, body={'query': written})
                if parsed.status != 200:
                    # The text the request wrote may be quoted back; nothing else.
                    assert parsed.status in (400, 422), parsed.body
                    assert not [value for value in _leaks(parsed.body) if value not in written], parsed.body
                    continue
                parsed_any += 1
                tree = {'logic': 'AND', 'filters': parsed.body['filters']}
                _refusal(await _answer(seal_key, 'POST', QUERY, body={'filters': tree, 'limit': 0}))
        assert parsed_any >= 20, parsed_any

        # The four tests that take tokens do, of the field's own values.
        values = await _answer(
            seal_key,
            'POST',
            AGGREGATE,
            body={'aggregations': [{'field': 'secret_code', 'op': 'count_values'}]},
        )
        tokens = [item['value'] for item in values.body['results'][0]['value']]
        assert len(tokens) == 2 and all(private.is_sealed(token) for token in tokens)
        for op, value, total in (('=', tokens[0], 3), ('!=', tokens[0], 3), ('in', tokens, 6), ('not in', tokens, 0)):
            counted = await _answer(
                seal_key,
                'POST',
                QUERY,
                body={'filters': {'field': 'secret_code', 'operator': op, 'value': value}, 'limit': 0},
            )
            assert counted.status == 200 and counted.body['total'] == total, (op, counted.body)

    world(scenario)


def test_halving_a_private_number_learns_nothing(world):
    """The attack written out: bisect secret_number with `>` (by filter node and by filter text), moving by the count
    each step answers. Every step gets the same refusal, so the attacker's interval never depends on the values."""

    async def scenario(w: World):
        await w.switch_on(power_user=True)
        await _plant(w)
        seal_key = (await rule.verdict(ADMIN, 'mcp')).seal_key
        assert seal_key is not None
        for by_text in (False, True):
            low, high, words = 0.0, 2_000_000.0, set()
            for _ in range(24):
                middle = (low + high) / 2
                node = {'field': 'secret_number', 'operator': '>', 'value': middle}
                if by_text:
                    parsed = await _answer(seal_key, 'POST', PARSE, body={'query': f'secret_number > {middle}'})
                    assert parsed.status == 200, parsed.body
                    node = {'logic': 'AND', 'filters': parsed.body['filters']}
                answer = await _answer(seal_key, 'POST', QUERY, body={'filters': node, 'limit': 0})
                words.add(_refusal(answer))
                total = answer.body.get('total', 0) if answer.status == 200 else 0
                low, high = (middle, high) if total else (low, middle)
            assert len(words) == 1
            assert high < 1, 'the interval moved only one way: it never learned where the values are'

    world(scenario)


# A request for each route a tool may list (private.COVERED_ROUTES), made on the planted dataset: its answer holds no
# planted value. A route added to COVERED_ROUTES fails this test until it has an example here.
ROUTE_EXAMPLES: dict[tuple[str, str], list[Any]] = {
    ('GET', '/api/datasets'): [None],
    ('GET', '/api/datasets/{id}'): [None],
    ('GET', '/api/datasets/{id}/schema'): [None],
    ('GET', '/api/datasets/{id}/field-stats'): [None],
    ('GET', '/api/datasets/{id}/coverage'): [None],
    ('GET', '/api/datasets/{id}/concepts'): [None],
    ('GET', '/api/datasets/{id}/views'): [None],
    ('POST', AGGREGATE): ['every field'],
    ('POST', QUERY): [{'limit': 50}, {'limit': 50, 'sort_by': 'score'}],
    ('POST', HISTOGRAM): [{'field': name} for name in ('crop', 'score', *QUESTIONED)],
    ('POST', PARSE): [{'query': 'secret_code exists AND crop = "wheat"'}, {'query': 'detections.label exists'}],
    ('POST', '/api/datasets/{id}/filter_query/format'): [
        {'filters': [{'field': 'secret_code', 'operator': '=', 'value': TEXT[0]}]},
        {
            'filters': [
                {'field': 'secret_number', 'operator': 'in', 'value': NUMBERS},
                {'field': 'tags', 'operator': '=', 'value': TAG},
            ],
        },
    ],
    ('GET', '/api/datasets/{id}/concepts/{concept_id}/boundary'): [{'concept_id': CONCEPT_ID}],
    ('GET', '/api/regions'): [None],
    ('POST', '/api/search'): [{'dataset_id': TRIALS, 'vector': [1.0, 0.0, 0.5, 0.0], 'field_name': 'clip'}],
    ('GET', '/api/views/{slug}'): [{'slug': 'saved-secret'}, {'slug': 'default-secret'}],
}


def test_every_route_a_tool_may_list_seals_its_answer(world):
    assert set(ROUTE_EXAMPLES) == set(private.COVERED_ROUTES)

    async def scenario(w: World):
        await w.switch_on(power_user=True)
        fields = await _plant(w)
        seal_key = (await rule.verdict(ADMIN, 'mcp')).seal_key
        assert seal_key is not None
        every = [{'field': name, 'op': op} for name in fields for op in ('count_values', 'distinct', 'values', 'count')]
        body: Any
        for (method, template), bodies in ROUTE_EXAMPLES.items():
            for body in bodies:
                if body == 'every field':
                    body = {'aggregations': every}
                answer = await _answer(seal_key, method, template, body=body)
                assert answer.status in (200, 403), (template, body, answer.status, answer.body)
                assert not _leaks(answer.body), f'{method} {template} {body}: {_leaks(answer.body)}'

        # A request's own value of a private field is never turned into a token, by an answer that echoes the request
        # (the filter text's reading, an error) or any other: a token is made only from what the dataset holds.
        fresh = 'ZQXFRESH'
        guess = {'field': 'secret_code', 'operator': '=', 'value': fresh}
        own = private.Seal(seal_key, TRIALS)
        for template, body in (
            (PARSE, {'query': f'secret_code = "{fresh}"'}),
            (PARSE, {'query': 'crop exists', 'current': [guess]}),
            (PARSE, {'query': f'secret_code = "{fresh}" AND', 'current': [guess]}),
            ('/api/datasets/{id}/filter_query/format', {'filters': [guess]}),
            (QUERY, {'filters': guess, 'limit': 1}),
            (QUERY, {'filters': [guess], 'limit': 1}),
            (QUERY, {'filters': guess, 'limit': 'many'}),
            (AGGREGATE, {'aggregations': [{'field': 'secret_code', 'op': 'count_values'}], 'filters': guess}),
            (AGGREGATE, {'aggregations': [guess]}),
            (HISTOGRAM, {'field': 'crop', 'filters': guess}),
            (HISTOGRAM, {'field': 'crop', 'filters': [guess]}),
        ):
            answer = await _answer(seal_key, 'POST', template, body=body)
            for token in private.sealed_in(answer.body):
                assert all(own.open(token, name) != (True, fresh) for name in PRIVATE), (template, body, answer.body)

    world(scenario)


SALARIES = [7300, 1200, 9900, 4100, 5600, 2800, 8700, 3300, 6500, 1900, 7800, 4600]


def _ranked_lists(node: Any, opened: dict[str, float]) -> Iterable[list[str]]:
    """Every list in an answer that holds two or more tokens of `opened`, as its tokens in order."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _ranked_lists(value, opened)
    elif isinstance(node, list):
        tokens = []
        for item in node:
            for candidate in (item, *(item.values() if isinstance(item, dict) else ())):
                if isinstance(candidate, str) and candidate in opened:
                    tokens.append(candidate)
        if len(tokens) >= 2:
            yield tokens
        for item in node:
            yield from _ranked_lists(item, opened)


def _order_statistics(node: Any) -> Iterable[str]:
    """Order statistics of `salary` that hold something (describe_dataset's `range: null` holds nothing)."""
    if isinstance(node, dict):
        if node.get('name') == 'salary' or node.get('field') == 'salary' or node.get('field_name') == 'salary':
            yield from (key for key in node if key in private.ORDER_KEYS and node[key] is not None)
        for key, value in node.items():
            if key == 'salary' and isinstance(value, dict):
                yield from (name for name in value if name in private.ORDER_KEYS and value[name] is not None)
            yield from _order_statistics(value)
    elif isinstance(node, list):
        for value in node:
            yield from _order_statistics(value)


def test_the_largest_value_of_a_private_field_cant_be_found(world):
    """The attack written out. With every token any tool or covered route gives for a private number field, and an
    equality filter per token (all a model may do with one), the sample holding the largest value can't be named, nor
    the smallest, nor any two samples put in order: no answer keeps an order statistic of the field, and every list of
    its tokens is in token order, which says nothing of the values."""

    async def scenario(w: World):
        await w.switch_on(power_user=True)
        fields = await _plant(w)
        samples = [{'filepath': f'pay-{n}.jpg', 'metadata': {'salary': value}} for n, value in enumerate(SALARIES)]
        await datasets.add_samples(TRIALS, {'samples': samples}, user=USER)
        marked = await w.api(
            ADMIN,
            'PATCH',
            f'/api/datasets/{TRIALS}/fields/salary/description',
            json={'values_private': True},
        )
        assert marked.status_code == 200, marked.text
        await run_refresh(w.database, TRIALS)
        seal_key = (await rule.verdict(ADMIN, 'mcp')).seal_key
        assert seal_key is not None
        answers: list[Any] = []
        for (method, template), bodies in ROUTE_EXAMPLES.items():
            for body in bodies:
                if body == 'every field':
                    body = {
                        'aggregations': [
                            {'field': name, 'op': op}
                            for name in (*fields, 'salary')
                            for op in ('count_values', 'distinct', 'values', 'count')
                        ],
                    }
                answers.append((await _answer(seal_key, method, template, body=body)).body)
        examples = Examples([*fields, 'salary'])
        for tool in assistant.tools():
            for arguments in _argument_sets(tool, examples):
                answers.append(
                    await assistant.call_tool(user_id=ADMIN, door='mcp', name=tool.name, arguments=arguments),
                )

        # The test's own knowledge (never the model's): which token holds which salary.
        own = private.Seal(seal_key, TRIALS)
        # (A count of values writes them as text: '7300' and 7300 are two tokens of one salary.)
        opened = {
            token: float(own.open(token, 'salary')[1])
            for token in private.sealed_in(answers)
            if own.open(token, 'salary')[0]
        }
        assert set(opened.values()) == set(SALARIES), 'every salary reached some answer, sealed'
        assert not list(_order_statistics(answers)), 'an order statistic of a private field says which is largest'
        lists = list(_ranked_lists(answers, opened))
        assert lists, 'the answers list the tokens'
        for tokens in lists:
            assert tokens == sorted(tokens), f'a sealed list in an order of its own: {[opened[t] for t in tokens]}'
        # So the attacker's best guesses (the first or last of any list) name the largest only by chance, and the
        # order of the tokens is the order of their text: a sample's token, by an equality filter, ranks nothing.
        by_value = sorted(opened, key=lambda token: opened[token])
        assert by_value != sorted(by_value), 'this planting happens to seal in value order: plant other values'

    world(scenario)


def test_a_token_from_every_covered_route_filters_as_its_value_does(world):
    """The round trip: each token any covered route answers with, used as an `=` filter on its field, selects exactly
    the samples its plain value selects for a person. One value can have two tokens (a count writes 7300 as '7300'):
    both select the same samples."""

    async def scenario(w: World):
        await w.switch_on(power_user=True)
        fields = await _plant(w)
        samples = [{'filepath': f'pay-{n}.jpg', 'metadata': {'salary': value}} for n, value in enumerate(SALARIES)]
        await datasets.add_samples(TRIALS, {'samples': samples}, user=USER)
        marked = await w.api(
            ADMIN,
            'PATCH',
            f'/api/datasets/{TRIALS}/fields/salary/description',
            json={'values_private': True},
        )
        assert marked.status_code == 200, marked.text
        await run_refresh(w.database, TRIALS)
        seal_key = (await rule.verdict(ADMIN, 'mcp')).seal_key
        assert seal_key is not None
        own = private.Seal(seal_key, TRIALS)
        names = (*PRIVATE, 'detections.label', 'salary')
        every = [
            {'field': name, 'op': op} for name in (*fields, 'salary') for op in ('count_values', 'distinct', 'values')
        ]
        found: dict[tuple[str, str], Any] = {}  # (field, token) -> its plain value
        carrying = set()
        for (method, template), bodies in ROUTE_EXAMPLES.items():
            for body in bodies:
                answer = await _answer(
                    seal_key,
                    method,
                    template,
                    body={'aggregations': every} if body == 'every field' else body,
                )
                for token in set(private.sealed_in(answer.body)):
                    for name in names:
                        ok, plain = own.open(token, name)
                        if ok:
                            found[(name, token)] = plain
                            carrying.add((method, template))
        assert {
            ('GET', '/api/datasets/{id}'),
            ('GET', '/api/datasets/{id}/schema'),
            ('GET', '/api/datasets/{id}/field-stats'),
            ('GET', '/api/datasets/{id}/views'),
            ('POST', AGGREGATE),
            ('POST', QUERY),
        } <= carrying, carrying
        salaries = {(name, plain) for (name, _), plain in found.items() if name == 'salary'}
        assert ('salary', 7300) in salaries and ('salary', '7300') in salaries, 'both forms of one value were seen'
        for (name, token), plain in sorted(found.items(), key=lambda item: item[0]):
            body = {
                'filters': {'field': name, 'operator': '=', 'value': token},
                'limit': 50,
                'fields': 'id',
                'slim': True,
            }
            by_token = await _answer(seal_key, 'POST', QUERY, body=body)
            body['filters'] = {'field': name, 'operator': '=', 'value': plain}
            by_value = await w.api(ADMIN, 'POST', f'/api/datasets/{TRIALS}/samples/query', json=body)
            assert by_token.status == by_value.status_code, (name, plain, by_token.body, by_value.text)
            if by_value.status_code == 200:
                ids = sorted(item['id'] for item in by_token.body['items'])
                assert ids == sorted(item['id'] for item in by_value.json()['items']), (name, plain)
                if name == 'salary':
                    assert len(ids) == 1, (plain, ids)

    world(scenario)
