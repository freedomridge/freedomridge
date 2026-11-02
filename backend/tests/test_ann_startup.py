"""Startup's ANN warning stays useful without reading every stored vector."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from backend import database


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
@pytest.mark.parametrize('index_state', ['valid', 'invalid', 'not_ready', 'wrong_dimension', 'wrong_operator'])
def test_startup_ann_check_is_bounded_and_warns_for_unusable_indexes(monkeypatch, caplog, index_state):
    async def scenario():
        conn = await asyncpg.connect(os.environ['FRIDGE_TEST_POSTGRES_DSN'])
        schema = f'ann_startup_{uuid4().hex}'
        await conn.execute(f'CREATE SCHEMA {schema}')
        try:
            await conn.execute(f'SET search_path TO {schema}, public')
            await conn.execute((Path(__file__).resolve().parents[1] / 'db/schema.sql').read_text())
            await conn.execute(
                """INSERT INTO datasets(name, owner_id) VALUES ('ann', 1);
                   INSERT INTO samples(dataset_id, filepath, media_type)
                   SELECT 1, 'ann-' || g, 'image' FROM generate_series(1, 20002) g;
                   CREATE SEQUENCE dimension_reads;
                   CREATE FUNCTION vector_dims(vector) RETURNS int LANGUAGE plpgsql STRICT AS $$
                   BEGIN
                       PERFORM nextval('dimension_reads');
                       RETURN public.vector_dims($1);
                   END $$;
                   INSERT INTO embeddings(sample_id, field_name, vector, x, y)
                   SELECT g, field, '[1,2,3]'::vector, NULL, NULL
                   FROM generate_series(1, 10050) g, unnest(ARRAY['clip', 'unregistered']) field;
                   INSERT INTO embeddings(sample_id, field_name, vector, x, y)
                   SELECT g, 'null_prefix', CASE WHEN g > 10001 THEN '[1,2,3]'::vector END, NULL, NULL
                   FROM generate_series(1, 20002) g;
                   INSERT INTO embeddings(sample_id, field_name, vector, x, y)
                   VALUES (1, 'small', '[1,2,3]', NULL, NULL);""",
            )
            # A wrong-dimension index can only exist with no matching rows. Use a null
            # expression so Postgres can build it; it still must not satisfy the check.
            expression = 'NULL::vector(4)' if index_state == 'wrong_dimension' else 'vector::vector(3)'
            operator = 'vector_l2_ops' if index_state == 'wrong_operator' else 'vector_cosine_ops'
            await conn.execute(
                f"CREATE INDEX hnsw_clip ON embeddings USING hnsw (({expression}) {operator}) "
                "WHERE field_name = 'clip'",
            )
            if index_state in ('invalid', 'not_ready'):
                column = 'indisvalid' if index_state == 'invalid' else 'indisready'
                await conn.execute(f"UPDATE pg_index SET {column} = false WHERE indexrelid = 'hnsw_clip'::regclass")
            await conn.execute(
                'DROP INDEX IF EXISTS idx_embeddings_visualization, idx_embeddings_field_coords; '
                'ALTER TABLE embeddings DROP COLUMN IF EXISTS x CASCADE, DROP COLUMN IF EXISTS y CASCADE',
            )
            # No ANALYZE or field/model registration: rare/missing fields must still be found.
            monkeypatch.setattr(database, 'db', conn)
            await database._warn_missing_ann_indexes()
            reads = await conn.fetchval('SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM dimension_reads')
            assert reads == 0, f'startup de-TOASTed vectors ({reads})'
            messages = caplog.text
            assert "field 'unregistered'" in messages
            assert "field 'null_prefix'" in messages
            assert "field 'small'" not in messages
            assert ("field 'clip'" in messages) == (index_state != 'valid')
            assert 'health-check skipped' not in messages
        finally:
            await conn.execute('SET search_path TO public')
            await conn.execute(f'DROP SCHEMA {schema} CASCADE')
            await conn.close()

    asyncio.run(scenario())


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
def test_startup_uses_bounded_primary_key_windows_and_catalog_for_toasted_middle_field(monkeypatch, caplog):
    async def scenario():
        conn = await asyncpg.connect(os.environ['FRIDGE_TEST_POSTGRES_DSN'])
        schema = f'ann_startup_{uuid4().hex}'
        queries = []

        class RecordingDB:
            async def fetch(self, query, *args):
                queries.append(query)
                return await conn.fetch(query, *args)

        await conn.execute(f'CREATE SCHEMA {schema}')
        try:
            await conn.execute(f'SET search_path TO {schema}, public')
            await conn.execute((Path(__file__).resolve().parents[1] / 'db/schema.sql').read_text())
            await conn.execute(
                """INSERT INTO datasets(name, owner_id) VALUES ('ann', 1);
                   INSERT INTO samples(dataset_id, filepath, media_type)
                   SELECT 1, 'ann-' || g, 'image' FROM generate_series(1, 132000) g;
                   INSERT INTO embeddings(sample_id, field_name, vector)
                   SELECT g, CASE WHEN g <= 60000 THEN 'first'
                                  WHEN g <= 72000 THEN 'middle' ELSE 'last' END,
                          CASE WHEN g BETWEEN 60001 AND 72000
                               THEN array_fill(0.1::real, ARRAY[512])::vector ELSE '[1]'::vector END
                   FROM generate_series(1, 132000) g;
                   DROP INDEX IF EXISTS idx_embeddings_visualization, idx_embeddings_field_coords;
                   ALTER TABLE embeddings DROP COLUMN IF EXISTS x CASCADE, DROP COLUMN IF EXISTS y CASCADE;
                   ANALYZE embeddings;""",
            )
            assert await conn.fetchval(
                "SELECT pg_relation_size(reltoastrelid) > 0 FROM pg_class WHERE oid='embeddings'::regclass",
            )
            monkeypatch.setattr(database, 'db', RecordingDB())
            await database._warn_missing_ann_indexes()
            assert "field 'middle'" in caplog.text  # outside both windows, discovered through pg_stats
            assert 'health-check skipped' not in caplog.text
            plan = json.loads(await conn.fetchval('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + queries[0]))[0]['Plan']
            assert plan.get('Shared Hit Blocks', 0) + plan.get('Shared Read Blocks', 0) < 20000
            pending = [plan]
            scans = []
            while pending:
                node = pending.pop()
                if node.get('Relation Name') == 'embeddings':
                    scans.append(node)
                pending.extend(node.get('Plans', []))
            assert len(scans) == 2
            assert all(node['Index Name'] == 'embeddings_pkey' for node in scans)
            assert all(node['Actual Rows'] <= 50001 and node['Actual Loops'] == 1 for node in scans)
        finally:
            await conn.execute('SET search_path TO public')
            await conn.execute(f'DROP SCHEMA {schema} CASCADE')
            await conn.close()

    asyncio.run(scenario())
