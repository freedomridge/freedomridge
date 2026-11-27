"""A list field kept in a column of its own can hold "never set" apart from the empty list: the implementer's own tests
of what it built (the spec's tests are written apart, from the contract). The API with real tokens on a database of
its own per test (the region tests' world), the registration job driven step by step as the runner drives it, the
migration on a column made as the app made it before, and the SDK's Sample alone."""

import json
from pathlib import Path
from typing import Any

import pytest
from fridge.core.sample import Sample

from backend import job_queue, list_fields
from backend.routers import datasets
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests import test_regions_spec
from backend.tests.test_access_rule_spec import ADMIN

database = test_regions_spec.database
regions_world = test_regions_spec.regions_world
registry_database = registry_helpers.registry_database
ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / 'backend/db/migrations/0045_list_columns_hold_never_set.sql'
D, OTHER = 801, 802


async def query(r, dataset_id: int, filters: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {'limit': 500}
    if filters is not None:
        body['filters'] = filters
    response = await r.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/samples/query', json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def lists(r, dataset_id: int, field: str) -> dict[str, Any]:
    """Each sample's list as the samples query gives it back (the key with null when never set)."""
    return {item['filepath']: item[field] for item in (await query(r, dataset_id))['items']}


async def shown(r, dataset_id: int, text: str) -> set[str]:
    parsed = await r.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/filter_query/parse', json={'query': text})
    assert parsed.status_code == 200, parsed.text
    answer = await query(r, dataset_id, parsed.json()['filter'])
    names = {item['filepath'] for item in answer['items']}
    assert answer['total'] == len(names)
    return names


async def add(r, dataset_id: int, samples: list, on_duplicate: str = 'error'):
    return await r.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/samples',
        json={'samples': samples, 'on_duplicate': on_duplicate},
    )


async def stored(r, dataset_id: int, field: str) -> dict[str, Any]:
    rows = await r.database.fetch(
        f'SELECT filepath, {field} AS value FROM samples WHERE dataset_id = $1',
        dataset_id,
    )
    return {row['filepath']: row['value'] for row in rows}


async def run_job(r, job_id) -> dict:
    """The registration job to its end, step by step, as the runner runs it."""
    job = await job_queue.claim_job(r.database.pool, 'list-test', kinds=[list_fields.KIND], lease_seconds=600)
    assert job is not None and str(job['id']) == str(job_id)
    step = list_fields.handler(r.database.pool)
    for _ in range(1000):
        result = await step(job)
        job = await job_queue.publish_step(
            r.database.pool,
            job['id'],
            job['lease_token'],
            checkpoint=result.checkpoint,
            done=result.done,
            total=result.total,
            result=result.result,
            finished=result.finished,
            publisher=result.publish,
        )
        if job['state'] != 'running':
            return job
    pytest.fail('The registration job did not finish')


def test_three_states_written_and_given_back(regions_world):
    async def scenario(r):
        await r.dataset(D)
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'lnv_pests'})
        assert made.status_code == 200, made.text
        default = await r.database.fetchval(
            "SELECT atthasdef FROM pg_attribute WHERE attrelid = 'samples'::regclass AND attname = 'lnv_pests'",
        )
        assert default is False  # a column made now has no default
        assert (await add(r, D, [{'filepath': 'without.jpg'}])).status_code == 200
        assert (await add(r, D, [{'filepath': 'empty.jpg', 'array_fields': {'lnv_pests': []}}])).status_code == 200
        assert (await add(r, D, [{'filepath': 'null.jpg', 'array_fields': {'lnv_pests': None}}])).status_code == 200
        response = await add(
            r,
            D,
            [
                {'filepath': 'listed.jpg', 'array_fields': {'lnv_pests': ['aphid']}},
                {'filepath': 'beside.jpg'},  # in a request where another sample names the field
            ],
        )
        assert response.status_code == 200
        assert await lists(r, D, 'lnv_pests') == {
            'without.jpg': None,
            'empty.jpg': [],
            'null.jpg': None,
            'listed.jpg': ['aphid'],
            'beside.jpg': None,
        }
        ids = {item['filepath']: item['id'] for item in (await query(r, D))['items']}
        patched = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{D}/samples/{ids["listed.jpg"]}',
            json={'array_fields': {'lnv_pests': None}},
        )
        assert patched.status_code == 200, patched.text
        assert (await lists(r, D, 'lnv_pests'))['listed.jpg'] is None
        # Added again without the field, beside a sample that names it: the list stays (rule 1.7, section 9.2).
        await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{D}/samples/{ids["empty.jpg"]}',
            json={'array_fields': {'lnv_pests': ['mite']}},
        )
        again = await add(
            r,
            D,
            [{'filepath': 'empty.jpg'}, {'filepath': 'without.jpg', 'array_fields': {'lnv_pests': []}}],
            'update',
        )
        assert again.status_code == 200, again.text
        after = await lists(r, D, 'lnv_pests')
        assert after['empty.jpg'] == ['mite'] and after['without.jpg'] == []
        # A mirror's own tags still take a list only.
        local = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{D}/samples/{ids["null.jpg"]}',
            json={'array_fields': {'@local.tags': None}},
        )
        assert local.status_code == 422

    regions_world(scenario)


