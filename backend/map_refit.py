"""Map re-fit job (kind ``map_refit``): refit one embedding field's UMAP map and swap it in at once.

Three steps with file handoffs, so the heavy middle one can later move to a compute worker:

1. **Export** (server): one consistent snapshot of the field's vectors, as PostgreSQL binary COPY of
   ``(sample_id, vector_version, vector)``, into the job's work folder. Before it, the job marks itself
   the field's active re-fit (``embedding_map_state.active_job_id``); placement of new samples on that
   field's map pauses until the job ends, so they wait in the projection queue for the new map.
2. **Fit and transform** (pure computation): ``python -m fridge.map_fit`` in its own memory-capped
   scope (``systemd-run --user --scope -p MemoryMax=…``), or in a container without systemd, within the
   container's own memory limit (``containment``). The cap keeps ~20% of host RAM free; an
   out-of-memory kill ends only that process. Too little memory skips the run with the reason. It runs
   on one CPU per core and keeps at least 20% of the cores (rounded up) entirely free for the database and
   the app (``fit_cpus``).
3. **Publish** (server): one transaction under the dataset's exclusive lifecycle gate. It replaces the
   field's coordinates, skipping samples whose vector changed or that were deleted after the export
   (they stay queued and are placed on the new map), rebuilds the map's ranks, records the new map as
   current, and finishes the job in that same transaction.

No model is kept: new samples are placed on a re-fitted map from their nearest mapped neighbours
(``map_placement``). Failure or cancellation at any step leaves the old map in place; placement resumes on it
as soon as the job is no longer queued or running. Files live behind ``map_artifacts``.

Each step names where the next one runs (``StepResult.placement``): the export, the publish and the ranks on
the server, the fit on compute. A runner that takes both placements (the default, one machine) carries on; one
that takes only one gives the job back to the queue for the other side, so a compute worker can do the fit.
The job is queued as compute work.
"""

import asyncio
import io
import json
import logging
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
from fastapi import HTTPException

from . import job_heartbeat, job_queue
from .diskspace import estimate_viz_rank_bytes, require_free_space
from .field_registry import MAP_RANK_APPLICATION, MAP_SWAP_APPLICATION
from .job_queue import JobPolicy
from .job_runner import StepResult
from .map_artifacts import LocalArtifactStore, sha256

logger = logging.getLogger(__name__)
KIND = 'map_refit'
# A fit of a few million points takes about an hour; the unit budget covers the slowest step. Each step
# publishes a checkpoint, so a restart repeats only the step it interrupted.
# Why the rank step cannot be cancelled (job_queue.cancel_job refuses a job whose checkpoint says so).
UNCANCELLABLE = (
    'The new map is already in use; it is being ranked for the scatter, which cannot be stopped. It finishes on its own'
)
RANK_MESSAGE = 'The new map is in; ranking it for the scatter (it cannot be stopped while it runs); ingest has resumed'
POLICY = JobPolicy(max_unit_seconds=6 * 3600, max_failures=3, retry_seconds=60, max_retry_seconds=3600)
# One fit at a time: the queue starts at most one unfinished job holding this resource.
RESOURCES = {'exclusive': 'map_fit'}
# Host RAM the fit's memory cap always leaves free for the database and the app.
KEEP_FREE_FRACTION = 0.20
# Host CPU cores the fit leaves free for the database and the app (fit_cpus): at least this share, rounded up to
# whole cores, with at least one core left to the fit (16 cores: 4 free, the measured configuration).
KEEP_FREE_CORE_FRACTION = 0.20
# The child's interpreter and libraries (numpy, numba, umap-learn without TensorFlow: ~350 MB measured).
CHILD_BASELINE_BYTES = 1024**3
# _auto_fit_sample_size's usable minimum and its share of the memory left after the vectors.
MINIMUM_FIT_POINTS = 50_000
FIT_BUDGET_FRACTION = 0.75
# fridge.core.dataset.FIT_VECTOR_COPIES (the SDK is not importable here): the default memory per fit
# point is this many float32 vectors of the field's dimension.
FIT_VECTOR_COPIES = 13
# How long publication polls for the dataset's exclusive lifecycle gate before retrying later. Writers
# hold it shared only for their own transaction, so polling gets in at a gap.
GATE_WAIT_SECONDS = 30.0
# After taking the gate, how long a re-fit looks for a writer that queued in the same instant (a writer
# looks for a re-fit, finds none, then queues: a few milliseconds) before it keeps the gate.
GATE_GRACE_SECONDS = 0.2
# A map left unranked is healed at once, then (after a failed or cancelled heal) after 1 h, 2 h, 4 h, ...
# up to a day.
HEAL_BACKOFF_HOURS = 1
HEAL_BACKOFF_MAX_HOURS = 24
# A fit handed to compute workers that none claims in this long ends, and placement resumes on the old map.
UNCLAIMED_FIT_HOURS = 12
# What a compute worker downloads for a fit, and what it uploads: fridge.map_fit's coordinates and manifest. No
# fit keeps a model (new samples are placed by their nearest mapped neighbours), and none could cross: a model is a
# pickle, which runs code when loaded.
REMOTE_INPUTS = ('vectors.bin',)
REMOTE_OUTPUTS = ('ids.npy', 'versions.npy', 'coords.npy', 'manifest.json')
# Each output's columns, for its size cap and its check: (columns, largest item size in bytes).
_REMOTE_ARRAYS = {'ids.npy': (1, 8), 'versions.npy': (1, 8), 'coords.npy': (2, 8)}
_NPY_HEADER_BYTES = 4096
_MANIFEST_BYTES = 64 * 1024
# The re-rank after the swap (a test slows it down to look at writers meanwhile).
RANK_SQL = 'SELECT recompute_viz_rank($1, $2, TRUE)'
# Publication's statement budget scales with the map to allow for the full re-rank,
# with a ten-minute floor for smaller maps.
PUBLISH_SECONDS_PER_POINT = 1e-4
PUBLISH_MIN_SECONDS = 600.0
STOP_GRACE_SECONDS = 8
SDK_ROOT = Path(__file__).resolve().parent.parent / 'python'
_EXPORT = """SELECT e.sample_id, e.xmin::text::bigint, e.vector
             FROM embeddings e JOIN samples s ON s.id = e.sample_id
             WHERE s.dataset_id = $1 AND e.field_name = $2 AND e.vector IS NOT NULL"""
_STATE = """SELECT m.*, (j.state IN ('queued','running')) IS TRUE AS active
            FROM embedding_map_state m LEFT JOIN jobs j ON j.id = m.active_job_id
            WHERE m.dataset_id = $1 AND m.field_name = $2"""


class MapRefitError(RuntimeError):
    """A re-fit that cannot succeed by retrying (out of memory, invalid output)."""


def gigabytes(value: int) -> str:
    return f'{value / 1024**3:.1f} GB'


def is_patch_field(field: str) -> bool:
    import re

    return re.search(r'_patch_\d+x\d+(_r\d+c\d+)?$', field) is not None


async def read_state(connection, dataset_id: int, field: str) -> Optional[Dict[str, Any]]:
    row = await connection.fetchrow(_STATE, dataset_id, field)
    return dict(row) if row else None


async def current_points(connection, dataset_id: int, field: str) -> int:
    """Points on the map now: the exact per-map counter (embedding_viz_meta.total_points), no scan."""
    return int(
        await connection.fetchval(
            'SELECT total_points FROM embedding_viz_meta WHERE dataset_id=$1 AND field_name=$2',
            dataset_id,
            field,
        )
        or 0,
    )


