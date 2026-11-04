"""Dataset.load_view turns a saved view back into a DatasetView.

The app applies a saved view as the AND of its stages' filters (frontend FilterContext.globalFilters,
and python/fridge/test_integrity.py `resolve`). The stage filters are already server filter dicts, so
the SDK passes them through verbatim and refuses, naming the stage, anything it can't reproduce.
No server: a stub client serves the saved views and records the query the view sends.
"""

import copy

import pytest
from fridge.core.client import Client
from fridge.core.dataset import Dataset
from fridge.core.view import DatasetView, filters_from_view_stages


def stage(description, flt, type_='filter', **extra):
    return {'type': type_, 'description': description, 'filter': flt, **extra}


SPLIT_VAL = stage('split: val', {'field': 'split', 'operator': '=', 'value': 'val'})
IDX_GE_9 = stage('idx ≥ 9', {'field': 'idx', 'operator': '>=', 'value': 9})
TAGGED = stage('tags: keep', {'field': 'tags', 'operator': 'in', 'value': ['keep', 'review']})


class FakeResponse:
    ok = True
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, views):
        self.views, self.queries = views, []

    def get(self, url, **kw):
        assert url.endswith('/datasets/5/views'), url
        return FakeResponse(copy.deepcopy(self.views))

    def post(self, url, json=None, **kw):
        assert url.endswith('/datasets/5/samples/query'), url
        self.queries.append(json)
        return FakeResponse({'items': [], 'total': 42})


def dataset_with_views(*views):
    client = Client.__new__(Client)
    client.base_url = 'http://test.invalid/api'
    client.session = FakeSession([{'id': i, 'name': n, 'content': c} for i, (n, c) in enumerate(views, 1)])
    ds = Dataset.__new__(Dataset)
    ds._id, ds.name, ds.client, ds._samples_query = 5, 'crops', client, None
    return ds


def load(stages, name='eval'):
    return dataset_with_views((name, {'viewStages': stages})).load_view(name)


def test_returns_a_dataset_view_that_is_the_and_of_the_stages_in_order():
    view = load([SPLIT_VAL, IDX_GE_9])

    assert isinstance(view, DatasetView)
    assert view._filters == {
        'logic': 'AND',
        'filters': [
            {'field': 'split', 'operator': '=', 'value': 'val'},
            {'field': 'idx', 'operator': '>=', 'value': 9},
        ],
    }


def test_single_stage_keeps_the_and_wrapper_the_app_sends():
    assert load([TAGGED])._filters == {'logic': 'AND', 'filters': [TAGGED['filter']]}


def test_no_stages_is_every_sample():
    view = load([])
    assert view._filters is None
    assert len(view) == 42
    assert 'filters' not in view._dataset.client.session.queries[-1]


@pytest.mark.parametrize(
    'flt',
    [
        {'field': 'a', 'operator': '=', 'value': 'x'},
        {'field': 'a', 'operator': '!=', 'value': 'x'},
        {'field': 'a', 'operator': '>', 'value': 1},
        {'field': 'a', 'operator': '<', 'value': 1},
        {'field': 'a', 'operator': '>=', 'value': 1},
        {'field': 'a', 'operator': '<=', 'value': 1},
        {'field': 'a', 'operator': 'in', 'value': ['x', 'y']},
        {'field': 'a', 'operator': 'not in', 'value': ['x']},
        {'field': 'filepath', 'operator': 'contains', 'value': 'field_7'},
        {'field': 'tags', 'operator': 'in', 'value': ['keep']},
        {'field': 'a', 'operator': 'is_null', 'value': None},
        {'field': 'a', 'operator': 'exists', 'value': None},
        {'field': 'a', 'operator': 'not_exists', 'value': None},
        {'field': 'location', 'operator': 'geo_polygon', 'value': [[0, 0], [0, 1], [1, 1], [0, 0]]},
        {'field': 'location', 'operator': 'geo_polygon_exclude', 'value': [[0, 0], [0, 1], [1, 1], [0, 0]]},
        {'field': 'clip', 'operator': 'embedding_polygon', 'value': {'polygon': [[0, 0]], 'model_id': 'm'}},
        {'field': 'c', 'operator': 'concept_select', 'value': {'concept_id': 1, 'op': '>=', 'threshold': 0.5}},
        {'logic': 'OR', 'filters': [{'field': 'a', 'operator': 'not_exists', 'value': None}]},
        {'logic': 'NOT', 'filters': [{'field': 'a', 'operator': '=', 'value': 1}]},
        {'field': 'objects', 'operator': 'has_matching_object', 'value': {'field': 'cls', 'operator': '=', 'value': 1}},
    ],
)
def test_every_operator_the_frontend_can_save_passes_through_unchanged(flt):
    assert load([stage('x', flt)])._filters == {'logic': 'AND', 'filters': [flt]}


