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
# Rows every schema since the baseline accepts, so a migration also has to work on a database that holds data.
SEED = """
INSERT INTO datasets (name) VALUES ('upgrade-check');
INSERT INTO samples (dataset_id, filepath, media_type) SELECT id, 's3://upgrade-check/a.jpg', 'image' FROM datasets;
INSERT INTO views (dataset_id, name, slug, content) SELECT id, 'v', 'upgrade-check', '{}' FROM datasets;
"""


# The oldest release whose database this checkout's migrations upgrade: the current schema baseline, the upgrade
# stop (docs/UPGRADING.md, "What is supported"). 3.0.0 squashed the migrations up to 2.14.0 into its baseline, so a
# 2.14.0 database upgrades straight to 3.x and an older one goes to 2.14.0 first; the release just before it is
# refused. A major version that starts a new baseline moves both.
BASELINE_RELEASE = 'v2.14.0'
LAST_BEFORE_BASELINE = 'v2.13.2'


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
    # Seeded like the upgraded ones: each dataset has its own embeddings partition (0021).
    fresh = make((ROOT / 'backend/db/schema.sql').read_text() + SEED)
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


def test_a_release_before_the_baseline_is_refused_and_left_as_it_was(database):
    """Its migrations are no longer in this checkout: the runner names the release to upgrade to first and changes
    nothing."""
    make, psql, schema, migrate = database
    old_schema_sql = subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{LAST_BEFORE_BASELINE}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = make(old_schema_sql + SEED)
    ledger = 'SELECT string_agg(filename, \',\' ORDER BY filename) FROM schema_migrations'
    before = (schema(name), psql(name, ledger))
    result = migrate(name)
    assert result.returncode == 1, result.stdout
    assert f'before {BASELINE_RELEASE[1:]}' in result.stderr and f'Check out {BASELINE_RELEASE}' in result.stderr
    assert (schema(name), psql(name, ledger)) == before


def test_apply_migrations_sets_the_databases_plan_cache_mode_on_every_run(database):
    """A restored backup doesn't carry the database's settings (pg_dump without --create): the runner sets them again
    each time, as schema.sql does on a fresh install."""
    make, psql, _, migrate = database
    name = make((ROOT / 'backend/db/schema.sql').read_text())
    setting = (
        "SELECT coalesce(array_to_string(setconfig, ','), '') FROM pg_db_role_setting WHERE setrole = 0 "
        'AND setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())'
    )
    assert 'plan_cache_mode=force_custom_plan' in psql(name, setting)
    psql(name, f'ALTER DATABASE {name} RESET plan_cache_mode')
    assert 'plan_cache_mode' not in psql(name, setting)
    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
    assert 'plan_cache_mode=force_custom_plan' in psql(name, setting)
