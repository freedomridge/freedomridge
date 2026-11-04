"""scripts/backup_local.py: a dump counts as a backup only after it is read back and proven complete.

The read-back and retention checks run anywhere. The end-to-end cases need a disposable Postgres
container (FRIDGE_TEST_POSTGRES_CONTAINER, as in CI) and create their own throwaway databases in it.
"""

import fcntl
import gzip
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from scripts.db_test_safety import UnsafeTestDatabase, require_container

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('scripts.backup_local', ROOT / 'scripts' / 'backup_local.py')
assert spec is not None and spec.loader is not None
backup_local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_local)

GZIP_READ = ['gzip', '-dc']
DUMP = """--
-- PostgreSQL database dump
--
\\restrict TOKEN1
SET statement_timeout = 0;
COPY public.items (id, note) FROM stdin;
1\tone\\nline
2\ttwo
\\.
COPY public."Mixed Case" (x) FROM stdin;
\\.
COPY other.things (y) FROM stdin;
7
\\.
--
-- PostgreSQL database dump complete
--
\\unrestrict TOKEN1
"""
COUNTS = {'public.items': 2, 'public."Mixed Case"': 0, 'other.things': 1}


def write_dump(path, text):
    with gzip.open(path, 'wt') as handle:
        handle.write(text)
    return path


def test_read_back_accepts_a_complete_dump(tmp_path):
    path = write_dump(tmp_path / 'ok.sql.gz', DUMP)
    assert backup_local.verify(path, COUNTS, GZIP_READ) == COUNTS


def test_read_back_accepts_the_older_completion_comment(tmp_path):
    text = DUMP.replace('\\restrict TOKEN1\n', '').replace('\\unrestrict TOKEN1\n', '')
    path = write_dump(tmp_path / 'old.sql.gz', text)
    assert backup_local.verify(path, COUNTS, GZIP_READ) == COUNTS


@pytest.mark.parametrize(
    'change, message',
    [
        (lambda text: text.replace('\\unrestrict TOKEN1\n', ''), 'incomplete'),
        (lambda text: text.replace('\\unrestrict TOKEN1', '\\unrestrict OTHER'), 'incomplete'),
        (
            lambda text: (
                text.replace('\\restrict TOKEN1\n', '')
                .replace('\\unrestrict TOKEN1\n', '')
                .replace('-- PostgreSQL database dump complete\n', '')
            ),
            'incomplete',
        ),
        (lambda text: text.replace('2\ttwo\n', ''), 'row counts differ'),
        (lambda text: text.replace('COPY other.things (y) FROM stdin;\n7\n\\.\n', ''), 'missing from the dump'),
    ],
)
def test_read_back_rejects_incomplete_or_wrong_dumps(tmp_path, change, message):
    path = write_dump(tmp_path / 'bad.sql.gz', change(DUMP))
    with pytest.raises(backup_local.BackupError, match=message):
        backup_local.verify(path, COUNTS, GZIP_READ)


def test_read_back_rejects_a_truncated_gzip_stream(tmp_path):
    path = write_dump(tmp_path / 'whole.sql.gz', DUMP * 200)
    truncated = tmp_path / 'truncated.sql.gz'
    truncated.write_bytes(path.read_bytes()[:-40])
    with pytest.raises(backup_local.BackupError):
        backup_local.verify(truncated, COUNTS, GZIP_READ)


def test_read_back_handles_data_that_looks_like_dump_syntax_and_unusual_names(tmp_path):
    text = DUMP.replace(
        '2\ttwo\n',
        '2\ttwo\nCOPY this line is data\n-- PostgreSQL database dump complete\n',
    ).replace(
        '--\n-- PostgreSQL database dump complete',
        'COPY public.nocols  FROM stdin;\n\n\\.\nCOPY public."odd (name)" (x) FROM stdin;\n1\n\\.\n'
        '--\n-- PostgreSQL database dump complete',
    )
    counts = {**COUNTS, 'public.items': 4, 'public.nocols': 1, 'public."odd (name)"': 1}
    path = write_dump(tmp_path / 'edge.sql.gz', text)
    assert backup_local.verify(path, counts, GZIP_READ) == counts


