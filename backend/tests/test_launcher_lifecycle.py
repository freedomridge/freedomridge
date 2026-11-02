"""Standalone launcher lifecycle tests never connect to a database or use application ports."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend import launch

ROOT = Path(__file__).resolve().parents[2]


def wait_ready(path, process):
    deadline = time.monotonic() + 10
    while not path.exists():
        assert process.poll() is None
        assert time.monotonic() < deadline, 'Test child did not start'
        time.sleep(0.02)


def process_state(identifier):
    """The process's state letter, or None once it has been reaped.

    It can vanish between checks, or between opening and reading its stat file (ProcessLookupError)."""
    try:
        return Path(f'/proc/{identifier}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return None


def group_members(group):
    members = []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            status = (process / 'stat').read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(status[2]) == group and status[0] != 'Z':
            members.append(int(process.name))
    return members


@pytest.mark.parametrize('mode', ['grace_expired', 'second_interrupt', 'parent_killed', 'child_stopped_parent_killed'])
def test_stubborn_children_cannot_outlive_launcher(tmp_path, mode):
    ready = tmp_path / 'ready'
    program = (
        'import os,signal,subprocess,sys,time; '
        'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
        'child=subprocess.Popen([sys.executable,"-c",'
        '"import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(90)"]); '
        f'open({str(ready)!r},"w").write(str(os.getpid())+" "+str(child.pid)); time.sleep(90)'
    )
    environment = {
        **os.environ,
        'FRIDGE_RUNNER_ENABLED': 'false',
        'FRIDGE_SHUTDOWN_SECONDS': '0.3' if mode == 'grace_expired' else '10',
    }
    with (tmp_path / 'launcher.log').open('wb') as log:
        process = subprocess.Popen(
            [sys.executable, '-m', 'backend.launch', '--', sys.executable, '-c', program],
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=log,
        )
        child_pid = None
        try:
            wait_ready(ready, process)
            child_pid, grandchild_pid = map(int, ready.read_text().split())
            members = set(group_members(child_pid)) | {child_pid, grandchild_pid}
            started = time.monotonic()
            if mode in ('parent_killed', 'child_stopped_parent_killed'):
                if mode == 'child_stopped_parent_killed':
                    os.kill(child_pid, signal.SIGSTOP)
                process.kill()
            else:
                process.send_signal(signal.SIGINT)
                if mode == 'second_interrupt':
                    time.sleep(0.2)
                    process.send_signal(signal.SIGINT)
            process.wait(timeout=3)
            assert process.returncode != 0
            assert time.monotonic() - started < 2
            deadline = time.monotonic() + 2
            for identifier in members:
                while process_state(identifier) not in (None, 'Z'):
                    assert time.monotonic() < deadline, f'Orphaned managed process {identifier}'
                    time.sleep(0.02)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize('parent_alive', [True, False])
def test_guard_closes_unused_descriptors_and_never_starts_for_a_dead_parent(parent_alive):
    lifeline, writer = os.pipe()
    process = None
    try:
        if not parent_alive:
            os.close(writer)
        process = subprocess.Popen(
            [sys.executable, '-m', 'backend.process_guard', str(lifeline), '--', sys.executable, '-c', 'print("ran")'],
            cwd=ROOT,
            pass_fds=(lifeline,),
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, stderr.decode()
        assert stdout == (b'ran\n' if parent_alive else b'')
    finally:
        os.close(lifeline)
        if parent_alive:
            os.close(writer)
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            deadline = time.monotonic() + 2
            while group_members(process.pid):
                if time.monotonic() >= deadline:
                    os.killpg(process.pid, signal.SIGKILL)
                    pytest.fail('A parent-liveness guard leaked after its lifeline closed')
                time.sleep(0.02)


@pytest.mark.parametrize('value', ['false', '0', 'no', 'off', ' FALSE ', ' Off '])
def test_common_false_values_disable_runner(monkeypatch, value):
    commands = []

    class Process:
        pid = 12345
        returncode = 0

        def __init__(self, command, **options):
            commands.append(command)
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else 0

        def wait(self, **options):
            return 0

    monkeypatch.setenv('FRIDGE_RUNNER_ENABLED', value)
    monkeypatch.setattr(subprocess, 'Popen', Process)
    monkeypatch.setattr(launch, 'terminate_group', lambda *_: None)
    launch.supervise(['test-api'])
    assert len(commands) == 1


def test_inherited_ignored_hangup_is_preserved(tmp_path):
    ready = tmp_path / 'ready'
    program = f'import time; open({str(ready)!r},"w").write("ready"); time.sleep(90)'
    launcher = (
        'import signal; signal.signal(signal.SIGHUP,signal.SIG_IGN); '
        'from backend.launch import main; raise SystemExit(main())'
    )
    with (tmp_path / 'nohup.log').open('wb') as log:
        process = subprocess.Popen(
            [sys.executable, '-c', launcher, '--', sys.executable, '-c', program],
            cwd=ROOT,
            env={**os.environ, 'FRIDGE_RUNNER_ENABLED': 'false'},
            stdout=log,
            stderr=log,
        )
        try:
            wait_ready(ready, process)
            process.send_signal(signal.SIGHUP)
            time.sleep(0.4)
            assert process.poll() is None, 'The launcher must preserve nohup behavior'
            process.terminate()
            process.wait(timeout=3)
            assert process.returncode == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)


@pytest.mark.parametrize('module_available', [True, False])
def test_shell_launcher_checks_and_uses_the_same_python(tmp_path, module_available):
    binary = tmp_path / 'bin'
    binary.mkdir()
    calls = tmp_path / 'python-calls'
    python = binary / 'python3'
    python.write_text(
        '#!/bin/sh\n'
        f'printf "%s\\n" "$*" >> {str(calls)!r}\n'
        f'if [ "$1" = "-c" ]; then exit {0 if module_available else 1}; fi\n'
        'exit 0\n',
    )
    python.chmod(0o755)
    if not module_available:
        executable = binary / 'uvicorn'
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
    tailscale = binary / 'tailscale'
    tailscale.write_text('#!/bin/sh\nexit 0\n')
    tailscale.chmod(0o755)
    result = subprocess.run(
        ['bash', str(ROOT / 'scripts/run_backend.sh')],
        env={
            **os.environ,
            'ROOT_DIR': str(tmp_path),
            'PATH': str(binary) + ':/usr/bin:/bin',
            'FRIDGE_LOG_FILE': '',
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == (0 if module_available else 127)
    arguments = calls.read_text().splitlines()
    assert arguments[0] == '-c import uvicorn'
    if module_available:
        assert arguments[1].startswith('-m backend.launch -- python3 -m uvicorn backend.main:app ')
    else:
        assert len(arguments) == 1
