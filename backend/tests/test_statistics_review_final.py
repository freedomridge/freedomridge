"""Late registration, stable string controls and bounded automatic statistics history."""

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from backend import access, field_registry, job_queue, job_service, statistics, statistics_measure
from backend.routers import datasets, jobs
from backend.tests import test_field_registry_postgres
from backend.tests.statistics_helpers import refresh_during, run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest
from backend.tests.test_job_runner import make_worker

if TYPE_CHECKING:
    from uuid import UUID

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


@pytest.mark.parametrize('incidental_write', [False, True])
@pytest.mark.parametrize(
    'name,kind,value,measured_name',
    [
        ('vehicle_type', 'string', 'CAR', 'vehicle_type'),
        ('detections', 'list', [{'label': 'CAR'}], 'detections.label'),
    ],
)
def test_registering_previously_written_metadata_measures_all_existing_blocks(
    registry_database,
    incidental_write,
    name,
    kind,
    value,
    measured_name,
):
    async def scenario(database):
        for block in range(4):
            await database.execute(
                "INSERT INTO samples(id,dataset_id,filepath,media_type,metadata) "
                "SELECT $1::int+number,1,'backfill-'||($1::int+number),'image','{}'::jsonb "
                'FROM generate_series(1,20) number',
                block * statistics_measure.BLOCK_SIZE,
            )
        await run_refresh(database, 1)
        await database.execute('UPDATE samples SET metadata=metadata || $1::jsonb WHERE dataset_id=1', {name: value})
        await run_refresh(database, 1)
        assert not await database.fetchval('SELECT count(*) FROM dataset_stats_pending WHERE dataset_id=1')
        assert not await database.fetchval(
            'SELECT count(*) FROM dataset_block_field_stats WHERE dataset_id=1 AND field_name=$1',
            measured_name,
        )
        async with database.pool.acquire() as connection, connection.transaction():
            if incidental_write:
                await connection.execute("UPDATE samples SET metadata=metadata || '{\"unrelated\":1}' WHERE id=1")
            await connection.execute(
                'INSERT INTO dataset_fields(dataset_id,field_name,field_type) VALUES (1,$1,$2)',
                name,
                kind,
            )
        await database.execute('UPDATE datasets SET stats_need_refresh=TRUE WHERE id=1')
        job = await statistics.request_refresh(database.pool, 1)
        assert job['barrier']['full'] is True
        await run_refresh(database, 1)
        cached = await database.fetchrow(
            'SELECT * FROM dataset_field_stats WHERE dataset_id=1 AND field_name=$1',
            measured_name,
        )
        assert cached['sample_count'] == 80
        assert cached['unique_values'] == ['CAR'] and cached['values_complete'] is True
        schema = {field['name']: field for field in await datasets.get_dataset_schema(1)}
        assert schema[measured_name]['values'] == ['CAR']
        assert schema[measured_name]['values_complete'] is True

    registry_database(scenario)


@pytest.mark.parametrize('count,expected_type', [(99, 'enum'), (100, 'enum'), (101, 'string'), (150, 'string')])
def test_measured_string_cardinality_not_preview_controls_presentation(registry_database, count, expected_type):
    async def scenario(database):
        await ingest([{'site': f'site-{number:03}', 'weather': 'SUNNY'} for number in range(count)])
        await run_refresh(database, 1)

        async def check():
            for fields in (
                await datasets.get_dataset_schema(1),
                await datasets._get_dataset_field_stats_impl(1, None),
            ):
                by_name = {field['name']: field for field in fields}
                assert by_name['site']['type'] == expected_type
                assert by_name['site']['values_complete'] is (count <= 100)
                assert by_name['weather']['type'] == 'enum' and by_name['weather']['values_complete'] is True

        await check()
        await ingest([{'site': 'site-000', 'weather': 'SUNNY'}], prefix='during-sync')
        await check()
        await run_refresh(database, 1)
        await check()

    registry_database(scenario)


def test_new_categorical_field_has_complete_values_before_its_first_refresh(registry_database):
    async def scenario(database):
        values = [f'site-{number}' for number in range(5)]
        await ingest([{'site': value} for value in values])
        field = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'site')
        assert field['type'] == 'enum'
        assert set(field['values']) == set(values)
        assert field['values_complete'] is True

    registry_database(scenario)


