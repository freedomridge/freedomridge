"""A field's description comes from whoever knows how its values are made: the server describes by name only the six
fields it makes itself.

Written from the contract (m1-contract-computed-field-descriptions, sections 1 and 3), not from the code, and tested
where a user meets it: the HTTP API signed in with real API tokens, and the assistant's describe_dataset called where
the doors call it, on the test database. The SDK's half (section 2) is tested against a running server in
python/fridge/test_sdk.py, the app's in frontend/e2e/field-descriptions.spec.ts. The rule:

- The server gives a description by name to exactly six fields, the ones every dataset has and only the server writes:
  id, filepath, created_at, last_modified_at, tags, media_type. Their texts are unchanged (BUILT_IN_TEXTS, as main had
  them before the change), and they read description_source "default" until someone writes one.
- Every other field has the description and unit someone wrote for it ("set"), or none (null, null, null). That holds
  for a field named as one of the thirteen the SDK's compute_metadata() and compute_metrics() write, whoever made it:
  the schema, the field statistics and describe_dataset say the same, and describe_dataset's undescribed_fields counts
  it.
- Nothing stored changes: a description someone wrote for one of those fields is kept.

Where the contract leaves a shape open, these tests read it this way:
- "Whoever made it" is a field of the user's own written by a plain upload (POST samples), its values on a scale of
  the user's own (brightness from 0 to 1, sizes in centimetres and kilobytes).
- A unit written alone leaves description_source null: it speaks of the description only, as the field-description
  spec reads it (test_field_descriptions_spec.py). Cleared again, the field reads null, null, null in every read, as
  any field with nothing set, and the PATCH answer says the same.
- "Nothing stored changes" is checked as: a description and a unit stored for brightness before (written straight into
  dataset_fields, where 0035 keeps them) read back as they were written, "set". "No migration" is not tested.
- undescribed_fields is the number of the schema's fields whose description_source is null (the assistant core spec's
  reading); with nothing written, that is every field but the six.
- Sections 3.1 and 3.2 are checked lightly, as the repository checks docs elsewhere (test_assistant_guests_spec.py):
  docs/SDK.md and docs/DATASET_MANAGEMENT.md no longer say a field of your own called brightness gets the metric's
  description, docs/SDK.md's field descriptions section shows COMPUTED_FIELD_DESCRIPTIONS applied with
  set_field_description and says near it that applying them replaces (or overwrites) what was written, the lines
  holding `or ""` for the unit (contract 2.3; that they apply them only to the fields a dataset has, and the sentence
  on an interrupted run, are not checked), and CHANGELOG.md's Unreleased section no
  longer says the server describes the fields compute_metadata() and compute_metrics() write. Section 3.3 (the
  describe_dataset tool's own words) is not tested.
- Section 3.4: "an example cut" on a name is the name followed by a comparison operator (`brightness < 40`), looked for
  in assistant.instructions() and in each of the thirteen tools of assistant.tools(): its description and every text
  of its input schema (a property's description is part of what a model reads). The sentence about a field's scale
  is checked by sense: a sentence of the instructions that speaks of a cut, a threshold or a scale and names `range`
  and describe_dataset, or field_summary.
"""

import os
import re
from typing import Any, Optional

import pytest

from backend import assistant
from backend.tests import test_access_rule_spec, test_field_registry_postgres
from backend.tests.test_access_rule_spec import ADMIN, World
from backend.tests.test_assistant_core_spec import open_door, result
from backend.tests.test_assistant_guests_spec import section
from backend.tests.test_field_descriptions_spec import (
    METADATA_FIELDS,
    METRIC_FIELDS,
    NOTHING,
    SYSTEM_FIELDS,
    described,
    four,
    reading,
    readings,
)
from backend.tests.test_upgrade_paths import ROOT

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

D = 40  # a dataset whose fields of the user's own are named as the computed ones
COMPUTED = METADATA_FIELDS + METRIC_FIELDS  # the thirteen compute_metadata() and compute_metrics() write
# The server's descriptions of the fields every dataset has, word for word as main had them before the change.
BUILT_IN_TEXTS = {
    'id': "The sample's number on this server, unique across all of its datasets.",
    'filepath': "Where the sample's file is, such as an s3:// address or a path on the server's disk.",
    'created_at': (
        "When the sample was added: the time given by whoever added it, or the server's clock when none was given."
    ),
    'last_modified_at': (
        'When the sample was last edited, such as a change to its metadata, tags or location; new vectors and '
        "renaming or clearing a field across the dataset don't count."
    ),
    'tags': 'Labels that people and scripts put on the sample to group, filter and select samples.',
    'media_type': (
        "The kind of file the sample is, such as image, video or audio: given when it was added, or else guessed from "
        "the file's extension."
    ),
}