def test_filters_read_a_list_column_as_a_list_in_a_field(regions_world):
    async def scenario(r):
        await r.dataset(D)
        await r.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'lnv_marks'})
        await add(
            r,
            D,
            [
                {'filepath': 'never.jpg'},
                {'filepath': 'empty.jpg', 'array_fields': {'lnv_marks': []}},
                {'filepath': 'x.jpg', 'array_fields': {'lnv_marks': ['x']}},
                {'filepath': 'y.jpg', 'array_fields': {'lnv_marks': ['y']}},
            ],
        )
        everything = {'never.jpg', 'empty.jpg', 'x.jpg', 'y.jpg'}
        expected = {
            'lnv_marks is null': {'never.jpg'},
            'lnv_marks is not null': everything - {'never.jpg'},
            'lnv_marks is empty': {'empty.jpg'},
            'lnv_marks is not empty': {'x.jpg', 'y.jpg'},
            'lnv_marks not in ("x")': {'empty.jpg', 'y.jpg'},
            'NOT (lnv_marks contains "x")': {'empty.jpg', 'y.jpg'},
            'NOT (lnv_marks is empty)': {'x.jpg', 'y.jpg'},
            'lnv_marks exists': everything - {'never.jpg'},
            'lnv_marks not exists': {'never.jpg'},
            'lnv_marks in ("x")': {'x.jpg'},
        }
        for text, rows in expected.items():
            assert await shown(r, D, text) == rows, text
        # Tags are not touched: an untagged sample has no value for them.
        assert await shown(r, D, 'tags is null') == everything
        assert await shown(r, D, 'tags is empty') == everything
        assert await shown(r, D, 'tags exists') == set()

    regions_world(scenario)


async def other_made(r, name: str) -> None:
    """Another dataset makes the column first, and has a sample with a list there."""
    await r.dataset(OTHER)
    made = await r.call(ADMIN, 'POST', f'/api/datasets/{OTHER}/array-fields', json={'field_name': name})
    assert made.status_code == 200, made.text
    await add(r, OTHER, [{'filepath': 'other.jpg', 'array_fields': {name: ['theirs']}}])


async def old_default(r, name: str, rows: int) -> None:
    """D's rows written before the upgrade: the column's old default, the empty list; one holds a list (a version's
    restore can leave one)."""
    await add(r, D, [{'filepath': f'old-{n}.jpg'} for n in range(rows)])
    await r.database.execute(
        f"UPDATE samples SET {name} = '{{}}', last_modified_at = '2026-01-01T00:00:00Z' WHERE dataset_id = $1",
        D,
    )
    await r.database.execute(
        f"UPDATE samples SET {name} = '{{kept}}' WHERE dataset_id = $1 AND filepath = 'old-0.jpg'",
        D,
    )


def test_registering_a_name_another_dataset_made_inside_the_request(regions_world):
    async def scenario(r):
        await other_made(r, 'lnv_shared')
        await r.dataset(D)
        await old_default(r, 'lnv_shared', 5)
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'lnv_shared'})
        assert made.status_code == 200, made.text
        assert made.json() == {'message': "Array field 'lnv_shared' registered for this dataset", 'created': False}
        values = await stored(r, D, 'lnv_shared')
        assert values.pop('old-0.jpg') == ['kept']
        assert set(values.values()) == {None}
        assert await stored(r, OTHER, 'lnv_shared') == {'other.jpg': ['theirs']}

    regions_world(scenario)


