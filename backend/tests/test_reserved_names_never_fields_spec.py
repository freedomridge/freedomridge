"""A stored metadata key named like one of the five older names (`id`, `dataset_id`, `filepath`, `created_at`,
`last_modified_at`) is not a field: never registered from metadata, and the registrations main left go back to the
sample's own field by a migration. The values stay in the samples.

Written from the contract (f7-contract-reserved-names-never-fields), not from the code, and tested where it is met:
the HTTP API signed in with real API tokens (the access-rule tests' World, a database of its own per test), the state
"a sample carries a stored key, and the registry has the row main gives it" written into the database directly in the
shape read on main (a dataset_fields row: field_type `string`, origin `metadata`, owner `self`, as main registered the
stored text and dates written here); the migration run as an upgrade runs it (the #513 spec's harness:
scripts/apply_migrations.sh on a database brought to the state before it); a mirror's sync driven by the follower
sync's own steps against the app in this process (test_follower_sync's harness); Sample.save() through the SDK with
its requests served by the app in this process; and the docs' text.

What main e0f754fe does, read before writing (what it does with filters and the field routes is pinned and passes
before the fix; what it does with rows of the five is told here, and the tests require the contract instead):
- No edit, upload, bulk edit or statistics refresh registers a stored key since #514: they refuse the key or leave a
  stored one out. Main registers one in three places: a version revert to a version whose snapshot holds such a field
  (it restores the definition; not for `tags` and `media_type`), a revert to a version saved before field definitions
  were kept in snapshots (it infers fields from the version's samples; not for `tags` and `media_type`), and a
  mirror's sync when its primary lists such a field (all seven). Field discovery (the overlay caches) shows nothing for
  a stored key.
- The value picker the sidebar and the filter builder share (POST /fields/<name>/autocomplete) reads the stored key of
  the five, and of `media_type`, whether or not the name is registered: it offers the stored values.
- With main's row, the field list (schema and field-stats, which the sidebar reads) shows `id`, `filepath`,
  `created_at` and `last_modified_at` once, as the sample's own field; the stored values go into that name's
  statistics row, and the value picker offers them. A filter that ticks one (`in`) answers 400 for `id`, `created_at`
  and `last_modified_at` (as it does on a dataset with no stored key), and 200 with no sample for `filepath`.
  `dataset_id` shows as a field of its own, with the stored values, and a filter on it reads the stored key. When the
  last sample carrying the key loses it, a row with a description stays as a metadata field; one without goes.
- A filter that names `dataset_id` reads the metadata key, also on a dataset where no sample carries one: `in [the
  dataset's own id]` finds nothing.
- The four readers by a caller's name read these columns, and the metadata key for every other of the seven names:
  the sample lookup `id` and `filepath`; the scatter's include_fields `id`, `dataset_id`, `filepath`, `created_at`
  and `tags`; diversity sampling none (it refuses `tags`, an array column); the grid's sort `id`, `filepath`,
  `created_at` and `last_modified_at`, and for another name the registry's row (without one, by id).
- The media route (GET /samples/{id}/media) reads `field=filepath` from the column and any other name from
  `metadata->>'<name>'`, a dotted name from `metadata->'<first>'->>'<second>'`; its range, thumbnail (`w`), crop and
  version requests build the path the same way. Its heatmap overlay (`overlay=<name>`) reads `metadata->'<name>'`
  (composites a stored heatmap, answers 500 for a stored text) and refuses a dotted name.
- A dotted name whose first part is one of the seven: the scatter reads into a stored object and a stored list's
  first element; the filter and the aggregations read into a stored list when the registry has main's `list` row of
  the first part; the histogram, the value picker, the sort and the sampler find nothing; the lookup refuses it.

Where the contract is unclear, these tests read it this way:
- 1.1 "the field list has the name once, as the sample's own field": schema and field-stats each list `id`,
  `filepath`, `created_at`, `last_modified_at` (and `tags`, `media_type`) exactly once, with the type main gives the
  sample's own field and no stored value in the entry, and no metadata row of the name in the registry; `dataset_id`,
  which is no field of the sample's in the field list, is not listed. The sidebar's values for the name, the value
  picker's answer, offer no stored value.
- 1.1 "reading counts too" and 3.2: a row an older release left that the migration has not reached (the database
  before the migration, as between the upgrade's two steps) is read as no field: the field list as above, the value
  picker, the statistics after the one ordinary refresh the planting ran (no statistics row of the five holds a stored
  value: 1.1 names "the field list and statistics"), the assistant's describe_dataset and field_values, and the field
  routes by name; the row itself may stand, so the registry is checked only after the migration.
- 1.1's paths are each tested from the state after the migration (a stored key, no row): editing another key, sending
  the whole metadata back, a bulk edit, an upload, one statistics refresh (pass today, required), the two reverts and a
  mirror's sync (fail today). The mirror's primary is made to list the field by answering its state with the field
  added, as a primary that still lists one would. The sidebar's values (2.3) are tested in that state too, for the
  seven names (1.2).
- 1.3's field routes by name are not settled: for each of the five, the answer main gives on a dataset where no sample
  carries the key (pinned, in a fixed order: describe, promote, clone, rename, clear) is required with stored keys:
  the status, and the detail of a refusal. After the migration this is compared with a dataset that never had either.
  "A way to remove such a key from every sample" is checked as removal per sample, by ids and by a filter that keeps
  every sample.
- 2.2 "what a field removal leaves": on main, a removal of a registry row leaves a `dataset_stats_pending` row of its
  own transaction with block -1 naming the field, and for a followed dataset a `dataset_changes` row of kind `fields`;
  the datasets, statistics state and statistics rows don't change. The new pending rows are those of transactions the
  database didn't have before; every sample keeps its row version.
- 2.3 "after ONE ordinary statistics refresh": the statistics runner run once per dataset, not forced. "The statistics
  of each name are the sample's own": no statistics row of the five names holds a stored value. "No other field's
  statistics are marked": the migration marks only the five names, and the other fields' statistics rows keep their
  values and counts through the refresh.
- 2.4: a line "for each field" mentions the dataset (its id or name) and the field; what it kept is named by the words
  `description`, `unit` and `private`; a leftover index by its name. A second run is the new migration files run
  again directly: no row changes and no line names a planted dataset or index. With nothing to do it prints at least
  one line and changes nothing.
- 2.5: for each dataset changed, a line that mentions it says the values stay: it has the words `value` and `metadata`
  and one of `stay`, `remain`, `still`, `untouched`.
- 3.1: "what main answers on a dataset where no sample carries such a key" is pinned literally, by sample position;
  the same is required with stored keys. For `dataset_id` that answer is main's metadata reading (nothing found), not
  the sample's column; if the fix reads the column instead, the pin changes and the contract has to say which.
- 1.1's four readers: each is pinned as main answers on a dataset where no sample carries the key, and required to
  answer the same (measured in the same test) with stored keys, with and without main's row. Where main reads the
  metadata key on that dataset although the name is a sample's own field (the lookup by `created_at`,
  `last_modified_at`, `media_type`, `tags`; the scatter's `last_modified_at` and `media_type`; diversity sampling on
  any of them; the sort by `media_type` and `tags` without a row), the contract's "the sample's own field where the
  name has one" and "as on a dataset where no sample carries such a key" give different answers: the pin holds
  main's, and the requirement holds whichever the fix gives on both datasets alike.
- 1.1's media route and dotted names, added later, in the same style. The media route is compared by status, the
  refusal's detail (the sample's id written N) and the bytes' digest, for the file, a range of it, a thumbnail, a
  crop and the file as a version holds it; the stored keys name a real file under the data root. A dotted name
  (`<name>.x`) is asked of the scatter (both routes), the filter, the histogram, the value picker, the aggregations,
  the sort, the lookup, the sampler, the heatmap overlay and the media route, with the samples storing objects
  holding `x` and lists of them, each with and without main's row of its type (`object`, `list`). The heatmap
  overlay's plain names (1.1's fifth reader) are tested too, with a stored heatmap and a stored text.
- 3.2 is checked on an unmigrated row in main's shape, with and without a description, by what a reader sees (the
  field list, with the description and unit, the value picker and the routes by name); and as a fact about the
  registry after the migration (the rows it turned back) and for a described built-in (no row appears).
"""

import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, cast
from urllib.parse import urlparse

import httpx
import pytest
from fridge.core.dataset import Dataset
from PIL import Image

from backend import follower_sync, main
from backend.routers import datasets, followers
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests import test_follower_sync, test_regions_spec, test_upgrade_paths
from backend.tests.changelog_helpers import release_heading
from backend.tests.test_access_rule_spec import ADMIN, World
from backend.tests.test_assistant_conversation_spec import count_statistics
from backend.tests.test_assistant_core_spec import open_door
from backend.tests.test_assistant_views_spec import call
from backend.tests.test_backend_four_spec import OLDER
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_protected_metadata_keys_spec import (
    MIGRATIONS,
    RELEASE,
    SEVEN,
    Upgrade,
    UpgradeWorld,
    counted,
    sdk,
)
from backend.tests.test_regions_spec import ADMIN_USER, Regions, apply_before
from backend.tests.test_upgrade_paths import ROOT

