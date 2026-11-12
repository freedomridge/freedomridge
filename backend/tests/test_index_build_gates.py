"""One index build at a time on the whole database (index_jobs.enqueue_build): a CREATE INDEX CONCURRENTLY of an
expression or partial index waits for every other one's snapshot, even on another table, so a second build at once
would only wait and could run out its budget. Every job that runs one holds the index_build resource, and the queue
starts none while another job owns it (job_queue.claim_job): a build queued meanwhile waits unstarted, holding no
dataset lane. The rebuild drains still decide what is queued (field rebuilds first, lane-aware). Also: a two-part sort
counts the samples with a value only for a page past them all, once."""

import ast
import importlib
import inspect
import json
import os
import types
import uuid
from pathlib import Path

import asyncpg
import pytest
from fastapi import HTTPException

from backend import access, bulk_jobs, follower_sync, index_actions, index_jobs, job_queue, job_service
from backend.routers import datasets
from backend.routers import jobs as jobs_router
from backend.tests import job_helpers, test_field_registry_postgres
from backend.tests.test_field_index_rebuilds import builds, queue_field, rows, sample_counts
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = test_field_registry_postgres.registry_database
needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
FIELD, ANN, RESTORE = index_jobs.FIELD_KIND, index_jobs.ANN_KIND, index_actions.RESTORE_KIND
BACKEND = Path(index_jobs.__file__).parent
WAITING = 'for another index build to finish (one index build at a time)'


