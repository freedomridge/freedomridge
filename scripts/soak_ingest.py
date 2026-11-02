#!/usr/bin/env python3
"""Continuous SDK ingest against an explicitly acknowledged disposable stack."""

import argparse
import asyncio
import csv
import json
import math
import os
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg
from fridge import Dataset, Sample, connect
from fridge.core.client import Client


def validate_target(api_url, dsn, container, acknowledged=False):
    """Require explicit targets; the acknowledgment never overrides known live targets."""
    api, db = urlsplit(api_url), urlsplit(dsn)
    for target in (api, db):
        host = unquote(target.hostname or '').lower().rstrip('.')
        if 'freedomridge-db' in host.split('.') or target.port == 8000:
            raise ValueError("Known live host/port is forbidden, even with acknowledgment")
    # The live database container publishes PostgreSQL's default port on the host.
    if db.port == 5432:
        raise ValueError("Port 5432 is the live database's published port; use a throwaway port")
    if container.lower().strip().split('.')[0] == 'freedomridge-db':
        raise ValueError("freedomridge-db is always forbidden")
    if (
        api.scheme not in ('http', 'https')
        or db.scheme not in ('postgres', 'postgresql')
        or not api.hostname
        or not db.hostname
        or not api.port
        or not db.port
        or not container.strip()
        or api.username
        or api.query
        or api.fragment
        or db.query
        or db.fragment
    ):
        raise ValueError("Supply explicit API and PostgreSQL URLs with ports and a container name; no URL overrides")
    if not acknowledged:
        raise ValueError("Pass --i-know-this-is-not-live after verifying both targets are disposable")


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def evaluate(samples, batches, burst_ends, *, p95_limit, recovery_seconds, near_zero, growth_ratio):
    """Fail closed on missing evidence. Disk compares aggregate bytes/new row in thirds."""
    latencies = [b['latency_s'] for b in batches]
    p95 = percentile(latencies, 0.95)
    failures = []
    if p95 is None or p95 >= p95_limit:
        failures.append('ingest p95 exceeds limit or no batches were measured')
    if not batches or any(b['error'] for b in batches):
        failures.append('ingest/retry errors')
    if not samples or any(s.get('error') or not s.get('health_ok') for s in samples):
        failures.append('health or telemetry failed')
    if any(s.get('api_errors') for s in samples):
        failures.append('UI read errors')
    if any(s.get('failed_jobs', 0) for s in samples):
        failures.append('failed jobs')
    for end in burst_ends:
        recovered = any(
            end <= s['elapsed_s'] <= end + recovery_seconds
            and s.get('queues')
            and all(q['count'] <= near_zero for q in s['queues'].values())
            and not s.get('error')
            for s in samples
        )
        if not recovered:
            failures.append(f'backlog did not recover within {recovery_seconds:g}s after burst at {end:g}s')
    intervals = []
    for before, after in zip(samples, samples[1:]):
        added = after.get('inserted', 0) - before.get('inserted', 0)
        if added > 0 and before.get('data_bytes') is not None and after.get('data_bytes') is not None:
            intervals.append((max(0, after['data_bytes'] - before['data_bytes']), added))
    early = late = None
    if len(intervals) < 6:
        failures.append('insufficient ingest windows for disk growth comparison (need six)')
    else:
        width = len(intervals) // 3
        early = sum(n for n, _ in intervals[:width]) / sum(n for _, n in intervals[:width])
        late = sum(n for n, _ in intervals[-width:]) / sum(n for _, n in intervals[-width:])
        # Page allocation can give a zero early slope; do not silently pass it.
        if early <= 0 or late > early * growth_ratio:
            failures.append('late disk bytes/sample exceeds growth ratio or early slope is zero')
    return {
        'passed': not failures,
        'failures': failures,
        'batch_p50_s': percentile(latencies, 0.5),
        'batch_p95_s': p95,
        'batch_max_s': max(latencies, default=None),
        'early_bytes_per_sample': early,
        'late_bytes_per_sample': late,
    }


