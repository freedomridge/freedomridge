"""0029 gives every object sample its image in samples.parent_id, and deletes the objects whose image is already gone.

A database from the last release before it (2.10.0) holds every kind of object row there is: linked ones, objects whose
image was deleted, objects that name an image of another dataset, an object or junk, objects with no parent, a
dataset being deleted, followed datasets, and a mirror of another primary's dataset. The migration must link exactly
the resolvable ones, delete exactly the orphans (a parent_id no sample has, in any dataset) outside mirrors, log those
deletes for followers and statistics like any delete (the fill itself logs nothing), and leave a mirror's rows to its
sync. An object naming a sample of another dataset is mis-pointed, not orphaned: it is kept.
"""

import asyncio
import os
import subprocess
from urllib.parse import urlparse

import asyncpg
import pytest

from backend.tests import test_upgrade_paths
from backend.tests.test_upgrade_paths import ROOT

database = test_upgrade_paths.database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires disposable PostgreSQL',
)
RELEASE = 'v2.10.0'  # the last release without the column
SEED = """
INSERT INTO users (username, password_hash, role) VALUES ('follower:lab', 'x', 'power_user');
INSERT INTO datasets (id, name, followed) VALUES
    (1, 'primary', TRUE), (2, 'other', FALSE), (3, 'going', FALSE), (4, 'linked', TRUE), (5, 'mirror', FALSE);
SELECT setval(pg_get_serial_sequence('datasets', 'id'), 5);
INSERT INTO followers (name, user_id) SELECT 'lab', id FROM users WHERE username = 'follower:lab';
INSERT INTO follower_datasets (follower_id, dataset_id, start_txid, acked_txid)
    SELECT f.id, d.id, txid_current(), txid_current() FROM followers f, datasets d WHERE d.followed;
INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES
    (1, 1, 'a.jpg', 'image', '{}'),
    (2, 1, 'b.jpg', 'image', '{}'),
    (3, 2, 'c.jpg', 'image', '{}'),
    (10, 1, 'a.jpg#0', 'object', '{"parent_id": 1, "label": "weed"}'),
    (11, 1, 'b.jpg#0', 'object', '{"parent_id": "2", "label": "corn"}'),
    (12, 1, 'gone.jpg#0', 'object', '{"parent_id": 999, "label": "weed", "only_on_orphans": 1}'),
    (13, 1, 'c.jpg#0', 'object', '{"parent_id": 3, "label": "weed"}'),
    (14, 1, 'a.jpg#0#0', 'object', '{"parent_id": 10}'),
    (15, 1, 'junk-text', 'object', '{"parent_id": "abc"}'),
    (16, 1, 'junk-negative', 'object', '{"parent_id": -1}'),
    (17, 1, 'junk-fraction', 'object', '{"parent_id": 1.5}'),
    (18, 1, 'none-null', 'object', '{"parent_id": null}'),
    (19, 1, 'none-missing', 'object', '{"label": "standalone"}'),
    (20, 1, 'crop.png', 'image', '{"parent_id": 1}'),
    (21, 1, 'never.jpg#0', 'object', '{"parent_id": 9999999999}'),
    (22, 1, 'b.jpg#1', 'object', '{"parent_id": "0002"}'),
    (30, 3, 'd.jpg', 'image', '{}'),
    (31, 3, 'd.jpg#0', 'object', '{"parent_id": 30}'),
    (32, 3, 'gone.jpg#1', 'object', '{"parent_id": 998}'),
    (40, 4, 'e.jpg', 'image', '{}'),
    (41, 4, 'e.jpg#0', 'object', '{"parent_id": 40}');
SELECT setval(pg_get_serial_sequence('samples', 'id'), 100);
INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) VALUES (1, 10, 'clip_obj', '[0,1]'),
    (1, 12, 'clip_obj', '[1,0]'), (1, 12, 'clip_obj_patch_2x2_r0c0', '[1,1]');
INSERT INTO sample_hashes (sample_id, field_name, hash_hex, hash_bits, prefix)
    VALUES (12, 'dhash_8', 'ff00', B'1111111100000000', B'1111111100000000');
INSERT INTO dataset_fields (dataset_id, field_name, field_type) VALUES
    (1, 'label', 'string'), (1, 'only_on_orphans', 'number'), (1, 'parent_id', 'number');
INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, unique_values, values_complete) VALUES
    (1, 'label', 'string', '{corn,weed}', TRUE), (1, 'only_on_orphans', 'number', NULL, TRUE),
    (1, 'parent_id', 'number', NULL, TRUE), (1, 'tags', 'array', '{}', TRUE), (4, 'parent_id', 'number', NULL, TRUE);
-- A mirror of another primary's dataset, written as its sync writes it.
INSERT INTO primary_links (url, token_encrypted, identity) VALUES ('https://primary.example/api', 'x', 'cluster');
INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, state, feed_cursor)
    SELECT 5, id, 7, 'following', 1 FROM primary_links;
BEGIN;
SELECT set_config('fridge.mirror_sync', '5', TRUE);
INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES
    (50, 5, 'm.jpg', 'image', '{}'),
    (51, 5, 'm.jpg#0', 'object', '{"parent_id": 50}'),
    (52, 5, 'm-gone.jpg#0', 'object', '{"parent_id": 997}'),
    (53, 5, 'm.jpg#0#0', 'object', '{"parent_id": 51}'),
    (54, 5, 'm-none', 'object', '{"parent_id": null}'),
    (55, 5, 'c.jpg#1', 'object', '{"parent_id": 3}');
INSERT INTO mirror_samples (dataset_id, origin_id, sample_id) SELECT 5, 1000 + id, id FROM samples WHERE dataset_id = 5;
COMMIT;
UPDATE datasets SET deleting = TRUE WHERE id = 3;
"""
# What followers, statistics and duplicate work are told, each row as text.
LOGS = {
    'blocks': "SELECT format('%s/%s/%s', dataset_id, block_id, transaction_id) FROM sample_block_changes",
    'vectors': "SELECT format('%s/%s/%s/%s/%s', dataset_id, sample_id, kind, field_name, transaction_id) "
    'FROM vector_changes',
    'datasets': "SELECT format('%s/%s/%s', dataset_id, kind, transaction_id) FROM dataset_changes",
    'statistics': "SELECT format('%s/%s/%s/%s', dataset_id, block_id, field_names, transaction_id) "
    'FROM dataset_stats_pending',
    'duplicates': "SELECT format('%s/%s/%s', dataset_id, block_id, transaction_id) FROM duplicate_pending",
}