def test_crossing_the_value_cap_during_measurement_cannot_restore_an_enum_preview(registry_database):
    async def scenario(database):
        await ingest([{'site': f'site-{number:03}'} for number in range(95)])
        await run_refresh(database, 1)
        await refresh_during(
            database,
            lambda: ingest([{'site': f'late-{number:03}'} for number in range(25)], prefix='late'),
        )
        field = next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'site')
        assert field['type'] == 'string'
        assert field.get('values') is None
        assert field['values_complete'] is False

    registry_database(scenario)


def test_dataset_history_keeps_user_work_visible_amid_automatic_statistics(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        explicit = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1, created_by=1)
        await database.execute(
            """INSERT INTO jobs(id,kind,dataset_id,state,dedupe_owner,finished_at)
               SELECT md5('scheduled-'||number)::uuid,'statistics',1,'succeeded','system',clock_timestamp()
               FROM generate_series(1,120) number""",
        )
        failed = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1)
        await database.execute("UPDATE jobs SET state='failed' WHERE id=$1", failed['id'])
        deleted_user = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1)
        await database.execute(
            "UPDATE jobs SET state='succeeded',dedupe_owner='user:deleted' WHERE id=$1",
            deleted_user['id'],
        )
        active = await job_queue.enqueue_job(database.pool, 'statistics', dataset_id=1)
        expected = {job['id'] for job in (explicit, failed, deleted_user, active)}
        found: set[UUID] = set()
        cursor = None
        while True:
            page = await jobs.list_dataset_jobs(1, limit=2, cursor=cursor, user=USER)
            found.update(item.id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert found == expected
        all_jobs = await jobs.list_dataset_jobs(1, limit=100, cursor=None, user=USER, include_system_statistics=True)
        assert len(all_jobs.items) == 100 and all_jobs.next_cursor is not None
        detail = await jobs.get_job(all_jobs.items[-1].id, USER)
        assert detail.kind == 'statistics'

    registry_database(scenario)


def test_string_control_changes_only_with_cardinality_not_refresh_freshness(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'site': f'site-{number:03}'} for number in range(100)]))['ids'][0]
        await run_refresh(database, 1)

        async def site():
            return next(field for field in await datasets.get_dataset_schema(1) if field['name'] == 'site')

        assert (await site())['type'] == 'enum'
        await ingest([{'site': 'overflow'}], prefix='overflow')
        assert (await site())['type'] == 'string'
        await ingest([{'site': 'site-000'}], prefix='small-batch')
        assert (await site())['type'] == 'string'
        await run_refresh(database, 1)
        assert (await site())['type'] == 'string'
        await database.execute("UPDATE samples SET metadata='{\"site\":\"short\"}' WHERE dataset_id=1")
        await run_refresh(database, 1)
        assert (await site())['type'] == 'enum'
        assert (await site())['values'] == ['short']
        await datasets.update_sample(1, sample_id, datasets.SampleUpdate(metadata={'site': 'changed'}), USER)
        assert (await site())['type'] == 'enum'
        assert (await site())['values_complete'] is False
        await run_refresh(database, 1)
        assert (await site())['type'] == 'enum'
        assert (await site())['values_complete'] is True
        assert set((await site())['values']) == {'short', 'changed'}

    registry_database(scenario)


