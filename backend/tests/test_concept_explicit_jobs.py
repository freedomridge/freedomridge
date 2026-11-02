"""Explicit concept operations are user-lane jobs answering within five seconds."""

import asyncio
import json
import os
import sys
import time

import asyncpg
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from backend import access, concept_jobs, job_queue, job_wait
from backend.routers import concepts, datasets
from backend.routers import jobs as jobs_router
from backend.tests import job_helpers
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_concept_jobs import finish as finish_automatic
from backend.tests.test_concept_maintenance import concept as automatic_concept
from backend.tests.test_concept_maintenance import scores

registry_database = registry_helpers.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
USER = {'id': 1, 'role': 'admin'}
KIND = concept_jobs.EXPLICIT_KIND


@pytest.fixture
def concept_database(registry_database, monkeypatch):
    def run(scenario):
        async def wrapped(database):
            for module in (concepts, jobs_router, access):
                monkeypatch.setattr(module, 'db', database)
            await scenario(database)

        registry_database(wrapped)

    return run


async def add_samples(database, count, *, dataset_id=1, start=0, vector=None):
    """Samples with two-dimensional clip vectors spread around the unit circle."""
    ids = []
    for index in range(start, start + count):
        sample_id = await database.fetchval(
            "INSERT INTO samples(dataset_id,filepath,media_type) VALUES($1,$2,'image') RETURNING id",
            dataset_id,
            f'sample-{dataset_id}-{index}',
        )
        value = vector or f'[{1 + index % 7},{(index * 3) % 11 - 5}]'
        await database.execute(
            "INSERT INTO embeddings(sample_id,field_name,vector) VALUES($1,'clip',$2::text::vector)",
            sample_id,
            value,
        )
        ids.append(sample_id)
    return ids


async def oracle(database, concept_id):
    """Scores the definition implies, from the same SQL function as every scorer."""
    rows = await database.fetch(
        """SELECT e.sample_id, concept_score_value(c.kind, e.vector, c.weight::vector, c.bias) AS score
           FROM embedding_concepts c JOIN samples s ON s.dataset_id=c.dataset_id
           JOIN embeddings e ON e.sample_id=s.id AND e.field_name=c.embedding_field
           WHERE c.id=$1 AND e.vector IS NOT NULL ORDER BY e.sample_id""",
        concept_id,
    )
    return {row['sample_id']: row['score'] for row in rows}


def body(response):
    assert response.status_code == 202, response.body
    return json.loads(response.body)


def exclude_filter(concept_id, threshold=0.5):
    value = {'concept_id': concept_id, 'threshold': threshold}
    return {'field': '_concept', 'operator': 'concept_exclude', 'value': value}


async def count_excluded(concept_id):
    return (await datasets.list_samples_post(1, datasets.SamplesRequest(filters=exclude_filter(concept_id))))['total']


def test_fast_create_answers_final_json_within_five_seconds(concept_database):
    async def scenario(database):
        seeds = await add_samples(database, 20)
        async with job_helpers.runner(database, kinds=[KIND]):
            started = time.monotonic()
            created = await concepts.create_concept(
                1,
                concepts.ConceptCreate(name='leaf', seed_ids=seeds[:3]),
                user=USER,
            )
            assert time.monotonic() - started <= job_wait.ANSWER_SECONDS + 1
        assert isinstance(created, dict)
        assert set(created) == {
            'id',
            'dataset_id',
            'name',
            'embedding_field',
            'kind',
            'spec',
            'bias',
            'threshold',
            'scored_count',
            'created_at',
            'updated_at',
            'matched',
            'score_min',
            'score_max',
        }
        assert created['name'] == 'leaf' and created['scored_count'] == 20
        assert created['spec']['seed_ids'] == seeds[:3]
        assert await scores(database, created['id']) == pytest.approx(await oracle(database, created['id']))
        row = await database.fetchrow('SELECT * FROM embedding_concepts WHERE id=$1', created['id'])
        assert row['scores_ready'] and row['score_job_id'] is None and row['score_initialized']
        # The default threshold is calibrated into the score band, as the synchronous endpoint did.
        assert created['score_min'] < created['threshold'] < created['score_max']
        assert created['matched'] == await database.fetchval(
            'SELECT count(*) FROM embedding_concept_scores WHERE concept_id=$1 AND score>$2',
            created['id'],
            created['threshold'],
        )

    concept_database(scenario)


