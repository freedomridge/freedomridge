"""Index advisor regressions, one test each.
Each fails on the code before these fixes (or kills a mutation that survived its tests)."""

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend import dataset_schedules, field_usage, index_advisor, index_jobs, job_queue
from backend.routers import datasets
from backend.routers import jobs as jobs_router
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.test_dataset_schedules import add_schedule, local
from backend.tests.test_field_registry_postgres import ingest
from backend.tests.test_index_advisor import ADMIN, NOW, _dataset_snapshot, _usage, ix, snap
from backend.tests.test_map_refit import seed as seed_map

registry_database = test_field_registry_postgres.registry_database
requires_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL test database',
)
ROOT = Path(__file__).resolve().parents[2]
WATCHED = NOW - timedelta(days=30)


def _names(items):
    return sorted(item['name'] for item in items)


def _pairs(review):
    return sorted((pair['redundant']['name'], pair['kept']['name']) for pair in review['overlapping'])


def _watched(*oids, since=WATCHED):
    return [{'oid': oid, 'first_seen_at': since} for oid in oids]


@pytest.fixture(autouse=True)
def empty_pending():
    field_usage.take_pending()
    yield
    field_usage.take_pending()


# --- verdicts are joint -------------------------------------------------------------------------


def test_a_used_index_is_never_offered_with_its_idle_keeper():
    indexes = [
        ix('narrow', 1, keys=['a'], scans=100_000, last_scan=NOW - timedelta(minutes=1), size=100),
        ix('wide', 2, keys=['a', 'b'], scans=0, size=300),
    ]
    review, _ = index_advisor.build_review(snap(indexes, observations=_watched(1, 2)), None)
    assert _names(review['possibly_unused']) == ['wide']
    assert _pairs(review) == []  # removing `narrow` too would leave its queries without an index


def test_a_pair_is_offered_only_when_the_kept_index_is_used_at_least_as_much():
    recent = NOW - timedelta(hours=1)
    indexes = [
        ix('a', 1, keys=['a'], scans=1, last_scan=recent),
        ix('ab', 2, keys=['a', 'b'], scans=100, last_scan=recent),
        ix('abc', 3, keys=['a', 'b', 'c'], scans=50, last_scan=recent),
    ]
    review, _ = index_advisor.build_review(snap(indexes, observations=_watched(1, 2, 3)), None)
    pairs = _pairs(review)
    assert pairs == [('a', 'abc')]  # never ('ab', 'abc'): ab is used more than abc
    kept = {pair[1] for pair in pairs}
    assert not kept & {pair[0] for pair in pairs}  # a keeper is never itself offered
    assert not kept & set(_names(review['possibly_unused']))


def test_removals_never_leave_a_foreign_key_without_an_index():
    indexes = [
        ix('child_fk', 1, table='child', keys=['parent_id'], size=100),
        ix('child_fk_x', 2, table='child', keys=['parent_id', 'x'], size=300),
    ]
    keys = [{'name': 'child_parent_fk', 'table_name': 'child', 'parent': 'parent', 'columns': ['parent_id']}]
    review, _ = index_advisor.build_review(snap(indexes, observations=_watched(1, 2), foreign_keys=keys), None)
    needed = {item['name']: item['needed'] for item in review['needed']}
    assert list(needed) == ['child_fk'] and 'last index left for foreign key child_parent_fk' in needed['child_fk'][0]
    assert _names(review['possibly_unused']) == ['child_fk_x']
    assert _pairs(review) == []


# --- no back-dating ------------------------------------------------------------------------------


def test_a_restore_into_an_old_cluster_is_not_reported_idle():
    # A dump restored into a new database of another cluster: no observations, an old statistics start.
    indexes = [
        ix('idx_samples_metadata_gin', 1, table='samples', method='gin', size=5_500_000_000),
        ix('idx_samples_tags_gin', 2, table='samples', method='gin', size=900_000_000),
        ix('my_hand_made', 3, size=10_000_000),
    ]
    review, _ = index_advisor.build_review(snap(indexes, stats_start=NOW - timedelta(days=90)), None)
    assert review['possibly_unused'] == []
    assert _names(review['too_new']) == ['idx_samples_metadata_gin', 'idx_samples_tags_gin', 'my_hand_made']
    assert review['enough_history'] is False and review['history_since'] == NOW.isoformat()


