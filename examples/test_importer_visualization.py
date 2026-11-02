"""Importer maps use the RAM-scaled SDK path, including recovery and failures."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from examples._shared.importer import reduce_if_written


@pytest.fixture
def maps():
    client = Mock()
    client.reduce_embeddings.side_effect = AssertionError('Importer must never call server reduction')
    client.get_embedding_field_inventory.return_value = [{'field_name': 'clip', 'count': 210122}]
    client.get_vectors.return_value = {'items': [{'sample_id': 1}]}
    dataset = Mock(_id=7)
    dataset.compute_visualization.return_value = {'fit_sample_size': 150000, 'reduced': 210122}
    args = SimpleNamespace(dataset_name='older dataset', no_reduce=False, skip_embeddings=False)
    return client, dataset, args


def test_client_map_reports_fit_and_every_placed_point(maps, capsys):
    client, dataset, args = maps
    reduce_if_written(client, dataset, args, 'clip', written=20)
    dataset.compute_visualization.assert_called_once_with(field_name='clip')
    client.reduce_embeddings.assert_not_called()
    assert 'clip UMAP: fit 150,000; placed 210,122 points' in capsys.readouterr().out


def test_managed_map_is_a_note(maps, capsys):
    client, dataset, args = maps
    dataset.compute_visualization.return_value = {'skipped': True, 'reason': 'map_managed'}
    reduce_if_written(client, dataset, args, 'clip', written=20)
    assert 'Skipping clip UMAP: map managed by scheduled re-fits' in capsys.readouterr().out
    client.reduce_embeddings.assert_not_called()


def test_warn_keeps_import_and_prints_rebuild_command(maps, capsys):
    client, dataset, args = maps
    args.reduce_errors = 'warn'
    dataset.compute_visualization.side_effect = RuntimeError('fit failed')
    reduce_if_written(client, dataset, args, 'clip_obj', written=20)
    output = capsys.readouterr().out
    assert 'Samples are imported' in output
    assert 'fit failed' in output
    assert "Dataset('older dataset').compute_visualization(field_name='clip_obj')" in output
    client.reduce_embeddings.assert_not_called()


def test_map_failure_raises_by_default(maps):
    client, dataset, args = maps
    dataset.compute_visualization.side_effect = RuntimeError('fit failed')
    with pytest.raises(RuntimeError, match='fit failed'):
        reduce_if_written(client, dataset, args, 'clip', written=20)
    client.reduce_embeddings.assert_not_called()


def test_map_failure_prints_rebuild_command_before_raising(maps, capsys):
    client, dataset, args = maps
    dataset.compute_visualization.side_effect = RuntimeError('fit failed')
    with pytest.raises(RuntimeError):
        reduce_if_written(client, dataset, args, 'clip', written=20)
    assert "Dataset('older dataset').compute_visualization(field_name='clip')" in capsys.readouterr().out


def test_failed_ranking_is_not_reported_as_success(maps, capsys):
    client, dataset, args = maps
    dataset.compute_visualization.return_value = {'fit_sample_size': 20, 'reduced': 20, 'ranked': False}
    with pytest.raises(RuntimeError, match=r"recompute_viz_rank\(7, 'clip_obj', full=True\)"):
        reduce_if_written(client, dataset, args, 'clip_obj', written=20)
    assert 'placed' not in capsys.readouterr().out


def test_failed_ranking_warns_in_warn_mode(maps, capsys):
    client, dataset, args = maps
    args.reduce_errors = 'warn'
    dataset.compute_visualization.return_value = {'fit_sample_size': 20, 'reduced': 20, 'ranked': False}
    reduce_if_written(client, dataset, args, 'clip', written=20)
    assert 'ranking the map failed' in capsys.readouterr().out


@pytest.mark.parametrize('flag', ['no_reduce', 'skip_embeddings'])
def test_opt_out_skips_recovery_and_map(maps, flag):
    client, dataset, args = maps
    setattr(args, flag, True)
    reduce_if_written(client, dataset, args, 'clip', written=20, recover=True)
    dataset.compute_visualization.assert_not_called()
    assert client.mock_calls == []


def test_no_writes_preserves_map(maps, capsys):
    client, dataset, args = maps
    reduce_if_written(client, dataset, args, 'clip', written=0)
    dataset.compute_visualization.assert_not_called()
    assert 'no samples written' in capsys.readouterr().out


@pytest.mark.parametrize('count,pending,expected', [(4, True, False), (5, False, False), (5, True, True)])
def test_recovery_requires_five_embeddings_and_pending_coordinates(maps, count, pending, expected):
    client, dataset, args = maps
    client.get_embedding_field_inventory.return_value = [{'field_name': 'clip', 'count': count}]
    client.get_vectors.return_value = {'items': [1] if pending else []}
    reduce_if_written(client, dataset, args, 'clip', written=0, recover=True)
    if expected:
        dataset.compute_visualization.assert_called_once_with(field_name='clip')
    else:
        dataset.compute_visualization.assert_not_called()
    if count >= 5:
        client.get_vectors.assert_called_once_with(7, field_name='clip', limit=1, pending_only=True)
    else:
        client.get_vectors.assert_not_called()
    client.reduce_embeddings.assert_not_called()