def test_cleanup_prunes_only_old_successful_system_statistics_in_bounded_units(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO jobs(id,kind,dataset_id,state,dedupe_owner,finished_at)
               SELECT md5('expired-'||number)::uuid,'statistics',1,'succeeded','system',
                   clock_timestamp()-interval '2 days' FROM generate_series(1,2500) number""",
        )
        protected = []
        for kind, state, owner, age in [
            ('statistics', 'succeeded', 'system', '1 hour'),
            ('statistics', 'failed', 'system', '2 days'),
            ('statistics', 'cancelled', 'system', '2 days'),
            ('statistics', 'queued', 'system', '2 days'),
            ('statistics', 'running', 'system', '2 days'),
            ('statistics', 'succeeded', 'user:deleted', '2 days'),
            ('other', 'succeeded', 'system', '2 days'),
        ]:
            job = await job_queue.enqueue_job(database.pool, kind, dataset_id=2)
            await database.execute(
                """UPDATE jobs SET state=$2,dedupe_owner=$3,finished_at=clock_timestamp()-$4::text::interval,
                   lease_token=CASE WHEN $2='running' THEN gen_random_uuid() END,
                   lease_expires_at=CASE WHEN $2='running' THEN clock_timestamp()+interval '1 minute' END,
                   worker_id=CASE WHEN $2='running' THEN 'test-worker' END WHERE id=$1""",
                job['id'],
                state,
                owner,
                age,
            )
            protected.append(job['id'])
        cleanup = await job_service.schedule_cleanup(database.pool)
        claim = await job_queue.claim_job(database.pool, 'cleanup', kinds=[job_service.SELECTION_CLEANUP])
        assert claim is not None
        handler = job_service.selection_cleanup(database.pool)
        unit = await handler(claim)
        assert not unit.finished and unit.publish is not None
        await job_queue.publish_step(
            database.pool,
            claim['id'],
            claim['lease_token'],
            checkpoint=unit.checkpoint,
            publisher=unit.publish,
            release=True,
        )
        assert await database.fetchval('SELECT count(*) FROM jobs WHERE dataset_id=1') == 1500
        worker = await make_worker(database, {job_service.SELECTION_CLEANUP: handler})
        await worker.run_once()
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', cleanup['id']) == 'succeeded'
        assert not await database.fetchval('SELECT count(*) FROM jobs WHERE dataset_id=1')
        assert set(await database.fetchval('SELECT array_agg(id) FROM jobs WHERE dataset_id=2')) == set(protected)
        assert not await database.fetchval(
            'SELECT count(*) FROM job_controls WHERE job_id NOT IN (SELECT id FROM jobs)',
        )

    registry_database(scenario)


def test_history_pagination_skips_automatic_statistics_via_its_partial_index(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await database.execute(
            """INSERT INTO jobs(id,kind,dataset_id,state,dedupe_owner,finished_at)
               SELECT md5('history-'||number)::uuid,'statistics',1,'succeeded','system',clock_timestamp()
               FROM generate_series(1,20000) number""",
        )
        for number in range(5):
            await job_queue.enqueue_job(database.pool, 'user-work', dataset_id=1, created_by=1)
        await database.execute('ANALYZE jobs; ANALYZE job_controls')
        query = AsyncMock(wraps=database.fetch)
        monkeypatch.setattr(database, 'fetch', query)
        page = await jobs.list_dataset_jobs(1, limit=2, cursor=None, user=USER)
        assert len(page.items) == 2 and page.next_cursor
        statement, *parameters = query.call_args.args
        plan = json.dumps(await database.fetchval('EXPLAIN (FORMAT JSON) ' + statement, *parameters))
        assert 'idx_jobs_dataset_history' in plan
        assert 'Seq Scan' not in plan

    registry_database(scenario)


@pytest.mark.parametrize('source', ['cli', 'quality', 'admin'])
def test_explicit_refreshes_stay_visible_and_survive_retention(registry_database, monkeypatch, source):
    async def scenario(database):
        monkeypatch.setattr(jobs, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        await ingest([{'score': 1}])
        await run_refresh(database, 1)
        await run_refresh(database, 2)
        await database.execute("DELETE FROM jobs WHERE kind='statistics'")
        await ingest([{'score': 2}], prefix='new')
        if source == 'cli':
            job = await statistics.request_refresh(database.pool, 1, force=True)
        elif source == 'quality':
            await database.execute(field_registry.REQUEST_FULL_REFRESH, 1)
            job = (await statistics.schedule_statistics(database.pool))[0]
        else:
            response = await datasets.refresh_stale_stats(USER, wait=False)
            job = await database.fetchrow(
                'SELECT * FROM jobs WHERE id=$1::text::uuid',
                json.loads(response.body)['jobs'][0],
            )
            assert job['created_by'] == USER['id']
            assert job['barrier']['full'] is False
        await database.execute(
            "UPDATE jobs SET state='succeeded',finished_at=clock_timestamp()-interval '2 days' WHERE id=$1",
            job['id'],
        )
        page = await jobs.list_dataset_jobs(1, limit=100, cursor=None, user=USER)
        assert [item.id for item in page.items] == [job['id']]
        await job_service.schedule_cleanup(database.pool)
        worker = await make_worker(
            database,
            {job_service.SELECTION_CLEANUP: job_service.selection_cleanup(database.pool)},
        )
        await worker.run_once()
        assert await database.fetchval('SELECT state FROM jobs WHERE id=$1', job['id']) == 'succeeded'

    registry_database(scenario)
