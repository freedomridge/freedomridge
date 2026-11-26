"""What the assistant's counting tools cost (docs/ASSISTANT.md, "What is quick and what is slow"), against a disposable
PostgreSQL: how many requests that read the samples each call of field_values and field_summary makes, and how many
statements those run, pinned so that a later change can't quietly bring the extra reads back; field_summary's answer
when only its second read runs out of time; what each too_slow says; and describe_dataset's `indexed`.
"""

import os
from typing import Any

import asyncpg
import pytest

from backend.assistant import api as assistant_api
from backend.assistant import inner
from backend.routers import datasets
from backend.tests import test_assistant_core
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_assistant_core import POWER, TRIALS, World, refused
from backend.tests.test_assistant_views import CROPS, _plant, ok
from backend.tests.test_field_registry_postgres import USER

registry_database = test_assistant_core.registry_database
world = test_assistant_core.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

AGGREGATE = '/api/datasets/{id}/aggregate'
HISTOGRAM = '/api/datasets/{id}/histogram'
QUERY = '/api/datasets/{id}/samples/query'
SCHEMA = '/api/datasets/{id}/schema'
TIMED_OUT = asyncpg.QueryCanceledError('canceling statement due to statement timeout')
BRIGHTNESS = [10 * n + 5 for n in range(len(CROPS))]
# Two images with drawings besides _plant's twelve: their labels are a list a sample.
DRAWN = [
    {'filepath': 'drawn-1.jpg', 'metadata': {'crop': 'wheat', 'detections': [{'label': 'weed'}, {'label': 'rock'}]}},
    {'filepath': 'drawn-2.jpg', 'metadata': {'crop': 'oats', 'detections': [{'label': 'weed'}]}},
]


class Watch:
    """The requests a tool makes, and the statements the aggregate route runs for them."""

    def __init__(self, monkeypatch: Any):
        self.requests: list[tuple[str, str, Any, Any]] = []
        self.statements: list[str] = []
        real_request, real_statements = inner.request, datasets._field_statements

        async def request(identity: Any, listed: Any, method: str, template: str, **kwargs: Any) -> Any:
            self.requests.append((method, template, kwargs.get('body'), kwargs.get('query')))
            return await real_request(identity, listed, method, template, **kwargs)

        def field_statements(*args: Any, **kwargs: Any) -> Any:
            found = real_statements(*args, **kwargs)
            self.statements += [statement.sql() for statement in found]
            return found

        monkeypatch.setattr(inner, 'request', request)
        monkeypatch.setattr(datasets, '_field_statements', field_statements)

    def clear(self) -> None:
        self.requests.clear()
        self.statements.clear()

    def reads(self) -> list[str]:
        """The requests that read the samples, in order: `count` for a samples query that counts (the view's count),
        else the route (the aggregate or the histogram route: a read of the field). The probe for object rows
        (working._images) reads one sample at most and is left out."""
        return [
            'count' if template == QUERY else template
            for _, template, body, _ in self.requests
            if template in (AGGREGATE, HISTOGRAM) or (template == QUERY and not (body or {}).get('skip_count'))
        ]


# (tool, field, extra arguments): the reads of the field (requests after the view's one count), and the statements
# they run.
CASES = [
    ('field_values', 'crop', {}, 1, 1),
    ('field_values', 'brightness', {}, 1, 1),
    ('field_values', 'crop', {'search': 'ea'}, 2, 2),  # samples_with_value is over the view, whatever the search
    ('field_values', 'tags', {}, 1, 2),  # a list's elements, then its samples
    ('field_values', 'detections.label', {}, 1, 2),  # the labels, then the samples with the drawing
    ('field_summary', 'crop', {}, 1, 1),
    ('field_summary', 'tags', {}, 1, 2),
    ('field_summary', 'detections.label', {}, 1, 2),
    ('field_summary', 'brightness', {}, 2, 2),
    ('field_summary', 'taken_at', {}, 2, 2),
    ('field_summary', 'serial', {}, 1, 1),  # private: its counts only
    ('field_values', 'serial', {}, 1, 1),
]


