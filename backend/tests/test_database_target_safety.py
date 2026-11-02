"""No target mismatch may reach test setup, migrations, or backups."""

import importlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.tests import test_backup_local, test_backup_restore_postgres
from scripts.db_test_safety import PROTECTED_PROJECTS, UnsafeTestDatabase, require_container, require_test_url

ROOT = Path(__file__).resolve().parents[2]
SAFE_ID = 'a' * 64
PROTECTED_ID = 'b' * 64


def container_info(name='/disposable', identity=SAFE_ID):
    return {
        'Name': name,
        'Id': identity,
        'Config': {'Labels': None},
        'HostConfig': {'PortBindings': None},
        'NetworkSettings': {'Ports': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '55494'}]}},
    }


@pytest.mark.parametrize('same_server', [True, False])
def test_container_comparison_resolves_names_and_id_prefixes(monkeypatch, same_server):
    def run(cmd, **kwargs):
        assert cmd[:4] == ['docker', 'inspect', '--type', 'container']
        identity = SAFE_ID if same_server or cmd[-1] == 'disposable' else PROTECTED_ID
        return SimpleNamespace(stdout=json.dumps(container_info(identity=identity)))

    monkeypatch.setattr(subprocess, 'run', run)
    if same_server:
        assert require_container('disposable', SAFE_ID[:12]) == SAFE_ID
    else:
        with pytest.raises(UnsafeTestDatabase, match='must match'):
            require_container('disposable', PROTECTED_ID[:12])


def test_url_guard_requires_test_dsn_but_allows_another_database_on_same_endpoint(monkeypatch):
    monkeypatch.delenv('FRIDGE_TEST_POSTGRES_DSN', raising=False)
    with pytest.raises(UnsafeTestDatabase, match='must match'):
        require_test_url('postgres://test-server:55494/restore')
    monkeypatch.setenv('FRIDGE_TEST_POSTGRES_DSN', 'postgres://user:password@test-server:55494/source')
    require_test_url('postgresql://user:password@test-server:55494/restore?sslmode=disable')


@pytest.mark.parametrize('key', ['PGHOSTADDR', 'PGPORT', 'PGSERVICE', 'PGSERVICEFILE'])
def test_url_guard_rejects_libpq_environment_routing(monkeypatch, key):
    monkeypatch.setenv('FRIDGE_TEST_POSTGRES_DSN', 'postgres://user@test-server:55494/source')
    monkeypatch.setenv(key, 'unexpected')
    with pytest.raises(UnsafeTestDatabase, match='routing environment'):
        require_test_url('postgres://user@test-server:55494/restore')


def docker_stub(tmp_path, name='/freedomridge-db'):
    calls = tmp_path / 'calls.jsonl'
    docker = tmp_path / 'docker'
    docker.write_text(
        f'#!{sys.executable}\nimport json, pathlib, sys\n'
        f'with pathlib.Path({str(calls)!r}).open("a") as log: log.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        f'if sys.argv[1] == "inspect": print({json.dumps(container_info(name, PROTECTED_ID))!r})\n'
        'else: sys.exit(99)\n',
    )
    docker.chmod(0o755)
    psql = tmp_path / 'psql'
    psql.write_text(f'#!/bin/sh\necho \'["psql"]\' >> "{calls}"\nexit 99\n')
    psql.chmod(0o755)
    return calls