def test_registering_a_name_another_dataset_made_through_the_job(regions_world, monkeypatch):
    async def scenario(r):
        monkeypatch.setattr(list_fields, 'INLINE_SAMPLES', 2)
        monkeypatch.setattr(list_fields, 'STEP_IDS', 2)
        await other_made(r, 'lnv_big')
        await r.dataset(D)
        await old_default(r, 'lnv_big', 7)
        await r.call(ADMIN, 'POST', f'/api/datasets/{D}/versions', json={'name': 'v1'})
        made = await r.call(
            ADMIN,
            'POST',
            f'/api/datasets/{D}/array-fields',
            params={'wait': 'false'},
            json={'field_name': 'lnv_big'},
        )
        assert made.status_code == 202, made.text
        job_id = made.json()['job_id']
        joined = await r.call(
            ADMIN,
            'POST',
            f'/api/datasets/{D}/array-fields',
            params={'wait': 'false'},
            json={'field_name': 'lnv_big'},
        )
        assert joined.status_code == 202 and joined.json()['job_id'] == job_id
        listed = await r.call(ADMIN, 'GET', f'/api/datasets/{D}/array-fields')
        assert 'lnv_big' not in {field['name'] for field in listed.json()}
        refused = await add(r, D, [{'filepath': 'new.jpg', 'array_fields': {'lnv_big': []}}])
        assert refused.status_code == 409, refused.text
        assert job_id in refused.json()['detail'] and 'being added to this dataset' in refused.json()['detail']
        assert refused.headers['Retry-After'] == str(list_fields.RETRY_SECONDS)
        assert int(refused.headers[list_fields.END_HEADER]) > 0
        version_id = await r.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', D)
        restore = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/versions/{version_id}/revert')
        assert restore.status_code == 409 and job_id in restore.text
        assert (await add(r, D, [{'filepath': 'plain.jpg'}])).status_code == 200  # samples can be added meanwhile
        job = await run_job(r, job_id)
        assert job['state'] == 'succeeded', job
        values = await stored(r, D, 'lnv_big')
        assert values.pop('old-0.jpg') == ['kept']
        assert set(values.values()) == {None}
        unchanged = await r.database.fetchval(
            "SELECT bool_and(last_modified_at = '2026-01-01T00:00:00Z') FROM samples "
            "WHERE dataset_id = $1 AND filepath LIKE 'old-%'",
            D,
        )
        assert unchanged
        listed = await r.call(ADMIN, 'GET', f'/api/datasets/{D}/array-fields')
        assert 'lnv_big' in {field['name'] for field in listed.json()}
        assert (await add(r, D, [{'filepath': 'new.jpg', 'array_fields': {'lnv_big': []}}])).status_code == 200
        assert (await lists(r, D, 'lnv_big'))['new.jpg'] == []

    regions_world(scenario)


def test_a_restore_writes_a_list_column_only_where_the_version_had_the_field(regions_world):
    async def scenario(r):
        await other_made(r, 'lnv_late')
        await r.dataset(D)
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'lnv_had'})
        assert made.status_code == 200
        await add(
            r,
            D,
            [
                {'filepath': 'a.jpg', 'array_fields': {'lnv_had': ['a']}},
                {'filepath': 'b.jpg', 'array_fields': {'lnv_had': []}},
                {'filepath': 'c.jpg'},
            ],
        )
        await r.call(ADMIN, 'POST', f'/api/datasets/{D}/versions', json={'name': 'v1'})
        version_id = await r.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', D)
        # After the version: the lists change, D registers the name OTHER made, and a column no dataset had then.
        ids = {item['filepath']: item['id'] for item in (await query(r, D))['items']}
        for name, value in (('a.jpg', None), ('b.jpg', ['b']), ('c.jpg', [])):
            await r.call(
                ADMIN,
                'PATCH',
                f'/api/datasets/{D}/samples/{ids[name]}',
                json={'array_fields': {'lnv_had': value}},
            )
        await r.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'lnv_late'})
        await r.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'lnv_new'})
        for name in ('a.jpg', 'b.jpg'):
            await r.call(
                ADMIN,
                'PATCH',
                f'/api/datasets/{D}/samples/{ids[name]}',
                json={'array_fields': {'lnv_late': ['late'], 'lnv_new': ['new']}},
            )
        restored = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/versions/{version_id}/revert')
        assert restored.status_code == 200, restored.text
        assert await lists(r, D, 'lnv_had') == {'a.jpg': ['a'], 'b.jpg': [], 'c.jpg': None}
        assert await lists(r, D, 'lnv_late') == {'a.jpg': None, 'b.jpg': None, 'c.jpg': None}
        assert await lists(r, D, 'lnv_new') == {'a.jpg': None, 'b.jpg': None, 'c.jpg': None}
        listed = await r.call(ADMIN, 'GET', f'/api/datasets/{D}/array-fields')
        assert {'lnv_had', 'lnv_late', 'lnv_new'} <= {field['name'] for field in listed.json()}
        assert await stored(r, OTHER, 'lnv_late') == {'other.jpg': ['theirs']}

    regions_world(scenario)


