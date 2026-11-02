"""Parse documented shell recipes with their real CLI, stopping before execution."""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def recipes():
    paths = [*REPO.glob('*.md'), *(REPO / 'docs').rglob('*.md'), *(REPO / 'examples').rglob('*.md')]
    for path in sorted(paths):
        for block in re.findall(r'```(?:bash|sh|shell)\n(.*?)```', path.read_text(), re.DOTALL):
            for line in block.replace('\\\n', ' ').splitlines():
                if not re.search(r'python3?\s+examples/', line):
                    continue
                words = shlex.split(line, comments=True)
                for index, word in enumerate(words[:-1]):
                    script = words[index + 1]
                    if word not in ('python', 'python3') or not script.startswith('examples/'):
                        continue
                    if not (REPO / script).is_file() or script.endswith('test_sdk_unit.py'):
                        continue
                    args = ['1' if item == 'N' else item for item in words[index + 2 :]]
                    yield pytest.param(script, args, id=f'{path.relative_to(REPO)}:{script}:{args}')


@pytest.mark.parametrize('script,args', list(recipes()))
def test_documented_command_parses(script, args, tmp_path):
    # --help alone hides missing required options and unknown flags. Let the real
    # parser validate the actual recipe, then exit before auth, IO or networking.
    harness = '''
import argparse
import runpy
import socket
import sys
class Parsed(BaseException):
    pass
original = argparse.ArgumentParser.parse_args
def parse(self, *a, **kw):
    original(self, *a, **kw)
    raise Parsed
def blocked(*a, **kw):
    raise AssertionError('A docs parser test attempted a network connection')
argparse.ArgumentParser.parse_args = parse
socket.socket.connect = blocked
sys.argv = sys.argv[1:]
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
except Parsed:
    pass
else:
    raise AssertionError('Recipe never reached argparse')
'''
    environment = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    result = subprocess.run(
        [sys.executable, '-c', harness, str(REPO / script), *args],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
