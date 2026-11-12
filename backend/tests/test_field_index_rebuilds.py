"""Field indexes rebuilt in the background (0032, index_jobs.queue_rebuilds): a datetime field's index moves from its
stored text to its instant. The build replaces an index of the field's name built on another expression, the queue
takes field rows beside vector rows (one build of each kind at a time, the smallest dataset first), and a rebuilding
field shows as such in the API and the index advisor until its index is built."""

import os
from datetime import date

import pytest
from fastapi import HTTPException

from backend import access, dataset_deletion, field_usage, index_advisor, index_jobs, job_queue
from backend.job_wait import JobConflict
from backend.routers import datasets
from backend.routers import index_advisor as advisor_router
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.test_dataset_deletion import finish as finish_deletion
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
KIND = index_jobs.FIELD_KIND
ADMIN = {'id': 1, 'role': 'admin', 'username': 'admin'}
INSTANT = "metadata_instant_v1((metadata ->> '{}'::text))"


async def text_index(database, field, dataset_id=1):
    """A field's index as promote built it on a datetime field before 0032: on the stored text."""
    name = datasets._promoted_index_name(dataset_id, field)
    await database.execute(
        f"""CREATE INDEX "{name}" ON samples ((metadata->>'{field}')) WHERE dataset_id = {dataset_id}""",
    )
    return name


async def index(database, name):
    """(key expression as PostgreSQL prints it, valid), or None when there is no index of that name."""
    row = await database.fetchrow(
        """SELECT pg_get_expr(i.indexprs, i.indrelid) AS expression, i.indisvalid AS valid
           FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
           WHERE c.relname = $1 AND c.relnamespace = current_schema()::regnamespace""",
        name,
    )
    return None if row is None else (row['expression'], row['valid'])


async def promoted(database, field, dataset_id=1):
    return await database.fetchval(
        'SELECT is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2',
        dataset_id,
        field,
    )


async def queue_field(database, *rows):
    for dataset_id, field in rows:
        await database.execute(
            "INSERT INTO ann_index_rebuilds (kind, dataset_id, field_name) VALUES ('field', $1, $2)",
            dataset_id,
            field,
        )


async def rows(database):
    found = await database.fetch('SELECT kind, dataset_id, field_name FROM ann_index_rebuilds ORDER BY 1, 2, 3')
    return [tuple(row) for row in found]


async def builds(database, kind=KIND):
    found = await database.fetch(
        "SELECT dataset_id, COALESCE(args->>'field', args->>'field_name') AS field, state, args->>'rebuild' AS rebuild "
        'FROM jobs WHERE kind = $1 ORDER BY created_at',
        kind,
    )
    return [tuple(row) for row in found]


async def sample_counts(database, counts):
    """The statistics' sample counts the queue orders by (the largest field's per dataset)."""
    for dataset_id, count in counts.items():
        await database.execute(
            'UPDATE dataset_field_stats SET sample_count = $2 WHERE dataset_id = $1',
            dataset_id,
            count,
        )


