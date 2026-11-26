"""`tags` and `media_type` are protected metadata keys: refused wherever metadata is written from now on, and removed
from the samples that carry one by a migration. And (the reserved-keys-on-edits contract,
f6-contract-reserved-keys-on-edits) the five older names, `id`, `dataset_id`, `filepath`, `created_at` and
`last_modified_at`, are refused on edits too, while a sample that already carries one of the seven stays editable.

Written from the contract (f5-contract-protected-metadata-keys), not from the code, and tested where it is met: the
HTTP API signed in with real API tokens (the access-rule tests' World, a database of its own per test, as
test_regions_spec and test_backend_four_spec use it); the migration run as an upgrade runs it, by
scripts/apply_migrations.sh, on a database brought to the state before it (the schema of the last release before
this piece, v2.13.2, and every migration up to the new ones), with the keys written into `samples` directly; and the
docs' text. The SDK's side is a case in python/fridge/test_sdk.py, against a running server; sync_between_instances
is tested in test_sync_between_instances_protected_keys.py.

Where the contract is unclear, or main doesn't show what it names, these tests read it this way:
- 1.1: "refused with the sentence the five already get" is main's whole answer for the five when samples are added
  (422, one `value_error` per sample naming its position in the batch, `Value error, metadata key(s) [...] name sample
  columns — set them on the sample itself`, the names sorted). An edit (PATCH, bulk-set by ids, per sample, by a
  filter, in the background) and a rename or clone has no answer of main's to copy: it is read as a 422, or a 400 for
  a field operation (the routes' own refusals are 400s), with the same sentence in its detail, nothing written and no
  job queued. Both names are checked alone and together, with every value kind (text, list, object, null).
- 1.3: the five are pinned where samples are added, in every `on_duplicate` mode and in a batch.
- Reserved keys on edits, 1.1: each of the seven is refused on every edit route with the status the route gives for
  `tags` (read from main after #513: 422 for PATCH, every form of bulk-set and the quality routes, 400 for a rename's
  or clone's new name), the sentence, nothing written and nothing queued. "The quality routes that write a key under
  a name the caller chooses" are uniqueness/compute, representativeness/compute, normalize and duplicates/finalize,
  each asked with an otherwise well-formed request.
- Reserved keys on edits, section 2: "exactly the value the sample already stores" is read as the value the API reads
  back, sent as it was read; the stored key is then compared as the database spells it (1 and 1.0 apart). A number
  sent as 1 for a stored 1.0, or an object with its keys in another order, is not settled and not tested. A sample
  carrying a stored key is written into `samples` directly; Sample.save() on one is driven through the SDK with its
  requests served by the app in the test's own process (a running server can't be given a stored key from outside),
  and the running-server SDK suite checks that adding one of the five by save() or set_values is refused.
- Reserved keys on edits, 2.4: on main the one SDK call that sends a sample's whole metadata back is Sample.save().
  Dataset.add_samples(on_duplicate='update') sends whole metadata too, but adding samples refuses the seven as before
  (1.2). set_values and set_values_per_sample name the key to write and are refused. Whether the per-sample bulk form
  may send a stored value back unchanged is not settled, and no SDK call sends many samples' whole metadata, so it is
  not tested. 3.2 (a bulk edit queued before the upgrade) can't be made through the public surface once the routes
  refuse the key, and 3.4 asks for a report: neither has a test.
- 1.2 and 1.5: a sample that still carries a protected key (written into the database directly) can be edited with
  other keys, and the key can be removed (PATCH, bulk-set by ids, bulk-set by a filter as a job). `@local.tags` on a
  mirror is checked where main writes it, as a mirror's own tags (array_fields and bulk-tag): unchanged.
- 2.1 is compared field by field: every column of every sample (`last_modified_at` to the microsecond) and every
  metadata key but the two top-level ones, nested `tags` inside another key and look-alike keys included; the
  datasets, their fields and statistics, vectors, hashes, selections, saved views and the jobs and change-feed tables
  as whole tables. A row with no such key keeps its row version (xmin): the migration touches only the rows that
  carry a key.
- 2.3 shows on main as rows in `dataset_stats_pending` and `dataset_stats_state` (a statistics recount), `jobs` (a
  job, an index rebuild), and `sample_block_changes`, `vector_changes`, `dataset_changes` and `map_block_changes` (the
  follower change feed, which logs a dataset with `datasets.followed`): one dataset here is followed, and none of
  these tables may change.
- 2.4: an administrator upgrading sees a migration's own output where scripts/apply_migrations.sh shows it: the
  notices that start with the migration's number (`NOTICE:  0042: ...`), printed as indented lines after `Applying
  <file>...`. For each dataset changed, a line names the dataset (its id or its name), the key and the number of
  samples; with nothing to remove, the migration prints at least one such line and changes no row (what it says is not
  asserted further).
- 2.5: a second run is the new migration files run again directly (as the runner runs them, without the ledger row):
  no row changes and no dataset or count is reported. An interruption is simulated as the runner's transaction
  failing at its end: nothing is changed and nothing recorded, and the next upgrade does all of it.
- 2.2: main keeps copies of a sample's metadata in a dataset version's snapshot (`version_snapshots`), which a revert
  writes back, and a revert with `restore_deleted` re-creates deleted samples from it: both are tested. A queued bulk
  edit that sets such a key can't be made through the public surface once the fix refuses it (its job's payload is
  internal), so it has no test. Main keeps no other store of deleted samples to restore from.
- 2.8: main makes such a field with a rename or a clone onto the name (it answers 200), leaving a `dataset_fields` row
  (string, metadata, owner self), the values under that key, and once indexed `is_promoted` and an index
  `idx_samples_meta_<dataset>_<field>`; the code under test refuses that rename, so the state is written with SQL in
  that shape (a test checks that the state is the one the migration is for, and that, as the reserved-names-never-
  fields contract (f7) has it, such a row is no field when read even before the migration). "Counted from
  the samples" is checked after the statistics runner has run once, not forced, as the app's runner runs it: that is
  where the migration's marking must show. "The other fields' statistics untouched" is their values and counts: main's
  runner rewrites every row of a dataset it counts, so the time of the count is left aside. Followers hear of a field
  removal on main as a `dataset_changes` row of kind `fields` for a followed dataset; that is required, and nothing
  more in the change feed. A line "for each field it removed" names the dataset, the field and the word field; the
  index is checked by its name in the output whenever it is still there after the upgrade. The mirror's row stays.
  What was said about the name (description, unit, the private mark) is written on that one row, as main's routes
  leave it whether the mark was set before the rename or after (the same row either way, so the two datasets for the
  two orders hold the same row); afterwards it is checked through the field list, describe_dataset and the
  assistant's field_values (no value of the field in any answer), not through the row's own columns.
- 2.9: a metadata that is a list holding `tags`, the text `tags`, JSON null and SQL NULL, written with the samples
  table's user triggers disabled for the statement and enabled again; the check that the migration fails when a key
  is left after its update can't be set up from outside, and has no test.
- 3.2: current-diff's `modified` list against a version saved before the upgrade names the cleaned sample, also after
  a revert to that version, and is empty against a version saved after.
- The new migration is found as the files after 0041, the last on the main this was written against; with none,
  every migration test fails saying so (the 2.8 test of the state before it runs on the database before it).
- Section 3 is checked lightly: one paragraph or list of the uploader docs (docs/SDK.md, docs/DATASET_MANAGEMENT.md,
  docs/QUICK_START.md) names all seven keys, and CHANGELOG.md's Unreleased upgrade notes name both keys and say the
  migration removes the stored ones.
"""

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, cast
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest
import requests
from fridge.core import client as client_module
from fridge.core.client import Client
from fridge.core.dataset import Dataset
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from backend import database as database_module
from backend import main
from backend.database import PLANNING, Database, init_connection
from backend.routers import datasets, versions
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN, ROLES, World
from backend.tests.test_assistant_conversation_spec import count_statistics
from backend.tests.test_assistant_core_spec import open_door
from backend.tests.test_assistant_views_spec import call
from backend.tests.test_backend_four_spec import KEYED, OLDER, keyed, refused_at, sentence, stored, upload
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_regions_spec import ADMIN_USER, Regions, apply_before, nodes, post, selected
from backend.tests.test_upgrade_paths import ROOT
from scripts.db_test_safety import require_container