@pytest.mark.parametrize(
    'mode',
    [
        'mismatch',
        'match',
        'protected',
        'unavailable',
        'missing-dsn',
        'missing-container',
        'database-url-unset',
        'database-url-other-db',
        'database-url-mismatch',
        'database-url-routing',
        'database-url-invalid',
        'database-url-empty',
        'protected-port',
        'protected-project',
    ],
)
def test_session_validates_identity_before_test_setup(tmp_path, mode):
    child = tmp_path / 'isolated'
    child.mkdir()
    touched = child / 'test-ran'
    events = child / 'identity.jsonl'
    allowed = mode in ('match', 'database-url-unset', 'database-url-other-db')
    info = container_info('/freedomridge-db' if mode == 'protected' else '/disposable')
    if mode == 'protected-port':
        info['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'] = '5432'
    if mode == 'protected-project':
        info['Config']['Labels'] = {'com.docker.compose.project': 'freedomridge'}
    # Copy the real session hook into an isolated pytest root, replacing only I/O.
    stub = f'''
import asyncpg, subprocess, json
from pathlib import Path
from types import SimpleNamespace
_events = Path({str(events)!r})
def _record(value):
    with _events.open('a') as log: log.write(json.dumps(value) + '\\n')
def _run(cmd, **kwargs):
    _record(cmd)
    assert cmd[:4] == ['docker', 'inspect', '--type', 'container'] or 'pg_control_system()' in ' '.join(cmd)
    if {mode!r} == 'unavailable': raise OSError('unavailable password secret')
    return SimpleNamespace(stdout={json.dumps(info)!r} if cmd[1] == 'inspect' else '111\\n', returncode=0)
class _Connection:
    async def fetchval(self, sql):
        _record(sql)
        assert sql == 'SELECT system_identifier FROM pg_control_system()'
        return 111 if {allowed!r} else 222
    async def close(self): pass
async def _connect(dsn, **kwargs):
    _record(kwargs)
    assert kwargs['server_settings']['default_transaction_read_only'] == 'on'
    return _Connection()
subprocess.run = _run
asyncpg.connect = _connect
'''
    (child / 'conftest.py').write_text((ROOT / 'backend/tests/conftest.py').read_text() + stub)
    # Check at collection time: backend modules must capture the verified DSN,
    # including when DATABASE_URL was absent or selected another database/user.
    (child / 'test_probe.py').write_text(
        'import os\nfrom backend import database, init_db, job_service\n'
        'assert database.DATABASE_URL == init_db.DATABASE_URL == os.environ["FRIDGE_TEST_POSTGRES_DSN"]\n'
        'assert job_service.DATABASE_URL == database.DATABASE_URL\n'
        f'def test_probe():\n    open({str(touched)!r}, "w").close()\n',
    )
    dsn = 'postgres://user:password@server:55494/db'
    env = {
        **os.environ,
        'FRIDGE_TEST_POSTGRES_CONTAINER': '' if mode == 'missing-container' else 'disposable',
        'FRIDGE_DB_CONTAINER': 'disposable',
        'FRIDGE_TEST_POSTGRES_DSN': '' if mode == 'missing-dsn' else dsn,
        'DATABASE_URL': dsn,
        'PYTHONPATH': str(ROOT) + os.pathsep + os.environ.get('PYTHONPATH', ''),
    }
    if mode == 'database-url-unset':
        env.pop('DATABASE_URL')
    elif mode == 'database-url-other-db':
        env['DATABASE_URL'] = 'postgresql://other:secret@server:55494/other'
    elif mode == 'database-url-mismatch':
        env['DATABASE_URL'] = 'postgres://user:secret@localhost:5432/live'
    elif mode == 'database-url-routing':
        env['DATABASE_URL'] = dsn + '?host=localhost&port=5432'
    elif mode == 'database-url-invalid':
        env['DATABASE_URL'] = 'postgres://user:secret@server:bad/db'
    elif mode == 'database-url-empty':
        env['DATABASE_URL'] = ''
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--confcutdir', str(child), str(child)],
        env=env,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == (0 if allowed else 2), output
    assert touched.exists() == allowed
    assert 'secret' not in output and 'password' not in output
    if mode == 'mismatch':
        assert 'different PostgreSQL servers' in output
    if mode == 'mismatch' or allowed:
        assert 'pg_control_system()' in events.read_text()
    else:
        assert 'Unsafe test database target' in output
        assert not events.exists() or 'pg_control_system()' not in events.read_text()


