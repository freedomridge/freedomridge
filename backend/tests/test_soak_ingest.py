"""Offline safety and verdict tests; telemetry integration uses the assigned test database."""

import importlib.util
import os
import random
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('soak_ingest', ROOT / 'scripts' / 'soak_ingest.py')
assert spec is not None and spec.loader is not None
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)

API = 'http://127.0.0.1:18460/api'
DSN = 'postgres://freedomridge:password@127.0.0.1:55460/freedomridge'


@pytest.mark.parametrize('acknowledged', [False, True])
@pytest.mark.parametrize(
    'api,dsn,container',
    [
        ('http://127.0.0.1:8000/api', DSN, 'fr-cx-soak-db'),
        ('http://localhost:8000/api', DSN, 'fr-cx-soak-db'),
        ('http://[::1]:8000/api', DSN, 'fr-cx-soak-db'),
        (API, 'postgres://u:p@freedomridge-db:5432/db', 'fr-cx-soak-db'),
        (API, DSN, 'freedomridge-db'),
        (API, DSN, 'FREEDOMRIDGE-DB.'),
        ('http://FREEDOMRIDGE-DB.:18460/api', DSN, 'fr-cx-soak-db'),
        (API, 'postgres://u:p@freedomridge%2ddb:5432/db', 'fr-cx-soak-db'),
    ],
)
def test_live_is_never_overridden(api, dsn, container, acknowledged):
    with pytest.raises(ValueError, match='forbidden'):
        soak.validate_target(api, dsn, container, acknowledged)


@pytest.mark.parametrize('dsn', ['postgres://u:p@127.0.0.1:5432/db', 'postgresql://u:p@localhost:5432/freedomridge'])
def test_live_database_port_is_refused(dsn):
    with pytest.raises(ValueError, match="live database's published port"):
        soak.validate_target(API, dsn, 'fr-cx-soak-db', True)


def test_explicit_nonlive_acknowledgment_required():
    with pytest.raises(ValueError, match='i-know-this-is-not-live'):
        soak.validate_target(API, DSN, 'fr-cx-soak-db')
    soak.validate_target(API, DSN, 'fr-cx-soak-db', True)


@pytest.mark.parametrize(
    'api,dsn',
    [
        ('', DSN),
        (API, ''),
        ('http://localhost/api', DSN),
        (API, 'postgres://localhost/db'),
        (API, DSN + '?host=freedomridge-db'),
        (API + '?target=live', DSN),
        ('http://user@127.0.0.1:18460/api', DSN),
    ],
)
def test_implicit_or_overridden_targets_rejected(api, dsn):
    with pytest.raises(ValueError):
        soak.validate_target(api, dsn, 'fr-cx-soak-db', True)


def evidence():
    samples = [
        {
            'elapsed_s': i * 10,
            'inserted': i * 100,
            'data_bytes': 100000 + i * 10000,
            'queues': {'dataset_stats_pending': {'count': 0, 'oldest_s': None}},
            'health_ok': True,
            'failed_jobs': 0,
            'api_errors': [],
            'error': '',
        }
        for i in range(10)
    ]
    batches = [{'latency_s': 0.5, 'error': ''} for _ in range(20)]
    return samples, batches


def verdict(samples, batches, burst_ends=(20, 60)):
    return soak.evaluate(
        samples,
        batches,
        burst_ends,
        p95_limit=1,
        recovery_seconds=20,
        near_zero=2,
        growth_ratio=1.5,
    )


def test_healthy_run_passes_and_uses_growth_not_absolute_size():
    samples, batches = evidence()
    # A large pre-existing dataset must not dilute the bytes/new-sample slope.
    for sample in samples:
        sample['data_bytes'] += 100_000_000_000
    result = verdict(samples, batches)
    assert result['passed']
    assert result['batch_p50_s'] == result['batch_p95_s'] == 0.5
    assert result['early_bytes_per_sample'] == result['late_bytes_per_sample'] == 100


@pytest.mark.parametrize('failure', ['health', 'jobs', 'ui', 'telemetry', 'ingest', 'latency', 'bloat'])
def test_failures_are_never_hidden_by_later_recovery(failure):
    samples, batches = evidence()
    if failure == 'health':
        samples[1]['health_ok'] = False
    elif failure == 'jobs':
        samples[1]['failed_jobs'] = 1
    elif failure == 'ui':
        samples[1]['api_errors'] = ['timeout']
    elif failure == 'telemetry':
        samples[1]['error'] = 'timeout'
        samples[1]['data_bytes'] = None
    elif failure == 'ingest':
        batches[1]['error'] = 'retry inserted rows'
    elif failure == 'latency':
        batches[-2]['latency_s'] = batches[-1]['latency_s'] = 1
    elif failure == 'bloat':
        for i, sample in enumerate(samples[6:], 1):
            sample['data_bytes'] += i * 100000
    assert not verdict(samples, batches)['passed']


