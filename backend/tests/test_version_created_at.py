"""A version can be dated from the past, for history brought in from another system: it is listed, and is the
latest or not, by that date."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fridge.core.dataset import Dataset
from pydantic import ValidationError

from backend.routers import versions
from backend.tests.test_field_registry_postgres import ingest, registry_database

__all__ = ['registry_database']
needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

THEN = datetime(2024, 9, 7, 16, 21, 30, 126000, tzinfo=timezone.utc)


@needs_postgres
def test_a_version_dated_in_the_past_is_listed_by_that_date(registry_database):
    async def scenario(database):
        await ingest([{'split': 'train'}])
        await versions.create_version(1, versions.CreateVersionRequest(name='made now'))
        imported = await versions.create_version(1, versions.CreateVersionRequest(name='imported', created_at=THEN))
        assert imported['created_at'] == THEN
        assert [version['name'] for version in await versions.list_versions(1)] == ['made now', 'imported']
        await database.execute("""UPDATE samples SET metadata = '{"split": "val"}'""")
        assert (await versions.version_status(1))['latest_version']['name'] == 'made now'

        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        with pytest.raises(HTTPException) as refused:
            await versions.create_version(1, versions.CreateVersionRequest(name='early', created_at=tomorrow))
        assert refused.value.status_code == 400 and 'future' in refused.value.detail
        assert await database.fetchval('SELECT count(*) FROM dataset_versions') == 2

    registry_database(scenario)


def test_a_date_without_a_utc_offset_is_refused():
    with pytest.raises(ValidationError, match='timezone'):
        versions.CreateVersionRequest(name='naive', created_at=datetime(2024, 9, 7))


def test_the_sdk_sends_the_date_and_checks_the_server_used_it():
    dataset = Dataset.__new__(Dataset)
    dataset._id = 7
    dataset.client = Mock()
    dataset.client.create_version.return_value = {'sample_count': 3, 'created_at': '2024-09-07T16:21:30.126000Z'}

    dataset.create_version('imported', created_at=THEN)
    dataset.client.create_version.assert_called_with(7, 'imported', None, True, created_at=THEN.isoformat())

    with pytest.raises(ValueError, match='needs a timezone'):
        dataset.create_version('naive', created_at=datetime(2024, 9, 7))
    # A server from before versions could be dated ignores created_at and dates the version now.
    dataset.client.create_version.return_value = {'sample_count': 3, 'created_at': '2026-11-11T07:00:00+00:00'}
    with pytest.raises(ValueError, match='without the created_at given'):
        dataset.create_version('imported', created_at=THEN)
