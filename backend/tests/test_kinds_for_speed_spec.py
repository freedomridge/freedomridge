"""The server's record of which kind of row (image, object) has been seen with which field, used only to skip the
other kind's rows (contract g6, kinds for speed), tested where a user or a script meets it: the HTTP API signed in
with real API tokens (the region tests' world: a database of its own per test), the page's wrapped filter on both
grids, and the scripts and background work as the suite drives them. Written from the contract, not from the code.

What is tested, rule by rule:
- 1.7: wherever the API gives a field's `sample_count` (GET and POST field-stats, brief, a facet request and its
  stream) it also gives `on_images` and `on_objects`, which say the kinds seen with the field: after ingest, after
  each write path that puts a field on a row of a kind for the first time (1.3), after the last value of a kind is
  removed and the clearing has run (1.4), on a dataset without objects, and on a dataset not yet examined (1.5).
- 1.2 and 2.3, as a user meets them: after each write path puts the first value of a field on a row of a kind, a
  filter on that field finds the row at once on both grids. Nothing waits or retries between the write and the
  grids being asked. These are pins: the base skips nothing, so they pass there, and must keep passing.
- 1.2 (made exact): a kind whose rows hold a field only as a JSON null has the field: its member is true.
- 1.4 and 5.2: a first value written while the clearing runs is found at once, and still after the clearing ends.
- 1.4 c: a mark no row needs is cleared by a later clearing with no further write, also when the write that took
  the kind's last value raced the clearing before it.
- 1.5: a dataset made before the record (by the last release's schema, then this checkout's migrations) counts every
  field as seen on both kinds, and reads as before. So does it on a database the record's migration has not reached
  (the migrations up to the newest on the base only), where no read fails; and the record's migration, run a second
  time after the upgrade, changes nothing.
- 1.6: `location` and a concept's scores, written to an object for the first time, are found at once by a place and
  a concept filter (pins; the contract leaves to the implementer which of them the record covers).

How the expectations are made. After each write the grids are asked first; only then is the dataset read back (its
rows, kinds, image links, metadata, `@local.` values, locations and concept scores, read from the samples' own
tables, never from the record), and what each grid must show is computed by the reference of the piece before
(test_objects_filter_reading_spec.Reading, written from contract g5 and checked there against the owner's table).
Each write is checked to be a first value: before it, no row of the kind has a value for the field; after it, one
does. Each pin also checks that it can fail: the same reference over the rows with the field taken off the written
kind's rows (what a reading that wrongly skipped the kind would see) shows other rows on each grid that can show the
difference. The members expected are read off the same rows: a kind has been seen with a field when one of its rows
has the key, a JSON null under it included (contract 1.2, made exact: `exists` and `not exists` ask for the key).

What the contract settles, as these tests use it:
- When the clearing runs (contract 1.4, changed 2026-11-25): it is a job of its own, kind `field_kinds`, queued
  when the dataset's statistics are counted and a write that may have taken a kind's last value was noted.
  `let_the_clearing_run` counts the statistics as the suite drives that count
  (test_assistant_conversation_spec.count_statistics), then runs the queued `field_kinds` job to its end as a runner
  slot runs it, using nothing of the implementation but `backend.field_kinds`'s KIND, handler and POLICY. No job
  queued means nothing was noted and there is nothing to run. The module is imported when a clearing is first run,
  so the rest of this file also runs where it isn't there yet.
- A dataset made after the upgrade is examined from the start: its record holds what its writes marked (1.5).
- The examination of a dataset from before the record is done neither inside the migration nor during a request
  (1.5): so a dataset just upgraded, before any background work has run, is a dataset not yet examined, and every
  request asked of it answers as one.

Where the contract leaves a point open, these tests read it this way, and stay away from what it leaves open:
- A mark set for a kind none of whose rows has the field is left open only for fields a row lost a value of during
  the test (it may stay set until the clearing runs); elsewhere a member must be exactly what the rows say.
- A field no row holds any more may stay in field-stats or go; its members are not asserted then.
- The datasets are made and filled through the API (not by inserting rows), so that the record is built by the
  server's own write paths, as a user's dataset is.
"""

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest

from backend import concept_jobs, follower_sync, main
from backend import database as database_module
from backend.crypto import decrypt_secret, encrypt_secret
from backend.database import PLANNING, Database, init_connection
from backend.job_heartbeat import heartbeat_connector
from backend.job_runner import ServerWorker
from backend.routers import datasets, followers, versions
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN, ROLES
from backend.tests.test_assistant_conversation_spec import count_statistics
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_follower_sync import sync
from backend.tests.test_objects_filter_reading_spec import SQUARE, Reading, Row, card, page_filter
from backend.tests.test_regions_spec import ADMIN_USER, Regions, apply_before, nodes, post, selected
from backend.tests.test_upgrade_paths import ROOT
from scripts import copy_objects

database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(
    not (os.getenv('FRIDGE_TEST_POSTGRES_DSN') and os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')),
    reason='Requires disposable PostgreSQL',
)

GRAINS = ('image', 'object')
# The last release before the record: a database made by its schema has datasets the record has never examined.
RELEASE_BEFORE_THE_RECORD = 'v2.13.2'


# --- The planted dataset ---


@dataclass(frozen=True)
class Plant:
    """A row as it is sent: its metadata, its image (by name) for an object, a location, a vector."""

    metadata: dict[str, Any]
    image: Optional[str] = None
    is_object: bool = False
    location: Optional[tuple[float, float]] = None
    vector: Optional[list[float]] = None


def an_image(metadata: dict[str, Any], **more: Any) -> Plant:
    return Plant(metadata, **more)


def an_object(of: str, metadata: dict[str, Any], **more: Any) -> Plant:
    return Plant(metadata, image=of, is_object=True, **more)


# `colour` and `n` only images have, `label` and `hue` only objects, `source` both. car-4's image has no colour and
# car-4 alone has a hue: a write that gives car-4 a colour is the first object value of `colour`, and img-4 has a
# colour from then on only through car-4. img-5 has no objects. img-1 is inside SQUARE, img-2 outside; no object has a
# location. car-4 and truck-3 alone have a vector (`clip`), 0.04 apart: the quality routes write to them only.
ROWS: dict[str, Plant] = {
    'img-1': an_image({'colour': 'red', 'source': 'drone', 'n': 10}, location=(2.5, 2.5)),
    'img-2': an_image({'colour': 'blue', 'n': 20}, location=(32.0, 2.5)),
    'img-3': an_image({'colour': 'red', 'n': 30}),
    'img-4': an_image({'n': 40}),
    'img-5': an_image({'colour': 'red', 'n': 50}),
    'car-1': an_object('img-1', {'label': 'car'}),
    'truck-1': an_object('img-1', {'label': 'truck'}),
    'car-2': an_object('img-2', {'label': 'car', 'source': 'tractor'}),
    'truck-3': an_object('img-3', {'label': 'truck'}, vector=[0.96, 0.28]),
    'car-4': an_object('img-4', {'label': 'car', 'hue': 'green'}, vector=[1.0, 0.0]),
}
PLAIN_ROWS = {name: row for name, row in ROWS.items() if not row.is_object}


def body(name: str, row: Plant, ids: dict[str, int]) -> dict[str, Any]:
    metadata = dict(row.metadata)
    if row.image is not None:
        metadata['parent_id'] = ids[row.image]
    sent: dict[str, Any] = {'filepath': f'{name}.jpg', 'metadata': metadata}
    if row.is_object:
        sent['media_type'] = 'object'
    if row.location is not None:
        sent['location'] = {'type': 'Point', 'coordinates': list(row.location)}
    if row.vector is not None:
        sent['embeddings'] = {'clip': row.vector}
    return sent


@dataclass
class Kinds:
    """A dataset made through the API, the ids of its rows by name, and the dataset the grids are read on (a mirror
    of it, for the mirror's sync)."""

    dataset_id: int
    name: str
    ids: dict[str, int]
    read_id: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)