def test_slow_operation_answers_202_and_the_job_returns_the_same_shape(concept_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(job_wait, 'ANSWER_SECONDS', 0.3)
        seeds = await add_samples(database, 12)
        started = time.monotonic()
        handle = body(await concepts.create_concept(1, concepts.ConceptCreate(name='slow', seed_ids=seeds[:2]), USER))
        assert time.monotonic() - started < 2
        assert handle['state'] == 'queued' and set(handle) == {'job_id', 'state'}
        later = concepts.ConceptCreate(name='later', seed_ids=seeds[:1])
        assert body(await concepts.create_concept(1, later, USER, wait=False))
        job = await job_helpers.finish(database, KIND)
        assert str(job['id']) == handle['job_id'] and job['state'] == 'succeeded'
        result = job['result']
        assert result['name'] == 'slow' and result['scored_count'] == 12
        concept = await concepts._get_concept(1, result['id'])
        assert {key: concept[key] for key in ('matched', 'score_min', 'score_max', 'threshold', 'scored_count')} == {
            key: result[key] for key in ('matched', 'score_min', 'score_max', 'threshold', 'scored_count')
        }
        assert result['updated_at'] == concept['updated_at'].isoformat()
        await job_helpers.finish(database, KIND)
        names = {concept['name']: concept for concept in await concepts.list_concepts(1)}
        assert names['later']['scores_ready'] and names['later']['score_job'] is None

    concept_database(scenario)


def test_rescore_rebuild_refine_keep_their_result_shapes(concept_database):
    async def scenario(database):
        seeds = await add_samples(database, 30)
        async with job_helpers.runner(database, kinds=[KIND]):
            created = await concepts.create_concept(1, concepts.ConceptCreate(name='c', seed_ids=seeds[:3]), USER)
            concept_id = created['id']
            assert await concepts.rescore_concept(1, concept_id, full=True, user=USER) == {
                'concept_id': concept_id,
                'scored': 30,
            }
            await database.execute(
                "UPDATE embeddings SET vector='[-3,1]' WHERE sample_id = ANY($1::int[])",
                seeds[10:12],
            )
            await concept_jobs.dispatch_changes(database.pool)
            assert await concepts.rescore_concept(1, concept_id, full=False, user=USER) == {
                'concept_id': concept_id,
                'scored': 2,
            }
            assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))
            rebuilt = await concepts.rebuild_concept(
                1,
                concept_id,
                concepts.ConceptRebuild(seed_ids=seeds[5:8], threshold=0.75),
                USER,
            )
            assert rebuilt['spec']['seed_ids'] == seeds[5:8] and rebuilt['threshold'] == 0.75
            assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))
            retuned = await concepts.rebuild_concept(1, concept_id, concepts.ConceptRebuild(threshold=0.7), USER)
            assert retuned['threshold'] == 0.7
            hidden = await database.fetchval(
                'SELECT count(*) FROM embedding_concept_scores WHERE concept_id=$1 AND score>0.7',
                concept_id,
            )
            refined = await concepts.refine_concept(
                1,
                concept_id,
                concepts.ConceptRefine(keep_ids=[seeds[20]], exclude_ids=[seeds[21]], threshold=0.7),
                USER,
            )
            assert refined['kind'] == 'probe'
            assert refined['spec']['pos_added'] == [seeds[21]] and refined['spec']['neg_ids'] == [seeds[20]]
            # The cut level is preserved: about as many samples hidden under the new scale.
            assert refined['matched'] <= hidden
            assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))
            assert set(refined) == set(created)

    concept_database(scenario)


