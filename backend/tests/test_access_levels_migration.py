"""0033 gives every share a level and owner_id stops giving access, so each owner without a share is given one.

A database from the last release before it (2.12.1) holds every kind of access there is: an owner without a share, an
owner with one, a dataset with no owner, a global dataset, private datasets shared and not, a dataset being deleted
(whose owner still sees it until the deletion finishes), and a global dataset being deleted (which gets the everyone
level edit past the write guard that refuses writes to a dataset being deleted, a guard that is on again after).
After the upgrade every user sees exactly the datasets they saw, at the level their role gave them there: an admin
manages every dataset, a power user edits, a guest views. A user who saw a dataset no longer sees is refused (403), as
before. The global datasets give everyone edit, as they did; the private ones hold view, where a dataset made global
starts, so one made global after the upgrade gives view.
"""

import asyncio
import os
import subprocess
from urllib.parse import urlparse

import asyncpg
import pytest
from fastapi import HTTPException

from backend import access
from backend.database import Database, init_connection
from backend.tests import test_upgrade_paths
from backend.tests.test_upgrade_paths import ROOT

database = test_upgrade_paths.database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires disposable PostgreSQL',
)
RELEASE = 'v2.12.1'  # the last release without levels
SEED = """
INSERT INTO users (id, username, password_hash, role) VALUES
    (10, 'owner', 'x', 'power_user'), (11, 'owner-shared', 'x', 'power_user'), (12, 'shared', 'x', 'power_user'),
    (13, 'nobody', 'x', 'power_user'), (14, 'guest-shared', 'x', 'guest'), (15, 'guest', 'x', 'guest'),
    (16, 'guest-owner', 'x', 'guest'), (17, 'admin-two', 'x', 'admin');
SELECT setval(pg_get_serial_sequence('users', 'id'), 17);
INSERT INTO datasets (id, name, owner_id, is_global, followed) VALUES
    (1, 'owned', 10, FALSE, FALSE),
    (2, 'owned-shared', 11, FALSE, FALSE),
    (3, 'no-owner-shared', NULL, FALSE, FALSE),
    (4, 'global', 10, TRUE, TRUE),
    (5, 'unshared', NULL, FALSE, FALSE),
    (6, 'going', 10, FALSE, FALSE),
    (7, 'guest-owned', 16, FALSE, FALSE),
    (8, 'global-going', NULL, TRUE, FALSE);
SELECT setval(pg_get_serial_sequence('datasets', 'id'), 8);
INSERT INTO dataset_access (dataset_id, user_id, granted_by_user_id) VALUES
    (2, 11, 1), (2, 14, 1), (3, 12, 1), (3, 14, 1), (6, 12, 1);
UPDATE datasets SET deleting = TRUE WHERE id IN (6, 8);
"""
# Who saw what before: 2.12.1's access.filter_datasets_for_user, as it was.
OLD_VISIBLE = """
SELECT string_agg(id::text, ',' ORDER BY id) FROM datasets
 WHERE {admin} OR is_global = TRUE OR owner_id = {user}
    OR id IN (SELECT dataset_id FROM dataset_access WHERE user_id = {user})
"""
USERS = {
    10: 'power_user',
    11: 'power_user',
    12: 'power_user',
    13: 'power_user',
    14: 'guest',
    15: 'guest',
    16: 'guest',
    1: 'admin',
    17: 'admin',
}
# What each role could do in the datasets it saw: an admin everything, a power user write, a guest read.
LEVEL_OF_ROLE = {'admin': 'manage', 'power_user': 'edit', 'guest': 'view'}