async def unfinished_job(
    connection,
    dataset_id: int,
    field: str,
    *,
    refits_only: bool = False,
) -> Optional[Dict[str, Any]]:
    """The map's queued or running job; with ``refits_only``, only a re-fit (not a rank-only heal job)."""
    row = await connection.fetchrow(
        f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
            WHERE kind=$1 AND dataset_id=$2 AND args->>'field'=$3 AND state IN ('queued','running')
              AND NOT ($4 AND args ? 'rank_only')
            ORDER BY created_at LIMIT 1""",
        KIND,
        dataset_id,
        field,
        refits_only,
    )
    return dict(row) if row else None


class MapAdmissionBusy(job_queue.QueueContended):
    """A full layout write or another admission currently owns this map's admission lock."""


async def submit(
    connection,
    dataset_id: int,
    field: str,
    *,
    schedule: Optional[Dict[str, Any]] = None,
    created_by: Optional[int] = None,
    window_end: Optional[datetime] = None,
    wait: bool = True,
) -> Tuple[Dict[str, Any], bool]:
    """Queue a re-fit of one map, or return the one already queued or running for it. A scheduled run
    carries the end of its start window; Run now has none. A rank-only heal job does not count: the
    re-fit queues behind it in the dataset's maintenance lane (and re-ranks anyway)."""
    key = json.dumps([KIND, dataset_id, field])
    if wait:
        await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))', key)
    elif not await connection.fetchval('SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))', key):
        raise MapAdmissionBusy('This map has an in-flight layout write or admission; retry shortly')
    existing = await unfinished_job(connection, dataset_id, field, refits_only=True)
    if existing is not None:
        return existing, False
    job = await job_queue.enqueue_job(
        connection,
        KIND,
        dataset_id=dataset_id,
        args={
            'field': field,
            'schedule_id': schedule['id'] if schedule else None,
            'window_end': window_end.isoformat() if window_end else None,
        },
        placement='compute',
        lane='maintenance',
        resources=dict(RESOURCES),
        created_by=created_by,
    )
    return job, True


# --- Memory ---------------------------------------------------------------------------------------


def host_memory() -> Tuple[int, int]:
    """(total, available) RAM in bytes, from /proc/meminfo."""
    values = {}
    with open('/proc/meminfo') as meminfo:
        for line in meminfo:
            name, amount = line.split(':', 1)
            values[name] = int(amount.split()[0]) * 1024
    return values['MemTotal'], values['MemAvailable']


def host_swap() -> int:
    """The host's swap space in bytes, from /proc/meminfo."""
    with open('/proc/meminfo') as meminfo:
        return next((int(line.split()[1]) * 1024 for line in meminfo if line.startswith('SwapTotal:')), 0)


def _cgroup_folder(pid) -> Optional[str]:
    """The cgroup v2 folder of a process ('self' for this one), or None outside cgroup v2."""
    try:
        with open(f'/proc/{pid}/cgroup') as cgroup:
            path = next(line.split(':', 2)[2].strip() for line in cgroup if line.startswith('0::'))
    except (OSError, StopIteration):
        return None
    return os.path.join('/sys/fs/cgroup', path.lstrip('/'))


def cgroup_headroom() -> Optional[int]:
    """What this process's own cgroup may still allocate, or None without a memory limit. Page cache counts as
    free, as the kernel reclaims it before an out-of-memory kill (the SDK's _cgroup_headroom_bytes, which sizes
    the fit the same way)."""
    folder = _cgroup_folder('self')
    if folder is None:
        return None
    try:
        with open(os.path.join(folder, 'memory.max')) as limit_file:
            limit = limit_file.read().strip()
        if limit == 'max':
            return None
        with open(os.path.join(folder, 'memory.current')) as current:
            used = int(current.read().strip())
        with open(os.path.join(folder, 'memory.stat')) as stat_file:
            stat = dict(line.split() for line in stat_file)
    except (OSError, ValueError):
        return None
    cached = int(stat.get('active_file', 0)) + int(stat.get('inactive_file', 0))
    return max(0, int(limit) - used + cached)


def fit_allowance() -> Tuple[int, int]:
    """(allowance, total): what the fit may use while KEEP_FREE_FRACTION of host RAM stays free, and never more
    than this process's own memory limit leaves (a container's: the fit shares it when there is no systemd)."""
    total, available = host_memory()
    allowance = available - int(KEEP_FREE_FRACTION * total)
    headroom = cgroup_headroom()
    return (allowance if headroom is None else min(allowance, headroom)), total


def _core_of(cpu: int) -> str:
    """The SMT siblings of a CPU (its core), as the kernel lists them."""
    try:
        with open(f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list') as siblings:
            return siblings.read().strip()
    except OSError:
        return str(cpu)


def fit_cpus() -> list:
    """The CPUs the fit runs on: one CPU of each core (its first SMT sibling), on every core this process may use
    but at least KEEP_FREE_CORE_FRACTION of them (rounded up, at least one core left to the fit), which stay
    entirely free. The free cores are spread evenly over the core numbering; where that numbering runs L3 domain
    by L3 domain, each domain gets its share. Reserving full cores avoids SMT contention with
    latency-sensitive database work. Affinity also works when CPU quota controllers are not
    delegated to user scopes. The fit's thread pools (numba, joblib, OpenBLAS) size themselves
    to the CPUs it may use."""
    cores: Dict[str, list] = {}
    for cpu in sorted(os.sched_getaffinity(0)):
        cores.setdefault(_core_of(cpu), []).append(cpu)
    groups = list(cores.values())
    free = len(groups) - max(1, int(len(groups) * (1 - KEEP_FREE_CORE_FRACTION)))
    skip = {int((k + 0.5) * len(groups) / free) for k in range(free)}
    return [group[0] for index, group in enumerate(groups) if index not in skip]


def minimum_fit_bytes(rows: int, dim: int, bytes_per_point: Optional[int] = None) -> int:
    """The smallest fit `_auto_fit_sample_size` would run: the resident float32 vectors, plus its
    usable-minimum fit (50,000 points at the measured or default cost per point and 75% budget),
    coordinates and the child's own baseline."""
    vectors = rows * dim * 4
    per_point = bytes_per_point or dim * 4 * FIT_VECTOR_COPIES
    minimum_fit = int(min(rows, MINIMUM_FIT_POINTS) * per_point / FIT_BUDGET_FRACTION)
    return vectors + minimum_fit + rows * 24 + CHILD_BASELINE_BYTES


class ContainmentUnavailable(RuntimeError):
    """The fit cannot be run in a memory-capped scope on this machine."""


def containment(allowance: int) -> list:
    """The command prefix that runs the fit memory-capped: its own systemd scope (_systemd_scope), or, where
    there is no systemd (a container), an empty prefix when this process's own cgroup has a memory limit
    without swap, a container's mem_limit. The fit then shares that limit: fit_allowance keeps it within what
    the limit leaves, and the fit makes itself the kernel's first choice for an out-of-memory kill
    (fridge.map_fit), so the runner survives one."""
    try:
        return _systemd_scope(allowance)
    except ContainmentUnavailable as error:
        problem = _shared_limit_problem()
        if problem:
            raise ContainmentUnavailable(f'{error}; {problem}') from None
        return []


def _shared_limit_problem() -> Optional[str]:
    """Why the fit cannot share this process's memory limit, or None when it can."""
    folder = _cgroup_folder('self')
    if folder is None or cgroup_headroom() is None:
        return 'this process has no memory limit of its own to share (in Docker, set JOB_RUNNER_MEMORY_LIMIT)'
    try:
        with open(os.path.join(folder, 'memory.swap.max')) as swap_file:
            swap = swap_file.read().strip()
    except OSError:
        swap = None  # swap is not accounted per cgroup on this kernel
    if swap != '0' and host_swap():
        # A fit that swaps instead of being stopped at the limit slows the whole host for hours.
        return (
            f'its memory limit allows swap (memory.swap.max={swap or "not accounted"}): set the swap limit to 0 '
            '(in Docker, memswap_limit equal to mem_limit)'
        )
    return None


def _systemd_scope(allowance: int) -> list:
    """The prefix that runs the fit in its own memory-capped scope (memory.max = the allowance, no swap).
    Verified from inside a probe scope: systemd-run succeeds even where the user manager does not delegate
    the memory controller, and then nothing is capped."""
    if shutil.which('systemd-run') is None:
        raise ContainmentUnavailable('systemd-run is not installed')
    command = ['systemd-run', '--user', '--scope', '--quiet', '-p', f'MemoryMax={allowance}', '-p', 'MemorySwapMax=0']
    folder = '/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)'
    try:
        probe = subprocess.run(
            [*command, '--', 'sh', '-c', f'cat "{folder}/memory.max" "{folder}/memory.swap.max"'],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContainmentUnavailable(f'systemd-run --user --scope did not run: {error}')
    if probe.returncode != 0:
        lines = (probe.stderr or probe.stdout).strip().splitlines()
        raise ContainmentUnavailable(f'systemd-run --user --scope failed: {lines[-1] if lines else probe.returncode}')
    values = probe.stdout.split()
    page = os.sysconf('SC_PAGE_SIZE')
    if len(values) != 2 or not values[0].isdigit() or not 0 <= allowance - int(values[0]) < page or values[1] != '0':
        raise ContainmentUnavailable(
            'the memory limit is not applied inside the scope (the user systemd manager does not delegate the '
            f'memory controller): memory.max={values[0] if values else "?"}',
        )
    return [*command, '--']


# --- Steps ----------------------------------------------------------------------------------------


async def _backup_running(pool) -> bool:
    async with pool.acquire(timeout=1) as connection:
        return bool(
            await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM jobs WHERE kind='backup' AND state IN ('queued','running'))",
                timeout=5,
            ),
        )


