"""Split the backend tests into shards that run side by side, each against its own database.

FRIDGE_TEST_SHARD=K/N makes pytest run shard K of N (backend/tests/conftest.py). A shard is a set of whole test
files, so a module's fixtures stay in one process. Every shard computes the same split from the same collected files
and backend/tests/shard_timings.json (each file's seconds in a full run): largest file first, into the shard with the
least time so far. A file missing from the timings counts as the median file. Stale timings only unbalance the
shards; every collected file still runs in exactly one of them.

Refresh the timings after a full run with `pytest --junitxml=<file>`:
    python -m scripts.backend_test_shards <file>
"""

import json
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

TIMINGS = Path(__file__).resolve().parent.parent / 'backend' / 'tests' / 'shard_timings.json'


def parse_shard(spec: str) -> tuple[int, int]:
    """'K/N' -> (K, N), 1 <= K <= N."""
    match = re.fullmatch(r'([1-9][0-9]*)/([1-9][0-9]*)', spec.strip())
    if not match or int(match[1]) > int(match[2]):
        raise ValueError(f'FRIDGE_TEST_SHARD must be K/N with 1 <= K <= N, not {spec!r}')
    return int(match[1]), int(match[2])


def split(files: list[str], count: int, timings: dict[str, float]) -> list[list[str]]:
    """The files of each of `count` shards."""
    default = statistics.median(timings.values()) if timings else 1.0
    shards: list[list[str]] = [[] for _ in range(count)]
    totals = [0.0] * count
    for name in sorted(set(files), key=lambda f: (-timings.get(f, default), f)):
        lightest = min(range(count), key=lambda i: (totals[i], i))
        shards[lightest].append(name)
        totals[lightest] += timings.get(name, default)
    return shards


def load_timings() -> dict[str, float]:
    return json.loads(TIMINGS.read_text())


def timings_from_junit(path: str) -> dict[str, float]:
    """Seconds per test file from a pytest JUnit XML report of the backend suite."""
    files: dict[str, float] = defaultdict(float)
    for case in ET.parse(path).iter('testcase'):
        # A module skipped as a whole has an empty classname and the module as its name.
        parts = (case.get('classname') or case.get('name') or '').split('.')
        if parts[:2] != ['backend', 'tests'] or len(parts) < 3:
            raise ValueError(f'not a backend test: {case.get("classname")!r} {case.get("name")!r}')
        files[f'backend/tests/{parts[2]}.py'] += float(case.get('time') or 0)
    return {name: round(seconds, 1) for name, seconds in sorted(files.items())}


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('Usage: python -m scripts.backend_test_shards <pytest JUnit XML of a full backend run>')
    timings = timings_from_junit(sys.argv[1])
    TIMINGS.write_text(json.dumps(timings, indent=0, sort_keys=True) + '\n')
    print(f'{TIMINGS}: {len(timings)} files, {sum(timings.values()) / 60:.1f} minutes')
