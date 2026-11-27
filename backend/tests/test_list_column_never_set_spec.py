"""A list column can hold "never set" (contract g10), tested where a user or a script meets it: the HTTP API signed in
with real API tokens (the region tests' world: a database of its own per test), the page's wrapping on a dataset with
objects, the SDK driven as a script drives it, a mirror's sync against a real primary (the follower tests' world: one
database plays both parts), and the upgrade as scripts/apply_migrations.sh runs it. The page itself is in
frontend/e2e/list-column-never-set.spec.ts. Written from the contract, not from the code.

The rule. A list field kept in a column of its own (`POST /api/datasets/{id}/array-fields`, the SDK's
`create_array_field`) holds one of three things for a sample: never set, an empty list, a list with elements. A sample
added without the field is never set; `null` sent for it is taken (never set on a new sample; on an update it unsets
the field); an update that doesn't name it leaves it; a sample added again with `on_duplicate: "update"` without it
keeps what is stored, whatever the rest of the request names. Never set comes back as `null` in every place a list
comes back under a key, and the SDK's `sample.f` is `None`. In filters a list column is read as a list in the metadata
is (contracts g7 and g9): `is null` shows the rows never set, `is empty` the rows with `[]`, a NOT shows a row with `[]`
and not a row never set; `exists` and `not exists` answer as `is not null` and `is null`. Rows written before the
upgrade keep `[]`. `tags` and `@local.tags` don't change.

The fixtures. COLUMN is a dataset without objects of eight rows, `c0` to `c7` (ROWS), with the list field `pests`
made as the app makes it: two rows added without the field (c0, c7), one whose column is then written NULL straight
to the database (c1, the way a row never set is stored, so a test of the reading doesn't hang on how the row was
written), two empty lists (c2, c6) and three lists with elements. Each row has a `plot` and a `rank`, and most a
`colour`, tags, a location, a point on the `clip` map and a score for the concept `scene`. OBJECTS is a dataset with
objects whose images and objects hold the list field in each state.

Where the contract leaves a point open, these tests read it this way:
- "Never set comes back as `null`" (section 9 point 4) is asked at the top level of a row: the samples query in both
  its forms (a row, and `fields` with `slim`), a version's rows (a whole row, and `fields`), the assistant's
  `get_samples` with `fields`, and the embeddings map's points with `include_fields` (its colour-by). The map a
  version's whole row also carries (`array_fields`) may hold `null` or leave the field out.
- A list "written NULL straight to the database" is never set: the stored form of never set is not tested, only what
  the app reads from a NULL column, which has no value today too.
- The edits of rule 1.8 are the bulk edit of an array field (`samples/bulk-tag` with `field_name`), which the page's
  tag editing uses for any list field, and a sample's PATCH with the whole list.
- A sample added again with `on_duplicate: "update"` and `null` for the field takes it, as an update does: never set.
- Through the page's wrapping on a dataset with objects (section 9 point 5), the hiding tests of a list column
  (`not in`, `!=`, and a NOT around `in` or `contains`) are answered by the grid's own row, as tags' are (contract g7,
  rule 1.5), now with the empty list a value; a showing test is pinned as main answers it (by the row or its partner).
  Which rows `is null` and `is empty` ask there is not tested here.
- "The SDK's `save()` never sends a `None` it loaded": a field never set, loaded and saved with another change, stays
  never set. A script that unsets the field assigns `None` to it (`sample.pests = None`) and saves. A script that adds
  a sample never set leaves the field out or gives it `None`.
- The sidebar's count of the rows with no value is the field-stats entry's `null_count`, the number the page's
  "(no value)" tick is shown for (as for a metadata field); the tick sends `{field, operator: is_null}`.
- A version "that recorded no list fields at all" is one whose `dataset_snapshot` has no field registry (no `fields`,
  no `field_registry_version`), as versions made before the registry was recorded: the test writes one so.
- The migration of section 4 is found as the files after 0044, the last on the main this was written against; with
  none, the migration tests fail saying so. "Run again" is its files run a second time directly, without their
  ledger rows.
- The registration step (rule 1.2, section 9 points 1 and 9), decided by the owner on 2026-11-26. A dataset's rows
  "written before the upgrade" are written straight to the table with `[]` in the column. The request that registers
  the field over more than 1,000 samples answers 202 with the job as a field's promotion does (`{job_id, state}`);
  `?wait=false` asks for it at once (a server that doesn't take it answers after its five seconds). No runner runs in
  these tests: the job is run step by step with the runner's own handler for its kind (job_service.run_service's
  handlers, whatever the kind is named), and "a stop" is the runner's lease running out, the job claimed again once
  the queue makes it available. A write held back by the job is answered 409 with Retry-After, the job's id in its
  sentence, and a header named `...-Seconds` (as `X-Map-Swap-Seconds`) holding the seconds until the job is expected
  to end; a restore meanwhile is refused with 409. While the field isn't the dataset's yet no filter names it, so how
  far the walk is is read in the table, never set being a NULL column. "In steps of one block" is read as: on a
  dataset of 9,000 samples (more than two blocks of 4,096), a step changes some of its samples and not all. The SDK's
  `create_array_field` "waits for" the job: it returns only once the job has ended.
- A version's value of a list column (section 9 points 11 to 13): its list where the version had the field as the
  dataset's; never set where the dataset has the field now and didn't then; nothing where it neither had it then nor
  has it now. "Nothing" comes back as no key at all: not at the top of a version's row and not in its map of lists
  (`array_fields`). The diff, its lists of modified samples (`.../samples` and `.../select` with `change_type:
  modified`) and the compare of two versions count no sample modified by a column that holds nothing of the
  dataset's. The status is asked against a version made before versions recorded the dataset's settings and fields
  (`dataset_snapshot` NULL): a registration changes the fields, so against a version that recorded them the status
  rightly reports a change.
- A registration "dated" before the version, a row of another origin taken over (origin `system`, kept for its
  description), the samples deleted until a dataset counts at most 1,000, and a list field the dataset "has dropped
  since" (its registration taken out, the column staying: no route drops a list field) are written straight to the
  tables.
- "A sync loses its checkpoint" is read as: after a step, the sync's job goes on from no checkpoint (as a sync started
  again does), and what the step committed stays. It is lost after each step from the first that changed the mirror's
  rows to the one after which the field is the mirror's (once for each state of the mirror seen). A moment inside one
  step is not reached from outside.
- Every name used here is main's or one this file used before points 11 to 13; those points added none.
- Each test of what changes asks something main answers first, as a control, so a failure on main is the rule's. The
  pins (named so) pass on main and after.
"""

import asyncio
import json
import os
import signal
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fridge import Condition, Dataset, Sample, sync_between_instances

from backend import database as database_module
from backend import field_kinds, follower_sync, job_queue, job_service, main, statistics
from backend.database import Database, init_connection
from backend.routers import datasets, embeddings, followers
from backend.tests import test_field_registry_postgres
from backend.tests import test_follower_sync as follower
from backend.tests import test_one_rule_everywhere_spec as one_rule
from backend.tests.test_access_rule_spec import ADMIN, World
from backend.tests.test_assistant_conversation_spec import count_statistics
from backend.tests.test_assistant_core_spec import open_door, settings
from backend.tests.test_assistant_views_spec import result, view_ids
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_is_empty_spec import LOCAL_WRITES, plant_local
from backend.tests.test_objects_filter_reading_spec import page_filter
from backend.tests.test_one_rule_everywhere_spec import Planted, either, leaf, negated, ok, parsed, saved_view, stage_of
from backend.tests.test_protected_metadata_keys_spec import sdk
from backend.tests.test_regions_spec import MIGRATIONS, apply_before, apply_migration, post, selected
from backend.tests.test_upgrade_paths import ROOT

database = one_rule.database
regions_world = one_rule.regions_world
registry_database = test_field_registry_postgres.registry_database  # the follower tests' world
USER = test_field_registry_postgres.USER
pytestmark = one_rule.pytestmark

COLUMN, OBJECTS, COPY, OTHER = 84, 85, 86, 87
FIELD = 'pests'
UNSET = 'added without the field'
NULLED = 'column written NULL'
CUT = 0.5
QUERY = [1.0, 0.0, 0.0, 0.0]


# --- The planted rows ---


@dataclass(frozen=True)
class Item:
    pests: Any  # UNSET, NULLED, or the list sent
    colour: Optional[str] = None  # None: no key
    tags: tuple[str, ...] = ()
    location: Optional[tuple[float, float]] = None
    point: Optional[tuple[float, float]] = None  # on the `clip` map
    score: Optional[float] = None  # the concept `scene`


ROWS: dict[str, Item] = {
    'c0': Item(UNSET, 'red', ('seen',), (1.0, 1.0), (1.0, 1.0), 0.75),
    'c1': Item(NULLED, 'blue', (), (2.0, 2.0), (2.0, 2.0), 0.25),
    'c2': Item([], 'red', ('done',), (3.0, 3.0), (3.0, 3.0), 0.625),
    'c3': Item(['aphid'], 'blue', ('seen',), None, (4.0, 4.0), 0.125),
    'c4': Item(['mite'], None, (), (5.0, 5.0), None, 0.875),
    'c5': Item(['aphid', 'mite'], 'green', ('done', 'seen'), (6.0, 6.0), (6.0, 6.0), None),
    'c6': Item([], 'blue', ('seen',), None, (7.0, 7.0), 0.375),
    'c7': Item(UNSET, 'green', (), (8.0, 8.0), None, None),
}
EVERY = set(ROWS)
NEVER_SET = {'c0', 'c1', 'c7'}
EMPTY_LIST = {'c2', 'c6'}
WITH_ELEMENTS = {'c3', 'c4', 'c5'}
LISTED = EMPTY_LIST | WITH_ELEMENTS  # a list is there, empty or not
APHID = {'c3', 'c5'}
MITE = {'c4', 'c5'}
NOT_APHID = LISTED - APHID  # a list, empty or not, holding no aphid
LOCATED = {name for name, item in ROWS.items() if item.location is not None}
ON_THE_MAP = {name for name, item in ROWS.items() if item.point is not None}
UNTAGGED = {name for name, item in ROWS.items() if not item.tags}
# What main answers for a list column (read as tags are: a row with no element has no value).
MAIN_NO_VALUE = NEVER_SET | EMPTY_LIST


def stored(item: Item) -> Optional[list[str]]:
    """What the field holds for the row by the contract: None for never set."""
    return item.pests if isinstance(item.pests, list) else None


def body_of(name: str, number: int, item: Item) -> dict[str, Any]:
    metadata: dict[str, Any] = {'plot': f'p{number}', 'rank': number}
    if item.colour is not None:
        metadata['colour'] = item.colour
    body: dict[str, Any] = {'filepath': f'{name}.jpg', 'metadata': metadata, 'tags': list(item.tags)}
    if isinstance(item.pests, list):
        body['array_fields'] = {FIELD: item.pests}
    if item.location is not None:
        body['location'] = {'type': 'Point', 'coordinates': list(item.location)}
    return body


async def make_field(r: Any, dataset_id: int, name: str = FIELD) -> None:
    """The list field made as the app makes it."""
    made = await r.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/array-fields', json={'field_name': name})
    assert made.is_success, made.text


async def ids_of(r: Any, dataset_id: int) -> dict[str, int]:
    rows = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', dataset_id)
    return {row['filepath'][: -len('.jpg')]: row['id'] for row in rows}


async def plant(r: Any, dataset_id: int = COLUMN) -> Planted:
    """ROWS as a dataset without objects: the list field made first, the samples added through the API, c1's column
    written NULL; a vector each, the points on the `clip` map and the concept `scene`."""
    await r.dataset(dataset_id)
    await make_field(r, dataset_id)
    samples = [body_of(name, number, item) for number, (name, item) in enumerate(ROWS.items())]
    added = await post(r, f'/api/datasets/{dataset_id}/samples', {'samples': samples})
    assert added.is_success, added.text
    ids = await ids_of(r, dataset_id)
    assert set(ids) == EVERY, sorted(ids)
    nulled = [ids[name] for name, item in ROWS.items() if item.pests == NULLED]
    await r.database.execute(f'UPDATE samples SET {FIELD} = NULL WHERE id = ANY($1::int[])', nulled)
    # The lists sent are stored as sent (a check of the planting, true on main).
    held = await r.database.fetch(f'SELECT id, {FIELD} FROM samples WHERE dataset_id = $1', dataset_id)
    by_id = {row['id']: row[FIELD] for row in held}
    for name, item in ROWS.items():
        if isinstance(item.pests, list):
            assert by_id[ids[name]] == item.pests, (name, by_id[ids[name]])
    vectors = [
        {'sample_id': ids[name], 'vector': [1.0, number / 100, 0.0, 0.0], 'field_name': 'clip'}
        for number, name in enumerate(ROWS)
    ]
    assert (await post(r, f'/api/datasets/{dataset_id}/embeddings', vectors)).is_success
    r.monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    points = [
        {'sample_id': ids[name], 'x': item.point[0], 'y': item.point[1]}
        for name, item in ROWS.items()
        if item.point is not None
    ]
    response = await r.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/embeddings/coordinates',
        params={'field_name': 'clip'},
        json=points,
    )
    assert response.status_code == 200, response.text
    concept = await r.database.fetchval(
        'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
        "VALUES ($1, 'scene', 'clip', 'seed', '[1,0,0,0]', $2) RETURNING id",
        dataset_id,
        CUT,
    )
    for name, item in ROWS.items():
        if item.score is not None:
            await r.database.execute(
                'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, $3)',
                concept,
                ids[name],
                item.score,
            )
    sidebar = await r.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/field-stats')
    assert sidebar.status_code == 200, sidebar.text
    return Planted(dataset_id, ids, concept)


async def small(r: Any, dataset_id: int, lists: dict[str, Any], field: str = FIELD) -> Planted:
    """A dataset of a sample per name with the list field: UNSET adds it without the field, a list sends that list."""
    await r.dataset(dataset_id)
    await make_field(r, dataset_id, field)
    samples = [
        {
            'filepath': f'{name}.jpg',
            'metadata': {'plot': name},
            **({'array_fields': {field: held}} if isinstance(held, list) else {}),
        }
        for name, held in lists.items()
    ]
    added = await post(r, f'/api/datasets/{dataset_id}/samples', {'samples': samples})
    assert added.is_success, added.text
    ids = await ids_of(r, dataset_id)
    nulled = [ids[name] for name, held in lists.items() if held == NULLED]
    if nulled:
        await r.database.execute(f'UPDATE samples SET {field} = NULL WHERE id = ANY($1::int[])', nulled)
    return Planted(dataset_id, ids, 0)


async def lists_held(r: Any, p: Planted, field: str = FIELD) -> dict[str, Any]:
    """Each sample's list as the samples query gives a whole row: the field is there, `null` when never set."""
    body = await ok(await post(r, f'/api/datasets/{p.dataset_id}/samples/query', {'limit': 1000}), 'a whole row')
    found = {}
    for item in body['items']:
        assert field in item, ('the field is in every row', item)
        found[p.names([item['id']]).pop()] = item[field]
    return found


async def slim_lists_held(
    r: Any,
    p: Planted,
    field: str = FIELD,
    names: Optional[set[str]] = None,
) -> dict[str, Any]:
    """The same, as the slim form gives it (`fields`, as the SDK and the export ask); with `names`, of those samples
    only (asked by their ids, so a dataset past one page is read whole where it matters)."""
    request: dict[str, Any] = {'limit': 1000, 'fields': field, 'slim': True}
    if names is not None:
        request['filters'] = {'field': 'id', 'operator': 'in', 'value': sorted(p.of(names))}
    body = await ok(await post(r, f'/api/datasets/{p.dataset_id}/samples/query', request), 'the slim form')
    found = {}
    for item in body['items']:
        assert field in item, ('the field is in every row', item)
        found[p.names([item['id']]).pop()] = item[field]
    assert names is None or set(found) == names, ('the samples asked for', sorted(found))
    return found


def sorted_lists(held: dict[str, Any]) -> dict[str, Any]:
    return {name: sorted(value) if isinstance(value, list) else value for name, value in held.items()}


async def assert_shows(r: Any, p: Planted, cases: list[tuple[Any, set[str]]]) -> None:
    """Each form (text, read by the parse route, or a filter node) shows these rows, through samples/query and its
    count."""
    for form, names in cases:
        found = await one_rule.shown(r, p, form)
        assert found == names, (form, 'shown', sorted(found), 'expected', sorted(names))


async def patch(r: Any, p: Planted, name: str, body: dict[str, Any]) -> httpx.Response:
    return await r.call(ADMIN, 'PATCH', f'/api/datasets/{p.dataset_id}/samples/{p.ids[name]}', json=body)


async def bulk_edit(r: Any, p: Planted, names: set[str], add: list[str], remove: list[str]) -> None:
    """The bulk edit of the list field, as the page's tag editing sends it for any list field."""
    body = {'sample_ids': sorted(p.of(names)), 'field_name': FIELD, 'add_tags': add, 'remove_tags': remove}
    response = await post(r, f'/api/datasets/{p.dataset_id}/samples/bulk-tag', body)
    assert response.status_code in (200, 202), response.text
    if response.status_code == 202:
        await finish(r.database)


async def add_again(r: Any, p: Planted, samples: list[dict[str, Any]]) -> httpx.Response:
    return await post(r, f'/api/datasets/{p.dataset_id}/samples', {'samples': samples, 'on_duplicate': 'update'})


async def version_now(r: Any, dataset_id: int, name: str) -> int:
    await ok(await post(r, f'/api/datasets/{dataset_id}/versions', {'name': name}), name)
    version = await r.database.fetchval(
        'SELECT id FROM dataset_versions WHERE dataset_id = $1 AND name = $2',
        dataset_id,
        name,
    )
    assert version is not None, name
    return int(version)


async def restore(r: Any, dataset_id: int, version: int) -> None:
    response = await r.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/versions/{version}/revert')
    assert response.status_code == 200, response.text


# --- Section 1: storage and writes, and what comes back (8.1) ---


