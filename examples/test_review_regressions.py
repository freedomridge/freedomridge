"""Fresh-clone regressions: no server, model download or inherited PYTHONPATH."""

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from fridge.core.client import Client

from examples._shared.importer import ClipEncoder
from examples.import_demo_data import build_parser, commands, load_env

REPO = Path(__file__).resolve().parents[1]
ENTRYPOINTS = sorted((REPO / 'examples').glob('*/ingest*.py')) + [
    REPO / 'examples/import_demo_data.py',
    REPO / 'examples/download_demo_data.py',
    REPO / 'examples/_shared/verify.py',
]
ENTRYPOINTS += [
    path
    for path in sorted((REPO / 'examples/sdk').glob('*.py'))
    if path.name not in ('__init__.py', 'test_sdk_unit.py')
]


@pytest.mark.parametrize('script', ENTRYPOINTS, ids=lambda p: str(p.relative_to(REPO)))
def test_script_help_without_pythonpath(script, tmp_path):
    environment = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    result = subprocess.run(
        [sys.executable, str(script), '--help'],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout


def test_verifier_refuses_optimized_python():
    result = subprocess.run(
        [
            sys.executable,
            '-O',
            str(REPO / 'examples/_shared/verify.py'),
            '--kind',
            'coco',
            '--dataset-name',
            'fixture',
            '--api-url',
            'http://127.0.0.1:18086/api',
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert 'requires assertions' in result.stderr


@pytest.mark.parametrize('shape', ['tensor', 'pooler_output', 'image_embeds'])
@pytest.mark.parametrize('normalize', [False, True])
def test_clip_model_output_shapes(shape, normalize):
    import torch

    vectors = torch.tensor([[3.0, 4.0]])
    output = vectors if shape == 'tensor' else SimpleNamespace(**{shape: vectors})
    encoder = ClipEncoder(SimpleNamespace(device='cpu'))
    encoder.device = 'cpu'
    encoder.model = SimpleNamespace(get_image_features=lambda **_: output)

    class Inputs(dict):
        def to(self, _):
            return self

    encoder.processor = lambda **_: Inputs()
    assert encoder([object()], normalize=normalize)[0] == pytest.approx([0.6, 0.8] if normalize else [3, 4])


@pytest.mark.parametrize('cuda_available', [False, True])
def test_auto_device_warns_for_unusable_cuda(monkeypatch, capsys, cuda_available):
    from examples._shared.importer import auto_device

    def broken(*_, **kwargs):
        raise RuntimeError('driver mismatch')

    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: cuda_available), empty=broken)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='GPU 0: fixture'))
    assert auto_device(torch) == 'cpu'
    warning = capsys.readouterr().err
    assert 'CUDA is unusable' in warning and 'nvidia-smi' in warning and 'CPU' in warning


def test_env_quotes_hashes_backslashes(tmp_path):
    path = tmp_path / 'import.env'
    path.write_text(
        "FRIDGE_PASSWORD=abc#def\\ghi # comment\n"
        "FRIDGE_USERNAME='adm\\in # literal'\n"
        'QUOTED="a\\\\b#c" # comment\n'
        'LITERAL=${FRIDGE_PASSWORD}\n',
    )
    environment: dict[str, str] = {}
    load_env(path, environment)
    assert environment == {
        'FRIDGE_PASSWORD': 'abc#def\\ghi',
        'FRIDGE_USERNAME': 'adm\\in # literal',
        'QUOTED': 'a\\b#c',
        'LITERAL': '${FRIDGE_PASSWORD}',
    }


@pytest.mark.parametrize('source', ['default', 'env', 'cli'])
def test_tusimple_sources(tmp_path, source):
    options = ['--datasets', 'tusimple', '--data-root', str(tmp_path)]
    environment = {'FRIDGE_API_TOKEN': 'fixture'}
    for benchmark in ('lanes', 'velocity'):
        if source == 'env':
            environment[f'FRIDGE_TUSIMPLE_{benchmark.upper()}_SOURCE'] = str(tmp_path / benchmark)
        if source == 'cli':
            options.extend([f'--tusimple-{benchmark}-source', str(tmp_path / benchmark)])
            environment[f'FRIDGE_TUSIMPLE_{benchmark.upper()}_SOURCE'] = '/ignored'
    _, runs = commands(build_parser().parse_args(options), environment)
    for name, command in runs:
        benchmark = name.split('_')[1]
        folder = 'TusimpleLaneDetection' if benchmark == 'lanes' else 'TusimpleVelocity'
        expected = tmp_path / 'tusimple' / folder if source == 'default' else tmp_path / benchmark
        assert command[command.index('--source-root') + 1] == str(expected)


