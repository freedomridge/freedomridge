"""The SDK polls one durable job with bounded waiting and no swallowed failures."""

from unittest.mock import Mock
from uuid import UUID

import pytest
import requests
from fridge import Job, JobCancelled, JobFailed, JobTimeout
from fridge.core import job as job_module
from fridge.core.client import Client
from fridge.core.dataset import Dataset

JOB_ID = UUID('813b5d30-e738-4d1d-9ace-0acf1e6466fd')


def status(state='running', **values):
    return {'id': str(JOB_ID), 'state': state, 'progress': {'done': 0, 'total': 10, 'message': 'Working'}, **values}


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    current = Clock()
    monkeypatch.setattr(job_module, 'time', current)
    return current


def client_with_response(body, *, ok=True, code=200):
    client = Client.__new__(Client)
    client.base_url = 'http://test.invalid/api'
    response = Mock(ok=ok, status_code=code, reason='Response')
    response.json.return_value = body
    client.session = Mock()
    client.session.get.return_value = response
    client.session.post.return_value = response
    return client


def test_get_job_reconnects_without_submitting_or_fetching():
    client = client_with_response(status())
    handle = client.get_job(JOB_ID)
    assert isinstance(handle, Job)
    assert handle.id == str(JOB_ID)
    assert str(JOB_ID) in repr(handle)
    client.session.get.assert_not_called()
    client.session.post.assert_not_called()


def test_statistics_refresh_submits_once_and_waits_on_the_job(clock):
    client = client_with_response({'job_id': str(JOB_ID), 'state': 'queued'})
    result = {'requested_dataset_refreshed': True, 'refreshed_count': 1}
    client.get_job_status = Mock(side_effect=[status(), status('succeeded', result=result)])
    assert client.refresh_stats(7, timeout=10, force=True) == result
    client.session.post.assert_called_once_with(
        'http://test.invalid/api/datasets/7/refresh-stats',
        params={'wait': 'false', 'force': 'true'},
        timeout=(10, 30),
    )
    assert client.get_job_status.call_count == 2


def test_statistics_async_handle_and_wait_timeout_never_cancel(clock):
    client = client_with_response({'job_id': str(JOB_ID), 'state': 'queued'})
    dataset = Dataset.__new__(Dataset)
    dataset.client, dataset._id, dataset.name = client, 7, 'training'
    handle = dataset.refresh_stats(wait=False)
    assert isinstance(handle, Job) and handle.id == str(JOB_ID)
    client.session.get.assert_not_called()
    client.get_job_status = Mock(return_value=status())
    with pytest.raises(JobTimeout) as error:
        handle.wait(timeout=0.5)
    assert error.value.job_id == str(JOB_ID)
    assert client.session.post.call_count == 1


def test_dataset_delete_blocks_on_one_job_and_preserves_result(clock):
    client = client_with_response({'job_id': str(JOB_ID), 'state': 'queued'}, code=202)
    client.session.delete.return_value = client.session.post.return_value
    result = {'message': 'Dataset 42 deleted successfully'}
    client.get_job_status = Mock(side_effect=[status(), status('succeeded', result=result)])
    dataset = Dataset.__new__(Dataset)
    dataset.client, dataset._id = client, 42
    assert dataset.delete(timeout=10) == result
    client.session.delete.assert_called_once_with('http://test.invalid/api/datasets/42', timeout=(10, 30))
    assert client.get_job_status.call_count == 2


def test_dataset_delete_async_timeout_and_resume_keep_job_identity(clock):
    client = client_with_response({'job_id': str(JOB_ID), 'state': 'queued'}, code=202)
    client.session.delete.return_value = client.session.post.return_value
    handle = client.delete_dataset(7, wait=False)
    assert isinstance(handle, Job) and handle.id == str(JOB_ID)
    client.session.get.assert_not_called()
    client.get_job_status = Mock(return_value=status())
    with pytest.raises(JobTimeout) as error:
        handle.wait(timeout=0.5)
    assert error.value.job_id == str(JOB_ID)
    client.session.post.assert_not_called()
    dataset = Dataset.__new__(Dataset)
    dataset.client, dataset._id = client, 7
    resumed = dataset.resume_deletion(wait=False)
    assert resumed.id == handle.id
    client.session.post.assert_called_once_with(
        'http://test.invalid/api/datasets/7/deletion/resume',
        timeout=(10, 30),
    )