database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(
    not (os.getenv('FRIDGE_TEST_POSTGRES_DSN') and os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')),
    reason='Requires disposable PostgreSQL',
)

PROTECTED = ('tags', 'media_type')
SEVEN = (*OLDER, *PROTECTED)  # a sample's own fields: no metadata key may be named like one
VALUES: list[Any] = ['x', ['a', 'b'], {'k': 1}, None]  # every kind of value a key can hold
LOOK_ALIKES = ('source_tags', 'tags_v2', 'media_type_guess', 'created_at_source', 'filepath_original')


async def jobs(r: Regions) -> int:
    return await r.database.fetchval('SELECT count(*) FROM jobs')


async def fields(r: Regions) -> list[tuple]:
    rows = await r.database.fetch(
        'SELECT field_name, field_type, origin FROM dataset_fields WHERE dataset_id = $1 ORDER BY field_name',
        KEYED,
    )
    return [tuple(row.values()) for row in rows]


async def carry(r: Regions, sample_id: int, keys: dict[str, Any]) -> None:
    """Keys written into a sample's metadata directly, as a database from before the rule may hold them."""
    await r.database.execute(
        'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
        sample_id,
        keys,
    )


# --- 1. Refused wherever metadata is written ---


@pytest.mark.parametrize('name', [*OLDER, *PROTECTED])
def test_a_protected_key_is_refused_where_samples_are_added(regions_world, name):
    """For `tags` and `media_type` a requirement (fails on main); for the five main refuses, a pin (passes)."""

    async def scenario(r):
        await keyed(r)
        before = await stored(r)
        for value in VALUES:
            for mode in ('error', 'skip', 'update'):
                for filepath in ('new.jpg', 'k1.jpg'):  # a new sample, and one the dataset has
                    response = await upload(
                        r,
                        [{'filepath': filepath, 'metadata': {name: value, 'ok': 9}}],
                        on_duplicate=mode,
                    )
                    assert (response.status_code, response.json()) == (422, refused_at(0, name)), (
                        value,
                        mode,
                        response.text,
                    )
        # A batch is refused whole, saying which sample.
        batch = [
            {'filepath': 'fine-0.jpg', 'metadata': {'ok': 1}},
            {'filepath': 'bad-1.jpg', 'metadata': {name: 'x'}},
            {'filepath': 'fine-2.jpg', 'metadata': {'ok': 2}},
            {'filepath': 'bad-3.jpg', 'metadata': {name: ['y'], 'ok': 3}},
        ]
        response = await upload(r, batch)
        expected = {'detail': refused_at(1, name)['detail'] + refused_at(3, name)['detail']}
        assert (response.status_code, response.json()) == (422, expected), response.text
        assert await stored(r) == before  # nothing added, nothing changed

    regions_world(scenario)


def test_both_protected_keys_and_one_of_the_five_in_one_sample_are_named_in_one_sentence(regions_world):
    async def scenario(r):
        await keyed(r)
        before = await stored(r)
        for keys in (('tags', 'media_type'), ('media_type', 'created_at'), ('tags', 'id', 'media_type')):
            response = await upload(r, [{'filepath': 'both.jpg', 'metadata': {key: 'x' for key in keys}}])
            assert (response.status_code, response.json()) == (422, refused_at(0, *keys)), response.text
        assert await stored(r) == before

    regions_world(scenario)


@pytest.mark.parametrize('key', SEVEN)
def test_an_edit_that_sets_a_protected_key_is_refused_and_changes_nothing(regions_world, key):
    """Every edit route, with every kind of value: refused with the sentence and the status the routes give for `tags`
    (422), nothing written, nothing queued. For the five older names, the reserved-keys-on-edits contract (1.1)."""

    async def scenario(r):
        ids = await keyed(r)
        before, queued = await stored(r), await jobs(r)
        for value in VALUES:
            for method, path, body in (
                ('PATCH', f'samples/{ids[0]}', {'metadata': {key: value}}),
                ('PATCH', f'samples/{ids[0]}', {'metadata': {key: value, 'ok': 7}, 'tags': ['changed']}),
                ('POST', 'samples/bulk-set', {'sample_ids': ids[:2], 'metadata': {key: value}}),
                ('POST', 'samples/bulk-set', {'sample_ids': ids[:2], 'metadata': {key: value, 'ok': 7}}),
                ('POST', 'samples/bulk-set', {'updates': [{'sample_id': ids[1], 'metadata': {key: value}}]}),
                (
                    'POST',
                    'samples/bulk-set',
                    {
                        'updates': [
                            {'sample_id': ids[0], 'metadata': {'ok': 7}},
                            {'sample_id': ids[1], 'metadata': {key: value}},
                        ],
                    },
                ),
                ('POST', 'samples/bulk-set', {'filters': {}, 'metadata': {key: value}}),
                (
                    'POST',
                    'samples/bulk-set',
                    {'filters': {'field': 'ok', 'operator': '=', 'value': 1}, 'metadata': {key: value}},
                ),
                ('POST', 'samples/bulk-set', {'sample_ids': ids, 'metadata': {key: value}, 'background': True}),
            ):
                response = await r.call(ADMIN, method, f'/api/datasets/{KEYED}/{path}', json=body)
                where = (method, path, body, response.status_code, response.text)
                assert response.status_code == 422, where
                assert sentence(key) in json.dumps(response.json()['detail'], ensure_ascii=False), where
                assert await stored(r) == before, where  # nothing changed
                assert await jobs(r) == queued, where  # nothing queued

    regions_world(scenario)


@pytest.mark.parametrize('key', SEVEN)
def test_a_protected_key_is_refused_as_the_new_name_of_a_field(regions_world, key):
    """A rename's or a clone's new name (inline, and in the background as a job): refused with the sentence and the
    status the routes give for `tags` (400), the field and its values as they were, no job queued."""

    async def scenario(r):
        await keyed(r)
        before, defined, queued = await stored(r), await fields(r), await jobs(r)
        for operation in ('rename', 'clone'):
            for body in (
                {'new_name': key},
                {'new_name': key, 'overwrite': True},
                {'new_name': key, 'background': True},
            ):
                response = await post(r, f'/api/datasets/{KEYED}/fields/ok/{operation}', body)
                where = (operation, body, response.status_code, response.text)
                assert response.status_code == 400, where
                assert sentence(key) in json.dumps(response.json()['detail'], ensure_ascii=False), where
                assert (await stored(r), await fields(r), await jobs(r)) == (before, defined, queued), where

    regions_world(scenario)


@pytest.mark.parametrize('key', SEVEN)
def test_a_protected_key_is_refused_as_the_key_a_quality_route_writes(regions_world, key):
    """The quality routes that write a score or a group id under a key the caller names (uniqueness, representativeness,
    their normalisation, the near-duplicate groups): refused with the sentence and the status they give for `tags`
    (422), each request otherwise well formed, nothing written and nothing queued."""

    async def scenario(r):
        await keyed(r)
        before, queued = await stored(r), await jobs(r)
        for path, body in (
            ('quality/uniqueness/compute', {'field': 'clip', 'metadata_field': key}),
            ('quality/representativeness/compute', {'field': 'clip', 'centroid': '[1,0]', 'metadata_field': key}),
            ('quality/normalize', {'metadata_field': key, 'lo': 0, 'span': 1}),
            ('quality/duplicates/finalize', {'token': 'some-token', 'group_field': key}),
        ):
            response = await post(r, f'/api/datasets/{KEYED}/{path}', body)
            where = (path, response.status_code, response.text)
            assert response.status_code == 422, where
            assert sentence(key) in json.dumps(response.json()['detail'], ensure_ascii=False), where
            assert (await stored(r), await jobs(r)) == (before, queued), where

    regions_world(scenario)


def test_several_names_in_one_edit_are_named_together_in_one_sentence(regions_world):
    """Contract 1.3 of the reserved-keys piece: several of the seven in one edit are named in one sentence, sorted."""

    async def scenario(r):
        ids = await keyed(r)
        before = await stored(r)
        for keys in (('created_at', 'tags'), ('id', 'filepath', 'media_type'), ('last_modified_at', 'dataset_id')):
            for method, path, body in (
                ('PATCH', f'samples/{ids[0]}', {'metadata': {key: 'x' for key in keys}}),
                ('POST', 'samples/bulk-set', {'sample_ids': ids, 'metadata': {key: 'x' for key in keys}}),
                (
                    'POST',
                    'samples/bulk-set',
                    {'updates': [{'sample_id': ids[1], 'metadata': {key: 1 for key in keys}}]},
                ),
            ):
                response = await r.call(ADMIN, method, f'/api/datasets/{KEYED}/{path}', json=body)
                where = (method, path, keys, response.status_code, response.text)
                assert response.status_code == 422, where
                assert sentence(*keys) in json.dumps(response.json()['detail'], ensure_ascii=False), where
        assert await stored(r) == before

    regions_world(scenario)


def test_a_sample_that_still_carries_a_protected_key_can_be_edited_and_the_key_removed(regions_world):
    """Contract 1.2 (passes on main): removing is not setting. A sample that still carries such a key (written into
    the database directly) takes an edit of other keys, which keeps it, and the key is removed by PATCH and by both
    bulk forms."""

    async def scenario(r):
        ids = await keyed(r)
        first, second, third = ids
        for sample_id in ids:
            await carry(r, sample_id, {'tags': 'legacy', 'media_type': ['legacy']})
        carried = {'tags': 'legacy', 'media_type': ['legacy']}

        async def metadata(sample_id: int) -> dict[str, Any]:
            return await r.database.fetchval('SELECT metadata FROM samples WHERE id = $1', sample_id)

        response = await r.call(ADMIN, 'PATCH', f'/api/datasets/{KEYED}/samples/{first}', json={'metadata': {'ok': 5}})
        assert response.status_code == 200, response.text
        assert await metadata(first) == {**carried, 'ok': 5}
        response = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{KEYED}/samples/{first}',
            json={'remove_metadata_keys': ['tags']},
        )
        assert response.status_code == 200, response.text
        assert await metadata(first) == {'media_type': ['legacy'], 'ok': 5}
        response = await post(
            r,
            f'/api/datasets/{KEYED}/samples/bulk-set',
            {'sample_ids': [first, second], 'remove_metadata_keys': ['media_type']},
        )
        assert response.status_code == 200, response.text
        assert (await metadata(first), await metadata(second)) == ({'ok': 5}, {'tags': 'legacy', 'ok': 1})
        node = {'field': 'ok', 'operator': '=', 'value': 2}
        response = await post(
            r,
            f'/api/datasets/{KEYED}/samples/bulk-set',
            {'filters': node, 'remove_metadata_keys': ['tags', 'media_type']},
        )
        assert response.status_code in (200, 202), response.text
        if response.status_code == 202:
            await finish(r.database)
        assert await metadata(third) == {'ok': 2}

    regions_world(scenario)


def test_heading_stays_accepted_in_metadata(regions_world):
    """Contract 1.4, a pin: importers put a compass heading in metadata on purpose."""

    async def scenario(r):
        ids = await keyed(r)
        response = await upload(r, [{'filepath': 'h.jpg', 'metadata': {'heading': 12.5}}])
        assert response.status_code == 200, response.text
        heading = await r.database.fetchval(
            "SELECT metadata->'heading' FROM samples WHERE dataset_id = $1 AND filepath = 'h.jpg'",
            KEYED,
        )
        assert heading == 12.5
        response = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{KEYED}/samples/{ids[0]}',
            json={'metadata': {'heading': 270}},
        )
        assert response.status_code == 200, response.text
        response = await post(
            r,
            f'/api/datasets/{KEYED}/samples/bulk-set',
            {'sample_ids': ids[1:], 'metadata': {'heading': 90}},
        )
        assert response.status_code == 200, response.text
        found = await r.database.fetch(
            "SELECT metadata->'heading' AS h FROM samples WHERE id = ANY($1) ORDER BY id",
            ids,
        )
        assert [row['h'] for row in found] == [270, 90, 90]

    regions_world(scenario)


