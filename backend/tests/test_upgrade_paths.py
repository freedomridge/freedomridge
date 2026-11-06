"""Every release upgrades straight to this code (docs/UPGRADING.md, "What is supported").

For each release since the current schema baseline (BASELINE_RELEASE), a database is created from its
backend/db/schema.sql, given a dataset, a sample and a saved view, and upgraded with this checkout's
scripts/apply_migrations.sh. Its schema must then equal a fresh install's from this checkout's
schema.sql, its ledger must record every migration, and its rows must still be there. Old schemas
come from git, so the checkout needs its tags (`git fetch --tags`; CI checks out with fetch-depth 0).
"""

import difflib
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from scripts.db_test_safety import UnsafeTestDatabase, require_container

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires disposable PostgreSQL',
)
# Rows every 1.x schema accepts, so a migration also has to work on a database that holds data.
SEED = """
INSERT INTO datasets (name) VALUES ('upgrade-check');
INSERT INTO samples (dataset_id, filepath, media_type) SELECT id, 's3://upgrade-check/a.jpg', 'image' FROM datasets;
INSERT INTO views (dataset_id, name, slug, content) SELECT id, 'v', 'upgrade-check', '{}' FROM datasets;
"""


# The oldest release whose database this checkout's migrations upgrade: the start of the current schema
# baseline (docs/UPGRADING.md, "What is supported"). 2.0 kept 1.x's baseline, so every 1.x release upgrades
# straight to 2.x; a major version that starts a new baseline moves this to its own first release.
BASELINE_RELEASE = 'v1.0.0'


def _version(tag):
    return tuple(int(part) for part in tag[1:].split('.'))


def release_tags():
    """The releases since the current schema baseline, oldest first."""
    tags = subprocess.run(
        ['git', '-C', str(ROOT), 'tag', '--list', 'v*'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    releases = [t for t in tags if re.fullmatch(r'v\d+\.\d+\.\d+', t) and _version(t) >= _version(BASELINE_RELEASE)]
    return sorted(releases, key=_version)


@pytest.fixture
def database():
    """make(name) creates a database in the disposable container; all of them are dropped afterwards."""
    try:
        container = require_container(os.environ['FRIDGE_TEST_POSTGRES_CONTAINER'])
    except UnsafeTestDatabase as error:
        pytest.fail(str(error))
    user = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN']).username or 'freedomridge'
    made = []

    def psql(name, sql):
        result = subprocess.run(
            ['docker', 'exec', '-i', container, 'psql', '-X', '-U', user, '-d', name, '-v', 'ON_ERROR_STOP=1', '-Atq'],
            input=sql,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f'psql on {name} failed: {result.stderr.strip()}'
        return result.stdout.strip()

    def make(schema_sql):
        name = f'_upgrade_{uuid4().hex[:12]}'
        psql('postgres', f'CREATE DATABASE {name}')
        made.append(name)
        psql(name, schema_sql)
        return name

    def schema(name):
        dump = subprocess.run(
            ['docker', 'exec', container, 'pg_dump', '-U', user, '--schema-only', '-d', name],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        # Comments, settings and pg_dump 18's per-dump \restrict key differ between any two dumps.
        noise = re.compile(r'^(--|SET |SELECT pg_catalog\.|\\(un)?restrict )')
        return [line for line in dump.splitlines() if line and not noise.match(line)]

    def migrate(name):
        env = {**os.environ, 'FRIDGE_DB_CONTAINER': container, 'FRIDGE_DB_NAME': name, 'FRIDGE_DB_USER': user}
        env['FRIDGE_PYTHON'] = sys.executable
        return subprocess.run(
            ['bash', 'scripts/apply_migrations.sh'],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    try:
        yield make, psql, schema, migrate
    finally:
        for name in made:
            psql('postgres', f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


def test_every_release_upgrades_to_a_fresh_installs_schema(database):
    make, psql, schema, migrate = database
    releases = release_tags()
    assert releases, (
        'No release tags in this checkout: run `git fetch --tags` (CI checks out with fetch-depth 0). '
        'In a fork: `git fetch upstream --tags && git push origin --tags`.'
    )
    fresh = make((ROOT / 'backend/db/schema.sql').read_text())
    expected_schema = schema(fresh)
    expected_ledger = psql(fresh, 'SELECT string_agg(filename, \',\' ORDER BY filename) FROM schema_migrations')

    for tag in releases:
        old_schema_sql = subprocess.run(
            ['git', '-C', str(ROOT), 'show', f'{tag}:backend/db/schema.sql'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        name = make(old_schema_sql + SEED)
        result = migrate(name)
        assert result.returncode == 0, f'{tag}: apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
        upgraded = schema(name)
        if upgraded != expected_schema:
            diff = '\n'.join(list(difflib.unified_diff(expected_schema, upgraded, 'fresh', tag, lineterm=''))[:80])
            pytest.fail(f'{tag} upgraded differs from a fresh install of this schema.sql:\n{diff}')
        ledger = psql(name, 'SELECT string_agg(filename, \',\' ORDER BY filename) FROM schema_migrations')
        assert ledger == expected_ledger, tag
        rows = psql(
            name,
            "SELECT (SELECT count(*) FROM datasets) || ',' || (SELECT count(*) FROM samples) || ','"
            " || (SELECT count(*) FROM views)",
        )
        assert rows == '1,1,1', tag
