"""Exercise the launcher with synthetic peers and a Python stub; never launch a server."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = 'http://localhost:3000,http://localhost:5173,http://localhost'
# Synthetic CGNAT fixtures only; no real network identifiers.
STATUS = '''100.64.0.10 devbox devbox.example.ts.net linux -
100.64.0.11 build-one owner linux -
100.64.0.12 temporary-two owner linux -
100.64.0.13 peer owner linux -
192.0.2.1 ignored owner linux -
'''


def launch(tmp_path, *, enabled=None, skip='', status=STATUS, port='3000'):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (tmp_path / '.env').write_text(f'ALLOWED_ORIGINS={BASE}\n')
    (tmp_path / 'status').write_text(status)
    # A glob must not expand into a matching filename before it tests a peer name.
    (tmp_path / 'build-local-file').touch()
    programs = {
        'tailscale': '#!/bin/sh\necho called >> "$ROOT_DIR/calls"\ncat "$ROOT_DIR/status"\n',
        'python3': '#!/bin/sh\n[ "$1" = "-c" ] && exit 0\nprintf "%s" "$ALLOWED_ORIGINS" > "$ROOT_DIR/origins"\n',
    }
    for name, source in programs.items():
        path = bin_dir / name
        path.write_text(source)
        path.chmod(0o755)
    environment = {
        'PATH': f'{bin_dir}:/usr/bin:/bin',
        'ROOT_DIR': str(tmp_path),
        'FRIDGE_LOG_FILE': '',
        'FRIDGE_TAILSCALE_SKIP': skip,
        'FRIDGE_FRONTEND_PORT': port,
    }
    if enabled is not None:
        environment['FRIDGE_TAILSCALE_ORIGINS'] = enabled
    subprocess.run(['bash', str(ROOT / 'scripts/run_backend.sh')], env=environment, check=True, capture_output=True)
    return (tmp_path / 'origins').read_bytes()


@pytest.mark.parametrize('enabled', [None, '0', 'true'])
def test_discovery_is_strictly_opt_in(tmp_path, enabled):
    assert launch(tmp_path, enabled=enabled) == BASE.encode()
    assert not (tmp_path / 'calls').exists()


@pytest.mark.parametrize('skip', ['', 'build-*', 'build-* temporary-*'])
def test_discovery_preserves_order_and_skips_globs(tmp_path, skip):
    expected = BASE
    for number, host in [(10, 'devbox'), (11, 'build-one'), (12, 'temporary-two'), (13, 'peer')]:
        if host == 'build-one' and skip or host == 'temporary-two' and 'temporary-*' in skip:
            continue
        expected += f',http://100.64.0.{number}:4300,http://{host}:4300,http://{host}.example.ts.net:4300'
    assert launch(tmp_path, enabled='1', skip=skip, port='4300') == expected.encode()
    assert (tmp_path / 'calls').read_text() == 'called\n'


def test_empty_status_keeps_configured_origins(tmp_path):
    assert launch(tmp_path, enabled='1', status='') == BASE.encode()


def test_without_dns_suffix_only_ip_and_short_name_are_added(tmp_path):
    assert (
        launch(tmp_path, enabled='1', status='100.64.0.10 devbox owner linux -\n')
        == (BASE + ',http://100.64.0.10:3000,http://devbox:3000').encode()
    )