def test_keys_that_only_contain_a_protected_name_are_ordinary_keys(regions_world):
    """Contract 1.5 (and 1.4 of the reserved-keys piece), a pin: `source_tags`, `tags_v2`, `media_type_guess`,
    `created_at_source` and `filepath_original` are written, become fields and filter."""

    async def scenario(r):
        ids = await keyed(r)
        response = await upload(
            r,
            [{'filepath': f'l{k}.jpg', 'metadata': {name: f'v{k}' for name in LOOK_ALIKES}} for k in range(2)],
        )
        assert response.status_code == 200, response.text
        response = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{KEYED}/samples/{ids[0]}',
            json={'metadata': {'tags_v2': 'edited'}},
        )
        assert response.status_code == 200, response.text
        response = await post(
            r,
            f'/api/datasets/{KEYED}/samples/bulk-set',
            {'sample_ids': ids[1:], 'metadata': {'media_type_guess': 'bulk'}},
        )
        assert response.status_code == 200, response.text
        response = await post(r, f'/api/datasets/{KEYED}/fields/ok/clone', {'new_name': 'source_tags_copy'})
        assert response.status_code == 200, response.text
        schema = await r.call(ADMIN, 'GET', f'/api/datasets/{KEYED}/schema')
        names = {field['name'] for field in schema.json()}
        assert {*LOOK_ALIKES, 'source_tags_copy'} <= names, names
        lone = await r.database.fetchval("SELECT id FROM samples WHERE dataset_id = $1 AND filepath = 'l1.jpg'", KEYED)
        assert await selected(r, {'field': 'tags_v2', 'operator': '=', 'value': 'v1'}, KEYED) == {lone}
        assert await selected(
            r,
            {'logic': 'AND', 'filters': await nodes(r, 'media_type_guess = "bulk"', KEYED)},
            KEYED,
        ) == set(ids[1:])

    regions_world(scenario)


def test_a_mirrors_own_local_tags_are_written_as_before(regions_world):
    """Contract 1.5, a pin: a mirror's own `@local.tags` is written where main writes it, as its own tags."""

    async def scenario(r):
        ids = await keyed(r)
        await r.database.execute(
            'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES ($1, 7, 0)',
            KEYED,
        )
        await r.database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = $1", KEYED)
        response = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{KEYED}/samples/{ids[0]}',
            json={'array_fields': {'@local.tags': ['checked']}},
        )
        assert response.status_code == 200, response.text
        response = await post(
            r,
            f'/api/datasets/{KEYED}/samples/bulk-tag',
            {'sample_ids': ids[1:], 'field_name': '@local.tags', 'add_tags': ['seen']},
        )
        assert response.status_code in (200, 202), response.text
        if response.status_code == 202:
            await finish(r.database)
        rows = await r.database.fetch('SELECT local_tags FROM samples WHERE id = ANY($1) ORDER BY id', ids)
        assert [row['local_tags'] for row in rows] == [['checked'], ['seen'], ['seen']]
        # Its own fields named like a sample's own (`@local.created_at`, `@local.id`) are ordinary keys of its own.
        response = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{KEYED}/samples/{ids[0]}',
            json={'metadata': {'@local.created_at': 'its own', '@local.id': 7}},
        )
        assert response.status_code == 200, response.text
        local = await r.database.fetchval('SELECT local_metadata FROM samples WHERE id = $1', ids[0])
        assert local == {'created_at': 'its own', 'id': 7}, local

    regions_world(scenario)


# --- A sample that already carries one stays editable (the reserved-keys-on-edits contract, section 2) ---

EDITED = 60
# The values a stored key holds here: text, a number written 1 and written 1.0, a date, a list, an object, JSON null.
STORED: list[Any] = ['an old value', 1, 1.0, '1999-03-04T05:06:07Z', ['a', 1], {'b': 1, 'a': [1, 2]}, None]


async def carrying(r: Regions, name: str) -> list[int]:
    """A dataset with one sample per value of STORED, each carrying `name` with that value (written into `samples`
    directly, as only an old database or a path outside the routes holds it), its own tag `own` and a key `ok`, and
    one sample last with no such key. Returns the ids in that order."""
    await r.dataset(EDITED)
    samples = [{'filepath': f'c{k}.jpg', 'tags': ['own'], 'metadata': {'ok': k}} for k in range(len(STORED) + 1)]
    await datasets.add_samples(EDITED, {'samples': samples}, user=ADMIN_USER)
    by_path = {
        row['filepath']: row['id']
        for row in await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', EDITED)
    }
    ids = [by_path[f'c{k}.jpg'] for k in range(len(STORED) + 1)]
    for sample_id, value in zip(ids, STORED):
        await r.database.execute(
            'UPDATE samples SET metadata = metadata || jsonb_build_object($2::text, $3::jsonb) WHERE id = $1',
            sample_id,
            name,
            value,
        )
    return ids


async def read(r: Regions, sample_id: int) -> dict[str, Any]:
    """The sample's metadata as the API reads it."""
    node = {'field': 'id', 'operator': 'in', 'value': [sample_id]}
    response = await post(r, f'/api/datasets/{EDITED}/samples/query', {'filters': node, 'limit': 1})
    assert response.status_code == 200, response.text
    [item] = response.json()['items']
    return item['metadata']


async def held(r: Regions, sample_id: int) -> tuple:
    """What the database holds of the sample: its own fields, its metadata as text, and last_modified_at."""
    row = await r.database.fetchrow(
        'SELECT id, dataset_id, filepath, media_type, tags, created_at, metadata::text AS metadata, last_modified_at '
        'FROM samples WHERE id = $1',
        sample_id,
    )
    return tuple(row.values())


async def value_held(r: Regions, sample_id: int, name: str) -> Optional[str]:
    """The stored key's value as the database spells it (1 and 1.0 apart), None when the key is gone."""
    return await r.database.fetchval('SELECT (metadata -> $2)::text FROM samples WHERE id = $1', sample_id, name)


@pytest.mark.parametrize('name', SEVEN)
def test_an_edit_that_sends_a_stored_reserved_key_back_unchanged_goes_through(regions_world, name):
    """Sections 2.1 to 2.3 and 2.5 (a requirement). Read "exactly the value stored" as the value the API reads back,
    sent as it was read: the edit goes through, the other key changes, and the stored key (spelled as the database
    holds it, 1 and 1.0 apart) and the sample's own fields stay as they were. A number sent as 1 for a stored 1.0, or
    an object with its keys in another order, is not settled by the contract and is not tested. A different value is
    refused with the sentence and the status PATCH gives for `tags` (422), nothing written; so is the key added to a
    sample without it; removing it goes through, alone and in bulk."""

    async def scenario(r):
        ids = await carrying(r, name)
        for index, (sample_id, value) in enumerate(zip(ids, STORED)):
            stored_value = await value_held(r, sample_id, name)
            before = await held(r, sample_id)
            metadata = await read(r, sample_id)
            assert metadata[name] == value, (name, metadata)
            response = await r.call(
                ADMIN,
                'PATCH',
                f'/api/datasets/{EDITED}/samples/{sample_id}',
                json={'metadata': {**metadata, 'ok': 100 + index}},
            )
            where = (name, value, response.status_code, response.text)
            assert response.status_code == 200, where
            after = await held(r, sample_id)
            assert after[:6] == before[:6], where  # the sample's own fields
            assert await value_held(r, sample_id, name) == stored_value, where
            assert (await read(r, sample_id))['ok'] == 100 + index, where
            # A different value for it is refused, and nothing is written.
            other = 'something else' if value != 'something else' else 'another thing'
            before = await held(r, sample_id)
            metadata = await read(r, sample_id)
            response = await r.call(
                ADMIN,
                'PATCH',
                f'/api/datasets/{EDITED}/samples/{sample_id}',
                json={'metadata': {**metadata, name: other, 'ok': -1}},
            )
            where = (name, value, response.status_code, response.text)
            assert response.status_code == 422, where
            assert sentence(name) in json.dumps(response.json()['detail'], ensure_ascii=False), where
            assert await held(r, sample_id) == before, where
        # Added to a sample that has none: refused.
        lone = ids[-1]
        before = await held(r, lone)
        response = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{EDITED}/samples/{lone}',
            json={'metadata': {name: 'x'}},
        )
        assert response.status_code == 422, response.text
        assert await held(r, lone) == before
        # Removed: alone, and in bulk.
        response = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{EDITED}/samples/{ids[0]}',
            json={'remove_metadata_keys': [name]},
        )
        assert response.status_code == 200, response.text
        response = await post(
            r,
            f'/api/datasets/{EDITED}/samples/bulk-set',
            {'sample_ids': ids[1:3], 'remove_metadata_keys': [name]},
        )
        assert response.status_code == 200, response.text
        for sample_id in ids[:3]:
            assert await value_held(r, sample_id, name) is None, sample_id
        assert await value_held(r, ids[3], name) is not None

    regions_world(scenario)


class AsgiAdapter(BaseAdapter):
    """The SDK's requests, served by the app in this test's event loop: each is handed to the loop from the SDK's
    thread and answered there, as a running server would answer it."""

    def __init__(self, loop: asyncio.AbstractEventLoop, client: httpx.AsyncClient):
        super().__init__()
        self.loop, self.client = loop, client

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # type: ignore[override]
        coroutine = self.client.request(
            request.method,
            request.url,
            headers=dict(request.headers),
            content=request.body,
        )
        answer = asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=120)
        response = requests.Response()
        response.status_code = answer.status_code
        response.headers = CaseInsensitiveDict(answer.headers)
        response._content = answer.content
        response.url = str(request.url)
        response.reason = answer.reason_phrase
        response.request = request
        response.encoding = answer.encoding or 'utf-8'
        return response

    def close(self) -> None:
        pass


def sdk(r: Regions, loop: asyncio.AbstractEventLoop, monkeypatch: Any) -> None:
    """The SDK's global client, signed in with the admin's API token, its requests served by the app."""
    client = Client('http://test/api', api_token=r.tokens[ADMIN])
    client.session.mount('http://test', AsgiAdapter(loop, r.client))
    monkeypatch.setattr(client_module, '_client', client)


def refused_by(call: Callable[[], Any], name: str) -> None:
    """The SDK call raises the server's refusal of `name`, with its sentence."""
    try:
        call()
    except requests.HTTPError as error:
        assert error.response is not None and error.response.status_code == 422, error
        assert sentence(name) in str(error), error
    else:
        raise AssertionError(f'the SDK call was accepted with a metadata key named {name!r}')