async def _wait_for_backup(pool) -> None:
    """Never during the backup: wait, without spending the failure budget, until it finishes."""
    if await _backup_running(pool):
        raise job_queue.QueueContended('Waiting for the database backup to finish')


def _job_args(job) -> Tuple[int, str]:
    field = (job['args'] or {}).get('field')
    if not field or is_patch_field(field):
        raise MapRefitError('A map re-fit needs a whole-image embedding field')
    return job['dataset_id'], field


async def _estimate(pool, dataset_id: int, field: str) -> Tuple[int, int]:
    """(points, dimension) of the field without scanning it: the per-map point counter plus the projection queue, and
    one vector's dimension."""
    async with pool.acquire(timeout=1) as connection:
        points = await current_points(connection, dataset_id, field)
        pending = await connection.fetchval(
            'SELECT count(*) FROM embedding_projection_pending WHERE dataset_id=$1 AND field_name=$2',
            dataset_id,
            field,
            timeout=60,
        )
        dimension = await connection.fetchval(
            """SELECT vector_dims(e.vector) FROM (
                   (SELECT sample_id FROM embedding_viz_rank WHERE dataset_id=$1 AND field_name=$2 LIMIT 1)
                   UNION ALL
                   (SELECT sample_id FROM embedding_projection_pending WHERE dataset_id=$1 AND field_name=$2 LIMIT 1)
               ) one JOIN embeddings e ON e.sample_id = one.sample_id AND e.field_name = $2 LIMIT 1""",
            dataset_id,
            field,
            timeout=60,
        )
    return points + pending, dimension or 0


def _memory_check(rows: int, dim: int, bytes_per_point: Optional[int] = None) -> Tuple[Optional[str], int]:
    """(reason to skip or None, allowance)."""
    allowance, total = fit_allowance()
    needed = minimum_fit_bytes(rows, dim, bytes_per_point)
    if allowance < needed:
        within = ", within this process's memory limit" if cgroup_headroom() is not None else ''
        return (
            f'Not enough memory to fit this map: it needs at least {gigabytes(needed)} and '
            f'{gigabytes(max(0, allowance))} is available while keeping {KEEP_FREE_FRACTION:.0%} of '
            f'{gigabytes(total)} RAM free{within}'
        ), allowance
    return None, allowance


async def _start(pool, job, store, fits_here=True):
    dataset_id, field = _job_args(job)
    window_end = (job['args'] or {}).get('window_end')
    if window_end and datetime.now(timezone.utc) >= datetime.fromisoformat(window_end):
        return _skip(
            job,
            f'Missed its start window (it ended {window_end}): it waited behind another map re-fit or a backup; '
            'the next scheduled time will try again',
        )
    await _wait_for_backup(pool)
    # Skip before pausing projection and exporting when this machine cannot fit the map anyway. A runner that
    # leaves fits to compute workers doesn't measure itself: the worker sizes the fit to its own memory.
    rows, dim = await _estimate(pool, dataset_id, field)
    if rows and dim and fits_here:
        reason, allowance = _memory_check(rows, dim, (await _learned_sizing(pool, dataset_id, field))[0])
        if reason:
            return _skip(job, reason)
        try:
            await asyncio.to_thread(containment, allowance)
        except ContainmentUnavailable as error:
            return _skip(job, f'Memory containment is not available on this machine: {error}')
    model_id = str(uuid.uuid4())

    async def activate(connection):
        await connection.execute(
            """INSERT INTO embedding_map_state (dataset_id, field_name, active_job_id, active_since)
               VALUES ($1, $2, $3, clock_timestamp())
               ON CONFLICT (dataset_id, field_name) DO UPDATE
                 SET active_job_id = EXCLUDED.active_job_id, active_since = EXCLUDED.active_since""",
            dataset_id,
            field,
            job['id'],
        )

    return StepResult(
        checkpoint={'phase': 'export', 'model_id': model_id},
        message='Exporting vectors; new samples wait to be placed until the new map is ready',
        publish=activate,
        placement='server',  # the export reads the database
    )


async def _export(pool, job, store, fits_here=True):
    dataset_id, field = _job_args(job)
    await _wait_for_backup(pool)
    work = store.work_dir(job['id'])
    work.mkdir(parents=True, exist_ok=True)
    # The API writes a compute worker's uploads into this folder (staging_dir), and in Docker it runs as another
    # user than this root job-runner. Sticky, as /tmp: each writes and removes only its own files.
    work.chmod(0o1777)
    path = work / 'vectors.bin'
    async with pool.acquire(timeout=1) as connection:
        points = await current_points(connection, dataset_id, field)
        pending = await connection.fetchval(
            'SELECT count(*) FROM embedding_projection_pending WHERE dataset_id=$1 AND field_name=$2',
            dataset_id,
            field,
            timeout=60,
        )
        dimension = await connection.fetchval(
            """SELECT vector_dims(e.vector) FROM embedding_viz_rank r
               JOIN embeddings e ON e.sample_id = r.sample_id AND e.field_name = r.field_name
               WHERE r.dataset_id=$1 AND r.field_name=$2 LIMIT 1""",
            dataset_id,
            field,
            timeout=60,
        )
        # The export (30 bytes of row header plus the vector), the fit's outputs (20 bytes a row), and a quarter
        # more to spare.
        needed = int((points + pending) * (30 + 4 * (dimension or 1024) + 20) * 1.25)
        free = shutil.disk_usage(work).free
        if free < needed:
            return _skip(
                job,
                f'Not enough disk space in the model folder: the re-fit needs about {gigabytes(needed)}, '
                f'{gigabytes(free)} is free',
            )
        async with connection.transaction(isolation='repeatable_read', readonly=True):
            await connection.execute('SET LOCAL statement_timeout = 0')
            await connection.copy_from_query(
                _EXPORT,
                dataset_id,
                field,
                output=str(path),
                format='binary',
                timeout=POLICY.max_unit_seconds,
            )
    rows, dim = await asyncio.to_thread(_export_shape, path)
    if rows < 5:
        raise MapRefitError(f'Not enough vectors to fit a map (found {rows}, need at least 5)')
    return StepResult(
        checkpoint={**job['checkpoint'], 'phase': 'fit', 'rows': rows, 'dimension': dim},
        message=(
            f'Fitting the map on {rows:,} vectors; new samples wait to be placed until it is ready'
            if fits_here
            else f'Waiting for a compute worker to fit the map on {rows:,} vectors; new samples wait to be placed'
        ),
        placement='compute',  # the fit is pure computation, so a compute worker can take it
    )


def _export_shape(path: Path) -> Tuple[int, int]:
    size = path.stat().st_size
    with open(path, 'rb') as source:
        header = source.read(19)
        extension = struct.unpack('>i', header[15:19])[0]
        offset = 19 + extension
        if size - offset <= 2:
            return 0, 0
        source.seek(offset + 2 + 4 + 4 + 4 + 8)
        dim = (struct.unpack('>i', source.read(4))[0] - 4) // 4
    return (size - offset - 2) // (30 + 4 * dim), dim


