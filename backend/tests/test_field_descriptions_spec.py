"""A description, a unit and a private flag for each field of a dataset.

Written from the contract, not from the code, and tested where a user meets it: the HTTP API, signed in with real API
tokens, on the test database. The rule:

- Stored per field of a dataset: `description` (at most 1,000 characters after trimming; empty means none), `unit` (at
  most 40 characters; empty means none), `values_private` (a boolean, false unless set; stored and returned only).
- PATCH /api/datasets/{id}/fields/{field_name}/description takes any of {"description", "unit", "values_private"}.
  Keys left out are unchanged; null or "" clears. It needs edit in the dataset: below it 403 "This needs edit access to
  the dataset", no access 403 "Access denied for this dataset", not signed in 401. A dataset being deleted refuses it
  with 409, as other writes. A field the dataset doesn't have is 404; text over a limit or holding a control character
  is 422. It answers the field's three values. A field the dataset's schema (GET /api/datasets/{id}/schema) lists
  takes them, the fields every dataset has included; a name it doesn't list is 404. A new metadata key written
  through the API, by any route that writes one, is listed and takes them.
- Every field in GET /api/datasets/{id}/schema and in the field-stats responses carries `description`, `unit`,
  `values_private` and `description_source`: "set" when someone wrote it, "default" when the server supplies it, null
  when there is none.
- The server supplies a description for the fields every dataset has, and for no other field, while nobody set one:
  setting one replaces it ("set"), clearing it brings it back ("default"). A field named as one the SDK's
  compute_metadata() or compute_metrics() writes has none by its name (m1-contract-computed-field-descriptions): what
  someone set, or nothing. The server supplies no unit: a unit someone set is the field's, cleared it is none.
  `description_source` speaks of the description only.
- On a mirror, the description and unit of a field the primary owns are refused as a change of that field's type is
  refused there (409); the mirror's own `@local.` fields take them. values_private is this instance's, for any field.
- One dataset's values never show on another dataset's field of the same name.
- Migration 0035 adds `description`, `unit` and `values_private` to dataset_fields, NULL, NULL and false for every
  existing field, and changes nothing else about fields (types, display types, promoted flags, owners).

Versions are left out: whether a version saves descriptions is decided from what versions already save about fields.

Where the contract leaves a shape open, these tests read it this way: a field with nothing set reads null for its
description and unit, and a description that is all spaces is empty once trimmed, so it clears. The PATCH answer is
checked where its values are plain (a field with no default description), not for a cleared field that has a default.
Control characters are the ones that aren't whitespace (NUL, BEL, ESC, DEL and a C1 one); tab and newline are not
asserted either way.
"""

import json
import os
import re
from typing import Any, Optional

import httpx
import pytest

from backend.routers import datasets
from backend.tests import test_access_rule_spec, test_field_registry_postgres, test_upgrade_paths
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_access_enforcement_spec import DENIED, SEALED, detail, needs, set_share
from backend.tests.test_access_rule_spec import ADMIN, GUEST, POWER, World
from backend.tests.test_bulk_jobs import finish

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
database = test_upgrade_paths.database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

ADMIN_USER = {'id': ADMIN, 'role': 'admin', 'username': f'user-{ADMIN}'}
D, E = 10, 11  # the dataset, and another with fields of the same names
KEYS = ('description', 'unit', 'values_private', 'description_source')
NOTHING: tuple[Optional[str], Optional[str], bool, Optional[str]] = (None, None, False, None)  # nothing set, no default
SYSTEM_FIELDS = ('id', 'filepath', 'media_type', 'created_at', 'last_modified_at', 'tags')
METADATA_FIELDS = ('width', 'height', 'aspect_ratio', 'megapixels', 'file_size_bytes')  # compute_metadata()
METRIC_FIELDS = (  # compute_metrics()
    'brightness',
    'blurriness',
    'contrast',
    'entropy',
    'salt_and_pepper',
    'saturation',
    'pct_underexposed',
    'pct_overexposed',
)
COMPUTED_FIELDS = METADATA_FIELDS + METRIC_FIELDS  # no description or unit by their names
# The units the server gave by name before the computed-field-descriptions contract; it gives no field a unit now.
UNITS_BEFORE = {
    'width': 'px',
    'height': 'px',
    'megapixels': 'MP',
    'file_size_bytes': 'bytes',
    'entropy': 'bits',
    'salt_and_pepper': '%',
    'pct_underexposed': '%',
    'pct_overexposed': '%',
}
# What a field's entry says about its values; describing a field changes none of it.
VALUE_KEYS = ('type', 'values', 'min', 'max', 'declared_type', 'display_type', 'is_promoted', 'origin', 'owner')
CONTROL = ('\x00', '\x07', '\x1b', '\x7f', '\x9b')


def metadata(index: int) -> dict[str, Any]:
    """A sample's metadata: fields of the user's own, and the fields compute_metadata() and compute_metrics() write."""
    computed: dict[str, Any] = {
        'width': 640 + index,
        'height': 480,
        'aspect_ratio': 1.3333,
        'megapixels': 0.307,
        'file_size_bytes': 1000 + index,
    }
    computed.update({name: 10.0 + position + index for position, name in enumerate(METRIC_FIELDS)})
    return {'label': ('cat', 'dog')[index % 2], 'score': 0.25 * (index + 1), 'note': f'n{index}', **computed}


async def open_dataset(w: World, dataset_id: int = D) -> list[int]:
    """A private dataset with three samples (the admins have it, nobody else); their ids."""
    await w.dataset(dataset_id)
    samples = [
        {'filepath': f'sample-{dataset_id}-{index}.jpg', 'tags': ['seen'], 'metadata': metadata(index)}
        for index in range(3)
    ]
    added = await datasets.add_samples(dataset_id, {'samples': samples}, user=ADMIN_USER)
    return added['ids']