def test_observations_of_another_database_start_over():
    indexes = [ix('kept', 1)]
    other_database = snap(indexes, observations=[{'oid': 1, 'first_seen_at': WATCHED, 'database_oid': 71}])
    assert index_advisor.first_seen(other_database) == {1: NOW}
    same = snap(indexes, observations=_watched(1))
    assert index_advisor.first_seen(same) == {1: WATCHED}


# --- full key definitions --------------------------------------------------------------------


@requires_database
def test_sort_order_include_columns_collations_and_operator_classes_are_compared(registry_database):
    async def scenario(database):
        await database.execute(
            """CREATE TABLE probe_order (id serial PRIMARY KEY, x int, y int, z text);
               CREATE INDEX probe_xy_asc ON probe_order (x, y);
               CREATE INDEX probe_xy_mixed ON probe_order (x, y DESC);
               CREATE INDEX probe_xyz ON probe_order (x, y, z);
               CREATE TABLE probe_cover (id serial PRIMARY KEY, x int, y int);
               CREATE INDEX probe_x_cover ON probe_cover (x) INCLUDE (y);
               CREATE INDEX probe_x_plain ON probe_cover (x);
               CREATE TABLE probe_text (id serial PRIMARY KEY, z text);
               CREATE INDEX probe_z_c ON probe_text (z COLLATE "C");
               CREATE INDEX probe_z_plain ON probe_text (z);
               CREATE INDEX probe_z_pattern ON probe_text (z text_pattern_ops);
               CREATE TABLE probe_back (id serial PRIMARY KEY, y int);
               CREATE INDEX probe_w_desc ON probe_back (y DESC);
               CREATE INDEX probe_w_asc ON probe_back (y);""",
        )
        snapshot = await index_advisor.collect(database.pool, None)
        review, _ = index_advisor.build_review(snapshot, None)
        pairs = dict(_pairs(review))
        assert 'probe_xy_mixed' not in pairs  # (x, y DESC) serves ORDER BY x, y DESC; (x, y) does not
        assert pairs.get('probe_xy_asc') == 'probe_xyz'  # (x, y) leads (x, y, z)
        assert 'probe_x_cover' not in pairs  # its INCLUDE (y) feeds index-only scans the plain one cannot
        assert pairs.get('probe_x_plain') == 'probe_x_cover'
        assert not {'probe_z_c', 'probe_z_plain', 'probe_z_pattern'} & set(pairs)  # collations, operator classes
        # A whole index read backwards gives the reverse order: (y DESC) and (y) are duplicates, one pair.
        assert ('probe_w_desc' in pairs) != ('probe_w_asc' in pairs)

    registry_database(scenario)


def test_prefix_needs_the_same_operator_families_and_duplicates_the_same_collation():
    operators = {1: {'1:s'}, 5: {'5:s'}}
    narrow = ix('narrow', 1, keys=['z'], families=[5])
    wide = ix('wide', 2, keys=['z', 'x'], families=[1, 1])
    assert index_advisor.redundancy(narrow, wide, operators) is None
    plain = ix('plain', 3, keys=['z'], collations=[100])
    c_collated = ix('c', 4, keys=['z'], collations=[950])
    assert index_advisor.redundancy(plain, c_collated, operators) is None
    assert index_advisor.redundancy(plain, ix('same', 5, keys=['z'], collations=[100]), operators) is not None