def test_manual_concepts_score_new_embeddings_and_rebuild(concept_database):
    async def scenario(database):
        seeds = await add_samples(database, 8)
        async with job_helpers.runner(database, kinds=[KIND]):
            concept_id = (await concepts.create_concept(1, concepts.ConceptCreate(name='m', seed_ids=seeds[:2]), USER))[
                'id'
            ]
            await concepts.set_auto_rescore(1, concept_id, concepts.AutoRescoreUpdate(enabled=False), USER)
            later = await add_samples(database, 5, start=100)
            assert await concepts.rescore_concept(1, concept_id, full=False, user=USER) == {
                'concept_id': concept_id,
                'scored': 5,
            }
            assert set(await scores(database, concept_id)) == set(seeds + later)
            assert await concepts.rescore_concept(1, concept_id, full=False, user=USER) == {
                'concept_id': concept_id,
                'scored': 0,
            }
            refined = await concepts.refine_concept(
                1,
                concept_id,
                concepts.ConceptRefine(exclude_ids=[later[0]]),
                USER,
            )
            assert refined['scored_count'] == 13
            assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))
            row = await database.fetchrow('SELECT * FROM embedding_concepts WHERE id=$1', concept_id)
            assert not row['auto_rescore'] and row['scores_ready']
            assert row['last_scored_embedding_id'] == await database.fetchval('SELECT max(id) FROM embeddings')

    concept_database(scenario)


async def fail_scores_for(database, sample_id):
    await database.execute(
        f"""CREATE FUNCTION fail_score() RETURNS trigger AS $$ BEGIN
                RAISE EXCEPTION 'injected scoring failure'; END $$ LANGUAGE plpgsql;
            CREATE TRIGGER fail_score BEFORE INSERT OR UPDATE ON embedding_concept_scores
            FOR EACH ROW WHEN (NEW.sample_id = {int(sample_id)}) EXECUTE FUNCTION fail_score()""",
    )


def test_definition_and_scores_stay_consistent_when_scoring_fails(concept_database):
    """A committed definition never serves the old definition's scores as its own."""

    async def scenario(database):
        seeds = await add_samples(database, 40)
        async with job_helpers.runner(database, kinds=[KIND]):
            concept_id = (await concepts.create_concept(1, concepts.ConceptCreate(name='c', seed_ids=seeds[:1]), USER))[
                'id'
            ]
            before = await count_excluded(concept_id)
            await fail_scores_for(database, seeds[-1])
            with pytest.raises((HTTPException, asyncpg.PostgresError)):
                await concepts.rebuild_concept(1, concept_id, concepts.ConceptRebuild(weight=[-1, 0]), USER)
        assert await database.fetchval('SELECT weight FROM embedding_concepts WHERE id=$1', concept_id) in (
            '[-1,0]',
            '[-1.0,0.0]',
        )
        assert await scores(database, concept_id) != pytest.approx(await oracle(database, concept_id))
        # Old scores under the new definition would silently answer `before`; the concept is refused.
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being rescored'):
            await count_excluded(concept_id)
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being rescored'):  # the app's 409
            await concepts.preview_concept(1, concept_id)
        listed = (await concepts.list_concepts(1))[0]
        assert listed['scores_ready'] is False and listed['score_job']['state'] == 'failed'
        assert not await concept_jobs.schedule_concepts(database.pool)
        await database.execute('DROP TRIGGER fail_score ON embedding_concept_scores')
        resumed = await jobs_router.resume_bulk_job(listed['score_job']['id'], USER)
        assert resumed['state'] == 'queued'
        job = await job_helpers.finish(database, KIND)
        assert job['state'] == 'succeeded' and job['attempt'] >= 2
        assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))
        assert await count_excluded(concept_id) != before or before == 0
        assert await database.fetchval('SELECT scores_ready FROM embedding_concepts WHERE id=$1', concept_id)

    concept_database(scenario)


async def submit(coroutine):
    return body(await coroutine)['job_id']


async def started_rebuild(database, concept_id, weight):
    """Submit a definition change, claim it and publish its definition unit."""
    await submit(concepts.rebuild_concept(1, concept_id, concepts.ConceptRebuild(weight=weight), USER, wait=False))
    job = await job_helpers.claim(database, KIND)
    job = await job_helpers.step(database, job, concept_jobs.explicit_handler(database.pool))
    assert job['state'] == 'running' and job['checkpoint']['phase'] == 'score'
    return job


