"""Expiry and target membership stay coherent while the runner prunes selections."""

import asyncio
import os
from datetime import timedelta

import asyncpg
import pytest
from fastapi import HTTPException

from backend import job_queue, job_service
from backend.routers import datasets
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_job_launch import wait_until

registry_database = registry_helpers.registry_database
USER = {'id': 1, 'role': 'admin'}

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an explicitly supplied disposable PostgreSQL database',
)


async def selected_samples(database, count=2500, *, expired=False):
    await database.execute("INSERT INTO users(id,username) VALUES (1,'reviewer') ON CONFLICT DO NOTHING")
    await database.execute(
        """INSERT INTO samples(dataset_id,filepath,media_type,tags)
           SELECT 1,'selection-'||number||'.jpg','image',ARRAY['before'] FROM generate_series(1,$1::int) AS number;
        """,
        count,
    )
    await database.execute(
        """INSERT INTO region_selections(token,sample_id,created_at)
           SELECT 'review-selection',id,clock_timestamp()-$1::interval FROM samples WHERE dataset_id=1""",
        timedelta(hours=3 if expired else 0),
    )


async def prune_once(database):
    await job_service.schedule_cleanup(database.pool)
    job = await job_queue.claim_job(database.pool, 'selection-review', kinds=[job_service.SELECTION_CLEANUP])
    assert job is not None
    unit = await job_service.selection_cleanup(database.pool)(job)
    await job_queue.publish_step(database.pool, job['id'], job['lease_token'], publisher=unit.publish, finished=True)


def test_partially_pruned_expired_token_is_not_live(registry_database):
    async def scenario(database):
        await selected_samples(database, expired=True)
        await prune_once(database)
        assert await database.fetchval('SELECT count(*) FROM region_selections') == 1500
        for operation in (
            lambda: datasets.bulk_tag_samples(
                1,
                datasets.BulkTagRequest(selection_token='review-selection', add_tags=['after']),
                USER,
            ),
            lambda: datasets.get_tag_counts(1, datasets.TagCountsRequest(selection_token='review-selection')),
        ):
            with pytest.raises(HTTPException) as failure:
                await operation()
            assert failure.value.status_code == 410
        assert await database.fetchval("SELECT count(*) FROM samples WHERE tags=ARRAY['before']") == 2500

    registry_database(scenario)


def test_filter_never_exposes_a_partially_pruned_expired_selection(registry_database):
    async def scenario(database):
        await selected_samples(database, expired=True)
        await prune_once(database)
        response = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters={'field': 'id', 'operator': 'in_selection', 'value': 'review-selection'}),
        )
        assert response['total'] == 0

    registry_database(scenario)


def test_bulk_rechecks_expiry_when_pinning_selection(registry_database, monkeypatch):
    async def scenario(database):
        await selected_samples(database)
        original = datasets._require_live_selection

        async def expire_after_validation(token, source=None):
            if source is not None:
                await source.execute("UPDATE region_selections SET created_at=clock_timestamp()-interval '3 hours'")
            await original(token, source)

        monkeypatch.setattr(datasets, '_require_live_selection', expire_after_validation)
        with pytest.raises(HTTPException) as failure:
            await datasets.bulk_tag_samples(
                1,
                datasets.BulkTagRequest(selection_token='review-selection', add_tags=['after'], remove_tags=['before']),
                USER,
            )
        assert failure.value.status_code == 410
        assert await database.fetchval("SELECT count(*) FROM samples WHERE tags=ARRAY['before']") == 2500
        assert await database.fetchval('SELECT count(*) FROM dataset_tags') == 0

    registry_database(scenario)


def test_bulk_captures_targets_once_even_if_cleanup_commits_during_mutation(registry_database, monkeypatch):
    async def scenario(database):
        await selected_samples(database, count=3)
        original = asyncpg.Connection.execute
        cleaned = False

        async def execute(connection, query, *args, **kwargs):
            nonlocal cleaned
            result = await original(connection, query, *args, **kwargs)
            if 'INSERT INTO dataset_tags' in query and not cleaned:
                cleaned = True
                await database.execute("UPDATE region_selections SET created_at=clock_timestamp()-interval '3 hours'")
                await prune_once(database)
            return result

        monkeypatch.setattr(asyncpg.Connection, 'execute', execute)
        result = await datasets.bulk_tag_samples(
            1,
            datasets.BulkTagRequest(selection_token='review-selection', add_tags=['after'], remove_tags=['before']),
            USER,
        )
        assert cleaned and result['updated_count'] == 3
        assert [row['tags'] for row in await database.fetch('SELECT tags FROM samples ORDER BY id')] == [['after']] * 3

    registry_database(scenario)


