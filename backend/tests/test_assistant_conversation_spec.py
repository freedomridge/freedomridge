"""The conversation the assistant is judged by, played as tool calls (milestone 1).

The owner's five turns: (1) "I want all the images in Kansas", (2) "can you tell me all the serial numbers", (3) "I
want these serial numbers", (4) "I want to remove the dark images" (the assistant offers brightness or digital gain),
(5) "remove equipment images" (a concept). No model is involved: the script plays the calls a sensible assistant
would make for each turn, in order, each argument taken only from the person's words and from earlier answers
(handles, keys, names, numbers), and checks every answer against what this file planted. It shows the tools are
enough for the conversation and that each answer carries what a person needs: the count, the filter as text, and a
link that opens exactly those samples.

Written from the contracts (m1-contract-assistant-core, m1-contract-working-views, m1-contract-finding-tools), not
from the code. Every expected answer is computed from the planted rows; nothing is typed in. The places come
from this file's own boundary set (a state named Kansas, and a country named Kansas too), loaded with the region
loader as the region tests load theirs. Every answer's JSON is at most 20 KB, and nothing the script does writes
(the access tests' snapshot, taken around the whole script).

What the script found, now in the contracts, and what it checks for each:
- Each place find_place gives carries its own `use`: the script takes the state's from the first answer.
- search_similar answers a working view (the searched view's stages, then one stage of the ids found, numbered
  `found_stage`): "remove the ones like this" is refine_view with `negate: [found_stage]`, and the found stage's words
  are the server's `id in (...)`, never the prompt.
- field_values pages with `offset` and `next_offset`, one list handle for every page: in the variant with more than 50
  serial numbers in Kansas, turn 2 reads them all a page at a time.
- describe_dataset's `search` takes several words, a field matching when any is found: one call with "dark bright
  gain" finds both brightness and digital gain (and a single-word call still works).
- With serial numbers kept from the assistant, "these" (3a, 3b) works through the list handle, and the person naming
  three of them (3c) is refused, as the rule says.

Readings where the contracts are silent:
- The conversation goes on from turn 3's second reading (every sample anywhere with one of the serial numbers), the
  view the later turns refine. Turn 4 cuts at the brightness summary's p25, keeping the samples at or above it.
- concept_preview's `just_above` are the samples above the cut nearest it (lowest scores first), `just_below` those
  at or under it nearest it (highest first); unscored samples are in neither. A score is compared within 1e-6 (a
  score may be kept in single precision).
- search_similar ranks by the cosine similarity of the field's vectors to the prompt's; the text model is stood in
  for where main's own text-search tests stand in for it (the `encode_text` the search and concept routes call),
  returning the "equipment" direction of the planted vectors.
- `in_list` stages select what the app's own `<field> in (...)` selects for the list's values (the working-views
  contract), the values read from the list's download by its person.
- A view's `filter` is its stages' texts joined with " AND " (as in the views spec).
"""

import asyncio
import importlib
import json
import math
import os
import time
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

import pytest

from backend import encoders, statistics
from backend.job_heartbeat import heartbeat_connector
from backend.job_runner import ServerWorker
from backend.routers import concepts as concepts_router
from backend.routers import search as search_router
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN, POWER
from backend.tests.test_assistant_core_spec import add, open_door, snapshot
from backend.tests.test_assistant_views_spec import (
    KEPT,
    VIEW_KEYS,
    api_ids,
    call,
    check_view,
    csv_rows,
    having,
    image_grain,
    opened,
    parsed,
    stages_of,
    tally,
    view_ids,
)
from backend.tests.test_regions_spec import PROPERTIES, SET, Regions, polygon, ring

