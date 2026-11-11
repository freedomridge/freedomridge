"""Migration target selection must be explicit and fail before any database access."""

import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

from backend.tests import test_backup_restore_postgres

restore_databases = test_backup_restore_postgres.restore_databases
ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / 'scripts/apply_migrations.sh'
requires_postgres = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires disposable PostgreSQL',
)


def runner_tree(tmp_path, migrations):
    """A copy of the runner and the checkout's migrations, plus `migrations` (name -> SQL), so a test
    can give it work without touching the checkout. schema.sql records every real migration, so the
    copy needs them too, or the runner would see a database newer than its code. Test files use 9xxx
    numbers so they never collide with a real migration."""
    tree = tmp_path / 'tree'
    (tree / 'scripts').mkdir(parents=True)
    for name in ('apply_migrations.sh', 'psql_url.py', 'db_test_safety.py'):
        shutil.copy(ROOT / 'scripts' / name, tree / 'scripts' / name)
    (tree / 'backend/db/migrations').mkdir(parents=True)
    shutil.copy(ROOT / 'backend/db/functions.sql', tree / 'backend/db/functions.sql')
    shutil.copy(ROOT / 'backend/db/database_settings.sql', tree / 'backend/db/database_settings.sql')
    for real in (ROOT / 'backend/db/migrations').glob('*.sql'):
        shutil.copy(real, tree / 'backend/db/migrations' / real.name)
    for name, sql in migrations.items():
        (tree / 'backend/db/migrations' / name).write_text(sql)
    return tree