def path(dataset_id: int, field: str) -> str:
    return f'/api/datasets/{dataset_id}/fields/{field}/description'


async def describe(
    w: World,
    field: str,
    body: dict[str, Any],
    user: Optional[int] = ADMIN,
    dataset_id: int = D,
) -> httpx.Response:
    return await w.call(user, 'PATCH', path(dataset_id, field), json=body)


async def described(w: World, field: str, body: dict[str, Any], dataset_id: int = D) -> dict[str, Any]:
    """An admin's PATCH that must succeed; its answer, which holds the field's three values."""
    response = await describe(w, field, body, dataset_id=dataset_id)
    assert response.status_code == 200, (field, body, response.text)
    answer = response.json()
    assert {'description', 'unit', 'values_private'} <= set(answer), answer
    return answer


def by_name(entries: Any, where: str) -> dict[str, dict[str, Any]]:
    assert isinstance(entries, list), (where, entries)
    found: dict[str, dict[str, Any]] = {}
    for entry in entries:
        assert entry['name'] not in found, (where, entry['name'])
        found[entry['name']] = entry
    return found


async def readings(w: World, dataset_id: int = D, fields: Optional[list[str]] = None) -> dict[str, dict]:
    """{where: {field name: entry}} for each read the contract names: the schema and the field-stats responses (GET,
    POST, a facet request for `fields`, and that request streamed)."""
    got: dict[str, dict] = {}
    response = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/schema')
    assert response.status_code == 200, response.text
    got['schema'] = by_name(response.json(), 'schema')
    response = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/field-stats')
    assert response.status_code == 200, response.text
    got['GET field-stats'] = by_name(response.json(), 'GET field-stats')
    response = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/field-stats', json={})
    assert response.status_code == 200, response.text
    got['POST field-stats'] = by_name(response.json(), 'POST field-stats')
    wanted = fields if fields is not None else sorted(got['POST field-stats'])
    response = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/field-stats', json={'fields': wanted})
    assert response.status_code == 200, response.text
    got['facet request'] = by_name(response.json(), 'facet request')
    response = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/field-stats',
        json={'fields': wanted, 'stream': True},
    )
    assert response.status_code == 200, response.text
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert lines and lines[-1] == {'done': True}, lines[-1:]
    for line in lines[:-1]:
        assert 'error' not in line and 'refusal' not in line, line
    got['streamed facet request'] = by_name(lines[:-1], 'streamed facet request')
    return got


def four(entry: dict[str, Any], where: str) -> tuple:
    missing = [key for key in KEYS if key not in entry]
    assert not missing, f'{where}: {entry["name"]} has no {missing}'
    return tuple(entry[key] for key in KEYS)


async def reading(w: World, field: str, dataset_id: int = D) -> tuple:
    """(description, unit, values_private, description_source) of the field, the same in every read."""
    seen = {
        where: four(entries[field], where)
        for where, entries in (await readings(w, dataset_id, [field])).items()
        if field in entries
    }
    assert 'schema' in seen and 'POST field-stats' in seen, (field, sorted(seen))
    assert len(set(seen.values())) == 1, (field, seen)
    return next(iter(seen.values()))


async def schema_reading(w: World, field: str, dataset_id: int = D) -> tuple:
    response = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/schema')
    assert response.status_code == 200, response.text
    return four(by_name(response.json(), 'schema')[field], 'schema')


def a_default(text: Any) -> str:
    """A default description: one non-empty sentence, as stored text may be."""
    assert isinstance(text, str), text
    assert text and text == text.strip() and len(text) <= 1000, text
    assert '\n' not in text and not any(char in text for char in CONTROL), text
    assert re.search(r'[.!?]\s+[A-Z]', text) is None, f'more than one sentence: {text!r}'
    return text


# --- Reading ---


def test_every_field_of_every_read_carries_the_four_values(world):
    async def scenario(w):
        await open_dataset(w)
        made = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'labels_x'})
        assert made.is_success, made.text
        entries_by_read = await readings(w)
        assert set(SYSTEM_FIELDS) <= set(entries_by_read['schema']), sorted(entries_by_read['schema'])
        for where, entries in entries_by_read.items():
            for entry in entries.values():
                values = four(entry, where)
                assert isinstance(values[2], bool), (where, entry)
                assert values[3] in (None, 'set', 'default'), (where, entry)

    world(scenario)


def test_a_field_nobody_described_has_none_of_the_values(world):
    async def scenario(w):
        await open_dataset(w)
        for field in ('label', 'score', 'note'):
            assert await reading(w, field) == NOTHING, field

    world(scenario)


# --- Writing ---


def test_each_value_is_set_alone_and_the_keys_left_out_stay(world):
    async def scenario(w):
        await open_dataset(w)
        answer = await described(w, 'note', {'description': 'What the scout wrote.'})
        assert (answer['description'], answer['unit'], answer['values_private']) == (
            'What the scout wrote.',
            None,
            False,
        )
        assert await reading(w, 'note') == ('What the scout wrote.', None, False, 'set')
        answer = await described(w, 'note', {'unit': 'words'})
        assert (answer['description'], answer['unit'], answer['values_private']) == (
            'What the scout wrote.',
            'words',
            False,
        )
        assert await reading(w, 'note') == ('What the scout wrote.', 'words', False, 'set')
        answer = await described(w, 'note', {'values_private': True})
        assert (answer['description'], answer['unit'], answer['values_private']) == (
            'What the scout wrote.',
            'words',
            True,
        )
        assert await reading(w, 'note') == ('What the scout wrote.', 'words', True, 'set')
        answer = await described(w, 'note', {'values_private': False})
        assert answer['values_private'] is False
        assert await reading(w, 'note') == ('What the scout wrote.', 'words', False, 'set')
        # All three at once, and the description trimmed.
        answer = await described(
            w,
            'score',
            {'description': '  How sure the model is.  ', 'unit': '%', 'values_private': True},
        )
        assert (answer['description'], answer['unit'], answer['values_private']) == (
            'How sure the model is.',
            '%',
            True,
        )
        assert await reading(w, 'score') == ('How sure the model is.', '%', True, 'set')
        assert await reading(w, 'note') == ('What the scout wrote.', 'words', False, 'set')  # one field's values

    world(scenario)


