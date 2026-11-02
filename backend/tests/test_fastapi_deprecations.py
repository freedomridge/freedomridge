"""Route declarations must not use deprecated FastAPI parameters, which a later FastAPI may reject."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_importing_the_app_raises_no_fastapi_deprecation():
    code = (
        'import warnings\n'
        'from fastapi.exceptions import FastAPIDeprecationWarning\n'
        "warnings.simplefilter('error', FastAPIDeprecationWarning)\n"
        'import backend.main\n'
    )
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]


def test_sample_listing_still_accepts_only_asc_or_desc():
    from backend.main import app

    parameters = app.openapi()['paths']['/api/datasets/{id}/samples']['get']['parameters']
    sort_order = next(parameter for parameter in parameters if parameter['name'] == 'sort_order')
    assert sort_order['schema']['anyOf'][0]['pattern'] == '^(asc|desc)$'