@pytest.mark.parametrize('name', SEVEN)
def test_sample_save_keeps_working_on_a_sample_that_carries_a_stored_reserved_key(regions_world, monkeypatch, name):
    """Sections 2.1, 2.2, 2.4 and 5.2 through the SDK, its requests served by the app in-process (a running server
    can't be given a stored key from outside: it is written into `samples` here). Sample.save() sends the sample's
    whole metadata back: with another key changed it goes through and the stored key stays; with the stored key's
    value changed it raises the server's sentence and nothing changes; with the key deleted from the sample's
    metadata it goes through and the key is gone. The SDK's bulk forms that name the key to write on many samples,
    set_values and set_values_per_sample, are refused with the sentence."""

    async def scenario(r):
        ids = await carrying(r, name)
        date = STORED.index('1999-03-04T05:06:07Z')
        sample_id, filepath = ids[date], f'c{date}.jpg'
        stored_value = await value_held(r, sample_id, name)
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def another_key_changed() -> None:
            ds = Dataset(f'dataset-{EDITED}')
            sample = ds[filepath]
            sample.metadata['ok'] = 55
            sample.save()

        await asyncio.to_thread(another_key_changed)
        assert await value_held(r, sample_id, name) == stored_value
        assert (await read(r, sample_id))['ok'] == 55
        before = await held(r, sample_id)

        def the_stored_value_changed() -> None:
            ds = Dataset(f'dataset-{EDITED}')
            sample = ds[filepath]
            sample.metadata[name] = 'changed'
            refused_by(sample.save, name)
            refused_by(lambda: ds.set_values([sample_id, ids[-1]], name, 'x'), name)
            refused_by(lambda: ds.set_values_per_sample(name, {sample_id: 'x', ids[-1]: 'y'}), name)

        await asyncio.to_thread(the_stored_value_changed)
        assert await held(r, sample_id) == before

        def the_key_deleted() -> None:
            ds = Dataset(f'dataset-{EDITED}')
            sample = ds[filepath]
            del sample.metadata[name]
            sample.save()

        await asyncio.to_thread(the_key_deleted)
        assert await value_held(r, sample_id, name) is None
        assert (await read(r, sample_id))['ok'] == 55

    regions_world(scenario)


def test_a_sample_that_carries_a_stored_reserved_key_reads_filters_and_lists_as_before(regions_world):
    """Section 2.6, a pin (passes on main): a sample with a stored `created_at` and `id` reads them back in its
    metadata, a filter on `created_at` reads the sample's own column, and the field list names `created_at` once,
    the sample's own field."""

    async def scenario(r):
        ids = await carrying(r, 'created_at')
        await carry(r, ids[0], {'id': 1.0})
        metadata = await read(r, ids[0])
        assert (metadata['created_at'], metadata['id']) == ('an old value', 1.0), metadata
        node = {'field': 'created_at', 'operator': '>=', 'value': '2001-01-01T00:00:00Z'}
        assert await selected(r, node, EDITED) == set(ids)  # by the samples' own dates, not the stored text
        schema = (await r.call(ADMIN, 'GET', f'/api/datasets/{EDITED}/schema')).json()
        created = [entry for entry in schema if entry['name'] == 'created_at']
        assert len(created) == 1 and created[0]['type'] == 'datetime', created
        assert 'id' in [entry['name'] for entry in schema]

    regions_world(scenario)


def test_a_version_revert_restores_a_stored_reserved_key_as_the_version_holds_it(regions_world):
    """Section 3.1, a pin (passes on main since #513): a revert restores a sample's metadata as the version holds it,
    a stored `created_at` key included; only `tags` and `media_type` are left out."""

    async def scenario(r):
        ids = await carrying(r, 'created_at')
        await carry(r, ids[0], {'tags': 'old', 'media_type': 'old'})
        made = await post(r, f'/api/datasets/{EDITED}/versions', {'name': 'with the stored keys'})
        assert made.status_code == 200, made.text
        removed = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{EDITED}/samples/{ids[0]}',
            json={'remove_metadata_keys': ['created_at', 'tags', 'media_type']},
        )
        assert removed.status_code == 200, removed.text
        reverted = await post(r, f'/api/datasets/{EDITED}/versions/{made.json()["id"]}/revert', {})
        assert reverted.status_code == 200, reverted.text
        metadata = await r.database.fetchval('SELECT metadata FROM samples WHERE id = $1', ids[0])
        assert metadata == {'ok': 0, 'created_at': 'an old value'}, metadata

    regions_world(scenario)


# --- 2. The migration ---

RELEASE = 'v2.13.2'  # the last release before this piece
LAST_BEFORE = '0041'  # the newest migration on the main the tests were written against
MIGRATIONS = ROOT / 'backend/db/migrations'
FARM, OBJECTS, NONE, FEW, VERSIONED = 101, 102, 103, 104, 105
NAMES = {FARM: 'keys-farm', OBJECTS: 'keys-objects', NONE: 'keys-none', FEW: 'keys-few', VERSIONED: 'keys-versioned'}
# Tables whose rows the migration must leave as they were (2.1, 2.3): what a sample is beside its own row, and where a
# statistics recount, a job, an index rebuild or a follower change feed entry would show.
UNTOUCHED = (
    'datasets',
    'dataset_fields',
    'dataset_field_stats',
    'dataset_stats_pending',
    'dataset_stats_state',
    'embeddings',
    'sample_hashes',
    'region_selections',
    'bulk_selection_pins',
    'views',
    'jobs',
    'sample_block_changes',
    'vector_changes',
    'dataset_changes',
    'map_block_changes',
)


def newer() -> bool:
    return any(path.name[:4] > LAST_BEFORE for path in MIGRATIONS.glob('*.sql'))


def new_migrations() -> list[Path]:
    found = sorted(
        (path for path in MIGRATIONS.glob('*.sql') if path.name[:4] > LAST_BEFORE),
        key=lambda p: p.name.encode(),
    )
    if not found:
        pytest.fail(
            f'No migration after {LAST_BEFORE} in backend/db/migrations: the migration that removes stored `tags` and '
            '`media_type` metadata keys is not here',
        )
    return found


class UpgradeWorld(World):
    """The access-rule tests' World, knowing its database's address (the statistics runner's heartbeat needs it)."""

    def __init__(self, database_: Any, client: httpx.AsyncClient, url: str):
        super().__init__(database_, client)
        self.url = url


class Upgrade:
    """A database in the state before the new migrations (the release's schema and every migration before them),
    phases run on it with the app pointed at it, and the upgrade run as scripts/apply_migrations.sh runs it. Without
    `require_new`, on a checkout with no new migration, the database has every migration (the state on main)."""

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
        first = self.new[0].name[:4] if self.new else (new_migrations()[0].name[:4] if newer() else '9999')
        apply_before(psql, self.name, first)
        self.url = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{self.name}').geturl()
        self.monkeypatch = monkeypatch
        self.state: dict[str, Any] = {}

    def phase(self, scenario: Callable[[UpgradeWorld], Awaitable[None]]) -> None:
        """The app on this database, the access-rule users signed in, for one scenario."""
        mp = self.monkeypatch
        mp.setattr(database_module, 'DATABASE_URL', self.url)
        mp.setattr(datasets, '_array_columns_cache', None)
        mp.setattr(datasets, 'require_free_space', AsyncMock())
        mp.setattr(datasets, 'validate_embedding_dims', AsyncMock())
        mp.setattr(versions, 'require_free_space', AsyncMock())
        mp.setattr(versions, 'estimate_snapshot_bytes', AsyncMock(return_value=0))

        async def body() -> None:
            pool = await asyncpg.create_pool(
                self.url,
                min_size=1,
                max_size=8,
                init=init_connection,
                server_settings={'statement_timeout': '30000', **PLANNING},
            )
            mp.setattr(database_module.db, 'pool', pool)
            database_ = Database()
            database_.pool = pool
            try:
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                    w = UpgradeWorld(database_, client, self.url)
                    for user_id, role in ROLES.items():
                        await w.user(user_id, role)
                    await scenario(w)
            finally:
                await pool.close()

        asyncio.run(body())

    def migrate(self) -> list[str]:
        """The upgrade, as scripts/apply_migrations.sh runs it; returns what it shows of the new migrations."""
        result = self._migrate(self.name)
        assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
        shown, inside = [], False
        names = {path.name for path in self.new}
        for line in result.stdout.splitlines():
            if line.startswith('Applying '):
                inside = line[len('Applying ') :].rstrip('.') in names
            elif inside and line.startswith('  '):
                shown.append(line.strip())
            elif not line.startswith('  '):
                inside = False
        return shown

    def run_again(self, tail: str = '') -> list[str]:
        """The new migration files run again directly, each in one transaction as the runner runs it (no ledger row),
        `tail` appended; returns their notices that start with their number."""
        container = require_container(os.environ['FRIDGE_TEST_POSTGRES_CONTAINER'])
        user = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN']).username or 'freedomridge'
        said = []
        for path in self.new:
            done = subprocess.run(
                ['docker', 'exec', '-i', container, 'psql', '-X', '-U', user, '-d', self.name]
                + ['-v', 'ON_ERROR_STOP=1', '--single-transaction', '-q'],
                input=f'{path.read_text()}\n{tail}\n',
                capture_output=True,
                text=True,
            )
            if tail:
                assert done.returncode != 0, f'{path.name} ran to the end although it was made to fail'
            else:
                assert done.returncode == 0, f'{path.name} failed when run again: {done.stderr}'
            number = f' {path.name[:4]}: '
            said += [line.split(number, 1)[1] for line in done.stderr.splitlines() if number in line]
        return said


@pytest.fixture
def upgrade(database, monkeypatch):
    """upgrade() makes the database before the new migrations; it fails the test when there are none."""
    return lambda require_new=True: Upgrade(database, monkeypatch, require_new)


async def snapshot(w: World) -> dict[str, Any]:
    """Every sample (its row version, its columns but metadata, its metadata) and every untouched table's rows."""
    rows = await w.database.fetch(
        "SELECT id, xmin::text AS version, to_jsonb(s) - 'metadata' AS rest, metadata FROM samples s ORDER BY id",
    )
    tables = {}
    for table in UNTOUCHED:
        tables[table] = await w.database.fetchval(
            f"SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {table} t",
        )
    return {
        'samples': {row['id']: (row['version'], row['rest'], row['metadata']) for row in rows},
        'tables': tables,
    }