@pytest.mark.parametrize('spelling', ['split', 'equals', 'env'])
@pytest.mark.parametrize(
    'url',
    [
        'postgres://user@other-server:55494/db',
        'postgres://user@test-server:5432/db',
        'postgres://user@test-server:55494/db?host=other-server',
        'postgres://user@test-server:55494/db?hostaddr=127.0.0.1',
        'postgres://user@test-server:55494/db?port=5432',
        'postgres://user@test-server:55494/db?service=live',
    ],
)
def test_url_mode_rejects_other_endpoint_before_any_client(tmp_path, spelling, url):
    calls = docker_stub(tmp_path)
    args = {'split': ['--database-url', url], 'equals': [f'--database-url={url}'], 'env': ['--database-url-env']}[
        spelling
    ]
    result = subprocess.run(
        ['/bin/bash', str(ROOT / 'scripts/apply_migrations.sh'), *args],
        env={
            **os.environ,
            'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
            'FRIDGE_PYTHON': sys.executable,
            'FRIDGE_TEST_MODE': '1',
            'FRIDGE_TEST_POSTGRES_DSN': 'postgres://user@test-server:55494/source',
            'DATABASE_URL': url,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not calls.exists(), calls.read_text() if calls.exists() else ''
    assert 'Unsafe test database target' in result.stderr


@pytest.mark.parametrize('target', [PROTECTED_ID, PROTECTED_ID[:12]])
def test_migration_rejects_protected_container_id_before_exec(tmp_path, target):
    calls = docker_stub(tmp_path)
    result = subprocess.run(
        ['/bin/bash', str(ROOT / 'scripts/apply_migrations.sh')],
        env={
            **os.environ,
            'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
            'FRIDGE_PYTHON': sys.executable,
            'FRIDGE_DB_CONTAINER': target,
            'FRIDGE_TEST_POSTGRES_CONTAINER': target,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert 'Unsafe test database target' in result.stderr
    assert all(json.loads(line)[0] == 'inspect' for line in calls.read_text().splitlines())


@pytest.mark.parametrize('fixture_name', ['restore', 'scratch'])
@pytest.mark.parametrize('target', ['fr-drill', PROTECTED_ID, PROTECTED_ID[:12]])
def test_fixtures_reject_protected_names_and_ids_before_exec(tmp_path, monkeypatch, fixture_name, target):
    monkeypatch.setenv('FRIDGE_TEST_POSTGRES_CONTAINER', target)
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[:4] == ['docker', 'inspect', '--type', 'container'], 'fixture reached docker exec/run'
        return SimpleNamespace(stdout=json.dumps(container_info('/fr-drill', PROTECTED_ID)))

    monkeypatch.setattr(subprocess, 'run', run)
    if fixture_name == 'restore':
        fixture = inspect.unwrap(test_backup_restore_postgres.restore_databases)(tmp_path, monkeypatch)
    else:
        fixture = inspect.unwrap(test_backup_local.scratch_databases)()
    try:
        with pytest.raises(pytest.fail.Exception, match='Unsafe test database target'):
            next(fixture)
    finally:
        fixture.close()
    assert all(cmd[1] == 'inspect' for cmd in calls)


@pytest.mark.parametrize('target', [None, 'freedomridge-db', PROTECTED_ID[:12]])
def test_backup_refuses_live_default_and_ids_before_dump(tmp_path, monkeypatch, capsys, target):
    monkeypatch.delenv('FRIDGE_DB_CONTAINER', raising=False)
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[:4] == ['docker', 'inspect', '--type', 'container'], 'backup reached a database command'
        return SimpleNamespace(stdout=json.dumps(container_info('/freedomridge-db', PROTECTED_ID)))

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(test_backup_local.backup_local, 'run', lambda *args: pytest.fail('backup started'))
    args = ['--dir', str(tmp_path)] + ([] if target is None else ['--container', target])
    assert test_backup_local.backup_local.main(args) == 1
    assert 'Unsafe test database target' in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('target', ['renamed-db', SAFE_ID, SAFE_ID[:12]])
@pytest.mark.parametrize('project', sorted(PROTECTED_PROJECTS))
def test_container_rejects_checkout_compose_labels(monkeypatch, target, project):
    info = container_info('/renamed-db')
    info['Config']['Labels'] = {'com.docker.compose.project': project}
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[:4] == ['docker', 'inspect', '--type', 'container']
        return SimpleNamespace(stdout=json.dumps(info))

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(UnsafeTestDatabase, match='protected container'):
        require_container(target)
    assert len(calls) == 1


@pytest.mark.parametrize('section, key', [('HostConfig', 'PortBindings'), ('NetworkSettings', 'Ports')])
@pytest.mark.parametrize('host_ip', ['0.0.0.0', '127.0.0.1', '::', '::1', ''])
def test_container_rejects_any_binding_of_host_port_5432(monkeypatch, section, key, host_ip):
    info = container_info('/renamed-db')
    info[section][key] = {
        '9999/tcp': [{'HostIp': host_ip, 'HostPort': '55494'}, {'HostIp': host_ip, 'HostPort': '5432'}],
    }

    def run(cmd, **kwargs):
        assert cmd[:4] == ['docker', 'inspect', '--type', 'container']
        return SimpleNamespace(stdout=json.dumps(info))

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(UnsafeTestDatabase, match='protected container'):
        require_container(SAFE_ID[:12])


@pytest.mark.parametrize('published', [True, False])
def test_container_allows_disposable_metadata(monkeypatch, published):
    info = container_info()
    info['Config']['Labels'] = {'com.docker.compose.project': 'isolated-ci'}
    if not published:
        info['NetworkSettings']['Ports'] = {'5432/tcp': None}
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(info)))
    assert require_container('disposable') == SAFE_ID


@pytest.mark.parametrize('output', ['', 'not json', '{}', 'null', '[]', '{"Name": "/disposable", "Id": null}'])
def test_container_inspection_fails_closed_on_missing_metadata(monkeypatch, output):
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=output))
    with pytest.raises(UnsafeTestDatabase, match='inspection failed'):
        require_container('disposable')


