"""The SDK describes the fields compute_metadata() and compute_metrics() write, in the run that writes them when it
skipped no sample for already having its values, where a field has none of its own; the server describes by name
only the built-in fields. Unit checks with a stand-in client (no server)."""

import warnings
from types import SimpleNamespace

import fridge
import fridge.core.dataset as dataset_module
import pytest
import requests
from fridge.core.dataset import Dataset
from PIL import Image

from backend.field_descriptions import BUILT_IN_FIELDS, DEFAULTS, described

METRICS = {
    'brightness': 1.0,
    'blurriness': 2.0,
    'contrast': 3.0,
    'entropy': 4.0,
    'salt_and_pepper': 5.0,
    'saturation': 6.0,
    'pct_underexposed': 7.0,
    'pct_overexposed': 8.0,
}


class Client:
    """Answers the schema with `schema` (or raises `unreadable`) and records each description written; `refuse`
    makes every write a 403."""

    def __init__(self, schema=(), refuse=False, unreadable=None):
        self.schema = list(schema)
        self.refuse = refuse
        self.unreadable = unreadable
        self.reads = 0
        self.writes = []

    def get_schema(self, dataset_id, filters=None):
        self.reads += 1
        if self.unreadable is not None:
            raise self.unreadable
        return self.schema

    def set_field_description(self, dataset_id, field, description=None, unit=None):
        if self.refuse:
            response = requests.Response()
            response.status_code = 403
            raise requests.HTTPError('403 Forbidden: This needs edit access to the dataset', response=response)
        self.writes.append((field, description, unit))
        return {}


def a_dataset(monkeypatch, client, samples):
    ds = object.__new__(Dataset)
    monkeypatch.setattr(ds, 'client', client, raising=False)
    monkeypatch.setattr(ds, '_id', 1, raising=False)
    stored = []
    monkeypatch.setattr(ds, 'iter_samples', lambda **kwargs: iter(samples), raising=False)
    monkeypatch.setattr(ds, 'set_values_per_sample_multi', lambda pending: stored.append(dict(pending)), raising=False)
    monkeypatch.setattr(ds, 'refresh_stats', lambda: None, raising=False)
    return ds, stored


def metrics(monkeypatch):
    monkeypatch.setattr(dataset_module, '_require_metric_dependencies', lambda: None)
    monkeypatch.setattr(dataset_module, 'compute_image_metrics', lambda img: dict(METRICS))


def test_the_server_describes_by_name_only_the_built_in_fields():
    assert DEFAULTS.keys() == BUILT_IN_FIELDS
    for name in fridge.COMPUTED_FIELD_DESCRIPTIONS:
        assert described(name, None) == {
            'description': None,
            'unit': None,
            'values_private': False,
            'description_source': None,
        }


def test_the_sdk_exports_the_thirteen_texts():
    texts = fridge.COMPUTED_FIELD_DESCRIPTIONS
    assert len(texts) == 13 and not texts.keys() & BUILT_IN_FIELDS
    assert set(METRICS) | {'width', 'height', 'aspect_ratio', 'megapixels', 'file_size_bytes'} == texts.keys()
    assert texts['brightness'][1] is None and texts['width'][1] == 'px' and texts['entropy'][1] == 'bits'
    assert 'COMPUTED_FIELD_DESCRIPTIONS' in fridge.__all__


def test_compute_metrics_describes_every_metric_it_wrote(monkeypatch):
    metrics(monkeypatch)
    client = Client()
    ds, stored = a_dataset(monkeypatch, client, [SimpleNamespace(id=1, filepath='a', metadata={})])
    assert ds.compute_metrics(image_loader=lambda path: Image.new('RGB', (4, 4))) is None
    assert stored == [{1: METRICS}]
    assert client.writes == [(name, *fridge.COMPUTED_FIELD_DESCRIPTIONS[name]) for name in METRICS]


def test_what_someone_wrote_is_kept_and_each_of_description_and_unit_is_filled_on_its_own(monkeypatch):
    metrics(monkeypatch)
    schema = [
        {'name': 'entropy', 'description': None, 'unit': 'nats'},
        {'name': 'salt_and_pepper', 'description': 'Ours.', 'unit': None},
        {'name': 'pct_overexposed', 'description': 'Ours.', 'unit': 'share'},
        {'name': 'brightness', 'description': 'Ours, 0 to 1.', 'unit': None},
    ]
    client = Client(schema)
    ds, _ = a_dataset(monkeypatch, client, [SimpleNamespace(id=1, filepath='a', metadata={})])
    ds.compute_metrics(image_loader=lambda path: Image.new('RGB', (4, 4)))
    written = {field: (description, unit) for field, description, unit in client.writes}
    assert written['entropy'] == (fridge.COMPUTED_FIELD_DESCRIPTIONS['entropy'][0], None)
    assert written['salt_and_pepper'] == (None, '%')
    assert 'pct_overexposed' not in written and 'brightness' not in written
    assert written['contrast'] == fridge.COMPUTED_FIELD_DESCRIPTIONS['contrast']


def test_a_run_that_wrote_nothing_describes_nothing(monkeypatch):
    metrics(monkeypatch)
    client = Client()
    ds, stored = a_dataset(monkeypatch, client, [SimpleNamespace(id=1, filepath='a', metadata={'brightness': 0.4})])
    ds.compute_metrics(image_loader=lambda path: Image.new('RGB', (4, 4)))
    assert stored == [] and client.reads == 0 and client.writes == []