def make_backup(folder, stamp, verified=True, size=1):
    dump = folder / f'freedomridge_{stamp}.sql.gz'
    dump.write_bytes(b'x')
    manifest = {'verified': verified, 'size_bytes': size}
    (folder / f'{dump.name}.manifest.json').write_text(json.dumps(manifest))
    return dump


def test_retention_keeps_the_newest_verified_dumps_and_touches_nothing_else(tmp_path):
    old = [make_backup(tmp_path, f'2026101{day}T020000Z') for day in range(1, 5)]
    unverified = make_backup(tmp_path, '20261009T020000Z', verified=False)
    without_manifest = tmp_path / 'freedomridge_20261001T020000Z.sql.gz'
    other = tmp_path / 'other_20261016T164042Z.sql.gz'
    for path in (without_manifest, other):
        path.write_bytes(b'x')

    backup_local.prune(tmp_path, 'freedomridge', keep=2)

    assert [path.exists() for path in old] == [False, False, True, True]
    assert not (tmp_path / f'{old[0].name}.manifest.json').exists()
    assert unverified.exists() and without_manifest.exists() and other.exists()


def test_retention_keeps_the_largest_dump_so_an_emptied_database_cant_push_it_out(tmp_path):
    full = make_backup(tmp_path, '20261011T020000Z', size=31_000_000_000)
    emptied = [make_backup(tmp_path, f'2026101{day}T020000Z', size=600) for day in range(2, 6)]
    one_off = tmp_path / 'freedomridge_1off-before-migration.sql.gz'
    one_off.write_bytes(b'x')
    (tmp_path / f'{one_off.name}.manifest.json').write_text(json.dumps({'verified': True, 'size_bytes': 1}))

    backup_local.prune(tmp_path, 'freedomridge', keep=2)

    assert full.exists()
    assert [path.exists() for path in emptied] == [False, False, True, True]
    assert one_off.exists()


def test_retention_never_deletes_the_dump_it_just_made(tmp_path):
    make_backup(tmp_path, '20991019T020000Z')  # from a night with a wrong clock
    current = make_backup(tmp_path, '20261019T020000Z')
    backup_local.prune(tmp_path, 'freedomridge', keep=1, current=current)
    assert current.exists()


def test_a_missing_backup_folder_is_not_created(tmp_path):
    folder = tmp_path / 'unmounted-drive' / 'backups'
    # A container that can't exist, so a regression here can never reach a real database.
    assert backup_local.main(['--dir', str(folder), '--container', f'fridge-no-such-{uuid4().hex}']) == 1
    assert not folder.exists()


requires_container = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires an explicit disposable PostgreSQL DSN and its Docker container name',
)


