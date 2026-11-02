"""Settings → System status: what an admin sees after an ingest 507, and what drives the banner."""

import asyncio
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import system_status
from backend.main import app
from backend.routers.auth import get_current_user

GB = 1024**3


@pytest.mark.parametrize(
    "free, level",
    [(None, "unknown"), (GB // 2, "paused"), (3 * GB, "low"), (10 * GB, "ok")],
)
def test_disk_level_matches_the_ingest_gate(free, level):
    # "paused" is exactly where require_free_space starts refusing ingest (the 1 GB headroom).
    assert system_status.disk_level(free) == level


def test_cache_folders_on_one_disk_are_one_entry_and_missing_ones_are_skipped(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    disks = system_status.cache_disks(
        {"S3 image cache": str(tmp_path / "a"), "Crop cache": str(tmp_path / "b"), "Gone": str(tmp_path / "x")},
    )
    assert len(disks) == 1
    assert [f["label"] for f in disks[0]["folders"]] == ["S3 image cache", "Crop cache"]
    assert disks[0]["total_bytes"] >= disks[0]["free_bytes"] > 0
    assert disks[0]["level"] in ("ok", "low")


def test_a_merge_the_server_has_not_loaded_says_restart_but_a_frontend_only_one_does_not(monkeypatch):
    running = {"commit": "aaaaaaa", "subject": "old", "committed_at": "2026-10-11T00:00:00+00:00", "backend_tree": "t1"}
    monkeypatch.setattr(system_status, "_RUNNING", running)
    monkeypatch.setattr(system_status, "_git_commit", lambda: {**running, "commit": "bbbbbbb", "backend_tree": "t2"})
    assert system_status.server_info()["restart_needed"] is True
    monkeypatch.setattr(system_status, "_git_commit", lambda: {**running, "commit": "ccccccc"})  # frontend-only merge
    assert system_status.server_info()["restart_needed"] is False
    monkeypatch.setattr(system_status, "_git_commit", lambda: None)  # no git checkout (a container)
    assert system_status.server_info()["restart_needed"] is False


def test_the_backend_tree_is_read_from_this_checkout():
    info = system_status._git_commit()
    if info is None:
        pytest.skip("not a git checkout")
    else:  # (not narrowed by pytest.skip under CI's lean mypy env)
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD:backend"],
            capture_output=True,
            text=True,
            cwd=system_status.REPO_ROOT,
        )
        assert info["backend_tree"] == tree.stdout.strip() and len(info["backend_tree"]) == 40


class _Pool:
    def __init__(self, fail=False):
        self.fail = fail

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                if pool.fail:
                    raise ConnectionRefusedError("database down")
                return object()

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class _Db:
    def __init__(self, fail=False):
        self.pool = _Pool(fail)
        self.fail = fail

    async def fetchval(self, query, *args):
        if self.fail:
            raise ConnectionRefusedError("database down")
        return 140 * GB


def test_collect_reports_the_database_disk_and_health(monkeypatch, tmp_path):
    async def volume(conn):
        return 172 * GB, 3 * GB

    async def nothing(db):
        return []

    monkeypatch.setattr(system_status, "db_volume_bytes", volume)
    monkeypatch.setattr(system_status, "pending_migrations", nothing)
    monkeypatch.setattr(system_status, "unknown_migrations", nothing)
    monkeypatch.setattr(system_status, "stale_functions", nothing)
    status = asyncio.run(system_status.collect(_Db(), {"S3 image cache": str(tmp_path)}))
    disk = status["database_disk"]
    assert (disk["total_bytes"], disk["free_bytes"], disk["level"]) == (172 * GB, 3 * GB, "low")
    assert disk["ingest_paused_below_bytes"] == GB and disk["low_below_bytes"] == 5 * GB
    assert status["database"] == {
        "reachable": True,
        "size_bytes": 140 * GB,
        "pending_migrations": [],
        "unknown_migrations": [],
        "stale_functions": [],
    }
    assert status["cache_disks"][0]["folders"][0]["label"] == "S3 image cache"
    assert "restart_needed" in status["server"]


def test_an_unreachable_database_is_reported_not_raised(tmp_path):
    status = asyncio.run(system_status.collect(_Db(fail=True), {}))
    assert status["database"]["reachable"] is False
    assert "database down" in status["database"]["error"]
    assert status["database_disk"]["level"] == "unknown"


@pytest.mark.parametrize("role, code", [("admin", 200), ("power_user", 403), ("guest", 403)])
def test_only_admins_can_read_it(monkeypatch, role, code):
    async def fake_collect(db, folders, backup_folder=None):
        return {"ok": True}

    monkeypatch.setattr(system_status, "collect", fake_collect)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "u", "role": role}
    try:
        assert TestClient(app).get("/api/admin/status").status_code == code
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# Backups: the folder scripts/backup_local.py writes to (FRIDGE_BACKUP_DIR).
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def _stamp(at):
    return at.strftime("%Y%m%dT%H%M%SZ")


def _dump(folder, at, *, prefix="freedomridge", manifest=True, verified=True, size=1000, **fields):
    dump = folder / f"{prefix}_{_stamp(at)}.sql.gz"
    dump.write_bytes(b"x" * size)
    if manifest:
        record = {
            "file": dump.name,
            "verified": verified,
            "created_at": _stamp(at),
            "size_bytes": size,
            "dump_seconds": 1020,
            "verify_seconds": 204,
            "server_version": "16.13 (Debian 16.13-1.pgdg120+1)",
            "schema_version": "0002_b.sql",
            "migrations_applied": 101,
            "row_counts": {"public.samples": 3, "public.datasets": 1},
            **fields,
        }
        (folder / f"{dump.name}.manifest.json").write_text(json.dumps(record))
    return dump


def _failure(folder, at, error="BackupError: pg_dump exited 1", prefix="freedomridge"):
    path = folder / f"{prefix}_{_stamp(at)}.failed.json"
    path.write_text(json.dumps({"verified": False, "failed_at": _stamp(at), "error": error}))
    return path


@pytest.mark.parametrize("folder", [None, ""])
def test_backups_not_configured(folder):
    assert system_status.read_backup_folder(folder, NOW) == {"level": "not_configured", "folder": None}


def test_a_missing_or_unreadable_backup_folder_says_so(tmp_path):
    missing = system_status.read_backup_folder(str(tmp_path / "unmounted"), NOW)
    assert missing["level"] == "unreadable" and "FileNotFoundError" in missing["error"]
    (tmp_path / "a-file").write_text("")
    assert system_status.read_backup_folder(str(tmp_path / "a-file"), NOW)["level"] == "unreadable"


def test_an_empty_backup_folder_has_no_backup(tmp_path):
    status = system_status.read_backup_folder(str(tmp_path), NOW)
    assert status["level"] == "none"
    assert status["latest"] is None and status["last_failure"] is None
    assert status["kept"] == {"count": 0, "total_bytes": 0, "oldest_created_at": None}
    assert status["disk"]["total_bytes"] >= status["disk"]["free_bytes"] > 0


def test_the_newest_verified_dump_is_latest_and_everything_else_is_not_counted(tmp_path):
    newest = NOW - timedelta(hours=13)
    _dump(tmp_path, NOW - timedelta(days=2), size=500)
    _dump(tmp_path, newest, size=700, migrations_applied=102)
    _dump(tmp_path, NOW - timedelta(hours=1), manifest=False)  # by hand, or a run still going
    _dump(tmp_path, NOW - timedelta(hours=2), verified=False)
    _dump(tmp_path, NOW - timedelta(hours=3), prefix="aurora", manifest=False)
    corrupt = _dump(tmp_path, NOW - timedelta(hours=4), manifest=False)
    (tmp_path / f"{corrupt.name}.manifest.json").write_text("{not json")
    (tmp_path / f"freedomridge_{_stamp(NOW)}.sql.gz.manifest.json").write_text(json.dumps({"verified": True}))
    (tmp_path / "freedomridge_20261399T000000Z.sql.gz").write_bytes(b"")  # not a real time
    (tmp_path / f"freedomridge_{_stamp(NOW)}.sql.gz.partial").write_bytes(b"x")
    (tmp_path / "backup.log").write_text("cron output")

    status = system_status.read_backup_folder(str(tmp_path), NOW)
    assert status["level"] == "ok"
    assert status["latest"] == {
        "file": f"freedomridge_{_stamp(newest)}.sql.gz",
        "created_at": newest.isoformat(),
        "size_bytes": 700,
        "dump_seconds": 1020,
        "verify_seconds": 204,
        "tables": 2,
        "schema_version": "0002_b.sql",
        "migrations_applied": 102,
        "server_version": "16.13 (Debian 16.13-1.pgdg120+1)",
    }
    oldest = (NOW - timedelta(days=2)).isoformat()
    assert status["kept"] == {"count": 2, "total_bytes": 1200, "oldest_created_at": oldest}
    assert status["unverified_dumps"] == 5  # including the one whose name isn't a real time
    assert status["last_failure"] is None


def test_a_failure_newer_than_the_latest_dump_is_failed_and_an_older_one_is_history(tmp_path):
    _failure(tmp_path, NOW - timedelta(days=3), error="old problem")
    _dump(tmp_path, NOW - timedelta(days=1))
    status = system_status.read_backup_folder(str(tmp_path), NOW)
    assert (status["level"], status["last_failure"]) == ("ok", None)

    failed_at = NOW - timedelta(hours=12)
    _failure(tmp_path, failed_at, error="BackupError: free space 3 GB, need 40 GB")
    _failure(tmp_path, failed_at - timedelta(hours=1), error="an earlier try")
    status = system_status.read_backup_folder(str(tmp_path), NOW)
    assert status["level"] == "failed"
    error = "BackupError: free space 3 GB, need 40 GB"
    assert status["last_failure"] == {"failed_at": failed_at.isoformat(), "error": error}
    assert status["latest"] is not None  # the last good one is still shown


def test_failures_with_no_good_backup_and_unreadable_records_still_fail(tmp_path):
    record = _failure(tmp_path, NOW - timedelta(hours=1))
    record.write_text("[]")
    status = system_status.read_backup_folder(str(tmp_path), NOW)
    assert status["level"] == "failed" and status["latest"] is None
    assert status["last_failure"]["error"] == "no reason recorded"


def test_a_backup_is_overdue_only_past_the_schedules_limit(tmp_path):
    _dump(tmp_path, NOW - timedelta(hours=36))
    daily = timedelta(hours=36)
    assert system_status.read_backup_folder(str(tmp_path), NOW, daily)["level"] == "ok"
    overdue = system_status.read_backup_folder(str(tmp_path), NOW + timedelta(seconds=1), daily)
    assert (overdue["level"], overdue["overdue_after_hours"]) == ("overdue", 36)
    unscheduled = system_status.read_backup_folder(str(tmp_path), NOW + timedelta(days=400))
    assert (unscheduled["level"], unscheduled["overdue_after_hours"]) == ("ok", None)  # a one-off backup


class _VersionDb:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, query, *args):
        if isinstance(self.row, Exception):
            raise self.row
        return self.row


