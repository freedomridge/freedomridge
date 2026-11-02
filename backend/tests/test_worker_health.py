"""Worker availability is reported explicitly, including undeployed migrations."""

import asyncio
from datetime import datetime, timezone

from backend.system_status import worker_health


class WorkerDatabase:
    def __init__(self, rows=(), error=None):
        self.rows = rows
        self.error = error

    async def fetch(self, query):
        assert "interval '60 seconds'" in query
        if self.error:
            raise self.error
        return self.rows


def test_no_registered_workers_is_explicitly_offline():
    status = asyncio.run(worker_health(WorkerDatabase()))
    assert status == {
        'available': True,
        'server': {'online': 0, 'last_seen': None},
        'compute': {'online': 0, 'last_seen': None},
    }


def test_server_and_compute_health_are_independent():
    seen = datetime.now(timezone.utc)
    status = asyncio.run(
        worker_health(WorkerDatabase([{'placement': 'server', 'last_seen': seen, 'online': 1}])),
    )
    assert status['server'] == {'online': 1, 'last_seen': seen}
    assert status['compute']['online'] == 0


def test_stale_workers_remain_visible_but_offline():
    seen = datetime(2020, 1, 1, tzinfo=timezone.utc)
    status = asyncio.run(
        worker_health(WorkerDatabase([{'placement': 'server', 'last_seen': seen, 'online': 0}])),
    )
    assert status['server'] == {'online': 0, 'last_seen': seen}


def test_missing_table_or_database_outage_is_not_healthy():
    status = asyncio.run(worker_health(WorkerDatabase(error=RuntimeError('workers migration missing'))))
    assert status['available'] is False
    assert 'workers migration missing' in status['error']
