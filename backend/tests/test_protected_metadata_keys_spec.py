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
from typing import Any, Callable, Optional

import httpx
import pytest
import requests
from fridge.core import client as client_module
from fridge.core.client import Client
from fridge.core.dataset import Dataset
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from backend.routers import datasets
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.changelog_helpers import release_heading
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_backend_four_spec import KEYED, OLDER, keyed, refused_at, sentence, stored, upload
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_regions_spec import ADMIN_USER, Regions, nodes, post, selected
from backend.tests.test_upgrade_paths import ROOT

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


# --- 3. Words ---

UPLOADER_DOCS = ('docs/SDK.md', 'docs/DATASET_MANAGEMENT.md', 'docs/QUICK_START.md')


def test_the_docs_list_the_seven_names_and_the_upgrade_notes_say_what_the_migration_does():
    blocks = []
    for doc in UPLOADER_DOCS:
        blocks += re.split(r'\n\s*\n', (ROOT / doc).read_text())
    listing = [block for block in blocks if all(re.search(rf'`{name}`', block) for name in SEVEN)]
    assert listing, f'no paragraph or list of {", ".join(UPLOADER_DOCS)} names all seven protected keys'
    changelog = (ROOT / 'CHANGELOG.md').read_text()
    unreleased = changelog.split('\n' + release_heading('2.14.0', changelog), 1)[1].split('\n## ', 1)[0]
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
    unreleased = changelog.split('\n' + release_heading('2.14.0', changelog), 1)[1].split('\n## ', 1)[0]
    notes = unreleased.split('### Upgrade notes', 1)[-1].split('\n### ', 1)[0]
    sentences = re.split(r'(?<=[.;:])\s+', ' '.join(notes.split()))
    five = r'`(id|dataset_id|filepath|created_at|last_modified_at)`'
    assert any(re.search(five, each) and re.search(r'\bedit', each) for each in sentences), notes


def test_the_upgrade_notes_say_cleaned_samples_show_as_modified_and_the_keep_statement_leaves_mirrors_out():
    """Contract 3.2's added words, lightly: a sentence of the upgrade notes says a cleaned sample shows as modified
    against a version, and the notes speak of mirrors."""
    changelog = (ROOT / 'CHANGELOG.md').read_text()
    unreleased = changelog.split('\n' + release_heading('2.14.0', changelog), 1)[1].split('\n## ', 1)[0]
    notes = unreleased.split('### Upgrade notes', 1)[-1].split('\n### ', 1)[0]
    sentences = re.split(r'(?<=[.;:])\s+', ' '.join(notes.split()))
    assert any(re.search(r'modified', each, re.I) and re.search(r'version', each, re.I) for each in sentences), notes
    assert re.search(r'mirror', notes, re.I), notes
