"""Index advisor review (backend/index_advisor.py): the rules on synthetic snapshots, and
the review job, its scheduling, its listing and its permissions against real PostgreSQL catalogs."""

import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import dataset_schedules, field_usage, index_advisor, index_jobs, job_queue
from backend.routers import jobs as jobs_router
from backend.routers import schedules
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.test_field_registry_postgres import ingest

registry_database = test_field_registry_postgres.registry_database
requires_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL test database',
)
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
ADMIN: Dict[str, Any] = {'id': 1, 'role': 'admin', 'username': 'admin'}
POWER: Dict[str, Any] = {'id': 2, 'role': 'power_user', 'username': 'power'}
ROOT = Path(__file__).resolve().parents[2]


def ix(name, oid, **options):
    keys = options.pop('keys', ['a'])
    return {
        'oid': oid,
        'name': name,
        'table_name': options.pop('table', 't'),
        'method': options.pop('method', 'btree'),
        'is_unique': options.pop('unique', False),
        'is_primary': options.pop('primary', False),
        'is_exclusion': False,
        'valid': options.pop('valid', True),
        'ready': True,
        'replica_identity': False,
        'constraint_backed': options.pop('constraint_backed', False),
        'building': options.pop('building', False),
        'definition': options.pop('definition', f'CREATE INDEX {name} ON t (a)'),
        'predicate': options.pop('predicate', None),
        'keys': keys,
        'include': options.pop('include', []),
        'families': options.pop('families', [1] * len(keys)),
        'collations': options.pop('collations', [0] * len(keys)),
        'options': options.pop('options', [0] * len(keys)),
        'size_bytes': options.pop('size', 8192),
        'scans': options.pop('scans', 0),
        'last_scan': options.pop('last_scan', None),
        **options,
    }


def snap(indexes, *, stats_start=NOW - timedelta(days=30), observations=None, foreign_keys=(), **extra):
    return {
        'now': NOW,
        'stats_start': stats_start,
        'system_identifier': 7,
        'database_oid': 70,
        'indexes': list(indexes),
        'operators': extra.pop('operators', {1: {'1:s'}, 2: {'1:s', '2:s', '3:s'}, 3: {'1:s', '9:s'}}),
        'foreign_keys': list(foreign_keys),
        'observations': [
            {'system_identifier': 7, 'database_oid': 70, 'last_scans': 0, **row} for row in (observations or [])
        ],
        'datasets': {1, 2},
        'removed': set(),
        **extra,
    }


# --- rules on synthetic snapshots -----------------------------------------------------------------


def test_needed_indexes_include_constraints_and_the_only_index_for_a_foreign_key():
    indexes = [
        ix('t_pkey', 1, primary=True, unique=True, constraint_backed=True, keys=['id']),
        ix('t_code', 2, unique=True, keys=['code']),
        ix('t_parent', 3, keys=['parent_id']),
        ix('u_parent', 4, table='u', keys=['parent_id']),
        ix('u_parent_more', 5, table='u', keys=['parent_id', 'x']),
    ]
    keys = [
        {'name': 't_parent_fk', 'table_name': 't', 'parent': 'p', 'columns': ['parent_id']},
        {'name': 'u_parent_fk', 'table_name': 'u', 'parent': 'p', 'columns': ['parent_id']},
    ]
    needed = index_advisor.needed_reasons(indexes, keys)
    assert needed[1] == ['primary key'] and needed[2] == ['enforces uniqueness']
    assert 'only index for foreign key t_parent_fk' in needed[3][0]
    assert 4 not in needed and 5 not in needed  # two indexes serve u's key: neither is the only one