database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(
    not (os.getenv('FRIDGE_TEST_POSTGRES_DSN') and os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')),
    reason='Requires disposable PostgreSQL',
)

MAIN, STATES, KEPT_SERIALS, NO_CONCEPT, WITH_OBJECTS = 900, 901, 902, 903, 904
STRADDLE = 11  # with objects: the image whose second object scores on the other side of the cut from it
ANSWER_LIMIT = 20_000  # bytes of JSON in any one answer
THRESHOLD = 0.62
GAIN_TEXT = "The camera's digital gain: higher in dark scenes."
EQUIPMENT_DIRECTION = [0.0, 0.0, 1.0, 0.0]  # what the stand-in text model makes of "equipment"

PRAIRIA = {'iso_a2': 'XP', 'adm0_a3': 'XPR', 'admin': 'Prairia'}
KANSAS_LAND = {'iso_a2': 'XK', 'adm0_a3': 'XKS', 'admin': 'Kansas'}  # a country named Kansas as well
FEATURES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        'XP-KS',
        {**PRAIRIA, 'iso_3166_2': 'XP-KS', 'adm1_code': 'XPR-01', 'name_en': 'Kansas', 'name': 'Kansas'}
        | {'region': 'Plains'},
        polygon(ring(0, 0, 10, 10)),
    ),
    (
        'XP-NE',
        {**PRAIRIA, 'iso_3166_2': 'XP-NE', 'adm1_code': 'XPR-02', 'name_en': 'Nebraska', 'name': 'Nebraska'}
        | {'region': 'Plains'},
        polygon(ring(10, 0, 20, 10)),
    ),
    (
        'XK-WH',
        {**KANSAS_LAND, 'iso_3166_2': 'XK-WH', 'adm1_code': 'XKS-01', 'name_en': 'Wheatland', 'name': 'Wheatland'},
        polygon(ring(40, 0, 50, 10)),
    ),
]
STATE_TEXT = {'kansas': 'Kansas', 'nebraska': 'Nebraska', 'wheatland': 'Wheatland', 'nowhere': 'Ontario'}


