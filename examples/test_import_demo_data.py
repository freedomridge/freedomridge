"""Runner preflight, literal credentials, ordering, logs and failure propagation."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples import import_demo_data as runner


def test_preflight_and_order(tmp_path):
    args = runner.build_parser().parse_args(['--datasets', 'inaturalist,coco', '--data-root', str(tmp_path)])
    with pytest.raises(ValueError, match='credentials|FRIDGE_API_TOKEN'):
        runner.commands(args, {})
    _, commands = runner.commands(args, {'FRIDGE_API_TOKEN': 'fixture'})
    assert [name for name, _ in commands] == ['coco', 'inaturalist']
    assert all(command[command.index('--limit') + 1] == '100' for _, command in commands)
    args.full = True
    _, commands = runner.commands(args, {'FRIDGE_API_TOKEN': 'fixture'})
    assert all(command[command.index('--limit') + 1] == '0' for _, command in commands)
    args.data_root = None
    with pytest.raises(ValueError, match='FRIDGE_DATA_ROOT'):
        runner.commands(args, {'FRIDGE_API_TOKEN': 'fixture'})


def test_env_file_is_literal_and_existing_environment_wins(tmp_path):
    path = tmp_path / 'credentials.env'
    path.write_text("export FRIDGE_API_TOKEN='literal-$(touch sentinel)'\nFRIDGE_USERNAME=demo # comment\n")
    environment = {'FRIDGE_USERNAME': 'existing'}
    runner.load_env(path, environment)
    assert environment == {'FRIDGE_USERNAME': 'existing', 'FRIDGE_API_TOKEN': 'literal-$(touch sentinel)'}
    assert not Path('sentinel').exists()


def test_runner_logs_and_keeps_going_after_failure(monkeypatch, tmp_path):
    monkeypatch.setenv('FRIDGE_API_TOKEN', 'fixture')
    monkeypatch.delenv('FRIDGE_DATA_ROOT', raising=False)
    codes = iter([1, 0])

    def run(command, **kwargs):
        kwargs['stdout'].write('fixture importer output\n')
        assert kwargs['env']['HF_HUB_OFFLINE'] == '1'
        assert 'fixture' not in command
        return SimpleNamespace(returncode=next(codes))

    monkeypatch.setattr(subprocess, 'run', run)
    logs = tmp_path / 'logs'
    assert runner.main(['--data-root', str(tmp_path), '--datasets', 'coco,inaturalist', '--log-dir', str(logs)]) == 1
    status = (logs / 'status.txt').read_text()
    assert 'coco: FAIL' in status and 'inaturalist: PASS' in status
    assert (logs / 'coco.log').read_text().endswith('fixture importer output\n')


def test_kitti_selection_and_all_order(tmp_path):
    for selection in (['--all'], ['--datasets', 'eurosat,kitti,open_images']):
        args = runner.build_parser().parse_args(['--data-root', str(tmp_path), *selection])
        _, commands = runner.commands(args, {'FRIDGE_API_TOKEN': 'fixture'})
        names = [name for name, _ in commands]
        assert names.index('open_images') + 1 == names.index('kitti') == names.index('eurosat') - 1
        command = dict(commands)['kitti']
        assert command[2].endswith('/examples/kitti/ingest.py')
        assert command[command.index('--data-root') + 1] == str(tmp_path / 'kitti')


def test_media_uri_gets_one_folder_per_dataset(tmp_path):
    argv = [
        '--datasets',
        'coco,open_images,kitti',
        '--data-root',
        str(tmp_path),
        '--media-uri',
        's3://demo-media/media/',
    ]
    _, commands = runner.commands(runner.build_parser().parse_args(argv), {'FRIDGE_API_TOKEN': 'fixture'})
    uris = {name: command[command.index('--media-uri') + 1] for name, command in commands}
    assert uris == {
        'coco': 's3://demo-media/media/coco',
        'open_images': 's3://demo-media/media/open_images_v7',
        'kitti': 's3://demo-media/media/kitti',
    }
    _, commands = runner.commands(runner.build_parser().parse_args(argv[:4]), {'FRIDGE_API_TOKEN': 'fixture'})
    assert not any('--media-uri' in command for _, command in commands)


@pytest.mark.parametrize('datasets', ['bdd100k', 'tusimple', 'coco,tusimple'])
def test_media_uri_refuses_datasets_that_stay_local(tmp_path, datasets):
    argv = ['--datasets', datasets, '--data-root', str(tmp_path), '--media-uri', 's3://demo-media/media']
    with pytest.raises(ValueError, match='--media-uri cannot include'):
        runner.commands(runner.build_parser().parse_args(argv), {'FRIDGE_API_TOKEN': 'fixture'})


def test_bdd100k_and_tusimple_have_no_media_uri():
    from examples.bdd100k import ingest as bdd100k
    from examples.tusimple import ingest_lanes

    for build in (bdd100k.build_parser, ingest_lanes.parser):
        with pytest.raises(SystemExit):
            build().parse_args(['--data-root', '/unused', '--media-uri', 's3://demo-media/media'])
