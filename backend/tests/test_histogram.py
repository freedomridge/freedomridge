from unittest.mock import AsyncMock

import pytest

from backend.routers import datasets


@pytest.fixture(autouse=True)
def builtin_arrays(monkeypatch):
    monkeypatch.setattr(datasets, 'get_dataset_array_columns', AsyncMock(return_value={'tags', 'inferred_tags'}))


class FakeDB:
    """
    Minimal async stub for db.fetchrow/fetch keyed by substring match of the SQL.
    Also exposes a `pool` whose acquired connections delegate to the same maps,
    mirroring the regression `async with db.pool.acquire()` path (SET LOCAL jit=off
    inside a transaction).
    """

    def __init__(self, fetchrow_map=None, fetch_map=None, fetchval_map=None):
        self.fetchrow_map = fetchrow_map or {}
        self.fetch_map = fetch_map or {}
        self.fetchval_map = fetchval_map or {}
        self.calls = []
        self.pool = _FakePool(self)

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        for key, value in self.fetchval_map.items():
            if key in query:
                return value
        raise AssertionError(f"No fetchval match for query: {query}")

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        for key, value in self.fetchrow_map.items():
            if key in query:
                return value
        raise AssertionError(f"No fetchrow match for query: {query}")

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "MIN(val)" in query:  # the numeric stats pass is fetched so it can run in parallel
            return [self.fetchrow_map["MIN(val)"]]
        for key, value in self.fetch_map.items():
            if key in query:
                return value
        raise AssertionError(f"No fetch match for query: {query}")

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))


class _FakeConn:
    def __init__(self, db):
        self._db = db

    def transaction(self):
        return _NullAsyncCM(None)

    async def execute(self, query, *args, **kwargs):
        await self._db.execute(query, *args)

    async def fetchrow(self, query, *args, **kwargs):
        return await self._db.fetchrow(query, *args)

    async def fetch(self, query, *args, **kwargs):
        return await self._db.fetch(query, *args)


class _FakePool:
    def __init__(self, db):
        self._db = db

    def acquire(self):
        return _NullAsyncCM(_FakeConn(self._db))


class _NullAsyncCM:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_histogram_numeric(monkeypatch):
    # Simulate numeric data (id field)
    fake_db = FakeDB(
        fetchrow_map={
            "MIN(val)": {"min": 0, "max": 100, "total": 5, "total_rows": 5},
        },
        fetch_map={
            "width_bucket": [
                {"bucket": 1, "count": 2},
                {"bucket": 2, "count": 3},
            ],
        },
        fetchval_map={"FROM dataset_fields": None},
    )
    monkeypatch.setattr(datasets, "db", fake_db)

    resp = await datasets.get_histogram(id=1, field="id", bins=2, filters=None)

    assert resp["field"] == "id"
    assert resp["bins"] == 2
    assert resp["total"] == 5
    assert len(resp["buckets"]) == 2
    assert resp["buckets"][0]["range"]["min"] == 0
    assert resp["buckets"][1]["range"]["max"] == 100


@pytest.mark.asyncio
async def test_histogram_timestamp(monkeypatch):
    # Simulate timestamp data that converts to epoch
    fake_db = FakeDB(
        fetchrow_map={
            "MIN(val)": {"min": 1_700_000_000_000_000, "max": 1_700_010_000_000_000, "total": 4, "total_rows": 4},
        },
        fetch_map={
            "AS bucket": [
                {"bucket": 1, "count": 1},
                {"bucket": 2, "count": 3},
            ],
        },
        fetchval_map={"FROM dataset_fields": None},
    )
    monkeypatch.setattr(datasets, "db", fake_db)

    resp = await datasets.get_histogram(id=1, field="created_at", bins=2, filters=None)

    assert resp["field"] == "created_at"
    assert resp["bins"] == 2
    assert len(resp["buckets"]) == 2
    # ISO labels should be present
    assert "T" in resp["buckets"][0]["label"]
    assert resp["buckets"][0]["range"]["min_label"]


@pytest.mark.asyncio
async def test_histogram_categorical(monkeypatch):
    # One materialized GROUP BY now yields total/missing/distinct/buckets in a single fetchrow
    # (was four separate scans). buckets come back as a jsonb array of {value, cnt}.
    fake_db = FakeDB(
        fetchrow_map={
            "grouped AS MATERIALIZED": {
                "total_rows": 5,
                "missing_count": 0,
                "distinct_total": 2,
                "buckets": [{"value": "cat", "cnt": 3}, {"value": "dog", "cnt": 2}],
            },
        },
        fetchval_map={"FROM dataset_fields": None},
    )
    monkeypatch.setattr(datasets, "db", fake_db)

    resp = await datasets.get_histogram(id=1, field="media_type", bins=None, filters=None, top=50)

    assert resp["field"] == "media_type"
    assert resp["buckets"][0]["key"] == "cat"
    assert resp["total"] == 5
    # The top-N cap is signalled, not silent: distinct_total + other_count are reported.
    assert resp["distinct_total"] == 2
    assert resp["other_count"] == 0  # 2 categories shown (cat=3, dog=2), total-missing=5, all shown