def test_overlaps_duplicates_prefixes_and_operator_subsets():
    indexes = [
        ix('dup_used', 1, keys=['a'], scans=9),
        ix('dup_idle', 2, keys=['a'], scans=0),
        ix('lead', 3, keys=['b']),
        ix('lead_more', 4, keys=['b', 'c']),
        ix('gin_narrow', 5, method='gin', keys=['metadata'], families=[1], size=5),
        ix('gin_wide', 6, method='gin', keys=['metadata'], families=[2], size=13, scans=133),
        ix('gin_other', 7, method='gin', keys=['metadata'], families=[3]),  # not a subset either way
        ix('uniq', 8, unique=True, keys=['d']),
        ix('plain_d', 9, keys=['d', 'e']),
        ix('partial', 10, keys=['b'], predicate='(x = 1)'),  # another predicate: no overlap with `lead`
    ]
    pairs = {pair['redundant']['name']: pair for pair in index_advisor.overlaps(indexes, snap([])['operators'], {})}
    assert pairs['dup_idle']['kept']['name'] == 'dup_used' and pairs['dup_idle']['rule'] == 'duplicate'
    assert 'dup_used' not in pairs
    assert pairs['lead']['kept']['name'] == 'lead_more' and pairs['lead']['rule'] == 'prefix'
    assert pairs['gin_narrow']['kept']['name'] == 'gin_wide' and pairs['gin_narrow']['rule'] == 'operators'
    assert 'gin_wide' not in pairs and 'gin_other' not in pairs
    assert 'uniq' not in pairs  # a unique index enforces something the wider one does not
    assert 'partial' not in pairs


def test_a_needed_index_is_never_offered_as_redundant():
    indexes = [ix('a1', 1, keys=['a']), ix('a2', 2, keys=['a'], scans=5)]
    pairs = index_advisor.overlaps(indexes, {1: {'1:s'}}, {1: ['only index for foreign key k']})
    # a1 is needed, so its duplicate is the one to remove, even though the duplicate is used more.
    assert [(pair['redundant']['name'], pair['kept']['name']) for pair in pairs] == [('a2', 'a1')]
    assert index_advisor.overlaps(indexes, {1: {'1:s'}}, {1: ['k'], 2: ['k']}) == []


def test_first_seen_is_never_back_dated():
    start = NOW - timedelta(days=30)
    indexes = [ix('old', 1, scans=5), ix('new', 2), ix('reset', 3, scans=1)]
    # The very first review, with nothing observed yet: every index is first seen now.
    assert set(index_advisor.first_seen(snap(indexes, stats_start=start)).values()) == {NOW}
    # Observations exist only for another cluster (a dump restored here, or pg_upgrade): every index is new.
    other = snap(indexes, observations=[{'oid': 1, 'first_seen_at': start, 'system_identifier': 99}])
    assert set(index_advisor.first_seen(other).values()) == {NOW}
    # A known cluster: kept, new OID now, and a counter that went backwards (a per-table reset) now.
    known = snap(
        indexes,
        observations=[
            {'oid': 1, 'first_seen_at': start, 'last_scans': 5},
            {'oid': 3, 'first_seen_at': start, 'last_scans': 4},
        ],
    )
    assert index_advisor.first_seen(known) == {1: start, 2: NOW, 3: NOW}


def test_unused_needs_a_full_window_of_observation():
    indexes = [
        ix('idle_long', 1, size=300),
        ix('idle_recent_use', 2, scans=4, last_scan=NOW - timedelta(days=2)),
        ix('idle_since_use', 3, scans=4, last_scan=NOW - timedelta(days=16), size=200),
        ix('seen_lately', 4),
        ix('broken', 5, valid=False, size=100),
        ix('being_built', 6, building=True),
        ix('t_pkey', 7, primary=True, unique=True, constraint_backed=True),
        ix('used_no_time', 8, scans=3),  # PostgreSQL before 16: used, last use unknown
    ]
    observations = [{'oid': oid, 'first_seen_at': NOW - timedelta(days=20)} for oid in (1, 2, 3, 5, 6, 7, 8)]
    observations.append({'oid': 4, 'first_seen_at': NOW - timedelta(days=2)})
    review, _ = index_advisor.build_review(snap(indexes, observations=observations), None)
    assert [item['name'] for item in review['possibly_unused']] == ['idle_long', 'idle_since_use', 'broken']
    assert review['possibly_unused'][2]['invalid'] is True
    assert [item['name'] for item in review['too_new']] == ['seen_lately']
    assert [item['name'] for item in review['building']] == ['being_built']
    assert [item['name'] for item in review['needed']] == ['t_pkey']
    assert review['enough_history'] is True
    young, _ = index_advisor.build_review(snap(indexes, stats_start=NOW - timedelta(days=3)), None)
    assert young['enough_history'] is False
    assert [item['name'] for item in young['possibly_unused']] == ['broken']  # never used by queries anyway


def _usage(field, usage, uses, mean_ms, bucket):
    histogram = [0] * field_usage.BUCKETS
    histogram[bucket] = uses
    return {
        'usage': usage,
        'field_name': field,
        'day': date(2026, 10, 21),
        'uses': uses,
        'total_ms': uses * mean_ms,
        'latency_log2': histogram,
    }