def _versions(server, schema, applied):
    return {"server_version": server, "schema_version": schema, "migrations_applied": applied}


@pytest.mark.parametrize(
    "current, same_schema, same_major",
    [
        (_versions("16.4", "0002_b.sql", 101), True, True),
        (_versions("16.13", "0004_d.sql", 103), False, True),
        (_versions("17.0", "0002_b.sql", 101), True, False),
    ],
)
def test_backups_compare_the_dump_with_this_database(tmp_path, current, same_schema, same_major):
    _dump(tmp_path, datetime.now(timezone.utc) - timedelta(hours=1))
    status = asyncio.run(system_status.backups(_VersionDb(current), str(tmp_path)))
    assert status["current"] == current
    assert (status["same_schema"], status["same_postgres_major"]) == (same_schema, same_major)


def test_backups_without_a_readable_database_still_show_the_folder(tmp_path):
    _dump(tmp_path, datetime.now(timezone.utc) - timedelta(hours=1))
    status = asyncio.run(system_status.backups(_VersionDb(ConnectionRefusedError("down")), str(tmp_path)))
    assert status["level"] == "ok" and "current" not in status


def test_collect_includes_backups(monkeypatch, tmp_path):
    async def volume(conn):
        return 172 * GB, 30 * GB

    monkeypatch.setattr(system_status, "db_volume_bytes", volume)
    assert asyncio.run(system_status.collect(_Db(fail=True), {}))["backups"]["level"] == "not_configured"
    status = asyncio.run(system_status.collect(_Db(fail=True), {}, backup_folder=str(tmp_path)))
    assert status["backups"]["level"] == "none"