def synthetic_batch(start, count, rng, clip_field, dino_field, prefix='soak/'):
    def vector(dim):
        values = [rng.gauss(0, 1) for _ in range(dim)]
        norm = math.sqrt(sum(v * v for v in values))
        return [round(v / norm, 7) for v in values]

    return [
        Sample(
            filepath=f'{prefix}{i:012d}.jpg',
            tags=['soak', 'day' if i % 2 else 'night'],
            metadata={
                'segment': i % 16,
                'confidence': round(rng.random(), 4),
                'reviewed': i % 5 == 0,
                'captured_at': datetime.now(timezone.utc).isoformat(),
                'machine': 'cam-01',
            },
            location=(-122.42 + rng.random() * 0.01, 37.77 + rng.random() * 0.01),  # San Francisco
            embeddings={clip_field: vector(512), dino_field: vector(1024)},
            # Adjacent pairs deliberately share their perceptual hash.
            hashes={'phash_8': f'{(i // 2 * 11400714819323198485) % (1 << 64):016x}'},
        ).to_dict()
        for i in range(start, start + count)
    ]


def client_for(args):
    client = Client(base_url=args.api_url.rstrip('/'), timeout=(5, args.request_timeout))
    # Never follow redirects to an unchecked target, including localhost:8000.
    client.session.max_redirects = 0
    client.session.busy_retry_seconds = args.request_timeout
    return client


class Telemetry:
    def __init__(self, conn):
        self.conn = conn
        self.queues = {}
        self.wal_start = None
        self.wal_previous = None

    async def discover(self):
        rows = await self.conn.fetch(
            """SELECT table_name, column_name, data_type FROM information_schema.columns
               WHERE table_schema='public' ORDER BY table_name, ordinal_position""",
        )
        tables = {}
        for row in rows:
            tables.setdefault(row['table_name'], {})[row['column_name']] = row['data_type']
        for table, columns in tables.items():
            if table == 'dataset_stats_pending' or (
                ('concept' in table or 'dup' in table) and ('pending' in table or 'queue' in table)
            ):
                timestamp = next(
                    (
                        c
                        for c in ('requested_at', 'queued_at', 'created_at', 'updated_at')
                        if c in columns and columns[c].startswith('timestamp')
                    ),
                    None,
                )
                self.queues[table] = timestamp
        self.wal_start = await self.conn.fetchval('SELECT pg_current_wal_lsn()::text')
        self.wal_previous = self.wal_start

    async def sample(self):
        queues = {}
        for table, timestamp in self.queues.items():
            quoted = '"' + table.replace('"', '""') + '"'
            age = f'EXTRACT(EPOCH FROM now() - min("{timestamp}"))' if timestamp else 'NULL'
            row = await self.conn.fetchrow(f'SELECT count(*) AS count, {age} AS oldest_s FROM {quoted}')
            queues[table] = {
                'count': row['count'],
                'oldest_s': float(row['oldest_s']) if row['oldest_s'] is not None else None,
            }
        relations = await self.conn.fetch(
            """SELECT c.oid::regclass::text AS name, pg_table_size(c.oid) AS table_bytes,
                      pg_indexes_size(c.oid) AS index_bytes, pg_total_relation_size(c.oid) AS total_bytes
               FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relname=ANY($1::text[])""",
            ['samples', 'embeddings', *self.queues],
        )
        hnsw = await self.conn.fetch(
            """SELECT i.relname AS name, pg_relation_size(i.oid) AS bytes
               FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid
               JOIN pg_class t ON t.oid=x.indrelid JOIN pg_am a ON a.oid=i.relam
               JOIN pg_namespace n ON n.oid=t.relnamespace
               WHERE n.nspname='public' AND t.relname IN ('samples','embeddings') AND a.amname='hnsw'""",
        )
        jobs = await self.conn.fetch(
            """SELECT kind, state, count(*) AS count FROM jobs
               WHERE state IN ('queued','running','failed') GROUP BY kind,state""",
        )
        vacuum = await self.conn.fetch(
            """SELECT relname, n_dead_tup, last_autovacuum::text FROM pg_stat_user_tables
               WHERE schemaname='public' AND relname IN ('samples','embeddings')""",
        )
        wal = await self.conn.fetchval('SELECT pg_current_wal_lsn()::text')
        delta = await self.conn.fetchval(
            'SELECT pg_wal_lsn_diff($1::text::pg_lsn,$2::text::pg_lsn)',
            wal,
            self.wal_previous,
        )
        total = await self.conn.fetchval(
            'SELECT pg_wal_lsn_diff($1::text::pg_lsn,$2::text::pg_lsn)',
            wal,
            self.wal_start,
        )
        self.wal_previous = wal
        return {
            'queues': queues,
            'jobs': [dict(r) for r in jobs],
            'failed_jobs': sum(r['count'] for r in jobs if r['state'] == 'failed'),
            'database_bytes': await self.conn.fetchval('SELECT pg_database_size(current_database())'),
            'data_bytes': sum(r['total_bytes'] for r in relations if r['name'] in ('samples', 'embeddings')),
            'relations': [dict(r) for r in relations],
            'hnsw': [dict(r) for r in hnsw],
            'wal_lsn': wal,
            'wal_delta_bytes': int(delta),
            'wal_total_bytes': int(total),
            'vacuum': [dict(r) for r in vacuum],
        }


