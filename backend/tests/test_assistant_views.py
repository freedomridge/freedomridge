"""Working views, value lists and their tools (docs/ASSISTANT.md, "Working views"), against a disposable PostgreSQL:
make_view, refine_view, field_values, field_summary and get_samples, the people's endpoints, links and housekeeping.
"""

import csv
import io
import os
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from backend.assistant import store
from backend.routers import datasets
from backend.tests import test_assistant_core
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_assistant_core import ADMIN, CLOSED, OTHER, POWER, TRIALS, World, refused
from backend.tests.test_field_registry_postgres import USER

registry_database = test_assistant_core.registry_database
world = test_assistant_core.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

ADDRESS = 'https://data.example.org'
CROPS = ['wheat', 'barley', 'wheat', 'oats', 'wheat', 'barley', 'rye', 'wheat', 'oats', 'wheat', 'barley', 'wheat']
SERIALS = ['SN-7', 'SN-3', 'SN-7', 'SN-9', 'SN-1', 'SN-3', 'SN-7', 'SN-2', 'SN-9', 'SN-7', 'SN-3', 'SN-5']


def _samples() -> list[dict[str, Any]]:
    return [
        {
            'filepath': f'plot-{n}.jpg',
            'tags': ['dark'] if n % 3 == 0 else ['bright'],
            'metadata': {
                'crop': CROPS[n],
                'brightness': 10 * n + 5,
                'serial': SERIALS[n],
                'taken_at': f'2026-0{1 + n % 9}-15T12:00:00Z',
            },
        }
        for n in range(len(CROPS))
    ]


async def _plant(w: World, *, private: bool = True) -> None:
    await w.switch_on(power_user=True)
    await datasets.add_samples(TRIALS, {'samples': _samples()}, user=USER)
    if private:
        marked = await w.api(
            ADMIN,
            'PATCH',
            f'/api/datasets/{TRIALS}/fields/serial/description',
            json={'values_private': True},
        )
        assert marked.status_code == 200, marked.text
    await run_refresh(w.database, TRIALS)


