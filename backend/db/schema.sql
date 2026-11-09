-- Enable extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

-- Users table
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    email TEXT,
    role TEXT NOT NULL DEFAULT 'guest',
    password_hash TEXT,
    must_change_password BOOLEAN DEFAULT false,
    is_active BOOLEAN DEFAULT true,
    is_demo_account BOOLEAN DEFAULT false,
    last_login TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin', 'power_user', 'guest'));
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active) WHERE is_active = true;

-- Datasets table
CREATE TABLE IF NOT EXISTS datasets (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    deleting BOOLEAN NOT NULL DEFAULT FALSE,
    deletion_job_id UUID,
    description TEXT,
    media_fields TEXT[] DEFAULT '{filepath}',
    grid_media_field TEXT DEFAULT 'filepath',    -- Default media field for grid view
    modal_media_field TEXT DEFAULT 'filepath',   -- Default media field for sample detail view
    segmentation_config JSONB DEFAULT '{}'::jsonb,  -- Mask targets and color schemes
    stats_last_updated TIMESTAMP,
    stats_need_refresh BOOLEAN DEFAULT TRUE,
    stats_watermark_id BIGINT NOT NULL DEFAULT 0,        -- highest sample id folded into stats (incremental refresh)
    stats_full_refreshed_at TIMESTAMP,                   -- last full recompute (drives full-vs-incremental)
    stats_full_requests BIGINT NOT NULL DEFAULT 0,       -- bumped by each write that asks for a full recompute
    storage_config_id INTEGER,  -- FK to storage_configs(id) added below; defined here without FK
                                -- because storage_configs is declared later in this file
    owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_global BOOLEAN DEFAULT FALSE,  -- private: admins, the owner and granted users
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    default_view_id INTEGER,  -- the saved view the app opens on; FK to views(id) added below, after views
    followed BOOLEAN NOT NULL DEFAULT FALSE  -- a follower instance follows it: it logs its changes
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_datasets_active_name ON datasets(name) WHERE NOT deleting;

-- Per-user dataset access grants (only consulted when datasets.is_global = FALSE).
CREATE TABLE IF NOT EXISTS dataset_access (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    granted_by_user_id INTEGER REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dataset_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_dataset_access_user ON dataset_access(user_id);

-- Samples table (images)
CREATE TABLE IF NOT EXISTS samples (
    id SERIAL PRIMARY KEY,
    dataset_id INTEGER REFERENCES datasets(id) ON DELETE CASCADE,
    filepath TEXT NOT NULL,
    media_type TEXT NOT NULL,
    tags TEXT[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}'::jsonb,
    location GEOMETRY(Point, 4326), -- PostGIS Point (Longitude, Latitude)
    heading FLOAT, -- Heading in degrees (optional)
    -- Stored lon/lat for fast map clustering (index-only arithmetic instead of per-row
    -- PostGIS). Kept in lockstep with `location` by trg_samples_set_lonlat below — NOT a
    -- cache. See docs/MAP_REQUIREMENTS.md.
    lon DOUBLE PRECISION,
    lat DOUBLE PRECISION,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_modified_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_by_user_id INTEGER REFERENCES users(id),
    modified_by_user_id INTEGER REFERENCES users(id),
    -- Named explicitly so add_samples' duplicate handler
    -- — which checks for 'samples_dataset_filepath_unique' in the violation text —
    -- works on a fresh schema.sql-initialised prod DB too. With the auto-generated
    -- name, on_duplicate='error' fell through to a 500 that aborted the batch.
    CONSTRAINT samples_dataset_filepath_unique UNIQUE (dataset_id, filepath)
);

-- Maintain lon/lat from `location` in-transaction on every write — zero
-- staleness, no refresh job. Fires on INSERT and on UPDATEs that touch location.
CREATE OR REPLACE FUNCTION samples_set_lonlat() RETURNS trigger AS $$
BEGIN
    IF NEW.location IS NULL THEN
        NEW.lon := NULL;
        NEW.lat := NULL;
    ELSE
        NEW.lon := ST_X(NEW.location::geometry);
        NEW.lat := ST_Y(NEW.location::geometry);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_samples_set_lonlat ON samples;
CREATE TRIGGER trg_samples_set_lonlat
    BEFORE INSERT OR UPDATE OF location ON samples
    FOR EACH ROW EXECUTE FUNCTION samples_set_lonlat();

-- Keep the visibility map fresh under heavy ingestion so the map's index-only scan stays
-- fast. Default insert-autovacuum fires far too rarely on a large table.
-- (The covering index idx_samples_lonlat_cover is built online via
-- scripts/backfill_sample_lonlat.py — CREATE INDEX CONCURRENTLY can't run in init.)
ALTER TABLE samples SET (
    autovacuum_vacuum_insert_scale_factor = 0,
    autovacuum_vacuum_insert_threshold = 50000
);

CREATE OR REPLACE FUNCTION field_type_canonical(kind TEXT) RETURNS TEXT
LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN kind IN ('bool', 'boolean') THEN 'boolean'
        WHEN kind IN ('int', 'integer', 'float', 'number') THEN 'number'
        WHEN kind IN ('date', 'datetime') THEN 'datetime'
        WHEN kind IN ('array', 'list') THEN 'list'
        WHEN kind IN ('dict', 'object') THEN 'object'
        WHEN kind IN ('enum', 'string') THEN 'string'
        ELSE 'unknown'
    END
$$;


CREATE OR REPLACE FUNCTION normalize_dataset_snapshot(snapshot JSONB) RETURNS JSONB
LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN snapshot IS NULL THEN NULL ELSE
        (snapshot - 'fields' - 'field_registry_version') || jsonb_build_object('fields', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'field_name', field->>'field_name',
                'field_type', field_type_canonical(field->>'field_type'),
                'display_type', CASE WHEN field->>'field_type' = 'enum'
                    THEN COALESCE(field->>'display_type', 'enum') ELSE field->>'display_type' END,
                'overlay_config', field->'overlay_config',
                'origin', COALESCE(field->>'origin',
                    CASE WHEN field->>'field_type' = 'array' THEN 'native_array' ELSE 'metadata' END)
            ) ORDER BY field->>'field_name')
            FROM jsonb_array_elements(COALESCE(snapshot->'fields', '[]'::jsonb)) field
            WHERE COALESCE(field->>'origin', 'metadata') <> 'system'
              AND field->>'field_name' NOT IN ('tags', 'media_type', 'detections.label',
                'ground_truth.label', 'predictions.label', 'polygons.label', 'segmentation.labels',
                '__heatmap_fields__', '__mask_overlay_fields__', '__keypoint_fields__', '__classification_fields__')
        ), '[]'::jsonb)) END
$$;

CREATE OR REPLACE FUNCTION dataset_snapshots_equal(left_snapshot JSONB, right_snapshot JSONB) RETURNS BOOLEAN
LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
    WITH normalized AS (
        SELECT normalize_dataset_snapshot(left_snapshot) AS lhs,
               normalize_dataset_snapshot(right_snapshot) AS rhs
    ), definitions AS (
        SELECT lhs_field, rhs_field FROM normalized,
            LATERAL jsonb_array_elements(lhs->'fields') lhs_field
            FULL JOIN LATERAL jsonb_array_elements(rhs->'fields') rhs_field
                ON lhs_field->>'field_name' = rhs_field->>'field_name'
    )
    SELECT CASE
        WHEN left_snapshot IS NULL OR right_snapshot IS NULL
            THEN left_snapshot IS NOT DISTINCT FROM right_snapshot
        WHEN left_snapshot->>'field_registry_version' = '1' AND right_snapshot->>'field_registry_version' = '1'
            THEN lhs = rhs
        ELSE (lhs - 'fields') = (rhs - 'fields') AND NOT EXISTS (
            SELECT 1 FROM definitions WHERE
                CASE WHEN lhs_field IS NOT NULL AND rhs_field IS NOT NULL
                    THEN lhs_field <> rhs_field
                    WHEN (lhs_field IS NULL AND left_snapshot->>'field_registry_version' IS DISTINCT FROM '1'
                              AND right_snapshot->>'field_registry_version' = '1')
                      OR (rhs_field IS NULL AND right_snapshot->>'field_registry_version' IS DISTINCT FROM '1'
                              AND left_snapshot->>'field_registry_version' = '1')
                    THEN COALESCE(lhs_field, rhs_field)->>'origin' <> 'metadata'
                      OR COALESCE(lhs_field, rhs_field)->>'display_type' IS NOT NULL
                      OR COALESCE(lhs_field, rhs_field)->>'overlay_config' IS NOT NULL
                    ELSE TRUE
                END
        )
    END FROM normalized
$$;