def _old_schema():
    return subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{RELEASE}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _logs(psql, name):
    return {key: set(filter(None, psql(name, query).splitlines())) for key, query in LOGS.items()}


def _new(before, after):
    """The rows the migration added, without their transaction id (the last part)."""
    return {key: sorted(row.rsplit('/', 1)[0] for row in after[key] - before[key]) for key in after}


def test_0029_links_resolvable_objects_and_deletes_orphans_outside_mirrors(database):
    make, psql, _, migrate = database
    name = make(_old_schema() + SEED)
    before = _logs(psql, name)
    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'

    rows = dict(
        line.split('|')
        for line in psql(name, "SELECT id, coalesce(parent_id::text, '-') FROM samples ORDER BY id").splitlines()
    )
    # Linked: an image of their own dataset, named as a number (10, 41, 51) or a string (11, 22).
    linked = {'10': '1', '11': '2', '22': '2', '41': '40', '51': '50'}  # 22 with zeros in front
    # Kept with no image: naming an object (14, 53), a sample of another dataset (13, 55), junk (15-17), nothing (18,
    # 19, 54), a mirror's orphan (52), everything of the dataset being deleted (31, 32), and the non-objects, the crop
    # (20) among them.
    unlinked = '1 2 3 13 14 15 16 17 18 19 20 30 31 32 40 50 52 53 54 55'.split()
    # Deleted: the orphans outside the mirror, naming an image that is gone (12) or an id no sample ever had (21).
    assert rows == {**linked, **dict.fromkeys(unlinked, '-')}

    # The orphans' vectors and hashes went with them; the linked object's stayed.
    assert psql(name, 'SELECT string_agg(sample_id || field_name, \',\' ORDER BY sample_id) FROM embeddings') == (
        '10clip_obj'
    )
    assert psql(name, 'SELECT count(*) FROM sample_hashes') == '0'
    # The field caches as a bulk delete leaves them: a field only the orphans had is unregistered, and the deleted
    # rows' other fields and tags lose their complete value lists. Another dataset's caches are untouched.
    fields = "SELECT string_agg(field_name, ',' ORDER BY field_name) FROM dataset_fields WHERE dataset_id = 1"
    assert psql(name, fields) == 'label,parent_id'
    caches = (
        "SELECT string_agg(dataset_id || field_name || values_complete, ',' ORDER BY dataset_id, field_name) "
        'FROM dataset_field_stats'
    )
    assert psql(name, caches) == '1labelfalse,1parent_idfalse,1tagsfalse,4parent_idtrue'

    # The deletes are logged like any sample delete: the followed dataset's changed block (a follower deletes the
    # samples it no longer finds there, with their vectors and hashes) and its field registry for followers,
    # statistics and duplicate work for the block. The fill logged nothing: dataset 4 (followed, linked only), the
    # mirror and the dataset being deleted have no new rows.
    assert _new(before, _logs(psql, name)) == {
        'blocks': ['1/0'],
        'vectors': [],
        'datasets': ['1/fields'],
        'statistics': ['1/-1/{only_on_orphans}', '1/0/'],
        'duplicates': ['1/0'],
    }

    # The objects with no image, by dataset: the kept ones above.
    assert psql(name, "SELECT pg_get_indexdef('idx_samples_unlinked_objects'::regclass)") == (
        "CREATE INDEX idx_samples_unlinked_objects ON public.samples USING btree (dataset_id) "
        "WHERE ((media_type = 'object'::text) AND (parent_id IS NULL))"
    )
    # The triggers' index is rebuilt on the digits with leading zeros trimmed (2.10's held the exact text).
    assert psql(name, "SELECT pg_get_indexdef('idx_samples_parent_id'::regclass)") == (
        "CREATE INDEX idx_samples_parent_id ON public.samples USING btree "
        "(ltrim((metadata ->> 'parent_id'::text), '0'::text)) WHERE (media_type = 'object'::text)"
    )

    progress = result.stdout
    assert 'left dataset 3 (going) alone: it is being deleted' in progress
    assert (
        'dataset 1 (primary): deleted 2 objects whose image is gone (no sample has their parent_id any more); kept, '
        'with no image: 1 naming another object, 1 naming a sample of another dataset, 3 with a parent_id that is not '
        'a sample id, 2 with no parent_id'
    ) in progress
    assert (
        'dataset 5 (mirror), a mirror: kept 1 objects whose image is gone, until its primary deletes them (a mirror '
        'changes only through its sync); kept, with no image: 1 naming another object, 1 naming a sample of another '
        'dataset, 0 with a parent_id that is not a sample id, 1 with no parent_id'
    ) in progress
    assert 'dataset 4 (linked):' not in progress  # nothing to report there


def test_0029_refuses_while_a_client_is_connected_and_names_an_array_field_in_the_way(database):
    """It holds samples until it commits, so it names what is still connected; and an array field already called
    parent_id is a column it needs. Either way nothing changes."""
    make, psql, _, migrate = database
    name = make(_old_schema() + test_upgrade_paths.SEED)
    dsn = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()

    async def while_connected():
        connection = await asyncpg.connect(dsn, server_settings={'application_name': 'freedomridge backend'})
        try:
            return migrate(name)
        finally:
            await connection.close()

    result = asyncio.run(while_connected())
    assert result.returncode != 0
    assert 'stop the app first. Still connected: freedomridge backend' in result.stderr
    psql(name, "ALTER TABLE samples ADD COLUMN parent_id TEXT[] DEFAULT '{}'")
    result = migrate(name)
    assert result.returncode != 0
    assert 'samples already has a column named parent_id (an array field)' in result.stderr
    assert psql(name, "SELECT count(*) FROM schema_migrations WHERE filename LIKE '0029%'") == '0'
    kind = "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'samples'::regclass AND attname"
    assert psql(name, kind + " = 'parent_id'") == 'text[]'  # the array field is as it was
