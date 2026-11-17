"""sync_between_instances gives the dataset it creates on the target its source's visibility and everyone level.

A global source at edit makes a target every signed-in user can edit there too; one at view, a target at view; a
private source, a private target. A global source that reports no everyone_level (a server from before access levels)
gave every signed-in user edit, so its target is at edit. The script may set the level when it creates the dataset or
in the settings it sends after: the fake target applies both, as the server does, and the test reads what it holds.
"""

import pytest
from fridge import sync_between_instances

from backend.tests.test_sync_between_instances_tags import FakeClient


class Target(FakeClient):
    """The fake clients of test_sync_between_instances_tags, recording the target dataset as the server would hold it:
    created global at its everyone level (view when none is sent), changed by each settings PATCH."""

    source: dict = {}
    held: dict = {}

    def __init__(self, url):
        super().__init__(url)
        self.session.patch = self._patch

    def get_dataset(self, name):
        return {'id': 1, 'name': name, **Target.source} if self.base_url == 'source' else None

    def create_dataset(self, name, description=None, is_global=False, **kwargs):
        level = kwargs.get('everyone_level')
        Target.held = {'is_global': bool(is_global), 'everyone_level': level or 'view'}
        return {'id': 2, 'name': name, **Target.held}

    def _patch(self, url, json=None, **kwargs):
        for key in ('is_global', 'everyone_level'):
            if (json or {}).get(key) is not None:
                Target.held[key] = json[key]


@pytest.mark.parametrize(
    'source,wanted',
    [
        ({'is_global': True, 'everyone_level': 'edit'}, (True, 'edit')),
        ({'is_global': True, 'everyone_level': 'tag'}, (True, 'tag')),
        ({'is_global': True, 'everyone_level': 'view'}, (True, 'view')),
        ({'is_global': True}, (True, 'edit')),  # an older source server: global meant edit
        ({'is_global': False, 'everyone_level': 'view'}, (False, None)),
        ({'is_global': False}, (False, None)),
    ],
    ids=['global at edit', 'global at tag', 'global at view', 'global, no level', 'private', 'private, no level'],
)
def test_the_target_dataset_takes_the_sources_visibility_and_everyone_level(monkeypatch, source, wanted):
    monkeypatch.setattr(sync_between_instances, 'Client', Target)
    monkeypatch.setattr(Target, 'source', source)
    monkeypatch.setattr(Target, 'held', {})
    sync_between_instances.sync('source', 'target', 'demo', skip_embeddings=True, skip_hashes=True)
    if wanted[0]:
        assert (Target.held['is_global'], Target.held['everyone_level']) == wanted, Target.held
    else:
        assert Target.held['is_global'] is False, Target.held  # a private source's copy is private
