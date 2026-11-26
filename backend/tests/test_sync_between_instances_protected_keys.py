"""sync_between_instances from a source that has not been upgraded (protected-keys contract, 1.6): its samples may
carry a metadata key named `tags` or `media_type`, which the target refuses. The copy leaves those keys out, keeps
every other key, and says so once, with a count.

Read this way: "once" is one line of the run's output for the whole run, however many pages the samples come in;
"with a count" is the number of samples a key was left out of (the line gives it), or the number of each key's
samples, read from the line.
"""

import re
from types import SimpleNamespace

from fridge import sync_between_instances

# The source's samples: (id, metadata). Three carry a protected key, one under another key only (kept).
SOURCE: list[tuple[int, dict]] = [
    (1, {'crop': 'corn', 'tags': 'night'}),
    (2, {'crop': 'rye', 'media_type': 'video'}),
    (3, {'crop': 'oat', 'tags': ['a', 'b']}),
    (4, {'crop': 'corn', 'info': {'tags': 'kept'}, 'source_tags': 'kept'}),
    (5, {'crop': 'rye'}),
]


class FakeClient:
    instances: dict = {}

    def __init__(self, url):
        self.base_url = url
        self.added: list = []
        self.session = SimpleNamespace(patch=lambda *a, **k: None)
        FakeClient.instances[url] = self

    def login(self, user, password):
        pass

    def get_dataset(self, name):
        return {'id': 1, 'name': name} if self.base_url == 'source' else None

    def create_dataset(self, name, description=None, is_global=False, **options):
        return {'id': 2, 'name': name}

    def get_samples(self, dataset_id, limit, offset):
        return {'total': len(SOURCE)}

    def get_schema(self, dataset_id, filters=None, lightning_mode=True):
        return [{'name': 'crop', 'origin': 'metadata'}]

    def list_array_fields(self, dataset_id):
        return [{'name': 'tags'}]

    def create_array_field(self, dataset_id, name):
        pass

    def query_samples(self, dataset_id, limit, offset):
        rows = [
            {
                'id': sample_id,
                'filepath': f'/data/{sample_id}.jpg',
                'tags': ['own'],
                'media_type': 'image',
                'metadata': dict(metadata),
            }
            for sample_id, metadata in SOURCE
        ]
        return {'items': rows[offset : offset + limit]}

    def add_samples(self, dataset_id, payload, on_duplicate):
        for row in payload:
            assert not {'tags', 'media_type'} & set(row['metadata']), row  # the target would refuse the batch
        self.added.extend(payload)

    def lookup_samples(self, dataset_id, field, values):
        return {value: 100 + index for index, value in enumerate(values)}


def test_a_source_whose_samples_carry_a_protected_key_is_copied_without_it_and_says_so_once(monkeypatch, capsys):
    monkeypatch.setattr(sync_between_instances, 'Client', FakeClient)
    FakeClient.instances.clear()
    sync_between_instances.sync('source', 'target', 'demo', batch_size=2, skip_embeddings=True, skip_hashes=True)
    output = capsys.readouterr().out
    target = FakeClient.instances['target']
    assert len(target.added) == len(SOURCE)
    for row, (_, metadata) in zip(target.added, SOURCE):
        assert row['metadata'] == {key: value for key, value in metadata.items() if key not in ('tags', 'media_type')}
        assert (row['tags'], row['media_type']) == (['own'], 'image')  # the sample's own fields, as before
    said = [
        line for line in output.splitlines() if re.search(r'\b(tags|media_type)\b', line) and re.search(r'\d', line)
    ]
    assert len(said) == 1, f'not said once with a count: {said}'
    numbers = set(re.findall(r'\d+', said[0]))
    assert '3' in numbers or {'2', '1'} <= numbers, said[0]