def test_cancel_keeps_the_concept_refused_and_resume_finishes_it(concept_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(concept_jobs, 'EXPLICIT_BATCH_SIZE', 4)
        seeds = await add_samples(database, 40)
        async with job_helpers.runner(database, kinds=[KIND]):
            concept_id = (await concepts.create_concept(1, concepts.ConceptCreate(name='c', seed_ids=seeds[:1]), USER))[
                'id'
            ]
        job = await started_rebuild(database, concept_id, [-1, 2])
        handler = concept_jobs.explicit_handler(database.pool)
        job = await job_helpers.step(database, job, handler)
        assert job['checkpoint']['examined'] == 4
        cancelled = await job_queue.cancel_job(database.pool, job['id'])
        assert cancelled is not None and cancelled['cancel_requested']
        job = await job_helpers.step(database, job, handler)
        assert job['state'] == 'cancelled' and job['checkpoint']['examined'] == 4
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await count_excluded(concept_id)
        await concept_jobs.dispatch_changes(database.pool)
        assert not await concept_jobs.schedule_concepts(database.pool)
        assert (await jobs_router.resume_bulk_job(job['id'], USER))['state'] == 'queued'
        job = await job_helpers.finish(database, KIND)
        assert job['state'] == 'succeeded' and job['checkpoint']['examined'] == 40
        assert job['result']['scored_count'] == 40
        assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))
        await count_excluded(concept_id)

    concept_database(scenario)


def test_lost_lease_publishes_nothing_and_the_next_owner_resumes(concept_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(concept_jobs, 'EXPLICIT_BATCH_SIZE', 5)
        seeds = await add_samples(database, 20)
        async with job_helpers.runner(database, kinds=[KIND]):
            concept_id = (await concepts.create_concept(1, concepts.ConceptCreate(name='c', seed_ids=seeds[:1]), USER))[
                'id'
            ]
        job = await started_rebuild(database, concept_id, [0, 1])
        unit = await concept_jobs.explicit_handler(database.pool)(job)
        before = await scores(database, concept_id)
        await database.execute("UPDATE jobs SET lease_expires_at=NOW()-interval '10 minutes' WHERE id=$1", job['id'])
        with pytest.raises(job_queue.LeaseLost):
            await job_queue.publish_step(
                database.pool,
                job['id'],
                job['lease_token'],
                checkpoint=unit.checkpoint,
                publisher=unit.publish,
            )
        assert await scores(database, concept_id) == before
        job = await job_helpers.claim(database, KIND)
        assert job['attempt'] == 2 and job['checkpoint']['phase'] == 'score' and job['checkpoint']['examined'] == 0
        assert (await job_helpers.finish(database, KIND, job))['state'] == 'succeeded'
        assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))

    concept_database(scenario)


def test_process_killed_between_units_resumes_its_checkpoint(concept_database, monkeypatch, tmp_path):
    async def scenario(database):
        seeds = await add_samples(database, 30)
        async with job_helpers.runner(database, kinds=[KIND]):
            concept_id = (await concepts.create_concept(1, concepts.ConceptCreate(name='c', seed_ids=seeds[:1]), USER))[
                'id'
            ]
        job_id = await submit(
            concepts.rebuild_concept(1, concept_id, concepts.ConceptRebuild(weight=[3, -1]), USER, wait=False),
        )
        namespace = await database.fetchval('SELECT current_schema()')
        ready = tmp_path / 'ready'
        code = """
import asyncio, os, sys
from pathlib import Path
import asyncpg
from backend import concept_jobs, job_queue
from backend.database import init_connection
async def run():
    pool = await asyncpg.create_pool(os.environ['FRIDGE_TEST_POSTGRES_DSN'], min_size=1, max_size=3,
        init=init_connection, server_settings={'search_path': sys.argv[1]+',public'})
    concept_jobs.EXPLICIT_BATCH_SIZE = 7
    job = await job_queue.claim_job(pool, 'crash-test', kinds=['concept_rescore'], lease_seconds=600)
    for _ in range(2):
        unit = await concept_jobs.explicit_handler(pool)(job)
        job = await job_queue.publish_step(pool, job['id'], job['lease_token'], publisher=unit.publish,
                                           checkpoint=unit.checkpoint, done=unit.done)
    Path(sys.argv[2]).write_text(str(job['id']))
    await asyncio.Event().wait()
asyncio.run(run())
"""
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', code, namespace, str(ready))
        try:

            async def wait_ready():
                while not ready.exists():
                    assert process.returncode is None
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_ready(), 20)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        saved = await database.fetchval('SELECT checkpoint FROM jobs WHERE id=$1', job_id)
        assert saved['phase'] == 'score' and saved['examined'] == 7
        assert not await database.fetchval('SELECT scores_ready FROM embedding_concepts WHERE id=$1', concept_id)
        await database.execute("UPDATE jobs SET lease_expires_at=NOW()-interval '10 minutes' WHERE id=$1", job_id)
        job = await job_helpers.claim(database, KIND)
        assert str(job['id']) == job_id and job['attempt'] == 2 and job['checkpoint'] == saved
        assert (await job_helpers.finish(database, KIND, job))['state'] == 'succeeded'
        assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))

    concept_database(scenario)