def _dataset_snapshot(usage, *, rates=(), promoted=(), fields=('country', 'score', 'site', 'flag', 'notes')):
    types = {'flag': 'boolean'}
    field_index = ix(
        'idx_samples_meta_1_score',
        1,
        table='samples',
        definition="CREATE INDEX idx_samples_meta_1_score ON public.samples USING btree "
        "((((metadata ->> 'score'::text))::double precision)) WHERE (dataset_id = 1)",
        predicate='(dataset_id = 1)',
    )
    return snap(
        [field_index],
        dataset={'id': 1, 'name': 'd', 'deleting': False},
        fields=[
            {
                'field_name': name,
                'field_type': types.get(name, 'string'),
                'origin': 'metadata',
                'is_promoted': name in promoted or name == 'score',
            }
            for name in fields
        ],
        usage=usage,
        build_rates=list(rates),
        samples_bytes=10**9,
    )


def test_suggestions_compare_with_indexed_fields_and_pay_back_the_build():
    usage = [
        _usage('score', 'filter', 100, 50, 6),  # indexed: the reference
        _usage('country', 'filter', 40, 3050, 12),  # 3 s slower x 40 = 120 s of extra waiting
        _usage('site', 'filter', 2, 3050, 12),  # 6 s of extra waiting
        _usage('notes', 'filter', 50, 20, 5),  # faster than the indexed field: not slow
        _usage('flag', 'filter', 90, 9000, 14),  # a boolean: promote cannot index it
    ]
    unmeasured = index_advisor.suggestions(_dataset_snapshot(usage), {'score': _dataset_snapshot(usage)['indexes'][0]})
    assert [(item['field'], item['pays_back']) for item in unmeasured] == [('country', None), ('site', None)]
    country = unmeasured[0]
    assert country['excess_seconds'] == pytest.approx(120)
    assert country['usages']['filter'] == {
        'uses': 40,
        'mean_ms': 3050.0,
        'p50_ms': 4096.0,
        'p95_ms': 4096.0,
        'reference_ms': 50.0,
        'slower': True,
    }
    # A measured build of 60 s on this heap: country's 120 s of waiting pays it back, site's 6 s does not.
    measured_snapshot = _dataset_snapshot(usage, rates=[60 / 10**9])
    measured = index_advisor.suggestions(measured_snapshot, {'score': measured_snapshot['indexes'][0]})
    assert [(item['field'], item['pays_back'], item['build_seconds']) for item in measured] == [('country', True, 60.0)]


def test_without_indexed_fields_the_reference_is_the_datasets_own_average():
    usage = [_usage('', 'filter', 90, 100, 7), _usage('country', 'filter', 10, 900, 10)]
    result = index_advisor.suggestions(_dataset_snapshot(usage, fields=('country',)), {})
    assert [item['field'] for item in result] == ['country']
    assert result[0]['usages']['filter']['reference_ms'] == 180.0  # mean over the dataset's filter requests


def test_a_promoted_field_whose_index_is_missing_is_suggested_first():
    result = index_advisor.suggestions(_dataset_snapshot([], promoted=('site',)), {})
    assert result[0]['field'] == 'score' or result[0]['reason'] == 'index_missing'
    assert {item['field'] for item in result if item['reason'] == 'index_missing'} == {'score', 'site'}


def test_percentiles_are_bucket_upper_edges():
    assert index_advisor.percentile([0, 3, 1] + [0] * 13, 0.5) == 2.0
    assert index_advisor.percentile([0, 3, 1] + [0] * 13, 0.95) == 4.0
    assert index_advisor.percentile([0] * 16, 0.5) is None


def test_core_indexes_follow_schema_sql_creates_and_drops():
    core = index_advisor.core_indexes()
    assert core['idx_samples_metadata_gin'] == 'samples'
    assert core['idx_samples_dataset_id_id'] == 'samples'
    assert 'idx_embeddings_visualization' not in core  # 058 retired it


# --- real catalogs --------------------------------------------------------------------------------


async def _review(database, dataset_id=None):
    snapshot = await index_advisor.collect(database.pool, dataset_id)
    review, _ = index_advisor.build_review(snapshot, dataset_id)
    return review