def test_rule_1_4_a_sample_added_without_the_field_is_never_set_and_comes_back_null(regions_world):
    """Rules 1.4 and 2.1: added without the field, a sample is never set and comes back `null` in both forms of the
    samples query; with `[]` the empty list; with a list that list. An edit that doesn't name the field (its metadata,
    its tags) leaves it never set (rule 1.6). `is null` then finds it. The list and the empty list are asked first, as
    main answers them; main stored `[]` for the sample added without the field."""

    async def scenario(r):
        p = await small(r, COLUMN, {'unset': UNSET, 'empty': [], 'aphid': ['aphid']})
        held = await lists_held(r, p)
        assert (held['empty'], held['aphid']) == ([], ['aphid']), held
        assert held['unset'] is None, held
        assert await slim_lists_held(r, p) == {'unset': None, 'empty': [], 'aphid': ['aphid']}
        assert (await patch(r, p, 'unset', {'metadata': {'seen': 1}})).status_code == 200
        assert (await patch(r, p, 'unset', {'tags': ['looked']})).status_code == 200
        assert (await lists_held(r, p))['unset'] is None
        await assert_shows(r, p, [(leaf(FIELD, 'is_null'), {'unset'}), ('pests is null', {'unset'})])

    regions_world(scenario)


def test_rule_1_5_null_sent_for_a_new_sample_is_taken_and_the_sample_is_never_set(regions_world):
    """`null` sent for a list field on a new sample is taken (main refused the request, 422): the sample is never set,
    and the other samples of the request are added with their lists. Adding a sample with a list is asked first."""

    async def scenario(r):
        p = await small(r, COLUMN, {'aphid': ['aphid']})
        samples = [
            {'filepath': 'nulled.jpg', 'metadata': {'plot': 'nulled'}, 'array_fields': {FIELD: None}},
            {'filepath': 'mite.jpg', 'metadata': {'plot': 'mite'}, 'array_fields': {FIELD: ['mite']}},
        ]
        added = await post(r, f'/api/datasets/{COLUMN}/samples', {'samples': samples})
        assert added.status_code == 200, added.text
        p = Planted(COLUMN, await ids_of(r, COLUMN), 0)
        assert await lists_held(r, p) == {'aphid': ['aphid'], 'nulled': None, 'mite': ['mite']}
        await assert_shows(r, p, [(leaf(FIELD, 'is_null'), {'nulled'})])

    regions_world(scenario)


def test_rule_1_5_null_sent_in_an_edit_unsets_the_field(regions_world):
    """A PATCH with `null` for the field unsets it, on a list with elements and on an empty list (main refused it,
    422). Changing the list by a PATCH is asked first."""

    async def scenario(r):
        p = await small(r, COLUMN, {'aphid': ['aphid'], 'empty': [], 'mite': ['mite']})
        changed = await patch(r, p, 'mite', {'array_fields': {FIELD: ['mite', 'thrips']}})
        assert changed.status_code == 200, changed.text
        for name in ('aphid', 'empty'):
            response = await patch(r, p, name, {'array_fields': {FIELD: None}})
            assert response.status_code == 200, (name, response.text)
        assert await lists_held(r, p) == {'aphid': None, 'empty': None, 'mite': ['mite', 'thrips']}
        await assert_shows(r, p, [(leaf(FIELD, 'is_null'), {'aphid', 'empty'}), (leaf(FIELD, 'is_empty'), set())])

    regions_world(scenario)


def test_rule_1_6_pin_an_edit_that_does_not_name_the_field_leaves_it_as_it_is(regions_world):
    """A pin: a PATCH of the metadata, of the tags or of both, without the field, leaves an empty list, a list and a
    column written NULL as they are."""

    async def scenario(r):
        p = await small(r, COLUMN, {'empty': [], 'aphid': ['aphid'], 'nulled': NULLED})
        before = await lists_held(r, p)
        assert before == {'empty': [], 'aphid': ['aphid'], 'nulled': None}
        for name in before:
            for body in ({'metadata': {'seen': 1}}, {'tags': ['looked']}, {'metadata': {'n': 2}, 'tags': []}):
                assert (await patch(r, p, name, body)).status_code == 200, (name, body)
        assert await lists_held(r, p) == before

    regions_world(scenario)


def test_rule_1_7_pin_a_sample_added_again_alone_without_the_field_keeps_its_list(regions_world):
    """A pin (section 9 point 2): a sample added again alone in its request, with `on_duplicate: "update"` and
    without the field, keeps an empty list, a list and a column written NULL. A metadata key the request leaves out is
    removed (a list field is a field of the dataset, not a key of the sample's document)."""

    async def scenario(r):
        p = await small(r, COLUMN, {'empty': [], 'aphid': ['aphid'], 'nulled': NULLED})
        for name in ('empty', 'aphid', 'nulled'):
            response = await patch(r, p, name, {'metadata': {'gone': True}})
            assert response.status_code == 200, response.text
            again = await add_again(r, p, [{'filepath': f'{name}.jpg', 'metadata': {'plot': name, 'again': 1}}])
            assert again.status_code == 200, again.text
        assert await lists_held(r, p) == {'empty': [], 'aphid': ['aphid'], 'nulled': None}
        rows = await ok(await post(r, f'/api/datasets/{COLUMN}/samples/query', {'limit': 10}))
        assert all(item['metadata'] == {'plot': p.names([item['id']]).pop(), 'again': 1} for item in rows['items'])

    regions_world(scenario)


def test_rule_1_7_a_sample_added_again_without_the_field_keeps_its_list_when_another_sample_of_the_request_names_it(
    regions_world,
):
    """Section 9 point 2: in one request with `on_duplicate: "update"`, the samples that don't name the field keep
    what they hold (an empty list, a list, never set) while the one that names it takes its list. Main gave the others
    `[]`. The same request removes a metadata key it leaves out, as main does, and it stays so (asked first)."""

    async def scenario(r):
        p = await small(r, COLUMN, {'empty': [], 'aphid': ['aphid'], 'unset': UNSET, 'named': ['mite']})
        for name in p.ids:
            assert (await patch(r, p, name, {'metadata': {'gone': True}})).status_code == 200
        again = await add_again(
            r,
            p,
            [
                {'filepath': 'empty.jpg', 'metadata': {'plot': 'empty'}},
                {'filepath': 'aphid.jpg', 'metadata': {'plot': 'aphid'}},
                {'filepath': 'unset.jpg', 'metadata': {'plot': 'unset'}},
                {'filepath': 'named.jpg', 'metadata': {'plot': 'named'}, 'array_fields': {FIELD: ['thrips']}},
            ],
        )
        assert again.status_code == 200, again.text
        rows = await ok(await post(r, f'/api/datasets/{COLUMN}/samples/query', {'limit': 10}))
        assert all('gone' not in item['metadata'] for item in rows['items']), rows['items']
        held = await lists_held(r, p)
        assert held['named'] == ['thrips'], held
        assert (held['empty'], held['aphid']) == ([], ['aphid']), held
        assert held['unset'] is None, held

    regions_world(scenario)


def test_rule_1_7_pin_a_sample_added_again_with_the_field_takes_the_list_sent(regions_world):
    """A pin: added again with `on_duplicate: "update"` and the field, a sample takes the list sent, an empty list
    included, alone and beside a sample that doesn't name it."""

    async def scenario(r):
        p = await small(r, COLUMN, {'aphid': ['aphid'], 'empty': [], 'other': ['mite']})
        again = await add_again(
            r,
            p,
            [
                {'filepath': 'aphid.jpg', 'metadata': {'plot': 'aphid'}, 'array_fields': {FIELD: []}},
                {'filepath': 'empty.jpg', 'metadata': {'plot': 'empty'}, 'array_fields': {FIELD: ['thrips']}},
            ],
        )
        assert again.status_code == 200, again.text
        alone = await add_again(r, p, [{'filepath': 'other.jpg', 'array_fields': {FIELD: ['mite', 'thrips']}}])
        assert alone.status_code == 200, alone.text
        assert await lists_held(r, p) == {'aphid': [], 'empty': ['thrips'], 'other': ['mite', 'thrips']}

    regions_world(scenario)


def test_rule_1_7_a_sample_added_again_with_null_for_the_field_is_never_set(regions_world):
    """`null` is taken where a list is (rule 1.5): added again with `on_duplicate: "update"` and `null`, a sample is
    never set (main refused the request, 422). Added again with a list is asked first."""

    async def scenario(r):
        p = await small(r, COLUMN, {'aphid': ['aphid'], 'empty': []})
        listed = await add_again(r, p, [{'filepath': 'empty.jpg', 'array_fields': {FIELD: ['mite']}}])
        assert listed.status_code == 200, listed.text
        nulled = await add_again(r, p, [{'filepath': 'aphid.jpg', 'array_fields': {FIELD: None}}])
        assert nulled.status_code == 200, nulled.text
        assert await lists_held(r, p) == {'aphid': None, 'empty': ['mite']}

    regions_world(scenario)


def test_rule_1_8_an_edit_that_takes_the_last_element_out_leaves_an_empty_list_a_value(regions_world):
    """The bulk edit that removes a list's last element, and a PATCH with `[]`, leave the empty list, a value: it comes
    back `[]`, `is empty` finds it, `is null` doesn't, and a NOT shows it. Main answered `is null` for it, and its NOT
    didn't show it. What comes back is asked first, as main answers it."""

    async def scenario(r):
        p = await small(r, COLUMN, {'aphid': ['aphid'], 'mite': ['mite'], 'other': ['rust']})
        await bulk_edit(r, p, {'aphid'}, [], ['aphid'])
        assert (await patch(r, p, 'mite', {'array_fields': {FIELD: []}})).status_code == 200
        assert await lists_held(r, p) == {'aphid': [], 'mite': [], 'other': ['rust']}
        await assert_shows(
            r,
            p,
            [
                (leaf(FIELD, 'is_empty'), {'aphid', 'mite'}),
                (leaf(FIELD, 'is_null'), set()),
                (leaf(FIELD, 'is_not_null'), {'aphid', 'mite', 'other'}),
                (negated(leaf(FIELD, 'in', ['rust'])), {'aphid', 'mite'}),
            ],
        )

    regions_world(scenario)


def test_rule_1_8_pin_an_edit_that_adds_an_element_to_a_field_never_set_makes_a_list_of_it(regions_world):
    """A pin: on a column written NULL, the bulk edit that adds an element makes a list of it, and so does a PATCH
    with a list."""

    async def scenario(r):
        p = await small(r, COLUMN, {'first': NULLED, 'second': NULLED, 'kept': NULLED})
        await bulk_edit(r, p, {'first'}, ['aphid'], [])
        assert (await patch(r, p, 'second', {'array_fields': {FIELD: ['mite']}})).status_code == 200
        assert await lists_held(r, p) == {'first': ['aphid'], 'second': ['mite'], 'kept': None}

    regions_world(scenario)


def test_rule_1_8_an_edit_that_adds_an_element_to_a_sample_added_without_the_field_makes_a_list_of_it(regions_world):
    """The same on samples added without the field: never set until the edit, then the list; a bulk edit that removes
    an element it doesn't hold leaves it never set. Main stored `[]` for them."""

    async def scenario(r):
        p = await small(r, COLUMN, {'first': UNSET, 'second': UNSET, 'kept': UNSET})
        await bulk_edit(r, p, {'kept'}, [], ['aphid'])
        assert await lists_held(r, p) == {'first': None, 'second': None, 'kept': None}
        await bulk_edit(r, p, {'first'}, ['aphid'], [])
        assert (await patch(r, p, 'second', {'array_fields': {FIELD: ['mite']}})).status_code == 200
        assert await lists_held(r, p) == {'first': ['aphid'], 'second': ['mite'], 'kept': None}

    regions_world(scenario)


# --- Rule 2.1 and section 9 point 4: never set comes back as null ---


def test_rule_2_1_never_set_comes_back_null_in_every_place_a_list_comes_back(regions_world):
    """Section 9 point 4, on COLUMN: the samples query in both its forms, a version's rows (a whole row, and `fields`),
    the assistant's samples (`get_samples` with `fields`) and the embeddings map's points with the field included
    (its colour-by) give `null` for the rows never set and `[]` for the empty lists. c1 (its column written NULL) is
    asked first in each place, as main answers it; main gave `[]` for the rows added without the field."""

    want = {name: stored(item) for name, item in ROWS.items()}

    async def scenario(r):
        p = await plant(r)
        version = await version_now(r, COLUMN, 'as planted')
        rows = sorted_lists(await lists_held(r, p))
        slim = sorted_lists(await slim_lists_held(r, p))
        whole = await ok(await post(r, f'/api/datasets/{COLUMN}/versions/{version}/samples/query', {'limit': 100}))
        version_rows = {p.names([item['id']]).pop(): item for item in whole['items']}
        fields = await ok(
            await post(
                r,
                f'/api/datasets/{COLUMN}/versions/{version}/samples/query',
                {'limit': 100, 'fields': [FIELD]},
            ),
        )
        version_fields = {p.names([item['id']]).pop(): item for item in fields['items']}
        await open_door(r)
        await settings(r, {'sample_fields': True})
        view = await result(ADMIN, 'make_view', {'dataset': COLUMN})
        page = await result(ADMIN, 'get_samples', {'view': view['view'], 'fields': [FIELD], 'limit': 50})
        told = {p.names([sample['id']]).pop(): sample for sample in page['samples']}
        scatter = await ok(
            await post(
                r,
                f'/api/datasets/{COLUMN}/embeddings/query',
                {'field': 'clip', 'include_fields': [FIELD], 'sample_pct': 100},
            ),
        )
        coloured = {p.names([point['sample_id']]).pop(): point for point in scatter['points']}
        assert set(told) == EVERY and set(version_rows) == set(version_fields) == EVERY
        assert set(coloured) == ON_THE_MAP, sorted(coloured)
        places: dict[str, dict[str, Any]] = {
            'the samples query': rows,
            'its slim form': slim,
            "a version's whole row": {name: item.get(FIELD, 'absent') for name, item in version_rows.items()},
            "a version's fields": {name: item.get(FIELD, 'absent') for name, item in version_fields.items()},
            "the assistant's samples": {name: sample.get(FIELD, 'absent') for name, sample in told.items()},
            "the map's colour-by": {name: point.get(FIELD, 'absent') for name, point in coloured.items()},
        }
        for where, held in places.items():
            held = sorted_lists(held)
            assert held['c1'] is None, (where, 'a column written NULL, as main gives it', held['c1'])
        for where, held in places.items():
            held = sorted_lists(held)
            assert held == {name: want[name] for name in held}, (where, held)
        for name, item in version_rows.items():  # its map of the lists: null there, or the field left out
            assert (item.get('array_fields') or {}).get(FIELD) == want[name], (name, item.get('array_fields'))

    regions_world(scenario)


# --- Section 3: filters (rule 3.1, section 9 point 5) ---


def test_rule_3_1_a_negation_of_a_list_column_shows_the_rows_whose_list_is_there_empty_or_not_and_holds_none_named(
    regions_world,
):
    """`f not in (...)`, `f != v`, `NOT (f in (...))` and `NOT (f contains ...)` show the rows whose list is there,
    empty or not, and holds none of the named: the empty lists c2 and c6 and the list c4; not a row never set (c0 and
    c7 added without the field, c1 written NULL). A NOT around the list and another field needs both (c4 has no
    colour). Main showed only the rows with an element. The shows are asked first, as main answers them."""

    async def scenario(r):
        p = await plant(r)
        await assert_shows(
            r,
            p,
            [('pests in ("aphid")', APHID), (leaf(FIELD, 'in', ['aphid']), APHID), ('pests contains "mite"', MITE)],
        )
        await assert_shows(
            r,
            p,
            [
                ('pests not in ("aphid")', NOT_APHID),
                (leaf(FIELD, 'not in', ['aphid']), NOT_APHID),
                ('pests != "aphid"', NOT_APHID),
                (leaf(FIELD, '!=', 'aphid'), NOT_APHID),
                ('NOT (pests in ("aphid"))', NOT_APHID),
                (negated(leaf(FIELD, 'in', ['aphid'])), NOT_APHID),
                ('NOT (pests contains "aphid")', NOT_APHID),
                (negated(leaf(FIELD, 'contains', 'aphid')), NOT_APHID),
                ('pests not in ("aphid", "mite")', EMPTY_LIST),
                ('NOT (pests in ("aphid") OR colour = "red")', {'c6'}),
                (negated(either(leaf(FIELD, 'in', ['aphid']), leaf('colour', '=', 'red'))), {'c6'}),
            ],
        )

    regions_world(scenario)


def test_rule_3_1_is_null_shows_the_rows_never_set_and_is_not_null_the_rows_with_a_list_empty_or_not(regions_world):
    """`f is null` shows the rows never set (added without the field, or written NULL); `f is not null` the rows with
    a list, empty or not. Main read the empty list as no value. A show is asked first."""

    async def scenario(r):
        p = await plant(r)
        await assert_shows(r, p, [('pests contains "aphid"', APHID)])
        await assert_shows(
            r,
            p,
            [
                ('pests is null', NEVER_SET),
                (leaf(FIELD, 'is_null'), NEVER_SET),
                ('pests is not null', LISTED),
                (leaf(FIELD, 'is_not_null'), LISTED),
                ('NOT (pests is null)', LISTED),
                ('pests is null OR pests in ("mite")', NEVER_SET | MITE),
            ],
        )

    regions_world(scenario)


def test_rule_3_1_is_empty_shows_the_rows_with_an_empty_list_and_is_not_empty_those_with_an_element(regions_world):
    """`f is empty` shows the rows with `[]`, not a row never set; `f is not empty` and `NOT (f is empty)` the rows
    with at least one element. Main answered `is empty` as `is null`. A show is asked first."""

    async def scenario(r):
        p = await plant(r)
        await assert_shows(r, p, [(leaf(FIELD, 'in', ['mite']), MITE)])
        await assert_shows(
            r,
            p,
            [
                ('pests is empty', EMPTY_LIST),
                (leaf(FIELD, 'is_empty'), EMPTY_LIST),
                ('pests is not empty', WITH_ELEMENTS),
                (leaf(FIELD, 'is_not_empty'), WITH_ELEMENTS),
                ('NOT (pests is empty)', WITH_ELEMENTS),
                (negated(leaf(FIELD, 'is_empty')), WITH_ELEMENTS),
                ('pests is empty AND colour = "blue"', {'c6'}),
                ('pests is null OR pests is empty', NEVER_SET | EMPTY_LIST),
            ],
        )

    regions_world(scenario)