def own_metadata(index: int) -> dict[str, Any]:
    """A sample's metadata from the user's own pipeline: fields named as the thirteen computed ones, each on a scale of
    its own, and one more field of its own."""
    return {
        'width': 12.5 + index,  # centimetres on the print
        'height': 9.0,
        'aspect_ratio': 1.39,
        'megapixels': 0.5,
        'file_size_bytes': 2.5 + index,  # kilobytes
        'brightness': 0.25 * (index + 1),  # from 0 to 1
        'blurriness': 0.1 * index,
        'contrast': 0.8,
        'entropy': 0.3,
        'salt_and_pepper': 0.01,
        'saturation': 0.4,
        'pct_underexposed': 0.02,
        'pct_overexposed': 0.03,
        'plot': f'P{index}',
    }


async def own_dataset(w: World, dataset_id: int = D) -> list[int]:
    """A private dataset (the admins have it) of three samples written by a plain upload; their ids."""
    await w.dataset(dataset_id)
    samples = [
        {'filepath': f's3://own-pipeline/{dataset_id}/{index}.jpg', 'metadata': own_metadata(index)}
        for index in range(3)
    ]
    response = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/samples', json={'samples': samples})
    assert response.is_success, response.text
    return response.json()['ids']


async def tool_fields(dataset_id: int = D) -> tuple[dict[str, dict[str, Any]], int]:
    """describe_dataset's fields of the dataset, every page, by name, and its undescribed_fields (the same on every
    page)."""
    fields: dict[str, dict[str, Any]] = {}
    counted = set()
    offset: Optional[int] = 0
    while offset is not None:
        page = await result(ADMIN, 'describe_dataset', {'dataset': dataset_id, 'offset': offset})
        counted.add(page['undescribed_fields'])
        for field in page['fields']:
            assert field['name'] not in fields, field['name']
            fields[field['name']] = field
        offset = page['next_offset']
    assert len(counted) == 1, counted
    return fields, counted.pop()