database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
registry_database = registry_helpers.registry_database
pytestmark = pytest.mark.skipif(
    not (os.getenv('FRIDGE_TEST_POSTGRES_DSN') and os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')),
    reason='Requires disposable PostgreSQL',
)

FIVE = OLDER
# The values stored under a name: the first sample's text, the second's a date, the third holds none.
STORED_TEXT, STORED_DATE = 'ZQXSTOREDTEXT', '1999-03-04T05:06:07Z'
STORED = (STORED_TEXT, STORED_DATE)
MARKS = (STORED_TEXT, '1999-03-04')  # how a stored value shows in any answer
# The type main's field list gives the sample's own field of the name (dataset_id is no field of the sample's there).
OWN_TYPE = {'id': 'number', 'filepath': 'filepath', 'created_at': 'datetime', 'last_modified_at': 'datetime'}


async def post(w: World, path: str, body: Any) -> httpx.Response:
    return await w.call(ADMIN, 'POST', path, json=body)


def shows_stored(answer: Any) -> bool:
    text = json.dumps(answer, default=str)
    return any(mark in text for mark in MARKS)


async def three(w: World, dataset: int, names: tuple[str, ...] = FIVE) -> list[int]:
    """A dataset of three samples (metadata {'ok': k}); the first carries each of `names` with STORED_TEXT, the
    second with STORED_DATE (written into `samples` directly, as only an old database holds them), the third none.
    Returns the ids in that order."""
    await w.dataset(dataset)
    samples = [{'filepath': f'{dataset}-{k}.jpg', 'metadata': {'ok': k}} for k in range(3)]
    await datasets.add_samples(dataset, {'samples': samples}, user=ADMIN_USER)
    ids = [
        row['id'] for row in await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id', dataset)
    ]
    for sample_id, value in zip(ids, STORED):
        if names:
            await w.database.execute(
                'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
                sample_id,
                {name: value for name in names},
            )
    return ids


async def main_row(
    w: World,
    dataset: int,
    name: str,
    *,
    description: Optional[str] = None,
    unit: Optional[str] = None,
    private: bool = False,
    promoted: bool = False,
    field_type: str = 'string',
) -> None:
    """The registry row main gave a stored key of the name (a metadata field of the dataset's own, `string` for the
    text and dates stored here, `object` or `list` for an object or a list)."""
    await w.database.execute(
        'INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, owner, is_promoted, description, '
        "unit, values_private) VALUES ($1, $2, $7, 'metadata', 'self', $3, $4, $5, $6)",
        dataset,
        name,
        promoted,
        description,
        unit,
        private,
        field_type,
    )


async def registered(w: World, dataset: int) -> set[str]:
    """The names among the seven the registry holds as metadata fields of the dataset."""
    rows = await w.database.fetch(
        "SELECT field_name FROM dataset_fields WHERE dataset_id = $1 AND origin = 'metadata' "
        'AND field_name = ANY($2::text[])',
        dataset,
        list(SEVEN),
    )
    return {row['field_name'] for row in rows}


async def stored_values(w: World, ids: list[int], name: str) -> list[Any]:
    rows = await w.database.fetch(
        'SELECT metadata -> $2 AS value FROM samples WHERE id = ANY($1::int[]) ORDER BY id',
        ids,
        name,
    )
    return [row['value'] for row in rows]


def assert_listed_as_own(listing: list[dict[str, Any]], name: str, where: Any) -> None:
    entries = [entry for entry in listing if entry['name'] == name]
    if name == 'dataset_id':
        assert entries == [], (where, entries)
        return
    assert len(entries) == 1, (where, name, entries)
    [entry] = entries
    assert entry.get('origin') != 'metadata', (where, entry)
    if name in OWN_TYPE:
        assert entry['type'] == OWN_TYPE[name], (where, entry)
    assert not shows_stored(entry), (where, entry)


async def assert_offers_no_stored_value(w: World, dataset: int, names: tuple[str, ...], where: Any = None) -> None:
    """The sidebar's values: the value picker (POST /fields/<name>/autocomplete) offers no stored value of the names
    (an answer other than 200 offers none)."""
    for name in names:
        offered = await post(w, f'/api/datasets/{dataset}/fields/{name}/autocomplete', {'limit': 100})
        if offered.status_code == 200:
            assert not shows_stored(offered.json()), (dataset, where, name, offered.text)


async def assert_the_samples_own(w: World, dataset: int, names: tuple[str, ...], where: Any = None) -> None:
    """1.1: no metadata field of the names in the registry, and the field list reads each as the sample's own."""
    assert not await registered(w, dataset) & set(names), (dataset, where, await registered(w, dataset))
    await assert_read_as_own(w, dataset, names, where)


async def assert_read_as_own(w: World, dataset: int, names: tuple[str, ...], where: Any = None) -> dict[str, Any]:
    """1.1's reading: the field list (schema and field-stats) has each name once, as the sample's own, with no stored
    value (dataset_id not at all), whatever rows the registry holds. Returns both listings' entries by name."""
    where = (dataset, where)
    schema = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset}/schema')
    stats = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset}/field-stats')
    assert schema.status_code == stats.status_code == 200, (schema.text, stats.text)
    for name in names:
        assert_listed_as_own(schema.json(), name, (where, 'schema'))
        assert_listed_as_own(stats.json(), name, (where, 'field-stats'))
    return {
        'schema': {entry['name']: entry for entry in schema.json()},
        'field-stats': {entry['name']: entry for entry in stats.json()},
    }


# --- 1. From now on: never registered ---

QUIET = 30


def test_editing_uploading_and_counting_register_no_stored_key(regions_world):
    """1.1 and 1.2 (pass on main since #514; required). A sample carrying a stored key of one of the seven names, no
    row of the name: an edit of another key, an edit that sends the whole metadata back (as Sample.save() does), a
    bulk edit of another key, an upload of another sample and one statistics refresh leave the registry without a
    metadata field of the name; the field list has it once, as the sample's own; the values stay in the samples."""

    async def scenario(r):
        for index, name in enumerate(SEVEN):
            dataset = QUIET + index
            ids = await three(r, dataset, (name,))
            edited = await r.call(
                ADMIN,
                'PATCH',
                f'/api/datasets/{dataset}/samples/{ids[0]}',
                json={'metadata': {'ok': 50}},
            )
            assert edited.status_code == 200, (name, edited.text)
            whole = await r.database.fetchval('SELECT metadata FROM samples WHERE id = $1', ids[1])
            edited = await r.call(
                ADMIN,
                'PATCH',
                f'/api/datasets/{dataset}/samples/{ids[1]}',
                json={'metadata': {**whole, 'ok': 51}},
            )
            assert edited.status_code == 200, (name, edited.text)
            bulk = await post(r, f'/api/datasets/{dataset}/samples/bulk-set', {'sample_ids': ids, 'metadata': {'m': 1}})
            assert bulk.status_code == 200, (name, bulk.text)
            added = await post(
                r,
                f'/api/datasets/{dataset}/samples',
                {'samples': [{'filepath': f'{dataset}-new.jpg', 'metadata': {'ok': 9}}]},
            )
            assert added.status_code == 200, (name, added.text)
            await count_statistics(r, dataset)
            await assert_the_samples_own(r, dataset, (name,), name)
            assert await stored_values(r, ids[:2], name) == list(STORED), name

    regions_world(scenario)


PICKED = 40


@pytest.mark.parametrize('name', SEVEN)
def test_the_sidebar_s_values_offer_no_stored_value(regions_world, name):
    """2.3's sidebar values in the state after the migration, a requirement (fails on main: the value picker reads
    the stored key whether or not the name is registered). A stored key of one of the seven names, no row of the name,
    the statistics counted: the value picker the sidebar and the filter builder share (POST
    /fields/<name>/autocomplete) offers no stored value, and the field-stats entry of the name holds none."""

    async def scenario(r):
        await three(r, PICKED, (name,))
        await count_statistics(r, PICKED)
        await assert_offers_no_stored_value(r, PICKED, (name,))
        listing = (await r.call(ADMIN, 'GET', f'/api/datasets/{PICKED}/field-stats')).json()
        assert not shows_stored([entry for entry in listing if entry['name'] == name]), listing

    regions_world(scenario)


HELD = 50


@pytest.mark.parametrize('name', SEVEN)
def test_a_revert_to_a_version_that_holds_such_a_field_registers_none(regions_world, name):
    """1.1 and 1.2, a requirement (fails on main: the revert restores the definition). A version saved while main's
    row stood (a stored key of one of the seven names, registered); the row then goes, as the migration leaves a row
    with nothing to keep; a revert to the version leaves no metadata field of the name, the field list has it once, as
    the sample's own, and the five names' stored values are back in the samples as the version holds them."""

    async def scenario(r):
        dataset = HELD
        ids = await three(r, dataset, (name,))
        await main_row(r, dataset, name)
        made = await post(r, f'/api/datasets/{dataset}/versions', {'name': 'with the field'})
        assert made.status_code == 200, made.text
        await r.database.execute(
            'DELETE FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
            dataset,
            name,
        )
        reverted = await post(r, f'/api/datasets/{dataset}/versions/{made.json()["id"]}/revert', {})
        assert reverted.status_code == 200, reverted.text
        await assert_the_samples_own(r, dataset, (name,), name)
        if name in FIVE:
            assert await stored_values(r, ids[:2], name) == list(STORED), name

    regions_world(scenario)


OLD_VERSION = 70


@pytest.mark.parametrize('name', SEVEN)
def test_a_revert_to_a_version_from_before_field_definitions_registers_none(regions_world, name):
    """1.1 and 1.2, a requirement (fails on main for the five, whose names such a revert infers from the version's
    samples). A version whose snapshot has no field definitions (as one saved by a release before they were kept: its
    `field_registry_version` taken out), samples carrying a stored key of one of the seven names, no row of the name:
    the revert leaves no metadata field of the name, and the field list has it once, as the sample's own."""

    async def scenario(r):
        dataset = OLD_VERSION
        await three(r, dataset, (name,))
        made = await post(r, f'/api/datasets/{dataset}/versions', {'name': 'old'})
        assert made.status_code == 200, made.text
        await r.database.execute(
            "UPDATE dataset_versions SET dataset_snapshot = dataset_snapshot - 'field_registry_version' WHERE id = $1",
            made.json()['id'],
        )
        reverted = await post(r, f'/api/datasets/{dataset}/versions/{made.json()["id"]}/revert', {})
        assert reverted.status_code == 200, reverted.text
        await assert_the_samples_own(r, dataset, (name,), name)

    regions_world(scenario)


def test_a_mirror_s_sync_from_a_primary_that_still_lists_one_registers_none(monkeypatch, registry_database):
    """1.1 and 1.2, a requirement (fails on main: the mirror stores the primary's field). A primary whose samples
    carry stored keys of the five and whose state lists a metadata field of each of the seven names (its state
    answered with them added, as a primary that still lists them answers): after the sync the mirror's registry has
    no metadata field of the seven, its field list has each once, as the sample's own (`dataset_id` not at all), and
    its samples carry the stored keys as the primary holds them."""

    async def scenario(database):
        _, link_id = await test_follower_sync.setup(monkeypatch, database)
        await registry_helpers.ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=1, prefix='s3://images/a')
        first = await database.fetchval('SELECT min(id) FROM samples WHERE dataset_id = 1')
        await database.execute(
            'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
            first,
            {name: STORED_TEXT for name in FIVE},
        )
        answer_of_the_primary = follower_sync.call

        async def still_listing(client, method, path, **kwargs):
            answer = await answer_of_the_primary(client, method, path, **kwargs)
            if method == 'GET' and path.endswith('/state'):
                listed = {field['field_name'] for field in answer['fields']}
                answer['fields'] += [
                    {
                        'field_name': name,
                        'field_type': 'string',
                        'origin': 'metadata',
                        'is_promoted': False,
                        'display_type': None,
                        'overlay_config': None,
                        'description': None,
                        'unit': None,
                    }
                    for name in SEVEN
                    if name not in listed
                ]
            return answer

        monkeypatch.setattr(follower_sync, 'call', still_listing)
        follower = await database.fetchval('SELECT id FROM followers')
        await followers.follow_dataset(follower, 1, followers.FollowOptions(), user=registry_helpers.USER)
        await test_follower_sync.sync(database, link_id)
        mirror = (await test_follower_sync.mirror_of(database))['dataset_id']
        metadata_fields = await database.fetch(
            "SELECT field_name FROM dataset_fields WHERE dataset_id = $1 AND origin = 'metadata' "
            'AND field_name = ANY($2::text[])',
            mirror,
            list(SEVEN),
        )
        assert [row['field_name'] for row in metadata_fields] == [], metadata_fields
        schema = await datasets.get_dataset_schema(mirror)
        stats = await datasets._get_dataset_field_stats_impl(mirror, None)
        for name in SEVEN:
            assert_listed_as_own(schema, name, ('mirror schema', name))
            assert_listed_as_own(stats, name, ('mirror field-stats', name))
        carried = [
            row['metadata']
            for row in (await test_follower_sync.copied(database, {'dataset_id': mirror})).values()
            if row['metadata'].get('crop') == 'corn'
        ]
        assert carried == [{'crop': 'corn', **{name: STORED_TEXT for name in FIVE}}], carried

    registry_database(scenario)