def test_rule_9_5_exists_and_not_exists_of_a_list_column_answer_as_is_not_null_and_is_null(regions_world):
    """Section 9 point 5: `exists` shows the rows with a list, empty or not, and `not exists` the rows never set. Main
    answered them as tags: a list with an element exists. `is null` of the column written NULL is asked first."""

    async def scenario(r):
        p = await plant(r)
        assert 'c1' in await one_rule.shown(r, p, leaf(FIELD, 'is_null'))
        await assert_shows(
            r,
            p,
            [
                ('pests exists', LISTED),
                (leaf(FIELD, 'exists'), LISTED),
                ('pests not exists', NEVER_SET),
                (leaf(FIELD, 'not_exists'), NEVER_SET),
            ],
        )
        assert await one_rule.shown(r, p, 'pests exists') == await one_rule.shown(r, p, 'pests is not null')
        assert await one_rule.shown(r, p, 'pests not exists') == await one_rule.shown(r, p, 'pests is null')

    regions_world(scenario)


COMPARED = [
    leaf(FIELD, 'in', ['aphid']),
    leaf(FIELD, 'not in', ['aphid']),
    leaf(FIELD, '!=', 'aphid'),
    leaf(FIELD, 'contains', 'mite'),
    leaf(FIELD, 'is_empty'),
    leaf(FIELD, 'is_not_empty'),
    negated(leaf(FIELD, 'in', ['aphid'])),
    negated(leaf(FIELD, 'contains', 'mite')),
]


def test_rule_3_1_each_row_is_in_exactly_one_of_is_null_is_empty_and_is_not_empty_and_no_test_shows_a_row_never_set(
    regions_world,
):
    """A list column as a list in a field (contract g9, rule 1.3, now for it): each row is in exactly one of
    `is null`, `is empty` and `is not empty`; no comparison, no NOT of one and no emptiness test shows a row that
    `is null` shows. Main's `is null` and `is empty` both showed the empty lists."""

    async def scenario(r):
        p = await plant(r)
        null, empty, filled = [
            await one_rule.shown(r, p, leaf(FIELD, operator)) for operator in ('is_null', 'is_empty', 'is_not_empty')
        ]
        assert null and empty and filled, ('the property tests nothing here', null, empty, filled)
        assert not null & empty and not null & filled and not empty & filled, (null, empty, filled)
        assert null | empty | filled == EVERY
        for node in COMPARED:
            found = await one_rule.shown(r, p, node)
            assert not found & null, (node, 'shows rows with no value', sorted(found & null))

    regions_world(scenario)


# --- Rule 3.3: every reader of a filter, the embedding map's fast path included ---


async def every_reader(r: Any, p: Planted, text: str, names: set[str], monkeypatch: Any, version: int) -> None:
    """Every reader of filters keeps exactly the rows `names` for the filter text (the list of contract g7, 2.1)."""
    node = await parsed(r, text, COLUMN)
    [rule] = node['filters']
    assert names and names != EVERY, ('the reader tests nothing here', text)
    base = f'/api/datasets/{COLUMN}'
    ids = p.of(names)
    # The grid's list and its count.
    assert await selected(r, node, COLUMN) == ids, ('samples/query', text)
    # An export's pages, as the export dialog asks them.
    exported: list[int] = []
    after: Optional[int] = None
    while True:
        body: dict[str, Any] = {'limit': 2, 'filters': node, 'fields': 'filepath', 'slim': True, 'skip_count': True}
        if after is not None:
            body['after_id'] = after
        items = (await ok(await post(r, f'{base}/samples/query', body), text))['items']
        exported += [item['id'] for item in items]
        if len(items) < 2:
            break
        after = items[-1]['id']
    assert sorted(exported) == sorted(ids), ('the export', text)
    numbers = {name: number for number, name in enumerate(ROWS)}
    plots = sorted(f'p{numbers[name]}' for name in names)
    ranks = [numbers[name] for name in names]
    aggregations = [{'field': 'plot', 'op': 'count'}, {'field': 'plot', 'op': 'values'}]
    count, values = (await ok(await post(r, f'{base}/aggregate', {'filters': node, 'aggregations': aggregations})))[
        'results'
    ]
    assert (count['value'], sorted(values['value'])) == (len(names), plots), ('aggregate', text)
    histogram = await ok(await post(r, f'{base}/histogram', {'field': 'rank', 'filters': node, 'bins': 10}))
    assert histogram['total'] == len(names), ('histogram', text, histogram)
    [facet] = await ok(await post(r, f'{base}/field-stats', {'filters': node, 'fields': ['rank']}))
    assert (facet['min'], facet['max']) == (min(ranks), max(ranks)), ('field-stats', text, facet)
    listed = await ok(await post(r, f'{base}/fields/plot/autocomplete', {'filters': node, 'limit': 100}))
    assert sorted(item['value'] for item in listed['values']) == plots, ('autocomplete', text)
    # The map, and the embeddings map.
    points = await ok(await post(r, f'{base}/map_points', {'filters': node, 'points': True}))
    assert {item['id'] for item in points['items']} == p.of(names & LOCATED), ('map_points', text)
    box = await ok(await post(r, f'{base}/map_extent', {'filters': node}))
    assert box['count'] == len(names & LOCATED), ('map_extent', text, box)
    scatter = await ok(await post(r, f'{base}/embeddings/query', {'field': 'clip', 'filters': node, 'sample_pct': 100}))
    assert {item['sample_id'] for item in scatter['points']} == p.of(names & ON_THE_MAP), ('embeddings map', text)
    # Select All and its token.
    token = await ok(await post(r, f'{base}/selection_token', {'filters': node}))
    assert token['count'] == len(names), ('selection_token', text)
    in_token = {'field': 'id', 'operator': 'in_selection', 'value': token['token']}
    assert await selected(r, in_token, COLUMN) == ids, ('in_selection', text)
    search = {'dataset_id': COLUMN, 'vector': QUERY, 'field_name': 'clip', 'limit': 1000}
    found = await ok(await post(r, '/api/search', {**search, 'filters': node}))
    assert {item['id'] for item in found} == ids, ('/api/search', text)
    # A version's query (a filter read on one row).
    rows = await ok(await post(r, f'{base}/versions/{version}/samples/query', {'filters': node, 'limit': 1000}))
    assert {item['id'] for item in rows['items']} == ids, ('a version: samples/query', text)
    kept = await ok(
        await post(
            r,
            f'{base}/versions/{version}/aggregate',
            {'filters': node, 'aggregations': [{'field': 'plot', 'op': 'values'}]},
        ),
    )
    assert sorted(kept['results'][0]['value']) == plots, ('a version: aggregate', text)
    # A saved view, read back as saved and opened by its stage's filter.
    name = f'view-{uuid4().hex[:8]}'
    made = await saved_view(r, p, rule, name)
    read = await ok(await r.call(ADMIN, 'GET', f"/api/views/{made['slug']}"))
    assert read['content'] == {'viewStages': [stage_of(rule)]}, ('the saved view, read back', text)
    assert await selected(r, read['content']['viewStages'][0]['filter'], COLUMN) == ids, ('its stage', text)
    # The SDK: the text, the filter as a script writes it (a single test), and the saved view loaded.
    sdk(r, asyncio.get_running_loop(), monkeypatch)

    def through_the_sdk() -> tuple[set[int], int, set[int], set[int]]:
        dataset = Dataset(f'dataset-{COLUMN}')
        view = dataset.query(text)
        written = (
            dataset.samples[Condition(rule['field'], rule['operator'], rule['value'])] if 'field' in rule else view
        )
        return set(view.ids()), view.count(), set(written.ids()), set(dataset.load_view(name).ids())

    assert await asyncio.to_thread(through_the_sdk) == (ids, len(names), ids, ids), ('the SDK', text)
    # The assistant: its view of the text, and the saved view opened.
    await open_door(r)
    view = await result(ADMIN, 'make_view', {'dataset': COLUMN, 'filter': text})
    assert view['count'] == len(names), ('the assistant: make_view', text, view['count'])
    assert await view_ids(ADMIN, view['view']) == ids, ('the assistant: get_samples', text)
    opened = await result(ADMIN, 'open_saved_view', {'slug': made['slug']})
    assert opened['count'] == len(names), ('the assistant: open_saved_view', text, opened['count'])
    assert await view_ids(ADMIN, opened['view']) == ids, ('the assistant: its samples', text)
    # A bulk tag by the filter, run as the job runner would, last (it changes the tags): its count, and its rows.
    tag = f'bulk-{uuid4().hex[:12]}'
    tagged = await post(r, f'{base}/samples/bulk-tag', {'filters': node, 'add_tags': [tag]})
    assert tagged.status_code in (200, 202), tagged.text
    if tagged.status_code == 202:
        updated = (await finish(r.database))['result']['updated_count']
    else:
        updated = tagged.json()['updated_count']
    rows_tagged = await r.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 AND $2 = ANY(tags)', COLUMN, tag)
    assert (updated, {item['id'] for item in rows_tagged}) == (len(names), ids), ('the bulk tag', text)


def test_rule_3_3_pin_every_reader_shows_the_rows_of_a_show_of_a_list_column(regions_world, monkeypatch):
    """The readers below, with a filter main and the rule read alike: a pin, and the control for the next test."""

    async def scenario(r):
        p = await plant(r)
        version = await version_now(r, COLUMN, 'as planted')
        await every_reader(r, p, 'pests in ("aphid")', APHID, monkeypatch, version)

    regions_world(scenario)


READERS: list[tuple[str, set[str]]] = [
    ('pests not in ("aphid")', NOT_APHID),
    ('NOT (pests contains "aphid")', NOT_APHID),
    ('pests is null', NEVER_SET),
    ('pests is not null', LISTED),
    ('pests is empty', EMPTY_LIST),
]


@pytest.mark.parametrize(('text', 'names'), READERS, ids=[text for text, _ in READERS])
def test_rule_3_3_every_reader_reads_a_list_column_as_a_list_in_a_field(regions_world, monkeypatch, text, names):
    async def scenario(r):
        p = await plant(r)
        version = await version_now(r, COLUMN, 'as planted')
        await every_reader(r, p, text, names, monkeypatch, version)

    regions_world(scenario)


# (the filter text, the rows the samples query shows, written out by hand). Each beside an exclusion the embedding
# map's fast path knows (a concept's exclude, a tags exclusion), or alone.
SCATTER: list[tuple[str, set[str]]] = [
    (f'not concept "scene" > {CUT} AND pests in ("aphid")', {'c3'}),
    (f'not concept "scene" > {CUT} AND pests not in ("aphid")', {'c6'}),
    ('tags not in ("done") AND pests is empty', {'c6'}),
    ('tags not in ("done") AND pests is null', {'c0'}),
    ('pests not in ("aphid")', NOT_APHID),
    ('NOT (pests in ("aphid")) AND tags not in ("seen")', {'c2'}),
]


@pytest.mark.parametrize(('text', 'names'), SCATTER, ids=[text for text, _ in SCATTER])
def test_rule_3_3_the_embedding_map_shows_the_points_of_the_rows_the_samples_query_shows(regions_world, text, names):
    """The embedding map, its fast path for exclusions included, gives the points of the rows the samples query gives,
    and those are the rows the rule gives. The first is a pin (a show)."""

    async def scenario(r):
        p = await plant(r)
        node = await parsed(r, text, COLUMN)
        rows = await selected(r, node, COLUMN)
        assert p.names(rows) == names, (text, sorted(p.names(rows)))
        assert rows & p.of(ON_THE_MAP), (text, 'the map tests nothing here')
        scatter = await ok(
            await post(
                r,
                f'/api/datasets/{COLUMN}/embeddings/query',
                {'field': 'clip', 'filters': node, 'sample_pct': 100},
            ),
            text,
        )
        found = {item['sample_id'] for item in scatter['points']}
        assert found == rows & p.of(ON_THE_MAP), (text, sorted(p.names(found)), sorted(p.names(rows)))

    regions_world(scenario)


# --- Section 9 point 5: through the page's wrapping on a dataset with objects ---

# (the image a row is an object of, or None for an image; and with "lo-" an object with no image; its list)
OBJECT_ROWS: dict[str, tuple[Optional[str], bool, Any]] = {
    'im-a': (None, False, ['aphid']),
    'ob-a1': ('im-a', True, []),
    'ob-a2': ('im-a', True, UNSET),
    'im-b': (None, False, []),
    'ob-b1': ('im-b', True, ['aphid']),
    'im-c': (None, False, UNSET),
    'ob-c1': ('im-c', True, ['mite']),
    'im-d': (None, False, ['mite']),
    'im-e': (None, False, NULLED),
    'ob-e1': ('im-e', True, []),
    'lo-1': (None, True, ['mite']),
    'lo-2': (None, True, UNSET),
}
# The hiding tests, read on the grid's own row: its list is there, empty or not, and holds no aphid.
OWN_ROW_NOT_APHID = {'image': {'im-b', 'im-d'}, 'object': {'ob-a1', 'ob-c1', 'ob-e1', 'lo-1'}}
# The show, as main answers it: met by the row or its partner.
SHOWN_APHID = {'image': {'im-a', 'im-b'}, 'object': {'ob-a1', 'ob-a2', 'ob-b1'}}


async def plant_objects(r: Any) -> Planted:
    await r.dataset(OBJECTS)
    await make_field(r, OBJECTS)
    ids: dict[str, int] = {}
    for objects in (False, True):
        batch = []
        for name, (image, is_object, held) in OBJECT_ROWS.items():
            if is_object is not objects:
                continue
            body: dict[str, Any] = {'filepath': f'{name}.jpg', 'metadata': {'plot': name}}
            if is_object:
                body['media_type'] = 'object'
                if image is not None:
                    body['metadata']['parent_id'] = ids[image]
            if isinstance(held, list):
                body['array_fields'] = {FIELD: held}
            batch.append(body)
        added = await post(r, f'/api/datasets/{OBJECTS}/samples', {'samples': batch})
        assert added.is_success, added.text
        ids = await ids_of(r, OBJECTS)
    assert set(ids) == set(OBJECT_ROWS), sorted(ids)
    nulled = [ids[name] for name, (_, _, held) in OBJECT_ROWS.items() if held == NULLED]
    await r.database.execute(f'UPDATE samples SET {FIELD} = NULL WHERE id = ANY($1::int[])', nulled)
    sidebar = await r.call(ADMIN, 'GET', f'/api/datasets/{OBJECTS}/field-stats')
    assert sidebar.status_code == 200, sidebar.text
    return Planted(OBJECTS, ids, 0)


def test_rule_9_5_through_the_wrapping_a_negation_of_a_list_column_is_answered_by_the_grids_own_row(regions_world):
    """Section 9 point 5, on both grids: a hiding test of a list column is answered by the grid's own row, as tags'
    are, now with the empty list a value: an image or object whose own list is there, empty or not, and holds no
    aphid (im-b's empty list, although its object holds aphid; ob-a1's and ob-e1's empty lists). Main read the empty
    list as no value. The show is asked first, as main answers it (by the row or its partner)."""

    async def scenario(r):
        planted = await plant_objects(r)

        async def grid(grain: str, stage: dict[str, Any]) -> set[str]:
            return planted.names(await selected(r, page_filter([stage], grain), OBJECTS))

        for grain in ('image', 'object'):
            assert await grid(grain, leaf(FIELD, 'in', ['aphid'])) == SHOWN_APHID[grain], grain
        for stage in (
            leaf(FIELD, 'not in', ['aphid']),
            leaf(FIELD, '!=', 'aphid'),
            negated(leaf(FIELD, 'in', ['aphid'])),
            negated(leaf(FIELD, 'contains', 'aphid')),
        ):
            for grain in ('image', 'object'):
                assert await grid(grain, stage) == OWN_ROW_NOT_APHID[grain], (stage, grain)

    regions_world(scenario)


def test_rule_9_5_pin_through_the_wrapping_a_show_of_a_list_column_is_met_by_the_row_or_its_partner(regions_world):
    """A pin: `in` and `contains` of a list column through the wrapping, as main answers them."""

    async def scenario(r):
        planted = await plant_objects(r)

        async def grid(grain: str, stage: dict[str, Any]) -> set[str]:
            return planted.names(await selected(r, page_filter([stage], grain), OBJECTS))

        for grain in ('image', 'object'):
            assert await grid(grain, leaf(FIELD, 'in', ['aphid'])) == SHOWN_APHID[grain], grain
            assert await grid(grain, leaf(FIELD, 'contains', 'aphid')) == SHOWN_APHID[grain], grain

    regions_world(scenario)


# --- Section 9 point 10: which rows a test asks through the wrapping, as tags' tests ask them on main ---

# The list each letter stands for: never set (added without the field), an empty list, a list with aphid, with mite.
HELD = {'U': UNSET, 'E': [], 'A': ['aphid'], 'M': ['mite']}
# Images, `im-<its list><its one object's list>` (no second letter: no object); objects, `ob-<its list><its image's
# list>`; objects with no image, `lo-<its list>`. Every pairing of the three states, and two with mite.
PAIRED_IMAGES = ['im-U', 'im-E', 'im-A', 'im-UU', 'im-UE', 'im-UA', 'im-EU', 'im-EE', 'im-EA', 'im-AU', 'im-AE']
PAIRED_IMAGES += ['im-AA', 'im-AM', 'im-MA']
PAIRED_OBJECTS = {  # an object: its image
    'ob-UU': 'im-UU',
    'ob-EU': 'im-UE',
    'ob-AU': 'im-UA',
    'ob-UE': 'im-EU',
    'ob-EE': 'im-EE',
    'ob-AE': 'im-EA',
    'ob-UA': 'im-AU',
    'ob-EA': 'im-AE',
    'ob-AA': 'im-AA',
    'ob-MA': 'im-AM',
    'ob-AM': 'im-MA',
    'lo-U': None,
    'lo-E': None,
    'lo-A': None,
}
PAIRED_ROWS = [*PAIRED_IMAGES, *PAIRED_OBJECTS]
SHOWS = {
    'in': leaf(FIELD, 'in', ['aphid']),
    'contains': leaf(FIELD, 'contains', 'aphid'),
    'is_not_null': leaf(FIELD, 'is_not_null'),
    'exists': leaf(FIELD, 'exists'),
    'is_empty': leaf(FIELD, 'is_empty'),
}
WHOLE = {'is_null': leaf(FIELD, 'is_null'), 'not_exists': leaf(FIELD, 'not_exists')}
OWN = {
    'not in': leaf(FIELD, 'not in', ['aphid']),
    '!=': leaf(FIELD, '!=', 'aphid'),
    'NOT in': negated(leaf(FIELD, 'in', ['aphid'])),
    'NOT contains': negated(leaf(FIELD, 'contains', 'aphid')),
    'is_not_empty': leaf(FIELD, 'is_not_empty'),
    'NOT is_empty': negated(leaf(FIELD, 'is_empty')),
}

