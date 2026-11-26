"""sync_between_instances from a source that still stores metadata keys named tags or media_type: a server of this
version refuses them, so they are left out of the copy (the samples' own tags and media_type travel as before), and
the run says so in one line for the whole copy, naming the keys it met and the number of samples."""

from typing import Any

from fridge import sync_between_instances

from backend.tests.test_sync_between_instances_tags import FakeClient

ROWS: list[dict[str, Any]] = [
    {'id': 1, 'filepath': 'a.jpg', 'tags': ['own'], 'media_type': 'image', 'metadata': {'tags': 'x', 'n': 1}},
    {'id': 2, 'filepath': 'b.jpg', 'tags': [], 'media_type': 'image', 'metadata': {'n': 2, 'source_tags': 's'}},
    {'id': 3, 'filepath': 'c.jpg', 'tags': [], 'media_type': 'image', 'metadata': {'tags': ['y']}},
    {'id': 4, 'filepath': 'd.jpg', 'tags': [], 'media_type': 'image', 'metadata': {}},
    {'id': 5, 'filepath': 'e.jpg', 'tags': [], 'media_type': 'video', 'metadata': {'tags': 'z', 'media_type': 'scan'}},
]


class Source(FakeClient):
    def get_samples(self, dataset_id, limit, offset):
        return {'total': len(ROWS)}

    def get_schema(self, dataset_id, filters=None, lightning_mode=True):
        return []

    def list_array_fields(self, dataset_id):
        return []

    def query_samples(self, dataset_id, limit, offset):
        return {'items': [dict(row, metadata=dict(row['metadata'])) for row in ROWS[offset : offset + limit]]}


def sync(monkeypatch, capsys):
    monkeypatch.setattr(sync_between_instances, 'Client', Source)
    sync_between_instances.sync('source', 'target', 'demo', batch_size=2, skip_embeddings=True, skip_hashes=True)
    return Source.instances['target'].added, capsys.readouterr().out


def test_the_keys_are_left_out_and_said_once(monkeypatch, capsys):
    added, out = sync(monkeypatch, capsys)
    assert [row['metadata'] for row in added] == [{'n': 1}, {'n': 2, 'source_tags': 's'}, {}, {}, {}]
    assert [(row['tags'], row['media_type']) for row in added][::4] == [(['own'], 'image'), ([], 'video')]
    said = [line for line in out.splitlines() if 'Left out the metadata' in line]
    assert len(said) == 1, out  # three pages, one line
    assert said[0].strip().startswith('Left out the metadata keys media_type and tags of 3 samples:')


def test_nothing_is_said_without_them(monkeypatch, capsys):
    kept = [dict(row) for row in ROWS]
    ROWS[:] = [dict(row, metadata={'n': row['id']}) for row in kept]
    try:
        added, out = sync(monkeypatch, capsys)
    finally:
        ROWS[:] = kept
    assert len(added) == 5 and 'Left out the metadata' not in out