def test_dataset_name_lookup_ignores_deleting_predecessor():
    client = client_with_response({})
    active = {'id': 8, 'name': 'training', 'deleting': False}
    client.get_datasets = Mock(return_value=[{'id': 7, 'name': 'training', 'deleting': True}, active])
    assert client.get_dataset('training') == active


@pytest.mark.parametrize('method', ['delete_dataset', 'resume_dataset_deletion'])
@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan')])
def test_dataset_deletion_invalid_wait_never_submits(method, timeout):
    client = client_with_response({})
    with pytest.raises(ValueError, match='timeout'):
        getattr(client, method)(7, timeout=timeout)
    client.session.delete.assert_not_called()
    client.session.post.assert_not_called()


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf')])
def test_statistics_rejects_invalid_timeout_before_submitting(timeout):
    client = client_with_response({})
    with pytest.raises(ValueError, match='timeout'):
        client.refresh_stats(7, timeout=timeout)
    client.session.post.assert_not_called()


def test_status_passes_a_total_http_budget_instead_of_the_default_300_second_read_timeout():
    client = client_with_response(status())
    assert client.get_job(JOB_ID).status(timeout=0.25) == status()
    arguments = client.session.get.call_args
    assert arguments.args == (f'http://test.invalid/api/jobs/{JOB_ID}',)
    deadline = arguments.kwargs['timeout']
    assert deadline.total == deadline.connect_timeout == 0.25
    assert deadline.read_timeout <= 0.25


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan')])
def test_direct_status_rejects_nonfinite_or_nonpositive_request_timeouts(timeout):
    client = client_with_response(status())
    with pytest.raises(ValueError, match='timeout'):
        client.get_job(JOB_ID).status(timeout=timeout)
    client.session.get.assert_not_called()


def test_cancel_preserves_the_running_acknowledgement():
    body = status(cancel_requested=True)
    client = client_with_response(body)
    assert client.get_job(JOB_ID).cancel() == body
    client.session.post.assert_called_once_with(f'http://test.invalid/api/jobs/{JOB_ID}/cancel')


def test_dataset_history_preserves_opaque_cursor_and_page_shape():
    page = {'items': [status()], 'next_cursor': 'opaque-cursor'}
    client = client_with_response(page)
    dataset = Dataset.__new__(Dataset)
    dataset.client, dataset._id = client, 7
    assert dataset.list_jobs(limit=12, cursor='previous-cursor') == page
    client.session.get.assert_called_once_with(
        'http://test.invalid/api/datasets/7/jobs',
        params={'limit': 12, 'cursor': 'previous-cursor'},
    )


def test_dataset_history_can_include_retained_automatic_statistics():
    page = {'items': [status()], 'next_cursor': None}
    client = client_with_response(page)
    dataset = Dataset.__new__(Dataset)
    dataset.client, dataset._id = client, 7
    assert dataset.list_jobs(include_system_statistics=True) == page
    client.session.get.assert_called_once_with(
        'http://test.invalid/api/datasets/7/jobs',
        params={'limit': 50, 'include_system_statistics': 'true'},
    )


def test_wait_returns_exact_result_and_reports_changed_progress(clock):
    completed = {'requested_dataset_refreshed': True, 'fields_updated': 12}
    client = Mock()
    client.get_job_status.side_effect = [status('queued'), status(), status(), status('succeeded', result=completed)]
    report = Mock()
    assert Job(client, JOB_ID).wait(timeout=10, progress=report) == completed
    assert report.call_count == 3
    assert clock.sleeps == [1, 1, 1]
    assert [call.kwargs['timeout'] for call in client.get_job_status.call_args_list] == [10, 9, 8, 7]


@pytest.mark.parametrize('result', [None, [], {}, 0])
def test_wait_does_not_replace_a_successful_falsy_result(clock, result):
    client = Mock()
    client.get_job_status.return_value = status('succeeded', result=result)
    assert Job(client, JOB_ID).wait() == result
    assert clock.sleeps == []


@pytest.mark.parametrize('state,error_type', [('failed', JobFailed), ('cancelled', JobCancelled)])
def test_terminal_errors_keep_job_id_actual_message_and_structured_error(clock, state, error_type):
    error = {'code': 'bad_dimension', 'message': 'Embedding dimension changed from 512 to 768'}
    client = Mock()
    client.get_job_status.return_value = status(state, error=error)
    with pytest.raises(error_type, match='dimension changed') as caught:
        Job(client, JOB_ID).wait()
    assert caught.value.job_id == str(JOB_ID)
    assert caught.value.error == error
    assert caught.value.status['state'] == state
    assert clock.sleeps == []