def test_the_migration_takes_the_defaults_off_and_runs_twice(regions_world):
    async def scenario(r):
        await r.dataset(D)
        # A column as add_array_column made it before: a default, and a row that holds it.
        await r.database.execute("ALTER TABLE samples ADD COLUMN lnv_before TEXT[] DEFAULT '{}'")
        await r.database.execute("INSERT INTO sample_array_columns (column_name) VALUES ('lnv_before')")
        await add(r, D, [{'filepath': 'before.jpg'}])
        sql = MIGRATION.read_text()
        for _ in range(2):
            async with r.database.pool.acquire() as connection, connection.transaction():
                await connection.execute(sql)
        defaults = {
            row['attname']: row['atthasdef']
            for row in await r.database.fetch(
                "SELECT attname, atthasdef FROM pg_attribute WHERE attrelid = 'samples'::regclass "
                "AND attname IN ('lnv_before', 'tags', 'local_tags')",
            )
        }
        assert defaults == {'lnv_before': False, 'tags': True, 'local_tags': True}
        await add(r, D, [{'filepath': 'after.jpg'}])
        assert await stored(r, D, 'lnv_before') == {'before.jpg': [], 'after.jpg': None}
        assert await stored(r, D, 'tags') == {'before.jpg': [], 'after.jpg': []}

    regions_world(scenario)


def test_the_sidebar_counts_the_rows_never_set(registry_database):
    from backend.tests.statistics_helpers import run_refresh
    from backend.tests.test_field_registry_postgres import USER

    async def scenario(database):
        await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='lnv_side'), user=USER)
        samples = [
            {'filepath': 'never-1.jpg'},
            {'filepath': 'never-2.jpg'},
            {'filepath': 'empty.jpg', 'array_fields': {'lnv_side': []}},
            {'filepath': 'listed.jpg', 'array_fields': {'lnv_side': ['x']}},
        ]
        await datasets.add_samples(1, {'samples': samples}, user=USER)
        results = await run_refresh(database)
        assert all(result['error'] is None for result in results), results
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        entry = next(field for field in fields if field['name'] == 'lnv_side')
        assert entry['null_count'] == 2
        tags = next(field for field in fields if field['name'] == 'tags')
        assert 'null_count' not in tags

    registry_database(scenario)


def test_the_sdk_keeps_never_set_and_never_writes_back_one_it_read(monkeypatch):
    monkeypatch.setattr(Sample, '_known_array_fields', {*Sample._known_array_fields, 'lnv_sdk'})
    read = Sample.from_row({'id': 1, 'filepath': 'a.jpg', 'tags': [], 'metadata': {}, 'lnv_sdk': None})
    assert read.lnv_sdk is None and (read.lnv_sdk or []) == []
    assert read._array_fields_to_save() == {}
    read.lnv_sdk = ['x']
    assert read._array_fields_to_save() == {'lnv_sdk': ['x']}
    listed = Sample.from_row({'id': 2, 'filepath': 'b.jpg', 'tags': [], 'metadata': {}, 'lnv_sdk': ['y']})
    listed.lnv_sdk = None
    assert listed._array_fields_to_save() == {'lnv_sdk': None}  # an explicit unset is sent
    added = Sample('c.jpg', array_fields={'lnv_sdk': None, 'other': []})
    assert added.to_dict()['array_fields'] == {'other': []}  # never set is left out when adding
    assert 'array_fields' not in Sample('d.jpg', array_fields={'lnv_sdk': None}).to_dict()
    assert json.dumps(added.to_dict())  # sendable as it is