@pytest.mark.asyncio
async def test_histogram_datetime_metadata_field(monkeypatch):
    # A metadata datetime field whose NAME matches none of the created_at/_at/_date
    # patterns (e.g. `datetime`, `local_datetime`) must still take the time-binned path
    # because dataset_field_stats types it 'datetime' — otherwise it falls to the
    # categorical path and renders ~one bucket per distinct timestamp (the bug).
    fake_db = FakeDB(
        fetchrow_map={
            "MIN(val)": {"min": 1_700_000_000_000_000, "max": 1_700_010_000_000_000, "total": 4, "total_rows": 4},
        },
        fetch_map={
            "AS bucket": [
                {"bucket": 1, "count": 1},
                {"bucket": 2, "count": 3},
            ],
        },
        fetchval_map={"FROM dataset_fields": "datetime"},
    )
    monkeypatch.setattr(datasets, "db", fake_db)

    resp = await datasets.get_histogram(id=1, field="local_datetime", bins=2, filters=None)

    assert resp["field"] == "local_datetime"
    assert resp["bins"] == 2
    # Took the timestamp path: ISO bucket labels, not a per-value categorical grouping.
    assert "T" in resp["buckets"][0]["label"]
    assert resp["buckets"][0]["range"]["min_label"]


# ---- a single created_at value, and hex ids ----


async def test_single_value_timestamp_field_is_one_bucket_not_no_data(monkeypatch):
    # Regression: every sample shares one created_at (one ingest batch) -> the panel said "No data for
    # this field" and reported missing = total. It is one bucket holding every sample.
    fake_db = FakeDB(
        fetchrow_map={
            "MIN(val)": {"min": 1_700_000_000_000_000, "max": 1_700_000_000_000_000, "total": 24, "total_rows": 24},
        },
        fetch_map={"AS bucket": [{"bucket": 1, "count": 24}]},
        fetchval_map={"FROM dataset_fields": None},
    )
    monkeypatch.setattr(datasets, "db", fake_db)
    resp = await datasets.get_histogram(id=1, field="created_at", bins=None, filters=None)
    assert resp["missing"] == 0
    assert [b["count"] for b in resp["buckets"]] == [24]
    assert resp["buckets"][0]["range"]["min_label"] == "2023-11-14T22:13:20.000000"


@pytest.mark.parametrize('declared,as_number', [(None, False), ('string', True)])
async def test_mostly_text_field_gets_the_categorical_histogram(monkeypatch, declared, as_number):
    # Regression: hex ids like "0347677e05719710" pass the number regex now and then. A field whose
    # present values are mostly NOT numbers is categorical, not a numeric range over a few ids.
    # Declared text skips detection by default; detection still guards undeclared fields and
    # declared text read as numbers.
    fake_db = FakeDB(
        fetchrow_map={
            "MIN(val)": {"min": 8.0e14, "max": 3.1e53, "total": 3, "total_rows": 100, "present": 90},
            "grouped AS MATERIALIZED": {
                "total_rows": 100,
                "missing_count": 10,
                "distinct_total": 90,
                "buckets": [{"value": "0c6b86b4119e11e7", "cnt": 1}],
            },
        },
        fetchval_map={"FROM dataset_fields": declared},
    )
    monkeypatch.setattr(datasets, "db", fake_db)
    resp = await datasets.get_histogram(
        id=1,
        field="parent_image_id",
        bins=None,
        filters=None,
        top=50,
        as_number=as_number,
    )
    assert resp["buckets"][0]["key"] == "0c6b86b4119e11e7"
    numeric_sql = next(q for kind, q, _ in fake_db.calls if "MIN(val)" in q)
    assert "pg_input_is_valid" in numeric_sql  # a string field never casts what float8 cannot hold


async def test_number_typed_field_skips_the_extra_per_row_work(monkeypatch):
    # A field the stats type 'number' (every value a JSON number) needs neither
    # an extra exact-type check nor a separate presence count per row.
    fake_db = FakeDB(
        fetchrow_map={"MIN(val)": {"min": 0, "max": 10, "total": 5, "total_rows": 5, "present": None}},
        fetch_map={"width_bucket": [{"bucket": 1, "count": 5}]},
        fetchval_map={"FROM dataset_fields": "number"},
    )
    monkeypatch.setattr(datasets, "db", fake_db)
    resp = await datasets.get_histogram(id=1, field="speed_mph", bins=1, filters=None)
    assert [b["count"] for b in resp["buckets"]] == [5]
    numeric_sql = next(q for kind, q, _ in fake_db.calls if "MIN(val)" in q)
    assert "pg_input_is_valid" not in numeric_sql and "NULL AS raw" in numeric_sql