def test_wait_deadline_includes_http_duration_and_clamps_last_sleep(clock):
    client = Mock()

    def fetch(*args, **kwargs):
        clock.now += 0.4
        return status()

    client.get_job_status.side_effect = fetch
    with pytest.raises(JobTimeout, match='not cancelled'):
        Job(client, JOB_ID).wait(timeout=0.5, poll_interval=1)
    assert clock.now == 0.5
    assert clock.sleeps == [pytest.approx(0.1)]
    client.get_job_status.assert_called_once_with(str(JOB_ID), timeout=0.5)
    client.cancel_job.assert_not_called()


def test_a_status_response_received_after_the_deadline_cannot_report_timely_success(clock):
    client = Mock()

    def slow_fetch(*args, **kwargs):
        clock.now += 2
        return status('succeeded', result={'finished': True})

    client.get_job_status.side_effect = slow_fetch
    with pytest.raises(JobTimeout):
        Job(client, JOB_ID).wait(timeout=1)


def test_request_timeout_at_the_wait_deadline_retains_the_transport_cause(clock):
    client = Mock()

    def timed_out(*args, **kwargs):
        clock.now += 1
        raise requests.Timeout('Status connection timed out')

    client.get_job_status.side_effect = timed_out
    with pytest.raises(JobTimeout) as caught:
        Job(client, JOB_ID).wait(timeout=1)
    assert isinstance(caught.value.__cause__, requests.Timeout)


@pytest.mark.parametrize('error', [requests.ConnectionError('offline'), requests.Timeout('network timeout')])
def test_transport_errors_retry_until_deadline_without_cancelling_job(clock, error):
    client = Mock()
    client.get_job_status.side_effect = error
    with pytest.raises(JobTimeout) as caught:
        Job(client, JOB_ID).wait(timeout=10)
    assert caught.value.__cause__ is error
    assert clock.now == 10
    assert clock.sleeps == [0.5, 1, 2, 4, 2.5]
    client.cancel_job.assert_not_called()


@pytest.mark.parametrize('code', [502, 503, 504])
def test_gateway_retry_preserves_result_and_uses_falsey_http_response(clock, code):
    response = requests.Response()
    response.status_code = code
    client = Mock()
    client.get_job_status.side_effect = [
        requests.HTTPError('gateway restart', response=response),
        requests.ConnectionError('reconnecting'),
        status('succeeded', result={'exact': [0, False, None]}),
    ]
    assert Job(client, JOB_ID).wait(timeout=10) == {'exact': [0, False, None]}
    assert clock.sleeps == [0.5, 1]


def test_retry_backoff_resets_after_valid_status(clock):
    client = Mock()
    client.get_job_status.side_effect = [
        requests.Timeout(),
        requests.Timeout(),
        status(),
        requests.Timeout(),
        status('succeeded', result=7),
    ]
    assert Job(client, JOB_ID).wait(timeout=10) == 7
    assert clock.sleeps == [0.5, 1, 1, 0.5]


@pytest.mark.parametrize('code', [401, 403, 404, 409, 422, 500])
def test_nontransient_http_errors_are_not_retried(clock, code):
    response = requests.Response()
    response.status_code = code
    client = Mock()
    client.get_job_status.side_effect = requests.HTTPError('fatal', response=response)
    with pytest.raises(requests.HTTPError):
        Job(client, JOB_ID).wait(timeout=1)
    assert clock.sleeps == []
    assert client.get_job_status.call_count == 1


def test_status_does_not_use_the_field_mutation_busy_retry_budget(clock, monkeypatch):
    client = Client(base_url='http://test.invalid/api')
    response = requests.Response()
    response.status_code = 409
    response.headers['Retry-After'] = '1'
    response._content = b'{"detail":"busy"}'
    transport = Mock(return_value=response)
    monkeypatch.setattr(requests.Session, 'request', transport)
    with pytest.raises(requests.HTTPError):
        client.get_job(JOB_ID).wait(timeout=0.2)
    assert transport.call_count == 1
    assert clock.sleeps == []


def test_heartbeat_changes_do_not_spam_progress_callbacks(clock):
    client = Mock()
    client.get_job_status.side_effect = [
        status(heartbeat_at='first'),
        status(heartbeat_at='second'),
        status(failures=1),
        status('succeeded', result=1),
    ]
    progress = Mock()
    assert Job(client, JOB_ID).wait(progress=progress) == 1
    assert progress.call_count == 3


