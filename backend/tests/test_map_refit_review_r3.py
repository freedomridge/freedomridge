"""Scheduled map re-fit validation and containment regressions."""

import ast
import asyncio
import json
import os
import uuid
from pathlib import Path

import asyncpg
import pytest
from fridge import map_fit
from fridge.core import dataset as sdk_dataset

from backend import map_refit
from backend.map_artifacts import LocalArtifactStore
from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_map_refit import FIELD, fake_reducer, run_job, seed, submit
from backend.tests.test_map_refit_review_r2 import export_file, lasso, managed_map

registry_database = test_field_registry_postgres.registry_database
BACKEND = Path(__file__).resolve().parents[1]
needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')


# --- every samples-path variant refuses a stale lasso with the 409, never a 500 -----------------


@needs_postgres
def test_page_only_keyset_and_slim_grid_queries_refuse_a_stale_lasso(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(datasets, 'db', database)
        await seed(database, n=40)
        model = await managed_map(database)
        current, stale = lasso(model), lasso(uuid.uuid4())
        variants: dict[str, dict] = {
            'page only': {'skip_count': True},
            'keyset page': {'skip_count': True, 'after_id': 0},
            'slim page': {'skip_count': True, 'slim': True, 'fields': 'id,filepath'},
            'page and count': {},
        }
        for name, options in variants.items():
            page = await datasets.list_samples_post(1, datasets.SamplesRequest(limit=5, filters=current, **options))
            assert page['items'], name
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='re-fitted'):
                await datasets.list_samples_post(1, datasets.SamplesRequest(limit=5, filters=stale, **options))
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='re-fitted'):
            await datasets.list_samples(1, limit=5, offset=0, filters=json.dumps(stale))

    registry_database(scenario)


# --- the growth bound never goes below the floor, and only follows a memory-limited fit ----------


def test_the_growth_bound_never_cuts_below_the_floor(tmp_path, monkeypatch):
    source = tmp_path / 'vectors.bin'
    export_file(source, 100_000, 2)
    monkeypatch.setattr(sdk_dataset, '_available_ram_bytes', lambda: 64 * 1024**3)
    monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)

    def fit_size(bound):
        output = tmp_path / f'out-{bound}'
        map_fit.main(['--input', str(source), '--output', str(output), '--max-fit-size', str(bound)])
        return json.loads((output / 'manifest.json').read_text())['fit_size']

    assert fit_size(60_000) == 60_000  # above the floor the bound holds
    assert fit_size(20_000) == 50_000  # a map last fitted at 20k still gets the 50k floor


@needs_postgres
def test_a_fit_of_every_point_does_not_bound_the_next_one(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)
    sizes = []

    async def fit(command, source, output, log_path, allowance, bytes_per_point=None, max_fit_size=None, shared=False):
        sizes.append(max_fit_size)
        monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
        await asyncio.to_thread(map_fit.main, ['--input', str(source), '--output', str(output)])

    monkeypatch.setattr(map_refit, 'run_fit', fit)

    async def scenario(database):
        await seed(database, n=200)
        assert (await run_job(database, await submit(database), store))['state'] == 'succeeded'
        # Last fit: all 200 points, with a measured cost: nothing to bound.
        await database.execute('UPDATE embedding_map_state SET fit_bytes_per_point = 20000, fit_size = 200')
        assert (await run_job(database, await submit(database), store))['state'] == 'succeeded'
        # Last fit: a sample (memory-limited): bounded at 1/0.75 of it.
        await database.execute('UPDATE embedding_map_state SET fit_bytes_per_point = 20000, fit_size = 150')
        assert (await run_job(database, await submit(database), store))['state'] == 'succeeded'
        assert sizes == [None, None, 200]

    registry_database(scenario)


# --- a retry after an out-of-memory kill fits 75% of what was attempted, as its message says ------


