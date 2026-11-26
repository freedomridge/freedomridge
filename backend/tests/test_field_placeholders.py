"""A 'system' row of dataset_fields (0035) holds what no definition does: a built-in field's description, or a private
flag kept when a field went. A definition of the name takes it over; the field it stands for shows nowhere and costs
nothing until then (backend/field_registry.py, forget_definitions and register_fields)."""

import os

import pytest

from backend.tests import test_access_rule_spec, test_field_registry_postgres
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_bulk_jobs import finish

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
D = 10
ROW = 'SELECT origin, description, unit, values_private FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2'


async def a_dataset(w) -> list[int]:
    await w.dataset(D)
    samples = [{'filepath': f'p-{n}.jpg', 'metadata': {'note': f'n{n}', 'other': n}} for n in range(3)]
    added = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/samples', json={'samples': samples})
    assert added.is_success, added.text
    return added.json()['ids']


async def patch(w, field: str, body: dict) -> dict:
    response = await w.call(ADMIN, 'PATCH', f'/api/datasets/{D}/fields/{field}/description', json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def listed(w) -> dict:
    return {entry['name']: entry for entry in (await w.call(ADMIN, 'GET', f'/api/datasets/{D}/schema')).json()}


async def row(w, field: str):
    found = await w.database.fetchrow(ROW, D, field)
    return tuple(found) if found else None


def test_a_metadata_key_named_as_a_described_built_in_registers_and_keeps_the_description(world):
    async def scenario(w):
        ids = await a_dataset(w)
        await patch(w, 'created_at', {'description': 'When it arrived.', 'unit': 'UTC'})
        assert await row(w, 'created_at') == ('system', 'When it arrived.', 'UTC', False)
        # A sample PATCH (or bulk-set) may write such a key; it registers as on a dataset that described nothing.
        written = await w.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{D}/samples/{ids[0]}',
            json={'metadata': {'created_at': 'x'}},
        )
        assert written.is_success, written.text
        assert await row(w, 'created_at') == ('metadata', 'When it arrived.', 'UTC', False)
        assert (await listed(w))['created_at']['description'] == 'When it arrived.'
        # Cleared, the key goes and the built-in keeps its description.
        cleared = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/created_at/clear')
        assert cleared.is_success, cleared.text
        assert await row(w, 'created_at') == ('system', 'When it arrived.', 'UTC', False)
        assert (await listed(w))['created_at']['description'] == 'When it arrived.'

    world(scenario)


def test_a_flag_whose_field_never_comes_back_shows_nowhere_and_costs_nothing(world):
    async def scenario(w):
        await a_dataset(w)
        await patch(w, 'note', {'values_private': True, 'description': 'A note.'})
        cleared = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clear')
        assert cleared.is_success, cleared.text
        assert await row(w, 'note') == ('system', None, None, True)
        assert 'note' not in await listed(w)
        stats = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/field-stats', json={})
        assert 'note' not in {entry['name'] for entry in stats.json()}
        # Not a field of the dataset: no description for it, and the statistics measure nothing for it.
        refused = await w.call(ADMIN, 'PATCH', f'/api/datasets/{D}/fields/note/description', json={'unit': 'x'})
        assert refused.status_code == 404, refused.text
        await run_refresh(w.database, D)
        assert (
            await w.database.fetchval(
                "SELECT count(*) FROM dataset_field_stats WHERE dataset_id = $1 AND field_name = 'note'",
                D,
            )
            == 0
        )
        # Nor does a version hold it.
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{D}/versions', json={'name': 'v'})).is_success
        snapshot = await w.database.fetchval('SELECT dataset_snapshot FROM dataset_versions WHERE dataset_id = $1', D)
        assert 'note' not in {field['field_name'] for field in snapshot['fields']}

    world(scenario)


def test_a_bulk_clear_keeps_the_flag(world):
    async def scenario(w):
        await a_dataset(w)
        await patch(w, 'note', {'values_private': True})
        submitted = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clear', params={'background': 'true'})
        assert submitted.status_code == 202, submitted.text
        await finish(w.database)
        assert await row(w, 'note') == ('system', None, None, True)
        await w.call(
            ADMIN,
            'POST',
            f'/api/datasets/{D}/samples',
            json={'samples': [{'filepath': 'q.jpg', 'metadata': {'note': 'back'}}]},
        )
        assert await row(w, 'note') == ('metadata', None, None, True)

    world(scenario)