def without(metadata: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The metadata with its top-level `tags` and `media_type` keys left out."""
    return None if metadata is None else {key: value for key, value in metadata.items() if key not in PROTECTED}


# The samples planted before the migration: (dataset, plot, media type, own tags, location, keys written directly).
PLANTED: list[tuple[int, str, str, list[str], Optional[list[float]], dict[str, Any]]] = [
    (FARM, 'f0', 'image', ['reviewed'], [1.0, 2.0], {'tags': 'night'}),
    (FARM, 'f1', 'image', [], [1.5, 2.5], {'tags': ['a', 'b']}),
    (FARM, 'f2', 'image', ['x', 'y'], None, {'tags': {'k': 1}, 'media_type': None}),
    (FARM, 'f3', 'image', [], [3.0, 4.0], {'media_type': ['video', {'x': 2}]}),
    (FARM, 'f4', 'image', [], [3.5, 4.5], {'info': {'tags': 'keep', 'media_type': 'keep'}}),  # nested: kept
    (FARM, 'f5', 'image', [], None, {'source_tags': 'keep', 'tags_v2': 1, 'media_type_guess': 'x'}),  # look-alikes
    (FARM, 'f6', 'image', [], None, {}),
    (OBJECTS, 'i0', 'image', [], [5.0, 5.0], {}),
    (OBJECTS, 'i1', 'image', [], [6.0, 6.0], {'media_type': 'image'}),
    (OBJECTS, 'i2', 'image', [], None, {}),
    (NONE, 'n0', 'image', ['plain'], [7.0, 7.0], {}),
    (NONE, 'n1', 'image', [], None, {}),
    (FEW, 'w0', 'image', [], None, {'media_type': {'nested': {'tags': 1}}}),
    (FEW, 'w1', 'image', [], None, {}),
]
# The objects under OBJECTS' images: (plot, its image's plot, keys written directly).
PLANTED_OBJECTS: list[tuple[str, str, dict[str, Any]]] = [
    ('o0', 'i0', {'tags': 'box'}),
    ('o1', 'i1', {'tags': ['x']}),
    ('o2', 'i2', {}),
]


def removed_counts() -> dict[tuple[int, str], int]:
    """(dataset, key): how many samples the migration takes the key from."""
    counts: dict[tuple[int, str], int] = {}
    rows = [(dataset, keys) for dataset, _, _, _, _, keys in PLANTED]
    rows += [(OBJECTS, keys) for _, _, keys in PLANTED_OBJECTS]
    for dataset, keys in rows:
        for key in PROTECTED:
            if key in keys:
                counts[(dataset, key)] = counts.get((dataset, key), 0) + 1
    return counts


async def plant(w: World) -> None:
    """The datasets of PLANTED (FARM followed by a follower, so its changes are logged), with vectors, a selection
    and a saved view, and the keys written into `samples` directly."""
    for dataset in (FARM, OBJECTS, NONE, FEW):
        await w.database.execute('INSERT INTO datasets (id, name) VALUES ($1, $2)', dataset, NAMES[dataset])
        samples = [
            {
                'filepath': f'{plot}.jpg',
                'media_type': kind,
                'tags': tags,
                'metadata': {'plot': plot, 'n': index},
                **({'location': {'type': 'Point', 'coordinates': point}} if point else {}),
            }
            for index, (owner, plot, kind, tags, point, _) in enumerate(PLANTED)
            if owner == dataset
        ]
        await datasets.add_samples(dataset, {'samples': samples}, user=ADMIN_USER)
    ids = {
        row['plot']: row['id'] for row in await w.database.fetch("SELECT id, metadata->>'plot' AS plot FROM samples")
    }
    children = [
        {'filepath': f'{plot}.jpg', 'media_type': 'object', 'metadata': {'plot': plot, 'parent_id': ids[parent]}}
        for plot, parent, _ in PLANTED_OBJECTS
    ]
    await datasets.add_samples(OBJECTS, {'samples': children}, user=ADMIN_USER)
    ids = {
        row['plot']: row['id'] for row in await w.database.fetch("SELECT id, metadata->>'plot' AS plot FROM samples")
    }
    vectors = [
        {'sample_id': ids[plot], 'vector': [1.0, index / 10, 0.0], 'field_name': 'clip'}
        for index, (owner, plot, *_rest) in enumerate(PLANTED)
        if owner == FARM
    ]
    response = await w.call(ADMIN, 'POST', f'/api/datasets/{FARM}/embeddings', json=vectors)
    assert response.is_success, response.text
    token = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{FARM}/selection_token',
        json={'filters': {'field': 'plot', 'operator': 'in', 'value': ['f0', 'f2', 'f4']}},
    )
    assert token.status_code == 200, token.text
    view = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{FARM}/views',
        json={'name': 'some', 'content': {'filters': {'field': 'plot', 'operator': '=', 'value': 'f1'}}},
    )
    assert view.status_code == 200, view.text
    for _, plot, _, _, _, keys in PLANTED:
        if keys:
            await w.database.execute(
                'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
                ids[plot],
                keys,
            )
    for plot, _, keys in PLANTED_OBJECTS:
        if keys:
            await w.database.execute(
                'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
                ids[plot],
                keys,
            )
    await w.database.execute('UPDATE datasets SET followed = TRUE WHERE id = $1', FARM)
    carrying = await w.database.fetchval("SELECT count(*) FROM samples WHERE metadata ?| array['tags', 'media_type']")
    assert carrying == len({plot for _, plot, *_rest, keys in PLANTED if set(keys) & set(PROTECTED)}) + 2


def assert_only_the_keys_went(before: dict[str, Any], after: dict[str, Any]) -> None:
    """2.1, 2.3 and 2.5's touching only rows with a key: each sample as it was but for the two top-level keys, a row
    without one not even rewritten, and every untouched table as it was."""
    assert set(after['samples']) == set(before['samples'])
    for sample_id, (version, rest, metadata) in before['samples'].items():
        version_after, rest_after, metadata_after = after['samples'][sample_id]
        assert rest_after == rest, (sample_id, rest, rest_after)  # last_modified_at to the microsecond included
        assert metadata_after == without(metadata), (sample_id, metadata, metadata_after)
        if without(metadata) == metadata:
            assert version_after == version, (sample_id, 'a row without the keys was rewritten')
    for table, digest in before['tables'].items():
        assert after['tables'][table] == digest, f'{table} changed'


def mentions(line: str, dataset: int) -> bool:
    return bool(re.search(rf'(?<![\w-]){dataset}(?![\w-])', line)) or NAMES[dataset] in line


def test_the_migration_removes_the_two_keys_and_nothing_else_and_says_how_many(upgrade):
    u = upgrade()
    u.phase(plant)

    async def before(w: World) -> None:
        u.state['before'] = await snapshot(w)

    u.phase(before)
    shown = u.migrate()

    async def after(w: World) -> None:
        state = await snapshot(w)
        assert_only_the_keys_went(u.state['before'], state)
        left = await w.database.fetchval("SELECT count(*) FROM samples WHERE metadata ?| array['tags', 'media_type']")
        assert left == 0

    u.phase(after)
    # 2.4: for each dataset it changed, the samples it removed each key from.
    for (dataset, key), count in removed_counts().items():
        said = [
            line
            for line in shown
            if mentions(line, dataset) and key in line and re.search(rf'(?<![\w.]){count}(?![\w.])', line)
        ]
        assert said, f'nothing shown for {key} removed from {count} sample(s) of dataset {dataset}: {shown}'


def test_with_nothing_to_remove_the_migration_says_so_and_changes_no_row(upgrade):
    u = upgrade()

    async def plant_without(w: World) -> None:
        for dataset in (NONE, FEW):
            await w.database.execute('INSERT INTO datasets (id, name) VALUES ($1, $2)', dataset, NAMES[dataset])
            samples = [
                {
                    'filepath': f'{dataset}-{k}.jpg',
                    'tags': ['own'],
                    'metadata': {'n': k, 'info': {'tags': 'keep'}, 'tags_v2': k},
                }
                for k in range(3)
            ]
            await datasets.add_samples(dataset, {'samples': samples}, user=ADMIN_USER)
        u.state['before'] = await snapshot(w)

    u.phase(plant_without)
    shown = u.migrate()
    assert shown, 'the migration showed nothing on a database with nothing to remove'

    async def after(w: World) -> None:
        assert_only_the_keys_went(u.state['before'], await snapshot(w))

    u.phase(after)


def test_an_interrupted_migration_changes_nothing_and_a_second_run_changes_nothing(upgrade):
    u = upgrade()
    u.phase(plant)

    async def before(w: World) -> None:
        u.state['before'] = await snapshot(w)

    u.phase(before)
    # Interrupted: the runner's transaction fails at its end. Nothing is changed and nothing recorded.
    u.run_again(tail="SELECT 1 / 0;  -- the upgrade interrupted")

    async def unchanged(w: World) -> None:
        state = await snapshot(w)
        assert state == u.state['before'], 'an interrupted migration left changes behind'
        assert not await w.database.fetchval(
            'SELECT count(*) FROM schema_migrations WHERE filename = ANY($1)',
            [path.name for path in u.new],
        )

    u.phase(unchanged)
    u.migrate()  # the next upgrade does all of it

    async def done(w: World) -> None:
        state = await snapshot(w)
        assert_only_the_keys_went(u.state['before'], state)
        u.state['after'] = state

    u.phase(done)
    said = u.run_again()

    async def again(w: World) -> None:
        assert await snapshot(w) == u.state['after'], 'a second run changed rows'

    u.phase(again)
    reported = [line for line in said for dataset in NAMES if mentions(line, dataset)]
    assert not reported, f'a second run reported datasets: {reported}'


def test_a_version_or_a_deleted_sample_from_before_the_migration_does_not_bring_the_keys_back(upgrade):
    u = upgrade()

    async def before(w: World) -> None:
        await w.database.execute('INSERT INTO datasets (id, name) VALUES ($1, $2)', VERSIONED, NAMES[VERSIONED])
        samples = [
            {'filepath': f'v{k}.jpg', 'tags': ['own'], 'metadata': {'plot': f'v{k}', 'crop': 'corn'}} for k in range(3)
        ]
        await datasets.add_samples(VERSIONED, {'samples': samples}, user=ADMIN_USER)
        ids = {
            row['plot']: row['id']
            for row in await w.database.fetch(
                "SELECT id, metadata->>'plot' AS plot FROM samples WHERE dataset_id = $1",
                VERSIONED,
            )
        }
        await w.database.execute(
            'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
            ids['v0'],
            {'tags': 'x', 'media_type': ['y']},
        )
        await w.database.execute(
            'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
            ids['v1'],
            {'tags': ['z']},
        )
        made = await w.call(ADMIN, 'POST', f'/api/datasets/{VERSIONED}/versions', json={'name': 'before the migration'})
        assert made.status_code == 200, made.text
        deleted = await w.call(ADMIN, 'DELETE', f'/api/datasets/{VERSIONED}/samples/{ids["v1"]}')
        assert deleted.is_success, deleted.text
        u.state.update(ids=ids, version=made.json()['id'])

    u.phase(before)
    u.migrate()

    async def after(w: World) -> None:
        ids, version = u.state['ids'], u.state['version']
        reverted = await w.call(
            ADMIN,
            'POST',
            f'/api/datasets/{VERSIONED}/versions/{version}/revert',
            params={'restore_deleted': 'true'},
        )
        assert reverted.status_code == 200, reverted.text
        rows = await w.database.fetch(
            'SELECT id, metadata, tags FROM samples WHERE dataset_id = $1 ORDER BY id',
            VERSIONED,
        )
        found = {row['id']: (row['metadata'], row['tags']) for row in rows}
        assert set(found) == set(ids.values()), found  # the deleted sample is back
        for plot, sample_id in ids.items():
            assert found[sample_id] == ({'plot': plot, 'crop': 'corn'}, ['own']), (plot, found[sample_id])

    u.phase(after)


# 2.8: a field registered under one of the two names. Main makes one with a rename or a clone onto the name: it leaves
# a dataset_fields row (field_type string, origin metadata, owner self), the values under that metadata key, and once
# indexed (promote) is_promoted and an index idx_samples_meta_<dataset>_<field> on (metadata ->> '<field>') WHERE
# dataset_id = <dataset>. The code under test refuses that rename, so the state is written with SQL in that shape.
FIELD_TAGS, FIELD_EMPTY, FIELD_MEDIA, FIELD_MIRROR = 201, 202, 203, 204
FIELD_NAMES = {
    FIELD_TAGS: 'field-tags',
    FIELD_EMPTY: 'field-tags-empty',
    FIELD_MEDIA: 'field-media-type',
    FIELD_MIRROR: 'field-mirror',
}
FIELD_OF = {FIELD_TAGS: 'tags', FIELD_EMPTY: 'tags', FIELD_MEDIA: 'media_type', FIELD_MIRROR: 'tags'}
LEFTOVER_INDEX = f'idx_samples_meta_{FIELD_TAGS}_tags'
STATS_ROWS = (
    "SELECT field_name, to_jsonb(s) - 'last_updated' AS row FROM dataset_field_stats s WHERE dataset_id = $1 "
    "AND field_name NOT IN ('tags', 'media_type') ORDER BY field_name"
)


async def counted(w: UpgradeWorld, dataset: int) -> None:
    """The statistics runner run for the dataset to its end, not forced (count_statistics reads only the world's
    database and address)."""
    await count_statistics(cast('Regions', w), dataset)


async def found(w: UpgradeWorld, node: dict[str, Any], dataset: int) -> set[int]:
    """The samples a filter keeps, as samples/query lists them (its total agreeing)."""
    return await selected(cast('Regions', w), node, dataset)


async def plant_fields(w: UpgradeWorld) -> None:
    """Four datasets of four samples (three with the sample's own tag `own`), as main leaves them after a field was
    renamed onto `tags` (with values, and indexed), onto `tags` with every value since removed, cloned onto
    `media_type`, and a mirror whose primary has such a field; the statistics counted, as the runner counts them."""
    for dataset in FIELD_NAMES:
        await w.database.execute('INSERT INTO datasets (id, name) VALUES ($1, $2)', dataset, FIELD_NAMES[dataset])
        samples = [
            {'filepath': f'{dataset}-{k}.jpg', 'tags': ['own'] if k else [], 'metadata': {'kind': 'k', 'n': k}}
            for k in range(4)
        ]
        await datasets.add_samples(dataset, {'samples': samples}, user=ADMIN_USER)
    await w.database.execute(
        "UPDATE samples SET metadata = metadata || jsonb_build_object('tags', 'L' || (metadata->>'n')::int % 2) "
        'WHERE dataset_id = $1',
        FIELD_TAGS,
    )
    await w.database.execute(
        "UPDATE samples SET metadata = metadata || '{\"media_type\": \"k\"}'::jsonb WHERE dataset_id = $1",
        FIELD_MEDIA,
    )
    for dataset, promoted in ((FIELD_TAGS, True), (FIELD_EMPTY, False), (FIELD_MEDIA, False), (FIELD_MIRROR, False)):
        await w.database.execute(
            'INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, owner, is_promoted) '
            "VALUES ($1, $2, 'string', 'metadata', 'self', $3)",
            dataset,
            FIELD_OF[dataset],
            promoted,
        )
    await w.database.execute(
        f"CREATE INDEX {LEFTOVER_INDEX} ON samples USING btree ((metadata ->> 'tags')) WHERE dataset_id = {FIELD_TAGS}",
    )
    await w.database.execute(
        'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES ($1, 7, 0)',
        FIELD_MIRROR,
    )
    await w.database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = $1", FIELD_MIRROR)
    for dataset in (FIELD_TAGS, FIELD_EMPTY, FIELD_MEDIA):
        await counted(w, dataset)


async def sidebar(w: World, dataset: int) -> dict[str, dict[str, Any]]:
    """The field statistics the sidebar reads, by field name."""
    response = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset}/field-stats')
    assert response.status_code == 200, response.text
    return {entry['name']: entry for entry in response.json()}


async def own_values(w: World, dataset: int) -> tuple[list[str], list[str]]:
    """The sample's own tags and media types, as the samples hold them."""
    tags = await w.database.fetchval(
        'SELECT coalesce(array_agg(DISTINCT tag ORDER BY tag), ARRAY[]::text[]) '
        'FROM samples, unnest(tags) tag WHERE dataset_id = $1',
        dataset,
    )
    kinds = await w.database.fetchval(
        'SELECT array_agg(DISTINCT media_type ORDER BY media_type) FROM samples WHERE dataset_id = $1',
        dataset,
    )
    return list(tags), list(kinds)


def test_before_the_migration_a_metadata_field_named_tags_is_no_field_when_read(upgrade):
    """The state the 2.8 tests start from, before migration 0042 runs: the registry rows are there in main's shape
    (string metadata fields named `tags` and `media_type`, owner self, one indexed with its index, one a mirror's) and
    the samples carry the keys. A requirement of the reserved-names-never-fields contract (f7, 1.1 and 1.2: reading
    counts too, and the seven names follow one rule): such a row is no field when read, also before the migration has
    cleaned it. The field list shows `tags` and `media_type` once each, as the sample's own, with the samples' own
    values and not indexed; the value picker offers no stored value; a filter on `tags` reads the sample's own tags.
    On main e0f754fe the field list showed the stored values there instead (`L0`, `L1` in the sidebar's `tags`, which
    was indexed; no `own` where the values were removed; `k` in `media_type`)."""
    u = upgrade(require_new=False)

    async def scenario(w: UpgradeWorld) -> None:
        await plant_fields(w)
        rows = await w.database.fetch(
            'SELECT dataset_id, field_name, field_type, origin, owner, is_promoted FROM dataset_fields '
            "WHERE field_name IN ('tags', 'media_type') ORDER BY dataset_id",
        )
        assert [tuple(row) for row in rows] == [
            (FIELD_TAGS, 'tags', 'string', 'metadata', 'self', True),
            (FIELD_EMPTY, 'tags', 'string', 'metadata', 'self', False),
            (FIELD_MEDIA, 'media_type', 'string', 'metadata', 'self', False),
            (FIELD_MIRROR, 'tags', 'string', 'metadata', 'primary', False),
        ], rows
        assert await w.database.fetchval('SELECT 1 FROM pg_indexes WHERE indexname = $1', LEFTOVER_INDEX)
        carried = {
            dataset: await w.database.fetchval(
                'SELECT count(*) FROM samples WHERE dataset_id = $1 AND metadata ? $2',
                dataset,
                FIELD_OF[dataset],
            )
            for dataset in FIELD_NAMES
        }
        assert carried == {FIELD_TAGS: 4, FIELD_EMPTY: 0, FIELD_MEDIA: 4, FIELD_MIRROR: 0}, carried
        stored = {FIELD_TAGS: ('L0', 'L1'), FIELD_EMPTY: (), FIELD_MEDIA: ('k',)}
        for dataset, values in stored.items():
            tags, kinds = await own_values(w, dataset)
            listed = await sidebar(w, dataset)
            assert sorted(listed['tags'].get('values') or []) == tags, (dataset, listed['tags'])
            assert sorted(listed['media_type'].get('values') or []) == kinds, (dataset, listed['media_type'])
            assert not listed['tags'].get('is_promoted'), (dataset, listed['tags'])
            schema = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset}/schema')
            names = [entry['name'] for entry in schema.json()]
            assert names.count('tags') == names.count('media_type') == 1, (dataset, names)
            for name in ('tags', 'media_type'):
                offered = await w.call(
                    ADMIN,
                    'POST',
                    f'/api/datasets/{dataset}/fields/{name}/autocomplete',
                    json={'limit': 100},
                )
                if offered.status_code == 200:
                    picked = {item['value'] for item in offered.json()['values']}
                    assert not picked & set(values), (dataset, name, offered.text)
        own = await w.database.fetch(
            'SELECT id FROM samples WHERE dataset_id = $1 AND $2 = ANY(tags)',
            FIELD_TAGS,
            'own',
        )
        assert await found(w, {'field': 'tags', 'operator': 'in', 'value': ['own']}, FIELD_TAGS) == {
            row['id'] for row in own
        }
        assert await found(w, {'field': 'tags', 'operator': 'in', 'value': ['L1']}, FIELD_TAGS) == set()

    u.phase(scenario)


def test_a_field_registered_under_a_protected_name_goes_with_its_values(upgrade):
    """Contract 2.8, a requirement. After the upgrade, on a dataset that is not a mirror: no metadata field named
    `tags` or `media_type` in the registry; the sidebar's `tags` and `media_type` are the sample's own, counted from
    the samples once the statistics runner has run (the migration marks them to be counted: the runner is run
    without forcing a full count); a filter on `tags` finds by the sample's own tags; the output has a line for each
    field removed, naming the index left over that key; a followed dataset logs a field change for its followers as
    any field removal does (a `dataset_changes` row of kind `fields`), and nothing else in the change feed; the other
    fields' statistics keep their values (main's runner rewrites every row of a dataset it counts, so the time of the
    count is left aside); the mirror's registry row is left as it is."""
    u = upgrade()

    async def before(w: UpgradeWorld) -> None:
        await plant_fields(w)
        await w.database.execute('UPDATE datasets SET followed = TRUE WHERE id = $1', FIELD_TAGS)
        u.state['stats'] = {
            dataset: [tuple(row) for row in await w.database.fetch(STATS_ROWS, dataset)] for dataset in FIELD_NAMES
        }
        u.state['changes'] = [
            tuple(row) for row in await w.database.fetch('SELECT id, dataset_id, kind FROM dataset_changes')
        ]
        u.state['feed'] = {
            table: await w.database.fetchval(f'SELECT count(*) FROM {table}')
            for table in ('sample_block_changes', 'vector_changes', 'map_block_changes')
        }
        u.state['mirror'] = [
            tuple(row)
            for row in await w.database.fetch('SELECT * FROM dataset_fields WHERE dataset_id = $1', FIELD_MIRROR)
        ]

    u.phase(before)
    shown = u.migrate()

    async def after(w: UpgradeWorld) -> None:
        left = await w.database.fetch(
            "SELECT dataset_id, field_name FROM dataset_fields WHERE origin = 'metadata' "
            "AND field_name IN ('tags', 'media_type') ORDER BY dataset_id",
        )
        assert [tuple(row) for row in left] == [(FIELD_MIRROR, 'tags')], left
        assert [
            tuple(row)
            for row in await w.database.fetch('SELECT * FROM dataset_fields WHERE dataset_id = $1', FIELD_MIRROR)
        ] == u.state['mirror']
        changes = [tuple(row) for row in await w.database.fetch('SELECT id, dataset_id, kind FROM dataset_changes')]
        added = [change[1:] for change in changes if change not in u.state['changes']]
        assert added and set(added) == {(FIELD_TAGS, 'fields')}, added
        for table, count in u.state['feed'].items():
            assert await w.database.fetchval(f'SELECT count(*) FROM {table}') == count, table
        index = await w.database.fetchval("SELECT indexname FROM pg_indexes WHERE indexname = $1", LEFTOVER_INDEX)
        u.state['index_left'] = index is not None
        for dataset in (FIELD_TAGS, FIELD_EMPTY, FIELD_MEDIA):
            await counted(w, dataset)
            tags, kinds = await own_values(w, dataset)
            listed = await sidebar(w, dataset)
            assert sorted(listed['tags'].get('values') or []) == tags, (dataset, listed['tags'])
            assert sorted(listed['media_type'].get('values') or []) == kinds, (dataset, listed['media_type'])
            assert not listed['tags'].get('is_promoted'), (dataset, listed['tags'])
            schema = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset}/schema')
            names = [entry['name'] for entry in schema.json()]
            assert names.count('tags') == names.count('media_type') == 1, names
            rows = [tuple(row) for row in await w.database.fetch(STATS_ROWS, dataset)]
            assert rows == u.state['stats'][dataset], (dataset, 'another field was recounted')
        own = await w.database.fetch(
            'SELECT id FROM samples WHERE dataset_id = $1 AND $2 = ANY(tags)',
            FIELD_TAGS,
            'own',
        )
        assert await found(w, {'field': 'tags', 'operator': 'in', 'value': ['own']}, FIELD_TAGS) == {
            row['id'] for row in own
        }
        assert await found(w, {'field': 'tags', 'operator': 'in', 'value': ['L1']}, FIELD_TAGS) == set()

    u.phase(after)
    for dataset in (FIELD_TAGS, FIELD_EMPTY, FIELD_MEDIA):
        field = FIELD_OF[dataset]
        said = [
            line
            for line in shown
            if mentions_field_dataset(line, dataset) and field in line and 'field' in line.replace(field, '')
        ]
        assert said, f'no line for the field {field} removed from dataset {dataset}: {shown}'
    if u.state['index_left']:
        assert any(LEFTOVER_INDEX in line for line in shown), f'the index left over is not named: {shown}'


