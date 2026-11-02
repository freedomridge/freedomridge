"""The scheduled map re-fit leaves KEEP_FREE_CORE_FRACTION of the CPU cores entirely to the database and the app, and
runs on one CPU of each other core (2026-10-22 soak: on every CPU it took load to 30-40 and ingest batches from ~1-2 s
to 10-20 s)."""

import asyncio
import json
import logging
import os
import shutil
import sys

import pytest

from backend import map_refit


@pytest.mark.parametrize(
    ('cores', 'smt', 'free'),
    [
        (16, 2, [2, 6, 10, 14]),  # SMT: CPUs n and n+16 share core n; two free cores per L3 half (0-7, 8-15)
        (32, 2, [2, 6, 11, 16, 20, 25, 29]),
        (8, 1, [2, 6]),
        (4, 1, [2]),
        (2, 2, [1]),
        (1, 2, []),  # a single core is never taken from the fit
    ],
)
def test_the_fit_leaves_a_fifth_of_the_cores_free(monkeypatch, cores, smt, free):
    monkeypatch.setattr(os, 'sched_getaffinity', lambda pid: set(range(cores * smt)))
    monkeypatch.setattr(map_refit, '_core_of', lambda cpu: str(cpu % cores))
    expected = [core for core in range(cores) if core not in free]  # the first CPU of each other core
    assert map_refit.fit_cpus() == expected


def run_child(monkeypatch, tmp_path):
    """run_fit with a stand-in for the interpreter that runs fridge.map_fit: the stand-in records the CPUs it may run
    on, then exits 0. Returns those CPUs."""
    seen = tmp_path / 'cpus.json'
    child = tmp_path / 'child'
    record = f'json.dump(sorted(os.sched_getaffinity(0)), open({str(seen)!r}, "w"))'
    child.write_text(f'#!{sys.executable}\nimport json, os\n{record}\n')
    child.chmod(0o755)
    monkeypatch.setattr(sys, 'executable', str(child))
    asyncio.run(map_refit.run_fit([], tmp_path / 'in', tmp_path / 'out', tmp_path / 'log', 1024**3))
    return json.loads(seen.read_text())


def siblings(cpu):
    """The CPUs sharing a core with `cpu`, read from sysfs here (not through map_refit), e.g. '0,16' or '0-1'."""
    path = f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list'
    if not os.path.exists(path):
        pytest.skip('no CPU topology in sysfs')
    cpus: set = set()
    for part in open(path).read().strip().split(','):
        first, _, last = part.partition('-')
        cpus.update(range(int(first), int(last or first) + 1))
    return cpus


def test_the_fit_child_runs_on_one_cpu_of_each_of_its_cores(monkeypatch, tmp_path):
    cpus = run_child(monkeypatch, tmp_path)
    assert cpus == map_refit.fit_cpus()
    for cpu in cpus:  # no fit thread ever shares a core: the free share is whole cores
        assert not (siblings(cpu) - {cpu}) & set(cpus), (cpu, siblings(cpu))
    cores = {frozenset(siblings(cpu)) for cpu in os.sched_getaffinity(0)}
    if len(cores) > 1:
        assert len(cpus) < len(cores)


def test_without_taskset_the_fit_runs_unpinned(monkeypatch, tmp_path, caplog):
    which = shutil.which
    monkeypatch.setattr(shutil, 'which', lambda name, *args, **kwargs: None if name == 'taskset' else which(name))
    with caplog.at_level(logging.WARNING, logger='backend.map_refit'):
        cpus = run_child(monkeypatch, tmp_path)
    assert cpus == sorted(os.sched_getaffinity(0))
    assert [r.message for r in caplog.records if 'taskset' in r.message] == [
        'Map re-fit: taskset is not installed, so the fit runs on every CPU and keeps none free',
    ]
