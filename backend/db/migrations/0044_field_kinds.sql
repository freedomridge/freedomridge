-- Which kind of sample has been seen with which field (docs/FILTER_QUERY.md, "What the server skips"): images (every
-- sample that isn't an object) and objects. The filter reader skips a kind's rows for a field that kind has never been
-- seen with, for speed only. Three empty tables, their functions and triggers; no sample is read or written here. The
-- datasets that exist now are examined afterwards by the job runner (backend/field_kinds.py); until then every field of
-- theirs counts as seen on both kinds, and nothing is skipped (also while this file hasn't run: field_kinds.load).
-- Re-runnable: run a second time it changes nothing (CREATE TABLE IF NOT EXISTS, CREATE OR REPLACE for the functions
-- and the triggers), and the tables keep their rows.
SET LOCAL lock_timeout = '2s';

-- checking_images / checking_objects: the token of the running check of that kind, if any (field_kinds.py).
CREATE TABLE IF NOT EXISTS dataset_field_kinds (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    on_images BOOLEAN NOT NULL DEFAULT FALSE,
    on_objects BOOLEAN NOT NULL DEFAULT FALSE,
    checking_images UUID,
    checking_objects UUID,
    PRIMARY KEY (dataset_id, field_name)
);

CREATE TABLE IF NOT EXISTS dataset_field_kinds_state (
    dataset_id INTEGER PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    examined_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT clock_timestamp(),
    held_passes INTEGER NOT NULL DEFAULT 0,
    retry_after TIMESTAMP WITH TIME ZONE
);

-- xact: the noting transaction; a clearing pass forgets a note only if it had committed when the pass began.
CREATE TABLE IF NOT EXISTS dataset_field_kinds_pending (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    id BIGSERIAL,
    noted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT clock_timestamp(),
    xact XID8 NOT NULL DEFAULT pg_current_xact_id(),
    PRIMARY KEY (dataset_id, id)
);

-- Restore recovery (backend/job_queue.py) also normalizes the notes' writing transactions and restarts the field_kinds
-- jobs, whose checkpoint holds a snapshot of the cluster they ran on.
CREATE OR REPLACE FUNCTION normalize_restored_work()
RETURNS BOOLEAN AS $$
DECLARE
    previous_transaction BIGINT;
    current_transaction BIGINT;
    previous_system BIGINT;
    current_system BIGINT;
    merged_rows BIGINT;
    restored BOOLEAN;
    observed_snapshot TEXT;
BEGIN
    -- Serialize before assigning an xid. Another runner can recover while this one
    -- continues unrelated work; callers retry a busy recovery without logging an error.
    IF NOT pg_try_advisory_xact_lock(20261062,0) THEN
        RETURN FALSE;
    END IF;
    SELECT transaction_id,system_identifier INTO previous_transaction,previous_system
        FROM transaction_high_water WHERE singleton;
    SELECT system_identifier INTO current_system FROM pg_control_system();
    current_transaction := txid_current();
    -- A missing mark has unknown provenance. Conservatively normalize before re-seeding it.
    restored := previous_transaction IS NULL OR current_system<>previous_system
        OR current_transaction<previous_transaction;
    IF restored THEN
        -- Block queue writers while merging keys. No samples or embeddings are scanned.
        LOCK TABLE dataset_stats_pending,concept_embedding_pending,concept_score_pending,
            concept_score_count_deltas,duplicate_pending,embedding_concepts,dataset_field_kinds_pending
            IN SHARE ROW EXCLUSIVE MODE;
        WITH removed AS (
            -- A dataset being deleted is left alone: its guard refuses these writes, schedulers
            -- skip it, and its deletion job drains these rows by key.
            DELETE FROM dataset_stats_pending q WHERE NOT EXISTS (SELECT 1 FROM datasets d WHERE d.id=q.dataset_id AND d.deleting) RETURNING q.*
        )
        INSERT INTO dataset_stats_pending(dataset_id,block_id,transaction_id,field_names,requested_at)
        SELECT dataset_id,block_id,3,
               CASE WHEN bool_or(field_names IS NULL) THEN NULL ELSE
                   COALESCE(array_agg(DISTINCT name ORDER BY name) FILTER (WHERE name IS NOT NULL),'{}'::text[])
               END,min(requested_at)
        FROM removed LEFT JOIN LATERAL unnest(field_names) name ON TRUE GROUP BY dataset_id,block_id;
        GET DIAGNOSTICS merged_rows = ROW_COUNT;
        RAISE LOG 'Restore recovery: dataset_stats_pending merged to % rows', merged_rows;
        WITH removed AS (
            DELETE FROM concept_embedding_pending q WHERE NOT EXISTS (SELECT 1 FROM datasets d WHERE d.id=q.dataset_id AND d.deleting) RETURNING dataset_id,field_name,block_id
        )
        INSERT INTO concept_embedding_pending(dataset_id,field_name,block_id,transaction_id)
        SELECT DISTINCT dataset_id,field_name,block_id,3 FROM removed;
        GET DIAGNOSTICS merged_rows = ROW_COUNT;
        RAISE LOG 'Restore recovery: concept_embedding_pending merged to % rows', merged_rows;
        WITH removed AS (
            DELETE FROM concept_score_pending q WHERE NOT EXISTS (SELECT 1 FROM embedding_concepts c JOIN datasets d ON d.id=c.dataset_id WHERE c.id=q.concept_id AND d.deleting) RETURNING concept_id,block_id
        )
        INSERT INTO concept_score_pending(concept_id,block_id,transaction_id)
        SELECT DISTINCT concept_id,block_id,3 FROM removed;
        GET DIAGNOSTICS merged_rows = ROW_COUNT;
        RAISE LOG 'Restore recovery: concept_score_pending merged to % rows', merged_rows;
        WITH removed AS (
            DELETE FROM concept_score_count_deltas q WHERE NOT EXISTS (SELECT 1 FROM embedding_concepts c JOIN datasets d ON d.id=c.dataset_id WHERE c.id=q.concept_id AND d.deleting) RETURNING concept_id,delta
        )
        INSERT INTO concept_score_count_deltas(concept_id,transaction_id,delta)
        SELECT concept_id,3,sum(delta) FROM removed GROUP BY concept_id;
        GET DIAGNOSTICS merged_rows = ROW_COUNT;
        RAISE LOG 'Restore recovery: concept_score_count_deltas merged to % rows', merged_rows;
        WITH removed AS (
            DELETE FROM duplicate_pending q WHERE NOT EXISTS (SELECT 1 FROM datasets d WHERE d.id=q.dataset_id AND d.deleting) RETURNING dataset_id,block_id
        )
        INSERT INTO duplicate_pending(dataset_id,block_id,transaction_id)
        SELECT DISTINCT dataset_id,block_id,3 FROM removed;
        GET DIAGNOSTICS merged_rows = ROW_COUNT;
        RAISE LOG 'Restore recovery: duplicate_pending merged to % rows', merged_rows;
        -- The record of which kind of sample has which field (0044): a note's writing transaction becomes 3, visible in
        -- every snapshot, so the next clearing pass forgets it (and checks every mark first). A check's token left on a
        -- mark stays: it costs the next writer of that field one write of the mark.
        UPDATE dataset_field_kinds_pending SET xact='3';
        GET DIAGNOSTICS merged_rows = ROW_COUNT;
        RAISE LOG 'Restore recovery: dataset_field_kinds_pending normalized % rows', merged_rows;

        UPDATE embedding_concepts SET score_definition_transaction=3,
            score_manual_snapshot=NULL,score_manual_transaction=NULL
        WHERE NOT EXISTS (SELECT 1 FROM datasets d WHERE d.id=embedding_concepts.dataset_id AND d.deleting);
        observed_snapshot := txid_current_snapshot()::text;
        UPDATE jobs j SET barrier=jsonb_build_object(
            'snapshot',observed_snapshot,'stats_full_requests',d.stats_full_requests,
            'full',COALESCE((j.barrier->>'full')::boolean,FALSE)
                OR NOT COALESCE((SELECT initialized FROM dataset_stats_state WHERE dataset_id=d.id),FALSE)
                OR EXISTS(SELECT 1 FROM dataset_stats_pending WHERE dataset_id=d.id AND block_id=-1),
            'upper_id',GREATEST(COALESCE((SELECT max(id) FROM samples WHERE dataset_id=d.id),0),
                COALESCE((SELECT max(block_id)*4096+4095 FROM dataset_stats_blocks WHERE dataset_id=d.id),0)))
        FROM datasets d WHERE j.dataset_id=d.id AND j.kind='statistics' AND j.state IN ('queued','running');
        UPDATE jobs j SET barrier=jsonb_build_object(
            'snapshot',observed_snapshot,'dataset_id',c.dataset_id,'score_revision',c.score_revision,
            'bootstrap',NOT c.score_initialized OR COALESCE((j.barrier->>'bootstrap')::boolean,FALSE),
            'upper_id',GREATEST(COALESCE((SELECT max(id) FROM samples WHERE dataset_id=c.dataset_id),0),
                COALESCE((SELECT max(sample_id) FROM embedding_concept_scores WHERE concept_id=c.id),0)))
        FROM embedding_concepts c WHERE j.args->>'concept_id'=c.id::text AND j.kind='concept_scoring'
            AND j.state IN ('queued','running');
        UPDATE jobs j SET barrier=jsonb_build_object(
            'snapshot',observed_snapshot,'full',j.args ? 'config',
            'configs',COALESCE((SELECT jsonb_agg(config ORDER BY config) FROM duplicate_configs
                WHERE dataset_id=j.dataset_id),'[]'::jsonb),
            'upper_id',GREATEST(COALESCE((SELECT max(id) FROM samples WHERE dataset_id=j.dataset_id),0),
                COALESCE((SELECT max(sample_id) FROM duplicate_members
                    WHERE dataset_id=j.dataset_id AND config=j.args->>'config'),0),
                COALESCE((SELECT max(sample_id) FROM sample_dup_secondary
                    WHERE dataset_id=j.dataset_id AND hash_config=j.args->>'config'),0)))
        WHERE j.kind='duplicate_maintenance' AND j.state IN ('queued','running');
        UPDATE duplicate_configs c SET initialized=FALSE WHERE EXISTS(
            SELECT 1 FROM jobs j WHERE j.kind='duplicate_maintenance' AND j.state IN ('queued','running')
                AND j.dataset_id=c.dataset_id AND j.args->>'config'=c.config);
        UPDATE jobs SET state='queued',checkpoint='{}',progress_done=0,progress_total=NULL,
            result=NULL,error=NULL,failures=0,contentions=0,started_at=NULL,finished_at=NULL,
            lease_token=NULL,lease_expires_at=NULL,worker_id=NULL,heartbeat_at=NULL,
            available_at=clock_timestamp(),updated_at=clock_timestamp(),
            message='Restarted after database restore'
        WHERE kind IN ('statistics','concept_scoring','duplicate_maintenance','field_kinds')
            AND state IN ('queued','running');
        -- Failed/cancelled work can be resumed by a later explicit request too.
        UPDATE jobs SET barrier='{}',checkpoint='{}'
        WHERE kind IN ('statistics','concept_scoring','duplicate_maintenance','field_kinds')
            AND state IN ('failed','cancelled');
    END IF;
    INSERT INTO transaction_high_water(singleton,transaction_id,system_identifier)
    VALUES(TRUE,current_transaction,current_system)
    ON CONFLICT (singleton) DO UPDATE SET transaction_id=EXCLUDED.transaction_id,
        system_identifier=EXCLUDED.system_identifier;
    RETURN restored;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION field_kinds_mark(target_dataset INTEGER, image_names TEXT[], object_names TEXT[])
RETURNS VOID AS $$
BEGIN
    image_names := COALESCE(image_names, '{}');
    object_names := COALESCE(object_names, '{}');
    IF target_dataset IS NULL OR cardinality(image_names) + cardinality(object_names) = 0 THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock_shared(20261125, target_dataset);
    IF current_setting('transaction_isolation') = 'read committed' THEN
        image_names := ARRAY(
            SELECT DISTINCT wanted.key FROM unnest(image_names) AS wanted(key)
            WHERE NOT EXISTS (
                SELECT 1 FROM dataset_field_kinds k
                WHERE k.dataset_id = target_dataset AND k.field_name = wanted.key
                  AND k.on_images AND k.checking_images IS NULL));
        object_names := ARRAY(
            SELECT DISTINCT wanted.key FROM unnest(object_names) AS wanted(key)
            WHERE NOT EXISTS (
                SELECT 1 FROM dataset_field_kinds k
                WHERE k.dataset_id = target_dataset AND k.field_name = wanted.key
                  AND k.on_objects AND k.checking_objects IS NULL));
        IF cardinality(image_names) + cardinality(object_names) = 0 THEN
            RETURN;
        END IF;
    END IF;
    INSERT INTO dataset_field_kinds AS k (dataset_id, field_name, on_images, on_objects)
    SELECT target_dataset, wanted.key, wanted.key = ANY(image_names), wanted.key = ANY(object_names)
    FROM (SELECT DISTINCT unnest(image_names || object_names) AS key) wanted ORDER BY wanted.key
    ON CONFLICT (dataset_id, field_name) DO UPDATE
    SET on_images = k.on_images OR EXCLUDED.on_images, on_objects = k.on_objects OR EXCLUDED.on_objects,
        checking_images = CASE WHEN EXCLUDED.on_images THEN NULL ELSE k.checking_images END,
        checking_objects = CASE WHEN EXCLUDED.on_objects THEN NULL ELSE k.checking_objects END
    WHERE (EXCLUDED.on_images AND (NOT k.on_images OR k.checking_images IS NOT NULL))
       OR (EXCLUDED.on_objects AND (NOT k.on_objects OR k.checking_objects IS NOT NULL));
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION field_kinds_note(target_dataset INTEGER)
RETURNS VOID AS $$
BEGIN
    IF target_dataset IS NULL OR current_setting('fridge.deletion_dataset', TRUE) = target_dataset::text
       OR current_setting('fridge.field_kinds_noted_' || target_dataset, TRUE) = 'on' THEN
        RETURN;
    END IF;
    INSERT INTO dataset_field_kinds_pending (dataset_id)
    SELECT target_dataset WHERE EXISTS (SELECT 1 FROM datasets WHERE id = target_dataset);
    PERFORM set_config('fridge.field_kinds_noted_' || target_dataset, 'on', TRUE);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION field_kinds_inserted() RETURNS trigger AS $$
DECLARE
    target RECORD;
BEGIN
    FOR target IN
        SELECT seen.dataset_id,
               COALESCE(array_agg(seen.key ORDER BY seen.key) FILTER (WHERE NOT seen.is_object), '{}') AS images,
               COALESCE(array_agg(seen.key ORDER BY seen.key) FILTER (WHERE seen.is_object), '{}') AS objects
        FROM (
            SELECT r.dataset_id, r.media_type = 'object' AS is_object, k.key
            FROM field_kinds_new r CROSS JOIN LATERAL jsonb_object_keys(
                CASE WHEN jsonb_typeof(r.metadata) = 'object' THEN r.metadata ELSE '{}'::jsonb END) AS k(key)
            UNION
            SELECT r.dataset_id, r.media_type = 'object', '@local.' || k.key
            FROM field_kinds_new r CROSS JOIN LATERAL jsonb_object_keys(
                CASE WHEN jsonb_typeof(r.local_metadata) = 'object' THEN r.local_metadata ELSE '{}'::jsonb END) AS k(key)
            UNION
            SELECT r.dataset_id, r.media_type = 'object', 'location' FROM field_kinds_new r WHERE r.location IS NOT NULL
        ) seen
        GROUP BY seen.dataset_id
        ORDER BY seen.dataset_id
    LOOP
        PERFORM field_kinds_mark(target.dataset_id, target.images, target.objects);
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION field_kinds_keys(document JSONB) RETURNS TEXT[] AS $$
    SELECT CASE WHEN jsonb_typeof(document) = 'object' THEN ARRAY(SELECT jsonb_object_keys(document))
                ELSE '{}'::text[] END
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION field_kinds_same_keys(a JSONB, b JSONB) RETURNS BOOLEAN AS $$
    SELECT CASE WHEN jsonb_typeof(a) = 'object' THEN ARRAY(SELECT jsonb_object_keys(a)) ELSE '{}'::text[] END
         = CASE WHEN jsonb_typeof(b) = 'object' THEN ARRAY(SELECT jsonb_object_keys(b)) ELSE '{}'::text[] END
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION field_kinds_updated() RETURNS trigger AS $$
DECLARE
    is_object BOOLEAN := NEW.media_type = 'object';
    moved BOOLEAN := OLD.dataset_id IS DISTINCT FROM NEW.dataset_id
        OR (OLD.media_type = 'object') IS DISTINCT FROM (NEW.media_type = 'object');
    old_keys TEXT[] := field_kinds_keys(OLD.metadata);
    new_keys TEXT[] := field_kinds_keys(NEW.metadata);
    local_changed BOOLEAN := moved OR OLD.local_metadata IS DISTINCT FROM NEW.local_metadata;
    old_local TEXT[] := CASE WHEN local_changed THEN field_kinds_keys(OLD.local_metadata) ELSE '{}' END;
    new_local TEXT[] := CASE WHEN local_changed THEN field_kinds_keys(NEW.local_metadata) ELSE '{}' END;
    located BOOLEAN := NEW.location IS NOT NULL AND (moved OR OLD.location IS NULL);
    note TEXT := 'fridge.field_kinds_' || NEW.dataset_id || CASE WHEN is_object THEN '_objects' ELSE '_images' END;
    marked TEXT[];
    marked_local TEXT[];
    names TEXT[];
    local_names TEXT[];
BEGIN
    IF moved OR NOT (old_keys <@ new_keys) OR NOT (old_local <@ new_local)
       OR (OLD.location IS NOT NULL AND NEW.location IS NULL) THEN
        IF current_setting('fridge.field_kinds_noted_' || OLD.dataset_id, TRUE) IS DISTINCT FROM 'on' THEN
            PERFORM field_kinds_note(OLD.dataset_id);
        END IF;
    END IF;
    IF NEW.dataset_id IS NULL THEN
        RETURN NULL;  -- a sample in no dataset: nothing to mark (its old dataset was noted above)
    END IF;
    IF moved THEN
        old_keys := '{}';
        old_local := '{}';
    END IF;
    marked := COALESCE(NULLIF(current_setting(note, TRUE), ''), '{}')::text[];
    marked_local := COALESCE(NULLIF(current_setting(note || '_local', TRUE), ''), '{}')::text[];
    IF new_keys <@ (old_keys || marked) AND new_local <@ (old_local || marked_local)
       AND (NOT located OR 'location' = ANY(marked)) THEN
        RETURN NULL;
    END IF;
    names := ARRAY(SELECT key FROM unnest(new_keys) AS k(key) WHERE key <> ALL(old_keys) AND key <> ALL(marked));
    IF located AND NOT 'location' = ANY(marked) THEN
        names := names || 'location'::text;
    END IF;
    local_names := ARRAY(SELECT key FROM unnest(new_local) AS k(key) WHERE key <> ALL(old_local) AND key <> ALL(marked_local));
    IF cardinality(names) = 0 AND cardinality(local_names) = 0 THEN
        RETURN NULL;
    END IF;
    marked := marked || names;
    names := names || ARRAY(SELECT '@local.' || key FROM unnest(local_names) AS k(key));
    PERFORM field_kinds_mark(
        NEW.dataset_id, CASE WHEN is_object THEN '{}' ELSE names END, CASE WHEN is_object THEN names END);
    PERFORM set_config(note, marked::text, TRUE);
    PERFORM set_config(note || '_local', (marked_local || local_names)::text, TRUE);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION field_kinds_deleted() RETURNS trigger AS $$
BEGIN
    PERFORM field_kinds_note(gone.dataset_id)
    FROM (SELECT DISTINCT dataset_id FROM field_kinds_old) gone ORDER BY gone.dataset_id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION field_kinds_truncated() RETURNS trigger AS $$
BEGIN
    INSERT INTO dataset_field_kinds_pending (dataset_id) SELECT id FROM datasets ORDER BY id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION field_kinds_dataset_created() RETURNS trigger AS $$
BEGIN
    INSERT INTO dataset_field_kinds_state (dataset_id) VALUES (NEW.id) ON CONFLICT DO NOTHING;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION require_field_kinds(target_dataset INTEGER, image_names TEXT[], object_names TEXT[])
RETURNS BOOLEAN AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM dataset_field_kinds_state WHERE dataset_id = target_dataset)
       OR EXISTS (SELECT 1 FROM dataset_field_kinds
                  WHERE dataset_id = target_dataset AND ((on_images AND field_name = ANY(image_names))
                                                         OR (on_objects AND field_name = ANY(object_names)))) THEN
        RAISE EXCEPTION 'The fields seen on each kind of sample changed while the filter was read'
            USING ERRCODE = '40001';
    END IF;
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;

CREATE OR REPLACE TRIGGER trg_samples_field_kinds_insert
    AFTER INSERT ON samples REFERENCING NEW TABLE AS field_kinds_new
    FOR EACH STATEMENT EXECUTE FUNCTION field_kinds_inserted();
CREATE OR REPLACE TRIGGER trg_samples_field_kinds_update
    AFTER UPDATE OF metadata, local_metadata, media_type, dataset_id, location ON samples FOR EACH ROW
    WHEN (OLD.dataset_id IS DISTINCT FROM NEW.dataset_id
          OR (OLD.media_type = 'object') IS DISTINCT FROM (NEW.media_type = 'object')
          OR (OLD.location IS NULL) IS DISTINCT FROM (NEW.location IS NULL)
          OR NOT field_kinds_same_keys(OLD.metadata, NEW.metadata)
          OR (OLD.local_metadata IS DISTINCT FROM NEW.local_metadata
              AND NOT field_kinds_same_keys(OLD.local_metadata, NEW.local_metadata)))
    EXECUTE FUNCTION field_kinds_updated();
CREATE OR REPLACE TRIGGER trg_samples_field_kinds_delete
    AFTER DELETE ON samples REFERENCING OLD TABLE AS field_kinds_old
    FOR EACH STATEMENT EXECUTE FUNCTION field_kinds_deleted();
CREATE OR REPLACE TRIGGER trg_samples_field_kinds_truncate
    AFTER TRUNCATE ON samples FOR EACH STATEMENT EXECUTE FUNCTION field_kinds_truncated();
CREATE OR REPLACE TRIGGER trg_datasets_field_kinds
    AFTER INSERT ON datasets FOR EACH ROW EXECUTE FUNCTION field_kinds_dataset_created();
