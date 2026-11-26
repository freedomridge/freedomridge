"""0042 removes the stored metadata keys named tags or media_type, as an upgrade runs it (scripts/apply_migrations.sh).

A database just before it holds such keys in a followed dataset, a dataset with none, a dataset being deleted and a
mirror of another primary's dataset, written as each was written. The migration must remove exactly the two keys,
everywhere, touch no other row, and leave everything else as it was: the other keys, the samples' own tags and
media_type, last_modified_at. It must tell nobody (no follower change, no statistics work, no job), say per dataset
what it did, change nothing and name no dataset when run again, say so when there is nothing, and refuse while the
app is connected when there is something to remove.
"""

import asyncio
import os
import re
from urllib.parse import urlparse

import asyncpg
import pytest

from backend.assistant.private import private_fields
from backend.tests import test_upgrade_paths
from backend.tests.test_upgrade_paths import ROOT

database = test_upgrade_paths.database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires disposable PostgreSQL',
)
# This checkout's schema as it was just before 0042, which changes no table: its ledger without the file.
BEFORE = (ROOT / 'backend/db/schema.sql').read_text() + (
    "\nDELETE FROM schema_migrations WHERE filename = '0042_protected_metadata_keys.sql';\n"
)
SEED = """
INSERT INTO users (username, password_hash, role) VALUES ('follower:lab', 'x', 'power_user');
INSERT INTO datasets (id, name, followed) VALUES (1, 'primary', TRUE), (2, 'clean', FALSE), (3, 'going', FALSE),
    (4, 'mirror', FALSE);
SELECT setval(pg_get_serial_sequence('datasets', 'id'), 4);
INSERT INTO followers (name, user_id) SELECT 'lab', id FROM users WHERE username = 'follower:lab';
INSERT INTO follower_datasets (follower_id, dataset_id, start_txid, acked_txid)
    SELECT f.id, 1, txid_current(), txid_current() FROM followers f;
INSERT INTO samples (id, dataset_id, filepath, media_type, tags, metadata, last_modified_at) VALUES
    (1, 1, 'a.jpg', 'image', '{sunny}', '{"tags": "a", "n": 1}', '2026-01-01T00:00:00Z'),
    (2, 1, 'b.jpg', 'image', '{}', '{"media_type": "scan", "n": 2, "heading": 5}', '2026-01-01T00:00:00Z'),
    (3, 1, 'c.jpg', 'video', '{x}', '{"tags": ["x"], "media_type": "y", "source_tags": "s", "tags_v2": 2}',
        '2026-01-01T00:00:00Z'),
    (4, 1, 'd.jpg', 'image', '{}', '{"n": 4, "media_type_guess": "image"}', '2026-01-01T00:00:00Z'),
    (5, 2, 'e.jpg', 'image', '{}', '{"note": {"tags": "nested"}}', '2026-01-01T00:00:00Z'),
    (6, 3, 'f.jpg', 'image', '{}', '{"tags": "going"}', '2026-01-01T00:00:00Z');
SELECT setval(pg_get_serial_sequence('samples', 'id'), 100);
-- A mirror of another primary's dataset, written as its sync writes it.
INSERT INTO primary_links (url, token_encrypted, identity) VALUES ('https://primary.example/api', 'x', 'cluster');
INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, state, feed_cursor)
    SELECT 4, id, 7, 'following', 1 FROM primary_links;
BEGIN;
SELECT set_config('fridge.mirror_sync', '4', TRUE);
INSERT INTO samples (id, dataset_id, filepath, media_type, metadata, last_modified_at)
    VALUES (7, 4, 'm.jpg', 'image', '{"tags": "m", "label": "weed"}', '2026-01-01T00:00:00Z');
INSERT INTO mirror_samples (dataset_id, origin_id, sample_id) VALUES (4, 1007, 7);
COMMIT;
UPDATE datasets SET deleting = TRUE WHERE id = 3;
"""
# What followers (the change feed), statistics, the duplicate work and the runner are told, each row as text.
LOGS = (
    'sample_block_changes',
    'vector_changes',
    'dataset_changes',
    'map_block_changes',
    'dataset_stats_pending',
    'dataset_stats_state',
    'duplicate_pending',
    'jobs',
)
ROWS = "SELECT id, metadata::text, media_type, tags::text, last_modified_at FROM samples ORDER BY id"
VERSIONS = 'SELECT id, xmin::text, ctid::text FROM samples ORDER BY id'  # a row's version


def _logs(psql, name):
    return {table: set(filter(None, psql(name, f'SELECT x::text FROM {table} x').splitlines())) for table in LOGS}


def _rows(psql, name):
    return {line.split('|', 1)[0]: line for line in psql(name, ROWS).splitlines()}