@pytest.mark.parametrize('host', ['localhost', '127.0.0.1', '[::1]', 'LOCALHOST.', '%6cocalhost'])
@pytest.mark.parametrize('port', [8000, 3000])
@pytest.mark.parametrize('override', [None, '0', 'true', '1'])
def test_sdk_guard_requires_explicit_live_override(monkeypatch, host, port, override):
    from fridge._test_safety import require_test_url as require_sdk_url

    url = f'http://{host}:{port}/api'
    monkeypatch.setenv('FRIDGE_TEST_URL', url)
    monkeypatch.delenv('FRIDGE_ALLOW_LIVE_SDK_TESTS', raising=False)
    if override is not None:
        monkeypatch.setenv('FRIDGE_ALLOW_LIVE_SDK_TESTS', override)
    if override == '1':
        assert require_sdk_url() == url  # Parse only: never contact these ports.
    else:
        with pytest.raises(SystemExit, match='FRIDGE_ALLOW_LIVE_SDK_TESTS=1'):
            require_sdk_url()


@pytest.mark.parametrize('url', [None, '', 'not-a-url', 'ftp://server/api', 'http://user:secret@server:bad/api'])
def test_sdk_guard_rejects_missing_or_invalid_url_even_with_override(monkeypatch, url):
    from fridge._test_safety import require_test_url as require_sdk_url

    monkeypatch.delenv('FRIDGE_TEST_URL', raising=False)
    monkeypatch.setenv('FRIDGE_ALLOW_LIVE_SDK_TESTS', '1')
    if url is not None:
        monkeypatch.setenv('FRIDGE_TEST_URL', url)
    with pytest.raises(SystemExit, match='Unsafe SDK test target') as error:
        require_sdk_url()
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('url', ['http://127.0.0.1:8001/api', 'http://127.0.0.1:8094/api', 'https://test-server/api'])
def test_sdk_guard_accepts_ci_and_explicit_test_urls(monkeypatch, url):
    from fridge._test_safety import require_test_url as require_sdk_url

    monkeypatch.setenv('FRIDGE_TEST_URL', url)
    monkeypatch.delenv('FRIDGE_ALLOW_LIVE_SDK_TESTS', raising=False)
    assert require_sdk_url() == url


@pytest.mark.parametrize(
    'module',
    ['test_sdk', 'test_integrity', 'test_map_contract', 'test_objects', 'test_synth_integration'],
)
@pytest.mark.parametrize(
    'url',
    [None, 'http://localhost:8000/api', 'http://[::1]:3000/api', 'http://127.0.0.1:8001/api'],
)
def test_every_sdk_suite_checks_target_before_connect(monkeypatch, module, url):
    suite = importlib.import_module(f'fridge.{module}')
    monkeypatch.delenv('FRIDGE_TEST_URL', raising=False)
    monkeypatch.delenv('FRIDGE_ALLOW_LIVE_SDK_TESTS', raising=False)
    if url is not None:
        monkeypatch.setenv('FRIDGE_TEST_URL', url)
    calls = []

    def connect(target):
        calls.append(target)
        raise SystemExit('stub connection')

    monkeypatch.setattr(suite, 'connect', connect)
    allowed = url == 'http://127.0.0.1:8001/api'
    with pytest.raises(SystemExit, match='stub connection' if allowed else 'Unsafe SDK test target'):
        suite.run_tests()
    assert calls == ([url] if allowed else [])