def test_null_or_empty_clears(world):
    async def scenario(w):
        await open_dataset(w)
        for cleared in (None, '', '   '):
            await described(w, 'note', {'description': 'Scout notes.', 'unit': 'words'})
            answer = await described(w, 'note', {'description': cleared})
            assert (answer['description'], answer['unit']) == (None, 'words'), cleared
            assert await reading(w, 'note') == (None, 'words', False, None), cleared
        for cleared in (None, ''):
            await described(w, 'note', {'description': 'Scout notes.', 'unit': 'words'})
            answer = await described(w, 'note', {'unit': cleared})
            assert (answer['description'], answer['unit']) == ('Scout notes.', None), cleared
            assert await reading(w, 'note') == ('Scout notes.', None, False, 'set'), cleared
        await described(w, 'note', {'description': None, 'unit': None, 'values_private': False})
        assert await reading(w, 'note') == NOTHING

    world(scenario)


def test_describing_a_field_changes_nothing_else_about_it(world):
    """values_private is stored and returned only: the field's values are listed as before, in every read."""

    async def scenario(w):
        await open_dataset(w)
        before = await readings(w)
        await described(w, 'label', {'description': 'What the picture shows.', 'unit': 'class', 'values_private': True})
        after = await readings(w)
        assert sorted(after) == sorted(before)
        for where in before:
            assert sorted(after[where]) == sorted(before[where]), where
            old, new = before[where]['label'], after[where]['label']
            assert {key: new.get(key) for key in VALUE_KEYS} == {key: old.get(key) for key in VALUE_KEYS}, where
            assert four(new, where) == ('What the picture shows.', 'class', True, 'set'), where

    world(scenario)


def test_text_over_a_limit_or_with_a_control_character_is_422_and_changes_nothing(world):
    async def scenario(w):
        await open_dataset(w)
        longest = 'a' * 1000
        assert (await described(w, 'note', {'description': longest}))['description'] == longest
        assert (await described(w, 'note', {'description': f'   {longest}   '}))['description'] == longest
        assert (await described(w, 'note', {'unit': 'u' * 40}))['unit'] == 'u' * 40
        await described(w, 'note', {'description': 'Scout notes.', 'unit': 'words', 'values_private': True})
        kept = ('Scout notes.', 'words', True, 'set')
        refused: list[dict[str, Any]] = [
            {'description': 'a' * 1001},
            {'description': 'a' * 1001, 'values_private': False},
            {'unit': 'u' * 41},
            {'unit': 'u' * 41, 'description': 'Other notes.'},
            *[{'description': f'Scout{char}notes.'} for char in CONTROL],
            *[{'unit': f'wo{char}rds'} for char in CONTROL],
        ]
        for body in refused:
            response = await describe(w, 'note', body)
            assert response.status_code == 422, (body, response.text)
            assert await schema_reading(w, 'note') == kept, body
        assert await reading(w, 'note') == kept

    world(scenario)


def test_a_field_the_dataset_does_not_have_is_404(world):
    async def scenario(w):
        await open_dataset(w)
        await open_dataset(w, E)
        await w.database.execute(
            "UPDATE samples SET metadata = metadata || '{\"only_there\": 1}' WHERE dataset_id = $1",
            E,
        )
        for field in ('nope', 'Note', 'only_there'):
            response = await describe(w, field, {'description': 'Anything.'})
            assert response.status_code == 404, (field, response.text)
        entries = (await readings(w))['schema']
        assert 'nope' not in entries and 'only_there' not in entries

    world(scenario)


def test_the_fields_the_schema_lists_take_values_and_a_key_it_does_not_list_is_404(world):
    async def scenario(w):
        ids = await open_dataset(w)
        made = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/array-fields', json={'field_name': 'labels_x'})
        assert made.is_success, made.text
        # A key written straight into the samples table, which nothing registered: listed nowhere, so a 404.
        await w.database.execute(
            "INSERT INTO samples (dataset_id, filepath, media_type, metadata) VALUES ($1, 'loose.jpg', 'image', $2)",
            D,
            {'loose': 7},
        )
        assert 'loose' not in await schema_names(w)
        for body in ({'description': 'A key nobody registered.', 'unit': 'kg'}, {'values_private': True}):
            response = await describe(w, 'loose', body)
            assert response.status_code == 404, (body, response.text)
        # Written through the API later, it is listed, with nothing stored by the refused requests.
        written = await w.call(ADMIN, 'PATCH', f'/api/datasets/{D}/samples/{ids[0]}', json={'metadata': {'loose': 8}})
        assert written.status_code == 200, written.text
        assert 'loose' in await schema_names(w)
        assert await reading(w, 'loose') == NOTHING
        for field in (*SYSTEM_FIELDS, 'score', 'labels_x', 'loose'):
            assert field in await schema_names(w), field
            text = f'About {field}.'
            answer = await described(w, field, {'description': text, 'unit': 'u', 'values_private': True})
            assert (answer['description'], answer['unit'], answer['values_private']) == (text, 'u', True), field
            assert await reading(w, field) == (text, 'u', True, 'set'), field

    world(scenario)


