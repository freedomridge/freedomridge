"""Atomic media writes; approximate byte accounting and eviction stay off the serving path.

When enabled, each process counts bytes written (including replacements). Maintenance
merges those counts every 15 minutes and walks the cache only above the estimated
budget. Workers coordinate only maintenance, with a non-blocking flock; readers and
writers never acquire that lock. The existing hourly worker prune reconciles idle or
unaccounted files. This is a soft disk budget, not a limit on media that can be served.
"""

import fcntl
import json
import logging
import os
import queue
import threading
import time
import uuid
from pathlib import Path

from .cache_pruning import prune

log = logging.getLogger(__name__)
_MAINTENANCE_SECONDS = 15 * 60.0


def _maintenance_enabled() -> bool:
    return (
        os.getenv('FRIDGE_ENV', 'development').strip().lower() in ('production', 'prod')
        or os.getenv('DEMO_MODE', 'false').lower() == 'true'
        or 'FRIDGE_CACHE_MAX_BYTES' in os.environ
    )


class _Budget:
    def __init__(self, roots: tuple[str, ...], budget: int):
        self.roots = roots
        self.budget = budget
        self.writes: queue.SimpleQueue[int] = queue.SimpleQueue()
        self.interval = max(0.01, float(os.getenv('FRIDGE_CACHE_MAINTENANCE_SECONDS', str(_MAINTENANCE_SECONDS))))

    def run(self) -> None:
        pending = 0
        next_check = time.monotonic() + self.interval
        while True:
            try:
                pending += self.writes.get(timeout=max(0, next_check - time.monotonic()))
            except queue.Empty:
                pass
            if time.monotonic() < next_check:
                continue
            try:
                if self.reconcile(pending) is not None:
                    pending = 0
            except OSError:
                log.exception('Media cache maintenance failed')
            next_check = time.monotonic() + self.interval

    def reconcile(self, written: int) -> int | None:
        """Merge incremental counts; scan only over budget, rate-limited across workers."""
        Path(self.roots[0]).mkdir(parents=True, exist_ok=True)
        with open(Path(self.roots[0]) / '.cache-budget.lock', 'a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            lock.seek(0)
            try:
                state = json.load(lock)
                previous = float(state['monotonic'])
                total = max(0, int(state['bytes']))
            except (ValueError, TypeError, KeyError):
                # Legacy wall-clock timestamp, empty file or interrupted metadata write.
                # The hourly worker prune still accounts for pre-existing/idle files.
                previous, total = -self.interval, 0
            now = time.monotonic()
            total += written
            if total > self.budget and (now - previous >= self.interval or now < previous):
                # Writes racing this scan remain queued and may be counted twice;
                # that conservative estimate is corrected by the next real scan.
                total = prune(self.roots, self.budget)
                previous = time.monotonic()
            lock.seek(0)
            lock.truncate()
            json.dump({'monotonic': previous, 'bytes': total}, lock)
            return total


_budgets: dict[tuple, _Budget] = {}


def _track_write(roots: tuple[str, ...], budget: int, size: int) -> None:
    if not _maintenance_enabled():
        return
    # pid keeps a forked worker from reusing its parent's thread/queue. setdefault
    # publishes one owner under the GIL; there is no request-side global lock.
    key = (os.getpid(), roots, budget)
    tracker = _budgets.get(key)
    if tracker is None:
        candidate = _Budget(roots, budget)
        tracker = _budgets.setdefault(key, candidate)
        if tracker is candidate:
            threading.Thread(target=tracker.run, name='media-cache-maintenance', daemon=True).start()
    tracker.writes.put(size)


def write_cache(path: str, body: bytes, roots: tuple[str, ...], budget: int) -> None:
    """Constant work per write, independent of cache size; no scan or eviction lock."""
    if len(body) > budget or budget <= 0:
        return
    roots = tuple(dict.fromkeys(os.path.realpath(root) for root in roots))
    target = Path(path)
    if not any(target.resolve().is_relative_to(root) for root in roots):
        raise ValueError("Cache target must be inside a cache root")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f'{target.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('wb') as output:
            output.write(body)
        os.replace(temporary, target)
        _track_write(roots, budget, len(body))
    finally:
        temporary.unlink(missing_ok=True)


def read_cache(path: str) -> bytes | None:
    """Open before returning bytes so eviction cannot race a deferred FileResponse."""
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None
