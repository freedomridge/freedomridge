"""A version records who made it: the user making the request, or for history brought in from another system the
author it had there."""

import os
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fridge.core.dataset import Dataset

from backend.routers import versions
from backend.tests.test_field_registry_postgres import ingest, registry_database

__all__ = ['registry_database']
needs_postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

RICHARD = {'id': 1, 'role': 'admin', 'username': 'richard'}


@needs_postgres
def test_a_version_records_who_made_it(registry_database):
    async def scenario(database):
        await ingest([{'split': 'train'}])
        mine = await versions.post_version(1, versions.CreateVersionRequest(name='mine'), user=RICHARD)
        imported = await versions.post_version(
            1,
            versions.CreateVersionRequest(name='imported', created_by=' ada@example.com '),
            user=RICHARD,
        )
        # History with a date and no author: who made it is unknown, not whoever brings it in.
        then = datetime(2024, 9, 7, tzinfo=timezone.utc)
        dated = await versions.post_version(
            1,
            versions.CreateVersionRequest(name='dated', created_at=then),
            user=RICHARD,
        )
        # Made without a request, so by no user.
        unknown = await versions.create_version(1, versions.CreateVersionRequest(name='unknown'))
        made_by = {'mine': 'richard', 'imported': 'ada@example.com', 'dated': None, 'unknown': None}
        assert {v['name']: v['created_by'] for v in (mine, imported, dated, unknown)} == made_by
        assert {v['name']: v['created_by'] for v in await versions.list_versions(1)} == made_by
        assert (await versions.get_version(1, imported['id']))['created_by'] == 'ada@example.com'
        # A stated author or date is a claim about another system: who made the request is recorded beside it.
        brought_in_by = {'mine': None, 'imported': 'richard', 'dated': 'richard', 'unknown': None}
        assert {v['name']: v['imported_by'] for v in (mine, imported, dated, unknown)} == brought_in_by
        assert {v['name']: v['imported_by'] for v in await versions.list_versions(1)} == brought_in_by
        assert (await versions.get_version(1, dated['id']))['imported_by'] == 'richard'

        with pytest.raises(HTTPException) as refused:
            await versions.post_version(1, versions.CreateVersionRequest(name='blank', created_by='  '), user=RICHARD)
        assert refused.value.status_code == 400

        # A revert's undo point is made by whoever reverts.
        reverted = await versions.revert_to_version(1, mine['id'], user=RICHARD)
        assert (await versions.get_version(1, reverted['auto_snapshot_id']))['created_by'] == 'richard'

    registry_database(scenario)


def test_the_sdk_names_the_maker_and_checks_the_server_recorded_it():
    dataset = Dataset.__new__(Dataset)
    dataset._id = 7
    dataset.client = Mock()
    dataset.client.create_version.return_value = {'sample_count': 3, 'created_by': 'ada'}

    dataset.create_version('imported', created_by='ada')
    dataset.client.create_version.assert_called_with(7, 'imported', None, True, created_by='ada')

    # A server from before versions recorded their maker ignores created_by.
    dataset.client.create_version.return_value = {'sample_count': 3}
    with pytest.raises(ValueError, match='without the created_by given'):
        dataset.create_version('imported', created_by='ada')