def test_each_call_reads_the_samples_as_few_times_as_it_takes(world, monkeypatch):
    async def scenario(w: World):
        await _plant(w)
        await datasets.add_samples(TRIALS, {'samples': DRAWN}, user=USER)
        await run_refresh(w.database, TRIALS)
        watch = Watch(monkeypatch)
        view = ok(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop = "wheat"'))
        for where in ({'dataset': TRIALS}, {'view': view['view']}):
            for name, field, extra, requests, statements in CASES:
                watch.clear()
                ok(await w.tool(POWER, name, field=field, **where, **extra))
                case = (name, field, extra, where)
                assert watch.reads() == ['count', *[AGGREGATE] * requests], (case, watch.requests)
                assert len(watch.statements) == statements, (case, watch.statements)
                # Looking the field up reads no samples: the schema without the lists of values it reads from them.
                schemas = [query for _, template, _, query in watch.requests if template == SCHEMA]
                assert schemas == [{'values': 'false'}], (case, watch.requests)

    world(scenario)


def test_a_summary_whose_second_read_runs_out_of_time_still_gives_the_first(world, monkeypatch):
    async def scenario(w: World):
        await _plant(w, private=False)
        real = datasets.run_aggregations

        async def second_read_slow(id: int, aggregations: Any, *args: Any, **kwargs: Any) -> Any:
            if any(spec.op == 'quantiles' for spec in aggregations):
                raise TIMED_OUT
            return await real(id, aggregations, *args, **kwargs)

        monkeypatch.setattr(datasets, 'run_aggregations', second_read_slow)
        whole = ok(await w.tool(POWER, 'make_view', dataset=TRIALS))
        for where, about in (({'dataset': TRIALS}, 'this dataset'), ({'view': whole['view']}, 'this view')):
            summary = ok(await w.tool(POWER, 'field_summary', field='brightness', **where))
            assert (summary['min'], summary['max']) == (min(BRIGHTNESS), max(BRIGHTNESS)), summary
            assert summary['mean'] == pytest.approx(sum(BRIGHTNESS) / len(BRIGHTNESS)), summary
            assert (summary['samples_with_value'], summary['samples_without']) == (len(CROPS), 0), summary
            assert (summary['quantiles'], summary['histogram'], summary['distinct']) == (None, None, None), summary
            assert f'the {len(CROPS):,} samples of {about}' in summary['note'], summary
            assert 'narrow the view' in summary['note'], summary
        # A text field's summary is one read: nothing runs out on its own.
        assert 'note' not in ok(await w.tool(POWER, 'field_summary', dataset=TRIALS, field='crop'))

        async def every_read_slow(*args: Any, **kwargs: Any) -> Any:
            raise TIMED_OUT

        monkeypatch.setattr(datasets, 'run_aggregations', every_read_slow)
        for name in ('field_summary', 'field_values'):
            message = refused(await w.tool(POWER, name, dataset=TRIALS, field='brightness'), 'too_slow')
            assert message.startswith(name) and f'every one of the {len(CROPS):,} samples of this dataset' in message
            assert 'whether or not the field is indexed' in message and 'Narrow the view first' in message, message
            assert 'administrator' not in message, message
        # The view's count itself: no number to give.
        monkeypatch.setattr(datasets, 'run_aggregations', real)
        real_query = datasets._list_samples_post

        async def count_slow(id: int, request: Any) -> Any:
            if not request.skip_count:
                raise TIMED_OUT
            return await real_query(id, request)

        monkeypatch.setattr(datasets, '_list_samples_post', count_slow)
        for name in ('field_summary', 'field_values'):
            message = refused(await w.tool(POWER, name, view=whole['view'], field='brightness'), 'too_slow')
            assert message.startswith(f'{name} reads every sample of this view, whether or not'), message
            assert 'even counting them took longer' in message and 'administrator' not in message, message

    world(scenario)


def test_too_slow_names_the_fields_that_are_not_indexed(world, monkeypatch):
    async def scenario(w: World):
        await _plant(w, private=False)
        real = datasets._list_samples_post

        async def slow(id: int, request: Any) -> Any:
            if request.skip_count and request.limit == 1:  # working._images' probe for object rows
                return await real(id, request)
            raise TIMED_OUT

        monkeypatch.setattr(datasets, '_list_samples_post', slow)
        message = refused(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='brightness < 40'), 'too_slow')
        assert message.startswith('Counting this view took longer') and "Its filter uses 'brightness'" in message
        assert 'every sample of the dataset was read for it' in message, message
        assert "ask an administrator to index 'brightness'" in message, message
        # Tested only in ways no index on it would serve: named, without the advice to index it.
        message = refused(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop contains "whe"'), 'too_slow')
        assert "Its filter uses 'crop', which is not indexed" in message and 'administrator' not in message, message
        assert 'Narrow the view by an indexed field first' in message, message
        await w.database.execute(
            "UPDATE dataset_fields SET is_promoted = TRUE WHERE dataset_id = $1 AND field_name = 'brightness'",
            TRIALS,
        )
        message = refused(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='brightness < 40'), 'too_slow')
        assert 'Narrow the view' in message and 'administrator' not in message and 'not indexed' not in message
        both = 'brightness < 40 AND crop = "wheat"'
        message = refused(await w.tool(POWER, 'make_view', dataset=TRIALS, filter=both), 'too_slow')
        assert "Its filter uses 'crop', which is not indexed" in message and "'brightness'" not in message, message
        # A list field's index serves `in`, not `=`.
        message = refused(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='tags in (dark)'), 'too_slow')
        assert 'Narrow the view' in message and 'not indexed' not in message and 'uses the list' not in message, message
        message = refused(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='tags = dark'), 'too_slow')
        assert "It uses the list field 'tags' otherwise than with in" in message, message
        assert 'every sample of the dataset was read for it' in message and 'test a list field with in' in message
        assert 'administrator' not in message, message
        # A page sorted by a field that is not indexed; a page of the whole dataset neither sorted nor filtered.
        message = refused(await w.tool(POWER, 'get_samples', dataset=TRIALS, sort_by='crop'), 'too_slow')
        assert message.startswith('Reading this page') and "Its sort uses 'crop'" in message, message
        assert refused(await w.tool(POWER, 'get_samples', dataset=TRIALS), 'too_slow') == assistant_api.TOO_SLOW
        assert 'index' not in assistant_api.TOO_SLOW

    world(scenario)


