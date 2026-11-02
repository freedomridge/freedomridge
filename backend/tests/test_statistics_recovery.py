"""Bounded statistics reads discard broken sockets before attempting transaction cleanup."""

import asyncio
import os
import time

import pytest

from backend import statistics_measure
from backend.tests import test_field_registry_postgres
from backend.tests.tcp_faults import blackhole_pool
from backend.tests.test_job_launch import wait_until

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')


def test_blackholed_measurement_releases_its_snapshot_and_pool_connection(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(statistics_measure, 'MEASURE_SECONDS', 0.1)
        backend_pid = None
        async with blackhole_pool(database) as (pool, flows):

            async def read(connection):
                nonlocal backend_pid
                backend_pid = await connection.fetchval('SELECT pg_backend_pid()')
                port = connection._transport.get_extra_info('sockname')[1]
                flows[port].set()
                await connection.execute('SELECT 1')

            started = time.monotonic()
            with pytest.raises(statistics_measure.MeasurementTooLarge):
                await asyncio.wait_for(statistics_measure._read_snapshot(pool, read), 3)
            assert time.monotonic() - started < 2.5

            async def released():
                return not await database.fetchval(
                    'SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)',
                    backend_pid,
                )

            await wait_until(released, timeout=2)
            assert (
                await asyncio.wait_for(
                    statistics_measure._read_snapshot(pool, lambda connection: connection.fetchval('SELECT 42')),
                    2,
                )
                == 42
            )

    registry_database(scenario)
