"""0030 adds the key that deletes an image's objects with it. A database that ran 0029 before 0030 existed can hold
objects whose column names an image deleted since: 0030 deletes those, as 0029 deletes its orphans (logged for
followers and statistics, the field caches kept), except on a mirror or in a dataset being deleted, where it only
clears their column. Then the key is checked."""

import asyncio
import os
from urllib.parse import urlparse

import asyncpg
import pytest

from backend.tests import test_upgrade_paths
from backend.tests.test_object_parent_migration import _logs, _new
from backend.tests.test_upgrade_paths import ROOT

database = test_upgrade_paths.database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires disposable PostgreSQL',
)
# 0029's schema: this one without the key, and its ledger without 0030.
BEFORE_0030 = """
ALTER TABLE samples DROP CONSTRAINT samples_parent_fkey;
DELETE FROM schema_migrations WHERE filename = '0030_object_parent_key.sql';
"""
SEED = """
INSERT INTO users (username, password_hash, role) VALUES ('follower:lab', 'x', 'power_user');
INSERT INTO datasets (id, name, followed) VALUES (1, 'primary', TRUE), (3, 'going', FALSE), (5, 'mirror', FALSE);
SELECT setval(pg_get_serial_sequence('datasets', 'id'), 5);
INSERT INTO followers (name, user_id) SELECT 'lab', id FROM users WHERE username = 'follower:lab';
INSERT INTO follower_datasets (follower_id, dataset_id, start_txid, acked_txid)
    SELECT f.id, d.id, txid_current(), txid_current() FROM followers f, datasets d WHERE d.followed;
INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES
    (1, 1, 'a.jpg', 'image', '{}'),
    (2, 1, 'b.jpg', 'image', '{}'),
    (10, 1, 'a.jpg#0', 'object', '{"parent_id": 1, "label": "weed"}'),
    (11, 1, 'b.jpg#0', 'object', '{"parent_id": 2, "label": "corn"}'),
    (12, 1, 'b.jpg#1', 'object', '{"parent_id": "0002", "label": "corn", "only_on_stale": 1}'),
    (30, 3, 'd.jpg', 'image', '{}'),
    (31, 3, 'd.jpg#0', 'object', '{"parent_id": 30}');
SELECT setval(pg_get_serial_sequence('samples', 'id'), 100);
INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES (1, 10, 'clip_obj', '[0,1]'),
    (1, 11, 'clip_obj', '[1,0]');
INSERT INTO dataset_fields (dataset_id, field_name, field_type) VALUES
    (1, 'label', 'string'), (1, 'only_on_stale', 'number'), (1, 'parent_id', 'number');
INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, unique_values, values_complete) VALUES
    (1, 'label', 'string', '{corn,weed}', TRUE), (1, 'only_on_stale', 'number', NULL, TRUE),
    (1, 'parent_id', 'number', NULL, TRUE);
INSERT INTO primary_links (url, token_encrypted, identity) VALUES ('https://primary.example/api', 'x', 'cluster');
INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, state, feed_cursor)
    SELECT 5, id, 7, 'following', 1 FROM primary_links;
BEGIN;
SELECT set_config('fridge.mirror_sync', '5', TRUE);
INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES
    (50, 5, 'm.jpg', 'image', '{}'),
    (51, 5, 'm.jpg#0', 'object', '{"parent_id": 50}');
INSERT INTO mirror_samples (dataset_id, origin_id, sample_id) SELECT 5, 1000 + id, id FROM samples WHERE dataset_id = 5;
COMMIT;
-- 0029 linked them; the images are deleted before 0030 (nothing cleared their objects' column then).
DELETE FROM samples WHERE id IN (2, 30);
BEGIN;
SELECT set_config('fridge.mirror_sync', '5', TRUE);
DELETE FROM samples WHERE id = 50;
COMMIT;
UPDATE datasets SET deleting = TRUE WHERE id = 3;
"""


def _schema_sql():
    return (ROOT / 'backend' / 'db' / 'schema.sql').read_text()


def test_0030_deletes_objects_whose_image_went_since_0029_and_adds_the_key(database):
    make, psql, _, migrate = database
    name = make(_schema_sql() + BEFORE_0030 + SEED)
    stale = "SELECT string_agg(id || '>' || parent_id, ',' ORDER BY id) FROM samples WHERE parent_id IS NOT NULL"
    assert psql(name, stale) == '10>1,11>2,12>2,31>30,51>50'
    before = _logs(psql, name)
    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'

    # Deleted on the primary (11, 12) with its vector; cleared on the mirror (51) and in the dataset being deleted (31).
    rows = "SELECT string_agg(id || '>' || coalesce(parent_id::text, '-'), ',' ORDER BY id) FROM samples"
    assert psql(name, rows) == '1>-,10>1,31>-,51>-'
    assert psql(name, "SELECT string_agg(sample_id::text, ',') FROM embeddings") == '10'
    # The field caches as a bulk delete leaves them.
    fields = "SELECT string_agg(field_name, ',' ORDER BY field_name) FROM dataset_fields WHERE dataset_id = 1"
    assert psql(name, fields) == 'label,parent_id'
    caches = "SELECT string_agg(field_name || values_complete, ',' ORDER BY field_name) FROM dataset_field_stats"
    assert psql(name, caches) == 'labelfalse,parent_idfalse,tagsfalse'
    # Logged like any delete, on the primary only.
    assert _new(before, _logs(psql, name)) == {
        'blocks': ['1/0'],
        'vectors': [],
        'datasets': ['1/fields'],
        'statistics': ['1/-1/{only_on_stale}', '1/0/'],
        'duplicates': ['1/0'],
    }
    progress = result.stdout  # apply_migrations.sh shows each "0030:" line without its prefix
    assert 'dataset 1 (primary): deleted 2 objects whose image was deleted since 0029' in progress
    assert 'dataset 3 (going): 1 objects name an image deleted since 0029; a mirror' in progress
    assert 'dataset 5 (mirror): 1 objects name an image deleted since 0029; a mirror' in progress
    assert "checked every object's image in" in progress

    # The key holds (test_upgrade_paths compares the whole schema with a fresh install's).
    key = "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'samples_parent_fkey'"
    assert psql(name, key) == (
        'FOREIGN KEY (dataset_id, parent_id) REFERENCES samples(dataset_id, id) ON DELETE CASCADE'
    )
    psql(name, 'DELETE FROM samples WHERE id = 1')
    assert psql(name, 'SELECT count(*) FROM samples WHERE id = 10') == '0'


def test_0030_refuses_while_a_client_is_connected(database):
    make, psql, _, migrate = database
    name = make(_schema_sql() + BEFORE_0030 + test_upgrade_paths.SEED)
    dsn = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()

    async def while_connected():
        connection = await asyncpg.connect(dsn, server_settings={'application_name': 'freedomridge backend'})
        try:
            return migrate(name)
        finally:
            await connection.close()

    result = asyncio.run(while_connected())
    assert result.returncode != 0
    assert "0030 checks every object sample's image, so stop the app first. Still connected: freedomridge" in (
        result.stderr
    )
    assert psql(name, "SELECT count(*) FROM pg_constraint WHERE conname = 'samples_parent_fkey'") == '0'