def test_0042_removes_the_two_keys_and_nothing_else_and_tells_nobody(database):
    make, psql, _, migrate = database
    name = make(BEFORE + SEED)
    before_rows, before_logs = _rows(psql, name), _logs(psql, name)
    before_versions = dict(line.split('|', 1) for line in psql(name, VERSIONS).splitlines())

    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'

    metadata = dict(line.split('|') for line in psql(name, 'SELECT id, metadata::text FROM samples').splitlines())
    assert metadata == {
        '1': '{"n": 1}',
        '2': '{"n": 2, "heading": 5}',
        '3': '{"tags_v2": 2, "source_tags": "s"}',
        '4': '{"n": 4, "media_type_guess": "image"}',  # a name that only contains one is an ordinary key
        '5': '{"note": {"tags": "nested"}}',  # only top-level keys name a field
        '6': '{}',  # a dataset being deleted too
        '7': '{"label": "weed"}',  # the mirror's row too
    }
    after_rows = _rows(psql, name)
    for key, row in before_rows.items():  # own media_type, own tags, last_modified_at as they were
        assert after_rows[key].split('|')[2:] == row.split('|')[2:], (row, after_rows[key])
    assert _logs(psql, name) == before_logs  # no follower change, statistics work, duplicate work or job
    versions = dict(line.split('|', 1) for line in psql(name, VERSIONS).splitlines())
    assert [key for key in versions if versions[key] != before_versions[key]] == ['1', '2', '3', '6', '7']
    assert psql(name, "SELECT count(*) FROM schema_migrations WHERE filename LIKE '0042_%'") == '1'

    shown = [line.strip() for line in result.stdout.splitlines() if line.startswith('  ')]  # the migration's lines
    assert 'dataset 1 (primary): removed tags from 2 samples, media_type from 2 samples' in shown
    assert 'dataset 3 (going): removed tags from 1 sample' in shown
    assert 'dataset 4 (mirror), a mirror: removed tags from 1 sample' in shown
    assert '5 samples changed in all; run VACUUM (ANALYZE) samples once the app is up' in shown
    assert not [line for line in shown if 'clean' in line]

    # Run again, without its ledger row: no row changes, no dataset named.
    psql(name, "DELETE FROM schema_migrations WHERE filename = '0042_protected_metadata_keys.sql'")
    again = migrate(name)
    assert again.returncode == 0, f'{again.stdout}\n{again.stderr}'
    assert dict(line.split('|', 1) for line in psql(name, VERSIONS).splitlines()) == versions
    assert '0042: nothing to remove' in again.stdout or 'nothing to remove: no sample' in again.stdout
    assert not re.search(r'dataset \d', again.stdout)


def test_0042_with_nothing_to_remove_says_so_and_changes_nothing(database):
    make, psql, _, migrate = database
    name = make(BEFORE + test_upgrade_paths.SEED)
    before = _rows(psql, name)
    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
    assert "nothing to remove: no sample's metadata has a key named tags or media_type" in result.stdout
    assert _rows(psql, name) == before


def test_0042_refuses_while_the_app_is_connected_and_something_is_to_remove(database):
    make, psql, _, migrate = database
    name = make(BEFORE + SEED)
    before = _rows(psql, name)
    dsn = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()

    async def while_connected():
        connection = await asyncpg.connect(dsn, server_settings={'application_name': 'freedomridge backend'})
        try:
            return migrate(name)
        finally:
            await connection.close()

    refused = asyncio.run(while_connected())
    assert refused.returncode != 0
    assert 'stop the app first. Still connected: freedomridge backend' in refused.stdout + refused.stderr
    assert _rows(psql, name) == before
    assert psql(name, "SELECT count(*) FROM schema_migrations WHERE filename LIKE '0042_%'") == '0'
    assert migrate(name).returncode == 0  # once it is stopped


# Fields an earlier release let a rename or a clone register under the two names: in the followed dataset a promoted
# one with values and an index, which landed on the built-in field's row and so carries what was said about the name
# (a description, a unit, the private mark that keeps its values from the assistant); a plain one without values in
# another; a mirror's, which is its primary's; and one in the dataset being deleted, whose guard refuses any write.
FIELDS = """
INSERT INTO dataset_fields (dataset_id, field_name, field_type, is_promoted, display_type, description, unit,
                            values_private)
    VALUES (1, 'tags', 'string', TRUE, 'enum', 'What the camera saw', 'label', TRUE),
           (2, 'media_type', 'string', FALSE, NULL, NULL, NULL, FALSE);
CREATE INDEX idx_samples_meta_1_tags ON samples ((metadata->>'tags')) WHERE dataset_id = 1;
BEGIN;
SELECT set_config('fridge.mirror_sync', '4', TRUE);
INSERT INTO dataset_fields (dataset_id, field_name, field_type) VALUES (4, 'tags', 'string');
COMMIT;
BEGIN;
SELECT set_config('fridge.writable_dataset', '3', TRUE);
INSERT INTO dataset_fields (dataset_id, field_name, field_type) VALUES (3, 'tags', 'string');
COMMIT;
"""
REGISTERED = (
    "SELECT dataset_id || ':' || field_name || ':' || origin FROM dataset_fields "
    "WHERE field_name IN ('tags', 'media_type')"
)
PENDING = "SELECT format('%s|%s|%s|%s', dataset_id, block_id, field_names, transaction_id) FROM dataset_stats_pending"
CHANGES = "SELECT format('%s|%s|%s', dataset_id, kind, transaction_id) FROM dataset_changes"


