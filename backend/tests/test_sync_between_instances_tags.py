"""sync_between_instances must send `tags` as the built-in column, never as a dynamic array field.

Every database registers `tags` in sample_array_columns, so GET /array-fields lists it. Sending it
under `array_fields` made the target answer 422 "Unregistered array fields: ['tags']" for any
dataset with tagged samples.
"""

from types import SimpleNamespace

from fridge import sync_between_instances


class FakeClient:
    instances: dict = {}

    def __init__(self, url):
        self.base_url = url
        self.added: list = []
        self.created_fields: list = []
        self.session = SimpleNamespace(patch=lambda *a, **k: None)
        FakeClient.instances[url] = self

    def login(self, user, password):
        pass

    def get_dataset(self, name):
        return {'id': 1, 'name': name} if self.base_url == 'source' else None

    def create_dataset(self, name, description=None, is_global=False):
        return {'id': 2, 'name': name}

    def get_samples(self, dataset_id, limit, offset):
        return {'total': 1}

    def get_schema(self, dataset_id, filters=None, lightning_mode=True):
        # The source is a mirror with a field of its own.
        return [{'name': 'crop', 'origin': 'metadata'}, {'name': '@local.weeds', 'origin': 'local'}]

    def list_array_fields(self, dataset_id):
        # A fresh target already lists only the seeded `tags`.
        return [{'name': 'class_names'}, {'name': 'tags'}] if self.base_url == 'source' else [{'name': 'tags'}]

    def create_array_field(self, dataset_id, name):
        self.created_fields.append(name)

    def query_samples(self, dataset_id, limit, offset):
        row = {'id': 7, 'filepath': '/data/a.jpg', 'tags': ['val'], 'metadata': {}, 'class_names': ['cat']}
        return {'items': [row] if offset == 0 else []}

    def add_samples(self, dataset_id, payload, on_duplicate):
        self.added.extend(payload)

    def lookup_samples(self, dataset_id, field, values):
        return {value: 70 for value in values}


def test_tags_travel_as_the_builtin_column(monkeypatch, capsys):
    monkeypatch.setattr(sync_between_instances, 'Client', FakeClient)
    sync_between_instances.sync('source', 'target', 'demo', skip_embeddings=True, skip_hashes=True)
    assert "Left out, 1 field(s) of the source server's own: @local.weeds" in capsys.readouterr().out
    target = FakeClient.instances['target']
    assert target.created_fields == ['class_names']
    [row] = target.added
    assert row['tags'] == ['val']
    assert row['array_fields'] == {'class_names': ['cat']}