def test_a_page_whose_statement_runs_out_of_time_is_too_slow_not_a_fault(world, monkeypatch):
    """The samples query answers a page that runs out of time as it answers its count (it used to be a 500)."""

    async def scenario(w: World):
        await _plant(w, private=False)
        db = vars(datasets)['db']  # the test's database, which the world fixture puts in the module
        real = db.fetch

        async def fetch(sql: str, *args: Any, **kwargs: Any) -> Any:
            if 'OFFSET' in sql and args[-2:] == (7, 0):  # the page of seven get_samples asks for, not its count
                raise TIMED_OUT
            return await real(sql, *args, **kwargs)

        monkeypatch.setattr(db, 'fetch', fetch)
        message = refused(await w.tool(POWER, 'get_samples', dataset=TRIALS, limit=7), 'too_slow')
        assert message == assistant_api.TOO_SLOW
        monkeypatch.setattr(db, 'fetch', real)
        assert ok(await w.tool(POWER, 'get_samples', dataset=TRIALS, limit=7))['total'] == len(CROPS)

    world(scenario)


def test_describe_dataset_says_which_fields_are_indexed(world):
    async def scenario(w: World):
        await _plant(w, private=False)

        async def marks() -> dict[str, bool]:
            described = ok(await w.tool(POWER, 'describe_dataset', dataset=TRIALS, limit=100))
            return {field['name']: field['indexed'] for field in described['fields']}

        first = await marks()
        built_in = {'id': True, 'filepath': True, 'created_at': True, 'media_type': True, 'last_modified_at': False}
        assert {name: first[name] for name in [*built_in, 'tags']} == {**built_in, 'tags': True}, first  # `in`
        assert first['crop'] is False and first['brightness'] is False, first
        await w.database.execute(
            "UPDATE dataset_fields SET is_promoted = TRUE WHERE dataset_id = $1 AND field_name = 'crop'",
            TRIALS,
        )
        assert (await marks())['crop'] is True

    world(scenario)


