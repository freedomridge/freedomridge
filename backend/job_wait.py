"""The five-second rule for job-backed endpoints: the final result, the same error, or 202 with the job."""

import asyncio
import time

from fastapi import HTTPException
from fastapi.responses import JSONResponse

ANSWER_SECONDS = 5.0
POLL_SECONDS = 0.1


class JobInputError(ValueError):
    """Invalid input found while the job runs. The waiting request answers with this status, as the
    synchronous endpoint did; a later reader sees a failed job with the same message."""

    status_code = 400


class JobNotFound(JobInputError):
    status_code = 404


class JobConflict(JobInputError):
    status_code = 409


class JobUnprocessable(JobInputError):
    status_code = 422


class JobInsufficientStorage(JobInputError):
    """Not enough free disk on the database volume (507, as the request's own free-space check)."""

    status_code = 507


# The runner records only the exception's type name and message.
_STATUS = {
    error.__name__: error.status_code
    for error in (JobInputError, JobNotFound, JobConflict, JobUnprocessable, JobInsufficientStorage)
}


# These operations can join another submitter's job. Their results and input refusals are public
# to authorized dataset readers; internal failures remain private to the submitter and admins.
_JOINED_KINDS = ('field_index_build', 'ann_index_build', 'duplicate_maintenance', 'index_drop')


def job_outcome(job, user: dict) -> dict:
    """The same requester-aware result/error allowlist for waiting responses, status and history."""
    if user['role'] == 'admin' or (job['created_by'] is not None and job['created_by'] == user['id']):
        return {'result': job['result'], 'error': job['error']}
    if job['kind'] not in _JOINED_KINDS:
        return {}
    error = job['error'] if isinstance(job['error'], dict) else None
    if error is not None and error.get('type') not in _STATUS:
        error = {'type': 'JobFailed', 'message': 'The job failed; an administrator can see why'}
    return {'result': job['result'], 'error': error}


def accepted(job) -> JSONResponse:
    return JSONResponse(status_code=202, content={'job_id': str(job['id']), 'state': job['state']})


async def answer(pool, job, started: float, *, user: dict, wait: bool = True, failure_status: int = 500):
    """Wait until `started + ANSWER_SECONDS` at most. A wait never cancels the job."""
    if not wait:
        return accepted(job)
    remaining = started + ANSWER_SECONDS - time.monotonic()
    if remaining <= 0:
        return accepted(job)
    current = job

    async def poll():
        nonlocal current
        while True:
            current = await pool.fetchrow(
                'SELECT id,kind,created_by,state,result,error FROM jobs WHERE id=$1',
                job['id'],
            )
            if current is None:
                raise HTTPException(status_code=404, detail='Job not found')
            if current['state'] == 'succeeded':
                return job_outcome(current, user).get('result')
            if current['state'] == 'failed':
                error = job_outcome(current, user).get('error')
                error = error if isinstance(error, dict) else {}
                status = _STATUS.get(str(error.get('type')), failure_status)
                raise HTTPException(
                    status_code=status,
                    detail=error.get('message') or f"Job {current['id']} failed",
                )
            if current['state'] == 'cancelled':
                raise HTTPException(status_code=409, detail=f"Job {current['id']} was cancelled")
            await asyncio.sleep(POLL_SECONDS)

    try:
        # asyncpg's query timeout excludes pool acquisition. Bound the whole polling operation,
        # including acquisition and sleeps, by the response budget left after submission.
        return await asyncio.wait_for(poll(), timeout=remaining)
    except asyncio.TimeoutError:
        return accepted(current)