@pytest.fixture
def scratch_databases():
    try:
        container = require_container(os.environ['FRIDGE_TEST_POSTGRES_CONTAINER'])
    except UnsafeTestDatabase as error:
        pytest.fail(str(error))
    user = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN']).username or 'freedomridge'
    names = []

    def psql(database, sql):
        return subprocess.run(
            [
                'docker',
                'exec',
                '-i',
                container,
                'psql',
                '-X',
                '-q',
                '-At',
                '-v',
                'ON_ERROR_STOP=1',
                '-U',
                user,
                '-d',
                database,
            ],
            input=sql,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def create():
        name = f'_bklocal_{uuid4().hex[:12]}'
        psql('postgres', f'CREATE DATABASE {name};')
        names.append(name)
        return name

    yield container, user, psql, create
    for name in names:
        psql('postgres', f'DROP DATABASE IF EXISTS {name} WITH (FORCE);')


SEED = r"""
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE items(id serial PRIMARY KEY, note text);
INSERT INTO items(note)
    SELECT 'row ' || g || E'\twith a tab\nand a newline \\ and a backslash' FROM generate_series(1, 2000) g;
CREATE TABLE "Mixed Case"(x int);
INSERT INTO "Mixed Case" SELECT generate_series(1, 7);
CREATE TABLE empty_one(x int);
CREATE TABLE schema_migrations(filename text PRIMARY KEY);
INSERT INTO schema_migrations VALUES ('0000_baseline.sql'), ('0001_b.sql'), ('0002_c.sql');
CREATE TABLE parent_rows(x int);
CREATE TABLE child_rows() INHERITS (parent_rows);
INSERT INTO child_rows VALUES (1), (2);
CREATE SCHEMA other;
CREATE TABLE other.things(y int);
INSERT INTO other.things SELECT generate_series(1, 3);
"""
EXPECTED = {
    'other.things': 3,
    'public."Mixed Case"': 7,
    'public.empty_one': 0,
    'public.child_rows': 2,
    'public.items': 2000,
    'public.parent_rows': 0,
    'public.schema_migrations': 3,
}


@requires_container
def test_backup_is_verified_and_restores(tmp_path, scratch_databases):
    container, user, psql, create = scratch_databases
    source, target = create(), create()
    psql(source, SEED)

    args = ['--dir', str(tmp_path), '--container', container, '--user', user, '--db', source]
    assert backup_local.main(args) == 0

    [dump] = tmp_path.glob('freedomridge_*.sql.gz')
    manifest = json.loads((tmp_path / f'{dump.name}.manifest.json').read_text())
    assert manifest['verified'] is True and manifest['row_counts'] == EXPECTED
    assert manifest['schema_version'] == '0002_c.sql' and manifest['migrations_applied'] == 3
    assert not list(tmp_path.glob('*.partial')) and not list(tmp_path.glob('*.failed.json'))

    restored = subprocess.run(
        f'gzip -dc {dump} | docker exec -i {container} psql -X -q -v ON_ERROR_STOP=1 -U {user} -d {target}',
        shell=True,
        capture_output=True,
        text=True,
    )
    assert restored.returncode == 0, restored.stderr
    counts = psql(
        target,
        'SELECT count(*) FROM items; SELECT count(*) FROM "Mixed Case"; SELECT note FROM items WHERE id = 1;',
    )
    assert counts.split('\n')[:2] == ['2000', '7']
    assert 'with a tab\nand a newline \\ and a backslash' in counts


@requires_container
def test_failed_backup_leaves_a_record_and_keeps_earlier_backups(tmp_path, scratch_databases):
    container, user, psql, create = scratch_databases
    earlier = make_backup(tmp_path, '20261001T020000Z')

    missing = f'_bklocal_missing_{uuid4().hex[:8]}'
    args = ['--dir', str(tmp_path), '--container', container, '--user', user, '--db', missing]
    assert backup_local.main(args) == 1

    [record] = tmp_path.glob('freedomridge_*.failed.json')
    assert json.loads(record.read_text())['verified'] is False
    assert list(tmp_path.glob('freedomridge_*.sql.gz')) == [earlier]
    assert not list(tmp_path.glob('*.partial'))


def run_backup(tmp_path, container, user, database, *extra):
    return backup_local.main(
        ['--dir', str(tmp_path), '--container', container, '--user', user, '--db', database, *extra],
    )


def failure(tmp_path):
    [record] = tmp_path.glob('freedomridge_*.failed.json')
    assert not list(tmp_path.glob('freedomridge_*.sql.gz')) and not list(tmp_path.glob('*.partial'))
    return json.loads(record.read_text())['error']


@requires_container
@pytest.mark.parametrize(
    'extra_flag, message',
    [('--exclude-table-data=public.items', 'missing from the dump'), ('--no-such-option', 'pg_dump exited')],
)
def test_a_dump_that_fails_or_misses_rows_is_not_kept(tmp_path, scratch_databases, monkeypatch, extra_flag, message):
    container, user, psql, create = scratch_databases
    source = create()
    psql(source, SEED)
    flags = backup_local.dump_flags()
    monkeypatch.setattr(backup_local, 'dump_flags', lambda: [*flags, extra_flag])
    assert run_backup(tmp_path, container, user, source) == 1
    assert message in failure(tmp_path)


@requires_container
def test_a_failing_compressor_is_not_kept(tmp_path, scratch_databases, monkeypatch):
    container, user, psql, create = scratch_databases
    source = create()
    psql(source, SEED)
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'pigz').write_text('#!/bin/sh\ncat > /dev/null\nexit 1\n')
    (fake / 'pigz').chmod(0o755)
    monkeypatch.setenv('PATH', f'{fake}:{os.environ["PATH"]}')
    output = tmp_path / 'out'
    output.mkdir()
    assert run_backup(output, container, user, source) == 1
    assert 'pigz exited 1' in failure(output)