@pytest.mark.parametrize(
    'marker',
    ['FRIDGE_TEST_MODE', 'PYTEST_CURRENT_TEST', 'FRIDGE_TEST_POSTGRES_CONTAINER', 'FRIDGE_TEST_POSTGRES_DSN'],
)
@pytest.mark.parametrize('container', ['', 'freedomridge-db', '/freedomridge-db', 'fr-drill', 'fr-drill2'])
def test_test_mode_rejects_unsafe_container_before_any_client_call(tmp_path, marker, container):
    called = tmp_path / 'called'
    for name in ('docker', 'psql'):
        stub = tmp_path / name
        stub.write_text(f'#!/bin/sh\necho called >> "{called}"\nexit 99\n')
        stub.chmod(0o755)
    env = {**os.environ, 'PATH': str(tmp_path), 'FRIDGE_DB_CONTAINER': container}
    for key in (
        'FRIDGE_TEST_MODE',
        'PYTEST_CURRENT_TEST',
        'FRIDGE_TEST_POSTGRES_CONTAINER',
        'FRIDGE_TEST_POSTGRES_DSN',
    ):
        env.pop(key, None)
    env[marker] = 'disposable-test-db'
    result = subprocess.run(['/bin/bash', str(RUNNER)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert not called.exists(), result.stdout + result.stderr
    assert 'Unsafe test database target' in result.stderr


@requires_postgres
def test_restore_fixture_pins_container_and_user(monkeypatch, request):
    monkeypatch.delenv('FRIDGE_DB_CONTAINER', raising=False)
    monkeypatch.delenv('FRIDGE_DB_USER', raising=False)
    _, _, container, user = request.getfixturevalue('restore_databases')
    assert os.environ['FRIDGE_DB_CONTAINER'] == container
    assert os.environ['FRIDGE_DB_USER'] == user


@pytest.mark.parametrize('container', ['', 'freedomridge-db', '/freedomridge-db', 'fr-drill', 'fr-drill2'])
def test_restore_fixture_rejects_protected_targets_before_docker(tmp_path, monkeypatch, container):
    monkeypatch.setenv('FRIDGE_TEST_POSTGRES_CONTAINER', container)

    def unexpected_call(*args, **kwargs):
        raise AssertionError('The fixture must reject the target before any subprocess call')

    monkeypatch.setattr(subprocess, 'run', unexpected_call)
    fixture = inspect.unwrap(restore_databases)(tmp_path, monkeypatch)
    with pytest.raises(pytest.fail.Exception, match='Unsafe test database target'):
        next(fixture)


@pytest.mark.parametrize('query_password', ['', '&password=query%2Bsecret%3Dvalue'])
def test_psql_password_is_environment_only_and_other_uri_options_are_preserved(tmp_path, query_password):
    capture = tmp_path / 'connection.json'
    stub = tmp_path / 'psql'
    stub.write_text(
        f'#!{sys.executable}\nimport json, os, pathlib, sys\n'
        f'pathlib.Path({str(capture)!r}).write_text(json.dumps([sys.argv[1:], os.environ["PGPASSWORD"]]))\n',
    )
    stub.chmod(0o755)
    options = 'sslmode=require&application_name=literal+plus'
    url = f'postgresql://user:encoded%40secret%3Avalue@host:5432/db?{options}{query_password}'
    subprocess.run(
        [sys.executable, str(ROOT / 'scripts/psql_url.py'), '-X', '-Atc', 'SELECT 1'],
        env={**os.environ, 'DATABASE_URL': url, 'PATH': str(tmp_path)},
        check=True,
    )
    args, password = json.loads(capture.read_text())
    assert args == [f'--dbname=postgresql://user@host:5432/db?{options}', '-X', '-Atc', 'SELECT 1']
    assert password == ('query+secret=value' if query_password else 'encoded@secret:value')


@pytest.mark.parametrize(
    'args',
    [
        ['--database-url'],
        ['--database-url', ''],
        ['--database-url='],
        ['--database-url', '--typo'],
        ['--database-url', 'not-a-url'],
        ['--database-url=postgres://'],
        ['--database-url=postgres://host/db\nbad'],
        ['--database-url=postgres://host:wrong/db'],
        ['--database-url=postgres://user:bad%GG@host/db'],
        ['--database-url', 'postgres://host/db', 'extra'],
        ['--database-url=postgres://host/db', '--database-url=postgres://other/db'],
        ['--typo'],
        ['unexpected'],
        ['--database-url-env', 'unexpected'],
    ],
)
def test_bad_arguments_never_resolve_a_container_or_call_psql(tmp_path, args):
    called = tmp_path / 'called'
    for name in ('docker', 'psql'):
        stub = tmp_path / name
        stub.write_text(f'#!/bin/sh\necho called >> "{called}"\nexit 99\n')
        stub.chmod(0o755)
    result = subprocess.run(
        ['/bin/bash', str(RUNNER), *args],
        env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH']},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not called.exists(), result.stdout + result.stderr
    assert 'Usage:' in result.stderr


def test_missing_psql_fails_before_any_database_work(tmp_path):
    result = subprocess.run(
        ['/bin/bash', str(RUNNER), '--database-url', os.environ['DATABASE_URL']],
        env={**os.environ, 'PATH': str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert 'psql client' in result.stderr
    assert 'Applying' not in result.stdout


@requires_postgres
@pytest.mark.parametrize('spelling', ['split', 'equals'])
def test_url_argument_overrides_environment_and_container_target(tmp_path, restore_databases, spelling):
    (ambient, intended), sql, _, _ = restore_databases
    for name in (ambient, intended):
        sql(name, (ROOT / 'backend/db/schema.sql').read_text())
    tree = runner_tree(tmp_path, {'9001_marker.sql': 'CREATE TABLE ci_marker (id INTEGER);\n'})
    parsed = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])
    url = parsed._replace(path=f'/{intended}').geturl()
    args = ['--database-url', url] if spelling == 'split' else [f'--database-url={url}']
    result = subprocess.run(
        ['bash', str(tree / 'scripts/apply_migrations.sh'), *args],
        cwd=tree,
        env={
            **os.environ,
            'DATABASE_URL': parsed._replace(path=f'/{ambient}').geturl(),
            'FRIDGE_DB_NAME': ambient,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sql(ambient, "SELECT to_regclass('ci_marker')") == ''
    assert sql(intended, "SELECT count(*) FROM schema_migrations WHERE filename='9001_marker.sql'") == '1'
    assert sql(intended, "SELECT to_regclass('ci_marker')") == 'ci_marker'
    calls = (Path(os.environ['PATH'].split(os.pathsep)[0]) / 'psql-argv.log').read_text()
    assert url not in calls
    password = parsed.password
    assert not password or password not in calls


@requires_postgres
def test_container_mode_does_not_inherit_client_pgoptions(tmp_path, restore_databases):
    (target, _), sql, _, _ = restore_databases
    sql(target, (ROOT / 'backend/db/schema.sql').read_text())
    # Would fail under the client's search_path (nowhere to create it) and statement_timeout (1 ms).
    tree = runner_tree(tmp_path, {'9001_marker.sql': 'CREATE TABLE ci_marker (id INTEGER);\nSELECT pg_sleep(0.05);\n'})
    result = subprocess.run(
        ['bash', str(tree / 'scripts/apply_migrations.sh')],
        cwd=tree,
        env={
            **os.environ,
            'FRIDGE_DB_NAME': target,
            'PGOPTIONS': '-c search_path=nowhere -c statement_timeout=1',
            'PGCONNECT_TIMEOUT': 'not-an-integer',
            'PGAPPNAME': 'must-not-leak',
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sql(target, "SELECT to_regclass('ci_marker')") == 'ci_marker'


@requires_postgres
def test_runner_refuses_a_newer_database_and_one_not_created_from_schema_sql(tmp_path, restore_databases):
    (target, _), sql, _, _ = restore_databases
    sql(target, (ROOT / 'backend/db/schema.sql').read_text())
    tree = runner_tree(tmp_path, {'9001_marker.sql': 'CREATE TABLE ci_marker (id INTEGER);\n'})

    def run():
        return subprocess.run(
            ['bash', str(tree / 'scripts/apply_migrations.sh')],
            cwd=tree,
            env={**os.environ, 'FRIDGE_DB_NAME': target},
            capture_output=True,
            text=True,
        )

    sql(target, "INSERT INTO schema_migrations (filename) VALUES ('9002_newer.sql')")
    result = run()
    assert result.returncode != 0 and 'does not have: 9002_newer.sql' in result.stderr, result.stdout + result.stderr
    assert sql(target, "SELECT to_regclass('ci_marker')") == ''

    sql(target, 'DELETE FROM schema_migrations')
    result = run()
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'not created from backend/db/schema.sql' in result.stderr
    assert sql(target, "SELECT to_regclass('ci_marker')") == ''