def test_automatic_unit_measured_before_explicit_submission_cannot_publish(concept_database, monkeypatch):
    async def scenario(database):
        await add_samples(database, 12)
        concept_id = await automatic_concept(database)
        await finish_automatic(database, concept_id)
        await database.execute("UPDATE embeddings SET vector='[-1,-1]' WHERE sample_id=(SELECT min(id) FROM samples)")
        await concept_jobs.dispatch_changes(database.pool)
        automatic = await concept_jobs.request_scoring(database.pool, concept_id)
        claimed = await job_queue.claim_job(database.pool, 'auto', kinds=[concept_jobs.KIND])
        assert claimed is not None and automatic is not None
        measured = await concept_jobs.handler(database.pool)(claimed)
        assert measured.publish is not None
        await submit(concepts.rebuild_concept(1, concept_id, concepts.ConceptRebuild(weight=[0, 1]), USER, wait=False))
        # The explicit owner is visible to automatic scheduling at once.
        assert await concept_jobs.request_scoring(database.pool, concept_id) is None
        with pytest.raises(job_queue.QueueContended):
            await job_queue.publish_step(
                database.pool,
                claimed['id'],
                claimed['lease_token'],
                checkpoint=measured.checkpoint,
                publisher=measured.publish,
            )
        explicit = await job_helpers.finish(database, KIND)
        assert explicit['state'] == 'succeeded'
        expected = await oracle(database, concept_id)
        assert await scores(database, concept_id) == pytest.approx(expected)
        # The automatic job retries, sees the newer revision and ends without writing.
        await job_queue.defer_job(database.pool, claimed['id'], claimed['lease_token'], {}, delay_seconds=0)
        retried = await job_queue.claim_job(database.pool, 'auto', kinds=[concept_jobs.KIND])
        assert retried is not None and retried['id'] == automatic['id']
        finished = await concept_jobs.handler(database.pool)(retried)
        assert finished.finished and finished.publish is None
        assert await scores(database, concept_id) == pytest.approx(expected)

    concept_database(scenario)


def test_one_owner_per_concept_and_late_changes_stay_pending_for_upkeep(concept_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(concept_jobs, 'EXPLICIT_BATCH_SIZE', 3)
        seeds = await add_samples(database, 15)
        concept_id = await automatic_concept(database)
        await finish_automatic(database, concept_id)
        job_id = await submit(concepts.rescore_concept(1, concept_id, full=True, user=USER, wait=False))
        with pytest.raises(HTTPException) as conflict:
            await concepts.refine_concept(1, concept_id, concepts.ConceptRefine(exclude_ids=[seeds[0]]), USER)
        assert conflict.value.status_code == 409 and job_id in conflict.value.detail
        with pytest.raises(HTTPException) as toggled:
            await concepts.set_auto_rescore(1, concept_id, concepts.AutoRescoreUpdate(enabled=False), USER)
        assert toggled.value.status_code == 409
        job = await job_helpers.claim(database, KIND)
        handler = concept_jobs.explicit_handler(database.pool)
        job = await job_helpers.step(database, job, handler)
        job = await job_helpers.step(database, job, handler)
        # A change below the cursor and an arrival above the scan boundary, mid-job.
        await database.execute("UPDATE embeddings SET vector='[-2,1]' WHERE sample_id=$1", seeds[1])
        later = await add_samples(database, 2, start=50)
        await concept_jobs.dispatch_changes(database.pool)
        assert await concept_jobs.schedule_concepts(database.pool) == []
        job = await job_helpers.finish(database, KIND, job)
        assert job['result'] == {'concept_id': concept_id, 'scored': 15}
        assert set(await scores(database, concept_id)).isdisjoint(later)
        jobs = await concept_jobs.schedule_concepts(database.pool)
        assert len(jobs) == 1
        await finish_automatic(database, concept_id, jobs[0])
        assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))
        assert await database.fetchval('SELECT scored_count FROM embedding_concepts WHERE id=$1', concept_id) == 17

    concept_database(scenario)