def test_promote_replaces_an_index_of_the_fields_name_on_another_expression(registry_database):
    """CREATE INDEX CONCURRENTLY IF NOT EXISTS kept any index of the name: a datetime field's text index stayed, and
    the field was called indexed on an index its queries can't use."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01T10:00:00+02:00'}, {'captured': '2026-01-02'}])
        name = await text_index(database, 'captured')
        async with job_helpers.runner(database, kinds=[KIND]):
            answer = await datasets.promote_field(1, 'captured', USER)
        assert answer == {'message': 'Field captured promoted successfully'}
        assert await index(database, name) == (INSTANT.format('captured'), True)
        assert await promoted(database, 'captured')

    registry_database(scenario)


def test_a_refused_build_after_the_old_index_went_leaves_no_index(registry_database):
    """The old index goes before the build; a build its values refuse keeps nothing, so the field is an ordinary
    unindexed field, not one called indexed on its old index."""

    async def scenario(database):
        await ingest([{'score': 1}, {'score': 2}])
        await database.execute(
            """UPDATE samples SET metadata = jsonb_set(metadata, '{score}', '"abc"')
               WHERE id = (SELECT min(id) FROM samples)""",
        )
        name = await text_index(database, 'score')  # left from when it was a string
        async with job_helpers.runner(database, kinds=[KIND]):
            with pytest.raises(HTTPException) as refused:
                await datasets.promote_field(1, 'score', USER)
        assert refused.value.status_code == 409 and 'check values' in refused.value.detail
        assert await index(database, name) is None and not await promoted(database, 'score')

    registry_database(scenario)


def test_a_rebuild_whose_field_changed_type_gets_the_conflict_and_leaves_no_index(registry_database):
    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}])
        name = await text_index(database, 'captured')
        await queue_field(database, (1, 'captured'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        await database.execute("UPDATE dataset_fields SET field_type = 'string' WHERE field_name = 'captured'")
        job = await job_helpers.claim(database, KIND)
        with pytest.raises(JobConflict, match='Field definition changed'):
            await index_jobs.field_handler(database.pool)(job)
        assert await index(database, name) is None and not await promoted(database, 'captured')

    registry_database(scenario)


def test_field_rebuilds_go_one_at_a_time_smallest_dataset_first(registry_database):
    """By the sample count the statistics keep, never by counting: a small dataset's field is indexed in seconds,
    not after a large one's."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01', 'seen': '2026-01-01'}] * 3, prefix='large')
        await ingest([{'captured': '2026-01-01'}], dataset_id=2, prefix='small')
        await sample_counts(database, {1: 500, 2: 20})
        await queue_field(database, (1, 'seen'), (1, 'captured'), (2, 'captured'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert await index_jobs.queue_rebuilds(database.pool) == 0  # one queued: the rest wait their turn
        for _ in range(2):
            assert (await job_helpers.finish(database, KIND))['state'] == 'succeeded'
            assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert (await job_helpers.finish(database, KIND))['state'] == 'succeeded'
        assert await builds(database) == [
            (2, 'captured', 'succeeded', 'true'),
            (1, 'captured', 'succeeded', 'true'),
            (1, 'seen', 'succeeded', 'true'),
        ]
        assert await rows(database) == []  # each publication took its own row
        assert await index_jobs.queue_rebuilds(database.pool) == 0

    registry_database(scenario)


def test_a_vector_build_that_hasnt_started_doesnt_hold_the_field_rebuilds(registry_database):
    """One index build at a time across both kinds, field builds first (test_index_build_gates): a vector build
    queued (by hand here) but not started yet doesn't hold the field rebuilds, and the vector rows wait for them."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01', 'seen': '2026-01-02'}])
        await database.execute(
            "INSERT INTO ann_index_rebuilds (dataset_id, field_name) VALUES (2, 'clip'), (2, 'dino')",  # 2.9.0's rows
        )
        await index_jobs.submit_ann_index(database.pool, 2, USER['id'], 'clip')
        await queue_field(database, (1, 'captured'), (1, 'seen'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert await index_jobs.queue_rebuilds(database.pool) == 0
        assert [build[:2] for build in await builds(database)] == [(1, 'captured')]
        assert len(await builds(database, index_jobs.ANN_KIND)) == 1  # only the one queued by hand
        assert (await job_helpers.finish(database, KIND))['state'] == 'succeeded'
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert [build[:2] for build in await builds(database)] == [(1, 'captured'), (1, 'seen')]
        assert await rows(database) == [('field', 1, 'seen'), ('vector', 2, 'clip'), ('vector', 2, 'dino')]

    registry_database(scenario)


def test_a_deleted_dataset_or_field_leaves_no_job(registry_database):
    async def scenario(database):
        await ingest([{'captured': '2026-01-01', 'gone': '2026-01-01'}])
        await ingest([{'captured': '2026-01-01'}], dataset_id=2, prefix='two')
        await database.execute("DELETE FROM dataset_fields WHERE dataset_id = 1 AND field_name = 'gone'")
        await queue_field(database, (1, 'gone'), (2, 'captured'))
        await dataset_deletion.submit(database.pool, 2, USER['id'])  # being deleted: skipped
        assert await index_jobs.queue_rebuilds(database.pool) == 0
        assert await builds(database) == []
        assert await rows(database) == [('field', 2, 'captured')]  # the field's row went; the dataset's waits
        await finish_deletion(database)
        assert await rows(database) == []
        assert await index_jobs.queue_rebuilds(database.pool) == 0 and await builds(database) == []

    registry_database(scenario)


def test_a_failed_field_rebuild_is_tried_again_a_day_later_and_a_cancelled_one_not_again(registry_database):
    """The vector rebuilds' rules, for field rows: a row stays until its field is indexed, a failed build is tried
    again but not within a day, and whoever cancels a build this queued doesn't want it (Index in the Indexes tab or
    the advisor builds it again)."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}], prefix='one')
        await ingest([{'captured': '2026-01-01'}], dataset_id=2, prefix='two')
        await sample_counts(database, {1: 10, 2: 20})
        await queue_field(database, (1, 'captured'), (2, 'captured'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        await database.execute(
            """UPDATE jobs SET state = 'failed', finished_at = now(), error = '{"type": "JobConflict"}'
               WHERE kind = $1""",
            KIND,
        )
        assert await index_jobs.queue_rebuilds(database.pool) == 1  # dataset 1's failed: 2's goes next
        assert (await job_helpers.finish(database, KIND))['state'] == 'succeeded'
        assert await index_jobs.queue_rebuilds(database.pool) == 0  # 1's failed under a day ago
        assert await rows(database) == [('field', 1, 'captured')]
        await database.execute("UPDATE jobs SET finished_at = now() - interval '25 hours' WHERE state = 'failed'")
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        [retry] = await database.fetch("SELECT id FROM jobs WHERE kind = $1 AND state = 'queued'", KIND)
        await job_queue.cancel_job(database.pool, retry['id'])
        assert await index_jobs.queue_rebuilds(database.pool) == 0
        assert await rows(database) == []
        assert [build[:3] for build in await builds(database)] == [
            (1, 'captured', 'failed'),
            (2, 'captured', 'succeeded'),
            (1, 'captured', 'cancelled'),
        ]

    registry_database(scenario)


def test_a_rebuilding_field_shows_as_rebuilding_until_its_index_is_on_its_instant(registry_database, monkeypatch):
    """0032's state: the text index is there and the field is marked not indexed, with a rebuild queued. The field
    lists and the index advisor say "rebuilding", the advisor suggests no index for it, and once the build is done
    the field is indexed on its instant and the advisor counts the new index as the field's."""

    async def scenario(database):
        monkeypatch.setattr(access, 'db', database)
        monkeypatch.setattr(advisor_router, 'db', database)
        await ingest([{'captured': '2026-01-01T10:00:00+02:00', 'score': 1}])
        name = await text_index(database, 'captured')
        await queue_field(database, (1, 'captured'))

        async def field_stats():
            return {field['name']: field for field in await datasets.get_dataset_field_stats(1)}

        fields = await field_stats()
        assert fields['captured']['index_rebuilding'] is True and fields['captured']['is_promoted'] is False
        assert 'index_rebuilding' not in fields['score']
        assert (await advisor_router.dataset_index_advisor(1, user=ADMIN))['rebuilding'] == ['captured']
        snapshot = await index_advisor.collect(database.pool, 1)
        assert snapshot['rebuilding'] == {'captured'}
        # However slow its requests, a field being rebuilt is never suggested.
        histogram = [0] * field_usage.BUCKETS
        histogram[14] = 50
        snapshot['usage'] = [
            {
                'usage': 'filter',
                'field_name': 'captured',
                'day': date(2026, 11, 1),
                'uses': 50,
                'total_ms': 50 * 9000.0,
                'latency_log2': histogram,
            },
            {
                'usage': 'filter',
                'field_name': '',
                'day': date(2026, 11, 1),
                'uses': 500,
                'total_ms': 500 * 10.0,
                'latency_log2': [500] + [0] * (field_usage.BUCKETS - 1),
            },
        ]
        assert index_advisor.suggestions(snapshot, {}) == []
        assert [item['field'] for item in index_advisor.suggestions({**snapshot, 'rebuilding': set()}, {})] == [
            'captured',
        ]

        assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert (await job_helpers.finish(database, KIND))['state'] == 'succeeded'
        assert await index(database, name) == (INSTANT.format('captured'), True)
        assert await promoted(database, 'captured') and await rows(database) == []
        fields = await field_stats()
        assert fields['captured']['is_promoted'] is True and 'index_rebuilding' not in fields['captured']
        assert (await advisor_router.dataset_index_advisor(1, user=ADMIN))['rebuilding'] == []
        [rebuilt] = [
            item for item in (await index_advisor.collect(database.pool, 1))['indexes'] if item['name'] == name
        ]
        assert index_advisor.classify(rebuilt) == ('field', 1, 'captured')

    registry_database(scenario)


def test_removing_a_rebuilding_fields_index_takes_its_rebuild_away(registry_database):
    """Whoever removes the field's index doesn't want it: the queue must not build it again behind their back."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}])
        name = await text_index(database, 'captured')
        await queue_field(database, (1, 'captured'))
        async with job_helpers.runner(database, kinds=[KIND, 'index_drop']):
            await datasets.demote_field(1, 'captured', USER)
        assert await index(database, name) is None and await rows(database) == []
        assert await index_jobs.queue_rebuilds(database.pool) == 0

    registry_database(scenario)


def test_each_types_printed_expression_is_what_postgresql_prints(registry_database):
    """The build tells an index on another expression by how PostgreSQL prints its key: field_index_printed must be
    exactly that for every type promote indexes, or every build would replace its own index."""

    async def scenario(database):
        await ingest([{'score': 1.5, 'label': 'a', 'captured': '2026-01-01', 'my-field': 'x'}])
        for field, field_type in (
            ('score', 'number'),
            ('label', 'string'),
            ('captured', 'datetime'),
            ('my-field', 'string'),
        ):
            name = datasets._promoted_index_name(1, field)
            expression = index_jobs.field_index_expression(field_type, field)
            await database.execute(f'CREATE INDEX "{name}" ON samples {expression} WHERE dataset_id = 1')
            assert (await index(database, name))[0] == index_jobs.field_index_printed(field_type, field)
        assert index_jobs.field_index_expression('boolean', 'flag') is None

    registry_database(scenario)
