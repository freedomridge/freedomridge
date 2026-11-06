"""A map re-fit split between a server-only runner and a compute-only one: the export, the publish and the ranks
on the server, the fit on compute. One runner that takes both still does it all without handing off."""

import os

import pytest

from backend import map_refit
from backend.map_artifacts import LocalArtifactStore
from backend.tests import test_field_registry_postgres
from backend.tests.test_job_runner import make_worker
from backend.tests.test_map_refit import fake_fit, map_points, map_state, seed, submit

registry_database = test_field_registry_postgres.registry_database
__all__ = ['fake_fit', 'registry_database']
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


async def runner(database, store, placements):
    return await make_worker(
        database,
        {map_refit.KIND: map_refit.handler(database.pool, store)},
        policies={map_refit.KIND: map_refit.POLICY},
        placements=placements,
    )


def test_the_fit_runs_on_compute_and_the_rest_on_the_server(registry_database, tmp_path, fake_fit):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=120)
        old = await map_points(database)
        job = await submit(database)
        server, compute = await runner(database, store, ('server',)), await runner(database, store, ('compute',))
        seen = []  # (placement, phase) at each hand-off
        for _ in range(20):
            row = await database.fetchrow('SELECT state, placement, checkpoint FROM jobs WHERE id = $1', job['id'])
            if row['state'] in ('succeeded', 'failed', 'cancelled'):
                break
            seen.append((row['placement'], (row['checkpoint'] or {}).get('phase', 'start')))
            await database.execute('UPDATE jobs SET available_at = clock_timestamp() WHERE id = $1', job['id'])
            await (server if row['placement'] == 'server' else compute).run_once()
        assert row['state'] == 'succeeded', row
        # Queued as compute work: the compute runner starts it, then each side takes its steps.
        assert seen == [('compute', 'start'), ('server', 'export'), ('compute', 'fit'), ('server', 'publish')]
        assert fake_fit, 'the fit ran'
        new = await map_points(database)
        assert set(new) == set(old) and new != old  # every point moved to the new map
        assert (await map_state(database))['model_id'] is not None

    registry_database(scenario)


def test_one_runner_that_takes_both_never_hands_the_job_back(registry_database, tmp_path, fake_fit):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await seed(database, n=120)
        job = await submit(database)
        both = await runner(database, store, ('server', 'compute'))  # the default step budget: 10 s
        await both.run_once()
        row = await database.fetchrow('SELECT state, attempt FROM jobs WHERE id = $1', job['id'])
        assert (row['state'], row['attempt']) == ('succeeded', 1)

    registry_database(scenario)


def test_a_rank_only_heal_job_is_server_work(registry_database, tmp_path):
    async def scenario(database):
        await seed(database, n=10)
        async with database.pool.acquire() as connection, connection.transaction():
            healing, _ = await map_refit.submit_rank(connection, 1, 'clip')
        assert healing['placement'] == 'server'

    registry_database(scenario)
