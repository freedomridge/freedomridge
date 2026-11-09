-- The change log a follower instance reads: what changed in a followed dataset, stamped with the writing
-- transaction so a feed can serve it in commit order. A dataset no follower follows (datasets.followed) logs
-- nothing, and a dataset being deleted logs only its final 'deleted'. New tables and triggers only.
--
-- Samples are logged by block (id / 4096, the statistics block) from the samples statistics trigger, where it
-- already records statistics work once per changed block and transaction, so sample writes gain no trigger and
-- no transition table. A follower re-reads a changed block and compares it with its copy, which also
-- finds the samples deleted from it.
SET LOCAL lock_timeout = '2s';

ALTER TABLE datasets ADD COLUMN followed BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE sample_block_changes (
    dataset_id INTEGER NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current(),
    block_id BIGINT NOT NULL,
    PRIMARY KEY (dataset_id, transaction_id, block_id)
);

CREATE TABLE vector_changes (
    id BIGSERIAL PRIMARY KEY,
    dataset_id INTEGER NOT NULL,
    sample_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('vector', 'hash')),
    field_name TEXT NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current()
);
CREATE INDEX vector_changes_feed ON vector_changes (dataset_id, transaction_id, id);

CREATE TABLE dataset_changes (
    id BIGSERIAL PRIMARY KEY,
    dataset_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fields', 'settings', 'views', 'deleted')),
    transaction_id BIGINT NOT NULL DEFAULT txid_current()
);
CREATE INDEX dataset_changes_feed ON dataset_changes (dataset_id, transaction_id, id);

-- A followed dataset's changed sample block, for its followers' feed. The samples statistics trigger calls it once per
-- changed block and transaction, where it records statistics work for the write (record_stats_work is also called
-- for statistics bookkeeping alone, which changes no sample).
CREATE OR REPLACE FUNCTION sample_block_changed(target_dataset INTEGER, target_block BIGINT) RETURNS VOID AS $$
BEGIN
    IF current_setting('fridge.deletion_dataset', TRUE) = target_dataset::text THEN
        RETURN;
    END IF;
    INSERT INTO sample_block_changes (dataset_id, block_id)
    SELECT target_dataset, target_block FROM datasets WHERE id = target_dataset AND followed
    ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION mark_dataset_stats_stale()
RETURNS TRIGGER AS $$
DECLARE
    affected TEXT[];
    recorded TEXT[];
    covered BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('fridge.deletion_dataset', TRUE) = OLD.dataset_id::text THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        INSERT INTO dataset_stats_pending(dataset_id, block_id)
        SELECT id, -1 FROM datasets ON CONFLICT DO NOTHING;
        RETURN NULL;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        SELECT field_names INTO recorded FROM dataset_stats_pending
        WHERE dataset_id=NEW.dataset_id AND block_id=NEW.id::bigint/4096 AND transaction_id=txid_current();
        IF FOUND THEN
            IF recorded IS NULL THEN
                covered := TRUE;
            ELSIF TG_OP = 'INSERT' THEN
                covered := COALESCE(NEW.metadata,'{}'::jsonb)-recorded = '{}'::jsonb
                    AND ARRAY['tags','media_type'] <@ recorded;
            ELSE
                covered := (COALESCE(OLD.metadata,'{}'::jsonb)-recorded)
                        IS NOT DISTINCT FROM (COALESCE(NEW.metadata,'{}'::jsonb)-recorded)
                    AND (OLD.tags IS NOT DISTINCT FROM NEW.tags OR 'tags'=ANY(recorded))
                    AND (OLD.media_type IS NOT DISTINCT FROM NEW.media_type OR 'media_type'=ANY(recorded));
            END IF;
            IF covered AND (TG_OP = 'INSERT' OR (OLD.dataset_id,OLD.id::bigint/4096)
                IS NOT DISTINCT FROM (NEW.dataset_id,NEW.id::bigint/4096)) THEN
                RETURN NULL;
            END IF;
        END IF;
        affected := ARRAY(SELECT field_name FROM dataset_fields
                          WHERE dataset_id = NEW.dataset_id AND origin = 'native_array');
        IF TG_OP = 'INSERT' THEN
            affected := affected || ARRAY(SELECT jsonb_object_keys(NEW.metadata)) || ARRAY['tags', 'media_type'];
        ELSE
            IF OLD.metadata IS DISTINCT FROM NEW.metadata THEN
                affected := affected || ARRAY(
                    SELECT key FROM jsonb_object_keys(
                        COALESCE(OLD.metadata,'{}'::jsonb) || COALESCE(NEW.metadata,'{}'::jsonb)) key
                    WHERE OLD.metadata->key IS DISTINCT FROM NEW.metadata->key);
            END IF;
            IF OLD.tags IS DISTINCT FROM NEW.tags THEN affected := affected || ARRAY['tags']; END IF;
            IF OLD.media_type IS DISTINCT FROM NEW.media_type THEN affected := affected || ARRAY['media_type']; END IF;
        END IF;
        PERFORM sample_block_changed(NEW.dataset_id, NEW.id::bigint / 4096);
        PERFORM record_stats_work(NEW.dataset_id, NEW.id::bigint / 4096, affected);
    END IF;
    IF TG_OP = 'DELETE' OR (TG_OP = 'UPDATE' AND
        (OLD.dataset_id, OLD.id::bigint / 4096) IS DISTINCT FROM (NEW.dataset_id, NEW.id::bigint / 4096)) THEN
        IF EXISTS (SELECT 1 FROM datasets WHERE id = OLD.dataset_id) THEN
            PERFORM sample_block_changed(OLD.dataset_id, OLD.id::bigint / 4096);
            PERFORM record_stats_work(OLD.dataset_id, OLD.id::bigint / 4096, NULL);
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- A vector or hash row of a sample that still exists; rows deleted with their sample are covered by its block.
CREATE OR REPLACE FUNCTION log_vector_changes() RETURNS trigger AS $$
BEGIN
    -- Most installations follow nothing: skip reading the changed rows at all.
    IF NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL
       OR NOT EXISTS (SELECT 1 FROM datasets WHERE followed) THEN
        RETURN NULL;
    END IF;
    INSERT INTO vector_changes (dataset_id, sample_id, kind, field_name)
    SELECT DISTINCT s.dataset_id, c.sample_id, TG_ARGV[0], c.field_name
    FROM changed c JOIN samples s ON s.id = c.sample_id JOIN datasets d ON d.id = s.dataset_id
    WHERE d.followed;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION log_dataset_change() RETURNS trigger AS $$