def planted(many_serials: bool = False) -> list[dict[str, Any]]:
    """The samples: 14 in Kansas (the state), 9 in Nebraska, 5 in Wheatland (in the country named Kansas), 4 outside
    every region, 3 with no location. Serial numbers repeat within Kansas and some appear elsewhere too. With
    `many_serials`, 60 more Kansas samples, each with a serial number of its own (more than a page of values)."""
    rows: list[dict[str, Any]] = []

    def put(where: str, point: Optional[tuple[float, float]], serial: Optional[str]) -> None:
        n = len(rows)
        brightness = 5.0 + 7 * n
        row: dict[str, Any] = {
            'n': n,
            'where': where,
            'point': point,
            'brightness': brightness,
            'digital_gain': round(2 + (250 - brightness) / 10, 2),  # higher in dark scenes
            'equipment': None if where == 'unplaced' else ((n * 37) % 100) / 100 + 0.005,
            'clip': [math.cos(n), math.sin(n), ((n * 29) % 50) / 50 + 0.01, 0.0],
        }
        if serial is not None:
            row['serial_number'] = serial
        if where in STATE_TEXT:
            row['state'] = STATE_TEXT[where]
        rows.append(row)

    for k in range(14):
        put('kansas', (1 + 0.5 * k, 1 + 0.5 * k), f'SN-{k % 9 + 1:03d}')
    for k in range(60 if many_serials else 0):
        put('kansas', (0.5 + (k % 15) * 0.6, 8.0 + (k // 15) * 0.4), f'SN-K{k:03d}')
    for k in range(9):
        put('nebraska', (11.0 + k, 5.0), f'SN-{k + 6:03d}')
    for k in range(5):
        put('wheatland', (41.0 + k, 5.0), 'SN-002' if k == 0 else f'SN-{k + 15:03d}')
    for k in range(4):
        put('nowhere', (100.0, 50.0 + k), f'SN-{k + 20:03d}')
    for serial in ('SN-024', 'SN-003', None):
        put('unplaced', None, serial)
    return rows


ROWS = planted()
SERIALS = sorted({row['serial_number'] for row in ROWS if 'serial_number' in row})


class Planted:
    def __init__(self, dataset_id: int, ids: list[int], rows: list[dict[str, Any]], objects: bool = False):
        self.dataset_id, self.ids, self.planted = dataset_id, ids, rows  # the images' ids, by planted n
        self.objects = objects  # whether object rows sit under the images

    def of(self, keep: Callable[[dict[str, Any]], bool]) -> set[int]:
        return {self.ids[row['n']] for row in self.planted if keep(row)}

    def rows(self, ids: set[int]) -> list[dict[str, Any]]:
        return [row for row in self.planted if self.ids[row['n']] in ids]


async def load_boundaries(r: Regions) -> None:
    """This file's boundary set, through the region loader's command line."""
    collection = {
        'type': 'FeatureCollection',
        'features': [
            {'type': 'Feature', 'properties': {name: properties.get(name) for name in PROPERTIES}, 'geometry': shape}
            for _, properties, shape in FEATURES
        ],
    }
    path = r.folder / 'conversation-boundaries.geojson'
    path.write_text(json.dumps(collection))
    module = importlib.import_module('backend.regions')
    if hasattr(module, 'DATABASE_URL'):
        r.monkeypatch.setattr(module, 'DATABASE_URL', r.url)
    argv = ['load', str(path), '--set', SET, '--no-verify', '--set-version', 'conversation']
    assert await asyncio.to_thread(module.main, argv) == 0


async def count_statistics(r: Regions, dataset_id: int) -> None:
    """The statistics job for the dataset run to the end, as statistics_helpers.run_refresh runs it, with the worker's
    heartbeat on this test's own database."""
    job = await statistics.request_refresh(r.database.pool, dataset_id)
    worker = ServerWorker(
        r.database.pool,
        {statistics.KIND: statistics.handler(r.database.pool)},
        heartbeat_connect=heartbeat_connector(r.url),
        policies={statistics.KIND: statistics.POLICY},
    )
    deadline = time.monotonic() + 30
    while True:
        assert time.monotonic() < deadline, 'the statistics job did not finish within 30 seconds'
        await worker.run_once()
        state = await r.database.fetchval('SELECT state FROM jobs WHERE id = $1', job['id'])
        if state in ('succeeded', 'failed', 'cancelled'):
            assert state == 'succeeded', state
            return
        await asyncio.sleep(0)


async def plant(
    r: Regions,
    dataset_id: int,
    *,
    located: bool = True,
    state_field: bool = False,
    private: bool = False,
    concept: bool = True,
    many_serials: bool = False,
    objects: bool = False,
) -> Planted:
    rows = planted(many_serials)
    await r.dataset(dataset_id)

    def carried(row: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            key: row[key] for key in ('serial_number', 'brightness', 'digital_gain') if key in row
        }
        if state_field and 'state' in row:
            metadata['state'] = row['state']
        return metadata

    samples = []
    for row in rows:
        sample: dict[str, Any] = {'filepath': f'c{dataset_id}-{row["n"]:02d}.jpg', 'metadata': carried(row)}
        if located and row['point'] is not None:
            sample['location'] = {'type': 'Point', 'coordinates': list(row['point'])}
        samples.append(sample)
    await add(dataset_id, samples)
    by_path = {
        item['filepath']: item['id']
        for item in await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', dataset_id)
    }
    ids = [by_path[f'c{dataset_id}-{row["n"]:02d}.jpg'] for row in rows]
    children: list[Any] = []
    if objects:  # one or two under every image, carrying its fields with the same values, and two of their own
        await add(
            dataset_id,
            [
                {
                    'filepath': f'c{dataset_id}-{row["n"]:02d}-{k}.jpg',
                    'media_type': 'object',
                    'metadata': {
                        **carried(row),
                        'parent_id': ids[row['n']],
                        'label': 'crate' if k else 'tractor',
                        'area': 5.0 * (k + 1),
                    },
                }
                for row in rows
                for k in range(1 + row['n'] % 2)
            ],
        )
        children = await r.database.fetch(
            "SELECT id, parent_id, filepath FROM samples WHERE dataset_id = $1 AND media_type = 'object'",
            dataset_id,
        )
        assert {child['parent_id'] for child in children} == set(ids), children
    vectors = [{'sample_id': ids[row['n']], 'vector': row['clip'], 'field_name': 'clip'} for row in rows]
    assert (await r.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/embeddings', json=vectors)).is_success
    if concept:
        concept_id = await r.database.fetchval(
            'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
            "VALUES ($1, 'equipment', 'clip', 'seed', '[0,0,1,0]', $2) RETURNING id",
            dataset_id,
            THRESHOLD,
        )
        score_of = {ids[row['n']]: row['equipment'] for row in rows if row['equipment'] is not None}
        for child in children:  # an object scores as its image ...
            if child['parent_id'] in score_of:
                score_of[child['id']] = score_of[child['parent_id']]
        straddling = f'c{dataset_id}-{STRADDLE:02d}-1.jpg'  # ... but for one, on the other side of the cut
        for child in children:
            if child['filepath'] == straddling:
                score_of[child['id']] = 0.1 if rows[STRADDLE]['equipment'] > THRESHOLD else 0.9
        await r.database.execute(
            'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) '
            'SELECT $1, unnest($2::bigint[]), unnest($3::float8[])',
            concept_id,
            list(score_of),
            list(score_of.values()),
        )
    for field, body in (('digital_gain', {'description': GAIN_TEXT}), ('serial_number', {'values_private': private})):
        response = await r.call(ADMIN, 'PATCH', f'/api/datasets/{dataset_id}/fields/{field}/description', json=body)
        assert response.status_code == 200, response.text
    # One saved view, the power user's own.
    node = (await parsed(r, ADMIN, dataset_id, 'brightness >= 100'))['filter']
    stage = {'type': 'filter', 'description': 'brightness >= 100', 'filter': node['filters'][0]}
    await r.database.execute(
        'INSERT INTO views (dataset_id, user_id, name, slug, content, is_public) VALUES ($1, $2, $3, $4, $5, FALSE)',
        dataset_id,
        POWER,
        'Bright plots',
        f'bright-plots-{dataset_id}',
        {'filters': node, 'viewStages': [stage]},
    )
    await r.share(dataset_id, POWER, 'view')
    await count_statistics(r, dataset_id)
    return Planted(dataset_id, ids, rows, objects)


# --- The script ---


class Script:
    """The person's assistant: every call goes through here, and every answer is kept and checked to be small."""

    def __init__(self, r: Regions, user_id: int, planted_: Planted):
        self.r, self.user, self.p = r, user_id, planted_
        self.answers: list[dict[str, Any]] = []

    async def ask(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        answer = await call(self.user, name, arguments)
        size = len(json.dumps(answer))
        assert size <= ANSWER_LIMIT, (name, arguments, size)
        self.answers.append(answer)
        return answer

    async def say(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        answer = await self.ask(name, arguments)
        assert answer['ok'], (name, arguments, answer)
        return answer['result']

    async def shows(self, view: dict[str, Any], expected: set[int], refined: Optional[str] = None) -> None:
        """The count, the filter as text, and a link that opens exactly the view's samples."""
        check_view(view, self.p.dataset_id, refined)
        assert view['count'] == len(expected), (view, len(expected))
        assert view['filter'] == ' AND '.join(stage['text'] for stage in view['stages']), view
        assert await view_ids(self.user, view['view']) == expected, view['filter']
        assert await opened(self.r, self.user, view, grain=self.p.objects) == expected, view['link']

    async def page_shows(self, view: dict[str, Any], added: Optional[str] = None) -> set[int]:
        """What the dataset's page shows for the view's link, with the filter text `added` as one more stage: the
        app's own query, at the image grain on a dataset with objects."""
        d = self.p.dataset_id
        params = parse_qs(urlsplit(view['link']).query, keep_blank_values=True)
        if 'q' in params:
            text = params['q'][0]
            stages = (await parsed(self.r, self.user, d, text))['filters'] if text else []
        else:
            stages = await stages_of(self.r, self.user, view['view'])
        if added:
            stages = [*stages, *(await parsed(self.r, self.user, d, added))['filters']]
        node = {'logic': 'AND', 'filters': stages} if stages else None
        return await api_ids(self.r, self.user, d, image_grain(node) if self.p.objects else node)

    async def app_selects(self, field: str, operator: str, listed: dict[str, Any]) -> set[int]:
        """What the app's own `<field> <operator> (<the list's values>)` selects, for the list's person."""
        download = await self.r.call(self.user, 'GET', urlsplit(listed['download']).path)
        written = ', '.join(json.dumps(value) for value, _ in csv_rows(download))
        node = (await parsed(self.r, self.user, self.p.dataset_id, f'{field} {operator} ({written})'))['filter']
        return await api_ids(self.r, self.user, self.p.dataset_id, image_grain(node) if self.p.objects else node)


async def converse(s: Script, *, private: bool = False, concept: bool = True) -> dict[str, Any]:
    """The five turns. Returns the views made along the way."""
    p, d = s.p, s.p.dataset_id

    # Before anything: what the dataset holds.
    described = await s.say('describe_dataset', {'dataset': d, 'limit': 100})
    assert described['fields_total'] == len(described['fields']), described  # one page holds them all
    fields = {field['name']: field for field in described['fields']}

    # 1. "I want all the images in Kansas."
    in_kansas = p.of(lambda r: r['where'] == 'kansas')
    state = fields.get('state')
    if state is not None and state['type'] == 'text':
        text = 'state = "Kansas"'  # the dataset says where in its own words: no region needed
    else:
        assert described['samples_with_location'] == len(p.of(lambda r: r['point'] is not None)), described
        places = await s.say('find_place', {'text': 'Kansas'})
        assert places['loaded'] is True and places['total'] == 2, places
        assert sorted(place['kind'] for place in places['places']) == ['country', 'state'], places
        for place in places['places']:  # each with the text that keeps its own samples
            assert place['use'] == f'location within "{place["key"]}"', place
        chosen = next(place for place in places['places'] if place['kind'] == 'state')  # the person means the state
        assert (chosen['name'], chosen['country']) == ('Kansas', 'Prairia'), chosen
        text = chosen['use']
    kansas = await s.say('make_view', {'dataset': d, 'filter': text})
    await s.shows(kansas, in_kansas)

    # 2. "Can you tell me all the serial numbers?"
    serial_field = next(name for name in fields if 'serial' in name)
    pages: list[dict[str, Any]] = []
    offset: Optional[int] = 0
    while offset is not None:  # a page at a time, as many as there are
        arguments = {'view': kansas['view'], 'field': serial_field, 'limit': 50, 'offset': offset}
        pages.append(await s.say('field_values', arguments))
        assert pages[-1]['offset'] == offset, pages[-1]
        offset = None if private else pages[-1]['next_offset']  # a private field's values: none to page through
    serials = pages[0]
    kansas_rows = p.rows(in_kansas)
    counted = tally(kansas_rows, 'serial_number')
    assert (serials['distinct'], serials['samples_with_value']) == (len(counted), having(kansas_rows, 'serial_number'))
    assert [item for page in pages for item in page['values']] == ([] if private else counted), pages
    assert len(pages) == (1 if private else math.ceil(len(counted) / 50)), len(pages)
    assert len({page['list'] for page in pages}) == 1 and serials['list'].startswith('vl_'), pages
    assert all(page['complete'] is True for page in pages), pages

    # 3. "I want these serial numbers." (a) these, in Kansas: the same samples, by rule.
    listed = {'field': serial_field, 'list': serials['list']}
    same = await s.say('refine_view', {'view': kansas['view'], 'in_list': listed})
    assert in_kansas <= await s.app_selects(serial_field, 'in', serials)
    await s.shows(same, in_kansas & await s.app_selects(serial_field, 'in', serials), refined=kansas['view'])
    if private:
        assert same['stages'][-1]['text'] == f'{serial_field} in ({len(counted)} {KEPT})', same
    # (b) these, anywhere in the dataset: more than the Kansas samples.
    whole = await s.say('make_view', {'dataset': d})
    await s.shows(whole, set(p.ids))
    everywhere = await s.say('refine_view', {'view': whole['view'], 'in_list': listed})
    anywhere = await s.app_selects(serial_field, 'in', serials)
    assert anywhere > in_kansas, (len(anywhere), len(in_kansas))
    await s.shows(everywhere, anywhere, refined=whole['view'])
    # (c) three of them, named.
    if private:
        three = [row['serial_number'] for row in kansas_rows[:3]]  # the person's own words: the script has none
        written = f'{serial_field} in ({", ".join(json.dumps(value) for value in three)})'
        refused = await s.ask('refine_view', {'view': kansas['view'], 'add': written})
        assert not refused['ok'] and refused['error']['code'] == 'refused', refused
        assert serial_field in refused['error']['message'] and 'assistant' in refused['error']['message'].lower()
    else:
        three = [item['value'] for item in serials['values'][:3]]
        written = f'{serial_field} in ({", ".join(json.dumps(value) for value in three)})'
        named = await s.say('refine_view', {'view': kansas['view'], 'add': written})
        assert named['stages'][-1]['text'] == (await parsed(s.r, s.user, d, written))['parts'][0], named
        await s.shows(named, p.of(lambda r: r['where'] == 'kansas' and r.get('serial_number') in three), kansas['view'])
    current = everywhere

    # 4. "I want to remove the dark images." Find what speaks of darkness, offer it, cut where the person says.
    schema = (await s.r.call(s.user, 'GET', f'/api/datasets/{d}/schema')).json()

    def speaking_of(words: str) -> list[str]:
        """The schema's fields with any of the words in their name or description, in its order."""
        return [
            entry['name']
            for entry in schema
            if any(
                word in entry['name'].lower() or word in (entry.get('description') or '').lower()
                for word in words.lower().split()
            )
        ]

    for words in ('dark', 'dark bright gain'):
        page = await s.say('describe_dataset', {'dataset': d, 'search': words, 'limit': 100})
        assert [field['name'] for field in page['fields']] == speaking_of(words), (words, page['fields'])
    offered = {field['name'] for field in page['fields']}
    assert offered == {'brightness', 'digital_gain'}, offered  # both in one call, and nothing else speaks of darkness
    current_rows = p.rows(anywhere)
    summaries = {}
    for name in sorted(offered):
        summary = await s.say('field_summary', {'view': current['view'], 'field': name})
        values = [row[name] for row in current_rows]
        assert (summary['samples_with_value'], summary['samples_without']) == (len(values), 0), summary
        assert (summary['min'], summary['max']) == (min(values), max(values)), summary
        summaries[name] = summary
    cut = summaries['brightness']['quantiles']['p25']
    bright = await s.say('refine_view', {'view': current['view'], 'add': f'brightness >= {cut!r}'})
    lit = {p.ids[row['n']] for row in current_rows if row['brightness'] >= cut}
    assert 0 < len(lit) < len(anywhere), (cut, len(lit))
    await s.shows(bright, lit, refined=current['view'])

    # 5. "Remove equipment images."
    known = await s.say('list_concepts', {'dataset': d})
    named_equipment = [item for item in known['concepts'] if 'equipment' in item['name'].lower()]
    bright_rows = p.rows(lit)
    if concept:
        (equipment,) = named_equipment
        assert (equipment['threshold'], equipment['ready']) == (THRESHOLD, True), equipment
        assert equipment['use'] == f'concept "{equipment["name"]}" > {THRESHOLD}', equipment
        name = equipment['name']
        preview = await s.say('concept_preview', {'view': bright['view'], 'concept': name})
        assert (preview['concept'], preview['threshold'], preview['view_count']) == (name, THRESHOLD, len(lit)), preview
        assert (preview['keep'], preview['remove']) == (equipment['use'], f'not {equipment["use"]}'), preview
        # A view with stages: exact counts, and the nearest samples only for the whole dataset.
        assert (preview['just_above'], preview['just_below']) == (None, None) and preview['boundary_note'], preview
        above_ids = await s.page_shows(bright, f'concept "{name}" > {THRESHOLD}')
        below_ids = await s.page_shows(bright, f'concept "{name}" <= {THRESHOLD}')
        unscored = {p.ids[r['n']] for r in bright_rows if r['equipment'] is None}
        assert (preview['above'], preview['below'], preview['unscored']) == (
            len(above_ids),
            len(below_ids),
            len(unscored),
        ), preview  # unscored tells the model, before the filter, about the images without a score
        excess = preview['above'] + preview['below'] + preview['unscored'] - preview['view_count']
        if p.objects:
            # The straddling image counts on both sides; the note says how many do.
            assert p.ids[STRADDLE] in lit and excess >= 1 and str(excess) in preview['note'], preview
        else:
            assert excess == 0 and 'note' not in preview, preview
            assert above_ids == {p.ids[r['n']] for r in bright_rows if (r['equipment'] or 0) > THRESHOLD}
        # The whole dataset: the nearest samples each side of the cut, nearest first.
        whole_preview = await s.say('concept_preview', {'dataset': d, 'concept': name})
        everything = p.rows(set(p.ids))
        nearest_above = sorted(
            (r for r in everything if (r['equipment'] or 0) > THRESHOLD),
            key=lambda r: r['equipment'],
        )
        nearest_below = sorted(
            (r for r in everything if r['equipment'] is not None and r['equipment'] <= THRESHOLD),
            key=lambda r: -r['equipment'],
        )
        assert whole_preview['view_count'] == len(p.ids), whole_preview
        for side, nearest in (('just_above', nearest_above[:5]), ('just_below', nearest_below[:5])):
            listed = whole_preview[side]
            assert [item['id'] for item in listed] == [p.ids[r['n']] for r in nearest], (side, listed)
            for item, row in zip(listed, nearest):
                assert math.isclose(item['score'], row['equipment'], abs_tol=1e-6), (item, row)
                assert parse_qs(urlsplit(item['link']).query) == {'sample': [str(item['id'])]}, item
        # Remove them: what the page shows for that filter.
        final = await s.say('refine_view', {'view': bright['view'], 'add': preview['remove']})
        if p.objects:  # the app's exclusion keeps only images with a score: what the page shows decides
            expected_final = await s.page_shows(bright, preview['remove'])
            assert unscored <= lit - expected_final, (unscored, len(expected_final))
        else:
            # Rewritten for contract g7 (one rule everywhere, rules 1.3 and 2.1): the exclusion keeps only the images
            # that have a score, at or below the cut, on this dataset too (it kept the unscored ones), and their count
            # is the one concept_preview gave.
            expected_final = lit - above_ids - unscored
            assert expected_final == below_ids and len(expected_final) == preview['below'], preview
        await s.shows(final, expected_final, refined=bright['view'])
    else:
        assert named_equipment == [], known
        # No concept: ask for samples like the words, among the view's.
        similar = await s.say('search_similar', {'view': bright['view'], 'text': 'equipment', 'limit': 5})

        def likeness(row: dict[str, Any]) -> float:
            vector = row['clip']
            return sum(a * b for a, b in zip(vector, EQUIPMENT_DIRECTION)) / math.sqrt(sum(a * a for a in vector))

        ranked = sorted(bright_rows, key=likeness, reverse=True)[:5]
        found = [p.ids[row['n']] for row in ranked]
        assert [item['id'] for item in similar['samples']] == found, similar['samples']
        assert (similar['from'], similar['count'], similar['searched'], similar['field']) == (
            bright['view'],
            5,
            len(lit),
            'clip',
        ), similar
        # A working view: the stages of the view searched, then one of the ids found.
        stages, found_stage = similar['stages'], similar['found_stage']
        assert found_stage == len(stages) == len(bright['stages']) + 1, similar
        assert [stage['text'] for stage in stages[:-1]] == [stage['text'] for stage in bright['stages']], stages
        found_text = stages[-1]['text']
        assert found_text.startswith('id in (') and 'equipment' not in found_text.lower(), (
            found_text
        )  # never the prompt
        assert (await parsed(s.r, s.user, d, found_text))['parts'] == [found_text], found_text  # the server's words
        as_view = {key: value for key, value in similar.items() if key in VIEW_KEYS | {'from', 'link_note'}}
        await s.shows(as_view, set(found), refined=bright['view'])
        # "Remove the ones like this": the view searched without them.
        final = await s.say('refine_view', {'view': similar['view'], 'negate': [found_stage]})
        await s.shows(final, lit - set(found), refined=similar['view'])
    # Afterwards: the person's saved view, opened as a working view to compare with.
    saved = await s.say('list_saved_views', {'dataset': d})
    assert [(v['slug'], v['mine'], v['default']) for v in saved['views']] == [(f'bright-plots-{d}', True, False)]
    opened_saved = await s.say('open_saved_view', {'slug': saved['views'][0]['slug']})
    assert opened_saved['saved'] == {'slug': f'bright-plots-{d}', 'name': 'Bright plots'}, opened_saved
    assert opened_saved['count'] == len(p.of(lambda r: r['brightness'] >= 100)), opened_saved
    parts = (await parsed(s.r, s.user, d, 'brightness >= 100'))['parts']
    assert [stage['text'] for stage in opened_saved['stages']] == parts, opened_saved['stages']
    assert await view_ids(s.user, opened_saved['view']) == p.of(lambda r: r['brightness'] >= 100)
    link = urlsplit(opened_saved['link'])
    assert link.path.endswith(f'/datasets/{d}') and parse_qs(link.query) == {'view': [f'bright-plots-{d}']}
    assert opened_saved['link'] == saved['views'][0]['link'], (opened_saved['link'], saved['views'][0]['link'])

    return {'kansas': kansas, 'everywhere': everywhere, 'bright': bright, 'final': final}


async def play(r: Regions, dataset_id: int, **planting: Any) -> Script:
    """Plant, open the door, and play the conversation as the power user, the database unchanged by it."""
    await load_boundaries(r)
    p = await plant(r, dataset_id, **planting)
    await open_door(r)
    before = await snapshot(r)
    s = Script(r, POWER, p)
    await converse(s, private=planting.get('private', False), concept=planting.get('concept', True))
    assert await snapshot(r) == before  # nothing the script did wrote
    return s


def test_the_conversation_by_region_and_concept(regions_world):
    async def scenario(r):
        await play(r, MAIN)

    regions_world(scenario)


def test_the_conversation_on_a_dataset_with_a_state_field_needs_no_region(regions_world):
    """Also the variant with more serial numbers in Kansas than a page holds: turn 2 reads them a page at a time."""

    async def scenario(r):
        s = await play(r, STATES, located=False, state_field=True, many_serials=True)
        assert not any(answer['ok'] and 'places' in answer['result'] for answer in s.answers)  # find_place never asked

    regions_world(scenario)


def test_the_conversation_with_serial_numbers_kept_from_the_assistant(regions_world):
    async def scenario(r):
        s = await play(r, KEPT_SERIALS, private=True)
        written = json.dumps(s.answers)
        for serial in SERIALS:
            assert serial not in written, serial  # in no answer of the whole script

    regions_world(scenario)


def test_without_an_equipment_concept_the_conversation_falls_to_similar_samples(regions_world):
    async def scenario(r):
        async def stand_in(prompt: str, registered_model: Optional[str] = None) -> list[float]:
            return list(EQUIPMENT_DIRECTION)

        for module in (encoders, search_router, concepts_router):
            r.monkeypatch.setattr(module, 'encode_text', stand_in)
        await play(r, NO_CONCEPT, concept=False)

    regions_world(scenario)


def test_the_conversation_is_unaffected_by_objects_under_every_image(regions_world):
    """Objects under every image, carrying its fields and its score: every number is the images' (working views,
    rule 7), and each link opens on them as the dataset page does."""

    async def scenario(r):
        await play(r, WITH_OBJECTS, objects=True)
        objects = await r.database.fetchval(
            "SELECT count(*) FROM samples WHERE dataset_id = $1 AND media_type = 'object'",
            WITH_OBJECTS,
        )
        assert objects == sum(1 + row['n'] % 2 for row in ROWS), objects

    regions_world(scenario)
