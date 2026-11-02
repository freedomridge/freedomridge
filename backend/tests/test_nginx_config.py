"""Render the production nginx configuration without Docker or a running server."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def render(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    nginx = bindir / 'nginx'
    nginx.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$NGINX_CALLS"\n')
    nginx.chmod(0o755)
    envsubst = bindir / 'envsubst'
    envsubst.write_text(
        f'#!{sys.executable}\n'
        'import os, sys\n'
        'assert sys.argv[1:] == ["$DOMAIN_NAME"]\n'
        'sys.stdout.write(sys.stdin.read().replace("${DOMAIN_NAME}", os.environ["DOMAIN_NAME"]))\n',
    )
    envsubst.chmod(0o755)
    output = tmp_path / 'nginx.conf'
    allowlist = tmp_path / 'allowlist.conf'
    calls = tmp_path / 'nginx-calls'
    # Also prove an empty setting clears a previously populated file.
    allowlist.write_text('192.0.2.0/24 1;\n')

    def run(value, template='nginx.conf'):
        env = {
            'PATH': str(bindir) + os.pathsep + os.defpath,
            'DOMAIN_NAME': 'demo.example',
            'NGINX_TEMPLATE_PATH': str(ROOT / template),
            'NGINX_OUTPUT_PATH': str(output),
            'NGINX_ALLOWLIST_PATH': str(allowlist),
            'NGINX_CALLS': str(calls),
        }
        if value is not None:
            env['FRIDGE_DEMO_IP_ALLOWLIST'] = value
        result = subprocess.run(
            ['sh', str(ROOT / 'ops/nginx/render.sh')],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result, output, allowlist, calls

    return run


@pytest.mark.parametrize('template', ['nginx.conf', 'ops/nginx/bootstrap.conf'])
def test_render_allowlist_and_only_domain_variable(render, template):
    result, output, allowlist, calls = render(
        '203.0.113.7, 2001:db8::1\t192.0.2.0/24,\n2001:db8:1::/48',
        template,
    )
    assert result.returncode == 0, result.stderr
    assert allowlist.read_text() == '203.0.113.7 1;\n2001:db8::1 1;\n192.0.2.0/24 1;\n2001:db8:1::/48 1;\n'
    assert output.read_text() == (ROOT / template).read_text().replace('${DOMAIN_NAME}', 'demo.example')
    assert calls.read_text() == '-g\ndaemon off;\n'


@pytest.mark.parametrize('value', [None, '', ' , \t\n, '])
def test_render_empty_allowlist(render, value):
    result, output, allowlist, calls = render(value)
    assert result.returncode == 0, result.stderr
    assert allowlist.read_text() == ''
    assert output.exists() and calls.exists()


@pytest.mark.parametrize(
    'entry',
    ['example.com', '203.0.113.7;', '*', '$(id)', '192.0.2.0/-1', '0.0.0.0/0', '::/0', '::/00', '0.0.0.0/000'],
)
def test_render_rejects_invalid_entry_before_nginx(render, entry):
    result, output, allowlist, calls = render(f'203.0.113.7, {entry}')
    assert result.returncode == 1
    assert 'FRIDGE_DEMO_IP_ALLOWLIST' in result.stderr
    assert entry in result.stderr
    assert not output.exists()
    assert not calls.exists()


def test_every_nginx_zone_uses_client_key():
    zones = []
    for path in ROOT.rglob('*.conf'):
        for match in re.finditer(r'^\s*limit_(?:req|conn)_zone\s+(\S+)', path.read_text(), re.M):
            zones.append((path.relative_to(ROOT), match.group(1)))
    assert len(zones) == 8
    assert all(key == '$fridge_client_key' for _, key in zones), zones
    config = (ROOT / 'nginx.conf').read_text()
    assert re.search(
        r'geo\s+\$fridge_ip_allowlisted\s*\{\s*default 0;\s*include /etc/nginx/allowlist.conf;\s*}',
        config,
    )
    assert re.search(
        r'map\s+\$fridge_ip_allowlisted\s+\$fridge_client_key\s*\{\s*default \$binary_remote_addr;'
        r'\s*1 "\$binary_remote_addr\$cookie_session_id";\s*}',
        config,
    )
    assert config.index('geo $fridge_ip_allowlisted') < config.index('limit_req_zone')


def test_compose_renderer_mounts_and_shared_environment():
    compose = (ROOT / 'docker-compose.prod.yml').read_text()
    for name in ('backend', 'nginx'):
        service = re.search(rf'^  {name}:\n(.*?)(?=^  \S|\Z)', compose, re.M | re.S)
        assert service is not None
        assert 'FRIDGE_DEMO_IP_ALLOWLIST: ${FRIDGE_DEMO_IP_ALLOWLIST:-}' in service.group(1)
    assert 'command: /bin/sh /etc/nginx/fridge/render.sh' in compose
    assert '${NGINX_CONFIG_TEMPLATE:-./nginx.conf}:/etc/nginx/nginx.conf.template:ro' in compose
    assert './ops/nginx:/etc/nginx/fridge:ro' in compose
    for name, default in (('FRIDGE_DEMO_IP_HOURLY_LIMIT', 5), ('FRIDGE_DEMO_IP_LIVE_CAP', 10)):
        assert f'{name}: ${{{name}:-{default}}}' in compose
        for path in ('.env.example', 'ops/terraform/user_data.sh'):
            assert f'{name}={default}\n' in (ROOT / path).read_text()
    for path in ('.env.example', 'ops/terraform/user_data.sh'):
        assert 'FRIDGE_DEMO_IP_ALLOWLIST=\n' in (ROOT / path).read_text()