def test_counts_use_one_snapshot_after_token_validation(registry_database, monkeypatch):
    async def scenario(database):
        await selected_samples(database, count=3)
        original = asyncpg.Connection.fetch
        cleaned = False

        async def fetch(connection, query, *args, **kwargs):
            nonlocal cleaned
            result = await original(connection, query, *args, **kwargs)
            if 'count(DISTINCT s.id)' in query and not cleaned:
                cleaned = True
                await database.execute("UPDATE region_selections SET created_at=clock_timestamp()-interval '3 hours'")
                await prune_once(database)
            return result

        monkeypatch.setattr(asyncpg.Connection, 'fetch', fetch)
        result = await datasets.get_tag_counts(1, datasets.TagCountsRequest(selection_token='review-selection'))
        assert cleaned
        assert result['total_samples'] == 3
        assert result['tag_counts'] == {'before': 3}

    registry_database(scenario)


def test_cleanup_during_a_sample_lock_wait_cannot_shrink_bulk_targets(registry_database):
    async def scenario(database):
        await selected_samples(database, count=3)
        operation = None
        try:
            async with database.pool.acquire() as blocker, blocker.transaction():
                await blocker.execute('SELECT id FROM samples WHERE dataset_id=1 ORDER BY id LIMIT 1 FOR UPDATE')
                operation = asyncio.create_task(
                    datasets.bulk_tag_samples(
                        1,
                        datasets.BulkTagRequest(selection_token='review-selection', add_tags=['after']),
                        USER,
                    ),
                )

                async def waiting():
                    return await database.fetchval(
                        """SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database()
                           AND wait_event_type='Lock' AND query LIKE '%ORDER BY id FOR UPDATE%')""",
                    )

                await wait_until(waiting, timeout=2)
                await database.execute("UPDATE region_selections SET created_at=clock_timestamp()-interval '3 hours'")
                await prune_once(database)
                assert await database.fetchval('SELECT count(*) FROM region_selections') == 0
            result = await asyncio.wait_for(operation, 3)
            assert result['updated_count'] == 3
            assert await database.fetchval("SELECT count(*) FROM samples WHERE tags @> ARRAY['after']") == 3
        finally:
            if operation is not None:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)

    registry_database(scenario)


def test_combined_tag_edit_counts_union_once_and_does_not_stamp_noops(registry_database):
    async def scenario(database):
        await selected_samples(database, count=3)
        identifiers = [row['id'] for row in await database.fetch('SELECT id FROM samples ORDER BY id')]
        await database.execute('UPDATE samples SET tags=$2 WHERE id=$1', identifiers[0], [])
        await database.execute('UPDATE samples SET tags=$2 WHERE id=$1', identifiers[1], ['before', 'after'])
        await database.execute('UPDATE samples SET tags=$2 WHERE id=$1', identifiers[2], ['after'])
        untouched = await database.fetchval('SELECT last_modified_at FROM samples WHERE id=$1', identifiers[2])
        result = await datasets.bulk_tag_samples(
            1,
            datasets.BulkTagRequest(selection_token='review-selection', add_tags=['after'], remove_tags=['before']),
            USER,
        )
        assert result['updated_count'] == 2
        assert [row['tags'] for row in await database.fetch('SELECT tags FROM samples ORDER BY id')] == [['after']] * 3
        assert await database.fetchval('SELECT last_modified_at FROM samples WHERE id=$1', identifiers[2]) == untouched
        result = await datasets.bulk_tag_samples(
            1,
            datasets.BulkTagRequest(sample_ids=identifiers, add_tags=['after'], remove_tags=['before']),
            USER,
        )
        assert result['updated_count'] == 0

    registry_database(scenario)
