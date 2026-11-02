"""Durable background job handles, independent of any local worker process."""

import math
import time
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

import requests

if TYPE_CHECKING:
    from .client import Client


class JobError(RuntimeError):
    """A terminal job outcome, with the server's structured status preserved."""

    def __init__(self, status: dict[str, Any]):
        self.job_id = str(status['id'])
        self.status = status
        self.error = status.get('error')
        detail = self.error or status.get('progress', {}).get('message') or status['state']
        if isinstance(detail, dict):
            detail = detail.get('message') or str(detail)
        super().__init__(f"Job {self.job_id} {status['state']}: {detail}")


class JobFailed(JobError):
    """The worker reported a failure, rather than a successful partial result."""


class JobCancelled(JobError):
    """The worker acknowledged cancellation; committed partial progress may remain."""


class JobTimeout(TimeoutError):
    """Waiting expired; the job itself has not been cancelled."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__(f'Waiting for job {job_id} timed out; the job is not cancelled')


class Job:
    """A reconnectable job handle. Status and cancellation use the caller's current API credentials."""

    def __init__(self, client: 'Client', job_id: str | UUID):
        self.client = client
        self.id = str(UUID(str(job_id)))

    def status(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Read current progress, result and error without waiting for completion."""
        return self.client.get_job_status(self.id, timeout=timeout)

    def cancel(self) -> dict[str, Any]:
        """Request cancellation; a running job remains running until its worker acknowledges it."""
        return self.client.cancel_job(self.id)

    def wait(
        self,
        timeout: float = 1800,
        *,
        poll_interval: float = 1,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        """Return the job's result, or raise JobFailed, JobCancelled, or JobTimeout.

        The finite timeout covers status requests and poll sleeps. A wait timeout
        never cancels server work. Temporary network/gateway errors are retried;
        other API errors and progress callback errors propagate.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError('timeout must be finite and non-negative')
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError('poll_interval must be finite and positive')
        deadline = time.monotonic() + timeout
        previous = None
        retry_delay = 0.5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JobTimeout(self.id)
            try:
                status = self.status(timeout=min(remaining, 30))
            except requests.RequestException as error:
                response = error.response
                transient = (
                    isinstance(
                        error,
                        (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError),
                    )
                    and not isinstance(error, requests.exceptions.SSLError)
                ) or (response is not None and response.status_code in (502, 503, 504))
                if not transient:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise JobTimeout(self.id) from error
                time.sleep(min(retry_delay, remaining))
                if time.monotonic() >= deadline:
                    raise JobTimeout(self.id) from error
                retry_delay = min(retry_delay * 2, 5)
                continue
            retry_delay = 0.5
            if time.monotonic() >= deadline:
                raise JobTimeout(self.id)
            visible = {key: status.get(key) for key in ('state', 'progress', 'cancel_requested', 'failures', 'error')}
            if progress is not None and visible != previous:
                progress(status)
                previous = visible
                if time.monotonic() >= deadline:
                    raise JobTimeout(self.id)
            if status['state'] == 'succeeded':
                return status.get('result')
            if status['state'] == 'failed':
                raise JobFailed(status)
            if status['state'] == 'cancelled':
                raise JobCancelled(status)
            if status['state'] not in ('queued', 'running'):
                raise ValueError(f"Unknown job state: {status['state']}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JobTimeout(self.id)
            time.sleep(min(poll_interval, remaining))

    def __repr__(self) -> str:
        return f'Job(id={self.id!r})'