def test_an_out_of_memory_retry_is_three_quarters_of_the_killed_attempt(tmp_path):
    (tmp_path / 'plan.json').write_text(json.dumps({'rows': 1_200_000, 'fit_size': 400_000, 'bytes_per_point': 20_000}))
    job = {'dataset_id': 1, 'args': {'field': FIELD}, 'checkpoint': {'phase': 'fit'}}
    step = asyncio.run(map_refit._after_out_of_memory(job, tmp_path, map_refit.FitOutOfMemory('killed')))
    assert step.checkpoint is not None and step.message is not None
    assert step.checkpoint['max_fit_size'] == 300_000
    assert step.checkpoint['bytes_per_point'] == 26_667
    assert 'retrying with at most 300,000' in step.message
    # Near the floor the retry stops at it.
    (tmp_path / 'plan.json').write_text(json.dumps({'rows': 1_200_000, 'fit_size': 60_000, 'bytes_per_point': 20_000}))
    step = asyncio.run(map_refit._after_out_of_memory(job, tmp_path, map_refit.FitOutOfMemory('killed')))
    assert step.checkpoint is not None and step.checkpoint['max_fit_size'] == map_refit.MINIMUM_FIT_POINTS


@needs_postgres
def test_the_retry_runs_with_the_retry_size(registry_database, tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)
    attempts = []

    async def fit(command, source, output, log_path, allowance, bytes_per_point=None, max_fit_size=None, shared=False):
        attempts.append((bytes_per_point, max_fit_size))
        if len(attempts) == 1:
            Path(output).mkdir(parents=True, exist_ok=True)
            plan = {'rows': 1_200_000, 'fit_size': 400_000, 'bytes_per_point': 20_000}
            (Path(output) / 'plan.json').write_text(json.dumps(plan))
            raise map_refit.FitOutOfMemory('The fit was stopped by the kernel')
        monkeypatch.setattr(sdk_dataset, 'fit_reducer', fake_reducer)
        await asyncio.to_thread(map_fit.main, ['--input', str(source), '--output', str(output)])

    monkeypatch.setattr(map_refit, 'run_fit', fit)

    async def scenario(database):
        await seed(database, n=200)
        assert (await run_job(database, await submit(database), store))['state'] == 'succeeded'
        assert attempts[1] == (26_667, 300_000)

    registry_database(scenario)


# --- the call-site guard also catches dataset_id=None and aliased imports ----------------------


def dataset_less_compiles(source: str, path: str) -> set:
    """parse_filter_node calls (under any imported alias) that pass no dataset, or an explicit None."""
    tree = ast.parse(source)
    names = {'parse_filter_node'}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {alias.asname for alias in node.names if alias.name == 'parse_filter_node' and alias.asname}
    found = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or function.name == 'parse_filter_node':
            continue
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if called not in names:
                continue
            given = next((keyword.value for keyword in call.keywords if keyword.arg == 'dataset_id'), None)
            if given is None and len(call.args) >= 6:
                given = call.args[5]
            if given is None or (isinstance(given, ast.Constant) and given.value is None):
                found.add((path, function.name, call.lineno))
    return found


def test_the_guard_catches_none_and_aliases():
    source = '''
from .datasets import parse_filter_node as compile_filter

async def scoped(f, args, id):
    parse_filter_node(f, args, 2, dataset_id=id)
    parse_filter_node(f, args, 2, None, None, id)

async def unscoped(f, args):
    parse_filter_node(f, args, 2)
    parse_filter_node(f, args, 2, dataset_id=None)
    parse_filter_node(f, args, 2, None, None, None)
    compile_filter(f, args, 2)
'''
    assert sorted(line for _, _, line in dataset_less_compiles(source, 'x.py')) == [9, 10, 11, 12]


def test_every_dataset_scoped_filter_compile_passes_its_dataset():
    """Without a dataset the lasso's map check is correlated: one plpgsql call per matching sample and no
    parallel plan (an 88% lasso over 1M samples went from 0.88 to 3.16 s)."""
    # _scatter_rejections (and its nested visit) compiles only tags / media_type leaves, never a lasso.
    exempt = {('routers/embeddings.py', '_scatter_rejections'), ('routers/embeddings.py', 'visit')}
    missing = set()
    for path in BACKEND.rglob('*.py'):
        if 'tests' not in path.parts:
            relative = str(path.relative_to(BACKEND))
            missing |= {hit for hit in dataset_less_compiles(path.read_text(), relative) if hit[:2] not in exempt}
    assert missing == set()


# --- a malformed lasso field is the compiler's 400, not a 500 from the up-front check -----------


def test_the_up_front_check_skips_a_malformed_lasso_field():
    malformed = {'logic': 'AND', 'filters': [{'field': 5, 'operator': 'embedding_polygon', 'value': [[0, 0], [1, 1]]}]}
    assert datasets._lasso_maps(malformed) == set()
    asyncio.run(datasets.require_current_lassos(1, malformed))  # no query: the compiler answers 400 later
