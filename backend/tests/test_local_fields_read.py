"""Reading a mirror's own fields (docs/FOLLOWERS.md): the samples query returns them on a mirror's rows (and asked for
by name in a slim page), the SDK shows them as `sample['@local.<name>']`, colour-by reads them, the field listing
says who owns each field, and rename, clone and clear say they don't take them yet."""

import os

import pytest
from fastapi import HTTPException
from fridge.core.sample import Sample

from backend.routers import datasets
from backend.routers.embeddings import _build_include_fields
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.statistics_helpers import run_refresh

registry_database = registry_helpers.registry_database
ingest, USER = registry_helpers.ingest, registry_helpers.USER
needs_postgres = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def mirror(database):
    await ingest([{'crop': 'corn'}, {'crop': 'soy'}], dataset_id=2, prefix='s3://images/m')
    await ingest([{'crop': 'rye'}], dataset_id=1, prefix='s3://images/p')
    await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')
    await database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = 2")
    first = await database.fetchval('SELECT min(id) FROM samples WHERE dataset_id = 2')
    await datasets.update_sample(2, first, datasets.SampleUpdate(metadata={'@local.weeds': 3}), user=USER)
    await datasets.bulk_tag_samples(
        2,
        datasets.BulkTagRequest(field_name='@local.tags', sample_ids=[first], add_tags=['checked']),
        user=USER,
    )
    return first


@needs_postgres
def test_a_mirrors_rows_carry_its_own_fields(registry_database):
    async def scenario(database):
        first = await mirror(database)
        page = await datasets._list_samples_post(2, datasets.SamplesRequest(limit=10))
        rows = {item['id']: item for item in page['items']}
        assert (rows[first]['local_metadata'], rows[first]['local_tags']) == ({'weeds': 3}, ['checked'])
        assert next(item for item in rows.values() if item['id'] != first)['local_metadata'] == {}
        # Asked for by name in a slim page.
        slim = await datasets._list_samples_post(
            2,
            datasets.SamplesRequest(limit=10, slim=True, fields='id,@local.weeds,@local.tags'),
        )
        mine = next(item for item in slim['items'] if item['id'] == first)
        assert (mine['local_metadata'], mine['local_tags']) == ({'weeds': 3}, ['checked'])
        # An ordinary dataset's rows are as they were.
        other = await datasets._list_samples_post(1, datasets.SamplesRequest(limit=10))
        assert all('local_metadata' not in item for item in other['items'])

        # Who owns each field, and `@local.tags` shown as the array it is, with its values.
        await run_refresh(database, 2)
        fields = {field['name']: field for field in await datasets.get_dataset_schema(2)}
        assert (fields['crop']['owner'], fields['@local.weeds']['owner']) == ('primary', 'self')
        assert fields['@local.tags']['type'] == 'array' and fields['@local.tags'].get('values') == ['checked']

        for call in (
            datasets.rename_field(2, '@local.weeds', datasets.FieldRenameRequest(new_name='weeds2'), user=USER),
            datasets.clone_field(2, '@local.weeds', datasets.FieldRenameRequest(new_name='weeds2'), user=USER),
            datasets.clear_field(2, '@local.weeds', user=USER),
        ):
            with pytest.raises(HTTPException) as refused:
                await call
            assert refused.value.status_code == 400 and '@local.' in refused.value.detail

    registry_database(scenario)


def test_the_sdk_shows_a_mirrors_own_fields_by_their_names():
    row = {'id': 7, 'filepath': 'a.jpg', 'metadata': {'crop': 'corn'}, 'local_metadata': {'weeds': 3},
           'local_tags': ['checked']}  # fmt: skip
    sample = Sample.from_row(row)
    assert (sample['@local.weeds'], sample['@local.tags'], sample['crop']) == (3, ['checked'], 'corn')
    assert sample.local == {'weeds': 3, 'tags': ['checked']} and sample.metadata == {'crop': 'corn'}
    with pytest.raises(KeyError):
        sample['@local.kind']
    assert Sample.from_row({'id': 8, 'filepath': 'b.jpg', 'metadata': {}}).local == {}
    # Reading by name doesn't make a sample a sequence.
    with pytest.raises(TypeError):
        list(sample)


def test_colour_by_reads_a_local_field():
    qualified, unqualified = _build_include_fields(['@local.weeds', '@local.tags', '@local.a.b', 'crop'])
    assert qualified == [
        """s.local_metadata->>'weeds' as "@local_weeds\"""",
        's.local_tags as "@local_tags"',
        "s.metadata->>'crop' as crop",
    ]
    assert unqualified == ['"@local_weeds"', '"@local_tags"', 'crop']
