"""A dataset keeps its shares, with users and with groups, until its deletion finishes.

Written from the rule, not from the deletion's code, and checked where it is met: the share rows, and the HTTP API
signed in with real API tokens (test_access_rule_spec's world). The deletion job is driven one unit at a time (the claim
and step of test_dataset_deletion) and the rule is checked after every unit, so the tests don't depend on which unit
removes what. The rule:

1. At every point of a deletion that has not finished, every dataset_access and dataset_group_access row the dataset had
   when its deletion started is still there, with its level: whatever step it is stopped at, after the dataset's own
   tables are gone while samples remain, and when only the dataset's row is left to remove.
2. So, for a stopped deletion (cancelled, failed, or not finished yet): the manager who started it, not an admin, still
   sees the dataset in the dataset list, deleting, at manage, gets its deletion state (its job) and can resume it; a
   user with a lower level still sees it at that level and is refused the resume with the manage message; a user with
   no share gets 403; GET /groups lists it for each group it is shared with, at the group's level, deleting.
3. Writes stay refused on a deleting dataset, changes to its shares included (granting, changing a level, revoking: 409
   as for any write), so the shares a stopped deletion keeps are exactly the ones it started with.
4. When the deletion finishes nothing of the dataset is left: no shares, no rows in any of its tables; it is gone from
   the dataset list, from every group's list, and GET /datasets/{id} answers 404.
5. A deletion part-way through under the order before this rule (its shares already removed, its saved position
   anywhere in that order) still finishes after the change and leaves nothing behind.
"""

import os
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

import asyncpg
import httpx
import pytest

from backend import dataset_deletion, job_queue
from backend.routers import datasets
from backend.tests import test_access_rule_spec, test_field_registry_postgres
from backend.tests.test_access_rule_spec import ADMIN, GUEST, OTHER_POWER, POWER, World
from backend.tests.test_dataset_deletion import claim, step

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

SEALED = 'Dataset is being deleted; writes are disabled. Resume deletion to finish.'
DENIED = 'Access denied for this dataset'
NEEDS_MANAGE = 'This needs manage access to the dataset'
ADMIN_USER = {'id': ADMIN, 'role': 'admin', 'username': f'user-{ADMIN}'}
SHARE_TABLES = ('dataset_access', 'dataset_group_access')
# The dataset deleted (registry_database's 'training'), and one beside it whose rows all stay.
DATASET, OTHER = 1, 2
# Power users: one in the readers group, one in the editors group, one with no share at all.
READER, EDITOR, NOBODY = 30, 31, 32
# The dataset's shares with users and with groups. The guest's share says edit; a guest is at view whatever it says.
USER_SHARES = {POWER: 'manage', OTHER_POWER: 'tag', GUEST: 'edit'}
GROUP_SHARES = {'readers': 'view', 'editors': 'edit', 'leads': 'manage'}
# The level each user below manage has in the dataset through those shares.
LOWER = {OTHER_POWER: 'tag', GUEST: 'view', READER: 'view', EDITOR: 'edit'}
# Small batches: many units, and the samples removed a few at a time.
BATCH = 2

Shares = tuple[list[tuple[int, int]], list[tuple[int, int]]]
Removal = tuple[str, tuple[str, ...], list[dict[str, Any]]]


def detail(response: httpx.Response) -> Any:
    try:
        return response.json().get('detail')
    except (ValueError, AttributeError):
        return None


def said(response: httpx.Response) -> str:
    return f'{response.status_code} {response.text[:200]}'


def refusal(kind: str, deleting: list[tuple[str, int]]) -> str:
    """The 409 for deleting a user or a group ('user', 'group') with a share in the datasets being deleted, given as
    (name, id) in the order they are named: by name, then id."""
    named = ', '.join(f"'{name}' (id {dataset_id})" for name, dataset_id in deleting)
    if len(deleting) == 1:
        text = (
            f"This {kind} has a share in a dataset being deleted, {named}. Resume or finish that dataset's deletion "
            f'before deleting the {kind}.'
        )
    else:
        text = (
            f'This {kind} has a share in datasets being deleted: {named}. Resume or finish their deletions before '
            f'deleting the {kind}.'
        )
    return text + (' You can deactivate the account in the meantime.' if kind == 'user' else '')


async def prepare(w: World) -> dict[str, int]:
    """The users and the groups the shares name, and the groups' ids by name ('outsiders' is shared with nothing)."""
    for user_id in (READER, EDITOR, NOBODY):
        await w.user(user_id, 'power_user')
    return {
        'readers': await w.group('readers', READER),
        'editors': await w.group('editors', EDITOR),
        'leads': await w.group('leads'),
        'outsiders': await w.group('outsiders'),
    }


