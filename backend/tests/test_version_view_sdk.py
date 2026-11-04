"""dataset.version() returns a read-only view whose queries are sent to the version endpoints."""

from unittest.mock import Mock

import pytest
from fridge import F
from fridge.core.dataset import Dataset
from fridge.core.version_view import VersionReadOnlyError, VersionView

VERSIONS = [
    {'id': 12, 'name': 'v2', 'include_metadata': True},
    {'id': 11, 'name': 'v1', 'include_metadata': True},
    {'id': 10, 'name': 'hashes', 'include_metadata': False},
]


@pytest.fixture
def dataset():
    ds = Dataset.__new__(Dataset)
    ds._id = 7
    ds._name = 'train'
    ds.client = Mock()
    ds.client.list_versions.return_value = VERSIONS
    return ds


def test_version_resolves_name_id_and_newest(dataset):
    assert dataset.version('v1').version_id == 11
    assert dataset.version(version_id=12).name == 'v2'
    assert dataset.version().name == 'v2'
    with pytest.raises(ValueError, match="'nope' not found"):
        dataset.version('nope')
    with pytest.raises(ValueError, match='hash-only'):
        dataset.version('hashes')
    dataset.client.list_versions.return_value = []
    with pytest.raises(ValueError, match='not found'):
        dataset.version()


def test_aggregates_go_to_the_version_endpoint_with_filters(dataset):
    view = dataset.version('v1').filter(F('split').is_in(['train', 'val']))
    dataset.client.aggregate_version.return_value = [{'op': 'count', 'field': 'id', 'value': 5}]
    assert view.count() == 5 and len(view) == 5
    dataset.client.aggregate_version.assert_called_with(
        7,
        11,
        [{'field': 'id', 'op': 'count'}],
        filters={'field': 'split', 'operator': 'in', 'value': ['train', 'val']},
    )
    dataset.client.aggregate_version.return_value = [{'value': ['a', 'b']}]
    assert view.distinct('source_id', limit=2) == ['a', 'b']
    assert dataset.client.aggregate_version.call_args.args[2] == [{'field': 'source_id', 'op': 'distinct', 'limit': 2}]
    dataset.client.aggregate_version.return_value = [
        {'value': [{'value': 'corn', 'count': 3}, {'value': 'soy', 'count': 1}]},
    ]
    assert view.count_values('category') == {'corn': 3, 'soy': 1}
    dataset.client.aggregate_version.return_value = [{'value': 5}]
    assert view.limit(2).count() == 2


def test_filters_chain_without_mutating_the_parent(dataset):
    base = dataset.version('v1')
    one = base.filter(F('a') == 1)
    both = one.filter(F('b') == 2).exclude(F('c') == 3)
    assert base._filters is None
    assert one._filters == {'field': 'a', 'operator': '=', 'value': 1}
    assert both._filters['logic'] == 'AND' and len(both._filters['filters']) == 2


def test_iteration_pages_by_id_cursor_and_yields_read_only_samples(dataset):

    def page(ids):
        return [{'id': i, 'filepath': f'{i}.jpg', 'metadata': {'k': i}, 'version_id': 11} for i in ids]

    dataset.client.query_version_samples.side_effect = [page([1, 2]), page([3, 4]), page([5])]
    samples = list(dataset.version('v1').select_fields(['k']).iter_samples(batch_size=2))
    assert [s.id for s in samples] == [1, 2, 3, 4, 5]
    calls = dataset.client.query_version_samples.call_args_list
    assert [c.kwargs['after_id'] for c in calls] == [None, 2, 4]
    assert all(c.kwargs['fields'] == ['k'] and c.kwargs['limit'] == 2 for c in calls)
    with pytest.raises(ValueError, match='read-only'):
        samples[0].save()


def test_limit_stops_paging_exactly(dataset):
    dataset.client.query_version_samples.side_effect = [
        [{'id': 1, 'filepath': 'a'}, {'id': 2, 'filepath': 'b'}],
        [{'id': 3, 'filepath': 'c'}],
    ]
    assert [s.id for s in dataset.version('v1').limit(3).iter_samples(batch_size=2)] == [1, 2, 3]
    assert dataset.client.query_version_samples.call_args_list[1].kwargs['limit'] == 1
    dataset.client.query_version_samples.side_effect = [[]]
    assert dataset.version('v1').first() is None


def test_writes_and_unsupported_operations_raise_clearly(dataset):
    view = dataset.version('v1')
    for write in ('tag_samples', 'untag_samples', 'set_values', 'delete_samples'):
        with pytest.raises(VersionReadOnlyError, match='read-only'):
            getattr(view, write)
    with pytest.raises(NotImplementedError, match='order_by'):
        view.order_by
    with pytest.raises(AttributeError):
        view.no_such_thing
    assert isinstance(view, VersionView)
    dataset.client.query_version_samples.assert_not_called()