def test_one_predicate_says_which_lists_hold_no_value_when_empty():
    from backend import negation

    assert negation.EMPTY_IS_NO_VALUE == {'tags', '@local.tags'}
    assert not negation.empty_is_no_value('inferred_tags')
    assert datasets._value_sql('inferred_tags', None, None, {'tags', 'inferred_tags'}) == 'inferred_tags IS NOT NULL'
    assert datasets._value_sql('tags', None, None, {'tags', 'inferred_tags'}) == 'array_length(tags, 1) IS NOT NULL'


# --- The fix round (contract section 9, points 11 to 13) ---


async def version(r, dataset_id: int, name: str) -> int:
    made = await r.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/versions', json={'name': name})
    assert made.status_code == 200, made.text
    return made.json()['id']


async def get(r, path: str, **params) -> Any:
    response = await r.call(ADMIN, 'GET', path, params=params or None)
    assert response.status_code == 200, response.text
    return response.json()


async def post(r, path: str, body: dict) -> Any:
    response = await r.call(ADMIN, 'POST', path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def nothing_modified_since(r, version_id: int) -> None:
    """The diff of D's live rows against the version, and its list of modified samples, say no sample changed."""
    diff = await get(r, f'/api/datasets/{D}/versions/current-diff/{version_id}')
    assert diff['modified'] == 0, diff
    listed = await get(r, f'/api/datasets/{D}/versions/current-diff/{version_id}/samples', change_type='modified')
    assert listed['sample_ids'] == []


def test_after_a_restore_the_diff_against_the_version_restored_is_empty(regions_world):
    """Point 11: the status, the diff and the changed-sample lists read a version's list fields as the restore writes
    them, so a column the dataset does not register (which the restore leaves alone) is no change of the dataset's."""

    async def scenario(r):
        await other_made(r, 'lnv_stray')
        await r.dataset(D)
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'lnv_mine'})
        assert made.status_code == 200, made.text
        await add(r, D, [{'filepath': 'a.jpg', 'array_fields': {'lnv_mine': ['a']}}, {'filepath': 'b.jpg'}])
        v1 = await version(r, D, 'v1')
        await r.database.execute("UPDATE samples SET lnv_stray = '{stray}' WHERE dataset_id = $1", D)
        ids = {item['filepath']: item['id'] for item in (await query(r, D))['items']}
        patched = await r.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{D}/samples/{ids["a.jpg"]}',
            json={'array_fields': {'lnv_mine': ['changed']}},
        )
        assert patched.status_code == 200, patched.text
        restored = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/versions/{v1}/revert')
        assert restored.status_code == 200, restored.text
        assert await stored(r, D, 'lnv_mine') == {'a.jpg': ['a'], 'b.jpg': None}
        assert await stored(r, D, 'lnv_stray') == {'a.jpg': ['stray'], 'b.jpg': ['stray']}  # left alone
        await nothing_modified_since(r, v1)
        status = await get(r, f'/api/datasets/{D}/versions/status')
        assert status['has_changes'] is False and status['current_version']['id'] == v1, status

    regions_world(scenario)