def ok(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer['ok'] is True, answer
    return answer['result']


@pytest.fixture(autouse=True)
def _address(monkeypatch):
    monkeypatch.setenv('FRIDGE_PUBLIC_URL', ADDRESS)


def test_make_view_counts_and_writes_its_stages_and_a_q_link(world):
    async def scenario(w: World):
        await _plant(w)
        view = ok(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop = "wheat" AND brightness >= 40'))
        assert view['count'] == sum(1 for n, crop in enumerate(CROPS) if crop == 'wheat' and 10 * n + 5 >= 40)
        assert view['view'].startswith('wv_') and view['dataset'] == TRIALS
        assert [stage['n'] for stage in view['stages']] == [1, 2]
        assert view['filter'] == ' AND '.join(stage['text'] for stage in view['stages'])
        link = urlparse(view['link'])
        assert f'{link.scheme}://{link.netloc}' == ADDRESS and link.path == f'/datasets/{TRIALS}'
        assert parse_qs(link.query)['q'] == [view['filter']] and 'link_note' not in view
        whole = ok(await w.tool(POWER, 'make_view', dataset=TRIALS))
        assert whole['count'] == len(CROPS) and whole['stages'] == []
        # Text the server can't take: invalid with its details, and no view made.
        before = await w.database.fetchval('SELECT count(*) FROM assistant_views')
        bad = await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop = ')
        refused(bad, 'invalid')
        assert {'line', 'column', 'suggestions'} <= set(bad['error']['details'])
        assert await w.database.fetchval('SELECT count(*) FROM assistant_views') == before

    world(scenario)


def test_refine_view_makes_a_new_view_and_leaves_the_old_one(world):
    async def scenario(w: World):
        await _plant(w)
        first = ok(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop = "wheat"'))
        dark = ok(await w.tool(POWER, 'refine_view', view=first['view'], add='tags = "dark"'))
        assert dark['from'] == first['view'] and len(dark['stages']) == 2
        assert dark['count'] == sum(1 for n, crop in enumerate(CROPS) if crop == 'wheat' and n % 3 == 0)
        negated = ok(await w.tool(POWER, 'refine_view', view=dark['view'], negate=[2]))
        assert negated['count'] == 6 - dark['count'] and negated['stages'][1]['text'].startswith('NOT (')
        twice = ok(await w.tool(POWER, 'refine_view', view=negated['view'], negate=[2]))
        assert twice['count'] == dark['count']
        removed = ok(await w.tool(POWER, 'refine_view', view=dark['view'], remove=[1]))
        assert removed['count'] == sum(1 for n in range(len(CROPS)) if n % 3 == 0)
        again = ok(await w.tool(POWER, 'field_summary', view=first['view'], field='crop'))
        assert again['samples_with_value'] == 6  # the first view, as it was
        assert 'stage 3' in refused(await w.tool(POWER, 'refine_view', view=first['view'], remove=[3]), 'invalid')
        refused(await w.tool(POWER, 'refine_view', view=first['view']), 'invalid')

    world(scenario)


def test_a_handle_is_its_users_and_reads_as_the_user_now(world):
    async def scenario(w: World):
        await _plant(w)
        await w.share(TRIALS, OTHER, 'view')
        view = ok(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop = "oats"'))
        assert '30 days' in refused(await w.tool(OTHER, 'get_samples', view=view['view']), 'not_found')
        assert '30 days' in refused(await w.tool(POWER, 'get_samples', view='wv_nothing'), 'not_found')
        await w.database.execute('DELETE FROM dataset_access WHERE dataset_id = $1 AND user_id = $2', TRIALS, POWER)
        refused(await w.tool(POWER, 'get_samples', view=view['view']), 'refused')
        refused(await w.tool(POWER, 'make_view', dataset=CLOSED), 'refused')

    world(scenario)


def test_field_values_lists_counts_keeps_the_list_and_filters_by_it(world):
    async def scenario(w: World):
        await _plant(w, private=False)
        values = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='crop', limit=2))
        assert values['values'] == [{'value': 'wheat', 'count': 6}, {'value': 'barley', 'count': 3}]
        assert values['distinct'] == 4 and values['samples_with_value'] == len(CROPS) and values['complete'] is True
        assert values['list'].startswith('vl_') and values['download'].startswith(ADDRESS)
        searched = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='crop', search='AR'))
        assert [item['value'] for item in searched['values']] == ['barley'] and searched['distinct'] == 1
        tags = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='tags'))
        assert {item['value']: item['count'] for item in tags['values']} == {'bright': 8, 'dark': 4}
        whole = ok(await w.tool(POWER, 'make_view', dataset=TRIALS))
        kept = ok(
            await w.tool(
                POWER,
                'refine_view',
                view=whole['view'],
                not_in_list={'field': 'crop', 'list': searched['list']},
            ),
        )
        assert kept['count'] == len(CROPS) - 3 and 'link_note' not in kept  # a short list writes as text
        refused(await w.tool(POWER, 'field_values', dataset=TRIALS, field='taken_at'), 'invalid')

    world(scenario)


def test_a_list_over_the_limit_is_not_kept_for_filtering_and_still_downloads(world, monkeypatch):
    monkeypatch.setattr(store, 'LIST_LIMIT', 3)

    async def scenario(w: World):
        await _plant(w, private=False)
        values = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='serial', limit=50))
        assert values['list'] is None and values['distinct'] == 6 and '6' in values['list_note']
        handle = values['download'].rsplit('/', 2)[-2]
        rows = list(csv.reader(io.StringIO((await w.api(POWER, 'GET', f'/api/assistant/lists/{handle}/csv')).text)))
        assert rows[0] == ['value', 'count'] and len(rows) == 7

    world(scenario)


