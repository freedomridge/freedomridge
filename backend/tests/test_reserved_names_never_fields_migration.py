"""0043 turns the metadata fields an older release registered under `id`, `dataset_id`, `filepath`, `created_at` or
`last_modified_at` back into the sample's own field, as an upgrade runs it (scripts/apply_migrations.sh).

It touches only the field registry: a row that says something about the name (a description, a unit, the private
mark) keeps that as the sample's own field's row, any other goes; the stored keys stay in their samples, untouched;
the registry's triggers mark the names for the statistics refresh and tell a followed dataset's followers, and
nothing else; a mirror's and a dataset being deleted's registry stay; each change has a line, naming any index left
over the key; with nothing to do it says so; a second run does nothing and names no dataset.
"""

import os
import re

import pytest

from backend.tests import test_upgrade_paths
from backend.tests.test_upgrade_paths import ROOT

database = test_upgrade_paths.database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires disposable PostgreSQL',
)
BEFORE = (ROOT / 'backend/db/schema.sql').read_text() + (
    "\nDELETE FROM schema_migrations WHERE filename = '0043_reserved_names_never_fields.sql';\n"
)
SEED = """
INSERT INTO datasets (id, name, followed) VALUES (1, 'farm', TRUE), (2, 'going', FALSE), (3, 'mirror', FALSE),
    (4, 'plain', FALSE);
SELECT setval(pg_get_serial_sequence('datasets', 'id'), 4);
INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES
    (1, 1, 'a.jpg', 'image', '{"created_at": "1999-03-04T05:06:07Z", "dataset_id": "patched", "n": 1}'),
    (2, 1, 'b.jpg', 'image', '{"id": 99, "n": 2}'),
    (3, 4, 'c.jpg', 'image', '{"created_at_source": "camera"}');
SELECT setval(pg_get_serial_sequence('samples', 'id'), 10);
-- What an older release registered: in the followed dataset a described private created_at, a promoted dataset_id
-- with its index, a plain id; in a dataset of its own a look-alike name, which stays a field.
INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin, is_promoted, display_type, description, unit,
                            values_private)
    VALUES (1, 'created_at', 'string', 'metadata', FALSE, 'enum', 'When the camera took it', 'UTC', TRUE),
           (1, 'dataset_id', 'string', 'metadata', TRUE, NULL, NULL, NULL, FALSE),
           (1, 'id', 'string', 'metadata', FALSE, NULL, NULL, NULL, FALSE),
           (4, 'created_at_source', 'string', 'metadata', FALSE, NULL, NULL, NULL, FALSE);
CREATE INDEX idx_samples_meta_1_dataset_id ON samples ((metadata->>'dataset_id')) WHERE dataset_id = 1;
-- A dataset being deleted and a mirror, each with such a field.
BEGIN;
SELECT set_config('fridge.writable_dataset', '2', TRUE);
INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin) VALUES (2, 'filepath', 'string', 'metadata');
COMMIT;
INSERT INTO primary_links (url, token_encrypted, identity) VALUES ('https://primary.example/api', 'x', 'cluster');
INSERT INTO mirrors (dataset_id, link_id, origin_dataset_id, state, feed_cursor)
    SELECT 3, id, 7, 'following', 1 FROM primary_links;
BEGIN;
SELECT set_config('fridge.mirror_sync', '3', TRUE);
INSERT INTO dataset_fields (dataset_id, field_name, field_type, origin)
    VALUES (3, 'last_modified_at', 'string', 'metadata');
COMMIT;
DELETE FROM dataset_stats_pending;
DELETE FROM dataset_changes;
UPDATE datasets SET deleting = TRUE WHERE id = 2;
"""
REGISTRY = (
    "SELECT format('%s:%s:%s:%s:%s:%s:%s:%s', dataset_id, field_name, field_type, origin, is_promoted, "
    "coalesce(description, '-'), coalesce(unit, '-'), values_private) FROM dataset_fields ORDER BY 1"
)
SAMPLES = 'SELECT id, xmin::text, metadata::text, last_modified_at FROM samples ORDER BY id'
UNTOUCHED = ('sample_block_changes', 'vector_changes', 'map_block_changes', 'duplicate_pending', 'jobs')


def _shown(stdout):
    return [line.strip() for line in stdout.splitlines() if line.startswith('  ')]


def test_0043_turns_the_registrations_back_and_touches_no_sample(database):
    make, psql, _, migrate = database
    name = make(BEFORE + SEED)
    samples = psql(name, SAMPLES)
    untouched = {table: psql(name, f'SELECT x::text FROM {table} x ORDER BY 1') for table in UNTOUCHED}

    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'

    assert psql(name, REGISTRY).splitlines() == [
        '1:created_at:unknown:system:f:When the camera took it:UTC:t',  # what was said about the name stays
        '2:filepath:string:metadata:f:-:-:f',  # being deleted: left
        '3:last_modified_at:string:metadata:f:-:-:f',  # a mirror's: its primary's
        '4:created_at_source:string:metadata:f:-:-:f',  # a look-alike is an ordinary field
    ]
    assert psql(name, SAMPLES) == samples  # the stored keys, and every row version, as they were
    assert psql(name, "SELECT to_regclass('idx_samples_meta_1_dataset_id') IS NOT NULL") == 't'
    pending = psql(name, "SELECT format('%s|%s|%s', dataset_id, block_id, field_names) FROM dataset_stats_pending")
    assert pending == '1|-1|{created_at,dataset_id,id}'  # the names alone, for the ordinary refresh
    assert set(psql(name, 'SELECT dataset_id || kind FROM dataset_changes').splitlines()) == {'1fields'}
    for table, rows in untouched.items():
        assert psql(name, f'SELECT x::text FROM {table} x ORDER BY 1') == rows, table

    shown = _shown(result.stdout)
    assert len(shown) == 3, shown
    assert shown[0].startswith('dataset 1 (farm): created_at is no metadata field any more')
    assert "(keeping its description, unit, private mark for the sample's own field)" in shown[0]
    assert shown[1].startswith('dataset 1 (farm): dataset_id is no metadata field any more')
    assert 'idx_samples_meta_1_dataset_id' in shown[1] and 'DROP INDEX CONCURRENTLY' in shown[1]
    assert shown[2].startswith('dataset 1 (farm): id is no metadata field any more')
    assert all("the values stay in the samples' metadata" in line for line in shown)

    # Run again, without its ledger row: nothing changes, no dataset named.
    registry = psql(name, REGISTRY)
    psql(name, "DELETE FROM schema_migrations WHERE filename = '0043_reserved_names_never_fields.sql'")
    again = migrate(name)
    assert again.returncode == 0, f'{again.stdout}\n{again.stderr}'
    assert psql(name, REGISTRY) == registry and psql(name, SAMPLES) == samples
    shown = _shown(again.stdout)
    assert len(shown) == 1 and shown[0].startswith('nothing to do') and not re.search(r'dataset \d', shown[0])


def test_0043_with_nothing_to_do_says_so(database):
    make, psql, _, migrate = database
    name = make(BEFORE + test_upgrade_paths.SEED)
    registry = psql(name, REGISTRY)
    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
    assert any(line.startswith('nothing to do') for line in _shown(result.stdout)), result.stdout
    assert psql(name, REGISTRY) == registry