def test_a_field_a_revert_removes_keeps_its_flag_for_when_it_comes_back(world):
    async def scenario(w):
        ids = await a_dataset(w)
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{D}/versions', json={'name': 'v1'})).is_success
        version = await w.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', D)
        written = await w.call(ADMIN, 'PATCH', f'/api/datasets/{D}/samples/{ids[0]}', json={'metadata': {'late': 1}})
        assert written.is_success, written.text
        await patch(w, 'late', {'values_private': True})
        reverted = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/versions/{version}/revert')
        assert reverted.is_success, reverted.text
        assert await row(w, 'late') == ('system', None, None, True)
        assert 'late' not in await listed(w)
        again = await w.call(ADMIN, 'PATCH', f'/api/datasets/{D}/samples/{ids[1]}', json={'metadata': {'late': 2}})
        assert again.is_success, again.text
        assert await row(w, 'late') == ('metadata', None, None, True)

    world(scenario)


def test_a_rename_or_a_clone_onto_a_placeholder_keeps_what_it_held(world):
    async def scenario(w):
        await a_dataset(w)
        await patch(w, 'note', {'values_private': True})
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clear')).is_success
        await patch(w, 'other', {'description': 'Another number.'})
        renamed = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/other/rename', json={'new_name': 'note'})
        assert renamed.is_success, renamed.text
        # The field brings its description; the name's flag stays: either one's makes it private.
        assert await row(w, 'note') == ('metadata', 'Another number.', None, True)
        assert await row(w, 'other') is None  # a rename takes the definition along
        await patch(w, 'filepath', {'unit': 'path'})
        cloned = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clone', json={'new_name': 'filepath'})
        assert cloned.is_success, cloned.text
        assert await row(w, 'filepath') == ('metadata', 'Another number.', 'path', True)
        assert await row(w, 'note') == ('metadata', 'Another number.', None, True)  # a clone leaves the source

    world(scenario)


def test_a_name_whose_field_went_is_no_field_though_its_flag_is_kept(world):
    """Every field route answers for it what it answers for a name the dataset never had; the flag stays."""

    async def scenario(w):
        await a_dataset(w)
        await patch(w, 'note', {'values_private': True})
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clear')).is_success
        assert await row(w, 'note') == ('system', None, None, True)
        base = f'/api/datasets/{D}/fields'
        for name in ('note', 'never_was'):
            again = await w.call(ADMIN, 'POST', f'{base}/{name}/clear')
            assert (again.status_code, again.json()) == (200, {'updated_count': 0}), (name, again.text)
            answers = [
                await w.call(ADMIN, 'PATCH', f'{base}/{name}/type', json={'field_type': 'number'}),
                await w.call(ADMIN, 'POST', f'{base}/{name}/promote', params={'wait': 'false'}),
                await w.call(ADMIN, 'POST', f'{base}/{name}/rename', json={'new_name': f'{name}_2'}),
                await w.call(ADMIN, 'POST', f'{base}/{name}/clone', json={'new_name': f'{name}_3'}),
                await w.call(ADMIN, 'POST', f'{base}/{name}/display-type', json={'display_type': 'enum'}),
                await w.call(ADMIN, 'PUT', f'{base}/{name}/overlay_config', json={'opacity': 0.5}),
                await w.call(ADMIN, 'PATCH', f'{base}/{name}/description', json={'unit': 'x'}),
            ]
            assert [answer.status_code for answer in answers] == [404] * 7, (name, [a.text for a in answers])
        assert await row(w, 'note') == ('system', None, None, True)

    world(scenario)


def test_a_name_the_schema_lists_keeps_its_refusals(world):
    async def scenario(w):
        await a_dataset(w)
        for name in ('tags', 'filepath'):
            await patch(w, name, {'values_private': True})
            cleared = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/{name}/clear')
            assert cleared.status_code == 400, (name, cleared.text)
            assert await row(w, name) == ('system', None, None, True)

    world(scenario)
