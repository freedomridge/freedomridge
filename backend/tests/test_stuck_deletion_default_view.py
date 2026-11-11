"""A dataset sealed for deletion before 2.9.0 kept its default view. Its deletion removed the view's row, the foreign
key then set default_view_id to NULL in the sealed dataset row, guard_dataset_row refused that, and the job failed
there at every resume. The deletion's own transaction may make that one update now (0023): resuming finishes it."""

import os

import asyncpg
import pytest

from backend import dataset_deletion, job_queue
from backend.routers import datasets
from backend.tests import test_field_registry_postgres
from backend.tests.test_dataset_deletion import claim, finish, step
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
VIEWS_UNIT = [table for table, _ in dataset_deletion.DATASET_TABLES].index('views')
REFUSED = 'Dataset is being deleted; writes are disabled'


async def stuck(database, dataset_id, *, mirror=False):
    """The state the code before 2.9.0 left: the dataset sealed with its default view kept, and its deletion failed
    at the views unit. Returns the view's id."""
    await ingest([{'score': 1}], dataset_id=dataset_id)
    view_id = await database.fetchval(
        "INSERT INTO views (dataset_id, name, slug) VALUES ($1, 'opening', $2) RETURNING id",
        dataset_id,
        f'opening-{dataset_id}',
    )
    await database.execute('UPDATE datasets SET default_view_id = $1 WHERE id = $2', view_id, dataset_id)
    if mirror:
        await database.execute(
            "INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor, state) VALUES ($1, 7, 0, 'stopped')",
            dataset_id,
        )
    job = await job_queue.enqueue_job(database.pool, dataset_deletion.KIND, dataset_id=dataset_id, lane='user')
    # The seal as it was before 2.9.0: the default view stays.
    await database.execute(
        'UPDATE datasets SET deleting = TRUE, deletion_job_id = $2 WHERE id = $1',
        dataset_id,
        job['id'],
    )
    current = await claim(database)
    while (current['checkpoint'].get('phase'), current['checkpoint'].get('table')) != ('tables', VIEWS_UNIT):
        current = await step(database, current)
    await job_queue.fail_job(database.pool, current['id'], current['lease_token'], {'message': REFUSED})
    row = await database.fetchrow('SELECT deleting, default_view_id FROM datasets WHERE id = $1', dataset_id)
    assert row['deleting'] and row['default_view_id'] == view_id
    assert await database.fetchval('SELECT state FROM jobs WHERE id = $1', job['id']) == 'failed'
    return view_id


@pytest.mark.parametrize('mirror', [False, True], ids=['dataset', 'stopped mirror'])
def test_resuming_a_deletion_stuck_on_its_default_view_finishes_it(registry_database, mirror):
    async def scenario(database):
        view_id = await stuck(database, 2, mirror=mirror)
        assert (await datasets.resume_dataset_deletion(2, USER))['state'] == 'queued'
        assert (await finish(database))['state'] == 'succeeded'
        assert await database.fetchval('SELECT count(*) FROM datasets WHERE id = 2') == 0
        assert await database.fetchval('SELECT count(*) FROM views WHERE id = $1', view_id) == 0
        assert await database.fetchval('SELECT count(*) FROM mirrors') == 0
        assert await database.fetchval('SELECT count(*) FROM datasets WHERE id = 1') == 1

    registry_database(scenario)


def test_outside_the_deletion_the_sealed_row_still_refuses_that_update(registry_database):
    async def scenario(database):
        view_id = await stuck(database, 2)

        async def update(statement, deletion=None):
            async with database.pool.acquire() as connection, connection.transaction():
                if deletion is not None:
                    await connection.execute("SELECT set_config('fridge.deletion_dataset', $1, TRUE)", deletion)
                await connection.execute(statement)

        for statement, deletion in [
            ('UPDATE datasets SET default_view_id = NULL WHERE id = 2', None),
            # Another dataset's deletion.
            ('UPDATE datasets SET default_view_id = NULL WHERE id = 2', '1'),
            # This deletion, changing more than the default view.
            ("UPDATE datasets SET default_view_id = NULL, description = 'changed' WHERE id = 2", '2'),
            ("UPDATE datasets SET description = 'changed' WHERE id = 2", '2'),
        ]:
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match=REFUSED):
                await update(statement, deletion)
        row = await database.fetchrow('SELECT default_view_id, description FROM datasets WHERE id = 2')
        assert (row['default_view_id'], row['description']) == (view_id, None)

    registry_database(scenario)