def test_after_a_registration_no_version_from_before_says_a_sample_was_modified(regions_world):
    """Point 11 and point 13: a dataset registers a name another dataset made, so its empty lists from before become
    never set. Every version from before held no value of the field: the status, the diffs and the lists of changed
    samples say nothing changed, and the version's rows come back, filter and count with the field never set (and a
    column the dataset neither had nor has not at all)."""

    async def scenario(r):
        await other_made(r, 'lnv_reg')
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{OTHER}/array-fields', json={'field_name': 'lnv_none'})
        assert made.status_code == 200, made.text
        await r.dataset(D)
        await add(r, D, [{'filepath': f'old-{n}.jpg'} for n in range(3)])
        # The rows of D written before the upgrade: the old default in both columns.
        await r.database.execute("UPDATE samples SET lnv_reg = '{}', lnv_none = '{}' WHERE dataset_id = $1", D)
        v1 = await version(r, D, 'v1')
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'lnv_reg'})
        assert made.status_code == 200, made.text
        assert set((await stored(r, D, 'lnv_reg')).values()) == {None}
        await nothing_modified_since(r, v1)

        # The version's rows as browsed: the field never set, the column D never had not there.
        full = await get(r, f'/api/datasets/{D}/versions/{v1}/samples', full='true')
        assert len(full['items']) == 3
        for item in full['items']:
            assert item['array_fields'] == {'lnv_reg': None} and item['lnv_reg'] is None, item
            assert 'lnv_none' not in item
        queried = await post(r, f'/api/datasets/{D}/versions/{v1}/samples/query', {})
        assert [item['array_fields'] for item in queried['items']] == [{'lnv_reg': None}] * 3
        slim = await post(r, f'/api/datasets/{D}/versions/{v1}/samples/query', {'fields': ['lnv_reg']})
        assert [item['lnv_reg'] for item in slim['items']] == [None] * 3
        for operator, rows in (('is_null', 3), ('is_empty', 0), ('is_not_null', 0)):
            node = {'field': 'lnv_reg', 'operator': operator, 'value': None}
            found = await post(r, f'/api/datasets/{D}/versions/{v1}/samples/query', {'filters': node})
            assert len(found['items']) == rows, operator
            counted = await post(
                r,
                f'/api/datasets/{D}/versions/{v1}/aggregate',
                {'aggregations': [{'field': 'id', 'op': 'count'}], 'filters': node},
            )
            assert counted['results'][0]['value'] == rows, operator

        # The compare of two versions: each by what it had.
        v2 = await version(r, D, 'v2')
        pair = await get(r, f'/api/datasets/{D}/versions/diff/{v1}/{v2}')
        assert pair['modified'] == 0, pair
        listed = await get(r, f'/api/datasets/{D}/versions/diff/{v1}/{v2}/samples', change_type='modified')
        assert listed['sample_ids'] == []

        # The status's comparison of the rows: v1 as a version that recorded no settings (dataset_snapshots_equal
        # sees the new field otherwise), and v2 gone.
        assert (await r.call(ADMIN, 'DELETE', f'/api/datasets/{D}/versions/{v2}')).status_code == 200
        await r.database.execute('UPDATE dataset_versions SET dataset_snapshot = NULL WHERE id = $1', v1)
        status = await get(r, f'/api/datasets/{D}/versions/status')
        assert status['has_changes'] is False and status['current_version']['id'] == v1, status

    regions_world(scenario)


def test_only_a_version_that_did_not_record_the_registry_reads_the_registration_date(regions_world):
    """Point 12: a version that recorded the field registry and had no list field had none, whatever a registration's
    date says; only a version that did not record it reads by the date. And a registration that takes over a row of
    another kind dates from now."""

    async def scenario(r):
        await other_made(r, 'lnv_taken')
        await r.dataset(D)
        await add(r, D, [{'filepath': 'a.jpg'}, {'filepath': 'b.jpg'}])
        await r.database.execute("UPDATE samples SET lnv_taken = '{}' WHERE dataset_id = $1", D)  # the old default
        v1 = await version(r, D, 'v1')
        recorded = await r.database.fetchval('SELECT dataset_snapshot FROM dataset_versions WHERE id = $1', v1)
        assert recorded['field_registry_version'] == 1
        # A row of another kind under the name, from long before.
        await r.database.execute(
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, created_at) "
            "VALUES ($1, 'lnv_taken', 'unknown', 'system', '2000-01-01T00:00:00Z')",
            D,
        )
        made = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'lnv_taken'})
        assert made.status_code == 200, made.text
        dates = await r.database.fetchrow(
            "SELECT f.created_at AS registered, v.created_at AS taken FROM dataset_fields f, dataset_versions v "
            "WHERE f.dataset_id = $1 AND f.field_name = 'lnv_taken' AND v.id = $2",
            D,
            v1,
        )
        assert dates['registered'] > dates['taken']
        ids = {item['filepath']: item['id'] for item in (await query(r, D))['items']}
        for name, value in (('a.jpg', ['x']), ('b.jpg', [])):
            patched = await r.call(
                ADMIN,
                'PATCH',
                f'/api/datasets/{D}/samples/{ids[name]}',
                json={'array_fields': {'lnv_taken': value}},
            )
            assert patched.status_code == 200, patched.text
        # As if taken over before the rule: the date older than the version. v1 recorded the registry: no list field.
        await r.database.execute(
            "UPDATE dataset_fields SET created_at = '2000-01-01T00:00:00Z' WHERE dataset_id = $1 "
            "AND field_name = 'lnv_taken'",
            D,
        )
        restored = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/versions/{v1}/revert')
        assert restored.status_code == 200, restored.text
        assert await stored(r, D, 'lnv_taken') == {'a.jpg': None, 'b.jpg': None}
        await nothing_modified_since(r, v1)
        # The same version as one from before the registry was recorded: it had the field by the date.
        await r.database.execute('UPDATE dataset_versions SET dataset_snapshot = NULL WHERE id = $1', v1)
        restored = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/versions/{v1}/revert')
        assert restored.status_code == 200, restored.text
        assert await stored(r, D, 'lnv_taken') == {'a.jpg': [], 'b.jpg': []}
        await nothing_modified_since(r, v1)

    regions_world(scenario)