def mentions_field_dataset(line: str, dataset: int) -> bool:
    return bool(re.search(rf'(?<![\w-]){dataset}(?![\w-])', line)) or FIELD_NAMES[dataset] in line


# 2.8's last point: what was said about the name stays with it. A rename or clone onto `tags` lands on the row the
# sample's own field has: main's routes leave one dataset_fields row (string, metadata, owner self) carrying the
# description, the unit and the private mark, whether the mark was set before the rename (on the built-in's own row,
# which the field takes over) or after it (read through the routes on main: the same row either way). The code under
# test refuses the rename, so that row is written with SQL.
NOTES = {'description': 'What the labels on a plot mean', 'unit': 'labels', 'values_private': True}
OWN_TAG, OWN_KIND, META_VALUE = 'ZQXOWNTAG', 'zqxownkind', 'ZQXMETAVALUE'
PRIVATE_AFTER, PRIVATE_BEFORE, PRIVATE_MEDIA, PLAIN_TAGS, PRIVATE_MIRROR = 211, 212, 213, 214, 215
PRIVATE_NAMES = {
    PRIVATE_AFTER: 'private-tags-set-after',
    PRIVATE_BEFORE: 'private-tags-set-before',
    PRIVATE_MEDIA: 'private-media-type',
    PLAIN_TAGS: 'plain-tags',
    PRIVATE_MIRROR: 'private-mirror',
}
PRIVATE_FIELD = {
    PRIVATE_AFTER: 'tags',
    PRIVATE_BEFORE: 'tags',
    PRIVATE_MEDIA: 'media_type',
    PLAIN_TAGS: 'tags',
    PRIVATE_MIRROR: 'tags',
}
NOTED = (PRIVATE_AFTER, PRIVATE_BEFORE, PRIVATE_MEDIA)