def test_a_partial_index_does_not_serve_a_foreign_key():
    indexes = [
        ix('child_partial', 1, table='child', keys=['parent_id'], predicate='(x > 0)'),
        ix('child_full', 2, table='child', keys=['parent_id']),
    ]
    keys = [{'name': 'child_parent_fk', 'table_name': 'child', 'parent': 'parent', 'columns': ['parent_id']}]
    needed = index_advisor.needed_reasons(indexes, keys)
    assert 2 in needed and 1 not in needed  # the full one is the only usable supporter


# --- the measured build time excludes waiting --------------------------------------------------


@requires_database
def test_a_build_that_waited_for_an_old_snapshot_records_only_its_own_time(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}, {'score': 2}])
        reader = await database.pool.acquire()
        transaction = reader.transaction(isolation='repeatable_read')
        await transaction.start()
        await reader.fetchval('SELECT count(*) FROM datasets')  # an old snapshot, as pg_dump holds
        wait = 3.0
        try:
            async with job_helpers.runner(database, kinds=[index_jobs.FIELD_KIND]):
                promotion = asyncio.create_task(datasets.promote_field(1, 'score', ADMIN, wait=False))
                await promotion
                await asyncio.sleep(wait)  # CREATE INDEX CONCURRENTLY waits for the old snapshot meanwhile
                await transaction.rollback()
                transaction = None

                async def finished():
                    while (
                        await database.fetchval("SELECT state FROM jobs WHERE kind = 'field_index_build'")
                        != 'succeeded'
                    ):
                        await asyncio.sleep(0.05)

                await asyncio.wait_for(finished(), 30)
        finally:
            if transaction is not None:
                await transaction.rollback()
            await database.pool.release(reader)
        checkpoint = await database.fetchval("SELECT checkpoint FROM jobs WHERE kind = 'field_index_build'")
        assert checkpoint['build_seconds'] < wait - 1  # the old code recorded the whole wait as building
        assert checkpoint['waited_seconds'] >= wait - 1

    registry_database(scenario)


# --- telemetry hygiene ------------------------------------------------------------------------


