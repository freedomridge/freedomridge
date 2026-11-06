"""Map re-fits without systemd, as in the Docker job-runner: the fit shares this process's own memory limit."""

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from backend import map_refit

GB = 1024**3
ROOT = Path(__file__).resolve().parents[2]


def fake_cgroup(tmp_path, monkeypatch, limit=str(4 * GB), swap='0'):
    """No systemd, and this process's cgroup is a folder of the files map_refit reads: 1 GB used, of which
    0.5 GB is page cache."""
    (tmp_path / 'memory.max').write_text(f'{limit}\n')
    (tmp_path / 'memory.current').write_text(f'{GB}\n')
    (tmp_path / 'memory.stat').write_text(f'anon {GB // 2}\nactive_file {GB // 4}\ninactive_file {GB // 4}\n')
    if swap is not None:
        (tmp_path / 'memory.swap.max').write_text(f'{swap}\n')
    monkeypatch.setattr(map_refit, '_cgroup_folder', lambda pid: str(tmp_path))
    monkeypatch.setattr(map_refit, 'host_swap', lambda: 2 * GB)

    def no_systemd(allowance):
        raise map_refit.ContainmentUnavailable('systemd-run is not installed')

    monkeypatch.setattr(map_refit, '_systemd_scope', no_systemd)


def test_without_systemd_the_fit_shares_this_process_memory_limit(tmp_path, monkeypatch):
    fake_cgroup(tmp_path, monkeypatch)
    assert map_refit.containment(GB) == []
    # The page cache counts as free, as the kernel reclaims it before an out-of-memory kill.
    assert map_refit.cgroup_headroom() == 4 * GB - GB + GB // 2
    # The limit bounds the fit, not the host's RAM.
    monkeypatch.setattr(map_refit, 'host_memory', lambda: (64 * GB, 60 * GB))
    assert map_refit.fit_allowance() == (4 * GB - GB + GB // 2, 64 * GB)


@pytest.mark.parametrize(
    ('limit', 'swap', 'problem'),
    [
        ('max', '0', 'set JOB_RUNNER_MEMORY_LIMIT'),  # no limit to share
        (str(4 * GB), 'max', 'allows swap'),  # the fit would swap instead of stopping at the limit
        (str(4 * GB), None, 'allows swap'),  # swap is not accounted per cgroup, and the host has some
    ],
)
def test_without_systemd_or_a_safe_limit_the_fit_is_refused_with_the_reason(
    tmp_path,
    monkeypatch,
    limit,
    swap,
    problem,
):
    fake_cgroup(tmp_path, monkeypatch, limit=limit, swap=swap)
    with pytest.raises(map_refit.ContainmentUnavailable) as error:
        map_refit.containment(GB)
    assert 'systemd-run is not installed' in str(error.value) and problem in str(error.value)


def test_a_limit_that_allows_swap_is_safe_on_a_host_without_swap(tmp_path, monkeypatch):
    fake_cgroup(tmp_path, monkeypatch, swap='max')
    monkeypatch.setattr(map_refit, 'host_swap', lambda: 0)
    assert map_refit.containment(GB) == []


# Run in a process whose own cgroup has a 400 MB limit: a fit that stays within it, one that runs out (it makes
# itself the kernel's first choice, as fridge.map_fit does), and one killed for another reason.
RUN_FITS = """
import asyncio
import sys
from pathlib import Path

from backend import map_refit

work = Path(sys.argv[1])
children = {
    'within': 'x = b"1" * (50 * 1024**2); import time; time.sleep(3)',
    'beyond': 'open("/proc/self/oom_score_adj", "w").write("1000"); x = b"1" * (1024**3)',
    'killed': 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)',
}


async def fit(name):
    (work / f'{name}.py').write_text(children[name])
    prefix = ['bash', '-c', f'exec {sys.executable} {work / name}.py', '--']
    try:
        peak = await map_refit.run_fit(prefix, work / 'in', work / name, work / f'{name}.log', 1, shared=True)
        print(name, 'succeeded', peak)
    except map_refit.FitOutOfMemory:
        print(name, 'out-of-memory')
    except ConnectionError:
        print(name, 'killed-otherwise')


for name in children:
    asyncio.run(fit(name))
"""


def test_a_fit_sharing_a_real_memory_limit_is_measured_and_its_out_of_memory_kill_detected(tmp_path):
    map_refit._systemd_scope(400 * 1024**2)  # this machine applies a scope's memory limit
    # A scope of its own name: a container's cgroup is not a systemd-run `run-*.scope` either, so the fit
    # is measured only by reading the shared cgroup.
    limited = ['systemd-run', '--user', '--scope', '--quiet', '--unit', f'fridge-shared-limit-test-{uuid.uuid4().hex}']
    limited += ['-p', f'MemoryMax={400 * 1024**2}', '-p', 'MemorySwapMax=0', '--']
    finished = subprocess.run(
        [*limited, sys.executable, '-c', RUN_FITS, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
        check=True,
    )
    results = dict(line.split(' ', 1) for line in finished.stdout.splitlines())
    state, peak = results['within'].split()
    assert state == 'succeeded' and int(peak) > 50 * 1024**2  # sampled from the shared cgroup
    assert results['beyond'] == 'out-of-memory'  # and the runner itself survived it
    assert results['killed'] == 'killed-otherwise'  # SIGKILL without an out-of-memory kill
