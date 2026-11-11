"""The check of a short search answer against PostgreSQL (test_search_iterative_scan.py drives a fake connection):
the nested transaction is a savepoint, a statement past the budget arrives as QueryCanceledError, the index's rows
are the answer with the incomplete header, and the outer transaction and its connection stay usable."""

import json
import os
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import Request

from backend.models import Sample, SearchRequest
from backend.routers import search
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_field_registry_postgres import USER, ingest

registry_database = registry_helpers.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)


def test_a_check_past_its_budget_answers_with_the_index_s_rows(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(search, 'db', database)
        monkeypatch.setattr(search, 'get_optional_user', AsyncMock(return_value=USER))
        monkeypatch.setattr(search, 'check_dataset_access', AsyncMock())
        request = Request({'type': 'http', 'method': 'POST', 'path': '/search', 'headers': []})
        sample_ids = (await ingest([{'level': 1.0}, {'level': 2.0}, {'level': 3.0}]))['ids']
        await database.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
            "SELECT dataset_id, id, 'clip', '[1,0]'::vector FROM samples WHERE id = ANY($1::integer[])",
            sample_ids,
        )
        ask = SearchRequest(dataset_id=1, vector=[1, 0], limit=50)

        # Three of fifty asked, and the count finds no more: a complete answer, as before.
        complete = await search.vector_search(ask, request)
        assert sorted(row['id'] for row in complete) == sorted(sample_ids)

        # The count takes longer than the budget.
        fetchval = asyncpg.Connection.fetchval

        async def slow_count(self, query, *args, **kwargs):
            if 'count(*) FROM (' in query:
                await self.execute('SELECT pg_sleep(2)')
            return await fetchval(self, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetchval', slow_count)
        monkeypatch.setattr(search, '_SHORT_CHECK_SECONDS', 0.2)
        answer = await search.vector_search(ask, request)
        assert answer.headers[search.SEARCH_INCOMPLETE_HEADER] == '1'
        rows = json.loads(answer.body)
        assert sorted(row['id'] for row in rows) == sorted(sample_ids)
        assert set(rows[0]) == set(Sample.model_fields)

        # The pool's connections are as they were: no leftover timeout, no aborted transaction.
        monkeypatch.setattr(asyncpg.Connection, 'fetchval', fetchval)
        monkeypatch.setattr(search, '_SHORT_CHECK_SECONDS', 5.0)
        assert len(await search.vector_search(ask, request)) == 3

    registry_database(scenario, pool_size=1)