def test_wait_keeps_actionable_http_permission_errors(clock):
    client = client_with_response({'detail': 'Access revoked'}, ok=False, code=403)
    with pytest.raises(requests.HTTPError, match='Access revoked'):
        client.get_job(JOB_ID).wait()


def test_progress_callback_errors_are_not_swallowed(clock):
    client = Mock()
    client.get_job_status.return_value = status()
    progress = Mock(side_effect=RuntimeError('progress output failed'))
    with pytest.raises(RuntimeError, match='progress output failed'):
        Job(client, JOB_ID).wait(progress=progress)


def test_progress_callback_duration_cannot_bypass_the_wait_deadline(clock):
    client = Mock()
    client.get_job_status.return_value = status('succeeded', result={'finished': True})

    def slow_progress(state):
        clock.now += 2

    with pytest.raises(JobTimeout):
        Job(client, JOB_ID).wait(timeout=1, progress=slow_progress)


def test_unknown_job_state_is_an_error_not_an_endless_poll(clock):
    client = Mock()
    client.get_job_status.return_value = status('invalid-state')
    with pytest.raises(ValueError, match='Unknown job state'):
        Job(client, JOB_ID).wait()
    assert clock.sleeps == []


@pytest.mark.parametrize('timeout', [-1, float('inf'), float('nan')])
def test_wait_requires_a_finite_nonnegative_timeout(timeout):
    client = Mock()
    with pytest.raises(ValueError, match='timeout'):
        Job(client, JOB_ID).wait(timeout=timeout)
    client.get_job_status.assert_not_called()


@pytest.mark.parametrize('poll_interval', [0, -1, float('inf'), float('nan')])
def test_poll_interval_must_be_finite_and_positive(poll_interval):
    with pytest.raises(ValueError, match='poll_interval'):
        Job(Mock(), JOB_ID).wait(poll_interval=poll_interval)


def test_zero_wait_budget_never_starts_a_request(clock):
    client = Mock()
    with pytest.raises(JobTimeout):
        Job(client, JOB_ID).wait(timeout=0)
    client.get_job_status.assert_not_called()


def test_invalid_job_id_cannot_change_the_request_path():
    with pytest.raises(ValueError):
        Job(Mock(), '../datasets/7')


@pytest.mark.parametrize(
    'method,args',
    [
        ('bulk_tag_samples', (7, [1, 2])),
        ('bulk_set_metadata', (7, [1, 2])),
        ('bulk_delete_samples', (7, [1, 2])),
        ('rename_field', (7, 'old', 'new')),
        ('clone_field', (7, 'old', 'new')),
        ('clear_field', (7, 'old')),
        ('resume_bulk_job', (JOB_ID,)),
    ],
)
def test_bulk_sdk_blocking_compatibility_and_async_handle(clock, method, args):
    client = client_with_response({'job_id': str(JOB_ID)}, code=202)
    result = {'deleted_count' if method == 'bulk_delete_samples' else 'updated_count': 123}
    client.get_job_status = Mock(return_value=status('succeeded', result=result))
    assert getattr(client, method)(*args) == result
    assert client.session.post.call_count == 1
    assert isinstance(getattr(client, method)(*args, wait=False), Job)
    assert client.get_job_status.call_count == 1


def test_bulk_sdk_preserves_small_synchronous_result():
    result = {'updated_count': 2, 'message': 'Updated tags for 2 samples'}
    client = client_with_response(result)
    assert client.bulk_tag_samples(7, [1, 2]) == result


def test_bulk_view_freezes_filters_and_limited_membership(clock):
    from fridge.core.view import DatasetView

    client = client_with_response({'job_id': str(JOB_ID)}, code=202)
    client.get_job_status = Mock(return_value=status('succeeded', result={'updated_count': 3}))
    dataset = Dataset.__new__(Dataset)
    dataset._id = 7
    dataset._dirty = False
    dataset.client = client
    view = DatasetView(dataset, filters={'field': 'score', 'operator': '>', 'value': 1})
    view.ids = Mock(side_effect=AssertionError('An unrestricted view must not enumerate IDs'))
    assert dataset.set_values(view, 'label', 'reviewed') == 3
    assert client.session.post.call_args.kwargs['json']['filters'] == view._filters
    assert isinstance(view.tag_samples(['yes'], wait=False), Job)
    limited = view.limit(2).sort_by('score')
    limited.ids = Mock(return_value=[3, 8])
    assert isinstance(limited.delete_samples(wait=False), Job)
    assert client.session.post.call_args.kwargs['json']['sample_ids'] == [3, 8]