SAVED = 90


@pytest.mark.parametrize('name', FIVE)
def test_a_stored_key_stays_save_sends_it_back_and_it_can_be_removed_from_every_sample(
    regions_world,
    monkeypatch,
    name,
):
    """1.3 (passes on main; required). A sample carrying a stored key, no row of the name: it reads back with the key;
    Sample.save() with another key changed goes through and sends the key back unchanged; the key is removed per
    sample, by ids, and from every sample by a filter that keeps all of them; at no point is the name a metadata
    field of the dataset."""

    async def scenario(r):
        ids = await three(r, SAVED, (name,))
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def another_key_changed() -> None:
            sample = Dataset(f'dataset-{SAVED}')[f'{SAVED}-0.jpg']
            assert sample.metadata[name] == STORED_TEXT, sample.metadata
            sample.metadata['ok'] = 55
            sample.save()

        await asyncio.to_thread(another_key_changed)
        assert await r.database.fetchval('SELECT metadata FROM samples WHERE id = $1', ids[0]) == {
            'ok': 55,
            name: STORED_TEXT,
        }
        assert not await registered(r, SAVED)
        removed = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{SAVED}/samples/{ids[0]}',
            json={'remove_metadata_keys': [name]},
        )
        assert removed.status_code == 200, removed.text
        removed = await post(
            r,
            f'/api/datasets/{SAVED}/samples/bulk-set',
            {'sample_ids': [ids[1]], 'remove_metadata_keys': [name]},
        )
        assert removed.status_code == 200, removed.text
        assert await stored_values(r, ids, name) == [None, None, None]
        await r.database.execute(
            'UPDATE samples SET metadata = metadata || $2::jsonb WHERE dataset_id = $1',
            SAVED,
            {name: STORED_TEXT},
        )
        every = {'field': 'ok', 'operator': 'exists', 'value': None}
        removed = await post(
            r,
            f'/api/datasets/{SAVED}/samples/bulk-set',
            {'filters': every, 'remove_metadata_keys': [name]},
        )
        assert removed.status_code in (200, 202), removed.text
        if removed.status_code == 202:
            await finish(r.database)
        assert await stored_values(r, ids, name) == [None, None, None]
        assert not await registered(r, SAVED)

    regions_world(scenario)


# 1.3's field routes by name, in this order, and what main answers for each of the five on a dataset where no sample
# carries the key: (status, the detail of a refusal).
ROUTES = (
    ('PATCH', 'description', {'description': 'Said.'}),
    ('POST', 'promote', None),
    ('POST', 'clone', {'new_name': 'copy_of_it'}),
    ('POST', 'rename', {'new_name': 'moved_to'}),
    ('POST', 'clear', None),
)
BUILT_IN_ANSWERS = [
    (200, None),
    (400, 'Cannot promote field type: unknown'),
    (400, 'Rename and clone require a metadata field'),
    (400, 'Rename and clone require a metadata field'),
    (400, 'Clear requires a metadata field'),
]
ROUTE_ANSWERS = {
    **{name: BUILT_IN_ANSWERS for name in ('id', 'filepath', 'created_at', 'last_modified_at')},
    'dataset_id': [
        (404, "Field 'dataset_id' is not a field of this dataset"),
        (404, "Field 'dataset_id' has no definition in this dataset"),
        (404, "Field 'dataset_id' is not registered"),
        (404, "Field 'dataset_id' is not registered"),
        (200, None),
    ],
}


async def route_answers(w: World, dataset: int, name: str) -> list[tuple[int, Optional[str]]]:
    answers: list[tuple[int, Optional[str]]] = []
    for method, route, body in ROUTES:
        options = {'json': body} if body is not None else {}
        response = await w.call(ADMIN, method, f'/api/datasets/{dataset}/fields/{name}/{route}', **options)
        detail = response.json().get('detail') if response.status_code >= 400 else None
        answers.append((response.status_code if response.status_code >= 400 else 200, detail))
    return answers


ROUTED = 110


def test_the_field_routes_answer_for_one_of_the_five_with_stored_keys_as_without(regions_world):
    """1.3, which the contract leaves to the implementer: pinned as main answers on a dataset where no sample carries
    the key (passes on main), and required the same with stored keys and no row (the state after the migration):
    describe, promote, clone, rename, clear, in that order; for the four that main refuses to clear, the stored values
    stay."""

    async def scenario(r):
        for index, name in enumerate(FIVE):
            without, carrying = ROUTED + 2 * index, ROUTED + 2 * index + 1
            await three(r, without, ())
            assert await route_answers(r, without, name) == ROUTE_ANSWERS[name], name
            ids = await three(r, carrying, (name,))
            assert await route_answers(r, carrying, name) == ROUTE_ANSWERS[name], name
            if name != 'dataset_id':
                assert await stored_values(r, ids[:2], name) == list(STORED), name
            assert not await registered(r, carrying)

    regions_world(scenario)


# --- 3. What a reader sees afterwards ---

FILTERED = 130


def filters_of(name: str, dataset: int, ids: list[int], first_path: str) -> list[dict[str, Any]]:
    operations: dict[str, list[tuple[str, Any]]] = {
        'id': [('in', [ids[0]]), ('>=', ids[1]), ('exists', None), ('in', [STORED_TEXT])],
        'dataset_id': [
            ('in', [dataset]),
            ('=', dataset),
            ('exists', None),
            ('not_exists', None),
            ('in', [STORED_TEXT]),
        ],
        'filepath': [('in', [first_path]), ('contains', '.jpg'), ('exists', None), ('in', [STORED_TEXT])],
        'created_at': [('>=', '2001-01-01T00:00:00Z'), ('exists', None), ('in', [STORED_TEXT]), ('in', [STORED_DATE])],
        'last_modified_at': [
            ('>=', '2001-01-01T00:00:00Z'),
            ('exists', None),
            ('in', [STORED_TEXT]),
            ('in', [STORED_DATE]),
        ],
    }
    return [{'field': name, 'operator': operator, 'value': value} for operator, value in operations[name]]


# What main answers for those filters on a dataset where no sample carries the key: the samples kept, by position,
# or the status and detail of a refusal.
INTEGER = "'str' object cannot be interpreted as an integer"
FILTER_ANSWERS: dict[str, list[Any]] = {
    'id': [[0], [1, 2], [0, 1, 2], (400, INTEGER)],
    'dataset_id': [[], [], [], [0, 1, 2], []],
    'filepath': [[0], [0, 1, 2], [0, 1, 2], []],
    'created_at': [[0, 1, 2], [0, 1, 2], (400, f"Invalid created_at value: '{STORED_TEXT}'"), []],
    'last_modified_at': [[0, 1, 2], [0, 1, 2], (400, f"Invalid last_modified_at value: '{STORED_TEXT}'"), []],
}


async def filter_answer(w: World, dataset: int, ids: list[int], node: dict[str, Any]) -> Any:
    response = await post(w, f'/api/datasets/{dataset}/samples/query', {'limit': 100, 'filters': node})
    if response.status_code != 200:
        return response.status_code, response.text
    return sorted(ids.index(item['id']) for item in response.json()['items'])


def matches(answer: Any, expected: Any) -> bool:
    if isinstance(expected, tuple):
        return isinstance(answer, tuple) and answer[0] == expected[0] and expected[1] in answer[1]
    return answer == expected


async def assert_filters_answer(w: World, dataset: int, name: str, carrying: bool, row: bool = False) -> None:
    ids = await three(w, dataset, (name,) if carrying else ())
    if row:
        await main_row(w, dataset, name)
    for node, expected in zip(filters_of(name, dataset, ids, f'{dataset}-0.jpg'), FILTER_ANSWERS[name]):
        answer = await filter_answer(w, dataset, ids, node)
        assert matches(answer, expected), (name, 'with stored keys' if carrying else 'without', node, answer)


@pytest.mark.parametrize('name', FIVE)
def test_today_a_filter_naming_one_of_the_five_answers_so_where_no_sample_carries_it(regions_world, name):
    """3.1's pin (passes on main): for each of the five, filters naming it (by its own values, `exists`, and a ticked
    stored value) on a dataset where no sample carries the key answer as FILTER_ANSWERS has it."""

    async def scenario(r):
        await assert_filters_answer(r, FILTERED, name, carrying=False)

    regions_world(scenario)


@pytest.mark.parametrize('name', FIVE)
def test_a_filter_naming_one_of_the_five_answers_with_stored_keys_as_without(regions_world, name):
    """3.1, a requirement: the same filters answer the same where samples carry the key, with no row (the state after
    the migration) and with main's row (an older release's, not yet migrated). Fails on main for `dataset_id`, whose
    filter reads the stored key."""

    async def scenario(r):
        await assert_filters_answer(r, FILTERED, name, carrying=True)
        await assert_filters_answer(r, FILTERED + 1, name, carrying=True, row=True)

    regions_world(scenario)


LAST = 150


@pytest.mark.parametrize('name', FIVE)
def test_when_the_last_stored_key_goes_an_unmigrated_row_is_no_field_to_a_reader(regions_world, name):
    """3.2 for a row an older release left that the migration has not reached (a requirement; fails on main, where
    the row with a description stays as a metadata field after the last key went, and is read as one). Main's row of
    the name, with a description and a unit and without, the stored keys removed from the samples that carry them (one
    by PATCH, one by bulk-set): the field list has the name once, as the sample's own (`dataset_id` not at all), with
    the description and unit written about the name; the value picker offers no stored value; the field routes by name
    answer as on a dataset where no sample carries the key. The row itself may stand: the registry is not checked."""

    async def scenario(r):
        for dataset, said in ((LAST, True), (LAST + 1, False)):
            ids = await three(r, dataset, (name,))
            if said:
                await main_row(r, dataset, name, description='Said.', unit='u')
            else:
                await main_row(r, dataset, name)
            await remove_both(r, dataset, ids, name)
            listed = await assert_read_as_own(r, dataset, (name,), said)
            if name != 'dataset_id' and said:
                for listing in ('schema', 'field-stats'):
                    entry = listed[listing][name]
                    assert (entry.get('description'), entry.get('unit')) == ('Said.', 'u'), (listing, entry)
            await assert_offers_no_stored_value(r, dataset, (name,), said)
            assert await route_answers(r, dataset, name) == ROUTE_ANSWERS[name], (name, said)

    regions_world(scenario)


