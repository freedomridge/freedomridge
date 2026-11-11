"""backend/db/postgresql.conf is what the database container loads (a bind mount, applied on reload)."""

import asyncio
import os
import re
from pathlib import Path

import asyncpg
import pytest

CONF = Path(__file__).resolve().parents[1] / 'db' / 'postgresql.conf'
TRY_COMPOSE = Path(__file__).resolve().parents[2] / 'docker-compose.try.yml'
MEMORY = ('shared_buffers', 'effective_cache_size', 'work_mem', 'maintenance_work_mem')


def _settings() -> dict:
    found = {}
    for line in CONF.read_text().splitlines():
        match = re.match(r'^\s*([a-z_]+)\s*=\s*([^#]+?)\s*(?:#.*)?$', line)
        if match:
            found[match.group(1)] = match.group(2).strip().strip("'")
    return found


def _bytes(value: str) -> int:
    match = re.fullmatch(r'(\d+)(kB|MB|GB|TB)', value)
    assert match, value
    return int(match.group(1)) * 1024 ** {'kB': 1, 'MB': 2, 'GB': 3, 'TB': 4}[match.group(2)]


def test_the_try_file_sizes_postgresql_for_a_laptop():
    """`npx freedomridge` runs docker-compose.try.yml on laptops, with this file mounted. Its memory settings are for a
    32 GB server: 8 GB of shared buffers alone is more than a small Docker Desktop VM has, and PostgreSQL then doesn't
    start. The try file sets each one smaller, from an environment variable to give it more."""
    command = re.search(r'^\s+command: (postgres .*)$', TRY_COMPOSE.read_text(), re.M)
    assert command, 'docker-compose.try.yml has no postgres command'
    overrides = dict(re.findall(r'-c ([a-z_]+)=(\S+)', command.group(1)))
    server = _settings()
    defaults = {}
    for name in MEMORY:
        default = re.fullmatch(r'\$\{POSTGRES_[A-Z_]+:-(\w+)\}', overrides.get(name, ''))
        assert default, f'docker-compose.try.yml sets {name} from no environment variable: {overrides.get(name)}'
        defaults[name] = _bytes(default.group(1))
        assert defaults[name] < _bytes(server[name]), name
    assert defaults['shared_buffers'] <= _bytes('1GB')


def test_map_refit_swaps_get_room_for_wal():
    # A large-dataset re-fit swap wrote ~15 GB of WAL at max_wal_size=4GB (full-page images after each
    # checkpoint); 16GB + lz4 page images measured ~10-12 GB.
    settings = _settings()
    assert settings['max_wal_size'] == '16GB'
    assert settings['wal_compression'] == 'lz4'


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
def test_every_setting_is_one_the_database_image_accepts():
    """A misspelt name or an unsupported value would only show up when live reloads (and is then
    ignored with a log line), so check them against the same image (Dockerfile.db)."""

    async def check():
        connection = await asyncpg.connect(os.environ['FRIDGE_TEST_POSTGRES_DSN'])
        try:
            rows = await connection.fetch(
                'SELECT name, vartype, enumvals FROM pg_settings WHERE name = ANY($1::text[])',
                list(_settings()),
            )
        finally:
            await connection.close()
        return {row['name']: row for row in rows}

    known = asyncio.run(check())
    settings = _settings()
    assert sorted(set(settings) - set(known)) == []
    for name, value in settings.items():
        if known[name]['vartype'] == 'enum':
            assert value in known[name]['enumvals'], (name, value, known[name]['enumvals'])