def test_deleted_concept_fails_its_job_visibly(concept_database):
    async def scenario(database):
        seeds = await add_samples(database, 5)
        async with job_helpers.runner(database, kinds=[KIND]):
            concept_id = (await concepts.create_concept(1, concepts.ConceptCreate(name='c', seed_ids=seeds[:1]), USER))[
                'id'
            ]
        job = await started_rebuild(database, concept_id, [1, 1])
        await concepts.delete_concept(1, concept_id, USER)
        with pytest.raises(job_wait.JobConflict, match='deleted'):
            await concept_jobs.explicit_handler(database.pool)(job)

    concept_database(scenario)


def test_permissions_are_unchanged(concept_database):
    async def scenario(database):
        from backend.routers.auth import require_role

        seeds = await add_samples(database, 5)
        guard = require_role('power_user')
        with pytest.raises(HTTPException) as refused:
            guard({'id': 9, 'role': 'guest'})
        assert refused.value.status_code == 403
        for route in concepts.router.routes:
            assert isinstance(route, APIRoute)
            if route.methods and route.methods & {'POST', 'DELETE'}:
                calls = [getattr(dependency.call, '__qualname__', '') for dependency in route.dependant.dependencies]
                assert any('require_role' in call for call in calls), route.path
        job_id = await submit(
            concepts.create_concept(1, concepts.ConceptCreate(name='c', seed_ids=seeds[:1]), USER, wait=False),
        )
        await database.execute(
            "INSERT INTO users(id,username,role) VALUES (7,'viewer','guest'),(8,'other','power_user')",
        )
        await database.execute('INSERT INTO dataset_access(dataset_id,user_id) VALUES (1,7),(1,8)')
        await job_helpers.finish(database, KIND)
        viewer = await jobs_router.get_job(job_id, {'id': 7, 'role': 'guest'})
        assert viewer.state == 'succeeded' and 'result' not in viewer.model_dump(exclude_unset=True)
        submitter = await jobs_router.get_job(job_id, USER)
        assert submitter.result['name'] == 'c'
        with pytest.raises(HTTPException) as other:
            await jobs_router.resume_bulk_job(job_id, {'id': 8, 'role': 'power_user'})
        assert other.value.status_code == 403

    concept_database(scenario)


async def refusal(call):
    """The app maps ObjectNotInPrerequisiteStateError to 409; a route may also raise the 409 itself."""
    try:
        await call
    except asyncpg.ObjectNotInPrerequisiteStateError as error:
        return 409, str(error)
    except HTTPException as error:
        return error.status_code, str(error.detail)
    return 200, ''


def test_every_read_path_refuses_a_concept_being_rescored_with_409(concept_database, monkeypatch):
    async def scenario(database):
        from backend.routers import embeddings

        monkeypatch.setattr(embeddings, 'db', database)
        monkeypatch.setattr(concept_jobs, 'EXPLICIT_BATCH_SIZE', 4)
        seeds = await add_samples(database, 20)
        for index, sample_id in enumerate(seeds):
            await database.execute(
                "INSERT INTO embedding_coordinates(sample_id,field_name,x,y) VALUES($1,'clip',$2,$3)",
                sample_id,
                float(index),
                float(index % 5),
            )
            await database.execute(
                "UPDATE samples SET location=ST_SetSRID(ST_MakePoint($2,$3),4326) WHERE id=$1",
                sample_id,
                -100.0 + index * 0.01,
                40.0,
            )
        async with job_helpers.runner(database, kinds=[KIND]):
            concept_id = (await concepts.create_concept(1, concepts.ConceptCreate(name='c', seed_ids=seeds[:1]), USER))[
                'id'
            ]
        await started_rebuild(database, concept_id, [-1, 2])
        node = exclude_filter(concept_id)
        calls = {
            'count': datasets.list_samples_post(1, datasets.SamplesRequest(filters=node, limit=0)),
            'grid page': datasets.list_samples_post(
                1,
                datasets.SamplesRequest(filters=node, limit=50, skip_count=True),
            ),
            'sdk slim': datasets.list_samples_post(
                1,
                datasets.SamplesRequest(filters=node, limit=50, skip_count=True, slim=True, fields='id,filepath'),
            ),
            # The impl, not the POST route: the route takes a Request for cancel-on-disconnect.
            'field stats': datasets._get_dataset_field_stats_impl(1, node),
            'field stats exact': datasets._get_dataset_field_stats_impl(1, node, lightning_mode=False),
            'histogram': datasets.get_histogram(1, 'media_type', bins=None, filters=json.dumps(node), top=50),
            'map points': datasets.get_dataset_map_points_post(1, datasets.MapPointsRequest(filters=node)),
            'map extent': datasets.get_dataset_map_extent(1, datasets.MapExtentRequest(filters=node)),
            'scatter': embeddings.get_embeddings_post(1, embeddings.EmbeddingsRequest(filters=node)),
        }
        outcomes = {name: await refusal(call) for name, call in calls.items()}
        wrong = {name: result for name, result in outcomes.items() if result[0] != 409 or 'rescored' not in result[1]}
        assert not wrong, wrong

    concept_database(scenario)