def test_a_second_request_joins_the_pending_job_whatever_the_dataset_counts_now(regions_world, monkeypatch):
    """Point 12: the pending job of the name is looked for before the dataset's size. A job walks D; samples are
    deleted until D is under the bound; a second request joins the job, and nothing is registered until it ends."""

    async def scenario(r):
        monkeypatch.setattr(list_fields, 'INLINE_SAMPLES', 2)
        monkeypatch.setattr(list_fields, 'STEP_IDS', 2)
        await other_made(r, 'lnv_join')
        await r.dataset(D)
        await add(r, D, [{'filepath': f'old-{n}.jpg'} for n in range(5)])
        await r.database.execute("UPDATE samples SET lnv_join = '{}' WHERE dataset_id = $1", D)
        made = await r.call(
            ADMIN,
            'POST',
            f'/api/datasets/{D}/array-fields',
            params={'wait': 'false'},
            json={'field_name': 'lnv_join'},
        )
        assert made.status_code == 202, made.text
        job_id = made.json()['job_id']
        await r.database.execute("DELETE FROM samples WHERE dataset_id = $1 AND filepath <> 'old-0.jpg'", D)
        joined = await r.call(
            ADMIN,
            'POST',
            f'/api/datasets/{D}/array-fields',
            params={'wait': 'false'},
            json={'field_name': 'lnv_join'},
        )
        assert joined.status_code == 202, joined.text
        assert joined.json()['job_id'] == job_id
        registered = "SELECT count(*) FROM dataset_fields WHERE dataset_id = $1 AND field_name = 'lnv_join'"
        assert await r.database.fetchval(registered, D) == 0
        refused = await add(r, D, [{'filepath': 'new.jpg', 'array_fields': {'lnv_join': []}}])
        assert refused.status_code == 409, refused.text
        job = await run_job(r, job_id)
        assert job['state'] == 'succeeded', job
        assert await r.database.fetchval(registered, D) == 1
        assert await stored(r, D, 'lnv_join') == {'old-0.jpg': None}

    regions_world(scenario)


def test_a_job_finding_the_field_registered_some_other_way_changes_no_row(regions_world, monkeypatch):
    """A registration that came in before the job was there to be joined: the job's walk then changes nothing, so an
    empty list written since the field became the dataset's is kept."""

    async def scenario(r):
        monkeypatch.setattr(list_fields, 'INLINE_SAMPLES', 2)
        monkeypatch.setattr(list_fields, 'STEP_IDS', 2)
        await other_made(r, 'lnv_race')
        await r.dataset(D)
        await add(r, D, [{'filepath': f'old-{n}.jpg'} for n in range(5)])
        await r.database.execute("UPDATE samples SET lnv_race = '{}' WHERE dataset_id = $1", D)
        made = await r.call(
            ADMIN,
            'POST',
            f'/api/datasets/{D}/array-fields',
            params={'wait': 'false'},
            json={'field_name': 'lnv_race'},
        )
        assert made.status_code == 202, made.text
        async with r.database.pool.acquire() as connection, connection.transaction():
            await list_fields.empty_to_never_set(connection, D, 'lnv_race')
            await list_fields.register(connection, D, 'lnv_race')
        written = await add(r, D, [{'filepath': 'real.jpg', 'array_fields': {'lnv_race': []}}])
        assert written.status_code == 200, written.text
        job = await run_job(r, made.json()['job_id'])
        assert job['state'] == 'succeeded', job
        assert (await stored(r, D, 'lnv_race'))['real.jpg'] == []

    regions_world(scenario)


