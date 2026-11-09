-- A follower's side (docs/FOLLOWERS.md): the primaries it follows, and its mirrors of their datasets.
--
-- A mirror is read-only here: its samples, field registry and settings, the vector and hash fields that came from
-- the primary, and the views copied from it change only through the sync job, which sets fridge.mirror_sync to the
-- mirror's dataset id in its own transaction. Everything else about a mirror stays local and writable: vector fields
-- only this instance has, maps, statistics, concepts, access grants, storage and views made here.
--
-- The checks ride on what already runs: sample_block_changed (from the samples statistics trigger, once per changed
-- block and transaction) and the 0007 change-log triggers. An instance with no mirrors pays one indexed EXISTS per
-- block.
SET LOCAL lock_timeout = '2s';

CREATE TABLE primary_links (
    id SERIAL PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,         -- the primary's API, e.g. https://primary.example.com/api
    token_encrypted TEXT NOT NULL,    -- the follower token, encrypted with STORAGE_ENCRYPTION_KEY
    follower_name TEXT,               -- this instance's name on the primary, from its config
    identity TEXT,                    -- the primary database's identity, from its config
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_synced_at TIMESTAMPTZ,
    last_error TEXT
);

CREATE TABLE mirrors (
    dataset_id INTEGER PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    -- NULL once the link is removed here; the mirror is then a stopped, static copy.
    link_id INTEGER REFERENCES primary_links(id) ON DELETE SET NULL,
    origin_dataset_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'copying' CHECK (state IN ('copying', 'following', 'stopped')),
    tags TEXT NOT NULL DEFAULT 'primary' CHECK (tags IN ('primary', 'both')),
    views TEXT NOT NULL DEFAULT 'primary' CHECK (views IN ('primary', 'both')),
    -- Copying: the blocks left to copy, from the primary's plan. Changes during the copy come through the feed.
    copy_blocks BIGINT[] NOT NULL DEFAULT '{}',
    copy_total INTEGER NOT NULL DEFAULT 0,
    -- The feed's next `after`: start_txid - 1 until the copy is done.
    feed_cursor BIGINT NOT NULL,
    -- The primary's vector and hash fields seen so far: read-only here, and the primary's copy wins on a name clash.
    vector_fields TEXT[] NOT NULL DEFAULT '{}',
    hash_fields TEXT[] NOT NULL DEFAULT '{}',
    -- Shared views on the primary that use a concept: concepts are per instance, so those views stay there.
    concept_views TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_synced_at TIMESTAMPTZ,
    last_error TEXT
);
-- One live mirror per primary dataset; a stopped one stays beside a new copy.
CREATE UNIQUE INDEX mirrors_live_origin ON mirrors (link_id, origin_dataset_id) WHERE state <> 'stopped';

CREATE TABLE mirror_samples (
    dataset_id INTEGER NOT NULL REFERENCES mirrors(dataset_id) ON DELETE CASCADE,
    origin_id INTEGER NOT NULL,
    sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    -- An object sample's metadata.parent_id on the primary; stored here translated, or null until its parent arrives.
    origin_parent_id INTEGER,
    PRIMARY KEY (dataset_id, origin_id),
    UNIQUE (sample_id)
);
CREATE INDEX mirror_samples_parent ON mirror_samples (dataset_id, origin_parent_id) WHERE origin_parent_id IS NOT NULL;

CREATE TABLE mirror_views (
    dataset_id INTEGER NOT NULL REFERENCES mirrors(dataset_id) ON DELETE CASCADE,
    origin_view_id INTEGER NOT NULL,
    view_id INTEGER NOT NULL UNIQUE REFERENCES views(id) ON DELETE CASCADE,
    PRIMARY KEY (dataset_id, origin_view_id)
);

CREATE OR REPLACE FUNCTION require_mirror_sync(target_dataset INTEGER) RETURNS VOID AS $$
BEGIN
    IF current_setting('fridge.mirror_sync', TRUE) IS DISTINCT FROM target_dataset::text
       AND EXISTS (SELECT 1 FROM mirrors WHERE dataset_id = target_dataset) THEN
        RAISE EXCEPTION 'This dataset is a read-only mirror of a dataset on another Freedom Ridge instance: change it there'
            USING ERRCODE = '55000';
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION sample_block_changed(target_dataset INTEGER, target_block BIGINT) RETURNS VOID AS $$
BEGIN
    IF current_setting('fridge.deletion_dataset', TRUE) = target_dataset::text THEN
        RETURN;
    END IF;
    -- A mirror's samples change only through its sync (0009).
    PERFORM require_mirror_sync(target_dataset);
    INSERT INTO sample_block_changes (dataset_id, block_id)
    SELECT target_dataset, target_block FROM datasets WHERE id = target_dataset AND followed
    ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql;