def test_a_private_field_is_filtered_by_its_list_and_never_shown(world):
    async def scenario(w: World):
        await _plant(w)
        values = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='serial'))
        assert values['values'] == [] and values['distinct'] == 6 and values['samples_with_value'] == len(CROPS)
        assert values['list'] and values['download']
        assert 'kept from the assistant' in refused(
            await w.tool(POWER, 'field_values', dataset=TRIALS, field='serial', search='SN'),
            'refused',
        )
        whole = ok(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop = "wheat"'))
        listed = ok(
            await w.tool(POWER, 'refine_view', view=whole['view'], in_list={'field': 'serial', 'list': values['list']}),
        )
        assert listed['count'] == 6 and 'SN-' not in str(listed)
        assert listed['stages'][1]['text'] == 'serial in (6 values kept from the assistant)'
        assert '?w=' in listed['link'] and listed['link_note']
        excluded = ok(await w.tool(POWER, 'refine_view', view=listed['view'], negate=[2]))
        assert excluded['count'] == 0
        # Questioning it otherwise is refused, and makes no view.
        for text in ('serial = "SN-7"', 'serial != "SN-3"', 'serial contains "SN"', 'serial in ("SN-1", "SN-2")'):
            refused(await w.tool(POWER, 'make_view', dataset=TRIALS, filter=text), 'refused')
        refused(await w.tool(POWER, 'get_samples', dataset=TRIALS, sort_by='serial'), 'refused')
        summary = ok(await w.tool(POWER, 'field_summary', dataset=TRIALS, field='serial'))
        assert summary == {
            'field': 'serial',
            'type': 'text',
            'unit': None,
            'samples_with_value': len(CROPS),
            'samples_without': 0,
            'distinct': 6,
        }
        # The person's download has the values.
        handle = values['list']
        rows = dict(csv.reader(io.StringIO((await w.api(POWER, 'GET', f'/api/assistant/lists/{handle}/csv')).text)))
        assert rows['SN-7'] == '4' and rows['value'] == 'count'
        assert (await w.api(OTHER, 'GET', f'/api/assistant/lists/{handle}/csv')).status_code == 404

    world(scenario)


def test_field_summary_of_a_number_and_a_datetime(world):
    async def scenario(w: World):
        await _plant(w)
        numbers = ok(await w.tool(POWER, 'field_summary', dataset=TRIALS, field='brightness'))
        assert numbers['min'] == 5 and numbers['max'] == 115 and numbers['mean'] == 60
        assert set(numbers['quantiles']) == {'p5', 'p25', 'p50', 'p75', 'p95'}
        assert sum(bucket['count'] for bucket in numbers['histogram']) == len(CROPS) <= 20 * len(numbers['histogram'])
        dates = ok(await w.tool(POWER, 'field_summary', dataset=TRIALS, field='taken_at'))
        assert dates['type'] == 'datetime' and 'mean' not in dates
        assert dates['min'].startswith('2026-01-15') and dates['max'].startswith('2026-09-15')

    world(scenario)


def test_get_samples_shows_fields_only_when_allowed(world):
    async def scenario(w: World):
        await _plant(w)
        page = ok(await w.tool(POWER, 'get_samples', dataset=TRIALS, limit=5, offset=10))
        assert page['total'] == len(CROPS) and page['next_offset'] is None and len(page['samples']) == 2
        assert (
            set(page['samples'][0]) == {'id', 'link'}
            and f'?sample={page["samples"][0]["id"]}' in page['samples'][0]['link']
        )
        assert 'administrator' in refused(
            await w.tool(POWER, 'get_samples', dataset=TRIALS, fields=['crop']),
            'refused',
        )
        await w.api(ADMIN, 'PATCH', '/api/admin/assistant', json={'sample_fields': True})
        page = ok(await w.tool(POWER, 'get_samples', dataset=TRIALS, fields=['crop', 'serial', 'filepath'], limit=1))
        assert page['samples'][0]['crop'] == CROPS[0] and page['samples'][0]['filepath'] == 'plot-0.jpg'
        assert page['withheld'] == ['serial'] and 'serial' not in page['samples'][0]

    world(scenario)


def test_the_view_endpoint_is_for_its_user_and_shows_the_values(world):
    async def scenario(w: World):
        await _plant(w)
        await w.share(TRIALS, OTHER, 'view')
        values = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='serial'))
        whole = ok(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop = "wheat"'))
        view = ok(
            await w.tool(POWER, 'refine_view', view=whole['view'], in_list={'field': 'serial', 'list': values['list']}),
        )
        opened = await w.api(POWER, 'GET', f'/api/assistant/views/{view["view"]}')
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body['dataset_id'] == TRIALS and len(body['stages']) == 2 and body['expires_at'] > body['created_at']
        assert sorted(body['stages'][1]['filter']['value']) == sorted(set(SERIALS))
        assert body['stages'][0]['filter'] == {'field': 'crop', 'operator': '=', 'value': 'wheat'}
        other = await w.api(OTHER, 'GET', f'/api/assistant/views/{view["view"]}')
        assert other.status_code == 404 and "someone else's conversation" in other.json()['detail']
        gone = await w.api(POWER, 'GET', '/api/assistant/views/wv_nothing')
        assert gone.status_code == 404 and gone.json()['detail'] == (
            "This link's view doesn't exist or has expired: the assistant keeps the views it makes for 30 days after "
            'they were last used.'
        )

    world(scenario)


def test_housekeeping_and_deletion_remove_views_and_lists(world):
    async def scenario(w: World):
        await _plant(w, private=False)
        values = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='crop'))
        view = ok(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop exists'))
        await w.database.execute("UPDATE assistant_views SET used_at = now() - interval '31 days'")
        await w.database.execute("UPDATE assistant_lists SET used_at = now() - interval '31 days'")
        assert await store.prune(w.database.pool) == 2
        assert await w.database.fetchval('SELECT count(*) FROM assistant_lists WHERE handle = $1', values['list']) == 0
        refused(await w.tool(POWER, 'get_samples', view=view['view']), 'not_found')
        ok(await w.tool(POWER, 'make_view', dataset=TRIALS))
        await w.database.execute('DELETE FROM datasets WHERE id = $1', TRIALS)
        assert await w.database.fetchval('SELECT count(*) FROM assistant_views') == 0

    world(scenario)


def test_field_values_reads_a_page_at_a_time_and_every_page_names_one_list(world):
    async def scenario(w: World):
        await _plant(w, private=False)
        first = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='serial', limit=2))
        second = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='serial', limit=2, offset=2))
        last = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='serial', limit=2, offset=4))
        assert (first['offset'], first['next_offset'], second['next_offset'], last['next_offset']) == (0, 2, 4, None)
        assert first['list'] == second['list'] == last['list'] and first['download'] == last['download']
        pages = [item['value'] for page in (first, second, last) for item in page['values']]
        assert pages == ['SN-7', 'SN-3', 'SN-9', 'SN-1', 'SN-2', 'SN-5']  # commonest first, ties by value
        other = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='serial', search='SN-7'))
        assert other['list'] != first['list']  # another question, another list

    world(scenario)


