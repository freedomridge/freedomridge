"""Embedding jobs recover once from explicitly identified missing sample IDs."""

import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import requests
from fridge.core.sample import Sample
from PIL import Image

from backend.tests.test_sdk_ingest_retries import dataset, response


def rejected(ids):
    return response(422, {'detail': {'unknown_sample_ids': ids, 'message': 'Sample IDs absent from dataset'}})


@pytest.mark.parametrize('patches', [False, True])
@pytest.mark.parametrize('all_deleted', [False, True])
def test_upload_drops_exact_ids_and_reports_rows(monkeypatch, patches, all_deleted):
    client = dataset().client
    batch = [{'sample_id': sid, 'vector': [1, 0]} for sid in [1, 2, 2, 3]]
    unknown = [1, 2, 3] if all_deleted else [2]
    replies = [rejected(unknown)]
    if not all_deleted:
        replies.append(response(200, {'written': 2}))
    send = Mock(side_effect=replies)
    monkeypatch.setattr(requests.Session, 'request', send)
    upload = client.upload_patch_embeddings if patches else client.upload_embeddings
    with pytest.warns(UserWarning, match='dropped'):
        result = upload(1, batch)
    assert result == {
        'batches': 1,
        'total': 4,
        'written': 0 if all_deleted else 2,
        'dropped': 4 if all_deleted else 2,
        'dropped_sample_ids': unknown,
    }
    assert send.call_count == (1 if all_deleted else 2)
    if not all_deleted:
        assert send.call_args.kwargs['json'] == [batch[0], batch[3]]
    assert len(batch) == 4


@pytest.mark.parametrize('patches', [False, True])
@pytest.mark.parametrize('status', [400, 401, 403, 404, 409, 422, 500, 503, 507])
def test_other_upload_failures_are_not_retried(monkeypatch, patches, status):
    client = dataset().client
    send = Mock(return_value=response(status, {'detail': 'other failure'}))
    monkeypatch.setattr(requests.Session, 'request', send)
    upload = client.upload_patch_embeddings if patches else client.upload_embeddings
    with pytest.raises(requests.HTTPError):
        upload(1, [{'sample_id': 1, 'vector': [1, 0]}])
    assert send.call_count == 1


@pytest.mark.parametrize('patches', [False, True])
@pytest.mark.parametrize('failure', [rejected([2]), response(500, {'detail': 'database failure'})])
def test_second_upload_rejection_raises_without_another_retry(monkeypatch, patches, failure):
    client = dataset().client
    send = Mock(side_effect=[rejected([1]), failure])
    monkeypatch.setattr(requests.Session, 'request', send)
    upload = client.upload_patch_embeddings if patches else client.upload_embeddings
    with pytest.warns(UserWarning, match='dropped 1'):
        with pytest.raises(requests.HTTPError):
            upload(1, [{'sample_id': sid, 'vector': [1, 0]} for sid in [1, 2, 3]])
    assert send.call_count == 2


@pytest.mark.parametrize('ids', [[], [99], ['1'], [True], None])
def test_unactionable_rejection_is_not_swallowed(monkeypatch, ids):
    client = dataset().client
    send = Mock(return_value=rejected(ids))
    monkeypatch.setattr(requests.Session, 'request', send)
    with pytest.raises(requests.HTTPError):
        client.upload_embeddings(1, [{'sample_id': 1, 'vector': [1, 0]}])
    assert send.call_count == 1


@pytest.mark.parametrize('patches', [False, True])
@pytest.mark.parametrize('shape', ['tensor', 'pooler_output', 'image_embeds'])
def test_compute_continues_after_deletion_and_reports_drops(monkeypatch, patches, shape):
    ds = dataset()
    samples = [Sample.from_row({'id': sid, 'filepath': f'{sid}.jpg'}) for sid in range(1, 5)]
    ds.iter_samples = Mock(return_value=iter(samples))
    ds.client.get_samples = Mock(
        side_effect=lambda dataset_id, limit, offset: {
            'total': 4,
            'items': [{'id': s.id, 'filepath': s.filepath} for s in samples[offset : offset + limit]],
        },
    )
    ds.client.register_field_model = Mock()
    deleted = set()
    written = []

    def loader(filepath):
        if filepath == '2.jpg':
            deleted.add(1)
        return Image.new('RGB', (4, 4))

    def upload(*args, **kwargs):
        batch = kwargs['json']
        missing = sorted({item['sample_id'] for item in batch} & deleted)
        if missing:
            return rejected(missing)
        written.extend(batch)
        return response(200, {'written': len(batch)})

    send = Mock(side_effect=upload)
    monkeypatch.setattr(requests.Session, 'request', send)

    class Features:
        def __init__(self, count):
            self.count = count

        def norm(self, **kwargs):
            return 1

        def __truediv__(self, other):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return np.tile([0.6, 0.8], (self.count, 1))

    class Inputs(dict):
        def to(self, device):
            return self

    class Model:
        def __init__(self, *args):
            pass

        def to(self, device):
            return self

        def encode(self, images, **kwargs):
            return Features(len(images)).numpy()

        def get_image_features(self, count):
            features = Features(count)
            return features if shape == 'tensor' else SimpleNamespace(**{shape: features})

    class Processor:
        def __call__(self, images, **kwargs):
            return Inputs(count=len(images))

    monkeypatch.setitem(sys.modules, 'sentence_transformers', SimpleNamespace(SentenceTransformer=Model))
    monkeypatch.setitem(
        sys.modules,
        'transformers',
        SimpleNamespace(
            CLIPModel=SimpleNamespace(from_pretrained=lambda name, **kwargs: Model()),
            CLIPProcessor=SimpleNamespace(from_pretrained=lambda name, **kwargs: Processor()),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        'torch',
        SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: False),
            no_grad=nullcontext,
            is_tensor=lambda value: isinstance(value, Features),
        ),
    )
    with pytest.warns(UserWarning, match='dropped'):
        if patches:
            result = ds.compute_patch_embeddings(
                image_loader=loader,
                batch_size=2,
                scales=['2x2'],
                reduce=False,
                num_threads=1,
            )
        else:
            result = ds.compute_embeddings(image_loader=loader, batch_size=2)
    multiplier = 4 if patches else 1
    assert result == {
        'total': 4 * multiplier,
        'written': 3 * multiplier,
        'dropped': multiplier,
        'dropped_sample_ids': [1],
    }
    assert {row['sample_id'] for row in written} == {2, 3, 4}
    assert send.call_count == 3
    if not patches:
        ds.client.register_field_model.assert_called_once()