@requires_database
def test_a_nul_byte_field_name_does_not_lose_the_workers_minute(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US'}])
        field_usage.record(1, 'filter', ['country'], 0.01)
        with pytest.raises(Exception):
            await datasets.list_samples_post(
                1,
                datasets.SamplesRequest(filters={'field': 'bad\x00name', 'operator': '=', 'value': 'x'}, limit=1),
            )
        field_usage.record(1, 'filter', ['bad\x00name'], 0.01)  # even if recorded directly: skipped
        assert await field_usage.flush(database.pool) == 1
        rows = await database.fetch('SELECT field_name, uses FROM field_usage_daily')
        assert [(row['field_name'], row['uses']) for row in rows] == [('country', 1)]

    registry_database(scenario)


@requires_database
def test_rejected_requests_are_not_recorded(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US'}])
        with pytest.raises(HTTPException):
            await datasets.list_samples_post(
                1,
                datasets.SamplesRequest(filters={'field': 'country', 'operator': 'no-such-operator', 'value': 'x'}),
            )
        assert field_usage.take_pending() == {}

    registry_database(scenario)


def test_a_requests_time_is_shared_among_its_filter_fields():
    field_usage.record(1, 'filter', ['country', 'site'], 0.010)
    batch = field_usage.take_pending()
    assert sorted(entry[1] for entry in batch.values()) == [pytest.approx(5.0), pytest.approx(5.0)]


# --- the idle window and the review slot -------------------------------------------------------


def test_an_index_used_by_a_weekly_job_is_not_idle():
    indexes = [ix('weekly', 1, scans=12, last_scan=NOW - timedelta(days=8))]
    review, _ = index_advisor.build_review(snap(indexes, observations=_watched(1)), None)
    assert review['possibly_unused'] == [] and review['idle_days_required'] == 14


@requires_database
def test_the_review_slot_is_after_the_default_refit_window(registry_database):
    async def scenario(database):
        assert dataset_schedules.REVIEW_DEFAULTS['time'] == '06:00'
        assert str(await database.fetchval("SELECT time_of_day FROM system_job_schedules")) == '06:00:00'

    registry_database(scenario)


# --- rollback order ---------------------------------------------------------------------------


# --- scope and history pages -------------------------------------------------------------------


@requires_database
def test_the_system_view_lists_orphan_field_indexes_but_no_live_datasets(registry_database):
    async def scenario(database):
        await ingest([{'score': 1}])
        await database.execute(
            "CREATE INDEX idx_samples_meta_1_score ON samples ((metadata->>'score')) WHERE dataset_id = 1",
        )
        await database.execute(
            "CREATE INDEX idx_samples_meta_99_gone ON samples ((metadata->>'gone')) WHERE dataset_id = 99",
        )
        snapshot = await index_advisor.collect(database.pool, None)
        review, _ = index_advisor.build_review(snapshot, None)
        listed = {item['name']: item for item in review['too_new'] + review['possibly_unused']}
        assert 'idx_samples_meta_1_score' not in listed
        assert listed['idx_samples_meta_99_gone']['orphan'] is True

    registry_database(scenario)


@requires_database
def test_dataset_history_pages_never_repeat_a_review(registry_database, monkeypatch):
    async def scenario(database):
        from backend import access

        monkeypatch.setattr(access, 'db', database)
        monkeypatch.setattr(jobs_router, 'db', database)
        await ingest([{'country': 'US'}])
        await database.execute(
            """INSERT INTO jobs (id, kind, dataset_id, state, created_at, finished_at, args, lane)
               SELECT gen_random_uuid(), 'index_review', NULL, 'succeeded', now() - (g || ' minutes')::interval, now(),
                      '{"scope":"dataset","dataset_id":1}', 'user' FROM generate_series(1, 5) g""",
        )
        seen: list = []
        cursor = None
        while True:
            page = await jobs_router.list_dataset_jobs(1, limit=2, cursor=cursor, user=ADMIN)
            seen.extend(item.id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 5

    registry_database(scenario)


# --- suggestions without a measured build time ---------------------------------------------------


def test_unmeasured_builds_suggest_only_clearly_slower_promotable_fields():
    usage = [
        _usage('', 'filter', 1000, 50, 6),
        _usage('country', 'filter', 3, 60, 6),  # slower than average, but in the same latency bucket
        _usage('scene type', 'filter', 2, 7000, 13),  # clearly slower, but promote refuses the name
        _usage('site', 'filter', 2, 7000, 13),
    ]
    snapshot = _dataset_snapshot(usage, fields=('country', 'scene type', 'site'))
    snapshot['indexes'] = []
    result = index_advisor.suggestions(snapshot, {})
    assert [(item['field'], item['pays_back']) for item in result] == [('site', None)]
    assert result[0]['excess_seconds'] > 0


# --- one review cannot stop the scheduler pass --------------------------------------------------


@requires_database
def test_a_review_that_cannot_be_queued_does_not_stop_map_refits(registry_database, monkeypatch):
    async def scenario(database):
        await seed_map(database, n=30)
        await database.execute(
            """INSERT INTO dataset_job_schedules (dataset_id, job_type, target, enabled, frequency, weekday,
                   time_of_day, next_run_at)
               VALUES (1, 'index_review', 'indexes', TRUE, 'daily', NULL, '02:00', $1)""",
            local(2026, 10, 20, 2),
        )
        await add_schedule(database, min_new_samples=20)
        monkeypatch.setattr(index_advisor, 'submit', AsyncMock(side_effect=job_queue.QueueContended('busy')))
        queued = await dataset_schedules.schedule_dataset_jobs(database.pool, local(2026, 10, 20, 3, 0))
        assert [job['kind'] for job in queued] == ['map_refit']
        review = await database.fetchrow(
            "SELECT next_run_at FROM dataset_job_schedules WHERE job_type = 'index_review'",
        )
        assert review['next_run_at'] == local(2026, 10, 20, 2)  # still due: retried at the next pass

    registry_database(scenario)