async def schema_names(w: World, dataset_id: int = D) -> set[str]:
    response = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/schema')
    assert response.status_code == 200, response.text
    return {entry['name'] for entry in response.json()}


async def ok(response: httpx.Response) -> Any:
    assert response.is_success, response.text
    return response.json()


async def near_duplicate_vectors(w: World, ids: list[int]) -> None:
    """Vectors for the samples, the first two all but the same."""
    vectors = [[1.0, 0.0, 0.0, 0.0], [1.0, 0.01, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
    body = [{'sample_id': s, 'vector': vectors[i], 'field_name': 'clip'} for i, s in enumerate(ids)]
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/embeddings', json=body))


# Each route that can put a new metadata key on a sample: it writes one, and returns the key's name.


async def by_adding_samples(w: World, ids: list[int]) -> str:
    body = {'samples': [{'filepath': 'added.jpg', 'metadata': {'added_key': 1}}]}
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/samples', json=body))
    return 'added_key'


async def by_adding_samples_that_update(w: World, ids: list[int]) -> str:
    body = {'samples': [{'filepath': f'sample-{D}-0.jpg', 'metadata': {'late_key': 'x'}}], 'on_duplicate': 'update'}
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/samples', json=body))
    return 'late_key'


async def by_editing_a_sample(w: World, ids: list[int]) -> str:
    await ok(await w.call(ADMIN, 'PATCH', f'/api/datasets/{D}/samples/{ids[0]}', json={'metadata': {'edited_key': 2}}))
    return 'edited_key'


async def by_bulk_set(w: World, ids: list[int]) -> str:
    body = {'sample_ids': ids[:2], 'metadata': {'bulk_key': True}}
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/samples/bulk-set', json=body))
    return 'bulk_key'


async def by_bulk_set_per_sample(w: World, ids: list[int]) -> str:
    updates = [{'sample_id': ids[0], 'metadata': {'per_key': 1}}, {'sample_id': ids[1], 'metadata': {'per_key': 2}}]
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/samples/bulk-set', json={'updates': updates}))
    return 'per_key'


async def by_bulk_set_by_filter(w: World, ids: list[int]) -> str:
    """A filter's bulk set is a job: run it here, as the runner would."""
    body = {'filters': {'field': 'label', 'operator': '=', 'value': 'cat'}, 'metadata': {'job_key': 'y'}}
    response = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/samples/bulk-set', json=body)
    assert response.status_code in (200, 202), response.text
    if response.status_code == 202:
        await finish(w.database)
    return 'job_key'


async def by_renaming_a_field(w: World, ids: list[int]) -> str:
    body = {'new_name': 'renamed_note'}
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/rename', json=body))
    return 'renamed_note'


async def by_cloning_a_field(w: World, ids: list[int]) -> str:
    body = {'new_name': 'cloned_note'}
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clone', json=body))
    return 'cloned_note'


async def by_reverting_to_a_version(w: World, ids: list[int]) -> str:
    """note cleared after the version, then brought back by its revert."""
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/versions', json={'name': 'with notes'}))
    version = await w.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', D)
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clear'))
    assert 'note' not in await schema_names(w)
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/versions/{version}/revert'))
    return 'note'


async def by_computing_uniqueness(w: World, ids: list[int]) -> str:
    await near_duplicate_vectors(w, ids)
    body = {'field': 'clip', 'k': 1, 'allow_seq_scan': True, 'metadata_field': 'unusual'}
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/quality/uniqueness/compute', json=body))
    return 'unusual'


async def by_computing_representativeness(w: World, ids: list[int]) -> str:
    await near_duplicate_vectors(w, ids)
    centroid = await ok(
        await w.call(ADMIN, 'POST', f'/api/datasets/{D}/quality/representativeness/centroid', json={'field': 'clip'}),
    )
    body = {'field': 'clip', 'centroid': centroid['centroid'], 'metadata_field': 'typical'}
    await ok(await w.call(ADMIN, 'POST', f'/api/datasets/{D}/quality/representativeness/compute', json=body))
    return 'typical'


async def by_grouping_duplicates(w: World, ids: list[int]) -> str:
    await near_duplicate_vectors(w, ids)
    base = f'/api/datasets/{D}/quality/duplicates'
    body = {'field': 'clip', 'token': 'near-copies', 'allow_seq_scan': True}
    pairs = await ok(await w.call(ADMIN, 'POST', f'{base}/compute', json=body))
    assert pairs['pairs_written'] >= 1, pairs
    await ok(
        await w.call(ADMIN, 'POST', f'{base}/finalize', json={'token': 'near-copies', 'group_field': 'copy_group'}),
    )
    return 'copy_group'


NEW_KEY_ROUTES = [
    pytest.param(by_adding_samples, id='POST samples'),
    pytest.param(by_adding_samples_that_update, id='POST samples, on_duplicate update'),
    pytest.param(by_editing_a_sample, id='PATCH samples/{sample_id}'),
    pytest.param(by_bulk_set, id='POST samples/bulk-set'),
    pytest.param(by_bulk_set_per_sample, id='POST samples/bulk-set, per sample'),
    pytest.param(by_bulk_set_by_filter, id='POST samples/bulk-set, by filter (a job)'),
    pytest.param(by_renaming_a_field, id='POST fields/{field_name}/rename'),
    pytest.param(by_cloning_a_field, id='POST fields/{field_name}/clone'),
    pytest.param(by_reverting_to_a_version, id='POST versions/{version_id}/revert'),
    pytest.param(by_computing_uniqueness, id='POST quality/uniqueness/compute'),
    pytest.param(by_computing_representativeness, id='POST quality/representativeness/compute'),
    pytest.param(by_grouping_duplicates, id='POST quality/duplicates/finalize'),
]


@pytest.mark.parametrize('write', NEW_KEY_ROUTES)
def test_a_new_key_written_through_the_api_is_listed_and_takes_a_description(world, write):
    async def scenario(w):
        ids = await open_dataset(w)
        key = await write(w, ids)
        assert key in await schema_names(w), f'{key} is not listed by the schema after {write.__name__}'
        assert key in {entry['name'] for entry in (await readings(w, fields=[key]))['POST field-stats'].values()}
        answer = await described(w, key, {'description': f'Written {write.__name__}.', 'unit': 'u'})
        assert (answer['description'], answer['unit']) == (f'Written {write.__name__}.', 'u'), answer
        assert await reading(w, key) == (f'Written {write.__name__}.', 'u', False, 'set')

    world(scenario)


# --- Defaults ---


def test_the_server_describes_the_fields_every_dataset_has_and_no_other(world):
    async def scenario(w):
        await open_dataset(w)
        entries = await readings(w)
        for where, fields in entries.items():
            for field in SYSTEM_FIELDS:
                if field not in fields:
                    continue
                description, unit, values_private, source = four(fields[field], where)
                assert (source, values_private) == ('default', False), (where, field, fields[field])
                a_default(description)
                assert unit is None, (where, field, unit)
            for field in COMPUTED_FIELDS:  # named as the SDK's computed fields, but nobody described them
                if field in fields:
                    assert four(fields[field], where) == NOTHING, (where, field, fields[field])
        for field in SYSTEM_FIELDS + COMPUTED_FIELDS:
            assert field in entries['schema'] or field in entries['POST field-stats'], field
        for field in ('label', 'score', 'note'):
            assert await reading(w, field) == NOTHING, field

    world(scenario)


def test_setting_a_description_replaces_the_default_and_clearing_it_brings_the_default_back(world):
    async def scenario(w):
        await open_dataset(w)
        for field in SYSTEM_FIELDS:
            default, unit, _, source = await reading(w, field)
            assert (source, unit) == ('default', None), field
            a_default(default)
            for cleared in (None, '', '   '):
                assert (await described(w, field, {'description': 'Ours.'}))['description'] == 'Ours.'
                assert await reading(w, field) == ('Ours.', None, False, 'set'), field
                await described(w, field, {'description': cleared})
                assert await reading(w, field) == (default, None, False, 'default'), (field, cleared)
        # A field named as one the SDK computes has no default: cleared, it has none.
        for field in ('width', 'file_size_bytes', 'brightness', 'pct_overexposed'):
            assert await reading(w, field) == NOTHING, field
            for cleared in (None, '', '   '):
                assert (await described(w, field, {'description': 'Ours.'}))['description'] == 'Ours.'
                assert await reading(w, field) == ('Ours.', None, False, 'set'), field
                await described(w, field, {'description': cleared})
                assert await reading(w, field) == NOTHING, (field, cleared)
        # A unit or the private flag alone leaves the default description as it is.
        default = (await reading(w, 'filepath'))[0]
        await described(w, 'filepath', {'unit': 'level'})
        assert await reading(w, 'filepath') == (default, 'level', False, 'default')
        await described(w, 'created_at', {'values_private': True})
        created_at = await reading(w, 'created_at')
        assert (created_at[1], created_at[2], created_at[3]) == (None, True, 'default')

    world(scenario)


def test_no_field_has_a_unit_by_its_name_and_a_set_unit_cleared_leaves_none(world):
    async def scenario(w):
        await open_dataset(w)
        for field in UNITS_BEFORE:
            assert await reading(w, field) == NOTHING, field
            for cleared in (None, ''):
                await described(w, field, {'unit': 'counts'})
                assert await reading(w, field) == (None, 'counts', False, None), field
                await described(w, field, {'unit': cleared})
                assert await reading(w, field) == NOTHING, (field, cleared)
        # A description set, no unit set: no unit shows beside it.
        await described(w, 'entropy', {'description': 'How much the pixels vary.'})
        assert await reading(w, 'entropy') == ('How much the pixels vary.', None, False, 'set')
        await described(w, 'entropy', {'unit': 'nats'})
        assert await reading(w, 'entropy') == ('How much the pixels vary.', 'nats', False, 'set')
        await described(w, 'entropy', {'description': None, 'unit': None})
        assert await reading(w, 'entropy') == NOTHING
        # The fields the server describes have no unit either; one set beside the default is kept, and cleared, none.
        for field in SYSTEM_FIELDS + COMPUTED_FIELDS:
            assert (await reading(w, field))[1] is None, field
        default = (await reading(w, 'tags'))[0]
        await described(w, 'tags', {'unit': 'labels'})
        assert await reading(w, 'tags') == (default, 'labels', False, 'default')
        await described(w, 'tags', {'unit': None})
        assert await reading(w, 'tags') == (default, None, False, 'default')

    world(scenario)


# --- Who may ---

# (who, user (None: not signed in), the user's own share in the dataset, the level that gives them (None: no access))
CALLERS = [
    ('an admin', ADMIN, None, 'manage'),
    ('a manager', POWER, 'manage', 'manage'),
    ('an editor', POWER, 'edit', 'edit'),
    ('a tagger', POWER, 'tag', 'tag'),
    ('a viewer', POWER, 'view', 'view'),
    ('a guest with a manage share, which is view for a guest', GUEST, 'manage', 'view'),
    ('a power user with no access', POWER, None, None),
    ('nobody signed in', None, None, None),
]


async def only_share(w: World, user: Optional[int], share: Optional[str]) -> None:
    """The dataset shared with `user` at `share` and with no other power user or guest."""
    for someone in (POWER, GUEST):
        await set_share(w, D, someone, None)
    if share is not None and user is not None:
        await set_share(w, D, user, share)


def test_each_role_and_level(world):
    async def scenario(w):
        await open_dataset(w)
        for who, user, share, level in CALLERS:
            await only_share(w, user, share)
            if user is not None:
                assert await w.level(user, D) == level, who
        state: tuple[Optional[str], Optional[str], bool, Optional[str]] = NOTHING
        for who, user, share, level in CALLERS:
            await only_share(w, user, share)
            bodies: list[dict[str, Any]] = [
                {'description': f'Written by {who}.', 'unit': 'cm', 'values_private': True},
                {'values_private': False},
            ]
            for body in bodies:
                response = await describe(w, 'note', body, user=user)
                if level in ('edit', 'manage'):
                    assert response.status_code == 200, (who, response.text)
                    state = (f'Written by {who}.', 'cm', body['values_private'], 'set')
                elif user is None:
                    assert response.status_code == 401, (who, response.text)
                elif level is None:
                    assert (response.status_code, detail(response)) == (403, DENIED), (who, response.text)
                else:
                    assert (response.status_code, detail(response)) == (403, needs('edit')), (who, response.text)
                assert await schema_reading(w, 'note') == state, who

    world(scenario)


def test_a_dataset_being_deleted_refuses_with_409_as_other_writes(world):
    async def scenario(w):
        await open_dataset(w)
        await w.share(D, POWER, 'edit')
        deleting = await w.call(ADMIN, 'DELETE', f'/api/datasets/{D}')
        assert deleting.is_success, deleting.text
        for user in (ADMIN, POWER):
            other = await w.call(user, 'PATCH', f'/api/datasets/{D}', json={'description': 'Late.'})
            assert (other.status_code, detail(other)) == (409, SEALED), (user, other.text)
            for body in ({'description': 'Late.'}, {'unit': 'cm'}, {'values_private': True}):
                response = await describe(w, 'note', body, user=user)
                assert (response.status_code, detail(response)) == (409, SEALED), (user, body, response.text)
        assert await schema_reading(w, 'note') == NOTHING

    world(scenario)


# --- Mirrors ---


def test_on_a_mirror_only_its_own_fields_take_a_description_and_any_field_its_private_flag(world):
    async def scenario(w):
        ids = await open_dataset(w)
        # A mirror of a dataset on another server, its samples copied before it became one.
        await w.database.execute(
            'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES ($1, 9, 0)',
            D,
        )
        await w.database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = $1", D)
        own = await w.call(
            ADMIN,
            'PATCH',
            f'/api/datasets/{D}/samples/{ids[0]}',
            json={'metadata': {'@local.weeds': 3}},
        )
        assert own.status_code == 200, own.text
        # Changing the type of a field the primary owns is refused here: a 409.
        retyped = await w.call(ADMIN, 'PATCH', f'/api/datasets/{D}/fields/score/type', json={'field_type': 'string'})
        assert retyped.status_code == 409, retyped.text
        brightness = await reading(w, 'brightness')
        for field, before in (('score', NOTHING), ('brightness', brightness)):
            for body in (
                {'description': 'Changed here.'},
                {'unit': 'cm'},
                {'description': 'Changed here.', 'unit': 'cm'},
            ):
                response = await describe(w, field, body)
                assert response.status_code == retyped.status_code, (field, body, response.text)
                assert await schema_reading(w, field) == before, (field, body)
        # The mirror's own field.
        answer = await described(w, '@local.weeds', {'description': 'Weeds counted here.', 'unit': 'plants'})
        assert (answer['description'], answer['unit']) == ('Weeds counted here.', 'plants')
        assert await reading(w, '@local.weeds') == ('Weeds counted here.', 'plants', False, 'set')
        # The private flag, on any field.
        created_at = await reading(w, 'created_at')
        for field in ('score', 'brightness', '@local.weeds', 'id', 'created_at'):
            assert (await described(w, field, {'values_private': True}))['values_private'] is True, field
            assert (await reading(w, field))[2] is True, field
        assert await reading(w, 'score') == (None, None, True, None)
        assert await reading(w, 'brightness') == (None, None, True, None)  # no description by its name
        assert await reading(w, 'created_at') == (created_at[0], created_at[1], True, 'default')  # the default stays

    world(scenario)


# --- One dataset's ---


def test_one_datasets_values_never_show_on_anothers_field_of_the_same_name(world):
    async def scenario(w):
        await open_dataset(w)
        await open_dataset(w, E)
        others = {field: await reading(w, field, E) for field in ('brightness', 'id')}
        assert {field: values[3] for field, values in others.items()} == {'brightness': None, 'id': 'default'}
        for field in ('note', 'brightness', 'id'):
            await described(
                w,
                field,
                {'description': f'The first dataset says {field}.', 'unit': 'u', 'values_private': True},
            )
            assert (await reading(w, field))[3] == 'set'
        assert await reading(w, 'note', E) == NOTHING
        for field, before in others.items():
            assert await reading(w, field, E) == before, field
        await described(w, 'note', {'description': 'The other dataset says note.'}, dataset_id=E)
        assert await reading(w, 'note', E) == ('The other dataset says note.', None, False, 'set')
        assert await reading(w, 'note') == ('The first dataset says note.', 'u', True, 'set')

    world(scenario)


# --- A built-in field's description and a field of your own of the same name ---


async def own_key(w: World, dataset_id: int, ids: list[int], route: str, name: str) -> httpx.Response:
    """A sample given a metadata key named as a built-in field, through one route."""
    value = f'mine-{name}'
    base = f'/api/datasets/{dataset_id}'
    if route == 'POST samples':
        body = {'samples': [{'filepath': f'own-{name}.jpg', 'metadata': {name: value}}]}
        return await w.call(ADMIN, 'POST', f'{base}/samples', json=body)
    if route == 'PATCH sample':
        return await w.call(ADMIN, 'PATCH', f'{base}/samples/{ids[0]}', json={'metadata': {name: value}})
    if route == 'bulk-set':
        return await w.call(
            ADMIN,
            'POST',
            f'{base}/samples/bulk-set',
            json={'sample_ids': ids[:2], 'metadata': {name: value}},
        )
    updates = [{'sample_id': ids[1], 'metadata': {name: value}}]
    return await w.call(ADMIN, 'POST', f'{base}/samples/bulk-set', json={'updates': updates})


async def what_shows(w: World, dataset_id: int, name: str) -> dict[str, Any]:
    """What the dataset shows of `name`: its schema entry (but its description), the sample metadata holding it,
    and what filters on it select, by filepath."""
    schema = await w.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/schema')
    assert schema.status_code == 200, schema.text
    entries = [
        {key: value for key, value in entry.items() if key not in KEYS and key not in ('min', 'max', 'values')}
        for entry in schema.json()
        if entry['name'] == name
    ]
    rows = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/samples/query', json={'limit': 100})
    assert rows.status_code == 200, rows.text
    held = sorted((same(item['filepath']), (item.get('metadata') or {}).get(name)) for item in rows.json()['items'])
    filtered = {}
    # (`id contains` is left out: the server fails on it, described or not: operator does not exist: integer ~~* text)
    checks = [('=', f'mine-{name}'), ('exists', None)] + ([] if name == 'id' else [('contains', 'mine')])
    for operator, value in checks:
        node = {'field': name, 'operator': operator, 'value': value}
        response = await w.call(
            ADMIN,
            'POST',
            f'/api/datasets/{dataset_id}/samples/query',
            json={'filters': node, 'limit': 100},
        )
        filtered[operator] = (
            response.status_code,
            sorted(same(item['filepath']) for item in response.json()['items'])
            if response.status_code == 200
            else same(response.text),
        )
    return {'entries': entries, 'held': held, 'filtered': filtered}


OWN_KEY_ROUTES = ('POST samples', 'PATCH sample', 'bulk-set', 'bulk-set, per sample')


def same(text: str) -> str:
    """Text with the datasets' own numbers taken out, to compare two datasets' answers."""
    return re.sub(r'\b3\d\d\b', '<dataset>', re.sub(r'sample-\d+-', 'sample-', text))


@pytest.mark.parametrize('name', SYSTEM_FIELDS)
def test_describing_a_built_in_field_never_hides_a_field_of_your_own_with_its_name(world, name):
    """The same steps on two datasets, one with the built-in field described: what is listed, held and filtered is
    the same in both, a refused key is refused in both, and the description stays."""

    async def scenario(w):
        for route in OWN_KEY_ROUTES:
            described_id, plain_id = 300 + 2 * OWN_KEY_ROUTES.index(route), 301 + 2 * OWN_KEY_ROUTES.index(route)
            described_ids = await open_dataset(w, described_id)
            plain_ids = await open_dataset(w, plain_id)
            text = f'The built-in {name}.'
            await described(w, name, {'description': text, 'unit': 'u'}, dataset_id=described_id)
            on_described = await own_key(w, described_id, described_ids, route, name)
            on_plain = await own_key(w, plain_id, plain_ids, route, name)
            assert on_described.status_code == on_plain.status_code, (route, on_described.text, on_plain.text)
            if not on_plain.is_success:  # a name the API refuses as a metadata key: refused alike
                assert same(on_described.text) == same(on_plain.text), route
            shown = await what_shows(w, described_id, name)
            assert shown == await what_shows(w, plain_id, name), (route, shown)
            assert (await schema_reading(w, name, described_id))[:2] == (text, 'u'), route

    world(scenario)


# --- values_private when a field goes and comes back ---


async def private_again(w: World, field: str, dataset_id: int = D) -> None:
    names = {entry['name'] for entry in (await w.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/schema')).json()}
    assert field in names, f'{field} is not listed again'
    assert (await schema_reading(w, field, dataset_id))[2] is True, field


async def add_one(w: World, metadata: dict[str, Any], filepath: str, dataset_id: int = D) -> None:
    added = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/samples',
        json={'samples': [{'filepath': filepath, 'metadata': metadata}]},
    )
    assert added.is_success, added.text


async def gone(w: World, field: str, dataset_id: int = D) -> None:
    names = {entry['name'] for entry in (await w.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/schema')).json()}
    assert field not in names, f'{field} is still listed'


def test_a_private_field_cleared_and_written_again_is_still_private(world):
    async def scenario(w):
        await open_dataset(w)
        await described(w, 'note', {'values_private': True})
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clear')).is_success
        await gone(w, 'note')
        await add_one(w, {'note': 'back'}, 'back.jpg')
        await private_again(w, 'note')

    world(scenario)


def test_a_private_field_whose_last_sample_is_deleted_and_comes_back_is_still_private(world):
    async def scenario(w):
        ids = await open_dataset(w)
        written = await w.call(ADMIN, 'PATCH', f'/api/datasets/{D}/samples/{ids[0]}', json={'metadata': {'solo': 1}})
        assert written.is_success, written.text
        await described(w, 'solo', {'values_private': True})
        assert (await w.call(ADMIN, 'DELETE', f'/api/datasets/{D}/samples/{ids[0]}')).is_success
        await run_refresh(w.database, D)  # (the field may stay listed until its statistics are counted again)
        await add_one(w, {'solo': 2}, 'solo-again.jpg')
        await private_again(w, 'solo')

    world(scenario)


def test_a_private_field_renamed_away_and_back_is_still_private(world):
    async def scenario(w):
        await open_dataset(w)
        await described(w, 'note', {'values_private': True})
        for old, new in (('note', 'note_elsewhere'), ('note_elsewhere', 'note')):
            renamed = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/{old}/rename', json={'new_name': new})
            assert renamed.is_success, renamed.text
        await private_again(w, 'note')

    world(scenario)


def test_a_private_field_cleared_and_brought_back_by_a_revert_is_still_private(world):
    async def scenario(w):
        await open_dataset(w)
        await described(w, 'note', {'values_private': True})
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{D}/versions', json={'name': 'v1'})).is_success
        version = await w.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', D)
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clear')).is_success
        await gone(w, 'note')
        reverted = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/versions/{version}/revert')
        assert reverted.is_success, reverted.text
        await private_again(w, 'note')

    world(scenario)


def test_a_revert_to_a_version_from_before_the_mark_keeps_the_field_private(world):
    async def scenario(w):
        await open_dataset(w)
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{D}/versions', json={'name': 'before'})).is_success
        version = await w.database.fetchval('SELECT id FROM dataset_versions WHERE dataset_id = $1', D)
        await described(w, 'note', {'values_private': True})
        reverted = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/versions/{version}/revert')
        assert reverted.is_success, reverted.text
        await private_again(w, 'note')
        # And when the field was cleared after the mark.
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clear')).is_success
        reverted = await w.call(ADMIN, 'POST', f'/api/datasets/{D}/versions/{version}/revert')
        assert reverted.is_success, reverted.text
        await private_again(w, 'note')

    world(scenario)


def test_on_a_mirror_its_own_private_field_gone_and_back_is_still_private(world):
    async def scenario(w):
        ids = await open_dataset(w)
        await w.database.execute(
            'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES ($1, 9, 0)',
            D,
        )
        await w.database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = $1", D)
        sample = f'/api/datasets/{D}/samples/{ids[0]}'
        assert (await w.call(ADMIN, 'PATCH', sample, json={'metadata': {'@local.weeds': 3}})).is_success
        await described(w, '@local.weeds', {'values_private': True})
        await described(w, 'score', {'values_private': True})
        removed = await w.call(ADMIN, 'PATCH', sample, json={'remove_metadata_keys': ['@local.weeds']})
        assert removed.is_success, removed.text
        await gone(w, '@local.weeds')
        assert (await w.call(ADMIN, 'PATCH', sample, json={'metadata': {'@local.weeds': 4}})).is_success
        await private_again(w, '@local.weeds')
        await private_again(w, 'score')

    world(scenario)


# --- A private field that is gone ---


async def field_action(w: World, action: str, name: str) -> httpx.Response:
    base = f'/api/datasets/{D}/fields/{name}'
    if action == 'clear':
        return await w.call(ADMIN, 'POST', f'{base}/clear')
    if action == 'set its type':
        return await w.call(ADMIN, 'PATCH', f'{base}/type', json={'field_type': 'string'})
    if action == 'promote':
        return await w.call(ADMIN, 'POST', f'{base}/promote', params={'wait': 'false'})
    if action == 'rename from':
        return await w.call(ADMIN, 'POST', f'{base}/rename', json={'new_name': 'moved_here'})
    if action == 'clone from':
        return await w.call(ADMIN, 'POST', f'{base}/clone', json={'new_name': 'copied_here'})
    return await w.call(ADMIN, 'PATCH', f'{base}/description', json={'description': 'Anything.'})


FIELD_ACTIONS = ('clear', 'set its type', 'promote', 'rename from', 'clone from', 'describe')


def test_a_private_field_that_is_gone_behaves_like_a_name_with_no_field(world):
    """A cleared private field is unlisted: every field action answers it as it answers a name that never existed,
    its description is a 404, and the remembered mark comes back with the field."""

    async def scenario(w):
        await open_dataset(w)
        await described(w, 'note', {'values_private': True})
        assert (await w.call(ADMIN, 'POST', f'/api/datasets/{D}/fields/note/clear')).is_success
        await gone(w, 'note')
        differ = []
        for action in FIELD_ACTIONS:
            cleared = await field_action(w, action, 'note')
            never = await field_action(w, action, 'never_there')
            same = (cleared.status_code, cleared.text.replace('note', '<name>')) == (
                never.status_code,
                never.text.replace('never_there', '<name>'),
            )
            if not same:
                differ.append(
                    f'{action}: cleared {cleared.status_code} {cleared.text[:100]} / '
                    f'never {never.status_code} {never.text[:100]}',
                )
            if action == 'describe' and cleared.status_code != 404:
                differ.append(f'describe: {cleared.status_code} {cleared.text[:120]}, not 404')
        await gone(w, 'note')
        names = {entry['name'] for entry in (await w.call(ADMIN, 'GET', f'/api/datasets/{D}/schema')).json()}
        assert not {'moved_here', 'copied_here'} & names, names
        # The mark is still remembered: written again, the field is private.
        await add_one(w, {'note': 'back'}, 'back.jpg')
        await private_again(w, 'note')
        # A name the schema lists keeps its own refusal: the built-in tags field still can't be cleared, and keeps its
        # description and its values.
        await described(w, 'tags', {'description': 'What reviewers marked.'})
        built_in = await field_action(w, 'clear', 'tags')
        assert 400 <= built_in.status_code < 500 and built_in.status_code != 404, built_in.text
        assert (await schema_reading(w, 'tags'))[0] == 'What reviewers marked.'
        tagged = await w.database.fetchval(
            "SELECT count(*) FROM samples WHERE dataset_id = $1 AND 'seen' = ANY(tags)",
            D,
        )
        assert tagged == 3, tagged
        assert not differ, '\n'.join(differ)

    world(scenario)
