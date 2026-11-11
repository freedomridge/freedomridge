-- 2.9.0: embeddings is partitioned by dataset, one partition per dataset, so a search plans and locks one dataset's
-- vectors and vector indexes only (docs/UPGRADING.md, 2.9.0). Rows are routed by dataset_id, so every vector is
-- copied into a new table. The vector (HNSW) indexes aren't copied: the app builds them again per dataset after it
-- starts, and searches are exact until then.
--
-- This is the one migration that rewrites a large table itself, with the app stopped (CONTRIBUTING, Database
-- migrations, rule 3). Copying while the app ran would need every vector write and query in two versions until the
-- copy finished. So it refuses to run while anything else is connected, and checks the disk and the lock table
-- first. Like every migration it runs in one transaction: if it fails or is interrupted, nothing has changed and it
-- can run again. Its progress lines start with "0021:" (apply_migrations.sh shows them as they come).
SET LOCAL lock_timeout = '2s';

-- 1. Nothing else may write while the vectors are copied. (A health check's pg_isready shows up with no user.)
DO $$
DECLARE
    others TEXT;
BEGIN
    SELECT string_agg(DISTINCT coalesce(nullif(application_name, ''), 'a client') || ' (' || usename || ')', ', ')
      INTO others
      FROM pg_stat_activity
     WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend'
       AND usename IS NOT NULL;
    IF others IS NOT NULL THEN
        RAISE EXCEPTION '0021 copies every vector into a new table, so stop the app first. Still connected: %', others
            USING HINT = 'Stop every service but the database (backend, job runner, workers, backups), then run '
                         'apply_migrations.sh again.';
    END IF;
END $$;
-- Held until the commit, so a client that connects later (a backup's pg_dump, say) waits for the migration instead
-- of taking a lock the later steps need and failing it after the whole copy.
LOCK TABLE samples, datasets, embeddings, embedding_coordinates, embedding_projection_pending IN ACCESS EXCLUSIVE MODE;

-- 2. Room for a second copy of the vectors (heap and TOAST) with their btree indexes, the coordinates written again,
-- the WAL written meanwhile (as much again, until it levels off at max_wal_size, which is soft), and headroom: a
-- quarter of it, at least as much again up to 1 GB (the new btree keys are larger, and running out of disk while
-- writing WAL stops the server). Where the server can't run df (no superuser, a managed database), the check is
-- skipped, as the app's own disk check is.
DO $$
DECLARE
    written BIGINT;
    needed BIGINT;
    free BIGINT;
    data_dir TEXT := current_setting('data_directory');
BEGIN
    written := pg_table_size('embeddings')
        + (SELECT coalesce(sum(pg_relation_size(i.indexrelid)), 0)
             FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am a ON a.oid = c.relam
            WHERE i.indrelid = 'embeddings'::regclass AND a.amname = 'btree')
        + pg_total_relation_size('embedding_coordinates')
        + coalesce(pg_relation_size(to_regclass('idx_samples_dataset_id_id')), 0);
    needed := written
        + least(written, pg_size_bytes(current_setting('max_wal_size')))
        + greatest(written / 4, least(greatest(written, pg_size_bytes('64MB')), pg_size_bytes('1GB')));
    IF position('''' IN data_dir) > 0 THEN
        RAISE NOTICE '0021: skipped the free-space check (the data directory''s name has a quote)';
        RETURN;
    END IF;
    BEGIN
        CREATE TEMP TABLE _fridge_df (line TEXT) ON COMMIT DROP;
        EXECUTE format('COPY _fridge_df FROM PROGRAM %L', 'df -B1 --output=avail ''' || data_dir || ''' | tail -n 1');
        SELECT trim(line)::BIGINT INTO free FROM _fridge_df LIMIT 1;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE '0021: skipped the free-space check: %', SQLERRM;
        RETURN;
    END;
    IF free < needed THEN
        RAISE EXCEPTION '0021 needs about % free on the database''s disk while it copies the vectors; % is free',
            pg_size_pretty(needed), pg_size_pretty(free)
            USING HINT = 'Free some space, or move the database to a larger disk, then run apply_migrations.sh again.';
    END IF;
    RAISE NOTICE '0021: % free on the database''s disk, about % needed', pg_size_pretty(free), pg_size_pretty(needed);
END $$;

-- 3. One transaction holds every partition's locks until it commits: 19 per dataset, measured (the partition, its TOAST
-- table and index, its keys, the FKs' triggers, its ANALYZE). The lock table has room for max_locks_per_transaction
-- per connection: about 270 datasets at PostgreSQL's defaults (64 × 100), 670 with the repository's 250 connections.
DO $$
DECLARE
    datasets_count BIGINT := (SELECT count(*) FROM datasets);
    room BIGINT := current_setting('max_locks_per_transaction')::BIGINT
        * (current_setting('max_connections')::BIGINT + current_setting('max_prepared_transactions')::BIGINT);
    per_dataset CONSTANT INTEGER := 20;
BEGIN
    IF datasets_count * per_dataset > room * 0.8 THEN
        RAISE EXCEPTION '0021 makes one partition per dataset in one transaction, and % datasets need more locks '
                        'than this server''s lock table holds', datasets_count
            USING HINT = format('Set max_locks_per_transaction to at least %s in postgresql.conf, restart the '
                                'database, and run apply_migrations.sh again.',
                                ceil(datasets_count * per_dataset / 0.8
                                     / (current_setting('max_connections')::NUMERIC
                                        + current_setting('max_prepared_transactions')::NUMERIC)));
    END IF;
END $$;

-- 4. The old tables move aside. Their vector indexes go with them; the dataset and field of each are kept, so the
-- app builds them again per dataset (index_jobs).
CREATE TABLE ann_index_rebuilds (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    PRIMARY KEY (dataset_id, field_name)
);
INSERT INTO ann_index_rebuilds (dataset_id, field_name)
SELECT DISTINCT s.dataset_id, e.field_name
  FROM embeddings e JOIN samples s ON s.id = e.sample_id
 WHERE e.vector IS NOT NULL
   AND e.field_name IN (
       SELECT substring(pg_get_expr(i.indpred, i.indrelid) FROM $re$^\(field_name = '([^']+)'::text\)$$re$)
         FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am a ON a.oid = c.relam
        WHERE i.indrelid = 'embeddings'::regclass AND a.amname = 'hnsw' AND i.indisvalid);

ALTER TABLE embedding_coordinates DROP CONSTRAINT embedding_coordinates_sample_id_field_name_fkey;
ALTER TABLE embedding_projection_pending DROP CONSTRAINT embedding_projection_pending_sample_id_field_name_fkey;
ALTER TABLE embeddings RENAME TO embeddings_old;
ALTER INDEX embeddings_pkey RENAME TO embeddings_old_pkey;
ALTER INDEX embeddings_sample_id_field_name_key RENAME TO embeddings_old_sample_id_field_name_key;
ALTER SEQUENCE embeddings_id_seq RENAME TO embeddings_old_id_seq;
-- NOT NULL constraints are named, and a new one would get a "1" if the old names were still taken.
DO $$
DECLARE
    old_name TEXT;
BEGIN
    FOR old_name IN SELECT conname FROM pg_constraint WHERE conrelid = 'embeddings_old'::regclass AND contype = 'n' LOOP
        EXECUTE format('ALTER TABLE embeddings_old RENAME CONSTRAINT %I TO %I', old_name,
                       regexp_replace(old_name, '^embeddings_', 'embeddings_old_'));
    END LOOP;
END $$;

-- 5. The new table, bare: its keys, FK and triggers come after the copy (step 7), which loads faster and keeps the
-- triggers out of it. One partition per dataset.
CREATE TABLE embeddings (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    sample_id INTEGER NOT NULL,
    field_name TEXT NOT NULL DEFAULT 'clip',
    vector vector,
    x FLOAT,
    y FLOAT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    dataset_id INTEGER NOT NULL
) PARTITION BY LIST (dataset_id);

CREATE OR REPLACE FUNCTION create_embeddings_partition(p_schema TEXT, p_dataset_id INTEGER) RETURNS void
SET lock_timeout = '500ms' AS $$
DECLARE
    partition TEXT := format('%I.%I', p_schema, 'embeddings_d' || p_dataset_id);
BEGIN
    IF to_regclass(partition) IS NULL THEN
        EXECUTE format('CREATE TABLE %s (LIKE %I.embeddings)', partition, p_schema);
    ELSIF EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = to_regclass(partition)) THEN
        RETURN;
    END IF;
    -- ATTACH locks the parent and the tables its foreign keys tie the partition to (samples, which it references;
    -- coordinates and pending rows, which reference it). They are taken first, in one order, each waiting in the
    -- queue (so writers that keep overlapping can't starve it) for at most lock_timeout, which is under
    -- deadlock_timeout. Whoever waits on a lock held here started waiting after this did, so this times out first in
    -- any cycle (a writer taking them in the other order, an index build on samples waiting for this transaction): it
    -- fails with lock_not_available, its transaction rolls back holding nothing, and the caller tries again.
    EXECUTE format('LOCK TABLE %I.embeddings IN SHARE UPDATE EXCLUSIVE MODE', p_schema);
    EXECUTE format('LOCK TABLE %1$I.samples, %1$I.embedding_coordinates, %1$I.embedding_projection_pending '
                   'IN SHARE ROW EXCLUSIVE MODE', p_schema);
    EXECUTE format('ALTER TABLE %I.embeddings ATTACH PARTITION %s FOR VALUES IN (%s)', p_schema, partition, p_dataset_id);
    -- A TRUNCATE of one partition fires only that partition's triggers, so each carries the follower guard (0019).
    EXECUTE format('CREATE OR REPLACE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON %s '
                   'FOR EACH STATEMENT EXECUTE FUNCTION %I.refuse_followed_truncate()', partition, p_schema);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION datasets_create_embeddings_partition() RETURNS trigger AS $$
BEGIN
    PERFORM create_embeddings_partition(TG_TABLE_SCHEMA, NEW.id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_datasets_embeddings_partition
    AFTER INSERT ON datasets FOR EACH ROW EXECUTE FUNCTION datasets_create_embeddings_partition();


DO $$ BEGIN PERFORM create_embeddings_partition(current_schema(), id) FROM datasets ORDER BY id; END $$;

-- 6. The copy, dataset by dataset, straight into each partition. No trigger may exist yet: with the parent's
-- triggers in place, every copied vector would be new work: a change in the followers' log (every follower fetches
-- it again), a row in embedding_projection_pending (the whole map projected again) and concept scoring for all of it.
-- A vector with no sample (sample_id was nullable) is left out.
DO $$
DECLARE
    dataset RECORD;
    copied BIGINT;
    done BIGINT := 0;
    total BIGINT := (SELECT reltuples FROM pg_class WHERE oid = 'embeddings_old'::regclass);
    began TIMESTAMPTZ;
    orphans BIGINT;
BEGIN
    IF total < 1 THEN  -- never analyzed
        total := (SELECT count(*) FROM embeddings_old);
    END IF;
    -- Measured: 9.7M vectors copied in 355 s, and their map points in 181 s more.
    RAISE NOTICE '0021: copying about % vectors (%): about % min, the app stopped meanwhile',
        total, pg_size_pretty(pg_table_size('embeddings_old')), greatest(1, ceil(total / 17000.0 / 60));
    FOR dataset IN SELECT id, name FROM datasets ORDER BY id LOOP
        began := clock_timestamp();
        EXECUTE format(
            'INSERT INTO %I (id, sample_id, field_name, vector, x, y, created_at, dataset_id) OVERRIDING SYSTEM VALUE
             SELECT e.id, e.sample_id, e.field_name, e.vector, e.x, e.y, e.created_at, s.dataset_id
               FROM embeddings_old e JOIN samples s ON s.id = e.sample_id WHERE s.dataset_id = $1',
            'embeddings_d' || dataset.id
        ) USING dataset.id;
        GET DIAGNOSTICS copied = ROW_COUNT;
        done := done + copied;
        IF copied > 0 THEN
            EXECUTE format('ANALYZE %I', 'embeddings_d' || dataset.id);
            RAISE NOTICE '0021: copied % vectors of dataset % (%) in % s; % %% done', copied, dataset.id, dataset.name,
                round(extract(epoch FROM clock_timestamp() - began)::numeric, 1),
                CASE WHEN total > 0 THEN least(100, round(100.0 * done / total)) ELSE 100 END;
        END IF;
    END LOOP;
    SELECT count(*) INTO orphans FROM embeddings_old WHERE sample_id IS NULL;
    IF orphans > 0 THEN
        RAISE NOTICE '0021: left out % vectors that belonged to no sample', orphans;
    END IF;
    IF done + orphans <> (SELECT count(*) FROM embeddings_old) THEN
        RAISE EXCEPTION '0021 copied % vectors and left out % of the % there were; nothing was changed',
            done, orphans, (SELECT count(*) FROM embeddings_old)
            USING HINT = 'Something wrote vectors during the copy, or a vector''s sample has no dataset. Run '
                         'apply_migrations.sh again with nothing else connected; if it repeats, report it with this '
                         'message.';
    END IF;
END $$;
-- New ids continue after the old sequence, not after the newest vector left: a manual concept's "Score new" reads
-- ids above its watermark, which is past max(id) when the newest vectors were deleted since.
DO $$
DECLARE
    last_given BIGINT := (SELECT CASE WHEN is_called THEN last_value ELSE last_value - 1 END
                            FROM embeddings_old_id_seq);
    next_after BIGINT := greatest(coalesce((SELECT max(id) FROM embeddings), 0), last_given);
BEGIN
    IF next_after > 0 THEN
        PERFORM setval(pg_get_serial_sequence('embeddings', 'id'), next_after);
    END IF;
END $$;
DROP TABLE embeddings_old;

-- 7. Keys, built once over the copied rows, then the FKs, each checked with one join rather than row by row.
-- embeddings' FK names the sample's dataset too, so samples' (dataset_id, id) index becomes unique.
DO $$
DECLARE
    began TIMESTAMPTZ := clock_timestamp();
BEGIN
    ALTER TABLE embeddings ADD PRIMARY KEY (dataset_id, id);
    ALTER TABLE embeddings ADD UNIQUE (dataset_id, sample_id, field_name);
    DROP INDEX IF EXISTS idx_samples_dataset_id_id;
    CREATE UNIQUE INDEX idx_samples_dataset_id_id ON samples(dataset_id, id);
    ALTER TABLE embeddings
        ADD FOREIGN KEY (dataset_id, sample_id) REFERENCES samples (dataset_id, id) ON DELETE CASCADE;
    RAISE NOTICE '0021: built the keys and checked the vectors'' samples in % s',
        round(extract(epoch FROM clock_timestamp() - began)::numeric, 1);
END $$;

-- 8. Coordinates name their dataset, for the FK into its partition. Writing it isn't new work for the map's ranks or
-- for followers, so their triggers are off for it.
DO $$
DECLARE
    began TIMESTAMPTZ := clock_timestamp();
BEGIN
    ALTER TABLE embedding_coordinates ADD COLUMN dataset_id INTEGER;
    ALTER TABLE embedding_coordinates DISABLE TRIGGER USER;
    UPDATE embedding_coordinates c SET dataset_id = s.dataset_id FROM samples s WHERE s.id = c.sample_id;
    ALTER TABLE embedding_coordinates ENABLE TRIGGER USER;
    ALTER TABLE embedding_coordinates ALTER COLUMN dataset_id SET NOT NULL;
    ALTER TABLE embedding_coordinates
        ADD FOREIGN KEY (dataset_id, sample_id, field_name) REFERENCES embeddings(dataset_id, sample_id, field_name)
            ON DELETE CASCADE;
    ALTER TABLE embedding_projection_pending
        ADD FOREIGN KEY (dataset_id, sample_id, field_name) REFERENCES embeddings(dataset_id, sample_id, field_name)
            ON DELETE CASCADE;
    RAISE NOTICE '0021: gave the map coordinates their datasets in % s',
        round(extract(epoch FROM clock_timestamp() - began)::numeric, 1);
END $$;

CREATE OR REPLACE FUNCTION embedding_coordinates_fill_dataset() RETURNS trigger AS $$
BEGIN
    SELECT dataset_id INTO NEW.dataset_id FROM samples WHERE id = NEW.sample_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_embedding_coordinates_fill_dataset
    BEFORE INSERT ON embedding_coordinates FOR EACH ROW WHEN (NEW.dataset_id IS NULL)
    EXECUTE FUNCTION embedding_coordinates_fill_dataset();

-- 9. embeddings' triggers, as schema.sql makes them (the old table's went with it).
CREATE TRIGGER trg_embeddings_projection_insert
    AFTER INSERT ON embeddings REFERENCING NEW TABLE AS projection_new
    FOR EACH STATEMENT EXECUTE FUNCTION embeddings_queue_projection();
CREATE TRIGGER trg_embeddings_projection_update
    AFTER UPDATE ON embeddings REFERENCING OLD TABLE AS projection_old NEW TABLE AS projection_new
    FOR EACH STATEMENT EXECUTE FUNCTION embeddings_queue_projection();
CREATE TRIGGER trg_embeddings_reject_coordinate_update
    BEFORE UPDATE OF x, y ON embeddings
    FOR EACH ROW EXECUTE FUNCTION embeddings_reject_legacy_coordinates();
CREATE TRIGGER trg_embeddings_reject_coordinate_insert
    BEFORE INSERT ON embeddings FOR EACH ROW
    WHEN (NEW.x IS NOT NULL OR NEW.y IS NOT NULL)
    EXECUTE FUNCTION embeddings_reject_legacy_coordinates();
CREATE OR REPLACE TRIGGER concept_embeddings_inserted
AFTER INSERT ON embeddings REFERENCING NEW TABLE AS inserted_embeddings
FOR EACH STATEMENT EXECUTE FUNCTION record_concept_score_work();
CREATE OR REPLACE TRIGGER concept_embeddings_deleted
AFTER DELETE ON embeddings REFERENCING OLD TABLE AS deleted_embeddings
FOR EACH STATEMENT EXECUTE FUNCTION record_concept_score_work();
CREATE OR REPLACE TRIGGER concept_embeddings_updated
AFTER UPDATE OF sample_id,field_name,vector ON embeddings FOR EACH ROW
EXECUTE FUNCTION record_concept_score_update();
CREATE OR REPLACE TRIGGER concept_embeddings_truncated
AFTER TRUNCATE ON embeddings FOR EACH STATEMENT EXECUTE FUNCTION invalidate_concept_scores();
CREATE TRIGGER embeddings_log_insert AFTER INSERT ON embeddings REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_vector_changes('vector');
CREATE TRIGGER embeddings_log_update AFTER UPDATE ON embeddings REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_vector_changes('vector');
CREATE TRIGGER embeddings_log_delete AFTER DELETE ON embeddings REFERENCING OLD TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_vector_changes('vector');
-- The deletion guard, as schema.sql installs it on every dataset table.
CREATE TRIGGER deletion_guard_insert AFTER INSERT ON embeddings REFERENCING NEW TABLE AS deletion_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION guard_dataset_rows('sample');
CREATE TRIGGER deletion_guard_update_scope BEFORE UPDATE ON embeddings
    FOR EACH STATEMENT EXECUTE FUNCTION guard_dataset_update_scope();
CREATE TRIGGER deletion_guard_update BEFORE UPDATE ON embeddings FOR EACH ROW
    WHEN (OLD.sample_id IS DISTINCT FROM NEW.sample_id OR current_setting('fridge.guard_parent_rows', TRUE) = 'true')
    EXECUTE FUNCTION guard_dataset_update('sample');
CREATE TRIGGER deletion_guard_delete AFTER DELETE ON embeddings REFERENCING OLD TABLE AS deletion_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION guard_dataset_rows('sample');
-- The follower truncate guard (0019) left with the old table: the new one gets it, and each partition its own.
CREATE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON embeddings
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_followed_truncate();

-- Autovacuum never analyzes a partitioned table itself.
ANALYZE embeddings;
