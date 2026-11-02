#!/usr/bin/env python3
"""Copy stdin to stdout AND append it to a size-rotated log file.

scripts/run_backend.sh pipes the backend through this: it logged only to the terminal it was started
in, so the error_id a 500 response hands the user pointed at scrollback.
All uvicorn workers share the one pipe, so this is the single writer and rotation is safe (a
RotatingFileHandler per worker would race). Stdlib only: runs under any python3.

Usage: <cmd> 2>&1 | python3 scripts/logtee.py logs/backend.log
Env: FRIDGE_LOG_MAX_BYTES (default 50 MB), FRIDGE_LOG_BACKUPS (default 5).
"""

import os
import signal
import sys


def _rotate(path: str, backups: int) -> None:
    for n in range(backups - 1, 0, -1):
        if os.path.exists(f"{path}.{n}"):
            os.replace(f"{path}.{n}", f"{path}.{n + 1}")
    os.replace(path, f"{path}.1")


def main(path: str) -> None:
    max_bytes = int(os.getenv("FRIDGE_LOG_MAX_BYTES", str(50 * 1024 * 1024)))
    backups = max(1, int(os.getenv("FRIDGE_LOG_BACKUPS", "5")))
    # Outlive Ctrl-C: the backend's shutdown lines still land, and we exit on EOF when it does.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    log = None
    for line in sys.stdin.buffer:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        try:  # a full disk must never stop the terminal copy (or SIGPIPE the backend)
            if log is None:
                log = open(path, "ab")
            log.write(line)
            log.flush()
            if log.tell() >= max_bytes:
                log.close()
                log = None
                _rotate(path, backups)
                log = open(path, "ab")  # the live file always exists (tail -F, the next 500's lookup)
        except OSError:
            log = None


if __name__ == "__main__":
    main(sys.argv[1])