-- Dataset fields table for tracking field schemas
CREATE TABLE IF NOT EXISTS dataset_fields (
    id SERIAL PRIMARY KEY,
    dataset_id INTEGER REFERENCES datasets(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    field_type TEXT NOT NULL CONSTRAINT dataset_fields_type_check CHECK (field_type IN ('unknown', 'boolean', 'number', 'string', 'datetime', 'list', 'object')),
    origin TEXT NOT NULL DEFAULT 'metadata' CONSTRAINT dataset_fields_origin_check CHECK (origin IN ('metadata', 'native_array', 'system')),
    CONSTRAINT dataset_fields_native_type_check CHECK (origin <> 'native_array' OR field_type = 'list'),
    is_promoted BOOLEAN DEFAULT FALSE,
    display_type TEXT,       -- Override: 'enum' to show dropdown instead of slider for numbers
    overlay_config JSONB,    -- Per-field overlay display config: heatmap colormap/range/opacity
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(dataset_id, field_name)
);

-- Embeddings table for vector storage
-- Patch embeddings use field_name convention: "clip_patch_4x4_r0c0"
-- This avoids schema changes and works with the existing unique constraint.
CREATE TABLE IF NOT EXISTS embeddings (
    id SERIAL PRIMARY KEY,
    sample_id INTEGER REFERENCES samples(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL DEFAULT 'clip', -- Name of the embedding field
    vector vector, -- Variable-dimension embeddings (CLIP-512, DINOv2-1024, etc.)
    x FLOAT, -- Legacy, frozen and unused (coordinates live in embedding_coordinates); to be dropped
    y FLOAT, -- Legacy, frozen and unused
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sample_id, field_name)
);

-- Sample indexes
CREATE INDEX IF NOT EXISTS idx_samples_dataset_id ON samples(dataset_id);
CREATE INDEX IF NOT EXISTS idx_samples_dataset_id_id ON samples(dataset_id, id);
CREATE INDEX IF NOT EXISTS idx_samples_metadata ON samples USING gin (metadata);
CREATE INDEX IF NOT EXISTS idx_samples_metadata_gin ON samples USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_samples_location ON samples USING gist (location) WHERE location IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_samples_dataset_location ON samples(dataset_id) WHERE location IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_samples_tags_gin ON samples USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_samples_created_by ON samples(created_by_user_id);
-- (dataset_id, created_at): index-only MIN/MAX for the ingestion-time slider bounds
-- and fast created_at range filtering.
CREATE INDEX IF NOT EXISTS idx_samples_dataset_created_at ON samples(dataset_id, created_at);
-- Object/patch grain: objects are media_type='object' rows in the SAME
-- dataset as their parent image, with metadata.parent_id -> the image sample id. These
-- index the grain filter (grid/scatter "object view") and the image->objects pivot
-- (filter object rows by parent_id).
CREATE INDEX IF NOT EXISTS idx_samples_dataset_media_type ON samples(dataset_id, media_type);
CREATE INDEX IF NOT EXISTS idx_samples_parent_id ON samples ((metadata ->> 'parent_id'))
    WHERE media_type = 'object';

-- Embedding indexes
-- Note: HNSW index requires fixed-dimension vector column. With variable dimensions
-- (CLIP 512, DINOv2 1024 etc.) in the same column, we skip the HNSW index.
-- Similarity search uses the API/SDK which handles this without the index.
-- For production, consider partitioning embeddings by field_name with typed columns.
-- Layout writes must never update the vector heap.
CREATE TABLE IF NOT EXISTS embedding_coordinates (
    sample_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    x DOUBLE PRECISION NOT NULL CHECK (x > '-Infinity'::float8 AND x < 'Infinity'::float8),
    y DOUBLE PRECISION NOT NULL CHECK (y > '-Infinity'::float8 AND y < 'Infinity'::float8),
    PRIMARY KEY (sample_id, field_name),
    FOREIGN KEY (sample_id, field_name) REFERENCES embeddings(sample_id, field_name) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_embedding_coordinates_visualization
    ON embedding_coordinates(field_name, x, y, sample_id);

-- Only unprojected vectors: the dataset/field index bounds even an empty poll.
-- The PK also indexes the embedding FK's cascade lookup.
CREATE TABLE IF NOT EXISTS embedding_projection_pending (
    sample_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    PRIMARY KEY (sample_id, field_name),
    FOREIGN KEY (sample_id, field_name) REFERENCES embeddings(sample_id, field_name) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_embedding_projection_pending_dataset
    ON embedding_projection_pending(dataset_id, field_name, sample_id);

-- Precomputed even-coverage scatter ranks. Dedicated NARROW table (not a column on the
-- wide, TOASTed-vector, multi-indexed `embeddings`, where updating an indexed rank in place
-- prevents HOT updates and bloats the table on every re-fit) so a re-rank is a cheap INSERT,
-- not a multi-million-row wide UPDATE. x/y denormalized so the scatter needs no join.
CREATE TABLE IF NOT EXISTS embedding_viz_rank (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    sample_id  INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    rank       INTEGER NOT NULL,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    PRIMARY KEY (dataset_id, field_name, sample_id)
);
-- Covering index: `dataset_id = ? AND field_name = ? AND rank < ?` → index-only scan
CREATE INDEX IF NOT EXISTS idx_embedding_viz_rank_scan
    ON embedding_viz_rank (dataset_id, field_name, rank) INCLUDE (sample_id, x, y);
-- FK-check index: sample deletes probe this table per row (seq scan = 250ms/row)
CREATE INDEX IF NOT EXISTS idx_embedding_viz_rank_sample ON embedding_viz_rank (sample_id);

-- Per-(dataset, field) stats: occupied_cells + total_points drive budget→per_cell.
CREATE TABLE IF NOT EXISTS embedding_viz_meta (
    dataset_id     INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    field_name     TEXT NOT NULL,
    grid           INTEGER NOT NULL DEFAULT 200,
    -- x_min/x_max/y_min/y_max (NOT xmin/xmax — reserved Postgres system column names)
    x_min          DOUBLE PRECISION,
    x_max          DOUBLE PRECISION,
    y_min          DOUBLE PRECISION,
    y_max          DOUBLE PRECISION,
    occupied_cells INTEGER NOT NULL DEFAULT 1,
    total_points   INTEGER NOT NULL DEFAULT 0,
    updated_at     TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- TRUE once coordinates moved after the last full re-rank: x/y in
    -- embedding_viz_rank are still exact, only the even-coverage ORDER is approximate.
    rank_stale     BOOLEAN NOT NULL DEFAULT FALSE,
    -- Set while a full layout of the map is written over time (the SDK's compute_visualization): new samples
    -- aren't placed on it until then (backend/map_placement.py). Renewed by its writer; lapses if it dies.
    placement_held_until TIMESTAMPTZ,
    PRIMARY KEY (dataset_id, field_name)
);

-- Even-coverage scatter sample-priority: rebuilds embedding_viz_rank (within-grid-cell
-- random rank) so the visualization query is an index range scan (WHERE rank < per_cell)
-- instead of a per-request window-sort. Single source of truth for every full re-rank
-- (the SDK via /embeddings/recompute-viz-rank, /reduce, and map re-fits).
-- The scatter caps the points it sends the client; a per-request ROW_NUMBER() OVER
-- (PARTITION BY cell ORDER BY random()) did the same job far slower. Each occupied cell
-- contributes its first per_cell points, so sparse/outlier clusters stay visible.
CREATE OR REPLACE FUNCTION recompute_viz_rank(
    p_dataset_id INTEGER, p_field TEXT, p_full BOOLEAN DEFAULT TRUE
) RETURNS INTEGER AS $$
DECLARE
    g CONSTANT INTEGER := 200;
    v_xmin DOUBLE PRECISION; v_xmax DOUBLE PRECISION;
    v_ymin DOUBLE PRECISION; v_ymax DOUBLE PRECISION;
    v_total INTEGER; v_occupied INTEGER;
    v_n INTEGER := 0;
BEGIN
    PERFORM require_writable_dataset(p_dataset_id);
    IF NOT p_full THEN
        -- Heal a failed initial rank once; subsequent upkeep never scans the field.
        IF NOT EXISTS (SELECT 1 FROM embedding_viz_meta
                       WHERE dataset_id = p_dataset_id AND field_name = p_field) THEN
            RETURN recompute_viz_rank(p_dataset_id, p_field, TRUE);
        END IF;
        RETURN 0;
    END IF;
    IF p_full THEN
        -- Explicit full rebuilds fence only this dataset's rank writers: the coordinate
        -- triggers take 20261020 shared, and every sample/dataset delete path takes the
        -- field-lifecycle lock (20261016) exclusive first. Other datasets never wait.
        -- Incremental upkeep returns above without these locks or scanning old rows.
        PERFORM pg_advisory_xact_lock_shared(20261016, p_dataset_id);
        PERFORM pg_advisory_xact_lock(20261020, p_dataset_id);
        SELECT MIN(e.x), MAX(e.x), MIN(e.y), MAX(e.y), COUNT(*)
          INTO v_xmin, v_xmax, v_ymin, v_ymax, v_total
          FROM embedding_coordinates e JOIN samples s ON e.sample_id = s.id
         WHERE s.dataset_id = p_dataset_id AND e.field_name = p_field AND e.x IS NOT NULL;

        DELETE FROM embedding_viz_rank WHERE dataset_id = p_dataset_id AND field_name = p_field;

        IF v_total IS NULL OR v_total = 0 THEN
            DELETE FROM embedding_viz_meta WHERE dataset_id = p_dataset_id AND field_name = p_field;
            RETURN 0;
        END IF;

        INSERT INTO embedding_viz_rank (dataset_id, field_name, sample_id, rank, x, y)
        SELECT p_dataset_id, p_field, e.sample_id,
               ROW_NUMBER() OVER (
                   PARTITION BY
                       LEAST(GREATEST(FLOOR((e.x - v_xmin) / NULLIF(v_xmax - v_xmin, 0) * g)::int, 0), g - 1),
                       LEAST(GREATEST(FLOOR((e.y - v_ymin) / NULLIF(v_ymax - v_ymin, 0) * g)::int, 0), g - 1)
                   ORDER BY random()
               ) - 1,
               e.x, e.y
          FROM embedding_coordinates e JOIN samples s ON e.sample_id = s.id
         WHERE s.dataset_id = p_dataset_id AND e.field_name = p_field AND e.x IS NOT NULL;
        GET DIAGNOSTICS v_n = ROW_COUNT;

        SELECT COUNT(*) INTO v_occupied
          FROM embedding_viz_rank
         WHERE dataset_id = p_dataset_id AND field_name = p_field AND rank = 0;

        -- A full re-rank is the only thing that clears rank_stale (set by the
        -- coordinate trigger): the coverage ORDER is now
        -- computed for the layout the coordinates actually have. Under the fence the
        -- rows just inserted are the field's whole membership, so v_n is exact.
        INSERT INTO embedding_viz_meta
            (dataset_id, field_name, grid, x_min, x_max, y_min, y_max, occupied_cells, total_points, updated_at, rank_stale)
        VALUES (p_dataset_id, p_field, g, v_xmin, v_xmax, v_ymin, v_ymax, GREATEST(v_occupied, 1), v_n, now(), FALSE)
        ON CONFLICT (dataset_id, field_name) DO UPDATE SET
            grid = EXCLUDED.grid, x_min = EXCLUDED.x_min, x_max = EXCLUDED.x_max,
            y_min = EXCLUDED.y_min, y_max = EXCLUDED.y_max,
            occupied_cells = EXCLUDED.occupied_cells, total_points = EXCLUDED.total_points,
            updated_at = now(), rank_stale = FALSE;
        RETURN v_n;
    END IF;
    RETURN v_n;
END;
$$ LANGUAGE plpgsql;

-- Like 056: skip empty transitions, cache singleton plans, replan bulk joins.
CREATE OR REPLACE FUNCTION embeddings_queue_projection() RETURNS trigger AS $$
DECLARE
    changed_count INTEGER;
    changed RECORD;
    previous_vector vector;
BEGIN
    IF NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    SELECT count(*) INTO changed_count FROM (SELECT 1 FROM projection_new LIMIT 2) rows;
    IF changed_count = 0 THEN
        RETURN NULL;
    ELSIF changed_count = 1 THEN
        SELECT * INTO changed FROM projection_new;
        IF TG_OP = 'UPDATE' THEN
            SELECT vector INTO previous_vector FROM projection_old;
            IF changed.vector IS NOT DISTINCT FROM previous_vector THEN
                RETURN NULL;
            END IF;
            DELETE FROM embedding_coordinates
             WHERE sample_id = changed.sample_id AND field_name = changed.field_name;
            IF changed.vector IS NULL THEN
                DELETE FROM embedding_projection_pending
                 WHERE sample_id = changed.sample_id AND field_name = changed.field_name;
            END IF;
        END IF;
        IF changed.vector IS NOT NULL THEN
            INSERT INTO embedding_projection_pending (sample_id, field_name, dataset_id)
            SELECT changed.sample_id, changed.field_name, s.dataset_id
              FROM samples s WHERE s.id = changed.sample_id
            ON CONFLICT DO NOTHING;
        END IF;
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        EXECUTE $q$
            DELETE FROM embedding_coordinates c USING projection_new n, projection_old o
             WHERE n.id = o.id AND n.vector IS DISTINCT FROM o.vector
               AND c.sample_id = n.sample_id AND c.field_name = n.field_name$q$;
        EXECUTE $q$
            DELETE FROM embedding_projection_pending p USING projection_new n, projection_old o
             WHERE n.id = o.id AND n.vector IS DISTINCT FROM o.vector AND n.vector IS NULL
               AND p.sample_id = n.sample_id AND p.field_name = n.field_name$q$;
        EXECUTE $q$
            INSERT INTO embedding_projection_pending (sample_id, field_name, dataset_id)
            SELECT n.sample_id, n.field_name, s.dataset_id
              FROM projection_new n JOIN projection_old o ON o.id = n.id
              JOIN samples s ON s.id = n.sample_id
             WHERE n.vector IS NOT NULL AND n.vector IS DISTINCT FROM o.vector
            ON CONFLICT DO NOTHING$q$;
    ELSE
        EXECUTE $q$
            INSERT INTO embedding_projection_pending (sample_id, field_name, dataset_id)
            SELECT n.sample_id, n.field_name, s.dataset_id
              FROM projection_new n JOIN samples s ON s.id = n.sample_id
             WHERE n.vector IS NOT NULL
            ON CONFLICT DO NOTHING$q$;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_embeddings_projection_insert
    AFTER INSERT ON embeddings REFERENCING NEW TABLE AS projection_new
    FOR EACH STATEMENT EXECUTE FUNCTION embeddings_queue_projection();
CREATE TRIGGER trg_embeddings_projection_update
    AFTER UPDATE ON embeddings REFERENCING OLD TABLE AS projection_old NEW TABLE AS projection_new
    FOR EACH STATEMENT EXECUTE FUNCTION embeddings_queue_projection();

-- Code that still writes embeddings.x/y must fail loudly instead of writing the frozen columns.
CREATE OR REPLACE FUNCTION embeddings_reject_legacy_coordinates() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'coordinates live in embedding_coordinates, not embeddings.x/y';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_embeddings_reject_coordinate_update
    BEFORE UPDATE OF x, y ON embeddings
    FOR EACH ROW EXECUTE FUNCTION embeddings_reject_legacy_coordinates();
CREATE TRIGGER trg_embeddings_reject_coordinate_insert
    BEFORE INSERT ON embeddings FOR EACH ROW
    WHEN (NEW.x IS NOT NULL OR NEW.y IS NOT NULL)
    EXECUTE FUNCTION embeddings_reject_legacy_coordinates();

CREATE OR REPLACE FUNCTION coordinates_skip_unchanged() RETURNS trigger AS $$
BEGIN
    IF NEW IS NOT DISTINCT FROM OLD THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_coordinates_skip_unchanged
    BEFORE UPDATE ON embedding_coordinates FOR EACH ROW EXECUTE FUNCTION coordinates_skip_unchanged();

-- Keep embedding_viz_rank in step with coordinate writes (a move keeps its rank and marks coverage
-- stale; a clear removes the rank row and decrements the count), on narrow tables only. An
-- unranked field keeps its grid fallback until the next rank refresh.
CREATE OR REPLACE FUNCTION coordinates_sync_viz_rank() RETURNS trigger AS $$
BEGIN
    IF NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM coordinate_new) THEN
        RETURN NULL;
    END IF;
    -- Wait out a full rebuild of the same dataset (sorted, so writers cannot deadlock).
    PERFORM pg_advisory_xact_lock_shared(20261020, d.dataset_id)
       FROM (SELECT DISTINCT s.dataset_id FROM coordinate_new n JOIN samples s ON s.id = n.sample_id
             ORDER BY 1) d;
    IF TG_OP = 'UPDATE' THEN
        EXECUTE $q$
            UPDATE embedding_viz_rank r SET x = n.x, y = n.y
              FROM coordinate_new n JOIN samples s ON s.id = n.sample_id
             WHERE r.dataset_id = s.dataset_id AND r.field_name = n.field_name AND r.sample_id = n.sample_id$q$;
        EXECUTE $q$
            UPDATE embedding_viz_meta m SET rank_stale = TRUE
              FROM (SELECT DISTINCT s.dataset_id, n.field_name FROM coordinate_new n
                    JOIN samples s ON s.id = n.sample_id) d
             WHERE m.dataset_id = d.dataset_id AND m.field_name = d.field_name AND NOT m.rank_stale$q$;
    ELSE
        EXECUTE $q$
            INSERT INTO embedding_viz_rank (dataset_id, field_name, sample_id, rank, x, y)
            SELECT s.dataset_id, n.field_name, n.sample_id,
                   FLOOR(random() * GREATEST(m.total_points::double precision / GREATEST(m.occupied_cells, 1), 1.0))::int,
                   n.x, n.y
              FROM coordinate_new n JOIN samples s ON s.id = n.sample_id
              JOIN embedding_viz_meta m ON m.dataset_id = s.dataset_id AND m.field_name = n.field_name
            ON CONFLICT (dataset_id, field_name, sample_id) DO NOTHING$q$;
    END IF;
    EXECUTE $q$
        DELETE FROM embedding_projection_pending p USING coordinate_new n
         WHERE p.sample_id = n.sample_id AND p.field_name = n.field_name$q$;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_coordinates_insert
    AFTER INSERT ON embedding_coordinates REFERENCING NEW TABLE AS coordinate_new
    FOR EACH STATEMENT EXECUTE FUNCTION coordinates_sync_viz_rank();
CREATE TRIGGER trg_coordinates_update
    AFTER UPDATE ON embedding_coordinates REFERENCING NEW TABLE AS coordinate_new
    FOR EACH STATEMENT EXECUTE FUNCTION coordinates_sync_viz_rank();

CREATE OR REPLACE FUNCTION coordinates_delete_viz_rank() RETURNS trigger AS $$
BEGIN
    IF NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM coordinate_old) THEN
        RETURN NULL;
    END IF;
    -- Wait out a full rebuild of the same dataset (sorted, so writers cannot deadlock).
    PERFORM pg_advisory_xact_lock_shared(20261020, d.dataset_id)
       FROM (SELECT s.dataset_id FROM coordinate_old o JOIN samples s ON s.id = o.sample_id
             UNION SELECT r.dataset_id FROM coordinate_old o JOIN embedding_viz_rank r
                   ON r.sample_id = o.sample_id AND r.field_name = o.field_name ORDER BY 1) d;
    EXECUTE $q$
        UPDATE embedding_viz_meta m SET rank_stale = TRUE
          FROM coordinate_old o JOIN embeddings e USING (sample_id, field_name)
          JOIN samples s ON s.id = o.sample_id
         WHERE m.dataset_id = s.dataset_id AND m.field_name = o.field_name AND NOT m.rank_stale$q$;
    EXECUTE $q$
        DELETE FROM embedding_viz_rank r USING coordinate_old o
         WHERE r.sample_id = o.sample_id AND r.field_name = o.field_name$q$;
    -- Explicit clearing schedules re-projection; cascades cannot resurrect work
    -- because the embedding/sample no longer exists in this transaction.
    EXECUTE $q$
        INSERT INTO embedding_projection_pending (sample_id, field_name, dataset_id)
        SELECT e.sample_id, e.field_name, s.dataset_id
          FROM coordinate_old o JOIN embeddings e USING (sample_id, field_name)
          JOIN samples s ON s.id = e.sample_id
         WHERE e.vector IS NOT NULL
        ON CONFLICT DO NOTHING$q$;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_coordinates_delete
    AFTER DELETE ON embedding_coordinates REFERENCING OLD TABLE AS coordinate_old
    FOR EACH STATEMENT EXECUTE FUNCTION coordinates_delete_viz_rank();

-- Count only actual membership changes, including conflict-skipped bulk inserts.
-- Upsert also establishes metadata for the first full population.
CREATE OR REPLACE FUNCTION viz_rank_count_inserted() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rank_new) THEN
        RETURN NULL;
    END IF;
    INSERT INTO embedding_viz_meta (dataset_id, field_name, total_points)
    SELECT dataset_id, field_name, COUNT(*) FROM rank_new GROUP BY 1, 2 ORDER BY 1, 2
    ON CONFLICT (dataset_id, field_name) DO UPDATE
       SET total_points = embedding_viz_meta.total_points + EXCLUDED.total_points, updated_at = now();
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_viz_rank_count_inserted
    AFTER INSERT ON embedding_viz_rank REFERENCING NEW TABLE AS rank_new
    FOR EACH STATEMENT EXECUTE FUNCTION viz_rank_count_inserted();

-- Count the rank rows actually removed, including a sample FK cascade that
-- happens before the coordinate cascade. This also fixes clear's missing decrement.
CREATE OR REPLACE FUNCTION viz_rank_count_deleted() RETURNS trigger AS $$
BEGIN
    IF NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM rank_old) THEN
        RETURN NULL;
    END IF;
    EXECUTE $q$
        UPDATE embedding_viz_meta m
           SET total_points = GREATEST(0, m.total_points - c.n), updated_at = now()
          FROM (SELECT dataset_id, field_name, COUNT(*) AS n FROM rank_old GROUP BY 1, 2) c
         WHERE m.dataset_id = c.dataset_id AND m.field_name = c.field_name$q$;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_viz_rank_count_deleted
    AFTER DELETE ON embedding_viz_rank REFERENCING OLD TABLE AS rank_old
    FOR EACH STATEMENT EXECUTE FUNCTION viz_rank_count_deleted();

