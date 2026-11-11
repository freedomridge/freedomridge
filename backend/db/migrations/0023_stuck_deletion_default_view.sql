-- A dataset sealed for deletion before 2.9.0 kept its default view. Its deletion job removes the view's row, the
-- foreign key (datasets.default_view_id ON DELETE SET NULL) then updates the sealed dataset row, and
-- guard_dataset_row refused that update: the deletion failed there at every resume, and nothing could change the
-- row. 2.9.0 clears the default view at the seal, which frees new deletions only. Here the deletion's own fenced
-- transaction (fridge.deletion_dataset names the dataset) may make that one update, setting default_view_id to NULL
-- and changing nothing else, and log_dataset_settings neither refuses it on a mirror nor logs it for followers.
-- Resuming a stuck deletion then finishes it. No rows change here.
SET LOCAL lock_timeout = '2s';

CREATE OR REPLACE FUNCTION guard_dataset_row()
RETURNS TRIGGER AS $$
BEGIN
    -- Except one update, in the deletion's own fenced transaction: removing the default view's row makes its foreign
    -- key set default_view_id to NULL, and change nothing else. A dataset sealed before 2.9.0 kept its default view,
    -- and its deletion failed there at every resume (0023).
    IF TG_OP <> 'UPDATE' OR current_setting('fridge.deletion_dataset', TRUE) IS DISTINCT FROM OLD.id::text
       OR NEW.default_view_id IS NOT NULL
       OR (to_jsonb(NEW) - 'default_view_id') <> (to_jsonb(OLD) - 'default_view_id') THEN
        PERFORM require_writable_dataset(OLD.id, TG_OP = 'DELETE');
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.deleting THEN
        -- The seal: later writes in this transaction must check again.
        PERFORM set_config('fridge.writable_dataset', '', TRUE);
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION log_dataset_settings() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- Its subscriptions go with it (ON DELETE CASCADE), so nobody could read or acknowledge its log rows: they go too.
        DELETE FROM sample_block_changes WHERE dataset_id = OLD.id;
        DELETE FROM vector_changes WHERE dataset_id = OLD.id;
        DELETE FROM dataset_changes WHERE dataset_id = OLD.id;
        DELETE FROM map_block_changes WHERE dataset_id = OLD.id;
        RETURN NULL;
    END IF;
    -- The deletion's own change, its foreign key clearing the default view (guard_dataset_row), is no mirror's write
    -- and no news for followers: the dataset is going (0023).
    IF current_setting('fridge.deletion_dataset', TRUE) = NEW.id::text THEN
        RETURN NULL;
    END IF;
    IF (OLD.name, OLD.description, OLD.media_fields, OLD.grid_media_field, OLD.modal_media_field,
        OLD.segmentation_config, OLD.default_view_id)
        IS DISTINCT FROM (NEW.name, NEW.description, NEW.media_fields, NEW.grid_media_field, NEW.modal_media_field,
                          NEW.segmentation_config, NEW.default_view_id) THEN
        PERFORM require_mirror_sync(NEW.id);
        IF NEW.followed THEN
            INSERT INTO dataset_changes (dataset_id, kind) VALUES (NEW.id, 'settings');
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