async def fill(w: World, dataset_id: int, count: int = 5) -> None:
    """A few rows in each kind of the dataset's tables: samples (with their fields, statistics and tags), a vector field
    with its map (coordinates, ranks), hashes, duplicates, a concept with its scores and queues, a version with its
    snapshots, a saved view, and a followed dataset's change log."""
    samples = [
        {
            'filepath': f'deleted-{dataset_id}-{index}.jpg',
            'tags': ['seen'],
            'metadata': {'label': ('cat', 'dog')[index % 2], 'score': index},
        }
        for index in range(count)
    ]
    await datasets.add_samples(dataset_id, {'samples': samples}, user=ADMIN_USER)
    for statement in (
        "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
        "SELECT dataset_id, id, 'clip', '[1,0]'::vector FROM samples WHERE dataset_id = $1",
        "INSERT INTO sample_hashes (sample_id, field_name, hash_hex) "
        "SELECT id, 'hash', 'abcd' FROM samples WHERE dataset_id = $1",
        "INSERT INTO embedding_coordinates (sample_id, field_name, x, y) "
        "SELECT sample_id, field_name, 1, 2 FROM embeddings WHERE dataset_id = $1",
        "SELECT recompute_viz_rank($1, 'clip')",
        "INSERT INTO duplicate_configs (dataset_id, config) VALUES ($1, 'hash')",
        "INSERT INTO duplicate_members (dataset_id, config, sample_id, key) "
        "SELECT dataset_id, 'hash', id, 'abcd' FROM samples WHERE dataset_id = $1",
        'INSERT INTO duplicate_pending (dataset_id, block_id) VALUES ($1, -1) ON CONFLICT DO NOTHING',
        'INSERT INTO sample_block_changes (dataset_id, transaction_id, block_id) '
        'SELECT $1, n, 0 FROM generate_series(1, 3) n',
        'INSERT INTO vector_changes (dataset_id, sample_id, kind, field_name, transaction_id) '
        "SELECT dataset_id, id, 'vector', 'clip', id FROM samples WHERE dataset_id = $1",
        'INSERT INTO dataset_changes (dataset_id, kind, transaction_id) '
        "SELECT $1, 'fields', n FROM generate_series(1, 3) n",
        'INSERT INTO map_block_changes (dataset_id, transaction_id, field_name, block_id) '
        "SELECT $1, n, 'clip', 0 FROM generate_series(1, 3) n",
    ):
        await w.database.execute(statement, dataset_id)
    await w.database.execute(
        "INSERT INTO views (dataset_id, name, slug) VALUES ($1, 'saved', $2)",
        dataset_id,
        f'saved-{dataset_id}',
    )
    concept = await w.database.fetchval(
        'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) '
        "VALUES ($1, 'concept', 'clip', 'seed', '[1,0]') RETURNING id",
        dataset_id,
    )
    await w.database.execute('SELECT score_concept($1)', concept)
    await w.database.execute('INSERT INTO concept_score_pending (concept_id, block_id) VALUES ($1, 0)', concept)
    await w.database.execute(
        'INSERT INTO concept_score_count_deltas (concept_id, delta) VALUES ($1, 3) ON CONFLICT DO NOTHING',
        concept,
    )
    version = await w.database.fetchval(
        "INSERT INTO dataset_versions (dataset_id, name, sample_count) VALUES ($1, 'saved', $2) RETURNING id",
        dataset_id,
        count,
    )
    await w.database.execute(
        "INSERT INTO version_snapshots (version_id, sample_id, metadata_hash) "
        "SELECT $1, id, 'hash' FROM samples WHERE dataset_id = $2",
        version,
        dataset_id,
    )


async def share(w: World, dataset_id: int) -> None:
    """USER_SHARES and GROUP_SHARES in the dataset."""
    for user_id, level in USER_SHARES.items():
        await w.share(dataset_id, user_id, level)
    for name, level in GROUP_SHARES.items():
        await w.share_with_group(dataset_id, name, level)


async def shares(w: World, dataset_id: int) -> Shares:
    """(user_id, level) of each of the dataset's shares with a user, and (group_id, level) of each with a group."""
    users = await w.database.fetch(
        'SELECT user_id, level FROM dataset_access WHERE dataset_id = $1 ORDER BY user_id',
        dataset_id,
    )
    groups = await w.database.fetch(
        'SELECT group_id, level FROM dataset_group_access WHERE dataset_id = $1 ORDER BY group_id',
        dataset_id,
    )
    return [(row['user_id'], row['level']) for row in users], [(row['group_id'], row['level']) for row in groups]


@dataclass(frozen=True)
class Rows:
    """The ids of the dataset's samples, concepts and versions, read before its deletion."""

    samples: list[int]
    concepts: list[int]
    versions: list[int]


async def rows_of(w: World, dataset_id: int) -> Rows:
    async def ids(table: str) -> list[int]:
        return [
            row['id'] for row in await w.database.fetch(f'SELECT id FROM {table} WHERE dataset_id = $1', dataset_id)
        ]

    return Rows(await ids('samples'), await ids('embedding_concepts'), await ids('dataset_versions'))


async def sample_count(w: World, dataset_id: int) -> int:
    return await w.database.fetchval('SELECT count(*) FROM samples WHERE dataset_id = $1', dataset_id)


async def leftovers(w: World, dataset_id: int, rows: Rows) -> dict[str, int]:
    """Every row left of the dataset, by table and column: in each table of the test's schema with a column that names a
    dataset, a sample, a concept or a version, the rows naming the dataset or one of its own. Its jobs aside: a job's
    history outlives its dataset."""
    columns = await w.database.fetch(
        """SELECT c.table_name, c.column_name FROM information_schema.columns c
           JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
           WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE'
             AND c.column_name IN ('dataset_id', 'sample_id', 'concept_id', 'version_id')
           ORDER BY 1, 2""",
    )
    ids = {
        'dataset_id': [dataset_id],
        'sample_id': rows.samples,
        'concept_id': rows.concepts,
        'version_id': rows.versions,
    }
    found: dict[str, int] = {}
    for table, column in columns:
        if table == 'jobs':
            continue
        left = await w.database.fetchval(f'SELECT count(*) FROM {table} WHERE {column} = ANY($1::int[])', ids[column])
        if left:
            found[f'{table}.{column}'] = left
    if await w.database.fetchval('SELECT count(*) FROM datasets WHERE id = $1', dataset_id):
        found['datasets.id'] = 1
    return found


