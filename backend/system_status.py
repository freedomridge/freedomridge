"""What the admin System status page (Settings → System status) and the admin low-disk banner show:
the database disk, the disk(s) the image caches live on, database health, backups, and which code
the server is running. An admin who hits the ingest 507 ("not enough free disk") lands here.

Every number is read live on each call; nothing here writes.
"""

import asyncio
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import backups as backup_jobs
from .diskspace import HEADROOM_BYTES, db_volume_bytes
from .job_queue import JOB_TABLE
from .schema_check import pending_migrations, stale_functions, unknown_migrations
from .version import VERSION

# Below this the database disk is shown as Low (and the banner appears). Ingest is refused below
# the 1 GB headroom (diskspace.require_free_space), so this leaves room to act before that.
LOW_DISK_BYTES = 5 * 1024**3

REPO_ROOT = Path(__file__).resolve().parent.parent


def disk_level(free: Optional[int]) -> str:
    """ok / low / paused (ingest is being refused) / unknown (free space can't be read)."""
    if free is None:
        return "unknown"
    if free < HEADROOM_BYTES:
        return "paused"
    if free < LOW_DISK_BYTES:
        return "low"
    return "ok"


def cache_disks(folders: Dict[str, str]) -> List[Dict[str, Any]]:
    """One entry per filesystem holding the given cache folders (label → path)."""
    disks: Dict[int, Dict[str, Any]] = {}
    for label, path in folders.items():
        try:
            device = os.stat(path).st_dev
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        disk = disks.setdefault(device, {"folders": [], "total_bytes": usage.total, "free_bytes": usage.free})
        disk["folders"].append({"label": label, "path": path})
    for disk in disks.values():
        disk["level"] = "low" if disk["free_bytes"] < LOW_DISK_BYTES else "ok"
    return list(disks.values())


