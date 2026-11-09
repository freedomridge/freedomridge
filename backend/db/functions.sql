-- GENERATED from backend/db/schema.sql — do not edit by hand.
-- Regenerate after changing a function in schema.sql:
--     python -m backend.schema_check --emit-functions > backend/db/functions.sql
-- scripts/apply_migrations.sh re-applies this file at the end of every run (CREATE OR REPLACE is
-- idempotent), so a function body that drifted from schema.sql — a re-executed old migration, a
-- hand edit — is restored by the next run. GET /api/health reports the drift (stale_functions).

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

CREATE OR REPLACE FUNCTION embeddings_reject_legacy_coordinates() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'coordinates live in embedding_coordinates, not embeddings.x/y';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION coordinates_skip_unchanged() RETURNS trigger AS $$
BEGIN
    IF NEW IS NOT DISTINCT FROM OLD THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

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

CREATE OR REPLACE FUNCTION refresh_stale_datasets_stats()
RETURNS TABLE(dataset_id INTEGER, fields_updated INTEGER, error TEXT) AS $$
BEGIN
    RAISE EXCEPTION 'Statistics now run as durable jobs. Use POST /api/datasets/{id}/refresh-stats or the SDK.'
        USING ERRCODE = '0A000';
END;
$$ LANGUAGE plpgsql;

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

CREATE OR REPLACE FUNCTION remove_concept_score_work()
RETURNS TRIGGER AS $$
BEGIN
    DELETE FROM concept_score_pending WHERE concept_id=OLD.id;
    DELETE FROM concept_score_count_deltas WHERE concept_id=OLD.id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

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

CREATE OR REPLACE FUNCTION invalidate_concept_scores()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE embedding_concepts SET score_initialized = FALSE, score_revision = score_revision + 1;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

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

CREATE OR REPLACE FUNCTION ensure_single_default_storage()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.is_default = true THEN
        UPDATE storage_configs SET is_default = false WHERE id != NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION storage_configs_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION initialize_job_controls()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO job_controls(job_id) VALUES (NEW.id) ON CONFLICT DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

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

CREATE OR REPLACE FUNCTION mark_full_stats_request()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.stats_full_requests IS DISTINCT FROM OLD.stats_full_requests THEN
        PERFORM record_stats_work(NEW.id, -1, NULL);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION stats_datetime(value TEXT)
RETURNS TIMESTAMPTZ AS $$
    SELECT CASE
        WHEN value ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$'
             AND pg_input_is_valid(value, 'timestamp with time zone')
        THEN value::timestamptz
    END;
$$ LANGUAGE sql IMMUTABLE STRICT SET timezone = 'UTC';

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

CREATE OR REPLACE FUNCTION guard_dataset_update_scope()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM set_config('fridge.guard_parent_rows', (
        current_setting('transaction_isolation') <> 'read committed'
        OR EXISTS (SELECT 1 FROM datasets WHERE deleting))::text, TRUE);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

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
