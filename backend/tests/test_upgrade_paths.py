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


# The last release before 0021 rewrote embeddings, with what followers keep: a followed dataset (a subscription, an
# acknowledged cursor, change-log rows) and a mirror of another primary's dataset (written as its sync writes it).
FOLLOWER_RELEASE = 'v2.8.0'
FOLLOWER_SEED = """
INSERT INTO users (username, password_hash, role) VALUES ('follower:lab', 'x', 'power_user');
INSERT INTO datasets (name) VALUES ('followed'), ('mirror');
INSERT INTO followers (name, user_id) SELECT 'lab', id FROM users WHERE username = 'follower:lab';
INSERT INTO follower_datasets (follower_id, dataset_id, start_txid, acked_txid)
    SELECT id, 1, txid_current(), txid_current() FROM followers;
INSERT INTO samples (dataset_id, filepath, media_type)
    SELECT 1, 'followed-' || g, 'image' FROM generate_series(1, 20) g;
INSERT INTO embeddings (sample_id, field_name, vector) SELECT id, 'clip', ARRAY[id, 1]::real[]::vector FROM samples;
INSERT INTO embedding_coordinates (sample_id, field_name, x, y) SELECT id, 'clip', id, -id FROM samples;
INSERT INTO primary_links (url, token_encrypted, identity) VALUES ('https://primary.example/api', 'x', 'cluster');
INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, state, feed_cursor, vector_fields)
    SELECT 2, id, 5, 'following', 12345, '{clip}' FROM primary_links;
BEGIN;
SELECT set_config('fridge.mirror_sync', '2', TRUE);
INSERT INTO samples (dataset_id, filepath, media_type)
    SELECT 2, 'mirror-' || g, 'image' FROM generate_series(1, 10) g;
INSERT INTO mirror_samples (dataset_id, origin_id, sample_id)
    SELECT 2, 1000 + id, id FROM samples WHERE dataset_id = 2;
INSERT INTO embeddings (sample_id, field_name, vector)
    SELECT id, 'clip', ARRAY[id, 2]::real[]::vector FROM samples WHERE dataset_id = 2;
INSERT INTO embedding_coordinates (sample_id, field_name, x, y)
    SELECT id, 'clip', id, id FROM samples WHERE dataset_id = 2;
COMMIT;
"""
# Each as text, in both schemas: the followers' logs, the mirror's samples and cursor, and every vector and point.
FOLLOWER_STATE = {
    'logs': """SELECT (SELECT string_agg(format('%s/%s/%s/%s/%s', dataset_id, sample_id, kind, field_name,
                                                transaction_id), ',' ORDER BY id) FROM vector_changes)
               || '|' || (SELECT count(*) FROM sample_block_changes) || '|' || (SELECT count(*) FROM dataset_changes)
               || '|' || (SELECT count(*) FROM map_block_changes)""",
    'mirror': """SELECT (SELECT string_agg(format('%s/%s', origin_id, sample_id), ',' ORDER BY origin_id)
                         FROM mirror_samples)
                 || '|' || (SELECT feed_cursor FROM mirrors WHERE dataset_id = 2)""",
    'vectors': """SELECT string_agg(format('%s/%s/%s/%s', s.dataset_id, e.sample_id, e.field_name, e.vector), ','
                                    ORDER BY e.sample_id)
                  FROM embeddings e JOIN samples s ON s.id = e.sample_id""",
    'points': """SELECT string_agg(format('%s/%s/%s/%s', sample_id, field_name, x, y), ',' ORDER BY sample_id)
                 FROM embedding_coordinates""",
}


def test_0021_keeps_what_followers_and_mirrors_hold_and_logs_nothing(database):
    """0021 copies every vector and gives every map point its dataset. On a primary, none of it is a change its
    followers must fetch again; on a follower, its mirrors' rows are copied though only their sync may write them."""
    make, psql, _, migrate = database
    old_schema_sql = subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{FOLLOWER_RELEASE}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = make(old_schema_sql + FOLLOWER_SEED)
    before = {key: psql(name, query) for key, query in FOLLOWER_STATE.items()}
    assert before['logs'].count('/clip/') == 20, before['logs']  # the followed dataset's vectors were logged
    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
    after = {key: psql(name, query) for key, query in FOLLOWER_STATE.items()}
    assert after == before
    assert psql(name, 'SELECT count(*) FROM embeddings_d2') == '10'


def test_0021_continues_vector_ids_after_the_old_sequence_without_samples_index(database):
    """New ids continue after the last one the old table gave out, not after the newest vector left: a manual
    concept's "Score new" scores ids above its watermark, which is past max(id) once the newest vectors were deleted.
    The index advisor can have removed samples' (dataset_id, id) index, which 0021 makes unique."""
    make, psql, _, migrate = database
    old_schema_sql = subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{FOLLOWER_RELEASE}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = make(
        old_schema_sql
        + SEED
        + """
INSERT INTO samples (dataset_id, filepath, media_type)
    SELECT id, 's3://upgrade-check/' || g || '.jpg', 'image' FROM datasets, generate_series(2, 4) g;
INSERT INTO embeddings (sample_id, field_name, vector) SELECT id, 'clip', '[1,0]' FROM samples ORDER BY id;
DELETE FROM embeddings WHERE id > (SELECT min(id) FROM embeddings);
DROP INDEX idx_samples_dataset_id_id;
""",
    )
    given = int(psql(name, 'SELECT last_value FROM embeddings_id_seq'))
    assert given == 4 and psql(name, 'SELECT max(id) FROM embeddings') == '1'
    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
    new_id = psql(
        name,
        "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) "
        "SELECT dataset_id, id, 'other', '[1,1]' FROM samples ORDER BY id LIMIT 1 RETURNING id",
    )
    assert int(new_id) > given
    assert (
        psql(name, "SELECT indisunique FROM pg_index WHERE indexrelid = 'idx_samples_dataset_id_id'::regclass") == 't'
    )


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
