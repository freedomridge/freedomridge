"""The five older names a metadata key can't have (`id`, `dataset_id`, `filepath`, `created_at`, `last_modified_at`)
are refused on edits too, as `tags` and `media_type` are: an edit of a sample, every bulk set, a quality score's field,
a rename or clone onto one. A sample that already stores one stays editable: an edit that sends the key back with
exactly the stored value goes through and leaves it as it is (Sample.save() sends the whole metadata), a different
value or a new one is refused with nothing written, and removing it is allowed. A bulk edit queued before the refusal
leaves such a key out, and a rename queued onto one fails without writing."""

import json
import os

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.field_registry import unchanged_column_keys
from backend.routers import datasets, quality
from backend.tests import test_field_registry_postgres
from backend.tests.test_bulk_jobs import claim, finish, step, submitted
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
FIVE = ('id', 'dataset_id', 'filepath', 'created_at', 'last_modified_at')


@pytest.mark.parametrize(
    ('sent', 'stored', 'kept'),
    [
        ({'created_at': '1999-03-04T05:06:07Z'}, {'created_at': '1999-03-04T05:06:07Z'}, {'created_at'}),
        ({'created_at': '1999-03-04T05:06:07+00:00'}, {'created_at': '1999-03-04T05:06:07Z'}, set()),  # other text
        ({'created_at': 1, 'id': 7.0}, {'created_at': 1.0, 'id': 7}, {'created_at', 'id'}),  # numbers by value
        ({'dataset_id': 1}, {'dataset_id': True}, set()),  # true is no number
        ({'last_modified_at': None}, {'last_modified_at': None}, {'last_modified_at'}),
        ({'last_modified_at': None}, {}, set()),  # a new key, even null
        ({'filepath': {'a': None, 'b': [1, 2]}}, {'filepath': {'b': [1, 2], 'a': None}}, {'filepath'}),
        ({'filepath': {'b': [2, 1]}}, {'filepath': {'b': [1, 2]}}, set()),  # a list keeps its order
        ({'tags': 'old', 'media_type': 'scan'}, {'tags': 'old', 'media_type': 'scan'}, {'tags', 'media_type'}),
        ({'n': 1, 'heading': 2}, {'n': 1, 'heading': 2}, set()),  # ordinary keys aren't its business
        ({'created_at': 'x'}, ['created_at'], set()),  # metadata that is no object holds no key
    ],
)
def test_a_key_sent_back_with_exactly_the_stored_value_is_kept(sent, stored, kept):
    assert unchanged_column_keys(sent, stored) == kept


@pytest.mark.parametrize('name', FIVE)
def test_bulk_sets_and_quality_fields_refuse_the_five_at_the_request(name):
    for build in (
        lambda: datasets.BulkSetRequest(sample_ids=[1], metadata={name: 1}),
        lambda: datasets.BulkSetUpdate(sample_id=1, metadata={name: 1}),
        lambda: quality.NormalizeRequest(metadata_field=name, lo=0, span=1),
        lambda: quality.UniquenessRequest(field='clip', metadata_field=name),
    ):
        with pytest.raises(ValidationError, match=rf"\['{name}'\] name sample columns"):
            build()
    # An edit of one sample is checked against what the sample stores (update_sample), not here.
    assert datasets.SampleUpdate(metadata={name: 1}).metadata == {name: 1}


async def metadata(database, sample_id):
    return await database.fetchval('SELECT metadata FROM samples WHERE id = $1', sample_id)


async def edit(sample_id, **fields):
    return await datasets.update_sample(1, sample_id, datasets.SampleUpdate(**fields), USER)