def test_a_sample_added_again_keeps_what_it_leaves_out_and_unsets_what_it_sends_null(regions_world):
    """Point 12, last bullet (rules 1.5 and 1.7): one statement for the request, a list field a sample leaves out kept
    whatever the others name, `null` unsets it on a sample added again as on a new one, and new rows' ids in the
    order of the request."""

    async def scenario(r):
        await r.dataset(D)
        for name in ('lnv_again', 'lnv_beside'):
            made = await r.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': name})
            assert made.status_code == 200, made.text
        first = await add(
            r,
            D,
            [
                {'filepath': 'keep.jpg', 'array_fields': {'lnv_again': ['k']}},
                {'filepath': 'unset.jpg', 'array_fields': {'lnv_again': ['u'], 'lnv_beside': ['b']}},
                {'filepath': 'empty.jpg', 'array_fields': {'lnv_again': ['e'], 'lnv_beside': ['b']}},
                {'filepath': 'twice.jpg', 'array_fields': {'lnv_again': ['t']}},
            ],
        )
        assert first.status_code == 200, first.text
        again = await add(
            r,
            D,
            [
                {'filepath': 'new-1.jpg', 'array_fields': {'lnv_again': None}},
                {'filepath': 'keep.jpg'},
                {'filepath': 'unset.jpg', 'array_fields': {'lnv_again': None}},
                {'filepath': 'new-2.jpg'},
                {'filepath': 'empty.jpg', 'array_fields': {'lnv_again': []}},
                {'filepath': 'twice.jpg', 'array_fields': {'lnv_again': ['t2']}},
                {'filepath': 'new-3.jpg', 'array_fields': {'lnv_again': ['n']}},
                {'filepath': 'twice.jpg', 'array_fields': {'lnv_again': None}},  # the later copy wins
            ],
            'update',
        )
        assert again.status_code == 200, again.text
        assert await stored(r, D, 'lnv_again') == {
            'keep.jpg': ['k'],
            'unset.jpg': None,
            'empty.jpg': [],
            'twice.jpg': None,
            'new-1.jpg': None,
            'new-2.jpg': None,
            'new-3.jpg': ['n'],
        }
        assert await stored(r, D, 'lnv_beside') == {
            'keep.jpg': None,
            'unset.jpg': ['b'],
            'empty.jpg': ['b'],
            'twice.jpg': None,
            'new-1.jpg': None,
            'new-2.jpg': None,
            'new-3.jpg': None,
        }
        ids = again.json()['ids']
        assert ids[0] < ids[3] < ids[6]  # the new rows, in the order of the request
        assert ids[5] == ids[7]

    regions_world(scenario)


def test_the_sdk_save_brings_what_it_knows_as_never_set_to_what_it_sent(monkeypatch):
    """Point 11: read never set, give a list, save, set None, save: the second save unsets; a third sends nothing."""
    monkeypatch.setattr(Sample, '_known_array_fields', {*Sample._known_array_fields, 'lnv_sdk'})
    sent: list = []

    class Client:
        def update_sample(self, dataset_id, sample_id, **fields):
            sent.append(fields['array_fields'])

    class Dataset:
        _id = 1
        client = Client()

    sample = Sample.from_row({'id': 7, 'filepath': 'a.jpg', 'tags': [], 'metadata': {}, 'lnv_sdk': None})
    sample._dataset = Dataset()
    sample.lnv_sdk = ['x']
    sample.save()
    sample.lnv_sdk = None
    sample.save()
    sample.save()
    assert sent == [{'lnv_sdk': ['x']}, {'lnv_sdk': None}, None]