async def plant_private(w: UpgradeWorld) -> None:
    """Five datasets of four samples (three with the sample's own tag OWN_TAG, all of media type OWN_KIND), each with
    a field renamed or cloned onto `tags` or `media_type` holding META_VALUE: three with the description, unit and
    private mark on that row, one with none of them, and a mirror with a private row; the statistics counted."""
    for dataset, name in PRIVATE_NAMES.items():
        await w.database.execute('INSERT INTO datasets (id, name) VALUES ($1, $2)', dataset, name)
        samples = [
            {
                'filepath': f'{dataset}-{k}.jpg',
                'media_type': OWN_KIND,
                'tags': [OWN_TAG] if k else [],
                'metadata': {'kind': 'k', 'n': k},
            }
            for k in range(4)
        ]
        await datasets.add_samples(dataset, {'samples': samples}, user=ADMIN_USER)
        field = PRIVATE_FIELD[dataset]
        if dataset != PRIVATE_MIRROR:
            await w.database.execute(
                'UPDATE samples SET metadata = metadata || $2::jsonb WHERE dataset_id = $1',
                dataset,
                {field: META_VALUE},
            )
        noted = dataset in NOTED or dataset == PRIVATE_MIRROR
        await w.database.execute(
            'INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, owner, description, unit, '
            "values_private) VALUES ($1, $2, 'string', 'metadata', 'self', $3, $4, $5)",
            dataset,
            field,
            NOTES['description'] if noted else None,
            NOTES['unit'] if noted else None,
            noted,
        )
    await w.database.execute(
        'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES ($1, 7, 0)',
        PRIVATE_MIRROR,
    )
    await w.database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = $1", PRIVATE_MIRROR)
    for dataset in (*NOTED, PLAIN_TAGS):
        await counted(w, dataset)
    await open_door(w)


async def assert_kept_from_the_assistant(w: UpgradeWorld, dataset: int, when: str) -> None:
    """The assistant gets the field's description and unit, but none of its values: describe_dataset marks it private
    with no examples, field_values gives no value (or refuses), and neither answer holds a value of it."""
    field = PRIVATE_FIELD[dataset]
    described = await call(ADMIN, 'describe_dataset', {'dataset': dataset, 'limit': 100})
    assert described['ok'], (when, described)
    [entry] = [each for each in described['result']['fields'] if each['name'] == field]
    assert (entry['values_private'], entry['description'], entry['unit']) == (
        True,
        NOTES['description'],
        NOTES['unit'],
    ), (when, entry)
    assert not (entry['values'] or {}).get('examples'), (when, entry)
    values = await call(ADMIN, 'field_values', {'dataset': dataset, 'field': field, 'limit': 50})
    assert not values['ok'] or values['result']['values'] == [], (when, values)
    hidden = (OWN_TAG if field == 'tags' else OWN_KIND, META_VALUE)  # the field's values, its own and the metadata's
    for answer in (described, values):
        text = json.dumps(answer)
        for value in hidden:
            assert value not in text, (when, dataset, value)


def test_before_the_migration_a_private_field_named_tags_keeps_its_values_from_the_assistant(upgrade):
    """Pins main (passes before the fix): on such a dataset the assistant gets the description and unit of `tags`
    (or `media_type`) and none of its values."""
    u = upgrade(require_new=False)

    async def scenario(w: UpgradeWorld) -> None:
        await plant_private(w)
        for dataset in NOTED:
            await assert_kept_from_the_assistant(w, dataset, 'before the upgrade')

    u.phase(scenario)