def _resolve(node, namespace):
    """The value a constant, or a chain of names and attributes, has in a module's namespace (None if neither)."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return namespace.get(node.id)
    if isinstance(node, ast.Attribute):
        owner = _resolve(node.value, namespace)
        return None if owner is None else getattr(owner, node.attr, None)
    return None


def _handlers():
    """The job runner's handlers (job_service.run_service's `handlers`), each kind with the backend functions its
    handler calls."""
    service = ast.parse(inspect.getsource(job_service.run_service)).body[0]
    assert isinstance(service, ast.AsyncFunctionDef)
    local = {node.name: node for node in service.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    (registry,) = [
        node.value
        for node in service.body
        if isinstance(node, ast.Assign) and [getattr(target, 'id', None) for target in node.targets] == ['handlers']
    ]
    assert isinstance(registry, ast.Dict)
    namespace = vars(job_service)
    found = {}
    for key, value in zip(registry.keys, registry.values):
        body = local[value.id] if isinstance(value, ast.Name) and value.id in local else value
        called = {_resolve(node.func, namespace) for node in ast.walk(body) if isinstance(node, ast.Call)} | {
            _resolve(node.func.func, namespace)
            for node in ast.walk(body)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Call)
        }
        found[_resolve(key, namespace)] = [function for function in called if inspect.isfunction(function)]
    return found


def _reaches_build(function, seen) -> bool:
    """Whether the function, or a backend function it names (directly or as a module's attribute), calls _build."""
    if function.__code__ in seen or not function.__module__.startswith('backend'):
        return False
    seen.add(function.__code__)
    codes, names = [function.__code__], set()
    while codes:
        code = codes.pop()
        names |= set(code.co_names)
        codes += [constant for constant in code.co_consts if isinstance(constant, types.CodeType)]
    if '_build' in names:
        return True
    for name in names:
        value = function.__globals__.get(name)
        members = [value]
        if isinstance(value, types.ModuleType):
            backend = value.__name__.startswith('backend')
            members = [getattr(value, attribute, None) for attribute in names] if backend else []
        for member in members:
            if inspect.isfunction(member) and _reaches_build(member, seen):
                return True
    return False


def _queue_calls():
    """Every enqueue_job and enqueue_build call in the backend's code: (function, kind, where)."""
    calls = []
    for path in sorted(BACKEND.rglob('*.py')):
        relative = path.relative_to(BACKEND)
        source = path.read_text()
        if relative.parts[0] == 'tests' or ('enqueue_job(' not in source and 'enqueue_build(' not in source):
            continue
        namespace = vars(importlib.import_module('.'.join(('backend', *relative.with_suffix('').parts))))

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.within = []

            def visit_FunctionDef(self, node):
                self.within.append(node.name)
                self.generic_visit(node)
                self.within.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                name = getattr(node.func, 'attr', getattr(node.func, 'id', None))
                if name in ('enqueue_job', 'enqueue_build') and self.within[-1:] != ['enqueue_build']:
                    kind = (
                        node.args[1] if len(node.args) > 1 else next(k.value for k in node.keywords if k.arg == 'kind')
                    )
                    calls.append((name, _resolve(kind, namespace), f'{relative}:{node.lineno}'))
                self.generic_visit(node)

        Visitor().visit(ast.parse(source))
    return calls


def test_every_job_that_builds_an_index_is_queued_holding_the_index_build_resource():
    """Found from the code, not a list: the kinds whose handler reaches index_jobs._build (the one place that runs
    CREATE INDEX CONCURRENTLY for a job) are queued only through enqueue_build, which gives them the resource."""
    build_kinds = {kind for kind, functions in _handlers().items() if any(_reaches_build(f, set()) for f in functions)}
    assert {FIELD, ANN, RESTORE} <= build_kinds  # the walk finds them (promote and the advisor's Index are FIELD)
    calls = _queue_calls()
    unknown = [where for _, kind, where in calls if not isinstance(kind, str)]
    assert unknown == [], f'queued with a kind this test cannot read: {unknown}'
    direct = [(kind, where) for name, kind, where in calls if name == 'enqueue_job' and kind in build_kinds]
    assert direct == [], f'index builds queued without the index_build resource: {direct}'
    assert build_kinds <= {kind for name, kind, _ in calls if name == 'enqueue_build'}
    assert {kind for name, kind, _ in calls if name == 'enqueue_build'} <= build_kinds


async def vector_rows(database, *fields, dataset_id=2):
    for field in fields:  # 2.9.0's rows
        await database.execute(
            'INSERT INTO ann_index_rebuilds (dataset_id, field_name) VALUES ($1, $2)',
            dataset_id,
            field,
        )


async def running_vector_build(database, dataset_id=2, field='clip'):
    """A vector index build someone started by hand (build_ann_index), claimed by a runner: running."""
    await index_jobs.submit_ann_index(database.pool, dataset_id, USER['id'], field)
    return await job_helpers.claim(database, ANN)


async def unstarted(database, job_id):
    """A job that waits without having started: queued, never claimed, nothing put back."""
    job = await database.fetchrow('SELECT state, started_at, attempt, error, resources FROM jobs WHERE id = $1', job_id)
    assert (job['state'], job['started_at'], job['attempt'], job['error']) == ('queued', None, 0, None)
    assert job['resources'] == {'exclusive': index_jobs.INDEX_BUILD}


async def complete(database, job):
    await job_queue.complete_job(database.pool, job['id'], job['lease_token'])


async def lane_job(database, dataset_id):
    """Another user-lane job of the dataset (a bulk edit), queued: claimable unless something holds the lane."""
    async with job_queue._transaction(database.pool) as connection:
        return await job_queue.enqueue_job(connection, bulk_jobs.KIND, dataset_id=dataset_id, args={'operation': 'tag'})


@needs_postgres
def test_a_promote_during_a_vector_build_waits_unstarted_and_holds_no_lane(registry_database, monkeypatch):
    async def scenario(database):
        for module in (jobs_router, access):
            monkeypatch.setattr(module, 'db', database)
        await ingest([{'captured': '2026-01-01'}])
        vector = await running_vector_build(database)
        promote = await datasets.promote_field(1, 'captured', USER, wait=False)
        promote_id = uuid.UUID(json.loads(promote.body)['job_id'])
        assert await job_helpers.claim(database, FIELD) is None
        await unstarted(database, promote_id)
        bulk = await lane_job(database, 1)
        claimed = await job_helpers.claim(database, bulk_jobs.KIND)  # dataset 1's lane is free meanwhile
        assert claimed['id'] == bulk['id']
        await complete(database, claimed)
        status = await jobs_router.get_job(promote_id, USER)
        assert status.waiting_for == WAITING
        (listed,) = (await jobs_router.list_active_dataset_jobs(1, 25, None, USER)).waiting
        assert listed.waiting_for == WAITING
        await complete(database, vector)
        assert (await job_helpers.finish(database, FIELD))['id'] == promote_id
        assert await database.fetchval("SELECT is_promoted FROM dataset_fields WHERE field_name = 'captured'")
        assert (await jobs_router.get_job(promote_id, USER)).waiting_for is None

    registry_database(scenario)


@needs_postgres
def test_a_vector_build_during_a_field_build_waits_unstarted_and_holds_no_lane(registry_database):
    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}])
        await datasets.promote_field(1, 'captured', USER, wait=False)
        field = await job_helpers.claim(database, FIELD)
        vector = await index_jobs.submit_ann_index(database.pool, 2, USER['id'], 'clip')
        assert await job_helpers.claim(database, ANN) is None
        await unstarted(database, vector['id'])
        bulk = await lane_job(database, 2)
        claimed = await job_helpers.claim(database, bulk_jobs.KIND)  # dataset 2's lane is free meanwhile
        assert claimed['id'] == bulk['id']
        await complete(database, claimed)
        assert (await job_helpers.finish(database, FIELD, field))['state'] == 'succeeded'
        assert (await job_helpers.claim(database, ANN))['id'] == vector['id']

    registry_database(scenario)


