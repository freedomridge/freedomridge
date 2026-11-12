-- 2.11.0: deleting an image deletes its objects. samples.parent_id (0029) gets its key: (dataset_id, parent_id) names a
-- sample of the same dataset, and deleting that sample deletes the objects that name it, whatever deletes it (the
-- app's paths delete them first themselves, to count them and keep the field caches right; the key holds for every
-- other one, psql included). The (dataset_id, id) key it references is 0021's unique index, and the cascade finds an
-- image's objects by 0029's (dataset_id, parent_id) index.
--
-- It runs with the app stopped, like 0029, which comes right before it in the same release: the key is checked in one
-- pass over samples while nothing writes, and adding it NOT VALID first would only help if writers stayed connected.
-- Measured on 4 million samples, 3.2 million of them objects: 2.0 s looking for stale objects (step 2), 0.3 s checking
-- the key (step 3). Progress lines start with "0030:".
SET LOCAL lock_timeout = '2s';

-- 1. Nothing else may be connected (as 0029).
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
        RAISE EXCEPTION '0030 checks every object sample''s image, so stop the app first. Still connected: %', others
            USING HINT = 'Stop every service but the database (backend, job runner, workers, backups), then run '
                         'apply_migrations.sh again.';
    END IF;
END $$;
LOCK TABLE samples IN SHARE ROW EXCLUSIVE MODE;

-- 2. An object whose column names an image deleted since 0029 linked it (a database that ran 0029 before this
-- migration existed): its image is gone, so it is deleted, like 0029's orphans, with the triggers on, so followers and
-- statistics see it, and the field caches kept as a bulk delete keeps them. On a mirror, or in a dataset being deleted,
-- nothing may be deleted here: the column is cleared instead (what the rule gives for an image that isn't there), with
-- the triggers off, on purpose: it is no change for anyone (CONTRIBUTING, rule 4). Each dataset with any says how many.
DO $$
DECLARE
    dataset RECORD;
    stale INTEGER[];
    deleted BIGINT;
    names TEXT[];
    labels TEXT[];
    affected TEXT[];
    unused TEXT[];
BEGIN
    FOR dataset IN SELECT d.id, d.name, d.deleting OR EXISTS (SELECT 1 FROM mirrors m WHERE m.dataset_id = d.id) AS kept
                     FROM datasets d ORDER BY d.id LOOP
        stale := ARRAY(
            SELECT o.id FROM samples o
             WHERE o.dataset_id = dataset.id AND o.parent_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM samples p WHERE p.dataset_id = o.dataset_id AND p.id = o.parent_id)
             ORDER BY o.id);
        IF cardinality(stale) = 0 THEN
            CONTINUE;
        END IF;
        IF dataset.kept THEN
            ALTER TABLE samples DISABLE TRIGGER USER;
            UPDATE samples SET parent_id = NULL WHERE dataset_id = dataset.id AND id = ANY(stale);
            ALTER TABLE samples ENABLE TRIGGER USER;
            RAISE NOTICE '0030: dataset % (%): % objects name an image deleted since 0029; a mirror, or a dataset being deleted, keeps them, with no image',
                dataset.id, dataset.name, cardinality(stale);
            CONTINUE;
        END IF;
        WITH removed AS (
            DELETE FROM samples WHERE dataset_id = dataset.id AND id = ANY(stale) RETURNING metadata
        )
        SELECT count(*),
               ARRAY(SELECT DISTINCT k FROM removed r, jsonb_object_keys(
                         CASE WHEN jsonb_typeof(r.metadata) = 'object' THEN r.metadata ELSE '{}' END) k)
          INTO deleted, names
          FROM removed;
        -- As 0029: invalidate_values, then prune_fields (field_registry.py).
        names := names || ARRAY['tags'];
        labels := ARRAY['detections', 'polygons', 'ground_truth', 'predictions'] || ARRAY(
            SELECT v FROM dataset_field_stats s, unnest(s.unique_values) v
             WHERE s.dataset_id = dataset.id
               AND (s.field_name IN ('__detection_fields__', '__polyline_fields__', '__keypoint_fields__')
                    OR (s.field_name = '__classification_fields__' AND v IN (
                        SELECT f.field_name FROM dataset_fields f
                         WHERE f.dataset_id = dataset.id AND f.origin <> 'system' AND f.field_type = 'list'))));
        affected := ARRAY(
            SELECT n FROM unnest(names) n
            UNION
            SELECT n || '.label' FROM unnest(names) n
             WHERE n = ANY(labels) AND NOT EXISTS (
                 SELECT 1 FROM dataset_fields f
                  WHERE f.dataset_id = dataset.id AND f.origin <> 'system' AND f.field_name = n || '.label')
            UNION
            SELECT 'segmentation.labels' WHERE 'segmentation' = ANY(names));
        INSERT INTO dataset_field_stats (dataset_id, field_name, field_type, last_updated)
        SELECT dataset.id, candidate.n, COALESCE(definition.field_type, 'enum'), NULL
          FROM unnest(names) candidate(n)
          LEFT JOIN dataset_fields definition
            ON definition.dataset_id = dataset.id AND definition.field_name = candidate.n
         WHERE definition.field_name IS NOT NULL OR candidate.n IN ('tags', 'media_type')
         ORDER BY candidate.n
        ON CONFLICT (dataset_id, field_name) DO NOTHING;
        UPDATE dataset_field_stats SET values_complete = FALSE
         WHERE dataset_id = dataset.id AND field_name = ANY(affected);
        unused := ARRAY(
            SELECT f.field_name FROM dataset_fields f
             WHERE f.dataset_id = dataset.id AND f.field_name = ANY(names) AND f.origin = 'metadata'
               AND NOT f.is_promoted AND f.display_type IS NULL AND f.overlay_config IS NULL
               AND f.field_name <> ALL (ARRAY['tags', 'media_type', 'detections.label', 'ground_truth.label',
                   'predictions.label', 'polygons.label', 'segmentation.labels', '__heatmap_fields__',
                   '__probability_fields__', '__mask_overlay_fields__', '__cuboid_fields__',
                   '__keypoint_fields__', '__detection_fields__', '__polyline_fields__',
                   '__classification_fields__'])
               AND NOT EXISTS (SELECT 1 FROM samples s
                                WHERE s.dataset_id = dataset.id AND s.metadata ? f.field_name)
             ORDER BY f.field_name);
        DELETE FROM dataset_fields WHERE dataset_id = dataset.id AND field_name = ANY(unused);
        DELETE FROM dataset_field_stats WHERE dataset_id = dataset.id AND field_name = ANY(unused);
        RAISE NOTICE '0030: dataset % (%): deleted % objects whose image was deleted since 0029', dataset.id,
            dataset.name, deleted;
    END LOOP;
END $$;

-- 3. The key, checked here in one pass.
DO $$
DECLARE
    began TIMESTAMPTZ := clock_timestamp();
BEGIN
    ALTER TABLE samples ADD CONSTRAINT samples_parent_fkey
        FOREIGN KEY (dataset_id, parent_id) REFERENCES samples (dataset_id, id) ON DELETE CASCADE;
    RAISE NOTICE '0030: checked every object''s image in % s', round(extract(epoch FROM clock_timestamp() - began)::numeric, 1);
END $$;