# What each test shows on each grid, written out by hand from the rows' names.
PAIRED: dict[str, dict[str, set[str]]] = {
    'image': {
        'in': {'im-A', 'im-UA', 'im-EA', 'im-AU', 'im-AE', 'im-AA', 'im-AM', 'im-MA'},
        'is_not_null': set(PAIRED_IMAGES) - {'im-U', 'im-UU'},
        'is_empty': {'im-E', 'im-UE', 'im-EU', 'im-EE', 'im-EA', 'im-AE'},
        'is_null': {'im-U', 'im-UU'},
        'not in': {'im-E', 'im-EU', 'im-EE', 'im-EA', 'im-MA'},  # its own list is there and holds no aphid
        'is_not_empty': {'im-A', 'im-AU', 'im-AE', 'im-AA', 'im-AM', 'im-MA'},  # its own list holds an element
    },
    'object': {
        'in': {'ob-AU', 'ob-AE', 'ob-UA', 'ob-EA', 'ob-AA', 'ob-MA', 'ob-AM', 'lo-A'},
        'is_not_null': set(PAIRED_OBJECTS) - {'ob-UU', 'lo-U'},
        'is_empty': {'ob-EU', 'ob-UE', 'ob-EE', 'ob-AE', 'ob-EA', 'lo-E'},
        'is_null': {'ob-UU', 'lo-U'},
        'not in': {'ob-EU', 'ob-EE', 'ob-EA', 'ob-MA', 'lo-E'},
        'is_not_empty': {'ob-AU', 'ob-AE', 'ob-AA', 'ob-AM', 'ob-MA', 'lo-A'},
    },
}
for _grain in PAIRED.values():
    _grain['contains'], _grain['exists'], _grain['not_exists'] = _grain['in'], _grain['is_not_null'], _grain['is_null']
    _grain['!='] = _grain['NOT in'] = _grain['NOT contains'] = _grain['not in']
    _grain['NOT is_empty'] = _grain['is_not_empty']


def own_list(name: str) -> Any:
    return HELD[name.split('-')[1][0]]


def partners(name: str) -> list[str]:
    """The rows a grid's row is asked with: an image's objects, an object's image."""
    if name.startswith('im-'):
        return [item for item, image in PAIRED_OBJECTS.items() if image == name]
    image = PAIRED_OBJECTS[name]
    return [] if image is None else [image]


def paired_reading(grain: str, test: str) -> set[str]:
    """Section 9 point 10's words: a show is met by the row or a partner, `is null` and `not exists` by neither having
    a list, a negation and `is not empty` by the grid's own row. A list, empty or not, is a value."""
    rows = PAIRED_IMAGES if grain == 'image' else list(PAIRED_OBJECTS)

    def meets(held: Any, show: str) -> bool:
        listed = isinstance(held, list)
        return {
            'in': listed and 'aphid' in held,
            'contains': listed and 'aphid' in held,
            'is_not_null': listed,
            'exists': listed,
            'is_empty': held == [],
        }[show]

    found = set()
    for name in rows:
        asked = [own_list(name), *(own_list(partner) for partner in partners(name))]
        own = own_list(name)
        if test in SHOWS:
            shown = any(meets(held, test) for held in asked)
        elif test in WHOLE:
            shown = not any(isinstance(held, list) for held in asked)
        elif test in ('is_not_empty', 'NOT is_empty'):
            shown = isinstance(own, list) and own != []
        else:
            shown = isinstance(own, list) and 'aphid' not in own
        if shown:
            found.add(name)
    return found


def test_rule_9_10_the_reference_reads_the_points_words_as_the_hand_does():
    for grain, table in PAIRED.items():
        assert set(table) == {*SHOWS, *WHOLE, *OWN}, grain
        for test, names in table.items():
            assert paired_reading(grain, test) == names, (grain, test, sorted(paired_reading(grain, test)))


async def plant_paired(r: Any) -> Planted:
    """PAIRED_ROWS on OBJECTS, the list field made first: the images, then the objects naming them."""
    await r.dataset(OBJECTS)
    await make_field(r, OBJECTS)
    images = []
    for name in PAIRED_IMAGES:
        held = own_list(name)
        images.append(
            {
                'filepath': f'{name}.jpg',
                'metadata': {'plot': name},
                **({'array_fields': {FIELD: held}} if isinstance(held, list) else {}),
            },
        )
    added = await post(r, f'/api/datasets/{OBJECTS}/samples', {'samples': images})
    assert added.is_success, added.text
    ids = await ids_of(r, OBJECTS)
    objects = []
    for name, image in PAIRED_OBJECTS.items():
        held = own_list(name)
        objects.append(
            {
                'filepath': f'{name}.jpg',
                'media_type': 'object',
                'metadata': {'plot': name, **({} if image is None else {'parent_id': ids[image]})},
                **({'array_fields': {FIELD: held}} if isinstance(held, list) else {}),
            },
        )
    added = await post(r, f'/api/datasets/{OBJECTS}/samples', {'samples': objects})
    assert added.is_success, added.text
    ids = await ids_of(r, OBJECTS)
    assert set(ids) == set(PAIRED_ROWS), sorted(ids)
    sidebar = await r.call(ADMIN, 'GET', f'/api/datasets/{OBJECTS}/field-stats')
    assert sidebar.status_code == 200, sidebar.text
    return Planted(OBJECTS, ids, 0)


async def assert_paired(r: Any, planted: Planted, tests: dict[str, dict[str, Any]]) -> None:
    """Each test on each grid shows the rows PAIRED gives, through the grid's own request and its count."""
    for grain in ('image', 'object'):
        for test, node in tests.items():
            found = planted.names(await selected(r, page_filter([node], grain), OBJECTS))
            assert found == PAIRED[grain][test], (
                grain,
                test,
                'shown',
                sorted(found),
                'expected',
                sorted(PAIRED[grain][test]),
            )


def test_rule_9_10_pin_through_the_wrapping_in_and_contains_of_a_list_column_are_met_by_the_row_or_its_partner(
    regions_world,
):
    """A pin (section 9 point 10): `in` and `contains` of a list column, on both grids, are met by the grid's row or
    a partner (an image's object, an object's image), as tags' are on main."""

    async def scenario(r):
        planted = await plant_paired(r)
        await assert_paired(r, planted, {test: SHOWS[test] for test in ('in', 'contains')})

    regions_world(scenario)


def test_rule_9_10_through_the_wrapping_is_not_null_exists_and_is_empty_are_met_by_the_row_or_its_partner(
    regions_world,
):
    """Section 9 point 10, on both grids: `is not null` and `exists` show a row when it or a partner has a list, empty
    or not; `is empty` when it or a partner holds `[]` (im-UE: its object's empty list; ob-AE: its image's). Main read
    the empty list as no value. `in` is asked first, as main answers it."""

    async def scenario(r):
        planted = await plant_paired(r)
        await assert_paired(r, planted, {'in': SHOWS['in']})
        await assert_paired(r, planted, {test: SHOWS[test] for test in ('is_not_null', 'exists', 'is_empty')})

    regions_world(scenario)


def test_rule_9_10_through_the_wrapping_is_null_and_not_exists_are_read_for_the_row_as_a_whole(regions_world):
    """Section 9 point 10, on both grids: `is null` and `not exists` show a row when neither it nor a partner has a
    list (im-UU, ob-UU, and the rows never set with no partner); a row never set whose partner holds `[]` (im-UE,
    ob-UE) is not shown. Main read the empty list as no value. `in` is asked first."""

    async def scenario(r):
        planted = await plant_paired(r)
        await assert_paired(r, planted, {'in': SHOWS['in']})
        await assert_paired(r, planted, WHOLE)

    regions_world(scenario)


def test_rule_9_10_through_the_wrapping_a_negation_and_is_not_empty_are_answered_by_the_grids_own_row(regions_world):
    """Section 9 point 10, on both grids: `not in`, `!=` and a NOT around `in` or `contains` show a row whose own list
    is there, empty or not, and holds no aphid, whatever its partner holds (im-EA, ob-MA); `is not empty` and the NOT of
    `is empty` a row whose own list holds an element (not im-UA, whose object's does). Main read the empty list as no
    value, and answered `is not empty` as `is not null`, by the row or its partner. `in` is asked first."""

    async def scenario(r):
        planted = await plant_paired(r)
        await assert_paired(r, planted, {'in': SHOWS['in']})
        await assert_paired(r, planted, OWN)

    regions_world(scenario)


# --- Rule 3.4, section 9 point 6: the sidebar's count and tick ---


def test_rule_3_4_the_sidebar_counts_the_rows_never_set_and_its_no_value_tick_shows_exactly_them(regions_world):
    """Section 9 point 6: once the statistics are counted, the sidebar's entry for the list column has the count of
    the rows with no value (`null_count`, the number the page's "(no value)" tick is shown for): the rows never set,
    not the empty lists; and the filter the tick sends shows exactly those rows. Main's entry had no such count. A
    metadata field's count is asked first, as main gives it."""

    async def scenario(r):
        p = await plant(r)
        await count_statistics(r, COLUMN)
        entries = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{COLUMN}/field-stats'))
        by_name = {entry['name']: entry for entry in entries}
        assert by_name['colour'].get('null_count') == 1, by_name['colour']  # c4 has no colour
        assert by_name[FIELD].get('null_count') == len(NEVER_SET), by_name[FIELD]
        tick = {'field': FIELD, 'operator': 'is_null', 'value': None}
        assert await one_rule.shown(r, p, tick) == NEVER_SET

    regions_world(scenario)


# --- Rule 1.2, the first half of 8.3: a field made on a dataset that has samples, with a new name ---


def test_rule_1_2_a_list_field_made_with_a_name_nothing_else_uses_is_never_set_on_the_samples_already_there(
    regions_world,
):
    """A list field made with a name no dataset has used, on a dataset that already has samples: every one of them is
    never set (comes back `null`, `is null` finds them all), the new column has no default (rule 4.1), and a list
    written afterwards, an empty one included, is kept. Main gave every sample `[]`. The field's listing is asked
    first."""
    name = 'scouted_marks'  # no other test, and no other dataset, makes this name

    async def scenario(r):
        await r.dataset(COLUMN)
        samples = [{'filepath': f'{n}.jpg', 'metadata': {'plot': n}} for n in ('one', 'two', 'three')]
        assert (await post(r, f'/api/datasets/{COLUMN}/samples', {'samples': samples})).is_success
        assert await r.database.fetchval('SELECT 1 FROM sample_array_columns WHERE column_name = $1', name) is None
        await make_field(r, COLUMN, name)
        listed = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{COLUMN}/array-fields'))
        assert {'name': name} in listed, listed
        p = Planted(COLUMN, await ids_of(r, COLUMN), 0)
        assert await lists_held(r, p, name) == {'one': None, 'two': None, 'three': None}
        assert await one_rule.shown(r, p, leaf(name, 'is_null')) == {'one', 'two', 'three'}
        default = await r.database.fetchval(
            "SELECT column_default FROM information_schema.columns WHERE table_name = 'samples' AND column_name = $1",
            name,
        )
        assert default is None, default
        assert (await patch(r, p, 'one', {'array_fields': {name: []}})).status_code == 200
        assert (await patch(r, p, 'two', {'array_fields': {name: ['scratch']}})).status_code == 200
        assert await lists_held(r, p, name) == {'one': [], 'two': ['scratch'], 'three': None}

    regions_world(scenario)


# --- Rule 1.10, 8.4: tags and @local.tags are not touched ---


def test_rule_1_10_pin_tags_of_a_sample_added_without_them_come_back_empty_and_answer_every_test_as_before(
    regions_world,
):
    """A pin, on COLUMN (where a list column now holds never set): a sample added without tags comes back with
    `tags: []` in both forms of the samples query, never `null`; `tags is null` and `tags is empty` show the untagged
    samples, the negations only samples with a tag, `exists` a sample with a tag (contract g7 1.5 and g9 1.6)."""

    async def scenario(r):
        p = await plant(r)
        rows = await lists_held(r, p, 'tags')
        slim = await slim_lists_held(r, p, 'tags')
        for held in (rows, slim):
            assert {name: sorted(value) for name, value in held.items()} == {
                name: sorted(item.tags) for name, item in ROWS.items()
            }, held
        tagged = EVERY - UNTAGGED
        seen = {name for name, item in ROWS.items() if 'seen' in item.tags}
        done = {name for name, item in ROWS.items() if 'done' in item.tags}
        await assert_shows(
            r,
            p,
            [
                ('tags is null', UNTAGGED),
                ('tags is not null', tagged),
                ('tags is empty', UNTAGGED),
                ('tags is not empty', tagged),
                ('tags not in ("done")', tagged - done),
                ('NOT (tags contains "seen")', tagged - seen),
                (leaf('tags', '!=', 'seen'), tagged - seen),
                (leaf('tags', 'exists'), tagged),
                (leaf('tags', 'not_exists'), UNTAGGED),
            ],
        )

    regions_world(scenario)


def test_rule_1_10_pin_local_tags_never_written_come_back_as_the_empty_list(regions_world):
    """A pin: on a mirror, a sample whose own tags were never written comes back with `local_tags: []`, as one whose
    tags were written and emptied; `@local.tags is null` shows both."""

    async def scenario(r):
        p = await plant_local(r)
        body = await ok(await post(r, f'/api/datasets/{p.dataset_id}/samples/query', {'limit': 100}))
        held = {p.names([item['id']]).pop(): item['local_tags'] for item in body['items']}
        assert held == {name: writes[-1] if writes else [] for name, writes in LOCAL_WRITES.items()}, held
        untagged = {name for name, writes in LOCAL_WRITES.items() if not (writes and writes[-1])}
        assert await one_rule.shown(r, p, leaf('@local.tags', 'is_null')) == untagged

    regions_world(scenario)


# --- 8.5 and section 9 points 3 and 4: the SDK as a script uses it ---


def test_8_5_the_sdk_writes_the_three_states_and_reads_never_set_back_as_none(regions_world, monkeypatch):
    """A script makes the field, adds a sample without it, one with `[]` and one with a list, and reads them back,
    iterating the dataset and by path: `sample.pests` is None for the sample never set, `[]` and the list for the
    others. Main read the sample added without the field as `[]`. The list is asked first."""

    async def scenario(r):
        await r.dataset(COLUMN)
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def script() -> tuple[dict[str, Any], dict[str, Any]]:
            dataset = Dataset(f'dataset-{COLUMN}')
            dataset.create_array_field(FIELD)
            dataset.add_samples(
                [
                    Sample('s-unset.jpg', metadata={'plot': 'unset'}),
                    Sample('s-empty.jpg', metadata={'plot': 'empty'}, array_fields={FIELD: []}),
                    Sample('s-aphid.jpg', metadata={'plot': 'aphid'}, array_fields={FIELD: ['aphid']}),
                ],
            )
            iterated = {sample.filepath: sample.pests for sample in dataset.all()}
            again = Dataset(f'dataset-{COLUMN}')  # loaded again, as a later script would
            by_path = {path: again[path].pests for path in ('s-unset.jpg', 's-empty.jpg', 's-aphid.jpg')}
            return iterated, by_path

        iterated, by_path = await asyncio.to_thread(script)
        for read in (iterated, by_path):
            assert (read['s-aphid.jpg'], read['s-empty.jpg']) == (['aphid'], []), read
            assert read['s-unset.jpg'] is None, read

    regions_world(scenario)


def test_9_4_the_sdk_adds_a_sample_never_set_when_the_field_is_none(regions_world, monkeypatch):
    """A script that gives the field `None` on a new sample adds it never set (main refused the request, 422)."""

    async def scenario(r):
        p = await small(r, COLUMN, {'aphid': ['aphid']})
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def script() -> Any:
            dataset = Dataset(f'dataset-{COLUMN}')
            dataset.list_array_fields()
            dataset.add_samples([Sample('s-none.jpg', metadata={'plot': 'none'}, array_fields={FIELD: None})])
            return dataset['s-none.jpg'].pests

        assert await asyncio.to_thread(script) is None
        p = Planted(COLUMN, await ids_of(r, COLUMN), 0)
        assert await lists_held(r, p) == {'aphid': ['aphid'], 's-none': None}

    regions_world(scenario)


def test_9_3_sample_save_never_turns_a_field_never_set_into_an_empty_list(regions_world, monkeypatch):
    """Section 9 point 3: a script loads a sample never set (its column written NULL, so main can hold it too), changes
    its metadata and saves it: the field stays never set, and the metadata is saved. A sample with a list saved the
    same way keeps its list (asked first). Main's save() wrote `[]`."""

    async def scenario(r):
        p = await small(r, COLUMN, {'nulled': NULLED, 'aphid': ['aphid']})
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def script() -> None:
            dataset = Dataset(f'dataset-{COLUMN}')
            dataset.list_array_fields()
            for path in ('aphid.jpg', 'nulled.jpg'):
                sample = dataset[path]
                sample.metadata['seen'] = True
                sample.save()

        await asyncio.to_thread(script)
        rows = await ok(await post(r, f'/api/datasets/{COLUMN}/samples/query', {'limit': 10}))
        assert all(item['metadata'].get('seen') is True for item in rows['items']), rows['items']
        held = await lists_held(r, p)
        assert held['aphid'] == ['aphid'], held
        assert held['nulled'] is None, held

    regions_world(scenario)


def test_9_3_a_script_unsets_the_field_by_saving_none(regions_world, monkeypatch):
    """A script that sets the field to None and saves the sample unsets it (an explicit unset; main refused it,
    422). Saving another list is asked first."""

    async def scenario(r):
        p = await small(r, COLUMN, {'aphid': ['aphid'], 'mite': ['mite']})
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def script(path: str, value: Any) -> None:
            dataset = Dataset(f'dataset-{COLUMN}')
            dataset.list_array_fields()
            sample = dataset[path]
            sample.pests = value
            sample.save()

        await asyncio.to_thread(script, 'mite.jpg', ['mite', 'thrips'])
        assert (await lists_held(r, p))['mite'] == ['mite', 'thrips']
        await asyncio.to_thread(script, 'aphid.jpg', None)
        assert await lists_held(r, p) == {'aphid': None, 'mite': ['mite', 'thrips']}

    regions_world(scenario)