@needs_postgres
def test_a_rebuild_queued_beside_another_rebuild_waits_unstarted(registry_database):
    """The drains never queue a vector rebuild beside a field build; queued anyway (here by hand), the queue still
    starts one at a time."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}])
        await sample_counts(database, {1: 10})
        await queue_field(database, (1, 'captured'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        field = await job_helpers.claim(database, FIELD)
        async with job_queue._transaction(database.pool) as connection:
            vector = await index_jobs.enqueue_build(
                connection,
                ANN,
                dataset_id=2,
                args={'field': 'clip', 'rebuild': True},
            )
        assert await job_helpers.claim(database, ANN) is None
        await unstarted(database, vector['id'])
        assert (await job_helpers.finish(database, FIELD, field))['state'] == 'succeeded'
        assert (await job_helpers.claim(database, ANN))['id'] == vector['id']

    registry_database(scenario)


@needs_postgres
def test_a_build_put_back_after_it_started_still_holds_the_resource(registry_database):
    """Put back (here: its table busy with another index operation), a build that started still owns index_build,
    as it owns its lane: no second build is started meanwhile. A map re-fit's map_fit behaves the same way."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}])
        await datasets.promote_field(1, 'captured', USER, wait=False)
        holder = await asyncpg.connect(os.environ['FRIDGE_TEST_POSTGRES_DSN'])
        try:
            assert await holder.fetchval(
                'SELECT pg_try_advisory_lock($1, hashtext($2))',
                index_jobs.TABLE_LOCK,
                'samples',
            )
            field = await job_helpers.claim(database, FIELD)
            with pytest.raises(job_queue.QueueContended, match='samples'):
                await job_helpers.step(database, field, job_helpers.handlers(database.pool)[FIELD])
            detail = {'type': 'QueueContended', 'message': 'busy'}
            put_back = await job_queue.defer_job(
                database.pool,
                field['id'],
                field['lease_token'],
                detail,
                delay_seconds=0,
                count_failure=False,
            )
            assert put_back['state'] == 'queued' and put_back['started_at'] is not None
            vector = await index_jobs.submit_ann_index(database.pool, 2, USER['id'], 'clip')
            assert await job_helpers.claim(database, ANN) is None
            await unstarted(database, vector['id'])
        finally:
            await holder.close()
        assert (await job_helpers.finish(database, FIELD))['state'] == 'succeeded'
        claimed = await job_helpers.claim(database, ANN)
        assert claimed['id'] == vector['id']
        await complete(database, claimed)

        async with job_queue._transaction(database.pool) as connection:
            first, second = [
                await job_queue.enqueue_job(
                    connection,
                    'map_refit',
                    dataset_id=dataset,
                    resources={'exclusive': 'map_fit'},
                )
                for dataset in (1, 2)
            ]
        claimed = await job_helpers.claim(database, 'map_refit')
        assert claimed['id'] == first['id']
        await job_queue.defer_job(database.pool, claimed['id'], claimed['lease_token'], detail, delay_seconds=0)
        again = await job_helpers.claim(database, 'map_refit')
        assert again['id'] == first['id']  # the put-back one, never the second
        await complete(database, again)
        assert (await job_helpers.claim(database, 'map_refit'))['id'] == second['id']

    registry_database(scenario)