def api_sample(client, dataset_id):
    results = {'api_errors': [], 'health_ok': False}
    reads = {
        'health': lambda: client.session.get(f'{client.base_url}/health'),
        'page': lambda: client.query_samples(dataset_id, limit=25, slim=True, skip_count=True),
        'filtered_count': lambda: client.query_samples(
            dataset_id,
            limit=1,
            filters={'field': 'tags', 'operator': 'contains', 'value': 'day'},
        ),
    }
    for name, call in reads.items():
        started = time.monotonic()
        try:
            result = call()
            if name == 'health':
                result.raise_for_status()
                results['health_ok'] = result.json().get('status') == 'ok'
        except Exception as exc:
            results['api_errors'].append(f'{name}: {exc}')
        results[f'{name}_latency_s'] = time.monotonic() - started
    return results


def write_row(writer, handle, row):
    writer.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()})
    handle.flush()


async def run(args):
    conn = await asyncpg.connect(args.dsn, timeout=10, command_timeout=args.request_timeout)
    await conn.execute("SET default_transaction_read_only=on")
    client = connect(args.api_url)
    client.session.close()
    client.session = client_for(args).session
    reads_client = client_for(args)
    dataset_id = None
    samples, batches = [], []
    models = {}
    state = {'inserted': 0, 'retry_batches': 0, 'projection_count': 0}
    name = f'soak-ingest-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}'
    prefix = 'soak/'
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    fatal = ''
    cleanup = 'not requested'
    telemetry = Telemetry(conn)
    burst_ends = (
        [min(t + args.burst_seconds, args.duration) for t in range(0, math.ceil(args.duration), args.burst_every)]
        if args.burst_seconds
        else []
    )
    # Always evaluate the final drain, even without periodic bursts.
    burst_ends = sorted(set([*burst_ends, args.duration]))
    try:
        await telemetry.discover()
        if args.dataset_id is not None:
            # A restored copy's dataset, e.g. a large dataset at full size; --cleanup is refused for it.
            dataset_id = args.dataset_id
            # Paths are unique to this run, so a later run into the same dataset isn't skipped as duplicates.
            prefix = f'soak/{name}/'
            name = (await asyncio.to_thread(client.get_dataset_by_id, dataset_id))['name']
        else:
            dataset_id = (
                await asyncio.to_thread(
                    client.create_dataset,
                    name,
                    description='Disposable continuous-ingest soak',
                    is_global=False,
                )
            )['id']
        # Protect against accidentally monitoring a different database than the API uses.
        actual = await conn.fetchval('SELECT name FROM datasets WHERE id=$1', dataset_id)
        if actual != name:
            raise RuntimeError('API and telemetry database do not match')
        dataset = await asyncio.to_thread(Dataset.from_id, dataset_id)
        if args.model_dir:
            import joblib

            for field in (args.clip_field, args.dino_field):
                path = args.model_dir / f'{field}.joblib'
                if path.is_file():
                    models[field] = joblib.load(path)
        started = time.monotonic()
        with (
            (args.output / 'batches.csv').open('w') as batch_file,
            (args.output / 'samples.csv').open('w') as sample_file,
        ):
            batch_writer = csv.DictWriter(
                batch_file,
                fieldnames=['elapsed_s', 'retry', 'size', 'inserted', 'latency_s', 'error'],
            )
            batch_writer.writeheader()
            sample_writer = None

            async def ingest():
                rng = random.Random(args.seed)
                sequence, batch_number = 0, 0
                due = 0.0
                while time.monotonic() - started < args.duration:
                    await asyncio.sleep(max(0, started + due - time.monotonic()))
                    elapsed = time.monotonic() - started
                    if elapsed >= args.duration:
                        break
                    payload = await asyncio.to_thread(
                        synthetic_batch,
                        sequence,
                        args.batch_size,
                        rng,
                        args.clip_field,
                        args.dino_field,
                        prefix,
                    )
                    sequence += len(payload)
                    batch_number += 1
                    attempts = [False, True] if args.retry_every and batch_number % args.retry_every == 0 else [False]
                    for retry in attempts:
                        begin = time.monotonic()
                        inserted, error = 0, ''
                        try:
                            result = await asyncio.to_thread(
                                client.add_samples,
                                dataset_id,
                                payload,
                                on_duplicate='skip',
                            )
                            inserted = len(payload) - result.get('skipped', 0)
                            if retry and inserted:
                                raise RuntimeError('retry inserted rows: skip contract violated')
                            state['inserted'] += inserted
                        except Exception as exc:
                            error = str(exc)
                        row = {
                            'elapsed_s': time.monotonic() - started,
                            'retry': retry,
                            'size': len(payload),
                            'inserted': inserted,
                            'latency_s': time.monotonic() - begin,
                            'error': error,
                        }
                        batches.append(row)
                        write_row(batch_writer, batch_file, row)
                        state['retry_batches'] += int(retry)
                    for field, model in models.items():
                        state['projection_count'] += await asyncio.to_thread(
                            dataset.project_pending,
                            field,
                            model,
                            batch=args.batch_size,
                        )
                    multiplier = args.burst_multiplier if elapsed % args.burst_every < args.burst_seconds else 1
                    # No unbounded catch-up burst if the API falls behind.
                    due = max(due + args.batch_size / (args.rate * multiplier), time.monotonic() - started)

            async def monitor():
                nonlocal sample_writer
                deadline = args.duration + args.drain_seconds
                while True:
                    row = {
                        'elapsed_s': time.monotonic() - started,
                        'inserted': state['inserted'],
                        'error': '',
                        'queues': {},
                        'jobs': [],
                        'failed_jobs': 0,
                        'database_bytes': None,
                        'data_bytes': None,
                        'relations': [],
                        'hnsw': [],
                        'wal_lsn': '',
                        'wal_delta_bytes': None,
                        'wal_total_bytes': None,
                        'vacuum': [],
                    }
                    try:
                        row.update(await telemetry.sample())
                    except Exception as exc:
                        row['error'] = str(exc)
                    row.update(await asyncio.to_thread(api_sample, reads_client, dataset_id))
                    # Keep the counter and storage observation close; monitoring is independent of HTTP writes.
                    row['inserted'] = state['inserted']
                    row['elapsed_s'] = time.monotonic() - started
                    samples.append(row)
                    if sample_writer is None:
                        sample_writer = csv.DictWriter(sample_file, fieldnames=list(row))
                        sample_writer.writeheader()
                    write_row(sample_writer, sample_file, row)
                    print(f"{row['elapsed_s']:.0f}s inserted={state['inserted']} queues={row['queues']}", flush=True)
                    if row['elapsed_s'] >= deadline:
                        break
                    await asyncio.sleep(min(args.sample_seconds, deadline - row['elapsed_s']))

            results = await asyncio.gather(ingest(), monitor(), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise RuntimeError(str(result))
    except Exception as exc:
        fatal = str(exc)
    finally:
        if args.cleanup and dataset_id is not None:
            try:
                await asyncio.to_thread(client.delete_dataset, dataset_id)
                cleanup = f'deleted own dataset {dataset_id}'
            except Exception as exc:
                cleanup = f'FAILED: {exc}'
                fatal += f' cleanup failed: {exc}'
        await conn.close()
        client.session.close()
        reads_client.session.close()
    verdict = evaluate(
        samples,
        batches,
        burst_ends,
        p95_limit=args.p95_seconds,
        recovery_seconds=args.recovery_minutes * 60,
        near_zero=args.near_zero,
        growth_ratio=args.growth_ratio,
    )
    if fatal:
        verdict['passed'] = False
        verdict['failures'].append(fatal)
    summary = [
        f"# Soak ingest: {'PASS' if verdict['passed'] else 'FAIL'}",
        '',
        f"Dataset: `{name}` (id {dataset_id}); {cleanup}.",
        f"Observed wall time: {time.monotonic() - started:.1f}s; unique inserted: {state['inserted']}; "
        f"retry batches: {state['retry_batches']}; errors: {sum(bool(b['error']) for b in batches)}.",
        f"Load: {args.rate:g} samples/s, batch {args.batch_size}, burst {args.burst_multiplier:g}x for "
        f"{args.burst_seconds:g}s every {args.burst_every}s; ingest {args.duration:g}s, drain {args.drain_seconds:g}s.",
        f"Limits: p95 < {args.p95_seconds:g}s; queues <= {args.near_zero} within {args.recovery_minutes:g}min; "
        f"late/early disk slope <= {args.growth_ratio:g}.",
        f"Projection: {state['projection_count']} rows; "
        f"{'loaded fields ' + json.dumps(list(models)) if models else 'skipped (no saved model found)'}.",
        f"Detected queues: {json.dumps(telemetry.queues)}. Missing concept/duplicate queues are not certified.",
        "Database sizes, WAL, queues and job failures are global to the isolated stack; other workloads affect them.",
        "Disk comparison uses samples + embeddings including their indexes/TOAST; database/WAL totals are separate.",
        "Synthetic results on a small database do not certify a restored full-size copy. "
        "Backups and maintenance workers must be configured separately; absent work is not exercised.",
        '',
        '```json',
        json.dumps(verdict, indent=2),
        '```',
    ]
    if samples:
        summary += ['', 'Final telemetry:', '```json', json.dumps(samples[-1], indent=2), '```']
    (args.output / 'summary.md').write_text('\n'.join(summary) + '\n')
    print(f"{'PASS' if verdict['passed'] else 'FAIL'}: {args.output / 'summary.md'}", flush=True)
    return 0 if verdict['passed'] else 1


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--api-url', required=True, help='Explicit API URL including /api and port')
    p.add_argument('--dsn', default=os.getenv('DATABASE_URL'), help='Telemetry DSN; defaults only to DATABASE_URL')
    p.add_argument('--container', required=True, help='Disposable DB container identity (not used to execute Docker)')
    p.add_argument('--i-know-this-is-not-live', action='store_true')
    p.add_argument('--output', type=Path, required=True, help='New output directory')
    p.add_argument('--cleanup', action='store_true', help='Delete only the dataset created by this invocation')
    p.add_argument(
        '--dataset-id',
        type=int,
        help='Existing dataset of a restored copy to ingest into',
    )
    p.add_argument('--rate', type=float, default=0.7, help='Base samples/second')
    p.add_argument('--batch-size', type=int, default=50)
    p.add_argument('--duration', type=float, default=3600, help='Ingest seconds, excluding final drain')
    p.add_argument('--burst-every', type=int, default=300, help='Burst period in seconds')
    p.add_argument('--burst-seconds', type=float, default=60)
    p.add_argument('--burst-multiplier', type=float, default=5)
    p.add_argument('--drain-seconds', type=float, default=300)
    p.add_argument('--sample-seconds', type=float, default=15)
    p.add_argument('--retry-every', type=int, default=10, help='Replay every Nth batch; 0 disables')
    p.add_argument('--p95-seconds', type=float, default=10)
    p.add_argument('--recovery-minutes', type=float, default=5)
    p.add_argument('--near-zero', type=int, default=2)
    p.add_argument('--growth-ratio', type=float, default=1.5)
    p.add_argument('--request-timeout', type=float, default=30)
    p.add_argument('--seed', type=int, default=7)
    p.add_argument('--clip-field', default='soak_clip_512')
    p.add_argument('--dino-field', default='soak_dino_1024')
    p.add_argument(
        '--model-dir',
        type=Path,
        help='Trusted saved models named <embedding_field>.joblib; absent files skipped',
    )
    return p


def main():
    p = parser()
    args = p.parse_args()
    try:
        validate_target(args.api_url, args.dsn or '', args.container, args.i_know_this_is_not_live)
        for name in (
            'rate',
            'batch_size',
            'duration',
            'burst_every',
            'burst_multiplier',
            'drain_seconds',
            'sample_seconds',
            'p95_seconds',
            'recovery_minutes',
            'growth_ratio',
            'request_timeout',
        ):
            value = getattr(args, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if not 0 <= args.burst_seconds < args.burst_every or args.retry_every < 0 or args.near_zero < 0:
            raise ValueError('Invalid burst length, retry interval or near-zero limit')
        if args.dataset_id is not None and args.cleanup:
            raise ValueError('--cleanup deletes the whole dataset; it is refused with --dataset-id')
        if args.drain_seconds < args.recovery_minutes * 60:
            raise ValueError('Drain must cover the recovery deadline')
    except ValueError as exc:
        p.error(str(exc))
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
