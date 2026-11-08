"""Run this machine as a compute worker for a Freedom Ridge server.

    FRIDGE_API_URL=https://YOUR_DOMAIN/api FRIDGE_API_TOKEN=<worker token> python -m fridge.worker

The token must be a worker token: ``Client().create_token("my desktop", scope="worker")``. The worker
registers with the server, then claims compute work again and again (each claim waits up to
CLAIM_WAIT_SECONDS on the server, and keeps the worker online). The one kind so far is a map re-fit's fit:

1. download the exported vectors (``vectors.bin``, resumed after a dropped connection);
2. run ``python -m fridge.map_fit`` on them in a child process. The child sizes its fit to this machine's free
   memory and makes itself the kernel's first choice for an out-of-memory kill, so the worker survives one: the
   fit then runs again on at most SHRINK_AFTER_OUT_OF_MEMORY of the points, as the server does, down to the
   smallest useful fit;
3. upload the coordinates (``FIT_OUTPUTS``: never a model, which is a pickle and runs code when loaded) and
   complete with each one's sha256. The server checks them and swaps the new map in.

All the while a thread renews the job's lease. A lost lease (the server gave the job to someone else, or it
ended) or a cancel stops the fit at once; Ctrl-C or SIGTERM gives the job back. Work files live in
``FRIDGE_WORKER_DIR`` (default ``~/.fridge/worker``), one folder per job, removed when it is done.
"""

import argparse
import hashlib
import itertools
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from importlib import metadata
from pathlib import Path

import requests

from .core.client import WorkerLeaseLost, _json_detail, get_client

KINDS = ("map_refit",)
# How long to wait before trying again after the server failed to answer (down, or it dropped us).
RETRY_SECONDS = 20
CLAIM_WAIT_SECONDS = 25
# A map re-fit's fit: what it reads and what it sends back (the server's map_refit.REMOTE_INPUTS/REMOTE_OUTPUTS).
FIT_INPUT = "vectors.bin"
FIT_OUTPUTS = ("ids.npy", "versions.npy", "coords.npy", "manifest.json")
# After an out-of-memory kill, the next attempt's fit takes at most this share of the killed one's points.
SHRINK_AFTER_OUT_OF_MEMORY = 0.75
# A transfer or report that fails for a moment (the network, a busy or restarting server) is tried this often
# again, waiting 2, 4, 8... seconds, while the lease lasts.
TRANSFER_ATTEMPTS = 6


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


def work_root() -> Path:
    return Path(os.environ.get("FRIDGE_WORKER_DIR") or Path.home() / ".fridge" / "worker")


def _transient(error) -> bool:
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    status = error.response.status_code if isinstance(error, requests.HTTPError) and error.response is not None else 0
    return status == 429 or status >= 500


class _Lease:
    """Renews a claimed job's lease from a thread while the worker works on it (on its own session).
    ``lost``: the server says the job isn't this worker's any more. ``cancelled``: someone asked to cancel it."""

    def __init__(self, client, worker_id, job):
        self.client = client.clone() if hasattr(client, "clone") else client
        self.worker_id, self.job_id, self.lease_token = worker_id, job["id"], job["lease_token"]
        self.every = job.get("heartbeat_seconds") or 30
        self.message = None
        self.reason = "the job is no longer this worker's"
        self.lost, self.cancelled, self._stop = threading.Event(), threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._beat, name="fridge-worker-lease", daemon=True)

    def _beat(self):
        while not self._stop.wait(self.every):
            try:
                answer = self.client.worker_job_heartbeat(
                    self.worker_id,
                    self.job_id,
                    self.lease_token,
                    message=self.message,
                )
            except WorkerLeaseLost:
                self.lost.set()
                return
            except requests.RequestException as error:  # the next beat tries again, within the lease
                print(f"Could not renew the lease of job {self.job_id} ({error}); trying again", file=sys.stderr)
                continue
            except Exception as error:  # anything else (a server of another major version): stop the work now
                self.reason = f"the lease can't be renewed: {error}"
                self.lost.set()
                return
            if answer.get("cancel_requested"):
                self.cancelled.set()

    @property
    def ended(self) -> bool:
        return self.lost.is_set() or self.cancelled.is_set()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=60)
        return False