-- A vector or hash row of a sample that still exists; rows deleted with their sample are covered by its block.
CREATE OR REPLACE FUNCTION log_vector_changes() RETURNS trigger AS $$
BEGIN
    IF NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    -- The primary's fields on a mirror change only through its sync; fields only this instance has stay writable.
    IF EXISTS (SELECT 1 FROM mirrors) AND EXISTS (
        SELECT 1 FROM changed c JOIN samples s ON s.id = c.sample_id JOIN mirrors m ON m.dataset_id = s.dataset_id
        WHERE c.field_name = ANY (CASE TG_ARGV[0] WHEN 'vector' THEN m.vector_fields ELSE m.hash_fields END)
          AND s.dataset_id::text IS DISTINCT FROM current_setting('fridge.mirror_sync', TRUE)
    ) THEN
        RAISE EXCEPTION 'This field came from another Freedom Ridge instance, which this dataset mirrors: change it there'
            USING ERRCODE = '55000';
    END IF;
    -- Most installations follow nothing: skip reading the changed rows at all.
    IF NOT EXISTS (SELECT 1 FROM datasets WHERE followed) THEN
        RETURN NULL;
    END IF;
    INSERT INTO vector_changes (dataset_id, sample_id, kind, field_name)
    SELECT DISTINCT s.dataset_id, c.sample_id, TG_ARGV[0], c.field_name
    FROM changed c JOIN samples s ON s.id = c.sample_id JOIN datasets d ON d.id = s.dataset_id
    WHERE d.followed;
    RETURN NULL;
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

-- The field registry of a mirror is the primary's. is_promoted stays local: it stands for an index here.
CREATE OR REPLACE FUNCTION guard_mirror_fields() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (OLD.dataset_id, OLD.field_name, OLD.field_type, OLD.origin, OLD.display_type,
                             OLD.overlay_config)
        IS NOT DISTINCT FROM (NEW.dataset_id, NEW.field_name, NEW.field_type, NEW.origin, NEW.display_type,
                              NEW.overlay_config) THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' AND current_setting('fridge.deletion_dataset', TRUE) = OLD.dataset_id::text THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        PERFORM require_mirror_sync(OLD.dataset_id);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        PERFORM require_mirror_sync(NEW.dataset_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- A view copied from the primary changes only through the sync. BEFORE, so the mapping row is still there on DELETE.
CREATE OR REPLACE FUNCTION guard_mirror_views() RETURNS trigger AS $$
BEGIN
    IF current_setting('fridge.deletion_dataset', TRUE) IS DISTINCT FROM OLD.dataset_id::text
       AND EXISTS (SELECT 1 FROM mirror_views WHERE view_id = OLD.id) THEN
        PERFORM require_mirror_sync(OLD.dataset_id);
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER dataset_fields_mirror_guard AFTER INSERT OR UPDATE OR DELETE ON dataset_fields
    FOR EACH ROW EXECUTE FUNCTION guard_mirror_fields();
CREATE TRIGGER views_mirror_guard BEFORE UPDATE OR DELETE ON views
    FOR EACH ROW EXECUTE FUNCTION guard_mirror_views();

ALTER TABLE dataset_changes DROP CONSTRAINT dataset_changes_kind_check;
ALTER TABLE dataset_changes ADD CONSTRAINT dataset_changes_kind_check
    CHECK (kind IN ('fields', 'settings', 'views', 'deleted', 'maps'));

-- The primary's maps, for its followers: coordinates are logged like samples, by field and block, so a full re-fit
-- of a 3.6M-sample map logs about 880 rows. A full re-rank (the only thing that clears rank_stale) logs a 'maps'
-- dataset change, so a follower re-ranks its copy when the primary did. On a mirror, the coordinates of the
-- primary's fields change only through the sync.
CREATE TABLE map_block_changes (
    dataset_id INTEGER NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current(),
    field_name TEXT NOT NULL,
    block_id BIGINT NOT NULL,
    PRIMARY KEY (dataset_id, transaction_id, field_name, block_id)
);

CREATE OR REPLACE FUNCTION log_map_changes() RETURNS trigger AS $$
BEGIN
    IF NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM mirrors) AND EXISTS (
        SELECT 1 FROM changed c JOIN samples s ON s.id = c.sample_id JOIN mirrors m ON m.dataset_id = s.dataset_id
        WHERE c.field_name = ANY (m.vector_fields)
          AND s.dataset_id::text IS DISTINCT FROM current_setting('fridge.mirror_sync', TRUE)
    ) THEN
        RAISE EXCEPTION 'This map came from another Freedom Ridge instance, which this dataset mirrors: change it there'
            USING ERRCODE = '55000';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM datasets WHERE followed) THEN
        RETURN NULL;
    END IF;
    INSERT INTO map_block_changes (dataset_id, field_name, block_id)
    SELECT DISTINCT s.dataset_id, c.field_name, c.sample_id::bigint / 4096
    FROM changed c JOIN samples s ON s.id = c.sample_id JOIN datasets d ON d.id = s.dataset_id
    WHERE d.followed
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION log_map_ranked() RETURNS trigger AS $$
BEGIN
    IF current_setting('fridge.deletion_dataset', TRUE) IS DISTINCT FROM NEW.dataset_id::text
       AND NOT NEW.rank_stale AND (TG_OP = 'INSERT' OR OLD.rank_stale)
       AND EXISTS (SELECT 1 FROM datasets WHERE id = NEW.dataset_id AND followed) THEN
        INSERT INTO dataset_changes (dataset_id, kind) VALUES (NEW.dataset_id, 'maps');
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER coordinates_log_insert AFTER INSERT ON embedding_coordinates REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_map_changes();
CREATE TRIGGER coordinates_log_update AFTER UPDATE ON embedding_coordinates REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_map_changes();
CREATE TRIGGER coordinates_log_delete AFTER DELETE ON embedding_coordinates REFERENCING OLD TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION log_map_changes();
CREATE TRIGGER viz_meta_log_ranked AFTER INSERT OR UPDATE ON embedding_viz_meta
    FOR EACH ROW EXECUTE FUNCTION log_map_ranked();