def test_9_3_add_samples_from_keeps_the_three_states(regions_world, monkeypatch):
    """A copy of a dataset (`add_samples_from`) keeps the three states: a sample never set (added without the field,
    and one whose column was written NULL) is never set in the copy, `[]` is `[]`, a list is the list. Main's copy
    wrote `[]` for the rows never set. The list is asked first."""

    async def scenario(r):
        await small(r, COLUMN, {'unset': UNSET, 'nulled': NULLED, 'empty': [], 'aphid': ['aphid', 'mite']})
        await r.dataset(COPY)
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def script() -> dict[str, Any]:
            target = Dataset(f'dataset-{COPY}')
            target.add_samples_from(Dataset(f'dataset-{COLUMN}'), embedding_fields=[], hash_fields=[])
            return {sample.filepath: sample.pests for sample in target.all()}

        copied = sorted_lists(await asyncio.to_thread(script))
        assert copied['aphid.jpg'] == ['aphid', 'mite'], copied
        assert copied == {'unset.jpg': None, 'nulled.jpg': None, 'empty.jpg': [], 'aphid.jpg': ['aphid', 'mite']}
        q = Planted(COPY, await ids_of(r, COPY), 0)
        assert sorted_lists(await lists_held(r, q)) == {
            'unset': None,
            'nulled': None,
            'empty': [],
            'aphid': ['aphid', 'mite'],
        }

    regions_world(scenario)


class FakeServer:
    """The two servers sync_between_instances talks to, by URL: a source with the list field in each state."""

    instances: dict[str, 'FakeServer'] = {}

    def __init__(self, url: str):
        self.base_url = url
        self.added: list[dict[str, Any]] = []
        self.session = SimpleNamespace(patch=lambda *args, **kwargs: None)
        FakeServer.instances[url] = self

    def login(self, user: str, password: str) -> None:
        pass

    def get_dataset(self, name: str) -> Optional[dict[str, Any]]:
        return {'id': 1, 'name': name} if self.base_url == 'source' else None

    def create_dataset(self, name: str, description: Any = None, is_global: bool = False) -> dict[str, Any]:
        return {'id': 2, 'name': name}

    def get_samples(self, dataset_id: int, limit: int, offset: int) -> dict[str, Any]:
        return {'total': 3}

    def get_schema(self, dataset_id: int, filters: Any = None, lightning_mode: bool = True) -> list[dict[str, Any]]:
        return [{'name': 'plot', 'origin': 'metadata'}]

    def list_array_fields(self, dataset_id: int) -> list[dict[str, Any]]:
        return [{'name': FIELD}, {'name': 'tags'}] if self.base_url == 'source' else [{'name': 'tags'}]

    def create_array_field(self, dataset_id: int, name: str) -> None:
        pass

    def query_samples(self, dataset_id: int, limit: int, offset: int) -> dict[str, Any]:
        rows = [
            {'id': 1, 'filepath': '/data/unset.jpg', 'tags': [], 'metadata': {'plot': 'unset'}, FIELD: None},
            {'id': 2, 'filepath': '/data/empty.jpg', 'tags': [], 'metadata': {'plot': 'empty'}, FIELD: []},
            {'id': 3, 'filepath': '/data/aphid.jpg', 'tags': [], 'metadata': {'plot': 'aphid'}, FIELD: ['aphid']},
        ]
        return {'items': rows if offset == 0 else []}

    def add_samples(self, dataset_id: int, payload: list[dict[str, Any]], on_duplicate: str) -> None:
        self.added.extend(payload)

    def lookup_samples(self, dataset_id: int, field: str, values: list[Any]) -> dict[Any, int]:
        return {value: 10 + number for number, value in enumerate(values)}


def test_9_3_sync_between_instances_sends_an_empty_list_and_leaves_never_set_out(monkeypatch):
    """Section 9 point 3: the sync between two servers sends a sample's empty list as `[]` and leaves a field never
    set out; a list is sent as it is (asked first). Main left the empty list out, so the target stored its default."""
    monkeypatch.setattr(sync_between_instances, 'Client', FakeServer)
    sync_between_instances.sync('source', 'target', 'demo', skip_embeddings=True, skip_hashes=True)
    sent = {row['filepath']: row.get('array_fields') or {} for row in FakeServer.instances['target'].added}
    assert sent['/data/aphid.jpg'] == {FIELD: ['aphid']}, sent
    assert FIELD not in sent['/data/unset.jpg'], sent
    assert sent['/data/empty.jpg'] == {FIELD: []}, sent


# --- 8.6 and section 9 points 3 and 9: a version taken and restored ---


def test_8_6_a_version_restored_brings_the_three_states_back(regions_world):
    """A version taken, the lists changed (the sample never set given a list, the empty list given one, a list emptied
    by the bulk edit), the version restored: as they were. Main restored `[]` for the sample added without the field.
    The list is asked first."""

    async def scenario(r):
        p = await small(r, COLUMN, {'unset': UNSET, 'empty': [], 'aphid': ['aphid']})
        before = await lists_held(r, p)
        version = await version_now(r, COLUMN, 'three states')
        assert (await patch(r, p, 'unset', {'array_fields': {FIELD: ['x']}})).status_code == 200
        assert (await patch(r, p, 'empty', {'array_fields': {FIELD: ['y']}})).status_code == 200
        await bulk_edit(r, p, {'aphid'}, [], ['aphid'])
        assert await lists_held(r, p) == {'unset': ['x'], 'empty': ['y'], 'aphid': []}
        await restore(r, COLUMN, version)
        held = await lists_held(r, p)
        assert (held['aphid'], held['empty']) == (['aphid'], []), held
        assert held == before == {'unset': None, 'empty': [], 'aphid': ['aphid']}, (held, before)

    regions_world(scenario)


def test_9_3_a_restore_leaves_a_list_column_the_dataset_did_not_have_at_the_version_never_set(regions_world):
    """Section 9 point 3: the column was there (another dataset made it) but the dataset didn't register the field when
    the version was taken, its rows holding the old default `[]` (written so, as rows written before the upgrade hold
    it). The dataset then registers the field and writes lists; restored, every one of its rows is never set, where
    main wrote the old default back. The other dataset's lists are untouched (asked first, with the restore's other
    fields: its metadata comes back)."""

    async def scenario(r):
        other = await small(r, OTHER, {'kept': ['rust'], 'kept-empty': []})
        await r.dataset(COLUMN)
        samples = [{'filepath': f'{n}.jpg', 'metadata': {'plot': n}} for n in ('one', 'two', 'three')]
        assert (await post(r, f'/api/datasets/{COLUMN}/samples', {'samples': samples})).is_success
        await r.database.execute(f"UPDATE samples SET {FIELD} = '{{}}' WHERE dataset_id = $1", COLUMN)
        version = await version_now(r, COLUMN, 'before the field')
        await make_field(r, COLUMN)
        p = Planted(COLUMN, await ids_of(r, COLUMN), 0)
        for name, written in (('one', ['aphid']), ('two', []), ('three', ['mite'])):
            body = {'array_fields': {FIELD: written}, 'metadata': {'plot': 'changed'}}
            assert (await patch(r, p, name, body)).status_code == 200
        await restore(r, COLUMN, version)
        rows = await ok(await post(r, f'/api/datasets/{COLUMN}/samples/query', {'limit': 10}))
        assert {item['metadata']['plot'] for item in rows['items']} == {'one', 'two', 'three'}
        assert await lists_held(r, other) == {'kept': ['rust'], 'kept-empty': []}
        assert await lists_held(r, p) == {'one': None, 'two': None, 'three': None}

    regions_world(scenario)


async def as_before_the_registry(r: Any, version: int) -> None:
    """The version as one made before versions recorded the field registry: no fields, no registry version."""
    await r.database.execute(
        "UPDATE dataset_versions SET dataset_snapshot = dataset_snapshot - 'fields' - 'field_registry_version' "
        'WHERE id = $1',
        version,
    )
    snapshot = await r.database.fetchval('SELECT dataset_snapshot FROM dataset_versions WHERE id = $1', version)
    snapshot = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
    assert 'fields' not in snapshot and 'field_registry_version' not in snapshot, snapshot


def test_9_9_a_restore_of_a_version_without_a_field_registry_brings_back_a_field_registered_before_it(regions_world):
    """Section 9 point 9: a version that recorded no list fields (made before versions recorded the registry): the
    list column counts as the dataset's at that version because its registration is older than the version, so the
    restore brings back what the version holds: its `[]` (as a version taken before the upgrade does, rule 1.3), its
    list and its never set. The list and the empty list are asked first, as main restores them."""

    async def scenario(r):
        p = await small(r, COLUMN, {'empty': [], 'aphid': ['aphid'], 'unset': UNSET})
        version = await version_now(r, COLUMN, 'old format')
        await as_before_the_registry(r, version)
        for name, written in (('empty', ['x']), ('aphid', []), ('unset', ['y'])):
            assert (await patch(r, p, name, {'array_fields': {FIELD: written}})).status_code == 200
        await restore(r, COLUMN, version)
        held = await lists_held(r, p)
        assert (held['empty'], held['aphid']) == ([], ['aphid']), held
        assert held['unset'] is None, held

    regions_world(scenario)


def test_9_9_a_restore_of_a_version_without_a_field_registry_leaves_a_field_registered_after_it_never_set(
    regions_world,
):
    """Section 9 point 9: a version that recorded no list fields, taken while the column was there (another dataset
    made it) holding the old default `[]` in this dataset's rows; the dataset registers the field after the version
    and writes lists. Its registration is newer than the version, so the restore leaves the field never set. Main
    wrote the old default back. The other dataset's lists are asked first."""

    async def scenario(r):
        other = await small(r, OTHER, {'kept': ['rust']})
        await r.dataset(COLUMN)
        samples = [{'filepath': f'{n}.jpg', 'metadata': {'plot': n}} for n in ('one', 'two')]
        assert (await post(r, f'/api/datasets/{COLUMN}/samples', {'samples': samples})).is_success
        await r.database.execute(f"UPDATE samples SET {FIELD} = '{{}}' WHERE dataset_id = $1", COLUMN)
        version = await version_now(r, COLUMN, 'old format, before the field')
        await as_before_the_registry(r, version)
        await make_field(r, COLUMN)
        p = Planted(COLUMN, await ids_of(r, COLUMN), 0)
        assert (await patch(r, p, 'one', {'array_fields': {FIELD: ['aphid']}})).status_code == 200
        assert (await patch(r, p, 'two', {'array_fields': {FIELD: []}})).status_code == 200
        await restore(r, COLUMN, version)
        assert await lists_held(r, other) == {'kept': ['rust']}
        assert await lists_held(r, p) == {'one': None, 'two': None}

    regions_world(scenario)


def test_9_9_a_restore_sets_the_lists_of_a_column_that_was_not_there_at_the_version_to_never_set_and_keeps_the_field(
    regions_world,
):
    """Section 9 point 9: the column wasn't there when the version was taken (the version has no key for it); the
    dataset then makes the field and writes lists. Restored, the dataset's rows that hold a list there are never set,
    and the field stays registered. Another dataset that registered the name since keeps its lists. Main kept the
    lists written since. The field's listing and the other dataset's lists are asked first."""
    name = 'restore_marks'  # no other test, and no other dataset, makes this name

    async def scenario(r):
        await r.dataset(COLUMN)
        samples = [{'filepath': f'{n}.jpg', 'metadata': {'plot': n}} for n in ('one', 'two', 'three')]
        assert (await post(r, f'/api/datasets/{COLUMN}/samples', {'samples': samples})).is_success
        assert await r.database.fetchval('SELECT 1 FROM sample_array_columns WHERE column_name = $1', name) is None
        version = await version_now(r, COLUMN, 'before the column')
        await make_field(r, COLUMN, name)
        p = Planted(COLUMN, await ids_of(r, COLUMN), 0)
        assert (await patch(r, p, 'one', {'array_fields': {name: ['aphid']}})).status_code == 200
        assert (await patch(r, p, 'two', {'array_fields': {name: []}})).status_code == 200
        await r.dataset(OTHER)
        added = await post(r, f'/api/datasets/{OTHER}/samples', {'samples': [{'filepath': 'kept.jpg'}]})
        assert added.is_success, added.text
        await make_field(r, OTHER, name)
        other = Planted(OTHER, await ids_of(r, OTHER), 0)
        assert (await patch(r, other, 'kept', {'array_fields': {name: ['rust']}})).status_code == 200
        await restore(r, COLUMN, version)
        listed = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{COLUMN}/array-fields'))
        assert {'name': name} in listed, listed
        assert await lists_held(r, other, name) == {'kept': ['rust']}
        assert await lists_held(r, p, name) == {'one': None, 'two': None, 'three': None}

    regions_world(scenario)


# --- 8.6 and section 9 point 7: a mirror ---


async def primary_with_lists(database: Any, lists: dict[str, Any]) -> dict[str, int]:
    """The primary's dataset 1 with the list field and a sample per name (UNSET: added without the field)."""
    await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name=FIELD), user=USER)
    samples = [
        {
            'filepath': f'{name}.jpg',
            'metadata': {'plot': name},
            **({'array_fields': {FIELD: held}} if isinstance(held, list) else {}),
        }
        for name, held in lists.items()
    ]
    await datasets.add_samples(1, {'samples': samples}, user=USER)
    rows = await database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = 1')
    return {row['filepath'][: -len('.jpg')]: row['id'] for row in rows}


async def on_the_mirror(database: Any, mirror: int, statement: str, *args: Any) -> None:
    """A statement on a mirror's rows, run as the mirror's own sync writes them: in a transaction that names the
    mirror (migration 0009's guard refuses every other writer of a mirror). Setup, under bulk_setup's timeout."""
    async with test_field_registry_postgres.bulk_setup(database) as connection:
        await connection.execute("SELECT set_config('fridge.mirror_sync', $1, TRUE)", str(mirror))
        await connection.execute(statement, *args)


async def mirrored_lists(database: Any) -> dict[str, Any]:
    mirror = (await follower.mirror_of(database))['dataset_id']
    rows = await database.fetch(
        f'SELECT s.filepath, s.{FIELD} FROM mirror_samples m JOIN samples s ON s.id = m.sample_id '
        'WHERE m.dataset_id = $1',
        mirror,
    )
    return {row['filepath'][: -len('.jpg')]: row[FIELD] for row in rows}


def test_8_6_a_mirror_receives_the_three_states_as_they_are(monkeypatch, registry_database):
    """A mirror's first copy, then the feed: the sample never set arrives never set, `[]` as `[]`, the list as the
    list; then the primary's changes arrive as they are (a list given to the sample never set, the empty list given a
    list, a list emptied, and a field unset by `null`). Main's primary stored `[]` for the sample added without the
    field. The list is asked first."""

    async def scenario(database):
        _, link_id = await follower.setup(monkeypatch, database)
        ids = await primary_with_lists(database, {'unset': UNSET, 'empty': [], 'aphid': ['aphid'], 'mite': ['mite']})
        follower_id = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower_id, 1, followers.FollowOptions(), user=USER)
        await follower.sync(database, link_id)
        held = await mirrored_lists(database)
        assert (held['aphid'], held['empty']) == (['aphid'], []), held
        assert held == {'unset': None, 'empty': [], 'aphid': ['aphid'], 'mite': ['mite']}, held
        for name, written in (('unset', ['x']), ('empty', ['y'])):
            update = datasets.SampleUpdate(array_fields={FIELD: written})
            await datasets.update_sample(1, ids[name], update, user=USER)
        request = datasets.BulkTagRequest(sample_ids=[ids['aphid']], field_name=FIELD, remove_tags=['aphid'])
        await datasets.bulk_tag_samples(1, request, user=USER)
        await datasets.update_sample(1, ids['mite'], datasets.SampleUpdate(array_fields={FIELD: None}), user=USER)
        await follower.sync(database, link_id)
        assert await mirrored_lists(database) == {'unset': ['x'], 'empty': ['y'], 'aphid': [], 'mite': None}

    registry_database(scenario)


