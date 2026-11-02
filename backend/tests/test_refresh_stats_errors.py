"""Refresh submission, finite waits and failures use the same authorized durable jobs."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend import job_queue, statistics
from backend.routers import datasets


def setup_refresh(monkeypatch, *, error=None):
    job = {'id': uuid4(), 'dataset_id': 7, 'state': 'queued'}
    result = {
        'requested_dataset_refreshed': True,
        'refreshed_count': 1,
        'datasets': [{'dataset_id': 7, 'fields_updated': 3, 'error': None}],
    }
    monkeypatch.setattr(datasets, 'db', SimpleNamespace(pool=object()))
    submit = AsyncMock(return_value=job)
    wait = AsyncMock(return_value=result, side_effect=error)
    monkeypatch.setattr(statistics, 'request_refresh', submit)
    monkeypatch.setattr(statistics, 'wait_for_refresh', wait)
    return job, result, submit, wait


def test_requested_dataset_failing_is_a_500_naming_the_cause(monkeypatch):
    setup_refresh(monkeypatch, error=RuntimeError('Statistics job failed: duplicate key'))
    with pytest.raises(HTTPException) as error:
        asyncio.run(datasets.refresh_dataset_stats(7, user={'id': 1, 'role': 'admin'}))
    assert error.value.status_code == 500 and 'duplicate key' in error.value.detail


def test_refresh_waits_for_only_the_requested_dataset(monkeypatch):
    job, result, submit, wait = setup_refresh(monkeypatch)
    assert asyncio.run(datasets.refresh_dataset_stats(7, user={'id': 1, 'role': 'admin'})) == result
    assert submit.call_args.args[1] == 7
    assert submit.call_args.kwargs['created_by'] == 1
    assert wait.call_args.args[1] == job['id']


def test_async_refresh_returns_a_handle_without_waiting(monkeypatch):
    job, _, _, wait = setup_refresh(monkeypatch)
    response = asyncio.run(datasets.refresh_dataset_stats(7, user={'id': 1}, wait=False))
    assert response.status_code == 202
    assert json.loads(response.body)['job_id'] == str(job['id'])
    wait.assert_not_awaited()


def test_wait_timeout_preserves_the_handle_and_does_not_claim_success(monkeypatch):
    job, _, _, _ = setup_refresh(monkeypatch, error=TimeoutError('still running'))
    response = asyncio.run(datasets.refresh_dataset_stats(7, user={'id': 1}, timeout=0.01))
    body = json.loads(response.body)
    assert response.status_code == 202 and body['job_id'] == str(job['id'])
    assert body['requested_dataset_refreshed'] is False


def test_submission_contention_is_retryable(monkeypatch):
    _, _, submit, wait = setup_refresh(monkeypatch)
    submit.side_effect = job_queue.QueueBusy('busy')
    with pytest.raises(HTTPException) as error:
        asyncio.run(datasets.refresh_dataset_stats(7, user={'id': 1}))
    assert error.value.status_code == 409 and error.value.headers == {'Retry-After': '1'}
    wait.assert_not_awaited()


def test_batch_refresh_keeps_independent_dataset_failures(monkeypatch):
    job, result, _, wait = setup_refresh(monkeypatch)
    monkeypatch.setattr(statistics, 'schedule_statistics', AsyncMock(return_value=[job, {**job, 'dataset_id': 9}]))
    wait.side_effect = [RuntimeError('broken dataset'), result]
    response = asyncio.run(datasets.refresh_stale_stats({'id': 1}))
    assert response['refreshed_count'] == 1
    assert response['datasets'][0]['error'] == 'broken dataset'
    assert response['datasets'][1]['error'] is None


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan'), 1801])
def test_refresh_rejects_invalid_wait_deadlines(monkeypatch, timeout):
    _, _, submit, _ = setup_refresh(monkeypatch)
    with pytest.raises(HTTPException) as error:
        asyncio.run(datasets.refresh_dataset_stats(7, user={'id': 1}, timeout=timeout))
    assert error.value.status_code == 422
    submit.assert_not_awaited()