@requires_database
def test_system_review_finds_the_live_overlaps_and_honours_removed_core_indexes(registry_database):
    async def scenario(database):
        review = await _review(database)
        pairs = {pair['redundant']['name']: pair for pair in review['overlapping']}
        # The live case: GIN jsonb_path_ops on metadata next to GIN jsonb_ops on the same column.
        assert pairs['idx_samples_metadata_gin']['kept']['name'] == 'idx_samples_metadata'
        assert pairs['idx_samples_metadata_gin']['rule'] == 'operators'
        assert pairs['idx_samples_metadata_gin']['redundant']['kind'] == 'core'
        # idx_samples_dataset_id(dataset_id) leads several wider B-trees; any of them serves its searches.
        assert pairs['idx_samples_dataset_id']['rule'] == 'prefix'
        assert pairs['idx_samples_dataset_id']['kept']['name'] in {
            'idx_samples_dataset_id_id',
            'idx_samples_dataset_created_at',
            'idx_samples_dataset_media_type',
        }
        needed = {item['name']: item['needed'] for item in review['needed']}
        assert needed['samples_pkey'] == ['primary key']
        assert needed['idx_jobs_lane_owner'] == ['enforces uniqueness']
        assert review['missing_core'] == []
        assert review['enough_history'] in (True, False)  # a throwaway cluster: whatever its age
        await database.execute('DROP INDEX idx_samples_tags_gin')
        assert (await _review(database))['missing_core'] == [{'name': 'idx_samples_tags_gin', 'table': 'samples'}]
        await database.execute(
            """INSERT INTO removed_indexes (index_name, table_name, definition, kind)
               VALUES ('idx_samples_tags_gin', 'samples',
                       'CREATE INDEX idx_samples_tags_gin ON samples USING gin (tags)', 'core')""",
        )
        assert (await _review(database))['missing_core'] == []

    registry_database(scenario)


@requires_database
def test_a_foreign_key_index_is_needed_only_while_it_is_the_only_one(registry_database):
    async def scenario(database):
        await database.execute(
            """CREATE TABLE advisor_child (id serial PRIMARY KEY, dataset_id int REFERENCES datasets(id), x int);
               CREATE INDEX advisor_child_dataset ON advisor_child (dataset_id)""",
        )
        needed = {item['name']: item['needed'] for item in (await _review(database))['needed']}
        assert 'advisor_child_dataset' in needed and 'foreign key' in needed['advisor_child_dataset'][0]
        await database.execute('CREATE INDEX advisor_child_dataset_x ON advisor_child (dataset_id, x)')
        review = await _review(database)
        assert 'advisor_child_dataset' not in {item['name'] for item in review['needed']}
        pairs = {pair['redundant']['name']: pair['kept']['name'] for pair in review['overlapping']}
        assert pairs['advisor_child_dataset'] == 'advisor_child_dataset_x'

    registry_database(scenario)


@requires_database
def test_statistics_start_follows_a_reset(registry_database):
    async def scenario(database):
        await database.execute('SELECT pg_stat_reset()')
        reset_at = await database.fetchval('SELECT clock_timestamp()')
        snapshot = await index_advisor.collect(database.pool, None)
        assert snapshot['stats_start'] >= reset_at - timedelta(seconds=1)
        assert (await _review(database))['enough_history'] is False

    registry_database(scenario)