@needs_postgres
def test_a_restore_of_no_dataset_holds_the_resource_too(registry_database):
    """A restore of a removed index belongs to no dataset (no lane) but owns index_build like any build."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}])
        async with job_queue._transaction(database.pool) as connection:
            restore = await index_jobs.enqueue_build(connection, RESTORE, lane='user', args={'index_name': 'x'})
        claimed = await job_helpers.claim(database, RESTORE)
        assert claimed['id'] == restore['id'] and claimed['dataset_id'] is None
        await datasets.promote_field(1, 'captured', USER, wait=False)
        assert await job_helpers.claim(database, FIELD) is None
        await complete(database, claimed)
        assert (await job_helpers.finish(database, FIELD))['state'] == 'succeeded'

    registry_database(scenario)


@needs_postgres
def test_field_rebuilds_drain_before_any_vector_rebuild(registry_database):
    async def scenario(database):
        await ingest([{'captured': '2026-01-01', 'seen': '2026-01-02'}])
        await sample_counts(database, {1: 10})
        await vector_rows(database, 'clip', 'dino')
        await queue_field(database, (1, 'captured'), (1, 'seen'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert await index_jobs.queue_rebuilds(database.pool) == 0  # the vector rows wait for the queued field build
        assert await builds(database, ANN) == []
        for _ in range(2):
            assert (await job_helpers.finish(database, FIELD))['state'] == 'succeeded'
            assert await builds(database, ANN) == []
            assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert [build[:2] for build in await builds(database)] == [(1, 'captured'), (1, 'seen')]
        assert [build[:2] for build in await builds(database, ANN)] == [(2, 'clip')]  # only now
        assert await rows(database) == [('vector', 2, 'clip'), ('vector', 2, 'dino')]

    registry_database(scenario)


@needs_postgres
def test_a_field_rebuild_queued_during_a_vector_build_waits_for_it_unstarted(registry_database):
    """The field drain doesn't look at vector builds: the rebuild is queued, waits unstarted for the vector build
    (claim-time), shows as waiting in the Indexes tab, and holds the next vector rebuilds back, so it goes first."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}])
        await vector_rows(database, 'dino')
        vector = await running_vector_build(database)
        await queue_field(database, (1, 'captured'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert await index_jobs.queue_rebuilds(database.pool) == 0
        ((field_id, *_),) = await database.fetch('SELECT id FROM jobs WHERE kind = $1', FIELD)
        assert await job_helpers.claim(database, FIELD) is None
        await unstarted(database, field_id)
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        (captured,) = [field for field in fields if field.get('name') == 'captured']
        assert 'Another index build is running' in captured['index_rebuild_waiting']
        await complete(database, vector)
        assert await index_jobs.queue_rebuilds(database.pool) == 0  # the field rebuild still goes first
        assert (await job_helpers.finish(database, FIELD))['state'] == 'succeeded'
        assert await rows(database) == [('vector', 2, 'dino')]

    registry_database(scenario)


@needs_postgres
def test_a_build_that_cant_run_doesnt_hold_the_other_kind(registry_database):
    """Only a build that can run holds the vector drain back: a field build whose dataset's lane another job holds
    (a map re-fit here) doesn't stop the vector rebuilds of another dataset; and a vector build never holds the
    field drain."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01', 'seen': '2026-01-02'}])
        async with job_queue._transaction(database.pool) as connection:
            refit = await job_queue.enqueue_job(connection, 'map_refit', dataset_id=1, lane='user')
        await database.execute('UPDATE jobs SET started_at = now() WHERE id = $1', refit['id'])  # it holds the lane
        await datasets.promote_field(1, 'captured', USER, wait=False)  # queued behind the re-fit
        await vector_rows(database, 'clip')
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert [build[:2] for build in await builds(database, ANN)] == [(2, 'clip')]
        vector = await job_helpers.claim(database, ANN)
        await complete(database, vector)
        # The other way: dataset 2's lane held, a vector build queued there, and a field rebuild of dataset 1's.
        await database.execute('UPDATE jobs SET dataset_id = 2 WHERE id = $1', refit['id'])
        await database.execute("UPDATE jobs SET state = 'cancelled', finished_at = now() WHERE kind = $1", FIELD)
        await index_jobs.submit_ann_index(database.pool, 2, USER['id'], 'dino')
        await queue_field(database, (1, 'seen'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert [build[:2] for build in await builds(database) if build[2] == 'queued'] == [(1, 'seen')]

    registry_database(scenario)


async def stopped_bulk(database, dataset_id):
    """A rename stopped by its user (kept to be resumed or discarded): it holds the dataset's fields, not its lane."""
    job = uuid.uuid4()
    await database.execute(
        """INSERT INTO jobs (id, kind, dataset_id, args, lane, state, started_at, finished_at)
           VALUES ($1, 'bulk_mutation', $2, '{"operation": "rename"}', 'user', 'cancelled', now(), now())""",
        job,
        dataset_id,
    )
    await database.execute('INSERT INTO job_controls (job_id) VALUES ($1) ON CONFLICT DO NOTHING', job)
    return job


@needs_postgres
def test_a_stopped_bulk_job_parks_its_datasets_rebuild_without_failing_it(registry_database, monkeypatch):
    """A rename, clone or clear that is unfinished (a stopped one too) refuses field builds of its dataset. The
    dataset's rebuild is queued parked: unclaimable, holding nothing, shown waiting with the bulk job named in the
    jobs list and the Indexes tab; the other datasets' rebuilds go meanwhile; it builds once the bulk job is done."""

    async def scenario(database):
        for module in (jobs_router, access):
            monkeypatch.setattr(module, 'db', database)
        await ingest([{'captured': '2026-01-01'}], prefix='one')
        await ingest([{'captured': '2026-01-01'}], dataset_id=2, prefix='two')
        await sample_counts(database, {1: 10, 2: 20})
        stopped = await stopped_bulk(database, 1)
        await queue_field(database, (1, 'captured'), (2, 'captured'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        ((parked, error),) = await database.fetch(
            "SELECT id, error FROM jobs WHERE kind = $1 AND dataset_id = 1 "
            "AND available_at > now() + interval '12 hours'",
            FIELD,
        )
        assert error['code'] == 'fields_held' and f'rename job {stopped}' in error['message']
        assert (await jobs_router.get_job(parked, USER)).waiting_for.startswith(
            f'for the unfinished bulk rename job {stopped}',
        )
        assert await job_helpers.claim(database, FIELD) is None  # parked, and dataset 2's isn't queued yet
        assert await index_jobs.queue_rebuilds(database.pool) == 1  # dataset 2's, not held up
        built = await job_helpers.finish(database, FIELD)
        assert (built['dataset_id'], built['state']) == (2, 'succeeded')
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        (captured,) = [field for field in fields if field.get('name') == 'captured']
        assert f'rename job {stopped}' in captured['index_rebuild_waiting']
        assert await index_jobs.queue_rebuilds(database.pool) == 0
        await database.execute("""UPDATE jobs SET result = '{"discarded": {"by": "admin"}}' WHERE id = $1""", stopped)
        assert await index_jobs.queue_rebuilds(database.pool) == 0  # woken, not queued twice
        assert (await job_helpers.finish(database, FIELD))['id'] == parked
        assert await rows(database) == []
        assert [build[2] for build in await builds(database)] == ['succeeded', 'succeeded']

    registry_database(scenario)


@needs_postgres
def test_a_rebuild_claimed_before_its_dataset_was_parked_ends_unbuilt_and_is_queued_again(registry_database):
    """A bulk job stopped between two drain ticks: the rebuild already queued is claimed, finds it, and ends without
    building, saying so (never a failure, never a finished build); its row stays and the next tick parks a new one."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}])
        await queue_field(database, (1, 'captured'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        stopped = await stopped_bulk(database, 1)
        ended = await job_helpers.finish(database, FIELD)
        assert ended['state'] == 'succeeded' and ended['failures'] == 0
        assert ended['result']['rebuilt'] is False and ended['message'].startswith(index_jobs.NOT_REBUILT)
        assert f'rename job {stopped}' in ended['message']
        assert await rows(database) == [('field', 1, 'captured')]
        assert not await database.fetchval("SELECT is_promoted FROM dataset_fields WHERE field_name = 'captured'")
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        parked = await database.fetchval(
            "SELECT error->>'code' FROM jobs WHERE kind = $1 AND state = 'queued' AND started_at IS NULL",
            FIELD,
        )
        assert parked == 'fields_held'

    registry_database(scenario)


@needs_postgres
def test_a_stopped_bulk_job_can_be_resumed_while_its_datasets_rebuild_waits(registry_database):
    """A rebuild waiting for another build holds no lane, so a stopped bulk job of its dataset resumes and runs."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}], dataset_id=2, prefix='two')
        vector = await running_vector_build(database, dataset_id=1)
        await queue_field(database, (2, 'captured'))
        assert await index_jobs.queue_rebuilds(database.pool) == 1
        assert await job_helpers.claim(database, FIELD) is None
        stopped = await stopped_bulk(database, 2)
        assert (await bulk_jobs.resume(database.pool, stopped))['state'] == 'queued'
        assert (await job_helpers.claim(database, bulk_jobs.KIND))['id'] == stopped
        await complete(database, vector)

    registry_database(scenario)


@needs_postgres
def test_an_array_field_is_refused_while_an_index_build_runs(registry_database):
    """Its GIN build would wait for a running build's snapshot: 409 instead, and the follower's sync waits
    (QueueContended) to try again, never failing the mirror. While it builds, it holds index builds off."""

    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}])
        vector = await running_vector_build(database)
        with pytest.raises(HTTPException) as refused:
            await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='labels_x'), USER)
        assert refused.value.status_code == 409 and 'index build is running' in refused.value.detail
        assert refused.value.headers == {'Retry-After': '30'}  # a refusal with no effect: clients retry it
        assert not await database.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid = 'samples'::regclass AND attname = 'labels_x')",
        )
        with pytest.raises(job_queue.QueueContended, match='index build is running'):
            await follower_sync.ensure_array_columns(database.pool, 1, ['labels_y'])
        await complete(database, vector)
        assert (await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='labels_x'), USER))[
            'created'
        ]
        # Held by an array field's GIN build (the queue's claim lock for index_build): no build starts.
        holder = await asyncpg.connect(os.environ['FRIDGE_TEST_POSTGRES_DSN'])
        try:
            assert await holder.fetchval(
                'SELECT pg_try_advisory_lock($1, hashtext($2))',
                job_queue.EXCLUSIVE_LOCK,
                index_jobs.INDEX_BUILD,
            )
            await datasets.promote_field(1, 'captured', USER, wait=False)
            assert await job_helpers.claim(database, FIELD) is None
        finally:
            await holder.close()
        assert (await job_helpers.finish(database, FIELD))['state'] == 'succeeded'

    registry_database(scenario)


