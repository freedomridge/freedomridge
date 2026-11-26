"""A mirror's private flag (dataset_fields.values_private, 0035) is its own, on any field: the sync keeps it whatever
the primary does with the field, and the mirror guard lets the mirror store it and nothing else of the primary's."""

import os

import asyncpg
import pytest

from backend import follower_sync
from backend.field_registry import field_stats
from backend.tests import test_access_rule_spec
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_local_fields_storage import READ_ONLY, as_sync

registry_database = registry_helpers.registry_database
world = test_access_rule_spec.world
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
ROW = (
    'SELECT field_type, origin, description, unit, values_private FROM dataset_fields '
    'WHERE dataset_id = 2 AND field_name = $1'
)
# What the route inserts for a field with no row of its own, to hold its flag.
FLAG_ROW = (
    """INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin) VALUES (2, $1, 'unknown', 'system')"""
)


async def a_mirror(database) -> None:
    await database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (2, 7, 0)')


async def sync(database, fields: list) -> None:
    await as_sync(database, 2, lambda connection: follower_sync.write_fields(connection, 2, fields))


def score(**settings) -> dict:
    return {'field_name': 'score', 'field_type': 'number', 'origin': 'metadata', **settings}


def test_a_mirror_stores_a_flag_on_a_field_with_no_row_and_nothing_else(registry_database):
    async def scenario(database):
        await a_mirror(database)
        await database.execute(FLAG_ROW, 'filepath')
        await database.execute("UPDATE dataset_fields SET values_private = TRUE WHERE dataset_id = 2")
        assert tuple(await database.fetchrow(ROW, 'filepath')) == ('unknown', 'system', None, None, True)
        # Anything else about the primary's fields stays refused: a description on that row, a row saying more.
        with pytest.raises(asyncpg.PostgresError, match=READ_ONLY):
            await database.execute("UPDATE dataset_fields SET description = 'x' WHERE dataset_id = 2")
        with pytest.raises(asyncpg.PostgresError, match=READ_ONLY):
            await database.execute(
                "INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, description) "
                "VALUES (2, 'id', 'unknown', 'system', 'x')",
            )
        with pytest.raises(asyncpg.PostgresError, match=READ_ONLY):
            await database.execute(
                "INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin) "
                "VALUES (2, 'crop', 'string', 'metadata')",
            )
        with pytest.raises(asyncpg.PostgresError, match=READ_ONLY):
            await database.execute("DELETE FROM dataset_fields WHERE dataset_id = 2 AND field_name = 'filepath'")
        # A flag row says nothing the registry reads: the field isn't listed for having one.
        assert 'filepath' not in {stat['field_name'] for stat in await field_stats(database, 2)}

    registry_database(scenario)


def test_the_sync_never_resets_a_flag_set_on_the_mirror(registry_database):
    async def scenario(database):
        await a_mirror(database)
        await sync(database, [score(), {'field_name': 'crop', 'field_type': 'string', 'origin': 'metadata'}])
        await database.execute(
            "UPDATE dataset_fields SET values_private = TRUE WHERE dataset_id = 2 AND field_name = 'score'",
        )
        await database.execute(FLAG_ROW, 'id')
        await database.execute(
            "UPDATE dataset_fields SET values_private = TRUE WHERE dataset_id = 2 AND field_name = 'id'",
        )

        # The primary describes them: the description arrives, the flag stays.
        described = {'field_type': 'unknown', 'origin': 'system', 'description': 'The number.', 'unit': None}
        await sync(database, [score(description='Points.', unit='pt'), {'field_name': 'id', **described}])
        assert tuple(await database.fetchrow(ROW, 'score')) == ('number', 'metadata', 'Points.', 'pt', True)
        assert tuple(await database.fetchrow(ROW, 'id')) == ('unknown', 'system', 'The number.', None, True)

        # The primary clears its field (or deletes the last sample holding it): an unflagged field goes as before;
        # a flagged one stays as an empty row with its flag, no longer listed.
        await sync(database, [])
        assert tuple(await database.fetchrow(ROW, 'score')) == ('unknown', 'system', None, None, True)
        assert tuple(await database.fetchrow(ROW, 'id')) == ('unknown', 'system', None, None, True)
        assert await database.fetchrow(ROW, 'crop') is None
        assert 'score' not in {stat['field_name'] for stat in await field_stats(database, 2)}
        await sync(database, [])  # nothing left to change: the flag rows stay as they are
        assert (await database.fetchrow(ROW, 'score'))['values_private'] is True

        # It comes back: still private.
        await sync(database, [score(description='Points.')])
        assert tuple(await database.fetchrow(ROW, 'score')) == ('number', 'metadata', 'Points.', None, True)

        # A rename on the primary arrives as one field going and another coming: the flag stays with the old name.
        await sync(database, [{**score(), 'field_name': 'points'}])
        assert tuple(await database.fetchrow(ROW, 'score')) == ('unknown', 'system', None, None, True)
        assert tuple(await database.fetchrow(ROW, 'points')) == ('number', 'metadata', None, None, False)

        # Unflagged here, the empty row goes with the next sync, as any field the primary doesn't have.
        await database.execute(
            "UPDATE dataset_fields SET values_private = FALSE WHERE dataset_id = 2 AND field_name IN ('score', 'id')",
        )
        await sync(database, [{**score(), 'field_name': 'points'}])
        assert await database.fetchrow(ROW, 'score') is None
        assert await database.fetchrow(ROW, 'id') is None

    registry_database(scenario)


def test_a_request_with_a_description_on_a_primary_field_changes_nothing_not_even_the_flag(world):
    async def scenario(w):
        await w.dataset(10)
        await w.database.execute('INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES (10, 9, 0)')

        def path(field: str) -> str:
            return f'/api/datasets/10/fields/{field}/description'

        for field in ('filepath', 'media_type'):
            both = await w.call(ADMIN, 'PATCH', path(field), json={'description': 'Here.', 'values_private': True})
            assert both.status_code == 409, both.text
            flag = await w.database.fetchval(
                'SELECT values_private FROM dataset_fields WHERE dataset_id = 10 AND field_name = $1',
                field,
            )
            assert flag is None, field  # not even the row that would have held the flag
            alone = await w.call(ADMIN, 'PATCH', path(field), json={'values_private': True})
            assert alone.status_code == 200 and alone.json()['values_private'] is True, alone.text
            again = await w.call(ADMIN, 'PATCH', path(field), json={'unit': 'u', 'values_private': False})
            assert again.status_code == 409, again.text
            listed = {entry['name']: entry for entry in (await w.call(ADMIN, 'GET', '/api/datasets/10/schema')).json()}
            assert listed[field]['values_private'] is True and listed[field]['unit'] is None, listed[field]

    world(scenario)