def test_9_7_a_mirror_takes_null_from_its_primary_as_never_set(monkeypatch, registry_database):
    """Section 9 point 7: the primary's blocks give `null` for a sample never set (its column written NULL, so main
    can hold it too), and the mirror stores it never set: main's mirror stored `[]` for it. The blocks' list and empty
    list are asked first, as main gives them."""

    async def scenario(database):
        _, link_id = await follower.setup(monkeypatch, database)
        ids = await primary_with_lists(database, {'nulled': ['x'], 'empty': [], 'aphid': ['aphid']})
        await database.execute(f'UPDATE samples SET {FIELD} = NULL WHERE id = $1', ids['nulled'])
        follower_id = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower_id, 1, followers.FollowOptions(), user=USER)
        sub = dict(await database.fetchrow('SELECT * FROM followers WHERE id = $1', follower_id))
        blocks = sorted({sample_id // followers.BLOCK for sample_id in ids.values()})
        answer = await followers.follower_block_samples(1, followers.BlockRequest(blocks=blocks), follower=sub)
        sent = {row['filepath'][: -len('.jpg')]: row for row in answer['samples']}
        assert (sent['aphid'][FIELD], sent['empty'][FIELD]) == (['aphid'], []), sent
        assert FIELD in sent['nulled'] and sent['nulled'][FIELD] is None, sent['nulled']
        await follower.sync(database, link_id)
        assert await mirrored_lists(database) == {'nulled': None, 'empty': [], 'aphid': ['aphid']}

    registry_database(scenario)


# --- 8.2 and 8.7: the upgrade ---

RELEASE = 'v2.14.0'  # the last release before this piece: its schema is the database before the new migration
LAST_ON_MAIN = '0044'  # the last migration on the main this was written against
UPGRADE_SEED = """
INSERT INTO datasets (id, name) VALUES (1, 'listed'), (2, 'other');
SELECT setval(pg_get_serial_sequence('datasets', 'id'), 2);
ALTER TABLE samples ADD COLUMN pests TEXT[] DEFAULT '{}';
CREATE INDEX idx_samples_pests_gin ON samples USING gin (pests);
ALTER TABLE samples ADD COLUMN old_marks TEXT[] DEFAULT '{}';
INSERT INTO sample_array_columns (column_name) VALUES ('pests'), ('old_marks') ON CONFLICT DO NOTHING;
INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, is_promoted)
    VALUES (1, 'pests', 'list', 'native_array', TRUE);
INSERT INTO samples (dataset_id, filepath, media_type, metadata) VALUES
    (1, 's3://upgrade/a.jpg', 'image', '{"plot": "a"}'),
    (2, 's3://upgrade/d.jpg', 'image', '{"plot": "d"}');
INSERT INTO samples (dataset_id, filepath, media_type, metadata, pests) VALUES
    (1, 's3://upgrade/b.jpg', 'image', '{"plot": "b"}', '{aphid}'),
    (1, 's3://upgrade/c.jpg', 'image', '{"plot": "c"}', '{}');
"""
DEFAULTS = (
    "SELECT string_agg(column_name || '=' || coalesce(column_default, 'none'), ',' ORDER BY column_name) "
    "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'samples' "
    "AND column_name IN ('pests', 'old_marks', 'tags', 'local_tags')"
)
ROW_VERSIONS = "SELECT string_agg(filepath || '@' || xmin::text, ',' ORDER BY filepath) FROM samples"
ROWS_HELD = (
    "SELECT string_agg(filepath || '=' || coalesce(pests::text, 'null') || '/' || coalesce(old_marks::text, 'null'), "
    "',' ORDER BY filepath) FROM samples"
)


def new_migrations() -> list[Any]:
    found = sorted(
        (migration for migration in MIGRATIONS.glob('*.sql') if migration.name[:4] > LAST_ON_MAIN),
        key=lambda migration: migration.name.encode(),
    )
    assert found, f'no migration after {LAST_ON_MAIN}: the upgrade of section 4 is not there'
    return found


def before_this_piece(make: Any, psql: Any) -> str:
    old_schema = subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{RELEASE}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = make(old_schema + UPGRADE_SEED)
    apply_before(psql, name, f'{int(LAST_ON_MAIN) + 1:04d}')
    return name


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'), reason='Requires disposable PostgreSQL')
def test_4_1_the_migration_takes_the_default_off_every_list_column_rewrites_no_row_and_can_be_run_again(database):
    """Section 4 and 8.7: on a database before this piece, with a list column a dataset registers (`pests`) and one
    no dataset registers any more (`old_marks`), the new migration takes the default off both, leaves `tags` and
    `local_tags` theirs, rewrites no row (every row keeps its row version and what it holds), and run again changes
    nothing. The defaults of the columns `schema.sql` makes agree with a fresh install's. The state before it is asked
    first."""
    make, psql, _, migrate = database
    name = before_this_piece(make, psql)
    before = psql(name, DEFAULTS)
    assert before == "local_tags='{}'::text[],old_marks='{}'::text[],pests='{}'::text[],tags='{}'::text[]", before
    versions_before, held_before = psql(name, ROW_VERSIONS), psql(name, ROWS_HELD)
    migrations = new_migrations()
    for migration in migrations:
        apply_migration(psql, name, migration)
    after = "local_tags='{}'::text[],old_marks=none,pests=none,tags='{}'::text[]"
    assert psql(name, DEFAULTS) == after
    assert (psql(name, ROW_VERSIONS), psql(name, ROWS_HELD)) == (versions_before, held_before)
    for migration in migrations:  # run again, as a rerun of the runner would if its ledger row were lost
        psql(name, f'BEGIN;\n{migration.read_text()}\nCOMMIT;\n')
    assert psql(name, DEFAULTS) == after
    assert (psql(name, ROW_VERSIONS), psql(name, ROWS_HELD)) == (versions_before, held_before)
    assert migrate(name).returncode == 0
    fresh = make((ROOT / 'backend/db/schema.sql').read_text())
    shared = (
        "SELECT string_agg(column_name || '=' || coalesce(column_default, 'none'), ',' ORDER BY column_name) "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'samples' "
        "AND column_name NOT IN ('pests', 'old_marks')"
    )
    assert psql(name, shared) == psql(fresh, shared)


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'), reason='Requires disposable PostgreSQL')
def test_8_2_rows_written_before_the_upgrade_keep_their_empty_list_and_a_sample_added_after_is_never_set(
    database,
    monkeypatch,
):
    """Rule 1.3 and 3.2, 8.2: a database before this piece, its list column holding `[]` in rows written then (the
    old default, and one written `[]`), upgraded as scripts/apply_migrations.sh does. Read by the app: those rows
    still come back `[]`, a NOT shows them, `is empty` finds them and `is null` does not; a sample added afterwards
    without the field is never set. Main's `is null` showed the empty lists. The list is asked first."""
    make, psql, _, migrate = database
    name = before_this_piece(make, psql)
    result_ = migrate(name)
    assert result_.returncode == 0, f'apply_migrations.sh failed\n{result_.stdout}\n{result_.stderr}'

    async def after() -> None:
        url = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()
        pool = await asyncpg.create_pool(url, min_size=1, max_size=4, init=init_connection)
        monkeypatch.setattr(database_module.db, 'pool', pool)
        monkeypatch.setattr(datasets, '_array_columns_cache', None)
        try:
            upgraded = Database()
            upgraded.pool = pool
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                w = World(upgraded, client)
                await w.user(ADMIN, 'admin')
                p = Planted(1, {f'{n}': i for n, i in await names_and_ids(upgraded)}, 0)
                held = await lists_held(w, p)
                assert held['b'] == ['aphid'], held
                assert (held['a'], held['c']) == ([], []), held
                for form, names in (
                    (leaf(FIELD, 'is_empty'), {'a', 'c'}),
                    (leaf(FIELD, 'is_null'), set()),
                    (negated(leaf(FIELD, 'in', ['aphid'])), {'a', 'c'}),
                    (leaf(FIELD, 'not in', ['aphid']), {'a', 'c'}),
                ):
                    found = p.names(await shown_ids(w, form, 1))
                    assert found == names, (form, sorted(found))
                added = await w.call(
                    ADMIN,
                    'POST',
                    '/api/datasets/1/samples',
                    json={'samples': [{'filepath': 'e.jpg'}]},
                )
                assert added.status_code == 200, added.text
                p = Planted(1, {f'{n}': i for n, i in await names_and_ids(upgraded)}, 0)
                assert (await lists_held(w, p))['e'] is None
                assert p.names(await shown_ids(w, leaf(FIELD, 'is_null'), 1)) == {'e'}
        finally:
            await pool.close()

    asyncio.run(after())


async def shown_ids(r: Any, node: Any, dataset_id: int) -> set[int]:
    """samples/query's rows for the filter, its count agreeing (the region tests' `selected`, for any world)."""
    return await selected(r, node, dataset_id)


async def names_and_ids(source: Any) -> list[tuple[str, int]]:
    rows = await source.fetch('SELECT id, filepath FROM samples WHERE dataset_id = 1')
    return [(row['filepath'].rsplit('/', 1)[-1][: -len('.jpg')], row['id']) for row in rows]


# --- Rule 1.2, section 9 points 1 and 9, 8.3's second half: a dataset registers a name another dataset made ---

SMALL = 1000  # the most samples whose registration is done inside the request (section 9 point 1)
LARGE = 9000  # past it, and more than two blocks of 4,096 samples: the job walks it in steps
WAIT = {'wait': 'false'}  # as a field's promotion takes it: the job's handle at once, not after five seconds


class RunnerPool:
    """The runner's pool as job_service.run_service gets it, around this test's pool: the readiness check before the
    runner claims anything stops the service, so its handlers are built and bound to this pool, and nothing runs."""

    def __init__(self, pool: Any, stop: Any):
        self._pool, self._stop = pool, stop

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
        if query == job_service.LEDGER_READY_SQL:
            self._stop()
            return False
        return await self._pool.fetchval(query, *args, **kwargs)


async def runner_handlers(pool: Any, monkeypatch: Any) -> dict[str, Any]:
    """Every job kind's handler, as the job runner has them (whatever the new job's kind is named)."""
    seen: dict[str, Any] = {}
    callbacks: dict[int, Any] = {}
    loop = asyncio.get_running_loop()

    @asynccontextmanager
    async def local_runner(dsn: str, handlers: dict[str, Any], **options: Any) -> Any:
        seen['handlers'] = handlers
        stop = callbacks[signal.SIGTERM]
        yield SimpleNamespace(workers=[SimpleNamespace(pool=RunnerPool(pool, stop))], run=None, stop=stop)

    monkeypatch.setattr(loop, 'add_signal_handler', lambda signum, callback: callbacks.update({signum: callback}))
    monkeypatch.setattr(loop, 'remove_signal_handler', lambda signum: None)
    monkeypatch.setattr(job_service, 'local_runner', local_runner)
    await asyncio.wait_for(job_service.run_service('unused'), 30)
    return seen['handlers']


async def claim(pool: Any, kind: str) -> Any:
    job = await job_queue.claim_job(pool, 'list-column-spec', kinds=[kind], lease_seconds=600)
    assert job is not None, f'no {kind} job to claim'
    return job


async def run_step(pool: Any, handlers: dict[str, Any], job: Any) -> tuple[Any, Any]:
    """One step of a claimed job, published as a runner slot publishes it: (the job after it, the step's result). A
    step that meets a busy lock is run again from the same checkpoint, as the runner does."""
    for _ in range(50):
        try:
            outcome = await handlers[job['kind']](job)
            break
        except job_queue.QueueContended:
            await asyncio.sleep(0.05)
    else:
        pytest.fail('the step stayed contended through 50 attempts')
    if outcome.committed:
        return dict(await pool.fetchrow('SELECT * FROM jobs WHERE id = $1', job['id'])), outcome
    job = await job_queue.publish_step(
        pool,
        job['id'],
        job['lease_token'],
        checkpoint=outcome.checkpoint,
        done=outcome.done,
        total=outcome.total,
        message=outcome.message,
        result=outcome.result,
        finished=outcome.finished,
        publisher=outcome.publish,
    )
    return job, outcome


async def run_to_end(pool: Any, handlers: dict[str, Any], job: Any) -> Any:
    for _ in range(10000):
        if job['state'] == 'succeeded':
            return job
        assert job['state'] == 'running', dict(job)
        job, _ = await run_step(pool, handlers, job)
    pytest.fail('the job did not finish')


async def stop_and_take_up(pool: Any, job: Any) -> Any:
    """The runner running the job stops between two steps: its lease runs out, and once the queue makes the job
    available again (after its retry pause) a runner claims it and goes on from its checkpoint."""
    await pool.execute(
        "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = $1",
        job['id'],
    )
    for _ in range(240):
        again = await job_queue.claim_job(pool, 'list-column-spec', kinds=[job['kind']], lease_seconds=600)
        if again is not None:
            assert again['id'] == job['id'], 'another job was claimed'
            return again
        await asyncio.sleep(0.5)
    pytest.fail('the stopped job was never available again')


async def older_rows(database: Any, dataset_id: int, count: int, field: str = FIELD) -> None:
    """`count` samples written before the upgrade: the list column holds the old default `[]` in each."""
    async with test_field_registry_postgres.bulk_setup(database) as connection:
        await connection.execute(
            f'INSERT INTO samples (dataset_id, filepath, media_type, metadata, {field}) '
            "SELECT $1, 'old-' || g || '.jpg', 'image', jsonb_build_object('plot', 'old-' || g), '{}' "
            'FROM generate_series(1, $2) g',
            dataset_id,
            count,
        )


async def held_counts(database: Any, dataset_id: int, field: str = FIELD) -> tuple[int, int]:
    """(rows never set, rows holding `[]`) of the dataset, read in the table: what a walk has done so far."""
    row = await database.fetchrow(
        f"SELECT count(*) FILTER (WHERE {field} IS NULL) AS unset, count(*) FILTER (WHERE {field} = '{{}}') AS empty "
        'FROM samples WHERE dataset_id = $1',
        dataset_id,
    )
    return int(row['unset']), int(row['empty'])


async def total(r: Any, dataset_id: int, node: Any) -> int:
    body = await ok(await post(r, f'/api/datasets/{dataset_id}/samples/query', {'limit': 1, 'filters': node}), node)
    return int(body['total'])


async def listed(r: Any, dataset_id: int) -> set[str]:
    fields = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/array-fields'))
    return {item['name'] for item in fields}


async def schema_names(r: Any, dataset_id: int) -> set[str]:
    return {entry['name'] for entry in await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/schema'))}


async def modified_at(database: Any, dataset_id: int) -> str:
    return await database.fetchval(
        "SELECT md5(string_agg(id || '@' || last_modified_at::text, ',' ORDER BY id)) FROM samples "
        "WHERE dataset_id = $1 AND filepath LIKE 'old-%'",
        dataset_id,
    )


def says_the_job_holds_it(response: httpx.Response, job_id: str) -> None:
    """A write held back by the job: 409 with Retry-After, a sentence that names the job, and a header with the
    seconds until the job is expected to end (as a map's swap says how long it takes)."""
    assert response.status_code == 409, (response.status_code, response.text)
    assert response.headers.get('retry-after'), dict(response.headers)
    detail = response.json()['detail']
    detail = detail if isinstance(detail, str) else json.dumps(detail)
    assert job_id in detail, detail
    seconds = [
        value
        for name, value in response.headers.items()
        if name.lower().endswith('-seconds') and name.lower() != 'retry-after'
    ]
    assert seconds and all(float(value) > 0 for value in seconds), dict(response.headers)


def test_rule_1_2_a_dataset_of_1000_samples_registering_a_name_another_made_has_them_never_set_in_the_request(
    regions_world,
):
    """8.3's second half, section 9 point 1, a dataset of at most 1,000 samples: another dataset made the list field
    first; this one's 1,000 samples were written before the upgrade, the column holding `[]` in each. Registering the
    field is done inside the request (200, no job): every one of its samples is then never set (comes back `null`,
    `is null` counts them all, `is empty` none), a list written afterwards, an empty one included, is kept, and the
    first dataset's lists are untouched. Main left the old `[]` in them. The field's listing is asked first."""

    async def scenario(r):
        other = await small(r, OTHER, {'kept-empty': [], 'kept': ['rust'], 'kept-unset': UNSET})
        await r.dataset(COLUMN)
        await older_rows(r.database, COLUMN, SMALL)
        assert await held_counts(r.database, COLUMN) == (0, SMALL)
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{COLUMN}/array-fields', json={'field_name': FIELD})
        assert made.status_code == 200, (made.status_code, made.text)
        assert FIELD in await listed(r, COLUMN)
        p = Planted(COLUMN, await ids_of(r, COLUMN), 0)
        assert sum(value is not None for value in (await slim_lists_held(r, p)).values()) == 0
        assert await total(r, COLUMN, leaf(FIELD, 'is_null')) == SMALL
        assert await total(r, COLUMN, leaf(FIELD, 'is_empty')) == 0
        assert (await patch(r, p, 'old-1', {'array_fields': {FIELD: []}})).status_code == 200
        assert (await patch(r, p, 'old-2', {'array_fields': {FIELD: ['mite']}})).status_code == 200
        held = await slim_lists_held(r, p)
        assert (held['old-1'], held['old-2'], held['old-3']) == ([], ['mite'], None)
        assert await lists_held(r, other) == {'kept-empty': [], 'kept': ['rust'], 'kept-unset': None}

    regions_world(scenario)


def test_rule_1_2_a_larger_dataset_registering_a_name_another_made_goes_through_a_job_that_loses_no_list(
    regions_world,
    monkeypatch,
):
    """Section 9 points 1 and 9 on a dataset past 1,000 samples, its samples written before the upgrade (`[]`):
    registering the field answers 202 with a job, and a second request joins the same job. Until the job's last step
    the field is not listed among the dataset's fields; a write that names it (adding a sample, changing one, the bulk
    edit) is answered 409 with Retry-After, a sentence that names the job and the seconds until it is expected to end;
    a sample added without it goes in; a version's restore is refused. The job, run as the runner runs it, walks the
    samples in steps: stopped after a step that changed some of them and taken up again, it finishes. Then the field
    is listed, every sample is never set, a list written afterwards is kept, `last_modified_at` is untouched, and the
    first dataset's lists are as they were. Main registered the field at once, leaving the old `[]`."""

    async def scenario(r):
        pool = r.database.pool
        other = await small(r, OTHER, {'kept-empty': [], 'kept': ['rust']})
        await r.dataset(COLUMN)
        await older_rows(r.database, COLUMN, LARGE)
        p = Planted(COLUMN, await ids_of(r, COLUMN), 0)
        version = await version_now(r, COLUMN, 'before the field')
        before = await modified_at(r.database, COLUMN)
        handlers = await runner_handlers(pool, monkeypatch)
        body = {'field_name': FIELD}
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{COLUMN}/array-fields', json=body, params=WAIT)
        assert made.status_code == 202, (made.status_code, made.text)
        job_id = str(made.json()['job_id'])
        joined = await r.call(ADMIN, 'POST', f'/api/datasets/{COLUMN}/array-fields', json=body, params=WAIT)
        assert joined.status_code == 202 and str(joined.json()['job_id']) == job_id, joined.text

        async def held_back() -> None:
            assert FIELD not in await listed(r, COLUMN)
            assert FIELD not in await schema_names(r, COLUMN)
            sample = {'filepath': 'named.jpg', 'array_fields': {FIELD: ['aphid']}}
            says_the_job_holds_it(await post(r, f'/api/datasets/{COLUMN}/samples', {'samples': [sample]}), job_id)
            says_the_job_holds_it(await patch(r, p, 'old-1', {'array_fields': {FIELD: ['aphid']}}), job_id)
            edit = {'sample_ids': [p.ids['old-2']], 'field_name': FIELD, 'add_tags': ['aphid']}
            says_the_job_holds_it(await post(r, f'/api/datasets/{COLUMN}/samples/bulk-tag', edit), job_id)
            refused = await r.call(ADMIN, 'POST', f'/api/datasets/{COLUMN}/versions/{version}/revert')
            assert refused.status_code == 409, (refused.status_code, refused.text)

        await held_back()
        meanwhile = await post(r, f'/api/datasets/{COLUMN}/samples', {'samples': [{'filepath': 'meanwhile.jpg'}]})
        assert meanwhile.status_code == 200, meanwhile.text
        kind = await r.database.fetchval('SELECT kind FROM jobs WHERE id = $1', made.json()['job_id'])
        job = await claim(pool, kind)
        assert str(job['id']) == job_id
        for _ in range(100):
            job, outcome = await run_step(pool, handlers, job)
            assert not outcome.finished, 'the walk ended in the step that first changed samples: no steps to stop'
            if (await held_counts(r.database, COLUMN))[0] > 1:  # the sample added meanwhile is never set already
                break
        unset, empty = await held_counts(r.database, COLUMN)
        assert unset > 1 and empty > 0, ('a step changes some of the samples, not all', unset, empty)
        await held_back()
        job = await run_to_end(pool, handlers, await stop_and_take_up(pool, job))
        assert FIELD in await listed(r, COLUMN)
        assert await held_counts(r.database, COLUMN) == (LARGE + 1, 0)
        assert await total(r, COLUMN, leaf(FIELD, 'is_null')) == LARGE + 1
        assert await total(r, COLUMN, leaf(FIELD, 'is_empty')) == 0
        assert await modified_at(r.database, COLUMN) == before
        assert await lists_held(r, other) == {'kept-empty': [], 'kept': ['rust']}
        assert (await patch(r, p, 'old-1', {'array_fields': {FIELD: ['aphid']}})).status_code == 200
        assert (await patch(r, p, 'old-2', {'array_fields': {FIELD: []}})).status_code == 200
        asked = {'old-1', 'old-2', 'old-3', 'meanwhile'}
        held = await slim_lists_held(r, Planted(COLUMN, await ids_of(r, COLUMN), 0), names=asked)
        assert (held['old-1'], held['old-2'], held['old-3'], held['meanwhile']) == (['aphid'], [], None, None)

    regions_world(scenario)


def test_rule_1_2_the_sdks_create_array_field_waits_for_the_job(regions_world, monkeypatch):
    """Section 9 point 1: on a dataset past 1,000 samples written before the upgrade, a script's
    `create_array_field` returns once the job has ended (run here as the runner runs it while the script waits): the
    field is then listed and every sample never set. Main queued no job and left the old `[]`."""

    async def scenario(r):
        pool = r.database.pool
        await small(r, OTHER, {'kept': ['rust']})
        await r.dataset(COLUMN)
        await older_rows(r.database, COLUMN, LARGE)
        handlers = await runner_handlers(pool, monkeypatch)
        start = await r.database.fetchval('SELECT clock_timestamp()')
        sdk(r, asyncio.get_running_loop(), monkeypatch)
        script = asyncio.ensure_future(
            asyncio.to_thread(lambda: Dataset(f'dataset-{COLUMN}').create_array_field(FIELD)),
        )
        ran: list[Any] = []
        for _ in range(600):
            if script.done():
                break
            queued = await r.database.fetchrow(
                "SELECT id, kind FROM jobs WHERE state = 'queued' AND created_at >= $1 "
                'AND NOT (kind = ANY($2::text[])) ORDER BY created_at LIMIT 1',
                start,
                [statistics.KIND, field_kinds.KIND],
            )
            if queued is not None:
                ran.append(queued['kind'])
                await run_to_end(pool, handlers, await claim(pool, queued['kind']))
            await asyncio.sleep(0.1)
        await asyncio.wait_for(script, 60)
        assert ran, 'create_array_field returned without a job to wait for'
        assert FIELD in await listed(r, COLUMN)
        assert await held_counts(r.database, COLUMN) == (LARGE, 0)

    regions_world(scenario)


async def mirror_step_by_step(database: Any, link_id: int, mirror: int, *, stop_once: bool = False) -> bool:
    """One sync of the mirror, step by step as the runner runs it. After every step the mirror never has the list
    field registered while one of its rows still holds the old `[]` (section 9 point 9). With `stop_once`, the sync
    is stopped after the first step that left the walk part done, and taken up again. Whether that was seen."""
    step = follower_sync.handler(database.pool)
    job = await follower.sync_job(database, link_id)
    stopped = False
    for _ in range(400):
        job, outcome = await follower.run_step(database, step, job)
        registered = await database.fetchval(
            "SELECT 1 FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2 AND origin = 'native_array'",
            mirror,
            FIELD,
        )
        unset, empty = await held_counts(database, mirror)
        assert not (registered and empty), ('the field registered with the old [] still in rows', unset, empty)
        if outcome.finished:
            return stopped
        if stop_once and not stopped and unset and empty:
            stopped = True
            job = await stop_and_take_up(database.pool, job)
    pytest.fail('the sync did not finish')


def test_rule_1_2_a_mirror_registers_a_list_field_whose_column_was_there_only_after_its_rows_are_never_set(
    monkeypatch,
    registry_database,
):
    """Section 9 points 1 and 9, a mirror: its rows were written before the upgrade (the column, made by another
    dataset, holding `[]` in each). When the primary registers the list field, the mirror's sync runs the same walk
    on its own rows and registers the field only after it: at no step is the field the mirror's while a row holds the
    old `[]`, and at the end every row is never set. Main's mirror registered it at once and kept the `[]`. The first
    copy is asked first."""

    async def scenario(database):
        _, link_id = await follower.setup(monkeypatch, database)
        await datasets.create_array_field(2, datasets.CreateArrayFieldRequest(field_name=FIELD), user=USER)
        samples = [{'filepath': f'm-{n}.jpg', 'metadata': {'plot': n}} for n in range(3)]
        await datasets.add_samples(1, {'samples': samples}, user=USER)
        follower_id = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower_id, 1, followers.FollowOptions(), user=USER)
        await follower.sync(database, link_id)
        mirror = (await follower.mirror_of(database))['dataset_id']
        assert set(await mirrored_lists(database)) == {'m-0', 'm-1', 'm-2'}
        await on_the_mirror(database, mirror, f"UPDATE samples SET {FIELD} = '{{}}' WHERE dataset_id = $1", mirror)
        assert await held_counts(database, mirror) == (0, 3)
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name=FIELD), user=USER)
        await mirror_step_by_step(database, link_id, mirror)
        assert await database.fetchval(
            "SELECT 1 FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2 AND origin = 'native_array'",
            mirror,
            FIELD,
        )
        assert await mirrored_lists(database) == {'m-0': None, 'm-1': None, 'm-2': None}

    registry_database(scenario)