def test_the_histogram_operation_gives_the_routes_buckets_over_the_values_bounds_counts(world):
    async def scenario(w: World):
        await _plant(w, private=False)
        extra = [
            {'filepath': 'level-1.jpg', 'metadata': {'level': 3}},
            {'filepath': 'level-2.jpg', 'metadata': {'level': 3}},
            {'filepath': 'as-text.jpg', 'metadata': {'brightness': '500'}},
        ]
        await datasets.add_samples(TRIALS, {'samples': extra}, user=USER)
        await run_refresh(w.database, TRIALS)

        async def both(field: str, bins: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
            route = await w.api(POWER, 'POST', f'/api/datasets/{TRIALS}/histogram', json={'field': field, 'bins': bins})
            specs = [{'field': field, 'op': 'histogram', 'bins': bins}, {'field': field, 'op': 'bounds'}]
            answer = await w.api(POWER, 'POST', f'/api/datasets/{TRIALS}/aggregate', json={'aggregations': specs})
            assert route.status_code == 200 and answer.status_code == 200, (route.text, answer.text)
            histogram, bounds = answer.json()['results']
            return route.json()['buckets'], histogram['value'], bounds['value']

        for bins in (20, None, 3, 1):
            route, buckets, _ = await both('taken_at', bins)
            assert buckets == route, (bins, buckets, route)
            # All one number: the route's bucket of that value, which a summary leaves out, as it always has.
            route, buckets, _ = await both('level', bins)
            assert buckets == route == [{'key': '3', 'count': 2, 'label': '3'}], (bins, buckets, route)
            # A number written as text: counted, as bounds counts it; the route counts only JSON numbers.
            route, buckets, bounds = await both('brightness', bins)
            assert bounds == [5.0, 500.0] and buckets[-1]['range']['max'] == 500.0, (bins, buckets)
            assert sum(bucket['count'] for bucket in buckets) == len(BRIGHTNESS) + 1, (bins, buckets)
            assert sum(bucket['count'] for bucket in route) == len(BRIGHTNESS), (bins, route)
        assert ok(await w.tool(POWER, 'field_summary', dataset=TRIALS, field='level'))['histogram'] == []

    world(scenario)


def test_a_list_comes_back_as_rows_however_long(world, monkeypatch):
    """count_values, distinct and values, with a limit or without, are rows of the statement (one an item), never one
    JSON value, which PostgreSQL refuses past 256 MB: a list asked without a limit has no ceiling of the server's
    own."""

    async def scenario(w: World):
        await _plant(w, private=False)
        watch = Watch(monkeypatch)
        db = vars(datasets)['db']  # the test's database, which the world fixture puts in the module
        real, fetched = db.fetch, []

        async def fetch(sql: str, *args: Any, **kwargs: Any) -> Any:
            rows = await real(sql, *args, **kwargs)
            fetched.append((sql, len(rows)))
            return rows

        monkeypatch.setattr(db, 'fetch', fetch)
        specs = [
            {'field': 'crop', 'op': 'count_values'},
            {'field': 'crop', 'op': 'distinct'},
            {'field': 'crop', 'op': 'values'},
            {'field': 'crop', 'op': 'count_values', 'limit': 2},
            {'field': 'crop', 'op': 'values', 'limit': 0},
            {'field': 'crop', 'op': 'distinct_count'},
        ]
        answer = await w.api(POWER, 'POST', f'/api/datasets/{TRIALS}/aggregate', json={'aggregations': specs})
        assert answer.status_code == 200, answer.text
        counted, listed, every, top, none, distinct = (result['value'] for result in answer.json()['results'])
        tally = {'wheat': 6, 'barley': 3, 'oats': 2, 'rye': 1}
        assert counted == [{'value': value, 'count': count} for value, count in tally.items()], counted
        assert (listed, every) == (sorted(tally), sorted(CROPS)), (listed, every)
        assert (top, none, distinct) == (counted[:2], [], 4), (top, none, distinct)
        (sql,) = watch.statements
        assert 'jsonb_agg' not in sql and sql.count('UNION ALL') == 5, sql
        # One row of single values, then an item a row: 4 + 4 + 12 + 2 + 0.
        assert [count for statement, count in fetched if statement == sql] == [1 + 4 + 4 + 12 + 2], fetched

    world(scenario)


def test_text_too_large_to_be_a_number_is_text(world):
    """A hex id such as 0347677e05719710 reads as a number with an exponent and overflows when cast: it is counted and
    listed as the text it is, and the number operations pass over it."""

    async def scenario(w: World):
        await _plant(w, private=False)
        hex_id = '0347677e05719710'
        extra = [
            {'filepath': 'hex-1.jpg', 'metadata': {'code': hex_id, 'brightness': hex_id}},
            {'filepath': 'hex-2.jpg', 'metadata': {'code': hex_id}},
            {'filepath': 'hex-3.jpg', 'metadata': {'code': '12'}},
            {'filepath': 'hex-4.jpg', 'metadata': {'code': 'abc'}},
        ]
        await datasets.add_samples(TRIALS, {'samples': extra}, user=USER)
        specs = [
            {'field': 'code', 'op': 'count_values'},
            {'field': 'code', 'op': 'bounds'},
            {'field': 'brightness', 'op': 'count_values', 'limit': 1},
            {'field': 'brightness', 'op': 'bounds'},
            {'field': 'brightness', 'op': 'mean'},
            {'field': 'brightness', 'op': 'quantiles', 'quantiles': [0.5]},
            {'field': 'brightness', 'op': 'histogram', 'bins': 4},
        ]
        for request in ([spec] for spec in specs):  # each alone, then all together
            answer = await w.api(POWER, 'POST', f'/api/datasets/{TRIALS}/aggregate', json={'aggregations': request})
            assert answer.status_code == 200, (request, answer.text)
        answer = await w.api(POWER, 'POST', f'/api/datasets/{TRIALS}/aggregate', json={'aggregations': specs})
        assert answer.status_code == 200, answer.text
        codes, code_bounds, _, bounds, mean, _, histogram = (result['value'] for result in answer.json()['results'])
        # Ties: a number in its number's order, then the rest (the hex id among them) by text.
        assert codes == [{'value': hex_id, 'count': 2}, {'value': '12', 'count': 1}, {'value': 'abc', 'count': 1}]
        assert code_bounds == [12.0, 12.0], code_bounds
        assert bounds == [min(BRIGHTNESS), max(BRIGHTNESS)], bounds
        assert mean == pytest.approx(sum(BRIGHTNESS) / len(BRIGHTNESS)), mean
        assert sum(bucket['count'] for bucket in histogram) == len(BRIGHTNESS), histogram
        summary = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='code'))
        assert summary['values'][0] == {'value': hex_id, 'count': 2}, summary

    world(scenario)