-- Views table
CREATE TABLE IF NOT EXISTS views (
    id SERIAL PRIMARY KEY,
    dataset_id INTEGER REFERENCES datasets(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    description TEXT,
    slug TEXT NOT NULL UNIQUE,
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_public BOOLEAN DEFAULT false,
    shared_with_user_ids INTEGER[] DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    name_scope INTEGER NOT NULL DEFAULT 0,
    UNIQUE(dataset_id, name_scope, name)
);
CREATE INDEX IF NOT EXISTS idx_views_dataset_id ON views(dataset_id);
CREATE INDEX IF NOT EXISTS idx_views_user_id ON views(user_id);
CREATE INDEX IF NOT EXISTS idx_views_slug ON views(slug);
CREATE INDEX IF NOT EXISTS idx_views_public ON views(is_public) WHERE is_public = true;

-- A dataset's default view (migration 0002): the saved view the app opens the dataset on, and goes back to
-- when the filters are cleared. Only the app uses it. Deleting the view leaves the dataset without one.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'datasets_default_view_id_fkey'
          AND conrelid = 'datasets'::regclass
    ) THEN
        ALTER TABLE datasets
            ADD CONSTRAINT datasets_default_view_id_fkey
            FOREIGN KEY (default_view_id) REFERENCES views(id) ON DELETE SET NULL;
    END IF;
END $$;