def _git(*args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _git_commit() -> Optional[Dict[str, str]]:
    """The checkout's HEAD (short hash, subject, commit time) and the id of its backend/ tree, or
    None without a git checkout."""
    log = _git("log", "-1", "--format=%h%x00%s%x00%cI")
    backend_tree = _git("rev-parse", "HEAD:backend")
    parts = log.split("\0") if log else []
    if len(parts) != 3 or not backend_tree:
        return None
    return {"commit": parts[0], "subject": parts[1], "committed_at": parts[2], "backend_tree": backend_tree}


# The code this process loaded. Python doesn't reload a merge into the checkout, so a running
# server whose backend/ differs from the checkout's needs a restart to pick it up. The backend/
# tree id, not the commit: a frontend-only merge moves HEAD but needs no backend restart.
_STARTED_AT = datetime.now(timezone.utc)
_RUNNING = _git_commit()


def server_info() -> Dict[str, Any]:
    checkout = _git_commit()
    return {
        "version": VERSION,
        "started_at": _STARTED_AT.isoformat(),
        "running": _RUNNING,
        "checkout": checkout,
        "restart_needed": bool(_RUNNING and checkout and _RUNNING["backend_tree"] != checkout["backend_tree"]),
    }


async def database_health(db) -> Dict[str, Any]:
    try:
        size = await db.fetchval("SELECT pg_database_size(current_database())")
        pending = await pending_migrations(db)
        unknown = await unknown_migrations(db)
        stale = await stale_functions(db)
    except Exception as e:
        return {"reachable": False, "error": f"{type(e).__name__}: {e}"[:300]}
    return {
        "reachable": True,
        "size_bytes": size,
        "pending_migrations": pending,
        "unknown_migrations": unknown,
        "stale_functions": stale,
    }


async def worker_health(db) -> Dict[str, Any]:
    """A missing heartbeat is offline, never an implicitly healthy worker."""
    try:
        rows = await db.fetch(
            """SELECT placement, max(heartbeat_at) AS last_seen,
                      count(*) FILTER (WHERE heartbeat_at > clock_timestamp() - interval '60 seconds') AS online
               FROM workers GROUP BY placement""",
        )
    except Exception as error:
        return {'available': False, 'error': f'{type(error).__name__}: {error}'[:300]}
    health: Dict[str, Any] = {
        'available': True,
        'server': {'online': 0, 'last_seen': None},
        'compute': {'online': 0, 'last_seen': None},
    }
    for row in rows:
        health[row['placement']] = {'online': row['online'], 'last_seen': row['last_seen']}
    return health


# Backups: scripts/backup_local.py (a backup job, or cron) writes `freedomridge_<stamp>.sql.gz` and, only after
# reading the dump back and matching every table's row count, `<dump>.manifest.json` beside it. A failed
# run leaves `freedomridge_<stamp>.failed.json`. This reads that folder; it never runs or checks a dump itself.
# Only the script's default prefix counts, so another database's dumps in the same folder can't hide a failure.
_DUMP = re.compile(r"freedomridge_(\d{8}T\d{6}Z)\.sql\.gz")
_FAILURE = re.compile(r"freedomridge_(\d{8}T\d{6}Z)\.failed\.json")
_OTHER_DUMP = re.compile(r".+\.sql\.gz")
# Later than this is a wrong clock, not a backup: trusting it would keep the card OK (or FAILED) for good.
_CLOCK_SLACK = timedelta(hours=1)


def _stamp_time(stamp: str) -> Optional[datetime]:
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _same_size(dump: Path, manifest: Dict[str, Any]) -> bool:
    """A dump cut short or replaced after it was verified no longer matches its manifest's size."""
    try:
        return dump.stat().st_size == manifest.get("size_bytes")
    except OSError:
        return False


def read_backup_folder(
    folder: Optional[str],
    now: Optional[datetime] = None,
    overdue: Optional[timedelta] = None,
) -> Dict[str, Any]:
    """The backup card from the folder alone: the newest verified dump, a failure newer than it, how
    many are kept, and the folder's disk. `level` is not_configured / unreadable / failed / none /
    overdue / ok; overdue only when the newest verified dump is older than `overdue` (the schedule's)."""
    if not folder:
        return {"level": "not_configured", "folder": None}
    now = now or datetime.now(timezone.utc)
    path = Path(folder)
    try:
        names = {entry.name for entry in os.scandir(path)}
        usage = shutil.disk_usage(path)
    except OSError as error:
        return {"level": "unreadable", "folder": folder, "error": f"{type(error).__name__}: {error.strerror or error}"}

    verified: List[Tuple[datetime, str, Dict[str, Any]]] = []
    unverified = 0
    failures: List[Tuple[datetime, str]] = []
    for name in names:
        dump = _DUMP.fullmatch(name)
        failure = None if dump else _FAILURE.fullmatch(name)
        match = dump or failure
        stamp = _stamp_time(match.group(1)) if match else None
        if stamp is None or stamp > now + _CLOCK_SLACK:
            unverified += 1 if _OTHER_DUMP.fullmatch(name) else 0
            continue
        if failure:
            failures.append((stamp, name))
            continue
        manifest = _read_json(path / f"{name}.manifest.json") if f"{name}.manifest.json" in names else None
        if manifest is not None and manifest.get("verified") is True and _same_size(path / name, manifest):
            verified.append((stamp, name, manifest))
        else:
            unverified += 1
    verified.sort(key=lambda item: (item[0], item[1]), reverse=True)

    latest = None
    if verified:
        created, name, manifest = verified[0]
        counts = manifest.get("row_counts")
        latest = {
            "file": name,
            "created_at": created.isoformat(),
            "size_bytes": manifest.get("size_bytes"),
            "dump_seconds": manifest.get("dump_seconds"),
            "verify_seconds": manifest.get("verify_seconds"),
            "tables": len(counts) if isinstance(counts, dict) else None,
            "schema_version": manifest.get("schema_version"),
            "migrations_applied": manifest.get("migrations_applied"),
            "server_version": manifest.get("server_version"),
        }
    last_failure = None
    if failures:
        failed, name = max(failures)
        if not verified or failed > verified[0][0]:
            record = _read_json(path / name) or {}
            last_failure = {"failed_at": failed.isoformat(), "error": str(record.get("error") or "no reason recorded")}

    kept_bytes = 0
    for _, name, _ in verified:
        try:
            kept_bytes += (path / name).stat().st_size
        except OSError:
            pass
    if last_failure:
        level = "failed"
    elif not verified:
        level = "none"
    elif overdue is not None and now - verified[0][0] > overdue:
        level = "overdue"
    else:
        level = "ok"
    return {
        "level": level,
        "folder": folder,
        "overdue_after_hours": overdue.total_seconds() / 3600 if overdue is not None else None,
        "latest": latest,
        "last_failure": last_failure,
        "kept": {
            "count": len(verified),
            "total_bytes": kept_bytes,
            "oldest_created_at": verified[-1][0].isoformat() if verified else None,
        },
        "unverified_dumps": unverified,
        "disk": {"total_bytes": usage.total, "free_bytes": usage.free},
    }


def job_dump(folder: str, job_id: str) -> Optional[Dict[str, Any]]:
    """The verified dump a backup job made, found by the job id backup_local.py records in its manifest."""
    path = Path(folder)
    for manifest_path in path.glob("*.manifest.json"):
        name = manifest_path.name.removesuffix(".manifest.json")
        manifest = _read_json(manifest_path)
        if (
            _DUMP.fullmatch(name)
            and manifest is not None
            and manifest.get("job_id") == job_id
            and manifest.get("verified") is True
            and _same_size(path / name, manifest)
        ):
            counts = manifest.get("row_counts")
            return {
                "file": name,
                "size_bytes": manifest.get("size_bytes"),
                "tables": len(counts) if isinstance(counts, dict) else None,
            }
    return None


async def database_versions(db) -> Optional[Dict[str, Any]]:
    """This database's schema and Postgres version, read the way backup_local.py records them."""
    try:
        row = await db.fetchrow(
            """SELECT current_setting('server_version') AS server_version,
                      coalesce(max(filename) FILTER (WHERE filename ~ '^[0-9]'), '') AS schema_version,
                      count(*) AS migrations_applied
               FROM schema_migrations""",
        )
    except Exception:
        return None
    return dict(row) if row else None


def _major(version: Optional[str]) -> Optional[str]:
    match = re.match(r"\d+", version or "")
    return match.group(0) if match else None


# A thread of its own: a read on a stuck drive or network mount can block for good. Then only this thread is
# stuck, never the shared pool that serves images, and later checks give up after BACKUP_READ_SECONDS.
_BACKUP_READER = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="backup-folder")
BACKUP_READ_SECONDS = 10