def test_what_was_said_about_the_name_stays_when_the_field_goes(upgrade):
    """Contract 2.8's last point, a requirement. After the upgrade, on the three datasets whose field carried a
    description, a unit and the private mark: the name is the sample's own field again (no metadata field of the name,
    not promoted, shown once in the field list), still private to the assistant (the same tool calls give no value of
    it), with its description and unit in the field list and in describe_dataset; the sidebar's values are the
    samples' own after one ordinary statistics refresh. The field with nothing to keep is gone with no row left, the
    mirror's row is as it was, and the output has a line for each field removed."""
    u = upgrade()

    async def before(w: UpgradeWorld) -> None:
        await plant_private(w)
        u.state['mirror'] = [
            tuple(row)
            for row in await w.database.fetch('SELECT * FROM dataset_fields WHERE dataset_id = $1', PRIVATE_MIRROR)
        ]

    u.phase(before)
    shown = u.migrate()

    async def after(w: UpgradeWorld) -> None:
        metadata_rows = await w.database.fetch(
            "SELECT dataset_id FROM dataset_fields WHERE origin = 'metadata' AND field_name IN ('tags', 'media_type')",
        )
        assert [row['dataset_id'] for row in metadata_rows] == [PRIVATE_MIRROR], metadata_rows
        assert [
            tuple(row)
            for row in await w.database.fetch('SELECT * FROM dataset_fields WHERE dataset_id = $1', PRIVATE_MIRROR)
        ] == u.state['mirror']
        plain = await w.database.fetchval(
            "SELECT count(*) FROM dataset_fields WHERE dataset_id = $1 AND field_name = 'tags'",
            PLAIN_TAGS,
        )
        assert plain == 0, 'the field with nothing to keep left a row'
        for dataset in (*NOTED, PLAIN_TAGS):
            field = PRIVATE_FIELD[dataset]
            schema = (await w.call(ADMIN, 'GET', f'/api/datasets/{dataset}/schema')).json()
            entries = [entry for entry in schema if entry['name'] == field]
            assert len(entries) == 1, (dataset, entries)
            [entry] = entries
            if dataset in NOTED:
                kept = (entry.get('values_private'), entry.get('description'), entry.get('unit'))
                assert kept == (True, NOTES['description'], NOTES['unit']), (dataset, entry)
                await assert_kept_from_the_assistant(w, dataset, 'after the upgrade')
            else:
                assert not entry.get('values_private'), (dataset, entry)
            await counted(w, dataset)
            listed = await sidebar(w, dataset)
            own = [OWN_TAG] if field == 'tags' else [OWN_KIND]
            assert sorted(listed[field].get('values') or []) == own, (dataset, listed[field])
            assert not listed[field].get('is_promoted'), (dataset, listed[field])

    u.phase(after)
    for dataset in (*NOTED, PLAIN_TAGS):
        field = PRIVATE_FIELD[dataset]
        said = [
            line
            for line in shown
            if (re.search(rf'(?<![\w-]){dataset}(?![\w-])', line) or PRIVATE_NAMES[dataset] in line)
            and field in line
            and 'field' in line.replace(field, '')
        ]
        assert said, f'no line for the field {field} removed from dataset {dataset}: {shown}'


def test_a_metadata_that_is_not_an_object_is_left_as_it_is(upgrade):
    """Contract 2.9, a requirement: a sample whose metadata is a list holding the text `tags`, the text `tags`, JSON
    null or no value at all (written with SQL, the samples table's triggers off for it and on again after, as the app
    refuses it) is not changed by the migration, nor its row rewritten; a sample beside it is cleaned."""
    u = upgrade()

    async def before(w: UpgradeWorld) -> None:
        await w.database.execute('INSERT INTO datasets (id, name) VALUES ($1, $2)', FEW, NAMES[FEW])
        samples = [{'filepath': f'odd-{k}.jpg', 'metadata': {'n': k}} for k in range(5)]
        await datasets.add_samples(FEW, {'samples': samples}, user=ADMIN_USER)
        ids = [
            row['id'] for row in await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id', FEW)
        ]
        async with w.database.pool.acquire() as connection, connection.transaction():
            await connection.execute('ALTER TABLE samples DISABLE TRIGGER USER')
            for sample_id, value in zip(
                ids,
                ('\'["tags", "x"]\'::jsonb', '\'"tags"\'::jsonb', "'null'::jsonb", 'NULL'),
            ):
                await connection.execute(f'UPDATE samples SET metadata = {value} WHERE id = $1', sample_id)
            await connection.execute('ALTER TABLE samples ENABLE TRIGGER USER')
        await w.database.execute(
            'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
            ids[4],
            {'tags': 'x'},
        )
        rows = await w.database.fetch(
            'SELECT id, xmin::text AS version, metadata::text AS metadata FROM samples WHERE id = ANY($1) ORDER BY id',
            ids,
        )
        u.state['rows'] = [tuple(row) for row in rows]

    u.phase(before)
    u.migrate()

    async def after(w: UpgradeWorld) -> None:
        ids = [row[0] for row in u.state['rows']]
        rows = await w.database.fetch(
            'SELECT id, xmin::text AS version, metadata::text AS metadata FROM samples WHERE id = ANY($1) ORDER BY id',
            ids,
        )
        assert [tuple(row) for row in rows][:4] == u.state['rows'][:4]
        assert await w.database.fetchval('SELECT metadata FROM samples WHERE id = $1', ids[4]) == {'n': 4}

    u.phase(after)


def test_after_the_migration_a_cleaned_sample_shows_as_modified_against_a_version_saved_before(upgrade):
    """What contract 3.2's upgrade note says (a pin of it): against a version saved before the upgrade a sample the
    migration cleaned shows as modified (current-diff's modified list), and still after a revert to that version;
    against a version saved after the upgrade, nothing is modified."""
    u = upgrade()

    async def before(w: UpgradeWorld) -> None:
        await w.database.execute('INSERT INTO datasets (id, name) VALUES ($1, $2)', VERSIONED, NAMES[VERSIONED])
        samples = [{'filepath': f'm{k}.jpg', 'metadata': {'plot': f'm{k}'}} for k in range(3)]
        await datasets.add_samples(VERSIONED, {'samples': samples}, user=ADMIN_USER)
        ids = [
            row['id']
            for row in await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id', VERSIONED)
        ]
        await w.database.execute(
            'UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1',
            ids[0],
            {'media_type': 'x'},
        )
        made = await w.call(ADMIN, 'POST', f'/api/datasets/{VERSIONED}/versions', json={'name': 'before'})
        assert made.status_code == 200, made.text
        u.state.update(ids=ids, version=made.json()['id'])

    u.phase(before)
    u.migrate()

    async def after(w: UpgradeWorld) -> None:
        ids, version = u.state['ids'], u.state['version']

        async def modified(against: int) -> list[int]:
            response = await w.call(
                ADMIN,
                'GET',
                f'/api/datasets/{VERSIONED}/versions/current-diff/{against}/samples',
                params={'change_type': 'modified'},
            )
            assert response.status_code == 200, response.text
            return response.json()['sample_ids']

        assert await modified(version) == [ids[0]]
        reverted = await w.call(ADMIN, 'POST', f'/api/datasets/{VERSIONED}/versions/{version}/revert')
        assert reverted.status_code == 200, reverted.text
        assert await modified(version) == [ids[0]]
        made = await w.call(ADMIN, 'POST', f'/api/datasets/{VERSIONED}/versions', json={'name': 'after'})
        assert made.status_code == 200, made.text
        assert await modified(made.json()['id']) == []

    u.phase(after)


# --- 3. Words ---

UPLOADER_DOCS = ('docs/SDK.md', 'docs/DATASET_MANAGEMENT.md', 'docs/QUICK_START.md')


def test_the_docs_list_the_seven_names_and_the_upgrade_notes_say_what_the_migration_does():
    blocks = []
    for doc in UPLOADER_DOCS:
        blocks += re.split(r'\n\s*\n', (ROOT / doc).read_text())
    listing = [block for block in blocks if all(re.search(rf'`{name}`', block) for name in SEVEN)]
    assert listing, f'no paragraph or list of {", ".join(UPLOADER_DOCS)} names all seven protected keys'
    changelog = (ROOT / 'CHANGELOG.md').read_text()
    unreleased = changelog.split('\n## Unreleased', 1)[1].split('\n## ', 1)[0]
    notes = unreleased.split('### Upgrade notes', 1)
    assert len(notes) == 2, 'the Unreleased section has no Upgrade notes'
    upgrade_notes = notes[1].split('\n### ', 1)[0]
    for word in ('`tags`', '`media_type`'):
        assert word in upgrade_notes, (word, upgrade_notes)
    assert re.search(r'migration', upgrade_notes, re.I) and re.search(r'remov', upgrade_notes, re.I), upgrade_notes


def test_the_docs_say_the_seven_are_refused_wherever_metadata_is_written_and_a_stored_one_stays_until_removed():
    """Section 4 of the reserved-keys piece, lightly: the paragraph of docs/SDK.md that lists the seven names says
    they are refused wherever metadata is written and that a sample carrying one keeps it until it is removed; the
    Unreleased upgrade notes have a sentence that names one of the five and edits."""
    blocks = re.split(r'\n\s*\n', (ROOT / 'docs/SDK.md').read_text())
    [listing] = [block for block in blocks if all(re.search(rf'`{name}`', block) for name in SEVEN)]
    flat = ' '.join(listing.split())
    assert 'wherever metadata is written' in flat, flat
    assert not re.search(r'`tags` and `media_type` are refused wherever', flat), flat  # all seven now, not two
    assert re.search(r'\buntil\b[^.]*\bremoved\b', flat), flat
    changelog = (ROOT / 'CHANGELOG.md').read_text()
    unreleased = changelog.split('\n## Unreleased', 1)[1].split('\n## ', 1)[0]
    notes = unreleased.split('### Upgrade notes', 1)[-1].split('\n### ', 1)[0]
    sentences = re.split(r'(?<=[.;:])\s+', ' '.join(notes.split()))
    five = r'`(id|dataset_id|filepath|created_at|last_modified_at)`'
    assert any(re.search(five, each) and re.search(r'\bedit', each) for each in sentences), notes


def test_the_upgrade_notes_say_cleaned_samples_show_as_modified_and_the_keep_statement_leaves_mirrors_out():
    """Contract 3.2's added words, lightly: a sentence of the upgrade notes says a cleaned sample shows as modified
    against a version, and the notes speak of mirrors."""
    changelog = (ROOT / 'CHANGELOG.md').read_text()
    unreleased = changelog.split('\n## Unreleased', 1)[1].split('\n## ', 1)[0]
    notes = unreleased.split('### Upgrade notes', 1)[-1].split('\n### ', 1)[0]
    sentences = re.split(r'(?<=[.;:])\s+', ' '.join(notes.split()))
    assert any(re.search(r'modified', each, re.I) and re.search(r'version', each, re.I) for each in sentences), notes
    assert re.search(r'mirror', notes, re.I), notes
