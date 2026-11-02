"""New datasets are private until an admin opens them with is_global."""

import os

import pytest
from fastapi import HTTPException

from backend import access
from backend.routers import datasets
from backend.tests.test_field_registry_postgres import USER, registry_database

__all__ = ['registry_database']
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

GUEST = {'id': 2, 'role': 'guest'}


def test_new_datasets_are_private_until_an_admin_opens_them(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(access, 'db', database)
        await database.execute("SELECT setval('datasets_id_seq', 2)")  # the fixture seeds ids 1 and 2
        created = await datasets.create_dataset(datasets.DatasetCreate(name='fresh'), USER)
        assert created['is_global'] is False
        # The column default matches, so a row inserted without is_global is private too.
        assert await database.fetchval("INSERT INTO datasets(name) VALUES ('raw') RETURNING is_global") is False

        with pytest.raises(HTTPException) as denied:
            await access.check_dataset_access(created['id'], GUEST)
        assert denied.value.status_code == 403
        assert (await access.check_dataset_access(created['id'], USER))['id'] == created['id']

        opened = await datasets.update_dataset(created['id'], datasets.DatasetUpdate(is_global=True), USER)
        assert opened['is_global'] is True
        assert (await access.check_dataset_access(created['id'], GUEST))['id'] == created['id']

        closed = await datasets.update_dataset(created['id'], datasets.DatasetUpdate(is_global=False), USER)
        assert closed['is_global'] is False
        with pytest.raises(HTTPException):
            await access.check_dataset_access(created['id'], GUEST)

        public = await datasets.create_dataset(datasets.DatasetCreate(name='public', is_global=True), USER)
        assert (await access.check_dataset_access(public['id'], GUEST))['id'] == public['id']

    registry_database(scenario)