async def remove_both(w: World, dataset: int, ids: list[int], name: str) -> None:
    removed = await w.call(
        ADMIN,
        'PATCH',
        f'/api/datasets/{dataset}/samples/{ids[0]}',
        json={'remove_metadata_keys': [name]},
    )
    assert removed.status_code == 200, removed.text
    removed = await post(
        w,
        f'/api/datasets/{dataset}/samples/bulk-set',
        {'sample_ids': [ids[1]], 'remove_metadata_keys': [name]},
    )
    assert removed.status_code == 200, removed.text
    assert await stored_values(w, ids, name) == [None, None, None]


def test_when_the_last_stored_key_goes_a_described_built_in_keeps_its_description_and_no_row_appears(regions_world):
    """3.2 after the migration (passes on main; required): a described built-in (described through the route: the row
    the migration leaves) keeps its description and unit when the stored keys go, and no metadata row of the name
    appears. `dataset_id` has no field of the sample's to describe (the route answers 404)."""

    async def scenario(r):
        for index, name in enumerate(('id', 'filepath', 'created_at', 'last_modified_at')):
            dataset = LAST + 10 + index
            ids = await three(r, dataset, (name,))
            described = await r.call(
                ADMIN,
                'PATCH',
                f'/api/datasets/{dataset}/fields/{name}/description',
                json={'description': 'Said.', 'unit': 'u'},
            )
            assert described.status_code == 200, described.text
            await remove_both(r, dataset, ids, name)
            row = await r.database.fetchrow(
                'SELECT origin, description, unit FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
                dataset,
                name,
            )
            assert tuple(row) == ('system', 'Said.', 'u'), (name, row)
            assert await registered(r, dataset) == set(), name

    regions_world(scenario)


ALIKE = 170
LOOK_ALIKES = ('created_at_source', 'filepath_original', 'id_number', 'dataset_id_old', 'last_modified_at_local')


def test_look_alike_names_and_heading_stay_ordinary_fields(regions_world):
    """3.3, a pin (passes on main): names that only contain one of the five, and `heading`, written when samples are
    added, are metadata fields of the dataset, listed, and found by a filter on their values."""

    async def scenario(r):
        await r.dataset(ALIKE)
        names = (*LOOK_ALIKES, 'heading')
        samples = [
            {'filepath': f'alike-{k}.jpg', 'metadata': {name: f'{name}-{k}' for name in names}} for k in range(2)
        ]
        added = await post(r, f'/api/datasets/{ALIKE}/samples', {'samples': samples})
        assert added.status_code == 200, added.text
        rows = await r.database.fetch(
            "SELECT field_name FROM dataset_fields WHERE dataset_id = $1 AND origin = 'metadata'",
            ALIKE,
        )
        assert set(names) <= {row['field_name'] for row in rows}, rows
        listed = [entry['name'] for entry in (await r.call(ADMIN, 'GET', f'/api/datasets/{ALIKE}/schema')).json()]
        for name in names:
            assert listed.count(name) == 1, (name, listed)
            node = {'field': name, 'operator': 'in', 'value': [f'{name}-1']}
            response = await post(r, f'/api/datasets/{ALIKE}/samples/query', {'limit': 10, 'filters': node})
            assert response.status_code == 200, response.text
            assert [item['filepath'] for item in response.json()['items']] == ['alike-1.jpg'], (name, response.text)

    regions_world(scenario)


# --- 1.1's four readers by a caller's name (added to the contract on 2026-11-22) ---
#
# Each reader is asked the same thing on three datasets of three samples: one where no sample carries a key of the
# name, one whose samples carry stored keys of it (values that change the answer if read), and one with main's row of
# the name besides. Its answer is put in terms that don't depend on the dataset (sample positions, and whether a value
# is the sample's own or the stored one), so the three can be compared. Pinned: what main answers on the first.

READER = 190
STORED_FIRST = '1999-03-01T00:00:00Z'  # a created_at string that sorts before any sample's own


async def reader_dataset(
    w: World,
    dataset: int,
    name: str,
    stored: Optional[list[Any]],
    row: Any,
    vectors: bool = False,
    root: Optional[Path] = None,
) -> list[int]:
    """Three samples (`{dataset}-{k}.jpg`, the sample's own tag `own`, metadata {'ok': k}); with `stored`, sample k
    carries `name` with stored[k] (None: no key), written into `samples` directly; with `row`, main's row of the name;
    with `vectors`, a `clip` vector and a scatter point for each sample; with `root`, each sample's own file there
    (OWN_IMAGE). `row` is True for main's `string` row, or the row's type. Returns the ids in order."""
    if root is not None:
        for k in range(3):
            (root / f'{dataset}-{k}.jpg').write_bytes(OWN_IMAGE)
    await w.dataset(dataset)
    samples = [{'filepath': f'{dataset}-{k}.jpg', 'tags': ['own'], 'metadata': {'ok': k}} for k in range(3)]
    await datasets.add_samples(dataset, {'samples': samples}, user=ADMIN_USER)
    ids = [
        row_['id']
        for row_ in await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id', dataset)
    ]
    for sample_id, value in zip(ids, stored or []):
        if value is not None:
            await w.database.execute(
                'UPDATE samples SET metadata = metadata || jsonb_build_object($2::text, $3::jsonb) WHERE id = $1',
                sample_id,
                name,
                value,
            )
    if row:
        await main_row(w, dataset, name, field_type='string' if row is True else row)
    if vectors:
        uploaded = await post(
            w,
            f'/api/datasets/{dataset}/embeddings',
            [
                {'sample_id': sample_id, 'vector': [1.0, k / 10, 0.0], 'field_name': 'clip'}
                for k, sample_id in enumerate(ids)
            ],
        )
        assert uploaded.is_success, uploaded.text
        await w.database.execute(
            "INSERT INTO embedding_coordinates (sample_id, field_name, x, y) "
            "SELECT i, 'clip', i, -i FROM unnest($1::int[]) i",
            ids,
        )
    return ids


async def own_rows(w: World, ids: list[int]) -> list[dict[str, Any]]:
    """The samples' own fields, in order."""
    rows = await w.database.fetch(
        'SELECT id, dataset_id, filepath, created_at, last_modified_at, media_type, tags FROM samples '
        'WHERE id = ANY($1::int[]) ORDER BY id',
        ids,
    )
    return [dict(row) for row in rows]


async def assert_reader(w: World, name: str, answer: Any, stored: Any, vectors: bool = False) -> None:
    """The reader's answer with stored keys (`stored(dataset)`: each sample's value), and with main's row besides, is
    its answer where no sample carries one."""
    variants = [('with stored keys', stored, False), ("with stored keys and main's row", stored, True)]
    await assert_answers(w, name, answer, variants, vectors)


async def assert_answers(
    w: World,
    name: str,
    answer: Any,
    variants: list[tuple[str, Any, Any]],
    vectors: bool = False,
    root: Optional[Path] = None,
    asked: Optional[str] = None,
) -> None:
    """The reader's answer for `asked` (the name itself unless given) on each variant (a label, `stored(dataset)`:
    each sample's value of the name, and the row: False, True for main's `string` row, or its type) is its answer on
    a dataset where no sample carries the key."""
    ids = await reader_dataset(w, READER, name, None, False, vectors, root)
    without = await answer(w, READER, ids, asked or name)
    for offset, (label, stored, row) in enumerate(variants, start=1):
        dataset = READER + offset
        ids = await reader_dataset(w, dataset, name, stored(dataset), row, vectors, root)
        found = await answer(w, dataset, ids, asked or name)
        assert found == without, (asked or name, label, found, 'without:', without)


async def pin_reader(w: World, name: str, answer: Any, expected: Any, vectors: bool = False) -> None:
    ids = await reader_dataset(w, READER, name, None, False, vectors)
    found = await answer(w, READER, ids, name)
    assert found == expected, (name, found)


# The sample lookup (POST /samples/lookup, behind the SDK's lookup_samples and merge_samples(key_field=...)): asked for
# the first sample's own value of the name and for the value it stores (main's lookup takes ints for `id`, text for
# `filepath`, and any value for a metadata key); its answer is which of the two found which sample.
LOOKUP_STORED: dict[str, Any] = {
    'id': 900000,
    'filepath': 'stored-0.jpg',
    'dataset_id': None,  # the dataset's own id: a stored dataset_id that matches the lookup
    'created_at': STORED_FIRST,
    'last_modified_at': STORED_FIRST,
    'tags': 'stored-tag',
    'media_type': 'video',  # another kind
}
LOOKUP_ANSWERS: dict[str, Any] = {
    'id': {'own': 0},
    'filepath': {'own': 0},
    'dataset_id': {},
    'created_at': {},
    'last_modified_at': {},
    'tags': {},
    'media_type': {},
}


def looked_up_stored(name: str, dataset: int) -> Any:
    return dataset if name == 'dataset_id' else LOOKUP_STORED[name]


async def lookup_answer(w: World, dataset: int, ids: list[int], name: str) -> Any:
    own = (await own_rows(w, ids))[0][name]
    sent = own.isoformat() if hasattr(own, 'isoformat') else ('own' if name == 'tags' else own)
    stored = looked_up_stored(name, dataset)
    response = await post(
        w,
        f'/api/datasets/{dataset}/samples/lookup',
        {'field': name, 'values': [sent] + ([stored] if stored != sent else [])},
    )
    if response.status_code != 200:
        return response.status_code, response.json().get('detail')
    labels = {str(stored): 'stored', str(sent): 'own'}
    return {labels[key]: ids.index(sample_id) for key, sample_id in response.json().items()}


@pytest.mark.parametrize('name', SEVEN)
def test_today_the_sample_lookup_answers_so_where_no_sample_carries_the_key(regions_world, name):
    """1.1's lookup, a pin (passes on main): asked for the first sample's own value and a stored-looking one, main
    finds the sample by its own `id` and `filepath`, and nothing by the other names (it reads the metadata key)."""

    async def scenario(r):
        await pin_reader(r, name, lookup_answer, LOOKUP_ANSWERS[name])

    regions_world(scenario)


@pytest.mark.parametrize('name', SEVEN)
def test_the_sample_lookup_answers_with_stored_keys_as_without(regions_world, name):
    """1.1's lookup, a requirement: the same lookup answers the same where the first sample stores a key of the name
    that matches it, with and without main's row. Fails on main for every name but `id` and `filepath`."""

    async def scenario(r):
        await assert_reader(r, name, lookup_answer, lambda dataset: [looked_up_stored(name, dataset), None, None])

    regions_world(scenario)


