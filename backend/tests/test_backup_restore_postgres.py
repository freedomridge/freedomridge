"""Exercise the SDK restore and migration handoff against disposable PostgreSQL."""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from fridge import backup

from scripts.db_test_safety import UnsafeTestDatabase, require_container

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires an explicit disposable PostgreSQL DSN and Docker container',
)


@pytest.fixture
def restore_databases(tmp_path, monkeypatch):
    try:
        container = require_container(os.environ['FRIDGE_TEST_POSTGRES_CONTAINER'])
    except UnsafeTestDatabase as error:
        pytest.fail(str(error))
    parsed = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])
    user = parsed.username or 'freedomridge'
    # Pin both CLI modes before any Docker call; container mode must not discover
    # the live Compose service even when the caller supplied only the test DSN.
    monkeypatch.setenv('FRIDGE_DB_CONTAINER', container)
    monkeypatch.setenv('FRIDGE_DB_USER', user)
    names = [f'_restore_{uuid4().hex[:12]}' for _ in range(2)]

    def sql(name, text):
        return subprocess.run(
            ['docker', 'exec', '-i', container, 'psql', '-X', '-U', user, '-d', name, '-v', 'ON_ERROR_STOP=1', '-Atq'],
            input=text,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    # Use a matching client even on hosts without psql, but in the host's network
    # namespace: the DSN may use a published port (e.g. localhost:55449), not the
    # database container's port. Preserve the complete URI and real TCP connection.
    client = f'{container}-psql-{uuid4().hex[:12]}'
    image = subprocess.run(
        ['docker', 'inspect', '--type', 'container', '--format', '{{.Image}}', container],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    wrapper = tmp_path / 'psql'
    wrapper.write_text(
        f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
with pathlib.Path(__file__).with_name("psql-argv.log").open("a") as log:
    log.write(json.dumps(args) + "\\n")
data = None
if "-f" in args:
    i = args.index("-f")
    with open(args[i + 1], "rb") as source:
        data = source.read()
    del args[i:i + 2]
cmd = ["docker", "exec", "-i"]
for key in ("PGPASSWORD", "PGOPTIONS", "PGAPPNAME", "PGCONNECT_TIMEOUT"):
    if key in os.environ:
        cmd += ["-e", key + "=" + os.environ[key]]
cmd += [{client!r}, "psql", "-U", {user!r}] + args
sys.exit(subprocess.run(cmd, input=data).returncode)
""",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ["PATH"]}')
    monkeypatch.setenv('DATABASE_URL', parsed._replace(path=f'/{names[1]}').geturl())
    # The direct migration path must use DATABASE_URL, not this unrelated DB name.
    monkeypatch.setenv('FRIDGE_DB_NAME', '_restore_wrong_target')
    try:
        subprocess.run(
            ['docker', 'run', '-d', '--name', client, '--network', 'host', '--entrypoint', 'sleep', image, 'infinity'],
            capture_output=True,
            check=True,
        )
        for name in names:
            sql('postgres', f'CREATE DATABASE {name}')
        yield names, sql, container, user
    finally:
        try:
            for name in names:
                sql('postgres', f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')
        finally:
            subprocess.run(['docker', 'rm', '-f', client], capture_output=True, check=True)


@pytest.mark.parametrize('gzipped', [False, True])
def test_dump_requires_empty_target_then_restores_and_migrates(tmp_path, restore_databases, gzipped):
    import gzip

    (source, target), sql, container, user = restore_databases
    schema = (ROOT / 'backend/db/schema.sql').read_text()
    sql(source, schema)
    # A drifted function body in the dump: the runner the restore ends with must repair it.
    sql(
        source,
        """INSERT INTO datasets(name, owner_id) VALUES ('restored-dataset', 1);
           CREATE OR REPLACE FUNCTION mark_dataset_stats_stale() RETURNS TRIGGER
           AS $f$ BEGIN RETURN NEW; END; $f$ LANGUAGE plpgsql;""",
    )
    dump = subprocess.run(
        [
            'docker',
            'exec',
            container,
            'pg_dump',
            '-U',
            user,
            '-d',
            source,
            '--clean',
            '--if-exists',
            '--no-owner',
            '--no-acl',
        ],
        capture_output=True,
        check=True,
    ).stdout
    path = tmp_path / ('dump.sql.gz' if gzipped else 'dump.sql')
    path.write_bytes(gzip.compress(dump) if gzipped else dump)
    sql(target, schema)
    with pytest.raises(RuntimeError, match='EMPTY database'):
        backup.restore(str(path))
    assert sql(target, 'SELECT count(*) FROM backup_schedule') == '1'
    assert sql(target, "SELECT count(*) FROM datasets WHERE name = 'restored-dataset'") == '0'

    sql('postgres', f'DROP DATABASE {target};')
    sql('postgres', f'CREATE DATABASE {target};')
    sql(target, 'CREATE EXTENSION postgis; CREATE EXTENSION vector;')
    backup.restore(str(path))
    assert sql(target, "SELECT count(*) FROM datasets WHERE name = 'restored-dataset'") == '1'
    assert sql(target, 'SELECT count(*) FROM backup_schedule') == '1'
    assert sql(target, "SELECT count(*) FROM schema_migrations WHERE filename = '0000_baseline.sql'") == '1'
    assert 'record_stats_work' in sql(target, "SELECT prosrc FROM pg_proc WHERE proname = 'mark_dataset_stats_stale'")
