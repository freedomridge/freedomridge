#!/usr/bin/env python3
"""Back up a local Docker install's database and prove the dump is complete before keeping it.

Meant for cron on the machine that runs the database container, e.g. nightly at 02:00:

    0 2 * * * /usr/bin/python3 <repo>/scripts/backup_local.py --dir /backups >> /backups/backup.log 2>&1

The dump comes from `pg_dump` inside the container, so the client always matches the server and no
password is needed. It is compressed on the host with pigz (gzip if pigz is missing).

A dump counts as a backup only after a full read-back proves:
- pg_dump and the compressor exited 0, and the whole gzip stream, read back from disk, decompresses;
- the dump is complete: its closing `\\unrestrict` token matches the opening `\\restrict` token
  (pg_dump writes the close last; older pg_dump versions end with a "dump complete" comment instead);
- every table's row count in the dump equals the database's, counted in the exact snapshot the dump
  read (`pg_dump --snapshot`), so writes during the dump can't cause a false mismatch.

A verified dump gets `<file>.manifest.json` next to it. A failed or interrupted run stops its pg_dump,
deletes its partial dump, writes `<prefix>_<ts>.failed.json` with the reason, and exits 1.

Retention keeps the newest --keep verified dumps, plus the largest one, so a database that was
emptied by mistake can't push the last full backup out. It only deletes dumps that have this
script's manifest, and partial dumps older than 6 hours left by a run that died.

Standard library only, so the system python3 can run it from cron.
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .db_test_safety import UnsafeTestDatabase, require_container, test_mode
else:
    from db_test_safety import UnsafeTestDatabase, require_container, test_mode

REPO = Path(__file__).resolve().parent.parent
STALE_PARTIAL_SECONDS = 6 * 3600
# The snapshot session locks every table before pg_dump starts. An ALTER TABLE queued in between waits
# for the session, pg_dump's lock waits behind the ALTER, and the session waits for pg_dump: without a
# limit, all three would wait forever (and the app's queries on that table queue behind the ALTER).
LOCK_WAIT_SECONDS = 30

TABLES_SQL = """
SELECT format('%I.%I', n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%'
  -- Extension tables (e.g. PostGIS spatial_ref_sys) are recreated by CREATE EXTENSION; pg_dump copies
  -- at most their user-added rows, so their counts can't be compared.
  AND NOT EXISTS (SELECT 1 FROM pg_depend d
                  WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
ORDER BY 1"""

# Reads the decompressed dump once: the completion tokens and the number of rows in each COPY block.
# Inside a COPY block every line is data (COPY's text format escapes backslashes, so data never looks
# like `\.` or `\restrict`). The table name runs to the first space outside double quotes.
VERIFY_AWK = r"""
!copying && /^\\restrict / && restrict == "" { restrict = $2 }
!copying && /^\\unrestrict / { unrestrict = $2 }
!copying && /^-- PostgreSQL database dump complete$/ { complete = 1 }
!copying && /^COPY / {
    name = substr($0, 6)
    quoted = 0
    for (i = 1; i <= length(name); i++) {
        ch = substr(name, i, 1)
        if (ch == "\"") quoted = !quoted
        else if (ch == " " && !quoted) break
    }
    table = substr(name, 1, i - 1)
    copying = 1
    rows = 0
    next
}
copying && /^\\\.$/ { print "count\t" table "\t" rows; copying = 0; next }
copying { rows++ }
END {
    print "restrict\t" restrict
    print "unrestrict\t" unrestrict
    print "complete\t" complete + 0
}
"""


class BackupError(Exception):
    pass


def log(message):
    print(f'{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z} {message}', flush=True)


def dump_flags():
    """The sidecar's flags are the single definition (see the --clean rationale in backup_loop.sh)."""
    match = re.search(r'^PGDUMP_FLAGS="(.*)"$', (REPO / 'scripts' / 'backup_loop.sh').read_text(), re.M)
    if not match:
        raise BackupError('could not read PGDUMP_FLAGS from scripts/backup_loop.sh')
    return match.group(1).split()


def dump_name_pattern(prefix):
    return re.compile(rf'{re.escape(prefix)}_\d{{8}}T\d{{6}}Z\.sql\.gz')


def read_manifest(path):
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def fsync_path(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SnapshotSession:
    """A psql session inside the container holding a REPEATABLE READ snapshot for the dump to share."""

    def __init__(self, container, user, database, application):
        self.process = subprocess.Popen(
            ['docker', 'exec', '-i', '-e', f'PGAPPNAME={application}', container]
            + ['psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-U', user, '-d', database],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.queries = 0

    def query(self, sql):
        self.queries += 1
        marker = f'__backup_local_done_{self.queries}__'
        self.process.stdin.write(f"{sql};\nSELECT '{marker}';\n")
        self.process.stdin.flush()
        rows = []
        for line in self.process.stdout:
            line = line.rstrip('\n')
            if line == marker:
                return rows
            rows.append(line)
        raise BackupError('database session ended: ' + self.process.stderr.read().strip()[-500:])

    def close(self):
        try:
            self.process.stdin.write('COMMIT;\n\\q\n')
        except (OSError, ValueError):  # psql already exited, or stdin already closed
            pass
        # Close even after a failed write, or the pipe is only closed (noisily) when collected.
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()


def stop_backends(args, application):
    """End our pg_dump and snapshot session in the container.

    Killing a `docker exec` client stops pg_dump only at its next write; one stalled on a lock or a slow
    query keeps its locks and snapshot until then.
    """
    try:
        subprocess.run(
            ['docker', 'exec', args.container, 'psql', '-X', '-q', '-At', '-U', args.user, '-d', args.db, '-c']
            + [f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = '{application}'"],
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        log(f'could not stop the database sessions named {application}')


def verify(path, expected_counts, decompress):
    """Read the whole dump back; return its per-table row counts or raise BackupError."""
    reader = subprocess.Popen([*decompress, str(path)], stdout=subprocess.PIPE)
    awk = subprocess.run(
        ['awk', VERIFY_AWK],
        stdin=reader.stdout,
        capture_output=True,
        text=True,
        env={**os.environ, 'LC_ALL': 'C'},
    )
    reader.stdout.close()
    if reader.wait() != 0:
        raise BackupError(f'the gzip stream does not decompress cleanly (exit {reader.returncode})')
    if awk.returncode != 0:
        raise BackupError(f'reading the dump failed: {awk.stderr.strip()[-300:]}')
    fields, dumped = {}, {}
    for line in awk.stdout.splitlines():
        parts = line.split('\t')
        if parts[0] == 'count':
            dumped[parts[1]] = int(parts[2])
        else:
            fields[parts[0]] = parts[1] if len(parts) > 1 else ''
    if fields.get('restrict'):
        if fields['restrict'] != fields.get('unrestrict'):
            raise BackupError('the dump is incomplete: its closing \\unrestrict token is missing or differs')
    elif fields.get('complete') != '1':
        raise BackupError('the dump is incomplete: no completion marker at the end')
    missing = sorted(set(expected_counts) - set(dumped))
    if missing:
        raise BackupError(f'{len(missing)} table(s) missing from the dump: {", ".join(missing[:10])}')
    wrong = [
        f'{table} {dumped[table]} != {count}'
        for table, count in sorted(expected_counts.items())
        if dumped[table] != count
    ]
    if wrong:
        raise BackupError(f'row counts differ from the database for {len(wrong)} table(s): {"; ".join(wrong[:10])}')
    return dumped


def verified_dumps(folder, prefix):
    """(dump, manifest path, manifest) for each dump this script made and verified, newest first."""
    pattern = dump_name_pattern(prefix)
    found = []
    for dump in folder.iterdir():
        if pattern.fullmatch(dump.name):
            manifest_path = folder / f'{dump.name}.manifest.json'
            manifest = read_manifest(manifest_path)
            if manifest is not None and manifest.get('verified') is True:
                found.append((dump, manifest_path, manifest))
    return sorted(found, key=lambda item: item[0].name, reverse=True)


def prune(folder, prefix, keep, current=None):
    """Keep the newest `keep` verified dumps, the largest one, and `current`; delete the rest."""
    dumps = verified_dumps(folder, prefix)
    sizes = [(item[2].get('size_bytes') or 0, item[0].name) for item in dumps]
    largest = max(sizes)[1] if sizes else None
    for dump, manifest_path, _ in dumps[keep:]:
        if dump.name in (largest, getattr(current, 'name', None)):
            continue
        dump.unlink()
        manifest_path.unlink()
        log(f'pruned {dump.name}')


def run(args, folder):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    final = folder / f'{args.prefix}_{stamp}.sql.gz'
    partial = folder / f'{final.name}.partial'
    application = f'fridge-backup-{stamp}'
    compress = ['pigz', '-p', str(args.threads)] if shutil.which('pigz') else ['gzip', '-6']
    decompress = ['pigz', '-dc'] if shutil.which('pigz') else ['gzip', '-dc']
    flags = dump_flags()

    stale = re.compile(rf'{dump_name_pattern(args.prefix).pattern}\.partial')
    for leftover in folder.iterdir():
        if stale.fullmatch(leftover.name) and time.time() - leftover.stat().st_mtime > STALE_PARTIAL_SECONDS:
            leftover.unlink()
            log(f'removed {leftover.name}, left by a run that died')

    previous = verified_dumps(folder, args.prefix)
    started = time.monotonic()
    session = SnapshotSession(args.container, args.user, args.db, application)
    dump = compressor = None
    finished = False
    try:
        session.query(
            'SET statement_timeout = 0; SET idle_in_transaction_session_timeout = 0; '
            f"SET lock_timeout = '{LOCK_WAIT_SECONDS}s'",
        )
        session.query('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        snapshot = session.query('SELECT pg_export_snapshot()')[0]
        server_version, database_bytes = session.query(
            "SELECT current_setting('server_version') || '|' || pg_database_size(current_database())",
        )[0].split('|')
        schema = ''
        if session.query("SELECT to_regclass('public.schema_migrations') IS NOT NULL")[0] == 't':
            schema = session.query(
                # The newest numbered migration; unnumbered legacy files like add_segmentation_config.sql sort last.
                "SELECT coalesce(max(filename) FILTER (WHERE filename ~ '^[0-9]'), '') || '|' || count(*) "
                'FROM public.schema_migrations',
            )[0]
        tables = session.query(TABLES_SQL)
        if tables:
            # pg_dump takes the same locks; taking them now keeps a table from being dropped before it's counted.
            session.query(f'LOCK TABLE {", ".join(tables)} IN ACCESS SHARE MODE')

        estimate = previous[0][2].get('size_bytes', 0) if previous else int(database_bytes) // 3
        free = shutil.disk_usage(folder).free
        if free < estimate * 1.5:
            raise BackupError(
                f'only {free / 1e9:.1f} GB free in {folder}; the dump needs about {estimate / 1e9:.1f} GB',
            )

        log(f'dumping {args.db} from {args.container} to {final}')
        with open(partial, 'wb') as output, tempfile.TemporaryFile() as dump_stderr:
            dump = subprocess.Popen(
                ['docker', 'exec', '-e', f'PGAPPNAME={application}', args.container, 'nice', '-n', '10']
                + ['pg_dump', '-U', args.user, '-d', args.db, f'--snapshot={snapshot}']
                + [f'--lock-wait-timeout={LOCK_WAIT_SECONDS}s', *flags],
                stdout=subprocess.PIPE,
                stderr=dump_stderr,
            )
            compressor = subprocess.Popen(['nice', '-n', '10', *compress], stdin=dump.stdout, stdout=output)
            dump.stdout.close()
            # Count while the dump runs; the session stays open until pg_dump has finished with the snapshot.
            counts = {table: int(session.query(f'SELECT count(*) FROM ONLY {table}')[0]) for table in tables}
            if dump.wait() != 0:
                dump_stderr.seek(0)
                error = dump_stderr.read().decode(errors='replace').strip()
                raise BackupError(f'pg_dump exited {dump.returncode}: {error[-500:]}')
            if compressor.wait() != 0:
                raise BackupError(f'{compress[0]} exited {compressor.returncode}')
        session.close()
        dumped_seconds = time.monotonic() - started

        # Make the dump durable, then drop it from the page cache so the read-back reads the disk.
        descriptor = os.open(partial, os.O_RDONLY)
        try:
            os.fsync(descriptor)
            os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(descriptor)
        size = partial.stat().st_size
        log(f'dumped {size / 1e9:.2f} GB in {dumped_seconds / 60:.1f} min; reading it back')
        verify(partial, counts, decompress)
        partial.rename(final)
        manifest = {
            'file': final.name,
            'verified': True,
            'created_at': stamp,
            'container': args.container,
            'database': args.db,
            'size_bytes': size,
            'dump_seconds': round(dumped_seconds),
            'verify_seconds': round(time.monotonic() - started - dumped_seconds),
            'server_version': server_version,
            'database_bytes': int(database_bytes),
            'schema_version': schema.split('|')[0] if schema else None,
            'migrations_applied': int(schema.split('|')[1]) if schema else None,
            'pg_dump_flags': flags,
            'row_counts': counts,
            'job_id': args.job_id,
        }
        target = folder / f'{final.name}.manifest.json'
        temporary = folder / f'{target.name}.tmp'
        temporary.write_text(json.dumps(manifest, indent=2) + '\n')
        fsync_path(temporary)
        temporary.rename(target)
        fsync_path(folder)
        finished = True
        log(f'verified {final.name}: {len(counts)} tables, row counts exact')
        if previous and size < (previous[0][2].get('size_bytes') or 0) / 2:
            log(f'WARNING: this dump is less than half the size of {previous[0][0].name}; check the database')
        prune(folder, args.prefix, args.keep, current=final)
    finally:
        # Stop the dump before closing the session: closing waits for the session's current query (a count
        # of a big table, or a lock wait), and whoever stopped this run may not wait that long.
        for process in (dump, compressor):
            if process is not None and process.poll() is None:
                process.kill()
        if not finished:
            stop_backends(args, application)  # pg_dump in the container, and a count or lock wait in the session
        session.close()
        if partial.exists():
            partial.unlink()
    return final


STOP_SIGNALS = ('SIGTERM', 'SIGHUP', 'SIGINT')


def stop_on_signal(signum, _frame):
    for name in STOP_SIGNALS:
        signal.signal(getattr(signal, name), signal.SIG_IGN)  # let the cleanup finish
    raise BackupError(f'stopped by {signal.Signals(signum).name}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--dir', required=True, help='existing folder for dumps and manifests')
    parser.add_argument('--container', default=os.getenv('FRIDGE_DB_CONTAINER', 'freedomridge-db'))
    parser.add_argument('--user', default=os.getenv('FRIDGE_DB_USER', 'freedomridge'))
    parser.add_argument('--db', default=os.getenv('FRIDGE_DB_NAME', 'freedomridge'))
    parser.add_argument('--prefix', default='freedomridge', help='dump file name prefix')
    parser.add_argument('--keep', type=int, default=7, help='newest verified dumps to keep (default 7)')
    parser.add_argument('--threads', type=int, default=4, help='pigz threads (default 4)')
    parser.add_argument('--job-id', help="the job runner's backup job, recorded in the manifest")
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error('--keep must be at least 1')

    if test_mode():
        try:
            args.container = require_container(args.container, os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'))
        except UnsafeTestDatabase as error:
            log(f'BACKUP FAILED: {error}')
            return 1

    folder = Path(args.dir)
    if not folder.is_dir():
        # Never create it: an unmounted backup drive would turn into a dump on the parent filesystem.
        log(f'BACKUP FAILED: {folder} does not exist (is the backup drive mounted?)')
        return 1
    with open(folder / f'.{args.prefix}-backup.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log('another backup is still running; skipping this one')
            return 1
        handlers = {name: signal.signal(getattr(signal, name), stop_on_signal) for name in STOP_SIGNALS}
        try:
            run(args, folder)
            return 0
        except Exception as error:  # every failure, including a stop signal, leaves a record and exit 1 for cron
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            record = {'verified': False, 'failed_at': stamp, 'error': f'{type(error).__name__}: {error}'}
            (folder / f'{args.prefix}_{stamp}.failed.json').write_text(json.dumps(record, indent=2) + '\n')
            log(f'BACKUP FAILED: {record["error"]}')
            return 1
        finally:
            for name, handler in handlers.items():
                signal.signal(getattr(signal, name), handler)


if __name__ == '__main__':
    sys.exit(main())