async def backups(db, folder: Optional[str]) -> Dict[str, Any]:
    try:
        schedule = await db.fetchrow("SELECT interval_hours, start_hour, keep, timing_changed_at FROM backup_schedule")
        active = await db.fetchrow(
            f"""SELECT jobs.id, state, created_at, started_at, controls.cancel_requested FROM {JOB_TABLE}
                WHERE kind=$1 AND state IN ('queued','running') ORDER BY created_at LIMIT 1""",
            backup_jobs.KIND,
        )
    except Exception:
        schedule = active = None  # database unreachable
    schedule = dict(schedule) if schedule else None
    overdue = backup_jobs.overdue_after(schedule)
    try:
        status = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(_BACKUP_READER, read_backup_folder, folder, None, overdue),
            BACKUP_READ_SECONDS,
        )
    except (asyncio.TimeoutError, TimeoutError):
        error = f"no answer from the folder in {BACKUP_READ_SECONDS} s (a stuck drive or network mount?)"
        return {"level": "unreadable", "folder": folder, "error": error}
    if folder:
        now = datetime.now(timezone.utc)
        upcoming = backup_jobs.next_run(schedule, now) if schedule else None
        status["schedule"] = (
            {**schedule, "next_run_at": upcoming.isoformat() if upcoming else None} if schedule else None
        )
        status["active_job"] = dict(active) if active else None
    latest = status.get("latest")
    current = await database_versions(db) if latest else None
    if latest and current:
        status["current"] = current
        status["same_schema"] = (latest["schema_version"], latest["migrations_applied"]) == (
            current["schema_version"],
            current["migrations_applied"],
        )
        status["same_postgres_major"] = _major(latest["server_version"]) == _major(current["server_version"])
    return status


async def collect(db, cache_folders: Dict[str, str], backup_folder: Optional[str] = None) -> Dict[str, Any]:
    volume = None
    try:
        async with db.pool.acquire() as conn:
            volume = await db_volume_bytes(conn)
    except Exception:
        pass  # database unreachable: database_health reports it
    total, free = volume if volume else (None, None)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "database_disk": {
            "total_bytes": total,
            "free_bytes": free,
            "level": disk_level(free),
            "low_below_bytes": LOW_DISK_BYTES,
            "ingest_paused_below_bytes": HEADROOM_BYTES,
        },
        "cache_disks": cache_disks(cache_folders),
        "database": await database_health(db),
        "workers": await worker_health(db),
        "backups": await backups(db, backup_folder),
        "server": await asyncio.to_thread(server_info),  # git runs a subprocess
    }