def test_rule_1_2_a_mirrors_walk_stopped_in_between_is_taken_up_again_by_its_sync(monkeypatch, registry_database):
    """Section 9 point 9: a mirror of a dataset past 1,000 samples, its rows written before the upgrade (`[]`). The
    primary registers the list field (through its own job). The mirror's sync walks its rows in steps; stopped after
    a step that left the walk part done, the sync is taken up again and finishes it: the field is never the mirror's
    while a row holds the old `[]`, and at the end it is, with every row never set. Main's mirror registered the field
    at once and kept the `[]`."""

    async def scenario(database):
        pool = database.pool
        _, link_id = await follower.setup(monkeypatch, database)
        handlers = await runner_handlers(pool, monkeypatch)
        # The mirror's walk covers WALK_BLOCKS blocks of ids a step: one block here, so LARGE rows take more than
        # one step and a step can leave the walk part done.
        monkeypatch.setattr(follower_sync, 'WALK_BLOCKS', 1, raising=False)
        await datasets.create_array_field(2, datasets.CreateArrayFieldRequest(field_name=FIELD), user=USER)
        await older_rows(database, 1, LARGE)
        follower_id = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower_id, 1, followers.FollowOptions(), user=USER)
        await follower.sync(database, link_id)
        mirror = (await follower.mirror_of(database))['dataset_id']
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = $1', mirror) == LARGE
        await on_the_mirror(database, mirror, f"UPDATE samples SET {FIELD} = '{{}}' WHERE dataset_id = $1", mirror)
        assert await held_counts(database, mirror) == (0, LARGE)
        made = await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name=FIELD), user=USER)
        assert getattr(made, 'status_code', 200) == 202, ("the primary's registration queued no job", made)
        queued = await database.fetchrow('SELECT id, kind FROM jobs WHERE id = $1', json.loads(made.body)['job_id'])
        await run_to_end(pool, handlers, await claim(pool, queued['kind']))
        assert await mirror_step_by_step(database, link_id, mirror, stop_once=True), 'the walk was never part done'
        assert await held_counts(database, mirror) == (LARGE, 0)
        assert await database.fetchval(
            "SELECT 1 FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2 AND origin = 'native_array'",
            mirror,
            FIELD,
        )

    registry_database(scenario)


# --- Section 9 points 11, 12 and 13: a version's value of a list column has one reading ---

LATE = 'late_marks'  # a list field another dataset made first, which COLUMN registers only after its version
FOREIGN = 'other_marks'  # a list field another dataset made, which COLUMN never registers, at the version or now


async def written_straight(database: Any, p: Planted, lists: dict[str, Any], field: str = FIELD) -> None:
    """Each named row's column written straight to the table (None: NULL): the old default `[]` rows written before
    the upgrade hold, or a list in a column the dataset doesn't register (which no writer of the app reaches)."""
    for name, held in lists.items():
        await database.execute(f'UPDATE samples SET {field} = $2::text[] WHERE id = $1', p.ids[name], held)


async def three_samples(r: Any, dataset_id: int = COLUMN) -> Planted:
    """`one`, `two` and `three` added to the dataset, with a metadata field and no list field of their own."""
    await r.dataset(dataset_id)
    samples = [{'filepath': f'{n}.jpg', 'metadata': {'plot': n}} for n in ('one', 'two', 'three')]
    assert (await post(r, f'/api/datasets/{dataset_id}/samples', {'samples': samples})).is_success
    return Planted(dataset_id, await ids_of(r, dataset_id), 0)


async def no_sample_modified(r: Any, dataset_id: int, version: int, count: int, other: Optional[int] = None) -> None:
    """The diff against the version (or, with `other`, the compare of the version with that one) reports every one of
    the `count` samples unchanged, and its list of modified samples is empty."""
    base = f'/api/datasets/{dataset_id}/versions'
    where = f'{base}/current-diff/{version}' if other is None else f'{base}/diff/{version}/{other}'
    diff = await ok(await r.call(ADMIN, 'GET', where), where)
    assert (diff['added'], diff['removed'], diff['modified'], diff['unchanged']) == (0, 0, 0, count), (where, diff)
    listed_ = await ok(await r.call(ADMIN, 'GET', f'{where}/samples', params={'change_type': 'modified'}), where)
    assert listed_['sample_ids'] == [], (where, 'modified samples', listed_)
    chosen = await ok(await r.call(ADMIN, 'POST', f'{where}/select', params={'change_type': 'modified'}), where)
    assert chosen['total'] == 0, (where, 'selected', chosen)


