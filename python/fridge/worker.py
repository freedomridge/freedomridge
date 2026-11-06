"""Run this machine as a compute worker for a Freedom Ridge server.

    FRIDGE_API_URL=https://YOUR_DOMAIN/api FRIDGE_API_TOKEN=<worker token> python -m fridge.worker

The token must be a worker token: ``Client().create_token("my desktop", scope="worker")``. The worker
registers with the server and keeps a heartbeat, so the server lists it (GET /api/workers) and system status
counts it online. Taking compute jobs, starting with map re-fits, follows in the next changes.
"""

import argparse
import os
import shutil
import socket
import sys
import time
from importlib import metadata

import requests

from .core.client import get_client

KINDS = ("map_refit",)
# How long to wait before trying again while the worker is not registered (server down, or it dropped us).
RETRY_SECONDS = 20


def capabilities() -> dict:
    """What this machine offers: its RAM, its CPUs and whether it has an NVIDIA GPU."""
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        memory = None
    return {"memory_bytes": memory, "cpus": os.cpu_count(), "gpu": shutil.which("nvidia-smi") is not None}


def sdk_version() -> str:
    try:
        return metadata.version("freedomridge")
    except metadata.PackageNotFoundError:
        return ""


def run(client, name, kinds, sleep=time.sleep):
    """Register, then heartbeat until stopped. A server that is down, restarting, busy or not answering (5xx,
    429, a timeout) is retried; a server that no longer knows the worker (404: a day without a heartbeat) gets a
    new registration; any other refusal (a revoked token, one that isn't a worker token, a kind the server
    doesn't have) stops the worker with the server's reason, as only a person can fix it."""
    worker = None
    while True:
        try:
            if worker is None:
                worker = client.register_worker(name, kinds, capabilities(), socket.gethostname(), sdk_version())
                print(
                    f"Connected to {client.base_url} as {worker['worker_id']} ({name}), for {', '.join(kinds)}",
                    flush=True,
                )
            else:
                client.worker_heartbeat(worker["worker_id"])
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            if status == 404:
                worker = None
            elif status is not None and status != 429 and status < 500:
                raise
            print(f"{error}; trying again", file=sys.stderr)
        except (requests.ConnectionError, requests.Timeout) as error:
            print(f"The server is unreachable or not answering ({error}); trying again", file=sys.stderr)
        sleep(worker["heartbeat_seconds"] if worker else RETRY_SECONDS)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run this machine as a compute worker for a Freedom Ridge server")
    parser.add_argument("--name", default=socket.gethostname(), help="how the server lists this worker")
    parser.add_argument("--kinds", default=",".join(KINDS), help="the job kinds to take, comma separated")
    args = parser.parse_args(argv)
    kinds = [kind.strip() for kind in args.kinds.split(",") if kind.strip()]
    try:
        run(get_client(), args.name, kinds)
    except KeyboardInterrupt:
        pass
    except requests.HTTPError as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
