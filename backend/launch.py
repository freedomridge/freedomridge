"""Supervise one API master and one local runner, including their child processes."""

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from .job_service import positive_seconds

logger = logging.getLogger(__name__)


def terminate_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def supervise(api_command):
    grace = positive_seconds('FRIDGE_SHUTDOWN_SECONDS', 30)
    stopping = threading.Event()
    forcing = threading.Event()
    interrupts = 0

    def request_stop(signum, _frame):
        nonlocal interrupts
        if signum == signal.SIGINT:
            interrupts += 1
            if interrupts > 1:
                forcing.set()
        stopping.set()

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        if signum == signal.SIGHUP and signal.getsignal(signum) == signal.SIG_IGN:
            continue
        previous[signum] = signal.signal(signum, request_stop)
    lifeline, parent_alive = os.pipe()

    def start_child(command):
        return subprocess.Popen(
            [sys.executable, '-m', 'backend.process_guard', str(lifeline), '--', *command],
            start_new_session=True,
            pass_fds=(lifeline,),
        )

    children = []
    result = 0
    try:
        api = start_child(api_command)
        children.append(api)
        runner_enabled = os.getenv('FRIDGE_RUNNER_ENABLED', 'true').strip().lower() not in ('false', '0', 'no', 'off')
        runner = None
        runner_started = 0.0
        restart_at = 0.0
        retry_seconds = 1.0
        while not stopping.wait(0.1):
            if api.poll() is not None:
                logger.error('API process %s exited with %s; stopping the runner', api.pid, api.returncode)
                result = api.returncode or 1
                break
            now = time.monotonic()
            if runner is not None and runner.poll() is not None:
                terminate_group(runner, signal.SIGKILL)
                children.remove(runner)
                if now - runner_started >= 30:
                    retry_seconds = 1.0
                logger.error(
                    'Runner process %s exited with %s; API remains available, retrying in %.1fs',
                    runner.pid,
                    runner.returncode,
                    retry_seconds,
                )
                runner = None
                restart_at = now + retry_seconds
                retry_seconds = min(30, retry_seconds * 2)
            if runner_enabled and runner is None and now >= restart_at:
                try:
                    runner = start_child([sys.executable, '-m', 'backend.job_service'])
                except OSError:
                    logger.exception('Runner could not start; API remains available, retrying in %.1fs', retry_seconds)
                    restart_at = now + retry_seconds
                    retry_seconds = min(30, retry_seconds * 2)
                else:
                    runner_started = now
                    children.append(runner)
    finally:
        for child in children:
            terminate_group(child, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while any(child.poll() is None for child in children) and time.monotonic() < deadline:
            if forcing.wait(min(0.1, max(0, deadline - time.monotonic()))):
                break
        for child in children:
            if child.poll() is None:
                logger.warning('Force-stopping managed process %s', child.pid)
                result = result or 1
            terminate_group(child, signal.SIGKILL)
            child.wait()
        os.close(parent_alive)
        os.close(lifeline)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = arguments.command
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        parser.error('Provide an API command after --')
    logging.basicConfig(level=logging.INFO)
    return supervise(command)


if __name__ == '__main__':
    sys.exit(main())