async def _with_objects(w: World) -> tuple[list[int], list[int]]:
    """Six images and, as the object tests make them, objects linked to the first four (two weeds, two stones), with
    fields only objects carry (label, confidence). Answers the image ids and the object ids."""
    from backend.tests.test_object_parent_column import add, ids

    await w.switch_on(power_user=True)
    await add(
        TRIALS,
        [(None, {'crop': CROPS[n], 'brightness': 10 * n + 5, 'serial': SERIALS[n]}) for n in range(6)],
        'img',
    )
    images = await ids(w.database, TRIALS, 'img')
    kinds = [
        ('object', {'parent_id': image, 'label': label, 'confidence': 0.5 + n / 10})
        for n, (image, label) in enumerate(zip(images[:4], ['weed', 'weed', 'stone', 'stone']))
    ]
    await add(TRIALS, kinds, 'obj')
    objects = await ids(w.database, TRIALS, 'obj')
    marked = await w.api(
        ADMIN,
        'PATCH',
        f'/api/datasets/{TRIALS}/fields/serial/description',
        json={'values_private': True},
    )
    assert marked.status_code == 200, marked.text
    await run_refresh(w.database, TRIALS)
    return images, objects


def test_a_view_is_of_the_datasets_images_and_never_its_object_rows(world):
    from backend.assistant.grain import image_grain

    async def scenario(w: World):
        images, objects = await _with_objects(w)
        described = ok(await w.tool(POWER, 'describe_dataset', dataset=TRIALS))
        whole = ok(await w.tool(POWER, 'make_view', dataset=TRIALS))
        assert whole['count'] == described['dataset']['samples'] == len(images)
        serials = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='serial'))
        views = [whole]
        for text in ('crop = "wheat"', 'label = "weed"', 'NOT (crop = "wheat")', 'brightness < 30', 'confidence > 0.6'):
            views.append(ok(await w.tool(POWER, 'make_view', dataset=TRIALS, filter=text)))
        views.append(
            ok(
                await w.tool(
                    POWER,
                    'refine_view',
                    view=whole['view'],
                    in_list={'field': 'serial', 'list': serials['list']},
                ),
            ),
        )
        for view in views:
            # What the app's image grid counts for the same stages, as the person opens the view's link.
            opened = (await w.api(POWER, 'GET', f'/api/assistant/views/{view["view"]}')).json()
            filters = image_grain([stage['filter'] for stage in opened['stages']], True)
            grid = await w.api(
                POWER,
                'POST',
                f'/api/datasets/{TRIALS}/samples/query',
                json={'filters': filters, 'limit': 50},
            )
            assert view['count'] == grid.json()['total'], (view['filter'], view['count'], grid.json()['total'])
            page = ok(await w.tool(POWER, 'get_samples', view=view['view'], limit=50))
            listed = [sample['id'] for sample in page['samples']]
            assert page['total'] == view['count'] and set(listed) <= set(images), view['filter']
            assert listed == sorted(item['id'] for item in grid.json()['items'])
        # A field only objects carry: an image passes through one of its objects, as on the app's image grid.
        weeds = views[2]
        assert weeds['count'] == 2
        labels = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='label'))
        assert (labels['values'], labels['samples_with_value']) == ([], 0)  # the images themselves have no label
        crops = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='crop'))
        assert sum(item['count'] for item in crops['values']) == len(images)
        bright = ok(await w.tool(POWER, 'field_summary', dataset=TRIALS, field='brightness'))
        assert (bright['samples_with_value'], bright['samples_without']) == (len(images), 0)
        assert sum(bucket['count'] for bucket in bright['histogram']) == len(images)
        assert not set(objects) & {
            s['id'] for s in ok(await w.tool(POWER, 'get_samples', dataset=TRIALS, limit=50))['samples']
        }

    world(scenario)


