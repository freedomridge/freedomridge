"""Exec a managed command with a pipe-based parent-liveness guard for its whole process group."""

import argparse
import os
import select
import signal


def watch_parent(lifeline, group):
    try:
        os.closerange(0, lifeline)
        os.closerange(lifeline + 1, os.sysconf('SC_OPEN_MAX'))
        while os.read(lifeline, 1):
            pass
    finally:
        try:
            os.killpg(group, signal.SIGKILL)
        finally:
            os._exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lifeline', type=int)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = arguments.command
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        parser.error('Provide a managed command after --')
    if os.getpgrp() != os.getpid():
        raise RuntimeError('A managed command must have its own process group')
    os.fstat(arguments.lifeline)
    if select.select([arguments.lifeline], [], [], 0)[0]:
        os.close(arguments.lifeline)
        return
    previous = {
        signum: signal.signal(signum, signal.SIG_IGN) for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        if os.fork() == 0:
            watch_parent(arguments.lifeline, os.getpgrp())
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    os.close(arguments.lifeline)
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
