"""Regression tests for `fridge.backup` — the never-lose-data path.

The helper rejects initialized targets, streams decompressed SQL, stops on SQL errors,
and runs ANALYZE/migrations against the same target before reporting success. Real PostgreSQL
coverage is in test_backup_restore_postgres.py and scripts/test_backup_restore.sh.
"""

import gzip
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fridge import backup

SQL = "-- freedomridge test dump\nSELECT 1;\nSELECT 2;\n"


_STUB = """#!{python}
import pathlib, sys
args = sys.argv[1:]
if "-Atc" in args:
    print({target_state!r})
    sys.exit(0)
if "-c" in args:
    pathlib.Path({maintenance!r}).write_text(args[args.index("-c") + 1])
    sys.exit(0)
pathlib.Path({argv_log!r}).write_text("\\n".join(args))
if "-f" in args:
    data = pathlib.Path(args[args.index("-f") + 1]).read_bytes()
else:
    data = sys.stdin.buffer.read()
pathlib.Path({received!r}).write_bytes(data)
sys.stderr.write({stderr!r})
sys.exit({exit_code})
"""


def _stub_bin(tmp_path, name, *, exit_code=0, stderr="", target_state="f"):
    """Put a stub `name` on PATH that records its argv and whatever it was fed.

    Written in Python rather than shell: the argv/stdin-vs--f distinction is fiddly to get right
    in portable sh, and a subtly-broken stub produces a test that passes for the wrong reason.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    argv_log = tmp_path / f"{name}.argv"
    received = tmp_path / f"{name}.received"
    p = bindir / name
    p.write_text(
        _STUB.format(
            python=sys.executable,
            argv_log=str(argv_log),
            received=str(received),
            stderr=stderr,
            exit_code=exit_code,
            target_state=target_state,
            maintenance=str(tmp_path / "maintenance"),
        ),
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir, argv_log, received


@pytest.fixture(autouse=True)
def stub_migrations(tmp_path, monkeypatch):
    """Record the migration handoff; real PostgreSQL coverage lives below."""
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] == "bash" and cmd[1].endswith("/scripts/apply_migrations.sh"):
            assert cmd[2:] == ["--database-url-env"]
            assert kwargs["env"]["DATABASE_URL"] == os.environ["DATABASE_URL"]
            assert (tmp_path / "maintenance").read_text() == "ANALYZE"
            (tmp_path / "migrated").touch()
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


def test_backup_command_carries_clean_and_if_exists(tmp_path, monkeypatch):
    """pg_dump must be invoked with --clean --if-exists, or its dumps won't restore."""
    bindir, argv_log, _ = _stub_bin(tmp_path, "pg_dump")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    out = tmp_path / "dump.sql"
    out.write_text("")  # backup() stats the file for its size report

    backup.backup(str(out))

    args = argv_log.read_text().split("\n")
    assert "--clean" in args, f"pg_dump lost --clean; its dumps will silently restore nothing: {args}"
    assert "--if-exists" in args, f"pg_dump lost --if-exists: {args}"


@pytest.mark.parametrize("gzipped", [False, True])
def test_restore_feeds_psql_the_decompressed_sql(tmp_path, monkeypatch, gzipped):
    """psql must receive the real SQL for both .sql and .sql.gz inputs."""
    bindir, _, received = _stub_bin(tmp_path, "psql")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    if gzipped:
        src = tmp_path / "dump.sql.gz"
        with gzip.open(src, "wb") as fh:
            fh.write(SQL.encode())
    else:
        src = tmp_path / "dump.sql"
        src.write_text(SQL)

    backup.restore(str(src))

    got = received.read_bytes()
    assert not got.startswith(b"\x1f\x8b"), "psql was fed GZIP bytes — GzipFile was passed straight to subprocess"
    assert got.decode() == SQL
    assert (tmp_path / "migrated").exists()