def _retrying(lease, call, *args, sleep=time.sleep, **kwargs):
    """`call`, tried again after a failure that may pass, while this worker still holds the job."""
    for attempt in range(TRANSFER_ATTEMPTS):
        try:
            return call(*args, **kwargs)
        except requests.RequestException as error:
            if not _transient(error) or attempt == TRANSFER_ATTEMPTS - 1 or lease.lost.is_set():
                raise
            print(f"{error}; trying again", file=sys.stderr)
            sleep(2 ** (attempt + 1))


def run_fit(source, output, log, max_fit_size, stop):
    """``python -m fridge.map_fit`` on `source` in a child process. Its exit code, or None when `stop()` asked
    to end it first (the child is killed: a fit has nothing worth keeping halfway)."""
    command = [sys.executable, "-m", "fridge.map_fit", "--input", str(source), "--output", str(output)]
    if max_fit_size:
        command += ["--max-fit-size", str(max_fit_size)]
    with open(log, "ab") as target:
        child = subprocess.Popen(command, stdout=target, stderr=subprocess.STDOUT)
        try:
            while child.poll() is None:
                if stop():
                    child.kill()
                    child.wait()
                    return None
                time.sleep(1)
        except BaseException:
            child.kill()
            child.wait()
            raise
    return child.returncode