def test_a_users_views_and_lists_are_bounded(world, monkeypatch):
    monkeypatch.setattr(store, 'MAX_VIEWS', 3)
    monkeypatch.setattr(store, 'MAX_LISTS', 2)

    async def scenario(w: World):
        await _plant(w, private=False)
        await w.share(TRIALS, OTHER, 'view')
        made = [
            ok(await w.tool(POWER, 'make_view', dataset=TRIALS, filter=f'brightness > {n}'))['view'] for n in range(5)
        ]
        ok(await w.tool(OTHER, 'make_view', dataset=TRIALS))  # another user's rows count for that user alone
        kept = await w.database.fetch('SELECT handle FROM assistant_views WHERE user_id = $1', POWER)
        assert {row['handle'] for row in kept} == set(made[2:])
        assert '30 days' in refused(await w.tool(POWER, 'get_samples', view=made[0]), 'not_found')  # as an expired one
        # Lists: a list a kept view names lives as long as the view, and counts toward the ceiling.
        named = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='crop'))['list']
        ok(await w.tool(POWER, 'refine_view', view=made[4], in_list={'field': 'crop', 'list': named}))
        others = [
            ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='serial', search=f'SN-{n}'))['list']
            for n in (1, 2, 3, 5)
        ]
        lists = {
            row['handle']
            for row in await w.database.fetch('SELECT handle FROM assistant_lists WHERE user_id = $1', POWER)
        }
        assert lists == {named, others[-1]}, lists

    world(scenario)


def test_the_download_never_runs_a_formula_in_a_spreadsheet(world):
    async def scenario(w: World):
        await w.switch_on(power_user=True)
        notes = ['=HYPERLINK("http://example.invalid","x")', '+cmd', '-dash', '@at', '\ttab', '\rret', 'plain']
        rows = [
            {'filepath': f'n-{n}.jpg', 'metadata': {'note': note, 'level': -5 if n % 2 else 3}}
            for n, note in enumerate(notes)
        ]
        await datasets.add_samples(TRIALS, {'samples': rows}, user=USER)
        await run_refresh(w.database, TRIALS)
        for field in ('note', 'level'):
            handle = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field=field))['list']
            text = (await w.api(POWER, 'GET', f'/api/assistant/lists/{handle}/csv')).text
            cells = {row[0] for row in csv.reader(io.StringIO(text))} - {'value'}
            if field == 'note':
                assert cells == {"'" + note for note in notes[:-1]} | {'plain'}, cells
            else:
                assert cells == {'-5', '3'}, cells  # numbers as they are

    world(scenario)


