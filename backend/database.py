import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI

from .admin_bootstrap import bootstrap_admin
from .public_settings import load_settings
from .request_limits import RequestConnection, RequestPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")


async def init_connection(connection):
    """Configure JSONB codec so JSONB columns are returned as Python dicts."""
    await connection.set_type_codec('jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog')


class Database:
    def __init__(self):
        # Set in connect() during app startup (lifespan) before any request
        # handler runs, so it is treated as always-present for typing.
        self.pool: asyncpg.Pool = None

    async def connect(self):
        load_settings()
        # PER-WORKER pool sizes. With gunicorn -w N the backend can demand up to
        # N * max_size connections, which must stay below Postgres max_connections.
        # We keep a generous per-worker pool and instead RAISE the DB's connection
        # ceiling in production (docker-compose.prod.yml db: `-c max_connections=250`)
        # so 4 workers * 50 = 200 fits with headroom — rather than throttling the
        # pool. Override via env if your worker count / DB limit differ.
        min_size = int(os.getenv("DB_POOL_MIN_SIZE", "10"))
        max_size = int(os.getenv("DB_POOL_MAX_SIZE", "50"))
        pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=min_size,
            max_size=max_size,
            command_timeout=600,  # Timeout for queries (seconds)
            init=init_connection,  # Initialize JSONB codec on each connection
            connection_class=RequestConnection,
        )
        self.pool = RequestPool(pool)
        print(f"Connected to database with pool size: {min_size}-{max_size} (per worker)")

    async def disconnect(self):
        if self.pool:
            await self.pool.close()
            print("Disconnected from database")

    async def fetch(self, query, *args):
        async with self.pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def fetchrow(self, query, *args):
        async with self.pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def fetchval(self, query, *args):
        async with self.pool.acquire() as connection:
            return await connection.fetchval(query, *args)

    async def execute(self, query, *args):
        async with self.pool.acquire() as connection:
            return await connection.execute(query, *args)


db = Database()


async def _warn_missing_ann_indexes():
    """Warn about large unindexed fields without a coordinate index or a vector scan.

    Inspect bounded primary-key windows at both ends of the table, plus ANALYZE's
    field-frequency estimates for older fields in the middle. This is a startup warning,
    not an exhaustive audit: an unanalyzed field outside the windows can be missed.
    """
    try:
        fields = await db.fetch(
            """WITH bounded AS MATERIALIZED (
                   (SELECT id, field_name, vector IS NOT NULL AS has_vector
                    FROM embeddings ORDER BY id LIMIT 50001)
                   UNION
                   (SELECT id, field_name, vector IS NOT NULL AS has_vector
                    FROM embeddings ORDER BY id DESC LIMIT 50001)
               ), sampled AS (
                   SELECT field_name FROM bounded GROUP BY field_name
                   HAVING count(*) FILTER (WHERE has_vector) > 10000
               )
               SELECT field_name FROM sampled
               UNION
               SELECT frequency.field_name
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               JOIN pg_stats s ON s.schemaname = n.nspname AND s.tablename = c.relname
                              AND s.attname = 'field_name' AND NOT s.inherited
               LEFT JOIN pg_stats v ON v.schemaname = n.nspname AND v.tablename = c.relname
                                   AND v.attname = 'vector' AND NOT v.inherited
               CROSS JOIN LATERAL unnest(s.most_common_vals::text::text[], s.most_common_freqs)
                   AS frequency(field_name, fraction)
               WHERE c.oid = 'embeddings'::regclass AND c.reltuples > 100002
                 AND c.reltuples * frequency.fraction * (1 - COALESCE(v.null_frac, 0)) > 10000""",
        )
        if not fields:
            return
        # An ANN index the index advisor removed on purpose is not a missing one.
        removed = {
            row['field_name']
            for row in await db.fetch(
                """SELECT field_name FROM removed_indexes WHERE kind = 'ann' AND field_name IS NOT NULL
                   AND restored_at IS NULL AND closed_at IS NULL""",
            )
        }
        defs = [
            r['indexdef']
            for r in await db.fetch(
                """SELECT pg_get_indexdef(i.indexrelid) AS indexdef
                   FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                   JOIN pg_am am ON am.oid = c.relam
                   WHERE i.indrelid = 'embeddings'::regclass AND am.amname = 'hnsw'
                     AND i.indisvalid AND i.indisready""",
            )
        ]
        for field in fields:
            name = field['field_name']
            if name in removed:
                continue
            need_field = "field_name = '" + name.replace("'", "''") + "'"
            # A valid index on the column cast enforces its dimension on every non-NULL
            # vector in the field. No need to de-TOAST vectors just to check dimensions.
            if not any(
                need_field in d and 'vector_cosine_ops' in d and re.search(r'\(?vector\)?::vector\(\d+\)', d)
                for d in defs
            ):
                logger.warning(
                    "No HNSW index for embeddings field %r (over 10000 vectors in the sample or ANALYZE estimate) — "
                    "/search and quality endpoints will seq-scan. Build: python3 "
                    "scripts/build_ann_indexes.py --field %s",
                    name,
                    name,
                )
    except Exception as e:
        logger.warning("ANN index health-check skipped: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    try:
        await bootstrap_admin(db)
    except BaseException:
        await db.disconnect()
        raise
    await _warn_missing_ann_indexes()
    # Loud startup verdict; /api/health re-checks live so applying migrations needs no restart.
    from .schema_check import log_pending, pending_migrations, stale_functions, unknown_migrations

    log_pending(await pending_migrations(db), await stale_functions(db), await unknown_migrations(db))
    # Index advisor telemetry: this worker adds its field usage counts once a minute (backend/field_usage.py).
    from . import field_usage

    stopping = asyncio.Event()
    flusher = asyncio.create_task(field_usage.run(db.pool, stopping))
    try:
        yield
    finally:
        stopping.set()
        try:
            await asyncio.wait_for(flusher, 10)
        except Exception:
            flusher.cancel()
            logger.warning('Field usage telemetry: the last flush did not finish at shutdown')
        await db.disconnect()