def _last_line(path: Path) -> str:
    try:
        lines = [line for line in path.read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return ""
    return lines[-1][:500] if lines else ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(16 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def fit_map(client, worker_id, job, folder: Path) -> None:
    """A map re-fit's fit: download, fit (smaller after an out-of-memory kill), upload, complete."""
    from .core.dataset import MINIMUM_FIT_POINTS

    job_id, lease_token = job["id"], job["lease_token"]
    with _Lease(client, worker_id, job) as lease:
        lease.message = "Downloading the vectors to a compute worker"
        _retrying(lease, client.download_worker_file, worker_id, job_id, lease_token, FIT_INPUT, folder / FIT_INPUT)
        max_fit_size = None
        for attempt in itertools.count(1):
            output = folder / f"fit-{attempt}"
            lease.message = "Fitting the map on a compute worker" + (
                f", on at most {max_fit_size:,} points" if max_fit_size else ""
            )
            code = run_fit(folder / FIT_INPUT, output, folder / "fit.log", max_fit_size, lambda: lease.ended)
            if code is None or lease.ended:
                break
            if code == 0:
                break
            plan = output / "plan.json"
            if code == -signal.SIGKILL and plan.is_file():
                planned = json.loads(plan.read_text())
                if planned["fit_size"] > min(planned["rows"], MINIMUM_FIT_POINTS):
                    max_fit_size = int(planned["fit_size"] * SHRINK_AFTER_OUT_OF_MEMORY)
                    print(f"The fit ran out of memory at {planned['fit_size']:,} points; trying {max_fit_size:,}")
                    continue
                message = (
                    f"The fit ran out of memory on this worker at {planned['fit_size']:,} points, the smallest fit"
                )
            else:
                message = f"The fit exited with status {code}: {_last_line(folder / 'fit.log')}"
            _retrying(lease, client.fail_worker_job, worker_id, job_id, lease_token, "MapFitError", message)
            print(message, file=sys.stderr)
            return
        if lease.lost.is_set():
            print(f"Stopped job {job_id}: {lease.reason}", file=sys.stderr)
            return
        if lease.cancelled.is_set():
            _retrying(lease, client.release_worker_job, worker_id, job_id, lease_token)
            print(f"Job {job_id} was cancelled; given back")
            return
        lease.message = "Uploading the new map from a compute worker"
        checksums = {}
        for name in FIT_OUTPUTS:
            _retrying(lease, client.upload_worker_file, worker_id, job_id, lease_token, name, output / name)
            checksums[name] = _sha256(output / name)
        answer = _retrying(lease, client.complete_worker_job, worker_id, job_id, lease_token, {"checksums": checksums})
        print(f"Fitted the map of job {job_id}; the server takes it from here ({answer.get('state')})", flush=True)


def _about_the_worker(error: requests.HTTPError) -> bool:
    """A refusal of this worker or its token (it can't do any job), not of one job."""
    response = error.response
    if response is None or response.status_code in (401, 403):
        return True
    if response.status_code != 404:
        return False
    detail = _json_detail(response)
    return isinstance(detail, str) and detail.startswith("Unknown worker")


def do_job(client, worker_id, job) -> bool:
    """Work on a claimed job in its own folder; give back a job this worker doesn't do, or when it stops. False
    when it gave the job back without working on it (the caller waits before claiming again)."""
    folder = work_root() / str(job["id"])
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if job["kind"] == "map_refit" and (job.get("checkpoint") or {}).get("phase") == "fit":
            print(f"Took job {job['id']}: fitting a map for dataset {job.get('dataset_id')}", flush=True)
            fit_map(client, worker_id, job, folder)
        else:
            client.release_worker_job(worker_id, job["id"], job["lease_token"])
            return False
    except requests.HTTPError as error:
        response = error.response
        if response is None or _transient(error) or _about_the_worker(error):
            raise  # run() retries, registers again, or stops with the reason
        # The server refused this job's work (outputs it won't take, an input that's gone): fail the job with its
        # reason, rather than leave it to the next worker to fail the same way, and go on claiming.
        detail = _json_detail(response)
        message = f"{response.status_code}: {detail or error}"
        print(f"Job {job['id']} failed: {message}", file=sys.stderr)
        try:
            client.fail_worker_job(worker_id, job["id"], job["lease_token"], "WorkerError", message)
        except (requests.RequestException, WorkerLeaseLost) as reported:
            print(f"Could not report it ({reported}); its lease expires instead", file=sys.stderr)
    except WorkerLeaseLost:
        print(f"Job {job['id']} is no longer this worker's; stopped", file=sys.stderr)
    except KeyboardInterrupt:
        try:
            client.release_worker_job(worker_id, job["id"], job["lease_token"])
            print(f"Stopping: gave job {job['id']} back")
        except (requests.RequestException, WorkerLeaseLost):
            pass  # its lease expires instead
        raise
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return True


def run(client, name, kinds, sleep=time.sleep, work=do_job):
    """Register, then claim and work until stopped. A server that is down, restarting, busy or not answering
    (5xx, 429, a timeout) is retried; a server that no longer knows the worker (404: a day without a claim) gets a
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
            job = client.claim_worker_job(worker["worker_id"], wait_seconds=CLAIM_WAIT_SECONDS)
            if job is not None and work(client, worker["worker_id"], job) is False:
                sleep(RETRY_SECONDS)  # a job it gave back unworked: don't claim it again at once
            continue  # a claim waits on the server, so there's nothing to wait for here
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            if status == 404:
                worker = None
            elif status is not None and status != 429 and status < 500:
                raise
            print(f"{error}; trying again", file=sys.stderr)
        except (requests.ConnectionError, requests.Timeout) as error:
            print(f"The server is unreachable or not answering ({error}); trying again", file=sys.stderr)
        sleep(RETRY_SECONDS)


def _stop_on_sigterm(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run this machine as a compute worker for a Freedom Ridge server")
    parser.add_argument("--name", default=socket.gethostname(), help="how the server lists this worker")
    parser.add_argument("--kinds", default=",".join(KINDS), help="the job kinds to take, comma separated")
    args = parser.parse_args(argv)
    kinds = [kind.strip() for kind in args.kinds.split(",") if kind.strip()]
    signal.signal(signal.SIGTERM, _stop_on_sigterm)  # a service manager's stop gives the job back, like Ctrl-C
    try:
        run(get_client(), args.name, kinds)
    except KeyboardInterrupt:
        pass
    except requests.HTTPError as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
