"""Every ingest endpoint checks the database volume's free space before writing.

Reproduced 2026-10-12 on a throwaway database: a sync filling the volume PANICked Postgres on a WAL
write, and its crash recovery then failed for want of space too, leaving the database down until
someone freed space and started it by hand. Ingest is refused with a 507 while there is still room;
free space that can't be read (managed Postgres, non-superuser) never refuses.
"""

import asyncio
import importlib.util
import sys
import types
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest
from fastapi import HTTPException

from backend import diskspace
from backend.database import db
from backend.routers import datasets, embeddings, hashes

ROOT = Path(__file__).resolve().parents[2]

GB = 1024**3
USER = {"id": 1, "role": "admin"}


class Reached(Exception):
    """The endpoint got past the gate and touched the connection to write."""


class WriteConn:
    def transaction(self):
        return nullcontext()

    async def execute(self, query, *args, **kwargs):
        # The map writer's transaction now checks read-only fences before its space/write gate.
        if query.startswith('SET LOCAL') or query.startswith('SELECT pg_advisory_xact_lock_shared'):
            return None
        raise Reached('execute')

    async def fetchval(self, query, *args):
        if 'pg_try_advisory_xact_lock_shared' in query:
            return True
        if "kind='map_refit'" in query:
            return False
        raise Reached('fetchval')

    async def fetchrow(self, query, *args):
        if 'FROM embedding_map_state' in query:
            return None
        raise Reached('fetchrow')

    def __getattr__(self, name):
        def touched(*args, **kwargs):
            raise Reached(name)

        return touched


class FakePool:
    def __init__(self):
        self.acquired = 0

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                pool.acquired += 1
                return WriteConn()

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def executemany(self, *args, **kwargs):
        raise Reached("executemany")


async def _noop(*args, **kwargs):
    return None


async def _write(*args, **kwargs):
    raise Reached("execute")


class _FakeUMAP:
    def __init__(self, **kwargs):
        pass

    def fit_transform(self, vectors):
        return np.zeros((len(vectors), 2))


@pytest.fixture
def free(monkeypatch):
    """Set the free bytes the gate reads (and the reduce's row count); stub the reads each endpoint
    makes before it, and UMAP itself."""
    state: Dict[str, Any] = {"free": None, "n": 200}

    async def fake_free(conn):
        return state["free"]

    async def all_valid(dataset_id, ids):
        return set(ids)

    async def array_columns(dataset_id):
        return set()

    async def count(query, *args):
        return state["n"]

    async def fetch(query, *args):
        if "e.vector" in query:  # the reduce's vectors
            return [{"id": i, "vector": "[0.1,0.2,0.3]", "field_name": "clip"} for i in range(state["n"])]
        return [{"id": i} for i in args[1]]  # the hash upload's valid sample ids

    async def no_viz_rank_estimate(conn, dataset_id, field_name):
        return 0

    monkeypatch.setattr(diskspace, "db_free_bytes", fake_free)
    monkeypatch.setattr(db, "pool", FakePool())
    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(db, "fetchval", count)
    monkeypatch.setattr(db, "execute", _write)  # bulk-set's UPDATE
    monkeypatch.setattr(datasets, "get_dataset_array_columns", array_columns)
    monkeypatch.setattr(datasets, "validate_embedding_dims", _noop)
    monkeypatch.setattr(embeddings, "validate_embedding_dims", _noop)
    monkeypatch.setattr(embeddings, "_valid_sample_ids", all_valid)
    monkeypatch.setattr(embeddings, "estimate_viz_rank_bytes", no_viz_rank_estimate)
    monkeypatch.setitem(sys.modules, "umap", types.SimpleNamespace(UMAP=_FakeUMAP))
    return state


def _call(state, what, n=200, *, background=False):
    """The endpoint `what`, writing n rows (a sample with one embedding is 2 rows)."""
    state["n"] = n
    vec = [0.1, 0.2, 0.3]
    if what == "samples":
        n //= 2
        return datasets.add_samples(
            1,
            {"samples": [{"filepath": f"s3://b/{i}.jpg", "embeddings": {"clip": vec}} for i in range(n)]},
            USER,
        )
    if what == "embeddings":
        return embeddings.upload_embeddings(
            1,
            [embeddings.EmbeddingUpload(sample_id=i, vector=vec) for i in range(n)],
            USER,
        )
    if what == "patch embeddings":
        return embeddings.upload_patch_embeddings(
            1,
            [embeddings.PatchEmbeddingUpload(sample_id=i, vector=vec, patch_scale="2x2") for i in range(n)],
            USER,
        )
    if what == "coordinates":
        return embeddings.upload_coordinates(
            1,
            "clip",
            [embeddings.CoordinateUpdate(sample_id=i, x=0.0, y=0.0) for i in range(n)],
            USER,
        )
    if what == "reduced coordinates":
        return embeddings.reduce_embeddings(1, embeddings.ReduceRequest(field_name="clip"), USER)
    if what == "bulk-set":
        return datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=list(range(n)), metadata={"k": 1}, background=background),
            USER,
        )
    if what == "bulk-set per sample":
        updates = [datasets.BulkSetUpdate(sample_id=i, metadata={"k": i}) for i in range(n)]
        return datasets.bulk_set_metadata(1, datasets.BulkSetRequest(updates=updates, background=background), USER)
    assert what == "hashes"
    return hashes.upload_hashes(
        1,
        [hashes.HashUpload(sample_id=i, field_name="phash", hash_hex="ff" * 8) for i in range(n)],
        USER,
    )


