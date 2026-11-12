"""The migration that moves datetime fields' indexes to their instants, on a 2.9.0 database upgraded with
apply_migrations.sh: each indexed datetime field whose index is on its text is queued for a field rebuild and marked
not indexed, found from the catalog alone. It builds and drops no index, leaves number and string indexes and a
datetime index already on the instant alone, keeps 2.9.0's vector rebuild rows, and logs nothing for followers.
Then the app's rebuilds put each queued field's index on its instant, under the same name."""

import asyncio
import os
import subprocess
from types import SimpleNamespace
from urllib.parse import urlparse

import asyncpg
import pytest

from backend import index_advisor, index_jobs
from backend.database import init_connection
from backend.routers.datasets import _promoted_index_name
from backend.tests import job_helpers, test_upgrade_paths

database = test_upgrade_paths.database
ROOT = test_upgrade_paths.ROOT

pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Requires disposable PostgreSQL',
)
RELEASE = 'v2.9.0'
MIGRATIONS = sorted((ROOT / 'backend/db/migrations').glob('*.sql'))
THIS = next(path for path in MIGRATIONS if path.name.endswith('_datetime_index_rebuilds.sql'))
LONG = 'captured_at_the_camera_' + 'x' * 30  # its index name passes 63 characters: hashed
SEED = f"""
INSERT INTO datasets (id, name, followed) VALUES (1, 'upgrade-check', TRUE);
INSERT INTO samples (dataset_id, filepath, media_type, metadata)
    SELECT 1, 's3://upgrade-check/' || g || '.jpg', 'image',
           jsonb_build_object('captured', '2026-01-0' || g || 'T10:00:00+02:00', 'observed', '2026-02-0' || g,
                              '{LONG}', '2026-03-0' || g, 'score', g, 'label', 'l' || g)
    FROM generate_series(1, 5) g;
INSERT INTO dataset_fields (dataset_id, field_name, field_type, is_promoted) VALUES
    (1, 'captured', 'datetime', TRUE), (1, '{LONG}', 'datetime', TRUE), (1, 'observed', 'datetime', TRUE),
    (1, 'score', 'number', TRUE), (1, 'label', 'string', TRUE), (1, 'unindexed', 'datetime', FALSE);
CREATE INDEX idx_samples_meta_1_captured ON samples ((metadata->>'captured')) WHERE dataset_id = 1;
CREATE INDEX "{_promoted_index_name(1, LONG)}" ON samples ((metadata->>'{LONG}')) WHERE dataset_id = 1;
CREATE INDEX idx_samples_meta_1_score ON samples (((metadata->>'score')::float)) WHERE dataset_id = 1;
CREATE INDEX idx_samples_meta_1_label ON samples ((metadata->>'label')) WHERE dataset_id = 1;
INSERT INTO ann_index_rebuilds (dataset_id, field_name) VALUES (1, 'clip');
"""
INDEXES = """SELECT string_agg(format('%s %s %s', c.relname, c.oid, pg_get_indexdef(c.oid)), E'\n' ORDER BY c.relname)
             FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = 'samples'::regclass"""
FIELDS = (
    "SELECT string_agg(field_name || '=' || is_promoted, ',' ORDER BY field_name COLLATE \"C\") FROM dataset_fields"
)
QUEUE = """SELECT string_agg(format('%s/%s/%s', kind, dataset_id, field_name), ','
                            ORDER BY kind, dataset_id, field_name COLLATE "C") FROM ann_index_rebuilds"""


def test_the_migration_queues_text_indexed_datetime_fields_and_the_rebuilds_move_them_to_the_instant(database):
    make, psql, _, migrate = database
    old_schema_sql = subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{RELEASE}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = make(old_schema_sql + SEED)
    # The migrations between the release and this one, as apply_migrations.sh applies them: then a datetime field
    # indexed on its instant already (promoted after the function existed).
    recorded = set(psql(name, 'SELECT filename FROM schema_migrations').split())
    for path in MIGRATIONS:
        if path < THIS and path.name not in recorded:
            ledger = f"INSERT INTO schema_migrations (filename) VALUES ('{path.name}');"
            psql(name, f'BEGIN;\n{path.read_text()}\n{ledger}\nCOMMIT;')
    psql(
        name,
        "CREATE INDEX idx_samples_meta_1_observed ON samples ((metadata_instant_v1(metadata->>'observed'))) "
        'WHERE dataset_id = 1',
    )
    indexes, changes = psql(name, INDEXES), psql(name, 'SELECT count(*) FROM dataset_changes')

    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
    assert THIS.name in psql(name, 'SELECT filename FROM schema_migrations').split()
    assert psql(name, INDEXES) == indexes  # the same indexes, OIDs and definitions: none built or dropped
    assert psql(name, QUEUE) == f'field/1/captured,field/1/{LONG},vector/1/clip'
    assert psql(name, FIELDS) == f'captured=false,{LONG}=false,label=true,observed=true,score=true,unindexed=false'
    assert psql(name, 'SELECT count(*) FROM dataset_changes') == changes  # the followed dataset logged nothing

    async def rebuild():
        dsn = urlparse(os.environ['FRIDGE_TEST_POSTGRES_DSN'])._replace(path=f'/{name}').geturl()
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, init=init_connection)
        try:
            upgraded = SimpleNamespace(pool=pool)
            for _ in range(2):
                assert await index_jobs._queue_field_rebuild(pool) == 1
                assert (await job_helpers.finish(upgraded, index_jobs.FIELD_KIND))['state'] == 'succeeded'
            assert await index_jobs._queue_field_rebuild(pool) == 0
            return await index_advisor.collect(pool, 1)
        finally:
            await pool.close()

    snapshot = asyncio.run(rebuild())
    assert psql(name, QUEUE) == 'vector/1/clip'
    assert psql(name, FIELDS) == f'captured=true,{LONG}=true,label=true,observed=true,score=true,unindexed=false'
    owners = {index['name']: index_advisor.classify(index) for index in snapshot['indexes']}
    for field in ('captured', LONG):
        rebuilt = _promoted_index_name(1, field)
        assert owners[rebuilt] == ('field', 1, field)
        assert (
            psql(
                name,
                f"SELECT pg_get_expr(indexprs, indrelid) FROM pg_index WHERE indexrelid = '\"{rebuilt}\"'::regclass",
            )
            == f"metadata_instant_v1((metadata ->> '{field}'::text))"
        )