async def make_dataset(r: Regions, name: str) -> int:
    made = await post(r, '/api/datasets', {'name': name})
    assert made.status_code == 200, made.text
    return made.json()['id']


async def ingest(r: Regions, dataset_id: int, samples: list[dict[str, Any]], **options: Any) -> list[int]:
    added = await post(r, f'/api/datasets/{dataset_id}/samples', {'samples': samples, **options})
    assert added.status_code == 200, added.text
    return added.json()['ids']


async def ids_of(r: Regions, dataset_id: int) -> dict[str, int]:
    rows = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', dataset_id)
    return {row['filepath'][: -len('.jpg')]: row['id'] for row in rows}


async def plant(r: Regions, rows: dict[str, Plant] = ROWS, name: str = 'kinds') -> Kinds:
    """The rows as a dataset made and filled through the API: the images first, then the objects naming them."""
    dataset_id = await make_dataset(r, name)
    ids: dict[str, int] = {}
    for objects in (False, True):
        batch = [body(row_name, row, ids) for row_name, row in rows.items() if row.is_object is objects]
        if batch:
            await ingest(r, dataset_id, batch)
        ids = await ids_of(r, dataset_id)
    assert set(ids) == set(rows), sorted(ids)
    return Kinds(dataset_id, name, ids, read_id=dataset_id)


# --- What the dataset holds, read back from the samples' own tables (never from the record) ---


async def stored(r: Regions, dataset_id: int) -> tuple[dict[str, Row], dict[str, int], dict[int, str]]:
    """The dataset's rows as the reference reads them: metadata (a JSON null kept as None) with each `@local.` value
    beside it, the object's image, the location and the concept scores; the ids by name; the concepts by id."""
    samples = await r.database.fetch(
        'SELECT id, filepath, media_type, parent_id, metadata, local_metadata, lon, lat '
        'FROM samples WHERE dataset_id = $1',
        dataset_id,
    )
    names = {sample['id']: sample['filepath'][: -len('.jpg')] for sample in samples}
    concepts = {
        item['id']: item['name']
        for item in await r.database.fetch('SELECT id, name FROM embedding_concepts WHERE dataset_id = $1', dataset_id)
    }
    scores: dict[str, dict[str, float]] = {}
    for item in await r.database.fetch(
        'SELECT s.sample_id, s.score, c.name FROM embedding_concept_scores s '
        'JOIN embedding_concepts c ON c.id = s.concept_id WHERE c.dataset_id = $1',
        dataset_id,
    ):
        if item['sample_id'] in names:
            scores.setdefault(names[item['sample_id']], {})[item['name']] = item['score']

    def loaded(value: Any) -> dict[str, Any]:
        return json.loads(value) if isinstance(value, str) else dict(value or {})

    rows: dict[str, Row] = {}
    for sample in samples:
        name = names[sample['id']]
        values = {key: value for key, value in loaded(sample['metadata']).items() if key != 'parent_id'}
        values.update({f'@local.{key}': value for key, value in loaded(sample['local_metadata']).items()})
        is_object = sample['media_type'] == 'object'
        rows[name] = Row(
            values,
            image=names.get(sample['parent_id']) if is_object else None,
            is_object=is_object,
            location=None if sample['lon'] is None else (sample['lon'], sample['lat']),
            scores=scores.get(name, {}),
        )
    return rows, {name: sample_id for sample_id, name in names.items()}, concepts


def holders(rows: dict[str, Row], field_name: str, kind: str) -> set[str]:
    """The rows of the kind with a value for the field (a JSON null is no value)."""
    return {
        name
        for name, row in rows.items()
        if row.is_object is (kind == 'object') and row.values.get(field_name) is not None
    }


# --- The grids, asked at once ---


def both(*parts: dict[str, Any]) -> dict[str, Any]:
    return {'logic': 'AND', 'filters': list(parts)}


def label_of(form: Any) -> str:
    return form if isinstance(form, str) else json.dumps(form, sort_keys=True)


async def stages_of(r: Regions, dataset_id: int, forms: list[Any]) -> list[tuple[str, dict[str, Any]]]:
    """Each form as one filter card's stage: query text read by the parse route, or a filter node as it is. Parsed
    before the write, so that nothing but the grids' own requests comes between the write and the grids."""
    stages = []
    for form in forms:
        stage = card(await nodes(r, form, dataset_id)) if isinstance(form, str) else form
        stages.append((label_of(form), stage))
    return stages


async def grids(r: Regions, dataset_id: int, stages: list[tuple[str, dict[str, Any]]]) -> dict[tuple[str, str], set]:
    """What each grid lists for each stage, as the page wraps it (its count agreeing: `selected` checks it)."""
    return {
        (grain, label): await selected(r, page_filter([stage], grain, True), dataset_id)
        for label, stage in stages
        for grain in GRAINS
    }


def skipping(rows: dict[str, Row], field_name: str, kind: str) -> dict[str, Row]:
    """The rows as a reading that skips the kind for the field sees them (contract g6, "Words": its rows are not
    asked, every test of the field counts as not met there): the field taken off the kind's rows. `location` is the
    rows' location, and a concept's name its scores."""
    skipped = {}
    for name, row in rows.items():
        if row.is_object is not (kind == 'object'):
            skipped[name] = row
        elif field_name == 'location':
            skipped[name] = replace(row, location=None)
        elif field_name in row.scores:
            skipped[name] = replace(row, scores={})
        else:
            skipped[name] = replace(row, values={k: v for k, v in row.values.items() if k != field_name})
    return skipped


async def assert_grids_read(
    r: Regions,
    dataset_id: int,
    stages: list[tuple[str, dict[str, Any]]],
    seen: dict[tuple[str, str], set],
    skipped: Optional[tuple[str, str]] = None,
    noticed_on: tuple[str, ...] = GRAINS,
) -> None:
    """What the grids showed is what the contract's reading of the stored rows shows. With `skipped` (a field and a
    kind), the stages are first checked to notice that kind skipped for the field: on each grid of `noticed_on`, some
    stage would show other rows."""
    rows, ids, concepts = await stored(r, dataset_id)
    names = {sample_id: name for name, sample_id in ids.items()}
    reading = Reading(rows, concepts)
    if skipped is not None:
        blind = Reading(skipping(rows, *skipped), concepts)
        for grain in noticed_on:
            assert any(blind.grid(grain, both(stage)) != reading.grid(grain, both(stage)) for _, stage in stages), (
                'on the grid of',
                grain,
                'these filters would not notice',
                skipped,
                'skipped: the test has no teeth there',
            )
    for label, stage in stages:
        for grain in GRAINS:
            want = reading.grid(grain, both(stage))
            got = {names[sample_id] for sample_id in seen[(grain, label)]}
            assert got == want, (grain, label, 'shown', sorted(got), 'expected', sorted(want))


# --- The members ---


async def field_entries(response: httpx.Response) -> list[dict[str, Any]]:
    """A field-stats answer's field entries: its JSON list, or a facet stream's lines (less its closing line)."""
    assert response.status_code == 200, response.text
    try:
        return response.json()
    except ValueError:
        lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        return [line for line in lines if 'name' in line]