BEGIN
    -- Most installations follow nothing: skip reading the changed rows at all.
    IF NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL
       OR NOT EXISTS (SELECT 1 FROM datasets WHERE followed) THEN
        RETURN NULL;
    END IF;
    INSERT INTO dataset_changes (dataset_id, kind)
    SELECT DISTINCT c.dataset_id, TG_ARGV[0] FROM changed c JOIN datasets d ON d.id = c.dataset_id WHERE d.followed;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION log_dataset_settings() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.followed THEN
            INSERT INTO dataset_changes (dataset_id, kind) VALUES (OLD.id, 'deleted');
        END IF;
        RETURN NULL;
    END IF;
    IF NEW.followed AND (OLD.name, OLD.description, OLD.media_fields, OLD.grid_media_field, OLD.modal_media_field,
                         OLD.segmentation_config, OLD.default_view_id)
        IS DISTINCT FROM (NEW.name, NEW.description, NEW.media_fields, NEW.grid_media_field, NEW.modal_media_field,
                          NEW.segmentation_config, NEW.default_view_id) THEN
        INSERT INTO dataset_changes (dataset_id, kind) VALUES (NEW.id, 'settings');
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER embeddings_log_insert AFTER INSERT ON embeddings REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_vector_changes('vector');
CREATE TRIGGER embeddings_log_update AFTER UPDATE ON embeddings REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_vector_changes('vector');
CREATE TRIGGER embeddings_log_delete AFTER DELETE ON embeddings REFERENCING OLD TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_vector_changes('vector');
CREATE TRIGGER hashes_log_insert AFTER INSERT ON sample_hashes REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_vector_changes('hash');
CREATE TRIGGER hashes_log_update AFTER UPDATE ON sample_hashes REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_vector_changes('hash');
CREATE TRIGGER hashes_log_delete AFTER DELETE ON sample_hashes REFERENCING OLD TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_vector_changes('hash');
CREATE TRIGGER dataset_fields_log_insert AFTER INSERT ON dataset_fields REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_dataset_change('fields');
CREATE TRIGGER dataset_fields_log_update AFTER UPDATE ON dataset_fields REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_dataset_change('fields');
CREATE TRIGGER dataset_fields_log_delete AFTER DELETE ON dataset_fields REFERENCING OLD TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_dataset_change('fields');
CREATE TRIGGER views_log_insert AFTER INSERT ON views REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_dataset_change('views');
CREATE TRIGGER views_log_update AFTER UPDATE ON views REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_dataset_change('views');
CREATE TRIGGER views_log_delete AFTER DELETE ON views REFERENCING OLD TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_dataset_change('views');
CREATE TRIGGER datasets_log_settings AFTER UPDATE OR DELETE ON datasets
    FOR EACH ROW EXECUTE FUNCTION log_dataset_settings();