-- Admission history survives account deletion for the one-hour IP rate limit.
CREATE TABLE IF NOT EXISTS demo_admissions (
    id BIGSERIAL PRIMARY KEY,
    client_ip TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_demo_admissions_ip ON demo_admissions(client_ip, created_at);
CREATE INDEX IF NOT EXISTS idx_demo_admissions_user ON demo_admissions(user_id);

-- Sessions table
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(id, expires_at);

-- API tokens table
CREATE TABLE IF NOT EXISTS api_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    expires_at TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE,
    -- 'worker': a compute worker's token, which only the worker endpoints accept (routers/workers.py).
    -- 'follower': a follower instance's token, which only the follower endpoints accept (routers/followers.py).
    scope TEXT NOT NULL DEFAULT 'full' CONSTRAINT api_tokens_scope_check CHECK (scope IN ('full', 'worker', 'follower'))
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_token ON api_tokens(token);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user_id ON api_tokens(user_id);

-- Dataset field stats cache
CREATE TABLE IF NOT EXISTS dataset_field_stats (
    id SERIAL PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    field_type VARCHAR(50) NOT NULL,  -- 'number', 'string', 'boolean', 'array', 'object'
    sample_count INTEGER DEFAULT 0,   -- How many samples have this field
    min_value FLOAT,                   -- For numeric fields
    max_value FLOAT,                   -- For numeric fields
    unique_values TEXT[],              -- For enums (limited to first 100 unique values)
    values_complete BOOLEAN NOT NULL DEFAULT FALSE,
    last_updated TIMESTAMP DEFAULT NOW(),
    UNIQUE(dataset_id, field_name)
);

CREATE INDEX IF NOT EXISTS idx_dataset_field_stats_dataset ON dataset_field_stats(dataset_id);

-- Function to mark dataset stats as stale when samples change
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

DROP TRIGGER IF EXISTS trigger_mark_stats_stale ON samples;
CREATE TRIGGER trigger_mark_stats_stale
    AFTER INSERT ON samples
    FOR EACH ROW
    EXECUTE FUNCTION mark_dataset_stats_stale();

-- Advisory-lock namespace registry. Postgres advisory locks share ONE (classid, objid) space per
-- database, so every future pg_advisory_* user in this schema must claim a different first key:
--   20260724 = retired legacy statistics mutex (reserved)
--   20261016 = field registry lifecycle (shared writers/exclusive definition changes), keyed by dataset id
--   20261017 = field index operations (try-only build/drop/clear/rename), keyed by dataset id
--   20261020 = viz-rank rebuild fence (exclusive full recompute_viz_rank/shared coordinate triggers), keyed by dataset id
--   20261021 = sidebar facet scan slots shared by every backend worker (backend/routers/datasets.py), second key 0..K-1 (K = max_parallel_workers, or max_worker_processes when that is 0); second keys -1..-n (shared) = a worker with n requests whose fields wait for a slot
--   20261062 = restored-backup recovery (one normalizer at a time), second key 0
--   20261064 = dataset job schedules (one scheduler pass at a time), second key 0
--   20261065 = exclusive job resources (one started job per resource, e.g. map fits), keyed by hashtext(resource)
--   20261066 = ANN (HNSW) index builds, keyed by hashtext(embedding field name); session lock held by the build
--   20261067 = every concurrent index build or drop on one table (try-only, never waited for), keyed by hashtext(table name)
--   (single 64-bit keys are a separate space: job_queue.enqueue_job hashes its dedupe identity into one)
CREATE OR REPLACE FUNCTION refresh_stale_datasets_stats()
RETURNS TABLE(dataset_id INTEGER, fields_updated INTEGER, error TEXT) AS $$
BEGIN
    RAISE EXCEPTION 'Statistics now run as durable jobs. Use POST /api/datasets/{id}/refresh-stats or the SDK.'
        USING ERRCODE = '0A000';
END;
$$ LANGUAGE plpgsql;

-- Pre-computed image hashes for fast similarity/duplicate detection
-- Generic: works with any hash algorithm (dhash, phash, ahash, etc.)
CREATE TABLE IF NOT EXISTS sample_hashes (
    id SERIAL PRIMARY KEY,
    sample_id INTEGER REFERENCES samples(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,     -- Hash field name (e.g. 'dhash_8', 'phash_8')
    hash_hex TEXT NOT NULL,       -- Hex string representation
    hash_bits bit varying,        -- Bit representation for fast Hamming distance
    prefix bit(16),               -- First 16 bits for candidate filtering
    UNIQUE(sample_id, field_name)
);
CREATE INDEX IF NOT EXISTS idx_sample_hashes_dataset ON sample_hashes(sample_id);
CREATE INDEX IF NOT EXISTS idx_sample_hashes_field ON sample_hashes(field_name);
CREATE INDEX IF NOT EXISTS idx_sample_hashes_prefix ON sample_hashes(field_name, prefix);
-- Representative build for the "collapse duplicates" toggle
CREATE INDEX IF NOT EXISTS idx_sample_hashes_field_hex ON sample_hashes (field_name, hash_hex, sample_id);

-- "Collapse duplicates" toggle: precomputed hidden (non-representative) samples per
-- (dataset, hash_config). The collapse_duplicates filter operator is NOT EXISTS in here.
-- A small precomputed set, not a live GROUP BY (a multi-million-row scan per request) and
-- not a column on the wide samples/sample_hashes (the same narrow-table reason as
-- embedding_viz_rank). The representative of each hash group is MIN(sample_id).
CREATE TABLE IF NOT EXISTS sample_dup_secondary (
    dataset_id  INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    hash_config TEXT NOT NULL,                 -- a hash field_name, or '_all'
    sample_id   INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    PRIMARY KEY (dataset_id, hash_config, sample_id)
);
CREATE INDEX IF NOT EXISTS idx_dup_secondary_lookup ON sample_dup_secondary (hash_config, sample_id);
-- FK-check index: sample deletes probe this table per row
CREATE INDEX IF NOT EXISTS idx_dup_secondary_sample ON sample_dup_secondary (sample_id);

-- Reference rebuild for tests; production upkeep uses backend.duplicate_jobs.
CREATE OR REPLACE FUNCTION refresh_dup_secondary(p_dataset_id INTEGER, p_config TEXT)
RETURNS INTEGER AS $$
DECLARE v_n INTEGER;
BEGIN
    DELETE FROM sample_dup_secondary
     WHERE dataset_id = p_dataset_id AND hash_config = p_config;

    IF p_config = '_all' THEN
        -- A sample is a secondary iff another sample with a smaller id has the identical
        -- set of hashes (combined across all fields).
        INSERT INTO sample_dup_secondary (dataset_id, hash_config, sample_id)
        SELECT p_dataset_id, '_all', sample_id FROM (
            SELECT sample_id,
                   ROW_NUMBER() OVER (PARTITION BY combined_hash ORDER BY sample_id) AS rn
            FROM (
                SELECT h.sample_id,
                       string_agg(h.field_name || ':' || h.hash_hex, ',' ORDER BY h.field_name) AS combined_hash
                FROM sample_hashes h JOIN samples s ON h.sample_id = s.id
                WHERE s.dataset_id = p_dataset_id
                GROUP BY h.sample_id
            ) per_sample
        ) t WHERE rn > 1;
    ELSIF p_config = '_any' THEN
        -- Secondary iff a smaller-id sample shares its hash on ANY field (OR / union of
        -- the per-field secondaries): a sample that is a non-representative under any one
        -- hash is hidden. (Simple union, not transitive connected-components — it won't
        -- merge images that don't directly share a hash, which is the safer behavior.)
        INSERT INTO sample_dup_secondary (dataset_id, hash_config, sample_id)
        SELECT DISTINCT p_dataset_id, '_any', sample_id FROM (
            SELECT h.sample_id,
                   ROW_NUMBER() OVER (PARTITION BY h.field_name, h.hash_hex ORDER BY h.sample_id) AS rn
            FROM sample_hashes h JOIN samples s ON h.sample_id = s.id
            WHERE s.dataset_id = p_dataset_id
        ) t WHERE rn > 1;
    ELSE
        -- Secondary iff a smaller-id sample shares this exact hash for the field.
        INSERT INTO sample_dup_secondary (dataset_id, hash_config, sample_id)
        SELECT p_dataset_id, p_config, sample_id FROM (
            SELECT h.sample_id,
                   ROW_NUMBER() OVER (PARTITION BY h.hash_hex ORDER BY h.sample_id) AS rn
            FROM sample_hashes h JOIN samples s ON h.sample_id = s.id
            WHERE s.dataset_id = p_dataset_id AND h.field_name = p_config
        ) t WHERE rn > 1;
    END IF;

    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END;
$$ LANGUAGE plpgsql;

-- Field→model registry: which checkpoint produced each embedding field.
-- Text search/concepts encode prompts with a CLIP TEXT tower — only comparable to image
-- vectors from the SAME checkpoint (same dim ≠ same space; DINO has no text tower and must
-- be refused). Field-GLOBAL by field_name, matching dim validation + HNSW semantics.
CREATE TABLE IF NOT EXISTS embedding_field_models (
    field_name TEXT PRIMARY KEY,
    model      TEXT NOT NULL,  -- HF checkpoint id or sentence-transformers name
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Embedding "concepts": a linear direction w in an embedding field's space →
-- a per-sample calibrated [0,1] score. Producers (seed/probe/text) differ only in how w is
-- made; downstream is shared — rank (reuse similarity_search vector=w) or exclude (the
-- concept_exclude filter operator). Narrow scores table, for the same reason as
-- embedding_viz_rank and sample_dup_secondary: not a column on the wide, TOASTed-vector
-- `embeddings`, and not samples.metadata (a JSONB update per sample would churn its GIN
-- index). Scores are calibrated to [0,1] so a threshold means the same for every producer:
-- seed/text = 1 - cosine distance/2, probe = sigmoid(w·x + bias).
CREATE TABLE IF NOT EXISTS embedding_concepts (
    id                 SERIAL PRIMARY KEY,
    dataset_id         INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    embedding_field    TEXT NOT NULL,                       -- which vector space w lives in
    kind               TEXT NOT NULL,                       -- 'seed' | 'probe' | 'text'
    spec               JSONB NOT NULL DEFAULT '{}'::jsonb,  -- provenance: {seed_ids|pos_tag/neg_tag|prompt}
    weight             TEXT NOT NULL,                       -- w as a pgvector literal (field's dim)
    bias               DOUBLE PRECISION NOT NULL DEFAULT 0,
    threshold          DOUBLE PRECISION NOT NULL DEFAULT 0.5,  -- default exclude/select cut (per-view tunable)
    scored_count       INTEGER NOT NULL DEFAULT 0,
    auto_rescore       BOOLEAN NOT NULL DEFAULT TRUE,
    last_scored_embedding_id BIGINT NOT NULL DEFAULT 0,
    score_revision     BIGINT NOT NULL DEFAULT 0,
    score_initialized  BOOLEAN NOT NULL DEFAULT FALSE,
    score_definition_transaction BIGINT NOT NULL DEFAULT txid_current(),
    score_manual_snapshot TEXT,
    score_manual_transaction BIGINT,
    -- Explicit scoring job owning these scores; FALSE scores_ready = the
    -- definition changed and that job has not finished, so filters refuse the concept.
    score_job_id       UUID,
    scores_ready       BOOLEAN NOT NULL DEFAULT TRUE,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dataset_id, name)
);
CREATE INDEX IF NOT EXISTS idx_embedding_concepts_dataset ON embedding_concepts (dataset_id);

-- Narrow materialized scores. PK = per-sample anti/semi-join lookup for the concept_select /
-- concept_exclude operators; (concept_id, score) serves the preview count (score > t).
CREATE TABLE IF NOT EXISTS embedding_concept_scores (
    concept_id INTEGER NOT NULL REFERENCES embedding_concepts(id) ON DELETE CASCADE,
    sample_id  INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    score      REAL NOT NULL,
    PRIMARY KEY (concept_id, sample_id)
);
CREATE INDEX IF NOT EXISTS idx_concept_scores_score ON embedding_concept_scores (concept_id, score);
-- FK-check index: sample deletes probe this table per row (seq scan = 148ms/row)
CREATE INDEX IF NOT EXISTS idx_concept_scores_sample ON embedding_concept_scores (sample_id);

CREATE TABLE IF NOT EXISTS concept_embedding_pending (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    block_id BIGINT NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current(),
    PRIMARY KEY (dataset_id, field_name, block_id, transaction_id)
);

CREATE INDEX IF NOT EXISTS idx_concept_embedding_pending_delivery
    ON concept_embedding_pending(dataset_id,transaction_id,field_name,block_id);

CREATE TABLE IF NOT EXISTS concept_score_pending (
    concept_id INTEGER NOT NULL,
    block_id BIGINT NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current(),
    PRIMARY KEY (concept_id, block_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS concept_score_count_deltas (
    concept_id INTEGER NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current(),
    delta BIGINT NOT NULL,
    PRIMARY KEY (concept_id, transaction_id)
);

-- One call guards every concept a filter reads: the filter compiler ANDs it once in front of the whole
-- filter, where it is a One-Time Filter on an unchanged plan. Inside each concept EXISTS it was a join
-- filter that flipped the anti join to hash every sample, and PARALLEL UNSAFE forbade the parallel plan
-- (a broad exclusion filter: 0.18 s without a guard, 1.31 s with it). It only reads, and an error raised in a
-- parallel worker reaches the client with its SQLSTATE, so it is PARALLEL SAFE.
CREATE OR REPLACE FUNCTION require_concept_scores(target_concepts INTEGER[])
RETURNS BOOLEAN AS $$
DECLARE
    concept_name TEXT;
BEGIN
    SELECT name INTO concept_name FROM embedding_concepts
    WHERE id = ANY(target_concepts) AND NOT scores_ready ORDER BY id LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'Concept "%" is being rescored; use it again when its scoring job finishes', concept_name
            USING ERRCODE = '55000';
    END IF;
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION concept_score_value(
    producer_kind TEXT, embedding VECTOR, weight VECTOR, bias DOUBLE PRECISION
) RETURNS REAL AS $$
    SELECT CASE WHEN producer_kind = 'probe'
        THEN (1.0 / (1.0 + exp(-GREATEST(-30, LEAST(30, -(embedding <#> weight) + bias)))))::real
        ELSE (1.0 - (embedding <=> weight) / 2.0)::real END;
$$ LANGUAGE sql IMMUTABLE STRICT;

CREATE OR REPLACE FUNCTION mark_concept_definition_changed()
RETURNS TRIGGER AS $$
BEGIN
    IF (NEW.dataset_id, NEW.embedding_field, NEW.kind, NEW.weight, NEW.bias)
        IS DISTINCT FROM (OLD.dataset_id, OLD.embedding_field, OLD.kind, OLD.weight, OLD.bias) THEN
        NEW.score_revision := OLD.score_revision + 1;
        NEW.score_initialized := FALSE;
        NEW.score_definition_transaction := txid_current();
    END IF;
    IF NEW.auto_rescore IS DISTINCT FROM OLD.auto_rescore THEN
        NEW.score_revision := OLD.score_revision + 1;
        NEW.score_initialized := FALSE;
        DELETE FROM concept_score_pending WHERE concept_id = OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER concept_definition_changed
BEFORE UPDATE ON embedding_concepts
FOR EACH ROW EXECUTE FUNCTION mark_concept_definition_changed();

-- Skip empty transitions, cache singleton plans, and replan bulk joins using their actual size.
CREATE OR REPLACE FUNCTION record_concept_score_work()
RETURNS TRIGGER AS $$
DECLARE
    changed_count INTEGER;
BEGIN
    IF TG_OP = 'DELETE' AND NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT count(*) INTO changed_count FROM (SELECT 1 FROM inserted_embeddings LIMIT 2) changed;
        IF changed_count = 0 THEN
            RETURN NULL;
        ELSIF changed_count = 1 THEN
            INSERT INTO concept_embedding_pending(dataset_id,field_name,block_id)
            SELECT DISTINCT sample.dataset_id,changed.field_name,changed.sample_id::bigint / 1024
            FROM inserted_embeddings changed JOIN samples sample ON sample.id=changed.sample_id
            ON CONFLICT DO NOTHING;
        ELSE
            EXECUTE $q$
                INSERT INTO concept_embedding_pending(dataset_id,field_name,block_id)
                SELECT DISTINCT sample.dataset_id,changed.field_name,changed.sample_id::bigint / 1024
                FROM inserted_embeddings changed JOIN samples sample ON sample.id=changed.sample_id
                ON CONFLICT DO NOTHING$q$;
        END IF;
    ELSE
        SELECT count(*) INTO changed_count FROM (SELECT 1 FROM deleted_embeddings LIMIT 2) changed;
        IF changed_count = 0 THEN
            RETURN NULL;
        ELSIF changed_count = 1 THEN
            INSERT INTO concept_embedding_pending(dataset_id,field_name,block_id)
            SELECT DISTINCT sample.dataset_id,changed.field_name,changed.sample_id::bigint / 1024
            FROM deleted_embeddings changed JOIN samples sample ON sample.id=changed.sample_id
            ON CONFLICT DO NOTHING;
        ELSE
            EXECUTE $q$
                INSERT INTO concept_embedding_pending(dataset_id,field_name,block_id)
                SELECT DISTINCT sample.dataset_id,changed.field_name,changed.sample_id::bigint / 1024
                FROM deleted_embeddings changed JOIN samples sample ON sample.id=changed.sample_id
                ON CONFLICT DO NOTHING$q$;
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION record_concept_score_update()
RETURNS TRIGGER AS $$
BEGIN
    IF (OLD.sample_id,OLD.field_name,OLD.vector) IS DISTINCT FROM (NEW.sample_id,NEW.field_name,NEW.vector) THEN
        INSERT INTO concept_embedding_pending(dataset_id,field_name,block_id)
        SELECT DISTINCT sample.dataset_id,changed.field_name,changed.sample_id::bigint / 1024
        FROM (VALUES (OLD.sample_id,OLD.field_name),(NEW.sample_id,NEW.field_name)) changed(sample_id,field_name)
        JOIN samples sample ON sample.id=changed.sample_id
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER concept_embeddings_inserted
AFTER INSERT ON embeddings REFERENCING NEW TABLE AS inserted_embeddings
FOR EACH STATEMENT EXECUTE FUNCTION record_concept_score_work();
CREATE OR REPLACE TRIGGER concept_embeddings_deleted
AFTER DELETE ON embeddings REFERENCING OLD TABLE AS deleted_embeddings
FOR EACH STATEMENT EXECUTE FUNCTION record_concept_score_work();
CREATE OR REPLACE TRIGGER concept_embeddings_updated
AFTER UPDATE OF sample_id,field_name,vector ON embeddings FOR EACH ROW
EXECUTE FUNCTION record_concept_score_update();

-- No concept FK on these queues: sample cascades must not lock a concept being deleted.
CREATE OR REPLACE FUNCTION remove_concept_score_work()
RETURNS TRIGGER AS $$
BEGIN
    DELETE FROM concept_score_pending WHERE concept_id=OLD.id;
    DELETE FROM concept_score_count_deltas WHERE concept_id=OLD.id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER concept_deleted
AFTER DELETE ON embedding_concepts FOR EACH ROW EXECUTE FUNCTION remove_concept_score_work();

CREATE OR REPLACE FUNCTION record_concept_score_counts()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' AND NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' THEN
        INSERT INTO concept_score_count_deltas(concept_id, delta)
        SELECT inserted.concept_id, count(*) FROM inserted_scores inserted
        JOIN embedding_concepts concept ON concept.id = inserted.concept_id
        GROUP BY inserted.concept_id
        ON CONFLICT (concept_id, transaction_id) DO UPDATE
        SET delta = concept_score_count_deltas.delta + EXCLUDED.delta;
    ELSE
        INSERT INTO concept_score_count_deltas(concept_id, delta)
        SELECT deleted.concept_id, -count(*) FROM deleted_scores deleted
        JOIN embedding_concepts concept ON concept.id = deleted.concept_id
        GROUP BY deleted.concept_id
        ON CONFLICT (concept_id, transaction_id) DO UPDATE
        SET delta = concept_score_count_deltas.delta + EXCLUDED.delta;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER concept_scores_inserted
AFTER INSERT ON embedding_concept_scores REFERENCING NEW TABLE AS inserted_scores
FOR EACH STATEMENT EXECUTE FUNCTION record_concept_score_counts();
CREATE OR REPLACE TRIGGER concept_scores_deleted
AFTER DELETE ON embedding_concept_scores REFERENCING OLD TABLE AS deleted_scores
FOR EACH STATEMENT EXECUTE FUNCTION record_concept_score_counts();

CREATE OR REPLACE FUNCTION invalidate_concept_scores()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE embedding_concepts SET score_initialized = FALSE, score_revision = score_revision + 1;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER concept_embeddings_truncated
AFTER TRUNCATE ON embeddings FOR EACH STATEMENT EXECUTE FUNCTION invalidate_concept_scores();
CREATE OR REPLACE TRIGGER concept_scores_truncated
AFTER TRUNCATE ON embedding_concept_scores FOR EACH STATEMENT EXECUTE FUNCTION invalidate_concept_scores();

CREATE OR REPLACE FUNCTION settle_concept_score_count(target_concept INTEGER)
RETURNS VOID AS $$
    WITH claimed AS MATERIALIZED (
        SELECT ctid AS row_tid FROM concept_score_count_deltas WHERE concept_id = target_concept
        ORDER BY transaction_id LIMIT 1000 FOR UPDATE
    ), consumed AS (
        DELETE FROM concept_score_count_deltas WHERE ctid = ANY(ARRAY(SELECT row_tid FROM claimed))
        RETURNING delta
    )
    UPDATE embedding_concepts
    SET scored_count = scored_count + (SELECT COALESCE(sum(delta), 0) FROM consumed),
        updated_at = now()
    WHERE id = target_concept AND EXISTS (SELECT 1 FROM consumed);
$$ LANGUAGE sql;

CREATE OR REPLACE FUNCTION score_concept(p_concept_id INTEGER, p_full BOOLEAN DEFAULT TRUE)
RETURNS INTEGER AS $$
DECLARE
    definition embedding_concepts%ROWTYPE;
    observed_snapshot TEXT;
    target_ids INTEGER[];
    new_watermark BIGINT;
    scored INTEGER := 0;
BEGIN
    SELECT * INTO definition FROM embedding_concepts WHERE id = p_concept_id FOR NO KEY UPDATE;
    IF NOT FOUND THEN
        RETURN 0;
    END IF;
    observed_snapshot := txid_current_snapshot()::text;
    EXECUTE $q$SELECT embedding.id FROM embeddings embedding
        JOIN samples sample ON sample.id=embedding.sample_id
        WHERE embedding.id>$1 AND embedding.field_name=$2 AND sample.dataset_id=$3
        ORDER BY embedding.id DESC LIMIT 1$q$
    INTO new_watermark USING definition.last_scored_embedding_id,
        definition.embedding_field,definition.dataset_id;
    UPDATE embedding_concepts SET score_revision = score_revision + 1 WHERE id = p_concept_id;
    IF p_full OR (definition.auto_rescore AND NOT definition.score_initialized) THEN
        DELETE FROM embedding_concept_scores WHERE concept_id = p_concept_id;
        INSERT INTO embedding_concept_scores(concept_id, sample_id, score)
        SELECT p_concept_id, embedding.sample_id,
               concept_score_value(definition.kind, embedding.vector, definition.weight::vector, definition.bias)
        FROM embeddings embedding JOIN samples sample ON sample.id = embedding.sample_id
        WHERE sample.dataset_id = definition.dataset_id
          AND embedding.field_name = definition.embedding_field AND embedding.vector IS NOT NULL;
        GET DIAGNOSTICS scored = ROW_COUNT;
        UPDATE embedding_concepts SET scored_count = scored,
            score_initialized = definition.score_definition_transaction <> txid_current(), updated_at = now()
        WHERE id = p_concept_id;
        DELETE FROM concept_score_count_deltas WHERE concept_id = p_concept_id;
    ELSE
        IF NOT definition.auto_rescore THEN
            -- "Score new" on a manual concept keeps the indexed append-only contract.
            -- Replan with the current watermark rather than caching a broad ID-range plan.
            EXECUTE $q$SELECT array_agg(embedding.sample_id)
                FROM embeddings embedding JOIN samples sample ON sample.id=embedding.sample_id
                WHERE embedding.id>$1 AND embedding.id<=$4
                  AND embedding.field_name=$2 AND sample.dataset_id=$3$q$
            INTO target_ids USING definition.last_scored_embedding_id,
                definition.embedding_field,definition.dataset_id,new_watermark;
        ELSE
            SELECT array_agg(DISTINCT sample.id ORDER BY sample.id) INTO target_ids
            FROM (
                SELECT block_id FROM concept_score_pending WHERE concept_id=p_concept_id
                  AND (transaction_id=txid_current()
                       OR txid_visible_in_snapshot(transaction_id,observed_snapshot::txid_snapshot))
                UNION
                SELECT block_id FROM concept_embedding_pending
                WHERE dataset_id=definition.dataset_id AND field_name=definition.embedding_field
                  AND (transaction_id=txid_current()
                       OR txid_visible_in_snapshot(transaction_id,observed_snapshot::txid_snapshot))
                  AND (definition.score_manual_snapshot IS NULL OR transaction_id=txid_current() OR (
                      transaction_id>=txid_snapshot_xmin(definition.score_manual_snapshot::txid_snapshot)
                      AND transaction_id<>definition.score_manual_transaction
                      AND NOT txid_visible_in_snapshot(transaction_id,definition.score_manual_snapshot::txid_snapshot)))
            ) blocks
            JOIN samples sample ON sample.dataset_id=definition.dataset_id
              AND sample.id>=blocks.block_id*1024 AND sample.id<(blocks.block_id+1)*1024;
        END IF;
        INSERT INTO embedding_concept_scores(concept_id, sample_id, score)
        SELECT p_concept_id, embedding.sample_id,
               concept_score_value(definition.kind, embedding.vector, definition.weight::vector, definition.bias)
        FROM unnest(target_ids) target(sample_id)
        JOIN samples sample ON sample.id = target.sample_id AND sample.dataset_id = definition.dataset_id
        JOIN embeddings embedding ON embedding.sample_id = target.sample_id
          AND embedding.field_name = definition.embedding_field AND embedding.vector IS NOT NULL
        ON CONFLICT (concept_id, sample_id) DO UPDATE SET score = EXCLUDED.score
        WHERE embedding_concept_scores.score IS DISTINCT FROM EXCLUDED.score;
        GET DIAGNOSTICS scored = ROW_COUNT;
        DELETE FROM embedding_concept_scores existing
        WHERE existing.concept_id = p_concept_id AND existing.sample_id = ANY(target_ids)
          AND NOT EXISTS (
              SELECT 1 FROM embeddings embedding JOIN samples sample ON sample.id = embedding.sample_id
              WHERE sample.id = existing.sample_id AND sample.dataset_id = definition.dataset_id
                AND embedding.field_name = definition.embedding_field AND embedding.vector IS NOT NULL
          );
        LOOP
            PERFORM settle_concept_score_count(p_concept_id);
            EXIT WHEN NOT EXISTS (
                SELECT 1 FROM concept_score_count_deltas WHERE concept_id = p_concept_id
                  AND (transaction_id = txid_current()
                       OR txid_visible_in_snapshot(transaction_id, observed_snapshot::txid_snapshot))
            );
        END LOOP;
    END IF;
    DELETE FROM concept_score_pending WHERE concept_id = p_concept_id
      AND (transaction_id = txid_current()
           OR txid_visible_in_snapshot(transaction_id, observed_snapshot::txid_snapshot));
    UPDATE embedding_concepts SET score_manual_snapshot=observed_snapshot,
        score_manual_transaction=txid_current(),
        last_scored_embedding_id=COALESCE(new_watermark,definition.last_scored_embedding_id)
    WHERE id=p_concept_id;
    RETURN scored;
END;
$$ LANGUAGE plpgsql;

-- Storage configurations (S3 bucket configs managed via API)
CREATE TABLE IF NOT EXISTS storage_configs (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    storage_type TEXT NOT NULL DEFAULT 's3' CHECK (storage_type IN ('s3', 'minio', 'gcs')),
    bucket_name TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT 'us-east-1',
    access_key_id TEXT,
    secret_access_key_encrypted TEXT,
    endpoint_url TEXT,
    is_public_bucket BOOLEAN DEFAULT FALSE,
    is_default BOOLEAN DEFAULT FALSE,
    bucket_patterns TEXT[] NOT NULL DEFAULT '{}',
    created_by_user_id INTEGER REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- FALSE: serves every dataset. TRUE: serves only the datasets in storage_config_datasets.
    restricted BOOLEAN NOT NULL DEFAULT FALSE,
    -- Bumped on every UPDATE (trigger below): workers rebuild their cached S3 client when it changes.
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    -- The default serves datasets without a config of their own, so it can't be restricted.
    CONSTRAINT storage_configs_default_not_restricted CHECK (NOT (is_default AND restricted))
);
-- The datasets a restricted storage config serves.
CREATE TABLE IF NOT EXISTS storage_config_datasets (
    storage_config_id INTEGER NOT NULL REFERENCES storage_configs(id) ON DELETE CASCADE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    PRIMARY KEY (storage_config_id, dataset_id)
);
-- Used by the sample-media proxy to find the right credentials for an
-- s3://bucket/key path (bucket-first routing for multi-bucket datasets).
CREATE INDEX IF NOT EXISTS idx_storage_configs_bucket_name ON storage_configs(bucket_name);
-- Rows with a non-empty bucket_patterns array are the only ones we need to
-- glob-match against when the exact bucket_name lookup misses.
CREATE INDEX IF NOT EXISTS idx_storage_configs_has_patterns
    ON storage_configs ((array_length(bucket_patterns, 1)))
    WHERE array_length(bucket_patterns, 1) > 0;
-- Quick lookup of the default config.
CREATE INDEX IF NOT EXISTS idx_storage_configs_default ON storage_configs(is_default) WHERE is_default = true;

-- Enforce a SINGLE default storage config. The media-serving path falls back to
-- the default config when a dataset has no storage_config_id assigned; if two
-- rows had is_default=TRUE the chosen credentials would be nondeterministic.
CREATE OR REPLACE FUNCTION ensure_single_default_storage()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.is_default = true THEN
        UPDATE storage_configs SET is_default = false WHERE id != NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_single_default_storage ON storage_configs;
CREATE TRIGGER trigger_single_default_storage
    BEFORE INSERT OR UPDATE ON storage_configs
    FOR EACH ROW
    WHEN (NEW.is_default = true)
    EXECUTE FUNCTION ensure_single_default_storage();

CREATE OR REPLACE FUNCTION storage_configs_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_storage_configs_touch_updated_at ON storage_configs;
CREATE TRIGGER trigger_storage_configs_touch_updated_at
    BEFORE UPDATE ON storage_configs
    FOR EACH ROW
    EXECUTE FUNCTION storage_configs_touch_updated_at();

-- Add FK from datasets.storage_config_id → storage_configs.id (deferred from datasets table
-- declaration above because storage_configs is declared later in this file).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'datasets_storage_config_id_fkey'
          AND conrelid = 'datasets'::regclass
    ) THEN
        ALTER TABLE datasets
            ADD CONSTRAINT datasets_storage_config_id_fkey
            FOREIGN KEY (storage_config_id) REFERENCES storage_configs(id) ON DELETE SET NULL;
    END IF;
END $$;

-- Dataset versioning (named snapshots for tracking changes over time)
CREATE TABLE IF NOT EXISTS dataset_versions (
    id SERIAL PRIMARY KEY,
    dataset_id INTEGER REFERENCES datasets(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    sample_count INTEGER NOT NULL,
    include_metadata BOOLEAN DEFAULT TRUE,  -- FALSE = hash-only (lightweight)
    -- 2 = every row's media_type/location/heading recorded (NULL means none); 1 = legacy
    snapshot_format SMALLINT NOT NULL DEFAULT 1,
    dataset_snapshot JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(dataset_id, name)
);

CREATE TABLE IF NOT EXISTS version_snapshots (
    version_id INTEGER REFERENCES dataset_versions(id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL,
    metadata_hash TEXT NOT NULL,  -- MD5 of JSONB metadata for fast change detection
    hashes_hash TEXT,            -- MD5 of sample_hashes for detecting hash changes
    metadata JSONB,              -- Full metadata snapshot for restore/checkout
    tags TEXT[],                 -- Tags at time of snapshot
    filepath TEXT,               -- Filepath at time of snapshot
    location GEOMETRY(Point, 4326), -- Location at time of snapshot
    heading DOUBLE PRECISION,    -- Heading at time of snapshot
    array_fields_snapshot JSONB, -- Dynamic array field values at time of snapshot
    media_type TEXT,             -- media_type at time of snapshot (046; NULL in older snapshots)
    created_at TIMESTAMPTZ,
    last_modified_at TIMESTAMPTZ,
    PRIMARY KEY (version_id, sample_id)
);
CREATE INDEX IF NOT EXISTS idx_version_snapshots_version ON version_snapshots(version_id);

-- Dynamic array field registry: tracks which TEXT[] columns exist on samples
CREATE TABLE IF NOT EXISTS sample_array_columns (
    column_name TEXT PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO sample_array_columns (column_name) VALUES ('tags') ON CONFLICT DO NOTHING;

-- Transient server-side selection sets (embeddings lasso / large "select all" /
-- version-diff "view samples"). A request materializes its matching sample_ids ONCE
-- under a random token; queries then scope via the indexed `in_selection` filter
-- operator instead of shipping a multi-MB id array or re-scanning per page. Rows are
-- ephemeral and TTL-pruned (>2h) by the bounded selection cleanup job.
CREATE TABLE IF NOT EXISTS region_selections (
    token TEXT NOT NULL,
    sample_id INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- (token, sample_id) serves the in_selection EXISTS lookup and keeps a token's ids
-- ordered by sample_id (aligns with the grid's ORDER BY id).
CREATE INDEX IF NOT EXISTS idx_region_selections_token ON region_selections (token, sample_id);
-- Supports TTL pruning.
CREATE INDEX IF NOT EXISTS idx_region_selections_created ON region_selections (created_at);

-- Staging for server-side embedding near-duplicate detection (quality.py).
-- /quality/duplicates/compute streams candidate near-duplicate pairs (HNSW top-k within a
-- cosine threshold) here under a job token; /quality/duplicates/finalize union-finds them
-- into groups. Ephemeral — TTL-pruned and dropped on finalize.
CREATE TABLE IF NOT EXISTS quality_dup_pairs (
    token TEXT NOT NULL,
    a INTEGER NOT NULL,
    b INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_quality_dup_pairs_token ON quality_dup_pairs (token);
CREATE INDEX IF NOT EXISTS idx_quality_dup_pairs_created ON quality_dup_pairs (created_at);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    description TEXT,
    applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO schema_version (version, description)
VALUES (1, 'Initial schema with samples, embeddings, hashes, dataset settings')
ON CONFLICT (version) DO NOTHING;

-- Create default admin user (password: admin123 - CHANGE IN PROD)
INSERT INTO users (username, email, role, password_hash, must_change_password, is_active)
VALUES (
    'admin',
    'admin@freedomridge.local',
    'admin',
    '$2b$12$nkQ07rkpkp/mX8ereH68VeWNE1LgnfGQOMZTcf0nyy07OLwXB348e',  -- admin123
    true,  -- Must change password on first login
    true
)
ON CONFLICT (username) DO NOTHING;

-- Per-dataset tag registry: when a tag was first applied, by whom, and when it was last used.
-- created_at/created_by NULL = unknown (pre-registry or an unregistered path).
CREATE TABLE IF NOT EXISTS dataset_tags (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL DEFAULT 'tags',
    tag TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    last_used_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (dataset_id, field_name, tag)
);

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (length(kind) > 0),
    dataset_id INTEGER,
    placement TEXT NOT NULL DEFAULT 'server' CHECK (placement IN ('server','compute')),
    lane TEXT NOT NULL DEFAULT 'user' CHECK (lane IN ('maintenance','user')),
    args JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(args) = 'object'),
    barrier JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(barrier) = 'object'),
    resources JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(resources) = 'object'),
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
    progress_done BIGINT NOT NULL DEFAULT 0 CHECK (progress_done >= 0),
    progress_total BIGINT CHECK (progress_total >= 0),
    message TEXT,
    checkpoint JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(checkpoint) = 'object'),
    result JSONB,
    error JSONB,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    lease_token UUID,
    worker_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    failures INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    contentions INTEGER NOT NULL DEFAULT 0,
    max_failures INTEGER NOT NULL DEFAULT 5 CHECK (max_failures > 0),
    retry_seconds DOUBLE PRECISION NOT NULL DEFAULT 5 CHECK (retry_seconds > 0 AND retry_seconds < 'Infinity'::float8),
    max_retry_seconds DOUBLE PRECISION NOT NULL DEFAULT 300
        CHECK (max_retry_seconds >= retry_seconds AND max_retry_seconds < 'Infinity'::float8),
    dedupe_owner TEXT NOT NULL DEFAULT 'system',
    dedupe_key TEXT,
    CHECK ((state = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL AND worker_id IS NOT NULL)
        OR (state <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_jobs_queued_dedupe ON jobs(kind,dedupe_key,dedupe_owner)
    WHERE state='queued' AND started_at IS NULL AND dedupe_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_lane_owner ON jobs(dataset_id,lane)
    WHERE dataset_id IS NOT NULL AND (state='running' OR (state='queued' AND started_at IS NOT NULL));
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(placement,available_at,created_at,id) WHERE state='queued';
CREATE INDEX IF NOT EXISTS idx_jobs_expired ON jobs(lease_expires_at) WHERE state='running';
CREATE INDEX IF NOT EXISTS idx_jobs_dataset_created ON jobs(dataset_id,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_queued_dataset
    ON jobs(dataset_id,available_at,created_at,id) WHERE state='queued' AND dataset_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_global_created ON jobs(created_at DESC,id DESC) WHERE dataset_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_statistics_failure_cooldown ON jobs(dataset_id, finished_at)
    WHERE kind='statistics' AND state='failed';
CREATE INDEX IF NOT EXISTS idx_jobs_dataset_history ON jobs(dataset_id,created_at DESC,id DESC)
    WHERE NOT (kind IN ('statistics','concept_scoring') AND dedupe_owner='system' AND state='succeeded' AND NOT barrier @> '{"full":true}');
CREATE INDEX IF NOT EXISTS idx_statistics_history_retention ON jobs(finished_at,id)
    WHERE kind IN ('statistics','concept_scoring') AND dedupe_owner='system' AND state='succeeded' AND NOT barrier @> '{"full":true}';

CREATE TABLE IF NOT EXISTS dataset_job_lanes (
    dataset_id INTEGER REFERENCES datasets(id) ON DELETE CASCADE,
    lane TEXT NOT NULL CHECK (lane IN ('maintenance','user')),
    PRIMARY KEY (dataset_id,lane)
);

CREATE TABLE IF NOT EXISTS job_controls (
    job_id UUID PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    placement TEXT NOT NULL CHECK (placement IN ('server','compute')),
    name TEXT,
    hostname TEXT,
    capabilities JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(capabilities) = 'object'),
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    current_job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
    -- A compute worker's owner (the user of its worker token) and SDK version; NULL for the server's runners.
    user_id INTEGER CONSTRAINT workers_user_id_fkey REFERENCES users(id) ON DELETE CASCADE,
    sdk_version TEXT
);

CREATE OR REPLACE FUNCTION initialize_job_controls()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO job_controls(job_id) VALUES (NEW.id) ON CONFLICT DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER jobs_initialize_controls
AFTER INSERT ON jobs
FOR EACH ROW EXECUTE FUNCTION initialize_job_controls();

CREATE INDEX IF NOT EXISTS idx_jobs_kind_created ON jobs(kind,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_concept_jobs_active ON jobs ((args->>'concept_id'))
    WHERE kind = 'concept_scoring' AND state IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS idx_concept_jobs_failed ON jobs ((args->>'concept_id'), finished_at)
    WHERE kind = 'concept_scoring' AND state = 'failed';
CREATE INDEX IF NOT EXISTS idx_concept_jobs_resume ON jobs ((args->>'concept_id'),created_at DESC,id DESC)
    WHERE kind = 'concept_scoring';

CREATE OR REPLACE FUNCTION dispatch_concept_score_work(
    batch_size INTEGER, target_dataset INTEGER,
    target_field TEXT DEFAULT NULL, observed_snapshot TEXT DEFAULT NULL
) RETURNS BIGINT AS $$
    WITH claimed AS MATERIALIZED (
        SELECT ctid AS row_tid,* FROM concept_embedding_pending
        WHERE dataset_id = target_dataset
          AND (target_field IS NULL OR field_name = target_field)
          AND (observed_snapshot IS NULL OR transaction_id = txid_current()
               OR txid_visible_in_snapshot(transaction_id, observed_snapshot::txid_snapshot))
        ORDER BY transaction_id,field_name,block_id
        LIMIT batch_size FOR UPDATE SKIP LOCKED
    ), delivered AS (
        INSERT INTO concept_score_pending(concept_id,block_id,transaction_id)
        SELECT concept.id,claimed.block_id,claimed.transaction_id FROM claimed
        JOIN embedding_concepts concept ON concept.dataset_id = claimed.dataset_id
          AND concept.embedding_field = claimed.field_name AND concept.auto_rescore
        WHERE NOT EXISTS (
            SELECT 1 FROM jobs
            WHERE kind='concept_scoring' AND args->>'concept_id'=concept.id::text AND state='failed'
              AND barrier->>'score_revision'=concept.score_revision::text
              AND error->>'type' IN ('DataError','DataException','ValueError')
        )
        ON CONFLICT DO NOTHING RETURNING concept_id
    ), removed AS (
        DELETE FROM concept_embedding_pending
        WHERE ctid = ANY(ARRAY(SELECT row_tid FROM claimed))
        RETURNING block_id
    )
    SELECT count(*) FROM removed;
$$ LANGUAGE sql;

CREATE TABLE IF NOT EXISTS dataset_stats_pending (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    block_id BIGINT NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current(),
    field_names TEXT[],
    requested_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (dataset_id, block_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS dataset_stats_state (
    dataset_id INTEGER PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    initialized BOOLEAN NOT NULL DEFAULT FALSE,
    publish_pending BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS dataset_stats_blocks (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    block_id BIGINT NOT NULL CHECK (block_id >= 0),
    split_level SMALLINT NOT NULL DEFAULT 0 CHECK (split_level BETWEEN 0 AND 12),
    PRIMARY KEY (dataset_id, block_id)
);

CREATE TABLE IF NOT EXISTS dataset_stats_parts (
    dataset_id INTEGER NOT NULL,
    block_id BIGINT NOT NULL,
    part_id INTEGER NOT NULL,
    measured_snapshot TEXT NOT NULL,
    sample_count BIGINT NOT NULL,
    PRIMARY KEY (dataset_id, block_id, part_id),
    FOREIGN KEY (dataset_id, block_id) REFERENCES dataset_stats_blocks(dataset_id, block_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS dataset_block_field_stats (
    dataset_id INTEGER NOT NULL,
    block_id BIGINT NOT NULL,
    part_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    field_type TEXT NOT NULL,
    definition_token TEXT,
    sample_count BIGINT NOT NULL,
    min_value DOUBLE PRECISION,
    max_value DOUBLE PRECISION,
    text_min TEXT,
    text_max TEXT,
    unique_values TEXT[],
    values_complete BOOLEAN NOT NULL,
    PRIMARY KEY (dataset_id, block_id, part_id, field_name),
    FOREIGN KEY (dataset_id, block_id, part_id)
        REFERENCES dataset_stats_parts(dataset_id, block_id, part_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_block_stats_field ON dataset_block_field_stats(dataset_id, field_name, block_id, part_id);

CREATE OR REPLACE FUNCTION record_stats_work(target_dataset INTEGER, target_block BIGINT, names TEXT[])
RETURNS VOID AS $$
BEGIN
    IF current_setting('fridge.deletion_dataset', TRUE) = target_dataset::text THEN
        RETURN;
    END IF;
    INSERT INTO dataset_stats_pending(dataset_id, block_id, field_names)
    VALUES (target_dataset, target_block, names)
    ON CONFLICT (dataset_id, block_id, transaction_id) DO UPDATE
    SET field_names = CASE
        WHEN dataset_stats_pending.field_names IS NULL OR EXCLUDED.field_names IS NULL THEN NULL
        ELSE ARRAY(SELECT DISTINCT unnest(dataset_stats_pending.field_names || EXCLUDED.field_names) ORDER BY 1)
    END
    WHERE dataset_stats_pending.field_names IS NOT NULL
      AND (EXCLUDED.field_names IS NULL OR NOT EXCLUDED.field_names <@ dataset_stats_pending.field_names);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_stats_updated ON samples;
CREATE TRIGGER trigger_stats_updated AFTER UPDATE ON samples
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION mark_dataset_stats_stale();
DROP TRIGGER IF EXISTS trigger_stats_deleted ON samples;
CREATE TRIGGER trigger_stats_deleted AFTER DELETE ON samples
    FOR EACH ROW EXECUTE FUNCTION mark_dataset_stats_stale();
DROP TRIGGER IF EXISTS trigger_stats_truncated ON samples;
CREATE TRIGGER trigger_stats_truncated AFTER TRUNCATE ON samples
    FOR EACH STATEMENT EXECUTE FUNCTION mark_dataset_stats_stale();

CREATE OR REPLACE FUNCTION mark_field_stats_stale()
RETURNS TRIGGER AS $$
DECLARE
    target_dataset INTEGER;
    target_field TEXT;
BEGIN
    target_dataset := CASE WHEN TG_OP = 'DELETE' THEN OLD.dataset_id ELSE NEW.dataset_id END;
    -- Bulk field jobs visit every captured block; definition changes need no full rebuild.
    IF current_setting('fridge.bulk_dataset', TRUE) = target_dataset::text THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.origin = 'metadata' AND EXISTS (
        SELECT 1 FROM dataset_stats_pending WHERE dataset_id = NEW.dataset_id
          AND transaction_id = txid_current() AND block_id >= 0
          AND (field_names IS NULL OR field_names && ARRAY[NEW.field_name, NEW.field_name || '.label',
                                                           NEW.field_name || '.labels'])
    ) AND NOT EXISTS (
        SELECT 1 FROM dataset_block_field_stats WHERE dataset_id = NEW.dataset_id
          AND field_name IN (NEW.field_name, NEW.field_name || '.label', NEW.field_name || '.labels')
    ) THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' AND (OLD.dataset_id, OLD.field_name, OLD.field_type, OLD.origin)
        IS NOT DISTINCT FROM (NEW.dataset_id, NEW.field_name, NEW.field_type, NEW.origin) THEN
        RETURN NULL;
    END IF;
    target_dataset := CASE WHEN TG_OP = 'DELETE' THEN OLD.dataset_id ELSE NEW.dataset_id END;
    target_field := CASE WHEN TG_OP = 'DELETE' THEN OLD.field_name ELSE NEW.field_name END;
    IF EXISTS (SELECT 1 FROM datasets WHERE id = target_dataset) THEN
        PERFORM record_stats_work(target_dataset, -1, ARRAY[target_field]);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_field_stats_changed ON dataset_fields;
CREATE TRIGGER trigger_field_stats_changed AFTER INSERT OR UPDATE OR DELETE ON dataset_fields
    FOR EACH ROW EXECUTE FUNCTION mark_field_stats_stale();

CREATE OR REPLACE FUNCTION mark_full_stats_request()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.stats_full_requests IS DISTINCT FROM OLD.stats_full_requests THEN
        PERFORM record_stats_work(NEW.id, -1, NULL);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_full_stats_request ON datasets;
CREATE TRIGGER trigger_full_stats_request AFTER UPDATE OF stats_full_requests ON datasets
    FOR EACH ROW EXECUTE FUNCTION mark_full_stats_request();

CREATE OR REPLACE FUNCTION stats_datetime(value TEXT)
RETURNS TIMESTAMPTZ AS $$
    SELECT CASE
        WHEN value ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$'
             AND pg_input_is_valid(value, 'timestamp with time zone')
        THEN value::timestamptz
    END;
$$ LANGUAGE sql IMMUTABLE STRICT SET timezone = 'UTC';

-- Settings → System status → Backups: how often the job runner backs up the whole database
-- (scripts/backup_local.py into FRIDGE_BACKUP_DIR). One row. interval_hours NULL means no scheduled
-- backups; an admin can still run one now. Runs fall on start_hour (server local time) and every
-- interval_hours after it.
CREATE TABLE IF NOT EXISTS backup_schedule (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    interval_hours INTEGER CHECK (interval_hours BETWEEN 1 AND 8760),
    start_hour INTEGER NOT NULL DEFAULT 2 CHECK (start_hour BETWEEN 0 AND 23),
    keep INTEGER NOT NULL DEFAULT 7 CHECK (keep BETWEEN 1 AND 100),
    -- When interval_hours or start_hour last changed. The schedule starts at the first slot after it, so
    -- turning it on never starts a backup at once; changing only keep leaves it alone.
    timing_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL  -- who saved it last
);
INSERT INTO backup_schedule (singleton) VALUES (TRUE) ON CONFLICT DO NOTHING;

-- Incremental duplicate maintenance.
CREATE TABLE IF NOT EXISTS duplicate_configs (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    config TEXT NOT NULL,
    initialized BOOLEAN NOT NULL DEFAULT FALSE,
    restart_required BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (dataset_id, config)
);

-- Retain old keys after a sample/hash deletion so its successor can be promoted.
-- _any has one membership per field/hash; other configs have one per sample.
CREATE TABLE IF NOT EXISTS duplicate_members (
    dataset_id INTEGER NOT NULL,
    config TEXT NOT NULL,
    sample_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (dataset_id, config, sample_id, key),
    FOREIGN KEY (dataset_id, config) REFERENCES duplicate_configs(dataset_id, config) ON DELETE CASCADE,
    CONSTRAINT duplicate_key_size CHECK (octet_length(config) + octet_length(key) <= 2000)
);
CREATE INDEX IF NOT EXISTS idx_duplicate_group
    ON duplicate_members(dataset_id, config, key, sample_id);

CREATE TABLE IF NOT EXISTS duplicate_pending (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    block_id BIGINT NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current(),
    PRIMARY KEY (dataset_id, block_id, transaction_id)
);

-- Loose index scan: one seek per existing config, not one visit per hidden sample.
DO $$
DECLARE
    target RECORD;
BEGIN
    SELECT dataset_id, hash_config INTO target FROM sample_dup_secondary
    ORDER BY dataset_id, hash_config LIMIT 1;
    WHILE FOUND LOOP
        INSERT INTO duplicate_configs(dataset_id, config)
        VALUES (target.dataset_id, target.hash_config) ON CONFLICT DO NOTHING;
        SELECT dataset_id, hash_config INTO target FROM sample_dup_secondary
        WHERE (dataset_id, hash_config) > (target.dataset_id, target.hash_config)
        ORDER BY dataset_id, hash_config LIMIT 1;
    END LOOP;
END;
$$;

-- Skip empty transitions, cache singleton plans, and replan bulk joins using their actual size.
CREATE OR REPLACE FUNCTION record_duplicate_hash_work()
RETURNS TRIGGER AS $$
DECLARE
    changed_count INTEGER;
BEGIN
    IF TG_OP = 'DELETE' AND NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT count(*) INTO changed_count FROM (SELECT 1 FROM inserted_hashes LIMIT 2) changed;
        IF changed_count = 0 THEN
            RETURN NULL;
        ELSIF changed_count = 1 THEN
            INSERT INTO duplicate_pending(dataset_id,block_id)
            SELECT s.dataset_id,h.sample_id::bigint/1024
            FROM inserted_hashes h JOIN samples s ON s.id=h.sample_id ON CONFLICT DO NOTHING;
        ELSE
            EXECUTE $q$INSERT INTO duplicate_pending(dataset_id,block_id)
                SELECT DISTINCT s.dataset_id,h.sample_id::bigint/1024
                FROM inserted_hashes h JOIN samples s ON s.id=h.sample_id
                ON CONFLICT DO NOTHING$q$;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        SELECT count(*) INTO changed_count FROM (SELECT 1 FROM deleted_hashes LIMIT 2) changed;
        IF changed_count = 0 THEN
            RETURN NULL;
        ELSIF changed_count = 1 THEN
            INSERT INTO duplicate_pending(dataset_id,block_id)
            SELECT s.dataset_id,h.sample_id::bigint/1024
            FROM deleted_hashes h JOIN samples s ON s.id=h.sample_id ON CONFLICT DO NOTHING;
        ELSE
            EXECUTE $q$INSERT INTO duplicate_pending(dataset_id,block_id)
                SELECT DISTINCT s.dataset_id,h.sample_id::bigint/1024
                FROM deleted_hashes h JOIN samples s ON s.id=h.sample_id
                ON CONFLICT DO NOTHING$q$;
        END IF;
    ELSE
        SELECT count(*) INTO changed_count FROM (SELECT 1 FROM inserted_hashes LIMIT 2) changed;
        IF changed_count = 0 THEN
            RETURN NULL;
        ELSIF changed_count = 1 THEN
            IF EXISTS (SELECT 1 FROM inserted_hashes n,deleted_hashes o
                       WHERE (n.sample_id,n.field_name,n.hash_hex)
                           IS DISTINCT FROM (o.sample_id,o.field_name,o.hash_hex)) THEN
                INSERT INTO duplicate_pending(dataset_id,block_id)
                SELECT DISTINCT s.dataset_id,h.sample_id::bigint/1024 FROM (
                    SELECT sample_id FROM deleted_hashes UNION SELECT sample_id FROM inserted_hashes
                ) h JOIN samples s ON s.id=h.sample_id ON CONFLICT DO NOTHING;
            END IF;
        ELSE
            EXECUTE $q$INSERT INTO duplicate_pending(dataset_id,block_id)
                SELECT DISTINCT s.dataset_id,h.sample_id::bigint/1024 FROM (
                    (SELECT sample_id,field_name,hash_hex FROM deleted_hashes
                     EXCEPT SELECT sample_id,field_name,hash_hex FROM inserted_hashes)
                    UNION
                    (SELECT sample_id,field_name,hash_hex FROM inserted_hashes
                     EXCEPT SELECT sample_id,field_name,hash_hex FROM deleted_hashes)
                ) h JOIN samples s ON s.id=h.sample_id ON CONFLICT DO NOTHING$q$;
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION record_duplicate_sample_work()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' AND NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'DELETE' THEN
        EXECUTE $q$INSERT INTO duplicate_pending(dataset_id,block_id)
            SELECT DISTINCT s.dataset_id,s.id::bigint/1024
            FROM deleted_samples s JOIN datasets d ON d.id=s.dataset_id
            ON CONFLICT DO NOTHING$q$;
    ELSE
        INSERT INTO duplicate_pending(dataset_id,block_id)
            SELECT DISTINCT s.dataset_id,s.id::bigint/1024 FROM (
                VALUES (OLD.id,OLD.dataset_id),(NEW.id,NEW.dataset_id)
            ) s(id,dataset_id) JOIN datasets d ON d.id=s.dataset_id ON CONFLICT DO NOTHING;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION record_duplicate_truncate()
RETURNS TRIGGER AS $$
BEGIN
    IF NULLIF(current_setting('fridge.deletion_dataset', TRUE), '') IS NOT NULL THEN
        RETURN NULL;
    END IF;
    INSERT INTO duplicate_pending(dataset_id,block_id)
    SELECT id,-1 FROM datasets WHERE NOT deleting ON CONFLICT DO NOTHING;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER duplicate_hashes_inserted AFTER INSERT ON sample_hashes
REFERENCING NEW TABLE AS inserted_hashes FOR EACH STATEMENT EXECUTE FUNCTION record_duplicate_hash_work();
CREATE OR REPLACE TRIGGER duplicate_hashes_deleted AFTER DELETE ON sample_hashes
REFERENCING OLD TABLE AS deleted_hashes FOR EACH STATEMENT EXECUTE FUNCTION record_duplicate_hash_work();
CREATE OR REPLACE TRIGGER duplicate_hashes_updated AFTER UPDATE ON sample_hashes
REFERENCING OLD TABLE AS deleted_hashes NEW TABLE AS inserted_hashes
FOR EACH STATEMENT EXECUTE FUNCTION record_duplicate_hash_work();
CREATE OR REPLACE TRIGGER duplicate_samples_deleted AFTER DELETE ON samples
REFERENCING OLD TABLE AS deleted_samples FOR EACH STATEMENT EXECUTE FUNCTION record_duplicate_sample_work();
CREATE OR REPLACE TRIGGER duplicate_samples_updated AFTER UPDATE OF dataset_id ON samples
FOR EACH ROW WHEN (OLD.dataset_id IS DISTINCT FROM NEW.dataset_id)
EXECUTE FUNCTION record_duplicate_sample_work();
CREATE OR REPLACE TRIGGER duplicate_hashes_truncated AFTER TRUNCATE ON sample_hashes
FOR EACH STATEMENT EXECUTE FUNCTION record_duplicate_truncate();
CREATE OR REPLACE TRIGGER duplicate_samples_truncated AFTER TRUNCATE ON samples
FOR EACH STATEMENT EXECUTE FUNCTION record_duplicate_truncate();

CREATE INDEX IF NOT EXISTS idx_duplicate_jobs ON jobs(dataset_id,created_at DESC,id DESC)
    WHERE kind='duplicate_maintenance';
DROP INDEX IF EXISTS idx_jobs_dataset_history;
DROP INDEX IF EXISTS idx_statistics_history_retention;
CREATE INDEX idx_jobs_dataset_history ON jobs(dataset_id,created_at DESC,id DESC)
    WHERE NOT (kind IN ('statistics','concept_scoring','duplicate_maintenance','map_place')
               AND dedupe_owner='system' AND state='succeeded' AND NOT barrier @> '{"full":true}');
CREATE INDEX idx_statistics_history_retention ON jobs(finished_at,id)
    WHERE kind IN ('statistics','concept_scoring','duplicate_maintenance','map_place')
          AND dedupe_owner='system' AND state='succeeded' AND NOT barrier @> '{"full":true}';

-- Dataset deletion lifecycle.
CREATE OR REPLACE FUNCTION require_writable_dataset(target_dataset INTEGER, deleting_rows BOOLEAN DEFAULT FALSE)
RETURNS VOID AS $$
DECLARE
    removing BOOLEAN;
BEGIN
    IF target_dataset IS NULL THEN RETURN; END IF;
    -- Only the fenced deletion callback sets this transaction-local value.
    IF deleting_rows AND current_setting('fridge.deletion_dataset', TRUE) = target_dataset::text THEN
        RETURN;
    END IF;
    -- This transaction already holds the lock below on this writable dataset, so it cannot be sealed.
    IF current_setting('fridge.writable_dataset', TRUE) = target_dataset::text THEN
        RETURN;
    END IF;
    -- Compatible with ordinary metadata updates; deletion seals with FOR UPDATE first.
    -- Wait under the caller's existing timeouts, and reject stale repeatable-read snapshots.
    SELECT deleting INTO removing FROM datasets WHERE id = target_dataset FOR KEY SHARE;
    IF removing THEN
        RAISE EXCEPTION 'Dataset is being deleted; writes are disabled. Resume deletion to finish.'
            USING ERRCODE = '55000';
    END IF;
    IF FOUND THEN
        -- Transaction-local like the lock: rolling back a subtransaction releases both.
        PERFORM set_config('fridge.writable_dataset', target_dataset::text, TRUE);
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION guard_dataset_row()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM require_writable_dataset(OLD.id, TG_OP = 'DELETE');
    IF TG_OP = 'UPDATE' AND NEW.deleting THEN
        -- The seal: later writes in this transaction must check again.
        PERFORM set_config('fridge.writable_dataset', '', TRUE);
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION guard_dataset_rows()
RETURNS TRIGGER AS $$
DECLARE
    source TEXT;
    targets TEXT[] := '{}';
    target INTEGER;
    query TEXT;
BEGIN
    -- Temporary query results are not dataset writes, including creation and TTL cleanup.
    -- Also keep old installations cheap until migration replay removes these triggers.
    IF TG_TABLE_NAME IN ('region_selections', 'quality_dup_pairs') THEN RETURN NULL; END IF;
    IF TG_OP <> 'INSERT' THEN targets := array_append(targets, 'deletion_old_rows'); END IF;
    IF TG_OP <> 'DELETE' THEN targets := array_append(targets, 'deletion_new_rows'); END IF;
    FOREACH source IN ARRAY targets LOOP
        query := CASE TG_ARGV[0]
            WHEN 'sample' THEN format('SELECT DISTINCT parent.dataset_id FROM (SELECT DISTINCT sample_id FROM %I) changed JOIN samples parent ON parent.id=changed.sample_id', source)
            WHEN 'version' THEN format('SELECT DISTINCT parent.dataset_id FROM (SELECT DISTINCT version_id FROM %I) changed JOIN dataset_versions parent ON parent.id=changed.version_id', source)
            WHEN 'concept' THEN format('SELECT DISTINCT parent.dataset_id FROM (SELECT DISTINCT concept_id FROM %I) changed JOIN embedding_concepts parent ON parent.id=changed.concept_id', source)
            WHEN 'pair' THEN format('SELECT DISTINCT parent.dataset_id FROM %I changed JOIN samples parent ON parent.id IN (changed.a,changed.b)', source)
            ELSE format('SELECT DISTINCT dataset_id FROM %I', source)
        END;
        FOR target IN EXECUTE query || ' ORDER BY 1' LOOP
            PERFORM require_writable_dataset(target, TG_OP = 'DELETE');
        END LOOP;
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- UPDATE guards are row triggers: a statement transition table would copy every old and
-- new row of a whole-dataset update to temporary files. Dataset-keyed rows run this only
-- until the transaction holds its dataset's lock (the trigger's WHEN). Other rows need a
-- parent lookup, so they run it only for moves or under guard_dataset_update_scope.
CREATE OR REPLACE FUNCTION guard_dataset_update()
RETURNS TRIGGER AS $$
DECLARE
    owners INTEGER[];
    target INTEGER;
BEGIN
    IF TG_ARGV[0] = 'dataset' THEN
        owners := ARRAY[OLD.dataset_id, NEW.dataset_id];
    ELSIF TG_ARGV[0] = 'sample' THEN
        owners := ARRAY(SELECT dataset_id FROM samples WHERE id IN (OLD.sample_id, NEW.sample_id));
    ELSIF TG_ARGV[0] = 'concept' THEN
        owners := ARRAY(SELECT dataset_id FROM embedding_concepts WHERE id IN (OLD.concept_id, NEW.concept_id));
    ELSE
        owners := ARRAY(SELECT dataset_id FROM dataset_versions WHERE id IN (OLD.version_id, NEW.version_id));
    END IF;
    FOREACH target IN ARRAY owners LOOP
        PERFORM require_writable_dataset(target);
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Check parent-keyed rows while a deletion is visible to the statement, and always in an
-- older (repeatable-read) snapshot, which may predate a seal. An in-place update that
-- started before a concurrent seal committed only changes rows the deletion removes; any
-- work it queues still passes the locking INSERT/DELETE guards.
CREATE OR REPLACE FUNCTION guard_dataset_update_scope()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM set_config('fridge.guard_parent_rows', (
        current_setting('transaction_isolation') <> 'read committed'
        OR EXISTS (SELECT 1 FROM datasets WHERE deleting))::text, TRUE);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER dataset_write_guard BEFORE UPDATE OR DELETE ON datasets
FOR EACH ROW EXECUTE FUNCTION guard_dataset_row();

DO $$
DECLARE
    item RECORD;
    owner TEXT;
BEGIN
    FOR item IN SELECT * FROM (VALUES
        ('embedding_coordinates', 'sample'),
        ('embedding_projection_pending', 'dataset'),
        ('duplicate_configs', 'dataset'),
        ('duplicate_members', 'dataset'),
        ('duplicate_pending', 'dataset'),
        ('dataset_access', 'dataset'),
        ('samples', 'dataset'),
        ('dataset_fields', 'dataset'),
        ('embedding_viz_rank', 'dataset'),
        ('embedding_viz_meta', 'dataset'),
        ('views', 'dataset'),
        ('dataset_field_stats', 'dataset'),
        ('sample_dup_secondary', 'dataset'),
        ('embedding_concepts', 'dataset'),
        ('concept_embedding_pending', 'dataset'),
        ('dataset_versions', 'dataset'),
        ('dataset_tags', 'dataset'),
        ('dataset_stats_pending', 'dataset'),
        ('dataset_stats_state', 'dataset'),
        ('dataset_stats_blocks', 'dataset'),
        ('dataset_stats_parts', 'dataset'),
        ('dataset_block_field_stats', 'dataset'),
        ('embeddings', 'sample'),
        ('sample_hashes', 'sample'),
        ('version_snapshots', 'version'),
        ('embedding_concept_scores', 'concept'),
        ('concept_score_pending', 'concept'),
        ('concept_score_count_deltas', 'concept')
    ) guarded(table_name, kind) LOOP
        owner := item.kind || '_id';
        EXECUTE format('CREATE OR REPLACE TRIGGER deletion_guard_insert AFTER INSERT ON %I REFERENCING NEW TABLE AS deletion_new_rows FOR EACH STATEMENT EXECUTE FUNCTION guard_dataset_rows(%L)', item.table_name, item.kind);
        IF item.kind = 'dataset' THEN
            EXECUTE format('CREATE OR REPLACE TRIGGER deletion_guard_update BEFORE UPDATE ON %I FOR EACH ROW WHEN (OLD.dataset_id IS DISTINCT FROM NEW.dataset_id OR NEW.dataset_id::text IS DISTINCT FROM current_setting(''fridge.writable_dataset'', TRUE)) EXECUTE FUNCTION guard_dataset_update(%L)', item.table_name, item.kind);
        ELSE
            EXECUTE format('CREATE OR REPLACE TRIGGER deletion_guard_update_scope BEFORE UPDATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION guard_dataset_update_scope()', item.table_name);
            EXECUTE format('CREATE OR REPLACE TRIGGER deletion_guard_update BEFORE UPDATE ON %1$I FOR EACH ROW WHEN (OLD.%2$I IS DISTINCT FROM NEW.%2$I OR current_setting(''fridge.guard_parent_rows'', TRUE) = ''true'') EXECUTE FUNCTION guard_dataset_update(%3$L)', item.table_name, owner, item.kind);
        END IF;
        EXECUTE format('CREATE OR REPLACE TRIGGER deletion_guard_delete AFTER DELETE ON %I REFERENCING OLD TABLE AS deletion_old_rows FOR EACH STATEMENT EXECUTE FUNCTION guard_dataset_rows(%L)', item.table_name, item.kind);
    END LOOP;
END;
$$;

-- A logical backup carries this mark into the restored cluster.
CREATE TABLE IF NOT EXISTS transaction_high_water (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    transaction_id BIGINT NOT NULL,
    system_identifier BIGINT NOT NULL
);
-- Bootstrap also recognizes older backups which predate the mark. These are pending
-- queues and saved boundaries, never a scan of the samples or embeddings.
INSERT INTO transaction_high_water(singleton,transaction_id,system_identifier)
SELECT TRUE,GREATEST(txid_current(),
    (SELECT max(transaction_id) FROM dataset_stats_pending),
    (SELECT max(transaction_id) FROM concept_embedding_pending),
    (SELECT max(transaction_id) FROM concept_score_pending),
    (SELECT max(transaction_id) FROM concept_score_count_deltas),
    (SELECT max(transaction_id) FROM duplicate_pending),
    (SELECT max(NULLIF(split_part(barrier->>'snapshot',':',2),'')::bigint) FROM jobs
        WHERE kind IN ('statistics','concept_scoring','duplicate_maintenance')
            AND state IN ('queued','running','failed','cancelled')),
    (SELECT max(GREATEST(score_definition_transaction,split_part(score_manual_snapshot,':',2)::bigint))
        FROM embedding_concepts)),(SELECT system_identifier FROM pg_control_system())
ON CONFLICT (singleton) DO NOTHING;

-- Read-only claims never allocate an xid or contend with the recovery transaction.
CREATE OR REPLACE FUNCTION restored_work_pending()
RETURNS BOOLEAN AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM transaction_high_water
        WHERE singleton AND system_identifier=(SELECT system_identifier FROM pg_control_system())
          AND transaction_id<txid_snapshot_xmax(txid_current_snapshot())
    );
$$ LANGUAGE sql;

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
            concept_score_count_deltas,duplicate_pending,embedding_concepts
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
        WHERE kind IN ('statistics','concept_scoring','duplicate_maintenance') AND state IN ('queued','running');
        -- Failed/cancelled work can be resumed by a later explicit request too.
        UPDATE jobs SET barrier='{}',checkpoint='{}'
        WHERE kind IN ('statistics','concept_scoring','duplicate_maintenance') AND state IN ('failed','cancelled');
    END IF;
    INSERT INTO transaction_high_water(singleton,transaction_id,system_identifier)
    VALUES(TRUE,current_transaction,current_system)
    ON CONFLICT (singleton) DO UPDATE SET transaction_id=EXCLUDED.transaction_id,
        system_identifier=EXCLUDED.system_identifier;
    RETURN restored;
END;
$$ LANGUAGE plpgsql;


CREATE INDEX IF NOT EXISTS idx_region_selections_expiry ON region_selections(created_at, token);

CREATE TABLE IF NOT EXISTS bulk_job_targets (
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL,
    patch JSONB,
    PRIMARY KEY (job_id, sample_id)
);
CREATE TABLE IF NOT EXISTS bulk_selection_pins (
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    PRIMARY KEY (job_id, token)
);
CREATE INDEX IF NOT EXISTS idx_bulk_selection_pins_token ON bulk_selection_pins(token);

-- Dataset schedules and the scheduled map re-fit.
-- Recurring jobs for one dataset (its Jobs panel → Schedules). The runner's scheduler queues a job
-- when next_run_at passes and the job type's conditions hold. Times are wall-clock times in
-- `timezone`, so a daily 03:00 stays at 03:00 across daylight-saving changes.
CREATE TABLE IF NOT EXISTS dataset_job_schedules (
    id SERIAL PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL CHECK (job_type IN ('map_refit', 'index_review')),
    target TEXT NOT NULL CHECK (length(target) > 0),  -- map_refit: the embedding field; index_review: 'indexes'
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    frequency TEXT NOT NULL DEFAULT 'weekly' CHECK (frequency IN ('daily', 'weekly')),
    weekday SMALLINT DEFAULT 0 CHECK (weekday BETWEEN 0 AND 6),  -- weekly only; 0 = Sunday (the default)
    time_of_day TIME NOT NULL DEFAULT '03:00',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    min_new_samples INTEGER CHECK (min_new_samples > 0),  -- NULL: no sample condition
    -- NULL: no minimum gap. With one, next_run_at goes straight to the first time the gap allows.
    min_days_between INTEGER CHECK (min_days_between > 0),
    -- A due run starts only within this many minutes of its time; later, it is skipped and recorded.
    window_minutes INTEGER NOT NULL DEFAULT 180 CHECK (window_minutes > 0),
    next_run_at TIMESTAMPTZ,  -- NULL while disabled
    last_run_at TIMESTAMPTZ,
    last_job_id UUID,  -- no FK, like the map state below: a missing job reads as finished
    last_result JSONB,
    -- The scheduler's last decision at a due time, e.g. why it did not run.
    last_check JSONB,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (dataset_id, job_type, target),
    CHECK ((frequency = 'weekly') = (weekday IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_dataset_job_schedules_due ON dataset_job_schedules(next_run_at) WHERE enabled;

-- The map (UMAP model + coordinates) of one embedding field. A row appears with the first managed
-- re-fit; fields without one keep their legacy saved model. model_id names the immutable model file
-- that produced the current coordinates; projection uses only that model. active_job_id marks a
-- re-fit in progress: projection for the field pauses while that job is queued or running.
CREATE TABLE IF NOT EXISTS embedding_map_state (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    model_id UUID,
    artifact_location TEXT,  -- key inside the model store (FRIDGE_UMAP_MODEL_DIR)
    artifact_checksum TEXT,  -- sha256 of the model file
    artifact_bytes BIGINT,
    dimension INTEGER,
    fit_size INTEGER,
    refitted_at TIMESTAMPTZ,
    -- embedding_viz_meta.total_points when the current map was published.
    points_at_refit BIGINT NOT NULL DEFAULT 0,
    -- The fit's measured memory per fit point (peak anonymous memory beyond the vectors), from the
    -- last fit, or raised after an out-of-memory kill; sizes the next fit. NULL: the formula's default.
    fit_bytes_per_point BIGINT CHECK (fit_bytes_per_point > 0),
    active_job_id UUID,  -- no FK: a job that no longer exists is not active
    active_since TIMESTAMPTZ,
    PRIMARY KEY (dataset_id, field_name),
    -- A map may have no model file (it places new samples by neighbours); a model file needs its model id.
    CONSTRAINT embedding_map_state_artifact_needs_model CHECK (artifact_location IS NULL OR model_id IS NOT NULL)
);

-- A lasso selection is a polygon in one map's coordinates (`embedding_polygon` filters). Once a field's
-- map is re-fitted, the same polygon would silently select different samples: refuse it (409) unless it
-- was drawn on the field's current map. Fields without a re-fitted map keep accepting any polygon.
CREATE OR REPLACE FUNCTION require_current_map(p_dataset_id INTEGER, p_field TEXT, p_model_id TEXT)
RETURNS BOOLEAN AS $$
DECLARE
    current_model UUID;
    refitted TIMESTAMPTZ;
BEGIN
    SELECT model_id, refitted_at INTO current_model, refitted
    FROM embedding_map_state WHERE dataset_id = p_dataset_id AND field_name = p_field;
    IF current_model IS NULL OR current_model::text = p_model_id THEN
        RETURN TRUE;
    END IF;
    -- DETAIL is read by the API (a 409 with code map_refitted), so the UI can drop the stale lasso.
    RAISE EXCEPTION 'The map of % was re-fitted on % UTC after this lasso selection was drawn; draw the selection again on the current map (API: send its model_id from map_state)',
        p_field, to_char(refitted AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI')
        USING ERRCODE = '55000',
              DETAIL = json_build_object('code', 'map_refitted', 'dataset_id', p_dataset_id, 'field', p_field,
                                         'model_id', current_model)::text;
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;

-- Index advisor.

-- How often each dataset field is filtered, sorted, faceted or histogrammed, and how long it took, per
-- UTC day. field_name '' is a request with no field in that role (the dataset's unfiltered baseline).
-- latency_log2[k] counts requests that took [2^(k-1), 2^k) ms (k = 0: under 1 ms; the last: 16 s and
-- more). API workers add their counts once a minute; rows older than the review window are pruned.
CREATE TABLE IF NOT EXISTS field_usage_daily (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    usage TEXT NOT NULL CHECK (usage IN ('filter', 'sort', 'facet', 'histogram')),
    field_name TEXT NOT NULL,
    day DATE NOT NULL,
    uses BIGINT NOT NULL CHECK (uses >= 0),
    total_ms DOUBLE PRECISION NOT NULL CHECK (total_ms >= 0),
    latency_log2 BIGINT[] NOT NULL CHECK (cardinality(latency_log2) = 16),
    PRIMARY KEY (dataset_id, usage, field_name, day)
);
CREATE INDEX IF NOT EXISTS idx_field_usage_daily_day ON field_usage_daily(day);

-- Indexes an administrator removed through the index advisor, with what it takes to restore them
-- (pg_get_indexdef). An open row (not restored, not closed) is an intentional removal: the advisor's
-- missing-core-index check honours it. A row is closed when its index is re-created another way.
CREATE TABLE IF NOT EXISTS removed_indexes (
    id SERIAL PRIMARY KEY,
    index_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    definition TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('core', 'field', 'ann', 'array', 'other')),
    dataset_id INTEGER REFERENCES datasets(id) ON DELETE CASCADE,  -- field indexes
    field_name TEXT,
    size_bytes BIGINT,
    scans BIGINT,
    last_scan TIMESTAMPTZ,
    observed_since TIMESTAMPTZ,
    reason TEXT,
    removed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    removed_by_name TEXT,
    removed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    dropped_at TIMESTAMPTZ,  -- NULL until the DROP INDEX CONCURRENTLY finished
    drop_job_id UUID,
    restored_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    restored_by_name TEXT,
    restored_at TIMESTAMPTZ,
    restore_job_id UUID,
    closed_at TIMESTAMPTZ,
    closed_note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_removed_indexes_open ON removed_indexes(index_name)
    WHERE restored_at IS NULL AND closed_at IS NULL;

-- When the advisor first observed each physical index (its OID in this database of this cluster), so
-- an index is never called unused for longer than it has been watched. Never back-dated: the rows are
-- seeded with the time this migration runs, and an index without a matching row (new, or the database
-- was restored from a dump: other OIDs, another cluster or database) starts its window when first seen.
-- last_scans detects a counter that went backwards (a per-table statistics reset).
CREATE TABLE IF NOT EXISTS index_observations (
    index_oid OID NOT NULL,
    system_identifier BIGINT NOT NULL,
    database_oid OID NOT NULL,
    index_name TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_scans BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (index_oid, system_identifier, database_oid)
);
INSERT INTO index_observations (index_oid, system_identifier, database_oid, index_name, first_seen_at, last_scans)
SELECT c.oid, (SELECT system_identifier FROM pg_control_system()),
       (SELECT oid FROM pg_database WHERE datname = current_database()), c.relname, now(), COALESCE(s.idx_scan, 0)
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = c.oid
WHERE c.relnamespace = current_schema()::regnamespace
ON CONFLICT DO NOTHING;

-- Recurring jobs that belong to no dataset; one row per job type. The same rule columns and wall-clock
-- semantics as dataset_job_schedules; the dataset scheduler pass handles both.
CREATE TABLE IF NOT EXISTS system_job_schedules (
    job_type TEXT PRIMARY KEY CHECK (job_type IN ('index_review')),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    frequency TEXT NOT NULL DEFAULT 'weekly' CHECK (frequency IN ('daily', 'weekly')),
    weekday SMALLINT DEFAULT 0 CHECK (weekday BETWEEN 0 AND 6),  -- weekly only; 0 = Sunday
    -- 06:00: when the default map re-fit's start window (Sunday 03:00 + 180 minutes) closes, keeping
    -- the two defaults apart (dataset_schedules.review_default_slot; a test keeps them equal).
    time_of_day TIME NOT NULL DEFAULT '06:00',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    next_run_at TIMESTAMPTZ,  -- NULL while disabled
    last_run_at TIMESTAMPTZ,
    last_job_id UUID,
    last_check JSONB,
    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK ((frequency = 'weekly') = (weekday IS NOT NULL))
);
INSERT INTO system_job_schedules (job_type) VALUES ('index_review') ON CONFLICT (job_type) DO NOTHING;

-- The follower change log (migration 0007): what changed in a followed dataset, stamped with the writing
-- transaction for a feed in commit order. Samples are logged by block from the samples statistics trigger
-- (mark_dataset_stats_stale, above), through sample_block_changed below.
CREATE TABLE IF NOT EXISTS sample_block_changes (
    dataset_id INTEGER NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current(),
    block_id BIGINT NOT NULL,
    PRIMARY KEY (dataset_id, transaction_id, block_id)
);

CREATE TABLE IF NOT EXISTS vector_changes (
    id BIGSERIAL PRIMARY KEY,
    dataset_id INTEGER NOT NULL,
    sample_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('vector', 'hash')),
    field_name TEXT NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current()
);
CREATE INDEX IF NOT EXISTS vector_changes_feed ON vector_changes (dataset_id, transaction_id, id);

CREATE TABLE IF NOT EXISTS dataset_changes (
    id BIGSERIAL PRIMARY KEY,
    dataset_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fields', 'settings', 'views', 'deleted')),
    transaction_id BIGINT NOT NULL DEFAULT txid_current()
);
CREATE INDEX IF NOT EXISTS dataset_changes_feed ON dataset_changes (dataset_id, transaction_id, id);

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

-- Follower instances and what they follow (migration 0008): see routers/followers.py.
CREATE TABLE IF NOT EXISTS followers (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_id INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS follower_datasets (
    follower_id INTEGER NOT NULL REFERENCES followers(id) ON DELETE CASCADE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    tags TEXT NOT NULL DEFAULT 'primary' CHECK (tags IN ('primary', 'both')),
    views TEXT NOT NULL DEFAULT 'primary' CHECK (views IN ('primary', 'both')),
    -- Set just after the subscription commits: changes from transactions at or after it are in the log, and the
    -- follower's full copy starts once every earlier one has ended (a write in flight when the dataset became
    -- followed may not have seen the flag). NULL until then.
    start_txid BIGINT,
    acked_txid BIGINT,
    subscribed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (follower_id, dataset_id)
);
CREATE INDEX IF NOT EXISTS follower_datasets_dataset ON follower_datasets (dataset_id);

CREATE OR REPLACE FUNCTION sync_dataset_followed() RETURNS trigger AS $$
BEGIN
    UPDATE datasets d SET followed = EXISTS (SELECT 1 FROM follower_datasets f WHERE f.dataset_id = d.id)
    WHERE d.id IN (SELECT dataset_id FROM changed)
      AND d.followed IS DISTINCT FROM EXISTS (SELECT 1 FROM follower_datasets f WHERE f.dataset_id = d.id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER follower_datasets_followed_insert AFTER INSERT ON follower_datasets REFERENCING NEW TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION sync_dataset_followed();
CREATE TRIGGER follower_datasets_followed_delete AFTER DELETE ON follower_datasets REFERENCING OLD TABLE AS changed
    FOR EACH STATEMENT EXECUTE FUNCTION sync_dataset_followed();

-- Ledger of applied migrations: the 1.0.0 baseline, then the files in backend/db/migrations/
-- (0001_..., 0002_...) that scripts/apply_migrations.sh applies to existing databases. A fresh install
-- from this file IS current: when you add a migration, make the same change here and ADD ITS
-- FILENAME to this list (backend/tests/test_schema_check.py fails if the lists differ).
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations (filename) VALUES
    ('0000_baseline.sql'),
    ('0001_storage_config_dataset_scope.sql'),
    ('0002_dataset_default_view.sql'),
    ('0003_compute_worker_tokens.sql'),
    ('0004_map_state_without_model.sql'),
    ('0005_map_placement_history.sql'),
    ('0006_map_placement_hold.sql'),
    ('0007_follower_change_log.sql'),
    ('0008_followers.sql')
ON CONFLICT (filename) DO NOTHING;