@needs_postgres
def test_the_array_fields_claim_lock_is_free_after_a_request_that_died_holding_it(registry_database, monkeypatch):
    """add_array_column holds the queue's claim lock for index builds on a pooled connection. A failure between the
    lock and the unlock releases it (its `finally`), and so does the app pool's reset when a connection goes back
    still holding it: either way a build is claimed afterwards."""
    from backend.database import init_connection
    from backend.request_limits import RequestConnection, RequestPool

    async def scenario(database):
        await ingest([{'captured': '2026-01-01'}])
        claims = (job_queue.EXCLUSIVE_LOCK, index_jobs.INDEX_BUILD)

        async def broken(connection, resource, job_id=None):
            raise RuntimeError('the request died here')

        with monkeypatch.context() as patched:
            patched.setattr(job_queue, 'exclusive_owned', broken)
            with pytest.raises(RuntimeError):
                await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name='labels_z'), USER)
        # A connection given back still holding it, as by a request cancelled before its `finally`: a pool made as
        # the app makes its pool (database.Database.connect).
        app_pool = RequestPool(
            await asyncpg.create_pool(
                os.environ['FRIDGE_TEST_POSTGRES_DSN'],
                min_size=1,
                max_size=1,
                init=init_connection,
                connection_class=RequestConnection,
            ),
        )
        try:
            async with app_pool.acquire() as connection:
                assert await connection.fetchval('SELECT pg_try_advisory_lock($1, hashtext($2))', *claims)
            async with app_pool.acquire() as connection:  # the same connection, reset
                held = await connection.fetchval(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()",
                )
                assert held == 0
        finally:
            await app_pool.close()
        await datasets.promote_field(1, 'captured', USER, wait=False)
        assert (await job_helpers.finish(database, FIELD))['state'] == 'succeeded'

    registry_database(scenario)