def _old_schema():
    return subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{RELEASE}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_0033_keeps_what_every_user_sees_and_can_do(database, monkeypatch):
    make, psql, _, migrate = database
    name = make(_old_schema() + SEED)
    before = {
        user: psql(name, OLD_VISIBLE.format(admin='TRUE' if role == 'admin' else 'FALSE', user=user))
        for user, role in USERS.items()
    }
    # The dataset being deleted and the one a guest owns are seen by their owners only through owner_id.
    assert before[10] == '1,4,6,8' and before[16] == '4,7,8'
    changes = psql(name, 'SELECT count(*) FROM dataset_changes')

    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'

    # Every share is edit, the owners without one got one (the dataset being deleted too, written past its guard),
    # granted by nobody; a dataset with no owner got none.
    shares = (
        "SELECT string_agg(format('%s/%s/%s/%s', dataset_id, user_id, level, coalesce(granted_by_user_id::text, '-')), "
        "',' ORDER BY dataset_id, user_id) FROM dataset_access"
    )
    assert psql(name, shares) == '1/10/3/-,2/11/3/1,2/14/3/1,3/12/3/1,3/14/3/1,4/10/3/-,6/10/3/-,6/12/3/1,7/16/3/-'
    levels = "SELECT string_agg(format('%s/%s', id, everyone_level), ',' ORDER BY id) FROM datasets"
    assert psql(name, levels) == '1/1,2/1,3/1,4/3,5/1,6/1,7/1,8/3'  # the global dataset being deleted too
    # The write guard on datasets, which refuses writes to a dataset being deleted, is enabled again.
    guard = "SELECT tgenabled FROM pg_trigger WHERE tgname = 'dataset_write_guard' AND tgrelid = 'datasets'::regclass"
    assert psql(name, guard) == 'O'
    assert psql(name, 'SELECT count(*) FROM groups') == psql(name, 'SELECT count(*) FROM dataset_group_access') == '0'
    assert psql(name, 'SELECT count(*) FROM dataset_changes') == changes  # the followed dataset logged nothing

    async def after():
        dsn = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()
        upgraded = Database()
        upgraded.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, init=init_connection)
        monkeypatch.setattr(access, 'db', upgraded)
        try:
            # The guard is back on: a share written to the dataset being deleted is refused.
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                await upgraded.execute('INSERT INTO dataset_access (dataset_id, user_id) VALUES (6, 13)')
            # And a write to the global dataset being deleted, as the app's writes are refused (a 409 there).
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match='being deleted'):
                await upgraded.execute("UPDATE datasets SET description = 'late' WHERE id = 8")
            assert await upgraded.fetchval('SELECT description FROM datasets WHERE id = 8') is None
            seen: dict[int, str] = {}
            levels: dict[tuple[int | str, int], int | str] = {}  # a level's name, or the refusal's status
            for user_id, role in USERS.items():
                user = {'id': user_id, 'role': role}
                where, args = await access.filter_datasets_for_user(user)
                rows = await upgraded.fetch(f'SELECT id FROM datasets WHERE {where} ORDER BY id', *args)
                seen[user_id] = ','.join(str(row['id']) for row in rows)
                for dataset_id in range(1, 9):
                    try:
                        await access.check_dataset_access(dataset_id, user)
                    except HTTPException as error:
                        levels[user_id, dataset_id] = error.status_code
                        continue
                    row = await upgraded.fetchrow('SELECT * FROM datasets WHERE id = $1', dataset_id)
                    found = await access.effective_level(row, user)
                    assert found is not None
                    levels[user_id, dataset_id] = access.level_name(found)
            # Made global after the upgrade, a dataset that was private gives a user with no share view; the one
            # global before still gives edit.
            await upgraded.execute('UPDATE datasets SET is_global = TRUE WHERE id = 5')
            nobody = {'id': 13, 'role': 'power_user'}
            for dataset_id in (5, 4):
                row = await access.check_dataset_access(dataset_id, nobody)
                found = await access.effective_level(row, nobody)
                assert found is not None
                levels['made global after', dataset_id] = access.level_name(found)
            return seen, levels
        finally:
            await upgraded.pool.close()

    seen, levels = asyncio.run(after())
    assert (levels['made global after', 5], levels['made global after', 4]) == ('view', 'edit')
    assert seen == before
    for user_id, role in USERS.items():
        visible = before[user_id].split(',')
        for dataset_id in range(1, 9):
            expected = LEVEL_OF_ROLE[role] if str(dataset_id) in visible else 403
            assert levels[user_id, dataset_id] == expected, (user_id, dataset_id)