def test_each_queue_must_recover_by_deadline():
    samples, batches = evidence()
    for sample in samples:
        if sample['elapsed_s'] < 50:
            sample['queues']['concept_pending'] = {'count': 8, 'oldest_s': 12}
    result = verdict(samples, batches)
    assert not result['passed']
    assert any('after burst at 20s' in failure for failure in result['failures'])


def test_recovery_can_occur_before_deadline_without_waiting_until_end():
    samples, batches = evidence()
    samples[2]['queues']['dataset_stats_pending']['count'] = 10
    samples[4]['queues']['dataset_stats_pending']['count'] = 10
    assert verdict(samples, batches)['passed']


def test_missing_recovery_observation_and_missing_queues_fail():
    samples, batches = evidence()
    assert not verdict(samples, batches, burst_ends=(100,))['passed']
    for sample in samples:
        sample['queues'] = {}
    assert not verdict(samples, batches)['passed']


def test_missing_disk_windows_and_no_batches_fail():
    samples, batches = evidence()
    assert not verdict(samples[:3], batches, burst_ends=())['passed']
    assert not verdict(samples, [], burst_ends=())['passed']
    assert not verdict([], batches, burst_ends=())['passed']


def test_synthetic_payload_has_both_vectors_typed_fields_and_duplicate_hashes():
    rows = soak.synthetic_batch(0, 3, random.Random(7), 'clip', 'dino')
    assert len({r['filepath'] for r in rows}) == 3
    assert rows[0]['hashes'] == rows[1]['hashes'] != rows[2]['hashes']
    for row in rows:
        assert len(row['embeddings']['clip']) == 512
        assert len(row['embeddings']['dino']) == 1024
        assert sum(v * v for v in row['embeddings']['clip']) == pytest.approx(1, abs=1e-5)
        assert isinstance(row['metadata']['reviewed'], bool)
        assert isinstance(row['metadata']['segment'], int)
        assert row['location']['type'] == 'Point'


async def test_readonly_telemetry_discovers_queues_and_measures_wal():
    dsn = os.getenv('FRIDGE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly configured disposable database')
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute('SET default_transaction_read_only=on')
        telemetry = soak.Telemetry(conn)
        await telemetry.discover()
        assert telemetry.queues['dataset_stats_pending'] == 'requested_at'
        first = await telemetry.sample()
        second = await telemetry.sample()
        assert second['database_bytes'] > 0
        assert second['wal_total_bytes'] >= first['wal_total_bytes'] >= 0
        assert second['wal_delta_bytes'] >= 0
        assert {'samples', 'embeddings'} <= {r['name'] for r in second['relations']}
    finally:
        await conn.close()


def test_existing_dataset_is_never_cleaned_up(monkeypatch, capsys, tmp_path):
    # --cleanup deletes a whole dataset; with --dataset-id that would be a restored real dataset.
    argv = [
        'soak_ingest.py',
        '--api-url',
        API,
        '--dsn',
        DSN,
        '--container',
        'fr-cx-soak-db',
        '--i-know-this-is-not-live',
        '--output',
        str(tmp_path / 'out'),
        '--dataset-id',
        '7',
        '--cleanup',
    ]
    monkeypatch.setattr('sys.argv', argv)
    monkeypatch.setattr(soak, 'run', lambda args: pytest.fail('must refuse before running'))
    with pytest.raises(SystemExit) as exit_info:
        soak.main()
    assert exit_info.value.code == 2
    assert 'refused with --dataset-id' in capsys.readouterr().err


def test_runs_into_an_existing_dataset_use_their_own_paths():
    # With --dataset-id, a second run must not regenerate the first run's paths (all skipped as duplicates).
    first = soak.synthetic_batch(0, 2, random.Random(7), 'clip', 'dino', 'soak/run-a/')
    second = soak.synthetic_batch(0, 2, random.Random(7), 'clip', 'dino', 'soak/run-b/')
    assert {s['filepath'] for s in first}.isdisjoint({s['filepath'] for s in second})
    assert soak.synthetic_batch(0, 1, random.Random(7), 'clip', 'dino')[0]['filepath'] == 'soak/000000000000.jpg'
