"""Index advisor cancellation, applicability, and busy-index regressions.
Each test fails on the code before these fixes (or kills a mutation that survived there)."""

import asyncio
import re
from datetime import timedelta
from pathlib import Path

from backend import dataset_schedules, index_advisor, index_jobs
from backend.tests.test_index_advisor import NOW, _dataset_snapshot, _usage, ix, snap

ROOT = Path(__file__).resolve().parents[2]


# --- "twice as slow" compares measured means, not histogram buckets ---------------------------------


def test_twice_as_slow_means_twice_the_measured_mean():
    usage = [
        _usage('', 'filter', 1000, 15, 4),  # the dataset's baseline: 15 ms, bucket [8, 16)
        _usage('country', 'filter', 2, 17, 5),  # 13% slower, but across a power of two: bucket [16, 32)
        _usage('site', 'filter', 2, 40, 6),  # more than twice as slow
    ]
    snapshot = _dataset_snapshot(usage, fields=('country', 'site'))
    snapshot['indexes'] = []
    result = index_advisor.suggestions(snapshot, {})
    assert [item['field'] for item in result] == ['site']  # the old code also suggested `country`


# --- tests for the surviving mutations ----------------------------------------------------------


def test_an_idle_keeper_is_never_offered_even_with_more_cumulative_scans():
    indexes = [
        ix('narrow', 1, keys=['a'], scans=10, last_scan=NOW - timedelta(days=1)),
        ix('wide', 2, keys=['a', 'b'], scans=5000, last_scan=NOW - timedelta(days=20), size=300),
    ]
    observations = [{'oid': oid, 'first_seen_at': NOW - timedelta(days=30)} for oid in (1, 2)]
    review, _ = index_advisor.build_review(snap(indexes, observations=observations), None)
    assert [item['name'] for item in review['possibly_unused']] == ['wide']
    assert review['overlapping'] == []  # removing `narrow` as redundant to an idle `wide` would remove both


def test_gin_and_gist_indexes_do_not_serve_a_foreign_key():
    indexes = [
        ix('child_gin', 1, table='child', method='gin', keys=['parent_id']),
        ix('child_gist', 2, table='child', method='gist', keys=['parent_id']),
        ix('child_btree', 3, table='child', keys=['parent_id']),
    ]
    keys = [{'name': 'child_parent_fk', 'table_name': 'child', 'parent': 'parent', 'columns': ['parent_id']}]
    needed = index_advisor.needed_reasons(indexes, keys)
    assert list(needed) == [3]  # the B-tree is the only index a foreign key lookup can use


# --- the lock-wait sampler -----------------------------------------------------------------------


class _Pool:
    """A pool whose connections answer the sampler's query from `answers` (then False: not waiting)."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.held = 0
        self.acquired = 0

    def acquire(self, timeout=None):
        pool = self

        class Connection:
            async def fetchval(self, *args, **kwargs):
                return pool.answers.pop(0) if pool.answers else False

        class Borrow:
            async def __aenter__(self):
                pool.held += 1
                pool.acquired += 1
                return Connection()

            async def __aexit__(self, *error):
                pool.held -= 1

        return Borrow()


def test_the_sampler_borrows_a_connection_per_sample_and_counts_waits():
    async def scenario():
        pool = _Pool([True, True, True])
        held = []
        async with index_jobs._lock_waits(pool, 4242) as waits:
            for _ in range(4):
                await asyncio.sleep(index_jobs.WAIT_SAMPLE_SECONDS * 0.6)
                held.append(pool.held)
        return pool, held, waits

    pool, held, waits = asyncio.run(scenario())
    assert pool.acquired >= 3 and pool.held == 0
    assert held.count(0) >= 3  # the old code held one connection for the whole build
    assert waits.seconds is not None and waits.seconds > 0


def test_a_backend_missing_from_pg_stat_activity_means_the_waits_were_not_watched():
    async def scenario():
        async with index_jobs._lock_waits(_Pool([None]), 4242) as waits:
            await asyncio.sleep(index_jobs.WAIT_SAMPLE_SECONDS * 2)
        return waits

    assert asyncio.run(scenario()).seconds is None  # the old code counted it as "not waiting": waits as building


def test_a_sub_second_build_never_reads_as_zero_seconds():
    assert index_jobs.measured_build_seconds(0.3, 0.29) == index_jobs.WAIT_SAMPLE_SECONDS
    assert index_jobs.measured_build_seconds(0.2, 0.5) == index_jobs.WAIT_SAMPLE_SECONDS
    assert index_jobs.measured_build_seconds(10.0, 4.0) == 6.0


# --- a stopped database container shuts down cleanly ---------------------------------------------
# The compose files are deliberately unchanged: any change to the db service makes the next `docker compose
# up` recreate the database container (test_backup_restore.test_database_compose_config_does_not_trigger_a_
# recreate). JOBS.md documents stopping it with time for the shutdown checkpoint instead.


def test_the_clean_stop_is_documented_with_a_grace_derived_from_shared_buffers():
    shared = re.search(r'^shared_buffers = (\d+)GB', (ROOT / 'backend/db/postgresql.conf').read_text(), re.M)
    assert shared
    jobs = (ROOT / 'docs/architecture/JOBS.md').read_text()
    grace = re.search(r'docker stop -t (\d+) freedomridge-db', jobs)
    assert grace, 'JOBS.md must say how to stop the database without killing it'
    # Every dirty buffer is written at shutdown: shared_buffers at 100 MB/s must fit in the grace period.
    assert int(grace.group(1)) >= int(shared.group(1)) * 1024 / 100


# --- the review's default slot ------------------------------------------------------------------


def test_the_review_slot_carries_past_midnight():
    assert dataset_schedules.review_default_slot({'time': '03:00', 'window_minutes': 180, 'weekday': 0}) == (0, '06:00')
    assert dataset_schedules.review_default_slot({'time': '22:00', 'window_minutes': 180, 'weekday': 6}) == (0, '01:00')
    assert (dataset_schedules.REVIEW_DEFAULTS['weekday'], dataset_schedules.REVIEW_DEFAULTS['time']) == (0, '06:00')