ENDPOINTS = [
    "samples",
    "embeddings",
    "patch embeddings",
    "coordinates",
    "reduced coordinates",
    "hashes",
    "bulk-set",
    "bulk-set per sample",
]
# What each one's 507 says it was refusing (for 200 rows).
REFUSING = {
    "samples": "adding 100 samples",  # 100 samples x (the sample + its embedding) = 200 rows
    "bulk-set": "setting metadata on 200 samples",
    "bulk-set per sample": "setting metadata on 200 samples",
}


def _reaches_the_write(state, what, n=200):
    """True when the endpoint got past the gate to its write (the embedding endpoints wrap a write
    error in a 500 "Database error: ..."; the vector upload opens its write transaction first)."""
    try:
        asyncio.run(_call(state, what, n))
    except Reached:
        return True
    except HTTPException as e:
        detail = str(e.detail)
        return e.status_code == 500 and detail in ("Database error: executemany", "Database error: transaction")
    return False


@pytest.mark.parametrize("what", ENDPOINTS)
def test_ingest_is_refused_with_507_before_any_write_when_the_disk_is_nearly_full(free, what):
    free["free"] = GB // 2
    with pytest.raises(HTTPException) as e:
        asyncio.run(_call(free, what))
    assert e.value.status_code == 507
    assert REFUSING.get(what, f" 200 {what}") in e.value.detail
    assert "0.50 GB free" in e.value.detail and "Free space or grow the volume" in e.value.detail


@pytest.mark.parametrize("what", ENDPOINTS)
@pytest.mark.parametrize("room", [50 * GB, None], ids=["plenty", "unreadable"])
def test_ingest_goes_ahead_with_room_or_when_free_space_cannot_be_read(free, what, room):
    free["free"] = room
    assert _reaches_the_write(free, what)


@pytest.mark.parametrize("what", ENDPOINTS)
def test_a_huge_request_needs_room_for_its_rows_not_just_the_headroom(free, what):
    # Room for 50k writes plus headroom admits 200 rows but refuses 100k.
    # Coordinates use the smaller narrow-table estimate.
    row_bytes = (
        diskspace.COORDINATE_ROW_BYTES if what in ("coordinates", "reduced coordinates") else diskspace.INGEST_ROW_BYTES
    )
    free["free"] = diskspace.HEADROOM_BYTES + 50_000 * row_bytes
    assert _reaches_the_write(free, what)
    with pytest.raises(HTTPException) as e:
        asyncio.run(_call(free, what, 100_000))
    assert e.value.status_code == 507


@pytest.mark.parametrize("what", ["bulk-set", "bulk-set per sample"])
def test_background_bulk_request_checks_total_space_before_job_submission(free, what):
    free["free"] = 2 * GB
    with pytest.raises(HTTPException) as e:
        asyncio.run(_call(free, what, 100_000, background=True))
    assert e.value.status_code == 507
    assert "setting metadata on 100000 samples" in e.value.detail


def test_near_the_line_the_message_does_not_read_as_a_contradiction(free):
    free["free"] = GB - 12 * 1024**2  # 1012 MB
    with pytest.raises(HTTPException) as e:
        asyncio.run(_call(free, "coordinates"))
    assert "needs about 1.00 GB" in e.value.detail and "0.99 GB free" in e.value.detail


def test_the_umap_projection_worker_checks_before_writing_coordinates(free, monkeypatch):
    """scripts/umap_project_job.py writes x/y straight to the database (a co-located worker, no API),
    so it runs the same check itself before each batch; main() logs the 507 and retries next cycle."""
    spec = importlib.util.spec_from_file_location("umap_project_job", ROOT / "scripts" / "umap_project_job.py")
    assert spec is not None and spec.loader is not None
    job = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(job)

    class Model:
        n_features_in_ = 3

        def transform(self, X):
            return np.zeros((len(X), 2))

    class Conn:
        def __init__(self):
            self.pending = [{"sample_id": i, "vector": "[0.1,0.2,0.3]", "vector_version": "1"} for i in range(200)]
            self.written = 0

        async def fetch(self, query, *args):
            rows, self.pending = self.pending, []
            return rows

        async def executemany(self, query, values):
            self.written += len(values)

        async def execute(self, query, *args):
            if "INSERT INTO embedding_coordinates" in query:
                self.written += len(args[0])
            return None

        async def fetchrow(self, query, *args):  # the map fence: no scheduled re-fits for this field
            return None

        async def fetchval(self, query, *args):  # no queued re-fit before activation, either
            return False

        def transaction(self):
            class Transaction:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            return Transaction()

    free["free"] = GB // 2
    conn = Conn()
    with pytest.raises(HTTPException) as e:
        asyncio.run(job._project(conn, 7, "clip", Model(), 5000))
    assert e.value.status_code == 507 and "projecting 200 UMAP points" in e.value.detail
    assert conn.written == 0

    free["free"] = 50 * GB
    conn = Conn()
    assert asyncio.run(job._project(conn, 7, "clip", Model(), 5000)) == 200
    assert conn.written == 200