@pytest.mark.parametrize('auto', [True, False])
def test_partial_rescore_cannot_ready_a_stopped_definition_change(concept_database, monkeypatch, auto):
    """rescore(full=False) after a stopped rebuild must not re-enable mixed scores."""

    async def scenario(database):
        monkeypatch.setattr(concept_jobs, 'EXPLICIT_BATCH_SIZE', 4)
        seeds = await add_samples(database, 40)
        async with job_helpers.runner(database, kinds=[KIND]):
            concept_id = (await concepts.create_concept(1, concepts.ConceptCreate(name='c', seed_ids=seeds[:1]), USER))[
                'id'
            ]
        if not auto:
            await concepts.set_auto_rescore(1, concept_id, concepts.AutoRescoreUpdate(enabled=False), USER)
        await database.execute('DELETE FROM concept_embedding_pending')
        await database.execute('DELETE FROM concept_score_pending')
        handler = concept_jobs.explicit_handler(database.pool)
        job = await started_rebuild(database, concept_id, [-1, 2])
        job = await job_helpers.step(database, job, handler)
        await job_queue.cancel_job(database.pool, job['id'])
        assert (await job_helpers.step(database, job, handler))['state'] == 'cancelled'
        with pytest.raises(HTTPException) as refused:
            await concepts.rescore_concept(1, concept_id, full=False, user=USER)
        assert refused.value.status_code == 409 and 'resume' in refused.value.detail.lower()
        row = await database.fetchrow(
            'SELECT scores_ready,score_job_id FROM embedding_concepts WHERE id=$1',
            concept_id,
        )
        assert not row['scores_ready'] and row['score_job_id'] == job['id']
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await count_excluded(concept_id)
        # The stopped job still owns the concept and finishes it; then a partial rescore is fine.
        assert (await jobs_router.resume_bulk_job(job['id'], USER))['state'] == 'queued'
        assert (await job_helpers.finish(database, KIND))['state'] == 'succeeded'
        assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))
        await submit(concepts.rescore_concept(1, concept_id, full=False, user=USER, wait=False))
        assert (await job_helpers.finish(database, KIND))['state'] == 'succeeded'
        await count_excluded(concept_id)

    concept_database(scenario)


def test_a_partial_pass_never_marks_scores_ready(concept_database, monkeypatch):
    """Defence in depth for the mixed-scores case: only a full pass publishes scores_ready."""

    async def scenario(database):
        seeds = await add_samples(database, 8)
        async with job_helpers.runner(database, kinds=[KIND]):
            concept_id = (await concepts.create_concept(1, concepts.ConceptCreate(name='c', seed_ids=seeds[:1]), USER))[
                'id'
            ]
        await submit(concepts.rescore_concept(1, concept_id, full=False, user=USER, wait=False))
        await database.execute('UPDATE embedding_concepts SET scores_ready=FALSE WHERE id=$1', concept_id)
        assert (await job_helpers.finish(database, KIND))['state'] == 'succeeded'
        assert not await database.fetchval('SELECT scores_ready FROM embedding_concepts WHERE id=$1', concept_id)

    concept_database(scenario)