@requires_container
def test_rows_written_after_the_snapshot_are_left_out_of_dump_and_counts(
    tmp_path,
    scratch_databases,
    monkeypatch,
):
    container, user, psql, create = scratch_databases
    source = create()
    psql(source, SEED)
    query = backup_local.SnapshotSession.query

    def write_after_export(session, sql):
        rows = query(session, sql)
        if 'pg_export_snapshot' in sql:
            psql(source, "INSERT INTO items(note) SELECT 'late' FROM generate_series(1, 500);")
        return rows

    monkeypatch.setattr(backup_local.SnapshotSession, 'query', write_after_export)
    assert run_backup(tmp_path, container, user, source) == 0
    [manifest] = tmp_path.glob('*.manifest.json')
    assert json.loads(manifest.read_text())['row_counts']['public.items'] == 2000
    assert psql(source, 'SELECT count(*) FROM items;').strip() == '2500'


@requires_container
def test_a_run_skips_while_another_holds_the_lock(tmp_path, scratch_databases):
    container, user, psql, create = scratch_databases
    source = create()
    with open(tmp_path / '.freedomridge-backup.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_backup(tmp_path, container, user, source) == 1
    assert not list(tmp_path.glob('freedomridge_*'))


@requires_container
def test_too_little_free_space_stops_before_dumping(tmp_path, scratch_databases, monkeypatch):
    container, user, psql, create = scratch_databases
    source = create()
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(backup_local.shutil, 'disk_usage', lambda _: usage(1, 1, 0))
    assert run_backup(tmp_path, container, user, source) == 1
    assert 'free' in failure(tmp_path)


@requires_container
def test_only_stale_partial_dumps_are_removed(tmp_path, scratch_databases):
    container, user, psql, create = scratch_databases
    source = create()
    stale = tmp_path / 'freedomridge_20261001T020000Z.sql.gz.partial'
    fresh = tmp_path / 'freedomridge_20261002T020000Z.sql.gz.partial'
    for path in (stale, fresh):
        path.write_bytes(b'x')
    seven_hours_ago = time.time() - 7 * 3600
    os.utime(stale, (seven_hours_ago, seven_hours_ago))
    assert run_backup(tmp_path, container, user, source) == 0
    assert not stale.exists() and fresh.exists()


@requires_container
def test_a_stopped_run_ends_its_pg_dump_and_keeps_nothing(tmp_path, scratch_databases):
    container, user, psql, create = scratch_databases
    source = create()
    psql(source, 'CREATE TABLE big AS SELECT g AS id, md5(g::text) AS h FROM generate_series(1, 6000000) g;')
    sessions = (
        "SELECT count(*) FROM pg_stat_activity "
        f"WHERE datname = '{source}' AND application_name LIKE 'fridge-backup-%' AND query LIKE 'COPY%';"
    )
    process = subprocess.Popen(
        [sys.executable, str(ROOT / 'scripts' / 'backup_local.py'), '--dir', str(tmp_path)]
        + ['--container', container, '--user', user, '--db', source, '--threads', '1'],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while psql('postgres', sessions).strip() == '0':
            assert process.poll() is None and time.monotonic() < deadline, 'the dump never started'
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 1, output
    assert 'stopped by SIGTERM' in failure(tmp_path)
    # Left alone, pg_dump would keep dumping the 6M-row table for seconds after its client was killed.
    deadline = time.monotonic() + 1.5
    while psql('postgres', sessions).strip() != '0':
        assert time.monotonic() < deadline, 'pg_dump kept running in the container'
        time.sleep(0.1)


@requires_container
def test_a_stop_during_a_long_count_ends_pg_dump_at_once(tmp_path, scratch_databases, monkeypatch):
    # The job runner kills the script 8 s after SIGTERM. A count of a big table still running in the snapshot
    # session must not hold up stopping pg_dump, or pg_dump outlives the script and finishes an orphan dump.
    container, user, psql, create = scratch_databases
    source = create()
    psql(source, 'CREATE TABLE big AS SELECT g AS id, md5(g::text) AS h FROM generate_series(1, 6000000) g;')
    copying = (
        "SELECT count(*) FROM pg_stat_activity "
        f"WHERE datname = '{source}' AND application_name LIKE 'fridge-backup-%' AND query LIKE 'COPY%';"
    )
    query = backup_local.SnapshotSession.query

    def slow_counts(session, sql):
        return query(session, sql.replace('SELECT count(*) FROM ONLY', 'SELECT count(*) FROM pg_sleep(60), ONLY'))

    monkeypatch.setattr(backup_local.SnapshotSession, 'query', slow_counts)
    stopped = []

    def stop_once_dumping():
        deadline = time.monotonic() + 60
        while psql('postgres', copying).strip() == '0':
            if time.monotonic() > deadline:
                return
            time.sleep(0.05)
        stopped.append(time.monotonic())
        os.kill(os.getpid(), signal.SIGTERM)  # main() handles it while this test runs main() in-process

    watcher = threading.Thread(target=stop_once_dumping, daemon=True)
    watcher.start()
    assert run_backup(tmp_path, container, user, source, '--threads', '1') == 1
    watcher.join()
    assert stopped, 'the dump never started'
    assert time.monotonic() - stopped[0] < 8
    assert 'stopped by SIGTERM' in failure(tmp_path)
    deadline = time.monotonic() + 1.5
    while psql('postgres', copying).strip() != '0':
        assert time.monotonic() < deadline, 'pg_dump kept running in the container'
        time.sleep(0.1)


@requires_container
def test_an_alter_table_queued_before_pg_dump_starts_cannot_hang_the_backup(tmp_path, scratch_databases, monkeypatch):
    container, user, psql, create = scratch_databases
    source = create()
    psql(source, SEED)
    monkeypatch.setattr(backup_local, 'LOCK_WAIT_SECONDS', 2)
    waiting = (
        "SELECT count(*) FROM pg_stat_activity "
        f"WHERE datname = '{source}' AND wait_event_type = 'Lock' AND query LIKE 'ALTER TABLE%';"
    )
    alter = []
    query = backup_local.SnapshotSession.query

    def queue_alter_after_the_session_locks(session, sql):
        rows = query(session, sql)
        if sql.startswith('LOCK TABLE'):
            alter.append(
                subprocess.Popen(
                    ['docker', 'exec', container, 'psql', '-X', '-q', '-U', user, '-d', source]
                    + ['-c', 'ALTER TABLE items ADD COLUMN later int'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ),
            )
            deadline = time.monotonic() + 30
            while psql('postgres', waiting).strip() == '0':
                assert time.monotonic() < deadline, 'the ALTER never queued'
                time.sleep(0.05)
        return rows

    monkeypatch.setattr(backup_local.SnapshotSession, 'query', queue_alter_after_the_session_locks)
    stop = (
        'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
        f"WHERE datname = '{source}' AND application_name LIKE 'fridge-backup-%';"
    )
    watchdog = threading.Timer(60, lambda: psql('postgres', stop))  # so a regression fails instead of hanging
    watchdog.start()
    started = time.monotonic()
    try:
        assert run_backup(tmp_path, container, user, source) == 1
    finally:
        watchdog.cancel()
    assert time.monotonic() - started < 30
    # pg_dump enforces --lock-wait-timeout as a statement timeout on its LOCK TABLE.
    assert 'timeout' in (error := failure(tmp_path)) and 'LOCK TABLE' in error
    assert alter[0].wait(timeout=30) == 0