# The embeddings scatter's include_fields (POST /embeddings/query and /embeddings/lookup): the value given beside each
# point, as the sample's own value, the stored one, or none.
SCATTER_STORED: dict[str, list[Any]] = {
    'id': [900000, 900001, 900002],
    'dataset_id': [800000, 800001, 800002],
    'filepath': ['stored-0.jpg', 'stored-1.jpg', 'stored-2.jpg'],
    'created_at': ['1999-03-01T00:00:00Z', '1999-03-02T00:00:00Z', '1999-03-03T00:00:00Z'],
    'last_modified_at': ['1999-03-01T00:00:00Z', '1999-03-02T00:00:00Z', '1999-03-03T00:00:00Z'],
    'tags': ['stored-0', 'stored-1', 'stored-2'],
    'media_type': ['video', 'audio', 'pointcloud'],
}
OWN_BESIDE = ('id', 'dataset_id', 'filepath', 'created_at', 'tags')  # main gives the sample's own value of these


def beside(value: Any, own: Any, stored: Any) -> Optional[str]:
    if value is None:
        return None
    if str(value) == str(stored):
        return 'stored'
    if hasattr(own, 'isoformat'):
        return 'own' if isinstance(value, str) and datetime.fromisoformat(value) == own else repr(value)
    return 'own' if value == own else repr(value)


async def scatter_answer(w: World, dataset: int, ids: list[int], name: str) -> Any:
    own = await own_rows(w, ids)
    stored = SCATTER_STORED[name]
    found: dict[str, Any] = {}
    for route, body in (
        ('query', {'field': 'clip', 'include_fields': [name], 'sample_pct': 100}),
        ('lookup', {'field': 'clip', 'sample_ids': ids, 'include_fields': [name]}),
    ):
        response = await post(w, f'/api/datasets/{dataset}/embeddings/{route}', body)
        if response.status_code != 200:
            found[route] = (response.status_code, response.text)
            continue
        found[route] = {
            ids.index(point['sample_id']): beside(
                point.get(name),
                own[ids.index(point['sample_id'])][name],
                stored[ids.index(point['sample_id'])],
            )
            for point in response.json()['points']
        }
    return found


def scatter_expected(name: str) -> dict[str, Any]:
    kind = 'own' if name in OWN_BESIDE else None
    return {route: dict.fromkeys(range(3), kind) for route in ('query', 'lookup')}


@pytest.mark.parametrize('name', SEVEN)
def test_today_the_scatter_s_include_fields_answer_so_where_no_sample_carries_the_key(regions_world, name):
    """1.1's scatter, a pin (passes on main): beside each point main gives the sample's own `id`, `dataset_id`,
    `filepath`, `created_at` and `tags`, and nothing for `last_modified_at` and `media_type` (it reads the metadata
    key)."""

    async def scenario(r):
        await pin_reader(r, name, scatter_answer, scatter_expected(name), vectors=True)

    regions_world(scenario)


@pytest.mark.parametrize('name', SEVEN)
def test_the_scatter_s_include_fields_answer_with_stored_keys_as_without(regions_world, name):
    """1.1's scatter, a requirement: beside each point the same, where the samples store keys of the name, with and
    without main's row. Fails on main for `last_modified_at` and `media_type`."""

    async def scenario(r):
        await assert_reader(r, name, scatter_answer, lambda _: SCATTER_STORED[name], vectors=True)

    regions_world(scenario)


# Diversity sampling (POST /sparse-sample with the name as its one field): how many it picks from how many candidates,
# or its refusal. The stored values are numbers, which main would diversify on.
SAMPLED_STORED = [0, 50, 100]
NO_FIELD = (400, 'None of the requested fields can be used for diversity sampling')


async def sampling_answer(w: World, dataset: int, ids: list[int], name: str) -> Any:
    response = await post(w, f'/api/datasets/{dataset}/sparse-sample', {'count': 2, 'fields': [name]})
    if response.status_code != 200:
        return response.status_code, response.json().get('detail')
    body = response.json()
    return body['count'], body['candidate_count'], len(body['sample_ids'])


@pytest.mark.parametrize('name', SEVEN)
def test_today_diversity_sampling_answers_so_where_no_sample_carries_the_key(regions_world, name):
    """1.1's sampler, a pin (passes on main): on a field named like one of them main finds no candidate (it reads
    the metadata key), and refuses `tags`, an array."""

    async def scenario(r):
        await pin_reader(r, name, sampling_answer, NO_FIELD if name == 'tags' else (0, 0, 0))

    regions_world(scenario)


@pytest.mark.parametrize('name', SEVEN)
def test_diversity_sampling_answers_with_stored_keys_as_without(regions_world, name):
    """1.1's sampler, a requirement: the same answer where the samples store numbers under the name, with and without
    main's row. Fails on main for every name but `tags`."""

    async def scenario(r):
        await assert_reader(r, name, sampling_answer, lambda _: SAMPLED_STORED)

    regions_world(scenario)


# The grid sorted by a name (POST /samples/query with sort_by): the order of the samples, by their planted `ok`, in
# both directions; main breaks ties by id. Read, the stored values would put the last sample first ascending.
SORT_STORED: dict[str, list[Any]] = {
    name: ['1999-03-03T00:00:00Z', '1999-03-02T00:00:00Z', STORED_FIRST]
    if name in ('created_at', 'last_modified_at')
    else ['zc', 'zb', 'za']
    for name in SEVEN
}
SORT_ANSWERS: dict[str, dict[str, list[int]]] = {
    name: {'asc': [0, 1, 2], 'desc': [2, 1, 0] if name in ('id', 'filepath') else [0, 1, 2]} for name in SEVEN
}


async def sort_answer(w: World, dataset: int, ids: list[int], name: str) -> Any:
    found: dict[str, Any] = {}
    for order in ('asc', 'desc'):
        response = await post(
            w,
            f'/api/datasets/{dataset}/samples/query',
            {'limit': 100, 'sort_by': name, 'sort_order': order},
        )
        if response.status_code != 200:
            found[order] = (response.status_code, response.text)
            continue
        found[order] = [item['metadata']['ok'] for item in response.json()['items']]
    return found


@pytest.mark.parametrize('name', SEVEN)
def test_today_the_grid_sorted_by_a_name_is_ordered_so_where_no_sample_carries_the_key(regions_world, name):
    """1.1's sort, a pin (passes on main): by `id` and `filepath` the samples' own order, by `created_at` and
    `last_modified_at` their own (equal here, so by id), and by `dataset_id`, `tags` and `media_type`, which have no
    row, by id in both directions."""

    async def scenario(r):
        await pin_reader(r, name, sort_answer, SORT_ANSWERS[name])

    regions_world(scenario)


@pytest.mark.parametrize('name', SEVEN)
def test_the_grid_sorted_by_a_name_is_ordered_with_stored_keys_as_without(regions_world, name):
    """1.1's sort, a requirement: the same order where the samples store keys of the name, with and without main's
    row. Fails on main for `dataset_id`, `tags` and `media_type` with the row, which main sorts by the stored key."""

    async def scenario(r):
        await assert_reader(r, name, sort_answer, lambda _: SORT_STORED[name])

    regions_world(scenario)


# --- 1.1's media route and dotted names (added to the contract on 2026-11-23) ---
#
# The same style: the answer where no sample carries a key of the name is pinned as main gives it, and the same
# answer is required where the samples store keys of it that would change the answer if read, with and without main's
# row (of the type main gave the stored value: `string` for text, `object` for an object, `list` for a list).

STORED_PATH = 'stored.png'  # a real file under the data root, other than any sample's own


def png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(buffer, format='PNG')
    return buffer.getvalue()


OWN_IMAGE, STORED_IMAGE = png((200, 0, 0)), png((0, 0, 200))


def media_root(w: World) -> Path:
    """The app's data root (and its media caches) in this test's own folder, holding STORED_PATH."""
    regions = cast('Regions', w)
    root = regions.folder / 'data-root'
    root.mkdir(exist_ok=True)
    for setting in ('CROP_CACHE_PATH', 'COMPOSITE_CACHE_PATH', 'S3_LOCAL_PATH'):
        regions.monkeypatch.setattr(main, setting, str(regions.folder / setting.lower()))
    regions.monkeypatch.setattr(main, 'DATA_ROOT', str(root))
    (root / STORED_PATH).write_bytes(STORED_IMAGE)
    return root


async def media(w: World, sample_id: int, params: dict[str, Any], headers: Optional[dict[str, str]] = None) -> tuple:
    """GET /samples/{id}/media as the admin: the status and the bytes' digest, or the refusal's detail with the
    sample's id written N."""
    response = await w.client.get(
        f'/api/samples/{sample_id}/media',
        params=params,
        headers={'Authorization': f'Bearer {w.tokens[ADMIN]}', **(headers or {})},
    )
    if response.status_code in (200, 206):
        return response.status_code, hashlib.sha256(response.content).hexdigest()
    try:
        detail = response.json().get('detail')
    except ValueError:
        detail = response.text
    return response.status_code, str(detail).replace(f'Sample {sample_id} ', 'Sample N ')


async def media_answer(w: World, dataset: int, ids: list[int], asked: str) -> dict[str, tuple]:
    """The media route with `field=asked` for the first sample: the file, a range of it, a thumbnail, a crop, the file
    as a version holds it, and an overlay on it (of a heatmap no sample has: the base is looked up first)."""
    made = await post(w, f'/api/datasets/{dataset}/versions', {'name': 'v'})
    assert made.status_code == 200, made.text
    return {
        'file': await media(w, ids[0], {'field': asked}),
        'range': await media(w, ids[0], {'field': asked}, {'Range': 'bytes=0-99999'}),
        'thumbnail': await media(w, ids[0], {'field': asked, 'w': 8}),
        'crop': await media(w, ids[0], {'field': asked, 'crop': '0,0,0.5,0.5'}),
        'version': await media(w, ids[0], {'field': asked, 'version_id': made.json()['id']}),
        'overlay': await media(w, ids[0], {'field': asked, 'overlay': 'heat'}),
    }


@pytest.mark.parametrize('name', SEVEN)
def test_today_the_media_route_answers_so_for_a_field_named_like_one_where_no_sample_carries_it(regions_world, name):
    """1.1's media route, a pin (passes on main): with `field=filepath` the sample's own file (whole, as a range,
    from a version; a thumbnail and a crop of it; an overlay on it, refused for the heatmap no sample has); with any
    other of the seven, each of those answers 404 "Sample N has no value for field '<name>'"."""

    async def scenario(r):
        root = media_root(r)
        ids = await reader_dataset(r, READER, name, None, False, root=root)
        found = await media_answer(r, READER, ids, name)
        if name == 'filepath':
            own = hashlib.sha256(OWN_IMAGE).hexdigest()
            assert (found['file'], found['range'], found['version']) == ((200, own), (206, own), (200, own)), found
            assert found['thumbnail'][0] == found['crop'][0] == 200, found
            assert found['overlay'] == (404, 'Sample N has no heatmap in metadata.heat'), found
        else:
            assert found == dict.fromkeys(found, (404, f"Sample N has no value for field '{name}'")), found

    regions_world(scenario)


