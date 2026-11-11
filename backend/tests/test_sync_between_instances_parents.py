"""sync_between_instances must give an object sample its parent's id on the target, not the source's.

An object's metadata.parent_id is its image's sample id on the source. The target numbers its samples itself,
so copied as it was, the object pointed at whatever target sample had that id, or none.
"""

import os
from types import SimpleNamespace

import pytest
from fridge import sync_between_instances

from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable Postgres')

# Source samples by id: objects 3 and 4 of image 9 (3 is paged before its parent, 4 after it), and object 5,
# whose parent isn't in the dataset.
SOURCE = [
    {'id': 3, 'filepath': 'o3', 'media_type': 'object', 'metadata': {'parent_id': 9, 'label': 'weed'}},
    {'id': 9, 'filepath': 'i9', 'media_type': 'image', 'metadata': {}},
    {'id': 4, 'filepath': 'o4', 'media_type': 'object', 'metadata': {'parent_id': '9'}},
    {'id': 5, 'filepath': 'o5', 'media_type': 'object', 'metadata': {'parent_id': 77}},
]


class FakeClient:
    instances: dict = {}

    def __init__(self, url):
        self.base_url = url
        self.added: list = []
        self.updates: list = []
        self.session = SimpleNamespace(patch=lambda *a, **k: None)
        FakeClient.instances[url] = self

    def login(self, user, password):
        pass

    def get_dataset(self, name):
        return {'id': 1, 'name': name} if self.base_url == 'source' else None

    def create_dataset(self, name, description=None, is_global=False):
        return {'id': 2, 'name': name}

    def get_samples(self, dataset_id, limit, offset):
        return {'total': len(SOURCE)}

    def get_schema(self, dataset_id, filters=None, lightning_mode=True):
        return []  # no fields of the source server's own (@local.)

    def list_array_fields(self, dataset_id):
        return [{'name': 'tags'}]

    def query_samples(self, dataset_id, limit, offset):
        return {'items': SOURCE[offset : offset + limit]}

    def add_samples(self, dataset_id, payload, on_duplicate):
        self.added.extend(payload)

    def lookup_samples(self, dataset_id, field, values):
        # The target gives each sample its own id: 100 more than the source's.
        return {value: 100 + int(value[1:]) for value in values}

    def bulk_set_metadata(self, dataset_id, updates=None, **kwargs):
        self.updates.extend(updates)
        return {'updated_count': len(updates)}


def test_objects_get_their_parent_s_id_on_the_target(monkeypatch, capsys):
    monkeypatch.setattr(sync_between_instances, 'Client', FakeClient)
    sync_between_instances.sync('source', 'target', 'demo', batch_size=2, skip_embeddings=True, skip_hashes=True)
    target = FakeClient.instances['target']
    sent = {row['filepath']: row['metadata'] for row in target.added}
    # Sent with the target's id when the parent is already there, else without one until the second pass.
    assert sent == {'o3': {'label': 'weed'}, 'i9': {}, 'o4': {'parent_id': 109}, 'o5': {}}
    # Every object then gets its parent's target id, or none when its parent wasn't copied (not the source's id).
    assert sorted(target.updates, key=lambda u: u['sample_id']) == [
        {'sample_id': 103, 'metadata': {'parent_id': 109}},
        {'sample_id': 104, 'metadata': {'parent_id': 109}},
        {'sample_id': 105, 'metadata': {'parent_id': None}},
    ]
    assert "1 object's parent isn't in the dataset" in capsys.readouterr().out


@postgres
def test_an_orphan_s_empty_parent_is_stored_as_no_parent(registry_database):
    async def scenario(database):
        image = (await ingest([{}], prefix='s3://images/i'))['ids'][0]
        await ingest([{}], prefix='s3://images/o')
        child = await database.fetchval("SELECT id FROM samples WHERE filepath LIKE 's3://images/o%'")
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(updates=[datasets.BulkSetUpdate(sample_id=child, metadata={'parent_id': image})]),
            USER,
        )
        # The second pass's update for an object whose parent wasn't copied.
        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(updates=[datasets.BulkSetUpdate(sample_id=child, metadata={'parent_id': None})]),
            USER,
        )
        stored = await database.fetchval("SELECT metadata->>'parent_id' FROM samples WHERE id = $1", child)
        assert stored is None  # JSON null: the parent filters (parent_id ~ '^-?[0-9]+$') see no parent

    registry_database(scenario)
