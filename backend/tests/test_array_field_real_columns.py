"""An array field can't take the name of a column samples already has. The reserved list missed lon, lat and the
two user-id columns: over one of them ADD COLUMN IF NOT EXISTS did nothing, and the name was registered as an array
field, which the grid, the filters and a follower's sync would then read and write as TEXT[]."""

import os

import pytest
from fastapi import HTTPException

from backend import follower_sync
from backend.routers import datasets
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
USER = registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


def test_an_array_field_cannot_take_a_real_column_s_name(registry_database):
    async def scenario(database):
        for name in ('lon', 'lat', 'created_by_user_id', 'modified_by_user_id'):
            with pytest.raises(HTTPException) as refused:
                await datasets.create_array_field(1, datasets.CreateArrayFieldRequest(field_name=name), user=USER)
            assert refused.value.status_code == 400 and 'reserved' in refused.value.detail
            # A primary's array field by that name: the sync refuses it, as any name it can't hold.
            with pytest.raises(ValueError, match='cannot hold'):
                await follower_sync.ensure_array_columns(database.pool, 1, [name])
        assert await database.fetchval('SELECT array_agg(column_name) FROM sample_array_columns') == ['tags']
        kept = await database.fetchval(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'samples'::regclass AND attname = 'lon'",
        )
        assert kept == 'double precision'

    registry_database(scenario)