def test_bearer_wins_over_netrc_and_keeps_proxies(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('NETRC', raising=False)
    (tmp_path / '.netrc').write_text('machine fixture.invalid login wrong password wrong\n')
    (tmp_path / '.netrc').chmod(0o600)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:1234')
    client = Client('https://fixture.invalid/api', api_token='correct')
    request = client.session.prepare_request(requests.Request('GET', client.base_url))
    assert request.headers['Authorization'] == 'Bearer correct'
    assert client.session.trust_env
    settings = client.session.merge_environment_settings(client.base_url, {}, False, None, None)
    assert settings['proxies']['https'] == 'http://proxy.invalid:1234'
    client.set_token('replacement')
    request = client.session.prepare_request(requests.Request('GET', client.base_url))
    assert request.headers['Authorization'] == 'Bearer replacement'
    response = requests.Response()
    response.request = request
    redirected = request.copy()
    redirected.url = client.base_url + '/datasets'
    client.session.rebuild_auth(redirected, response)
    assert redirected.headers['Authorization'] == 'Bearer replacement'
    redirected.url = 'https://another.invalid/api'
    client.session.rebuild_auth(redirected, response)
    assert 'Authorization' not in redirected.headers


@pytest.mark.parametrize('explicit', [False, True])
def test_synthetic_api_target(monkeypatch, explicit):
    module = importlib.import_module('examples.sdk.generate_synthetic_dataset')
    monkeypatch.setenv('FRIDGE_API_URL', 'http://127.0.0.1:18086/api')
    argv = ['generate_synthetic_dataset.py']
    if explicit:
        argv.extend(['--api-url', 'http://127.0.0.1:18087/api'])
    monkeypatch.setattr(sys, 'argv', argv)
    targets: list[str] = []
    monkeypatch.setattr(module, 'connect', targets.append)
    monkeypatch.setattr(module, 'login', lambda *_: None)
    monkeypatch.setattr(module, 'get_client', lambda: None)
    monkeypatch.setattr(module, 'Dataset', lambda _: SimpleNamespace(add_samples=lambda _: None))
    monkeypatch.setattr(
        module,
        'generate',
        lambda _: SimpleNamespace(fridge_samples=lambda: [], samples=[], out_dir='fixture', class_counts=lambda: {}),
    )
    module.main()
    assert targets == [f'http://127.0.0.1:{18087 if explicit else 18086}/api']


@pytest.mark.parametrize('second_copy', [False, True])
def test_compose_defaults_and_second_copy(second_copy):
    if not shutil.which('docker'):
        pytest.skip('Docker Compose is not installed')
    environment = {
        k: v for k, v in os.environ.items() if not k.startswith(('FRIDGE_DB_', 'FRIDGE_WORKER_', 'COMPOSE_'))
    }
    environment['COMPOSE_PROJECT_NAME'] = 'fixture2' if second_copy else 'freedomridge'
    if second_copy:
        environment.update(
            FRIDGE_DB_CONTAINER='fixture2-db',
            FRIDGE_DB_PORT='55486',
            FRIDGE_WORKER_CONTAINER='fixture2-worker',
        )
    result = subprocess.run(
        [
            'docker',
            'compose',
            '--env-file',
            '/dev/null',
            '-f',
            str(REPO / 'docker-compose.yml'),
            'config',
            '--format',
            'json',
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)
    db = config['services']['db']
    assert db['container_name'] == ('fixture2-db' if second_copy else 'freedomridge-db')
    assert db['ports'][0]['published'] == ('55486' if second_copy else '5432')
    assert config['volumes']['postgres_data']['name'] == (
        'fixture2_postgres_data' if second_copy else 'freedomridge_postgres_data'
    )


@pytest.mark.parametrize(
    'module,expected',
    [
        ('bdd100k', 'per split'),
        ('eurosat', 'per class'),
        ('osv5m', 'country round-robin'),
        ('inaturalist', 'source JSON order'),
    ],
)
def test_limit_help_and_unused_options(module, expected):
    parser = importlib.import_module(f'examples.{module}.ingest').build_parser()
    help_text = parser.format_help()
    assert expected in help_text
    assert '--work-dir' not in help_text


def test_credentials_ignored_anywhere_in_repo():
    paths = ['import.env', 'examples/import.env', 'examples/coco/credentials.env']
    result = subprocess.run(['git', 'check-ignore', *paths], cwd=REPO, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == paths


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize(
    'script,args',
    [
        ('compute_embeddings.py', ['--dataset', 'fixture']),
        ('compute_hashes.py', ['fixture']),
        ('compute_patch_embeddings.py', ['--dataset', 'fixture']),
        ('compute_visualization.py', ['fixture']),
        ('generate_synthetic_dataset.py', []),
        ('example_dataset_delete.py', []),
        ('query_api_demo.py', ['--dataset', 'fixture']),
        ('sdk_api.py', ['--dataset', 'fixture']),
        ('test_sdk_queries.py', ['--dataset', 'fixture']),
    ],
)
def test_every_sdk_api_recipe_uses_configured_target(script, args, explicit):
    harness = '''
import os
import runpy
import socket
import sys
from fridge.core.client import Client
class TargetCaptured(BaseException):
    pass
def capture(self, base_url=None, *a, **kw):
    assert (base_url or os.getenv('FRIDGE_API_URL')) == os.environ['EXPECTED_API']
    raise TargetCaptured
def blocked(*a, **kw):
    raise AssertionError('SDK target test attempted a network connection')
Client.__init__ = capture
socket.socket.connect = blocked
sys.argv = sys.argv[1:]
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
except TargetCaptured:
    pass
else:
    raise AssertionError('No API client was created')
'''
    expected = f'http://127.0.0.1:{18087 if explicit else 18086}/api'
    environment = dict(
        os.environ,
        PYTHONPATH=str(REPO / 'python'),
        FRIDGE_API_URL='http://127.0.0.1:18086/api',
        EXPECTED_API=expected,
        FRIDGE_API_TOKEN='fixture',
    )
    command = [sys.executable, '-c', harness, str(REPO / 'examples/sdk' / script), *args]
    if explicit:
        command.extend(['--api-url', expected])
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('size', [(81, 1), (1, 81), (3, 5), (5, 3), (1, 1), (224, 224)])
def test_clip_inputs_accept_thin_and_tiny_crops(size):
    """A 1-pixel-high COCO crop crashed the import: the processor misread the channel axis."""
    transformers = pytest.importorskip('transformers')
    from fridge.core.clip import pixel_inputs
    from PIL import Image

    inputs = pixel_inputs(transformers.CLIPImageProcessor(), [Image.new('RGB', size, (120, 30, 200))])
    assert tuple(inputs['pixel_values'].shape) == (1, 3, 224, 224)


def test_clone_keeps_a_code_set_token_ahead_of_netrc(tmp_path, monkeypatch):
    """The patch uploader's second client must not lose a token set in code (it would get netrc's)."""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('NETRC', raising=False)
    monkeypatch.delenv('FRIDGE_API_TOKEN', raising=False)
    (tmp_path / '.netrc').write_text('machine fixture.invalid login wrong password wrong\n')
    (tmp_path / '.netrc').chmod(0o600)
    client = Client('https://fixture.invalid/api')
    client.set_token('in-code')
    client.session.cookies.set('session_id', 'abc')
    copy = client.clone()
    assert copy is not client and copy.session is not client.session
    assert copy.base_url == client.base_url
    assert copy.get_auth_cookies() == {'session_id': 'abc'}
    request = copy.session.prepare_request(requests.Request('GET', copy.base_url))
    assert request.headers['Authorization'] == 'Bearer in-code'
