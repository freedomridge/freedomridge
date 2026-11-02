"""Multi-GB single-transaction writes check the database volume's free space first and refuse
with a 507 instead of letting Postgres fill the disk mid-write (a WAL write failure PANICs it).
Unavailable free-space info (managed Postgres, non-superuser) must skip the gate, never refuse."""

import asyncio

import pytest
from fastapi import HTTPException

from backend import diskspace
from backend.diskspace import (
    HEADROOM_BYTES,
    db_free_bytes,
    db_volume_bytes,
    estimate_viz_rank_bytes,
    require_free_space,
)

GB = 1024**3


class FakeConn:
    """Answers the df dance and the estimate queries from a script; records executed SQL."""

    def __init__(
        self,
        data_dir="/var/lib/postgresql/data",
        df_line="180000000000 50000000000",
        fail_copy=False,
        vals=None,
    ):
        self.data_dir, self.df_line, self.fail_copy = data_dir, df_line, fail_copy
        self.vals = vals or {}
        self.executed: list = []

    async def fetchval(self, query, *args):
        if "data_directory" in query:
            return self.data_dir
        if "FROM _fridge_df" in query:
            return self.df_line
        for key, v in self.vals.items():
            if key in query:
                return v
        raise AssertionError(f"unexpected fetchval: {query}")

    async def execute(self, query, *args):
        self.executed.append(query)
        if "COPY" in query and self.fail_copy:
            raise RuntimeError("must be superuser or have privileges of the pg_execute_server_program role")


def test_free_bytes_reads_df_of_the_servers_data_directory():
    conn = FakeConn(data_dir="/pgdata", df_line=" 987654321   123456789 ")
    assert asyncio.run(db_free_bytes(conn)) == 123456789
    assert asyncio.run(db_volume_bytes(FakeConn(df_line="987654321 123456789"))) == (987654321, 123456789)
    copy = [q for q in conn.executed if q.startswith("COPY")][0]
    assert "df -B1 --output=size,avail /pgdata" in copy


def test_free_bytes_is_none_when_not_superuser_or_odd_output():
    assert asyncio.run(db_free_bytes(FakeConn(fail_copy=True))) is None
    assert asyncio.run(db_free_bytes(FakeConn(df_line="n/a"))) is None
    assert asyncio.run(db_free_bytes(FakeConn(df_line="123456789"))) is None  # size missing
    assert asyncio.run(db_free_bytes(FakeConn(data_dir="/x'; rm -rf /"))) is None  # never shell-interpolated


def test_gate_refuses_with_507_and_an_actionable_message():
    conn = FakeConn(df_line=f"{100 * GB} {3 * GB}")
    with pytest.raises(HTTPException) as e:
        asyncio.run(require_free_space(conn, 5 * GB, "a full snapshot of large-dataset"))
    assert e.value.status_code == 507
    assert "full snapshot of large-dataset" in e.value.detail
    assert f"{(5 * GB + HEADROOM_BYTES) / GB:.2f} GB" in e.value.detail
    assert "3.00 GB free" in e.value.detail
    assert "Settings → System status" in e.value.detail


def test_gate_passes_with_room_and_skips_when_unknown():
    asyncio.run(require_free_space(FakeConn(df_line=f"{100 * GB} {20 * GB}"), 5 * GB, "x"))  # no raise
    asyncio.run(require_free_space(FakeConn(fail_copy=True), 10**15, "x"))  # unknown → skipped


def test_viz_rank_estimate_is_twice_the_fields_share_of_the_table():
    conn = FakeConn(
        vals={
            "pg_total_relation_size('embedding_viz_rank')": 4 * GB,
            "count(*) FROM embedding_coordinates": 1_000,
            "sum(total_points)": 4_000,
        },
    )
    assert asyncio.run(estimate_viz_rank_bytes(conn, 7, "clip")) == 2 * GB


def test_headroom_is_at_least_a_gigabyte():
    assert diskspace.HEADROOM_BYTES >= GB