async def listed(w: World, user_id: int, dataset_id: int) -> Optional[dict]:
    """The dataset as GET /datasets lists it for the user; None: not listed."""
    response = await w.call(user_id, 'GET', '/api/datasets')
    assert response.status_code == 200, response.text
    found = [row for row in response.json() if row['id'] == dataset_id]
    assert len(found) <= 1, found
    return found[0] if found else None


async def group_lists(w: World, dataset_id: int) -> dict[int, list[dict]]:
    """Each group's entries for the dataset on the Groups page (GET /groups, an admin), by group id."""
    response = await w.call(ADMIN, 'GET', '/api/groups')
    assert response.status_code == 200, response.text
    return {
        group['id']: [entry for entry in group['datasets'] if entry['id'] == dataset_id] for group in response.json()
    }


async def gone(w: World, dataset_id: int) -> list[str]:
    """What rule 4 finds wrong over HTTP once the deletion has finished."""
    problems: list[str] = []
    for user_id in (ADMIN, POWER, *LOWER):
        if await listed(w, user_id, dataset_id) is not None:
            problems.append(f'user {user_id} still has it in the dataset list')
    for user_id in (ADMIN, POWER):
        response = await w.call(user_id, 'GET', f'/api/datasets/{dataset_id}')
        if response.status_code != 404:
            problems.append(f'user {user_id}: GET /datasets/{dataset_id} answered {said(response)}')
    for group_id, entries in (await group_lists(w, dataset_id)).items():
        if entries:
            problems.append(f'group {group_id} still lists it: {entries}')
    return problems


async def start(w: World, dataset_id: int) -> str:
    """The deletion started by its manager, a power user (DELETE /datasets/{id}): the dataset sealed; its job's id."""
    response = await w.call(POWER, 'DELETE', f'/api/datasets/{dataset_id}')
    assert response.status_code == 202, response.text
    return response.json()['job_id']


async def claimed(w: World, job_id: str) -> dict:
    """The deletion's job, claimed as a runner claims it."""
    current = await claim(w.database)
    assert current is not None and str(current['id']) == job_id, f'expected to claim job {job_id}, got {current}'
    return current


async def job_state(w: World, job_id: str) -> str:
    return await w.database.fetchval('SELECT state FROM jobs WHERE id = $1', UUID(job_id))


async def stop(w: World, current: dict, how: str) -> dict:
    """The deletion left `how` between two units: cancelled (an admin's POST /jobs/{id}/cancel, acknowledged as the
    runner does), failed (its unit failed for good), queued (the runner's slice ended) or running (still claimed)."""
    pool, job_id, lease = w.database.pool, current['id'], current['lease_token']
    if how == 'cancelled':
        response = await w.call(ADMIN, 'POST', f'/api/jobs/{job_id}/cancel')
        assert response.status_code == 200, response.text
        current = await job_queue.publish_step(pool, job_id, lease, release=True)
    elif how == 'failed':
        current = await job_queue.fail_job(pool, job_id, lease, {'type': 'RuntimeError', 'message': 'stopped here'})
    elif how == 'queued':
        current = await job_queue.publish_step(pool, job_id, lease, release=True)
    assert current['state'] == how, (how, current['state'])
    return current


# --- Rules 1 and 4 ---


def test_every_share_stays_until_the_deletion_finishes_and_then_nothing_is_left(world, monkeypatch):
    """Rule 1 after every unit of the deletion, then rule 4. The other dataset's rows, its shares included, stay."""
    monkeypatch.setattr(dataset_deletion, 'BATCH_SIZE', BATCH)

    async def scenario(w):
        await prepare(w)
        for dataset_id in (DATASET, OTHER):
            await fill(w, dataset_id)
            await share(w, dataset_id)
        started = await shares(w, DATASET)
        assert (len(started[0]), len(started[1])) == (len(USER_SHARES), len(GROUP_SHARES))
        others = await shares(w, OTHER)
        others_samples = await sample_count(w, OTHER)
        rows = await rows_of(w, DATASET)
        job_id = await start(w, DATASET)
        lost: list[str] = []
        if await shares(w, DATASET) != started:
            lost.append(f'sealed: {await shares(w, DATASET)}')
        current = await claimed(w, job_id)
        units, some_samples_left, no_samples_left = 0, False, False
        while True:
            current = await step(w.database, current)
            units += 1
            assert units < 5000, 'the deletion did not finish'
            if current['state'] == 'succeeded':
                break
            assert current['state'] == 'running', current
            now = await shares(w, DATASET)
            if now != started:
                lost.append(f'after unit {units} ({current["message"]!r}): users {now[0]}, groups {now[1]}')
            remaining = await sample_count(w, DATASET)
            some_samples_left |= 0 < remaining < len(rows.samples)
            no_samples_left |= remaining == 0

        # Rule 4.
        assert await leftovers(w, DATASET, rows) == {}
        assert await gone(w, DATASET) == []
        assert await shares(w, OTHER) == others
        assert await sample_count(w, OTHER) == others_samples
        # The deletion went through the points the rule names, unit by unit: samples removed a few at a time, and
        # unfinished with none left.
        assert units >= 20 and some_samples_left and no_samples_left, (units, some_samples_left, no_samples_left)
        # Rule 1.
        assert not lost, (
            f'{len(lost)} of {units} points of the deletion lost shares. At the start: users {started[0]}, groups '
            f'{started[1]}. First: ' + '; '.join(lost[:3])
        )

    world(scenario)