@pytest.mark.parametrize('name', SEVEN)
def test_the_media_route_answers_with_stored_keys_as_without(regions_world, name):
    """1.1's media route, a requirement: the same answers, bytes for bytes, where every sample stores the name as the
    path of a real file under the data root (another image), with and without main's row. Fails on main for every
    name but `filepath`, whose route reads the sample's own path."""

    async def scenario(r):
        root = media_root(r)

        def stored(_: int) -> list[Any]:
            return [STORED_PATH] * 3

        variants = [('with stored keys', stored, False), ("with stored keys and main's row", stored, True)]
        await assert_answers(r, name, media_answer, variants, root=root)

    regions_world(scenario)


STORED_HEATMAP = {'type': 'heatmap', 'map_path': STORED_PATH, 'range': [0, 1]}


async def overlay_answer(w: World, dataset: int, ids: list[int], asked: str) -> tuple:
    """The heatmap overlay (the media route's `overlay=asked`) on the first sample's own image."""
    return await media(w, ids[0], {'overlay': asked})


@pytest.mark.parametrize('name', SEVEN)
def test_today_the_heatmap_overlay_answers_so_where_no_sample_carries_the_key(regions_world, name):
    """1.1's heatmap overlay, a pin (passes on main): 404 "Sample N has no heatmap in metadata.<name>"."""

    async def scenario(r):
        root = media_root(r)
        ids = await reader_dataset(r, READER, name, None, False, root=root)
        assert await overlay_answer(r, READER, ids, name) == (404, f'Sample N has no heatmap in metadata.{name}')

    regions_world(scenario)


@pytest.mark.parametrize('name', SEVEN)
def test_the_heatmap_overlay_answers_with_stored_keys_as_without(regions_world, name):
    """1.1's heatmap overlay, a requirement: the same 404, word for word, where the samples store a heatmap under the
    name (pointing at a real file) or a text, with and without main's row. Fails on main for all seven (it composites
    the stored heatmap, and answers 500 for the text)."""

    async def scenario(r):
        root = media_root(r)

        def heatmap(_: int) -> list[Any]:
            return [STORED_HEATMAP] * 3

        def text(_: int) -> list[Any]:
            return ['patched'] * 3

        variants = [
            ('a stored heatmap', heatmap, False),
            ("a stored heatmap and main's row", heatmap, 'object'),
            ('a stored text', text, False),
            ("a stored text and main's row", text, True),
        ]
        await assert_answers(r, name, overlay_answer, variants, root=root)

    regions_world(scenario)


# A dotted name whose first part is one of the seven (`created_at.x`), through every reader that takes a name: the
# samples store objects holding `x` under the name ({'x': value}), or lists of them ([{'x': value}]), with values that
# would change the answer if read.


async def dotted_scatter_answer(w: World, dataset: int, ids: list[int], asked: str) -> dict[str, Any]:
    alias = asked.replace('.', '_')
    found: dict[str, Any] = {}
    for route, body in (
        ('query', {'field': 'clip', 'include_fields': [asked], 'sample_pct': 100}),
        ('lookup', {'field': 'clip', 'sample_ids': ids, 'include_fields': [asked]}),
    ):
        response = await post(w, f'/api/datasets/{dataset}/embeddings/{route}', body)
        if response.status_code != 200:
            found[route] = (response.status_code, response.text)
            continue
        points = {
            ids.index(point['sample_id']): point.get(alias, point.get(asked)) for point in response.json()['points']
        }
        found[route] = [points.get(k, 'no point') for k in range(3)]
    return found


async def dotted_filter_answer(w: World, dataset: int, ids: list[int], asked: str) -> dict[str, Any]:
    found = {}
    for value in ('stored-0', 'stored-1'):
        found[value] = await filter_answer(w, dataset, ids, {'field': asked, 'operator': 'in', 'value': [value]})
    return found


async def histogram_answer(w: World, dataset: int, ids: list[int], asked: str) -> Any:
    response = await post(w, f'/api/datasets/{dataset}/histogram', {'field': asked, 'bins': 5})
    if response.status_code != 200:
        return response.status_code, response.text
    body = response.json()
    return body.get('total'), body.get('missing'), body.get('buckets'), body.get('distinct_total')


async def picker_answer(w: World, dataset: int, ids: list[int], asked: str) -> Any:
    response = await post(w, f'/api/datasets/{dataset}/fields/{asked}/autocomplete', {'limit': 100})
    if response.status_code != 200:
        return response.status_code, response.text
    return sorted(str(item['value']) for item in response.json()['values'])


async def aggregations_answer(w: World, dataset: int, ids: list[int], asked: str) -> Any:
    aggregations = [{'field': asked, 'op': 'values'}, {'field': asked, 'op': 'count'}]
    response = await post(w, f'/api/datasets/{dataset}/aggregate', {'aggregations': aggregations})
    if response.status_code != 200:
        return response.status_code, response.text
    values, count = response.json()['results']
    return sorted(str(value) for value in values['value']), count['value']


async def dotted_lookup_answer(w: World, dataset: int, ids: list[int], asked: str) -> Any:
    response = await post(w, f'/api/datasets/{dataset}/samples/lookup', {'field': asked, 'values': ['stored-0']})
    if response.status_code != 200:
        return response.status_code, response.json().get('detail')
    return {key: ids.index(sample_id) for key, sample_id in response.json().items()}


async def dotted_media_answer(w: World, dataset: int, ids: list[int], asked: str) -> tuple:
    return await media(w, ids[0], {'field': asked})


TEXTS = ['stored-0', 'stored-1', 'stored-2']
# reader: (answer, each sample's `x`, needs vectors, needs files, main's answer where no sample carries the key)
DOTTED_READERS: dict[str, tuple[Any, list[Any], bool, bool, Any]] = {
    'scatter': (dotted_scatter_answer, TEXTS, True, False, lambda _: {'query': [None] * 3, 'lookup': [None] * 3}),
    'filter': (dotted_filter_answer, TEXTS, False, False, lambda _: {'stored-0': [], 'stored-1': []}),
    'histogram': (histogram_answer, [1, 5, 9], False, False, lambda _: (3, 3, [], 0)),
    'value_picker': (picker_answer, TEXTS, False, False, lambda _: []),
    'aggregations': (aggregations_answer, TEXTS, False, False, lambda _: ([], 3)),
    'sort': (sort_answer, ['zc', 'zb', 'za'], False, False, lambda _: {'asc': [0, 1, 2], 'desc': [0, 1, 2]}),
    'lookup': (dotted_lookup_answer, TEXTS, False, False, lambda _: (400, 'Invalid field name')),
    'sampler': (sampling_answer, [0, 50, 100], False, False, lambda _: (0, 0, 0)),
    'heatmap_overlay': (
        overlay_answer,
        [STORED_HEATMAP] * 3,
        False,
        True,
        lambda asked: (400, f'Invalid overlay field: {asked}'),
    ),
    'media_field': (
        dotted_media_answer,
        [STORED_PATH] * 3,
        False,
        True,
        lambda asked: (404, f"Sample N has no value for field '{asked}'"),
    ),
}
DOTTED_CASES = [(reader, name) for reader in DOTTED_READERS for name in SEVEN]


@pytest.mark.parametrize(('reader', 'name'), DOTTED_CASES)
def test_today_a_dotted_name_on_one_of_the_seven_answers_so_where_no_sample_carries_it(regions_world, reader, name):
    """1.1's dotted names, a pin (passes on main): `<name>.x` where no sample carries the key. The scatter gives no
    value; the filter, the histogram, the value picker, the aggregations, the sort and the sampler find nothing (the
    sort keeps id order); the lookup refuses the name ("Invalid field name"), the overlay too ("Invalid overlay
    field"), and the media route answers that the sample has no value for it."""

    async def scenario(r):
        answer, _, vectors, files, expected = DOTTED_READERS[reader]
        root = media_root(r) if files else None
        ids = await reader_dataset(r, READER, name, None, False, vectors, root)
        asked = f'{name}.x'
        found = await answer(r, READER, ids, asked)
        assert found == expected(asked), (reader, asked, found)

    regions_world(scenario)


@pytest.mark.parametrize(('reader', 'name'), DOTTED_CASES)
def test_a_dotted_name_on_one_of_the_seven_answers_with_stored_keys_as_without(regions_world, reader, name):
    """1.1's dotted names, a requirement: `<name>.x` answers the same where the samples store objects holding `x`
    under the name, or lists of them, with and without main's row (`object` or `list`). Fails on main for the
    scatter (it reads both shapes), the media route (it reads the object) and the aggregations (with main's `list`
    row, it reads the list)."""

    async def scenario(r):
        answer, values, vectors, files, _ = DOTTED_READERS[reader]
        root = media_root(r) if files else None

        def objects(_: int) -> list[Any]:
            return [{'x': value} for value in values]

        def lists(_: int) -> list[Any]:
            return [[{'x': value}] for value in values]

        variants = [
            ('stored objects', objects, False),
            ("stored objects and main's row", objects, 'object'),
            ('stored lists', lists, False),
            ("stored lists and main's row", lists, 'list'),
        ]
        await assert_answers(r, name, answer, variants, vectors, root, asked=f'{name}.x')

    regions_world(scenario)


# --- 2. The migration ---

LAST_BEFORE = '0042'  # the newest migration on the main the tests were written against


def new_migrations() -> list[Path]:
    found = sorted(
        (path for path in MIGRATIONS.glob('*.sql') if path.name[:4] > LAST_BEFORE),
        key=lambda p: p.name.encode(),
    )
    if not found:
        pytest.fail(
            f'No migration after {LAST_BEFORE} in backend/db/migrations: the migration that turns registrations of '
            'the five names back into the sample\'s own fields is not here',
        )
    return found