def test_a_tools_count_has_a_time_limit_for_every_user(world, monkeypatch):
    import time

    from backend import database as database_module
    from backend import request_limits

    monkeypatch.setattr(request_limits, 'ASSISTANT_STATEMENT_SECONDS', 1.0)

    async def scenario(w: World):
        import asyncpg

        await _plant(w, private=False)
        # A pool as the server makes one (database.Database.connect), so the request limits apply as they do there.
        path = await w.database.fetchval("SELECT current_setting('search_path')")
        served = await asyncpg.create_pool(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            min_size=1,
            max_size=4,
            init=database_module.init_connection,
            connection_class=request_limits.RequestConnection,
            server_settings={'search_path': path, 'statement_timeout': '10000'},
        )
        # The routers' database (the test's, here) and the app's both read through it.
        original = w.database.pool
        monkeypatch.setattr(database_module.db, 'pool', request_limits.RequestPool(served))
        w.database.pool = request_limits.RequestPool(served)
        try:
            async with original.acquire() as holder:
                async with holder.transaction():
                    await holder.execute('LOCK TABLE samples IN ACCESS EXCLUSIVE MODE')
                    started = time.monotonic()
                    answer = await w.tool(POWER, 'make_view', dataset=TRIALS, filter='crop = "wheat"')
                    took = time.monotonic() - started
        finally:
            w.database.pool = original
            await served.close()
        assert refused(answer, 'too_slow') and answer['error']['retryable'] is False
        assert 'result' not in answer  # never a partial count
        assert 0.9 < took < 5, took  # the tool's limit (1 s here), not the pool's 10 s
        # A person's own request keeps the limits it has (none for a power user).
        assert (
            request_limits.statement_seconds(request_limits.RequestContext(user={'id': POWER, 'role': 'power_user'}))
            is None
        )

    world(scenario)


def test_whether_a_dataset_has_objects_is_asked_once_per_tool_call(world, monkeypatch):
    from backend.assistant import inner

    real = inner.request
    asked: list[Any] = []

    async def counted(identity, listed, method, template, **kwargs):
        body = kwargs.get('body') or {}
        if isinstance(body, dict) and body.get('filters') == {
            'field': 'media_type',
            'operator': '=',
            'value': 'object',
        }:
            asked.append(body)
        return await real(identity, listed, method, template, **kwargs)

    monkeypatch.setattr(inner, 'request', counted)

    async def scenario(w: World):
        await _plant(w, private=False)
        ok(await w.tool(POWER, 'field_summary', dataset=TRIALS, field='brightness'))
        assert len(asked) == 1 and asked[0]['limit'] == 1 and asked[0]['skip_count'] is True

    world(scenario)


def test_one_view_is_bounded_in_stages_and_in_list_values(world, monkeypatch):
    from backend.assistant import working

    monkeypatch.setattr(working, 'MAX_STAGES', 4)
    monkeypatch.setattr(working, 'MAX_LIST_VALUES', 7)

    async def scenario(w: World):
        await _plant(w, private=False)
        # Stages: a refine loop stops at the cap, makes no view past it, and leaves the last good one usable.
        view = ok(await w.tool(POWER, 'make_view', dataset=TRIALS, filter='brightness > 0'))
        for n in range(1, 4):
            view = ok(await w.tool(POWER, 'refine_view', view=view['view'], add=f'brightness > {n}'))
        assert len(view['stages']) == 4
        before = await w.database.fetchval('SELECT count(*) FROM assistant_views')
        words = refused(await w.tool(POWER, 'refine_view', view=view['view'], add='crop exists'), 'invalid')
        assert 'at most 4 stages' in words and 'remove' in words
        assert await w.database.fetchval('SELECT count(*) FROM assistant_views') == before
        assert ok(await w.tool(POWER, 'get_samples', view=view['view']))['total'] == view['count']
        five = 'brightness > 1 AND brightness > 2 AND brightness > 3 AND brightness > 4 AND brightness > 5'
        refused(await w.tool(POWER, 'make_view', dataset=TRIALS, filter=five), 'invalid')
        # Removing and negating never grow a view: never refused for its size, even past a lowered cap.
        monkeypatch.setattr(working, 'MAX_STAGES', 2)
        ok(await w.tool(POWER, 'refine_view', view=view['view'], negate=[1]))
        ok(await w.tool(POWER, 'refine_view', view=view['view'], remove=[1]))
        # List values: a list named in two stages counts twice.
        monkeypatch.setattr(working, 'MAX_STAGES', 50)
        crops = ok(await w.tool(POWER, 'field_values', dataset=TRIALS, field='crop'))  # 4 values
        whole = ok(await w.tool(POWER, 'make_view', dataset=TRIALS))
        once = ok(
            await w.tool(POWER, 'refine_view', view=whole['view'], in_list={'field': 'crop', 'list': crops['list']}),
        )
        words = refused(
            await w.tool(POWER, 'refine_view', view=once['view'], not_in_list={'field': 'crop', 'list': crops['list']}),
            'invalid',
        )
        assert '7 values' in words and '8' in words and 'narrow' in words
        assert ok(await w.tool(POWER, 'field_summary', view=once['view'], field='crop'))['samples_with_value'] == 12

    world(scenario)