# --- Rule 2 ---

STOPS = ('cancelled', 'failed', 'queued', 'running')


async def answers(w: World, dataset_id: int, name: str, job_id: str, groups: dict[str, int]) -> list[str]:
    """What rule 2 finds wrong with the answers about the stopped deletion. The manager's resume, when allowed, puts a
    cancelled or failed deletion back in the queue."""
    problems: list[str] = []
    resume = f'/api/datasets/{dataset_id}/deletion/resume'
    state = await job_state(w, job_id)

    entry = await listed(w, POWER, dataset_id)
    if entry is None or (entry['deleting'], entry['access_level']) != (True, 'manage'):
        problems.append(f'manager: the dataset list gives {entry}')
    opened = await w.call(POWER, 'GET', f'/api/datasets/{dataset_id}')
    if opened.status_code != 200 or (opened.json()['deleting'], opened.json()['access_level']) != (True, 'manage'):
        problems.append(f'manager: GET /datasets/{dataset_id} answered {said(opened)}')
    job = await w.call(POWER, 'GET', f'/api/jobs/{job_id}')
    if job.status_code != 200 or job.json()['state'] != state:
        problems.append(f'manager: the deletion job ({state}) answered {said(job)}')
    resumed = await w.call(POWER, 'POST', resume)
    if resumed.status_code != 202 or resumed.json().get('job_id') != job_id:
        problems.append(f'manager: resume answered {said(resumed)}')

    for user_id, level in LOWER.items():
        entry = await listed(w, user_id, dataset_id)
        if entry is None or (entry['deleting'], entry['access_level']) != (True, level):
            problems.append(f'user {user_id} (at {level}): the dataset list gives {entry}')
        refused = await w.call(user_id, 'POST', resume)
        if (refused.status_code, detail(refused)) != (403, NEEDS_MANAGE):
            problems.append(f'user {user_id} (at {level}): resume answered {said(refused)}')

    opened = await w.call(NOBODY, 'GET', f'/api/datasets/{dataset_id}')
    if (opened.status_code, detail(opened)) != (403, DENIED):
        problems.append(f'no share: GET /datasets/{dataset_id} answered {said(opened)}')
    if await listed(w, NOBODY, dataset_id) is not None:
        problems.append('no share: the dataset is in their list')
    refused = await w.call(NOBODY, 'POST', resume)
    if (refused.status_code, detail(refused)) != (403, DENIED):
        problems.append(f'no share: resume answered {said(refused)}')

    lists = await group_lists(w, dataset_id)
    for group, level in GROUP_SHARES.items():
        wanted = [{'id': dataset_id, 'name': name, 'level': level, 'deleting': True}]
        if lists[groups[group]] != wanted:
            problems.append(f'Groups page, {group}: {lists[groups[group]]}')
    if lists[groups['outsiders']]:
        problems.append(f'Groups page, outsiders: {lists[groups["outsiders"]]}')
    return problems


def test_a_stopped_deletion_still_answers_each_user_by_the_shares_it_started_with(world, monkeypatch):
    """Rule 2 before the first unit and after every unit, the deletion left there cancelled, failed, queued or running
    in turn, and resumed by its manager."""
    monkeypatch.setattr(dataset_deletion, 'BATCH_SIZE', BATCH)

    async def scenario(w):
        groups = await prepare(w)
        await fill(w, DATASET)
        await share(w, DATASET)
        name = await w.database.fetchval('SELECT name FROM datasets WHERE id = $1', DATASET)
        job_id = await start(w, DATASET)
        problems: list[str] = []
        current: Optional[dict] = None  # not claimed yet: queued
        unit = 0
        while True:
            how = 'queued' if current is None else STOPS[unit % len(STOPS)]
            if current is not None:
                current = await stop(w, current, how)
            problems += [
                f'after unit {unit}, {how}: {problem}' for problem in await answers(w, DATASET, name, job_id, groups)
            ]
            if await job_state(w, job_id) in ('cancelled', 'failed'):  # the manager's resume was refused
                assert (await w.call(ADMIN, 'POST', f'/api/datasets/{DATASET}/deletion/resume')).status_code == 202
            if current is None or current['state'] != 'running':
                current = await claimed(w, job_id)
            current = await step(w.database, current)
            unit += 1
            assert unit < 5000, 'the deletion did not finish'
            if current['state'] == 'succeeded':
                break
            assert current['state'] == 'running', current
        assert unit >= 20, unit
        assert not problems, f'{len(problems)} wrong answers over {unit} units. First:\n' + '\n'.join(problems[:12])

    world(scenario)


# --- Rule 3 ---


