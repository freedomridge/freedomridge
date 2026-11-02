"""The backend image runs Python 3.10 even when CI uses a newer interpreter."""

import ast
import subprocess
import textwrap
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def test_backend_avoids_python_311_asyncio_apis():
    forbidden = {
        'asyncio.timeout',
        'asyncio.timeout_at',
        'asyncio.TaskGroup',
        'asyncio.Runner',
        'asyncio.Barrier',
        'ExceptionGroup',
        'BaseExceptionGroup',
        'builtins.ExceptionGroup',
        'builtins.BaseExceptionGroup',
    }
    violations = []
    for path in sorted(BACKEND.rglob('*.py')):
        if 'tests' in path.relative_to(BACKEND).parts:
            continue
        # Also rejects 3.11 syntax such as except*.
        tree = ast.parse(path.read_text(), filename=str(path), feature_version=(3, 10))
        imports = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imports[alias.asname or alias.name] = f'{node.module}.{alias.name}'
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                name = imports.get(node.id, node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                name = f'{imports.get(node.value.id, node.value.id)}.{node.attr}'
            else:
                continue
            if name in forbidden:
                violations.append(f'{path.relative_to(BACKEND)}:{node.lineno}: {name}')
    assert not violations, '\n'.join(violations)


@pytest.mark.parametrize('phase', ['query', 'poll', 'succeeded'])
def test_answer_on_python310(phase):
    interpreter = Path('/usr/bin/python3.10')
    if not interpreter.exists():
        pytest.skip('Requires the production Python 3.10 interpreter')
    # Stub only FastAPI's response types: run the real answer and asyncio implementation,
    # without requiring the host's Python 3.12 dependencies in the 3.10 interpreter.
    script = textwrap.dedent('''\
        import asyncio
        import runpy
        import sys
        import time
        import types

        fastapi = types.ModuleType('fastapi')
        fastapi.HTTPException = Exception
        responses = types.ModuleType('fastapi.responses')
        class JSONResponse:
            def __init__(self, status_code, content):
                self.status_code = status_code
                self.content = content
        responses.JSONResponse = JSONResponse
        sys.modules.update({'fastapi': fastapi, 'fastapi.responses': responses})
        module = runpy.run_path(sys.argv[1])
        phase = sys.argv[2]

        async def scenario():
            job = {'id': 'durable-job', 'state': 'queued'}
            class Pool:
                active = False
                calls = 0
                async def fetchrow(self, *args):
                    self.calls += 1
                    self.active = True
                    try:
                        if phase == 'query':
                            await asyncio.sleep(10)
                        return dict(job, state='succeeded' if phase == 'succeeded' else 'running',
                                    created_by=1, kind='field_index_build', result={'message': 'ok'}, error=None)
                    finally:
                        self.active = False
            pool = Pool()
            started = time.monotonic()
            response = await module['answer'](
                pool, job, started - module['ANSWER_SECONDS'] + 0.2,
                user={'id': 1, 'role': 'power_user'},
            )
            assert pool.calls > 0
            assert not pool.active
            assert not (asyncio.all_tasks() - {asyncio.current_task()})
            assert time.monotonic() - started < 1
            if phase == 'succeeded':
                assert response == {'message': 'ok'}
            else:
                assert response.status_code == 202
                assert response.content == {'job_id': 'durable-job',
                                            'state': 'queued' if phase == 'query' else 'running'}
            assert job == {'id': 'durable-job', 'state': 'queued'}
        asyncio.run(scenario())
    ''')
    result = subprocess.run(
        [str(interpreter), '-c', script, str(BACKEND / 'job_wait.py'), phase],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