def kinds_in(entries: list[dict[str, Any]]) -> dict[str, tuple[bool, bool]]:
    """`on_images` and `on_objects` of each field that has a `sample_count`, checked to be there and booleans."""
    found = {}
    for entry in entries:
        if 'sample_count' not in entry:
            continue
        missing = {'on_images', 'on_objects'} - set(entry)
        assert not missing, (entry['name'], 'has a sample_count but not', sorted(missing), 'its keys:', sorted(entry))
        assert isinstance(entry['on_images'], bool) and isinstance(entry['on_objects'], bool), entry
        found[entry['name']] = (entry['on_images'], entry['on_objects'])
    return found


async def kinds(r: Regions, dataset_id: int) -> dict[str, tuple[bool, bool]]:
    return kinds_in(await field_entries(await r.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/field-stats')))


def seen_in(rows: dict[str, Row]) -> dict[str, tuple[bool, bool]]:
    """Each metadata key's members as the rows say them: True when a row of the kind has the key, a JSON null under
    it included (contract 1.2), False when no row of the kind has it."""
    found: dict[str, list[bool]] = {}
    for row in rows.values():
        for key in row.values:
            found.setdefault(key, [False, False])[1 if row.is_object else 0] = True
    return {key: (pair[0], pair[1]) for key, pair in found.items()}


async def assert_kinds(r: Regions, dataset_id: int, unsettled: frozenset[str] = frozenset()) -> None:
    """The members of every field with a sample_count are what the stored rows say (rule 1.7), the invariant first
    (1.2: a kind with the field is never said to lack it). A kind whose last row with a field in `unsettled` lost it
    during the test may still be marked (1.4: until the clearing runs)."""
    got = await kinds(r, dataset_id)
    rows, _, _ = await stored(r, dataset_id)
    for name, pair in seen_in(rows).items():
        if any(pair):
            assert name in got, (name, 'a row has the field, and field-stats does not list it', sorted(got))
        if name not in got:
            continue
        for side, has, says in zip(('on_images', 'on_objects'), pair, got[name]):
            if has:
                assert says is True, (name, side, 'a row of the kind has the field: the API says the kind lacks it')
            elif name not in unsettled:
                assert says is False, (name, side, 'no row of the kind has the field: the API says it was seen')


# --- Background work, as the suite drives it ---


async def let_the_clearing_run(r: Regions, dataset_id: int) -> None:
    """The clearing of marks run to its end, as contract 1.4 says it runs: the dataset's statistics counted, then the
    `field_kinds` job that the count queued run to its end, as a runner slot runs it. When nothing was noted no job
    is queued, and there is nothing to run."""
    await count_statistics(r, dataset_id)
    field_kinds = importlib.import_module('backend.field_kinds')  # the clearing's job: KIND, handler, POLICY
    worker = ServerWorker(
        r.database.pool,
        {field_kinds.KIND: field_kinds.handler(r.database.pool)},
        heartbeat_connect=heartbeat_connector(r.url),
        policies={field_kinds.KIND: field_kinds.POLICY},
    )
    unfinished = "SELECT count(*) FROM jobs WHERE kind = $1 AND state IN ('queued', 'running')"
    deadline = time.monotonic() + 30
    while await r.database.fetchval(unfinished, field_kinds.KIND):
        assert time.monotonic() < deadline, 'the field_kinds job did not finish within 30 seconds'
        if not await worker.run_once():
            await asyncio.sleep(0.01)  # queued, but not yet to be claimed
    failed = await r.database.fetchval(
        "SELECT count(*) FROM jobs WHERE kind = $1 AND state = 'failed'",
        field_kinds.KIND,
    )
    assert not failed, 'a field_kinds job failed'


async def score_the_concepts(r: Regions, concept_id: int) -> None:
    """The concept's scoring job run to its end, as a runner slot runs it."""
    job = await concept_jobs.request_scoring(r.database.pool, concept_id)
    assert job is not None
    worker = ServerWorker(
        r.database.pool,
        {concept_jobs.KIND: concept_jobs.handler(r.database.pool)},
        heartbeat_connect=heartbeat_connector(r.url),
        policies={concept_jobs.KIND: concept_jobs.POLICY},
    )
    for _ in range(200):
        await worker.run_once()
        state = await r.database.fetchval('SELECT state FROM jobs WHERE id = $1', job['id'])
        if state in ('succeeded', 'failed', 'cancelled'):
            assert state == 'succeeded', state
            return
        await asyncio.sleep(0)
    pytest.fail('the concept scoring job did not finish')


async def run_job(r: Regions, response: httpx.Response) -> None:
    """A request answered with a queued job (202), the job run to its end as the runner runs it."""
    assert response.status_code == 202, response.text
    await finish(r.database)


# --- The writes ---


async def edit(r: Regions, dataset_id: int, sample_id: int, **sent: Any) -> None:
    response = await r.call(ADMIN, 'PATCH', f'/api/datasets/{dataset_id}/samples/{sample_id}', json=sent)
    assert response.status_code == 200, response.text


async def follow(r: Regions, primary_id: int) -> int:
    """A follower of the primary dataset on this same server, and this server's link to it (the follower sync's
    tests' setup, test_follower_sync.setup): its sync reaches the primary's real endpoints in process."""
    r.monkeypatch.setattr(follower_sync, 'require_free_space', AsyncMock())
    # The world's users were inserted with explicit ids: the follower's own account takes the next one.
    await r.database.execute("SELECT setval(pg_get_serial_sequence('users', 'id'), (SELECT max(id) FROM users))")
    created = await followers.create_follower(followers.FollowerCreate(name='lab'), user=ADMIN_USER)
    link_id = await r.database.fetchval(
        "INSERT INTO primary_links (url, token_encrypted, created_by) VALUES ('http://primary/api', $1, $2) "
        'RETURNING id',
        encrypt_secret(created['token']),
        ADMIN,
    )

    def connect(link: Any, instance: Optional[str] = None) -> httpx.AsyncClient:
        headers = {'Authorization': f"Bearer {decrypt_secret(link['token_encrypted'])}"}
        if instance:
            headers['X-Fridge-Instance'] = instance
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url=link['url'], headers=headers)

    r.monkeypatch.setattr(follower_sync, 'connect', connect)
    follower = await r.database.fetchval('SELECT id FROM followers')
    await followers.follow_dataset(follower, primary_id, followers.FollowOptions(), user=ADMIN_USER)
    return link_id


async def the_mirror(r: Regions, primary_id: int) -> int:
    listed = await r.call(ADMIN, 'GET', '/api/datasets')
    assert listed.status_code == 200, listed.text
    mirrors = [item['id'] for item in listed.json() if item.get('mirror') and item['id'] != primary_id]
    assert len(mirrors) == 1, listed.json()
    return mirrors[0]


Step = Callable[[Regions, Kinds], Awaitable[None]]


@dataclass(frozen=True)
class Write:
    """A write path (contract 1.3) putting the first value of `field` on a row of `kind`. `prepare` runs before it
    (it may name the dataset the grids are read on); `act` is the write itself, and may name it too. `noticed_on`:
    the grids on which the filters asked would show other rows if the kind were skipped (an image with no objects is
    on no object's row: only the images' grid can notice it)."""

    name: str
    field: str
    kind: str
    act: Step
    prepare: Optional[Step] = None
    unsettled: frozenset[str] = frozenset()
    noticed_on: tuple[str, ...] = GRAINS
    cleared_in_prepare: bool = False


async def nothing(r: Regions, w: Kinds) -> None:
    return None


# The first object value of `colour`: green, on car-4 (or on a new object of img-4).


async def ingest_a_new_object(r: Regions, w: Kinds) -> None:
    sent = {'label': 'car', 'colour': 'green', 'parent_id': w.ids['img-4']}
    await ingest(r, w.dataset_id, [{'filepath': 'car-4b.jpg', 'media_type': 'object', 'metadata': sent}])


async def ingest_over_an_object(r: Regions, w: Kinds) -> None:
    sent = {'label': 'car', 'hue': 'green', 'colour': 'green', 'parent_id': w.ids['img-4']}
    await ingest(
        r,
        w.dataset_id,
        [{'filepath': 'car-4.jpg', 'media_type': 'object', 'metadata': sent}],
        on_duplicate='update',
    )


async def ingest_an_image_over_as_an_object(r: Regions, w: Kinds) -> None:
    """img-5, sent again as an object of img-4 with a colour: a row changing kind."""
    sent = {'colour': 'green', 'parent_id': w.ids['img-4']}
    await ingest(
        r,
        w.dataset_id,
        [{'filepath': 'img-5.jpg', 'media_type': 'object', 'metadata': sent}],
        on_duplicate='update',
    )


async def patch_an_object(r: Regions, w: Kinds) -> None:
    await edit(r, w.dataset_id, w.ids['car-4'], metadata={'colour': 'green'})


async def bulk_set(r: Regions, w: Kinds) -> None:
    sent = {'sample_ids': [w.ids['car-4']], 'metadata': {'colour': 'green'}}
    response = await post(r, f'/api/datasets/{w.dataset_id}/samples/bulk-set', sent)
    assert response.status_code == 200, response.text


async def bulk_set_per_sample(r: Regions, w: Kinds) -> None:
    sent = {'updates': [{'sample_id': w.ids['car-4'], 'metadata': {'colour': 'green'}}]}
    response = await post(r, f'/api/datasets/{w.dataset_id}/samples/bulk-set', sent)
    assert response.status_code == 200, response.text


async def bulk_set_by_filter(r: Regions, w: Kinds) -> None:
    sent = {'filters': {'field': 'hue', 'operator': '=', 'value': 'green'}, 'metadata': {'colour': 'green'}}
    await run_job(r, await post(r, f'/api/datasets/{w.dataset_id}/samples/bulk-set', sent))


async def bulk_set_per_sample_in_the_background(r: Regions, w: Kinds) -> None:
    sent = {'updates': [{'sample_id': w.ids['car-4'], 'metadata': {'colour': 'green'}}], 'background': True}
    await run_job(r, await post(r, f'/api/datasets/{w.dataset_id}/samples/bulk-set', sent))


def field_operation(operation: str, background: bool) -> Step:
    """`hue` (car-4's alone) renamed or copied onto `colour`, which only images had."""

    async def act(r: Regions, w: Kinds) -> None:
        sent = {'new_name': 'colour', 'overwrite': True, 'background': background}
        response = await post(r, f'/api/datasets/{w.dataset_id}/fields/hue/{operation}', sent)
        if background:
            await run_job(r, response)
        else:
            assert response.status_code == 200, response.text

    return act


async def a_version_with_a_coloured_object(r: Regions, w: Kinds) -> None:
    await edit(r, w.dataset_id, w.ids['car-4'], metadata={'colour': 'green'})
    made = await post(r, f'/api/datasets/{w.dataset_id}/versions', {'name': 'with a coloured car'})
    assert made.status_code == 200, made.text
    w.extra['version'] = made.json()['id']


async def a_colour_taken_off_after_a_version(r: Regions, w: Kinds) -> None:
    """A version in which car-4 has a colour; then the colour taken off and the clearing run: no object has one."""
    await a_version_with_a_coloured_object(r, w)
    await edit(r, w.dataset_id, w.ids['car-4'], remove_metadata_keys=['colour'])
    await let_the_clearing_run(r, w.dataset_id)


async def a_coloured_object_deleted_after_a_version(r: Regions, w: Kinds) -> None:
    await a_version_with_a_coloured_object(r, w)
    deleted = await r.call(ADMIN, 'DELETE', f"/api/datasets/{w.dataset_id}/samples/{w.ids['car-4']}")
    assert deleted.status_code in (200, 204), deleted.text
    await let_the_clearing_run(r, w.dataset_id)


def revert(**params: str) -> Step:
    async def act(r: Regions, w: Kinds) -> None:
        response = await r.call(
            ADMIN,
            'POST',
            f"/api/datasets/{w.dataset_id}/versions/{w.extra['version']}/revert",
            params=params,
        )
        assert response.status_code == 200, response.text

    return act


async def a_crops_dataset(r: Regions, w: Kinds) -> None:
    """A sibling dataset of crops, one naming img-4 as its image, with a colour: what the object copy script reads."""
    crops = await make_dataset(r, 'kinds-crops')
    sent = {'label': 'car', 'colour': 'green', 'parent_id': w.ids['img-4']}
    await ingest(r, crops, [{'filepath': 'crop-4.jpg', 'metadata': sent}])


async def copy_the_crops(r: Regions, w: Kinds) -> None:
    r.monkeypatch.setattr(copy_objects, 'DATABASE_URL', r.url)
    await copy_objects.run(
        argparse.Namespace(src_dataset='kinds-crops', dest_dataset=w.name, field='clip_obj', no_viz=True),
    )


# The first object value of `n`: a quality score, written to car-4 and truck-3 (the rows with a vector).


async def uniqueness(r: Regions, w: Kinds) -> None:
    sent = {'field': 'clip', 'k': 1, 'metadata_field': 'n', 'allow_seq_scan': True}
    response = await post(r, f'/api/datasets/{w.dataset_id}/quality/uniqueness/compute', sent)
    assert response.status_code == 200, response.text


async def a_centroid(r: Regions, w: Kinds) -> None:
    response = await post(r, f'/api/datasets/{w.dataset_id}/quality/representativeness/centroid', {'field': 'clip'})
    assert response.status_code == 200, response.text
    w.extra['centroid'] = response.json()['centroid']


async def representativeness(r: Regions, w: Kinds) -> None:
    sent = {'field': 'clip', 'centroid': w.extra['centroid'], 'metadata_field': 'n'}
    response = await post(r, f'/api/datasets/{w.dataset_id}/quality/representativeness/compute', sent)
    assert response.status_code == 200, response.text


async def duplicate_pairs(r: Regions, w: Kinds) -> None:
    sent = {'field': 'clip', 'thresh': 0.05, 'k': 1, 'token': 'kinds', 'allow_seq_scan': True}
    response = await post(r, f'/api/datasets/{w.dataset_id}/quality/duplicates/compute', sent)
    assert response.status_code == 200, response.text
    assert response.json()['pairs_written'] >= 1, response.text


async def duplicate_groups(r: Regions, w: Kinds) -> None:
    """The groups written under `n`: finalize takes `n` off every row first, so the images lose theirs."""
    sent = {'token': 'kinds', 'group_field': 'n'}
    response = await post(r, f'/api/datasets/{w.dataset_id}/quality/duplicates/finalize', sent)
    assert response.status_code == 200, response.text


# A mirror's sync: the first copy (every row new on the mirror), and the feed of a change made on the primary.


async def a_follower_with_a_coloured_object(r: Regions, w: Kinds) -> None:
    await edit(r, w.dataset_id, w.ids['car-4'], metadata={'colour': 'green'})
    w.extra['link'] = await follow(r, w.dataset_id)
    w.read_id = None  # the mirror is made by the write


async def the_first_sync(r: Regions, w: Kinds) -> None:
    await sync(r.database, w.extra['link'])
    w.read_id = await the_mirror(r, w.dataset_id)


async def a_mirror_and_a_colour_on_the_primary(r: Regions, w: Kinds) -> None:
    w.extra['link'] = await follow(r, w.dataset_id)
    await sync(r.database, w.extra['link'])
    w.read_id = await the_mirror(r, w.dataset_id)
    await edit(r, w.dataset_id, w.ids['car-4'], metadata={'colour': 'green'})


async def the_next_sync(r: Regions, w: Kinds) -> None:
    await sync(r.database, w.extra['link'])


async def a_mirror(r: Regions, w: Kinds) -> None:
    w.extra['link'] = await follow(r, w.dataset_id)
    await sync(r.database, w.extra['link'])
    w.read_id = await the_mirror(r, w.dataset_id)
    w.extra['mirror_ids'] = await ids_of(r, w.read_id)


async def a_mirror_with_a_local_note_on_an_image(r: Regions, w: Kinds) -> None:
    await a_mirror(r, w)
    assert w.read_id is not None
    await edit(r, w.read_id, w.extra['mirror_ids']['img-1'], metadata={'@local.note': 'seen'})


def local_note(row: str, note: str) -> Step:
    """A mirror's own field (`@local.note`), written to one of its rows."""

    async def act(r: Regions, w: Kinds) -> None:
        assert w.read_id is not None
        await edit(r, w.read_id, w.extra['mirror_ids'][row], metadata={'@local.note': note})

    return act


# The first image value of `label`: tree, on img-2 (or on a new image).


async def ingest_a_new_image(r: Regions, w: Kinds) -> None:
    await ingest(r, w.dataset_id, [{'filepath': 'img-6.jpg', 'metadata': {'colour': 'red', 'label': 'tree'}}])


async def ingest_an_object_over_as_an_image(r: Regions, w: Kinds) -> None:
    """truck-1, sent again as an image with a label: a row changing kind."""
    await ingest(
        r,
        w.dataset_id,
        [{'filepath': 'truck-1.jpg', 'media_type': 'image', 'metadata': {'label': 'tree'}}],
        on_duplicate='update',
    )


async def patch_an_image(r: Regions, w: Kinds) -> None:
    await edit(r, w.dataset_id, w.ids['img-2'], metadata={'label': 'tree'})


async def bulk_set_an_image(r: Regions, w: Kinds) -> None:
    sent = {'sample_ids': [w.ids['img-2']], 'metadata': {'label': 'tree'}}
    response = await post(r, f'/api/datasets/{w.dataset_id}/samples/bulk-set', sent)
    assert response.status_code == 200, response.text


WRITES: list[Write] = [
    Write('ingest-a-new-object', 'colour', 'object', ingest_a_new_object),
    Write('ingest-update-of-an-object', 'colour', 'object', ingest_over_an_object),
    Write('ingest-update-turning-an-image-into-an-object', 'colour', 'object', ingest_an_image_over_as_an_object),
    Write('sample-patch', 'colour', 'object', patch_an_object),
    Write('bulk-set', 'colour', 'object', bulk_set),
    Write('bulk-set-per-sample', 'colour', 'object', bulk_set_per_sample),
    Write('bulk-set-by-filter-job', 'colour', 'object', bulk_set_by_filter),
    Write('bulk-set-per-sample-job', 'colour', 'object', bulk_set_per_sample_in_the_background),
    Write('field-rename', 'colour', 'object', field_operation('rename', False), unsettled=frozenset({'hue'})),
    Write('field-rename-job', 'colour', 'object', field_operation('rename', True), unsettled=frozenset({'hue'})),
    Write('field-copy', 'colour', 'object', field_operation('clone', False)),
    Write('field-copy-job', 'colour', 'object', field_operation('clone', True)),
    Write(
        'version-restore',
        'colour',
        'object',
        revert(),
        prepare=a_colour_taken_off_after_a_version,
        cleared_in_prepare=True,
    ),
    Write(
        'version-restore-of-a-deleted-object',
        'colour',
        'object',
        revert(restore_deleted='true'),
        prepare=a_coloured_object_deleted_after_a_version,
        cleared_in_prepare=True,
    ),
    Write('object-copy-script', 'colour', 'object', copy_the_crops, prepare=a_crops_dataset),
    Write('quality-uniqueness', 'n', 'object', uniqueness),
    Write('quality-representativeness', 'n', 'object', representativeness, prepare=a_centroid),
    Write(
        'quality-duplicate-groups',
        'n',
        'object',
        duplicate_groups,
        prepare=duplicate_pairs,
        unsettled=frozenset({'n'}),
    ),
    Write('mirror-first-sync', 'colour', 'object', the_first_sync, prepare=a_follower_with_a_coloured_object),
    Write('mirror-sync-of-a-change', 'colour', 'object', the_next_sync, prepare=a_mirror_and_a_colour_on_the_primary),
    Write('mirror-own-field-on-an-image', '@local.note', 'image', local_note('img-1', 'seen'), prepare=a_mirror),
    Write(
        'mirror-own-field-on-an-object',
        '@local.note',
        'object',
        local_note('car-4', 'checked'),
        prepare=a_mirror_with_a_local_note_on_an_image,
    ),
    Write('ingest-a-new-image', 'label', 'image', ingest_a_new_image, noticed_on=('image',)),
    Write(
        'ingest-update-turning-an-object-into-an-image',
        'label',
        'image',
        ingest_an_object_over_as_an_image,
        noticed_on=('image',),
    ),
    Write('sample-patch-of-an-image', 'label', 'image', patch_an_image),
    Write('bulk-set-of-an-image', 'label', 'image', bulk_set_an_image),
]
WRITE_IDS = [write.name for write in WRITES]


def note(operator: str, value: Any = None) -> dict[str, Any]:
    """A filter node on the mirror's own field, sent as a node: before the first value the field is unknown to the
    parse route."""
    return {'field': '@local.note', 'operator': operator, 'value': value}


# The filters asked after a first value of each field. Had the new value's kind been skipped, they would show other
# rows (each pin checks it, on each grid that can): `=` and `exists` would lose the row and its partner, `is null`
# would keep them, a NOT would keep a partner that has the field through its other rows.
FORMS: dict[str, list[Any]] = {
    'colour': [
        'colour = "green"',
        'colour != "green"',
        'colour is null',
        'colour exists',
        'colour = "green" AND label = "car"',
        'NOT (colour = "green" AND label = "car")',
    ],
    'n': ['n < 5', 'n > 5', 'NOT (n < 5)', 'n is null', 'n exists'],
    'label': [
        'label = "tree"',
        'label != "tree"',
        'label is null',
        'label exists',
        'colour = "red" AND label = "tree"',
    ],
    '@local.note': [note('=', 'checked'), note('=', 'seen'), note('!=', 'seen'), note('is_null'), note('exists')],
}


async def prepared(r: Regions, write: Write) -> Kinds:
    w = await plant(r)
    await (write.prepare or nothing)(r, w)
    return w


# --- 1.7: the two members ---


def test_1_7_every_form_of_field_stats_gives_on_images_and_on_objects_beside_sample_count(regions_world):
    """GET and POST field-stats, brief, a facet request and its stream: each field with a sample_count says which
    kinds have been seen with it, the same in every form, as the rows say right after they were ingested."""

    async def scenario(r):
        w = await plant(r)
        where = f'/api/datasets/{w.dataset_id}/field-stats'
        names = ['colour', 'n', 'source', 'label', 'hue']
        forms = {
            'GET': await r.call(ADMIN, 'GET', where),
            'GET brief': await r.call(ADMIN, 'GET', where, params={'brief': 'true'}),
            'POST': await post(r, where, {}),
            'POST facet request': await post(r, where, {'fields': names}),
            'POST facet stream': await post(r, where, {'fields': names, 'stream': True}),
        }
        answers = {form: kinds_in(await field_entries(response)) for form, response in forms.items()}
        expected = {
            'colour': (True, False),
            'n': (True, False),
            'source': (True, True),
            'label': (False, True),
            'hue': (False, True),
        }
        for form, found in answers.items():
            assert {name: found.get(name) for name in names} == expected, form
        assert all(found == answers['GET'] for form, found in answers.items() if not form.startswith('POST facet'))
        await assert_kinds(r, w.dataset_id)

    regions_world(scenario)


def test_1_7_a_dataset_without_objects_has_every_field_on_images_only(regions_world):
    async def scenario(r):
        w = await plant(r, PLAIN_ROWS, 'plain')
        found = await kinds(r, w.dataset_id)
        assert {name: found.get(name) for name in ('colour', 'n', 'source')} == {
            'colour': (True, False),
            'n': (True, False),
            'source': (True, False),
        }
        await assert_kinds(r, w.dataset_id)

    regions_world(scenario)


@pytest.mark.parametrize('write', WRITES, ids=WRITE_IDS)
def test_1_7_the_members_after_each_write_path_name_the_kind_it_wrote(regions_world, write):
    """Rules 1.3 and 1.7: before a write puts the first value of a field on a row of a kind, the field's member for
    that kind is false (or the field isn't listed yet); right after it, true; every other member stays what the rows
    say. The member before is not asked where the write's preparation took the kind's last value off and relies on
    the clearing (the version restores): that is the clearing's test, 1.4."""

    async def scenario(r):
        w = await prepared(r, write)
        side = 1 if write.kind == 'object' else 0
        if w.read_id is not None and not write.cleared_in_prepare:
            before = (await kinds(r, w.read_id)).get(write.field, (False, False))
            assert before[side] is False, (write.name, 'marked before the write', before)
        await write.act(r, w)
        assert w.read_id is not None
        rows, _, _ = await stored(r, w.read_id)
        assert holders(rows, write.field, write.kind), (write.name, 'wrote no value')
        found = await kinds(r, w.read_id)
        assert found.get(write.field, (False, False))[side] is True, (write.name, found.get(write.field))
        await assert_kinds(r, w.read_id, write.unsettled)

    regions_world(scenario)


# --- 1.2 and 2.3: a first value is found at once, on both grids, for each write path (pins) ---


@pytest.mark.parametrize('write', WRITES, ids=WRITE_IDS)
def test_2_3_pin_a_first_value_written_by_each_write_path_is_found_at_once_on_both_grids(regions_world, write):
    async def scenario(r):
        w = await prepared(r, write)
        stages = await stages_of(r, w.read_id or w.dataset_id, FORMS[write.field])
        if w.read_id is not None:
            rows, _, _ = await stored(r, w.read_id)
            assert not holders(rows, write.field, write.kind), (write.name, 'the kind had the field before')
        await write.act(r, w)
        assert w.read_id is not None
        seen = await grids(r, w.read_id, stages)  # at once: nothing between the write and the grids
        rows, _, _ = await stored(r, w.read_id)
        assert holders(rows, write.field, write.kind), (write.name, 'wrote no value')
        await assert_grids_read(r, w.read_id, stages, seen, (write.field, write.kind), write.noticed_on)

    regions_world(scenario)


def test_2_2_pin_a_json_null_as_the_first_object_key_is_found_by_exists_at_once(regions_world):
    """A JSON null under the key is the field (contract 1.2, made exact): `exists` asks for the key. car-4 gets
    `colour` as a JSON null: no object has a colour value, and `colour exists` must still find car-4 at once (no
    answer changes, 2.2). Its member is the next test's."""

    async def scenario(r):
        w = await plant(r)
        stages = await stages_of(
            r,
            w.dataset_id,
            ['colour exists', 'colour not exists', 'colour is null', 'NOT (colour exists)'],
        )
        await edit(r, w.dataset_id, w.ids['car-4'], metadata={'colour': None})
        seen = await grids(r, w.dataset_id, stages)
        rows, _, _ = await stored(r, w.dataset_id)
        assert 'colour' in rows['car-4'].values and rows['car-4'].values['colour'] is None
        assert w.ids['car-4'] in seen[('object', 'colour exists')]
        assert w.ids['img-4'] in seen[('image', 'colour exists')]
        await assert_grids_read(r, w.dataset_id, stages, seen, ('colour', 'object'))

    regions_world(scenario)


def test_1_2_a_kind_whose_rows_hold_a_field_only_as_a_json_null_is_seen_with_it(regions_world):
    """Rule 1.2, made exact, with 1.7: car-4's `colour`, written as a JSON null, is the first object to have the key.
    From that write on, `colour`'s on_objects is true, though no object has a value for it."""

    async def scenario(r):
        w = await plant(r)
        assert (await kinds(r, w.dataset_id))['colour'] == (True, False)
        await edit(r, w.dataset_id, w.ids['car-4'], metadata={'colour': None})
        rows, _, _ = await stored(r, w.dataset_id)
        assert rows['car-4'].values == {'label': 'car', 'hue': 'green', 'colour': None}
        assert not holders(rows, 'colour', 'object')
        assert (await kinds(r, w.dataset_id))['colour'] == (True, True)
        await assert_kinds(r, w.dataset_id)

    regions_world(scenario)


PLACE = {'field': 'location', 'operator': 'geo_polygon', 'value': SQUARE}
PLACE_FORMS: list[Any] = [
    PLACE,
    {'field': 'location', 'operator': 'geo_polygon_exclude', 'value': SQUARE},
    {'logic': 'NOT', 'filters': [PLACE]},
]


async def locate_an_object(r: Regions, w: Kinds) -> None:
    await edit(r, w.dataset_id, w.ids['car-4'], location={'type': 'Point', 'coordinates': [1.0, 1.0]})


async def ingest_a_located_object(r: Regions, w: Kinds) -> None:
    sent = {
        'filepath': 'car-4b.jpg',
        'media_type': 'object',
        'metadata': {'label': 'car', 'parent_id': w.ids['img-4']},
        'location': {'type': 'Point', 'coordinates': [1.0, 1.0]},
    }
    await ingest(r, w.dataset_id, [sent])


@pytest.mark.parametrize('act', [locate_an_object, ingest_a_located_object], ids=['sample-patch', 'ingest'])
def test_1_6_pin_a_first_location_on_an_object_is_found_at_once_by_a_place_on_both_grids(regions_world, act):
    """No object has a location until car-4 (or a new object of img-4) gets one inside SQUARE: img-4 is inside
    through it at once, and its exclusion leaves img-4 out at once."""

    async def scenario(r):
        w = await plant(r)
        stages = await stages_of(r, w.dataset_id, PLACE_FORMS)
        rows, _, _ = await stored(r, w.dataset_id)
        assert not any(row.location for row in rows.values() if row.is_object)
        await act(r, w)
        seen = await grids(r, w.dataset_id, stages)
        assert w.ids['img-4'] in seen[('image', label_of(PLACE))]
        await assert_grids_read(r, w.dataset_id, stages, seen, ('location', 'object'))

    regions_world(scenario)


def test_1_6_pin_a_concepts_first_scores_on_objects_are_found_at_once_on_both_grids(regions_world):
    """A concept on `clip`, which only objects have, scored by its job: the first scores any row has, all on
    objects. Its show and its exclude find them at once on both grids."""

    async def scenario(r):
        w = await plant(r)
        concept = await r.database.fetchval(
            'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
            "VALUES ($1, 'thing', 'clip', 'seed', '[1,0]', 0.5) RETURNING id",
            w.dataset_id,
        )
        stages = await stages_of(r, w.dataset_id, ['concept "thing" > 0.5', 'not concept "thing" > 0.5'])
        await score_the_concepts(r, concept)
        seen = await grids(r, w.dataset_id, stages)
        rows, _, _ = await stored(r, w.dataset_id)
        assert {name for name, row in rows.items() if row.scores} == {'car-4', 'truck-3'}
        assert w.ids['img-4'] in seen[('image', 'concept "thing" > 0.5')]
        await assert_grids_read(r, w.dataset_id, stages, seen, ('thing', 'object'))

    regions_world(scenario)


# --- 1.4: a mark is cleared once no row of the kind has the field ---


def test_1_4_a_kind_is_cleared_once_no_row_of_it_has_the_field_and_the_clearing_has_run(regions_world):
    """car-2's `source` is the objects' last: taken off, and the clearing run, `source` is on images only. The
    images' mark of `source` (img-1 still has one) stays."""

    async def scenario(r):
        w = await plant(r)
        assert (await kinds(r, w.dataset_id))['source'] == (True, True)
        await edit(r, w.dataset_id, w.ids['car-2'], remove_metadata_keys=['source'])
        await let_the_clearing_run(r, w.dataset_id)
        assert (await kinds(r, w.dataset_id))['source'] == (True, False)
        await assert_kinds(r, w.dataset_id)
        # And written again: marked at once.
        await edit(r, w.dataset_id, w.ids['truck-3'], metadata={'source': 'rover'})
        assert (await kinds(r, w.dataset_id))['source'] == (True, True)

    regions_world(scenario)


SOURCE_FORMS = ['source = "rover"', 'source != "rover"', 'source is null', 'source exists']


def test_2_3_pin_a_first_value_written_after_the_clearing_is_found_at_once_on_both_grids(regions_world):
    async def scenario(r):
        w = await plant(r)
        stages = await stages_of(r, w.dataset_id, SOURCE_FORMS)
        await edit(r, w.dataset_id, w.ids['car-2'], remove_metadata_keys=['source'])
        await let_the_clearing_run(r, w.dataset_id)
        # Read right after the clearing: nothing of the objects is lost or kept by it.
        await assert_grids_read(r, w.dataset_id, stages, await grids(r, w.dataset_id, stages))
        await edit(r, w.dataset_id, w.ids['truck-3'], metadata={'source': 'rover'})
        seen = await grids(r, w.dataset_id, stages)
        assert w.ids['truck-3'] in seen[('object', 'source = "rover"')]
        assert w.ids['img-3'] in seen[('image', 'source = "rover"')]
        await assert_grids_read(r, w.dataset_id, stages, seen, ('source', 'object'))

    regions_world(scenario)


# --- 5.2: a first value racing the clearing ---

RACE_ROUNDS = 12
RACE_STEP = 0.01  # seconds between the rounds' starts of the write: a clearing here takes about a tenth of a second
RACERS = ['car-1', 'truck-1', 'truck-3', 'car-4']  # objects; none of them has a `source`


async def race(r: Regions, w: Kinds, stages: list[tuple[str, dict[str, Any]]], check_round: Callable) -> None:
    """Rounds of: no object has a `source` and the clearing has run; then one object gets one while the clearing
    runs again, the write started at a different moment of the clearing each round. `check_round` is given the
    racer, what the grids showed right after the write (the clearing possibly still running), and is called again
    once both have ended."""
    holder = 'car-2'
    for round_ in range(RACE_ROUNDS):
        await edit(r, w.dataset_id, w.ids[holder], remove_metadata_keys=['source'])
        await let_the_clearing_run(r, w.dataset_id)
        racer = RACERS[round_ % len(RACERS)]

        async def write(racer: str = racer, delay: float = RACE_STEP * round_) -> dict[tuple[str, str], set]:
            await asyncio.sleep(delay)
            await edit(r, w.dataset_id, w.ids[racer], metadata={'source': 'rover'})
            return await grids(r, w.dataset_id, stages)

        seen, _ = await asyncio.gather(write(), let_the_clearing_run(r, w.dataset_id))
        await check_round(racer, seen)
        await check_round(racer, None)
        holder = racer


def test_5_2_pin_a_first_value_racing_the_clearing_is_found_at_once_and_after(regions_world):
    async def scenario(r):
        w = await plant(r)
        stages = await stages_of(r, w.dataset_id, ['source = "rover"', 'source is null'])

        async def check_round(racer: str, seen: Optional[dict]) -> None:
            seen = seen or await grids(r, w.dataset_id, stages)
            image = {'car-1': 'img-1', 'truck-1': 'img-1', 'truck-3': 'img-3', 'car-4': 'img-4'}[racer]
            assert w.ids[racer] in seen[('object', 'source = "rover"')], racer
            assert w.ids[image] in seen[('image', 'source = "rover"')], racer
            assert w.ids[racer] not in seen[('object', 'source is null')], racer
            await assert_grids_read(r, w.dataset_id, stages, seen, ('source', 'object'))

        await race(r, w, stages, check_round)

    regions_world(scenario)


def test_5_2_the_mark_of_a_first_value_racing_the_clearing_is_never_lost(regions_world):
    async def scenario(r):
        w = await plant(r)

        async def check_round(racer: str, seen: Optional[dict]) -> None:
            assert (await kinds(r, w.dataset_id))['source'][1] is True, racer

        await race(r, w, [], check_round)

    regions_world(scenario)


def test_1_4_c_a_mark_no_row_needs_is_cleared_by_a_later_clearing_without_any_further_write(regions_world):
    """Rule 1.4 c: a noted write is forgotten only by a clearing whose checks all began after it committed. Each
    round, the objects' last `source` is taken off while a clearing runs, the removal started at a different moment
    of it; then one more clearing, with no write since the removal, leaves `source` on images only."""

    async def scenario(r):
        w = await plant(r)
        for round_ in range(RACE_ROUNDS):
            holder = 'car-2' if round_ == 0 else RACERS[round_ % len(RACERS)]
            if round_:
                await edit(r, w.dataset_id, w.ids[holder], metadata={'source': 'rover'})
                await let_the_clearing_run(r, w.dataset_id)
                assert (await kinds(r, w.dataset_id))['source'] == (True, True), (round_, holder)

            async def remove(holder: str = holder, delay: float = RACE_STEP * round_) -> None:
                await asyncio.sleep(delay)
                await edit(r, w.dataset_id, w.ids[holder], remove_metadata_keys=['source'])

            await asyncio.gather(remove(), let_the_clearing_run(r, w.dataset_id))
            await let_the_clearing_run(r, w.dataset_id)  # the later clearing: no write since the removal
            assert (await kinds(r, w.dataset_id))['source'] == (True, False), (round_, holder)

    regions_world(scenario)


# --- 1.5: a dataset not yet examined ---

# A dataset as the last release before the record left it: images with `colour`, objects with `label`, `source` on
# both, registered and counted as that release registered them.
BEFORE_THE_RECORD = """
INSERT INTO datasets (id, name) VALUES (7, 'from-before-the-record');
INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES
    (101, 7, 'img-1.jpg', 'image', '{"colour": "red", "source": "drone"}'),
    (102, 7, 'img-2.jpg', 'image', '{"colour": "blue"}'),
    (103, 7, 'img-3.jpg', 'image', '{}'),
    (104, 7, 'car-1.jpg', 'object', '{"label": "car", "parent_id": 101}'),
    (105, 7, 'truck-2.jpg', 'object', '{"label": "truck", "source": "tractor", "parent_id": 102}'),
    (106, 7, 'car-3.jpg', 'object', '{"label": "car", "parent_id": 103}');
SELECT setval(pg_get_serial_sequence('samples', 'id'), 200);
SELECT setval(pg_get_serial_sequence('datasets', 'id'), 7);
INSERT INTO dataset_fields (dataset_id, field_name, field_type) VALUES
    (7, 'colour', 'string'), (7, 'source', 'string'), (7, 'label', 'string'), (7, 'parent_id', 'number');
INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, sample_count) VALUES
    (7, 'colour', 'string', 2), (7, 'source', 'string', 2), (7, 'label', 'string', 3), (7, 'parent_id', 'number', 3);
"""
OLD = 7


# The newest migration on the branch these tests were written against: a later one is the record's (1.5).
LAST_ON_THE_BASE = '0043'
MIGRATIONS = ROOT / 'backend/db/migrations'


def the_records_migrations() -> list[Path]:
    """The migration files after LAST_ON_THE_BASE, in the runner's (byte) order."""
    found = sorted(
        (path for path in MIGRATIONS.glob('*.sql') if path.name[:4] > LAST_ON_THE_BASE),
        key=lambda path: path.name.encode(),
    )
    assert found, f'no migration after {LAST_ON_THE_BASE} in backend/db/migrations: the record\'s migration is not here'
    return found


def from_before_the_record(database: Any, upgrade: bool) -> str:
    """A database made by the last release's schema with BEFORE_THE_RECORD in it. Then, with `upgrade`, this
    checkout's whole upgrade (scripts/apply_migrations.sh); without it, only the migrations up to LAST_ON_THE_BASE,
    one by one as the runner applies them: the record's migration has not run (1.5, as when the server is started
    before scripts/apply_migrations.sh). Its name."""
    make, psql, _, migrate = database
    old_schema = subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{RELEASE_BEFORE_THE_RECORD}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = make(old_schema + BEFORE_THE_RECORD)
    if upgrade:
        upgraded = migrate(name)
        assert upgraded.returncode == 0, f'apply_migrations.sh failed\n{upgraded.stdout}\n{upgraded.stderr}'
    else:
        apply_before(psql, name, f'{int(LAST_ON_THE_BASE) + 1:04d}')
    return name


def serve(name: str, monkeypatch: Any, tmp_path: Path, scenario: Callable[[Regions], Awaitable[None]]) -> None:
    """The region tests' world (test_regions_spec.regions_world) on the database `name`; no background work runs."""
    url = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()
    monkeypatch.setattr(database_module, 'DATABASE_URL', url)
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.setattr(datasets, '_array_columns_cache', None)
    monkeypatch.setattr(datasets, 'require_free_space', AsyncMock())
    monkeypatch.setattr(datasets, 'validate_embedding_dims', AsyncMock())
    monkeypatch.setattr(versions, 'require_free_space', AsyncMock())
    monkeypatch.setattr(versions, 'estimate_snapshot_bytes', AsyncMock(return_value=0))

    async def body() -> None:
        pool = await asyncpg.create_pool(
            url,
            min_size=1,
            max_size=8,
            init=init_connection,
            server_settings={'statement_timeout': '30000', **PLANNING},
        )
        monkeypatch.setattr(database_module.db, 'pool', pool)
        database_ = Database()
        database_.pool = pool
        try:
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                r = Regions(database_, client, url, tmp_path, monkeypatch)
                for user_id, role in ROLES.items():
                    await r.user(user_id, role)
                await scenario(r)
        finally:
            await pool.close()

    asyncio.run(body())


@pytest.fixture
def upgraded_world(database, monkeypatch, tmp_path):
    """The region tests' world on a database made by the last release's schema with BEFORE_THE_RECORD in it, then
    upgraded by this checkout's migrations; no background work runs."""

    def run(scenario):
        serve(from_before_the_record(database, upgrade=True), monkeypatch, tmp_path, scenario)

    return run


OLD_FORMS = [
    'colour = "red" AND label = "car"',
    'label != "car"',
    'colour != "red"',
    'NOT (colour = "red" AND label = "car")',
    'colour is null',
    'label is null',
    'source = "tractor"',
    'source != "drone"',
]


def test_1_5_pin_a_dataset_from_before_the_record_reads_as_before_on_both_grids(upgraded_world):
    async def scenario(r):
        stages = await stages_of(r, OLD, OLD_FORMS)
        await assert_grids_read(r, OLD, stages, await grids(r, OLD, stages))

    upgraded_world(scenario)


def test_1_5_a_dataset_not_yet_examined_counts_every_field_as_seen_on_both_kinds(upgraded_world):
    async def scenario(r):
        found = await kinds(r, OLD)
        assert {name: found.get(name) for name in ('colour', 'source', 'label')} == {
            'colour': (True, True),
            'source': (True, True),
            'label': (True, True),
        }
        assert all(pair == (True, True) for pair in found.values()), found

    upgraded_world(scenario)


def test_1_5_pin_a_database_the_records_migration_has_not_run_on_reads_as_after_the_upgrade(
    database,
    monkeypatch,
    tmp_path,
):
    """1.5 (added from the first full run): on a database the record's migration has not reached, the grids show what
    the contract's reading shows, the same rows as after the upgrade (the pin above), and nothing fails."""

    async def scenario(r: Regions) -> None:
        stages = await stages_of(r, OLD, OLD_FORMS)
        await assert_grids_read(r, OLD, stages, await grids(r, OLD, stages))

    serve(from_before_the_record(database, upgrade=False), monkeypatch, tmp_path, scenario)


def test_1_5_a_database_the_records_migration_has_not_run_on_counts_every_field_as_seen_on_both_kinds(
    database,
    monkeypatch,
    tmp_path,
):
    """1.5 (added from the first full run): no read fails over the missing record, and every form of field-stats says
    every field has been seen on both kinds."""

    async def scenario(r: Regions) -> None:
        where = f'/api/datasets/{OLD}/field-stats'
        for form, response in (
            ('GET', await r.call(ADMIN, 'GET', where)),
            ('GET brief', await r.call(ADMIN, 'GET', where, params={'brief': 'true'})),
            ('POST', await post(r, where, {})),
            ('POST facet request', await post(r, where, {'fields': ['colour', 'source', 'label']})),
        ):
            found = kinds_in(await field_entries(response))
            assert {'colour', 'source', 'label'} <= set(found), (form, found)
            assert all(pair == (True, True) for pair in found.values()), (form, found)

    serve(from_before_the_record(database, upgrade=False), monkeypatch, tmp_path, scenario)


def test_1_5_the_records_migration_run_a_second_time_changes_nothing(database, monkeypatch, tmp_path):
    """1.5 (added from the first full run): after the upgrade, the record's migration files run again, each in one
    transaction as the runner runs it, succeed and change nothing: the schema, the members of a dataset not yet
    examined (from before the record) and of one examined from the start (made after the upgrade), and what the
    grids show on both."""
    migrations = the_records_migrations()
    _, psql, schema, _ = database
    name = from_before_the_record(database, upgrade=True)

    async def scenario(r: Regions) -> None:
        w = await plant(r)
        stages = {
            OLD: await stages_of(r, OLD, OLD_FORMS),
            w.dataset_id: await stages_of(r, w.dataset_id, FORMS['colour']),
        }

        async def state() -> tuple[Any, ...]:
            members = {dataset: await kinds(r, dataset) for dataset in stages}
            shown = {dataset: await grids(r, dataset, stages[dataset]) for dataset in stages}
            return members, shown, schema(name)

        before = await state()
        assert before[0][w.dataset_id]['colour'] == (True, False), before[0]
        for migration in migrations:
            await asyncio.to_thread(psql, name, f'BEGIN;\n{migration.read_text()}\nCOMMIT;\n')
        after = await state()
        assert after[0] == before[0], ('members', before[0], after[0])
        assert after[1] == before[1], 'what the grids show'
        assert after[2] == before[2], 'the schema'

    serve(name, monkeypatch, tmp_path, scenario)