@pytest.mark.parametrize('where', ['sealed', 'part-way'])
def test_the_shares_of_a_dataset_being_deleted_cannot_be_changed(world, monkeypatch, where):
    """Granting, changing a level and revoking, a user's share or a group's, through the API (an admin, and the manager
    who started the deletion) or in the database: refused, and the shares stay the ones the deletion started with. So
    is removing a user or a group the dataset is shared with, which would remove its share. Sealed: before the first
    unit. Part-way: some of its samples removed, others not, and the deletion cancelled."""
    monkeypatch.setattr(dataset_deletion, 'BATCH_SIZE', BATCH)

    async def scenario(w):
        groups = await prepare(w)
        await fill(w, DATASET)
        await share(w, DATASET)
        started = await shares(w, DATASET)
        total = await sample_count(w, DATASET)
        job_id = await start(w, DATASET)
        if where == 'part-way':
            current = await claimed(w, job_id)
            while not 0 < await sample_count(w, DATASET) < total:
                current = await step(w.database, current)
                assert current['state'] == 'running', current
            await stop(w, current, 'cancelled')
        problems: list[str] = []
        access = f'/api/datasets/{DATASET}/access'
        changes: list[tuple[str, str, Any]] = [
            ('POST', access, {'user_id': NOBODY, 'level': 'view'}),  # a new share
            ('POST', access, {'user_id': OTHER_POWER, 'level': 'manage'}),  # a level changed
            ('POST', access, {'user_id': OTHER_POWER}),  # granted again, keeping its level
            ('POST', access, {'group': 'outsiders', 'level': 'view'}),  # a new group share
            ('POST', access, {'group': 'readers', 'level': 'edit'}),  # a group's level changed
            ('DELETE', f'{access}/{OTHER_POWER}', None),  # revoked
            ('DELETE', f'{access}/{POWER}', None),  # the manager's own, revoked
            ('DELETE', f'{access}/groups/{groups["readers"]}', None),  # a group's, revoked
        ]
        for caller in (ADMIN, POWER):
            for method, path, body in changes:
                response = await w.call(caller, method, path, **({} if body is None else {'json': body}))
                if (response.status_code, detail(response)) != (409, SEALED):
                    problems.append(f'user {caller}: {method} {path} {body} answered {said(response)}')
        name = await w.database.fetchval('SELECT name FROM datasets WHERE id = $1', DATASET)
        for kind, path in (('user', f'/api/users/{OTHER_POWER}'), ('group', f'/api/groups/{groups["readers"]}')):
            response = await w.call(ADMIN, 'DELETE', path)
            if (response.status_code, detail(response)) != (409, refusal(kind, [(name, DATASET)])):
                problems.append(f'DELETE {path} answered {said(response)}')
        for sql, args in (
            ('INSERT INTO dataset_access (dataset_id, user_id, level) VALUES ($1, $2, 1)', (NOBODY,)),
            ('UPDATE dataset_access SET level = 4 WHERE dataset_id = $1 AND user_id = $2', (OTHER_POWER,)),
            ('DELETE FROM dataset_access WHERE dataset_id = $1 AND user_id = $2', (OTHER_POWER,)),
            (
                'INSERT INTO dataset_group_access (dataset_id, group_id, level) VALUES ($1, $2, 1)',
                (groups['outsiders'],),
            ),
            ('UPDATE dataset_group_access SET level = 4 WHERE dataset_id = $1 AND group_id = $2', (groups['readers'],)),
            ('DELETE FROM dataset_group_access WHERE dataset_id = $1 AND group_id = $2', (groups['readers'],)),
        ):
            try:
                await w.database.execute(sql, DATASET, *args)
                problems.append(f'the database took: {sql}')
            except asyncpg.ObjectNotInPrerequisiteStateError as error:
                if 'being deleted' not in str(error):
                    problems.append(f'{sql}: {error}')
        now = await shares(w, DATASET)
        if now != started:
            problems.append(
                f'the shares were users {started[0]}, groups {started[1]}; now users {now[0]}, groups {now[1]}',
            )
        assert not problems, '\n'.join(problems)

    world(scenario)


# --- Deleting a user or a group with a share in a dataset being deleted ---

# Power users: one whose shares are in datasets being deleted, one whose shares are elsewhere, one who only owns one.
LEAVER, STAYER, OWNER = 40, 41, 42


async def dataset_named(w: World, dataset_id: int, name: str, owner: Optional[int] = None) -> None:
    await w.database.execute(
        'INSERT INTO datasets (id, name, is_global, owner_id) VALUES ($1, $2, FALSE, $3)',
        dataset_id,
        name,
        owner,
    )


async def seal(w: World, dataset_id: int) -> None:
    """The dataset's deletion started by an admin, and not finished."""
    response = await w.call(ADMIN, 'DELETE', f'/api/datasets/{dataset_id}')
    assert response.status_code == 202, response.text


async def finish_every_deletion(w: World) -> None:
    """Every deletion run to its end, a unit at a time."""
    while (current := await claim(w.database)) is not None:
        while current['state'] != 'succeeded':
            current = await step(w.database, current)
            assert current['state'] in ('running', 'succeeded'), current


async def user_standing(w: World, user_id: int) -> list[list[tuple]]:
    """What deleting the user would remove: the user, their shares and their views."""
    queries = (
        'SELECT id, username, is_active FROM users WHERE id = $1',
        'SELECT dataset_id, level FROM dataset_access WHERE user_id = $1 ORDER BY 1',
        'SELECT id FROM views WHERE user_id = $1 ORDER BY 1',
    )
    return [[tuple(row) for row in await w.database.fetch(query, user_id)] for query in queries]


