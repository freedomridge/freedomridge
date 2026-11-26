-- What each field of a dataset means: a description (at most 1,000 characters) and a unit (at most 40), written by
-- people with edit in the dataset (PATCH /datasets/{id}/fields/{field}/description), NULL for none. values_private
-- marks a field whose values aren't given to the assistant; it belongs to this instance, like is_promoted. A field
-- with no row of its own (id, tags, ...) gets one with origin 'system' when it is described
-- (backend/field_descriptions.py); such a row queues no statistics (mark_field_stats_stale).
--
-- On a mirror, the description and unit of the primary's fields are the primary's: they arrive with its field
-- registry and change only through the sync (guard_mirror_fields). values_private is the mirror's own, on any field:
-- a field with no row there gets an empty 'system' row to hold it, the one row the guard lets the mirror add. A
-- version saves descriptions and units with the field definitions and a revert restores them
-- (normalize_dataset_snapshot, dataset_snapshots_equal).
--
-- The new columns are NULL (values_private FALSE) on every row: no row is rewritten and no trigger fires.
SET LOCAL lock_timeout = '2s';

ALTER TABLE dataset_fields
    ADD COLUMN description TEXT
        CONSTRAINT dataset_fields_description_check CHECK (char_length(description) BETWEEN 1 AND 1000),
    ADD COLUMN unit TEXT CONSTRAINT dataset_fields_unit_check CHECK (char_length(unit) BETWEEN 1 AND 40),
    ADD COLUMN values_private BOOLEAN NOT NULL DEFAULT FALSE;

-- A field marked private keeps its flag when its definition goes: the row is emptied to origin 'system' instead of
-- deleted, and a definition of the name coming back takes it over (field_registry.forget_definitions). A mirror's
-- own `@local.` fields too, so their names may be 'system' rows. The check reads every row: dataset_fields is small.
ALTER TABLE dataset_fields DROP CONSTRAINT dataset_fields_local_check;
ALTER TABLE dataset_fields ADD CONSTRAINT dataset_fields_local_check
    CHECK (origin = 'system' OR starts_with(field_name, '@local.') = (origin = 'local'));

CREATE OR REPLACE FUNCTION guard_mirror_fields() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (OLD.dataset_id, OLD.field_name, OLD.field_type, OLD.origin, OLD.display_type,
                             OLD.overlay_config, OLD.description, OLD.unit)
        IS NOT DISTINCT FROM (NEW.dataset_id, NEW.field_name, NEW.field_type, NEW.origin, NEW.display_type,
                              NEW.overlay_config, NEW.description, NEW.unit) THEN
        RETURN NULL;
    END IF;
    -- (0035) A row that says nothing about the field but holds this instance's values_private: what a mirror's
    -- private flag on a field its primary gave no row (id, filepath, ...) is stored in.
    IF TG_OP = 'INSERT' AND NEW.origin = 'system' AND NEW.field_type = 'unknown' AND NOT NEW.is_promoted
       AND (NEW.display_type, NEW.overlay_config, NEW.description, NEW.unit) IS NOT DISTINCT FROM
           (NULL::text, NULL::jsonb, NULL::text, NULL::text) THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' AND current_setting('fridge.deletion_dataset', TRUE) = OLD.dataset_id::text THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' AND NOT starts_with(OLD.field_name, '@local.') THEN
        PERFORM require_mirror_sync(OLD.dataset_id);
    END IF;
    IF TG_OP <> 'DELETE' AND NOT starts_with(NEW.field_name, '@local.') THEN
        PERFORM require_mirror_sync(NEW.dataset_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- The row a built-in field is given when it is described queues no statistics: nothing in it is measured.
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
    -- (0035) A built-in field's row (origin 'system') holds only what is written about it: nothing to measure. A field
    -- that becomes one (a mirror's private flag kept once its primary removed the field) goes as if deleted.
    IF COALESCE(CASE WHEN TG_OP <> 'INSERT' THEN OLD.origin END, 'system') = 'system'
       AND COALESCE(CASE WHEN TG_OP <> 'DELETE' THEN NEW.origin END, 'system') = 'system' THEN
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
                    CASE WHEN field->>'field_type' = 'array' THEN 'native_array' ELSE 'metadata' END),
                'description', field->>'description',
                'unit', field->>'unit'
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
                      OR COALESCE(lhs_field, rhs_field)->>'description' IS NOT NULL
                      OR COALESCE(lhs_field, rhs_field)->>'unit' IS NOT NULL
                    ELSE TRUE
                END
        )
    END FROM normalized
$$;