def test_a_failure_newer_than_an_overdue_backup_says_failed(tmp_path):
    _dump(tmp_path, NOW - timedelta(hours=40))
    _failure(tmp_path, NOW - timedelta(hours=1))
    assert system_status.read_backup_folder(str(tmp_path), NOW, timedelta(hours=36))["level"] == "failed"


def test_another_databases_dumps_cannot_hide_a_failure(tmp_path):
    _failure(tmp_path, NOW - timedelta(hours=2))
    _dump(tmp_path, NOW - timedelta(hours=1), prefix="otherdb")
    status = system_status.read_backup_folder(str(tmp_path), NOW)
    assert (status["level"], status["latest"], status["unverified_dumps"]) == ("failed", None, 1)


def test_a_dump_from_a_wrong_clock_is_not_trusted(tmp_path):
    _dump(tmp_path, NOW + timedelta(hours=2))
    _failure(tmp_path, NOW + timedelta(days=1))
    status = system_status.read_backup_folder(str(tmp_path), NOW)
    assert (status["level"], status["unverified_dumps"]) == ("none", 1)


def test_a_dump_cut_short_after_it_was_verified_is_not_counted(tmp_path):
    older = _dump(tmp_path, NOW - timedelta(hours=30), size=900)
    newest = _dump(tmp_path, NOW - timedelta(hours=6), size=1000)
    newest.write_bytes(b"x" * 10)
    status = system_status.read_backup_folder(str(tmp_path), NOW)
    assert status["latest"]["file"] == older.name and status["unverified_dumps"] == 1


def test_a_stuck_backup_folder_is_unreadable_and_does_not_block_other_threads(monkeypatch, tmp_path):
    monkeypatch.setattr(system_status, "BACKUP_READ_SECONDS", 0.2)
    monkeypatch.setattr(system_status, "read_backup_folder", lambda *args: time.sleep(1))

    async def check():
        started = time.monotonic()
        status = await system_status.backups(_VersionDb(None), str(tmp_path))
        # The shared pool (images, crops) is still free while the backup reader is stuck.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 0.5) == "free"
        return status, time.monotonic() - started

    status, took = asyncio.run(check())
    assert status["level"] == "unreadable" and "no answer from the folder" in status["error"]
    assert took < 0.8