@pytest.mark.parametrize('multi', [False, True])
def test_per_sample_sdk_bounds_submissions_and_preserves_count(clock, multi):
    client = client_with_response({'job_id': str(JOB_ID)}, code=202)
    client.get_job_status = Mock(return_value=status('succeeded', result={'updated_count': 1}))
    dataset = Dataset.__new__(Dataset)
    dataset._id = 7
    dataset.client = client
    values = {1: 'a', 2: 'b', 3: 'c'}
    if multi:
        result = dataset.set_values_per_sample_multi({sid: {'label': v} for sid, v in values.items()}, batch_size=1)
    else:
        result = dataset.set_values_per_sample('label', values, batch_size=1)
    assert result == 3
    assert client.session.post.call_count == 3
    assert [call.kwargs['json']['updates'] for call in client.session.post.call_args_list] == [
        [{'sample_id': sid, 'metadata': {'label': value}}] for sid, value in values.items()
    ]
    assert isinstance(dataset.set_values_per_sample_multi({1: {'label': 'a'}}, wait=False), Job)
    jobs = dataset.set_values_per_sample('label', values, batch_size=2, wait=False)
    assert len(jobs) == 2 and all(isinstance(job, Job) for job in jobs)


def test_per_sample_sdk_caps_even_large_batch_size_and_stops_on_failure():
    dataset = Dataset.__new__(Dataset)
    dataset._id = 7
    dataset.client = Mock()
    dataset.client.bulk_set_metadata.side_effect = [{'updated_count': 5000}, RuntimeError('second batch failed')]
    with pytest.raises(RuntimeError, match='second batch'):
        dataset.set_values_per_sample('label', dict.fromkeys(range(20000), 'yes'), batch_size=20000)
    assert dataset._dirty
    calls = dataset.client.bulk_set_metadata.call_args_list
    assert len(calls) == 2
    assert [len(call.kwargs['updates']) for call in calls] == [5000, 5000]
    assert calls[0].kwargs['updates'][0]['sample_id'] == 0
    assert calls[1].kwargs['updates'][0]['sample_id'] == 5000


def test_id_list_view_sends_explicit_target_without_enumeration():
    from fridge.core.view import DatasetView

    dataset = Dataset.__new__(Dataset)
    view = DatasetView(dataset, filters={'field': 'id', 'operator': 'in', 'value': [5, 7, 9]})
    view.ids = Mock(side_effect=AssertionError('Pure ID filters need no query'))
    assert view._bulk_target() == {'sample_ids': [5, 7, 9], 'filters': view._filters}
    view._filters['logic'] = 'AND'
    assert view._bulk_target() == {'filters': view._filters}


@pytest.mark.parametrize('batch_size', [0, -1, 1.5, True])
def test_per_sample_invalid_batch_size_never_submits(batch_size):
    dataset = Dataset.__new__(Dataset)
    dataset._id = 7
    dataset.client = Mock()
    with pytest.raises(ValueError, match='batch_size'):
        dataset.set_values_per_sample_multi({1: {'label': 'a'}}, batch_size=batch_size)
    dataset.client.bulk_set_metadata.assert_not_called()


def test_discard_bulk_job_posts_once_and_returns_the_record():
    client = client_with_response({'job_id': str(JOB_ID), 'state': 'cancelled', 'discarded': True})
    assert client.discard_bulk_job(JOB_ID)['discarded'] is True
    assert client.session.post.call_args.args == (f'http://test.invalid/api/jobs/{JOB_ID}/discard',)


def test_per_sample_accepts_numpy_batch_size():
    numpy = pytest.importorskip('numpy')
    dataset = Dataset.__new__(Dataset)
    dataset._id = 7
    dataset.client = Mock()
    dataset.client.bulk_set_metadata.return_value = {'updated_count': 2}
    assert dataset.set_values_per_sample('label', {1: 'a', 2: 'b', 3: 'c'}, batch_size=numpy.int64(2)) == 4
    assert [len(call.kwargs['updates']) for call in dataset.client.bulk_set_metadata.call_args_list] == [2, 1]