@requires_database
def test_dataset_review_suggests_from_telemetry_and_lists_only_its_own_field_indexes(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US', 'score': index, 'idle': 'x'} for index in range(4)])
        await ingest([{'other': 1}], dataset_id=2)
        for dataset_id, field in ((1, 'score'), (1, 'idle'), (2, 'other')):
            await database.execute(
                f"""CREATE INDEX idx_samples_meta_{dataset_id}_{field} ON samples ((metadata->>'{field}'))
                    WHERE dataset_id = {dataset_id}""",
            )
            await database.execute(
                'UPDATE dataset_fields SET is_promoted = TRUE WHERE dataset_id = $1 AND field_name = $2',
                dataset_id,
                field,
            )
        today = field_usage._today()
        await database.execute(
            """INSERT INTO field_usage_daily VALUES
               (1, 'filter', 'score', $1, 100, 5000, array_fill(0::bigint, ARRAY[6]) || 100::bigint
                                                     || array_fill(0::bigint, ARRAY[9])),
               (1, 'filter', 'country', $1, 20, 60000, array_fill(0::bigint, ARRAY[12]) || 20::bigint
                                                       || array_fill(0::bigint, ARRAY[3])),
               (1, 'filter', 'country', $2, 999, 9999999, array_fill(0::bigint, ARRAY[16]))""",
            today,
            today - timedelta(days=field_usage.WINDOW_DAYS),  # outside the window: ignored
        )
        review = await _review(database, 1)
        assert [item['field'] for item in review['suggested']] == ['country']
        assert review['suggested'][0]['usages']['filter']['uses'] == 20
        assert review['suggested'][0]['pays_back'] is None  # no build measured yet
        names = {item['name'] for item in review['too_new'] + review['possibly_unused']}
        assert names <= {'idx_samples_meta_1_score', 'idx_samples_meta_1_idle'}
        assert 'idx_samples_meta_2_other' not in str(review)
        assert review['telemetry_since'] == today.isoformat()

    registry_database(scenario)


@requires_database
def test_review_job_is_lane_free_joins_and_records_observations(registry_database, monkeypatch):
    async def scenario(database):
        from backend import access

        monkeypatch.setattr(access, 'db', database)
        monkeypatch.setattr(jobs_router, 'db', database)
        await ingest([{'country': 'US'}])
        job, created = await index_advisor.submit(database.pool, 1, ADMIN['id'])
        again, joined = await index_advisor.submit(database.pool, 1, POWER['id'])
        assert created and not joined and again['id'] == job['id']
        assert job['dataset_id'] is None and job['lane'] == 'user'
        assert job['args'] == {'scope': 'dataset', 'dataset_id': 1}
        # Listed with the dataset's jobs, without owning its lane.
        active = await jobs_router.list_active_dataset_jobs(1, limit=25, cursor=None, user=ADMIN)
        assert [(item.kind, item.dataset_lane) for item in active.waiting] == [('index_review', False)]
        history = await jobs_router.list_dataset_jobs(1, limit=50, cursor=None, user=ADMIN)
        assert [item.kind for item in history.items] == ['index_review']
        assert not (await jobs_router.list_dataset_jobs(2, limit=50, cursor=None, user=ADMIN)).items
        # A long job holding the dataset's user lane does not hold the review back.
        await job_queue.enqueue_job(database.pool, 'bulk_mutation', dataset_id=1, lane='user')
        owner = await job_queue.claim_job(database.pool, 'test', kinds=['bulk_mutation'], lease_seconds=3600)
        assert owner is not None and owner['state'] == 'running'
        claimed = await job_queue.claim_job(database.pool, 'test', kinds=[index_advisor.KIND])
        assert claimed is not None and claimed['id'] == job['id']
        finished = await job_helpers.step(database, claimed, index_advisor.handler(database.pool))
        assert finished['state'] == 'succeeded'
        assert finished['message'].startswith('Index review: 0 suggested')
        assert finished['result']['scope'] == 'dataset' and 'suggested' in finished['result']
        observed = await database.fetch('SELECT index_oid, first_seen_at FROM index_observations')
        assert len(observed) == await database.fetchval(
            "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            'WHERE c.relnamespace = current_schema()::regnamespace',
        )
        # The next review keeps when each index was first seen.
        second, _ = await index_advisor.submit(database.pool, None, ADMIN['id'])
        claimed = await job_queue.claim_job(database.pool, 'test', kinds=[index_advisor.KIND])
        await job_helpers.step(database, claimed, index_advisor.handler(database.pool))
        assert sorted(await database.fetch('SELECT index_oid, first_seen_at FROM index_observations')) == sorted(
            observed,
        )
        async with database.pool.acquire() as connection:
            state = await index_advisor.latest(connection, None)
            assert state['review']['scope'] == 'system' and state['active_job'] is None
            assert (await index_advisor.latest(connection, 1))['review']['dataset_id'] == 1

    registry_database(scenario)


@requires_database
def test_review_of_a_deleted_dataset_finishes_skipped(registry_database):
    async def scenario(database):
        await ingest([{'country': 'US'}])
        job, _ = await index_advisor.submit(database.pool, 1, ADMIN['id'])
        await database.execute('UPDATE datasets SET deleting = TRUE WHERE id = 1')
        claimed = await job_queue.claim_job(database.pool, 'test', kinds=[index_advisor.KIND])
        finished = await job_helpers.step(database, claimed, index_advisor.handler(database.pool))
        assert finished['state'] == 'succeeded' and finished['result']['skipped'] == 'The dataset was deleted'

    registry_database(scenario)


@requires_database
def test_field_index_builds_record_their_measured_time(registry_database):
    async def scenario(database):
        from backend.routers import datasets

        await ingest([{'score': 1}, {'score': 2}])
        async with job_helpers.runner(database, kinds=[index_jobs.FIELD_KIND]):
            await datasets.promote_field(1, 'score', user=ADMIN)
        checkpoint = await database.fetchval(
            "SELECT checkpoint FROM jobs WHERE kind = 'field_index_build' AND state = 'succeeded'",
        )
        assert checkpoint['build_seconds'] >= 0 and checkpoint['table_bytes'] > 0
        snapshot = await index_advisor.collect(database.pool, 1)
        assert len(snapshot['build_rates']) == 1 and index_advisor.build_estimate(snapshot) is not None

    registry_database(scenario)


# --- scheduling -----------------------------------------------------------------------------------


@requires_database
def test_dataset_and_system_review_schedules_queue_reviews_when_due(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(schedules, 'db', database)
        await ingest([{'country': 'US'}])
        rule = {'enabled': True, 'frequency': 'weekly', 'weekday': 0, 'time': '03:00'}
        with pytest.raises(HTTPException) as refused:
            await schedules.create_schedule(1, schedules.ScheduleCreate(job_type='index_review', **rule), user=POWER)
        assert refused.value.status_code == 403
        created = await schedules.create_schedule(
            1,
            schedules.ScheduleCreate(job_type='index_review', min_new_samples=20000, **rule),
            user=ADMIN,
        )
        assert created['target'] == 'indexes' and created['map'] is None
        assert created['min_new_samples'] is None and created['next_run_at'] is not None
        with pytest.raises(HTTPException):
            await schedules.update_schedule(1, created['id'], schedules.ScheduleRule(**rule), user=POWER)
        await database.execute(
            """UPDATE system_job_schedules SET enabled = TRUE, next_run_at = now() - interval '1 minute'
               WHERE job_type = 'index_review'""",
        )
        await database.execute('UPDATE dataset_job_schedules SET next_run_at = now() - interval $$1 minute$$')
        queued = await dataset_schedules.schedule_dataset_jobs(database.pool)
        assert sorted(job['args']['scope'] for job in queued) == ['dataset', 'system']
        assert all(job['kind'] == 'index_review' and job['created_by'] is None for job in queued)
        rows = await database.fetch(
            "SELECT next_run_at, last_check FROM dataset_job_schedules WHERE job_type = 'index_review' "
            "UNION ALL SELECT next_run_at, last_check FROM system_job_schedules",
        )
        now = await database.fetchval('SELECT now()')
        assert all(row['next_run_at'] > now and row['last_check']['ran'] for row in rows)
        assert await dataset_schedules.schedule_dataset_jobs(database.pool) == []  # not due again
        listed = (await schedules.list_schedules(1))['items']
        assert listed[0]['last_job']['state'] == 'queued'
        job, created_now = await dataset_schedules.run_now(
            database.pool,
            dict(
                await database.fetchrow(
                    f'SELECT {dataset_schedules.COLUMNS} FROM dataset_job_schedules s',
                ),
            ),
            ADMIN['id'],
        )
        assert not created_now and job['args'] == {'scope': 'dataset', 'dataset_id': 1}  # joined the queued one

    registry_database(scenario)


def test_system_schedule_uses_wall_clock_rules():
    rule = {'frequency': 'weekly', 'weekday': 0, 'time_of_day': time(3, 0), 'timezone': 'America/New_York'}
    assert dataset_schedules.next_slot(rule, NOW).astimezone(timezone.utc) == datetime(
        2026,
        10,
        25,
        7,
        0,
        tzinfo=timezone.utc,
    )


# --- permissions ----------------------------------------------------------------------------------


@pytest.mark.parametrize('role', ['power_user', 'viewer'])
def test_only_administrators_can_use_the_advisor(role):
    from backend.main import app
    from backend.routers.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {'id': 5, 'username': 'u', 'role': role}
    try:
        client = TestClient(app)
        assert client.get('/api/datasets/1/index-advisor').status_code == 403
        assert client.post('/api/datasets/1/index-advisor/review').status_code == 403
        assert client.get('/api/admin/index-advisor').status_code == 403
        assert client.post('/api/admin/index-advisor/review').status_code == 403
        assert client.put('/api/admin/index-advisor/schedule', json={'enabled': True}).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# --- migration ------------------------------------------------------------------------------------