def _new(psql, name, query, before):
    return {row.rsplit('|', 1)[0] for row in set(psql(name, query).splitlines()) - before}


def test_0042_removes_a_field_registered_under_either_name_and_keeps_what_belongs_to_the_name(database):
    make, psql, _, migrate = database
    name = make(BEFORE + SEED + FIELDS)
    pending, changes, logs = (
        set(psql(name, PENDING).splitlines()),
        set(psql(name, CHANGES).splitlines()),
        _logs(psql, name),
    )

    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
    # Dataset 1's row is the built-in field's again, what was said about the name kept; the plain one is gone; the
    # mirror's and the dataset being deleted's stay.
    assert sorted(psql(name, REGISTERED).splitlines()) == ['1:tags:system', '3:tags:metadata', '4:tags:metadata']
    kept = "SELECT field_type, is_promoted, display_type, description, unit, values_private FROM dataset_fields"
    assert (
        psql(name, f"{kept} WHERE dataset_id = 1 AND field_name = 'tags'") == 'unknown|f||What the camera saw|label|t'
    )
    url = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()

    async def private():
        connection = await asyncpg.connect(url)
        try:
            return await private_fields(1, connection), await private_fields(2, connection)
        finally:
            await connection.close()

    assert asyncio.run(private()) == (frozenset({'tags'}), frozenset())  # still kept from the assistant
    # The sample's own field of each name counted again, and nothing else; followers of dataset 1 told.
    assert _new(psql, name, PENDING, pending) == {'1|-1|{tags}', '2|-1|{media_type}'}
    assert _new(psql, name, CHANGES, changes) == {'1|fields'}
    after = _logs(psql, name)
    for table in ('sample_block_changes', 'vector_changes', 'map_block_changes', 'duplicate_pending', 'jobs'):
        assert after[table] == logs[table], table
    assert psql(name, "SELECT to_regclass('idx_samples_meta_1_tags') IS NOT NULL") == 't'  # left, and named

    shown = [line.strip() for line in result.stdout.splitlines() if line.startswith('  ')]
    removed = [line for line in shown if 'removed the metadata field' in line]
    assert len(removed) == 2, shown
    assert removed[0].startswith('dataset 1 (primary): removed the metadata field tags')
    assert "(keeping its description, unit, private mark for the sample's own field)" in removed[0]
    assert 'idx_samples_meta_1_tags' in removed[0] and 'DROP INDEX CONCURRENTLY' in removed[0]
    assert removed[1].startswith('dataset 2 (clean): removed the metadata field media_type')
    assert 'index' not in removed[1] and 'keeping' not in removed[1]

    psql(name, "DELETE FROM schema_migrations WHERE filename = '0042_protected_metadata_keys.sql'")
    again = migrate(name)
    assert again.returncode == 0, f'{again.stdout}\n{again.stderr}'
    assert sorted(psql(name, REGISTERED).splitlines()) == ['1:tags:system', '3:tags:metadata', '4:tags:metadata']
    assert 'nothing to remove' in again.stdout and not re.search(r'dataset \d', again.stdout)


def test_0042_removes_such_a_field_with_no_sample_carrying_the_key_and_leaves_metadata_that_is_no_object(database):
    make, psql, _, migrate = database
    seed = """
INSERT INTO datasets (id, name) VALUES (1, 'emptied');
INSERT INTO samples (id, dataset_id, filepath, media_type, tags, metadata)
    VALUES (1, 1, 'a.jpg', 'image', '{x}', '{"n": 1}');
-- Metadata the app refuses (the samples triggers do), as an old database may hold it.
ALTER TABLE samples DISABLE TRIGGER USER;
INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES (2, 1, 'b.jpg', 'image', '["tags", "x"]'),
    (3, 1, 'c.jpg', 'image', '"tags"'), (4, 1, 'd.jpg', 'image', 'null');
ALTER TABLE samples ENABLE TRIGGER USER;
INSERT INTO dataset_fields (dataset_id, field_name, field_type) VALUES (1, 'tags', 'string');
"""
    name = make(BEFORE + seed)
    before = _rows(psql, name)
    dsn = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()

    async def while_connected():  # removing a field needs no app stopped: no sample changes
        connection = await asyncpg.connect(dsn, server_settings={'application_name': 'freedomridge backend'})
        try:
            return migrate(name)
        finally:
            await connection.close()

    result = asyncio.run(while_connected())
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
    assert psql(name, REGISTERED) == ''  # nothing said about the name: the row goes
    assert _rows(psql, name) == before  # a list, a string and JSON null stay as they were
    assert 'dataset 1 (emptied): removed the metadata field tags' in result.stdout
    assert 'nothing to remove' not in result.stdout and 'changed in all' not in result.stdout