@needs_postgres
def test_a_page_past_the_samples_with_a_value_counts_them_once(registry_database, monkeypatch):
    """A page of samples with a value needs no count; one past them all counts those, once, to know where the rest
    start (a page that holds the last of them starts the rest right after, uncounted)."""

    async def scenario(database):
        await database.execute(
            "INSERT INTO dataset_fields (dataset_id, field_name, field_type, is_promoted) "
            "VALUES (1, 'speed', 'number', TRUE)",
        )
        await database.execute(
            """INSERT INTO samples (dataset_id, filepath, media_type, metadata)
               SELECT 1, n::text, 'image', CASE WHEN n % 5 = 0 THEN '{}'::jsonb
                   ELSE jsonb_build_object('speed', n) END
               FROM generate_series(1, 100) n""",
        )
        await database.execute("CREATE INDEX idx_speed ON samples (((metadata->>'speed')::float)) WHERE dataset_id = 1")
        counts = []
        fetchval = database.fetchval

        async def recording(query, *args, **kwargs):
            if 'IS NOT NULL' in query and 'count(*)' in query:
                counts.append(query)
            return await fetchval(query, *args, **kwargs)

        monkeypatch.setattr(database, 'fetchval', recording)
        request = datasets.SamplesRequest(sort_by='speed', sort_order='desc', limit=5, offset=0)
        assert len((await datasets.list_samples_post(1, request))['items']) == 5
        straddling = await datasets.list_samples_post(1, request.model_copy(update={'offset': 78}))
        assert [item['filepath'] for item in straddling['items']] == ['2', '1', '5', '10', '15']
        assert counts == []
        page = await datasets.list_samples_post(1, request.model_copy(update={'offset': 85}))
        assert [item['filepath'] for item in page['items']] == ['30', '35', '40', '45', '50']  # the rest, by id
        assert len(counts) == 1

    registry_database(scenario)