def _skip(job, reason: str) -> StepResult:
    """Finish without a new map: the old one stays, and the schedule tries again at its next time."""
    dataset_id, field = job['dataset_id'], (job['args'] or {}).get('field')

    async def release(connection):
        await connection.execute(
            """UPDATE embedding_map_state SET active_job_id = NULL, active_since = NULL
               WHERE dataset_id=$1 AND field_name=$2 AND active_job_id=$3""",
            dataset_id,
            field,
            job['id'],
        )
        await _record_schedule_result(connection, job, {'skipped': reason})

    return StepResult(finished=True, result={'skipped': reason}, message=f'Skipped: {reason}', publish=release)


async def _record_schedule_result(connection, job, result) -> None:
    schedule_id = (job['args'] or {}).get('schedule_id')
    if schedule_id is not None:
        await connection.execute(
            'UPDATE dataset_job_schedules SET last_result=$3 WHERE id=$1 AND last_job_id=$2',
            schedule_id,
            job['id'],
            result,
        )


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), STOP_GRACE_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):
            process.kill()
            await process.wait()


def _last_line(path: Path) -> str:
    try:
        with open(path, 'rb') as log:
            log.seek(max(0, path.stat().st_size - 4096))
            lines = log.read().decode(errors='replace').strip().splitlines()
    except OSError:
        return ''
    return lines[-1] if lines else ''


class FitOutOfMemory(MapRefitError):
    """The kernel killed the fit at its memory cap."""


def _scope_folder(pid: int) -> Optional[str]:
    """The cgroup folder of the fit's own scope (systemd-run's transient `run-*.scope`), or None: while
    systemd-run starts, the child still runs in the caller's session cgroup, whose memory is not the
    fit's."""
    folder = _cgroup_folder(pid)
    name = os.path.basename(folder or '')
    if not (name.startswith('run-') and name.endswith('.scope')):
        return None
    return folder


def _memory_value(path: str, key: str) -> Optional[int]:
    """One counter of a cgroup memory file (`memory.stat` anon, `memory.events` oom_kill)."""
    try:
        with open(path) as values:
            return next(int(line.split()[1]) for line in values if line.split()[0] == key)
    except (OSError, StopIteration, ValueError, IndexError):
        return None


async def run_fit(
    command: list,
    source: Path,
    output: Path,
    log_path: Path,
    allowance: int,
    bytes_per_point: Optional[int] = None,
    max_fit_size: Optional[int] = None,
    shared: bool = False,
) -> Optional[int]:
    """Run `python -m fridge.map_fit` under the containment prefix; raise unless it succeeded. Returns the
    scope's peak anonymous memory (sampled every second), which sizes the next fit of this map. `shared`: the
    fit has no scope of its own and shares this process's memory-limited cgroup (an empty prefix), which is
    read instead; the runner's own memory in it cancels out, as the fit's anon_before_fit counts it too."""
    environment = {
        **os.environ,
        'PYTHONPATH': os.pathsep.join(filter(None, [str(SDK_ROOT), os.getenv('PYTHONPATH')])),
        'PYTHONUNBUFFERED': '1',  # fit.log shows progress as it happens
    }
    sizing = ['--bytes-per-point', str(bytes_per_point)] if bytes_per_point else []
    if max_fit_size:
        sizing += ['--max-fit-size', str(max_fit_size)]
    if shutil.which('taskset'):
        pinned = ['taskset', '-c', ','.join(map(str, fit_cpus()))]
    else:  # util-linux without taskset: the fit runs as before, on every CPU, rather than failing every night
        pinned = []
        logger.warning('Map re-fit: taskset is not installed, so the fit runs on every CPU and keeps none free')
    peak = None
    # The fit's cgroup folder: a scope once the child is in it, or this process's own when shared. A scope's
    # out-of-memory kills are counted in its parent, which outlives the scope; a shared cgroup counts its own.
    scope = await asyncio.to_thread(_cgroup_folder, 'self') if shared else None
    events = os.path.join(scope, 'memory.events') if scope else None
    kills_before = await asyncio.to_thread(_memory_value, events, 'oom_kill') if events else None
    with open(log_path, 'ab') as log:
        process = await asyncio.create_subprocess_exec(
            *command,
            *pinned,
            sys.executable,
            '-m',
            'fridge.map_fit',
            '--input',
            str(source),
            '--output',
            str(output),
            *sizing,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=environment,
        )
        try:
            while True:
                try:
                    code = await asyncio.wait_for(process.wait(), 1)
                    break
                except (asyncio.TimeoutError, TimeoutError):
                    if scope is None:
                        scope = await asyncio.to_thread(_scope_folder, process.pid)
                        if scope is not None:
                            events = os.path.join(os.path.dirname(scope), 'memory.events')
                            kills_before = await asyncio.to_thread(_memory_value, events, 'oom_kill')
                    if scope is not None:
                        anon = await asyncio.to_thread(_memory_value, os.path.join(scope, 'memory.stat'), 'anon')
                        if anon is not None:
                            peak = max(peak or 0, anon)
        except BaseException:  # cancelled, timed out, lease lost or runner shutdown
            await asyncio.shield(_stop(process))
            raise
    if code == -signal.SIGKILL and events is not None and kills_before is not None:
        # memory.events counts a scope's kills in its parent too (hierarchical), and the parent outlives
        # the scope; a shared cgroup counts its own. No new kill there means something else stopped the fit
        # (kill -9, a scope stop).
        kills_after = await asyncio.to_thread(_memory_value, events, 'oom_kill')
        if kills_after is not None and kills_after == kills_before:
            raise ConnectionError('The fit was killed, not for lack of memory; the re-fit runs it again')
    if code == -signal.SIGKILL:
        raise FitOutOfMemory(
            f'The fit was stopped by the kernel: it needed more than its {gigabytes(allowance)} memory '
            'allowance. The current map is unchanged.',
        )
    if code == -signal.SIGTERM:
        # The job did not stop it: a backend restart signals the runner's whole process group.
        raise ConnectionError('Interrupted by a backend restart; the re-fit runs again when the runner is back')
    if code != 0:
        raise MapRefitError(f'The fit exited with status {code}: {_last_line(log_path)}')
    return peak


