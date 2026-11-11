"""New samples on a map, re-fitted or not: placed by their nearest mapped neighbours, under the map writers' fence."""

import os
import time
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from fastapi import HTTPException

from backend import access, job_queue, job_service, map_placement
from backend.map_artifacts import LocalArtifactStore
from backend.routers import embeddings
from backend.routers import jobs as jobs_router
from backend.tests import test_field_registry_postgres
from backend.tests.test_job_runner import make_worker
from backend.tests.test_map_refit import (  # noqa: F401  (fake_fit is a fixture)
    FIELD,
    assert_ranks_exact,
    fake_fit,
    map_points,
    map_state,
    pending,
    run_job,
    submit,
)

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Disposable PostgreSQL required')
DIM = 8
# Two groups of images, far apart: the fake fit maps a vector v to (1000 + v[0], v[1]), so group A lands near
# (1005, 0) and group B near (1000, 5).
CENTRES = {'A': np.eye(DIM)[0] * 5, 'B': np.eye(DIM)[1] * 5}


def group_of(x, y):
    return 'A' if x > 1002.5 and y < 2.5 else 'B' if x < 1002.5 and y > 2.5 else None


async def add_samples(db, ids, groups, seed=0):
    """Samples with vectors around their group's centre (their embeddings queue them for the map)."""
    rng = np.random.default_rng(seed)
    vectors = [CENTRES[group] + rng.normal(scale=0.3, size=DIM) for group in groups]
    await db.execute(
        "INSERT INTO samples(id,dataset_id,filepath,media_type) "
        "SELECT i,1,'image-' || i,'image' FROM unnest($1::int[]) i",
        ids,
    )
    await db.execute(
        "INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) "
        "SELECT (SELECT dataset_id FROM samples WHERE id = i),i,$3,v::vector FROM unnest($1::int[],$2::text[]) u(i,v)",
        ids,
        [str(list(map(float, vector))) for vector in vectors],
        FIELD,
    )


async def refitted_map(database, store, n=200):
    """A dataset with n samples in two groups, on a map re-fitted by the (fake) fit."""
    ids = list(range(1, n + 1))
    await add_samples(database, ids, ['A' if i % 2 else 'B' for i in ids])
    await database.execute(
        "INSERT INTO embedding_coordinates(sample_id,field_name,x,y) SELECT i,$2,-100 - (i % 40),i % 13 "
        "FROM unnest($1::int[]) i",
        ids,
        FIELD,
    )  # an old map, which the re-fit replaces
    await database.execute('SELECT recompute_viz_rank(1,$1,TRUE)', FIELD)
    finished = await run_job(database, await submit(database), store)
    assert finished['state'] == 'succeeded', finished['error']
    return ids


async def refitted_map_points(database, ids):
    """Give `ids` map spots again (their queue rows go), and re-rank the map."""
    await database.execute(
        'INSERT INTO embedding_coordinates(sample_id,field_name,x,y) SELECT i,$2,1000 + i,0 FROM unnest($1::int[]) i',
        ids,
        FIELD,
    )
    await database.execute('SELECT recompute_viz_rank(1,$1,TRUE)', FIELD)


async def placer(database, lookups=2):
    return await make_worker(
        database,
        {map_placement.KIND: map_placement.handler(database.pool, lookups=lookups)},
        policies={map_placement.KIND: map_placement.POLICY},
        placements=('server',),
        step_budget_seconds=0.001,  # one batch per run
    )