async def status_names(r: Any, dataset_id: int) -> Optional[int]:
    """The version the status says the dataset is at (None: it has changes)."""
    status = await ok(await r.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/versions/status'))
    current = status.get('current_version')
    assert (current is None) == bool(status['has_changes']), status
    return None if current is None else int(current['id'])


async def as_before_the_settings(r: Any, version: int) -> None:
    """The version as one made before versions recorded the dataset's settings and fields (no dataset snapshot at
    all, the first snapshot format), dated a day earlier than it was made."""
    await r.database.execute(
        'UPDATE dataset_versions SET dataset_snapshot = NULL, snapshot_format = 1, '
        "created_at = created_at - interval '1 day' WHERE id = $1",
        version,
    )


def test_rule_9_11_after_a_restore_the_diff_against_it_is_empty_though_a_column_the_dataset_does_not_register_changed(
    regions_world,
):
    """Section 9 point 11, first case: another dataset registers the list field; COLUMN doesn't, and its rows hold
    lists in that column. A version is taken; then the rows' metadata changes and so do their lists in that column;
    the version is restored. The status then names the version, and the diff against it, its list of modified samples
    and their selection are empty: a column the dataset doesn't register holds nothing of the dataset's, for the
    readers that compare as for the restore. Passes on main, whose restore writes the column back as the version
    recorded it; the restore of this piece leaves it alone, so it holds there only when the readers that compare read
    the version as the restore does. The field's absence from COLUMN is asked first."""

    async def scenario(r):
        other = await small(r, OTHER, {'kept': ['rust']})
        p = await three_samples(r)
        await written_straight(r.database, p, {'one': ['x'], 'two': [], 'three': None})
        version = await version_now(r, COLUMN, 'a column it does not register')
        assert FIELD not in await listed(r, COLUMN)
        await written_straight(r.database, p, {'one': ['y'], 'two': ['z'], 'three': ['w']})
        for name in p.ids:
            assert (await patch(r, p, name, {'metadata': {'plot': f'{name}-changed'}})).status_code == 200
        assert await status_names(r, COLUMN) is None
        await restore(r, COLUMN, version)
        rows = await ok(await post(r, f'/api/datasets/{COLUMN}/samples/query', {'limit': 10}))
        assert {item['metadata']['plot'] for item in rows['items']} == {'one', 'two', 'three'}
        assert FIELD not in await listed(r, COLUMN)
        assert await lists_held(r, other) == {'kept': ['rust']}
        await no_sample_modified(r, COLUMN, version, 3)
        assert await status_names(r, COLUMN) == version

    regions_world(scenario)


def test_rule_9_11_registering_a_name_another_dataset_made_modifies_no_sample_for_the_status_the_diff_or_the_compare(
    regions_world,
):
    """Section 9 point 11, second case: another dataset made the list field; COLUMN's rows hold the old default `[]`
    in its column (as rows written before the upgrade do). Two versions are taken: one as made before versions
    recorded the dataset's settings and fields, and one that recorded them. COLUMN then registers the field: its
    samples are never set (rule 1.2). Nothing of the dataset's samples changed: the status names the older version
    (the newer recorded the fields as they were, without the list field), the diff against each version from before
    reports no sample modified and lists none, and so does the compare of each with a version taken after. Main left
    the old `[]` in the rows. The status and the diff before the registration are asked first, as main answers
    them."""

    async def scenario(r):
        await small(r, OTHER, {'kept': ['rust']})
        p = await three_samples(r)
        await written_straight(r.database, p, {name: [] for name in p.ids})
        old = await version_now(r, COLUMN, 'before the settings were recorded')
        await as_before_the_settings(r, old)
        before = await version_now(r, COLUMN, 'before the field')
        assert await status_names(r, COLUMN) == before
        await no_sample_modified(r, COLUMN, before, 3)
        await make_field(r, COLUMN)
        assert await lists_held(r, p) == {'one': None, 'two': None, 'three': None}
        assert await status_names(r, COLUMN) == old
        for version in (old, before):
            await no_sample_modified(r, COLUMN, version, 3)
        after = await version_now(r, COLUMN, 'after the field')
        for version in (old, before):
            await no_sample_modified(r, COLUMN, version, 3, other=after)
            await no_sample_modified(r, COLUMN, after, 3, other=version)

    regions_world(scenario)


def test_rule_9_13_a_versions_rows_as_browsed_give_a_list_column_as_that_versions_value(regions_world):
    """Section 9 point 13, second bullet: a version of COLUMN, which had `pests` as its own list field and not LATE
    (another dataset made LATE first; COLUMN registers it only after the version) nor FOREIGN (another dataset's,
    never COLUMN's); the rows held the old `[]` or a list in those two columns. Each route that returns the version's
    samples (its samples, whole; its query, whole and with `fields`) gives `pests` as the version held it, LATE as
    `null` for every row (the dataset has the field now and didn't then; a list written since is not the version's),
    and no FOREIGN at all, neither as a key of the row nor in its map of lists. A filter over the version finds the
    rows by LATE's `is null`, none by `is empty` or `is not null`, and its count agrees. Main gave LATE and FOREIGN as
    the version recorded the columns. `pests` is asked first, as main answers it."""

    async def scenario(r):
        await r.dataset(OTHER)
        for name in (LATE, FOREIGN):
            await make_field(r, OTHER, name)
        p = await small(r, COLUMN, {'unset': NULLED, 'empty': [], 'aphid': ['aphid']})
        await written_straight(r.database, p, {'unset': [], 'empty': [], 'aphid': ['x']}, LATE)
        await written_straight(r.database, p, {'unset': ['y'], 'empty': [], 'aphid': []}, FOREIGN)
        version = await version_now(r, COLUMN, f'before {LATE}')
        await make_field(r, COLUMN, LATE)
        assert (await patch(r, p, 'aphid', {'array_fields': {LATE: ['live']}})).status_code == 200
        base = f'/api/datasets/{COLUMN}/versions/{version}'
        samples = await ok(await r.call(ADMIN, 'GET', f'{base}/samples', params={'full': 'true', 'limit': 100}))
        whole = await ok(await post(r, f'{base}/samples/query', {'limit': 100}))
        fields = await ok(await post(r, f'{base}/samples/query', {'limit': 100, 'fields': [FIELD, LATE, FOREIGN]}))
        places = {
            "the version's samples": samples['items'],
            'its query, whole rows': whole['items'],
            'its query with fields': fields['items'],
        }
        rows = {where: {p.names([item['id']]).pop(): item for item in items} for where, items in places.items()}
        want = {'unset': None, 'empty': [], 'aphid': ['aphid']}
        for where, by_name in rows.items():
            assert {name: item.get(FIELD, 'absent') for name, item in by_name.items()} == want, (where, by_name)
        for where, by_name in rows.items():
            assert {name: item.get(LATE, 'absent') for name, item in by_name.items()} == dict.fromkeys(want), (
                where,
                LATE,
                by_name,
            )
            for name, item in by_name.items():
                assert FOREIGN not in item, (where, name, FOREIGN, item)
                assert FOREIGN not in (item.get('array_fields') or {}), (where, name, 'its map of lists', item)
                assert (item.get('array_fields') or {}).get(LATE) is None, (where, name, 'its map of lists', item)
        for node, names in (
            (leaf(LATE, 'is_null'), set(want)),
            (leaf(LATE, 'is_empty'), set()),
            (leaf(LATE, 'is_not_null'), set()),
            (leaf(LATE, 'exists'), set()),
        ):
            found = await ok(await post(r, f'{base}/samples/query', {'limit': 100, 'filters': node}), node)
            assert p.names(item['id'] for item in found['items']) == names, (node, found['items'])
            counted = await ok(
                await post(
                    r,
                    f'{base}/aggregate',
                    {'filters': node, 'aggregations': [{'field': 'plot', 'op': 'count'}]},
                ),
                node,
            )
            assert counted['results'][0]['value'] == len(names), (node, counted)

    regions_world(scenario)


def test_rule_9_13_a_versions_rows_as_browsed_give_the_versions_list_for_a_field_the_dataset_has_dropped_since(
    regions_world,
):
    """Section 9 point 13, second bullet, "also where the dataset has dropped the field since": a version of COLUMN,
    which had `pests` as its own list field; COLUMN then drops the field (its registration taken out of the table,
    the column staying), and its rows' lists change in the table. Each route that returns the version's samples (its
    samples, whole; its query, whole and with `fields`) gives `pests` as the version held it, its map of lists too,
    and a filter over the version reads that list: `is null` finds the row never set, `is empty` the empty list,
    `contains` and `is not null` the rows with a list, and each count agrees. Main gave the version's list in a whole
    row only: its `fields` form and its filters read the field as one the dataset doesn't have. A whole row of the
    version's samples is asked first, as main gives it."""
    want = {'unset': None, 'empty': [], 'aphid': ['aphid']}

    async def scenario(r):
        p = await small(r, COLUMN, {'unset': NULLED, 'empty': [], 'aphid': ['aphid']})
        version = await version_now(r, COLUMN, 'with the field')
        await r.database.execute(
            "DELETE FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2 AND origin = 'native_array'",
            COLUMN,
            FIELD,
        )
        assert FIELD not in await listed(r, COLUMN)
        await written_straight(r.database, p, {'unset': ['live'], 'empty': ['live'], 'aphid': None})
        base = f'/api/datasets/{COLUMN}/versions/{version}'
        samples = await ok(await r.call(ADMIN, 'GET', f'{base}/samples', params={'full': 'true', 'limit': 100}))
        by_name = {p.names([item['id']]).pop(): item for item in samples['items']}
        assert {name: item.get(FIELD, 'absent') for name, item in by_name.items()} == want, by_name
        whole = await ok(await post(r, f'{base}/samples/query', {'limit': 100}))
        fields = await ok(await post(r, f'{base}/samples/query', {'limit': 100, 'fields': [FIELD]}))
        places = {
            "the version's samples": samples['items'],
            'its query, whole rows': whole['items'],
            'its query with fields': fields['items'],
        }
        for where, items in places.items():
            rows = {p.names([item['id']]).pop(): item for item in items}
            assert {name: item.get(FIELD, 'absent') for name, item in rows.items()} == want, (where, rows)
            if where != 'its query with fields':
                for name, item in rows.items():
                    assert (item.get('array_fields') or {}).get(FIELD) == want[name], (where, name, item)
        for node, names in (
            (leaf(FIELD, 'is_null'), {'unset'}),
            (leaf(FIELD, 'is_empty'), {'empty'}),
            (leaf(FIELD, 'contains', 'aphid'), {'aphid'}),
            (leaf(FIELD, 'is_not_null'), {'empty', 'aphid'}),
        ):
            found = await ok(await post(r, f'{base}/samples/query', {'limit': 100, 'filters': node}), node)
            assert p.names(item['id'] for item in found['items']) == names, (node, found['items'])
            counted = await ok(
                await post(
                    r,
                    f'{base}/aggregate',
                    {'filters': node, 'aggregations': [{'field': 'plot', 'op': 'count'}]},
                ),
                node,
            )
            assert counted['results'][0]['value'] == len(names), (node, counted)

    regions_world(scenario)


def test_rule_9_12_a_second_request_joins_the_pending_job_though_the_dataset_was_brought_under_1000_samples(
    regions_world,
    monkeypatch,
):
    """Section 9 point 12, first bullet: a dataset past 1,000 samples written before the upgrade (`[]`) registers a
    name another dataset made: a job (202). Its samples are then deleted until it counts 900 (written straight to the
    table), and a second request to add the field JOINS the same job: nothing is registered until the job ends, so the
    field is not listed and a write naming it is still answered 409 with the job. The job, run as the runner runs it,
    ends; then the field is listed, every sample is never set, and a `[]` written afterwards stays `[]`. Main
    registered the field at once. The other dataset's lists are asked first."""

    async def scenario(r):
        pool = r.database.pool
        other = await small(r, OTHER, {'kept': ['rust']})
        assert await lists_held(r, other) == {'kept': ['rust']}
        await r.dataset(COLUMN)
        await older_rows(r.database, COLUMN, LARGE)
        handlers = await runner_handlers(pool, monkeypatch)
        body = {'field_name': FIELD}
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{COLUMN}/array-fields', json=body, params=WAIT)
        assert made.status_code == 202, (made.status_code, made.text)
        job_id = str(made.json()['job_id'])
        async with test_field_registry_postgres.bulk_setup(r.database) as connection:
            await connection.execute(
                "DELETE FROM samples WHERE dataset_id = $1 AND substring(filepath FROM '^old-([0-9]+)')::int % 10 <> 0",
                COLUMN,
            )
        kept = LARGE // 10
        assert await r.database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = $1', COLUMN) == kept
        assert kept <= SMALL
        joined = await r.call(ADMIN, 'POST', f'/api/datasets/{COLUMN}/array-fields', json=body, params=WAIT)
        assert joined.status_code == 202 and str(joined.json()['job_id']) == job_id, (joined.status_code, joined.text)
        p = Planted(COLUMN, await ids_of(r, COLUMN), 0)
        assert FIELD not in await listed(r, COLUMN)
        says_the_job_holds_it(await patch(r, p, 'old-10', {'array_fields': {FIELD: []}}), job_id)
        sample = {'filepath': 'named.jpg', 'array_fields': {FIELD: []}}
        says_the_job_holds_it(await post(r, f'/api/datasets/{COLUMN}/samples', {'samples': [sample]}), job_id)
        kind = await r.database.fetchval('SELECT kind FROM jobs WHERE id = $1', made.json()['job_id'])
        job = await claim(pool, kind)
        assert str(job['id']) == job_id
        await run_to_end(pool, handlers, job)
        assert FIELD in await listed(r, COLUMN)
        assert await held_counts(r.database, COLUMN) == (kept, 0)
        assert (await patch(r, p, 'old-10', {'array_fields': {FIELD: []}})).status_code == 200
        assert (await patch(r, p, 'old-20', {'array_fields': {FIELD: ['aphid']}})).status_code == 200
        held = await slim_lists_held(r, p, names={'old-10', 'old-20', 'old-30'})
        assert held == {'old-10': [], 'old-20': ['aphid'], 'old-30': None}, held
        assert await total(r, COLUMN, leaf(FIELD, 'is_empty')) == 1
        assert await lists_held(r, other) == {'kept': ['rust']}

    regions_world(scenario)


def test_rule_9_12_a_restore_of_a_version_that_recorded_the_registry_without_the_list_field_leaves_it_never_set(
    regions_world,
):
    """Section 9 point 12, second bullet: the version recorded the field registry and had no list field (COLUMN's
    rows then held the old `[]`, and a list, in a column another dataset made). COLUMN registers the field after the
    version, the registration dated before the version (written so: its date is not always "a list field since"), and
    writes lists. Restored, every one of its rows is never set: the version's registry says the field was not the
    dataset's, whatever the date. Main wrote the version's row values back. The version's registry is asked first."""

    async def scenario(r):
        other = await small(r, OTHER, {'kept': ['rust']})
        p = await three_samples(r)
        await written_straight(r.database, p, {'one': [], 'two': ['x'], 'three': []})
        version = await version_now(r, COLUMN, 'registry without a list field')
        snapshot = await r.database.fetchval('SELECT dataset_snapshot FROM dataset_versions WHERE id = $1', version)
        snapshot = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        assert snapshot.get('field_registry_version') == 1, snapshot
        assert [field for field in snapshot['fields'] if field.get('field_type') == 'list'] == [], snapshot
        await make_field(r, COLUMN)
        await r.database.execute(
            "UPDATE dataset_fields SET created_at = (SELECT created_at - interval '1 day' FROM dataset_versions "
            'WHERE id = $2) WHERE dataset_id = $1 AND field_name = $3',
            COLUMN,
            version,
            FIELD,
        )
        assert (await patch(r, p, 'one', {'array_fields': {FIELD: ['aphid']}})).status_code == 200
        assert (await patch(r, p, 'two', {'array_fields': {FIELD: []}})).status_code == 200
        await restore(r, COLUMN, version)
        assert await lists_held(r, other) == {'kept': ['rust']}
        assert await lists_held(r, p) == {'one': None, 'two': None, 'three': None}

    regions_world(scenario)


def test_rule_9_12_a_registration_that_takes_over_a_row_of_another_origin_is_dated_from_then(regions_world):
    """Section 9 point 12, second bullet, its last clause: COLUMN keeps a row of another origin for the name from
    before the version (origin `system`, holding only a description, as a field that went leaves it); the version is
    one made before versions recorded the field registry, its rows holding the old `[]` in the column another dataset
    made. COLUMN registers the list field after the version, which takes that row over. Restored, its rows are never
    set: the registration counts from when it was made, not from the row's date. Main wrote the old `[]` back. The
    other dataset's lists are asked first."""

    async def scenario(r):
        other = await small(r, OTHER, {'kept': ['rust']})
        p = await three_samples(r)
        await written_straight(r.database, p, {'one': [], 'two': ['x'], 'three': []})
        await r.database.execute(
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, description, created_at) "
            "VALUES ($1, $2, 'unknown', 'system', 'Pests seen on the plot', CURRENT_TIMESTAMP - interval '2 days')",
            COLUMN,
            FIELD,
        )
        version = await version_now(r, COLUMN, 'old format, a row of another origin')
        await as_before_the_registry(r, version)
        # Dated a day earlier than it was made: still after the row of another origin, well before the registration.
        await r.database.execute(
            "UPDATE dataset_versions SET created_at = created_at - interval '1 day' WHERE id = $1",
            version,
        )
        await make_field(r, COLUMN)
        assert (
            await r.database.fetchval(
                'SELECT origin FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
                COLUMN,
                FIELD,
            )
            == 'native_array'
        )
        assert (await patch(r, p, 'one', {'array_fields': {FIELD: ['aphid']}})).status_code == 200
        await restore(r, COLUMN, version)
        assert await lists_held(r, other) == {'kept': ['rust']}
        assert await lists_held(r, p) == {'one': None, 'two': None, 'three': None}

    regions_world(scenario)


async def registered_on(database: Any, dataset_id: int) -> bool:
    return bool(
        await database.fetchval(
            "SELECT 1 FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2 AND origin = 'native_array'",
            dataset_id,
            FIELD,
        ),
    )


async def sync_losing_checkpoints(database: Any, link_id: int, mirror: int) -> int:
    """One sync of the mirror, step by step as the runner runs it. From the first step that changed the mirror's rows
    to the one after which the list field is the mirror's, the sync loses its checkpoint after each step that left
    the mirror in a state not seen before: what the step committed stays, and the sync goes on from no checkpoint, as
    a sync started again does. The number of checkpoints lost."""
    step = follower_sync.handler(database.pool)
    job = await follower.sync_job(database, link_id)
    seen: set[tuple[int, bool]] = set()
    past = False
    for _ in range(400):
        job, outcome = await follower.run_step(database, step, job)
        if outcome.finished:
            return len(seen)
        registered = await registered_on(database, mirror)
        state = ((await held_counts(database, mirror))[0], registered)
        if not past and (state[0] or registered) and state not in seen:
            seen.add(state)
            await database.execute("UPDATE jobs SET checkpoint = '{}'::jsonb WHERE id = $1", job['id'])
            job = {**dict(job), 'checkpoint': {}}
            continue
        past = registered
    pytest.fail('the sync did not finish')


def test_rule_9_12_a_mirror_whose_sync_loses_its_checkpoint_between_its_walk_and_the_registration_ends_as_its_primary(
    monkeypatch,
    registry_database,
):
    """Section 9 point 12, third bullet: a mirror of a dataset past 1,000 samples, its rows written before the upgrade
    (the old `[]` in a column another dataset made). The primary registers the list field (through its job) and then
    writes a real `[]` to one sample and a list to another. The mirror's sync loses its checkpoint after each step from
    the start of its walk to the field's registration, and goes on each time from no checkpoint. It ends with its
    primary's values: the real `[]`, the list, and never set for every other row; the field is the mirror's. Main's
    mirror kept `[]` in every row. The first copy is asked first."""

    async def scenario(database):
        pool = database.pool
        _, link_id = await follower.setup(monkeypatch, database)
        handlers = await runner_handlers(pool, monkeypatch)
        await datasets.create_array_field(2, datasets.CreateArrayFieldRequest(field_name=FIELD), user=USER)
        await older_rows(database, 1, LARGE)
        follower_id = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower_id, 1, followers.FollowOptions(), user=USER)
        await follower.sync(database, link_id)
        mirror = (await follower.mirror_of(database))['dataset_id']
        assert await database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = $1', mirror) == LARGE
        await on_the_mirror(database, mirror, f"UPDATE samples SET {FIELD} = '{{}}' WHERE dataset_id = $1", mirror)
        assert await held_counts(database, mirror) == (0, LARGE)
        made = await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name=FIELD), user=USER)
        assert getattr(made, 'status_code', 200) == 202, ("the primary's registration queued no job", made)
        queued = await database.fetchrow('SELECT id, kind FROM jobs WHERE id = $1', json.loads(made.body)['job_id'])
        await run_to_end(pool, handlers, await claim(pool, queued['kind']))
        primary = {
            row['filepath'][: -len('.jpg')]: row['id']
            for row in await database.fetch(
                "SELECT id, filepath FROM samples WHERE dataset_id = 1 AND filepath IN ('old-1.jpg', 'old-2.jpg')",
            )
        }
        for name, written in (('old-1', []), ('old-2', ['aphid'])):
            update = datasets.SampleUpdate(array_fields={FIELD: written})
            await datasets.update_sample(1, primary[name], update, user=USER)
        lost = await sync_losing_checkpoints(database, link_id, mirror)
        assert lost, 'the sync finished in the step that began its walk: no checkpoint to lose'
        assert await registered_on(database, mirror)
        held = await mirrored_lists(database)
        assert (held['old-1'], held['old-2'], held['old-3']) == ([], ['aphid'], None), (held['old-1'], held['old-2'])
        assert await held_counts(database, mirror) == (LARGE - 2, 1)

    registry_database(scenario)


def test_rule_9_11_sample_save_after_a_save_sends_none_and_the_field_is_never_set(regions_world, monkeypatch):
    """Section 9 point 11, the SDK: a script reads a sample whose field was never set (its column written NULL, so
    main can hold it too), gives it a list and saves it, then sets the field to None and saves the same sample again:
    the server holds never set. The record of what was read never set is brought to what the first save sent. The
    first save's list is asked first, as main stores it."""

    async def scenario(r):
        p = await small(r, COLUMN, {'nulled': NULLED, 'aphid': ['aphid']})
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def first() -> tuple[Any, Any]:
            dataset = Dataset(f'dataset-{COLUMN}')
            dataset.list_array_fields()
            sample = dataset['nulled.jpg']
            read = sample.pests
            sample.pests = ['mite']
            sample.save()
            return sample, read

        def second(sample: Any) -> None:
            sample.pests = None
            sample.save()

        sample, read = await asyncio.to_thread(first)
        assert await lists_held(r, p) == {'nulled': ['mite'], 'aphid': ['aphid']}
        assert read is None, read
        await asyncio.to_thread(second, sample)
        assert await lists_held(r, p) == {'nulled': None, 'aphid': ['aphid']}

    regions_world(scenario)


# A request with `on_duplicate: "update"` whose samples alternate between naming the field, leaving it out and
# sending null: (the sample, what it held before or None for a new one, its entry: a list, LEFT_OUT or None, what it
# holds after).
LEFT_OUT = 'left out'
ALTERNATING: list[tuple[str, Any, Any, Any]] = [
    ('a', ['a'], ['x'], ['x']),
    ('new-1', None, ['n1'], ['n1']),
    ('b', ['b'], LEFT_OUT, ['b']),
    ('new-2', None, LEFT_OUT, None),
    ('c', ['c'], None, None),
    ('new-3', None, None, None),
    ('d', ['d'], [], []),
    ('new-4', None, [], []),
    ('e', [], LEFT_OUT, []),
    ('f', NULLED, None, None),
    ('g', NULLED, ['g'], ['g']),
    ('new-5', None, LEFT_OUT, None),
    ('h', NULLED, LEFT_OUT, None),
    ('i', [], None, None),
]


def test_rule_9_12_samples_added_again_alternating_the_field_left_out_and_null_each_hold_what_their_entry_says(
    regions_world,
):
    """Section 9 point 12, last bullet, with rules 1.5 and 1.7: one request with `on_duplicate: "update"` whose samples
    alternate between naming the field, leaving it out and sending `null`, some there already and some new. Each one
    there already keeps its list where its entry leaves the field out, takes the list its entry names, and is never
    set where its entry sends `null`; a new one holds the list named, or never set. The new ones get ids in the order
    of the request. Main refused the request (422 for the `null`). A sample added again with a list is asked first,
    as main stores it."""

    async def scenario(r):
        before = {name: held for name, held, _, _ in ALTERNATING if held is not None}
        p = await small(r, COLUMN, before)
        listed_first = await add_again(r, p, [{'filepath': 'a.jpg', 'array_fields': {FIELD: ['a', 'again']}}])
        assert listed_first.status_code == 200, listed_first.text
        assert (await lists_held(r, p))['a'] == ['a', 'again']
        samples = []
        for name, _, entry, _ in ALTERNATING:
            sample: dict[str, Any] = {'filepath': f'{name}.jpg', 'metadata': {'plot': name, 'again': True}}
            if entry != LEFT_OUT:
                sample['array_fields'] = {FIELD: entry}
            samples.append(sample)
        again = await add_again(r, p, samples)
        assert again.status_code == 200, again.text
        p = Planted(COLUMN, await ids_of(r, COLUMN), 0)
        assert set(p.ids) == {name for name, _, _, _ in ALTERNATING}, sorted(p.ids)
        assert await lists_held(r, p) == {name: after for name, _, _, after in ALTERNATING}
        new = [name for name, held, _, _ in ALTERNATING if held is None]
        assert sorted(new, key=lambda name: p.ids[name]) == new, {name: p.ids[name] for name in new}

    regions_world(scenario)
