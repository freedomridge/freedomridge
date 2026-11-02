"""Separate operation self-cancellation from cancellation of its owning worker."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

Result = TypeVar('Result')


class FailureBackoff:
    """Log outage transitions, with capped retries that reset after successful work."""

    def __init__(self, logger: logging.Logger, operation: str, initial_seconds: float):
        self.logger = logger
        self.operation = operation
        self.initial_seconds = initial_seconds
        self.delay = initial_seconds
        self.error_type: type[Exception] | None = None

    def failed(self, error: Exception) -> float:
        if self.error_type is None:
            self.logger.exception('%s failed; retrying with backoff', self.operation)
        elif type(error) is not self.error_type:
            self.logger.warning('%s failure changed to %s; retrying', self.operation, type(error).__name__)
        self.error_type = type(error)
        delay = self.delay
        self.delay = min(max(30, self.initial_seconds), self.delay * 2)
        return delay

    def recovered(self):
        if self.error_type is not None:
            self.logger.info('%s recovered', self.operation)
        self.error_type = None
        self.delay = self.initial_seconds


async def run_isolated(
    operation: Awaitable[Result],
    *,
    name: str,
    timeout: float | None = None,
    on_error: Callable[[], None] | None = None,
) -> Result:
    task = asyncio.ensure_future(operation)
    try:
        completed, _ = await asyncio.wait({task}, timeout=timeout)
        if not completed:
            raise TimeoutError(f'{name} exceeded its wall-time budget')
        if task.cancelled():
            raise RuntimeError(f'{name} cancelled its own task')
        return task.result()
    except BaseException:
        if on_error is not None:
            on_error()
        raise
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