async def group_standing(w: World, group_id: int) -> list[list[tuple]]:
    """What deleting the group would remove: the group, its members and its shares."""
    queries = (
        'SELECT id, name FROM groups WHERE id = $1',
        'SELECT user_id FROM group_members WHERE group_id = $1 ORDER BY 1',
        'SELECT dataset_id, level FROM dataset_group_access WHERE group_id = $1 ORDER BY 1',
    )
    return [[tuple(row) for row in await w.database.fetch(query, group_id)] for query in queries]


def test_deleting_a_user_or_group_with_a_share_in_a_dataset_being_deleted_is_refused_naming_each_one(world):
    """409, naming the dataset being deleted, or the datasets by name then id, and saying what to do; nothing is
    removed, the user's private views included. Their share in a dataset not being deleted isn't named. Once the
    deletions have finished, both delete as before."""

    async def scenario(w):
        await w.user(LEAVER, 'power_user')
        group = await w.group('departing', LEAVER)
        await dataset_named(w, 300, 'beta')
        await dataset_named(w, 303, 'kept')
        for dataset_id in (300, 303):
            await w.share(dataset_id, LEAVER, 'edit')
            await w.share_with_group(dataset_id, 'departing', 'view')
        await w.database.execute(
            "INSERT INTO views (dataset_id, user_id, name, slug) VALUES (303, $1, 'private', 'leaver-private')",
            LEAVER,
        )
        await seal(w, 300)

        async def refused(deleting: list[tuple[str, int]]) -> None:
            user, groups = await user_standing(w, LEAVER), await group_standing(w, group)
            response = await w.call(ADMIN, 'DELETE', f'/api/users/{LEAVER}')
            assert (response.status_code, detail(response)) == (409, refusal('user', deleting)), response.text
            response = await w.call(ADMIN, 'DELETE', f'/api/groups/{group}')
            assert (response.status_code, detail(response)) == (409, refusal('group', deleting)), response.text
            assert await user_standing(w, LEAVER) == user
            assert await group_standing(w, group) == groups
            assert (await w.call(LEAVER, 'GET', '/api/datasets')).status_code == 200  # still signed in

        await refused([('beta', 300)])
        # Two more, one taking the name of the one being deleted: named by name, then id.
        await dataset_named(w, 301, 'beta')
        await dataset_named(w, 302, 'alpha')
        for dataset_id in (301, 302):
            await w.share(dataset_id, LEAVER, 'view')
            await w.share_with_group(dataset_id, 'departing', 'edit')
            await seal(w, dataset_id)
        await refused([('alpha', 302), ('beta', 300), ('beta', 301)])

        await finish_every_deletion(w)
        response = await w.call(ADMIN, 'DELETE', f'/api/users/{LEAVER}')
        assert (response.status_code, response.json()) == (200, {'message': 'User deleted successfully'}), response.text
        response = await w.call(ADMIN, 'DELETE', f'/api/groups/{group}')
        assert response.status_code == 204, response.text
        assert await user_standing(w, LEAVER) == [[], [], []]
        assert await group_standing(w, group) == [[], [], []]

    world(scenario)


def test_a_user_or_group_whose_shares_are_all_in_datasets_not_being_deleted_deletes_as_before(world):
    """While another dataset is being deleted, shared with other users and groups: deleting this user and this group
    removes them and their shares, and leaves the dataset being deleted with its shares."""

    async def scenario(w):
        await w.user(STAYER, 'power_user')
        group = await w.group('staying', STAYER)
        await w.group('others', OTHER_POWER)
        await dataset_named(w, 300, 'going')
        await dataset_named(w, 303, 'kept')
        await w.share(303, STAYER, 'edit')
        await w.share_with_group(303, 'staying', 'view')
        await w.share(300, OTHER_POWER, 'edit')
        await w.share_with_group(300, 'others', 'view')
        await seal(w, 300)
        going = await shares(w, 300)
        response = await w.call(ADMIN, 'DELETE', f'/api/users/{STAYER}')
        assert (response.status_code, response.json()) == (200, {'message': 'User deleted successfully'}), response.text
        response = await w.call(ADMIN, 'DELETE', f'/api/groups/{group}')
        assert response.status_code == 204, response.text
        assert await user_standing(w, STAYER) == [[], [], []]
        assert await group_standing(w, group) == [[], [], []]
        assert await shares(w, 300) == going

    world(scenario)


def test_a_user_who_only_owns_a_dataset_being_deleted_is_still_refused(world):
    """No share, only its owner: still a 409 (its words are the older message's), nothing removed; deleted once the
    dataset's deletion has finished."""

    async def scenario(w):
        await w.user(OWNER, 'power_user')
        await dataset_named(w, 300, 'owned', OWNER)
        await seal(w, 300)
        before = await user_standing(w, OWNER)
        response = await w.call(ADMIN, 'DELETE', f'/api/users/{OWNER}')
        assert response.status_code == 409, response.text
        assert await user_standing(w, OWNER) == before
        await finish_every_deletion(w)
        response = await w.call(ADMIN, 'DELETE', f'/api/users/{OWNER}')
        assert response.status_code == 200, response.text

    world(scenario)


# --- Rule 5 ---


