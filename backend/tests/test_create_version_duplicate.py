"""POST /api/datasets/{id}/versions with a name that already exists is a 409, not an unhandled
500. The unhandled UniqueViolationError went through ServerErrorMiddleware's re-raise, after
which uvicorn closes the connection — on CI that surfaced as a connection reset instead of a
response. The live lock is python/fridge/test_integrity.py "duplicate version name rejected".
"""

import asyncio

import asyncpg
import pytest
from fastapi import HTTPException

from backend.routers import datasets as datasets_router
from backend.routers import versions as versions_router


class _Conn:
    def __init__(self, insert_error=None):
        self.insert_error = insert_error
        self.tx_exit_exc: list = []
        self.executed: list = []

    def transaction(self, isolation=None):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                conn.tx_exit_exc.append(exc_type)  # asyncpg rolls back when this is not None
                return False

        return _Tx()

    async def fetchval(self, query, *args):
        return 3

    async def fetch(self, query, *args):
        return []

    async def fetchrow(self, query, *args):
        if self.insert_error is not None:
            raise self.insert_error
        return {
            "id": 7,
            "name": args[1],
            "description": args[2],
            "sample_count": args[3],
            "include_metadata": args[4],
            "created_at": None,
        }

    async def execute(self, query, *args, **kwargs):
        self.executed.append(query)


class _DB:
    def __init__(self, conn):
        self.conn = conn
        db = self

        class _Pool:
            def acquire(self):
                class _Acq:
                    async def __aenter__(self):
                        return db.conn

                    async def __aexit__(self, *exc):
                        return False

                return _Acq()

        self.pool = _Pool()

    async def fetchrow(self, query, *args):
        return {"id": args[0]}  # the dataset exists


async def _no_array_columns():
    return set()


def _create(monkeypatch, conn, name="v1"):
    monkeypatch.setattr(versions_router, "db", _DB(conn))
    monkeypatch.setattr(datasets_router, "get_array_columns", _no_array_columns)
    req = versions_router.CreateVersionRequest(name=name)
    return asyncio.run(versions_router.create_version(4, req))


def test_duplicate_name_is_a_409_and_rolls_back(monkeypatch):
    conn = _Conn(
        insert_error=asyncpg.UniqueViolationError(
            'duplicate key value violates unique constraint "dataset_versions_dataset_id_name_key"',
        ),
    )
    with pytest.raises(HTTPException) as ei:
        _create(monkeypatch, conn)
    assert ei.value.status_code == 409
    assert "'v1'" in ei.value.detail and "already exists" in ei.value.detail
    assert conn.tx_exit_exc == [HTTPException], "the transaction must exit with the error (rollback)"
    assert conn.executed == [], "no snapshot rows may be written for a refused version"


def test_other_db_errors_are_not_relabeled_as_409(monkeypatch):
    conn = _Conn(insert_error=asyncpg.ForeignKeyViolationError("dataset_versions_dataset_id_fkey"))
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        _create(monkeypatch, conn)


def test_concurrent_settings_change_is_a_409_and_rolls_back(monkeypatch):
    conn = _Conn(insert_error=asyncpg.SerializationError("concurrent update"))
    with pytest.raises(HTTPException) as error:
        _create(monkeypatch, conn)
    assert error.value.status_code == 409
    assert "retry" in error.value.detail
    # Each attempt (the first and its two retries) rolled back.
    assert conn.tx_exit_exc == [asyncpg.SerializationError] * 3
    assert conn.executed == []


def test_new_name_still_creates_the_version(monkeypatch):
    conn = _Conn()
    out = _create(monkeypatch, conn, name="v2")
    assert out["id"] == 7 and out["name"] == "v2" and out["sample_count"] == 3
    assert len(conn.executed) == 1, "snapshot rows are written once"
    assert conn.tx_exit_exc == [None]