def three(field: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(description, unit, description_source) of one of describe_dataset's fields."""
    return field['description'], field['unit'], field['description_source']


# --- 1.1 The six ---


def test_the_six_built_in_fields_keep_the_servers_description_and_read_default(world):
    async def scenario(w):
        await own_dataset(w)
        await open_door(w)
        entries = await readings(w, D)
        assert set(BUILT_IN_TEXTS) <= set(entries['schema']), sorted(entries['schema'])
        for where, fields in entries.items():
            for name, text in BUILT_IN_TEXTS.items():
                if name in fields:
                    assert four(fields[name], where) == (text, None, False, 'default'), (where, name, fields[name])
        tool, _ = await tool_fields()
        for name, text in BUILT_IN_TEXTS.items():
            assert three(tool[name]) == (text, None, 'default'), (name, tool[name])

    world(scenario)


# --- 1.2, 1.3 Every other field ---


def test_a_field_of_your_own_named_as_a_computed_one_has_no_description_anywhere_and_is_counted_undescribed(world):
    async def scenario(w):
        await own_dataset(w)
        await open_door(w)
        entries = await readings(w, D)
        for name in COMPUTED:
            assert name in entries['schema'] and name in entries['POST field-stats'], name
        for where, fields in entries.items():
            for name in COMPUTED:
                if name in fields:
                    assert four(fields[name], where) == NOTHING, (where, name, fields[name])
        tool, undescribed = await tool_fields()
        for name in COMPUTED:
            assert (*three(tool[name]), tool[name]['values_private']) == (None, None, None, False), (name, tool[name])
        schema = entries['schema']
        without = {name for name, entry in schema.items() if entry['description_source'] is None}
        assert set(COMPUTED) <= without, sorted(set(COMPUTED) - without)
        assert without == set(schema) - set(SYSTEM_FIELDS), sorted(without ^ (set(schema) - set(SYSTEM_FIELDS)))
        assert undescribed == len(without), (undescribed, sorted(without))

    world(scenario)


def test_what_someone_writes_for_such_a_field_is_kept_and_cleared_again_it_has_none(world):
    async def scenario(w):
        await own_dataset(w)
        await open_door(w)
        _, undescribed = await tool_fields()
        written = {name: f'Our own {name}, from the field pipeline.' for name in COMPUTED}
        for name, text in written.items():
            answer = await described(w, name, {'description': text, 'unit': 'ours'}, dataset_id=D)
            assert (answer['description'], answer['unit']) == (text, 'ours'), (name, answer)
            assert await reading(w, name, D) == (text, 'ours', False, 'set'), name
        tool, now = await tool_fields()
        for name, text in written.items():
            assert three(tool[name]) == (text, 'ours', 'set'), (name, tool[name])
        assert now == undescribed - len(COMPUTED), (now, undescribed)
        # Cleared: none, not a description by name.
        for name in COMPUTED:
            answer = await described(w, name, {'description': None, 'unit': None}, dataset_id=D)
            assert (answer['description'], answer['unit']) == (None, None), (name, answer)
            assert await reading(w, name, D) == NOTHING, name
        tool, now = await tool_fields()
        for name in COMPUTED:
            assert three(tool[name]) == (None, None, None), (name, tool[name])
        assert now == undescribed, (now, undescribed)
        # Each on its own (a description alone gets no unit by its name, a unit alone no description), and each cleared
        # on its own.
        await described(w, 'brightness', {'description': 'How bright, from 0 to 1.'}, dataset_id=D)
        assert await reading(w, 'brightness', D) == ('How bright, from 0 to 1.', None, False, 'set')
        await described(w, 'height', {'description': 'Print height.'}, dataset_id=D)
        assert await reading(w, 'height', D) == ('Print height.', None, False, 'set')
        await described(w, 'height', {'description': None}, dataset_id=D)
        assert await reading(w, 'height', D) == NOTHING
        await described(w, 'width', {'unit': 'cm'}, dataset_id=D)
        assert await reading(w, 'width', D) == (None, 'cm', False, None)
        await described(w, 'entropy', {'description': 'Ours.', 'unit': 'nats'}, dataset_id=D)
        await described(w, 'entropy', {'description': ''}, dataset_id=D)
        assert await reading(w, 'entropy', D) == (None, 'nats', False, None)
        await described(w, 'brightness', {'description': '   '}, dataset_id=D)
        assert await reading(w, 'brightness', D) == NOTHING
        await described(w, 'width', {'unit': ''}, dataset_id=D)
        assert await reading(w, 'width', D) == NOTHING
        await described(w, 'entropy', {'unit': None}, dataset_id=D)
        assert await reading(w, 'entropy', D) == NOTHING

    world(scenario)


# --- 1.4 Nothing stored changes ---


def test_a_description_stored_for_such_a_field_before_reads_as_it_was_written(world):
    async def scenario(w):
        await own_dataset(w)
        await open_door(w)
        text = 'Mean grey level from our camera, from 0 to 1.'
        stored = await w.database.fetchval(
            'UPDATE dataset_fields SET description = $2, unit = $3 '
            "WHERE dataset_id = $1 AND field_name = 'brightness' RETURNING field_name",
            D,
            text,
            'fraction',
        )
        assert stored == 'brightness'
        assert await reading(w, 'brightness', D) == (text, 'fraction', False, 'set')
        tool, _ = await tool_fields()
        assert three(tool['brightness']) == (text, 'fraction', 'set'), tool['brightness']

    world(scenario)


# --- 3. Words ---


def test_the_docs_no_longer_describe_a_field_by_its_name_and_show_how_to_apply_the_sdks_descriptions():
    sdk = (ROOT / 'docs/SDK.md').read_text()
    rest = (ROOT / 'docs/DATASET_MANAGEMENT.md').read_text()
    for name, text in (('docs/SDK.md', sdk), ('docs/DATASET_MANAGEMENT.md', rest)):
        flat = ' '.join(text.split())
        assert "gets the metric's description" not in flat, name
        assert 'a field of the same name gets these too' not in flat.lower(), name
    descriptions = section(sdk, '### Field descriptions')
    assert 'COMPUTED_FIELD_DESCRIPTIONS' in descriptions, 'docs/SDK.md does not show COMPUTED_FIELD_DESCRIPTIONS'
    assert re.search(r'COMPUTED_FIELD_DESCRIPTIONS[\s\S]{0,400}set_field_description', descriptions), descriptions
    first = descriptions.index('COMPUTED_FIELD_DESCRIPTIONS')
    around = descriptions[max(0, first - 400) : first + 1000]
    assert re.search(r'replac|overwrit', around, re.I), (
        f'nothing says applying them replaces what was written: {around}'
    )
    assert 'or ""' in around, f'the lines do not clear a unit with `unit or ""`: {around}'
    unreleased = ' '.join(section((ROOT / 'CHANGELOG.md').read_text(), '## Unreleased').split())
    old = r'server describes the fields every dataset has and those `compute_metadata\(\)` and `compute_metrics\(\)`'
    assert not re.search(old, unreleased), 'CHANGELOG.md still says the server describes the computed fields'


# --- 3.4 The assistant is not taught a scale by an example ---

# One of the thirteen names, then a comparison: an example cut on it.
CUT = re.compile(r'\b(' + '|'.join(COMPUTED) + r')\b\s*(<=|>=|==|!=|=|<|>)')


def strings(value: Any) -> list[str]:
    """Every text in a tool's input schema: its descriptions, examples and the rest."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in strings(item)]
    return []


def test_no_instruction_or_tool_description_gives_an_example_cut_on_a_computed_name():
    tools = assistant.tools()
    assert len(tools) == 13, [tool.name for tool in tools]
    texts = {'instructions': assistant.instructions()}
    for tool in tools:
        texts[f'{tool.name} description'] = tool.description
        texts[f'{tool.name} input schema'] = '\n'.join(strings(tool.input_schema))
    cuts = {where: [found.group(0) for found in CUT.finditer(text)] for where, text in texts.items()}
    found = {where: cut for where, cut in cuts.items() if cut}
    assert not found, found


def test_the_instructions_say_to_read_a_number_fields_range_or_summary_before_choosing_a_cut():
    text = ' '.join(assistant.instructions().split())
    sentences = re.split(r'(?<=[.!?])\s+', text)
    told = [
        sentence
        for sentence in sentences
        if re.search(r'\b(cut|cuts|threshold|scale)\b', sentence, re.I)
        and (('range' in sentence and 'describe_dataset' in sentence) or 'field_summary' in sentence)
    ]
    assert told, 'no sentence of the instructions says to read a field\'s range or summary before choosing a cut'