@pytest.mark.parametrize('type_', ['filter', 'selection', 'map_region', 'similarity'])
def test_all_four_stage_types_are_accepted(type_):
    flt = {'field': 'id', 'operator': 'in', 'value': [1, 2, 3]}
    assert load([stage('s', flt, type_=type_, metadata={'count': 3})])._filters['filters'] == [flt]


def test_stage_metadata_is_display_state_and_does_not_change_the_query():
    plain = load([SPLIT_VAL])._filters
    decorated = load([{**SPLIT_VAL, 'metadata': {'collapse': True, 'config': 'phash', 'count': 7}}])._filters
    assert plain == decorated


def test_the_view_sends_the_combined_filter_and_supports_chaining():
    view = load([SPLIT_VAL, IDX_GE_9])
    ds = view._dataset

    assert len(view) == 42
    assert ds.client.session.queries[-1]['filters'] == view._filters

    narrowed = view.filter(ds.samples.score > 0.5)
    assert narrowed._filters['logic'] == 'AND'
    assert narrowed._filters['filters'][0] == view._filters
    assert narrowed._filters['filters'][1] == {'field': 'score', 'operator': '>', 'value': 0.5}
    assert view._filters == load([SPLIT_VAL, IDX_GE_9])._filters  # chaining did not mutate the loaded view


def test_unknown_name_raises_and_lists_what_exists():
    ds = dataset_with_views(('eval', {'viewStages': [SPLIT_VAL]}), ('train', {'viewStages': []}))
    with pytest.raises(ValueError, match=r"No saved view named 'nope'.*\['eval', 'train'\]"):
        ds.load_view('nope')


def test_name_match_is_exact():
    ds = dataset_with_views(('eval', {'viewStages': [SPLIT_VAL]}))
    with pytest.raises(ValueError, match='No saved view'):
        ds.load_view('Eval')


def test_picks_the_named_view_among_several():
    ds = dataset_with_views(('a', {'viewStages': [SPLIT_VAL]}), ('b', {'viewStages': [IDX_GE_9]}))
    assert ds.load_view('b')._filters['filters'] == [IDX_GE_9['filter']]


def test_does_not_mutate_the_stored_view():
    stages = [SPLIT_VAL]
    snapshot = copy.deepcopy(stages)
    load(stages)
    assert stages == snapshot


def test_view_saved_before_stages_existed_uses_its_compiled_filter():
    compiled = {'logic': 'AND', 'filters': [SPLIT_VAL['filter']]}
    ds = dataset_with_views(('old', {'filters': compiled}))
    assert ds.load_view('old')._filters == compiled


def test_view_with_neither_stages_nor_filters_is_an_error():
    ds = dataset_with_views(('odd', {}))
    with pytest.raises(ValueError, match="'odd' has neither viewStages nor filters"):
        ds.load_view('odd')


def test_unknown_stage_type_names_the_stage():
    with pytest.raises(ValueError, match=r"'eval', stage 1 \('regex': 'my regex'\): unsupported stage type"):
        load([SPLIT_VAL, stage('my regex', {'field': 'a', 'operator': '=', 'value': 1}, type_='regex')])


def test_stage_without_a_filter_names_the_stage():
    with pytest.raises(ValueError, match=r"stage 0 \('filter': 'empty'\): the stage has no filter"):
        load([{'type': 'filter', 'description': 'empty'}])
    with pytest.raises(ValueError, match='the stage has no filter'):
        load([stage('empty', {})])


@pytest.mark.parametrize(
    'flt',
    [
        {'field': 'id', 'operator': 'in_selection', 'value': 'tok_123'},
        {'logic': 'AND', 'filters': [{'field': 'id', 'operator': 'in_selection', 'value': 'tok_123'}]},
        {'field': '_cgself', 'operator': 'cg_self', 'value': {'field': 'id', 'operator': 'in_selection', 'value': 't'}},
    ],
)
def test_temporary_selection_is_refused_not_dropped(flt):
    with pytest.raises(ValueError, match=r"stage 1 \('selection': '60000 Selected'\).*temporary selection"):
        load([SPLIT_VAL, stage('60000 Selected', flt, type_='selection')])


def test_malformed_view_stages_are_an_error():
    with pytest.raises(ValueError, match='viewStages must be a list'):
        filters_from_view_stages('eval', {'type': 'filter'})
    with pytest.raises(ValueError, match='stage 0: expected an object'):
        filters_from_view_stages('eval', ['x'])