def test_a_run_that_skipped_a_sample_with_its_values_describes_nothing(monkeypatch):
    metrics(monkeypatch)
    for compute, own in (('compute_metrics', {'brightness': 0.4}), ('compute_metadata', {'width': 1})):
        client = Client()
        samples = [SimpleNamespace(id=1, filepath='a', metadata=own), SimpleNamespace(id=2, filepath='b', metadata={})]
        ds, stored = a_dataset(monkeypatch, client, samples)
        getattr(ds, compute)(image_loader=lambda path: Image.new('RGB', (4, 4)))
        assert [list(batch) for batch in stored] == [[2]], compute  # the other sample is written
        assert client.reads == 0 and client.writes == [], compute


def test_an_unreadable_image_is_no_such_skip(monkeypatch):
    metrics(monkeypatch)
    client = Client()
    samples = [SimpleNamespace(id=1, filepath='missing', metadata={}), SimpleNamespace(id=2, filepath='b', metadata={})]
    ds, _ = a_dataset(monkeypatch, client, samples)

    def load(path):
        if path == 'missing':
            raise FileNotFoundError(path)
        return Image.new('RGB', (4, 4))

    ds.compute_metrics(image_loader=load)
    assert [field for field, _, _ in client.writes] == list(METRICS)


def test_a_field_an_unwritten_sample_already_holds_is_not_described(monkeypatch, tmp_path):
    metrics(monkeypatch)
    client = Client()
    samples = [
        SimpleNamespace(id=1, filepath='missing', metadata={'brightness': 0.5}),  # the user's own, 0 to 1
        SimpleNamespace(id=2, filepath='b', metadata={}),
    ]
    ds, stored = a_dataset(monkeypatch, client, samples)

    def load(path):
        if path == 'missing':
            raise FileNotFoundError(path)
        return Image.new('RGB', (4, 4))

    ds.compute_metrics(image_loader=load, skip_existing=False)
    assert [list(batch) for batch in stored] == [[2]]
    assert [field for field, _, _ in client.writes] == [name for name in METRICS if name != 'brightness']

    # compute_metadata writes file_size_bytes for local files only: an s3:// sample's own value keeps it undescribed.
    local = tmp_path / 'c.png'
    Image.new('RGB', (8, 4)).save(local)
    client = Client()
    samples = [
        SimpleNamespace(id=3, filepath='s3://bucket/d.jpg', metadata={'file_size_bytes': 7}),
        SimpleNamespace(id=4, filepath=str(local), metadata={}),
    ]
    ds, stored = a_dataset(monkeypatch, client, samples)
    ds.compute_metadata(image_loader=lambda path: Image.new('RGB', (8, 4)))
    assert 'file_size_bytes' in stored[0][4] and 'file_size_bytes' not in stored[0][3]
    assert [field for field, _, _ in client.writes] == ['width', 'height', 'aspect_ratio', 'megapixels']


def test_a_value_the_run_overwrote_is_vouched_for(monkeypatch):
    metrics(monkeypatch)
    client = Client()
    samples = [SimpleNamespace(id=1, filepath='a', metadata={'brightness': 0.5, 'contrast': 0.1})]
    ds, _ = a_dataset(monkeypatch, client, samples)
    ds.compute_metrics(image_loader=lambda path: Image.new('RGB', (4, 4)), skip_existing=False)
    assert [field for field, _, _ in client.writes] == list(METRICS)


def test_compute_metadata_describes_only_the_fields_it_wrote(monkeypatch, tmp_path):
    client = Client()
    ds, _ = a_dataset(monkeypatch, client, [SimpleNamespace(id=1, filepath='s3://bucket/a.jpg', metadata={})])
    ds.compute_metadata(image_loader=lambda path: Image.new('RGB', (8, 4)))
    assert [field for field, _, _ in client.writes] == ['width', 'height', 'aspect_ratio', 'megapixels']

    local = tmp_path / 'b.png'
    Image.new('RGB', (8, 4)).save(local)
    client = Client()
    ds, _ = a_dataset(monkeypatch, client, [SimpleNamespace(id=2, filepath=str(local), metadata={})])
    ds.compute_metadata(image_loader=lambda path: Image.open(path))
    assert ('file_size_bytes', *fridge.COMPUTED_FIELD_DESCRIPTIONS['file_size_bytes']) in client.writes


def test_a_refusal_keeps_the_values_and_warns_once_naming_the_fields(monkeypatch):
    metrics(monkeypatch)
    client = Client(refuse=True)
    ds, stored = a_dataset(monkeypatch, client, [SimpleNamespace(id=1, filepath='a', metadata={})])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        assert ds.compute_metrics(image_loader=lambda path: Image.new('RGB', (4, 4))) is None
    assert stored == [{1: METRICS}]
    assert len(caught) == 1
    message = str(caught[0].message)
    assert all(name in message for name in METRICS) and 'edit access' in message


@pytest.mark.parametrize('failure', [requests.ConnectionError('refused'), requests.HTTPError('404 Not Found')])
def test_a_failed_schema_read_warns_too(monkeypatch, failure):
    metrics(monkeypatch)
    client = Client(unreadable=failure)
    ds, _ = a_dataset(monkeypatch, client, [SimpleNamespace(id=1, filepath='a', metadata={})])
    with pytest.warns(UserWarning, match='could not describe brightness'):
        ds.compute_metrics(image_loader=lambda path: Image.new('RGB', (4, 4)))
    assert client.writes == []