async def run_to_end(database, worker, job, limit=200):
    for _ in range(limit):
        row = await database.fetchrow('SELECT state FROM jobs WHERE id=$1', job['id'])
        if row['state'] in ('succeeded', 'failed', 'cancelled'):
            break
        await worker.run_once()
    return dict(await database.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id']))


@pytest.fixture
def free_space(monkeypatch):
    monkeypatch.setattr(map_placement, 'require_free_space', AsyncMock())


def test_a_map_that_grows_tenfold_places_every_new_sample_with_its_group(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,  # noqa: F811
    free_space,
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await refitted_map(database, store)
        state = await map_state(database)
        assert state['model_id'] is not None and state['artifact_location'] is None  # no model is kept
        assert list(store.models()) == []
        new = list(range(1001, 3001))
        groups = dict(zip(new, ['A' if i % 3 else 'B' for i in new]))
        await add_samples(database, new, [groups[i] for i in new], seed=1)
        assert await pending(database) == set(new)

        vectors = await database.fetch('SELECT sample_id, ctid::text, xmin::text FROM embeddings ORDER BY sample_id')
        [job] = await map_placement.schedule_placements(database.pool)
        assert job['barrier'] == {'upper': 3000} and job['lane'] == 'maintenance'
        assert await map_placement.schedule_placements(database.pool) == []  # one at a time per map
        worker = await placer(database)
        await worker.run_once()
        await add_samples(database, [5000], ['A'], seed=2)  # past the barrier: left for the next job
        finished = await run_to_end(database, worker, job)
        assert finished['state'] == 'succeeded', finished['error']
        assert finished['result'] == {'placed': 2000, 'without_neighbours': 0}
        assert finished['progress_done'] == finished['progress_total'] == 2000
        assert await pending(database) == {5000}
        # It only reads vectors: no embedding row is rewritten (that would queue it again).
        assert (await database.fetch('SELECT sample_id, ctid::text, xmin::text FROM embeddings ORDER BY sample_id'))[
            : len(vectors)
        ] == vectors

        points = await map_points(database)
        assert all(group_of(*points[i]) == groups[i] for i in new)  # every one with its own group
        meta = await assert_ranks_exact(database)
        assert meta['total_points'] == 2200

        [following] = await map_placement.schedule_placements(database.pool)
        assert (await run_to_end(database, worker, following))['state'] == 'succeeded'
        assert await pending(database) == set()
        assert group_of(*(await map_points(database))[5000]) == 'A'

        # Like the other automatic maintenance, finished placements stay out of the dataset's history, and
        # are deleted after a day.
        monkeypatch.setattr(jobs_router, 'db', database)
        monkeypatch.setattr(access, 'db', database)
        user = {'id': 1, 'role': 'admin'}

        async def history(**options):
            page = await jobs_router.list_dataset_jobs(1, limit=50, cursor=None, user=user, **options)
            return [item.kind for item in page.items]

        assert map_placement.KIND not in await history()
        assert (await history(include_system_statistics=True)).count(map_placement.KIND) == 2
        cleanup = {'created_at': await database.fetchval('SELECT now()') + timedelta(days=2), 'checkpoint': {}}
        step = await job_service._cleanup_statistics_history(database.pool, cleanup)
        async with database.pool.acquire() as connection, connection.transaction():
            await step.publish(connection)
        assert map_placement.KIND not in await history(include_system_statistics=True)

    registry_database(scenario)


def test_nothing_is_placed_while_the_map_is_re_fitted_or_on_a_map_replaced_meanwhile(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,  # noqa: F811
    free_space,
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await refitted_map(database, store)
        await add_samples(database, [1001, 1002], ['A', 'B'], seed=1)
        before = await map_points(database)

        # A re-fit under way (from its start step, as there: until then its export takes in what is placed): no
        # placement is scheduled, and one already queued places nothing.
        refit = await submit(database)
        await database.execute(
            'UPDATE embedding_map_state SET active_job_id=$2, active_since=clock_timestamp() '
            'WHERE dataset_id=1 AND field_name=$1',
            FIELD,
            refit['id'],
        )
        assert await map_placement.schedule_placements(database.pool) == []
        job = await job_queue.enqueue_job(  # queued just before the re-fit started
            database.pool,
            map_placement.KIND,
            dataset_id=1,
            args={'field': FIELD},
            barrier={'upper': 1002},
            lane='maintenance',
        )
        worker = await placer(database)
        finished = await run_to_end(database, worker, job)
        assert finished['state'] == 'succeeded' and finished['result']['placed'] == 0
        assert await map_points(database) == before and await pending(database) == {1001, 1002}
        await database.execute(
            "UPDATE jobs SET state='cancelled', finished_at=clock_timestamp() WHERE id=$1",
            refit['id'],
        )

        # The map is replaced between the lookup and the write: that batch is dropped, and the next one is
        # looked up on the new map.
        measure = map_placement._measure
        replaced: list[uuid.UUID] = []

        async def measure_then_replace(pool, job, lookups):
            batch = await measure(pool, job, lookups)
            if not replaced:
                replaced.append(uuid.uuid4())
                await database.execute(
                    'UPDATE embedding_map_state SET model_id=$2 WHERE dataset_id=1 AND field_name=$1',
                    FIELD,
                    replaced[0],
                )
            return batch

        monkeypatch.setattr(map_placement, '_measure', measure_then_replace)
        [job] = await map_placement.schedule_placements(database.pool)
        finished = await run_to_end(database, worker, job)
        assert finished['result']['placed'] == 0 and await pending(database) == {1001, 1002}
        monkeypatch.setattr(map_placement, '_measure', measure)
        [job] = await map_placement.schedule_placements(database.pool)
        assert (await run_to_end(database, worker, job))['result']['placed'] == 2
        assert await pending(database) == set()

    registry_database(scenario)


def test_a_vector_changed_after_its_lookup_is_not_placed_and_waits_for_the_next_job(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,  # noqa: F811
    free_space,
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await refitted_map(database, store)
        await add_samples(database, [1001, 1002, 1003], ['A', 'A', 'B'], seed=1)
        measure = map_placement._measure

        async def measure_then_change(pool, job, lookups):
            batch = await measure(pool, job, lookups)
            await database.execute(
                'UPDATE embeddings SET vector=$2::vector WHERE sample_id=1002 AND field_name=$1',
                FIELD,
                str(list(map(float, CENTRES['B']))),
            )
            return batch

        monkeypatch.setattr(map_placement, '_measure', measure_then_change)
        [job] = await map_placement.schedule_placements(database.pool)
        finished = await run_to_end(database, await placer(database), job)
        assert finished['result'] == {'placed': 2, 'without_neighbours': 0}  # what was written, not what was sent
        assert await pending(database) == {1002}

    registry_database(scenario)


def test_an_empty_map_is_not_scheduled_and_unplaceable_samples_back_off_for_five_minutes(
    registry_database,
    tmp_path,
    fake_fit,  # noqa: F811
    free_space,
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        ids = await refitted_map(database, store, n=20)
        await database.execute('DELETE FROM embedding_coordinates WHERE field_name=$1', FIELD)  # queues them all
        assert await pending(database) == set(ids)
        assert await map_placement.schedule_placements(database.pool) == []  # no map to place on

        await refitted_map_points(database, ids)
        # Zero vectors have no cosine distance to anything: nothing to place them by.
        await database.execute(
            "INSERT INTO samples(id,dataset_id,filepath,media_type) SELECT i,1,'image-' || i,'image' "
            'FROM generate_series(1001, 1003) i',
        )
        await database.execute(
            'INSERT INTO embeddings(dataset_id,sample_id,field_name,vector) '
            'SELECT (SELECT dataset_id FROM samples WHERE id = i),i,$1,$2::vector FROM generate_series(1001, 1003) i',
            FIELD,
            str([0.0] * DIM),
        )
        [job] = await map_placement.schedule_placements(database.pool)
        finished = await run_to_end(database, await placer(database), job)
        assert finished['result'] == {'placed': 0, 'without_neighbours': 3}
        assert await map_placement.schedule_placements(database.pool) == []
        await database.execute(
            "UPDATE jobs SET finished_at = finished_at - interval '6 minutes' WHERE id=$1",
            job['id'],
        )
        assert len(await map_placement.schedule_placements(database.pool)) == 1

    registry_database(scenario)


def test_the_lookup_uses_the_fields_vector_index(registry_database, tmp_path, fake_fit):  # noqa: F811
    """One index search per sample: the LATERAL form with the field's partial index, on a plan made for the
    field's name. (Ordering a plain join by the distance would sort every vector of the field.)"""
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await refitted_map(database, store, n=50)
        await database.execute(
            f'CREATE INDEX hnsw_test_{FIELD} ON embeddings_d1 USING hnsw ((vector::vector({DIM})) vector_cosine_ops) '
            f"WHERE field_name = '{FIELD}'",
        )

        async def plan(exact):
            async with database.pool.acquire() as connection, connection.transaction():
                await connection.execute(
                    "SELECT '[0]'::vector(1); SET LOCAL plan_cache_mode = force_custom_plan; "
                    'SET LOCAL enable_seqscan = off; SET LOCAL enable_sort = off',  # as on a large field
                )
                rows = await connection.fetch(
                    'EXPLAIN ' + map_placement.lookup_sql(DIM, exact=exact),
                    [1, 2],
                    FIELD,
                    1,
                )
            return '\n'.join(row[0] for row in rows)

        indexed = await plan(exact=False)
        assert f'Index Scan using hnsw_test_{FIELD}' in indexed and 'Order By' in indexed, indexed
        assert f'hnsw_test_{FIELD}' not in await plan(exact=True)

    registry_database(scenario)


def test_the_densest_group_wins_over_a_split_neighbourhood():
    # Ten neighbours in one group, five far away: the plain mean would land between them, in neither.
    positions = np.array([[0.0, 0.0]] * 10 + [[10.0, 0.0]] * 5) + np.linspace(0, 0.1, 15)[:, None]
    distances = np.full(15, 0.1)
    x, y = map_placement.place(positions, distances)
    assert x < 0.2 and abs(y) < 0.2
    # Duplicates at one spot (a median distance of 0) still give that spot.
    assert tuple(map_placement.place(np.zeros((15, 2)), distances)) == (0.0, 0.0)


def test_a_map_never_re_fitted_gets_its_new_samples_placed_too(registry_database, free_space, monkeypatch):
    async def scenario(database):
        # A map made by the SDK (a full layout, no re-fit state): each image where the fake fit would put it.
        ids = list(range(1, 201))
        await add_samples(database, ids, ['A' if i % 2 else 'B' for i in ids])
        await database.execute(
            """INSERT INTO embedding_coordinates(sample_id,field_name,x,y)
               SELECT e.sample_id, e.field_name, 1000 + (e.vector::real[])[1], (e.vector::real[])[2]
               FROM embeddings e WHERE e.field_name = $1""",
            FIELD,
        )
        await database.execute('SELECT recompute_viz_rank(1,$1,TRUE)', FIELD)
        assert await database.fetchval('SELECT count(*) FROM embedding_map_state') == 0
        monkeypatch.setattr(embeddings, 'db', database)
        assert (await embeddings.get_map_state(1, FIELD, {'id': 1, 'role': 'admin'}))['current_points'] == 200

        await add_samples(database, [1001, 1002], ['A', 'B'], seed=1)
        [job] = await map_placement.schedule_placements(database.pool)
        assert (await run_to_end(database, await placer(database), job))['result']['placed'] == 2
        points = await map_points(database)
        assert (group_of(*points[1001]), group_of(*points[1002])) == ('A', 'B')
        await assert_ranks_exact(database)

    registry_database(scenario)


def test_a_sample_another_writer_placed_meanwhile_keeps_its_place(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,  # noqa: F811
    free_space,
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await refitted_map(database, store)
        await add_samples(database, [1001, 1002], ['A', 'B'], seed=1)
        measure = map_placement._measure

        async def measure_then_place_one(pool, job, lookups):
            batch = await measure(pool, job, lookups)
            if batch and batch['placed']:
                # A full layout gives 1001 a spot between the lookup and the write (its queue row goes with it).
                await database.execute(
                    'INSERT INTO embedding_coordinates(sample_id,field_name,x,y) VALUES (1001,$1,-7,-7)',
                    FIELD,
                )
            return batch

        monkeypatch.setattr(map_placement, '_measure', measure_then_place_one)
        [job] = await map_placement.schedule_placements(database.pool)
        finished = await run_to_end(database, await placer(database), job)
        assert finished['result'] == {'placed': 1, 'without_neighbours': 0}
        points = await map_points(database)
        assert points[1001] == (-7, -7) and group_of(*points[1002]) == 'B'

    registry_database(scenario)


def test_nothing_is_written_without_disk_space_and_the_samples_wait(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,  # noqa: F811
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await refitted_map(database, store)
        await add_samples(database, [1001], ['A'], seed=1)
        full = AsyncMock(side_effect=HTTPException(status_code=507, detail='Not enough disk space'))
        monkeypatch.setattr(map_placement, 'require_free_space', full)
        [job] = await map_placement.schedule_placements(database.pool)
        failed = await run_to_end(database, await placer(database), job)
        assert failed['state'] == 'failed' and await pending(database) == {1001}
        full.assert_awaited()
        monkeypatch.setattr(map_placement, 'require_free_space', AsyncMock())
        await database.execute(
            "UPDATE jobs SET finished_at = finished_at - interval '6 minutes' WHERE id=$1",
            job['id'],
        )
        [job] = await map_placement.schedule_placements(database.pool)
        assert (await run_to_end(database, await placer(database), job))['result']['placed'] == 1

    registry_database(scenario)


def test_a_full_layout_holds_placement_and_a_batch_looked_up_before_the_hold_writes_nothing(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,  # noqa: F811
    free_space,
):
    store = LocalArtifactStore(tmp_path)

    async def scenario(database):
        await refitted_map(database, store)
        await add_samples(database, [1001, 1002], ['A', 'B'], seed=1)
        monkeypatch.setattr(embeddings, 'db', database)
        user = {'id': 1, 'role': 'admin'}
        measure = map_placement._measure

        async def measure_then_hold(pool, job, lookups):
            batch = await measure(pool, job, lookups)
            if batch and batch['placed']:  # an SDK layout starts between the lookup and the write
                assert (await embeddings.hold_placement(1, FIELD, user))['held_until'] is not None
            return batch

        monkeypatch.setattr(map_placement, '_measure', measure_then_hold)
        [job] = await map_placement.schedule_placements(database.pool)
        worker = await placer(database)
        finished = await run_to_end(database, worker, job)
        assert finished['result']['placed'] == 0 and await pending(database) == {1001, 1002}
        monkeypatch.setattr(map_placement, '_measure', measure)
        assert await map_placement.schedule_placements(database.pool) == []  # held: nothing is scheduled

        # The layout ends: its writer releases the hold, and the waiting samples are placed on the new layout.
        await embeddings.release_placement(1, FIELD, user)
        [job] = await map_placement.schedule_placements(database.pool)
        assert (await run_to_end(database, worker, job))['result']['placed'] == 2

        # A writer that dies stops renewing: its hold lapses, and placement resumes.
        await add_samples(database, [1003], ['A'], seed=2)
        await embeddings.hold_placement(1, FIELD, user)
        assert await map_placement.schedule_placements(database.pool) == []
        await database.execute(
            "UPDATE embedding_viz_meta SET placement_held_until = clock_timestamp() - interval '1 second'",
        )
        assert len(await map_placement.schedule_placements(database.pool)) == 1

    registry_database(scenario)


def test_the_sdk_holds_placement_from_before_its_vectors_until_its_layout_is_ranked(monkeypatch):
    from backend.tests.test_map_writer_followup import sdk_fixture

    sdk, _ = sdk_fixture(monkeypatch)
    calls = []
    record = lambda name: lambda *args, **kwargs: calls.append(name)  # noqa: E731
    held = {'held_until': '2026-11-06T12:00:00+00:00', 'renew_seconds': 0.01}

    def hold(*args):
        calls.append('hold')
        return held

    sdk.client.hold_map_placement = Mock(side_effect=hold)
    sdk.client.release_map_placement = Mock(side_effect=record('release'))
    sdk.client.clone = lambda: sdk.client
    vectors = sdk.client.get_vectors.return_value

    def slow_vectors(*args, **kwargs):  # long enough for the hold to be renewed
        calls.append('vectors')
        time.sleep(0.1)
        return vectors

    sdk.client.get_vectors = Mock(side_effect=slow_vectors)
    sdk.client.recompute_viz_rank = Mock(side_effect=record('rank'))
    assert sdk.compute_visualization(FIELD, 8)['reduced'] == 8
    assert calls[0] == 'hold' and calls[1] == 'vectors' and calls[-2:] == ['rank', 'release']
    assert calls.count('hold') > 1  # renewed while it worked
    renewals = calls.count('hold')
    time.sleep(0.1)
    assert calls.count('hold') == renewals  # and not after

    # A failed upload still releases it; a field without a map has nothing to hold.
    failing, _ = sdk_fixture(monkeypatch, refuse=True)
    failing.client.hold_map_placement = Mock(return_value=held)
    failing.client.clone = lambda: failing.client
    with pytest.raises(RuntimeError, match='upload refused'):
        failing.compute_visualization(FIELD, 8)
    failing.client.release_map_placement.assert_called_once_with(1, FIELD)
    unmapped, _ = sdk_fixture(monkeypatch)
    unmapped.compute_visualization(FIELD, 8)
    unmapped.client.hold_map_placement.assert_called_once()
    unmapped.client.release_map_placement.assert_not_called()


def lapsing_hold(sdk, failing):
    """A hold that lasts 50 ms, renewed every 10 ms while `failing` is empty; then every renewal fails."""
    held = {'held_until': '2026-11-06T12:00:00+00:00', 'hold_seconds': 0.05, 'renew_seconds': 0.01}

    def hold(*args):
        if failing:
            raise ConnectionError('server unreachable')
        return held

    sdk.client.hold_map_placement = Mock(side_effect=hold)
    sdk.client.clone = lambda: sdk.client


@pytest.mark.filterwarnings('ignore:Could not renew the hold')
def test_a_hold_that_lapses_during_the_fit_stops_the_layout_before_it_writes_anything(monkeypatch):
    from backend.tests.test_map_writer_followup import sdk_fixture

    sdk, _ = sdk_fixture(monkeypatch)
    failing: list = []
    lapsing_hold(sdk, failing)
    vectors = sdk.client.get_vectors.return_value

    def slow_vectors(*args, **kwargs):  # renewals stop working, and the hold lapses meanwhile
        failing.append(True)
        time.sleep(0.15)
        return vectors

    sdk.client.get_vectors = Mock(side_effect=slow_vectors)
    with pytest.raises(RuntimeError, match='lapsed.*nothing was uploaded, and the map is unchanged'):
        sdk.compute_visualization(FIELD, 8)
    sdk.client.upload_coordinates.assert_not_called()
    sdk.client.release_map_placement.assert_called_once_with(1, FIELD)


@pytest.mark.filterwarnings('ignore:Could not renew the hold')
def test_a_hold_that_lapses_during_the_upload_is_reported(monkeypatch):
    from backend.tests.test_map_writer_followup import sdk_fixture

    sdk, _ = sdk_fixture(monkeypatch)
    failing: list = []
    lapsing_hold(sdk, failing)

    def slow_upload(*args, **kwargs):
        failing.append(True)
        time.sleep(0.15)

    sdk.client.upload_coordinates = Mock(side_effect=slow_upload)
    with pytest.warns(UserWarning, match='lapsed.*while the coordinates were uploaded'):
        assert sdk.compute_visualization(FIELD, 8)['reduced'] == 8
    sdk.client.recompute_viz_rank.assert_called_once()