def released_order() -> list[tuple[str, tuple[str, ...]]]:
    """DATASET_TABLES as the release before this rule ran them, read from the module's own list: its tables in their
    order, the two share tables right after views, where that release removed them (before any sample). Only their place
    is the release's: a table added to or taken from the list since is in or out of this one too."""
    kept: list[tuple[str, tuple[str, ...]]] = [e for e in dataset_deletion.DATASET_TABLES if e[0] not in SHARE_TABLES]
    after = [table for table, _ in kept].index('views') + 1
    moved: list[tuple[str, tuple[str, ...]]] = [
        ('dataset_access', ('dataset_id', 'user_id')),
        ('dataset_group_access', ('dataset_id', 'group_id')),
    ]
    return kept[:after] + moved + kept[after:]


@dataclass(frozen=True)
class Stopped:
    """Where a deletion under the released order had got to. phase: concepts, versions, tables, samples or indexes;
    table (in tables): the index into released_order() it had reached; part: part of that table's rows removed (in
    concepts and versions: of the first one's children); batch (in samples): how far its first batch of samples had got,
    0 not begun, 1 begun, 2 done."""

    label: str
    phase: str
    table: int = 0
    part: bool = False
    batch: int = 0


def released_positions() -> list[Stopped]:
    names = [table for table, _ in released_order()]
    return [
        Stopped("concepts: part of the first concept's scores removed", 'concepts', part=True),
        Stopped("versions: part of the first version's snapshots removed", 'versions', part=True),
        *(Stopped(f'tables: at {name}', 'tables', table=index) for index, name in enumerate(names)),
        Stopped('tables: part of dataset_access removed', 'tables', table=names.index('dataset_access'), part=True),
        Stopped(
            'tables: part of dataset_group_access removed',
            'tables',
            table=names.index('dataset_group_access'),
            part=True,
        ),
        Stopped('tables: every one done', 'tables', table=len(names)),
        Stopped('samples: before the first batch', 'samples'),
        Stopped('samples: the first batch begun', 'samples', batch=1),
        Stopped('samples: the first batch done', 'samples', batch=2),
        Stopped('indexes: every sample removed', 'indexes'),
    ]


async def measure(
    w: World,
    table: str,
    keys: tuple[str, ...],
    where: str,
    args: list[Any],
    limit: Optional[int] = None,
) -> list[asyncpg.Record]:
    """The rows as the released job measured them, in key order: each one's key columns (its cursor) and its size."""
    key = ','.join(f"'{name}',t.{name}" for name in keys)
    order = ','.join(f't.{name}' for name in keys)
    query = (
        f'SELECT jsonb_build_object({key}) AS key, pg_column_size(t)::bigint AS bytes FROM {table} t '
        f'WHERE {where} ORDER BY {order}'
    )
    return await w.database.fetch(query + ('' if limit is None else f' LIMIT {int(limit)}'), *args)


