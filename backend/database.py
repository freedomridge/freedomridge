import asyncio
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI

from .admin_bootstrap import bootstrap_admin
from .public_settings import load_settings
from .request_limits import RequestConnection, RequestPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")


# Each statement is planned for its own values. A generic plan, which a prepared statement switches to after five runs
# and foreign-key checks use too, plans and locks every partition of embeddings and their indexes (0021): a map's
# coordinate writes check their foreign key into embeddings row by row. The database has the same default
# (schema.sql); the app sets it on its own connections as well. The cost: row triggers are planned for every row too,
# about 30 µs a row on a bulk insert into samples (150,000 rows: 4 s with generic plans, 9 s, measured).
PLANNING = {'plan_cache_mode': 'force_custom_plan'}


def application_name() -> str:
    """How this process's connections show in pg_stat_activity: "freedomridge job_service", say, so a migration that
    needs the app stopped (0021) can name what's still running."""
    process = os.path.splitext(os.path.basename(sys.argv[0] or ''))[0] or 'python'
    return f'freedomridge {process}'[:63]


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
            server_settings={'application_name': application_name(), **PLANNING},
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
    """Warn about a dataset's large vector field without its HNSW index.

    Each dataset's vectors are a partition (0021). ANALYZE's field-frequency estimates cover a partition's
    fields; one changed by more than the threshold since its last ANALYZE (or never analyzed) is also read at
    both ends of its primary key. This is a startup warning, not an exhaustive audit: an unanalyzed field
    outside the windows can be missed.
    """
    try:
        parts = await db.fetch(
            """SELECT d.id AS dataset_id, c.relname, n.nspname, c.reltuples,
                      (st.last_analyze IS NULL AND st.last_autoanalyze IS NULL)
                          OR COALESCE(st.n_mod_since_analyze, 0) > 10000 AS stale
               FROM datasets d JOIN pg_class c ON c.oid = to_regclass('embeddings_d' || d.id)
               JOIN pg_namespace n ON n.oid = c.relnamespace
               LEFT JOIN pg_stat_user_tables st ON st.relid = c.oid""",
        )
        fields = {
            (row['dataset_id'], row['field_name'])
            for row in await db.fetch(
                """SELECT p.dataset_id, frequency.field_name
                   FROM unnest($1::int[], $2::text[], $3::text[], $4::float8[])
                       AS p(dataset_id, relname, nspname, reltuples)
                   JOIN pg_stats s ON s.schemaname = p.nspname AND s.tablename = p.relname
                                  AND s.attname = 'field_name' AND NOT s.inherited
                   LEFT JOIN pg_stats v ON v.schemaname = p.nspname AND v.tablename = p.relname
                                       AND v.attname = 'vector' AND NOT v.inherited
                   CROSS JOIN LATERAL unnest(s.most_common_vals::text::text[], s.most_common_freqs)
                       AS frequency(field_name, fraction)
                   WHERE p.reltuples > 100002
                     AND p.reltuples * frequency.fraction * (1 - COALESCE(v.null_frac, 0)) > 10000""",
                [part['dataset_id'] for part in parts],
                [part['relname'] for part in parts],
                [part['nspname'] for part in parts],
                [part['reltuples'] for part in parts],
            )
        }
        # A stale partition is read at both ends of its key, one dataset per statement with its id as a value: a
        # join on the dataset column would plan and lock every partition at once.
        for part in parts:
            if not part['stale']:
                continue
            for row in await db.fetch(
                """SELECT field_name FROM (
                       (SELECT e.id, e.field_name, e.vector IS NOT NULL AS has_vector
                        FROM embeddings e WHERE e.dataset_id = $1 ORDER BY e.id LIMIT 50001)
                       UNION
                       (SELECT e.id, e.field_name, e.vector IS NOT NULL AS has_vector
                        FROM embeddings e WHERE e.dataset_id = $1 ORDER BY e.id DESC LIMIT 50001)
                   ) w GROUP BY field_name HAVING count(*) FILTER (WHERE has_vector) > 10000""",
                part['dataset_id'],
            ):
                fields.add((part['dataset_id'], row['field_name']))
        if not fields:
            return
        # An ANN index the index advisor removed on purpose is not a missing one.
        removed = {
            (row['dataset_id'], row['field_name'])
            for row in await db.fetch(
                """SELECT dataset_id, field_name FROM removed_indexes WHERE kind = 'ann' AND field_name IS NOT NULL
                   AND restored_at IS NULL AND closed_at IS NULL""",
            )
        }
        defs: dict = {}
        for r in await db.fetch(
            """SELECT h.inhrelid::regclass::text AS partition, pg_get_indexdef(i.indexrelid) AS indexdef
               FROM pg_inherits h JOIN pg_index i ON i.indrelid = h.inhrelid
               JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam
               WHERE h.inhparent = 'embeddings'::regclass AND am.amname = 'hnsw'
                 AND i.indisvalid AND i.indisready""",
        ):
            defs.setdefault(r['partition'].rsplit('.', 1)[-1], []).append(r['indexdef'])
        for dataset_id, name in sorted(fields):
            if (dataset_id, name) in removed:
                continue
            need_field = "field_name = '" + name.replace("'", "''") + "'"
            # A valid index on the column cast enforces its dimension on every non-NULL
            # vector in the field. No need to de-TOAST vectors just to check dimensions.
            if not any(
                need_field in d and 'vector_cosine_ops' in d and re.search(r'\(?vector\)?::vector\(\d+\)', d)
                for d in defs.get(f'embeddings_d{dataset_id}', [])
            ):
                logger.warning(
                    "No HNSW index for dataset %s's embeddings field %r (over 10000 vectors in the sample or ANALYZE "
                    "estimate) — /search and quality endpoints will seq-scan. Build: python3 "
                    "scripts/build_ann_indexes.py --dataset %s --field %s",
                    dataset_id,
                    name,
                    dataset_id,
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