def test_restore_raises_on_psql_failure(tmp_path, monkeypatch):
    """A failed restore must raise, never print success — this is the never-lose-data path."""
    bindir, _, _ = _stub_bin(tmp_path, "psql", exit_code=3, stderr="ERROR: boom\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    src = tmp_path / "dump.sql.gz"
    with gzip.open(src, "wb") as fh:
        fh.write(SQL.encode())

    with pytest.raises(RuntimeError, match="Restore FAILED"):
        backup.restore(str(src))
    assert not (tmp_path / "migrated").exists()


def test_restore_command_sets_on_error_stop(tmp_path, monkeypatch):
    """Without ON_ERROR_STOP=1, psql exits 0 after every statement failed."""
    bindir, argv_log, _ = _stub_bin(tmp_path, "psql")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    src = tmp_path / "dump.sql.gz"
    with gzip.open(src, "wb") as fh:
        fh.write(SQL.encode())

    backup.restore(str(src))
    assert "ON_ERROR_STOP=1" in argv_log.read_text().split("\n")
    assert "-X" in argv_log.read_text().split("\n")
    assert os.environ["DATABASE_URL"] not in argv_log.read_text()


def test_restore_cli_explains_missing_psql_without_a_traceback(tmp_path):
    src = tmp_path / "dump.sql"
    src.write_text(SQL)
    result = subprocess.run(
        [sys.executable, str(Path(backup.__file__)), "--restore", "--input", str(src)],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "psql client on PATH" in result.stderr
    assert "Traceback" not in result.stderr


def test_restore_cli_flushes_progress_before_migrations_when_redirected(tmp_path):
    bindir, _, _ = _stub_bin(tmp_path, "psql")
    migration = bindir / "bash"
    migration.write_text('#!/bin/sh\necho "Applying database migrations..."\n')
    migration.chmod(0o755)
    src = tmp_path / "dump.sql"
    src.write_text(SQL)
    result = subprocess.run(
        [sys.executable, str(Path(backup.__file__)), "--restore", "--input", str(src)],
        env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.index("Restoring database") < result.stdout.index("Applying database migrations")


def test_restore_refuses_initialized_target_before_loading_dump(tmp_path, monkeypatch):
    bindir, _, received = _stub_bin(tmp_path, "psql", target_state="t")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    src = tmp_path / "dump.sql"
    src.write_text(SQL)

    with pytest.raises(RuntimeError, match="EMPTY database"):
        backup.restore(str(src))
    assert not received.exists()
    assert not (tmp_path / "migrated").exists()


def test_restore_propagates_migration_failure(tmp_path, monkeypatch):
    bindir, _, _ = _stub_bin(tmp_path, "psql")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    src = tmp_path / "dump.sql"
    src.write_text(SQL)
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] == "bash":
            raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        backup.restore(str(src))


def test_sidecar_declares_clean_flags():
    """scripts/backup_loop.sh is the thing that actually runs nightly — guard its flags too."""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "..", "..", "scripts", "backup_loop.sh")
    with open(script) as fh:
        text = fh.read()
    line = next((ln for ln in text.splitlines() if ln.startswith("PGDUMP_FLAGS=")), None)
    assert line is not None, "backup_loop.sh no longer declares PGDUMP_FLAGS (test_backup_restore.sh reads it)"
    assert "--clean" in line and "--if-exists" in line, f"sidecar dumps will not restore: {line}"


@pytest.mark.skipif(not shutil.which("docker"), reason="Requires Docker Compose for configuration validation")
def test_database_compose_config_does_not_trigger_a_recreate():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            str(root / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    db_service = json.loads(result.stdout)["services"]["db"]
    assert db_service["image"] == "freedomridge-db:pg18"
    assert "pull_policy" not in db_service
    assert db_service["build"]["dockerfile"] == "Dockerfile.db"
    assert "stop_grace_period" not in db_service
    base = next(line for line in (root / "Dockerfile.db").read_text().splitlines() if line.startswith("FROM "))
    assert "@sha256:" in base, "the PostgreSQL/pgvector base must not drift on rebuild"


@pytest.mark.parametrize("container", ["fr-cx-restore-fake", "fr-rv-local", "fr-restore-test-local"])
def test_shell_restore_test_accepts_throwaways_and_finishes_cleanup_after_drop_failure(tmp_path, container):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "docker-calls"
    docker = bindir / "docker"
    docker.write_text(
        f'#!{sys.executable}\nimport json, pathlib, sys\n'
        f'with pathlib.Path({str(calls)!r}).open("a") as log:\n'
        '    log.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        'sys.exit(1)\n',
    )
    docker.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "scripts/test_backup_restore.sh")],
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(work),
            "FRIDGE_DB_CONTAINER": container,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert any("DROP DATABASE IF EXISTS _bkrt_src" in args for args in commands)
    assert any("DROP DATABASE IF EXISTS _bkrt_dst" in args for args in commands)
    assert not list(work.iterdir())


def test_stub_harness_would_catch_the_original_bug(tmp_path, monkeypatch):
    """Meta-test: passing GzipFile straight to subprocess really does deliver gzip bytes.

    This pins the reason the implementation streams the data itself, so a future 'simplification'
    back to `stdin=fh` fails here with an explanation rather than silently breaking recovery.
    """
    bindir, _, received = _stub_bin(tmp_path, "psql")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    src = tmp_path / "dump.sql.gz"
    with gzip.open(src, "wb") as fh:
        fh.write(SQL.encode())

    with gzip.open(src, "rb") as fh:
        subprocess.run(["psql"], stdin=fh, check=True)  # type: ignore[arg-type]  # the bug, on purpose

    assert received.read_bytes().startswith(b"\x1f\x8b"), (
        "GzipFile-as-stdin no longer delivers compressed bytes; the restore workaround may be revisitable"
    )