class NamesUpgrade(Upgrade):
    """The #513 harness, with the migrations after LAST_BEFORE as the new ones."""

    def __init__(self, database_: Any, monkeypatch: Any, require_new: bool = True):
        self.new = new_migrations() if require_new else []
        make, psql, _, self._migrate = database_
        old = subprocess.run(
            ['git', '-C', str(ROOT), 'show', f'{RELEASE}:backend/db/schema.sql'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.name = make(old)
        later = sorted(path.name for path in MIGRATIONS.glob('*.sql') if path.name[:4] > LAST_BEFORE)
        apply_before(psql, self.name, later[0][:4] if later else '9999')
        self.url = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{self.name}').geturl()
        self.monkeypatch = monkeypatch
        self.state: dict[str, Any] = {}


@pytest.fixture
def upgrade(database, monkeypatch):
    """upgrade() makes the database before the new migrations; it fails the test when there are none."""
    return lambda require_new=True: NamesUpgrade(database, monkeypatch, require_new)


VALUES, EMPTY, NOTED, PARTS, PROMOTED, MIRROR, DELETING, CONTROL, CARRY_ONLY = range(301, 310)
NAMES = {
    VALUES: 'names-values',
    EMPTY: 'names-empty',
    NOTED: 'names-noted',
    PARTS: 'names-parts',
    PROMOTED: 'names-promoted',
    MIRROR: 'names-mirror',
    DELETING: 'names-deleting',
    CONTROL: 'names-control',
    CARRY_ONLY: 'names-carry-only',
}
SAID, UNIT = 'What the name means here', 'its unit'
PLAIN = (None, None, False, False)  # (description, unit, private mark, promoted)
# The rows planted, in main's shape: (dataset, name) -> (description, unit, private mark, promoted).
ROWS: dict[tuple[int, str], tuple[Optional[str], Optional[str], bool, bool]] = {
    **{(VALUES, name): PLAIN for name in FIVE},
    **{(EMPTY, name): PLAIN for name in FIVE},
    **{(NOTED, name): (SAID, UNIT, True, False) for name in FIVE},
    (PARTS, 'created_at'): (SAID, None, False, False),
    (PARTS, 'id'): (None, UNIT, False, False),
    (PARTS, 'filepath'): (None, None, True, False),
    (PARTS, 'dataset_id'): (SAID, UNIT, False, False),
    (PARTS, 'last_modified_at'): PLAIN,
    (PROMOTED, 'created_at'): (None, None, False, True),
    (PROMOTED, 'dataset_id'): (None, None, False, True),
    **{(MIRROR, name): PLAIN for name in FIVE},
    **{(DELETING, name): PLAIN for name in FIVE},
}
CARRYING = (VALUES, NOTED, PARTS, PROMOTED, MIRROR, DELETING, CARRY_ONLY)  # samples hold stored keys of the five
LEFT_ALONE = (MIRROR, DELETING)
COUNTED = (VALUES, NOTED, PARTS, PROMOTED, CONTROL)
LEFTOVER = {(PROMOTED, name): f'idx_samples_meta_{PROMOTED}_{name}' for name in ('created_at', 'dataset_id')}
CHANGED = {key for key in ROWS if key[0] not in LEFT_ALONE}  # the rows the migration removes or turns back
EVERYTHING = tuple(NAMES)
NOTHING_TO_DO = (MIRROR, DELETING, CONTROL, CARRY_ONLY)


def expected_row(key: tuple[int, str]) -> Optional[tuple]:
    """2.1: the row after the migration, as (field_type, origin, is_promoted, display_type, overlay_config,
    description, unit, values_private), None for a row removed."""
    description, unit, private, _ = ROWS[key]
    if description is None and unit is None and not private:
        return None
    return ('unknown', 'system', False, None, None, description, unit, private)


ROW_OF = (
    'SELECT field_type, origin, is_promoted, display_type, overlay_config, description, unit, values_private '
    'FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2'
)


async def plant_names(w: UpgradeWorld, planted: tuple[int, ...] = EVERYTHING) -> None:
    """The datasets of `planted`, four samples each; in CARRYING ones the first two samples carry stored keys of the
    five; the ROWS of those datasets in main's shape, LEFTOVER indexes; MIRROR a mirror (its rows its primary's),
    VALUES followed, DELETING being deleted; the COUNTED ones' statistics counted."""
    for dataset in planted:
        await w.database.execute('INSERT INTO datasets (id, name) VALUES ($1, $2)', dataset, NAMES[dataset])
        samples = [{'filepath': f'{dataset}-{k}.jpg', 'metadata': {'ok': k, 'kind': f'k{k % 2}'}} for k in range(4)]
        await datasets.add_samples(dataset, {'samples': samples}, user=ADMIN_USER)
    for dataset in set(CARRYING) & set(planted):
        ids = await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id LIMIT 2', dataset)
        for row, value in zip(ids, STORED):
            await w.database.execute(
                'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
                row['id'],
                {name: value for name in FIVE},
            )
    for (dataset, name), (description, unit, private, promoted) in ROWS.items():
        if dataset in planted:
            await main_row(w, dataset, name, description=description, unit=unit, private=private, promoted=promoted)
    for (dataset, name), index in LEFTOVER.items():
        if dataset in planted:
            await w.database.execute(
                f"CREATE INDEX {index} ON samples USING btree ((metadata ->> '{name}')) WHERE dataset_id = {dataset}",
            )
    if MIRROR in planted:
        await w.database.execute(
            'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES ($1, 7, 0)',
            MIRROR,
        )
        await w.database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = $1", MIRROR)
    if VALUES in planted:
        await w.database.execute('UPDATE datasets SET followed = TRUE WHERE id = $1', VALUES)
    for dataset in COUNTED:
        if dataset in planted:
            await counted(w, dataset)
    if DELETING in planted:
        await w.database.execute('UPDATE datasets SET deleting = TRUE WHERE id = $1', DELETING)


# Tables a field removal leaves as they were (2.2), beside the samples.
UNCHANGED = (
    'datasets',
    'dataset_field_stats',
    'dataset_stats_state',
    'jobs',
    'embeddings',
    'sample_hashes',
    'views',
    'mirrors',
    'sample_block_changes',
    'vector_changes',
    'map_block_changes',
)


async def state_of(w: UpgradeWorld) -> dict[str, Any]:
    """Every sample (row version and row), the registry by row, the pending statistics and the change feed by row,
    and the other tables as digests."""
    samples = await w.database.fetch('SELECT id, xmin::text AS version, to_jsonb(s)::text AS row FROM samples s')
    fields = await w.database.fetch(
        "SELECT dataset_id, field_name, to_jsonb(f) - 'id' AS row FROM dataset_fields f "
        'ORDER BY dataset_id, field_name',
    )
    pending = await w.database.fetch(
        'SELECT dataset_id, block_id, transaction_id, field_names FROM dataset_stats_pending',
    )
    changes = await w.database.fetch('SELECT id, dataset_id, kind FROM dataset_changes')
    tables = {}
    for table in UNCHANGED:
        tables[table] = await w.database.fetchval(
            f"SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {table} t",
        )
    return {
        'samples': {row['id']: (row['version'], row['row']) for row in samples},
        'fields': {(row['dataset_id'], row['field_name']): row['row'] for row in fields},
        'pending': {(row['dataset_id'], row['block_id'], row['transaction_id']): row['field_names'] for row in pending},
        'changes': {row['id']: (row['dataset_id'], row['kind']) for row in changes},
        'tables': tables,
    }


def mentions(line: str, dataset: int) -> bool:
    return bool(re.search(rf'(?<![\w-]){dataset}(?![\w-])', line)) or NAMES[dataset] in line


def names_field(line: str, name: str) -> bool:
    return bool(re.search(rf'(?<![\w.]){name}(?!\w)', line))


async def indexes(w: UpgradeWorld) -> set[str]:
    rows = await w.database.fetch(
        'SELECT indexname FROM pg_indexes WHERE indexname = ANY($1::text[])',
        [*LEFTOVER.values()],
    )
    return {row['indexname'] for row in rows}


async def assert_private_from_the_assistant(w: UpgradeWorld, dataset: int, when: str) -> None:
    """The assistant gets the description and unit of `filepath` and `created_at` and none of their values:
    describe_dataset marks them private with no examples and no range, field_values gives no value (or refuses), and
    no answer holds a stored value or a sample's own path."""
    paths = [
        row['filepath'] for row in await w.database.fetch('SELECT filepath FROM samples WHERE dataset_id = $1', dataset)
    ]
    described = await call(ADMIN, 'describe_dataset', {'dataset': dataset, 'limit': 100})
    assert described['ok'], (when, described)
    answers: list[Any] = [described]
    for name in ('filepath', 'created_at'):
        [entry] = [each for each in described['result']['fields'] if each['name'] == name]
        assert (entry['values_private'], entry['description'], entry['unit']) == (True, SAID, UNIT), (when, entry)
        assert not (entry['values'] or {}).get('examples') and not entry.get('range'), (when, entry)
        values = await call(ADMIN, 'field_values', {'dataset': dataset, 'field': name, 'limit': 50})
        assert not values['ok'] or values['result']['values'] == [], (when, values)
        answers.append(values)
    text = json.dumps(answers)
    for value in (*MARKS, *paths):
        assert value not in text, (when, dataset, value)


READ_BEFORE = (VALUES, EMPTY, NOTED, PARTS, PROMOTED, MIRROR, CARRY_ONLY)  # read before the migration


def test_before_the_migration_rows_of_the_five_are_no_field_when_read(upgrade):
    """The state the migration tests start from, before the migration runs: every planted row is there in main's shape
    and the samples of the carrying datasets hold the stored keys. A requirement of 1.1 ("reading counts too") and 3.2
    for rows the migration has not reached yet (a database between the upgrade's two steps, a mirror until its next
    sync): on every planted dataset the field list has `id`, `filepath`, `created_at` and `last_modified_at` once, as
    the sample's own, and `dataset_id` not at all; the value picker offers no stored value; after the one ordinary
    statistics refresh the planting ran, no statistics row of the five holds a stored value and the indexed row reads
    as not indexed; the assistant lists no `dataset_id` and gives no stored value; and the private mark on a row
    still keeps the name's values from the assistant. On main e0f754fe these rows were read as fields: `dataset_id`
    was listed with the stored values, and `created_at`'s statistics and value picker held them."""
    u = upgrade(require_new=False)

    async def scenario(w: UpgradeWorld) -> None:
        await plant_names(w)
        for key, (description, unit, private, promoted) in ROWS.items():
            row = await w.database.fetchrow(
                'SELECT field_type, origin, owner, is_promoted, description, unit, values_private FROM dataset_fields '
                'WHERE dataset_id = $1 AND field_name = $2',
                *key,
            )
            owner = 'primary' if key[0] == MIRROR else 'self'
            assert row is not None and tuple(row) == (
                'string',
                'metadata',
                owner,
                promoted,
                description,
                unit,
                private,
            ), (key, row)
        assert await indexes(w) == set(LEFTOVER.values())
        for dataset in CARRYING:
            rows = await w.database.fetch(
                'SELECT metadata FROM samples WHERE dataset_id = $1 ORDER BY id LIMIT 2',
                dataset,
            )
            for row, value in zip(rows, STORED):
                assert {name: row['metadata'].get(name) for name in FIVE} == dict.fromkeys(FIVE, value), row
        for dataset in READ_BEFORE:
            listed = await assert_read_as_own(w, dataset, FIVE, 'before the migration')
            assert not listed['field-stats'].get('created_at', {}).get('is_promoted'), (dataset, listed['field-stats'])
            await assert_offers_no_stored_value(w, dataset, FIVE, 'before the migration')
        for dataset in COUNTED:
            rows = await w.database.fetch(
                'SELECT field_name, to_jsonb(s)::text AS row FROM dataset_field_stats s '
                'WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
                dataset,
                list(FIVE),
            )
            assert not [row['field_name'] for row in rows if shows_stored(row['row'])], (dataset, rows)
        await open_door(w)
        described = await call(ADMIN, 'describe_dataset', {'dataset': VALUES, 'limit': 100})
        assert described['ok'], described
        assert 'dataset_id' not in [entry['name'] for entry in described['result']['fields']], described
        assert not shows_stored(described), described
        for name in FIVE:
            values = await call(ADMIN, 'field_values', {'dataset': VALUES, 'field': name, 'limit': 50})
            assert not shows_stored(values), (name, values)
        await assert_private_from_the_assistant(w, NOTED, 'before the upgrade')

    u.phase(scenario)


def test_the_migration_turns_the_rows_back_touches_nothing_else_and_says_what_it_did(upgrade):
    """2.1, 2.2, 2.4 and 2.5, requirements. After the upgrade: every planted row of the five, on a dataset that is
    neither a mirror nor being deleted, is gone, or (with a description, a unit or the private mark) the sample's own
    field's row keeping those three; the mirror's and the deleting dataset's rows and every other row are as they
    were. No sample row is written (each keeps its row version), no job is queued, the other tables are as they were,
    the pending statistics gain only rows of block -1 naming the changed fields of the changed datasets, and the change
    feed only `fields` rows for the followed dataset. No index is dropped. A line for each field changed names the
    dataset, the field, what was kept and the index left over; a line for each changed dataset says the values stay in
    the samples' metadata. Run again, it changes nothing and names no planted dataset or index."""
    u = upgrade()

    async def before(w: UpgradeWorld) -> None:
        await plant_names(w)
        u.state['before'] = await state_of(w)

    u.phase(before)
    shown = u.migrate()

    async def after(w: UpgradeWorld) -> None:
        state = await state_of(w)
        earlier = u.state['before']
        for key in ROWS:
            row = await w.database.fetchrow(ROW_OF, *key)
            found = tuple(row) if row is not None else None
            if key in CHANGED:
                assert found == expected_row(key), (key, found)
            else:
                assert state['fields'].get(key) == earlier['fields'][key], (key, 'a left-alone row changed')
        for key, row in earlier['fields'].items():
            if key not in ROWS:
                assert state['fields'].get(key) == row, (key, 'another row changed')
        assert state['samples'] == earlier['samples'], 'a sample row was written'
        for table, digest in earlier['tables'].items():
            assert state['tables'][table] == digest, f'{table} changed'
        for key, names in earlier['pending'].items():
            assert state['pending'].get(key) == names, (key, 'a pending row changed')
        added = {key: names for key, names in state['pending'].items() if key not in earlier['pending']}
        marked: dict[int, set[str]] = {}
        for (dataset, block, _), names in added.items():
            assert block == -1 and names is not None, ('more than a field removal leaves', dataset, block, names)
            marked.setdefault(dataset, set()).update(names)
        expected: dict[int, set[str]] = {}
        for dataset, name in CHANGED:
            expected.setdefault(dataset, set()).add(name)
        assert marked == expected, marked
        new_changes = [change for key, change in state['changes'].items() if key not in earlier['changes']]
        assert new_changes and set(new_changes) == {(VALUES, 'fields')}, new_changes
        assert await indexes(w) == set(LEFTOVER.values()), 'an index was dropped'
        u.state['after'] = state

    u.phase(after)
    for dataset, name in sorted(CHANGED):
        lines = [line for line in shown if mentions(line, dataset) and names_field(line, name)]
        assert lines, f'no line for the field {name} of dataset {dataset}: {shown}'
        description, unit, private, _ = ROWS[(dataset, name)]
        for kept, word in ((description, 'description'), (unit, 'unit'), (private, 'private')):
            if kept:
                assert any(word in line for line in lines), (dataset, name, word, lines)
        index = LEFTOVER.get((dataset, name))
        if index:
            assert any(index in line for line in shown), (index, shown)
    for dataset in sorted({dataset for dataset, _ in CHANGED}):
        stays = [
            line
            for line in shown
            if mentions(line, dataset)
            and 'value' in line
            and 'metadata' in line
            and re.search(r'\b(stay|stays|remain|remains|still|untouched)\b', line)
        ]
        assert stays, f'no line says the values of dataset {dataset} stay in the samples: {shown}'
    said = u.run_again()

    async def again(w: UpgradeWorld) -> None:
        state = await state_of(w)
        assert state == u.state['after'], 'a second run changed something'

    u.phase(again)
    for line in said:
        assert not any(mentions(line, dataset) for dataset in NAMES), (line, said)
        assert not any(index in line for index in LEFTOVER.values()), (line, said)


STATS_OF_OTHERS = (
    "SELECT field_name, to_jsonb(s) - 'last_updated' - 'id' AS row FROM dataset_field_stats s WHERE dataset_id = $1 "
    'AND NOT (field_name = ANY($2::text[])) ORDER BY field_name'
)


def test_after_one_statistics_refresh_each_name_is_the_sample_s_own(upgrade):
    """2.3 (and 1.3's routes, 3.2 and the private mark after the upgrade), requirements. After the upgrade and one
    ordinary statistics refresh of each planted dataset: the field list has `id`, `filepath`, `created_at` and
    `last_modified_at` once, as the sample's own, `dataset_id` not at all, no metadata field of the five; the sidebar's
    values offer no stored value and no statistics row of the five holds one; the other fields' statistics keep their
    values and counts; the stored values are still in the samples; the private mark still keeps `filepath`'s and
    `created_at`'s values from the assistant; the field routes answer for the five as on a dataset that never had
    them; and removing the last stored keys leaves no metadata field and the turned-back rows as they are."""
    u = upgrade()

    async def before(w: UpgradeWorld) -> None:
        await plant_names(w)
        u.state['stats'] = {
            dataset: [tuple(row) for row in await w.database.fetch(STATS_OF_OTHERS, dataset, [*SEVEN])]
            for dataset in COUNTED
        }

    u.phase(before)
    u.migrate()

    async def after(w: UpgradeWorld) -> None:
        for dataset in (VALUES, NOTED, PARTS, PROMOTED):
            await counted(w, dataset)
            await assert_the_samples_own(w, dataset, FIVE, 'after the upgrade and one refresh')
            await assert_offers_no_stored_value(w, dataset, FIVE, 'after the upgrade and one refresh')
            rows = await w.database.fetch(
                'SELECT field_name, to_jsonb(s)::text AS row FROM dataset_field_stats s '
                'WHERE dataset_id = $1 AND field_name = ANY($2::text[])',
                dataset,
                list(FIVE),
            )
            assert not [row['field_name'] for row in rows if shows_stored(row['row'])], (dataset, rows)
            others = [tuple(row) for row in await w.database.fetch(STATS_OF_OTHERS, dataset, [*SEVEN])]
            assert others == u.state['stats'][dataset], (dataset, 'another field\'s statistics changed')
            carried = await w.database.fetch(
                'SELECT metadata FROM samples WHERE dataset_id = $1 ORDER BY id LIMIT 2',
                dataset,
            )
            for row, value in zip(carried, STORED):
                assert {name: row['metadata'].get(name) for name in FIVE} == {name: value for name in FIVE}, row
        listing = (await w.call(ADMIN, 'GET', f'/api/datasets/{PROMOTED}/field-stats')).json()
        [created] = [entry for entry in listing if entry['name'] == 'created_at']
        assert not created.get('is_promoted'), created
        await open_door(w)
        await assert_private_from_the_assistant(w, NOTED, 'after the upgrade')
        for name in FIVE:
            assert await route_answers(w, VALUES, name) == await route_answers(w, CONTROL, name), name
        ids = [
            row['id']
            for row in await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id', NOTED)
        ]
        for name in FIVE:
            await remove_both(w, NOTED, ids[:3], name)
            row = await w.database.fetchrow(ROW_OF, NOTED, name)
            assert row is not None and tuple(row) == expected_row((NOTED, name)), (name, row)
        assert not await registered(w, NOTED)

    u.phase(after)


def test_with_nothing_to_do_the_migration_says_so_and_changes_nothing(upgrade):
    """2.4, a requirement: with rows of the five only on a mirror and on a dataset being deleted, and stored keys with
    no row, the upgrade prints at least one line, names none of those datasets, and changes no row."""
    u = upgrade()

    async def before(w: UpgradeWorld) -> None:
        await plant_names(w, NOTHING_TO_DO)
        u.state['before'] = await state_of(w)

    u.phase(before)
    shown = u.migrate()

    async def after(w: UpgradeWorld) -> None:
        assert await state_of(w) == u.state['before']

    u.phase(after)
    assert shown, 'the migration said nothing'
    for line in shown:
        assert not any(mentions(line, dataset) for dataset in NOTHING_TO_DO), (line, shown)


# --- 4. Words ---


def test_the_docs_say_a_stored_one_is_no_field_and_the_upgrade_notes_name_the_migration():
    """Section 4, lightly: the paragraph of docs/SDK.md that lists the seven names says a stored one stays until it is
    removed and is not a field; the Unreleased upgrade notes name a migration after 0042 and say it changes the field
    list only; a Fixed entry says such a key is no longer listed or no longer a field."""
    blocks = re.split(r'\n\s*\n', (ROOT / 'docs/SDK.md').read_text())
    [listing] = [block for block in blocks if all(re.search(rf'`{name}`', block) for name in SEVEN)]
    flat = ' '.join(listing.split())
    assert re.search(r'\buntil\b[^.]*\bremoved\b', flat), flat
    assert re.search(r'\bnot (a|listed as a) field\b', flat), flat
    changelog = (ROOT / 'CHANGELOG.md').read_text()
    unreleased = changelog.split('\n' + release_heading('2.14.0', changelog), 1)[1].split('\n## ', 1)[0]
    notes = unreleased.split('### Upgrade notes', 1)[-1].split('\n### ', 1)[0]
    flat_notes = ' '.join(notes.split())
    numbers = [int(number) for number in re.findall(r'`?(\d{4})_\w+\.sql`?', flat_notes)]
    assert any(number > int(LAST_BEFORE) for number in numbers), notes
    sentences = re.split(r'(?<=[.;:])\s+', flat_notes)
    assert any(re.search(r'field list', each) and re.search(r'\bonly\b', each) for each in sentences), notes
    fixed = unreleased.split('### Fixed', 1)[-1].split('\n### ', 1)[0]
    entries = [' '.join(entry.split()) for entry in re.split(r'\n- ', fixed)]
    five = r'`(id|dataset_id|filepath|created_at|last_modified_at)`'
    assert any(
        re.search(five, entry) and re.search(r'\b(no longer|not) (listed|a field|registered|shown)\b', entry)
        for entry in entries
    ), fixed