class Released:
    """The released job's progress as it saved it: the rows it had removed, each table's cursor (the last key it
    removed there, and the parent it was removing for), and its counters."""

    def __init__(self) -> None:
        self.batch = dataset_deletion.BATCH_SIZE
        self.checkpoint: dict[str, Any] = {'batch_size': self.batch}
        self.cursors: dict[str, dict[str, Any]] = {}
        self.removals: list[Removal] = []

    def remove(self, table: str, keys: tuple[str, ...], rows: list[asyncpg.Record], scope: Any = None) -> None:
        """`rows` of `table` removed, in units of the batch size."""
        if not rows:
            return
        self.removals.append((table, keys, [row['key'] for row in rows]))
        self.cursors[table] = {'scope': scope, 'key': rows[-1]['key']}
        last = rows[(len(rows) - 1) // self.batch * self.batch :]
        self.checkpoint['deleted_rows'] = self.checkpoint.get('deleted_rows', 0) + len(rows)
        self.checkpoint['last_unit_rows'] = len(last)
        self.checkpoint['last_unit_bytes'] = sum(row['bytes'] for row in last)

    def saved(self, **position: Any) -> tuple[dict[str, Any], list[Removal]]:
        checkpoint = {**self.checkpoint, **position}
        if self.cursors:
            checkpoint['cursors'] = self.cursors
        return checkpoint, self.removals


async def released_state(w: World, dataset_id: int, at: Stopped) -> tuple[dict[str, Any], list[Removal]]:
    """The checkpoint the released job saved when it had got to `at`, and the rows it had removed by then. Its order:
    each concept's children then the concept, each version's snapshots then the version (by name); then the tables of
    released_order(); then the samples a batch at a time, each batch's rows in the sample tables first. It named no
    phase while removing concepts, and no table index before its first table was done."""
    released = Released()
    parents: list[tuple[str, str, str, tuple[tuple[str, tuple[str, ...]], ...]]] = [
        ('concepts', 'embedding_concepts', 'concept_id', dataset_deletion.CONCEPT_TABLES),
        ('versions', 'dataset_versions', 'version_id', (('version_snapshots', ('version_id', 'sample_id')),)),
    ]
    for phase, parent_table, column, children in parents:
        if phase != 'concepts':
            released.checkpoint['phase'] = phase
        part = at.phase == phase and at.part
        ordered = await w.database.fetch(
            f'SELECT id FROM {parent_table} WHERE dataset_id = $1 ORDER BY name',
            dataset_id,
        )
        for parent in [row['id'] for row in ordered]:
            for table, keys in children:
                rows = await measure(w, table, keys, f't.{column}=$1', [parent], released.batch if part else None)
                released.remove(table, keys, rows, parent)
                if part and rows:
                    return released.saved()
            released.remove(parent_table, ('id',), await measure(w, parent_table, ('id',), 't.id=$1', [parent]))
        assert not part, f'{at.label}: nothing to stop in'
    released.checkpoint['phase'] = 'tables'
    order = released_order()
    for index, (table, keys) in enumerate(order):
        if at.phase == 'tables' and at.table == index:
            if at.part:
                rows = await measure(w, table, keys, 't.dataset_id=$1', [dataset_id], released.batch)
                assert len(rows) == released.batch, f'{at.label}: {table} has too few rows to stop part-way'
                released.remove(table, keys, rows)
            return released.saved(**({'table': index} if index else {}))
        released.remove(table, keys, await measure(w, table, keys, 't.dataset_id=$1', [dataset_id]))
    released.checkpoint['table'] = len(order)
    if at.phase == 'tables':
        return released.saved()
    released.checkpoint['phase'] = 'samples'
    if at.phase == 'samples' and at.batch == 0:
        return released.saved()
    released.checkpoint['children_done'] = True  # no objects: its first look for them found none
    ordered = await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id', dataset_id)
    samples = [row['id'] for row in ordered]
    for first in range(0, len(samples), released.batch):
        ids = samples[first : first + released.batch]
        begun = at.phase == 'samples' and at.batch == 1
        for table, keys in dataset_deletion.SAMPLE_TABLES:
            rows = await measure(w, table, keys, 't.sample_id=ANY($1::int[])', [ids], released.batch if begun else None)
            released.remove(table, keys, rows)
            if begun and rows:
                return released.saved(sample_ids=ids)
        released.remove('samples', ('id',), await measure(w, 'samples', ('id',), 't.id=ANY($1::int[])', [ids]))
        if at.phase == 'samples':
            return released.saved()
    return released.saved(phase='indexes')


def removing(dataset_id: int, removals: list[Removal]) -> job_queue.Publisher:
    """The publication that removes those rows, in the deletion's own fenced transaction as the released job did."""

    async def publish(connection: asyncpg.Connection) -> None:
        await connection.execute("SELECT set_config('fridge.deletion_dataset', $1, TRUE)", str(dataset_id))
        for table, keys, rows in removals:
            matches = ' AND '.join(f't.{name}=target.{name}' for name in keys)
            targets = f'jsonb_populate_recordset(NULL::{table}, $1::jsonb) target'
            status = await connection.execute(f'DELETE FROM {table} t USING {targets} WHERE {matches}', rows)
            if int(status.split()[-1]) != len(rows):
                raise RuntimeError(f'{table}: {status}, expected {len(rows)}')

    return publish


async def stopped_under_the_release(w: World, dataset_id: int, job_id: str, at: Stopped) -> None:
    """The deletion as the release left it at `at` when the server stopped for the upgrade: its rows removed in the
    release's order up to there and its checkpoint saved, in one fenced publication of its job, which is queued."""
    checkpoint, removals = await released_state(w, dataset_id, at)
    current = await claimed(w, job_id)
    current = await job_queue.publish_step(
        w.database.pool,
        current['id'],
        current['lease_token'],
        checkpoint=checkpoint,
        done=checkpoint.get('deleted_rows', 0),
        release=True,
        publisher=removing(dataset_id, removals),
    )
    assert current['state'] == 'queued', current


def test_a_deletion_stopped_part_way_under_the_released_order_still_finishes_and_leaves_nothing(world, monkeypatch):
    """Rule 5 at every position of the released order (released_positions): the rows it had removed, its shares among
    them from dataset_access on, and the checkpoint it saved there; then the job resumed and run to its end by today's
    code. Each position is a dataset of its own; every failing one is reported."""
    monkeypatch.setattr(dataset_deletion, 'BATCH_SIZE', BATCH)
    names = [table for table, _ in released_order()]
    first_share, last_share = names.index('dataset_access'), names.index('dataset_group_access')

    async def scenario(w):
        await prepare(w)
        failures: list[str] = []
        positions = released_positions()
        for index, at in enumerate(positions):
            dataset_id = 100 + index
            await w.dataset(dataset_id)
            await fill(w, dataset_id)
            await share(w, dataset_id)
            rows = await rows_of(w, dataset_id)
            job_id = await start(w, dataset_id)
            try:
                await stopped_under_the_release(w, dataset_id, job_id, at)
                users, groups = await shares(w, dataset_id)
                past = at.phase in ('samples', 'indexes') or (at.phase == 'tables' and at.table > last_share)
                if past and (users or groups):
                    failures.append(f'{at.label}: the released state kept shares: users {users}, groups {groups}')
                if at.phase == 'tables' and at.table == first_share and at.part:
                    assert len(users) == len(USER_SHARES) - BATCH, users
                current = await claimed(w, job_id)
                units = 0
                while current['state'] != 'succeeded':
                    current = await step(w.database, current)
                    units += 1
                    assert units < 5000, 'the deletion did not finish'
                    assert current['state'] in ('running', 'succeeded'), current
                left = await leftovers(w, dataset_id, rows)
                if left:
                    failures.append(f'{at.label}: finished, leaving {left}')
                failures += [f'{at.label}: {problem}' for problem in await gone(w, dataset_id)]
            except Exception as error:
                failures.append(f'{at.label}: {type(error).__name__}: {error}')
                await job_queue.cancel_job(w.database.pool, UUID(job_id))  # out of the next position's way
        assert len(positions) > len(names)
        assert not failures, f'{len(failures)} of {len(positions)} positions:\n' + '\n'.join(failures)

    world(scenario)