@needs_postgres
def test_a_sample_that_stores_one_stays_editable_and_a_new_or_changed_one_is_refused(registry_database):
    async def scenario(database):
        ids = (await ingest([{'n': 0}, {'n': 1}]))['ids']
        stored = {'created_at': '1999-03-04T05:06:07Z', 'id': 7, 'tags': 'old'}
        await database.execute('UPDATE samples SET metadata = metadata || $2::jsonb WHERE id = $1', ids[0], stored)
        names = "SELECT field_name FROM dataset_fields WHERE field_name = ANY($1::text[])"

        # Sent back as stored (Sample.save() sends the whole metadata), another key changed: through, the keys kept.
        await edit(ids[0], metadata={**stored, 'id': 7.0, 'n': 10})
        assert await metadata(database, ids[0]) == {**stored, 'n': 10}
        assert await database.fetch(names, [*FIVE, 'tags', 'media_type']) == []  # not registered by being sent back

        # A changed value, or one the sample didn't have: refused, nothing written.
        stamp = await database.fetchval('SELECT last_modified_at FROM samples WHERE id = $1', ids[0])
        for sample_id, sent in ((ids[0], {'created_at': '2001-01-01', 'n': 11}), (ids[1], {'filepath': 'x', 'n': 12})):
            with pytest.raises(HTTPException) as refused:
                await edit(sample_id, metadata=sent)
            assert refused.value.status_code == 422
            assert 'name sample columns' in json.dumps(refused.value.detail)
        assert await metadata(database, ids[0]) == {**stored, 'n': 10}
        assert await metadata(database, ids[1]) == {'n': 1}
        assert await database.fetchval('SELECT last_modified_at FROM samples WHERE id = $1', ids[0]) == stamp
        with pytest.raises(HTTPException, match=r"\['created_at', 'id'\]"):
            await edit(ids[1], metadata={'id': 1, 'created_at': 2})

        # Removing one is allowed.
        await edit(ids[0], remove_metadata_keys=['created_at'])
        assert await metadata(database, ids[0]) == {'id': 7, 'tags': 'old', 'n': 10}

    registry_database(scenario)


@needs_postgres
def test_bulk_edits_queued_before_the_refusal_leave_the_five_out(registry_database):
    async def scenario(database):
        ids = (await ingest([{'n': 0}, {'n': 1}]))['ids']
        old = {'created_at': 'old', 'filepath': 'old'}  # as an older release queued them
        job = submitted(
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(background=True, sample_ids=[ids[0]], metadata={'kept': 1}),
                USER,
            ),
        )
        await database.execute(
            "UPDATE jobs SET args = jsonb_set(args, '{metadata}', args->'metadata' || $2::jsonb) WHERE id = $1",
            job,
            old,
        )
        await finish(database)
        updates = [datasets.BulkSetUpdate(sample_id=ids[1], metadata={'kept': 2})]
        job = submitted(
            await datasets.bulk_set_metadata(1, datasets.BulkSetRequest(background=True, updates=updates), USER),
        )
        await database.execute('UPDATE bulk_job_targets SET patch = patch || $2::jsonb WHERE job_id = $1', job, old)
        await finish(database)

        assert [await metadata(database, sample_id) for sample_id in ids] == [{'n': 0, 'kept': 1}, {'n': 1, 'kept': 2}]
        fields = {row['field_name'] for row in await database.fetch('SELECT field_name FROM dataset_fields')}
        assert fields.isdisjoint(FIVE)

    registry_database(scenario)


@needs_postgres
def test_a_rename_queued_onto_one_of_the_five_fails_and_writes_nothing(registry_database):
    async def scenario(database):
        ids = (await ingest([{'note': 'a'}, {'note': 'b'}]))['ids']
        job = submitted(
            await datasets.rename_field(1, 'note', datasets.FieldRenameRequest(new_name='memo', background=True), USER),
        )
        await database.execute(
            "UPDATE jobs SET args = jsonb_set(args, '{new_name}', '\"created_at\"') WHERE id = $1",
            job,
        )
        claimed = await claim(database)
        with pytest.raises(ValueError, match=r"\['created_at'\] name sample columns"):
            for _ in range(20):  # the units before the one that writes
                claimed = await step(database, claimed)
        assert [await metadata(database, sample_id) for sample_id in ids] == [{'note': 'a'}, {'note': 'b'}]

    registry_database(scenario)