async def _learned_sizing(pool, dataset_id: int, field: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """(memory per fit point measured by this map's last fit or raised after a kill, that fit's size, the
    points the map had then)."""
    async with pool.acquire(timeout=1) as connection:
        row = await connection.fetchrow(
            """SELECT fit_bytes_per_point, fit_size, points_at_refit FROM embedding_map_state
               WHERE dataset_id=$1 AND field_name=$2""",
            dataset_id,
            field,
        )
    return (row['fit_bytes_per_point'], row['fit_size'], row['points_at_refit']) if row else (None, None, None)


async def _fit(pool, job, store):
    dataset_id, field = _job_args(job)
    await _wait_for_backup(pool)
    checkpoint = job['checkpoint']
    rows, dim = checkpoint['rows'], checkpoint['dimension']
    # The fit is sized by the same RAM- and dimension-scaled formula, with the memory per fit point this
    # map measured last time (or raised after an out-of-memory kill) instead of its default.
    learned, last_fit_size, last_points = await _learned_sizing(pool, dataset_id, field)
    bytes_per_point = checkpoint.get('bytes_per_point') or learned
    if checkpoint.get('max_fit_size'):
        # A retry after an out-of-memory kill: 75% of the killed attempt (_after_out_of_memory).
        max_fit_size = checkpoint['max_fit_size']
    elif learned and last_fit_size and last_points and last_fit_size < last_points:
        # The cost per point rises with the fit size (measured: 18 KB at 300k, 26 KB at
        # 1.2M points, 512-d), more than the 25% headroom covers across a big jump: when the last fit was
        # held down by memory (a sample of the map), grow at most 1/0.75 beyond the size its cost was
        # measured at. A relative step, so the fit still reaches what the memory allows within a few
        # nights; the child never bounds below the 50,000-point floor. A fit of every point says nothing
        # about a jump (its cost, with the fixed overhead, over-estimates larger fits), so no bound then.
        max_fit_size = math.ceil(last_fit_size / FIT_BUDGET_FRACTION)
    else:
        max_fit_size = None
    reason, allowance = _memory_check(rows, dim, bytes_per_point)
    if reason:
        return _skip(job, reason)
    try:
        command = await asyncio.to_thread(containment, allowance)
    except ContainmentUnavailable as error:
        return _skip(job, f'Memory containment is not available on this machine: {error}')
    work = store.work_dir(job['id'])
    # One folder per attempt: a child orphaned by a killed runner never writes into a retry's outputs.
    output = work / f"fit-{job['attempt']}"
    logger.info(
        'Map re-fit %s: fitting %s rows of %s-d in %s %s',
        job['id'],
        rows,
        dim,
        gigabytes(allowance),
        'of a shared memory limit' if not command else 'scope',
    )
    try:
        peak = await run_fit(
            command,
            work / 'vectors.bin',
            output,
            work / 'fit.log',
            allowance,
            bytes_per_point,
            max_fit_size=max_fit_size,
            shared=not command,
        )
    except FitOutOfMemory as error:
        return await _after_out_of_memory(job, output, error)
    manifest = json.loads((output / 'manifest.json').read_text())
    if manifest['rows'] != rows:
        raise MapRefitError(f'The fit returned {manifest["rows"]} coordinates for {rows} exported vectors')
    measured = None
    if peak is not None and manifest.get('anon_before_fit') is not None and manifest['fit_size']:
        measured = max(1, (peak - manifest['anon_before_fit']) // manifest['fit_size'])
    return StepResult(
        checkpoint={
            **checkpoint,
            'phase': 'publish',
            'output': output.name,
            'fit_size': manifest['fit_size'],
            'fit_seconds': manifest['fit_seconds'],
            'umap_fit_seconds': manifest.get('umap_fit_seconds'),
            'transform_seconds': manifest.get('transform_seconds'),
            'peak_bytes': peak,
            'measured_bytes_per_point': measured,
        },
        message='Swapping in the new map; ingest into this dataset pauses briefly',
        placement='server',  # the swap and the ranks are database work
    )


async def _after_out_of_memory(job, output: Path, error: FitOutOfMemory) -> StepResult:
    """Retry the fit with fewer fit points. The fit was sized to use 75% of what the cap left after the
    vectors, so its memory per point was at least 1/0.75 of the estimate: use that, which makes the next
    fit 75% of this one. The raised estimate also sizes the next scheduled fit of this map."""
    checkpoint = job['checkpoint']
    kills = checkpoint.get('out_of_memory', 0) + 1
    try:
        plan = json.loads((output / 'plan.json').read_text())
    except (OSError, ValueError):
        plan = None
    floor = min(plan.get('rows', 0), MINIMUM_FIT_POINTS) if plan else 0
    if plan is None or kills >= POLICY.max_failures or plan['fit_size'] <= floor:  # cannot shrink below the floor
        raise error
    raised = math.ceil(plan['bytes_per_point'] / FIT_BUDGET_FRACTION)
    # The retry is 75% of what was attempted, not of what the raised cost would allow from RAM: the
    # killed attempt may itself have been held down by the growth bound (then RAM would allow more).
    retry_size = max(floor, int(plan['fit_size'] * FIT_BUDGET_FRACTION))
    dataset_id, field = job['dataset_id'], (job['args'] or {}).get('field')

    async def remember(connection):
        await connection.execute(
            'UPDATE embedding_map_state SET fit_bytes_per_point=$3 WHERE dataset_id=$1 AND field_name=$2',
            dataset_id,
            field,
            raised,
        )

    return StepResult(
        checkpoint={**checkpoint, 'bytes_per_point': raised, 'max_fit_size': retry_size, 'out_of_memory': kills},
        message=(
            f"The fit of {plan['fit_size']:,} points ran out of its memory allowance; retrying with "
            f"at most {retry_size:,}"
        ),
        publish=remember,
    )


def _copy_payload(ids, versions, coords) -> bytes:
    """Binary COPY rows (int4 sample_id, int8 version, float8 x, float8 y) for the staging table."""
    row = np.dtype(
        [
            ('fields', '>i2'),
            ('id_length', '>i4'),
            ('id', '>i4'),
            ('version_length', '>i4'),
            ('version', '>i8'),
            ('x_length', '>i4'),
            ('x', '>f8'),
            ('y_length', '>i4'),
            ('y', '>f8'),
        ],
    )
    rows = np.empty(len(ids), dtype=row)
    rows['fields'] = 4
    rows['id_length'] = 4
    rows['id'] = ids
    rows['version_length'] = 8
    rows['version'] = versions
    rows['x_length'] = 8
    rows['x'] = coords[:, 0]
    rows['y_length'] = 8
    rows['y'] = coords[:, 1]
    return b'PGCOPY\n\xff\r\n\x00' + struct.pack('>ii', 0, 0) + rows.tobytes() + struct.pack('>h', -1)


async def _take_gate(connection, dataset_id: int, *, exclusive: bool = True) -> None:
    """Take the dataset's lifecycle gate for the swap (exclusive) or the re-rank (shared).

    Only try-locks: a queued request would stall every writer behind it, and a try-lock fails against a
    queued conflicting writer, so a writer already waiting for the gate goes first. A writer that looked
    for a re-fit in the instant before the gate was taken (and saw none) queues just after: the re-fit
    then lets go (a savepoint) and tries again, so no writer sits out the swap behind it."""
    take = 'pg_try_advisory_xact_lock' if exclusive else 'pg_try_advisory_xact_lock_shared'
    deadline = time.monotonic() + GATE_WAIT_SECONDS
    while True:
        savepoint = connection.transaction()
        await savepoint.start()
        if await connection.fetchval(f'SELECT {take}(20261016, $1)', dataset_id):
            await asyncio.sleep(GATE_GRACE_SECONDS)
            if not await connection.fetchval(_GATE_WAITERS, dataset_id):
                await savepoint.commit()  # the lock stays with the transaction
                return
        await savepoint.rollback()  # lets go of the gate, if it was taken
        if time.monotonic() >= deadline:
            raise job_queue.QueueContended('Ingest kept the dataset busy; the new map is swapped in on retry')
        await asyncio.sleep(0.05)


# Writers queued for the dataset's lifecycle gate (not granted yet).
_GATE_WAITERS = """SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND classid = 20261016
                    AND objid = $1 AND objsubid = 2 AND NOT granted
                    AND database = (SELECT oid FROM pg_database WHERE datname = current_database()))"""


async def _publish(pool, job, store):
    dataset_id, field = _job_args(job)
    await _wait_for_backup(pool)  # the swap's write burst never overlaps the backup's dump
    checkpoint = job['checkpoint']
    output = store.work_dir(job['id']) / checkpoint.get('output', 'fit')
    ids, versions, coords = await asyncio.to_thread(
        lambda: (np.load(output / 'ids.npy'), np.load(output / 'versions.npy'), np.load(output / 'coords.npy')),
    )
    if not (len(ids) == len(versions) == len(coords) == checkpoint['rows']) or not np.isfinite(coords).all():
        raise MapRefitError('The fit outputs are incomplete; the current map is unchanged')
    if checkpoint.get('remote'):
        # A compute worker's rows must be exactly the exported ones, in their order: other ids would drop the
        # map's points below, and another order would put each point at another sample's place.
        from fridge.map_fit import read_export_ids  # here: importing fridge loads the whole SDK

        exported = await asyncio.to_thread(read_export_ids, store.work_dir(job['id']) / 'vectors.bin')
        if not (np.array_equal(ids, exported[0]) and np.array_equal(versions, exported[1])):
            raise MapRefitError("The compute worker's rows don't match the export; the current map is unchanged")
    payload = await asyncio.to_thread(_copy_payload, ids, versions, coords)
    budget = max(PUBLISH_MIN_SECONDS, len(ids) * PUBLISH_SECONDS_PER_POINT)
    timings: Dict[str, float] = {}
    loop = asyncio.get_running_loop()
    expired = False
    ending: list = []
    async with pool.acquire(timeout=5) as connection:
        server_pid = connection.get_server_pid()

        def expire():
            # The deadline is advertised to writers (X-Map-Swap-Seconds), so the swap must really end by
            # it: close the socket (the server notices within client_connection_check_interval, even
            # mid-statement) and end the server session from another connection, in case the closed
            # socket is not seen (a proxy in between). Either way the server rolls the swap back.
            nonlocal expired
            expired = True
            job_queue._terminate_connection(connection)
            ending.append(loop.create_task(_end_backend(pool, server_pid)))

        guard = None
        try:
            async with AsyncExitStack() as quiet, connection.transaction():
                started = time.monotonic()
                await connection.execute(
                    f"SET LOCAL statement_timeout = '{int(budget * 1000)}ms'; SET LOCAL lock_timeout = '5s'; "
                    "SET LOCAL idle_in_transaction_session_timeout = '60s'; "
                    "SET LOCAL client_connection_check_interval = '1s'",
                )
                await connection.execute('SELECT require_writable_dataset($1)', dataset_id)
                # Everything that does not need the gate happens first (staging the coordinates), and the
                # re-rank happens after it (the "rank" step), so ingest pauses only for the switch itself.
                await connection.execute(
                    """CREATE TEMP TABLE map_refit_stage (
                           sample_id integer PRIMARY KEY, vector_version bigint, x float8, y float8
                       ) ON COMMIT DROP""",
                )
                await connection.copy_to_table(
                    'map_refit_stage',
                    source=io.BytesIO(payload),
                    format='binary',
                    timeout=budget,
                )
                await connection.execute('ANALYZE map_refit_stage', timeout=budget)
                await connection.execute(
                    """CREATE TEMP TABLE map_refit_valid (sample_id integer PRIMARY KEY, x float8, y float8)
                       ON COMMIT DROP""",
                )
                timings['stage'] = time.monotonic() - started
                # While this session holds the gate, bounded writers tell their callers (409 + how long) to
                # retry until this deadline; the guard below ends the swap (rollback) if it reaches it.
                now = await connection.fetchval('SELECT extract(epoch FROM clock_timestamp())::float8')
                until = math.ceil(now + GATE_WAIT_SECONDS + budget)
                await connection.execute(f"SET LOCAL application_name = '{MAP_SWAP_APPLICATION} {until}'")
                guard = loop.call_later(GATE_WAIT_SECONDS + budget, expire)
                await _take_gate(connection, dataset_id)
                gated = time.monotonic()
                timings['gate_wait'] = gated - started - timings['stage']
                await connection.execute('SELECT pg_advisory_xact_lock(20261020, $1)', dataset_id)
                state = await connection.fetchrow(
                    'SELECT * FROM embedding_map_state WHERE dataset_id=$1 AND field_name=$2 FOR UPDATE',
                    dataset_id,
                    field,
                )
                if state is None or state['active_job_id'] != job['id']:
                    raise MapRefitError('Another re-fit took over this map; this one was not published')
                # Under the gate no vector can change: keep exactly the exported rows whose vector is
                # still the one exported (same row version) and whose sample still exists.
                valid = await connection.fetchval(
                    """WITH valid AS (
                           INSERT INTO map_refit_valid
                           SELECT st.sample_id, st.x, st.y FROM map_refit_stage st
                           JOIN embeddings e ON e.sample_id = st.sample_id AND e.field_name = $2
                            AND e.xmin::text::bigint = st.vector_version
                           JOIN samples s ON s.id = st.sample_id AND s.dataset_id = $1
                           RETURNING 1)
                       SELECT count(*) FROM valid""",
                    dataset_id,
                    field,
                    timeout=budget,
                )
                await connection.execute('ANALYZE map_refit_valid', timeout=budget)
                # Drop the field's ranks before moving its coordinates, so the coordinate trigger does not
                # move every rank row only for them to be rebuilt by the rank step.
                await connection.execute(
                    'DELETE FROM embedding_viz_rank WHERE dataset_id=$1 AND field_name=$2',
                    dataset_id,
                    field,
                    timeout=budget,
                )
                # Coordinates of samples that changed or arrived after the export (and any others not in it)
                # go; the delete trigger queues those whose vectors remain, for the new model.
                removed = await connection.fetchval(
                    """WITH removed AS (
                           DELETE FROM embedding_coordinates c USING samples s
                           WHERE s.id = c.sample_id AND s.dataset_id = $1 AND c.field_name = $2
                             AND NOT EXISTS (SELECT 1 FROM map_refit_valid v WHERE v.sample_id = c.sample_id)
                           RETURNING 1)
                       SELECT count(*) FROM removed""",
                    dataset_id,
                    field,
                    timeout=budget,
                )
                await connection.execute(
                    """INSERT INTO embedding_coordinates AS c (sample_id, field_name, x, y)
                       SELECT sample_id, $1, x, y FROM map_refit_valid ORDER BY sample_id
                       ON CONFLICT (sample_id, field_name) DO UPDATE SET x = EXCLUDED.x, y = EXCLUDED.y
                       WHERE (c.x, c.y) IS DISTINCT FROM (EXCLUDED.x, EXCLUDED.y)""",
                    field,
                    timeout=budget,
                )
                timings['coordinates'] = time.monotonic() - gated
                # Until the rank step has re-ranked the new map, readers draw it from its coordinates
                # (occupied_cells = 0 turns off the rank fast path everywhere). The rank rows are gone, but
                # the per-map point counter keeps counting the map's points: the coordinates just written, plus
                # (through the coordinate trigger's rank rows) points placed after this.
                await connection.execute(
                    """UPDATE embedding_viz_meta SET occupied_cells = 0, rank_stale = TRUE, total_points = $3,
                           updated_at = now()
                       WHERE dataset_id = $1 AND field_name = $2""",
                    dataset_id,
                    field,
                    valid,
                )
                points = valid
                published = await connection.fetchrow(
                    # model_id names the map's version; a re-fitted map keeps no model file.
                    """UPDATE embedding_map_state SET model_id=$3, artifact_location=NULL, artifact_checksum=NULL,
                           artifact_bytes=NULL, dimension=$4, fit_size=$5, refitted_at=clock_timestamp(),
                           points_at_refit=$6,
                           fit_bytes_per_point=COALESCE($7, fit_bytes_per_point)
                       WHERE dataset_id=$1 AND field_name=$2 RETURNING refitted_at""",
                    dataset_id,
                    field,
                    uuid.UUID(checkpoint['model_id']),
                    checkpoint['dimension'],
                    checkpoint['fit_size'],
                    points,
                    checkpoint.get('measured_bytes_per_point'),
                )
                timings['gate_held'] = time.monotonic() - gated
                result = {
                    'model_id': checkpoint['model_id'],
                    'refitted_at': published['refitted_at'].isoformat(),
                    'points': points,
                    'exported': checkpoint['rows'],
                    'fit_size': checkpoint['fit_size'],
                    'fit_peak_bytes': checkpoint.get('peak_bytes'),
                    'skipped_changed': checkpoint['rows'] - valid,
                    'removed_coordinates': removed,
                    'fit_seconds': checkpoint.get('fit_seconds'),
                    'umap_fit_seconds': checkpoint.get('umap_fit_seconds'),
                    'transform_seconds': checkpoint.get('transform_seconds'),
                    'publish_seconds': {name: round(value, 2) for name, value in timings.items()},
                }
                await _record_schedule_result(connection, job, result)
                from . import dataset_schedules  # imports this module

                await dataset_schedules.after_refit(connection, dataset_id, field, published['refitted_at'])
                # From the job-row fence to the COMMIT the job's own lease renewal waits (bounded by the lease),
                # so a renewal mid-commit on a slow disk does not roll the swap back.
                await quiet.enter_async_context(job_heartbeat.renewals_paused())
                await connection.execute("SET LOCAL lock_timeout = '2s'")
                # The swap commits with the job's next step, so the re-rank cannot be lost: a runner that
                # stops after this commit leaves the job running in its "rank" phase, retried on reclaim.
                ranking = await job_queue.checkpoint_locked(
                    connection,
                    job['id'],
                    job['lease_token'],
                    checkpoint={**checkpoint, 'phase': 'rank', 'result': result, 'uncancellable': UNCANCELLABLE},
                    message=RANK_MESSAGE,
                )
                # Only the COMMIT is left: never interrupt it, so a swap that made its deadline is not
                # reported as rolled back.
                if guard is not None:
                    guard.cancel()
                if expired:
                    raise TimeoutError('The swap reached its deadline')
        except BaseException as error:
            # A cancelled or timed-out statement must not wait on this socket; the server rolls back.
            job_queue._terminate_connection(connection)
            if expired:
                raise TimeoutError(
                    f'The new map was not swapped in within {GATE_WAIT_SECONDS + budget:.0f} s; the old map stays '
                    'and the swap is retried',
                ) from error
            raise
        finally:
            if guard is not None:
                guard.cancel()
            if ending:
                await asyncio.gather(*ending, return_exceptions=True)
    logger.info('Map re-fit %s swapped in %.1f s under the gate: %s', job['id'], timings['gate_held'], result)
    if state['artifact_location']:  # the model file of a map fitted before maps stopped keeping one
        await asyncio.to_thread(_quietly, store.delete, state['artifact_location'])
    await asyncio.to_thread(_quietly, store.remove_work, job['id'])
    return await _rank(pool, ranking, store)


async def _rank(pool, job, store):
    """Re-rank the new map for the scatter's fast path, after the swap released the gate.

    The gate is held shared (default ingest and new embeddings go on; deletes, update-mode writes, removed
    keys and field operations get the re-fit's 409 with its end) and `recompute_viz_rank(..., TRUE)` holds
    the dataset's rank fence 20261020, which coordinate writes wait for: projection stays paused (the map
    remains active until this commits), a vector change to a placed point is refused with a 409 after a
    few seconds, and other coordinate uploads likewise. It ranks every coordinate present, sets the exact
    point counter and grid, ends the re-fit's hold on projection, and finishes the job, in one transaction.
    The swap has already committed, so this step is not cancellable while it runs (a queued attempt
    can be); if it keeps failing, heal_unranked_maps queues a re-rank job, with a growing gap."""
    dataset_id, field = _job_args(job)
    checkpoint = job['checkpoint']
    result = dict(checkpoint.get('result') or {})
    budget = max(PUBLISH_MIN_SECONDS, (checkpoint.get('rows') or 0) * PUBLISH_SECONDS_PER_POINT)
    await _before_rank(pool, job, dataset_id, field)
    started = time.monotonic()
    async with pool.acquire(timeout=5) as connection:
        try:
            async with AsyncExitStack() as quiet, connection.transaction():
                await connection.execute(
                    f"SET LOCAL statement_timeout = '{int(budget * 1000)}ms'; "
                    f"SET LOCAL lock_timeout = '{int(budget)}s'; SET LOCAL client_connection_check_interval = '1s'",
                )
                # Named before the gate is taken, then again once it is: from there the statement's budget
                # (its statement_timeout) bounds the rest, plus finishing.
                now = await connection.fetchval('SELECT extract(epoch FROM clock_timestamp())::float8')
                until = math.ceil(now + GATE_WAIT_SECONDS + budget + 10)
                await connection.execute(f"SET LOCAL application_name = '{MAP_RANK_APPLICATION} {until}'")
                await _take_gate(connection, dataset_id, exclusive=False)
                now = await connection.fetchval('SELECT extract(epoch FROM clock_timestamp())::float8')
                until = math.ceil(now + budget + 10)
                await connection.execute(f"SET LOCAL application_name = '{MAP_RANK_APPLICATION} {until}'")
                ranked = await connection.fetchval(RANK_SQL, dataset_id, field, timeout=budget + 10)
                await connection.execute(
                    """UPDATE embedding_map_state SET active_job_id = NULL, active_since = NULL
                       WHERE dataset_id = $1 AND field_name = $2 AND active_job_id = $3""",
                    dataset_id,
                    field,
                    job['id'],
                )
                seconds = dict(result.get('publish_seconds') or {})
                seconds['ranks_after_gate'] = round(time.monotonic() - started, 2)
                points = result.get('points', ranked)
                result.update(ranked=ranked, publish_seconds=seconds, points=points)
                await _record_schedule_result(connection, job, result)
                await quiet.enter_async_context(job_heartbeat.renewals_paused())  # fence to COMMIT, as in the swap
                await connection.execute("SET LOCAL lock_timeout = '2s'")
                await job_queue.finish_locked(
                    connection,
                    job['id'],
                    job['lease_token'],
                    result=result,
                    message=(
                        f'Map re-fitted: {points:,} points'
                        if checkpoint.get('result')
                        else f'Map ranked: {ranked:,} points'
                    ),
                    done=points,
                    total=points,
                )
        except BaseException:
            job_queue._terminate_connection(connection)
            raise
    logger.info('Map job %s ranked %s points in %.1f s', job['id'], ranked, time.monotonic() - started)
    return StepResult(committed=True, result=result)


async def _before_rank(pool, job, dataset_id: int, field: str) -> None:
    """Before the rank writes: never during a backup (contention, retried); room on the database volume
    for the rebuild, by the API's full re-rank rule (else a failure that wrote nothing); and projection
    paused for the map, as during a re-fit (a re-fit's own rank step already holds it)."""
    await _wait_for_backup(pool)
    async with pool.acquire(timeout=5) as connection:
        try:
            await require_free_space(
                connection,
                await estimate_viz_rank_bytes(connection, dataset_id, field),
                f'the re-rank of {field} on dataset {dataset_id}',
            )
        except HTTPException as error:
            raise MapRefitError(str(error.detail)) from error
        await connection.execute(
            """UPDATE embedding_map_state SET active_job_id = $3, active_since = clock_timestamp()
               WHERE dataset_id = $1 AND field_name = $2 AND active_job_id IS DISTINCT FROM $3""",
            dataset_id,
            field,
            job['id'],
        )


async def submit_rank(connection, dataset_id: int, field: str) -> Tuple[Dict[str, Any], bool]:
    """Queue a re-rank of a map left unranked (a re-fit whose rank step failed), or return the map job
    already queued or running for it."""
    await connection.execute(
        'SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
        json.dumps([KIND, dataset_id, field]),
    )
    existing = await unfinished_job(connection, dataset_id, field)
    if existing is not None:
        return existing, False
    job = await job_queue.enqueue_job(
        connection,
        KIND,
        dataset_id=dataset_id,
        args={'field': field, 'schedule_id': None, 'window_end': None, 'rank_only': True},
        placement='server',  # re-ranking is database work
        lane='maintenance',
    )
    points = await current_points(connection, dataset_id, field)
    job = dict(
        await connection.fetchrow(
            'UPDATE jobs SET checkpoint=$2 WHERE id=$1 RETURNING *',
            job['id'],
            {'phase': 'rank', 'rows': points, 'uncancellable': UNCANCELLABLE},
        ),
    )
    return job, True


async def heal_unranked_maps(pool) -> int:
    """Queue a re-rank for every re-fitted map left without ranks (its re-fit's rank step failed for
    good): until ranked, its scatter takes the slow path. An empty map (no points, not stale) is left
    alone. Run by the job service every 10 minutes. Returns how many.

    A map whose heal jobs failed or were cancelled since its re-fit waits HEAL_BACKOFF_HOURS after the
    last one, doubling with each (at most HEAL_BACKOFF_MAX_HOURS): a rank that cannot succeed is not
    retried every few minutes, and cancelling a queued heal job stops it for a while."""
    async with pool.acquire(timeout=1) as connection:
        rows = await connection.fetch(
            """WITH ended AS (
                   SELECT j.dataset_id, j.args->>'field' AS field_name, count(*) AS attempts,
                          max(j.finished_at) AS last
                   FROM jobs j JOIN embedding_map_state s
                     ON s.dataset_id = j.dataset_id AND s.field_name = j.args->>'field'
                   WHERE j.kind = $1 AND j.args ? 'rank_only' AND j.state IN ('failed', 'cancelled')
                     AND j.finished_at > s.refitted_at
                   GROUP BY 1, 2)
               SELECT m.dataset_id, m.field_name FROM embedding_viz_meta m
               JOIN embedding_map_state s USING (dataset_id, field_name)
               JOIN datasets d ON d.id = m.dataset_id AND NOT d.deleting
               LEFT JOIN ended e USING (dataset_id, field_name)
               WHERE m.occupied_cells = 0 AND (m.total_points > 0 OR m.rank_stale) AND s.model_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind = $1 AND j.dataset_id = m.dataset_id
                                 AND j.args->>'field' = m.field_name AND j.state IN ('queued', 'running'))
                 AND (e.attempts IS NULL OR e.last + make_interval(hours => LEAST($3::float8,
                        $2::float8 * power(2, e.attempts - 1))::int) <= clock_timestamp())""",
            KIND,
            HEAL_BACKOFF_HOURS,
            HEAL_BACKOFF_MAX_HOURS,
        )
    queued = 0
    for row in rows:
        async with pool.acquire(timeout=1) as connection, connection.transaction():
            _, created = await submit_rank(connection, row['dataset_id'], row['field_name'])
        queued += created
    if queued:
        logger.warning('Queued %s re-rank job(s) for maps a re-fit left unranked', queued)
    return queued


async def _end_backend(pool, pid: int) -> None:
    try:
        async with pool.acquire(timeout=5) as other:
            await other.fetchval('SELECT pg_terminate_backend($1)', pid, timeout=5)
    except Exception:
        logger.warning('Could not end the map swap session %s; it ends when the server notices the closed socket', pid)


def _quietly(operation, *args) -> None:
    try:
        operation(*args)
    except OSError:
        logger.warning('Map artifact cleanup failed; the sweep retries it', exc_info=True)


def handler(pool, store: Optional[LocalArtifactStore] = None, fits_here: bool = True):
    """`fits_here`: this runner takes compute work, so it fits too; otherwise compute workers do (start and export
    then neither measure this machine nor say the fit is running)."""
    store = store or LocalArtifactStore()
    steps = {'fit': _fit, 'publish': _publish, 'rank': _rank}

    async def measure(job):
        if job['lane'] != 'maintenance':
            raise MapRefitError('Map re-fits belong to the maintenance lane')
        phase = job['checkpoint'].get('phase', 'start')
        if phase == 'start':
            return await _start(pool, job, store, fits_here)
        if phase == 'export':
            return await _export(pool, job, store, fits_here)
        return await steps[phase](pool, job, store)

    return measure


def remote_output_limit(job, name: str) -> int:
    """The largest a compute worker's output for this fit can be, from the exported rows."""
    if name == 'manifest.json':
        return _MANIFEST_BYTES
    columns, item = _REMOTE_ARRAYS[name]
    return _NPY_HEADER_BYTES + job['checkpoint']['rows'] * columns * item


def remote_fit_checkpoint(job, staged: Path, result: Any) -> Dict[str, Any]:
    """The checkpoint that hands a compute worker's fit back to the server, after checking its staged outputs:
    each one is there with the sha256 the worker reported (`result['checksums']`), the arrays read without
    pickles and have one row per exported vector, and the manifest agrees. Raises ValueError otherwise."""
    checkpoint = job['checkpoint']
    rows = checkpoint['rows']
    checksums = (result or {}).get('checksums') if isinstance(result, dict) else None
    if not isinstance(checksums, dict):
        raise ValueError("The result needs each output's sha256 in 'checksums'")
    for name in REMOTE_OUTPUTS:
        path = staged / name
        if not path.is_file():
            raise ValueError(f"The fit's output {name} was not uploaded")
        if sha256(path) != checksums.get(name):
            raise ValueError(f"The uploaded {name} doesn't match its sha256")
    for name, (columns, _) in _REMOTE_ARRAYS.items():
        try:
            array = np.load(staged / name, allow_pickle=False)
        except ValueError as error:
            raise ValueError(f'{name} is not a plain array: {error}') from None
        expected = (rows,) if columns == 1 else (rows, columns)
        kind = 'f' if name == 'coords.npy' else 'i'
        if array.shape != expected or array.dtype.kind != kind:
            raise ValueError(f'{name} is {array.dtype} {array.shape}; expected {expected} of {kind}')
    manifest = json.loads((staged / 'manifest.json').read_text())
    if not isinstance(manifest, dict) or manifest.get('rows') != rows:
        raise ValueError(f"The manifest doesn't describe {rows} rows")

    def seconds(name):
        value = manifest.get(name)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None

    fit_size = manifest.get('fit_size')
    return {
        **checkpoint,
        'phase': 'publish',
        'output': staged.name,
        'remote': True,
        'fit_size': fit_size if isinstance(fit_size, int) and 0 < fit_size <= rows else rows,
        'fit_seconds': seconds('fit_seconds'),
        'umap_fit_seconds': seconds('umap_fit_seconds'),
        'transform_seconds': seconds('transform_seconds'),
    }


async def expire_unclaimed_fits(pool) -> int:
    """End each re-fit whose fit no compute worker claimed for UNCLAIMED_FIT_HOURS: the old
    map stays, and projection resumes with its model (a map's re-fit is active only while its job is queued or
    running). Returns how many."""
    reason = f'No compute worker took the fit within {UNCLAIMED_FIT_HOURS} hours; the current map is unchanged'
    async with pool.acquire(timeout=5) as connection:
        stale = await connection.fetch(
            f"""SELECT {job_queue.JOB_COLUMNS} FROM {job_queue.JOB_TABLE}
                WHERE kind = $1 AND state = 'queued' AND placement = 'compute' AND checkpoint->>'phase' = 'fit'
                  AND updated_at < clock_timestamp() - $2 * interval '1 hour'""",
            KIND,
            UNCLAIMED_FIT_HOURS,
            timeout=30,
        )
    ended = 0
    for job in stale:
        cancelled = await job_queue.cancel_job(pool, job['id'])
        if cancelled is None or cancelled['state'] != 'cancelled':
            continue  # claimed meanwhile
        async with pool.acquire(timeout=5) as connection:
            await connection.execute('UPDATE jobs SET message = $2 WHERE id = $1', job['id'], f'Skipped: {reason}')
            await _record_schedule_result(connection, dict(job), {'skipped': reason})
        ended += 1
    return ended


async def sweep(pool, store: Optional[LocalArtifactStore] = None) -> int:
    """Delete work folders of finished re-fits and model files that are neither current nor owned by an
    unfinished re-fit (replaced models, failed or cancelled fits, deleted datasets). Returns the count.
    Files are listed before the database is read, so a re-fit that starts meanwhile keeps its files."""
    store = store or LocalArtifactStore()
    work = store.work_jobs()
    models = list(store.models())
    if not work and not models:
        return 0
    async with pool.acquire(timeout=1) as connection:
        # One statement, one snapshot: a publish committing meanwhile moves its model from "owned by an
        # unfinished re-fit" to "current" atomically, so the model is in one set or the other.
        rows = await connection.fetch(
            """SELECT NULL::uuid AS job_id, model_id FROM embedding_map_state WHERE model_id IS NOT NULL
               UNION ALL
               SELECT id, (checkpoint->>'model_id')::uuid FROM jobs
               WHERE kind=$1 AND state IN ('queued','running')""",
            KIND,
        )
    owned = {row['job_id'] for row in rows if row['job_id'] is not None}
    keep = {row['model_id'] for row in rows if row['model_id'] is not None}
    removed = 0
    for job_id in work:
        if job_id not in owned:
            await asyncio.to_thread(_quietly, store.remove_work, job_id)
            removed += 1
    for key, model_id in models:
        if model_id not in keep:
            await asyncio.to_thread(_quietly, store.delete, key)
            removed += 1
    return removed