def test_resume_queues_behind_another_started_job_of_the_lane(concept_database, monkeypatch):
    """Resuming while another user-lane job is started answered an unmapped 500."""

    async def scenario(database):
        monkeypatch.setattr(concept_jobs, 'EXPLICIT_BATCH_SIZE', 4)
        seeds = await add_samples(database, 40)
        async with job_helpers.runner(database, kinds=[KIND]):
            a = (await concepts.create_concept(1, concepts.ConceptCreate(name='a', seed_ids=seeds[:1]), USER))['id']
            b = (await concepts.create_concept(1, concepts.ConceptCreate(name='b', seed_ids=seeds[1:2]), USER))['id']
        handler = concept_jobs.explicit_handler(database.pool)
        job_a = await started_rebuild(database, a, [-1, 2])
        job_a = await job_helpers.step(database, job_a, handler)
        await job_queue.cancel_job(database.pool, job_a['id'])
        job_a = await job_helpers.step(database, job_a, handler)
        await submit(concepts.rebuild_concept(1, b, concepts.ConceptRebuild(weight=[2, 1]), USER, wait=False))
        job_b = await job_helpers.claim(database, KIND)
        assert job_b is not None and job_b['args']['concept_id'] == b
        job_b = await job_helpers.step(database, job_b, handler, release=True)
        assert job_b['state'] == 'queued' and job_b['started_at'] is not None
        assert (await jobs_router.resume_bulk_job(job_a['id'], USER))['state'] == 'queued'
        # A waits behind B, which keeps its lane; then A finishes from its checkpoint.
        claimed = await job_helpers.claim(database, KIND)
        assert claimed is not None and claimed['id'] == job_b['id']
        await job_helpers.finish(database, KIND, claimed)
        resumed = await job_helpers.finish(database, KIND)
        assert resumed['id'] == job_a['id'] and resumed['state'] == 'succeeded'
        for concept_id in (a, b):
            assert await scores(database, concept_id) == pytest.approx(await oracle(database, concept_id))

    concept_database(scenario)


def test_wrong_dimension_weight_is_a_400_and_submits_nothing(concept_database):
    """A raw weight of the wrong size used to fail its job (500) after publishing it."""

    async def scenario(database):
        seeds = await add_samples(database, 6)
        with pytest.raises(HTTPException) as created:
            await concepts.create_concept(1, concepts.ConceptCreate(name='w3', weight=[1, 2, 3]), USER)
        assert created.value.status_code == 400 and '3 dims' in created.value.detail
        async with job_helpers.runner(database, kinds=[KIND]):
            ok = (await concepts.create_concept(1, concepts.ConceptCreate(name='ok', seed_ids=seeds[:1]), USER))['id']
        before = await database.fetchrow('SELECT weight,scores_ready FROM embedding_concepts WHERE id=$1', ok)
        with pytest.raises(HTTPException) as rebuilt:
            await concepts.rebuild_concept(1, ok, concepts.ConceptRebuild(weight=[1, 2, 3]), USER)
        assert rebuilt.value.status_code == 400
        assert await database.fetchrow('SELECT weight,scores_ready FROM embedding_concepts WHERE id=$1', ok) == before
        assert not await database.fetchval("SELECT 1 FROM embedding_concepts WHERE name='w3'")
        assert await database.fetchval("SELECT count(*) FROM jobs WHERE kind='concept_rescore'") == 1

    concept_database(scenario)


def test_training_samples_cast_vectors_after_the_random_limit(concept_database):
    """The cast to text ran for every candidate vector below ORDER BY random() LIMIT."""

    async def scenario(database):
        await add_samples(database, 30)
        for name, sql, params in (
            ('background', concept_jobs.BACKGROUND_SQL, [1, 'clip', [], 5]),
            ('scored', concept_jobs.SCORED_SQL.format(op='>'), [1, 'clip', 0.5, 5, 1, []]),
        ):
            plan = json.loads(await database.fetchval(f'EXPLAIN (VERBOSE, FORMAT JSON) {sql}', *params))[0]['Plan']

            def below_limit(node, under=False):
                if node['Node Type'] == 'Limit':
                    under = True
                outputs = ' '.join(node.get('Output', [])) if under and node['Node Type'] != 'Limit' else ''
                found = [node['Node Type']] if '::text' in outputs else []
                for child in node.get('Plans', []):
                    found += below_limit(child, under)
                return found

            assert below_limit(plan) == [], (name, plan)

    concept_database(scenario)
