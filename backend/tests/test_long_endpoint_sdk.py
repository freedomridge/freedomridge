"""SDK methods of job-backed endpoints keep blocking and return their former result shapes."""

from unittest.mock import Mock

import pytest
from fridge import Job, JobFailed
from fridge.core.dataset import Dataset

from backend.tests.test_jobs_sdk import JOB_ID, client_with_response, status

CONCEPT = {
    'id': 3,
    'dataset_id': 7,
    'name': 'leaf',
    'embedding_field': 'clip',
    'kind': 'seed',
    'spec': {'seed_ids': [1, 2]},
    'bias': 0.0,
    'threshold': 0.81,
    'scored_count': 500000,
    'created_at': '2026-10-20T10:00:00+00:00',
    'updated_at': '2026-10-20T10:01:15+00:00',
    'matched': 1200,
    'score_min': 0.4,
    'score_max': 0.97,
}
CALLS = [
    ('create_concept', (7, 'leaf'), {'seed_ids': [1, 2]}, CONCEPT),
    ('rescore_concept', (7, 3), {}, {'concept_id': 3, 'scored': 500000}),
    ('refine_concept', (7, 3), {'exclude_ids': [9]}, CONCEPT),
    ('rebuild_concept', (7, 3), {'seed_ids': [4]}, CONCEPT),
    ('promote_field', (7, 'score'), {}, {'message': 'Field score promoted successfully'}),
    ('build_ann_index', (7, 'clip'), {}, {'field': 'clip', 'dim': 512, 'index': 'hnsw_emb_clip', 'built': True}),
]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    from fridge.core import job as job_module

    monkeypatch.setattr(job_module.time, 'sleep', lambda seconds: None)


@pytest.mark.parametrize(('method', 'args', 'kwargs', 'result'), CALLS)
def test_fast_answer_is_returned_unchanged_without_polling(method, args, kwargs, result):
    client = client_with_response(result, code=200)
    client.get_job_status = Mock()
    assert getattr(client, method)(*args, **kwargs) == result
    client.get_job_status.assert_not_called()
    assert 'wait' not in client.session.post.call_args.kwargs.get('params', {})


@pytest.mark.parametrize(('method', 'args', 'kwargs', 'result'), CALLS)
def test_202_blocks_on_the_job_and_returns_the_same_shape(method, args, kwargs, result):
    client = client_with_response({'job_id': str(JOB_ID), 'state': 'queued'}, code=202)
    client.get_job_status = Mock(side_effect=[status(), status('succeeded', result=result)])
    assert getattr(client, method)(*args, **kwargs, timeout=60) == result
    assert client.get_job_status.call_count == 2
    assert client.session.post.call_count == 1


@pytest.mark.parametrize(('method', 'args', 'kwargs', 'result'), CALLS)
def test_wait_false_returns_the_job_and_asks_for_an_immediate_answer(method, args, kwargs, result):
    client = client_with_response({'job_id': str(JOB_ID), 'state': 'queued'}, code=202)
    handle = getattr(client, method)(*args, **kwargs, wait=False)
    assert isinstance(handle, Job) and handle.id == str(JOB_ID)
    assert client.session.post.call_args.kwargs['params']['wait'] == 'false'
    client.session.get.assert_not_called()


@pytest.mark.parametrize(('method', 'args', 'kwargs', 'result'), CALLS)
def test_invalid_timeout_never_submits(method, args, kwargs, result):
    client = client_with_response(result)
    with pytest.raises(ValueError):
        getattr(client, method)(*args, **kwargs, timeout=float('nan'))
    client.session.post.assert_not_called()


def test_failed_job_raises_with_the_server_message():
    client = client_with_response({'job_id': str(JOB_ID), 'state': 'queued'}, code=202)
    error = {'type': 'JobConflict', 'message': 'The concept was deleted while it was being scored'}
    client.get_job_status = Mock(return_value=status('failed', error=error))
    with pytest.raises(JobFailed, match='deleted while it was being scored'):
        client.rescore_concept(7, 3)


def test_dataset_wrappers_pass_wait_and_timeout():
    client = client_with_response({'job_id': str(JOB_ID), 'state': 'queued'}, code=202)
    dataset = Dataset.__new__(Dataset)
    dataset.client, dataset._id = client, 7
    for call in (
        lambda: dataset.create_concept('leaf', seed_ids=[1], wait=False),
        lambda: dataset.rescore_concept(3, wait=False),
        lambda: dataset.refine_concept(3, exclude_ids=[1], wait=False),
        lambda: dataset.rebuild_concept(3, seed_ids=[1], wait=False),
        lambda: dataset.promote_field('score', wait=False),
        lambda: dataset.build_ann_index('clip', wait=False),
    ):
        assert isinstance(call(), Job)
